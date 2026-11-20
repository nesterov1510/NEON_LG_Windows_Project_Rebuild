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
  var body = JSON.stringify({ code: p.code, fingerprint: p.fingerprint });
  var done = false;
  function finish(status, data) {
    if (done) return;
    done = true;
    clearTimeout(timer);
    message.respond({ returnValue: true, status: status, data: data || null });
  }
  var req = https.request({
    hostname: 'beletapis.com', method: 'POST',
    path: '/api/v1/auth/check-qrcode' + (p.web === true ? '-web' : ''),
    headers: { Accept: 'application/json', 'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': Buffer.byteLength(body), Lang: 'ru', fingerprint: p.fingerprint,
      'X-Platform': 'Android', 'X-VersionCode': '182', 'app-version': '182', 'X-App-Version': '4.3.3' }
  }, function (res) {
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
});
