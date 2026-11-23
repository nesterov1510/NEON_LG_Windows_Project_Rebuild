/*
 * Belet Samsung client — network layer.
 *
 * Endpoints, headers, request bodies and response shapes below were taken from the
 * provided decompiled Android client (belet_src.zip, tm.belet.films 4.3.3 / versionCode 182)
 * and from a HAR capture of the official belet.tm web client. Nothing here is a guess about
 * the service contract beyond the documented fallbacks; every fallback is explicit.
 *
 * This file must stay ES5: Samsung Tizen televisions ship older Chromium engines.
 */
(function (global) {
  'use strict';

  var HOSTS = {
    auth: 'https://beletapis.com',
    authAlt: 'https://api.belet.tm',
    home: 'https://homepage.belet.me',
    film: 'https://film.beletapis.com',
    search: 'https://film-search.belet.me',
    searchWeb: 'https://search.belet.me'
  };

  // Headers the Android client always attaches. `X-Platform: Android` selects the mobile
  // API branch (the web client sends `Web` and uses cookies instead of bearer tokens).
  var CLIENT = {
    platform: 'Android',
    appVersion: '4.3.3',
    versionCode: '182',
    lang: 'ru'
  };

  var SESSION_KEY = 'neon_belet_samsung_session_v2';
  var DEVICE_KEY = 'neon_belet_samsung_device_v2';
  var REQUEST_TIMEOUT = 25000;

  var session = null;
  var device = null;
  var refreshing = false;
  var refreshQueue = [];
  var generation = 0;
  var qrAuthHost = HOSTS.auth;
  var clockProxyBase = '';

  function setClockProxy(baseUrl) {
    clockProxyBase = baseUrl ? String(baseUrl).replace(/\/+$/, '') : '';
  }

  function proxyUrl(targetUrl) {
    var s;
    var match;
    var host;
    var rest;
    if (!clockProxyBase || typeof targetUrl !== 'string') return targetUrl;
    s = targetUrl;
    if (s.indexOf(clockProxyBase + '/__proxy/') === 0) return s;
    match = /^https:\/\/([^\/:?#]+)(?::443)?(.*)$/i.exec(s);
    if (!match) {
      match = /^http:\/\/([^\/:?#]+)(?::80)?(.*)$/i.exec(s);
      if (!match) return s;
      host = match[1].toLowerCase();
      if (!/(?:^|\.)(belet\.me|belet\.tm|beletapis\.com)$/i.test(host)) return s;
      rest = match[2] || '';
      return clockProxyBase + '/__proxy/' + host +
        (rest && rest.charAt(0) === '/' ? rest : '/' + rest);
    }
    rest = match[2] || '';
    return clockProxyBase + '/__proxy/' + match[1].toLowerCase() +
      (rest && rest.charAt(0) === '/' ? rest : '/' + rest);
  }

  function randomHex(bytes) {
    var out = '';
    var data = new Uint8Array(bytes);
    var cryptoObj = global.crypto || global.msCrypto;
    var i;
    if (cryptoObj && cryptoObj.getRandomValues) {
      cryptoObj.getRandomValues(data);
      for (i = 0; i < data.length; i += 1) out += ('0' + data[i].toString(16)).slice(-2);
      return out;
    }
    for (i = 0; i < bytes * 2; i += 1) out += Math.floor(Math.random() * 16).toString(16);
    return out;
  }

  function storageGet(key) {
    try { return global.localStorage.getItem(key); } catch (e) { return null; }
  }

  function storageSet(key, value) {
    try { global.localStorage.setItem(key, value); return true; } catch (e) { return false; }
  }

  function storageRemove(key) {
    try { global.localStorage.removeItem(key); } catch (e) {}
  }

  function loadDevice() {
    var saved = storageGet(DEVICE_KEY);
    if (saved && /^(?:[a-f0-9]{32,64}|tv[a-f0-9]{32})$/i.test(saved)) return saved;
    // The official clients use a 32 character random fingerprint per installation.
    var created = randomHex(16);
    storageSet(DEVICE_KEY, created);
    return created;
  }

  function rotateDevice() {
    device = randomHex(16);
    storageSet(DEVICE_KEY, device);
    return device;
  }

  function loadSession() {
    var raw = storageGet(SESSION_KEY);
    var parsed;
    if (!raw) return null;
    try { parsed = JSON.parse(raw); } catch (e) { return null; }
    if (!parsed || typeof parsed.token !== 'string' || !parsed.token) return null;
    return parsed;
  }

  function persist(next) {
    session = next;
    if (!next) { storageRemove(SESSION_KEY); return true; }
    return storageSet(SESSION_KEY, JSON.stringify(next));
  }

  function apiError(kind, http, message, payload) {
    return { kind: kind, http: http || 0, message: message || '', payload: payload || null };
  }

  function errorFromStatus(http, payload) {
    var message = '';
    if (payload) {
      if (typeof payload.message === 'string') message = payload.message;
      else if (payload.error && typeof payload.error === 'string') message = payload.error;
      else if (typeof payload.error === 'string') message = payload.error;
    }
    if (http === 0) return apiError('network', 0, message);
    if (http === 401) return apiError('auth', 401, message, payload);
    if (http === 402) return apiError('payment', 402, message, payload);
    if (http === 403) return apiError('blocked', 403, message, payload);
    if (http === 404) return apiError('missing', 404, message, payload);
    if (http >= 500) return apiError('server', http, message, payload);
    return apiError('request', http, message, payload);
  }

  function parseJson(text) {
    if (!text) return null;
    try { return JSON.parse(text); } catch (e) { return undefined; }
  }

  function hasOverrideHeader(headers, name) {
    var target = String(name).toLowerCase();
    var k;
    for (k in headers) {
      if (Object.prototype.hasOwnProperty.call(headers, k) &&
          String(k).toLowerCase() === target &&
          headers[k] !== null && headers[k] !== undefined) {
        return true;
      }
    }
    return false;
  }

  function rawRequest(options, callback) {
    var xhr = new XMLHttpRequest();
    var done = false;
    var headers = options.headers || {};
    var body = null;
    var key;

    function finish(err, data, http) {
      if (done) return;
      done = true;
      callback(err, data, http);
    }

    try {
      xhr.open(options.method || 'GET', proxyUrl(options.url), true);
      xhr.timeout = options.timeout || REQUEST_TIMEOUT;
      try { xhr.withCredentials = false; } catch (e) {}
      if (!hasOverrideHeader(headers, 'Accept')) xhr.setRequestHeader('Accept', 'application/json');
      if (!hasOverrideHeader(headers, 'Lang')) xhr.setRequestHeader('Lang', CLIENT.lang);
      if (!hasOverrideHeader(headers, 'X-Platform')) xhr.setRequestHeader('X-Platform', CLIENT.platform);
      if (!hasOverrideHeader(headers, 'X-VersionCode')) xhr.setRequestHeader('X-VersionCode', CLIENT.versionCode);
      if (!hasOverrideHeader(headers, 'X-App-Version')) xhr.setRequestHeader('X-App-Version', CLIENT.appVersion);
      if (!hasOverrideHeader(headers, 'app-version')) xhr.setRequestHeader('app-version', CLIENT.versionCode);
      if (device && !hasOverrideHeader(headers, 'fingerprint')) {
        xhr.setRequestHeader('fingerprint', device);
      }
      for (key in headers) {
        if (Object.prototype.hasOwnProperty.call(headers, key) && headers[key] !== null && headers[key] !== undefined) {
          xhr.setRequestHeader(key, String(headers[key]));
        }
      }
      if (options.form) {
        body = options.form;
        if (!hasOverrideHeader(headers, 'Content-Type')) {
          xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
        }
      } else if (options.body !== undefined && options.body !== null) {
        body = JSON.stringify(options.body);
        if (!hasOverrideHeader(headers, 'Content-Type')) {
          xhr.setRequestHeader('Content-Type', 'application/json; charset=utf-8');
        }
      }
      xhr.onreadystatechange = function () {
        var parsed;
        if (xhr.readyState !== 4) return;
        parsed = parseJson(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) {
          if (parsed === undefined) return finish(apiError('format', xhr.status, 'unreadable json'), null, xhr.status);
          return finish(null, parsed, xhr.status);
        }
        finish(errorFromStatus(xhr.status, parsed), parsed, xhr.status);
      };
      xhr.onerror = function () { finish(apiError('network', 0), null, 0); };
      xhr.ontimeout = function () { finish(apiError('network', 0), null, 0); };
      xhr.send(body);
    } catch (e) {
      finish(apiError('network', 0), null, 0);
    }
  }

  function withToken(options, token) {
    options.headers = options.headers || {};
    options.headers.Authorization = token;
    return options;
  }

  function callRefresh(callback) {
    var current = session;
    if (!current || !current.refresh_token) {
      persist(null);
      return callback(apiError('auth', 401), null);
    }
    function handleRefreshResult(err, data) {
      var next;
      if (!err && data && typeof data.token === 'string' && data.token && typeof data.refresh_token === 'string' && data.refresh_token) {
        next = { token: data.token, refresh_token: data.refresh_token, fingerprint: device, saved_at: Date.now() };
        persist(next);
        return callback(null, next);
      }
      if (err && (err.kind === 'network' || err.kind === 'server')) {
        return callback(err, null);
      }
      if (err && err.http === 401) persist(null);
      callback(apiError('auth', err ? err.http : 401, err ? err.message : ''), null);
    }
    rawRequest({
      method: 'POST',
      url: HOSTS.auth + '/api/v2/auth/refresh-mobile',
      timeout: 10000,
      body: { fingerprint: device, refresh_token: current.refresh_token }
    }, function (err, data) {
      if (err && (err.kind === 'network' || err.http === 404 || err.http === 405 || err.http >= 500)) {
        return rawRequest({
          method: 'POST',
          url: HOSTS.authAlt + '/api/v2/auth/refresh-mobile',
          timeout: 10000,
          body: { fingerprint: device, refresh_token: current.refresh_token }
        }, function (altErr, altData) {
          if (!altErr) return handleRefreshResult(null, altData);
          handleRefreshResult(altErr || err, altData);
        });
      }
      handleRefreshResult(err, data);
    });
  }

  function refreshSession(callback) {
    if (refreshing) { refreshQueue.push(callback); return; }
    refreshing = true;
    refreshQueue.push(callback);
    callRefresh(function (err, next) {
      var queue = refreshQueue;
      refreshing = false;
      refreshQueue = [];
      queue.forEach(function (fn) { fn(err, next); });
    });
  }

  /**
   * Authenticated request with one token refresh + retry on HTTP 401.
   * `options.fallback` may be a second URL used when the first answers 404/405.
   */
  function authorized(options, callback, send) {
    send = send || rawRequest;
    var started = generation;
    if (!session) return callback(apiError('auth', 401), null);
    withToken(options, session.token);
    send(options, function (err, data, http) {
      if (started !== generation) return;
      if (err && err.kind === 'auth') {
        refreshSession(function (refreshErr, next) {
          var retry;
          if (started !== generation) return;
          if (refreshErr || !next) return callback(refreshErr || apiError('auth', 401), null);
          retry = { method: options.method, url: options.url, body: options.body, form: options.form, headers: options.headers, timeout: options.timeout };
          retry.headers = retry.headers || {};
          retry.headers.Authorization = next.token;
          send(retry, function (retryErr, retryData, retryHttp) {
            if (retryErr && retryErr.kind === 'auth') persist(null);
            if (retryErr && options.fallback && (retryErr.http === 404 || retryErr.http === 405)) {
              retry.url = options.fallback;
              return send(retry, callback);
            }
            callback(retryErr, retryData, retryHttp);
          });
        });
        return;
      }
      if (err && options.fallback && (err.http === 404 || err.http === 405)) {
        options.url = options.fallback;
        options.fallback = null;
        return authorized(options, callback, send);
      }
      callback(err, data, http);
    });
  }

  function listFrom(data) {
    if (!data) return [];
    if (Array.isArray(data)) return data;
    if (Array.isArray(data.result)) return data.result;
    if (Array.isArray(data.data)) return data.data;
    if (Array.isArray(data.items)) return data.items;
    if (Array.isArray(data.movies)) return data.movies;
    if (data.result && typeof data.result === 'object') {
      if (Array.isArray(data.result.items)) return data.result.items;
      if (Array.isArray(data.result.movies)) return data.result.movies;
      if (Array.isArray(data.result.data)) return data.result.data;
      var nested = firstArray(data.result);
      if (nested) return nested;
    }
    var direct = firstArray(data);
    return direct || [];
  }

  /* Endpoints disagree on the collection key (`episodes`, `seasons`, `categories`, …). */
  function firstArray(object) {
    if (!object || typeof object !== 'object') return null;
    for (var key in object) {
      if (!Object.prototype.hasOwnProperty.call(object, key)) continue;
      if (Array.isArray(object[key])) return object[key];
    }
    return null;
  }

  function statusOk(data) {
    return !data || typeof data.status === 'undefined' || data.status === 'ok' || data.status === true;
  }

  /* ── Auth ───────────────────────────────────────────────────────────── */

  function otpListFrom(data) {
    if (!data || typeof data !== 'object') return [];
    if (Array.isArray(data.otp)) return data.otp;
    if (data.result && Array.isArray(data.result.otp)) return data.result.otp;
    if (Array.isArray(data.result)) return data.result;
    if (Array.isArray(data.data)) return data.data;
    return [];
  }

  function otpMethods(callback) {
    rawRequest({ method: 'GET', url: HOSTS.auth + '/api/v1/auth/available-otp', timeout: 8000 }, function (err, data) {
      if (err) {
        // The web host answers the same route; used only when the mobile host is unreachable.
        return rawRequest({ method: 'GET', url: HOSTS.authAlt + '/api/v1/auth/available-otp', timeout: 8000 }, function (altErr, altData) {
          if (altErr) return callback(err, null);
          callback(null, otpListFrom(altData));
        });
      }
      callback(null, otpListFrom(data));
    });
  }

  function signIn(phone, signInType, callback) {
    var path = '/api/v1/auth/sign-in?sign_in_type=' + encodeURIComponent(signInType);
    var body = { phone: Number(phone) };
    var headers = { 'X-Code-Sending-Type': String(signInType) };
    rawRequest({
      method: 'POST',
      url: HOSTS.auth + path,
      body: body,
      headers: headers,
      timeout: 12000
    }, function (err, data) {
      if (!err) {
        if (!data || typeof data.token !== 'string' || !data.token) return callback(apiError('format', 200), null);
        return callback(null, data);
      }
      if (err.kind === 'network' || err.http === 404 || err.http === 405 || err.http >= 500) {
        return rawRequest({
          method: 'POST',
          url: HOSTS.authAlt + path,
          body: body,
          headers: headers,
          timeout: 12000
        }, function (altErr, altData) {
          if (altErr) return callback(err, null);
          if (!altData || typeof altData.token !== 'string' || !altData.token) return callback(apiError('format', 200), null);
          callback(null, altData);
        });
      }
      callback(err, null);
    });
  }

  function checkCode(code, tempToken, callback) {
    var payload = { code: code, fingerprint: device, token: tempToken };
    function accept(data) {
      if (!data || typeof data.token !== 'string' || !data.token) return callback(apiError('format', 200), null);
      generation += 1;
      persist({ token: data.token, refresh_token: String(data.refresh_token || ''), fingerprint: device, saved_at: Date.now() });
      callback(null, data);
    }
    rawRequest({
      method: 'POST',
      url: HOSTS.auth + '/api/v1/auth/check-code-mobile',
      body: payload,
      timeout: 12000
    }, function (err, data) {
      if (!err) return accept(data);
      if (err.kind === 'network' || err.http === 404 || err.http === 405 || err.http >= 500) {
        return rawRequest({
          method: 'POST',
          url: HOSTS.authAlt + '/api/v1/auth/check-code-mobile',
          body: payload,
          timeout: 12000
        }, function (altErr, altData) {
          if (altErr) return callback(err, null);
          accept(altData);
        });
      }
      callback(err, null);
    });
  }

  function parseQrPayload(data) {
    var rawCode, match, token, png;
    if (!data || typeof data.code !== 'string' || typeof data.qr_png !== 'string') return null;
    rawCode = data.code.replace(/^\s+|\s+$/g, '');
    match = /^https?:\/\/[^\/]+\/register\/([A-Za-z0-9_-]+)\/?$/.exec(rawCode);
    if (match) token = match[1];
    else if (/^[A-Za-z0-9_-]{6,256}$/.test(rawCode)) token = rawCode;
    else return null;
    png = data.qr_png.replace(/\s+/g, '').replace(/^data:image\/png;base64,/i, '');
    if (!/^iVBORw0KGgo[A-Za-z0-9+/=]+$/.test(png) || png.length > 600000) return null;
    return {
      code: token,
      image: 'data:image/png;base64,' + png
    };
  }

  function qrCode(callback) {
    qrAuthHost = HOSTS.auth;
    rawRequest({ method: 'GET', url: HOSTS.auth + '/api/v1/auth/get-qrcode', timeout: 8000 }, function (err, data) {
      var parsed = !err && parseQrPayload(data);
      if (parsed) return callback(null, parsed);
      rawRequest({ method: 'GET', url: HOSTS.authAlt + '/api/v1/auth/get-qrcode', timeout: 8000 }, function (altErr, altData) {
        var altParsed = !altErr && parseQrPayload(altData);
        if (altParsed) {
          qrAuthHost = HOSTS.authAlt;
          return callback(null, altParsed);
        }
        callback(err || apiError('format', 200), null);
      });
    });
  }

  function qrCheck(code, web, callback, host) {
    function direct() {
      rawRequest({
        method: 'POST',
        url: (host || HOSTS.auth) + '/api/v1/auth/check-qrcode' + (web ? '-web' : ''),
        body: { code: code, fingerprint: device }
      }, callback);
    }
    if (!global.beletCheckQrNative || (host && host !== HOSTS.auth)) return direct();
    global.beletCheckQrNative(code, device, web, function (http, data) {
      if (!http) return direct();
      callback(http >= 200 && http < 300 ? null : errorFromStatus(http, null), data, http);
    });
  }

  function logout(callback) {
    var token = session && session.token;
    var oldDevice = device;
    var headers;
    generation += 1;
    playerSession = '';
    mediaScheme = '';
    qrAuthHost = HOSTS.auth;
    persist(null);
    // Rotate the install fingerprint on explicit sign-out so a revoked/logged-out device ID
    // cannot poison subsequent OTP or QR endpoints on the login screen.
    rotateDevice();
    if (!token) {
      if (callback) callback(null);
      return;
    }
    headers = { Authorization: token };
    if (oldDevice) headers.fingerprint = oldDevice;
    rawRequest({
      method: 'POST',
      url: HOSTS.auth + '/api/v1/auth/log-out',
      timeout: 4000,
      headers: headers
    }, function (err) {
      if (callback) callback(err || null);
    });
  }

  /* ── Catalogue ──────────────────────────────────────────────────────── */

  function home(offset, limit, callback) {
    // v2 is what belet.tm itself calls; v1 stays as a fallback for older deployments.
    var url = HOSTS.home + '/api/v2/home_page?type_id=0&h_limit=12&limit=' + encodeURIComponent(limit) +
      '&offset=' + encodeURIComponent(offset);
    authorized({ method: 'GET', url: url, fallback: url.replace('/api/v2/', '/api/v1/'), timeout: 10000 }, function (err, data) {
      if (err) return callback(err, null);
      callback(null, listFrom(data));
    });
  }

  function categories(callback) {
    authorized({ method: 'GET', url: HOSTS.home + '/api/v1/categories', timeout: 10000 }, function (err, data) {
      if (err) return callback(err, null);
      callback(null, data && Array.isArray(data.categories) ? data.categories : listFrom(data));
    });
  }

  // A category page on the site is `home_page?type_id=<category>`: the server answers with
  // rows of movies. The TV grid flattens them and pages by row offset.
  function category(id, limit, offset, callback) {
    var url = HOSTS.home + '/api/v2/home_page?type_id=' + encodeURIComponent(id) +
      '&h_limit=12&limit=' + encodeURIComponent(limit) + '&offset=' + encodeURIComponent(offset);
    authorized({ method: 'GET', url: url, fallback: url.replace('/api/v2/', '/api/v1/') }, function (err, data) {
      if (err) return callback(err, null);
      var groups = listFrom(data);
      var items = [];
      groups.forEach(function (group) {
        var list = group && Array.isArray(group.items) ? group.items : (Array.isArray(group) ? group : []);
        list.forEach(function (item) {
          if (item && typeof item === 'object' && typeof item.id !== 'undefined' && item.type_id) items.push(item);
        });
      });
      callback(null, { items: items, rows: groups.length });
    });
  }

  /**
   * v3 answers with the richest card (seasons, trailers), but it rejects some
   * catalogue ids, so any non-auth failure retries v2 before giving up.
   */
  function movie(id, callback) {
    var path = '/movie/' + encodeURIComponent(id);
    function attempt(version, next) {
      authorized({ method: 'GET', url: HOSTS.film + '/api/v' + version + path }, function (err, data) {
        if (err) return next(err);
        var film = data && (data.movie || data.film || data.result || data);
        if (!film || typeof film !== 'object' || typeof film.id === 'undefined') return next(apiError('format', 200));
        next(null, film);
      });
    }
    attempt(3, function (err, film) {
      if (!err) return callback(null, film);
      if (err.kind === 'auth') return callback(err, null);
      attempt(2, function (fallbackErr, fallbackFilm) {
        if (fallbackErr) return callback(err, null);
        callback(null, fallbackFilm);
      });
    });
  }

  function seasons(movieId, callback) {
    var id = encodeURIComponent(movieId);
    var urls = [
      HOSTS.film + '/api/v2/seasons?movie_id=' + id,
      HOSTS.film + '/api/v2/seasons?movieId=' + id,
      HOSTS.film + '/api/v1/seasons?movie_id=' + id,
      HOSTS.film + '/api/v1/seasons?movieId=' + id
    ];
    var lastError = null;
    function tryUrl(index) {
      if (index >= urls.length) return callback(lastError, []);
      authorized({ method: 'GET', url: urls[index] }, function (err, data) {
        var list = err ? [] : listFrom(data);
        if (list.length) return callback(null, list);
        if (err && err.kind === 'auth') return callback(err, null);
        if (err) lastError = err;
        tryUrl(index + 1);
      });
    }
    tryUrl(0);
  }

  /**
   * v2 returns a plain season list; v3 paginates around `current_position` and needs
   * `direction`, so an incomplete v3 query answers empty. Try v2 first, then v3.
   */
  function episodes(seasonId, options, callback) {
    options = options || {};
    var id = encodeURIComponent(seasonId);
    var limit = encodeURIComponent(options.limit || 100);
    var urls = [
      HOSTS.film + '/api/v2/episodes?season_id=' + id + '&limit=' + limit,
      HOSTS.film + '/api/v2/episodes?seasonId=' + id + '&limit=' + limit,
      HOSTS.film + '/api/v3/episodes?season_id=' + id + '&limit=' + limit +
        '&current_position=' + encodeURIComponent(options.position || 1) +
        '&direction=' + encodeURIComponent(options.direction || 'next')
    ];
    var query = '?season_id=' + id + '&limit=' + limit +
      '&current_position=' + encodeURIComponent(options.position || 1) +
      '&direction=' + encodeURIComponent(options.direction || 'next');
    if (options.episodeId) query += '&episode_id=' + encodeURIComponent(options.episodeId);
    urls.push(HOSTS.film + '/api/v3/episodes' + query);
    urls.push(HOSTS.film + '/api/v3/episodes?seasonId=' + id + '&limit=' + limit +
      '&current_position=' + encodeURIComponent(options.position || 1) +
      '&direction=' + encodeURIComponent(options.direction || 'next'));
    if (options.episodeId) {
      urls[urls.length - 1] += '&episodeId=' + encodeURIComponent(options.episodeId);
    }
    var lastError = null;
    function tryUrl(index) {
      if (index >= urls.length) return callback(lastError, []);
      authorized({ method: 'GET', url: urls[index] }, function (err, data) {
        var list = err ? [] : listFrom(data);
        if (list.length) return callback(null, list);
        if (err && err.kind === 'auth') return callback(err, null);
        if (err) lastError = err;
        tryUrl(index + 1);
      });
    }
    tryUrl(0);
  }

  var VIDEO_URL = /\.(m3u8|mpd|mp4|m4v|mov|webm|ts)(\?|#|$)/i;

  function hasMediaMeta(entry) {
    if (!entry || typeof entry !== 'object') return false;
    return !!(entry.quality || entry.quality_label || entry.resolution || entry.height || entry.type ||
      entry.format || entry.mime_type || entry.mimeType || entry.content_type || entry.contentType ||
      entry.protocol || entry.duration || entry.bitrate || entry.hls);
  }

  function normalizeSource(entry) {
    var url = '';
    if (!entry) return null;
    if (typeof entry === 'string') url = entry;
    else url = entry.filename || entry.url || entry.src || entry.link || entry.file ||
      entry.hls || entry.hls_url || entry.hlsUrl || entry.stream_url || entry.streamUrl ||
      entry.playback_url || entry.playbackUrl || entry.file_url || entry.fileUrl ||
      entry.download_url || entry.downloadUrl || '';
    if (url && typeof url === 'object') {
      url = url.url || url.src || url.filename || url.link || '';
    }
    if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return null;
    // The payload also carries posters and other images; only video entries may become sources.
    if (/\.(jpe?g|png|webp|gif|svg|ico)(\?|#|$)/i.test(url)) return null;
    if (!VIDEO_URL.test(url) && !hasMediaMeta(entry)) return null;
    return {
      url: url,
      filename: url,
      quality: String((entry && (entry.quality || entry.quality_label || entry.resolution || entry.height)) || ''),
      type: String((entry && (entry.type || entry.format || entry.mime_type || entry.mimeType ||
        entry.content_type || entry.contentType || entry.protocol)) || '')
    };
  }

  // The files endpoint changed shape between app versions, so walk the payload instead of
  // assuming one exact nesting: any object with an http(s) url field counts as a source.
  function sourcesFrom(data) {
    var out = [];
    var seen = {};
    function walk(node, depth) {
      var source;
      if (!node || depth > 5) return;
      if (Array.isArray(node)) {
        node.forEach(function (item) { walk(item, depth + 1); });
        return;
      }
      if (typeof node !== 'object') return;
      source = normalizeSource(node);
      if (source && !seen[source.url]) {
        seen[source.url] = true;
        out.push(source);
      }
      var keys = Object.keys(node);
      keys.forEach(function (key) {
        var value = node[key];
        if (value && typeof value === 'object') walk(value, depth + 1);
      });
    }
    walk(data, 0);
    return out;
  }

  function playerSessionFrom(data, depth) {
    var keys;
    var value;
    var i;
    if (!data || typeof data !== 'object' || depth > 5) return '';
    value = data.session_id || data.sessionId || data.player_session_id || data.playerSessionId;
    if (value !== null && typeof value !== 'undefined' && String(value)) return String(value);
    keys = Object.keys(data);
    for (i = 0; i < keys.length; i += 1) {
      if (data[keys[i]] && typeof data[keys[i]] === 'object') {
        value = playerSessionFrom(data[keys[i]], depth + 1);
        if (value) return value;
      }
    }
    return '';
  }

  function files(movieId, typeId, callback) {
    var base = HOSTS.film + '/api/v3/files/' + encodeURIComponent(movieId);
    var type = encodeURIComponent(typeId);
    var urls = [base + '?type=' + type, base + '?type_id=' + type, base];
    var lastError = null;
    var lastData = null;
    function tryUrl(index) {
      if (index >= urls.length) {
        return callback(lastError || apiError('empty', 200, '', lastData), null);
      }
      authorized({ method: 'GET', url: urls[index] }, function (err, data) {
        var list;
        if (err) {
          lastError = err;
          if (err.kind === 'auth') return callback(err, null);
          return tryUrl(index + 1);
        }
        lastData = data;
        list = sourcesFrom(data);
        if (list.length) return callback(null, list, { sessionId: playerSessionFrom(data, 0) });
        tryUrl(index + 1);
      });
    }
    tryUrl(0);
  }

  /* ── Continue watching, favourites, likes ───────────────────────────── */

  function continues(callback) {
    authorized({ method: 'GET', url: HOSTS.film + '/api/v1/continues' }, function (err, data) {
      if (err) return callback(err, null);
      callback(null, listFrom(data));
    });
  }

  function favorites(callback) {
    authorized({
      method: 'GET',
      url: HOSTS.film + '/api/v1/favorites',
      fallback: HOSTS.film + '/api/v2/favorites'
    }, function (err, data) {
      if (err) return callback(err, null);
      callback(null, listFrom(data));
    });
  }

  function setFavorite(movieId, add, callback) {
    authorized({
      method: add ? 'POST' : 'DELETE',
      url: HOSTS.film + '/api/v1/favorites?movieId=' + encodeURIComponent(movieId)
    }, function (err, data) { callback(err || null, data); });
  }

  function setLike(movieId, value, callback) {
    // value: 1 — like, 0 — dislike, -1 — clear
    var url = HOSTS.film + '/api/v1/likes?movieId=' + encodeURIComponent(movieId) +
      (value >= 0 ? '&status=' + value : '');
    authorized({ method: value >= 0 ? 'POST' : 'DELETE', url: url }, function (err, data) { callback(err || null, data); });
  }

  function removeContinue(movieId, callback) {
    authorized({
      method: 'DELETE',
      url: HOSTS.film + '/api/v1/continues?movieId=' + encodeURIComponent(movieId)
    }, function (err, data) { callback(err || null, data); });
  }

  /* ── Player progress ────────────────────────────────────────────────── */

  function lastWatchTime(movieId, seasonId, watchTime, playTime, callback) {
    var form = 'movie_id=' + encodeURIComponent(movieId) +
      '&watch_time=' + encodeURIComponent(Math.max(0, Math.round(watchTime))) +
      '&play_time=' + encodeURIComponent(Math.max(0, Math.round(playTime)));
    if (seasonId) form += '&season_id=' + encodeURIComponent(seasonId);
    authorized({
      method: 'POST',
      url: HOSTS.film + '/api/v3/lastWatchTime',
      form: form,
      headers: { 'X-Player-Session-Id': playerSession || randomHex(8) }
    }, function (err, data) { if (callback) callback(err || null, data); });
  }

  /* ── Search ─────────────────────────────────────────────────────────── */

  function search(text, page, callback) {
    page = page || 1;
    var encoded = 'text=' + encodeURIComponent(text) + '&page=' + encodeURIComponent(page);
    // The Android client posts a hash map (text/page/type); the web client posts a form.
    authorized({
      method: 'POST',
      url: HOSTS.search + '/api/v2/search',
      form: encoded
    }, function (err, data) {
      if (!err) return callback(null, listFrom(data));
      if (err.kind === 'auth') return callback(err, null);
      authorized({
        method: 'POST',
        url: HOSTS.searchWeb + '/api/v2/search',
        form: encoded
      }, function (webErr, webData) {
        if (!webErr) return callback(null, listFrom(webData));
        callback(err, null);
      });
    });
  }

  /* ── Profile ────────────────────────────────────────────────────────── */

  function profile(callback) {
    authorized({ method: 'GET', url: HOSTS.auth + '/api/v2/users/profile', timeout: 10000 }, function (err, data) {
      if (!err) return callback(null, data || null);
      if (err.kind === 'auth') return callback(err, null);
      authorized({ method: 'GET', url: HOSTS.authAlt + '/api/v2/users/profile', timeout: 10000 }, function (altErr, altData) {
        if (!altErr) return callback(null, altData || null);
        callback(err, null);
      });
    });
  }

  function tizenVersion() {
    var match = String(window.navigator.userAgent || '').match(/Tizen[\s\/]*([\d.]+)/i);
    if (match) return match[1];
    try {
      if (window.tizen && window.tizen.systeminfo) {
        return String(window.tizen.systeminfo.getCapability('http://tizen.org/feature/platform.version') || 'Tizen');
      }
    } catch (e) {}
    return 'Tizen';
  }

  function model() {
    var name = '';
    try {
      if (window.webapis && window.webapis.productinfo && window.webapis.productinfo.getModel) {
        name = String(window.webapis.productinfo.getModel() || '');
      }
    } catch (e) {}
    if (!name) {
      try {
        var device = window.tizen && window.tizen.systeminfo && window.tizen.systeminfo.getCapability('http://tizen.org/system/model_name');
        if (device) name = String(device);
      } catch (e) {}
    }
    if (!name) {
      try { name = String(window.navigator.userAgent.match(/Tizen[^)]*/) || ''); } catch (e) {}
    }
    return name || 'Samsung Tizen TV';
  }

  // Keep the ordinary authenticated retry path, including the refreshed token,
  // but let webOS make this fixed POST outside the browser's CORS policy.
  function sendDeviceRegistration(options, callback) {
    if (!global.beletRegisterDeviceNative) return rawRequest(options, callback);
    global.beletRegisterDeviceNative(options.body, options.headers.Authorization, function (http) {
      if (!http) return rawRequest(options, callback);
      callback(http >= 200 && http < 300 ? null : errorFromStatus(http, null), null, http);
    });
  }

  /* DeviceBody on the Android client is app_version + fingerprint + model + os_version. */
  function registerDevice(callback) {
    var name = model();
    var lg = global.beletWebosIdentity && global.beletWebosIdentity();
    var body = {
      app_version: CLIENT.appVersion,
      fingerprint: device,
      model: lg ? lg.model : 'Samsung Tizen ' + name,
      os_version: lg ? lg.osVersion : tizenVersion()
    };
    authorized({
      method: 'POST',
      url: HOSTS.auth + '/api/v1/users/device',
      timeout: 10000,
      body: body,
      headers: { 'X-Device-Model': name }
    }, function (err, data) {
      if (!err || err.kind === 'auth' || global.beletRegisterDeviceNative) {
        if (callback) callback(err || null, data || null);
        return;
      }
      authorized({
        method: 'POST',
        url: HOSTS.authAlt + '/api/v1/users/device',
        timeout: 10000,
        body: body,
        headers: { 'X-Device-Model': name }
      }, function (altErr, altData) {
        if (!callback) return;
        if (!altErr) return callback(null, altData || null);
        callback(err || altErr, null);
      }, sendDeviceRegistration);
    }, sendDeviceRegistration);
  }

  var playerSession = '';
  /* The Android ExoPlayer sends the token as-is; some CDN edges accept Bearer as fallback. */
  var mediaScheme = '';

  function mediaHeaders(sessionId, isFragment, chunkLength) {
    var headers = {};
    headers['X-Platform'] = CLIENT.platform;
    headers['X-VersionCode'] = CLIENT.versionCode;
    headers['X-Player-Session-Id'] = String(sessionId || playerSession || '');
    headers.fingerprint = device || '';
    if (session && session.token) headers.Authorization = mediaScheme + session.token;
    if (isFragment) headers['X-Chunk-Length'] = String(chunkLength || '-1');
    return headers;
  }

  function nextMediaScheme() {
    if (mediaScheme) return false;
    mediaScheme = 'Bearer ';
    return true;
  }

  function newPlayerSession(id) {
    var next = String(id || randomHex(8));
    if (next !== playerSession) mediaScheme = '';
    playerSession = next;
    return playerSession;
  }

  function deviceInfo() {
    return {
      fingerprint: device,
      model: model(),
      appVersion: CLIENT.appVersion,
      versionCode: CLIENT.versionCode,
      hosts: { auth: HOSTS.auth, home: HOSTS.home, film: HOSTS.film, search: HOSTS.search }
    };
  }

  function healthCheck(callback) {
    var results = {};
    var pending = 3;
    function done(key) {
      return function (err) {
        results[key] = err ? (err.kind + (err.http ? ' ' + err.http : '')) : 'ok';
        pending -= 1;
        if (pending === 0) callback(results);
      };
    }
    rawRequest({ method: 'GET', url: HOSTS.auth + '/api/v1/auth/available-otp', timeout: 12000 }, done('auth'));
    rawRequest({ method: 'GET', url: HOSTS.home + '/api/v1/categories', timeout: 12000 }, done('home'));
    rawRequest({ method: 'GET', url: HOSTS.film + '/api/v3/movie/1', timeout: 12000 }, function (err, data, http) {
      // 401 is a healthy answer here: it proves the film API is reachable.
      results.film = (!err || err.http === 401) ? 'ok' : err.kind + (err.http ? ' ' + err.http : '');
      pending -= 1;
      if (pending === 0) callback(results);
    });
  }

  global.Belet = {
    HOSTS: HOSTS,
    CLIENT: CLIENT,
    init: function () {
      device = loadDevice();
      session = loadSession();
      if (session && session.fingerprint && session.fingerprint !== device) {
        // Older builds sometimes regenerated DEVICE_KEY before restoring the session.
        device = session.fingerprint;
        storageSet(DEVICE_KEY, device);
      }
      return session;
    },
    hasSession: function () { return !!(session && session.token); },
    session: function () { return session; },
    device: function () { return device; },
    resetDevice: rotateDevice,
    forget: function () { generation += 1; persist(null); },
    otpMethods: otpMethods,
    signIn: signIn,
    checkCode: checkCode,
    qrCode: qrCode,
    qrCheck: function (code, callback) {
      function result(err, data, http) {
        if ((err && http === 400) || (!err && !(data && typeof data.token === 'string' && data.token))) {
          return callback(apiError('pending', http), null, http);
        }
        callback(err, data, http);
      }
      function checkOnHost(host, onDone) {
        qrCheck(code, false, function (err, data, http) {
          if (http === 404 || http === 405) return qrCheck(code, true, onDone, host);
          onDone(err, data, http);
        }, host);
      }
      checkOnHost(HOSTS.auth, function (err, data, http) {
        if (qrAuthHost !== HOSTS.auth && err && (err.kind === 'network' || http === 404 || http === 405 || http >= 500)) {
          return checkOnHost(qrAuthHost, result);
        }
        result(err, data, http);
      });
    },
    acceptQrSession: function (data) {
      generation += 1;
      persist({ token: data.token, refresh_token: String(data.refresh_token || ''), fingerprint: device, saved_at: Date.now() });
    },
    logout: logout,
    home: home,
    categories: categories,
    category: category,
    movie: movie,
    seasons: seasons,
    episodes: episodes,
    files: files,
    continues: continues,
    favorites: favorites,
    setFavorite: setFavorite,
    setLike: setLike,
    removeContinue: removeContinue,
    lastWatchTime: lastWatchTime,
    search: search,
    profile: profile,
    deviceInfo: deviceInfo,
    model: model,
    tizenVersion: tizenVersion,
    registerDevice: registerDevice,
    refresh: refreshSession,
    mediaHeaders: mediaHeaders,
    nextMediaScheme: nextMediaScheme,
    newPlayerSession: newPlayerSession,
    healthCheck: healthCheck,
    setClockProxy: setClockProxy,
    clockProxy: function () { return clockProxyBase; },
    proxyUrl: proxyUrl,
    listFrom: listFrom,
    sourcesFrom: sourcesFrom,
    statusOk: statusOk,
    randomHex: randomHex
  };
}(this));
