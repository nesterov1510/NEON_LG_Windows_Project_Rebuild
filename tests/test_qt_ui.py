import os
from unittest import mock
import pytest

QtWidgets = pytest.importorskip('PyQt5.QtWidgets', exc_type=ImportError)
QApplication = QtWidgets.QApplication

from app_legacy import NeonWindow, _setup_windows_environment  # noqa: E402
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

    assert win.stack.count() == 5
    assert win.current_tab == 0
    assert not win.windowIcon().isNull()

    # Tab switching across LG, Samsung, Conf, Diagnostics and contacts
    win.switch_tab(1)
    assert win.current_tab == 1
    win.switch_tab(2)
    assert win.current_tab == 2
    win.switch_tab(3)
    assert win.current_tab == 3
    win.switch_tab(4)
    assert win.page_title.text() == 'MSB CALLBACK'
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

    win.switch_tab(2)
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

    # Log writes to LG, Samsung, and Conf terminals
    win.log('[+] Test synced log line')
    assert '[+] Test synced log line' in win.tv_term.edit.toPlainText()
    assert '[+] Test synced log line' in win.sam_term.edit.toPlainText()
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
    assert win.btn_sam_belet_launch is not None
    assert win.btn_sam_belet_remove is not None
    assert len(win.sam_host_ip_lbl.text()) > 5

    with mock.patch.object(api, 'install_samsung_belet', return_value={
        'ok': True,
        'duid': 'DUID_UI_1',
        'app_id': 'BeletTV001.Belet',
        'output': '[+] Belet (BeletTV001.Belet) installed successfully!',
    }) as m_sam_inst:
        win.install_samsung_belet()
        deadline = __import__('time').time() + 2.0
        while win.sam_busy and __import__('time').time() < deadline:
            win._pump()
            qt_app.processEvents()
        m_sam_inst.assert_called_once()
        assert 'BeletTV001.Belet' in win.sam_term.edit.toPlainText()

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

    # Ethernet Conf sharing + Custom DNS controls on Tab 3
    win.switch_tab(2)
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
    api.ethernet_active = True
    win._sync_status_badges()
    assert 'ETHERNET' in win.vpn_stat_val.text()
    assert 'ETHERNET' in win.kpi_vpn.val.text()
    api.ethernet_active = False
    win.vpn_state = 'DISCONNECTED'
    win._sync_status_badges()

    # Check ui.html includes Samsung System Setup, Belet, Ethernet Conf sharing controls, and Alem TV portal selectors
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ui.html'), 'r', encoding='utf-8') as hf:
        ui_html = hf.read()
    for token in (
        "openSysTune('samsung')", 'checkSamSdb()', 'authSamCert()', 'installSamBelet()', 'launchSamBelet()', 'removeSamBelet()',
        'ethAdapter', 'startEthernetShare()', 'stopEthernetShare()',
        'dnsPreset', 'dnsPrimary', 'dnsSecondary', 'dnsApplyUplink', 'applySharingDns()', 'resetSharingDns()',
        'alemPortal', 'samAlemPortal', 'installAlem()', 'installSamAlem()',
    ):
        assert token in ui_html

    # Check Alem TV Card portal selectors on webOS (Tab 0) and Samsung Tizen (Tab 1)
    assert win.alem_portal_combo is not None
    assert win.alem_portal_combo.count() >= 7
    assert win.btn_alem_install is not None
    assert win.sam_alem_portal_combo is not None
    assert win.sam_alem_portal_combo.count() >= 7
    assert win.btn_sam_alem_install is not None

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
        ('УСТАНОВИТЬ НА LG', 'PrimaryBtn'), ('УСТАНОВИТЬ НА SAMSUNG', 'PrimaryBtn'),
    ]:
        assert any(button.objectName() == style for button in by_text(text))
        assert 'QPushButton#' + style in win.styleSheet()
    win.close()
