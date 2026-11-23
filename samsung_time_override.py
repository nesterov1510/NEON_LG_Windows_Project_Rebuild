"""Samsung Tizen time-domain override: local DNS rewrite, /openapi/timesync HTTP gateway & SNTP sync."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate, parsedate_to_datetime
import http.server
import ipaddress
import os
import platform
import re
import select
import socket
import socketserver
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from samsung_tizen import get_local_ip_for_tv

DEFAULT_SOURCE_DOMAINS = (
    'time.samsungcloudsolution.com',
    'time.samsungcloudsolution.net',
)
DEFAULT_CHECK_DOMAINS = (
    'cdn.samsungcloudsolution.com',
    'cdn.samsungcloudsolution.net',
)
DEFAULT_UPSTREAM_DNS = ('1.1.1.1', '8.8.8.8')

TIME_TARGET_PRESETS = (
    {
        'id': 'time.cloudflare.com',
        'label_en': 'Cloudflare Time · time.cloudflare.com',
        'label_ru': 'Cloudflare Time · time.cloudflare.com',
    },
    {
        'id': 'time.google.com',
        'label_en': 'Google Public NTP · time.google.com',
        'label_ru': 'Google Public NTP · time.google.com',
    },
    {
        'id': 'pool.ntp.org',
        'label_en': 'NTP Pool · pool.ntp.org',
        'label_ru': 'NTP Pool · pool.ntp.org',
    },
    {
        'id': 'time.windows.com',
        'label_en': 'Microsoft Time · time.windows.com',
        'label_ru': 'Microsoft Time · time.windows.com',
    },
    {
        'id': 'ntp.msk-ix.ru',
        'label_en': 'MSK-IX NTP · ntp.msk-ix.ru',
        'label_ru': 'MSK-IX NTP · ntp.msk-ix.ru',
    },
    {
        'id': 'local',
        'label_en': 'Local PC Clock (Offline / No Internet)',
        'label_ru': 'Часы этого ПК (Офлайн / Без интернета)',
    },
)

_NTP_EPOCH_DELTA = 2208988800  # seconds between 1900-01-01 and 1970-01-01
_MIN_PLAUSIBLE_EPOCH = 1577836800.0  # 2020-01-01T00:00:00Z
_HOST_LABEL_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$')
_FIREWALL_RULE_PREFIX = 'NEON Samsung Time Override'


def is_valid_hostname(name):
    host = str(name or '').strip().rstrip('.').lower()
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.IPv4Address(host)
        return True
    except ipaddress.AddressValueError:
        pass
    labels = host.split('.')
    if len(labels) < 2 and host != 'local':
        return False
    return all(_HOST_LABEL_RE.match(lbl) for lbl in labels)


def normalize_time_target(raw_target):
    """Parse target time domain, URL, IPv4, or 'local'. Returns dict(host, port, scheme, is_ip, is_local, raw)."""
    text = str(raw_target or '').strip()
    if not text:
        raise ValueError('Enter a target time server domain, IPv4 address, or "local".')
    low = text.lower()
    if low in ('local', 'pc', 'localhost', 'local-pc'):
        return {
            'host': 'local',
            'port': None,
            'scheme': 'local',
            'is_ip': False,
            'is_local': True,
            'raw': 'local',
        }

    scheme = ''
    port = None
    host = text
    if '://' in text:
        parsed = urllib.parse.urlparse(text)
        scheme = (parsed.scheme or '').lower()
        if scheme not in ('http', 'https', 'ntp'):
            raise ValueError('Target URL scheme must be http://, https://, or ntp:// (or enter a bare domain/IP).')
        host = (parsed.hostname or '').strip()
        port = parsed.port
    elif ':' in text and text.count(':') == 1:
        maybe_host, maybe_port = text.rsplit(':', 1)
        if not maybe_port.isdigit():
            raise ValueError('Invalid port in target server address.')
        p_num = int(maybe_port)
        if not (1 <= p_num <= 65535):
            raise ValueError('Target port must be between 1 and 65535.')
        host = maybe_host.strip()
        port = p_num

    host = host.rstrip('.').lower()
    if not host or not is_valid_hostname(host):
        raise ValueError('Target must be a valid domain (e.g. time.cloudflare.com), IPv4 address, or "local".')
    is_ip = False
    try:
        ip = ipaddress.IPv4Address(host)
        if ip.is_unspecified or ip.is_multicast:
            raise ValueError('Target IPv4 address cannot be unspecified or multicast.')
        is_ip = True
    except ipaddress.AddressValueError:
        pass

    return {
        'host': host,
        'port': port,
        'scheme': scheme,
        'is_ip': is_ip,
        'is_local': False,
        'raw': text,
    }


def parse_source_domains(raw_domains, intercept_check=True):
    """Parse user-configured source domains intercepted by DNS override."""
    if raw_domains is None or not str(raw_domains).strip():
        items = list(DEFAULT_SOURCE_DOMAINS)
    else:
        tokens = re.split(r'[\s,;]+', str(raw_domains).strip())
        items = []
        for tok in tokens:
            if not tok:
                continue
            dom = tok.strip().rstrip('.').lower()
            if '://' in dom:
                dom = (urllib.parse.urlparse(dom).hostname or '').rstrip('.').lower()
            if not dom or not is_valid_hostname(dom) or dom == 'local':
                raise ValueError(f'Invalid Samsung source domain: {tok}')
            if dom not in items:
                items.append(dom)
        if not items:
            items = list(DEFAULT_SOURCE_DOMAINS)

    if intercept_check:
        for chk in DEFAULT_CHECK_DOMAINS:
            if chk not in items:
                items.append(chk)
    return items


def parse_upstream_dns(raw_upstream, allow_loopback=False):
    """Parse comma/space-separated IPv4 upstream DNS resolvers, avoiding loopback loops."""
    if not raw_upstream:
        return list(DEFAULT_UPSTREAM_DNS)
    if isinstance(raw_upstream, (list, tuple)):
        tokens = [str(x).strip() for x in raw_upstream if str(x).strip()]
    else:
        tokens = [t.strip() for t in re.split(r'[\s,;]+', str(raw_upstream)) if t.strip()]
    valid = []
    for tok in tokens:
        try:
            ip = ipaddress.IPv4Address(tok)
        except ipaddress.AddressValueError:
            continue
        if ip.is_unspecified or ip.is_multicast:
            continue
        if not allow_loopback and ip.is_loopback:
            continue
        s = str(ip)
        if s not in valid:
            valid.append(s)
    return valid or list(DEFAULT_UPSTREAM_DNS)


# ---------------------------------------------------------------------------
# DNS Wire Parsing & Response Construction (RFC 1035)
# ---------------------------------------------------------------------------
def _decode_dns_name(packet, offset):
    """Decode DNS QNAME starting at offset; returns (name_str, next_offset)."""
    labels = []
    pos = offset
    jumped = False
    end_offset = offset
    seen_pointers = set()
    pkt_len = len(packet)

    while pos < pkt_len:
        length = packet[pos]
        if length == 0:
            if not jumped:
                end_offset = pos + 1
            break
        if (length & 0xC0) == 0xC0:
            if pos + 1 >= pkt_len:
                raise ValueError('Truncated DNS compression pointer')
            ptr = ((length & 0x3F) << 8) | packet[pos + 1]
            if ptr in seen_pointers or ptr >= pkt_len:
                raise ValueError('Invalid DNS compression pointer loop')
            seen_pointers.add(ptr)
            if not jumped:
                end_offset = pos + 2
                jumped = True
            pos = ptr
            continue
        if length > 63:
            raise ValueError('Unsupported DNS label length')
        pos += 1
        if pos + length > pkt_len:
            raise ValueError('Truncated DNS label')
        labels.append(packet[pos:pos + length].decode('ascii', errors='replace'))
        pos += length
    else:
        raise ValueError('Unterminated DNS name')

    if not jumped and end_offset == offset:
        end_offset = pos + 1
    return '.'.join(labels), end_offset


def parse_dns_question(packet):
    """Parse header + first question of a DNS query packet."""
    if not packet or len(packet) < 12:
        raise ValueError('DNS packet shorter than 12-byte header')
    txid, flags, qdcount, _an, _ns, _ar = struct.unpack('!HHHHHH', packet[:12])
    if qdcount < 1:
        raise ValueError('DNS packet contains no questions')
    qname, qend = _decode_dns_name(packet, 12)
    if qend + 4 > len(packet):
        raise ValueError('Truncated DNS question section')
    qtype, qclass = struct.unpack('!HH', packet[qend:qend + 4])
    question_wire = packet[12:qend + 4]
    return {
        'txid': txid,
        'flags': flags,
        'qdcount': qdcount,
        'qname': qname,
        'normalized_name': qname.rstrip('.').lower(),
        'qtype': qtype,
        'qclass': qclass,
        'question_wire': question_wire,
    }


def build_dns_a_response(query_info, target_ipv4, ttl=30):
    """Build a valid RFC 1035 IPv4 A response (or NOERROR NODATA for AAAA/HTTPS)."""
    txid = query_info['txid']
    rd = query_info['flags'] & 0x0100
    resp_flags = 0x8080 | rd  # QR=1, AA=0, TC=0, RD=copied, RA=1, RCODE=0
    qtype = query_info['qtype']
    qclass = query_info['qclass']

    if qclass == 1 and qtype in (1, 255):  # IN A or ANY
        header = struct.pack('!HHHHHH', txid, resp_flags, 1, 1, 0, 0)
        answer = (
            b'\xc0\x0c'  # pointer to QNAME at offset 12
            + struct.pack('!HHIH', 1, 1, max(1, int(ttl)), 4)
            + socket.inet_aton(target_ipv4)
        )
        return header + query_info['question_wire'] + answer

    # For AAAA (28), HTTPS (65), SVCB (64), etc.: return NOERROR with 0 answers so TV uses IPv4 A
    header = struct.pack('!HHHHHH', txid, resp_flags, 1, 0, 0, 0)
    return header + query_info['question_wire']


def build_dns_query_packet(domain, qtype=1, txid=None):
    """Build a standard recursive DNS UDP query packet."""
    if txid is None:
        txid = int.from_bytes(os.urandom(2), 'big')
    labels = [lbl.encode('ascii') for lbl in domain.rstrip('.').split('.') if lbl]
    qname = b''.join(bytes([len(l)]) + l for l in labels) + b'\x00'
    header = struct.pack('!HHHHHH', txid, 0x0100, 1, 0, 0, 0)
    return txid, header + qname + struct.pack('!HH', qtype, 1)


def resolve_ipv4_via_upstream(domain, upstream_servers, timeout=2.5, port=53):
    """Resolve an external domain to IPv4 using explicit upstream DNS (bypassing local intercept)."""
    try:
        ipaddress.IPv4Address(domain)
        return domain
    except ipaddress.AddressValueError:
        pass

    txid, query = build_dns_query_packet(domain, qtype=1)
    for server_ip in parse_upstream_dns(upstream_servers, allow_loopback=True):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(query, (server_ip, int(port)))
            data, _ = sock.recvfrom(4096)
            if len(data) < 12:
                continue
            r_txid, _rflags, qd, an, _ns, _ar = struct.unpack('!HHHHHH', data[:12])
            if r_txid != txid or an < 1:
                continue
            pos = 12
            for _ in range(qd):
                _, pos = _decode_dns_name(data, pos)
                pos += 4
            for _ in range(an):
                if pos >= len(data):
                    break
                _, pos = _decode_dns_name(data, pos)
                if pos + 10 > len(data):
                    break
                rtype, rclass, _ttl, rdlen = struct.unpack('!HHIH', data[pos:pos + 10])
                pos += 10
                if pos + rdlen > len(data):
                    break
                if rtype == 1 and rclass == 1 and rdlen == 4:
                    return socket.inet_ntoa(data[pos:pos + 4])
                pos += rdlen
        except OSError:
            continue
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # Fallback to OS resolver if direct UDP 53 upstream isn't reachable (e.g. corporate/VPN DNS)
    for res in socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_DGRAM):
        candidate = res[4][0]
        if candidate and not candidate.startswith('127.'):
            return candidate
    raise OSError(f'Unable to resolve IPv4 address for {domain}')


# ---------------------------------------------------------------------------
# SNTP Client / Server & HTTP Time Synchronization
# ---------------------------------------------------------------------------
def _ntp_to_unix(sec_word, frac_word):
    return (float(sec_word) - _NTP_EPOCH_DELTA) + (float(frac_word) / 4294967296.0)


def _unix_to_ntp(unix_ts):
    ntp_float = float(unix_ts) + _NTP_EPOCH_DELTA
    if ntp_float < 0:
        ntp_float = 0.0
    sec_word = int(ntp_float)
    frac_word = int((ntp_float - sec_word) * 4294967296.0) & 0xFFFFFFFF
    return sec_word & 0xFFFFFFFF, frac_word


def fetch_sntp_time(host, port=123, timeout=2.5, upstream_dns=None):
    """Query an NTP server over UDP and return dict(unix_time, offset_sec, rtt_ms, remote_ip)."""
    remote_ip = resolve_ipv4_via_upstream(host, upstream_dns or DEFAULT_UPSTREAM_DNS, timeout=timeout)
    pkt = bytearray(48)
    pkt[0] = 0x1B  # LI=0, VN=3, Mode=3 (client)
    t1_unix = time.time()
    t1_s, t1_f = _unix_to_ntp(t1_unix)
    struct.pack_into('!II', pkt, 40, t1_s, t1_f)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(bytes(pkt), (remote_ip, int(port)))
        resp, _ = sock.recvfrom(1024)
        t4_unix = time.time()
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if len(resp) < 48:
        raise ValueError(f'Short NTP response ({len(resp)} bytes) from {host}')
    rx_s, rx_f, tx_s, tx_f = struct.unpack('!IIII', resp[32:48])
    if tx_s == 0:
        raise ValueError(f'Zero transmit timestamp in NTP response from {host}')
    t2_unix = _ntp_to_unix(rx_s, rx_f) if rx_s else _ntp_to_unix(tx_s, tx_f)
    t3_unix = _ntp_to_unix(tx_s, tx_f)
    if t3_unix < _MIN_PLAUSIBLE_EPOCH:
        raise ValueError(f'Implausible NTP timestamp from {host}: {t3_unix}')

    offset_sec = ((t2_unix - t1_unix) + (t3_unix - t4_unix)) / 2.0
    rtt_ms = max(0.0, (t4_unix - t1_unix) * 1000.0)
    return {
        'unix_time': t3_unix,
        'offset_sec': offset_sec,
        'rtt_ms': rtt_ms,
        'remote_ip': remote_ip,
    }


def fetch_http_time(host, scheme='', port=None, timeout=3.0):
    """Fetch UTC time via /openapi/timesync binary or HTTP/HTTPS Date header."""
    schemes = [scheme] if scheme in ('http', 'https') else ['https', 'http']
    last_err = None
    for sch in schemes:
        p_part = f':{port}' if port else ''
        for path, method in (('/openapi/timesync?client=NEON', 'GET'), ('/', 'HEAD')):
            url = f'{sch}://{host}{p_part}{path}'
            req = urllib.request.Request(url, method=method, headers={'User-Agent': 'NEON-TimeSync/1.0'})
            t_req = time.time()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    t_resp = time.time()
                    body = resp.read(64) if method == 'GET' else b''
                    if path.startswith('/openapi/timesync') and len(body) == 16:
                        ms1, _ms2 = struct.unpack('<QQ', body)
                        sec = ms1 / 1000.0
                        if sec >= _MIN_PLAUSIBLE_EPOCH:
                            offset_sec = sec - ((t_req + t_resp) / 2.0)
                            return {'unix_time': sec, 'offset_sec': offset_sec, 'method': f'{sch.upper()} timesync'}
                    date_hdr = resp.headers.get('Date')
                    if date_hdr:
                        dt = parsedate_to_datetime(date_hdr)
                        sec = dt.timestamp()
                        if sec >= _MIN_PLAUSIBLE_EPOCH:
                            offset_sec = sec - ((t_req + t_resp) / 2.0)
                            return {'unix_time': sec, 'offset_sec': offset_sec, 'method': f'{sch.upper()} Date'}
            except urllib.error.HTTPError as herr:
                t_resp = time.time()
                date_hdr = herr.headers.get('Date') if herr.headers else None
                if date_hdr:
                    try:
                        dt = parsedate_to_datetime(date_hdr)
                        sec = dt.timestamp()
                        if sec >= _MIN_PLAUSIBLE_EPOCH:
                            offset_sec = sec - ((t_req + t_resp) / 2.0)
                            return {'unix_time': sec, 'offset_sec': offset_sec, 'method': f'{sch.upper()} Date'}
                    except Exception:
                        pass
                last_err = herr
            except Exception as exc:
                last_err = exc
    raise OSError(f'HTTP time probe failed for {host}: {last_err}')


def sync_clock_from_target(target_spec, upstream_dns=None, timeout=2.5):
    """Synchronize UTC offset against user-selected domain/IP or local PC clock."""
    spec = normalize_time_target(target_spec) if isinstance(target_spec, str) else dict(target_spec)
    if spec['is_local']:
        now_s = time.time()
        return {
            'ok': True,
            'target': 'local',
            'method': 'local-pc',
            'offset_sec': 0.0,
            'offset_ms': 0,
            'utc_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_s)),
            'remote_ip': '127.0.0.1',
        }

    host = spec['host']
    scheme = spec.get('scheme', '')
    port = spec.get('port')
    if scheme in ('http', 'https') or (port is not None and port != 123):
        res = fetch_http_time(host, scheme=scheme or 'http', port=port, timeout=timeout)
        now_s = time.time() + res['offset_sec']
        return {
            'ok': True,
            'target': host,
            'method': res['method'],
            'offset_sec': res['offset_sec'],
            'offset_ms': int(round(res['offset_sec'] * 1000.0)),
            'utc_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_s)),
            'remote_ip': host if spec.get('is_ip') else '',
        }

    try:
        ntp = fetch_sntp_time(host, port=port or 123, timeout=timeout, upstream_dns=upstream_dns)
        now_s = time.time() + ntp['offset_sec']
        return {
            'ok': True,
            'target': host,
            'method': f'SNTP :123 ({ntp["rtt_ms"]:.0f}ms)',
            'offset_sec': ntp['offset_sec'],
            'offset_ms': int(round(ntp['offset_sec'] * 1000.0)),
            'utc_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_s)),
            'remote_ip': ntp['remote_ip'],
        }
    except Exception as ntp_exc:
        try:
            http_res = fetch_http_time(host, timeout=timeout)
            now_s = time.time() + http_res['offset_sec']
            return {
                'ok': True,
                'target': host,
                'method': http_res['method'],
                'offset_sec': http_res['offset_sec'],
                'offset_ms': int(round(http_res['offset_sec'] * 1000.0)),
                'utc_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_s)),
                'remote_ip': '',
            }
        except Exception as http_exc:
            raise OSError(f'SNTP ({ntp_exc}) & HTTP ({http_exc}) sync failed for {host}')


# ---------------------------------------------------------------------------
# HTTP Responder for Samsung /openapi/timesync & check.xml
# ---------------------------------------------------------------------------
class _SamsungTimeHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = (os.name != 'nt')

    def __init__(self, server_address, RequestHandlerClass, controller):
        self.controller = controller
        super().__init__(server_address, RequestHandlerClass)

    def handle_error(self, request, client_address):
        return


class _SamsungTimeHttpHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 5.0

    def log_message(self, format, *args):  # noqa: A002
        return

    def _local_utc_offset_seconds(self):
        if time.daylight and time.localtime().tm_isdst > 0:
            return -int(time.altzone)
        return -int(time.timezone)

    def _send_bytes(self, code, body, content_type, send_body=True):
        self.close_connection = True
        now_sec = self.server.controller.current_utc_seconds()
        self.send_response(code)
        self.send_header('Date', formatdate(now_sec, usegmt=True))
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Connection', 'close')
        self.end_headers()
        if send_body and body:
            self.wfile.write(body)
        try:
            self.wfile.flush()
        except OSError:
            pass

    def _handle(self, send_body=True):
        parsed = urllib.parse.urlsplit(self.path or '/')
        path = parsed.path or '/'
        path_low = path.lower()
        client_ip = self.client_address[0] if self.client_address else ''
        host_hdr = (self.headers.get('Host') or '').split(':', 1)[0].strip()

        if path_low.startswith('/openapi/timesync'):
            now_ms = self.server.controller.current_utc_ms()
            payload = struct.pack('<QQ', now_ms, now_ms)
            self.server.controller._record_event(
                'http_timesync', client_ip, f'{host_hdr or "time"} {path}',
            )
            self._send_bytes(200, payload, 'application/octet-stream', send_body=send_body)
            return

        if path_low == '/public/network/files/check.xml':
            self.server.controller._record_event(
                'http_check', client_ip, f'{host_hdr or "cdn"} {path}',
            )
            self._send_bytes(200, b'ok\n', 'text/plain; charset=utf-8', send_body=send_body)
            return

        if path_low.startswith('/openapi/zipcode/timezoneoffset'):
            offset_sec = self._local_utc_offset_seconds()
            body = f'{{"offset":{offset_sec},"utcOffsetMinutes":{offset_sec // 60}}}'.encode('utf-8')
            self.server.controller._record_event(
                'http_tz', client_ip, f'{host_hdr or "time"} {path}',
            )
            self._send_bytes(200, body, 'application/json; charset=utf-8', send_body=send_body)
            return

        self.server.controller._record_event('http_other', client_ip, f'{host_hdr} {path}')
        self._send_bytes(200, b'ok\n', 'text/plain; charset=utf-8', send_body=send_body)

    def do_GET(self):  # noqa: N802
        self._handle(send_body=True)

    def do_HEAD(self):  # noqa: N802
        self._handle(send_body=False)


# ---------------------------------------------------------------------------
# Windows Firewall Rule Helpers (idempotent, cleaned up on stop)
# ---------------------------------------------------------------------------
def _apply_windows_firewall_rules(ports):
    if platform.system() != 'Windows':
        return []
    notes = []
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    for proto, port, suffix in ports:
        if not port:
            continue
        name = f'{_FIREWALL_RULE_PREFIX} ({suffix})'
        try:
            subprocess.run(
                ['netsh', 'advfirewall', 'firewall', 'delete', 'rule', f'name={name}'],
                capture_output=True, text=True, timeout=8, creationflags=flags,
            )
            proc = subprocess.run(
                [
                    'netsh', 'advfirewall', 'firewall', 'add', 'rule',
                    f'name={name}', 'dir=in', 'action=allow',
                    f'protocol={proto}', f'localport={int(port)}',
                    'profile=private,public,domain',
                ],
                capture_output=True, text=True, timeout=8, creationflags=flags,
            )
            if proc.returncode != 0:
                notes.append(f'[!] Firewall rule "{name}" warning: {(proc.stderr or proc.stdout).strip()}')
        except Exception as exc:
            notes.append(f'[!] Firewall rule "{name}" skipped: {exc}')
    return notes


def _remove_windows_firewall_rules():
    if platform.system() != 'Windows':
        return
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    for suffix in ('DNS UDP 53', 'DNS TCP 53', 'HTTP 80', 'NTP 123'):
        name = f'{_FIREWALL_RULE_PREFIX} ({suffix})'
        try:
            subprocess.run(
                ['netsh', 'advfirewall', 'firewall', 'delete', 'rule', f'name={name}'],
                capture_output=True, text=True, timeout=8, creationflags=flags,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main Controller / Embedded DNS + HTTP + NTP Server
# ---------------------------------------------------------------------------
class SamsungTimeOverrideServer:
    """Self-contained DNS rewrite + Samsung /openapi/timesync HTTP + NTP server."""

    def __init__(self, log_cb=None):
        self._log_cb = log_cb
        self._lock = threading.RLock()
        self._running = False
        self._stop_event = threading.Event()

        self.mode = 'gateway'  # 'gateway' or 'dns_direct'
        self.target = 'time.cloudflare.com'
        self.target_spec = normalize_time_target(self.target)
        self.source_domains = list(DEFAULT_SOURCE_DOMAINS)
        self.intercept_check = True
        self.upstream_dns = list(DEFAULT_UPSTREAM_DNS)
        self.upstream_port = 53
        self.tv_ip = ''
        self.advertise_ip = ''
        self.direct_target_ip = ''

        self.dns_bind_ip = '0.0.0.0'
        self.dns_port = 53
        self.http_bind_ip = '0.0.0.0'
        self.http_port = 80
        self.ntp_port = 123

        self._dns_udp_sock = None
        self._dns_tcp_sock = None
        self._http_server = None
        self._ntp_sock = None
        self._pool = None
        self._threads = []

        self._offset_sec = 0.0
        self._sync_method = 'not synced'
        self._last_sync_at = 0.0
        self._sync_error = ''

        self._stats = {
            'dns_total': 0,
            'dns_overridden': 0,
            'dns_forwarded': 0,
            'http_timesync': 0,
            'http_check': 0,
            'http_tz': 0,
            'http_other': 0,
            'ntp_hits': 0,
            'last_query_at': 0.0,
            'last_client_ip': '',
            'last_domain': '',
        }
        self._events = deque(maxlen=25)
        self._route_cache = {}
        self._firewall_applied = False

    def _emit(self, line):
        if self._log_cb:
            try:
                self._log_cb(line)
            except Exception:
                pass

    def _record_event(self, kind, client_ip, detail):
        now = time.time()
        with self._lock:
            if kind in self._stats:
                self._stats[kind] += 1
            if kind in ('dns_overridden', 'http_timesync', 'http_check', 'ntp_hits'):
                self._stats['last_query_at'] = now
                if client_ip:
                    self._stats['last_client_ip'] = client_ip
                if detail:
                    self._stats['last_domain'] = detail
            stamp = time.strftime('%H:%M:%S', time.localtime(now))
            self._events.append(f'[{stamp}] {kind}: {client_ip or "?"} -> {detail}')

    def current_utc_seconds(self):
        with self._lock:
            offset = self._offset_sec
        return time.time() + offset

    def current_utc_ms(self):
        return int(round(self.current_utc_seconds() * 1000.0))

    def _local_ip_for_client(self, client_ip):
        if self.advertise_ip:
            return self.advertise_ip
        cached = self._route_cache.get(client_ip)
        if cached:
            return cached
        detected = get_local_ip_for_tv(client_ip or self.tv_ip or '192.168.1.101')
        if detected and not detected.startswith('127.'):
            self._route_cache[client_ip] = detected
            return detected
        return get_local_ip_for_tv(self.tv_ip or '192.168.1.101')

    def sync_now(self, target=None, upstream_dns=None, upstream_port=None):
        with self._lock:
            tgt = target if target is not None else self.target
            up = parse_upstream_dns(upstream_dns, allow_loopback=True) if upstream_dns is not None else list(self.upstream_dns)
            uport = int(upstream_port) if upstream_port is not None else int(self.upstream_port)
        spec = normalize_time_target(tgt)
        if spec['host'] in self.source_domains:
            raise ValueError(
                f'Target "{spec["host"]}" is one of the intercepted Samsung source domains; '
                'choose an external time server (e.g. time.cloudflare.com) or "local".'
            )

        if self.mode == 'dns_direct' and not spec['is_local']:
            ip = spec['host'] if spec['is_ip'] else resolve_ipv4_via_upstream(spec['host'], up, port=uport)
            with self._lock:
                self.direct_target_ip = ip
                self._last_sync_at = time.time()
                self._sync_error = ''
                self._sync_method = f'DNS A -> {ip}'
            return {
                'ok': True,
                'mode': self.mode,
                'target': spec['host'],
                'direct_ip': ip,
                'method': f'DNS A -> {ip}',
                'offset_ms': 0,
                'utc_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }

        res = sync_clock_from_target(spec, upstream_dns=up)
        with self._lock:
            self._offset_sec = float(res['offset_sec'])
            self._sync_method = res['method']
            self._last_sync_at = time.time()
            self._sync_error = ''
        return res

    def _bind_udp_with_fallback(self, bind_ip, port, fallback_ip=''):
        reuse = (os.name != 'nt')
        candidates = [bind_ip]
        if bind_ip == '0.0.0.0' and fallback_ip and fallback_ip not in candidates:
            candidates.append(fallback_ip)
        last_exc = None
        for ip in candidates:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if reuse:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((ip, int(port)))
                return sock, ip, sock.getsockname()[1]
            except OSError as exc:
                last_exc = exc
                try:
                    sock.close()
                except OSError:
                    pass
        raise last_exc

    def _bind_tcp_dns_optional(self, bind_ip, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_ip, int(port)))
            sock.listen(16)
            return sock
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            return None

    def _bind_http_with_fallback(self, bind_ip, port, fallback_ip=''):
        candidates = [bind_ip]
        if bind_ip == '0.0.0.0' and fallback_ip and fallback_ip not in candidates:
            candidates.append(fallback_ip)
        last_exc = None
        for ip in candidates:
            try:
                srv = _SamsungTimeHttpServer((ip, int(port)), _SamsungTimeHttpHandler, self)
                return srv, ip, srv.server_address[1]
            except OSError as exc:
                last_exc = exc
        raise last_exc

    def start(
        self,
        target='time.cloudflare.com',
        mode='gateway',
        source_domains=None,
        intercept_check=True,
        upstream_dns=None,
        tv_ip='',
        advertise_ip='',
        dns_bind_ip='0.0.0.0',
        dns_port=53,
        http_bind_ip='0.0.0.0',
        http_port=80,
        ntp_port=123,
        upstream_port=53,
        allow_loopback_upstream=False,
        manage_firewall=True,
    ):
        if self._running:
            self.stop()
        with self._lock:
            if self._running:
                return self.get_status()

            clean_mode = 'dns_direct' if str(mode or '').strip().lower() == 'dns_direct' else 'gateway'
            spec = normalize_time_target(target)
            if clean_mode == 'dns_direct' and spec['is_local']:
                raise ValueError('Direct DNS A-record mode requires a remote domain or IPv4 server (or switch to NEON Time Gateway).')

            sources = parse_source_domains(source_domains, intercept_check=bool(intercept_check))
            if spec['host'] in sources:
                raise ValueError(
                    f'Target server "{spec["host"]}" cannot also be in the intercepted Samsung source domain list.'
                )

            upstreams = parse_upstream_dns(upstream_dns, allow_loopback=allow_loopback_upstream)
            clean_tv_ip = str(tv_ip or '').strip()
            fallback_lan_ip = advertise_ip or get_local_ip_for_tv(clean_tv_ip or '192.168.1.101')

            self.mode = clean_mode
            self.target = spec['raw']
            self.target_spec = spec
            self.source_domains = sources
            self.intercept_check = bool(intercept_check)
            self.upstream_dns = upstreams
            self.upstream_port = int(upstream_port)
            self.tv_ip = clean_tv_ip
            self.advertise_ip = str(advertise_ip or '').strip()
            self.direct_target_ip = ''
            self._route_cache.clear()
            self._events.clear()
            for k in self._stats:
                self._stats[k] = 0 if isinstance(self._stats[k], int) else (0.0 if isinstance(self._stats[k], float) else '')

            notes = []
            # Resolve / sync before binding sockets so direct IP or clock offset is ready immediately.
            if clean_mode == 'dns_direct':
                direct_ip = spec['host'] if spec['is_ip'] else resolve_ipv4_via_upstream(
                    spec['host'], upstreams, timeout=3.0, port=self.upstream_port,
                )
                self.direct_target_ip = direct_ip
                self._offset_sec = 0.0
                self._sync_method = f'DNS A -> {direct_ip}'
                self._sync_error = ''
                self._last_sync_at = time.time()
                notes.append(f'[+] Direct DNS rewrite target resolved: {spec["host"]} -> {direct_ip}')
            else:
                try:
                    sync_res = sync_clock_from_target(spec, upstream_dns=upstreams, timeout=2.5)
                    self._offset_sec = float(sync_res['offset_sec'])
                    self._sync_method = sync_res['method']
                    self._sync_error = ''
                    self._last_sync_at = time.time()
                    notes.append(
                        f'[+] Synced UTC offset {sync_res["offset_ms"]:+d} ms from {spec["host"]} ({sync_res["method"]})'
                    )
                except Exception as exc:
                    self._offset_sec = 0.0
                    self._sync_method = 'fallback: local-pc'
                    self._sync_error = str(exc)
                    self._last_sync_at = time.time()
                    notes.append(
                        f'[!] Could not reach {spec["host"]} right now ({exc}); using PC clock until reachable.'
                    )

            # Bind DNS UDP (required) + optional TCP DNS
            dns_sock = None
            dns_tcp_sock = None
            bound_dns_ip = dns_bind_ip
            bound_dns_port = int(dns_port)
            attempts = 8 if int(dns_port) == 0 else 1
            for attempt in range(attempts):
                try:
                    cand_udp, cand_ip, cand_port = self._bind_udp_with_fallback(
                        dns_bind_ip, dns_port, fallback_ip=fallback_lan_ip,
                    )
                except OSError as exc:
                    raise OSError(
                        f'Could not bind DNS port {dns_port} ({exc}). Close any other local DNS server or check Administrator rights.'
                    )
                cand_tcp = self._bind_tcp_dns_optional(cand_ip, cand_port)
                if cand_tcp is not None or attempt == attempts - 1:
                    dns_sock, bound_dns_ip, bound_dns_port = cand_udp, cand_ip, cand_port
                    dns_tcp_sock = cand_tcp
                    break
                try:
                    cand_udp.close()
                except OSError:
                    pass

            if dns_bind_ip == '0.0.0.0' and bound_dns_ip != '0.0.0.0':
                notes.append(
                    f'[i] 0.0.0.0:{dns_port} busy (e.g. Windows ICS on 192.168.137.1); bound LAN adapter {bound_dns_ip}:{bound_dns_port}.'
                )
            http_srv = None
            bound_http_ip = ''
            bound_http_port = 0
            ntp_sock = None
            bound_ntp_port = 0

            try:
                if clean_mode == 'gateway':
                    try:
                        http_srv, bound_http_ip, bound_http_port = self._bind_http_with_fallback(
                            http_bind_ip, http_port, fallback_ip=fallback_lan_ip,
                        )
                    except OSError as exc:
                        raise OSError(
                            f'Could not bind HTTP port {http_port} for Samsung /openapi/timesync ({exc}). '
                            'Stop IIS/Nginx/port-80 apps or use Direct DNS mode.'
                        )

                    if ntp_port is not None:
                        try:
                            ntp_sock, _ntp_ip, bound_ntp_port = self._bind_udp_with_fallback(
                                bound_dns_ip, ntp_port, fallback_ip=fallback_lan_ip,
                            )
                        except OSError as ntp_exc:
                            notes.append(f'[i] Optional NTP UDP:{ntp_port} skipped ({ntp_exc}); HTTP :80 + DNS :53 active.')
            except Exception:
                for s in (dns_sock, dns_tcp_sock):
                    if s is not None:
                        try:
                            s.close()
                        except OSError:
                            pass
                if http_srv is not None:
                    try:
                        http_srv.server_close()
                    except Exception:
                        pass
                raise

            self._dns_udp_sock = dns_sock
            self._dns_tcp_sock = dns_tcp_sock
            self._http_server = http_srv
            self._ntp_sock = ntp_sock
            self.dns_bind_ip = bound_dns_ip
            self.dns_port = bound_dns_port
            self.http_bind_ip = bound_http_ip
            self.http_port = bound_http_port
            self.ntp_port = bound_ntp_port

            if manage_firewall and platform.system() == 'Windows':
                fw_ports = [('UDP', bound_dns_port, 'DNS UDP 53')]
                if dns_tcp_sock is not None:
                    fw_ports.append(('TCP', bound_dns_port, 'DNS TCP 53'))
                if http_srv is not None:
                    fw_ports.append(('TCP', bound_http_port, 'HTTP 80'))
                if ntp_sock is not None:
                    fw_ports.append(('UDP', bound_ntp_port, 'NTP 123'))
                fw_notes = _apply_windows_firewall_rules(fw_ports)
                notes.extend(fw_notes)
                self._firewall_applied = True

            self._stop_event.clear()
            self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix='neon-dns')
            self._threads = []

            t_dns = threading.Thread(target=self._dns_udp_loop, name='neon-dns-udp', daemon=True)
            t_dns.start()
            self._threads.append(t_dns)

            if dns_tcp_sock is not None:
                t_dtcp = threading.Thread(target=self._dns_tcp_loop, name='neon-dns-tcp', daemon=True)
                t_dtcp.start()
                self._threads.append(t_dtcp)

            if http_srv is not None:
                t_http = threading.Thread(target=http_srv.serve_forever, name='neon-sam-http', daemon=True)
                t_http.start()
                self._threads.append(t_http)

            if ntp_sock is not None:
                t_ntp = threading.Thread(target=self._ntp_loop, name='neon-sam-ntp', daemon=True)
                t_ntp.start()
                self._threads.append(t_ntp)

            if clean_mode == 'gateway' and not spec['is_local']:
                t_sync = threading.Thread(target=self._periodic_sync_loop, name='neon-sam-sync', daemon=True)
                t_sync.start()
                self._threads.append(t_sync)

            self._running = True
            display_pc_ip = self.advertise_ip or (
                bound_dns_ip if bound_dns_ip not in ('0.0.0.0', '') else fallback_lan_ip
            )
            notes.append(
                f'[+] Samsung Time Override ACTIVE ({clean_mode}) · DNS {bound_dns_ip}:{bound_dns_port}'
                + (f' · HTTP {bound_http_ip}:{bound_http_port}' if http_srv else '')
                + (f' · NTP :{bound_ntp_port}' if ntp_sock else '')
            )
            notes.append(f'[+] Set Samsung TV DNS manually to: {display_pc_ip} (upstream DNS: {", ".join(upstreams)})')
            for line in notes:
                self._emit(line)

            status = self.get_status()
            status['output'] = '\n'.join(notes)
            return status

    def stop(self):
        with self._lock:
            was_running = self._running
            self._running = False
            self._stop_event.set()
            dns_udp = self._dns_udp_sock
            dns_tcp = self._dns_tcp_sock
            http_srv = self._http_server
            ntp_sock = self._ntp_sock
            pool = self._pool
            threads = list(self._threads)
            fw = self._firewall_applied
            self._dns_udp_sock = None
            self._dns_tcp_sock = None
            self._http_server = None
            self._ntp_sock = None
            self._pool = None
            self._threads = []
            self._firewall_applied = False

        if http_srv is not None:
            try:
                http_srv.shutdown()
            except Exception:
                pass
            try:
                http_srv.server_close()
            except Exception:
                pass
        for s in (dns_udp, dns_tcp, ntp_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        for t in threads:
            try:
                t.join(timeout=1.2)
            except Exception:
                pass
        if pool is not None:
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass
        if fw and platform.system() == 'Windows':
            _remove_windows_firewall_rules()

        msg = '[+] Samsung Time Override stopped.' if was_running else '[i] Samsung Time Override is already stopped.'
        if was_running:
            self._emit(msg)
        status = self.get_status()
        status['output'] = msg
        return status

    def _answer_dns_packet(self, packet, client_ip):
        info = parse_dns_question(packet)
        qname = info['normalized_name']
        with self._lock:
            self._stats['dns_total'] += 1
            matched = qname in self.source_domains
            mode = self.mode
            direct_ip = self.direct_target_ip
            upstreams = list(self.upstream_dns)
            uport = int(self.upstream_port)

        if matched:
            answer_ip = direct_ip if (mode == 'dns_direct' and direct_ip) else self._local_ip_for_client(client_ip)
            resp = build_dns_a_response(info, answer_ip, ttl=30)
            self._record_event('dns_overridden', client_ip, f'{qname} -> {answer_ip}')
            return resp

        for up_ip in upstreams:
            fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            fwd.settimeout(2.5)
            try:
                fwd.sendto(packet, (up_ip, uport))
                resp, _ = fwd.recvfrom(4096)
                with self._lock:
                    self._stats['dns_forwarded'] += 1
                return resp
            except OSError:
                continue
            finally:
                try:
                    fwd.close()
                except OSError:
                    pass
        return None

    def _handle_udp_dns_packet(self, packet, addr):
        sock = self._dns_udp_sock
        if sock is None or self._stop_event.is_set():
            return
        try:
            resp = self._answer_dns_packet(packet, addr[0])
            if resp and not self._stop_event.is_set():
                sock.sendto(resp, addr)
        except Exception:
            pass

    def _dns_udp_loop(self):
        while not self._stop_event.is_set():
            sock = self._dns_udp_sock
            if sock is None:
                break
            try:
                readable, _, _ = select.select([sock], [], [], 0.35)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                data, addr = sock.recvfrom(4096)
            except OSError:
                break
            pool = self._pool
            if pool is not None and not self._stop_event.is_set():
                try:
                    pool.submit(self._handle_udp_dns_packet, data, addr)
                except RuntimeError:
                    break

    def _handle_tcp_dns_client(self, conn, addr):
        try:
            conn.settimeout(3.0)
            len_hdr = conn.recv(2)
            if len(len_hdr) < 2:
                return
            msg_len = struct.unpack('!H', len_hdr)[0]
            if msg_len < 12 or msg_len > 65535:
                return
            buf = bytearray()
            while len(buf) < msg_len:
                chunk = conn.recv(msg_len - len(buf))
                if not chunk:
                    return
                buf.extend(chunk)
            resp = self._answer_dns_packet(bytes(buf), addr[0])
            if resp:
                conn.sendall(struct.pack('!H', len(resp)) + resp)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dns_tcp_loop(self):
        while not self._stop_event.is_set():
            sock = self._dns_tcp_sock
            if sock is None:
                break
            try:
                readable, _, _ = select.select([sock], [], [], 0.35)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                conn, addr = sock.accept()
            except OSError:
                break
            pool = self._pool
            if pool is not None and not self._stop_event.is_set():
                try:
                    pool.submit(self._handle_tcp_dns_client, conn, addr)
                except RuntimeError:
                    try:
                        conn.close()
                    except OSError:
                        pass
                    break
            else:
                try:
                    conn.close()
                except OSError:
                    pass

    def _ntp_loop(self):
        while not self._stop_event.is_set():
            sock = self._ntp_sock
            if sock is None:
                break
            try:
                readable, _, _ = select.select([sock], [], [], 0.35)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                data, addr = sock.recvfrom(1024)
                if len(data) < 48:
                    continue
                rx_unix = self.current_utc_seconds()
                vn = (data[0] >> 3) & 0x07
                if vn < 1 or vn > 4:
                    vn = 4
                tx_origin = data[40:48]
                rx_s, rx_f = _unix_to_ntp(rx_unix)
                ref_s, ref_f = _unix_to_ntp(rx_unix - 1.0)
                tx_unix = self.current_utc_seconds()
                tx_s, tx_f = _unix_to_ntp(tx_unix)

                reply = bytearray(48)
                reply[0] = (0 << 6) | (vn << 3) | 4  # LI=0, VN=vn, Mode=4 (Server)
                reply[1] = 2  # Stratum 2
                reply[2] = data[2] or 6
                reply[3] = 0xEC  # Precision -20 (~1 us)
                reply[12:16] = b'NEON'
                struct.pack_into('!II', reply, 16, ref_s, ref_f)
                reply[24:32] = tx_origin
                struct.pack_into('!IIII', reply, 32, rx_s, rx_f, tx_s, tx_f)
                self._record_event('ntp_hits', addr[0], 'UDP :123 NTP')
                sock.sendto(bytes(reply), addr)
            except OSError:
                break
            except Exception:
                continue

    def _periodic_sync_loop(self):
        while not self._stop_event.wait(120.0):
            try:
                self.sync_now()
            except Exception as exc:
                with self._lock:
                    self._sync_error = str(exc)

    def get_status(self):
        with self._lock:
            pc_ip = self.advertise_ip or (
                self.dns_bind_ip if self.dns_bind_ip not in ('0.0.0.0', '') else get_local_ip_for_tv(self.tv_ip or '192.168.1.101')
            )
            return {
                'ok': True,
                'running': self._running,
                'mode': self.mode,
                'target': self.target,
                'target_host': self.target_spec['host'],
                'direct_ip': self.direct_target_ip,
                'source_domains': list(self.source_domains),
                'intercept_check': self.intercept_check,
                'upstream_dns': list(self.upstream_dns),
                'dns_bind_ip': self.dns_bind_ip,
                'dns_port': self.dns_port if self._dns_udp_sock is not None else 0,
                'http_bind_ip': self.http_bind_ip,
                'http_port': self.http_port if self._http_server is not None else 0,
                'ntp_port': self.ntp_port if self._ntp_sock is not None else 0,
                'pc_ip': pc_ip,
                'offset_ms': int(round(self._offset_sec * 1000.0)),
                'sync_method': self._sync_method,
                'sync_error': self._sync_error,
                'last_sync_at': self._last_sync_at,
                'stats': dict(self._stats),
                'recent_events': list(self._events),
            }
