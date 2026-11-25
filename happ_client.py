"""Happ-compatible proxy, subscription, routing, ping, and Xray/sing-box engine core."""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import platform
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import vless

try:
    import certifi
except ImportError:
    certifi = None


HAPP_USER_AGENT = 'Happ/4.8.6/Windows'
HAPP_DESKTOP_RELEASES_URL = 'https://github.com/Happ-proxy/happ-desktop/releases/latest'
DEFAULT_DNS_SERVER = '1.1.1.1'
DEFAULT_SOCKS_PORT = 10808
DEFAULT_HTTP_PORT = 10809

SUPPORTED_SCHEMES = ('vless', 'vmess', 'trojan', 'ss', 'hy2', 'hysteria2', 'socks', 'socks5')
ENCRYPTED_HAPP_RE = re.compile(r'^happ://crypt\d*/', re.IGNORECASE)
_RU_BYPASS_SUFFIXES = ('.ru', '.su', '.xn--p1ai', '.by', '.kz', '.tm')


def _b64decode_str(data):
    txt = str(data or '').strip()
    if not txt:
        raise ValueError('Empty base64 payload.')
    txt = txt.replace('-', '+').replace('_', '/')
    pad = (-len(txt)) % 4
    if pad:
        txt += '=' * pad
    raw = base64.b64decode(txt.encode('ascii'), validate=False)
    return raw.decode('utf-8', errors='replace')


def _safe_unquote(text):
    try:
        return urllib.parse.unquote(str(text or ''))
    except Exception:
        return str(text or '')


def _clean_label(text, fallback=''):
    val = _safe_unquote(text).replace('\r', ' ').replace('\n', ' ').strip()
    return val or fallback


def _stable_id(seed):
    return hashlib.sha256(str(seed or '').encode('utf-8', errors='replace')).hexdigest()[:16]


def _extract_flag(name):
    if not name:
        return ''
    chars = list(name)
    for i in range(len(chars) - 1):
        c1, c2 = ord(chars[i]), ord(chars[i + 1])
        if 0x1F1E6 <= c1 <= 0x1F1FF and 0x1F1E6 <= c2 <= 0x1F1FF:
            return chars[i] + chars[i + 1]
    return ''


def _validate_host_port(host, port):
    host = str(host or '').strip()
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    if not host:
        raise ValueError('Server host is empty.')
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError('Invalid server port.') from None
    if not (1 <= p <= 65535):
        raise ValueError('Server port out of range.')
    if any(ord(c) < 32 for c in host):
        raise ValueError('Invalid host characters.')
    return host, p


def _parse_bool(val):
    if isinstance(val, bool):
        return val
    return str(val or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _decode_maybe_base64_value(raw):
    val = str(raw or '').strip()
    if not val:
        return ''
    if val.lower().startswith('base64:'):
        payload = val.split(':', 1)[1].strip()
        try:
            return _b64decode_str(payload).strip()
        except Exception:
            return payload
    return _safe_unquote(val).strip()


def _format_bytes_gb(num):
    try:
        val = float(num)
    except (TypeError, ValueError):
        return '0 B'
    if val < 1024:
        return f'{int(val)} B'
    if val < 1024 ** 2:
        return f'{val / 1024:.1f} KB'
    if val < 1024 ** 3:
        return f'{val / (1024 ** 2):.1f} MB'
    if val < 1024 ** 4:
        return f'{val / (1024 ** 3):.2f} GB'
    return f'{val / (1024 ** 4):.2f} TB'


def parse_userinfo(header_value):
    info = {}
    txt = str(header_value or '').strip()
    if not txt:
        return {}
    for part in re.split(r'[;,\s]+', txt):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        k = k.strip().lower()
        v = v.strip()
        try:
            info[k] = int(float(v))
        except ValueError:
            pass
    if not info:
        return {}
    upload = max(0, info.get('upload', 0))
    download = max(0, info.get('download', 0))
    used = upload + download
    total = max(0, info.get('total', 0))
    expire = max(0, info.get('expire', 0))
    remaining = max(0, total - used) if total > 0 else None
    pct = min(100, int(round((used / total) * 100))) if total > 0 else None
    expire_str = ''
    if expire > 0:
        try:
            expire_str = time.strftime('%d.%m.%Y', time.localtime(expire))
        except Exception:
            expire_str = ''
    parts = []
    if total > 0:
        parts.append(f'{_format_bytes_gb(used)} / {_format_bytes_gb(total)}')
    elif used > 0:
        parts.append(f'Used {_format_bytes_gb(used)}')
    if expire_str:
        parts.append(f'exp {expire_str}')
    return {
        'upload': upload,
        'download': download,
        'used': used,
        'total': total,
        'remaining': remaining,
        'percent': pct,
        'expire': expire,
        'expire_str': expire_str,
        'summary': ' · '.join(parts),
    }


def is_encrypted_happ_link(text):
    s = str(text or '').strip()
    return bool(ENCRYPTED_HAPP_RE.match(s))


def parse_happ_routing_profile(payload, activate=True):
    """Decode happ://routing/[on]add/<base64_or_json> or dict into normalized routing dict."""
    if isinstance(payload, dict):
        data = dict(payload)
    else:
        txt = str(payload or '').strip()
        for prefix in ('happ://routing/onadd/', 'happ://routing/add/'):
            if txt.lower().startswith(prefix):
                activate = ('onadd' in prefix) or activate
                txt = txt[len(prefix):].strip()
                break
        txt = _safe_unquote(txt)
        if txt.startswith('{'):
            data = json.loads(txt)
        else:
            decoded = _b64decode_str(txt).strip()
            data = json.loads(decoded)
    if not isinstance(data, dict):
        raise ValueError('Invalid Happ routing profile payload.')

    def _norm_list(val):
        if isinstance(val, list):
            items = [str(x).strip() for x in val]
        elif isinstance(val, str):
            items = [x.strip() for x in re.split(r'[\r\n,;]+', val)]
        else:
            items = []
        return [x for x in items if x]

    name = str(data.get('Name') or data.get('name') or 'Happ Routing').strip()
    global_proxy = _parse_bool(data.get('GlobalProxy', data.get('global_proxy', True)))
    profile = {
        'id': _stable_id(json.dumps(data, sort_keys=True, ensure_ascii=False)),
        'name': name,
        'global_proxy': global_proxy,
        'domain_strategy': str(data.get('DomainStrategy') or 'IPIfNonMatch'),
        'remote_dns_ip': str(data.get('RemoteDNSIP') or '').strip(),
        'remote_dns_domain': str(data.get('RemoteDNSDomain') or '').strip(),
        'domestic_dns_ip': str(data.get('DomesticDNSIP') or '').strip(),
        'domestic_dns_domain': str(data.get('DomesticDNSDomain') or '').strip(),
        'direct_domains': _norm_list(data.get('DirectDomains') or data.get('direct_domains')),
        'proxy_domains': _norm_list(data.get('ProxyDomains') or data.get('proxy_domains')),
        'block_domains': _norm_list(data.get('BlockDomains') or data.get('block_domains')),
        'direct_ips': _norm_list(data.get('DirectIps') or data.get('direct_ips')),
        'proxy_ips': _norm_list(data.get('ProxyIps') or data.get('proxy_ips')),
        'block_ips': _norm_list(data.get('BlockIps') or data.get('block_ips')),
        'activate': bool(activate),
    }
    return profile


def unwrap_happ_link(raw):
    """Unwrap happ://add/... or detect happ://routing/... or happ://crypt..."""
    s = str(raw or '').strip()
    low = s.lower()
    if is_encrypted_happ_link(s):
        return {'kind': 'encrypted_happ', 'uri': s}
    if low.startswith('happ://routing/onadd/') or low.startswith('happ://routing/add/'):
        profile = parse_happ_routing_profile(s)
        return {'kind': 'routing', 'profile': profile}
    if low.startswith('happ://add/'):
        inner = s[len('happ://add/'):].strip()
        unq = _safe_unquote(inner).strip()
        if unq.startswith(('http://', 'https://', 'vless://', 'vmess://', 'trojan://', 'ss://', 'hy2://', 'hysteria2://', 'socks://', 'socks5://', '{', '[')):
            return {'kind': 'unwrapped', 'value': unq}
        try:
            dec = _b64decode_str(inner).strip()
            if dec:
                return {'kind': 'unwrapped', 'value': dec}
        except Exception:
            pass
        return {'kind': 'unwrapped', 'value': unq}
    if low.startswith('happ://open'):
        parsed = urllib.parse.urlsplit(s)
        qs = dict(urllib.parse.parse_qsl(parsed.query))
        target = qs.get('url') or qs.get('link') or qs.get('sub') or ''
        if target:
            return {'kind': 'unwrapped', 'value': _safe_unquote(target)}
    return {'kind': 'raw', 'value': s}


def _parse_vless_uri(raw_uri):
    uri = urllib.parse.urlsplit(raw_uri.strip())
    host, port = _validate_host_port(uri.hostname, uri.port or 443)
    user = _safe_unquote(uri.username or '').strip()
    if not user:
        raise ValueError('VLESS UUID is required.')
    q = dict(urllib.parse.parse_qsl(uri.query, keep_blank_values=True))
    security = (q.get('security') or 'none').strip().lower()
    if security not in ('none', 'tls', 'reality'):
        security = 'tls' if q.get('sni') else 'none'
    transport = (q.get('type') or 'tcp').strip().lower()
    if transport == 'splithttp':
        transport = 'xhttp'
    name = _clean_label(uri.fragment, f'{host}:{port}')
    extra_obj = None
    if q.get('extra'):
        try:
            extra_obj = json.loads(_safe_unquote(q['extra']))
        except Exception:
            extra_obj = None
    params = {
        'uuid': user,
        'flow': (q.get('flow') or '').strip(),
        'encryption': (q.get('encryption') or 'none').strip() or 'none',
        'security': security,
        'transport': transport,
        'sni': (q.get('sni') or q.get('peer') or host).strip(),
        'fp': (q.get('fp') or ('chrome' if security in ('tls', 'reality') else '')).strip(),
        'alpn': [a.strip() for a in (q.get('alpn') or '').split(',') if a.strip()],
        'pbk': (q.get('pbk') or '').strip(),
        'sid': (q.get('sid') or '').strip(),
        'spx': _safe_unquote(q.get('spx') or ''),
        'host': _safe_unquote(q.get('host') or ''),
        'path': _safe_unquote(q.get('path') or '/'),
        'service_name': _safe_unquote(q.get('serviceName') or q.get('servicename') or ''),
        'mode': (q.get('mode') or '').strip(),
        'header_type': (q.get('headerType') or 'none').strip().lower(),
        'seed': _safe_unquote(q.get('seed') or ''),
        'allow_insecure': _parse_bool(q.get('allowInsecure') or q.get('insecure')),
        'extra': extra_obj,
    }
    tag_bits = ['VLESS']
    if security == 'reality':
        tag_bits.append('Reality')
    elif security == 'tls':
        tag_bits.append('TLS')
    if transport not in ('tcp', 'raw'):
        tag_bits.append(transport.upper())
    return {
        'id': _stable_id(raw_uri),
        'protocol': 'vless',
        'name': name,
        'flag': _extract_flag(name),
        'host': host,
        'port': port,
        'security': security,
        'transport': transport,
        'summary': ' · '.join(tag_bits),
        'params': params,
        'raw': raw_uri.strip(),
        'latency_ms': None,
    }


def _parse_vmess_uri(raw_uri):
    s = raw_uri.strip()
    body = s[len('vmess://'):]
    # Try v2rayN base64 JSON first
    if '@' not in body.split('?', 1)[0]:
        try:
            decoded = _b64decode_str(body.split('#', 1)[0])
            obj = json.loads(decoded)
            if isinstance(obj, dict) and (obj.get('add') or obj.get('host')):
                host, port = _validate_host_port(obj.get('add') or obj.get('host'), obj.get('port'))
                uid = str(obj.get('id') or '').strip()
                if not uid:
                    raise ValueError('VMess UUID missing.')
                net = str(obj.get('net') or 'tcp').strip().lower()
                if net == 'splithttp':
                    net = 'xhttp'
                tls_raw = str(obj.get('tls') or '').strip().lower()
                security = 'tls' if tls_raw in ('tls', '1', 'true') else 'none'
                name = _clean_label(obj.get('ps'), f'{host}:{port}')
                alpn_raw = obj.get('alpn') or ''
                alpn = alpn_raw if isinstance(alpn_raw, list) else [a.strip() for a in str(alpn_raw).split(',') if a.strip()]
                params = {
                    'uuid': uid,
                    'alter_id': int(obj.get('aid') or 0),
                    'cipher': str(obj.get('scy') or obj.get('cipher') or 'auto').strip(),
                    'security': security,
                    'transport': net,
                    'sni': str(obj.get('sni') or obj.get('host') or host).strip(),
                    'fp': str(obj.get('fp') or ('chrome' if security == 'tls' else '')).strip(),
                    'alpn': alpn,
                    'host': str(obj.get('host') or '').strip(),
                    'path': str(obj.get('path') or '/').strip() or '/',
                    'service_name': str(obj.get('path') or '') if net == 'grpc' else '',
                    'header_type': str(obj.get('type') or 'none').strip().lower(),
                    'allow_insecure': _parse_bool(obj.get('allowInsecure') or obj.get('skip-cert-verify')),
                }
                return {
                    'id': _stable_id(s),
                    'protocol': 'vmess',
                    'name': name,
                    'flag': _extract_flag(name),
                    'host': host,
                    'port': port,
                    'security': security,
                    'transport': net,
                    'summary': f'VMess · {net.upper()}' + (' · TLS' if security == 'tls' else ''),
                    'params': params,
                    'raw': s,
                    'latency_ms': None,
                }
        except ValueError:
            raise
        except Exception:
            pass

    uri = urllib.parse.urlsplit(s)
    host, port = _validate_host_port(uri.hostname, uri.port or 443)
    uid = _safe_unquote(uri.username or '').strip()
    if not uid:
        raise ValueError('VMess UUID missing.')
    q = dict(urllib.parse.parse_qsl(uri.query, keep_blank_values=True))
    net = (q.get('type') or 'tcp').strip().lower()
    if net == 'splithttp':
        net = 'xhttp'
    security = (q.get('security') or ('tls' if q.get('sni') else 'none')).strip().lower()
    name = _clean_label(uri.fragment, f'{host}:{port}')
    params = {
        'uuid': uid,
        'alter_id': int(q.get('aid') or 0),
        'cipher': (q.get('encryption') or 'auto').strip(),
        'security': security,
        'transport': net,
        'sni': (q.get('sni') or host).strip(),
        'fp': (q.get('fp') or ('chrome' if security == 'tls' else '')).strip(),
        'alpn': [a.strip() for a in (q.get('alpn') or '').split(',') if a.strip()],
        'host': _safe_unquote(q.get('host') or ''),
        'path': _safe_unquote(q.get('path') or '/'),
        'service_name': _safe_unquote(q.get('serviceName') or ''),
        'header_type': (q.get('headerType') or 'none').strip().lower(),
        'allow_insecure': _parse_bool(q.get('allowInsecure') or q.get('insecure')),
    }
    return {
        'id': _stable_id(s),
        'protocol': 'vmess',
        'name': name,
        'flag': _extract_flag(name),
        'host': host,
        'port': port,
        'security': security,
        'transport': net,
        'summary': f'VMess · {net.upper()}' + (' · TLS' if security == 'tls' else ''),
        'params': params,
        'raw': s,
        'latency_ms': None,
    }


def _parse_trojan_uri(raw_uri):
    uri = urllib.parse.urlsplit(raw_uri.strip())
    host, port = _validate_host_port(uri.hostname, uri.port or 443)
    password = _safe_unquote(uri.username or '')
    if not password:
        raise ValueError('Trojan password is required.')
    q = dict(urllib.parse.parse_qsl(uri.query, keep_blank_values=True))
    security = (q.get('security') or 'tls').strip().lower()
    transport = (q.get('type') or 'tcp').strip().lower()
    if transport == 'splithttp':
        transport = 'xhttp'
    name = _clean_label(uri.fragment, f'{host}:{port}')
    params = {
        'password': password,
        'security': security,
        'transport': transport,
        'sni': (q.get('sni') or q.get('peer') or host).strip(),
        'fp': (q.get('fp') or ('chrome' if security in ('tls', 'reality') else '')).strip(),
        'alpn': [a.strip() for a in (q.get('alpn') or '').split(',') if a.strip()],
        'pbk': (q.get('pbk') or '').strip(),
        'sid': (q.get('sid') or '').strip(),
        'spx': _safe_unquote(q.get('spx') or ''),
        'host': _safe_unquote(q.get('host') or ''),
        'path': _safe_unquote(q.get('path') or '/'),
        'service_name': _safe_unquote(q.get('serviceName') or ''),
        'allow_insecure': _parse_bool(q.get('allowInsecure') or q.get('insecure')),
    }
    return {
        'id': _stable_id(raw_uri),
        'protocol': 'trojan',
        'name': name,
        'flag': _extract_flag(name),
        'host': host,
        'port': port,
        'security': security,
        'transport': transport,
        'summary': f'Trojan · {security.upper()}' + (f' · {transport.upper()}' if transport not in ('tcp', 'raw') else ''),
        'params': params,
        'raw': raw_uri.strip(),
        'latency_ms': None,
    }


def _parse_ss_uri(raw_uri):
    s = raw_uri.strip()
    frag = ''
    main = s[len('ss://'):]
    if '#' in main:
        main, frag = main.split('#', 1)
    query_str = ''
    if '?' in main:
        main, query_str = main.split('?', 1)
    main = main.rstrip('/')
    if '@' in main:
        userinfo, addr = main.rsplit('@', 1)
        if ':' not in userinfo:
            userinfo = _b64decode_str(userinfo)
        if ':' not in userinfo:
            raise ValueError('Invalid Shadowsocks credentials.')
        method, password = userinfo.split(':', 1)
    else:
        decoded = _b64decode_str(main)
        if '@' not in decoded or ':' not in decoded.rsplit('@', 1)[0]:
            raise ValueError('Invalid legacy Shadowsocks link.')
        userinfo, addr = decoded.rsplit('@', 1)
        method, password = userinfo.split(':', 1)
    # Parse host:port supporting IPv6 [::1]:8388
    parsed_addr = urllib.parse.urlsplit('ss://' + addr)
    host, port = _validate_host_port(parsed_addr.hostname, parsed_addr.port)
    name = _clean_label(frag, f'{host}:{port}')
    q = dict(urllib.parse.parse_qsl(query_str, keep_blank_values=True))
    return {
        'id': _stable_id(s),
        'protocol': 'shadowsocks',
        'name': name,
        'flag': _extract_flag(name),
        'host': host,
        'port': port,
        'security': 'none',
        'transport': 'tcp',
        'summary': f'SS · {method}',
        'params': {
            'method': method.strip(),
            'password': password,
            'plugin': _safe_unquote(q.get('plugin') or ''),
        },
        'raw': s,
        'latency_ms': None,
    }


def _parse_hy2_uri(raw_uri):
    s = raw_uri.strip()
    norm = 'https://' + s.split('://', 1)[1]
    uri = urllib.parse.urlsplit(norm)
    host, port = _validate_host_port(uri.hostname, uri.port or 443)
    password = _safe_unquote(uri.username or '')
    if not password and uri.password:
        password = _safe_unquote(uri.password)
    if not password:
        raise ValueError('Hysteria2 password/auth token is required.')
    q = dict(urllib.parse.parse_qsl(uri.query, keep_blank_values=True))
    name = _clean_label(uri.fragment, f'{host}:{port}')
    upmbps = q.get('upmbps') or q.get('up') or ''
    downmbps = q.get('downmbps') or q.get('down') or ''
    params = {
        'password': password,
        'sni': (q.get('sni') or host).strip(),
        'allow_insecure': _parse_bool(q.get('insecure') or q.get('allowInsecure')),
        'obfs': (q.get('obfs') or '').strip().lower(),
        'obfs_password': _safe_unquote(q.get('obfs-password') or ''),
        'alpn': [a.strip() for a in (q.get('alpn') or 'h3').split(',') if a.strip()],
        'up_mbps': int(re.sub(r'\D+', '', upmbps) or 0),
        'down_mbps': int(re.sub(r'\D+', '', downmbps) or 0),
        'mport': (q.get('mport') or '').strip(),
    }
    return {
        'id': _stable_id(s),
        'protocol': 'hysteria2',
        'name': name,
        'flag': _extract_flag(name),
        'host': host,
        'port': port,
        'security': 'tls',
        'transport': 'quic',
        'summary': 'Hysteria2 · QUIC',
        'params': params,
        'raw': s,
        'latency_ms': None,
    }


def _parse_socks_uri(raw_uri):
    s = raw_uri.strip()
    uri = urllib.parse.urlsplit(s)
    host, port = _validate_host_port(uri.hostname, uri.port or 1080)
    username = _safe_unquote(uri.username or '')
    password = _safe_unquote(uri.password or '')
    if username and not password and '@' in s:
        try:
            dec = _b64decode_str(username)
            if ':' in dec:
                username, password = dec.split(':', 1)
        except Exception:
            pass
    name = _clean_label(uri.fragment, f'{host}:{port}')
    return {
        'id': _stable_id(s),
        'protocol': 'socks',
        'name': name,
        'flag': _extract_flag(name),
        'host': host,
        'port': port,
        'security': 'none',
        'transport': 'tcp',
        'summary': 'SOCKS5',
        'params': {'username': username, 'password': password},
        'raw': s,
        'latency_ms': None,
    }


def parse_proxy_uri(raw_uri):
    s = str(raw_uri or '').strip()
    if not s or '://' not in s:
        raise ValueError('Not a valid proxy link.')
    scheme = s.split('://', 1)[0].lower()
    if scheme == 'vless':
        return _parse_vless_uri(s)
    if scheme == 'vmess':
        return _parse_vmess_uri(s)
    if scheme == 'trojan':
        return _parse_trojan_uri(s)
    if scheme == 'ss':
        return _parse_ss_uri(s)
    if scheme in ('hy2', 'hysteria2'):
        return _parse_hy2_uri(s)
    if scheme in ('socks', 'socks5'):
        return _parse_socks_uri(s)
    raise ValueError(f'Unsupported scheme: {scheme}')


def _node_from_xray_json(obj, idx=0):
    if not isinstance(obj, dict) or not isinstance(obj.get('outbounds'), list) or not obj['outbounds']:
        raise ValueError('Xray JSON must contain an "outbounds" list.')
    out0 = obj['outbounds'][0]
    proto = str(out0.get('protocol') or 'xray').lower()
    host = ''
    port = 443
    settings = out0.get('settings') or {}
    if isinstance(settings.get('vnext'), list) and settings['vnext']:
        host = str(settings['vnext'][0].get('address') or '')
        port = int(settings['vnext'][0].get('port') or 443)
    elif isinstance(settings.get('servers'), list) and settings['servers']:
        host = str(settings['servers'][0].get('address') or '')
        port = int(settings['servers'][0].get('port') or 443)
    elif settings.get('address'):
        host = str(settings.get('address') or '')
        port = int(settings.get('port') or 443)
    name = str(obj.get('remarks') or obj.get('name') or obj.get('ps') or '').strip()
    if not name:
        name = f'{proto.upper()} · {host}:{port}' if host else f'Xray Profile #{idx + 1}'
    return {
        'id': _stable_id(json.dumps(obj, sort_keys=True, ensure_ascii=False)),
        'protocol': 'xray_json',
        'name': name,
        'flag': _extract_flag(name),
        'host': host,
        'port': port,
        'security': 'json',
        'transport': 'xray',
        'summary': f'Xray JSON · {proto.upper()}',
        'params': {},
        'xray_config': obj,
        'latency_ms': None,
    }


def parse_subscription_metadata(headers=None, lines=None):
    meta = {}
    hmap = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    # Extract comment directives at top of body (#profile-title:, #announce:, etc.)
    for ln in (lines or [])[:40]:
        s = ln.strip()
        if not s.startswith('#'):
            continue
        m = re.match(r'^#\s*([A-Za-z0-9_-]+)\s*:\s*(.+)$', s)
        if m:
            k = m.group(1).strip().lower()
            v = m.group(2).strip()
            hmap.setdefault(k, v)

    if hmap.get('profile-title'):
        meta['title'] = _decode_maybe_base64_value(hmap['profile-title'])
    if hmap.get('subscription-userinfo'):
        ui = parse_userinfo(hmap['subscription-userinfo'])
        if ui:
            meta['userinfo'] = ui
    if hmap.get('profile-update-interval'):
        try:
            meta['update_interval_hours'] = max(1, int(float(hmap['profile-update-interval'])))
        except ValueError:
            pass
    if hmap.get('announce'):
        meta['announce'] = _decode_maybe_base64_value(hmap['announce'])[:260]
    if hmap.get('support-url'):
        meta['support_url'] = hmap['support-url'].strip()
    if hmap.get('profile-web-page-url'):
        meta['web_page_url'] = hmap['profile-web-page-url'].strip()
    if hmap.get('routing'):
        meta['routing_raw'] = hmap['routing'].strip()
    if 'routing-enable' in hmap:
        meta['routing_enable'] = _parse_bool(hmap['routing-enable'])
    if 'exclude-local-networks-enable' in hmap:
        meta['exclude_local'] = _parse_bool(hmap['exclude-local-networks-enable'])
    return meta


def parse_subscription_payload(body_text, headers=None):
    """Parse raw text, base64, URI lists, or Xray JSON subscriptions into nodes + metadata."""
    txt = str(body_text or '').strip().lstrip('\ufeff')
    raw_lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    meta = parse_subscription_metadata(headers=headers, lines=raw_lines)
    nodes = []
    routing_profiles = []

    # 1. Try JSON directly
    if txt.startswith('{') or txt.startswith('['):
        try:
            parsed = json.loads(txt)
            if isinstance(parsed, dict) and isinstance(parsed.get('outbounds'), list):
                nodes.append(_node_from_xray_json(parsed, 0))
            elif isinstance(parsed, list):
                for idx, item in enumerate(parsed):
                    if isinstance(item, dict) and isinstance(item.get('outbounds'), list):
                        nodes.append(_node_from_xray_json(item, idx))
                    elif isinstance(item, str):
                        try:
                            nodes.append(parse_proxy_uri(item))
                        except ValueError:
                            pass
            if nodes:
                return {'nodes': nodes, 'meta': meta, 'routing_profiles': routing_profiles}
        except Exception:
            pass

    def _collect_lines(lines):
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if s.startswith('#'):
                # Check if comment embeds happ://routing/...
                if 'happ://routing/' in s.lower():
                    idx_r = s.lower().find('happ://routing/')
                    try:
                        routing_profiles.append(parse_happ_routing_profile(s[idx_r:]))
                    except Exception:
                        pass
                continue
            low = s.lower()
            if low.startswith('happ://routing/'):
                try:
                    routing_profiles.append(parse_happ_routing_profile(s))
                except Exception:
                    pass
                continue
            if low.startswith('happ://add/'):
                unw = unwrap_happ_link(s)
                if unw.get('kind') == 'unwrapped':
                    s = unw['value']
            if s.startswith('{'):
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict) and isinstance(obj.get('outbounds'), list):
                        nodes.append(_node_from_xray_json(obj, len(nodes)))
                        continue
                except Exception:
                    pass
            if any(s.lower().startswith(sch + '://') for sch in SUPPORTED_SCHEMES):
                try:
                    nodes.append(parse_proxy_uri(s))
                except ValueError:
                    pass

    _collect_lines(raw_lines)

    # 2. If no nodes parsed yet, try Base64 decode of the whole payload
    if not nodes:
        candidate = ''.join(ln for ln in raw_lines if not ln.startswith('#'))
        if candidate:
            try:
                decoded = _b64decode_str(candidate).strip().lstrip('\ufeff')
                dec_lines = [ln.strip() for ln in decoded.splitlines() if ln.strip()]
                dec_meta = parse_subscription_metadata(headers=headers, lines=dec_lines)
                for k, v in dec_meta.items():
                    meta.setdefault(k, v)
                if decoded.startswith('{') or decoded.startswith('['):
                    sub = parse_subscription_payload(decoded, headers=headers)
                    nodes.extend(sub['nodes'])
                    routing_profiles.extend(sub['routing_profiles'])
                else:
                    _collect_lines(dec_lines)
            except Exception:
                pass

    # Parse Routing header if present
    if meta.get('routing_raw'):
        try:
            routing_profiles.append(parse_happ_routing_profile(meta['routing_raw']))
        except Exception:
            pass

    return {'nodes': nodes, 'meta': meta, 'routing_profiles': routing_profiles}


def default_hwid():
    raw = '|'.join([
        platform.node() or 'pc',
        platform.system() or 'win',
        platform.machine() or 'x64',
        str(uuid.getnode()),
    ])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32].upper()


def decrypt_subscription_body(body_text, url=''):
    """Decrypt Happ AES-128-GCM subscription payloads prefixed with encrypt-tag: using ?key=."""
    txt = str(body_text or '').strip()
    if not txt.lower().startswith('encrypt-tag:'):
        return txt
    payload_b64 = txt.split(':', 1)[1].strip()
    parsed = urllib.parse.urlsplit(str(url or ''))
    qs = dict(urllib.parse.parse_qsl(parsed.query))
    key_raw = (qs.get('key') or '').strip()
    if not key_raw:
        raise ValueError('Encrypted subscription requires ?key= in URL.')
    kpad = key_raw.replace('-', '+').replace('_', '/')
    if (-len(kpad)) % 4:
        kpad += '=' * ((-len(kpad)) % 4)
    key_bytes = base64.b64decode(kpad.encode('ascii'))
    if len(key_bytes) not in (16, 24, 32):
        key_bytes = hashlib.sha256(key_raw.encode('utf-8')).digest()[:16]
    ppad = payload_b64.replace('-', '+').replace('_', '/')
    if (-len(ppad)) % 4:
        ppad += '=' * ((-len(ppad)) % 4)
    raw = base64.b64decode(ppad.encode('ascii'))
    if len(raw) < 28:
        raise ValueError('Encrypted subscription payload is truncated.')
    nonce, ct = raw[:12], raw[12:]
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        plain = AESGCM(key_bytes).decrypt(nonce, ct, None)
    except Exception as exc:
        raise ValueError(f'Failed to decrypt encrypted subscription: {exc}') from None
    return plain.decode('utf-8', errors='replace')


def fetch_subscription(url, send_hwid=True, hwid=None, timeout=12):
    target = str(url or '').strip()
    if not target.lower().startswith(('http://', 'https://')):
        raise ValueError('Subscription URL must start with http:// or https://.')
    headers = {
        'User-Agent': HAPP_USER_AGENT,
        'Accept': '*/*',
    }
    if send_hwid:
        headers.update({
            'x-hwid': hwid or default_hwid(),
            'x-device-os': platform.system() or 'Windows',
            'x-ver-os': platform.release() or '10',
            'x-device-model': platform.machine() or 'x86_64',
        })
    req = urllib.request.Request(target, headers=headers, method='GET')
    ctx = None
    if certifi is not None:
        try:
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            final_url = resp.geturl() or target
            if str(final_url).lower().startswith('happ://'):
                return {'redirect_happ': final_url}
            resp_headers = {k: v for k, v in resp.headers.items()}
            body = resp.read(4 * 1024 * 1024).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        loc = exc.headers.get('Location') if exc.headers else None
        if loc and loc.lower().startswith('happ://'):
            return {'redirect_happ': loc}
        raise ValueError(f'Subscription server returned HTTP {exc.code}.') from None
    except urllib.error.URLError as exc:
        raise ValueError(f'Cannot reach subscription server: {exc.reason}') from None

    body = decrypt_subscription_body(body, target)
    parsed = parse_subscription_payload(body, headers=resp_headers)
    if not parsed['nodes']:
        raise ValueError('Subscription returned no valid proxy configurations.')
    if not parsed['meta'].get('title'):
        host = urllib.parse.urlsplit(target).hostname or 'Subscription'
        parsed['meta']['title'] = host
    return parsed


def ping_tcp_latency(host, port, timeout=2.5):
    if not host or not port:
        return None
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
        return max(1, int(round((time.perf_counter() - t0) * 1000)))
    except Exception:
        return -1


def ping_nodes_parallel(nodes, timeout=2.5, max_workers=12):
    results = {}
    targets = [n for n in nodes if n.get('host') and n.get('port')]
    if not targets:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(targets)))) as pool:
        futs = {pool.submit(ping_tcp_latency, n['host'], n['port'], timeout): n['id'] for n in targets}
        for fut in concurrent.futures.as_completed(futs):
            nid = futs[fut]
            try:
                results[nid] = fut.result()
            except Exception:
                results[nid] = -1
    return results


def _resolve_server_ips(host):
    """Resolve server domain to IPv4/IPv6 CIDR strings BEFORE starting TUN to prevent routing loops."""
    ips = []
    h = str(host or '').strip()
    if not h:
        return ips
    try:
        ip_obj = ipaddress.ip_address(h)
        suffix = '/128' if ip_obj.version == 6 else '/32'
        return [f'{ip_obj}{suffix}']
    except ValueError:
        pass
    try:
        for res in socket.getaddrinfo(h, None, proto=socket.IPPROTO_TCP):
            addr = res[4][0].split('%', 1)[0]
            try:
                ip_obj = ipaddress.ip_address(addr)
                if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast:
                    continue
                cidr = f'{ip_obj}/128' if ip_obj.version == 6 else f'{ip_obj}/32'
                if cidr not in ips:
                    ips.append(cidr)
            except ValueError:
                continue
    except Exception:
        pass
    return ips


def _collect_xray_server_hosts(xray_cfg):
    hosts = []
    for out in (xray_cfg or {}).get('outbounds', []):
        s = out.get('settings') or {}
        for item in s.get('vnext', []) or []:
            if item.get('address'):
                hosts.append(item['address'])
        for item in s.get('servers', []) or []:
            if item.get('address'):
                hosts.append(item['address'])
        if s.get('address'):
            hosts.append(s['address'])
    return list(dict.fromkeys(hosts))


def _xray_stream_settings(node, allow_fragment=False):
    p = node.get('params') or {}
    transport = node.get('transport') or 'tcp'
    security = node.get('security') or 'none'
    stream = {'network': transport, 'security': security}
    if transport == 'ws':
        ws = {'path': p.get('path') or '/'}
        if p.get('host'):
            ws['headers'] = {'Host': p['host']}
        stream['wsSettings'] = ws
    elif transport == 'grpc':
        stream['grpcSettings'] = {'serviceName': p.get('service_name') or ''}
        if p.get('mode') == 'multi':
            stream['grpcSettings']['multiMode'] = True
    elif transport == 'httpupgrade':
        hu = {'path': p.get('path') or '/'}
        if p.get('host'):
            hu['host'] = p['host']
        stream['httpupgradeSettings'] = hu
    elif transport == 'xhttp':
        xh = {'path': p.get('path') or '/'}
        if p.get('host'):
            xh['host'] = p['host']
        if p.get('mode'):
            xh['mode'] = p['mode']
        if isinstance(p.get('extra'), dict):
            xh['extra'] = p['extra']
        stream['xhttpSettings'] = xh
    elif transport == 'kcp':
        kcp = {}
        if p.get('header_type') and p['header_type'] != 'none':
            kcp['header'] = {'type': p['header_type']}
        if p.get('seed'):
            kcp['seed'] = p['seed']
        stream['kcpSettings'] = kcp
    elif transport in ('http', 'h2'):
        stream['network'] = 'http'
        hs = {'path': p.get('path') or '/'}
        if p.get('host'):
            hs['host'] = [h.strip() for h in str(p['host']).split(',') if h.strip()]
        stream['httpSettings'] = hs
    elif transport == 'raw':
        stream['network'] = 'raw'

    if security == 'tls':
        tls = {'serverName': p.get('sni') or node['host']}
        if p.get('fp'):
            tls['fingerprint'] = p['fp']
        if p.get('alpn'):
            tls['alpn'] = list(p['alpn'])
        if p.get('allow_insecure'):
            tls['allowInsecure'] = True
        stream['tlsSettings'] = tls
    elif security == 'reality':
        stream['realitySettings'] = {
            'serverName': p.get('sni') or node['host'],
            'fingerprint': p.get('fp') or 'chrome',
            'publicKey': p.get('pbk') or '',
            'shortId': p.get('sid') or '',
            'spiderX': p.get('spx') or '/',
        }
    if allow_fragment and security in ('tls', 'reality') and transport in ('tcp', 'raw', 'ws', 'httpupgrade', 'xhttp'):
        stream['sockopt'] = {'dialerProxy': 'fragment'}
    return stream


_PRIVATE_CIDRS = (
    '10.0.0.0/8',
    '169.254.0.0/16',
    '172.16.0.0/12',
    '192.168.0.0/16',
    '127.0.0.0/8',
    '224.0.0.0/4',
    '::1/128',
    'fc00::/7',
    'fe80::/10',
)

_GEODAT_CODES_CACHE = {}


def list_geodat_codes(dat_path):
    """Return uppercase category codes inside an Xray geoip.dat / geosite.dat file."""
    if not dat_path or not os.path.isfile(dat_path):
        return set()
    try:
        st = os.stat(dat_path)
        key = (os.path.abspath(dat_path), st.st_size, getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9)))
        cached = _GEODAT_CODES_CACHE.get(key)
        if cached is not None:
            return cached
        codes = set()
        size = st.st_size
        with open(dat_path, 'rb') as f:
            while f.tell() < size:
                tag_b = f.read(1)
                if not tag_b:
                    break
                tag = tag_b[0]
                body_len = 0
                shift = 0
                while shift < 64:
                    b = f.read(1)
                    if not b:
                        body_len = 0
                        break
                    val = b[0]
                    body_len |= (val & 0x7F) << shift
                    if not (val & 0x80):
                        break
                    shift += 7
                if body_len <= 0 or body_len > size:
                    break
                peek = f.read(min(body_len, 128))
                if tag == 0x0A and len(peek) >= 3 and peek[0] == 0x0A and not (peek[1] & 0x80):
                    code_len = peek[1]
                    if 0 < code_len <= len(peek) - 2:
                        try:
                            codes.add(peek[2:2 + code_len].decode('utf-8').upper())
                        except UnicodeDecodeError:
                            pass
                rem = body_len - len(peek)
                if rem > 0:
                    f.seek(rem, os.SEEK_CUR)
        _GEODAT_CODES_CACHE[key] = codes
        return codes
    except Exception:
        return set()


def _xray_assets_present(asset_dir):
    if not asset_dir:
        return False
    return (
        os.path.isfile(os.path.join(asset_dir, 'geoip.dat'))
        and os.path.isfile(os.path.join(asset_dir, 'geosite.dat'))
    )


def _filter_xray_domain_rules(items, has_geo, geosite_codes=None):
    out = []
    seen = set()

    def _add(val):
        if val not in seen:
            seen.add(val)
            out.append(val)

    for item in items or []:
        s = str(item or '').strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith('ext:'):
            if has_geo:
                _add(s)
            continue
        if low.startswith('geosite:'):
            code = s.split(':', 1)[1].split('@', 1)[0].strip().upper()
            if has_geo and geosite_codes and code in geosite_codes:
                _add(s)
                continue
            if code in ('RU', 'TLD-RU', 'CATEGORY-RU', 'LOCATED-IN-RU'):
                if has_geo and geosite_codes:
                    for alt in ('LOCATED-IN-RU', 'CATEGORY-RU'):
                        if alt in geosite_codes:
                            _add('geosite:' + alt.lower())
                for d in ('domain:ru', 'domain:su', 'domain:xn--p1ai'):
                    _add(d)
            elif code == 'PRIVATE':
                for d in ('domain:local', 'domain:localhost', 'domain:lan'):
                    _add(d)
            continue
        if low.startswith(('domain:', 'full:', 'regexp:', 'keyword:')):
            _add(s)
        else:
            _add('domain:' + s)
    return out


def _filter_xray_ip_rules(items, has_geo, geoip_codes=None):
    out = []
    seen = set()

    def _add(val):
        if val not in seen:
            seen.add(val)
            out.append(val)

    for item in items or []:
        s = str(item or '').strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith('ext:'):
            if has_geo:
                _add(s)
            continue
        if low.startswith('geoip:'):
            code = s.split(':', 1)[1].strip().upper()
            if has_geo and geoip_codes and code in geoip_codes:
                _add(s)
                continue
            if code == 'PRIVATE':
                for cidr in _PRIVATE_CIDRS:
                    _add(cidr)
            elif code == 'RU' and has_geo and geoip_codes and 'IN-RU' in geoip_codes:
                _add('geoip:in-ru')
            continue
        _add(s)
    return out


def build_xray_config(
    node,
    socks_port=DEFAULT_SOCKS_PORT,
    http_port=DEFAULT_HTTP_PORT,
    settings=None,
    routing_profile=None,
    asset_dir=None,
):
    """Build a validated Xray-core JSON config for any supported protocol or xray_json."""
    st = settings or {}
    has_geo = _xray_assets_present(asset_dir)
    geoip_codes = list_geodat_codes(os.path.join(asset_dir, 'geoip.dat')) if has_geo else set()
    geosite_codes = list_geodat_codes(os.path.join(asset_dir, 'geosite.dat')) if has_geo else set()
    listen_ip = '0.0.0.0' if st.get('allow_lan') else '127.0.0.1'
    inbounds = [
        {
            'tag': 'socks-in',
            'port': int(socks_port),
            'listen': listen_ip,
            'protocol': 'socks',
            'settings': {'auth': 'noauth', 'udp': True},
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls', 'quic']},
        },
    ]
    if int(http_port or 0) > 0:
        inbounds.append({
            'tag': 'http-in',
            'port': int(http_port),
            'listen': listen_ip,
            'protocol': 'http',
            'settings': {'allowTransparent': False},
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls', 'quic']},
        })

    if node.get('protocol') == 'xray_json':
        cfg = json.loads(json.dumps(node['xray_config']))
        cfg['log'] = {'loglevel': 'warning'}
        cfg['inbounds'] = inbounds
        if not isinstance(cfg.get('outbounds'), list) or not cfg['outbounds']:
            raise ValueError('Invalid Xray JSON outbounds.')
        if not any(o.get('tag') == 'direct' for o in cfg['outbounds']):
            cfg['outbounds'].append({'tag': 'direct', 'protocol': 'freedom'})
        routing = cfg.setdefault('routing', {})
        rules = list(routing.get('rules') or [])
        rules.insert(0, {
            'type': 'field',
            'ip': ['10.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', '224.0.0.0/4'],
            'outboundTag': 'direct',
        })
        routing['rules'] = rules
        return cfg

    p = node.get('params') or {}
    proto = node['protocol']
    frag_on = bool(st.get('fragment_enabled'))
    if proto == 'vless':
        user_obj = {'id': p['uuid'], 'encryption': p.get('encryption') or 'none'}
        if p.get('flow'):
            user_obj['flow'] = p['flow']
        outbound = {
            'tag': 'proxy',
            'protocol': 'vless',
            'settings': {'vnext': [{'address': node['host'], 'port': int(node['port']), 'users': [user_obj]}]},
            'streamSettings': _xray_stream_settings(node, allow_fragment=frag_on),
        }
    elif proto == 'vmess':
        outbound = {
            'tag': 'proxy',
            'protocol': 'vmess',
            'settings': {
                'vnext': [{
                    'address': node['host'],
                    'port': int(node['port']),
                    'users': [{'id': p['uuid'], 'alterId': int(p.get('alter_id') or 0), 'security': p.get('cipher') or 'auto'}],
                }],
            },
            'streamSettings': _xray_stream_settings(node, allow_fragment=frag_on),
        }
    elif proto == 'trojan':
        outbound = {
            'tag': 'proxy',
            'protocol': 'trojan',
            'settings': {'servers': [{'address': node['host'], 'port': int(node['port']), 'password': p['password']}]},
            'streamSettings': _xray_stream_settings(node, allow_fragment=frag_on),
        }
    elif proto == 'shadowsocks':
        outbound = {
            'tag': 'proxy',
            'protocol': 'shadowsocks',
            'settings': {
                'servers': [{
                    'address': node['host'],
                    'port': int(node['port']),
                    'method': p['method'],
                    'password': p['password'],
                }],
            },
        }
    elif proto == 'socks':
        srv = {'address': node['host'], 'port': int(node['port'])}
        if p.get('username') or p.get('password'):
            srv['users'] = [{'user': p.get('username') or '', 'pass': p.get('password') or ''}]
        outbound = {'tag': 'proxy', 'protocol': 'socks', 'settings': {'servers': [srv]}}
    elif proto == 'hysteria2':
        h_settings = {'version': 2, 'auth': p['password']}
        outbound = {
            'tag': 'proxy',
            'protocol': 'hysteria',
            'settings': {'version': 2, 'address': node['host'], 'port': int(node['port'])},
            'streamSettings': {
                'network': 'hysteria',
                'hysteriaSettings': h_settings,
                'security': 'tls',
                'tlsSettings': {
                    'serverName': p.get('sni') or node['host'],
                    'alpn': p.get('alpn') or ['h3'],
                    'allowInsecure': bool(p.get('allow_insecure')),
                },
            },
        }
    else:
        raise ValueError(f'Unsupported protocol for Xray: {proto}')

    outbounds = [outbound]
    if frag_on and (outbound.get('streamSettings') or {}).get('sockopt', {}).get('dialerProxy') == 'fragment':
        outbounds.append({
            'tag': 'fragment',
            'protocol': 'freedom',
            'settings': {
                'domainStrategy': 'AsIs',
                'fragment': {
                    'packets': 'tlshello',
                    'length': str(st.get('fragment_length') or '10-30'),
                    'interval': str(st.get('fragment_interval') or '5-10'),
                },
            },
        })
    outbounds.extend([
        {'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
        {'tag': 'block', 'protocol': 'blackhole'},
    ])

    rules = [{
        'type': 'field',
        'ip': list(_PRIVATE_CIDRS),
        'outboundTag': 'direct',
    }]

    rmode = str(st.get('routing_mode') or 'global').lower()
    final_tag = 'proxy'
    if rmode == 'bypass_lan_ru':
        ru_doms = ['domain:ru', 'domain:su', 'domain:xn--p1ai']
        if has_geo and geosite_codes:
            for cand in ('RU', 'LOCATED-IN-RU', 'CATEGORY-RU'):
                if cand in geosite_codes:
                    ru_doms.append('geosite:' + cand.lower())
        rules.append({'type': 'field', 'domain': ru_doms, 'outboundTag': 'direct'})
        if has_geo and geoip_codes:
            ru_ips = []
            if 'PRIVATE' in geoip_codes:
                ru_ips.append('geoip:private')
            if 'RU' in geoip_codes:
                ru_ips.append('geoip:ru')
            elif 'IN-RU' in geoip_codes:
                ru_ips.append('geoip:in-ru')
            if ru_ips:
                rules.append({'type': 'field', 'ip': ru_ips, 'outboundTag': 'direct'})
    elif rmode == 'custom' and routing_profile:
        b_dom = _filter_xray_domain_rules(routing_profile.get('block_domains'), has_geo, geosite_codes)
        b_ip = _filter_xray_ip_rules(routing_profile.get('block_ips'), has_geo, geoip_codes)
        if b_dom:
            rules.append({'type': 'field', 'domain': b_dom, 'outboundTag': 'block'})
        if b_ip:
            rules.append({'type': 'field', 'ip': b_ip, 'outboundTag': 'block'})

        p_dom = _filter_xray_domain_rules(routing_profile.get('proxy_domains'), has_geo, geosite_codes)
        p_ip = _filter_xray_ip_rules(routing_profile.get('proxy_ips'), has_geo, geoip_codes)
        if p_dom:
            rules.append({'type': 'field', 'domain': p_dom, 'outboundTag': 'proxy'})
        if p_ip:
            rules.append({'type': 'field', 'ip': p_ip, 'outboundTag': 'proxy'})

        d_dom = _filter_xray_domain_rules(routing_profile.get('direct_domains'), has_geo, geosite_codes)
        d_ip = _filter_xray_ip_rules(routing_profile.get('direct_ips'), has_geo, geoip_codes)
        if d_dom:
            rules.append({'type': 'field', 'domain': d_dom, 'outboundTag': 'direct'})
        if d_ip:
            rules.append({'type': 'field', 'ip': d_ip, 'outboundTag': 'direct'})

        if not routing_profile.get('global_proxy', True):
            final_tag = 'direct'

    rules.append({'type': 'field', 'port': '0-65535', 'outboundTag': final_tag})
    dns_ip = str(st.get('dns_server') or DEFAULT_DNS_SERVER).strip() or DEFAULT_DNS_SERVER
    return {
        'log': {'loglevel': 'warning'},
        'dns': {'servers': [dns_ip, 'localhost']},
        'inbounds': inbounds,
        'outbounds': outbounds,
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': rules},
    }


def _singbox_tls_for_node(node):
    p = node.get('params') or {}
    security = node.get('security') or 'none'
    if security not in ('tls', 'reality'):
        return None
    tls = {'enabled': True, 'server_name': p.get('sni') or node['host']}
    if p.get('alpn'):
        tls['alpn'] = list(p['alpn'])
    if p.get('allow_insecure'):
        tls['insecure'] = True
    if security == 'reality' or p.get('fp'):
        tls['utls'] = {'enabled': True, 'fingerprint': p.get('fp') or 'chrome'}
    if security == 'reality':
        tls['reality'] = {
            'enabled': True,
            'public_key': p.get('pbk') or '',
            'short_id': p.get('sid') or '',
        }
    return tls


def _singbox_transport_for_node(node):
    p = node.get('params') or {}
    tr = node.get('transport') or 'tcp'
    if tr == 'ws':
        res = {'type': 'ws', 'path': p.get('path') or '/'}
        if p.get('host'):
            res['headers'] = {'Host': p['host']}
        return res
    if tr == 'grpc':
        return {'type': 'grpc', 'service_name': p.get('service_name') or ''}
    if tr == 'httpupgrade':
        res = {'type': 'httpupgrade', 'path': p.get('path') or '/'}
        if p.get('host'):
            res['host'] = p['host']
        return res
    if tr in ('http', 'h2'):
        res = {'type': 'http', 'path': p.get('path') or '/'}
        if p.get('host'):
            res['host'] = [h.strip() for h in str(p['host']).split(',') if h.strip()]
        return res
    return None


def _singbox_outbound_for_node(node):
    p = node.get('params') or {}
    proto = node['protocol']
    if proto == 'vless':
        out = {
            'type': 'vless',
            'tag': 'vpn',
            'server': node['host'],
            'server_port': int(node['port']),
            'uuid': p['uuid'],
            'domain_resolver': 'bootstrap',
        }
        if p.get('flow'):
            out['flow'] = p['flow']
        tls = _singbox_tls_for_node(node)
        if tls:
            out['tls'] = tls
        tr = _singbox_transport_for_node(node)
        if tr:
            out['transport'] = tr
        return out
    if proto == 'vmess':
        out = {
            'type': 'vmess',
            'tag': 'vpn',
            'server': node['host'],
            'server_port': int(node['port']),
            'uuid': p['uuid'],
            'security': p.get('cipher') or 'auto',
            'alter_id': int(p.get('alter_id') or 0),
            'domain_resolver': 'bootstrap',
        }
        tls = _singbox_tls_for_node(node)
        if tls:
            out['tls'] = tls
        tr = _singbox_transport_for_node(node)
        if tr:
            out['transport'] = tr
        return out
    if proto == 'trojan':
        out = {
            'type': 'trojan',
            'tag': 'vpn',
            'server': node['host'],
            'server_port': int(node['port']),
            'password': p['password'],
            'domain_resolver': 'bootstrap',
        }
        tls = _singbox_tls_for_node(node)
        if tls:
            out['tls'] = tls
        tr = _singbox_transport_for_node(node)
        if tr:
            out['transport'] = tr
        return out
    if proto == 'shadowsocks':
        return {
            'type': 'shadowsocks',
            'tag': 'vpn',
            'server': node['host'],
            'server_port': int(node['port']),
            'method': p['method'],
            'password': p['password'],
            'domain_resolver': 'bootstrap',
        }
    if proto == 'hysteria2':
        out = {
            'type': 'hysteria2',
            'tag': 'vpn',
            'server': node['host'],
            'server_port': int(node['port']),
            'password': p['password'],
            'domain_resolver': 'bootstrap',
            'tls': {
                'enabled': True,
                'server_name': p.get('sni') or node['host'],
                'alpn': p.get('alpn') or ['h3'],
                'insecure': bool(p.get('allow_insecure')),
            },
        }
        if p.get('obfs') == 'salamander' and p.get('obfs_password'):
            out['obfs'] = {'type': 'salamander', 'password': p['obfs_password']}
        if p.get('up_mbps'):
            out['up_mbps'] = int(p['up_mbps'])
        if p.get('down_mbps'):
            out['down_mbps'] = int(p['down_mbps'])
        return out
    if proto == 'socks':
        out = {
            'type': 'socks',
            'tag': 'vpn',
            'server': node['host'],
            'server_port': int(node['port']),
            'domain_resolver': 'bootstrap',
        }
        if p.get('username'):
            out['username'] = p['username']
        if p.get('password'):
            out['password'] = p['password']
        return out
    raise ValueError(f'Protocol "{proto}" requires the Xray engine.')


def _singbox_domain_rule(items, target_key, val):
    suffixes, fulls, regexps, keywords = [], [], [], []
    for raw in items or []:
        s = str(raw or '').strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith(('geosite:', 'ext:')):
            continue
        if low.startswith('full:'):
            fulls.append(s[5:])
        elif low.startswith('domain:'):
            suffixes.append('.' + s[7:].lstrip('.'))
        elif low.startswith('keyword:'):
            keywords.append(s[8:])
        elif low.startswith('regexp:'):
            regexps.append(s[7:])
        else:
            suffixes.append('.' + s.lstrip('.'))
    rule = {}
    if suffixes:
        rule['domain_suffix'] = suffixes
    if fulls:
        rule['domain'] = fulls
    if keywords:
        rule['domain_keyword'] = keywords
    if regexps:
        rule['domain_regex'] = regexps
    if not rule:
        return None
    if target_key == 'reject':
        rule['action'] = 'reject'
    else:
        rule['outbound'] = val
    return rule


def _singbox_ip_rule(items, target_key, val):
    cidrs = []
    for raw in items or []:
        s = str(raw or '').strip()
        if not s or s.lower().startswith(('geoip:', 'ext:')):
            continue
        try:
            net = ipaddress.ip_network(s, strict=False)
            cidrs.append(str(net))
        except ValueError:
            pass
    if not cidrs:
        return None
    rule = {'ip_cidr': cidrs}
    if target_key == 'reject':
        rule['action'] = 'reject'
    else:
        rule['outbound'] = val
    return rule


def build_singbox_config(
    node=None,
    mode='tun',
    upstream_socks_port=None,
    extra_excludes=None,
    socks_port=DEFAULT_SOCKS_PORT,
    http_port=DEFAULT_HTTP_PORT,
    settings=None,
    routing_profile=None,
):
    """Build a sing-box 1.14.4 config either directly or as a TUN bridge over Xray SOCKS5."""
    st = settings or {}
    mode = str(mode or 'tun').lower()
    inbounds = []
    if mode in ('tun', 'mixed'):
        excludes = list(vless._TUN_ROUTE_EXCLUDES)
        for cidr in extra_excludes or []:
            if cidr and cidr not in excludes:
                excludes.append(cidr)
        inbounds.append({
            'type': 'tun',
            'interface_name': vless.ADAPTER,
            'auto_route': True,
            'strict_route': True,
            'address': ['172.19.0.1/30', 'fdfe:dcba:9876::1/126'],
            'route_exclude_address': excludes,
        })
    if mode in ('proxy', 'mixed'):
        listen_ip = '0.0.0.0' if st.get('allow_lan') else '127.0.0.1'
        inbounds.append({'type': 'socks', 'tag': 'socks-in', 'listen': listen_ip, 'listen_port': int(socks_port)})
        if int(http_port or 0) > 0:
            inbounds.append({'type': 'http', 'tag': 'http-in', 'listen': listen_ip, 'listen_port': int(http_port)})

    if upstream_socks_port is not None:
        vpn_outbound = {
            'type': 'socks',
            'tag': 'vpn',
            'server': '127.0.0.1',
            'server_port': int(upstream_socks_port),
        }
    else:
        vpn_outbound = _singbox_outbound_for_node(node)

    outbounds = [vpn_outbound, {'type': 'direct', 'tag': 'direct'}]
    rules = [{'action': 'sniff'}]
    if mode in ('tun', 'mixed'):
        rules.append({'port': 53, 'action': 'hijack-dns'})
    rules.append({'ip_is_private': True, 'outbound': 'direct'})

    final_out = 'vpn'
    if upstream_socks_port is not None:
        # When Xray handles routing/bypass, let sing-box prevent any xray.exe loop.
        rules.append({'process_name': ['xray.exe'], 'outbound': 'direct'})
    else:
        rmode = str(st.get('routing_mode') or 'global').lower()
        if rmode == 'bypass_lan_ru':
            rules.append({'domain_suffix': list(_RU_BYPASS_SUFFIXES), 'outbound': 'direct'})
        elif rmode == 'custom' and routing_profile:
            for r in (
                _singbox_domain_rule(routing_profile.get('block_domains'), 'reject', None),
                _singbox_ip_rule(routing_profile.get('block_ips'), 'reject', None),
                _singbox_domain_rule(routing_profile.get('proxy_domains'), 'outbound', 'vpn'),
                _singbox_ip_rule(routing_profile.get('proxy_ips'), 'outbound', 'vpn'),
                _singbox_domain_rule(routing_profile.get('direct_domains'), 'outbound', 'direct'),
                _singbox_ip_rule(routing_profile.get('direct_ips'), 'outbound', 'direct'),
            ):
                if r:
                    rules.append(r)
            if not routing_profile.get('global_proxy', True):
                final_out = 'direct'

    dns_ip = str(st.get('dns_server') or DEFAULT_DNS_SERVER).strip() or DEFAULT_DNS_SERVER
    return {
        'log': {'level': 'info'},
        'inbounds': inbounds,
        'outbounds': outbounds,
        'dns': {
            'servers': [
                {'type': 'local', 'tag': 'bootstrap'},
                {'type': 'tcp', 'tag': 'tunnel-dns', 'server': dns_ip, 'detour': 'vpn'},
            ],
            'final': 'tunnel-dns',
        },
        'route': {
            'auto_detect_interface': True,
            'rules': rules,
            'final': final_out,
        },
    }


def can_singbox_run(node):
    if not node:
        return False
    if node.get('protocol') == 'xray_json':
        return False
    if (node.get('transport') or '') in ('xhttp', 'splithttp', 'kcp'):
        return False
    return True


def requires_xray_engine(node, settings=None):
    if not node:
        return False
    if node.get('protocol') == 'xray_json':
        return True
    if (node.get('transport') or '') in ('xhttp', 'splithttp', 'kcp'):
        return True
    st = settings or {}
    if st.get('fragment_enabled') and node.get('security') in ('tls', 'reality') and node.get('transport') in ('tcp', 'raw', 'ws', 'httpupgrade', 'xhttp'):
        return True
    return False


def choose_engine_plan(node, settings=None, has_xray=True, has_singbox=True):
    """Select the optimal execution plan ('xray_proxy', 'singbox_proxy', 'singbox_tun', 'xray_with_singbox_tun')."""
    st = settings or {}
    mode = str(st.get('mode') or 'tun').lower()
    pref = str(st.get('engine') or 'auto').lower()
    need_xray = requires_xray_engine(node, st)
    p = (node or {}).get('params') or {}
    prefer_sb = (node or {}).get('protocol') == 'hysteria2' and (p.get('obfs') == 'salamander' or p.get('up_mbps') or p.get('down_mbps'))

    if mode == 'proxy':
        if (pref == 'sing-box' or prefer_sb) and not need_xray and has_singbox:
            return 'singbox_proxy'
        if has_xray:
            return 'xray_proxy'
        if has_singbox and not need_xray:
            return 'singbox_proxy'
        raise ValueError('Required proxy engine binary (xray.exe / sing-box.exe) is missing.')

    # TUN or Mixed mode
    use_xray_tun = need_xray or (pref == 'xray' and has_xray)
    if use_xray_tun:
        if not has_xray:
            raise ValueError('This profile (XHTTP / Fragment / Xray JSON) requires xray.exe.')
        if not has_singbox:
            raise ValueError('TUN mode requires sing-box.exe.')
        return 'xray_with_singbox_tun'

    if has_singbox:
        return 'singbox_tun'
    raise ValueError('TUN mode requires sing-box.exe.')


class HappStore:
    """Persistent store for subscriptions, manual nodes, routing profiles, and proxy settings."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.data = self._default_data()
        self.load()

    @staticmethod
    def _default_data():
        return {
            'version': 1,
            'hwid': default_hwid(),
            'settings': {
                'mode': 'tun',           # tun | proxy | mixed
                'engine': 'auto',        # auto | xray | sing-box
                'routing_mode': 'bypass_lan_ru',  # global | bypass_lan_ru | custom
                'active_routing_id': '',
                'fragment_enabled': False,
                'fragment_length': '10-30',
                'fragment_interval': '5-10',
                'dns_server': DEFAULT_DNS_SERVER,
                'send_hwid': True,
                'auto_update': False,
                'system_proxy': True,
                'allow_lan': False,
                'socks_port': DEFAULT_SOCKS_PORT,
                'http_port': DEFAULT_HTTP_PORT,
            },
            'subscriptions': [
                {
                    'id': 'manual',
                    'title': 'My Servers (Manual)',
                    'url': '',
                    'is_manual': True,
                    'updated_at': 0,
                    'meta': {},
                    'nodes': [],
                },
            ],
            'routing_profiles': [],
            'active_sub_id': 'manual',
            'active_node_id': '',
        }

    def load(self):
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return
            base = self._default_data()
            if isinstance(raw.get('hwid'), str) and raw['hwid']:
                base['hwid'] = raw['hwid']
            if isinstance(raw.get('settings'), dict):
                base['settings'].update(raw['settings'])
            if isinstance(raw.get('subscriptions'), list) and raw['subscriptions']:
                base['subscriptions'] = raw['subscriptions']
            if not any(s.get('id') == 'manual' for s in base['subscriptions']):
                base['subscriptions'].insert(0, self._default_data()['subscriptions'][0])
            if isinstance(raw.get('routing_profiles'), list):
                base['routing_profiles'] = raw['routing_profiles']
            base['active_sub_id'] = str(raw.get('active_sub_id') or 'manual')
            base['active_node_id'] = str(raw.get('active_node_id') or '')
            self.data = base
        except Exception:
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get_sub(self, sub_id):
        for s in self.data['subscriptions']:
            if s.get('id') == sub_id:
                return s
        return None

    def get_active_node(self, sub_id=None, node_id=None):
        sid = sub_id or self.data.get('active_sub_id') or 'manual'
        nid = node_id or self.data.get('active_node_id') or ''
        sub = self.get_sub(sid)
        if sub:
            for n in sub.get('nodes', []):
                if not nid or n.get('id') == nid:
                    return sub, n
        for sub in self.data['subscriptions']:
            for n in sub.get('nodes', []):
                if n.get('id') == nid:
                    return sub, n
        for sub in self.data['subscriptions']:
            if sub.get('nodes'):
                return sub, sub['nodes'][0]
        return None, None

    def select_node(self, sub_id, node_id):
        sub = self.get_sub(sub_id)
        if not sub:
            raise ValueError('Subscription not found.')
        node = next((n for n in sub.get('nodes', []) if n.get('id') == node_id), None)
        if not node:
            raise ValueError('Server node not found.')
        self.data['active_sub_id'] = sub_id
        self.data['active_node_id'] = node_id
        self.save()
        return node

    def update_settings(self, patch):
        if isinstance(patch, dict):
            for k, v in patch.items():
                if k in self.data['settings']:
                    self.data['settings'][k] = v
            self.save()
        return dict(self.data['settings'])

    def upsert_routing_profile(self, profile):
        if not isinstance(profile, dict) or not profile.get('id'):
            return
        profs = self.data['routing_profiles']
        for i, existing in enumerate(profs):
            if existing.get('id') == profile['id'] or existing.get('name') == profile['name']:
                profs[i] = profile
                break
        else:
            profs.append(profile)
        if profile.get('activate', True):
            self.data['settings']['active_routing_id'] = profile['id']
            self.data['settings']['routing_mode'] = 'custom'
        self.save()

    def active_routing_profile(self):
        rid = self.data['settings'].get('active_routing_id')
        for p in self.data['routing_profiles']:
            if p.get('id') == rid:
                return p
        return None

    def import_input(self, raw_text, title=''):
        txt = str(raw_text or '').strip()
        if not txt:
            raise ValueError('Paste a subscription URL, happ:// link, or proxy link first.')

        unw = unwrap_happ_link(txt)
        if unw['kind'] == 'encrypted_happ':
            return {
                'encrypted_happ': True,
                'uri': unw['uri'],
                'error': (
                    'This is a proprietary encrypted Happ link (happ://crypt*). '
                    'Open it with the official Happ Desktop app (bridge button below), or paste an https:// subscription or proxy link.'
                ),
            }
        if unw['kind'] == 'routing':
            prof = unw['profile']
            self.upsert_routing_profile(prof)
            return {'ok': True, 'kind': 'routing', 'profile': prof}
        if unw['kind'] == 'unwrapped':
            txt = unw['value']
            # Could become encrypted_happ after unwrap
            if is_encrypted_happ_link(txt):
                return self.import_input(txt, title=title)

        # HTTP/HTTPS subscription URL
        if txt.lower().startswith(('http://', 'https://')) and '\n' not in txt:
            res = fetch_subscription(
                txt,
                send_hwid=bool(self.data['settings'].get('send_hwid', True)),
                hwid=self.data.get('hwid'),
            )
            if res.get('redirect_happ'):
                return self.import_input(res['redirect_happ'], title=title)
            sub_id = _stable_id(txt)
            meta = res['meta']
            sub_title = title.strip() or meta.get('title') or 'Subscription'
            existing = self.get_sub(sub_id)
            sub_obj = {
                'id': sub_id,
                'title': sub_title,
                'url': txt,
                'is_manual': False,
                'updated_at': int(time.time()),
                'meta': meta,
                'nodes': res['nodes'],
            }
            if existing is not None:
                # Preserve previous ping latency when node ID matches
                old_lat = {n['id']: n.get('latency_ms') for n in existing.get('nodes', []) if n.get('latency_ms') is not None}
                for n in sub_obj['nodes']:
                    if n['id'] in old_lat:
                        n['latency_ms'] = old_lat[n['id']]
                idx = self.data['subscriptions'].index(existing)
                self.data['subscriptions'][idx] = sub_obj
            else:
                self.data['subscriptions'].append(sub_obj)
            for rp in res.get('routing_profiles', []):
                self.upsert_routing_profile(rp)
            self.data['active_sub_id'] = sub_id
            if not any(n['id'] == self.data.get('active_node_id') for n in sub_obj['nodes']):
                self.data['active_node_id'] = sub_obj['nodes'][0]['id']
            self.save()
            return {'ok': True, 'kind': 'subscription', 'subscription_id': sub_id, 'count': len(sub_obj['nodes']), 'title': sub_title}

        # Direct nodes / base64 / JSON
        parsed = parse_subscription_payload(txt)
        for rp in parsed.get('routing_profiles', []):
            self.upsert_routing_profile(rp)
        nodes = parsed['nodes']
        if not nodes:
            if parsed.get('routing_profiles'):
                self.save()
                return {'ok': True, 'kind': 'routing', 'profile': parsed['routing_profiles'][0]}
            raise ValueError('Could not recognize a valid subscription URL, JSON config, or proxy link (VLESS/VMess/Trojan/SS/Hysteria2/SOCKS5).')

        manual = self.get_sub('manual')
        existing_ids = {n['id']: i for i, n in enumerate(manual['nodes'])}
        for n in nodes:
            if n['id'] in existing_ids:
                manual['nodes'][existing_ids[n['id']]] = n
            else:
                manual['nodes'].append(n)
        manual['updated_at'] = int(time.time())
        self.data['active_sub_id'] = 'manual'
        self.data['active_node_id'] = nodes[0]['id']
        self.save()
        return {'ok': True, 'kind': 'nodes', 'subscription_id': 'manual', 'count': len(nodes), 'title': manual['title']}

    def update_subscription(self, sub_id=None):
        targets = [self.get_sub(sub_id)] if sub_id else [s for s in self.data['subscriptions'] if not s.get('is_manual') and s.get('url')]
        targets = [s for s in targets if s and s.get('url')]
        if not targets:
            raise ValueError('No remote subscriptions to update.')
        updated = []
        for s in targets:
            res = self.import_input(s['url'], title=s.get('title') or '')
            updated.append({'id': s['id'], 'title': res.get('title', s.get('title')), 'count': res.get('count', 0)})
        return updated

    def delete_subscription(self, sub_id):
        if sub_id == 'manual':
            manual = self.get_sub('manual')
            manual['nodes'] = []
            if self.data.get('active_sub_id') == 'manual':
                self.data['active_node_id'] = ''
            self.save()
            return True
        before = len(self.data['subscriptions'])
        self.data['subscriptions'] = [s for s in self.data['subscriptions'] if s.get('id') != sub_id]
        if len(self.data['subscriptions']) == before:
            return False
        if self.data.get('active_sub_id') == sub_id:
            self.data['active_sub_id'] = self.data['subscriptions'][0]['id']
            nodes = self.data['subscriptions'][0].get('nodes') or []
            self.data['active_node_id'] = nodes[0]['id'] if nodes else ''
        self.save()
        return True

    def delete_node(self, sub_id, node_id):
        sub = self.get_sub(sub_id)
        if not sub:
            return False
        before = len(sub.get('nodes', []))
        sub['nodes'] = [n for n in sub.get('nodes', []) if n.get('id') != node_id]
        if len(sub['nodes']) == before:
            return False
        if self.data.get('active_node_id') == node_id:
            self.data['active_node_id'] = sub['nodes'][0]['id'] if sub['nodes'] else ''
        self.save()
        return True

    def ping_subscription(self, sub_id=None):
        sid = sub_id or self.data.get('active_sub_id') or 'manual'
        sub = self.get_sub(sid)
        if not sub or not sub.get('nodes'):
            return {}
        res = ping_nodes_parallel(sub['nodes'])
        for n in sub['nodes']:
            if n['id'] in res:
                n['latency_ms'] = res[n['id']]
        self.save()
        return res

    def select_fastest_node(self, sub_id=None):
        sid = sub_id or self.data.get('active_sub_id') or 'manual'
        sub = self.get_sub(sid)
        if not sub or not sub.get('nodes'):
            raise ValueError('No servers available in this group.')
        res = self.ping_subscription(sid)
        reachable = [n for n in sub['nodes'] if isinstance(n.get('latency_ms'), int) and n['latency_ms'] > 0]
        if not reachable:
            raise ValueError('All servers timed out during ping.')
        best = min(reachable, key=lambda n: n['latency_ms'])
        self.data['active_sub_id'] = sid
        self.data['active_node_id'] = best['id']
        self.save()
        return best

    def summary(self):
        subs_out = []
        for s in self.data['subscriptions']:
            nodes_out = []
            for n in s.get('nodes', []):
                nodes_out.append({
                    'id': n['id'],
                    'name': n.get('name') or 'Server',
                    'flag': n.get('flag') or '',
                    'protocol': n.get('protocol') or '',
                    'host': n.get('host') or '',
                    'port': n.get('port') or 0,
                    'summary': n.get('summary') or '',
                    'latency_ms': n.get('latency_ms'),
                })
            subs_out.append({
                'id': s['id'],
                'title': s.get('title') or 'Group',
                'has_url': bool(s.get('url')),
                'is_manual': bool(s.get('is_manual')),
                'updated_at': s.get('updated_at', 0),
                'meta': dict(s.get('meta') or {}),
                'nodes': nodes_out,
            })
        return {
            'settings': dict(self.data['settings']),
            'subscriptions': subs_out,
            'routing_profiles': [
                {'id': p['id'], 'name': p['name'], 'global_proxy': p.get('global_proxy', True)}
                for p in self.data['routing_profiles']
            ],
            'active_sub_id': self.data.get('active_sub_id') or 'manual',
            'active_node_id': self.data.get('active_node_id') or '',
        }


def find_happ_desktop_exe():
    if platform.system() != 'Windows':
        return None
    candidates = [
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'FlyFrogLLC', 'Happ', 'Happ.exe'),
        os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'FlyFrogLLC', 'Happ', 'Happ.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Happ', 'Happ.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Happ', 'Happ.exe'),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'happ\shell\open\command') as key:
            cmd, _ = winreg.QueryValueEx(key, None)
            m = re.match(r'^\s*"([^"]+\.exe)"', str(cmd), re.IGNORECASE)
            if m and os.path.isfile(m.group(1)):
                return m.group(1)
    except Exception:
        pass
    return None
