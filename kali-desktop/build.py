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

VERSION = '1.0.3'
DEPENDENCIES = (
    'python3, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, '
    'gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, '
    'gstreamer1.0-plugins-ugly, gstreamer1.0-libav, gstreamer1.0-gl'
)
BELET_FILES = ('index.html', 'style.css', 'api.js', 'app.js', 'hls.min.js', 'hls.LICENSE',
               'belet-logo.png', 'login-background.png', 'belet-regular.ttf', 'belet-bold.ttf')

XHR_PROXY_JS = r"""(function () {
  var proxyBase = typeof window.__NEON_PROXY_BASE__ === 'string' ? window.__NEON_PROXY_BASE__ : '';
  if (!proxyBase || typeof window.XMLHttpRequest !== 'function') return;

  function toProxyUrl(url) {
    if (typeof url !== 'string') return url;
    if (url.indexOf(proxyBase + '/') === 0) return url;
    var match = /^(https?):\/\/(.+)$/i.exec(url);
    if (!match) return url;
    return proxyBase + '/' + match[1].toLowerCase() + '/' + match[2];
  }

  window.__neonToProxyUrl = toProxyUrl;

  var proto = window.XMLHttpRequest.prototype;
  var origOpen = proto.open;
  proto.open = function (method, url) {
    var args = Array.prototype.slice.call(arguments);
    var rawUrl = typeof url === 'string' ? url : String(url || '');
    var proxied = toProxyUrl(rawUrl);
    if (proxied !== rawUrl) {
      args[1] = proxied;
      this.__neonOrigUrl = rawUrl;
      try {
        Object.defineProperty(this, 'responseURL', {
          configurable: true,
          get: function () {
            if (this.readyState >= 2) {
              try {
                var finalUrl = this.getResponseHeader('X-Neon-Final-URL');
                if (finalUrl) return finalUrl;
              } catch (e) {}
            }
            return this.__neonOrigUrl || '';
          }
        });
      } catch (e) {}
    } else if (this.__neonOrigUrl) {
      delete this.__neonOrigUrl;
      try { delete this.responseURL; } catch (e) {}
    }
    return origOpen.apply(this, args);
  };
}());
"""

BELET_DESKTOP_JS = XHR_PROXY_JS + r"""
window.beletWebosIdentity = function () { return {model: 'Kali Linux PC', osVersion: 'Linux'}; };

(function () {
  var toProxyUrl = window.__neonToProxyUrl;
  var video = document.getElementById('video');
  var proto = window.HTMLMediaElement && window.HTMLMediaElement.prototype;
  var srcDesc = proto && Object.getOwnPropertyDescriptor(proto, 'src');
  if (!toProxyUrl || !video || !srcDesc || !srcDesc.set || !srcDesc.get) return;
  var origUrl = '';
  Object.defineProperty(video, 'src', {
    configurable: true,
    get: function () {
      var real = srcDesc.get.call(video);
      if (!real) { origUrl = ''; return ''; }
      if (real.indexOf('blob:') === 0) return real;
      return origUrl || real;
    },
    set: function (value) {
      var raw = (value === null || value === undefined) ? '' : String(value);
      if (!raw || raw.indexOf('blob:') === 0 || raw.indexOf('data:') === 0) {
        origUrl = '';
        return srcDesc.set.call(video, raw);
      }
      origUrl = raw;
      return srcDesc.set.call(video, toProxyUrl(raw));
    }
  });
}());
"""

ALEMTV_DESKTOP_JS = XHR_PROXY_JS + r"""
(function () {
  var video = document.getElementById('videoElement');
  var proto = window.HTMLMediaElement && window.HTMLMediaElement.prototype;
  if (!video || !proto) return;

  var srcDesc = Object.getOwnPropertyDescriptor(proto, 'src');
  var nativeLoad = proto.load;
  var nativePlay = proto.play;
  var nativePause = proto.pause;
  if (!srcDesc || !srcDesc.set || !nativeLoad || !nativePlay) return;

  var toProxyUrl = window.__neonToProxyUrl || function (u) { return u; };
  var targetUrl = '';
  var hlsInstance = null;
  var playGen = 0;
  var strategies = [];
  var strategyIndex = 0;
  var loaded = false;
  var manifestParsed = false;
  var mediaRecovered = false;
  var insideHlsAttach = false;
  var insideHlsDetach = false;
  var suppressNativeError = false;
  var appOnError = null;
  var syntheticCode = 0;

  function isHlsCandidate(url) {
    if (!url || !/^https?:\/\//i.test(url)) return false;
    var clean = url.split('#')[0].split('?')[0].toLowerCase();
    if (/\.m3u8$/.test(clean)) return true;
    return !/\.(mp4|m4v|webm|mkv|avi|mov|mp3|aac|ogg|wav|flac)$/.test(clean);
  }

  function destroyHls() {
    manifestParsed = false;
    if (!hlsInstance) return;
    var h = hlsInstance;
    hlsInstance = null;
    insideHlsDetach = true;
    suppressNativeError = true;
    try { h.destroy(); } catch (e) {}
    suppressNativeError = false;
    insideHlsDetach = false;
  }

  function clearNativeSrc() {
    suppressNativeError = true;
    insideHlsDetach = true;
    try { video.removeAttribute('src'); } catch (e) {}
    try {
      while (video.firstChild) video.removeChild(video.firstChild);
    } catch (e) {}
    try { nativeLoad.call(video); } catch (e) {}
    insideHlsDetach = false;
    suppressNativeError = false;
  }

  function buildStrategies(url) {
    var list = [];
    var hlsSupported = false;
    try { hlsSupported = !!(window.Hls && window.Hls.isSupported()); } catch (e) {}
    if (isHlsCandidate(url) && hlsSupported) list.push('hls');
    if (/^https?:\/\//i.test(url) && toProxyUrl(url) !== url) list.push('native-proxy');
    list.push('native-direct');
    return list;
  }

  function notifyFailure() {
    if (typeof appOnError !== 'function') return;
    syntheticCode = video.error ? 0 : 4;
    try {
      appOnError.call(video);
    } finally {
      syntheticCode = 0;
    }
  }

  function advanceStrategy(gen) {
    if (gen !== playGen) return;
    var nextGen = ++playGen;
    destroyHls();
    if (strategyIndex + 1 < strategies.length) {
      strategyIndex += 1;
      startStrategy(nextGen);
      return;
    }
    notifyFailure();
  }

  function startStrategy(gen) {
    if (gen !== playGen || !targetUrl) return;
    var strat = strategies[strategyIndex];
    if (!strat) {
      notifyFailure();
      return;
    }
    if (strat === 'hls') {
      var hls;
      manifestParsed = false;
      mediaRecovered = false;
      try {
        hls = new window.Hls({
          enableWorker: false,
          lowLatencyMode: false,
          backBufferLength: 10,
          maxBufferLength: 30,
          manifestLoadingTimeOut: 15000,
          levelLoadingTimeOut: 15000,
          fragLoadingTimeOut: 15000,
          manifestLoadingMaxRetry: 2,
          levelLoadingMaxRetry: 2,
          fragLoadingMaxRetry: 3
        });
      } catch (e) {
        advanceStrategy(gen);
        return;
      }
      hlsInstance = hls;
      hls.on(window.Hls.Events.ERROR, function (_ev, data) {
        if (gen !== playGen || hlsInstance !== hls || !data || !data.fatal) return;
        if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR && !mediaRecovered) {
          mediaRecovered = true;
          try { hls.recoverMediaError(); return; } catch (e) {}
        }
        advanceStrategy(gen);
      });
      hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
        if (gen !== playGen || hlsInstance !== hls) return;
        manifestParsed = true;
        try {
          var p = nativePlay.call(video);
          if (p && typeof p.catch === 'function') p.catch(function () {});
        } catch (e) {}
      });
      hls.on(window.Hls.Events.MEDIA_ATTACHED, function () {
        if (gen !== playGen || hlsInstance !== hls) return;
        try { hls.loadSource(targetUrl); } catch (e) { advanceStrategy(gen); }
      });
      insideHlsAttach = true;
      try {
        hls.attachMedia(video);
      } catch (e) {
        insideHlsAttach = false;
        advanceStrategy(gen);
        return;
      }
      insideHlsAttach = false;
      return;
    }

    var resolved = strat === 'native-proxy' ? toProxyUrl(targetUrl) : targetUrl;
    destroyHls();
    clearNativeSrc();
    try {
      srcDesc.set.call(video, resolved);
      nativeLoad.call(video);
      var np = nativePlay.call(video);
      if (np && typeof np.catch === 'function') np.catch(function () {});
    } catch (e) {
      advanceStrategy(gen);
    }
  }

  var errorDesc = Object.getOwnPropertyDescriptor(proto, 'error');
  if (errorDesc && errorDesc.get) {
    Object.defineProperty(video, 'error', {
      configurable: true,
      get: function () {
        var realErr = errorDesc.get.call(video);
        if (realErr) return realErr;
        return syntheticCode ? { code: syntheticCode } : null;
      }
    });
  }

  Object.defineProperty(video, 'onerror', {
    configurable: true,
    get: function () { return appOnError; },
    set: function (fn) { appOnError = typeof fn === 'function' ? fn : null; }
  });

  video.addEventListener('error', function () {
    if (!targetUrl || suppressNativeError) return;
    advanceStrategy(playGen);
  }, false);

  Object.defineProperty(video, 'src', {
    configurable: true,
    get: function () {
      var real = srcDesc.get ? srcDesc.get.call(video) : '';
      if (real && real.indexOf('blob:') === 0) return real;
      return targetUrl || real;
    },
    set: function (value) {
      var str = (value === null || value === undefined) ? '' : String(value).trim();
      if (str.indexOf('blob:') === 0) {
        return srcDesc.set.call(video, str);
      }
      playGen += 1;
      loaded = false;
      destroyHls();
      targetUrl = str;
      strategies = targetUrl ? buildStrategies(targetUrl) : [];
      strategyIndex = 0;
      if (!targetUrl) clearNativeSrc();
    }
  });

  video.load = function () {
    if (insideHlsAttach || insideHlsDetach || loaded || !targetUrl) {
      return nativeLoad.call(video);
    }
    loaded = true;
    playGen += 1;
    strategyIndex = 0;
    startStrategy(playGen);
  };

  video.play = function () {
    if (targetUrl && !loaded) {
      loaded = true;
      playGen += 1;
      strategyIndex = 0;
      startStrategy(playGen);
      return Promise.resolve();
    }
    if (hlsInstance && !manifestParsed) {
      return Promise.resolve();
    }
    return nativePlay.call(video);
  };

  if (nativePause) {
    video.pause = function () {
      return nativePause.call(video);
    };
  }
}());
"""


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
            # Inject desktop.js before hls.min.js/api.js so XHR proxying and device
            # identity apply from the very first request.
            html_path = root / shared / 'index.html'
            html = html_path.read_text(encoding='utf-8')
            marker = '<script src="hls.min.js"></script>'
            if html.count(marker) != 1:
                raise ValueError('Belet entry point changed')
            html_path.write_text(html.replace(marker, '<script src="desktop.js"></script>\n' + marker), encoding='utf-8')
            put(root, shared / 'desktop.js', BELET_DESKTOP_JS)
            origins = ['https://beletapis.com/*', 'https://*.beletapis.com/*', 'https://api.belet.tm/*', 'https://*.belet.me/*']
            icon = 'belet_large_icon.png'
        else:
            html = alemtv_app.build_alemtv_html(desktop=True)
            marker = '<script>\n(function(){'
            if html.count(marker) != 1:
                raise ValueError('Alem TV entry point changed')
            html = html.replace(
                marker,
                '<script src="hls.min.js"></script>\n<script src="desktop.js"></script>\n' + marker,
            )
            put(root, shared / 'index.html', html)
            put(root, shared / 'style.css', alemtv_app.build_alemtv_css())
            for name in ('hls.min.js', 'hls.LICENSE'):
                put(root, shared / name, (ROOT / 'belet_samsung_native' / name).read_bytes())
            put(root, shared / 'desktop.js', ALEMTV_DESKTOP_JS)
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
        packages_dir = HERE / 'packages'
        packages_dir.mkdir(exist_ok=True)
        for old in packages_dir.glob(f'{app}_*_all.deb'):
            old.unlink()
        output = packages_dir / f'{app}_{VERSION}_all.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(root), str(output)],
                       env={**os.environ, 'SOURCE_DATE_EPOCH': '1700000000'}, check=True)


if __name__ == '__main__':
    os.umask(0o022)
    build('beletfilm', 'BeletFilm')
    build('alemtv', 'Alem TV')
