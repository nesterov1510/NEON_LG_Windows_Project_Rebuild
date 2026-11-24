/*
 * Hisense / Toshiba VIDAA compatibility overlay for the shared Belet TV client.
 *
 * Loaded right before hls.min.js / api.js / app.js. Strictly ES5 for older
 * Opera / Blink / Odin / Chromium-based VIDAA TV engines.
 */
(function (global) {
  'use strict';

  var doc = global.document;
  var BACK_ALT_CODE = 461;
  var BELET_HOST_RE = /^https?:\/\/([^\/:?#]+)(?::\d+)?(\/.*)?$/i;
  var BELET_DOMAIN_RE = /(?:^|\.)(?:belet\.me|belet\.tm|beletapis\.com)$/i;
  var ALLOWED_DYNAMIC_HOSTS = {};

  if (global.NodeList && !global.NodeList.prototype.forEach) {
    global.NodeList.prototype.forEach = Array.prototype.forEach;
  }

  function safeStr(fn) {
    try {
      var val = fn();
      return (val === undefined || val === null) ? '' : String(val).replace(/^\s+|\s+$/g, '');
    } catch (e) {
      return '';
    }
  }

  function detectVidaaIdentity() {
    var ua = (global.navigator && global.navigator.userAgent) ? String(global.navigator.userAgent) : '';
    var brand = safeStr(function () {
      return typeof global.Hisense_GetBrand === 'function' ? global.Hisense_GetBrand() : '';
    });
    var model = safeStr(function () {
      return typeof global.Hisense_GetModelName === 'function' ? global.Hisense_GetModelName() : '';
    });
    var firmware = safeStr(function () {
      if (typeof global.Hisense_GetFirmWareVersion === 'function') return global.Hisense_GetFirmWareVersion();
      if (typeof global.Hisense_GetFirmwareVersion === 'function') return global.Hisense_GetFirmwareVersion();
      return '';
    });
    var osv = '';
    var uaVidaa = /\bVIDAA(?:[\/\s_]+([0-9._]+))?/i.exec(ua);
    var uaModel = /\b(Hisense|Toshiba|VIDAA)[\/\s_-]*([A-Za-z0-9_-]{2,24})?/i.exec(ua);
    var modelLabel;

    if (!brand) {
      if (/Toshiba/i.test(ua)) brand = 'Toshiba';
      else brand = 'Hisense';
    }
    if (!model && uaModel && uaModel[2] && !/^(TV|Smart|Browser|Linux)$/i.test(uaModel[2])) {
      model = uaModel[2];
    }
    if (model) {
      modelLabel = (model.toLowerCase().indexOf(brand.toLowerCase()) === 0)
        ? model
        : (brand + ' ' + model);
    } else {
      modelLabel = brand + ' VIDAA TV';
    }

    if (firmware) {
      osv = 'VIDAA ' + firmware;
    } else if (uaVidaa && uaVidaa[1]) {
      osv = 'VIDAA ' + uaVidaa[1];
    } else {
      osv = 'VIDAA';
    }

    return {
      model: modelLabel,
      osVersion: osv
    };
  }

  var cachedIdentity = detectVidaaIdentity();

  /* Used by belet_samsung_native/api.js so device registration reports Hisense/VIDAA instead of Tizen. */
  global.beletWebosIdentity = function () {
    return {
      model: cachedIdentity.model,
      osVersion: cachedIdentity.osVersion
    };
  };

  function exitVidaaApp() {
    var tried = false;
    try {
      if (typeof global.Hisense_ExitApp === 'function') {
        tried = true;
        global.Hisense_ExitApp();
        return;
      }
    } catch (e) {}
    try {
      if (global.omi_platform && typeof global.omi_platform.sendPlatformMessage === 'function') {
        tried = true;
        global.omi_platform.sendPlatformMessage(JSON.stringify({ msg: 'exit' }));
        return;
      }
    } catch (e) {}
    try {
      if (global.opera_omi && typeof global.opera_omi.sendPlatformMessage === 'function') {
        tried = true;
        global.opera_omi.sendPlatformMessage(JSON.stringify({ msg: 'exit' }));
        return;
      }
    } catch (e) {}
    try {
      if (global.PalmSystem && typeof global.PalmSystem.platformBack === 'function') {
        tried = true;
        global.PalmSystem.platformBack();
        return;
      }
    } catch (e) {}
    try { global.close(); } catch (e) {}
    if (!tried) {
      try {
        if (global.history && global.history.length > 1) global.history.back();
      } catch (e) {}
    }
  }

  /* Minimal Tizen/webapis compatibility surface without Tizen app-launch or AVPlay. */
  if (!global.webapis) global.webapis = {};
  if (!global.webapis.productinfo) {
    global.webapis.productinfo = {
      getRealModel: function () { return cachedIdentity.model; },
      getModel: function () { return cachedIdentity.model; },
      getModelCode: function () { return cachedIdentity.model; },
      getFirmware: function () { return cachedIdentity.osVersion; }
    };
  }
  if (!global.tizen) global.tizen = {};
  if (!global.tizen.application) {
    global.tizen.application = {
      getCurrentApplication: function () {
        return {
          appInfo: { id: 'tm.belet.vidaa' },
          exit: exitVidaaApp
        };
      }
    };
  }
  if (!global.tizen.tvinputdevice) {
    global.tizen.tvinputdevice = {
      registerKey: function () {},
      unregisterKey: function () {}
    };
  }
  if (!global.tizen.systeminfo) {
    global.tizen.systeminfo = {
      getCapability: function (uri) {
        if (uri === 'http://tizen.org/feature/platform.version') return cachedIdentity.osVersion;
        if (uri === 'http://tizen.org/system/model_name') return cachedIdentity.model;
        return '';
      },
      getPropertyValue: function (prop, onSuccess, onError) {
        if (prop === 'BUILD' && typeof onSuccess === 'function') {
          onSuccess({ model: cachedIdentity.model, buildVersion: cachedIdentity.osVersion });
          return;
        }
        if (typeof onError === 'function') onError({ message: 'not supported' });
      }
    };
  }

  /* ── Optional LAN CORS/TLS proxy bridge (active only when launched via NEON portal) ── */
  function resolveProxyBase() {
    var raw = typeof global.__NEON_BELET_PROXY__ === 'string' ? global.__NEON_BELET_PROXY__ : '';
    raw = raw.replace(/^\s+|\s+$/g, '').replace(/\/+$/, '');
    return raw;
  }

  var proxyBase = resolveProxyBase();

  function isSafePublicHost(host) {
    var h = String(host || '').toLowerCase();
    if (!h || h.indexOf('.') <= 0) return false;
    if (h === 'localhost' || /^127\./.test(h) || /^10\./.test(h) || /^192\.168\./.test(h)) return false;
    if (/^172\.(1[6-9]|2\d|3[0-1])\./.test(h)) return false;
    if (/\.local$|\.internal$|\.lan$/i.test(h)) return false;
    return /^[a-z0-9.-]+$/.test(h);
  }

  function rememberHostsFromText(text) {
    var re;
    var m;
    var host;
    if (!text || typeof text !== 'string') return;
    if (text.indexOf('http://') < 0 && text.indexOf('https://') < 0) return;
    re = /https?:\/\/([a-z0-9.-]+)(?::\d+)?\//gi;
    while ((m = re.exec(text)) !== null) {
      host = String(m[1] || '').toLowerCase();
      if (isSafePublicHost(host)) {
        ALLOWED_DYNAMIC_HOSTS[host] = true;
      }
    }
  }

  function shouldProxyHost(host) {
    var h = String(host || '').toLowerCase();
    if (!h) return false;
    if (BELET_DOMAIN_RE.test(h)) return true;
    return !!ALLOWED_DYNAMIC_HOSTS[h];
  }

  function toProxyUrl(url) {
    var m;
    var host;
    var path;
    if (!proxyBase || typeof url !== 'string') return url;
    if (url.indexOf(proxyBase + '/') === 0) return url;
    m = BELET_HOST_RE.exec(url);
    if (!m) return url;
    host = String(m[1] || '').toLowerCase();
    if (!shouldProxyHost(host)) return url;
    path = m[2] || '/';
    return proxyBase + '/__proxy/' + host + path;
  }

  global.__neonVidaaToProxyUrl = toProxyUrl;

  if (proxyBase && typeof global.XMLHttpRequest === 'function') {
    (function () {
      var proto = global.XMLHttpRequest.prototype;
      var origOpen = proto.open;
      var origSend = proto.send;

      proto.open = function (method, url) {
        var args = Array.prototype.slice.call(arguments);
        var rawUrl = typeof url === 'string' ? url : String(url || '');
        var proxied = toProxyUrl(rawUrl);
        if (proxied !== rawUrl) {
          args[1] = proxied;
          this.__neonOrigUrl = rawUrl;
          this.__neonProxied = true;
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
        } else {
          this.__neonProxied = false;
          if (this.__neonOrigUrl) {
            delete this.__neonOrigUrl;
            try { delete this.responseURL; } catch (e) {}
          }
        }
        return origOpen.apply(this, args);
      };

      proto.send = function () {
        if (this.__neonProxied && !this.__neonScanWired) {
          this.__neonScanWired = true;
          this.addEventListener('load', function () {
            try {
              if (this.responseType === '' || this.responseType === 'text') {
                rememberHostsFromText(this.responseText);
              }
            } catch (e) {}
          }, false);
        }
        return origSend.apply(this, arguments);
      };
    }());
  }

  /* Ensure Belet API + images stay bound to the LAN proxy when proxyBase is active. */
  function wireBeletProxyHook() {
    if (!proxyBase) return;
    if (global.Belet && !global.Belet.__neonVidaaWired) {
      global.Belet.__neonVidaaWired = true;
      var origSet = global.Belet.setClockProxy;
      if (typeof origSet === 'function') {
        origSet(proxyBase);
        global.Belet.setClockProxy = function (url) {
          return origSet(url || proxyBase);
        };
      }
    }
    var video = doc && doc.getElementById('video');
    var proto = global.HTMLMediaElement && global.HTMLMediaElement.prototype;
    var srcDesc = proto && Object.getOwnPropertyDescriptor(proto, 'src');
    if (video && !video.__neonVidaaSrcWired && srcDesc && srcDesc.set && srcDesc.get) {
      video.__neonVidaaSrcWired = true;
      var origVideoUrl = '';
      try {
        Object.defineProperty(video, 'src', {
          configurable: true,
          get: function () {
            var real = srcDesc.get.call(video);
            if (!real) { origVideoUrl = ''; return ''; }
            if (real.indexOf('blob:') === 0 || real.indexOf('data:') === 0) return real;
            return origVideoUrl || real;
          },
          set: function (value) {
            var raw = (value === null || value === undefined) ? '' : String(value);
            var m;
            if (!raw || raw.indexOf('blob:') === 0 || raw.indexOf('data:') === 0) {
              origVideoUrl = '';
              return srcDesc.set.call(video, raw);
            }
            origVideoUrl = raw;
            m = BELET_HOST_RE.exec(raw);
            if (m && isSafePublicHost(m[1])) {
              ALLOWED_DYNAMIC_HOSTS[String(m[1]).toLowerCase()] = true;
            }
            return srcDesc.set.call(video, toProxyUrl(raw));
          }
        });
      } catch (e) {}
    }
  }

  if (proxyBase) {
    try {
      (function () {
        var realBelet = global.Belet;
        Object.defineProperty(global, 'Belet', {
          configurable: true,
          enumerable: true,
          get: function () { return realBelet; },
          set: function (val) {
            realBelet = val;
            try { wireBeletProxyHook(); } catch (e) {}
          }
        });
      }());
    } catch (e) {}
    if (doc && doc.addEventListener) doc.addEventListener('DOMContentLoaded', wireBeletProxyHook, true);
    global.addEventListener('DOMContentLoaded', wireBeletProxyHook, false);
    setTimeout(wireBeletProxyHook, 0);
  }

  /* ── VIDAA remote control key normalization + air-mouse focus ── */
  function isInputOverlayOpen() {
    var numpad = doc && doc.getElementById('numpad');
    var keyboard = doc && doc.getElementById('keyboard');
    var active = doc && doc.activeElement;
    var tag = active && active.tagName ? String(active.tagName).toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return true;
    if (numpad && !numpad.classList.contains('hidden')) return true;
    if (keyboard && !keyboard.classList.contains('hidden')) return true;
    return false;
  }

  function dispatchNormalizedKey(code, keyName) {
    var ev;
    try {
      ev = doc.createEvent('Event');
      ev.initEvent('keydown', true, true);
    } catch (e) {
      return false;
    }
    try {
      Object.defineProperty(ev, 'keyCode', { get: function () { return code; } });
      Object.defineProperty(ev, 'which', { get: function () { return code; } });
      if (keyName) Object.defineProperty(ev, 'key', { get: function () { return keyName; } });
    } catch (e) {
      try { ev.keyCode = code; ev.which = code; } catch (e2) {}
    }
    ev.__neonSynthetic = true;
    doc.dispatchEvent(ev);
    return true;
  }

  if (doc && doc.addEventListener) {
    doc.addEventListener('keydown', function (event) {
      var code;
      var key;
      var mapped;
      if (!event || event.__neonSynthetic) return;
      code = event.keyCode || event.which || 0;
      key = typeof event.key === 'string' ? event.key : '';

      /* VIDAA Back button often arrives as Backspace (8), BrowserBack (166), or XF86Back. */
      if (!isInputOverlayOpen()) {
        if (code === 8 || code === 166 ||
            key === 'XF86Back' || key === 'Back' || key === 'GoBack' || key === 'BrowserBack') {
          if (typeof event.stopImmediatePropagation === 'function') event.stopImmediatePropagation();
          if (typeof event.preventDefault === 'function') event.preventDefault();
          dispatchNormalizedKey(BACK_ALT_CODE, 'Escape');
          return;
        }
      }

      /* Normalize missing keyCode on newer VIDAA WebViews where only event.key is set. */
      if (!code && key) {
        mapped = {
          ArrowLeft: 37,
          ArrowUp: 38,
          ArrowRight: 39,
          ArrowDown: 40,
          Enter: 13,
          Escape: 27
        }[key];
        if (mapped) {
          if (typeof event.stopImmediatePropagation === 'function') event.stopImmediatePropagation();
          if (typeof event.preventDefault === 'function') event.preventDefault();
          dispatchNormalizedKey(mapped, key);
        }
      }
    }, true);

    doc.addEventListener('mouseover', function (event) {
      var target = event && event.target;
      while (target && target !== doc) {
        if (target.classList && target.classList.contains('focusable')) {
          if (typeof target.focus === 'function') {
            try { target.focus(); } catch (e) {}
          }
          break;
        }
        target = target.parentNode;
      }
    }, false);
  }
}(window));
