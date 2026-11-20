"""Package the shared Belet TV client as an LG webOS IPK.

The application itself lives in ``belet_samsung_native/`` and is shared with the
Samsung WGT build; this module only adds the webOS manifest, the webOS overlay
script from ``belet_lg_client/`` and the webOS icons, then wraps everything into
the ar/tar structure webOS' appInstallService expects.
"""
import io
import json
import re
import tarfile
import gzip
import os
import time
from pathlib import Path

APP_ID = 'tm.belet.neonclient'
APP_TITLE = 'Bëlet'
APP_VENDOR = 'NEON'
PACKAGE_NAME = 'BeletLgClient-webOS-unsigned.ipk'

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'belet_samsung_native'
OVERLAY = ROOT / 'belet_lg_client'
ASSETS = ROOT / 'assets'

# Shared client sources (config.xml is Tizen-only and stays out of the IPK).
SHARED_FILES = ('index.html', 'style.css', 'api.js', 'app.js', 'hls.min.js', 'hls.LICENSE',
                'belet-logo.png', 'login-background.png', 'belet-regular.ttf', 'belet-bold.ttf')
OVERLAY_FILES = ('webos-shim.js',)
# The IPK must be byte-identical no matter how git checked the sources out, so the
# text files are normalised to LF while packaging (Windows checkouts are CRLF).
TEXT_FILES = ('index.html', 'style.css', 'api.js', 'app.js', 'hls.min.js', 'hls.LICENSE', 'webos-shim.js')
# Ship shared client files verbatim; only index.html needs the webOS shim tag.
SHIM_TAG = '<script src="webos-shim.js"></script>\n'


# webOS only accepts x.y.z for appinfo.json and needs a higher value to upgrade
# an already installed package, so the IPK carries its own revision while the
# Samsung widget keeps the version from the Tizen manifest.
WEBOS_REVISION = 5


def manifest_version():
    """Version of the shared Tizen client (the Samsung WGT ships exactly this)."""
    manifest = _read_text_file(SRC / 'config.xml').decode('utf-8')
    match = re.search(r'<widget[^>]*\bversion="([^"]+)"', manifest)
    if not match:
        raise ValueError('belet_samsung_native/config.xml has no widget version')
    return match.group(1)


def app_version():
    """webOS package version: manifest version with the packaging revision bumped."""
    base = manifest_version()
    parts = base.split('.')
    while len(parts) < 3:
        parts.append('0')
    return '%s.%s.%s' % (parts[0], parts[1], int(parts[2]) + WEBOS_REVISION)


def app_info(version=None):
    return {
        'id': APP_ID,
        'version': version or app_version(),
        'vendor': APP_VENDOR,
        'type': 'web',
        'main': 'index.html',
        'title': APP_TITLE,
        'icon': 'icon.png',
        'largeIcon': 'largeIcon.png',
        'splashBackground': 'splash.png',
        'bgColor': '#0d1112',
        'iconColor': '#0d1112',
        'resolution': '1920x1080',
        'disableBackHistoryAPI': True,
        'handlesRelaunch': False,
        'disallowScrollingInMainFrame': False,
        'useNativeScroll': True,
        'transparent': False,
        'uiRevision': 2,
    }


def _read_text_file(path):
    return path.read_bytes().replace(b'\r\n', b'\n')


def _index_html():
    html = _read_text_file(SRC / 'index.html').decode('utf-8')
    marker = '<script src="hls.min.js"></script>\n'
    if marker not in html:
        raise ValueError('index.html no longer loads hls.min.js; update the webOS overlay injection')
    if SHIM_TAG in html:
        return html.encode('utf-8')
    return html.replace(marker, SHIM_TAG + marker, 1).encode('utf-8')


def package_files(version=None):
    """Ordered (name, bytes) list placed at the root of the webOS application."""
    files = [('appinfo.json', json.dumps(app_info(version), indent=2, ensure_ascii=False).encode('utf-8') + b'\n')]
    for name in SHARED_FILES:
        data = _read_text_file(SRC / name) if name in TEXT_FILES else (SRC / name).read_bytes()
        if name == 'index.html':
            data = _index_html()
        files.append((name, data))
    for name in OVERLAY_FILES:
        files.append((name, _read_text_file(OVERLAY / name)))
    files.append(('icon.png', (ASSETS / 'belet_icon.png').read_bytes()))
    files.append(('largeIcon.png', (ASSETS / 'belet_large_icon.png').read_bytes()))
    files.append(('splash.png', (ASSETS / 'belet_splash.png').read_bytes()))
    return files


def _ar_member(name, data, mtime):
    header = (
        f'{name:<16}'
        f'{int(mtime):<12}'
        f'{"0":<6}'
        f'{"0":<6}'
        f'{"100644":<8}'
        f'{len(data):<10}'
        '`\n'
    ).encode('ascii')
    out = header + data
    if len(data) % 2:
        out += b'\n'
    return out


class _UstarEntry(tarfile.TarInfo):
    def tobuf(self, format=tarfile.USTAR_FORMAT, encoding='utf-8', errors='strict'):
        header = bytearray(super().tobuf(format, encoding, errors))
        # Python 3.8 writes ASCII octal zero in devmajor/devminor even for
        # regular files and directories; newer versions leave the fields empty.
        # These are single-block USTAR headers, never device nodes or PAX entries.
        header[329:345] = bytes(16)
        header[148:156] = b' ' * 8
        header[148:156] = ('%06o\0 ' % sum(header)).encode('ascii')
        return bytes(header)


def _gzipped_tar(mtime):
    """(tarfile, buffer) writing a deterministic gzip stream: the header mtime is fixed."""
    buf = io.BytesIO()
    gz = gzip.GzipFile(fileobj=buf, mode='wb', mtime=mtime)
    tar = tarfile.open(fileobj=gz, mode='w', format=tarfile.USTAR_FORMAT)
    return tar, gz, buf


def _tarball(members, mtime):
    tar, gz, buf = _gzipped_tar(mtime)
    with tar:
        for name, payload in members:
            info = _UstarEntry(name)
            info.type = tarfile.REGTYPE
            info.mode = 0o644
            info.size = len(payload)
            info.uid = 0
            info.gid = 0
            info.uname = 'root'
            info.gname = 'root'
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(payload))
    gz.close()
    return buf.getvalue()


def _directories(base):
    parts = base.split('/')
    return ['/'.join(parts[:index + 1]) for index in range(len(parts))]


# Fixed timestamp keeps the IPK byte-reproducible; honour SOURCE_DATE_EPOCH if set.
DEFAULT_MTIME = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))


def build_ipk(mtime=None, version=None):
    """Return the IPK bytes for the LG webOS Belet client."""
    mtime = int(DEFAULT_MTIME if mtime is None else mtime)
    files = package_files(version)
    base = f'usr/palm/applications/{APP_ID}'

    service = APP_ID + '.qr'
    service_root = f'usr/palm/services/{service}'
    package_root = f'usr/palm/packages/{APP_ID}'
    metadata = {'id': APP_ID, 'app': APP_ID, 'version': version or app_version(), 'services': [service]}
    payloads = [(f'{base}/{name}', payload) for name, payload in files]
    for name in ('package.json', 'services.json', 'qr.js'):
        payloads.append((f'{service_root}/{name}', _read_text_file(OVERLAY / 'qr-service' / name)))
    payloads.append((f'{package_root}/packageinfo.json', json.dumps(metadata).encode('utf-8')))
    dirs = dict.fromkeys(d for root in (base, service_root, package_root) for d in _directories(root))
    data_members = [(d + '/', b'') for d in dirs] + payloads

    tar, gz, data_buf = _gzipped_tar(mtime)
    with tar:
        for name, payload in data_members:
            info = _UstarEntry(name)
            info.uid = 0
            info.gid = 0
            info.uname = 'root'
            info.gname = 'root'
            info.mtime = mtime
            if name.endswith('/'):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.size = 0
                tar.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.mode = 0o644
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    gz.close()

    installed_kb = max(1, (sum(len(payload) for _, payload in payloads) + 1023) // 1024)
    control = (
        f'Package: {APP_ID}\n'
        f'Version: {app_info(version)["version"]}\n'
        'Section: misc\n'
        'Priority: optional\n'
        'Architecture: all\n'
        f'Installed-Size: {installed_kb}\n'
        'Maintainer: NEON Belet <help@belet.tm>\n'
        'Description: Belet TV client for LG webOS\n'
        'webOS-Package-Format-Version: 2\n'
        'webOS-Packager-Version: 2.0.0\n'
    ).encode('utf-8')
    control_tar = _tarball([('control', control)], mtime)

    ipk = io.BytesIO()
    ipk.write(b'!<arch>\n')
    ipk.write(_ar_member('debian-binary', b'2.0\n', mtime))
    ipk.write(_ar_member('control.tar.gz', control_tar, mtime))
    ipk.write(_ar_member('data.tar.gz', data_buf.getvalue(), mtime))
    return ipk.getvalue()
