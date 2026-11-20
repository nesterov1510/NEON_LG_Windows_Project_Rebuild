"""Build-time only: stage the pinned upstream Windows x64 engine for PyInstaller."""
import hashlib
import io
from pathlib import Path
import urllib.request
import zipfile

VERSION = '1.14.4'
NAME = 'sing-box-' + VERSION + '-windows-amd64-legacy-windows-7'
URL = 'https://github.com/SagerNet/sing-box/releases/download/v' + VERSION + '/' + NAME + '.zip'
SHA256 = 'd7d4dc70328eb60b5dd90125bab31b0913c0370ee730e55acd5034a7e1bd19af'
DEST = Path(__file__).resolve().parents[1] / 'assets/vless/sing-box.exe'

if __name__ == '__main__':
    with urllib.request.urlopen(URL, timeout=120) as response:
        archive = response.read(40 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise SystemExit('VLESS engine archive checksum mismatch; refusing to bundle.')
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        member, = [n for n in z.namelist() if n.rsplit('/', 1)[-1] == 'sing-box.exe']
        DEST.write_bytes(z.read(member))
    print('Staged verified Windows VLESS engine:', DEST)
