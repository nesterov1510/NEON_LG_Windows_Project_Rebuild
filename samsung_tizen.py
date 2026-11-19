"""Pure-Python Samsung Tizen SDB (port 26101), W3C Widget XML-DSig signer & cert manager."""
import base64
import datetime
import hashlib
import http.client
import io
import json
import os
import platform
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import xml.etree.ElementTree as ET
import zipfile

try:
    import certifi
except ImportError:
    certifi = None

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID

BELET_PKG_ID = 'BeletTV001'
BELET_APP_ID = 'BeletTV001.Belet'
BELET_URL = 'https://belet.tm/'

ALEMTV_PKG_ID = 'AlemTV0001'
ALEMTV_APP_ID = 'AlemTV0001.AlemTV'
ALEMTV_URL = 'http://telecom.alemtv.tm/'

# Samsung VD & Tizen official endpoints
TIZEN_EXTENSION_INFO_URL = 'https://download.tizen.org/sdk/tizenstudio/official/extension_info.xml'
TIZEN_CERT_EXT_FALLBACK_URL = 'https://download.tizen.org/sdk/extensions/tizen-certificate-extension_2.0.75.zip'
TIZEN_SDK_CERT_GEN_URL = (
    'https://download.tizen.org/sdk/tizenstudio/official/binary/'
    'certificate-generator_0.1.4_ubuntu-64.zip'
)


def _resource_path(*parts):
    root = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)

SAMSUNG_SIGNINGATE_URL = 'https://account.samsung.com/accounts/be1dce529476c1a6d407c4c7578c31bd/signInGate'
SAMSUNG_CLIENT_ID = 'v285zxnl3h'
SAMSUNG_STATE = 'accountcheckdogeneratedstatetext'
SAMSUNG_CALLBACK_PORT = 4794
SAMSUNG_CALLBACK_PATH = '/signin/callback'

SAMSUNG_AUTHOR_V3_URL = 'https://svdca.samsungqbe.com/apis/v3/authors'
SAMSUNG_DIST_V1_URL = 'https://svdca.samsungqbe.com/apis/v1/distributors'
SAMSUNG_DIST_V3_URL = 'https://svdca.samsungqbe.com/apis/v3/distributors'

TIZEN_LEGACY_DIST_PASS = 'tizenpkcs12passfordsigner'
TIZEN_DEV_CA_KEY_PASS = b'tizencertificatefordevelopercaroqkfwk'

# Exact W3C Widget DigSig SHA-512 base64 digests of inclusive-C14N <Object Id="prop">
AUTHOR_PROP_DIGEST = 'aXbSAVgmAz0GsBUeZ1UmNDRrxkWhDUVGb45dZcNRq429wX3X+x6kaXT3NdNDTSNVTU+ypkysPMGvQY10fG1EWQ=='
DISTRIBUTOR_PROP_DIGEST = '/r5npk2VVA46QFJnejgONBEh4BWtjrtu9x/IFeLksjWyGmB/cMWKSJWQl7aU3YRQRZ3AesG8gF7qGyvKX9Snig=='

# SDB v1 wire constants (TCP 26101)
_SDB_PORT = 26101
_A_SYNC = 0x434E5953
_A_CNXN = 0x4E584E43
_A_OPEN = 0x4E45504F
_A_OKAY = 0x59414B4F
_A_CLSE = 0x45534C43
_A_WRTE = 0x45545257
_SDB_VERSION = 0x01000000
_SDB_MAXDATA = 256 * 1024


class SdbError(Exception):
    """Base error raised by SDB wire transport or Tizen device commands."""


class SdbTransientError(SdbError):
    """Connection or channel closed before completing; safe to reconnect and retry."""


def get_local_ip_for_tv(tv_ip='192.168.1.101'):
    """Best-effort detection of this PC's LAN IPv4 address reachable from the TV."""
    target = str(tv_ip or '').strip() or '192.168.1.101'
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    detected = None
    try:
        sock.connect((target, _SDB_PORT))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith('127.'):
            detected = ip
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if detected and not detected.startswith('169.254.'):
        return detected
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if ip and not ip.startswith('127.') and not ip.startswith('169.254.'):
                return ip
    except OSError:
        pass
    return detected or '192.168.1.x'


# ── SDB (TCP 26101) Wire Client ──────────────────────────────────────────────

class SdbClient:
    """Minimal pure-Python SDB client (Android ADB-derived protocol on TCP 26101)."""

    def __init__(self, host, port=_SDB_PORT, timeout=8.0):
        self.host = str(host).strip()
        self.port = int(port)
        self.timeout = float(timeout)
        self._sock = None
        self.maxdata = _SDB_MAXDATA
        self.banner = ''
        self._next_local_id = 1

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _recv_exact(self, n):
        chunks = []
        remaining = n
        while remaining > 0:
            try:
                part = self._sock.recv(remaining)
            except socket.timeout as exc:
                raise SdbError(f'SDB read timeout from {self.host}:{self.port}') from exc
            except OSError as exc:
                raise SdbTransientError(f'SDB socket error: {exc}') from exc
            if not part:
                raise SdbTransientError('TV closed SDB socket')
            chunks.append(part)
            remaining -= len(part)
        return b''.join(chunks)

    def _send_packet(self, cmd, arg0, arg1, data=b''):
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        checksum = sum(data) & 0xFFFFFFFF
        magic = cmd ^ 0xFFFFFFFF
        hdr = struct.pack('<IIIIII', cmd, arg0, arg1, len(data), checksum, magic)
        try:
            self._sock.sendall(hdr + data)
        except OSError as exc:
            raise SdbTransientError(f'SDB write failed: {exc}') from exc

    def _read_packet(self, timeout=None):
        old = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            hdr = self._recv_exact(24)
            cmd, arg0, arg1, length, checksum, magic = struct.unpack('<IIIIII', hdr)
            if (cmd ^ 0xFFFFFFFF) != magic:
                raise SdbError(f'Corrupt SDB magic header: 0x{cmd:08x}')
            if length > 4 * 1024 * 1024:
                raise SdbError(f'Oversized SDB payload: {length} bytes')
            data = self._recv_exact(length) if length else b''
            if length and (sum(data) & 0xFFFFFFFF) != checksum:
                raise SdbError('SDB checksum mismatch')
            return cmd, arg0, arg1, data
        finally:
            if timeout is not None and self._sock is not None:
                try:
                    self._sock.settimeout(old)
                except OSError:
                    pass

    def connect(self, retries=3):
        last_err = None
        for attempt in range(max(1, retries)):
            self.close()
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                sock.settimeout(self.timeout)
                self._sock = sock
                self._send_packet(_A_CNXN, _SDB_VERSION, _SDB_MAXDATA, b'host::\x00')
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    cmd, _arg0, arg1, data = self._read_packet(timeout=self.timeout)
                    if cmd == _A_CNXN:
                        if arg1 > 0:
                            self.maxdata = min(_SDB_MAXDATA, arg1)
                        self.banner = data.rstrip(b'\x00').decode('utf-8', errors='replace')
                        return self
                raise SdbError(f'No CNXN response from {self.host}:{self.port}')
            except SdbTransientError as exc:
                last_err = exc
                self.close()
                if attempt + 1 < retries:
                    time.sleep(0.35 * (attempt + 1))
            except OSError as exc:
                self.close()
                raise SdbError(
                    f'Cannot reach SDB port {self.port} on {self.host}: {exc}. '
                    'Enable Developer Mode on TV (Apps -> 12345), set Host PC IP, and reboot TV.'
                ) from exc
        raise last_err or SdbTransientError('SDB handshake failed')

    def _open_stream(self, service_name, timeout=None):
        if self._sock is None:
            self.connect()
        local_id = self._next_local_id
        self._next_local_id += 1
        payload = service_name.encode('utf-8')
        if not payload.endswith(b'\x00'):
            payload += b'\x00'
        self._send_packet(_A_OPEN, local_id, 0, payload)
        t = timeout if timeout is not None else self.timeout
        deadline = time.monotonic() + t
        while time.monotonic() < deadline:
            cmd, arg0, arg1, _data = self._read_packet(timeout=t)
            if arg1 != local_id and cmd != _A_CNXN:
                continue
            if cmd == _A_OKAY:
                return local_id, arg0
            if cmd == _A_CLSE:
                raise SdbTransientError(f'TV closed channel before OKAY for {service_name!r}')
        raise SdbError(f'Timed out opening SDB stream {service_name!r}')

    def _close_stream(self, local_id, remote_id):
        try:
            self._send_packet(_A_CLSE, local_id, remote_id)
        except Exception:
            pass

    def _send_wrte(self, local_id, remote_id, data):
        chunk_limit = max(1024, min(65536, self.maxdata))
        offset = 0
        while offset < len(data):
            piece = data[offset:offset + chunk_limit]
            self._send_packet(_A_WRTE, local_id, remote_id, piece)
            while True:
                cmd, _arg0, arg1, _body = self._read_packet(timeout=max(self.timeout, 15.0))
                if arg1 != local_id:
                    continue
                if cmd == _A_OKAY:
                    break
                if cmd == _A_CLSE:
                    raise SdbTransientError('TV closed SDB stream during write')
            offset += len(piece)

    def run_command(self, service, timeout=30.0, done_pattern=None):
        """Open an SDB service channel and collect all output until CLSE (or done_pattern)."""
        local_id, remote_id = self._open_stream(service, timeout=min(timeout, 15.0))
        chunks = []
        deadline = time.monotonic() + timeout
        rx_done = re.compile(done_pattern, re.IGNORECASE) if done_pattern else None
        try:
            while time.monotonic() < deadline:
                rem = max(0.25, deadline - time.monotonic())
                try:
                    cmd, arg0, arg1, data = self._read_packet(timeout=rem)
                except SdbError as exc:
                    if chunks and 'timeout' in str(exc).lower():
                        break
                    raise
                if arg1 != local_id:
                    continue
                if cmd == _A_WRTE:
                    chunks.append(data)
                    self._send_packet(_A_OKAY, local_id, remote_id or arg0)
                    if rx_done is not None:
                        text = b''.join(chunks).decode('utf-8', errors='replace')
                        if rx_done.search(text):
                            # Allow trailing line/CLSE briefly without hanging for full timeout
                            deadline = min(deadline, time.monotonic() + 0.35)
                elif cmd == _A_CLSE:
                    self._close_stream(local_id, remote_id or arg0)
                    break
            return b''.join(chunks).decode('utf-8', errors='replace')
        except Exception:
            self._close_stream(local_id, remote_id)
            raise

    def run_shell(self, cmd, timeout=30.0, done_pattern=None):
        """Run a Samsung Tizen SDB shell verb (`shell:<cmd>`)."""
        clean = str(cmd or '').strip()
        service = clean if clean.startswith('shell:') else f'shell:{clean}'
        return self.run_command(service, timeout=timeout, done_pattern=done_pattern)

    def capability(self):
        for svc in ('capability:', 'shell:0 capability'):
            try:
                raw = self.run_command(svc, timeout=8.0)
                caps = {}
                for line in raw.splitlines():
                    line = line.strip('\r\n\x00 ')
                    if ':' in line:
                        k, v = line.split(':', 1)
                        caps[k.strip()] = v.strip()
                if caps:
                    return caps
            except SdbTransientError:
                try:
                    self.connect()
                except Exception:
                    pass
            except SdbError:
                pass
        return {}

    @staticmethod
    def resolve_sdk_toolpath(caps):
        path = (caps or {}).get('sdk_toolpath', '').strip()
        if path:
            return path.rstrip('/')
        ver = (caps or {}).get('platform_version', '').strip()
        if ver.startswith('2.'):
            return '/opt/usr/apps/tmp'
        return '/home/owner/share/tmp/sdk_tools'

    def get_duid(self, caps=None, retries=3):
        if caps and caps.get('duid'):
            cand = caps['duid'].strip('\r\n\x00 ')
            if len(cand) >= 8:
                return cand
        last_err = None
        for attempt in range(max(1, retries)):
            try:
                out = self.run_shell('0 getduid', timeout=10.0)
                duid = out.strip('\r\n\x00 ')
                if duid and len(duid) >= 8 and 'fail' not in duid.lower() and 'closed' not in duid.lower():
                    return duid
                last_err = SdbTransientError(f'Empty/invalid DUID response: {out!r}')
            except SdbTransientError as exc:
                last_err = exc
            if attempt + 1 < retries:
                time.sleep(0.65 * (attempt + 1))
                self.connect()
        raise SdbError(f'Could not read Samsung TV DUID via SDB: {last_err}')

    def push_bytes(self, payload, remote_path):
        """Upload raw bytes to TV via SDB sync: service (SEND/DATA/DONE/STAT/QUIT)."""
        local_id, remote_id = self._open_stream('sync:', timeout=15.0)
        rx_buf = bytearray()

        def read_sync_bytes(n):
            while len(rx_buf) < n:
                cmd, _arg0, arg1, data = self._read_packet(timeout=30.0)
                if arg1 != local_id:
                    continue
                if cmd == _A_WRTE:
                    rx_buf.extend(data)
                    self._send_packet(_A_OKAY, local_id, remote_id)
                elif cmd == _A_CLSE:
                    raise SdbTransientError('TV closed sync channel unexpectedly')
            out = bytes(rx_buf[:n])
            del rx_buf[:n]
            return out

        try:
            spec = f'{remote_path},{0o100644}'.encode('utf-8')
            self._send_wrte(local_id, remote_id, b'SEND' + struct.pack('<I', len(spec)) + spec)

            chunk_size = max(1024, min(65536, self.maxdata - 16))
            offset = 0
            while offset < len(payload):
                piece = payload[offset:offset + chunk_size]
                self._send_wrte(local_id, remote_id, b'DATA' + struct.pack('<I', len(piece)) + piece)
                offset += len(piece)

            self._send_wrte(local_id, remote_id, b'DONE' + struct.pack('<I', int(time.time())))

            status = read_sync_bytes(8)
            tag, val = status[:4], struct.unpack('<I', status[4:8])[0]
            if tag == b'FAIL':
                err_msg = read_sync_bytes(val).decode('utf-8', errors='replace') if val else 'unknown'
                raise SdbError(f'SDB sync upload failed ({remote_path}): {err_msg}')
            if tag != b'OKAY':
                raise SdbError(f'Unexpected SDB sync response: {tag!r}')

            # Best-effort STAT check on same sync stream before QUIT
            try:
                rbytes = remote_path.encode('utf-8')
                self._send_wrte(local_id, remote_id, b'STAT' + struct.pack('<I', len(rbytes)) + rbytes)
                st = read_sync_bytes(16)
                if st[:4] == b'STAT':
                    _mode, size, _mtime = struct.unpack('<III', st[4:16])
                    if size not in (0, len(payload)):
                        raise SdbError(f'Uploaded size mismatch: got {size}, expected {len(payload)}')
            except SdbError:
                raise
            except Exception:
                pass

            try:
                self._send_wrte(local_id, remote_id, b'QUIT\x00\x00\x00\x00')
            except Exception:
                pass
        finally:
            self._close_stream(local_id, remote_id)

    def install_wgt(self, pkg_id, remote_wgt_path, timeout=120.0):
        return self.run_shell(
            f'0 vd_appinstall {pkg_id} {remote_wgt_path}',
            timeout=timeout,
            done_pattern=r'(?:spend time\(|install completed|install failed|download failed|check certificate error|invalid certificate chain)',
        )

    def list_apps(self, retries=3):
        last_err = None
        for attempt in range(max(1, retries)):
            try:
                return self.run_shell('0 vd_applist', timeout=15.0)
            except SdbTransientError as exc:
                last_err = exc
                if attempt + 1 < retries:
                    time.sleep(0.4 * (attempt + 1))
                    self.connect()
        raise last_err or SdbTransientError('vd_applist failed')

    def launch_app(self, app_id):
        reply = self.run_shell(
            f'0 was_execute {app_id}',
            timeout=15.0,
            done_pattern=r'(?:launched|spend time|fail|error|refused)',
        ).strip()
        low = reply.lower()
        if ('400' in low or 'fail' in low) and 'not a smarthub app' not in low:
            try:
                alt = self.run_shell(f'0 execute {app_id}', timeout=15.0).strip()
                if alt and 'fail' not in alt.lower() and 'error' not in alt.lower():
                    return alt
            except Exception:
                pass
        if not reply:
            raise SdbError(f'TV returned empty response when launching {app_id}')
        if 'not a smarthub app' in low or 'fail' in low or 'error' in low or 'refused' in low:
            raise SdbError(f'TV refused to launch {app_id}: {reply}')
        return reply

    def uninstall_app(self, pkg_id, retries=3):
        last_err = None
        for attempt in range(max(1, retries)):
            try:
                reply = self.run_shell(
                    f'0 vd_appuninstall {pkg_id}',
                    timeout=30.0,
                    done_pattern=r'(?:uninstall completed|uninstall failed|spend time|fail|error)',
                )
                low = reply.lower()
                if 'fail' in low or 'error' in low:
                    # Fallback to pkgcmd on sets that refuse vd_appuninstall
                    pkg_reply = self.run_shell(f'pkgcmd -u -n {pkg_id} -q', timeout=30.0)
                    return reply + ('\n' + pkg_reply if pkg_reply else '')
                return reply
            except SdbTransientError as exc:
                last_err = exc
                if attempt + 1 < retries:
                    time.sleep(0.4 * (attempt + 1))
                    self.connect()
            except SdbError:
                return self.run_shell(f'pkgcmd -u -n {pkg_id} -q', timeout=30.0)
        raise last_err or SdbError(f'Uninstall failed for {pkg_id}')


def diagnose_install_output(output, pkg_id=BELET_PKG_ID):
    """Classify Samsung vd_appinstall output into structured recovery guidance."""
    text = str(output or '')
    low = text.lower()

    if 'install completed' in low or 'installing[100]' in low:
        if 'install failed' not in low and 'failed[' not in low:
            return {'status': 'ok', 'reason': ''}

    if 'transport' in low and 'lost' in low or 'connection reset' in low:
        return {'status': 'transport_lost', 'reason': 'SDB connection reset by TV'}

    if 'failed[116]' in low or 'download failed[116]' in low:
        return {'status': 'no_space', 'reason': 'Not enough storage space on Samsung TV (error 116)'}

    cert_fail = any(tok in low for tok in ('118012', '118, -12', '118,-12', '118011', '118, -11'))
    if cert_fail:
        if 'check certificate error' in low:
            if 'same id' in low and 'different certificate' in low:
                return {'status': 'cert_mismatch', 'reason': 'Installed app uses a different signing certificate'}
            if 'invalid certificate chain' in low:
                return {'status': 'cert_invalid_chain', 'reason': 'TV rejected signing certificate chain (118/-12)'}
            if 'not yet valid' in low:
                return {'status': 'cert_not_yet_valid', 'reason': 'Certificate not yet valid (check TV clock/date)'}
            if 'expired' in low:
                return {'status': 'cert_expired', 'reason': 'Signing certificate expired'}
            if 'invalid format' in low:
                return {'status': 'cert_invalid_format', 'reason': 'Invalid certificate format in signature'}
            return {'status': 'cert_rejected', 'reason': 'TV certificate check error'}
        return {'status': 'cert_mismatch', 'reason': f'Same package ID ({pkg_id}) with different certificate (118012)'}

    if '118, -22' in low or '118,-22' in low or '118022' in low:
        return {'status': 'pkg_blocked', 'reason': f'Package ID {pkg_id} blocked by TV security manager (118,-22)'}

    if '118, -19' in low or '118,-19' in low or '118019' in low:
        return {'status': 'manifest_error', 'reason': 'TV rejected config.xml / API version (118019)'}

    if '118, -4' in low or '118,-4' in low:
        return {'status': 'api_mismatch', 'reason': 'TV refused operation / privilege (118,-4)'}

    if 'failed' in low or 'error' in low:
        return {'status': 'failed', 'reason': text.strip() or 'Installation failed'}

    return {'status': 'unknown', 'reason': text.strip()}


# ── Belet .wgt Builder & Pure-Python W3C XML-DSig Signer ─────────────────────

_BELET_PROXY_PORT = 18765


def _build_belet_nav_js(target_url=BELET_URL):
    """Build strict-ES5 fullscreen D-pad spatial-navigation + remote-key bridge injected into Belet."""
    safe_target = json.dumps(str(target_url or BELET_URL))
    return (
        '(function(){\n'
        'if(window.__NEON_NAV_LOADED__)return;\n'
        'window.__NEON_NAV_LOADED__=true;\n'
        'var TARGET_URL=window.__NEON_TARGET_URL__||' + safe_target + ';\n'
        'var targetMatch=String(TARGET_URL).match(/^(https?:)\\/\\/([^\\/:?#]+)(?::(\\d+))?/i);\n'
        'var TARGET_HOST=targetMatch?targetMatch[2].toLowerCase():"belet.tm";\n'
        'var IS_PROXY=(location.protocol==="http:"||location.protocol==="https:")&&location.hostname!==TARGET_HOST;\n'
        'var IS_FILE=(location.protocol==="file:"||location.origin==="null"||!!window.__NEON_FETCH_MODE__);\n'
        'function isTargetHost(h){\n'
        '  var low=String(h||"").toLowerCase();\n'
        '  return low===TARGET_HOST||(low.length>TARGET_HOST.length+1&&low.slice(-TARGET_HOST.length-1)==="."+TARGET_HOST);\n'
        '}\n'
        'function rewriteForProxy(raw){\n'
        '  if(!IS_PROXY||typeof raw!=="string")return raw;\n'
        '  var s=raw.replace(/^\\s+/,"");\n'
        '  if(s.indexOf("/__neon_")===0)return s;\n'
        '  var m=s.match(/^https?:\\/\\/([^\\/:?#]+)(?::\\d+)?(\\/[^#\\s]*|\\?[^#\\s]*|#[^\\s]*)?$/i);\n'
        '  if(!m)return raw;\n'
        '  var h=m[1].toLowerCase();\n'
        '  var rest=m[2]||"/";\n'
        '  if(rest.charAt(0)==="?"||rest.charAt(0)==="#")rest="/"+rest;\n'
        '  if(h===TARGET_HOST)return location.origin+rest;\n'
        '  if(isTargetHost(h))return location.origin+"/__neon_host/"+h+rest;\n'
        '  return raw;\n'
        '}\n'
        'function resolveForTarget(raw){\n'
        '  var s=String(raw||"");\n'
        '  if(!s)return TARGET_URL;\n'
        '  if(/^https?:\\/\\//i.test(s))return s;\n'
        '  try{return new URL(s,TARGET_URL).href;}catch(e){}\n'
        '  var baseOrigin=TARGET_URL.replace(/^(https?:\\/\\/[^\\/?#]+).*$/i,"$1");\n'
        '  if(s.charAt(0)==="/")return baseOrigin+s;\n'
        '  return baseOrigin+"/"+s;\n'
        '}\n'
        'function rewriteWsForProxy(raw){\n'
        '  if(!IS_PROXY||typeof raw!=="string")return raw;\n'
        '  var s=raw.replace(/^\\s+/,"");\n'
        '  var m=s.match(/^wss?:\\/\\/([^\\/:?#]+)(?::\\d+)?(\\/[^#\\s]*|\\?[^#\\s]*)?$/i);\n'
        '  if(!m)return raw;\n'
        '  var h=m[1].toLowerCase();\n'
        '  if(!isTargetHost(h))return raw;\n'
        '  var wsProto=(location.protocol==="https:")?"wss:":"ws:";\n'
        '  var rest=m[2]||"/";\n'
        '  if(rest.charAt(0)==="?")rest="/"+rest;\n'
        '  if(h===TARGET_HOST)return wsProto+"//"+location.host+rest;\n'
        '  return wsProto+"//"+location.host+"/__neon_host/"+h+rest;\n'
        '}\n'
        'var pendingXhr={};\n'
        'var xhrSeq=0;\n'
        'if(IS_PROXY){\n'
        '  (function(){\n'
        '    try{\n'
        '      var proto=(typeof HTMLDocument!=="undefined"&&HTMLDocument.prototype)||Document.prototype;\n'
        '      var desc=Object.getOwnPropertyDescriptor(proto,"cookie")||Object.getOwnPropertyDescriptor(Document.prototype,"cookie");\n'
        '      if(desc&&typeof desc.get==="function"&&typeof desc.set==="function"&&desc.configurable!==false){\n'
        '        var jarClientCookies={};\n'
        '        var syncProxyCookie=function(rawLine){\n'
        '          try{\n'
        '            var payload=JSON.stringify({cookie:String(rawLine||"")});\n'
        '            if(typeof navigator!=="undefined"&&typeof navigator.sendBeacon==="function"){\n'
        '              if(navigator.sendBeacon("/__neon_cookie",payload))return;\n'
        '            }\n'
        '            if(typeof XMLHttpRequest==="function"){\n'
        '              var x=new XMLHttpRequest();\n'
        '              x.open("POST","/__neon_cookie",true);\n'
        '              x.setRequestHeader("Content-Type","application/json");\n'
        '              x.send(payload);\n'
        '            }\n'
        '          }catch(e){}\n'
        '        };\n'
        '        Object.defineProperty(document,"cookie",{\n'
        '          configurable:true,\n'
        '          get:function(){\n'
        '            var nat="";\n'
        '            try{nat=String(desc.get.call(document)||"");}catch(e){nat="";}\n'
        '            var parts=nat?nat.split(/;\\s*/):[];\n'
        '            var have={};\n'
        '            for(var i=0;i<parts.length;i++){\n'
        '              var eq=parts[i].indexOf("=");\n'
        '              if(eq>0)have[parts[i].slice(0,eq)]=1;\n'
        '            }\n'
        '            for(var k in jarClientCookies){\n'
        '              if(Object.prototype.hasOwnProperty.call(jarClientCookies,k)&&!have[k]){\n'
        '                parts.push(k+"="+jarClientCookies[k]);\n'
        '              }\n'
        '            }\n'
        '            return parts.join("; ");\n'
        '          },\n'
        '          set:function(v){\n'
        '            var raw=String(v||"");\n'
        '            var first=raw.split(";")[0]||"";\n'
        '            var eq=first.indexOf("=");\n'
        '            var cName=eq>0?first.slice(0,eq).replace(/^\\s+|\\s+$/g,""):"";\n'
        '            var cVal=eq>0?first.slice(eq+1).replace(/^\\s+|\\s+$/g,""):"";\n'
        '            var isDel=/;\\s*max-age\\s*=\\s*-?0+(?:\\s*;|$)/i.test(raw)||/;\\s*max-age\\s*=\\s*-\\d+/i.test(raw)||/;\\s*expires\\s*=\\s*[^;]*(?:1970|1969)/i.test(raw);\n'
        '            var clean=raw\n'
        '              .replace(/;\\s*domain\\s*=\\s*[^;]*/gi,"")\n'
        '              .replace(/;\\s*secure\\b/gi,"")\n'
        '              .replace(/SameSite\\s*=\\s*None/gi,"SameSite=Lax");\n'
        '            if(cName&&(/^__(?:Host|Secure)-/i.test(cName)||isDel||/;\\s*domain\\s*=\\s*[^;]*/i.test(raw))){\n'
        '              if(/^__(?:Host|Secure)-/i.test(cName)){\n'
        '                if(isDel)delete jarClientCookies[cName];\n'
        '                else jarClientCookies[cName]=cVal;\n'
        '              }\n'
        '              syncProxyCookie(raw);\n'
        '            }\n'
        '            try{desc.set.call(document,clean);}catch(e){}\n'
        '          }\n'
        '        });\n'
        '      }\n'
        '    }catch(e){}\n'
        '  })();\n'
        '  if(typeof window.fetch==="function"){\n'
        '    var origFetch=window.fetch;\n'
        '    window.fetch=function(input,init){\n'
        '      try{\n'
        '        if(typeof input==="string"){\n'
        '          var rw=rewriteForProxy(input);\n'
        '          return origFetch.call(window,rw,init);\n'
        '        }else if(input&&typeof input.url==="string"){\n'
        '          var rwReqUrl=rewriteForProxy(input.url);\n'
        '          if(rwReqUrl!==input.url&&typeof Request==="function"){\n'
        '            try{\n'
        '              return origFetch.call(window,new Request(rwReqUrl,input),init);\n'
        '            }catch(e2){\n'
        '              var reqOpts={method:input.method||"GET",headers:input.headers,credentials:input.credentials,cache:input.cache,redirect:input.redirect};\n'
        '              return origFetch.call(window,rwReqUrl,init||reqOpts);\n'
        '            }\n'
        '          }\n'
        '        }\n'
        '      }catch(e){}\n'
        '      return origFetch.call(window,input,init);\n'
        '    };\n'
        '  }\n'
        '  if(window.XMLHttpRequest&&XMLHttpRequest.prototype&&XMLHttpRequest.prototype.open){\n'
        '    var origXhrOpen=XMLHttpRequest.prototype.open;\n'
        '    XMLHttpRequest.prototype.open=function(method,url){\n'
        '      var args=[];\n'
        '      for(var i=0;i<arguments.length;i++)args.push(arguments[i]);\n'
        '      if(typeof url==="string")args[1]=rewriteForProxy(url);\n'
        '      return origXhrOpen.apply(this,args);\n'
        '    };\n'
        '  }\n'
        '  if(typeof window.EventSource==="function"){\n'
        '    try{\n'
        '      var OrigES=window.EventSource;\n'
        '      window.EventSource=function(u,cfg){\n'
        '        var rwu=rewriteForProxy(String(u||""));\n'
        '        return cfg!==undefined?new OrigES(rwu,cfg):new OrigES(rwu);\n'
        '      };\n'
        '      window.EventSource.prototype=OrigES.prototype;\n'
        '    }catch(e){}\n'
        '  }\n'
        '  if(typeof window.WebSocket==="function"){\n'
        '    try{\n'
        '      var OrigWS=window.WebSocket;\n'
        '      window.WebSocket=function(u,protos){\n'
        '        var rwu=rewriteWsForProxy(String(u||""));\n'
        '        return protos!==undefined?new OrigWS(rwu,protos):new OrigWS(rwu);\n'
        '      };\n'
        '      window.WebSocket.prototype=OrigWS.prototype;\n'
        '      window.WebSocket.CONNECTING=OrigWS.CONNECTING;\n'
        '      window.WebSocket.OPEN=OrigWS.OPEN;\n'
        '      window.WebSocket.CLOSING=OrigWS.CLOSING;\n'
        '      window.WebSocket.CLOSED=OrigWS.CLOSED;\n'
        '    }catch(e){}\n'
        '  }\n'
        '  try{\n'
        '    if(window.navigation&&typeof window.navigation.addEventListener==="function"){\n'
        '      window.navigation.addEventListener("navigate",function(ev){\n'
        '        try{\n'
        '          var dest=ev&&ev.destination&&ev.destination.url?String(ev.destination.url):"";\n'
        '          if(!dest||dest.indexOf(location.origin)===0)return;\n'
        '          var hm=dest.match(/^https?:\\/\\/([^\\/:?#]+)/i);\n'
        '          if(hm&&isTargetHost(hm[1])){\n'
        '            var rw=rewriteForProxy(dest);\n'
        '            if(rw!==dest&&ev.cancelable!==false&&typeof ev.preventDefault==="function"){\n'
        '              ev.preventDefault();\n'
        '              if(ev.navigationType==="replace")location.replace(rw);else location.assign(rw);\n'
        '            }\n'
        '          }\n'
        '        }catch(e){}\n'
        '      });\n'
        '    }\n'
        '  }catch(e){}\n'
        '  try{\n'
        '    if(typeof Location==="function"&&Location.prototype){\n'
        '      var wrapLoc=function(fnName){\n'
        '        var orig=Location.prototype[fnName];\n'
        '        if(typeof orig!=="function")return;\n'
        '        try{\n'
        '          Location.prototype[fnName]=function(u){\n'
        '            var dest=typeof u==="string"?rewriteForProxy(u):u;\n'
        '            return orig.call(this,dest);\n'
        '          };\n'
        '        }catch(e){}\n'
        '      };\n'
        '      wrapLoc("assign");\n'
        '      wrapLoc("replace");\n'
        '    }\n'
        '  }catch(e){}\n'
        '  try{\n'
        '    var origWinOpen=window.open;\n'
        '    window.open=function(u){\n'
        '      if(typeof u==="string"&&u&&u!=="about:blank"){\n'
        '        var rw=rewriteForProxy(u);\n'
        '        if(rw.indexOf(location.origin)===0||u.charAt(0)==="/"){\n'
        '          location.assign(rw);\n'
        '          return window;\n'
        '        }\n'
        '      }\n'
        '      return origWinOpen?origWinOpen.apply(window,arguments):null;\n'
        '    };\n'
        '  }catch(e){}\n'
        '}else if(IS_FILE&&window.parent&&window.parent!==window){\n'
        '  if(typeof window.fetch==="function"&&typeof Promise==="function"){\n'
        '    var origFileFetch=window.fetch;\n'
        '    window.fetch=function(input,init){\n'
        '      var rawUrl=(typeof input==="string")?input:(input&&typeof input.url==="string"?input.url:"");\n'
        '      var absUrl=resolveForTarget(rawUrl);\n'
        '      var hostM=absUrl.match(/^https?:\\/\\/([^\\/:?#]+)/i);\n'
        '      if(!hostM||!isTargetHost(hostM[1])){\n'
        '        return origFileFetch.call(window,input,init);\n'
        '      }\n'
        '      var method=(init&&init.method)||(input&&input.method)||"GET";\n'
        '      var hdrObj={};\n'
        '      var srcHdr=(init&&init.headers)||(input&&input.headers);\n'
        '      try{\n'
        '        if(srcHdr&&typeof srcHdr.forEach==="function"){\n'
        '          srcHdr.forEach(function(v,k){hdrObj[k]=v;});\n'
        '        }else if(srcHdr){\n'
        '          for(var hk in srcHdr)if(Object.prototype.hasOwnProperty.call(srcHdr,hk))hdrObj[hk]=srcHdr[hk];\n'
        '        }\n'
        '      }catch(e){}\n'
        '      var bodyVal=(init&&init.body!==undefined)?init.body:null;\n'
        '      if(typeof URLSearchParams==="function"&&bodyVal instanceof URLSearchParams){bodyVal=bodyVal.toString();}\n'
        '      if(bodyVal!==null&&typeof bodyVal!=="string"){bodyVal=null;}\n'
        '      if(bodyVal===null&&input&&input.method&&input.method!=="GET"&&input.method!=="HEAD"){\n'
        '        return origFileFetch.call(window,input,init);\n'
        '      }\n'
        '      return new Promise(function(resolve,reject){\n'
        '        var id="nx_"+(++xhrSeq);\n'
        '        pendingXhr[id]=function(res){\n'
        '          if(res.error){origFileFetch.call(window,absUrl,init).then(resolve,reject);return;}\n'
        '          var finalRespUrl=res.responseURL||absUrl;\n'
        '          if(typeof Response==="function"){\n'
        '            try{\n'
        '              var rBody=(res.status===204||res.status===304||res.status===205)?null:(res.body||"");\n'
        '              var respObj=new Response(rBody,{status:res.status||200,statusText:res.statusText||"",headers:res.headers||{}});\n'
        '              try{Object.defineProperty(respObj,"url",{value:finalRespUrl});}catch(eU){}\n'
        '              try{Object.defineProperty(respObj,"redirected",{value:!!(res.responseURL&&res.responseURL!==absUrl)});}catch(eR){}\n'
        '              resolve(respObj);\n'
        '              return;\n'
        '            }catch(e){}\n'
        '          }\n'
        '          resolve({\n'
        '            ok:(res.status>=200&&res.status<300),\n'
        '            status:res.status||200,\n'
        '            statusText:res.statusText||"",\n'
        '            url:finalRespUrl,\n'
        '            redirected:!!(res.responseURL&&res.responseURL!==absUrl),\n'
        '            headers:{get:function(k){var lh=String(k||"").toLowerCase();for(var p in res.headers)if(p.toLowerCase()===lh)return res.headers[p];return null;}},\n'
        '            text:function(){return Promise.resolve(res.body||"");},\n'
        '            json:function(){return Promise.resolve(JSON.parse(res.body||"{}"));}\n'
        '          });\n'
        '        };\n'
        '        try{\n'
        '          window.parent.postMessage({type:"neon-xhr",id:id,method:String(method||"GET"),url:absUrl,headers:hdrObj,body:bodyVal},"*");\n'
        '        }catch(e){\n'
        '          delete pendingXhr[id];\n'
        '          origFileFetch.call(window,absUrl,init).then(resolve,reject);\n'
        '        }\n'
        '      });\n'
        '    };\n'
        '  }\n'
        '  if(window.XMLHttpRequest&&XMLHttpRequest.prototype&&XMLHttpRequest.prototype.open){\n'
        '    (function(){\n'
        '      var origOpen=XMLHttpRequest.prototype.open;\n'
        '      var origSend=XMLHttpRequest.prototype.send;\n'
        '      var origSetHdr=XMLHttpRequest.prototype.setRequestHeader;\n'
        '      XMLHttpRequest.prototype.open=function(method,url,asyncFlag){\n'
        '        this.__neonMeta=null;\n'
        '        try{\n'
        '          var absU=resolveForTarget(url);\n'
        '          var hm=absU.match(/^https?:\\/\\/([^\\/:?#]+)/i);\n'
        '          if(hm&&isTargetHost(hm[1])&&asyncFlag!==false){\n'
        '            this.__neonMeta={method:String(method||"GET"),url:absU,headers:{}};\n'
        '            return;\n'
        '          }\n'
        '        }catch(e){}\n'
        '        return origOpen.apply(this,arguments);\n'
        '      };\n'
        '      XMLHttpRequest.prototype.setRequestHeader=function(k,v){\n'
        '        if(this.__neonMeta){this.__neonMeta.headers[k]=v;return;}\n'
        '        return origSetHdr.apply(this,arguments);\n'
        '      };\n'
        '      XMLHttpRequest.prototype.send=function(body){\n'
        '        var self=this;\n'
        '        if(!self.__neonMeta)return origSend.apply(self,arguments);\n'
        '        var meta=self.__neonMeta;\n'
        '        var bStr=(body!==undefined&&body!==null)?(typeof body==="string"?body:String(body)):null;\n'
        '        var id="nx_"+(++xhrSeq);\n'
        '        pendingXhr[id]=function(res){\n'
        '          if(res.error){\n'
        '            if(res.error==="timeout"){try{if(self.ontimeout)self.ontimeout({});}catch(e){}return;}\n'
        '            try{if(self.onerror)self.onerror({});}catch(e){}\n'
        '            return;\n'
        '          }\n'
        '          try{Object.defineProperty(self,"readyState",{value:4,configurable:true});}catch(e){}\n'
        '          try{Object.defineProperty(self,"status",{value:res.status||200,configurable:true});}catch(e){}\n'
        '          try{Object.defineProperty(self,"statusText",{value:res.statusText||"",configurable:true});}catch(e){}\n'
        '          try{Object.defineProperty(self,"responseURL",{value:res.responseURL||meta.url,configurable:true});}catch(e){}\n'
        '          try{Object.defineProperty(self,"responseText",{value:res.body||"",configurable:true});}catch(e){}\n'
        '          try{Object.defineProperty(self,"response",{value:res.body||"",configurable:true});}catch(e){}\n'
        '          self.getAllResponseHeaders=function(){\n'
        '            var out=[];\n'
        '            for(var k in (res.headers||{}))if(Object.prototype.hasOwnProperty.call(res.headers,k))out.push(k+": "+res.headers[k]);\n'
        '            return out.join("\\r\\n");\n'
        '          };\n'
        '          self.getResponseHeader=function(k){\n'
        '            var lh=String(k||"").toLowerCase();\n'
        '            for(var p in (res.headers||{}))if(p.toLowerCase()===lh)return res.headers[p];\n'
        '            return null;\n'
        '          };\n'
        '          try{if(typeof self.onreadystatechange==="function")self.onreadystatechange();}catch(e){}\n'
        '          try{if(typeof self.onload==="function")self.onload();}catch(e){}\n'
        '        };\n'
        '        try{\n'
        '          window.parent.postMessage({type:"neon-xhr",id:id,method:meta.method,url:meta.url,headers:meta.headers,body:bStr},"*");\n'
        '        }catch(e){\n'
        '          delete pendingXhr[id];\n'
        '          origOpen.call(self,meta.method,meta.url,true);\n'
        '          origSend.call(self,body);\n'
        '        }\n'
        '      };\n'
        '    })();\n'
        '  }\n'
        '  try{\n'
        '    window.open=function(u){\n'
        '      if(typeof u==="string"&&u&&u!=="about:blank"){\n'
        '        var r=resolveForTarget(u);\n'
        '        var hm=r.match(/^https?:\\/\\/([^\\/:?#]+)/i);\n'
        '        if(hm&&isTargetHost(hm[1])){postToParent("neon-navigate",{url:r});return window;}\n'
        '      }\n'
        '      return null;\n'
        '    };\n'
        '  }catch(e){}\n'
        '}\n'
        'var entryHref="";\n'
        'try{\n'
        '  entryHref=sessionStorage.getItem("__neon_entry_href")||"";\n'
        '  if(!entryHref){\n'
        '    entryHref=location.pathname+location.search;\n'
        '    sessionStorage.setItem("__neon_entry_href",entryHref);\n'
        '  }\n'
        '}catch(e){entryHref=(location.pathname||"/")+(location.search||"");}\n'
        'var spaDepth=0;\n'
        'var virtualStack=[];\n'
        'if(window.history){\n'
        '  var origPush=window.history.pushState;\n'
        '  var origReplace=window.history.replaceState;\n'
        '  var origBack=window.history.back;\n'
        '  window.history.pushState=function(state,title,url){\n'
        '    spaDepth++;\n'
        '    try{\n'
        '      if(typeof origPush==="function")return origPush.apply(window.history,arguments);\n'
        '    }catch(e){\n'
        '      virtualStack.push({state:state,title:title,url:String(url||"")});\n'
        '    }\n'
        '  };\n'
        '  window.history.replaceState=function(state,title,url){\n'
        '    try{\n'
        '      if(typeof origReplace==="function")return origReplace.apply(window.history,arguments);\n'
        '    }catch(e){\n'
        '      if(virtualStack.length){virtualStack[virtualStack.length-1]={state:state,title:title,url:String(url||"")};}\n'
        '    }\n'
        '  };\n'
        '  window.history.back=function(){\n'
        '    if(virtualStack.length){\n'
        '      var popped=virtualStack.pop();\n'
        '      if(spaDepth>0)spaDepth--;\n'
        '      try{window.dispatchEvent(new PopStateEvent("popstate",{state:popped?popped.state:null}));}catch(e){}\n'
        '      return;\n'
        '    }\n'
        '    if(spaDepth>0)spaDepth--;\n'
        '    if(typeof origBack==="function")return origBack.apply(window.history,arguments);\n'
        '  };\n'
        '}\n'
        'window.addEventListener("popstate",function(){if(spaDepth>0)spaDepth--;});\n'
        'var currentFocusEl=null;\n'
        'var cursorMode=false;\n'
        'var cursorX=Math.round((window.innerWidth||1920)/2);\n'
        'var cursorY=Math.round((window.innerHeight||1080)/2);\n'
        'var ringEl=null;\n'
        'var cursorEl=null;\n'
        'var toastEl=null;\n'
        'var toastTimer=null;\n'
        'var styleDone=false;\n'
        'var scanTimer=null;\n'
        'var hydratedReady=false;\n'
        'var playerNavExplicit=false;\n'
        'var playerNavAt=0;\n'
        'var lastBackExitAt=0;\n'
        'var playerWakeTimers=[];\n'
        'var wakeBarEls=[];\n'
        'var seekStreakTotal=0;\n'
        'var seekStreakAt=0;\n'
        'var seekTargetTime=0;\n'
        'var seekLastVid=null;\n'
        'var seekSeq=0;\n'
        'var playerInactivityTimer=null;\n'
        'function dismissPlayerNav(){\n'
        '  if(playerInactivityTimer){clearTimeout(playerInactivityTimer);playerInactivityTimer=null;}\n'
        '  var v=activeVideo(false);\n'
        '  if(!v||v.paused)return;\n'
        '  playerNavExplicit=false;\n'
        '  clearPlayerWakeTimers();\n'
        '  clearPlayerWakeBars();\n'
        '  if(currentFocusEl){\n'
        '    try{currentFocusEl.className=String(currentFocusEl.className||"").replace(/\\bneon-tv-focused\\b/g,"").replace(/\\s+/g," ").trim();}catch(e){}\n'
        '    try{if(document.activeElement&&document.activeElement===currentFocusEl)document.activeElement.blur();}catch(e){}\n'
        '    currentFocusEl=null;\n'
        '  }\n'
        '  if(document.activeElement&&document.activeElement!==document.body&&document.activeElement!==document.documentElement){\n'
        '    try{document.activeElement.blur();}catch(e){}\n'
        '  }\n'
        '  if(ringEl)ringEl.style.opacity="0";\n'
        '}\n'
        'function resetPlayerInactivity(){\n'
        '  if(playerInactivityTimer){clearTimeout(playerInactivityTimer);playerInactivityTimer=null;}\n'
        '  var v=activeVideo(false);\n'
        '  if(!v||v.paused)return;\n'
        '  playerInactivityTimer=setTimeout(function(){dismissPlayerNav();},4800);\n'
        '}\n'
        '(function(){\n'
        '  try{\n'
        '    var proto=(typeof HTMLMediaElement!=="undefined"&&HTMLMediaElement.prototype)||(typeof HTMLVideoElement!=="undefined"&&HTMLVideoElement.prototype);\n'
        '    if(proto){\n'
        '      var desc=Object.getOwnPropertyDescriptor(proto,"currentTime");\n'
        '      if(desc&&typeof desc.get==="function"&&typeof desc.set==="function"){\n'
        '        var pendingTimes=new WeakMap();\n'
        '        Object.defineProperty(proto,"currentTime",{\n'
        '          configurable:true,\n'
        '          enumerable:desc.enumerable,\n'
        '          get:function(){\n'
        '            var p=pendingTimes.get(this);\n'
        '            if(p&&(Date.now()-p.at)<2500)return p.time;\n'
        '            return desc.get.call(this);\n'
        '          },\n'
        '          set:function(val){\n'
        '            var n=Number(val);\n'
        '            if(isNaN(n))return;\n'
        '            var now=Date.now();\n'
        '            pendingTimes.set(this,{time:n,at:now});\n'
        '            desc.set.call(this,n);\n'
        '            try{if(typeof this.fastSeek==="function")this.fastSeek(n);}catch(e){}\n'
        '          }\n'
        '        });\n'
        '      }\n'
        '    }\n'
        '  }catch(e){}\n'
        '})();\n'
        'function getStyleSafe(el){\n'
        '  try{return window.getComputedStyle?window.getComputedStyle(el):null;}catch(e){return null;}\n'
        '}\n'
        'function getRectSafe(el){\n'
        '  try{return el&&el.getBoundingClientRect?el.getBoundingClientRect():null;}catch(e){return null;}\n'
        '}\n'
        'function tagOf(el){return (el&&el.tagName)?String(el.tagName).toUpperCase():"";}\n'
        'function isTextInput(el){\n'
        '  var t=tagOf(el);\n'
        '  if(t==="TEXTAREA")return !el.disabled&&!el.readOnly;\n'
        '  if(t!=="INPUT")return false;\n'
        '  var tp=String(el.type||"text").toLowerCase();\n'
        '  return tp!=="button"&&tp!=="submit"&&tp!=="reset"&&tp!=="checkbox"&&tp!=="radio"&&tp!=="range"&&tp!=="file"&&tp!=="hidden"&&!el.disabled&&!el.readOnly;\n'
        '}\n'
        'function isSliderEl(el){\n'
        '  if(!el)return false;\n'
        '  if(tagOf(el)==="INPUT"&&String(el.type||"").toLowerCase()==="range")return true;\n'
        '  var role=String(el.getAttribute&&el.getAttribute("role")||"").toLowerCase();\n'
        '  return role==="slider";\n'
        '}\n'
        'function ensureUi(){\n'
        '  var host=document.documentElement||document.body;\n'
        '  if(!host)return;\n'
        '  if(!styleDone&&!document.getElementById("neon-tv-style")){\n'
        '    var st=document.createElement("style");\n'
        '    st.id="neon-tv-style";\n'
        '    st.type="text/css";\n'
        '    st.appendChild(document.createTextNode(\n'
        '      "html,body{scroll-behavior:auto !important;-webkit-overflow-scrolling:auto !important;}\\n"+\n'
        '      "*:not(#neon-tv-ring):not(#neon-tv-cursor):not(#neon-tv-toast){box-shadow:none !important;text-shadow:none !important;will-change:auto !important;}\\n"+\n'
        '      "*{backdrop-filter:none !important;-webkit-backdrop-filter:none !important;}\\n"+\n'
        '      "*:not(#neon-tv-ring):not(#neon-tv-cursor){transition-duration:0.04s !important;transition-delay:0s !important;}\\n"+\n'
        '      "*:not(#neon-tv-ring):not(#neon-tv-cursor):not(#neon-tv-toast){animation-iteration-count:1 !important;animation-duration:0.001s !important;}\\n"+\n'
        '      "img,svg{filter:none !important;}\\n"+\n'
        '      ".neon-tv-focused{outline:2px solid rgba(125,211,252,0.35) !important;outline-offset:2px !important;}\\n"+\n'
        '      "#neon-tv-ring{position:fixed;pointer-events:none;z-index:2147483645;border:3px solid #38bdf8;"+\n'
        '      "background:rgba(56,189,248,0.14);border-radius:8px;box-shadow:none !important;"+\n'
        '      "transition:left 60ms cubic-bezier(0.16,1,0.3,1),top 60ms cubic-bezier(0.16,1,0.3,1),width 60ms cubic-bezier(0.16,1,0.3,1),height 60ms cubic-bezier(0.16,1,0.3,1),opacity 50ms ease-out;"+\n'
        '      "will-change:left,top,width,height;opacity:0;box-sizing:border-box;display:block;}\\n"+\n'
        '      "#neon-tv-cursor{position:fixed;pointer-events:none;z-index:2147483646;width:24px;height:24px;"+\n'
        '      "margin:-12px 0 0 -12px;border-radius:50%;border:3px solid #ffffff;background:rgba(56,189,248,0.92);"+\n'
        '      "box-shadow:0 2px 12px rgba(0,0,0,0.7);display:none;}\\n"+\n'
        '      "#neon-tv-toast{position:fixed;right:32px;bottom:28px;z-index:2147483647;padding:10px 16px;"+\n'
        '      "border-radius:10px;background:rgba(8,14,22,0.92);border:1px solid rgba(56,189,248,0.55);"+\n'
        '      "color:#eaf4ff;font:600 15px/1.3 sans-serif;pointer-events:none;display:none;}"+\n'
        '      "[data-neon-wake-bar=\\"1\\"]{opacity:1 !important;visibility:visible !important;pointer-events:auto !important;}"\n'
        '    ));\n'
        '    (document.head||host).appendChild(st);\n'
        '    styleDone=true;\n'
        '  }\n'
        '  if(!ringEl||!ringEl.parentNode){\n'
        '    ringEl=document.getElementById("neon-tv-ring")||document.createElement("div");\n'
        '    ringEl.id="neon-tv-ring";\n'
        '    host.appendChild(ringEl);\n'
        '  }\n'
        '  if(!cursorEl||!cursorEl.parentNode){\n'
        '    cursorEl=document.getElementById("neon-tv-cursor")||document.createElement("div");\n'
        '    cursorEl.id="neon-tv-cursor";\n'
        '    host.appendChild(cursorEl);\n'
        '  }\n'
        '  if(!toastEl||!toastEl.parentNode){\n'
        '    toastEl=document.getElementById("neon-tv-toast")||document.createElement("div");\n'
        '    toastEl.id="neon-tv-toast";\n'
        '    host.appendChild(toastEl);\n'
        '  }\n'
        '}\n'
        'function showToast(msg){\n'
        '  ensureUi();\n'
        '  if(!toastEl)return;\n'
        '  toastEl.textContent=msg;\n'
        '  toastEl.style.display="block";\n'
        '  if(toastTimer)clearTimeout(toastTimer);\n'
        '  toastTimer=setTimeout(function(){if(toastEl)toastEl.style.display="none";},2200);\n'
        '}\n'
        'function hasAttr(el,name){return !!(el&&el.hasAttribute&&el.hasAttribute(name));}\n'
        'function inDoc(el){\n'
        '  if(!el)return false;\n'
        '  if(el===document.body||el===document.documentElement)return true;\n'
        '  if(typeof el.isConnected==="boolean")return el.isConnected;\n'
        '  try{return !!(document.documentElement&&document.documentElement.contains(el));}catch(e){return false;}\n'
        '}\n'
        'function isElemVisible(el,r){\n'
        '  if(!inDoc(el))return false;\n'
        '  if(el.disabled||el.getAttribute("aria-disabled")==="true")return false;\n'
        '  if(!r)r=getRectSafe(el);\n'
        '  if(!r||r.width<4||r.height<4)return false;\n'
        '  var vw=window.innerWidth||1920;\n'
        '  var vh=window.innerHeight||1080;\n'
        '  if(r.top>vh+950||r.bottom<-350||r.left>vw+1800)return false;\n'
        '  var cur=el;\n'
        '  while(cur&&cur.nodeType===1){\n'
        '    if(cur.getAttribute("aria-hidden")==="true"||hasAttr(cur,"inert")||hasAttr(cur,"hidden"))return false;\n'
        '    var st=getStyleSafe(cur);\n'
        '    if(st){\n'
        '      if(st.display==="none"||st.visibility==="hidden"||st.opacity==="0")return false;\n'
        '      if(cur!==el&&(st.overflowX==="hidden"||st.overflowX==="clip"||st.overflowY==="hidden"||st.overflowY==="clip")){\n'
        '        var ar=getRectSafe(cur);\n'
        '        if(ar&&(ar.width<2||ar.height<2))return false;\n'
        '        if(ar&&((st.overflowX==="hidden"||st.overflowX==="clip")&&(r.right<=ar.left+1||r.left>=ar.right-1)))return false;\n'
        '        if(ar&&((st.overflowY==="hidden"||st.overflowY==="clip")&&(r.bottom<=ar.top+1||r.top>=ar.bottom-1)))return false;\n'
        '      }\n'
        '    }\n'
        '    cur=cur.parentElement;\n'
        '  }\n'
        '  if(r.right<=2){\n'
        '    var p=el.parentElement;\n'
        '    var inScrolled=false;\n'
        '    while(p&&p.nodeType===1){\n'
        '      if((p.scrollWidth||0)>(p.clientWidth||0)+4){inScrolled=true;break;}\n'
        '      p=p.parentElement;\n'
        '    }\n'
        '    if(!inScrolled)return false;\n'
        '  }\n'
        '  var cx=r.left+r.width/2,cy=r.top+r.height/2;\n'
        '  if(cx>=2&&cx<=vw-2&&cy>=2&&cy<=vh-2&&typeof document.elementFromPoint==="function"){\n'
        '    try{\n'
        '      var hit=document.elementFromPoint(cx,cy);\n'
        '      if(hit&&hit!==el&&!el.contains(hit)&&!hit.contains(el)){\n'
        '        var hCur=hit;\n'
        '        while(hCur&&hCur.nodeType===1){\n'
        '          var hSt=getStyleSafe(hCur);\n'
        '          if(hSt&&(hSt.position==="fixed"||hSt.position==="sticky")&&!hCur.contains(el))return false;\n'
        '          hCur=hCur.parentElement;\n'
        '        }\n'
        '      }\n'
        '    }catch(e){}\n'
        '  }\n'
        '  return true;\n'
        '}\n'
        'function activeDialogRoot(){\n'
        '  var base=document.body||document.documentElement;\n'
        '  if(!base||!base.querySelectorAll)return base;\n'
        '  var dlgs=base.querySelectorAll("[role=\\"dialog\\"],[role=\\"alertdialog\\"],[aria-modal=\\"true\\"]");\n'
        '  for(var i=dlgs.length-1;i>=0;i--){\n'
        '    var d=dlgs[i];\n'
        '    var dr=getRectSafe(d);\n'
        '    if(!dr||dr.width<40||dr.height<40)continue;\n'
        '    if(d.getAttribute&&d.getAttribute("aria-modal")!=="true"&&(dr.width<140||dr.height<80))continue;\n'
        '    if(isElemVisible(d,dr)){\n'
        '      var inner=d.querySelectorAll("a[href],button,input,textarea,select,[role=\\"button\\"],[tabindex]");\n'
        '      if(inner&&inner.length)return d;\n'
        '    }\n'
        '  }\n'
        '  return base;\n'
        '}\n'
        'function isSemanticControl(el){\n'
        '  var t=tagOf(el);\n'
        '  if(t==="A")return hasAttr(el,"href");\n'
        '  if(t==="BUTTON"||t==="SELECT"||t==="TEXTAREA"||t==="SUMMARY")return true;\n'
        '  if(t==="INPUT")return String(el.type||"").toLowerCase()!=="hidden";\n'
        '  if(t==="VIDEO")return hasAttr(el,"controls");\n'
        '  var role=String(el.getAttribute&&el.getAttribute("role")||"").toLowerCase();\n'
        '  if(role==="button"||role==="link"||role==="menuitem"||role==="menuitemradio"||role==="menuitemcheckbox"||role==="tab"||role==="option"||role==="switch"||role==="checkbox"||role==="radio"||role==="slider")return true;\n'
        '  if(hasAttr(el,"data-neon-focusable")||hasAttr(el,"aria-selected")||hasAttr(el,"aria-pressed"))return true;\n'
        '  var ti=el.getAttribute&&el.getAttribute("tabindex");\n'
        '  if(ti!==null&&ti!==""&&ti!=="-1")return true;\n'
        '  return false;\n'
        '}\n'
        'function hasReactHandlers(obj){\n'
        '  return !!(obj&&(typeof obj.onClick==="function"||typeof obj.onClickCapture==="function"||typeof obj.onMouseDown==="function"||typeof obj.onPointerDown==="function"||typeof obj.onMouseUp==="function"||typeof obj.onPointerUp==="function"||typeof obj.onTouchEnd==="function"||typeof obj.onTouchStart==="function"));\n'
        '}\n'
        'function hasReactClick(el){\n'
        '  if(!el)return false;\n'
        '  try{\n'
        '    var names=Object.getOwnPropertyNames(el);\n'
        '    for(var i=0;i<names.length;i++){\n'
        '      var k=names[i];\n'
        '      if(k.indexOf("__reactProps$")===0||k.indexOf("__reactEventHandlers$")===0){\n'
        '        if(hasReactHandlers(el[k]))return true;\n'
        '      }else if(k.indexOf("__reactFiber$")===0||k.indexOf("__reactInternalInstance$")===0){\n'
        '        var fb=el[k];\n'
        '        if(fb&&hasReactHandlers(fb.memoizedProps))return true;\n'
        '      }\n'
        '    }\n'
        '  }catch(e){}\n'
        '  return false;\n'
        '}\n'
        'function collectCandidates(){\n'
        '  var root=activeDialogRoot();\n'
        '  if(!root||!root.querySelectorAll)return [];\n'
        '  var out=[];\n'
        '  var seen=[];\n'
        '  function addCandidate(el){\n'
        '    if(seen.indexOf(el)>=0)return false;\n'
        '    if(el.id==="neon-tv-ring"||el.id==="neon-tv-cursor"||el.id==="neon-tv-toast")return false;\n'
        '    var r=getRectSafe(el);\n'
        '    if((!r||r.width<4||r.height<4)&&el.children&&el.children.length===1){\n'
        '      r=getRectSafe(el.children[0]);\n'
        '    }\n'
        '    if(!isElemVisible(el,r))return false;\n'
        '    seen.push(el);\n'
        '    out.push({el:el,r:r});\n'
        '    return true;\n'
        '  }\n'
        '  var sem=root.querySelectorAll("a[href],button,input:not([type=\\"hidden\\"]),textarea,select,summary,video[controls],[role=\\"button\\"],[role=\\"link\\"],[role=\\"menuitem\\"],[role=\\"menuitemradio\\"],[role=\\"menuitemcheckbox\\"],[role=\\"tab\\"],[role=\\"option\\"],[role=\\"switch\\"],[role=\\"checkbox\\"],[role=\\"radio\\"],[role=\\"slider\\"],[aria-selected],[aria-pressed],[tabindex],[data-neon-focusable=\\"1\\"],[onclick]");\n'
        '  for(var i=0;i<sem.length;i++){\n'
        '    var el=sem[i];\n'
        '    var ti=el.getAttribute&&el.getAttribute("tabindex");\n'
        '    if(ti==="-1"&&!hasAttr(el,"data-neon-focusable")&&!isSemanticControl(el)&&!hasReactClick(el))continue;\n'
        '    addCandidate(el);\n'
        '  }\n'
        '  var vw=window.innerWidth||1920;\n'
        '  var vh=window.innerHeight||1080;\n'
        '  var extras=root.querySelectorAll("a,div,li,article,section,figure,label,span,p,h1,h2,h3,h4,img");\n'
        '  for(var j=0;j<extras.length;j++){\n'
        '    var ex=extras[j];\n'
        '    if(seen.indexOf(ex)>=0)continue;\n'
        '    var isBareA=(tagOf(ex)==="A");\n'
        '    var skipFast=false;\n'
        '    var ancFast=ex.parentElement;\n'
        '    while(ancFast&&ancFast!==root.parentElement){\n'
        '      if(seen.indexOf(ancFast)>=0&&(tagOf(ancFast)==="BUTTON"||tagOf(ancFast)==="A")){skipFast=true;break;}\n'
        '      ancFast=ancFast.parentElement;\n'
        '    }\n'
        '    if(skipFast)continue;\n'
        '    var rcClick=hasReactClick(ex);\n'
        '    var isPtr=false;\n'
        '    if(!rcClick&&!isBareA){\n'
        '      var st=getStyleSafe(ex);\n'
        '      isPtr=!!(st&&st.cursor==="pointer");\n'
        '      if(!isPtr)continue;\n'
        '    }\n'
        '    var er=getRectSafe(ex);\n'
        '    if(!er||er.width<10||er.height<10)continue;\n'
        '    if(er.width>vw*0.85&&er.height>vh*0.72)continue;\n'
        '    var skipAnc=false;\n'
        '    var anc=ex.parentElement;\n'
        '    while(anc&&anc!==root.parentElement){\n'
        '      if(seen.indexOf(anc)>=0){\n'
        '        if(tagOf(anc)==="BUTTON"||tagOf(anc)==="A"){skipAnc=true;break;}\n'
        '        if(!rcClick&&!isPtr){skipAnc=true;break;}\n'
        '        var ar=getRectSafe(anc);\n'
        '        if(ar&&er.width>=ar.width*0.85&&er.height>=ar.height*0.85){skipAnc=true;break;}\n'
        '      }\n'
        '      anc=anc.parentElement;\n'
        '    }\n'
        '    if(skipAnc)continue;\n'
        '    var innerSem=ex.querySelectorAll("a[href],button,input:not([type=\\"hidden\\"]),select,textarea,[role=\\"button\\"],[role=\\"link\\"],[role=\\"slider\\"]");\n'
        '    if(innerSem&&innerSem.length>1)continue;\n'
        '    if(innerSem&&innerSem.length===1){\n'
        '      if(er.width>vw*0.42&&er.height<260)continue;\n'
        '      var ir=getRectSafe(innerSem[0]);\n'
        '      if(ir&&ir.width>=er.width*0.65&&ir.height>=er.height*0.65)continue;\n'
        '    }\n'
        '    if(!isBareA&&er.width>vw*0.42&&er.height<260&&ex.querySelectorAll){\n'
        '      var innerD=ex.querySelectorAll("div,span,button");\n'
        '      var rcKids=0;\n'
        '      for(var di=0;di<innerD.length&&di<40;di++){\n'
        '        if(hasReactClick(innerD[di])){rcKids++;if(rcKids>=1)break;}\n'
        '      }\n'
        '      if(rcKids>=1)continue;\n'
        '    }\n'
        '    addCandidate(ex);\n'
        '  }\n'
        '  return out;\n'
        '}\n'
        'function updateRing(){\n'
        '  ensureUi();\n'
        '  if(!ringEl)return;\n'
        '  if(cursorMode||!inDoc(currentFocusEl)){\n'
        '    ringEl.style.opacity="0";\n'
        '    return;\n'
        '  }\n'
        '  var vid=activeVideo(false);\n'
        '  if(vid&&!vid.paused&&currentFocusEl&&isPlayerArea(currentFocusEl,vid)){\n'
        '    var crSt=getStyleSafe(currentFocusEl);\n'
        '    if(!crSt||crSt.display==="none"||crSt.visibility==="hidden"||crSt.opacity==="0"){\n'
        '      dismissPlayerNav();\n'
        '      return;\n'
        '    }\n'
        '    var pr=currentFocusEl.parentElement;\n'
        '    for(var lvl=0;lvl<6&&pr&&pr!==document.body&&pr!==document.documentElement;lvl++){\n'
        '      var prSt=getStyleSafe(pr);\n'
        '      if(prSt&&(prSt.display==="none"||prSt.visibility==="hidden"||prSt.opacity==="0")){\n'
        '        dismissPlayerNav();\n'
        '        return;\n'
        '      }\n'
        '      pr=pr.parentElement;\n'
        '    }\n'
        '  }\n'
        '  var r=getRectSafe(currentFocusEl);\n'
        '  if(!r||r.width<2||r.height<2){\n'
        '    ringEl.style.opacity="0";\n'
        '    return;\n'
        '  }\n'
        '  var st=getStyleSafe(currentFocusEl);\n'
        '  ringEl.style.left=Math.round(r.left-2)+"px";\n'
        '  ringEl.style.top=Math.round(r.top-2)+"px";\n'
        '  ringEl.style.width=Math.round(r.width+4)+"px";\n'
        '  ringEl.style.height=Math.round(r.height+4)+"px";\n'
        '  ringEl.style.borderRadius=(st&&st.borderRadius&&st.borderRadius!=="0px")?st.borderRadius:"10px";\n'
        '  ringEl.style.opacity="1";\n'
        '}\n'
        'function scrollElComfortably(el){\n'
        '  var r=getRectSafe(el);\n'
        '  if(!r)return;\n'
        '  var vw=window.innerWidth||1920;\n'
        '  var vh=window.innerHeight||1080;\n'
        '  if(r.top<74||r.bottom>vh-48||r.left<32||r.right>vw-32){\n'
        '    try{el.scrollIntoView({block:"center",inline:"center",behavior:"auto"});}\n'
        '    catch(e){try{el.scrollIntoView(false);}catch(e2){}}\n'
        '  }\n'
        '}\n'
        'function focusEl(el,openIme){\n'
        '  if(!el)return;\n'
        '  if(currentFocusEl&&currentFocusEl!==el){\n'
        '    try{currentFocusEl.className=String(currentFocusEl.className||"").replace(/\\bneon-tv-focused\\b/g,"").replace(/\\s+/g," ").replace(/^\\s+|\\s+$/g,"");}catch(e){}\n'
        '  }\n'
        '  if(document.activeElement&&document.activeElement!==el&&document.activeElement!==document.body&&document.activeElement!==document.documentElement){\n'
        '    try{document.activeElement.blur();}catch(e){}\n'
        '  }\n'
        '  currentFocusEl=el;\n'
        '  var vFocus=activeVideo(false);\n'
        '  if(vFocus&&isPlayerArea(el,vFocus))resetPlayerInactivity();\n'
        '  if(hydratedReady){\n'
        '    try{\n'
        '      var cn=String(el.className||"");\n'
        '      if(cn.indexOf("neon-tv-focused")<0)el.className=(cn?cn+" ":"")+"neon-tv-focused";\n'
        '    }catch(e){}\n'
        '  }\n'
        '  scrollElComfortably(el);\n'
        '  if(isTextInput(el)){\n'
        '    if(openIme){try{el.focus();}catch(e){}}\n'
        '  }else{\n'
        '    if(!hasAttr(el,"tabindex")&&el.tabIndex<0){\n'
        '      try{el.setAttribute("data-neon-focusable","1");el.setAttribute("tabindex","-1");}catch(e){}\n'
        '    }\n'
        '    try{el.focus({preventScroll:true});}catch(e){try{el.focus();}catch(e2){}}\n'
        '  }\n'
        '  updateRing();\n'
        '}\n'
        'function pickInitial(cands){\n'
        '  if(!cands||!cands.length)return null;\n'
        '  var vh=window.innerHeight||1080;\n'
        '  var vw=window.innerWidth||1920;\n'
        '  var best=null,bestScore=1e12;\n'
        '  for(var i=0;i<cands.length;i++){\n'
        '    var r=cands[i].r;\n'
        '    var inVp=(r.top>=0&&r.left>=0&&r.bottom<=vh&&r.right<=vw)?0:50000;\n'
        '    var sc=inVp+r.top*1.5+r.left;\n'
        '    if(sc<bestScore){bestScore=sc;best=cands[i].el;}\n'
        '  }\n'
        '  return best;\n'
        '}\n'
        'var userAllowInputFocusUntil=0;\n'
        'function findNext(cur,dir,cands){\n'
        '  if(!cands||!cands.length)return null;\n'
        '  var cr=cur?getRectSafe(cur):null;\n'
        '  if(!cur||!cr){\n'
        '    return pickInitial(cands);\n'
        '  }\n'
        '  var cx=cr.left+cr.width/2,cy=cr.top+cr.height/2;\n'
        '  function scanPass(strictRow){\n'
        '    var best=null,bestScore=1e15;\n'
        '    for(var i=0;i<cands.length;i++){\n'
        '      var c=cands[i];\n'
        '      if(c.el===cur)continue;\n'
        '      var r=c.r;\n'
        '      var tx=r.left+r.width/2,ty=r.top+r.height/2;\n'
        '      var alongEdge=0,alongCenter=0,crossEdge=0,centerCross=0,overlap=false,score=0;\n'
        '      if(dir==="down"){\n'
        '        if(!(ty>cy+2&&(r.top>=cr.top+2||r.bottom>cr.bottom+2)))continue;\n'
        '        alongEdge=Math.max(0,r.top-cr.bottom);\n'
        '        alongCenter=ty-cy;\n'
        '        overlap=(r.left<cr.right-1)&&(r.right>cr.left+1);\n'
        '        crossEdge=overlap?0:(r.left>=cr.right?r.left-cr.right:cr.left-r.right);\n'
        '        centerCross=Math.abs(tx-cx);\n'
        '        score=alongEdge*2.0+alongCenter*0.25+crossEdge*0.85+centerCross*0.12;\n'
        '      }else if(dir==="up"){\n'
        '        if(!(ty<cy-2&&(r.bottom<=cr.bottom-2||r.top<cr.top-2)))continue;\n'
        '        alongEdge=Math.max(0,cr.top-r.bottom);\n'
        '        alongCenter=cy-ty;\n'
        '        overlap=(r.left<cr.right-1)&&(r.right>cr.left+1);\n'
        '        crossEdge=overlap?0:(r.left>=cr.right?r.left-cr.right:cr.left-r.right);\n'
        '        centerCross=Math.abs(tx-cx);\n'
        '        score=alongEdge*2.0+alongCenter*0.25+crossEdge*0.85+centerCross*0.12;\n'
        '      }else if(dir==="right"){\n'
        '        if(!(tx>cx+2&&(r.left>=cr.left+2||r.right>cr.right+2)))continue;\n'
        '        alongEdge=Math.max(0,r.left-cr.right);\n'
        '        alongCenter=tx-cx;\n'
        '        overlap=(r.top<cr.bottom-1)&&(r.bottom>cr.top+1);\n'
        '        crossEdge=overlap?0:(r.top>=cr.bottom?r.top-cr.bottom:cr.top-r.bottom);\n'
        '        if(strictRow&&!overlap)continue;\n'
        '        centerCross=Math.abs(ty-cy);\n'
        '        score=alongEdge*2.0+alongCenter*0.25+crossEdge*(strictRow?1.2:1.6)+centerCross*0.22;\n'
        '      }else if(dir==="left"){\n'
        '        if(!(tx<cx-2&&(r.right<=cr.right-2||r.left<cr.left-2)))continue;\n'
        '        alongEdge=Math.max(0,cr.left-r.right);\n'
        '        alongCenter=cx-tx;\n'
        '        overlap=(r.top<cr.bottom-1)&&(r.bottom>cr.top+1);\n'
        '        crossEdge=overlap?0:(r.top>=cr.bottom?r.top-cr.bottom:cr.top-r.bottom);\n'
        '        if(strictRow&&!overlap)continue;\n'
        '        centerCross=Math.abs(ty-cy);\n'
        '        score=alongEdge*2.0+alongCenter*0.25+crossEdge*(strictRow?1.2:1.6)+centerCross*0.22;\n'
        '      }\n'
        '      if(score<bestScore){bestScore=score;best=c.el;}\n'
        '    }\n'
        '    return best;\n'
        '  }\n'
        '  var res=scanPass(true);\n'
        '  if(!res&&(dir==="left"||dir==="right"))res=scanPass(false);\n'
        '  return res;\n'
        '}\n'
        'function moveFocus(dir){\n'
        '  hydratedReady=true;\n'
        '  var cands=collectCandidates();\n'
        '  if(!cands.length)return false;\n'
        '  var curInCands=false;\n'
        '  if(inDoc(currentFocusEl)){\n'
        '    for(var ci=0;ci<cands.length;ci++){\n'
        '      if(cands[ci].el===currentFocusEl){curInCands=true;break;}\n'
        '    }\n'
        '  }\n'
        '  if(!curInCands){\n'
        '    var initEl=pickInitial(cands);\n'
        '    if(initEl){focusEl(initEl,false);return true;}\n'
        '    return false;\n'
        '  }\n'
        '  var next=findNext(currentFocusEl,dir,cands);\n'
        '  if(next){\n'
        '    focusEl(next,false);\n'
        '    return true;\n'
        '  }\n'
        '  if(dir==="down"&&typeof window.scrollBy==="function"){\n'
        '    var docEl=document.documentElement||document.body;\n'
        '    var maxY=docEl?(docEl.scrollHeight-(window.innerHeight||1080)):0;\n'
        '    if((window.scrollY||window.pageYOffset||0)<maxY-4){\n'
        '      window.scrollBy(0,Math.round((window.innerHeight||1080)*0.42));\n'
        '      setTimeout(function(){\n'
        '        var c2=collectCandidates();\n'
        '        var n2=findNext(currentFocusEl,"down",c2);\n'
        '        if(n2)focusEl(n2,false);else updateRing();\n'
        '      },60);\n'
        '      return true;\n'
        '    }\n'
        '  }else if(dir==="up"&&typeof window.scrollBy==="function"){\n'
        '    if((window.scrollY||window.pageYOffset||0)>4){\n'
        '      window.scrollBy(0,-Math.round((window.innerHeight||1080)*0.42));\n'
        '      setTimeout(function(){\n'
        '        var c2=collectCandidates();\n'
        '        var n2=findNext(currentFocusEl,"up",c2);\n'
        '        if(n2)focusEl(n2,false);else updateRing();\n'
        '      },60);\n'
        '      return true;\n'
        '    }\n'
        '  }\n'
        '  return false;\n'
        '}\n'
        'function fireClick(el){\n'
        '  if(!el)return;\n'
        '  if(isTextInput(el)){\n'
        '    userAllowInputFocusUntil=Date.now()+1200;\n'
        '    focusEl(el,true);\n'
        '    try{el.click();}catch(e){}\n'
        '    return;\n'
        '  }\n'
        '  if(tagOf(el)==="LABEL")userAllowInputFocusUntil=Date.now()+900;\n'
        '  var r=getRectSafe(el);\n'
        '  var cx=r?Math.round(r.left+r.width/2):0;\n'
        '  var cy=r?Math.round(r.top+r.height/2):0;\n'
        '  var opts={bubbles:true,cancelable:true,view:window,button:0,buttons:1,clientX:cx,clientY:cy,pointerId:1,pointerType:"mouse",isPrimary:true};\n'
        '  try{if(typeof PointerEvent==="function")el.dispatchEvent(new PointerEvent("pointerdown",opts));}catch(e){}\n'
        '  try{el.dispatchEvent(new MouseEvent("mousedown",opts));}catch(e){}\n'
        '  try{if(typeof PointerEvent==="function")el.dispatchEvent(new PointerEvent("pointerup",opts));}catch(e){}\n'
        '  try{el.dispatchEvent(new MouseEvent("mouseup",opts));}catch(e){}\n'
        '  try{el.click();}catch(e){try{el.dispatchEvent(new MouseEvent("click",opts));}catch(e2){}}\n'
        '  setTimeout(function(){scheduleRescan(120);},120);\n'
        '}\n'
        'function postToParent(type,extra){\n'
        '  var payload={type:type};\n'
        '  if(extra){for(var k in extra)if(Object.prototype.hasOwnProperty.call(extra,k))payload[k]=extra[k];}\n'
        '  if(window.parent&&window.parent!==window){\n'
        '    try{window.parent.postMessage(payload,"*");return true;}catch(e){}\n'
        '  }\n'
        '  return false;\n'
        '}\n'
        'function exitNow(){\n'
        '  if(!postToParent("neon-exit")){\n'
        '    try{tizen.application.getCurrentApplication().exit();}catch(e){try{window.close();}catch(e2){}}\n'
        '  }\n'
        '}\n'
        'function isPlayerArea(el,vid){\n'
        '  if(!el||!vid)return false;\n'
        '  var vr=getRectSafe(vid),fr=getRectSafe(el);\n'
        '  if(!vr||!fr)return false;\n'
        '  return fr.bottom>=vr.top-40&&fr.top<=vr.bottom+80&&fr.right>=vr.left-20&&fr.left<=vr.right+20;\n'
        '}\n'
        'function isSmallPlayerControl(el,vid){\n'
        '  if(!el||!vid||!inDoc(el)||!isElemVisible(el))return false;\n'
        '  if(el===vid||tagOf(el)==="VIDEO")return false;\n'
        '  var vr=getRectSafe(vid),fr=getRectSafe(el);\n'
        '  if(!vr||!fr)return false;\n'
        '  if(!(fr.bottom>=vr.top-40&&fr.top<=vr.bottom+80&&fr.right>=vr.left-20&&fr.left<=vr.right+20))return false;\n'
        '  if(fr.width>=vr.width*0.50&&fr.height>=vr.height*0.40)return false;\n'
        '  if(!playerNavExplicit&&fr.top<vr.top+vr.height*0.45)return false;\n'
        '  return true;\n'
        '}\n'
        'function isTimelineSlider(el,vid){\n'
        '  if(!isSliderEl(el))return false;\n'
        '  var al=(String(el.getAttribute&&el.getAttribute("aria-label")||"")+" "+String(el.id||"")+" "+String(el.className||"")).toLowerCase();\n'
        '  if(/volume|громк|звук|mute|audio/i.test(al))return false;\n'
        '  var r=getRectSafe(el),vr=vid?getRectSafe(vid):null;\n'
        '  var vw=window.innerWidth||1920;\n'
        '  if(r&&vr)return r.width>=vr.width*0.30;\n'
        '  if(r)return r.width>=vw*0.22;\n'
        '  return true;\n'
        '}\n'
        'function clearPlayerWakeBars(){\n'
        '  for(var i=0;i<wakeBarEls.length;i++){\n'
        '    try{wakeBarEls[i].removeAttribute("data-neon-wake-bar");}catch(e){}\n'
        '  }\n'
        '  wakeBarEls=[];\n'
        '}\n'
        'function clearPlayerWakeTimers(){\n'
        '  for(var i=0;i<playerWakeTimers.length;i++){try{clearTimeout(playerWakeTimers[i]);}catch(e){}}\n'
        '  playerWakeTimers=[];\n'
        '}\n'
        'function forceRevealPlayerBar(vid){\n'
        '  if(!vid)return false;\n'
        '  var vr=getRectSafe(vid);\n'
        '  if(!vr)return false;\n'
        '  var doc=vid.ownerDocument||document;\n'
        '  var anc=vid.parentElement;\n'
        '  var changed=false;\n'
        '  for(var lvl=0;lvl<6&&anc;lvl++){\n'
        '    if(anc!==doc.body&&anc!==doc.documentElement){\n'
        '      var stA=getStyleSafe(anc);\n'
        '      if(stA&&stA.display!=="none"&&(stA.opacity==="0"||stA.visibility==="hidden")){\n'
        '        var arA=getRectSafe(anc);\n'
        '        if(arA&&arA.top>=vr.top+vr.height*0.42&&arA.height>=24&&arA.height<=280){\n'
        '          try{anc.setAttribute("data-neon-wake-bar","1");if(wakeBarEls.indexOf(anc)<0)wakeBarEls.push(anc);changed=true;}catch(e){}\n'
        '        }\n'
        '      }\n'
        '    }\n'
        '    if(anc.querySelectorAll){\n'
        '      try{\n'
        '        var kids=anc.querySelectorAll("div,section,footer,nav");\n'
        '        for(var ki=0;ki<kids.length&&ki<80;ki++){\n'
        '          var k=kids[ki];\n'
        '          if(!k||k===vid)continue;\n'
        '          var kr=getRectSafe(k);\n'
        '          if(!kr||kr.width<vr.width*0.20||kr.height<24||kr.height>280)continue;\n'
        '          if(kr.top<vr.top+vr.height*0.42||kr.bottom>vr.bottom+60)continue;\n'
        '          var kst=getStyleSafe(k);\n'
        '          if(!kst||kst.display==="none")continue;\n'
        '          if(kst.opacity!=="0"&&kst.visibility!=="hidden")continue;\n'
        '          var hasCtrl=false;\n'
        '          if(k.querySelectorAll){\n'
        '            var kBtns=k.querySelectorAll("button,[role=\\"button\\"],input:not([type=\\"hidden\\"]),[role=\\"slider\\"]");\n'
        '            if(kBtns&&kBtns.length)hasCtrl=true;\n'
        '          }\n'
        '          if(hasCtrl){\n'
        '            try{k.setAttribute("data-neon-wake-bar","1");if(wakeBarEls.indexOf(k)<0)wakeBarEls.push(k);changed=true;}catch(e){}\n'
        '          }\n'
        '        }\n'
        '      }catch(e){}\n'
        '    }\n'
        '    anc=anc.parentElement;\n'
        '  }\n'
        '  return changed;\n'
        '}\n'
        'function pickPlayerControlFocus(vid,dirY){\n'
        '  if(!vid)return null;\n'
        '  var vr=getRectSafe(vid);\n'
        '  if(!vr)return null;\n'
        '  var cands=collectCandidates();\n'
        '  var bestBtn=null,bestBtnSc=1e12;\n'
        '  var bestAny=null,bestAnySc=1e12;\n'
        '  var midY=vr.top+vr.height*0.45;\n'
        '  var midX=vr.left+vr.width/2;\n'
        '  for(var i=0;i<cands.length;i++){\n'
        '    var el=cands[i].el,r=cands[i].r;\n'
        '    if(!isSmallPlayerControl(el,vid))continue;\n'
        '    var cy=r.top+r.height/2,cx=r.left+r.width/2;\n'
        '    var inBot=(cy>=midY);\n'
        '    var zonePen=(dirY==="up")?(inBot?1000:0):(inBot?0:2000);\n'
        '    var sc=zonePen+Math.abs(cx-midX)*0.2+(dirY==="up"?cy:-cy);\n'
        '    if(!isSliderEl(el)){\n'
        '      if(sc<bestBtnSc){bestBtnSc=sc;bestBtn=el;}\n'
        '    }\n'
        '    if(sc<bestAnySc){bestAnySc=sc;bestAny=el;}\n'
        '  }\n'
        '  return bestBtn||bestAny;\n'
        '}\n'
        'function handleBack(){\n'
        '  if(cursorMode){\n'
        '    cursorMode=false;\n'
        '    if(cursorEl)cursorEl.style.display="none";\n'
        '    updateRing();\n'
        '    showToast("Режим фокуса (пульт)");\n'
        '    postToParent("neon-back-handled");\n'
        '    return;\n'
        '  }\n'
        '  if(document.activeElement&&isTextInput(document.activeElement)){\n'
        '    var curInp=document.activeElement;\n'
        '    currentFocusEl=curInp;\n'
        '    try{curInp.blur();}catch(e){}\n'
        '    updateRing();\n'
        '    postToParent("neon-back-handled");\n'
        '    return;\n'
        '  }\n'
        '  var dlg=activeDialogRoot();\n'
        '  if(dlg&&dlg!==(document.body||document.documentElement)){\n'
        '    var cls=null;\n'
        '    try{\n'
        '      var btns=dlg.querySelectorAll("button,[role=\\"button\\"],[data-close]");\n'
        '      for(var bi=0;bi<btns.length;bi++){\n'
        '        var b=btns[bi];\n'
        '        var al=String(b.getAttribute("aria-label")||"").toLowerCase();\n'
        '        if(hasAttr(b,"data-close")||al.indexOf("close")>=0||al.indexOf("закрыт")>=0){cls=b;break;}\n'
        '      }\n'
        '    }catch(e){}\n'
        '    if(cls){fireClick(cls);postToParent("neon-back-handled");return;}\n'
        '    try{document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",keyCode:27,which:27,bubbles:true}));}catch(e){}\n'
        '    postToParent("neon-back-handled");\n'
        '    return;\n'
        '  }\n'
        '  var fsEl=document.fullscreenElement||document.webkitFullscreenElement;\n'
        '  var vidBack=activeVideo(false);\n'
        '  var curHref=(location.pathname||"/")+(location.search||"");\n'
        '  var canBack=(spaDepth>0)||(virtualStack.length>0)||((window.history&&window.history.length>1)&&curHref!==entryHref);\n'
        '  var t2Depth=Number(window.__NEON_TIER2_DEPTH__)||0;\n'
        '  if(vidBack){\n'
        '    postToParent("neon-back-handled");\n'
        '    if(playerNavExplicit){\n'
        '      clearPlayerWakeTimers();\n'
        '      clearPlayerWakeBars();\n'
        '      playerNavExplicit=false;\n'
        '      currentFocusEl=null;\n'
        '      updateRing();\n'
        '      showToast("Панель скрыта • Назад ещё раз — выйти из плеера");\n'
        '      return;\n'
        '    }\n'
        '    if(fsEl){\n'
        '      try{if(document.exitFullscreen)document.exitFullscreen();else if(document.webkitExitFullscreen)document.webkitExitFullscreen();}catch(e){}\n'
        '      return;\n'
        '    }\n'
        '    if(canBack){\n'
        '      try{window.history.back();}catch(e){}\n'
        '      setTimeout(function(){scheduleRescan(120);},150);\n'
        '      return;\n'
        '    }\n'
        '    if(IS_FILE&&t2Depth>0){\n'
        '      postToParent("neon-back-tier2");\n'
        '      return;\n'
        '    }\n'
        '    var backBtn=null;\n'
        '    try{\n'
        '      var vwB=window.innerWidth||1920,vhB=window.innerHeight||1080;\n'
        '      var candsB=collectCandidates();\n'
        '      for(var bbi=0;bbi<candsB.length;bbi++){\n'
        '        var be=candsB[bbi].el,br=candsB[bbi].r;\n'
        '        var bTxt=(String(be.getAttribute&&be.getAttribute("aria-label")||"")+" "+String(be.getAttribute&&be.getAttribute("title")||"")+" "+String(be.textContent||"")).toLowerCase();\n'
        '        if(/\\bback\\b|назад|вернут|exit.*player|закрыт/i.test(bTxt)){\n'
        '          backBtn=be;break;\n'
        '        }\n'
        '        if(!backBtn&&br.left<vwB*0.18&&br.top<vhB*0.18&&br.width<140&&br.height<100&&tagOf(be)!=="A")backBtn=be;\n'
        '      }\n'
        '    }catch(e){}\n'
        '    if(backBtn){fireClick(backBtn);return;}\n'
        '    try{if(window.history&&typeof window.history.back==="function")window.history.back();}catch(e){}\n'
        '    var curPath=String(location.pathname||"/");\n'
        '    if(curPath!=="/"&&curPath!=="/ru"){\n'
        '      setTimeout(function(){\n'
        '        if(activeVideo(false)){\n'
        '          if(IS_FILE)postToParent("neon-navigate",{url:resolveForTarget("/")});\n'
        '          else try{location.assign("/");}catch(e){}\n'
        '        }\n'
        '      },220);\n'
        '    }\n'
        '    return;\n'
        '  }\n'
        '  if(fsEl){\n'
        '    try{if(document.exitFullscreen)document.exitFullscreen();else if(document.webkitExitFullscreen)document.webkitExitFullscreen();}catch(e){}\n'
        '    postToParent("neon-back-handled");\n'
        '    return;\n'
        '  }\n'
        '  if(canBack){\n'
        '    postToParent("neon-back-handled");\n'
        '    try{window.history.back();}catch(e){}\n'
        '    setTimeout(function(){scheduleRescan(120);},150);\n'
        '    return;\n'
        '  }\n'
        '  if(IS_FILE&&t2Depth>0){\n'
        '    postToParent("neon-back-handled");\n'
        '    postToParent("neon-back-tier2");\n'
        '    return;\n'
        '  }\n'
        '  var nowB=Date.now();\n'
        '  if(lastBackExitAt&&(nowB-lastBackExitAt)<220){\n'
        '    postToParent("neon-back-handled");\n'
        '    return;\n'
        '  }\n'
        '  if(lastBackExitAt&&(nowB-lastBackExitAt)<=3200){\n'
        '    lastBackExitAt=0;\n'
        '    exitNow();\n'
        '    return;\n'
        '  }\n'
        '  lastBackExitAt=nowB;\n'
        '  postToParent("neon-back-at-root");\n'
        '  showToast("Нажмите Назад ещё раз для выхода");\n'
        '}\n'
        'function updateCursorPos(){\n'
        '  ensureUi();\n'
        '  var vw=window.innerWidth||1920,vh=window.innerHeight||1080;\n'
        '  if(cursorX<4)cursorX=4;\n'
        '  if(cursorY<4)cursorY=4;\n'
        '  if(cursorX>vw-4)cursorX=vw-4;\n'
        '  if(cursorY>vh-4)cursorY=vh-4;\n'
        '  if(cursorEl){\n'
        '    cursorEl.style.display=cursorMode?"block":"none";\n'
        '    cursorEl.style.left=cursorX+"px";\n'
        '    cursorEl.style.top=cursorY+"px";\n'
        '  }\n'
        '}\n'
        'function fmtTime(sec){\n'
        '  var s=Math.max(0,Math.floor(Number(sec)||0));\n'
        '  var h=Math.floor(s/3600);\n'
        '  var m=Math.floor((s%3600)/60);\n'
        '  var r=s%60;\n'
        '  var mm=(h>0&&m<10)?("0"+m):String(m);\n'
        '  var ss=r<10?("0"+r):String(r);\n'
        '  return h>0?(h+":"+mm+":"+ss):(mm+":"+ss);\n'
        '}\n'
        'function activeVideo(allowAny){\n'
        '  if(!document.querySelectorAll)return null;\n'
        '  var vw=window.innerWidth||1920,vh=window.innerHeight||1080;\n'
        '  var fs=document.fullscreenElement||document.webkitFullscreenElement;\n'
        '  var list=[];\n'
        '  try{\n'
        '    var v0=document.querySelectorAll("video");\n'
        '    for(var i=0;i<v0.length;i++)list.push(v0[i]);\n'
        '  }catch(e){}\n'
        '  try{\n'
        '    var frms=document.querySelectorAll("iframe");\n'
        '    for(var fi=0;fi<frms.length;fi++){\n'
        '      try{\n'
        '        var fd=frms[fi].contentDocument||(frms[fi].contentWindow&&frms[fi].contentWindow.document);\n'
        '        if(fd&&fd.querySelectorAll){\n'
        '          var fv=fd.querySelectorAll("video");\n'
        '          for(var fvi=0;fvi<fv.length;fvi++)list.push(fv[fvi]);\n'
        '        }\n'
        '      }catch(e2){}\n'
        '    }\n'
        '  }catch(e){}\n'
        '  var best=null,bestArea=0;\n'
        '  for(var j=0;j<list.length;j++){\n'
        '    var v=list[j];\n'
        '    if(!v)continue;\n'
        '    var r=getRectSafe(v);\n'
        '    if(!r||r.width<240||r.height<140)continue;\n'
        '    var st=getStyleSafe(v);\n'
        '    if(st&&(st.display==="none"||st.visibility==="hidden"||st.opacity==="0"))continue;\n'
        '    var inFs=!!(fs&&(fs===v||(fs.contains&&fs.contains(v))));\n'
        '    if(!inFs&&(r.width<vw*0.50||r.height<vh*0.40))continue;\n'
        '    if(!inFs&&v.loop&&v.muted&&!hasAttr(v,"controls"))continue;\n'
        '    var dur=Number(v.duration);\n'
        '    var isWatchPath=/watch|player|movie|film|serial|episode/i.test(String(location.pathname||""));\n'
        '    if(!inFs&&v.muted&&(!isFinite(dur)||dur<60)&&!isWatchPath&&!hasAttr(v,"controls"))continue;\n'
        '    var area=r.width*r.height;\n'
        '    if(area>bestArea){bestArea=area;best=v;}\n'
        '  }\n'
        '  if(best||!allowAny)return best;\n'
        '  for(var k=0;k<list.length;k++){\n'
        '    if(list[k]&&!list[k].paused)return list[k];\n'
        '  }\n'
        '  if(list.length)return list[0];\n'
        '  try{\n'
        '    var ma=document.querySelectorAll("video,audio");\n'
        '    if(ma&&ma.length)return ma[0];\n'
        '  }catch(e){}\n'
        '  return null;\n'
        '}\n'
        'function wakePlayerControls(v){\n'
        '  if(!v)return;\n'
        '  var r=getRectSafe(v);\n'
        '  var doc=v.ownerDocument||document;\n'
        '  var win=(doc&&doc.defaultView)||window;\n'
        '  var vw=win.innerWidth||window.innerWidth||1920,vh=win.innerHeight||window.innerHeight||1080;\n'
        '  var cx=r?Math.round(r.left+r.width/2):Math.round(vw/2);\n'
        '  var cyMid=r?Math.round(r.top+r.height*0.5):Math.round(vh*0.5);\n'
        '  var cyBot=r?Math.round(r.top+r.height*0.86):Math.round(vh*0.86);\n'
        '  var dx=((seekSeq++%2)?13:-13);\n'
        '  var targets=[];\n'
        '  function addTarget(el){\n'
        '    if(!el||targets.indexOf(el)>=0)return;\n'
        '    if(el.id&&String(el.id).indexOf("neon-tv-")===0)return;\n'
        '    targets.push(el);\n'
        '  }\n'
        '  addTarget(v);\n'
        '  var pts=[[cx+dx,cyBot],[cx-dx,cyMid]];\n'
        '  for(var pi=0;pi<pts.length;pi++){\n'
        '    if(doc&&typeof doc.elementFromPoint==="function"){\n'
        '      try{\n'
        '        var hit=doc.elementFromPoint(pts[pi][0],pts[pi][1]);\n'
        '        var hc=hit;\n'
        '        for(var hi=0;hi<10&&hc;hi++){addTarget(hc);hc=hc.parentElement;}\n'
        '      }catch(e){}\n'
        '    }\n'
        '  }\n'
        '  var p=v.parentElement;\n'
        '  for(var ui=0;ui<10&&p;ui++){\n'
        '    addTarget(p);\n'
        '    if(p.className&&typeof p.className==="string"){\n'
        '      if(p.className.indexOf("vjs-user-inactive")>=0){\n'
        '        try{p.className=p.className.replace(/\\bvjs-user-inactive\\b/g,"vjs-user-active");}catch(e){}\n'
        '      }\n'
        '      if(p.className.indexOf("plyr--hide-controls")>=0){\n'
        '        try{p.className=p.className.replace(/\\bplyr--hide-controls\\b/g,"");}catch(e){}\n'
        '      }\n'
        '    }\n'
        '    p=p.parentElement;\n'
        '  }\n'
        '  addTarget(doc.body);\n'
        '  addTarget(doc.documentElement);\n'
        '  var o1={bubbles:true,cancelable:true,view:win,clientX:cx+dx,clientY:cyBot,screenX:cx+dx,screenY:cyBot,pointerId:1,pointerType:"mouse",isPrimary:true};\n'
        '  var o2={bubbles:true,cancelable:true,view:win,clientX:cx-dx,clientY:cyBot-6,screenX:cx-dx,screenY:cyBot-6,pointerId:1,pointerType:"mouse",isPrimary:true};\n'
        '  var MouseCtor=(win&&typeof win.MouseEvent==="function")?win.MouseEvent:MouseEvent;\n'
        '  var PtrCtor=(win&&typeof win.PointerEvent==="function")?win.PointerEvent:(typeof PointerEvent==="function"?PointerEvent:null);\n'
        '  var evTypes=["pointerover","pointerenter","pointermove","mouseover","mouseenter","mousemove"];\n'
        '  for(var ti=0;ti<targets.length;ti++){\n'
        '    var t=targets[ti];\n'
        '    for(var ei=0;ei<evTypes.length;ei++){\n'
        '      var et=evTypes[ei];\n'
        '      try{\n'
        '        if(et.indexOf("pointer")===0&&PtrCtor)t.dispatchEvent(new PtrCtor(et,o1));\n'
        '        else if(et.indexOf("mouse")===0)t.dispatchEvent(new MouseCtor(et,o1));\n'
        '      }catch(e){}\n'
        '    }\n'
        '  }\n'
        '  setTimeout(function(){\n'
        '    for(var tj=0;tj<targets.length;tj++){\n'
        '      try{targets[tj].dispatchEvent(new MouseCtor("mousemove",o2));}catch(e){}\n'
        '    }\n'
        '  },25);\n'
        '}\n'
        'function focusPlayerControlsDeferred(vid,dirY){\n'
        '  clearPlayerWakeTimers();\n'
        '  var ctrl0=pickPlayerControlFocus(vid,dirY);\n'
        '  if(ctrl0){focusEl(ctrl0,false);return true;}\n'
        '  var delays=[80,200,380];\n'
        '  for(var di=0;di<delays.length;di++){\n'
        '    (function(attempt){\n'
        '      var tm=setTimeout(function(){\n'
        '        var vNow=activeVideo(false);\n'
        '        if(!vNow||!playerNavExplicit)return;\n'
        '        var cNow=pickPlayerControlFocus(vNow,dirY);\n'
        '        if(!cNow&&attempt>=0){\n'
        '          if(forceRevealPlayerBar(vNow))cNow=pickPlayerControlFocus(vNow,dirY);\n'
        '        }\n'
        '        if(cNow){\n'
        '          if(!isSmallPlayerControl(currentFocusEl,vNow))focusEl(cNow,false);\n'
        '          clearPlayerWakeTimers();\n'
        '          return;\n'
        '        }\n'
        '        if(attempt===delays.length-1){\n'
        '          if(!moveFocus(dirY))ensureValidFocus();\n'
        '        }\n'
        '      },delays[attempt]);\n'
        '      playerWakeTimers.push(tm);\n'
        '    })(di);\n'
        '  }\n'
        '  return true;\n'
        '}\n'
        'function seekVideo(v,deltaSec){\n'
        '  if(!v)return false;\n'
        '  var now=Date.now();\n'
        '  var cur=Number(v.currentTime)||0;\n'
        '  var base=(v===seekLastVid&&(now-seekStreakAt)<2500)?seekTargetTime:cur;\n'
        '  var dur=Number(v.duration);\n'
        '  if((!isFinite(dur)||dur<=0)&&v.seekable&&v.seekable.length>0){\n'
        '    try{dur=Number(v.seekable.end(v.seekable.length-1));}catch(e){}\n'
        '  }\n'
        '  var next=base+deltaSec;\n'
        '  if(next<0)next=0;\n'
        '  if(isFinite(dur)&&dur>0&&next>dur)next=Math.max(0,dur-0.5);\n'
        '  try{\n'
        '    v.currentTime=next;\n'
        '    if(typeof v.fastSeek==="function")try{v.fastSeek(next);}catch(eFs){}\n'
        '  }catch(e){return false;}\n'
        '  if((now-seekStreakAt)<2500&&v===seekLastVid&&((deltaSec<0&&seekStreakTotal<0)||(deltaSec>0&&seekStreakTotal>0))){\n'
        '    seekStreakTotal+=deltaSec;\n'
        '  }else{\n'
        '    seekStreakTotal=deltaSec;\n'
        '  }\n'
        '  seekStreakAt=now;\n'
        '  seekTargetTime=next;\n'
        '  seekLastVid=v;\n'
        '  wakePlayerControls(v);\n'
        '  var prefix=(seekStreakTotal<0?"\\u23EA ":"\\u23E9 ")+(seekStreakTotal<0?("-"+Math.abs(seekStreakTotal)):("+"+seekStreakTotal))+"\\u0441";\n'
        '  var timeTxt=(isFinite(dur)&&dur>0)?(fmtTime(next)+" / "+fmtTime(dur)):fmtTime(next);\n'
        '  showToast(prefix+" \\u2022 "+timeTxt);\n'
        '  return true;\n'
        '}\n'
        'function stepSlider(el,dir,vid){\n'
        '  if(!el)return false;\n'
        '  var v=vid||activeVideo(false);\n'
        '  if(v&&isTimelineSlider(el,v))return seekVideo(v,dir>0?10:-10);\n'
        '  if(tagOf(el)==="INPUT"){\n'
        '    try{\n'
        '      var mn=parseFloat(el.min);if(!isFinite(mn))mn=0;\n'
        '      var mx=parseFloat(el.max);if(!isFinite(mx))mx=100;\n'
        '      var cur=parseFloat(el.value);if(!isFinite(cur))cur=mn;\n'
        '      var stp=parseFloat(el.step);\n'
        '      var delta=(isFinite(stp)&&stp>0)?stp:Math.max(0.05,(mx-mn)/20);\n'
        '      var nxt=Math.max(mn,Math.min(mx,cur+dir*delta));\n'
        '      var setter=null;\n'
        '      try{setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set;}catch(e){}\n'
        '      if(setter)setter.call(el,String(nxt));else el.value=String(nxt);\n'
        '      try{el.dispatchEvent(new Event("input",{bubbles:true}));}catch(e){}\n'
        '      try{el.dispatchEvent(new Event("change",{bubbles:true}));}catch(e){}\n'
        '      return true;\n'
        '    }catch(e){}\n'
        '  }\n'
        '  return false;\n'
        '}\n'
        'function handleMediaKey(code,keyStr){\n'
        '  var v=activeVideo(true);\n'
        '  if(!v)return false;\n'
        '  if(code===415||keyStr==="MediaPlay"){try{v.play();}catch(e){}wakePlayerControls(v);showToast("\\u25B6 Воспроизведение");return true;}\n'
        '  if(code===19||keyStr==="MediaPause"){try{v.pause();}catch(e){}wakePlayerControls(v);showToast("\\u23F8 Пауза");return true;}\n'
        '  if(code===10252||keyStr==="MediaPlayPause"){try{if(v.paused)v.play();else v.pause();}catch(e){}wakePlayerControls(v);showToast(v.paused?"\\u23F8 Пауза":"\\u25B6 Воспроизведение");return true;}\n'
        '  if(code===413||keyStr==="MediaStop"){try{v.pause();v.currentTime=0;}catch(e){}wakePlayerControls(v);return true;}\n'
        '  if(code===412||code===10232||keyStr==="MediaRewind"||keyStr==="MediaTrackPrevious"){return seekVideo(v,-10);}\n'
        '  if(code===417||code===10233||keyStr==="MediaFastForward"||keyStr==="MediaTrackNext"){return seekVideo(v,10);}\n'
        '  return false;\n'
        '}\n'
        'function handleRemoteKey(ev,isSynthetic){\n'
        '  var code=ev.keyCode||0;\n'
        '  var keyStr=ev.key||"";\n'
        '  function swallow(){\n'
        '    if(!isSynthetic&&ev.preventDefault)ev.preventDefault();\n'
        '    if(!isSynthetic&&ev.stopPropagation)ev.stopPropagation();\n'
        '  }\n'
        '  if(code===10182){swallow();exitNow();return true;}\n'
        '  if(code===10009||keyStr==="Back"||keyStr==="BrowserBack"){swallow();handleBack();return true;}\n'
        '  if(code===27||keyStr==="Escape"){swallow();handleBack();return true;}\n'
        '  if(code===406||code===405||code===457){\n'
        '    swallow();\n'
        '    cursorMode=!cursorMode;\n'
        '    updateCursorPos();\n'
        '    updateRing();\n'
        '    showToast(cursorMode?"Курсор включён (Синяя / Info — фокус)":"Режим фокуса (Синяя / Info — курсор)");\n'
        '    return true;\n'
        '  }\n'
        '  if(code===65376){\n'
        '    var ae=document.activeElement;\n'
        '    if(ae&&isTextInput(ae)){\n'
        '      currentFocusEl=ae;\n'
        '      if(ae.form&&String(ae.type||"").toLowerCase()==="search"){\n'
        '        var sub=ae.form.querySelector("button[type=\\"submit\\"],input[type=\\"submit\\"],button:not([type])");\n'
        '        if(sub&&!sub.disabled){fireClick(sub);}\n'
        '      }\n'
        '      try{ae.blur();}catch(e){}\n'
        '      updateRing();\n'
        '      swallow();\n'
        '      return true;\n'
        '    }\n'
        '  }\n'
        '  if(code===65385){\n'
        '    if(document.activeElement&&isTextInput(document.activeElement)){\n'
        '      currentFocusEl=document.activeElement;\n'
        '      try{document.activeElement.blur();}catch(e){}\n'
        '      updateRing();\n'
        '      swallow();\n'
        '      return true;\n'
        '    }\n'
        '  }\n'
        '  if(cursorMode){\n'
        '    var step=32;\n'
        '    if(code===37||keyStr==="ArrowLeft"){swallow();cursorX-=step;if(cursorX<=16)window.scrollBy(-120,0);updateCursorPos();return true;}\n'
        '    if(code===39||keyStr==="ArrowRight"){swallow();cursorX+=step;if(cursorX>=(window.innerWidth||1920)-16)window.scrollBy(120,0);updateCursorPos();return true;}\n'
        '    if(code===38||keyStr==="ArrowUp"){swallow();cursorY-=step;if(cursorY<=16)window.scrollBy(0,-160);updateCursorPos();return true;}\n'
        '    if(code===40||keyStr==="ArrowDown"){swallow();cursorY+=step;if(cursorY>=(window.innerHeight||1080)-16)window.scrollBy(0,160);updateCursorPos();return true;}\n'
        '    if(code===13||keyStr==="Enter"){\n'
        '      swallow();\n'
        '      var hit=(typeof document.elementFromPoint==="function")?document.elementFromPoint(cursorX,cursorY):null;\n'
        '      if(hit)fireClick(hit);\n'
        '      return true;\n'
        '    }\n'
        '  }\n'
        '  var aeInput=(document.activeElement&&isTextInput(document.activeElement))?document.activeElement:null;\n'
        '  if(aeInput){\n'
        '    var noSel=(typeof aeInput.selectionStart!=="number");\n'
        '    var valLen=String(aeInput.value||"").length;\n'
        '    var selStart=noSel?0:aeInput.selectionStart;\n'
        '    var selEnd=noSel?valLen:aeInput.selectionEnd;\n'
        '    if(code===38||keyStr==="ArrowUp"){swallow();currentFocusEl=aeInput;try{aeInput.blur();}catch(e){}moveFocus("up");return true;}\n'
        '    if(code===40||keyStr==="ArrowDown"){swallow();currentFocusEl=aeInput;try{aeInput.blur();}catch(e){}moveFocus("down");return true;}\n'
        '    if((code===37||keyStr==="ArrowLeft")&&(noSel||selStart===0)){swallow();currentFocusEl=aeInput;try{aeInput.blur();}catch(e){}moveFocus("left");return true;}\n'
        '    if((code===39||keyStr==="ArrowRight")&&(noSel||selEnd>=valLen)){swallow();currentFocusEl=aeInput;try{aeInput.blur();}catch(e){}moveFocus("right");return true;}\n'
        '    return false;\n'
        '  }\n'
        '  if(handleMediaKey(code,keyStr)){swallow();return true;}\n'
        '  if(code===8||keyStr==="Backspace"){swallow();handleBack();return true;}\n'
        '  var dlgRoot=activeDialogRoot();\n'
        '  var inDialog=!!(dlgRoot&&dlgRoot!==(document.body||document.documentElement));\n'
        '  var vid=!inDialog?activeVideo(false):null;\n'
        '  if(!vid){\n'
        '    if(playerNavExplicit){playerNavExplicit=false;clearPlayerWakeTimers();clearPlayerWakeBars();}\n'
        '  }\n'
        '  var onSmallCtrl=!!(vid&&isSmallPlayerControl(currentFocusEl,vid));\n'
        '  if(vid&&onSmallCtrl)playerNavExplicit=true;\n'
        '  if(vid&&playerNavExplicit&&(!inDoc(currentFocusEl)||!isElemVisible(currentFocusEl))){\n'
        '    playerNavExplicit=false;\n'
        '    clearPlayerWakeBars();\n'
        '  }\n'
        '  if(vid&&vid.ownerDocument&&vid.ownerDocument!==document&&vid.ownerDocument.defaultView&&vid.ownerDocument.defaultView!==window){\n'
        '    if((code===38||keyStr==="ArrowUp"||code===40||keyStr==="ArrowDown")||(playerNavExplicit&&(code===37||keyStr==="ArrowLeft"||code===39||keyStr==="ArrowRight"||code===13||keyStr==="Enter"))){\n'
        '      try{vid.ownerDocument.defaultView.postMessage({type:"neon-key",keyCode:code,key:keyStr},"*");}catch(e){}\n'
        '    }\n'
        '  }\n'
        '  if(code===37||keyStr==="ArrowLeft"||code===39||keyStr==="ArrowRight"){\n'
        '    var dirX=(code===37||keyStr==="ArrowLeft")?-1:1;\n'
        '    if(isSliderEl(currentFocusEl)&&stepSlider(currentFocusEl,dirX,vid)){swallow();return true;}\n'
        '    if(vid&&(playerNavExplicit||onSmallCtrl)){\n'
        '      if(!onSmallCtrl&&isPlayerArea(currentFocusEl,vid)){\n'
        '        var crF=currentFocusEl?getRectSafe(currentFocusEl):null;\n'
        '        var vrF=getRectSafe(vid);\n'
        '        if(!crF||(vrF&&crF.width>=vrF.width*0.5&&crF.height>=vrF.height*0.4)){\n'
        '          playerNavExplicit=false;\n'
        '          clearPlayerWakeBars();\n'
        '        }\n'
        '      }\n'
        '      if(playerNavExplicit||onSmallCtrl){\n'
        '        playerNavAt=Date.now();\n'
        '        resetPlayerInactivity();\n'
        '        wakePlayerControls(vid);\n'
        '        swallow();\n'
        '        moveFocus(dirX<0?"left":"right");\n'
        '        return true;\n'
        '      }\n'
        '    }\n'
        '    if(vid&&!playerNavExplicit&&!onSmallCtrl){swallow();seekVideo(vid,dirX*10);return true;}\n'
        '    swallow();moveFocus(dirX<0?"left":"right");return true;\n'
        '  }\n'
        '  if(code===40||keyStr==="ArrowDown"||code===38||keyStr==="ArrowUp"){\n'
        '    var dirY=(code===40||keyStr==="ArrowDown")?"down":"up";\n'
        '    if(vid){\n'
        '      wakePlayerControls(vid);\n'
        '      swallow();\n'
        '      if(!playerNavExplicit&&!onSmallCtrl){\n'
        '        playerNavExplicit=true;\n'
        '        playerNavAt=Date.now();\n'
        '        resetPlayerInactivity();\n'
        '        focusPlayerControlsDeferred(vid,dirY);\n'
        '        return true;\n'
        '      }\n'
        '      playerNavAt=Date.now();\n'
        '      resetPlayerInactivity();\n'
        '      var movedY=moveFocus(dirY);\n'
        '      if(movedY){\n'
        '        if(!isSmallPlayerControl(currentFocusEl,vid)&&isPlayerArea(currentFocusEl,vid)){\n'
        '          playerNavExplicit=false;\n'
        '          clearPlayerWakeBars();\n'
        '        }\n'
        '      }else if(dirY==="up"){\n'
        '        var cUp=pickPlayerControlFocus(vid,"up");\n'
        '        if(cUp&&cUp!==currentFocusEl)focusEl(cUp,false);\n'
        '      }\n'
        '      return true;\n'
        '    }\n'
        '    swallow();moveFocus(dirY);return true;\n'
        '  }\n'
        '  if(code===13||keyStr==="Enter"){\n'
        '    if(vid&&playerNavExplicit){playerNavAt=Date.now();wakePlayerControls(vid);resetPlayerInactivity();}\n'
        '    if(!inDoc(currentFocusEl)||!isElemVisible(currentFocusEl)){\n'
        '      var c0=collectCandidates();\n'
        '      var f0=pickInitial(c0);\n'
        '      if(f0)focusEl(f0,false);\n'
        '      else if(vid){\n'
        '        swallow();\n'
        '        try{if(vid.paused)vid.play();else vid.pause();}catch(e){}\n'
        '        wakePlayerControls(vid);\n'
        '        showToast(vid.paused?"\\u23F8 Пауза":"\\u25B6 Воспроизведение");\n'
        '        return true;\n'
        '      }\n'
        '    }\n'
        '    if(currentFocusEl){\n'
        '      swallow();\n'
        '      var bTxt=(String(currentFocusEl.getAttribute&&currentFocusEl.getAttribute("aria-label")||"")+" "+String(currentFocusEl.title||"")+" "+String(currentFocusEl.className||"")+" "+String(currentFocusEl.textContent||"")).toLowerCase();\n'
        '      if(vid&&/\\+10|10\\s*(?:с|c|s)\\b|\\bforward.*10\\b|\\bseek.*forward\\b|10\\s*сек/i.test(bTxt)){\n'
        '        seekVideo(vid,10);\n'
        '      }else if(vid&&/-(?:10|10\\s*(?:с|c|s)\\b)|\\brewind.*10\\b|\\bseek.*back/i.test(bTxt)){\n'
        '        seekVideo(vid,-10);\n'
        '      }\n'
        '      fireClick(currentFocusEl);\n'
        '      return true;\n'
        '    }\n'
        '  }\n'
        '  return false;\n'
        '}\n'
        'function applyTvPerfTweaks(){\n'
        '  try{\n'
        '    if(location.pathname.indexOf("/watch")<0){\n'
        '      var vids=document.querySelectorAll("video._3snskik");\n'
        '      for(var vi=0;vi<vids.length;vi++){\n'
        '        var bv=vids[vi];\n'
        '        if(bv&&!bv.controls&&bv.muted&&bv.id!=="hero-vid"){\n'
        '          try{bv.pause();}catch(eP){}\n'
        '          try{bv.removeAttribute("autoplay");}catch(eA){}\n'
        '        }\n'
        '      }\n'
        '    }\n'
        '  }catch(e){}\n'
        '}\n'
        'function ensureValidFocus(){\n'
        '  applyTvPerfTweaks();\n'
        '  hydratedReady=true;\n'
        '  if(document.activeElement&&isTextInput(document.activeElement)&&Date.now()>userAllowInputFocusUntil){\n'
        '    var aeAuto=document.activeElement;\n'
        '    try{aeAuto.blur();}catch(e){}\n'
        '    currentFocusEl=aeAuto;\n'
        '  }\n'
        '  var cands=collectCandidates();\n'
        '  if(!cands.length){updateRing();return;}\n'
        '  var dlgE=activeDialogRoot();\n'
        '  var inDlgE=!!(dlgE&&dlgE!==(document.body||document.documentElement));\n'
        '  var vidE=!inDlgE?activeVideo(false):null;\n'
        '  if(inDoc(currentFocusEl)){\n'
        '    for(var i=0;i<cands.length;i++){\n'
        '      if(cands[i].el===currentFocusEl){updateRing();return;}\n'
        '    }\n'
        '    if(vidE&&!isSmallPlayerControl(currentFocusEl,vidE)){playerNavExplicit=false;clearPlayerWakeBars();currentFocusEl=null;updateRing();return;}\n'
        '  }\n'
        '  if(vidE&&!playerNavExplicit&&(document.fullscreenElement||document.webkitFullscreenElement)){\n'
        '    updateRing();return;\n'
        '  }\n'
        '  var initEl=pickInitial(cands);\n'
        '  if(initEl)focusEl(initEl,false);\n'
        '}\n'
        'function scheduleRescan(delay){\n'
        '  if(scanTimer)clearTimeout(scanTimer);\n'
        '  scanTimer=setTimeout(ensureValidFocus,delay||160);\n'
        '}\n'
        'window.addEventListener("keydown",function(ev){handleRemoteKey(ev,false);},true);\n'
        'document.addEventListener("mousedown",function(){userAllowInputFocusUntil=Date.now()+900;},true);\n'
        'document.addEventListener("touchstart",function(){userAllowInputFocusUntil=Date.now()+900;},true);\n'
        'document.addEventListener("focusin",function(ev){\n'
        '  var t=ev&&ev.target;\n'
        '  if(!t||t.nodeType!==1)return;\n'
        '  if(isTextInput(t)){\n'
        '    if(Date.now()>userAllowInputFocusUntil){\n'
        '      try{t.blur();}catch(e){}\n'
        '      currentFocusEl=t;\n'
        '      hydratedReady=true;\n'
        '      try{var cn=String(t.className||"");if(cn.indexOf("neon-tv-focused")<0)t.className=(cn?cn+" ":"")+"neon-tv-focused";}catch(e){}\n'
        '      updateRing();\n'
        '      return;\n'
        '    }\n'
        '    currentFocusEl=t;\n'
        '    updateRing();\n'
        '  }\n'
        '},true);\n'
        'window.addEventListener("message",function(ev){\n'
        '  var d=ev&&ev.data;\n'
        '  if(!d)return;\n'
        '  if(d.type==="neon-key"){\n'
        '    handleRemoteKey({keyCode:d.keyCode||0,key:d.key||""},true);\n'
        '  }else if(d.type==="neon-ping"){\n'
        '    postToParent("neon-nav-ready",{href:String(location.href||"")});\n'
        '  }else if(d.type==="neon-xhr-res"&&d.id&&pendingXhr[d.id]){\n'
        '    var cb=pendingXhr[d.id];\n'
        '    delete pendingXhr[d.id];\n'
        '    cb(d);\n'
        '  }\n'
        '});\n'
        'if(IS_FILE){\n'
        '  (function(){\n'
        '    function makeMemStorage(initData,kind){\n'
        '      var store={};\n'
        '      if(initData&&typeof initData==="object"){\n'
        '        for(var ik in initData){\n'
        '          if(Object.prototype.hasOwnProperty.call(initData,ik))store[String(ik)]=String(initData[ik]);\n'
        '        }\n'
        '      }\n'
        '      var api={\n'
        '        getItem:function(k){return Object.prototype.hasOwnProperty.call(store,k)?store[k]:null;},\n'
        '        setItem:function(k,v){\n'
        '          var sk=String(k),sv=String(v);\n'
        '          store[sk]=sv;\n'
        '          postToParent("neon-storage",{kind:kind,op:"set",key:sk,val:sv});\n'
        '        },\n'
        '        removeItem:function(k){\n'
        '          var sk=String(k);\n'
        '          delete store[sk];\n'
        '          postToParent("neon-storage",{kind:kind,op:"remove",key:sk});\n'
        '        },\n'
        '        clear:function(){\n'
        '          store={};\n'
        '          postToParent("neon-storage",{kind:kind,op:"clear"});\n'
        '        },\n'
        '        key:function(idx){\n'
        '          var ks=[];\n'
        '          for(var p in store)if(Object.prototype.hasOwnProperty.call(store,p))ks.push(p);\n'
        '          return (idx>=0&&idx<ks.length)?ks[idx]:null;\n'
        '        }\n'
        '      };\n'
        '      try{\n'
        '        Object.defineProperty(api,"length",{get:function(){var n=0;for(var p in store)if(Object.prototype.hasOwnProperty.call(store,p))n++;return n;}});\n'
        '      }catch(e){}\n'
        '      return api;\n'
        '    }\n'
        '    try{if(window.localStorage)window.localStorage.getItem("__neon_t");}\n'
        '    catch(e){try{Object.defineProperty(window,"localStorage",{value:makeMemStorage(window.__NEON_INIT_LS__,"ls"),configurable:true});}catch(e2){}}\n'
        '    try{if(window.sessionStorage)window.sessionStorage.getItem("__neon_t");}\n'
        '    catch(e){try{Object.defineProperty(window,"sessionStorage",{value:makeMemStorage(window.__NEON_INIT_SS__,"ss"),configurable:true});}catch(e2){}}\n'
        '    var probeCk=false;\n'
        '    try{\n'
        '      var prevCk=document.cookie;\n'
        '      document.cookie="__neon_ck_probe=1; path=/";\n'
        '      if(String(document.cookie||"").indexOf("__neon_ck_probe=1")>=0){\n'
        '        probeCk=true;\n'
        '        document.cookie="__neon_ck_probe=; max-age=0; path=/";\n'
        '      }\n'
        '    }catch(e){probeCk=false;}\n'
        '    if(!probeCk){\n'
        '      var ckStore={};\n'
        '      var initCk=window.__NEON_INIT_CK__;\n'
        '      if(initCk&&typeof initCk==="object"){\n'
        '        for(var ckKey in initCk)if(Object.prototype.hasOwnProperty.call(initCk,ckKey))ckStore[ckKey]=String(initCk[ckKey]);\n'
        '      }\n'
        '      try{\n'
        '        Object.defineProperty(document,"cookie",{\n'
        '          configurable:true,\n'
        '          get:function(){\n'
        '            var out=[];\n'
        '            for(var k in ckStore)if(Object.prototype.hasOwnProperty.call(ckStore,k))out.push(k+"="+ckStore[k]);\n'
        '            return out.join("; ");\n'
        '          },\n'
        '          set:function(v){\n'
        '            var raw=String(v||"");\n'
        '            var first=raw.split(";")[0]||"";\n'
        '            var eq=first.indexOf("=");\n'
        '            if(eq<=0)return;\n'
        '            var k=first.slice(0,eq).replace(/^\\s+|\\s+$/g,"");\n'
        '            var val=first.slice(eq+1).replace(/^\\s+|\\s+$/g,"");\n'
        '            var isDel=/;\\s*max-age\\s*=\\s*-?0+(?:\\s*;|$)/i.test(raw)||/;\\s*max-age\\s*=\\s*-\\d+/i.test(raw)||/;\\s*expires\\s*=\\s*[^;]*(?:1970|1969)/i.test(raw);\n'
        '            if(!k)return;\n'
        '            if(isDel){delete ckStore[k];postToParent("neon-storage",{kind:"ck",op:"remove",key:k});}\n'
        '            else{ckStore[k]=val;postToParent("neon-storage",{kind:"ck",op:"set",key:k,val:val});}\n'
        '          }\n'
        '        });\n'
        '      }catch(e){}\n'
        '    }\n'
        '    try{\n'
        '      if(window.history){\n'
        '        var wrapHist=function(fnName){\n'
        '          var orig=window.history[fnName];\n'
        '          if(typeof orig!=="function")return;\n'
        '          window.history[fnName]=function(state,title,u){\n'
        '            try{\n'
        '              if(u!==undefined&&u!==null)TARGET_URL=resolveForTarget(String(u));\n'
        '              return orig.apply(window.history,arguments);\n'
        '            }catch(e){}\n'
        '          };\n'
        '        };\n'
        '        wrapHist("pushState");\n'
        '        wrapHist("replaceState");\n'
        '      }\n'
        '    }catch(e){}\n'
        '  })();\n'
        '  try{\n'
        '    if(typeof Location==="function"&&Location.prototype){\n'
        '      var wrapFileLoc=function(fnName){\n'
        '        var orig=Location.prototype[fnName];\n'
        '        if(typeof orig!=="function")return;\n'
        '        try{\n'
        '          Location.prototype[fnName]=function(u){\n'
        '            var r=resolveForTarget(u);\n'
        '            var hm=r.match(/^https?:\\/\\/([^\\/:?#]+)/i);\n'
        '            if(hm&&isTargetHost(hm[1])){postToParent("neon-navigate",{url:r});return;}\n'
        '            return orig.apply(this,arguments);\n'
        '          };\n'
        '        }catch(e){}\n'
        '      };\n'
        '      wrapFileLoc("assign");\n'
        '      wrapFileLoc("replace");\n'
        '    }\n'
        '  }catch(e){}\n'
        '  try{\n'
        '    if(window.navigation&&typeof window.navigation.addEventListener==="function"){\n'
        '      window.navigation.addEventListener("navigate",function(ev){\n'
        '        try{\n'
        '          var dest=ev&&ev.destination&&ev.destination.url?String(ev.destination.url):"";\n'
        '          if(!dest||dest.indexOf("about:")===0||dest.indexOf("blob:")===0||dest.indexOf("data:")===0)return;\n'
        '          var hm=dest.match(/^https?:\\/\\/([^\\/:?#]+)/i);\n'
        '          if(hm&&isTargetHost(hm[1])&&ev.cancelable!==false&&typeof ev.preventDefault==="function"){\n'
        '            ev.preventDefault();\n'
        '            postToParent("neon-navigate",{url:dest});\n'
        '          }\n'
        '        }catch(e){}\n'
        '      });\n'
        '    }\n'
        '  }catch(e){}\n'
        '  document.addEventListener("submit",function(ev){\n'
        '    var f=ev&&ev.target;\n'
        '    if(f&&f.method&&String(f.method).toUpperCase()==="GET"&&f.action){\n'
        '      var r=resolveForTarget(f.getAttribute("action")||f.action);\n'
        '      var hm=r.match(/^https?:\\/\\/([^\\/:?#]+)/i);\n'
        '      if(hm&&isTargetHost(hm[1])){\n'
        '        ev.preventDefault();\n'
        '        postToParent("neon-navigate",{url:r});\n'
        '      }\n'
        '    }\n'
        '  },true);\n'
        '}else if(IS_PROXY){\n'
        '  document.addEventListener("click",function(ev){\n'
        '    var t=ev&&ev.target;\n'
        '    while(t&&t.nodeType===1){\n'
        '      if(tagOf(t)==="A"&&hasAttr(t,"href")){\n'
        '        var raw=t.getAttribute("href")||"";\n'
        '        if(/^https?:\\/\\//i.test(raw)){\n'
        '          var rwPre=rewriteForProxy(raw);\n'
        '          if(rwPre!==raw){try{t.setAttribute("href",rwPre);}catch(e){}}\n'
        '        }\n'
        '        if(t.getAttribute("target")==="_blank"){try{t.removeAttribute("target");}catch(e){}}\n'
        '        break;\n'
        '      }\n'
        '      t=t.parentElement;\n'
        '    }\n'
        '  },true);\n'
        '  document.addEventListener("submit",function(ev){\n'
        '    var f=ev&&ev.target;\n'
        '    if(f&&f.action){\n'
        '      var rawAct=f.getAttribute("action")||f.action;\n'
        '      if(/^https?:\\/\\//i.test(rawAct)){\n'
        '        var rwAct=rewriteForProxy(rawAct);\n'
        '        if(rwAct!==rawAct){try{f.setAttribute("action",rwAct);}catch(e){}}\n'
        '      }\n'
        '    }\n'
        '  },true);\n'
        '}\n'
        'document.addEventListener("click",function(ev){\n'
        '  var t=ev&&ev.target;\n'
        '  while(t&&t.nodeType===1){\n'
        '    if(tagOf(t)==="A"&&hasAttr(t,"href")){\n'
        '      var rawHref=t.getAttribute("href")||"";\n'
        '      if(t.getAttribute("target")==="_blank")t.removeAttribute("target");\n'
        '      if(IS_PROXY&&/^https?:\\/\\//i.test(rawHref)){\n'
        '        var rw=rewriteForProxy(rawHref);\n'
        '        if(rw!==rawHref){\n'
        '          ev.preventDefault();\n'
        '          location.assign(rw);\n'
        '          return;\n'
        '        }\n'
        '      }else if(IS_FILE&&!ev.defaultPrevented&&rawHref.charAt(0)!=="#"){\n'
        '        var resolved=t.href||resolveForTarget(rawHref);\n'
        '        if(/^https?:\\/\\//i.test(resolved)&&isTargetHost((resolved.match(/^https?:\\/\\/([^\\/:?#]+)/i)||[])[1])){\n'
        '          ev.preventDefault();\n'
        '          postToParent("neon-navigate",{url:resolved});\n'
        '          return;\n'
        '        }\n'
        '      }\n'
        '      break;\n'
        '    }\n'
        '    t=t.parentElement;\n'
        '  }\n'
        '},false);\n'
        'window.addEventListener("scroll",updateRing,true);\n'
        'window.addEventListener("resize",updateRing);\n'
        'postToParent("neon-nav-ready",{href:String(location.href||"")});\n'
        'function bootNav(){\n'
        '  postToParent("neon-nav-ready",{href:String(location.href||"")});\n'
        '  scheduleRescan(240);\n'
        '  if(typeof MutationObserver==="function"&&(document.body||document.documentElement)){\n'
        '    try{\n'
        '      var mo=new MutationObserver(function(muts){\n'
        '        var onlyNeon=true;\n'
        '        if(muts&&muts.length){\n'
        '          for(var mi=0;mi<muts.length;mi++){\n'
        '            var m=muts[mi];\n'
        '            var tid=m&&m.target&&m.target.id?String(m.target.id):"";\n'
        '            if(tid.indexOf("neon-tv-")!==0){\n'
        '              var nodes=m&&m.addedNodes;\n'
        '              if(!nodes||!nodes.length){onlyNeon=false;break;}\n'
        '              for(var ni=0;ni<nodes.length;ni++){\n'
        '                var nid=nodes[ni]&&nodes[ni].id?String(nodes[ni].id):"";\n'
        '                if(nid.indexOf("neon-tv-")!==0){onlyNeon=false;break;}\n'
        '              }\n'
        '              if(!onlyNeon)break;\n'
        '            }\n'
        '          }\n'
        '        }else{onlyNeon=false;}\n'
        '        if(onlyNeon)return;\n'
        '        var vidMo=activeVideo(false);\n'
        '        if(vidMo&&!playerNavExplicit)return;\n'
        '        if(!inDoc(currentFocusEl)||!isElemVisible(currentFocusEl,getRectSafe(currentFocusEl))){\n'
        '          scheduleRescan(220);\n'
        '        }else{\n'
        '          updateRing();\n'
        '        }\n'
        '      });\n'
        '      mo.observe(document.body||document.documentElement,{childList:true,subtree:true});\n'
        '    }catch(e){}\n'
        '  }\n'
        '}\n'
        'if(document.readyState==="loading"){\n'
        '  document.addEventListener("DOMContentLoaded",bootNav);\n'
        '}else{\n'
        '  bootNav();\n'
        '}\n'
        '})();\n'
    )


def _build_belet_proxy_js(target_url=BELET_URL, nav_js=None):
    """Build single-file ES5 Node.js reverse proxy for Tizen 4..10 <tizen:service>."""
    if nav_js is None:
        nav_js = _build_belet_nav_js(target_url)
    safe_target = json.dumps(str(target_url or BELET_URL))
    safe_nav_js = json.dumps(nav_js)
    return (
        "(function(nodeRequire,hostModule,hostExports){\n"
        "'use strict';\n"
        "var reqFn=nodeRequire||(typeof require==='function'?require:null);\n"
        "if(!reqFn&&typeof process!=='undefined'){\n"
        "  try{if(typeof process.getBuiltinModule==='function'){var bm=process.getBuiltinModule('module');if(bm&&typeof bm.createRequire==='function')reqFn=bm.createRequire(process.cwd()+'/');}}catch(e){}\n"
        "  if(!reqFn){try{if(process.mainModule&&typeof process.mainModule.require==='function')reqFn=process.mainModule.require.bind(process.mainModule);}catch(e){}}\n"
        "  if(!reqFn){try{if(typeof process.getBuiltinModule==='function')reqFn=function(m){return process.getBuiltinModule(m);};}catch(e){}}\n"
        "}\n"
        "var http=reqFn('http');\n"
        "var https=reqFn('https');\n"
        "var url=reqFn('url');\n"
        "var zlib=reqFn('zlib');\n"
        "var os=reqFn('os');\n"
        "var fs=null,pathMod=null,netMod=null,tlsMod=null;\n"
        "try{fs=reqFn('fs');}catch(e){}\n"
        "try{pathMod=reqFn('path');}catch(e){}\n"
        "try{netMod=reqFn('net');}catch(e){}\n"
        "try{tlsMod=reqFn('tls');}catch(e){}\n"
        "var TARGET_URL=" + safe_target + ";\n"
        "var PORT=" + str(_BELET_PROXY_PORT) + ";\n"
        "var LISTEN_HOST='0.0.0.0';\n"
        "var NEON_NAV_JS=" + safe_nav_js + ";\n"
        "var PARSED=url.parse(TARGET_URL);\n"
        "var BASE_PROTOCOL=PARSED.protocol||'https:';\n"
        "var BASE_HOSTNAME=String(PARSED.hostname||'belet.tm').toLowerCase();\n"
        "var BASE_HOST=PARSED.host||BASE_HOSTNAME;\n"
        "var BASE_PORT=PARSED.port?parseInt(PARSED.port,10):(BASE_PROTOCOL==='https:'?443:80);\n"
        "var httpAgent=null,httpsAgent=null;\n"
        "if(http&&typeof http.Agent==='function'){\n"
        "  try{httpAgent=new http.Agent({keepAlive:true,maxSockets:16,keepAliveMsecs:15000});}catch(e){httpAgent=null;}\n"
        "}\n"
        "if(BASE_PROTOCOL==='https:'&&https&&typeof https.Agent==='function'){\n"
        "  try{httpsAgent=new https.Agent({keepAlive:true,maxSockets:16,keepAliveMsecs:15000,maxCachedSessions:32});}\n"
        "  catch(e){try{httpsAgent=new https.Agent({keepAlive:true,maxCachedSessions:0});}catch(e2){httpsAgent=null;}}\n"
        "}\n"
        "var cryptoMod=null;\n"
        "try{cryptoMod=reqFn('crypto');}catch(e){}\n"
        "var JAR_FILE='';\n"
        "var CACHE_DIR='';\n"
        "try{\n"
        "  if(PORT>0&&fs&&pathMod&&os&&typeof os.tmpdir==='function'){\n"
        "    JAR_FILE=pathMod.join(os.tmpdir(),'neon_belet_cookies_'+PORT+'.json');\n"
        "    CACHE_DIR=pathMod.join(os.tmpdir(),'neon_belet_cache_'+PORT);\n"
        "    if(fs.mkdirSync){try{fs.mkdirSync(CACHE_DIR,{recursive:true});}catch(eM){try{fs.mkdirSync(CACHE_DIR);}catch(eM2){}}}\n"
        "  }\n"
        "}catch(e){JAR_FILE='';CACHE_DIR='';}\n"
        "var jarEpoch=1;\n"
        "var STATIC_MAX_BYTES=16*1024*1024;\n"
        "var STATIC_ITEM_MAX=1536*1024;\n"
        "var DATA_MAX_BYTES=4*1024*1024;\n"
        "var DATA_ITEM_MAX=512*1024;\n"
        "var DATA_TTL_MS=180000;\n"
        "var HTML_TTL_MS=300000;\n"
        "var STATIC_TTL_MS=86400000;\n"
        "var staticMap={};var staticOrder=[];var staticBytes=0;\n"
        "var dataMap={};var dataOrder=[];var dataBytes=0;\n"
        "var inflightStatic={};\n"
        "function hashKey(s){\n"
        "  if(cryptoMod&&typeof cryptoMod.createHash==='function'){\n"
        "    try{return cryptoMod.createHash('md5').update(String(s)).digest('hex');}catch(e){}\n"
        "  }\n"
        "  var h=2166136261;\n"
        "  var str=String(s);\n"
        "  for(var i=0;i<str.length;i++){h^=str.charCodeAt(i);h=(h*16777619)>>>0;}\n"
        "  return h.toString(16)+'_'+str.length;\n"
        "}\n"
        "function isStaticPath(pathname,ctype){\n"
        "  var p=String(pathname||'').toLowerCase();\n"
        "  if(/\\.(?:m3u8|mpd|ts|m4s|mp4|m4v|m4a|webm|mkv|avi)(?:$|\\?)/.test(p))return false;\n"
        "  if(p.indexOf('/_next/static/')===0||p.indexOf('/_next/image')===0)return true;\n"
        "  if(/\\.(?:js|mjs|css|woff2?|ttf|otf|eot|svg|png|jpe?g|webp|avif|gif|ico)$/.test(p))return true;\n"
        "  var ct=String(ctype||'').toLowerCase();\n"
        "  if(ct.indexOf('text/css')===0||ct.indexOf('application/javascript')>=0||ct.indexOf('text/javascript')>=0||ct.indexOf('font/')===0||ct.indexOf('image/')===0)return true;\n"
        "  return false;\n"
        "}\n"
        "function isImmutableStatic(pathname){\n"
        "  var p=String(pathname||'').toLowerCase();\n"
        "  return p.indexOf('/_next/static/')===0||/\\.(?:woff2?|ttf|otf)$/.test(p);\n"
        "}\n"
        "function isHtmlCacheable(pathname){\n"
        "  var p=String(pathname||'').toLowerCase();\n"
        "  if(p.indexOf('/api/')===0||p.indexOf('/_next/')===0||p.indexOf('/__neon_')===0)return false;\n"
        "  if(/\\.(?:m3u8|mpd|ts|m4s|mp4|m4v|m4a|webm|mkv|avi|js|json|css|png|jpg|jpeg|gif|webp|svg|ico|woff2?|ttf|otf)(?:$|\\?)/.test(p))return false;\n"
        "  if(/\\/(?:auth|login|logout|register|otp|qr|session|token|user|profile|me|account|payment|subscribe)(?:\\/|$)/i.test(p))return false;\n"
        "  return true;\n"
        "}\n"
        "function isDataCacheable(pathname,ctype){\n"
        "  var p=String(pathname||'').toLowerCase();\n"
        "  if(/\\/(?:auth|login|logout|register|otp|qr|session|token|user|profile|me|account|payment|subscribe|watch|history)(?:\\/|$)/i.test(p))return false;\n"
        "  var ct=String(ctype||'').toLowerCase();\n"
        "  if(ct.indexOf('application/json')>=0||ct.indexOf('text/x-component')>=0)return true;\n"
        "  if(p.indexOf('/_next/data/')===0)return true;\n"
        "  return false;\n"
        "}\n"
        "function clearDataCache(){\n"
        "  jarEpoch++;\n"
        "  dataMap={};dataOrder=[];dataBytes=0;\n"
        "}\n"
        "function lruGet(map,order,key,ttlMs){\n"
        "  if(!Object.prototype.hasOwnProperty.call(map,key))return null;\n"
        "  var e=map[key];\n"
        "  if(!e||(ttlMs>0&&(Date.now()-e.ts)>ttlMs)){\n"
        "    delete map[key];\n"
        "    return null;\n"
        "  }\n"
        "  var idx=order.indexOf(key);\n"
        "  if(idx>=0)order.splice(idx,1);\n"
        "  order.push(key);\n"
        "  return e;\n"
        "}\n"
        "function lruSet(map,order,key,entry,maxBytes,isData){\n"
        "  var sz=entry&&entry.body?entry.body.length:0;\n"
        "  if(sz<=0)return;\n"
        "  if(Object.prototype.hasOwnProperty.call(map,key)){\n"
        "    var old=map[key];\n"
        "    if(old&&old.body){if(isData)dataBytes-=old.body.length;else staticBytes-=old.body.length;}\n"
        "    var oi=order.indexOf(key);\n"
        "    if(oi>=0)order.splice(oi,1);\n"
        "  }\n"
        "  map[key]=entry;\n"
        "  order.push(key);\n"
        "  if(isData)dataBytes+=sz;else staticBytes+=sz;\n"
        "  while((isData?dataBytes:staticBytes)>maxBytes&&order.length>1){\n"
        "    var k0=order.shift();\n"
        "    var rm=map[k0];\n"
        "    if(rm&&rm.body){if(isData)dataBytes-=rm.body.length;else staticBytes-=rm.body.length;}\n"
        "    delete map[k0];\n"
        "  }\n"
        "}\n"
        "function diskCacheRead(sKey){\n"
        "  if(!CACHE_DIR||!fs||typeof fs.readFileSync!=='function')return null;\n"
        "  var hk=hashKey(sKey);\n"
        "  var mf=pathMod.join(CACHE_DIR,hk+'.json');\n"
        "  var bf=pathMod.join(CACHE_DIR,hk+'.bin');\n"
        "  try{\n"
        "    var meta=JSON.parse(fs.readFileSync(mf,'utf8'));\n"
        "    if(!meta||(Date.now()-meta.ts)>STATIC_TTL_MS)return null;\n"
        "    var body=fs.readFileSync(bf);\n"
        "    return {body:body,headers:meta.headers||{},status:meta.status||200,etag:meta.etag||'',ts:meta.ts};\n"
        "  }catch(e){return null;}\n"
        "}\n"
        "function diskCacheWrite(sKey,entry){\n"
        "  if(!CACHE_DIR||!fs||typeof fs.writeFile!=='function')return;\n"
        "  var hk=hashKey(sKey);\n"
        "  var mf=pathMod.join(CACHE_DIR,hk+'.json');\n"
        "  var bf=pathMod.join(CACHE_DIR,hk+'.bin');\n"
        "  var meta=JSON.stringify({ts:entry.ts,status:entry.status,etag:entry.etag,headers:entry.headers});\n"
        "  try{fs.writeFile(bf,entry.body,function(err){if(!err)try{fs.writeFile(mf,meta,'utf8',function(){});}catch(e){}});}catch(e){}\n"
        "}\n"
        "var cookieJar=[];\n"
        "var jarSaveTimer=null;\n"
        "function loadJar(){\n"
        "  if(!JAR_FILE||!fs||typeof fs.readFileSync!=='function')return;\n"
        "  try{\n"
        "    var raw=fs.readFileSync(JAR_FILE,'utf8');\n"
        "    var arr=JSON.parse(raw);\n"
        "    if(Object.prototype.toString.call(arr)==='[object Array]'){\n"
        "      var now=Date.now();\n"
        "      for(var i=0;i<arr.length;i++){\n"
        "        var c=arr[i];\n"
        "        if(!c||!c.name||!c.domain)continue;\n"
        "        if(c.expiresMs!==null&&c.expiresMs!==undefined&&c.expiresMs<=now)continue;\n"
        "        cookieJar.push(c);\n"
        "      }\n"
        "    }\n"
        "  }catch(e){}\n"
        "}\n"
        "function scheduleSaveJar(){\n"
        "  if(!JAR_FILE||!fs)return;\n"
        "  try{\n"
        "    if(typeof fs.writeFileSync==='function'){\n"
        "      fs.writeFileSync(JAR_FILE,JSON.stringify(cookieJar),{encoding:'utf8',mode:384});\n"
        "      if(typeof fs.chmodSync==='function')fs.chmodSync(JAR_FILE,384);\n"
        "    }else if(typeof fs.writeFile==='function'){\n"
        "      fs.writeFile(JAR_FILE,JSON.stringify(cookieJar),{encoding:'utf8',mode:384},function(){});\n"
        "    }\n"
        "  }catch(e){}\n"
        "}\n"
        "loadJar();\n"
        "function defaultCookiePath(reqPath){\n"
        "  var p=String(reqPath||'/').split('?')[0]||'/';\n"
        "  if(p.charAt(0)!=='/')return '/';\n"
        "  var idx=p.lastIndexOf('/');\n"
        "  return idx<=0?'/':p.slice(0,idx);\n"
        "}\n"
        "function domainMatches(cookieDom,hostOnly,reqHost){\n"
        "  var h=String(reqHost||'').toLowerCase();\n"
        "  var d=String(cookieDom||'').toLowerCase();\n"
        "  if(!d||!h)return false;\n"
        "  if(hostOnly)return h===d;\n"
        "  return h===d||(h.length>d.length+1&&h.slice(-d.length-1)==='.'+d);\n"
        "}\n"
        "function pathMatches(cookiePath,reqPath){\n"
        "  var cp=String(cookiePath||'/');\n"
        "  var rp=String(reqPath||'/').split('?')[0]||'/';\n"
        "  if(cp==='/')return true;\n"
        "  if(rp===cp)return true;\n"
        "  if(rp.indexOf(cp)!==0)return false;\n"
        "  return cp.charAt(cp.length-1)==='/'||rp.charAt(cp.length)==='/';\n"
        "}\n"
        "function parseSetCookie(raw,reqHost,reqPath){\n"
        "  var s=String(raw||'');\n"
        "  var parts=s.split(';');\n"
        "  if(!parts.length)return null;\n"
        "  var nv=parts[0];\n"
        "  var eq=nv.indexOf('=');\n"
        "  if(eq<=0)return null;\n"
        "  var name=nv.slice(0,eq).replace(/^\\s+|\\s+$/g,'');\n"
        "  var val=nv.slice(eq+1).replace(/^\\s+|\\s+$/g,'');\n"
        "  if(!name)return null;\n"
        "  var domain=String(reqHost||BASE_HOSTNAME).toLowerCase();\n"
        "  var hostOnly=true;\n"
        "  var pathVal=defaultCookiePath(reqPath);\n"
        "  var expiresMs=null;\n"
        "  var hasMaxAge=false;\n"
        "  for(var i=1;i<parts.length;i++){\n"
        "    var seg=parts[i].replace(/^\\s+|\\s+$/g,'');\n"
        "    if(!seg)continue;\n"
        "    var aeq=seg.indexOf('=');\n"
        "    var ak=(aeq>=0?seg.slice(0,aeq):seg).replace(/^\\s+|\\s+$/g,'').toLowerCase();\n"
        "    var av=(aeq>=0?seg.slice(aeq+1):'').replace(/^\\s+|\\s+$/g,'');\n"
        "    if(ak==='domain'&&av){\n"
        "      var d=av.replace(/^\\./,'').toLowerCase();\n"
        "      if(d&&domainMatches(d,false,reqHost)){\n"
        "        domain=d;\n"
        "        hostOnly=false;\n"
        "      }\n"
        "    }else if(ak==='path'&&av&&av.charAt(0)==='/'){\n"
        "      pathVal=av;\n"
        "    }else if(ak==='max-age'&&av!==''){\n"
        "      var secs=parseInt(av,10);\n"
        "      if(!isNaN(secs)){\n"
        "        hasMaxAge=true;\n"
        "        expiresMs=(secs<=0)?0:(Date.now()+secs*1000);\n"
        "      }\n"
        "    }else if(ak==='expires'&&av&&!hasMaxAge){\n"
        "      var ms=Date.parse(av);\n"
        "      if(!isNaN(ms))expiresMs=ms;\n"
        "    }\n"
        "  }\n"
        "  return {name:name,value:val,domain:domain,hostOnly:hostOnly,path:pathVal,expiresMs:expiresMs};\n"
        "}\n"
        "function jarStore(scVal,reqHost,reqPath){\n"
        "  var list=(Object.prototype.toString.call(scVal)==='[object Array]')?scVal:[scVal];\n"
        "  var changed=false;\n"
        "  var now=Date.now();\n"
        "  for(var i=0;i<list.length;i++){\n"
        "    var item=parseSetCookie(list[i],reqHost,reqPath);\n"
        "    if(!item)continue;\n"
        "    for(var j=cookieJar.length-1;j>=0;j--){\n"
        "      var ex=cookieJar[j];\n"
        "      if(ex.name===item.name&&ex.domain===item.domain&&ex.path===item.path){\n"
        "        cookieJar.splice(j,1);\n"
        "        changed=true;\n"
        "      }\n"
        "    }\n"
        "    if(item.expiresMs===null||item.expiresMs>now){\n"
        "      cookieJar.push(item);\n"
        "      changed=true;\n"
        "    }\n"
        "  }\n"
        "  if(changed){clearDataCache();scheduleSaveJar();}\n"
        "}\n"
        "function buildUpstreamCookieHeader(reqHost,reqPath,browserCookieStr){\n"
        "  var now=Date.now();\n"
        "  var map={};\n"
        "  var order=[];\n"
        "  for(var i=cookieJar.length-1;i>=0;i--){\n"
        "    var e=cookieJar[i];\n"
        "    if(e.expiresMs!==null&&e.expiresMs<=now){cookieJar.splice(i,1);continue;}\n"
        "  }\n"
        "  for(var j=0;j<cookieJar.length;j++){\n"
        "    var c=cookieJar[j];\n"
        "    if(domainMatches(c.domain,c.hostOnly,reqHost)&&pathMatches(c.path,reqPath)){\n"
        "      if(!Object.prototype.hasOwnProperty.call(map,c.name))order.push(c.name);\n"
        "      map[c.name]=c.value;\n"
        "    }\n"
        "  }\n"
        "  if(browserCookieStr){\n"
        "    var pairs=String(browserCookieStr).split(';');\n"
        "    for(var k=0;k<pairs.length;k++){\n"
        "      var p=pairs[k];\n"
        "      var eq=p.indexOf('=');\n"
        "      if(eq<=0)continue;\n"
        "      var bn=p.slice(0,eq).replace(/^\\s+|\\s+$/g,'');\n"
        "      var bv=p.slice(eq+1).replace(/^\\s+|\\s+$/g,'');\n"
        "      if(!bn)continue;\n"
        "      if(!Object.prototype.hasOwnProperty.call(map,bn))order.push(bn);\n"
        "      map[bn]=bv;\n"
        "    }\n"
        "  }\n"
        "  if(!order.length)return '';\n"
        "  var out=[];\n"
        "  for(var oi=0;oi<order.length;oi++){\n"
        "    out.push(order[oi]+'='+map[order[oi]]);\n"
        "  }\n"
        "  return out.join('; ');\n"
        "}\n"
        "function bufFrom(data,enc){\n"
        "  if(typeof Buffer.from==='function'&&Buffer.from!==Uint8Array.from){\n"
        "    return enc?Buffer.from(data,enc):Buffer.from(data);\n"
        "  }\n"
        "  return enc?new Buffer(data,enc):new Buffer(data);\n"
        "}\n"
        "function isAllowedUpstreamHost(h){\n"
        "  var low=String(h||'').toLowerCase().replace(/:\\d+$/,'');\n"
        "  return low===BASE_HOSTNAME||(low.length>BASE_HOSTNAME.length+1&&low.slice(-BASE_HOSTNAME.length-1)==='.'+BASE_HOSTNAME);\n"
        "}\n"
        "function ownAddresses(){\n"
        "  var out=['127.0.0.1'],ifs;\n"
        "  try{ifs=os.networkInterfaces()||{};}catch(e){return out;}\n"
        "  for(var name in ifs){\n"
        "    if(!Object.prototype.hasOwnProperty.call(ifs,name))continue;\n"
        "    var list=ifs[name]||[];\n"
        "    for(var i=0;i<list.length;i++){\n"
        "      var a=list[i];\n"
        "      if(a&&a.address)out.push(String(a.address).replace(/^::ffff:/,'').replace(/%.*$/,''));\n"
        "    }\n"
        "  }\n"
        "  return out;\n"
        "}\n"
        "function fromThisTv(req){\n"
        "  var s=req&&(req.socket||req.connection);\n"
        "  var raw=s&&s.remoteAddress?String(s.remoteAddress):'';\n"
        "  if(!raw)return true;\n"
        "  var ip=raw.replace(/^::ffff:/,'').replace(/%.*$/,'');\n"
        "  if(ip==='::1'||/^127\\./.test(ip))return true;\n"
        "  return ownAddresses().indexOf(ip)>=0;\n"
        "}\n"
        "function setCors(res,reqOrigin,reqHeaders){\n"
        "  var safeOrig=(reqOrigin&&reqOrigin!=='null'&&reqOrigin!=='file://')?reqOrigin:'*';\n"
        "  res.setHeader('Access-Control-Allow-Origin',safeOrig);\n"
        "  if(safeOrig!=='*')res.setHeader('Access-Control-Allow-Credentials','true');\n"
        "  res.setHeader('Access-Control-Allow-Methods','GET,POST,PUT,PATCH,DELETE,OPTIONS,HEAD');\n"
        "  res.setHeader('Access-Control-Allow-Headers',reqHeaders||'Content-Type,Authorization,X-Requested-With,RSC,Next-Router-State-Tree,Next-Router-Prefetch,Next-Url');\n"
        "  res.setHeader('Access-Control-Allow-Private-Network','true');\n"
        "  res.setHeader('Cross-Origin-Resource-Policy','cross-origin');\n"
        "}\n"
        "function rewriteSetCookie(val){\n"
        "  var s=String(val||'');\n"
        "  s=s.replace(/;\\s*Secure\\b/gi,'');\n"
        "  s=s.replace(/;\\s*Domain=[^;]*/gi,'');\n"
        "  s=s.replace(/SameSite=None/gi,'SameSite=Lax');\n"
        "  return s;\n"
        "}\n"
        "function rewriteLocation(loc,proxyHost){\n"
        "  var raw=(Object.prototype.toString.call(loc)==='[object Array]')?loc[0]:loc;\n"
        "  var s=String(raw||'');\n"
        "  var m=s.match(/^(?:https?:)?\\/\\/([^\\/:?#]+)(?::(\\d+))?(\\/.*)?$/i);\n"
        "  if(!m)return s;\n"
        "  var h=m[1].toLowerCase();\n"
        "  var rest=m[3]||'/';\n"
        "  var pOrigin=proxyHost?('http://'+proxyHost):'';\n"
        "  if(h===BASE_HOSTNAME)return pOrigin+rest;\n"
        "  if(isAllowedUpstreamHost(h))return pOrigin+'/__neon_host/'+h+rest;\n"
        "  return s;\n"
        "}\n"
        "function rewriteRedirectHeader(val,proxyHost){\n"
        "  var raw=(Object.prototype.toString.call(val)==='[object Array]')?val[0]:val;\n"
        "  var s=String(raw||'');\n"
        "  var semi=s.indexOf(';');\n"
        "  if(semi>=0){\n"
        "    return rewriteLocation(s.slice(0,semi),proxyHost)+s.slice(semi);\n"
        "  }\n"
        "  return rewriteLocation(s,proxyHost);\n"
        "}\n"
        "function rewriteReferer(refUrl){\n"
        "  if(!refUrl)return '';\n"
        "  try{\n"
        "    var rp=url.parse(String(refUrl));\n"
        "    var p=rp.path||'/';\n"
        "    var hm=p.match(/^\\/__neon_host\\/([^\\/]+)(\\/.*)?$/);\n"
        "    if(hm&&isAllowedUpstreamHost(hm[1])){\n"
        "      return BASE_PROTOCOL+'//'+hm[1].toLowerCase()+(hm[2]||'/');\n"
        "    }\n"
        "    return BASE_PROTOCOL+'//'+BASE_HOST+(p.charAt(0)==='/'?p:'/'+p);\n"
        "  }catch(e){return BASE_PROTOCOL+'//'+BASE_HOST+'/';}\n"
        "}\n"
        "function escapeRx(str){return String(str).replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&');}\n"
        "var ORIGIN_ATTR_RX=new RegExp('(href|src|action|poster|data-src)=([\"\\'])(?:https?:)?\\\\/\\\\/((?:[a-z0-9-]+\\\\.)*'+escapeRx(BASE_HOSTNAME)+')(?::\\\\d+)?([\\\\/?#][^\"\\']*?)?\\\\2','gi');\n"
        "var ORIGIN_INLINE_RX=new RegExp('(?:https?:)?\\\\/\\\\/((?:[a-z0-9-]+\\\\.)*'+escapeRx(BASE_HOSTNAME)+')(?::\\\\d+)?([\\\\/?#][^\\\\s,\"\\'>]*)?','gi');\n"
        "var META_REFRESH_RX=new RegExp('(<meta[^>]*http-equiv\\\\s*=\\\\s*[\"\\']?refresh[\"\\']?[^>]*content\\\\s*=\\\\s*[\"\\'][^\"\\']*?url=)(?:https?:)?\\\\/\\\\/((?:[a-z0-9-]+\\\\.)*'+escapeRx(BASE_HOSTNAME)+')(?::\\\\d+)?([\\\\/?#][^\"\\';>]*)?','gi');\n"
        "function rewriteHtml(html){\n"
        "  var out=String(html||'');\n"
        "  out=out.replace(/<meta[^>]*http-equiv\\s*=\\s*[\"']?(?:Content-Security-Policy|Content-Security-Policy-Report-Only|X-WebKit-CSP|X-Content-Security-Policy|X-Frame-Options)[\"']?[^>]*>/gi,'');\n"
        "  out=out.replace(/<base\\b[^>]*>/gi,'');\n"
        "  out=out.replace(/\\s+integrity=([\"'])[^\"']*\\1/gi,'');\n"
        "  out=out.replace(/\\s+crossorigin(?:=([\"'])[^\"']*\\1)?/gi,'');\n"
        "  out=out.replace(META_REFRESH_RX,function(_m,pre,host,rest){\n"
        "    var p=rest?(rest.charAt(0)==='/'?rest:'/'+rest):'/';\n"
        "    if(String(host||'').toLowerCase()===BASE_HOSTNAME)return pre+p;\n"
        "    return pre+'/__neon_host/'+String(host).toLowerCase()+p;\n"
        "  });\n"
        "  out=out.replace(ORIGIN_ATTR_RX,function(_m,attr,q,host,rest){\n"
        "    var p=rest?(rest.charAt(0)==='/'?rest:'/'+rest):'/';\n"
        "    if(String(host||'').toLowerCase()===BASE_HOSTNAME)return attr+'='+q+p+q;\n"
        "    return attr+'='+q+'/__neon_host/'+String(host).toLowerCase()+p+q;\n"
        "  });\n"
        "  out=out.replace(/srcset=([\"'])([^\"']*)\\1/gi,function(_m,q,val){\n"
        "    var rw=String(val||'').replace(ORIGIN_INLINE_RX,function(_u,host,rest){\n"
        "      var p=rest?(rest.charAt(0)==='/'?rest:'/'+rest):'/';\n"
        "      if(String(host||'').toLowerCase()===BASE_HOSTNAME)return p;\n"
        "      return '/__neon_host/'+String(host).toLowerCase()+p;\n"
        "    });\n"
        "    return 'srcset='+q+rw+q;\n"
        "  });\n"
        "  var tag='<script src=\"/__neon_nav.js\" data-neon=\"1\"></script>';\n"
        "  if(/<head\\b[^>]*>/i.test(out)){\n"
        "    out=out.replace(/<head\\b[^>]*>/i,function(m){return m+tag;});\n"
        "  }else if(/<html\\b[^>]*>/i.test(out)){\n"
        "    out=out.replace(/<html\\b[^>]*>/i,function(m){return m+'<head>'+tag+'</head>';});\n"
        "  }else{\n"
        "    out=tag+out;\n"
        "  }\n"
        "  return out;\n"
        "}\n"
        "function decodeBody(buf,enc,cb){\n"
        "  var e=String(enc||'').toLowerCase();\n"
        "  if(e.indexOf('gzip')>=0){\n"
        "    zlib.gunzip(buf,function(err,res){cb(err?buf:res);});\n"
        "    return;\n"
        "  }\n"
        "  if(e.indexOf('deflate')>=0){\n"
        "    zlib.inflate(buf,function(err,res){\n"
        "      if(!err)return cb(res);\n"
        "      zlib.inflateRaw(buf,function(err2,res2){cb(err2?buf:res2);});\n"
        "    });\n"
        "    return;\n"
        "  }\n"
        "  if(e.indexOf('br')>=0&&typeof zlib.brotliDecompress==='function'){\n"
        "    zlib.brotliDecompress(buf,function(err,res){cb(err?buf:res);});\n"
        "    return;\n"
        "  }\n"
        "  cb(buf);\n"
        "}\n"
        "function handleRequest(req,res){\n"
        "  if(!fromThisTv(req)){\n"
        "    res.writeHead(403,{'Content-Type':'text/plain'});\n"
        "    res.end('Forbidden');\n"
        "    return;\n"
        "  }\n"
        "  var reqOrigin=req.headers&&req.headers.origin?String(req.headers.origin):'';\n"
        "  var reqHdrs=req.headers&&req.headers['access-control-request-headers']?String(req.headers['access-control-request-headers']):'';\n"
        "  setCors(res,reqOrigin,reqHdrs);\n"
        "  if(req.method==='OPTIONS'){\n"
        "    res.writeHead(204);\n"
        "    res.end();\n"
        "    return;\n"
        "  }\n"
        "  var parsedReq=url.parse(req.url||'/');\n"
        "  var pathname=parsedReq.pathname||'/';\n"
        "  if(pathname==='/__neon_ping'){\n"
        "    var ping=bufFrom(JSON.stringify({ok:true,app:'belet',ver:'1.2.7',port:PORT,node:(typeof process!=='undefined'&&process.version)?process.version:''}),'utf8');\n"
        "    res.writeHead(200,{'Content-Type':'application/json; charset=utf-8','Content-Length':ping.length,'Cache-Control':'no-store'});\n"
        "    res.end(ping);\n"
        "    return;\n"
        "  }\n"
        "  if(pathname==='/__neon_nav.js'){\n"
        "    var navBuf=bufFrom(NEON_NAV_JS,'utf8');\n"
        "    res.writeHead(200,{'Content-Type':'application/javascript; charset=utf-8','Content-Length':navBuf.length,'Cache-Control':'no-store'});\n"
        "    res.end(navBuf);\n"
        "    return;\n"
        "  }\n"
        "  if(pathname==='/__neon_cookie'&&req.method==='POST'){\n"
        "    var cChunks=[];\n"
        "    req.on('data',function(ch){cChunks.push(ch);});\n"
        "    req.on('end',function(){\n"
        "      try{\n"
        "        var rawB=Buffer.concat(cChunks).toString('utf8');\n"
        "        var parsedB=JSON.parse(rawB||'{}');\n"
        "        if(parsedB&&parsedB.cookie)jarStore(String(parsedB.cookie),BASE_HOSTNAME,'/');\n"
        "      }catch(e){}\n"
        "      res.writeHead(204,{'Cache-Control':'no-store'});\n"
        "      res.end();\n"
        "    });\n"
        "    return;\n"
        "  }\n"
        "  var upHost=BASE_HOSTNAME;\n"
        "  var upPort=BASE_PORT;\n"
        "  var upPath=req.url||'/';\n"
        "  var hm=pathname.match(/^\\/__neon_host\\/([^\\/]+)(\\/.*)?$/);\n"
        "  if(hm){\n"
        "    if(!isAllowedUpstreamHost(hm[1])){\n"
        "      res.writeHead(403,{'Content-Type':'text/plain'});\n"
        "      res.end('Disallowed host');\n"
        "      return;\n"
        "    }\n"
        "    upHost=hm[1].toLowerCase();\n"
        "    upPort=BASE_PROTOCOL==='https:'?443:80;\n"
        "    upPath=(hm[2]||'/')+(parsedReq.search||'');\n"
        "  }\n"
        "  var upPathname=(url.parse(upPath).pathname)||'/';\n"
        "  var inH=req.headers||{};\n"
        "  var reqMethod=String(req.method||'GET').toUpperCase();\n"
        "  if(reqMethod==='POST'||reqMethod==='PUT'||reqMethod==='PATCH'||reqMethod==='DELETE')clearDataCache();\n"
        "  function sendCachedEntry(targetRes,entry,orig,reqHdr){\n"
        "    if(!targetRes||targetRes.headersSent)return;\n"
        "    var outH={};\n"
        "    var eh=entry.headers||{};\n"
        "    for(var ek in eh)if(Object.prototype.hasOwnProperty.call(eh,ek))outH[ek]=eh[ek];\n"
        "    var safeOrig=(orig&&orig!=='null'&&orig!=='file://')?orig:'*';\n"
        "    outH['access-control-allow-origin']=safeOrig;\n"
        "    if(safeOrig!=='*')outH['access-control-allow-credentials']='true';else delete outH['access-control-allow-credentials'];\n"
        "    outH['access-control-allow-private-network']='true';\n"
        "    outH['cross-origin-resource-policy']='cross-origin';\n"
        "    outH['x-neon-cache']='HIT';\n"
        "    if(entry.etag)outH['etag']=entry.etag;\n"
        "    if(reqHdr&&reqHdr['if-none-match']&&entry.etag&&String(reqHdr['if-none-match']).indexOf(entry.etag)>=0){\n"
        "      delete outH['content-length'];\n"
        "      targetRes.writeHead(304,outH);\n"
        "      targetRes.end();\n"
        "      return;\n"
        "    }\n"
        "    outH['content-length']=entry.body.length;\n"
        "    targetRes.writeHead(entry.status||200,outH);\n"
        "    targetRes.end(entry.body);\n"
        "  }\n"
        "  var canStaticCache=(reqMethod==='GET'&&!inH.range&&!inH['if-range']&&!inH.authorization&&isStaticPath(upPathname,''));\n"
        "  var sKey=canStaticCache?(upHost+'|'+upPath):'';\n"
        "  var canHtmlCache=(reqMethod==='GET'&&!inH.range&&!inH['if-range']&&!inH.authorization&&isHtmlCacheable(upPathname));\n"
        "  var hKey=canHtmlCache?('html|'+jarEpoch+'|'+upHost+'|'+upPath):'';\n"
        "  var rscHdr=String(inH['rsc']||'')+'|'+String(inH['next-router-state-tree']||'');\n"
        "  var dKey=(reqMethod==='GET'&&!inH.range&&!inH['if-range']&&!inH.authorization)?(jarEpoch+'|'+upHost+'|'+upPath+'|'+rscHdr):'';\n"
        "  if(canStaticCache){\n"
        "    var sHit=lruGet(staticMap,staticOrder,sKey,STATIC_TTL_MS);\n"
        "    if(!sHit){\n"
        "      sHit=diskCacheRead(sKey);\n"
        "      if(sHit)lruSet(staticMap,staticOrder,sKey,sHit,STATIC_MAX_BYTES,false);\n"
        "    }\n"
        "    if(sHit){sendCachedEntry(res,sHit,reqOrigin,inH);return;}\n"
        "    if(inflightStatic[sKey]){\n"
        "      inflightStatic[sKey].push({res:res,orig:reqOrigin,inH:inH,fallback:function(){handleRequest(req,res);}});\n"
        "      return;\n"
        "    }\n"
        "    inflightStatic[sKey]=[];\n"
        "  }else if(hKey){\n"
        "    var hHit=lruGet(staticMap,staticOrder,hKey,HTML_TTL_MS);\n"
        "    if(!hHit){\n"
        "      hHit=diskCacheRead(hKey);\n"
        "      if(hHit)lruSet(staticMap,staticOrder,hKey,hHit,STATIC_MAX_BYTES,false);\n"
        "    }\n"
        "    if(hHit){sendCachedEntry(res,hHit,reqOrigin,inH);return;}\n"
        "  }else if(dKey){\n"
        "    var dHit=lruGet(dataMap,dataOrder,dKey,DATA_TTL_MS);\n"
        "    if(dHit){sendCachedEntry(res,dHit,reqOrigin,inH);return;}\n"
        "  }\n"
        "  var outHeaders={};\n"
        "  for(var k in inH){\n"
        "    if(!Object.prototype.hasOwnProperty.call(inH,k))continue;\n"
        "    var lk=k.toLowerCase();\n"
        "    if(lk==='host'||lk==='connection'||lk==='keep-alive'||lk==='proxy-connection'||lk==='origin'||lk==='referer'||lk==='cookie'||lk==='accept-encoding'||lk==='x-forwarded-host'||lk==='x-forwarded-proto'||lk==='x-forwarded-for'||lk==='sec-fetch-dest'||lk==='sec-fetch-mode'||lk==='sec-fetch-site'||lk==='sec-fetch-user'||lk==='upgrade-insecure-requests'||lk==='if-none-match'||lk==='if-modified-since')continue;\n"
        "    outHeaders[k]=inH[k];\n"
        "  }\n"
        "  var mergedCk=buildUpstreamCookieHeader(upHost,upPathname,inH.cookie||'');\n"
        "  if(mergedCk)outHeaders['cookie']=mergedCk;\n"
        "  var hostHdr=upHost;\n"
        "  if((BASE_PROTOCOL==='https:'&&upPort!==443)||(BASE_PROTOCOL==='http:'&&upPort!==80))hostHdr+=':'+upPort;\n"
        "  outHeaders['host']=hostHdr;\n"
        "  outHeaders['accept-encoding']='gzip, deflate';\n"
        "  if(inH.origin||(reqMethod!=='GET'&&reqMethod!=='HEAD'))outHeaders['origin']=BASE_PROTOCOL+'//'+BASE_HOST;\n"
        "  if(inH.referer)outHeaders['referer']=rewriteReferer(inH.referer);\n"
        "  var transport=(BASE_PROTOCOL==='https:')?https:http;\n"
        "  var reqOpts={\n"
        "    hostname:upHost,\n"
        "    port:upPort,\n"
        "    path:upPath,\n"
        "    method:reqMethod,\n"
        "    headers:outHeaders\n"
        "  };\n"
        "  if(BASE_PROTOCOL==='https:'){\n"
        "    reqOpts.rejectUnauthorized=false;\n"
        "    reqOpts.servername=upHost;\n"
        "    if(httpsAgent)reqOpts.agent=httpsAgent;\n"
        "  }else if(httpAgent){\n"
        "    reqOpts.agent=httpAgent;\n"
        "  }\n"
        "  var waitersFlushed=false;\n"
        "  function flushInflight(entry){\n"
        "    if(waitersFlushed||!sKey)return;\n"
        "    waitersFlushed=true;\n"
        "    var list=inflightStatic[sKey];\n"
        "    delete inflightStatic[sKey];\n"
        "    if(!list||!list.length)return;\n"
        "    for(var wi=0;wi<list.length;wi++){\n"
        "      var w=list[wi];\n"
        "      if(!w||!w.res||w.res.headersSent)continue;\n"
        "      if(entry){sendCachedEntry(w.res,entry,w.orig,w.inH);}\n"
        "      else{try{w.fallback();}catch(eW){}}\n"
        "    }\n"
        "  }\n"
        "  var aborted=false;\n"
        "  var upReq=transport.request(reqOpts,function(upRes){\n"
        "    if(aborted&&(!sKey||!inflightStatic[sKey]||!inflightStatic[sKey].length))return;\n"
        "    var respH={};\n"
        "    var upH=upRes.headers||{};\n"
        "    var hasSetCookie=false;\n"
        "    var stripH={\n"
        "      'content-security-policy':1,\n"
        "      'content-security-policy-report-only':1,\n"
        "      'x-webkit-csp':1,\n"
        "      'x-content-security-policy':1,\n"
        "      'x-frame-options':1,\n"
        "      'strict-transport-security':1,\n"
        "      'transfer-encoding':1,\n"
        "      'connection':1,\n"
        "      'keep-alive':1,\n"
        "      'proxy-connection':1,\n"
        "      'upgrade':1,\n"
        "      'te':1,\n"
        "      'trailer':1,\n"
        "      'cross-origin-resource-policy':1,\n"
        "      'cross-origin-embedder-policy':1,\n"
        "      'cross-origin-embedder-policy-report-only':1,\n"
        "      'cross-origin-opener-policy':1,\n"
        "      'cross-origin-opener-policy-report-only':1,\n"
        "      'origin-agent-cluster':1,\n"
        "      'permissions-policy':1,\n"
        "      'clear-site-data':1\n"
        "    };\n"
        "    for(var hk in upH){\n"
        "      if(!Object.prototype.hasOwnProperty.call(upH,hk))continue;\n"
        "      var lhk=hk.toLowerCase();\n"
        "      if(stripH[lhk])continue;\n"
        "      if(lhk==='set-cookie'){\n"
        "        hasSetCookie=true;\n"
        "        var sc=upH[hk];\n"
        "        jarStore(sc,upHost,upPathname);\n"
        "        if(Object.prototype.toString.call(sc)==='[object Array]'){\n"
        "          var mapped=[];\n"
        "          for(var sci=0;sci<sc.length;sci++)mapped.push(rewriteSetCookie(sc[sci]));\n"
        "          respH['set-cookie']=mapped;\n"
        "        }else{\n"
        "          respH['set-cookie']=rewriteSetCookie(sc);\n"
        "        }\n"
        "      }else if(lhk==='location'||lhk==='x-nextjs-redirect'||lhk==='x-action-redirect'){\n"
        "        respH[lhk]=rewriteRedirectHeader(upH[hk],inH.host||'');\n"
        "      }else{\n"
        "        respH[hk]=upH[hk];\n"
        "      }\n"
        "    }\n"
        "    var safeRespOrig=(reqOrigin&&reqOrigin!=='null'&&reqOrigin!=='file://')?reqOrigin:'*';\n"
        "    respH['access-control-allow-origin']=safeRespOrig;\n"
        "    if(safeRespOrig!=='*')respH['access-control-allow-credentials']='true';\n"
        "    respH['access-control-allow-private-network']='true';\n"
        "    respH['cross-origin-resource-policy']='cross-origin';\n"
        "    var ctype=String(upH['content-type']||'');\n"
        "    var status=upRes.statusCode||200;\n"
        "    var isSse=/text\\/event-stream/i.test(ctype);\n"
        "    if(isSse){\n"
        "      try{upReq.setTimeout(0);}catch(e){}\n"
        "      try{if(res.setTimeout)res.setTimeout(0);}catch(e){}\n"
        "    }\n"
        "    var isHtml=/text\\/html|application\\/xhtml\\+xml/i.test(ctype)&&status!==204&&status!==304&&reqMethod!=='HEAD';\n"
        "    if(!isHtml){\n"
        "      var upCc=String(upH['cache-control']||'');\n"
        "      var wantStaticStore=(status===200&&!hasSetCookie&&!isSse&&reqMethod==='GET'&&!inH.range&&!inH['if-range']&&!inH.authorization&&isStaticPath(upPathname,ctype));\n"
        "      var wantDataStore=(!wantStaticStore&&status===200&&!hasSetCookie&&!isSse&&reqMethod==='GET'&&!inH.range&&!inH['if-range']&&!inH.authorization&&!/(?:no-store|private)/i.test(upCc)&&isDataCacheable(upPathname,ctype));\n"
        "      if((wantStaticStore||wantDataStore)&&status===200){\n"
        "        var maxBuf=wantStaticStore?STATIC_ITEM_MAX:DATA_ITEM_MAX;\n"
        "        var cLen=parseInt(upH['content-length']||'-1',10);\n"
        "        if(!isNaN(cLen)&&cLen>maxBuf){\n"
        "          flushInflight(null);\n"
        "          if(!aborted&&!res.headersSent){res.writeHead(status,respH);upRes.pipe(res);}else{upRes.resume();}\n"
        "          return;\n"
        "        }\n"
        "        var skStore=sKey||(upHost+'|'+upPath);\n"
        "        if(wantStaticStore){\n"
        "          delete respH['pragma'];delete respH['expires'];\n"
        "          respH['cache-control']=isImmutableStatic(upPathname)?'public, max-age=604800, immutable':'public, max-age=3600';\n"
        "          if(!respH['etag'])respH['etag']='\"neon-'+hashKey(skStore)+'\"';\n"
        "        }\n"
        "        var cBufs=[],cTotal=0,cOverflow=false;\n"
        "        upRes.on('data',function(ch){\n"
        "          if(cOverflow){if(!aborted&&res.headersSent)res.write(ch);return;}\n"
        "          cTotal+=ch.length;\n"
        "          if(cTotal>maxBuf){\n"
        "            cOverflow=true;\n"
        "            flushInflight(null);\n"
        "            if(!aborted&&!res.headersSent){\n"
        "              res.writeHead(status,respH);\n"
        "              for(var bi=0;bi<cBufs.length;bi++)res.write(cBufs[bi]);\n"
        "              res.write(ch);\n"
        "            }\n"
        "            cBufs=null;\n"
        "            return;\n"
        "          }\n"
        "          cBufs.push(ch);\n"
        "        });\n"
        "        upRes.on('end',function(){\n"
        "          if(cOverflow){if(!aborted&&res.headersSent)res.end();return;}\n"
        "          var fullBuf=Buffer.concat(cBufs);\n"
        "          var cacheH={};\n"
        "          for(var chK in respH)if(Object.prototype.hasOwnProperty.call(respH,chK))cacheH[chK]=respH[chK];\n"
        "          var entry={body:fullBuf,headers:cacheH,status:status,etag:cacheH['etag']||'',ts:Date.now()};\n"
        "          if(wantStaticStore){\n"
        "            lruSet(staticMap,staticOrder,skStore,entry,STATIC_MAX_BYTES,false);\n"
        "            diskCacheWrite(skStore,entry);\n"
        "            flushInflight(entry);\n"
        "          }else if(wantDataStore&&dKey){\n"
        "            lruSet(dataMap,dataOrder,dKey,entry,DATA_MAX_BYTES,true);\n"
        "          }\n"
        "          if(!aborted&&!res.headersSent){\n"
        "            respH['content-length']=fullBuf.length;\n"
        "            respH['x-neon-cache']='MISS';\n"
        "            if(inH['if-none-match']&&entry.etag&&String(inH['if-none-match']).indexOf(entry.etag)>=0){\n"
        "              delete respH['content-length'];\n"
        "              res.writeHead(304,respH);\n"
        "              res.end();\n"
        "            }else{\n"
        "              res.writeHead(status,respH);\n"
        "              res.end(fullBuf);\n"
        "            }\n"
        "          }\n"
        "        });\n"
        "        return;\n"
        "      }\n"
        "      flushInflight(null);\n"
        "      res.writeHead(status,respH);\n"
        "      if(reqMethod==='HEAD'||status===204||status===304){upRes.resume();res.end();return;}\n"
        "      upRes.pipe(res);\n"
        "      return;\n"
        "    }\n"
        "    flushInflight(null);\n"
        "    var chunks=[],total=0,overflow=false;\n"
        "    upRes.on('data',function(ch){\n"
        "      total+=ch.length;\n"
        "      if(total>8*1024*1024){overflow=true;return;}\n"
        "      chunks.push(ch);\n"
        "    });\n"
        "    upRes.on('end',function(){\n"
        "      if(aborted)return;\n"
        "      var rawBuf=Buffer.concat(chunks);\n"
        "      if(overflow){\n"
        "        res.writeHead(status,respH);\n"
        "        res.end(rawBuf);\n"
        "        return;\n"
        "      }\n"
        "      decodeBody(rawBuf,upH['content-encoding'],function(plainBuf){\n"
        "        var htmlOut=rewriteHtml(plainBuf.toString('utf8'));\n"
        "        var outBuf=bufFrom(htmlOut,'utf8');\n"
        "        delete respH['content-encoding'];\n"
        "        delete respH['Content-Encoding'];\n"
        "        respH['content-type']='text/html; charset=utf-8';\n"
        "        respH['content-length']=outBuf.length;\n"
        "        if(status===200&&hKey&&!hasSetCookie&&outBuf.length<STATIC_ITEM_MAX){\n"
        "          respH['cache-control']='public, max-age=300';\n"
        "          if(!respH['etag'])respH['etag']='\"neon-html-'+hashKey(hKey)+'\"';\n"
        "          var hEntry={body:outBuf,headers:respH,status:status,etag:respH['etag'],ts:Date.now()};\n"
        "          lruSet(staticMap,staticOrder,hKey,hEntry,STATIC_MAX_BYTES,false);\n"
        "          diskCacheWrite(hKey,hEntry);\n"
        "        }\n"
        "        res.writeHead(status,respH);\n"
        "        res.end(outBuf);\n"
        "      });\n"
        "    });\n"
        "  });\n"
        "  upReq.setTimeout(60000,function(){flushInflight(null);try{upReq.abort();}catch(e){}});\n"
        "  upReq.on('error',function(err){\n"
        "    flushInflight(null);\n"
        "    if(res.headersSent)return;\n"
        "    var msg=err&&err.message?err.message:'Upstream error';\n"
        "    var errHtml='<!DOCTYPE html><html><body style=\"background:#070b12;color:#eaf4ff\"><script>'+\n"
        "      'try{if(window.parent&&window.parent!==window)window.parent.postMessage({type:\"neon-proxy-upstream-error\",error:'+JSON.stringify(msg)+'},\"*\");}catch(e){}'+\n"
        "      '</script></body></html>';\n"
        "    var b=bufFrom(errHtml,'utf8');\n"
        "    res.writeHead(502,{'Content-Type':'text/html; charset=utf-8','Content-Length':b.length});\n"
        "    res.end(b);\n"
        "  });\n"
        "  res.on('close',function(){if(!res.finished){aborted=true;if(!sKey||!inflightStatic[sKey]||!inflightStatic[sKey].length){flushInflight(null);try{upReq.abort();}catch(e){}}}});\n"
        "  if(reqMethod==='GET'||reqMethod==='HEAD'){upReq.end();}else{req.pipe(upReq);}\n"
        "}\n"
        "function handleUpgrade(req,clientSocket,head){\n"
        "  if(!fromThisTv(req)){try{clientSocket.destroy();}catch(e){}return;}\n"
        "  var sockMod=(BASE_PROTOCOL==='https:')?tlsMod:netMod;\n"
        "  if(!sockMod){try{clientSocket.destroy();}catch(e){}return;}\n"
        "  var parsedReq=url.parse(req.url||'/');\n"
        "  var pathname=parsedReq.pathname||'/';\n"
        "  var upHost=BASE_HOSTNAME;\n"
        "  var upPort=BASE_PORT;\n"
        "  var upPath=req.url||'/';\n"
        "  var hm=pathname.match(/^\\/__neon_host\\/([^\\/]+)(\\/.*)?$/);\n"
        "  if(hm){\n"
        "    if(!isAllowedUpstreamHost(hm[1])){try{clientSocket.destroy();}catch(e){}return;}\n"
        "    upHost=hm[1].toLowerCase();\n"
        "    upPort=BASE_PROTOCOL==='https:'?443:80;\n"
        "    upPath=(hm[2]||'/')+(parsedReq.search||'');\n"
        "  }\n"
        "  var upPathname=(url.parse(upPath).pathname)||'/';\n"
        "  var inH=req.headers||{};\n"
        "  var hostHdr=upHost;\n"
        "  if((BASE_PROTOCOL==='https:'&&upPort!==443)||(BASE_PROTOCOL==='http:'&&upPort!==80))hostHdr+=':'+upPort;\n"
        "  var lines=['GET '+upPath+' HTTP/1.1','Host: '+hostHdr];\n"
        "  var mergedCk=buildUpstreamCookieHeader(upHost,upPathname,inH.cookie||'');\n"
        "  if(mergedCk)lines.push('Cookie: '+mergedCk);\n"
        "  lines.push('Origin: '+BASE_PROTOCOL+'//'+BASE_HOST);\n"
        "  for(var k in inH){\n"
        "    if(!Object.prototype.hasOwnProperty.call(inH,k))continue;\n"
        "    var lk=k.toLowerCase();\n"
        "    if(lk==='host'||lk==='origin'||lk==='cookie'||lk==='referer')continue;\n"
        "    lines.push(k+': '+inH[k]);\n"
        "  }\n"
        "  var connOpts={host:upHost,port:upPort};\n"
        "  if(BASE_PROTOCOL==='https:'){connOpts.servername=upHost;connOpts.rejectUnauthorized=false;}\n"
        "  var upSocket;\n"
        "  try{upSocket=sockMod.connect(connOpts);}catch(e){try{clientSocket.destroy();}catch(e2){}return;}\n"
        "  upSocket.on('error',function(){try{clientSocket.destroy();}catch(e){}});\n"
        "  upSocket.on('end',function(){try{clientSocket.destroy();}catch(e){}});\n"
        "  upSocket.on('close',function(){try{clientSocket.destroy();}catch(e){}});\n"
        "  clientSocket.on('error',function(){try{upSocket.destroy();}catch(e){}});\n"
        "  clientSocket.on('end',function(){try{upSocket.destroy();}catch(e){}});\n"
        "  clientSocket.on('close',function(){try{upSocket.destroy();}catch(e){}});\n"
        "  var hsDone=false,hsBuf=bufFrom('');\n"
        "  upSocket.on('data',function onUpData(chunk){\n"
        "    if(hsDone){clientSocket.write(chunk);return;}\n"
        "    hsBuf=Buffer.concat([hsBuf,chunk]);\n"
        "    var idx=hsBuf.indexOf('\\r\\n\\r\\n');\n"
        "    if(idx<0){\n"
        "      if(hsBuf.length>32768){hsDone=true;clientSocket.write(hsBuf);hsBuf=null;}\n"
        "      return;\n"
        "    }\n"
        "    hsDone=true;\n"
        "    var hdrText=hsBuf.slice(0,idx).toString('latin1');\n"
        "    var tailBuf=hsBuf.slice(idx+4);\n"
        "    hsBuf=null;\n"
        "    var rawLines=hdrText.split('\\r\\n');\n"
        "    var outLines=[rawLines[0]];\n"
        "    for(var li=1;li<rawLines.length;li++){\n"
        "      var ln=rawLines[li];\n"
        "      if(/^set-cookie\\s*:/i.test(ln)){\n"
        "        var scVal=ln.replace(/^set-cookie\\s*:\\s*/i,'');\n"
        "        jarStore(scVal,upHost,upPathname);\n"
        "        outLines.push('Set-Cookie: '+rewriteSetCookie(scVal));\n"
        "      }else{\n"
        "        outLines.push(ln);\n"
        "      }\n"
        "    }\n"
        "    clientSocket.write(bufFrom(outLines.join('\\r\\n')+'\\r\\n\\r\\n','latin1'));\n"
        "    if(tailBuf.length)clientSocket.write(tailBuf);\n"
        "    upSocket.removeListener('data',onUpData);\n"
        "    upSocket.pipe(clientSocket);\n"
        "    clientSocket.pipe(upSocket);\n"
        "  });\n"
        "  upSocket.once('connect',function(){\n"
        "    try{\n"
        "      upSocket.write(bufFrom(lines.join('\\r\\n')+'\\r\\n\\r\\n','latin1'));\n"
        "      if(head&&head.length)upSocket.write(head);\n"
        "    }catch(e){try{clientSocket.destroy();}catch(e2){}}\n"
        "  });\n"
        "}\n"
        "var server=http.createServer(handleRequest);\n"
        "server.on('upgrade',handleUpgrade);\n"
        "var serverStarted=false;\n"
        "var listenRetries=0;\n"
        "server.on('error',function(err){\n"
        "  serverStarted=false;\n"
        "  if(err&&err.code==='EADDRINUSE'&&listenRetries<25){\n"
        "    listenRetries++;\n"
        "    setTimeout(startServer,350);\n"
        "  }\n"
        "});\n"
        "server.on('close',function(){serverStarted=false;});\n"
        "function startServer(){\n"
        "  if(serverStarted)return server;\n"
        "  serverStarted=true;\n"
        "  try{\n"
        "    server.listen(PORT,LISTEN_HOST);\n"
        "  }catch(e){serverStarted=false;}\n"
        "  return server;\n"
        "}\n"
        "startServer();\n"
        "if(typeof process!=='undefined'&&process.on){\n"
        "  process.on('uncaughtException',function(){});\n"
        "}\n"
        "var onStart=function(){startServer();};\n"
        "var onRequest=function(){startServer();};\n"
        "var onStop=function(){try{if(httpAgent&&httpAgent.destroy)httpAgent.destroy();if(httpsAgent&&httpsAgent.destroy)httpsAgent.destroy();if(typeof server.closeAllConnections==='function')server.closeAllConnections();server.close();serverStarted=false;}catch(e){}};\n"
        "var onExit=function(){try{if(httpAgent&&httpAgent.destroy)httpAgent.destroy();if(httpsAgent&&httpsAgent.destroy)httpsAgent.destroy();if(typeof server.closeAllConnections==='function')server.closeAllConnections();server.close();serverStarted=false;}catch(e){}};\n"
        "var expObj=(hostModule&&hostModule.exports)?hostModule.exports:(hostExports||null);\n"
        "if(expObj){\n"
        "  expObj.server=server;\n"
        "  expObj.PORT=PORT;\n"
        "  expObj.LISTEN_HOST=LISTEN_HOST;\n"
        "  expObj.startServer=startServer;\n"
        "  expObj.onStart=onStart;\n"
        "  expObj.onRequest=onRequest;\n"
        "  expObj.onStop=onStop;\n"
        "  expObj.onExit=onExit;\n"
        "}else if(typeof global==='object'&&global){\n"
        "  global.onStart=onStart;\n"
        "  global.onRequest=onRequest;\n"
        "  global.onStop=onStop;\n"
        "  global.onExit=onExit;\n"
        "}\n"
        "})(typeof require==='function'?require:null,typeof module==='object'?module:null,typeof exports==='object'?exports:null);\n"
    )


def _build_legacy_belet_wgt(target_url=BELET_URL, pkg_id=BELET_PKG_ID, app_name='Belet', icon_bytes=None):
    """Build the pre-proxy Belet WGT that opens belet.tm directly, without a TV-side localhost service."""
    if not re.fullmatch(r'[A-Za-z0-9]{10}', str(pkg_id)):
        raise ValueError(f'Tizen package ID must be exactly 10 alphanumeric chars, got {pkg_id!r}')

    from xml.sax.saxutils import escape as xml_escape

    app_id = f'{pkg_id}.{app_name}'
    safe_app_id = xml_escape(app_id, {'"': '&quot;'})
    safe_app_name = xml_escape(str(app_name or 'Belet'))
    safe_url = json.dumps(str(target_url or BELET_URL))
    safe_pkg = json.dumps(str(pkg_id))

    if icon_bytes is None:
        for candidate in (('assets', 'belet_large_icon.png'), ('assets', 'belet_icon.png')):
            p = _resource_path(*candidate)
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    icon_bytes = f.read()
                break
    if not icon_bytes:
        raise FileNotFoundError('Belet icon asset missing for .wgt build')

    config_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<widget xmlns="http://www.w3.org/ns/widgets" xmlns:tizen="http://tizen.org/ns/widgets" '
        'id="https://belet.tm/tizen" version="1.2.8" viewmodes="maximized">\n'
        f'    <tizen:application id="{safe_app_id}" package="{pkg_id}" required_version="2.3"/>\n'
        '    <content src="index.html"/>\n'
        '    <feature name="http://tizen.org/feature/screen.size.all"/>\n'
        '    <icon src="icon.png"/>\n'
        f'    <name>{safe_app_name}</name>\n'
        '    <access origin="*" subdomains="true"/>\n'
        '    <tizen:allow-navigation>*</tizen:allow-navigation>\n'
        '    <tizen:content-security-policy>'
        "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "style-src * 'unsafe-inline'; img-src * data:; media-src * blob:;"
        '</tizen:content-security-policy>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/internet"/>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/tv.inputdevice"/>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/application.launch"/>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/application.info"/>\n'
        '    <tizen:profile name="tv"/>\n'
        '    <tizen:setting screen-orientation="landscape" context-menu="disable" '
        'background-support="disable" encryption="disable" install-location="auto" hwkey-event="enable"/>\n'
        '</widget>\n'
    ).encode('utf-8')

    index_html = (
        '<!DOCTYPE html>\n'
        '<html lang="ru">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">\n'
        '<title>Belet</title>\n'
        '<style>\n'
        '*{box-sizing:border-box;}\n'
        'html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;'
        'background:radial-gradient(circle at 50% 42%,#0e233b 0%,#070b12 72%);'
        'color:#eaf4ff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}\n'
        '.wrap{position:absolute;top:50%;left:50%;'
        '-webkit-transform:translate(-50%,-50%);transform:translate(-50%,-50%);'
        'width:88%;max-width:860px;}\n'
        '.card{text-align:center;padding:38px 48px;border-radius:28px;'
        'background:rgba(13,17,22,0.86);border:1px solid rgba(44,232,255,0.28);'
        'box-shadow:0 24px 64px rgba(0,0,0,0.58);}\n'
        '.logo{width:116px;height:116px;border-radius:24px;margin-bottom:14px;}\n'
        '.title{font-size:36px;font-weight:700;letter-spacing:0.6px;margin-bottom:8px;}\n'
        '.status{font-size:21px;line-height:1.4;color:#8be9ff;min-height:32px;margin:8px 0 24px;}\n'
        '.status.err{color:#ff9e9e;}\n'
        '.actions{width:100%;max-width:680px;margin:0 auto;}\n'
        '.btn{display:block;width:100%;margin:0 auto 14px;padding:18px 26px;border-radius:16px;'
        'border:3px solid rgba(130,178,214,0.28);background:rgba(20,31,46,0.92);'
        'color:#eaf4ff;text-align:left;cursor:pointer;outline:none;'
        '-webkit-transition:transform 0.12s ease,border-color 0.12s ease;'
        'transition:transform 0.12s ease,border-color 0.12s ease;}\n'
        '.btn.primary{background:linear-gradient(135deg,rgba(25,75,112,0.95),rgba(13,43,74,0.95));'
        'border-color:rgba(44,232,255,0.45);}\n'
        '.btn.exit{text-align:center;padding:13px 22px;margin-top:8px;margin-bottom:0;'
        'background:rgba(26,33,44,0.75);border-color:rgba(148,163,184,0.24);color:#c2d4e6;}\n'
        '.btn.is-focus,.btn:focus{border-color:#2ce8ff;'
        'background:linear-gradient(135deg,#2ce8ff,#1484ff);color:#04101c;'
        '-webkit-transform:scale(1.025);transform:scale(1.025);'
        'box-shadow:0 0 0 4px rgba(44,232,255,0.30);}\n'
        '.btn-main{display:block;font-size:23px;font-weight:700;line-height:1.25;}\n'
        '.btn-sub{display:block;font-size:16px;opacity:0.86;margin-top:4px;}\n'
        '.tip{margin-top:22px;padding-top:16px;border-top:1px solid rgba(148,163,184,0.2);'
        'font-size:17px;line-height:1.45;color:#9bb5cd;}\n'
        '.tip b{color:#eaf4ff;}\n'
        '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="wrap">\n'
        '<div class="card">\n'
        '<img class="logo" src="icon.png" alt="Belet">\n'
        '<div class="title">Belet</div>\n'
        '<div id="status" class="status">Автозапуск в Браузере ТВ (курсор пульта)… Нажмите ↓ для выбора.</div>\n'
        '<div class="actions">\n'
        '<button id="btnBrowser" class="btn primary is-focus" type="button" tabindex="0">'
        '<span class="btn-main">Открыть с курсором пульта · Браузер ТВ</span>'
        '<span class="btn-sub">Open with TV Remote Cursor (Samsung Internet · recommended)</span>'
        '</button>\n'
        '<button id="btnWebview" class="btn" type="button" tabindex="0">'
        '<span class="btn-main">Полный экран без панели · WebView</span>'
        '<span class="btn-sub">Fullscreen WebView without toolbar (USB / Bluetooth mouse)</span>'
        '</button>\n'
        '<button id="btnExit" class="btn exit" type="button" tabindex="0">'
        '<span class="btn-main">Выход / Exit</span>'
        '</button>\n'
        '</div>\n'
        '<div class="tip">'
        '<b>Пульт / Remote:</b> ↑↓ выбор · OK открыть · RETURN выход<br>'
        'В Браузере ТВ стрелки управляют курсором. Чтобы убрать верхнюю панель браузера: '
        '<b>⚙ Настройки → Скрыть вкладки и панель меню</b> (Hide Tabs &amp; Menu).'
        '</div>\n'
        '</div>\n'
        '</div>\n'
        '<script>\n'
        '(function(){\n'
        'var TARGET=' + safe_url + ';\n'
        'var SELF_PKG=' + safe_pkg + ';\n'
        'var BROWSER_IDS=["3202010022079","3201907018784","org.tizen.browser","com.samsung.tv.browser"];\n'
        'var statusEl=document.getElementById("status");\n'
        'var btnBrowser=document.getElementById("btnBrowser");\n'
        'var btnWebview=document.getElementById("btnWebview");\n'
        'var btnExit=document.getElementById("btnExit");\n'
        'var btns=[btnBrowser,btnWebview,btnExit];\n'
        'var focusIdx=0;\n'
        'var autoTimer=null;\n'
        'var retryTimer=null;\n'
        'var autoCancelled=false;\n'
        'var launchedBrowser=false;\n'
        'var busy=false;\n'
        'function setStatus(msg,isErr){\n'
        '  if(!statusEl)return;\n'
        '  statusEl.textContent=msg;\n'
        '  statusEl.className=isErr?"status err":"status";\n'
        '}\n'
        'function setFocus(idx){\n'
        '  if(idx<0)idx=btns.length-1;\n'
        '  if(idx>=btns.length)idx=0;\n'
        '  focusIdx=idx;\n'
        '  for(var i=0;i<btns.length;i++){\n'
        '    if(!btns[i])continue;\n'
        '    var active=(i===focusIdx);\n'
        '    if(active){\n'
        '      if(btns[i].className.indexOf("is-focus")<0)btns[i].className+=" is-focus";\n'
        '      try{btns[i].focus();}catch(e){}\n'
        '    }else{\n'
        '      btns[i].className=btns[i].className.replace(/\\bis-focus\\b/g,"").replace(/\\s+/g," ");\n'
        '    }\n'
        '  }\n'
        '}\n'
        'function cancelAuto(){\n'
        '  autoCancelled=true;\n'
        '  if(autoTimer){clearTimeout(autoTimer);autoTimer=null;}\n'
        '}\n'
        'function exitApp(){\n'
        '  cancelAuto();\n'
        '  if(retryTimer){clearTimeout(retryTimer);retryTimer=null;}\n'
        '  try{tizen.application.getCurrentApplication().exit();}catch(e){try{window.close();}catch(e2){}}\n'
        '}\n'
        'function launchKnownIds(ctrl,list,idx,onOk,onAllFail){\n'
        '  if(idx>=list.length){onAllFail();return;}\n'
        '  var id=list[idx];\n'
        '  try{\n'
        '    tizen.application.launchAppControl(ctrl,id,onOk,function(){\n'
        '      launchKnownIds(ctrl,list,idx+1,onOk,onAllFail);\n'
        '    });\n'
        '  }catch(e){\n'
        '    launchKnownIds(ctrl,list,idx+1,onOk,onAllFail);\n'
        '  }\n'
        '}\n'
        'function tryFindBrowser(ctrl,onOk,onAllFail){\n'
        '  if(typeof tizen.application.findAppControl!=="function"){\n'
        '    launchKnownIds(ctrl,BROWSER_IDS,0,onOk,onAllFail);\n'
        '    return;\n'
        '  }\n'
        '  try{\n'
        '    tizen.application.findAppControl(ctrl,function(appInfos,appCtrl){\n'
        '      var preferred=[];\n'
        '      var others=[];\n'
        '      var seen={};\n'
        '      if(appInfos&&appInfos.length){\n'
        '        for(var i=0;i<appInfos.length;i++){\n'
        '          var aid=appInfos[i]&&appInfos[i].id;\n'
        '          if(!aid||seen[aid]||aid.indexOf(SELF_PKG)===0)continue;\n'
        '          seen[aid]=true;\n'
        '          var low=aid.toLowerCase();\n'
        '          if(low.indexOf("browser")>=0||low.indexOf("internet")>=0||aid==="3201907018784"||aid==="3202010022079"){\n'
        '            preferred.push(aid);\n'
        '          }else{\n'
        '            others.push(aid);\n'
        '          }\n'
        '        }\n'
        '      }\n'
        '      var pool=preferred.concat(others);\n'
        '      for(var j=0;j<BROWSER_IDS.length;j++){\n'
        '        if(!seen[BROWSER_IDS[j]]){pool.push(BROWSER_IDS[j]);seen[BROWSER_IDS[j]]=true;}\n'
        '      }\n'
        '      launchKnownIds(appCtrl||ctrl,pool,0,onOk,onAllFail);\n'
        '    },function(){\n'
        '      launchKnownIds(ctrl,BROWSER_IDS,0,onOk,onAllFail);\n'
        '    });\n'
        '  }catch(e){\n'
        '    launchKnownIds(ctrl,BROWSER_IDS,0,onOk,onAllFail);\n'
        '  }\n'
        '}\n'
        'function openBrowser(){\n'
        '  if(busy)return;\n'
        '  cancelAuto();\n'
        '  if(retryTimer){clearTimeout(retryTimer);retryTimer=null;}\n'
        '  if(typeof tizen==="undefined"||!tizen.application||typeof tizen.application.launchAppControl!=="function"){\n'
        '    setStatus("Браузер ТВ недоступен — выберите WebView ниже (нужна мышь).",true);\n'
        '    setFocus(1);\n'
        '    return;\n'
        '  }\n'
        '  var AppCtrl=tizen.ApplicationControl||tizen.application.ApplicationControl;\n'
        '  if(typeof AppCtrl!=="function"){\n'
        '    setStatus("ApplicationControl недоступен — выберите WebView ниже.",true);\n'
        '    setFocus(1);\n'
        '    return;\n'
        '  }\n'
        '  busy=true;\n'
        '  setStatus("Запуск Браузера ТВ (Samsung Internet)…",false);\n'
        '  var ctrl;\n'
        '  try{\n'
        '    ctrl=new AppCtrl("http://tizen.org/appcontrol/operation/view",TARGET,null,null,null,null);\n'
        '  }catch(e){\n'
        '    try{ctrl=new AppCtrl("http://tizen.org/appcontrol/operation/view",TARGET);}catch(e2){ctrl=null;}\n'
        '  }\n'
        '  if(!ctrl){\n'
        '    busy=false;\n'
        '    setStatus("Не удалось создать запрос браузера — выберите WebView.",true);\n'
        '    setFocus(1);\n'
        '    return;\n'
        '  }\n'
        '  function onOk(){\n'
        '    busy=false;\n'
        '    launchedBrowser=true;\n'
        '    setStatus("Открыто в Браузере ТВ (стрелки двигают курсор, OK — выбор).",false);\n'
        '  }\n'
        '  function onAllFail(){\n'
        '    busy=false;\n'
        '    setStatus("Браузер ТВ не найден — выберите WebView ниже (для мыши).",true);\n'
        '    setFocus(1);\n'
        '  }\n'
        '  try{\n'
        '    tizen.application.launchAppControl(ctrl,null,onOk,function(){\n'
        '      tryFindBrowser(ctrl,onOk,onAllFail);\n'
        '    });\n'
        '  }catch(e){\n'
        '    tryFindBrowser(ctrl,onOk,onAllFail);\n'
        '  }\n'
        '}\n'
        'function openWebview(){\n'
        '  if(busy)return;\n'
        '  cancelAuto();\n'
        '  busy=true;\n'
        '  setStatus("Загрузка belet.tm в полноэкранном WebView…",false);\n'
        '  if(retryTimer)clearTimeout(retryTimer);\n'
        '  retryTimer=setTimeout(function(){\n'
        '    busy=false;\n'
        '    setStatus("Не удаётся открыть belet.tm — проверьте Wi-Fi / Conf на ТВ и нажмите OK.",true);\n'
        '    setFocus(1);\n'
        '  },8000);\n'
        '  setTimeout(function(){\n'
        '    try{window.location.replace(TARGET);}catch(e){window.location.href=TARGET;}\n'
        '  },60);\n'
        '}\n'
        'var actions=[openBrowser,openWebview,exitApp];\n'
        'function triggerCurrent(){\n'
        '  var fn=actions[focusIdx];\n'
        '  if(fn)fn();\n'
        '}\n'
        'if(btnBrowser)btnBrowser.onclick=function(){setFocus(0);openBrowser();};\n'
        'if(btnWebview)btnWebview.onclick=function(){setFocus(1);openWebview();};\n'
        'if(btnExit)btnExit.onclick=function(){setFocus(2);exitApp();};\n'
        'document.addEventListener("keydown",function(ev){\n'
        '  var k=ev.keyCode;\n'
        '  if(k===38||k===37){\n'
        '    ev.preventDefault();\n'
        '    cancelAuto();\n'
        '    setFocus(focusIdx-1);\n'
        '    setStatus("Выберите режим стрелками ↑↓ и нажмите OK.",false);\n'
        '  }else if(k===40||k===39){\n'
        '    ev.preventDefault();\n'
        '    cancelAuto();\n'
        '    setFocus(focusIdx+1);\n'
        '    setStatus("Выберите режим стрелками ↑↓ и нажмите OK.",false);\n'
        '  }else if(k===13||ev.key==="Enter"){\n'
        '    ev.preventDefault();\n'
        '    cancelAuto();\n'
        '    triggerCurrent();\n'
        '  }else if(k===10009||k===10182||k===27||k===8){\n'
        '    ev.preventDefault();\n'
        '    exitApp();\n'
        '  }\n'
        '});\n'
        'window.addEventListener("tizenhwkey",function(ev){\n'
        '  if(ev&&ev.keyName==="back"){exitApp();}\n'
        '});\n'
        'document.addEventListener("visibilitychange",function(){\n'
        '  if(!document.hidden&&launchedBrowser){\n'
        '    busy=false;\n'
        '    setStatus("Belet готов. Нажмите OK для открытия с курсором или RETURN для выхода.",false);\n'
        '    setFocus(0);\n'
        '  }\n'
        '});\n'
        'setFocus(0);\n'
        'autoTimer=setTimeout(function(){\n'
        '  if(!autoCancelled&&!launchedBrowser)openBrowser();\n'
        '},900);\n'
        '})();\n'
        '</script>\n'
        '</body>\n'
        '</html>\n'
    ).encode('utf-8')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('config.xml', config_xml)
        zf.writestr('index.html', index_html)
        zf.writestr('icon.png', icon_bytes)
    return buf.getvalue()




def build_belet_wgt(
    target_url=BELET_URL,
    pkg_id=BELET_PKG_ID,
    app_name='Belet',
    icon_bytes=None,
    enable_service=True,
    tv_ip=None,
):
    """Build an unsigned Tizen .wgt package rendering Belet in a fullscreen WebView with TV D-pad navigation."""
    if not re.fullmatch(r'[A-Za-z0-9]{10}', str(pkg_id)):
        raise ValueError(f'Tizen package ID must be exactly 10 alphanumeric chars, got {pkg_id!r}')

    from xml.sax.saxutils import escape as xml_escape

    app_id = f'{pkg_id}.{app_name}'
    svc_id = f'{pkg_id}.BeletProxy'
    safe_app_id = xml_escape(app_id, {'"': '&quot;'})
    safe_svc_id = xml_escape(svc_id, {'"': '&quot;'})
    safe_app_name = xml_escape(str(app_name or 'Belet'))
    safe_url = json.dumps(str(target_url or BELET_URL))
    safe_pkg = json.dumps(str(pkg_id))
    safe_svc_js = json.dumps(svc_id)
    raw_hint_ip = str(tv_ip or '').strip()
    if not re.fullmatch(r'(?:\d{1,3}\.){3}\d{1,3}', raw_hint_ip):
        raw_hint_ip = ''
    safe_hint_ip = json.dumps(raw_hint_ip)
    req_ver = '3.0' if enable_service else '2.3'
    profile_name = 'tv-samsung' if enable_service else 'tv'

    if icon_bytes is None:
        for candidate in (('assets', 'belet_large_icon.png'), ('assets', 'belet_icon.png')):
            p = _resource_path(*candidate)
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    icon_bytes = f.read()
                break
    if not icon_bytes:
        raise FileNotFoundError('Belet icon asset missing for .wgt build')

    service_feature_xml = '    <feature name="http://tizen.org/feature/web.service"/>\n' if enable_service else ''
    service_decl_xml = (
        f'    <tizen:service id="{safe_svc_id}" auto-restart="true">\n'
        '        <tizen:content src="service/proxy.js"/>\n'
        '        <tizen:name>BeletProxy</tizen:name>\n'
        '        <tizen:description>Belet local reverse proxy</tizen:description>\n'
        '    </tizen:service>\n'
    ) if enable_service else ''

    config_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<widget xmlns="http://www.w3.org/ns/widgets" xmlns:tizen="http://tizen.org/ns/widgets" '
        'id="https://belet.tm/tizen" version="1.3.1" viewmodes="maximized">\n'
        f'    <tizen:application id="{safe_app_id}" package="{pkg_id}" required_version="{req_ver}"/>\n'
        '    <content src="index.html"/>\n'
        '    <feature name="http://tizen.org/feature/screen.size.all"/>\n'
        + service_feature_xml
        + '    <icon src="icon.png"/>\n'
        + f'    <name>{safe_app_name}</name>\n'
        + '    <access origin="*" subdomains="true"/>\n'
        + '    <tizen:allow-navigation>*</tizen:allow-navigation>\n'
        + '    <tizen:allow-mixed-content>true</tizen:allow-mixed-content>\n'
        + '    <tizen:content-security-policy>'
        + "default-src 'self' * 'unsafe-inline' 'unsafe-eval' data: blob:; "
        + f"frame-src * http: https: data: blob: http://127.0.0.1:{_BELET_PROXY_PORT} http://localhost:{_BELET_PROXY_PORT}; "
        + f"connect-src * http: https: ws: wss: data: blob: http://127.0.0.1:{_BELET_PROXY_PORT} http://localhost:{_BELET_PROXY_PORT};"
        + '</tizen:content-security-policy>\n'
        + '    <tizen:metadata key="http://samsung.com/tv/metadata/floating.navigation" value="false"/>\n'
        + '    <tizen:metadata key="http://samsung.com/tv/metadata/no_use_process_pool" value="true"/>\n'
        + '    <tizen:privilege name="http://tizen.org/privilege/internet"/>\n'
        + '    <tizen:privilege name="http://developer.samsung.com/privilege/network.public"/>\n'
        + '    <tizen:privilege name="http://tizen.org/privilege/tv.inputdevice"/>\n'
        + '    <tizen:privilege name="http://tizen.org/privilege/application.launch"/>\n'
        + service_decl_xml
        + f'    <tizen:profile name="{profile_name}"/>\n'
        + '    <tizen:setting screen-orientation="landscape" context-menu="disable" '
        + 'background-support="enable" encryption="disable" install-location="auto" hwkey-event="enable"/>\n'
        + '</widget>\n'
    ).encode('utf-8')

    nav_js = _build_belet_nav_js(target_url)
    proxy_js = _build_belet_proxy_js(target_url, nav_js=nav_js) if enable_service else None

    index_html = (
        '<!DOCTYPE html>\n'
        '<html lang="ru">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">\n'
        '<title>Belet</title>\n'
        '<style>\n'
        '*{box-sizing:border-box;}\n'
        'html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;'
        'background:radial-gradient(circle at 50% 42%,#0e233b 0%,#070b12 72%);'
        'color:#eaf4ff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}\n'
        '#splash{position:fixed;top:0;left:0;right:0;bottom:0;z-index:5;display:flex;'
        'align-items:center;justify-content:center;flex-direction:column;'
        'background:radial-gradient(circle at 50% 42%,#0e233b 0%,#070b12 72%);}\n'
        '.card{text-align:center;padding:38px 52px;border-radius:26px;'
        'background:rgba(13,17,22,0.82);border:1px solid rgba(44,232,255,0.26);'
        'box-shadow:0 24px 64px rgba(0,0,0,0.58);max-width:760px;width:86%;}\n'
        '.logo{width:112px;height:112px;border-radius:22px;margin-bottom:16px;}\n'
        '.title{font-size:32px;font-weight:700;letter-spacing:0.6px;}\n'
        '.status{margin-top:10px;font-size:19px;color:#8be9ff;min-height:28px;}\n'
        '.spin{width:38px;height:38px;border-radius:50%;margin:18px auto 0;'
        'border:3px solid rgba(44,232,255,0.22);border-top-color:#2ce8ff;'
        '-webkit-animation:sp 0.9s linear infinite;animation:sp 0.9s linear infinite;}\n'
        '@-webkit-keyframes sp{to{-webkit-transform:rotate(360deg);}}\n'
        '@keyframes sp{to{transform:rotate(360deg);}}\n'
        '#errBox{display:none;margin-top:22px;}\n'
        '.btn{display:inline-block;margin:8px 8px 0;padding:14px 28px;border-radius:14px;'
        'border:3px solid rgba(130,178,214,0.32);background:rgba(20,31,46,0.94);'
        'color:#eaf4ff;font-size:19px;font-weight:700;cursor:pointer;outline:none;}\n'
        '.btn.is-focus,.btn:focus{border-color:#2ce8ff;'
        'background:linear-gradient(135deg,#2ce8ff,#1484ff);color:#04101c;'
        'box-shadow:0 0 0 4px rgba(44,232,255,0.30);}\n'
        '#beletFrame{position:fixed;top:0;left:0;width:100vw;height:100vh;border:0;background:#000;z-index:10;display:none;}\n'
        '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div id="splash">\n'
        '<div class="card">\n'
        '<img class="logo" src="icon.png" alt="Belet">\n'
        '<div class="title">Belet</div>\n'
        '<div id="status" class="status">Загрузка Belet…</div>\n'
        '<div id="spinner" class="spin"></div>\n'
        '<div id="errBox">\n'
        '<button id="btnRetry" class="btn is-focus" type="button">Повторить / Retry</button>\n'
        '<button id="btnDirect" class="btn" type="button">Выход / Exit</button>\n'
        '</div>\n'
        '</div>\n'
        '</div>\n'
        '<iframe id="beletFrame" tabindex="0" allow="autoplay; fullscreen; encrypted-media; clipboard-read; clipboard-write" allowfullscreen></iframe>\n'
        '<script src="$WEBAPIS/webapis/webapis.js"></script>\n'
        '<script>\n'
        '(function(){\n'
        'var TARGET=' + safe_url + ';\n'
        'var TARGET_PATH=(function(){var m=String(TARGET||"").match(/^https?:\\/\\/[^\\/?#]+([^#]*)/i);var p=(m&&m[1])?m[1]:"/";return p.charAt(0)==="/"?p:("/"+p);})();\n'
        'var SELF_PKG=' + safe_pkg + ';\n'
        'var SVC_ID=' + safe_svc_js + ';\n'
        'var PROXY_PORT=' + str(_BELET_PROXY_PORT) + ';\n'
        'var ENABLE_SVC=' + ('true' if enable_service else 'false') + ';\n'
        'var HINT_TV_IP=' + safe_hint_ip + ';\n'
        'var splashEl=document.getElementById("splash");\n'
        'var statusEl=document.getElementById("status");\n'
        'var spinEl=document.getElementById("spinner");\n'
        'var errBox=document.getElementById("errBox");\n'
        'var btnRetry=document.getElementById("btnRetry");\n'
        'var btnDirect=document.getElementById("btnDirect");\n'
        'var frameEl=document.getElementById("beletFrame");\n'
        'var frameReady=false;\n'
        'var frameNavReady=false;\n'
        'var everHadNav=false;\n'
        'var activeProxyBase="";\n'
        'var proxyRecoverTries=0;\n'
        'var navAck=false;\n'
        'var navPingTimer=null;\n'
        'var usingTier2=false;\n'
        'var lastTier2Url=TARGET;\n'
        'var tier2Stack=[];\n'
        'var bootDiag="init";\n'
        'var svcLaunchDiag="pending";\n'
        'var svcTimeoutDiag="";\n'
        'var tvIp=HINT_TV_IP;\n'
        'var rtcStarted=false;\n'
        'var wrtTried=false;\n'
        'var backTimer=null;\n'
        'var lastBackSentAt=0;\n'
        'var lastBackSrc="";\n'
        'var errFocusIdx=0;\n'
        'var cachedNavJs=null;\n'
        'var xhrCache={};\n'
        'var neonStore=(function(){\n'
        '  var mem={ls:{},ss:{},ck:{}};\n'
        '  var pfx={ls:"__neon_belet_ls__",ck:"__neon_belet_ck__"};\n'
        '  for(var k in pfx){\n'
        '    try{\n'
        '      if(window.localStorage){\n'
        '        var raw=window.localStorage.getItem(pfx[k]);\n'
        '        if(raw){var obj=JSON.parse(raw);if(obj&&typeof obj==="object")mem[k]=obj;}\n'
        '      }\n'
        '    }catch(e){}\n'
        '  }\n'
        '  try{\n'
        '    if(window.sessionStorage){\n'
        '      var sraw=window.sessionStorage.getItem("__neon_belet_ss__");\n'
        '      if(sraw){var sobj=JSON.parse(sraw);if(sobj&&typeof sobj==="object")mem.ss=sobj;}\n'
        '    }\n'
        '  }catch(e){}\n'
        '  function save(kind){\n'
        '    try{\n'
        '      if((kind==="ls"||kind==="ck")&&window.localStorage){\n'
        '        window.localStorage.setItem(pfx[kind],JSON.stringify(mem[kind]));\n'
        '      }else if(kind==="ss"&&window.sessionStorage){\n'
        '        window.sessionStorage.setItem("__neon_belet_ss__",JSON.stringify(mem.ss));\n'
        '      }\n'
        '    }catch(e){}\n'
        '  }\n'
        '  return {\n'
        '    snapshot:function(kind){\n'
        '      var src=mem[kind]||{},out={};\n'
        '      for(var p in src)if(Object.prototype.hasOwnProperty.call(src,p))out[p]=src[p];\n'
        '      return out;\n'
        '    },\n'
        '    apply:function(d){\n'
        '      var kind=d&&d.kind;\n'
        '      if(!kind||!mem[kind])return;\n'
        '      if(d.op==="set"&&d.key!==undefined)mem[kind][String(d.key)]=String(d.val||"");\n'
        '      else if(d.op==="remove"&&d.key!==undefined)delete mem[kind][String(d.key)];\n'
        '      else if(d.op==="clear")mem[kind]={};\n'
        '      save(kind);\n'
        '    }\n'
        '  };\n'
        '})();\n'
        'function safeJson(obj){return JSON.stringify(obj||{}).replace(/</g,"\\\\u003c");}\n'
        'function setStatus(msg){if(statusEl)statusEl.textContent=msg;}\n'
        'function exitApp(){\n'
        '  if(backTimer){clearTimeout(backTimer);backTimer=null;}\n'
        '  try{tizen.application.getCurrentApplication().exit();}catch(e){try{window.close();}catch(e2){}}\n'
        '}\n'
        'function registerRemoteKeys(){\n'
        '  if(typeof tizen==="undefined"||!tizen.tvinputdevice||typeof tizen.tvinputdevice.registerKey!=="function")return;\n'
        '  var ks=["ColorF0Red","ColorF1Green","ColorF2Yellow","ColorF3Blue","Info","MediaPlay","MediaPause","MediaStop","MediaRewind","MediaFastForward","MediaPlayPause","MediaTrackPrevious","MediaTrackNext"];\n'
        '  for(var i=0;i<ks.length;i++){\n'
        '    try{tizen.tvinputdevice.registerKey(ks[i]);}catch(e){}\n'
        '  }\n'
        '}\n'
        'function platformMajor(){\n'
        '  try{\n'
        '    if(typeof tizen!=="undefined"&&tizen.systeminfo&&typeof tizen.systeminfo.getCapability==="function"){\n'
        '      return parseInt(tizen.systeminfo.getCapability("http://tizen.org/feature/platform.version"),10)||0;\n'
        '    }\n'
        '  }catch(e){}\n'
        '  return 0;\n'
        '}\n'
        'function hasWebServiceCap(){\n'
        '  return !!ENABLE_SVC;\n'
        '}\n'
        'function readWebServiceCapInfo(){\n'
        '  try{\n'
        '    if(typeof tizen!=="undefined"&&tizen.systeminfo&&typeof tizen.systeminfo.getCapability==="function"){\n'
        '      return String(tizen.systeminfo.getCapability("http://tizen.org/feature/web.service"));\n'
        '    }\n'
        '  }catch(e){return "err";}\n'
        '  return "na";\n'
        '}\n'
        'function probeTvIp(){\n'
        '  try{\n'
        '    if(typeof webapis!=="undefined"&&webapis.network&&typeof webapis.network.getIp==="function"){\n'
        '      var wip=webapis.network.getIp();\n'
        '      if(wip&&typeof wip==="string"&&wip!=="0.0.0.0"&&(!tvIp||tvIp===HINT_TV_IP))tvIp=wip.replace(/^\\s+|\\s+$/g,"");\n'
        '    }\n'
        '  }catch(e){}\n'
        '  try{\n'
        '    if(typeof tizen!=="undefined"&&tizen.systeminfo&&typeof tizen.systeminfo.getPropertyValue==="function"){\n'
        '      var props=["ETHERNET_NETWORK","WIFI_NETWORK","NETWORK"];\n'
        '      for(var i=0;i<props.length;i++){\n'
        '        try{\n'
        '          tizen.systeminfo.getPropertyValue(props[i],function(val){\n'
        '            var ip=val&&(val.ipAddress||val.ip);\n'
        '            if(ip&&ip!=="0.0.0.0"&&(!tvIp||tvIp===HINT_TV_IP))tvIp=String(ip).replace(/^\\s+|\\s+$/g,"");\n'
        '          },function(){});\n'
        '        }catch(e){}\n'
        '      }\n'
        '    }\n'
        '  }catch(e){}\n'
        '  try{\n'
        '    var PC=window.RTCPeerConnection||window.webkitRTCPeerConnection;\n'
        '    if(PC&&(!tvIp||tvIp===HINT_TV_IP)&&!rtcStarted){\n'
        '      rtcStarted=true;\n'
        '      var pc=new PC({iceServers:[]});\n'
        '      if(pc.createDataChannel)pc.createDataChannel("");\n'
        '      pc.onicecandidate=function(ev){\n'
        '        var c=ev&&ev.candidate&&ev.candidate.candidate;\n'
        '        if(!c)return;\n'
        '        var m=c.match(/(\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3})/);\n'
        '        if(m&&m[1]!=="0.0.0.0"&&m[1].indexOf("127.")!==0){\n'
        '          tvIp=m[1];\n'
        '          try{pc.close();}catch(e){}\n'
        '        }\n'
        '      };\n'
        '      var offerP=pc.createOffer();\n'
        '      if(offerP&&typeof offerP.then==="function"){\n'
        '        offerP.then(function(o){return pc.setLocalDescription(o);})["catch"](function(){});\n'
        '      }\n'
        '    }\n'
        '  }catch(e){}\n'
        '}\n'
        'function getProxyCandidates(){\n'
        '  var raw=["http://127.0.0.1:"+PROXY_PORT];\n'
        '  if(tvIp)raw.push("http://"+tvIp+":"+PROXY_PORT);\n'
        '  if(HINT_TV_IP)raw.push("http://"+HINT_TV_IP+":"+PROXY_PORT);\n'
        '  raw.push("http://localhost:"+PROXY_PORT);\n'
        '  var out=[];\n'
        '  for(var i=0;i<raw.length;i++){\n'
        '    if(out.indexOf(raw[i])<0)out.push(raw[i]);\n'
        '  }\n'
        '  return out;\n'
        '}\n'
        'function pingJson(url,cb){\n'
        '  try{\n'
        '    var x=new XMLHttpRequest();\n'
        '    x.open("GET",url+"/__neon_ping",true);\n'
        '    x.timeout=500;\n'
        '    x.onload=function(){\n'
        '      try{\n'
        '        var d=JSON.parse(x.responseText);\n'
        '        if(x.status===200&&d&&d.ok){cb(null,url);return;}\n'
        '      }catch(e){}\n'
        '      cb(new Error("bad ping"));\n'
        '    };\n'
        '    x.onerror=function(){cb(new Error("err"));};\n'
        '    x.ontimeout=function(){cb(new Error("timeout"));};\n'
        '    x.send();\n'
        '  }catch(e){cb(e);}\n'
        '}\n'
        'function withWrtService(cb){\n'
        '  if(platformMajor()<9){cb();return;}\n'
        '  if(window.__neonWrtService){cb();return;}\n'
        '  if(wrtTried){cb();return;}\n'
        '  wrtTried=true;\n'
        '  var done=false;\n'
        '  function finish(){if(done)return;done=true;cb();}\n'
        '  try{window.addEventListener("neon-wrt-ready",finish);}catch(e){}\n'
        '  setTimeout(finish,2500);\n'
        '  try{\n'
        '    if(document.createElement&&document.body){\n'
        '      var s=document.createElement("script");\n'
        '      s.type="module";\n'
        '      s.text="import { Service } from \'wrt:service\'; window.__neonWrtService = Service; window.dispatchEvent(new Event(\'neon-wrt-ready\'));";\n'
        '      s.onerror=finish;\n'
        '      document.body.appendChild(s);\n'
        '      return;\n'
        '    }\n'
        '  }catch(e){}\n'
        '  finish();\n'
        '}\n'
        'function launchProxyLegacy(){\n'
        '  if(typeof tizen!=="undefined"&&tizen.application){\n'
        '    if(tizen.ApplicationControl&&typeof tizen.application.launchAppControl==="function"){\n'
        '      try{\n'
        '        tizen.application.launchAppControl(\n'
        '          new tizen.ApplicationControl("http://tizen.org/appcontrol/operation/service"),\n'
        '          SVC_ID,\n'
        '          function(){svcLaunchDiag="appctl_ok";},\n'
        '          function(err){\n'
        '            svcLaunchDiag="appctl_err_"+((err&&(err.name||err.message))||"fail");\n'
        '            try{\n'
        '              if(tizen.application.launch){\n'
        '                tizen.application.launch(SVC_ID,function(){svcLaunchDiag="launch_ok";},function(e2){svcLaunchDiag="launch_err_"+((e2&&(e2.name||e2.message))||"fail");});\n'
        '              }\n'
        '            }catch(e){}\n'
        '          }\n'
        '        );\n'
        '        return;\n'
        '      }catch(e){svcLaunchDiag="appctl_exc";}\n'
        '    }\n'
        '    try{\n'
        '      if(tizen.application.launch){\n'
        '        tizen.application.launch(SVC_ID,function(){svcLaunchDiag="launch_ok";},function(e3){svcLaunchDiag="launch_err_"+((e3&&(e3.name||e3.message))||"fail");});\n'
        '      }\n'
        '    }catch(e){svcLaunchDiag="launch_exc";}\n'
        '  }else{svcLaunchDiag="no_tizen_app";}\n'
        '}\n'
        'function launchProxyService(){\n'
        '  var WrtSvc=window.__neonWrtService;\n'
        '  if(typeof WrtSvc==="function"){\n'
        '    try{\n'
        '      var p=new WrtSvc(SVC_ID).start();\n'
        '      if(p&&typeof p.then==="function"){\n'
        '        p.then(function(){svcLaunchDiag="wrt_ok";},function(e){svcLaunchDiag="wrt_err_"+((e&&(e.message||e.name))||"fail");launchProxyLegacy();});\n'
        '      }else{svcLaunchDiag="wrt_sync";}\n'
        '      return;\n'
        '    }catch(e){svcLaunchDiag="wrt_exc";}\n'
        '  }\n'
        '  launchProxyLegacy();\n'
        '}\n'
        'function focusIframe(){\n'
        '  if(!frameEl)return;\n'
        '  try{frameEl.focus();}catch(e){}\n'
        '  try{if(frameEl.contentWindow)frameEl.contentWindow.focus();}catch(e){}\n'
        '}\n'
        'function formatBootDiag(){\n'
        '  return svcTimeoutDiag?(bootDiag+" / "+svcTimeoutDiag):bootDiag;\n'
        '}\n'
        'function verifyFrameNavBridge(){\n'
        '  if(navPingTimer){clearTimeout(navPingTimer);navPingTimer=null;}\n'
        '  navAck=false;\n'
        '  if(usingTier2){\n'
        '    try{\n'
        '      var _d=frameEl.contentDocument||(frameEl.contentWindow&&frameEl.contentWindow.document);\n'
        '      var _loc="";\n'
        '      try{_loc=String(frameEl.contentWindow&&frameEl.contentWindow.location&&frameEl.contentWindow.location.href||"");}catch(e2){}\n'
        '      if(!_d){frameNavReady=false;bootDiag="tier2_cross_origin";showErrorUi("Страница Belet перенаправлена ("+formatBootDiag()+"). Нажмите Повторить или Выход.");return;}\n'
        '      if(_loc.indexOf("chrome-error:")===0){frameNavReady=false;bootDiag="tier2_blocked_frame";showErrorUi("Встроенный фрейм заблокирован ("+formatBootDiag()+"). Нажмите Повторить или Выход.");return;}\n'
        '    }catch(e){frameNavReady=false;bootDiag="tier2_cross_origin";showErrorUi("Страница Belet перенаправлена ("+formatBootDiag()+"). Нажмите Повторить или Выход.");return;}\n'
        '  }\n'
        '  try{if(frameEl&&frameEl.contentWindow)frameEl.contentWindow.postMessage({type:"neon-ping"},"*");}catch(e){}\n'
        '  navPingTimer=setTimeout(function(){\n'
        '    if(!navAck){\n'
        '      frameNavReady=false;\n'
        '      if(usingTier2){bootDiag="tier2_no_nav";showErrorUi("Нет ответа от навигации Belet ("+formatBootDiag()+"). Нажмите Повторить или Выход.");return;}\n'
        '      if(!everHadNav){bootDiag="proxy_no_nav";svcTimeoutDiag=bootDiag;loadViaWebViewFetch(TARGET);}\n'
        '      else if(activeProxyBase&&proxyRecoverTries<2){proxyRecoverTries++;frameEl.src=activeProxyBase+TARGET_PATH;}\n'
        '    }\n'
        '  },3500);\n'
        '}\n'
        'function showIframeUrl(srcUrl){\n'
        '  if(!frameEl)return;\n'
        '  usingTier2=false;\n'
        '  activeProxyBase=String(srcUrl||"").replace(/^(https?:\\/\\/[^\\/?#]+).*$/i,"$1");\n'
        '  proxyRecoverTries=0;\n'
        '  frameReady=false;\n'
        '  frameNavReady=true;\n'
        '  bootDiag="proxy_ok";\n'
        '  if(errBox)errBox.style.display="none";\n'
        '  if(spinEl)spinEl.style.display="block";\n'
        '  frameEl.style.display="block";\n'
        '  frameEl.onload=function(){\n'
        '    frameReady=true;\n'
        '    frameNavReady=true;\n'
        '    if(splashEl)splashEl.style.display="none";\n'
        '    focusIframe();\n'
        '    verifyFrameNavBridge();\n'
        '  };\n'
        '  frameEl.src=srcUrl;\n'
        '}\n'
        'function loadNavText(cb){\n'
        '  if(cachedNavJs!==null){cb(cachedNavJs);return;}\n'
        '  try{\n'
        '    var x=new XMLHttpRequest();\n'
        '    x.open("GET","neon-nav.js",true);\n'
        '    x.onload=function(){cachedNavJs=x.responseText||"";cb(cachedNavJs);};\n'
        '    x.onerror=function(){cachedNavJs="";cb("");};\n'
        '    x.send();\n'
        '  }catch(e){cachedNavJs="";cb("");}\n'
        '}\n'
        'function rewriteFetchHtml(html,baseUrl,navCode){\n'
        '  var out=String(html||"");\n'
        '  out=out.replace(/<meta[^>]*http-equiv\\s*=\\s*["\']?(?:Content-Security-Policy|Content-Security-Policy-Report-Only|X-WebKit-CSP|X-Content-Security-Policy|X-Frame-Options|refresh)["\']?[^>]*>/gi,"");\n'
        '  out=out.replace(/<base\\b[^>]*>/gi,"");\n'
        '  out=out.replace(/\\s+integrity=(["\'])[^"\']*\\1/gi,"");\n'
        '  out=out.replace(/\\s+crossorigin(?:=(["\'])[^"\']*\\1)?/gi,"");\n'
        '  var safeBase=String(baseUrl||TARGET).replace(/"/g,"&quot;");\n'
        '  var initState="window.__NEON_FETCH_MODE__=true;window.__NEON_TIER2_DEPTH__="+(tier2Stack.length|0)+";window.__NEON_TARGET_URL__="+safeJson(String(baseUrl||TARGET))+";window.__NEON_INIT_LS__="+safeJson(neonStore.snapshot("ls"))+";window.__NEON_INIT_SS__="+safeJson(neonStore.snapshot("ss"))+";window.__NEON_INIT_CK__="+safeJson(neonStore.snapshot("ck"))+";";\n'
        '  var pre="<base href=\\""+safeBase+"\\"><script>"+initState+"<\\/script><script>"+String(navCode||"").replace(/<\\/script>/gi,"<\\\\/script>")+"<\\/script>";\n'
        '  if(/<head\\b[^>]*>/i.test(out)){\n'
        '    out=out.replace(/<head\\b[^>]*>/i,function(m){return m+pre;});\n'
        '  }else{\n'
        '    out=pre+out;\n'
        '  }\n'
        '  return out;\n'
        '}\n'
        'function showErrorUi(msg){\n'
        '  frameReady=false;\n'
        '  if(frameEl)frameEl.style.display="none";\n'
        '  if(splashEl)splashEl.style.display="flex";\n'
        '  if(spinEl)spinEl.style.display="none";\n'
        '  if(errBox)errBox.style.display="block";\n'
        '  setStatus(msg||"Не удалось загрузить belet.tm. Проверьте сеть / Conf.");\n'
        '  setErrFocus(0);\n'
        '}\n'
        'function setErrFocus(idx){\n'
        '  var list=[btnRetry,btnDirect];\n'
        '  errFocusIdx=(idx%2+2)%2;\n'
        '  for(var i=0;i<list.length;i++){\n'
        '    if(!list[i])continue;\n'
        '    if(i===errFocusIdx){\n'
        '      if(list[i].className.indexOf("is-focus")<0)list[i].className+=" is-focus";\n'
        '      try{list[i].focus();}catch(e){}\n'
        '    }else{\n'
        '      list[i].className=list[i].className.replace(/\\bis-focus\\b/g,"");\n'
        '    }\n'
        '  }\n'
        '}\n'
        'function loadViaWebViewFetch(url,isPop){\n'
        '  var nextUrl=url||TARGET;\n'
        '  if(!isPop&&usingTier2&&lastTier2Url&&nextUrl!==lastTier2Url){\n'
        '    if(!tier2Stack.length||tier2Stack[tier2Stack.length-1]!==lastTier2Url){\n'
        '      tier2Stack.push(lastTier2Url);\n'
        '      if(tier2Stack.length>20)tier2Stack.shift();\n'
        '    }\n'
        '  }\n'
        '  usingTier2=true;\n'
        '  lastTier2Url=nextUrl;\n'
        '  setStatus("Загрузка Belet ("+bootDiag+")…");\n'
        '  loadNavText(function(navCode){\n'
        '    var cacheKey="__neon_cached_html_"+nextUrl;\n'
        '    var cached=null;\n'
        '    try{\n'
        '      if(window.localStorage){\n'
        '        var raw=window.localStorage.getItem(cacheKey);\n'
        '        if(raw){\n'
        '          var entry=JSON.parse(raw);\n'
        '          if(entry&&entry.html&&(Date.now()-entry.ts)<86400000)cached=entry.html;\n'
        '        }\n'
        '      }\n'
        '    }catch(e){}\n'
        '    if(cached&&!frameReady){\n'
        '      try{\n'
        '        var preHtml=rewriteFetchHtml(cached,nextUrl,navCode);\n'
        '        frameEl.style.display="block";\n'
        '        var doc0=frameEl.contentDocument||(frameEl.contentWindow&&frameEl.contentWindow.document);\n'
        '        if(doc0){\n'
        '          doc0.open();doc0.write(preHtml);doc0.close();\n'
        '          frameReady=true;frameNavReady=true;\n'
        '          if(splashEl)splashEl.style.display="none";\n'
        '          focusIframe();\n'
        '          verifyFrameNavBridge();\n'
        '        }\n'
        '      }catch(eP){}\n'
        '    }\n'
        '    try{\n'
        '      var x=new XMLHttpRequest();\n'
        '      x.open("GET",nextUrl,true);\n'
        '      try{x.withCredentials=true;}catch(eW){}\n'
        '      x.timeout=14000;\n'
        '      x.onload=function(){\n'
        '        if(x.status>=200&&x.status<400&&x.responseText){\n'
        '          var finalUrl=x.responseURL||url||TARGET;\n'
        '          lastTier2Url=finalUrl;\n'
        '          try{if(window.localStorage&&x.status===200)window.localStorage.setItem(cacheKey,JSON.stringify({html:x.responseText,ts:Date.now()}));}catch(eS){}\n'
        '          var html=rewriteFetchHtml(x.responseText,finalUrl,navCode);\n'
        '          frameEl.style.display="block";\n'
        '          var wroteBlank=false;\n'
        '          frameEl.onload=function(){\n'
        '            if(wroteBlank){\n'
        '              focusIframe();\n'
        '              verifyFrameNavBridge();\n'
        '              return;\n'
        '            }\n'
        '            wroteBlank=true;\n'
        '            frameEl.onload=function(){\n'
        '              focusIframe();\n'
        '              verifyFrameNavBridge();\n'
        '            };\n'
        '            try{\n'
        '              var doc=frameEl.contentDocument||(frameEl.contentWindow&&frameEl.contentWindow.document);\n'
        '              if(doc){\n'
        '                doc.open();\n'
        '                doc.write(html);\n'
        '                doc.close();\n'
        '                frameReady=true;\n'
        '                frameNavReady=true;\n'
        '                if(splashEl)splashEl.style.display="none";\n'
        '                focusIframe();\n'
        '                verifyFrameNavBridge();\n'
        '                return;\n'
        '              }\n'
        '            }catch(e){}\n'
        '            showErrorUi("Не удалось открыть страницу Belet ("+bootDiag+").");\n'
        '          };\n'
        '          frameEl.src="about:blank";\n'
        '        }else if(!frameReady){\n'
        '          showErrorUi("Ошибка загрузки belet.tm (HTTP "+x.status+", "+bootDiag+").");\n'
        '        }\n'
        '      };\n'
        '      x.onerror=function(){if(!frameReady)showErrorUi("Нет соединения с belet.tm ("+bootDiag+"). Проверьте Wi-Fi / Conf.");};\n'
        '      x.ontimeout=function(){if(!frameReady)showErrorUi("Тайм-аут загрузки belet.tm ("+bootDiag+").");};\n'
        '      x.send();\n'
        '    }catch(e){if(!frameReady)showErrorUi("Ошибка загрузки belet.tm ("+bootDiag+").");}\n'
        '  });\n'
        '}\n'
        'function isCacheableXhr(method,url,hdrs){\n'
        '  if(String(method||"GET").toUpperCase()!=="GET")return false;\n'
        '  var u=String(url||"").toLowerCase();\n'
        '  if(/\\/(?:auth|login|logout|register|otp|qr|session|token|user|profile|me|account|payment|subscribe|watch|history)(?:\\/|$|\\?)/i.test(u))return false;\n'
        '  if(hdrs){\n'
        '    for(var k in hdrs)if(Object.prototype.hasOwnProperty.call(hdrs,k)&&String(k).toLowerCase()==="authorization")return false;\n'
        '  }\n'
        '  return /_next\\/data\\/|\\.json(?:$|\\?)|\\/api\\//i.test(u);\n'
        '}\n'
        'function handleParentXhr(req,srcWin){\n'
        '  var targetWin=srcWin||(frameEl&&frameEl.contentWindow);\n'
        '  function reply(payload){\n'
        '    payload.type="neon-xhr-res";\n'
        '    payload.id=req.id;\n'
        '    try{if(targetWin)targetWin.postMessage(payload,"*");}catch(e){}\n'
        '  }\n'
        '  var mth=String(req.method||"GET").toUpperCase();\n'
        '  if(mth==="POST"||mth==="PUT"||mth==="PATCH"||mth==="DELETE")xhrCache={};\n'
        '  var cKey=isCacheableXhr(mth,req.url,req.headers)?String(req.url):"";\n'
        '  if(cKey&&xhrCache[cKey]&&(Date.now()-xhrCache[cKey].ts)<180000){\n'
        '    var ce=xhrCache[cKey];\n'
        '    reply({status:ce.status,statusText:ce.statusText,responseURL:ce.responseURL,headers:ce.headers,body:ce.body});\n'
        '    return;\n'
        '  }\n'
        '  try{\n'
        '    var x=new XMLHttpRequest();\n'
        '    x.open(req.method||"GET",req.url,true);\n'
        '    try{x.withCredentials=true;}catch(eW){}\n'
        '    x.timeout=16000;\n'
        '    var hdrs=req.headers||{};\n'
        '    var skip={host:1,origin:1,referer:1,"content-length":1,"user-agent":1,cookie:1,connection:1};\n'
        '    for(var hk in hdrs){\n'
        '      if(!Object.prototype.hasOwnProperty.call(hdrs,hk))continue;\n'
        '      if(skip[String(hk).toLowerCase()])continue;\n'
        '      try{x.setRequestHeader(hk,hdrs[hk]);}catch(e){}\n'
        '    }\n'
        '    x.onload=function(){\n'
        '      var respHdrs={};\n'
        '      try{\n'
        '        var raw=x.getAllResponseHeaders?x.getAllResponseHeaders():"";\n'
        '        var lines=String(raw||"").split(/\\r?\\n/);\n'
        '        for(var i=0;i<lines.length;i++){\n'
        '          var idx=lines[i].indexOf(":");\n'
        '          if(idx>0){\n'
        '            var k=lines[i].slice(0,idx).trim().toLowerCase();\n'
        '            var v=lines[i].slice(idx+1).trim();\n'
        '            if(k)respHdrs[k]=v;\n'
        '          }\n'
        '        }\n'
        '      }catch(e){}\n'
        '      var resObj={status:x.status||200,statusText:x.statusText||"",responseURL:x.responseURL||req.url,headers:respHdrs,body:x.responseText||""};\n'
        '      if(cKey&&resObj.status===200&&!/(?:no-store|private)/i.test(String(respHdrs["cache-control"]||""))){\n'
        '        xhrCache[cKey]={ts:Date.now(),status:resObj.status,statusText:resObj.statusText,responseURL:resObj.responseURL,headers:respHdrs,body:resObj.body};\n'
        '      }\n'
        '      reply(resObj);\n'
        '    };\n'
        '    x.onerror=function(){reply({error:"network"});};\n'
        '    x.ontimeout=function(){reply({error:"timeout"});};\n'
        '    x.send(req.body!==undefined?req.body:null);\n'
        '  }catch(e){reply({error:"xhr_exception"});}\n'
        '}\n'
        'function startBoot(){\n'
        '  svcTimeoutDiag="";\n'
        '  if(errBox)errBox.style.display="none";\n'
        '  if(spinEl)spinEl.style.display="block";\n'
        '  if(splashEl)splashEl.style.display="flex";\n'
        '  setStatus("Запуск Belet…");\n'
        '  if(!hasWebServiceCap()){\n'
        '    bootDiag="svc_disabled_in_wgt";\n'
        '    svcTimeoutDiag=bootDiag;\n'
        '    loadViaWebViewFetch(TARGET);\n'
        '    return;\n'
        '  }\n'
        '  probeTvIp();\n'
        '  withWrtService(function(){\n'
        '    launchProxyService();\n'
        '    var tries=0,resolved=false,lastErr="none";\n'
        '    function poll(){\n'
        '      if(resolved)return;\n'
        '      var cands=getProxyCandidates();\n'
        '      var left=cands.length;\n'
        '      for(var i=0;i<cands.length;i++){\n'
        '        pingJson(cands[i],function(err,okUrl){\n'
        '          if(resolved)return;\n'
        '          if(!err&&okUrl){\n'
        '            resolved=true;\n'
        '            showIframeUrl(okUrl+TARGET_PATH);\n'
        '            return;\n'
        '          }\n'
        '          if(err&&err.message)lastErr=err.message;\n'
        '          left--;\n'
        '          if(left<=0&&!resolved){\n'
        '            tries++;\n'
        '            if(tries===16||tries===45)launchProxyService();\n'
        '            if(tries>=100){\n'
        '              resolved=true;\n'
        '              bootDiag="proxy_timeout_"+svcLaunchDiag+"_cap_"+readWebServiceCapInfo()+"_"+lastErr;\n'
        '              svcTimeoutDiag=bootDiag;\n'
        '              loadViaWebViewFetch(TARGET);\n'
        '              return;\n'
        '            }\n'
        '            setTimeout(poll,250);\n'
        '          }\n'
        '        });\n'
        '      }\n'
        '    }\n'
        '    poll();\n'
        '  });\n'
        '}\n'
        'function forwardKeyToFrame(code,keyStr,src){\n'
        '  if(!frameReady||!frameEl||!frameEl.contentWindow)return false;\n'
        '  var isBack=(code===10009||code===27||keyStr==="Back");\n'
        '  if(isBack){\n'
        '    var now=Date.now();\n'
        '    if(backTimer&&src&&lastBackSrc&&src!==lastBackSrc&&(now-lastBackSentAt)<180)return false;\n'
        '    lastBackSentAt=now;\n'
        '    lastBackSrc=src||"";\n'
        '  }\n'
        '  focusIframe();\n'
        '  try{frameEl.contentWindow.postMessage({type:"neon-key",keyCode:code,key:keyStr||""},"*");}catch(e){}\n'
        '  if(isBack){\n'
        '    if(backTimer)clearTimeout(backTimer);\n'
        '    backTimer=setTimeout(exitApp,1500);\n'
        '  }\n'
        '  return true;\n'
        '}\n'
        'window.addEventListener("message",function(ev){\n'
        '  var d=ev&&ev.data;\n'
        '  if(!d)return;\n'
        '  if(d.type==="neon-exit"){\n'
        '    exitApp();\n'
        '  }else if(d.type==="neon-back-handled"||d.type==="neon-back-at-root"){\n'
        '    if(backTimer){clearTimeout(backTimer);backTimer=null;}\n'
        '    lastBackSentAt=0;lastBackSrc="";\n'
        '  }else if(d.type==="neon-back-tier2"){\n'
        '    if(backTimer){clearTimeout(backTimer);backTimer=null;}\n'
        '    lastBackSentAt=0;lastBackSrc="";\n'
        '    if(tier2Stack.length){\n'
        '      loadViaWebViewFetch(tier2Stack.pop(),true);\n'
        '    }\n'
        '  }else if(d.type==="neon-nav-ready"){\n'
        '    navAck=true;\n'
        '    everHadNav=true;\n'
        '    proxyRecoverTries=0;\n'
        '    frameNavReady=true;\n'
        '    if(navPingTimer){clearTimeout(navPingTimer);navPingTimer=null;}\n'
        '  }else if(d.type==="neon-xhr"&&d.id&&d.url){\n'
        '    handleParentXhr(d,ev.source);\n'
        '  }else if(d.type==="neon-storage"){\n'
        '    neonStore.apply(d);\n'
        '  }else if(d.type==="neon-proxy-upstream-error"&&!usingTier2){\n'
        '    bootDiag="proxy_upstream_"+(d.error||"err");\n'
        '    svcTimeoutDiag=bootDiag;\n'
        '    loadViaWebViewFetch(TARGET);\n'
        '  }else if(d.type==="neon-navigate"&&d.url){\n'
        '    loadViaWebViewFetch(d.url,false);\n'
        '  }\n'
        '});\n'
        'window.addEventListener("keydown",function(ev){\n'
        '  var code=ev.keyCode||0;\n'
        '  var keyStr=ev.key||"";\n'
        '  if(code===10182){ev.preventDefault();exitApp();return;}\n'
        '  if(frameReady){\n'
        '    if(frameNavReady&&(code===37||code===38||code===39||code===40||code===13||code===10009||code===27||code===8||code===405||code===406||code===457||code===412||code===413||code===415||code===417||code===19||code===10252||code===10232||code===10233)){\n'
        '      if(ev.preventDefault)ev.preventDefault();\n'
        '      forwardKeyToFrame(code,keyStr,"kd");\n'
        '    }else if(!frameNavReady&&(code===10009||code===27||keyStr==="Back")){\n'
        '      if(ev.preventDefault)ev.preventDefault();\n'
        '      exitApp();\n'
        '    }\n'
        '    return;\n'
        '  }\n'
        '  if(code===10009||code===27||code===8){ev.preventDefault();exitApp();return;}\n'
        '  if(errBox&&errBox.style.display==="block"){\n'
        '    if(code===37||code===38){ev.preventDefault();setErrFocus(errFocusIdx-1);}\n'
        '    else if(code===39||code===40){ev.preventDefault();setErrFocus(errFocusIdx+1);}\n'
        '    else if(code===13||keyStr==="Enter"){\n'
        '      ev.preventDefault();\n'
        '      if(errFocusIdx===0)startBoot();else exitApp();\n'
        '    }\n'
        '  }\n'
        '},true);\n'
        'window.addEventListener("tizenhwkey",function(ev){\n'
        '  if(ev&&ev.keyName==="back"){\n'
        '    if(frameReady&&frameNavReady){forwardKeyToFrame(10009,"Back","hw");}\n'
        '    else{exitApp();}\n'
        '  }\n'
        '});\n'
        'if(btnRetry)btnRetry.onclick=function(){startBoot();};\n'
        'if(btnDirect)btnDirect.onclick=function(){exitApp();};\n'
        'registerRemoteKeys();\n'
        'startBoot();\n'
        '})();\n'
        '</script>\n'
        '</body>\n'
        '</html>\n'
    ).encode('utf-8')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('config.xml', config_xml)
        zf.writestr('index.html', index_html)
        zf.writestr('neon-nav.js', nav_js.encode('utf-8'))
        if proxy_js is not None:
            zf.writestr('service/proxy.js', proxy_js.encode('utf-8'))
        zf.writestr('icon.png', icon_bytes)
    return buf.getvalue()


def _wrap76(b64_text):
    return '\n'.join(b64_text[i:i + 76] for i in range(0, len(b64_text), 76))


def _sha512_b64(data):
    return base64.b64encode(hashlib.sha512(data).digest()).decode('ascii')


def _strip_root_from_chain(cert_chain):
    """Keep [leaf, intermediate...] and exclude self-signed root CAs from KeyInfo."""
    if not cert_chain:
        return []
    out = [cert_chain[0]]
    for cert in cert_chain[1:]:
        if cert is not None and cert != cert_chain[0] and cert.subject != cert.issuer and cert not in out:
            out.append(cert)
    return out


def _build_signature_xml(sig_id, role, prop_digest, file_entries, private_key, cert_chain):
    canon_refs = []
    stored_refs = []
    for uri, payload in file_entries:
        digest = _sha512_b64(payload)
        canon_refs.append(
            f'<Reference URI="{uri}">'
            '<DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha512"></DigestMethod>'
            f'<DigestValue>{digest}</DigestValue>'
            '</Reference>'
        )
        stored_refs.append(
            f'<Reference URI="{uri}">'
            '<DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha512" />'
            f'<DigestValue>{digest}</DigestValue>'
            '</Reference>'
        )

    canon_refs.append(
        '<Reference URI="#prop">'
        '<Transforms><Transform Algorithm="http://www.w3.org/2006/12/xml-c14n11"></Transform></Transforms>'
        '<DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha512"></DigestMethod>'
        f'<DigestValue>{prop_digest}</DigestValue>'
        '</Reference>'
    )
    stored_refs.append(
        '<Reference URI="#prop">'
        '<Transforms><Transform Algorithm="http://www.w3.org/2006/12/xml-c14n11" /></Transforms>'
        '<DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha512" />'
        f'<DigestValue>{prop_digest}</DigestValue>'
        '</Reference>'
    )

    signed_info_for_c14n = (
        '<SignedInfo xmlns="http://www.w3.org/2000/09/xmldsig#">'
        '<CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"></CanonicalizationMethod>'
        '<SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha512"></SignatureMethod>'
        + ''.join(canon_refs)
        + '</SignedInfo>'
    )
    canonical_si = ET.canonicalize(signed_info_for_c14n).encode('utf-8')
    sig_raw = private_key.sign(canonical_si, padding.PKCS1v15(), hashes.SHA512())
    sig_b64 = base64.b64encode(sig_raw).decode('ascii')

    cert_tags = []
    for cert in _strip_root_from_chain(cert_chain):
        der = cert.public_bytes(serialization.Encoding.DER)
        cert_b64 = _wrap76(base64.b64encode(der).decode('ascii'))
        cert_tags.append(f'<X509Certificate>\n{cert_b64}\n</X509Certificate>')

    signed_info_inner = (
        '<SignedInfo>'
        '<CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#" />'
        '<SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha512" />'
        + ''.join(stored_refs)
        + '</SignedInfo>'
    )
    key_info = '<KeyInfo><X509Data>' + ''.join(cert_tags) + '</X509Data></KeyInfo>'
    obj_prop = (
        '<Object Id="prop">'
        '<SignatureProperties xmlns:dsp="http://www.w3.org/2009/xmldsig-properties">'
        f'<SignatureProperty Id="profile" Target="#{sig_id}">'
        '<dsp:Profile URI="http://www.w3.org/ns/widgets-digsig#profile"></dsp:Profile>'
        '</SignatureProperty>'
        f'<SignatureProperty Id="role" Target="#{sig_id}">'
        f'<dsp:Role URI="http://www.w3.org/ns/widgets-digsig#role-{role}"></dsp:Role>'
        '</SignatureProperty>'
        f'<SignatureProperty Id="identifier" Target="#{sig_id}">'
        '<dsp:Identifier></dsp:Identifier>'
        '</SignatureProperty>'
        '</SignatureProperties>'
        '</Object>'
    )
    return (
        f'<Signature Id="{sig_id}" xmlns="http://www.w3.org/2000/09/xmldsig#">'
        + signed_info_inner
        + f'<SignatureValue>{sig_b64}</SignatureValue>'
        + key_info
        + obj_prop
        + '</Signature>'
    ).encode('utf-8')


def sign_wgt_bytes(wgt_bytes, author_key, author_certs, dist_key, dist_certs):
    """Sign a .wgt archive with W3C Widget XML-DSig (author-signature.xml + signature1.xml)."""
    if not author_certs or not dist_certs:
        raise ValueError('Both author and distributor certificate chains are required')

    files = {}
    with zipfile.ZipFile(io.BytesIO(wgt_bytes), 'r') as zin:
        for info in zin.infolist():
            if info.is_dir():
                continue
            name = info.filename.lstrip('/')
            if name == 'author-signature.xml' or re.fullmatch(r'signature\d+\.xml', name):
                continue
            files[name] = zin.read(info.filename)

    entries = [
        (urllib.parse.quote(name, safe='/'), files[name])
        for name in sorted(files.keys())
    ]

    author_xml = _build_signature_xml(
        'AuthorSignature',
        'author',
        AUTHOR_PROP_DIGEST,
        entries,
        author_key,
        author_certs,
    )
    dist_entries = [('author-signature.xml', author_xml)] + entries
    dist_xml = _build_signature_xml(
        'DistributorSignature',
        'distributor',
        DISTRIBUTOR_PROP_DIGEST,
        dist_entries,
        dist_key,
        dist_certs,
    )

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
        zout.writestr('author-signature.xml', author_xml)
        zout.writestr('signature1.xml', dist_xml)
        for name in sorted(files.keys()):
            zout.writestr(name, files[name])
    return out_buf.getvalue()


def verify_wgt_signatures(wgt_bytes, require_chain=False):
    """Cryptographically verify W3C Widget XML-DSig author & distributor signatures."""
    ns = {'ds': 'http://www.w3.org/2000/09/xmldsig#'}
    with zipfile.ZipFile(io.BytesIO(wgt_bytes), 'r') as zf:
        names = set(zf.namelist())
        if 'author-signature.xml' not in names or 'signature1.xml' not in names:
            raise ValueError('Missing author-signature.xml or signature1.xml')

        for sig_file, expected_prop_digest in (
            ('author-signature.xml', AUTHOR_PROP_DIGEST),
            ('signature1.xml', DISTRIBUTOR_PROP_DIGEST),
        ):
            xml_raw = zf.read(sig_file).decode('utf-8')
            root = ET.fromstring(xml_raw)
            si = root.find('ds:SignedInfo', ns)
            sv = root.find('ds:SignatureValue', ns)
            x509_els = root.findall('.//ds:X509Certificate', ns)
            if si is None or sv is None or not x509_els:
                raise ValueError(f'Malformed signature XML in {sig_file}')

            for ref in si.findall('ds:Reference', ns):
                uri = ref.attrib.get('URI', '')
                dv = ref.find('ds:DigestValue', ns)
                got_b64 = ''.join((dv.text or '').split()) if dv is not None else ''
                if uri == '#prop':
                    exp_b64 = expected_prop_digest
                else:
                    member = urllib.parse.unquote(uri)
                    if member not in names:
                        raise ValueError(f'{sig_file}: missing signed file {member}')
                    exp_b64 = base64.b64encode(hashlib.sha512(zf.read(member)).digest()).decode('ascii')
                if got_b64 != exp_b64:
                    raise ValueError(f'{sig_file}: digest mismatch on {uri}')

            certs = [
                x509.load_der_x509_certificate(base64.b64decode(''.join((el.text or '').split())))
                for el in x509_els
            ]
            if require_chain and not _is_valid_signing_chain(certs):
                raise ValueError(f'{sig_file}: invalid or incomplete signing certificate chain ({len(certs)} cert(s))')
            leaf_cert = certs[0]
            si_str = ET.tostring(si, encoding='unicode')
            # Ensure XMLDSig default namespace on <SignedInfo> when parsed with prefix ns0
            si_clean = re.sub(r'<(/?)ns0:', r'<\1', si_str).replace(
                'xmlns:ns0="http://www.w3.org/2000/09/xmldsig#"',
                'xmlns="http://www.w3.org/2000/09/xmldsig#"',
            )
            if 'xmlns="http://www.w3.org/2000/09/xmldsig#"' not in si_clean.split('>', 1)[0]:
                si_clean = si_clean.replace('<SignedInfo', '<SignedInfo xmlns="http://www.w3.org/2000/09/xmldsig#"', 1)
            si_c14n = ET.canonicalize(si_clean).encode('utf-8')
            sig_bytes = base64.b64decode(''.join((sv.text or '').split()))
            leaf_cert.public_key().verify(sig_bytes, si_c14n, padding.PKCS1v15(), hashes.SHA512())
    return True


# ── Certificate Helpers, OAuth Receiver & Tizen Provisioning ─────────────────

_SSL_CTX_CACHE = None


def _build_ssl_context():
    """Create a strict SSLContext enriched with certifi + Windows ROOT/CA stores."""
    global _SSL_CTX_CACHE
    if _SSL_CTX_CACHE is not None:
        return _SSL_CTX_CACHE

    ctx = ssl.create_default_context()
    if certifi is not None:
        try:
            ca_file = certifi.where()
            if ca_file and os.path.isfile(ca_file):
                ctx.load_verify_locations(cafile=ca_file)
        except Exception:
            pass

    if hasattr(ssl, 'enum_certificates'):
        pems = []
        for store in ('ROOT', 'CA'):
            try:
                for cert_der, encoding, trust in ssl.enum_certificates(store):
                    if encoding != 'x509_asn':
                        continue
                    if trust is False:
                        continue
                    try:
                        pems.append(ssl.DER_cert_to_PEM_cert(cert_der))
                    except Exception:
                        continue
            except Exception:
                continue
        if pems:
            blob = '\n'.join(pems)
            try:
                ctx.load_verify_locations(cadata=blob)
            except Exception:
                for pem in pems:
                    try:
                        ctx.load_verify_locations(cadata=pem)
                    except Exception:
                        continue

    _SSL_CTX_CACHE = ctx
    return ctx


def _is_ssl_cert_error(exc):
    if isinstance(exc, ssl.SSLError):
        return True
    reason = getattr(exc, 'reason', None)
    if isinstance(reason, ssl.SSLError):
        return True
    txt = str(exc).upper()
    return 'CERTIFICATE_VERIFY_FAILED' in txt or 'UNABLE TO GET LOCAL ISSUER' in txt


def _windows_schannel_fetch(url, method='GET', headers=None, data=None, timeout=30):
    """Fetch HTTPS via native Windows Schannel (handles AIA intermediate CA downloads)."""
    tmp_path = None
    out_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='neon_http_', suffix='.json')
        out_fd, out_path = tempfile.mkstemp(prefix='neon_http_body_', suffix='.bin')
        os.close(out_fd)
        payload = {
            'url': str(url),
            'method': str(method or ('POST' if data is not None else 'GET')).upper(),
            'headers': dict(headers or {}),
            'body_b64': base64.b64encode(data).decode('ascii') if data else '',
            'timeout_ms': int(max(5, float(timeout)) * 1000),
            'out_path': out_path,
        }
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        p_lit = tmp_path.replace("'", "''")
        ps_script = (
            "$ProgressPreference='SilentlyContinue'; "
            "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
            "try{[Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls13}catch{}; "
            f"$p=Get-Content -Raw -Encoding UTF8 -Path '{p_lit}' | ConvertFrom-Json; "
            "$req=[System.Net.HttpWebRequest]::Create($p.url); "
            "$req.Method=$p.method; $req.Timeout=[int]$p.timeout_ms; $req.ReadWriteTimeout=[int]$p.timeout_ms; "
            "if($p.headers){foreach($pr in $p.headers.PSObject.Properties){"
            "$k=$pr.Name; $v=[string]$pr.Value; "
            "if($k -ieq 'Content-Type'){$req.ContentType=$v} "
            "elseif($k -ieq 'User-Agent'){$req.UserAgent=$v} "
            "elseif($k -ieq 'Accept'){$req.Accept=$v} "
            "else{$req.Headers[$k]=$v}"
            "}}; "
            "if($p.body_b64){$b=[Convert]::FromBase64String($p.body_b64); $req.ContentLength=$b.Length; "
            "$st=$req.GetRequestStream(); $st.Write($b,0,$b.Length); $st.Close()}; "
            "$code=200; $fs=[System.IO.File]::OpenWrite($p.out_path); "
            "try{"
            "try{$resp=$req.GetResponse(); $code=[int]$resp.StatusCode; $rs=$resp.GetResponseStream(); if($rs){$rs.CopyTo($fs); $rs.Close()}; $resp.Close()} "
            "catch [System.Net.WebException]{"
            "$we=$_.Exception; if($we.Response){$code=[int]$we.Response.StatusCode; $rs=$we.Response.GetResponseStream(); if($rs){$rs.CopyTo($fs); $rs.Close()}; $we.Response.Close()} else {throw}"
            "}"
            "} finally {$fs.Close()}; "
            "$out=@{status=$code}; "
            "[Console]::WriteLine(($out|ConvertTo-Json -Compress))"
        )
        kw = {}
        if platform.system() == 'Windows':
            kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-Command', ps_script],
            capture_output=True,
            text=True,
            timeout=max(15, int(float(timeout) + 20)),
            **kw,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or 'Windows Schannel HTTP request failed').strip())
        lines = [ln.strip() for ln in (proc.stdout or '').splitlines() if ln.strip().startswith('{')]
        if not lines:
            raise RuntimeError('Empty response from Windows Schannel HTTP helper')
        res = json.loads(lines[-1])
        status = int(res.get('status', 200))
        with open(out_path, 'rb') as bf:
            body = bf.read()
        if status >= 400:
            raise urllib.error.HTTPError(url, status, f'HTTP {status}', {}, io.BytesIO(body))
        return body
    finally:
        for p in (tmp_path, out_path):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


def _http_read(req_or_url, timeout=30, allow_http_mirror=False):
    """Fetch HTTPS bytes using certifi/OS CA stores, falling back to Windows Schannel on TLS chain gaps."""
    ctx = _build_ssl_context()
    url = req_or_url.full_url if hasattr(req_or_url, 'full_url') else str(req_or_url)
    method = req_or_url.get_method() if hasattr(req_or_url, 'get_method') else 'GET'
    headers = dict(req_or_url.header_items()) if hasattr(req_or_url, 'header_items') else {}
    data = getattr(req_or_url, 'data', None)

    def _open(target):
        try:
            with urllib.request.urlopen(target, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except TypeError:
            with urllib.request.urlopen(target, timeout=timeout) as resp:
                return resp.read()

    try:
        return _open(req_or_url)
    except urllib.error.HTTPError:
        raise
    except Exception as exc:
        if not _is_ssl_cert_error(exc):
            raise
        if platform.system() == 'Windows':
            try:
                return _windows_schannel_fetch(url, method=method, headers=headers, data=data, timeout=timeout)
            except urllib.error.HTTPError:
                raise
            except Exception:
                pass
        if allow_http_mirror and url.startswith('https://download.tizen.org/') and data is None:
            mirror_url = 'http://' + url[len('https://'):]
            return _open(mirror_url)
        raise


def _cert_utc_bounds(cert):
    nb = getattr(cert, 'not_valid_before_utc', None)
    na = getattr(cert, 'not_valid_after_utc', None)
    if nb is None:
        nb = cert.not_valid_before.replace(tzinfo=datetime.timezone.utc)
    if na is None:
        na = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    return nb, na


def cert_is_valid_now(cert):
    now = datetime.datetime.now(datetime.timezone.utc)
    nb, na = _cert_utc_bounds(cert)
    return nb <= now <= na


def cert_covers_duid(cert, duid):
    if not duid:
        return False
    needle = f'URN:tizen:deviceid={duid}'.lower()
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        for uri in san.get_values_for_type(x509.UniformResourceIdentifier):
            if str(uri).strip().lower() == needle:
                return True
    except x509.ExtensionNotFound:
        return False
    return False


def _extract_certs(raw_bytes):
    """Parse one or more X.509 certificates from PEM (even with openssl text dump) or DER."""
    text = raw_bytes.decode('utf-8', errors='ignore') if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes)
    pems = re.findall(
        r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
        text,
        flags=re.DOTALL,
    )
    if pems:
        return [x509.load_pem_x509_certificate(p.encode('ascii')) for p in pems]
    if isinstance(raw_bytes, (bytes, bytearray)) and raw_bytes:
        return [x509.load_der_x509_certificate(bytes(raw_bytes))]
    raise ValueError('No valid X.509 certificate found in response')


def _order_chain(leaf, extras):
    """Order [leaf, intermediate_ca, ...] walking issuer -> subject cleanly."""
    chain = [leaf]
    pool = [c for c in (extras or []) if c is not None and c != leaf]
    cur = leaf
    while cur.issuer != cur.subject:
        nxt = next((c for c in pool if c.subject == cur.issuer), None)
        if nxt is None:
            break
        chain.append(nxt)
        pool.remove(nxt)
        cur = nxt
    for c in pool:
        if c not in chain:
            chain.append(c)
    return chain


def _verify_cert_signed_by(child_cert, parent_cert):
    if child_cert is None or parent_cert is None:
        return False
    if child_cert.issuer != parent_cert.subject:
        return False
    try:
        verify_direct = getattr(child_cert, 'verify_directly_issued_by', None)
        if verify_direct is not None:
            verify_direct(parent_cert)
            return True
    except Exception:
        return False
    try:
        pub = parent_cert.public_key()
        if isinstance(pub, rsa.RSAPublicKey) and child_cert.signature_hash_algorithm is not None:
            pub.verify(
                child_cert.signature,
                child_cert.tbs_certificate_bytes,
                padding.PKCS1v15(),
                child_cert.signature_hash_algorithm,
            )
            return True
        if isinstance(pub, ec.EllipticCurvePublicKey) and child_cert.signature_hash_algorithm is not None:
            pub.verify(
                child_cert.signature,
                child_cert.tbs_certificate_bytes,
                ec.ECDSA(child_cert.signature_hash_algorithm),
            )
            return True
    except Exception:
        return False
    return False


def _is_valid_signing_chain(chain):
    """Verify [leaf, intermediate...] has >=2 non-root certs with valid issuer->subject signatures."""
    clean = _strip_root_from_chain(chain)
    if len(clean) < 2:
        return False
    leaf = clean[0]
    if leaf.subject == leaf.issuer:
        return False
    prev = leaf
    for ca in clean[1:]:
        if ca is None or ca.subject == ca.issuer:
            return False
        if not _verify_cert_signed_by(prev, ca):
            return False
        prev = ca
    return True


def _format_chain_summary(chain):
    if not chain:
        return 'empty'
    parts = []
    for cert in chain:
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        parts.append(cn_attrs[0].value if cn_attrs else cert.subject.rfc4514_string())
    return f'{len(chain)} cert(s): ' + ' -> '.join(parts)


def _encode_multipart(fields, files):
    boundary = '----NEONForm' + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
            f'{v}\r\n'.encode('utf-8')
        )
    for k, (filename, content, ctype) in files.items():
        header = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{k}"; filename="{filename}"\r\n'
            f'Content-Type: {ctype}\r\n\r\n'
        ).encode('utf-8')
        body = content.encode('utf-8') if isinstance(content, str) else bytes(content)
        parts.append(header + body + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode('utf-8'))
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


def load_p12_bundle(p12_path, password=''):
    with open(p12_path, 'rb') as f:
        raw = f.read()
    pwd_bytes = password.encode('utf-8') if password else None
    key, cert, extras = pkcs12.load_key_and_certificates(raw, pwd_bytes)
    if key is None or cert is None:
        raise ValueError(f'Missing key or leaf certificate in {p12_path}')
    return key, _order_chain(cert, extras)


def _try_p12_passwords(p12_path, candidates):
    for pwd in candidates:
        try:
            return load_p12_bundle(p12_path, pwd)
        except ValueError:
            continue
        except Exception:
            continue
    return None


def build_samsung_authorize_url():
    return (
        f'{SAMSUNG_SIGNINGATE_URL}?locale=&clientId={SAMSUNG_CLIENT_ID}'
        f'&redirect_uri={urllib.parse.quote(f"http://localhost:{SAMSUNG_CALLBACK_PORT}{SAMSUNG_CALLBACK_PATH}", safe="")}'
        f'&state={SAMSUNG_STATE}&tokenType=TOKEN'
    )


def _parse_http_request(conn):
    buf = bytearray()
    while b'\r\n\r\n' not in buf and len(buf) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    raw = bytes(buf)
    sep = raw.find(b'\r\n\r\n')
    head_bytes = raw[:sep] if sep >= 0 else raw
    body_bytes = bytearray(raw[sep + 4:] if sep >= 0 else b'')

    head_lines = head_bytes.decode('utf-8', errors='replace').split('\r\n')
    req_parts = head_lines[0].split(' ') if head_lines else []
    method = req_parts[0].upper() if len(req_parts) >= 1 else ''
    path = req_parts[1] if len(req_parts) >= 2 else ''

    content_len = 0
    for line in head_lines[1:]:
        if line.lower().startswith('content-length:'):
            try:
                content_len = int(line.split(':', 1)[1].strip())
            except ValueError:
                content_len = 0
    while len(body_bytes) < content_len and len(body_bytes) < 262144:
        chunk = conn.recv(4096)
        if not chunk:
            break
        body_bytes.extend(chunk)
    return method, path, bytes(body_bytes).decode('utf-8', errors='replace')


def _send_http_response(conn, status, html_body):
    reason = {200: 'OK', 400: 'Bad Request', 404: 'Not Found'}.get(status, 'OK')
    payload = html_body.encode('utf-8')
    resp = (
        f'HTTP/1.1 {status} {reason}\r\n'
        'Content-Type: text/html; charset=utf-8\r\n'
        f'Content-Length: {len(payload)}\r\n'
        'Connection: close\r\n\r\n'
    ).encode('ascii') + payload
    try:
        conn.sendall(resp)
    except OSError:
        pass


def parse_samsung_oauth_callback(method, path, body):
    """Parse SignInGate POST form-urlencoded or GET query OAuth payload."""
    parsed_url = urllib.parse.urlparse(path or '')
    if not parsed_url.path.startswith(SAMSUNG_CALLBACK_PATH):
        return None
    params = {}
    if method == 'POST' and body:
        if body.lstrip().startswith('{'):
            data = json.loads(body)
            if isinstance(data, dict) and data.get('access_token'):
                return data
        for k, v in urllib.parse.parse_qsl(body, keep_blank_values=True):
            params[k] = v
    elif parsed_url.query:
        for k, v in urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True):
            params[k] = v

    state = params.get('state')
    if state is not None and state != SAMSUNG_STATE:
        return None
    code_raw = params.get('code') or ''
    if not code_raw:
        if params.get('access_token'):
            return params
        return None
    token_obj = json.loads(code_raw)
    if not isinstance(token_obj, dict) or not token_obj.get('access_token') or not token_obj.get('userId'):
        return None
    return token_obj


class SamsungCertManager:
    """Discovers, caches, downloads CAs, and provisions Samsung/Tizen certs."""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.cert_dir = os.path.join(self.data_dir, 'samsung_cert')
        self.ca_dir = os.path.join(self.data_dir, 'samsung_ca')
        self.sdk_ca_dir = os.path.join(self.data_dir, 'tizen_sdk_ca')

    @staticmethod
    def _safe_duid(duid):
        return re.sub(r'[^A-Za-z0-9_-]+', '_', str(duid or '').strip()) or 'default'

    def _external_profile_dirs(self):
        home = os.path.expanduser('~')
        appdata = os.environ.get('APPDATA') or os.path.join(home, '.config')
        localappdata = os.environ.get('LOCALAPPDATA') or appdata
        roots = [
            self.cert_dir,
            os.path.join(home, 'tizen-studio-data', 'SamsungCertificate'),
            os.path.join(home, 'SamsungCertificate'),
            os.path.join(appdata, 'Apps2Samsung', 'Certificates'),
            os.path.join(localappdata, 'Apps2Samsung', 'Certificates'),
        ]
        found = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            found.append(root)
            try:
                for entry in os.listdir(root):
                    sub = os.path.join(root, entry)
                    if os.path.isdir(sub):
                        found.append(sub)
            except OSError:
                pass
        return found

    def _bundled_ca_dir(self):
        d = _resource_path('assets', 'samsung_ca')
        if os.path.isdir(d):
            try:
                if any(n.lower().endswith(('.cer', '.crt')) for n in os.listdir(d)):
                    return d
            except OSError:
                pass
        return None

    def _seed_bundled_ca_certs(self):
        """Copy built-in official Samsung CA certs into self.ca_dir when missing (avoids 44 MB stalls)."""
        src_dir = self._bundled_ca_dir()
        if not src_dir:
            return 0
        os.makedirs(self.ca_dir, exist_ok=True)
        copied = 0
        for fname in sorted(os.listdir(src_dir)):
            if not fname.lower().endswith(('.cer', '.crt')):
                continue
            src = os.path.join(src_dir, fname)
            dst = os.path.join(self.ca_dir, fname)
            if not os.path.isfile(dst):
                with open(src, 'rb') as sf:
                    data = sf.read()
                with open(dst, 'wb') as df:
                    df.write(data)
                copied += 1
        return copied

    @staticmethod
    def _parse_extension_info(xml_text):
        """Extract Samsung Certificate Extension (url, version) namespace-agnostically."""
        root = ET.fromstring(xml_text)
        for el in root.iter():
            tag = el.tag.split('}')[-1].lower()
            if tag != 'extension':
                continue
            fields = {}
            for child in list(el):
                cname = child.tag.split('}')[-1].lower()
                fields[cname] = (child.text or '').strip()
            if 'Samsung Certificate Extension' in fields.get('name', ''):
                repo_url = fields.get('repository', '')
                version = fields.get('version', '')
                if repo_url:
                    return repo_url, version
        return '', ''

    @staticmethod
    def _extract_extension_zip(ext_zip_bytes, target_dir):
        """Extract Samsung CA .cer/.crt files cleanly from tizen-certificate-extension archive."""
        stage_dir = target_dir + '.staging_' + uuid.uuid4().hex[:8]
        os.makedirs(stage_dir, exist_ok=True)
        extracted = 0
        try:
            with zipfile.ZipFile(io.BytesIO(ext_zip_bytes)) as outer:
                inner_zips = [n for n in outer.namelist() if n.lower().endswith('.zip')]
                if not inner_zips:
                    raise ValueError('No inner archive inside Samsung Certificate Extension')
                # Prefer cert-add-on zip and org.tizen.common.cert jar first so newer CA resources win
                inner_zips.sort(key=lambda n: (0 if 'cert-add-on' in n.lower() else 1, n))
                for iz_name in inner_zips:
                    with zipfile.ZipFile(io.BytesIO(outer.read(iz_name))) as iz:
                        jars = [n for n in iz.namelist() if n.lower().endswith('.jar')]
                        jars.sort(key=lambda n: (0 if 'org.tizen.common.cert' in n.lower() else 1, n))
                        for jar_name in jars:
                            with zipfile.ZipFile(io.BytesIO(iz.read(jar_name))) as jz:
                                cert_entries = [n for n in jz.namelist() if n.lower().endswith(('.cer', '.crt'))]
                                cert_entries.sort(key=lambda n: (0 if '/ca/' in n.lower() or n.lower().startswith('ca/') else 1, n))
                                for fname in cert_entries:
                                    bname = os.path.basename(fname)
                                    if not bname:
                                        continue
                                    out_path = os.path.join(stage_dir, bname)
                                    if not os.path.isfile(out_path):
                                        with open(out_path, 'wb') as f:
                                            f.write(jz.read(fname))
                                        extracted += 1
                    if extracted and all(os.path.isfile(os.path.join(stage_dir, r)) for r in ('vd_tizen_dev_author_ca.cer', 'vd_tizen_dev_public2.crt')):
                        break
            if not extracted:
                raise ValueError('No CA certificates extracted from Samsung Certificate Extension')
            os.makedirs(target_dir, exist_ok=True)
            for fname in os.listdir(stage_dir):
                os.replace(os.path.join(stage_dir, fname), os.path.join(target_dir, fname))
            return extracted
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    def _try_p12_with_pwd(self, p12_path, candidates):
        for pwd in candidates:
            try:
                key, chain = load_p12_bundle(p12_path, pwd)
                return key, chain, pwd
            except Exception:
                continue
        return None

    def _repair_or_validate_chain(self, key, chain, p12_path, pwd, preferred_names, can_rewrite=False, allow_remote_refresh=True):
        clean = _strip_root_from_chain(chain)
        if _is_valid_signing_chain(clean):
            return clean
        leaf = clean[0]
        ca = self._select_ca_cert(leaf, preferred_names)
        if ca is None:
            try:
                self.ensure_samsung_ca_certs(use_bundled=True)
                ca = self._select_ca_cert(leaf, preferred_names)
            except Exception:
                pass
        if ca is None and allow_remote_refresh:
            try:
                self.ensure_samsung_ca_certs(force_refresh=True)
                ca = self._select_ca_cert(leaf, preferred_names)
            except Exception:
                pass
        if ca is None:
            return None
        repaired = [leaf, ca]
        if not _is_valid_signing_chain(repaired):
            return None
        if can_rewrite and p12_path and key is not None:
            try:
                enc = BestAvailableEncryption(pwd.encode('utf-8')) if pwd else serialization.NoEncryption()
                repaired_p12 = pkcs12.serialize_key_and_certificates(
                    b'UserCertificate',
                    key,
                    leaf,
                    [ca],
                    enc,
                )
                with open(p12_path, 'wb') as f:
                    f.write(repaired_p12)
            except Exception:
                pass
        return repaired

    def find_saved_samsung_certs(self, duid):
        """Return (author_key, author_certs, dist_key, dist_certs, source) if valid for DUID."""
        safe = self._safe_duid(duid)
        for pdir in self._external_profile_dirs():
            author_p12 = os.path.join(pdir, 'author.p12')
            if not os.path.isfile(author_p12):
                continue
            dist_candidates = [
                os.path.join(pdir, f'distributor_{safe}.p12'),
                os.path.join(pdir, 'distributor.p12'),
            ]
            dist_p12 = next((p for p in dist_candidates if os.path.isfile(p)), None)
            if not dist_p12:
                continue

            pw_candidates = ['', TIZEN_LEGACY_DIST_PASS]
            pw_file = os.path.join(pdir, 'password.txt')
            if os.path.isfile(pw_file):
                try:
                    with open(pw_file, 'r', encoding='utf-8') as f:
                        pw_candidates.insert(0, f.read().strip())
                except OSError:
                    pass

            author_res = self._try_p12_with_pwd(author_p12, pw_candidates)
            dist_res = self._try_p12_with_pwd(dist_p12, pw_candidates)
            if not author_res or not dist_res:
                continue

            a_key, a_chain, a_pwd = author_res
            d_key, d_chain, d_pwd = dist_res
            if not cert_is_valid_now(a_chain[0]) or not cert_is_valid_now(d_chain[0]):
                continue
            if duid and not cert_covers_duid(d_chain[0], duid):
                continue

            is_own_dir = os.path.abspath(pdir) == os.path.abspath(self.cert_dir)
            valid_a = self._repair_or_validate_chain(
                a_key,
                a_chain,
                author_p12,
                a_pwd,
                ['vd_tizen_dev_author_ca.cer', 'author_ca.cer'],
                can_rewrite=is_own_dir,
                allow_remote_refresh=is_own_dir,
            )
            valid_d = self._repair_or_validate_chain(
                d_key,
                d_chain,
                dist_p12,
                d_pwd,
                ['vd_tizen_dev_public2.crt', 'public2.crt', 'vd_tizen_dev_partner2.crt'],
                can_rewrite=is_own_dir,
                allow_remote_refresh=is_own_dir,
            )
            if not valid_a or not valid_d:
                continue
            if is_own_dir and dist_p12 != os.path.join(pdir, 'distributor.p12') and valid_d != d_chain:
                try:
                    shutil.copy2(dist_p12, os.path.join(pdir, 'distributor.p12'))
                except OSError:
                    pass
            return {
                'source': pdir,
                'author_key': a_key,
                'author_certs': valid_a,
                'dist_key': d_key,
                'dist_certs': valid_d,
            }
        return None

    def _load_or_init_neon_author_key(self):
        author_p12 = os.path.join(self.cert_dir, 'author.p12')
        pw_file = os.path.join(self.cert_dir, 'password.txt')
        if os.path.isfile(author_p12) and os.path.isfile(pw_file):
            try:
                with open(pw_file, 'r', encoding='utf-8') as f:
                    pwd = f.read().strip()
                key, chain = load_p12_bundle(author_p12, pwd)
                valid_chain = self._repair_or_validate_chain(
                    key,
                    chain,
                    author_p12,
                    pwd,
                    ['vd_tizen_dev_author_ca.cer', 'author_ca.cer'],
                    can_rewrite=True,
                    allow_remote_refresh=False,
                )
                if valid_chain and cert_is_valid_now(valid_chain[0]):
                    return key, valid_chain, pwd, True
                if key is not None:
                    return key, None, pwd, False
            except Exception:
                pass
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pwd = uuid.uuid4().hex[:16]
        return key, None, pwd, False

    def ensure_samsung_ca_certs(self, force_refresh=False, use_bundled=False):
        """Ensure official Samsung VD author/distributor intermediate CA certs are available."""
        req_files = ('vd_tizen_dev_author_ca.cer', 'vd_tizen_dev_public2.crt')
        if use_bundled and not force_refresh:
            self._seed_bundled_ca_certs()
            if all(os.path.isfile(os.path.join(self.ca_dir, f)) for f in req_files):
                return self.ca_dir

        if not force_refresh and all(os.path.isfile(os.path.join(self.ca_dir, f)) for f in req_files):
            return self.ca_dir

        ext_zip_bytes = None
        ext_ver = ''
        repo_url = ''
        try:
            xml_text = _http_read(TIZEN_EXTENSION_INFO_URL, timeout=30, allow_http_mirror=True).decode('utf-8', errors='replace')
            repo_url, ext_ver = self._parse_extension_info(xml_text)
        except Exception:
            repo_url = ''

        download_urls = []
        if repo_url:
            download_urls.append(repo_url)
        if TIZEN_CERT_EXT_FALLBACK_URL not in download_urls:
            download_urls.append(TIZEN_CERT_EXT_FALLBACK_URL)

        last_err = None
        for dl_url in download_urls:
            for _attempt in range(2):
                try:
                    ext_zip_bytes = _http_read(dl_url, timeout=240, allow_http_mirror=True)
                    if ext_zip_bytes:
                        break
                except Exception as exc:
                    last_err = exc
                    ext_zip_bytes = None
            if ext_zip_bytes:
                break

        if not ext_zip_bytes:
            if not force_refresh and self._seed_bundled_ca_certs() > 0:
                if all(os.path.isfile(os.path.join(self.ca_dir, f)) for f in req_files):
                    return self.ca_dir
            raise last_err or RuntimeError('Failed to download Samsung Certificate Extension')

        self._extract_extension_zip(ext_zip_bytes, self.ca_dir)
        marker = f'{ext_ver or "remote"}|{repo_url or TIZEN_CERT_EXT_FALLBACK_URL}'
        try:
            with open(os.path.join(self.ca_dir, '.extension'), 'w', encoding='utf-8') as mf:
                mf.write(marker)
        except OSError:
            pass
        return self.ca_dir

    def _select_ca_cert(self, leaf_cert, preferred_names, extra_certs=None):
        for ca in (extra_certs or ()):
            try:
                if ca is not None and ca.subject != ca.issuer and _verify_cert_signed_by(leaf_cert, ca):
                    return ca
            except Exception:
                continue

        search_dirs = []
        if os.path.isdir(self.ca_dir):
            search_dirs.append((self.ca_dir, False))
        bundled_dir = self._bundled_ca_dir()
        if bundled_dir and os.path.abspath(bundled_dir) != os.path.abspath(self.ca_dir):
            search_dirs.append((bundled_dir, True))

        for sdir, from_bundled in search_dirs:
            candidates = []
            for pref in preferred_names:
                p = os.path.join(sdir, pref)
                if os.path.isfile(p):
                    candidates.append(p)
            try:
                for fname in sorted(os.listdir(sdir)):
                    if fname.lower().endswith(('.cer', '.crt')):
                        p = os.path.join(sdir, fname)
                        if p not in candidates:
                            candidates.append(p)
            except OSError:
                continue

            for p in candidates:
                try:
                    with open(p, 'rb') as f:
                        raw_ca = f.read()
                    for ca in _extract_certs(raw_ca):
                        if ca.subject != ca.issuer and _verify_cert_signed_by(leaf_cert, ca):
                            if from_bundled:
                                try:
                                    os.makedirs(self.ca_dir, exist_ok=True)
                                    with open(os.path.join(self.ca_dir, os.path.basename(p)), 'wb') as df:
                                        df.write(raw_ca)
                                except OSError:
                                    pass
                            return ca
                except Exception:
                    continue
        return None

    def _resolve_ca_for_leaf(self, leaf_cert, preferred_names, extra_certs=None, label='Samsung'):
        ca = self._select_ca_cert(leaf_cert, preferred_names, extra_certs=extra_certs)
        if ca is None:
            try:
                self.ensure_samsung_ca_certs(force_refresh=True)
            except Exception as exc:
                raise RuntimeError(
                    f'Missing verified {label} intermediate CA for issuer "{leaf_cert.issuer.rfc4514_string()}" '
                    f'(CA refresh failed: {exc})'
                ) from exc
            ca = self._select_ca_cert(leaf_cert, preferred_names, extra_certs=extra_certs)
        if ca is None or not _is_valid_signing_chain([leaf_cert, ca]):
            raise RuntimeError(
                f'Could not find verified {label} intermediate CA matching issuer "{leaf_cert.issuer.rfc4514_string()}". '
                'Certificate chain would be rejected by TV (118/-12).'
            )
        return ca

    def _open_oauth_listener(self):
        try:
            srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            srv.bind(('::', SAMSUNG_CALLBACK_PORT))
            srv.listen(8)
            srv.settimeout(0.5)
            return srv
        except OSError:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('127.0.0.1', SAMSUNG_CALLBACK_PORT))
            srv.listen(8)
            srv.settimeout(0.5)
            return srv

    def sign_in_and_provision(self, duid, open_browser=True, timeout=240.0, stop_event=None, url_cb=None):
        """Perform Samsung Account OAuth2 sign-in and mint VD Author + DUID Distributor certs."""
        if not duid:
            raise ValueError('TV DUID is required to create a Samsung TV certificate')

        self.ensure_samsung_ca_certs(use_bundled=True)
        auth_url = build_samsung_authorize_url()
        srv = self._open_oauth_listener()
        token_info = None
        try:
            if url_cb:
                url_cb(auth_url)
            if open_browser:
                try:
                    webbrowser.open(auth_url)
                except Exception:
                    pass

            deadline = time.monotonic() + float(timeout)
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    raise RuntimeError('Samsung sign-in cancelled')
                try:
                    conn, _addr = srv.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(5.0)
                    method, path, body = _parse_http_request(conn)
                    try:
                        parsed = parse_samsung_oauth_callback(method, path, body)
                    except Exception:
                        parsed = None
                    if parsed is not None:
                        _send_http_response(
                            conn,
                            200,
                            '<!DOCTYPE html><html><meta charset="utf-8"><body style="background:#081016;color:#72ffab;'
                            'font-family:Segoe UI,sans-serif;display:flex;align-items:center;justify-content:center;height:90vh">'
                            '<div style="text-align:center;border:1px solid #2b664b;padding:32px 42px;border-radius:16px;background:#0e1920">'
                            '<h2>✓ Samsung Account Sign-In Complete</h2>'
                            '<p style="color:#b8d0dc">Вход выполнен! Вы можете закрыть эту вкладку и вернуться в NEON.</p>'
                            '</div></body></html>',
                        )
                        token_info = parsed
                        break
                    is_cb = urllib.parse.urlparse(path or '').path.startswith(SAMSUNG_CALLBACK_PATH)
                    _send_http_response(
                        conn,
                        400 if is_cb else 404,
                        '<h3>Invalid Samsung OAuth callback</h3>' if is_cb else '<h3>Not found</h3>',
                    )
        finally:
            try:
                srv.close()
            except OSError:
                pass

        if token_info is None:
            raise TimeoutError('Samsung account sign-in timed out before callback arrived')

        return self.provision_with_oauth_token(duid, token_info)

    def provision_with_oauth_token(self, duid, token_info):
        """Exchange Samsung OAuth token + CSRs at svdca.samsungqbe.com and save .p12 bundle."""
        self.ensure_samsung_ca_certs(use_bundled=True)
        access_token = str(token_info['access_token'])
        user_id = str(token_info['userId'])
        user_email = str(token_info.get('inputEmailID') or token_info.get('email') or 'user@neon.local')

        os.makedirs(self.cert_dir, exist_ok=True)
        author_key, existing_author_chain, pwd, had_author = self._load_or_init_neon_author_key()

        if had_author and existing_author_chain and cert_is_valid_now(existing_author_chain[0]) and _is_valid_signing_chain(existing_author_chain):
            author_chain = existing_author_chain
        else:
            author_csr = (
                x509.CertificateSigningRequestBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'NEON')]))
                .sign(author_key, hashes.SHA256())
                .public_bytes(serialization.Encoding.PEM)
            )
            a_body, a_ctype = _encode_multipart(
                {'access_token': access_token, 'user_id': user_id, 'platform': 'VD'},
                {'csr': ('author.csr', author_csr, 'application/octet-stream')},
            )
            req_a = urllib.request.Request(
                SAMSUNG_AUTHOR_V3_URL,
                data=a_body,
                headers={'Content-Type': a_ctype},
                method='POST',
            )
            try:
                signed_author_raw = _http_read(req_a, timeout=30)
            except urllib.error.HTTPError as exc:
                err_txt = exc.read().decode('utf-8', errors='replace')
                raise RuntimeError(f'Samsung Author CSR failed ({exc.code}): {err_txt}') from exc

            author_certs_resp = _extract_certs(signed_author_raw)
            author_leaf = author_certs_resp[0]
            author_ca = self._resolve_ca_for_leaf(
                author_leaf,
                ['vd_tizen_dev_author_ca.cer', 'author_ca.cer'],
                extra_certs=author_certs_resp[1:],
                label='Samsung Author',
            )
            author_chain = [author_leaf, author_ca]

            enc = BestAvailableEncryption(pwd.encode('utf-8')) if pwd else serialization.NoEncryption()
            author_p12 = pkcs12.serialize_key_and_certificates(
                b'UserCertificate',
                author_key,
                author_leaf,
                [author_ca],
                enc,
            )
            with open(os.path.join(self.cert_dir, 'author.p12'), 'wb') as f:
                f.write(author_p12)
            with open(os.path.join(self.cert_dir, 'password.txt'), 'w', encoding='utf-8') as f:
                f.write(pwd)

        # Mint DUID-bound Distributor certificate (reuse same author_key as Apps2Samsung)
        dist_key = author_key
        san = x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier('URN:tizen:packageid='),
            x509.UniformResourceIdentifier(f'URN:tizen:deviceid={duid}'),
        ])
        dist_csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, 'TizenSDK'),
                x509.NameAttribute(NameOID.EMAIL_ADDRESS, user_email),
            ]))
            .add_extension(san, critical=False)
            .sign(dist_key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
        )

        # Register distributor profile via v1 then fetch signed cert via v3
        d1_body, d1_ctype = _encode_multipart(
            {
                'access_token': access_token,
                'user_id': user_id,
                'privilege_level': 'public',
                'developer_type': 'Individual',
                'platform': 'VD',
            },
            {'csr': ('distributor.csr', dist_csr, 'application/octet-stream')},
        )
        req_d1 = urllib.request.Request(SAMSUNG_DIST_V1_URL, data=d1_body, headers={'Content-Type': d1_ctype}, method='POST')
        try:
            profile_xml = _http_read(req_d1, timeout=30)
        except urllib.error.HTTPError as exc:
            err_txt = exc.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'Samsung Distributor v1 failed ({exc.code}): {err_txt}') from exc

        d3_body, d3_ctype = _encode_multipart(
            {
                'access_token': access_token,
                'user_id': user_id,
                'privilege_level': 'public',
                'developer_type': 'Individual',
                'platform': 'VD',
            },
            {'csr': ('distributor.csr', dist_csr, 'application/octet-stream')},
        )
        req_d3 = urllib.request.Request(SAMSUNG_DIST_V3_URL, data=d3_body, headers={'Content-Type': d3_ctype}, method='POST')
        try:
            signed_dist_raw = _http_read(req_d3, timeout=30)
        except urllib.error.HTTPError as exc:
            err_txt = exc.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'Samsung Distributor v3 failed ({exc.code}): {err_txt}') from exc

        dist_certs_resp = _extract_certs(signed_dist_raw)
        dist_leaf = dist_certs_resp[0]
        dist_ca = self._resolve_ca_for_leaf(
            dist_leaf,
            ['vd_tizen_dev_public2.crt', 'public2.crt', 'vd_tizen_dev_partner2.crt'],
            extra_certs=dist_certs_resp[1:],
            label='Samsung Distributor',
        )
        dist_chain = [dist_leaf, dist_ca]

        safe = self._safe_duid(duid)
        enc = BestAvailableEncryption(pwd.encode('utf-8')) if pwd else serialization.NoEncryption()
        dist_p12 = pkcs12.serialize_key_and_certificates(
            b'UserCertificate',
            dist_key,
            dist_leaf,
            [dist_ca],
            enc,
        )
        with open(os.path.join(self.cert_dir, f'distributor_{safe}.p12'), 'wb') as f:
            f.write(dist_p12)
        with open(os.path.join(self.cert_dir, 'distributor.p12'), 'wb') as f:
            f.write(dist_p12)
        if profile_xml:
            with open(os.path.join(self.cert_dir, f'device-profile_{safe}.xml'), 'wb') as f:
                f.write(profile_xml)

        return {
            'source': self.cert_dir,
            'author_key': author_key,
            'author_certs': author_chain,
            'dist_key': dist_key,
            'dist_certs': dist_chain,
        }

    def ensure_tizen_sdk_certs(self):
        """Download Tizen SDK certificate-generator assets & mint local Tizen cert (pre-2023 TVs)."""
        prefix = 'data/tools/certificate-generator/certificates/'
        dev_ca_path = os.path.join(self.sdk_ca_dir, 'developer', 'tizen-developer-ca.cer')
        dev_key_path = os.path.join(self.sdk_ca_dir, 'developer', 'tizen-developer-ca-privatekey.pem')
        dist_legacy_path = os.path.join(self.sdk_ca_dir, 'distributor', 'tizen-distributor-signer.p12')
        dist_new_path = os.path.join(self.sdk_ca_dir, 'distributor', 'sdk-public', 'tizen-distributor-signer-new.p12')

        if not (os.path.isfile(dev_ca_path) and os.path.isfile(dev_key_path)):
            archive_bytes = _http_read(TIZEN_SDK_CERT_GEN_URL, timeout=45, allow_http_mirror=True)
            os.makedirs(self.sdk_ca_dir, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                for member in zf.namelist():
                    if member.startswith(prefix) and not member.endswith('/'):
                        rel = member[len(prefix):]
                        out_path = os.path.join(self.sdk_ca_dir, *rel.split('/'))
                        os.makedirs(os.path.dirname(out_path), exist_ok=True)
                        with open(out_path, 'wb') as f:
                            f.write(zf.read(member))

        with open(dev_ca_path, 'rb') as f:
            ca_cert = _extract_certs(f.read())[0]
        with open(dev_key_path, 'rb') as f:
            raw_key_text = f.read().decode('utf-8', errors='ignore')
        m_key = re.search(
            r'-----BEGIN (?:RSA |ENCRYPTED )?PRIVATE KEY-----.*?-----END (?:RSA |ENCRYPTED )?PRIVATE KEY-----',
            raw_key_text,
            flags=re.DOTALL,
        )
        if not m_key:
            raise ValueError('Tizen developer CA private key block not found')
        ca_key = serialization.load_pem_private_key(
            m_key.group(0).encode('ascii'),
            password=TIZEN_DEV_CA_KEY_PASS,
        )

        # Build local Author cert issued by Tizen Developers CA
        author_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        author_leaf = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'NEON')]))
            .issuer_name(ca_cert.subject)
            .public_key(author_key.public_key())
            .serial_number(int(time.time() * 1000))
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.EMAIL_PROTECTION]),
                critical=False,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(author_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )

        # Load ready-made SDK public distributor signer (try new 3DES first, then legacy)
        dist_res = None
        for p12_cand in (dist_legacy_path, dist_new_path):
            if os.path.isfile(p12_cand):
                dist_res = _try_p12_passwords(p12_cand, [TIZEN_LEGACY_DIST_PASS, ''])
                if dist_res is not None:
                    break
        if dist_res is None:
            raise ValueError('Could not load Tizen SDK distributor signer (OpenSSL legacy RC2 unsupported or missing)')

        d_key, d_chain = dist_res
        return {
            'source': 'tizen-sdk-local',
            'author_key': author_key,
            'author_certs': [author_leaf, ca_cert],
            'dist_key': d_key,
            'dist_certs': d_chain,
        }
