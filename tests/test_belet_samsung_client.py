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
        # The client talks to the Belet API directly, so no proxy/localhost bridge.
        assert 'localhost' not in config.lower()
        assert '127.0.0.1' not in config


def test_client_bundles_are_es5_and_self_contained():
    for name in ('api.js', 'app.js'):
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
                 'https://*.belet.me'):
        assert host in config, host
    # Tizen needs the wildcard access rule for CDN images; the CSP below is what
    # actually pins scripts, XHR and media to the Belet hosts.
    assert '<tizen:content-security-policy>' in config
    assert "script-src 'self'" in config
    # HLS segments are fetched by hls.js over XHR, so videofilm.belet.me must be in connect-src.
    csp = config[config.index('<tizen:content-security-policy>'):config.index('</tizen:content-security-policy>')]
    connect = csp[csp.index('connect-src'):csp.index(';', csp.index('connect-src'))]
    assert 'https://*.belet.me' in connect
    assert 'media-src https: blob:' in csp
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
    assert calls[0][0] == '192.168.1.3'
    assert calls[0][2] == {'_expected_pkg': 'BeletFilm1', '_launch': True}
    for name in ('app_legacy.py', 'ui.html'):
        assert 'УСТАНОВИТЬ BELET SAMSUNG · НОВЫЙ КЛИЕНТ' in (root / name).read_text(encoding='utf-8')
