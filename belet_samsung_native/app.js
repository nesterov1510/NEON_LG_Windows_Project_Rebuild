/* Samsung client prototype. Based on the observed Android request DTOs, not official Belet code. */
(function () {
  'use strict';
  var AUTH = 'https://beletapis.com';
  var HOME = 'https://homepage.belet.me';
  var FILM = 'https://film.beletapis.com';
  var STORAGE = 'neon_belet_samsung_v1';
  var $ = function (id) { return document.getElementById(id); };
  var session = null, tempToken = null, busy = false, refreshWait = [];
  function message(text) { $('status').textContent = text || ''; }
  function show(id) {
    ['login', 'home', 'player'].forEach(function (x) { $(x).classList.toggle('hidden', x !== id); });
    $('logout').classList.toggle('hidden', !session);
    setTimeout(function () { var el = $(id).querySelector('button:not([disabled]), input:not([disabled]), select:not([disabled])'); if (el) el.focus(); }, 30);
  }
  function loadSession() {
    try { var s = JSON.parse(localStorage.getItem(STORAGE) || 'null'); if (s && s.token && s.fingerprint) return s; } catch (e) {}
    return null;
  }
  function saveSession(s) {
    try { localStorage.setItem(STORAGE, JSON.stringify(s)); $('sessionNote').textContent = 'Сессия хранится в данных приложения на телевизоре.'; return true; } catch (e) { $('sessionNote').textContent = 'Сессия не сохранена: вход будет потерян после закрытия.'; return false; }
  }
  function forgetSession() { session = null; tempToken = null; $('sessionNote').textContent = ''; try { localStorage.removeItem(STORAGE); } catch (e) {} }
  function fingerprint() {
    var bytes = new Uint8Array(16), c = window.crypto || window.msCrypto, result = '', i;
    if (!c || !c.getRandomValues) return null;
    c.getRandomValues(bytes);
    for (i = 0; i < bytes.length; i++) result += ('0' + bytes[i].toString(16)).slice(-2);
    return result;
  }
  var device = null;
  try { device = localStorage.getItem(STORAGE + '_device'); } catch (e) {}
  if (!device) { device = fingerprint(); try { if (device) localStorage.setItem(STORAGE + '_device', device); } catch (e) {} }
  function request(method, url, body, token, cb) {
    var x = new XMLHttpRequest();
    try {
      x.open(method, url, true); x.timeout = 20000;
      x.setRequestHeader('Accept', 'application/json');
      x.setRequestHeader('lang', 'ru');
      if (device) x.setRequestHeader('fingerprint', device);
      if (token) x.setRequestHeader('Authorization', token);
      if (body !== null) x.setRequestHeader('Content-Type', 'application/json');
      x.onreadystatechange = function () {
        if (x.readyState !== 4) return;
        var parsed = null;
        try { if (x.responseText) parsed = JSON.parse(x.responseText); } catch (e) {}
        cb(x.status, parsed);
      };
      x.send(body === null ? null : JSON.stringify(body));
    } catch (e) { cb(0, null); }
  }
  function fail(status) { if (status === 402) { message('Belet вернул HTTP 402. Возможно, требуется активная подписка или оплата; проверьте условия в аккаунте.'); return; } message(status >= 200 && status < 300 ? 'Неожиданный формат ответа Belet.' : status ? 'Сервис Belet ответил HTTP ' + status + '. Проверьте данные и повторите.' : 'Нет ответа Belet. Проверьте сеть и совместимость телевизора.'); }
  function errorFor(status) { if (status === 401 && !session) { message('Сессия завершена. Войдите снова.'); show('login'); loadOtp(true); } else fail(status); }
  function refresh(cb) {
    if (!session || !session.refresh_token) { forgetSession(); return cb(false, 401); }
    if (refreshWait.length) { refreshWait.push(cb); return; }
    refreshWait.push(cb);
    var refreshToken = session.refresh_token;
    request('POST', AUTH + '/api/v2/auth/refresh-mobile', {fingerprint: device, refresh_token: refreshToken}, null, function (status, obj) {
      var ok = !!session && session.refresh_token === refreshToken && status >= 200 && status < 300 && obj && typeof obj.token === 'string' && typeof obj.refresh_token === 'string';
      if (ok) { session.token = obj.token; session.refresh_token = obj.refresh_token; if (!saveSession(session)) message('Вход сохранён только до закрытия приложения.'); }
      else if (status === 401) forgetSession();
      var wait = refreshWait; refreshWait = []; wait.forEach(function (fn) { fn(ok, status); });
    });
  }
  function protectedGet(url, cb, retried) {
    if (!session) return cb(401, null);
    var currentSession = session;
    request('GET', url, null, session.token, function (status, obj) {
      if (session !== currentSession) return;
      if (status === 401 && !retried) return refresh(function (ok, code) { if (ok) protectedGet(url, cb, true); else cb(code, null); });
      cb(status, obj);
    });
  }
  function loadOtp(preserveMessage) {
    request('GET', AUTH + '/api/v1/auth/available-otp', null, null, function (status, obj) {
      var list = obj && obj.otp; var select = $('otp'); select.textContent = '';
      if (!(status >= 200 && status < 300) || !Array.isArray(list) || !list.length) { fail(status); $('send').disabled = true; return; }
      list.forEach(function (item) { if (item && typeof item.type === 'number') { var option = document.createElement('option'); option.value = String(item.type); option.textContent = item.name || ('Способ ' + item.type); select.appendChild(option); } });
      $('send').disabled = !select.options.length; if (!preserveMessage) message('');
    });
  }
  $('send').onclick = function () {
    if (busy) return;
    var phone = $('phone').value.replace(/[^0-9]/g, '');
    if (!/^\d{8,15}$/.test(phone) || !device || !$('otp').value) { message('Укажите номер с кодом страны и выберите способ получения кода.'); return; }
    tempToken = null; $('codeSection').classList.add('hidden'); busy = true; message('Запрашиваем код…');
    request('POST', AUTH + '/api/v1/auth/sign-in?sign_in_type=' + encodeURIComponent($('otp').value), {phone: Number(phone)}, null, function (status, obj) {
      busy = false;
      if (status >= 200 && status < 300 && obj && typeof obj.token === 'string' && obj.token) { tempToken = obj.token; $('codeSection').classList.remove('hidden'); $('code').focus(); message('Введите полученный код.'); }
      else fail(status);
    });
  };
  $('verify').onclick = function () {
    if (busy || !tempToken) return;
    var code = $('code').value.trim();
    if (!/^\d{3,10}$/.test(code)) { message('Введите код подтверждения.'); return; }
    busy = true; message('Проверяем код…');
    request('POST', AUTH + '/api/v1/auth/check-code-mobile', {code: code, fingerprint: device, token: tempToken}, null, function (status, obj) {
      busy = false; $('code').value = '';
      if (!(status >= 200 && status < 300 && obj && typeof obj.token === 'string' && obj.token && typeof obj.refresh_token === 'string')) { fail(status); return; }
      session = {token: obj.token, refresh_token: obj.refresh_token, fingerprint: device}; tempToken = null;
      if (!saveSession(session)) message('Вход сохранён только до закрытия приложения.');
      $('phone').value = ''; $('codeSection').classList.add('hidden'); loadHome();
    });
  };
  // Android ImagesDTO has vertical / horizontal image variants with named sizes.
  function posterUrl(movie) {
    var images = movie.images || {}, types = ['vertical', 'vertical_without_name', 'horizontal_with_name', 'horizontal_without_name'];
    var sizes = ['medium', 'standard', 'default', 'high'], candidates = [], i, j, entry;
    for (i = 0; i < types.length; i++) {
      entry = images[types[i]];
      if (!entry || typeof entry !== 'object') continue;
      for (j = 0; j < sizes.length; j++) {
        if (entry[sizes[j]] && typeof entry[sizes[j]].url === 'string') candidates.push(entry[sizes[j]].url);
      }
    }
    if (typeof movie.image_url === 'string') candidates.push(movie.image_url);
    for (i = 0; i < candidates.length; i++) if (/^https:\/\//i.test(candidates[i])) return candidates[i];
    return null;
  }
  function appendRows(result) {
    var rows = $('rows'); rows.textContent = '';
    result.forEach(function (group) {
      var movies = group && group.movies;
      if (!Array.isArray(movies) || !movies.length) return;
      var heading = document.createElement('h2'); heading.textContent = group.title_ru || group.title || group.title_tk || 'Фильмы'; rows.appendChild(heading);
      var row = document.createElement('div'); row.className = 'row'; rows.appendChild(row);
      movies.forEach(function (movie) {
        if (!movie || !Number.isInteger(movie.id) || !Number.isInteger(movie.type_id)) return;
        var btn = document.createElement('button'), label = document.createElement('span'); btn.className = 'movie';
        var poster = document.createElement('div'), url = posterUrl(movie); poster.className = 'poster';
        if (url) { var image = document.createElement('img'); image.alt = ''; image.onload = function () { poster.className = 'poster loaded'; }; image.onerror = function () { image.style.display = 'none'; }; image.src = url; poster.appendChild(image); }
        btn.appendChild(poster);
        label.textContent = movie.title || movie.name || 'Фильм'; btn.appendChild(label);
        btn.onclick = function () { playMovie(movie); }; row.appendChild(btn);
      });
    });
  }
  function loadHome() {
    show('home'); message('Загружаем каталог…');
    protectedGet(HOME + '/api/v1/home_page?type_id=1&limit=12&offset=0&h_limit=12', function (status, obj) {
      if (!(status >= 200 && status < 300 && obj && Array.isArray(obj.result))) { errorFor(status); return; }
      appendRows(obj.result); message($('rows').children.length ? '' : 'Каталог пуст или содержит неподдерживаемые разделы.');
      var first = $('rows').querySelector('button'); if (first) first.focus();
    });
  }
  function playMovie(movie) {
    message('Получаем видео…');
    protectedGet(FILM + '/api/v3/files/' + encodeURIComponent(movie.id) + '?type=' + encodeURIComponent(movie.type_id), function (status, obj) {
      if (!(status >= 200 && status < 300 && obj && Array.isArray(obj.sources))) { errorFor(status); return; }
      var source = null;
      obj.sources.some(function (s) { if (s && typeof s.filename === 'string' && /^https:\/\//i.test(s.filename)) { source = s.filename; return true; } return false; });
      if (!source) { message('Сервис не предоставил поддерживаемый HTTPS-источник видео.'); return; }
      $('playerTitle').textContent = movie.title || movie.name || 'Belet';
      var video = $('video'); video.src = source; show('player'); message('');
      var promise = video.play(); if (promise && promise.catch) promise.catch(function () { message('Не удалось начать воспроизведение.'); });
    });
  }
  $('video').onerror = function () { message('Телевизор не смог воспроизвести этот формат или поток.'); };
  $('back').onclick = function () { var video = $('video'); video.pause(); video.removeAttribute('src'); video.load(); show('home'); message(''); };
  $('logout').onclick = function () {
    if (!session || !window.confirm('Выйти из Belet на этом устройстве?')) return;
    var token = session.token; forgetSession(); $('rows').textContent = ''; $('video').pause(); $('video').removeAttribute('src'); $('phone').value = ''; $('code').value = '';
    request('POST', AUTH + '/api/v1/auth/log-out', null, token, function (status) { message(status >= 200 && status < 300 ? 'Выход выполнен.' : 'Локальная сессия удалена; выход на сервере не подтверждён.'); });
    show('login'); loadOtp(true);
  };
  document.addEventListener('keydown', function (e) {
    var key = e.keyCode, current = document.activeElement;
    if (key === 10009 || key === 27 || key === 461) {
      if (!$('player').classList.contains('hidden')) { e.preventDefault(); $('back').click(); }
      return;
    }
    if (current && current.tagName === 'VIDEO') return;
    if (current && current.tagName === 'INPUT' && (key === 37 || key === 39)) return;
    if (current && current.tagName === 'SELECT') {
      if (current === $('otp') && key === 39) { e.preventDefault(); $('send').focus(); }
      else if (current === $('otp') && key === 37) { e.preventDefault(); $('phone').focus(); }
      return;
    }
    if (key !== 37 && key !== 38 && key !== 39 && key !== 40) return;
    var scope = !$('home').classList.contains('hidden') ? $('home') : !$('login').classList.contains('hidden') ? $('login') : $('player');
    var list = Array.prototype.slice.call(scope.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled])')).filter(function (el) { return !!(el.offsetWidth || el.offsetHeight); });
    if (session && $('logout').offsetWidth) list.push($('logout'));
    var rect = current && current.getBoundingClientRect(), best = null, score = Infinity;
    if (!rect) return;
    list.forEach(function (el) { if (el === current) return; var r = el.getBoundingClientRect(); var dx = r.left + r.width / 2 - rect.left - rect.width / 2, dy = r.top + r.height / 2 - rect.top - rect.height / 2;
      if ((key === 37 && dx >= -1) || (key === 39 && dx <= 1) || (key === 38 && dy >= -1) || (key === 40 && dy <= 1)) return;
      var primary = (key === 37 || key === 39) ? Math.abs(dx) : Math.abs(dy), secondary = (key === 37 || key === 39) ? Math.abs(dy) : Math.abs(dx), n = primary + 3 * secondary;
      if (n < score) { score = n; best = el; }
    });
    if (best) { e.preventDefault(); best.focus(); if (best.scrollIntoView) { try { best.scrollIntoView({block:'nearest', inline:'nearest'}); } catch (e2) { best.scrollIntoView(false); } } }
  });
  session = loadSession(); if (session) { $('sessionNote').textContent = 'Сессия хранится в данных приложения на телевизоре.'; device = session.fingerprint; loadHome(); } else { show('login'); if (!device) message('На телевизоре недоступно безопасное создание идентификатора.'); else loadOtp(); }
})();
