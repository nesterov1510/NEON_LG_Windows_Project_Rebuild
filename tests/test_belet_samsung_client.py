import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from local_tv_packages import validate_wgt
from samsung_tizen import BELET_PKG_ID
from scripts.build_belet_samsung import FILES, SRC, build


TEXT_MEMBERS = ('.xml', '.html', '.css', '.js', '.LICENSE')


def _same_member(archive_bytes, source_bytes, name):
    """Text members are compared line-wise: a Windows checkout may hold CRLF."""
    if name.endswith(TEXT_MEMBERS):
        return archive_bytes.replace(b'\r\n', b'\n') == source_bytes.replace(b'\r\n', b'\n')
    return archive_bytes == source_bytes


def _client_source(name):
    return (SRC / name).read_text(encoding='utf-8')


def _strip_comments_and_strings(js):
    """Rough ES5-syntax probe helper: drop comments and string literals."""
    out = []
    i = 0
    length = len(js)
    while i < length:
        char = js[i]
        nxt = js[i + 1] if i + 1 < length else ''
        if char == '/' and nxt == '/':
            while i < length and js[i] != '\n':
                i += 1
        elif char == '/' and nxt == '*':
            i += 2
            while i < length - 1 and not (js[i] == '*' and js[i + 1] == '/'):
                i += 1
            i += 2
        elif char in '"\'':
            quote = char
            i += 1
            while i < length and js[i] != quote:
                i += 2 if js[i] == '\\' else 1
            i += 1
        else:
            out.append(char)
            i += 1
    return ''.join(out)


def test_package_is_isolated_and_contains_only_local_app(tmp_path):
    output = build(tmp_path / 'client.wgt')
    data = output.read_bytes()
    assert validate_wgt(data) == ('BeletFilm1', 'BeletFilm1.BeletClient')
    assert BELET_PKG_ID != 'BeletFilm1'
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == set(FILES)
        for name in FILES:
            assert _same_member(archive.read(name), (SRC / name).read_bytes(), name), name
        html = archive.read('index.html').decode('utf-8')
        config = archive.read('config.xml').decode('utf-8')
        assert '<iframe' not in html.lower()
        assert 'service/proxy.js' not in config
        assert '18765' not in config
        # Direct HTTPS API client; local service is strictly the optional 18767 TLS clock bridge.
        assert 'service/clock-proxy.js' in config
        assert 'BeletFilm1.ClockProxy' in config
        assert '18767' in config


def test_client_bundles_are_es5_and_self_contained():
    for name in ('api.js', 'app.js', 'service/clock-proxy.js'):
        code = _strip_comments_and_strings(_client_source(name))
        assert '=>' not in code, '%s must stay ES5 for the Tizen web engine' % name
        assert '`' not in code, '%s must not use template literals' % name
        assert not re.search(r'\b(let|const|class)\s', code), '%s must not use ES6 declarations' % name
    html = _client_source('index.html')
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', html)
    assert scripts == ['hls.min.js', 'api.js', 'app.js']
    for src in scripts:
        assert not src.startswith(('http://', 'https://', '//')), src
        assert (SRC / src).is_file(), src
    assert ' onclick=' not in html.lower()


def test_bundled_hls_player_is_vendored_with_its_license():
    vendor = SRC / 'hls.min.js'
    license_file = SRC / 'hls.LICENSE'
    assert vendor.is_file() and license_file.is_file()
    text = vendor.read_text(encoding='utf-8', errors='replace')
    assert 'Hls' in text
    assert 'enableWorker' in text, 'expected the hls.js build'
    assert 'Apache License' in license_file.read_text(encoding='utf-8')
    assert 'hls.min.js' in FILES and 'hls.LICENSE' in FILES


def test_stylesheet_avoids_css_younger_than_tizen_engines():
    css = _client_source('style.css')
    assert 'inset:' not in css, '`inset` shorthand is unsupported on Tizen 4-6 web engines'
    assert not re.search(r'[^-]gap:', css), 'flexbox gap needs Chromium 84; use margins/grid-gap'
    assert 'grid-gap:' in css
    assert 'display: flex' in css


def test_client_targets_belet_api_hosts_and_ships_csp():
    config = _client_source('config.xml')
    for host in ('https://beletapis.com', 'https://*.beletapis.com', 'https://api.belet.tm',
                 'https://belet.tm', 'https://*.belet.tm', 'https://*.belet.me'):
        assert host in config, host
    # Tizen needs the wildcard access rule for CDN images; the CSP below is what
    # actually pins scripts, XHR and media to the Belet hosts.
    assert '<tizen:content-security-policy>' in config
    assert "script-src 'self'" in config
    # HLS segments are fetched by hls.js over XHR, so videofilm.belet.me must be in connect-src.
    csp = config[config.index('<tizen:content-security-policy>'):config.index('</tizen:content-security-policy>')]
    connect = csp[csp.index('connect-src'):csp.index(';', csp.index('connect-src'))]
    assert 'https://*.belet.me' in connect
    assert 'https://*.belet.tm' in connect
    assert 'http:' in connect
    img_src = csp[csp.index('img-src'):csp.index(';', csp.index('img-src'))]
    media_src = csp[csp.index('media-src'):csp.index(';', csp.index('media-src'))]
    for src_tok in ("'self'", 'https:', 'http:', 'data:', 'blob:'):
        assert src_tok in img_src
    for src_tok in ('https:', 'http:', 'blob:'):
        assert src_tok in media_src
    assert 'http://developer.samsung.com/privilege/avplay' in config, 'AVPlay needs its Tizen privilege'
    api = _client_source('api.js')
    for path in ('/api/v1/auth/sign-in', '/api/v1/auth/check-code-mobile', '/api/v1/auth/get-qrcode',
                 '/api/v1/auth/check-qrcode', '/api/v2/auth/refresh-mobile', '/api/v2/users/profile',
                 '/api/v3/movie/', '/api/v3/files/', '/api/v3/lastWatchTime', '/api/v1/favorites',
                 '/api/v2/search', '/api/v1/users/device'):
        assert path in api, path


def test_every_dom_id_used_by_app_exists_in_markup():
    html = _client_source('index.html')
    app = _client_source('app.js')
    markup_ids = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r"\$\('([^']+)'\)", app))
    missing = sorted(used - markup_ids)
    assert not missing, 'app.js references unknown ids: %s' % missing


def test_mock_login_catalog_playback_and_logout():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not available')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / 'tests/belet_samsung_client_mock.js'), str(SRC / 'app.js')],
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'belet_samsung_client_mock: ok' in result.stdout


def test_bundled_wgt_matches_sources():
    root = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(root / 'samsung-belet/BeletSamsungClient-unsigned.wgt') as archive:
        assert set(archive.namelist()) == set(FILES)
        for name in FILES:
            assert _same_member(archive.read(name), (SRC / name).read_bytes(), name), name


def test_neon_embeds_client_in_all_packaging_configs():
    root = Path(__file__).resolve().parents[1]
    for name in ('build_windows.bat', 'build_windows_81.bat', 'build_linux.sh',
                 '.github/workflows/build-windows.yml', '.github/workflows/build-windows-81.yml',
                 '.github/workflows/build-linux.yml'):
        assert 'samsung-belet/BeletSamsungClient-unsigned.wgt' in (root / name).read_text(encoding='utf-8')


def test_bundled_client_installs_signs_and_launches_without_replacing_belet(monkeypatch):
    from core import Api
    events = []
    class Sdb:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def capability(self): return {}
        def get_duid(self, **kwargs): return 'mock-device'
        def resolve_sdk_toolpath(self, caps): return '/tmp/sdk_tools'
        def push_bytes(self, payload, path): events.append(('upload', payload, path))
        def install_wgt(self, pkg, path, timeout):
            events.append(('install', pkg))
            return 'install completed'
        def launch_app(self, app):
            events.append(('launch', app))
            return 'launched'
    class Cert:
        def find_saved_samsung_certs(self, duid):
            return {'author_key': 'a', 'author_certs': ['a'], 'dist_key': 'd', 'dist_certs': ['d']}
    api = Api()
    monkeypatch.setattr('core.SdbClient', Sdb)
    monkeypatch.setattr(api, '_samsung_cert_manager', lambda: Cert())
    def sign(data, *args):
        assert validate_wgt(data) == ('BeletFilm1', 'BeletFilm1.BeletClient')
        events.append(('sign',))
        return b'signed'
    monkeypatch.setattr('core.sign_wgt_bytes', sign)
    monkeypatch.setattr('core.verify_wgt_signatures', lambda data: events.append(('verify', data)))
    result = api.install_samsung_belet_client('192.168.1.3')
    assert result['ok'] and result['pkg_id'] == 'BeletFilm1'
    assert events == [('sign',), ('verify', b'signed'), ('upload', b'signed', '/tmp/sdk_tools/BeletFilm1.wgt'),
                      ('install', 'BeletFilm1'), ('launch', 'BeletFilm1.BeletClient')]
    assert BELET_PKG_ID not in str(events)
    monkeypatch.setattr(Sdb, 'launch_app', lambda *args: (_ for _ in ()).throw(RuntimeError('not launched')))
    result = api.install_samsung_belet_client('192.168.1.3')
    assert result['ok'] and 'automatic launch failed' in result['output']


def test_bundled_client_rejects_wrong_package_before_connecting(monkeypatch):
    from core import Api
    from samsung_tizen import build_belet_wgt
    monkeypatch.setattr('core.read_package', lambda *args: build_belet_wgt())
    monkeypatch.setattr('core.SdbClient', lambda *args, **kwargs: pytest.fail('should not connect'))
    result = Api().install_samsung_belet_client('192.168.1.3')
    assert not result['ok'] and 'unexpected package ID' in result['error']


def test_client_packaged_resource_resolution_and_ui(monkeypatch, tmp_path):
    import sys
    from core import Api
    root = Path(__file__).resolve().parents[1]
    bundle = tmp_path / 'samsung-belet'
    bundle.mkdir()
    (bundle / 'BeletSamsungClient-unsigned.wgt').write_bytes((root / 'samsung-belet/BeletSamsungClient-unsigned.wgt').read_bytes())
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    api = Api()
    calls = []
    def install(ip, path, **kwargs):
        calls.append((ip, path, kwargs))
        assert validate_wgt(Path(path).read_bytes())[0] == 'BeletFilm1'
        return {'ok': True}
    monkeypatch.setattr(api, 'install_local_samsung_wgt', install)
    assert api.install_samsung_belet_client('192.168.1.3')['ok']
    assert api.install_samsung_belet_direct_client('192.168.1.3')['ok']
    assert calls[0][0] == '192.168.1.3'
    assert calls[0][2] == {'_expected_pkg': 'BeletFilm1', '_launch': True}
    assert calls[1][2] == {'_expected_pkg': 'BeletFilm1', '_launch': True, '_bypass_mode': 'direct'}
    assert 'BELET TV · С ОБХОДОМ' in (root / 'ui.html').read_text(encoding='utf-8')
    assert 'BELET TV · БЕЗ ОБХОДА' in (root / 'ui.html').read_text(encoding='utf-8')
    ui_qt_txt = (root / 'ui_qt.py').read_text(encoding='utf-8')
    assert 'Belet TV · С обходом' in ui_qt_txt
    assert 'Belet TV · Без обхода' in ui_qt_txt


def test_direct_client_strips_clock_proxy_and_sets_bypass_off(monkeypatch):
    from core import Api
    uploaded = []

    class Sdb:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def capability(self): return {}
        def get_duid(self, **kwargs): return 'mock-device'
        def resolve_sdk_toolpath(self, caps): return '/tmp/sdk_tools'
        def push_bytes(self, payload, path): uploaded.append(payload)
        def install_wgt(self, pkg, path, timeout): return 'install completed'
        def launch_app(self, app): return 'launched'

    class Cert:
        def find_saved_samsung_certs(self, duid):
            return {'author_key': 'a', 'author_certs': ['a'], 'dist_key': 'd', 'dist_certs': ['d']}

    api = Api()
    monkeypatch.setattr('core.SdbClient', Sdb)
    monkeypatch.setattr('core._tizen_local_ip', lambda *a: '192.168.1.50')
    monkeypatch.setattr(api, '_samsung_cert_manager', lambda: Cert())
    monkeypatch.setattr('core.sign_wgt_bytes', lambda data, *args: data)
    monkeypatch.setattr('core.verify_wgt_signatures', lambda data: None)

    r_bypass = api.install_samsung_belet_client('192.168.1.3')
    r_direct = api.install_samsung_belet_direct_client('192.168.1.3')
    assert r_bypass['ok'] and r_direct['ok'] and len(uploaded) == 2

    with zipfile.ZipFile(__import__('io').BytesIO(uploaded[0])) as zb:
        assert 'service/clock-proxy.js' in zb.namelist()
        assert '<tizen:service' in zb.read('config.xml').decode('utf-8')
        html_b = zb.read('index.html').decode('utf-8')
        assert '<meta name="neon-bypass-mode" content="always">' in html_b
        assert '<meta name="neon-pc-bridge" content="192.168.1.50">' in html_b

    with zipfile.ZipFile(__import__('io').BytesIO(uploaded[1])) as zd:
        assert 'service/clock-proxy.js' not in zd.namelist()
        assert 'wrt-service.js' not in zd.namelist()
        cfg_d = zd.read('config.xml').decode('utf-8')
        assert '<tizen:service' not in cfg_d
        assert 'background-support="disable"' in cfg_d
        html_d = zd.read('index.html').decode('utf-8')
        assert '<meta name="neon-bypass-mode" content="off">' in html_d
        assert 'neon-pc-bridge' not in html_d


def test_launch_and_remove_samsung_belet_client(monkeypatch):
    from core import Api
    calls = []

    class Sdb:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def launch_app(self, app):
            calls.append(('launch', app))
            return 'successfully launched'
        def uninstall_app(self, pkg):
            calls.append(('uninstall', pkg))
            return 'uninstall completed'

    api = Api()
    monkeypatch.setattr('core.SdbClient', Sdb)
    r1 = api.launch_samsung_belet_client('192.168.1.3')
    r2 = api.remove_samsung_belet_client('192.168.1.3')
    assert r1['ok'] and r1['app_id'] == 'BeletFilm1.BeletClient'
    assert r2['ok'] and r2['pkg_id'] == 'BeletFilm1'
    assert calls == [('launch', 'BeletFilm1.BeletClient'), ('uninstall', 'BeletFilm1')]


def test_bundled_client_strips_service_on_manifest_error(monkeypatch):
    from core import Api
    uploaded = []
    install_calls = []

    class Sdb:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def capability(self): return {}
        def get_duid(self, **kwargs): return 'mock-device'
        def resolve_sdk_toolpath(self, caps): return '/tmp/sdk_tools'
        def push_bytes(self, payload, path): uploaded.append(payload)
        def install_wgt(self, pkg, path, timeout):
            install_calls.append(pkg)
            return 'install failed[118019]' if len(install_calls) == 1 else 'install completed'
        def launch_app(self, app): return 'launched'

    class Cert:
        def find_saved_samsung_certs(self, duid):
            return {'author_key': 'a', 'author_certs': ['a'], 'dist_key': 'd', 'dist_certs': ['d']}

    api = Api()
    monkeypatch.setattr('core.SdbClient', Sdb)
    monkeypatch.setattr('core._tizen_local_ip', lambda *a: '192.168.1.50')
    monkeypatch.setattr(api, '_samsung_cert_manager', lambda: Cert())
    monkeypatch.setattr('core.sign_wgt_bytes', lambda data, *args: data)
    monkeypatch.setattr('core.verify_wgt_signatures', lambda data: None)
    result = api.install_samsung_belet_client('192.168.1.3')
    assert result['ok'] and len(uploaded) == 2 and len(install_calls) == 2
    assert 'ClockProxy' in result['output']
    with zipfile.ZipFile(__import__('io').BytesIO(uploaded[1])) as zf:
        assert 'service/clock-proxy.js' not in zf.namelist()
        assert 'wrt-service.js' not in zf.namelist()
        assert '<tizen:service' not in zf.read('config.xml').decode('utf-8')
        assert '<meta name="neon-pc-bridge" content="192.168.1.50">' in zf.read('index.html').decode('utf-8')


def test_clock_proxy_handles_1970_clock_tls_and_m3u8_rewriting(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not available')
    import datetime
    import json
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ca_key = rsa.generate_private_key(65537, 2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'TestCA')])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=100))
        .not_valid_after(now + datetime.timedelta(days=900))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    def make_leaf(host, issuer=(ca_name, ca_key), offset_days=-30, span_days=365):
        k = rsa.generate_private_key(65537, 2048)
        n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        c = (
            x509.CertificateBuilder()
            .subject_name(n).issuer_name(issuer[0])
            .public_key(k.public_key())
            .serial_number(2)
            .not_valid_before(now + datetime.timedelta(days=offset_days))
            .not_valid_after(now + datetime.timedelta(days=offset_days + span_days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(issuer[1], hashes.SHA256())
        )
        return k, c

    k_ok, c_ok = make_leaf('beletapis.com')
    k_cdn, c_cdn = make_leaf('cdn-media.example.org')
    k_bad, c_bad = make_leaf('evil.example')
    k_exp, c_exp = make_leaf('api.belet.tm', offset_days=-400, span_days=100)
    k_self = rsa.generate_private_key(65537, 2048)
    n_self = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'belet.tm')])
    c_self = (
        x509.CertificateBuilder()
        .subject_name(n_self).issuer_name(n_self)
        .public_key(k_self.public_key()).serial_number(3)
        .not_valid_before(now - datetime.timedelta(days=30))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('belet.tm')]), critical=False)
        .sign(k_self, hashes.SHA256())
    )

    for fn, obj in [('ca.pem', ca_cert), ('ok.pem', c_ok), ('cdn.pem', c_cdn),
                     ('bad.pem', c_bad), ('exp.pem', c_exp), ('self.pem', c_self)]:
        (tmp_path / fn).write_bytes(obj.public_bytes(serialization.Encoding.PEM))
    for fn, obj in [('ok.key', k_ok), ('cdn.key', k_cdn), ('bad.key', k_bad),
                     ('exp.key', k_exp), ('self.key', k_self)]:
        (tmp_path / fn).write_bytes(obj.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    proxy_path = SRC / 'service' / 'clock-proxy.js'
    runner = tmp_path / 'test_clock_proxy.js'
    runner.write_text('''
const assert = require('assert');
const fs = require('fs');
const http = require('http');
const https = require('https');
const vm = require('vm');
const td = __TD__;
const caPem = fs.readFileSync(td + '/ca.pem');
const serverDateHdr = new Date().toUTCString();

const srvOk = https.createServer({ key: fs.readFileSync(td + '/ok.key'), cert: fs.readFileSync(td + '/ok.pem') }, (req, res) => {
  let body = ''; req.on('data', d => body += d); req.on('end', () => {
    if (req.url === '/api/v3/files/1') {
      res.writeHead(200, { 'Content-Type': 'application/json', Date: serverDateHdr });
      res.end(JSON.stringify({ url: 'https://cdn-media.example.org/hls/1/master.m3u8', got: body }));
      return;
    }
    if (req.url === '/redir-rel') {
      res.writeHead(302, { Location: '/v2/next?x=1', Date: serverDateHdr });
      res.end();
      return;
    }
    if (req.url === '/redir-proto') {
      res.writeHead(302, { Location: '//cdn-media.example.org/hls/1/master.m3u8', Date: serverDateHdr });
      res.end();
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json', Date: serverDateHdr });
    res.end(JSON.stringify({ ok: true }));
  });
});
const srvCdn = https.createServer({ key: fs.readFileSync(td + '/cdn.key'), cert: fs.readFileSync(td + '/cdn.pem') }, (req, res) => {
  res.writeHead(200, { 'Content-Type': 'application/vnd.apple.mpegurl', Date: serverDateHdr });
  res.end('#EXTM3U\\n#EXT-X-KEY:METHOD=AES-128,URI="https://keys.belet.me/k.key"\\n#EXT-X-MAP:URI="/hls/1/init.mp4"\\nhttps://cdn-media.example.org/hls/1/720.m3u8\\n/hls/1/480.m3u8\\nseg1.ts\\n');
});
const srvBadHost = https.createServer({ key: fs.readFileSync(td + '/bad.key'), cert: fs.readFileSync(td + '/bad.pem') }, (req, res) => res.end('no'));
const srvSelf = https.createServer({ key: fs.readFileSync(td + '/self.key'), cert: fs.readFileSync(td + '/self.pem') }, (req, res) => res.end('no'));
const srvExp = https.createServer({ key: fs.readFileSync(td + '/exp.key'), cert: fs.readFileSync(td + '/exp.pem') }, (req, res) => {
  res.writeHead(200, { Date: serverDateHdr }); res.end('exp');
});

Promise.all([
  new Promise(r => srvOk.listen(0, '127.0.0.1', r)),
  new Promise(r => srvCdn.listen(0, '127.0.0.1', r)),
  new Promise(r => srvBadHost.listen(0, '127.0.0.1', r)),
  new Promise(r => srvSelf.listen(0, '127.0.0.1', r)),
  new Promise(r => srvExp.listen(0, '127.0.0.1', r)),
]).then(async () => {
  let modePort = srvOk.address().port;
  const wrappedHttps = {
    Agent: function (o) { return new https.Agent(Object.assign({ ca: caPem }, o)); },
    request(opts, cb) {
      const o = Object.assign({}, opts, { host: '127.0.0.1', hostname: '127.0.0.1', port: modePort, ca: caPem });
      return https.request(o, cb);
    }
  };
  const RealDate = Date;
  const fakeMs = RealDate.UTC(1970, 0, 1, 1, 2, 0);
  function FakeDate(a) { return a === undefined ? new RealDate(fakeMs) : new RealDate(a); }
  FakeDate.now = () => fakeMs; FakeDate.UTC = RealDate.UTC; FakeDate.parse = RealDate.parse;
  const mod = { exports: {} };
  const ctx = {
    require: (n) => n === 'https' ? wrappedHttps : require(n),
    module: mod, exports: mod.exports, Buffer, console, process, setTimeout, clearTimeout, Date: FakeDate
  };
  const src = fs.readFileSync(__PROXY_PATH__, 'utf8').replace(/var PORT = \\d+;/, 'var PORT = 0;');
  vm.runInNewContext(src, ctx);
  const srv = mod.exports.server;
  if (!srv.listening) await new Promise(r => srv.once('listening', r));
  const p = srv.address().port;
  const get = (path, method = 'GET', body = null, headers = {}) => new Promise((res, rej) => {
    const req = http.request({ host: '127.0.0.1', port: p, path, method, headers }, r => {
      let b = ''; r.on('data', d => b += d); r.on('end', () => res({ status: r.statusCode, headers: r.headers, body: b }));
    });
    req.on('error', rej);
    if (body) req.write(body);
    req.end();
  });

  try {
    const p1 = await get('/__ping');
    assert.strictEqual(p1.status, 200);
    assert.strictEqual(JSON.parse(p1.body).sslClockYear, 1970);

    const r1 = await get('/__proxy/beletapis.com/api/v3/files/1', 'POST', '{"x":1}', { Authorization: 'tok123' });
    assert.strictEqual(r1.status, 200);
    assert.strictEqual(JSON.parse(r1.body).got, '{"x":1}');
    const rRedirRel = await get('/__proxy/beletapis.com/redir-rel');
    assert.strictEqual(rRedirRel.status, 302);
    assert.strictEqual(rRedirRel.headers.location, 'http://127.0.0.1:' + p + '/__proxy/beletapis.com/v2/next?x=1');
    const rRedirProto = await get('/__proxy/beletapis.com/redir-proto');
    assert.strictEqual(rRedirProto.status, 302);
    assert.strictEqual(rRedirProto.headers.location, 'http://127.0.0.1:' + p + '/__proxy/cdn-media.example.org/hls/1/master.m3u8');
    const p2 = await get('/__ping');
    assert(JSON.parse(p2.body).serverTimeMs > 1704067200000);

    modePort = srvCdn.address().port;
    const r2 = await get('/__proxy/cdn-media.example.org/hls/1/master.m3u8');
    assert.strictEqual(r2.status, 200);
    assert(r2.body.includes('http://127.0.0.1:' + p + '/__proxy/keys.belet.me/k.key'));
    assert(r2.body.includes('URI="http://127.0.0.1:' + p + '/__proxy/cdn-media.example.org/hls/1/init.mp4"'));
    assert(r2.body.includes('http://127.0.0.1:' + p + '/__proxy/cdn-media.example.org/hls/1/720.m3u8'));
    assert(r2.body.includes('\\nhttp://127.0.0.1:' + p + '/__proxy/cdn-media.example.org/hls/1/480.m3u8\\n'));

    assert.strictEqual((await get('/__proxy/evil-untrusted.com/x')).status, 403);
    assert.strictEqual((await get('/__proxy/127.0.0.1/x')).status, 403);

    modePort = srvBadHost.address().port;
    assert.strictEqual((await get('/__proxy/homepage.belet.me/test')).status, 502);

    modePort = srvSelf.address().port;
    assert.strictEqual((await get('/__proxy/belet.tm/test')).status, 502);

    modePort = srvExp.address().port;
    assert.strictEqual((await get('/__proxy/api.belet.tm/test')).status, 502);
  } finally {
    mod.exports.onStop();
    [srvOk, srvCdn, srvBadHost, srvSelf, srvExp].forEach(s => { if (s.closeAllConnections) s.closeAllConnections(); s.close(); });
  }
});
'''.replace('__TD__', json.dumps(str(tmp_path))).replace('__PROXY_PATH__', json.dumps(str(proxy_path))), encoding='utf-8')
    res = subprocess.run([node, str(runner)], capture_output=True, text=True, encoding='utf-8', timeout=25)
    assert res.returncode == 0, res.stdout + res.stderr


def test_inline_wrt_module_csp_hash_matches_config():
    import base64
    import hashlib
    app = _client_source('app.js')
    config = _client_source('config.xml')
    m = re.search(r'var WRT_INLINE_CODE = "([^"]+)";', app)
    assert m, 'WRT_INLINE_CODE constant missing in app.js'
    expected_hash = 'sha256-' + base64.b64encode(hashlib.sha256(m.group(1).encode('utf-8')).digest()).decode('ascii')
    assert expected_hash in config, f'CSP missing exact hash {expected_hash}'


def test_pc_bridge_server_proxies_https_and_rewrites_m3u8(tmp_path, monkeypatch):
    import datetime
    import http.client
    import json
    import ssl
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from core import Api, _BeletTvBridge

    ca_key = rsa.generate_private_key(65537, 2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'TestBridgeCA')])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=10))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(65537, 2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'beletapis.com')])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name).issuer_name(ca_name)
        .public_key(leaf_key.public_key()).serial_number(2)
        .not_valid_before(now - datetime.timedelta(days=10))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName('beletapis.com'),
            x509.DNSName('cdn-pc.example.org'),
        ]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    (tmp_path / 'ca.pem').write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / 'leaf.pem').write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / 'leaf.key').write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    class UpHandler(BaseHTTPRequestHandler):
        def log_message(self, *a): return
        def do_POST(self):
            ln = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(ln).decode('utf-8')
            out = json.dumps({'url': 'https://cdn-pc.example.org/hls/9/master.m3u8', 'echo': body}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        def do_GET(self):
            m3u = b'#EXTM3U\n#EXT-X-MAP:URI="/hls/9/init.mp4"\nhttps://keys.belet.me/k.key\n/hls/9/720.m3u8\n'
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(m3u)))
            self.end_headers()
            self.wfile.write(m3u)

    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(str(tmp_path / 'leaf.pem'), str(tmp_path / 'leaf.key'))
    up = HTTPServer(('127.0.0.1', 0), UpHandler)
    up.socket = srv_ctx.wrap_socket(up.socket, server_side=True)
    up_t = threading.Thread(target=up.serve_forever, daemon=True)
    up_t.start()

    cli_ctx = ssl.create_default_context(cafile=str(tmp_path / 'ca.pem'))

    def open_up(h, p, ctx, timeout):
        import socket as _sock

        class LocalHttps(http.client.HTTPSConnection):
            def connect(self):
                raw = _sock.create_connection(('127.0.0.1', up.server_address[1]), timeout)
                self.sock = ctx.wrap_socket(raw, server_hostname=h)

        return LocalHttps(h, p, context=ctx, timeout=timeout)

    bridge = _BeletTvBridge(
        host='127.0.0.1',
        port=0,
        ssl_context=cli_ctx,
        upstream_opener=open_up,
    )
    port = bridge.start()
    try:
        ping = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{port}/__ping', timeout=5).read().decode('utf-8'))
        assert ping['ok'] and ping['mode'] == 'pc-bridge' and ping['port'] == port

        req = urllib.request.Request(
            f'http://127.0.0.1:{port}/__proxy/beletapis.com/api/v3/files/9',
            data=b'{"x":1}',
            method='POST',
            headers={'Content-Type': 'application/json'},
        )
        r1 = json.loads(urllib.request.urlopen(req, timeout=5).read().decode('utf-8'))
        assert r1['echo'] == '{"x":1}'

        m3u_out = urllib.request.urlopen(f'http://127.0.0.1:{port}/__proxy/cdn-pc.example.org/hls/9/master.m3u8', timeout=5).read().decode('utf-8')
        assert f'http://127.0.0.1:{port}/__proxy/keys.belet.me/k.key' in m3u_out
        assert f'URI="http://127.0.0.1:{port}/__proxy/cdn-pc.example.org/hls/9/init.mp4"' in m3u_out
        assert f'\nhttp://127.0.0.1:{port}/__proxy/cdn-pc.example.org/hls/9/720.m3u8\n' in m3u_out

        with pytest.raises(urllib.error.HTTPError) as e403:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/__proxy/evil-untrusted.com/x', timeout=5)
        assert e403.value.code == 403

        assert bridge.rewrite_location('/v2/next?a=1', f'http://127.0.0.1:{port}', 'beletapis.com') == (
            f'http://127.0.0.1:{port}/__proxy/beletapis.com/v2/next?a=1'
        )
        assert bridge.rewrite_location('//cdn-pc.example.org/hls/9/a.m3u8', f'http://127.0.0.1:{port}', 'beletapis.com') == (
            f'http://127.0.0.1:{port}/__proxy/cdn-pc.example.org/hls/9/a.m3u8'
        )
        bridge.scan_json_for_hosts('{"poster":"http://cdn-pc2.example.org/poster.jpg"}')
        assert bridge.is_allowed_host('cdn-pc2.example.org') is True
    finally:
        bridge.stop()
        up.shutdown()
        up.server_close()

    # Verify Api lifecycle + install_local_samsung_wgt baking PC IP meta tag
    api = Api()
    monkeypatch.setattr('core._tizen_local_ip', lambda *a: '192.168.1.50')
    monkeypatch.setattr(api, '_ensure_windows_bridge_firewall', lambda *a: None)
    st0 = api.start_belet_tv_bridge('192.168.1.101', port=0)
    assert st0['ok'] and st0['running'] and st0['pc_ip'] == '192.168.1.50' and st0['port'] > 0
    assert api.get_belet_tv_bridge_status()['running'] is True
    assert api.stop_belet_tv_bridge()['running'] is False
    assert api.get_belet_tv_bridge_status()['running'] is False

    root = Path(__file__).resolve().parents[1]
    for name in ('ui.html', 'app_legacy.py'):
        assert '18767' in (root / name).read_text(encoding='utf-8')

