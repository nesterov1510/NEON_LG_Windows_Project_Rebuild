import os
from unittest import mock
import pytest

QtWidgets = pytest.importorskip('PyQt5.QtWidgets', exc_type=ImportError)
QApplication = QtWidgets.QApplication

from app_legacy import ConfirmDialog, NeonWindow, _setup_windows_environment  # noqa: E402
from core import Api  # noqa: E402


@pytest.fixture(scope='module')
def qt_app():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    _setup_windows_environment()
    app = QApplication.instance() or QApplication([])
    yield app


def test_qt_window_tabs_i18n_and_auth_visibility(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    api = Api()
    win = NeonWindow(api=api)
    win.show()
    qt_app.processEvents()

    assert win.stack.count() == 7
    assert win.current_tab == 0
    assert not win.windowIcon().isNull()

    # Tab switching across LG, Samsung, Hisense, Conf, Diagnostics, Contacts and Samsung Apps
    win.switch_tab(1)
    assert win.current_tab == 1
    win.switch_tab(2)
    assert win.current_tab == 2
    win.switch_tab(3)
    assert win.current_tab == 3
    win.switch_tab(4)
    assert win.current_tab == 4
    win.switch_tab(5)
    assert win.page_title.text() == 'MSB CALLBACK'
    win.switch_tab(6)
    assert win.current_tab == 6
    assert 'SAMSUNG' in win.page_title.text().upper()
    win.switch_tab(0)

    # Language switch RU <-> EN (ensure no unintended & mnemonic markers)
    initial_lang = win.lang
    win.toggle_language()
    assert win.lang != initial_lang
    for btn in win.tab_buttons:
        txt = btn.text().strip()
        assert txt != ''
        assert '&' not in txt
    win.toggle_language()
    assert win.lang == initial_lang

    # Rain FX toggle & ToggleSwitch sync
    assert win.backdrop.fx_enabled is True
    assert win.fx_switch.isChecked() is True
    win.toggle_fx()
    assert win.backdrop.fx_enabled is False
    assert win.fx_switch.isChecked() is False
    win.toggle_fx()
    assert win.backdrop.fx_enabled is True
    assert win.fx_switch.isChecked() is True

    # Auth box auto-shows when selecting auth-user-pass profile
    ovpn_auth = tmp_path / 'auth.ovpn'
    ovpn_auth.write_text('client\nauth-user-pass\n', encoding='utf-8')
    ovpn_cert = tmp_path / 'cert.ovpn'
    ovpn_cert.write_text('client\n', encoding='utf-8')
    api.remember_profile(str(ovpn_auth))
    api.remember_profile(str(ovpn_cert))

    win.switch_tab(3)
    win.profile_combo.addItems(['auth', 'cert'])
    win.profile_combo.setCurrentText('auth')
    qt_app.processEvents()
    assert win.vpn_auth_box.isVisible() is True

    win.profile_combo.setCurrentText('cert')
    qt_app.processEvents()
    assert win.vpn_auth_box.isVisible() is False

    # Custom & preset Area Option manual input sync
    from ui_qt import REGIONS
    assert win.region_code_input.text() == '22282'
    win.region_code_input.setText('19589')
    qt_app.processEvents()
    assert win.region_combo.currentIndex() == len(REGIONS)
    assert win.kpi_area.val.text() == '19589'

    # Language switch preserves manually entered custom code
    win.toggle_language()
    assert win.region_code_input.text() == '19589'
    assert win.region_combo.currentIndex() == len(REGIONS)
    win.toggle_language()

    # Typing a preset code selects matching preset; selecting index 0 syncs back to 22282
    win.region_code_input.setText('3642')
    qt_app.processEvents()
    assert REGIONS[win.region_combo.currentIndex()][2] == '3642'
    win.region_combo.setCurrentIndex(0)
    qt_app.processEvents()
    assert win.region_code_input.text() == '22282'

    # Log writes to LG, Samsung, Hisense, and Conf terminals
    win.log('[+] Test synced log line')
    assert '[+] Test synced log line' in win.tv_term.edit.toPlainText()
    assert '[+] Test synced log line' in win.sam_term.edit.toPlainText()
    assert '[+] Test synced log line' in win.his_term.edit.toPlainText()
    assert '[+] Test synced log line' in win.vpn_term.edit.toPlainText()

    # Samsung TV tab controls, Local Set guide, discovery, connect, and PC arrow navigation
    win.switch_tab(1)
    assert win.sam_localset_combo.count() >= 8
    idx_usa = win.sam_localset_combo.findData('USA')
    win.sam_localset_combo.setCurrentIndex(idx_usa)
    qt_app.processEvents()
    assert 'USA' in win.sam_localset_hint.text()

    with mock.patch.object(api, 'discover_samsung', return_value={
        'ok': True,
        'devices': [{'ip': '192.168.1.55', 'name': 'Samsung QN90C', 'model': 'QE55QN90C', 'port': 8002}],
    }):
        win.discover_samsung()
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        assert win.sam_found_box.isVisible() is True
        assert win.sam_ip.text() == '192.168.1.55'

    with mock.patch.object(api, 'connect_samsung', return_value={
        'ok': True,
        'ip': '192.168.1.55',
        'port': 8002,
        'protocol': 'wss-8002',
        'token_saved': True,
        'info': {'name': 'Samsung QN90C', 'model': 'QE55QN90C', 'os': 'Tizen', 'country': 'DE', 'language': 'de_DE', 'power_state': 'on'},
        'output': 'Connected Samsung QN90C',
    }):
        win.connect_samsung()
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        assert win.sam_connected is True
        assert win.kpi_sam.val.text() == '192.168.1.55'
        assert win.kpi_sam_proto.val.text() == 'WSS-8002'
        assert 'DE' in win.kpi_sam_country.val.text()

    with mock.patch.object(api, 'samsung_send_key', return_value={'ok': True, 'output': 'Sent KEY_DOWN'}) as m_key:
        win.sam_kb_toggle.setChecked(True)
        win.sam_term.setFocus()
        QtGui = pytest.importorskip('PyQt5.QtGui')
        ev = QtGui.QKeyEvent(QtGui.QKeyEvent.KeyPress, pytest.importorskip('PyQt5.QtCore').Qt.Key_Down, pytest.importorskip('PyQt5.QtCore').Qt.NoModifier)
        win.keyPressEvent(ev)
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_key.assert_called_with('KEY_DOWN')

    # Samsung System Setup + Belet SDB UI controls
    assert win.btn_sam_sys_tune is not None
    assert win.btn_sam_sdb_check is not None
    assert win.btn_sam_cert is not None
    assert win.btn_sam_belet_install is not None
    assert win.btn_sam_belet_direct is not None
    assert win.btn_sam_belet_launch is not None
    assert win.btn_sam_belet_remove is not None
    assert len(win.sam_host_ip_lbl.text()) > 5

    with mock.patch.object(api, 'install_samsung_belet_client', return_value={
        'ok': True,
        'pkg_id': 'BeletFilm1',
        'app_id': 'BeletFilm1.BeletClient',
        'output': '[+] Belet (BeletFilm1.BeletClient) installed WITH bypass!',
    }) as m_sam_inst:
        win.install_samsung_belet_client()
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_sam_inst.assert_called_once()
        assert 'BeletFilm1.BeletClient' in win.sam_term.edit.toPlainText()

    with mock.patch.object(api, 'install_samsung_belet_direct_client', return_value={
        'ok': True,
        'pkg_id': 'BeletFilm1',
        'app_id': 'BeletFilm1.BeletClient',
        'output': '[+] Belet (BeletFilm1.BeletClient) installed WITHOUT bypass!',
    }) as m_sam_dir:
        win.install_samsung_belet_direct_client()
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_sam_dir.assert_called_once()
        assert 'WITHOUT bypass' in win.sam_term.edit.toPlainText()

    # LG page: the new native Belet client (tm.belet.neonclient) is installable from the UI
    prev_tv_connected = win.tv_connected
    assert win.btn_belet_lg_install is not None
    assert win.btn_belet_lg_launch is not None
    assert win.btn_belet_lg_remove is not None
    assert 'neonclient' in win.belet_lg_hint.text()

    with mock.patch.object(api, 'install_belet_lg_client', return_value={'ok': True, 'output': 'installed'}) as m_lg_inst:
        win.tv_connected = True
        win.install_lg_belet_client()
        deadline = __import__('time').time() + 2.0
        while win.tv_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_lg_inst.assert_called_once()

    with mock.patch.object(api, 'launch_belet_lg_client', return_value={'ok': True, 'output': 'launched'}) as m_lg_launch:
        win.launch_lg_belet_client()
        deadline = __import__('time').time() + 2.0
        while win.tv_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_lg_launch.assert_called_once()

    with mock.patch.object(api, 'remove_belet_lg_client', return_value={'ok': True, 'output': 'removed'}) as m_lg_rm, \
         mock.patch('app_legacy.ConfirmDialog.exec_', return_value=ConfirmDialog.Accepted):
        win.remove_lg_belet_client()
        deadline = __import__('time').time() + 2.0
        while win.tv_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_lg_rm.assert_called_once()
    win.tv_connected = prev_tv_connected

    # Check zero visible VPN/OpenVPN wording across EN/RU translations & live widgets
    import re
    from ui_qt import TR, SystemTuneDialog
    for lang_key, strings in TR.items():
        for k, val in strings.items():
            assert not re.search(r'\bvpn\b|openvpn', val, re.I), f'VPN leaked in TR[{lang_key}][{k}]: {val}'
    for btn in win.tab_buttons:
        assert 'vpn' not in btn.text().lower()
    assert 'vpn' not in win.hs_ssid.text().lower()
    assert 'vpn' not in win.diag_ovpn_card.lbl.text().lower()

    # SystemTuneDialog is purely visual in both LG and Samsung modes and calls zero Api mutating methods
    with mock.patch.object(api, 'connect', side_effect=AssertionError('must not connect')), \
         mock.patch.object(api, 'action', side_effect=AssertionError('must not act')), \
         mock.patch.object(api, 'install_belet_app', side_effect=AssertionError('must not install')), \
         mock.patch.object(api, 'install_samsung_belet', side_effect=AssertionError('must not install samsung')), \
         mock.patch.object(api, 'install_samsung_belet_client', side_effect=AssertionError('must not install samsung client')), \
         mock.patch.object(api, 'install_samsung_belet_direct_client', side_effect=AssertionError('must not install samsung direct')), \
         mock.patch.object(api, 'connect_samsung', side_effect=AssertionError('must not connect samsung')), \
         mock.patch.object(api, 'samsung_action', side_effect=AssertionError('must not send samsung action')), \
         mock.patch.object(api, 'connect_vpn', side_effect=AssertionError('must not start conf')), \
         mock.patch.object(api, 'start_vpn_hotspot', side_effect=AssertionError('must not start hotspot')), \
         mock.patch.object(api, 'start_vpn_ethernet', side_effect=AssertionError('must not start ethernet')):
        for tune_mode in ('lg', 'samsung'):
            dlg = SystemTuneDialog(win, lang=win.lang, mode=tune_mode)
            dlg.show()
            for _ in range(150):
                dlg._tick()
            qt_app.processEvents()
            assert dlg._progress >= 70
            assert len(dlg.hex_view.toPlainText()) > 50
            assert len(dlg.stream_view.toPlainText()) > 50
            dlg.close()

    # Ethernet Conf sharing + Custom DNS controls on Tab 3 (Conf · Wi-Fi · LAN)
    win.switch_tab(3)
    assert win.eth_combo is not None
    assert win.btn_eth_start is not None
    assert win.btn_eth_stop is not None
    assert win.side_eth_lbl is not None
    assert win.dns_preset_combo is not None
    assert win.dns_primary is not None
    assert win.dns_secondary is not None
    assert win.chk_dns_uplink is not None
    assert win.btn_dns_apply is not None
    assert win.btn_dns_reset is not None
    assert win.dns_preset_combo.currentData() == 'auto'
    assert win.dns_primary.isEnabled() is False

    # Selecting Cloudflare fills fields and enables inputs; manual edit promotes to custom & survives EN<->RU toggle
    cf_idx = win.dns_preset_combo.findData('cloudflare')
    win.dns_preset_combo.setCurrentIndex(cf_idx)
    qt_app.processEvents()
    assert win.dns_primary.isEnabled() is True
    assert win.dns_primary.text() == '1.1.1.1'
    assert win.dns_secondary.text() == '1.0.0.1'

    win.dns_primary.setText('9.9.9.9')
    win.dns_secondary.setText('1.1.1.1')
    win._on_dns_text_edited()
    qt_app.processEvents()
    assert win.dns_preset_combo.currentData() == 'custom'
    win.toggle_language()
    assert win.dns_preset_combo.currentData() == 'custom'
    assert win.dns_primary.text() == '9.9.9.9'
    win.toggle_language()

    # Invalid custom IP blocks apply_dns before hitting backend
    win.dns_primary.setText('999.999.1.1')
    win._on_dns_text_edited()
    with mock.patch.object(api, 'apply_sharing_dns') as m_dns_bad:
        win.apply_dns()
        m_dns_bad.assert_not_called()

    # Valid Apply DNS & Reset (Auto) persist through Api (mock OS network calls so Windows CI runners stay deterministic)
    def _wait_net(timeout=4.0):
        dl = __import__('time').time() + timeout
        while win.net_busy and __import__('time').time() < dl:
            win._pump()
            qt_app.processEvents()

    _wait_net()
    win.dns_preset_combo.setCurrentIndex(win.dns_preset_combo.findData('google'))
    qt_app.processEvents()
    with mock.patch('core.platform.system', return_value='Windows'), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': [
             {'name': 'OpenVPN Wintun', 'device': 'Wintun Userspace Tunnel', 'status': 2, 'shared': False, 'has_vpn_route': True, 'has_default_gw': False, 'category': 'vpn'},
         ]}), \
         mock.patch.object(api, '_set_adapter_ipv4_dns', return_value={'ok': True, 'adapter': 'OpenVPN Wintun', 'was_static': False, 'prev_static': [], 'previous': [], 'current': ['8.8.8.8', '8.8.4.4']}) as m_set_dns:
        win.apply_dns()
        _wait_net()
    assert win.net_busy is False
    m_set_dns.assert_called_once_with('OpenVPN Wintun', ['8.8.8.8', '8.8.4.4'])
    assert api.load_settings()['dns_mode'] == 'google'
    assert api.load_settings()['dns_primary'] == '8.8.8.8'

    with mock.patch('core.platform.system', return_value='Windows'), \
         mock.patch.object(api, 'network_connections', return_value={'adapters': []}):
        win.reset_dns()
        _wait_net()
    assert win.net_busy is False
    assert win.dns_preset_combo.currentData() == 'auto'
    assert win.dns_primary.isEnabled() is False
    assert api.load_settings()['dns_mode'] == 'auto'
    with mock.patch.object(api, 'network_connections', return_value={'adapters': [
        {'name': 'Broadcom', 'device': 'Broadcom NetLink (TM) Gigabit Ethernet', 'phys_media': '802.3', 'hardware': True, 'virtual': False, 'status': 2, 'shared': True, 'sharing_type': 0, 'has_default_gw': True, 'category': 'ethernet'},
        {'name': 'Wi-Fi', 'device': 'Broadcom 802.11ac Network Adapter', 'phys_media': 'Wireless LAN', 'hardware': True, 'virtual': False, 'status': 2, 'shared': False, 'has_default_gw': True, 'category': 'wifi'},
        {'name': 'Ethernet 2', 'device': 'Realtek USB GbE', 'phys_media': '802.3', 'hardware': True, 'virtual': False, 'status': 2, 'shared': False, 'has_default_gw': False, 'category': 'ethernet'},
        {'name': 'OpenVPN Connect', 'device': 'OpenVPN Data Channel Offload', 'status': 2, 'shared': False, 'has_vpn_route': True, 'category': 'vpn'},
    ]}):
        win.refresh_adapters()
        deadline = __import__('time').time() + 2.0
        while win.net_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        assert win.eth_combo.currentData() == 'Ethernet 2'
        assert 'OpenVPN Connect' in win.pub_combo.currentText()

    win.vpn_state = 'CONNECTED'
    api.vpn_state = 'CONNECTED'
    api.ethernet_active = True
    win._sync_status_badges()
    assert 'ETHERNET' in win.vpn_stat_val.text()
    assert 'ETHERNET' in win.kpi_vpn.val.text()
    api.ethernet_active = False
    api.vpn_state = 'DISCONNECTED'
    win.vpn_state = 'DISCONNECTED'
    win._sync_status_badges()

    # Check ui.html includes Samsung System Setup, Belet, Ethernet Conf sharing controls, and Alem TV portal selectors
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ui.html'), 'r', encoding='utf-8') as hf:
        ui_html = hf.read()
    for token in (
        "openSysTune('samsung')", 'checkSamSdb()', 'authSamCert()',
        'installSamBeletClient()', 'installSamBeletDirectClient()', 'launchSamBeletClient()', 'removeSamBeletClient()',
        'ethAdapter', 'startEthernetShare()', 'stopEthernetShare()',
        'dnsPreset', 'dnsPrimary', 'dnsSecondary', 'dnsApplyUplink', 'applySharingDns()', 'resetSharingDns()',
        'alemPortal', 'samAlemPortal', 'installAlem()', 'installSamAlem()',
        'samTimeMode', 'samTimePreset', 'samTimeTarget', 'samTimeSources',
        'samTimeChkCheck', 'samTimeUpstream', 'startSamTimeOverride()', 'stopSamTimeOverride()', 'syncSamTimeNow()',
        'tabHis', 'hisPage', 'hisStartPairing()', 'hisBeletMode', 'hisInstallBelet()', 'hisInstallAlem()', 'hisInstallCallback()',
        'installCallbackHisense()',
        'tabSamApps', 'samAppsPage', 'loadSamCatalog(', 'checkSamAppsTv()',
        'installSamCatalogApp()', 'launchSamCatalogApp()', 'uninstallSamCatalogApp()',
    ):
        assert token in ui_html

    # Hisense VIDAA tab controls (Tab 2)
    win.switch_tab(2)
    assert win.his_ip is not None
    assert win.btn_his_scan is not None
    assert win.btn_his_pair is not None
    assert win.his_belet_mode_combo.count() == 4
    assert win.his_belet_url_wrap.isVisible() is False
    idx_custom = win.his_belet_mode_combo.findData('custom_url')
    win.his_belet_mode_combo.setCurrentIndex(idx_custom)
    qt_app.processEvents()
    assert win.his_belet_url_wrap.isVisible() is True
    win.his_belet_mode_combo.setCurrentIndex(0)
    qt_app.processEvents()
    assert win.his_belet_url_wrap.isVisible() is False
    assert win.btn_his_belet_install is not None
    assert win.btn_his_alem_install is not None
    assert win.btn_his_cb_install is not None
    assert win.his_alem_combo.count() >= 7
    with mock.patch.object(api, 'discover_hisense', return_value={
        'ok': True,
        'devices': [{'ip': '192.168.1.77', 'name': 'Hisense 55U7HQ', 'model': 'VIDAA', 'brand': 'his'}],
    }):
        win.his_scan()
        deadline = __import__('time').time() + 2.0
        while win.his_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        assert win.his_found_wrap.isVisible() is True
        assert win.his_ip.text() == '192.168.1.77'

    with mock.patch.object(api, 'start_hisense_pairing', return_value={
        'ok': True,
        'status': 'pin_required',
        'pairing_id': 'pair-xyz',
        'host': '192.168.1.77',
        'auth_profile': 'modern',
        'brand': 'his',
    }):
        win.his_start_pairing()
        deadline = __import__('time').time() + 2.0
        while win.his_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        assert win.his_pin_wrap.isVisible() is True
        assert win._his_pairing_id == 'pair-xyz'

    with mock.patch.object(api, 'install_hisense_belet', return_value={
        'ok': True, 'confirmed': True, 'app_id': 'tm.belet.vidaa',
    }) as m_hb:
        win.his_install_belet()
        deadline = __import__('time').time() + 2.0
        while win.his_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_hb.assert_called_once()

    with mock.patch.object(api, 'install_hisense_callback', return_value={
        'ok': True, 'confirmed': True, 'app_id': 'neonmsbcallback',
    }) as m_hcb:
        win._install_callback_hisense()
        deadline = __import__('time').time() + 2.0
        while win.his_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_hcb.assert_called_once()

    # Check Alem TV Card portal selectors on webOS (Tab 0) and Samsung Tizen (Tab 1)
    assert win.alem_portal_combo is not None
    assert win.alem_portal_combo.count() >= 7
    assert win.btn_alem_install is not None
    assert win.sam_alem_portal_combo is not None
    assert win.sam_alem_portal_combo.count() >= 7
    assert win.btn_sam_alem_install is not None

    # Check Samsung Time Domain Override card widgets & start/stop flow
    assert win.sam_time_mode_combo.count() == 2
    assert win.sam_time_preset_combo.count() >= 6
    assert win.sam_time_target_input.text() == 'time.cloudflare.com'
    win.sam_time_target_input.setText('pool.ntp.org')
    qt_app.processEvents()
    assert win.sam_time_preset_combo.currentData() == 'pool.ntp.org'
    win.sam_time_target_input.setText('my-ntp.example.com')
    qt_app.processEvents()
    assert win.sam_time_preset_combo.currentData() == 'custom'
    with mock.patch.object(api, 'start_samsung_time_override', return_value={
        'ok': True,
        'running': True,
        'mode': 'gateway',
        'target': 'my-ntp.example.com',
        'pc_ip': '192.168.1.45',
        'dns_port': 53,
        'http_port': 80,
        'ntp_port': 123,
        'offset_ms': 15,
        'sync_method': 'SNTP :123',
        'stats': {'dns_overridden': 2, 'http_timesync': 1, 'http_check': 1, 'ntp_hits': 0},
        'output': '[+] Samsung Time Override ACTIVE',
    }) as m_st_start:
        win.start_samsung_time_override()
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_st_start.assert_called_once()
        assert win.btn_sam_time_stop.isEnabled() is True
        assert 'my-ntp.example.com' in win.sam_term.edit.toPlainText() or 'ACTIVE' in win.sam_time_status_lbl.text()

    with mock.patch.object(api, 'stop_samsung_time_override', return_value={
        'ok': True,
        'running': False,
        'pc_ip': '192.168.1.45',
        'output': '[+] Samsung Time Override stopped.',
    }) as m_st_stop:
        win.stop_samsung_time_override()
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_st_stop.assert_called_once()
        assert win.btn_sam_time_stop.isEnabled() is False

    # One-time PC sync, diagnostic, autonomous 24/7 SSH deploy & TV text entry controls
    assert win.sam_time_chk_autostop is not None
    assert win.btn_sam_time_diag is not None
    assert win.btn_sam_time_deploy is not None
    assert win.btn_sam_time_export is not None
    assert win.btn_sam_time_snippets is not None
    assert win.sam_text_input is not None
    assert win.btn_sam_send_text is not None
    assert win.btn_sam_hotel is not None

    with mock.patch.object(api, 'diagnose_samsung_time_reachability', return_value={
        'ok': True,
        'best_public_dns': '1.1.1.1',
        'output': '[+] Direct DNS 1.1.1.1 works without PC!',
    }) as m_diag:
        win.diagnose_samsung_time_reachability()
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_diag.assert_called_once()

    with mock.patch.object(api, 'samsung_send_text', return_value={'ok': True, 'output': 'Sent text'}) as m_txt:
        win.sam_text_input.setText('1.1.1.1')
        win.sam_send_text()
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_txt.assert_called_once_with('1.1.1.1')

    win._force_exit = True
    win.close()
    qt_app.processEvents()


def test_new_buttons_have_neon_styles(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    win = NeonWindow(api=Api())
    buttons = win.findChildren(QtWidgets.QPushButton)
    def by_text(text):
        matches = [b for b in buttons if b.text() == text]
        assert matches, text
        return matches
    for text, style in [
        ('Подключить пульт', 'PrimaryBtn'), ('Отключить', 'GhostBtn'),
        ('▲', 'SamKeyPrimary'), ('HOME', 'SamKeyBtn'),
        ('Belet TV · С обходом (1970 / TLS)', 'PrimaryBtn'),
        ('Belet TV · Без обхода (Новый клиент)', 'NormalBtn'),
        ('УСТАНОВИТЬ НА LG', 'PrimaryBtn'), ('УСТАНОВИТЬ НА SAMSUNG', 'PrimaryBtn'),
        ('УСТАНОВИТЬ НА HISENSE', 'PrimaryBtn'),
        ('Установить Belet на Hisense', 'PrimaryBtn'),
        ('Установить Alem TV на Hisense', 'PrimaryBtn'),
        ('Установить MSB Callback', 'PrimaryBtn'),
    ]:
        assert any(button.objectName() == style for button in by_text(text))
        assert 'QPushButton#' + style in win.styleSheet()
    win.close()


def test_happ_widgets_and_ovpn_askpass_prompt(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    api = Api()
    win = NeonWindow(api=api)
    win.show()
    win.switch_tab(3)
    qt_app.processEvents()

    # Happ built-in widgets present and functional
    assert win.happ_input is not None
    assert win.happ_sub_combo is not None
    assert win.happ_node_list is not None
    assert win.happ_mode_combo.count() == 3
    assert win.happ_route_combo.count() == 3

    # Importing a direct proxy link populates Happ UI list immediately
    res = api.happ_import('vless://11111111-1111-4111-8111-111111111111@203.0.113.10:443?security=tls#NL%20Node')
    assert res['ok'] is True
    win.happ_refresh_ui()
    qt_app.processEvents()
    assert win.happ_node_list.count() == 1
    assert 'NL Node' in win.happ_node_list.item(0).text()

    # OpenVPN profile with encrypted private key reveals askpass box & calls ConfAuthDialog
    ovpn_enc = tmp_path / 'enc.ovpn'
    ovpn_enc.write_text(
        'client\n<key>\n-----BEGIN ENCRYPTED PRIVATE KEY-----\nMII...\n-----END ENCRYPTED PRIVATE KEY-----\n</key>\n',
        encoding='utf-8',
    )
    api.remember_profile(str(ovpn_enc))
    win.profile_combo.addItem('enc')
    win.profile_combo.setCurrentText('enc')
    qt_app.processEvents()
    assert win.vpn_auth_box.isVisible() is True
    assert win.vpn_auth_user_wrap.isVisible() is False
    assert win.vpn_auth_key_wrap.isVisible() is True

    win._force_exit = True
    win.close()
    qt_app.processEvents()


def test_ui_ux_splitters_filters_badges_and_wheel_guard(qt_app, tmp_path, monkeypatch):
    from PyQt5 import QtCore, QtGui

    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    api = Api()
    win = NeonWindow(api=api)
    win.show()
    qt_app.processEvents()

    # Screen-adaptive sizing fits compact 1280x720 / 1366x768 displays cleanly
    assert win.minimumWidth() <= 960
    assert win.minimumHeight() <= 600
    assert win.width() <= 1240
    assert win.height() <= 750

    # LG, Samsung, Hisense, and Conf pages all use horizontal QSplitter
    for tab_idx in (0, 1, 2, 3):
        splitter = win.stack.widget(tab_idx).findChild(QtWidgets.QSplitter)
        assert splitter is not None
        assert splitter.orientation() == QtCore.Qt.Horizontal

    # Conf page segmented filter & collapsible cards
    win.switch_tab(3)
    qt_app.processEvents()
    assert win._conf_card_happ.isVisible() is True
    assert win._conf_card_ics.isVisible() is True
    assert win._conf_card_ics._collapsed is True

    win.btn_conf_seg_share.click()
    qt_app.processEvents()
    assert win._conf_card_eth.isVisible() is True
    assert win._conf_card_hs.isVisible() is True
    assert win._conf_card_happ.isVisible() is False
    assert win._conf_card_ovpn.isVisible() is False

    win.btn_conf_seg_dns.click()
    qt_app.processEvents()
    assert win._conf_card_dns.isVisible() is True
    assert win._conf_card_ics.isVisible() is True
    assert win._conf_card_ics._collapsed is False  # auto-expanded when DNS & ICS chosen

    win.btn_conf_seg_all.click()
    qt_app.processEvents()
    assert win._conf_card_happ.isVisible() is True

    # Selecting auth .ovpn auto-expands Card 2 even if collapsed
    ovpn_plain = tmp_path / 'plain.ovpn'
    ovpn_plain.write_text('client\n', encoding='utf-8')
    ovpn_auth = tmp_path / 'need_auth.ovpn'
    ovpn_auth.write_text('client\nauth-user-pass\n', encoding='utf-8')
    api.remember_profile(str(ovpn_plain))
    api.remember_profile(str(ovpn_auth))
    win.profile_combo.addItem('plain')
    win.profile_combo.addItem('need_auth')
    win.profile_combo.setCurrentText('plain')
    win._conf_card_ovpn.set_collapsed(True)
    assert win._conf_card_ovpn._collapsed is True
    win.profile_combo.setCurrentText('need_auth')
    qt_app.processEvents()
    assert win._conf_card_ovpn._collapsed is False
    assert win.vpn_auth_box.isVisible() is True

    # Live KPIs, quick-stop bar & inline badges sync cleanly
    assert win.btn_quick_disc.isEnabled() is False
    assert win.btn_quick_stop_share.isEnabled() is False
    assert win.happ_inline_badge.objectName() == 'InlineBadgeIdle'
    assert win.eth_inline_badge.objectName() == 'InlineBadgeIdle'

    win.vpn_state = 'CONNECTED'
    api.vpn_state = 'CONNECTED'
    api.vpn_profile = 'Happ: Edge Node'
    api.ethernet_active = True
    api.ethernet_iface = 'Ethernet'
    api.hotspot_active = True
    win._sync_status_badges()
    qt_app.processEvents()
    assert win.btn_quick_disc.isEnabled() is True
    assert win.btn_quick_stop_share.isEnabled() is True
    assert win.happ_inline_badge.objectName() == 'InlineBadgeOk'
    assert win.eth_inline_badge.objectName() == 'InlineBadgeOk'
    assert win.hs_inline_badge.objectName() == 'InlineBadgeOk'
    assert win.kpi_conf_conn.val.text() == 'CONNECTED'
    assert 'Ethernet' in win.kpi_conf_share.val.text()

    # Closed QComboBox wheel guard ignores wheel scroll so scrolling never changes values
    orig_idx = win.happ_mode_combo.currentIndex()
    wheel_ev = QtGui.QWheelEvent(
        QtCore.QPointF(10, 10),
        QtCore.QPointF(10, 10),
        QtCore.QPoint(0, -120),
        QtCore.QPoint(0, -120),
        QtCore.Qt.NoButton,
        QtCore.Qt.NoModifier,
        QtCore.Qt.ScrollUpdate,
        False,
    )
    assert win.eventFilter(win.happ_mode_combo, wheel_ev) is True
    assert win.happ_mode_combo.currentIndex() == orig_idx

    # Language switch updates segmented pills & right-pane KPI headers
    win.toggle_language()
    qt_app.processEvents()
    assert win.btn_conf_seg_share.text() != ''
    assert win.kpi_conf_conn.lbl.text() != ''

    win._force_exit = True
    win.close()
    qt_app.processEvents()


def test_samsung_apps_catalog_tab_interactions(qt_app, tmp_path, monkeypatch):
    from unittest import mock
    from samsung_catalog import normalize_catalog
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    api = Api()
    api.save_settings({
        'samsung_catalog_settings': {
            'jellyfin': {'server_url': 'https://saved.nas:8096'}
        }
    })
    raw_catalog = {
        'schemaVersion': 1,
        'categories': ['media', 'utility'],
        'category_colors': {'media': '#4caf50', 'utility': '#607d8b'},
        'apps': [
            {
                'file': 'Jellyfin-tizen6.wgt',
                'name': 'Jellyfin',
                'description': 'Media client Tizen 6+',
                'category': 'media',
                'group': 'jellyfin',
                'variant': 'Tizen 6+',
                'version': '1.0',
                'required_version': '6.0',
                'package_id': 'Jellyfin01',
                'app_id': 'Jellyfin01.Main',
                'cert_level': 'public',
                'repo': 'jellyfin/jellyfin-tizen',
                'user_settings': {
                    'cfg.js': [
                        {'id': 'server_url', 'label': 'Server URL', 'type': 'text', 'required': True, 'find': 'X', 'replace': '{value}'},
                        {'id': 'api_secret_token', 'label': 'API Secret', 'type': 'text', 'secret': True, 'required': False, 'find': 'Y', 'replace': '{value}'},
                    ]
                },
            },
            {
                'file': 'Jellyfin-tizen5.wgt',
                'name': 'Jellyfin',
                'description': 'Media client Tizen 5',
                'category': 'media',
                'group': 'jellyfin',
                'variant': 'Tizen 5',
                'version': '0.9',
                'required_version': '5.0',
                'package_id': 'Jellyfin01',
                'app_id': 'Jellyfin01.Main',
                'cert_level': 'public',
                'repo': 'jellyfin/jellyfin-tizen',
            },
            {
                'file': 'Overscan.tpk',
                'name': 'Overscan',
                'description': 'Display border tool',
                'category': 'utility',
                'version': '1.0',
                'required_version': '6.0',
                'package_id': 'org.apps2samsung.overscan',
                'app_id': 'org.apps2samsung.overscan.ui',
                'cert_level': 'partner',
                'flags': ['root'],
                'repo': 'Apps2Samsung/Overscan',
            },
            {
                'file': 'AdultPortal.wgt',
                'name': 'AdultPortal',
                'description': '18+ catalog item',
                'category': 'media',
                'version': '1.0',
                'required_version': '5.0',
                'package_id': 'Adult00001',
                'app_id': 'Adult00001.Main',
                'adult': True,
            },
        ],
        'removed': [
            {
                'files': ['OldGone.wgt'],
                'reason': 'upstream_archived',
                'note': 'Removed',
                'since': '2026-08-01',
                'package_ids': ['OldGone001'],
            }
        ],
    }
    norm_catalog = normalize_catalog(
        raw_catalog,
        assets_map={
            'Jellyfin-tizen6.wgt': {'browser_download_url': 'https://github.com/a/b/Jellyfin-tizen6.wgt', 'size': 2097152, 'digest': 'sha256:' + ('1' * 64)},
            'Jellyfin-tizen5.wgt': {'browser_download_url': 'https://github.com/a/b/Jellyfin-tizen5.wgt', 'size': 1048576, 'digest': 'sha256:' + ('2' * 64)},
            'Overscan.tpk': {'browser_download_url': 'https://github.com/a/b/Overscan.tpk', 'size': 524288, 'digest': 'sha256:' + ('3' * 64)},
            'AdultPortal.wgt': {'browser_download_url': 'https://github.com/a/b/AdultPortal.wgt', 'size': 102400, 'digest': 'sha256:' + ('4' * 64)},
        },
        release_meta={'tag_name': 'community-934', 'published_at': '2026-11-10T00:00:00Z'},
    )

    win = NeonWindow(api=api)
    win.show()
    qt_app.processEvents()

    def _wait_sam():
        deadline = __import__('time').time() + 3.0
        while win.sam_apps_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        win._pump()
        qt_app.processEvents()

    with mock.patch('samsung_catalog.SamsungCatalogManager.get_catalog', return_value=norm_catalog):
        win.switch_tab(6)
        _wait_sam()

    # Adult app hidden by default -> 2 visible groups (Jellyfin + Overscan)
    assert win.sam_apps_list.count() == 2
    assert win.sam_apps_adult_chk.isChecked() is False

    # Selected Jellyfin -> has 2 variants & prefilled saved server_url + password-masked secret field
    assert win.sam_apps_detail_title.text() == 'Jellyfin'
    assert win.sam_apps_variant_combo.count() == 2
    assert win._sam_setting_inputs['server_url'].text() == 'https://saved.nas:8096'
    assert win._sam_setting_inputs['api_secret_token'].echoMode() == QtWidgets.QLineEdit.Password

    # Switch variant to Tizen 5 -> user_settings box clears
    win.sam_apps_variant_combo.setCurrentIndex(1)
    qt_app.processEvents()
    assert win._sam_setting_inputs == {}

    # Toggle 18+ switch via user click -> AdultPortal appears and preference persists in settings
    from PyQt5 import QtCore, QtTest
    QtTest.QTest.mouseClick(win.sam_apps_adult_chk, QtCore.Qt.LeftButton)
    qt_app.processEvents()
    assert win.sam_apps_adult_chk.isChecked() is True
    assert win.sam_apps_list.count() == 3
    assert api.load_settings()['samsung_catalog_show_adult'] is True

    # Removed category disables Install button
    removed_idx = win.sam_apps_cat_combo.findData('__removed__')
    assert removed_idx >= 0
    win.sam_apps_cat_combo.setCurrentIndex(removed_idx)
    qt_app.processEvents()
    assert win.sam_apps_list.count() == 1
    assert win.btn_sam_apps_install.isEnabled() is False

    # Return to All Categories, search Overscan, sync TV IP two-way, Check TV Apps, and test version override confirm
    win.sam_apps_cat_combo.setCurrentIndex(0)
    qt_app.processEvents()
    win.sam_apps_search.setText('Overscan')
    qt_app.processEvents()
    assert win.sam_apps_list.count() == 1

    win.sam_apps_ip.setText('192.168.1.99')
    qt_app.processEvents()
    assert win.sam_ip.text() == '192.168.1.99'

    with mock.patch.object(api, 'check_samsung_tv_installed', return_value={'ok': True, 'package_ids': ['org.apps2samsung.overscan'], 'app_ids': ['org.apps2samsung.overscan.ui'], 'count': 1}):
        win.check_samsung_apps_tv()
        _wait_sam()
    assert 'org.apps2samsung.overscan' in win._sam_installed_pkgs

    calls = []

    def fake_install(**kwargs):
        calls.append(kwargs)
        if not kwargs.get('force_version_override'):
            return {'ok': False, 'needs_version_confirm': True, 'tv_version': '5.5', 'required_version': '6.0', 'error': 'Version too old'}
        return {'ok': True, 'pkg_id': 'org.apps2samsung.overscan', 'app_id': 'org.apps2samsung.overscan.ui', 'cert_level': 'partner'}

    win.sam_apps_search.setText('')
    qt_app.processEvents()
    win.sam_apps_list.setCurrentRow(1)
    qt_app.processEvents()
    with mock.patch.object(api, 'install_samsung_catalog_app', side_effect=fake_install), \
         mock.patch('app_legacy.ConfirmDialog.exec_', return_value=ConfirmDialog.Accepted):
        win.install_selected_samsung_catalog_app()
        _wait_sam()
        _wait_sam()
    assert len(calls) == 2
    assert calls[0]['force_version_override'] is False
    assert calls[1]['force_version_override'] is True

    win._force_exit = True
    win.close()
    qt_app.processEvents()

