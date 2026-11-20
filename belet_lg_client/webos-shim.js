/*
 * LG webOS overlay for the shared Belet TV client (tm.belet.neonclient).
 *
 * index.html/api.js/app.js/style.css are the very same files that ship in the
 * Samsung WGT; this script only fills the platform seams webOS does not have:
 *
 *   • Tizen device APIs (webapis.productinfo / tizen.systeminfo) are emulated so
 *     the About screen and the device registration report a real LG model;
 *   • the Magic Remote pointer moves the DOM focus, because the client keeps a
 *     keyboard focus model instead of mouse hover;
 *   • Back on the first screen leaves the app instead of doing nothing.
 *
 * The client already understands webOS key codes (arrows 37-40, OK 13, Back 461,
 * Play 415, Pause 19, FF 417, RW 412, Stop 413), so no key translation is needed.
 */
(function (global) {
  var doc = global.document;
  var modelName = '';
  var osVersion = '';

  function userAgent() {
    try { return String(global.navigator.userAgent || ''); } catch (e) { return ''; }
  }

  function guessFromUserAgent() {
    var ua = userAgent();
    var webos = /Web0S|webOS\.TV|webOS/i.test(ua);
    var chromium = ua.match(/Chrome\/([0-9]+)/);
    if (!osVersion) osVersion = webos ? 'webOS' : 'LG';
    if (!modelName) {
      modelName = webos ? 'LG webOS TV' : 'LG Smart TV';
      if (chromium) modelName = modelName + ' · Chromium ' + chromium[1];
    }
  }

  function applySystemInfo(info) {
    if (!info || typeof info !== 'object') return;
    if (info.modelName) modelName = String(info.modelName);
    if (info.version) osVersion = String(info.version);
    if (info.platform) osVersion = String(info.platform) + (info.version ? ' ' + info.version : '');
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

  /* api.js already reads model()/tizenVersion() through these two surfaces. */
  global.webapis = global.webapis || {};
  if (!global.webapis.productinfo) {
    global.webapis.productinfo = {
      getModel: function () { return modelName; },
      getVersion: function () { return osVersion; }
    };
  }
  global.tizen = global.tizen || {};
  if (!global.tizen.systeminfo) {
    global.tizen.systeminfo = {
      getCapability: function (name) {
        if (name === 'http://tizen.org/feature/platform.version') return osVersion;
        if (name === 'http://tizen.org/system/model_name') return modelName;
        return '';
      }
    };
  }

  function focusableOf(node) {
    var limit = 0;
    while (node && limit < 6) {
      if (node.classList && node.classList.contains('focusable') && !node.disabled) return node;
      node = node.parentNode;
      limit += 1;
    }
    return null;
  }

  /* Magic Remote: hovering an item focuses it, so OK/arrows continue from there. */
  doc.addEventListener('mouseover', function (event) {
    var target = focusableOf(event.target);
    if (target && target !== doc.activeElement && target.focus) {
      try { target.focus(); } catch (e) {}
    }
  }, false);

  function exitApp() {
    try {
      if (global.PalmSystem && global.PalmSystem.platformBack) { global.PalmSystem.platformBack(); return; }
    } catch (e) {}
    try {
      if (global.webOS && global.webOS.platformBack) { global.webOS.platformBack(); return; }
    } catch (e) {}
    try { global.close(); } catch (e) {}
  }

  /* Registered after app.js so its handler runs first: when the client consumed
   * Back it calls preventDefault(), and an unconsumed Back means "leave the app". */
  global.setTimeout(function () {
    doc.addEventListener('keydown', function (event) {
      var key = event.keyCode || event.which;
      if (key !== 461 && key !== 10009) return;
      if (event.defaultPrevented) return;
      exitApp();
    }, false);
  }, 0);

  global.__NEON_TV_PLATFORM__ = 'webos';
}(this));
