from unittest import mock

import pytest

from core import Api


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    instance = Api()
    yield instance
    instance.close()


def adapters():
    return [
        {'name': 'Wi-Fi', 'category': 'wifi', 'status': 2, 'has_default_gw': True},
        {'name': 'Ethernet', 'category': 'ethernet', 'status': 2, 'has_default_gw': False, 'hardware': True},
    ]


def test_direct_ethernet_uses_physical_uplink_without_vpn(api):
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': adapters()}), \
         mock.patch.object(api, 'set_sharing', return_value={'ok': True}) as share:
        result = api.start_direct_ethernet()
    assert result['ok']
    assert result['public_adapter'] == 'Wi-Fi'
    share.assert_called_once_with('Wi-Fi', 'Ethernet')
    assert api.ethernet_active and api.ethernet_adapter == 'Ethernet'


@pytest.mark.parametrize('case', ['vpn', 'hotspot', 'existing_ics', 'uplink_target', 'ambiguous'])
def test_direct_ethernet_refuses_unsafe_or_conflicting_setup(api, case):
    data = adapters()
    target = None
    if case == 'vpn':
        api.vpn_state = 'CONNECTED'
    elif case == 'hotspot':
        api.hotspot_active = True
    elif case == 'existing_ics':
        data[0]['shared'] = True
    elif case == 'uplink_target':
        target = 'Wi-Fi'
    elif case == 'ambiguous':
        data.append({'name': 'Ethernet uplink', 'category': 'ethernet', 'status': 2, 'has_default_gw': True})
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': data}), \
         mock.patch.object(api, 'set_sharing') as share:
        result = api.start_direct_ethernet(target)
    assert 'error' in result
    share.assert_not_called()
    api.hotspot_active = False


@pytest.mark.parametrize('vpn', [True, False])
def test_combined_sharing_starts_both_and_stops_together(api, vpn):
    data = adapters()
    data.append({'name': 'Hosted', 'category': 'hotspot', 'status': 2})
    if vpn:
        data.append({'name': 'OpenVPN', 'category': 'vpn', 'status': 2, 'has_vpn_route': True})
        api.vpn_state = 'CONNECTED'
        api.vpn_adapter_name = 'OpenVPN'
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, '_driver_supports_hosted_network', return_value=True), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': data}), \
         mock.patch.object(api, '_detect_hotspot_and_vpn_adapters', return_value=(None, 'Hosted', data)), \
         mock.patch.object(api, '_run_netsh', return_value=(0, 'ok', '')) as netsh, \
         mock.patch.object(api, '_configure_combined_ics') as configure, \
         mock.patch.object(api, '_powershell', return_value='ok'):
        result = api.start_combined_sharing('NEON', 'Password123', None, vpn)
        assert result['ok']
        configure.assert_called_once_with('OpenVPN' if vpn else 'Wi-Fi', 'Ethernet', 'Hosted')
        assert api.hotspot_active and api.ethernet_active
        assert api.stop_vpn_ethernet()['ok']
        assert not api.hotspot_active and not api.ethernet_active and api._combined_sharing is None
        assert any(call.args[:3] == ('wlan', 'stop', 'hostednetwork') for call in netsh.call_args_list)


def test_combined_refuses_unverified_vpn_and_unsupported_hotspot(api):
    api.vpn_state = 'CONNECTED'
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, '_driver_supports_hosted_network', return_value=False), \
         mock.patch.object(api, '_run_netsh') as netsh:
        assert 'Hosted Network' in api.start_combined_sharing('NEON', 'Password123')['error']
        netsh.assert_not_called()
    data = adapters() + [{'name': 'OtherVPN', 'category': 'vpn', 'status': 2}]
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, '_driver_supports_hosted_network', return_value=True), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': data}), \
         mock.patch.object(api, '_run_netsh') as netsh:
        assert 'verified' in api.start_combined_sharing('NEON', 'Password123')['error']
        netsh.assert_not_called()


def test_combined_ics_failure_stops_hotspot(api):
    data = adapters()
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, '_driver_supports_hosted_network', return_value=True), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': data}), \
         mock.patch.object(api, '_detect_hotspot_and_vpn_adapters', return_value=(None, 'Hosted', data)), \
         mock.patch.object(api, '_run_netsh', return_value=(0, 'ok', '')) as netsh, \
         mock.patch.object(api, '_configure_combined_ics', side_effect=RuntimeError('Windows refused')):
        result = api.start_combined_sharing('NEON', 'Password123', None, False)
    assert 'Windows refused' in result['error']
    assert not api.hotspot_active and not api.ethernet_active
    assert any(call.args[:3] == ('wlan', 'stop', 'hostednetwork') for call in netsh.call_args_list)


def test_combined_vpn_drop_during_setup_stops_both(api):
    data = adapters() + [{'name': 'OpenVPN', 'category': 'vpn', 'status': 2}]
    api.vpn_state = 'CONNECTED'
    api.vpn_adapter_name = 'OpenVPN'
    def drop(*args):
        api.vpn_state = 'DISCONNECTED'
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, '_driver_supports_hosted_network', return_value=True), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': data}), \
         mock.patch.object(api, '_detect_hotspot_and_vpn_adapters', return_value=(None, 'Hosted', data)), \
         mock.patch.object(api, '_run_netsh', return_value=(0, 'ok', '')), \
         mock.patch.object(api, '_configure_combined_ics', side_effect=drop), \
         mock.patch.object(api, '_powershell', return_value='ok'):
        result = api.start_combined_sharing('NEON', 'Password123')
    assert 'disconnected' in result['error']
    assert not api.hotspot_active and not api.ethernet_active


def test_direct_ethernet_allows_after_stale_vpn_error_and_names_conflicting_ics(api):
    # Stale vpn_state == 'ERROR' without live child process must not block direct Ethernet sharing
    api.vpn_state = 'ERROR'
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': adapters()}), \
         mock.patch.object(api, 'set_sharing', return_value={'ok': True}) as share:
        res = api.start_direct_ethernet()
    assert res['ok'] is True
    assert api.vpn_state == 'DISCONNECTED'
    share.assert_called_once_with('Wi-Fi', 'Ethernet')

    # Live child process must still refuse even if vpn_state == 'ERROR'
    api.ethernet_active = False
    api.vpn_state = 'ERROR'
    live_proc = mock.Mock()
    live_proc.poll.return_value = None
    api.openvpn_process = live_proc
    with mock.patch('platform.system', return_value='Windows'):
        refused = api.start_direct_ethernet()
    assert 'Disconnect Conf' in refused['error']
    api.openvpn_process = None
    api.vpn_state = 'DISCONNECTED'

    # Existing ICS refusal names the active shared adapter(s) and suggests Disable All ICS
    data = adapters()
    data[0]['shared'] = True
    data[0]['sharing_type'] = 0
    with mock.patch('platform.system', return_value='Windows'), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': data}), \
         mock.patch.object(api, 'set_sharing') as share2:
        ics_err = api.start_direct_ethernet()
    assert 'Wi-Fi (Public)' in ics_err['error']
    assert 'Disable All ICS' in ics_err['error']
    share2.assert_not_called()
