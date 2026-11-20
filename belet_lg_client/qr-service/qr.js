/* TV-local LS2 method, not an HTTP proxy. No caller-supplied URL, headers,
 * redirects, filesystem storage or logging of pairing codes and tokens. */
'use strict';
var https = require('https');
var Service = require('webos-service');
var service = new Service(require('./package.json').name);

service.register('check', function (message) {
  var p = message.payload || {};
  if (typeof p.code !== 'string' || !/^[A-Za-z0-9_-]{1,512}$/.test(p.code) ||
      typeof p.fingerprint !== 'string' || !/^[A-Za-z0-9]{1,64}$/.test(p.fingerprint)) {
    message.respond({ returnValue: false, errorText: 'Invalid pairing request' });
    return;
  }
  post(message, '/api/v1/auth/check-qrcode' + (p.web === true ? '-web' : ''),
    { code: p.code, fingerprint: p.fingerprint });
});

service.register('registerDevice', function (message) {
  var p = message.payload || {}, b = p.body || {};
  if (typeof p.token !== 'string' || !/^[\x21-\x7e]{1,8192}$/.test(p.token) ||
      typeof b.model !== 'string' || !b.model || b.model.length > 200 ||
      typeof b.os_version !== 'string' || !b.os_version || b.os_version.length > 100 ||
      typeof b.fingerprint !== 'string' || !/^[A-Za-z0-9]{1,64}$/.test(b.fingerprint)) {
    message.respond({ returnValue: false, errorText: 'Invalid registration request' });
    return;
  }
  post(message, '/api/v1/users/device', { model: b.model, os_version: b.os_version,
    fingerprint: b.fingerprint, app_version: '4.3.3' }, p.token);
});

function post(message, path, payload, token) {
  var body = JSON.stringify(payload);
  var done = false;
  function finish(status, data) {
    if (done) return;
    done = true;
    clearTimeout(timer);
    message.respond({ returnValue: true, status: status, data: data || null });
  }
  var headers = { Accept: 'application/json', 'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body), Lang: 'ru', fingerprint: payload.fingerprint,
    'X-Platform': 'Android', 'X-VersionCode': '182', 'app-version': '182', 'X-App-Version': '4.3.3' };
  if (token) {
    headers.Authorization = token;
    headers['X-Device-Model'] = payload.model.replace(/[^ -~]/g, '_');
  }
  var req = https.request({ hostname: 'beletapis.com', method: 'POST', path: path, headers: headers }, function (res) {
    var text = '';
    res.setEncoding('utf8');
    res.on('data', function (chunk) {
      if (done) return;
      text += chunk;
      if (text.length > 65536) { finish(502); req.abort(); }
    });
    res.on('error', function () { finish(0); });
    res.on('end', function () {
      var data;
      if (token) return finish(res.statusCode);
      if (res.statusCode >= 200 && res.statusCode < 300 && text) {
        try { data = JSON.parse(text); } catch (err) { return finish(502); }
      }
      finish(res.statusCode, data && typeof data.token === 'string' ?
        { token: data.token, refresh_token: data.refresh_token || '' } : null);
    });
  });
  var timer = setTimeout(function () { finish(0); req.abort(); }, 12000);
  req.on('error', function () { finish(0); });
  req.end(body);
}
