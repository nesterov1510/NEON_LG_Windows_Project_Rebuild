"""Tests for standalone Kali Linux desktop packages (beletfilm & alemtv)."""
import base64
import http.server
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading
import types
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parent.parent
KALI_DIR = ROOT / 'kali-desktop'


def _extract_deb_via_python(deb_path, extract_dir):
    data = Path(deb_path).read_bytes()
    assert data.startswith(b'!<arch>\n'), f'Invalid ar archive: {deb_path}'
    pos = 8
    control_text = ''
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        pos += 60
        name = header[:16].decode('ascii', 'replace').strip().rstrip('/')
        size = int(header[48:58].decode('ascii', 'replace').strip())
        member = data[pos:pos + size]
        pos += size + (size % 2)
        if name.startswith('control.tar'):
            with tarfile.open(fileobj=io.BytesIO(member), mode='r:*') as tf:
                for entry in tf.getmembers():
                    if entry.name.lstrip('./') == 'control':
                        f = tf.extractfile(entry)
                        if f:
                            control_text = f.read().decode('utf-8', 'replace')
        elif name.startswith('data.tar'):
            with tarfile.open(fileobj=io.BytesIO(member), mode='r:*') as tf:
                for entry in tf.getmembers():
                    rel = Path(entry.name.lstrip('./'))
                    if not rel.parts:
                        continue
                    target = (Path(extract_dir) / rel).resolve()
                    if Path(extract_dir).resolve() not in target.parents and target != Path(extract_dir).resolve():
                        raise RuntimeError(f'Unsafe deb path: {entry.name}')
                    if entry.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif entry.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        f = tf.extractfile(entry)
                        if f:
                            target.write_bytes(f.read())
    return control_text


def _extract_deb(deb_path, extract_dir):
    dpkg_deb = shutil.which('dpkg-deb')
    if dpkg_deb:
        subprocess.run([dpkg_deb, '-x', str(deb_path), str(extract_dir)], check=True)
        return subprocess.check_output([dpkg_deb, '-f', str(deb_path)], text=True)
    return _extract_deb_via_python(deb_path, extract_dir)


def _load_player_module():
    gi_mod = types.ModuleType('gi')
    gi_mod.require_version = lambda *args, **kwargs: None
    repo_mod = types.ModuleType('gi.repository')
    for name in ('Gdk', 'GLib', 'Gtk', 'WebKit2'):
        setattr(repo_mod, name, types.SimpleNamespace())
    sys.modules['gi'] = gi_mod
    sys.modules['gi.repository'] = repo_mod
    spec = importlib.util.spec_from_file_location('kali_desktop_player', KALI_DIR / 'player.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _start_upstream_server():
    recorded = []
    # Minimal valid PNG prefix required by api.js qrCode format check.
    fake_qr_png = base64.b64encode(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR').decode('ascii')

    class UpstreamHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            return

        def _capture(self, body=b''):
            recorded.append({
                'method': self.command,
                'path': self.path,
                'headers': {k.lower(): v for k, v in self.headers.items()},
                'body': body.decode('utf-8', 'replace'),
            })

        def _json(self, status, obj):
            payload = json.dumps(obj).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == '/api/v1/auth/get-qrcode':
                self._capture()
                self._json(200, {'code': 'https://film.belet.me/register/qrToken99', 'qr_png': fake_qr_png})
                return
            if self.path == '/redirect-to-m3u8':
                self._capture()
                self.send_response(302)
                self.send_header('Location', '/cdn/live/index.m3u8?token=xyz')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path.startswith('/cdn/live/index.m3u8'):
                self._capture()
                body = b'#EXTM3U\n#EXTINF:4.0,\n/hls/seg1.ts\n'
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == '/chunked':
                self._capture()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                for part in (b'hello ', b'world'):
                    self.wfile.write(f'{len(part):x}\r\n'.encode('ascii') + part + b'\r\n')
                self.wfile.write(b'0\r\n\r\n')
                return
            if self.path == '/range':
                self._capture()
                rng = self.headers.get('Range')
                data = b'0123456789'
                if rng == 'bytes=2-5':
                    slice_data = data[2:6]
                    self.send_response(206)
                    self.send_header('Content-Range', 'bytes 2-5/10')
                    self.send_header('Content-Length', str(len(slice_data)))
                    self.end_headers()
                    self.wfile.write(slice_data)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._capture()
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length)
            self._capture(body)
            if self.path == '/api/v1/auth/check-qrcode':
                self._json(200, {'token': 'qr-session-token', 'refresh_token': 'qr-refresh-token'})
                return
            if self.path.startswith('/api/v1/auth/sign-in'):
                self._json(200, {'token': 'sms-temp-token'})
                return
            if self.path == '/api/v1/auth/check-code-mobile':
                self._json(200, {'token': 'sms-session-token', 'refresh_token': 'sms-refresh-token'})
                return
            if self.path == '/api/v1/users/device':
                self._json(200, {'status': 'ok'})
                return
            self._json(200, {'ok': True, 'path': self.path, 'token': 'tok-123', 'refresh_token': 'ref-456'})

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), UpstreamHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, recorded


def test_deb_packages_contain_proxy_and_hls_support(tmp_path):
    pkg_dir = KALI_DIR / 'packages'
    belet_debs = list(pkg_dir.glob('beletfilm_*_all.deb'))
    alem_debs = list(pkg_dir.glob('alemtv_*_all.deb'))
    assert len(belet_debs) == 1
    assert len(alem_debs) == 1

    node = shutil.which('node')
    for deb, app in ((belet_debs[0], 'beletfilm'), (alem_debs[0], 'alemtv')):
        extract = tmp_path / app
        ctrl = _extract_deb(deb, extract)
        assert 'Version: 1.0.3' in ctrl
        for dep in ('gir1.2-webkit2-4.1', 'gstreamer1.0-plugins-base', 'gstreamer1.0-plugins-good',
                    'gstreamer1.0-plugins-bad', 'gstreamer1.0-plugins-ugly', 'gstreamer1.0-libav',
                    'gstreamer1.0-gl'):
            assert dep in ctrl

        shared = extract / 'usr' / 'share' / app
        html = (shared / 'index.html').read_text(encoding='utf-8')
        assert (shared / 'desktop.js').is_file()
        assert (shared / 'hls.min.js').is_file()
        assert (shared / 'hls.LICENSE').is_file()
        assert html.index('<script src="desktop.js"></script>') < html.index('api.js' if app == 'beletfilm' else '(function(){')
        if app == 'alemtv':
            assert html.index('<script src="hls.min.js"></script>') < html.index('<script src="desktop.js"></script>')
        if node:
            subprocess.run([node, '--check', str(shared / 'desktop.js')], check=True)

    import alemtv_app
    tv_html = alemtv_app.build_alemtv_html(desktop=False)
    assert 'desktop.js' not in tv_html
    assert 'hls.min.js' not in tv_html


def test_loopback_proxy_headers_redirects_range_and_security():
    player_mod = _load_player_module()
    upstream, recorded = _start_upstream_server()
    belet_base, belet_srv = player_mod._start_proxy('beletfilm')
    alem_base, alem_srv = player_mod._start_proxy('alemtv')
    try:
        # 1. Invalid token or self-loop blocked.
        bad_url = belet_base.rsplit('/', 1)[0] + '/deadbeef/http/127.0.0.1:' + str(upstream.server_port) + '/range'
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(bad_url)
        assert exc.value.code == 403

        loop_url = f'{belet_base}/http/127.0.0.1:{belet_srv.server_port}/range'
        with pytest.raises(urllib.error.HTTPError) as exc2:
            urllib.request.urlopen(loop_url)
        assert exc2.value.code == 400

        # 2. CORS preflight.
        req_opt = urllib.request.Request(f'{belet_base}/http/127.0.0.1:{upstream.server_port}/api/v1/auth/check-qrcode', method='OPTIONS')
        with urllib.request.urlopen(req_opt) as resp:
            assert resp.status == 204
            assert resp.headers.get('Access-Control-Allow-Origin') == '*'

        # 3. BeletFilm POST strips browser Origin/Referer/Sec-Fetch/User-Agent and preserves app headers.
        req = urllib.request.Request(
            f'{belet_base}/http/127.0.0.1:{upstream.server_port}/api/v1/auth/check-qrcode',
            data=b'{"code":"abc","fingerprint":"0123"}',
            method='POST',
            headers={
                'Content-Type': 'application/json; charset=utf-8',
                'Origin': 'null',
                'Referer': 'file:///usr/share/beletfilm/index.html',
                'Sec-Fetch-Mode': 'cors',
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)',
                'X-Platform': 'Android',
                'X-VersionCode': '182',
                'fingerprint': '0123',
            },
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            assert resp.headers.get('Access-Control-Allow-Origin') == '*'
            assert resp.headers.get('X-Neon-Final-URL') == f'http://127.0.0.1:{upstream.server_port}/api/v1/auth/check-qrcode'
            data = json.loads(resp.read().decode('utf-8'))
            assert data['token'] == 'qr-session-token'
        seen = recorded[-1]
        for stripped in ('origin', 'referer', 'sec-fetch-mode', 'user-agent'):
            assert stripped not in seen['headers']
        assert seen['headers']['x-platform'] == 'Android'
        assert seen['headers']['fingerprint'] == '0123'
        assert '"code":"abc"' in seen['body']

        # 4. Alem TV preserves User-Agent while stripping Origin/Sec-Fetch.
        req_alem = urllib.request.Request(
            f'{alem_base}/http/127.0.0.1:{upstream.server_port}/redirect-to-m3u8',
            headers={'Origin': 'null', 'User-Agent': 'WebKitTest/1.0', 'Sec-Fetch-Site': 'cross-site'},
        )
        with urllib.request.urlopen(req_alem) as resp:
            assert resp.status == 200
            assert resp.headers.get('X-Neon-Final-URL') == f'http://127.0.0.1:{upstream.server_port}/cdn/live/index.m3u8?token=xyz'
            assert b'#EXTM3U' in resp.read()
        seen_alem = recorded[-1]
        assert seen_alem['headers'].get('user-agent') == 'WebKitTest/1.0'
        assert 'origin' not in seen_alem['headers']
        assert 'sec-fetch-site' not in seen_alem['headers']

        # 5. Chunked and Range streaming.
        with urllib.request.urlopen(f'{alem_base}/http/127.0.0.1:{upstream.server_port}/chunked') as resp:
            assert resp.read() == b'hello world'
        req_range = urllib.request.Request(
            f'{alem_base}/http/127.0.0.1:{upstream.server_port}/range',
            headers={'Range': 'bytes=2-5'},
        )
        with urllib.request.urlopen(req_range) as resp:
            assert resp.status == 206
            assert resp.read() == b'2345'
    finally:
        belet_srv.server_close()
        alem_srv.server_close()
        upstream.server_close()


def test_belet_and_alemtv_desktop_js_end_to_end(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js not installed')
    player_mod = _load_player_module()
    upstream, recorded = _start_upstream_server()
    belet_base, belet_srv = player_mod._start_proxy('beletfilm')
    alem_base, alem_srv = player_mod._start_proxy('alemtv')

    _extract_deb(next((KALI_DIR / 'packages').glob('beletfilm_*_all.deb')), tmp_path / 'belet')
    _extract_deb(next((KALI_DIR / 'packages').glob('alemtv_*_all.deb')), tmp_path / 'alem')

    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const http = require('http');
const assert = require('assert');

const [beletDir, alemtvDesktopPath, beletBase, alemBase, upstreamBase] = process.argv.slice(1);

function makeXhrClass() {
  class FakeXHR {
    constructor() {
      this.readyState = 0;
      this.status = 0;
      this.responseText = '';
      this.responseURL = '';
      this._headers = {};
      this._respHeaders = {};
    }
    open(method, url) {
      this._method = method;
      this._url = url;
      this.readyState = 1;
    }
    setRequestHeader(k, v) { this._headers[k] = v; }
    getResponseHeader(k) { return this._respHeaders[k.toLowerCase()] || null; }
    send(body) {
      const u = new URL(this._url);
      const req = http.request({
        hostname: u.hostname,
        port: u.port,
        path: u.pathname + u.search,
        method: this._method,
        headers: this._headers,
      }, (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          this.status = res.statusCode;
          this._respHeaders = res.headers;
          this.responseText = Buffer.concat(chunks).toString('utf8');
          this.readyState = 4;
          if (typeof this.onload === 'function') this.onload();
          if (typeof this.onreadystatechange === 'function') this.onreadystatechange();
        });
      });
      req.on('error', (e) => {
        if (typeof this.onerror === 'function') this.onerror(e);
      });
      if (body) req.write(body);
      req.end();
    }
  }
  return FakeXHR;
}

function makeStorage() {
  const store = {};
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
}

async function runBeletCheck() {
  const XHR = makeXhrClass();
  let attrSrc = '';
  class MediaBase {
    get src() { return attrSrc; }
    set src(v) { attrSrc = String(v || ''); }
  }
  const videoEl = Object.assign(new MediaBase(), {
    removeAttribute(name) { if (name === 'src') attrSrc = ''; }
  });
  const ctx = {
    window: null,
    document: { getElementById: (id) => (id === 'video' ? videoEl : null) },
    navigator: { userAgent: 'Mozilla/5.0 (X11; Linux x86_64)' },
    localStorage: makeStorage(),
    XMLHttpRequest: XHR,
    HTMLMediaElement: MediaBase,
    __NEON_PROXY_BASE__: beletBase,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(beletDir + '/desktop.js', 'utf8'), ctx);
  vm.runInContext(fs.readFileSync(beletDir + '/api.js', 'utf8'), ctx);

  assert.strictEqual(
    JSON.stringify(ctx.beletWebosIdentity()),
    JSON.stringify({ model: 'Kali Linux PC', osVersion: 'Linux' })
  );

  // Point auth host to upstream test server and execute real Belet QR + SMS + device registration.
  ctx.Belet.HOSTS.auth = upstreamBase;
  ctx.Belet.init();

  await new Promise((resolve, reject) => {
    ctx.Belet.qrCode((err, qr) => {
      if (err) return reject(err);
      assert.strictEqual(qr.code, 'qrToken99');
      ctx.Belet.qrCheck(qr.code, (checkErr, data) => {
        if (checkErr) return reject(checkErr);
        assert.strictEqual(data.token, 'qr-session-token');
        ctx.Belet.acceptQrSession(data);
        ctx.Belet.registerDevice((regErr) => {
          if (regErr) return reject(regErr);
          ctx.Belet.signIn('99361000000', 1, (smsErr, smsData) => {
            if (smsErr) return reject(smsErr);
            assert.strictEqual(smsData.token, 'sms-temp-token');
            ctx.Belet.checkCode('1234', smsData.token, (codeErr, codeData) => {
              if (codeErr) return reject(codeErr);
              assert.strictEqual(codeData.token, 'sms-session-token');
              resolve();
            });
          });
        });
      });
    });
  });

  await new Promise((resolve, reject) => {
    const xhr = new ctx.XMLHttpRequest();
    xhr.open('GET', upstreamBase + '/redirect-to-m3u8', true);
    xhr.setRequestHeader('Origin', 'null');
    xhr.setRequestHeader('X-Platform', 'Android');
    xhr.onload = () => {
      try {
        assert.strictEqual(xhr.status, 200);
        assert.strictEqual(xhr.responseURL, upstreamBase + '/cdn/live/index.m3u8?token=xyz');
        assert.ok(xhr.responseText.includes('#EXTM3U'));
        resolve();
      } catch (err) { reject(err); }
    };
    xhr.onerror = reject;
    xhr.send();
  });

  // Verify #video src proxies https:// while leaving blob: and removeAttribute alone.
  videoEl.src = 'https://videofilm.belet.me/a/index.m3u8';
  assert.strictEqual(videoEl.src, 'https://videofilm.belet.me/a/index.m3u8');
  assert.strictEqual(attrSrc, beletBase + '/https/videofilm.belet.me/a/index.m3u8');
  videoEl.src = 'blob:file://123';
  assert.strictEqual(videoEl.src, 'blob:file://123');
  assert.strictEqual(attrSrc, 'blob:file://123');
  videoEl.removeAttribute('src');
  assert.strictEqual(videoEl.src, '');
}

async function runAlemCheck() {
  const XHR = makeXhrClass();
  let attrSrc = '';
  let loadCalls = [];
  let playCalls = 0;
  let errorListeners = [];
  let errorCount = 0;
  let fakeHlsInstances = [];
  let hlsFailAll = false;
  let nativeErrors = 0;

  class MediaBase {
    get src() { return attrSrc; }
    set src(v) { attrSrc = String(v || ''); }
    get error() { return nativeErrors ? { code: 4 } : null; }
    load() {
      loadCalls.push(attrSrc);
      if (attrSrc && (attrSrc.startsWith(alemBase) || attrSrc.startsWith('http://direct-fail'))) {
        nativeErrors++;
        errorListeners.forEach((fn) => fn());
      }
    }
    play() { playCalls++; return Promise.resolve(); }
    pause() {}
  }

  const videoEl = Object.assign(new MediaBase(), {
    firstChild: null,
    removeAttribute(name) { if (name === 'src') attrSrc = ''; },
    removeChild() {},
    querySelector() { return null; },
    addEventListener(type, fn) { if (type === 'error') errorListeners.push(fn); }
  });

  class FakeHls {
    constructor(cfg) {
      this.cfg = cfg;
      this.handlers = {};
      this.destroyed = false;
      this.recoverCalls = 0;
      fakeHlsInstances.push(this);
    }
    static isSupported() { return true; }
    on(ev, fn) { this.handlers[ev] = fn; }
    attachMedia(media) {
      media.src = 'blob:file://hls-' + fakeHlsInstances.length;
      if (this.handlers.MEDIA_ATTACHED) this.handlers.MEDIA_ATTACHED();
    }
    loadSource(url) {
      this.loadedUrl = url;
      setTimeout(() => {
        if (this.destroyed) return;
        if (hlsFailAll) {
          this.handlers.ERROR({}, { fatal: true, type: 'MEDIA_ERROR' });
          if (!this.destroyed) this.handlers.ERROR({}, { fatal: true, type: 'MEDIA_ERROR' });
        } else {
          this.handlers.MANIFEST_PARSED();
        }
      }, 15);
    }
    recoverMediaError() { this.recoverCalls++; }
    destroy() { this.destroyed = true; attrSrc = ''; }
  }
  FakeHls.Events = { ERROR: 'ERROR', MANIFEST_PARSED: 'MANIFEST_PARSED', MEDIA_ATTACHED: 'MEDIA_ATTACHED' };
  FakeHls.ErrorTypes = { MEDIA_ERROR: 'MEDIA_ERROR', NETWORK_ERROR: 'NETWORK_ERROR' };

  const ctx = {
    window: null,
    document: { getElementById: (id) => (id === 'videoElement' ? videoEl : null) },
    XMLHttpRequest: XHR,
    HTMLMediaElement: MediaBase,
    Hls: FakeHls,
    __NEON_PROXY_BASE__: alemBase,
    Promise,
    setTimeout,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(alemtvDesktopPath, 'utf8'), ctx);

  videoEl.onerror = function () { errorCount++; };

  // Happy path via HLS.js: play() before MANIFEST_PARSED does not call nativePlay prematurely.
  videoEl.src = 'http://telecom.alemtv.tm/hls/1/index.m3u8?token=t1';
  videoEl.load();
  await videoEl.play();
  assert.strictEqual(playCalls, 0);
  await new Promise((r) => setTimeout(r, 40));
  assert.strictEqual(fakeHlsInstances.length, 1);
  assert.strictEqual(fakeHlsInstances[0].cfg.enableWorker, false);
  assert.strictEqual(fakeHlsInstances[0].loadedUrl, 'http://telecom.alemtv.tm/hls/1/index.m3u8?token=t1');
  assert.strictEqual(videoEl.src, 'blob:file://hls-1');
  assert.strictEqual(playCalls, 1);
  assert.strictEqual(errorCount, 0);

  // Resume after pause triggers native play once manifest is already parsed.
  videoEl.pause();
  await videoEl.play();
  assert.strictEqual(playCalls, 2);

  // Stop stream cleanly (backToChannelList).
  videoEl.pause();
  videoEl.src = '';
  assert.strictEqual(fakeHlsInstances[0].destroyed, true);
  assert.strictEqual(videoEl.src, '');
  assert.strictEqual(errorCount, 0);

  // Fallback chain: HLS fails -> recoverMediaError once -> native-proxy fails -> native-direct fails -> onerror fires ONCE.
  hlsFailAll = true;
  loadCalls = [];
  videoEl.src = 'http://direct-fail/live/index.m3u8';
  videoEl.load();
  await videoEl.play();
  await new Promise((r) => setTimeout(r, 50));
  assert.strictEqual(fakeHlsInstances[1].recoverCalls, 1);
  const nonEmptyLoads = loadCalls.filter(Boolean);
  assert.strictEqual(nonEmptyLoads[0], alemBase + '/http/direct-fail/live/index.m3u8');
  assert.strictEqual(nonEmptyLoads[1], 'http://direct-fail/live/index.m3u8');
  assert.strictEqual(errorCount, 1);
}

Promise.all([runBeletCheck(), runAlemCheck()]).catch((e) => {
  console.error(e);
  process.exit(1);
});
"""
    try:
        subprocess.run(
            [
                node, '-e', node_script,
                str(tmp_path / 'belet' / 'usr' / 'share' / 'beletfilm'),
                str(tmp_path / 'alem' / 'usr' / 'share' / 'alemtv' / 'desktop.js'),
                belet_base,
                alem_base,
                f'http://127.0.0.1:{upstream.server_port}',
            ],
            check=True,
        )
        paths = [r['path'] for r in recorded]
        assert '/api/v1/auth/get-qrcode' in paths
        assert '/api/v1/auth/check-qrcode' in paths
        assert '/api/v1/users/device' in paths
        assert '/api/v1/auth/check-code-mobile' in paths
        for rec in recorded:
            if rec['path'].startswith('/api/v1/'):
                assert 'origin' not in rec['headers']
                assert 'user-agent' not in rec['headers']
                assert rec['headers'].get('x-platform') == 'Android'
    finally:
        belet_srv.server_close()
        alem_srv.server_close()
        upstream.server_close()
