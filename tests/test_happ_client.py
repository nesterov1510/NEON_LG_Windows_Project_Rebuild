import ast
import base64
import json
import os
import re
from unittest import mock

import pytest

import happ_client
from core import Api


SAMPLE_VLESS_REALITY_XHTTP = (
    'vless://11111111-1111-4111-8111-111111111111@203.0.113.10:443'
    '?security=reality&sni=cdn.example.org&fp=chrome&pbk=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    '&sid=abcd&spx=%2F&type=xhttp&path=%2Fxhttp&mode=auto#NL%20XHTTP'
)
SAMPLE_VMESS = 'vmess://' + base64.b64encode(json.dumps({
    'v': '2',
    'ps': 'DE VMess WS',
    'add': '203.0.113.20',
    'port': 443,
    'id': '22222222-2222-4222-8222-222222222222',
    'aid': 0,
    'net': 'ws',
    'path': '/ws',
    'host': 'ws.example.org',
    'tls': 'tls',
    'sni': 'ws.example.org',
}).encode('utf-8')).decode('ascii')
SAMPLE_TROJAN = 'trojan://secretpass@203.0.113.30:443?sni=tr.example.org&type=grpc&serviceName=trgrpc#FI%20Trojan'
SAMPLE_SS = 'ss://' + base64.b64encode(b'chacha20-ietf-poly1305:ss-secret').decode('ascii').rstrip('=') + '@203.0.113.40:8388#SG%20SS'
SAMPLE_HY2 = 'hy2://hy2pass@203.0.113.50:8443?sni=hy.example.org&obfs=salamander&obfs-password=obfpass&insecure=1#PL%20Hysteria2'
SAMPLE_SOCKS = 'socks5://alice:bob@203.0.113.60:1080#US%20SOCKS5'


def test_parse_all_proxy_uris():
    n_vless = happ_client.parse_proxy_uri(SAMPLE_VLESS_REALITY_XHTTP)
    assert n_vless['protocol'] == 'vless'
    assert n_vless['name'] == 'NL XHTTP'
    assert n_vless['transport'] == 'xhttp'
    assert n_vless['security'] == 'reality'
    assert n_vless['params']['path'] == '/xhttp'
    assert n_vless['params']['sid'] == 'abcd'

    n_vmess = happ_client.parse_proxy_uri(SAMPLE_VMESS)
    assert n_vmess['protocol'] == 'vmess'
    assert n_vmess['name'] == 'DE VMess WS'
    assert n_vmess['transport'] == 'ws'
    assert n_vmess['security'] == 'tls'

    n_tr = happ_client.parse_proxy_uri(SAMPLE_TROJAN)
    assert n_tr['protocol'] == 'trojan'
    assert n_tr['transport'] == 'grpc'
    assert n_tr['params']['service_name'] == 'trgrpc'

    n_ss = happ_client.parse_proxy_uri(SAMPLE_SS)
    assert n_ss['protocol'] == 'shadowsocks'
    assert n_ss['params']['method'] == 'chacha20-ietf-poly1305'
    assert n_ss['params']['password'] == 'ss-secret'

    n_hy2 = happ_client.parse_proxy_uri(SAMPLE_HY2)
    assert n_hy2['protocol'] == 'hysteria2'
    assert n_hy2['params']['obfs'] == 'salamander'
    assert n_hy2['params']['obfs_password'] == 'obfpass'
    assert n_hy2['params']['allow_insecure'] is True

    n_socks = happ_client.parse_proxy_uri(SAMPLE_SOCKS)
    assert n_socks['protocol'] == 'socks'
    assert n_socks['params']['username'] == 'alice'
    assert n_socks['params']['password'] == 'bob'


def test_parse_xray_json_single_and_array():
    xray_obj = {
        'remarks': 'Custom Xray Node',
        'outbounds': [
            {
                'tag': 'proxy',
                'protocol': 'vless',
                'settings': {'vnext': [{'address': '203.0.113.70', 'port': 443, 'users': [{'id': '33333333-3333-4333-8333-333333333333', 'encryption': 'none'}]}]},
                'streamSettings': {'network': 'tcp', 'security': 'tls', 'tlsSettings': {'serverName': 'x.example.org'}},
            },
            {'tag': 'direct', 'protocol': 'freedom'},
        ],
    }
    sub_single = happ_client.parse_subscription_payload(json.dumps(xray_obj))
    assert len(sub_single['nodes']) == 1
    assert sub_single['nodes'][0]['protocol'] == 'xray_json'
    assert sub_single['nodes'][0]['host'] == '203.0.113.70'

    sub_arr = happ_client.parse_subscription_payload(json.dumps([xray_obj, xray_obj]))
    assert len(sub_arr['nodes']) == 2


def test_parse_base64_subscription_and_happ_directives():
    raw_lines = '\n'.join([
        '#profile-title: base64:' + base64.b64encode('Мой Прокси'.encode('utf-8')).decode('ascii'),
        '#subscription-userinfo: upload=1048576; download=2147483648; total=107374182400; expire=1893456000',
        '#announce: base64:' + base64.b64encode('Серверы обновлены'.encode('utf-8')).decode('ascii'),
        '#profile-update-interval: 6',
        SAMPLE_VLESS_REALITY_XHTTP,
        SAMPLE_HY2,
        SAMPLE_VMESS,
        SAMPLE_TROJAN,
        SAMPLE_SS,
    ])
    b64_body = base64.b64encode(raw_lines.encode('utf-8')).decode('ascii')
    sub = happ_client.parse_subscription_payload(b64_body)
    assert sub['meta']['title'] == 'Мой Прокси'
    assert len(sub['nodes']) == 5
    assert sub['meta']['userinfo']['total'] == 107374182400
    assert sub['meta']['userinfo']['used'] == 1048576 + 2147483648
    assert sub['meta']['userinfo']['expire'] == 1893456000
    assert sub['meta']['announce'] == 'Серверы обновлены'
    assert sub['meta']['update_interval_hours'] == 6


def test_parse_happ_routing_profile_and_encrypted_subscription():
    route_payload = {
        'Name': 'RU Bypass Rule',
        'GlobalProxy': 'false',
        'DirectDomains': ['domain:yandex.ru', 'geosite:category-ru'],
        'DirectIps': ['geoip:ru', 'geoip:private'],
        'ProxyDomains': ['domain:youtube.com'],
        'BlockDomains': ['geosite:category-ads-all'],
    }
    raw_b64 = base64.urlsafe_b64encode(json.dumps(route_payload).encode('utf-8')).decode('ascii').rstrip('=')
    rule = happ_client.parse_happ_routing_profile('happ://routing/onadd/' + raw_b64)
    assert rule['name'] == 'RU Bypass Rule'
    assert rule['global_proxy'] is False
    assert 'domain:youtube.com' in rule['proxy_domains']
    assert 'geoip:ru' in rule['direct_ips']

    # AES-128-GCM encrypted subscription with ?key=
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key_bytes = b'0123456789abcdef'
    nonce = b'123456789012'
    ct = AESGCM(key_bytes).encrypt(nonce, SAMPLE_VLESS_REALITY_XHTTP.encode('utf-8'), None)
    enc_tag = 'encrypt-tag:' + base64.b64encode(nonce + ct).decode('ascii')
    url = 'https://sub.example.org/feed?key=' + base64.urlsafe_b64encode(key_bytes).decode('ascii').rstrip('=')
    dec = happ_client.decrypt_subscription_body(enc_tag, url)
    assert dec.strip() == SAMPLE_VLESS_REALITY_XHTTP


def test_build_xray_and_singbox_configs_and_routing(tmp_path):
    (tmp_path / 'geoip.dat').write_bytes(b'stub')
    (tmp_path / 'geosite.dat').write_bytes(b'stub')
    node_xhttp = happ_client.parse_proxy_uri(SAMPLE_VLESS_REALITY_XHTTP)
    xcfg = happ_client.build_xray_config(
        node_xhttp,
        settings={
            'fragment_enabled': True,
            'fragment_length': '20-60',
            'fragment_interval': '10-30',
            'routing_mode': 'bypass_lan_ru',
            'allow_lan': True,
        },
        socks_port=10808,
        http_port=10809,
        asset_dir=str(tmp_path),
    )
    assert xcfg['inbounds'][0]['listen'] == '0.0.0.0'
    assert xcfg['inbounds'][1]['protocol'] == 'http'
    proxy_out = next(o for o in xcfg['outbounds'] if o['tag'] == 'proxy')
    assert proxy_out['streamSettings']['network'] == 'xhttp'
    assert proxy_out['streamSettings']['sockopt']['dialerProxy'] == 'fragment'
    assert any(o['tag'] == 'fragment' for o in xcfg['outbounds'])
    assert any('geoip:ru' in (r.get('ip') or []) for r in xcfg['routing']['rules'])

    # Without geoip.dat / geosite.dat in asset_dir, geo rules must not crash Xray
    xcfg_nogeo = happ_client.build_xray_config(
        node_xhttp,
        settings={'routing_mode': 'bypass_lan_ru'},
        asset_dir=str(tmp_path / 'empty'),
    )
    for r in xcfg_nogeo['routing']['rules']:
        for item in (r.get('ip') or []) + (r.get('domain') or []):
            assert not item.startswith('geoip:')
            assert not item.startswith('geosite:')

    # sing-box 1.14.4 TUN-over-Xray config
    scfg_bridge = happ_client.build_singbox_config(
        mode='tun',
        upstream_socks_port=18080,
        extra_excludes=['203.0.113.10/32'],
        settings={'routing_mode': 'global'},
    )
    tun_in = next(i for i in scfg_bridge['inbounds'] if i['type'] == 'tun')
    assert tun_in['address'] == ['172.19.0.1/30', 'fdfe:dcba:9876::1/126']
    assert '203.0.113.10/32' in tun_in['route_exclude_address']
    assert any(o.get('type') == 'socks' and o.get('server_port') == 18080 for o in scfg_bridge['outbounds'])
    assert any(r.get('process_name') == ['xray.exe'] and r.get('outbound') == 'direct' for r in scfg_bridge['route']['rules'])

    # sing-box standalone Hysteria2
    node_hy2 = happ_client.parse_proxy_uri(SAMPLE_HY2)
    scfg_hy2 = happ_client.build_singbox_config(node=node_hy2, mode='mixed', settings={'routing_mode': 'bypass_lan_ru'})
    hy_out = next(o for o in scfg_hy2['outbounds'] if o['tag'] == 'vpn')
    assert hy_out['type'] == 'hysteria2'
    assert hy_out['obfs']['type'] == 'salamander'


def test_engine_plan_selection():
    xhttp = happ_client.parse_proxy_uri(SAMPLE_VLESS_REALITY_XHTTP)
    hy2 = happ_client.parse_proxy_uri(SAMPLE_HY2)
    assert happ_client.choose_engine_plan(xhttp, {'mode': 'tun'}, has_xray=True, has_singbox=True) == 'xray_with_singbox_tun'
    assert happ_client.choose_engine_plan(xhttp, {'mode': 'proxy'}, has_xray=True, has_singbox=True) == 'xray_proxy'
    assert happ_client.choose_engine_plan(hy2, {'mode': 'tun'}, has_xray=True, has_singbox=True) == 'singbox_tun'
    assert happ_client.choose_engine_plan(hy2, {'mode': 'proxy'}, has_xray=True, has_singbox=True) == 'singbox_proxy'


def test_api_happ_import_ping_and_dual_core_connect(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    api = Api()

    # 1. crypt4 link gives helpful bridge message without crash
    c_res = api.happ_import('happ://crypt4/ABCDEF123456')
    assert c_res.get('crypt_link') is True
    assert 'Happ Desktop' in c_res['error']

    # 2. HTTP subscription fetch uses Happ/4.8.6/Windows User-Agent & HWID
    captured_req = {}
    body = '\n'.join([SAMPLE_VLESS_REALITY_XHTTP, SAMPLE_HY2]).encode('utf-8')
    fake_resp = mock.MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = body
    fake_resp.geturl.return_value = 'https://panel.example.org/sub'
    fake_resp.headers = {
        'Profile-Title': 'base64:' + base64.b64encode(b'Test Sub').decode('ascii'),
        'Subscription-Userinfo': 'upload=100; download=200; total=10000; expire=1900000000',
    }

    def fake_urlopen(req, timeout=12, context=None):
        captured_req['url'] = req.full_url
        captured_req['headers'] = dict(req.header_items())
        return fake_resp

    with mock.patch('happ_client.urllib.request.urlopen', side_effect=fake_urlopen):
        imp = api.happ_import('happ://add/https://panel.example.org/sub')
    assert imp['ok'] is True
    assert captured_req['url'] == 'https://panel.example.org/sub'
    ua_hdrs = {k.lower(): v for k, v in captured_req['headers'].items()}
    assert 'happ/' in ua_hdrs['user-agent'].lower()
    assert 'x-hwid' in ua_hdrs

    # 3. Fastest node selection via TCP ping mock
    with mock.patch('happ_client.ping_tcp_latency', side_effect=[145, 42]):
        fast = api.happ_select_fastest(imp['active_sub_id'])
    assert fast['ok'] is True
    assert fast['fastest_node']['latency_ms'] in (42, 145)

    # 4. Dual-core xray + sing-box TUN launch + disconnect cleanup
    sub = next(s for s in fast['subscriptions'] if s['id'] == fast['active_sub_id'])
    xhttp_node = next(n for n in sub['nodes'] if 'XHTTP' in n['summary'])
    api.happ_select_node(sub['id'], xhttp_node['id'])

    popen_calls = []

    def make_proc(cmd, **kwargs):
        popen_calls.append(cmd)
        proc = mock.MagicMock()
        proc.stdin = mock.MagicMock()
        proc.stdout = mock.MagicMock()
        if 'xray' in os.path.basename(cmd[0]).lower():
            proc.stdout.readline.side_effect = ['Xray 26.11.9 started\n', '']
        else:
            proc.stdout.readline.side_effect = ['INFO[0000] sing-box started (0.04s)\n', '']
        proc.poll.return_value = None
        proc.wait.return_value = 0
        return proc

    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, '_xray_exe', return_value=str(tmp_path / 'xray.exe')), \
         mock.patch.object(api, '_singbox_exe', return_value=str(tmp_path / 'sing-box.exe')), \
         mock.patch('subprocess.Popen', side_effect=make_proc):
        (tmp_path / 'xray.exe').write_bytes(b'MZ')
        (tmp_path / 'sing-box.exe').write_bytes(b'MZ')
        conn = api.happ_connect()
        assert conn['ok'] is True
        assert conn['plan'] == 'xray_with_singbox_tun'
        assert len(popen_calls) == 2
        assert popen_calls[0][1:] == ['run', '-format', 'json', '-c', 'stdin:']
        assert popen_calls[1][1:] == ['run', '-c', 'stdin']

        disc = api.disconnect_vpn()
        assert disc['ok'] is True
        assert api.openvpn_process is None
        assert api._vpn_aux_process is None


def test_adapter_classification_recognizes_happ_and_singbox():
    assert Api._classify_adapter({'name': 'NEON-VLESS', 'device': 'Wintun Userspace Tunnel'}) == 'vpn'
    assert Api._classify_adapter({'name': 'Happ TUN', 'device': 'Happ Tunnel Adapter'}) == 'vpn'
    assert Api._classify_adapter({'name': 'tun2proxy', 'device': 'tun2proxy-bin'}) == 'vpn'


def test_tr_translations_contain_zero_forbidden_vpn_words():
    ui_qt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ui_qt.py')
    with open(ui_qt_path, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename='ui_qt.py')
    tr_dict = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == 'TR':
                    tr_dict = ast.literal_eval(node.value)
                    break
    assert isinstance(tr_dict, dict)
    for lang_key in ('en', 'ru'):
        for k, val in tr_dict[lang_key].items():
            assert not re.search(r'\bvpn\b|openvpn', val, re.I), f'Forbidden word in TR[{lang_key}][{k}]: {val}'


def test_ui_html_contains_happ_and_askpass_controls():
    html_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ui.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
    for token in ('happImport()', 'happConnect()', 'happPing()', 'happBest()', 'vpnKeyPass'):
        assert token in html
