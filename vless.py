"""Convert a VLESS share link into an in-memory sing-box Windows TUN config."""
import ipaddress
import platform
import re
import sys
import uuid
from urllib.parse import parse_qsl, urlsplit

ADAPTER = 'NEON-VLESS'


def _is_windows_below_10():
    if platform.system() != 'Windows':
        return False
    try:
        return sys.getwindowsversion().major < 10
    except Exception:
        return False


_TUN_ROUTE_EXCLUDES = [
    '10.0.0.0/8',
    '169.254.0.0/16',
    '172.16.0.0/15',
    '172.18.0.0/16',
    '172.20.0.0/14',
    '172.24.0.0/13',
    '192.168.0.0/16',
    '224.0.0.0/4',
    '255.255.255.255/32',
    'fc00::/7',
    'fe80::/10',
    'ff00::/8',
]


def configuration(link):
    # Never include user input in exceptions: the UUID is a credential.
    try:
        if not isinstance(link, str) or len(link) > 8192 or any(ord(c) < 32 for c in link):
            raise ValueError
        uri = urlsplit(link.strip())
        host, port = uri.hostname, uri.port
        if uri.scheme != 'vless' or not host or not port or uri.password is not None or uri.path not in ('', '/'):
            raise ValueError
        if ':' in host:
            ipaddress.IPv6Address(host)
        elif not re.fullmatch(r'[A-Za-z0-9.-]+', host):
            raise ValueError
        user = str(uuid.UUID(uri.username or ''))
        pairs = parse_qsl(uri.query, keep_blank_values=True, max_num_fields=48)
        q = dict(pairs)
        if len(q) != len(pairs) or any(any(ord(c) < 32 for c in v) for v in q.values()):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid vless:// link (UUID, server and port required).') from None

    security, transport = q.get('security', 'none'), q.get('type', 'tcp')
    if security not in ('tls', 'reality'):
        raise ValueError('This VPN requires a TLS or REALITY VLESS profile.')
    if transport not in ('tcp', 'raw', 'ws', 'grpc') or q.get('headerType', 'none') != 'none':
        raise ValueError('Supported transports: TCP, WebSocket, gRPC.')
    if q.get('encryption', 'none') != 'none' or q.get('flow', '') not in ('', 'xtls-rprx-vision'):
        raise ValueError('Unsupported VLESS encryption or flow.')
    tls = {'enabled': True, 'server_name': q.get('sni') or host}
    if q.get('alpn'):
        tls['alpn'] = q['alpn'].split(',')
    if security == 'reality' or q.get('fp'):
        tls['utls'] = {'enabled': True, 'fingerprint': q.get('fp') or 'chrome'}
    if security == 'reality':
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', q.get('pbk', '')) or not re.fullmatch(r'(?:[a-fA-F0-9]{2}){0,8}', q.get('sid', '')):
            raise ValueError('Invalid REALITY public key or short ID.')
        tls['reality'] = {'enabled': True, 'public_key': q['pbk'], 'short_id': q.get('sid', '')}
    outbound = {'type': 'vless', 'tag': 'vpn', 'server': host, 'server_port': port,
                'uuid': user, 'tls': tls, 'domain_resolver': 'bootstrap'}
    if q.get('flow'):
        outbound['flow'] = q['flow']
    if transport == 'ws':
        outbound['transport'] = {'type': 'ws', 'path': q.get('path') or '/'}
        if q.get('host'):
            outbound['transport']['headers'] = {'Host': q['host']}
    elif transport == 'grpc':
        outbound['transport'] = {'type': 'grpc', 'service_name': q.get('serviceName', '')}
    return {'log': {'level': 'info'},
            'certificate': {'store': 'mozilla'},
            'inbounds': [{'type': 'tun', 'interface_name': ADAPTER, 'stack': 'gvisor', 'mtu': 1500,
                          'auto_route': True, 'strict_route': not _is_windows_below_10(),
                          'address': ['172.19.0.1/30'],
                          'route_exclude_address': list(_TUN_ROUTE_EXCLUDES)}],
            'outbounds': [outbound],
            'dns': {'servers': [{'type': 'local', 'tag': 'bootstrap', 'prefer_go': True},
                                {'type': 'https', 'tag': 'tunnel-dns', 'server': '1.1.1.1',
                                 'path': '/dns-query', 'detour': 'vpn'}],
                    'final': 'tunnel-dns',
                    'strategy': 'ipv4_only'},
            'route': {'auto_detect_interface': True,
                      'default_domain_resolver': 'bootstrap',
                      'rules': [{'action': 'sniff'}, {'port': 53, 'action': 'hijack-dns'}],
                      'final': 'vpn'}}
