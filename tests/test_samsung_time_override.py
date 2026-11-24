import ast
import base64
import http.server
import json
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from core import Api
import samsung_time_override as sto
import samsung_tv


def _start_mock_upstream_dns(records):
    """Run a tiny UDP DNS responder returning A records from `records` dict."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                q = sto.parse_dns_question(data)
                ip = records.get(q['normalized_name'])
                if ip and q['qtype'] == 1:
                    resp = sto.build_dns_a_response(q, ip, ttl=60)
                else:
                    resp = struct.pack('!HHHHHH', q['txid'], 0x8183, 1, 0, 0, 0) + q['question_wire']
                sock.sendto(resp, addr)
            except Exception:
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    def close():
        stop.set()
        try:
            sock.close()
        except OSError:
            pass
        t.join(timeout=1.0)

    return port, close


def _start_mock_ntp_server(offset_sec=5.0):
    """Run a tiny UDP NTP server shifted by `offset_sec` from local clock."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 48:
                continue
            now_s = time.time() + offset_sec
            rx_s, rx_f = sto._unix_to_ntp(now_s)
            tx_s, tx_f = sto._unix_to_ntp(now_s)
            reply = bytearray(48)
            reply[0] = 0x1C  # LI=0, VN=3, Mode=4
            reply[1] = 1
            reply[12:16] = b'TEST'
            struct.pack_into('!II', reply, 16, rx_s, rx_f)
            reply[24:32] = data[40:48]
            struct.pack_into('!IIII', reply, 32, rx_s, rx_f, tx_s, tx_f)
            sock.sendto(bytes(reply), addr)

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    def close():
        stop.set()
        try:
            sock.close()
        except OSError:
            pass
        t.join(timeout=1.0)

    return port, close


def _udp_dns_query(host, port, domain, qtype=1):
    txid, pkt = sto.build_dns_query_packet(domain, qtype=qtype)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    try:
        sock.sendto(pkt, (host, port))
        data, _ = sock.recvfrom(4096)
    finally:
        sock.close()
    r_txid, r_flags, qd, an, _ns, _ar = struct.unpack('!HHHHHH', data[:12])
    assert r_txid == txid
    pos = 12
    for _ in range(qd):
        _, pos = sto._decode_dns_name(data, pos)
        pos += 4
    answers = []
    for _ in range(an):
        _, pos = sto._decode_dns_name(data, pos)
        rtype, rclass, ttl, rdlen = struct.unpack('!HHIH', data[pos:pos + 10])
        pos += 10
        rdata = data[pos:pos + rdlen]
        pos += rdlen
        if rtype == 1 and rdlen == 4:
            answers.append(('A', socket.inet_ntoa(rdata), ttl))
        else:
            answers.append((rtype, rdata, ttl))
    return r_flags & 0x0F, answers


def _tcp_dns_query(host, port, domain, qtype=1):
    txid, pkt = sto.build_dns_query_packet(domain, qtype=qtype)
    conn = socket.create_connection((host, port), timeout=3.0)
    try:
        conn.sendall(struct.pack('!H', len(pkt)) + pkt)
        hdr = conn.recv(2)
        msg_len = struct.unpack('!H', hdr)[0]
        buf = bytearray()
        while len(buf) < msg_len:
            chunk = conn.recv(msg_len - len(buf))
            assert chunk
            buf.extend(chunk)
        data = bytes(buf)
    finally:
        conn.close()
    r_txid, r_flags, qd, an, _ns, _ar = struct.unpack('!HHHHHH', data[:12])
    assert r_txid == txid
    assert (r_flags & 0x0F) == 0
    pos = 12
    for _ in range(qd):
        _, pos = sto._decode_dns_name(data, pos)
        pos += 4
    assert an == 1
    _, pos = sto._decode_dns_name(data, pos)
    rtype, _rclass, _ttl, rdlen = struct.unpack('!HHIH', data[pos:pos + 10])
    pos += 10
    assert rtype == 1 and rdlen == 4
    return socket.inet_ntoa(data[pos:pos + 4])


def test_normalize_target_and_parse_sources():
    assert sto.normalize_time_target('time.cloudflare.com')['host'] == 'time.cloudflare.com'
    assert sto.normalize_time_target('local')['is_local'] is True
    u = sto.normalize_time_target('https://time.example.org:8443/openapi/timesync')
    assert u['scheme'] == 'https'
    assert u['host'] == 'time.example.org'
    assert u['port'] == 8443
    assert sto.normalize_time_target('192.168.1.50')['is_ip'] is True

    with pytest.raises(ValueError):
        sto.normalize_time_target('')
    with pytest.raises(ValueError):
        sto.normalize_time_target('0.0.0.0')
    with pytest.raises(ValueError):
        sto.normalize_time_target('ftp://bad.example.com')

    srcs = sto.parse_source_domains('time.samsungcloudsolution.com, custom.time.tv', intercept_check=True)
    assert 'time.samsungcloudsolution.com' in srcs
    assert 'custom.time.tv' in srcs
    assert 'cdn.samsungcloudsolution.com' in srcs

    srcs_no_chk = sto.parse_source_domains('time.samsungcloudsolution.com', intercept_check=False)
    assert srcs_no_chk == ['time.samsungcloudsolution.com']

    assert sto.parse_upstream_dns('127.0.0.1, 1.1.1.1, 8.8.8.8') == ['1.1.1.1', '8.8.8.8']
    assert sto.parse_upstream_dns('127.0.0.1', allow_loopback=True) == ['127.0.0.1']


def test_gateway_end_to_end_dns_http_timesync_checkxml_ntp():
    up_port, close_up = _start_mock_upstream_dns({'example.org': '93.184.216.34'})
    ntp_port, close_ntp = _start_mock_ntp_server(offset_sec=12.0)

    # Mock a custom HTTP /openapi/timesync upstream server shifted +12s via NTP or HTTP
    class MockRemoteHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            ms = int(round((time.time() + 12.0) * 1000.0))
            body = struct.pack('<QQ', ms, ms)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    remote_srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), MockRemoteHandler)
    remote_http_port = remote_srv.server_address[1]
    remote_thread = threading.Thread(target=remote_srv.serve_forever, daemon=True)
    remote_thread.start()

    srv = sto.SamsungTimeOverrideServer()
    try:
        # Also verify SNTP client against mock NTP server
        sntp = sto.fetch_sntp_time('127.0.0.1', port=ntp_port, upstream_dns=['127.0.0.1'])
        assert 10.5 <= sntp['offset_sec'] <= 13.5

        status = srv.start(
            target=f'http://127.0.0.1:{remote_http_port}',
            mode='gateway',
            source_domains='time.samsungcloudsolution.com, time.samsungcloudsolution.net',
            intercept_check=True,
            upstream_dns=['127.0.0.1'],
            upstream_port=up_port,
            allow_loopback_upstream=True,
            advertise_ip='192.168.1.45',
            dns_bind_ip='127.0.0.1',
            dns_port=0,
            http_bind_ip='127.0.0.1',
            http_port=0,
            ntp_port=0,
            manage_firewall=False,
        )
        assert status['running'] is True
        assert 10500 <= status['offset_ms'] <= 13500
        d_port = status['dns_port']
        h_port = status['http_port']
        n_port = status['ntp_port']
        assert d_port > 0 and h_port > 0 and n_port > 0

        # 1. Samsung domains intercepted over UDP & TCP DNS
        rcode, ans = _udp_dns_query('127.0.0.1', d_port, 'Time.SamsungCloudSolution.COM', qtype=1)
        assert rcode == 0 and ans == [('A', '192.168.1.45', 30)]

        rcode_chk, ans_chk = _udp_dns_query('127.0.0.1', d_port, 'cdn.samsungcloudsolution.com', qtype=1)
        assert rcode_chk == 0 and ans_chk == [('A', '192.168.1.45', 30)]

        # 2. AAAA returns NOERROR NODATA so TV uses overridden IPv4
        rcode_aaaa, ans_aaaa = _udp_dns_query('127.0.0.1', d_port, 'time.samsungcloudsolution.com', qtype=28)
        assert rcode_aaaa == 0 and ans_aaaa == []

        # 3. TCP DNS works cleanly too
        assert _tcp_dns_query('127.0.0.1', d_port, 'time.samsungcloudsolution.net') == '192.168.1.45'

        # 4. Non-Samsung domains forward to upstream DNS
        rcode_fwd, ans_fwd = _udp_dns_query('127.0.0.1', d_port, 'example.org', qtype=1)
        assert rcode_fwd == 0 and ans_fwd == [('A', '93.184.216.34', 60)]

        # 5. HTTP /openapi/timesync returns exact 16-byte <QQ epoch milliseconds
        t_before = time.time()
        with urllib.request.urlopen(f'http://127.0.0.1:{h_port}/openapi/timesync?client=T20O', timeout=3) as resp:
            assert resp.status == 200
            assert resp.headers.get('Date')
            payload = resp.read()
        assert len(payload) == 16
        ms1, ms2 = struct.unpack('<QQ', payload)
        assert ms1 == ms2
        assert (t_before + 10.0) * 1000.0 <= ms1 <= (time.time() + 14.0) * 1000.0

        # 6. HTTP check.xml & timezoneoffset
        with urllib.request.urlopen(f'http://127.0.0.1:{h_port}/Public/network/files/check.xml', timeout=3) as resp:
            assert resp.status == 200
            assert resp.read().strip() == b'ok'
        with urllib.request.urlopen(f'http://127.0.0.1:{h_port}/openapi/zipcode/timezoneoffset', timeout=3) as resp:
            assert resp.status == 200
            tz_data = json.loads(resp.read().decode('utf-8'))
            assert 'offset' in tz_data

        # 7. Built-in UDP NTP responder reflects synchronized offset
        ntp_res = sto.fetch_sntp_time('127.0.0.1', port=n_port, upstream_dns=['127.0.0.1'])
        assert 10.5 <= ntp_res['offset_sec'] <= 13.5

        final = srv.get_status()
        assert final['stats']['dns_overridden'] >= 4
        assert final['stats']['dns_forwarded'] == 1
        assert final['stats']['http_timesync'] == 1
        assert final['stats']['http_check'] == 1
        assert final['stats']['http_tz'] == 1
        assert final['stats']['ntp_hits'] == 1
    finally:
        srv.stop()
        remote_srv.shutdown()
        remote_srv.server_close()
        close_up()
        close_ntp()
    assert srv.get_status()['running'] is False


def test_dns_direct_mode_rewrites_to_custom_domain_ip():
    up_port, close_up = _start_mock_upstream_dns({'my-custom-ntp.example': '203.0.113.77'})
    srv = sto.SamsungTimeOverrideServer()
    try:
        st = srv.start(
            target='my-custom-ntp.example',
            mode='dns_direct',
            source_domains='time.samsungcloudsolution.com',
            intercept_check=False,
            upstream_dns=['127.0.0.1'],
            upstream_port=up_port,
            allow_loopback_upstream=True,
            dns_bind_ip='127.0.0.1',
            dns_port=0,
            manage_firewall=False,
        )
        assert st['running'] is True
        assert st['mode'] == 'dns_direct'
        assert st['direct_ip'] == '203.0.113.77'
        assert st['http_port'] == 0
        assert st['ntp_port'] == 0

        rcode, ans = _udp_dns_query('127.0.0.1', st['dns_port'], 'time.samsungcloudsolution.com', qtype=1)
        assert rcode == 0 and ans == [('A', '203.0.113.77', 30)]
    finally:
        srv.stop()
        close_up()


def test_api_samsung_time_settings_and_close(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    api = Api()
    st0 = api.get_samsung_time_status()
    assert st0['running'] is False
    assert st0['target'] == 'time.cloudflare.com'

    sync_res = api.sync_samsung_time_now('local')
    assert sync_res['ok'] is True
    assert sync_res['method'] == 'local-pc'
    assert sync_res['offset_ms'] == 0

    saved = api.save_samsung_time_settings(
        target='pool.ntp.org',
        mode='gateway',
        source_domains='time.samsungcloudsolution.com, custom.time.test',
        intercept_check=False,
        upstream_dns='9.9.9.9',
    )
    assert saved['target'] == 'pool.ntp.org'
    assert saved['intercept_check'] is False

    loaded = api.load_settings()
    assert loaded['samsung_time_target'] == 'pool.ntp.org'
    assert loaded['samsung_time_sources'] == 'time.samsungcloudsolution.com, custom.time.test'
    assert loaded['samsung_time_intercept_check'] is False
    assert loaded['samsung_time_upstream_dns'] == '9.9.9.9'

    # Reject loopback self-redirect target inside intercepted domains
    bad = api.start_samsung_time_override(
        target='time.samsungcloudsolution.com',
        mode='gateway',
        source_domains='time.samsungcloudsolution.com',
    )
    assert bad['ok'] is False

    # Close cleanly tears down any active time server
    stopped = api.stop_samsung_time_override()
    assert stopped['running'] is False
    api.close()


def _free_port(sock_type=socket.SOCK_DGRAM):
    for _ in range(12):
        s = socket.socket(socket.AF_INET, sock_type)
        s.bind(('127.0.0.1', 0))
        p = s.getsockname()[1]
        s.close()
        # Verify both UDP and TCP bind cleanly for DNS port
        if sock_type == socket.SOCK_DGRAM:
            try:
                t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                t.bind(('127.0.0.1', p))
                t.close()
                return p
            except OSError:
                continue
        return p
    raise RuntimeError('No free port found')


def test_auto_stop_after_sync_shuts_down_after_timesync():
    srv = sto.SamsungTimeOverrideServer()
    try:
        st = srv.start(
            target='local',
            mode='gateway',
            source_domains='time.samsungcloudsolution.com',
            intercept_check=True,
            dns_bind_ip='127.0.0.1',
            dns_port=0,
            http_bind_ip='127.0.0.1',
            http_port=0,
            ntp_port=0,
            manage_firewall=False,
            auto_stop_after_sync=True,
        )
        assert st['running'] is True
        assert st['auto_stop_after_sync'] is True
        h_port = st['http_port']
        with urllib.request.urlopen(f'http://127.0.0.1:{h_port}/openapi/timesync', timeout=3) as resp:
            assert resp.status == 200
            assert len(resp.read()) == 16

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            cur = srv.get_status()
            if not cur['running']:
                break
            time.sleep(0.05)
        cur = srv.get_status()
        assert cur['running'] is False
        assert '127.0.0.1' in cur['auto_stopped_reason']
        assert '127.0.0.1' in cur['synced_clients']
    finally:
        srv.stop()


def test_standalone_gateway_script_executes_end_to_end(tmp_path):
    script = sto.generate_standalone_gateway_script(
        target='local',
        source_domains='time.samsungcloudsolution.com, time.samsungcloudsolution.net',
        intercept_check=True,
        upstream_dns='1.1.1.1',
        advertise_ip='192.168.1.77',
    )
    ast.parse(script)
    script_path = tmp_path / 'gw.py'
    script_path.write_text(script, encoding='utf-8')

    dns_p = _free_port(socket.SOCK_DGRAM)
    http_p = _free_port(socket.SOCK_STREAM)
    ntp_p = _free_port(socket.SOCK_DGRAM)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(script_path),
            '--bind', '127.0.0.1',
            '--dns-port', str(dns_p),
            '--http-port', str(http_p),
            '--ntp-port', str(ntp_p),
            '--advertise-ip', '192.168.1.77',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait until HTTP /openapi/timesync is serving
        payload = b''
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{http_p}/openapi/timesync', timeout=1) as resp:
                    payload = resp.read()
                if len(payload) == 16:
                    break
            except Exception:
                time.sleep(0.1)
        assert len(payload) == 16
        ms1, ms2 = struct.unpack('<QQ', payload)
        assert ms1 == ms2 and ms1 > 1700000000000

        with urllib.request.urlopen(f'http://127.0.0.1:{http_p}/Public/network/files/check.xml', timeout=2) as resp:
            assert resp.read().strip() == b'ok'

        rcode, ans = _udp_dns_query('127.0.0.1', dns_p, 'time.samsungcloudsolution.com', qtype=1)
        assert rcode == 0 and ans == [('A', '192.168.1.77', 30)]

        ntp_res = sto.fetch_sntp_time('127.0.0.1', port=ntp_p, upstream_dns=['127.0.0.1'])
        assert abs(ntp_res['offset_sec']) < 5.0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_router_snippets_and_reachability_diagnostics(monkeypatch):
    snips = sto.generate_router_snippets('192.168.1.1')
    assert 'address=/time.samsungcloudsolution.com/192.168.1.1' in snips['openwrt_dnsmasq']
    assert '/ip dns static add name="time.samsungcloudsolution.com" address=192.168.1.1' in snips['mikrotik_cli']
    assert '/opt/neon-samsung-time/gateway.py' in snips['linux_systemd']

    def _fake_resolve(domain, upstreams, timeout=2.5, port=53):
        if '1.1.1.1' in upstreams:
            return '52.10.20.30'
        raise OSError('blocked')

    monkeypatch.setattr(sto, 'resolve_ipv4_via_upstream', _fake_resolve)
    monkeypatch.setattr(sto.socket, 'getaddrinfo', lambda *a, **kw: (_ for _ in ()).throw(OSError('blocked')))
    monkeypatch.setattr(
        sto,
        'probe_samsung_timesync_http',
        lambda ip, port=80, host_header='time.samsungcloudsolution.com', timeout=2.5: (
            {'ok': True, 'utc_iso': '2026-11-20T00:00:00Z', 'epoch_ms': 1795132800000}
            if ip == '52.10.20.30'
            else {'ok': False, 'error': 'timeout'}
        ),
    )
    diag = sto.diagnose_samsung_time_reachability(resolvers=['system', '1.1.1.1', '8.8.8.8'])
    assert diag['ok'] is True
    assert diag['best_public_dns'] == '1.1.1.1'
    assert '1.1.1.1' in diag['recommendation']

    # Verify default resolver list covers CIS/AdGuard/OpenDNS resolvers too
    seen_default = []
    monkeypatch.setattr(sto, 'resolve_ipv4_via_upstream', lambda d, ups, **kw: seen_default.extend(ups) or (_ for _ in ()).throw(OSError('blocked')))
    diag_def = sto.diagnose_samsung_time_reachability()
    assert diag_def['any_direct_working'] is False
    for pub in ('1.1.1.1', '8.8.8.8', '9.9.9.9', '77.88.8.8', '94.140.14.14', '208.67.222.222'):
        assert pub in seen_default


def test_deploy_samsung_time_remote_linux_openwrt_mikrotik(monkeypatch):
    import paramiko

    monkeypatch.setattr(sto, 'probe_samsung_timesync_http', lambda *a, **kw: {'ok': True, 'utc_iso': '2026-11-20T00:00:00Z'})
    monkeypatch.setattr(sto, 'resolve_ipv4_via_upstream', lambda *a, **kw: '192.168.1.1')

    def _run_deploy_for_platform(platform_kind):
        commands = []
        uploaded = {}

        class FakeSftp:
            def open(self, path, mode):
                buf = []

                class _F:
                    def write(self, data):
                        buf.append(data)

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        uploaded[path] = ''.join(buf)

                return _F()

            def close(self):
                pass

        class FakeStream:
            def __init__(self, txt='', rc=0):
                self._txt = txt.encode('utf-8')
                self.channel = type('Ch', (), {'recv_exit_status': lambda _s: rc})()

            def read(self):
                return self._txt

        class FakeSSH:
            def set_missing_host_key_policy(self, *a):
                pass

            def connect(self, *a, **kw):
                pass

            def open_sftp(self):
                return FakeSftp()

            def exec_command(self, cmd, timeout=30):
                commands.append(cmd)
                if 'uname -s' in cmd:
                    if platform_kind == 'mikrotik':
                        return None, FakeStream('name: MikroTik'), FakeStream('')
                    return None, FakeStream('Linux'), FakeStream('')
                if '/system resource print' in cmd and platform_kind == 'mikrotik':
                    return None, FakeStream('platform: MikroTik RouterOS'), FakeStream('')
                if 'openwrt_release' in cmd:
                    return None, FakeStream('OPENWRT' if platform_kind == 'openwrt' else 'LINUX'), FakeStream('')
                if 'command -v python3' in cmd:
                    return None, FakeStream('/usr/bin/python3'), FakeStream('')
                if 'command -v systemctl' in cmd:
                    return None, FakeStream('/bin/systemctl' if platform_kind == 'linux' else ''), FakeStream('')
                if 'netstat -ln' in cmd:
                    return None, FakeStream(''), FakeStream('')
                return None, FakeStream('OK'), FakeStream('')

            def close(self):
                pass

        monkeypatch.setattr(paramiko, 'SSHClient', FakeSSH)
        res = sto.deploy_samsung_time_remote(
            host='192.168.1.1',
            username='root',
            password='secret',
            port=22,
            target='time.cloudflare.com',
            upstream_dns=['1.1.1.1'],
            gateway_ip='192.168.1.1',
        )
        assert res['ok'] is True
        assert res['tv_dns_ip'] == '192.168.1.1'
        return res, commands, uploaded

    r_lin, _cmds_lin, up_lin = _run_deploy_for_platform('linux')
    assert r_lin['platform'] == 'linux-systemd'
    assert '/tmp/neon_samsung_time_gateway.py' in up_lin

    r_ow, _cmds_ow, up_ow = _run_deploy_for_platform('openwrt')
    assert r_ow['platform'] == 'openwrt'
    assert '/etc/dnsmasq.d/neon-samsung-time.conf' in up_ow
    assert '--dns-port 0' in up_ow['/etc/init.d/neon-samsung-time']

    r_mt, cmds_mt, _up_mt = _run_deploy_for_platform('mikrotik')
    assert r_mt['platform'] == 'mikrotik'
    assert any('/ip dns static add' in c for c in cmds_mt)


def test_api_export_deploy_and_samsung_send_text(tmp_path, monkeypatch):
    import paramiko

    monkeypatch.setattr(sto, 'probe_samsung_timesync_http', lambda *a, **kw: {'ok': True, 'utc_iso': '2026-11-20T00:00:00Z'})
    monkeypatch.setattr(sto, 'resolve_ipv4_via_upstream', lambda *a, **kw: '192.168.1.99')

    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    api = Api()

    out_file = tmp_path / 'my_gw.py'
    exp = api.export_samsung_time_gateway(save_path=str(out_file), target='time.cloudflare.com')
    assert exp['ok'] is True
    assert out_file.exists()
    ast.parse(out_file.read_text(encoding='utf-8'))

    # Verify deploy_samsung_time_remote never stores password inside settings.json
    class FakeSftp:
        def open(self, path, mode):
            class _F:
                def write(self, data):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            return _F()

        def close(self):
            pass

    class FakeStream:
        def __init__(self, txt='', rc=0):
            self._txt = txt.encode('utf-8')
            self.channel = type('Ch', (), {'recv_exit_status': lambda _s: rc})()

        def read(self):
            return self._txt

    class FakeStdin:
        def write(self, data):
            pass

        def flush(self):
            pass

    class FakeSSH:
        def set_missing_host_key_policy(self, *a):
            pass

        def connect(self, *a, **kw):
            pass

        def open_sftp(self):
            return FakeSftp()

        def exec_command(self, cmd, timeout=30):
            if 'uname -s' in cmd:
                return FakeStdin(), FakeStream('Linux'), FakeStream('')
            if 'openwrt_release' in cmd:
                return FakeStdin(), FakeStream('LINUX'), FakeStream('')
            if 'command -v python3' in cmd:
                return FakeStdin(), FakeStream('/usr/bin/python3'), FakeStream('')
            if 'command -v systemctl' in cmd:
                return FakeStdin(), FakeStream('/bin/systemctl'), FakeStream('')
            return FakeStdin(), FakeStream('OK'), FakeStream('')

        def close(self):
            pass

    monkeypatch.setattr(paramiko, 'SSHClient', FakeSSH)
    dep = api.deploy_samsung_time_remote(
        host='192.168.1.99',
        username='admin',
        password='SUPER_SECRET_SSH_PASSWORD',
        port=2222,
    )
    assert dep['ok'] is True
    assert dep['is_private_host'] is True

    dep_pub = api.deploy_samsung_time_remote(
        host='93.184.216.34',
        username='admin',
        password='SUPER_SECRET_SSH_PASSWORD',
        port=2222,
    )
    assert dep_pub['ok'] is True
    assert dep_pub['is_private_host'] is False
    import os
    raw_settings = open(os.path.join(api._data_dir(), 'settings.json'), 'r', encoding='utf-8').read()
    assert 'SUPER_SECRET_SSH_PASSWORD' not in raw_settings
    assert '93.184.216.34' in raw_settings

    # Verify SamsungWsClient.send_text & Api.samsung_send_text RFC-6455 wire frame formatting
    srv_sock, cli_sock = socket.socketpair()
    try:
        ws_client = samsung_tv.SamsungWsClient('192.168.1.101')
        ws_client._sock = cli_sock
        api.samsung_client = ws_client
        api.samsung_ip = '192.168.1.101'

        res_txt = api.samsung_send_text('1.1.1.1')
        assert res_txt['ok'] is True

        def _recv_ws_json(s):
            hdr = s.recv(2)
            assert (hdr[0] & 0x0F) == 0x1
            ln = hdr[1] & 0x7F
            if ln == 126:
                ln = struct.unpack('!H', s.recv(2))[0]
            mask = s.recv(4)
            raw = bytearray(s.recv(ln))
            for i in range(ln):
                raw[i] ^= mask[i % 4]
            return json.loads(bytes(raw).decode('utf-8'))

        f1 = _recv_ws_json(srv_sock)
        f2 = _recv_ws_json(srv_sock)
        assert f1['params']['TypeOfRemote'] == 'SendInputString'
        assert base64.b64decode(f1['params']['Cmd']).decode('utf-8') == '1.1.1.1'
        assert f2['params']['TypeOfRemote'] == 'SendInputEnd'
    finally:
        api.samsung_client = None
        srv_sock.close()
        cli_sock.close()
    api.close()

