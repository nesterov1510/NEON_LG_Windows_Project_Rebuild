"""Modern WebView2 desktop launcher."""
import os

import webview
from core import Api, resource


def webview_storage_path():
    """Persistent WebView profile dir (%APPDATA% on Windows).

    pywebview runs in private mode by default, which wipes cookies and
    localStorage on every exit. The Alem TV client stores the terminal MAC
    (alem_mac) and session id (alem_sid) in localStorage, so without a
    persistent profile every relaunch would ask for login/password again.
    """
    base = os.environ.get('APPDATA') or os.path.join(
        os.path.expanduser('~'), 'AppData', 'Roaming')
    return os.path.join(base, 'NEON_LG_Windows_Project')

if __name__ == '__main__':
    with open(resource('ui.html'), 'r', encoding='utf-8') as f:
        html = f.read()
    api = Api()
    file_dialog_enum = getattr(webview, 'FileDialog', None)
    api.open_dialog_type = getattr(file_dialog_enum, 'OPEN', None) or getattr(webview, 'OPEN_DIALOG', 10)
    window = webview.create_window(
        'NEON / LG webOS Region Console',
        html=html,
        js_api=api,
        width=1440,
        height=920,
        min_size=(940, 680),
        background_color='#060a08',
    )
    api.window = window
    storage_path = webview_storage_path()
    os.makedirs(storage_path, exist_ok=True)
    try:
        webview.start(debug=False, private_mode=False, storage_path=storage_path)
    finally:
        api.close()
