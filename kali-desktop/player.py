#!/usr/bin/python3
"""Desktop host for a bundled player. Installed as /usr/bin/{beletfilm,alemtv}."""
import sys
from pathlib import Path

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gdk, GLib, Gtk, WebKit2


def player_window(app):
    title = {'beletfilm': 'BeletFilm', 'alemtv': 'Alem TV'}[app]
    base = Path('/usr/share') / app
    GLib.set_prgname(app)
    GLib.set_application_name(title)
    window = Gtk.Window(title=title)
    window.set_default_size(1152, 648)
    window.set_icon_name(app)
    window.connect('destroy', Gtk.main_quit)
    data = WebKit2.WebsiteDataManager(base_data_directory=str(Path(GLib.get_user_data_dir()) / app),
                                      base_cache_directory=str(Path(GLib.get_user_cache_dir()) / app))
    context = WebKit2.WebContext.new_with_website_data_manager(data)
    view = WebKit2.WebView.new_with_context(context)
    view.get_settings().set_media_playback_requires_user_gesture(False)
    # Only the player API/CDN endpoints need cross-origin access, not all websites.
    view.set_cors_allowlist((base / 'origins.txt').read_text().splitlines())
    view.connect('close', lambda *_: window.destroy())
    # Fit the 1920x1080 TV layouts on a laptop and when resizing the window.
    view.connect('size-allocate', lambda w, a: w.set_zoom_level(max(0.1, min(a.width / 1920, a.height / 1080))))

    def key_press(_, event):
        if event.keyval == Gdk.KEY_F11:
            if window.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
                window.unfullscreen()
            else:
                window.fullscreen()
            return True
        return False

    window.connect('key-press-event', key_press)
    window.add(view)
    view.load_uri((base / 'index.html').as_uri())
    window.show_all()
    view.grab_focus()
    return window


if __name__ == '__main__':
    main_window = player_window(Path(sys.argv[0]).name)
    Gtk.main()
