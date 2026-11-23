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
    if raw_domains is None:
        items = list(DEFAULT_SOURCE_DOMAINS)
    elif isinstance(raw_domains, (list, tuple, set)):
        tokens = [str(x).strip() for x in raw_domains if str(x).strip()]
        if not tokens:
            items = list(DEFAULT_SOURCE_DOMAINS)
        else:
            items = []
            for tok in tokens:
                dom = tok.strip().rstrip('.').lower()
                if '://' in dom:
                    dom = (urllib.parse.urlparse(dom).hostname or '').rstrip('.').lower()
                if not dom or not is_valid_hostname(dom) or dom == 'local':
                    raise ValueError(f'Invalid Samsung source domain: {tok}')
                if dom not in items:
                    items.append(dom)
    elif not str(raw_domains).strip():
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
        self.auto_stop_after_sync = False

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
        self._synced_clients = []
        self._auto_stop_scheduled = False
        self._auto_stopped_reason = ''

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

    def _schedule_auto_stop(self, client_ip):
        def _do_stop():
            time.sleep(1.5)
            if self._running and self.auto_stop_after_sync:
                with self._lock:
                    self._auto_stopped_reason = f'auto-stopped after sync ({client_ip or "TV"})'
                self._emit(
                    f'[+] One-time sync complete for TV ({client_ip or "TV"})! '
                    'Switch TV DNS back to Auto (or 1.1.1.1 / Router / VPS) before shutting down PC.'
                )
                self.stop()

        t = threading.Thread(target=_do_stop, name='neon-sam-autostop', daemon=True)
        t.start()

    def _record_event(self, kind, client_ip, detail):
        now = time.time()
        first_timesync = False
        trigger_autostop = False
        with self._lock:
            if kind in self._stats:
                self._stats[kind] += 1
            if kind in ('dns_overridden', 'http_timesync', 'http_check', 'ntp_hits'):
                self._stats['last_query_at'] = now
                if client_ip:
                    self._stats['last_client_ip'] = client_ip
                if detail:
                    self._stats['last_domain'] = detail
            if kind == 'http_timesync':
                ip_lbl = client_ip or 'TV'
                if ip_lbl not in self._synced_clients:
                    self._synced_clients.append(ip_lbl)
                    first_timesync = True
                if self.auto_stop_after_sync and not self._auto_stop_scheduled and self._running:
                    self._auto_stop_scheduled = True
                    trigger_autostop = True
            stamp = time.strftime('%H:%M:%S', time.localtime(now))
            self._events.append(f'[{stamp}] {kind}: {client_ip or "?"} -> {detail}')
        if first_timesync:
            self._emit(
                f'[+] Samsung TV ({client_ip or "TV"}) successfully fetched clock via /openapi/timesync!'
            )
        if trigger_autostop:
            self._schedule_auto_stop(client_ip)

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
        auto_stop_after_sync=False,
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
            self.auto_stop_after_sync = bool(auto_stop_after_sync) and (clean_mode == 'gateway')
            self._synced_clients = []
            self._auto_stop_scheduled = False
            self._auto_stopped_reason = ''
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
                'auto_stop_after_sync': self.auto_stop_after_sync,
                'auto_stopped_reason': self._auto_stopped_reason,
                'sync_completed': self._stats.get('http_timesync', 0) > 0,
                'synced_clients': list(self._synced_clients),
                'stats': dict(self._stats),
                'recent_events': list(self._events),
            }


# ---------------------------------------------------------------------------
# Diagnostics, Standalone 24/7 Gateway Script & Router/VPS Deployment
# ---------------------------------------------------------------------------
def probe_samsung_timesync_http(ip_or_host, port=80, host_header='time.samsungcloudsolution.com', timeout=2.5):
    """Test direct HTTP GET /openapi/timesync against an IP/host and validate 16-byte <QQ reply."""
    import http.client
    conn = http.client.HTTPConnection(str(ip_or_host), int(port), timeout=timeout)
    try:
        conn.request('GET', '/openapi/timesync?client=NEON', headers={
            'Host': host_header,
            'Connection': 'close',
            'User-Agent': 'NEON-Diag/1.0',
        })
        resp = conn.getresponse()
        body = resp.read(64)
        if resp.status == 200 and len(body) == 16:
            ms1, _ms2 = struct.unpack('<QQ', body)
            sec = ms1 / 1000.0
            if sec >= _MIN_PLAUSIBLE_EPOCH:
                return {
                    'ok': True,
                    'status': resp.status,
                    'utc_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(sec)),
                    'epoch_ms': ms1,
                }
        return {'ok': False, 'status': resp.status, 'error': f'Unexpected HTTP {resp.status} ({len(body)} bytes)'}
    except Exception as exc:
        return {'ok': False, 'status': 0, 'error': str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def diagnose_samsung_time_reachability(
    resolvers=None,
    domain='time.samsungcloudsolution.com',
    timeout=2.5,
    dns_port=53,
    http_port=80,
):
    """Probe whether Samsung's official time domain works directly via ISP/public DNS."""
    probe_list = list(resolvers) if resolvers is not None else ['system', '1.1.1.1', '8.8.8.8', '9.9.9.9']
    results = []
    working_dns = []
    lines = [f'[*] Testing direct reachability of http://{domain}:{http_port}/openapi/timesync ...']

    for res_id in probe_list:
        entry = {'resolver': res_id, 'resolved_ip': '', 'http_ok': False, 'utc_iso': '', 'error': ''}
        try:
            if res_id == 'system':
                infos = socket.getaddrinfo(domain, http_port, socket.AF_INET, socket.SOCK_STREAM)
                resolved = infos[0][4][0] if infos else ''
                if not resolved:
                    raise OSError('System DNS returned no IPv4 address')
            else:
                resolved = resolve_ipv4_via_upstream(domain, [res_id], timeout=timeout, port=dns_port)
            entry['resolved_ip'] = resolved
            http_res = probe_samsung_timesync_http(resolved, port=http_port, host_header=domain, timeout=timeout)
            entry['http_ok'] = bool(http_res.get('ok'))
            entry['utc_iso'] = http_res.get('utc_iso', '')
            if entry['http_ok']:
                if res_id != 'system':
                    working_dns.append(res_id)
                lines.append(f'[+] DNS {res_id}: OK -> {resolved} (UTC {entry["utc_iso"]})')
            else:
                entry['error'] = http_res.get('error', 'HTTP failed')
                lines.append(f'[!] DNS {res_id}: resolved {resolved}, but HTTP failed ({entry["error"]})')
        except Exception as exc:
            entry['error'] = str(exc)
            lines.append(f'[!] DNS {res_id}: failed ({exc})')
        results.append(entry)

    sys_ok = any(r['resolver'] == 'system' and r['http_ok'] for r in results)
    rec = working_dns[0] if working_dns else ''
    any_ok = sys_ok or bool(working_dns)
    if sys_ok:
        summary = f'Direct Samsung server works via current network DNS! No PC needed.'
    elif rec:
        summary = f'Current network DNS failed, but {rec} works! Set TV/Router DNS = {rec} once (no PC needed).'
    else:
        summary = (
            'time.samsungcloudsolution.com is blocked or unreachable in this network. '
            'For 24/7 time without PC, deploy the standalone gateway to a Router/Linux/VPS.'
        )
    lines.append(f'[i] {summary}')
    return {
        'ok': True,
        'any_direct_working': any_ok,
        'system_dns_working': sys_ok,
        'recommended_dns': rec,
        'best_public_dns': rec,
        'results': results,
        'summary': summary,
        'recommendation': summary,
        'output': '\n'.join(lines),
    }


_STANDALONE_GATEWAY_TEMPLATE = '''#!/usr/bin/env python3
"""Standalone 24/7 Samsung Tizen Time Domain Override Gateway (zero external deps).
Generated by NEON Control. Runs DNS (:53 UDP+TCP), HTTP /openapi/timesync (:80), and NTP (:123).
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate, parsedate_to_datetime
import http.server
import ipaddress
import os
import select
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TARGET = __DEFAULT_TARGET__
DEFAULT_SOURCES = __DEFAULT_SOURCES__
DEFAULT_UPSTREAMS = __DEFAULT_UPSTREAMS__
DEFAULT_ADVERTISE_IP = __DEFAULT_ADVERTISE_IP__
NTP_DELTA = 2208988800
MIN_EPOCH = 1577836800.0

_offset_lock = threading.Lock()
_offset_sec = 0.0
_route_cache = {}
_stop_evt = threading.Event()


def current_utc_sec():
    with _offset_lock:
        return time.time() + _offset_sec


def decode_name(pkt, off):
    labels = []
    jumped = False
    end_off = off
    seen = set()
    pos = off
    while pos < len(pkt):
        ln = pkt[pos]
        if ln == 0:
            if not jumped:
                end_off = pos + 1
            break
        if (ln & 0xC0) == 0xC0:
            ptr = ((ln & 0x3F) << 8) | pkt[pos + 1]
            if ptr in seen or ptr >= len(pkt):
                raise ValueError('bad dns ptr')
            seen.add(ptr)
            if not jumped:
                end_off = pos + 2
            jumped = True
            pos = ptr
            continue
        pos += 1
        labels.append(pkt[pos:pos + ln].decode('ascii', errors='replace'))
        pos += ln
    return '.'.join(labels), end_off


def parse_dns_q(pkt):
    txid, flags, qd = struct.unpack('!HHH', pkt[:6])
    if qd < 1:
        raise ValueError('empty dns')
    qname, qend = decode_name(pkt, 12)
    qtype, qclass = struct.unpack('!HH', pkt[qend:qend + 4])
    return txid, flags, qname.rstrip('.').lower(), qtype, qclass, pkt[12:qend + 4]


def build_dns_resp(txid, flags, qtype, qclass, qwire, ip):
    rd = flags & 0x0100
    if qtype in (1, 255) and qclass == 1:
        hdr = struct.pack('!HHHHHH', txid, 0x8180 | rd, 1, 1, 0, 0)
        ans = b'\\xc0\\x0c' + struct.pack('!HHIH', 1, 1, 30, 4) + socket.inet_aton(ip)
        return hdr + qwire + ans
    return struct.pack('!HHHHHH', txid, 0x8180 | rd, 1, 0, 0, 0) + qwire


def local_ip_for(client_ip, advertise_ip):
    if advertise_ip:
        return advertise_ip
    if client_ip in _route_cache:
        return _route_cache[client_ip]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((client_ip or '8.8.8.8', 80))
        ip = s.getsockname()[0]
        if ip and not ip.startswith('127.'):
            _route_cache[client_ip] = ip
            return ip
    except OSError:
        pass
    finally:
        s.close()
    return '127.0.0.1'


def sync_from_target(target, upstreams):
    global _offset_sec
    if target.lower() in ('local', 'pc', 'localhost'):
        with _offset_lock:
            _offset_sec = 0.0
        return
    try:
        ipaddress.IPv4Address(target)
        rip = target
    except ValueError:
        rip = ''
        for up in upstreams:
            try:
                txid = os.getpid() & 0xFFFF
                q = b''.join(bytes([len(p)]) + p.encode('ascii') for p in target.split('.')) + b'\\x00'
                pkt = struct.pack('!HHHHHH', txid, 0x0100, 1, 0, 0, 0) + q + struct.pack('!HH', 1, 1)
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(2.5)
                s.sendto(pkt, (up, 53))
                d, _ = s.recvfrom(4096)
                s.close()
                _, _, _, _, _, qwire = parse_dns_q(d)
                an = struct.unpack('!H', d[6:8])[0]
                pos = 12 + len(qwire)
                for _ in range(an):
                    _, pos = decode_name(d, pos)
                    rt, _, _, rdl = struct.unpack('!HHIH', d[pos:pos + 10])
                    pos += 10
                    if rt == 1 and rdl == 4:
                        rip = socket.inet_ntoa(d[pos:pos + 4])
                        break
                    pos += rdl
                if rip:
                    break
            except Exception:
                pass
        if not rip:
            rip = socket.gethostbyname(target)
    try:
        pkt = bytearray(48)
        pkt[0] = 0x1B
        t1 = time.time()
        n1 = t1 + NTP_DELTA
        struct.pack_into('!II', pkt, 40, int(n1), int((n1 - int(n1)) * 4294967296.0))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.5)
        s.sendto(bytes(pkt), (rip, 123))
        r, _ = s.recvfrom(1024)
        t4 = time.time()
        s.close()
        rxs, rxf, txs, txf = struct.unpack('!IIII', r[32:48])
        t2 = (rxs - NTP_DELTA) + (rxf / 4294967296.0)
        t3 = (txs - NTP_DELTA) + (txf / 4294967296.0)
        if t3 >= MIN_EPOCH:
            with _offset_lock:
                _offset_sec = ((t2 - t1) + (t3 - t4)) / 2.0
            return
    except Exception:
        pass
    for sch in ('https', 'http'):
        try:
            req = urllib.request.Request(sch + '://' + target + '/', method='HEAD')
            tr = time.time()
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                dh = resp.headers.get('Date')
                if dh:
                    sec = parsedate_to_datetime(dh).timestamp()
                    if sec >= MIN_EPOCH:
                        with _offset_lock:
                            _offset_sec = sec - ((tr + time.time()) / 2.0)
                        return
        except Exception:
            pass


class TimeHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        return


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        return

    def _reply(self, code, body, ctype, send_body=True):
        self.close_connection = True
        self.send_response(code)
        self.send_header('Date', formatdate(current_utc_sec(), usegmt=True))
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        if send_body and body:
            self.wfile.write(body)
        self.wfile.flush()

    def _go(self, send_body=True):
        p = (urllib.parse.urlsplit(self.path or '/').path or '/').lower()
        if p.startswith('/openapi/timesync'):
            ms = int(round(current_utc_sec() * 1000.0))
            self._reply(200, struct.pack('<QQ', ms, ms), 'application/octet-stream', send_body)
        elif p == '/public/network/files/check.xml':
            self._reply(200, b'ok\\n', 'text/plain; charset=utf-8', send_body)
        elif p.startswith('/openapi/zipcode/timezoneoffset'):
            off = -int(time.altzone if (time.daylight and time.localtime().tm_isdst > 0) else time.timezone)
            self._reply(200, ('{"offset":%d,"utcOffsetMinutes":%d}' % (off, off // 60)).encode('utf-8'),
                        'application/json; charset=utf-8', send_body)
        else:
            self._reply(200, b'ok\\n', 'text/plain; charset=utf-8', send_body)

    def do_GET(self):
        self._go(True)

    def do_HEAD(self):
        self._go(False)


def main():
    ap = argparse.ArgumentParser(description='NEON Samsung Tizen Time Override Gateway')
    ap.add_argument('--target', default=DEFAULT_TARGET)
    ap.add_argument('--sources', default=','.join(DEFAULT_SOURCES))
    ap.add_argument('--upstream', default=','.join(DEFAULT_UPSTREAMS))
    ap.add_argument('--advertise-ip', default=DEFAULT_ADVERTISE_IP)
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--dns-port', type=int, default=53)
    ap.add_argument('--http-port', type=int, default=80)
    ap.add_argument('--ntp-port', type=int, default=123)
    args = ap.parse_args()

    sources = set(s.strip().rstrip('.').lower() for s in args.sources.split(',') if s.strip())
    upstreams = [u.strip() for u in args.upstream.split(',') if u.strip()] or ['1.1.1.1', '8.8.8.8']
    sync_from_target(args.target, upstreams)

    fallback_ip = local_ip_for('8.8.8.8', args.advertise_ip)

    def _bind_sock(sock_type, port_num):
        cands = [args.bind]
        if args.bind == '0.0.0.0' and fallback_ip and fallback_ip not in cands:
            cands.append(fallback_ip)
        last_e = None
        for bip in cands:
            s = socket.socket(socket.AF_INET, sock_type)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((bip, port_num))
                return s, bip
            except OSError as exc:
                last_e = exc
                s.close()
        raise last_e

    dns_udp = None
    dns_tcp = None
    if args.dns_port > 0:
        dns_udp, dns_bip = _bind_sock(socket.SOCK_DGRAM, args.dns_port)
        try:
            dns_tcp, _ = _bind_sock(socket.SOCK_STREAM, args.dns_port)
            dns_tcp.listen(16)
        except OSError:
            dns_tcp = None

    http_srv = None
    if args.http_port > 0:
        try:
            http_srv = TimeHTTP((args.bind, args.http_port), Handler)
        except OSError:
            if args.bind == '0.0.0.0' and fallback_ip and fallback_ip != '0.0.0.0':
                http_srv = TimeHTTP((fallback_ip, args.http_port), Handler)
            else:
                raise
    ntp_sock = None
    if args.ntp_port > 0:
        try:
            ntp_sock, _ = _bind_sock(socket.SOCK_DGRAM, args.ntp_port)
        except OSError:
            ntp_sock = None

    pool = ThreadPoolExecutor(max_workers=16)

    def answer_dns(pkt, cip):
        txid, flags, qname, qtype, qclass, qwire = parse_dns_q(pkt)
        if qname in sources:
            return build_dns_resp(txid, flags, qtype, qclass, qwire, local_ip_for(cip, args.advertise_ip))
        for up in upstreams:
            fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            fwd.settimeout(2.5)
            try:
                fwd.sendto(pkt, (up, 53))
                r, _ = fwd.recvfrom(4096)
                return r
            except OSError:
                continue
            finally:
                fwd.close()
        return None

    def _handle_udp(pkt, addr):
        try:
            resp = answer_dns(pkt, addr[0])
            if resp and not _stop_evt.is_set():
                dns_udp.sendto(resp, addr)
        except Exception:
            pass

    def udp_dns_loop():
        while not _stop_evt.is_set():
            r, _, _ = select.select([dns_udp], [], [], 0.4)
            if not r:
                continue
            pkt, addr = dns_udp.recvfrom(4096)
            pool.submit(_handle_udp, pkt, addr)

    def tcp_dns_loop():
        while not _stop_evt.is_set():
            r, _, _ = select.select([dns_tcp], [], [], 0.4)
            if not r:
                continue
            conn, addr = dns_tcp.accept()
            def _cli(c=conn, a=addr):
                try:
                    c.settimeout(3.0)
                    ln = struct.unpack('!H', c.recv(2))[0]
                    buf = bytearray()
                    while len(buf) < ln:
                        ch = c.recv(ln - len(buf))
                        if not ch:
                            return
                        buf.extend(ch)
                    resp = answer_dns(bytes(buf), a[0])
                    if resp:
                        c.sendall(struct.pack('!H', len(resp)) + resp)
                except Exception:
                    pass
                finally:
                    c.close()
            pool.submit(_cli)

    def ntp_loop():
        while not _stop_evt.is_set():
            r, _, _ = select.select([ntp_sock], [], [], 0.4)
            if not r:
                continue
            d, addr = ntp_sock.recvfrom(1024)
            if len(d) < 48:
                continue
            now_u = current_utc_sec()
            n = now_u + NTP_DELTA
            sec, frac = int(n), int((n - int(n)) * 4294967296.0)
            rep = bytearray(48)
            rep[0] = ((((d[0] >> 3) & 7) or 4) << 3) | 4
            rep[1] = 2
            rep[2] = d[2] or 6
            rep[3] = 0xEC
            rep[12:16] = b'NEON'
            struct.pack_into('!II', rep, 16, sec - 1, frac)
            rep[24:32] = d[40:48]
            struct.pack_into('!IIII', rep, 32, sec, frac, sec, frac)
            ntp_sock.sendto(bytes(rep), addr)

    def sync_loop():
        while not _stop_evt.wait(300.0):
            sync_from_target(args.target, upstreams)

    if dns_udp:
        threading.Thread(target=udp_dns_loop, daemon=True).start()
    if dns_tcp:
        threading.Thread(target=tcp_dns_loop, daemon=True).start()
    if http_srv:
        threading.Thread(target=http_srv.serve_forever, daemon=True).start()
    if ntp_sock:
        threading.Thread(target=ntp_loop, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()

    print('NEON Samsung Time Gateway running (target=%s dns=%s http=%s ntp=%s)' % (
        args.target, args.dns_port, args.http_port, args.ntp_port), flush=True)

    def _sig(*_):
        _stop_evt.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    while not _stop_evt.wait(0.5):
        pass
    if http_srv:
        http_srv.shutdown()
        http_srv.server_close()
    for s in (dns_udp, dns_tcp, ntp_sock):
        if s:
            s.close()


if __name__ == '__main__':
    main()
'''


def generate_standalone_gateway_script(
    target='time.cloudflare.com',
    source_domains=None,
    intercept_check=True,
    upstream_dns=None,
    advertise_ip='',
):
    """Return a zero-dependency standalone Python 3.6+ script for 24/7 Router/Raspberry Pi/VPS hosting."""
    import json
    spec = normalize_time_target(target)
    sources = parse_source_domains(source_domains, intercept_check=bool(intercept_check))
    if spec['host'] in sources:
        raise ValueError(f'Target "{spec["host"]}" cannot also be in intercepted domains.')
    upstreams = parse_upstream_dns(upstream_dns)
    adv = str(advertise_ip or '').strip()
    if adv:
        ipaddress.IPv4Address(adv)
    script = _STANDALONE_GATEWAY_TEMPLATE
    script = script.replace('__DEFAULT_TARGET__', json.dumps(spec['raw']))
    script = script.replace('__DEFAULT_SOURCES__', json.dumps(sources))
    script = script.replace('__DEFAULT_UPSTREAMS__', json.dumps(upstreams))
    script = script.replace('__DEFAULT_ADVERTISE_IP__', json.dumps(adv))
    return script


def generate_router_snippets(gateway_ip, source_domains=None, intercept_check=True):
    """Return copy-ready DNS rewrite rules for routers / AdGuard / Pi-hole / Linux systemd."""
    ip_txt = str(gateway_ip or '').strip() or '<GATEWAY_IP>'
    if ip_txt != '<GATEWAY_IP>':
        ipaddress.IPv4Address(ip_txt)
    sources = parse_source_domains(source_domains, intercept_check=bool(intercept_check))
    dnsmasq = '\n'.join(f'address=/{d}/{ip_txt}' for d in sources)
    mikrotik_rules = '\n'.join(
        f'/ip dns static add name="{d}" address={ip_txt} ttl=00:00:30 comment="NEON-SAMSUNG-TIME"'
        for d in sources
    )
    mikrotik_cli = '/ip dns set allow-remote-requests=yes\n' + mikrotik_rules
    keenetic = '\n'.join(f'ip host {d} {ip_txt}' for d in sources) + '\nsystem configuration save'
    hosts_lines = '\n'.join(f'{ip_txt} {d}' for d in sources)
    linux_systemd = (
        '[Unit]\n'
        'Description=NEON Samsung Tizen Time Override Gateway\n'
        'After=network-online.target\n'
        'Wants=network-online.target\n\n'
        '[Service]\n'
        'Type=simple\n'
        'ExecStart=/usr/bin/python3 /opt/neon-samsung-time/gateway.py\n'
        'Restart=always\n'
        'RestartSec=3\n\n'
        '[Install]\n'
        'WantedBy=multi-user.target'
    )
    return {
        'gateway_ip': ip_txt,
        'sources': sources,
        'dnsmasq': dnsmasq,
        'openwrt_dnsmasq': dnsmasq,
        'mikrotik': mikrotik_rules,
        'mikrotik_cli': mikrotik_cli,
        'keenetic': keenetic,
        'keenetic_cli': keenetic,
        'linux_systemd': linux_systemd,
        'hosts': hosts_lines,
    }


def deploy_samsung_time_remote(
    host,
    username,
    password,
    port=22,
    target='time.cloudflare.com',
    source_domains=None,
    intercept_check=True,
    upstream_dns=None,
    advertise_ip='',
    gateway_ip='',
    timeout=12.0,
):
    """One-time SSH deployment from NEON on PC to a 24/7 Linux/VPS/Raspberry Pi/OpenWrt/MikroTik host."""
    import paramiko
    import shlex

    clean_host = str(host or '').strip()
    clean_user = str(username or '').strip()
    clean_pass = str(password or '')
    clean_port = int(port or 22)
    if not clean_host or not clean_user:
        raise ValueError('Enter SSH host IP and username.')
    if not is_valid_hostname(clean_host):
        raise ValueError(f'Invalid remote host/IP: {clean_host}')

    spec = normalize_time_target(target)
    sources = parse_source_domains(source_domains, intercept_check=bool(intercept_check))
    upstreams = parse_upstream_dns(upstream_dns)
    adv_ip = str(advertise_ip or '').strip()
    gw_ip = str(gateway_ip or '').strip()
    for label, ip_val in (('advertise_ip', adv_ip), ('gateway_ip', gw_ip)):
        if ip_val:
            ipaddress.IPv4Address(ip_val)

    script_text = generate_standalone_gateway_script(
        target=spec['raw'],
        source_domains=sources,
        intercept_check=bool(intercept_check),
        upstream_dns=upstreams,
        advertise_ip=adv_ip or (clean_host if is_valid_hostname(clean_host) and _is_ipv4(clean_host) else ''),
    )

    lines = [f'[*] Connecting via SSH to {clean_user}@{clean_host}:{clean_port} ...']
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=clean_host,
            port=clean_port,
            username=clean_user,
            password=clean_pass,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )

        def _exec(cmd, sudo_pw=''):
            if sudo_pw:
                full = f'sudo -S -p "" sh -c {shlex.quote(cmd)}'
            else:
                full = cmd
            stdin, stdout, stderr = client.exec_command(full, timeout=25.0)
            if sudo_pw:
                stdin.write(sudo_pw + '\n')
                stdin.flush()
            rc = stdout.channel.recv_exit_status()
            out = stdout.read().decode('utf-8', errors='replace').strip()
            err = stderr.read().decode('utf-8', errors='replace').strip()
            return rc, out, err

        rc_u, uname_out, _ = _exec('uname -s 2>/dev/null || /system identity print')
        low_u = (uname_out or '').lower()

        # Case 1: MikroTik RouterOS
        if 'name:' in low_u or rc_u != 0 and ('linux' not in low_u):
            rc_mt, mt_out, _ = _exec('/system resource print')
            if rc_mt == 0 or 'mikrotik' in (mt_out or '').lower() or 'routeros' in (mt_out or '').lower():
                target_gw = gw_ip or adv_ip
                if not target_gw:
                    raise ValueError(
                        'MikroTik RouterOS detected! Enter "Gateway HTTP :80 IP" (e.g. a VPS or Raspberry Pi IP) '
                        'for /ip dns static rules.'
                    )
                _exec('/ip dns static remove [find comment="NEON-SAMSUNG-TIME"]')
                for dom in sources:
                    _exec(f'/ip dns static add name="{dom}" address={target_gw} ttl=00:00:30 comment="NEON-SAMSUNG-TIME"')
                lines.append(f'[+] MikroTik DNS static rules added for {len(sources)} domains -> {target_gw}')
                return {
                    'ok': True,
                    'platform': 'mikrotik',
                    'host': clean_host,
                    'dns_ip_for_tv': clean_host,
                    'tv_dns_ip': clean_host,
                    'gateway_ip': target_gw,
                    'output': '\n'.join(lines),
                }

        # Case 2: OpenWrt (dnsmasq + optional python3)
        rc_ow, ow_out, _ = _exec('test -f /etc/openwrt_release && echo OPENWRT || echo LINUX')
        is_openwrt = ('OPENWRT' in (ow_out or ''))
        if is_openwrt:
            router_gw = gw_ip or adv_ip or clean_host
            conf_lines = '\n'.join(f'address=/{d}/{router_gw}' for d in sources) + '\n'
            sftp = client.open_sftp()
            try:
                _exec('mkdir -p /etc/dnsmasq.d')
                with sftp.open('/etc/dnsmasq.d/neon-samsung-time.conf', 'w') as rf:
                    rf.write(conf_lines)
            finally:
                sftp.close()
            _exec('/etc/init.d/dnsmasq restart')
            lines.append(f'[+] OpenWrt dnsmasq configured: {", ".join(sources)} -> {router_gw}')
            rc_py, py_out, _ = _exec('command -v python3 || true')
            if py_out.strip() and (not gw_ip or gw_ip == clean_host):
                sftp = client.open_sftp()
                try:
                    _exec('mkdir -p /opt/neon-samsung-time')
                    with sftp.open('/opt/neon-samsung-time/gateway.py', 'w') as rf:
                        rf.write(script_text)
                finally:
                    sftp.close()
                _exec('chmod 755 /opt/neon-samsung-time/gateway.py')
                # Run HTTP+NTP responder (port 53 stays with dnsmasq!)
                rc_p80, p80_out, _ = _exec('netstat -ln 2>/dev/null | grep ":80 " || ss -ltn 2>/dev/null | grep ":80 " || true')
                if p80_out.strip():
                    lines.append('[!] Port 80 on OpenWrt is already used (likely LuCI uhttpd). Move LuCI off :80 or point gateway IP to another host.')
                else:
                    init_script = (
                        '#!/bin/sh /etc/rc.common\n'
                        'START=95\n'
                        'USE_PROCD=1\n'
                        'start_service() {\n'
                        '  procd_open_instance\n'
                        '  procd_set_param command /usr/bin/python3 /opt/neon-samsung-time/gateway.py --dns-port 0 --ntp-port 0\n'
                        '  procd_set_param respawn\n'
                        '  procd_close_instance\n'
                        '}\n'
                    )
                    sftp = client.open_sftp()
                    try:
                        with sftp.open('/etc/init.d/neon-samsung-time', 'w') as rf:
                            rf.write(init_script)
                    finally:
                        sftp.close()
                    _exec('chmod 755 /etc/init.d/neon-samsung-time && /etc/init.d/neon-samsung-time enable && /etc/init.d/neon-samsung-time restart')
                    lines.append('[+] Installed OpenWrt procd HTTP service (/etc/init.d/neon-samsung-time)')
            return {
                'ok': True,
                'platform': 'openwrt',
                'host': clean_host,
                'dns_ip_for_tv': clean_host,
                'tv_dns_ip': clean_host,
                'gateway_ip': router_gw,
                'output': '\n'.join(lines),
            }

        # Case 3: Standard Linux / VPS / Raspberry Pi / Debian / Ubuntu
        sudo_pw = '' if clean_user == 'root' else clean_pass
        rc_py, py_path, _ = _exec('command -v python3 || true')
        if not py_path.strip():
            raise RuntimeError('python3 is not installed on target Linux host.')

        sftp = client.open_sftp()
        try:
            with sftp.open('/tmp/neon_samsung_time_gateway.py', 'w') as rf:
                rf.write(script_text)
        finally:
            sftp.close()

        rc_cp, _, err_cp = _exec(
            'mkdir -p /opt/neon-samsung-time && mv /tmp/neon_samsung_time_gateway.py /opt/neon-samsung-time/gateway.py && chmod 755 /opt/neon-samsung-time/gateway.py',
            sudo_pw=sudo_pw,
        )
        if rc_cp != 0:
            raise RuntimeError(f'Failed to install /opt/neon-samsung-time/gateway.py: {err_cp}')

        rc_sys, sys_out, _ = _exec('command -v systemctl || true')
        if sys_out.strip():
            unit = (
                '[Unit]\n'
                'Description=NEON Samsung Tizen Time Domain Override Gateway\n'
                'After=network-online.target\n'
                'Wants=network-online.target\n\n'
                '[Service]\n'
                'Type=simple\n'
                f'ExecStart={py_path.strip().splitlines()[0]} /opt/neon-samsung-time/gateway.py\n'
                'Restart=always\n'
                'RestartSec=3\n\n'
                '[Install]\n'
                'WantedBy=multi-user.target\n'
            )
            sftp = client.open_sftp()
            try:
                with sftp.open('/tmp/neon-samsung-time.service', 'w') as rf:
                    rf.write(unit)
            finally:
                sftp.close()
            rc_sv, _, err_sv = _exec(
                'mv /tmp/neon-samsung-time.service /etc/systemd/system/neon-samsung-time.service && '
                'systemctl daemon-reload && systemctl enable neon-samsung-time && systemctl restart neon-samsung-time',
                sudo_pw=sudo_pw,
            )
            if rc_sv != 0:
                raise RuntimeError(f'systemd service start failed: {err_sv}')
            lines.append('[+] Installed & started systemd service neon-samsung-time (auto-starts on reboot).')
        else:
            _exec(
                f'pkill -f "/opt/neon-samsung-time/gateway.py" >/dev/null 2>&1 || true; '
                f'nohup {py_path.strip().splitlines()[0]} /opt/neon-samsung-time/gateway.py >/var/log/neon-samsung-time.log 2>&1 &',
                sudo_pw=sudo_pw,
            )
            lines.append('[+] Started background gateway process (/opt/neon-samsung-time/gateway.py).')

        _exec(
            'if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi "active"; then '
            'ufw allow 53 >/dev/null 2>&1; ufw allow 80/tcp >/dev/null 2>&1; ufw allow 123/udp >/dev/null 2>&1; fi',
            sudo_pw=sudo_pw,
        )

        # Verify live from PC against remote host!
        time.sleep(0.8)
        http_check = probe_samsung_timesync_http(clean_host, port=80, timeout=3.0)
        dns_ok = False
        try:
            resolved_chk = resolve_ipv4_via_upstream(sources[0], [clean_host], timeout=3.0, port=53)
            dns_ok = bool(resolved_chk)
            lines.append(f'[+] Live DNS verify @{clean_host}: {sources[0]} -> {resolved_chk}')
        except Exception as dexc:
            lines.append(f'[!] Live DNS verify note (check firewall/systemd-resolved if port 53 busy): {dexc}')
        if http_check.get('ok'):
            lines.append(f'[+] Live HTTP verify http://{clean_host}/openapi/timesync -> OK (UTC {http_check.get("utc_iso")})')
        else:
            lines.append(f'[!] Live HTTP verify note: {http_check.get("error")}')

        lines.append(f'[+] Done! Set Samsung TV (or Router) DNS once to: {clean_host} — PC can now stay OFF.')
        return {
            'ok': True,
            'platform': 'linux-systemd' if sys_out.strip() else 'linux-nohup',
            'host': clean_host,
            'dns_ip_for_tv': clean_host,
            'tv_dns_ip': clean_host,
            'dns_verified': dns_ok,
            'http_verified': bool(http_check.get('ok')),
            'output': '\n'.join(lines),
        }
    finally:
        try:
            client.close()
        except Exception:
            pass


def _is_ipv4(text):
    try:
        ipaddress.IPv4Address(str(text).strip())
        return True
    except ipaddress.AddressValueError:
        return False
