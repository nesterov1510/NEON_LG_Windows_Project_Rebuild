"""Package and bundle the full-featured Belet TV client for Hisense / Toshiba VIDAA.

All Belet UI, API, and playback code comes verbatim from ``belet_samsung_native/``
(shared across Samsung Tizen, LG webOS, and Hisense VIDAA) with only a thin
ES5 VIDAA overlay (``belet_vidaa_client/vidaa-shim.js``) injected before ``hls.min.js``.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

APP_ID = 'tm.belet.vidaa'
APP_TITLE = 'Bëlet'

SHARED_FILES = (
    'index.html',
    'style.css',
    'api.js',
    'app.js',
    'hls.min.js',
    'hls.LICENSE',
    'belet-logo.png',
    'login-background.png',
    'belet-regular.ttf',
    'belet-bold.ttf',
)

TEXT_FILES = frozenset({'index.html', 'style.css', 'api.js', 'app.js', 'hls.min.js', 'hls.LICENSE'})

MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.ttf': 'font/ttf',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.txt': 'text/plain; charset=utf-8',
}


def _find_dir(rel_name: str) -> Path:
    candidates = [Path(__file__).resolve().parent / rel_name]
    meipass = getattr(sys, '_MEIPASS', '')
    if meipass:
        candidates.append(Path(meipass) / rel_name)
    exe_dir = Path(getattr(sys, 'executable', '') or '.').resolve().parent
    candidates.extend([exe_dir / rel_name, exe_dir / '_internal' / rel_name])
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


def _lf(data: bytes) -> bytes:
    return data.replace(b'\r\n', b'\n')


def _read_shared_bytes(rel_path: str) -> bytes:
    path = _find_dir('belet_samsung_native') / rel_path
    data = path.read_bytes()
    return _lf(data) if rel_path in TEXT_FILES else data


def _read_shim_bytes() -> bytes:
    path = _find_dir('belet_vidaa_client') / 'vidaa-shim.js'
    return _lf(path.read_bytes())


def _safe_inline_js(js_text: str) -> str:
    return re.sub(r'</(script)', r'<\/\1', js_text, flags=re.IGNORECASE)


def _proxy_bootstrap_tag(proxy_base: str) -> str:
    cleaned = str(proxy_base or '').strip().rstrip('/')
    if not cleaned:
        return ''
    return f'<script>window.__NEON_BELET_PROXY__ = {json.dumps(cleaned)};</script>\n'


def _index_html(proxy_base: str = '') -> str:
    html = _read_shared_bytes('index.html').decode('utf-8')
    injection = _proxy_bootstrap_tag(proxy_base) + '<script src="vidaa-shim.js"></script>\n'
    marker = '<script src="hls.min.js"></script>\n'
    if marker not in html:
        raise RuntimeError('belet_samsung_native/index.html is missing the hls.min.js script tag')
    return html.replace(marker, injection + marker, 1)


def package_files(proxy_base: str = '') -> dict[str, bytes]:
    """Return every static file needed to serve the Belet VIDAA web app."""
    files: dict[str, bytes] = {
        'index.html': _index_html(proxy_base=proxy_base).encode('utf-8'),
        'vidaa-shim.js': _read_shim_bytes(),
    }
    for name in SHARED_FILES:
        if name == 'index.html':
            continue
        files[name] = _read_shared_bytes(name)
    return files


def read_package_file(rel_path: str, proxy_base: str = '') -> tuple[bytes, str] | None:
    """Return ``(content_bytes, content_type)`` for ``rel_path``, or ``None`` if absent."""
    clean = str(rel_path or '').lstrip('/')
    if not clean:
        clean = 'index.html'
    if clean in ('..',) or '/' in clean or '\\' in clean:
        return None
    if clean == 'index.html':
        return _index_html(proxy_base=proxy_base).encode('utf-8'), MIME_TYPES['.html']
    if clean == 'vidaa-shim.js':
        return _read_shim_bytes(), MIME_TYPES['.js']
    if clean not in SHARED_FILES:
        return None
    ext = Path(clean).suffix.lower()
    return _read_shared_bytes(clean), MIME_TYPES.get(ext, 'application/octet-stream')


def _data_url(data: bytes, mime: str) -> str:
    return f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')


def build_standalone_html(proxy_base: str = '', include_fonts: bool = True) -> str:
    """Build a single self-contained HTML5 document for TV Flash or offline Data URI.

    All shared UI/API/HLS scripts, stylesheet rules, logo, and login backdrop are
    inlined from ``belet_samsung_native/`` without touching the source directory.
    """
    html = _read_shared_bytes('index.html').decode('utf-8')
    css = _read_shared_bytes('style.css').decode('utf-8')
    shim_js = _safe_inline_js(_read_shim_bytes().decode('utf-8'))
    hls_js = _safe_inline_js(_read_shared_bytes('hls.min.js').decode('utf-8'))
    api_js = _safe_inline_js(_read_shared_bytes('api.js').decode('utf-8'))
    app_js = _safe_inline_js(_read_shared_bytes('app.js').decode('utf-8'))

    logo_data = _data_url(_read_shared_bytes('belet-logo.png'), 'image/png')
    bg_data = _data_url(_read_shared_bytes('login-background.png'), 'image/png')

    if include_fonts:
        reg_data = _data_url(_read_shared_bytes('belet-regular.ttf'), 'font/ttf')
        bold_data = _data_url(_read_shared_bytes('belet-bold.ttf'), 'font/ttf')
        css = css.replace("url('belet-regular.ttf')", f"url('{reg_data}')")
        css = css.replace("url('belet-bold.ttf')", f"url('{bold_data}')")
    else:
        css = re.sub(r"@font-face\s*\{[^}]*belet-(?:regular|bold)\.ttf[^}]*\}\s*", '', css)

    css = css.replace("url('login-background.png')", f"url('{bg_data}')")

    app_js = app_js.replace("var BRAND_IMAGE = 'belet-logo.png';", f"var BRAND_IMAGE = {json.dumps(logo_data)};")
    html = html.replace('src="belet-logo.png"', f'src="{logo_data}"')

    css_link = '<link rel="stylesheet" href="style.css">'
    if css_link not in html:
        raise RuntimeError('belet_samsung_native/index.html is missing style.css link')
    html = html.replace(css_link, f'<style>\n{css}\n</style>', 1)

    scripts_block = (
        '<script src="hls.min.js"></script>\n'
        '<script src="api.js"></script>\n'
        '<script src="app.js"></script>'
    )
    if scripts_block not in html:
        raise RuntimeError('belet_samsung_native/index.html is missing expected script tags')

    inline_scripts = (
        _proxy_bootstrap_tag(proxy_base)
        + f'<script>\n{shim_js}\n</script>\n'
        + f'<script>\n{hls_js}\n</script>\n'
        + f'<script>\n{api_js}\n</script>\n'
        + f'<script>\n{app_js}\n</script>'
    )
    return html.replace(scripts_block, inline_scripts, 1)


def build_vidaa_data_url(proxy_base: str = '', include_fonts: bool = False) -> str:
    """Build a self-contained data:text/html;base64,... URL for the full Belet client."""
    raw = build_standalone_html(proxy_base=proxy_base, include_fonts=include_fonts).encode('utf-8')
    return 'data:text/html;charset=utf-8;base64,' + base64.b64encode(raw).decode('ascii')
