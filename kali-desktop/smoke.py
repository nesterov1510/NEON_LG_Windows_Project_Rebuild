"""After apt install: xvfb-run -a /usr/bin/python3 smoke.py beletfilm (or alemtv)."""
import http.server
import json
import runpy
import sys
import threading
import warnings

from gi import PyGIWarning

warnings.filterwarnings("error", category=PyGIWarning)


def _start_mock_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/api/check')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/api/check':
                payload = json.dumps({
                    'ok': True,
                    'method': 'GET',
                    'hadOrigin': 'origin' in {k.lower() for k in self.headers.keys()},
                    'hadUa': 'user-agent' in {k.lower() for k in self.headers.keys()},
                    'platform': self.headers.get('X-Platform', ''),
                }).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length).decode('utf-8', 'replace')
            payload = json.dumps({
                'ok': True,
                'method': 'POST',
                'hadOrigin': 'origin' in {k.lower() for k in self.headers.keys()},
                'hadUa': 'user-agent' in {k.lower() for k in self.headers.keys()},
                'platform': self.headers.get('X-Platform', ''),
                'body': body,
            }).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


app, = sys.argv[1:]
mock_srv = _start_mock_server()
mock_base = f'http://127.0.0.1:{mock_srv.server_port}'
launcher = runpy.run_path('/usr/bin/' + app)
Gtk, GLib = launcher['Gtk'], launcher['GLib']
window = launcher['player_window'](app)
view = window.get_child().get_visible_child() if app == 'beletfilm' else window.get_child()
ready = False
checking = False
probe = {
    'alemtv': (
        "typeof window.bootAlemTV === 'function' && !!localStorage.alem_portal && "
        "typeof window.__NEON_PROXY_BASE__ === 'string' && typeof window.Hls === 'function' && "
        "window.Hls.isSupported() === true"
    ),
    'beletfilm': (
        "typeof Belet === 'object' && !!document.getElementById('toQr').onclick && !!Belet.device() && "
        "typeof window.__NEON_PROXY_BASE__ === 'string' && typeof window.Hls === 'function' && "
        "window.Hls.isSupported() === true"
    ),
}[app]


def received(v, result, _):
    global ready, checking
    checking = False
    ready = v.evaluate_javascript_finish(result).to_boolean()
    if ready:
        Gtk.main_quit()


def poll():
    global checking
    if not checking and not view.is_loading():
        checking = True
        view.evaluate_javascript(probe, -1, None, None, None, received, None)
    return not ready


GLib.timeout_add(200, poll)
GLib.timeout_add_seconds(30, Gtk.main_quit)
Gtk.main()
if not ready:
    sys.exit(f'{app}: packaged client did not initialize')

# Verify in real WebKitGTK that XHR routes through the loopback proxy, strips
# Origin (and User-Agent for beletfilm), and preserves redirected responseURL.
xhr_test_js = f"""
window.__smokeProxyDone = null;
(function () {{
  var x1 = new XMLHttpRequest();
  x1.open('GET', {json.dumps(mock_base + '/redirect')}, true);
  x1.setRequestHeader('X-Platform', 'Android');
  x1.onload = function () {{
    var d1 = JSON.parse(x1.responseText);
    var x2 = new XMLHttpRequest();
    x2.open('POST', {json.dumps(mock_base + '/api/check')}, true);
    x2.setRequestHeader('Content-Type', 'application/json');
    x2.setRequestHeader('X-Platform', 'Android');
    x2.onload = function () {{
      var d2 = JSON.parse(x2.responseText);
      window.__smokeProxyDone = JSON.stringify({{
        s1: x1.status, url1: x1.responseURL, d1: d1,
        s2: x2.status, url2: x2.responseURL, d2: d2
      }});
    }};
    x2.onerror = function () {{ window.__smokeProxyDone = JSON.stringify({{ err: 'post' }}); }};
    x2.send('{{"ping":1}}');
  }};
  x1.onerror = function () {{ window.__smokeProxyDone = JSON.stringify({{ err: 'get' }}); }};
  x1.send();
}})();
"""
proxy_result = None
proxy_checking = False
view.evaluate_javascript(xhr_test_js, -1, None, None, None, None, None)


def got_proxy(v, result, _):
    global proxy_result, proxy_checking
    proxy_checking = False
    val = v.evaluate_javascript_finish(result)
    if val.is_string():
        proxy_result = json.loads(val.to_string())
        Gtk.main_quit()


def poll_proxy():
    global proxy_checking
    if not proxy_checking and proxy_result is None:
        proxy_checking = True
        view.evaluate_javascript('window.__smokeProxyDone', -1, None, None, None, got_proxy, None)
    return proxy_result is None


GLib.timeout_add(100, poll_proxy)
GLib.timeout_add_seconds(10, Gtk.main_quit)
Gtk.main()
assert proxy_result and not proxy_result.get('err'), f'{app}: XHR proxy check failed: {proxy_result}'
assert proxy_result['s1'] == 200 and proxy_result['url1'] == mock_base + '/api/check', proxy_result
assert proxy_result['s2'] == 200 and proxy_result['url2'] == mock_base + '/api/check', proxy_result
assert proxy_result['d1']['hadOrigin'] is False and proxy_result['d2']['hadOrigin'] is False, proxy_result
assert proxy_result['d2']['platform'] == 'Android' and proxy_result['d2']['body'] == '{"ping":1}', proxy_result
if app == 'beletfilm':
    assert proxy_result['d2']['hadUa'] is False, proxy_result

print(f'{app}: installed client initialized in WebKitGTK (MSE + proxy verified)')

if app == 'beletfilm':
    switch = next(w for w in window.get_titlebar().get_children() if isinstance(w, Gtk.ToggleButton))
    switch.set_active(True)
    website = window.get_child().get_visible_child()
    assert website != view and website.get_uri() == 'https://belet.tm/'
    switch.set_active(False)
    assert window.get_child().get_visible_child() == view

window.destroy()
mock_srv.server_close()
