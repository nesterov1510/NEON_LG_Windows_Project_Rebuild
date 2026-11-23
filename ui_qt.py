"""Modern 2026 dark-glass PyQt5 components, QSS theme, and RU/EN i18n for NEON."""
import math
import random
from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF, QPropertyAnimation, pyqtProperty, QEasingCurve
from PyQt5.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QBrush,
    QRadialGradient,
    QTextCharFormat,
    QTextCursor,
)
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

REGIONS = [
    ('United States · US · 22282 [Verified]', 'США · US · 22282 [проверен]', '22282', True),
    ('CIS / СНГ · 3642 · Unverified', 'CIS / СНГ · 3642 [не проверен]', '3642', False),
    ('European Union · EU/KR · 19461', 'Европа · EU/KR · 19461', '19461', False),
    ('China · CN · 13741', 'Китай · CN · 13741', '13741', False),
    ('Hong Kong · HK · 13869', 'Гонконг · HK · 13869', '13869', False),
    ('Japan · JP · 19345', 'Япония · JP · 19345', '19345', False),
    ('Canada · CA · 18362', 'Канада · CA · 18362', '18362', False),
    ('Brazil · BR · 18789', 'Бразилия · BR · 18789', '18789', False),
    ('Singapore · AJ · 4837', 'Сингапур · AJ · 4837', '4837', False),
]

TR = {
    'en': {
        'app_sub': 'LG · Samsung · Conf',
        'nav_tv': 'LG webOS TV',
        'nav_sam': 'Samsung TV',
        'nav_vpn': 'Conf · Wi-Fi · LAN',
        'nav_diag': 'Diagnostics',
        'page_tv_title': 'LG webOS Region Control',
        'page_tv_sub': 'Connect via Developer Mode SSH 9922 and manage TV NVRAM & apps safely',
        'page_sam_title': 'Samsung Smart TV Service Control',
        'page_sam_sub': 'Connect via LAN WebSocket (8002/8001) to open Service Menu, MRT Option, Local Set & Factory Reset',
        'page_vpn_title': 'Network Conf · Wi-Fi Hotspot · Ethernet Cable',
        'page_vpn_sub': 'Background Conf engine + share over Wi-Fi Hotspot or Ethernet cable (192.168.137.x)',
        'page_diag_title': 'System & Network Diagnostics',
        'page_diag_sub': 'Verify OS compatibility, UAC elevation, Conf engine, and Wi-Fi driver capabilities',
        'side_status': 'LIVE TELEMETRY',
        'side_tv': 'LG TV',
        'side_sam': 'Samsung TV',
        'side_vpn': 'Conf Engine',
        'side_hs': 'Wi-Fi Hotspot',
        'side_eth': 'Ethernet LAN',
        'side_fx': 'Matrix Backdrop',
        'admin_ok': 'Administrator',
        'admin_no': 'Standard User',
        'status_disc': 'Disconnected',
        'status_conn_ing': 'Connecting…',
        'status_conn': 'TV Connected',
        'status_vpn_ing': 'Conf Connecting…',
        'status_vpn': 'Conf Active',
        'status_hot': 'Conf + Hotspot Live',
        'status_eth': 'Conf + Ethernet Live',
        'status_err': 'Error',
        'card_conn': 'Device Connection',
        'lbl_ip': 'TV IP Address (Local LAN)',
        'lbl_tv_pass': 'Developer Mode Passphrase',
        'ph_tv_pass': 'Case-sensitive key passphrase',
        'chk_os26': 'webOS 26 (/media/developer/temp)',
        'hint_tv': 'TV and PC must share a LAN. Turn on Developer Mode & Key Server on the TV.',
        'btn_connect_tv': 'Connect to TV',
        'btn_disconnect_tv': 'Disconnect',
        'card_reg': 'Target Region (NVRAM)',
        'lbl_area': 'Preset Region',
        'lbl_area_custom': 'Area Option Code (preset or manual)',
        'ph_area_custom': 'e.g. 22282 or custom numeric code',
        'region_custom_item': 'Custom code (manual input)',
        'custom_tag': 'Manual',
        'err_area_code': 'Enter a valid numeric Area Option code (1–4294967295).',
        'btn_read': 'Read Current',
        'btn_verify': 'Verify All Layers',
        'btn_write': 'Write Region to NVRAM',
        'warn_nvram': 'Writes directly to TV NVRAM. Only US (22282) is fully verified by upstream; always Read Current first.',
        'card_belet': 'Belet TV App (belet.tm)',
        'belet_desc': 'Packages and installs a chromeless fullscreen webOS app (no address bar, official Belet icon).',
        'btn_belet_install': 'Install Belet on TV',
        'btn_belet_launch': 'Launch',
        'btn_belet_remove': 'Remove',
        'belet_lg_hint': 'New Belet client (tm.belet.neonclient): the same Tizen UI as the Samsung build — code/QR sign-in, catalog, serials, HLS player. The site wrapper above stays installed.',
        'btn_belet_lg_install': 'Install new Belet client (LG)',
        'btn_belet_lg_launch': 'Launch',
        'btn_belet_lg_remove': 'Remove',
        'card_alemtv': 'Alem TV App (All Portals · IPTVPORTAL)',
        'alemtv_desc': 'Packages and installs standalone Alem TV Smart TV app (IPTVPORTAL backend, full EPG guide, Flussonic tokens, and remote control navigation).',
        'lbl_alem_portal': 'Portal server / Operator:',
        'btn_alemtv_install': 'Install Alem TV on TV',
        'btn_alemtv_launch': 'Launch',
        'btn_alemtv_remove': 'Remove',
        'kpi_tv': 'TV CONNECTION',
        'kpi_tv_idle': 'Awaiting device',
        'kpi_area': 'SELECTED CODE',
        'kpi_vpn': 'CONF & HOTSPOT',
        'kpi_vpn_idle': 'Idle',
        'backup_none': 'No NVRAM backup yet',
        'backup_prefix': 'Last backup: ',
        'term_title': 'Live System Console',
        'btn_copy': 'Copy',
        'btn_save': 'Save Log',
        'btn_clear': 'Clear',
        'btn_reboot': 'Reboot TV',
        'btn_sys_tune': 'System Setup',
        'reboot_hint': 'Always read and note the original NVRAM value before modifying.',
        'scanner_title': 'Executing Operation',
        'scanner_tag': 'WORKING',
        'vpn_card1': 'Network Conf Profile',
        'vpn_card1_desc': 'Select a certificate/key or username/password .ovpn conf file. Runs silently without external GUI.',
        'lbl_profile': 'Local Conf Profile (.ovpn)',
        'btn_browse': 'Browse Conf',
        'btn_refresh': 'Refresh',
        'lbl_vpn_user': 'Conf Username (auth-user-pass)',
        'lbl_vpn_pass': 'Conf Password',
        'ph_vpn_user': 'Required by this Conf profile',
        'ph_vpn_pass': 'Password (never saved to disk)',
        'btn_vpn_conn': 'Start Conf',
        'btn_vpn_disc': 'Disconnect',
        'vpn_stat_lbl': 'Conf state:',
        'dns_card': 'DNS for Hotspot & Ethernet Sharing',
        'dns_desc': 'Sets IPv4 DNS for clients (TVs/devices) over Wi-Fi Hotspot or Ethernet ICS. Applied automatically when Conf connects & sharing starts.',
        'lbl_dns_preset': 'DNS Provider Preset',
        'lbl_dns_primary': 'Primary DNS (IPv4)',
        'lbl_dns_secondary': 'Secondary DNS (optional)',
        'ph_dns_primary': 'e.g. 1.1.1.1 or 8.8.8.8',
        'ph_dns_secondary': 'e.g. 1.0.0.1 (optional)',
        'ph_dns_auto': 'Auto (from Conf / DHCP)',
        'chk_dns_uplink': 'Also set DNS on main internet uplink (auto-restored on stop)',
        'btn_dns_apply': 'Apply DNS',
        'btn_dns_reset': 'Reset (Auto)',
        'dns_stat_lbl': 'Active DNS profile:',
        'hs_card': 'Wi-Fi Hotspot Sharing',
        'hs_desc': 'Start Conf first. Uses Hosted Network on Win 8.1 or Mobile Hotspot on Win 10/11 and routes ICS via Conf.',
        'lbl_ssid': 'Hotspot Name (SSID)',
        'lbl_hs_pass': 'Wi-Fi Password (8–63 chars)',
        'ph_hs_pass': 'Set hotspot password',
        'btn_hs_start': 'Start Conf Hotspot',
        'btn_hs_stop': 'Stop Hotspot',
        'btn_hs_win': 'Open Windows Hotspot Settings',
        'eth_card': 'Ethernet Sharing (Wi-Fi + Conf → LAN)',
        'eth_desc': 'If your PC gets internet via Wi-Fi and Conf is active, share Conf-routed internet out through your Ethernet cable to a TV or switch (DHCP 192.168.137.x).',
        'lbl_eth_iface': 'Target Ethernet Adapter (Cable to TV / Device)',
        'eth_auto_hint': 'Auto-detects physical Ethernet (click ⟳ if plugged in after launch).',
        'btn_eth_start': 'Share via Ethernet',
        'btn_eth_stop': 'Stop',
        'ics_card': 'Manual ICS Routing (Fallback)',
        'ics_desc': 'If auto-detection fails, refresh adapters and choose Conf (Public) → Hotspot/Ethernet (Private).',
        'btn_adapters': 'Refresh Adapters',
        'lbl_pub': 'Public / Conf Adapter (TAP / Wintun / DCO)',
        'lbl_priv': 'Private / Hotspot or Ethernet Adapter',
        'btn_share': 'Route Conf → Target',
        'btn_stop_ics': 'Disable All ICS',
        'failsafe_warn': 'Auto-protection disables hotspot/Ethernet ICS if Conf engine exits unexpectedly. Verify client IP after connect.',
        'diag_title': 'Environment & Hardware Check',
        'diag_sub': 'Inspect OS support, UAC elevation, Conf runtime binary, and Windows network adapters.',
        'btn_diag_run': 'Run Diagnostics',
        'btn_diag_copy': 'Copy Report',
        'btn_diag_logs': 'Open Data Folder',
        'diag_os': 'OPERATING SYSTEM',
        'diag_admin': 'PRIVILEGES (UAC)',
        'diag_ovpn': 'CONF ENGINE',
        'diag_wifi': 'HOTSPOT DRIVER',
        'diag_adapters': 'Windows Network Adapters',
        'col_name': 'Adapter Name',
        'col_device': 'Device Description',
        'col_status': 'Link Status',
        'col_ics': 'ICS Sharing',
        'confirm_ok': 'Confirm',
        'confirm_cancel': 'Cancel',
        'footer': 'NEON Community Utility  ·  Windows 8.1 / 10 / 11  ·  LG · Samsung · Conf Suite',
        'sam_card_conn': 'Samsung TV Connection (LAN)',
        'sam_lbl_ip': 'Samsung TV IP Address',
        'sam_btn_scan': 'Scan',
        'sam_lbl_found': 'Discovered TVs on Network',
        'sam_lbl_proto': 'Transport Protocol',
        'sam_hint_pair': 'On first connect, press "Allow" on your TV screen. Token saves automatically.',
        'sam_btn_conn': 'Connect',
        'sam_btn_disc': 'Disconnect',
        'sam_card_svc': 'Service Menu & MRT Option',
        'sam_svc_hint': 'Option → MRT → Local Set. Exit safely via Power/Reboot or Exit once (wait 3s through RGB flash); if stuck on RGB/text, press FACTORY once.',
        'sam_btn_sm': 'Service Menu (Info + Factory)',
        'sam_btn_3spd': 'Factory + 3Speed',
        'sam_btn_adv': 'Advanced (0098)',
        'sam_btn_opt': 'Option (OK)',
        'sam_btn_exit': 'Exit (Factory ×2)',
        'sam_lbl_hosp': 'Legacy / Alt Unlock:',
        'sam_lbl_localset': 'Target Local Set / MRT Guide',
        'sam_btn_smarthub': 'Smart Hub (Terms)',
        'sam_card_reset': 'Factory Reset & Power',
        'sam_btn_reset': 'Factory Reset',
        'sam_btn_power': 'Power / Reboot',
        'sam_kpi_tv': 'SAMSUNG TV',
        'sam_kpi_idle': 'Awaiting TV',
        'sam_kpi_proto': 'TRANSPORT / TOKEN',
        'sam_kpi_proto_idle': 'Not paired',
        'sam_kpi_country': 'TV COUNTRY / LANG',
        'sam_remote_title': 'Virtual Service Remote',
        'sam_kb_nav': 'KB Nav',
        'sam_card_belet': 'Belet TV (BeletFilm1 · Tizen SDB 26101)',
        'sam_belet_desc': 'Installs the new native Belet TV client (BeletFilm1) on Samsung Tizen via SDB 26101. Choose "With bypass" if TV clock resets to 1970 or HTTPS fails, or "Without bypass" for TVs with accurate time and direct HTTPS. On TV: Apps → 12345 → Developer Mode ON → Host PC IP below → Reboot TV.',
        'sam_belet_host_prefix': 'This PC LAN IP (Host IP on TV): ',
        'sam_btn_sdb_check': 'Check SDB 26101',
        'sam_btn_cert': 'Samsung Cert (Sign-In)',
        'sam_btn_belet_install': 'Belet TV · With bypass (1970 / TLS)',
        'sam_btn_belet_direct': 'Belet TV · Without bypass (New client)',
        'sam_btn_belet_launch': 'Launch',
        'sam_btn_belet_remove': 'Remove',
        'sam_card_alemtv': 'Alem TV App (Tizen · All Portals)',
        'sam_alemtv_desc': 'Packages, signs (.wgt XML-DSig) and installs Alem TV on Samsung TV via SDB 26101 with EPG guide, channels, archive timeshift, and TV remote support.',
        'sam_lbl_alem_portal': 'Portal server / Operator:',
        'sam_btn_alemtv_install': 'Install Alem TV on Samsung TV',
        'sam_btn_alemtv_launch': 'Launch',
        'sam_btn_alemtv_remove': 'Remove',
        'sam_card_time': 'Samsung Time Domain Override (Tizen)',
        'sam_time_desc': 'Redirects Samsung hardcoded time requests (time.samsungcloudsolution.com/.net) to any custom time server or local PC clock via DNS (:53) + /openapi/timesync (:80) + NTP (:123). Works on all retail Tizen TVs without root.',
        'sam_time_lbl_mode': 'Redirect Mode:',
        'sam_time_lbl_target': 'Target Time Server (domain, IP or "local"):',
        'sam_time_lbl_sources': 'Intercepted Samsung Domain(s):',
        'sam_time_chk_check': 'Also answer connectivity probe (cdn.samsungcloudsolution.com/check.xml)',
        'sam_time_lbl_upstream': 'Upstream DNS (for all other domains):',
        'sam_time_btn_start': 'Start Time Override',
        'sam_time_btn_stop': 'Stop',
        'sam_time_btn_sync': 'Sync / Test',
        'sam_time_btn_diag': 'Check DNS (1.1.1.1 / 8.8.8.8)',
        'sam_time_chk_autostop': 'One-Time PC Sync (auto-stop after TV syncs; switch TV DNS back before PC off)',
        'sam_time_remote_title': 'Autonomous 24/7 Setup (PC Not Needed Afterward):',
        'sam_time_lbl_remote_host': 'Router / Linux / VPS IP (SSH):',
        'sam_time_lbl_remote_user': 'SSH User / Port:',
        'sam_time_lbl_remote_pass': 'SSH Password:',
        'sam_time_btn_deploy': 'Install 24/7 on Router / VPS (1-Click)',
        'sam_time_btn_export': 'Save Standalone .py Script',
        'sam_time_btn_snippets': 'Router Rules (MikroTik/OpenWrt/Keenetic)',
        'sam_time_note_nopc': 'Note: Retail Tizen hardcodes time.samsungcloudsolution.com in read-only firmware. Use "Check DNS" first (1.1.1.1/8.8.8.8 may work without PC), or deploy 24/7 once to a Router/VPS from PC.',
        'sam_btn_send_text': 'Send Text/IP to TV',
        'sam_txt_placeholder': 'Type DNS IP or text for TV field (e.g. 1.1.1.1)',
        'sam_btn_hotel': 'Hotel/NTP Menu (119)',
        'sam_time_dns_prefix': 'On TV → Network → IP Settings → Manual DNS: ',
        'sam_time_status_idle': 'Status: STOPPED',
        'sam_time_status_active': 'Status: ACTIVE',
    },
    'ru': {
        'app_sub': 'LG · Samsung · Conf',
        'nav_tv': 'LG webOS ТВ',
        'nav_sam': 'Samsung ТВ',
        'nav_vpn': 'Conf · Wi-Fi · LAN',
        'nav_diag': 'Диагностика',
        'page_tv_title': 'Управление регионом LG webOS',
        'page_tv_sub': 'Подключение по Developer Mode SSH 9922, смена NVRAM и установка webOS-приложений',
        'page_sam_title': 'Сервисное управление Samsung Smart TV',
        'page_sam_sub': 'Подключение по сети LAN (8002/8001): Service Menu, MRT Option, Local Set и Factory Reset',
        'page_vpn_title': 'Сетевой Conf · Точка Wi-Fi · Ethernet-кабель',
        'page_vpn_sub': 'Фоновый движок Conf + раздача по Wi-Fi или кабелю Ethernet (192.168.137.x)',
        'page_diag_title': 'Диагностика системы и сети',
        'page_diag_sub': 'Проверка версии Windows, прав администратора, движка Conf и сетевых адаптеров',
        'side_status': 'СТАТУС СИСТЕМЫ',
        'side_tv': 'LG ТВ',
        'side_sam': 'Samsung ТВ',
        'side_vpn': 'Модуль Conf',
        'side_hs': 'Точка Wi-Fi',
        'side_eth': 'Ethernet LAN',
        'side_fx': 'Фон «Матрица»',
        'admin_ok': 'Администратор',
        'admin_no': 'Обычный запуск',
        'status_disc': 'Отключено',
        'status_conn_ing': 'Подключение…',
        'status_conn': 'ТВ подключён',
        'status_vpn_ing': 'Запуск Conf…',
        'status_vpn': 'Conf активен',
        'status_hot': 'Conf + Точка активны',
        'status_eth': 'Conf + Ethernet активен',
        'status_err': 'Ошибка',
        'card_conn': 'Подключение к телевизору',
        'lbl_ip': 'IP-адрес телевизора (локальная сеть)',
        'lbl_tv_pass': 'Пароль Developer Mode',
        'ph_tv_pass': 'Пароль ключа (учитывается регистр)',
        'chk_os26': 'webOS 26 (/media/developer/temp)',
        'hint_tv': 'ТВ и ПК должны быть в одной сети. Включите Developer Mode и Key Server на ТВ.',
        'btn_connect_tv': 'Подключиться к ТВ',
        'btn_disconnect_tv': 'Отключить',
        'card_reg': 'Целевой регион (NVRAM)',
        'lbl_area': 'Готовый пресет региона',
        'lbl_area_custom': 'Код Area Option (вручную или из списка)',
        'ph_area_custom': 'Например: 22282 или свой числовой код',
        'region_custom_item': 'Пользовательский код (ручной ввод)',
        'custom_tag': 'Ручной ввод',
        'err_area_code': 'Введите корректный числовой код Area Option (1–4294967295).',
        'btn_read': 'Прочитать',
        'btn_verify': 'Проверить слои',
        'btn_write': 'Записать регион в NVRAM',
        'warn_nvram': 'Запись меняет NVRAM телевизора. Полностью проверен только США (22282); всегда сначала делайте «Прочитать».',
        'card_belet': 'Приложение Belet (belet.tm)',
        'belet_desc': 'Устанавливает полноэкранное PWA-приложение на webOS без адресной строки и с иконкой Belet.',
        'btn_belet_install': 'Установить Belet на ТВ',
        'btn_belet_launch': 'Запустить',
        'btn_belet_remove': 'Удалить',
        'belet_lg_hint': 'Новый клиент Belet (tm.belet.neonclient): тот же интерфейс, что в Samsung-сборке — вход по коду/QR, каталог, серии, плеер HLS. Обёртка сайта выше остаётся установленной.',
        'btn_belet_lg_install': 'Установить новый клиент Belet (LG)',
        'btn_belet_lg_launch': 'Запустить',
        'btn_belet_lg_remove': 'Удалить',
        'card_alemtv': 'Приложение Alem TV (Все порталы · IPTVPORTAL)',
        'alemtv_desc': 'Устанавливает полноэкранное Smart TV приложение Alem TV на webOS (IPTVPORTAL, телепрограмма EPG, токены Flussonic и управление пультом).',
        'lbl_alem_portal': 'Сервер портала (Оператор):',
        'btn_alemtv_install': 'Установить Alem TV на ТВ',
        'btn_alemtv_launch': 'Запустить',
        'btn_alemtv_remove': 'Удалить',
        'kpi_tv': 'СВЯЗЬ С ТВ',
        'kpi_tv_idle': 'Ожидание ТВ',
        'kpi_area': 'ВЫБРАННЫЙ КОД',
        'kpi_vpn': 'CONF И ТОЧКА',
        'kpi_vpn_idle': 'Простой',
        'backup_none': 'Бэкап NVRAM не снят',
        'backup_prefix': 'Бэкап NVRAM: ',
        'term_title': 'Живая консоль вывода',
        'btn_copy': 'Копировать',
        'btn_save': 'Сохранить',
        'btn_clear': 'Очистить',
        'btn_reboot': 'Перезагрузить ТВ',
        'btn_sys_tune': 'Настройка системы',
        'reboot_hint': 'Перед записью сохраните исходный код региона.',
        'scanner_title': 'Выполнение операции',
        'scanner_tag': 'В РАБОТЕ',
        'vpn_card1': 'Сетевой профиль Conf',
        'vpn_card1_desc': 'Выберите конфиг (.ovpn — сертификат или логин/пароль). Работает скрытно без сторонних окон.',
        'lbl_profile': 'Локальный профиль Conf (.ovpn)',
        'btn_browse': 'Выбрать конфиг',
        'btn_refresh': 'Обновить',
        'lbl_vpn_user': 'Логин Conf (auth-user-pass)',
        'lbl_vpn_pass': 'Пароль Conf',
        'ph_vpn_user': 'Требуется выбранным профилем',
        'ph_vpn_pass': 'Пароль (не сохраняется на диск)',
        'btn_vpn_conn': 'Запустить Conf',
        'btn_vpn_disc': 'Отключить',
        'vpn_stat_lbl': 'Статус Conf:',
        'dns_card': 'DNS для точки Wi-Fi и Ethernet',
        'dns_desc': 'Задаёт IPv4 DNS для устройств на точке Wi-Fi или кабеле Ethernet (ICS). Применяется автоматически при запуске Conf и раздачи.',
        'lbl_dns_preset': 'Пресет DNS-сервера',
        'lbl_dns_primary': 'Основной DNS (IPv4)',
        'lbl_dns_secondary': 'Резервный DNS (необяз.)',
        'ph_dns_primary': 'Напр.: 1.1.1.1 или 8.8.8.8',
        'ph_dns_secondary': 'Напр.: 1.0.0.1 (необяз.)',
        'ph_dns_auto': 'Авто (от Conf / DHCP)',
        'chk_dns_uplink': 'Также задать DNS на основном интернет-адаптере (с авто-возвратом)',
        'btn_dns_apply': 'Применить DNS',
        'btn_dns_reset': 'Сброс (Авто)',
        'dns_stat_lbl': 'Профиль DNS:',
        'hs_card': 'Раздача Wi-Fi точки доступа',
        'hs_desc': 'Сначала запустите Conf. На Win 8.1 запускает Hosted Network, на Win 10/11 — Mobile Hotspot через Conf.',
        'lbl_ssid': 'Имя Wi-Fi сети (SSID)',
        'lbl_hs_pass': 'Пароль Wi-Fi (8–63 символа)',
        'ph_hs_pass': 'Задайте пароль точки доступа',
        'btn_hs_start': 'Запустить Conf-точку',
        'btn_hs_stop': 'Остановить',
        'btn_hs_win': 'Открыть настройки точки Windows',
        'eth_card': 'Раздача через Ethernet (Wi-Fi + Conf)',
        'eth_desc': 'Если ПК подключён к интернету по Wi-Fi и включён Conf, раздайте интернет с Conf через Ethernet-кабель на ТВ или свитч (DHCP 192.168.137.x).',
        'lbl_eth_iface': 'Целевой Ethernet-адаптер (кабель к ТВ / устройству)',
        'eth_auto_hint': 'Физический Ethernet выбирается автоматически (нажмите ⟳, если подключили позже).',
        'btn_eth_start': 'Раздать по Ethernet',
        'btn_eth_stop': 'Остановить',
        'ics_card': 'Ручной маршрут ICS (резерв)',
        'ics_desc': 'Если автоопределение не сработало, обновите адаптеры и выберите Conf (внешний) → Точка/Ethernet (внутренний).',
        'btn_adapters': 'Обновить адаптеры',
        'lbl_pub': 'Внешний адаптер · Conf (TAP / Wintun / DCO)',
        'lbl_priv': 'Внутренний адаптер · Точка Wi-Fi или Ethernet',
        'btn_share': 'Направить Conf → Цель',
        'btn_stop_ics': 'Сбросить все ICS',
        'failsafe_warn': 'Автозащита отключает точку/Ethernet ICS при внезапном обрыве Conf-движка. Проверяйте адрес на клиенте.',
        'diag_title': 'Проверка системы и сети',
        'diag_sub': 'Информация о Windows, правах UAC, движке Conf и поддерживаемых режимах Wi-Fi-точки.',
        'btn_diag_run': 'Проверить систему',
        'btn_diag_copy': 'Копировать отчёт',
        'btn_diag_logs': 'Папка данных',
        'diag_os': 'ОПЕРАЦИОННАЯ СИСТЕМА',
        'diag_admin': 'ПРАВА ДОСТУПА (UAC)',
        'diag_ovpn': 'ДВИЖОК CONF',
        'diag_wifi': 'ДРАЙВЕР ТОЧКИ WI-FI',
        'diag_adapters': 'Сетевые адаптеры Windows',
        'col_name': 'Имя адаптера',
        'col_device': 'Устройство',
        'col_status': 'Состояние',
        'col_ics': 'Общий доступ ICS',
        'confirm_ok': 'Подтвердить',
        'confirm_cancel': 'Отмена',
        'footer': 'Утилита NEON  ·  Windows 8.1 / 10 / 11  ·  LG · Samsung · Conf Suite',
        'sam_card_conn': 'Подключение к Samsung ТВ (LAN)',
        'sam_lbl_ip': 'IP-адрес Samsung ТВ',
        'sam_btn_scan': 'Найти',
        'sam_lbl_found': 'Найденные ТВ в локальной сети',
        'sam_lbl_proto': 'Протокол подключения',
        'sam_hint_pair': 'При первом запуске нажмите «Разрешить» на ТВ. Токен сохранится автоматически.',
        'sam_btn_conn': 'Подключиться',
        'sam_btn_disc': 'Отключить',
        'sam_card_svc': 'Сервисное меню и MRT Option',
        'sam_svc_hint': 'Option → MRT → Local Set. Выход: Power/Reboot или «Выйти» 1 раз (ждать 3с); при красном/синем тесте нажмите FACTORY 1 раз на тексте.',
        'sam_btn_sm': 'Service Menu (Info + Factory)',
        'sam_btn_3spd': 'Factory + 3Speed',
        'sam_btn_adv': 'Advanced (0098)',
        'sam_btn_opt': 'Option (OK)',
        'sam_btn_exit': 'Выйти (Factory ×2)',
        'sam_lbl_hosp': 'Альтернативный вход (старые ТВ):',
        'sam_lbl_localset': 'Справочник Local Set / MRT Option',
        'sam_btn_smarthub': 'Smart Hub (Регион)',
        'sam_card_reset': 'Factory Reset и питание',
        'sam_btn_reset': 'Factory Reset (Сброс)',
        'sam_btn_power': 'Питание / Reboot',
        'sam_kpi_tv': 'SAMSUNG ТВ',
        'sam_kpi_idle': 'Ожидание ТВ',
        'sam_kpi_proto': 'ПРОТОКОЛ / ТОКЕН',
        'sam_kpi_proto_idle': 'Нет пары',
        'sam_kpi_country': 'СТРАНА / ЯЗЫК ТВ',
        'sam_remote_title': 'Виртуальный пульт (LAN)',
        'sam_kb_nav': 'Стрелки ПК',
        'sam_card_belet': 'Belet TV (BeletFilm1 · Tizen SDB 26101)',
        'sam_belet_desc': 'Устанавливает новый нативный клиент Belet TV (BeletFilm1) на Samsung Tizen по SDB 26101. Выберите «С обходом» при часах 1970 или проблемах HTTPS, либо «Без обхода» для прямого HTTPS при точных часах ТВ. На ТВ: Apps → 12345 → Developer Mode ON → Host IP ниже → Reboot ТВ.',
        'sam_belet_host_prefix': 'IP этого ПК (ввести на ТВ как Host IP): ',
        'sam_btn_sdb_check': 'Проверить SDB',
        'sam_btn_cert': 'Сертификат Samsung',
        'sam_btn_belet_install': 'Belet TV · С обходом (1970 / TLS)',
        'sam_btn_belet_direct': 'Belet TV · Без обхода (Новый клиент)',
        'sam_btn_belet_launch': 'Запустить',
        'sam_btn_belet_remove': 'Удалить',
        'sam_card_alemtv': 'Приложение Alem TV (Tizen · Все порталы)',
        'sam_alemtv_desc': 'Собирает, подписывает (.wgt XML-DSig) и ставит Alem TV на Samsung ТВ по SDB 26101 (полноэкранный плеер с программой EPG, архивом и управлением пультом).',
        'sam_lbl_alem_portal': 'Сервер портала (Оператор):',
        'sam_btn_alemtv_install': 'Установить Alem TV на Samsung ТВ',
        'sam_btn_alemtv_launch': 'Запустить',
        'sam_btn_alemtv_remove': 'Удалить',
        'sam_card_time': 'Смена домена времени Samsung Tizen',
        'sam_time_desc': 'Перенаправляет штатные запросы времени ТВ (time.samsungcloudsolution.com/.net) на выбранный сервер или часы ПК через DNS (:53) + /openapi/timesync (:80) + NTP (:123). Работает на любых розничных ТВ без root.',
        'sam_time_lbl_mode': 'Режим перенаправления:',
        'sam_time_lbl_target': 'Новый сервер / домен времени (или "local"):',
        'sam_time_lbl_sources': 'Перехватываемые домены Samsung:',
        'sam_time_chk_check': 'Отвечать и на проверку сети (cdn.samsungcloudsolution.com/check.xml)',
        'sam_time_lbl_upstream': 'Внешний DNS (для остальных сайтов):',
        'sam_time_btn_start': 'Запустить перехват времени',
        'sam_time_btn_stop': 'Остановить',
        'sam_time_btn_sync': 'Проверить / Синхр.',
        'sam_time_btn_diag': 'Тест DNS (1.1.1.1 / 8.8.8.8)',
        'sam_time_chk_autostop': 'Разовая синхр. с ПК (авто-стоп после синхр. ТВ; верните DNS перед выкл. ПК)',
        'sam_time_remote_title': 'Автономно 24/7 (чтобы ПК потом вообще не подключать):',
        'sam_time_lbl_remote_host': 'IP Роутера / Linux / VPS (SSH):',
        'sam_time_lbl_remote_user': 'SSH Логин / Порт:',
        'sam_time_lbl_remote_pass': 'SSH Пароль:',
        'sam_time_btn_deploy': 'Установить 24/7 на Роутер / VPS (1 раз)',
        'sam_time_btn_export': 'Сохранить .py скрипт',
        'sam_time_btn_snippets': 'Правила роутера (MikroTik/OpenWrt/Keenetic)',
        'sam_time_note_nopc': 'Важно: в обычных ТВ Samsung домен time.samsungcloudsolution.com зашит в прошивке. Нажмите «Тест DNS» (возможно хватит 1.1.1.1/8.8.8.8 без ПК) или 1 раз установите шлюз на роутер/VPS прямо с ПК.',
        'sam_btn_send_text': 'Отправить текст/IP на ТВ',
        'sam_txt_placeholder': 'DNS IP или текст в поле на ТВ (напр. 1.1.1.1)',
        'sam_btn_hotel': 'Hotel/NTP Меню (119)',
        'sam_time_dns_prefix': 'На ТВ → Сеть → Настройки IP → DNS Вручную: ',
        'sam_time_status_idle': 'Статус: ОСТАНОВЛЕН',
        'sam_time_status_active': 'Статус: АКТИВЕН',
    },
}


def build_neon_qss(chevron_path=''):
    chev_url = str(chevron_path).replace('\\', '/')
    chev_rule = f'image: url("{chev_url}"); width: 12px; height: 12px;' if chev_url else ''
    return NEON_QSS.replace('__CHEVRON_RULE__', chev_rule)


NEON_QSS = """
QWidget {
    color: #e6eff4;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
}

QWidget#ScrollContent {
    background: transparent;
}

/* Sidebar */
QFrame#Sidebar {
    background-color: rgba(11, 17, 22, 246);
    border-right: 1px solid #1b2830;
}

QLabel#BrandName {
    color: #f0f8f4;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 17px;
    font-weight: bold;
    letter-spacing: 1px;
}

QLabel#VersionChip {
    background-color: #133124;
    color: #5ef2a0;
    border: 1px solid #265941;
    border-radius: 6px;
    padding: 1px 6px;
    font-family: "Consolas", monospace;
    font-size: 10px;
    font-weight: bold;
}

QLabel#BrandSub {
    color: #778d99;
    font-size: 11px;
}

QPushButton#SideNavBtn {
    background-color: transparent;
    color: #8ea3b0;
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 10px 14px;
    text-align: left;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#SideNavBtn:hover {
    background-color: rgba(24, 37, 46, 180);
    color: #e4f0f5;
}
QPushButton#SideNavBtn[active="true"] {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(25, 68, 49, 225), stop:1 rgba(17, 36, 32, 200));
    color: #72ffab;
    border: 1px solid #2b664b;
}

QFrame#SideWidget {
    background-color: rgba(16, 25, 32, 220);
    border: 1px solid #1d2d37;
    border-radius: 12px;
}

QLabel#SectionCap {
    color: #6b818f;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 0.9px;
}

QLabel#EnvBadge {
    background-color: rgba(18, 46, 34, 210);
    color: #5ef2a0;
    border: 1px solid #265c43;
    border-radius: 10px;
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#EnvBadge[warn="true"] {
    background-color: rgba(48, 36, 16, 210);
    color: #ffd073;
    border: 1px solid #6b5222;
}

/* Header Strip */
QFrame#TopHeader {
    background-color: rgba(10, 15, 20, 210);
    border-bottom: 1px solid #19252d;
}

QLabel#PageTitle {
    color: #f1f7fa;
    font-size: 16px;
    font-weight: bold;
}

QLabel#PageSub {
    color: #7e94a1;
    font-size: 11px;
}

QPushButton#LangBtn {
    background-color: #121c23;
    color: #b7ccd6;
    border: 1px solid #243642;
    border-radius: 8px;
    padding: 5px 11px;
    font-family: "Consolas", monospace;
    font-size: 11px;
    font-weight: bold;
}
QPushButton#LangBtn:hover {
    border: 1px solid #3fa678;
    color: #72ffab;
}

QPushButton#WinBtn {
    background-color: transparent;
    color: #889ea9;
    border: none;
    border-radius: 7px;
    min-width: 30px;
    max-width: 30px;
    min-height: 28px;
    max-height: 28px;
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
}
QPushButton#WinBtn:hover {
    background-color: #192630;
    color: #f0f8fc;
}

QPushButton#WinCloseBtn {
    background-color: transparent;
    color: #93a7b2;
    border: none;
    border-radius: 7px;
    min-width: 30px;
    max-width: 30px;
    min-height: 28px;
    max-height: 28px;
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
}
QPushButton#WinCloseBtn:hover {
    background-color: #9c2b3a;
    color: #ffffff;
}

/* Glass Cards */
QFrame#GlassCard {
    background-color: rgba(15, 23, 29, 234);
    border: 1px solid #1f2f39;
    border-radius: 14px;
}

QFrame#AuthBox {
    background-color: rgba(11, 20, 24, 240);
    border: 1px solid #24463c;
    border-radius: 10px;
}

QFrame#KpiCard {
    background-color: rgba(15, 23, 29, 234);
    border: 1px solid #1e2d37;
    border-radius: 13px;
}

QFrame#TerminalShell {
    background-color: rgba(9, 14, 18, 244);
    border: 1px solid #1e2e38;
    border-radius: 14px;
}

QFrame#TermHeader {
    background-color: rgba(14, 22, 28, 245);
    border-bottom: 1px solid #1a2932;
    border-top-left-radius: 13px;
    border-top-right-radius: 13px;
}

QLabel#StepBadge {
    background-color: #153528;
    color: #6bffac;
    border: 1px solid #28654b;
    border-radius: 10px;
    min-width: 20px;
    max-width: 20px;
    min-height: 20px;
    max-height: 20px;
    font-family: "Consolas", monospace;
    font-size: 11px;
    font-weight: bold;
}

QLabel#CardTitle {
    color: #ecf4f8;
    font-size: 14px;
    font-weight: bold;
}

QLabel#FieldLabel {
    color: #8ea5b2;
    font-size: 11px;
    font-weight: 600;
}

QLabel#HintLabel {
    color: #778d99;
    font-size: 11px;
}

QLabel#WarnBox {
    background-color: rgba(41, 31, 14, 215);
    border: 1px solid #6b5122;
    border-radius: 10px;
    color: #eccb83;
    padding: 9px 11px;
    font-size: 11px;
}

/* Inputs & Combos */
QLineEdit {
    background-color: #090f14;
    color: #ebf4f8;
    border: 1px solid #22333d;
    border-radius: 9px;
    padding: 0 12px;
    min-height: 38px;
    font-family: "Consolas", monospace;
    font-size: 12px;
    selection-background-color: #216649;
    selection-color: #ffffff;
}
QLineEdit:focus {
    border: 1px solid #3ee68f;
    background-color: #0b1319;
}
QLineEdit:disabled {
    background-color: #070c10;
    color: #5c727e;
    border: 1px solid #18252d;
}

QComboBox {
    background-color: #090f14;
    color: #ebf4f8;
    border: 1px solid #22333d;
    border-radius: 9px;
    padding: 0 12px;
    min-height: 38px;
    font-size: 12px;
}
QComboBox:hover {
    border: 1px solid #355161;
}
QComboBox:focus {
    border: 1px solid #3ee68f;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 32px;
    border-left: 1px solid #1b2a33;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}
QComboBox::down-arrow {
    __CHEVRON_RULE__
}
QComboBox QAbstractItemView {
    background-color: #0d151b;
    color: #e6f1f6;
    border: 1px solid #2c5f48;
    selection-background-color: #173c2d;
    selection-color: #72ffab;
    outline: none;
    padding: 4px;
    font-size: 12px;
}

/* Buttons */
QPushButton#PrimaryBtn {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #5bff9d, stop:1 #22d68a);
    color: #04150d;
    border: none;
    border-radius: 10px;
    min-height: 40px;
    padding: 0 16px;
    font-size: 13px;
    font-weight: bold;
}
QPushButton#PrimaryBtn:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7dffb0, stop:1 #36e69c);
}
QPushButton#PrimaryBtn:pressed {
    background-color: #24c980;
}
QPushButton#PrimaryBtn:disabled {
    background-color: #182722;
    color: #536b60;
}

QPushButton#NormalBtn {
    background-color: #142129;
    color: #cde3ed;
    border: 1px solid #263b47;
    border-radius: 9px;
    min-height: 37px;
    padding: 0 13px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#NormalBtn:hover {
    background-color: #1a2d37;
    border: 1px solid #3c6f60;
    color: #72ffab;
}
QPushButton#NormalBtn:disabled {
    background-color: #10191f;
    color: #506470;
    border: 1px solid #1b2830;
}

QPushButton#GhostBtn {
    background-color: transparent;
    color: #97adb9;
    border: 1px solid #23333d;
    border-radius: 9px;
    min-height: 36px;
    padding: 0 13px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#GhostBtn:hover {
    background-color: rgba(24, 38, 46, 180);
    color: #eef6fa;
    border: 1px solid #395566;
}
QPushButton#GhostBtn:disabled {
    color: #4f636e;
    border: 1px solid #1a262e;
}

QPushButton#DangerBtn {
    background-color: rgba(54, 21, 29, 220);
    color: #ff9ba7;
    border: 1px solid #75303f;
    border-radius: 9px;
    min-height: 37px;
    padding: 0 13px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#DangerBtn:hover {
    background-color: #521d2a;
    border: 1px solid #c24b60;
    color: #ffd0d5;
}
QPushButton#DangerBtn:disabled {
    background-color: #201216;
    color: #69444b;
    border: 1px solid #381e24;
}

QPushButton#MiniBtn {
    background-color: #131e25;
    color: #97aeb9;
    border: 1px solid #22333d;
    border-radius: 7px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton#MiniBtn:hover {
    background-color: #1a2b34;
    color: #72ffab;
    border: 1px solid #326e57;
}

QPushButton#EyeBtn {
    background-color: #0e171d;
    color: #8da6b3;
    border: 1px solid #22333d;
    border-radius: 9px;
    min-width: 52px;
    max-width: 52px;
    min-height: 38px;
    max-height: 38px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton#EyeBtn:hover {
    color: #72ffab;
    border: 1px solid #378767;
}

QTextEdit#TerminalView {
    background-color: transparent;
    border: none;
    color: #a6ecc0;
    font-family: "Consolas", monospace;
    font-size: 12px;
    selection-background-color: #1f4f3c;
    padding: 12px 15px;
}

QScrollArea {
    background: transparent;
    border: none;
}

QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 4px 1px;
}
QScrollBar::handle:vertical {
    background: rgba(95, 130, 148, 75);
    min-height: 28px;
    border-radius: 3px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(94, 242, 160, 150);
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 7px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #253844;
    min-width: 28px;
    border-radius: 3px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

QTableWidget {
    background-color: rgba(11, 17, 22, 235);
    alternate-background-color: rgba(15, 23, 29, 235);
    color: #dfeaf0;
    gridline-color: #1b2a33;
    border: 1px solid #20313b;
    border-radius: 12px;
    font-family: "Consolas", monospace;
    font-size: 11px;
}
QHeaderView::section {
    background-color: #121c23;
    color: #8ba3b0;
    border: none;
    border-bottom: 1px solid #21323d;
    padding: 9px 11px;
    font-family: "Segoe UI", sans-serif;
    font-size: 11px;
    font-weight: bold;
}

QToolTip {
    background-color: #101920;
    color: #e4f0f5;
    border: 1px solid #2f7058;
    padding: 6px 8px;
    font-size: 11px;
}

QPushButton#SamKeyBtn {
    background-color: #131e26;
    color: #dbeaf2;
    border: 1px solid #243744;
    border-radius: 8px;
    min-height: 32px;
    padding: 2px 8px;
    font-family: "Consolas", "Segoe UI", monospace;
    font-size: 11px;
    font-weight: bold;
}
QPushButton#SamKeyBtn:hover {
    background-color: #1a2d38;
    border: 1px solid #3fe394;
    color: #72ffab;
}
QPushButton#SamKeyBtn:pressed {
    background-color: #143628;
}

QPushButton#SamKeyPrimary {
    background-color: #173a2b;
    color: #72ffab;
    border: 1px solid #2f7556;
    border-radius: 8px;
    min-height: 32px;
    padding: 2px 8px;
    font-family: "Consolas", "Segoe UI", monospace;
    font-size: 12px;
    font-weight: bold;
}
QPushButton#SamKeyPrimary:hover {
    background-color: #20523c;
    border: 1px solid #5ef2a0;
}

QPushButton#SamKeyWarn {
    background-color: rgba(54, 24, 31, 210);
    color: #ff9fa9;
    border: 1px solid #753442;
    border-radius: 8px;
    min-height: 32px;
    padding: 2px 8px;
    font-family: "Consolas", "Segoe UI", monospace;
    font-size: 11px;
    font-weight: bold;
}
QPushButton#SamKeyWarn:hover {
    background-color: #541f2b;
    border: 1px solid #c94e63;
}
"""


def add_elevation(widget, blur=24, alpha=90):
    """Apply subtle soft elevation shadow to modern glass cards."""
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, 6)
    eff.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)


class ToggleSwitch(QWidget):
    """Modern smooth pill toggle switch (replaces boxy legacy checkboxes)."""

    def __init__(self, text='', checked=False, on_change=None, parent=None):
        super().__init__(parent)
        self._checked = bool(checked)
        self._on_change = on_change
        self._pos = 1.0 if self._checked else 0.0
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(10)

        self.track = _SwitchTrack(self)
        self.lbl = QLabel(text)
        self.lbl.setStyleSheet('color:#b8cbd5;font-size:12px;')
        lay.addWidget(self.track)
        lay.addWidget(self.lbl, 1)

        self._anim = QPropertyAnimation(self, b'thumb_pos', self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def get_thumb_pos(self):
        return self._pos

    def set_thumb_pos(self, val):
        self._pos = float(val)
        self.track.update()

    thumb_pos = pyqtProperty(float, fget=get_thumb_pos, fset=set_thumb_pos)

    def isChecked(self):
        return self._checked

    def setChecked(self, state):
        state = bool(state)
        if state == self._checked:
            return
        self._checked = state
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if state else 0.0)
        self._anim.start()

    def setText(self, text):
        self.lbl.setText(text)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
            if self._on_change:
                self._on_change(self._checked)
        super().mousePressEvent(event)


class _SwitchTrack(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setFixedSize(38, 21)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        t = self.owner.get_thumb_pos()

        # Interpolate track background from slate #1c2b33 to emerald #26c982
        bg_r = int(28 + (38 - 28) * t)
        bg_g = int(43 + (201 - 43) * t)
        bg_b = int(51 + (130 - 51) * t)
        p.setPen(QPen(QColor(50 + int(30 * t), 76 + int(150 * t), 90 + int(60 * t)), 1))
        p.setBrush(QColor(bg_r, bg_g, bg_b))
        p.drawRoundedRect(1, 1, 36, 19, 9.5, 9.5)

        knob_x = 3 + t * 17
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#f4faf7') if t > 0.4 else QColor('#9fb3bf'))
        p.drawEllipse(QRectF(knob_x, 3, 15, 15))


class MatrixRainWidget(QWidget):
    """Deep midnight-obsidian backdrop with ambient emerald aura & subtle Matrix rain."""

    GLYPHS = '010101ABCDEF<>◈'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fx_enabled = True
        self.drops = []
        self._col_step = 30
        self._font = QFont('Consolas', 10)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def set_fx_enabled(self, enabled):
        self.fx_enabled = bool(enabled)
        if self.fx_enabled and not self._timer.isActive():
            self._timer.start(50)
        elif not self.fx_enabled and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _init_drops(self):
        w = max(self.width(), 960)
        h = max(self.height(), 640)
        cols = max(1, w // self._col_step)
        while len(self.drops) < cols:
            x = len(self.drops) * self._col_step + 12
            self.drops.append({
                'x': x,
                'y': random.uniform(-h, h),
                'speed': random.uniform(2.5, 5.8),
                'chars': [random.choice(self.GLYPHS) for _ in range(6)],
            })
        self.drops = self.drops[:cols]

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._init_drops()

    def _tick(self):
        if not self.fx_enabled:
            return
        h = max(self.height(), 640)
        for d in self.drops:
            d['y'] += d['speed']
            if random.random() < 0.12:
                idx = random.randrange(len(d['chars']))
                d['chars'][idx] = random.choice(self.GLYPHS)
            if d['y'] - 110 > h:
                d['y'] = random.uniform(-140, -20)
                d['speed'] = random.uniform(2.5, 5.8)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect()

        # Deep modern slate-obsidian canvas
        p.fillRect(rect, QColor(8, 12, 16))

        # Top-right emerald ambient aura
        g1 = QRadialGradient(QPointF(rect.width() * 0.85, -rect.height() * 0.05), max(rect.width(), rect.height()) * 0.55)
        g1.setColorAt(0.0, QColor(20, 74, 53, 105))
        g1.setColorAt(1.0, QColor(8, 12, 16, 0))
        p.fillRect(rect, QBrush(g1))

        # Bottom-left deep teal aura
        g2 = QRadialGradient(QPointF(rect.width() * 0.12, rect.height() * 0.92), max(rect.width(), rect.height()) * 0.45)
        g2.setColorAt(0.0, QColor(14, 48, 60, 85))
        g2.setColorAt(1.0, QColor(8, 12, 16, 0))
        p.fillRect(rect, QBrush(g2))

        if self.fx_enabled:
            if not self.drops:
                self._init_drops()
            p.setFont(self._font)
            alphas = [34, 26, 19, 13, 8, 4]
            for d in self.drops:
                y = d['y']
                for j, ch in enumerate(d['chars']):
                    yy = y - j * 17
                    if -10 <= yy <= rect.height() + 10:
                        if j == 0:
                            p.setPen(QColor(135, 255, 180, alphas[0]))
                        else:
                            p.setPen(QColor(52, 210, 140, alphas[min(j, len(alphas) - 1)]))
                        p.drawText(int(d['x']), int(yy), ch)

        win = self.window()
        if not (win and win.isMaximized()):
            p.setPen(QPen(QColor(34, 56, 66), 1))
            p.drawRect(0, 0, rect.width() - 1, rect.height() - 1)


class StatusPill(QFrame):
    """Modern status badge with pulsing LED indicator."""

    def __init__(self, text='Disconnected', parent=None):
        super().__init__(parent)
        self._color = QColor('#64776a')
        self._pulse = False
        self._alpha = 255
        self._dir = -18
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 12, 4)
        lay.setSpacing(7)
        self.dot = _PulseDot(self)
        self.lbl = QLabel(text)
        self.lbl.setFont(QFont('Segoe UI', 10, QFont.DemiBold))
        self.lbl.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        lay.addWidget(self.dot)
        lay.addWidget(self.lbl)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.set_state('DISCONNECTED', text)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(70)

    def set_state(self, kind, text):
        palette = {
            'DISCONNECTED': ('#6e828f', '#9bb0bd', '#22323c', '#10181e', False),
            'CONNECTING': ('#ffd166', '#ffd985', '#6b5321', '#211b0f', True),
            'CONNECTED': ('#3ee68f', '#7dffb5', '#27684b', '#0e231b', True),
            'VPN': ('#5bff9d', '#8fffbd', '#2d7a58', '#0f281e', True),
            'HOTSPOT': ('#a3ff70', '#c7ff99', '#4a7a33', '#162611', True),
            'ETHERNET': ('#45f0ff', '#9bf7ff', '#287a85', '#0e2529', True),
            'ERROR': ('#ff6b7d', '#ff9aa6', '#7a2d38', '#261015', False),
        }
        dot_c, txt_c, border_c, bg_c, pulse = palette.get(kind, palette['DISCONNECTED'])
        self._color = QColor(dot_c)
        self._pulse = pulse
        self.dot.set_color(self._color)
        self.lbl.setText(text)
        self.lbl.setStyleSheet(f'color:{txt_c};background:transparent;border:none;')
        self.setStyleSheet(f'QFrame{{background:{bg_c};border:1px solid {border_c};border-radius:13px;}}')
        if not pulse:
            self.dot.set_alpha(255)

    def _step(self):
        if not self._pulse:
            return
        self._alpha += self._dir
        if self._alpha <= 95:
            self._alpha = 95
            self._dir = 18
        elif self._alpha >= 255:
            self._alpha = 255
            self._dir = -18
        self.dot.set_alpha(self._alpha)


class _PulseDot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(11, 11)
        self._color = QColor('#64776a')
        self._alpha = 255

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

    def set_alpha(self, alpha):
        self._alpha = alpha
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        glow = QColor(self._color)
        glow.setAlpha(int(self._alpha * 0.35))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(0, 0, 11, 11)
        core = QColor(self._color)
        core.setAlpha(self._alpha)
        p.setBrush(core)
        p.drawEllipse(2, 2, 7, 7)


class ScannerBar(QFrame):
    """Smooth modern activity progress beam shown during async SSH/network tasks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVisible(False)
        self.setFixedHeight(48)
        self.setStyleSheet(
            'QFrame{background:rgba(15, 29, 25, 235);'
            'border:1px solid #255a45;border-radius:10px;}'
        )
        self._phase = 0.0
        self._title = 'Executing Operation'
        self._tag = 'WORKING'
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self, title, tag):
        self._title = title
        self._tag = tag
        self.setVisible(True)
        if not self._timer.isActive():
            self._timer.start(30)

    def stop(self):
        self._timer.stop()
        self.setVisible(False)

    def _tick(self):
        self._phase = (self._phase + 0.03) % 1.0
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()

        p.setFont(QFont('Segoe UI', 10, QFont.DemiBold))
        p.setPen(QColor('#9bf8c6'))
        p.drawText(14, 21, self._title)

        p.setFont(QFont('Consolas', 8, QFont.Bold))
        p.setPen(QColor('#649680'))
        p.drawText(QRectF(0, 9, w - 14, 16), Qt.AlignRight | Qt.AlignVCenter, self._tag)

        track = QRectF(14, 32, w - 28, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#162b26'))
        p.drawRoundedRect(track, 2, 2)

        beam_w = max(56.0, (w - 28) * 0.30)
        x_pos = 14 + ((w - 28 - beam_w) * (0.5 - 0.5 * math.cos(self._phase * 2 * math.pi)))
        grad = QLinearGradient(x_pos, 0, x_pos + beam_w, 0)
        grad.setColorAt(0.0, QColor('#22d68a'))
        grad.setColorAt(0.5, QColor('#7dffb5'))
        grad.setColorAt(1.0, QColor('#22d68a'))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(x_pos, 32, beam_w, 4), 2, 2)


class NeonTerminal(QFrame):
    """Syntax-colored live terminal widget with copy/save/clear actions."""

    def __init__(self, on_save=None, parent=None):
        super().__init__(parent)
        self.setObjectName('TerminalShell')
        add_elevation(self, blur=26, alpha=105)
        self._on_save = on_save

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QFrame()
        bar.setObjectName('TermHeader')
        bar.setFixedHeight(44)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(14, 0, 12, 0)
        bl.setSpacing(7)

        for color in ('#3ee68f', '#f4bd4f', '#3a4b55'):
            dot = QLabel('●')
            dot.setStyleSheet(f'color:{color};font-size:10px;background:transparent;')
            bl.addWidget(dot)

        self.title_lbl = QLabel('Live System Console')
        self.title_lbl.setStyleSheet('color:#b9ccd6;font-size:12px;font-weight:600;margin-left:6px;')
        bl.addWidget(self.title_lbl)
        bl.addStretch(1)

        self.copy_btn = QPushButton('Copy')
        self.copy_btn.setObjectName('MiniBtn')
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.clicked.connect(self.copy_all)

        self.save_btn = QPushButton('Save Log')
        self.save_btn.setObjectName('MiniBtn')
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self._handle_save)

        self.clear_btn = QPushButton('Clear')
        self.clear_btn.setObjectName('MiniBtn')
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear)

        bl.addWidget(self.copy_btn)
        bl.addWidget(self.save_btn)
        bl.addWidget(self.clear_btn)
        root.addWidget(bar)

        self.edit = QTextEdit()
        self.edit.setObjectName('TerminalView')
        self.edit.setReadOnly(True)
        self.edit.setLineWrapMode(QTextEdit.WidgetWidth)
        self.edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.edit, 1)

        self.scanner = ScannerBar(self)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(12, 0, 12, 10)
        wrap.addWidget(self.scanner)
        root.addLayout(wrap)

    def set_labels(self, title, copy_txt, save_txt, clear_txt):
        self.title_lbl.setText(title)
        self.copy_btn.setText(copy_txt)
        self.save_btn.setText(save_txt)
        self.clear_btn.setText(clear_txt)

    def append_line(self, text, kind=None):
        s = str(text)
        if kind is None:
            if s.startswith('$ '):
                kind = 'cmd'
            elif s.startswith('[!]') or 'AUTH_FAILED' in s or 'fatal error' in s.lower():
                kind = 'bad'
            elif s.startswith('[+]') or 'Initialization Sequence Completed' in s:
                kind = 'good'
            elif s.startswith('[CONF]') or s.startswith('[VPN]') or s.startswith('[NEON]'):
                kind = 'vpn'
            elif s.startswith('─') or s.startswith('//'):
                kind = 'dim'
            else:
                kind = 'normal'

        colors = {
            'cmd': '#e4ff82',
            'good': '#5ef2a0',
            'bad': '#ff8292',
            'vpn': '#6be8d4',
            'dim': '#617a87',
            'normal': '#b8e8cb',
        }
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colors.get(kind, '#b8e8cb')))
        if kind in ('cmd', 'good', 'bad'):
            fmt.setFontWeight(QFont.Bold)

        cursor = self.edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(s + '\n', fmt)

        doc = self.edit.document()
        if doc.blockCount() > 1600:
            c2 = QTextCursor(doc)
            c2.movePosition(QTextCursor.Start)
            for _ in range(doc.blockCount() - 1200):
                c2.select(QTextCursor.BlockUnderCursor)
                c2.removeSelectedText()
                c2.deleteChar()

        sb = self.edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear(self):
        self.edit.clear()

    def copy_all(self):
        QApplication.clipboard().setText(self.edit.toPlainText())

    def _handle_save(self):
        if self._on_save:
            self._on_save(self.edit.toPlainText())


class KpiCard(QFrame):
    """Elevated telemetry metric card."""

    def __init__(self, label, value, sub='', parent=None):
        super().__init__(parent)
        self.setObjectName('KpiCard')
        add_elevation(self, blur=20, alpha=75)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(15, 12, 15, 12)
        lay.setSpacing(4)
        self.lbl = QLabel(label)
        self.lbl.setStyleSheet('color:#78909e;font-size:10px;font-weight:bold;letter-spacing:0.8px;')
        self.val = QLabel(value)
        self.val.setStyleSheet('color:#edf5fa;font-family:"Consolas",monospace;font-size:15px;font-weight:bold;')
        self.sub = QLabel(sub)
        self.sub.setStyleSheet('color:#6b828f;font-size:11px;')
        lay.addWidget(self.lbl)
        lay.addWidget(self.val)
        lay.addWidget(self.sub)

    def set_data(self, value, accent='#edf5fa', sub=None):
        self.val.setText(value)
        self.val.setStyleSheet(f'color:{accent};font-family:"Consolas",monospace;font-size:15px;font-weight:bold;')
        if sub is not None:
            self.sub.setText(sub)


class ToastBanner(QFrame):
    """Floating bottom-right toast notification."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVisible(False)
        add_elevation(self, blur=28, alpha=140)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(15, 11, 15, 11)
        lay.setSpacing(10)
        self.icon = QLabel('◈')
        self.icon.setFont(QFont('Consolas', 12, QFont.Bold))
        self.msg = QLabel('')
        self.msg.setWordWrap(True)
        self.msg.setMaximumWidth(380)
        self.msg.setStyleSheet('color:#eef6fa;font-size:12px;')
        lay.addWidget(self.icon)
        lay.addWidget(self.msg)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_toast(self, text, level='info', ms=3800):
        styles = {
            'info': ('#0f241c', '#2d7a58', '#5ef2a0', '◈'),
            'warn': ('#261e10', '#7c6125', '#ffd166', '⚠'),
            'error': ('#2b1218', '#8f3344', '#ff8292', '!'),
        }
        bg, border, fg, glyph = styles.get(level, styles['info'])
        self.setStyleSheet(f'QFrame{{background-color:{bg};border:1px solid {border};border-radius:11px;}}')
        self.icon.setText(glyph)
        self.icon.setStyleSheet(f'color:{fg};background:transparent;')
        self.msg.setText(text)
        self.adjustSize()
        if self.parent():
            pw = self.parent().width()
            ph = self.parent().height()
            self.move(max(16, pw - self.width() - 24), max(72, ph - self.height() - 36))
        self.setVisible(True)
        self.raise_()
        self._timer.start(ms)


class ConfirmDialog(QDialog):
    """Modern elevated modal confirmation dialog."""

    def __init__(self, parent, title, body, ok_text='Confirm', cancel_text='Cancel', danger=False):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMaximumWidth(520)

        border_col = '#8c3443' if danger else '#2c6d52'
        self.setStyleSheet(
            f'QDialog{{background-color:#101920;border:1px solid {border_col};border-radius:14px;}}'
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(12)

        hdr = QLabel(title)
        hdr.setWordWrap(True)
        hdr.setStyleSheet(
            f'color:{"#ff95a3" if danger else "#6effad"};font-size:15px;font-weight:bold;'
        )
        lay.addWidget(hdr)

        txt = QLabel(body)
        txt.setWordWrap(True)
        txt.setStyleSheet('color:#d4e3eb;font-size:12px;')
        lay.addWidget(txt)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        btns.addStretch(1)

        cancel_btn = QPushButton(cancel_text)
        cancel_btn.setObjectName('GhostBtn')
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)

        ok_btn = QPushButton(ok_text)
        ok_btn.setObjectName('DangerBtn' if danger else 'PrimaryBtn')
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.clicked.connect(self.accept)

        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        lay.addLayout(btns)


class _WaveformWidget(QWidget):
    """Animated cyber-telemetry dual-channel signal oscilloscope (pure visual FX)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(130)
        self._phase = 0.0
        self._samples_a = [0.0] * 64
        self._samples_b = [0.0] * 64

    def step(self):
        self._phase += 0.24
        va = 0.55 * math.sin(self._phase) + 0.28 * math.sin(self._phase * 2.7) + random.uniform(-0.16, 0.16)
        vb = 0.45 * math.cos(self._phase * 0.85) + 0.30 * math.sin(self._phase * 3.4) + random.uniform(-0.14, 0.14)
        self._samples_a = self._samples_a[1:] + [max(-1.0, min(1.0, va))]
        self._samples_b = self._samples_b[1:] + [max(-1.0, min(1.0, vb))]
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()

        p.fillRect(self.rect(), QColor(8, 14, 18))
        p.setPen(QPen(QColor(24, 46, 56, 150), 1))
        for gx in range(28, w, 36):
            p.drawLine(gx, 0, gx, h)
        for gy in range(22, h, 26):
            p.drawLine(0, gy, w, gy)
        p.setPen(QPen(QColor(35, 80, 74, 180), 1, Qt.DashLine))
        p.drawLine(0, h // 2, w, h // 2)

        def draw_wave(samples, color, width):
            p.setPen(QPen(color, width))
            pts = []
            n = max(1, len(samples) - 1)
            for i, val in enumerate(samples):
                x = (i / n) * (w - 16) + 8
                y = (h * 0.5) - val * (h * 0.36)
                pts.append(QPointF(x, y))
            for i in range(len(pts) - 1):
                p.drawLine(pts[i], pts[i + 1])

        draw_wave(self._samples_b, QColor('#29d8ff'), 1.6)
        draw_wave(self._samples_a, QColor('#3ef297'), 2.2)

        # Scanning vertical radar cursor
        cx = int(((self._phase * 18) % max(20, w - 20)) + 10)
        p.setPen(QPen(QColor(94, 242, 160, 120), 1))
        p.drawLine(cx, 6, cx, h - 6)


class SystemTuneDialog(QDialog):
    """High-tech visual calibration & telemetry suite (zero real side effects)."""

    _STAGES = [
        ('Initializing Ring-0 Bus & DMA Memory Map', 'Инициализация шины DMA и карты памяти'),
        ('Probing RSA-2048 Keychain & Entropy Pool', 'Калибровка крипто-пула и ключей RSA-2048'),
        ('Synchronizing webOS Luna IPC Timing Matrix', 'Синхронизация таймингов шины Luna IPC'),
        ('Verifying SoC NVRAM Sector Parity Checksums', 'Верификация контрольных сумм секторов NVRAM'),
        ('Calibrating Kernel Packet & Socket Buffers', 'Калибровка буферов ядра и сокетных фреймов'),
        ('Committing Cryptographic Integrity Lock', 'Фиксация системной конфигурации и верификация'),
    ]

    _LOG_TEMPLATES = [
        '[MEM] Mapping physical DMA region 0xFFFF8800{hex4}000 .. LOCKED',
        '[BUS] ACPI PCIe lane sync -> 8.0 GT/s | jitter={lat}ms | OK',
        '[SEC] Entropy pool reseeded (4096-bit ChaCha20) -> CRC32=0x{hex4}{hex4}',
        '[IPC] Luna frame descriptor #{num} ACK in {lat}ms (priority=REALTIME)',
        '[NVRAM] Parity sector 0x00{hex4} checksum verified :: 0 errors',
        '[NET] Tuning TX/RX ring buffer depth=4096 MTU=1500 -> OPTIMAL',
        '[CPU] Thread #{tid} MSR telemetry locked | IRQ affinity=0x0F',
        '[SYS] Deep kernel integrity digest SHA-256:{hex4}{hex4}..{hex4} PASS',
    ]

    _SAMSUNG_STAGES = [
        ('Initializing Tizen SoC Bus & DMA Memory Map', 'Инициализация шины Tizen SoC и карты DMA'),
        ('Probing Knox Vault & RSA-2048 Keychain', 'Калибровка Knox Vault и ключей RSA-2048'),
        ('Synchronizing MRT Option & Local Set Matrix', 'Синхронизация таблицы MRT Option и Local Set'),
        ('Verifying Smart Hub NVRAM Sector Checksums', 'Верификация секторов NVRAM Smart Hub'),
        ('Calibrating SDB 26101 & WSS Frame Buffers', 'Калибровка SDB 26101 и фреймов WSS'),
        ('Committing Tizen Cryptographic Integrity Lock', 'Фиксация системной конфигурации и целостности'),
    ]

    _SAMSUNG_LOG_TEMPLATES = [
        '[SOC] Mapping Samsung Tizen DMA region 0xFFFF8800{hex4}000 .. LOCKED',
        '[BUS] OneConnect PCIe / LAN sync -> 8.0 GT/s | jitter={lat}ms | OK',
        '[KNOX] RSA-2048 entropy reseeded -> SHA512=0x{hex4}{hex4}',
        '[MRT] Service Menu frame #{num} ACK in {lat}ms (WSS/SDB)',
        '[NVRAM] Smart Hub sector 0x00{hex4} checksum verified :: 0 errors',
        '[SDB] Tuning TCP 26101 TX/RX ring depth=4096 MTU=1500 -> OPTIMAL',
        '[CPU] Core #{tid} Tizen telemetry locked | IRQ affinity=0x0F',
        '[SYS] Tizen integrity digest SHA-512:{hex4}{hex4}..{hex4} PASS',
    ]

    def __init__(self, parent=None, lang='ru', mode='lg'):
        super().__init__(parent)
        self.lang = 'ru' if lang == 'ru' else 'en'
        self.mode = 'samsung' if mode == 'samsung' else 'lg'
        self._stages = self._SAMSUNG_STAGES if self.mode == 'samsung' else self._STAGES
        self._log_templates = self._SAMSUNG_LOG_TEMPLATES if self.mode == 'samsung' else self._LOG_TEMPLATES
        self.setObjectName('TuneDialog')
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self.resize(1060, 660)
        self.setMinimumSize(940, 580)
        self.setStyleSheet(
            'QDialog#TuneDialog{background-color:#080e13;border:1px solid #245a46;border-radius:16px;}'
            'QFrame#TuneHdr{background-color:#0c151b;border:1px solid #1b313d;border-radius:12px;}'
            'QFrame#TuneCard{background-color:#0b1319;border:1px solid #1b2d37;border-radius:12px;}'
            'QDialog#TuneDialog QLabel{border:none;background:transparent;}'
        )
        self._tick_count = 0
        self._progress = 0
        self._stage_rows = []
        self._spinner_chars = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # Header
        hdr = QFrame()
        hdr.setObjectName('TuneHdr')
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(16, 11, 14, 11)
        hl.setSpacing(10)

        self.live_dot = QLabel('●')
        self.live_dot.setStyleSheet('color:#3ef297;font-size:13px;')
        hl.addWidget(self.live_dot)

        title_txt = (
            'NEON // ГЛУБОКАЯ КАЛИБРОВКА И НАСТРОЙКА СИСТЕМЫ'
            if self.lang == 'ru'
            else 'NEON // DEEP KERNEL & SYSTEM CALIBRATION SUITE'
        )
        title_lbl = QLabel(title_txt)
        title_lbl.setStyleSheet('color:#eaf6fb;font-family:"Consolas",monospace;font-size:13px;font-weight:bold;')
        hl.addWidget(title_lbl, 1)

        enc_pill = QLabel('RING-0 · AES-256 · LIVE')
        enc_pill.setStyleSheet(
            'background:#102920;color:#5ef2a0;border:1px solid #286a4e;border-radius:10px;'
            'padding:4px 10px;font-family:"Consolas",monospace;font-size:10px;font-weight:bold;'
        )
        hl.addWidget(enc_pill)

        close_x = QPushButton('✕')
        close_x.setObjectName('WinCloseBtn')
        close_x.setCursor(Qt.PointingHandCursor)
        close_x.clicked.connect(self.accept)
        hl.addWidget(close_x)
        root.addWidget(hdr)

        # Progress bar strip
        prog_box = QFrame()
        prog_box.setObjectName('TuneCard')
        pl = QVBoxLayout(prog_box)
        pl.setContentsMargins(14, 9, 14, 10)
        pl.setSpacing(6)
        top_p = QHBoxLayout()
        self.phase_lbl = QLabel('')
        self.phase_lbl.setStyleSheet('color:#7de8ff;font-family:"Consolas",monospace;font-size:11px;font-weight:bold;')
        self.pct_lbl = QLabel('0%')
        self.pct_lbl.setStyleSheet('color:#5ef2a0;font-family:"Consolas",monospace;font-size:13px;font-weight:bold;')
        top_p.addWidget(self.phase_lbl, 1)
        top_p.addWidget(self.pct_lbl)
        pl.addLayout(top_p)

        self._bar_bg = QFrame()
        self._bar_bg.setFixedHeight(8)
        self._bar_bg.setStyleSheet('background:#132028;border-radius:4px;border:none;')
        self._bar_fill = QFrame(self._bar_bg)
        self._bar_fill.setGeometry(0, 0, 4, 8)
        self._bar_fill.setStyleSheet(
            'background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #1ad992,stop:1 #36e2ff);'
            'border-radius:4px;border:none;'
        )
        pl.addWidget(self._bar_bg)
        root.addWidget(prog_box)

        # Split middle area
        body = QHBoxLayout()
        body.setSpacing(12)

        # Left column: 6 calibration stages + live memory dump
        left_col = QVBoxLayout()
        left_col.setSpacing(10)

        stages_frame = QFrame()
        stages_frame.setObjectName('TuneCard')
        sl = QVBoxLayout(stages_frame)
        sl.setContentsMargins(14, 12, 14, 12)
        sl.setSpacing(7)
        cap1 = QLabel('ПОДСИСТЕМЫ И ДИАГНОСТИКА' if self.lang == 'ru' else 'CALIBRATION PIPELINE')
        cap1.setStyleSheet('color:#6b8696;font-size:10px;font-weight:bold;letter-spacing:1px;')
        sl.addWidget(cap1)

        for en_t, ru_t in self._stages:
            row = QHBoxLayout()
            row.setSpacing(8)
            ico = QLabel('○')
            ico.setFixedWidth(18)
            ico.setStyleSheet('color:#4f6875;font-family:"Consolas",monospace;font-size:12px;font-weight:bold;')
            txt = QLabel(ru_t if self.lang == 'ru' else en_t)
            txt.setStyleSheet('color:#8ba3b0;font-size:12px;')
            stat = QLabel('WAIT')
            stat.setStyleSheet('color:#536b78;font-family:"Consolas",monospace;font-size:10px;font-weight:bold;')
            row.addWidget(ico)
            row.addWidget(txt, 1)
            row.addWidget(stat)
            sl.addLayout(row)
            self._stage_rows.append((ico, txt, stat))
        left_col.addWidget(stages_frame)

        hex_frame = QFrame()
        hex_frame.setObjectName('TuneCard')
        xl = QVBoxLayout(hex_frame)
        xl.setContentsMargins(14, 10, 14, 10)
        xl.setSpacing(6)
        cap2 = QLabel('ДАМП РЕГИСТРОВ И ПАМЯТИ (HEX)' if self.lang == 'ru' else 'MEMORY REGISTER STREAM (HEX)')
        cap2.setStyleSheet('color:#6b8696;font-size:10px;font-weight:bold;letter-spacing:1px;')
        xl.addWidget(cap2)
        self.reg_lbl = QLabel('RAX=0000000000000000  CR3=000000014A2000  IRQ=16')
        self.reg_lbl.setStyleSheet('color:#36e2ff;font-family:"Consolas",monospace;font-size:10px;')
        xl.addWidget(self.reg_lbl)
        self.hex_view = QTextEdit()
        self.hex_view.setReadOnly(True)
        self.hex_view.setStyleSheet(
            'background:#060b0f;color:#52d68d;border:1px solid #14242d;border-radius:8px;'
            'font-family:"Consolas",monospace;font-size:10px;padding:6px;'
        )
        xl.addWidget(self.hex_view, 1)
        left_col.addWidget(hex_frame, 1)

        body.addLayout(left_col, 5)

        # Right column: Live Waveform + Scrolling Kernel Console
        right_col = QVBoxLayout()
        right_col.setSpacing(10)

        wave_frame = QFrame()
        wave_frame.setObjectName('TuneCard')
        wl = QVBoxLayout(wave_frame)
        wl.setContentsMargins(14, 10, 14, 12)
        wl.setSpacing(8)
        wh = QHBoxLayout()
        cap3 = QLabel('ТЕЛЕМЕТРИЯ КАНАЛА' if self.lang == 'ru' else 'SIGNAL TELEMETRY')
        cap3.setStyleSheet('color:#6b8696;font-size:10px;font-weight:bold;letter-spacing:1px;')
        self.metrics_lbl = QLabel('ENTROPY 4096b · LAT 0.14ms · CRC PASS')
        self.metrics_lbl.setStyleSheet('color:#5ef2a0;font-family:"Consolas",monospace;font-size:10px;')
        wh.addWidget(cap3)
        wh.addStretch(1)
        wh.addWidget(self.metrics_lbl)
        wl.addLayout(wh)

        self.waveform = _WaveformWidget(self)
        wl.addWidget(self.waveform)
        right_col.addWidget(wave_frame)

        log_frame = QFrame()
        log_frame.setObjectName('TuneCard')
        ll = QVBoxLayout(log_frame)
        ll.setContentsMargins(14, 10, 14, 10)
        ll.setSpacing(6)
        cap4 = QLabel('ПОТОК ИНСТРУКЦИЙ ЯДРА' if self.lang == 'ru' else 'KERNEL TELEMETRY STREAM')
        cap4.setStyleSheet('color:#6b8696;font-size:10px;font-weight:bold;letter-spacing:1px;')
        ll.addWidget(cap4)
        self.stream_view = QTextEdit()
        self.stream_view.setReadOnly(True)
        self.stream_view.setStyleSheet(
            'background:#060b0f;color:#9df7c4;border:1px solid #14242d;border-radius:8px;'
            'font-family:"Consolas",monospace;font-size:11px;padding:8px;'
        )
        ll.addWidget(self.stream_view, 1)
        right_col.addWidget(log_frame, 1)

        body.addLayout(right_col, 6)
        root.addLayout(body, 1)

        # Bottom controls
        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.summary_lbl = QLabel('')
        self.summary_lbl.setStyleSheet('color:#7e98a8;font-size:11px;')
        foot.addWidget(self.summary_lbl, 1)

        rerun_btn = QPushButton('Повторить' if self.lang == 'ru' else 'Re-calibrate')
        rerun_btn.setObjectName('NormalBtn')
        rerun_btn.setCursor(Qt.PointingHandCursor)
        rerun_btn.clicked.connect(self.restart)

        done_btn = QPushButton('Закрыть' if self.lang == 'ru' else 'Close')
        done_btn.setObjectName('PrimaryBtn')
        done_btn.setCursor(Qt.PointingHandCursor)
        done_btn.clicked.connect(self.accept)

        foot.addWidget(rerun_btn)
        foot.addWidget(done_btn)
        root.addLayout(foot)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.restart()
        self._timer.start(65)

    def restart(self):
        self._tick_count = 0
        self._progress = 0
        self.hex_view.clear()
        self.stream_view.clear()
        self.stream_view.append('[SYS] Attaching secure diagnostic probe to kernel bus...')
        self.summary_lbl.setText(
            'Выполняется глубокая калибровка подсистем...'
            if self.lang == 'ru'
            else 'Running deep subsystem calibration...'
        )
        self._update_progress_bar()

    def _update_progress_bar(self):
        bw = max(20, self._bar_bg.width())
        fw = max(6, int((bw * self._progress) / 100.0))
        self._bar_fill.setGeometry(0, 0, fw, 8)
        self.pct_lbl.setText(f'{self._progress}%')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_progress_bar()

    def _tick(self):
        self._tick_count += 1
        self.waveform.step()

        # Blink dot subtly
        self.live_dot.setStyleSheet(
            f'color:{"#3ef297" if (self._tick_count // 5) % 2 == 0 else "#1a6e47"};font-size:13px;background:transparent;'
        )

        # Advance 0 -> 100% smoothly (~11 sec)
        if self._progress < 100 and self._tick_count % 2 == 0:
            self._progress = min(100, self._progress + 1)
        self._update_progress_bar()

        done_stages = min(len(self._stage_rows), (self._progress * len(self._stage_rows)) // 100)
        spin = self._spinner_chars[self._tick_count % len(self._spinner_chars)]
        for i, (ico, txt, stat) in enumerate(self._stage_rows):
            if i < done_stages:
                ico.setText('✓')
                ico.setStyleSheet('color:#5ef2a0;font-family:"Consolas",monospace;font-size:12px;font-weight:bold;')
                txt.setStyleSheet('color:#e2f5ea;font-size:12px;font-weight:600;')
                stat.setText('OK')
                stat.setStyleSheet('color:#5ef2a0;font-family:"Consolas",monospace;font-size:10px;font-weight:bold;')
            elif i == done_stages and self._progress < 100:
                ico.setText(spin)
                ico.setStyleSheet('color:#36e2ff;font-family:"Consolas",monospace;font-size:12px;font-weight:bold;')
                txt.setStyleSheet('color:#ffffff;font-size:12px;font-weight:600;')
                stat.setText('SYNC')
                stat.setStyleSheet('color:#36e2ff;font-family:"Consolas",monospace;font-size:10px;font-weight:bold;')
            else:
                ico.setText('○')
                ico.setStyleSheet('color:#4f6875;font-family:"Consolas",monospace;font-size:12px;')
                txt.setStyleSheet('color:#7d95a3;font-size:12px;')
                stat.setText('WAIT')
                stat.setStyleSheet('color:#536b78;font-family:"Consolas",monospace;font-size:10px;')

        if self._progress >= 100:
            self.phase_lbl.setText(
                'STATUS: ВСЕ ПОДСИСТЕМЫ ОТКАЛИБРОВАНЫ · КАНАЛ СТАБИЛЕН'
                if self.lang == 'ru'
                else 'STATUS: ALL SUBSYSTEMS CALIBRATED · TELEMETRY LOCKED'
            )
            self.summary_lbl.setText(
                'Готово: 6/6 модулей проверены и синхронизированы.'
                if self.lang == 'ru'
                else 'Complete: 6/6 modules verified and locked.'
            )
        else:
            stage_idx = min(len(self._stages) - 1, done_stages)
            st_name = self._stages[stage_idx][1 if self.lang == 'ru' else 0]
            self.phase_lbl.setText(f'STAGE {stage_idx + 1}/6 :: {st_name}')

        # Update register/metrics counters
        hx = lambda n: ''.join(random.choice('0123456789ABCDEF') for _ in range(n))
        lat = f'{random.uniform(0.08, 0.29):.2f}'
        self.reg_lbl.setText(f'RAX=0x{hx(12)}  CR3=0x{hx(8)}  IRQ={random.randint(12, 63)}')
        self.metrics_lbl.setText(f'ENTROPY {random.randint(4080, 4096)}b · LAT {lat}ms · PKT {1024 + self._tick_count * 7}')

        # Append hex row & kernel stream row every few ticks
        addr = 0x8800A000 + (self._tick_count * 16) % 0xFFFF
        bytes_str = ' '.join(hx(2) for _ in range(8))
        self.hex_view.append(f'0x{addr:08X}: {bytes_str}')
        if self.hex_view.document().blockCount() > 140:
            c = self.hex_view.textCursor()
            c.movePosition(QTextCursor.Start)
            c.select(QTextCursor.BlockUnderCursor)
            c.removeSelectedText()
            c.deleteChar()
        self.hex_view.verticalScrollBar().setValue(self.hex_view.verticalScrollBar().maximum())

        if self._tick_count % 2 == 0:
            tmpl = random.choice(self._log_templates)
            line = tmpl.format(hex4=hx(4), lat=lat, num=1000 + self._tick_count * 3, tid=random.randint(1, 16))
            self.stream_view.append(line)
            if self.stream_view.document().blockCount() > 180:
                c = self.stream_view.textCursor()
                c.movePosition(QTextCursor.Start)
                c.select(QTextCursor.BlockUnderCursor)
                c.removeSelectedText()
                c.deleteChar()
            self.stream_view.verticalScrollBar().setValue(self.stream_view.verticalScrollBar().maximum())

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)

