"""Hisense / Toshiba VIDAA Smart TV discovery, pairing, remote, app install, and LAN/vidaahub portal."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import email.utils
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import re
import secrets
import socket
import ssl
import struct
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


def get_local_ip_for_tv(tv_ip: str | None = None) -> str:
    target = (tv_ip or '').strip() or '8.8.8.8'
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 36669))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith('127.'):
            return ip
    except OSError:
        pass
    finally:
        sock.close()
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith('127.'):
            return ip
    except OSError:
        pass
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


def launch_payload(app_id: str, name: str, url: str, store_type: int = 99) -> str:
    return json.dumps({
        'appId': app_id,
        'name': name,
        'url': url,
        'urlType': 37,
        'storeType': store_type,
        'appName': name,
        'appUrl': url,
    }, ensure_ascii=False)


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
            'appId': str(item.get('appId') or item.get('id') or item.get('Id') or ''),
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


def tile_matches_request(tile: dict, app_id: str, url: str) -> bool:
    if not isinstance(tile, dict):
        return False
    tid = str(tile.get('appId') or tile.get('Id') or '').lower()
    if tid != app_id.lower():
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
    return bool(addrs) and all(normalize_tile_url(a) == wanted for a in addrs)


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
        local = get_local_ip_for_tv(None)
        parts = local.split('.')
        if len(parts) == 4:
            targets.insert(0, f'{parts[0]}.{parts[1]}.{parts[2]}.255')
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
            apps: list[dict] = []
            if wait_confirm:
                time.sleep(1.5)
                for attempt in range(2):
                    since = len(cli.messages)
                    cli.publish(topics['ui'] + 'actions/applist', '0')
                    raw = cli.wait_for('ui_service/data/applist', 6.0, since)
                    apps = parse_applist(raw) or []
                    if any(tile_matches_request(a, clean_id, clean_url) for a in apps):
                        confirmed = True
                        break
                    if attempt == 0:
                        time.sleep(1.2)
            launched = False
            if auto_launch:
                cli.publish(
                    topics['ui'] + 'actions/launchapp',
                    launch_payload(clean_id, clean_title, clean_url),
                )
                launched = True
                time.sleep(0.5)
            return {
                'ok': True,
                'confirmed': confirmed,
                'status': 'installed' if confirmed else 'sent_unconfirmed',
                'app_id': clean_id,
                'title': clean_title,
                'url': clean_url,
                'launched': launched,
                'apps_count': len(apps),
            }

        res = self._run_authenticated(host, op)
        if res['confirmed']:
            self._log(f'Плитка «{clean_title}» ({clean_id}) подтверждена в списке приложений ТВ')
        else:
            self._log(
                f'Команда установки «{clean_title}» отправлена, но ТВ не подтвердил её в applist сразу '
                '(переоткройте «Мои приложения» на ТВ или используйте режим Браузера ТВ / vidaahub.com)'
            )
        return res

    def launch_app(self, host: str, app_id: str, title: str = '', url: str = '') -> dict:
        def op(cli: MqttTlsClient, topics: dict[str, str], _sess: VidaaSession):
            target_id = app_id
            target_title = title or app_id
            target_url = url
            target_store = 99
            if not target_url:
                cli.subscribe([topics['applist_reply']])
                time.sleep(0.2)
                since = len(cli.messages)
                cli.publish(topics['ui'] + 'actions/applist', '0')
                raw = cli.wait_for('data/applist', 6.0, since)
                apps = parse_applist(raw) or []
                for a in apps:
                    if (
                        (target_id and a['appId'].lower() == target_id.lower())
                        or (title and a['name'].lower() == title.lower())
                    ):
                        target_id = a['appId']
                        target_title = a['name'] or target_title
                        target_url = a['url']
                        target_store = int(a.get('storeType') or 99)
                        break
            cli.publish(
                topics['ui'] + 'actions/launchapp',
                launch_payload(target_id, target_title, target_url, store_type=target_store),
            )
            time.sleep(0.4)
            return {'ok': True, 'app_id': target_id, 'title': target_title, 'url': target_url}

        res = self._run_authenticated(host, op)
        self._log(f'Отправлен запуск приложения «{res["title"]}»')
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
    <div class="sub">Автономная установка приложений через ТВ-браузер, TV Flash и мост Hisense</div>
  </div>
  <div class="badges" id="envInfo">Определение среды VIDAA…</div>
</div>

<div class="grid">
  <div class="card">
    <h2>MSB Callback</h2>
    <p>Офлайн-визитка с диагностикой сети, LAN/Wi-Fi IP и QR-кодами. В режиме TV Flash работает без ПК и интернета.</p>
    <button class="btn btn-prim" onclick="installOfflineFlash('neonmsbcallback','MSB Callback','/apps/msbcallback/index.html','/assets/msb_icon.png')">Установить в память ТВ (Offline)</button>
    <button class="btn" onclick="installBridgeApp('neonmsbcallback','MSB Callback',ORIGIN+'/apps/msbcallback','/assets/msb_icon.png')">Плитка через Hisense API</button>
    <button class="btn" onclick="openUrl('/apps/msbcallback')">Открыть сейчас</button>
    <button class="btn btn-danger" onclick="uninstallApp('neonmsbcallback','MSB Callback')">Удалить</button>
  </div>

  <div class="card">
    <h2>Alem TV</h2>
    <p>Standalone Smart TV клиент Alem TV с EPG, архивом и порталом оператора.</p>
    <button class="btn btn-prim" onclick="installOfflineFlash('tmalemtvvidaa','Alem TV','/apps/alemtv/index.html','/assets/alemtv_large_icon.png')">Установить в память ТВ (Offline)</button>
    <button class="btn" onclick="installBridgeApp('tmalemtvvidaa','Alem TV',ORIGIN+'/apps/alemtv','/assets/alemtv_large_icon.png')">Через Hisense API (LAN)</button>
    <button class="btn" onclick="openUrl('/apps/alemtv')">Открыть сейчас</button>
    <button class="btn btn-danger" onclick="uninstallApp('tmalemtvvidaa','Alem TV')">Удалить</button>
  </div>

  <div class="card">
    <h2>Belet TV</h2>
    <p>Официальный веб-клиент Belet (https://belet.tm/) для Hisense VIDAA.</p>
    <button class="btn btn-prim" onclick="installBridgeApp('tm.belet.vidaa','Belet','https://belet.tm/','/assets/belet_large_icon.png')">Установить (Hisense API)</button>
    <button class="btn" onclick="installUrlFlash('tm.belet.vidaa','Belet','https://belet.tm/','/assets/belet_large_icon.png')">Записать в Appinfo.json</button>
    <button class="btn" onclick="openUrl('https://belet.tm/')">Открыть сейчас</button>
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
  try {{ if (typeof Hisense_GetModelName === 'function') model = Hisense_GetModelName(); }} catch(e){{}}
  var envText = 'Модель: ' + (model || 'Браузер / Smart TV') +
    '<br>HiUtils TV Flash: ' + (hasHiUtils ? 'ДОСТУПЕН' : 'нет (нужен https://vidaahub.com или браузер ТВ)') +
    '<br>Hisense_installApp: ' + (hasBridge ? 'ДОСТУПЕН' : 'нет');
  document.getElementById('envInfo').innerHTML = envText;

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
    if (cur && cur.ret) {{
      var data = (typeof cur.msg === 'string') ? JSON.parse(cur.msg) : cur.msg;
      if (data && data.AppInfo) return data;
    }}
    return {{ AppInfo: [] }};
  }}

  function upsertAppInfo(appId, title, appUrl, iconUrl) {{
    var reg = readAppInfo();
    var kept = [];
    for (var i = 0; i < reg.AppInfo.length; i++) {{
      if (reg.AppInfo[i].Id !== appId) kept.push(reg.AppInfo[i]);
    }}
    kept.push({{
      Id: appId, AppName: title, Title: title,
      URL: appUrl, StartCommand: appUrl,
      IconURL: iconUrl, Icon_96: iconUrl, Image: iconUrl, Thumb: iconUrl,
      Type: 'Browser', InstallTime: new Date().toISOString().split('T')[0],
      RunTimes: 0, StoreType: 'custom', PreInstall: false
    }});
    reg.AppInfo = kept;
    return HiUtils_createRequest('fileWrite', {{ path: 'websdk/Appinfo.json', mode: 6, writedata: JSON.stringify(reg) }});
  }}

  window.installOfflineFlash = function(appId, title, htmlPath, iconPath) {{
    if (!hasHiUtils) {{
      log('HiUtils_createRequest недоступен на этом домене — пробую data:/Hisense API…');
      if (hasBridge) {{
        fetchText(htmlPath, function(err, body){{
          if (err) return log('Ошибка загрузки: ' + err);
          var durl = 'data:text/html;charset=utf-8;base64,' + btoa(unescape(encodeURIComponent(body)));
          installBridgeApp(appId, title, durl, iconPath);
        }});
      }} else {{
        log('Откройте эту страницу через режим vidaahub.com (DNS на ПК) или установите по MQTT из программы NEON.');
      }}
      return;
    }}
    log('Загрузка ' + title + ' и запись во внутреннюю память ТВ…');
    fetchText(htmlPath, function(err, body){{
      if (err) return log('Ошибка загрузки HTML: ' + err);
      var flashPath = 'websdk/' + appId + '/index.html';
      var w1 = HiUtils_createRequest('fileWrite', {{ path: flashPath, mode: 6, writedata: body }});
      if (!w1 || !w1.ret) return log('Ошибка fileWrite HTML: ' + JSON.stringify(w1));
      var localUrl = 'file:///APPS/' + flashPath;
      var fullIcon = ORIGIN + iconPath;
      var w2 = upsertAppInfo(appId, title, localUrl, fullIcon);
      if (w2 && w2.ret) {{
        refreshLauncher(appId);
        log('ГОТОВО! «' + title + '» записан в память ТВ (' + localUrl + '). Переоткройте список приложений или перезагрузите ТВ.');
      }} else {{
        log('Ошибка записи Appinfo.json: ' + JSON.stringify(w2));
      }}
    }});
  }};

  window.installUrlFlash = function(appId, title, appUrl, iconPath) {{
    if (!hasHiUtils) return log('HiUtils_createRequest недоступен на этом домене');
    var w = upsertAppInfo(appId, title, appUrl, ORIGIN + iconPath);
    if (w && w.ret) {{ refreshLauncher(appId); log('Сохранено в Appinfo.json: ' + title); }}
    else log('Ошибка записи Appinfo.json: ' + JSON.stringify(w));
  }};

  window.installBridgeApp = function(appId, title, appUrl, iconPath) {{
    if (!hasBridge) return log('Hisense_installApp недоступен. Для его активации включите режим vidaahub.com.');
    var icon = ORIGIN + iconPath;
    log('Установка через Hisense_installApp: ' + title + '…');
    try {{
      Hisense_installApp(appId, title, icon, icon, icon, appUrl, 'store', function(status){{
        if (!status) {{
          refreshLauncher(appId);
          log('УСПЕХ: ' + title + ' установлен в меню приложений ТВ!');
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
      did = true;
      var reg = readAppInfo();
      reg.AppInfo = reg.AppInfo.filter(function(a){{ return a.Id !== appId; }});
      var w = HiUtils_createRequest('fileWrite', {{ path: 'websdk/Appinfo.json', mode: 6, writedata: JSON.stringify(reg) }});
      refreshLauncher();
      log('Удалено из websdk/Appinfo.json (' + appId + '): ' + JSON.stringify(w));
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


class HisensePortalServer:
    """Serves tile PNG icons, self-contained HTML5 apps, and optional vidaahub.com DNS/HTTPS."""

    def __init__(self, log_fn=None):
        self._log_fn = log_fn or (lambda _m: None)
        self._lock = threading.RLock()
        self._http_server: ThreadingHTTPServer | None = None
        self._https_server: ThreadingHTTPServer | None = None
        self._http80_server: ThreadingHTTPServer | None = None
        self._dns_server: _DnsBridgeServer | None = None
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

    def _make_handler(self):
        portal_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, format, *args):  # noqa: A002
                pass

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                qs = urllib.parse.parse_qs(parsed.query)
                host_hdr = self.headers.get('Host') or f'{portal_ref.pc_ip}:{portal_ref.port}'
                scheme = 'https' if isinstance(self.connection, ssl.SSLSocket) else 'http'
                origin = f'{scheme}://{host_hdr}'

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

    def ensure_started(self, tv_ip: str | None = None, alem_portal: str | None = None) -> dict:
        with self._lock:
            if alem_portal:
                self.alem_portal = alem_portal
            self.pc_ip = get_local_ip_for_tv(tv_ip)
            if self._http_server is None:
                srv = ThreadingHTTPServer(('0.0.0.0', 0), self._make_handler())
                srv.daemon_threads = True
                self.port = srv.server_address[1]
                self._http_server = srv
                threading.Thread(target=srv.serve_forever, daemon=True, name='neon-vidaa-http').start()
                self._log(f'Локальный сервер Hisense VIDAA запущен: http://{self.pc_ip}:{self.port}/')
            return {
                'ok': True,
                'port': self.port,
                'pc_ip': self.pc_ip,
                'url': f'http://{self.pc_ip}:{self.port}/',
                'vidaahub_running': self.vidaahub_running,
                'dns_port': self.dns_port,
                'https_port': self.https_port,
                'warnings': list(self.warnings),
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
            base = self.ensure_started(tv_ip=tv_ip, alem_portal=alem_portal)
            self.warnings = []
            handler_cls = self._make_handler()

            if self._dns_server is None:
                try:
                    dns = _DnsBridgeServer(self.pc_ip, upstream=upstream_dns or '8.8.8.8', port=dns_port)
                    self.dns_port = dns.start()
                    self._dns_server = dns
                    self._log(f'DNS-мост vidaahub.com запущен (UDP :{self.dns_port} -> {self.pc_ip})')
                except OSError as e:
                    self.warnings.append(
                        f'Не удалось занять UDP:{dns_port} ({e}). Запустите от администратора или направьте vidaahub.com -> {self.pc_ip} на роутере.'
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
                    srv_tls = ThreadingHTTPServer(('0.0.0.0', https_port), handler_cls)
                    srv_tls.daemon_threads = True
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

            if self._http80_server is None and http80_port:
                try:
                    srv80 = ThreadingHTTPServer(('0.0.0.0', http80_port), handler_cls)
                    srv80.daemon_threads = True
                    self._http80_server = srv80
                    threading.Thread(
                        target=srv80.serve_forever, daemon=True, name='neon-vidaahub-http80'
                    ).start()
                except OSError:
                    pass

            self.vidaahub_running = bool(self._dns_server or self._https_server)
            base.update({
                'vidaahub_running': self.vidaahub_running,
                'vidaahub_url': 'https://vidaahub.com/',
                'dns_port': self.dns_port,
                'https_port': self.https_port,
                'warnings': list(self.warnings),
            })
            return base

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
            self.port = 0
            self.https_port = 0
            self.dns_port = 0
            self.vidaahub_running = False
