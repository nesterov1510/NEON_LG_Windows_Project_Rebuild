# Belet для LG webOS — новый клиент (IPK)

Тот же клиент, что и `belet_samsung_native/` для Samsung Tizen, упакованный в
webOS-пакет `tm.belet.neonclient`. Код приложения не дублируется: IPK собирается
из тех же `index.html`, `style.css`, `api.js`, `app.js` и `hls.min.js`, поэтому
вход по коду/QR, каталог, серии, карточка фильма, поиск, профиль, избранное и
плеер HLS ведут себя одинаково на обеих платформах.

## Что добавляется только для webOS

| Файл | Назначение |
| --- | --- |
| `webos-shim.js` | Подставляет устройства webOS вместо Tizen API (`webapis.productinfo.getModel`, `tizen.systeminfo.getCapability` → модель из `PalmSystem.deviceInfo` и Luna `com.webos.service.tv.systeminfo`, иначе из User-Agent), переносит фокус на элемент под курсором Magic Remote и закрывает приложение по Back на первом экране (`PalmSystem.platformBack`). |
| `appinfo.json` | Манифест webOS: `id tm.belet.neonclient`, `type web`, `main index.html`, разрешение `1920x1080`, иконки 80×80 / 130×130 и заставка 1920×1080 из `assets/`. |
| `icon.png`, `largeIcon.png`, `splash.png` | Иконки и заставка webOS (в Tizen-пакет не входят). |

Файлы клиента упаковываются **байт-в-байт** как в Samsung-сборке: `api.js`,
`app.js`, `style.css` и `hls.min.js` не правятся. Это важно: `api.js` отправляет в
теле регистрации устройства (`POST /api/v1/users/device`) строку
`model: 'Samsung Tizen <модель>'` и версию ОС в формате Tizen после успешного входа.
Для QR используются отдельные запросы `get-qrcode` и `check-qrcode`, как на Samsung. Надстройка подменяет
**не текст клиента, а платформенные API**: `tizen.systeminfo.getCapability`
возвращает `6.0` и модель телевизора, и клиент сам формирует «самсунговский»
запрос. Единственное отличие `index.html` — строка
`<script src="webos-shim.js"></script>` перед `hls.min.js`.

На старых webOS надстройка добавляет `NodeList.forEach`, если его нет. Без него
инициализация останавливалась до подключения кнопок входа. Тест извлекает
скрипты из готового IPK и проверяет QR-вход с имитацией ответов Belet.

В IPK также входит локальная служба `tm.belet.neonclient.qr` для одного запроса:
проверки подтверждения QR по HTTPS. Это исключает CORS браузера LG на этом POST;
выдача QR, хранение сессии и каталог остаются в общем клиенте Samsung. Если служба
недоступна, клиент использует прежний XHR. HTTP 400 означает ожидание; при сетевой
ошибке, 408, 429 или 5xx опрос повторяется в пределах времени жизни экрана QR.
При окончательном отказе под QR виден HTTP-статус, без токена и кода сопряжения.

Версия пакета — `0.6.6` (версия Tizen-манифеста `0.6.3` + ревизия упаковки):
webOS требует `x.y.z` в `appinfo.json` и устанавливает обновление только при
большей версии, а сам Tizen-виджет остаётся 0.6.3.

## Ключи пульта

Клиент уже понимает коды webOS, отдельная трансляция не нужна: стрелки 37–40,
OK/Enter 13, Back 461, Play 415, Pause 19, FF 417, RW 412, Stop 413.
Back на первом экране (когда приложение не обработало нажатие) закрывает
приложение через `PalmSystem.platformBack()`.

## Плеер

На LG нет `avplay`, поэтому цепочка движков — **hls.js (MSE) → `<video>`**.
HLS-манифест и сегменты запрашиваются теми же заголовками, что у сайта
`belet.tm` и Android-приложения (`Authorization`, `fingerprint`, `X-Platform`,
`X-VersionCode`, `X-Player-Session-Id`, `X-Chunk-Length`), поэтому ответы CDN
`videofilm.belet.me` не отличаются от Samsung-версии. Текущий движок и причина
сбоя видны в бейдже плеера и на экране «О приложении».

## Сборка

```bash
python scripts/build_belet_lg_client.py      # → lg-belet/BeletLgClient-webOS-unsigned.ipk
python -m pytest tests/test_belet_lg_client.py -q
```

Сборка байт-воспроизводима: текст нормализуется в LF, а отметка времени в tar/gzip
фиксирована (`SOURCE_DATE_EPOCH`), поэтому один и тот же коммит даёт одинаковый IPK
на Linux и Windows — это проверяет `tests/test_belet_lg_client.py`.

IPK кладётся в `lg-belet/BeletLgClient-webOS-unsigned.ipk`, входит в Windows-сборку
NEON (`--add-data "lg-belet;lg-belet"`) и публикуется ассетом чернового релиза.

## Установка на телевизор

* **Из NEON**: подключитесь к LG по SSH (Developer Mode), затем в разделе Belet
  нажмите **«УСТАНОВИТЬ НОВЫЙ КЛИЕНТ (LG)»** — IPK загрузится в
  `/media/developer/temp` и установится через `luna://com.webos.appInstallService/dev/install`.
  Кнопки LAUNCH и REMOVE работают с тем же `tm.belet.neonclient`.
* **Вручную** (CLI webOS): `ares-setup-device` → `ares-install -d <device> lg-belet/BeletLgClient-webOS-unsigned.ipk`
  → `ares-launch -d <device> tm.belet.neonclient`.

Приложение не заменяет прежнюю обёртку сайта `tm.belet.webos` — это отдельный
пакет, обе версии могут стоять на телевизоре одновременно.
