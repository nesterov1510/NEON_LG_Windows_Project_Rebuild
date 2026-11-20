#!/usr/bin/python3
"""Desktop host for a bundled player. Installed as /usr/bin/{beletfilm,alemtv}."""
import sys
from pathlib import Path

import gi

# GTK 3 and GTK 4 can coexist on Kali. Do not let PyGI select Gdk 4.
gi.require_version('Gdk', '3.0')
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
    home = (base / 'index.html').as_uri()
    if app == 'beletfilm':
        # Keep the desktop website's normal origin, cookies and CORS policy;
        # never forward tokens or pairing codes from the bundled TV client.
        stack = Gtk.Stack()
        stack.add_named(view, 'client')
        window.add(stack)
        header = Gtk.HeaderBar(title=title, show_close_button=True)
        switch = Gtk.ToggleButton(label='Веб-версия belet.tm')
        header.pack_end(switch)
        window.set_titlebar(header)

        def switch_mode(button):
            if button.get_active():
                view.load_uri('about:blank')  # stop any outstanding QR polling
                web = WebKit2.WebView.new_with_context(context)
                web.get_settings().set_media_playback_requires_user_gesture(False)
                stack.add_named(web, 'website')
                web.show()
                stack.set_visible_child(web)
                web.load_uri('https://belet.tm/')
                web.grab_focus()
            else:
                stack.get_child_by_name('website').destroy()
                stack.set_visible_child(view)
                view.load_uri(home)
                view.grab_focus()

        # Website login may use persistent cookies rather than localStorage.
        context.get_cookie_manager().set_persistent_storage(
            str(Path(data.get_base_data_directory()) / 'web-cookies.sqlite'),
            WebKit2.CookiePersistentStorage.SQLITE)
        switch.connect('toggled', switch_mode)
    else:
        window.add(view)
    view.load_uri(home)
    window.show_all()
    view.grab_focus()
    return window


if __name__ == '__main__':
    main_window = player_window(Path(sys.argv[0]).name)
    Gtk.main()
