import io
import json
import tarfile
import zipfile
from html.parser import HTMLParser
from unittest.mock import patch

import msb_callback
from core import Api
from test_belet_experimental import members


class Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []
    def handle_starttag(self, tag, attrs):
        self.refs.extend(value for name, value in attrs if name in ('src', 'href'))


def check_html(html, files):
    parser = Resources()
    parser.feed(html)
    assert set(parser.refs) == {'website.png', 'contact.png'}
    assert set(parser.refs) <= set(files)
    assert '+993 63 70 59 53' in html
    assert 'tvrepairmsb@gmail.com' in html
    assert 'fetch(' not in html and 'XMLHttpRequest' not in html


def test_offline_lg_and_samsung_packages():
    archive = dict(members(msb_callback.build_ipk()))
    with tarfile.open(fileobj=io.BytesIO(archive['data.tar.gz']), mode='r:gz') as tar:
        prefix = 'usr/palm/applications/' + msb_callback.LG_ID + '/'
        files = {x.name[len(prefix):]: tar.extractfile(x).read() for x in tar if x.isfile() and x.name.startswith(prefix)}
    info = json.loads(files['appinfo.json'])
    assert info['id'] == msb_callback.LG_ID
    check_html(files['index.html'].decode('utf-8'), files)
    with zipfile.ZipFile(io.BytesIO(msb_callback.build_wgt())) as z:
        files = {name: z.read(name) for name in z.namelist()}
    assert msb_callback.SAMSUNG_APP_ID.encode() in files['config.xml']
    assert b'privilege/internet' not in files['config.xml']
    check_html(files['index.html'].decode('utf-8'), files)


def test_lg_installer_uses_own_package():
    class Sftp:
        def put(self, src, dst): self.dst = dst
        def close(self): pass
    class Client:
        def open_sftp(self): return Sftp()
    api = Api()
    api.client = Client()
    api.remote_script = '/tmp/change_region.sh'
    with patch.object(api, '_exec', return_value=0):
        result = api.install_lg_callback()
    assert result['ok']
    assert 'MSB Callback' in result['output']
    assert msb_callback.LG_ID in result['output']


def test_samsung_installer_uses_own_package(monkeypatch):
    class FakeSdb:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def capability(self): return {'platform_version': '7.0'}
        def get_duid(self, **kwargs): return 'test-duid'
        def resolve_sdk_toolpath(self, caps): return '/home/owner/share/tmp/sdk_tools'
        def push_bytes(self, data, path): assert msb_callback.SAMSUNG_PKG_ID in path
        def install_wgt(self, pkg, path, timeout):
            assert pkg == msb_callback.SAMSUNG_PKG_ID
            return 'installed'
        def list_apps(self): return msb_callback.SAMSUNG_PKG_ID
        def launch_app(self, app):
            assert app == msb_callback.SAMSUNG_APP_ID
            return 'launched'
    class Certs:
        def find_saved_samsung_certs(self, duid):
            return {'source': 'saved', 'author_key': None, 'author_certs': [], 'dist_key': None, 'dist_certs': []}
    api = Api()
    monkeypatch.setattr('core.SdbClient', FakeSdb)
    monkeypatch.setattr('core._tizen_local_ip', lambda ip: '192.168.1.2')
    monkeypatch.setattr(api, '_samsung_cert_manager', lambda: Certs())
    monkeypatch.setattr('core.sign_wgt_bytes', lambda data, *args: data)
    monkeypatch.setattr('core.verify_wgt_signatures', lambda data: None)
    monkeypatch.setattr('core.diagnose_install_output', lambda out, pkg: {'status': 'ok'})
    result = api.install_samsung_callback('192.168.1.3')
    assert result['ok']
    assert result['app_id'] == msb_callback.SAMSUNG_APP_ID
