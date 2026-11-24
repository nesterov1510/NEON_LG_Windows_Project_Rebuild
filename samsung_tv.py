"""Pure-stdlib Samsung Smart TV LAN control (Tizen WS/WSS 8001/8002 & Legacy 55000)."""
import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

_WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

SAMSUNG_LOCAL_SETS = [
    ('CIS_RUSSIA', 'CIS / Russia (Russian UI & CIS Smart Hub)', 'СНГ / Россия (русский язык и регион СНГ)'),
    ('CIS_UKR', 'CIS / Ukraine', 'СНГ / Украина'),
    ('CIS_KZ', 'CIS / Kazakhstan', 'СНГ / Казахстан'),
    ('EU_TURKEY', 'Europe / Turkey (adds Russian/CIS fallback on EU sets)', 'Европа / Турция (часто открывает русский на EU ТВ)'),
    ('EU_RS_AFR', 'Europe / RS Africa (fallback if CIS unavailable)', 'Европа / RS Africa (резерв, если нет CIS)'),
    ('EU_GER', 'Europe / Germany (HbbTV & EU apps)', 'Европа / Германия (EU приложения и HbbTV)'),
    ('EU_UK', 'Europe / United Kingdom (English + PVR enabled)', 'Европа / Великобритания (English + PVR)'),
    ('PANEURO', 'Pan-European', 'Пан-Европейский (PANEURO)'),
    ('USA', 'United States (US Smart Hub, removes non-US geo locks)', 'США (US Smart Hub, снимает ограничения)'),
    ('ASIA_ATV', 'Asia / Analog & Digital', 'Азия (ASIA_ATV)'),
]


def _no_proxy_opener(ssl_ctx=None):
    handlers = [urllib.request.ProxyHandler({})]
    if ssl_ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_ctx))
    return urllib.request.build_opener(*handlers)


def _permissive_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers('DEFAULT:@SECLEVEL=1')
    except Exception:
        pass
    return ctx


def fetch_samsung_device_info(ip, timeout=2.5):
    """Fetch Samsung Tizen /api/v2/ metadata over port 8001 HTTP or 8002 HTTPS."""
    for scheme, port, ctx in (
        ('http', 8001, None),
        ('https', 8002, _permissive_ssl_context()),
    ):
        url = f'{scheme}://{ip}:{port}/api/v2/'
        try:
            opener = _no_proxy_opener(ctx)
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
            data = json.loads(raw)
            dev = data.get('device') if isinstance(data.get('device'), dict) else {}
            if not dev and 'type' not in data:
                continue
            return {
                'ok': True,
                'scheme': scheme,
                'port': port,
                'name': dev.get('name') or data.get('name') or 'Samsung Smart TV',
                'model': dev.get('modelName') or dev.get('model') or 'Unknown',
                'os': dev.get('OS') or 'Tizen',
                'power_state': dev.get('PowerState') or 'on',
                'token_auth': str(dev.get('TokenAuthSupport', '')).lower() == 'true',
                'mac': dev.get('wifiMac') or '',
                'country': dev.get('countryCode') or '',
                'language': dev.get('Language') or '',
                'duid': dev.get('duid') or '',
                'developer_mode': str(dev.get('developerMode', '')).strip(),
                'developer_ip': str(dev.get('developerIP', '')).strip(),
                'raw': data,
            }
        except Exception:
            continue
    return None


def discover_samsung_tvs(timeout=1.6):
    """Discover Samsung TVs on LAN using SSDP M-SEARCH and verify /api/v2/."""
    found = {}
    targets = (
        'urn:samsung.com:device:RemoteControlReceiver:1',
        'urn:dial-multiscreen-org:service:dial:1',
        'ssdp:all',
    )
    candidate_ips = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.35)
        for st in targets:
            msg = (
                'M-SEARCH * HTTP/1.1\r\n'
                'HOST: 239.255.255.250:1900\r\n'
                'MAN: "ssdp:discover"\r\n'
                'MX: 1\r\n'
                f'ST: {st}\r\n\r\n'
            ).encode('ascii')
            try:
                sock.sendto(msg, ('239.255.255.250', 1900))
            except OSError:
                break

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode('utf-8', errors='replace')
            lower = text.lower()
            if 'samsung' in lower or ':8001' in lower or ':8002' in lower or ':7676' in lower or ':7678' in lower:
                candidate_ips.add(addr[0])
                m = re.search(r'^LOCATION:\s*(http[s]?://[^\r\n]+)', text, re.I | re.M)
                if m:
                    parsed = urllib.parse.urlparse(m.group(1).strip())
                    if parsed.hostname:
                        candidate_ips.add(parsed.hostname)
    finally:
        sock.close()

    for ip in sorted(candidate_ips):
        info = fetch_samsung_device_info(ip, timeout=1.2)
        if info:
            found[ip] = {
                'ip': ip,
                'name': info['name'],
                'model': info['model'],
                'os': info['os'],
                'power_state': info['power_state'],
                'mac': info['mac'],
                'country': info['country'],
                'port': 8002 if info['token_auth'] else info['port'],
            }
        else:
            # Could be legacy pre-2014 Samsung TV (port 55000)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.6)
            try:
                if s.connect_ex((ip, 55000)) == 0:
                    found[ip] = {
                        'ip': ip,
                        'name': 'Samsung TV (Legacy 55000)',
                        'model': 'Orsay / Pre-2014',
                        'os': 'Orsay',
                        'power_state': 'on',
                        'mac': '',
                        'country': '',
                        'port': 55000,
                    }
            finally:
                s.close()

    return list(found.values())


class SamsungWsClient:
    """Minimal RFC 6455 WebSocket client tailored for Samsung Smart TV v2 API."""

    def __init__(self, host, port=8002, name='NEON Remote', token=None, use_ssl=None, timeout=8.0):
        self.host = host
        self.port = int(port)
        self.name = name
        self.token = token
        self.use_ssl = bool(self.port == 8002) if use_ssl is None else bool(use_ssl)
        self.timeout = float(timeout)
        self._sock = None
        self._buf = bytearray()

    def _build_path(self):
        b64_name = urllib.parse.quote(base64.b64encode(self.name.encode('utf-8')).decode('ascii'), safe='')
        path = f'/api/v2/channels/samsung.remote.control?name={b64_name}'
        if self.token:
            path += '&token=' + urllib.parse.quote(str(self.token), safe='')
        return path

    def connect(self, auth_timeout=None):
        self.close()
        raw_sock = socket.create_connection((self.host, self.port), timeout=min(self.timeout, 5.0))
        if self.use_ssl:
            ctx = _permissive_ssl_context()
            sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        else:
            sock = raw_sock
        sock.settimeout(auth_timeout or self.timeout)
        self._sock = sock

        ws_key = base64.b64encode(os.urandom(16)).decode('ascii')
        path = self._build_path()
        req = (
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {self.host}:{self.port}\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {ws_key}\r\n'
            'Sec-WebSocket-Version: 13\r\n'
            'Origin: http://localhost\r\n\r\n'
        ).encode('ascii')
        sock.sendall(req)

        hdr_bytes = self._read_until(b'\r\n\r\n', max_bytes=16384)
        hdr_text = hdr_bytes.decode('latin-1', errors='replace')
        status_line = hdr_text.split('\r\n', 1)[0]
        if ' 101 ' not in status_line:
            self.close()
            raise RuntimeError(f'WebSocket handshake rejected ({status_line})')

        expected_accept = base64.b64encode(
            hashlib.sha1((ws_key + _WS_GUID).encode('ascii')).digest()
        ).decode('ascii')
        m = re.search(r'^Sec-WebSocket-Accept:\s*(\S+)', hdr_text, re.I | re.M)
        if m and m.group(1).strip() != expected_accept:
            self.close()
            raise RuntimeError('WebSocket Sec-WebSocket-Accept mismatch')

        # Wait for ms.channel.connect / ms.channel.unauthorized
        deadline = time.time() + (auth_timeout or self.timeout)
        while time.time() < deadline:
            remaining = max(0.5, deadline - time.time())
            sock.settimeout(remaining)
            msg = self._recv_text()
            if msg is None:
                raise RuntimeError('TV closed WebSocket before ms.channel.connect')
            try:
                data = json.loads(msg)
            except ValueError:
                continue
            event = data.get('event')
            if event == 'ms.channel.connect':
                payload = data.get('data') or {}
                tok = payload.get('token')
                if not tok and isinstance(payload.get('clients'), list):
                    for cl in payload['clients']:
                        attrs = cl.get('attributes') if isinstance(cl, dict) else None
                        if isinstance(attrs, dict) and attrs.get('token'):
                            tok = attrs['token']
                            break
                if tok:
                    self.token = str(tok)
                sock.settimeout(self.timeout)
                return self.token
            if event in ('ms.channel.unauthorized', 'ms.channel.timeOut', 'ms.error'):
                self.close()
                raise PermissionError(f'Samsung TV denied or timed out pairing ({event})')

        self.close()
        raise TimeoutError('Timed out waiting for Samsung TV approval ("Allow" popup)')

    def _read_until(self, marker, max_bytes=32768):
        while True:
            idx = self._buf.find(marker)
            if idx != -1:
                end = idx + len(marker)
                out = bytes(self._buf[:end])
                del self._buf[:end]
                return out
            if len(self._buf) > max_bytes:
                raise RuntimeError('WebSocket HTTP response header too large')
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError('Connection closed during HTTP handshake')
            self._buf.extend(chunk)

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError('Unexpected EOF from TV WebSocket')
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode, data=b''):
        if not self._sock:
            raise ConnectionError('WebSocket not connected')
        length = len(data)
        hdr = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            hdr.append(0x80 | length)
        elif length <= 0xFFFF:
            hdr.append(0x80 | 126)
            hdr.extend(struct.pack('!H', length))
        else:
            hdr.append(0x80 | 127)
            hdr.extend(struct.pack('!Q', length))
        mask = os.urandom(4)
        hdr.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self._sock.sendall(bytes(hdr) + masked)

    def _recv_frame(self):
        b1, b2 = self._read_exact(2)
        fin = (b1 & 0x80) != 0
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack('!H', self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b''
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _recv_text(self):
        chunks = []
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x8:  # CLOSE
                try:
                    self._send_frame(0x8, b'')
                except OSError:
                    pass
                return None
            if opcode == 0x9:  # PING -> PONG
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # PONG
                continue
            if opcode in (0x1, 0x0):
                chunks.append(payload)
                if fin:
                    return b''.join(chunks).decode('utf-8', errors='replace')

    def is_connected(self):
        return self._sock is not None

    def send_key(self, key, cmd='Click'):
        payload = json.dumps({
            'method': 'ms.remote.control',
            'params': {
                'Cmd': cmd,
                'DataOfCmd': key,
                'Option': 'false',
                'TypeOfRemote': 'SendRemoteKey',
            },
        }).encode('utf-8')
        self._send_frame(0x1, payload)

    def send_text(self, text, end_input=True):
        """Type text into an active native Tizen text/IP field via SendInputString."""
        raw = str(text or '')
        b64 = base64.b64encode(raw.encode('utf-8')).decode('ascii')
        payload = json.dumps({
            'method': 'ms.remote.control',
            'params': {
                'Cmd': b64,
                'TypeOfRemote': 'SendInputString',
                'DataOfCmd': 'base64',
            },
        }).encode('utf-8')
        self._send_frame(0x1, payload)
        if end_input:
            self.send_input_end()

    def send_input_end(self):
        payload = json.dumps({
            'method': 'ms.remote.control',
            'params': {
                'Cmd': '',
                'TypeOfRemote': 'SendInputEnd',
                'DataOfCmd': 'base64',
            },
        }).encode('utf-8')
        self._send_frame(0x1, payload)

    def close(self):
        if self._sock is not None:
            try:
                self._send_frame(0x8, b'')
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._buf.clear()


class SamsungLegacyClient:
    """Legacy pre-2014 Orsay Samsung TV client over TCP port 55000."""

    def __init__(self, host, port=55000, name='NEON Remote', client_ip='192.168.1.1', timeout=5.0):
        self.host = host
        self.port = int(port)
        self.name = name
        self.client_ip = client_ip
        self.timeout = float(timeout)
        self._sock = None

    @staticmethod
    def _ser(text, raw=False):
        b = text.encode('utf-8') if isinstance(text, str) else bytes(text)
        if not raw:
            b = base64.b64encode(b)
        return struct.pack('<H', len(b)) + b

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('Legacy Samsung TV closed connection')
            buf.extend(chunk)
        return bytes(buf)

    def _read_response(self, first=False):
        hdr = self._recv_exact(3)
        tv_len = struct.unpack('<H', hdr[1:3])[0]
        _tv_name = self._recv_exact(tv_len)
        resp_len = struct.unpack('<H', self._recv_exact(2))[0]
        resp = self._recv_exact(resp_len)
        if resp == b'\x64\x00\x01\x00' or resp == b'\x00\x00\x00\x00':
            return True
        if resp[:1] == b'\x0a' and first:
            return self._read_response(first=False)
        if resp == b'\x64\x00\x00\x00' or resp[:1] == b'\x65':
            raise PermissionError('Samsung Legacy TV denied access on port 55000')
        return True

    def connect(self, auth_timeout=None):
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=auth_timeout or self.timeout)
        self._sock = s
        payload = b'\x64\x00' + self._ser(self.client_ip) + self._ser('NEON-PC') + self._ser(self.name)
        packet = b'\x00\x00\x00' + self._ser(payload, raw=True)
        s.sendall(packet)
        self._read_response(first=True)
        return None

    def is_connected(self):
        return self._sock is not None

    def send_key(self, key, cmd='Click'):
        if not self._sock:
            raise ConnectionError('Legacy client not connected')
        payload = b'\x00\x00\x00' + self._ser(key)
        packet = b'\x00\x00\x00' + self._ser(payload, raw=True)
        self._sock.sendall(packet)
        self._read_response(first=False)

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
