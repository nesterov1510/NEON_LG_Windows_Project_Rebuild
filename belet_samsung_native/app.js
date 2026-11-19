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
    homeLoaded: false,
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
    login: { phone: '', code: '', tempToken: '', methods: [], methodIndex: 0, busy: false },
    qr: { generation: 0, timer: null, code: null },
    toastTimer: null,
    backArmed: false,
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
  var KB_ROWS = ['йцукенгшщзхъ', 'фывапролджэ', 'ячсмитьбю', '1234567890', '- '];

  function inputOverlay(kind, options) {
    var isNum = kind === 'num';
    var host = isNum ? $('numpad') : $('keyboard');
    var keysHost = isNum ? $('numpadKeys') : $('keyboardKeys');
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
      NUM_KEYS.forEach(function (key) {
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

  function describeError(err) {
    if (!err) return 'Неизвестная ошибка.';
    if (err.kind === 'network') {
      return 'Нет ответа от серверов Belet (HTTP 0). Проверьте Wi-Fi и время на телевизоре. ' +
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

  function handleAuthError(err) {
    if (!err || err.kind !== 'auth') return false;
    state.homeLoaded = false;
    showLogin();
    toast('Сессия истекла — войдите снова', 4200);
    return true;
  }

  /* ── login ────────────────────────────────────────────────────────── */

  function showLogin() {
    stopQr();
    cancelPlayback(true);
    screen('login');
    state.mainFocus = $('phoneDisplay');
    $('loginPhone').classList.remove('hidden');
    $('loginCode').classList.add('hidden');
    $('loginQr').classList.add('hidden');
    $('loginTitle').textContent = 'Вход в Belet';
    $('loginHint').textContent = 'Введите номер телефона, привязанный к аккаунту Belet.';
    updatePhoneDisplay();
    loadOtpMethods();
    setTimeout(function () {
      var target = state.login.phone ? $('phoneDisplay') : $('phoneDisplay');
      if (target) target.focus();
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
    $('sendCode').disabled = !(value.length >= 8 && state.login.methods.length);
  }

  function loadOtpMethods() {
    Belet.otpMethods(function (err, methods) {
      var select = $('methodValue');
      state.login.methods = [];
      if (err) {
        select.textContent = 'Способ недоступен';
        $('sendCode').disabled = true;
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

  function startQr() {
    stopQr();
    var generation = state.qr.generation;
    var started = Date.now();
    $('loginPhone').classList.add('hidden');
    $('loginCode').classList.add('hidden');
    $('loginQr').classList.remove('hidden');
    $('loginTitle').textContent = 'Вход по QR-коду';
    $('loginHint').textContent = 'Отсканируйте код телефоном — телевизор войдёт автоматически.';
    $('qrImage').removeAttribute('src');
    $('qrStatus').textContent = 'Получаем QR-код…';
    state.mainFocus = $('qrRefresh');
    setTimeout(function () { $('loginQr').querySelector('.focusable').focus(); }, 30);

    Belet.qrCode(function (err, data) {
      var code;
      if (generation !== state.qr.generation) return;
      if (err || !data) {
        $('qrStatus').textContent = 'Не удалось получить QR-код. Проверьте сеть и обновите.';
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
            stopQr();
            $('qrStatus').textContent = 'Вход выполнен.';
            loadHome(true);
            return;
          }
          if (checkErr && (checkErr.kind === 'pending' || checkErr.http === 400)) {
            state.qr.timer = setTimeout(poll, 3000);
            return;
          }
          $('qrStatus').textContent = 'Не удалось проверить QR-код. Обновите его или войдите по SMS.';
        });
      }
      state.qr.timer = setTimeout(poll, 2500);
    });
  }

  /* ── catalogue rendering ──────────────────────────────────────────── */

  function pickImage(images, kind, size) {
    var entry, sizes = size || ['medium', 'standard', 'default', 'high'];
    var order = kind === 'horizontal'
      ? ['horizontal_with_name', 'horizontal_without_name', 'vertical', 'vertical_without_name']
      : ['vertical', 'vertical_without_name', 'horizontal_without_name', 'horizontal_with_name'];
    var i, j;
    if (!images || typeof images !== 'object') return null;
    for (i = 0; i < order.length; i += 1) {
      entry = images[order[i]];
      if (!entry || typeof entry !== 'object') continue;
      for (j = 0; j < sizes.length; j += 1) {
        if (entry[sizes[j]] && typeof entry[sizes[j]].url === 'string') return entry[sizes[j]].url;
      }
    }
    if (images.name && typeof images.name.url === 'string') return images.name.url;
    return null;
  }

  function posterOf(item) {
    return pickImage(item && item.images, 'vertical', ['medium', 'standard', 'default', 'high']) ||
      (item && typeof item.image_url === 'string' ? item.image_url : null) ||
      (item && typeof item.image === 'string' ? item.image : null);
  }

  function backdropOf(item) {
    return pickImage(item && item.images, 'horizontal', ['high', 'default', 'medium', 'standard']) ||
      (item && typeof item.image_url === 'string' ? item.image_url : null);
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
    if (image) {
      var img = doc.createElement('img');
      img.alt = '';
      img.onerror = function () { img.style.visibility = 'hidden'; };
      img.src = image;
      poster.appendChild(img);
    }
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
    if (image) {
      $('heroImage').src = image;
      $('heroImage').alt = '';
    }
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

  function loadHome(showSpinner) {
    var collected = [];
    screen('home');
    setHidden($('hero'), true);
    setHidden($('homeEmpty'), true);
    clear($('rows'));
    state.mainFocus = null;
    if (showSpinner) toast('Загружаем каталог…', 1800);

    function fetchPage(offset, pagesLeft) {
      Belet.home(offset, 3, function (err, groups) {
        if (err) {
          if (handleAuthError(err)) return;
          if (!collected.length) {
            $('homeEmpty').classList.remove('hidden');
            $('homeEmpty').querySelector('h2').textContent = 'Каталог не загрузился';
            $('homeEmpty').querySelector('p').textContent = describeError(err);
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
      state.home.groups = collected;
      state.homeLoaded = true;
      $('homeEmpty').querySelector('h2').textContent = 'Каталог пуст';
      $('homeEmpty').querySelector('p').textContent = 'Сервис ответил, но не вернул ни одного раздела. Проверьте подписку и повторите.';
      renderRows(collected);
      var first = $('rows').querySelector('.card') || $('heroPlay');
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

    if (poster) $('detailPoster').src = poster;
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
    if (seasons.length || movie.last_episode_info || movie.type_id === 2) {
      loadSeasons(movie, seasons);
    }
    state.mainFocus = $('detailPlay');
  }

  function loadSeasons(movie, seasons) {
    $('episodesSection').classList.remove('hidden');
    if (seasons.length) {
      renderSeasonChips(movie, seasons);
      var preferred = movie.last_episode_info && movie.last_episode_info.season_id;
      selectSeason(preferred || seasons[0].id);
      return;
    }
    $('episodeList').innerHTML = '<p class="search-state">Загружаем сезоны…</p>';
    Belet.seasons(movie.id, function (err, list) {
      var fetched = (list || []).filter(function (season) { return season && season.id; });
      if (err || !fetched.length) {
        $('episodeList').innerHTML = '<p class="search-state">' +
          (err ? describeError(err) : 'Список сезонов пуст.') + '</p>';
        return;
      }
      state.detail.seasons = fetched;
      renderSeasonChips(movie, fetched);
      var preferred = movie.last_episode_info && movie.last_episode_info.season_id;
      selectSeason(preferred || fetched[0].id);
    });
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
    if (!seasonId) return;
    detail.seasonId = seasonId;
    markSeasonChips();
    $('episodeList').innerHTML = '<p class="search-state">Загружаем серии…</p>';
    Belet.episodes(seasonId, { limit: 100 }, function (err, list) {
      if (err) {
        if (handleAuthError(err)) return;
        $('episodeList').innerHTML = '<p class="search-state">' + describeError(err) + '</p>';
        return;
      }
      detail.episodes = (list || []).map(function (episode, index) {
        return {
          raw: episode,
          id: episode.id || episode.episode_id,
          name: episode.name || episode.title || ('Серия ' + (index + 1)),
          position: episode.episode_position || episode.position || (index + 1),
          duration: episode.duration || ''
        };
      });
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
    var typeId = 4;
    var resumeAt = 0;
    var last = movie && movie.last_episode_info;
    if (last && Number(last.episode_id) === Number(episode.id)) resumeAt = Number(last.episode_watch_time || 0);
    if (episode.raw && Number(episode.raw.watch_time || 0) > resumeAt) resumeAt = Number(episode.raw.watch_time);
    if (episode.raw && Array.isArray(episode.raw.sources) && episode.raw.sources.length) {
      playSources(Belet.sourcesFrom(episode.raw), {
        title: movieTitle(movie),
        subtitle: episode.name,
        movieId: movie.id,
        seasonId: detail.seasonId,
        episodeId: episode.id,
        startAt: resumeAt,
        trackProgress: true,
        typeId: typeId
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
    if (!movie) return;
    if (movie.type_id === 2 && !(movie.last_episode_info && movie.last_episode_info.episode_id) &&
        state.detail.episodes.length) {
      playEpisode(state.detail.episodes[0]);
      return;
    }
    if (movie.type_id === 2 && movie.last_episode_info && movie.last_episode_info.episode_id) {
      var info = movie.last_episode_info;
      var episode = {
        id: info.episode_id,
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
    Belet.files(id, typeId, function (err, sources) {
      if (err) {
        if (handleAuthError(err)) return;
        playerError(describeError(err));
        return;
      }
      startSources(sources, options);
    });
  }

  function playSources(sources, options) {
    options = options || {};
    openPlayer(options);
    startSources(sources || [], options);
  }

  /* ── player ───────────────────────────────────────────────────────── */

  /* Samsung TVs play HLS best through the native AVPlay plugin; <video> is kept as a
     fallback for emulators and models without webapis.avplay. */
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

  function openPlayer(options) {
    var player = state.player;
    var video = $('video');
    cancelPlayback(true);
    screen('player');
    player.active = true;
    player.movie = null;
    player.mode = avplayAvailable() ? 'avplay' : 'video';
    player.avPlaying = false;
    player.avError = false;
    player.avOpened = false;
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
    try { video.pause(); } catch (e) {}
    video.removeAttribute('src');
    try { video.load(); } catch (e) {}
  }

  function startSources(sources, options) {
    var player = state.player;
    sources = (sources || []).filter(function (source) { return source && source.url; });
    if (!sources.length) {
      playerError('Сервис не предоставил видеопоток для этого фильма.');
      return;
    }
    sources.sort(function (a, b) {
      var aHls = /\.m3u8(\?|$)/i.test(a.url) ? 0 : 1;
      var bHls = /\.m3u8(\?|$)/i.test(b.url) ? 0 : 1;
      if (aHls !== bHls) return aHls - bHls;
      return (parseInt(b.quality, 10) || 0) - (parseInt(a.quality, 10) || 0);
    });
    player.sources = sources;
    player.sourceIndex = 0;
    player.movie = options || {};
    $('playerBadge').textContent = sources[0].quality || 'HLS';
    setHidden($('playerSpinner'), false);
    setHidden($('playerStatus'), true);
    if (player.mode === 'avplay') startAvplay(0);
    else startVideo(0);
  }

  function nextSource(reason) {
    var player = state.player;
    if (player.sourceIndex + 1 < player.sources.length) {
      player.sourceIndex += 1;
      $('playerBadge').textContent = player.sources[player.sourceIndex].quality || 'HLS';
      if (player.mode === 'avplay') startAvplay(player.sourceIndex);
      else startVideo(player.sourceIndex);
      return true;
    }
    // Native playback gave up on every source: retry the whole list in the HTML5 player.
    if (player.mode === 'avplay') {
      player.noAvplay = true;
      player.mode = 'video';
      player.sourceIndex = 0;
      stopAvplay();
      avplaySurface(false);
      startVideo(0);
      return true;
    }
    playerError(reason);
    return false;
  }

  function startVideo(index) {
    var player = state.player;
    var video = $('video');
    var source = player.sources[index];
    player.mode = 'video';
    avplaySurface(false);
    try { $('video').classList.remove('hidden'); } catch (e) {}
    player.sourceIndex = index;
    $('playerBadge').textContent = source.quality || 'HLS';
    setHidden($('playerSpinner'), false);
    video.onloadedmetadata = function () {
      var startAt = player.startAt;
      if (startAt > 5 && isFinite(video.duration) && startAt < video.duration - 20) {
        try { video.currentTime = startAt; } catch (e) {}
        toast('Продолжаем с ' + formatTime(startAt), 2600);
      }
      setHidden($('playerSpinner'), true);
      updateProgress();
    };
    video.oncanplay = function () { setHidden($('playerSpinner'), true); };
    video.onwaiting = function () { setHidden($('playerSpinner'), false); };
    video.onplaying = function () {
      setHidden($('playerSpinner'), true);
      setHidden($('playerStatus'), true);
      hideOsdSoon();
    };
    video.onerror = function () {
      var code = video.error ? video.error.code : 0;
      if (nextSource('Телевизор не смог воспроизвести поток' + (code ? ' (код ' + code + ')' : '') +
          '. Возможна несовместимость формата или ограничение сервиса.')) return;
    };
    video.onended = function () {
      player.ending = true;
      reportProgress();
      exitPlayer();
    };
    try {
      video.src = source.url;
      video.load();
      var promise = video.play();
      if (promise && promise['catch']) {
        promise['catch'](function () {
          showOsd(true);
          playerMessage('Нажмите OK, чтобы начать воспроизведение.');
        });
      }
    } catch (e) {
      nextSource('Не удалось запустить плеер телевизора.');
    }
  }

  function startAvplay(index) {
    var player = state.player;
    var api = avplayApi();
    var source = player.sources[index];
    if (!api) {
      player.noAvplay = true;
      startVideo(0);
      return;
    }
    player.mode = 'avplay';
    player.sourceIndex = index;
    player.avError = false;
    player.avPlaying = false;
    player.avOpened = false;
    $('playerBadge').textContent = source.quality || 'HLS';
    setHidden($('playerSpinner'), false);
    try { $('video').classList.add('hidden'); } catch (e) {}
    avplaySurface(true);
    var failed = false;
    var fail = function (message) {
      if (failed) return;
      failed = true;
      nextSource(message || 'Телевизор не смог воспроизвести поток.');
    };
    try {
      api.open(source.url);
      player.avOpened = true;
      api.setDisplayRect(0, 0, window.innerWidth || 1920, window.innerHeight || 1080);
      api.setListener({
        onbufferingstart: function () { setHidden($('playerSpinner'), false); },
        onbufferingprogress: function () {},
        onbufferingcomplete: function () { setHidden($('playerSpinner'), true); },
        oncurrentplaytime: function (ms) {
          player.avTime = (Number(ms) || 0) / 1000;
        },
        onstreamcompleted: function () {
          player.ending = true;
          reportProgress();
          exitPlayer();
        },
        onerror: function () { fail('Телевизор не смог воспроизвести поток. Возможна несовместимость формата.'); }
      });
      api.prepareAsync(function () {
        if (!player.active || player.mode !== 'avplay') return;
        try {
          if (player.startAt > 5) api.seekTo(Math.round(player.startAt * 1000));
          api.play();
          player.avPlaying = true;
          setHidden($('playerSpinner'), true);
          hideOsdSoon();
          startAvplayTicker();
        } catch (e) {
          fail('Не удалось запустить воспроизведение на телевизоре.');
        }
      });
    } catch (e) {
      fail('Не удалось открыть поток на телевизоре.');
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
      if (!state.player.active || state.player.mode !== 'avplay') return;
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

  function playbackNow() {
    var player = state.player;
    if (player.mode === 'avplay') {
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
      try {
        buffered = video.buffered.end(video.buffered.length - 1) / (video.duration || 1);
      } catch (e) {}
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
    if (player.mode === 'avplay') {
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
        playerMessage('Не удалось управлять воспроизведением.');
      }
      showOsd(true);
      return;
    }
    var video = $('video');
    var promise;
    if (!video.src) return;
    if (video.paused) {
      promise = video.play();
      if (promise && promise['catch']) {
        promise['catch'](function () { playerMessage('Не удалось начать воспроизведение.'); });
      }
    } else {
      video.pause();
      reportProgress();
    }
    showOsd(true);
  }

  function seek(delta) {
    var player = state.player;
    var state0 = playbackNow();
    var target;
    if (!state0.duration) return;
    target = Math.max(0, Math.min(state0.duration - 1, state0.current + delta));
    if (player.mode === 'avplay') {
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
    var state0 = playbackNow();
    var duration = state0.duration || 0;
    var current = state0.current || 0;
    var percent = duration ? (current / duration) * 100 : 0;
    var buffered = (state0.buffered || 0) * 100;
    $('progressFill').style.width = percent + '%';
    $('progressKnob').style.left = percent + '%';
    $('progressBuffer').style.width = Math.min(100, buffered) + '%';
    $('timeNow').textContent = formatTime(current);
    $('timeTotal').textContent = formatTime(duration);
    state.player.duration = duration;
    if (state.player.trackProgress && duration && current - state.player.reported > 20 && !state0.paused) {
      state.player.reported = current;
      reportProgress();
    }
  }

  function reportProgress() {
    var player = state.player;
    var state0 = playbackNow();
    if (!player.active || !player.trackProgress || !player.movieId) return;
    if (!isFinite(state0.current) || state0.current < 3) return;
    Belet.lastWatchTime(player.movieId, player.seasonId, state0.current, state0.duration, function (err) {
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
    var video = $('video');
    if (player.active && !silent) reportProgress();
    player.active = false;
    player.ending = false;
    if (player.osdTimer) {
      clearTimeout(player.osdTimer);
      player.osdTimer = null;
    }
    stopAvplay();
    avplaySurface(false);
    try { video.classList.remove('hidden'); } catch (e) {}
    try { video.pause(); } catch (e) {}
    try {
      video.onloadedmetadata = null;
      video.onerror = null;
      video.onended = null;
      video.onplaying = null;
      video.onwaiting = null;
      video.oncanplay = null;
      video.removeAttribute('src');
      video.load();
    } catch (e) {}
    showOsd(false);
  }

  /* ── profile & diagnostics ────────────────────────────────────────── */

  function openProfile() {
    screen('profile');
    state.mainFocus = $('profileRefresh');
    setTimeout(function () { $('profileRefresh').focus(); }, 30);
    loadProfile();
  }

  function loadProfile() {
    $('profilePlan').textContent = 'Загрузка данных…';
    Belet.profile(function (err, data) {
      if (err) {
        if (handleAuthError(err)) return;
        $('profilePlan').textContent = describeError(err);
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
    screen('about');
    state.mainFocus = $('aboutRetest');
    setTimeout(function () { $('aboutRetest').focus(); }, 30);
    runHealthCheck();
  }

  function runHealthCheck() {
    var host = $('aboutStatus');
    var rows = [];
    host.innerHTML = '<div class="about-row"><span>Проверка…</span><span></span></div>';
    Belet.healthCheck(function (results) {
      var labels = {
        auth: 'Авторизация (beletapis.com)',
        home: 'Каталог (homepage.belet.me)',
        film: 'Видеосервис (film.beletapis.com)'
      };
      var order = ['auth', 'home', 'film'];
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
      var deviceRow = el('div', 'about-row');
      deviceRow.appendChild(el('span', '', 'Идентификатор устройства'));
      deviceRow.appendChild(el('span', '', Belet.device() || '—'));
      host.appendChild(deviceRow);
      var sessionRow = el('div', 'about-row');
      sessionRow.appendChild(el('span', '', 'Сессия'));
      sessionRow.appendChild(el('span', '', Belet.hasSession() ? 'активна' : 'не выполнен вход'));
      host.appendChild(sessionRow);
    });
  }

  function logout() {
    dialog('Выйти из Belet?', 'Локальная сессия будет удалена с телевизора.', [
      {
        label: 'Выйти',
        primary: true,
        onSelect: function () {
          Belet.logout(function (err) {
            if (err) toast('Локальная сессия удалена; сервер не подтвердил выход', 4000);
            state.homeLoaded = false;
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
    if (state.screen === 'browse' || state.screen === 'search' || state.screen === 'profile' || state.screen === 'about') {
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
    var paused = playbackNow().paused;
    if (key === KEYS.OK) { togglePlayback(); return true; }
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
    $('methodNext').onclick = function () { cycleMethod(1); };
    $('sendCode').onclick = function () { sendCode(); };
    $('toQr').onclick = startQr;
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
      renderCodeValue();
      $('phoneDisplay').focus();
    };
    $('qrRefresh').onclick = startQr;
    $('toPhone').onclick = function () {
      stopQr();
      $('loginQr').classList.add('hidden');
      $('loginPhone').classList.remove('hidden');
      $('loginTitle').textContent = 'Вход в Belet';
      $('loginHint').textContent = 'Введите номер телефона, привязанный к аккаунту Belet.';
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

    $('browseMoreBtn').onclick = function () { loadCategory(state.browse.id, false); };
    $('searchField').onclick = editSearch;

    $('detailPlay').onclick = playMovieOrResume;
    $('detailFav').onclick = toggleFavorite;

    $('profileRefresh').onclick = loadProfile;
    $('profileFavorites').onclick = openFavorites;
    $('profileHelp').onclick = openAbout;
    $('profileLogout').onclick = logout;

    $('aboutRetest').onclick = runHealthCheck;
    $('aboutBack').onclick = function () {
      if (state.previous && state.previous !== 'about') screen(state.previous);
      else screen('home');
      setTimeout(function () { focusScreen(); }, 40);
    };

    on($('video'), 'timeupdate', updateProgress);
    on($('video'), 'progress', updateProgress);
    on($('video'), 'pause', function () {
      if (state.player.mode === 'video') { showOsd(true); reportProgress(); }
    });
    on($('video'), 'play', function () { hideOsdSoon(); });

    on(window, 'beforeunload', function () { reportProgress(); });
    on(doc, 'visibilitychange', function () {
      if (doc.hidden) reportProgress();
    });
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
    if (node) node.textContent = pad2(date.getHours()) + ':' + pad2(date.getMinutes());
  }

  function boot() {
    var session;
    registerTvKeys();
    wire();
    tickClock();
    setInterval(tickClock, 30000);
    session = Belet.init();
    if (session) {
      $('bootStatus').textContent = 'Восстанавливаем сессию…';
      loadHome(true);
    } else {
      $('bootStatus').textContent = 'Требуется вход в аккаунт Belet';
      setTimeout(function () { showLogin(); }, 350);
    }
  }

  if (doc.readyState === 'loading') on(doc, 'DOMContentLoaded', boot);
  else boot();
}());
