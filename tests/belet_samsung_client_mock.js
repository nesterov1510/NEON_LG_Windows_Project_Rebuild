/*
 * Headless harness for the Samsung Belet client.
 *
 * It provides a deliberately small DOM/XMLHttpRequest emulation, loads the real
 * api.js + app.js from the repository and drives the UI the way the remote does.
 * This is a regression test, not a browser: layout, CSS and real playback are out
 * of scope, and the fake service below only models what the app consumes.
 */
'use strict';

const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');

const srcArg = process.argv[2] || path.join(__dirname, '..', 'belet_samsung_native');
// Accept either the client directory or the path of app.js.
const srcDir = srcArg.endsWith('.js') ? path.dirname(srcArg) : srcArg;

const webosQr = process.argv[3] === '--webos-qr';
// Older webOS engines return indexed NodeLists with no native forEach.
function LegacyNodeList(nodes) {
  this.length = nodes.length;
  nodes.forEach((node, i) => { this[i] = node; });
}

/* ── DOM ───────────────────────────────────────────────────────────── */

let document;
const timers = [];
let timerSeq = 0;

function Element(tag) {
  this.tagName = String(tag || 'div').toUpperCase();
  this.children = [];
  this.parentNode = null;
  this.style = {};
  this.attributes = {};
  this.disabled = false;
  this._text = '';
  this._classes = new Set();
  this._listeners = {};
  this.onclick = null;
  this.offsetWidth = 10;
  this.offsetHeight = 10;
  this.value = '';
}

Object.defineProperty(Element.prototype, 'className', {
  get() { return Array.from(this._classes).join(' '); },
  set(value) {
    this._classes = new Set(String(value || '').split(/\s+/).filter(Boolean));
  }
});

Element.prototype.classList = null;

function classListFor(node) {
  return {
    add(name) { node._classes.add(name); },
    remove(name) { node._classes.delete(name); },
    contains(name) { return node._classes.has(name); },
    toggle(name, force) {
      const on = force === undefined ? !node._classes.has(name) : !!force;
      if (on) node._classes.add(name); else node._classes.delete(name);
      return on;
    }
  };
}

Object.defineProperty(Element.prototype, 'classList', {
  get() { return classListFor(this); }
});

Object.defineProperty(Element.prototype, 'textContent', {
  get() { return this._text || this.children.map(c => c.textContent || '').join(''); },
  set(value) { this._text = String(value); this.children = []; }
});

Object.defineProperty(Element.prototype, 'innerHTML', {
  get() { return this._text; },
  set(value) {
    this._text = String(value || '');
    this.children = [];
    if (this._text.indexOf('<') >= 0) {
      // Markup is never read back by the app; keep only a placeholder node.
      const placeholder = new Element('p');
      placeholder.textContent = this._text.replace(/<[^>]+>/g, '');
      this.children = [placeholder];
      placeholder.parentNode = this;
      this._text = '';
    }
  }
});

Object.defineProperty(Element.prototype, 'firstChild', {
  get() { return this.children.length ? this.children[0] : (this._text ? { textContent: this._text } : null); }
});

Element.prototype.appendChild = function (node) {
  if (node.parentNode) node.parentNode.removeChild(node);
  this.children.push(node);
  node.parentNode = this;
  return node;
};

Element.prototype.removeChild = function (node) {
  const index = this.children.indexOf(node);
  if (index >= 0) this.children.splice(index, 1);
  node.parentNode = null;
  return node;
};

Element.prototype.setAttribute = function (name, value) { this.attributes[name] = String(value); };
Element.prototype.getAttribute = function (name) {
  return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
};
Element.prototype.removeAttribute = function (name) {
  delete this.attributes[name];
  if (name === 'src') delete this.src;
};

Element.prototype.addEventListener = function (type, fn) {
  (this._listeners[type] = this._listeners[type] || []).push(fn);
};
Element.prototype.dispatch = function (type, event) {
  (this._listeners[type] || []).forEach(fn => fn(event || {}));
  // Inline handlers (video.onerror = ...) behave like real DOM properties.
  const inline = this['on' + type];
  if (typeof inline === 'function') inline.call(this, event || {});
};

Element.prototype.focus = function () {
  document.activeElement = this;
  this.dispatch('focus');
};
Element.prototype.blur = function () {
  if (document.activeElement === this) document.activeElement = null;
};

Element.prototype.getBoundingClientRect = function () {
  return { left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 };
};
Element.prototype.getClientRects = function () { return [{ width: 10, height: 10 }]; };
Element.prototype.scrollIntoView = function () {};

function matchesSelector(node, selector) {
  const parts = String(selector).split(',').map(s => s.trim()).filter(Boolean);
  return parts.some(part => matchesSimple(node, part));
}

function matchesSimple(node, selector) {
  selector = selector.trim();
  let notHidden = false;
  if (selector.endsWith(':not(.hidden)')) {
    notHidden = true;
    selector = selector.slice(0, -':not(.hidden)'.length);
  }
  const attrMatch = selector.match(/^([a-zA-Z0-9_-]*)\[([a-zA-Z0-9_-]+)="([^"]*)"\]$/);
  let matched;
  if (attrMatch) {
    const tagOk = !attrMatch[1] || node.tagName === attrMatch[1].toUpperCase();
    matched = tagOk && node.getAttribute(attrMatch[2]) === attrMatch[3];
  } else if (selector === '*') matched = true;
  else if (selector.startsWith('#')) matched = node.attributes.id === selector.slice(1);
  else if (selector.startsWith('.')) matched = node._classes.has(selector.slice(1));
  else matched = node.tagName === selector.toUpperCase();
  if (matched && notHidden) matched = !node._classes.has('hidden');
  return matched;
}

Element.prototype.matches = function (selector) { return matchesSelector(this, selector); };

function descendants(node, out) {
  out = out || [];
  (node.children || []).forEach(child => {
    out.push(child);
    descendants(child, out);
  });
  return out;
}

Element.prototype.querySelectorAll = function (selector) {
  const nodes = descendants(this).filter(node => !node._selectorOnly && matchesSelector(node, selector));
  return webosQr ? new LegacyNodeList(nodes) : nodes;
};
Element.prototype.querySelector = function (selector) {
  return this.querySelectorAll(selector)[0] || null;
};

function makeDom() {
  document = new Element('document');
  document.tagName = 'DOCUMENT';
  document.readyState = 'complete';
  document.activeElement = null;
  document._listeners = {};
  document.addEventListener = Element.prototype.addEventListener;
  document.dispatch = Element.prototype.dispatch;
  document.getElementById = function (id) { return document.querySelector('#' + id); };
  document.createElement = tag => new Element(tag);
  document.body = new Element('body');
  document.children = [document.body];
  document.body.parentNode = document;
  document.hidden = false;
}

function add(parent, tag, id, className, text) {
  const node = new Element(tag);
  if (id) node.setAttribute('id', id);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  parent.appendChild(node);
  return node;
}

const ids = {};

function buildDom() {
  makeDom();
  const app = add(document.body, 'div', 'app');

  /* top bar */
  const topbar = add(app, 'header', 'topbar', 'hidden');
  add(topbar, 'img', null, 'topbar-logo');
  const nav = add(topbar, 'nav', 'nav');
  [['home', 'Главная'], ['browse', 'Каталог'], ['search', 'Поиск'], ['profile', 'Профиль']]
    .forEach(([key, label]) => {
      const item = add(nav, 'button', null, 'nav-item focusable', label);
      item.setAttribute('data-nav', key);
    });
  add(topbar, 'div', 'clock', 'clock');

  const main = add(app, 'main', 'main');

  const boot = add(main, 'section', 'boot', 'screen');
  const bootInner = add(boot, 'div', null, 'boot-inner');
  add(bootInner, 'p', 'bootStatus', 'boot-status');

  const login = add(main, 'section', 'login', 'screen hidden');
  const loginCard = add(login, 'div', null, 'login-card');
  add(loginCard, 'h1', 'loginTitle');
  add(loginCard, 'p', 'loginHint');

  const phoneStep = add(loginCard, 'div', 'loginPhone', 'login-step');
  const phoneDisplay = add(phoneStep, 'button', 'phoneDisplay', 'field focusable');
  add(phoneDisplay, 'span', 'phoneValue', 'field-value placeholder');
  add(phoneStep, 'button', 'methodPrev', 'pill focusable');
  const methodDisplay = add(phoneStep, 'button', 'methodDisplay', 'pill wide focusable');
  add(methodDisplay, 'span', 'methodValue');
  add(phoneStep, 'button', 'methodNext', 'pill focusable');
  add(phoneStep, 'button', 'sendCode', 'primary focusable');
  add(phoneStep, 'button', 'toQr', 'ghost focusable');

  const codeStep = add(loginCard, 'div', 'loginCode', 'login-step hidden');
  const codeDisplay = add(codeStep, 'button', 'codeDisplay', 'field focusable');
  add(codeDisplay, 'span', 'codeValue', 'field-value placeholder');
  add(codeStep, 'button', 'verifyCode', 'primary focusable');
  add(codeStep, 'button', 'resendCode', 'ghost focusable');
  add(codeStep, 'button', 'backToPhone', 'ghost focusable');

  const qrStep = add(loginCard, 'div', 'loginQr', 'login-step hidden');
  const qrFrame = add(qrStep, 'div', null, 'qr-frame');
  add(qrFrame, 'img', 'qrImage');
  add(qrStep, 'p', 'qrStatus', 'qr-status');
  add(qrStep, 'button', 'qrRefresh', 'ghost focusable');
  add(qrStep, 'button', 'toPhone', 'ghost focusable');
  add(loginCard, 'button', 'loginHelp', 'link focusable');

  const home = add(main, 'section', 'home', 'screen hidden');
  const hero = add(home, 'div', 'hero', 'hero hidden');
  add(hero, 'img', 'heroImage', 'hero-image');
  add(hero, 'h1', 'heroTitle');
  add(hero, 'p', 'heroMeta', 'hero-meta');
  add(hero, 'p', 'heroText', 'hero-text');
  add(hero, 'button', 'heroPlay', 'primary focusable');
  add(hero, 'button', 'heroInfo', 'ghost focusable');
  add(home, 'div', 'rows', 'rows');
  add(home, 'div', 'homeLoading', 'home-loading hidden');
  const homeEmpty = add(home, 'div', 'homeEmpty', 'empty hidden');
  add(homeEmpty, 'h2');
  add(homeEmpty, 'p');
  add(homeEmpty, 'button', 'homeReload', 'ghost focusable');
  add(homeEmpty, 'button', 'homeHelp', 'ghost focusable');

  const browse = add(main, 'section', 'browse', 'screen hidden');
  add(browse, 'h1', 'browseTitle');
  add(browse, 'div', 'browseChips', 'chips');
  add(browse, 'div', 'browseGrid', 'grid');
  const browseMore = add(browse, 'div', 'browseMore', 'grid-foot hidden');
  add(browseMore, 'button', 'browseMoreBtn', 'ghost focusable');
  const browseEmpty = add(browse, 'div', 'browseEmpty', 'empty hidden');
  add(browseEmpty, 'h2');
  add(browseEmpty, 'p');

  const search = add(main, 'section', 'search', 'screen hidden');
  add(search, 'h1');
  const searchField = add(search, 'button', 'searchField', 'field compact focusable');
  add(searchField, 'span', 'searchValue', 'field-value placeholder');
  add(search, 'div', 'searchGrid', 'grid');
  const searchEmpty = add(search, 'div', 'searchEmpty', 'empty hidden');
  add(searchEmpty, 'h2');
  add(searchEmpty, 'p');

  const detail = add(main, 'section', 'detail', 'screen hidden');
  const detailBackdrop = add(detail, 'div', 'detailBackdrop', 'detail-backdrop');
  add(detailBackdrop, 'img', 'detailBackdropImg', 'detail-backdrop-img');
  const detailScroll = add(detail, 'div', null, 'detail-scroll');
  const detailTop = add(detailScroll, 'div', null, 'detail-top');
  add(detailTop, 'img', 'detailPoster', 'detail-poster');
  const detailInfo = add(detailTop, 'div', null, 'detail-info');
  add(detailInfo, 'h1', 'detailTitle');
  add(detailInfo, 'p', 'detailMeta');
  add(detailInfo, 'p', 'detailGenres');
  add(detailInfo, 'p', 'detailText');
  add(detailInfo, 'button', 'detailPlay', 'primary focusable');
  add(detailInfo, 'button', 'detailFav', 'ghost focusable');
  add(detailInfo, 'button', 'detailTrailer', 'ghost focusable hidden');
  const episodes = add(detailScroll, 'div', 'episodesSection', 'episodes hidden');
  add(episodes, 'div', 'seasonChips', 'chips');
  add(episodes, 'div', 'episodeList', 'episodes-list');

  const player = add(main, 'section', 'player', 'screen hidden');
  const video = add(player, 'video', 'video');
  add(player, 'object', 'avPlayer', 'hidden');
  video.paused = true;
  video.currentTime = 0;
  video.duration = 100;
  video.buffered = { length: 0 };
  video.play = function () { this.paused = false; this.dispatch('play'); return { catch() {} }; };
  video.pause = function () { this.paused = true; this.dispatch('pause'); };
  video.load = function () {};
  const osd = add(player, 'div', 'playerOsd', 'player-osd');
  add(osd, 'h1', 'playerTitle');
  add(osd, 'p', 'playerSubtitle');
  add(osd, 'div', 'playerBadge', 'osd-badge');
  add(osd, 'div', 'progress', 'progress');
  add(osd, 'div', 'progressBuffer', 'progress-buffer');
  add(osd, 'div', 'progressFill', 'progress-fill');
  add(osd, 'div', 'progressKnob', 'progress-knob');
  add(osd, 'div', 'timeNow', 'time');
  add(osd, 'div', 'timeTotal', 'time');
  add(player, 'div', 'playerStatus', 'player-status hidden');
  add(player, 'div', 'playerSpinner', 'player-spinner hidden');

  const profile = add(main, 'section', 'profile', 'screen hidden');
  add(profile, 'h1');
  add(profile, 'h2', 'profilePhone');
  add(profile, 'p', 'profilePlan');
  add(profile, 'button', 'profileRefresh', 'list-item focusable');
  add(profile, 'button', 'profileFavorites', 'list-item focusable');
  add(profile, 'button', 'profileHelp', 'list-item focusable');
  add(profile, 'button', 'profileLogout', 'list-item danger focusable');

  const about = add(main, 'section', 'about', 'screen hidden');
  add(about, 'h1');
  add(about, 'div', 'aboutStatus', 'about-card');
  add(about, 'div', 'aboutClockAlert', 'about-alert hidden');
  add(about, 'button', 'aboutRetest', 'primary focusable');
  add(about, 'button', 'aboutBack', 'ghost focusable');

  const numpad = add(app, 'div', 'numpad', 'overlay hidden');
  const numpadCard = add(numpad, 'div', null, 'pad-card');
  add(numpadCard, 'h2', 'numpadTitle');
  add(numpadCard, 'div', 'numpadValue', 'pad-value');
  add(numpadCard, 'div', 'numpadKeys', 'pad-keys');
  add(numpadCard, 'button', 'numpadDelete', 'ghost focusable');
  add(numpadCard, 'button', 'numpadClear', 'ghost focusable');
  add(numpadCard, 'button', 'numpadDone', 'primary focusable');

  const keyboard = add(app, 'div', 'keyboard', 'overlay hidden');
  const kbCard = add(keyboard, 'div', null, 'pad-card wide-card');
  add(kbCard, 'h2', 'keyboardTitle');
  add(kbCard, 'div', 'keyboardValue', 'pad-value');
  add(kbCard, 'div', 'keyboardKeys', 'kb-keys');
  add(kbCard, 'button', 'kbDelete', 'ghost focusable');
  add(kbCard, 'button', 'kbClear', 'ghost focusable');
  add(kbCard, 'button', 'kbDone', 'primary focusable');

  const dialog = add(app, 'div', 'dialog', 'overlay hidden');
  const dialogCard = add(dialog, 'div', null, 'dialog-card');
  add(dialogCard, 'h2', 'dialogTitle');
  add(dialogCard, 'p', 'dialogText');
  add(dialogCard, 'div', 'dialogActions', 'dialog-actions');
  add(app, 'div', 'toast', 'toast hidden');

  ['app', 'topbar', 'clock', 'nav', 'boot', 'bootStatus', 'login', 'loginTitle', 'loginHint', 'loginPhone',
    'phoneDisplay', 'phoneValue', 'methodPrev', 'methodDisplay', 'methodValue', 'methodNext', 'sendCode', 'toQr',
    'loginCode', 'codeDisplay', 'codeValue', 'verifyCode', 'resendCode', 'backToPhone', 'loginQr', 'qrImage',
    'qrStatus', 'qrRefresh', 'toPhone', 'loginHelp', 'home', 'hero', 'heroImage', 'heroTitle', 'heroMeta',
    'heroText', 'heroPlay', 'heroInfo', 'rows', 'homeLoading', 'homeEmpty', 'homeReload', 'homeHelp', 'browse', 'browseTitle', 'browseChips',
    'browseGrid', 'browseMore', 'browseMoreBtn', 'browseEmpty', 'search', 'searchField', 'searchValue',
    'searchGrid', 'searchEmpty', 'detail', 'detailBackdrop', 'detailBackdropImg', 'detailPoster', 'detailTitle', 'detailMeta',
    'detailGenres', 'detailText', 'detailPlay', 'detailFav', 'detailTrailer', 'episodesSection', 'seasonChips',
    'episodeList', 'player', 'video', 'playerOsd', 'playerTitle', 'playerSubtitle', 'playerBadge', 'progress',
    'progressBuffer', 'progressFill', 'progressKnob', 'timeNow', 'timeTotal', 'playerStatus', 'playerSpinner',
    'profile', 'profilePhone', 'profilePlan', 'profileRefresh', 'profileFavorites', 'profileHelp',
    'profileLogout', 'about', 'aboutStatus', 'aboutClockAlert', 'aboutRetest', 'aboutBack', 'numpad', 'numpadTitle', 'numpadValue',
    'numpadKeys', 'numpadDelete', 'numpadClear', 'numpadDone', 'keyboard', 'keyboardTitle', 'keyboardValue',
    'keyboardKeys', 'kbDelete', 'kbClear', 'kbDone', 'dialog', 'dialogTitle', 'dialogText', 'dialogActions',
    'toast', 'avPlayer'].forEach(id => { ids[id] = document.getElementById(id); });
}

/* ── fake service ──────────────────────────────────────────────────── */

const calls = [];
let store = {};
let routes = [];
const blobRecords = {};
let blobSequence = 0;

class MockBlob {
  constructor(parts, options) {
    this.parts = parts;
    this.type = options && options.type || '';
  }
}

function route(match, status, json) {
  routes.push({ match, status, json });
}

function routeText(match, status, text) {
  routes.push({ match, status, text });
}

function headerOf(xhr, name) {
  const keys = Object.keys(xhr.headers);
  for (const key of keys) if (key.toLowerCase() === name.toLowerCase()) return xhr.headers[key];
  return undefined;
}

class XHR {
  constructor() { this.headers = {}; this.readyState = 0; this.status = 0; }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(key, value) {
    assert.strictEqual(headerOf(this, key), undefined, 'header set twice (would concatenate in Chromium): ' + key);
    this.headers[key] = String(value);
  }
  send(body) {
    calls.push({
      method: this.method,
      url: this.url,
      body: body || null,
      headers: Object.assign({}, this.headers)
    });
    const index = routes.findIndex(r => (r.match instanceof RegExp
      ? r.match.test(this.url)
      : this.url.indexOf(r.match) >= 0));
    if (index < 0) throw new Error('unexpected request: ' + this.method + ' ' + this.url);
    const item = routes.splice(index, 1)[0];
    this.status = item.status;
    this.responseText = item.text !== undefined ? item.text : (item.json === undefined ? '' : JSON.stringify(item.json));
    this.readyState = 4;
    if (this.onreadystatechange) this.onreadystatechange();
  }
}

/* ── boot the app ──────────────────────────────────────────────────── */

buildDom();

const sandbox = {
  document,
  XMLHttpRequest: XHR,
  localStorage: {
    getItem: key => (Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null),
    setItem: (key, value) => { store[key] = String(value); },
    removeItem: key => { delete store[key]; }
  },
  setTimeout: (fn, delay) => {
    if (delay !== undefined && delay <= 60) { fn(); return 0; }
    const id = ++timerSeq;
    timers.push({ id, fn, delay: delay || 0 });
    return id;
  },
  clearTimeout: id => {
    const index = timers.findIndex(t => t.id === id);
    if (index >= 0) timers.splice(index, 1);
  },
  setInterval: () => 0,
  clearInterval: () => {},
  Blob: MockBlob,
  URL: {
    createObjectURL(blob) {
      const url = 'blob:belet-' + (++blobSequence);
      blobRecords[url] = blob;
      return url;
    },
    revokeObjectURL(url) { delete blobRecords[url]; }
  },
  console,
  Math,
  Date,
  JSON,
  Number,
  String,
  Array,
  Object,
  isFinite,
  parseInt,
  parseFloat,
  encodeURIComponent,
  decodeURIComponent
};
const windowListeners = {};
sandbox.window = sandbox;
sandbox.global = sandbox;
sandbox.self = sandbox;
sandbox.navigator = { userAgent: 'Tizen' };
sandbox.addEventListener = (event, fn) => {
  (windowListeners[event] = windowListeners[event] || []).push(fn);
};
function dispatchWindow(event) {
  (windowListeners[event] || []).forEach(fn => fn({ type: event }));
}

let nativeRegistration;
vm.createContext(sandbox);
if (webosQr) {
  sandbox.navigator.userAgent = 'Mozilla/5.0 (Web0S; Linux/SmartTV) Chrome/38.0';
  sandbox.NodeList = LegacyNodeList;
  const methods = {};
  sandbox.PalmSystem = { deviceInfo: { modelName: 'OLED55C9', platform: 'webOS', version: '4.9' } };
  const serviceId = 'tm.belet.neonclient.qr';
  const replies = [[503, {}], [400, {}], [200, { token: 'qr-access', refresh_token: 'qr-refresh' }]];
  const dependencies = {
    './package.json': { name: serviceId },
    'webos-service': function () { this.register = (method, fn) => { methods[method] = fn; }; },
    https: { request(opts, cb) {
      assert.strictEqual(opts.hostname, 'beletapis.com');
      const registering = opts.path === '/api/v1/users/device';
      if (!registering) assert.strictEqual(opts.path, '/api/v1/auth/check-qrcode');
      assert.strictEqual(opts.method, 'POST');
      return { on() {}, end(body) {
        if (registering) {
          nativeRegistration = { body: JSON.parse(body), token: opts.headers.Authorization };
        } else assert.deepStrictEqual(JSON.parse(body), { code: 'qr-code-1', fingerprint: store.neon_belet_samsung_device_v2 });
        assert.strictEqual(opts.headers.fingerprint, JSON.parse(body).fingerprint);
        const [statusCode, data] = registering ? [200, {}] : replies.shift();
        cb({ statusCode, setEncoding() {}, on(event, fn) {
          if (event === 'data') fn(JSON.stringify(data));
          if (event === 'end') fn();
        } });
      } };
    } }
  };
  vm.runInNewContext(fs.readFileSync(path.join(srcDir, 'qr.js'), 'utf8'), {
    require: name => dependencies[name], Buffer, setTimeout, clearTimeout
  });
  sandbox.PalmServiceBridge = function () {
    this.call = (url, args) => {
      const prefix = 'luna://' + serviceId + '/';
      assert(url.startsWith(prefix));
      methods[url.slice(prefix.length)]({ payload: JSON.parse(args), respond: r => this.onservicecallback(JSON.stringify(r)) });
    };
  };
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'webos-shim.js'), 'utf8'), sandbox, { filename: 'webos-shim.js' });
}
vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), sandbox, { filename: 'api.js' });
vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), sandbox, { filename: 'app.js' });

function fireKey(code) {
  let prevented = false;
  document.dispatch('keydown', {
    keyCode: code,
    preventDefault() { prevented = true; }
  });
  return prevented;
}

function runTimers(limit) {
  // Only timers that existed when called; polling timers must not re-enter the same flush.
  const due = timers.splice(0, limit || 50);
  due.forEach(item => item.fn());
}

function click(node) {
  assert(node, 'element to click is missing');
  if (node.onclick) node.onclick({ preventDefault() {} });
}

function byText(host, text) {
  return host.querySelectorAll('*').find(node => node.textContent === text);
}

/* ── scenario ──────────────────────────────────────────────────────── */

/* 1. Login screen and OTP methods */
route('/api/v1/auth/available-otp', 200, { otp: [{ name: 'SMS', type: 2 }, { name: 'Звонок', type: 1 }] });
runTimers();
assert(!ids.login.classList.contains('hidden'), 'login is shown without a session');
assert.strictEqual(ids.methodValue.textContent, 'SMS');
assert.strictEqual(calls.filter(c => c.url.indexOf('available-otp') >= 0).length, 1);

function testQrLogin() {
  route('/api/v1/auth/get-qrcode', 200, {
    code: 'https://film.belet.me/register/qr-code-1',
    qr_png: 'iVBORw0KGgoAAAANSUhEUg=='
  });
  click(ids.toQr);
  assert(!ids.loginQr.classList.contains('hidden'));
  assert(ids.qrImage.src.indexOf('data:image/png;base64,') === 0);
  const qrCheckRoute = timers.find(t => t.delay >= 2000);
  assert(qrCheckRoute, 'QR polling scheduled');
  if (webosQr) {
    runTimers();
    assert(ids.qrStatus.textContent.includes('503'), 'transient service error is retried');
  } else route('/api/v1/auth/check-qrcode', 400, { message: 'waiting' });
  runTimers();
  if (!webosQr) route('/api/v1/auth/check-qrcode', 200, { token: 'qr-access', refresh_token: 'qr-refresh' });
  route('/api/v1/users/device', 200, { msg: 'Successfully added' });
  route('/api/v2/home_page', 200, { status: 'ok', result: [] });
  runTimers();
  assert.strictEqual(JSON.parse(store.neon_belet_samsung_session_v2).token, 'qr-access');
  assert(!ids.home.classList.contains('hidden'), 'QR login opens home');
}

if (webosQr) {
  testQrLogin();
  assert.deepStrictEqual(nativeRegistration, { token: 'qr-access', body: { model: 'LG OLED55C9',
    fingerprint: JSON.parse(store.neon_belet_samsung_session_v2).fingerprint, os_version: 'webOS 4.9', app_version: '4.3.3' } });
  let exits = 0;
  sandbox.PalmSystem = { platformBack() { exits++; } };
  route('/api/v1/categories', 200, { categories: [{ id: 1, name: 'Films' }] });
  route(/type_id=1&/, 200, { status: 'ok', result: [{ items: [{ id: 21, type_id: 1, name: 'Film' }] }] });
  click(ids.nav.querySelectorAll('.nav-item')[1]);
  route('/api/v3/movie/21', 200, { movie: { id: 21, name: 'Film' } });
  click(ids.browseGrid.querySelector('.card'));
  assert(!ids.detail.classList.contains('hidden'));
  assert(fireKey(461));
  assert(!ids.browse.classList.contains('hidden'), 'LG Back returns to the catalogue');
  assert(fireKey(461));
  assert(!ids.home.classList.contains('hidden'));
  assert(fireKey(461));
  assert.strictEqual(exits, 0, 'first Back on home warns rather than exiting');
  assert(fireKey(461));
  assert.strictEqual(exits, 1, 'confirmed exit goes to webOS exactly once');
  const restored = JSON.parse(store.neon_belet_samsung_session_v2);
  restored.fingerprint = 'tv' + 'a'.repeat(32);
  store.neon_belet_samsung_session_v2 = JSON.stringify(restored);
  sandbox.Belet.init();
  assert.strictEqual(store.neon_belet_samsung_device_v2, restored.fingerprint, 'restore repairs the install identifier');
  sandbox.Belet.forget();
  sandbox.Belet.init();
  assert.strictEqual(sandbox.Belet.device(), restored.fingerprint, 'legacy device ID survives logout and relaunch');
  console.log('webos IPK QR login and Back navigation: ok');
  process.exit(0);
}

/* 2. Phone entry through the on-screen numeric pad */
click(ids.phoneDisplay);
assert(!ids.numpad.classList.contains('hidden'), 'numpad opens');
const phoneDigits = '99362560726';
phoneDigits.split('').forEach(digit => click(byText(ids.numpadKeys, digit)));
click(ids.numpadDone);
assert.strictEqual(ids.phoneValue.textContent, '+99362560726');
assert(!ids.sendCode.disabled);

/* 3. Request the SMS code */
route('/api/v1/auth/sign-in?sign_in_type=2', 200, { token: 'temp-token' });
click(ids.sendCode);
const signIn = calls.find(c => c.url.indexOf('/auth/sign-in') >= 0);
assert.strictEqual(signIn.method, 'POST');
assert.strictEqual(signIn.body, JSON.stringify({ phone: 99362560726 }));
assert(signIn.headers.fingerprint && signIn.headers.fingerprint.length >= 16, 'device fingerprint header');
assert.strictEqual(signIn.headers['X-Platform'], 'Android');
assert.strictEqual(signIn.headers.Lang, 'ru');
assert(!ids.loginCode.classList.contains('hidden'), 'code step is shown');

/* 4. Confirm the code and receive the session */
route('/api/v1/auth/check-code-mobile', 200, { token: 'access-token', refresh_token: 'refresh-token' });
route('/api/v1/users/device', 200, { msg: 'Successfully added' });
route('/api/v2/home_page', 200, {
  status: 'ok',
  result: [
    {
      id: 1, title: 'Главное', source: 'header', items: [
        { id: 10, type_id: 1, name: 'Hero Film', description: 'Описание', age: '16', rating_kp: 7.5,
          images: { horizontal_with_name: { high: { url: 'https://img.example/hero.jpg' } },
            vertical: { medium: { url: 'https://img.example/hero-poster.jpg' } } } }
      ]
    },
    {
      id: 2, title: 'Продолжить просмотр', source: 'continue_watching', items: [
        { id: 11, type_id: 1, name: 'Continue Film', duration: '01:40:00', watch_time: 600,
          images: { vertical: { medium: { url: 'https://img.example/c.jpg' } } } }
      ]
    },
    {
      id: 3, title: 'Смотрят сегодня', source: 'top', items: [
        { id: 12, type_id: 2, name: 'Serial', images: { vertical: { medium: { url: 'https://img.example/s.jpg' } } } }
      ]
    }
  ]
});
click(ids.codeDisplay);
'12345'.split('').forEach(digit => click(byText(ids.numpadKeys, digit)));
click(ids.numpadDone);
runTimers();
assert.strictEqual(calls.find(c => c.url.indexOf('check-code-mobile') >= 0).body,
  JSON.stringify({ code: '12345', fingerprint: JSON.parse(store.neon_belet_samsung_session_v2).fingerprint, token: 'temp-token' }));
assert.strictEqual(JSON.parse(store.neon_belet_samsung_session_v2).token, 'access-token');
const deviceCall = calls.find(c => c.url.indexOf('/api/v1/users/device') >= 0);
assert(deviceCall, 'TV registers itself in the account device list');
assert.strictEqual(deviceCall.method, 'POST');
assert(deviceCall.body.indexOf('"app_version":"4.3.3"') >= 0, 'device payload has app_version');
assert(deviceCall.body.indexOf('Samsung Tizen') >= 0, 'device payload has the model name');
const deviceBody = JSON.parse(deviceCall.body);
assert(deviceBody.fingerprint === JSON.parse(store.neon_belet_samsung_session_v2).fingerprint,
  'device payload repeats the install fingerprint (DeviceBody on Android does)');
assert(deviceBody.os_version, 'device payload has os_version');
assert(deviceCall.headers['X-Device-Model'], 'device request names the TV model');
assert(!ids.home.classList.contains('hidden'), 'home opens after login');
const rows = ids.rows.querySelectorAll('.row-block');
assert.strictEqual(rows.length, 2, 'two content rows');
assert.strictEqual(ids.heroTitle.textContent, 'Hero Film');
assert.strictEqual(ids.rows.querySelector('.card'), ids.rows.querySelector('.row-block').querySelector('.card'));

/* 5. Detail + series episodes */
route('/api/v3/movie/12', 200, {
  status: 'ok',
  movie: {
    id: 12, name: 'Serial', type_id: 2, description: 'Сериал', year: 2024, age: '18',
    genres: ['Драма'], favorites: false, watch_time: 120,
    images: { vertical: { medium: { url: 'https://img.example/s.jpg' } },
      horizontal_with_name: { high: { url: 'https://img.example/s-bg.jpg' } } },
    seasons: [{ id: 5, name: 'Сезон 1' }],
    last_episode_info: { episode_id: 501, season_id: 5, episode_position: 2, episode_name: 'Серия 2' }
  }
});
route('/api/v2/episodes', 200, { status: 'ok', result: [{ id: 501, name: 'Серия 2', episode_position: 2, duration: '00:45:00' }] });
click(ids.rows.querySelectorAll('.card')[1]);
assert(!ids.detail.classList.contains('hidden'));
assert.strictEqual(ids.detailTitle.textContent, 'Serial');
assert(ids.detailPlay.textContent.indexOf('Продолжить') === 0, 'resume label');
assert.strictEqual(ids.seasonChips.querySelectorAll('.chip').length, 1);
assert.strictEqual(ids.episodeList.querySelectorAll('.episode').length, 1);

/* 6. Playback: files lookup, HLS source and player keys */
route('/api/v3/files/501?type=4', 200, { sources: [
  { filename: 'https://cdn.example/low.m3u8', quality: '720p' },
  { filename: 'https://cdn.example/high.m3u8', quality: '1080p' }
] });
click(ids.episodeList.querySelectorAll('.episode')[0]);
assert.strictEqual(ids.video.src, 'https://cdn.example/high.m3u8');
assert(!ids.player.classList.contains('hidden'));
assert(fireKey(19) === true, 'pause is consumed by the player');
assert.strictEqual(ids.video.paused, true);
fireKey(415);
assert.strictEqual(ids.video.paused, false);
ids.video.currentTime = 40;
fireKey(37);
assert.strictEqual(ids.video.currentTime, 30);
fireKey(39);
assert.strictEqual(ids.video.currentTime, 40);

/* 7. Progress is reported to the film API */
ids.video.currentTime = 120;
ids.video.duration = 2700;
route('/api/v3/lastWatchTime', 200, { status: 'ok' });
ids.video.dispatch('pause');
const progressCalls = calls.filter(c => c.url.indexOf('lastWatchTime') >= 0);
const progressCall = progressCalls[progressCalls.length - 1];
assert(progressCall, 'progress request');
assert.strictEqual(progressCall.method, 'POST');
assert(progressCall.body.indexOf('movie_id=12') >= 0);
assert(progressCall.body.indexOf('watch_time=120') >= 0);
assert(progressCalls.length >= 2, 'seeking also reports progress');

/* 8. Leave the player and the detail screen */
fireKey(10009);
assert(!ids.detail.classList.contains('hidden'), 'back returns to detail');
fireKey(10009);
assert(!ids.home.classList.contains('hidden'), 'back returns to home');

/* 9. Favourites toggle */
route('/api/v3/movie/11', 200, { status: 'ok', movie: { id: 11, name: 'Continue Film', type_id: 1, favorites: false } });
route('/api/v1/favorites?movieId=11', 200, { status: 'ok' });
click(ids.rows.querySelectorAll('.card')[0]);
click(ids.detailFav);
assert(calls.find(c => c.url.indexOf('favorites?movieId=11') >= 0).method === 'POST');
assert.strictEqual(ids.detailFav.textContent, '✓ В избранном');

/* 9b. A serial without seasons in its payload asks the seasons endpoint */
fireKey(10009);
assert(!ids.home.classList.contains('hidden'), 'back to home before the serial test');
route('/api/v3/movie/12', 200, {
  status: 'ok',
  movie: { id: 12, name: 'Serial', type_id: '2', description: 'Сериал', favorites: true,
    images: {}, seasons: [], episode_info: null }
});
route('/api/v2/seasons', 200, { status: 'ok', result: [{ season_id: 7, season_name: 'Сезон 1' }] });
route('/api/v2/episodes', 404, { message: 'not found' });
route('/api/v2/episodes?seasonId=', 404, { message: 'not found' });
route('/api/v3/episodes', 200, { status: 'ok', result: [
  { episode_id: 701, name: 'Пилот', episode_position: 1, duration: 2700, type_id: 4 }
] });
route('/api/v3/files/701?type=4', 200, { sources: [] });
route('/api/v3/files/701?type_id=4', 200, { result: [
  { hls: 'https://cdn.example/a.m3u8', quality: '720p' },
  { hls: 'https://cdn.example/b.m3u8', quality: '1080p' }
] });
click(ids.rows.querySelectorAll('.row-block')[1].querySelector('.card'));
assert.strictEqual(ids.seasonChips.querySelectorAll('.chip').length, 1, 'season fetched from the API');
assert.strictEqual(ids.episodeList.querySelectorAll('.episode').length, 1, 'episodes fetched for that season');

/* 9c. Play on a serial starts the first episode; a broken source falls back */
click(ids.detailPlay);
assert(!ids.player.classList.contains('hidden'), 'player opens for the first episode');
assert.strictEqual(ids.video.src, 'https://cdn.example/b.m3u8', 'best quality first');
ids.video.dispatch('error');
assert.strictEqual(ids.video.src, 'https://cdn.example/a.m3u8', 'falls back to the next source');
ids.video.currentTime = 90;
ids.video.duration = 2700;
route('/api/v3/lastWatchTime', 200, { status: 'ok' });
ids.video.dispatch('pause');
const episodeProgress = calls.filter(c => c.url.indexOf('lastWatchTime') >= 0).pop();
assert.strictEqual(episodeProgress.body.indexOf('season_id=7') > 0, true, 'progress carries the season');
fireKey(10009);

/* 9d. Tizen AVPlay is preferred over the HTML5 video element */
const avplayCalls = [];
let avplayFail = false;
sandbox.webapis = {
  avplay: {
    open(url) { avplayCalls.push(['open', url]); this.url = url; },
    setStreamingProperty(name, value) { avplayCalls.push(['property', name, value]); },
    setDisplayRect() {},
    setListener(listener) { this.listener = listener; },
    prepareAsync(next) {
      avplayCalls.push(['prepare', this.url]);
      if (avplayFail) { this.listener.onerror(); return; }
      next();
    },
    play() { avplayCalls.push(['play']); },
    pause() { avplayCalls.push(['pause']); },
    stop() { avplayCalls.push(['stop']); },
    close() {},
    seekTo(ms) { avplayCalls.push(['seek', ms]); },
    getCurrentTime() { return 5000; },
    getDuration() { return 2700000; }
  }
};
route('/api/v3/files/701?type=4', 200, { sources: [
  { filename: 'https://cdn.example/a.m3u8', quality: '720p' },
  { filename: 'https://cdn.example/b.m3u8', quality: '1080p' }
] });
click(ids.episodeList.querySelectorAll('.episode')[0]);
assert.strictEqual(avplayCalls[0][0], 'open', 'avplay opens the stream');
assert.strictEqual(avplayCalls[0][1], 'https://cdn.example/b.m3u8', 'best quality on avplay');
assert(avplayCalls.some(call => call[0] === 'play'), 'avplay starts playback');
assert(!ids.avPlayer.classList.contains('hidden'), 'avplay surface is visible');
assert(ids.video.classList.contains('hidden'), 'video element is hidden while avplay runs');
assert(fireKey(19) === true);
assert.strictEqual(avplayCalls.filter(call => call[0] === 'pause').length, 1, 'pause goes to avplay');
fireKey(10009);

/* 9e. When AVPlay rejects a source the client retries it with <video>, then moves on */
avplayFail = true;
avplayCalls.length = 0;
route('/api/v3/files/701?type=4', 200, { sources: [
  { filename: 'https://cdn.example/a.m3u8', quality: '720p' },
  { filename: 'https://cdn.example/b.m3u8', quality: '1080p' }
] });
click(ids.episodeList.querySelectorAll('.episode')[0]);
assert.strictEqual(avplayCalls.filter(call => call[0] === 'open').length, 1, 'avplay tried the best source');
assert(ids.avPlayer.classList.contains('hidden'), 'avplay surface closed after failure');
assert.strictEqual(ids.video.src, 'https://cdn.example/b.m3u8', 'the same source falls back to the video element');
ids.video.dispatch('error');
assert.strictEqual(ids.video.src, 'https://cdn.example/a.m3u8', 'a broken source moves to the next one');
ids.video.dispatch('error');
assert(!ids.playerStatus.classList.contains('hidden'), 'playback failure is shown');
assert(ids.playerStatus.textContent.indexOf('Не удалось воспроизвести') >= 0, 'failure message explains what broke');
/* OK retries every engine/source combination without another API call */
const retryMark = avplayCalls.length;
fireKey(13);
assert(avplayCalls.length > retryMark, 'OK retries playback after a failure');
assert.strictEqual(ids.video.src, 'https://cdn.example/b.m3u8', 'retry starts from the best source again');
fireKey(10009);

/* 9g. HLS is played through hls.js (MSE) when the engine supports it */
class FakeHls {
  constructor(config) {
    this.config = config;
    this.handlers = {};
    FakeHls.instances.push(this);
  }
  on(event, callback) { this.handlers[event] = callback; }
  attachMedia(video) {
    this.video = video;
    if (this.handlers.mediaAttached) this.handlers.mediaAttached();
  }
  loadSource(url) {
    this.url = url;
    if (this.handlers.manifestParsed) this.handlers.manifestParsed();
  }
  destroy() { this.destroyed = true; }
}
class FakeHlsLoader {
  constructor() { this.stats = {}; }
  load(context, config, callbacks) { this.context = context; this.callbacks = callbacks; }
  abort() {}
  destroy() {}
  getCacheAge() { return null; }
  getResponseHeader() { return null; }
}
FakeHls.instances = [];
FakeHls.isSupported = () => true;
FakeHls.DefaultConfig = { loader: FakeHlsLoader };
FakeHls.Events = { MEDIA_ATTACHED: 'mediaAttached', MANIFEST_PARSED: 'manifestParsed', LEVEL_LOADED: 'levelLoaded', ERROR: 'error' };
sandbox.Hls = FakeHls;
avplayFail = true;
avplayCalls.length = 0;
route('/api/v3/files/701?type=4', 200, { sources: [
  { filename: 'https://cdn.example/a.m3u8', quality: '720p' },
  { filename: 'https://cdn.example/video/high?token=signed', type: 'application/vnd.apple.mpegurl', quality_label: '1080p' }
], session_id: 'belet-player-session' });
click(ids.episodeList.querySelectorAll('.episode')[0]);
const hlsPlayer = FakeHls.instances[FakeHls.instances.length - 1];
assert(hlsPlayer, 'hls.js instance created');
assert.strictEqual(hlsPlayer.url, 'https://cdn.example/video/high?token=signed',
  'hls.js opens the selected original Belet source directly');
assert.strictEqual(hlsPlayer.config.enableWorker, false, 'no blob workers, CSP stays strict');
assert.strictEqual(ids.video.paused, false, 'hls.js playback started on the video element');
assert.strictEqual(avplayCalls.length, 0, 'Belet HLS/MSE is preferred before AVPlay');
/* a fatal HLS error degrades to the next engine without losing the position */
hlsPlayer.handlers.error({}, { fatal: true, details: 'manifestLoadError' });
assert.strictEqual(hlsPlayer.destroyed, true, 'broken hls.js instance is destroyed');
assert.strictEqual(ids.video.src, 'https://cdn.example/video/high?token=signed', 'native video is the final fallback');
fireKey(10009);

/* 9e-2. Media requests carry the API auth headers and a 401 replays them after a refresh */
route('/api/v3/files/701?type=4', 200, { sources: [
  { filename: 'https://cdn.example/c.m3u8', quality: '1080p' }
], session_id: 'belet-player-session' });
click(ids.episodeList.querySelectorAll('.episode')[0]);
const authedPlayer = FakeHls.instances[FakeHls.instances.length - 1];
const sessionToken = JSON.parse(store.neon_belet_samsung_session_v2).token;
const hlsLoader = new authedPlayer.config.loader({});
const hlsContext = { url: 'https://cdn.example/c.m3u8', headers: {} };
const refreshCountBeforeMediaLoad = calls.filter(c => c.url.indexOf('/api/v2/auth/refresh-mobile') >= 0).length;
hlsLoader.load(hlsContext, {}, {});
assert.strictEqual(calls.filter(c => c.url.indexOf('/api/v2/auth/refresh-mobile') >= 0).length,
  refreshCountBeforeMediaLoad, 'media requests use the current token without refreshing on every playlist/segment');
assert.strictEqual(hlsLoader.loader.context.headers.Authorization, sessionToken, 'manifest/segments ask with the access token');
assert.deepStrictEqual(Object.keys(hlsLoader.loader.context.headers), [
  'X-Platform', 'X-VersionCode', 'X-Player-Session-Id', 'fingerprint', 'Authorization'
], 'media requests match the Android ExoPlayer headers');
assert.strictEqual(hlsLoader.loader.context.headers['X-Platform'], 'Android');
assert.strictEqual(hlsLoader.loader.context.headers['X-VersionCode'], '182');
assert.strictEqual(hlsLoader.loader.context.headers.fingerprint, JSON.parse(store.neon_belet_samsung_session_v2).fingerprint,
  'the Android fingerprint header is sent on manifests');
authedPlayer.handlers.levelLoaded({}, { level: 2, details: { totalduration: 15, fragments: [{}, {}, {}] } });
const fragmentLoader = new authedPlayer.config.fLoader({});
fragmentLoader.load({ url: 'https://cdn.example/segment.ts', headers: {}, frag: { level: 2 } }, {}, {});
assert.strictEqual(fragmentLoader.loader.context.headers['X-Chunk-Length'], '5000',
  'fragment requests include the level chunk duration');

/* first 401 refreshes the token before replaying the Belet web-player request */
const instancesBefore = FakeHls.instances.length;
route('/api/v2/auth/refresh-mobile', 200, { token: 'media-token', refresh_token: 'media-refresh' });
authedPlayer.handlers.error({}, { fatal: true, details: 'manifestLoadError', response: { code: 401 } });
const replay = FakeHls.instances[FakeHls.instances.length - 1];
assert.strictEqual(FakeHls.instances.length, instancesBefore + 1, 'a media 401 restarts hls.js');
assert.strictEqual(replay.url, 'https://cdn.example/c.m3u8', 'the original manifest URL is replayed after token refresh');
const replayLoader = new replay.config.loader({});
replayLoader.load({ url: 'https://cdn.example/c.m3u8', headers: {} }, {}, {});
assert.strictEqual(replayLoader.loader.context.headers.Authorization, 'media-token', 'the retry uses the refreshed raw token');
assert.strictEqual(JSON.parse(store.neon_belet_samsung_session_v2).token, 'media-token', 'the access token was refreshed');

/* a further 401 tries the CDN's Bearer scheme after the refreshed raw token */
const instancesAfterScheme = FakeHls.instances.length;
replay.handlers.error({}, { fatal: true, details: 'manifestLoadError', response: { code: 401 } });
const replay2 = FakeHls.instances[FakeHls.instances.length - 1];
assert.strictEqual(FakeHls.instances.length, instancesAfterScheme + 1, 'the Bearer fallback restarts hls.js');
assert.strictEqual(replay2.url, 'https://cdn.example/c.m3u8');
assert.strictEqual(JSON.parse(store.neon_belet_samsung_session_v2).token, 'media-token', 'the refreshed session is retained');
const replayLoader2 = new replay2.config.loader({});
replayLoader2.load({ url: 'https://cdn.example/c.m3u8', headers: {} }, {}, {});
assert.strictEqual(replayLoader2.loader.context.headers.Authorization, 'Bearer media-token', 'the retry supports CDNs that require Bearer');
fireKey(10009);

sandbox.Hls = undefined;
avplayFail = false;

/* 9f. Catalog tab: categories, a category grid and paging */
route('/api/v1/categories', 200, { categories: [{ id: 1, name: 'Фильмы' }, { id: 2, name: 'Сериалы' }] });
route(/type_id=1&/, 200, { status: 'ok', result: [
  { id: 501, title: 'Смотрят сегодня', source: 'top', items: [
    { id: 21, type_id: 1, name: 'Film A', images: { vertical: { medium: { url: 'https://img.example/a.jpg' } } } },
    { id: 22, type_id: 1, name: 'Film B' }
  ] }
] });
click(ids.nav.querySelectorAll('.nav-item')[1]);
assert(!ids.browse.classList.contains('hidden'), 'catalog opens');
let tizenExits = 0;
sandbox.tizen = { application: { getCurrentApplication() { return { exit() { tizenExits++; } }; } } };
route('/api/v3/movie/21', 200, { movie: { id: 21, type_id: 1, name: 'Film A' } });
click(ids.browseGrid.querySelector('.card'));
assert(!ids.detail.classList.contains('hidden'));
assert(fireKey(10009), 'Samsung Return is consumed in detail');
assert(!ids.browse.classList.contains('hidden'), 'Samsung Return restores the catalogue');
assert.strictEqual(tizenExits, 0, 'Samsung Return did not leave the app');
assert.strictEqual(ids.browseChips.querySelectorAll('.chip').length, 2, 'category chips');
assert.strictEqual(ids.browseTitle.textContent, 'Фильмы');
assert.strictEqual(ids.browseGrid.querySelectorAll('.card').length, 2, 'category rows are flattened into the grid');

route(/type_id=1&/, 200, { status: 'ok', result: [
  { id: 502, title: 'Ещё', source: 'top', items: [{ id: 23, type_id: 1, name: 'Film C' }] }
] });
click(ids.browseMoreBtn);
assert.strictEqual(ids.browseGrid.querySelectorAll('.card').length, 3, 'more button appends a page');
assert(!ids.browseMore.classList.contains('hidden'), 'paging stays available while rows arrive');

route(/type_id=1&/, 200, { status: 'ok', result: [] });
click(ids.browseMoreBtn);
assert(ids.browseMore.classList.contains('hidden'), 'paging stops on an empty page');
fireKey(10009);
assert(!ids.home.classList.contains('hidden'), 'back leaves the catalog');

/* 10. Search through the TV keyboard */
click(ids.nav.querySelectorAll('.nav-item')[2]);
assert(!ids.search.classList.contains('hidden'));
click(ids.searchField);
assert(!ids.keyboard.classList.contains('hidden'));
['д', 'ю', 'н', 'а'].forEach(letter => click(byText(ids.keyboardKeys, letter.toUpperCase())));
click(ids.kbDone);
const searchCall = calls.find(c => c.url.indexOf('/api/v2/search') >= 0);
assert(searchCall && searchCall.method === 'POST');
assert(searchCall.body.indexOf('text=') >= 0);

/* 11. Profile and logout */
click(ids.nav.querySelectorAll('.nav-item')[3]);
assert(!ids.profile.classList.contains('hidden'));
route('/api/v2/users/profile', 200, {
  profile_page_view: {
    user_info_view: { phone: 99362560726 },
    individual_subscription_view: { subscription_name: 'Standard', status_message: { text: 'Истекает 11 Декабря' } }
  }
});
click(ids.profileRefresh);
assert.strictEqual(ids.profilePhone.textContent, '+993 ••• 0726');
assert(ids.profilePlan.textContent.indexOf('Standard') >= 0);

const deviceBeforeLogout = store.neon_belet_samsung_device_v2;
click(ids.profileLogout);
assert(!ids.dialog.classList.contains('hidden'));
route('/api/v1/auth/log-out', 200, { status: 'ok' });
route('/api/v1/auth/available-otp', 200, { otp: [{ name: 'SMS', type: 2 }] });
click(ids.dialogActions.querySelectorAll('*')[0]);
assert(!store.neon_belet_samsung_session_v2, 'session removed');
assert(!ids.login.classList.contains('hidden'), 'back at login');
const deviceAfterLogout = store.neon_belet_samsung_device_v2;
assert(/^[a-f0-9]{32}$/.test(deviceAfterLogout), 'logout rotates install fingerprint to fresh 32-hex id');
assert.notStrictEqual(deviceAfterLogout, deviceBeforeLogout, 'logout does not reuse the signed-out fingerprint');
const logoutCall = calls.filter(c => c.url.indexOf('/api/v1/auth/log-out') >= 0).pop();
assert.strictEqual(logoutCall.headers.fingerprint, deviceBeforeLogout, 'log-out sends the signed-out fingerprint');
const postLogoutOtp = calls.filter(c => c.url.indexOf('/api/v1/auth/available-otp') >= 0).pop();
assert.strictEqual(postLogoutOtp.headers.fingerprint, deviceAfterLogout, 'post-logout OTP request uses the rotated fingerprint');

/* 12. QR sign-in flow */
testQrLogin();

/* 13. A 401 refreshes the token once and retries the same request */
route('/api/v2/home_page', 200, {
  status: 'ok',
  result: [{ id: 1, title: 'После входа', source: 'top', items: [
    { id: 20, type_id: 1, name: 'Retry Film', images: { vertical: { medium: { url: 'https://img.example/r.jpg' } } } }
  ] }]
});
click(ids.homeReload);
assert.strictEqual(ids.rows.querySelectorAll('.card').length, 1);

route('/api/v3/movie/20', 401, { message: 'expired' });
route('/api/v2/auth/refresh-mobile', 200, { token: 'new-token', refresh_token: 'new-refresh' });
route('/api/v3/movie/20', 200, { status: 'ok', movie: { id: 20, name: 'Retry Film', type_id: 1 } });
click(ids.rows.querySelector('.card'));
assert.strictEqual(JSON.parse(store.neon_belet_samsung_session_v2).token, 'new-token');
assert.strictEqual(JSON.parse(store.neon_belet_samsung_session_v2).refresh_token, 'new-refresh');
assert.strictEqual(ids.detailTitle.textContent, 'Retry Film');

/* 14. A rejected refresh clears the session and returns to login */
route('/api/v3/movie/20', 401, {});
route('/api/v2/auth/refresh-mobile', 401, {});
route('/api/v1/auth/available-otp', 200, { otp: [{ name: 'SMS', type: 1 }] });
fireKey(10009);
click(ids.rows.querySelector('.card'));
runTimers();
assert(!store.neon_belet_samsung_session_v2, 'session cleared after rejected refresh');
assert(!ids.login.classList.contains('hidden'), 'login shown after rejected refresh');

/* 15. Post-logout resilience: even if log-out returns 500 and OTP/QR initially reject the device,
 *     phone input stays unlocked via fallback and both OTP and QR recover via device rotation + authAlt */
route('/api/v2/home_page', 200, {
  status: 'ok',
  result: [{ id: 1, title: 'Снова дома', source: 'top', items: [
    { id: 30, type_id: 1, name: 'Film 30', images: { vertical: { medium: { url: 'https://img.example/30.jpg' } } } }
  ] }]
});
route('/api/v1/auth/get-qrcode', 200, {
  code: 'https://film.belet.me/register/qr-before-step15',
  qr_png: 'iVBORw0KGgoAAAANSUhEUg=='
});
click(ids.toQr);
runTimers();
route('/api/v1/auth/check-qrcode', 200, { token: 'step15-access', refresh_token: 'step15-refresh' });
route('/api/v1/users/device', 200, { msg: 'ok' });
runTimers();
assert(!ids.home.classList.contains('hidden'), 'logged in before step 15 logout');

click(ids.nav.querySelectorAll('.nav-item')[3]);
assert(!ids.profile.classList.contains('hidden'));
const step15OldDevice = store.neon_belet_samsung_device_v2;
click(ids.profileLogout);
route('/api/v1/auth/log-out', 500, { message: 'server error' });
route('https://beletapis.com/api/v1/auth/available-otp', 403, { message: 'stale fingerprint' });
route('https://api.belet.tm/api/v1/auth/available-otp', 403, { message: 'stale fingerprint' });
click(ids.dialogActions.querySelectorAll('*')[0]);
assert(!store.neon_belet_samsung_session_v2, 'session deleted even if log-out fails');
assert(!ids.login.classList.contains('hidden'), 'login opened after failed log-out');
assert.notStrictEqual(store.neon_belet_samsung_device_v2, step15OldDevice, 'fingerprint rotated on logout and OTP retry');
assert(ids.methodValue.textContent.indexOf('резерв') >= 0, 'OTP failure uses SMS fallback instead of locking phone entry');
click(ids.phoneDisplay);
'99362560726'.split('').forEach(digit => click(byText(ids.numpadKeys, digit)));
click(ids.numpadDone);
assert.strictEqual(ids.sendCode.disabled, false, 'sendCode stays enabled with phone + fallback method');

route('https://beletapis.com/api/v1/auth/available-otp', 500, {});
route('https://api.belet.tm/api/v1/auth/available-otp', 200, { otp: [{ name: 'SMS', type: 2 }] });
runTimers();
assert.strictEqual(ids.methodValue.textContent, 'SMS', 'auto-retry recovers OTP methods via authAlt');
assert.strictEqual(ids.sendCode.disabled, false);

const preQrRetryDevice = store.neon_belet_samsung_device_v2;
route('https://beletapis.com/api/v1/auth/get-qrcode', 500, { message: 'bad gateway' });
route('https://api.belet.tm/api/v1/auth/get-qrcode', 500, { message: 'bad gateway' });
click(ids.toQr);
assert.notStrictEqual(store.neon_belet_samsung_device_v2, preQrRetryDevice, 'QR error rotates stale fingerprint before retry');
const qrRotatedDevice = store.neon_belet_samsung_device_v2;
route('https://beletapis.com/api/v1/auth/get-qrcode', 500, {});
route('https://api.belet.tm/api/v1/auth/get-qrcode', 200, {
  code: 'https://belet.tm/register/qr-recovered-1',
  qr_png: 'data:image/png;base64,iVBORw0KGgo\nAAAANSUhEUg=='
});
runTimers();
assert(ids.qrImage.src.indexOf('data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==') === 0, 'QR recovers via authAlt and retry');
assert.strictEqual(calls.filter(c => c.url.indexOf('get-qrcode') >= 0).pop().headers.fingerprint, qrRotatedDevice);
/* Poll falls back to authAlt if primary check-qrcode returns 500 after authAlt QR issuance */
route('https://beletapis.com/api/v1/auth/check-qrcode', 500, {});
route('https://api.belet.tm/api/v1/auth/check-qrcode', 400, { message: 'waiting' });
runTimers();
assert(ids.qrStatus.textContent.indexOf('Ожидаем подтверждение') >= 0, 'QR polling continues via authAlt');

click(ids.toPhone);
assert(!ids.loginPhone.classList.contains('hidden'));

/* Even if available-otp fails twice, phone sign-in stays enabled and methodDisplay retries cleanly */
route('https://beletapis.com/api/v1/auth/get-qrcode', 200, {
  code: 'https://film.belet.me/register/qr-tmp',
  qr_png: 'iVBORw0KGgoAAAANSUhEUg=='
});
click(ids.toQr);
click(ids.toPhone);
route('/api/v1/auth/sign-in?sign_in_type=2', 200, { token: 'tmp-2' });
route('/api/v1/auth/check-code-mobile', 200, { token: 'tmp-access-2', refresh_token: 'tmp-refresh-2' });
route('/api/v1/users/device', 200, { msg: 'ok' });
route('/api/v2/home_page', 200, { status: 'ok', result: [] });
click(ids.sendCode);
click(ids.codeDisplay);
'1234'.split('').forEach(digit => click(byText(ids.numpadKeys, digit)));
click(ids.numpadDone);
runTimers();
click(ids.nav.querySelectorAll('.nav-item')[3]);
click(ids.profileLogout);
route('/api/v1/auth/log-out', 200, { status: 'ok' });
route('https://beletapis.com/api/v1/auth/available-otp', 500, {});
route('https://api.belet.tm/api/v1/auth/available-otp', 500, {});
click(ids.dialogActions.querySelectorAll('*')[0]);
// Now both initial and retry fail; sendCode stays usable for entered phone
route('https://beletapis.com/api/v1/auth/available-otp', 500, {});
route('https://api.belet.tm/api/v1/auth/available-otp', 500, {});
runTimers();
click(ids.phoneDisplay);
'99362560726'.split('').forEach(digit => click(byText(ids.numpadKeys, digit)));
click(ids.numpadDone);
assert.strictEqual(ids.sendCode.disabled, false, 'sendCode stays enabled after two available-otp failures');
route('https://beletapis.com/api/v1/auth/sign-in?sign_in_type=2', 500, {});
route('https://api.belet.tm/api/v1/auth/sign-in?sign_in_type=2', 200, { token: 'fallback-temp' });
click(ids.sendCode);
assert(!ids.loginCode.classList.contains('hidden'), 'signIn completes via authAlt even when available-otp failed');
route('/api/v1/auth/available-otp', 200, { otp: [{ name: 'SMS', type: 2 }] });
click(ids.backToPhone);
assert(!ids.loginPhone.classList.contains('hidden'));
assert.strictEqual(ids.methodValue.textContent, 'SMS', 'backToPhone refreshes OTP methods when fallback was active');

/* Opening diagnostics from login returns to login when logged out */
route('/api/v1/auth/available-otp', 200, { otp: [] });
route('/api/v1/categories', 200, { categories: [] });
route('/api/v3/movie/1', 401, {});
click(ids.loginHelp);
assert(!ids.about.classList.contains('hidden'));
assert(fireKey(10009), 'Back on about is consumed');
assert(!ids.login.classList.contains('hidden'), 'Back from about while logged out returns to login, not home/profile');
assert(ids.home.classList.contains('hidden') && ids.profile.classList.contains('hidden'));

/* 16. TV reboot / HTTP 0 resilience: multi-retry on QR, home, refresh-mobile, online recovery, and webapis.network boot wait */
route('https://beletapis.com/api/v1/auth/get-qrcode', 0, {});
route('https://api.belet.tm/api/v1/auth/get-qrcode', 0, {});
click(ids.toQr);
assert(ids.qrStatus.textContent.indexOf('HTTP 0') >= 0, 'QR reports HTTP 0 retry status');
route('https://beletapis.com/api/v1/auth/get-qrcode', 0, {});
route('https://api.belet.tm/api/v1/auth/get-qrcode', 0, {});
runTimers();
route('https://beletapis.com/api/v1/auth/get-qrcode', 200, {
  code: 'https://belet.tm/register/qr-step16',
  qr_png: 'data:image/png;base64,iVBORw0KGgoStep16=='
});
runTimers();
assert(ids.qrImage.src.indexOf('data:image/png;base64,iVBORw0KGgoStep16==') === 0, 'QR recovers after multiple HTTP 0 network failures');

route('/api/v1/auth/check-qrcode', 200, { token: 'step16-access', refresh_token: 'step16-refresh' });
route('/api/v1/users/device', 0, {});
route('/api/v1/users/device', 0, {});
route('/api/v2/home_page', 0, {});
runTimers();
assert(!ids.home.classList.contains('hidden'), 'home screen opens even on initial HTTP 0');
assert(!ids.homeEmpty.classList.contains('hidden'), 'homeEmpty displays network retry message');
assert.strictEqual(document.activeElement, ids.homeReload, 'homeReload is focused when homeEmpty appears');
assert(store.neon_belet_samsung_session_v2, 'session is kept during home HTTP 0');

route('/api/v2/home_page', 0, {});
runTimers();
route('/api/v2/home_page', 200, {
  status: 'ok',
  result: [{ id: 1, title: 'После сети', source: 'top', items: [
    { id: 40, type_id: 1, name: 'Recovered Film 40', images: { vertical: { medium: { url: 'https://img.example/40.jpg' } } } }
  ] }]
});
route('https://beletapis.com/api/v1/users/device', 200, { msg: 'ok' });
runTimers();
assert(ids.homeEmpty.classList.contains('hidden'), 'home auto-retries HTTP 0 and loads catalogue');
assert.strictEqual(ids.rows.querySelectorAll('.card').length, 1);

/* refresh-mobile hitting HTTP 0 preserves session and falls back to authAlt */
route('/api/v3/movie/40', 401, {});
route('https://beletapis.com/api/v2/auth/refresh-mobile', 0, {});
route('https://api.belet.tm/api/v2/auth/refresh-mobile', 0, {});
click(ids.rows.querySelector('.card'));
assert.strictEqual(JSON.parse(store.neon_belet_samsung_session_v2).token, 'step16-access', 'HTTP 0 on refresh-mobile does not wipe session');
assert(ids.login.classList.contains('hidden'), 'HTTP 0 on refresh-mobile does not kick user to login');

route('/api/v3/movie/40', 401, {});
route('https://beletapis.com/api/v2/auth/refresh-mobile', 0, {});
route('https://api.belet.tm/api/v2/auth/refresh-mobile', 200, { token: 'step16-refreshed', refresh_token: 'step16-r2' });
route('/api/v3/movie/40', 200, { status: 'ok', movie: { id: 40, name: 'Recovered Film 40', type_id: 1 } });
click(ids.rows.querySelector('.card'));
assert.strictEqual(JSON.parse(store.neon_belet_samsung_session_v2).token, 'step16-refreshed', 'refresh-mobile recovers via authAlt');
assert.strictEqual(ids.detailTitle.textContent, 'Recovered Film 40');

/* window online event triggers immediate home reload when home was stuck on HTTP 0 */
fireKey(10009);
route('/api/v2/home_page', 0, {});
click(ids.homeReload);
assert(!ids.homeEmpty.classList.contains('hidden'));
timers.length = 0;
route('/api/v2/home_page', 200, {
  status: 'ok',
  result: [{ id: 1, title: 'Онлайн', source: 'top', items: [
    { id: 41, type_id: 1, name: 'Online Film 41', images: { vertical: { medium: { url: 'https://img.example/41.jpg' } } } }
  ] }]
});
dispatchWindow('online');
assert(ids.homeEmpty.classList.contains('hidden'), 'online event recovers home immediately');
assert.strictEqual(ids.rows.querySelectorAll('.card').length, 1);

/* Cold-reboot simulation while logged in: webapis.network initially disconnected (0), then connects (1) + home HTTP 0 retry */
(() => {
  const bootCallsBefore = calls.length;
  let connType = 0;
  buildDom();
  timers.length = 0;
  const bootSandbox = Object.assign({}, sandbox, {
    document,
    webapis: { network: { getActiveConnectionType: () => connType } }
  });
  bootSandbox.window = bootSandbox;
  bootSandbox.global = bootSandbox;
  bootSandbox.self = bootSandbox;
  vm.createContext(bootSandbox);
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), bootSandbox, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), bootSandbox, { filename: 'app.js' });

  assert.strictEqual(calls.length, bootCallsBefore, 'no HTTP calls fired before TV network interface connects');
  assert(ids.bootStatus.textContent.indexOf('сети') >= 0, 'boot displays waiting-for-network status');

  connType = 1;
  route('https://beletapis.com/api/v1/users/device', 200, { msg: 'ok' });
  route('/api/v2/home_page', 0, {});
  runTimers();
  assert(!ids.home.classList.contains('hidden'), 'boot advances to home after network becomes ready');
  assert(!ids.homeEmpty.classList.contains('hidden'));
  assert.strictEqual(document.activeElement, ids.homeReload);

  route('/api/v2/home_page', 200, {
    status: 'ok',
    result: [{ id: 1, title: 'После перезагрузки', source: 'top', items: [
      { id: 42, type_id: 1, name: 'Reboot Film 42', images: { vertical: { medium: { url: 'https://img.example/42.jpg' } } } }
    ] }]
  });
  runTimers();
  assert(ids.homeEmpty.classList.contains('hidden'), 'post-reboot home loads cleanly once server responds');
  assert.strictEqual(ids.rows.querySelectorAll('.card').length, 1);
})();

/* 17. TV clock reset to 1970 on Ethernet: explicit guidance + about alert + auto-recovery once NTP syncs */
(() => {
  const RealDate = Date;
  let fakeNow = RealDate.UTC(1970, 0, 1, 1, 2, 0);
  function FakeDate(arg) {
    if (!(this instanceof FakeDate)) return new RealDate(fakeNow).toString();
    return new RealDate(arg === undefined ? fakeNow : arg);
  }
  FakeDate.now = () => fakeNow;
  FakeDate.UTC = RealDate.UTC;
  FakeDate.parse = RealDate.parse;

  buildDom();
  timers.length = 0;
  let tzDateObj = new RealDate(2026, 10, 23, 15, 42, 0);
  const clockSandbox = Object.assign({}, sandbox, {
    document,
    Date: FakeDate,
    tizen: {
      time: {
        getCurrentDateTime: () => tzDateObj,
        setDateTimeChangeListener: () => {}
      }
    },
    webapis: { network: { getActiveConnectionType: () => 3 } }
  });
  clockSandbox.window = clockSandbox;
  clockSandbox.global = clockSandbox;
  clockSandbox.self = clockSandbox;
  calls.length = 0;
  vm.createContext(clockSandbox);
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), clockSandbox, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), clockSandbox, { filename: 'app.js' });

  assert(ids.bootStatus.textContent.indexOf('времени') >= 0, 'boot pauses briefly for TV clock sync when year < 2026');

  // Step through the 4s boot wait while TV clock is still 1970 — ZERO direct https:// requests sent
  let steps = 0;
  while (timers.length && steps < 10) {
    const batch = timers.splice(0, timers.length);
    for (const item of batch) item.fn();
    steps += 1;
  }
  assert(!ids.home.classList.contains('hidden'), 'home screen opens after initial clock wait');
  assert(!ids.homeEmpty.classList.contains('hidden'));
  assert(ids.homeEmpty.querySelector('h2').textContent.indexOf('дата и время') >= 0, 'homeEmpty highlights 1970 TV clock error');
  assert(ids.homeEmpty.querySelector('p').textContent.indexOf('1970') >= 0, 'homeEmpty prints 1970 date');
  assert(!calls.some(c => c.url.indexOf('https://') === 0), 'never sends direct https:// during boot/home when clock is invalid');

  // Opening Проверка сети и справка directly via #homeHelp shows #aboutClockAlert without probing https://
  click(ids.homeHelp);
  assert(!ids.about.classList.contains('hidden'));
  assert.strictEqual(ids.clock.textContent, '15:42', 'topbar uses Tizen UI clock when manual time is set');
  assert(!ids.aboutClockAlert.classList.contains('hidden'), 'about shows clock alert when browser SSL clock < 2026');
  assert(ids.aboutStatus.textContent.indexOf('Время Tizen') >= 0, 'about highlights mismatch between Tizen manual clock and browser SSL clock');
  assert(ids.aboutClockAlert.textContent.indexOf('23.11.2026') >= 0, 'alert explains manual Tizen time vs browser SSL 1970 clock');
  assert(!calls.some(c => c.url.indexOf('https://') === 0), 'health check skips direct https:// when clock is invalid');

  // Back to home, then simulate TV NTP syncing clock from 1970 to 2026
  click(ids.aboutBack);
  assert(!ids.home.classList.contains('hidden'));
  fakeNow = RealDate.UTC(2026, 10, 23, 12, 0, 0);
  route('https://beletapis.com/api/v1/users/device', 200, { msg: 'ok' });
  route('/api/v2/home_page', 200, {
    status: 'ok',
    result: [{ id: 1, title: 'После синхронизации часов', source: 'top', items: [
      { id: 43, type_id: 1, name: 'Synced Clock Film 43', images: { vertical: { medium: { url: 'https://img.example/43.jpg' } } } }
    ] }]
  });
  runTimers();
  assert(ids.homeEmpty.classList.contains('hidden'), 'clock sync automatically reloads home and hides error');
  assert.strictEqual(ids.rows.querySelectorAll('.card').length, 1);
})();

/* 18. Stuck 1970 clock bypassed automatically by BeletFilm1.ClockProxy local TLS service */
(() => {
  const RealDate = Date;
  const fakeNow = RealDate.UTC(1970, 0, 1, 1, 2, 0);
  function FakeDate(arg) {
    if (!(this instanceof FakeDate)) return new RealDate(fakeNow).toString();
    return new RealDate(arg === undefined ? fakeNow : arg);
  }
  FakeDate.now = () => fakeNow;
  FakeDate.UTC = RealDate.UTC;
  FakeDate.parse = RealDate.parse;

  buildDom();
  timers.length = 0;
  calls.length = 0;
  const avplayOpened = [];
  let launchedSvc = null;
  const proxySandbox = Object.assign({}, sandbox, {
    document,
    Date: FakeDate,
    tizen: {
      ApplicationControl: function (operation) { this.operation = operation; },
      application: {
        launchAppControl: (ctrl, id, onOk) => { launchedSvc = { ctrl, id }; if (onOk) onOk(); },
        getCurrentApplication: () => ({ appInfo: { id: 'BeletFilm1.BeletClient' } })
      },
      time: {
        getCurrentDateTime: () => new RealDate(RealDate.UTC(2026, 10, 23, 15, 45, 0)),
        setDateTimeChangeListener: () => {}
      }
    },
    webapis: {
      network: { getActiveConnectionType: () => 3 },
      avplay: {
        open: (u) => { avplayOpened.push(u); },
        setListener: () => {},
        setDisplayRect: () => {},
        setStreamingProperty: () => {},
        prepareAsync: (ok) => { if (ok) ok(); },
        play: () => {},
        pause: () => {},
        close: () => {},
        stop: () => {},
        getCurrentTime: () => 0,
        getDuration: () => 100000
      }
    }
  });
  proxySandbox.tizen.time.getCurrentDateTime = () => new RealDate(fakeNow);
  proxySandbox.window = proxySandbox;
  proxySandbox.global = proxySandbox;
  proxySandbox.self = proxySandbox;
  vm.createContext(proxySandbox);

  route('http://127.0.0.1:18767/__ping', 0, {});
  route('http://localhost:18767/__ping', 0, {});
  route('http://127.0.0.1:18767/__ping', 200, {
    ok: true,
    service: 'BeletFilm1.ClockProxy',
    port: 18767,
    sslClockYear: 1970,
    serverTimeMs: new RealDate(2026, 10, 23, 15, 45, 0).getTime()
  });
  route('http://127.0.0.1:18767/__proxy/beletapis.com/api/v1/users/device', 200, { msg: 'ok' });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v2/home_page', 200, {
    status: 'ok',
    result: [{
      id: 1, title: 'Работа через локальный мост', source: 'top', items: [
        { id: 99, type_id: 1, name: 'Proxy Film 99',
          images: { vertical: { medium: { url: 'https://img.belet.me/99.jpg' } } } }
      ]
    }]
  });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v2/home_page', 200, {
    status: 'ok',
    result: []
  });

  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), proxySandbox, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), proxySandbox, { filename: 'app.js' });
  let steps18 = 0;
  while (timers.length && steps18 < 6 && ids.home.classList.contains('hidden')) {
    runTimers();
    steps18 += 1;
  }

  assert.strictEqual(launchedSvc.id, 'BeletFilm1.ClockProxy', 'launches ClockProxy Tizen service when clock < 2024');
  assert(!ids.home.classList.contains('hidden'), 'home opens automatically via ClockProxy while TV clock stays 1970');
  assert(ids.homeEmpty.classList.contains('hidden'), 'no 1970 error banner when ClockProxy serves home');
  assert.strictEqual(ids.clock.textContent, '15:45', 'topbar displays serverTimeMs clock even with 1970 OS time');

  const card99 = ids.rows.querySelector('.card');
  const poster = card99 && card99.querySelector('img');
  assert(poster && poster.src.indexOf('http://127.0.0.1:18767/__proxy/img.belet.me/99.jpg') === 0,
    'poster images route through ClockProxy when TV clock is 1970: ' + (poster && poster.src));

  // Verify movie detail + playback URL also route through ClockProxy
  route('http://127.0.0.1:18767/__proxy/film.beletapis.com/api/v3/movie/99', 200, {
    movie: {
      id: 99, type_id: 1, name: 'Proxy Film 99',
      files: {
        '720p': { url: 'https://videofilm.belet.me/hls/99/720.m3u8' },
        hls: 'https://videofilm.belet.me/hls/99/master.m3u8'
      }
    }
  });
  route('http://127.0.0.1:18767/__proxy/film.beletapis.com/api/v3/files/99?type=1', 200, {
    sources: [{ filename: 'https://videofilm.belet.me/hls/99/master.m3u8', quality: '1080p' }]
  });
  click(ids.rows.querySelector('.card'));
  assert(!ids.detail.classList.contains('hidden'), 'detail view loads through ClockProxy');
  click(ids.detailPlay);
  assert(avplayOpened.some(u => u.indexOf('http://127.0.0.1:18767/__proxy/videofilm.belet.me/hls/99/') === 0),
    'playback URL routes through ClockProxy: ' + JSON.stringify(avplayOpened));
  fireKey(10009); // exit player to detail
  fireKey(10009); // exit detail to home

  // Diagnostics show ClockProxy active and hide #aboutClockAlert
  route('http://127.0.0.1:18767/__proxy/beletapis.com/api/v1/auth/available-otp', 200, { ok: true });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v1/categories', 200, { ok: true });
  route('http://127.0.0.1:18767/__proxy/film.beletapis.com/api/v3/movie/1', 200, { ok: true });
  click(ids.homeHelp);
  runTimers();
  assert(!ids.about.classList.contains('hidden'));
  assert(ids.aboutClockAlert.classList.contains('hidden'), 'clock alert hidden once ClockProxy is active');
  assert(ids.aboutStatus.textContent.indexOf('Обход часов ТВ (SSL)') >= 0, 'about shows ClockProxy status');
  assert(ids.aboutStatus.textContent.indexOf('активен') >= 0, 'ClockProxy marked active');
})();

/* 19. Inline WRT module injection on Tizen 10 & service-free 1970 TV never leaks direct https:// requests */
(() => {
  const RealDate = Date;
  const fakeNow = RealDate.UTC(1970, 0, 1, 1, 2, 0);
  function FakeDate(arg) {
    if (!(this instanceof FakeDate)) return new RealDate(fakeNow).toString();
    return new RealDate(arg === undefined ? fakeNow : arg);
  }
  FakeDate.now = () => fakeNow;
  FakeDate.UTC = RealDate.UTC;
  FakeDate.parse = RealDate.parse;

  buildDom();
  timers.length = 0;
  calls.length = 0;

  // Test inline WRT module injection on Tizen 10 first with tizen.application present
  const t10Sandbox = Object.assign({}, sandbox, {
    document,
    Date: FakeDate,
    tizen: {
      systeminfo: { getCapability: () => '10.0' },
      ApplicationControl: function (op) { this.operation = op; },
      application: { launchAppControl: () => {} }
    },
    webapis: { network: { getActiveConnectionType: () => 3 } }
  });
  t10Sandbox.window = t10Sandbox;
  t10Sandbox.global = t10Sandbox;
  t10Sandbox.self = t10Sandbox;
  vm.createContext(t10Sandbox);
  route('http://127.0.0.1:18767/__ping', 0, {});
  route('http://localhost:18767/__ping', 0, {});
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), t10Sandbox, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), t10Sandbox, { filename: 'app.js' });
  const injectedModules = document.body.querySelectorAll('script').filter(s => s.type === 'module');
  assert(injectedModules.some(s => (s.text || '').indexOf("from 'wrt:service'") >= 0),
    'Tizen 10 injects inline wrt:service module to avoid file:// CORS');

  // Verify service-free 1970 TV enters homeEmpty safely without firing direct https:// requests
  buildDom();
  timers.length = 0;
  calls.length = 0;
  const noSvcSandbox = Object.assign({}, sandbox, {
    document,
    Date: FakeDate,
    tizen: { systeminfo: { getCapability: () => '10.0' } },
    webapis: { network: { getActiveConnectionType: () => 3 } }
  });
  noSvcSandbox.window = noSvcSandbox;
  noSvcSandbox.global = noSvcSandbox;
  noSvcSandbox.self = noSvcSandbox;
  vm.createContext(noSvcSandbox);
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), noSvcSandbox, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), noSvcSandbox, { filename: 'app.js' });
  let s19 = 0;
  while (timers.length && s19 < 10 && ids.home.classList.contains('hidden')) {
    const batch = timers.splice(0, timers.length);
    for (const item of batch) item.fn();
    s19 += 1;
  }
  assert(!ids.home.classList.contains('hidden'));
  assert(!ids.homeEmpty.classList.contains('hidden'));
  assert(!calls.some(c => c.url.indexOf('https://') === 0), 'service-free 1970 TV never sends direct https:// requests');
})();

/* 20. Cold ClockProxy start > 4s (QA55Q8FAAKXXT Launch #1): no early 1970 abort, retries service cleanly */
(() => {
  const RealDate = Date;
  const fakeNow = RealDate.UTC(1970, 0, 1, 1, 2, 0);
  function FakeDate(arg) {
    if (!(this instanceof FakeDate)) return new RealDate(fakeNow).toString();
    return new RealDate(arg === undefined ? fakeNow : arg);
  }
  FakeDate.now = () => fakeNow;
  FakeDate.UTC = RealDate.UTC;
  FakeDate.parse = RealDate.parse;

  buildDom();
  timers.length = 0;
  calls.length = 0;

  let launchCount = 0;
  const coldListeners = {};
  const coldSandbox = Object.assign({}, sandbox, {
    document,
    Date: FakeDate,
    addEventListener: (ev, fn) => {
      (coldListeners[ev] = coldListeners[ev] || []).push(fn);
    },
    setTimeout: (fn, delay) => {
      if (delay !== undefined && delay <= 60) { fn(); return 0; }
      const id = ++timerSeq;
      timers.push({ id, fn, due: (coldSandbox.__nowMs || 0) + (delay || 0), delay: delay || 0 });
      return id;
    },
    tizen: {
      systeminfo: { getCapability: () => '10.0' },
      ApplicationControl: function (op) { this.operation = op; },
      application: {
        launchAppControl: (ctrl, id, onOk) => { launchCount += 1; if (onOk) onOk(); },
        getCurrentApplication: () => ({ appInfo: { id: 'BeletFilm1.BeletClient' } })
      },
      time: {
        getCurrentDateTime: () => new RealDate(fakeNow),
        setDateTimeChangeListener: () => {}
      }
    },
    webapis: { network: { getActiveConnectionType: () => 3 } }
  });
  coldSandbox.__nowMs = 0;
  coldSandbox.window = coldSandbox;
  coldSandbox.global = coldSandbox;
  coldSandbox.self = coldSandbox;
  vm.createContext(coldSandbox);

  // Queue 15 failed cold polls (t=0 plus 350..4900ms -> past old 4000ms timeout!)
  for (let p = 0; p < 15; p += 1) {
    route('http://127.0.0.1:18767/__ping', 0, {});
    route('http://localhost:18767/__ping', 0, {});
  }
  // Then at t=5250ms ClockProxy finishes cold startup and binds :18767
  route('http://127.0.0.1:18767/__ping', 200, {
    ok: true,
    mode: 'clock-proxy',
    port: 18767,
    sslClockYear: 1970,
    serverTimeMs: new RealDate(2026, 10, 23, 19, 15, 0).getTime()
  });
  route('http://127.0.0.1:18767/__proxy/beletapis.com/api/v1/users/device', 200, { msg: 'ok' });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v2/home_page', 200, {
    status: 'ok',
    result: [{
      id: 1, title: 'Запуск #1 без выхода', source: 'top', items: [
        { id: 202, type_id: 1, name: 'First Launch Film 202',
          images: { vertical: { medium: { url: 'https://img.belet.me/202.jpg' } } } }
      ]
    }]
  });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v2/home_page', 200, { status: 'ok', result: [] });

  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), coldSandbox, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), coldSandbox, { filename: 'app.js' });

  let guard = 0;
  while (timers.length && guard < 40 && ids.home.classList.contains('hidden')) {
    timers.sort((a, b) => a.due - b.due);
    const next = timers.shift();
    coldSandbox.__nowMs = next.due;
    next.fn();
    guard += 1;
  }

  assert(!ids.home.classList.contains('hidden'), 'Launch #1 enters home automatically even when ClockProxy takes >5s');
  assert(ids.homeEmpty.classList.contains('hidden'), 'no 1970 SSL failure on Launch #1');
  assert.strictEqual(ids.rows.querySelectorAll('.card').length, 1);
  assert(launchCount >= 2, 'ClockProxy service launch retried during cold wait (launches=' + launchCount + ')');
  assert(!calls.some(c => c.url.indexOf('https://') === 0), 'never sent direct https:// request while ClockProxy was starting');
})();

/* 21. Unconfigured 2025 factory clock without session launches ClockProxy immediately and skips https:// OTP/QR calls */
(() => {
  const RealDate = Date;
  const fake2025 = RealDate.UTC(2025, 0, 1, 0, 15, 0);
  function Fake2025(arg) {
    if (!(this instanceof Fake2025)) return new RealDate(fake2025).toString();
    return new RealDate(arg === undefined ? fake2025 : arg);
  }
  Fake2025.now = () => fake2025;
  Fake2025.UTC = RealDate.UTC;
  Fake2025.parse = RealDate.parse;

  buildDom();
  timers.length = 0;
  calls.length = 0;
  delete store.neon_belet_samsung_session_v2;

  let svcLaunches = 0;
  const s21 = Object.assign({}, sandbox, {
    document,
    Date: Fake2025,
    tizen: {
      systeminfo: { getCapability: () => '10.0' },
      ApplicationControl: function (op) { this.operation = op; },
      application: {
        launchAppControl: (c, id, ok) => { svcLaunches += 1; if (ok) ok(); }
      }
    },
    webapis: { network: { getActiveConnectionType: () => 3 } }
  });
  s21.window = s21;
  s21.global = s21;
  s21.self = s21;
  vm.createContext(s21);

  // First boot without proxy responding -> enters login UI without firing ANY https:// OTP or QR request
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), s21, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), s21, { filename: 'app.js' });
  route('http://127.0.0.1:18767/__ping', 0, {});
  route('http://localhost:18767/__ping', 0, {});
  // Wait for 18s boot loop without proxy -> login opens directly with fallback OTP, zero https:// calls
  let g21 = 0;
  while (timers.length && g21 < 80 && ids.login.classList.contains('hidden')) {
    const batch = timers.splice(0, timers.length);
    for (const item of batch) {
      if (routes.length === 0 && !item.fn.toString().includes('tickClock')) {
        // Queue 0 for ongoing loopback pings until boot opens login
        route('http://127.0.0.1:18767/__ping', 0, {});
        route('http://localhost:18767/__ping', 0, {});
      }
      item.fn();
    }
    g21 += 1;
  }
  routes.length = 0;
  assert(svcLaunches >= 1, '2025 factory clock immediately launched ClockProxy');
  assert(!ids.login.classList.contains('hidden'), 'login opens cleanly');
  assert(!calls.some(c => c.url.indexOf('https://') === 0), 'never checked https:// OTP while clock < 2026');
  click(ids.toQr);
  assert(!calls.some(c => c.url.indexOf('https://') === 0), 'QR tab also skips direct https:// while clock < 2026');
  assert(ids.qrStatus.textContent.indexOf('Подключение к серверам Belet') >= 0, 'QR tab shows clean loading status');
  assert(!/обход|SSL|прокси/i.test(ids.qrStatus.textContent), 'QR tab omits technical bypass wording');

  // Now ClockProxy responds -> recoverNetworkScreens auto-fetches QR over HTTP ClockProxy!
  route('http://127.0.0.1:18767/__ping', 200, {
    ok: true, mode: 'clock-proxy', port: 18767, sslClockYear: 2025,
    serverTimeMs: new RealDate(2026, 10, 23, 20, 0, 0).getTime()
  });
  route('http://127.0.0.1:18767/__proxy/beletapis.com/api/v1/auth/get-qrcode', 200, {
    code: 'qr-bypass-21',
    qr_png: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
  });
  runTimers();
  assert(calls.some(c => c.url.indexOf('http://127.0.0.1:18767/__proxy/beletapis.com/api/v1/auth/get-qrcode') === 0),
    'QR request goes through ClockProxy once active');
  assert(ids.qrImage.src && ids.qrImage.src.indexOf('data:image/png;base64,') === 0, 'QR image renders via ClockProxy');
  assert(!calls.some(c => c.url.indexOf('https://') === 0), 'zero direct https:// requests across entire 2025 login session');
})();

/* 22. Forced bypass mode (neon-bypass-mode="always") activates ClockProxy even with 2026+ TV clock + retries broken images */
(() => {
  const RealDate = Date;
  const now2026 = RealDate.UTC(2026, 10, 23, 21, 0, 0);
  function Fake2026(arg) {
    if (!(this instanceof Fake2026)) return new RealDate(now2026).toString();
    return new RealDate(arg === undefined ? now2026 : arg);
  }
  Fake2026.now = () => now2026;
  Fake2026.UTC = RealDate.UTC;
  Fake2026.parse = RealDate.parse;

  buildDom();
  const bypassMeta = document.createElement('meta');
  bypassMeta.setAttribute('name', 'neon-bypass-mode');
  bypassMeta.setAttribute('content', 'always');
  document.body.appendChild(bypassMeta);

  store.neon_belet_samsung_session_v2 = JSON.stringify({
    token: 'tok-22', refresh: 'ref-22', phone: '+99361000022', fingerprint: 'fp-22'
  });
  timers.length = 0;
  calls.length = 0;

  let svcLaunches22 = 0;
  const s22 = Object.assign({}, sandbox, {
    document,
    Date: Fake2026,
    setTimeout: (fn, delay) => {
      const id = ++timerSeq;
      timers.push({ id, fn, delay: delay || 0 });
      return id;
    },
    tizen: {
      systeminfo: { getCapability: () => '10.0' },
      ApplicationControl: function (op) { this.operation = op; },
      application: {
        launchAppControl: (c, id, ok) => { svcLaunches22 += 1; if (ok) ok(); },
        getCurrentApplication: () => ({ appInfo: { id: 'BeletFilm1.BeletClient' } })
      },
      time: { getCurrentDateTime: () => new RealDate(now2026), setDateTimeChangeListener: () => {} }
    },
    webapis: { network: { getActiveConnectionType: () => 3 } }
  });
  s22.window = s22;
  s22.global = s22;
  s22.self = s22;
  vm.createContext(s22);

  route('http://127.0.0.1:18767/__ping', 200, {
    ok: true, mode: 'clock-proxy', port: 18767, sslClockYear: 2026, serverTimeMs: now2026
  });
  route('http://127.0.0.1:18767/__proxy/beletapis.com/api/v1/users/device', 200, { msg: 'ok' });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v2/home_page', 200, {
    status: 'ok',
    result: [{
      id: 1, title: 'Принудительный обход', source: 'top', items: [
        { id: 303, type_id: 1, name: 'Bypass Film 303',
          images: { vertical: { medium: { url: 'http://img.belet.me/303.jpg' } } } }
      ]
    }]
  });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v2/home_page', 200, { status: 'ok', result: [] });

  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), s22, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), s22, { filename: 'app.js' });
  runTimers();

  assert(svcLaunches22 >= 1, 'forced bypass launches ClockProxy even when TV date is 2026');
  assert(!ids.home.classList.contains('hidden'), 'home renders in forced bypass mode');
  assert(!calls.some(c => c.url.indexOf('https://') === 0), 'API requests go through ClockProxy instead of direct HTTPS');

  const card303 = ids.rows.querySelector('.card');
  const poster = card303 && card303.querySelector('img');
  assert(poster, 'poster img created');
  assert.strictEqual(poster.getAttribute('referrerpolicy'), 'no-referrer', 'poster requests avoid cross-origin referer issues');
  assert.strictEqual(poster.src, 'http://127.0.0.1:18767/__proxy/img.belet.me/303.jpg',
    'public http:// poster URL routes via ClockProxy when bypass is active');

  // Verify 2 retries before fallback placeholder on broken poster image
  let retries = 0;
  const origRm = poster.removeAttribute.bind(poster);
  poster.removeAttribute = function (name) {
    if (name === 'src') retries += 1;
    return origRm(name);
  };
  poster.dispatch('error');
  runTimers();
  assert.strictEqual(retries, 1, 'first retry resets and reassigns poster URL');
  assert.strictEqual(poster.src, 'http://127.0.0.1:18767/__proxy/img.belet.me/303.jpg');
  poster.dispatch('error');
  runTimers();
  assert.strictEqual(retries, 2, 'second retry resets and reassigns poster URL');
  assert.strictEqual(poster.src, 'http://127.0.0.1:18767/__proxy/img.belet.me/303.jpg');
  poster.dispatch('error');
  assert(poster.classList.contains('brand-fallback'), 'third failure shows brand fallback');
  assert.strictEqual(poster.src, 'belet-logo.png');
})();

/* 23. Direct mode (neon-bypass-mode="off") skips ClockProxy and calls direct HTTPS */
(() => {
  const RealDate = Date;
  const now2026 = RealDate.UTC(2026, 10, 23, 21, 5, 0);
  function Fake2026(arg) {
    if (!(this instanceof Fake2026)) return new RealDate(now2026).toString();
    return new RealDate(arg === undefined ? now2026 : arg);
  }
  Fake2026.now = () => now2026;
  Fake2026.UTC = RealDate.UTC;
  Fake2026.parse = RealDate.parse;

  buildDom();
  const offMeta = document.createElement('meta');
  offMeta.setAttribute('name', 'neon-bypass-mode');
  offMeta.setAttribute('content', 'off');
  document.body.appendChild(offMeta);
  store.neon_belet_samsung_session_v2 = JSON.stringify({
    token: 'tok-23', refresh: 'ref-23', phone: '+99361000023', fingerprint: 'fp-23'
  });
  timers.length = 0;
  calls.length = 0;

  let svcLaunches23 = 0;
  const s23 = Object.assign({}, sandbox, {
    document,
    Date: Fake2026,
    tizen: {
      systeminfo: { getCapability: () => '10.0' },
      ApplicationControl: function (op) { this.operation = op; },
      application: {
        launchAppControl: () => { svcLaunches23 += 1; },
        getCurrentApplication: () => ({ appInfo: { id: 'BeletFilm1.BeletClient' } })
      }
    },
    webapis: { network: { getActiveConnectionType: () => 3 } }
  });
  s23.window = s23;
  s23.global = s23;
  s23.self = s23;
  vm.createContext(s23);

  route('https://beletapis.com/api/v1/users/device', 200, { msg: 'ok' });
  route('https://homepage.belet.me/api/v2/home_page', 200, {
    status: 'ok',
    result: [{
      id: 1, title: 'Прямой HTTPS', source: 'top', items: [
        { id: 404, type_id: 1, name: 'Direct Film 404',
          images: { vertical: { medium: { url: 'https://img.belet.me/404.jpg' } } } }
      ]
    }]
  });
  route('https://homepage.belet.me/api/v2/home_page', 200, { status: 'ok', result: [] });

  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), s23, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), s23, { filename: 'app.js' });
  runTimers();

  assert.strictEqual(svcLaunches23, 0, 'direct mode never launches ClockProxy');
  assert(!calls.some(c => c.url.indexOf(':18767') >= 0), 'direct mode never contacts any port 18767 bridge');
  assert(!ids.home.classList.contains('hidden'), 'direct mode renders home directly over HTTPS');
  assert(calls.some(c => c.url.indexOf('https://homepage.belet.me/api/v2/home_page') === 0), 'direct HTTPS request succeeded');
  const card404 = ids.rows.querySelector('.card');
  const directPoster = card404 && card404.querySelector('img');
  assert(directPoster, 'direct poster img created');
  assert.strictEqual(directPoster.src, 'https://img.belet.me/404.jpg', 'direct mode keeps original https:// poster URL');
})();

/* 24. Forced bypass late ClockProxy readiness upgrades already-mounted home posters and retries via proxy */
(() => {
  const RealDate = Date;
  const now2026 = RealDate.UTC(2026, 10, 23, 21, 10, 0);
  function Fake2026(arg) {
    if (!(this instanceof Fake2026)) return new RealDate(now2026).toString();
    return new RealDate(arg === undefined ? now2026 : arg);
  }
  Fake2026.now = () => now2026;
  Fake2026.UTC = RealDate.UTC;
  Fake2026.parse = RealDate.parse;

  buildDom();
  const bypassMeta = document.createElement('meta');
  bypassMeta.setAttribute('name', 'neon-bypass-mode');
  bypassMeta.setAttribute('content', 'always');
  document.body.appendChild(bypassMeta);

  store.neon_belet_samsung_session_v2 = JSON.stringify({
    token: 'tok-24', refresh: 'ref-24', phone: '+99361000024', fingerprint: 'fp-24'
  });
  timers.length = 0;
  calls.length = 0;

  const s24 = Object.assign({}, sandbox, {
    document,
    Date: Fake2026,
    setTimeout: (fn, delay) => {
      if (delay !== undefined && delay <= 60) { fn(); return 0; }
      const id = ++timerSeq;
      timers.push({ id, fn, due: (s24.__nowMs || 0) + (delay || 0), delay: delay || 0 });
      return id;
    },
    tizen: {
      systeminfo: { getCapability: () => '10.0' },
      ApplicationControl: function (op) { this.operation = op; },
      application: {
        launchAppControl: (c, id, ok) => { if (ok) ok(); },
        getCurrentApplication: () => ({ appInfo: { id: 'BeletFilm1.BeletClient' } })
      }
    },
    webapis: { network: { getActiveConnectionType: () => 3 } }
  });
  s24.__nowMs = 0;
  s24.window = s24;
  s24.global = s24;
  s24.self = s24;
  vm.createContext(s24);

  // Forced bypass on 2026+ waits up to 8000ms for ClockProxy, then falls back to direct HTTPS so home opens
  route('https://beletapis.com/api/v1/users/device', 200, { msg: 'ok' });
  route('https://homepage.belet.me/api/v2/home_page', 200, {
    status: 'ok',
    result: [{
      id: 1, title: 'Поздний обход', source: 'top', items: [
        { id: 505, type_id: 1, name: 'Late Bypass Film 505',
          images: { vertical: { medium: { url: 'https://img.belet.me/505.jpg' } } } }
      ]
    }]
  });
  route('https://homepage.belet.me/api/v2/home_page', 200, { status: 'ok', result: [] });

  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), s24, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), s24, { filename: 'app.js' });

  let g24 = 0;
  while (timers.length && g24 < 60 && ids.home.classList.contains('hidden')) {
    timers.sort((a, b) => a.due - b.due);
    const next = timers.shift();
    s24.__nowMs = next.due;
    if (routes.every(r => String(r.match).indexOf('__ping') < 0)) {
      route('http://127.0.0.1:18767/__ping', 0, {});
      route('http://localhost:18767/__ping', 0, {});
    }
    next.fn();
    g24 += 1;
  }
  // Consume any leftover __ping routes before testing late ClockProxy recovery
  for (let i = routes.length - 1; i >= 0; i -= 1) {
    if (String(routes[i].match).indexOf('__ping') >= 0) routes.splice(i, 1);
  }
  assert(!ids.home.classList.contains('hidden'), '2026+ forced bypass enters home via fallback if ClockProxy takes >8s');
  const card505 = ids.rows.querySelector('.card');
  const poster505 = card505 && card505.querySelector('img');
  assert.strictEqual(poster505.src, 'https://img.belet.me/505.jpg', 'initial poster uses direct HTTPS during fallback');

  // Now ClockProxy finishes starting on a background probe -> recovers home posters onto HTTP proxy!
  route('http://127.0.0.1:18767/__ping', 200, {
    ok: true, mode: 'clock-proxy', port: 18767, sslClockYear: 2026, serverTimeMs: now2026
  });
  runTimers();
  const updatedCard505 = ids.rows.querySelector('.card');
  const updatedPoster505 = updatedCard505 && updatedCard505.querySelector('img');
  assert.strictEqual(updatedPoster505.src, 'http://127.0.0.1:18767/__proxy/img.belet.me/505.jpg',
    'late ClockProxy readiness refreshes mounted home posters through proxy');
})();

/* 25. Clean stylized boot, incremental home rendering without DOM resets, rich banner/poster shapes & detailBackdropImg */
(() => {
  const RealDate = Date;
  const fakeNow = RealDate.UTC(1970, 0, 1, 1, 5, 0);
  function Fake1970(arg) {
    if (!(this instanceof Fake1970)) return new RealDate(fakeNow).toString();
    return new RealDate(arg === undefined ? fakeNow : arg);
  }
  Fake1970.now = () => fakeNow;
  Fake1970.UTC = RealDate.UTC;
  Fake1970.parse = RealDate.parse;

  buildDom();
  store.neon_belet_samsung_session_v2 = JSON.stringify({
    token: 'tok-25', refresh: 'ref-25', phone: '+99361000025', fingerprint: 'fp-25'
  });
  timers.length = 0;
  calls.length = 0;

  let bootHomeCb = null;
  const s25 = Object.assign({}, sandbox, {
    document,
    Date: Fake1970,
    XMLHttpRequest: function MockAsyncXhr() {
      const base = new sandbox.XMLHttpRequest();
      const origSend = base.send.bind(base);
      const origOpen = base.open.bind(base);
      let curUrl = '';
      base.open = function (m, u, a) {
        curUrl = u;
        return origOpen(m, u, a);
      };
      base.send = function (body) {
        if (curUrl.indexOf('/api/v2/home_page') >= 0 && curUrl.indexOf('offset=3') >= 0 && !bootHomeCb) {
          calls.push({ method: 'GET', url: curUrl, headers: {}, body });
          bootHomeCb = () => {
            const index = routes.findIndex(r => curUrl.indexOf(r.match) >= 0);
            const item = index >= 0 ? routes.splice(index, 1)[0] : null;
            base.status = item ? item.status : 200;
            base.responseText = JSON.stringify(item && item.json !== undefined ? item.json : { status: 'ok', result: [] });
            base.readyState = 4;
            if (base.onreadystatechange) base.onreadystatechange();
          };
          return;
        }
        return origSend(body);
      };
      return base;
    },
    tizen: {
      systeminfo: { getCapability: () => '10.0' },
      ApplicationControl: function (op) { this.operation = op; },
      application: {
        launchAppControl: (c, id, ok) => { if (ok) ok(); },
        getCurrentApplication: () => ({ appInfo: { id: 'BeletFilm1.BeletClient' } })
      }
    },
    webapis: { network: { getActiveConnectionType: () => 3 } }
  });
  s25.window = s25;
  s25.global = s25;
  s25.self = s25;
  vm.createContext(s25);

  route('http://127.0.0.1:18767/__ping', 0, {});
  route('http://localhost:18767/__ping', 0, {});
  route('http://127.0.0.1:18767/__ping', 200, {
    ok: true, mode: 'clock-proxy', port: 18767, sslClockYear: 1970,
    serverTimeMs: RealDate.UTC(2026, 10, 23, 22, 0, 0)
  });
  route('http://127.0.0.1:18767/__proxy/beletapis.com/api/v1/users/device', 200, { msg: 'ok' });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v2/home_page', 200, {
    status: 'ok',
    result: [
      {
        id: 1, title: 'Баннеры', source: 'header', items: [
          { movie: { id: '701', name: 'Hero Banner 701', images: { horizontal: { large: 'banners/hero 701.jpg' } } } }
        ]
      },
      {
        id: 2, title: 'Новинки', source: 'top', items: [
          { id: 702, type_id: 1, name: 'Poster String 702', poster: 'https://img.belet.me/posters/p 702.jpg' },
          { id: 703, type_id: 1, name: 'Array Image 703', images: [{ type: 'vertical', url: '/arr/703.jpg' }] }
        ]
      }
    ]
  });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v2/home_page', 200, {
    status: 'ok',
    result: [
      {
        id: 3, title: 'Сериалы', source: 'series', items: [
          { id: 704, type_id: 2, name: 'Series 704', banner: 'banners/series704.jpg' }
        ]
      }
    ]
  });
  route('http://127.0.0.1:18767/__proxy/homepage.belet.me/api/v2/home_page', 200, { status: 'ok', result: [] });

  vm.runInContext(fs.readFileSync(path.join(srcDir, 'api.js'), 'utf8'), s25, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(path.join(srcDir, 'app.js'), 'utf8'), s25, { filename: 'app.js' });

  assert(!/обход|SSL|прокси/i.test(ids.bootStatus.textContent),
    'initial boot status omits bypass wording: ' + ids.bootStatus.textContent);

  // Advance through ClockProxy ping -> page 1 opens home while page 2 is still pending
  runTimers();
  assert(!/обход|SSL|прокси/i.test(ids.bootStatus.textContent),
    'boot status never prints bypass wording: ' + ids.bootStatus.textContent);
  assert(!ids.home.classList.contains('hidden'), 'home opens as soon as page 1 renders');
  assert(ids.homeLoading.classList.contains('hidden'), 'homeLoading hidden once rows appear');
  assert(!ids.hero.classList.contains('hidden'), 'header banner without type_id renders hero');
  assert.strictEqual(ids.heroImage.src, 'http://127.0.0.1:18767/__proxy/img.belet.me/banners/hero%20701.jpg',
    'nested header movie horizontal large banner encodes spaces and routes via proxy');

  const firstCardsBeforePage2 = ids.rows.querySelectorAll('.card');
  assert.strictEqual(firstCardsBeforePage2.length, 2);
  const card702Before = firstCardsBeforePage2[0];
  const img702Before = card702Before.querySelector('img');
  assert.strictEqual(img702Before.src, 'http://127.0.0.1:18767/__proxy/img.belet.me/posters/p%20702.jpg');
  assert.strictEqual(firstCardsBeforePage2[1].querySelector('img').src,
    'http://127.0.0.1:18767/__proxy/img.belet.me/arr/703.jpg');

  // Complete page 2 -> earlier row DOM nodes & heroImage must NOT be destroyed/recreated!
  let heroResetCalls = 0;
  const origHeroRm = ids.heroImage.removeAttribute.bind(ids.heroImage);
  ids.heroImage.removeAttribute = function (n) { if (n === 'src') heroResetCalls += 1; return origHeroRm(n); };
  assert(bootHomeCb, 'offset=3 async request queued');
  bootHomeCb();
  const cardsAfterPage2 = ids.rows.querySelectorAll('.card');
  assert.strictEqual(cardsAfterPage2.length, 3, 'page 2 appended cleanly');
  assert.strictEqual(cardsAfterPage2[0], card702Before, 'incremental home render preserves earlier cards without wiping #rows');
  assert.strictEqual(cardsAfterPage2[0].querySelector('img'), img702Before, 'in-flight poster <img> node stays untouched');
  assert.strictEqual(heroResetCalls, 0, 'heroImage not aborted/restarted during page 2 arrival');
  assert.strictEqual(cardsAfterPage2[2].querySelector('img').src,
    'http://127.0.0.1:18767/__proxy/img.belet.me/banners/series704.jpg');

  // Open detail and verify #detailBackdropImg loads via ClockProxy immediately and upgrades cleanly
  route('http://127.0.0.1:18767/__proxy/film.beletapis.com/api/v3/movie/702', 200, {
    movie: {
      id: 702, type_id: 1, name: 'Poster String 702',
      poster: 'https://img.belet.me/posters/p 702.jpg',
      banner: 'https://img.belet.me/banners/backdrop702.jpg'
    }
  });
  click(cardsAfterPage2[0]);
  assert(!ids.detail.classList.contains('hidden'));
  assert.strictEqual(ids.detailBackdropImg.src,
    'http://127.0.0.1:18767/__proxy/img.belet.me/banners/backdrop702.jpg',
    'detailBackdropImg renders film backdrop via ClockProxy');
})();

assert.strictEqual(routes.length, 0, 'all queued responses were consumed: ' + JSON.stringify(routes.map(r => r.match)));
console.log('belet_samsung_client_mock: ok');
