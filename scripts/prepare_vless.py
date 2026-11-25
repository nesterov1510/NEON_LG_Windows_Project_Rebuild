"""Build-time only: stage pinned upstream Windows x64 sing-box + Xray-core engines for PyInstaller."""
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
import urllib.request
import zipfile

SINGBOX_VERSION = '1.14.4'
SINGBOX_NAME = 'sing-box-' + SINGBOX_VERSION + '-windows-amd64-legacy-windows-7'
SINGBOX_URL = 'https://github.com/SagerNet/sing-box/releases/download/v' + SINGBOX_VERSION + '/' + SINGBOX_NAME + '.zip'
SINGBOX_SHA256 = 'd7d4dc70328eb60b5dd90125bab31b0913c0370ee730e55acd5034a7e1bd19af'

XRAY_VERSION = 'v26.11.9'
XRAY_URL = 'https://github.com/XTLS/Xray-core/releases/download/' + XRAY_VERSION + '/Xray-windows-64.zip'
XRAY_SHA256 = '1262522266337b824f07beb8e1375786b437683b3890105469088f8e17e0a69d'

DEST_DIR = Path(__file__).resolve().parents[1] / 'assets/vless'
DEST = DEST_DIR / 'sing-box.exe'
XRAY_DEST = DEST_DIR / 'xray.exe'


def _download_verified(url, expected_sha256, max_bytes=60 * 1024 * 1024):
    with urllib.request.urlopen(url, timeout=180) as response:
        archive = response.read(max_bytes)
    actual = hashlib.sha256(archive).hexdigest().lower()
    if actual != expected_sha256.lower():
        raise ValueError(f'Engine archive checksum mismatch for {url}: got {actual}, expected {expected_sha256}.')
    return archive


def stage():
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    sb_archive = _download_verified(SINGBOX_URL, SINGBOX_SHA256)
    with zipfile.ZipFile(io.BytesIO(sb_archive)) as z:
        member, = [n for n in z.namelist() if n.rsplit('/', 1)[-1] == 'sing-box.exe']
        DEST.write_bytes(z.read(member))
    print('Staged verified Windows sing-box engine:', DEST)

    xr_archive = _download_verified(XRAY_URL, XRAY_SHA256)
    with zipfile.ZipFile(io.BytesIO(xr_archive)) as z:
        names = {n.rsplit('/', 1)[-1]: n for n in z.namelist() if not n.endswith('/')}
        if 'xray.exe' not in names:
            raise ValueError('xray.exe not found inside Xray-windows-64.zip')
        for fname in ('xray.exe', 'geoip.dat', 'geosite.dat'):
            if fname in names:
                (DEST_DIR / fname).write_bytes(z.read(names[fname]))
    print('Staged verified Windows Xray-core engine:', XRAY_DEST)


def _format_proc_error(result):
    parts = []
    for stream in (result.stderr, result.stdout):
        for ln in (stream or '').splitlines():
            s = ln.strip()
            if s and s not in parts:
                parts.append(s)
    return ' | '.join(parts) or f'exit {result.returncode}'


def _run_singbox_check(config):
    result = subprocess.run(
        [str(DEST), 'check', '-c', 'stdin'],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError('sing-box config check: ' + _format_proc_error(result))


def _run_xray_check(config):
    result = subprocess.run(
        [str(XRAY_DEST), 'run', '-test', '-format', 'json', '-c', 'stdin:'],
        cwd=str(DEST_DIR),
        input=json.dumps(config),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError('Xray config check: ' + _format_proc_error(result))


def check():
    sys.path.insert(0, str(DEST_DIR.parents[1]))
    from vless import configuration
    import happ_client

    _run_singbox_check(configuration('vless://00000000-0000-4000-8000-000000000001@example.org:443?security=tls'))

    sample_xhttp = happ_client.parse_proxy_uri(
        'vless://00000000-0000-4000-8000-000000000001@198.51.100.10:443'
        '?security=reality&sni=example.org&pbk=AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA&sid=ab&type=xhttp&path=%2Fxhttp#TestXHTTP'
    )
    _run_xray_check(happ_client.build_xray_config(
        sample_xhttp,
        settings={'fragment_enabled': True, 'routing_mode': 'bypass_lan_ru'},
        asset_dir=str(DEST_DIR),
    ))
    _run_singbox_check(happ_client.build_singbox_config(
        mode='mixed',
        upstream_socks_port=10808,
        extra_excludes=['198.51.100.10/32'],
    ))

    sample_hy2 = happ_client.parse_proxy_uri(
        'hy2://secret@198.51.100.20:443?sni=example.org&obfs=salamander&obfs-password=obf#Hy2'
    )
    _run_singbox_check(happ_client.build_singbox_config(
        sample_hy2,
        mode='tun',
        settings={'routing_mode': 'bypass_lan_ru'},
    ))
    print('Verified sing-box + Xray-core configs successfully.')


if __name__ == '__main__':
    try:
        stage()
        if '--check' in sys.argv:
            check()
    except Exception as exc:
        raise SystemExit('::error::Cannot bundle proxy engines: ' + str(exc))
