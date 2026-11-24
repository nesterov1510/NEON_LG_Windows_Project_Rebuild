/*
 * Tizen local TLS clock-recovery service for Belet Samsung (BeletFilm1.ClockProxy).
 *
 * Only activated by the app when Chromium WebView cannot validate modern HTTPS
 * certificates because the TV kernel clock is stuck in the past (e.g. 1970-01-01).
 * Strict ES5 and self-contained for Tizen 4..10 Node.js service engines.
 */
(function (nodeRequire, hostModule, hostExports) {
  'use strict';

  var reqFn = nodeRequire || (typeof require === 'function' ? require : null);
  if (!reqFn && typeof process !== 'undefined') {
    try {
      if (typeof process.getBuiltinModule === 'function') {
        var bm = process.getBuiltinModule('module');
        if (bm && typeof bm.createRequire === 'function') reqFn = bm.createRequire(process.cwd() + '/');
      }
    } catch (e) {}
    if (!reqFn) {
      try {
        if (process.mainModule && typeof process.mainModule.require === 'function') {
          reqFn = process.mainModule.require.bind(process.mainModule);
        }
      } catch (e) {}
    }
    if (!reqFn) {
      try {
        if (typeof process.getBuiltinModule === 'function') {
          reqFn = function (m) { return process.getBuiltinModule(m); };
        }
      } catch (e) {}
    }
  }

  var http = reqFn('http');
  var https = reqFn('https');
  var url = reqFn('url');
  var os = reqFn('os');
  var dnsMod = null;
  var zlib = null;
  var tlsMod = null;
  try { dnsMod = reqFn('dns'); } catch (e) {}
  try { zlib = reqFn('zlib'); } catch (e) {}
  try { tlsMod = reqFn('tls'); } catch (e) {}

  var PORT = 18767;
  var LISTEN_HOST = '0.0.0.0';
  var MIN_VALID_MS = 1704067200000; /* 2024-01-01T00:00:00Z */
  var MAX_BUFFERED_BODY = 256 * 1024;
  var ANDROID_UA = 'belet_films_android';
  var WEB_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
  var FALLBACK_DNS_SERVERS = ['119.235.124.77', '1.1.1.1', '8.8.8.8'];
  var BASE_DOMAINS = ['beletapis.com', 'belet.tm', 'belet.me'];
  var API_SCAN_HOSTS = {
    'beletapis.com': true,
    'api.belet.tm': true,
    'homepage.belet.me': true,
    'film.beletapis.com': true,
    'film-search.belet.me': true,
    'search.belet.me': true
  };
  var dynamicHosts = {};
  var dynamicOrder = [];
  var resolvedIpCache = {};
  var lastServerDateMs = 0;
  var lastServerMonoMs = 0;
  var strictAgent = null;
  var skewAgent = null;

  try {
    strictAgent = new https.Agent({ keepAlive: true, maxSockets: 8, keepAliveMsecs: 15000 });
  } catch (e) { strictAgent = null; }
  try {
    skewAgent = new https.Agent({
      keepAlive: true,
      maxSockets: 8,
      maxCachedSessions: 0,
      keepAliveMsecs: 15000,
      rejectUnauthorized: false
    });
  } catch (e) { skewAgent = null; }

  function bufFrom(data, enc) {
    if (typeof Buffer.from === 'function' && Buffer.from !== Uint8Array.from) {
      return enc ? Buffer.from(data, enc) : Buffer.from(data);
    }
    return enc ? new Buffer(data, enc) : new Buffer(data);
  }

  function trimStr(s) {
    return String(s || '').replace(/^\s+|\s+$/g, '');
  }

  function ownAddresses() {
    var out = ['127.0.0.1'];
    var ifs;
    var name;
    var list;
    var i;
    var addr;
    try { ifs = os.networkInterfaces() || {}; } catch (e) { return out; }
    for (name in ifs) {
      if (!Object.prototype.hasOwnProperty.call(ifs, name)) continue;
      list = ifs[name] || [];
      for (i = 0; i < list.length; i += 1) {
        addr = list[i];
        if (addr && addr.address) {
          out.push(String(addr.address).replace(/^::ffff:/, '').replace(/%.*$/, ''));
        }
      }
    }
    return out;
  }

  function fromThisTv(req) {
    var sock = req && (req.socket || req.connection);
    var raw = sock && sock.remoteAddress ? String(sock.remoteAddress) : '';
    var ip;
    if (!raw) return true;
    ip = raw.replace(/^::ffff:/, '').replace(/%.*$/, '');
    if (ip === '::1' || /^127\./.test(ip)) return true;
    return ownAddresses().indexOf(ip) >= 0;
  }

  function isSafePublicHost(rawHost) {
    var h = trimStr(rawHost).toLowerCase();
    if (!h || h.length > 253) return false;
    if (/[\s\/\\@:\[\]]/.test(h)) return false;
    if (!/^[a-z0-9.-]+$/.test(h)) return false;
    if (h.indexOf('.') <= 0 || h.charAt(0) === '.' || h.charAt(h.length - 1) === '.') return false;
    if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(h)) return false;
    if (h === 'localhost' || /\.local$/i.test(h) || /\.internal$/i.test(h) || /\.lan$/i.test(h) || /\.home\.arpa$/i.test(h)) {
      return false;
    }
    return true;
  }

  function isStaticAllowedHost(host) {
    var h = String(host || '').toLowerCase();
    var i;
    var d;
    if (!isSafePublicHost(h)) return false;
    for (i = 0; i < BASE_DOMAINS.length; i += 1) {
      d = BASE_DOMAINS[i];
      if (h === d || (h.length > d.length + 1 && h.slice(-d.length - 1) === '.' + d)) return true;
    }
    return false;
  }

  function registerDynamicHost(host) {
    var h = trimStr(host).toLowerCase();
    if (!isSafePublicHost(h) || isStaticAllowedHost(h)) return;
    if (Object.prototype.hasOwnProperty.call(dynamicHosts, h)) return;
    dynamicHosts[h] = true;
    dynamicOrder.push(h);
    if (dynamicOrder.length > 256) {
      delete dynamicHosts[dynamicOrder.shift()];
    }
  }

  function isAllowedHost(host) {
    var h = trimStr(host).toLowerCase();
    if (!isSafePublicHost(h)) return false;
    if (isStaticAllowedHost(h)) return true;
    return !!dynamicHosts[h];
  }

  function estimatedServerTimeMs() {
    if (!lastServerDateMs) return 0;
    return lastServerDateMs + Math.max(0, Date.now() - lastServerMonoMs);
  }

  function recordServerDate(dateHeader) {
    var ms;
    if (!dateHeader) return 0;
    ms = Date.parse(String(dateHeader));
    if (!isNaN(ms) && ms >= MIN_VALID_MS) {
      lastServerDateMs = ms;
      lastServerMonoMs = Date.now();
      return ms;
    }
    return 0;
  }

  function setCors(res, reqOrigin, reqHeaders) {
    var safeOrig = (reqOrigin && reqOrigin !== 'null' && reqOrigin !== 'file://') ? reqOrigin : '*';
    res.setHeader('Access-Control-Allow-Origin', safeOrig);
    if (safeOrig !== '*') res.setHeader('Access-Control-Allow-Credentials', 'true');
    res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,PATCH,DELETE,OPTIONS,HEAD');
    res.setHeader('Access-Control-Allow-Headers', reqHeaders ||
      'Accept,Authorization,Content-Type,Lang,X-Platform,X-VersionCode,X-App-Version,app-version,' +
      'fingerprint,X-Device-Model,X-Code-Sending-Type,X-Player-Session-Id,X-Chunk-Length,Range');
    res.setHeader('Access-Control-Allow-Private-Network', 'true');
    res.setHeader('Cross-Origin-Resource-Policy', 'cross-origin');
  }

  function isLegacyTizenNode() {
    var hasRoots = !!(tlsMod && tlsMod.rootCertificates && tlsMod.rootCertificates.length);
    var major = 0;
    try {
      major = parseInt(String((typeof process !== 'undefined' && process.versions && process.versions.node) || '0'), 10) || 0;
    } catch (e) {}
    return !hasRoots || (major > 0 && major < 12);
  }

  function isSelfSignedCert(cert) {
    if (!cert) return false;
    if (cert.issuerCertificate && cert.issuerCertificate === cert) return true;
    if (cert.fingerprint && cert.issuerCertificate && cert.issuerCertificate.fingerprint === cert.fingerprint) return true;
    if (!cert.issuerCertificate && cert.subject && cert.issuer) {
      try {
        if (JSON.stringify(cert.subject) === JSON.stringify(cert.issuer)) return true;
      } catch (e) {}
    }
    return false;
  }

  function verifyClockSkewSocket(sock, upHost, clockOk) {
    var cert;
    var idErr;
    var authErr;
    var checkMs;
    var dateErr;
    if (!sock) return new Error('missing TLS socket');
    if (sock.__neonVerifiedHost === upHost) {
      if (clockOk) {
        dateErr = verifyCertWindowAgainstDate(sock, Date.now());
        if (dateErr) return dateErr;
      }
      return null;
    }
    cert = typeof sock.getPeerCertificate === 'function' ? sock.getPeerCertificate(true) : null;
    if (!cert || !cert.subject) return new Error('missing peer certificate');
    if (tlsMod && typeof tlsMod.checkServerIdentity === 'function') {
      idErr = tlsMod.checkServerIdentity(upHost, cert);
      if (idErr) return idErr;
    }
    checkMs = clockOk ? Date.now() : estimatedServerTimeMs();
    if (checkMs) {
      dateErr = verifyCertWindowAgainstDate(sock, checkMs);
      if (dateErr) return dateErr;
    }
    if (sock.authorized) {
      sock.__neonVerifiedHost = upHost;
      return null;
    }
    authErr = String(sock.authorizationError || '');
    if (!clockOk && authErr === 'CERT_NOT_YET_VALID') {
      sock.__neonVerifiedHost = upHost;
      return null;
    }
    if ((authErr === 'UNABLE_TO_GET_ISSUER_CERT_LOCALLY' ||
         authErr === 'UNABLE_TO_GET_ISSUER_CERT' ||
         authErr === 'UNABLE_TO_VERIFY_LEAF_SIGNATURE' ||
         (authErr === 'SELF_SIGNED_CERT_IN_CHAIN' && isLegacyTizenNode())) &&
        !isSelfSignedCert(cert)) {
      sock.__neonVerifiedHost = upHost;
      return null;
    }
    return new Error('TLS certificate rejected: ' + (authErr || 'unauthorized'));
  }

  function verifyCertWindowAgainstDate(sock, serverMs) {
    var cert;
    var vf;
    var vt;
    if (!serverMs || !sock || typeof sock.getPeerCertificate !== 'function') return null;
    cert = sock.getPeerCertificate(false);
    if (!cert) return null;
    vf = cert.valid_from ? Date.parse(cert.valid_from) : NaN;
    vt = cert.valid_to ? Date.parse(cert.valid_to) : NaN;
    if (!isNaN(vf) && serverMs < vf - 86400000) return new Error('certificate not yet valid for server date');
    if (!isNaN(vt) && serverMs > vt + 86400000) return new Error('certificate expired for server date');
    return null;
  }

  function decodeBody(buf, enc, cb) {
    var e = String(enc || '').toLowerCase();
    if (zlib && e.indexOf('gzip') >= 0 && typeof zlib.gunzip === 'function') {
      zlib.gunzip(buf, function (err, res) { cb(err ? buf : res); });
      return;
    }
    if (zlib && e.indexOf('deflate') >= 0 && typeof zlib.inflate === 'function') {
      zlib.inflate(buf, function (err, res) {
        if (!err) return cb(res);
        if (typeof zlib.inflateRaw === 'function') {
          zlib.inflateRaw(buf, function (err2, res2) { cb(err2 ? buf : res2); });
        } else {
          cb(buf);
        }
      });
      return;
    }
    if (zlib && e.indexOf('br') >= 0 && typeof zlib.brotliDecompress === 'function') {
      zlib.brotliDecompress(buf, function (err, res) { cb(err ? buf : res); });
      return;
    }
    cb(buf);
  }

  function scanJsonForHosts(text) {
    var clean = String(text || '').replace(/\\u002f/gi, '/');
    var rx = /https?:(?:\\\/|\/){2}([a-z0-9.-]+)(?::\d+)?(?:[\/"'?:\\]|$)/gi;
    var match;
    while ((match = rx.exec(clean)) !== null) {
      registerDynamicHost(match[1]);
    }
  }

  function rewriteM3u8(text, proxyOrigin, upHost) {
    var out = String(text || '').replace(/https:\/\/([a-z0-9.-]+)(?::443)?([^\s"'<>]*)/gi, function (full, host, rest) {
      var h = String(host || '').toLowerCase();
      var p = rest || '/';
      if (!isSafePublicHost(h)) return full;
      registerDynamicHost(h);
      if (p.charAt(0) !== '/') p = '/' + p;
      return proxyOrigin + '/__proxy/' + h + p;
    });
    out = out.replace(/URI="\/\/([a-z0-9.-]+)(\/[^"]*)"/gi, function (full, host, p) {
      var h = String(host || '').toLowerCase();
      if (!isSafePublicHost(h)) return full;
      registerDynamicHost(h);
      return 'URI="' + proxyOrigin + '/__proxy/' + h + p + '"';
    });
    if (upHost && isSafePublicHost(upHost)) {
      out = out.replace(/URI="(\/(?!\/)[^"]*)"/g, function (full, p) {
        return 'URI="' + proxyOrigin + '/__proxy/' + upHost + p + '"';
      });
      out = out.replace(/(^|\r?\n)(\/(?!\/)[^\r\n]*)/g, function (full, br, p) {
        return br + proxyOrigin + '/__proxy/' + upHost + p;
      });
    }
    return out;
  }

  function rewriteRedirectLocation(loc, proxyOrigin, upHost) {
    var raw = (Object.prototype.toString.call(loc) === '[object Array]') ? loc[0] : loc;
    var s = String(raw || '');
    var m = s.match(/^https:\/\/([a-z0-9.-]+)(?::443)?(\/.*)?$/i);
    var mh;
    var mp;
    var h;
    if (m) {
      h = m[1].toLowerCase();
      if (!isSafePublicHost(h)) return s;
      registerDynamicHost(h);
      return proxyOrigin + '/__proxy/' + h + (m[2] || '/');
    }
    mh = s.match(/^http:\/\/([a-z0-9.-]+)(?::80)?(\/.*)?$/i);
    if (mh) {
      h = mh[1].toLowerCase();
      if (isStaticAllowedHost(h)) return proxyOrigin + '/__proxy/' + h + (mh[2] || '/');
      return s;
    }
    mp = s.match(/^\/\/([a-z0-9.-]+)(\/.*)?$/);
    if (mp) {
      h = mp[1].toLowerCase();
      if (!isSafePublicHost(h)) return s;
      registerDynamicHost(h);
      return proxyOrigin + '/__proxy/' + h + (mp[2] || '/');
    }
    if (s.charAt(0) === '/' && upHost && isSafePublicHost(upHost)) {
      return proxyOrigin + '/__proxy/' + upHost + s;
    }
    return s;
  }

  function parseProxyRoute(rawUrl) {
    var parsed = url.parse(rawUrl || '/');
    var pathname = parsed.pathname || '/';
    var m = pathname.match(/^\/__proxy\/([a-z0-9.-]+)(\/.*)?$/i);
    if (!m) return null;
    return {
      host: m[1].toLowerCase(),
      path: (m[2] || '/') + (parsed.search || ''),
      pathname: m[2] || '/'
    };
  }

  function sendJsonError(res, status, msg) {
    var body;
    if (res.headersSent) return;
    body = bufFrom(JSON.stringify({ ok: false, error: msg }), 'utf8');
    res.writeHead(status, {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': body.length,
      'Cache-Control': 'no-store'
    });
    res.end(body);
  }

  function makeFixedLookup(ip) {
    return function (hostname, opts, cb) {
      if (typeof opts === 'function') { cb = opts; opts = {}; }
      if (opts && opts.all) cb(null, [{ address: ip, family: 4 }]);
      else cb(null, ip, 4);
    };
  }

  function getCachedIpv4(host) {
    var entry = resolvedIpCache[host];
    if (!entry) return null;
    if (Date.now() - entry.at > 300000) {
      delete resolvedIpCache[host];
      return null;
    }
    return entry.ip;
  }

  function fallbackResolveIpv4(host, cb) {
    var cached = getCachedIpv4(host);
    var resolver;
    var done = false;
    var timer;
    function finish(ip) {
      if (done) return;
      done = true;
      if (timer) clearTimeout(timer);
      if (ip) resolvedIpCache[host] = { ip: ip, at: Date.now() };
      cb(ip || null);
    }
    if (cached) { finish(cached); return; }
    if (!dnsMod || typeof dnsMod.Resolver !== 'function' || !isSafePublicHost(host)) {
      finish(null);
      return;
    }
    try {
      resolver = new dnsMod.Resolver();
      resolver.setServers(FALLBACK_DNS_SERVERS);
      timer = setTimeout(function () { finish(null); }, 1800);
      resolver.resolve4(host, function (err, addrs) {
        var i;
        var addr;
        if (!err && addrs && addrs.length) {
          for (i = 0; i < addrs.length; i += 1) {
            addr = String(addrs[i] || '');
            if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(addr) && !/^127\.|^0\./.test(addr)) {
              finish(addr);
              return;
            }
          }
        }
        finish(null);
      });
    } catch (e) {
      finish(null);
    }
  }

  function handleRequest(req, res) {
    var reqOrigin = req.headers && req.headers.origin ? String(req.headers.origin) : '';
    var reqHdrs = req.headers && req.headers['access-control-request-headers']
      ? String(req.headers['access-control-request-headers']) : '';
    var parsedReq;
    var pathname;
    var route;
    var upHost;
    var upPath;
    var upPathname;
    var proxyHostHdr;
    var proxyOrigin;
    var clockOk;
    var inH;
    var outH;
    var k;
    var lk;
    var reqMethod;
    var isBeletStaticHost;
    var isBodyless;
    var aborted = false;
    var reqPaused = false;
    var canRetry = false;
    var reqBodyBuf = null;
    var preChunks = null;
    var activeUpReq = null;

    if (!fromThisTv(req)) {
      res.writeHead(403, { 'Content-Type': 'text/plain' });
      res.end('Forbidden');
      return;
    }

    setCors(res, reqOrigin, reqHdrs);
    if (req.method === 'OPTIONS') {
      res.writeHead(204, { 'Cache-Control': 'no-store' });
      res.end();
      return;
    }

    parsedReq = url.parse(req.url || '/');
    pathname = parsedReq.pathname || '/';
    if (pathname === '/__ping') {
      var pingBuf = bufFrom(JSON.stringify({
        ok: true,
        mode: 'clock-proxy',
        port: PORT,
        serverTimeMs: estimatedServerTimeMs() || null,
        sslClockYear: new Date().getFullYear()
      }), 'utf8');
      res.writeHead(200, {
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Length': pingBuf.length,
        'Cache-Control': 'no-store'
      });
      res.end(pingBuf);
      return;
    }

    route = parseProxyRoute(req.url || '/');
    if (!route) {
      sendJsonError(res, 404, 'Unknown proxy route');
      return;
    }
    upHost = route.host;
    upPath = route.path;
    upPathname = route.pathname;
    if (!isAllowedHost(upHost)) {
      sendJsonError(res, 403, 'Disallowed host');
      return;
    }

    proxyHostHdr = (req.headers && req.headers.host) ? String(req.headers.host) : ('127.0.0.1:' + PORT);
    proxyOrigin = 'http://' + proxyHostHdr;
    clockOk = (new Date().getFullYear() >= 2026);
    inH = req.headers || {};
    outH = {};
    reqMethod = String(req.method || 'GET').toUpperCase();
    isBeletStaticHost = isStaticAllowedHost(upHost);
    isBodyless = (reqMethod === 'GET' || reqMethod === 'HEAD');
    canRetry = isBodyless;
    for (k in inH) {
      if (!Object.prototype.hasOwnProperty.call(inH, k)) continue;
      lk = k.toLowerCase();
      if (lk === 'host' || lk === 'connection' || lk === 'keep-alive' || lk === 'proxy-connection' ||
          lk === 'origin' || lk === 'referer' || lk === 'accept-encoding' ||
          lk === 'x-forwarded-host' || lk === 'x-forwarded-proto' || lk === 'x-forwarded-for' ||
          lk.indexOf('sec-fetch-') === 0 ||
          (isBeletStaticHost && lk === 'user-agent')) {
        continue;
      }
      outH[lk] = inH[k];
    }
    outH.host = upHost;
    if (isBeletStaticHost) outH['user-agent'] = ANDROID_UA;
    if (zlib) outH['accept-encoding'] = 'gzip, deflate';
    else outH['accept-encoding'] = 'identity';

    res.on('close', function () {
      if (!res.finished && !res.writableFinished) {
        aborted = true;
        if (activeUpReq) {
          try { activeUpReq.abort(); } catch (e) {}
        }
      }
    });

    function startAttempt(useFreshSocket, retryCount, forceIpv4, useWebFallback, overrideIp) {
      var reqOpts;
      var attemptHeaders = {};
      var hk2;
      var upReq;
      var startedUpstream = false;
      var tlsRejected = false;
      var attemptTimedOut = false;
      var connectTimer = null;
      var ipToUse = overrideIp || getCachedIpv4(upHost);
      var isMediaPath = /\.m3u8|\.(?:ts|m4s|mp4)(?:$|\?)/i.test(upPathname);

      if (aborted) return;

      for (hk2 in outH) {
        if (Object.prototype.hasOwnProperty.call(outH, hk2)) attemptHeaders[hk2] = outH[hk2];
      }
      if (useWebFallback && isBeletStaticHost) {
        attemptHeaders['user-agent'] = WEB_UA;
        attemptHeaders.referer = 'https://belet.tm/';
      }

      reqOpts = {
        hostname: upHost,
        port: 443,
        path: upPath,
        method: reqMethod,
        headers: attemptHeaders,
        servername: upHost,
        rejectUnauthorized: false
      };
      if (ipToUse) {
        reqOpts.lookup = makeFixedLookup(ipToUse);
      } else if (forceIpv4) {
        reqOpts.family = 4;
      }
      if (!useFreshSocket && skewAgent) reqOpts.agent = skewAgent;
      else reqOpts.agent = false;

      function clearConnectTimer() {
        if (connectTimer) {
          clearTimeout(connectTimer);
          connectTimer = null;
        }
      }

      function flushBodyToUpstream() {
        var ci;
        clearConnectTimer();
        if (startedUpstream || aborted || tlsRejected) return;
        startedUpstream = true;
        if (isBodyless) {
          upReq.end();
          return;
        }
        if (reqBodyBuf !== null) {
          upReq.end(reqBodyBuf);
          return;
        }
        if (preChunks && preChunks.length) {
          for (ci = 0; ci < preChunks.length; ci += 1) upReq.write(preChunks[ci]);
          preChunks = null;
        }
        if (reqPaused && typeof req.resume === 'function') req.resume();
        req.pipe(upReq);
      }

      upReq = https.request(reqOpts, function (upRes) {
        var upH = upRes.headers || {};
        var status = upRes.statusCode || 200;
        var serverMs = recordServerDate(upH.date);
        var dateErr;
        var respH = {};
        var hk;
        var lhk;
        var ctype;
        var isM3u8;
        var isJsonScan;
        var chunks;
        var total;
        var overflow;

        clearConnectTimer();
        dateErr = verifyCertWindowAgainstDate(upRes.socket, serverMs || (clockOk ? Date.now() : 0));
        if (dateErr) {
          tlsRejected = true;
          upRes.resume();
          sendJsonError(res, 502, dateErr.message);
          return;
        }

        if (canRetry && retryCount < 2 && !aborted) {
          if (status === 403 && isBeletStaticHost && isBodyless && !useWebFallback) {
            upRes.resume();
            startAttempt(true, retryCount + 1, forceIpv4, true, ipToUse);
            return;
          }
          if (status === 502 || status === 503 || status === 504 || status === 429) {
            upRes.resume();
            startAttempt(true, retryCount + 1, true, useWebFallback, ipToUse);
            return;
          }
        }

        for (hk in upH) {
          if (!Object.prototype.hasOwnProperty.call(upH, hk)) continue;
          lhk = hk.toLowerCase();
          if (lhk === 'transfer-encoding' || lhk === 'connection' || lhk === 'keep-alive' ||
              lhk === 'proxy-connection' || lhk === 'upgrade' || lhk === 'te' || lhk === 'trailer' ||
              lhk === 'strict-transport-security' || lhk === 'content-security-policy' ||
              lhk === 'content-security-policy-report-only' || lhk === 'cross-origin-resource-policy') {
            continue;
          }
          if (lhk === 'location') {
            respH[lhk] = rewriteRedirectLocation(upH[hk], proxyOrigin, upHost);
            continue;
          }
          respH[lhk] = upH[hk];
        }

        ctype = String(respH['content-type'] || '').toLowerCase();
        isM3u8 = /\.m3u8(?:$|\?)/i.test(upPathname) || ctype.indexOf('mpegurl') >= 0;
        isJsonScan = (!!API_SCAN_HOSTS[upHost] || isStaticAllowedHost(upHost)) &&
          (ctype.indexOf('json') >= 0 || ctype.indexOf('text/plain') >= 0);

        upRes.on('error', function (uErr) {
          if (!aborted && !res.headersSent) {
            sendJsonError(res, 502, (uErr && uErr.message) ? uErr.message : 'Upstream stream error');
          } else {
            try { res.destroy(); } catch (e) {}
          }
        });
        upRes.on('aborted', function () {
          if (!aborted && res.headersSent && !res.writableFinished && !res.finished) {
            try { res.destroy(); } catch (e) {}
          }
        });

        if ((isM3u8 || isJsonScan) && reqMethod !== 'HEAD' && status >= 200 && status < 300) {
          chunks = [];
          total = 0;
          overflow = false;
          upRes.on('data', function (ch) {
            total += ch.length;
            if (total > 4 * 1024 * 1024) {
              if (!overflow) {
                overflow = true;
                if (!aborted && !res.headersSent) {
                  res.writeHead(status, respH);
                  for (var ci = 0; ci < chunks.length; ci += 1) res.write(chunks[ci]);
                }
                chunks = null;
              }
              if (!aborted && res.headersSent) res.write(ch);
              return;
            }
            chunks.push(ch);
          });
          upRes.on('end', function () {
            var rawBuf;
            if (aborted) return;
            if (overflow) {
              if (res.headersSent) res.end();
              return;
            }
            rawBuf = Buffer.concat(chunks);
            decodeBody(rawBuf, respH['content-encoding'], function (plainBuf) {
              var text = plainBuf.toString('utf8');
              var outBuf;
              if (isJsonScan) {
                scanJsonForHosts(text);
                outBuf = rawBuf;
              } else {
                text = rewriteM3u8(text, proxyOrigin, upHost);
                outBuf = bufFrom(text, 'utf8');
                delete respH['content-encoding'];
              }
              respH['content-length'] = outBuf.length;
              if (!res.headersSent) {
                res.writeHead(status, respH);
                res.end(outBuf);
              }
            });
          });
          return;
        }

        if (!aborted && !res.headersSent) res.writeHead(status, respH);
        if (reqMethod === 'HEAD' || status === 204 || status === 304) {
          upRes.resume();
          res.end();
          return;
        }
        upRes.pipe(res);
      });

      activeUpReq = upReq;

      if (!isMediaPath) {
        connectTimer = setTimeout(function () {
          connectTimer = null;
          if (!startedUpstream && !aborted && !tlsRejected) {
            attemptTimedOut = true;
            try { upReq.abort(); } catch (e) {}
          }
        }, 8000);
      }

      upReq.on('socket', function (sock) {
        var peerReady;
        if (!sock) {
          flushBodyToUpstream();
          return;
        }
        if (sock.__neonVerifiedHost === upHost) {
          if (clockOk) {
            var cachedDateErr = verifyClockSkewSocket(sock, upHost, clockOk);
            if (cachedDateErr) {
              clearConnectTimer();
              tlsRejected = true;
              try { upReq.destroy(cachedDateErr); } catch (e) {}
              sendJsonError(res, 502, cachedDateErr.message);
              return;
            }
          }
          flushBodyToUpstream();
          return;
        }
        peerReady = !!(sock.encrypted && !sock.connecting && !sock.pending &&
          typeof sock.getPeerCertificate === 'function' && sock.getPeerCertificate(false) &&
          sock.getPeerCertificate(false).subject);
        if (peerReady) {
          var syncErr = verifyClockSkewSocket(sock, upHost, clockOk);
          if (syncErr) {
            clearConnectTimer();
            tlsRejected = true;
            try { upReq.destroy(syncErr); } catch (e) {}
            sendJsonError(res, 502, syncErr.message);
            return;
          }
          flushBodyToUpstream();
          return;
        }
        if (typeof sock.once === 'function') {
          sock.once('secureConnect', function () {
            var tlsErr = verifyClockSkewSocket(sock, upHost, clockOk);
            if (tlsErr) {
              clearConnectTimer();
              tlsRejected = true;
              try { upReq.destroy(tlsErr); } catch (e) {}
              sendJsonError(res, 502, tlsErr.message);
              return;
            }
            flushBodyToUpstream();
          });
        } else {
          flushBodyToUpstream();
        }
      });

      upReq.setTimeout(isMediaPath ? 45000 : 25000, function () {
        attemptTimedOut = true;
        try { upReq.abort(); } catch (e) {}
      });

      upReq.on('error', function (err) {
        var code = err && err.code ? String(err.code) : '';
        var msg = String(err && err.message || '');
        var netRetryable = (
          attemptTimedOut ||
          code === 'ECONNRESET' || code === 'EPIPE' || code === 'ERR_STREAM_DESTROYED' ||
          code === 'ETIMEDOUT' || code === 'ESOCKETTIMEDOUT' || code === 'ECONNABORTED' ||
          code === 'ENETUNREACH' || code === 'EHOSTUNREACH' || code === 'ECONNREFUSED' ||
          /socket hang up/i.test(msg)
        );
        var dnsFailed = (code === 'ENOTFOUND' || code === 'EAI_AGAIN' || code === 'ECONNREFUSED');
        clearConnectTimer();
        if (aborted || res.headersSent) return;
        if (retryCount < 2 && !tlsRejected && canRetry && dnsFailed && !ipToUse && isSafePublicHost(upHost)) {
          fallbackResolveIpv4(upHost, function (resolvedIp) {
            if (aborted || res.headersSent) return;
            if (resolvedIp) {
              startAttempt(true, retryCount + 1, true, useWebFallback, resolvedIp);
              return;
            }
            if (code === 'ECONNREFUSED') {
              startAttempt(true, retryCount + 1, true, useWebFallback, null);
              return;
            }
            sendJsonError(res, 502, msg || 'Upstream DNS error');
          });
          return;
        }
        if (retryCount < 2 && !tlsRejected && canRetry && netRetryable) {
          startAttempt(true, retryCount + 1, true, useWebFallback, ipToUse);
          return;
        }
        sendJsonError(res, 502, msg || 'Upstream network error');
      });
    }

    if (isBodyless) {
      startAttempt(false, 0, false, false, null);
      return;
    }

    var declaredLen = parseInt(String(inH['content-length'] || ''), 10);
    if (!isNaN(declaredLen) && declaredLen > MAX_BUFFERED_BODY) {
      if (typeof req.pause === 'function') {
        req.pause();
        reqPaused = true;
      }
      canRetry = false;
      startAttempt(false, 0, false, false, null);
      return;
    }

    var bodyChunks = [];
    var bodyLen = 0;
    var streamedFallback = false;

    function onBodyData(chunk) {
      if (streamedFallback || aborted) return;
      bodyLen += chunk.length;
      bodyChunks.push(chunk);
      if (bodyLen > MAX_BUFFERED_BODY) {
        streamedFallback = true;
        req.removeListener('data', onBodyData);
        req.removeListener('end', onBodyEnd);
        if (typeof req.pause === 'function') {
          req.pause();
          reqPaused = true;
        }
        preChunks = bodyChunks;
        bodyChunks = null;
        canRetry = false;
        startAttempt(false, 0, false, false, null);
      }
    }

    function onBodyEnd() {
      if (streamedFallback || aborted) return;
      req.removeListener('data', onBodyData);
      req.removeListener('end', onBodyEnd);
      reqBodyBuf = Buffer.concat(bodyChunks || []);
      bodyChunks = null;
      canRetry = true;
      outH['content-length'] = reqBodyBuf.length;
      delete outH['transfer-encoding'];
      startAttempt(false, 0, false, false, null);
    }

    req.on('error', function () {
      aborted = true;
    });
    req.on('data', onBodyData);
    req.on('end', onBodyEnd);
  }

  var server = http.createServer(handleRequest);
  var serverStarted = false;
  var listenRetries = 0;

  server.on('error', function (err) {
    serverStarted = false;
    if (err && err.code === 'EADDRINUSE' && listenRetries < 20) {
      listenRetries += 1;
      setTimeout(startServer, 350);
    }
  });
  server.on('close', function () { serverStarted = false; });

  function startServer() {
    if (serverStarted) return server;
    serverStarted = true;
    try { server.listen(PORT, LISTEN_HOST); } catch (e) { serverStarted = false; }
    return server;
  }

  startServer();

  if (typeof process !== 'undefined' && process.on) {
    process.on('uncaughtException', function () {});
  }

  var onStart = function () { startServer(); };
  var onRequest = function () { startServer(); };
  var onStop = function () {
    try {
      if (strictAgent && strictAgent.destroy) strictAgent.destroy();
      if (skewAgent && skewAgent.destroy) skewAgent.destroy();
      if (typeof server.closeAllConnections === 'function') server.closeAllConnections();
      server.close();
      serverStarted = false;
    } catch (e) {}
  };
  var onExit = onStop;

  var expObj = (hostModule && hostModule.exports) ? hostModule.exports : (hostExports || null);
  if (expObj) {
    expObj.server = server;
    expObj.PORT = PORT;
    expObj.LISTEN_HOST = LISTEN_HOST;
    expObj.startServer = startServer;
    expObj.onStart = onStart;
    expObj.onRequest = onRequest;
    expObj.onStop = onStop;
    expObj.onExit = onExit;
  } else if (typeof global === 'object' && global) {
    global.onStart = onStart;
    global.onRequest = onRequest;
    global.onStop = onStop;
    global.onExit = onExit;
  }
}(typeof require === 'function' ? require : null, typeof module === 'object' ? module : null, typeof exports === 'object' ? exports : null));
