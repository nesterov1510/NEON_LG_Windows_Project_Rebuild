import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from local_tv_packages import validate_wgt
from samsung_tizen import BELET_PKG_ID
from scripts.build_belet_samsung import FILES, SRC, build


def test_package_is_isolated_and_contains_only_local_app(tmp_path):
    output = build(tmp_path / 'client.wgt')
    data = output.read_bytes()
    assert validate_wgt(data) == ('BeletFilm1', 'BeletFilm1.BeletClient')
    assert BELET_PKG_ID != 'BeletFilm1'
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == set(FILES)
        for name in FILES:
            assert archive.read(name) == (SRC / name).read_bytes()
        html = archive.read('index.html').decode('utf-8')
        config = archive.read('config.xml').decode('utf-8')
        assert '<iframe' not in html.lower()
        assert 'service/proxy.js' not in config
        assert 'экспериментальная версия' in html


def test_mock_login_catalog_playback_and_logout():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not available')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / 'tests/belet_samsung_client_mock.js'), str(SRC / 'app.js')],
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
