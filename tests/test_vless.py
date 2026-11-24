import io
import json
from unittest.mock import Mock, patch

import pytest

import core
import vless

LINK = 'vless://d5203810-2aab-4043-b036-543e2e39647e@vpn.example:443?security=tls'


def test_tls_websocket_tun():
    cfg = vless.configuration(LINK + '&type=ws&path=%2Fservice&host=ws.example#office')
    proxy, = cfg['outbounds']
    assert proxy['transport'] == {'type': 'ws', 'path': '/service', 'headers': {'Host': 'ws.example'}}
    assert proxy['tls'] == {'enabled': True, 'server_name': 'vpn.example'}
    assert cfg['inbounds'][0]['auto_route'] and cfg['route']['final'] == proxy['tag']


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
