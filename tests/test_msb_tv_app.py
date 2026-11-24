import io
import json
import re
import shutil
import subprocess
import tarfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import msb_callback
from core import Api

ROOT = Path(__file__).resolve().parent.parent


def members(ipk):
    pos = 8
    while pos < len(ipk):
        header = ipk[pos:pos + 60]
        size = int(header[48:58].decode().strip())
        name = header[:16].decode().strip().rstrip('/')
        data = ipk[pos + 60:pos + 60 + size]
        yield name, data
        pos += 60 + size + (size % 2)


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
    for elem_id in ('lanCard', 'lanIp', 'lanMask', 'lanGw', 'lanDns', 'lanMac',
                    'wifiCard', 'wifiIp', 'wifiMask', 'wifiGw', 'wifiDns', 'wifiMac', 'wifiSsid',
                    'refreshBtn'):
        assert f'id="{elem_id}"' in html
    assert 'gap:' not in html
    assert 'inset:' not in html
    lowered = html.lower()
    assert 'ремонт' not in lowered
    assert 'tv repair' not in lowered
    assert 'приём техники' not in lowered
    assert 'обращайтесь по номеру' in lowered and 'на сайт' in lowered


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
    assert b'http://developer.samsung.com/privilege/network.public' in files['config.xml']
    check_html(files['index.html'].decode('utf-8'), files)


def test_desktop_callback_pages_have_no_repair_copy():
    ui_html = (ROOT / 'ui.html').read_text(encoding='utf-8')
    m = re.search(r'<section[^>]*id="callbackPage".*?</section>', ui_html, re.S)
    assert m is not None
    cb_section = m.group(0).lower()
    assert 'ремонт' not in cb_section
    assert 'tv repair' not in cb_section
    assert 'приём техники' not in cb_section
    assert 'обращайтесь по номеру' in cb_section

    legacy = (ROOT / 'app_legacy.py').read_text(encoding='utf-8')
    idx = legacy.index('def _build_callback_page')
    block = legacy[idx:idx + 3500].lower()
    assert 'ремонт' not in block
    assert 'tv repair' not in block
    assert 'приём техники' not in block
    assert 'обращайтесь по номеру' in block


def test_tv_network_detection_for_webos_tizen_and_fallback():
    if not shutil.which('node'):
        return
    m = re.search(r'<script>([\s\S]*)</script>', msb_callback.HTML)
    assert m is not None
    script = m.group(1)

    harness = r'''
const assert = require('assert');
const vm = require('vm');
const SCRIPT = __SCRIPT__;

function makeDom(ids) {
  const nodes = {};
  const addedScripts = [];
  for (const id of ids) {
    nodes[id] = { id, textContent: '', className: '', onclick: null };
  }
  const head = {
    appendChild(el) {
      addedScripts.push(el);
    }
  };
  return {
    nodes,
    addedScripts,
    doc: {
      head,
      body: head,
      documentElement: head,
      getElementById(id) { return nodes[id] || null; },
      createElement(tag) { return { tag, src: '', onload: null, onerror: null }; },
      addEventListener() {}
    }
  };
}

const IDS = [
  'tvModel', 'netSummary', 'tvClock', 'netUpdated', 'refreshBtn',
  'lanCard', 'lanStatus', 'lanIp', 'lanMask', 'lanGw', 'lanDns', 'lanMac',
  'wifiCard', 'wifiStatus', 'wifiIp', 'wifiMask', 'wifiGw', 'wifiDns', 'wifiMac', 'wifiSsid'
];

// 1. LG webOS with simultaneous LAN + Wi-Fi + zero-flicker refresh check
(function testWebos() {
  const dom = makeDom(IDS);
  let intervalFn = null;
  let disconnectNext = false;
  function FakeBridge() { this.onservicecallback = null; }
  FakeBridge.prototype.call = function (uri) {
    const cb = this.onservicecallback;
    if (!cb) return;
    if (uri.indexOf('getStatus') >= 0) {
      cb(JSON.stringify({
        returnValue: true,
        wired: disconnectNext
          ? { state: 'disconnected' }
          : {
              state: 'connected',
              ipAddress: '192.168.10.42',
              netmask: '255.255.255.0',
              gateway: '192.168.10.1',
              dns1: '1.1.1.1',
              macAddress: 'aa:bb:cc:dd:ee:01'
            },
        wifi: {
          state: 'connected',
          ipAddress: '192.168.20.77',
          netmask: '255.255.255.0',
          gateway: '192.168.20.1',
          dns1: '8.8.8.8',
          dns2: '8.8.4.4',
          ssid: 'Meryosab-5G',
          macAddress: 'aa:bb:cc:dd:ee:02'
        }
      }));
    } else if (uri.indexOf('getSystemInfo') >= 0) {
      cb(JSON.stringify({ returnValue: true, modelName: 'OLED55C3' }));
    }
  };
  const win = {
    navigator: { userAgent: 'Web0S; Linux/SmartTV' },
    PalmSystem: { deviceInfo: JSON.stringify({ modelName: 'OLED55C3' }), platformBack() {} },
    PalmServiceBridge: FakeBridge,
    setInterval(fn) { intervalFn = fn; return 1; },
    setTimeout(fn) { return 1; }
  };
  win.window = win;
  win.document = dom.doc;
  vm.runInNewContext(SCRIPT, {
    window: win, document: dom.doc, navigator: win.navigator,
    setInterval: win.setInterval, setTimeout: win.setTimeout,
    Date, Math, JSON, String, RegExp
  });

  assert.strictEqual(dom.nodes.tvModel.textContent, 'LG OLED55C3');
  assert.strictEqual(dom.nodes.lanIp.textContent, '192.168.10.42');
  assert.strictEqual(dom.nodes.lanMask.textContent, '255.255.255.0');
  assert.strictEqual(dom.nodes.lanGw.textContent, '192.168.10.1');
  assert.strictEqual(dom.nodes.lanDns.textContent, '1.1.1.1');
  assert.strictEqual(dom.nodes.lanMac.textContent, 'AA:BB:CC:DD:EE:01');
  assert.strictEqual(dom.nodes.wifiIp.textContent, '192.168.20.77');
  assert.strictEqual(dom.nodes.wifiDns.textContent, '8.8.8.8, 8.8.4.4');
  assert.ok(dom.nodes.wifiSsid.textContent.includes('Meryosab-5G'));
  assert.ok(dom.nodes.netSummary.textContent.includes('LAN + WI-FI'));

  // Refresh clears unplugged wired adapter without breaking wifi
  disconnectNext = true;
  assert.ok(typeof intervalFn === 'function');
  intervalFn();
  assert.strictEqual(dom.nodes.lanIp.textContent, '—');
  assert.strictEqual(dom.nodes.wifiIp.textContent, '192.168.20.77');
})();

// 2. Samsung Tizen with systeminfo + dynamic $WEBAPIS loader + webapis.network
(function testTizen() {
  const dom = makeDom(IDS);
  const win = {
    navigator: { userAgent: 'Tizen 7.0 SmartTV' },
    tizen: {
      systeminfo: {
        getCapability() { return 'QN90C'; },
        getPropertyValue(prop, ok) {
          if (prop === 'ETHERNET_NETWORK') {
            ok({ status: 'CONNECTED', ipAddress: '10.0.0.15', macAddress: '11:22:33:44:55:66' });
          } else if (prop === 'WIFI_NETWORK') {
            ok({ status: 'CONNECTED', ipAddress: '192.168.1.88', ssid: 'Ashgabat-WiFi', macAddress: '66:55:44:33:22:11' });
          }
        }
      }
    },
    setInterval() { return 1; },
    setTimeout() { return 1; }
  };
  win.window = win;
  win.document = dom.doc;
  vm.runInNewContext(SCRIPT, {
    window: win, document: dom.doc, navigator: win.navigator,
    setInterval: win.setInterval, setTimeout: win.setTimeout,
    Date, Math, JSON, String, RegExp
  });

  assert.strictEqual(dom.addedScripts.length, 1);
  assert.strictEqual(dom.addedScripts[0].src, '$WEBAPIS/webapis/webapis.js');
  // Simulate $WEBAPIS/webapis/webapis.js completing load
  win.webapis = {
    productinfo: { getModel() { return 'QE65QN90C'; } },
    network: {
      getActiveConnectionType() { return 1; },
      getIp() { return '192.168.1.88'; },
      getSubnetMask() { return '255.255.255.0'; },
      getGateway() { return '192.168.1.1'; },
      getDns() { return '9.9.9.9'; },
      getMac() { return '66:55:44:33:22:11'; },
      getWiFiSsid() { return 'Ashgabat-WiFi'; }
    }
  };
  dom.addedScripts[0].onload();

  assert.strictEqual(dom.nodes.tvModel.textContent, 'Samsung QE65QN90C');
  assert.strictEqual(dom.nodes.lanIp.textContent, '10.0.0.15');
  assert.strictEqual(dom.nodes.wifiIp.textContent, '192.168.1.88');
  assert.strictEqual(dom.nodes.wifiGw.textContent, '192.168.1.1');
  assert.strictEqual(dom.nodes.wifiDns.textContent, '9.9.9.9');
  assert.ok(dom.nodes.wifiSsid.textContent.includes('Ashgabat-WiFi'));
})();

// 3. Generic browser / WebRTC local IP fallback
(function testWebRtcFallback() {
  const dom = makeDom(IDS);
  function FakePC() {}
  FakePC.prototype.createDataChannel = function () {};
  FakePC.prototype.createOffer = function () {
    return {
      then: (fn) => {
        fn({ sdp: '' });
        if (this.onicecandidate) {
          this.onicecandidate({ candidate: { candidate: 'candidate:1 1 udp 2122260223 192.168.50.9 54321 typ host' } });
        }
        return { ['catch']: () => {} };
      }
    };
  };
  FakePC.prototype.setLocalDescription = function () {};
  FakePC.prototype.close = function () {};
  const win = {
    navigator: { userAgent: 'Mozilla/5.0' },
    RTCPeerConnection: FakePC,
    setInterval() { return 1; },
    setTimeout() { return 1; }
  };
  win.window = win;
  win.document = dom.doc;
  vm.runInNewContext(SCRIPT, {
    window: win, document: dom.doc, navigator: win.navigator,
    setInterval: win.setInterval, setTimeout: win.setTimeout,
    Date, Math, JSON, String, RegExp
  });

  assert.ok(dom.nodes.netSummary.textContent.includes('192.168.50.9'));
  assert.ok(dom.nodes.netUpdated.textContent.includes('192.168.50.9'));
})();
// 4. Hisense / Toshiba VIDAA global functions (Hisense_Get*)
(function testHisenseVidaa() {
  const dom = makeDom(IDS);
  const win = {
    navigator: { userAgent: 'Mozilla/5.0 (Linux; VIDAA) Hisense-TV/9.0' },
    Hisense_GetBrand() { return 'Hisense'; },
    Hisense_GetModelName() { return '55U7HQ'; },
    Hisense_GetNetWorkType() { return 1; },
    Hisense_GetIpAddress() { return '192.168.1.99'; },
    Hisense_GetMacAddress() { return 'aa:11:22:33:44:55'; },
    Hisense_GetWifiSSID() { return 'Home-5G'; },
    setInterval() { return 1; },
    setTimeout() { return 1; }
  };
  win.window = win;
  win.document = dom.doc;
  vm.runInNewContext(SCRIPT, {
    window: win, document: dom.doc, navigator: win.navigator,
    setInterval: win.setInterval, setTimeout: win.setTimeout,
    Date, Math, JSON, String, RegExp
  });
  assert.strictEqual(dom.nodes.tvModel.textContent, 'Hisense 55U7HQ');
  assert.strictEqual(dom.nodes.wifiIp.textContent, '192.168.1.99');
  assert.strictEqual(dom.nodes.wifiMac.textContent, 'AA:11:22:33:44:55');
  assert.ok(dom.nodes.wifiSsid.textContent.includes('Home-5G'));
})();
console.log('msb_callback_tv_ok');
'''.replace('__SCRIPT__', json.dumps(script))

    proc = subprocess.run(['node', '-e', harness], capture_output=True, text=True, check=True)
    assert 'msb_callback_tv_ok' in proc.stdout


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


def test_hisense_callback_data_url_and_desktop_buttons():
    html = msb_callback.build_hisense_html(inline_assets=True)
    assert 'data:image/png;base64,' in html
    assert 'probeHisense' in html
    assert 'fetch(' not in html
    data_url = msb_callback.build_hisense_data_url()
    assert data_url.startswith('data:text/html;charset=utf-8;base64,')
    import base64
    decoded = base64.b64decode(data_url.split(',', 1)[1]).decode('utf-8')
    assert 'Meryosab Callback' in decoded
    assert 'data:image/png;base64,' in decoded

    ui_html = (ROOT / 'ui.html').read_text(encoding='utf-8')
    assert 'installCallbackHisense()' in ui_html
    assert 'УСТАНОВИТЬ НА HISENSE' in ui_html
    legacy = (ROOT / 'app_legacy.py').read_text(encoding='utf-8')
    assert 'УСТАНОВИТЬ НА HISENSE' in legacy
    assert '_install_callback_hisense' in legacy
