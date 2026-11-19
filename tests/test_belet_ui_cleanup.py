from pathlib import Path

from core import Api
from samsung_tizen import BELET_APP_ID, BELET_PKG_ID


def test_only_normal_belet_buttons_remain_in_interfaces():
    root = Path(__file__).resolve().parents[1]
    html = (root / 'ui.html').read_text(encoding='utf-8')
    legacy = (root / 'app_legacy.py').read_text(encoding='utf-8')
    for text in (html, legacy):
        for removed in ('BELET DIRECT TEST', 'BELET BROWSER TEST',
                        'УСТАНОВИТЬ BELET TEST', 'ДИАГНОСТИКА DIRECT TEST'):
            assert removed not in text
    assert 'onclick="installBelet()"' in html
    assert 'onclick="installSamBelet()"' in html
    assert 'self.btn_belet_install.clicked.connect(self.install_belet)' in legacy
    assert 'self.btn_sam_belet_install.clicked.connect(self.install_samsung_belet)' in legacy
    assert BELET_PKG_ID == 'BeletTV001' and BELET_APP_ID == 'BeletTV001.Belet'
    for method in ('install_samsung_belet_direct', 'install_samsung_belet_browser', 'debug_samsung_belet_direct'):
        assert not hasattr(Api, method)
