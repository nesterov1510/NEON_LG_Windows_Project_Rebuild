from __future__ import annotations

import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

import belet_vidaa_client
import core
import hisense_vidaa

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'belet_samsung_native'


def _lf(data: bytes) -> bytes:
    return data.replace(b'\r\n', b'\n')


def test_shared_client_sources_match_belet_samsung_native():
    files = belet_vidaa_client.package_files('http://192.168.1.10:18999')
    for name in belet_vidaa_client.SHARED_FILES:
        if name == 'index.html':
            continue
        expected = _lf((SRC / name).read_bytes()) if name in belet_vidaa_client.TEXT_FILES else (SRC / name).read_bytes()
        assert files[name] == expected, f'{name} must come unmodified from belet_samsung_native'

    html = files['index.html'].decode('utf-8')
    original = _lf((SRC / 'index.html').read_bytes()).decode('utf-8')
    expected_html = original.replace(
        '<script src="hls.min.js"></script>\n',
        '<script>window.__NEON_BELET_PROXY__ = "http://192.168.1.10:18999";</script>\n'
        '<script src="vidaa-shim.js"></script>\n'
        '<script src="hls.min.js"></script>\n',
        1,
    )
    assert html == expected_html
    assert html.index('vidaa-shim.js') < html.index('hls.min.js') < html.index('api.js') < html.index('app.js')

    shim = files['vidaa-shim.js'].decode('utf-8')
    assert 'beletWebosIdentity' in shim
    assert 'Hisense_GetModelName' in shim
    assert 'Hisense_ExitApp' in shim
    assert 'BACK_ALT_CODE = 461' in shim
    assert '=>' not in shim
    assert 'tizen.application.launch' not in shim


def test_standalone_html_and_data_url_inline_all_assets():
    full_html = belet_vidaa_client.build_standalone_html(proxy_base='', include_fonts=True)
    assert '<link rel="stylesheet"' not in full_html
    assert '<script src=' not in full_html
    assert 'belet-logo.png' not in full_html
    assert 'login-background.png' not in full_html
    assert 'belet-regular.ttf' not in full_html
    assert 'data:font/ttf;base64,' in full_html
    assert 'data:image/png;base64,' in full_html
    assert 'Hisense_GetModelName' in full_html
    assert 'Belet' in full_html

    data_url = belet_vidaa_client.build_vidaa_data_url()
    assert data_url.startswith('data:text/html;charset=utf-8;base64,')
    decoded = base64.b64decode(data_url.split(',', 1)[1]).decode('utf-8')
    assert 'data:font/ttf;base64,' not in decoded
    assert 'data:image/png;base64,' in decoded
    assert 'beletWebosIdentity' in decoded


def test_portal_serves_belet_files_standalone_and_safe_proxy(monkeypatch):
    class _UpstreamHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

        def do_POST(self):  # noqa: N802
            clen = int(self.headers.get('Content-Length') or '0')
            body_in = self.rfile.read(clen).decode('utf-8')
            out = json.dumps({
                'ok': True,
                'echo': body_in,
                'auth': self.headers.get('Authorization', ''),
                'stream': 'https://cdn-media.example.org/hls/master.m3u8',
            }).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):  # noqa: N802
            if self.path.endswith('.m3u8'):
                m3u8 = (
                    '#EXTM3U\n'
                    '#EXT-X-KEY:METHOD=AES-128,URI="https://keys.example.net/k.bin"\n'
                    'https://cdn-seg.example.org/seg/001.ts\n'
                ).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                self.send_header('Content-Length', str(len(m3u8)))
                self.end_headers()
                self.wfile.write(m3u8)
                return
            payload = b'SEGMENT-BYTES-OK'
            self.send_response(206)
            self.send_header('Content-Type', 'video/mp2t')
            self.send_header('Content-Range', 'bytes 0-15/16')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    up_srv = ThreadingHTTPServer(('127.0.0.1', 0), _UpstreamHandler)
    up_port = up_srv.server_address[1]
    threading.Thread(target=up_srv.serve_forever, daemon=True).start()

    portal = hisense_vidaa.HisensePortalServer()
    monkeypatch.setattr(
        portal,
        '_open_upstream',
        lambda scheme, host, timeout=15.0: http.client.HTTPConnection('127.0.0.1', up_port, timeout=timeout),
    )
    info = portal.ensure_started(tv_ip='127.0.0.1')
    base = f'http://127.0.0.1:{info["port"]}'
    try:
        with urllib.request.urlopen(f'{base}/apps/belet/', timeout=3) as resp:
            idx_html = resp.read().decode('utf-8')
            assert '<script src="vidaa-shim.js"></script>' in idx_html
            assert 'window.__NEON_BELET_PROXY__' in idx_html

        with urllib.request.urlopen(f'{base}/apps/belet', timeout=3) as resp:
            no_slash_html = resp.read().decode('utf-8')
            assert '<base href="/apps/belet/">' in no_slash_html

        with urllib.request.urlopen(f'{base}/apps/belet/vidaa-shim.js', timeout=3) as resp:
            assert 'beletWebosIdentity' in resp.read().decode('utf-8')

        with urllib.request.urlopen(f'{base}/apps/belet/standalone.html', timeout=3) as resp:
            st_html = resp.read().decode('utf-8')
            assert 'window.__NEON_BELET_PROXY__' not in st_html
            assert 'data:font/ttf;base64,' in st_html

        with urllib.request.urlopen(f'{base}/__ping', timeout=3) as resp:
            ping = json.loads(resp.read().decode('utf-8'))
            assert ping['ok'] is True
            assert ping['serverTimeMs'] > 1700000000000

        # SSRF / non-Belet hosts are strictly blocked before any network attempt.
        for bad_host in ('127.0.0.1', '192.168.1.1', 'localhost', 'evil.example.com'):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(f'{base}/__proxy/{bad_host}/secret', timeout=3)
            assert err.value.code == 403

        # CORS preflight echoes custom Belet headers
        req_opt = urllib.request.Request(
            f'{base}/__proxy/beletapis.com/api/v1/login',
            method='OPTIONS',
            headers={'Access-Control-Request-Headers': 'Authorization, fingerprint, X-Platform'},
        )
        with urllib.request.urlopen(req_opt, timeout=3) as resp:
            assert resp.status == 204
            assert resp.headers.get('Access-Control-Allow-Origin') == '*'
            assert 'fingerprint' in resp.headers.get('Access-Control-Allow-Headers', '')

        # POST JSON response scans dynamic CDN stream host and enables m3u8/segment proxy
        req_post = urllib.request.Request(
            f'{base}/__proxy/beletapis.com/api/v1/movie',
            data=b'{"id":77}',
            method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer tok123'},
        )
        with urllib.request.urlopen(req_post, timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            assert data['auth'] == 'Bearer tok123'
            assert data['echo'] == '{"id":77}'

        # m3u8 playlist URLs and #EXT-X-KEY URI are rewritten through /__proxy/
        with urllib.request.urlopen(f'{base}/__proxy/cdn-media.example.org/hls/master.m3u8', timeout=3) as resp:
            playlist = resp.read().decode('utf-8')
            assert f'{base}/__proxy/keys.example.net/k.bin' in playlist
            assert f'{base}/__proxy/cdn-seg.example.org/seg/001.ts' in playlist

        # Dynamic segment host unlocked by m3u8 rewrite streams 206 Partial Content
        with urllib.request.urlopen(f'{base}/__proxy/cdn-seg.example.org/seg/001.ts', timeout=3) as resp:
            assert resp.status == 206
            assert resp.read() == b'SEGMENT-BYTES-OK'
            assert 'cdn-seg.example.org' in resp.headers.get('X-Neon-Final-URL', '')
    finally:
        portal.stop()
        up_srv.shutdown()
        up_srv.server_close()


def test_core_hisense_belet_install_modes(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    api = core.Api()
    calls = []
    monkeypatch.setattr(api._hisense_ctrl, 'install_tile', lambda *a, **kw: (calls.append((a, kw)) or {
        'ok': True, 'confirmed': True, 'app_id': a[1], 'title': a[2], 'url': a[3],
    }))
    try:
        # 1. Default LAN full-client mode serves /apps/belet/ instead of https://belet.tm/
        res_lan = api.install_hisense_belet('192.168.1.99', mode='lan_server', launch=False)
        assert res_lan['ok'] is True
        assert res_lan['url'].endswith('/apps/belet/')
        assert 'https://belet.tm/' not in res_lan['url']

        # 2. Offline Data URI embeds full self-contained client HTML5
        res_data = api.install_hisense_belet('192.168.1.99', mode='data_url', launch=False)
        assert res_data['ok'] is True
        assert res_data['url'].startswith('data:text/html;charset=utf-8;base64,')
        decoded = base64.b64decode(res_data['url'].split(',', 1)[1]).decode('utf-8')
        assert 'beletWebosIdentity' in decoded

        # 3. TV Flash mode opens selfinstaller portal containing standalone Belet flash button
        res_flash = api.install_hisense_belet('192.168.1.99', mode='tv_flash', launch=False)
        assert res_flash['ok'] is True
        assert res_flash['mode'] == 'tv_flash'
        with urllib.request.urlopen(res_flash['portal_url'], timeout=3) as resp:
            installer_html = resp.read().decode('utf-8')
            assert '/apps/belet/standalone.html' in installer_html
            assert 'tm.belet.vidaa' in installer_html

        # 4. Backwards-compatible positional URL signature still works when custom URL is passed
        res_custom = api.install_hisense_belet('192.168.1.99', 'https://custom.example.org/', False)
        assert res_custom['ok'] is True
        assert res_custom['url'] == 'https://custom.example.org/'
    finally:
        api.close()


def test_vidaa_shim_boots_real_belet_api_and_selfinstaller_js_in_node(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is not available')

    files = belet_vidaa_client.package_files('http://192.168.1.10:18999')
    for name in ('vidaa-shim.js', 'api.js', 'app.js'):
        (tmp_path / name).write_bytes(files[name])

    installer_html = hisense_vidaa.build_tv_selfinstaller_html('http://192.168.1.10:18999')
    m = re.search(r'<script>([\s\S]*)</script>', installer_html)
    assert m is not None
    (tmp_path / 'selfinstaller.js').write_text(m.group(1), encoding='utf-8')

    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const dir = process.argv[2];

// 1. Verify ES5/JS syntax of selfinstaller & every client JS file
for (const f of ['vidaa-shim.js', 'api.js', 'app.js', 'selfinstaller.js']) {
  new vm.Script(fs.readFileSync(dir + '/' + f, 'utf8'), { filename: f });
}

// 2. Boot vidaa-shim.js + real belet_samsung_native/api.js under LAN proxy
let exitCalled = false;
const xhrCalls = [];
function MockXHR() {
  this.headers = {};
  this.readyState = 0;
  this.status = 0;
  this.responseText = '';
  this._listeners = {};
}
MockXHR.prototype.open = function (method, url) {
  this._method = method;
  this._url = url;
};
MockXHR.prototype.setRequestHeader = function (k, v) { this.headers[k] = v; };
MockXHR.prototype.addEventListener = function (ev, fn) {
  (this._listeners[ev] = this._listeners[ev] || []).push(fn);
};
MockXHR.prototype.send = function (body) {
  xhrCalls.push({ method: this._method, url: this._url, body: body ? String(body) : '', headers: this.headers });
  this.readyState = 4;
  this.status = 200;
  this.responseText = '{"data":{"ok":true}}';
  if (typeof this.onreadystatechange === 'function') this.onreadystatechange();
};

const listeners = {};
const store = {
  neon_belet_samsung_session_v2: JSON.stringify({ token: 'tok-1', refresh: 'ref-1' })
};
const ctx = vm.createContext({
  window: null,
  document: {
    activeElement: null,
    getElementById: () => null,
    addEventListener: (ev, fn) => { listeners[ev] = fn; },
    createEvent: () => ({ initEvent: () => {} }),
    dispatchEvent: () => {}
  },
  navigator: { userAgent: 'Mozilla/5.0 VIDAA/6.0 Hisense/55U7HQ' },
  Hisense_GetBrand: () => 'Hisense',
  Hisense_GetModelName: () => '55U7HQ',
  Hisense_GetFirmWareVersion: () => 'N0520',
  Hisense_ExitApp: () => { exitCalled = true; },
  __NEON_BELET_PROXY__: 'http://192.168.1.10:18999',
  XMLHttpRequest: MockXHR,
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; }
  },
  NodeList: function () {},
  HTMLMediaElement: function () {},
  setTimeout, clearTimeout, Date, Math, JSON, Object, Array, String, Number, Error, RegExp, encodeURIComponent
});
ctx.window = ctx;
ctx.window.addEventListener = (ev, fn) => { (ctx['__win_' + ev] = fn); };

vm.runInContext(fs.readFileSync(dir + '/vidaa-shim.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(dir + '/api.js', 'utf8'), ctx);
// Proxy must already be attached synchronously when api.js assigns window.Belet (before app.js boots)
assert.strictEqual(ctx.Belet.clockProxy(), 'http://192.168.1.10:18999');
if (ctx.__win_DOMContentLoaded) ctx.__win_DOMContentLoaded();

// Activate session via official public API and ensure clock proxy stays sticky
ctx.Belet.acceptQrSession({ token: 'tok-1', refresh_token: 'ref-1' });
ctx.Belet.setClockProxy('');
assert.strictEqual(ctx.Belet.clockProxy(), 'http://192.168.1.10:18999');

// Device registration + home catalog request use real Hisense/VIDAA identity through LAN proxy
ctx.Belet.registerDevice(() => {});
ctx.Belet.home(0, 12, () => {});
assert(xhrCalls.length >= 2, 'expected >=2 XHR calls, got ' + xhrCalls.length);
const req = xhrCalls[0];
assert(req.url.startsWith('http://192.168.1.10:18999/__proxy/'), 'expected proxy URL, got ' + req.url);
const payload = JSON.parse(req.body);
assert.strictEqual(payload.model, 'Hisense 55U7HQ');
assert.strictEqual(payload.os_version, 'VIDAA N0520');

// Exit calls Hisense_ExitApp
ctx.tizen.application.getCurrentApplication().exit();
assert.strictEqual(exitCalled, true);

// 3. Autonomous TV Flash mode (empty proxyBase) goes directly to Belet HTTPS
const xhrDirect = [];
function DirectXHR() {}
DirectXHR.prototype.open = function (m, u) { xhrDirect.push(u); };
DirectXHR.prototype.setRequestHeader = function () {};
DirectXHR.prototype.send = function () {};
const directStore = {};
const ctxDirect = vm.createContext({
  window: null,
  document: { activeElement: null, getElementById: () => null, addEventListener: () => {} },
  navigator: { userAgent: 'VIDAA' },
  Hisense_GetModelName: () => '65U8HQ',
  XMLHttpRequest: DirectXHR,
  NodeList: function () {},
  localStorage: {
    getItem: (k) => (k in directStore ? directStore[k] : null),
    setItem: (k, v) => { directStore[k] = String(v); },
    removeItem: (k) => { delete directStore[k]; }
  },
  setTimeout, clearTimeout, Date, Math, JSON, Object, Array, String, Number, Error, RegExp, encodeURIComponent
});
ctxDirect.window = ctxDirect;
ctxDirect.window.addEventListener = () => {};
vm.runInContext(fs.readFileSync(dir + '/vidaa-shim.js', 'utf8'), ctxDirect);
vm.runInContext(fs.readFileSync(dir + '/api.js', 'utf8'), ctxDirect);
ctxDirect.Belet.acceptQrSession({ token: 'tok-1', refresh_token: 'ref-1' });
ctxDirect.Belet.registerDevice(() => {});
ctxDirect.Belet.home(0, 12, () => {});
assert(xhrDirect.length >= 2 && xhrDirect[0].startsWith('https://'), 'expected direct HTTPS in TV Flash mode, got ' + JSON.stringify(xhrDirect));
"""
    runner = tmp_path / 'runner.js'
    runner.write_text(script, encoding='utf-8')
    subprocess.run([node, str(runner), str(tmp_path)], check=True, timeout=30)

