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

Версия пакета берётся из `belet_samsung_native/config.xml`, поэтому Samsung WGT и
LG IPK всегда одной версии. В webOS-копии `api.js` подпись устройства меняется с
`Samsung Tizen <модель>` на `LG webOS <модель>` — в списке устройств Belet на
телефоне LG-телевизор показывается правильно. Исходники Samsung при этом не
меняются: патч применяется только к копии внутри IPK (и проверяется тестом).

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
