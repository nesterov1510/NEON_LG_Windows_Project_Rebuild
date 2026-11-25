# NEON — LG webOS + VPN Hotspot

Unofficial Windows desktop utility for `lennylxx/lg-geolock-bypass`. Includes a modern dark-glass sidebar UI, real TV command output, saved OpenVPN profile shortcuts (certificate/key and `auth-user-pass`), and direct Windows Wi-Fi Hotspot + ICS controls.

## Windows 8.1 + Windows 10/11 — Modern Desktop Installer (`app_legacy.py`)

The `app_legacy.py` edition is built with **PyQt5** specifically for **Windows 8.1**, and also runs smoothly on **Windows 10 and Windows 11** (without requiring WebView2). It includes:
- Modern dark-glass sidebar navigation with subtle animated Matrix backdrop toggle (`Matrix Backdrop`), rounded pill switches, KPI telemetry cards, syntax-colored live terminal, animated connection scanner, custom multi-size Win32 application icon, and instant **RU / EN** language switcher.
- Full LG webOS SSH control (`9922`), **webOS 26** `/media/developer/temp` toggle, automatic NVRAM backup value display on Read, and TV reboot.
- Background OpenVPN runner with **username/password (`auth-user-pass`) support** (temporary credentials wiped immediately after connect/fail/stop), **passphrase protected private keys** (the key password field appears when the profile or its `key` file is encrypted, delivered over the local management pipe) and **emergency leak protection** (stops hotspot & ICS automatically if the OpenVPN tunnel drops unexpectedly).
- Dual-mode Wi-Fi Hotspot engine: uses **Windows Hosted Network (`netsh wlan`)** on Windows 8.1 / compatible drivers, and automatically falls back to **Windows Mobile Hotspot (WinRT)** on Windows 10/11 adapters that dropped Hosted Network support.
- Built-in **Diagnostics** tab checking OS/build, Administrator UAC status, OpenVPN binary, Wi-Fi hotspot capability, and live adapter ICS states.
- Full **NSIS Windows Installer** (`NEON_Setup_Win81_10_11.exe`) creating Desktop & Start Menu shortcuts, Add/Remove Programs registration, and a complete uninstaller that removes all program files, shortcuts, registry entries, and `%APPDATA%\NEON` data.

Push changes to GitHub (or open **Actions → Build Windows 8.1 / 10 / 11 Installer → Run workflow**) and download **NEON-Windows-Installer** (`NEON_Setup_Win81_10_11.exe`), or **NeonLGControl-Windows81-Portable** for the portable folder build.

The modern WebView2 edition (`app.py` + `ui.html`) is also maintained for Windows 10/11 via **Actions → Build Windows app**.

## Linux / Ubuntu — AppImage (`app_linux.py`)

NEON runs on Linux/Ubuntu as a self-contained **AppImage** (no install needed — just `chmod +x` and run). The Linux launcher uses the same WebView UI as Windows.

- **Works on Linux:** Samsung Tizen install/launch (SDB over TCP), Alem TV app generation and install, TV SSH control, all UI features.
- **Windows-only on Linux:** VPN/Hotspot/ICS controls (OpenVPN engine, `netsh`, WinRT) report a friendly "available in the Windows build" error — use the Windows build for VPN features.

**Build locally:**

```bash
./build_linux.sh
# → dist/NeonLGControl.AppImage
./dist/NeonLGControl.AppImage
```

**Build via GitHub Actions:** open **Actions → Build Linux AppImage → Run workflow** and download the **NeonLGControl-Linux-AppImage** artifact.

**Alem TV на LG и Samsung:** для смены аккаунта откройте в приложении на ТВ **Настройки → Выйти из аккаунта → Да, выйти**. Клиент отвяжет терминал у оператора и откроет форму входа; выбранный портал сохранится. При ошибке отвязки привязка на ТВ не стирается — выход можно повторить.

**Persistent login:** the WebView profile (cookies, localStorage) is stored in `~/.local/share/NEON_LG_Windows_Project` (or `$XDG_DATA_HOME/NEON_LG_Windows_Project`), so the Alem TV terminal MAC and session survive exit and relaunch — you only enter login/password once.

## OpenVPN integration

NEON runs `openvpn.exe` as a hidden background process and displays its live log/status in the NEON terminal; it does **not** open the OpenVPN GUI. Both certificate/key `.ovpn` profiles and `auth-user-pass` profiles are supported. Browse to `.ovpn` files; their paths are remembered in `%APPDATA%\NEON\profiles.json` and non-secret app preferences in `%APPDATA%\NEON\settings.json`. VPN and TV passwords are never saved to disk.

**Passwords inside a profile.** NEON reads the selected `.ovpn` before connecting and shows exactly the fields it asks for: a username + password when the profile contains `auth-user-pass`, and a **private key password** when the key is encrypted (`BEGIN ENCRYPTED PRIVATE KEY`, or `Proc-Type: 4,ENCRYPTED` / `DEK-Info`, inline in the profile or in the `key` file it references). Profiles that use `askpass` are now supported the same way instead of being refused. The key password is delivered over the local OpenVPN management pipe (`--management-query-passwords` with `--management-hold`, so the engine never blocks waiting for a console prompt): it is kept in memory only, never written to disk and never echoed into the log. The `auth-user-pass` file NEON writes for OpenVPN is overwritten and deleted as soon as the tunnel connects, fails or is stopped, and both password fields clear themselves once the engine has taken the values. If the server rejects them, the terminal shows why (`Conf engine rejected the key password.`) instead of silently retrying.

**Точка доступа и телевизор.** В карточке Conf поля появляются автоматически; вводить их нужно только если профиль их требует. Неправильный пароль ключа или логин/пароль теперь видны в терминале сразу, а не после таймаута ТВ.

**One-time Windows dependency:** OpenVPN Community must be installed once to provide `openvpn.exe` and the TAP/Wintun network driver. The driver is a Windows system component that needs administrator installation.

## VPN hotspot on Windows 8.1 & Windows 10/11

1. Install OpenVPN Community once, then run NEON as administrator. In **VPN HOTSPOT**, browse to an `.ovpn` profile (if `auth-user-pass` is present, enter your VPN username/password).
2. Press **Connect VPN**. Wait for **Initialization Sequence Completed** in the live terminal before starting the hotspot.
3. Enter the hotspot SSID and password, then press **Start VPN Hotspot**. NEON automatically selects Hosted Network (Win 8.1) or Mobile Hotspot (Win 10/11), detects the connected VPN and hotspot virtual adapters, and configures ICS routing.
4. If adapter detection fails, use **Refresh Adapters**, choose the VPN/public and hotspot/private adapters manually, and click **Route VPN → Hotspot**. Always test a client's public IP/DNS to confirm VPN egress.

For ordinary internet over Ethernet without NEON VPN, disconnect Conf and choose **Share internet without VPN** (WebView: **Share without Conf**). The computer needs one connected physical Wi-Fi/Ethernet internet uplink and a separate Ethernet port for the client; set the client to DHCP. NEON refuses to replace an existing ICS session in this mode. Other VPN software can affect routing; verify the client's public IP and DNS. This mode is not VPN protected.

For simultaneous Ethernet and Wi-Fi, use **Ethernet + hotspot via VPN** or **Ethernet + hotspot without VPN**. This experimental mode requires a Wi-Fi driver reporting Hosted Network support; it does not use Windows Mobile Hotspot. It attempts to configure Windows ICS with both private adapters and verifies the reported configuration. Some Windows versions, drivers, VPN adapters, or bridge configurations may reject this setup. Stop existing sharing first. Stopping either output stops both. Test internet, public IP and DNS separately on both clients; changing DNS may require stopping and restarting combined sharing. This mode has not been verified on physical Windows hardware.

NEON stops the hosted network/mobile hotspot and its own ICS route on normal exit, disconnect, or unexpected tunnel exit. **This is not a guaranteed OS-level kill switch:** a system crash, forced kill, or driver/provider change can still bypass routing. Test with a non-sensitive device first.

## TV regions

The UI lists United States `22282`, CIS/SNG `3642` (requested; **not verified by upstream**), EU/KR group `19461`, and other codes. Upstream lists US `22282` as fully verified; compatibility varies by TV model/firmware. Region writes modify NVRAM and can affect features/apps. Always run **Read Current** first and proceed at your own risk.

`change_region.sh` and `UPSTREAM-LICENSE` come from upstream. NEON is independent and unaffiliated with LG or OpenVPN.

### Пульт LG webOS
На странице LG введите IP телевизора и нажмите «Подключить» в карточке пульта. Компьютер и ТВ должны быть в одной локальной сети; при первом подключении разрешите запрос на телевизоре. Пульт работает отдельно от SSH и не требует Developer Mode. Ключ сопряжения сохраняется в пользовательской папке NEON: не передавайте файл `lg_remote_keys.json` другим людям. Соединение использует локальный webOS WebSocket без шифрования, поэтому применяйте его только в доверенной сети. Для включения выключенного телевизора пульт не предназначен. Поддержка отдельных кнопок зависит от модели и открытого приложения.

### MSB Callback на телевизорах
Раздел MSB Callback в NEON устанавливает отдельное приложение на LG (через Developer Mode и SSH) или Samsung (через Developer Mode, SDB и подпись). После установки контакты и два QR-кода хранятся внутри приложения и доступны без интернета. Для открытия сайта после сканирования телефон должен иметь интернет; контактная карточка доступна офлайн. Для установки нужны локальное сетевое соединение и, при необходимости получения сертификата Samsung, доступ к интернету.

### Установка приложения из файла
На странице LG нажмите **«Выбрать IPK и установить»** после подключения к Developer Mode по SSH. Выбранный IPK загружается на ТВ и устанавливается без изменения подписи; NEON не подписывает LG IPK. На странице Samsung укажите IP ТВ и нажмите **«Выбрать WGT и установить»**. NEON проверяет WGT с Tizen web-манифестом, использует сохранённый сертификат Samsung для этого ТВ или предлагает вход в Samsung Account, затем переподписывает и отправляет пакет через SDB. Оригинальный файл на компьютере не изменяется. Только используйте доверенные файлы: подпись NEON не делает чужой код безопасным. На телевизоре должен быть включён Developer Mode, для Samsung — указан IP компьютера и выполнена перезагрузка. Установка стороннего приложения может обновить приложение с тем же ID, а замена подписи автора может привести к отказу ТВ; NEON не удаляет установленные приложения автоматически. Совместимость, привилегии и запуск зависят от пакета, сертификата и модели ТВ. Физическая проверка требуется. Поддерживаются файлы до 128 МБ, WGT — только веб-приложения Tizen, не TPK и не другие форматы.

### Belet Samsung: клиент для телевизоров Samsung
Отдельный веб-клиент Tizen находится в `belet_samsung_native/`: вход по SMS-коду или QR, обновление сессии, главная со строками и баннером, разделы каталога, поиск с экранной клавиатурой, карточка фильма с сезонами и сериями, избранное, профиль с подпиской, плеер HLS с перемоткой и записью прогресса просмотра. Сценарии воспроизведены по декомпилированному Android-приложению Belet 4.3.3 и сверены с запросами сайта `belet.tm`.

Для установки в NEON выберите **«Установить Belet Samsung · Новый клиент»**: пакет `BeletFilm1` включён в сборку и подписывается сертификатом для вашего ТВ. Обычное приложение Belet (`BeletTV001`) не заменяется. Архитектура, список API-хостов, управление пультом, сборка и тесты описаны в `belet_samsung_native/README.md`. Логика клиента покрыта автоматическими тестами на моделируемом сервере; запуск и воспроизведение на физическом телевизоре ещё не подтверждены.

### Belet LG webOS: новый клиент для телевизоров LG
Тот же клиент (вход по коду и QR, главная, каталог, поиск, карточка фильма с сериями, избранное, профиль, плеер HLS), упакованный для LG в пакет `tm.belet.neonclient`. Код приложения не дублируется: IPK собирается из исходников `belet_samsung_native/` плюс webOS-надстройка `belet_lg_client/webos-shim.js` (модель телевизора из `PalmSystem.deviceInfo` и Luna `com.webos.service.tv.systeminfo`, фокус под курсором Magic Remote, выход по запросу клиента). На LG плеер идёт по цепочке hls.js (MSE) → `<video>`; привилегия `avplay` там не используется. Файлы клиента упаковываются байт-в-байт как в Samsung-сборке (в `index.html` добавляется только тег `webos-shim.js`): после входа LG регистрируется с моделью LG и webOS через службу на ТВ. Надстройка эмулирует платформенные API Tizen (`webapis.productinfo`, `tizen.systeminfo` / `tvinputdevice` / `application`). Версия пакета — `0.6.8` (0.6.3 + ревизия упаковки: webOS ставит обновление только при большей версии), прежняя обёртка сайта `tm.belet.webos` не заменяется.

В NEON: подключитесь к LG по SSH (Developer Mode) и в разделе Belet нажмите **«УСТАНОВИТЬ НОВЫЙ КЛИЕНТ (LG)»** — IPK из `lg-belet/BeletLgClient-webOS-unsigned.ipk` загрузится и установится через `com.webos.appInstallService`; кнопки LAUNCH и REMOVE работают с `tm.belet.neonclient`. Вручную: `python scripts/build_belet_lg_client.py` и `ares-install -d <device> lg-belet/BeletLgClient-webOS-unsigned.ipk`. Подробности и тесты — в `belet_lg_client/README.md`.

### Встроенный VLESS (Windows x64)
В NEON откройте вкладку VPN, вставьте ссылку `vless://` своего провайдера и нажмите **Подключить VLESS**. Сначала отключите активный OpenVPN. Нужны права администратора для системного TUN-адаптера `NEON-VLESS`. Для остановки используйте обычную кнопку отключения VPN. Поддерживаются TCP, WebSocket и gRPC с TLS или REALITY; XHTTP и незашифрованные профили не поддерживаются. Ссылка не записывается в настройки и не передаётся в командной строке.

**Проверка интернета вместо «просто подключено».** `sing-box started` означает лишь, что создан TUN-адаптер, поэтому после подъёма туннеля NEON сам проверяет доступ: DNS-запрос через резолвер туннеля и TCP-соединение с `1.1.1.1:443`. Результат (и тексты ошибок движка, очищенные от ссылки и UUID) видны в терминале: `VLESS: internet reachable through the tunnel` либо `[!] VLESS: tunnel interface is up but no traffic gets through` с подсказкой, что делать.

**Почему туннель раньше «подключался без интернета».** sing-box привязывает свои сокеты к физическому аплинку (`auto_detect_interface`), но Windows игнорирует эту привязку, пока на аплинке включён IP-forwarding — а его включает ICS / Mobile Hotspot, то есть основной сценарий NEON. Трафик туннеля возвращался в TUN, и клиенты оставались без сети. Теперь NEON до запуска движка резолвит адрес сервера, стартует туннель на конкретном IP (SNI и проверка сертификата остаются по доменному имени) и добавляет этот адрес в `route_exclude_address` + правило `outbound: direct`, то есть на уровне таблицы маршрутов, где привязка интерфейсов уже ни на что не влияет. Если резолвер недоступен, поведение прежнее и в лог выводится предупреждение.

**Ещё два исправления.** `dns_mode: native` вместо значения по умолчанию `hijack`: на Windows с `strict_route` режим `hijack` дополнительно блокирует порт 53 вне TUN, что глушило резолвер хоста и DNS-прокси ICS у клиентов точки доступа (сам TUN-резолвер `172.19.0.2` продолжает работать). И ссылка может содержать `allowInsecure=1` — NEON передаёт её движку (`tls.insecure`), как другие клиенты; без этого сервер с самоподписанным сертификатом «поднимал туннель», по которому ничего не ходило. Собственный DNS-пресет NEON больше не перезаписывает DNS на адаптере `NEON-VLESS` — им управляет движок.

**Почему это не ловилось раньше.** В sing-box 1.14 «имя хоста резолвится, когда DNS-серверов несколько и резолвер не выбран» — это фатальная ошибка запуска движка, а не предупреждение. Поэтому в конфиге всегда указан `route.default_domain_resolver` (и `domain_resolver` у outbound): если бы NEON не смог разрешить адрес сервера до подъёма туннеля, старая сборка просто не запускала движок, оставляя поднятый адаптер без трафика. `scripts/prepare_vless.py --check` прогоняет три формы конфига через `sing-box check` на этапе сборки, чтобы подобные опции отсекались до выхода установщика.

**Строгая маршрутизация хоста.** Переключатель `Strict host routing` в карточке Conf (сохраняется как `vless_strict_route` в `%APPDATA%\NEON\settings.json`) включает/выключает WFP-фильтры `strict_route`. По умолчанию он включён (защита от утечек); если драйвер Wi-Fi или точка доступа конфликтуют с ними, выключите — NEON всё равно гасит hotspot/ICS при падении туннеля. При расшаривании выбирайте публичным адаптером именно `NEON-VLESS`, а не Wi-Fi/Ethernet-аплинк: иначе клиенты пойдут в обход туннеля.

В Windows-установщик и portable-сборки включён sing-box 1.14.4, отдельно устанавливать VPN-клиент или скачивать компоненты при подключении не нужно. Для сборки из исходников `scripts/prepare_vless.py` получает закреплённый архив и проверяет SHA-256 до упаковки (`build_windows*.bat` делают это автоматически). Исходники и лицензию движка см. в `assets/vless/`.
