/*
 * Belet Samsung client — interface and player.
 *
 * Features: SMS/QR sign-in, home rows (header, continue watching, tops), category catalogue
 * with paging, search, title details with seasons and episodes, favourites, profile,
 * network diagnostics, and an HLS player with resume/progress reporting.
 *
 * The UI is keyboard/remote driven: every interactive element carries `.focusable` and the
 * spatial navigation below moves focus. No mouse is required anywhere.
 *
 * ES5 only (older Tizen engines). See api.js for the network contract and its sources.
 */
(function () {
  'use strict';

  var Belet = window.Belet;
  var doc = document;

  var KEYS = {
    OK: 13,
    LEFT: 37,
    UP: 38,
    RIGHT: 39,
    DOWN: 40,
    PLAY: 415,
    PAUSE: 19,
    PLAY_PAUSE: 179,
    STOP: 413,
    REWIND: 412,
    FORWARD: 417,
    BACK: 10009,
    ESC: 27,
    BACK_ALT: 461,
    RED: 403,
    GREEN: 404,
    YELLOW: 405,
    BLUE: 406
  };

  var SCREENS = ['boot', 'login', 'home', 'browse', 'search', 'detail', 'player', 'profile', 'about'];

  var state = {
    screen: 'boot',
    previous: 'home',
    bootDone: false,
    bootTimer: null,
    bootSession: null,
    clockSynced: true,
    clockSyncTimer: null,
    bypassFallbackReady: false,
    homeLoaded: false,
    homeGen: 0,
    homeRetryTimer: null,
    home: { groups: [], hero: null, items: [] },
    categories: [],
    categoriesLoaded: false,
    browse: { mode: 'category', id: null, title: '', items: [], offset: 0, loading: false, done: false },
    search: { text: '', items: [], page: 1, loading: false, done: false },
    detail: { movie: null, seasons: [], seasonId: null, episodes: [], loadingEpisodes: false },
    player: {
      movie: null, movieId: null, seasonId: null, episodeId: null, title: '', subtitle: '',
      sources: [], sourceIndex: 0, startAt: 0, reported: 0, timer: null, osdTimer: null,
      active: false, ending: false, duration: 0
    },
    login: {
      phone: '', code: '', tempToken: '', methods: [], methodIndex: 0,
      busy: false, generation: 0, loggingOut: false, otpTimer: null, otpFallback: false
    },
    qr: { generation: 0, timer: null, code: null },
    profileRetryTimer: null,
    toastTimer: null,
    backArmed: false,
    playbackInfo: 'плеер ещё не запускался',
    input: null,
    dialogActions: [],
    mainFocus: null
  };

  /* ── tiny DOM helpers ─────────────────────────────────────────────── */

  function $(id) { return doc.getElementById(id); }

  function el(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function on(node, event, handler) {
    if (node && node.addEventListener) node.addEventListener(event, handler, false);
  }

  function hasClass(node, name) { return !!(node && node.classList && node.classList.contains(name)); }

  function setHidden(node, hidden) {
    if (!node) return;
    if (hidden) node.classList.add('hidden');
    else node.classList.remove('hidden');
  }

  function closest(node, selector) {
    while (node && node !== doc.body) {
      if (node.matches && node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function isVisible(node) {
    return !!(node && (node.offsetWidth || node.offsetHeight || node.getClientRects().length));
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function formatTime(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    return h ? h + ':' + pad2(m) + ':' + pad2(s) : pad2(m) + ':' + pad2(s);
  }

  function parseDuration(value) {
    var parts;
    if (typeof value === 'number') return value;
    if (typeof value !== 'string' || !value) return 0;
    if (/^\d+(\.\d+)?$/.test(value)) return Math.round(parseFloat(value));
    parts = value.split(':');
    if (parts.length === 2) return (parseInt(parts[0], 10) || 0) * 60 + (parseInt(parts[1], 10) || 0);
    if (parts.length === 3) {
      return (parseInt(parts[0], 10) || 0) * 3600 + (parseInt(parts[1], 10) || 0) * 60 + (parseInt(parts[2], 10) || 0);
    }
    return 0;
  }

  /* Duration arrives either as seconds (number) or as an already formatted string. */
  function durationText(value) {
    var seconds;
    if (typeof value === 'string' && value.indexOf(':') >= 0) return value;
    seconds = parseDuration(value);
    if (!seconds) return '';
    if (seconds >= 3600) {
      var hours = Math.floor(seconds / 3600);
      var minutes = Math.round((seconds % 3600) / 60);
      return hours + ' ч' + (minutes ? ' ' + minutes + ' мин' : '');
    }
    return Math.max(1, Math.round(seconds / 60)) + ' мин';
  }

  /* Genres come back as [{id, name}], but plain strings show up in some payloads. */
  function genreNames(list) {
    if (!Array.isArray(list)) return '';
    return list.map(function (genre) {
      if (!genre) return '';
      return typeof genre === 'string' ? genre : (genre.name || '');
    }).filter(function (name) { return !!name; }).join(' · ');
  }

  function maskPhone(phone) {
    var digits = String(phone || '').replace(/\D/g, '');
    if (digits.length < 6) return 'Аккаунт Belet';
    return '+' + digits.slice(0, 3) + ' ••• ' + digits.slice(-4);
  }

  /* ── toasts, dialogs, overlays ────────────────────────────────────── */

  function toast(text, ms) {
    var node = $('toast');
    node.textContent = text;
    setHidden(node, false);
    if (state.toastTimer) clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(function () {
      setHidden(node, true);
      state.toastTimer = null;
    }, ms || 3200);
  }

  function dialog(title, text, actions) {
    var actionsHost = $('dialogActions');
    clear(actionsHost);
    state.dialogActions = actions || [];
    $('dialogTitle').textContent = title || '';
    $('dialogText').textContent = text || '';
    state.dialogActions.forEach(function (action, index) {
      var button = el('button', 'focusable ' + (action.primary ? 'primary' : 'ghost'), action.label);
      button.onclick = function () { chooseDialog(index); };
      actionsHost.appendChild(button);
    });
    setHidden($('dialog'), false);
    focusFirst($('dialog'));
  }

  function closeDialog() {
    setHidden($('dialog'), true);
    var previous = document.activeElement;
    var returnTo = state.mainFocus;
    if (returnTo && isVisible(returnTo)) returnTo.focus();
    else if (previous) previous.blur();
  }

  function chooseDialog(index) {
    var action = state.dialogActions[index];
    closeDialog();
    if (action && action.onSelect) action.onSelect();
  }

  function openDialogRoot() {
    return hasClass($('dialog'), 'hidden') ? null : $('dialog');
  }

  /* Generic input overlay: numeric pad or full keyboard. */
  var NUM_KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'delete', '0', 'ok'];
  var NUM_IP_KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '.', '0', 'ok'];
  var KB_ROWS = ['йцукенгшщзхъ', 'фывапролджэ', 'ячсмитьбю', '1234567890', '- '];

  function inputOverlay(kind, options) {
    var isNum = kind === 'num';
    var host = isNum ? $('numpad') : $('keyboard');
    var keysHost = isNum ? $('numpadKeys') : $('keyboardKeys');
    var padKeys = options.allowDot ? NUM_IP_KEYS : NUM_KEYS;
    state.input = {
      kind: kind,
      value: options.value || '',
      maxLength: options.maxLength || (isNum ? 15 : 40),
      onDone: options.onDone,
      onCancel: options.onCancel
    };
    $(isNum ? 'numpadTitle' : 'keyboardTitle').textContent = options.title || '';
    clear(keysHost);
    if (isNum) {
      padKeys.forEach(function (key) {
        var button;
        if (key === 'delete') button = el('button', 'key focusable', '⌫');
        else if (key === 'ok') button = el('button', 'key focusable', 'OK');
        else button = el('button', 'key focusable', key);
        button.onclick = function () {
          if (key === 'delete') inputDelete();
          else if (key === 'ok') inputDone();
          else inputAppend(key);
        };
        keysHost.appendChild(button);
      });
    } else {
      KB_ROWS.forEach(function (row) {
        var rowNode = el('div', 'kb-row');
        row.split('').forEach(function (char) {
          var button;
          if (char === '-') return;
          if (char === ' ') return;
          button = el('button', 'key focusable', char.toUpperCase());
          button.onclick = (function (value) {
            return function () { inputAppend(value); };
          }(char));
          rowNode.appendChild(button);
        });
        if (row === KB_ROWS[2]) {
          var space = el('button', 'key space focusable', 'пробел');
          space.onclick = function () { inputAppend(' '); };
          rowNode.appendChild(space);
        }
        keysHost.appendChild(rowNode);
      });
    }
    setHidden(host, false);
    inputRender();
    var firstEnabled = keysHost.querySelector('.focusable');
    if (firstEnabled) firstEnabled.focus();
  }

  function inputRender() {
    var input = state.input;
    var node = $(input.kind === 'num' ? 'numpadValue' : 'keyboardValue');
    node.textContent = input.value || (input.kind === 'num' ? '—' : 'Введите запрос');
    node.className = 'pad-value' + (input.value ? '' : ' muted');
  }

  function inputAppend(char) {
    var input = state.input;
    if (!input) return;
    if (input.value.length >= input.maxLength) return;
    input.value += char;
    inputRender();
  }

  function inputDelete() {
    if (!state.input) return;
    state.input.value = state.input.value.slice(0, -1);
    inputRender();
  }

  function inputClear() {
    if (!state.input) return;
    state.input.value = '';
    inputRender();
  }

  function inputDone() {
    var input = state.input;
    if (!input) return;
    if (!input.value) {
      toast(input.kind === 'num' ? 'Введите данные' : 'Введите запрос');
      return;
    }
    var value = input.value;
    var done = input.onDone;
    state.input = null;
    setHidden($('numpad'), true);
    setHidden($('keyboard'), true);
    if (done) done(value);
  }

  function closeInput() {
    var input = state.input;
    if (!input) return;
    state.input = null;
    setHidden($('numpad'), true);
    setHidden($('keyboard'), true);
    if (input.onCancel) input.onCancel();
    else if (state.mainFocus && isVisible(state.mainFocus)) state.mainFocus.focus();
  }

  function activeOverlay() {
    if (!hasClass($('dialog'), 'hidden')) return $('dialog');
    if (!hasClass($('numpad'), 'hidden')) return $('numpad');
    if (!hasClass($('keyboard'), 'hidden')) return $('keyboard');
    return null;
  }

  /* ── focus and spatial navigation ─────────────────────────────────── */

  function focusFirst(root) {
    var list = focusables(root);
    if (list.length) {
      list[0].focus();
      reveal(list[0]);
      return list[0];
    }
    return null;
  }

  function focusables(root) {
    var nodes = (root || doc).querySelectorAll('.focusable');
    var out = [];
    var i;
    for (i = 0; i < nodes.length; i += 1) {
      if (!nodes[i].disabled && isVisible(nodes[i])) out.push(nodes[i]);
    }
    return out;
  }

  function navigationRoot() {
    var overlay = activeOverlay();
    if (overlay) return overlay;
    return doc;
  }

  function currentScreen() {
    return doc.querySelector('.screen:not(.hidden)') || doc;
  }

  function focusScreen() {
    return focusFirst(currentScreen());
  }

  function reveal(node) {
    var track = closest(node, '.row-track');
    var scroller = closest(node, '#home, #browse, #search, .detail-scroll, #profile, #about, .pad-card');
    var rect, host, margin;
    if (track) {
      rect = node.getBoundingClientRect();
      host = track.getBoundingClientRect();
      if (rect.left < host.left + 56) track.scrollLeft -= (host.left + 56 - rect.left);
      else if (rect.right > host.right - 56) track.scrollLeft += (rect.right - (host.right - 56));
    }
    if (scroller) {
      rect = node.getBoundingClientRect();
      host = scroller.getBoundingClientRect();
      margin = 150;
      if (rect.top < host.top + margin) scroller.scrollTop -= (host.top + margin - rect.top);
      else if (rect.bottom > host.bottom - 70) scroller.scrollTop += (rect.bottom - (host.bottom - 70));
    }
    try {
      if (!track && node.scrollIntoView) node.scrollIntoView(false);
    } catch (e) {}
  }

  function move(direction) {
    var root = navigationRoot();
    var items = focusables(root);
    var current = doc.activeElement;
    var rect, i, best, bestScore, candidate, r, dx, dy, primary, secondary, score;
    if (!items.length) return false;
    if (!current || items.indexOf(current) === -1) {
      current = items[0];
      current.focus();
      reveal(current);
      return true;
    }
    rect = current.getBoundingClientRect();
    best = null;
    bestScore = Infinity;
    for (i = 0; i < items.length; i += 1) {
      candidate = items[i];
      if (candidate === current) continue;
      r = candidate.getBoundingClientRect();
      dx = (r.left + r.width / 2) - (rect.left + rect.width / 2);
      dy = (r.top + r.height / 2) - (rect.top + rect.height / 2);
      if (direction === 'left' && dx > -4) continue;
      if (direction === 'right' && dx < 4) continue;
      if (direction === 'up' && dy > -4) continue;
      if (direction === 'down' && dy < 4) continue;
      primary = (direction === 'left' || direction === 'right') ? Math.abs(dx) : Math.abs(dy);
      secondary = (direction === 'left' || direction === 'right') ? Math.abs(dy) : Math.abs(dx);
      score = primary + secondary * 2.6;
      if (score < bestScore) { bestScore = score; best = candidate; }
    }
    if (!best) return false;
    best.focus();
    reveal(best);
    return true;
  }

  /* ── screens ──────────────────────────────────────────────────────── */

  function screen(name) {
    var i;
    for (i = 0; i < SCREENS.length; i += 1) setHidden($(SCREENS[i]), SCREENS[i] !== name);
    state.screen = name;
    setHidden($('topbar'), name === 'boot' || name === 'login' || name === 'player');
    $('nav').querySelectorAll('.nav-item').forEach(function (item) {
      item.classList.toggle('active', item.getAttribute('data-nav') === name);
    });
    if (name === 'home' || name === 'browse' || name === 'search' || name === 'profile') {
      state.previous = name;
    }
  }

  function readTizenClock() {
    try {
      if (window.tizen && window.tizen.time && window.tizen.time.getCurrentDateTime) {
        return window.tizen.time.getCurrentDateTime();
      }
    } catch (e) {}
    return null;
  }

  function formatDateObj(d) {
    if (!d) return '';
    return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' +
      pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function readBypassMode() {
    var metas, i, name, content;
    try {
      metas = doc && doc.querySelectorAll ? doc.querySelectorAll('meta') : [];
      for (i = 0; i < metas.length; i += 1) {
        name = metas[i] && metas[i].getAttribute ? metas[i].getAttribute('name') : '';
        if (name === 'neon-bypass-mode') {
          content = String(metas[i].getAttribute('content') || '').replace(/^\s+|\s+$/g, '').toLowerCase();
          if (content === 'always' || content === 'off') return content;
        }
      }
    } catch (e) {}
    return 'auto';
  }

  function isBypassModeForced() {
    return readBypassMode() === 'always';
  }

  function isBypassModeDisabled() {
    return readBypassMode() === 'off';
  }

  function isClockInvalid() {
    return new Date().getFullYear() < 2026;
  }

  function shouldUseBypass() {
    if (isBypassModeDisabled()) return false;
    return isClockInvalid() || isBypassModeForced();
  }

  function isClockBypassed() {
    return !!(window.Belet && Belet.clockProxy && Belet.clockProxy());
  }

  function isClockBlockingHttps() {
    if (isBypassModeDisabled()) return false;
    if (isClockBypassed()) return false;
    if (isClockInvalid()) return true;
    if (isBypassModeForced()) return !state.bypassFallbackReady;
    return false;
  }

  function clockDateText() {
    return formatDateObj(new Date());
  }

  var CLOCK_PROXY_PORT = 18767;
  var CLOCK_PROXY_SVC = 'BeletFilm1.ClockProxy';
  var PC_BRIDGE_KEY = 'neon_belet_pc_bridge_v1';
  var WRT_INLINE_CODE = "import { Service } from 'wrt:service'; window.__neonWrtService = Service; window.dispatchEvent(new Event('neon-wrt-ready'));";
  var clockProxyState = {
    launched: false,
    probing: false,
    wrtTried: false,
    tvIp: '',
    base: '',
    diag: 'не требуется',
    launchDiag: '',
    serverOffsetMs: 0,
    serverSynced: false,
    watchTicks: 0
  };

  function isValidIpv4Host(value) {
    return /^\d{1,3}(?:\.\d{1,3}){3}$/.test(String(value || '').replace(/^\s+|\s+$/g, ''));
  }

  function readBundledPcBridge() {
    var metas, i, name, content;
    try {
      metas = doc && doc.querySelectorAll ? doc.querySelectorAll('meta') : [];
      for (i = 0; i < metas.length; i += 1) {
        name = metas[i] && metas[i].getAttribute ? metas[i].getAttribute('name') : '';
        if (name === 'neon-pc-bridge') {
          content = String(metas[i].getAttribute('content') || '').replace(/^\s+|\s+$/g, '');
          if (isValidIpv4Host(content)) return content;
        }
      }
    } catch (e) {}
    return '';
  }

  function hasManualPcBridgeHost() {
    var saved = '';
    try {
      if (window.localStorage) saved = String(window.localStorage.getItem(PC_BRIDGE_KEY) || '').replace(/^\s+|\s+$/g, '');
    } catch (e) {}
    return isValidIpv4Host(saved);
  }

  function getPcBridgeHost() {
    var saved = '';
    try {
      if (window.localStorage) saved = String(window.localStorage.getItem(PC_BRIDGE_KEY) || '').replace(/^\s+|\s+$/g, '');
    } catch (e) {}
    if (isValidIpv4Host(saved)) return saved;
    return readBundledPcBridge();
  }

  function savePcBridgeHost(ip) {
    var clean = String(ip || '').replace(/^\s+|\s+$/g, '');
    try {
      if (window.localStorage) {
        if (clean) window.localStorage.setItem(PC_BRIDGE_KEY, clean);
        else window.localStorage.removeItem(PC_BRIDGE_KEY);
      }
    } catch (e) {}
  }

  function canProbeAnyClockBridge() {
    return hasTizenServiceApi() || !!getPcBridgeHost();
  }

  function hasTizenServiceApi() {
    return !!(window.tizen && (window.tizen.application || window.tizen.ApplicationControl));
  }

  function tizenPlatformMajor() {
    try {
      if (window.tizen && window.tizen.systeminfo && window.tizen.systeminfo.getCapability) {
        return parseInt(window.tizen.systeminfo.getCapability('http://tizen.org/feature/platform.version'), 10) || 0;
      }
    } catch (e) {}
    return 0;
  }

  function probeTvIp() {
    var props, i;
    try {
      if (window.webapis && window.webapis.network && window.webapis.network.getIp) {
        var wip = window.webapis.network.getIp();
        if (wip && typeof wip === 'string' && wip !== '0.0.0.0' && wip.indexOf('127.') !== 0) {
          clockProxyState.tvIp = wip.replace(/^\s+|\s+$/g, '');
        }
      }
    } catch (e) {}
    try {
      if (window.tizen && window.tizen.systeminfo && window.tizen.systeminfo.getPropertyValue) {
        props = ['ETHERNET_NETWORK', 'WIFI_NETWORK', 'NETWORK'];
        for (i = 0; i < props.length; i += 1) {
          try {
            window.tizen.systeminfo.getPropertyValue(props[i], function (val) {
              var ip = val && (val.ipAddress || val.ip);
              if (ip && ip !== '0.0.0.0' && String(ip).indexOf('127.') !== 0) {
                clockProxyState.tvIp = String(ip).replace(/^\s+|\s+$/g, '');
              }
            }, function () {});
          } catch (e) {}
        }
      }
    } catch (e) {}
  }

  function getClockProxyCandidates(opts) {
    var raw = [];
    var out = [];
    var pcIp = getPcBridgeHost();
    var includePc = !opts || opts.includePc !== false;
    var i;
    if (hasTizenServiceApi()) {
      raw.push('http://127.0.0.1:' + CLOCK_PROXY_PORT);
      raw.push('http://localhost:' + CLOCK_PROXY_PORT);
      if (clockProxyState.tvIp && (!opts || opts.includeTvIp !== false)) {
        raw.push('http://' + clockProxyState.tvIp + ':' + CLOCK_PROXY_PORT);
      }
    }
    if (pcIp && includePc) raw.push('http://' + pcIp + ':' + CLOCK_PROXY_PORT);
    for (i = 0; i < raw.length; i += 1) {
      if (out.indexOf(raw[i]) < 0) out.push(raw[i]);
    }
    return out;
  }

  function withWrtService(cb) {
    var done = false;
    var s;
    var sFile;
    function finish() {
      if (done) return;
      done = true;
      cb();
    }
    if (tizenPlatformMajor() < 9 || window.__neonWrtService || clockProxyState.wrtTried) {
      cb();
      return;
    }
    clockProxyState.wrtTried = true;
    try {
      on(window, 'neon-wrt-ready', function () {
        if (!done) {
          finish();
          return;
        }
        if (shouldUseBypass() && !isClockBypassed() && typeof window.__neonWrtService === 'function') {
          try {
            var lateP = new window.__neonWrtService(CLOCK_PROXY_SVC).start();
            if (lateP && typeof lateP.then === 'function') {
              lateP.then(
                function () { clockProxyState.launchDiag = 'wrt_late_ok'; },
                function () {}
              );
            }
          } catch (e) {}
        }
      });
    } catch (e) {}
    setTimeout(function () {
      if (done || window.__neonWrtService) return;
      try {
        if (doc && doc.createElement && (doc.head || doc.body)) {
          sFile = doc.createElement('script');
          sFile.type = 'module';
          sFile.src = 'wrt-service.js';
          sFile.onerror = function () {};
          (doc.head || doc.body).appendChild(sFile);
        }
      } catch (e) {}
    }, 150);
    setTimeout(finish, 450);
    try {
      if (doc && doc.createElement && (doc.head || doc.body)) {
        s = doc.createElement('script');
        s.type = 'module';
        s.text = WRT_INLINE_CODE;
        s.onerror = function () {};
        (doc.head || doc.body).appendChild(s);
        return;
      }
    } catch (e) {}
    finish();
  }

  function launchClockProxyLegacy() {
    var AppCtrl;
    if (!hasTizenServiceApi()) {
      clockProxyState.launchDiag = 'no_tizen_app';
      return;
    }
    AppCtrl = window.tizen.ApplicationControl || (window.tizen.application && window.tizen.application.ApplicationControl);
    if (AppCtrl && window.tizen.application && window.tizen.application.launchAppControl) {
      try {
        window.tizen.application.launchAppControl(
          new AppCtrl('http://tizen.org/appcontrol/operation/service'),
          CLOCK_PROXY_SVC,
          function () { clockProxyState.launchDiag = 'appctl_ok'; },
          function (err) {
            clockProxyState.launchDiag = 'appctl_err_' + ((err && (err.name || err.message)) || 'fail');
            try {
              if (window.tizen.application.launch) {
                window.tizen.application.launch(
                  CLOCK_PROXY_SVC,
                  function () { clockProxyState.launchDiag = 'launch_ok'; },
                  function (e2) { clockProxyState.launchDiag = 'launch_err_' + ((e2 && (e2.name || e2.message)) || 'fail'); }
                );
              }
            } catch (e) {}
          }
        );
        return;
      } catch (e) { clockProxyState.launchDiag = 'appctl_exc'; }
    }
    try {
      if (window.tizen.application && window.tizen.application.launch) {
        window.tizen.application.launch(
          CLOCK_PROXY_SVC,
          function () { clockProxyState.launchDiag = 'launch_ok'; },
          function (e3) { clockProxyState.launchDiag = 'launch_err_' + ((e3 && (e3.name || e3.message)) || 'fail'); }
        );
      }
    } catch (e) { clockProxyState.launchDiag = 'launch_exc'; }
  }

  function launchClockProxyService(forceRetry) {
    if (!shouldUseBypass() || !hasTizenServiceApi()) return;
    if (clockProxyState.launched && !forceRetry) return;
    clockProxyState.launched = true;
    clockProxyState.diag = 'запуск локального прокси…';
    probeTvIp();
    withWrtService(function () {
      var WrtSvc = window.__neonWrtService;
      if (typeof WrtSvc === 'function') {
        try {
          var p = new WrtSvc(CLOCK_PROXY_SVC).start();
          if (p && typeof p.then === 'function') {
            p.then(
              function () {
                clockProxyState.launchDiag = 'wrt_ok';
                if (forceRetry && !isClockBypassed()) launchClockProxyLegacy();
              },
              function (e) {
                clockProxyState.launchDiag = 'wrt_err_' + ((e && (e.message || e.name)) || 'fail');
                launchClockProxyLegacy();
              }
            );
          } else {
            clockProxyState.launchDiag = 'wrt_sync';
            if (forceRetry && !isClockBypassed()) launchClockProxyLegacy();
          }
          return;
        } catch (e) {
          clockProxyState.launchDiag = 'wrt_exc';
        }
      }
      launchClockProxyLegacy();
    });
  }

  function pingClockProxyCandidate(baseUrl, cb) {
    var xhr = new XMLHttpRequest();
    var done = false;
    var isLoopback = baseUrl.indexOf('127.0.0.1') >= 0 || baseUrl.indexOf('localhost') >= 0;
    function finish(ok, data) {
      if (done) return;
      done = true;
      cb(ok, data || null);
    }
    try {
      xhr.open('GET', baseUrl + '/__ping', true);
      xhr.timeout = isLoopback ? 350 : 600;
      xhr.onreadystatechange = function () {
        var parsed;
        if (xhr.readyState !== 4) return;
        if (xhr.status === 200) {
          try { parsed = JSON.parse(xhr.responseText); } catch (e) { parsed = null; }
          if (parsed && parsed.ok) return finish(true, parsed);
        }
        finish(false, null);
      };
      xhr.onerror = function () { finish(false, null); };
      xhr.ontimeout = function () { finish(false, null); };
      xhr.send();
    } catch (e) {
      finish(false, null);
    }
  }

  function updateClockBridgeButtons() {
    var showBtn = shouldUseBypass();
    var pcIp = getPcBridgeHost();
    var label = pcIp ? ('Мост ПК: ' + pcIp) : 'Мост через ПК (IP)';
    if ($('homePcBridge')) {
      setHidden($('homePcBridge'), !showBtn);
      $('homePcBridge').textContent = label;
    }
    if ($('aboutPcBridge')) {
      setHidden($('aboutPcBridge'), !showBtn);
      $('aboutPcBridge').textContent = label;
    }
  }

  function openPcBridgePrompt() {
    var current = getPcBridgeHost() || '192.168.1.';
    inputOverlay('num', {
      title: 'IP компьютера с программой NEON (Мост 1970)',
      value: current,
      maxLength: 15,
      allowDot: true,
      onDone: function (value) {
        var clean = String(value || '').replace(/^\s+|\s+$/g, '');
        if (!isValidIpv4Host(clean)) {
          toast('Введите IPv4 вида 192.168.1.50', 3200);
          return;
        }
        savePcBridgeHost(clean);
        updateClockBridgeButtons();
        toast('Проверяем мост на ПК ' + clean + ':18767…', 2500);
        probeClockProxy(function (ok) {
          if (ok) {
            state.clockSynced = true;
            toast('Мост через ПК подключён — загружаем Belet…', 3000);
            recoverNetworkScreens(true);
            return;
          }
          toast('ПК ' + clean + ':18767 не ответил — включите «Мост Belet (1970 ТВ)» в Neon на ПК', 4200);
          if (state.screen === 'about') runHealthCheck();
        });
      }
    });
  }

  function probeClockProxy(callback, options) {
    var doneCb = typeof callback === 'function' ? callback : function () {};
    var opts = options || {};
    var candidates;
    var index = 0;
    updateClockBridgeButtons();
    if (!shouldUseBypass()) {
      if (window.Belet && Belet.setClockProxy) Belet.setClockProxy('');
      clockProxyState.base = '';
      clockProxyState.diag = isBypassModeDisabled()
        ? 'отключён (режим без обхода)'
        : 'не требуется (часы ТВ в норме)';
      doneCb(false);
      return;
    }
    if (!canProbeAnyClockBridge()) {
      clockProxyState.diag = hasTizenServiceApi()
        ? 'ожидание сервиса'
        : 'локальный сервис недоступен — укажите IP ПК-моста в Neon';
      doneCb(false);
      return;
    }
    if (clockProxyState.probing) {
      doneCb(isClockBypassed());
      return;
    }
    if (hasTizenServiceApi()) {
      launchClockProxyService(!!opts.forceLaunch);
      probeTvIp();
    } else {
      clockProxyState.diag = 'подключение к ПК-мосту…';
    }
    clockProxyState.probing = true;
    candidates = getClockProxyCandidates(opts);
    function tryNext() {
      var candidate;
      if (index >= candidates.length) {
        clockProxyState.probing = false;
        clockProxyState.diag = 'не отвечает' +
          (clockProxyState.launchDiag ? ' (' + clockProxyState.launchDiag + ')' : '') +
          (getPcBridgeHost() ? ' · ПК ' + getPcBridgeHost() : '');
        doneCb(false);
        return;
      }
      candidate = candidates[index];
      index += 1;
      pingClockProxyCandidate(candidate, function (ok, data) {
        var wasBypassed = isClockBypassed();
        var modeLabel;
        if (!ok) return tryNext();
        clockProxyState.probing = false;
        clockProxyState.base = candidate;
        modeLabel = (data && data.mode === 'pc-bridge') ? 'ПК-мост ' : '';
        clockProxyState.diag = 'активен (' + modeLabel + candidate + ')';
        if (window.Belet && Belet.setClockProxy) Belet.setClockProxy(candidate);
        if (data && data.serverTimeMs && data.serverTimeMs >= 1704067200000) {
          clockProxyState.serverOffsetMs = data.serverTimeMs - Date.now();
          clockProxyState.serverSynced = true;
        }
        tickClock();
        updateClockBridgeButtons();
        doneCb(true, !wasBypassed);
      });
    }
    tryNext();
  }

  function clockHelpText() {
    var tz = readTizenClock();
    var tzValid = !!(tz && tz.getFullYear && tz.getFullYear() >= 2026);
    if (!isClockInvalid() && isBypassModeForced()) {
      return 'Включён режим обхода SSL — ожидаем запуск локального прокси Belet или ПК-моста.';
    }
    if (tzValid) {
      return 'В настройках ТВ время задано (' + formatDateObj(tz) + '), но системное время браузера осталось ' +
        clockDateText() + ' — из-за этого прямые HTTPS-запросы заблокированы. ' +
        'Запущен режим ожидания обхода часов. Для системной синхронизации: Настройки ТВ → Общие → Диспетчер системы → Время → Часы → «Авто».';
    }
    return 'Время на ТВ не настроено (' + clockDateText() + ') — прямые HTTPS-запросы отключены, ожидаем запуск локального обхода SSL или настройку часов в меню ТВ.';
  }

  var deviceRegistration = { state: 'не отправлена', model: '' };

  function registerDevice() {
    if (isClockBlockingHttps()) {
      deviceRegistration.state = 'ожидает обхода SSL (без прямых HTTPS)';
      scheduleClockWatch();
      return;
    }
    deviceRegistration.state = 'отправляется…';
    Belet.registerDevice(function (err) {
      deviceRegistration.model = Belet.model();
      if (err) {
        if (err.kind === 'network' && isClockBlockingHttps()) {
          deviceRegistration.state = 'ожидает синхронизации времени (HTTP 0)';
        } else if (err.kind === 'network') {
          deviceRegistration.state = 'нет ответа от сервера (HTTP 0)';
        } else {
          deviceRegistration.state = 'ошибка: ' + describeError(err);
        }
        return;
      }
      deviceRegistration.state = 'сервер принял регистрацию телевизора';
    });
  }

  function describeError(err) {
    if (!err) return 'Неизвестная ошибка.';
    if (err.kind === 'network') {
      if (isClockBlockingHttps()) return clockHelpText();
      return 'Нет ответа от серверов Belet (HTTP 0). Проверьте подключение к сети и время на телевизоре. ' +
        'Если сеть работает, встроенный браузер ТВ мог заблокировать запрос политикой CORS.';
    }
    if (err.kind === 'auth') return 'Сессия истекла. Войдите в аккаунт снова.';
    if (err.kind === 'payment') return 'Belet сообщает, что для этого фильма нужна подписка или оплата (HTTP 402). Проверьте условия в аккаунте.';
    if (err.kind === 'blocked') return 'Belet отклонил запрос (HTTP 403). Возможно, ограничение аккаунта или региона.';
    if (err.kind === 'missing') return 'Сервис не нашёл запрошенные данные (HTTP 404).';
    if (err.kind === 'format') return 'Сервис вернул неожиданный ответ. Возможно, изменилась версия API.';
    if (err.kind === 'empty') return 'Сервис не предоставил видеопоток для этого фильма.';
    return 'Сервис Belet ответил ошибкой' + (err.http ? ' HTTP ' + err.http : '') + (err.message ? ': ' + err.message : '.');
  }

  function networkConnectionType() {
    try {
      if (window.webapis && window.webapis.network && window.webapis.network.getActiveConnectionType) {
        return window.webapis.network.getActiveConnectionType();
      }
    } catch (e) {}
    return null;
  }

  function isNetworkReady() {
    var connType = networkConnectionType();
    if (connType === 0) return false;
    if (typeof window.navigator !== 'undefined' && window.navigator.onLine === false) return false;
    return true;
  }

  function networkLabel() {
    var connType = networkConnectionType();
    if (connType === 0) return 'нет подключения (Tizen)';
    if (connType === 1) return 'Wi-Fi';
    if (connType === 2) return 'мобильная сеть';
    if (connType === 3) return 'Ethernet';
    if (typeof window.navigator !== 'undefined' && window.navigator.onLine === false) return 'офлайн';
    return 'подключена';
  }

  function clearClockSyncTimer() {
    if (state.clockSyncTimer) {
      clearTimeout(state.clockSyncTimer);
      state.clockSyncTimer = null;
    }
  }

  function scheduleClockWatch() {
    clearClockSyncTimer();
    if (!shouldUseBypass() || isClockBypassed()) {
      state.clockSynced = true;
      return;
    }
    state.clockSynced = false;
    state.clockSyncTimer = setTimeout(function () {
      state.clockSyncTimer = null;
      tickClock();
      if (!isClockBypassed() && shouldUseBypass()) {
        clockProxyState.watchTicks += 1;
        probeClockProxy(function (ok, newlyActivated) {
          if (ok && newlyActivated) {
            state.clockSynced = true;
            toast('Запущен обход SSL — подключаемся к Belet…', 3000);
            recoverNetworkScreens(true);
            return;
          }
          if (!isClockBypassed() && shouldUseBypass()) scheduleClockWatch();
        }, {
          forceLaunch: hasTizenServiceApi() && (clockProxyState.watchTicks % 2 === 0)
        });
      }
    }, 2000);
  }

  function clearBootTimer() {
    if (state.bootTimer) {
      clearTimeout(state.bootTimer);
      state.bootTimer = null;
    }
  }

  function clearHomeTimer() {
    if (state.homeRetryTimer) {
      clearTimeout(state.homeRetryTimer);
      state.homeRetryTimer = null;
    }
  }

  function clearProfileTimer() {
    if (state.profileRetryTimer) {
      clearTimeout(state.profileRetryTimer);
      state.profileRetryTimer = null;
    }
  }

  function clearOtpTimer() {
    if (state.login.otpTimer) {
      clearTimeout(state.login.otpTimer);
      state.login.otpTimer = null;
    }
  }

  function resetCatalogueState() {
    state.homeGen += 1;
    clearHomeTimer();
    clearProfileTimer();
    state.homeLoaded = false;
    state.home = { groups: [], hero: null, items: [] };
    state.categories = [];
    state.categoriesLoaded = false;
    state.browse = { mode: 'category', id: null, title: '', items: [], offset: 0, loading: false, done: false };
    state.search = { text: '', items: [], page: 1, loading: false, done: false };
    state.detail = { movie: null, seasons: [], seasonId: null, episodes: [], loadingEpisodes: false };
    clear($('rows'));
    clear($('browseGrid'));
    clear($('searchGrid'));
  }

  function handleAuthError(err) {
    if (!err || err.kind !== 'auth') return false;
    if (state.login.loggingOut || state.screen === 'login') return true;
    resetCatalogueState();
    showLogin();
    toast('Сессия истекла — войдите снова', 4200);
    return true;
  }

  /* ── login ────────────────────────────────────────────────────────── */

  function focusLoginStep() {
    var target = $('phoneDisplay');
    if (!hasClass($('loginQr'), 'hidden')) target = $('qrRefresh');
    else if (!hasClass($('loginCode'), 'hidden')) target = $('codeDisplay');
    state.mainFocus = target;
    setTimeout(function () {
      if (state.screen === 'login' && !activeOverlay() && target && isVisible(target)) target.focus();
    }, 40);
  }

  function showLogin() {
    stopQr();
    clearOtpTimer();
    clearHomeTimer();
    clearProfileTimer();
    cancelPlayback(true);
    state.input = null;
    state.dialogActions = [];
    setHidden($('dialog'), true);
    setHidden($('numpad'), true);
    setHidden($('keyboard'), true);
    state.login.phone = '';
    state.login.code = '';
    state.login.tempToken = '';
    state.login.methods = [];
    state.login.methodIndex = 0;
    state.login.busy = false;
    state.login.otpFallback = false;
    state.login.generation += 1;
    screen('login');
    state.previous = 'login';
    state.mainFocus = $('phoneDisplay');
    $('loginPhone').classList.remove('hidden');
    $('loginCode').classList.add('hidden');
    $('loginQr').classList.add('hidden');
    $('loginTitle').textContent = 'Вход в Belet';
    $('loginHint').textContent = 'Введите номер телефона, привязанный к аккаунту Belet.';
    renderCodeValue();
    updatePhoneDisplay();
    loadOtpMethods(0);
    setTimeout(function () {
      if (state.screen === 'login' && !activeOverlay() && !hasClass($('loginPhone'), 'hidden')) {
        $('phoneDisplay').focus();
      }
    }, 40);
  }

  function updatePhoneDisplay() {
    var value = state.login.phone;
    var node = $('phoneValue');
    if (value) {
      node.textContent = '+' + value;
      node.classList.remove('placeholder');
    } else {
      node.textContent = 'Нажмите OK, чтобы ввести номер';
      node.classList.add('placeholder');
    }
    $('sendCode').disabled = !(value.length >= 8 && state.login.methods.length && !state.login.busy);
  }

  function fallbackOtpMethods() {
    return [{ name: 'SMS (резерв)', type: 2 }, { name: 'Звонок (резерв)', type: 1 }];
  }

  function loadOtpMethods(attempt) {
    var retryAttempt = typeof attempt === 'number' ? attempt : 0;
    var gen = state.login.generation;
    var select = $('methodValue');
    clearOtpTimer();
    state.login.otpFallback = false;
    if (isClockBlockingHttps()) {
      state.login.methods = fallbackOtpMethods();
      state.login.otpFallback = true;
      state.login.methodIndex = 0;
      select.textContent = state.login.methods[0].name;
      updatePhoneDisplay();
      scheduleClockWatch();
      return;
    }
    if (!state.login.methods.length) {
      select.textContent = retryAttempt ? 'Повторяем запрос…' : 'Загружаем способы…';
    }
    Belet.otpMethods(function (err, methods) {
      var isNetwork, maxRetries, delays, delay;
      if (gen !== state.login.generation || state.screen !== 'login') return;
      state.login.methods = [];
      if (err) {
        isNetwork = err.kind === 'network';
        maxRetries = isNetwork ? 3 : 1;
        delays = [1200, 2500, 5000];
        if (isClockBlockingHttps()) scheduleClockWatch();
        if (retryAttempt === 0 && !Belet.hasSession() && Belet.resetDevice) Belet.resetDevice();
        state.login.methods = fallbackOtpMethods();
        state.login.otpFallback = true;
        state.login.methodIndex = 0;
        select.textContent = state.login.methods[0].name;
        updatePhoneDisplay();
        if (retryAttempt < maxRetries) {
          delay = isNetwork ? delays[retryAttempt] : 900;
          state.login.otpTimer = setTimeout(function () {
            state.login.otpTimer = null;
            if (gen === state.login.generation && state.screen === 'login') loadOtpMethods(retryAttempt + 1);
          }, delay);
        }
        return;
      }
      methods.forEach(function (item) {
        if (item && typeof item.type === 'number') state.login.methods.push({ name: item.name || ('Способ ' + item.type), type: item.type });
      });
      if (!state.login.methods.length) state.login.methods = [{ name: 'SMS', type: 1 }];
      state.login.methodIndex = Math.min(state.login.methodIndex, state.login.methods.length - 1);
      select.textContent = state.login.methods[state.login.methodIndex].name;
      updatePhoneDisplay();
    });
  }

  function cycleMethod(step) {
    var methods = state.login.methods;
    if (!methods.length) return;
    state.login.methodIndex = (state.login.methodIndex + step + methods.length) % methods.length;
    $('methodValue').textContent = methods[state.login.methodIndex].name;
  }

  function editPhone() {
    inputOverlay('num', {
      title: 'Номер телефона',
      value: state.login.phone,
      maxLength: 15,
      onDone: function (value) {
        state.login.phone = value.replace(/\D/g, '');
        updatePhoneDisplay();
        if (state.login.phone.length < 8) toast('Введите номер с кодом страны, например 99312345678');
      }
    });
  }

  function editCode() {
    inputOverlay('num', {
      title: 'Код подтверждения',
      value: state.login.code,
      maxLength: 10,
      onDone: function (value) {
        state.login.code = value.replace(/\D/g, '');
        renderCodeValue();
        verifyCode();
      }
    });
  }

  function renderCodeValue() {
    var node = $('codeValue');
    if (state.login.code) {
      node.textContent = state.login.code;
      node.classList.remove('placeholder');
    } else {
      node.textContent = 'Нажмите OK, чтобы ввести код';
      node.classList.add('placeholder');
    }
  }

  function sendCode(silent) {
    var phone = state.login.phone;
    var method = state.login.methods[state.login.methodIndex];
    if (state.login.busy) return;
    if (!(phone.length >= 8 && phone.length <= 15)) {
      toast('Введите номер телефона с кодом страны');
      return;
    }
    if (!method) {
      toast('Способ получения кода недоступен. Проверьте сеть.');
      return;
    }
    if (isClockBlockingHttps()) {
      toast('Ожидаем запуск обхода SSL — попробуйте через пару секунд.', 3000);
      scheduleClockWatch();
      if (canProbeAnyClockBridge()) {
        probeClockProxy(function (ok) {
          if (ok) {
            state.clockSynced = true;
            recoverNetworkScreens(true);
          }
        });
      }
      return;
    }
    state.login.busy = true;
    $('sendCode').disabled = true;
    if (!silent) toast('Запрашиваем код…', 1600);
    Belet.signIn(phone, method.type, function (err, data) {
      state.login.busy = false;
      updatePhoneDisplay();
      if (err) {
        if (err.http === 402) {
          dialog('Нужна подписка', describeError(err), [{ label: 'Понятно', primary: true }]);
        } else {
          dialog('Не удалось отправить код', describeError(err) + '\n\nПроверьте номер и способ получения кода.', [
            { label: 'Повторить', primary: true, onSelect: function () { sendCode(); } },
            { label: 'Закрыть' }
          ]);
        }
        return;
      }
      state.login.tempToken = data.token;
      state.login.code = '';
      renderCodeValue();
      $('loginTitle').textContent = 'Введите код';
      $('loginHint').textContent = 'Код отправлен на номер +' + phone + '. Введите его на телевизоре.';
      $('loginPhone').classList.add('hidden');
      $('loginCode').classList.remove('hidden');
      state.mainFocus = $('codeDisplay');
      $('codeDisplay').focus();
      toast('Код отправлен. Введите его на телевизоре.', 4000);
    });
  }

  function verifyCode() {
    if (state.login.busy) return;
    if (!state.login.tempToken) {
      toast('Сначала получите код');
      return;
    }
    if (state.login.code.length < 4) {
      toast('Введите код из сообщения');
      return;
    }
    state.login.busy = true;
    var code = state.login.code;
    Belet.checkCode(code, state.login.tempToken, function (err) {
      state.login.busy = false;
      if (err) {
        dialog('Код не принят', describeError(err), [
          { label: 'Ввести заново', primary: true, onSelect: function () { state.login.code = ''; renderCodeValue(); editCode(); } },
          { label: 'Закрыть' }
        ]);
        return;
      }
      state.login.tempToken = '';
      state.login.code = '';
      state.login.phone = '';
      registerDevice();
      $('loginTitle').textContent = 'Вход в Belet';
      $('loginHint').textContent = 'Введите номер телефона, привязанный к аккаунту Belet.';
      $('loginCode').classList.add('hidden');
      $('loginPhone').classList.remove('hidden');
      state.mainFocus = $('phoneDisplay');
      loadHome(true);
    });
  }

  function stopQr() {
    state.qr.generation += 1;
    if (state.qr.timer) {
      clearTimeout(state.qr.timer);
      state.qr.timer = null;
    }
    state.qr.code = null;
  }

  function startQr(attempt) {
    var retryAttempt = typeof attempt === 'number' ? attempt : 0;
    stopQr();
    clearOtpTimer();
    var generation = state.qr.generation;
    var started = Date.now();
    $('loginPhone').classList.add('hidden');
    $('loginCode').classList.add('hidden');
    $('loginQr').classList.remove('hidden');
    $('loginTitle').textContent = 'Вход по QR-коду';
    $('loginHint').textContent = 'Отсканируйте код телефоном — телевизор войдёт автоматически.';
    if (retryAttempt === 0) $('qrImage').removeAttribute('src');
    if (isClockBlockingHttps()) {
      $('qrStatus').textContent = canProbeAnyClockBridge()
        ? ('Время ТВ не настроено (' + clockDateText() + ') — запускаем обход SSL без прямых HTTPS…')
        : clockHelpText();
      state.mainFocus = $('qrRefresh');
      setTimeout(function () {
        if (state.screen === 'login' && !activeOverlay() && !hasClass($('loginQr'), 'hidden')) {
          $('qrRefresh').focus();
        }
      }, 30);
      scheduleClockWatch();
      return;
    }
    $('qrStatus').textContent = retryAttempt ? 'Повторяем запрос QR-кода…' : 'Получаем QR-код…';
    state.mainFocus = $('qrRefresh');
    setTimeout(function () {
      if (state.screen === 'login' && !activeOverlay() && !hasClass($('loginQr'), 'hidden')) {
        $('qrRefresh').focus();
      }
    }, 30);

    Belet.qrCode(function (err, data) {
      var code, isNetwork, maxRetries, delays, delay;
      if (generation !== state.qr.generation || state.screen !== 'login') return;
      if (err || !data) {
        isNetwork = !!(err && err.kind === 'network');
        maxRetries = isNetwork ? 3 : 1;
        delays = [1200, 2500, 5000];
        if (retryAttempt === 0 && !Belet.hasSession() && Belet.resetDevice) Belet.resetDevice();
        if (isClockBlockingHttps()) scheduleClockWatch();
        if (retryAttempt < maxRetries) {
          delay = isNetwork ? delays[retryAttempt] : 900;
          $('qrStatus').textContent = isNetwork
            ? (isClockBlockingHttps()
              ? 'Время ТВ сброшено (' + clockDateText() + ') — HTTPS блокируется. Повторяем запрос…'
              : 'Нет связи с сервером (HTTP 0). Повторяем запрос QR-кода…')
            : 'Повторяем запрос QR-кода…';
          state.qr.timer = setTimeout(function () {
            state.qr.timer = null;
            if (generation === state.qr.generation && state.screen === 'login') startQr(retryAttempt + 1);
          }, delay);
          return;
        }
        $('qrStatus').textContent = (isNetwork && isClockBlockingHttps())
          ? clockHelpText()
          : ('Не удалось получить QR-код' +
            (err && err.http ? ' (HTTP ' + err.http + ')' : (isNetwork ? ' (HTTP 0)' : '')) +
            '. Проверьте сеть и обновите.');
        return;
      }
      code = data.code;
      state.qr.code = code;
      $('qrImage').src = data.image;
      $('qrStatus').textContent = 'Ожидаем подтверждение на телефоне…';

      function poll() {
        if (generation !== state.qr.generation) return;
        if (Date.now() - started > 180000) {
          stopQr();
          $('qrStatus').textContent = 'Время ожидания истекло. Обновите QR-код.';
          return;
        }
        Belet.qrCheck(code, function (checkErr, checkData) {
          if (generation !== state.qr.generation) return;
          if (!checkErr && checkData && checkData.token) {
            Belet.acceptQrSession(checkData);
            stopQr();
            $('qrStatus').textContent = 'Вход выполнен.';
            registerDevice();
            loadHome(true);
            return;
          }
          if (checkErr && (checkErr.kind === 'pending' || checkErr.http === 400)) {
            state.qr.timer = setTimeout(poll, 3000);
            return;
          }
          var status = checkErr ? checkErr.http : 0;
          if (!status || status === 408 || status === 429 || status >= 500) {
            $('qrStatus').textContent = 'Проверка QR: HTTP ' + status + '. Повторяем запрос…';
            state.qr.timer = setTimeout(poll, 5000);
            return;
          }
          $('qrStatus').textContent = 'Не удалось проверить QR-код (HTTP ' + status + '). Обновите его или войдите по SMS.';
        });
      }
      state.qr.timer = setTimeout(poll, 2500);
    });
  }

  /* ── catalogue rendering ──────────────────────────────────────────── */

  var IMAGE_BASE = 'https://img.belet.me/';
  var BRAND_IMAGE = 'belet-logo.png';

  function imageUrl(value) {
    var url;
    var resolved;
    if (typeof value !== 'string') return null;
    url = value.replace(/^\s+|\s+$/g, '');
    if (!url) return null;
    if (/^(https?:|data:|file:)/i.test(url)) resolved = url;
    else if (url.indexOf('//') === 0) resolved = 'https:' + url;
    else if (/^img\.belet\.me\//i.test(url)) resolved = 'https://' + url;
    else resolved = IMAGE_BASE + (url.charAt(0) === '/' ? url.slice(1) : url);
    if (window.Belet && Belet.proxyUrl && /^https?:/i.test(resolved)) {
      return Belet.proxyUrl(resolved);
    }
    return resolved;
  }

  function setContentImage(img, source) {
    var gen = (img._beletImgGen || 0) + 1;
    var retryCount = 0;
    img._beletImgGen = gen;
    if (img._beletRetryTimer) {
      clearTimeout(img._beletRetryTimer);
      img._beletRetryTimer = null;
    }

    function showBrandFallback() {
      if (img._beletImgGen !== gen) return;
      if (img._beletBrandFallback) {
        img.style.visibility = 'hidden';
        return;
      }
      img._beletBrandFallback = true;
      img.classList.add('brand-fallback');
      img.alt = 'Belet';
      img.src = BRAND_IMAGE;
    }

    function handleImageError() {
      if (img._beletImgGen !== gen) return;
      if (!img._beletBrandFallback && source && /^https?:\/\//i.test(source) && retryCount < 2) {
        var delay = retryCount === 0 ? 350 : 900;
        retryCount += 1;
        img._beletRetryTimer = setTimeout(function () {
          img._beletRetryTimer = null;
          if (img._beletImgGen !== gen || img._beletBrandFallback) return;
          if (img.removeAttribute) img.removeAttribute('src');
          img.src = source;
        }, delay);
        return;
      }
      showBrandFallback();
    }

    img._beletBrandFallback = false;
    img.classList.remove('brand-fallback');
    img.alt = '';
    img.style.visibility = '';
    try {
      img.referrerPolicy = 'no-referrer';
      if (img.setAttribute) img.setAttribute('referrerpolicy', 'no-referrer');
    } catch (e) {}
    img.onload = function () {
      if (img._beletImgGen !== gen) return;
      if (img._beletRetryTimer) {
        clearTimeout(img._beletRetryTimer);
        img._beletRetryTimer = null;
      }
    };
    img.onerror = handleImageError;
    source = imageUrl(source);
    if (source) img.src = source;
    else showBrandFallback();
  }

  function pickImage(images, kind, size) {
    var entry, sizes = size || ['medium', 'standard', 'default', 'high'];
    var order = kind === 'horizontal'
      ? ['horizontal_with_name', 'horizontal-with-name', 'horizontal_without_name', 'horizontal-without-name', 'vertical', 'vertical_without_name', 'vertical-without-name']
      : ['vertical', 'vertical_without_name', 'vertical-without-name', 'horizontal_without_name', 'horizontal-without-name', 'horizontal_with_name', 'horizontal-with-name'];
    var i, j, url;
    if (!images || typeof images !== 'object') return null;
    for (i = 0; i < order.length; i += 1) {
      entry = images[order[i]];
      if (!entry || typeof entry !== 'object') continue;
      for (j = 0; j < sizes.length; j += 1) {
        if (entry[sizes[j]] && typeof entry[sizes[j]].url === 'string') {
          url = imageUrl(entry[sizes[j]].url);
          if (url) return url;
        }
      }
    }
    if (images.name && typeof images.name.url === 'string') return imageUrl(images.name.url);
    return null;
  }

  function posterOf(item) {
    return pickImage(item && item.images, 'vertical', ['medium', 'standard', 'default', 'high']) ||
      (item && typeof item.image_url === 'string' ? imageUrl(item.image_url) : null) ||
      (item && typeof item.image === 'string' ? imageUrl(item.image) : null);
  }

  function backdropOf(item) {
    return pickImage(item && item.images, 'horizontal', ['high', 'default', 'medium', 'standard']) ||
      (item && typeof item.image_url === 'string' ? imageUrl(item.image_url) : null);
  }

  function itemsOf(group) {
    if (!group) return [];
    if (Array.isArray(group.items)) return group.items;
    if (Array.isArray(group.movies)) return group.movies;
    return [];
  }

  function isPlayable(item) {
    return item && typeof item.id === 'number' && typeof item.type_id === 'number' && item.type_id > 0 && item.type_id < 6;
  }

  function movieTitle(item) {
    return (item && (item.title || item.name || item.title_ru || item.title_tk)) || 'Без названия';
  }

  function cardNode(item, options) {
    options = options || {};
    var card = doc.createElement('button');
    var poster = el('div', 'poster');
    var image = posterOf(item);
    var name = el('span', 'name', movieTitle(item));
    var badge;
    var progress;
    var progressValue;
    var duration;
    card.className = 'card focusable' + (options.continueWatching ? ' continue' : '');
    card.setAttribute('type', 'button');
    var img = doc.createElement('img');
    setContentImage(img, image);
    poster.appendChild(img);
    if (item && item.age) {
      badge = el('span', 'badge', item.age + '+');
      poster.appendChild(badge);
    }
    if (options.continueWatching) {
      duration = parseDuration(item.duration) || parseDuration(item.episode_info && item.episode_info.duration);
      progressValue = duration ? Math.min(100, Math.round((Number(item.watch_time || 0) / duration) * 100)) : 0;
      progress = el('div', 'progress-mini');
      var fill = el('i');
      fill.style.width = Math.max(2, progressValue) + '%';
      progress.appendChild(fill);
      poster.appendChild(progress);
    }
    card.appendChild(poster);
    card.appendChild(name);
    card.onclick = function () { openDetail(item); };
    return card;
  }

  function renderRows(groups) {
    var rows = $('rows');
    var heroGroup = null;
    var heroItem = null;
    clear(rows);
    groups.forEach(function (group) {
      var items = itemsOf(group).filter(isPlayable);
      var row;
      var title;
      if (!items.length) return;
      if (!heroGroup && group.source === 'header') {
        heroGroup = group;
        heroItem = items[0];
        return;
      }
      title = el('h2', 'row-title', group.title || group.title_ru || group.title_tk || 'Подборка');
      row = el('div', 'row-track');
      items.forEach(function (item) {
        row.appendChild(cardNode(item, { continueWatching: group.source === 'continue_watching' }));
      });
      var block = el('div', 'row-block');
      block.appendChild(title);
      block.appendChild(row);
      rows.appendChild(block);
    });
    renderHero(heroItem || (groups.length ? itemsOf(groups[0])[0] : null));
    $('homeEmpty').classList.toggle('hidden', !!rows.children.length);
    updateClockBridgeButtons();
  }

  function renderHero(item) {
    var hero = $('hero');
    if (!item) {
      setHidden(hero, true);
      return;
    }
    var image = backdropOf(item) || posterOf(item);
    var meta = [];
    state.home.hero = item;
    setContentImage($('heroImage'), image);
    var duration = durationText(item.duration);
    var genres = genreNames(item.genres);
    if (item.age) meta.push(item.age + '+');
    if (item.rating_kp) meta.push('КП ' + item.rating_kp);
    if (duration) meta.push(duration);
    if (item.year) meta.push(item.year);
    if (genres) meta.push(genres);
    $('heroTitle').textContent = movieTitle(item);
    $('heroMeta').textContent = meta.join('  ·  ');
    $('heroText').textContent = item.description || '';
    setHidden(hero, false);
  }

  function loadHome(showSpinner, attempt) {
    var retryAttempt = typeof attempt === 'number' ? attempt : 0;
    var collected = [];
    var gen;
    stopQr();
    clearOtpTimer();
    clearHomeTimer();
    state.homeLoaded = false;
    state.homeGen += 1;
    gen = state.homeGen;
    screen('home');
    setHidden($('hero'), true);
    setHidden($('homeEmpty'), true);
    clear($('rows'));
    state.mainFocus = null;

    function focusReload() {
      state.mainFocus = $('homeReload');
      if (state.screen === 'home' && !activeOverlay() && $('homeReload')) {
        $('homeReload').focus();
      }
    }

    if (isClockBlockingHttps()) {
      $('homeEmpty').classList.remove('hidden');
      updateClockBridgeButtons();
      $('homeEmpty').querySelector('h2').textContent = 'Сбиты дата и время на телевизоре';
      $('homeEmpty').querySelector('p').textContent = canProbeAnyClockBridge()
        ? ('Время ТВ (' + clockDateText() + ') не настроено — прямые HTTPS не отправляются, запускаем локальный обход SSL…')
        : clockHelpText();
      focusReload();
      scheduleClockWatch();
      return;
    }
    if (showSpinner) toast('Загружаем каталог…', 1800);

    function fetchPage(offset, pagesLeft) {
      Belet.home(offset, 3, function (err, groups) {
        var delays, delay;
        if (gen !== state.homeGen || state.screen !== 'home') return;
        if (err) {
          if (handleAuthError(err)) return;
          if (!collected.length) {
            delays = [1500, 3000, 5000, 7000];
            $('homeEmpty').classList.remove('hidden');
            updateClockBridgeButtons();
            if (isClockBlockingHttps()) scheduleClockWatch();
            if (err.kind === 'network' && retryAttempt < delays.length) {
              delay = delays[retryAttempt];
              $('homeEmpty').querySelector('h2').textContent = isClockBlockingHttps()
                ? 'Сбиты дата и время на телевизоре'
                : 'Подключение к серверам Belet…';
              $('homeEmpty').querySelector('p').textContent = describeError(err) +
                '\nАвтоматический повтор через ' + Math.round(delay / 1000) + ' с…';
              focusReload();
              state.homeRetryTimer = setTimeout(function () {
                state.homeRetryTimer = null;
                if (gen === state.homeGen && state.screen === 'home' && !state.homeLoaded && Belet.hasSession()) {
                  loadHome(false, retryAttempt + 1);
                }
              }, delay);
              return;
            }
            $('homeEmpty').querySelector('h2').textContent = isClockBlockingHttps()
              ? 'Сбиты дата и время на телевизоре'
              : 'Каталог не загрузился';
            $('homeEmpty').querySelector('p').textContent = describeError(err);
            focusReload();
            return;
          }
          finish();
          return;
        }
        collected = collected.concat(groups);
        // Paint each page as it arrives so the TV shows content instead of a spinner.
        if (groups.length) {
          state.home.groups = collected;
          renderRows(collected);
        }
        if (groups.length && pagesLeft > 1) return fetchPage(offset + 3, pagesLeft - 1);
        finish();
      });
    }

    function finish() {
      clearHomeTimer();
      state.home.groups = collected;
      state.homeLoaded = true;
      $('homeEmpty').querySelector('h2').textContent = 'Каталог пуст';
      $('homeEmpty').querySelector('p').textContent = 'Сервис ответил, но не вернул ни одного раздела. Проверьте подписку и повторите.';
      renderRows(collected);
      if (deviceRegistration.state !== 'сервер принял регистрацию телевизора' &&
          deviceRegistration.state !== 'отправляется…') {
        registerDevice();
      }
      var first = $('rows').querySelector('.card') || ($('hero') && !hasClass($('hero'), 'hidden') ? $('heroPlay') : null) || $('homeReload');
      state.mainFocus = first;
      if (first) {
        first.focus();
        reveal(first);
      }
    }

    fetchPage(0, 3);
  }

  /* ── browse (categories / favourites) ─────────────────────────────── */

  function openBrowse() {
    screen('browse');
    $('browseTitle').textContent = 'Каталог';
    renderCategories();
    if (state.browse.mode !== 'category' || !state.browse.id) {
      state.browse.mode = 'category';
      state.browse.id = state.categories.length ? state.categories[0].id : 1;
      state.browse.title = state.categories.length ? state.categories[0].name : 'Фильмы';
      state.browse.items = [];
      state.browse.done = false;
    }
    loadCategory(state.browse.id, true);
  }

  function renderCategories() {
    var host = $('browseChips');
    clear(host);
    state.categories.forEach(function (category) {
      var chip = el('button', 'chip focusable', category.name || 'Раздел');
      chip.onclick = function () { loadCategory(category.id, true); };
      chip.setAttribute('data-category', category.id);
      host.appendChild(chip);
    });
  }

  function markChips() {
    var chips = $('browseChips').querySelectorAll('.chip');
    var i;
    for (i = 0; i < chips.length; i += 1) {
      chips[i].classList.toggle('active', Number(chips[i].getAttribute('data-category')) === Number(state.browse.id));
    }
  }

  function loadCategories(callback) {
    if (state.categoriesLoaded && state.categories.length) {
      if (callback) callback();
      return;
    }
    Belet.categories(function (err, list) {
      if (err) {
        if (!handleAuthError(err)) toast('Разделы каталога недоступны: ' + describeError(err), 4000);
        if (callback) callback();
        return;
      }
      state.categories = [];
      (list || []).forEach(function (category) {
        if (category && typeof category.id === 'number' && category.name) state.categories.push(category);
      });
      state.categoriesLoaded = true;
      renderCategories();
      if (callback) callback();
    });
  }

  function loadCategory(id, reset) {
    var browse = state.browse;
    if (reset) {
      browse.mode = 'category';
      browse.id = id;
      browse.title = '';
      browse.items = [];
      browse.offset = 0;
      browse.done = false;
      $('browseGrid').innerHTML = '';
      $('browseEmpty').classList.add('hidden');
      $('browseMore').classList.add('hidden');
      state.categories.forEach(function (category) {
        if (category.id === id) browse.title = category.name;
      });
      $('browseTitle').textContent = browse.title || 'Каталог';
      markChips();
    }
    if (browse.loading || browse.done) return;
    browse.loading = true;
    Belet.category(id, 6, browse.offset, function (err, page) {
      browse.loading = false;
      if (err) {
        if (handleAuthError(err)) return;
        $('browseEmpty').classList.remove('hidden');
        $('browseEmpty').querySelector('h2').textContent = 'Не удалось загрузить раздел';
        $('browseEmpty').querySelector('p').textContent = describeError(err);
        return;
      }
      var items = ((page && page.items) || []).filter(isPlayable);
      browse.offset += (page && page.rows) || 0;
      if (!items.length || !(page && page.rows)) browse.done = true;
      renderGrid($('browseGrid'), items, { append: true });
      browse.items = browse.items.concat(items);
      $('browseEmpty').classList.toggle('hidden', !!browse.items.length);
      setHidden($('browseMore'), browse.done || !browse.items.length);
      if (!browse.items.length) return;
      if (!doc.activeElement || doc.activeElement === doc.body) {
        focusFirst($('browseGrid'));
      }
    });
  }

  function renderGrid(host, items, options) {
    options = options || {};
    if (!options.append) clear(host);
    items.forEach(function (item) {
      host.appendChild(cardNode(item, {}));
    });
  }

  function openFavorites() {
    screen('browse');
    state.browse.mode = 'favorites';
    state.browse.title = 'Избранное';
    state.browse.items = [];
    state.browse.offset = 0;
    state.browse.done = true;
    $('browseTitle').textContent = 'Избранное';
    $('browseChips').innerHTML = '';
    $('browseGrid').innerHTML = '';
    $('browseMore').classList.add('hidden');
    $('browseEmpty').classList.add('hidden');
    toast('Загружаем избранное…', 1600);
    Belet.favorites(function (err, list) {
      if (err) {
        if (handleAuthError(err)) return;
        $('browseEmpty').classList.remove('hidden');
        $('browseEmpty').querySelector('h2').textContent = 'Избранное недоступно';
        $('browseEmpty').querySelector('p').textContent = describeError(err);
        return;
      }
      var items = (list || []).filter(isPlayable);
      state.browse.items = items;
      renderGrid($('browseGrid'), items, {});
      $('browseEmpty').classList.toggle('hidden', !!items.length);
      if (!items.length) {
        $('browseEmpty').classList.remove('hidden');
        $('browseEmpty').querySelector('h2').textContent = 'Избранное пусто';
        $('browseEmpty').querySelector('p').textContent = 'Добавляйте фильмы кнопкой «В избранное» на странице фильма.';
      }
      focusFirst($('browseGrid'));
    });
  }

  /* ── search ───────────────────────────────────────────────────────── */

  function openSearch() {
    screen('search');
    state.mainFocus = $('searchField');
    setTimeout(function () { $('searchField').focus(); }, 30);
    renderSearchResults(state.search.items);
  }

  function editSearch() {
    inputOverlay('text', {
      title: 'Поиск по каталогу Belet',
      value: state.search.text,
      maxLength: 40,
      onDone: function (value) {
        state.search.text = value;
        $('searchValue').textContent = value;
        $('searchValue').classList.remove('placeholder');
        runSearch();
      }
    });
  }

  function runSearch() {
    var text = state.search.text;
    if (!text) return;
    state.search.page = 1;
    state.search.items = [];
    state.search.done = false;
    $('searchGrid').innerHTML = '';
    $('searchEmpty').classList.add('hidden');
    searchPage();
  }

  function searchPage() {
    var search = state.search;
    if (search.loading || search.done) return;
    search.loading = true;
    toast('Ищем «' + search.text + '»…', 1500);
    Belet.search(search.text, search.page, function (err, list) {
      search.loading = false;
      if (err) {
        if (handleAuthError(err)) return;
        $('searchEmpty').classList.remove('hidden');
        $('searchEmpty').querySelector('h2').textContent = 'Поиск не сработал';
        $('searchEmpty').querySelector('p').textContent = describeError(err);
        return;
      }
      var items = flattenList(list).filter(isPlayable);
      if (!items.length) search.done = true;
      else search.page += 1;
      search.items = search.items.concat(items);
      renderSearchResults(items, true);
      if (!search.items.length) {
        $('searchEmpty').classList.remove('hidden');
        $('searchEmpty').querySelector('h2').textContent = 'Ничего не найдено';
        $('searchEmpty').querySelector('p').textContent = 'Попробуйте другое название.';
      }
      if (search.items.length) {
        var target = $('searchGrid').querySelector('.card');
        state.mainFocus = target;
        if (!doc.activeElement || doc.activeElement === $('searchField')) {
          target.focus();
          reveal(target);
        }
      }
    });
  }

  function flattenList(list) {
    var out = [];
    (list || []).forEach(function (item) {
      if (!item) return;
      if (Array.isArray(item.movies)) {
        item.movies.forEach(function (movie) { if (movie) out.push(movie); });
      } else if (Array.isArray(item.items)) {
        item.items.forEach(function (movie) { if (movie) out.push(movie); });
      } else if (item.id) {
        out.push(item);
      }
    });
    return out;
  }

  function renderSearchResults(items, append) {
    renderGrid($('searchGrid'), items || [], { append: !!append });
  }

  /* ── detail ───────────────────────────────────────────────────────── */

  function openDetail(item) {
    state.detail = { movie: item, seasons: [], seasonId: null, episodes: [], loadingEpisodes: false };
    screen('detail');
    $('episodesSection').classList.add('hidden');
    $('detailTitle').textContent = movieTitle(item);
    $('detailMeta').textContent = '';
    $('detailGenres').textContent = '';
    $('detailText').textContent = 'Загружаем описание…';
    $('detailPoster').removeAttribute('src');
    $('detailBackdrop').style.backgroundImage = 'none';
    setHidden($('detailTrailer'), true);
    state.mainFocus = $('detailPlay');
    $('detailPlay').textContent = 'Смотреть';
    setTimeout(function () { $('detailPlay').focus(); }, 30);

    Belet.movie(item.id, function (err, movie) {
      if (err) {
        if (handleAuthError(err)) return;
        $('detailText').textContent = describeError(err);
        return;
      }
      renderDetail(movie);
    });
  }

  function renderDetail(movie) {
    var detail = state.detail;
    var meta = [];
    var poster = posterOf(movie) || posterOf(detail.movie);
    var backdrop = backdropOf(movie) || posterOf(movie);
    var watch = Number(movie.watch_time || 0);
    detail.movie = movie;

    setContentImage($('detailPoster'), poster);
    if (backdrop) $('detailBackdrop').style.backgroundImage = 'url("' + backdrop + '")';
    $('detailTitle').textContent = movieTitle(movie);
    var year = movie.year || (movie.release_date ? String(movie.release_date).slice(0, 4) : '');
    var duration = durationText(movie.duration);
    if (year) meta.push(year);
    if (movie.age) meta.push(movie.age + '+');
    if (duration) meta.push(duration);
    if (movie.rating_kp) meta.push('КП ' + movie.rating_kp);
    if (movie.rating_imdb) meta.push('IMDb ' + movie.rating_imdb);
    if (Array.isArray(movie.countries) && movie.countries.length) {
      meta.push(movie.countries.map(function (c) { return c && c.name ? c.name : ''; }).join(', '));
    }
    $('detailMeta').textContent = meta.join('  ·  ');
    $('detailGenres').textContent = genreNames(movie.genres);
    $('detailText').textContent = movie.description || 'Описание отсутствует.';
    $('detailFav').textContent = movie.favorites ? '✓ В избранном' : 'В избранное';
    $('detailFav').setAttribute('data-favorite', movie.favorites ? '1' : '0');
    if (watch > 30) $('detailPlay').textContent = 'Продолжить с ' + formatTime(watch);
    else $('detailPlay').textContent = 'Смотреть';

    var trailer = Array.isArray(movie.trailers) ? movie.trailers[0] : null;
    if (trailer && (trailer.hls || trailer.sourceURL)) {
      $('detailTrailer').classList.remove('hidden');
      $('detailTrailer').onclick = function () {
        playSources([{ url: trailer.hls || trailer.sourceURL, quality: '' }], {
          title: movieTitle(movie),
          subtitle: 'Трейлер',
          movieId: movie.id,
          trackProgress: false
        });
      };
    } else {
      $('detailTrailer').classList.add('hidden');
    }

    var seasons = Array.isArray(movie.seasons) ? movie.seasons : [];
    detail.seasons = seasons;
    if (seasons.length || lastEpisodeInfo(movie) || Number(movie.type_id) === 2) {
      loadSeasons(movie, seasons);
    }
    state.mainFocus = $('detailPlay');
  }

  function loadSeasons(movie, seasons) {
    var detail = state.detail;
    var normalized = normalizeSeasons(seasons);
    $('episodesSection').classList.remove('hidden');
    if (normalized.length) {
      detail.seasons = normalized;
      renderSeasonChips(movie, normalized);
      var lastEpisode = lastEpisodeInfo(movie);
      var preferred = lastEpisode && (lastEpisode.season_id || lastEpisode.seasonId);
      selectSeason(preferred || normalized[0].id);
      return;
    }
    $('episodeList').innerHTML = '<p class="search-state">Загружаем сезоны…</p>';
    Belet.seasons(movie.id, function (err, list) {
      var fetched;
      if (state.detail !== detail) return;
      fetched = normalizeSeasons(list);
      if (err || !fetched.length) {
        $('episodeList').innerHTML = '<p class="search-state">' +
          (err ? describeError(err) : 'Список сезонов пуст.') + '</p>';
        return;
      }
      detail.seasons = fetched;
      renderSeasonChips(movie, fetched);
      var lastEpisode = lastEpisodeInfo(movie);
      var preferred = lastEpisode && (lastEpisode.season_id || lastEpisode.seasonId);
      selectSeason(preferred || fetched[0].id);
    });
  }

  function lastEpisodeInfo(movie) {
    return movie && (movie.last_episode_info || movie.episode_info || movie.lastEpisodeInfo) || null;
  }

  function normalizeSeasons(seasons) {
    if (!Array.isArray(seasons)) seasons = Belet.listFrom(seasons);
    return (seasons || []).map(function (season, index) {
      if (!season || typeof season !== 'object') return null;
      var id = season.id || season.season_id || season.seasonId || season.id_season;
      if (id === null || typeof id === 'undefined' || id === '') return null;
      return {
        id: id,
        name: season.name || season.title || season.season_name || ('Сезон ' + (index + 1)),
        raw: season
      };
    }).filter(function (season) { return !!season; });
  }

  function renderSeasonChips(movie, seasons) {
    var host = $('seasonChips');
    clear(host);
    seasons.forEach(function (season, index) {
      var chip = el('button', 'chip focusable', season.name || ('Сезон ' + (index + 1)));
      chip.onclick = function () { selectSeason(season.id); };
      chip.setAttribute('data-season', season.id);
      host.appendChild(chip);
    });
  }

  function markSeasonChips() {
    var chips = $('seasonChips').querySelectorAll('.chip');
    var i;
    for (i = 0; i < chips.length; i += 1) {
      chips[i].classList.toggle('active', Number(chips[i].getAttribute('data-season')) === Number(state.detail.seasonId));
    }
  }

  function selectSeason(seasonId) {
    var detail = state.detail;
    var requestedId = String(seasonId || '');
    if (!seasonId) return;
    detail.seasonId = seasonId;
    detail.episodes = [];
    detail.loadingEpisodes = true;
    markSeasonChips();
    $('episodeList').innerHTML = '<p class="search-state">Загружаем серии…</p>';
    Belet.episodes(seasonId, { limit: 100 }, function (err, list) {
      if (state.detail !== detail || String(detail.seasonId) !== requestedId) return;
      detail.loadingEpisodes = false;
      if (err) {
        if (handleAuthError(err)) return;
        $('episodeList').innerHTML = '<p class="search-state">' + describeError(err) + '</p>';
        return;
      }
      detail.episodes = (list || []).map(function (episode, index) {
        return {
          raw: episode,
          id: episode.id || episode.episode_id || episode.episodeId || episode.content_id,
          name: episode.name || episode.title || ('Серия ' + (index + 1)),
          position: episode.episode_position || episode.position || episode.episode_number || (index + 1),
          duration: episode.duration || ''
        };
      }).filter(function (episode) { return episode.id !== null && typeof episode.id !== 'undefined'; });
      renderEpisodes();
    });
  }

  function renderEpisodes() {
    var host = $('episodeList');
    var detail = state.detail;
    clear(host);
    if (!detail.episodes.length) {
      host.innerHTML = '<p class="search-state">' +
        (detail.seasonId ? 'В этом сезоне пока нет серий.' : 'Список серий недоступен для этого фильма.') + '</p>';
      return;
    }
    detail.episodes.forEach(function (episode) {
      var button = el('button', 'episode focusable');
      var num = el('span', 'num', episode.position);
      var body = el('span', 'ep-body');
      var name = el('span', 'ep-name', episode.name);
      var meta = el('span', 'ep-meta', episode.duration ? formatTime(parseDuration(episode.duration)) : '');
      body.appendChild(name);
      body.appendChild(meta);
      button.appendChild(num);
      button.appendChild(body);
      button.onclick = function () { playEpisode(episode); };
      host.appendChild(button);
    });
  }

  function playEpisode(episode) {
    var detail = state.detail;
    var movie = detail.movie;
    var typeId = episode.raw && (episode.raw.file_type_id || episode.raw.fileTypeId) || 4;
    var resumeAt = 0;
    var last = lastEpisodeInfo(movie);
    if (last && Number(last.episode_id) === Number(episode.id)) resumeAt = Number(last.episode_watch_time || 0);
    if (episode.raw && Number(episode.raw.watch_time || 0) > resumeAt) resumeAt = Number(episode.raw.watch_time);
    if (episode.raw && Array.isArray(episode.raw.sources) && episode.raw.sources.length) {
      playById(episode.id, typeId, {
        title: movieTitle(movie),
        subtitle: episode.name,
        movieId: movie.id,
        seasonId: detail.seasonId,
        episodeId: episode.id,
        startAt: resumeAt,
        trackProgress: true,
        typeId: typeId,
        fallbackSources: Belet.sourcesFrom(episode.raw)
      });
      return;
    }
    playById(episode.id, typeId, {
      title: movieTitle(movie),
      subtitle: episode.name,
      movieId: movie.id,
      seasonId: detail.seasonId,
      episodeId: episode.id
    });
  }

  function playMovieOrResume() {
    var movie = state.detail.movie;
    var lastEpisode;
    if (!movie) return;
    lastEpisode = lastEpisodeInfo(movie);
    if (Number(movie.type_id) === 2 && !(lastEpisode && (lastEpisode.episode_id || lastEpisode.episodeId)) &&
        state.detail.episodes.length) {
      playEpisode(state.detail.episodes[0]);
      return;
    }
    if (Number(movie.type_id) === 2 && lastEpisode && (lastEpisode.episode_id || lastEpisode.episodeId)) {
      var info = lastEpisode;
      var episode = {
        id: info.episode_id || info.episodeId,
        name: info.episode_name || 'Серия',
        position: info.episode_position,
        duration: info.episode_duration,
        raw: null
      };
      if (state.detail.seasonId && info.season_id && Number(state.detail.seasonId) !== Number(info.season_id)) {
        selectSeason(info.season_id);
      }
      playEpisode(episode);
      return;
    }
    playById(movie.id, movie.type_id || 1, {
      title: movieTitle(movie),
      subtitle: movie.year ? String(movie.year) : '',
      movieId: movie.id,
      startAt: Number(movie.watch_time || 0),
      seasonId: state.detail.seasonId,
      trackProgress: true
    });
  }

  function playById(id, typeId, options) {
    options = options || {};
    options.title = options.title || '';
    openPlayer(options);
    Belet.files(id, typeId, function (err, sources, meta) {
      if (err) {
        if (handleAuthError(err)) return;
        if (options.fallbackSources && options.fallbackSources.length) {
          startSources(options.fallbackSources, options);
          return;
        }
        playerError(describeError(err));
        return;
      }
      startSources(sources, options, meta);
    });
  }

  function playSources(sources, options) {
    options = options || {};
    openPlayer(options);
    startSources(sources || [], options);
  }

  /* ── player ───────────────────────────────────────────────────────── */

  /* Follow belet.tm's HLS.js/MSE path first, with AVPlay and native video as fallbacks. */
  function avplayApi() {
    try {
      if (window.webapis && window.webapis.avplay) return window.webapis.avplay;
    } catch (e) {}
    return null;
  }

  function avplayAvailable() {
    return !!avplayApi() && !state.player.noAvplay;
  }

  function avplaySurface(show) {
    var node = $('avPlayer');
    if (!node) return;
    setHidden(node, !show);
  }

  function isHlsSource(source) {
    if (!source) return false;
    if (/\.m3u8(?:[?#]|$)/i.test(source.url || '')) return true;
    return /(?:^|[\s/+_.-])(?:hls|m3u8|mpegurl|x-mpegurl)(?:$|[\s;,+_.-])/i.test(source.type || '');
  }

  function engineLabel(engine) {
    if (engine === 'hls') return 'MSE';
    if (engine === 'avplay') return 'AVPlay';
    return 'Video';
  }

  function openPlayer(options) {
    var player = state.player;
    cancelPlayback(true);
    screen('player');
    player.active = true;
    player.movie = null;
    player.engine = null;
    player.avPlaying = false;
    player.avError = false;
    player.avOpened = false;
    player.started = false;
    player.reauthorized = false;
    player.attempts = [];
    player.beletSessionId = window.Belet && window.Belet.newPlayerSession ?
      String(window.Belet.newPlayerSession()) : '';
    player.chunkLengths = {};
    player.movieId = options.movieId || null;
    player.seasonId = options.seasonId || null;
    player.episodeId = options.episodeId || null;
    player.title = options.title || '';
    player.subtitle = options.subtitle || '';
    player.startAt = Number(options.startAt || 0);
    player.trackProgress = options.trackProgress !== false;
    player.typeId = options.typeId || null;
    player.reported = 0;
    player.ending = false;
    try { if (doc.activeElement && doc.activeElement.blur) doc.activeElement.blur(); } catch (e) {}
    $('playerTitle').textContent = player.title;
    $('playerSubtitle').textContent = player.subtitle;
    $('playerBadge').textContent = '';
    setHidden($('playerStatus'), true);
    setHidden($('playerSpinner'), false);
    avplaySurface(false);
    showOsd(true);
    try { $('video').pause(); } catch (e) {}
    try { $('video').removeAttribute('src'); $('video').load(); } catch (e) {}
  }

  function startSources(sources, options, meta) {
    var player = state.player;
    sources = (sources || []).filter(function (source) { return source && source.url; }).map(function (source) {
      var proxied = (window.Belet && window.Belet.proxyUrl) ? window.Belet.proxyUrl(source.url) : source.url;
      if (proxied === source.url) return source;
      return { url: proxied, filename: source.filename, quality: source.quality, type: source.type };
    });
    if (!sources.length) {
      playerError('Сервис не предоставил видеопоток для этого фильма.');
      return;
    }
    sources.sort(function (a, b) {
      var aHls = isHlsSource(a) ? 0 : 1;
      var bHls = isHlsSource(b) ? 0 : 1;
      if (aHls !== bHls) return aHls - bHls;
      return (parseInt(b.quality, 10) || 0) - (parseInt(a.quality, 10) || 0);
    });
    player.sources = sources;
    if (meta && meta.sessionId) player.beletSessionId = String(meta.sessionId);
    if (window.Belet && window.Belet.newPlayerSession && meta && meta.sessionId) {
      window.Belet.newPlayerSession(player.beletSessionId);
    }
    player.chunkLengths = {};
    player.sourceIndex = 0;
    player.strategyIndex = 0;
    player.failed = false;
    player.movie = options || {};
    state.playbackInfo = 'источников: ' + sources.length + ' · ' + hostOf(sources[0].url);
    beginAttempt();
  }

  function hostOf(url) {
    var proxied = String(url || '').match(/^https?:\/\/[^\/]+\/__proxy\/([^\/?#]+)/i);
    if (proxied) return proxied[1];
    var match = String(url || '').match(/^https?:\/\/([^\/]+)/i);
    return match ? match[1] : '—';
  }

  function strategiesFor(source) {
    var list = [];
    var hlsWorks = false;
    try { hlsWorks = !!(window.Hls && window.Hls.isSupported()); } catch (e) {}
    if (isHlsSource(source) && hlsWorks) list.push('hls');
    if (avplayAvailable()) list.push('avplay');
    list.push('native');
    return list;
  }

  function beginAttempt() {
    var player = state.player;
    var source = player.sources[player.sourceIndex];
    var strategies;
    if (!source) { attemptFailed('нет источников'); return; }
    strategies = strategiesFor(source);
    player.strategies = strategies;
    if (player.strategyIndex >= strategies.length) { attemptFailed('движки исчерпаны'); return; }
    player.engine = strategies[player.strategyIndex];
    player.started = false;
    $('playerBadge').textContent = (source.quality || 'HLS') + ' · ' + engineLabel(player.engine);
    setHidden($('playerSpinner'), false);
    setHidden($('playerStatus'), true);
    armWatchdog();
    if (player.engine === 'hls') startHls(source);
    else if (player.engine === 'avplay') startAvplaySource(source);
    else startNativeVideo(source);
  }

  function attemptFailed(reason) {
    var player = state.player;
    var detail;
    player.attempts.push(engineLabel(player.engine) + ': ' + reason);
    disarmWatchdog();
    teardownEngine();
    if (player.strategyIndex + 1 < (player.strategies || []).length) {
      player.strategyIndex += 1;
      beginAttempt();
      return;
    }
    if (player.sourceIndex + 1 < player.sources.length) {
      player.sourceIndex += 1;
      player.strategyIndex = 0;
      beginAttempt();
      return;
    }
    detail = player.attempts.slice(-3).join('; ');
    player.failed = true;
    state.playbackInfo = 'ошибка · ' + detail;
    playerError('Не удалось воспроизвести поток (' + detail + '). Проверьте подписку и соединение.');
  }

  function armWatchdog() {
    disarmWatchdog();
    state.player.watchdog = setTimeout(function () {
      if (state.player.active && !state.player.started) attemptFailed('нет старта за 15 с');
    }, 15000);
  }

  function disarmWatchdog() {
    if (state.player.watchdog) {
      clearTimeout(state.player.watchdog);
      state.player.watchdog = null;
    }
  }

  function onPlaybackStarted() {
    var player = state.player;
    player.started = true;
    state.playbackInfo = 'ok · ' + engineLabel(player.engine) + ' · ' + hostOf((player.sources[player.sourceIndex] || {}).url);
    disarmWatchdog();
    setHidden($('playerSpinner'), true);
    setHidden($('playerStatus'), true);
    hideOsdSoon();
  }

  function attachVideoEvents() {
    var video = $('video');
    video.onloadedmetadata = function () {
      var startAt = state.player.startAt;
      if (startAt > 5 && isFinite(video.duration) && startAt < video.duration - 20) {
        try { video.currentTime = startAt; } catch (e) {}
        toast('Продолжаем с ' + formatTime(startAt), 2600);
      }
      setHidden($('playerSpinner'), true);
      updateProgress();
    };
    video.oncanplay = function () { setHidden($('playerSpinner'), true); };
    video.onwaiting = function () { setHidden($('playerSpinner'), false); };
    video.onplaying = function () { onPlaybackStarted(); };
    video.onerror = function () {
      if (state.player.engine === 'avplay') return;
      var code = video.error ? video.error.code : 0;
      attemptFailed('video' + (code ? ' ' + code : ''));
    };
    video.onended = function () {
      state.player.ending = true;
      reportProgress();
      exitPlayer();
    };
  }

  function detachVideoEvents() {
    var video = $('video');
    try {
      video.onloadedmetadata = null;
      video.oncanplay = null;
      video.onwaiting = null;
      video.onplaying = null;
      video.onerror = null;
      video.onended = null;
    } catch (e) {}
  }

  function mediaRequestSetup(xhr, url) {
    var target;
    if (!xhr || !window.Belet || !window.Belet.mediaHeaders) return;
    target = window.Belet.proxyUrl ? window.Belet.proxyUrl(url) : url;
    try { if (xhr.readyState === 0) xhr.open('GET', target, true); } catch (e) {}
    var headers = window.Belet.mediaHeaders(state.player.beletSessionId, false, null);
    for (var key in headers) {
      if (!Object.prototype.hasOwnProperty.call(headers, key)) continue;
      try { xhr.setRequestHeader(key, headers[key]); } catch (e) {}
    }
  }

  function beletHlsLoaderClass(hls, isFragment) {
    var BaseLoader = window.Hls && window.Hls.DefaultConfig && window.Hls.DefaultConfig.loader;
    if (!BaseLoader || !window.Belet || !window.Belet.mediaHeaders) return null;
    function BeletLoader(config) {
      this.loader = new BaseLoader(config);
      this.stats = this.loader.stats;
      this.cancelled = false;
    }
    BeletLoader.prototype.load = function (context, config, callbacks) {
      var self = this;
      function send() {
        var level = context && context.frag ? Number(context.frag.level) : -1;
        var chunkLength = isFragment && state.player.chunkLengths ? state.player.chunkLengths[level] : null;
        var headers;
        if (self.cancelled || !state.player.active || (hls && state.player.hls !== hls)) return;
        if (window.Belet && window.Belet.proxyUrl && context && context.url) {
          context.url = window.Belet.proxyUrl(context.url);
        }
        context.headers = context.headers || {};
        headers = window.Belet.mediaHeaders(state.player.beletSessionId, isFragment, chunkLength);
        Object.keys(headers).forEach(function (key) { context.headers[key] = headers[key]; });
        self.loader.load(context, config, callbacks);
        self.stats = self.loader.stats;
      }
      send();
    };
    BeletLoader.prototype.abort = function () {
      this.cancelled = true;
      if (this.loader && this.loader.abort) this.loader.abort();
    };
    BeletLoader.prototype.destroy = function () {
      this.cancelled = true;
      if (this.loader && this.loader.destroy) this.loader.destroy();
      this.loader = null;
    };
    BeletLoader.prototype.getCacheAge = function () {
      return this.loader && this.loader.getCacheAge ? this.loader.getCacheAge() : null;
    };
    BeletLoader.prototype.getResponseHeader = function (name) {
      return this.loader && this.loader.getResponseHeader ? this.loader.getResponseHeader(name) : null;
    };
    return BeletLoader;
  }

  function startHls(source) {
    var player = state.player;
    var video = $('video');
    var hls;
    var requestLoader;
    var fragmentLoader;
    avplaySurface(false);
    try { video.classList.remove('hidden'); } catch (e) {}
    try {
      requestLoader = beletHlsLoaderClass(null, false);
      fragmentLoader = beletHlsLoaderClass(null, true);
      hls = new window.Hls({
        enableWorker: false,
        lowLatencyMode: false,
        backBufferLength: 10,
        maxBufferLength: 30,
        manifestLoadingTimeOut: 15000,
        fragLoadingTimeOut: 15000,
        manifestLoadingMaxRetry: 3,
        manifestLoadingRetryDelay: 500,
        levelLoadingMaxRetry: 2,
        fragLoadingMaxRetry: 3,
        fragLoadingRetryDelay: 500,
        loader: requestLoader || undefined,
        fLoader: fragmentLoader || undefined,
        xhrSetup: requestLoader ? undefined : mediaRequestSetup
      });
    } catch (e) {
      attemptFailed('hls init');
      return;
    }
    player.hls = hls;
    attachVideoEvents();
    hls.on(window.Hls.Events.ERROR, function (event, data) {
      if (!data || !data.fatal) return;
      var status = data.response && data.response.code ? data.response.code : 0;
      if (status === 401) {
        /* Match belet.tm: refresh the access token before retrying the raw-token request. */
        if (!player.reauthorized && window.Belet && window.Belet.refresh) {
          player.reauthorized = true;
          teardownEngine();
          disarmWatchdog();
          window.Belet.refresh(function (refreshErr) {
            if (!state.player.active) return;
            if (refreshErr) { attemptFailed('hls http 401'); return; }
            window.Belet.newPlayerSession(player.beletSessionId || undefined);
            state.player.strategyIndex = 0;
            beginAttempt();
          });
          return;
        }
        /* Some regional CDN paths still require the HTTP Bearer authorization scheme. */
        if (window.Belet && window.Belet.nextMediaScheme && window.Belet.nextMediaScheme()) {
          teardownEngine();
          disarmWatchdog();
          player.strategyIndex = 0;
          beginAttempt();
          return;
        }
      }
      var code = status ? ' HTTP ' + status : '';
      var failedUrl = data.context && data.context.url ? hostOf(data.context.url) : '';
      attemptFailed('hls ' + (data.details || data.type || 'error') + code + (failedUrl ? ' @ ' + failedUrl : ''));
    });
    hls.on(window.Hls.Events.LEVEL_LOADED, function (event, data) {
      var details = data && data.details;
      var fragments = details && details.fragments;
      if (details && fragments && fragments.length && isFinite(details.totalduration)) {
        player.chunkLengths[data.level] = String(Math.floor(details.totalduration / fragments.length * 1000));
      }
    });
    hls.on(window.Hls.Events.MEDIA_ATTACHED, function () {
      if (!player.active || player.hls !== hls) return;
      /* ExoPlayer consumes Belet's exact source URL and headers. Keep the original
       * manifest intact so HLS.js resolves its relative playlist/segment paths. */
      hls.loadSource(source.url);
    });
    hls.on(window.Hls.Events.MANIFEST_PARSED, function () { playVideoElement(); });
    try {
      hls.attachMedia(video);
    } catch (e) {
      attemptFailed('hls attach');
    }
  }

  function startNativeVideo(source) {
    var video = $('video');
    avplaySurface(false);
    try { video.classList.remove('hidden'); } catch (e) {}
    attachVideoEvents();
    try {
      video.src = source.url;
      video.load();
    } catch (e) {
      attemptFailed('video src');
      return;
    }
    playVideoElement();
  }

  function playVideoElement() {
    var player = state.player;
    var video = $('video');
    var startAt = player.startAt;
    var promise;
    if (player.engine === 'avplay') return;
    if (startAt > 5 && isFinite(video.duration) && startAt < video.duration - 20) {
      try { video.currentTime = startAt; } catch (e) {}
    }
    try {
      promise = video.play();
    } catch (e) {
      attemptFailed('play');
      return;
    }
    if (promise && promise['catch']) {
      promise['catch'](function () {
        if (!player.active) return;
        // Most often this is the TV's autoplay policy: the stream is fine, it just waits
        // for a key press. Do not degrade the engine in that case.
        disarmWatchdog();
        showOsd(true);
        playerMessage('Нажмите OK, чтобы начать воспроизведение');
      });
    }
  }

  function startAvplaySource(source) {
    var player = state.player;
    var api = avplayApi();
    var failed = false;
    if (!api) { player.noAvplay = true; attemptFailed('avplay недоступен'); return; }
    player.avPlaying = false;
    player.avOpened = false;
    avplaySurface(true);
    try { $('video').classList.add('hidden'); } catch (e) {}
    function fail(reason) {
      if (failed || !player.active) return;
      failed = true;
      attemptFailed(reason || 'avplay error');
    }
    try {
      api.open(source.url);
      player.avOpened = true;
      try { if (api.setStreamingProperty) api.setStreamingProperty('USER_AGENT', 'belet_films_android'); } catch (e) {}
      api.setDisplayRect(0, 0, window.innerWidth || 1920, window.innerHeight || 1080);
      api.setListener({
        onbufferingstart: function () { setHidden($('playerSpinner'), false); },
        onbufferingprogress: function () {},
        onbufferingcomplete: function () { setHidden($('playerSpinner'), true); },
        oncurrentplaytime: function (ms) {
          player.avTime = (Number(ms) || 0) / 1000;
          if (!player.avPlaying) {
            player.avPlaying = true;
            onPlaybackStarted();
          }
        },
        onstreamcompleted: function () {
          player.ending = true;
          reportProgress();
          exitPlayer();
        },
        onerror: function (error) {
          var code = error && (error.code || error.name || error.message);
          fail('avplay error' + (code ? ' ' + code : ''));
        }
      });
      api.prepareAsync(function () {
        if (!player.active || player.engine !== 'avplay') return;
        try {
          if (player.startAt > 5) api.seekTo(Math.round(player.startAt * 1000));
          api.play();
          player.avPlaying = true;
          setHidden($('playerSpinner'), true);
          hideOsdSoon();
          startAvplayTicker();
        } catch (e) {
          fail('avplay play');
        }
      }, function (error) {
        var code = error && (error.code || error.name || error.message);
        fail('avplay prepare' + (code ? ' ' + code : ''));
      });
    } catch (e) {
      fail('avplay open');
    }
  }

  function stopAvplay() {
    var api = avplayApi();
    var player = state.player;
    stopAvplayTicker();
    player.avPlaying = false;
    if (!api || !player.avOpened) return;
    player.avOpened = false;
    try { api.stop(); } catch (e) {}
    try { api.close(); } catch (e) {}
  }

  function startAvplayTicker() {
    stopAvplayTicker();
    state.player.ticker = setInterval(function () {
      if (!state.player.active || state.player.engine !== 'avplay') return;
      var api = avplayApi();
      if (!api) return;
      try {
        state.player.avDuration = (Number(api.getDuration()) || 0) / 1000;
        state.player.avTime = (Number(api.getCurrentTime()) || 0) / 1000;
      } catch (e) {}
      updateProgress();
    }, 500);
  }

  function stopAvplayTicker() {
    if (state.player.ticker) {
      clearInterval(state.player.ticker);
      state.player.ticker = null;
    }
  }

  function teardownEngine() {
    var player = state.player;
    var video = $('video');
    stopAvplayTicker();
    if (player.hls) {
      try { player.hls.destroy(); } catch (e) {}
      player.hls = null;
    }
    stopAvplay();
    avplaySurface(false);
    detachVideoEvents();
    try { video.pause(); } catch (e) {}
    try {
      video.removeAttribute('src');
      video.load();
    } catch (e) {}
    try { video.classList.remove('hidden'); } catch (e) {}
  }

  function playbackNow() {
    var player = state.player;
    if (player.engine === 'avplay') {
      var api = avplayApi();
      var current = Number(player.avTime) || 0;
      var duration = Number(player.avDuration) || 0;
      if (api) {
        try { current = (Number(api.getCurrentTime()) || 0) / 1000 || current; } catch (e) {}
        try { duration = (Number(api.getDuration()) || 0) / 1000 || duration; } catch (e) {}
      }
      return { current: current, duration: duration, paused: !player.avPlaying, buffered: 0 };
    }
    var video = $('video');
    var buffered = 0;
    if (video.buffered && video.buffered.length) {
      try { buffered = video.buffered.end(video.buffered.length - 1) / (video.duration || 1); } catch (e) {}
    }
    return { current: video.currentTime || 0, duration: video.duration || 0, paused: !!video.paused, buffered: buffered };
  }

  function playerMessage(text) {
    var node = $('playerStatus');
    node.textContent = text;
    setHidden(node, false);
  }

  function playerError(text) {
    setHidden($('playerSpinner'), true);
    playerMessage(text);
    showOsd(true);
  }

  function togglePlayback() {
    var player = state.player;
    if (player.engine === 'avplay') {
      var api = avplayApi();
      if (!api) return;
      try {
        if (player.avPlaying) {
          api.pause();
          player.avPlaying = false;
          reportProgress();
        } else {
          api.play();
          player.avPlaying = true;
          hideOsdSoon();
        }
      } catch (e) {
        playerMessage('Не удалось управлять воспроизведением');
      }
      showOsd(true);
      return;
    }
    var video = $('video');
    var promise;
    if (!video.src && !player.hls) return;
    if (video.paused) {
      promise = video.play();
      if (promise && promise['catch']) {
        promise['catch'](function () { playerMessage('Не удалось начать воспроизведение'); });
      }
    } else {
      video.pause();
      reportProgress();
    }
    showOsd(true);
  }

  function seek(delta) {
    var player = state.player;
    var info = playbackNow();
    var target;
    if (!info.duration) return;
    target = Math.max(0, Math.min(info.duration - 1, info.current + delta));
    if (player.engine === 'avplay') {
      var api = avplayApi();
      if (api) {
        try { api.seekTo(Math.round(target * 1000)); } catch (e) {}
      }
      player.avTime = target;
    } else {
      try { $('video').currentTime = target; } catch (e) {}
    }
    toast((delta > 0 ? '▶▶ +' : '◀◀ -') + Math.abs(delta) + ' сек', 1200);
    showOsd(true);
    reportProgress();
  }

  function updateProgress() {
    var info = playbackNow();
    var duration = info.duration || 0;
    var current = info.current || 0;
    var percent = duration ? (current / duration) * 100 : 0;
    var buffered = (info.buffered || 0) * 100;
    $('progressFill').style.width = percent + '%';
    $('progressKnob').style.left = percent + '%';
    $('progressBuffer').style.width = Math.min(100, buffered) + '%';
    $('timeNow').textContent = formatTime(current);
    $('timeTotal').textContent = formatTime(duration);
    state.player.duration = duration;
    if (state.player.trackProgress && duration && current - state.player.reported > 20 && !info.paused) {
      state.player.reported = current;
      reportProgress();
    }
  }

  function reportProgress() {
    var player = state.player;
    var info = playbackNow();
    if (!player.active || !player.trackProgress || !player.movieId) return;
    if (!isFinite(info.current) || info.current < 3) return;
    Belet.lastWatchTime(player.movieId, player.seasonId, info.current, info.duration, function (err) {
      if (err && err.kind === 'auth') handleAuthError(err);
    });
  }

  function hideOsdSoon() {
    if (state.player.osdTimer) clearTimeout(state.player.osdTimer);
    state.player.osdTimer = setTimeout(function () {
      if (state.player.active && !playbackNow().paused) showOsd(false);
    }, 4200);
  }

  function showOsd(on) {
    $('player').classList.toggle('osd-on', on !== false);
    if (on !== false) hideOsdSoon();
  }

  function exitPlayer() {
    var detailMovie = state.detail.movie;
    reportProgress();
    cancelPlayback(true);
    if (detailMovie) {
      screen('detail');
      state.mainFocus = $('detailPlay');
      setTimeout(function () { $('detailPlay').focus(); }, 40);
    } else {
      screen(state.previous || 'home');
      state.mainFocus = null;
      setTimeout(function () { focusScreen(); }, 40);
    }
  }

  function cancelPlayback(silent) {
    var player = state.player;
    if (player.active && !silent) reportProgress();
    player.active = false;
    player.ending = false;
    if (player.osdTimer) {
      clearTimeout(player.osdTimer);
      player.osdTimer = null;
    }
    disarmWatchdog();
    teardownEngine();
    showOsd(false);
  }

  /* ── profile & diagnostics ────────────────────────────────────────── */

  function openProfile() {
    screen('profile');
    state.mainFocus = $('profileRefresh');
    setTimeout(function () { $('profileRefresh').focus(); }, 30);
    loadProfile();
  }

  function loadProfile(attempt) {
    var retryAttempt = typeof attempt === 'number' ? attempt : 0;
    clearProfileTimer();
    $('profilePlan').textContent = retryAttempt ? 'Повторяем загрузку профиля…' : 'Загрузка данных…';
    Belet.profile(function (err, data) {
      if (state.screen !== 'profile') return;
      if (err) {
        if (handleAuthError(err)) return;
        $('profilePlan').textContent = describeError(err);
        if (err.kind === 'network' && retryAttempt < 2) {
          state.profileRetryTimer = setTimeout(function () {
            state.profileRetryTimer = null;
            if (state.screen === 'profile' && Belet.hasSession()) loadProfile(retryAttempt + 1);
          }, 2000);
        }
        return;
      }
      var view = data && data.profile_page_view;
      var info = view && view.user_info_view;
      var subscription = view && view.individual_subscription_view;
      var phone = info && info.phone;
      if (phone) $('profilePhone').textContent = maskPhone(phone);
      if (subscription && subscription.subscription_name) {
        var status = subscription.status_message && subscription.status_message.text;
        $('profilePlan').textContent = subscription.subscription_name + (status ? ' · ' + status : '');
      } else {
        $('profilePlan').textContent = 'Активная подписка не найдена. Проверьте тариф в приложении Belet.';
      }
    });
  }

  function openAbout() {
    if (!Belet.hasSession()) state.previous = 'login';
    screen('about');
    state.mainFocus = $('aboutRetest');
    setTimeout(function () { $('aboutRetest').focus(); }, 30);
    runHealthCheck();
  }

  function runHealthCheck() {
    var host = $('aboutStatus');
    var alertBox = $('aboutClockAlert');
    host.innerHTML = '<div class="about-row"><span>Проверка…</span><span></span></div>';
    if (isClockBlockingHttps()) {
      scheduleClockWatch();
      if (alertBox) {
        alertBox.textContent = clockHelpText();
        setHidden(alertBox, false);
      }
    } else if (alertBox) {
      setHidden(alertBox, true);
    }
    function runProbeAndCheck() {
      function renderDiag(results) {
        var labels = {
          auth: 'Авторизация (beletapis.com)',
          home: 'Каталог (homepage.belet.me)',
          film: 'Видеосервис (film.beletapis.com)'
        };
        var order = ['auth', 'home', 'film'];
        var now = new Date();
        var year = now.getFullYear();
        var timeValid = year >= 2026;
        var bypassed = isClockBypassed();
        var tz = readTizenClock();
        var tzYear = (tz && tz.getFullYear) ? tz.getFullYear() : 0;
        var tzValid = tzYear >= 2026;
        var timeText = formatDateObj(now) +
          (timeValid ? '' : (bypassed ? ' (обход через локальный прокси ТВ)' : ' (не синхронизировано — HTTPS пропущен)'));
        var netOk = isNetworkReady();
        if (alertBox) {
          if (!timeValid && !bypassed) {
            alertBox.textContent = clockHelpText();
            setHidden(alertBox, false);
          } else {
            setHidden(alertBox, true);
          }
        }
        host.innerHTML = '';
        order.forEach(function (key) {
          var value = results[key] || 'нет ответа';
          var row = el('div', 'about-row');
          var name = el('span', '', labels[key]);
          var status = el('span', value === 'ok' ? 'ok' : 'bad', value === 'ok' ? 'доступен' : value);
          row.appendChild(name);
          row.appendChild(status);
          host.appendChild(row);
        });
        var netRow = el('div', 'about-row');
        netRow.appendChild(el('span', '', 'Сеть телевизора'));
        netRow.appendChild(el('span', netOk ? 'ok' : 'bad', networkLabel()));
        host.appendChild(netRow);
        var timeRow = el('div', 'about-row');
        timeRow.appendChild(el('span', '', 'Время браузера (SSL)'));
        timeRow.appendChild(el('span', (timeValid || bypassed) ? 'ok' : 'bad', timeText));
        host.appendChild(timeRow);
        if (tz && tzYear !== year) {
          var tzRow = el('div', 'about-row');
          tzRow.appendChild(el('span', '', 'Время Tizen (настройки ТВ)'));
          tzRow.appendChild(el('span', tzValid ? 'ok' : 'bad',
            formatDateObj(tz) + (tzValid && !timeValid ? ' (задано вручную — браузер ещё на ' + year + ')' : '')));
          host.appendChild(tzRow);
        }
        if (!timeValid && clockProxyState.serverSynced) {
          var srvTimeRow = el('div', 'about-row');
          srvTimeRow.appendChild(el('span', '', 'Время с сервера Belet'));
          srvTimeRow.appendChild(el('span', 'ok', formatDateObj(new Date(Date.now() + clockProxyState.serverOffsetMs))));
          host.appendChild(srvTimeRow);
        }
        if (!timeValid || bypassed || isBypassModeForced()) {
          var proxyRow = el('div', 'about-row');
          proxyRow.appendChild(el('span', '', 'Обход часов ТВ (SSL)'));
          proxyRow.appendChild(el('span', bypassed ? 'ok' : 'bad', clockProxyState.diag));
          host.appendChild(proxyRow);
        }
        var modelRow = el('div', 'about-row');
        modelRow.appendChild(el('span', '', 'Модель телевизора'));
        modelRow.appendChild(el('span', '', Belet.model()));
        host.appendChild(modelRow);
        var deviceRow = el('div', 'about-row');
        deviceRow.appendChild(el('span', '', 'Идентификатор устройства'));
        deviceRow.appendChild(el('span', '', Belet.device() || '—'));
        host.appendChild(deviceRow);
        var listRow = el('div', 'about-row');
        listRow.appendChild(el('span', '', 'Список устройств Belet'));
        listRow.appendChild(el('span', '', deviceRegistration.state));
        host.appendChild(listRow);
        var playRow = el('div', 'about-row');
        playRow.appendChild(el('span', '', 'Последнее воспроизведение'));
        playRow.appendChild(el('span', '', state.playbackInfo));
        host.appendChild(playRow);
        var sessionRow = el('div', 'about-row');
        sessionRow.appendChild(el('span', '', 'Сессия'));
        sessionRow.appendChild(el('span', '', Belet.hasSession() ? 'активна' : 'не выполнен вход'));
        host.appendChild(sessionRow);
        updateClockBridgeButtons();
      }
      if (isClockBlockingHttps()) {
        scheduleClockWatch();
        renderDiag({
          auth: 'ожидает обхода SSL (HTTPS пропущен)',
          home: 'ожидает обхода SSL (HTTPS пропущен)',
          film: 'ожидает обхода SSL (HTTPS пропущен)'
        });
        return;
      }
      Belet.healthCheck(renderDiag);
    }
    updateClockBridgeButtons();
    if (isClockBlockingHttps() && canProbeAnyClockBridge()) {
      probeClockProxy(function () { runProbeAndCheck(); });
      return;
    }
    runProbeAndCheck();
  }

  function logout() {
    if (state.login.loggingOut) return;
    dialog('Выйти из Belet?', 'Локальная сессия будет удалена с телевизора.', [
      {
        label: 'Выйти',
        primary: true,
        onSelect: function () {
          if (state.login.loggingOut) return;
          state.login.loggingOut = true;
          deviceRegistration.state = 'не отправлена';
          resetCatalogueState();
          $('profilePhone').textContent = '';
          $('profilePlan').textContent = '';
          Belet.logout(function (err) {
            state.login.loggingOut = false;
            if (err) toast('Локальная сессия удалена; сервер не подтвердил выход', 4000);
            showLogin();
          });
        }
      },
      { label: 'Отмена' }
    ]);
  }

  /* ── key handling ─────────────────────────────────────────────────── */

  function isBack(key) {
    return key === KEYS.BACK || key === KEYS.ESC || key === KEYS.BACK_ALT;
  }

  function handleBack() {
    var overlay = activeOverlay();
    if (overlay === $('dialog')) {
      closeDialog();
      return true;
    }
    if (overlay === $('numpad') || overlay === $('keyboard')) {
      closeInput();
      return true;
    }
    if (state.screen === 'player') {
      exitPlayer();
      return true;
    }
    if (state.screen === 'detail') {
      screen(state.previous || 'home');
      setTimeout(function () { focusScreen(); }, 40);
      return true;
    }
    if (state.screen === 'about') {
      if (!Belet.hasSession()) {
        screen('login');
        focusLoginStep();
        return true;
      }
      if (state.previous && state.previous !== 'about' && state.previous !== 'login') screen(state.previous);
      else screen('home');
      setTimeout(function () { focusScreen(); }, 40);
      return true;
    }
    if (state.screen === 'browse' || state.screen === 'search' || state.screen === 'profile') {
      if (state.screen === 'browse' && state.browse.mode === 'favorites') {
        screen('profile');
        setTimeout(function () { state.mainFocus = $('profileFavorites'); $('profileFavorites').focus(); }, 40);
        return true;
      }
      screen('home');
      setTimeout(function () { focusScreen(); }, 40);
      return true;
    }
    if (state.screen === 'home') {
      if (!state.backArmed) {
        state.backArmed = true;
        toast('Нажмите RETURN ещё раз, чтобы выйти из Belet', 2600);
        setTimeout(function () { state.backArmed = false; }, 2800);
        return true;
      }
      exitApplication();
      return true;
    }
    if (state.screen === 'login' || state.screen === 'boot') {
      exitApplication();
      return true;
    }
    return true;
  }

  function exitApplication() {
    try {
      if (window.tizen && window.tizen.application) {
        window.tizen.application.getCurrentApplication().exit();
        return;
      }
    } catch (e) {}
    try { window.close(); } catch (e) {}
  }

  function handlePlayerKey(key) {
    var player = state.player;
    var paused = playbackNow().paused;
    if (key === KEYS.OK) {
      // After a failed start, OK retries every engine/source combination once more.
      if (player.failed) {
        player.failed = false;
        player.attempts = [];
        player.sourceIndex = 0;
        player.strategyIndex = 0;
        setHidden($('playerStatus'), true);
        beginAttempt();
        return true;
      }
      togglePlayback();
      return true;
    }
    if (key === KEYS.LEFT || key === KEYS.REWIND) { seek(-10); return true; }
    if (key === KEYS.RIGHT || key === KEYS.FORWARD) { seek(10); return true; }
    if (key === KEYS.UP || key === KEYS.DOWN) { showOsd(true); return true; }
    if (key === KEYS.PLAY) {
      if (paused) togglePlayback();
      return true;
    }
    if (key === KEYS.PAUSE) {
      if (!paused) togglePlayback();
      showOsd(true);
      return true;
    }
    if (key === KEYS.PLAY_PAUSE) { togglePlayback(); return true; }
    if (key === KEYS.STOP) { exitPlayer(); return true; }
    return false;
  }

  on(doc, 'keydown', function (event) {
    var key = event.keyCode;
    var handled = false;

    if (!hasClass($('dialog'), 'hidden')) {
      if (isBack(key)) { closeDialog(); handled = true; }
      else if (key === KEYS.LEFT || key === KEYS.RIGHT || key === KEYS.UP || key === KEYS.DOWN) handled = move({
        37: 'left', 38: 'up', 39: 'right', 40: 'down'
      }[key]);
      if (handled) event.preventDefault();
      return;
    }

    if (!hasClass($('numpad'), 'hidden') || !hasClass($('keyboard'), 'hidden')) {
      if (isBack(key)) { closeInput(); handled = true; }
      else if (!hasClass($('numpad'), 'hidden') && key >= 48 && key <= 57) { inputAppend(String(key - 48)); handled = true; }
      else if (!hasClass($('numpad'), 'hidden') && key >= 96 && key <= 105) { inputAppend(String(key - 96)); handled = true; }
      else if (!hasClass($('numpad'), 'hidden') && key === 8) { inputDelete(); handled = true; }
      else if (key === KEYS.LEFT || key === KEYS.RIGHT || key === KEYS.UP || key === KEYS.DOWN) handled = move({
        37: 'left', 38: 'up', 39: 'right', 40: 'down'
      }[key]);
      if (handled) event.preventDefault();
      return;
    }

    if (state.screen === 'player') {
      handled = handlePlayerKey(key);
      if (isBack(key)) handled = handleBack();
      if (handled) event.preventDefault();
      return;
    }

    if (isBack(key)) {
      handled = handleBack();
      if (handled) event.preventDefault();
      return;
    }

    if (key === KEYS.LEFT || key === KEYS.RIGHT || key === KEYS.UP || key === KEYS.DOWN) {
      handled = move({ 37: 'left', 38: 'up', 39: 'right', 40: 'down' }[key]);
      if (handled) event.preventDefault();
      return;
    }

    if (key === KEYS.OK) {
      // Chromium on Tizen turns OK/Enter into a click on the focused element.
      return;
    }
  });

  /* ── wiring ───────────────────────────────────────────────────────── */

  function wire() {
    $('nav').querySelectorAll('.nav-item').forEach(function (item) {
      item.onclick = function () {
        var target = item.getAttribute('data-nav');
        if (target === 'home') { if (!state.homeLoaded) loadHome(true); else { screen('home'); focusFirst($('home')); } }
        else if (target === 'browse') { loadCategories(function () { openBrowse(); }); }
        else if (target === 'search') openSearch();
        else if (target === 'profile') openProfile();
      };
    });

    $('phoneDisplay').onclick = editPhone;
    $('methodPrev').onclick = function () { cycleMethod(-1); };
    $('methodDisplay').onclick = function () {
      if (state.login.otpFallback || !state.login.methods.length) loadOtpMethods(0);
      else cycleMethod(1);
    };
    $('methodNext').onclick = function () { cycleMethod(1); };
    $('sendCode').onclick = function () { sendCode(); };
    $('toQr').onclick = function () { startQr(0); };
    $('codeDisplay').onclick = editCode;
    $('verifyCode').onclick = verifyCode;
    $('resendCode').onclick = function () { sendCode(true); };
    $('backToPhone').onclick = function () {
      state.login.code = '';
      state.login.tempToken = '';
      $('loginCode').classList.add('hidden');
      $('loginPhone').classList.remove('hidden');
      $('loginTitle').textContent = 'Вход в Belet';
      $('loginHint').textContent = 'Введите номер телефона, привязанный к аккаунту Belet.';
      state.mainFocus = $('phoneDisplay');
      renderCodeValue();
      updatePhoneDisplay();
      if (!state.login.methods.length || state.login.otpFallback) loadOtpMethods(0);
      $('phoneDisplay').focus();
    };
    $('qrRefresh').onclick = function () { startQr(0); };
    $('toPhone').onclick = function () {
      stopQr();
      $('loginQr').classList.add('hidden');
      $('loginPhone').classList.remove('hidden');
      $('loginTitle').textContent = 'Вход в Belet';
      $('loginHint').textContent = 'Введите номер телефона, привязанный к аккаунту Belet.';
      state.mainFocus = $('phoneDisplay');
      updatePhoneDisplay();
      if (!state.login.methods.length || state.login.otpFallback) loadOtpMethods(0);
      $('phoneDisplay').focus();
    };
    $('loginHelp').onclick = openAbout;

    $('numpadDelete').onclick = inputDelete;
    $('numpadClear').onclick = inputClear;
    $('numpadDone').onclick = inputDone;
    $('kbDelete').onclick = inputDelete;
    $('kbClear').onclick = inputClear;
    $('kbDone').onclick = inputDone;

    $('heroPlay').onclick = function () {
      if (!state.home.hero) return;
      openDetail(state.home.hero);
      setTimeout(function () { $('detailPlay').click(); }, 60);
    };
    $('heroInfo').onclick = function () { if (state.home.hero) openDetail(state.home.hero); };
    $('homeReload').onclick = function () { state.homeLoaded = false; loadHome(true); };
    if ($('homePcBridge')) $('homePcBridge').onclick = openPcBridgePrompt;
    $('homeHelp').onclick = openAbout;

    $('browseMoreBtn').onclick = function () { loadCategory(state.browse.id, false); };
    $('searchField').onclick = editSearch;

    $('detailPlay').onclick = playMovieOrResume;
    $('detailFav').onclick = toggleFavorite;

    $('profileRefresh').onclick = function () { loadProfile(0); };
    $('profileFavorites').onclick = openFavorites;
    $('profileHelp').onclick = openAbout;
    $('profileLogout').onclick = logout;

    $('aboutRetest').onclick = runHealthCheck;
    if ($('aboutPcBridge')) $('aboutPcBridge').onclick = openPcBridgePrompt;
    $('aboutBack').onclick = function () {
      if (!Belet.hasSession()) {
        screen('login');
        focusLoginStep();
        return;
      }
      if (state.previous && state.previous !== 'about' && state.previous !== 'login') screen(state.previous);
      else screen('home');
      setTimeout(function () { focusScreen(); }, 40);
    };

    on($('video'), 'timeupdate', updateProgress);
    on($('video'), 'progress', updateProgress);
    on($('video'), 'pause', function () {
      if (state.player.engine !== 'avplay') { showOsd(true); reportProgress(); }
    });
    on($('video'), 'play', function () { hideOsdSoon(); });

    on(window, 'beforeunload', function () { reportProgress(); });
    on(window, 'online', recoverNetworkScreens);
    on(doc, 'visibilitychange', function () {
      if (doc.hidden) reportProgress();
      else {
        tickClock();
        recoverNetworkScreens();
      }
    });
  }

  function recoverNetworkScreens(fromClockSync) {
    if (!isNetworkReady()) return;
    if (state.screen === 'boot' && !state.bootDone) {
      startBootSession();
      return;
    }
    if (state.screen === 'home' && Belet.hasSession()) {
      if (!state.homeLoaded) {
        if (fromClockSync && deviceRegistration.state !== 'сервер принял регистрацию телевизора' &&
            deviceRegistration.state !== 'отправляется…') {
          registerDevice();
        }
        loadHome(false, 0);
        return;
      }
      if (fromClockSync && state.home.groups && state.home.groups.length) {
        renderRows(state.home.groups);
        return;
      }
    }
    if (state.screen === 'detail' && fromClockSync && state.detail && state.detail.movie) {
      renderDetail(state.detail.movie);
      return;
    }
    if (state.screen === 'login' && !state.login.busy) {
      if (!hasClass($('loginQr'), 'hidden') && (!state.qr.code || fromClockSync)) startQr(0);
      else if (!hasClass($('loginPhone'), 'hidden') && (!state.login.methods.length || state.login.otpFallback || fromClockSync)) loadOtpMethods(0);
      return;
    }
    if (state.screen === 'browse' && !state.browse.loading && !state.browse.items.length && Belet.hasSession()) {
      if (state.browse.mode === 'favorites') openFavorites();
      else loadCategories(function () { loadCategory(state.browse.id || 1, true); });
      return;
    }
    if (state.screen === 'profile' && Belet.hasSession()) {
      loadProfile(0);
      return;
    }
    if (state.screen === 'about') {
      if (fromClockSync && Belet.hasSession() &&
          deviceRegistration.state !== 'сервер принял регистрацию телевизора' &&
          deviceRegistration.state !== 'отправляется…') {
        registerDevice();
      }
      runHealthCheck();
    }
  }

  function toggleFavorite() {
    var movie = state.detail.movie;
    var button = $('detailFav');
    var isFavorite;
    if (!movie) return;
    isFavorite = button.getAttribute('data-favorite') === '1';
    button.disabled = true;
    Belet.setFavorite(movie.id, !isFavorite, function (err) {
      button.disabled = false;
      if (err) {
        if (handleAuthError(err)) return;
        toast(describeError(err), 3600);
        return;
      }
      movie.favorites = !isFavorite;
      button.setAttribute('data-favorite', movie.favorites ? '1' : '0');
      button.textContent = movie.favorites ? '✓ В избранном' : 'В избранное';
      toast(movie.favorites ? 'Добавлено в избранное' : 'Удалено из избранного', 2200);
    });
  }

  function registerTvKeys() {
    try {
      if (window.tizen && window.tizen.tvinputdevice) {
        ['MediaPlay', 'MediaPause', 'MediaPlayPause', 'MediaStop', 'MediaRewind', 'MediaFastForward', 'MediaTrackPrevious', 'MediaTrackNext'].forEach(function (name) {
          try { window.tizen.tvinputdevice.registerKey(name); } catch (e) {}
        });
      }
    } catch (e) {}
  }

  function tickClock() {
    var node = $('clock');
    var date = new Date();
    var nowSynced = date.getFullYear() >= 2026;
    var displayDate = date;
    var tz;
    if (nowSynced && isClockBypassed() && !isBypassModeForced()) {
      if (window.Belet && Belet.setClockProxy) Belet.setClockProxy('');
      clockProxyState.base = '';
      clockProxyState.diag = 'не требуется (часы ТВ синхронизировались)';
    }
    if (!nowSynced) {
      tz = readTizenClock();
      if (tz && tz.getFullYear && tz.getFullYear() >= 2026) displayDate = tz;
      else if (clockProxyState.serverSynced) displayDate = new Date(Date.now() + clockProxyState.serverOffsetMs);
    }
    if (node) node.textContent = pad2(displayDate.getHours()) + ':' + pad2(displayDate.getMinutes());
    if (!state.clockSynced && nowSynced && (!isBypassModeForced() || isClockBypassed())) {
      state.clockSynced = true;
      clearClockSyncTimer();
      toast('Время синхронизировано — подключаемся к Belet…', 3000);
      recoverNetworkScreens(true);
    }
  }

  function startBootSession() {
    var session;
    if (state.bootDone) return;
    state.bootDone = true;
    clearBootTimer();
    if (shouldUseBypass() && !isClockBypassed()) scheduleClockWatch();
    session = state.bootSession;
    if (session) {
      $('bootStatus').textContent = 'Восстанавливаем сессию…';
      registerDevice();
      loadHome(true);
    } else {
      $('bootStatus').textContent = 'Требуется вход в аккаунт Belet';
      setTimeout(function () { showLogin(); }, 350);
    }
  }

  function waitForNetworkAtBoot(waited) {
    var netOk = isNetworkReady();
    var clockOk = !isClockBlockingHttps();
    var hasSvc = hasTizenServiceApi();
    var pollMs = (hasSvc && !clockOk) ? 350 : 500;
    var clockBudget = hasSvc
      ? (isClockInvalid() ? 18000 : 8000)
      : (canProbeAnyClockBridge() ? (isClockInvalid() ? 7000 : 2500) : (isClockInvalid() ? 4000 : 0));
    var shouldRetryLaunch = hasSvc && waited > 0 && (waited % 2800 < pollMs);
    if (state.bootDone) return;
    if ((netOk && clockOk) || (!netOk && waited >= 8000) || (netOk && !clockOk && waited >= clockBudget)) {
      if (!isClockInvalid() && isBypassModeForced() && !isClockBypassed()) {
        state.bypassFallbackReady = true;
      }
      startBootSession();
      return;
    }
    if (!clockOk) {
      $('bootStatus').textContent = canProbeAnyClockBridge()
        ? (hasSvc && waited >= 1000
          ? 'Запускаем обход SSL (' + Math.ceil(waited / 1000) + ' с)…'
          : 'Запускаем обход SSL…')
        : 'Ожидаем синхронизацию времени телевизора…';
      if (canProbeAnyClockBridge() && waited > 0) {
        probeClockProxy(function (ok) {
          if (ok && isNetworkReady() && !state.bootDone) startBootSession();
        }, {
          includePc: hasManualPcBridgeHost() || waited >= (isClockInvalid() ? 5600 : 3500),
          includeTvIp: waited >= 4200 && (waited % 2100 < pollMs),
          forceLaunch: shouldRetryLaunch
        });
      }
    }
    state.bootTimer = setTimeout(function () {
      state.bootTimer = null;
      waitForNetworkAtBoot(waited + pollMs);
    }, pollMs);
  }

  function boot() {
    registerTvKeys();
    wire();
    try {
      if (window.tizen && window.tizen.time && window.tizen.time.setDateTimeChangeListener) {
        window.tizen.time.setDateTimeChangeListener(function () { tickClock(); });
      }
    } catch (e) {}
    state.clockSynced = !shouldUseBypass();
    tickClock();
    setInterval(tickClock, 30000);
    updateClockBridgeButtons();
    state.bootSession = Belet.init();
    if (isNetworkReady() && !shouldUseBypass()) {
      startBootSession();
      return;
    }
    if (shouldUseBypass() && canProbeAnyClockBridge()) {
      $('bootStatus').textContent = isClockInvalid()
        ? 'Время ТВ сброшено — запускаем обход SSL…'
        : 'Запускаем обход SSL Belet…';
      probeClockProxy(function (ok) {
        if (ok && isNetworkReady() && !state.bootDone) startBootSession();
      }, {
        includePc: hasManualPcBridgeHost(),
        includeTvIp: false
      });
    } else {
      $('bootStatus').textContent = isNetworkReady()
        ? 'Ожидаем синхронизацию времени телевизора…'
        : 'Ожидаем подключение телевизора к сети…';
    }
    waitForNetworkAtBoot(0);
  }

  if (doc.readyState === 'loading') on(doc, 'DOMContentLoaded', boot);
  else boot();
}());
