from __future__ import annotations

import ast
import datetime
import ipaddress
import json
import socket
import ssl
import struct
import threading
import urllib.request
from pathlib import Path

import pytest

import alemtv_app
import hisense_vidaa
import msb_callback
from core import Api


ROOT = Path(__file__).resolve().parents[1]


def _make_server_ssl_context(tmp_path: Path) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '127.0.0.1')])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / 'broker.crt'
    key_path = tmp_path / 'broker.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx


class _MockVidaaBroker:
    """Minimal MQTT 3.1.1 over TLS mock implementing VIDAA topics & token expiry."""

    def __init__(self, tmp_path: Path, apps=None, expire_access_token: str | None = None, ignore_app_install: bool = False):
        self.ssl_ctx = _make_server_ssl_context(tmp_path)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.apps = list(apps or [
            {'appId': 'netflix', 'name': 'Netflix', 'url': 'https://www.netflix.com'},
        ])
        self.expire_access_token = expire_access_token
        self.ignore_app_install = bool(ignore_app_install)
        self.rejected_stale_once = False
        self.received = []
        self.expected_pin = 4821
        self._conns = []
        self._handlers = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for conn in list(self._conns):
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for t in list(self._handlers):
            if t.is_alive():
                t.join(timeout=2.0)
        for conn in list(self._conns):
            try:
                conn.close()
            except OSError:
                pass
        self._thread.join(timeout=2.0)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                raw, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn = self.ssl_ctx.wrap_socket(raw, server_side=True)
            except OSError:
                raw.close()
                continue
            self._conns.append(conn)
            t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            self._handlers.append(t)
            t.start()

    @staticmethod
    def _read_exact(conn, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError('eof')
            buf.extend(chunk)
        return bytes(buf)

    def _read_packet(self, conn):
        first = self._read_exact(conn, 1)[0]
        mult = 1
        rem = 0
        while True:
            b = self._read_exact(conn, 1)[0]
            rem += (b & 0x7F) * mult
            if (b & 0x80) == 0:
                break
            mult *= 128
        body = self._read_exact(conn, rem) if rem else b''
        return first, body

    @staticmethod
    def _send_publish(conn, topic: str, payload: bytes):
        tb = topic.encode('utf-8')
        vh = struct.pack('!H', len(tb)) + tb
        body = vh + payload
        rem = len(body)
        enc = bytearray([0x30])
        while True:
            d = rem % 128
            rem //= 128
            if rem > 0:
                d |= 0x80
            enc.append(d)
            if rem == 0:
                break
        conn.sendall(bytes(enc) + body)

    def _handle_client(self, conn):
        conn.settimeout(0.5)
        cid = ''
        try:
            while not self._stop.is_set():
                try:
                    hdr, body = self._read_packet(conn)
                except socket.timeout:
                    continue
                except (EOFError, OSError):
                    break
                ptype = (hdr >> 4) & 0x0F
                if ptype == 1:  # CONNECT
                    pos = 0
                    plen = struct.unpack('!H', body[pos:pos + 2])[0]
                    pos += 2 + plen
                    _proto_level = body[pos]
                    flags = body[pos + 1]
                    pos += 4  # proto_level + flags + keepalive(2)
                    clen = struct.unpack('!H', body[pos:pos + 2])[0]
                    pos += 2
                    cid = body[pos:pos + clen].decode('utf-8')
                    pos += clen
                    uname = pwd = ''
                    if flags & 0x80:
                        ulen = struct.unpack('!H', body[pos:pos + 2])[0]
                        pos += 2
                        uname = body[pos:pos + ulen].decode('utf-8')
                        pos += ulen
                    if flags & 0x40:
                        pwlen = struct.unpack('!H', body[pos:pos + 2])[0]
                        pos += 2
                        pwd = body[pos:pos + pwlen].decode('utf-8')
                        pos += pwlen
                    if self.expire_access_token and pwd == self.expire_access_token and not self.rejected_stale_once:
                        self.rejected_stale_once = True
                        conn.sendall(bytes([0x20, 0x02, 0x00, 0x05]))
                        break
                    conn.sendall(bytes([0x20, 0x02, 0x00, 0x00]))
                elif ptype == 8:  # SUBSCRIBE
                    pid = struct.unpack('!H', body[:2])[0]
                    conn.sendall(bytes([0x90, 0x03, (pid >> 8) & 0xFF, pid & 0xFF, 0x00]))
                elif ptype == 12:  # PINGREQ
                    conn.sendall(bytes([0xD0, 0x00]))
                elif ptype == 14:  # DISCONNECT
                    break
                elif ptype == 3:  # PUBLISH
                    tlen = struct.unpack('!H', body[:2])[0]
                    topic = body[2:2 + tlen].decode('utf-8')
                    pos = 2 + tlen
                    if hdr & 0x06:
                        pid = struct.unpack('!H', body[pos:pos + 2])[0]
                        pos += 2
                        conn.sendall(bytes([0x40, 0x02, (pid >> 8) & 0xFF, pid & 0xFF]))
                    payload = body[pos:].decode('utf-8', errors='replace')
                    self.received.append((topic, payload))
                    self._dispatch(conn, cid, topic, payload)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, conn, cid, topic, payload):
        if topic.endswith('/actions/gettvstate') or topic.endswith('/actions/vidaa_app_connect'):
            self._send_publish(
                conn,
                f'/remoteapp/mobile/{cid}/ui_service/data/authentication',
                json.dumps({'result': 0}).encode('utf-8'),
            )
        elif topic.endswith('/actions/authenticationcode'):
            data = json.loads(payload)
            ok = int(data.get('authNum', -1)) == self.expected_pin
            self._send_publish(
                conn,
                f'/remoteapp/mobile/{cid}/ui_service/data/authenticationcode',
                json.dumps({'result': 1 if ok else 0}).encode('utf-8'),
            )
        elif topic.endswith('/data/gettoken'):
            req = json.loads(payload) if payload else {}
            is_refresh = bool(req.get('refreshtoken'))
            self._send_publish(
                conn,
                f'/remoteapp/mobile/{cid}/platform_service/data/tokenissuance',
                json.dumps({
                    'accesstoken': 'fresh-access-2' if is_refresh else 'initial-access-1',
                    'refreshtoken': 'refresh-tok-2' if is_refresh else 'refresh-tok-1',
                    'accesstoken_duration_day': 2,
                    'refreshtoken_duration_day': 30,
                }).encode('utf-8'),
            )
        elif topic.endswith('/actions/uievent'):
            data = json.loads(payload)
            if data.get('type') == 'app_install' and not self.ignore_app_install:
                info = data.get('app_info', {})
                self.apps.append({
                    'appId': info.get('Id', ''),
                    'name': info.get('Title', ''),
                    'url': info.get('URL', ''),
                })
        elif topic.endswith('/actions/applist'):
            self._send_publish(
                conn,
                f'/remoteapp/mobile/{cid}/ui_service/data/applist',
                json.dumps(self.apps).encode('utf-8'),
            )


def test_vidaa_bundle_and_ssl_context():
    cert_pem, key_pem = hisense_vidaa.load_client_pem_key()
    assert b'BEGIN CERTIFICATE' in cert_pem
    assert b'PRIVATE KEY' in key_pem
    ctx = hisense_vidaa.create_vidaa_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)


def test_session_credentials_and_profile_selection():
    c_his, u_his, p_his = hisense_vidaa.session_credentials('AA:BB:CC:DD:EE:FF', 0, auth_profile='static', brand='his')
    c_tos, _, _ = hisense_vidaa.session_credentials('AA:BB:CC:DD:EE:FF', 0, auth_profile='static', brand='tos')
    c_mod, u_mod, p_mod = hisense_vidaa.session_credentials('AA:BB:CC:DD:EE:FF', 1700000000, auth_profile='modern', brand='his')

    assert '$his$' in c_his and u_his == 'hisenseservice' and p_his == 'multimqttservice'
    assert '$tos$' in c_tos
    assert u_mod.startswith('his$') and len(p_mod) == 32
    assert hisense_vidaa.select_profile_order('auto', brand='his') == ['static', 'modern', 'middle', 'remotenow']
    assert hisense_vidaa.select_profile_order('auto', brand='his', transport_protocol=3300) == [
        'modern', 'static', 'middle', 'remotenow',
    ]
    assert hisense_vidaa.select_profile_order('middle', brand='his')[0] == 'middle'


def test_descriptor_xml_payloads_and_urls():
    xml = (
        '<root xmlns="urn:schemas-upnp-org:device-1-0">'
        '<device><UDN>uuid:hisense-123</UDN><friendlyName>Living Room TV</friendlyName>'
        '<modelName>55U7HQ</modelName><manufacturer>Hisense</manufacturer>'
        '<modelDescription>brand=his\ntransport_protocol=3300\nvidaa_support=1\nmacwifi=aa:bb:cc:dd:ee:01</modelDescription>'
        '</device></root>'
    )
    info = hisense_vidaa.parse_descriptor_xml('192.168.1.50', xml, tv_ts=1700000000)
    assert info.is_vidaa is True
    assert info.brand == 'his'
    assert info.transport_protocol == 3300
    assert info.friendly_name == 'Living Room TV'
    assert info.mac == 'aa:bb:cc:dd:ee:01'

    tos_xml = (
        '<root><device><UDN>uuid:hisense-tos</UDN><friendlyName>Toshiba</friendlyName>'
        '<modelName>50C350</modelName><manufacturer>Toshiba</manufacturer>'
        '<modelDescription>brand=tos\nvidaa_support=1</modelDescription></device></root>'
    )
    tos = hisense_vidaa.parse_descriptor_xml('192.168.1.51', tos_xml)
    assert tos.is_vidaa is True
    assert tos.brand == 'tos'

    non_vidaa = hisense_vidaa.parse_descriptor_xml('1.2.3.4', '<root><device><UDN>uuid:roku</UDN></device></root>')
    assert non_vidaa.is_vidaa is False

    raw = hisense_vidaa.install_payload('tm.belet.vidaa', 'Belet', 'https://belet.tm/', 'https://pc/icon.png')
    payload = json.loads(raw)
    assert payload['type'] == 'app_install'
    assert payload['app_info']['Id'] == 'tm.belet.vidaa'
    assert payload['app_info']['URL'] == 'https://belet.tm/'

    assert hisense_vidaa.validate_app_url('https://belet.tm/') == 'https://belet.tm/'
    assert hisense_vidaa.validate_app_url('data:text/html;base64,PGgxPk9LPC9oMT4=')
    with pytest.raises(ValueError):
        hisense_vidaa.validate_app_url('javascript:alert(1)')
    assert hisense_vidaa.resolve_vidaa_key('back') == 'KEY_RETURNS'
    assert hisense_vidaa.resolve_vidaa_key('KEY_OK') == 'KEY_OK'


def test_real_mqtt_tls_pairing_install_token_refresh_and_keys(tmp_path, monkeypatch):
    broker = _MockVidaaBroker(tmp_path, expire_access_token='initial-access-1')
    monkeypatch.setattr(hisense_vidaa, 'MQTT_PORT', broker.port)
    monkeypatch.setattr(hisense_vidaa, 'fetch_tv_descriptor', lambda ip, timeout=2.0: hisense_vidaa.VidaaTvInfo(
        ip=ip, friendly_name='Hisense 55U7HQ', model_name='55U7HQ', brand='his', transport_protocol=3300,
        is_vidaa=True, tv_ts=1700000000,
    ))
    logs = []
    try:
        store = hisense_vidaa.HisenseSessionStore(str(tmp_path / 'hisense_sessions.json'))
        ctrl = hisense_vidaa.HisenseController(store, logs.append)

        res = ctrl.start_pairing('127.0.0.1', auth_profile='modern')
        assert res['ok'] and res['status'] == 'pin_required'
        pid = res['pairing_id']

        paired = ctrl.confirm_pairing(pid, '4821')
        assert paired['ok'] and paired['status'] == 'paired'
        assert store.get('127.0.0.1').access_token == 'initial-access-1'

        # Broker rejects 'initial-access-1' once with CONNACK rc=5 -> controller refreshes via refresh_token!
        inst = ctrl.install_tile(
            '127.0.0.1',
            app_id='tmalemtvvidaa',
            title='Alem TV',
            url='data:text/html;base64,PGgxPkFsZW08L2gxPg==',
            icon_url='http://192.168.1.2:41731/assets/alemtv_large_icon.png',
            wait_confirm=True,
            auto_launch=True,
        )
        assert inst['ok'] is True
        assert inst['confirmed'] is True
        assert broker.rejected_stale_once is True
        assert store.get('127.0.0.1').access_token == 'fresh-access-2'

        assert ctrl.send_key('127.0.0.1', 'back')['key'] == 'KEY_RETURNS'
        assert ctrl.show_toast('127.0.0.1', 'Hello VIDAA')['ok'] is True

        topics_sent = [t for t, _ in broker.received]
        assert any(t.endswith('/actions/sendkey') for t in topics_sent)
        assert any(t.endswith('/actions/launchapp') for t in topics_sent)
        assert any(t.endswith('/actions/showmessage') for t in topics_sent)
        key_payload = [p for t, p in broker.received if t.endswith('/actions/sendkey')][0]
        assert key_payload == 'KEY_RETURNS'
    finally:
        broker.close()


def test_hisense_portal_server_and_dns_bridge():
    logs = []
    portal = hisense_vidaa.HisensePortalServer(logs.append)
    res = portal.start_vidaahub_bridge(
        tv_ip='127.0.0.1',
        upstream_dns='127.0.0.1',
        alem_portal='http://iptv.tmalem.tv',
        dns_port=0,
        https_port=0,
        http80_port=0,
    )
    assert res['ok'] is True
    assert res['vidaahub_running'] is True
    base = f'http://127.0.0.1:{res["port"]}'
    https_base = f'https://127.0.0.1:{res["https_port"]}'
    try:
        for path, must_contain in (
            ('/', 'NEON · HISENSE VIDAA INSTALLER'),
            ('/vidaa/selfinstall.html', 'Hisense_installApp'),
            ('/vidaa/selfinstall.html', 'HiUtils_createRequest'),
            ('/vidaa/selfinstall.html', '/apps/belet/standalone.html'),
            ('/apps/msbcallback', 'Meryosab Callback'),
            ('/apps/alemtv', 'Alem TV'),
            ('/apps/belet/', 'vidaa-shim.js'),
            ('/apps/belet/standalone.html', 'beletWebosIdentity'),
        ):
            with urllib.request.urlopen(base + path, timeout=3) as resp:
                assert resp.status == 200
                text = resp.read().decode('utf-8')
                assert must_contain in text

        unverified = ssl._create_unverified_context()
        with urllib.request.urlopen(https_base + '/vidaa/selfinstall.html', context=unverified, timeout=3) as resp:
            assert resp.status == 200
            assert 'Hisense_installApp' in resp.read().decode('utf-8')

        for png_path in ('/assets/belet_large_icon.png', '/assets/msb_icon.png', '/assets/alemtv_large_icon.png'):
            with urllib.request.urlopen(base + png_path, timeout=3) as resp:
                assert resp.status == 200
                assert resp.read().startswith(b'\x89PNG\r\n\x1a\n')

        # Test real UDP DNS responder for vidaahub.com
        qname = b'\x08vidaahub\x03com\x00'
        pkt = struct.pack('!HHHHHH', 0x1234, 0x0100, 1, 0, 0, 0) + qname + struct.pack('!HH', 1, 1)
        cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cli.settimeout(2.0)
        cli.sendto(pkt, ('127.0.0.1', res['dns_port']))
        reply, _ = cli.recvfrom(512)
        cli.close()
        assert reply[:2] == b'\x12\x34'
        assert reply[-4:] == socket.inet_aton(res['pc_ip'])
    finally:
        portal.stop()


def test_core_api_hisense_installers_and_alemtv_vidaa_html(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    alem_html = alemtv_app.build_alemtv_vidaa_html('http://iptv.tmalem.tv')
    assert '<link rel="stylesheet"' not in alem_html
    assert 'src="app.js"' not in alem_html
    assert 'code === 8' in alem_html
    assert 'data:image/png;base64,' in alem_html
    alem_data = alemtv_app.build_alemtv_vidaa_data_url('http://iptv.tmalem.tv')
    assert alem_data.startswith('data:text/html;charset=utf-8;base64,')

    broker = _MockVidaaBroker(tmp_path)
    monkeypatch.setattr(hisense_vidaa, 'MQTT_PORT', broker.port)
    monkeypatch.setattr(hisense_vidaa, 'fetch_tv_descriptor', lambda ip, timeout=2.0: hisense_vidaa.VidaaTvInfo(
        ip=ip, friendly_name='Hisense Test', model_name='VIDAA', brand='his', transport_protocol=3300,
        is_vidaa=True, tv_ts=1700000000,
    ))
    api = Api()
    try:
        p1 = api.start_hisense_pairing('127.0.0.1', 'modern')
        assert p1['ok'] and p1['status'] == 'pin_required'
        p2 = api.confirm_hisense_pairing(p1['pairing_id'], '4821')
        assert p2['ok'] and p2['status'] == 'paired'

        # Default Belet VIDAA install deploys full-featured Belet client (not https://belet.tm/)
        belet = api.install_hisense_belet('127.0.0.1', mode='lan_server', launch=True)
        assert belet['ok'] and belet['confirmed']
        assert belet['url'].endswith('/apps/belet/')

        belet_data = api.install_hisense_belet('127.0.0.1', mode='data_url', launch=False)
        assert belet_data['ok'] and belet_data['url'].startswith('data:text/html;charset=utf-8;base64,')

        belet_custom = api.install_hisense_belet('127.0.0.1', 'https://belet.tm/', False)
        assert belet_custom['ok'] and belet_custom['url'] == 'https://belet.tm/'

        alem_data_res = api.install_hisense_alemtv('127.0.0.1', 'http://iptv.tmalem.tv', 'data_url', False)
        assert alem_data_res['ok'] and alem_data_res['url'].startswith('data:text/html;')

        alem_lan_res = api.install_hisense_alemtv('127.0.0.1', 'http://iptv.tmalem.tv', 'lan_server', False)
        assert alem_lan_res['ok'] and '/apps/alemtv' in alem_lan_res['url']

        cb_data_res = api.install_hisense_callback('127.0.0.1', 'data_url', '', False)
        assert cb_data_res['ok'] and cb_data_res['url'].startswith('data:text/html;')

        cb_flash_res = api.install_hisense_callback('127.0.0.1', 'tv_flash', '', False)
        assert cb_flash_res['ok'] and cb_flash_res['portal_url'].startswith('http://')

        custom_res = api.install_hisense_custom_app('127.0.0.1', 'my.tv', 'My TV', 'https://example.com/', '', False)
        assert custom_res['ok'] and custom_res['confirmed']

        apps_res = api.list_hisense_apps('127.0.0.1')
        assert apps_res['ok']
        ids = {a['appId'] for a in apps_res['apps']}
        assert {'tm.belet.vidaa', alemtv_app.ALEMTV_VIDAA_ID, msb_callback.HISENSE_ID, 'my.tv'}.issubset(ids)

        logs = api.read_hisense_logs()['logs']
        assert len(logs) > 0
        assert api.read_hisense_logs()['logs'] == []  # drained
    finally:
        api.close()
        broker.close()


def test_desktop_ui_and_build_scripts_include_hisense_support():
    ui_html = (ROOT / 'ui.html').read_text(encoding='utf-8')
    for token in (
        'tabHis', 'hisPage', 'hisIp', 'hisPinWrap', 'hisBeletMode', 'hisInstallBelet()',
        'hisInstallAlem()', 'hisInstallCallback()', 'installCallbackHisense()',
        'hisStartPortal(', 'read_hisense_logs',
    ):
        assert token in ui_html

    # Parse TR dict from ui_qt.py via AST so test works even in headless OpenGL-less Linux runners
    ui_qt_tree = ast.parse((ROOT / 'ui_qt.py').read_text(encoding='utf-8'))
    tr_dict = None
    for node in ui_qt_tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'TR':
                    tr_dict = ast.literal_eval(node.value)
    assert isinstance(tr_dict, dict)
    assert set(tr_dict['en']) == set(tr_dict['ru'])
    for lang in ('en', 'ru'):
        for key in (
            'nav_his', 'page_his_title', 'his_lbl_belet_mode', 'his_btn_belet_install',
            'his_btn_alem_install', 'his_btn_cb_install', 'his_card_portal',
        ):
            assert key in tr_dict[lang], f'Missing TR[{lang}][{key}]'

    legacy = (ROOT / 'app_legacy.py').read_text(encoding='utf-8')
    for sym in (
        'def _build_hisense_page', 'def his_start_pairing', 'def his_confirm_pin',
        'def his_install_belet', 'def his_install_alem', 'def his_install_callback',
        'def _install_callback_hisense', 'def his_open_portal_on_tv', 'his_belet_mode_combo',
    ):
        assert sym in legacy

    for build_rel in (
        'build_windows.bat', 'build_windows_81.bat', 'build_linux.sh',
        '.github/workflows/build-windows.yml', '.github/workflows/build-windows-81.yml',
        '.github/workflows/build-linux.yml',
    ):
        build_txt = (ROOT / build_rel).read_text(encoding='utf-8')
        assert 'belet_samsung_native' in build_txt, f'{build_rel} must bundle belet_samsung_native'
        assert 'belet_vidaa_client' in build_txt, f'{build_rel} must bundle belet_vidaa_client'

    assert (ROOT / 'assets' / 'vidaa_client.bundle').is_file()
    assert (ROOT / 'belet_vidaa_client' / 'vidaa-shim.js').is_file()


def test_vidaa_black_screen_prevention_browser_fallback_and_hidden_urls(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    # Helper checks: mediaId + HTTP_APP_DEFAULT_URL* + find_browser_app
    parsed = hisense_vidaa.parse_applist(json.dumps([
        {'appId': '16', 'name': 'TV Browser', 'url': 'url16', 'storeType': 100},
        {'mediaId': 'tm.belet.vidaa', 'name': 'Belet', 'url': 'HTTP_APP_DEFAULT_URLtm.belet.vidaa', 'storeType': 99},
    ]))
    assert parsed[1]['appId'] == 'tm.belet.vidaa'
    browser = hisense_vidaa.find_browser_app(parsed)
    assert browser is not None and browser['appId'] == '16' and browser['storeType'] == 100
    assert hisense_vidaa.tile_matches_request(parsed[1], 'tm.belet.vidaa', 'http://pc:18880/apps/belet/', allow_hidden_url=False) is False
    assert hisense_vidaa.tile_matches_request(parsed[1], 'tm.belet.vidaa', 'http://pc:18880/apps/belet/', allow_hidden_url=True) is True
    assert json.loads(hisense_vidaa.launch_payload('16', 'TV Browser', 'http://192.168.137.1:18880/', store_type=100))['storeType'] == 100

    # Simulate VIDAA firmware that ignores app_install but has built-in TV Browser (appId=16, url='browser')
    broker = _MockVidaaBroker(
        tmp_path,
        apps=[{'appId': '16', 'name': 'TV Browser', 'url': 'browser', 'storeType': 100}],
        ignore_app_install=True,
    )
    monkeypatch.setattr(hisense_vidaa, 'MQTT_PORT', broker.port)
    monkeypatch.setattr(hisense_vidaa, 'fetch_tv_descriptor', lambda ip, timeout=2.0: hisense_vidaa.VidaaTvInfo(
        ip=ip, friendly_name='Hisense VIDAA', model_name='50A5FE', brand='his', transport_protocol=3300,
        is_vidaa=True, tv_ts=1700000000,
    ))
    logs = []
    try:
        store = hisense_vidaa.HisenseSessionStore(str(tmp_path / 'hisense_sessions.json'))
        ctrl = hisense_vidaa.HisenseController(store, logs.append)
        p1 = ctrl.start_pairing('127.0.0.1', auth_profile='modern')
        assert ctrl.confirm_pairing(p1['pairing_id'], '4821')['ok']

        # 1. Unconfirmed data: URI install MUST NOT call actions/launchapp (which causes a black screen!)
        data_res = ctrl.install_tile(
            '127.0.0.1',
            'tmalemtvvidaa',
            'Alem TV',
            'data:text/html;base64,PGgxPkFsZW08L2gxPg==',
            auto_launch=True,
            poll_delays=(0.02, 0.02),
        )
        assert data_res['ok'] is True
        assert data_res['status'] == 'sent_unconfirmed'
        assert data_res['confirmed'] is False
        assert data_res['launched'] is False
        assert data_res['browser_opened'] is False
        assert [p for t, p in broker.received if t.endswith('/actions/launchapp')] == []

        # 2. Launching uninstalled data: URI directly refuses instead of black-screening TV
        bad_launch = ctrl.launch_app('127.0.0.1', 'tmalemtvvidaa', 'Alem TV', 'data:text/html;base64,PGgxPkFsZW08L2gxPg==')
        assert bad_launch['ok'] is False
        assert bad_launch['launched'] is False
        assert [p for t, p in broker.received if t.endswith('/actions/launchapp')] == []

        # 3. Unconfirmed HTTP LAN install routes launch through real TV Browser (appId='16')
        lan_res = ctrl.install_tile(
            '127.0.0.1',
            'tm.belet.vidaa',
            'Belet',
            'http://192.168.137.1:18880/apps/belet/',
            auto_launch=True,
            poll_delays=(0.02, 0.02),
        )
        assert lan_res['ok'] is True
        assert lan_res['status'] == 'sent_unconfirmed'
        assert lan_res['browser_opened'] is True
        assert lan_res['launched'] is True
        launches = [json.loads(p) for t, p in broker.received if t.endswith('/actions/launchapp')]
        assert len(launches) == 1
        assert launches[0]['appId'] == '16'
        assert launches[0]['url'] == 'http://192.168.137.1:18880/apps/belet/'
        assert launches[0]['storeType'] == 100

        # 4. VIDAA U5 (url='url16' on TV Browser) skips fake MQTT URL launch to avoid black screen / wrong page
        broker.apps[0]['url'] = 'url16'
        broker.received.clear()
        u5_res = ctrl.install_tile(
            '127.0.0.1',
            'tm.belet.vidaa',
            'Belet',
            'http://192.168.137.1:18880/apps/belet/',
            auto_launch=True,
            poll_delays=(0.02, 0.02),
        )
        assert u5_res['ok'] is True and u5_res['launched'] is False
        assert u5_res['launch_skipped_reason'] == 'tv_browser_ignores_mqtt_url'
        assert [p for t, p in broker.received if t.endswith('/actions/launchapp')] == []

        # Restore 'browser' so Api.open_hisense_portal_on_tv exercises TV Browser launch
        broker.apps[0]['url'] = 'browser'

        # 5. Api.open_hisense_portal_on_tv opens TV Browser (appId='16'), NEVER fake 'neon.vidaa.installer'
        #    and Api.install_hisense_alemtv defaults to 'lan_server' instead of giant 'data_url'
        api = Api()
        try:
            ap1 = api.start_hisense_pairing('127.0.0.1', 'modern')
            assert api.confirm_hisense_pairing(ap1['pairing_id'], '4821')['ok']
            broker.received.clear()
            portal_res = api.open_hisense_portal_on_tv('127.0.0.1', alem_portal='http://iptv.tmalem.tv')
            assert portal_res['ok'] is True and portal_res['launched'] is True
            portal_launches = [json.loads(p) for t, p in broker.received if t.endswith('/actions/launchapp')]
            assert len(portal_launches) == 1
            assert portal_launches[0]['appId'] == '16'
            assert portal_launches[0]['url'].startswith('http://')

            monkeypatch.setattr(api._hisense_ctrl, 'install_tile', lambda *a, **kw: {
                'ok': True, 'confirmed': True, 'app_id': a[1], 'title': a[2], 'url': a[3],
            })
            default_alem = api.install_hisense_alemtv('127.0.0.1', launch=False)
            assert default_alem['ok'] is True
            assert default_alem['mode'] == 'lan_server'
            assert '/apps/alemtv?' in default_alem['url']
        finally:
            api.close()
    finally:
        broker.close()


def test_portal_preferred_ports_tv_probe_and_hosts_fallback(tmp_path):
    logs = []
    p1 = hisense_vidaa.HisensePortalServer(logs.append)
    p2 = hisense_vidaa.HisensePortalServer(logs.append)
    try:
        i1 = p1.ensure_started(tv_ip='127.0.0.1')
        i2 = p2.ensure_started(tv_ip='127.0.0.1')
        assert i1['port'] in hisense_vidaa._VIDAA_HTTP_PREFERRED_PORTS
        assert i2['port'] in hisense_vidaa._VIDAA_HTTP_PREFERRED_PORTS
        assert i1['port'] != i2['port']

        base = f'http://127.0.0.1:{i1["port"]}'
        opt_req = urllib.request.Request(f'{base}/__tv_probe', method='OPTIONS')
        with urllib.request.urlopen(opt_req, timeout=3) as resp:
            assert resp.status == 204
            assert resp.headers.get('Access-Control-Allow-Origin') == '*'

        probe_req = urllib.request.Request(
            f'{base}/__tv_probe',
            data=json.dumps({
                'page': 'selfinstall',
                'model': '55U7HQ',
                'osVersion': 'VIDAA6.0',
                'hasHiUtils': False,
                'hasInstallApp': False,
            }).encode('utf-8'),
            method='POST',
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(probe_req, timeout=3) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode('utf-8'))['ok'] is True
        assert p1.last_tv_probe['model'] == '55U7HQ'
        assert any('[TV PROBE' in line and '55U7HQ' in line for line in logs)

        # hosts fallback safely adds and cleanly removes only NEON VIDAAHUB block
        hosts_file = tmp_path / 'hosts'
        hosts_file.write_text('127.0.0.1 localhost\n# user entry\n10.0.0.5 mybox\n', encoding='utf-8')
        ok_h, _ = hisense_vidaa._hosts_set_vidaahub('192.168.137.1', hosts_path=str(hosts_file))
        assert ok_h is True
        written = hosts_file.read_text(encoding='utf-8')
        assert '192.168.137.1 vidaahub.com' in written
        assert '10.0.0.5 mybox' in written

        assert hisense_vidaa._hosts_clear_vidaahub(hosts_path=str(hosts_file)) is True
        cleared = hosts_file.read_text(encoding='utf-8')
        assert 'vidaahub.com' not in cleared
        assert hisense_vidaa._HOSTS_BEGIN_MARKER not in cleared
        assert '10.0.0.5 mybox' in cleared
    finally:
        p2.stop()
        p1.stop()
