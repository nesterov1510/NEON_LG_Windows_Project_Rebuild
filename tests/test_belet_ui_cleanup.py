from pathlib import Path

from core import Api
from samsung_tizen import BELET_APP_ID, BELET_PKG_ID


def test_only_normal_belet_buttons_remain_in_interfaces():
    root = Path(__file__).resolve().parents[1]
    html = (root / 'ui.html').read_text(encoding='utf-8')
    legacy = (root / 'app_legacy.py').read_text(encoding='utf-8')
    samsung_html = (root / 'belet_samsung_native/index.html').read_text(encoding='utf-8')
    for text in (html, legacy):
        for removed in ('BELET DIRECT TEST', 'BELET BROWSER TEST',
                        'УСТАНОВИТЬ BELET TEST', 'ДИАГНОСТИКА DIRECT TEST'):
            assert removed not in text
    assert 'onclick="installBelet()"' in html
    assert 'onclick="installSamBelet()"' not in html
    assert 'samBeletBridgeBtn' not in html
    assert 'toggleSamBeletBridge' not in html
    assert 'onclick="installSamBeletClient()"' in html
    assert 'onclick="installSamBeletDirectClient()"' in html
    assert 'onclick="launchSamBeletClient()"' in html
    assert 'onclick="removeSamBeletClient()"' in html
    assert not (root / 'samsung-belet/BeletTV001-unsigned.wgt').exists()
    assert 'btn_sam_belet_bridge' not in legacy
    assert 'toggle_samsung_belet_bridge' not in legacy
    assert 'homePcBridge' not in samsung_html and 'aboutPcBridge' not in samsung_html
    assert 'self.btn_belet_install.clicked.connect(self.install_belet)' in legacy
    assert 'self.btn_sam_belet_install.clicked.connect(self.install_samsung_belet_client)' in legacy
    assert 'self.btn_sam_belet_direct.clicked.connect(self.install_samsung_belet_direct_client)' in legacy
    assert 'self.btn_sam_belet_launch.clicked.connect(self.launch_samsung_belet_client)' in legacy
    assert 'self.btn_sam_belet_remove.clicked.connect(self.remove_samsung_belet_client)' in legacy
    assert BELET_PKG_ID == 'BeletTV001' and BELET_APP_ID == 'BeletTV001.Belet'
    for method in ('install_samsung_belet_direct', 'install_samsung_belet_browser', 'debug_samsung_belet_direct',
                   'start_belet_tv_bridge', 'stop_belet_tv_bridge', 'get_belet_tv_bridge_status'):
        assert not hasattr(Api, method)
