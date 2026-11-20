/*
 * LG webOS overlay for the shared Belet TV client (tm.belet.neonclient).
 *
 * index.html / api.js / app.js / style.css are the byte-identical files that
 * ship in the Samsung WGT — nothing in them is rewritten for LG. This script
 * only fills the platform seams webOS does not have:
 *
 *   • the Tizen surfaces the client touches (webapis.productinfo,
 *     tizen.systeminfo, tizen.tvinputdevice, tizen.application) are emulated so
 *     the client runs exactly the same code path as on a Samsung TV: the device
 *     registration body still says "Samsung Tizen <model>" and os_version still
 *     looks like a Tizen release during post-login registration;
 *   • the Magic Remote pointer moves the DOM focus, because the client keeps a
 *     keyboard focus model instead of mouse hover;
 *   • Back on the first screen leaves the app instead of doing nothing.
 *
 * The client already understands webOS key codes (arrows 37-40, OK 13, Back 461,
 * Play 415, Pause 19, FF 417, RW 412, Stop 413), so no key translation is needed.
 * Every call is wrapped: a missing webOS API must never break the client.
 */
(function (global) {
  var doc = global.document;
  var modelName = '';
  var realOsVersion = '';

  /* wire() and screen() iterate querySelectorAll results. Older webOS DOMs
   * lack NodeList.forEach even though Array.forEach is available. Install it
   * before app.js boots, otherwise the login buttons never get handlers. */
  if (global.NodeList && !global.NodeList.prototype.forEach) {
    global.NodeList.prototype.forEach = Array.prototype.forEach;
  }

  /* Reported to the backend through tizen.systeminfo, so keep the shape of a
   * Tizen release: api.js sends it as os_version of the device registration. */
  var TIZEN_VERSION = '6.0';

  function userAgent() {
    try { return String(global.navigator.userAgent || ''); } catch (e) { return ''; }
  }

  function guessFromUserAgent() {
    var ua = userAgent();
    var webos = /Web0S|webOS/i.test(ua);
    var chromium = ua.match(/Chrome\/([0-9]+)/);
    if (!realOsVersion) realOsVersion = webos ? 'webOS' : 'LG';
    if (!modelName) {
      modelName = webos ? 'LG webOS TV' : 'LG Smart TV';
      if (chromium) modelName = modelName + ' ' + chromium[1];
    }
  }

  function applySystemInfo(info) {
    if (!info || typeof info !== 'object') return;
    if (info.modelName) modelName = String(info.modelName);
    if (info.version) realOsVersion = String(info.version);
    if (info.platform) realOsVersion = String(info.platform) + (info.version ? ' ' + info.version : '');
  }

  /* PalmSystem.deviceInfo exists on every webOS TV and needs no permission. */
  try {
    if (global.PalmSystem && global.PalmSystem.deviceInfo) {
      var raw = global.PalmSystem.deviceInfo;
      applySystemInfo(typeof raw === 'string' ? JSON.parse(raw) : raw);
    }
  } catch (e) {}

  /* Newer builds also answer a Luna query; failure is fine, the UA guess stays. */
  try {
    if (global.webOS && global.webOS.service && global.webOS.service.request) {
      global.webOS.service.request('luna://com.webos.service.tv.systeminfo', {
        method: 'getSystemInfo',
        parameters: { keys: ['modelName', 'version'] },
        onSuccess: function (response) { applySystemInfo(response); },
        onFailure: function () {}
      });
    }
  } catch (e) {}

  guessFromUserAgent();

  function modelValue() {
    return modelName || 'LG webOS TV';
  }

  function capability(name) {
    switch (name) {
      case 'http://tizen.org/feature/platform.version': return TIZEN_VERSION;
      case 'http://tizen.org/feature/platform.name': return 'Tizen';
      case 'http://tizen.org/feature/platform.core.cpu.arch': return 'arm';
      case 'http://tizen.org/system/model_name': return modelValue();
      case 'http://tizen.org/feature/screen.size.all': return '1920x1080';
      default: return '';
    }
  }

  function noop() {}

  function exitApp() {
    try {
      if (global.PalmSystem && global.PalmSystem.platformBack) { global.PalmSystem.platformBack(); return; }
    } catch (e) {}
    try {
      if (global.webOS && global.webOS.platformBack) { global.webOS.platformBack(); return; }
    } catch (e) {}
    try { global.close(); } catch (e) {}
  }

  /* ── webapis (Tizen web API namespace) ─────────────────────────────── */
  try {
    global.webapis = global.webapis || {};
    if (!global.webapis.productinfo) {
      global.webapis.productinfo = {
        getModel: function () { return modelValue(); },
        getVersion: function () { return realOsVersion || TIZEN_VERSION; },
        getDuid: function () { return ''; },
        isSamsungTV: function () { return false; }
      };
    }
    /* No avplay on webOS: app.js then uses the hls.js → <video> chain. */
  } catch (e) {}

  /* ── tizen (systeminfo / tvinputdevice / application) ──────────────── */
  try {
    global.tizen = global.tizen || {};
    if (!global.tizen.systeminfo) {
      global.tizen.systeminfo = {
        getCapability: function (name) { return capability(name); },
        getPropertyValue: function (name, onSuccess, onError) {
          var values = {
            MODEL_NAME: modelValue(),
            VENDOR: 'LG',
            PLATFORM_VERSION: TIZEN_VERSION,
            FIRMWARE_VERSION: realOsVersion
          };
          if (Object.prototype.hasOwnProperty.call(values, name)) {
            try { if (onSuccess) onSuccess({ value: values[name] }); } catch (e) {}
          } else {
            try { if (onError) onError(new Error('Not supported on webOS')); } catch (e) {}
          }
        }
      };
    }
    if (!global.tizen.tvinputdevice) {
      global.tizen.tvinputdevice = {
        registerKey: function () { return true; },
        registerKeyBatch: function () { return true; },
        unregisterKey: function () { return true; },
        getSupportedKeys: function (onSuccess) {
          try { if (onSuccess) onSuccess([]); } catch (e) {}
        }
      };
    }
    if (!global.tizen.application) {
      var currentApp = { exit: function () { exitApp(); }, hide: noop };
      global.tizen.application = {
        getCurrentApplication: function () { return currentApp; },
        exit: function () { exitApp(); }
      };
    }
  } catch (e) {}

  /* ── Magic Remote: hovering an item focuses it ─────────────────────── */
  function focusableOf(node) {
    var limit = 0;
    while (node && limit < 6) {
      if (node.classList && node.classList.contains('focusable') && !node.disabled) return node;
      node = node.parentNode;
      limit += 1;
    }
    return null;
  }

  try {
    doc.addEventListener('mouseover', function (event) {
      var target = focusableOf(event.target);
      if (target && target !== doc.activeElement && target.focus) {
        try { target.focus(); } catch (e) {}
      }
    }, false);
  } catch (e) {}

  /* Registered after app.js so its handler runs first: when the client consumed
   * Back it calls preventDefault(), and an unconsumed Back means "leave the app". */
  try {
    global.setTimeout(function () {
      doc.addEventListener('keydown', function (event) {
        var key = event.keyCode || event.which;
        if (key !== 461 && key !== 10009) return;
        if (event.defaultPrevented) return;
        exitApp();
      }, false);
    }, 0);
  } catch (e) {}

  /* A packaged app can display get-qrcode while Chromium blocks the cross-origin
   * JSON POST. Only QR polling uses the local service; keep Samsung's XHR path
   * available if the LS2 bridge/service is unavailable on this firmware. */
  var Bridge = global.WebOSServiceBridge || global.PalmServiceBridge;
  if (Bridge) {
    global.beletCheckQrNative = function (code, fingerprint, web, callback) {
      var bridge, timer, done = false;
      function finish(status, data) {
        if (done) return;
        done = true;
        global.clearTimeout(timer);
        callback(status, data);
      }
      try {
        bridge = new Bridge();
        bridge.onservicecallback = function (raw) {
          var response;
          try { response = JSON.parse(raw); } catch (e) {}
          finish(response && response.returnValue && Number(response.status) || 0, response && response.data);
        };
        timer = global.setTimeout(function () {
          try { bridge.cancel(); } catch (e) {}
          finish(0);
        }, 16000);
        bridge.call('luna://tm.belet.neonclient.qr/check', JSON.stringify({ code: code, fingerprint: fingerprint, web: web }));
      } catch (e) { finish(0); }
    };
  }

  global.__NEON_TV_PLATFORM__ = 'webos';
}(this));
