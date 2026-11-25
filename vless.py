"""Convert a VLESS share link into an in-memory sing-box Windows TUN config."""
import ipaddress
import re
import uuid
from urllib.parse import parse_qsl, urlsplit

ADAPTER = 'NEON-VLESS'
DIRECT = 'direct'


_TUN_ROUTE_EXCLUDES = [
    '10.0.0.0/8',
    '169.254.0.0/16',
    '172.16.0.0/15',
    '172.18.0.0/16',
    '172.20.0.0/14',
    '192.168.0.0/16',
    '224.0.0.0/4',
    '255.255.255.255/32',
    'fc00::/7',
    'fe80::/10',
    'ff00::/8',
]


def _is_hostname(host):
    """True when `host` is a DNS name rather than a literal address."""
    try:
        ipaddress.ip_address(str(host))
    except ValueError:
        return True
    return False


def bypass_prefixes(addresses):
    """Host routes for the addresses the tunnel itself dials.

    sing-box binds its own sockets to the uplink (``auto_detect_interface``), but
    Windows ignores that binding while IP forwarding is enabled on the uplink — which
    is exactly the state ICS / Mobile Hotspot create. Those packets are then sent back
    into the TUN: the tunnel carries itself and every client reports "no internet".
    Keeping a more specific route for the server address out of the TUN fixes it at the
    routing-table level, where interface binding cannot interfere.
    """
    prefixes = []
    for item in tuple(addresses or ()):
        text = str(item).strip().strip('[]')
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified:
            continue
        prefix = '{}/{}'.format(ip, 32 if ip.version == 4 else 128)
        if prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def link_host(link):
    """Server name from a vless:// link, or '' when the link is unusable.

    Used by the caller to resolve the address before the TUN exists; parsing stays
    here so the link format is understood in exactly one place.
    """
    try:
        host = urlsplit(str(link).strip()).hostname or ''
    except ValueError:
        return ''
    host = host.strip('[]')
    if not host or len(host) > 253 or any(ord(c) < 32 for c in host):
        return ''
    if not re.fullmatch(r'[A-Za-z0-9.\-:]+', host):
        return ''
    return host


def configuration(link, server_addresses=None, strict_route=True):
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
    tls = {'enabled': True, 'server_name': q.get('sni') or q.get('servername') or host}
    # A profile link may opt out of certificate verification (self-signed server certs,
    # `allowInsecure=1` in v2rayN/nekorewrite links). Honour it: otherwise sing-box
    # rejects the handshake, the tunnel reports "started" and nothing ever flows.
    if q.get('allowInsecure', q.get('allowinsecure', '')).lower() in ('1', 'true', 'yes'):
        tls['insecure'] = True
    if q.get('alpn'):
        tls['alpn'] = q['alpn'].split(',')
    if security == 'reality' or q.get('fp'):
        tls['utls'] = {'enabled': True, 'fingerprint': q.get('fp') or 'chrome'}
    if security == 'reality':
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', q.get('pbk', '')) or not re.fullmatch(r'(?:[a-fA-F0-9]{2}){0,8}', q.get('sid', '')):
            raise ValueError('Invalid REALITY public key or short ID.')
        tls['reality'] = {'enabled': True, 'public_key': q['pbk'], 'short_id': q.get('sid', '')}
    bypass = bypass_prefixes(server_addresses)
    # When NEON already resolved the server name, dial the address directly: the tunnel
    # must not depend on DNS that it is about to take over itself. TLS SNI/verification
    # keep using the hostname from the link.
    server = host
    if bypass and _is_hostname(host):
        for prefix in bypass:
            address = prefix.split('/')[0]
            if ':' not in address:
                server = address
                break
    outbound = {'type': 'vless', 'tag': 'vpn', 'server': server, 'server_port': port,
                'uuid': user, 'tls': tls, 'domain_resolver': 'bootstrap'}
    if q.get('flow'):
        outbound['flow'] = q['flow']
    if transport == 'ws':
        outbound['transport'] = {'type': 'ws', 'path': q.get('path') or '/'}
        if q.get('host'):
            outbound['transport']['headers'] = {'Host': q['host']}
    elif transport == 'grpc':
        outbound['transport'] = {'type': 'grpc', 'service_name': q.get('serviceName', '')}
    rules = []
    if bypass:
        rules.append({'ip_cidr': bypass, 'outbound': DIRECT})
    rules.append({'port': 53, 'action': 'hijack-dns'})
    return {'log': {'level': 'info'},
            'inbounds': [{'type': 'tun', 'interface_name': ADAPTER, 'auto_route': True,
                          # strict_route installs Windows Filtering Platform rules that keep the
                          # host off the uplink. They are the right choice for a plain VPN client,
                          # but a sharing box (ICS / Mobile Hotspot) also forwards, and some drivers
                          # then stop answering — `vless_strict_route: false` is the escape hatch.
                          'strict_route': bool(strict_route),
                          'address': ['172.19.0.1/30', 'fdfe:dcba:9876::1/126'],
                          # `native` registers DNS on the TUN adapter but skips the extra
                          # port-53 block that `hijack` adds on Windows with strict_route:
                          # that block also silences the host resolver used for ICS clients.
                          'dns_mode': 'native',
                          'route_exclude_address': list(_TUN_ROUTE_EXCLUDES) + bypass}],
            'outbounds': [outbound, {'type': 'direct', 'tag': DIRECT}],
            'dns': {'servers': [{'type': 'local', 'tag': 'bootstrap'},
                                {'type': 'tcp', 'tag': 'tunnel-dns', 'server': '1.1.1.1', 'detour': 'vpn'}],
                    'final': 'tunnel-dns'},
            'route': {'auto_detect_interface': True, 'rules': rules, 'final': 'vpn'}}
