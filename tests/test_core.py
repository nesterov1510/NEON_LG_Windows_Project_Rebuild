import json
import os
import subprocess
import tempfile
import threading
import time
from unittest import mock

import pytest

from core import Api


@pytest.fixture
def tmp_api(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    api = Api()
    yield api
    api.close()


def test_normalize_ip(tmp_api):
    assert tmp_api._normalize_ip(' 192.168.1.50 ') == '192.168.1.50'
    assert tmp_api._normalize_ip('http://192.168.1.50:9991/') == '192.168.1.50'
    assert tmp_api._normalize_ip('https://lg-tv.local') == 'lg-tv.local'
    assert tmp_api._normalize_ip('192.168.1.50 bad') == ''
    assert tmp_api._normalize_ip('') == ''


def test_settings_never_persist_secrets(tmp_api):
    saved = tmp_api.save_settings({
        'tv_ip': '192.168.1.77',
        'webos26': True,
        'passphrase': 'secret',
        'password': 'secret',
        'vpn_pass': 'secret',
        'hotspot_pass': 'secret',
    })
    assert saved['tv_ip'] == '192.168.1.77'
    assert saved['webos26'] is True
    for key in ('passphrase', 'password', 'vpn_pass', 'hotspot_pass'):
        assert key not in saved
    raw = open(tmp_api._settings_path(), 'r', encoding='utf-8').read()
    assert 'secret' not in raw


def test_remember_profile_preserves_existing_profiles(tmp_api, tmp_path):
    p1 = tmp_path / 'us_west.ovpn'
    p2 = tmp_path / 'sub' / 'us_east.ovpn'
    p2.parent.mkdir(parents=True)
    p1.write_text('client\n', encoding='utf-8')
    p2.write_text('client\n', encoding='utf-8')

    # Pre-seed profiles.json directly on disk as if from an earlier session
    store = tmp_api._profile_store_path()
    os.makedirs(os.path.dirname(store), exist_ok=True)
    with open(store, 'w', encoding='utf-8') as f:
        json.dump({'us_west': str(p1)}, f)

    # Calling remember_profile BEFORE list_profiles must NOT wipe us_west!
    res = tmp_api.remember_profile(str(p2))
    assert res['profile'] == 'us_east'
    with open(store, 'r', encoding='utf-8') as f:
        stored = json.load(f)
    assert 'us_west' in stored
    assert 'us_east' in stored


def test_remember_profile_disambiguates_duplicate_names(tmp_api, tmp_path):
    d1 = tmp_path / 'office'
    d2 = tmp_path / 'home'
    d1.mkdir()
    d2.mkdir()
    p1 = d1 / 'vpn.ovpn'
    p2 = d2 / 'vpn.ovpn'
    p1.write_text('client\n', encoding='utf-8')
    p2.write_text('client\n', encoding='utf-8')

    r1 = tmp_api.remember_profile(str(p1))
    r2 = tmp_api.remember_profile(str(p2))
    assert r1['profile'] == 'vpn'
    assert r2['profile'] == 'vpn (home)'


def test_inspect_profile_auth_ignores_comments(tmp_api, tmp_path):
    cert_only = tmp_path / 'cert.ovpn'
    cert_only.write_text(
        '# auth-user-pass\n; auth-user-pass\nclient\nremote 1.2.3.4 1194\n',
        encoding='utf-8',
    )
    user_pass = tmp_path / 'creds.ovpn'
    user_pass.write_text(
        'client\nauth-user-pass # inline comment\n',
        encoding='utf-8',
    )
    tmp_api.remember_profile(str(cert_only))
    tmp_api.remember_profile(str(user_pass))

    assert tmp_api.inspect_profile('cert')['needs_auth'] is False
    assert tmp_api.inspect_profile('creds')['needs_auth'] is True


def test_connect_and_actions_handle_exit_codes_and_backup(tmp_api):
    fake_resp = mock.MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = b'FAKE_KEY'

    fake_sftp = mock.MagicMock()
    fake_client = mock.MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    fake_transport = mock.MagicMock()
    fake_transport.is_active.return_value = True
    fake_client.get_transport.return_value = fake_transport

    def make_exec(stdout_text, stderr_text='', exit_code=0):
        out = mock.MagicMock()
        err = mock.MagicMock()
        out.read.return_value = stdout_text.encode('utf-8')
        err.read.return_value = stderr_text.encode('utf-8')
        out.channel.recv_exit_status.return_value = exit_code
        return mock.MagicMock(), out, err

    fake_client.exec_command.side_effect = [
        make_exec('', '', 0),  # chmod 755
        make_exec('Current area option: 19461\n  continentIdx: 5', '', 0),  # read
        make_exec('[-] Failed', '', 1),  # failed write
        make_exec('Rebooting TV...', '', 0),  # reboot
    ]

    with mock.patch('urllib.request.urlopen', return_value=fake_resp), \
         mock.patch('paramiko.RSAKey.from_private_key', return_value='PKEY'), \
         mock.patch('paramiko.SSHClient', return_value=fake_client):
        res = tmp_api.connect('http://192.168.1.44:9991', 'pass123', webos26=True)

    assert res['ok'] is True
    assert res['remote_script'] == '/media/developer/temp/change_region.sh'
    fake_sftp.put.assert_called_once()

    read_res = tmp_api.action('read')
    assert read_res['ok'] is True
    assert read_res['current_area'] == '19461'
    assert tmp_api.load_settings()['last_tv_area_backup'] == '19461'

    fail_res = tmp_api.action('22282')
    assert fail_res['ok'] is False
    assert 'exited with status 1' in fail_res['error']

    reb_res = tmp_api.action('reboot')
    assert reb_res['ok'] is True
    assert reb_res['disconnected'] is True
    assert tmp_api.client is None


def test_action_detects_dropped_ssh_transport(tmp_api):
    fake_client = mock.MagicMock()
    fake_transport = mock.MagicMock()
    fake_transport.is_active.return_value = False
    fake_client.get_transport.return_value = fake_transport
    tmp_api.client = fake_client

    res = tmp_api.action('read')
    assert res['ok'] is False
    assert res['disconnected'] is True
    assert tmp_api.client is None


def test_openvpn_auth_file_cleanup_and_emergency_leak_stop(tmp_api, tmp_path):
    ovpn = tmp_path / 'corp.ovpn'
    ovpn.write_text('client\nauth-user-pass\n', encoding='utf-8')
    tmp_api.remember_profile(str(ovpn))

    # Fake stdout producing Initialization Sequence Completed then unexpected close
    fake_stdout = mock.MagicMock()
    fake_stdout.readline.side_effect = [
        'Mon Nov 10 10:00:00 2026 Initialization Sequence Completed\n',
        '',
    ]
    fake_proc = mock.MagicMock()
    fake_proc.stdout = fake_stdout
    fake_proc.poll.side_effect = [None, 1, 1]

    tmp_api.hotspot_active = True
    tmp_api.app_shared_pair = ('OpenVPN Wintun', 'Local Area Connection* 12')

    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, '_openvpn_exe', return_value='C:\\OpenVPN\\bin\\openvpn.exe'), \
         mock.patch('subprocess.Popen', return_value=fake_proc), \
         mock.patch.object(tmp_api, 'stop_vpn_hotspot') as mock_stop:
        # Missing credentials should return needs_auth=True
        missing = tmp_api.connect_vpn('corp', '', '')
        assert missing['needs_auth'] is True

        res = tmp_api.connect_vpn('corp', 'alice', 's3cr3t!')
        assert res['ok'] is True

        # Wait for background _read_openvpn_log thread to finish
        deadline = time.time() + 10.0
        while time.time() < deadline and not mock_stop.called:
            time.sleep(0.05)

    assert mock_stop.called
    assert tmp_api._auth_temp_file is None
    # Verify no leftover neon_auth_*.txt temp file
    leftovers = list(tmp_path.glob('neon_auth_*.txt'))
    assert leftovers == []


def test_start_vpn_hotspot_win81_and_win10_fallback(tmp_api):
    tmp_api.vpn_state = 'CONNECTED'
    adapters = [
        {'name': 'OpenVPN TAP', 'device': 'TAP-Windows Adapter V9', 'status': 7, 'shared': False},
        {'name': 'OpenVPN Wintun', 'device': 'Wintun Userspace Tunnel', 'status': 2, 'shared': False},
        {'name': 'Local Area Connection* 11', 'device': 'Microsoft Hosted Network Virtual Adapter', 'status': 2, 'shared': False},
    ]

    # Case 1: Windows 8.1 with Hosted Network supported
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'windows_info', return_value={'is_windows': True, 'major': 6, 'minor': 3, 'build': 9600, 'label': 'Windows 8.1', 'supports_winrt_hotspot': False}), \
         mock.patch.object(tmp_api, '_driver_supports_hosted_network', return_value=True), \
         mock.patch.object(tmp_api, '_run_netsh', return_value=(0, 'The hosted network started.', '')), \
         mock.patch.object(tmp_api, 'network_connections', return_value={'adapters': adapters}), \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'ok': True, 'output': 'ok'}) as mock_ics:
        res = tmp_api.start_vpn_hotspot('NEON-TV', 'Pass12345')

    assert res['ok'] is True
    assert res['mode'] == 'hostednetwork'
    # Should prefer connected OpenVPN Wintun (status=2) over disconnected TAP (status=7)
    mock_ics.assert_called_once_with('OpenVPN Wintun', 'Local Area Connection* 11')
    assert '\\n' not in res['output']  # no literal escaped \n!

    # Case 2: Windows 10/11 where Hosted Network unsupported -> WinRT fallback
    tmp_api.hotspot_active = False
    tmp_api.hotspot_mode = None
    win10_adapters = [
        {'name': 'OpenVPN Wintun', 'device': 'Wintun Userspace Tunnel', 'status': 2, 'shared': False},
        {'name': 'Local Area Connection* 12', 'device': 'Microsoft Wi-Fi Direct Virtual Adapter', 'status': 2, 'shared': False},
    ]
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'windows_info', return_value={'is_windows': True, 'major': 10, 'minor': 0, 'build': 22621, 'label': 'Windows 11', 'supports_winrt_hotspot': True}), \
         mock.patch.object(tmp_api, '_driver_supports_hosted_network', return_value=False), \
         mock.patch.object(tmp_api, '_start_winrt_hotspot', return_value='Windows Mobile Hotspot started') as mock_winrt, \
         mock.patch.object(tmp_api, 'network_connections', return_value={'adapters': win10_adapters}), \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'ok': True, 'output': 'ok'}) as mock_ics2:
        res2 = tmp_api.start_vpn_hotspot('NEON-WIN11', 'Pass12345')

    assert res2['ok'] is True
    assert res2['mode'] == 'winrt'
    mock_winrt.assert_called_once_with('NEON-WIN11', 'Pass12345')
    mock_ics2.assert_called_once_with('OpenVPN Wintun', 'Local Area Connection* 12')


def test_change_region_sh_aborts_on_write_failure(tmp_path):
    # Syntax check
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sh_path = os.path.join(repo_root, 'change_region.sh')
    chk = subprocess.run(['sh', '-n', sh_path], capture_output=True, text=True)
    assert chk.returncode == 0, chk.stderr

    # Stub node so first invocation (write_area) fails with exit 1 and records calls
    log_file = tmp_path / 'node_calls.txt'
    fake_node = tmp_path / 'node'
    fake_node.write_text(
        f'#!/bin/sh\necho "CALL" >> "{log_file}"\nexit 1\n',
        encoding='utf-8',
    )
    os.chmod(fake_node, 0o755)
    env = os.environ.copy()
    env['PATH'] = str(tmp_path) + os.pathsep + env.get('PATH', '')

    res = subprocess.run(['sh', sh_path, '22282'], env=env, capture_output=True, text=True)
    assert res.returncode == 1
    # Must stop after write_area without calling set_configd_us!
    calls = log_file.read_text(encoding='utf-8').splitlines()
    assert len(calls) == 1


def test_ui_html_contains_vpn_logs_auth_and_hotspot_controls():
    import re
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(repo_root, 'ui.html'), 'r', encoding='utf-8').read()
    assert 'id="vpnScreen"' in html
    assert 'id="vpnAuthBox"' in html
    assert 'inspect_profile' in html
    assert 'openHotspot()' in html
    assert 'id="customArea"' in html
    assert 'customAreaChanged()' in html
    assert 'installBelet()' in html
    assert 'openSysTune()' in html
    assert 'id="dnsPreset"' in html
    assert 'id="dnsPrimary"' in html
    assert 'id="dnsSecondary"' in html
    assert 'id="dnsApplyUplink"' in html
    assert 'applySharingDns()' in html
    assert 'resetSharingDns()' in html
    assert 'apply_sharing_dns' in html
    assert 'reset_sharing_dns' in html
    # Strip CSS/JS identifier tokens and verify zero visible "VPN" / "OpenVPN" labels remain
    visible = re.sub(r'<style>.*?</style>', '', html, flags=re.S)
    visible = re.sub(r'<script>.*?</script>', '', visible, flags=re.S)
    visible_text = re.sub(r'<[^>]+>', ' ', visible)
    assert not re.search(r'\bvpn\b|openvpn', visible_text, re.I), visible_text


def test_action_allows_custom_area_codes_and_rejects_invalid(tmp_api):
    fake_client = mock.MagicMock()
    fake_transport = mock.MagicMock()
    fake_transport.is_active.return_value = True
    fake_client.get_transport.return_value = fake_transport
    tmp_api.client = fake_client

    out = mock.MagicMock()
    err = mock.MagicMock()
    out.read.return_value = b'[+] NVRAM contiArea2All set to 19589\n'
    err.read.return_value = b''
    out.channel.recv_exit_status.return_value = 0
    fake_client.exec_command.return_value = (mock.MagicMock(), out, err)

    res = tmp_api.action('019589')
    assert res['ok'] is True
    executed_cmd = fake_client.exec_command.call_args[0][0]
    assert executed_cmd.endswith(' 19589')
    assert tmp_api.load_settings()['region_code'] == '19589'

    for bad in ('0', '-5', 'abc', '22282; id', '22282 && whoami'):
        bad_res = tmp_api.action(bad)
        assert bad_res['ok'] is False


def test_windows_icon_has_dib_layers_and_png_256():
    import struct

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ico_path = os.path.join(repo_root, 'assets', 'neon.ico')
    data = open(ico_path, 'rb').read()
    reserved, typ, count = struct.unpack_from('<HHH', data, 0)
    assert reserved == 0 and typ == 1 and count >= 6

    sizes = []
    for i in range(count):
        w, h, _cc, _res, _pl, _bpp, sz, off = struct.unpack_from('<BBBBHHII', data, 6 + i * 16)
        w = w or 256
        h = h or 256
        sizes.append((w, h))
        header = data[off:off + 4]
        if w < 256:
            # Windows 8.1 / Explorer / PyInstaller require BITMAPINFOHEADER (40 bytes), not PNG!
            assert struct.unpack('<I', header)[0] == 40
        else:
            assert header == b'\x89PNG'
    assert (16, 16) in sizes
    assert (32, 32) in sizes
    assert (48, 48) in sizes
    assert (256, 256) in sizes


def test_connect_tv_ssh_falls_back_to_legacy_ssh_rsa_for_lg_dropbear(tmp_api):
    import socket
    import paramiko

    host_key = paramiko.RSAKey.generate(2048)
    client_key = paramiko.RSAKey.generate(2048)

    class LegacyDropbearServer(paramiko.ServerInterface):
        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

        def get_allowed_auths(self, username):
            return 'publickey'

        def check_auth_publickey(self, username, key):
            return (
                paramiko.AUTH_SUCCESSFUL
                if username == 'prisoner' and key == client_key
                else paramiko.AUTH_FAILED
            )

    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    port = srv.getsockname()[1]
    srv.listen(6)
    stop_event = threading.Event()

    def serve():
        while not stop_event.is_set():
            try:
                srv.settimeout(0.15)
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            tr = paramiko.Transport(conn, disabled_algorithms={'pubkeys': ['rsa-sha2-512', 'rsa-sha2-256']})
            tr.add_server_key(host_key)
            tr.local_version = 'SSH-2.0-dropbear_2019.78'
            tr.server_sig_algs = False
            tr.start_server(server=LegacyDropbearServer())
            # Generous deadline: the client may retry up to three key
            # profiles (default, rsa-sha2-256, legacy ssh-rsa); on a loaded
            # CI runner the handshakes can exceed a tight timeout.
            deadline = time.time() + 10.0
            while tr.is_active() and time.time() < deadline and not stop_event.is_set():
                time.sleep(0.03)
            tr.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    orig_connect = paramiko.SSHClient.connect

    def redirect_port(self, hostname, **kw):
        assert kw.get('look_for_keys') is False
        assert kw.get('allow_agent') is False
        kw['port'] = port
        return orig_connect(self, hostname, **kw)

    try:
        out = []
        with mock.patch.object(paramiko.SSHClient, 'connect', redirect_port):
            client, remote_ver = tmp_api._connect_tv_ssh('127.0.0.1', client_key, out)
        try:
            assert remote_ver == 'SSH-2.0-dropbear_2019.78'
            assert client.get_transport().is_active() is True
            assert any('legacy ssh-rsa' in line for line in out)
        finally:
            client.close()

        # Also verify actionable error when wrong key is rejected on all attempts
        wrong_key = paramiko.RSAKey.generate(2048)
        with mock.patch.object(paramiko.SSHClient, 'connect', redirect_port):
            with pytest.raises(RuntimeError) as excinfo:
                tmp_api._connect_tv_ssh('127.0.0.1', wrong_key, [])
        assert 'SSH authentication failed' in str(excinfo.value)
        assert 'Key Server' in str(excinfo.value)
    finally:
        stop_event.set()
        srv.close()


def test_connect_strips_crlf_from_change_region_sh_before_sftp_upload(tmp_api, tmp_path):
    crlf_sh = tmp_path / 'change_region_crlf.sh'
    crlf_sh.write_bytes(b'#!/bin/sh\r\nNODE_PATH=/usr/lib\r\nexport NODE_PATH\r\n')

    uploaded_bytes = []
    fake_sftp = mock.MagicMock()

    def capture_put(local_path, remote_path):
        uploaded_bytes.append(open(local_path, 'rb').read())

    fake_sftp.put.side_effect = capture_put
    fake_client = mock.MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    out = mock.MagicMock()
    err = mock.MagicMock()
    out.read.return_value = b''
    err.read.return_value = b''
    out.channel.recv_exit_status.return_value = 0
    fake_client.exec_command.return_value = (mock.MagicMock(), out, err)

    fake_resp = mock.MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = b'FAKE_KEY'

    with mock.patch('core.resource', return_value=str(crlf_sh)), \
         mock.patch('urllib.request.urlopen', return_value=fake_resp), \
         mock.patch('paramiko.RSAKey.from_private_key', return_value='PKEY'), \
         mock.patch('paramiko.SSHClient', return_value=fake_client):
        res = tmp_api.connect('192.168.1.50', 'pass123')

    assert res['ok'] is True
    assert len(uploaded_bytes) == 1
    assert b'\r' not in uploaded_bytes[0]
    assert uploaded_bytes[0] == b'#!/bin/sh\nNODE_PATH=/usr/lib\nexport NODE_PATH\n'


def test_build_and_install_belet_ipk_structure_and_luna(tmp_api, tmp_path):
    import io
    import shutil
    import tarfile

    ipk_bytes = tmp_api.build_belet_ipk()
    assert ipk_bytes.startswith(b'!<arch>\n')

    # Parse ar members strictly (plain 16-char names, even-byte padding)
    pos = len(b'!<arch>\n')
    members = {}
    while pos < len(ipk_bytes):
        hdr = ipk_bytes[pos:pos + 60]
        assert len(hdr) == 60
        assert hdr[58:60] == b'`\n'
        name = hdr[:16].decode('ascii').strip()
        assert not name.endswith('/')  # LG appInstallService rejects GNU '/' suffix
        size = int(hdr[48:58].decode('ascii').strip())
        pos += 60
        members[name] = ipk_bytes[pos:pos + size]
        pos += size + (size % 2)

    assert list(members.keys()) == ['debian-binary', 'control.tar.gz', 'data.tar.gz']
    assert members['debian-binary'] == b'2.0\n'

    # If binutils `ar` is present locally, double-check real archive extraction compatibility
    if shutil.which('ar'):
        ipk_path = tmp_path / 'tm.belet.webos.ipk'
        ipk_path.write_bytes(ipk_bytes)
        ar_list = subprocess.run(['ar', 't', str(ipk_path)], capture_output=True, text=True)
        assert ar_list.returncode == 0
        assert ar_list.stdout.splitlines() == ['debian-binary', 'control.tar.gz', 'data.tar.gz']

    with tarfile.open(fileobj=io.BytesIO(members['control.tar.gz']), mode='r:gz') as tf:
        ctrl_names = tf.getnames()
        assert ctrl_names == ['control']
        ctrl_txt = tf.extractfile('control').read().decode('utf-8')
        assert 'Package: tm.belet.webos\n' in ctrl_txt
        assert 'webOS-Package-Format-Version: 2\n' in ctrl_txt
        assert 'Installed-Size:' in ctrl_txt

    with tarfile.open(fileobj=io.BytesIO(members['data.tar.gz']), mode='r:gz') as tf:
        names = tf.getnames()
        assert not any(n.startswith('./') for n in names)
        base = 'usr/palm/applications/tm.belet.webos'
        for req in (f'{base}/appinfo.json', f'{base}/index.html', f'{base}/icon.png', f'{base}/largeIcon.png', f'{base}/splash.png'):
            assert req in names
        appinfo = json.loads(tf.extractfile(f'{base}/appinfo.json').read().decode('utf-8'))
        assert appinfo['id'] == 'tm.belet.webos'
        assert appinfo['version'] == '1.0.2'
        assert f"Version: {appinfo['version']}\n" in ctrl_txt
        assert appinfo['type'] == 'web'
        assert appinfo['main'] == 'index.html'
        assert appinfo['icon'] == 'icon.png'
        assert appinfo['largeIcon'] == 'largeIcon.png'
        assert appinfo['splashBackground'] == 'splash.png'
        assert appinfo['handlesRelaunch'] is False
        assert appinfo['disallowScrollingInMainFrame'] is False
        assert appinfo['useNativeScroll'] is True
        html = tf.extractfile(f'{base}/index.html').read().decode('utf-8')
        assert 'https://belet.tm/' in html
        assert 'window.location.replace' in html
        assert '<iframe' not in html.lower()

    # Test install, launch, and remove over mocked SFTP + SSH
    fake_sftp = mock.MagicMock()
    fake_client = mock.MagicMock()
    fake_client.open_sftp.return_value = fake_sftp
    fake_transport = mock.MagicMock()
    fake_transport.is_active.return_value = True
    fake_client.get_transport.return_value = fake_transport

    def make_exec(stdout_text, exit_code=0):
        out = mock.MagicMock()
        err = mock.MagicMock()
        out.read.return_value = stdout_text.encode('utf-8')
        err.read.return_value = b''
        out.channel.recv_exit_status.return_value = exit_code
        return mock.MagicMock(), out, err

    fake_client.exec_command.side_effect = [
        make_exec('{"details":{"state":"installed"}}', 0),
        make_exec('{"returnValue":true}', 0),
        make_exec('{"details":{"state":"removed"}}', 0),
    ]
    tmp_api.client = fake_client
    tmp_api.remote_script = '/media/developer/temp/change_region.sh'

    res_inst = tmp_api.install_belet_app()
    assert res_inst['ok'] is True
    assert fake_sftp.put.call_args[0][1] == '/media/developer/temp/tm.belet.webos.ipk'
    install_sh = fake_client.exec_command.call_args_list[0][0][0]
    assert 'luna://com.webos.appInstallService/dev/install' in install_sh
    if shutil.which('sh'):
        assert subprocess.run(['sh', '-n', '-c', install_sh]).returncode == 0

    res_launch = tmp_api.launch_belet_app()
    assert res_launch['ok'] is True
    assert 'luna://com.webos.applicationManager/launch' in fake_client.exec_command.call_args_list[1][0][0]

    res_rm = tmp_api.remove_belet_app()
    assert res_rm['ok'] is True
    assert 'luna://com.webos.appInstallService/dev/remove' in fake_client.exec_command.call_args_list[2][0][0]


def test_samsung_ws_protocol_token_pairing_stale_recovery_and_actions(tmp_api, monkeypatch):
    import base64
    import hashlib
    import socket
    import struct
    from samsung_tv import _WS_GUID

    received_keys = []
    tokens_issued = ['TOK_FIRST_99', 'TOK_SECOND_77']
    reject_token_once = ['STALE_OLD_TOK']

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    stop_flag = threading.Event()

    def send_ws_frame(conn, opcode, data):
        hdr = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            hdr.append(n)
        elif n <= 0xFFFF:
            hdr.append(126)
            hdr.extend(struct.pack('!H', n))
        else:
            hdr.append(127)
            hdr.extend(struct.pack('!Q', n))
        conn.sendall(bytes(hdr) + data)

    def recv_exact(conn, buf, n):
        while len(buf) < n:
            c = conn.recv(4096)
            if not c:
                raise EOFError()
            buf.extend(c)
        out = bytes(buf[:n])
        del buf[:n]
        return out

    def recv_ws_frame(conn, buf):
        b1, b2 = recv_exact(conn, buf, 2)
        op = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack('!H', recv_exact(conn, buf, 2))[0]
        elif ln == 127:
            ln = struct.unpack('!Q', recv_exact(conn, buf, 8))[0]
        mask = recv_exact(conn, buf, 4) if masked else None
        payload = recv_exact(conn, buf, ln) if ln else b''
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return op, payload

    def handle_conn(conn):
        buf = bytearray()
        try:
            while b'\r\n\r\n' not in buf:
                c = conn.recv(4096)
                if not c:
                    return
                buf.extend(c)
            end = buf.find(b'\r\n\r\n') + 4
            hdr_txt = bytes(buf[:end]).decode('latin-1')
            del buf[:end]

            req_line = hdr_txt.split('\r\n', 1)[0]
            ws_key = ''
            for ln in hdr_txt.split('\r\n'):
                if ln.lower().startswith('sec-websocket-key:'):
                    ws_key = ln.split(':', 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1((ws_key + _WS_GUID).encode('ascii')).digest()).decode('ascii')
            conn.sendall(
                (
                    'HTTP/1.1 101 Switching Protocols\r\n'
                    'Upgrade: websocket\r\n'
                    'Connection: Upgrade\r\n'
                    f'Sec-WebSocket-Accept: {accept}\r\n\r\n'
                ).encode('ascii')
            )

            # Check query token
            tok_param = None
            if 'token=' in req_line:
                tok_param = req_line.split('token=', 1)[1].split(' ', 1)[0]

            if tok_param and tok_param in reject_token_once:
                reject_token_once.remove(tok_param)
                send_ws_frame(conn, 0x1, json.dumps({'event': 'ms.channel.unauthorized'}).encode('utf-8'))
                return

            if not tok_param:
                issued = tokens_issued.pop(0) if tokens_issued else 'TOK_FALLBACK'
                send_ws_frame(conn, 0x1, json.dumps({'event': 'ms.channel.connect', 'data': {'token': issued}}).encode('utf-8'))
            else:
                # Test PING -> PONG handling before ms.channel.connect
                send_ws_frame(conn, 0x9, b'hi')
                op, pong = recv_ws_frame(conn, buf)
                assert op == 0xA and pong == b'hi'
                send_ws_frame(conn, 0x1, json.dumps({'event': 'ms.channel.connect', 'data': {'clients': []}}).encode('utf-8'))

            while not stop_flag.is_set():
                try:
                    op, payload = recv_ws_frame(conn, buf)
                except EOFError:
                    break
                if op == 0x8:
                    break
                if op == 0x1:
                    msg = json.loads(payload.decode('utf-8'))
                    p = msg['params']
                    received_keys.append((p['DataOfCmd'], p['Cmd']))
        except Exception:
            pass
        finally:
            conn.close()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop_flag.is_set():
            try:
                c, _ = srv.accept()
                threading.Thread(target=handle_conn, args=(c,), daemon=True).start()
            except socket.timeout:
                continue

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()

    try:
        fake_info = {
            'ok': True,
            'scheme': 'http',
            'port': port,
            'name': 'Living Room QLED',
            'model': 'QE55S95BAU',
            'os': 'Tizen',
            'power_state': 'on',
            'token_auth': False,
            'mac': 'AA:BB:CC:DD:EE:FF',
            'country': 'DE',
            'language': 'de_DE',
        }
        # Connect directly via mocked 8001 port pointing to local test WS server
        monkeypatch.setattr('core.fetch_samsung_device_info', lambda ip, timeout=2.5: fake_info)
        monkeypatch.setattr('time.sleep', lambda _s: None)

        orig_ws_cls = __import__('core').SamsungWsClient

        def pat_ws(host, port=8002, **kw):
            kw['use_ssl'] = False
            return orig_ws_cls(host, port=srv.getsockname()[1], **kw)

        monkeypatch.setattr('core.SamsungWsClient', pat_ws)

        res = tmp_api.connect_samsung('127.0.0.1', port='8001')
        assert res['ok'] is True
        assert res['token_saved'] is True
        assert tmp_api.load_settings()['samsung_tokens']['127.0.0.1'] == 'TOK_FIRST_99'

        # Verify Service Menu, 3Speed, Advanced 0098, Hospitality Press/Release, Smart Hub Unlock, and Factory Reset
        assert tmp_api.samsung_action('service_menu_factory')['ok'] is True
        assert tmp_api.samsung_action('service_menu_3speed')['ok'] is True
        assert tmp_api.samsung_action('unlock_advanced_tv')['ok'] is True
        assert tmp_api.samsung_action('hospitality_step3')['ok'] is True
        assert tmp_api.samsung_action('smart_hub_region_unlock')['ok'] is True
        assert tmp_api.SAMSUNG_SEQUENCES['exit_service_menu'][0][1] >= 2.5
        assert tmp_api.samsung_action('factory_reset_from_option')['ok'] is True
        assert tmp_api.samsung_action('exit_service_menu')['ok'] is True
        assert tmp_api.samsung_action('exit_factory_loop')['ok'] is True

        wait_until = time.time() + 5.0
        while time.time() < wait_until and len(received_keys) < 19:
            threading.Event().wait(0.01)

        assert ('KEY_INFO', 'Click') in received_keys
        assert ('KEY_FACTORY', 'Click') in received_keys
        assert ('KEY_3SPEED', 'Click') in received_keys
        assert ('KEY_9', 'Click') in received_keys
        assert ('KEY_8', 'Click') in received_keys
        assert ('KEY_VOLUP', 'Press') in received_keys
        assert ('KEY_VOLUP', 'Release') in received_keys
        assert ('KEY_CHUP', 'Click') in received_keys
        assert ('KEY_ENTER', 'Click') in received_keys

        # Stale token recovery (simulates Factory Reset invalidating saved TV token)
        tmp_api.save_settings({'samsung_tokens': {'127.0.0.1': 'STALE_OLD_TOK'}})
        res2 = tmp_api.connect_samsung('127.0.0.1', port='8001')
        assert res2['ok'] is True
        assert tmp_api.load_settings()['samsung_tokens']['127.0.0.1'] == 'TOK_SECOND_77'

        # Key validation
        bad = tmp_api.samsung_send_key('BAD KEY; rm -rf')
        assert bad['ok'] is False
    finally:
        stop_flag.set()
        t.join(timeout=1.0)
        srv.close()


def test_samsung_legacy_port_55000_protocol(tmp_api, monkeypatch):
    import base64
    import socket
    import struct

    captured = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(2)
    port = srv.getsockname()[1]

    def read_str(conn):
        ln = struct.unpack('<H', conn.recv(2))[0]
        return conn.recv(ln)

    def serve():
        conn, _ = srv.accept()
        try:
            _hdr = conn.recv(3)
            _ = read_str(conn)  # handshake payload
            # Send access granted (\x64\x00\x01\x00)
            tv_name = b'LegacyTV'
            conn.sendall(b'\x00' + struct.pack('<H', len(tv_name)) + tv_name + struct.pack('<H', 4) + b'\x64\x00\x01\x00')
            while True:
                h = conn.recv(3)
                if not h:
                    break
                pl = read_str(conn)
                # payload is \x00\x00\x00 + len(2B) + base64(KEY)
                klen = struct.unpack('<H', pl[3:5])[0]
                captured.append(base64.b64decode(pl[5:5 + klen]).decode('ascii'))
                conn.sendall(b'\x00' + struct.pack('<H', len(tv_name)) + tv_name + struct.pack('<H', 4) + b'\x00\x00\x00\x00')
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        monkeypatch.setattr('core.fetch_samsung_device_info', lambda ip, timeout=2.5: None)
        orig_legacy = __import__('core').SamsungLegacyClient
        monkeypatch.setattr('core.SamsungLegacyClient', lambda host, port=55000, **kw: orig_legacy(host, port=srv.getsockname()[1], **kw))
        res = tmp_api.connect_samsung('127.0.0.1', port='55000')
        assert res['ok'] is True
        assert res['protocol'] == 'legacy-55000'
        assert tmp_api.samsung_send_key('KEY_FACTORY')['ok'] is True
        assert captured == ['KEY_FACTORY']
    finally:
        srv.close()


def test_belet_wgt_build_sign_and_verify(tmp_path):
    import datetime
    import io
    import zipfile
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from samsung_tizen import (
        AUTHOR_PROP_DIGEST,
        DISTRIBUTOR_PROP_DIGEST,
        build_belet_wgt,
        sign_wgt_bytes,
        verify_wgt_signatures,
    )

    unsigned = build_belet_wgt(target_url='https://belet.tm/', pkg_id='BeletTV001', app_name='Belet')
    with zipfile.ZipFile(io.BytesIO(unsigned), 'r') as zf:
        names = set(zf.namelist())
        assert {'config.xml', 'index.html', 'icon.png'}.issubset(names)
        cfg = zf.read('config.xml').decode('utf-8')
        assert 'package="BeletTV001"' in cfg
        assert 'id="BeletTV001.Belet"' in cfg
        html = zf.read('index.html').decode('utf-8')
        assert 'https://belet.tm/' in html

    now = datetime.datetime.now(datetime.timezone.utc)
    a_key = rsa.generate_private_key(65537, 2048)
    d_key = rsa.generate_private_key(65537, 2048)
    a_ca_key = rsa.generate_private_key(65537, 2048)
    d_ca_key = rsa.generate_private_key(65537, 2048)

    a_ca = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Author CA')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Author CA')]))
        .public_key(a_ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(a_ca_key, hashes.SHA256())
    )
    a_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'NEON')]))
        .issuer_name(a_ca.subject)
        .public_key(a_key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(a_ca_key, hashes.SHA256())
    )
    d_ca = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Distributor CA')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Distributor CA')]))
        .public_key(d_ca_key.public_key())
        .serial_number(3)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(d_ca_key, hashes.SHA256())
    )
    d_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'TizenSDK')]))
        .issuer_name(d_ca.subject)
        .public_key(d_key.public_key())
        .serial_number(4)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.UniformResourceIdentifier('URN:tizen:packageid='),
                x509.UniformResourceIdentifier('URN:tizen:deviceid=TESTDUID12345'),
            ]),
            critical=False,
        )
        .sign(d_ca_key, hashes.SHA256())
    )

    signed = sign_wgt_bytes(unsigned, a_key, [a_leaf, a_ca], d_key, [d_leaf, d_ca])
    assert verify_wgt_signatures(signed) is True

    with zipfile.ZipFile(io.BytesIO(signed), 'r') as zf:
        auth_xml = zf.read('author-signature.xml').decode('utf-8')
        dist_xml = zf.read('signature1.xml').decode('utf-8')
        assert AUTHOR_PROP_DIGEST in auth_xml
        assert DISTRIBUTOR_PROP_DIGEST in dist_xml
        assert 'URI="author-signature.xml"' in dist_xml

    # Tampering index.html inside signed .wgt must invalidate digest verification
    tampered_buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(signed), 'r') as zin, zipfile.ZipFile(tampered_buf, 'w') as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == 'index.html':
                data += b'<!-- tampered -->'
            zout.writestr(item, data)
    with pytest.raises(ValueError, match='digest mismatch'):
        verify_wgt_signatures(tampered_buf.getvalue())


def test_samsung_cert_manager_oauth_and_provisioning(tmp_path, monkeypatch):
    import datetime
    import urllib.parse
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtensionOID, NameOID
    from samsung_tizen import (
        SAMSUNG_STATE,
        SamsungCertManager,
        parse_samsung_oauth_callback,
    )

    body = urllib.parse.urlencode({
        'state': SAMSUNG_STATE,
        'code': json.dumps({
            'access_token': 'AT_12345',
            'userId': 'UID_999',
            'inputEmailID': 'dev@example.com',
        }),
    })
    parsed = parse_samsung_oauth_callback('POST', '/signin/callback', body)
    assert parsed['access_token'] == 'AT_12345'
    assert parsed['userId'] == 'UID_999'
    assert parse_samsung_oauth_callback('POST', '/signin/callback', 'state=wrong&code={}') is None

    mgr = SamsungCertManager(str(tmp_path))
    os.makedirs(mgr.ca_dir, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_a_key = rsa.generate_private_key(65537, 2048)
    ca_d_key = rsa.generate_private_key(65537, 2048)
    ca_a = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Samsung VD Author CA')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Samsung VD Root')]))
        .public_key(ca_a_key.public_key())
        .serial_number(10)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(ca_a_key, hashes.SHA256())
    )
    ca_d = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Samsung VD Public CA')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Samsung VD Root')]))
        .public_key(ca_d_key.public_key())
        .serial_number(11)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(ca_d_key, hashes.SHA256())
    )
    with open(os.path.join(mgr.ca_dir, 'vd_tizen_dev_author_ca.cer'), 'wb') as f:
        f.write(ca_a.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(mgr.ca_dir, 'vd_tizen_dev_public2.crt'), 'wb') as f:
        f.write(ca_d.public_bytes(serialization.Encoding.PEM))

    def fake_urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        raw = req.data.decode('latin-1')
        m_csr = re.search(r'-----BEGIN CERTIFICATE REQUEST-----.*?-----END CERTIFICATE REQUEST-----', raw, re.DOTALL)
        csr = x509.load_pem_x509_csr(m_csr.group(0).encode('ascii'))
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        if 'authors' in url:
            cert = (
                x509.CertificateBuilder()
                .subject_name(csr.subject)
                .issuer_name(ca_a.subject)
                .public_key(csr.public_key())
                .serial_number(20)
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=365))
                .sign(ca_a_key, hashes.SHA256())
            )
            resp.read.return_value = cert.public_bytes(serialization.Encoding.PEM)
        elif 'v1/distributors' in url:
            resp.read.return_value = b'<profile version="1"/>'
        else:
            assert b'name="privilege_level"\r\n\r\npublic\r\n' in req.data
            san = csr.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
            cert = (
                x509.CertificateBuilder()
                .subject_name(csr.subject)
                .issuer_name(ca_d.subject)
                .public_key(csr.public_key())
                .serial_number(21)
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=365))
                .add_extension(san, critical=False)
                .sign(ca_d_key, hashes.SHA256())
            )
            resp.read.return_value = cert.public_bytes(serialization.Encoding.PEM)
        return resp

    import re
    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    bundle = mgr.provision_with_oauth_token('TVDUID987654321', parsed)
    assert len(bundle['author_certs']) == 2
    assert len(bundle['dist_certs']) == 2

    reused = mgr.find_saved_samsung_certs('TVDUID987654321')
    assert reused is not None
    assert mgr.find_saved_samsung_certs('OTHERDUID000000') is None


def test_https_ca_download_handles_ssl_local_issuer_via_schannel_and_mirror(tmp_path, monkeypatch):
    import io
    import ssl
    import urllib.error
    import zipfile
    import samsung_tizen
    from samsung_tizen import SamsungCertManager, _build_ssl_context, _http_read

    ctx = _build_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)

    # Build a minimal nested Tizen Samsung Certificate Extension archive
    jar_buf = io.BytesIO()
    with zipfile.ZipFile(jar_buf, 'w') as jf:
        jf.writestr('ca/vd_tizen_dev_author_ca.cer', b'CERT_A')
        jf.writestr('ca/vd_tizen_dev_public2.crt', b'CERT_D')
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, 'w') as iz:
        iz.writestr('plugins/cert.jar', jar_buf.getvalue())
    outer_buf = io.BytesIO()
    with zipfile.ZipFile(outer_buf, 'w') as oz:
        oz.writestr('binary/ext.zip', inner_buf.getvalue())
    ext_bytes = outer_buf.getvalue()

    seen_urls = []

    def fail_ssl_then_http(target, timeout=30, context=None):
        u = target.full_url if hasattr(target, 'full_url') else str(target)
        seen_urls.append(u)
        if u.startswith('https://'):
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate'))
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = ext_bytes
        return resp

    # 1. On Windows, SSL local issuer error triggers native Windows Schannel helper first
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch('urllib.request.urlopen', side_effect=fail_ssl_then_http), \
         mock.patch.object(samsung_tizen, '_windows_schannel_fetch', return_value=b'{"ok":true}') as m_sch:
        out = _http_read('https://svdca.samsungqbe.com/apis/v3/authors', timeout=10)
    assert out == b'{"ok":true}'
    m_sch.assert_called_once()

    # 2. For download.tizen.org extension downloads, falls back cleanly to official HTTP mirror if TLS fails
    seen_urls.clear()
    mgr = SamsungCertManager(str(tmp_path / 'ssl_mgr'))
    with mock.patch('platform.system', return_value='Linux'), \
         mock.patch('urllib.request.urlopen', side_effect=fail_ssl_then_http):
        ca_dir = mgr.ensure_samsung_ca_certs()
    assert os.path.isfile(os.path.join(ca_dir, 'vd_tizen_dev_author_ca.cer'))
    assert os.path.isfile(os.path.join(ca_dir, 'vd_tizen_dev_public2.crt'))
    assert any(u.startswith('http://download.tizen.org/') for u in seen_urls)


def test_bundled_samsung_ca_certs_are_valid_non_root_2025_intermediates():
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from samsung_tizen import SamsungCertManager

    ca_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'samsung_ca')
    for req in ('vd_tizen_dev_author_ca.cer', 'vd_tizen_dev_public2.crt', 'vd_tizen_dev_partner2.crt', 'author_ca.cer', 'public2.crt'):
        p = os.path.join(ca_dir, req)
        assert os.path.isfile(p), f'Missing bundled CA {req}'
        with open(p, 'rb') as f:
            cert = x509.load_pem_x509_certificate(f.read())
        assert cert.subject != cert.issuer, f'{req} must be an intermediate CA, not a self-signed root'

    with open(os.path.join(ca_dir, 'vd_tizen_dev_author_ca.cer'), 'rb') as f:
        vd_auth = x509.load_pem_x509_certificate(f.read())
    assert vd_auth.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == 'Samsung VD Author CA'
    assert vd_auth.public_key().key_size >= 4096

    with open(os.path.join(ca_dir, 'vd_tizen_dev_public2.crt'), 'rb') as f:
        vd_pub = x509.load_pem_x509_certificate(f.read())
    assert vd_pub.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == 'VD DEVELOPER Public CA Class'
    assert vd_pub.public_key().key_size >= 4096

    mgr = SamsungCertManager(os.path.join(ca_dir, '_unused_tmp'))
    assert os.path.abspath(mgr._bundled_ca_dir()) == os.path.abspath(ca_dir)


def test_wgt_xmldsig_matches_tizen_tv_format_and_strips_root():
    import datetime
    import io
    import re
    import zipfile
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from samsung_tizen import build_belet_wgt, sign_wgt_bytes, verify_wgt_signatures

    now = datetime.datetime.now(datetime.timezone.utc)

    def make_chain(root_cn, int_cn, leaf_cn, san_duid=None):
        rk = rsa.generate_private_key(65537, 2048)
        ik = rsa.generate_private_key(65537, 2048)
        lk = rsa.generate_private_key(65537, 2048)
        root = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, root_cn)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, root_cn)]))
            .public_key(rk.public_key())
            .serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(rk, hashes.SHA256())
        )
        inter = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, int_cn)]))
            .issuer_name(root.subject)
            .public_key(ik.public_key())
            .serial_number(2)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(rk, hashes.SHA256())
        )
        b = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, leaf_cn)]))
            .issuer_name(inter.subject)
            .public_key(lk.public_key())
            .serial_number(3)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
        )
        if san_duid:
            b = b.add_extension(
                x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f'URN:tizen:deviceid={san_duid}')]),
                critical=False,
            )
        leaf = b.sign(ik, hashes.SHA256())
        return lk, [leaf, inter, root]

    ak, a_chain = make_chain('Tizen Developers Root Class', 'Samsung VD Author CA', 'NEON')
    dk, d_chain = make_chain('VD DEVELOPER Public Root Class', 'VD DEVELOPER Public CA Class', 'TizenSDK', 'DUID12345')

    unsigned = build_belet_wgt()
    signed = sign_wgt_bytes(unsigned, ak, a_chain, dk, d_chain)
    assert verify_wgt_signatures(signed, require_chain=True) is True

    with zipfile.ZipFile(io.BytesIO(signed), 'r') as zf:
        names = zf.namelist()
        # Signatures must be first in archive matching TizenWgtSigner.cs & TV-Store/signer.js
        assert names[:2] == ['author-signature.xml', 'signature1.xml']
        for sig_name in ('author-signature.xml', 'signature1.xml'):
            xml = zf.read(sig_name).decode('utf-8')
            # Only [leaf, intermediate] embedded — root stripped
            x509_blocks = re.findall(r'<X509Certificate>(.*?)</X509Certificate>', xml, flags=re.DOTALL)
            assert len(x509_blocks) == 2
            assert all(blk.startswith('\n') and blk.endswith('\n') for blk in x509_blocks)
            # DigestValue & SignatureValue must be plain unwrapped base64
            for dv in re.findall(r'<DigestValue>(.*?)</DigestValue>', xml, flags=re.DOTALL):
                assert '\n' not in dv and '\r' not in dv
            for sv in re.findall(r'<SignatureValue>(.*?)</SignatureValue>', xml, flags=re.DOTALL):
                assert '\n' not in sv and '\r' not in sv


def test_samsung_cert_manager_heals_1cert_p12_refreshes_stale_ca_and_rejects_broken_chain(tmp_path, monkeypatch):
    import datetime
    import io
    import re
    import zipfile
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
    from cryptography.x509.oid import ExtensionOID, NameOID
    from samsung_tizen import SamsungCertManager, verify_wgt_signatures, build_belet_wgt, sign_wgt_bytes

    now = datetime.datetime.now(datetime.timezone.utc)
    rk = rsa.generate_private_key(65537, 2048)
    ca_a_key = rsa.generate_private_key(65537, 2048)
    ca_d_key = rsa.generate_private_key(65537, 2048)
    stale_key = rsa.generate_private_key(65537, 2048)

    root = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Tizen Developers Root Class')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Tizen Developers Root Class')]))
        .public_key(rk.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(rk, hashes.SHA256())
    )
    stale_ca = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Samsung Author CA Class')]))
        .issuer_name(root.subject)
        .public_key(stale_key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(rk, hashes.SHA256())
    )
    ca_a = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Samsung VD Author CA')]))
        .issuer_name(root.subject)
        .public_key(ca_a_key.public_key())
        .serial_number(3)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(rk, hashes.SHA256())
    )
    ca_d = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'VD DEVELOPER Public CA Class')]))
        .issuer_name(root.subject)
        .public_key(ca_d_key.public_key())
        .serial_number(4)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(rk, hashes.SHA256())
    )

    # 1. Simulate a PC left with broken 1-cert .p12s from earlier build + stale 2.0.70 CA in ca_dir,
    #    while bundled_dir holds the updated matching CAs.
    mgr = SamsungCertManager(str(tmp_path / 'heal_case'))
    os.makedirs(mgr.cert_dir, exist_ok=True)
    os.makedirs(mgr.ca_dir, exist_ok=True)
    bundled_dir = str(tmp_path / 'bundled_ca')
    os.makedirs(bundled_dir, exist_ok=True)
    with open(os.path.join(mgr.ca_dir, 'vd_tizen_dev_author_ca.cer'), 'wb') as f:
        f.write(stale_ca.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(mgr.ca_dir, 'vd_tizen_dev_public2.crt'), 'wb') as f:
        f.write(stale_ca.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(bundled_dir, 'vd_tizen_dev_author_ca.cer'), 'wb') as f:
        f.write(ca_a.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(bundled_dir, 'vd_tizen_dev_public2.crt'), 'wb') as f:
        f.write(ca_d.public_bytes(serialization.Encoding.PEM))

    key = rsa.generate_private_key(65537, 2048)
    a_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'NEON')]))
        .issuer_name(ca_a.subject)
        .public_key(key.public_key())
        .serial_number(10)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(ca_a_key, hashes.SHA256())
    )
    d_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'TizenSDK')]))
        .issuer_name(ca_d.subject)
        .public_key(key.public_key())
        .serial_number(11)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier('URN:tizen:deviceid=DUIDHEAL01')]),
            critical=False,
        )
        .sign(ca_d_key, hashes.SHA256())
    )
    pwd = 'secretpw'
    with open(os.path.join(mgr.cert_dir, 'password.txt'), 'w', encoding='utf-8') as f:
        f.write(pwd)
    with open(os.path.join(mgr.cert_dir, 'author.p12'), 'wb') as f:
        f.write(pkcs12.serialize_key_and_certificates(b'UserCertificate', key, a_leaf, None, BestAvailableEncryption(pwd.encode())))
    with open(os.path.join(mgr.cert_dir, 'distributor_DUIDHEAL01.p12'), 'wb') as f:
        f.write(pkcs12.serialize_key_and_certificates(b'UserCertificate', key, d_leaf, None, BestAvailableEncryption(pwd.encode())))

    with mock.patch.object(mgr, '_bundled_ca_dir', return_value=bundled_dir):
        healed = mgr.find_saved_samsung_certs('DUIDHEAL01')
    assert healed is not None
    assert len(healed['author_certs']) == 2
    assert len(healed['dist_certs']) == 2
    signed = sign_wgt_bytes(build_belet_wgt(), healed['author_key'], healed['author_certs'], healed['dist_key'], healed['dist_certs'])
    assert verify_wgt_signatures(signed, require_chain=True) is True

    # 2. Unrepairable 1-cert profile (unknown CA) must be rejected instead of risking TV 118/-12
    mgr_bad = SamsungCertManager(str(tmp_path / 'bad_case'))
    os.makedirs(mgr_bad.cert_dir, exist_ok=True)
    os.makedirs(mgr_bad.ca_dir, exist_ok=True)
    with open(os.path.join(mgr_bad.ca_dir, 'vd_tizen_dev_author_ca.cer'), 'wb') as f:
        f.write(stale_ca.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(mgr_bad.ca_dir, 'vd_tizen_dev_public2.crt'), 'wb') as f:
        f.write(stale_ca.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(mgr_bad.cert_dir, 'password.txt'), 'w', encoding='utf-8') as f:
        f.write(pwd)
    with open(os.path.join(mgr_bad.cert_dir, 'author.p12'), 'wb') as f:
        f.write(pkcs12.serialize_key_and_certificates(b'UserCertificate', key, a_leaf, None, BestAvailableEncryption(pwd.encode())))
    with open(os.path.join(mgr_bad.cert_dir, 'distributor_DUIDHEAL01.p12'), 'wb') as f:
        f.write(pkcs12.serialize_key_and_certificates(b'UserCertificate', key, d_leaf, None, BestAvailableEncryption(pwd.encode())))
    with mock.patch.object(mgr_bad, '_bundled_ca_dir', return_value=None), \
         mock.patch.object(mgr_bad, 'ensure_samsung_ca_certs', side_effect=RuntimeError('offline')):
        assert mgr_bad.find_saved_samsung_certs('DUIDHEAL01') is None

    # 3. provision_with_oauth_token refreshes CAs if local ca_dir was stale, and refuses to write a 1-cert .p12 if still unmatched
    jar_buf = io.BytesIO()
    with zipfile.ZipFile(jar_buf, 'w') as jf:
        jf.writestr('res/ca/vd_tizen_dev_author_ca.cer', ca_a.public_bytes(serialization.Encoding.PEM))
        jf.writestr('res/ca/vd_tizen_dev_public2.crt', ca_d.public_bytes(serialization.Encoding.PEM))
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, 'w') as iz:
        iz.writestr('plugins/org.tizen.common.cert_2.0.75.jar', jar_buf.getvalue())
    outer_buf = io.BytesIO()
    with zipfile.ZipFile(outer_buf, 'w') as oz:
        oz.writestr('binary/cert-add-on_2.0.75_windows-64.zip', inner_buf.getvalue())
    ext_bytes = outer_buf.getvalue()

    def fake_provision_urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        if 'extension_info.xml' in url:
            resp.read.return_value = b'<extensions xmlns="http://tizen.org"><extension><name>Samsung Certificate Extension</name><version>2.0.75</version><repository>https://download.tizen.org/sdk/extensions/tizen-certificate-extension_2.0.75.zip</repository></extension></extensions>'
            return resp
        if 'tizen-certificate-extension' in url:
            resp.read.return_value = ext_bytes
            return resp
        raw = req.data.decode('latin-1')
        m_csr = re.search(r'-----BEGIN CERTIFICATE REQUEST-----.*?-----END CERTIFICATE REQUEST-----', raw, re.DOTALL)
        csr = x509.load_pem_x509_csr(m_csr.group(0).encode('ascii'))
        if 'authors' in url:
            cert = (
                x509.CertificateBuilder()
                .subject_name(csr.subject)
                .issuer_name(ca_a.subject)
                .public_key(csr.public_key())
                .serial_number(20)
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=365))
                .sign(ca_a_key, hashes.SHA256())
            )
            resp.read.return_value = cert.public_bytes(serialization.Encoding.PEM)
        elif 'v1/distributors' in url:
            resp.read.return_value = b'<profile version="1"/>'
        else:
            san = csr.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
            cert = (
                x509.CertificateBuilder()
                .subject_name(csr.subject)
                .issuer_name(ca_d.subject)
                .public_key(csr.public_key())
                .serial_number(21)
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=365))
                .add_extension(san, critical=False)
                .sign(ca_d_key, hashes.SHA256())
            )
            resp.read.return_value = cert.public_bytes(serialization.Encoding.PEM)
        return resp

    mgr_fresh = SamsungCertManager(str(tmp_path / 'fresh_case'))
    os.makedirs(mgr_fresh.ca_dir, exist_ok=True)
    with open(os.path.join(mgr_fresh.ca_dir, 'vd_tizen_dev_author_ca.cer'), 'wb') as f:
        f.write(stale_ca.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(mgr_fresh.ca_dir, 'vd_tizen_dev_public2.crt'), 'wb') as f:
        f.write(stale_ca.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setattr('urllib.request.urlopen', fake_provision_urlopen)
    with mock.patch.object(mgr_fresh, '_bundled_ca_dir', return_value=None):
        fresh_bundle = mgr_fresh.provision_with_oauth_token('DUIDFRESH01', {'access_token': 'AT', 'userId': 'UID'})
    assert len(fresh_bundle['author_certs']) == 2
    assert len(fresh_bundle['dist_certs']) == 2

    # Never write a 1-cert author.p12 when remote CA refresh still cannot verify the leaf
    mgr_fail = SamsungCertManager(str(tmp_path / 'fail_case'))
    with mock.patch.object(mgr_fail, '_bundled_ca_dir', return_value=None), \
         mock.patch.object(mgr_fail, 'ensure_samsung_ca_certs', side_effect=[str(tmp_path), RuntimeError('no matching CA')]):
        with pytest.raises(RuntimeError, match='Missing verified Samsung Author intermediate CA'):
            mgr_fail.provision_with_oauth_token('DUIDFAIL01', {'access_token': 'AT', 'userId': 'UID'})
    assert not os.path.exists(os.path.join(mgr_fail.cert_dir, 'author.p12'))


def test_sdb_wire_client_and_belet_install_launch_remove(tmp_api, tmp_path, monkeypatch):
    import datetime
    import socket
    import struct
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from samsung_tizen import (
        _A_CLSE,
        _A_CNXN,
        _A_OKAY,
        _A_OPEN,
        _A_WRTE,
        diagnose_install_output,
        verify_wgt_signatures,
    )

    assert diagnose_install_output('install failed[118012]')['status'] == 'cert_mismatch'
    assert diagnose_install_output('install failed[118, -12] reason: Check certificate error : :Invalid certificate chain.:<-2>')['status'] == 'cert_invalid_chain'
    assert diagnose_install_output('download failed[116]')['status'] == 'no_space'
    assert diagnose_install_output('install failed[118, -22]')['status'] == 'pkg_blocked'
    assert diagnose_install_output('installing[100]\ninstall completed')['status'] == 'ok'

    # Prepare cached cert bundle for TVDUID111222333
    now = datetime.datetime.now(datetime.timezone.utc)
    a_key = rsa.generate_private_key(65537, 2048)
    d_key = rsa.generate_private_key(65537, 2048)
    a_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'NEON')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'NEON')]))
        .public_key(a_key.public_key())
        .serial_number(100)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(a_key, hashes.SHA256())
    )
    d_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'TizenSDK')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'TizenSDK')]))
        .public_key(d_key.public_key())
        .serial_number(101)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier('URN:tizen:deviceid=TVDUID111222333')]),
            critical=False,
        )
        .sign(d_key, hashes.SHA256())
    )

    state = {'uploaded': bytearray(), 'installed': False, 'install_calls': 0, 'commands': [], 'launched': False}
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def recv_exact(conn, n):
        buf = bytearray()
        while len(buf) < n:
            part = conn.recv(n - len(buf))
            if not part:
                raise EOFError()
            buf.extend(part)
        return bytes(buf)

    def send_pkt(conn, cmd, arg0, arg1, data=b''):
        hdr = struct.pack('<IIIIII', cmd, arg0, arg1, len(data), sum(data) & 0xFFFFFFFF, cmd ^ 0xFFFFFFFF)
        conn.sendall(hdr + data)

    def handle(conn):
        sync_streams = {}
        next_rid = 50
        try:
            while True:
                hdr = recv_exact(conn, 24)
                cmd, arg0, arg1, dlen, _chk, _mag = struct.unpack('<IIIIII', hdr)
                data = recv_exact(conn, dlen) if dlen else b''
                if cmd == _A_CNXN:
                    send_pkt(conn, _A_CNXN, 0x01000000, 65536, b'device::ro.product.model=QN90C\x00')
                elif cmd == _A_OPEN:
                    svc = data.rstrip(b'\x00').decode('utf-8')
                    state['commands'].append(svc)
                    rid = next_rid
                    next_rid += 1
                    if svc == 'sync:':
                        send_pkt(conn, _A_OKAY, rid, arg0)
                        sync_streams[arg0] = bytearray()
                    elif svc == 'capability:':
                        send_pkt(conn, _A_OKAY, rid, arg0)
                        send_pkt(conn, _A_WRTE, rid, arg0, b'platform_version:8.0\nsdk_toolpath:/home/owner/share/tmp/sdk_tools\n')
                        send_pkt(conn, _A_CLSE, rid, arg0)
                    elif svc == 'shell:0 getduid':
                        send_pkt(conn, _A_OKAY, rid, arg0)
                        send_pkt(conn, _A_WRTE, rid, arg0, b'TVDUID111222333\n')
                        send_pkt(conn, _A_CLSE, rid, arg0)
                    elif svc.startswith('shell:0 vd_appinstall '):
                        send_pkt(conn, _A_OKAY, rid, arg0)
                        state['install_calls'] += 1
                        if state['install_calls'] == 1:
                            send_pkt(conn, _A_WRTE, rid, arg0, b'install failed[118012]\n')
                        else:
                            state['installed'] = True
                            send_pkt(conn, _A_WRTE, rid, arg0, b'installing[100]\ninstall completed\nspend time(250ms)\n')
                        send_pkt(conn, _A_CLSE, rid, arg0)
                    elif svc == 'shell:0 vd_appuninstall BeletTV001':
                        send_pkt(conn, _A_OKAY, rid, arg0)
                        state['installed'] = False
                        send_pkt(conn, _A_WRTE, rid, arg0, b'uninstall completed\n')
                        send_pkt(conn, _A_CLSE, rid, arg0)
                    elif svc == 'shell:0 vd_applist':
                        send_pkt(conn, _A_OKAY, rid, arg0)
                        out = b'BeletTV001.Belet\tBelet\n' if state['installed'] else b''
                        if out:
                            send_pkt(conn, _A_WRTE, rid, arg0, out)
                        send_pkt(conn, _A_CLSE, rid, arg0)
                    elif svc == 'shell:0 was_execute BeletTV001.Belet':
                        send_pkt(conn, _A_OKAY, rid, arg0)
                        state['launched'] = True
                        send_pkt(conn, _A_WRTE, rid, arg0, b'Application launched\n')
                        send_pkt(conn, _A_CLSE, rid, arg0)
                    else:
                        # Simulate real Samsung TV sdbd rejecting non-prefixed verbs with CLSE before OKAY
                        send_pkt(conn, _A_CLSE, rid, arg0)
                elif cmd == _A_WRTE:
                    send_pkt(conn, _A_OKAY, arg1, arg0)
                    if arg0 in sync_streams:
                        buf = sync_streams[arg0]
                        buf.extend(data)
                        while len(buf) >= 8:
                            tag = bytes(buf[:4])
                            val = struct.unpack('<I', buf[4:8])[0]
                            if tag in (b'SEND', b'DATA'):
                                if len(buf) < 8 + val:
                                    break
                                if tag == b'DATA':
                                    state['uploaded'].extend(buf[8:8 + val])
                                del buf[:8 + val]
                            elif tag == b'DONE':
                                del buf[:8]
                                send_pkt(conn, _A_WRTE, 99, arg0, b'OKAY\x00\x00\x00\x00')
                            elif tag == b'STAT':
                                if len(buf) < 8 + val:
                                    break
                                del buf[:8 + val]
                                send_pkt(conn, _A_WRTE, 99, arg0, b'STAT' + struct.pack('<III', 0o100644, len(state['uploaded']), 0))
                            elif tag == b'QUIT':
                                del buf[:8]
                                send_pkt(conn, _A_CLSE, 99, arg0)
                                break
                            else:
                                break
        except Exception:
            pass
        finally:
            conn.close()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                threading.Thread(target=handle, args=(c,), daemon=True).start()
            except socket.timeout:
                continue

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        import samsung_tizen
        monkeypatch.setattr('core.fetch_samsung_device_info', lambda ip, timeout=2.2: {'model': 'QE55QN90C', 'developer_mode': '1', 'developer_ip': '127.0.0.1'})
        orig_sdb = samsung_tizen.SdbClient
        monkeypatch.setattr('core.SdbClient', lambda host, port=26101, timeout=8.0: orig_sdb(host, port=srv.getsockname()[1], timeout=timeout))
        monkeypatch.setattr(
            tmp_api._samsung_cert_manager().__class__,
            'find_saved_samsung_certs',
            lambda _self, duid: {
                'source': str(tmp_path),
                'author_key': a_key,
                'author_certs': [a_leaf],
                'dist_key': d_key,
                'dist_certs': [d_leaf],
            } if duid == 'TVDUID111222333' else None,
        )

        chk = tmp_api.check_samsung_dev_mode('127.0.0.1')
        assert chk['ok'] is True
        assert chk['duid'] == 'TVDUID111222333'
        assert chk['has_cert'] is True

        res = tmp_api.install_samsung_belet('127.0.0.1')
        assert res['ok'] is True
        assert res['app_id'] == 'BeletTV001.Belet'
        assert state['install_calls'] == 2
        assert state['installed'] is True
        assert state['launched'] is True
        assert verify_wgt_signatures(bytes(state['uploaded'])) is True

        lres = tmp_api.launch_samsung_belet('127.0.0.1')
        assert lres['ok'] is True

        rres = tmp_api.remove_samsung_belet('127.0.0.1')
        assert rres['ok'] is True
        assert state['installed'] is False

        # Every SDB shell command must carry the `shell:` prefix (never bare `0 getduid`)
        assert 'shell:0 getduid' in state['commands']
        assert '0 getduid' not in state['commands']
        assert any(c.startswith('shell:0 vd_appinstall ') for c in state['commands'])
        assert 'shell:0 vd_applist' in state['commands']
        assert 'shell:0 was_execute BeletTV001.Belet' in state['commands']
        assert 'shell:0 vd_appuninstall BeletTV001' in state['commands']
    finally:
        stop.set()
        t.join(timeout=1.0)
        srv.close()


def test_classify_adapter_distinguishes_ethernet_wifi_vpn_virtual():
    assert Api._classify_adapter({
        'name': 'Беспроводная сеть',
        'device': 'Intel(R) Wi-Fi 6 AX201 160MHz',
        'phys_media': 'Native 802.11',
        'hardware': True,
        'virtual': False,
        'netcon_media': 3,
    }) == 'wifi'
    assert Api._classify_adapter({
        'name': 'Ethernet',
        'device': 'Realtek PCIe GbE Family Controller',
        'phys_media': '802.3',
        'hardware': True,
        'virtual': False,
        'netcon_media': 3,
    }) == 'ethernet'
    assert Api._classify_adapter({
        'name': 'Подключение по локальной сети* 12',
        'device': 'Microsoft Wi-Fi Direct Virtual Adapter',
        'phys_media': 'Native 802.11',
        'hardware': False,
        'virtual': True,
    }) == 'hotspot'
    assert Api._classify_adapter({
        'name': 'OpenVPN Data Channel Offload',
        'device': 'OpenVPN Data Channel Offload',
        'phys_media': 'Unspecified',
        'virtual': True,
    }) == 'vpn'
    assert Api._classify_adapter({
        'name': 'vEthernet (Default Switch)',
        'device': 'Hyper-V Virtual Ethernet Adapter',
        'phys_media': '802.3',
        'virtual': True,
    }) == 'virtual'


def test_start_vpn_ethernet_auto_selects_and_tears_down_safely(tmp_api):
    # Fails closed when Conf is not connected
    tmp_api.vpn_state = 'DISCONNECTED'
    with mock.patch('platform.system', return_value='Windows'):
        blocked = tmp_api.start_vpn_ethernet()
    assert 'error' in blocked

    tmp_api.vpn_state = 'CONNECTED'
    adapters = [
        {'name': 'Беспроводная сеть', 'device': 'Intel(R) Wi-Fi 6 AX201', 'phys_media': 'Native 802.11', 'hardware': True, 'virtual': False, 'status': 2, 'shared': False},
        {'name': 'VirtualBox Host-Only Network', 'device': 'VirtualBox Host-Only Ethernet Adapter', 'phys_media': '802.3', 'hardware': False, 'virtual': True, 'status': 2, 'shared': False},
        {'name': 'OpenVPN Wintun', 'device': 'Wintun Userspace Tunnel', 'phys_media': 'Unspecified', 'hardware': False, 'virtual': True, 'status': 2, 'shared': False},
        {'name': 'Ethernet 2', 'device': 'ASIX AX88179 USB 3.0 to Gigabit Ethernet Adapter', 'phys_media': '802.3', 'hardware': True, 'virtual': False, 'status': 7, 'shared': False},
        {'name': 'Ethernet', 'device': 'Realtek PCIe GbE Family Controller', 'phys_media': '802.3', 'hardware': True, 'virtual': False, 'status': 2, 'shared': False},
    ]
    post_adapters = [
        dict(a, ipv4=('192.168.137.1' if a['name'] == 'Ethernet' else ''))
        for a in adapters
    ]

    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'network_connections', side_effect=[{'adapters': adapters}, {'adapters': post_adapters}]), \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'ok': True, 'output': 'ICS enabled'}) as m_share, \
         mock.patch('time.sleep'):
        res = tmp_api.start_vpn_ethernet()

    assert res['ok'] is True
    assert res['vpn_adapter'] == 'OpenVPN Wintun'
    assert res['ethernet_adapter'] == 'Ethernet'
    assert res['ethernet_ip'] == '192.168.137.1'
    assert res['cable_connected'] is True
    assert tmp_api.ethernet_active is True
    assert tmp_api.ethernet_adapter == 'Ethernet'
    assert tmp_api.load_settings()['ethernet_adapter'] == 'Ethernet'
    m_share.assert_called_once_with('OpenVPN Wintun', 'Ethernet')

    # Switching from Ethernet to Hotspot stops Ethernet cleanly first
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'stop_vpn_ethernet', return_value={'ok': True, 'output': 'stopped'}) as m_stop_eth, \
         mock.patch.object(tmp_api, 'windows_info', return_value={'is_windows': True, 'supports_winrt_hotspot': True}), \
         mock.patch.object(tmp_api, '_driver_supports_hosted_network', return_value=False), \
         mock.patch.object(tmp_api, '_start_winrt_hotspot', return_value='Mobile Hotspot started'), \
         mock.patch.object(tmp_api, 'network_connections', return_value={'adapters': [
             {'name': 'OpenVPN Wintun', 'device': 'Wintun', 'status': 2},
             {'name': 'Local Area Connection* 12', 'device': 'Microsoft Wi-Fi Direct Virtual Adapter', 'status': 2},
         ]}), \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'ok': True, 'output': 'ok'}):
        hs_res = tmp_api.start_vpn_hotspot('NEON-NET', 'Pass12345')
    assert hs_res['ok'] is True
    m_stop_eth.assert_called_once()

    # Manual Ethernet override & emergency teardown on Conf drop
    tmp_api.hotspot_active = False
    tmp_api.ethernet_active = False
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'network_connections', return_value={'adapters': adapters}), \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'ok': True, 'output': 'ok'}) as m_share2, \
         mock.patch('time.sleep'):
        res2 = tmp_api.start_vpn_ethernet('Ethernet 2')
    assert res2['ok'] is True
    assert res2['ethernet_adapter'] == 'Ethernet 2'
    assert res2['cable_connected'] is False
    m_share2.assert_called_once_with('OpenVPN Wintun', 'Ethernet 2')

    with mock.patch.object(tmp_api, '_disable_app_shared_pair', return_value={'ok': True, 'output': 'disabled'}) as m_dis:
        tmp_api.app_shared_pair = ('OpenVPN Wintun', 'Ethernet 2')
        tmp_api._emergency_stop_sharing()
    assert tmp_api.ethernet_active is False
    assert tmp_api.ethernet_adapter is None
    m_dis.assert_called_once()


def test_broadcom_uplink_never_selected_for_ethernet_or_vpn_sharing(tmp_api):
    # Case 1: Broadcom Wi-Fi (including PCIe / Wireless LAN / ndis_medium=9) must classify as wifi, not ethernet
    broadcom_wifi = {
        'name': 'Беспроводная сеть',
        'device': 'Broadcom 802.11ac Network Adapter',
        'phys_media': 'Wireless LAN',
        'ndis_medium': 9,
        'hardware': True,
        'virtual': False,
        'status': 2,
        'has_default_gw': True,
        'default_gw': '192.168.1.1',
    }
    assert Api._classify_adapter(broadcom_wifi) == 'wifi'

    # Broadcom PCIe Wi-Fi misreporting empty/802.3 phys_media must still NOT match generic 'pcie' as ethernet if Wireless/ndis_medium=1
    broadcom_wifi_alt = {
        'name': 'Wi-Fi',
        'device': 'Broadcom BCM4360 PCIe Adapter',
        'phys_media': '802.3',
        'ndis_medium': 1,
        'hardware': True,
        'virtual': False,
        'status': 2,
        'has_default_gw': True,
    }
    assert Api._classify_adapter(broadcom_wifi_alt) == 'wifi'

    # Case 2: Broadcom NetLink Gigabit Ethernet receiving ordinary internet (has_default_gw=True) MUST be excluded
    # from Ethernet auto-selection when Realtek/USB Ethernet is available for TV, AND even if previously bound in ICS
    broadcom_isp = {
        'name': 'Broadcom',
        'device': 'Broadcom NetLink (TM) Gigabit Ethernet',
        'phys_media': '802.3',
        'ndis_medium': 14,
        'hardware': True,
        'virtual': False,
        'status': 2,
        'shared': True,
        'sharing_type': 0,  # e.g. Windows Mobile Hotspot bound ICS to Broadcom!
        'has_default_gw': True,
        'default_gw': '192.168.1.1',
    }
    tv_eth = {
        'name': 'Ethernet 2',
        'device': 'Realtek USB GbE Family Controller',
        'phys_media': '802.3',
        'ndis_medium': 14,
        'hardware': True,
        'virtual': False,
        'status': 2,
        'shared': False,
        'has_default_gw': False,
    }
    custom_ovpn = {
        'name': 'Local Area Connection 3',
        'device': 'TAP-Win32 Adapter V9',
        'phys_media': 'Unspecified',
        'hardware': False,
        'virtual': True,
        'status': 2,
        'shared': False,
        'has_vpn_route': True,
        'has_default_gw': False,
        'guid': '{11111111-2222-3333-4444-555555555555}',
        'if_index': 29,
    }

    # Verify OpenVPN log parser captures TAP/Wintun/DCO names & GUIDs
    class _FakeProc:
        def __init__(self, lines):
            self.stdout = __import__('io').StringIO('\n'.join(lines) + '\n')
        def poll(self):
            return 0
    tmp_api._vpn_stop_requested = True
    tmp_api._read_openvpn_log(_FakeProc([
        r'Thu Nov 10 10:00:00 2026 Open TAP device "Local Area Connection 3" PATH="\\.\Global\{11111111-2222-3333-4444-555555555555}.tap"',
        'Thu Nov 10 10:00:02 2026 Initialization Sequence Completed',
    ]))
    assert tmp_api.vpn_adapter_name == 'Local Area Connection 3'
    assert tmp_api.vpn_adapter_guid == '{11111111-2222-3333-4444-555555555555}'

    # Test with external/active OpenVPN adapter even if vpn_state is CONNECTED or external up
    tmp_api.vpn_state = 'DISCONNECTED'
    adapters = [broadcom_isp, tv_eth, custom_ovpn]
    post_adapters = [dict(a, ipv4=('192.168.137.1' if a['name'] == 'Ethernet 2' else '')) for a in adapters]

    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'network_connections', side_effect=[{'adapters': adapters}, {'adapters': post_adapters}]), \
         mock.patch.object(tmp_api, '_clear_conflicting_ics', return_value='Cleared non-VPN ICS') as m_clr, \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'ok': True, 'output': 'ok'}) as m_share, \
         mock.patch('time.sleep'):
        res = tmp_api.start_vpn_ethernet()

    assert res['ok'] is True
    assert res['vpn_adapter'] == 'Local Area Connection 3'
    assert res['ethernet_adapter'] == 'Ethernet 2'
    assert 'Broadcom' in res['output']
    m_clr.assert_called_once_with('Local Area Connection 3', 'Ethernet 2')
    m_share.assert_called_once_with('Local Area Connection 3', 'Ethernet 2')

    # Case 3: If ONLY Broadcom uplink exists and no separate TV Ethernet port is present, NEVER bind Broadcom — return error!
    tmp_api.vpn_state = 'CONNECTED'
    tmp_api.ethernet_active = False
    tmp_api.app_shared_pair = None
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'network_connections', return_value={'adapters': [broadcom_isp, custom_ovpn]}):
        blocked = tmp_api.start_vpn_ethernet()
    assert blocked.get('ok') is False
    assert 'Skipped "Broadcom"' in blocked.get('error', '')


def test_sharing_dns_validation_presets_and_persistence(tmp_api):
    presets = tmp_api.dns_presets()
    ids = [p['id'] for p in presets['presets']]
    assert ids == ['auto', 'cloudflare', 'google', 'quad9', 'adguard', 'opendns', 'yandex', 'custom']
    assert presets['current']['mode'] == 'auto'

    # Auto mode clears servers & uplink toggle
    v_auto = tmp_api.validate_dns_config('auto', '1.1.1.1', '8.8.8.8', True)
    assert v_auto['ok'] is True and v_auto['servers'] == [] and v_auto['apply_uplink'] is False

    # Preset auto-populates primary/secondary when fields are blank
    v_cf = tmp_api.validate_dns_config('cloudflare')
    assert v_cf['ok'] is True and v_cf['mode'] == 'cloudflare' and v_cf['servers'] == ['1.1.1.1', '1.0.0.1']

    # Editing preset values promotes to custom and deduplicates identical secondary
    v_edit = tmp_api.validate_dns_config('cloudflare', '9.9.9.9', '9.9.9.9', True)
    assert v_edit['ok'] is True and v_edit['mode'] == 'custom' and v_edit['servers'] == ['9.9.9.9'] and v_edit['apply_uplink'] is True

    # Invalid / unsafe IPv4 inputs rejected cleanly
    for bad in ('abc', '999.1.1.1', '0.0.0.0', '127.0.0.1', '169.254.10.1', '224.0.0.1', '255.255.255.255'):
        bad_res = tmp_api.validate_dns_config('custom', bad, '', False)
        assert bad_res['ok'] is False
    assert tmp_api.validate_dns_config('custom', '', '', False)['ok'] is False

    # Non-Windows persists cleanly in settings.json and resets back to auto
    with mock.patch('platform.system', return_value='Linux'):
        saved = tmp_api.apply_sharing_dns('google', '', '', True)
        assert saved['ok'] is True and saved['saved_only'] is True
        assert saved['dns_mode'] == 'google' and saved['servers'] == ['8.8.8.8', '8.8.4.4']
        st = tmp_api.load_settings()
        assert st['dns_mode'] == 'google'
        assert st['dns_primary'] == '8.8.8.8'
        assert st['dns_secondary'] == '8.8.4.4'
        assert st['dns_apply_uplink'] is True

        reset = tmp_api.reset_sharing_dns()
        assert reset['ok'] is True and reset['dns_mode'] == 'auto' and reset['servers'] == []
        st2 = tmp_api.load_settings()
        assert st2['dns_mode'] == 'auto'
        assert st2['dns_primary'] == ''
        assert st2['dns_apply_uplink'] is False


def test_sharing_dns_applied_to_ethernet_hotspot_uplink_restore_and_rebind(tmp_api):
    adapters = [
        {
            'name': 'Wi-Fi',
            'device': 'Intel Wi-Fi 6 AX200',
            'phys_media': 'Wireless LAN',
            'ndis_medium': 9,
            'hardware': True,
            'virtual': False,
            'status': 2,
            'shared': False,
            'has_default_gw': True,
            'dns_servers': ['192.168.1.1'],
            'category': 'wifi',
        },
        {
            'name': 'OpenVPN Wintun',
            'device': 'Wintun Userspace Tunnel',
            'status': 2,
            'shared': False,
            'has_vpn_route': True,
            'has_default_gw': False,
            'dns_servers': ['10.8.0.1'],
            'category': 'vpn',
        },
        {
            'name': 'Ethernet',
            'device': 'Realtek PCIe GbE Family Controller',
            'phys_media': '802.3',
            'ndis_medium': 14,
            'hardware': True,
            'virtual': False,
            'status': 2,
            'shared': False,
            'has_default_gw': False,
            'category': 'ethernet',
        },
        {
            'name': 'Local Area Connection* 12',
            'device': 'Microsoft Wi-Fi Direct Virtual Adapter',
            'status': 2,
            'shared': False,
            'category': 'hotspot',
        },
    ]
    post_eth = [dict(a, ipv4=('192.168.137.1' if a['name'] == 'Ethernet' else '')) for a in adapters]

    tmp_api.save_settings({
        'dns_mode': 'cloudflare',
        'dns_primary': '1.1.1.1',
        'dns_secondary': '1.0.0.1',
        'dns_apply_uplink': True,
    })
    tmp_api.vpn_state = 'CONNECTED'

    dns_calls = []

    def fake_set_dns(name, servers):
        dns_calls.append((name, list(servers)))
        if name == 'Wi-Fi':
            return {
                'ok': True,
                'adapter': name,
                'was_static': True,
                'prev_static': ['192.168.1.1'],
                'previous': ['192.168.1.1'],
                'current': list(servers),
            }
        return {'ok': True, 'adapter': name, 'was_static': False, 'prev_static': [], 'previous': [], 'current': list(servers)}

    # 1) Ethernet share applies DNS to Conf tunnel + Uplink before enabling ICS, and stop restores Uplink DNS
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'network_connections', side_effect=[{'adapters': adapters}, {'adapters': post_eth}]), \
         mock.patch.object(tmp_api, '_set_adapter_ipv4_dns', side_effect=fake_set_dns), \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'ok': True, 'output': 'ics ok'}) as m_share, \
         mock.patch('time.sleep'):
        res = tmp_api.start_vpn_ethernet()
    assert res['ok'] is True
    assert ('OpenVPN Wintun', ['1.1.1.1', '1.0.0.1']) in dns_calls
    assert ('Wi-Fi', ['1.1.1.1', '1.0.0.1']) in dns_calls
    assert tmp_api._uplink_dns_backups == {'Wi-Fi': ['192.168.1.1']}
    m_share.assert_called_once_with('OpenVPN Wintun', 'Ethernet')

    # 2) Live DNS update while Ethernet ICS is active rebinds ICS so Windows SharedAccess picks up new DNS
    dns_calls.clear()
    tmp_api.app_shared_pair = ('OpenVPN Wintun', 'Ethernet')
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'network_connections', return_value={'adapters': adapters}), \
         mock.patch.object(tmp_api, '_set_adapter_ipv4_dns', side_effect=fake_set_dns), \
         mock.patch.object(tmp_api, '_disable_app_shared_pair', return_value={'ok': True}) as m_dis, \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'ok': True, 'output': 'rebound'}) as m_rebind:
        live_res = tmp_api.apply_sharing_dns('custom', '9.9.9.9', '149.112.112.112', True)
    assert live_res['ok'] is True
    assert live_res['ics_refreshed'] is True
    assert ('OpenVPN Wintun', ['9.9.9.9', '149.112.112.112']) in dns_calls
    assert ('Wi-Fi', ['9.9.9.9', '149.112.112.112']) in dns_calls
    m_dis.assert_called_once()
    m_rebind.assert_called_once_with('OpenVPN Wintun', 'Ethernet')

    # 3) Stopping Ethernet sharing restores Uplink DNS backup cleanly
    dns_calls.clear()
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, '_disable_app_shared_pair', return_value={'ok': True, 'output': 'disabled'}), \
         mock.patch.object(tmp_api, '_set_adapter_ipv4_dns', side_effect=fake_set_dns):
        stop_res = tmp_api.stop_vpn_ethernet()
    assert stop_res['ok'] is True
    assert ('Wi-Fi', ['192.168.1.1']) in dns_calls
    assert tmp_api._uplink_dns_backups == {}

    # 4) WinRT Hotspot start applies DNS before ICS, live change warns cleanly without tearing down WinRT,
    #    and disconnect_vpn restores any Uplink backup
    dns_calls.clear()
    tmp_api.app_shared_pair = None
    tmp_api.save_settings({'dns_mode': 'adguard', 'dns_primary': '', 'dns_secondary': '', 'dns_apply_uplink': True})
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'windows_info', return_value={'is_windows': True, 'major': 10, 'minor': 0, 'build': 22621, 'label': 'Windows 11', 'supports_winrt_hotspot': True}), \
         mock.patch.object(tmp_api, '_driver_supports_hosted_network', return_value=False), \
         mock.patch.object(tmp_api, '_start_winrt_hotspot', return_value='ok'), \
         mock.patch.object(tmp_api, 'network_connections', return_value={'adapters': adapters}), \
         mock.patch.object(tmp_api, '_set_adapter_ipv4_dns', side_effect=fake_set_dns), \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'ok': True, 'output': 'ok'}) as m_hs_share:
        hs_res = tmp_api.start_vpn_hotspot('NEON-DNS', 'Pass12345')
    assert hs_res['ok'] is True
    assert ('OpenVPN Wintun', ['94.140.14.14', '94.140.15.15']) in dns_calls
    assert ('Wi-Fi', ['94.140.14.14', '94.140.15.15']) in dns_calls
    m_hs_share.assert_called_once_with('OpenVPN Wintun', 'Local Area Connection* 12')

    tmp_api.app_shared_pair = ('OpenVPN Wintun', 'Local Area Connection* 12')
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'network_connections', return_value={'adapters': adapters}), \
         mock.patch.object(tmp_api, '_set_adapter_ipv4_dns', side_effect=fake_set_dns), \
         mock.patch.object(tmp_api, '_disable_app_shared_pair') as m_no_dis:
        hs_upd = tmp_api.apply_sharing_dns('google', '', '', False)
    assert hs_upd['ok'] is True and hs_upd['ics_refreshed'] is False
    assert 'Mobile Hotspot' in hs_upd['output']
    m_no_dis.assert_not_called()
    assert ('Wi-Fi', ['192.168.1.1']) in dns_calls
    assert tmp_api._uplink_dns_backups == {}

    # 5) Rollback Uplink DNS backup if set_sharing fails during start_vpn_ethernet
    dns_calls.clear()
    tmp_api.ethernet_active = False
    tmp_api.hotspot_active = False
    tmp_api.app_shared_pair = None
    tmp_api.save_settings({'dns_mode': 'cloudflare', 'dns_primary': '1.1.1.1', 'dns_secondary': '1.0.0.1', 'dns_apply_uplink': True})
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(tmp_api, 'network_connections', return_value={'adapters': adapters}), \
         mock.patch.object(tmp_api, '_set_adapter_ipv4_dns', side_effect=fake_set_dns), \
         mock.patch.object(tmp_api, 'set_sharing', return_value={'error': 'ICS denied'}):
        fail_eth = tmp_api.start_vpn_ethernet('Ethernet')
    assert fail_eth['ok'] is False
    assert tmp_api._uplink_dns_backups == {}
    assert ('Wi-Fi', ['192.168.1.1']) in dns_calls


def test_belet_wgt_service_manifest_es5_and_fallback(tmp_path):
    import io
    import re
    import shutil
    import subprocess
    import xml.etree.ElementTree as ET
    import zipfile
    from samsung_tizen import build_belet_wgt

    wgt_svc = build_belet_wgt(target_url='https://belet.tm/ru', pkg_id='BeletTV001', app_name='Belet', enable_service=True, tv_ip='192.168.1.99')
    with zipfile.ZipFile(io.BytesIO(wgt_svc), 'r') as zf:
        names = set(zf.namelist())
        assert {'config.xml', 'index.html', 'neon-nav.js', 'service/proxy.js', 'icon.png'}.issubset(names)
        cfg = zf.read('config.xml').decode('utf-8')
        idx_html = zf.read('index.html').decode('utf-8')
        nav_js = zf.read('neon-nav.js').decode('utf-8')
        proxy_js = zf.read('service/proxy.js').decode('utf-8')

    root = ET.fromstring(cfg)
    assert root.attrib.get('version') == '1.3.0'
    assert 'required_version="3.0"' in cfg
    assert 'http://tizen.org/feature/web.service' in cfg
    assert '<tizen:service id="BeletTV001.BeletProxy" auto-restart="true">' in cfg
    assert '<tizen:content src="service/proxy.js"/>' in cfg
    assert 'on-boot=' not in cfg
    assert 'background-support="enable"' in cfg
    assert 'http://tizen.org/privilege/network.public' not in cfg
    assert 'http://tizen.org/privilege/application.info' not in cfg
    assert 'register.keygrab.mode.special' not in cfg
    assert 'tizen:allow-navigation' not in cfg
    assert 'use.keypad.without.useraction' not in cfg
    assert 'wrt:' not in cfg
    for priv in (
        'http://tizen.org/privilege/internet',
        'http://developer.samsung.com/privilege/network.public',
        'http://tizen.org/privilege/tv.inputdevice',
        'http://tizen.org/privilege/application.launch',
    ):
        assert priv in cfg
    assert 'hwkey-event="enable"' in cfg
    assert 'floating.navigation' in cfg
    assert '$WEBAPIS/webapis/webapis.js' in idx_html
    assert 'var HINT_TV_IP="192.168.1.99";' in idx_html

    # Invalid/injection tv_ip hint must be sanitized to empty string
    wgt_bad_ip = build_belet_wgt(target_url='https://belet.tm/ru', tv_ip='1.2.3.4";alert(1)//')
    with zipfile.ZipFile(io.BytesIO(wgt_bad_ip), 'r') as zf:
        assert 'var HINT_TV_IP="";' in zf.read('index.html').decode('utf-8')

    # Tier-2 fallback package (Tizen 2.x or manifest refusal fallback)
    wgt_nosvc = build_belet_wgt(target_url='https://belet.tm/ru', pkg_id='BeletTV001', app_name='Belet', enable_service=False)
    with zipfile.ZipFile(io.BytesIO(wgt_nosvc), 'r') as zf:
        nosvc_names = set(zf.namelist())
        assert 'service/proxy.js' not in nosvc_names
        assert {'config.xml', 'index.html', 'icon.png'}.issubset(nosvc_names)
        nosvc_cfg = zf.read('config.xml').decode('utf-8')
        nosvc_html = zf.read('index.html').decode('utf-8')
        assert 'version="1.3.0"' in nosvc_cfg
        assert 'required_version="2.3"' in nosvc_cfg
        assert '<tizen:service' not in nosvc_cfg
        assert 'https://belet.tm/ru' in nosvc_html
        assert 'var ENABLE_SVC=false;' in nosvc_html
        assert '<iframe id="beletFrame"' in nosvc_html
        assert 'btnBrowser' not in nosvc_html
        assert 'Открыть напрямую' not in nosvc_html

    # Self-contained service/proxy.js must not rely on __dirname or relative require() (breaks Tizen 10 vm.runInThisContext)
    assert '__dirname' not in proxy_js
    assert '__filename' not in proxy_js
    assert "require('./" not in proxy_js
    assert "require('fs')" not in proxy_js
    assert '(function(nodeRequire,hostModule,hostExports){' in proxy_js

    m_script = re.search(r'<script>\s*(.*?)\s*</script>', idx_html, flags=re.S)
    assert m_script, 'index.html must contain inline <script>'
    idx_js = m_script.group(1)

    # Strict ES5 checks across outer JS, injected nav JS, and background service JS
    idx_js_no_wrt = re.sub(r'"import \{ Service \} from \'wrt:service\';[^"]*"', '""', idx_js)
    for label, src in (('index.html', idx_js_no_wrt), ('neon-nav.js', nav_js), ('service/proxy.js', proxy_js)):
        assert not re.search(r'\b(const|let)\b', src), f'{label} must be ES5 (no const/let)'
        assert '=>' not in src, f'{label} must be ES5 (no arrow functions)'
        assert '`' not in src, f'{label} must be ES5 (no template literals)'
        assert '.closest(' not in src, f'{label} must not use .closest()'
        assert '.includes(' not in src, f'{label} must not use .includes()'
    assert '[aria-label*="close" i]' not in nav_js
    assert '.isConnected&&' not in nav_js
    assert '10009' in nav_js and '10009' in idx_js
    assert 'tizenhwkey' in idx_js

    node_exe = shutil.which('node') or shutil.which('nodejs')
    assert node_exe, 'Node.js runtime must be available for JS verification'
    for fname, src in (('idx.js', idx_js), ('nav.js', nav_js), ('proxy.js', proxy_js)):
        p = tmp_path / fname
        p.write_text(src, encoding='utf-8')
        chk = subprocess.run([node_exe, '--check', str(p)], capture_output=True, text=True)
        assert chk.returncode == 0, f'node --check failed for {fname}: {chk.stderr}'


def test_belet_proxy_service_live_http_rewriting_and_cookies(tmp_path):
    import io
    import shutil
    import subprocess
    import zipfile
    from samsung_tizen import build_belet_wgt

    wgt_bytes = build_belet_wgt(target_url='https://belet.tm/', pkg_id='BeletTV001', app_name='Belet', enable_service=True)
    with zipfile.ZipFile(io.BytesIO(wgt_bytes), 'r') as zf:
        proxy_src = zf.read('service/proxy.js').decode('utf-8')
        nav_js = zf.read('neon-nav.js').decode('utf-8')

    runner = tmp_path / 'test_proxy_live.js'
    runner.write_text(
        """
const assert = require('assert');
const http = require('http');
const net = require('net');
const zlib = require('zlib');
const vm = require('vm');

const rawProxySrc = """ + json.dumps(proxy_src) + """;
const jarFile = """ + json.dumps(str(tmp_path / 'cookie-test.json')) + """;
const expectedNav = """ + json.dumps(nav_js) + """;

let sawApiHeaders = null;
let sawApiBody = '';
let sawWsUpgradeCookie = '';

const upstream = http.createServer((req, res) => {
  if (req.url === '/') {
    const html = [
      '<!DOCTYPE html><html><head>',
      '<meta http-equiv="Content-Security-Policy" content="default-src \\'self\\'">',
      '<meta http-equiv="X-Frame-Options" content="SAMEORIGIN">',
      '<meta http-equiv="refresh" content="5;url=http://127.0.0.1:' + upstream.address().port + '/ru">',
      '<base href="https://belet.tm/">',
      '<title>Belet Mock</title>',
      '<script src="/_next/app.js" integrity="sha256-abc" crossorigin="anonymous"></script>',
      '</head><body>',
      '<a href="http://127.0.0.1:' + upstream.address().port + '/movies">Movies</a>',
      '<a href="//127.0.0.1:' + upstream.address().port + '/series">Series</a>',
      '<a href="https://app.127.0.0.1:' + upstream.address().port + '/ru">App</a>',
      '<img srcset="http://127.0.0.1:' + upstream.address().port + '/p1.jpg 1x, https://cdn.127.0.0.1:' + upstream.address().port + '/p2.jpg 2x">',
      '<form action="http://127.0.0.1:' + upstream.address().port + '/search"></form>',
      '</body></html>',
    ].join('');
    const gz = zlib.gzipSync(Buffer.from(html, 'utf8'));
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Encoding': 'gzip',
      'X-Frame-Options': 'DENY',
      'Content-Security-Policy': "default-src 'self'",
      'X-WebKit-CSP': "default-src 'self'",
      'Cross-Origin-Resource-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Set-Cookie': ['sid=xyz; Path=/; Secure; Domain=belet.tm; SameSite=None'],
    });
    res.end(gz);
  } else if (req.url === '/redir') {
    res.writeHead(302, {
      'Location': 'http://127.0.0.1:' + upstream.address().port + '/ru',
    });
    res.end();
  } else if (req.url === '/action-redir') {
    res.writeHead(200, {
      'Content-Type': 'text/x-component',
      'x-action-redirect': 'http://127.0.0.1:' + upstream.address().port + '/ru;push',
      'x-nextjs-redirect': 'http://127.0.0.1:' + upstream.address().port + '/home',
    });
    res.end('0:{}');
  } else if (req.url === '/qr-login') {
    res.writeHead(200, {
      'Content-Type': 'application/json',
      'Set-Cookie': [
        '__Host-belet_sess=tok123; Path=/; HttpOnly; Secure; SameSite=Lax',
        '__Secure-ref=ref456; Path=/; Secure',
      ],
    });
    res.end(JSON.stringify({ ok: true }));
  } else if (req.url === '/qr-logout') {
    res.writeHead(200, {
      'Content-Type': 'application/json',
      'Set-Cookie': ['__Host-belet_sess=; Path=/; Max-Age=0'],
    });
    res.end(JSON.stringify({ ok: true }));
  } else if (req.url === '/me') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ cookie: req.headers.cookie || '', referer: req.headers.referer || '' }));
  } else if (req.url === '/api/login' && req.method === 'POST') {
    sawApiHeaders = req.headers;
    let chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      sawApiBody = Buffer.concat(chunks).toString('utf8');
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });
  } else if (req.url === '/empty') {
    res.writeHead(204);
    res.end();
  } else {
    res.writeHead(404);
    res.end('nope');
  }
});

upstream.on('upgrade', (req, sock) => {
  sawWsUpgradeCookie = req.headers.cookie || '';
  sock.on('error', () => {});
  sock.on('end', () => sock.destroy());
  sock.write(
    'HTTP/1.1 101 Switching Protocols\\r\\n' +
    'Upgrade: websocket\\r\\n' +
    'Connection: Upgrade\\r\\n' +
    'Set-Cookie: __Host-ws=wsk1; Path=/; HttpOnly; Secure\\r\\n\\r\\n'
  );
  sock.on('data', (d) => sock.write(d));
});

function httpReq(port, method, path, headers, body) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port, method, path, headers: headers || {} }, (res) => {
      const ch = [];
      res.on('data', (d) => ch.push(d));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(ch).toString('utf8') }));
    });
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

upstream.listen(0, '127.0.0.1', async () => {
  try {
    const upPort = upstream.address().port;
    const patched = rawProxySrc
      .replace(/var TARGET_URL="https:\\/\\/belet\\.tm\\/";/, 'var TARGET_URL="http://127.0.0.1:' + upPort + '/";')
      .replace(/var PORT=\\d+;/, 'var PORT=0;')
      .replace('loadJar();', 'JAR_FILE=' + JSON.stringify(jarFile) + ';loadJar();');

    const mod = { exports: {} };
    const sandbox = {
      require,
      module: mod,
      exports: mod.exports,
      Buffer,
      console,
      process,
      setTimeout,
      clearTimeout,
    };
    vm.runInNewContext(patched, sandbox);
    const srv = mod.exports.server;
    await new Promise((res) => {
      if (srv.listening) return res();
      srv.once('listening', res);
    });
    const proxyPort = srv.address().port;

    const ping = await httpReq(proxyPort, 'GET', '/__neon_ping', { 'Origin': 'file://' });
    assert.strictEqual(ping.status, 200);
    const pingData = JSON.parse(ping.body);
    assert.strictEqual(pingData.ok, true);
    assert.strictEqual(pingData.ver, '1.2.7');
    assert.strictEqual(ping.headers['access-control-allow-origin'], '*');
    assert.strictEqual(ping.headers['access-control-allow-credentials'], undefined);

    const nav = await httpReq(proxyPort, 'GET', '/__neon_nav.js');
    assert.strictEqual(nav.status, 200);
    assert.strictEqual(nav.body, expectedNav);

    const page = await httpReq(proxyPort, 'GET', '/', {
      'Sec-Fetch-Dest': 'iframe',
      'Sec-Fetch-Site': 'cross-site',
      'Sec-Fetch-Mode': 'navigate',
    });
    assert.strictEqual(page.status, 200);
    assert.strictEqual(page.headers['x-frame-options'], undefined);
    assert.strictEqual(page.headers['content-security-policy'], undefined);
    assert.strictEqual(page.headers['x-webkit-csp'], undefined);
    assert.strictEqual(page.headers['cross-origin-embedder-policy'], undefined);
    assert.strictEqual(page.headers['cross-origin-opener-policy'], undefined);
    assert.strictEqual(page.headers['cross-origin-resource-policy'], 'cross-origin');
    assert.strictEqual(page.headers['access-control-allow-origin'], '*');
    const cookieStr = (page.headers['set-cookie'] || []).join(';');
    assert(!/Secure/i.test(cookieStr));
    assert(!/Domain=/i.test(cookieStr));
    assert(/SameSite=Lax/i.test(cookieStr));
    assert(!page.body.includes('Content-Security-Policy'));
    assert(!page.body.includes('X-Frame-Options'));
    assert(!page.body.includes('<base '));
    assert(!page.body.includes('integrity='));
    assert(!page.body.includes('crossorigin='));
    assert(page.body.includes('<head><script src="/__neon_nav.js" data-neon="1"></script>'));
    assert(page.body.includes('href="/movies"'));
    assert(page.body.includes('href="/series"'));
    assert(page.body.includes('url=/ru"'));
    assert(page.body.includes('href="/__neon_host/app.127.0.0.1/ru"'));
    assert(page.body.includes('srcset="/p1.jpg 1x, /__neon_host/cdn.127.0.0.1/p2.jpg 2x"'));
    assert(page.body.includes('action="/search"'));

    const redir = await httpReq(proxyPort, 'GET', '/redir');
    assert.strictEqual(redir.status, 302);
    assert.strictEqual(redir.headers.location, 'http://127.0.0.1:' + proxyPort + '/ru');

    const actRedir = await httpReq(proxyPort, 'GET', '/action-redir');
    assert.strictEqual(actRedir.status, 200);
    assert.strictEqual(actRedir.headers['x-action-redirect'], 'http://127.0.0.1:' + proxyPort + '/ru;push');
    assert.strictEqual(actRedir.headers['x-nextjs-redirect'], 'http://127.0.0.1:' + proxyPort + '/home');

    // Server-side Cookie Jar preserves __Host-*, __Secure-*, HttpOnly, and client /__neon_cookie writes across requests, plus path-preserving Referer
    const qrLogin = await httpReq(proxyPort, 'GET', '/qr-login');
    assert.strictEqual(qrLogin.status, 200);
    const persisted = JSON.parse(require('fs').readFileSync(jarFile, 'utf8'));
    assert(persisted.some(c => c.name === '__Host-belet_sess'));
    const ckSync = await httpReq(proxyPort, 'POST', '/__neon_cookie', { 'Content-Type': 'application/json' }, JSON.stringify({ cookie: '__Host-client=cl999; Path=/; Secure' }));
    assert.strictEqual(ckSync.status, 204);
    const me1 = await httpReq(proxyPort, 'GET', '/me', { 'Referer': 'http://127.0.0.1:' + proxyPort + '/ru/login' });
    const me1Data = JSON.parse(me1.body);
    assert(me1Data.cookie.includes('__Host-belet_sess=tok123'));
    assert(me1Data.cookie.includes('__Secure-ref=ref456'));
    assert(me1Data.cookie.includes('__Host-client=cl999'));
    assert.strictEqual(me1Data.referer, 'http://127.0.0.1:' + upPort + '/ru/login');

    // WebSocket upgrade tunnel forwards Cookie Jar cookies, strips Secure on 101 Set-Cookie, stores WS Set-Cookie, and pipes payload
    await new Promise((resolve, reject) => {
      const sock = net.connect(proxyPort, '127.0.0.1', () => {
        sock.write(
          'GET /ws-qr HTTP/1.1\\r\\n' +
          'Host: 127.0.0.1:' + proxyPort + '\\r\\n' +
          'Upgrade: websocket\\r\\n' +
          'Connection: Upgrade\\r\\n' +
          'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\\r\\n' +
          'Sec-WebSocket-Version: 13\\r\\n\\r\\n'
        );
      });
      let buf = '';
      let sentPing = false;
      sock.on('data', (chunk) => {
        buf += chunk.toString('utf8');
        if (!sentPing && buf.includes('\\r\\n\\r\\n')) {
          assert(buf.startsWith('HTTP/1.1 101'));
          assert(!/Secure/i.test(buf));
          sentPing = true;
          sock.write('PING_QR');
        } else if (sentPing && buf.includes('PING_QR')) {
          sock.destroy();
          resolve();
        }
      });
      sock.on('error', reject);
    });
    assert(sawWsUpgradeCookie.includes('__Host-belet_sess=tok123'));

    // Logout Max-Age=0 deletes cookie from Jar, while WS Set-Cookie persists
    await httpReq(proxyPort, 'GET', '/qr-logout');
    assert(!JSON.parse(require('fs').readFileSync(jarFile, 'utf8')).some(c => c.name === '__Host-belet_sess'));
    const me2 = await httpReq(proxyPort, 'GET', '/me');
    const me2Data = JSON.parse(me2.body);
    assert(!me2Data.cookie.includes('__Host-belet_sess=tok123'));
    assert(me2Data.cookie.includes('__Host-ws=wsk1'));

    const api = await httpReq(
      proxyPort,
      'POST',
      '/api/login',
      {
        'Content-Type': 'application/json',
        'X-Forwarded-Host': 'evil.local',
        'X-Forwarded-For': '1.2.3.4',
        'Sec-Fetch-Dest': 'iframe',
        'Sec-Fetch-Site': 'cross-site',
        'Origin': 'http://127.0.0.1:' + proxyPort,
      },
      '{"phone":"99361000000"}'
    );
    assert.strictEqual(api.status, 200);
    assert.strictEqual(JSON.parse(api.body).ok, true);
    assert.strictEqual(sawApiBody, '{"phone":"99361000000"}');
    assert.strictEqual(sawApiHeaders.host, '127.0.0.1:' + upPort);
    assert.strictEqual(sawApiHeaders['x-forwarded-host'], undefined);
    assert.strictEqual(sawApiHeaders['x-forwarded-for'], undefined);
    assert.strictEqual(sawApiHeaders['sec-fetch-dest'], undefined);
    assert.strictEqual(sawApiHeaders['sec-fetch-site'], undefined);

    const empty = await httpReq(proxyPort, 'GET', '/empty');
    assert.strictEqual(empty.status, 204);

    const head = await httpReq(proxyPort, 'HEAD', '/');
    assert.strictEqual(head.status, 200);
    assert.strictEqual(head.body, '');

    // Tizen 10 vm.runInThisContext style: global require/module, no __dirname, lifecycle onStop closes cleanly
    assert.strictEqual(typeof mod.exports.onStart, 'function');
    assert.strictEqual(typeof mod.exports.onRequest, 'function');
    assert.strictEqual(typeof mod.exports.onStop, 'function');
    assert.strictEqual(typeof mod.exports.onExit, 'function');
    mod.exports.onStop();

    // Tizen 10 bare vm.runInThisContext style without global require/module (uses process.getBuiltinModule / mainModule)
    if (typeof process.getBuiltinModule === 'function' || (process.mainModule && typeof process.mainModule.require === 'function')) {
      const bareGlobal = { Buffer, console, process, setTimeout, clearTimeout };
      bareGlobal.global = bareGlobal;
      vm.runInNewContext(patched, bareGlobal);
      assert.strictEqual(typeof bareGlobal.onStart, 'function');
      assert.strictEqual(typeof bareGlobal.onStop, 'function');
      bareGlobal.onStop();
    }
    if (upstream.closeAllConnections) upstream.closeAllConnections();
    upstream.close();
  } catch (err) {
    console.error(err);
    process.exit(1);
  }
});
""",
        encoding='utf-8',
    )
    node_exe = shutil.which('node') or shutil.which('nodejs')
    chk = subprocess.run([node_exe, str(runner)], capture_output=True, text=True, timeout=20)
    assert chk.returncode == 0, chk.stderr + chk.stdout


def test_belet_nav_js_spatial_nav_ime_cursor_and_back(tmp_path):
    import io
    import shutil
    import subprocess
    import zipfile
    from samsung_tizen import build_belet_wgt

    wgt_bytes = build_belet_wgt(target_url='https://belet.tm/', pkg_id='BeletTV001', app_name='Belet')
    with zipfile.ZipFile(io.BytesIO(wgt_bytes), 'r') as zf:
        nav_js = zf.read('neon-nav.js').decode('utf-8')

    runner = tmp_path / 'test_nav_vm.js'
    runner.write_text(
        """
const assert = require('assert');
const vm = require('vm');
const navSrc = """ + json.dumps(nav_js) + """;

function makeEl(tag, opts) {
  opts = opts || {};
  const attrs = Object.assign({}, opts.attrs || {});
  const el = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    id: opts.id || '',
    type: opts.type || '',
    className: opts.className || '',
    value: opts.value || '',
    selectionStart: opts.selectionStart || 0,
    disabled: !!opts.disabled,
    readOnly: !!opts.readOnly,
    tabIndex: typeof opts.tabIndex === 'number' ? opts.tabIndex : -1,
    style: Object.assign({}, opts.style || {}),
    children: [],
    parentElement: null,
    parentNode: null,
    form: opts.form || null,
    _rect: opts.rect || { left: 0, top: 0, width: 100, height: 40, right: 100, bottom: 40 },
    _computed: Object.assign({ display: 'block', visibility: 'visible', opacity: '1', cursor: 'default', borderRadius: '8px', position: 'static', overflowX: 'visible', overflowY: 'visible' }, opts.computed || {}),
    clicks: 0,
    focusedCount: 0,
    blurredCount: 0,
    dispatched: [],
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) || (k === 'id' && !!this.id); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : (k === 'id' ? this.id : null); },
    setAttribute(k, v) { attrs[k] = String(v); if (k === 'id') this.id = String(v); },
    removeAttribute(k) { delete attrs[k]; },
    getBoundingClientRect() { return this._rect; },
    appendChild(child) { child.parentElement = this; child.parentNode = this; this.children.push(child); return child; },
    contains(other) {
      let c = other;
      while (c) { if (c === this) return true; c = c.parentElement; }
      return false;
    },
    click() { this.clicks++; },
    focus() { this.focusedCount++; doc.activeElement = this; },
    blur() { this.blurredCount++; if (doc.activeElement === this) doc.activeElement = doc.body; },
    scrollIntoView() {},
    dispatchEvent(ev) { this.dispatched.push(ev); return true; },
    querySelectorAll(sel) {
      const res = [];
      function walk(node) {
        for (const ch of node.children || []) {
          if (matchesSimple(ch, sel)) res.push(ch);
          walk(ch);
        }
      }
      walk(this);
      return res;
    },
    querySelector(sel) {
      return this.querySelectorAll(sel)[0] || null;
    },
  };
  return el;
}

function matchesSimple(el, sel) {
  const parts = sel.split(',').map((s) => s.trim());
  for (const p of parts) {
    if (p === 'a[href]' && el.tagName === 'A' && el.hasAttribute('href')) return true;
    if (p === 'a' && el.tagName === 'A') return true;
    if (p === 'p' && el.tagName === 'P') return true;
    if (p === 'span' && el.tagName === 'SPAN') return true;
    if (p === 'button' && el.tagName === 'BUTTON') return true;
    if (p === 'input:not([type="hidden"])' && el.tagName === 'INPUT' && el.type !== 'hidden') return true;
    if (p === 'div' && el.tagName === 'DIV') return true;
    if (p === 'video,audio' || p === 'video') { if (el.tagName === 'VIDEO') return true; }
    if (p === '[role="dialog"]' && el.getAttribute('role') === 'dialog') return true;
    if (p === '[role="button"]' && el.getAttribute('role') === 'button') return true;
    if (p === '[role="tab"]' && el.getAttribute('role') === 'tab') return true;
    if (p === '[data-close]' && el.hasAttribute('data-close')) return true;
    if (p === 'button[type="submit"]' && el.tagName === 'BUTTON' && el.type === 'submit') return true;
  }
  return false;
}

const head = makeEl('head');
const body = makeEl('body', { rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
const docEl = makeEl('html', { rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
docEl.appendChild(head);
docEl.appendChild(body);

const navHome = makeEl('a', { id: 'nav-home', attrs: { href: '/' }, rect: { left: 40, top: 20, width: 120, height: 40, right: 160, bottom: 60 } });
const formSub = makeEl('button', { id: 'btn-search', type: 'submit', rect: { left: 460, top: 90, width: 90, height: 44, right: 550, bottom: 134 } });
const searchForm = makeEl('form', { rect: { left: 40, top: 90, width: 510, height: 44, right: 550, bottom: 134 } });
const searchInput = makeEl('input', { id: 'inp-search', type: 'search', form: searchForm, rect: { left: 40, top: 90, width: 400, height: 44, right: 440, bottom: 134 } });
searchForm.appendChild(searchInput);
searchForm.appendChild(formSub);

// React cursor:pointer card (no <a>/<button> inside) + hidden invisible ghost card
const card1 = makeEl('div', { id: 'card-1', computed: { cursor: 'pointer' }, rect: { left: 40, top: 200, width: 220, height: 300, right: 260, bottom: 500 } });
const card1Title = makeEl('div', { id: 'card-1-title', computed: { cursor: 'pointer' }, rect: { left: 48, top: 460, width: 180, height: 24, right: 228, bottom: 484 } });
card1.appendChild(card1Title);
const card2 = makeEl('div', { id: 'card-2', computed: { cursor: 'pointer' }, rect: { left: 290, top: 200, width: 220, height: 300, right: 510, bottom: 500 } });
const hiddenCard = makeEl('div', { id: 'card-hidden', computed: { cursor: 'pointer', opacity: '0' }, rect: { left: 540, top: 200, width: 220, height: 300, right: 760, bottom: 500 } });
const video = makeEl('video', { id: 'vid' });
video.paused = true;
video.currentTime = 20;
video.duration = 120;
video.play = function() { this.paused = false; };
video.pause = function() { this.paused = true; };

body.appendChild(navHome);
body.appendChild(searchForm);
body.appendChild(card1);
body.appendChild(card2);
body.appendChild(hiddenCard);
body.appendChild(video);

const docListeners = {};
const winListeners = {};
const timers = [];
const parentMsgs = [];
let historyBacks = 0;

const doc = {
  readyState: 'complete',
  documentElement: docEl,
  head,
  body,
  activeElement: body,
  createElement(t) { return makeEl(t); },
  createTextNode(s) { return { nodeType: 3, textContent: s }; },
  getElementById(id) {
    let found = null;
    function w(n) {
      if (n.id === id) found = n;
      for (const c of n.children || []) if (!found) w(c);
    }
    w(docEl);
    return found;
  },
  querySelectorAll(s) { return docEl.querySelectorAll(s); },
  querySelector(s) { return docEl.querySelector(s); },
  elementFromPoint(x, y) {
    if (x >= 290 && x <= 510 && y >= 200 && y <= 500) return card2;
    return body;
  },
  addEventListener(ev, fn) { docListeners[ev] = fn; },
  dispatchEvent() {},
};

const win = {
  innerWidth: 1920,
  innerHeight: 1080,
  scrollY: 0,
  document: doc,
  getComputedStyle(el) { return el._computed || { display: 'block', visibility: 'visible', opacity: '1', cursor: 'default' }; },
  addEventListener(ev, fn) { winListeners[ev] = fn; },
  scrollBy(dx, dy) { this.scrollY += dy; },
  parent: { postMessage(msg) { parentMsgs.push(msg); } },
  history: {
    length: 1,
    pushState() { throw new Error('SecurityError'); },
    replaceState() { throw new Error('SecurityError'); },
    back() { historyBacks++; },
  },
};
win.window = win;

let fakeNow = 10000;
const sandbox = {
  window: win,
  document: doc,
  Date: { now: () => fakeNow },
  location: { protocol: 'http:', hostname: '127.0.0.1', origin: 'http://127.0.0.1:18765', pathname: '/', search: '', assign() {} },
  navigator: { userAgent: 'Tizen' },
  MouseEvent: function(type, opts) { this.type = type; Object.assign(this, opts); },
  KeyboardEvent: function(type, opts) { this.type = type; Object.assign(this, opts); },
  PopStateEvent: function(type, opts) { this.type = type; Object.assign(this, opts); },
  setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
  clearTimeout() {},
  console,
};
vm.runInNewContext(navSrc, sandbox);

// Hydrated ring must NOT exist synchronously before boot timer runs (React hydration guard!)
assert.strictEqual(doc.getElementById('neon-tv-ring'), null);
timers.shift().fn();
assert(doc.getElementById('neon-tv-ring'), '#neon-tv-ring injected after boot delay');
assert.strictEqual(doc.activeElement, navHome);

// ArrowDown onto searchInput must NOT call searchInput.focus() (prevents premature IME keyboard!)
winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(searchInput.focusedCount, 0);

// Pressing OK on searchInput opens IME (.focus())
winListeners.keydown({ keyCode: 13, key: 'Enter', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(searchInput.focusedCount, 1);
assert.strictEqual(doc.activeElement, searchInput);

// IME_DONE (65376) submits form and blurs input
winListeners.keydown({ keyCode: 65376, key: '', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(formSub.clicks, 1);
assert.strictEqual(searchInput.blurredCount, 1);

// Focus again, then ArrowDown escapes single-line input to card1 (NOT card-hidden, NOT duplicate card1Title!)
winListeners.keydown({ keyCode: 13, key: 'Enter', preventDefault() {}, stopPropagation() {} });
winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(doc.activeElement, card1);

// ArrowRight moves to card2, OK clicks React cursor:pointer card2
winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(doc.activeElement, card2);
winListeners.keydown({ keyCode: 13, key: 'Enter', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(card2.clicks, 1);

// Blue key (406) toggles cursor, arrows move cursor, Back (10009) exits cursor mode cleanly
winListeners.keydown({ keyCode: 406, key: '', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(doc.getElementById('neon-tv-cursor').style.display, 'block');
winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(doc.getElementById('neon-tv-cursor').style.display, 'none');
assert(parentMsgs.some((m) => m.type === 'neon-back-handled'));

// Media keys control <video>
winListeners.keydown({ keyCode: 415, preventDefault() {}, stopPropagation() {} });
assert.strictEqual(video.paused, false);
winListeners.keydown({ keyCode: 417, preventDefault() {}, stopPropagation() {} });
assert.strictEqual(video.currentTime, 30);

// file:// SecurityError pushState uses virtualStack -> Back pops virtualStack without exiting;
// then on root (/), 1st Back warns (neon-back-at-root), <220ms bounce is ignored, 2nd Back (after 500ms) exits!
win.history.pushState({ p: 1 }, '', '/movie/1');
fakeNow += 500;
winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {}, stopPropagation() {} });
assert(!parentMsgs.some((m) => m.type === 'neon-exit'));
fakeNow += 500;
winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {}, stopPropagation() {} });
assert(!parentMsgs.some((m) => m.type === 'neon-exit'));
assert(parentMsgs.some((m) => m.type === 'neon-back-at-root'));
fakeNow += 100; // <220ms repeat bounce must NOT exit
winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {}, stopPropagation() {} });
assert(!parentMsgs.some((m) => m.type === 'neon-exit'));
fakeNow += 500; // within 220..3200ms confirms exit
winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {}, stopPropagation() {} });
assert(parentMsgs.some((m) => m.type === 'neon-exit'));
""",
        encoding='utf-8',
    )
    node_exe = shutil.which('node') or shutil.which('nodejs')
    chk = subprocess.run([node_exe, str(runner)], capture_output=True, text=True, timeout=15)
    assert chk.returncode == 0, chk.stderr + chk.stdout


def test_belet_index_html_tier1_tier2_and_back_handshake(tmp_path):
    import io
    import re
    import shutil
    import subprocess
    import zipfile
    from samsung_tizen import build_belet_wgt

    wgt_bytes = build_belet_wgt(target_url='https://belet.tm/ru', pkg_id='BeletTV001', app_name='Belet', enable_service=True)
    with zipfile.ZipFile(io.BytesIO(wgt_bytes), 'r') as zf:
        idx_html = zf.read('index.html').decode('utf-8')
        nav_js = zf.read('neon-nav.js').decode('utf-8')
    idx_js = re.search(r'<script>\s*(.*?)\s*</script>', idx_html, re.S).group(1)

    wgt_hint = build_belet_wgt(target_url='https://belet.tm/ru', pkg_id='BeletTV001', app_name='Belet', enable_service=True, tv_ip='192.168.1.77')
    with zipfile.ZipFile(io.BytesIO(wgt_hint), 'r') as zf:
        idx_hint_js = re.search(r'<script>\s*(.*?)\s*</script>', zf.read('index.html').decode('utf-8'), re.S).group(1)

    runner = tmp_path / 'test_idx_tiers.js'
    runner.write_text(
        """
const assert = require('assert');
const vm = require('vm');
const idxSrc = """ + json.dumps(idx_js) + """;
const idxHintSrc = """ + json.dumps(idx_hint_js) + """;
const navJs = """ + json.dumps(nav_js) + """;

function makeIndexHarness(opts) {
  opts = opts || {};
  let exitCount = 0;
  let launchedSvc = [];
  let postedToFrame = [];
  let writtenHtml = '';
  let replacedTopUrl = '';
  const winListeners = {};
  const timers = [];

  const frameDoc = { open() {}, write(s) { writtenHtml = s; }, close() {} };
  const els = {
    splash: { style: { display: 'flex' } },
    status: { textContent: '' },
    spinner: { style: { display: 'block' } },
    errBox: { style: { display: 'none' } },
    btnRetry: { className: 'btn is-focus', focus() {}, onclick: null },
    btnDirect: { className: 'btn', focus() {}, onclick: null },
    beletFrame: {
      style: { display: 'none' },
      src: '',
      onload: null,
      contentDocument: frameDoc,
      contentWindow: {
        document: frameDoc,
        location: { href: opts.frameLoc || 'about:blank' },
        focus() {},
        postMessage(m) { postedToFrame.push(m); },
      },
      focus() {},
    },
  };

  const sentXhrs = [];
  const lsBacking = {};
  const ssBacking = {};
  function MockXHR() { this._reqHeaders = {}; this.withCredentials = false; }
  MockXHR.prototype.open = function(m, u) { this._method = m; this._url = u; };
  MockXHR.prototype.setRequestHeader = function(k, v) { this._reqHeaders[k] = v; };
  MockXHR.prototype.getAllResponseHeaders = function() { return 'content-type: text/x-component\\r\\nx-neon-test: ok'; };
  MockXHR.prototype.send = function(body) {
    sentXhrs.push({ url: this._url, method: this._method, withCredentials: this.withCredentials });
    if (this._url.endsWith('/__neon_ping')) {
      const ok = opts.pingOnlyHost ? this._url.indexOf(opts.pingOnlyHost) >= 0 : !!opts.pingOk;
      if (ok) {
        this.status = 200;
        this.responseText = JSON.stringify({ ok: true });
        if (this.onload) this.onload();
      } else {
        this.status = 0;
        if (this.onerror) this.onerror();
      }
    } else if (this._url === 'neon-nav.js') {
      this.status = 200;
      this.responseText = navJs;
      if (this.onload) this.onload();
    } else if (this._url.indexOf('https://belet.tm/api/') === 0) {
      this.status = 200;
      this.responseURL = this._url;
      this.responseText = JSON.stringify({ items: [1, 2, 3], hits: sentXhrs.length });
      if (this.onload) this.onload();
    } else if (this._url.indexOf('https://belet.tm/login') === 0) {
      this.status = 200;
      this.responseURL = this._url;
      this.responseText = JSON.stringify({ method: this._method, body: body || null, hdr: this._reqHeaders['x-test'] || '' });
      if (this.onload) this.onload();
    } else if (this._url.indexOf('https://belet.tm/') === 0) {
      this.status = 200;
      this.responseURL = this._url === 'https://belet.tm/movie/42' ? 'https://belet.tm/movie/42' : 'https://belet.tm/ru';
      this.responseText = '<html><head><base href="https://old.example/"><meta http-equiv="refresh" content="0;url=https://belet.tm/other"><meta http-equiv="Content-Security-Policy" content="x"></head><body><h1>Belet</h1></body></html>';
      if (this.onload) this.onload();
    }
  };

  const sandbox = {
    document: {
      getElementById: (id) => els[id] || null,
      body: { appendChild() {} },
      createElement: () => ({}),
    },
    window: {
      addEventListener: (ev, fn) => { winListeners[ev] = fn; },
      location: { replace(u) { replacedTopUrl = u; }, href: '' },
      close() {},
      localStorage: {
        getItem: (k) => (Object.prototype.hasOwnProperty.call(lsBacking, k) ? lsBacking[k] : null),
        setItem: (k, v) => { lsBacking[k] = String(v); },
      },
      sessionStorage: {
        getItem: (k) => (Object.prototype.hasOwnProperty.call(ssBacking, k) ? ssBacking[k] : null),
        setItem: (k, v) => { ssBacking[k] = String(v); },
      },
    },
    XMLHttpRequest: MockXHR,
    setTimeout(fn, ms) {
      const t = { id: timers.length + 1, fn, ms, cleared: false };
      timers.push(t);
      return t.id;
    },
    clearTimeout(id) {
      const f = timers.find((x) => x.id === id);
      if (f) f.cleared = true;
    },
    webapis: opts.tvIp ? { network: { getIp: () => opts.tvIp } } : undefined,
    tizen: {
      ApplicationControl: function(op) { this.operation = op; },
      tvinputdevice: { registerKey() {} },
      systeminfo: {
        getCapability: (k) => {
          if (k.indexOf('platform.version') >= 0) return '7.0';
          if (k.indexOf('web.service') >= 0 && Object.prototype.hasOwnProperty.call(opts, 'webServiceCap')) return opts.webServiceCap;
          return true;
        },
        getPropertyValue: () => {},
      },
      application: {
        getCurrentApplication: () => ({ exit() { exitCount++; } }),
        launchAppControl: (_c, id, okCb) => { launchedSvc.push(id); if (okCb) okCb(); },
      },
    },
  };
  sandbox.window.window = sandbox.window;
  vm.runInNewContext(opts.src || idxSrc, sandbox);
  return {
    els,
    winListeners,
    timers,
    getExitCount: () => exitCount,
    launchedSvc,
    postedToFrame,
    getWrittenHtml: () => writtenHtml,
    getReplacedTopUrl: () => replacedTopUrl,
    sentXhrs,
    lsBacking,
  };
}

// 1. Tier-1 Proxy mode boots service, pings /__neon_ping, opens iframe preserving /ru path, and Back 450ms timer waits for child reply
const t1 = makeIndexHarness({ pingOk: true });
assert.strictEqual(t1.launchedSvc[0], 'BeletTV001.BeletProxy');
assert.strictEqual(t1.els.beletFrame.src, 'http://127.0.0.1:18765/ru');
t1.els.beletFrame.onload();
assert.strictEqual(t1.els.splash.style.display, 'none');
assert.strictEqual(t1.postedToFrame[0].type, 'neon-ping');
t1.winListeners.message({ data: { type: 'neon-nav-ready' } });

// Key forwarding + Back 1500ms timer canceled by neon-back-handled + opposite-source hw/kd dedup
t1.winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {} });
assert.strictEqual(t1.postedToFrame[1].keyCode, 40);
t1.winListeners.tizenhwkey({ keyName: 'back' });
assert.strictEqual(t1.postedToFrame[2].keyCode, 10009);
const postedBeforeDup = t1.postedToFrame.length;
t1.winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {} }); // immediate Samsung kd duplicate while backTimer pending
assert.strictEqual(t1.postedToFrame.length, postedBeforeDup);
t1.winListeners.message({ data: { type: 'neon-back-handled' } });
assert.strictEqual(t1.timers.find((t) => t.ms === 1500).cleared, true);
assert.strictEqual(t1.getExitCount(), 0);

// neon-back-at-root also cancels safety exit timer so 1st Back at root shows toast without exiting
t1.winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {} });
t1.winListeners.message({ data: { type: 'neon-back-at-root' } });
assert.strictEqual(t1.timers.filter((t) => t.ms === 1500 && !t.cleared).length, 0);
assert.strictEqual(t1.getExitCount(), 0);

// If child does NOT handle Back within 1500ms, app exits cleanly (never trapped!)
t1.winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {} });
t1.timers.filter((t) => t.ms === 1500 && !t.cleared)[0].fn();
assert.strictEqual(t1.getExitCount(), 1);

// If iframe navigates away and stops answering neon-ping within 3500ms, frameNavReady drops to false and Tier-1 recovers back onto proxy origin
t1.els.beletFrame.src = 'https://belet.tm/escaped';
t1.els.beletFrame.onload();
const pingTimer = t1.timers.filter((t) => t.ms === 3500 && !t.cleared).pop();
assert(pingTimer);
pingTimer.fn();
const countBefore = t1.postedToFrame.length;
t1.winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {} });
assert.strictEqual(t1.postedToFrame.length, countBefore);
assert.strictEqual(t1.els.beletFrame.src, 'http://127.0.0.1:18765/ru');

// 1b. If 127.0.0.1 loopback is isolated (Tizen 10), webapis.network.getIp() tvIp fallback connects seamlessly!
const t1Lan = makeIndexHarness({ tvIp: '192.168.1.44', pingOnlyHost: '192.168.1.44' });
assert.strictEqual(t1Lan.els.beletFrame.src, 'http://192.168.1.44:18765/ru');

// 1c. Even when Tizen getCapability('web.service') falsely returns false on retail TVs, still launches proxy & connects Tier-1!
const t1FalseCap = makeIndexHarness({ webServiceCap: false, pingOk: true });
assert.strictEqual(t1FalseCap.launchedSvc[0], 'BeletTV001.BeletProxy');
assert.strictEqual(t1FalseCap.els.beletFrame.src, 'http://127.0.0.1:18765/ru');

// 1d. Install-time HINT_TV_IP allows connecting to LAN IP proxy immediately even when webapis.network is absent!
const t1Hint = makeIndexHarness({ src: idxHintSrc, pingOnlyHost: '192.168.1.77' });
assert.strictEqual(t1Hint.els.beletFrame.src, 'http://192.168.1.77:18765/ru');

// 2. When service ping never responds, exhausts poll (100 x 250ms) and falls back to Tier-2 WebView XHR with <base href> + neon-nav.js
const t2 = makeIndexHarness({ pingOk: false, webServiceCap: false });
for (let i = 0; i < 110; i++) {
  const p = t2.timers.find((x) => !x.cleared && x.ms === 250);
  if (p) { p.cleared = true; p.fn(); }
}
assert.strictEqual(t2.els.beletFrame.src, 'about:blank');
t2.els.beletFrame.onload();
const htmlOut = t2.getWrittenHtml();
assert(htmlOut.includes('<base href="https://belet.tm/ru">'));
assert(htmlOut.includes('window.__NEON_FETCH_MODE__=true'));
assert(htmlOut.includes('window.__NEON_TIER2_DEPTH__=0'));
assert(!htmlOut.includes('Content-Security-Policy'));
assert(!htmlOut.includes('http-equiv="refresh"'));
assert(!htmlOut.includes('https://old.example/'));
// Subsequent onload triggered by doc.close() must NOT re-write document
const prevWritten = t2.getWrittenHtml();
t2.els.beletFrame.onload();
assert.strictEqual(t2.getWrittenHtml(), prevWritten);

// Tier-2 neon-xhr parent bridge runs privileged XMLHttpRequest, caches read-only JSON GET for 20s, and invalidates on POST
const xhrReplies = [];
t2.winListeners.message({
  data: { type: 'neon-xhr', id: 'nx_api1', method: 'GET', url: 'https://belet.tm/api/movies', headers: {} },
  source: { postMessage(m) { xhrReplies.push(m); } },
});
const apiHits1 = t2.sentXhrs.filter((x) => x.url === 'https://belet.tm/api/movies').length;
t2.winListeners.message({
  data: { type: 'neon-xhr', id: 'nx_api2', method: 'GET', url: 'https://belet.tm/api/movies', headers: {} },
  source: { postMessage(m) { xhrReplies.push(m); } },
});
const apiHits2 = t2.sentXhrs.filter((x) => x.url === 'https://belet.tm/api/movies').length;
assert.strictEqual(apiHits1, 1);
assert.strictEqual(apiHits2, 1); // served from 20s cache!
assert.strictEqual(xhrReplies[1].id, 'nx_api2');
assert.strictEqual(xhrReplies[1].body, xhrReplies[0].body);

t2.winListeners.message({
  data: { type: 'neon-xhr', id: 'nx_99', method: 'POST', url: 'https://belet.tm/login?_rsc=1', headers: { 'x-test': 'yes', 'Origin': 'evil' }, body: 'payload' },
  source: { postMessage(m) { xhrReplies.push(m); } },
});
const r99 = xhrReplies.find((r) => r.id === 'nx_99');
assert.strictEqual(r99.type, 'neon-xhr-res');
assert.strictEqual(r99.status, 200);
assert.strictEqual(r99.headers['x-neon-test'], 'ok');
assert.strictEqual(JSON.parse(r99.body).hdr, 'yes');
assert(t2.sentXhrs.some((x) => x.url === 'https://belet.tm/ru' && x.withCredentials === true));
assert(t2.sentXhrs.some((x) => x.url === 'https://belet.tm/login?_rsc=1' && x.withCredentials === true));

// Tier-2 neon-storage persists ls/ss/ck across page navigations, pushes tier2Stack, and neon-back-tier2 pops cleanly back!
t2.winListeners.message({ data: { type: 'neon-storage', kind: 'ls', op: 'set', key: 'token', val: 'jwt_ok' } });
t2.winListeners.message({ data: { type: 'neon-storage', kind: 'ss', op: 'set', key: 'qr_step', val: 'done' } });
t2.winListeners.message({ data: { type: 'neon-storage', kind: 'ck', op: 'set', key: 'sid', val: 's999' } });
assert.strictEqual(JSON.parse(t2.lsBacking.__neon_belet_ls__).token, 'jwt_ok');
t2.winListeners.message({ data: { type: 'neon-navigate', url: 'https://belet.tm/movie/42' } });
t2.els.beletFrame.onload();
const navHtml = t2.getWrittenHtml();
assert(navHtml.includes('window.__NEON_TIER2_DEPTH__=1'));
assert(navHtml.includes('window.__NEON_INIT_LS__={"token":"jwt_ok"}'));
assert(navHtml.includes('window.__NEON_INIT_SS__={"qr_step":"done"}'));
assert(navHtml.includes('window.__NEON_INIT_CK__={"sid":"s999"}'));
t2.winListeners.message({ data: { type: 'neon-back-tier2' } });
t2.els.beletFrame.onload();
const popHtml = t2.getWrittenHtml();
assert(popHtml.includes('<base href="https://belet.tm/ru">'));
assert(popHtml.includes('window.__NEON_TIER2_DEPTH__=0'));

// Tier-2 chrome-error:// navigation inside iframe triggers explicit tier2_blocked_frame diagnostic & error UI (never swallows 3.5s)
const t2Blocked = makeIndexHarness({ pingOk: false, frameLoc: 'chrome-error://chromewebdata/' });
for (let i = 0; i < 110; i++) {
  const p = t2Blocked.timers.find((x) => !x.cleared && x.ms === 250);
  if (p) { p.cleared = true; p.fn(); }
}
t2Blocked.els.beletFrame.onload();
t2Blocked.els.beletFrame.onload();
assert.strictEqual(t2Blocked.els.errBox.style.display, 'block');
assert(t2Blocked.els.status.textContent.includes('tier2_blocked_frame'));
assert(t2Blocked.els.status.textContent.includes('appctl_ok'));

// If Tier-2 iframe ever escapes about:blank to cross-origin, NEVER silently hijack top-level without user consent — show error UI with diagnostic & btnDirect!
Object.defineProperty(t2.els.beletFrame, 'contentDocument', { get() { throw new Error('SecurityError'); } });
t2.els.beletFrame.onload();
assert.strictEqual(t2.getReplacedTopUrl(), '');
assert.strictEqual(t2.els.errBox.style.display, 'block');
assert(t2.els.status.textContent.includes('tier2_cross_origin'));
assert(t2.els.status.textContent.includes('cap_false'));
t2.els.btnDirect.onclick();
assert.strictEqual(t2.getReplacedTopUrl(), '');
""",
        encoding='utf-8',
    )
    node_exe = shutil.which('node') or shutil.which('nodejs')
    chk = subprocess.run([node_exe, str(runner)], capture_output=True, text=True, timeout=15)
    assert chk.returncode == 0, chk.stderr + chk.stdout


def test_belet_nav_js_login_autofocus_qr_react_click_and_tier2_fetch_bridge(tmp_path):
    import io
    import shutil
    import subprocess
    import zipfile
    from samsung_tizen import build_belet_wgt

    wgt_bytes = build_belet_wgt(target_url='https://belet.tm/', pkg_id='BeletTV001', app_name='Belet')
    with zipfile.ZipFile(io.BytesIO(wgt_bytes), 'r') as zf:
        nav_js = zf.read('neon-nav.js').decode('utf-8')

    runner = tmp_path / 'test_login_qr_nav.js'
    runner.write_text(
        """
const assert = require('assert');
const vm = require('vm');
const navSrc = """ + json.dumps(nav_js) + """;

function makeEl(tag, opts) {
  opts = opts || {};
  const attrs = Object.assign({}, opts.attrs || {});
  const el = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    id: opts.id || '',
    type: opts.type || '',
    className: opts.className || '',
    value: opts.value || '',
    selectionStart: Object.prototype.hasOwnProperty.call(opts, 'selectionStart') ? opts.selectionStart : null,
    selectionEnd: Object.prototype.hasOwnProperty.call(opts, 'selectionEnd') ? opts.selectionEnd : null,
    disabled: !!opts.disabled,
    readOnly: !!opts.readOnly,
    tabIndex: typeof opts.tabIndex === 'number' ? opts.tabIndex : -1,
    style: Object.assign({}, opts.style || {}),
    children: [],
    parentElement: null,
    parentNode: null,
    form: opts.form || null,
    _rect: opts.rect || { left: 0, top: 0, width: 100, height: 40, right: 100, bottom: 40 },
    _computed: Object.assign({ display: 'block', visibility: 'visible', opacity: '1', cursor: 'default', borderRadius: '8px', position: 'static', overflowX: 'visible', overflowY: 'visible' }, opts.computed || {}),
    clicks: 0,
    focusedCount: 0,
    blurredCount: 0,
    dispatched: [],
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) || (k === 'id' && !!this.id); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : (k === 'id' ? this.id : null); },
    setAttribute(k, v) { attrs[k] = String(v); if (k === 'id') this.id = String(v); },
    removeAttribute(k) { delete attrs[k]; },
    getBoundingClientRect() { return this._rect; },
    appendChild(child) { child.parentElement = this; child.parentNode = this; this.children.push(child); return child; },
    contains(other) {
      let c = other;
      while (c) { if (c === this) return true; c = c.parentElement; }
      return false;
    },
    click() { this.clicks++; },
    focus() { this.focusedCount++; doc.activeElement = this; },
    blur() { this.blurredCount++; if (doc.activeElement === this) doc.activeElement = doc.body; },
    scrollIntoView() {},
    dispatchEvent(ev) { this.dispatched.push(ev); return true; },
    querySelectorAll(sel) {
      const res = [];
      function walk(node) {
        for (const ch of node.children || []) {
          if (matchesSimple(ch, sel)) res.push(ch);
          walk(ch);
        }
      }
      walk(this);
      return res;
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
  };
  if (opts.reactOnClick) {
    el.__reactProps$test = { onClick: () => { el.clicks++; } };
  }
  if (opts.reactOnMouseUp) {
    el.__reactInternalInstance$test = { memoizedProps: { onMouseUp: () => { el.clicks++; } } };
  }
  return el;
}

function matchesSimple(el, sel) {
  const parts = sel.split(',').map((s) => s.trim());
  for (const p of parts) {
    if (p === 'a[href]' && el.tagName === 'A' && el.hasAttribute('href')) return true;
    if (p === 'a' && el.tagName === 'A') return true;
    if (p === 'p' && el.tagName === 'P') return true;
    if (p === 'span' && el.tagName === 'SPAN') return true;
    if (p === 'b' && el.tagName === 'B') return true;
    if (p === 'strong' && el.tagName === 'STRONG') return true;
    if (p === 'button' && el.tagName === 'BUTTON') return true;
    if (p === 'input:not([type="hidden"])' && el.tagName === 'INPUT' && el.type !== 'hidden') return true;
    if (p === 'div' && el.tagName === 'DIV') return true;
    if (p === '[role="dialog"]' && el.getAttribute('role') === 'dialog') return true;
  }
  return false;
}

const head = makeEl('head');
const body = makeEl('body', { rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
const docEl = makeEl('html', { rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
docEl.appendChild(head);
docEl.appendChild(body);

// Non-modal small toast with role="dialog" (<140x80) must NOT trap activeDialogRoot() away from page buttons
const miniToast = makeEl('div', { id: 'mini-toast', attrs: { role: 'dialog' }, rect: { left: 20, top: 20, width: 100, height: 40, right: 120, bottom: 60 } });
// Model Belet /login screen: phone <input type="number"> (selectionStart === null), "Продолжать" button,
// "Войти через QR-код" <p> with React onClick & NO cursor:pointer, plus a <span> inside an outer container with __reactInternalInstance$ onMouseUp!
const loginForm = makeEl('form', { rect: { left: 660, top: 320, width: 600, height: 142, right: 1260, bottom: 462 } });
const phoneInput = makeEl('input', { id: 'phone', type: 'number', form: loginForm, selectionStart: null, selectionEnd: null, rect: { left: 660, top: 320, width: 600, height: 52, right: 1260, bottom: 372 } });
const continueBtn = makeEl('button', { id: 'continue', type: 'submit', rect: { left: 660, top: 410, width: 600, height: 52, right: 1260, bottom: 462 } });
const qrSwitch = makeEl('p', { id: 'qr-switch', reactOnClick: true, computed: { cursor: 'default' }, rect: { left: 760, top: 510, width: 400, height: 32, right: 1160, bottom: 542 } });
const outerWrap = makeEl('div', { id: 'outer-wrap', computed: { cursor: 'default' }, rect: { left: 660, top: 560, width: 600, height: 100, right: 1260, bottom: 660 } });
const innerMouseUp = makeEl('span', { id: 'inner-mouseup', reactOnMouseUp: true, computed: { cursor: 'default' }, rect: { left: 760, top: 590, width: 400, height: 32, right: 1160, bottom: 622 } });
outerWrap.appendChild(innerMouseUp);

loginForm.appendChild(phoneInput);
loginForm.appendChild(continueBtn);
body.appendChild(miniToast);
body.appendChild(loginForm);
body.appendChild(qrSwitch);
body.appendChild(outerWrap);

const docListeners = {};
const winListeners = {};
const navApiListeners = {};
const timers = [];
const parentMsgs = [];

const doc = {
  readyState: 'complete',
  documentElement: docEl,
  head,
  body,
  activeElement: body,
  createElement(t) { return makeEl(t); },
  createTextNode(s) { return { nodeType: 3, textContent: s }; },
  getElementById(id) {
    let found = null;
    function w(n) {
      if (n.id === id) found = n;
      for (const c of n.children || []) if (!found) w(c);
    }
    w(docEl);
    return found;
  },
  querySelectorAll(s) { return docEl.querySelectorAll(s); },
  querySelector(s) { return docEl.querySelector(s); },
  elementFromPoint() { return body; },
  addEventListener(ev, fn, cap) {
    docListeners[ev] = fn;
    (docListeners[ev + (cap ? ':cap' : ':bub')] = docListeners[ev + (cap ? ':cap' : ':bub')] || []).push(fn);
  },
  dispatchEvent() {},
};

const origFetchCalls = [];
const win = {
  __NEON_FETCH_MODE__: true,
  __NEON_TARGET_URL__: 'https://belet.tm/ru',
  __NEON_INIT_LS__: { pre_tok: 'pt1' },
  __NEON_INIT_SS__: { flow: 'qr' },
  __NEON_INIT_CK__: { pre_ck: 'pc1' },
  innerWidth: 1920,
  innerHeight: 1080,
  scrollY: 0,
  document: doc,
  getComputedStyle(el) { return el._computed || { display: 'block', visibility: 'visible', opacity: '1', cursor: 'default' }; },
  addEventListener(ev, fn) { winListeners[ev] = fn; },
  scrollBy() {},
  parent: { postMessage(msg) { parentMsgs.push(msg); } },
  fetch(u) { origFetchCalls.push(u); return Promise.resolve({ ok: true, status: 200 }); },
  history: { length: 1, pushState() { throw new Error('SecurityError'); }, replaceState() { throw new Error('SecurityError'); }, back() {} },
  navigation: { addEventListener(ev, fn) { navApiListeners[ev] = fn; } },
};
Object.defineProperty(win, 'localStorage', {
  get() { throw new Error('SecurityError'); },
  configurable: true,
});
Object.defineProperty(win, 'sessionStorage', {
  get() { throw new Error('SecurityError'); },
  configurable: true,
});
Object.defineProperty(doc, 'cookie', {
  get() { return ''; },
  set() {},
  configurable: true,
});
function MockBaseXhr() {}
MockBaseXhr.prototype.open = function() {};
MockBaseXhr.prototype.setRequestHeader = function() {};
MockBaseXhr.prototype.send = function() {};
win.XMLHttpRequest = MockBaseXhr;
win.window = win;

const sandbox = {
  window: win,
  document: doc,
  XMLHttpRequest: MockBaseXhr,
  location: { protocol: 'about:', hostname: '', origin: 'null', href: 'about:blank', pathname: 'blank', search: '', assign() {} },
  navigator: { userAgent: 'Tizen' },
  MouseEvent: function(type, opts) { this.type = type; Object.assign(this, opts); },
  PointerEvent: function(type, opts) { this.type = type; Object.assign(this, opts); },
  KeyboardEvent: function(type, opts) { this.type = type; Object.assign(this, opts); },
  PopStateEvent: function(type, opts) { this.type = type; Object.assign(this, opts); },
  Response: function(b, init) { this.body = b; this.status = init.status; this.headers = init.headers; this.ok = init.status >= 200 && init.status < 300; },
  setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
  clearTimeout() {},
  console,
};
vm.runInNewContext(navSrc, sandbox);
// Early boot neon-nav-ready fires synchronously, AND IS_FILE installs bridged localStorage/sessionStorage/cookie from __NEON_INIT_*__
assert(parentMsgs.some((m) => m.type === 'neon-nav-ready'));
assert.strictEqual(win.localStorage.getItem('pre_tok'), 'pt1');
assert.strictEqual(win.sessionStorage.getItem('flow'), 'qr');
win.localStorage.setItem('k1', 'v1');
assert.strictEqual(win.localStorage.getItem('k1'), 'v1');
assert(parentMsgs.some((m) => m.type === 'neon-storage' && m.kind === 'ls' && m.key === 'k1' && m.val === 'v1'));
assert(doc.cookie.includes('pre_ck=pc1'));
doc.cookie = 'sess2=s2val; Path=/; Secure';
assert(doc.cookie.includes('sess2=s2val'));
assert(parentMsgs.some((m) => m.type === 'neon-storage' && m.kind === 'ck' && m.key === 'sess2' && m.val === 's2val'));
// history.pushState / replaceState throwing SecurityError on about:blank is swallowed cleanly
win.history.pushState({}, '', '/ru/account');
timers.shift().fn();

// 1. React autoFocus fires focusin on phoneInput -> neon-nav.js blurs it immediately (prevents surprise IME lock!) & places focus ring on phoneInput
doc.activeElement = phoneInput;
docListeners.focusin({ target: phoneInput });
assert.strictEqual(phoneInput.blurredCount, 1);
assert.strictEqual(doc.activeElement, body);

// 1b. IME_DONE (65376) on /login phone <input type="number"> blurs cleanly WITHOUT prematurely submitting login form!
winListeners.keydown({ keyCode: 13, key: 'Enter', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(doc.activeElement, phoneInput);
winListeners.keydown({ keyCode: 65376, key: '', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(doc.activeElement, body);
assert.strictEqual(continueBtn.clicks, 0);

// 2. Pressing ArrowDown moves cleanly from phoneInput -> continueBtn -> qrSwitch -> innerMouseUp (skipAnc allows React-clickable child inside non-interactive <div>!)
winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
assert.ok(doc.activeElement === continueBtn, 'expected continueBtn, got ' + (doc.activeElement && doc.activeElement.id));
winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
assert.ok(doc.activeElement === qrSwitch, 'expected qrSwitch, got ' + (doc.activeElement && doc.activeElement.id));
winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
assert.ok(doc.activeElement === innerMouseUp, 'expected innerMouseUp, got ' + (doc.activeElement && doc.activeElement.id));

// 3. Pressing OK on qrSwitch dispatches PointerEvent with button:0/pointerType:"mouse"/isPrimary:true and clicks qrSwitch!
winListeners.keydown({ keyCode: 38, key: 'ArrowUp', preventDefault() {}, stopPropagation() {} });
assert.ok(doc.activeElement === qrSwitch, 'expected qrSwitch, got ' + (doc.activeElement && doc.activeElement.id));
winListeners.keydown({ keyCode: 13, key: 'Enter', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(qrSwitch.clicks, 1);
const pe = qrSwitch.dispatched.find((d) => d.type === 'pointerdown');
assert(pe && pe.button === 0 && pe.pointerType === 'mouse' && pe.isPrimary === true);

// 4. Even if user explicitly opened IME on phoneInput (type="number", selectionStart === null), ArrowLeft escapes straight to navigation!
winListeners.keydown({ keyCode: 38, key: 'ArrowUp', preventDefault() {}, stopPropagation() {} });
winListeners.keydown({ keyCode: 38, key: 'ArrowUp', preventDefault() {}, stopPropagation() {} });
winListeners.keydown({ keyCode: 13, key: 'Enter', preventDefault() {}, stopPropagation() {} });
assert.ok(doc.activeElement === phoneInput, 'expected phoneInput, got ' + (doc.activeElement && doc.activeElement.id));
winListeners.keydown({ keyCode: 37, key: 'ArrowLeft', preventDefault() {}, stopPropagation() {} });
assert.ok(doc.activeElement === body, 'expected body after ArrowLeft blur, got ' + (doc.activeElement && doc.activeElement.id));

// 4b. Real modal <div role="dialog"> (>=140x80) DOES isolate focus inside the modal!
const bigModal = makeEl('div', { id: 'big-modal', attrs: { role: 'dialog' }, rect: { left: 460, top: 240, width: 1000, height: 600, right: 1460, bottom: 840 } });
const modalCloseBtn = makeEl('button', { id: 'modal-close', attrs: { 'aria-label': 'Close' }, rect: { left: 1380, top: 260, width: 50, height: 50, right: 1430, bottom: 310 } });
bigModal.appendChild(modalCloseBtn);
body.appendChild(bigModal);
winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
assert.ok(doc.activeElement === modalCloseBtn, 'expected modalCloseBtn inside real modal, got ' + (doc.activeElement && doc.activeElement.id));
bigModal._computed.display = 'none';
winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
assert.ok(doc.activeElement !== modalCloseBtn);

// 4c. Tier-2 Navigation API intercept prevents hard cross-origin iframe redirect and forwards neon-navigate to parent
let navEvPrevented = false;
assert(typeof navApiListeners.navigate === 'function');
navApiListeners.navigate({
  cancelable: true,
  destination: { url: 'https://belet.tm/ru/login' },
  preventDefault() { navEvPrevented = true; },
});
assert.strictEqual(navEvPrevented, true);
assert(parentMsgs.some((m) => m.type === 'neon-navigate' && m.url === 'https://belet.tm/ru/login'));

// 5. Info (457) or Yellow (405) also toggles virtual cursor for minimalist Samsung remotes lacking Blue key
winListeners.keydown({ keyCode: 457, key: '', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(doc.getElementById('neon-tv-cursor').style.display, 'block');
winListeners.keydown({ keyCode: 405, key: '', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(doc.getElementById('neon-tv-cursor').style.display, 'none');

// 6. Tier-2 IS_FILE fetch & XMLHttpRequest bridge post neon-xhr to parent and resolve on neon-xhr-res with url/redirected
(async () => {
  const fp = win.fetch('/login?_rsc=abc');
  const xhrMsg = parentMsgs.find((m) => m.type === 'neon-xhr');
  assert(xhrMsg && xhrMsg.url === 'https://belet.tm/login?_rsc=abc');
  winListeners.message({ data: { type: 'neon-xhr-res', id: xhrMsg.id, status: 200, responseURL: 'https://belet.tm/ru/home', headers: { 'content-type': 'text/x-component' }, body: 'RSC_OK' } });
  const resolvedRes = await fp;
  assert(resolvedRes && resolvedRes.status === 200 && resolvedRes.body === 'RSC_OK');
  assert.strictEqual(resolvedRes.url, 'https://belet.tm/ru/home');
  assert.strictEqual(resolvedRes.redirected, true);

  let xhrDone = false;
  const x = new sandbox.XMLHttpRequest();
  x.open('POST', 'https://belet.tm/api/qr-check', true);
  x.setRequestHeader('X-Test', '1');
  x.onload = () => { xhrDone = true; };
  x.send('{"qr":1}');
  const xhrMsg2 = parentMsgs.slice().reverse().find((m) => m.type === 'neon-xhr' && m.url === 'https://belet.tm/api/qr-check');
  assert(xhrMsg2);
  winListeners.message({ data: { type: 'neon-xhr-res', id: xhrMsg2.id, status: 200, responseURL: 'https://belet.tm/api/qr-check', headers: { 'content-type': 'application/json' }, body: '{"ok":true}' } });
  assert.strictEqual(xhrDone, true);
  assert.strictEqual(x.responseText, '{"ok":true}');

  // 7. IS_PROXY: capture-phase link rewrite + document.cookie patch + WebSocket rewrite + window.navigation intercept
  const pCapListeners = [];
  const pNavListeners = {};
  const nativeCookies = {};
  function MockDocument() {}
  Object.defineProperty(MockDocument.prototype, 'cookie', {
    configurable: true,
    get() {
      return Object.keys(nativeCookies).map((k) => k + '=' + nativeCookies[k]).join('; ');
    },
    set(v) {
      const s = String(v || '');
      if (/;\\s*(?:domain=|secure\\b)/i.test(s)) return; // simulates Chromium dropping domain=.belet.tm / Secure on http://127.0.0.1
      const first = s.split(';')[0] || '';
      const eq = first.indexOf('=');
      if (eq <= 0) return;
      const ck = first.slice(0, eq).trim();
      const cv = first.slice(eq + 1).trim();
      if (/;\\s*max-age\\s*=\\s*-?0+/i.test(s)) { delete nativeCookies[ck]; return; }
      if (/^__(?:Host|Secure)-/i.test(ck)) return; // Chromium rejects __Host-/__Secure- over plain http://
      nativeCookies[ck] = cv;
    },
  });
  const pDoc = Object.assign(new MockDocument(), {
    readyState: 'complete',
    documentElement: docEl,
    head,
    body,
    activeElement: body,
    createElement: (t) => makeEl(t),
    createTextNode: (s) => ({ nodeType: 3, textContent: s }),
    getElementById: () => null,
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener(ev, fn, cap) { if (ev === 'click' && cap) pCapListeners.push(fn); },
  });
  const wsCalls = [];
  const beacons = [];
  const assignedLocs = [];
  function MockWS(u) { wsCalls.push(u); this.url = u; }
  MockWS.CONNECTING = 0; MockWS.OPEN = 1; MockWS.CLOSING = 2; MockWS.CLOSED = 3;
  const pWin = {
    innerWidth: 1920,
    innerHeight: 1080,
    document: pDoc,
    WebSocket: MockWS,
    navigation: { addEventListener(ev, fn) { pNavListeners[ev] = fn; } },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1', cursor: 'default' }),
    addEventListener() {},
    parent: { postMessage() {} },
    history: { length: 1, pushState() {}, replaceState() {}, back() {} },
  };
  pWin.window = pWin;
  vm.runInNewContext(navSrc, {
    window: pWin,
    document: pDoc,
    Document: MockDocument,
    WebSocket: MockWS,
    location: {
      protocol: 'http:',
      hostname: '127.0.0.1',
      host: '127.0.0.1:18765',
      origin: 'http://127.0.0.1:18765',
      pathname: '/ru',
      search: '',
      assign(u) { assignedLocs.push(u); },
      replace(u) { assignedLocs.push(u); },
    },
    navigator: {
      userAgent: 'Tizen',
      sendBeacon(u, d) { beacons.push({ u, d }); return true; },
    },
    setTimeout() { return 1; },
    clearTimeout() {},
    console,
  });
  const reactLink = makeEl('a', { attrs: { href: 'https://belet.tm/ru/movie/42', target: '_blank' } });
  pCapListeners.forEach((fn) => fn({ target: reactLink, preventDefault() {} }));
  assert.strictEqual(reactLink.getAttribute('href'), 'http://127.0.0.1:18765/ru/movie/42');
  assert.strictEqual(reactLink.getAttribute('target'), null);

  // __Host- cookie set via JS is mirrored into getter & synced to /__neon_cookie, normal cookie has domain/secure stripped
  pDoc.cookie = '__Host-auth=tk9; domain=.belet.tm; secure; path=/; SameSite=None';
  pDoc.cookie = 'locale=ru; domain=.belet.tm; secure; path=/';
  assert(pDoc.cookie.includes('__Host-auth=tk9'));
  assert(pDoc.cookie.includes('locale=ru'));
  assert(beacons.some((b) => b.u === '/__neon_cookie' && String(b.d).includes('__Host-auth=tk9')));
  pDoc.cookie = '__Host-auth=; max-age=0; path=/';
  assert(!pDoc.cookie.includes('__Host-auth=tk9'));

  // WebSocket & window.navigation stay pinned on proxy origin
  const ws = new pWin.WebSocket('wss://api.belet.tm/qr');
  assert.strictEqual(ws.url, 'ws://127.0.0.1:18765/__neon_host/api.belet.tm/qr');
  let pNavPrevented = false;
  pNavListeners.navigate({
    cancelable: true,
    navigationType: 'push',
    destination: { url: 'https://belet.tm/ru/home' },
    preventDefault() { pNavPrevented = true; },
  });
  assert.strictEqual(pNavPrevented, true);
  assert.strictEqual(assignedLocs[0], 'http://127.0.0.1:18765/ru/home');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
""",
        encoding='utf-8',
    )
    node_exe = shutil.which('node') or shutil.which('nodejs')
    chk = subprocess.run([node_exe, str(runner)], capture_output=True, text=True, timeout=15)
    assert chk.returncode == 0, chk.stderr + chk.stdout



@pytest.mark.parametrize("reject_service", [False, True])
def test_install_samsung_belet_uses_fullscreen_wgt(tmp_api, tmp_path, monkeypatch, reject_service):
    import datetime
    import io
    import zipfile
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    a_key = rsa.generate_private_key(65537, 2048)
    d_key = rsa.generate_private_key(65537, 2048)
    a_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Author')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Author')]))
        .public_key(a_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(a_key, hashes.SHA256())
    )
    d_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Dist')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Dist')]))
        .public_key(d_key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(d_key, hashes.SHA256())
    )

    pushed_wgts = []
    install_attempts = []

    class FakeSdb:
        def __init__(self, host, port=26101, timeout=8.0):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def capability(self):
            return {'platform_version': '5.5', 'duid': 'DUIDFALLBACK01'}

        def get_duid(self, caps=None):
            return 'DUIDFALLBACK01'

        def resolve_sdk_toolpath(self, caps=None):
            return '/home/owner/share/tmp/sdk_tools/tmp'

        def push_bytes(self, data, remote):
            pushed_wgts.append(data)

        def install_wgt(self, pkg_id, remote, timeout=120.0):
            install_attempts.append(pkg_id)
            if reject_service and len(install_attempts) == 1:
                return 'install failed [118, -19]'
            return 'installing[100]\ninstall completed\n'

        def list_apps(self):
            return 'BeletTV001.Belet\tBelet\n' if install_attempts else ''

        def launch_app(self, app_id):
            return 'launched'

    monkeypatch.setattr('core.SdbClient', FakeSdb)
    monkeypatch.setattr(
        tmp_api._samsung_cert_manager().__class__,
        'find_saved_samsung_certs',
        lambda _self, duid: {
            'source': str(tmp_path),
            'author_key': a_key,
            'author_certs': [a_leaf],
            'dist_key': d_key,
            'dist_certs': [d_leaf],
        },
    )

    res = tmp_api.install_samsung_belet('192.168.1.55')
    assert res['ok'] is True
    assert len(pushed_wgts) == (2 if reject_service else 1)
    assert len(install_attempts) == len(pushed_wgts)
    with zipfile.ZipFile(io.BytesIO(pushed_wgts[0]), 'r') as first:
        assert 'service/proxy.js' in first.namelist()
    with zipfile.ZipFile(io.BytesIO(pushed_wgts[-1]), 'r') as z1:
        assert ('service/proxy.js' in z1.namelist()) is not reject_service
        html = z1.read('index.html').decode('utf-8')
        assert 'https://belet.tm/' in html
        assert '<iframe id="beletFrame"' in html
        assert 'btnBrowser' not in html


def test_belet_nav_js_player_seek_and_controls(tmp_path):
    import io
    import shutil
    import subprocess
    import zipfile
    from samsung_tizen import build_belet_wgt

    wgt_bytes = build_belet_wgt(target_url='https://belet.tm/watch/123', pkg_id='BeletTV001', app_name='Belet')
    with zipfile.ZipFile(io.BytesIO(wgt_bytes), 'r') as zf:
        nav_js = zf.read('neon-nav.js').decode('utf-8')

    runner = tmp_path / 'test_player_seek.js'
    runner.write_text(
        """
const assert = require('assert');
const vm = require('vm');
const navSrc = """ + json.dumps(nav_js) + """;

function makeEl(tag, opts) {
  opts = opts || {};
  const attrs = Object.assign({}, opts.attrs || {});
  const el = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    id: opts.id || '',
    type: opts.type || '',
    disabled: false,
    readOnly: false,
    tabIndex: attrs.tabindex !== undefined ? parseInt(attrs.tabindex, 10) : -1,
    style: {},
    className: '',
    textContent: opts.textContent || '',
    children: [],
    parentElement: null,
    parentNode: null,
    _rect: opts.rect || { left: 0, top: 0, width: 100, height: 30, right: 100, bottom: 30 },
    _computed: Object.assign({ display: 'block', visibility: 'visible', opacity: '1', cursor: 'default', overflowX: 'visible', overflowY: 'visible', borderRadius: '8px', position: 'static' }, opts.computed || {}),
    dispatched: [],
    clicks: 0,
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : (k === 'id' ? this.id : null); },
    setAttribute(k, v) { attrs[k] = String(v); },
    removeAttribute(k) { delete attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) || (k === 'id' && !!this.id); },
    getBoundingClientRect() { return this._rect; },
    appendChild(c) { c.parentElement = this; c.parentNode = this; this.children.push(c); return c; },
    contains(t) {
      let cur = t;
      while (cur) { if (cur === this) return true; cur = cur.parentElement; }
      return false;
    },
    focus() { doc.activeElement = this; },
    blur() { if (doc.activeElement === this) doc.activeElement = body; },
    click() { this.clicks++; if (opts.onClick) opts.onClick(); },
    dispatchEvent(ev) { this.dispatched.push(ev.type); return true; },
    scrollIntoView() {},
    querySelectorAll(sel) {
      const res = [];
      function walk(n) {
        for (const c of n.children || []) {
          if (matches(c, sel)) res.push(c);
          walk(c);
        }
      }
      walk(this);
      return res;
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
  };
  if (opts.reactOnClick) el.__reactProps$t = { onClick() { el.clicks++; } };
  return el;
}

function matches(el, sel) {
  for (const p of sel.split(',').map((s) => s.trim())) {
    if (p === 'video' && el.tagName === 'VIDEO') return true;
    if (p === 'video,audio' && (el.tagName === 'VIDEO' || el.tagName === 'AUDIO')) return true;
    if (p === 'iframe' && el.tagName === 'IFRAME') return true;
    if (p === 'a[href]' && el.tagName === 'A' && el.hasAttribute('href')) return true;
    if (p === 'a' && el.tagName === 'A') return true;
    if (p === 'button' && el.tagName === 'BUTTON') return true;
    if (p === 'input:not([type="hidden"])' && el.tagName === 'INPUT' && el.type !== 'hidden') return true;
    if (p === '[role="slider"]' && el.getAttribute('role') === 'slider') return true;
    if (p === '[role="button"]' && el.getAttribute('role') === 'button') return true;
    if (p === '[role="dialog"]' && el.getAttribute('role') === 'dialog') return true;
    if (p === 'div' && el.tagName === 'DIV') return true;
  }
  return false;
}

let doc, body;
function bootHarness(pathname, bootOpts) {
  bootOpts = bootOpts || {};
  const head = makeEl('head');
  body = makeEl('body', { rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
  const docEl = makeEl('html', { rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
  docEl.appendChild(head);
  docEl.appendChild(body);
  const winListeners = {};
  const timers = [];
  const parentMsgs = [];
  let historyBacks = 0;
  let fakeNow = 50000;
  doc = {
    readyState: 'complete',
    documentElement: docEl,
    head,
    body,
    activeElement: body,
    createElement: (t) => makeEl(t),
    createTextNode: (s) => ({ nodeType: 3, textContent: s }),
    getElementById(id) {
      let f = null;
      (function w(n) { if (n.id === id) f = n; for (const c of n.children || []) if (!f) w(c); })(docEl);
      return f;
    },
    querySelectorAll: (s) => docEl.querySelectorAll(s),
    querySelector: (s) => docEl.querySelector(s),
    elementFromPoint: () => body,
    addEventListener() {},
  };
  const win = {
    innerWidth: 1920,
    innerHeight: 1080,
    scrollY: 0,
    document: doc,
    getComputedStyle: (el) => {
      if (el && el.getAttribute && el.getAttribute('data-neon-wake-bar') === '1') {
        return Object.assign({}, el._computed, { opacity: '1', visibility: 'visible' });
      }
      return el._computed;
    },
    addEventListener: (ev, fn) => { winListeners[ev] = fn; },
    parent: { postMessage(m) { parentMsgs.push(m); } },
    history: {
      length: typeof bootOpts.historyLength === 'number' ? bootOpts.historyLength : 1,
      pushState() {},
      replaceState() {},
      back() { historyBacks++; },
    },
  };
  win.window = win;
  const loc = { protocol: 'http:', hostname: '127.0.0.1', origin: 'http://127.0.0.1:18765', pathname: pathname || '/watch/123', search: '', assignCalls: [], assign(u) { this.assignCalls.push(u); } };
  vm.runInNewContext(navSrc, {
    window: win,
    document: doc,
    Date: { now: () => fakeNow },
    location: loc,
    navigator: { userAgent: 'Tizen' },
    MouseEvent: function(type, o) { this.type = type; Object.assign(this, o || {}); },
    PointerEvent: function(type, o) { this.type = type; Object.assign(this, o || {}); },
    Event: function(type, o) { this.type = type; Object.assign(this, o || {}); },
    setTimeout(fn, ms) { const t = { id: timers.length + 1, fn, ms, cleared: false }; timers.push(t); return t.id; },
    clearTimeout(id) { const f = timers.find((x) => x.id === id); if (f) f.cleared = true; },
    console,
  });
  return {
    doc,
    body,
    winListeners,
    timers,
    parentMsgs,
    loc,
    getHistoryBacks: () => historyBacks,
    advanceNow: (ms) => { fakeNow += ms; },
  };
}

// 1. Active movie player: ArrowRight / ArrowLeft seek +-10s (even with rapid HLS buffering lag & when paused!), OK toggles pause, ArrowDown enters control bar
const h1 = bootHarness('/watch/123');
const playerWrap = makeEl('div', { id: 'player-wrap', rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
let realCurTime = 120;
let lagMode = false;
const vid = makeEl('video', { id: 'main-vid', rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
vid.paused = false;
vid.muted = false;
vid.loop = false;
vid.duration = 3600;
Object.defineProperty(vid, 'currentTime', {
  get() { return lagMode ? 130 : realCurTime; }, // simulates decoder lag on rapid key taps
  set(v) { realCurTime = v; },
});
vid.play = function() { this.paused = false; };
vid.pause = function() { this.paused = true; };

const pauseOverlay = makeEl('button', {
  id: 'pause-overlay',
  rect: { left: 0, top: 80, width: 1920, height: 820, right: 1920, bottom: 900 },
  onClick() { vid.paused = !vid.paused; },
});
const controlsBar = makeEl('div', { id: 'controls-bar', rect: { left: 0, top: 920, width: 1920, height: 140, right: 1920, bottom: 1060 } });
const seekSlider = makeEl('input', { id: 'seek-slider', type: 'range', rect: { left: 60, top: 930, width: 1800, height: 24, right: 1860, bottom: 954 } });
const btnSubtitles = makeEl('button', { id: 'btn-sub', rect: { left: 880, top: 980, width: 140, height: 48, right: 1020, bottom: 1028 } });
const btnQuality = makeEl('button', { id: 'btn-qual', rect: { left: 1120, top: 980, width: 140, height: 48, right: 1260, bottom: 1028 } });
controlsBar.appendChild(seekSlider);
controlsBar.appendChild(btnSubtitles);
controlsBar.appendChild(btnQuality);
playerWrap.appendChild(vid);
playerWrap.appendChild(pauseOverlay);
playerWrap.appendChild(controlsBar);
h1.body.appendChild(playerWrap);
h1.timers.shift().fn(); // initial boot focus lands on pauseOverlay

// First ArrowRight seeks +10s (120 -> 130) & wakes player controls via mousemove
h1.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(realCurTime, 130);
assert(vid.dispatched.includes('mousemove'));
const toast = h1.doc.getElementById('neon-tv-toast');
assert(toast && toast.textContent.includes('+10') && toast.textContent.includes('2:10'));

// Rapid ArrowRight presses keep advancing +10s each even if decoder still reports 130
lagMode = true;
h1.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(realCurTime, 140);
h1.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(realCurTime, 150);
assert(toast.textContent.includes('+30'));
lagMode = false;

// ArrowLeft seeks -10s (150 -> 140)
h1.winListeners.keydown({ keyCode: 37, key: 'ArrowLeft', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(realCurTime, 140);
assert(toast.textContent.includes('-10'));

// OK pauses via pauseOverlay, and seeking STILL works while paused!
h1.winListeners.keydown({ keyCode: 13, key: 'Enter', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(vid.paused, true);
h1.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(realCurTime, 150);

// ArrowDown enters player control bar; ArrowLeft/Right then navigate between control buttons (Subtitles <-> Quality)
h1.winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
// Focus moves to controls below pauseOverlay (either seekSlider or btnSubtitles depending on geometry); step down to button row if on slider
if (h1.doc.activeElement === seekSlider) {
  // On slider itself, ArrowRight seeks rather than leaving slider!
  h1.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
  assert.strictEqual(realCurTime, 160);
  assert.ok(h1.doc.activeElement === seekSlider);
  h1.winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
}
assert.ok(h1.doc.activeElement === btnSubtitles, 'expected btnSubtitles, got ' + (h1.doc.activeElement && h1.doc.activeElement.id));
h1.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.ok(h1.doc.activeElement === btnQuality, 'expected btnQuality, got ' + (h1.doc.activeElement && h1.doc.activeElement.id));

// Pressing ArrowUp twice back to full-player pauseOverlay and pressing ArrowRight immediately resumes seeking!
const beforeUpSeek = realCurTime;
h1.winListeners.keydown({ keyCode: 38, key: 'ArrowUp', preventDefault() {}, stopPropagation() {} });
if (h1.doc.activeElement === seekSlider) {
  h1.winListeners.keydown({ keyCode: 38, key: 'ArrowUp', preventDefault() {}, stopPropagation() {} });
}
assert.ok(h1.doc.activeElement === pauseOverlay, 'expected pauseOverlay, got ' + (h1.doc.activeElement && h1.doc.activeElement.id));
h1.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(realCurTime, beforeUpSeek + 10);

// If control bar auto-hides after descending, next ArrowLeft automatically returns to seeking!
h1.winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
controlsBar._computed.opacity = '0';
vid.paused = false;
h1.winListeners.keydown({ keyCode: 37, key: 'ArrowLeft', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(realCurTime, beforeUpSeek);

// 2. Looped muted hero preview banner on homepage MUST NOT hijack ArrowLeft/Right catalog card navigation!
const h2 = bootHarness('/ru');
const heroVid = makeEl('video', { id: 'hero-preview', rect: { left: 0, top: 0, width: 1280, height: 480, right: 1280, bottom: 480 } });
heroVid.muted = true;
heroVid.loop = true;
heroVid.currentTime = 5;
heroVid.duration = 25;
const cardA = makeEl('button', { id: 'card-a', rect: { left: 40, top: 540, width: 220, height: 300, right: 260, bottom: 840 } });
const cardB = makeEl('button', { id: 'card-b', rect: { left: 290, top: 540, width: 220, height: 300, right: 510, bottom: 840 } });
h2.body.appendChild(heroVid);
h2.body.appendChild(cardA);
h2.body.appendChild(cardB);
h2.timers.shift().fn();
assert.ok(h2.doc.activeElement === cardA);
h2.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.ok(h2.doc.activeElement === cardB);
assert.strictEqual(heroVid.currentTime, 5);

// 3. Same-origin nested <iframe> containing <video> can also be seeked via D-pad and MediaRewind (412)
const h3 = bootHarness('/watch/embed');
const innerVid = makeEl('video', { id: 'inner-vid', rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
innerVid.currentTime = 60;
innerVid.duration = 1800;
innerVid.muted = false;
const innerDoc = { querySelectorAll: (s) => (s === 'video' || s === 'video,audio') ? [innerVid] : [] };
const innerFrame = makeEl('iframe', { id: 'player-iframe', rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
innerFrame.contentDocument = innerDoc;
h3.body.appendChild(innerFrame);
h3.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(innerVid.currentTime, 70);
h3.winListeners.keydown({ keyCode: 412, key: 'MediaRewind', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(innerVid.currentTime, 60);

// 4. CSS-:hover hidden bottom controls (opacity: '0' + React onClick controls-bar wrapper) wake on ArrowDown,
//    never seek +-10s even after >10s browsing buttons or stepping volume slider, and Return never exits app!
const h4 = bootHarness('/watch/999', { historyLength: 2 });
const vid4 = makeEl('video', { id: 'vid4', rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
vid4.paused = false;
vid4.muted = false;
vid4.duration = 2400;
vid4.currentTime = 300;
vid4.play = function() { this.paused = false; };
vid4.pause = function() { this.paused = true; };

const hoverBar = makeEl('div', {
  id: 'hover-bar',
  reactOnClick: true,
  computed: { opacity: '0', cursor: 'pointer' },
  rect: { left: 0, top: 920, width: 1920, height: 160, right: 1920, bottom: 1080 },
});
const volSlider = makeEl('input', {
  id: 'vol-slider',
  type: 'range',
  attrs: { min: '0', max: '1', step: '0.1', 'aria-label': 'Volume' },
  rect: { left: 320, top: 980, width: 140, height: 28, right: 460, bottom: 1008 },
});
volSlider.min = '0';
volSlider.max = '1';
volSlider.step = '0.1';
volSlider.value = '0.5';
const btnAudio = makeEl('button', { id: 'btn-audio', rect: { left: 680, top: 980, width: 140, height: 48, right: 820, bottom: 1028 } });
const btnQual4 = makeEl('button', { id: 'btn-qual4', rect: { left: 880, top: 980, width: 140, height: 48, right: 1020, bottom: 1028 } });
hoverBar.appendChild(volSlider);
hoverBar.appendChild(btnAudio);
hoverBar.appendChild(btnQual4);
h4.body.appendChild(vid4);
h4.body.appendChild(hoverBar);
h4.timers.shift().fn(); // boot

// Press ArrowDown -> hoverBar starts at opacity:0 -> deferred 80ms/200ms timer calls forceRevealPlayerBar -> button focused (never wide hoverBar!)
h4.winListeners.keydown({ keyCode: 40, key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
for (const tm of h4.timers.filter((x) => !x.cleared && (x.ms === 80 || x.ms === 200))) {
  tm.fn();
}
assert.strictEqual(hoverBar.getAttribute('data-neon-wake-bar'), '1');
assert.ok(h4.doc.activeElement === btnAudio || h4.doc.activeElement === btnQual4 || h4.doc.activeElement === volSlider, 'got ' + (h4.doc.activeElement && h4.doc.activeElement.id));
assert.notStrictEqual(h4.doc.activeElement, hoverBar);

// Even 12 seconds later on buttons, ArrowLeft / ArrowRight navigate between buttons/sliders and MUST NOT seek video!
h4.advanceNow(12000);
assert.strictEqual(h4.doc.activeElement, btnQual4);
h4.winListeners.keydown({ keyCode: 37, key: 'ArrowLeft', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(h4.doc.activeElement, btnAudio);
assert.strictEqual(vid4.currentTime, 300);
h4.winListeners.keydown({ keyCode: 37, key: 'ArrowLeft', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(h4.doc.activeElement, volSlider);
assert.strictEqual(vid4.currentTime, 300);
// On volume slider, ArrowRight adjusts volume and MUST NOT seek video!
h4.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(vid4.currentTime, 300);
assert.notStrictEqual(volSlider.value, '0.5');

// 1st Return inside player with wake bar active closes bar cleanly (never exits app!)
h4.winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(hoverBar.getAttribute('data-neon-wake-bar'), null);
assert(!h4.parentMsgs.some((m) => m.type === 'neon-exit'));
assert(h4.parentMsgs.some((m) => m.type === 'neon-back-handled'));

// 2nd Return now inside active player calls history.back() instead of exiting app!
h4.winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(h4.getHistoryBacks(), 1);
assert(!h4.parentMsgs.some((m) => m.type === 'neon-exit'));

// 5. Even with history.length === 1 and NO spaDepth, 1st Return in active video clicks player back button instead of exiting app!
const h5 = bootHarness('/watch/555', { historyLength: 1 });
const vid5 = makeEl('video', { id: 'vid5', rect: { left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 } });
vid5.muted = false;
vid5.duration = 1200;
vid5.currentTime = 100;
const backBtn5 = makeEl('button', { id: 'player-back', attrs: { 'aria-label': 'Back to movie' }, rect: { left: 32, top: 32, width: 64, height: 64, right: 96, bottom: 96 } });
h5.body.appendChild(vid5);
h5.body.appendChild(backBtn5);
h5.timers.shift().fn();
// Even if boot autofocus sits on corner back button, ArrowRight still seeks +10s until ArrowDown opens controls!
h5.winListeners.keydown({ keyCode: 39, key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(vid5.currentTime, 110);
h5.winListeners.keydown({ keyCode: 10009, key: 'Back', preventDefault() {}, stopPropagation() {} });
assert.strictEqual(backBtn5.clicks, 1);
assert(!h5.parentMsgs.some((m) => m.type === 'neon-exit'));
""",
        encoding='utf-8',
    )
    node_exe = shutil.which('node') or shutil.which('nodejs')
    chk = subprocess.run([node_exe, str(runner)], capture_output=True, text=True, timeout=15)
    assert chk.returncode == 0, chk.stderr + chk.stdout


def test_belet_proxy_caching_and_keepalive(tmp_path):
    import io
    import shutil
    import subprocess
    import zipfile
    from samsung_tizen import build_belet_wgt

    wgt_bytes = build_belet_wgt(target_url='https://belet.tm/', pkg_id='BeletTV001', app_name='Belet')
    with zipfile.ZipFile(io.BytesIO(wgt_bytes), 'r') as zf:
        proxy_js = zf.read('service/proxy.js').decode('utf-8')

    runner = tmp_path / 'test_proxy_cache.js'
    runner.write_text(
        """
const assert = require('assert');
const http = require('http');
const vm = require('vm');
const src = """ + json.dumps(proxy_js) + """;

let staticHits = 0;
let parallelHits = 0;
let dataHits = 0;
let authHits = 0;
let hlsHits = 0;
let rangeHits = 0;

const upstream = http.createServer((req, res) => {
  if (req.url === '/_next/static/chunks/app-123.js') {
    staticHits++;
    res.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'no-cache' });
    res.end('console.log("chunk-' + staticHits + '");');
    return;
  }
  if (req.url === '/_next/static/chunks/parallel.js') {
    parallelHits++;
    setTimeout(() => {
      res.writeHead(200, { 'Content-Type': 'application/javascript' });
      res.end('console.log("parallel-' + parallelHits + '");');
    }, 30);
    return;
  }
  if (req.url === '/api/catalog') {
    dataHits++;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ rev: dataHits }));
    return;
  }
  if (req.url === '/api/auth/me') {
    authHits++;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ user: authHits }));
    return;
  }
  if (req.url === '/api/watch-ping' && req.method === 'POST') {
    res.writeHead(200, { 'Content-Type': 'application/json', 'Set-Cookie': 'sid=new_' + Date.now() + '; Path=/' });
    res.end(JSON.stringify({ ok: true }));
    return;
  }
  if (req.url === '/live/stream.m3u8') {
    hlsHits++;
    res.writeHead(200, { 'Content-Type': 'application/vnd.apple.mpegurl' });
    res.end('#EXTM3U\\n#EXT-X-VERSION:3\\n' + hlsHits);
    return;
  }
  if (req.url === '/video/movie.mp4') {
    rangeHits++;
    res.writeHead(206, { 'Content-Type': 'video/mp4', 'Content-Range': 'bytes 0-3/100' });
    res.end('ABCD');
    return;
  }
  res.writeHead(404);
  res.end('nope');
});

function httpReq(port, method, path, headers) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port, path, method, headers: headers || {} }, (res) => {
      const ch = [];
      res.on('data', (d) => ch.push(d));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(ch).toString('utf8') }));
    });
    req.on('error', reject);
    req.end();
  });
}

upstream.listen(0, '127.0.0.1', async () => {
  const upPort = upstream.address().port;
  const patched = src
    .replace(/var TARGET_URL=[^;]+;/, "var TARGET_URL='http://127.0.0.1:" + upPort + "/';")
    .replace(/var PORT=\\d+;/, 'var PORT=0;');
  const mod = { exports: {} };
  vm.runInNewContext(patched, {
    require,
    module: mod,
    exports: mod.exports,
    Buffer,
    console,
    process,
    setTimeout,
    clearTimeout,
  });
  const srv = mod.exports.server;
  await new Promise((r) => (srv.listening ? r() : srv.once('listening', r)));
  const proxyPort = srv.address().port;

  try {
    // 0. /__neon_ping reports 1.2.7
    const ping = await httpReq(proxyPort, 'GET', '/__neon_ping');
    assert.strictEqual(JSON.parse(ping.body).ver, '1.2.7');

    // 1. Static asset: 1st MISS sets immutable Cache-Control & ETag; 2nd HIT avoids upstream; If-None-Match -> 304!
    const s1 = await httpReq(proxyPort, 'GET', '/_next/static/chunks/app-123.js');
    assert.strictEqual(s1.status, 200);
    assert.strictEqual(staticHits, 1);
    assert.strictEqual(s1.headers['x-neon-cache'], 'MISS');
    assert.strictEqual(s1.headers['cache-control'], 'public, max-age=604800, immutable');
    assert(s1.headers['etag']);

    const s2 = await httpReq(proxyPort, 'GET', '/_next/static/chunks/app-123.js');
    assert.strictEqual(s2.status, 200);
    assert.strictEqual(staticHits, 1);
    assert.strictEqual(s2.headers['x-neon-cache'], 'HIT');
    assert.strictEqual(s2.body, s1.body);

    const s3 = await httpReq(proxyPort, 'GET', '/_next/static/chunks/app-123.js', { 'If-None-Match': s1.headers['etag'] });
    assert.strictEqual(s3.status, 304);
    assert.strictEqual(staticHits, 1);

    // 2. Concurrent inflight GET coalescing merges 4 parallel uncached static requests into 1 upstream roundtrip!
    const parRes = await Promise.all([
      httpReq(proxyPort, 'GET', '/_next/static/chunks/parallel.js'),
      httpReq(proxyPort, 'GET', '/_next/static/chunks/parallel.js'),
      httpReq(proxyPort, 'GET', '/_next/static/chunks/parallel.js'),
      httpReq(proxyPort, 'GET', '/_next/static/chunks/parallel.js'),
    ]);
    assert.strictEqual(parallelHits, 1);
    for (const pr of parRes) {
      assert.strictEqual(pr.status, 200);
      assert.strictEqual(pr.body, 'console.log("parallel-1");');
    }

    // 3. Safe JSON data cache + mutation/Set-Cookie invalidation
    const d1 = await httpReq(proxyPort, 'GET', '/api/catalog');
    assert.strictEqual(JSON.parse(d1.body).rev, 1);
    const d2 = await httpReq(proxyPort, 'GET', '/api/catalog');
    assert.strictEqual(JSON.parse(d2.body).rev, 1);
    assert.strictEqual(d2.headers['x-neon-cache'], 'HIT');
    assert.strictEqual(dataHits, 1);

    // Mutating POST + Set-Cookie bumps jarEpoch and clears dataCache!
    await httpReq(proxyPort, 'POST', '/api/watch-ping');
    const d3 = await httpReq(proxyPort, 'GET', '/api/catalog');
    assert.strictEqual(JSON.parse(d3.body).rev, 2);
    assert.strictEqual(dataHits, 2);

    // 3. Auth endpoints, HLS playlists (.m3u8), and Range requests NEVER cache!
    const a1 = await httpReq(proxyPort, 'GET', '/api/auth/me');
    const a2 = await httpReq(proxyPort, 'GET', '/api/auth/me');
    assert.strictEqual(JSON.parse(a1.body).user, 1);
    assert.strictEqual(JSON.parse(a2.body).user, 2);
    assert.strictEqual(authHits, 2);

    await httpReq(proxyPort, 'GET', '/live/stream.m3u8');
    await httpReq(proxyPort, 'GET', '/live/stream.m3u8');
    assert.strictEqual(hlsHits, 2);

    await httpReq(proxyPort, 'GET', '/video/movie.mp4', { Range: 'bytes=0-3' });
    await httpReq(proxyPort, 'GET', '/video/movie.mp4', { Range: 'bytes=0-3' });
    assert.strictEqual(rangeHits, 2);

    mod.exports.onStop();
    if (upstream.closeAllConnections) upstream.closeAllConnections();
    upstream.close();
  } catch (err) {
    console.error(err);
    process.exit(1);
  }
});
""",
        encoding='utf-8',
    )
    node_exe = shutil.which('node') or shutil.which('nodejs')
    chk = subprocess.run([node_exe, str(runner)], capture_output=True, text=True, timeout=15)
    assert chk.returncode == 0, chk.stderr + chk.stdout





