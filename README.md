# NEON — LG webOS + VPN Hotspot

Unofficial Windows desktop utility for `lennylxx/lg-geolock-bypass`. Includes a modern dark-glass sidebar UI, real TV command output, saved OpenVPN profile shortcuts (certificate/key and `auth-user-pass`), and direct Windows Wi-Fi Hotspot + ICS controls.

## Windows 8.1 + Windows 10/11 — Modern Desktop Installer (`app_legacy.py`)

The `app_legacy.py` edition is built with **PyQt5** specifically for **Windows 8.1**, and also runs smoothly on **Windows 10 and Windows 11** (without requiring WebView2). It includes:
- Modern dark-glass sidebar navigation with subtle animated Matrix backdrop toggle (`Matrix Backdrop`), rounded pill switches, KPI telemetry cards, syntax-colored live terminal, animated connection scanner, custom multi-size Win32 application icon, and instant **RU / EN** language switcher.
- Full LG webOS SSH control (`9922`), **webOS 26** `/media/developer/temp` toggle, automatic NVRAM backup value display on Read, and TV reboot.
- Background OpenVPN runner with **username/password (`auth-user-pass`) support** (temporary credentials wiped immediately after connect/fail/stop) and **emergency leak protection** (stops hotspot & ICS automatically if the OpenVPN tunnel drops unexpectedly).
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

**Persistent login:** the WebView profile (cookies, localStorage) is stored in `~/.local/share/NEON_LG_Windows_Project` (or `$XDG_DATA_HOME/NEON_LG_Windows_Project`), so the Alem TV terminal MAC and session survive exit and relaunch — you only enter login/password once.

## OpenVPN integration

NEON runs `openvpn.exe` as a hidden background process and displays its live log/status in the NEON terminal; it does **not** open the OpenVPN GUI. Both certificate/key `.ovpn` profiles and `auth-user-pass` profiles are supported. Browse to `.ovpn` files; their paths are remembered in `%APPDATA%\NEON\profiles.json` and non-secret app preferences in `%APPDATA%\NEON\settings.json`. VPN and TV passwords are never saved to disk.

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
