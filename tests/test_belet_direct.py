import io
import zipfile
import xml.etree.ElementTree as ET

import belet_direct
from core import Api


def test_direct_wgt_uses_official_site_without_proxy_or_iframe():
    with zipfile.ZipFile(io.BytesIO(belet_direct.build_wgt())) as z:
        assert set(z.namelist()) == {'config.xml', 'index.html', 'icon.png', 'style.css', 'boot.js'}
        cfg = ET.fromstring(z.read('config.xml'))
        html = z.read('index.html').decode('utf-8')
        boot = z.read('boot.js').decode('utf-8')
    assert cfg.find('{http://tizen.org/ns/widgets}application').attrib['id'] == belet_direct.APP
    assert cfg.find('{http://tizen.org/ns/widgets}allow-navigation').text == '*'
    assert "window.location.replace('https://belet.tm/')" in boot
    assert '<script src="boot.js"></script>' in html
    assert cfg.attrib['version'] == '1.0.1'
    assert cfg.find('{http://tizen.org/ns/widgets}content-security-policy') is not None
    assert 'iframe' not in html and 'localhost' not in html and '127.0.0.1' not in html


def test_samsung_direct_installer_targets_distinct_package(monkeypatch):
    class Sdb:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def capability(self): return {'platform_version': '7.0'}
        def get_duid(self, **kwargs): return 'test'
        def resolve_sdk_toolpath(self, caps): return '/tmp'
        def push_bytes(self, data, path):
            assert path.endswith('/' + belet_direct.PACKAGE + '.wgt')
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                assert 'service/proxy.js' not in z.namelist()
        def install_wgt(self, pkg, path, timeout):
            assert pkg == belet_direct.PACKAGE
            return 'installed'
        def list_apps(self): return belet_direct.PACKAGE
        def launch_app(self, app):
            assert app == belet_direct.APP
            return 'launched'
    class Cert:
        def find_saved_samsung_certs(self, duid):
            return {'source': 'saved', 'author_key': None, 'author_certs': [], 'dist_key': None, 'dist_certs': []}
    api = Api()
    monkeypatch.setattr('core.SdbClient', Sdb)
    monkeypatch.setattr('core._tizen_local_ip', lambda ip: '192.168.1.2')
    monkeypatch.setattr(api, '_samsung_cert_manager', lambda: Cert())
    monkeypatch.setattr('core.sign_wgt_bytes', lambda data, *args: data)
    monkeypatch.setattr('core.verify_wgt_signatures', lambda data: None)
    monkeypatch.setattr('core.diagnose_install_output', lambda out, pkg: {'status': 'ok'})
    result = api.install_samsung_belet_direct('192.168.1.3')
    assert result['ok']
    assert result['pkg_id'] == belet_direct.PACKAGE
    assert result['app_id'] == belet_direct.APP


def test_direct_boot_runs_and_retry_uses_same_url(tmp_path):
    import json
    import shutil
    import subprocess
    import pytest
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js not installed')
    with zipfile.ZipFile(io.BytesIO(belet_direct.build_wgt())) as z:
        boot = z.read('boot.js').decode('utf-8')
    runner = r'''
const vm=require('vm'),assert=require('assert');
const urls=[], timers=[];
const button={focus(){this.focused=true}}, status={textContent:''};
const sandbox={window:{location:{replace(url){urls.push(url)}}}, document:{getElementById(id){return id==='retry'?button:status}},setTimeout(fn,delay){timers.push({fn,delay})}};
vm.runInNewContext(SCRIPT,sandbox);
assert(button.focused);assert.strictEqual(typeof button.onclick,'function');
timers.find(t=>t.delay===100).fn();button.onclick();
assert.deepStrictEqual(urls,['https://belet.tm/','https://belet.tm/']);
'''.replace('SCRIPT', json.dumps(boot))
    script = tmp_path / 'run.js'
    script.write_text(runner, encoding='utf-8')
    p = subprocess.run([node, str(script)], capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert p.returncode == 0, p.stderr
