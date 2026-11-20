import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import belet_lg_client
from local_tv_packages import validate_ipk

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'belet_samsung_native'
BASE = f'usr/palm/applications/{belet_lg_client.APP_ID}'


def ar_members(data):
    assert data.startswith(b'!<arch>\n')
    pos, members = len(b'!<arch>\n'), {}
    while pos < len(data):
        header = data[pos:pos + 60]
        assert header[58:60] == b'`\n'
        name = header[:16].decode('ascii').strip()
        assert not name.endswith('/')
        size = int(header[48:58].decode('ascii').strip())
        members[name] = data[pos + 60:pos + 60 + size]
        pos += 60 + size + (size % 2)
    return members


def ipk_files(data):
    members = ar_members(data)
    with tarfile.open(fileobj=io.BytesIO(members['data.tar.gz'])) as tar:
        return {
            name[len(BASE) + 1:]: tar.extractfile(name).read()
            for name in tar.getnames()
            if name.startswith(BASE + '/') and not name.endswith('/')
        }


def test_ipk_is_a_valid_webos_package():
    data = belet_lg_client.build_ipk(mtime=1700000000)
    validate_ipk(data)
    members = ar_members(data)
    assert list(members) == ['debian-binary', 'control.tar.gz', 'data.tar.gz']
    assert members['debian-binary'] == b'2.0\n'

    with tarfile.open(fileobj=io.BytesIO(members['control.tar.gz'])) as tar:
        control = tar.extractfile('control').read().decode('utf-8')
    version = belet_lg_client.app_version()
    assert f'Package: {belet_lg_client.APP_ID}\n' in control
    assert f'Version: {version}\n' in control
    assert 'webOS-Package-Format-Version: 2\n' in control


def test_appinfo_and_assets_match_webos_requirements():
    files = ipk_files(belet_lg_client.build_ipk(mtime=1700000000))
    info = json.loads(files['appinfo.json'].decode('utf-8'))
    assert info['id'] == 'tm.belet.neonclient'
    assert info['type'] == 'web'
    assert info['main'] == 'index.html'
    assert info['version'] == belet_lg_client.app_version()
    for key in ('icon', 'largeIcon', 'splashBackground'):
        assert files[info[key]], f'{key} is bundled'
    assert b'<widget' not in files['appinfo.json']


def lf(data):
    """The IPK is line-ending independent; compare against LF-normalised sources."""
    return data.replace(b'\r\n', b'\n')


def test_shared_client_sources_are_identical_except_webos_overlays():
    files = ipk_files(belet_lg_client.build_ipk(mtime=1700000000))
    for name in belet_lg_client.SHARED_FILES:
        if name in ('index.html', 'api.js'):
            continue
        expected = lf((SRC / name).read_bytes()) if name in belet_lg_client.TEXT_FILES else (SRC / name).read_bytes()
        assert files[name] == expected, f'{name} is the shared client file'

    html = files['index.html'].decode('utf-8')
    original = lf((SRC / 'index.html').read_bytes()).decode('utf-8')
    assert html == original.replace('<script src="hls.min.js"></script>\n',
                                    '<script src="webos-shim.js"></script>\n<script src="hls.min.js"></script>\n', 1)
    assert html.index('webos-shim.js') < html.index('api.js')

    api = files['api.js'].decode('utf-8')
    assert "'LG webOS '" in api and "'Samsung Tizen '" not in api
    assert api.replace("'LG webOS '", "'Samsung Tizen '") == lf((SRC / 'api.js').read_bytes()).decode('utf-8')

    shim = files['webos-shim.js'].decode('utf-8')
    assert 'platformBack' in shim and 'getCapability' in shim
    assert 'var ' in shim and '=>' not in shim, 'overlay stays ES5 for old webOS engines'


def test_tizen_manifest_is_not_shipped_in_the_lg_package():
    files = ipk_files(belet_lg_client.build_ipk(mtime=1700000000))
    assert 'config.xml' not in files
    assert 'icon.png' == 'icon.png' and files['icon.png'] == (ROOT / 'assets' / 'belet_icon.png').read_bytes()


def test_committed_ipk_matches_sources():
    committed = ROOT / 'lg-belet' / belet_lg_client.PACKAGE_NAME
    assert committed.is_file(), 'run scripts/build_belet_lg_client.py'
    validate_ipk(committed.read_bytes())
    assert ipk_files(committed.read_bytes()) == ipk_files(belet_lg_client.build_ipk(mtime=1))


def test_api_exposes_lg_client_actions(tmp_path, monkeypatch):
    from core import Api

    monkeypatch.setenv('APPDATA', str(tmp_path))
    api = Api()
    try:
        ipk = api.belet_lg_client_ipk()
        validate_ipk(ipk)
        assert ipk_files(ipk)['appinfo.json'] == ipk_files(belet_lg_client.build_ipk(mtime=1))['appinfo.json']
        assert api.install_belet_lg_client()['disconnected'] is True
        assert api.launch_belet_lg_client()['disconnected'] is True
        assert api.remove_belet_lg_client()['disconnected'] is True
    finally:
        api.client = None
        api.close()


def test_ipk_builder_refuses_drifted_shared_sources(monkeypatch):
    original = belet_lg_client.API_PATCHES
    monkeypatch.setattr(belet_lg_client, 'API_PATCHES', ((b"'not in api.js'", b"'x'"),))
    with pytest.raises(ValueError):
        belet_lg_client.build_ipk()
    monkeypatch.setattr(belet_lg_client, 'API_PATCHES', original)


def test_webos_shim_runs_on_a_fake_webos_dom():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not available')
    result = subprocess.run([node, str(ROOT / 'tests/belet_lg_shim_mock.js')],
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'belet_lg_shim_mock: ok' in result.stdout


def test_qt_ui_wires_the_lg_client_actions():
    """The Windows build is the PyQt5 UI (app_legacy.py); the LG client must be reachable there."""
    import ast

    legacy = (ROOT / 'app_legacy.py').read_text(encoding='utf-8')
    tree = ast.parse(legacy)
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for handler, api_call in (
        ('install_lg_belet_client', 'install_belet_lg_client'),
        ('launch_lg_belet_client', 'launch_belet_lg_client'),
        ('remove_lg_belet_client', 'remove_belet_lg_client'),
    ):
        assert handler in methods, f'{handler} is missing from the Qt UI'
        called = {n.attr for n in ast.walk(methods[handler]) if isinstance(n, ast.Attribute)}
        assert api_call in called, f'{handler} must call Api.{api_call}'
        assert f'self.btn_belet_lg_{handler.split("_")[0]}.clicked.connect(self.{handler})' in legacy

    for widget in ('btn_belet_lg_install', 'btn_belet_lg_launch', 'btn_belet_lg_remove', 'belet_lg_hint'):
        assert legacy.count(f'self.{widget}') >= 2, f'{widget} must be created and translated'

    # read the strings without importing PyQt5 (the dependency is optional in this test env)
    ui_tree = ast.parse((ROOT / 'ui_qt.py').read_text(encoding='utf-8'))
    tr_node = next(n.value for n in ui_tree.body
                   if isinstance(n, ast.Assign) and any(getattr(t, 'id', None) == 'TR' for t in n.targets))
    TR = ast.literal_eval(tr_node)
    for lang in ('en', 'ru'):
        for key in ('belet_lg_hint', 'btn_belet_lg_install', 'btn_belet_lg_launch', 'btn_belet_lg_remove'):
            assert key in TR[lang], f'TR[{lang}][{key}] is missing'
        assert 'neonclient' in TR[lang]['belet_lg_hint']
