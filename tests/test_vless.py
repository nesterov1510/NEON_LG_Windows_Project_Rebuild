import io
import json
from unittest.mock import Mock, patch

import pytest

import core
import vless

LINK = 'vless://d5203810-2aab-4043-b036-543e2e39647e@vpn.example:443?security=tls'


def test_tls_websocket_tun():
    cfg = vless.configuration(LINK + '&type=ws&path=%2Fservice&host=ws.example#office')
    proxy, direct = cfg['outbounds']
    assert proxy['transport'] == {'type': 'ws', 'path': '/service', 'headers': {'Host': 'ws.example'}}
    assert proxy['tls'] == {'enabled': True, 'server_name': 'vpn.example'}
    assert direct == {'type': 'direct', 'tag': 'direct'}
    assert cfg['inbounds'][0]['auto_route'] and cfg['route']['final'] == proxy['tag']


def test_tunnel_never_carries_its_own_server_address():
    """ICS/Mobile Hotspot enable forwarding, and Windows then ignores sing-box's
    interface binding — without a host route out of the TUN the tunnel dials itself."""
    cfg = vless.configuration(LINK, server_addresses=['203.0.113.9', '2001:db8::9', 'not-an-ip'])
    proxy, = [o for o in cfg['outbounds'] if o['tag'] == 'vpn']
    assert proxy['server'] == '203.0.113.9'
    assert proxy['tls']['server_name'] == 'vpn.example'
    tun = cfg['inbounds'][0]
    assert '203.0.113.9/32' in tun['route_exclude_address']
    assert '2001:db8::9/128' in tun['route_exclude_address']
    assert 'not-an-ip/32' not in tun['route_exclude_address']
    assert cfg['route']['rules'][0] == {'ip_cidr': ['203.0.113.9/32', '2001:db8::9/128'],
                                        'outbound': 'direct'}


def test_literal_server_kept_and_unresolved_link_has_no_bypass():
    literal = 'vless://00000000-0000-4000-8000-000000000001@203.0.113.9:443?security=tls'
    cfg = vless.configuration(literal, server_addresses=['203.0.113.9'])
    proxy, = [o for o in cfg['outbounds'] if o['tag'] == 'vpn']
    assert proxy['server'] == '203.0.113.9'
    assert '203.0.113.9/32' in cfg['inbounds'][0]['route_exclude_address']
    assert cfg['route']['rules'] == [{'ip_cidr': ['203.0.113.9/32'], 'outbound': 'direct'},
                                     {'port': 53, 'action': 'hijack-dns'}]
    plain = vless.configuration(LINK)
    assert [o['tag'] for o in plain['outbounds']] == ['vpn', 'direct']
    assert plain['route']['rules'] == [{'port': 53, 'action': 'hijack-dns'}]


def test_dns_stays_reachable_for_shared_clients_and_self_signed_profiles():
    tun = vless.configuration(LINK)['inbounds'][0]
    # `hijack` (the default with auto_route) also blocks port 53 outside the TUN on
    # Windows with strict_route, which silences the resolver ICS clients depend on.
    assert tun['dns_mode'] == 'native'
    assert 'insecure' not in vless.configuration(LINK)['outbounds'][0]['tls']
    allowed = vless.configuration(LINK + '&allowInsecure=1')['outbounds'][0]['tls']
    assert allowed['insecure'] is True and allowed['server_name'] == 'vpn.example'


def test_reality_grpc():
    cfg = vless.configuration(LINK.replace('=tls', '=reality') + '&sni=example.org&pbk=' + 'A'*43 + '&sid=ab&type=grpc&serviceName=hello')
    proxy = cfg['outbounds'][0]
    assert proxy['tls']['reality']['short_id'] == 'ab'
    assert proxy['transport']['service_name'] == 'hello'


@pytest.mark.parametrize('link', [LINK.replace(':443', ':99999'), LINK.replace('vless:', 'file:'), LINK + '&type=xhttp', LINK.replace('=tls', '=none'), LINK + '&security=tls'])
def test_bad_links_never_echo_credentials(link):
    with pytest.raises(ValueError) as exc:
        vless.configuration(link)
    assert LINK[8:44] not in str(exc.value)


def test_engine_is_bundled_and_link_only_goes_to_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    api = core.Api()
    exe = tmp_path / 'assets/vless/sing-box.exe'
    exe.parent.mkdir(parents=True)
    exe.touch()
    proc = Mock()
    proc.poll.return_value = None
    with patch('core.platform.system', return_value='Windows'), patch('core.resource', lambda p: str(tmp_path / p)), \
            patch('core.subprocess.Popen', return_value=proc) as spawn, patch('core.threading.Thread'):
        assert api.connect_vless(LINK)['ok']
        assert spawn.call_args.args[0] == [str(exe), 'run', '-c', 'stdin']
        assert json.loads(proc.stdin.write.call_args.args[0]) == vless.configuration(LINK)
        proc.stdin.close.assert_called_once()
        assert 'error' in api.connect_vless(LINK)
        assert api.disconnect_vpn()['ok']
        proc.terminate.assert_called_once()
    proc.stdout = io.StringIO('INFO sing-box started (0.2s)\n' + LINK + '\n')
    proc.wait.return_value = 1
    api.openvpn_process = proc
    api._vpn_stop_requested = False
    api._read_openvpn_log(proc, True)
    assert api.vpn_state == 'ERROR' and LINK not in '\n'.join(api.vpn_logs)
    api.close()


def test_connect_resolves_the_server_before_the_tunnel_exists(tmp_path, monkeypatch):
    """The tunnel must never dial itself: NEON pins the server address outside the TUN."""
    monkeypatch.setenv('APPDATA', str(tmp_path))
    api = core.Api()
    exe = tmp_path / 'assets/vless/sing-box.exe'
    exe.parent.mkdir(parents=True)
    exe.touch()
    proc = Mock()
    proc.poll.return_value = None
    proc.stdin = Mock()
    with patch('core.platform.system', return_value='Windows'), \
            patch('core.resource', lambda p: str(tmp_path / p)), \
            patch.object(core.Api, '_lookup_addresses', return_value=['203.0.113.9']), \
            patch('core.subprocess.Popen', return_value=proc), patch('core.threading.Thread'):
        assert api.connect_vless(LINK)['ok']
    cfg = json.loads(proc.stdin.write.call_args.args[0])
    assert [o for o in cfg['outbounds'] if o['tag'] == 'vpn'][0]['server'] == '203.0.113.9'
    assert '203.0.113.9/32' in cfg['inbounds'][0]['route_exclude_address']
    assert any('203.0.113.9' in line for line in api.vpn_logs)
    api.close()


def test_unresolved_server_still_starts_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    api = core.Api()
    exe = tmp_path / 'assets/vless/sing-box.exe'
    exe.parent.mkdir(parents=True)
    exe.touch()
    proc = Mock()
    proc.poll.return_value = None
    proc.stdin = Mock()
    with patch('core.platform.system', return_value='Windows'), \
            patch('core.resource', lambda p: str(tmp_path / p)), \
            patch.object(core.Api, '_lookup_addresses', return_value=[]), \
            patch('core.subprocess.Popen', return_value=proc), patch('core.threading.Thread'):
        assert api.connect_vless(LINK)['ok']
    assert any('not resolved before start' in line for line in api.vpn_logs)
    api.close()


def test_engine_errors_are_shown_without_leaking_the_profile():
    api = core.Api()
    api._vless_secret = 'd5203810-2aab-4043-b036-543e2e39647e'
    # Info chatter stays out of the terminal, problems do not.
    assert api._vless_log_lines('INFO[0000] outbound/vless: connection established') == []
    lines = api._vless_log_lines(
        'ERROR[0001] inbound/tun[NEON-VLESS]: vless://{}@vpn.example:443 x509 certificate failed'.format(
            api._vless_secret))
    assert len(lines) == 2
    joined = '\n'.join(lines)
    assert 'd5203810-2aab-4043' not in joined and LINK[8:44] not in joined
    assert 'allowInsecure=1' in joined
    assert api._vless_log_lines('ERROR[0002] tls: handshake failure')[-1].startswith('[!] VLESS: the TLS handshake')
    assert api._vless_log_lines('ERROR[0003] permission denied')[-1].startswith('[!] VLESS:')
    api.close()


def test_probe_reports_a_dead_tunnel(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    api = core.Api()
    api.vpn_profile = 'VLESS'
    api.vpn_state = 'CONNECTED'
    api.vpn_logs = []
    monkeypatch.setattr(core.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(api, '_probe_tunnel_dns', lambda **kw: True)
    monkeypatch.setattr(api, '_probe_tunnel_egress', lambda *a, **kw: False)
    api._probe_vless_tunnel()
    assert any('no traffic gets through' in line for line in api.vpn_logs)

    api.vpn_logs, api.vpn_state, api.vpn_profile = [], 'CONNECTED', 'VLESS'
    monkeypatch.setattr(api, '_probe_tunnel_egress', lambda *a, **kw: True)
    api._probe_vless_tunnel()
    assert any('internet reachable through the tunnel' in line for line in api.vpn_logs)

    # A different engine (or a stopped tunnel) must never be probed.
    api.vpn_logs, api.vpn_profile = [], 'corp'
    api._probe_vless_tunnel()
    assert api.vpn_logs == []
    api.close()


def test_strict_route_is_configurable_for_sharing_boxes():
    # strict_route installs WFP leak blockers, but it has been seen to interfere
    # with hosted-network/ICS forwarding on some drivers; the escape hatch must
    # stay one setting deep rather than one code edit deep.
    strict = vless.configuration(LINK)
    assert strict['inbounds'][0]['strict_route'] is True
    relaxed = vless.configuration(LINK, strict_route=False)
    assert relaxed['inbounds'][0]['strict_route'] is False
    # Turning it off must not touch the tunnel's own routing.
    relaxed['inbounds'][0].pop('strict_route')
    strict['inbounds'][0].pop('strict_route')
    assert relaxed['inbounds'] == strict['inbounds']


def test_tun_excludes_lan_ics_and_ssdp_but_keeps_tun_ip():
    import ipaddress

    cfg = vless.configuration(LINK)
    tun = cfg['inbounds'][0]
    excludes = [ipaddress.ip_network(c) for c in tun['route_exclude_address']]
    assert any(ipaddress.ip_address('192.168.137.1') in net for net in excludes)
    assert any(ipaddress.ip_address('192.168.1.50') in net for net in excludes)
    assert any(ipaddress.ip_address('10.0.0.5') in net for net in excludes)
    assert any(ipaddress.ip_address('239.255.255.250') in net for net in excludes)
    # Never exclude the TUN interface subnet itself
    assert not any(ipaddress.ip_address('172.19.0.1') in net for net in excludes)
    assert not any(ipaddress.ip_address('172.19.0.2') in net for net in excludes)
