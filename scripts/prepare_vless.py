"""Build-time only: stage the pinned upstream Windows x64 engine for PyInstaller."""
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
import urllib.request
import zipfile

VERSION = '1.14.4'
NAME = 'sing-box-' + VERSION + '-windows-amd64-legacy-windows-7'
URL = 'https://github.com/SagerNet/sing-box/releases/download/v' + VERSION + '/' + NAME + '.zip'
SHA256 = 'd7d4dc70328eb60b5dd90125bab31b0913c0370ee730e55acd5034a7e1bd19af'
DEST = Path(__file__).resolve().parents[1] / 'assets/vless/sing-box.exe'

def stage():
    with urllib.request.urlopen(URL, timeout=120) as response:
        archive = response.read(40 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise ValueError('VLESS engine archive checksum mismatch; refusing to bundle.')
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        member, = [n for n in z.namelist() if n.rsplit('/', 1)[-1] == 'sing-box.exe']
        DEST.write_bytes(z.read(member))
    print('Staged verified Windows VLESS engine:', DEST)


def check():
    # Exercise the same stdin protocol as the application, without needing a
    # server credential, changing routes, or exposing a real profile to CI.
    sys.path.insert(0, str(DEST.parents[2]))
    from vless import configuration
    config = configuration('vless://00000000-0000-4000-8000-000000000001@example.org:443?security=tls')
    result = subprocess.run([str(DEST), 'check', '-c', 'stdin'], input=json.dumps(config),
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('VLESS config check: ' + (result.stderr or result.stdout).strip())


if __name__ == '__main__':
    try:
        stage()
        if '--check' in sys.argv:
            check()
    except Exception as exc:
        raise SystemExit('::error::Cannot bundle VLESS: ' + str(exc))
