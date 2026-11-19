import io
import zipfile
from unittest.mock import MagicMock

import pytest
from core import Api
from local_tv_packages import validate_ipk, validate_wgt
from samsung_tizen import build_belet_wgt


def wgt_with_manifest(manifest, extra=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('config.xml', manifest)
        for name in extra:
            z.writestr(name, 'x')
    return buf.getvalue()


def test_valid_wgt_and_ipk():
    assert validate_wgt(build_belet_wgt()) == ('BeletTV001', 'BeletTV001.Belet')
    validate_ipk(Api().build_belet_ipk())
    with pytest.raises(ValueError): validate_ipk(b'not ipk')
    with pytest.raises(ValueError): validate_ipk(Api().build_belet_ipk()[:-15])


def test_reject_unsafe_and_invalid_wgt():
    with zipfile.ZipFile(io.BytesIO(build_belet_wgt())) as z:
        xml = z.read('config.xml')
    for data in [wgt_with_manifest(xml, ['../bad']), wgt_with_manifest(xml, ['a\\b']),
                 wgt_with_manifest(xml.replace(b'BeletTV001.Belet', b'wrong;rm')),
                 wgt_with_manifest(b'<!DOCTYPE widget [<!ENTITY x "a">]><widget/>'),
                 wgt_with_manifest(b'<!DOCTYPE widget [<!ENTITY x "a">]><widget/>'.decode().encode('utf-16')),
                 b'not zip']:
        with pytest.raises(ValueError): validate_wgt(data)


def test_samsung_installer_signs_and_checks_tv_status(tmp_path, monkeypatch):
    path = tmp_path / 'мой пакет.wgt'
    path.write_bytes(build_belet_wgt())
    calls = []
    class Sdb:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def capability(self): return {}
        def get_duid(self, **k): return 'device'
        def resolve_sdk_toolpath(self, caps): return '/tmp/sdk_tools'
        def push_bytes(self, data, path): calls.append(('push', data, path))
        def install_wgt(self, pkg, path, timeout):
            calls.append(('install', pkg, path))
            return 'install completed'
    bundle = dict(author_key='a', author_certs=['a'], dist_key='d', dist_certs=['d'])
    cert = MagicMock()
    cert.find_saved_samsung_certs.return_value = bundle
    api = Api()
    monkeypatch.setattr('core.SdbClient', Sdb)
    monkeypatch.setattr(api, '_samsung_cert_manager', lambda: cert)
    monkeypatch.setattr('core.sign_wgt_bytes', lambda data, *a: b'signed bytes')
    monkeypatch.setattr('core.verify_wgt_signatures', lambda data: calls.append(('verify', data)))
    res = api.install_local_samsung_wgt('192.168.1.3', str(path))
    assert res['ok'] and res['pkg_id'] == 'BeletTV001'
    assert calls == [('verify', b'signed bytes'), ('push', b'signed bytes', '/tmp/sdk_tools/BeletTV001.wgt'), ('install', 'BeletTV001', '/tmp/sdk_tools/BeletTV001.wgt')]
    monkeypatch.setattr(Sdb, 'install_wgt', lambda *a, **k: 'install failed[118, -12]')
    assert not api.install_local_samsung_wgt('192.168.1.3', str(path))['ok']
    cert.sign_in_and_provision.assert_not_called()


def test_lg_installer_sends_same_ipk_and_handles_failure(tmp_path, monkeypatch):
    api = Api()
    data = api.build_belet_ipk()
    path = tmp_path / 'приложение.ipk'
    path.write_bytes(data)
    uploaded = []
    class File(io.BytesIO):
        def close(self):
            uploaded.append(self.getvalue())
            super().close()
    class Sftp:
        def open(self, path, mode): return File()
        def remove(self, path): pass
        def close(self): pass
    class Client:
        def open_sftp(self): return Sftp()
    api.client = Client()
    api.remote_script = '/tmp/change_region.sh'
    commands = []
    monkeypatch.setattr(api, '_exec', lambda cmd, lines, timeout: commands.append(cmd) or 0)
    assert api.install_local_lg_ipk(str(path))['ok']
    assert uploaded == [data]
    assert 'appInstallService/dev/install' in commands[0] and 'приложение' not in commands[0]
    monkeypatch.setattr(api, '_exec', lambda *a, **k: 1)
    assert not api.install_local_lg_ipk(str(path))['ok']


def test_picker_cancel_and_no_connection():
    api = Api()
    assert not api.install_local_lg_ipk('/anything.ipk')['ok']
    api.window = MagicMock()
    api.open_dialog_type = 10
    api.window.create_file_dialog.return_value = None
    assert api.browse_tv_package('lg') == {'cancelled': True}
    assert api.browse_tv_package('bad').get('error')


def test_samsung_provisions_only_when_no_saved_cert(tmp_path, monkeypatch):
    path = tmp_path / 'app.wgt'
    path.write_bytes(build_belet_wgt())
    class Sdb:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def capability(self): return {}
        def get_duid(self, **k): return 'test-duid'
        def resolve_sdk_toolpath(self, caps): return '/tmp/sdk_tools'
        def push_bytes(self, *a): pass
        def install_wgt(self, *a, **k): return 'install completed'
    cert = MagicMock()
    cert.find_saved_samsung_certs.return_value = None
    cert.sign_in_and_provision.return_value = dict(author_key='a', author_certs=['a'], dist_key='d', dist_certs=['d'])
    api = Api()
    monkeypatch.setattr('core.SdbClient', Sdb)
    monkeypatch.setattr(api, '_samsung_cert_manager', lambda: cert)
    monkeypatch.setattr('core.sign_wgt_bytes', lambda data, *a: b'signed')
    monkeypatch.setattr('core.verify_wgt_signatures', lambda data: None)
    assert api.install_local_samsung_wgt('192.168.1.3', str(path))['ok']
    assert cert.sign_in_and_provision.call_args.args == ('test-duid',)


def test_interfaces_expose_local_installers():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    legacy = (root / 'app_legacy.py').read_text(encoding='utf-8')
    modern = (root / 'ui.html').read_text(encoding='utf-8')
    for label in ('ВЫБРАТЬ IPK И УСТАНОВИТЬ', 'ВЫБРАТЬ WGT И УСТАНОВИТЬ'):
        assert label in legacy and label in modern
    for name in ('install_local_lg_ipk', 'install_local_samsung_wgt'):
        assert name in legacy and name in modern
