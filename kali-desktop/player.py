#!/usr/bin/python3
"""Desktop host for a bundled player. Installed as /usr/bin/{beletfilm,alemtv}."""
import http.client
import http.server
import json
import secrets
import socketserver
import ssl
import sys
import threading
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import gi

# GTK 3 and GTK 4 can coexist on Kali. Do not let PyGI select Gdk 4.
gi.require_version('Gdk', '3.0')
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gdk, GLib, Gtk, WebKit2

_HOP_BY_HOP = frozenset((
    'connection', 'keep-alive', 'proxy-connection', 'proxy-authenticate',
    'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade',
))
_STRIP_REQ_HEADERS = _HOP_BY_HOP | frozenset((
    'host', 'origin', 'referer', 'accept-encoding', 'cookie',
))
_STRIP_RESP_HEADERS = _HOP_BY_HOP | frozenset((
    'set-cookie', 'access-control-allow-origin', 'access-control-allow-methods',
    'access-control-allow-headers', 'access-control-expose-headers',
    'access-control-allow-credentials', 'x-neon-final-url',
    'content-security-policy', 'x-frame-options',
))
_MAX_BODY_BYTES = 16 * 1024 * 1024
_PROXY_PREFIX = '/__neon_proxy/'


class _ProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_proxy(app):
    """Run a per-process loopback HTTP bridge for the bundled file:// client."""
    token = secrets.token_hex(16)
    ssl_ctx = ssl.create_default_context()
    strip_ua = (app == 'beletfilm')

    class _ProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, format, *args):
            return

        def _send_cors_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Access-Control-Expose-Headers', '*')
            self.send_header('Access-Control-Max-Age', '86400')

        def _send_error_response(self, status, message=''):
            payload = message.encode('utf-8')
            self.send_response(status)
            self._send_cors_headers()
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.end_headers()
            if self.command != 'HEAD' and payload:
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            self.close_connection = True

        def _parse_target(self):
            if not self.path.startswith(_PROXY_PREFIX):
                return None, 404
            rest = self.path[len(_PROXY_PREFIX):]
            parts = rest.split('/', 2)
            if len(parts) < 3 or not secrets.compare_digest(parts[0], token):
                return None, 403
            scheme = parts[1].lower()
            tail = parts[2]
            if scheme not in ('http', 'https') or not tail or tail.startswith('/'):
                return None, 400
            target_url = f'{scheme}://{tail}'
            parsed = urlsplit(target_url)
            if not parsed.hostname:
                return None, 400
            port = parsed.port or (443 if scheme == 'https' else 80)
            if port == self.server.server_port and parsed.hostname in ('127.0.0.1', 'localhost', '::1'):
                return None, 400
            return target_url, 200

        def do_OPTIONS(self):
            _, status = self._parse_target()
            if status not in (200, 404):
                self._send_error_response(status)
                return
            self.send_response(204)
            self._send_cors_headers()
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True

        def _handle_proxy(self):
            target_url, status = self._parse_target()
            if not target_url:
                self._send_error_response(status)
                return

            raw_len = self.headers.get('Content-Length')
            body = None
            if raw_len is not None:
                try:
                    length = int(raw_len)
                except ValueError:
                    self._send_error_response(400, 'Invalid Content-Length')
                    return
                if length < 0 or length > _MAX_BODY_BYTES:
                    self._send_error_response(413, 'Payload too large')
                    return
                body = self.rfile.read(length) if length > 0 else b''

            method = self.command
            if body is None and method in ('POST', 'PUT', 'PATCH'):
                body = b''

            req_headers = {}
            for key, val in self.headers.items():
                lower = key.lower()
                if lower in _STRIP_REQ_HEADERS or lower == 'content-length':
                    continue
                if lower.startswith(('sec-fetch-', 'sec-ch-')):
                    continue
                if strip_ua and lower == 'user-agent':
                    continue
                req_headers[key] = val
            if body is not None:
                req_headers['Content-Length'] = str(len(body))

            current_url = target_url
            for _ in range(6):
                parsed = urlsplit(current_url)
                scheme = parsed.scheme.lower()
                if scheme not in ('http', 'https') or not parsed.hostname:
                    self._send_error_response(400, 'Unsupported redirect URL')
                    return
                port = parsed.port or (443 if scheme == 'https' else 80)
                if port == self.server.server_port and parsed.hostname in ('127.0.0.1', 'localhost', '::1'):
                    self._send_error_response(400, 'Loop blocked')
                    return
                req_path = (parsed.path or '/') + (f'?{parsed.query}' if parsed.query else '')
                conn = None
                try:
                    if scheme == 'https':
                        conn = http.client.HTTPSConnection(parsed.hostname, port, timeout=20, context=ssl_ctx)
                    else:
                        conn = http.client.HTTPConnection(parsed.hostname, port, timeout=20)
                    conn.request(method, req_path, body=body, headers=req_headers)
                    resp = conn.getresponse()

                    location = resp.getheader('Location')
                    if resp.status in (301, 302, 303, 307, 308) and location:
                        next_url = urljoin(current_url, location)
                        conn.close()
                        conn = None
                        if resp.status in (301, 302, 303) and method not in ('GET', 'HEAD'):
                            method = 'GET'
                            body = None
                            req_headers = {
                                k: v for k, v in req_headers.items()
                                if k.lower() not in ('content-length', 'content-type')
                            }
                        current_url = next_url
                        continue

                    self.send_response(resp.status, resp.reason)
                    self._send_cors_headers()
                    self.send_header('X-Neon-Final-URL', current_url)
                    for rkey, rval in resp.headers.items():
                        rlow = rkey.lower()
                        if rlow in _STRIP_RESP_HEADERS:
                            continue
                        if rlow == 'content-length' and resp.chunked:
                            continue
                        self.send_header(rkey, rval)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True

                    if method != 'HEAD':
                        while True:
                            chunk = resp.read1(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    return
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                    return
                except (OSError, http.client.HTTPException, ValueError) as exc:
                    self._send_error_response(502, f'Upstream error: {exc}')
                    return
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass

            self._send_error_response(502, 'Too many redirects')

        do_GET = _handle_proxy
        do_HEAD = _handle_proxy
        do_POST = _handle_proxy
        do_PUT = _handle_proxy
        do_DELETE = _handle_proxy
        do_PATCH = _handle_proxy

    server = _ProxyServer(('127.0.0.1', 0), _ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy_base = f'http://127.0.0.1:{server.server_port}{_PROXY_PREFIX}{token}'
    return proxy_base, server


def player_window(app):
    title = {'beletfilm': 'BeletFilm', 'alemtv': 'Alem TV'}[app]
    base = Path('/usr/share') / app
    GLib.set_prgname(app)
    GLib.set_application_name(title)
    proxy_base, proxy_server = _start_proxy(app)
    window = Gtk.Window(title=title)
    window._neon_proxy_server = proxy_server
    window.set_default_size(1152, 648)
    window.set_icon_name(app)
    window.connect('destroy', lambda *_: (proxy_server.server_close(), Gtk.main_level() > 0 and Gtk.main_quit()))
    data = WebKit2.WebsiteDataManager(base_data_directory=str(Path(GLib.get_user_data_dir()) / app),
                                      base_cache_directory=str(Path(GLib.get_user_cache_dir()) / app))
    context = WebKit2.WebContext.new_with_website_data_manager(data)
    view = WebKit2.WebView.new_with_context(context)
    settings = view.get_settings()
    settings.set_media_playback_requires_user_gesture(False)
    settings.set_enable_mediasource(True)
    settings.set_allow_file_access_from_file_urls(True)
    # Only the bundled file:// client receives the loopback proxy token.
    ucm = view.get_user_content_manager()
    ucm.add_script(WebKit2.UserScript.new(
        f'if (location && location.protocol === "file:") window.__NEON_PROXY_BASE__ = {json.dumps(proxy_base)};\n',
        WebKit2.UserContentInjectedFrames.TOP_FRAME,
        WebKit2.UserScriptInjectionTime.START,
        None,
        None,
    ))
    origins = (base / 'origins.txt').read_text().splitlines()
    origins.append(proxy_base.rsplit(_PROXY_PREFIX, 1)[0] + '/*')
    view.set_cors_allowlist(origins)
    view.connect('close', lambda *_: window.destroy())
    # Fit the 1920x1080 TV layouts on a laptop and when resizing the window.
    view.connect('size-allocate', lambda w, a: w.set_zoom_level(max(0.1, min(a.width / 1920, a.height / 1080))))

    def key_press(_, event):
        if event.keyval == Gdk.KEY_F11:
            if window.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
                window.unfullscreen()
            else:
                window.fullscreen()
            return True
        return False

    window.connect('key-press-event', key_press)
    home = (base / 'index.html').as_uri()
    if app == 'beletfilm':
        # Keep the desktop website's normal origin, cookies and CORS policy;
        # never forward tokens or pairing codes from the bundled TV client.
        stack = Gtk.Stack()
        stack.add_named(view, 'client')
        window.add(stack)
        header = Gtk.HeaderBar(title=title, show_close_button=True)
        switch = Gtk.ToggleButton(label='Веб-версия belet.tm')
        header.pack_end(switch)
        window.set_titlebar(header)

        def switch_mode(button):
            if button.get_active():
                view.load_uri('about:blank')  # stop any outstanding QR polling
                web = WebKit2.WebView.new_with_context(context)
                web_settings = web.get_settings()
                web_settings.set_media_playback_requires_user_gesture(False)
                web_settings.set_enable_mediasource(True)
                stack.add_named(web, 'website')
                web.show()
                stack.set_visible_child(web)
                web.load_uri('https://belet.tm/')
                web.grab_focus()
            else:
                stack.get_child_by_name('website').destroy()
                stack.set_visible_child(view)
                view.load_uri(home)
                view.grab_focus()

        # Website login may use persistent cookies rather than localStorage.
        context.get_cookie_manager().set_persistent_storage(
            str(Path(data.get_base_data_directory()) / 'web-cookies.sqlite'),
            WebKit2.CookiePersistentStorage.SQLITE)
        switch.connect('toggled', switch_mode)
    else:
        window.add(view)
    view.load_uri(home)
    window.show_all()
    view.grab_focus()
    return window


if __name__ == '__main__':
    main_window = player_window(Path(sys.argv[0]).name)
    Gtk.main()
