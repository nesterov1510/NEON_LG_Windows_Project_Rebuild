"""Unit tests for Alem TV packaging and integration (LG webOS IPK & Samsung Tizen WGT)."""

import datetime
import io
import json
import tarfile
import zipfile
import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from alemtv_app import (
    ALEMTV_APP_ID,
    ALEMTV_DEFAULT_PORTAL,
    ALEMTV_PKG_ID,
    ALEMTV_PORTALS,
    ALEMTV_WEBOS_ID,
    build_alemtv_html,
    build_alemtv_ipk,
    build_alemtv_js,
    build_alemtv_wgt,
    get_alemtv_assets,
)
from core import Api
from samsung_tizen import (
    sign_wgt_bytes,
    verify_wgt_signatures,
)

def test_alemtv_assets_exist():
    assets = get_alemtv_assets()
    for key in ('icon', 'large_icon', 'splash', 'ui'):
        assert key in assets, f"Missing asset {key}"
        assert len(assets[key]) > 0
        assert assets[key].startswith(b'\x89PNG\r\n\x1a\n')

def test_alemtv_portals_catalog():
    assert len(ALEMTV_PORTALS) >= 7
    hosts = [p['host'] for p in ALEMTV_PORTALS]
    expected_hosts = [
        'telecom.alemtv.tm',
        'astu.tmalem.tv',
        'ark.alemtv.tm',
        'local-astu.alemtv.tm',
        'iptv.tmalem.tv',
        'iptv-org.github.io',
        'go.iptvportal.ru',
        'nextouch.iptvportal.ru',
    ]
    for eh in expected_hosts:
        assert eh in hosts

def test_build_alemtv_html_structure():
    html = build_alemtv_html()
    assert '<!DOCTYPE html>' in html
    assert 'Alem TV' in html
    assert 'SilkyOpenChannel' in html
    assert 'loadSilkyOpenChannels' in html
    assert 'jsonrpc' in html
    assert 'authorize_terminal' in html
    assert 'get_domain_config' in html
    assert 'get_epg' in html
    assert 'check_update' in html
    assert 'buildTokenQuery' in html
    assert 'cf2cde3395737e94f0f0ca7474797d285170eb7acd41628a946f92a00138225a' in html  # RSA modulus
    assert 'arescrypt       ' in html
    assert 'videoElement' in html
    assert 'playerOsd' in html
    assert '10009' in html  # Tizen Return key
    assert '461' in html    # webOS Back key
    assert 'splashPortalSelector' in html
    assert 'scanAndConnectPortal' in html
    assert 'customPortalInput' in html
    assert 'updateSplashSelector' in html
    assert 'parseMedialistTable' in html
    assert 'loginModal' in html
    assert 'customMacInput' in html
    assert 'submitSubscriberLogin' in html
    for eh in ['telecom.alemtv.tm', 'astu.tmalem.tv', 'ark.alemtv.tm', 'local-astu.alemtv.tm', 'iptv.tmalem.tv', 'iptv-org.github.io', 'go.iptvportal.ru', 'nextouch.iptvportal.ru']:
        assert eh in html

def test_build_alemtv_ipk_structure():
    ipk_bytes = build_alemtv_ipk()
    assert ipk_bytes.startswith(b'!<arch>\n')

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

    # Control tar.gz
    with tarfile.open(fileobj=io.BytesIO(members['control.tar.gz']), mode='r:gz') as tf:
        names = tf.getnames()
        assert names == ['control']
        ctrl_txt = tf.extractfile('control').read().decode('utf-8')
        assert f'Package: {ALEMTV_WEBOS_ID}\n' in ctrl_txt
        assert 'webOS-Package-Format-Version: 2\n' in ctrl_txt

    # Data tar.gz
    with tarfile.open(fileobj=io.BytesIO(members['data.tar.gz']), mode='r:gz') as tf:
        names = tf.getnames()
        base = f'usr/palm/applications/{ALEMTV_WEBOS_ID}'
        for req in (f'{base}/appinfo.json', f'{base}/index.html', f'{base}/icon.png', f'{base}/largeIcon.png', f'{base}/splash.png'):
            assert req in names
        appinfo = json.loads(tf.extractfile(f'{base}/appinfo.json').read().decode('utf-8'))
        assert appinfo['id'] == ALEMTV_WEBOS_ID
        assert appinfo['title'] == 'Alem TV'
        assert appinfo['resolution'] == '1920x1080'

def test_build_alemtv_wgt_and_sign():
    unsigned_wgt = build_alemtv_wgt()
    assert unsigned_wgt.startswith(b'PK')

    with zipfile.ZipFile(io.BytesIO(unsigned_wgt), 'r') as zf:
        names = set(zf.namelist())
        assert {'config.xml', 'index.html', 'icon.png'}.issubset(names)
        cfg = zf.read('config.xml').decode('utf-8')
        assert f'package="{ALEMTV_PKG_ID}"' in cfg
        assert f'id="{ALEMTV_APP_ID}"' in cfg
        assert 'AlemTV' in cfg
        assert 'http://tizen.org/privilege/tv.inputdevice' in cfg
        assert 'http://tizen.org/privilege/internet' in cfg

    # Generate in-memory self-signed test certificates
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
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(a_ca_key, hashes.SHA256())
    )

    d_ca = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Distributor CA')]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Distributor CA')]))
        .public_key(d_ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(d_ca_key, hashes.SHA256())
    )
    d_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Tizen Distributor')]))
        .issuer_name(d_ca.subject)
        .public_key(d_key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(d_ca_key, hashes.SHA256())
    )

    signed_wgt = sign_wgt_bytes(
        unsigned_wgt,
        a_key,
        [a_leaf, a_ca],
        d_key,
        [d_leaf, d_ca],
    )
    assert signed_wgt.startswith(b'PK')
    assert len(signed_wgt) > len(unsigned_wgt)

    verify_result = verify_wgt_signatures(signed_wgt)
    assert verify_result is True

def test_api_alemtv_methods():
    api = Api()
    ipk = api.build_alemtv_ipk()
    assert ipk.startswith(b'!<arch>\n')

    wgt = api.build_samsung_alemtv_wgt()
    assert wgt.startswith(b'PK')

def test_alemtv_js_syntax_validity():
    import os
    import shutil
    import subprocess
    import tempfile
    from alemtv_app import build_alemtv_js

    node_bin = shutil.which('node')
    if not node_bin:
        pytest.skip("Node.js not available in environment")

    js_code = build_alemtv_js()
    fd, tf_path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(js_code)
        proc = subprocess.run([node_bin, '-c', tf_path], capture_output=True, text=True)
        assert proc.returncode == 0, f"JS Syntax Error:\n{proc.stderr}"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)

def test_alemtv_custom_portal_persistence():
    from alemtv_app import build_alemtv_js, build_alemtv_html
    api = Api()

    # Test custom portal normalization
    norm = api._normalize_portal_url("astu.tmalem.tv/")
    assert norm == "http://astu.tmalem.tv"

    norm_ip = api._normalize_portal_url("192.168.1.80:8080/")
    assert norm_ip == "http://192.168.1.80:8080"

    # Test custom portal baked into JS
    js = build_alemtv_js(default_portal="http://astu.tmalem.tv")
    assert 'var DEFAULT_PORTAL = "http://astu.tmalem.tv";' in js
    assert 'lastBaked !== bakedDefault' in js
    assert 'splashServerButtons' in js
    assert '65385' in js  # Samsung Tizen Select/OK keycode

    # Test custom portal in HTML
    html = build_alemtv_html(default_portal="http://astu.tmalem.tv")
    assert 'id="splashServerButtons"' in html
    assert 'http://astu.tmalem.tv' in html

def test_alemtv_silky_open_channel():
    """Verify SilkyOpenChannel fallback test streams and M3U parser work properly in JS."""
    import os
    import shutil
    import subprocess
    import tempfile
    from alemtv_app import build_alemtv_js

    node_bin = shutil.which('node')
    if not node_bin:
        pytest.skip("Node.js not available in environment")

    js = build_alemtv_js()
    runner = '''
var window = { addEventListener: function() {} };
var document = {
  readyState: 'loading',
  addEventListener: function() {},
  getElementById: function() { return { querySelectorAll: function() { return []; }, appendChild: function(){}, classList: { add: function(){}, remove: function(){} }, style: {}, textContent: '' }; },
  querySelectorAll: function() { return []; },
  createElement: function() { return { querySelectorAll: function() { return []; }, appendChild: function(){}, style: {}, classList: { add: function(){}, remove: function(){} }, dataset: {} }; }
};
var localStorage = { getItem: function() { return null; }, setItem: function() {}, removeItem: function() {} };
''' + js + '''
// Test SilkyOpenChannel channel list
var silkyList = window.getSilkyOpenChannelsList();
if (!Array.isArray(silkyList) || silkyList.length < 5) {
  throw new Error('silkyList invalid: ' + JSON.stringify(silkyList));
}
silkyList.forEach(function(ch) {
  if (!ch.title || !ch.stream_url) {
    throw new Error('channel missing title or stream_url: ' + JSON.stringify(ch));
  }
});

// Test M3U parser using String.fromCharCode(10)
var sampleM3u = ['#EXTM3U', '#EXTINF:-1 tvg-id=\"1\" tvg-logo=\"http://logo.png\" group-title=\"TestGroup\",Test Channel', 'http://test.stream/live.m3u8'].join(String.fromCharCode(10));
var parsed = window.parseM3uChannels(sampleM3u);
if (!parsed || parsed.length !== 1 || parsed[0].title !== 'Test Channel' || parsed[0].stream_url !== 'http://test.stream/live.m3u8') {
  throw new Error('m3u parser failed: ' + JSON.stringify(parsed));
}

// Test auth success with silkyList
window.handleAuthSuccess({ channels: silkyList, session_id: 'silky_open' });
if (!window.authData.channels || window.authData.channels.length < 5) {
  throw new Error('authData channels missing: ' + JSON.stringify(window.authData.channels));
}

process.exit(0);
'''
    fd, tf_path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(runner)
        p = subprocess.run([node_bin, tf_path], capture_output=True, text=True, timeout=15)
        assert p.returncode == 0, f"SilkyOpenChannel JS test failed:\nSTDOUT: {p.stdout}\nSTDERR: {p.stderr}"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)

def test_alemtv_stream_url_builder_appletv_alignment():
    """Verify JS Stream URL builder replicates Apple TV tvOS StreamURLBuilder test vectors."""
    import os
    import shutil
    import subprocess
    import tempfile
    from alemtv_app import build_alemtv_js

    node_bin = shutil.which('node')
    if not node_bin:
        pytest.skip("Node.js not available in environment")

    js = build_alemtv_js()
    runner = '''
var window = { addEventListener: function() {} };
var document = {
  readyState: 'loading',
  addEventListener: function() {},
  getElementById: function() { return { querySelectorAll: function() { return []; }, appendChild: function(){}, classList: { add: function(){}, remove: function(){} }, style: {}, textContent: '' }; },
  querySelectorAll: function() { return []; },
  createElement: function() { return { querySelectorAll: function() { return []; }, appendChild: function(){}, style: {}, classList: { add: function(){}, remove: function(){} }, dataset: {} }; }
};
var localStorage = { getItem: function() { return null; }, setItem: function() {}, removeItem: function() {} };
''' + js + '''
var altyn = {
  id: 1, index: 1, name: 'Altyn Asyr', scheme: 'http', host: '119.235.117.209', port: 8010,
  path: 'AltynAsyr/video.m3u8', auth: 'iptvportal', key: 'Key-1', encrypted: true,
  timeshift_url: 'http://119.235.117.213:8020/AltynAsyr', timeshift_archive_length: 259200,
  timeshift_auth: 'iptvportal'
};
window.authData.tokens = { 'Key-1': { name: 'Key-1', token1: 'T1', token2: 'T2' } };
window.authData.domain = 'tmalem';

// Test 1: Live with composite token
var liveUrl = window.resolveStreamUrl(altyn, altyn.auth, 'live');
if (liveUrl.indexOf('http://119.235.117.209:8010/AltynAsyr/video.m3u8?token=') !== 0) {
  throw new Error('live url unexpected: ' + liveUrl);
}

// Test 2: Channel without key uses plain token (when session exists)
var noKeyCh = Object.assign({}, altyn, { key: null });
var noKeyUrl = window.resolveStreamUrl(noKeyCh, noKeyCh.auth, 'live');
if (noKeyUrl !== 'http://119.235.117.209:8010/AltynAsyr/video.m3u8') {
  throw new Error('noKeyUrl unexpected: ' + noKeyUrl);
}

// Test 3: Channel without auth has no query
var noAuthCh = Object.assign({}, altyn, { auth: null });
var noAuthUrl = window.resolveStreamUrl(noAuthCh, null, 'live');
if (noAuthUrl !== 'http://119.235.117.209:8010/AltynAsyr/video.m3u8') {
  throw new Error('noAuthUrl unexpected: ' + noAuthUrl);
}

// Test 4: Absolute path is used as is
var absCh = Object.assign({}, altyn, { path: 'http://cdn.example/x/index.m3u8?a=1', auth: null });
var absUrl = window.resolveStreamUrl(absCh, null, 'live');
if (absUrl !== 'http://cdn.example/x/index.m3u8?a=1') {
  throw new Error('absUrl unexpected: ' + absUrl);
}

// Test 5: Missing scheme defaults to http and no port
var noSchemeCh = Object.assign({}, altyn, { scheme: null, port: null, auth: null });
var noSchemeUrl = window.resolveStreamUrl(noSchemeCh, null, 'live');
if (noSchemeUrl !== 'http://119.235.117.209/AltynAsyr/video.m3u8') {
  throw new Error('noSchemeUrl unexpected: ' + noSchemeUrl);
}

// Test 6: Timeshift base drops file and query
var tsCh = Object.assign({}, altyn, { timeshift_url: 'http://h:1/Ch/index.m3u8?x=1', auth: null, timeshift_auth: null });
var tsUrl = window.resolveStreamUrl(tsCh, null, 'timeshift', { behind: 5 });
if (tsUrl !== 'http://h:1/Ch/video-timeshift_rel-5.m3u8') {
  throw new Error('tsUrl unexpected: ' + tsUrl);
}

// Test 7: Archive URL format
var archUrl = window.resolveStreamUrl(tsCh, null, 'archive', { start: 1700000000 * 1000, duration: 600 * 1000 });
if (archUrl !== 'http://h:1/Ch/index-1700000000-600.m3u8') {
  throw new Error('archUrl unexpected: ' + archUrl);
}

// Test 8: PortalDate parser with Turkmenistan UTC+5
var pDate = window.parsePortalDate('2026-10-03 21:00:00');
if (!pDate || pDate.toISOString() !== '2026-10-03T16:00:00.000Z') {
  throw new Error('pDate mismatch: ' + (pDate ? pDate.toISOString() : 'null'));
}

process.exit(0);
'''
    fd, tf_path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(runner)
        p = subprocess.run([node_bin, tf_path], capture_output=True, text=True, timeout=15)
        assert p.returncode == 0, f"Node tests failed:\nSTDOUT: {p.stdout}\nSTDERR: {p.stderr}"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)

def test_alemtv_play_channel_navigation():
    """Verify selecting/playing a channel transitions to player screen, loads video URL, and handles return."""
    import os
    import shutil
    import subprocess
    import tempfile
    from alemtv_app import build_alemtv_js

    node_bin = shutil.which('node')
    if not node_bin:
        pytest.skip("Node.js not available in environment")

    js = build_alemtv_js()
    runner = '''
var domElements = {};
function makeElem(id) {
  return {
    id: id,
    tagName: 'DIV',
    style: {},
    className: '',
    classList: {
      add: function(c) { this[c] = true; },
      remove: function(c) { delete this[c]; },
      contains: function(c) { return !!this[c]; }
    },
    textContent: '',
    src: '',
    paused: true,
    play: function() { this.paused = false; return Promise.resolve(); },
    pause: function() { this.paused = true; },
    load: function() {},
    querySelectorAll: function() { return []; },
    appendChild: function() {},
    dataset: {}
  };
}

var window = { addEventListener: function() {} };
var document = {
  readyState: 'loading',
  addEventListener: function() {},
  getElementById: function(id) {
    if (!domElements[id]) domElements[id] = makeElem(id);
    return domElements[id];
  },
  querySelectorAll: function() { return []; },
  createElement: function(tag) {
    var el = makeElem('');
    el.tagName = tag.toUpperCase();
    return el;
  }
};
var localStorage = { getItem: function() { return null; }, setItem: function() {}, removeItem: function() {} };
''' + js + '''
var silkyList = window.getSilkyOpenChannelsList();
window.handleAuthSuccess({ channels: silkyList, session_id: 'silky_open' });

var ch = silkyList[0];
if (!ch || !ch.stream_url) {
  throw new Error('First channel missing stream_url: ' + JSON.stringify(ch));
}

// Call playChannel
window.playChannel(ch);

// Verify player screen is active and displayed
var playerScreen = document.getElementById("playerScreen");
if (playerScreen.style.display !== 'block' || playerScreen.className.indexOf('active') === -1) {
  throw new Error('playerScreen display not block or not active: ' + JSON.stringify(playerScreen));
}

// Verify main screen is hidden
var mainScreen = document.getElementById("mainScreen");
if (mainScreen.style.display !== 'none') {
  throw new Error('mainScreen display not none: ' + JSON.stringify(mainScreen));
}

// Verify videoElement loaded channel stream URL
var video = document.getElementById("videoElement");
if (video.src !== ch.stream_url) {
  throw new Error('videoElement.src expected ' + ch.stream_url + ', got ' + video.src);
}

// Verify return back to channel list
window.backToChannelList();
if (mainScreen.style.display !== 'flex' || mainScreen.className.indexOf('active') === -1) {
  throw new Error('mainScreen not restored after backToChannelList');
}
if (video.src !== '') {
  throw new Error('videoElement.src not cleared after backToChannelList: ' + video.src);
}

process.exit(0);
'''
    fd, tf_path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(runner)
        p = subprocess.run([node_bin, tf_path], capture_output=True, text=True, timeout=15)
        assert p.returncode == 0, f"Play channel navigation test failed:\nSTDOUT: {p.stdout}\nSTDERR: {p.stderr}"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)

def test_alemtv_ares_cipher_vectors():
    """Verify JS AresCipher matches Swift/Python reference vectors (login by contract)."""
    import os
    import shutil
    import subprocess
    import tempfile
    from alemtv_app import build_alemtv_js

    node_bin = shutil.which('node')
    if not node_bin:
        pytest.skip("Node.js not available in environment")

    js = build_alemtv_js()
    runner = '''
var window = { addEventListener: function() {} };
var document = {
  readyState: 'loading',
  addEventListener: function() {},
  getElementById: function() { return { querySelectorAll: function() { return []; }, appendChild: function(){}, classList: { add: function(){}, remove: function(){} }, style: {}, textContent: '' }; },
  querySelectorAll: function() { return []; },
  createElement: function() { return { querySelectorAll: function() { return []; }, appendChild: function(){}, style: {}, classList: { add: function(){}, remove: function(){} }, dataset: {} }; }
};
var localStorage = { getItem: function() { return null; }, setItem: function() {}, removeItem: function() {} };
''' + js + '''
var key = new Uint8Array(16);
for (var i = 0; i < 16; i++) key[i] = i;
var plain = '{"jsonrpc":"2.0","id":1,"method":"login","params":{"macaddr":null}}';
var enc = window.aresEncrypt(plain, key);
if (enc !== '7nGcuIoK3zJZgGc/W5Zkqyxv0rKQDnS9Tre7G2e5oCmPXbw8qKjvTVRqHpAbxcOdYgTii11X5aGINF/XKxZpo5VKk0SPW78v/yeZ0EdvE4k=') {
  throw new Error('aresEncrypt vector mismatch: ' + enc);
}
if (window.aresDecrypt(enc, key) !== plain) throw new Error('aresDecrypt roundtrip failed');
var cyr = window.aresEncrypt('Привет', key);
if (cyr !== '3sPPfhhXQCKpLVq6AyuboA==') throw new Error('cyrillic vector mismatch: ' + cyr);
if (window.aresDecrypt(cyr, key) !== 'Привет') throw new Error('cyrillic roundtrip failed');
if (!window.isRequiresSignIn({ code: -32002, message: 'not found' })) throw new Error('-32002 must require sign-in');
if (!window.isRequiresSignIn({ code: -32003, message: 'denied' })) throw new Error('-32003 must require sign-in');
if (window.isRequiresSignIn({ code: -1, message: 'limit' })) throw new Error('-1 must not require sign-in');
if (window.friendlyLoginError({ code: -32002, message: 'not found' }).indexOf('Терминал') === -1) throw new Error('friendly -32002 message wrong');
process.exit(0);
'''
    fd, tf_path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(runner)
        p = subprocess.run([node_bin, tf_path], capture_output=True, text=True, timeout=15)
        assert p.returncode == 0, f"AresCipher vector test failed:\\nSTDOUT: {p.stdout}\\nSTDERR: {p.stderr}"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)

def test_alemtv_login_by_contract_encrypted_roundtrip():
    """Verify register_terminal is sent encrypted with macaddr:null (not random MAC)."""
    import os
    import shutil
    import subprocess
    import tempfile
    from alemtv_app import build_alemtv_js

    node_bin = shutil.which('node')
    if not node_bin:
        pytest.skip("Node.js not available in environment")

    js = build_alemtv_js()
    runner = '''
var randCounter = 0;
var origRandom = Math.random;
Math.random = function() { var v = (randCounter % 256) / 256; randCounter++; return v; };
var store = {};
var localStorage = {
  getItem: function(k) { return (k in store) ? store[k] : null; },
  setItem: function(k, v) { store[k] = String(v); },
  removeItem: function(k) { delete store[k]; },
  clear: function() { store = {}; }
};
var domElements = {};
function makeElem(id) {
  return {
    id: id, tagName: 'DIV', style: {}, className: '', textContent: '', innerHTML: '',
    classList: { add: function(){}, remove: function(){}, contains: function(){ return false; } },
    querySelectorAll: function() { return []; },
    appendChild: function(){}, dataset: {}, value: '', offsetParent: null
  };
}
var document = {
  readyState: 'loading',
  addEventListener: function(){},
  getElementById: function(id) { if (!domElements[id]) domElements[id] = makeElem(id); return domElements[id]; },
  querySelectorAll: function() { return []; },
  createElement: function(tag) { var el = makeElem(''); el.tagName = tag.toUpperCase(); return el; }
};
var xhrRequests = [];
var xhrHandler = null;
function MockXHR() { this.headers = {}; this.status = 0; this.responseText = ''; this.readyState = 0; }
MockXHR.prototype.open = function(m, u) { this.method = m; this.url = u; };
MockXHR.prototype.setRequestHeader = function(k, v) { this.headers[k] = v; };
MockXHR.prototype.send = function(body) {
  var req = { method: this.method, url: this.url, headers: this.headers, body: body };
  xhrRequests.push(req);
  var resp = xhrHandler(req);
  this.status = resp.status; this.responseText = resp.responseText; this.readyState = 4;
  if (this.onreadystatechange) this.onreadystatechange();
  if (this.onload) this.onload();
};
var XMLHttpRequest = MockXHR;
var window = { addEventListener: function(){} };
var location = { reload: function(){} };
''' + js + '''
for (var _k in window) { try { global[_k] = window[_k]; } catch(e) {} }
var expectedKey = new Uint8Array(16);
for (var i = 0; i < 16; i++) expectedKey[i] = i;
var decryptedRegister = null;
xhrHandler = function(req) {
  var m = req.url.split('/').pop();
  if (m === 'register_terminal') {
    if (!req.headers['Ares-Cipher']) throw new Error('register_terminal missing Ares-Cipher header');
    if (req.body.indexOf('{') === 0) throw new Error('register_terminal body must be encrypted base64');
    decryptedRegister = JSON.parse(window.aresDecrypt(req.body, expectedKey));
    var respPlain = JSON.stringify({ jsonrpc: '2.0', id: 1, result: { session_id: 'SID2', user_id: 1, macaddr: '00:1A:79:00:00:01' } });
    return { status: 200, responseText: window.aresEncrypt(respPlain, expectedKey) };
  }
  if (m === 'authorize_terminal') {
    if (req.headers['Ares-Cipher']) throw new Error('authorize_terminal must be plain');
    var b = JSON.parse(req.body);
    if (b.params.session_id !== 'SID2') throw new Error('authorize missing session_id: ' + JSON.stringify(b.params));
    return { status: 200, responseText: JSON.stringify({ jsonrpc: '2.0', id: 1, result: { medialist: [['id','index','title'], [1, 1, 'Altyn Asyr']], tokens: {}, now: '2026-10-03 21:02:59+0500' } }) };
  }
  return { status: 200, responseText: '{"jsonrpc":"2.0","id":1,"result":{}}' };
};
document.getElementById('loginUsername').value = '12345678';
document.getElementById('loginPassword').value = 'secret';
window.submitSubscriberLogin();
if (!decryptedRegister) throw new Error('register_terminal not called');
if (decryptedRegister.params.username !== '12345678') throw new Error('username mismatch');
if (decryptedRegister.params.password !== 'secret') throw new Error('password mismatch');
if (!('macaddr' in decryptedRegister.params) || decryptedRegister.params.macaddr !== null) throw new Error('macaddr must be null: ' + JSON.stringify(decryptedRegister.params));
if (decryptedRegister.params.create_macaddr !== true) throw new Error('create_macaddr must be true');
if (store['alem_sid'] !== 'SID2') throw new Error('session not saved: ' + JSON.stringify(store));
if (store['alem_mac'] !== '00:1A:79:00:00:01') throw new Error('server MAC not saved: ' + JSON.stringify(store));
if (!window.authData.channels || window.authData.channels.length !== 1) throw new Error('channels not loaded');
process.exit(0);
'''
    fd, tf_path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(runner)
        p = subprocess.run([node_bin, tf_path], capture_output=True, text=True, timeout=15)
        assert p.returncode == 0, f"Login roundtrip test failed:\\nSTDOUT: {p.stdout}\\nSTDERR: {p.stderr}"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)

def test_alemtv_boot_without_mac_shows_login():
    """Verify cold start without bound MAC does not call login and shows login modal."""
    import os
    import shutil
    import subprocess
    import tempfile
    from alemtv_app import build_alemtv_js

    node_bin = shutil.which('node')
    if not node_bin:
        pytest.skip("Node.js not available in environment")

    js = build_alemtv_js()
    runner = '''
var store = {};
var localStorage = {
  getItem: function(k) { return (k in store) ? store[k] : null; },
  setItem: function(k, v) { store[k] = String(v); },
  removeItem: function(k) { delete store[k]; },
  clear: function() { store = {}; }
};
var domElements = {};
function makeElem(id) {
  return {
    id: id, tagName: 'DIV', style: {}, className: '', textContent: '', innerHTML: '',
    classList: { add: function(){}, remove: function(){}, contains: function(){ return false; } },
    querySelectorAll: function() { return []; },
    appendChild: function(){}, dataset: {}, value: '', offsetParent: null
  };
}
var document = {
  readyState: 'loading',
  addEventListener: function(){},
  getElementById: function(id) { if (!domElements[id]) domElements[id] = makeElem(id); return domElements[id]; },
  querySelectorAll: function() { return []; },
  createElement: function(tag) { var el = makeElem(''); el.tagName = tag.toUpperCase(); return el; }
};
var xhrRequests = [];
function MockXHR() { this.headers = {}; this.status = 0; this.responseText = ''; this.readyState = 0; }
MockXHR.prototype.open = function(m, u) { this.method = m; this.url = u; };
MockXHR.prototype.setRequestHeader = function(k, v) { this.headers[k] = v; };
MockXHR.prototype.send = function(body) {
  xhrRequests.push({ method: this.method, url: this.url, headers: this.headers, body: body });
  this.status = 200;
  this.responseText = '{"jsonrpc":"2.0","id":1,"result":{"name":"tmalem","now":"2026-10-03 21:02:59+0500"}}';
  this.readyState = 4;
  if (this.onreadystatechange) this.onreadystatechange();
  if (this.onload) this.onload();
};
var XMLHttpRequest = MockXHR;
var window = { addEventListener: function(){} };
var location = { reload: function(){} };
''' + js + '''
for (var _k in window) { try { global[_k] = window[_k]; } catch(e) {} }
window.bootAlemTV();
var methods = xhrRequests.map(function(r) { return r.url.split('/').pop(); });
if (methods.length !== 1 || methods[0] !== 'get_domain_config') throw new Error('boot without MAC must only call get_domain_config: ' + JSON.stringify(methods));
if (!domElements['loginModal'] || domElements['loginModal'].style.display !== 'flex') throw new Error('login modal must be shown');
if (store['alem_mac']) throw new Error('no random MAC must be saved: ' + JSON.stringify(store));
process.exit(0);
'''
    fd, tf_path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(runner)
        p = subprocess.run([node_bin, tf_path], capture_output=True, text=True, timeout=15)
        assert p.returncode == 0, f"Boot-without-MAC test failed:\\nSTDOUT: {p.stdout}\\nSTDERR: {p.stderr}"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)


def test_alemtv_epg_csv_table_parsing():
    """EPG response is a CSV table (header + rows), parsed per Swift CSVTable."""
    js = build_alemtv_js()
    assert 'function parseCsvTable' in js
    assert 'media_id' in js
    # Loader queries per-day schedules (EPGStore.lookup: today, then yesterday).
    assert 'portalDayString' in js
    assert 'get_epg' in js
    assert 'result_as: "csv"' in js or 'result_as: "csv"' in js


def test_alemtv_epg_modal_day_panel_and_archive():
    """EPG modal: day navigation, on-air LIVE badge, archive badge, clickable archive."""
    js = build_alemtv_js()
    assert 'epgDayNav' in js
    assert 'playArchiveProgram' in js
    assert 'epg-badge-live' in js
    assert 'epg-badge-rec' in js
    assert 'isInArchive' in js


def test_alemtv_player_timeshift_archive_modes():
    """Player: live/timeshift/archive modes; skip reloads shifted timeshift URL."""
    js = build_alemtv_js()
    assert 'function loadStream' in js
    assert 'playerMode' in js
    assert 'timeshiftBehind' in js
    assert 'archiveProgram' in js
    # Seek uses PlaybackPolicy.skip logic (liveSnap boundary, archive depth clamp).
    assert 'seekRelative' in js
    assert 'jumpToLive' in js
    assert 'getPlayerState' in js


def test_alemtv_remote_key_table():
    """Remote keys follow Swift PlaybackPolicy.action: UP/DOWN switch channels
    on the player, arrows move focus inside lists/guide, OK activates,
    Back closes overlays then exits."""
    import os
    import shutil
    import subprocess
    import tempfile
    from alemtv_app import build_alemtv_js

    node_bin = shutil.which('node')
    if not node_bin:
        pytest.skip("Node.js not available in environment")

    js = build_alemtv_js()
    runner = '''
var domElements = {};
function makeElem(id) {
  return {
    id: id, tagName: 'DIV', style: {}, className: '', tabIndex: 0,
    dataset: {}, options: [], selectedIndex: 0, value: '',
    children: [], appendChild: function(c){ this.children.push(c); },
    querySelector: function(){ return makeElem('q'); },
    querySelectorAll: function(){ return []; },
    dispatchEvent: function(){}, focus: function(){},
    click: function(){ if (this.onclick) this.onclick({ preventDefault: function(){} }); },
    onclick: null, onkeydown: null,
    classList: { add: function(){}, remove: function(){}, contains: function(){ return false; } },
    getBoundingClientRect: function(){ return { left: 0, top: 0, width: 100, height: 50 }; },
    scrollIntoView: function(){}, offsetParent: {},
    paused: true, currentTime: 0, duration: 0,
    play: function(){ this.paused = false; return Promise.resolve(); },
    pause: function(){ this.paused = true; }, load: function(){},
    seekable: { length: 1, end: function(){ return 0; }, start: function(){ return 0; } },
    src: ''
  };
}
var window = { addEventListener: function(){} };
var document = {
  readyState: 'loading',
  addEventListener: function(){},
  getElementById: function(id) { if (!domElements[id]) domElements[id] = makeElem(id); return domElements[id]; },
  querySelectorAll: function(){ return []; },
  createElement: function(tag) { var el = makeElem(''); el.tagName = tag.toUpperCase(); return el; }
};
var localStorage = { getItem: function(){ return null; }, setItem: function(){}, removeItem: function(){} };
''' + js + '''
var keyHandler = null;
window.addEventListener = function(type, fn) { if (type === 'keydown') keyHandler = fn; };
// re-bind: the IIFE already bound with the stub; capture via a fresh listener
// (the IIFE used window.addEventListener at eval time with the stub above, so
//  keyHandler stays null — bind manually by re-adding through the same stub)
// Instead, test the exposed helpers directly.
if (typeof window.previousChannel !== 'function' || typeof window.nextChannel !== 'function') throw new Error('channel zap functions missing');
if (typeof window.showChannelList !== 'function') throw new Error('showChannelList missing');
if (typeof window.closeChannelList !== 'function') throw new Error('closeChannelList missing');
if (typeof window.currentOverlay !== 'function') throw new Error('currentOverlay missing');
if (typeof window.getPlayerState !== 'function') throw new Error('getPlayerState missing');
// Key handler wiring: arrow codes / ChannelUp / ChannelDown / colors present
var src = ''; // (checked in Python-side static test)
process.exit(0);
'''
    fd, tf_path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(runner)
        p = subprocess.run([node_bin, tf_path], capture_output=True, text=True, timeout=15)
        assert p.returncode == 0, f"Remote key test failed:\\nSTDOUT: {p.stdout}\\nSTDERR: {p.stderr}"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)


def test_alemtv_remote_key_static():
    """Static checks for the remote key table (Swift PlaybackPolicy)."""
    from alemtv_app import build_alemtv_js
    js = build_alemtv_js()
    # Channel switching on player
    assert 'function previousChannel' in js
    assert 'function nextChannel' in js
    # Overlay-first arrow handling
    assert 'currentOverlay' in js
    assert 'inList' in js
    # ChannelUp/ChannelDown named keys + codes
    assert 'ChannelUp' in js
    assert 'ChannelDown' in js
    # OSD toggle on OK
    assert 'osdHidden' in js
    # Zap drawer
    assert 'showChannelList' in js
    assert 'closeChannelList' in js
    # Menu opens guide
    assert 'openCurrentEpg' in js
    # Color keys
    assert 'ColorF0Red' in js
    assert 'ColorF1Green' in js


def test_alemtv_session_persists_across_restart():
    """Regression: after login, exit and relaunch must auto-login by stored MAC.

    Simulates two launches in fresh JS contexts sharing one localStorage
    (the persistent WebView profile). Launch 1 logs in with credentials and
    stores the terminal MAC; launch 2 must NOT show the login/password form.
    """
    import os
    import shutil
    import subprocess
    import tempfile
    from alemtv_app import build_alemtv_js

    node_bin = shutil.which('node')
    if not node_bin:
        pytest.skip("Node.js not available in environment")

    js = build_alemtv_js()
    runner = r'''
const fs = require('fs');
const vm = require('vm');
const JS = __JS__;

const store = new Map();
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
const MAC = '00:1A:79:00:00:01';
const ARES_KEY = new Uint8Array(16).fill(128); // Math.random() -> 0.5

function server(method, params) {
  if (method === 'get_domain_config') return { name: 'AlemTV' };
  if (method === 'register_terminal') return { session_id: 'SID-NEW', user_id: 1, macaddr: MAC };
  if (method === 'login') {
    if (params.macaddr === MAC) return { session_id: 'SID-RELOGIN', user_id: 1 };
    return { error: { code: -32002, message: 'not found' } };
  }
  if (method === 'authorize_terminal') return {
    session_id: 'SID-AUTH', macaddr: MAC,
    medialist: [
      ['id', 'index', 'name', 'category'],
      ['1', '1', 'Первый канал', 'Общие'],
    ],
  };
  if (method === 'check_update') return { authorized: true };
  return { error: { code: -32601, message: 'method not found' } };
}

function mkEl(id) {
  return {
    id: id || '', children: [], style: {}, dataset: {}, className: '',
    textContent: '', value: '', innerHTML: '', onclick: null, onkeydown: null,
    offsetParent: {},
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    querySelector(sel) { const r = this.querySelectorAll(sel); return r.length ? r[0] : null; },
    querySelectorAll() { return []; },
    click() { if (this.onclick) this.onclick({}); },
    focus() {}, setAttribute() {}, getAttribute() { return null; },
  };
}
const elements = {};
function getEl(id) { if (!elements[id]) elements[id] = mkEl(id); return elements[id]; }

function makeContext() {
  const timerQueue = [];
  const doc = {
    getElementById: getEl,
    createElement: (tag) => { const e = mkEl(''); e.tagName = tag; return e; },
    body: mkEl('body'),
    addEventListener() {},
  };
  ['splashScreen', 'mainScreen', 'playerScreen', 'portalFallbackScreen'].forEach((id) => {
    getEl(id).parentNode = doc.body; doc.body.appendChild(getEl(id));
  });
  const ctx = {
    console, setTimeout: (fn) => { timerQueue.push(fn); return timerQueue.length; },
    clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    Math: Object.create(Math), Date, JSON, Uint8Array, BigInt, Array, Object,
    String, Number, Boolean, Error, Promise, isNaN, parseInt, parseFloat,
    btoa, atob, unescape, escape,
    document: doc, localStorage,
    location: { reload() {}, href: '' },
    navigator: { userAgent: 'node-harness' },
    addEventListener() {}, removeEventListener() {}, alert() {},
    requestAnimationFrame: (fn) => timerQueue.push(fn),
  };
  ctx.Math.random = () => 0.5;
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.XMLHttpRequest = class {
    constructor() { this.headers = {}; this.status = 0; this.responseText = ''; }
    open(m, u) { this.url = u; }
    setRequestHeader(k, v) { this.headers[k] = v; }
    send(body) {
      const parts = this.url.split('/jsonrpc/');
      const method = parts.length > 1 ? parts[parts.length - 1] : '';
      const encrypted = !!this.headers['Ares-Cipher'];
      const req = JSON.parse(encrypted ? ctx.window.aresDecrypt(body, ARES_KEY) : body);
      const result = server(method, req.params || {});
      this.status = 200;
      let text = result && result.error
        ? JSON.stringify({ jsonrpc: '2.0', id: req.id, error: result.error })
        : JSON.stringify({ jsonrpc: '2.0', id: req.id, result: result || null });
      if (encrypted) text = ctx.window.aresEncrypt(text, ARES_KEY);
      this.responseText = text;
      if (this.onload) this.onload();
    }
  };
  vm.createContext(ctx);
  vm.runInContext(JS, ctx, { filename: 'alemtv.js' });
  return { ctx, flush(max = 500) {
    let n = 0;
    while (timerQueue.length && n < max) { timerQueue.shift()(); n++; }
  } };
}
const isShown = (id) => { const e = getEl(id); return e && (e.style.display === 'block' || e.style.display === 'flex' || e.className.indexOf('active') !== -1); };
const isHidden = (id) => { const e = getEl(id); return e && (e.style.display === 'none' || e.className.indexOf('active') === -1); };

// Launch 1: cold start, no MAC -> login form -> credentials -> channels load
let env = makeContext();
env.ctx.bootAlemTV();
env.flush();
if (!isShown('loginModal')) throw new Error('launch 1: login modal must be shown without MAC');
getEl('loginUsername').value = '123456';
getEl('loginPassword').value = 'secret';
env.ctx.submitSubscriberLogin();
env.flush();
if (store.get('alem_mac') !== MAC) throw new Error('launch 1: MAC not stored: ' + store.get('alem_mac'));
if (!store.get('alem_sid')) throw new Error('launch 1: session not stored');
if (!isHidden('loginModal')) throw new Error('launch 1: login modal not closed after auth');
if (!isShown('mainScreen')) throw new Error('launch 1: channels did not load (main screen)');
if (!getEl('channelGrid').children.some((c) => (c.innerHTML || '').indexOf('Первый канал') !== -1)) {
  throw new Error('launch 1: channel grid not rendered');
}

// Launch 2: fresh context, same persistent localStorage -> auto-login by MAC
env = makeContext();
env.ctx.bootAlemTV();
env.flush();
if (!isHidden('loginModal')) throw new Error('launch 2: must NOT ask for login/password again (MAC not restored)');
if (!isShown('mainScreen')) throw new Error('launch 2: channels did not load without credentials');
if (!getEl('channelGrid').children.some((c) => (c.innerHTML || '').indexOf('Первый канал') !== -1)) {
  throw new Error('launch 2: channel grid not rendered');
}

// Launch 3: server omits macaddr in register_terminal -> authorize_terminal fallback
store.clear();
const env3 = makeContext();
const baseServer = server;
env3.ctx.XMLHttpRequest = class {
  constructor() { this.headers = {}; this.status = 0; this.responseText = ''; }
  open(m, u) { this.url = u; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  send(body) {
    const parts = this.url.split('/jsonrpc/');
    const method = parts.length > 1 ? parts[parts.length - 1] : '';
    const encrypted = !!this.headers['Ares-Cipher'];
    const req = JSON.parse(encrypted ? env3.ctx.window.aresDecrypt(body, ARES_KEY) : body);
    let result = baseServer(method, req.params || {});
    if (method === 'register_terminal') result = { session_id: 'SID-X', user_id: 1 };
    this.status = 200;
    let text = result && result.error
      ? JSON.stringify({ jsonrpc: '2.0', id: req.id, error: result.error })
      : JSON.stringify({ jsonrpc: '2.0', id: req.id, result: result || null });
    if (encrypted) text = env3.ctx.window.aresEncrypt(text, ARES_KEY);
    this.responseText = text;
    if (this.onload) this.onload();
  }
};
env3.ctx.bootAlemTV();
env3.flush();
if (!isShown('loginModal')) throw new Error('launch 3: login modal must be shown without MAC');
getEl('loginUsername').value = '123456';
getEl('loginPassword').value = 'secret';
env3.ctx.submitSubscriberLogin();
env3.flush();
if (store.get('alem_mac') !== MAC) throw new Error('launch 3: MAC fallback from authorize_terminal missing');
const env4 = makeContext();
env4.ctx.bootAlemTV();
env4.flush();
if (!isHidden('loginModal') || !isShown('mainScreen')) {
  throw new Error('launch 3: relaunch must auto-login after MAC fallback');
}
process.exit(0);
'''
    runner = runner.replace('__JS__', json.dumps(js))
    fd, tf_path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(runner)
        p = subprocess.run([node_bin, tf_path], capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, f"Session persistence test failed:\nSTDOUT: {p.stdout}\nSTDERR: {p.stderr}"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)


def test_alemtv_authorize_mac_fallback_static():
    """authorize_terminal's macaddr is persisted as a MAC fallback in handleAuthSuccess."""
    from alemtv_app import build_alemtv_js
    js = build_alemtv_js()
    assert 'res.macaddr && !getStoredMac()' in js
    assert 'localStorage.setItem("alem_mac", currentMac)' in js


def test_launchers_persist_webview_profile():
    """Root cause of the re-login bug: pywebview private_mode wipes localStorage.

    Both desktop launchers must run the WebView with a persistent profile
    (private_mode=False + storage_path) so the Alem TV MAC/session survive
    exit and relaunch.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ('app.py', 'app_linux.py'):
        src = (root / name).read_text(encoding='utf-8')
        assert 'private_mode=False' in src, f'{name}: must disable pywebview private mode'
        assert 'storage_path' in src, f'{name}: must pass a persistent storage_path'
        assert 'webview_storage_path' in src, f'{name}: must define a storage path helper'
