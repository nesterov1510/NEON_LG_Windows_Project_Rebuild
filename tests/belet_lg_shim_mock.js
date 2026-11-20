/* Smoke test for belet_lg_client/webos-shim.js on a fake webOS DOM. */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SHIM = path.join(__dirname, '..', 'belet_lg_client', 'webos-shim.js');

function makeNode(className) {
  const node = {
    className: className || '',
    classList: {
      contains(name) { return String(this.owner.className || '').split(' ').indexOf(name) >= 0; }
    },
    focused: 0,
    focus() { node.focused += 1; sandbox.document.activeElement = node; }
  };
  node.classList.owner = node;
  return node;
}

const listeners = {};
const document = {
  activeElement: null,
  addEventListener(type, handler) { (listeners[type] = listeners[type] || []).push(handler); }
};
const calls = { platformBack: 0 };

const sandbox = {
  document,
  navigator: { userAgent: 'Mozilla/5.0 (Web0S; Linux/SmartTV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/68.0.3440.106 Safari/537.36 WebAppManager' },
  setTimeout(fn) { fn(); return 0; },
  PalmSystem: {
    deviceInfo: JSON.stringify({ modelName: 'OLED55C9PUA', version: '4.9.0', platform: 'webOS' }),
    platformBack() { calls.platformBack += 1; }
  },
  close() { calls.platformBack += 100; }
};
sandbox.window = sandbox;
sandbox.global = sandbox;
sandbox.self = sandbox;

vm.runInNewContext(fs.readFileSync(SHIM, 'utf8'), sandbox, { filename: 'webos-shim.js' });

/* device identity is exposed through the Tizen surfaces api.js already reads */
assert.strictEqual(sandbox.webapis.productinfo.getModel(), 'OLED55C9PUA', 'model comes from PalmSystem.deviceInfo');
assert.strictEqual(sandbox.tizen.systeminfo.getCapability('http://tizen.org/feature/platform.version'), 'webOS 4.9.0',
  'os_version is reported as webOS <version>');
assert.strictEqual(sandbox.tizen.systeminfo.getCapability('http://tizen.org/system/model_name'), 'OLED55C9PUA');
assert.strictEqual(sandbox.__NEON_TV_PLATFORM__, 'webos');

/* the shim must not fake Tizen-only player APIs */
assert.strictEqual(sandbox.webapis.avplay, undefined, 'no avplay on webOS: the hls.js/<video> chain is used');

/* Back: consumed by the client -> nothing happens, unconsumed -> leave the app */
const keydown = listeners.keydown || [];
assert.strictEqual(keydown.length, 1, 'one Back listener is registered after app.js');
keydown[0]({ keyCode: 461, defaultPrevented: true });
assert.strictEqual(calls.platformBack, 0, 'a handled Back stays inside the app');
keydown[0]({ keyCode: 461, defaultPrevented: false });
assert.strictEqual(calls.platformBack, 1, 'an unhandled Back leaves the app');
keydown[0]({ keyCode: 13, defaultPrevented: false });
assert.strictEqual(calls.platformBack, 1, 'OK is not the Back key');

/* Magic Remote hover moves the DOM focus to the nearest focusable element */
const card = makeNode('card focusable');
const label = makeNode('name');
label.parentNode = card;
const mouseover = listeners.mouseover || [];
assert.strictEqual(mouseover.length, 1, 'pointer support is installed');
mouseover[0]({ target: label });
assert.strictEqual(card.focused, 1, 'hovering a card focuses it');
assert.strictEqual(document.activeElement, card);

console.log('belet_lg_shim_mock: ok');
