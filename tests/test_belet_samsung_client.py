import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from local_tv_packages import validate_wgt
from samsung_tizen import BELET_PKG_ID
from scripts.build_belet_samsung import FILES, SRC, build


def test_package_is_isolated_and_contains_only_local_app(tmp_path):
    output = build(tmp_path / 'client.wgt')
    data = output.read_bytes()
    assert validate_wgt(data) == ('BeletFilm1', 'BeletFilm1.BeletClient')
    assert BELET_PKG_ID != 'BeletFilm1'
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == set(FILES)
        for name in FILES:
            assert archive.read(name) == (SRC / name).read_bytes()
        html = archive.read('index.html').decode('utf-8')
        config = archive.read('config.xml').decode('utf-8')
        assert '<iframe' not in html.lower()
        assert 'service/proxy.js' not in config
        assert 'экспериментальная версия' in html


def test_mock_login_catalog_playback_and_logout():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not available')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / 'tests/belet_samsung_client_mock.js'), str(SRC / 'app.js')],
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr


def test_bundled_wgt_matches_sources():
    root = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(root / 'samsung-belet/BeletSamsungClient-unsigned.wgt') as archive:
        assert set(archive.namelist()) == set(FILES)
        for name in FILES:
            assert archive.read(name) == (SRC / name).read_bytes()


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
