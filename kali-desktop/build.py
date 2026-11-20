#!/usr/bin/env python3
"""Build both offline-installable player payloads; dependencies come from apt."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import alemtv_app

VERSION = '1.0.2'
DEPENDENCIES = ('python3, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, '
                'gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-libav')
BELET_FILES = ('index.html', 'style.css', 'api.js', 'app.js', 'hls.min.js', 'hls.LICENSE',
               'belet-logo.png', 'login-background.png', 'belet-regular.ttf', 'belet-bold.ttf')


def put(root, path, content, mode=0o644):
    dest = root / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
    dest.chmod(mode)


def build(app, title):
    shared = Path('usr/share') / app
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o755)  # dpkg also archives the staging root directory
        if app == 'beletfilm':
            for name in BELET_FILES:
                put(root, shared / name, (ROOT / 'belet_samsung_native' / name).read_bytes())
            # The shared client otherwise labels every non-webOS device Samsung.
            html_path = root / shared / 'index.html'
            html = html_path.read_text(encoding='utf-8')
            marker = '<script src="api.js"></script>'
            if html.count(marker) != 1:
                raise ValueError('Belet entry point changed')
            html_path.write_text(html.replace(marker, '<script src="desktop.js"></script>\n' + marker), encoding='utf-8')
            put(root, shared / 'desktop.js', "window.beletWebosIdentity = function () { return {model: 'Kali Linux PC', osVersion: 'Linux'}; };\n")
            origins = ['https://beletapis.com/*', 'https://*.beletapis.com/*', 'https://api.belet.tm/*', 'https://*.belet.me/*']
            icon = 'belet_large_icon.png'
        else:
            put(root, shared / 'index.html', alemtv_app.build_alemtv_html(desktop=True))
            put(root, shared / 'style.css', alemtv_app.build_alemtv_css())
            icon = 'alemtv_large_icon.png'
            put(root, shared / 'icon.png', (ROOT / 'assets' / icon).read_bytes())
            urls = [urlsplit(p['url']) for p in alemtv_app.ALEMTV_PORTALS]
            origins = sorted({f'{u.scheme}://{u.netloc}/*' for u in urls})
        put(root, shared / 'origins.txt', '\n'.join(origins) + '\n')
        put(root, f'usr/share/pixmaps/{app}.png', (ROOT / 'assets' / icon).read_bytes())
        put(root, f'usr/bin/{app}', (HERE / 'player.py').read_bytes(), 0o755)
        put(root, f'usr/share/applications/{app}.desktop', f'''[Desktop Entry]
Type=Application
Name={title}
Exec={app}
Icon={app}
Terminal=false
Categories=AudioVideo;Player;Video;
''')
        put(root, 'DEBIAN/control', f'''Package: {app}
Version: {VERSION}
Section: video
Priority: optional
Architecture: all
Maintainer: NEON Project <noreply@users.noreply.github.com>
Depends: {DEPENDENCIES}
Description: {title} desktop player for Kali Linux
 Standalone GTK/WebKit player with a persistent per-user profile.
''')
        output = HERE / 'packages' / f'{app}_{VERSION}_all.deb'
        output.parent.mkdir(exist_ok=True)
        subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(root), str(output)],
                       env={**os.environ, 'SOURCE_DATE_EPOCH': '1700000000'}, check=True)


if __name__ == '__main__':
    os.umask(0o022)
    build('beletfilm', 'BeletFilm')
    build('alemtv', 'Alem TV')
