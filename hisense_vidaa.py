"""Hisense / Toshiba VIDAA Smart TV discovery, pairing, remote, app install, and LAN/vidaahub portal."""

from __future__ import annotations

import atexit
import base64
from dataclasses import dataclass, field
import email.utils
import gzip
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET


MQTT_PORT = 36669
SSDP_MULTICAST = '239.255.255.250'
SSDP_BROADCAST = '255.255.255.255'
SSDP_PORT = 1900
SSDP_MEDIA_RENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:1'
UPNP_PORTS = (18400, 38400, 80)

CLIENT_ID_PATTERN = '38D65DC30F45109A369A86FCE866A85B'
XOR_TIMESTAMP_MASK = 6239759785777146216
MODERN_SALT = 'h!i@s#$v%i^d&a*a'
STANDARD_SALT = 'h*i&s%e!r^v0i1c9'
OPERATION = 'vidaacommon_001'
AUTH_PROFILES = ('static', 'modern', 'middle', 'remotenow')
CLIENT_BUNDLE_MASK = b'k3'

PIN_WAIT = 60.0
PIN_RESULT_WAIT = 15.0
REFRESH_MARGIN = 12 * 3600
PAIR_FAILURES_BEFORE_NEW_UUID = 2

APP_PRESETS = {
    'belet': {
        'app_id': 'tm.belet.vidaa',
        'title': 'Belet',
        'url': 'https://belet.tm/',
        'icon_asset': 'belet_large_icon.png',
        'icon_fallback': 'belet_icon.png',
    },
    'alemtv': {
        'app_id': 'tmalemtvvidaa',
        'title': 'Alem TV',
        'url': 'http://iptv.tmalem.tv',
        'icon_asset': 'alemtv_large_icon.png',
        'icon_fallback': 'alemtv_icon.png',
    },
    'msbcallback': {
        'app_id': 'neonmsbcallback',
        'title': 'MSB Callback',
        'url': '',
        'icon_asset': 'msb_icon.png',
        'icon_fallback': 'msb_icon.png',
    },
}

VIDAA_KEY_MAP = {
    'power': 'KEY_POWER',
    'up': 'KEY_UP',
    'down': 'KEY_DOWN',
    'left': 'KEY_LEFT',
    'right': 'KEY_RIGHT',
    'ok': 'KEY_OK',
    'enter': 'KEY_OK',
    'select': 'KEY_OK',
    'back': 'KEY_RETURNS',
    'return': 'KEY_RETURNS',
    'returns': 'KEY_RETURNS',
    'home': 'KEY_HOME',
    'menu': 'KEY_MENU',
    'settings': 'KEY_MENU',
    'exit': 'KEY_EXIT',
    'info': 'KEY_INFO',
    'apps': 'KEY_APPS',
    'source': 'KEY_SOURCE',
    'vol_up': 'KEY_VOLUMEUP',
    'volume_up': 'KEY_VOLUMEUP',
    'vol_down': 'KEY_VOLUMEDOWN',
    'volume_down': 'KEY_VOLUMEDOWN',
    'mute': 'KEY_MUTE',
    'ch_up': 'KEY_CHANNELUP',
    'channel_up': 'KEY_CHANNELUP',
    'ch_down': 'KEY_CHANNELDOWN',
    'channel_down': 'KEY_CHANNELDOWN',
    'play': 'KEY_PLAY',
    'pause': 'KEY_PAUSE',
    'stop': 'KEY_STOP',
    'red': 'KEY_RED',
    'green': 'KEY_GREEN',
    'yellow': 'KEY_YELLOW',
    'blue': 'KEY_BLUE',
}


def resource_root() -> str:
    return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))


def read_asset(name: str) -> bytes | None:
    path = os.path.join(resource_root(), 'assets', name)
    if not os.path.isfile(path):
        return None
    with open(path, 'rb') as f:
        return f.read()


def default_data_dir() -> str:
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or os.path.expanduser('~')
    elif sys.platform == 'darwin':
        base = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    d = os.path.join(base, 'NEON_LG_Tool')
    os.makedirs(d, exist_ok=True)
    return d


_VPN_TUN_IPS = frozenset({'172.19.0.1'})


def _probe_udp_source_ip(target_ip: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_ip, 36669))
        return sock.getsockname()[0] or ''
    except OSError:
        return ''
    finally:
        sock.close()


def _is_usable_lan_ip(ip: str) -> bool:
    clean = (ip or '').strip()
    if not clean or clean == '0.0.0.0':
        return False
    return not (clean.startswith('127.') or clean.startswith('169.254.'))


def list_local_ipv4_addresses() -> list[str]:
    """Return local non-loopback/non-APIPA IPv4 addresses sorted with LAN/ICS first."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(ip: str) -> None:
        clean = (ip or '').strip()
        if not _is_usable_lan_ip(clean) or clean in seen:
            return
        seen.add(clean)
        found.append(clean)

    for probe in ('192.168.137.2', '192.168.1.254', '192.168.0.254', '10.0.0.1', '8.8.8.8'):
        _add(_probe_udp_source_ip(probe))

    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(item[4][0])
    except OSError:
        pass
    try:
        _add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass

    def _rank(ip: str) -> tuple[int, str]:
        if ip == '192.168.137.1':
            return (0, ip)
        if ip in _VPN_TUN_IPS:
            return (4, ip)
        if ip.startswith('192.168.'):
            return (1, ip)
        if ip.startswith('10.') or ip.startswith('172.'):
            return (2, ip)
        return (3, ip)

    return sorted(found, key=_rank)


def get_local_ip_for_tv(tv_ip: str | None = None) -> str:
    clean_tv = (tv_ip or '').strip()
    cands = list_local_ipv4_addresses()
    if clean_tv and not clean_tv.startswith('127.'):
        parts = clean_tv.split('.')
        if len(parts) == 4:
            prefix = '.'.join(parts[:3]) + '.'
            for ip in cands:
                if ip.startswith(prefix) and ip not in _VPN_TUN_IPS:
                    return ip
        direct = _probe_udp_source_ip(clean_tv)
        if _is_usable_lan_ip(direct) and direct not in _VPN_TUN_IPS:
            return direct
    for ip in cands:
        if ip not in _VPN_TUN_IPS:
            return ip
    if cands:
        return cands[0]
    if not clean_tv:
        fallback = _probe_udp_source_ip('8.8.8.8')
        if _is_usable_lan_ip(fallback):
            return fallback
    return '127.0.0.1'


# ---------------------------------------------------------------------------
# Client certificate & TLS context
# ---------------------------------------------------------------------------

def decode_client_bundle(blob: bytes) -> tuple[bytes, bytes]:
    import zlib
    raw = zlib.decompress(
        bytes(b ^ CLIENT_BUNDLE_MASK[i % 2] for i, b in enumerate(base64.b85decode(blob)))
    )
    marker = b'-----END CERTIFICATE-----'
    idx = raw.find(marker)
    if idx < 0:
        raise ValueError('Invalid VIDAA client certificate bundle')
    idx += len(marker)
    return raw[:idx] + b'\n', raw[idx:].strip() + b'\n'


def load_client_pem_key(bundle_path: str | None = None) -> tuple[bytes, bytes]:
    if not bundle_path:
        bundle_path = os.path.join(resource_root(), 'assets', 'vidaa_client.bundle')
    if not os.path.isfile(bundle_path):
        raise FileNotFoundError(
            f'VIDAA TLS client bundle missing: {bundle_path}'
        )
    with open(bundle_path, 'rb') as f:
        return decode_client_bundle(f.read())


def create_vidaa_ssl_context(bundle_path: str | None = None) -> ssl.SSLContext:
    cert, key = load_client_pem_key(bundle_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if hasattr(ssl, 'OP_LEGACY_SERVER_CONNECT'):
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    with tempfile.TemporaryDirectory(prefix='neon-vidaa-tls-') as td:
        cert_p = os.path.join(td, 'client.pem')
        key_p = os.path.join(td, 'client.key')
        for p, data in ((cert_p, cert), (key_p, key)):
            with open(p, 'wb') as f:
                f.write(data)
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        ctx.load_cert_chain(cert_p, key_p)
    return ctx


# ---------------------------------------------------------------------------
# VIDAA MQTT Auth & Topic Helpers
# ---------------------------------------------------------------------------

def _md5_upper(s: str) -> str:
    return hashlib.md5(s.encode('utf-8')).hexdigest().upper()


def new_device_uuid() -> str:
    raw = bytearray(uuid.uuid4().bytes[:6])
    raw[0] = (raw[0] & 0xFE) | 0x02  # locally administered unicast MAC
    return ':'.join(f'{b:02x}' for b in raw)


def session_credentials(
    device_uuid: str,
    tv_timestamp: int,
    token: str | None = None,
    *,
    auth_profile: str = 'modern',
    brand: str = 'his',
) -> tuple[str, str, str]:
    if auth_profile not in AUTH_PROFILES:
        raise ValueError(f'Unsupported VIDAA auth profile: {auth_profile}')
    clean = device_uuid.replace('-', ':').lower()
    brand_clean = re.sub(r'[^a-z0-9]', '', (brand or 'his').lower()) or 'his'
    race = _md5_upper(f'{CLIENT_ID_PATTERN}${clean}')[:6]
    client_id = f'{clean}${brand_clean}${race}_{OPERATION}'
    if auth_profile == 'static':
        return client_id, 'hisenseservice', token if token is not None else 'multimqttservice'
    ts = int(tv_timestamp)
    user_ts = ts if auth_profile == 'remotenow' else (ts ^ XOR_TIMESTAMP_MASK)
    username = f'{brand_clean}${user_ts}'
    if token is not None:
        return client_id, username, token
    digit = sum(int(ch) for ch in str(abs(ts))) % 10
    salt = MODERN_SALT if auth_profile == 'modern' else STANDARD_SALT
    val_md5 = _md5_upper(f'{brand_clean}{digit}{salt}')[:6]
    return client_id, username, _md5_upper(f'{ts}${val_md5}')


def tv_topics(client_id: str) -> dict[str, str]:
    mob = f'/remoteapp/mobile/{client_id}/'
    return {
        'ui': f'/remoteapp/tv/ui_service/{client_id}/',
        'platform': f'/remoteapp/tv/platform_service/{client_id}/',
        'remote': f'/remoteapp/tv/remote_service/{client_id}/',
        'mobile': mob,
        'broadcast': '/remoteapp/mobile/broadcast/',
        'auth_reply': mob + 'ui_service/data/authentication',
        'pin_reply': mob + 'ui_service/data/authenticationcode',
        'applist_reply': mob + 'ui_service/data/applist',
        'token_reply': mob + 'platform_service/data/tokenissuance',
    }


def validate_app_url(url: str) -> str:
    val = (url or '').strip()
    if not val:
        raise ValueError('URL приложения пуст')
    low = val.lower()
    if low.startswith(('javascript:', 'vbscript:')):
        raise ValueError('Небезопасная схема URL')
    if not low.startswith(('https://', 'http://', 'data:text/html', 'file:///')):
        raise ValueError('URL должен начинаться с https://, http://, data:text/html или file:///')
    return val


def sanitize_app_id(value: str, fallback_title: str = 'CustomApp') -> str:
    s = re.sub(r'[^A-Za-z0-9._-]', '', (value or '').strip())
    if s:
        return s[:64]
    slug = re.sub(r'[^a-z0-9]', '', (fallback_title or '').lower())
    return ('neon.' + (slug or 'customapp'))[:64]


def install_payload(app_id: str, name: str, url: str, image: str = '') -> str:
    return json.dumps({
        'type': 'app_install',
        'app_info': {
            'Title': name,
            'StoreType': 99,
            'mediaId': app_id,
            'Id': app_id,
            'Image': image or '',
            'URL': url,
            'configUrlDownload': 0,
            'configUrl': '',
        },
    }, ensure_ascii=False)


def launch_payload(app_id: str, name: str, url: str, store_type: int | None = None) -> str:
    payload = {
        'appId': app_id,
        'name': name,
        'url': url,
        'urlType': 37,
        'appName': name,
        'appUrl': url,
    }
    if store_type is not None:
        try:
            payload['storeType'] = int(store_type)
        except (TypeError, ValueError):
            pass
    return json.dumps(payload, ensure_ascii=False)


def parse_applist(payload: str) -> list[dict] | None:
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append({
            'appId': str(item.get('appId') or item.get('id') or item.get('Id') or item.get('mediaId') or ''),
            'name': str(item.get('name') or item.get('appName') or item.get('Title') or ''),
            'url': str(item.get('url') or item.get('appUrl') or item.get('URL') or ''),
            'icon': str(item.get('icon') or item.get('iconUrl') or item.get('Image') or ''),
            'storeType': item.get('storeType', item.get('StoreType', 99)),
            'raw': item,
        })
    return out


def normalize_tile_url(url: str) -> str:
    value = (url or '').strip()
    if not value:
        return ''
    if value.lower().startswith('data:'):
        return value
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.hostname:
        return value[:-1] if len(value) > 1 and value.endswith('/') else value
    try:
        port = parsed.port
    except ValueError:
        return value
    default_port = {'http': 80, 'https': 443}.get(parsed.scheme.lower())
    host = parsed.hostname.lower()
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    netloc = host
    if port is not None and port != default_port:
        netloc += f':{port}'
    path = parsed.path
    if len(path) > 1 and path.endswith('/'):
        path = path[:-1]
    elif path == '/':
        path = ''
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, parsed.fragment))


def tile_ids(tile: dict) -> list[str]:
    if not isinstance(tile, dict):
        return []
    raw = tile.get('raw') if isinstance(tile.get('raw'), dict) else {}
    ids: list[str] = []
    for src in (tile, raw):
        for key in ('appId', 'Id', 'id', 'mediaId'):
            val = src.get(key)
            if isinstance(val, (str, int)) and str(val).strip():
                clean = str(val).strip().lower()
                if clean not in ids:
                    ids.append(clean)
    return ids


def tile_matches_request(
    tile: dict, app_id: str, url: str, allow_hidden_url: bool = False
) -> bool:
    if not isinstance(tile, dict):
        return False
    if app_id.strip().lower() not in tile_ids(tile):
        return False
    wanted = normalize_tile_url(url)
    if not wanted:
        return True
    raw = tile.get('raw') if isinstance(tile.get('raw'), dict) else tile
    addrs = [
        str(raw.get(k)).strip()
        for k in ('url', 'appUrl', 'URL')
        if isinstance(raw.get(k), str) and raw.get(k).strip()
    ]
    if not addrs or all(a.upper().startswith('HTTP_APP_DEFAULT_URL') for a in addrs):
        return bool(allow_hidden_url)
    return all(normalize_tile_url(a) == wanted for a in addrs)


def find_browser_app(apps: list[dict] | None) -> dict | None:
    """Return the built-in TV Browser entry from a VIDAA applist, if present."""
    if not apps:
        return None
    for entry in apps:
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get('appId') or '').strip()
        eurl = str(entry.get('url') or '').strip().lower()
        if eid == '16' or eurl in ('browser', 'url16'):
            return entry
    for entry in apps:
        if not isinstance(entry, dict):
            continue
        ename = str(entry.get('name') or '').strip().lower()
        eurl = str(entry.get('url') or '').strip().lower()
        if 'browser' in ename and not eurl.startswith(('http://', 'https://', 'data:')):
            return entry
    return None


def resolve_vidaa_key(key: str) -> str:
    raw = (key or '').strip()
    if not raw:
        raise ValueError('Key name is empty')
    low = raw.lower()
    if low in VIDAA_KEY_MAP:
        return VIDAA_KEY_MAP[low]
    up = raw.upper()
    if up.startswith('KEY_'):
        return up
    return 'KEY_' + re.sub(r'[^A-Z0-9_]', '_', up)


# ---------------------------------------------------------------------------
# Pure-Python MQTT v3.1.1 TLS Client (no paho-mqtt dependency)
# ---------------------------------------------------------------------------

def _mqtt_str(s: str) -> bytes:
    b = s.encode('utf-8')
    return struct.pack('!H', len(b)) + b


def _encode_rem_len(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n > 0:
            byte |= 0x80
        out.append(byte)
        if n == 0:
            break
    return bytes(out)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('MQTT connection closed by peer')
        buf.extend(chunk)
    return bytes(buf)


def _read_packet(sock: socket.socket) -> tuple[int, bytes]:
    first = _recv_exact(sock, 1)[0]
    mult = 1
    rem_len = 0
    for _ in range(4):
        digit = _recv_exact(sock, 1)[0]
        rem_len += (digit & 0x7F) * mult
        if (digit & 0x80) == 0:
            break
        mult *= 128
    payload = _recv_exact(sock, rem_len) if rem_len > 0 else b''
    return first, payload


class MqttTlsClient:
    """Minimal, thread-safe MQTT 3.1.1 over TLS client for VIDAA port 36669."""

    def __init__(
        self,
        host: str,
        port: int = MQTT_PORT,
        *,
        client_id: str,
        username: str,
        password: str,
        ssl_context: ssl.SSLContext | None = None,
        keepalive: int = 30,
        socket_factory=None,
    ):
        self.host = host
        self.port = int(port)
        self.client_id = client_id
        self.username = username
        self.password = password
        self.ssl_context = ssl_context
        self.keepalive = int(keepalive)
        self._socket_factory = socket_factory
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._running = False
        self._closed = False
        self._next_pid = 1
        self.messages: list[tuple[str, str]] = []
        self._subacks: dict[int, list[int]] = {}
        self._reader_thread: threading.Thread | None = None
        self._ping_thread: threading.Thread | None = None
        self.connect_rc: int | None = None

    def connect(self, timeout: float = 5.0) -> int:
        if self._socket_factory is not None:
            sock = self._socket_factory(self.host, self.port, timeout)
        else:
            ctx = self.ssl_context or create_vidaa_ssl_context()
            raw = socket.create_connection((self.host, self.port), timeout=timeout)
            sock = ctx.wrap_socket(raw, server_hostname=self.host)
        sock.settimeout(timeout)
        self._sock = sock

        var_hdr = b'\x00\x04MQTT\x04\xc2' + struct.pack('!H', self.keepalive)
        body = _mqtt_str(self.client_id) + _mqtt_str(self.username) + _mqtt_str(self.password)
        pkt = b'\x10' + _encode_rem_len(len(var_hdr) + len(body)) + var_hdr + body
        sock.sendall(pkt)

        while True:
            hdr, payload = _read_packet(sock)
            ptype = hdr >> 4
            if ptype == 2:  # CONNACK
                if len(payload) < 2:
                    raise ConnectionError('Malformed MQTT CONNACK')
                self.connect_rc = payload[1]
                break

        if self.connect_rc != 0:
            self.close()
            return self.connect_rc

        sock.settimeout(1.0)
        self._running = True
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name='neon-vidaa-mqtt-read'
        )
        self._reader_thread.start()
        self._ping_thread = threading.Thread(
            target=self._ping_loop, daemon=True, name='neon-vidaa-mqtt-ping'
        )
        self._ping_thread.start()
        return 0

    def _send_bytes(self, data: bytes) -> None:
        with self._send_lock:
            if not self._sock or self._closed:
                raise ConnectionError('MQTT socket is closed')
            self._sock.sendall(data)

    def _read_loop(self) -> None:
        sock = self._sock
        while self._running and sock is not None:
            try:
                hdr, payload = _read_packet(sock)
            except socket.timeout:
                continue
            except OSError:
                break
            ptype = hdr >> 4
            if ptype == 3:  # PUBLISH
                qos = (hdr >> 1) & 0x03
                if len(payload) < 2:
                    continue
                tlen = struct.unpack('!H', payload[:2])[0]
                pos = 2 + tlen
                topic = payload[2:pos].decode('utf-8', 'replace')
                if qos > 0:
                    if len(payload) < pos + 2:
                        continue
                    pid = struct.unpack('!H', payload[pos:pos + 2])[0]
                    pos += 2
                    if qos == 1:
                        try:
                            self._send_bytes(b'\x40\x02' + struct.pack('!H', pid))
                        except OSError:
                            pass
                text = payload[pos:].decode('utf-8', 'replace')
                with self._cond:
                    self.messages.append((topic, text))
                    self._cond.notify_all()
            elif ptype == 9:  # SUBACK
                if len(payload) >= 3:
                    pid = struct.unpack('!H', payload[:2])[0]
                    codes = list(payload[2:])
                    with self._cond:
                        self._subacks[pid] = codes
                        self._cond.notify_all()
            elif ptype == 14:  # DISCONNECT
                break
        with self._cond:
            self._running = False
            self._cond.notify_all()

    def _ping_loop(self) -> None:
        interval = max(5.0, self.keepalive * 0.5)
        next_ping = time.monotonic() + interval
        while self._running:
            time.sleep(0.5)
            if not self._running:
                break
            if time.monotonic() >= next_ping:
                try:
                    self._send_bytes(b'\xc0\x00')
                except OSError:
                    break
                next_ping = time.monotonic() + interval

    def subscribe(self, topics: list[str], timeout: float = 3.0) -> list[int]:
        if isinstance(topics, str):
            topics = [topics]
        if not topics:
            return []
        with self._send_lock:
            pid = self._next_pid
            self._next_pid = (pid % 65535) + 1
            body = struct.pack('!H', pid)
            for t in topics:
                body += _mqtt_str(t) + b'\x00'  # QoS 0
            pkt = b'\x82' + _encode_rem_len(len(body)) + body
            if not self._sock or self._closed:
                raise ConnectionError('MQTT socket is closed')
            self._sock.sendall(pkt)

        end = time.monotonic() + timeout
        with self._cond:
            while pid not in self._subacks and self._running:
                rem = end - time.monotonic()
                if rem <= 0:
                    break
                self._cond.wait(rem)
            return list(self._subacks.get(pid, []))

    def publish(self, topic: str, payload: str = '') -> None:
        raw = payload.encode('utf-8') if isinstance(payload, str) else payload
        body = _mqtt_str(topic) + raw
        pkt = b'\x30' + _encode_rem_len(len(body)) + body
        self._send_bytes(pkt)

    def wait_for(self, suffix: str, timeout: float = 5.0, since: int = 0) -> str | None:
        end = time.monotonic() + timeout
        with self._cond:
            idx = max(0, int(since))
            while True:
                while idx < len(self.messages):
                    topic, payload = self.messages[idx]
                    idx += 1
                    if topic.endswith(suffix):
                        return payload
                if not self._running:
                    return None
                rem = end - time.monotonic()
                if rem <= 0:
                    return None
                self._cond.wait(rem)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._running = False
        sock = self._sock
        with self._cond:
            self._cond.notify_all()
        if sock is not None:
            try:
                sock.sendall(b'\xe0\x00')
            except OSError:
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        cur = threading.current_thread()
        for t in (self._reader_thread, self._ping_thread):
            if t is not None and t is not cur and t.is_alive():
                t.join(timeout=2.0)
        self._reader_thread = None
        self._ping_thread = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._sock = None
        with self._cond:
            self._cond.notify_all()


# ---------------------------------------------------------------------------
# SSDP Discovery & UPnP Descriptor Probe
# ---------------------------------------------------------------------------

@dataclass
class VidaaTvInfo:
    ip: str
    friendly_name: str = ''
    manufacturer: str = ''
    model_name: str = ''
    model_description: str = ''
    brand: str = 'his'
    transport_protocol: int | None = None
    is_vidaa: bool = False
    tv_ts: int | None = None
    mac: str = ''
    raw_xml: str = field(default='', repr=False)

    def to_dict(self) -> dict:
        return {
            'ip': self.ip,
            'name': self.friendly_name or self.ip,
            'manufacturer': self.manufacturer,
            'model': self.model_name or self.brand.upper(),
            'brand': self.brand,
            'transport_protocol': self.transport_protocol,
            'is_vidaa': self.is_vidaa,
            'mac': self.mac,
        }


def parse_descriptor_xml(ip: str, xml_text: str, tv_ts: int | None = None) -> VidaaTvInfo:
    friendly = ''
    manufacturer = ''
    model_name = ''
    desc = ''
    try:
        root = ET.fromstring(xml_text)
        for node in root.iter():
            tag = node.tag.rsplit('}', 1)[-1]
            txt = (node.text or '').strip()
            if tag == 'friendlyName' and txt and not friendly:
                friendly = txt
            elif tag == 'manufacturer' and txt and not manufacturer:
                manufacturer = txt
            elif tag == 'modelName' and txt and not model_name:
                model_name = txt
            elif tag == 'modelDescription' and txt and not desc:
                desc = txt
    except ET.ParseError:
        m_name = re.search(r'<friendlyName>([^<]+)<', xml_text)
        friendly = m_name.group(1).strip() if m_name else ''
        m_man = re.search(r'<manufacturer>([^<]+)<', xml_text)
        manufacturer = m_man.group(1).strip() if m_man else ''
        m_mod = re.search(r'<modelName>([^<]+)<', xml_text)
        model_name = m_mod.group(1).strip() if m_mod else ''
        m_desc = re.search(r'<modelDescription>(.*?)</modelDescription>', xml_text, re.S)
        desc = m_desc.group(1).strip() if m_desc else ''

    kv = {}
    for line in desc.splitlines():
        k, sep, v = line.partition('=')
        if sep:
            kv[k.strip().lower()] = v.strip()

    brand_raw = kv.get('brand', '').lower()
    brand = brand_raw if re.fullmatch(r'[a-z0-9]{1,8}', brand_raw) else 'his'
    tp_val = None
    if 'transport_protocol' in kv:
        try:
            tp_val = int(re.match(r'\d+', kv['transport_protocol']).group(0))
        except Exception:
            tp_val = None
    mac = kv.get('macwifi') or kv.get('macethernet') or kv.get('mac') or ''

    low = (xml_text + '\n' + desc).lower()
    is_vidaa = (
        'vidaa_support' in kv
        or 'transport_protocol' in kv
        or 'hisense' in low
        or 'vidaa' in low
        or brand_raw in ('his', 'tos')
    )
    return VidaaTvInfo(
        ip=ip,
        friendly_name=friendly or ip,
        manufacturer=manufacturer,
        model_name=model_name,
        model_description=desc[:240],
        brand=brand,
        transport_protocol=tp_val,
        is_vidaa=is_vidaa,
        tv_ts=tv_ts,
        mac=mac,
        raw_xml=xml_text,
    )


def fetch_tv_descriptor(ip: str, location_url: str | None = None, timeout: float = 2.0) -> VidaaTvInfo | None:
    urls = [location_url] if location_url else [
        f'http://{ip}:{port}/MediaServer/rendererdevicedesc.xml' for port in UPNP_PORTS
    ]
    for loc in urls:
        conn = None
        try:
            u = urllib.parse.urlsplit(loc)
            if u.scheme != 'http' or not u.hostname:
                continue
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
            target = u.path or '/'
            if u.query:
                target += '?' + u.query
            conn.request('GET', target)
            resp = conn.getresponse()
            body = resp.read().decode('utf-8', 'replace')
            if resp.status != 200 or not body.strip():
                continue
            tv_ts = None
            date_hdr = resp.getheader('Date', '')
            if date_hdr:
                try:
                    tv_ts = int(email.utils.parsedate_to_datetime(date_hdr).timestamp())
                except Exception:
                    tv_ts = None
            return parse_descriptor_xml(ip, body, tv_ts=tv_ts)
        except Exception:
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return None


def tv_timestamp(ip: str, timeout: float = 2.0) -> int:
    info = fetch_tv_descriptor(ip, timeout=timeout)
    if info and info.tv_ts:
        return info.tv_ts
    return int(time.time())


def _ssdp_location(packet: bytes) -> str | None:
    text = packet.decode('utf-8', 'replace')
    head = text[:32].upper()
    if head.startswith('NOTIFY'):
        nt = re.search(r'^NT:\s*(\S+)', text, re.I | re.M)
        nts = re.search(r'^NTS:\s*(\S+)', text, re.I | re.M)
        if (not nts or nts.group(1).lower() != 'ssdp:alive'
                or not nt or nt.group(1) != SSDP_MEDIA_RENDERER):
            return None
    elif not head.startswith('HTTP/1.1 200'):
        return None
    m = re.search(r'^Location:\s*(\S+)', text, re.I | re.M)
    return m.group(1) if m else None


def _directed_broadcasts() -> list[str]:
    targets = [SSDP_BROADCAST, SSDP_MULTICAST]
    try:
        locals_list = list_local_ipv4_addresses() or [get_local_ip_for_tv(None)]
        bcasts = []
        for local in locals_list:
            if local in _VPN_TUN_IPS:
                continue
            parts = local.split('.')
            if len(parts) == 4:
                bcasts.append(f'{parts[0]}.{parts[1]}.{parts[2]}.255')
        targets = bcasts + targets
    except Exception:
        pass
    return list(dict.fromkeys(targets))


def discover_vidaa_tvs(timeout: float = 2.5) -> list[VidaaTvInfo]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    locations: dict[str, str] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.35)
        targets = _directed_broadcasts()
        msg = '\r\n'.join([
            'M-SEARCH * HTTP/1.1',
            f'HOST: {SSDP_MULTICAST}:{SSDP_PORT}',
            'MAN: "ssdp:discover"',
            'MX: 2',
            f'ST: {SSDP_MEDIA_RENDERER}',
            'CONTENT-LENGTH: 0',
            '', '',
        ]).encode('ascii')
        for tgt in targets:
            try:
                sock.sendto(msg, (tgt, SSDP_PORT))
            except OSError:
                pass
        end = time.monotonic() + timeout
        retransmitted = False
        while time.monotonic() < end:
            try:
                data, addr = sock.recvfrom(4096)
                loc = _ssdp_location(data)
                if loc:
                    locations[addr[0]] = loc
            except socket.timeout:
                if not retransmitted:
                    retransmitted = True
                    for tgt in targets:
                        try:
                            sock.sendto(msg, (tgt, SSDP_PORT))
                        except OSError:
                            pass
            except OSError:
                break
    finally:
        sock.close()

    found: dict[str, VidaaTvInfo] = {}
    for host, loc in locations.items():
        info = fetch_tv_descriptor(host, loc, timeout=1.8)
        if info and info.is_vidaa:
            found[host] = info
    return sorted(found.values(), key=lambda t: t.ip)


# ---------------------------------------------------------------------------
# Session Store & Auth Flow
# ---------------------------------------------------------------------------

@dataclass
class VidaaSession:
    host: str = ''
    device_uuid: str = ''
    client_id: str = ''
    username: str = ''
    access_token: str = ''
    refresh_token: str = ''
    access_time: int = 0
    access_days: int = 0
    refresh_time: int = 0
    refresh_days: int = 0
    expires_at: float = 0.0
    pair_failures: int = 0
    auth_profile: str = 'modern'
    brand: str = 'his'
    friendly_name: str = ''
    model_name: str = ''

    @property
    def tokenless(self) -> bool:
        return self.auth_profile == 'static' and not self.access_token and not self.refresh_token

    @property
    def is_paired(self) -> bool:
        return bool(self.host and self.client_id and (self.access_token or self.tokenless))

    def set_tokens(self, tok: dict) -> None:
        access = tok.get('accesstoken')
        refresh_tok = tok.get('refreshtoken', self.refresh_token)
        if not isinstance(access, str) or not access:
            raise ValueError('Пустой access token в ответе ТВ')
        now = time.time()
        self.access_token = access
        self.refresh_token = str(refresh_tok or '')
        self.access_time = int(tok.get('accesstoken_time', now))
        self.access_days = max(1, int(tok.get('accesstoken_duration_day', self.access_days or 2)))
        self.refresh_time = int(tok.get('refreshtoken_time', now))
        self.refresh_days = max(1, int(tok.get('refreshtoken_duration_day', self.refresh_days or 30)))
        self.expires_at = now + self.access_days * 86400


class HisenseSessionStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(default_data_dir(), 'hisense_sessions.json')
        self._lock = threading.Lock()

    def _read_all(self) -> dict[str, dict]:
        if not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _write_all(self, data: dict[str, dict]) -> None:
        parent = os.path.dirname(self.path)
        os.makedirs(parent, exist_ok=True)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=parent, delete=False
            ) as f:
                pending = f.name
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                os.chmod(pending, 0o600)
            except OSError:
                pass
            os.replace(pending, self.path)
        finally:
            if pending and os.path.isfile(pending):
                os.remove(pending)

    def get(self, host: str) -> VidaaSession | None:
        with self._lock:
            item = self._read_all().get(host)
        if not isinstance(item, dict):
            return None
        known = set(VidaaSession.__dataclass_fields__)
        return VidaaSession(**{k: v for k, v in item.items() if k in known})

    def save(self, sess: VidaaSession) -> None:
        with self._lock:
            all_data = self._read_all()
            all_data[sess.host] = dict(sess.__dict__)
            self._write_all(all_data)

    def delete(self, host: str) -> bool:
        with self._lock:
            all_data = self._read_all()
            removed = host in all_data
            all_data.pop(host, None)
            self._write_all(all_data)
            return removed


def select_profile_order(
    preferred: str = 'auto',
    saved_profile: str | None = None,
    brand: str = 'his',
    transport_protocol: int | None = None,
) -> list[str]:
    if preferred in AUTH_PROFILES:
        base = (preferred,) + AUTH_PROFILES
    elif saved_profile in AUTH_PROFILES:
        base = (saved_profile,) + AUTH_PROFILES
    elif (brand or '').lower() == 'his' and transport_protocol is not None and transport_protocol >= 3290:
        base = ('modern', 'static', 'middle', 'remotenow')
    else:
        # Try static first to avoid throttling static auth on Toshiba/older VIDAA.
        base = AUTH_PROFILES
    return list(dict.fromkeys(base))


class _PairingAttempt:
    def __init__(
        self,
        host: str,
        auth_profile: str,
        controller: 'HisenseController',
    ):
        self.id = secrets.token_urlsafe(12)
        self.host = host
        self.preferred_profile = auth_profile
        self.controller = controller
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.submitted = threading.Event()
        self.done = threading.Event()
        self.state = 'starting'
        self.pin = ''
        self.error = ''
        self.deadline = 0.0
        self.active_profile = ''
        self.brand = 'his'
        self.session: VidaaSession | None = None

    def snapshot(self) -> dict:
        with self.lock:
            if self.state == 'awaiting_pin':
                return {
                    'ok': True,
                    'status': 'pin_required',
                    'pairing_id': self.id,
                    'host': self.host,
                    'auth_profile': self.active_profile,
                    'brand': self.brand,
                    'expires_in': max(0, int(self.deadline - time.monotonic())),
                }
            if self.state == 'complete':
                return {
                    'ok': True,
                    'status': 'paired',
                    'pairing_id': self.id,
                    'host': self.host,
                    'auth_profile': self.active_profile,
                    'brand': self.brand,
                }
            return {
                'ok': False,
                'status': self.state,
                'error': self.error or 'Ожидание ответа ТВ…',
            }

    def cancel(self) -> None:
        with self.lock:
            if self.state not in ('starting', 'awaiting_pin'):
                return
            self.state = 'cancelled'
            self.error = 'Запрос PIN отменён'
            self.submitted.set()

    def submit_pin(self, pin: str) -> None:
        if not re.fullmatch(r'\d{4}', (pin or '').strip()):
            raise ValueError('Введите 4 цифры PIN-кода с экрана ТВ')
        with self.lock:
            if self.state in ('confirming', 'complete'):
                return
            if self.state != 'awaiting_pin' or time.monotonic() >= self.deadline:
                raise ValueError(self.error or 'Срок действия PIN-кода истёк. Запросите заново.')
            self.pin = pin.strip()
            self.state = 'confirming'
            self.submitted.set()

    def run(self) -> None:
        mqtt_client: MqttTlsClient | None = None
        prev = self.controller.store.get(self.host)
        reuse = bool(
            prev and prev.device_uuid and prev.pair_failures < PAIR_FAILURES_BEFORE_NEW_UUID
        )
        device_uuid = prev.device_uuid if reuse else new_device_uuid()
        try:
            info = fetch_tv_descriptor(self.host, timeout=2.0)
            brand = (info.brand if info else 'his') or 'his'
            tv_ts = (info.tv_ts if info and info.tv_ts else None) or tv_timestamp(self.host)
            tp = info.transport_protocol if info else None
            self.brand = brand

            profiles = select_profile_order(
                self.preferred_profile,
                prev.auth_profile if reuse else None,
                brand=brand,
                transport_protocol=tp,
            )

            chosen_profile = ''
            cid = uname = ''
            attempted = []
            for profile in profiles:
                c_id, u_name, pwd = session_credentials(
                    device_uuid, tv_ts, None, auth_profile=profile, brand=brand
                )
                mqtt_id = (
                    'neon_' + new_device_uuid().replace(':', '')
                    if profile == 'static'
                    else c_id
                )
                cli = self.controller._make_mqtt(
                    self.host, client_id=mqtt_id, username=u_name, password=pwd
                )
                try:
                    rc = cli.connect(timeout=5.0)
                except Exception:
                    cli.close()
                    raise
                attempted.append(f'{profile}:rc={rc}')
                if rc == 0:
                    mqtt_client = cli
                    chosen_profile = profile
                    cid, uname = c_id, u_name
                    break
                cli.close()
                if rc not in (4, 5):
                    raise ConnectionError(f'ТВ отклонил подключение MQTT (rc={rc})')

            if mqtt_client is None:
                raise ConnectionError(
                    'ТВ отклонил авторизацию (' + '; '.join(attempted) + '). Проверьте сеть и модель.'
                )

            self.active_profile = chosen_profile
            topics = tv_topics(cid)
            mqtt_client.subscribe([
                topics['auth_reply'],
                topics['pin_reply'],
                topics['token_reply'],
                topics['applist_reply'],
            ])
            time.sleep(0.25)

            since = len(mqtt_client.messages)
            notif = None
            if chosen_profile == 'static':
                mqtt_client.publish(topics['ui'] + 'actions/gettvstate', '')
                notif = mqtt_client.wait_for('data/authentication', 4.0, since)
            if notif is None:
                mqtt_client.publish(
                    topics['ui'] + 'actions/vidaa_app_connect',
                    json.dumps({'app_version': 2, 'connect_result': 0, 'device_type': 'Mobile App'}),
                )
                notif = mqtt_client.wait_for('data/authentication', 8.0, since)

            tok_dict = None
            need_pin = True
            if notif is None and chosen_profile == 'static':
                # Some legacy static TVs accept commands without a PIN dialog.
                since_apps = len(mqtt_client.messages)
                mqtt_client.publish(topics['ui'] + 'actions/applist', '0')
                raw_apps = mqtt_client.wait_for('data/applist', 4.0, since_apps)
                if raw_apps and parse_applist(raw_apps) is not None:
                    need_pin = False

            if need_pin:
                with self.lock:
                    if self.state == 'cancelled':
                        return
                    self.state = 'awaiting_pin'
                    self.deadline = time.monotonic() + PIN_WAIT
                self.ready.set()

                if not self.submitted.wait(PIN_WAIT):
                    raise TimeoutError('Истекло время ожидания ввода PIN-кода')

                with self.lock:
                    if self.state == 'cancelled':
                        return
                    pin = self.pin
                    self.pin = ''

                since_pin = len(mqtt_client.messages)
                mqtt_client.publish(
                    topics['ui'] + 'actions/authenticationcode',
                    json.dumps({'authNum': int(pin)}),
                )
                pin_reply = mqtt_client.wait_for('ui_service/data/authenticationcode', PIN_RESULT_WAIT, since_pin)
                if pin_reply is None:
                    raise TimeoutError('ТВ не подтвердил PIN-код. Запросите новый PIN.')
                res_obj = json.loads(pin_reply)
                if not isinstance(res_obj, dict) or res_obj.get('result') != 1:
                    raise ValueError('Неверный или просроченный PIN-код')

                mqtt_client.publish(topics['platform'] + 'data/gettoken', json.dumps({'refreshtoken': ''}))
                mqtt_client.publish(topics['ui'] + 'actions/authenticationcodeclose', '')
                tok_raw = mqtt_client.wait_for(
                    'platform_service/data/tokenissuance',
                    5.0 if chosen_profile == 'static' else 12.0,
                    since_pin,
                )
                if tok_raw:
                    parsed = json.loads(tok_raw)
                    if isinstance(parsed, dict) and 'accesstoken' in parsed:
                        tok_dict = parsed
                if tok_dict is None and chosen_profile != 'static':
                    raise TimeoutError('PIN принят, но ТВ не выдал токен авторизации')
                if tok_dict is None and chosen_profile == 'static':
                    since_apps = len(mqtt_client.messages)
                    mqtt_client.publish(topics['ui'] + 'actions/applist', '0')
                    apps_raw = mqtt_client.wait_for('ui_service/data/applist', 6.0, since_apps)
                    if apps_raw is None or parse_applist(apps_raw) is None:
                        raise TimeoutError('PIN принят, но ТВ не подтвердил доступ к приложениям')

            sess = VidaaSession(
                host=self.host,
                device_uuid=device_uuid,
                client_id=cid,
                username=uname,
                pair_failures=0,
                auth_profile=chosen_profile,
                brand=brand,
                friendly_name=(info.friendly_name if info else self.host),
                model_name=(info.model_name if info else ''),
            )
            if tok_dict is not None:
                sess.set_tokens(tok_dict)
            self.controller.store.save(sess)
            self.session = sess
            with self.lock:
                self.state = 'complete'
        except Exception as e:
            if prev and reuse:
                prev.pair_failures += 1
                self.controller.store.save(prev)
            with self.lock:
                if self.state != 'cancelled':
                    self.state = 'failed'
                    self.error = str(e)
        finally:
            if mqtt_client is not None:
                mqtt_client.close()
            self.ready.set()
            self.done.set()


class HisenseController:
    """High-level Hisense VIDAA controller managing pairing, tokens, tiles, and remote."""

    def __init__(
        self,
        store: HisenseSessionStore | None = None,
        log_fn=None,
        mqtt_factory=None,
    ):
        self.store = store or HisenseSessionStore()
        self._log_fn = log_fn or (lambda msg: None)
        self._mqtt_factory = mqtt_factory
        self._op_lock = threading.RLock()
        self._pair_lock = threading.Lock()
        self._attempt: _PairingAttempt | None = None

    def _log(self, msg: str) -> None:
        try:
            self._log_fn(msg)
        except Exception:
            pass

    def _make_mqtt(self, host: str, *, client_id: str, username: str, password: str) -> MqttTlsClient:
        if self._mqtt_factory is not None:
            return self._mqtt_factory(
                host=host, port=MQTT_PORT, client_id=client_id, username=username, password=password
            )
        return MqttTlsClient(
            host,
            MQTT_PORT,
            client_id=client_id,
            username=username,
            password=password,
        )

    def start_pairing(self, host: str, auth_profile: str = 'auto') -> dict:
        ip = (host or '').strip()
        if not ip:
            return {'ok': False, 'error': 'Укажите IP-адрес Hisense VIDAA TV'}
        with self._pair_lock:
            if self._attempt is not None:
                self._attempt.cancel()
            att = _PairingAttempt(ip, auth_profile or 'auto', self)
            self._attempt = att
            threading.Thread(target=att.run, daemon=True, name='neon-hisense-pair').start()

        self._log(f'Запрос PIN-кода на Hisense TV {ip} (профиль: {auth_profile})…')
        if not att.ready.wait(25.0):
            att.cancel()
            return {'ok': False, 'error': 'ТВ не ответил на запрос PIN-кода (порт 36669)'}
        snap = att.snapshot()
        if snap.get('ok'):
            self._log(f'PIN-код показан на экране ТВ {ip} (профиль: {snap.get("auth_profile")})')
        else:
            self._log(f'Ошибка запроса PIN на {ip}: {snap.get("error")}')
        return snap

    def confirm_pairing(self, pairing_id: str, pin: str) -> dict:
        with self._pair_lock:
            att = self._attempt
        if att is None or not secrets.compare_digest(att.id, (pairing_id or '').strip()):
            return {'ok': False, 'error': 'Сессия ввода PIN не найдена. Нажмите «Подключить» снова.'}
        try:
            att.submit_pin(pin)
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        att.done.wait(PIN_RESULT_WAIT + 15.0)
        snap = att.snapshot()
        if snap.get('ok'):
            self._log(f'Hisense TV {att.host} сопряжён (профиль: {snap.get("auth_profile")})')
        else:
            self._log(f'Ошибка подтверждения PIN на {att.host}: {snap.get("error")}')
        return snap

    def cancel_pairing(self, pairing_id: str | None = None) -> dict:
        with self._pair_lock:
            att = self._attempt
            if att and (not pairing_id or secrets.compare_digest(att.id, pairing_id)):
                att.cancel()
        return {'ok': True}

    def close(self) -> None:
        with self._pair_lock:
            att = self._attempt
            self._attempt = None
        if att:
            att.cancel()
            att.done.wait(1.0)

    def _connect_saved(self, sess: VidaaSession, token: str | None) -> tuple[MqttTlsClient, dict[str, str]]:
        ts = 0 if sess.tokenless else tv_timestamp(sess.host)
        c_id, u_name, pwd = session_credentials(
            sess.device_uuid,
            ts,
            token,
            auth_profile=sess.auth_profile,
            brand=sess.brand or 'his',
        )
        c_id = sess.client_id or c_id
        u_name = sess.username or u_name
        mqtt_id = ('neon_' + new_device_uuid().replace(':', '')) if sess.auth_profile == 'static' else c_id
        cli = self._make_mqtt(sess.host, client_id=mqtt_id, username=u_name, password=pwd)
        rc = cli.connect(timeout=6.0)
        if rc != 0:
            raise PermissionError(f'rc={rc}')
        return cli, tv_topics(c_id)

    def _refresh_tokens(self, sess: VidaaSession) -> VidaaSession:
        if sess.tokenless or not sess.refresh_token:
            return sess
        cli, topics = self._connect_saved(sess, sess.refresh_token)
        try:
            cli.subscribe([topics['token_reply']])
            time.sleep(0.2)
            since = len(cli.messages)
            cli.publish(topics['platform'] + 'data/gettoken', json.dumps({'refreshtoken': sess.refresh_token}))
            raw = cli.wait_for('platform_service/data/tokenissuance', 10.0, since)
            if not raw:
                raise PermissionError('ТВ не выдал обновлённый токен — выполните повторное сопряжение')
            tok = json.loads(raw)
            sess.set_tokens(tok)
            self.store.save(sess)
            return sess
        finally:
            cli.close()

    def _run_authenticated(self, host: str, fn):
        with self._op_lock:
            sess = self.store.get(host)
            if sess is None or not sess.is_paired:
                raise RuntimeError(
                    f'ТВ {host} не сопряжён с NEON. Нажмите «Подключить (PIN)» и введите 4-значный PIN с экрана.'
                )
            if not sess.tokenless and sess.expires_at and time.time() >= (sess.expires_at - REFRESH_MARGIN):
                try:
                    sess = self._refresh_tokens(sess)
                except Exception as e:
                    self._log(f'Предупреждение обновления токена {host}: {e}')

            tok = None if sess.tokenless else sess.access_token
            try:
                cli, topics = self._connect_saved(sess, tok)
            except PermissionError:
                if sess.tokenless:
                    raise RuntimeError(f'ТВ {host} отклонил сохранённую сессию. Выполните повторное сопряжение (PIN).')
                sess = self._refresh_tokens(sess)
                cli, topics = self._connect_saved(sess, sess.access_token)

            try:
                return fn(cli, topics, sess)
            finally:
                cli.close()

    def list_apps(self, host: str) -> list[dict]:
        def op(cli: MqttTlsClient, topics: dict[str, str], _sess: VidaaSession):
            cli.subscribe([topics['applist_reply']])
            time.sleep(0.2)
            since = len(cli.messages)
            cli.publish(topics['ui'] + 'actions/applist', '0')
            raw = cli.wait_for('data/applist', 8.0, since)
            return parse_applist(raw) if raw else []
        apps = self._run_authenticated(host, op)
        self._log(f'Получен список приложений Hisense ({len(apps)} шт.)')
        return apps

    def install_tile(
        self,
        host: str,
        app_id: str,
        title: str,
        url: str,
        icon_url: str = '',
        wait_confirm: bool = True,
        auto_launch: bool = False,
        poll_delays: tuple[float, float] = (1.5, 3.5),
    ) -> dict:
        clean_url = validate_app_url(url)
        clean_id = sanitize_app_id(app_id, title)
        clean_title = (title or clean_id).strip()[:64]

        def op(cli: MqttTlsClient, topics: dict[str, str], _sess: VidaaSession):
            cli.subscribe([topics['applist_reply']])
            time.sleep(0.2)
            cli.publish(
                topics['ui'] + 'actions/uievent',
                install_payload(clean_id, clean_title, clean_url, icon_url or ''),
            )
            confirmed = False
            matched_tile: dict | None = None
            apps: list[dict] = []
            if wait_confirm:
                time.sleep(poll_delays[0])
                for attempt in range(2):
                    since = len(cli.messages)
                    cli.publish(topics['ui'] + 'actions/applist', '0')
                    raw = cli.wait_for('ui_service/data/applist', 6.0, since)
                    apps = parse_applist(raw) or []
                    allow_hidden = (attempt == 1)
                    for item in apps:
                        if tile_matches_request(item, clean_id, clean_url, allow_hidden_url=allow_hidden):
                            matched_tile = item
                            confirmed = True
                            break
                    if confirmed:
                        break
                    if attempt == 0 and poll_delays[1] > 0:
                        time.sleep(poll_delays[1])
            launched = False
            launched_via = ''
            launch_skipped_reason = ''
            browser = find_browser_app(apps)
            if auto_launch:
                if confirmed and matched_tile is not None:
                    m_id = matched_tile.get('appId') or clean_id
                    m_name = matched_tile.get('name') or clean_title
                    m_url = matched_tile.get('url') or clean_url
                    if str(m_url).upper().startswith('HTTP_APP_DEFAULT_URL'):
                        m_url = clean_url
                    m_store = matched_tile.get('storeType')
                    cli.publish(
                        topics['ui'] + 'actions/launchapp',
                        launch_payload(m_id, m_name, m_url, store_type=m_store),
                    )
                    launched = True
                    launched_via = 'tile'
                    time.sleep(0.5)
                elif not wait_confirm and not clean_url.lower().startswith('data:'):
                    cli.publish(
                        topics['ui'] + 'actions/launchapp',
                        launch_payload(clean_id, clean_title, clean_url),
                    )
                    launched = True
                    launched_via = 'direct'
                    time.sleep(0.5)
                elif clean_url.lower().startswith(('http://', 'https://')) and browser is not None:
                    b_url = str(browser.get('url') or '').strip().lower()
                    if b_url != 'url16':
                        cli.publish(
                            topics['ui'] + 'actions/launchapp',
                            launch_payload(
                                browser.get('appId') or '16',
                                browser.get('name') or 'TV Browser',
                                clean_url,
                                store_type=browser.get('storeType'),
                            ),
                        )
                        launched = True
                        launched_via = 'tv_browser'
                        time.sleep(0.5)
                    else:
                        launch_skipped_reason = 'tv_browser_ignores_mqtt_url'
                elif clean_url.lower().startswith('data:'):
                    launch_skipped_reason = 'data_uri_rejected_by_tv'
                else:
                    launch_skipped_reason = 'tile_not_confirmed'
            return {
                'ok': True,
                'confirmed': confirmed,
                'status': 'installed' if confirmed else 'sent_unconfirmed',
                'app_id': clean_id,
                'title': clean_title,
                'url': clean_url,
                'launched': launched,
                'launched_via': launched_via,
                'browser_opened': (launched_via == 'tv_browser'),
                'launch_skipped_reason': launch_skipped_reason,
                'apps_count': len(apps),
                'browser_info': (
                    {'appId': browser.get('appId'), 'url': browser.get('url'), 'storeType': browser.get('storeType')}
                    if browser else None
                ),
            }

        res = self._run_authenticated(host, op)
        if res['confirmed']:
            self._log(f'Плитка «{clean_title}» ({clean_id}) подтверждена в списке приложений ТВ')
        else:
            if res.get('launched_via') == 'tv_browser':
                self._log(
                    f'Плитка «{clean_title}» не добавлена в applist по MQTT, но страница открыта через встроенный Браузер ТВ'
                )
            else:
                self._log(
                    f'ТВ не подтвердил установку плитки «{clean_title}» по MQTT '
                    '(чтобы избежать чёрного экрана, автозапуск отменён; откройте «Браузер ТВ» или /vidaa/selfinstall.html)'
                )
        return res

    def open_browser_url(self, host: str, url: str) -> dict:
        clean_url = validate_app_url(url)
        if not clean_url.lower().startswith(('http://', 'https://')):
            return {
                'ok': False,
                'launched': False,
                'browser_opened': False,
                'reason': 'requires_http_url',
                'error': 'Требуется http:// или https:// URL для Браузера ТВ',
                'url': clean_url,
            }

        def op(cli: MqttTlsClient, topics: dict[str, str], _sess: VidaaSession):
            cli.subscribe([topics['applist_reply']])
            time.sleep(0.2)
            since = len(cli.messages)
            cli.publish(topics['ui'] + 'actions/applist', '0')
            raw = cli.wait_for('data/applist', 6.0, since)
            apps = parse_applist(raw) or []
            browser = find_browser_app(apps)
            if not browser:
                return {
                    'ok': False,
                    'launched': False,
                    'browser_opened': False,
                    'reason': 'browser_not_in_applist',
                    'error': f'Встроенный Браузер ТВ не найден в applist; откройте вручную на ТВ: {clean_url}',
                    'url': clean_url,
                }
            if str(browser.get('url') or '').strip().lower() == 'url16':
                return {
                    'ok': False,
                    'launched': False,
                    'browser_opened': False,
                    'reason': 'tv_browser_url_ignored',
                    'error': f'Эта прошивка VIDAA (url16) не открывает URL по MQTT; откройте Браузер ТВ пультом и перейдите на {clean_url}',
                    'url': clean_url,
                    'browser': {'appId': browser.get('appId'), 'url': browser.get('url'), 'storeType': browser.get('storeType')},
                }
            cli.publish(
                topics['ui'] + 'actions/launchapp',
                launch_payload(
                    browser.get('appId') or '16',
                    browser.get('name') or 'TV Browser',
                    clean_url,
                    store_type=browser.get('storeType'),
                ),
            )
            time.sleep(0.4)
            return {
                'ok': True,
                'launched': True,
                'browser_opened': True,
                'url': clean_url,
                'browser': {'appId': browser.get('appId'), 'url': browser.get('url'), 'storeType': browser.get('storeType')},
            }

        res = self._run_authenticated(host, op)
        if res.get('launched'):
            self._log(f'Открыт Браузер ТВ ({res["browser"].get("appId")}) -> {clean_url}')
        else:
            self._log(
                f'Не удалось авто-открыть URL в Браузере ТВ ({res.get("reason")}): откройте вручную на ТВ {clean_url}'
            )
        return res

    def launch_app(self, host: str, app_id: str, title: str = '', url: str = '') -> dict:
        def op(cli: MqttTlsClient, topics: dict[str, str], _sess: VidaaSession):
            cli.subscribe([topics['applist_reply']])
            time.sleep(0.2)
            since = len(cli.messages)
            cli.publish(topics['ui'] + 'actions/applist', '0')
            raw = cli.wait_for('data/applist', 6.0, since)
            apps = parse_applist(raw) or []

            matched = None
            target_low = (app_id or '').strip().lower()
            title_low = (title or '').strip().lower()
            for a in apps:
                if (target_low and target_low in tile_ids(a)) or (
                    title_low and str(a.get('name') or '').strip().lower() == title_low
                ):
                    matched = a
                    break

            if matched is not None:
                t_id = matched.get('appId') or app_id
                t_title = matched.get('name') or title or t_id
                t_url = matched.get('url') or url
                if url and not str(url).lower().startswith('data:') and str(t_url).upper().startswith('HTTP_APP_DEFAULT_URL'):
                    t_url = url
                elif url and str(matched.get('storeType')) in ('99', '100', '98', '') and not matched.get('url'):
                    t_url = url
                t_store = matched.get('storeType')
                cli.publish(
                    topics['ui'] + 'actions/launchapp',
                    launch_payload(t_id, t_title, t_url or url, store_type=t_store),
                )
                time.sleep(0.4)
                return {'ok': True, 'launched': True, 'launched_via': 'tile', 'app_id': t_id, 'title': t_title, 'url': t_url}

            if url and str(url).lower().startswith(('http://', 'https://')):
                browser = find_browser_app(apps)
                if browser and str(browser.get('url') or '').strip().lower() != 'url16':
                    cli.publish(
                        topics['ui'] + 'actions/launchapp',
                        launch_payload(
                            browser.get('appId') or '16',
                            browser.get('name') or 'TV Browser',
                            url,
                            store_type=browser.get('storeType'),
                        ),
                    )
                    time.sleep(0.4)
                    return {
                        'ok': True,
                        'launched': True,
                        'launched_via': 'tv_browser',
                        'browser_opened': True,
                        'app_id': browser.get('appId') or '16',
                        'title': browser.get('name') or 'TV Browser',
                        'url': url,
                    }
                return {
                    'ok': False,
                    'launched': False,
                    'error': f'Приложение «{title or app_id}» не установлено на ТВ; откройте вручную в браузере ТВ: {url}',
                    'url': url,
                }

            if url and str(url).lower().startswith('data:'):
                return {
                    'ok': False,
                    'launched': False,
                    'error': f'Плитка «{title or app_id}» не установлена в списке ТВ; запуск Data URI без установки приводит к чёрному экрану. Используйте «Сервер NEON (LAN)» или Браузер ТВ.',
                }

            return {'ok': False, 'launched': False, 'error': f'Приложение «{title or app_id}» не найдено на ТВ.'}

        res = self._run_authenticated(host, op)
        if res.get('ok'):
            self._log(f'Отправлен запуск «{res["title"]}» ({res.get("launched_via", "tile")})')
        else:
            self._log(res.get('error') or f'Не удалось запустить «{title or app_id}»')
        return res

    def send_key(self, host: str, key: str) -> dict:
        resolved = resolve_vidaa_key(key)

        def op(cli: MqttTlsClient, topics: dict[str, str], _sess: VidaaSession):
            cli.publish(topics['remote'] + 'actions/sendkey', resolved)
            time.sleep(0.1)
            return {'ok': True, 'key': resolved}

        return self._run_authenticated(host, op)

    def show_toast(self, host: str, text: str, title: str = 'NEON') -> dict:
        payload = json.dumps({'message': text, 'title': title, 'duration': 4, 'type': 'notify'})

        def op(cli: MqttTlsClient, topics: dict[str, str], _sess: VidaaSession):
            cli.publish(topics['ui'] + 'actions/showmessage', payload)
            return {'ok': True}

        return self._run_authenticated(host, op)


# ---------------------------------------------------------------------------
# Built-in LAN Self-Installer Portal + Optional vidaahub.com DNS/HTTPS Bridge
# ---------------------------------------------------------------------------

def _build_vidaahub_selfsigned_cert(host_ip: str) -> tuple[bytes, bytes]:
    from datetime import datetime, timedelta, timezone
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'vidaahub.com'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'NEON TV Tool'),
    ])
    sans = [
        x509.DNSName('vidaahub.com'),
        x509.DNSName('*.vidaahub.com'),
        x509.DNSName('localhost'),
    ]
    try:
        sans.append(x509.IPAddress(ipaddress.ip_address(host_ip)))
    except Exception:
        pass
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _parse_dns_qname(packet: bytes) -> tuple[str, int, int] | None:
    if len(packet) < 12:
        return None
    qdcount = struct.unpack('!H', packet[4:6])[0]
    if qdcount < 1:
        return None
    pos = 12
    labels = []
    while pos < len(packet):
        length = packet[pos]
        pos += 1
        if length == 0:
            break
        if length & 0xC0:
            return None
        labels.append(packet[pos:pos + length].decode('ascii', 'replace'))
        pos += length
    if pos + 4 > len(packet):
        return None
    qtype, qclass = struct.unpack('!HH', packet[pos:pos + 4])
    return '.'.join(labels).lower(), qtype, qclass


def _build_dns_a_reply(query: bytes, target_ip: str) -> bytes:
    txid = query[:2]
    # Preserve question section up to first null byte + 4 bytes (QTYPE, QCLASS)
    pos = 12
    while pos < len(query):
        ln = query[pos]
        pos += 1
        if ln == 0:
            break
        pos += ln
    q_end = pos + 4
    question = query[12:q_end]
    header = txid + b'\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00'
    answer = b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04' + socket.inet_aton(target_ip)
    return header + question + answer


class _DnsBridgeServer:
    """Minimal UDP:53 responder: vidaahub.com -> PC LAN IP, other queries -> upstream DNS."""

    def __init__(self, target_ip: str, upstream: str = '8.8.8.8', port: int = 53):
        self.target_ip = target_ip
        self.upstream = upstream or '8.8.8.8'
        self.port = int(port)
        self._sock: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', self.port))
        sock.settimeout(0.5)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True, name='neon-vidaahub-dns')
        self._thread.start()
        return sock.getsockname()[1]

    def _serve(self) -> None:
        while self._running and self._sock is not None:
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = _parse_dns_qname(data)
            if not parsed:
                continue
            qname, qtype, _ = parsed
            if qname == 'vidaahub.com' or qname.endswith('.vidaahub.com'):
                if qtype == 1:
                    self._sock.sendto(_build_dns_a_reply(data, self.target_ip), addr)
                else:
                    # Empty NOERROR for AAAA etc. so TV prefers IPv4 A record immediately
                    txid = data[:2]
                    self._sock.sendto(txid + b'\x81\x80\x00\x01\x00\x00\x00\x00\x00\x00' + data[12:], addr)
            else:
                threading.Thread(
                    target=self._forward, args=(data, addr), daemon=True
                ).start()

    def _forward(self, query: bytes, client_addr) -> None:
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.settimeout(2.5)
        try:
            up.sendto(query, (self.upstream, 53))
            resp, _ = up.recvfrom(4096)
            if self._sock and self._running:
                self._sock.sendto(resp, client_addr)
        except OSError:
            pass
        finally:
            up.close()

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def build_tv_selfinstaller_html(base_origin: str, alem_portal: str = 'http://iptv.tmalem.tv') -> str:
    """ES5-compatible TV browser self-installer supporting HiUtils_createRequest & Hisense_installApp."""
    safe_origin = json.dumps(base_origin)
    safe_portal = json.dumps(alem_portal)
    return f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEON VIDAA Installer</title>
<style>
*{{box-sizing:border-box}}
html,body{{margin:0;padding:0;background:#070c10;color:#eef6f0;font-family:'Segoe UI',Arial,sans-serif}}
body{{padding:28px 36px}}
.hdr{{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1e332c;padding-bottom:14px;margin-bottom:20px}}
.logo{{font-size:28px;font-weight:800;color:#47e291;letter-spacing:1px}}
.sub{{font-size:14px;color:#93b0a2;margin-top:4px}}
.badges{{font-size:13px;color:#93b0a2;text-align:right;line-height:1.5}}
.grid{{display:flex;flex-wrap:wrap;margin:-8px}}
.card{{width:32%;min-width:290px;margin:8px;background:#0e171c;border:1px solid #1f332c;border-radius:14px;padding:18px}}
.card h2{{margin:0 0 8px 0;font-size:20px;color:#fff}}
.card p{{margin:0 0 14px 0;font-size:13px;color:#9ab6a7;line-height:1.45;min-height:42px}}
.btn{{display:inline-block;margin:4px 6px 4px 0;padding:10px 14px;border-radius:8px;border:1px solid #2b4e40;background:#142822;color:#e7fff1;font-size:14px;font-weight:700;cursor:pointer;text-decoration:none}}
.btn:focus,.btn:hover{{outline:2px solid #47e291;background:#1a3a2f;color:#fff}}
.btn-prim{{background:#19794c;border-color:#47e291}}
.btn-danger{{background:#34171c;border-color:#a84452;color:#ffd9df}}
.log{{margin-top:18px;background:#091014;border:1px solid #1b2f28;border-radius:12px;padding:14px;font-family:monospace;font-size:13px;color:#9ae6bd;white-space:pre-wrap;min-height:110px;max-height:240px;overflow:auto}}
</style>
</head>
<body>
<div class="hdr">
  <div>
    <div class="logo">NEON · HISENSE VIDAA INSTALLER</div>
    <div class="sub">Запуск и установка приложений через ТВ-браузер, Appinfo.json и мост Hisense</div>
  </div>
  <div class="badges" id="envInfo">Определение среды VIDAA…</div>
</div>

<div class="grid">
  <div class="card">
    <h2>MSB Callback</h2>
    <p>Офлайн-визитка с диагностикой сети, LAN/Wi-Fi IP и QR-кодами.</p>
    <button class="btn btn-prim" onclick="openUrl('/apps/msbcallback')">Открыть сейчас</button>
    <button class="btn" onclick="installLanTile('neonmsbcallback','MSB Callback',ORIGIN+'/apps/msbcallback','/assets/msb_icon.png')">Установить плитку (LAN)</button>
    <button class="btn" onclick="installOfflineFlash('neonmsbcallback','MSB Callback','/apps/msbcallback/index.html','/assets/msb_icon.png')">В память ТВ (Offline)</button>
    <button class="btn btn-danger" onclick="uninstallApp('neonmsbcallback','MSB Callback')">Удалить</button>
  </div>

  <div class="card">
    <h2>Alem TV</h2>
    <p>Standalone Smart TV клиент Alem TV с EPG, архивом и порталом оператора.</p>
    <button class="btn btn-prim" onclick="openUrl('/apps/alemtv')">Открыть сейчас</button>
    <button class="btn" onclick="installLanTile('tmalemtvvidaa','Alem TV',ORIGIN+'/apps/alemtv','/assets/alemtv_large_icon.png')">Установить плитку (LAN)</button>
    <button class="btn" onclick="installOfflineFlash('tmalemtvvidaa','Alem TV','/apps/alemtv/index.html','/assets/alemtv_large_icon.png')">В память ТВ (Offline)</button>
    <button class="btn btn-danger" onclick="uninstallApp('tmalemtvvidaa','Alem TV')">Удалить</button>
  </div>

  <div class="card">
    <h2>Belet TV</h2>
    <p>Полноценный Smart TV клиент Bëlet (QR/SMS-вход, каталог, поиск, сериалы, HLS-плеер — на общей базе с LG и Samsung).</p>
    <button class="btn btn-prim" onclick="openUrl('/apps/belet/')">Открыть сейчас</button>
    <button class="btn" onclick="installLanTile('tm.belet.vidaa','Belet',ORIGIN+'/apps/belet/','/assets/belet_large_icon.png')">Установить плитку (LAN + Прокси)</button>
    <button class="btn" onclick="installOfflineFlash('tm.belet.vidaa','Belet','/apps/belet/standalone.html','/assets/belet_large_icon.png','/apps/belet/standalone.html?nofonts=1')">В память ТВ (старые ТВ)</button>
    <button class="btn btn-danger" onclick="uninstallApp('tm.belet.vidaa','Belet')">Удалить</button>
  </div>
</div>

<div class="log" id="logBox">Готов к установке…</div>

<script>
(function(){{
  var ORIGIN = {safe_origin};
  var DEFAULT_PORTAL = {safe_portal};
  window.ORIGIN = ORIGIN;

  var logBox = document.getElementById('logBox');
  function log(msg) {{
    logBox.textContent = '[' + new Date().toTimeString().slice(0,8) + '] ' + msg + '\\n' + logBox.textContent;
  }}

  var hasHiUtils = (typeof window.HiUtils_createRequest === 'function');
  var hasBridge = (typeof window.Hisense_installApp === 'function');
  var model = '';
  var osVer = '';
  try {{ if (typeof Hisense_GetModelName === 'function') model = Hisense_GetModelName(); }} catch(e){{}}
  try {{ if (typeof Hisense_GetOSVersion === 'function') osVer = Hisense_GetOSVersion(); }} catch(e){{}}
  var envText = 'Модель: ' + (model || 'Браузер / Smart TV') + (osVer ? ' (' + osVer + ')' : '') +
    '<br>HiUtils (Appinfo.json): ' + (hasHiUtils ? 'ДОСТУПЕН' : 'нет (нужен https://vidaahub.com)') +
    '<br>Hisense_installApp: ' + (hasBridge ? 'ДОСТУПЕН' : 'нет (нужен https://vidaahub.com)');
  document.getElementById('envInfo').innerHTML = envText;

  try {{
    var probe = new XMLHttpRequest();
    probe.open('POST', ORIGIN + '/__tv_probe', true);
    probe.setRequestHeader('Content-Type', 'application/json');
    probe.send(JSON.stringify({{
      page: 'selfinstall',
      model: model,
      osVersion: osVer,
      ua: navigator.userAgent || '',
      href: String(window.location.href || ''),
      hasHiUtils: hasHiUtils,
      hasInstallApp: hasBridge
    }}));
  }} catch(e) {{}}

  function fetchText(path, cb) {{
    var x = new XMLHttpRequest();
    x.open('GET', path, true);
    x.onreadystatechange = function() {{
      if (x.readyState === 4) {{
        if (x.status >= 200 && x.status < 300) cb(null, x.responseText);
        else cb(new Error('HTTP ' + x.status));
      }}
    }};
    x.send();
  }}

  function refreshLauncher(appId) {{
    var msg = JSON.stringify({{
      type: 'APPMessage', MsgType: 'appControl', action: 'updateAppState',
      source: 'browser', startAppType: 2,
      param: {{ event: 'AllAppsUpdate', SubModuleName: 'AllApps', startFrom: '', appInfo: appId || '' }}
    }});
    try {{
      if (window.omi_platform && omi_platform.sendPlatformMessage) omi_platform.sendPlatformMessage(msg);
      else if (window.opera_omi && opera_omi.sendPlatformMessage) opera_omi.sendPlatformMessage(msg);
    }} catch(e) {{}}
  }}

  function readAppInfo() {{
    var cur = HiUtils_createRequest('fileRead', {{ path: 'websdk/Appinfo.json', mode: 6 }});
    if (!cur || !cur.ret) return null;
    try {{
      if (!cur.msg) return {{ AppInfo: [] }};
      var data = (typeof cur.msg === 'string') ? JSON.parse(cur.msg) : cur.msg;
      if (data && typeof data === 'object' && Array.isArray(data.AppInfo)) return data;
      return null;
    }} catch(e) {{
      return null;
    }}
  }}

  function hasRegistryEntry(reg, appId) {{
    if (!reg || !Array.isArray(reg.AppInfo)) return false;
    for (var i = 0; i < reg.AppInfo.length; i++) {{
      if (reg.AppInfo[i] && reg.AppInfo[i].Id === appId) return true;
    }}
    return false;
  }}

  function upsertAppInfo(appId, title, appUrl, iconUrl) {{
    var reg = readAppInfo();
    if (!reg) return {{ ret: false, msg: 'Appinfo.json недоступен для чтения (отказ во избежание затирания списка)' }};
    var kept = [];
    for (var i = 0; i < reg.AppInfo.length; i++) {{
      if (reg.AppInfo[i] && reg.AppInfo[i].Id !== appId) kept.push(reg.AppInfo[i]);
    }}
    kept.push({{
      Id: appId, AppName: title, Title: title,
      URL: appUrl, StartCommand: appUrl,
      IconURL: iconUrl, Icon_96: iconUrl, Image: iconUrl, Thumb: iconUrl,
      Type: 'Browser', InstallTime: new Date().toISOString().split('T')[0],
      RunTimes: 0, StoreType: 'custom', PreInstall: false
    }});
    reg.AppInfo = kept;
    var w = HiUtils_createRequest('fileWrite', {{ path: 'websdk/Appinfo.json', mode: 6, writedata: JSON.stringify(reg) }});
    if (w && w.ret) {{
      var verify = readAppInfo();
      if (verify && hasRegistryEntry(verify, appId)) {{
        w.verifiedOnDisk = true;
      }}
    }}
    return w;
  }}

  window.installLanTile = function(appId, title, appUrl, iconPath) {{
    if (hasHiUtils) {{
      var w = upsertAppInfo(appId, title, appUrl, ORIGIN + iconPath);
      if (w && w.ret) {{
        log('УСПЕХ! «' + title + '» записан в websdk/Appinfo.json (' + (w.verifiedOnDisk ? 'проверено на диске' : 'записано') + '). ПОЛНОСТЬЮ ПЕРЕЗАГРУЗИТЕ ТВ (выньте вилку из розетки на 10 сек), чтобы плитка появилась в меню.');
        return;
      }}
      log('Запись Appinfo.json не удалась (' + (w && w.msg ? w.msg : 'ошибка') + ') — пробую Hisense_installApp…');
    }}
    if (hasBridge) {{
      installBridgeApp(appId, title, appUrl, iconPath);
      return;
    }}
    log('На текущем адресе установка плиток заблокирована прошивкой. Нажмите «Открыть сейчас» — приложение запустится прямо здесь! Либо откройте https://vidaahub.com/.');
  }};

  window.installOfflineFlash = function(appId, title, htmlPath, iconPath, fallbackHtmlPath) {{
    if (!hasHiUtils) {{
      if (hasBridge && htmlPath.indexOf('belet') < 0) {{
        fetchText(fallbackHtmlPath || htmlPath, function(err, body){{
          if (err) return log('Ошибка загрузки: ' + err);
          var durl = 'data:text/html;charset=utf-8;base64,' + btoa(unescape(encodeURIComponent(body)));
          installBridgeApp(appId, title, durl, iconPath);
        }});
        return;
      }}
      log('Для записи в память ТВ нужна страница https://vidaahub.com/. Либо нажмите «Открыть сейчас» прямо в браузере ТВ.');
      return;
    }}
    log('Загрузка ' + title + ' и запись во внутреннюю память ТВ…');
    function writeFlash(pathToFetch, allowFallback) {{
      fetchText(pathToFetch, function(err, body){{
        if (err) return log('Ошибка загрузки HTML: ' + err);
        var flashPath = 'websdk/' + appId + '/index.html';
        var w1 = HiUtils_createRequest('fileWrite', {{ path: flashPath, mode: 6, writedata: body }});
        if (!w1 || !w1.ret) {{
          if (allowFallback && fallbackHtmlPath) {{
            log('Пробую облегчённую сборку без встроенных шрифтов…');
            return writeFlash(fallbackHtmlPath, false);
          }}
          return log('Ошибка fileWrite HTML: ' + JSON.stringify(w1));
        }}
        var localUrl = 'file:///APPS/' + flashPath;
        var fullIcon = ORIGIN + iconPath;
        var w2 = upsertAppInfo(appId, title, localUrl, fullIcon);
        if (w2 && w2.ret) {{
          log('Записано (' + localUrl + '). Перезагрузите ТВ (вынуть вилку на 10 сек). Если при открытии будет чёрный экран — ваша прошивка блокирует file://, используйте «Установить плитку (LAN)».');
        }} else {{
          log('Ошибка записи Appinfo.json: ' + JSON.stringify(w2));
        }}
      }});
    }}
    writeFlash(htmlPath, !!fallbackHtmlPath);
  }};

  window.installUrlFlash = function(appId, title, appUrl, iconPath) {{
    if (!hasHiUtils) return log('HiUtils_createRequest недоступен на этом домене');
    var w = upsertAppInfo(appId, title, appUrl, ORIGIN + iconPath);
    if (w && w.ret) {{ log('Сохранено в Appinfo.json: ' + title + '. Полностью перезагрузите ТВ (вилка из розетки на 10 сек).'); }}
    else log('Ошибка записи Appinfo.json: ' + JSON.stringify(w));
  }};

  window.installBridgeApp = function(appId, title, appUrl, iconPath) {{
    if (!hasBridge) return log('Hisense_installApp недоступен. Для его активации включите режим vidaahub.com или нажмите «Открыть сейчас».');
    var icon = ORIGIN + iconPath;
    log('Установка через Hisense_installApp: ' + title + '…');
    try {{
      Hisense_installApp(appId, title, icon, icon, icon, appUrl, 'store', function(status){{
        if (!status) {{
          refreshLauncher(appId);
          log('УСПЕХ: ' + title + ' установлен! Полностью перезагрузите ТВ, если плитка не появилась сразу.');
        }} else {{
          log('Hisense_installApp вернул статус ошибки: ' + status);
        }}
      }});
    }} catch(e) {{ log('Исключение Hisense_installApp: ' + e); }}
  }};

  window.uninstallApp = function(appId, title) {{
    var did = false;
    if (typeof window.Hisense_uninstallApp === 'function') {{
      did = true;
      Hisense_uninstallApp(appId, function(ok){{ log('Hisense_uninstallApp(' + appId + '): ' + ok); refreshLauncher(); }});
      Hisense_uninstallApp(title + 'debug', function(ok){{ log('Hisense_uninstallApp(' + title + 'debug): ' + ok); refreshLauncher(); }});
    }}
    if (hasHiUtils) {{
      var reg = readAppInfo();
      if (reg) {{
        did = true;
        reg.AppInfo = reg.AppInfo.filter(function(a){{ return a.Id !== appId; }});
        var w = HiUtils_createRequest('fileWrite', {{ path: 'websdk/Appinfo.json', mode: 6, writedata: JSON.stringify(reg) }});
        log('Удалено из websdk/Appinfo.json (' + appId + '): ' + JSON.stringify(w) + ' (перезагрузите ТВ)');
      }}
    }}
    if (!did) log('API удаления недоступен в текущем окне. Нажмите и удерживайте OK на плитке в меню ТВ.');
  }};

  window.openUrl = function(pathOrUrl) {{
    window.location.href = pathOrUrl;
  }};

  var btns = document.querySelectorAll('.btn');
  if (btns.length && btns[0].focus) try {{ btns[0].focus(); }} catch(e){{}}
}})();
</script>
</body>
</html>'''


_BELET_PROXY_BASE_DOMAINS = ('beletapis.com', 'belet.tm', 'belet.me')
_BELET_API_SCAN_HOSTS = frozenset({
    'beletapis.com',
    'api.belet.tm',
    'homepage.belet.me',
    'film.beletapis.com',
    'film-search.belet.me',
    'search.belet.me',
})
_BELET_HOST_TOKEN_RE = re.compile(r'https?://([a-z0-9.-]+)(?::\d+)?/', re.IGNORECASE)
_BELET_ALLOWED_REQ_HEADERS = frozenset({
    'accept',
    'authorization',
    'content-type',
    'fingerprint',
    'lang',
    'x-platform',
    'x-versioncode',
    'x-app-version',
    'app-version',
    'x-device-model',
    'x-player-session-id',
    'x-chunk-length',
    'x-code-sending-type',
    'range',
    'if-none-match',
    'if-modified-since',
})
_BELET_DEFAULT_CORS_HEADERS = (
    'Accept, Authorization, Content-Type, fingerprint, Lang, X-Platform, '
    'X-VersionCode, X-App-Version, app-version, X-Device-Model, '
    'X-Player-Session-Id, X-Chunk-Length, X-Code-Sending-Type, Range'
)
_BELET_FORWARD_RESP_HEADERS = (
    'content-type',
    'content-range',
    'accept-ranges',
    'etag',
    'last-modified',
    'cache-control',
)


def _is_safe_public_host(raw_host: str) -> bool:
    h = str(raw_host or '').strip().lower()
    if not h or len(h) > 253:
        return False
    if any(ch in h for ch in (' ', '/', '\\', '@', ':', '[', ']')):
        return False
    if not re.fullmatch(r'[a-z0-9.-]+', h):
        return False
    if '.' not in h or h.startswith('.') or h.endswith('.'):
        return False
    if h == 'localhost' or h.endswith(('.local', '.internal', '.lan', '.home.arpa')):
        return False
    try:
        ipaddress.ip_address(h)
        # Disallow raw IP addresses in the public domain proxy to avoid LAN/SSRF exposure.
        return False
    except ValueError:
        return True


def _is_belet_static_host(host: str) -> bool:
    h = str(host or '').lower()
    if not _is_safe_public_host(h):
        return False
    return any(h == d or h.endswith('.' + d) for d in _BELET_PROXY_BASE_DOMAINS)


def _remember_belet_hosts(text: str, register_fn) -> None:
    if not text or ('http://' not in text and 'https://' not in text):
        return
    for match in _BELET_HOST_TOKEN_RE.finditer(text):
        host = match.group(1).lower()
        if _is_safe_public_host(host):
            register_fn(host)


def _rewrite_proxy_url(raw_url: str, proxy_origin: str, up_host: str, register_fn) -> str:
    s = str(raw_url or '').strip()
    if not s or s.startswith(proxy_origin + '/__proxy/'):
        return s
    mh = re.match(r'^https?://([a-z0-9.-]+)(?::\d+)?(/.*)?$', s, re.IGNORECASE)
    if mh:
        h = mh.group(1).lower()
        if _is_safe_public_host(h):
            register_fn(h)
            return f'{proxy_origin}/__proxy/{h}{mh.group(2) or "/"}'
        return s
    mp = re.match(r'^//([a-z0-9.-]+)(/.*)?$', s)
    if mp:
        h = mp.group(1).lower()
        if _is_safe_public_host(h):
            register_fn(h)
            return f'{proxy_origin}/__proxy/{h}{mp.group(2) or "/"}'
        return s
    if s.startswith('/') and up_host and _is_safe_public_host(up_host):
        return f'{proxy_origin}/__proxy/{up_host}{s}'
    return s


def _rewrite_m3u8(text: str, proxy_origin: str, up_host: str, register_fn) -> str:
    def _attr_sub(m: re.Match) -> str:
        return 'URI="' + _rewrite_proxy_url(m.group(1), proxy_origin, up_host, register_fn) + '"'

    out_lines = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip('\r')
        trimmed = line.strip()
        if not trimmed:
            out_lines.append(line)
            continue
        if trimmed.startswith('#'):
            out_lines.append(re.sub(r'URI="([^"]+)"', _attr_sub, line))
            continue
        out_lines.append(_rewrite_proxy_url(trimmed, proxy_origin, up_host, register_fn))
    return '\n'.join(out_lines) + ('\n' if text.endswith('\n') else '')


_VIDAA_HTTP_PREFERRED_PORTS = tuple(range(18880, 18896))
_HOSTS_BEGIN_MARKER = '# BEGIN NEON VIDAAHUB'
_HOSTS_END_MARKER = '# END NEON VIDAAHUB'
_HOSTS_VIDAAHUB_DOMAINS = (
    'vidaahub.com',
    'www.vidaahub.com',
    'portal.vidaahub.com',
    'app.vidaahub.com',
    'api.vidaahub.com',
    'store.vidaahub.com',
    'tv.vidaahub.com',
)
_HOSTS_ATEXIT_REGISTERED = False


class _VidaaHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that disables SO_REUSEADDR on Windows to prevent silent port hijack."""
    allow_reuse_address = (os.name != 'nt')
    daemon_threads = True
    request_queue_size = 32


def _default_windows_hosts_path() -> str:
    sys_root = os.environ.get('SystemRoot') or r'C:\Windows'
    return os.path.join(sys_root, 'System32', 'drivers', 'etc', 'hosts')


def _strip_neon_hosts_block(content: str) -> str:
    out: list[str] = []
    inside = False
    for line in content.splitlines():
        s = line.strip()
        if s == _HOSTS_BEGIN_MARKER:
            inside = True
            continue
        if s == _HOSTS_END_MARKER:
            inside = False
            continue
        if not inside:
            out.append(line)
    res = '\n'.join(out)
    if content.endswith('\n') and res and not res.endswith('\n'):
        res += '\n'
    elif res and not res.endswith('\n'):
        res += '\n'
    return res


def _hosts_set_vidaahub(ip: str, hosts_path: str | None = None) -> tuple[bool, str]:
    """Write reversible vidaahub.com override into Windows hosts (or custom test path)."""
    global _HOSTS_ATEXIT_REGISTERED
    if hosts_path is None:
        if os.name != 'nt':
            return False, 'hosts fallback is only applicable on Windows'
        hosts_path = _default_windows_hosts_path()
    try:
        ip_obj = ipaddress.ip_address((ip or '').strip())
        clean_ip = str(ip_obj)
    except ValueError:
        return False, f'invalid IP: {ip!r}'

    try:
        existing = ''
        if os.path.exists(hosts_path):
            with open(hosts_path, 'r', encoding='utf-8', errors='replace') as f:
                existing = f.read()
        cleaned = _strip_neon_hosts_block(existing)
        block_lines = [_HOSTS_BEGIN_MARKER] + [f'{clean_ip} {dom}' for dom in _HOSTS_VIDAAHUB_DOMAINS] + [_HOSTS_END_MARKER]
        new_content = cleaned + '\n'.join(block_lines) + '\n'
        with open(hosts_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        if not _HOSTS_ATEXIT_REGISTERED and hosts_path == _default_windows_hosts_path():
            atexit.register(_hosts_clear_vidaahub)
            _HOSTS_ATEXIT_REGISTERED = True
        if os.name == 'nt':
            try:
                cflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                subprocess.run(['ipconfig', '/flushdns'], capture_output=True, timeout=5, creationflags=cflags)
            except Exception:
                pass
        return True, hosts_path
    except OSError as exc:
        return False, str(exc)


def _hosts_clear_vidaahub(hosts_path: str | None = None) -> bool:
    """Remove any NEON VIDAAHUB block from hosts file."""
    if hosts_path is None:
        if os.name != 'nt':
            return False
        hosts_path = _default_windows_hosts_path()
    try:
        if not os.path.exists(hosts_path):
            return False
        with open(hosts_path, 'r', encoding='utf-8', errors='replace') as f:
            existing = f.read()
        if _HOSTS_BEGIN_MARKER not in existing:
            return False
        cleaned = _strip_neon_hosts_block(existing)
        with open(hosts_path, 'w', encoding='utf-8') as f:
            f.write(cleaned)
        if os.name == 'nt':
            try:
                cflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                subprocess.run(['ipconfig', '/flushdns'], capture_output=True, timeout=5, creationflags=cflags)
            except Exception:
                pass
        return True
    except OSError:
        return False


def _inspect_windows_firewall_and_unblock(exe_path: str = '') -> dict:
    """Inspect Windows Firewall profiles/admin state & remove any inbound Block rules for NEON exe."""
    info: dict = {
        'admin': True,
        'removed_blocks': [],
        'profiles': {},
        'net': [],
        'warnings': [],
    }
    if os.name != 'nt':
        return info
    cflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    env = os.environ.copy()
    clean_exe = os.path.abspath(exe_path or sys.executable or '')
    env['NEON_FW_EXE'] = clean_exe if os.path.exists(clean_exe) else ''
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$exe=$env:NEON_FW_EXE; "
        "$isAdm=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); "
        "$removed=@(); "
        "if($exe){ "
        "Get-NetFirewallApplicationFilter -Program $exe | ForEach-Object { "
        "$r = $_ | Get-NetFirewallRule; "
        "if($r -and $r.Direction -eq 'Inbound' -and $r.Action -eq 'Block'){ "
        "$removed += [string]$r.DisplayName; "
        "$r | Remove-NetFirewallRule "
        "} } }; "
        "$profs=@{}; "
        "Get-NetFirewallProfile | ForEach-Object { "
        "$profs[[string]$_.Name]=@{enabled=[bool]$_.Enabled;allowInbound=[bool]$_.AllowInboundRules;defaultInbound=[string]$_.DefaultInboundAction} "
        "}; "
        "$cats=@(); "
        "Get-NetConnectionProfile | ForEach-Object { "
        "$cats += @{iface=[string]$_.InterfaceAlias;cat=[string]$_.NetworkCategory;idx=[int]$_.InterfaceIndex} "
        "}; "
        "@{admin=[bool]$isAdm;removed=$removed;profiles=$profs;net=$cats} | ConvertTo-Json -Compress -Depth 4"
    )
    try:
        res = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=8, creationflags=cflags, env=env,
        )
        raw = (res.stdout or '').strip()
        if res.returncode == 0 and raw.startswith('{'):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                info['admin'] = bool(parsed.get('admin', True))
                rem = parsed.get('removed') or []
                if isinstance(rem, str):
                    rem = [rem]
                info['removed_blocks'] = [str(x) for x in rem if x]
                if isinstance(parsed.get('profiles'), dict):
                    info['profiles'] = parsed['profiles']
                net = parsed.get('net') or []
                if isinstance(net, dict):
                    net = [net]
                info['net'] = net
    except Exception:
        pass

    if not info['admin']:
        info['warnings'].append(
            'NEON запущен без прав администратора — Брандмауэр Windows и порт :80 могут блокировать подключение ТВ (ERR_CONNECTION_TIMED_OUT).'
        )

    for pname in ('Public', 'Private'):
        p = info['profiles'].get(pname)
        if isinstance(p, dict) and p.get('enabled') and p.get('allowInbound') is False:
            net_ru = 'Общедоступная сеть (Public)' if pname == 'Public' else 'Частная сеть (Private)'
            info['warnings'].append(
                f'Брандмауэр Windows ({net_ru}): включено «Блокировать все входящие подключения» (AllowInboundRules=False) — '
                f'это вызывает ERR_CONNECTION_TIMED_OUT на ТВ даже при наличии разрешающих правил. '
                f'Отключите эту галочку в Безопасности Windows → Брандмауэр и защита сети → {net_ru}.'
            )
    return info


def _ensure_windows_firewall_rules(tcp_ports: list[int] | tuple[int, ...] = (), udp_ports: list[int] | tuple[int, ...] = ()) -> tuple[bool, str]:
    """Ensure clean, fresh Windows Firewall allow rules (all profiles + edge=yes) for NEON VIDAA."""
    if os.name != 'nt':
        return False, 'not windows'
    cflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    cmds: list[tuple[str, list[str], list[str] | None]] = []

    exe_path = os.path.abspath(sys.executable or '')
    if exe_path and os.path.exists(exe_path):
        rule_app = 'NEON-VIDAA-App'
        cmds.append((
            rule_app,
            ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
             f'name={rule_app}', 'dir=in', 'action=allow',
             f'program={exe_path}', 'enable=yes', 'profile=private,public,domain', 'edge=yes'],
            ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
             f'name={rule_app}', 'dir=in', 'action=allow',
             f'program={exe_path}', 'enable=yes', 'profile=private,public,domain'],
        ))

    for port in sorted({int(p) for p in tcp_ports if int(p) > 0}):
        rname = f'NEON-VIDAA-TCP-{port}'
        cmds.append((
            rname,
            ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
             f'name={rname}', 'dir=in', 'action=allow', 'protocol=TCP',
             f'localport={port}', 'enable=yes', 'profile=private,public,domain', 'edge=yes'],
            ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
             f'name={rname}', 'dir=in', 'action=allow', 'protocol=TCP',
             f'localport={port}', 'enable=yes', 'profile=private,public,domain'],
        ))
    for port in sorted({int(p) for p in udp_ports if int(p) > 0}):
        rname = f'NEON-VIDAA-UDP-{port}'
        cmds.append((
            rname,
            ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
             f'name={rname}', 'dir=in', 'action=allow', 'protocol=UDP',
             f'localport={port}', 'enable=yes', 'profile=private,public,domain'],
            None,
        ))

    if not cmds:
        return True, 'no ports'

    any_ok = False
    errors: list[str] = []
    for rname, add_cmd, fallback_cmd in cmds:
        try:
            subprocess.run(
                ['netsh', 'advfirewall', 'firewall', 'delete', 'rule', f'name={rname}'],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=6, creationflags=cflags,
            )
            res = subprocess.run(
                add_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=8, creationflags=cflags,
            )
            if res.returncode != 0 and fallback_cmd is not None:
                res = subprocess.run(
                    fallback_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=8, creationflags=cflags,
                )
            if res.returncode == 0:
                any_ok = True
            else:
                err_txt = (res.stderr or res.stdout or f'rc={res.returncode}').strip()
                errors.append(f'{rname}: {err_txt}')
        except Exception as exc:
            errors.append(f'{rname}: {exc}')
    return any_ok, '; '.join(errors) if errors else 'ok'


class HisensePortalServer:
    """Serves tile PNG icons, self-contained HTML5 apps, Belet proxy, and optional vidaahub.com DNS/HTTPS."""

    def __init__(
        self,
        log_fn=None,
        preferred_ports: tuple[int, ...] = _VIDAA_HTTP_PREFERRED_PORTS,
        bind_port80: bool | None = None,
        http80_port: int = 80,
    ):
        self._log_fn = log_fn or (lambda _m: None)
        self._preferred_ports = tuple(preferred_ports)
        self._bind_port80 = (os.name == 'nt') if bind_port80 is None else bool(bind_port80)
        self._http80_port_cfg = int(http80_port or 80)
        self._lock = threading.RLock()
        self._proxy_hosts: set[str] = set()
        self._proxy_lock = threading.Lock()
        self._http_server: ThreadingHTTPServer | None = None
        self._https_server: ThreadingHTTPServer | None = None
        self._http80_server: ThreadingHTTPServer | None = None
        self._http80_bind_ip: str = ''
        self._http80_actual_port: int = 0
        self._dns_server: _DnsBridgeServer | None = None
        self._firewall_ports: set[tuple[str, int]] = set()
        self._firewall_checked: bool = False
        self._firewall_warnings: list[str] = []
        self._last_fw_info: dict | None = None
        self._hosts_active: bool = False
        self.last_tv_probe: dict | None = None
        self.port: int = 0
        self.https_port: int = 0
        self.dns_port: int = 0
        self.pc_ip: str = '127.0.0.1'
        self.alem_portal: str = 'http://iptv.tmalem.tv'
        self.vidaahub_running: bool = False
        self.warnings: list[str] = []

    def _log(self, msg: str) -> None:
        try:
            self._log_fn(msg)
        except Exception:
            pass

    @property
    def running(self) -> bool:
        return self._http_server is not None

    def base_url(self, tv_ip: str | None = None) -> str:
        ip = get_local_ip_for_tv(tv_ip) if tv_ip else self.pc_ip
        return f'http://{ip}:{self.port}' if self.port else ''

    def icon_url(self, asset_name: str, tv_ip: str | None = None) -> str:
        base = self.base_url(tv_ip)
        return f'{base}/assets/{asset_name}' if base else ''

    def _register_proxy_host(self, host: str) -> None:
        h = str(host or '').strip().lower()
        if not _is_safe_public_host(h) or _is_belet_static_host(h):
            return
        with self._proxy_lock:
            if len(self._proxy_hosts) >= 256:
                self._proxy_hosts.clear()
            self._proxy_hosts.add(h)

    def _is_proxy_host_allowed(self, host: str) -> bool:
        h = str(host or '').strip().lower()
        if _is_belet_static_host(h):
            return True
        if not _is_safe_public_host(h):
            return False
        with self._proxy_lock:
            return h in self._proxy_hosts

    def _open_upstream(self, scheme: str, host: str, timeout: float = 15.0) -> http.client.HTTPConnection:
        if scheme == 'http':
            return http.client.HTTPConnection(host, 80, timeout=timeout)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(host, 443, timeout=timeout, context=ctx)

    def _make_handler(self):
        portal_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, format, *args):  # noqa: A002
                pass

            def _send_cors_headers(self) -> None:
                req_hdrs = (self.headers.get('Access-Control-Request-Headers') or '').strip()
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, HEAD, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', req_hdrs or _BELET_DEFAULT_CORS_HEADERS)
                self.send_header(
                    'Access-Control-Expose-Headers',
                    'X-Neon-Final-URL, Content-Length, Content-Range, Accept-Ranges',
                )

            def _send(self, code: int, body: bytes, content_type: str, cache: str = 'no-cache') -> None:
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self._send_cors_headers()
                self.send_header('Cache-Control', cache)
                self.send_header('Connection', 'close')
                self.end_headers()
                if self.command != 'HEAD' and body:
                    self.wfile.write(body)

            def _origin(self) -> str:
                host_hdr = self.headers.get('Host') or f'{portal_ref.pc_ip}:{portal_ref.port}'
                scheme = 'https' if isinstance(self.connection, ssl.SSLSocket) else 'http'
                return f'{scheme}://{host_hdr}'

            def _handle_proxy_request(self, method: str) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                m = re.match(r'^/__proxy/([a-z0-9.-]+)(/.*)?$', parsed.path, re.IGNORECASE)
                if not m:
                    self._send(404, b'Not Found', 'text/plain; charset=utf-8', cache='no-store')
                    return
                up_host = m.group(1).lower()
                up_path = (m.group(2) or '/') + (f'?{parsed.query}' if parsed.query else '')
                if not portal_ref._is_proxy_host_allowed(up_host):
                    self._send(403, b'Forbidden proxy host', 'text/plain; charset=utf-8', cache='no-store')
                    return

                content_len = 0
                try:
                    content_len = int(self.headers.get('Content-Length') or '0')
                except ValueError:
                    content_len = 0
                if content_len > 8 * 1024 * 1024:
                    self._send(413, b'Request body too large', 'text/plain; charset=utf-8', cache='no-store')
                    return
                req_body = self.rfile.read(content_len) if content_len > 0 else None

                out_headers: dict[str, str] = {}
                for h_name, h_val in self.headers.items():
                    if h_name.lower() in _BELET_ALLOWED_REQ_HEADERS:
                        out_headers[h_name] = h_val
                out_headers['Accept-Encoding'] = 'identity'
                is_belet_static = _is_belet_static_host(up_host)
                out_headers.setdefault(
                    'User-Agent',
                    'belet_films_android'
                    if is_belet_static
                    else 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                )
                if is_belet_static and up_host in _BELET_API_SCAN_HOSTS:
                    out_headers['Origin'] = 'https://belet.tm'
                    out_headers['Referer'] = 'https://belet.tm/'

                origin = self._origin()
                cur_scheme = 'https'
                cur_host = up_host
                cur_path = up_path
                cur_method = method
                cur_body = req_body
                cur_headers = dict(out_headers)
                final_url = f'{cur_scheme}://{cur_host}{cur_path}'

                conn = None
                resp = None
                try:
                    for _redir in range(6):
                        conn = portal_ref._open_upstream(cur_scheme, cur_host, timeout=18.0)
                        conn.request(cur_method, cur_path, body=cur_body, headers=cur_headers)
                        resp = conn.getresponse()
                        if resp.status in (301, 302, 303, 307, 308):
                            loc = (resp.getheader('Location') or '').strip()
                            resp.read()
                            conn.close()
                            conn = None
                            if not loc:
                                break
                            joined = urllib.parse.urljoin(final_url, loc)
                            parts = urllib.parse.urlsplit(joined)
                            next_host = (parts.hostname or '').lower()
                            if parts.scheme not in ('http', 'https') or not _is_safe_public_host(next_host):
                                break
                            portal_ref._register_proxy_host(next_host)
                            cur_scheme = parts.scheme
                            cur_host = next_host
                            cur_path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
                            final_url = joined
                            if resp.status == 303 or (resp.status in (301, 302) and cur_method == 'POST'):
                                cur_method = 'GET'
                                cur_body = None
                                cur_headers.pop('Content-Type', None)
                                cur_headers.pop('Content-Length', None)
                            continue
                        break

                    if resp is None:
                        self._send(502, b'Upstream did not respond', 'text/plain; charset=utf-8', cache='no-store')
                        return

                    ctype = (resp.getheader('Content-Type') or '').lower()
                    cenc = (resp.getheader('Content-Encoding') or '').lower()
                    up_pathname = urllib.parse.urlsplit(cur_path).path.lower()
                    is_m3u8 = up_pathname.endswith('.m3u8') or 'mpegurl' in ctype
                    is_json_scan = (
                        is_belet_static
                        and cur_host in _BELET_API_SCAN_HOSTS
                        and ('json' in ctype or 'text/plain' in ctype)
                    )

                    if (is_m3u8 or is_json_scan) and cur_method != 'HEAD' and 200 <= resp.status < 300:
                        raw_bytes = resp.read(4 * 1024 * 1024 + 1)
                        if len(raw_bytes) <= 4 * 1024 * 1024:
                            plain_bytes = raw_bytes
                            if 'gzip' in cenc:
                                try:
                                    plain_bytes = gzip.decompress(raw_bytes)
                                    cenc = ''
                                except Exception:
                                    plain_bytes = raw_bytes
                            try:
                                text = plain_bytes.decode('utf-8', 'replace')
                            except Exception:
                                text = ''
                            if is_json_scan:
                                _remember_belet_hosts(text, portal_ref._register_proxy_host)
                                out_bytes = raw_bytes
                            else:
                                rewritten = _rewrite_m3u8(
                                    text, origin, cur_host, portal_ref._register_proxy_host
                                )
                                out_bytes = rewritten.encode('utf-8')
                                cenc = ''
                            self.send_response(resp.status)
                            for h_name in _BELET_FORWARD_RESP_HEADERS:
                                h_val = resp.getheader(h_name)
                                if h_val:
                                    self.send_header(h_name.title(), h_val)
                            if cenc:
                                self.send_header('Content-Encoding', cenc)
                            self.send_header('Content-Length', str(len(out_bytes)))
                            self.send_header('X-Neon-Final-URL', final_url)
                            self.send_header('Cache-Control', 'no-store')
                            self._send_cors_headers()
                            self.send_header('Connection', 'close')
                            self.end_headers()
                            self.wfile.write(out_bytes)
                            return
                        # Fallback to streaming remainder if oversized
                        self.send_response(resp.status)
                        for h_name in _BELET_FORWARD_RESP_HEADERS:
                            h_val = resp.getheader(h_name)
                            if h_val:
                                self.send_header(h_name.title(), h_val)
                        self.send_header('X-Neon-Final-URL', final_url)
                        self._send_cors_headers()
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        self.wfile.write(raw_bytes)
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                        return

                    self.send_response(resp.status)
                    for h_name in _BELET_FORWARD_RESP_HEADERS:
                        h_val = resp.getheader(h_name)
                        if h_val:
                            self.send_header(h_name.title(), h_val)
                    clen_hdr = resp.getheader('Content-Length')
                    if clen_hdr and cur_method != 'HEAD':
                        self.send_header('Content-Length', clen_hdr)
                    if cenc:
                        self.send_header('Content-Encoding', cenc)
                    self.send_header('X-Neon-Final-URL', final_url)
                    self._send_cors_headers()
                    self.send_header('Connection', 'close')
                    self.end_headers()

                    if cur_method != 'HEAD' and resp.status not in (204, 304):
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except Exception as exc:
                    msg = json.dumps({'ok': False, 'error': str(exc)}).encode('utf-8')
                    try:
                        self._send(502, msg, 'application/json; charset=utf-8', cache='no-store')
                    except Exception:
                        pass
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass

            def do_OPTIONS(self):  # noqa: N802
                self.send_response(204)
                self._send_cors_headers()
                self.send_header('Access-Control-Max-Age', '600')
                self.send_header('Content-Length', '0')
                self.send_header('Connection', 'close')
                self.end_headers()

            def _handle_tv_probe(self) -> None:
                try:
                    raw_len = int(self.headers.get('Content-Length') or '0')
                except ValueError:
                    raw_len = 0
                raw_len = max(0, min(raw_len, 16384))
                body = self.rfile.read(raw_len) if raw_len else b'{}'
                try:
                    data = json.loads(body.decode('utf-8', errors='replace') or '{}')
                except Exception:
                    data = {'raw': body.decode('utf-8', errors='replace')[:512]}
                if not isinstance(data, dict):
                    data = {'value': data}
                peer = self.client_address[0] if self.client_address else ''
                data['remoteIp'] = peer
                data['ts'] = int(time.time())
                portal_ref.last_tv_probe = data
                page = str(data.get('page') or '')
                model = str(data.get('model') or '')
                os_ver = str(data.get('osVersion') or '')
                ua = str(data.get('ua') or self.headers.get('User-Agent') or '')
                err = str(data.get('error') or '')
                hi = data.get('hasHiUtils')
                inst = data.get('hasInstallApp')
                parts = [f'[TV PROBE {peer}]']
                if page:
                    parts.append(f'page={page}')
                if model or os_ver:
                    parts.append(f'tv={model} {os_ver}'.strip())
                if hi is not None or inst is not None:
                    parts.append(f'HiUtils={bool(hi)} installApp={bool(inst)}')
                if err:
                    parts.append(f'ERR={err[:200]}')
                if ua:
                    parts.append(f'UA={ua[:140]}')
                portal_ref._log(' '.join(parts))
                self._send(200, b'{"ok":true}', 'application/json; charset=utf-8', cache='no-store')

            def do_POST(self):  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                if path == '/__tv_probe':
                    self._handle_tv_probe()
                    return
                if self.path.startswith('/__proxy/'):
                    self._handle_proxy_request('POST')
                    return
                self._send(404, b'Not Found', 'text/plain; charset=utf-8')

            def do_PUT(self):  # noqa: N802
                if self.path.startswith('/__proxy/'):
                    self._handle_proxy_request('PUT')
                    return
                self._send(404, b'Not Found', 'text/plain; charset=utf-8')

            def do_DELETE(self):  # noqa: N802
                if self.path.startswith('/__proxy/'):
                    self._handle_proxy_request('DELETE')
                    return
                self._send(404, b'Not Found', 'text/plain; charset=utf-8')

            def do_HEAD(self):  # noqa: N802
                if self.path.startswith('/__proxy/'):
                    self._handle_proxy_request('HEAD')
                    return
                self.do_GET()

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                qs = urllib.parse.parse_qs(parsed.query)
                origin = self._origin()
                peer = self.client_address[0] if self.client_address else ''
                if peer and peer not in ('127.0.0.1', '::1') and (
                    path in ('/', '/index.html', '/vidaa/selfinstall.html', '/apps/alemtv', '/apps/alemtv/', '/apps/alemtv/index.html', '/apps/belet', '/apps/belet/', '/apps/belet/index.html', '/apps/belet/standalone.html', '/apps/msbcallback', '/apps/msbcallback/', '/apps/msbcallback/index.html')
                ):
                    ua = (self.headers.get('User-Agent') or '')[:100]
                    portal_ref._log(f'[TV HTTP] {peer} GET {path}' + (f' ({ua})' if ua else ''))

                if path == '/__ping':
                    payload = json.dumps({
                        'ok': True,
                        'service': 'neon-vidaa-proxy',
                        'serverTimeMs': int(time.time() * 1000),
                    }).encode('utf-8')
                    self._send(200, payload, 'application/json; charset=utf-8', cache='no-store')
                    return

                if path.startswith('/__proxy/'):
                    self._handle_proxy_request('GET')
                    return

                if path in ('/', '/index.html', '/vidaa/selfinstall.html'):
                    p_url = (qs.get('portal', [''])[0] or portal_ref.alem_portal).strip()
                    html = build_tv_selfinstaller_html(origin, alem_portal=p_url)
                    self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
                    return

                if path in ('/apps/msbcallback', '/apps/msbcallback/', '/apps/msbcallback/index.html'):
                    import msb_callback
                    html = msb_callback.build_hisense_html(inline_assets=True)
                    self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
                    return

                if path in ('/apps/alemtv', '/apps/alemtv/', '/apps/alemtv/index.html'):
                    import alemtv_app
                    p_url = (qs.get('portal', [''])[0] or portal_ref.alem_portal).strip()
                    html = alemtv_app.build_alemtv_vidaa_html(default_portal=p_url)
                    self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
                    return

                if path == '/apps/belet/standalone.html':
                    import belet_vidaa_client
                    use_proxy = (qs.get('proxy', ['0'])[0] or '').strip().lower() in ('1', 'true', 'yes')
                    no_fonts = (qs.get('nofonts', ['0'])[0] or '').strip().lower() in ('1', 'true', 'yes')
                    html = belet_vidaa_client.build_standalone_html(
                        proxy_base=origin if use_proxy else '',
                        include_fonts=not no_fonts,
                    )
                    self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
                    return

                if path in ('/apps/belet', '/apps/belet/', '/apps/belet/index.html'):
                    import belet_vidaa_client
                    item = belet_vidaa_client.read_package_file('index.html', proxy_base=origin)
                    if item is not None:
                        body_bytes, ctype = item
                        if path == '/apps/belet':
                            text = body_bytes.decode('utf-8')
                            text = text.replace('<head>', '<head>\n  <base href="/apps/belet/">', 1)
                            body_bytes = text.encode('utf-8')
                        self._send(200, body_bytes, ctype)
                        return

                if path.startswith('/apps/belet/'):
                    import belet_vidaa_client
                    rel_name = path[len('/apps/belet/'):]
                    item = belet_vidaa_client.read_package_file(rel_name, proxy_base=origin)
                    if item is not None:
                        body_bytes, ctype = item
                        cache = 'public, max-age=3600' if rel_name.endswith(('.png', '.ttf', '.js')) else 'no-cache'
                        self._send(200, body_bytes, ctype, cache=cache)
                        return

                if path.startswith('/assets/'):
                    name = os.path.basename(path[len('/assets/'):])
                    if name and '..' not in name:
                        data = read_asset(name)
                        if data is not None:
                            ctype = 'image/png' if name.lower().endswith('.png') else 'application/octet-stream'
                            self._send(200, data, ctype)
                            return

                if path == '/health':
                    self._send(200, b'{"ok":true}', 'application/json')
                    return

                self._send(404, b'Not Found', 'text/plain; charset=utf-8')

        return Handler

    @staticmethod
    def _self_probe_http(host: str, port: int) -> bool:
        if not host or not port:
            return False
        conn = None
        try:
            conn = http.client.HTTPConnection(host, int(port), timeout=1.0)
            conn.request('GET', '/__ping')
            resp = conn.getresponse()
            resp.read()
            return resp.status == 200
        except Exception:
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _short_url(self) -> str:
        if self._http80_server is None or not self._http80_actual_port:
            return ''
        if self._http80_actual_port == 80:
            return f'http://{self.pc_ip}/'
        return f'http://{self.pc_ip}:{self._http80_actual_port}/'

    def _alt_urls(self) -> list[str]:
        out: list[str] = []
        if not self.port:
            return out
        for ip in list_local_ipv4_addresses():
            if ip == self.pc_ip or ip in _VPN_TUN_IPS:
                continue
            if self._http80_server is not None and self._http80_actual_port == 80 and self._http80_bind_ip == '0.0.0.0':
                out.append(f'http://{ip}/')
            out.append(f'http://{ip}:{self.port}/')
        return out

    def _ensure_firewall(self, tcp_ports: list[int] | tuple[int, ...] = (), udp_ports: list[int] | tuple[int, ...] = ()) -> None:
        if os.name != 'nt':
            return
        if not self._firewall_checked:
            self._firewall_checked = True
            fw = _inspect_windows_firewall_and_unblock()
            self._last_fw_info = fw
            if fw.get('removed_blocks'):
                self._log(
                    'Брандмауэр Windows: сняты старые блокирующие правила (Block) для NEON: '
                    + ', '.join(fw['removed_blocks'])
                )
            self._firewall_warnings = list(fw.get('warnings') or [])
            for w in self._firewall_warnings:
                self._log('[!] ' + w)

        new_tcp = [int(p) for p in tcp_ports if int(p) > 0 and ('tcp', int(p)) not in self._firewall_ports]
        new_udp = [int(p) for p in udp_ports if int(p) > 0 and ('udp', int(p)) not in self._firewall_ports]
        if not new_tcp and not new_udp:
            return
        ok, info = _ensure_windows_firewall_rules(new_tcp, new_udp)
        if ok:
            for p in new_tcp:
                self._firewall_ports.add(('tcp', p))
            for p in new_udp:
                self._firewall_ports.add(('udp', p))
            self._log(
                f'Брандмауэр Windows: открыты порты TCP={new_tcp or "-"} UDP={new_udp or "-"} (все профили сети + edge=yes)'
            )
        elif info and info != 'not windows':
            msg = (
                f'Не удалось автоматически открыть Брандмауэр Windows ({info}). '
                f'Разрешите входящие TCP {new_tcp} в профиле «Общедоступная сеть» или запустите NEON от администратора.'
            )
            if msg not in self._firewall_warnings:
                self._firewall_warnings.append(msg)
            self._log('[!] ' + msg)

    def _bind_preferred_http(self, handler_cls) -> _VidaaHTTPServer:
        for port in self._preferred_ports:
            try:
                srv = _VidaaHTTPServer(('0.0.0.0', int(port)), handler_cls)
                return srv
            except OSError:
                continue
        return _VidaaHTTPServer(('0.0.0.0', 0), handler_cls)

    def _start_http80_if_free(self, handler_cls, port80: int) -> None:
        if not port80:
            return
        if self._http80_server is not None:
            if self._http80_bind_ip in ('0.0.0.0', self.pc_ip):
                return
            try:
                self._http80_server.shutdown()
                self._http80_server.server_close()
            except Exception:
                pass
            self._http80_server = None
            self._http80_bind_ip = ''
            self._http80_actual_port = 0

        candidates = ['0.0.0.0']
        if _is_usable_lan_ip(self.pc_ip):
            candidates.append(self.pc_ip)
        for bind_ip in candidates:
            try:
                srv80 = _VidaaHTTPServer((bind_ip, int(port80)), handler_cls)
                self._http80_server = srv80
                self._http80_bind_ip = bind_ip
                self._http80_actual_port = srv80.server_address[1]
                threading.Thread(
                    target=srv80.serve_forever, daemon=True, name='neon-vidaa-http80'
                ).start()
                self._log(
                    f'Дополнительный HTTP :{self._http80_actual_port} ({bind_ip}) активен: '
                    f'{self._short_url()}'
                )
                return
            except OSError:
                continue

    def ensure_started(self, tv_ip: str | None = None, alem_portal: str | None = None) -> dict:
        with self._lock:
            if alem_portal:
                self.alem_portal = alem_portal
            prev_ip = self.pc_ip
            if tv_ip:
                self.pc_ip = get_local_ip_for_tv(tv_ip)
            elif not self.pc_ip or self.pc_ip == '127.0.0.1' or self.pc_ip in _VPN_TUN_IPS:
                self.pc_ip = get_local_ip_for_tv(None)

            handler_cls = self._make_handler()
            started_now = False
            if self._http_server is None:
                srv = self._bind_preferred_http(handler_cls)
                self.port = srv.server_address[1]
                self._http_server = srv
                threading.Thread(target=srv.serve_forever, daemon=True, name='neon-vidaa-http').start()
                started_now = True

            if self._bind_port80:
                self._start_http80_if_free(handler_cls, self._http80_port_cfg)

            fw_tcp = [self.port]
            if self._http80_actual_port:
                fw_tcp.append(self._http80_actual_port)
            elif self._bind_port80 and self._http80_port_cfg:
                fw_tcp.append(self._http80_port_cfg)
            self._ensure_firewall(tcp_ports=fw_tcp)

            short = self._short_url()
            if started_now or self.pc_ip != prev_ip:
                extra = f' (или коротко: {short})' if short else ''
                self._log(f'Локальный сервер Hisense VIDAA запущен: http://{self.pc_ip}:{self.port}/{extra}')
                if self.pc_ip and self.pc_ip != '127.0.0.1' and not self._self_probe_http(self.pc_ip, self.port):
                    self._log(
                        f'[!] Самопроверка локального адреса http://{self.pc_ip}:{self.port}/__ping не ответила — '
                        'возможна блокировка антивирусом/брандмауэром на ПК.'
                    )

            combined_warnings = list(dict.fromkeys(self._firewall_warnings + self.warnings))
            return {
                'ok': True,
                'port': self.port,
                'port80': self._http80_actual_port,
                'port80_active': bool(self._http80_server is not None),
                'pc_ip': self.pc_ip,
                'url': f'http://{self.pc_ip}:{self.port}/',
                'short_url': short,
                'alt_urls': self._alt_urls(),
                'vidaahub_running': self.vidaahub_running,
                'dns_port': self.dns_port,
                'https_port': self.https_port,
                'warnings': combined_warnings,
            }

    def start_vidaahub_bridge(
        self,
        tv_ip: str | None = None,
        upstream_dns: str = '8.8.8.8',
        alem_portal: str | None = None,
        dns_port: int = 53,
        https_port: int = 443,
        http80_port: int = 80,
    ) -> dict:
        with self._lock:
            self.warnings = []
            base = self.ensure_started(tv_ip=tv_ip, alem_portal=alem_portal)
            handler_cls = self._make_handler()
            self._ensure_firewall(
                tcp_ports=[p for p in (self.port, https_port, http80_port) if p > 0],
                udp_ports=[p for p in (dns_port,) if p > 0],
            )

            if self._dns_server is None:
                try:
                    dns = _DnsBridgeServer(self.pc_ip, upstream=upstream_dns or '8.8.8.8', port=dns_port)
                    self.dns_port = dns.start()
                    self._dns_server = dns
                    self._log(f'DNS-мост vidaahub.com запущен (UDP :{self.dns_port} -> {self.pc_ip})')
                except OSError as e:
                    hosts_note = ''
                    if os.name == 'nt' and dns_port == 53:
                        ok_h, info_h = _hosts_set_vidaahub(self.pc_ip)
                        if ok_h:
                            self._hosts_active = True
                            hosts_note = (
                                f' Записан временный маршрут vidaahub.com -> {self.pc_ip} в hosts ПК '
                                f'(работает, если общий доступ ICS перенаправляет DNS через этот ПК).'
                            )
                    alt_link = self._short_url() or f'http://{self.pc_ip}:{self.port}/'
                    self.warnings.append(
                        f'Не удалось занять UDP:{dns_port} ({e} — обычно порт занят Windows ICS / общим интернетом). '
                        f'Для простого запуска без DNS используйте прямую ссылку {alt_link} в браузере ТВ.{hosts_note}'
                    )

            if self._https_server is None:
                try:
                    cert_pem, key_pem = _build_vidaahub_selfsigned_cert(self.pc_ip)
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    with tempfile.TemporaryDirectory(prefix='neon-vidaahub-') as td:
                        cp = os.path.join(td, 'cert.pem')
                        kp = os.path.join(td, 'key.pem')
                        with open(cp, 'wb') as f:
                            f.write(cert_pem)
                        with open(kp, 'wb') as f:
                            f.write(key_pem)
                        ctx.load_cert_chain(cp, kp)
                    srv_tls = _VidaaHTTPServer(('0.0.0.0', https_port), handler_cls)
                    srv_tls.socket = ctx.wrap_socket(srv_tls.socket, server_side=True)
                    self.https_port = srv_tls.server_address[1]
                    self._https_server = srv_tls
                    threading.Thread(
                        target=srv_tls.serve_forever, daemon=True, name='neon-vidaahub-https'
                    ).start()
                    self._log(f'HTTPS-портал https://vidaahub.com (:{self.https_port}) запущен')
                except OSError as e:
                    self.warnings.append(
                        f'Не удалось занять TCP:{https_port} ({e}).'
                    )

            self._start_http80_if_free(handler_cls, http80_port)

            self.vidaahub_running = bool(self._dns_server or self._https_server)
            combined_warnings = list(dict.fromkeys(self._firewall_warnings + self.warnings))
            base.update({
                'port80': self._http80_actual_port,
                'port80_active': bool(self._http80_server is not None),
                'short_url': self._short_url(),
                'vidaahub_running': self.vidaahub_running,
                'vidaahub_url': 'https://vidaahub.com/',
                'hosts_fallback': self._hosts_active,
                'dns_port': self.dns_port,
                'https_port': self.https_port,
                'warnings': combined_warnings,
            })
            return base

    def diagnose_connection(self, tv_ip: str | None = None) -> dict:
        """Verify local HTTP binding, Windows Firewall/ICS profile, and PC->TV reachability."""
        clean_tv = (tv_ip or '').strip()
        info = self.ensure_started(tv_ip=clean_tv or None)
        lines: list[str] = []
        issues: list[str] = []

        all_ips = list_local_ipv4_addresses()
        lan_ips = [ip for ip in all_ips if ip not in _VPN_TUN_IPS]
        has_vless = any(ip in _VPN_TUN_IPS for ip in all_ips)
        has_ics = ('192.168.137.1' in lan_ips)

        lines.append(f'Выбранный IP ПК для ТВ: {self.pc_ip}' + (' (Ethernet ICS)' if self.pc_ip == '192.168.137.1' else ''))
        if lan_ips:
            lines.append('Все локальные IPv4 ПК: ' + ', '.join(lan_ips))
        if has_vless:
            lines.append('Обнаружен TUN-интерфейс VLESS (172.19.0.1).')
        if has_ics and clean_tv and not clean_tv.startswith('192.168.137.') and not clean_tv.startswith('127.'):
            issues.append(
                f'На ПК активна подсеть Ethernet ICS (192.168.137.1), но IP ТВ указан как {clean_tv}. '
                'Если ТВ подключён кабелем к ПК, укажите его IP из 192.168.137.x.'
            )

        urls: list[str] = []
        if info.get('short_url'):
            urls.append(info['short_url'])
        urls.append(info['url'])
        lines.append('Ссылки установщика: ' + ' | '.join(urls))
        if info.get('alt_urls'):
            lines.append('Альтернативные IP ПК: ' + ' | '.join(info['alt_urls']))

        loop_ok = self._self_probe_http('127.0.0.1', self.port)
        pc_ok = self._self_probe_http(self.pc_ip, self.port) if self.pc_ip else False
        p80_ok = (
            self._self_probe_http(self.pc_ip, self._http80_actual_port)
            if (self._http80_server is not None and self._http80_actual_port) else False
        )
        lines.append(
            f'Самопроверка HTTP: localhost:{self.port}={"OK" if loop_ok else "FAIL"}, '
            f'{self.pc_ip}:{self.port}={"OK" if pc_ok else "FAIL"}'
            + (f', {self.pc_ip}:{self._http80_actual_port}={"OK" if p80_ok else "FAIL"}' if self._http80_actual_port else '')
        )
        if not loop_ok or not pc_ok:
            issues.append(
                f'Локальный HTTP-сервер не отвечает на {self.pc_ip}:{self.port} (возможна блокировка антивирусом/брандмауэром).'
            )

        tv_mqtt_ok = None
        if clean_tv:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.5)
            try:
                s.connect((clean_tv, MQTT_PORT))
                tv_mqtt_ok = True
                lines.append(f'ПК → ТВ ({clean_tv}:{MQTT_PORT}): ДОСТУПЕН')
            except OSError as exc:
                tv_mqtt_ok = False
                lines.append(f'ПК → ТВ ({clean_tv}:{MQTT_PORT}): НЕ ДОСТУПЕН ({exc})')
                issues.append(f'Нет связи ПК → ТВ {clean_tv}:{MQTT_PORT} ({exc}). Проверьте питание ТВ и IP.')
            finally:
                try:
                    s.close()
                except OSError:
                    pass

        fw_info = None
        if os.name == 'nt':
            with self._lock:
                self._firewall_checked = False
                self._firewall_ports.clear()
                fw_ports = [self.port] + ([self._http80_actual_port] if self._http80_actual_port else [])
                self._ensure_firewall(tcp_ports=fw_ports)
                fw_info = self._last_fw_info or {}
            lines.append(f'Права администратора: {"ДА" if fw_info.get("admin") else "НЕТ"}')
            profs = fw_info.get('profiles') or {}
            prof_parts = []
            for pname in ('Public', 'Private', 'Domain'):
                p = profs.get(pname)
                if isinstance(p, dict):
                    state = 'ON' if p.get('enabled') else 'OFF'
                    inb = 'allowRules' if p.get('allowInbound', True) else 'BLOCK-ALL!'
                    prof_parts.append(f'{pname}={state}/{inb}')
            if prof_parts:
                lines.append('Профили Firewall: ' + ', '.join(prof_parts))
            net_cats = [f'{n.get("iface")}: {n.get("cat")}' for n in (fw_info.get('net') or []) if n.get('iface')]
            if net_cats:
                lines.append('Категории сетей: ' + '; '.join(net_cats))
            for w in self._firewall_warnings:
                if w not in issues:
                    issues.append(w)

        if self.last_tv_probe:
            lines.append(
                f'Последний запрос от ТВ: {self.last_tv_probe.get("remoteIp")} '
                f'({self.last_tv_probe.get("model") or "TV"} / {self.last_tv_probe.get("osVersion") or ""})'
            )
        else:
            lines.append('Запросов от браузера ТВ к ПК ещё не поступало.')

        if not issues:
            lines.append(
                'Если при открытии этих ссылок браузер ТВ всё равно пишет ERR_CONNECTION_TIMED_OUT (-118), '
                'проверьте сторонний антивирус (Kaspersky/ESET/Avast — отключите сетевой экран на 1 мин для теста) '
                'или переключите Ethernet в «Частную сеть» Windows.'
            )

        for ln in lines:
            self._log('[DIAG] ' + ln)
        for iss in issues:
            self._log('[!] ' + iss)

        return {
            'ok': len(issues) == 0,
            'pc_ip': self.pc_ip,
            'tv_ip': clean_tv,
            'port': self.port,
            'port80': self._http80_actual_port,
            'urls': urls,
            'alt_urls': info.get('alt_urls') or [],
            'tv_mqtt_ok': tv_mqtt_ok,
            'issues': issues,
            'lines': lines,
            'warnings': issues,
        }

    def stop(self) -> None:
        with self._lock:
            for attr in ('_http_server', '_https_server', '_http80_server'):
                srv = getattr(self, attr)
                if srv is not None:
                    try:
                        srv.shutdown()
                        srv.server_close()
                    except Exception:
                        pass
                    setattr(self, attr, None)
            if self._dns_server is not None:
                self._dns_server.stop()
                self._dns_server = None
            if self._hosts_active:
                _hosts_clear_vidaahub()
                self._hosts_active = False
            self._http80_bind_ip = ''
            self._http80_actual_port = 0
            self._firewall_ports.clear()
            self._firewall_checked = False
            self._firewall_warnings = []
            self.warnings = []
            self.port = 0
            self.https_port = 0
            self.dns_port = 0
            self.vidaahub_running = False
