"""After apt install: xvfb-run -a /usr/bin/python3 smoke.py beletfilm (or alemtv)."""
import runpy
import sys

app, = sys.argv[1:]
launcher = runpy.run_path('/usr/bin/' + app)
Gtk, GLib = launcher['Gtk'], launcher['GLib']
window = launcher['player_window'](app)
view = window.get_child()
ready = False
checking = False
probe = {
    'alemtv': "typeof window.bootAlemTV === 'function' && !!localStorage.alem_portal",
    'beletfilm': "typeof Belet === 'object' && !!document.getElementById('toQr').onclick && !!Belet.device()",
}[app]


def received(view, result, _):
    global ready, checking
    checking = False
    ready = view.evaluate_javascript_finish(result).to_boolean()
    if ready:
        Gtk.main_quit()


def poll():
    global checking
    if not checking and not view.is_loading():
        checking = True
        view.evaluate_javascript(probe, -1, None, None, None, received, None)
    return not ready


GLib.timeout_add(200, poll)
GLib.timeout_add_seconds(30, Gtk.main_quit)
Gtk.main()
if not ready:
    sys.exit(f'{app}: packaged client did not initialize')
print(f'{app}: installed client initialized in WebKitGTK')
