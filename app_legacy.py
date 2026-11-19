"""Modern 2026 dark-glass PyQt5 application for Windows 8.1, 10 & 11."""
import ctypes
import os
import platform
import queue
import sys
import threading

from PyQt5.QtCore import Qt, QTimer, QRectF, QRegExp, QSize
from PyQt5.QtGui import QColor, QFont, QIcon, QPixmap, QRegExpValidator
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from alemtv_app import ALEMTV_PORTALS, ALEMTV_DEFAULT_PORTAL
from core import Api, DNS_PRESETS, resource
from samsung_tv import SAMSUNG_LOCAL_SETS
from ui_qt import (
    ConfirmDialog,
    KpiCard,
    MatrixRainWidget,
    NeonTerminal,
    REGIONS,
    StatusPill,
    SystemTuneDialog,
    TR,
    ToastBanner,
    ToggleSwitch,
    add_elevation,
    build_neon_qss,
)


def _setup_windows_environment():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    QApplication.setAttribute(Qt.AA_UseSoftwareOpenGL, True)
    if platform.system() == 'Windows':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('NEON.LGWebOS.Console.2')
        except Exception:
            pass


class NeonWindow(QMainWindow):
    RESIZE_MARGIN = 7

    def __init__(self, api=None):
        super().__init__()
        self.api = api or Api()
        self.settings = self.api.load_settings()
        self.lang = self.settings.get('language', 'ru')
        if self.lang not in TR:
            self.lang = 'ru'

        self.events = queue.Queue()
        self.tv_connected = False
        self.sam_connected = False
        self.tv_busy = False
        self.sam_busy = False
        self.net_busy = False
        self.vpn_state = 'DISCONNECTED'
        self.adapter_map = {}
        self._sam_found_devices = []
        self.current_tab = 0
        self._drag_pos = None
        self._resize_edges = None
        self._resize_origin = None
        self._resize_geom = None
        self._force_exit = False

        self.setWindowTitle('NEON — Smart TV & Conf Console')
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowSystemMenuHint
        )
        self.setMinimumSize(1080, 640)
        self.resize(1280, 800)

        self._app_icon = self._load_icon()
        if not self._app_icon.isNull():
            self.setWindowIcon(self._app_icon)
            app = QApplication.instance()
            if app is not None:
                app.setWindowIcon(self._app_icon)

        self.backdrop = MatrixRainWidget(self)
        self.backdrop.setMouseTracking(True)
        self.setCentralWidget(self.backdrop)
        self.setStyleSheet(build_neon_qss(resource('assets/chevron_down.png')))

        # Outer horizontal split: Sidebar | Content Area
        self.outer_layout = QHBoxLayout(self.backdrop)
        self.outer_layout.setContentsMargins(1, 1, 1, 1)
        self.outer_layout.setSpacing(0)

        self._build_sidebar()

        right_shell = QWidget()
        right_shell.setObjectName('ScrollContent')
        self.right_layout = QVBoxLayout(right_shell)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_layout.setSpacing(0)

        self._build_header()
        self._build_pages()
        self._build_footer()

        self.outer_layout.addWidget(right_shell, 1)

        self.toast = ToastBanner(self.backdrop)
        self._init_tray()
        self._apply_translations()
        self._restore_saved_inputs()
        self._refresh_env_badge()
        self._apply_win32_icons()

        self.log('NEON CONSOLE v2.0  //  READY')
        self.log('──────────────────────────────────────────────────────────')
        win_info = self.api.windows_info()
        self.log(f'[+] Platform: {win_info["label"]} | Admin UAC: {"YES" if self.api.is_admin() else "NO"}')
        if not self.api.is_admin() and win_info['is_windows']:
            self.log('[!] Run as Administrator for Conf routes, Hosted Network & ICS.')

        self._pump_timer = QTimer(self)
        self._pump_timer.timeout.connect(self._pump)
        self._pump_timer.start(220)

        if win_info['is_windows']:
            QTimer.singleShot(150, self.refresh_profiles)
        QTimer.singleShot(250, self.run_diagnostics)

    def _load_icon(self):
        ico = QIcon()
        png_path = resource('assets/neon.png')
        ico_path = resource('assets/neon.ico')
        if os.path.isfile(ico_path):
            ico.addFile(ico_path)
        if os.path.isfile(png_path):
            pix = QPixmap(png_path)
            if not pix.isNull():
                for sz in (16, 24, 32, 48, 64, 128, 256):
                    ico.addPixmap(pix.scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        return ico

    def _apply_win32_icons(self):
        if platform.system() != 'Windows':
            return
        ico_path = resource('assets/neon.ico')
        if not os.path.isfile(ico_path):
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            WM_SETICON = 0x0080
            h_small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            h_big = user32.LoadImageW(None, ico_path, IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
            if h_small:
                user32.SendMessageW(hwnd, WM_SETICON, 0, h_small)
            if h_big:
                user32.SendMessageW(hwnd, WM_SETICON, 1, h_big)
        except Exception:
            pass

    def t(self, key):
        return TR.get(self.lang, TR['en']).get(key, key)

    # ------------------------------------------------------------------
    # Sidebar, Header & Footer
    # ------------------------------------------------------------------
    def _build_sidebar(self):
        self.sidebar = QFrame()
        self.sidebar.setObjectName('Sidebar')
        self.sidebar.setFixedWidth(248)
        self.sidebar.installEventFilter(self)

        lay = QVBoxLayout(self.sidebar)
        lay.setContentsMargins(16, 18, 16, 16)
        lay.setSpacing(14)

        # Brand Header
        brand_row = QHBoxLayout()
        brand_row.setSpacing(11)
        logo = QLabel()
        logo.setFixedSize(42, 42)
        png_path = resource('assets/neon.png')
        if os.path.isfile(png_path):
            logo.setPixmap(QPixmap(png_path).scaled(42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            logo.setText('N')
            logo.setAlignment(Qt.AlignCenter)
            logo.setStyleSheet('background:#133124;border:1px solid #2b664b;border-radius:10px;color:#72ffab;font-size:18px;font-weight:bold;')
        brand_row.addWidget(logo)

        bcol = QVBoxLayout()
        bcol.setSpacing(2)
        top_t = QHBoxLayout()
        top_t.setSpacing(6)
        self.brand_name = QLabel('NEON')
        self.brand_name.setObjectName('BrandName')
        self.ver_chip = QLabel('v2.0')
        self.ver_chip.setObjectName('VersionChip')
        top_t.addWidget(self.brand_name)
        top_t.addWidget(self.ver_chip)
        top_t.addStretch(1)
        self.brand_sub = QLabel('')
        self.brand_sub.setObjectName('BrandSub')
        bcol.addLayout(top_t)
        bcol.addWidget(self.brand_sub)
        brand_row.addLayout(bcol)
        lay.addLayout(brand_row)

        lay.addSpacing(4)

        # Navigation Buttons
        self.tab_buttons = []
        icons = ['▣', '◈', '◉', '✦', '☎']
        for idx, glyph in enumerate(icons):
            btn = QPushButton(glyph)
            btn.setObjectName('SideNavBtn')
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _chk=False, i=idx: self.switch_tab(i))
            lay.addWidget(btn)
            self.tab_buttons.append(btn)

        # Live Telemetry Mini Widget inside Sidebar
        tele = QFrame()
        tele.setObjectName('SideWidget')
        tl = QVBoxLayout(tele)
        tl.setContentsMargins(13, 12, 13, 12)
        tl.setSpacing(9)
        self.side_status_cap = QLabel()
        self.side_status_cap.setObjectName('SectionCap')
        tl.addWidget(self.side_status_cap)

        self.side_tv_lbl, self.side_tv_dot = self._make_telemetry_row(tl)
        self.side_sam_lbl, self.side_sam_dot = self._make_telemetry_row(tl)
        self.side_vpn_lbl, self.side_vpn_dot = self._make_telemetry_row(tl)
        self.side_hs_lbl, self.side_hs_dot = self._make_telemetry_row(tl)
        self.side_eth_lbl, self.side_eth_dot = self._make_telemetry_row(tl)
        lay.addWidget(tele)

        lay.addStretch(1)

        # FX Toggle Switch
        fx_card = QFrame()
        fx_card.setObjectName('SideWidget')
        fl = QVBoxLayout(fx_card)
        fl.setContentsMargins(13, 10, 13, 10)
        fl.setSpacing(10)
        self.fx_switch = ToggleSwitch('', checked=True, on_change=self._on_fx_toggled)
        fl.addWidget(self.fx_switch)

        lang_row = QHBoxLayout()
        lang_row.setSpacing(8)
        self.lang_btn = QPushButton('RU / EN')
        self.lang_btn.setObjectName('LangBtn')
        self.lang_btn.setCursor(Qt.PointingHandCursor)
        self.lang_btn.clicked.connect(self.toggle_language)
        lang_row.addWidget(self.lang_btn, 1)
        fl.addLayout(lang_row)
        lay.addWidget(fx_card)

        self.env_badge = QLabel('Windows · Checking')
        self.env_badge.setObjectName('EnvBadge')
        self.env_badge.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.env_badge)

        self.outer_layout.addWidget(self.sidebar)

    def _make_telemetry_row(self, parent_lay):
        row = QHBoxLayout()
        row.setSpacing(8)
        dot = QLabel('●')
        dot.setStyleSheet('color:#5a6e7a;font-size:9px;background:transparent;')
        lbl = QLabel('')
        lbl.setStyleSheet('color:#9ab0bd;font-size:12px;')
        row.addWidget(dot)
        row.addWidget(lbl, 1)
        parent_lay.addLayout(row)
        return lbl, dot

    def _build_header(self):
        self.topbar = QFrame()
        self.topbar.setObjectName('TopHeader')
        self.topbar.setFixedHeight(62)
        self.topbar.installEventFilter(self)

        lay = QHBoxLayout(self.topbar)
        lay.setContentsMargins(22, 10, 14, 10)
        lay.setSpacing(12)

        tcol = QVBoxLayout()
        tcol.setSpacing(1)
        self.page_title = QLabel('')
        self.page_title.setObjectName('PageTitle')
        self.page_sub = QLabel('')
        self.page_sub.setObjectName('PageSub')
        tcol.addWidget(self.page_title)
        tcol.addWidget(self.page_sub)
        lay.addLayout(tcol, 1)

        self.status_pill = StatusPill('Disconnected')
        lay.addWidget(self.status_pill)

        lay.addSpacing(4)

        min_btn = QPushButton('—')
        min_btn.setObjectName('WinBtn')
        min_btn.setCursor(Qt.PointingHandCursor)
        min_btn.clicked.connect(self.showMinimized)

        self.max_btn = QPushButton('□')
        self.max_btn.setObjectName('WinBtn')
        self.max_btn.setCursor(Qt.PointingHandCursor)
        self.max_btn.clicked.connect(self._toggle_maximize)

        close_btn = QPushButton('✕')
        close_btn.setObjectName('WinCloseBtn')
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)

        lay.addWidget(min_btn)
        lay.addWidget(self.max_btn)
        lay.addWidget(close_btn)

        self.right_layout.addWidget(self.topbar)

    def _make_card(self, title_attr, step_num):
        card = QFrame()
        card.setObjectName('GlassCard')
        add_elevation(card, blur=22, alpha=80)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 15)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(9)
        badge = QLabel(str(step_num))
        badge.setObjectName('StepBadge')
        badge.setAlignment(Qt.AlignCenter)
        t_lbl = QLabel('')
        t_lbl.setObjectName('CardTitle')
        t_lbl.setWordWrap(True)
        head.addWidget(badge, 0, Qt.AlignTop)
        head.addWidget(t_lbl, 1)
        lay.addLayout(head)
        setattr(self, title_attr, t_lbl)
        return card, lay

    def _wrap_scroll(self, widget, fixed_width=None):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName('ScrollContent')
        scroll.setWidget(widget)
        if fixed_width:
            scroll.setFixedWidth(fixed_width)
        return scroll

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    def _build_pages(self):
        self.stack = QStackedWidget()
        self.right_layout.addWidget(self.stack, 1)

        self._build_tv_page()
        self._build_samsung_page()
        self._build_vpn_page()
        self._build_diag_page()
        self._build_callback_page()
        self.switch_tab(0)

    def _build_tv_page(self):
        page = QWidget()
        grid = QHBoxLayout(page)
        grid.setContentsMargins(20, 16, 20, 12)
        grid.setSpacing(16)

        left_inner = QWidget()
        left_inner.setObjectName('ScrollContent')
        left = QVBoxLayout(left_inner)
        left.setContentsMargins(0, 0, 6, 0)
        left.setSpacing(12)

        # Card 1: Connection
        conn_card, cl = self._make_card('lbl_card_conn', 1)
        self.lbl_ip = QLabel()
        self.lbl_ip.setObjectName('FieldLabel')
        self.tv_ip = QLineEdit()
        self.tv_ip.setPlaceholderText('192.168.1.42')
        cl.addWidget(self.lbl_ip)
        cl.addWidget(self.tv_ip)

        self.lbl_tv_pass = QLabel()
        self.lbl_tv_pass.setObjectName('FieldLabel')
        cl.addWidget(self.lbl_tv_pass)
        pass_row = QHBoxLayout()
        pass_row.setSpacing(6)
        self.tv_pass = QLineEdit()
        self.tv_pass.setEchoMode(QLineEdit.Password)
        self.tv_pass_eye = QPushButton('SHOW')
        self.tv_pass_eye.setObjectName('EyeBtn')
        self.tv_pass_eye.setCursor(Qt.PointingHandCursor)
        self.tv_pass_eye.clicked.connect(lambda: self._toggle_echo(self.tv_pass, self.tv_pass_eye))
        pass_row.addWidget(self.tv_pass, 1)
        pass_row.addWidget(self.tv_pass_eye)
        cl.addLayout(pass_row)

        self.chk_os26 = ToggleSwitch('')
        cl.addWidget(self.chk_os26)

        self.hint_tv = QLabel()
        self.hint_tv.setObjectName('HintLabel')
        self.hint_tv.setWordWrap(True)
        cl.addWidget(self.hint_tv)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_connect_tv = QPushButton()
        self.btn_connect_tv.setObjectName('PrimaryBtn')
        self.btn_connect_tv.setCursor(Qt.PointingHandCursor)
        self.btn_connect_tv.clicked.connect(self.connect_tv)

        self.btn_disconnect_tv = QPushButton()
        self.btn_disconnect_tv.setObjectName('GhostBtn')
        self.btn_disconnect_tv.setCursor(Qt.PointingHandCursor)
        self.btn_disconnect_tv.setEnabled(False)
        self.btn_disconnect_tv.clicked.connect(self.disconnect_tv)

        btn_row.addWidget(self.btn_connect_tv, 2)
        btn_row.addWidget(self.btn_disconnect_tv, 1)
        cl.addLayout(btn_row)
        left.addWidget(conn_card)

        remote_card, remote_layout = self._make_card('lbl_card_lg_remote', 0)
        self.lbl_card_lg_remote.setText('ПУЛЬТ LG / LG REMOTE')
        remote_note = QLabel('TV и компьютер должны быть в одной сети. При подключении подтвердите запрос на ТВ. Developer Mode не нужен.')
        remote_note.setObjectName('HintLabel')
        remote_note.setWordWrap(True)
        remote_layout.addWidget(remote_note)
        row = QHBoxLayout()
        for title, action in [('Подключить пульт', self._lg_remote_connect), ('Отключить', self._lg_remote_disconnect)]:
            button = QPushButton(title)
            button.setObjectName('PrimaryBtn' if title == 'Подключить пульт' else 'GhostBtn')
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(action)
            row.addWidget(button)
        remote_layout.addLayout(row)
        self.lg_remote_status = QLabel('Пульт: отключён')
        self.lg_remote_status.setObjectName('HintLabel')
        remote_layout.addWidget(self.lg_remote_status)
        buttons = QGridLayout()
        for title, key, r, c in [('', '', 0, 0), ('▲', 'up', 0, 1), ('HOME', 'home', 0, 2), ('◀', 'left', 1, 0), ('OK', 'ok', 1, 1), ('▶', 'right', 1, 2), ('BACK', 'back', 2, 0), ('▼', 'down', 2, 1), ('MENU', 'menu', 2, 2), ('VOL −', 'volume_down', 3, 0), ('MUTE', 'mute', 3, 1), ('VOL +', 'volume_up', 3, 2), ('CH −', 'channel_down', 4, 0), ('▶ / ❚❚', 'play', 4, 1), ('CH +', 'channel_up', 4, 2)]:
            if not key:
                continue
            button = QPushButton(title)
            button.setObjectName('SamKeyPrimary' if key in ('up', 'down', 'left', 'right', 'ok') else 'SamKeyBtn')
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda checked=False, k=key: self._lg_remote_key(k))
            buttons.addWidget(button, r, c)
        remote_layout.addLayout(buttons)
        left.addWidget(remote_card)

        # Card 2: Region Target
        reg_card, rl = self._make_card('lbl_card_reg', 2)
        self.lbl_area = QLabel()
        self.lbl_area.setObjectName('FieldLabel')
        self.region_combo = QComboBox()
        self.region_combo.currentIndexChanged.connect(self._on_region_changed)
        rl.addWidget(self.lbl_area)
        rl.addWidget(self.region_combo)

        self.lbl_area_custom = QLabel()
        self.lbl_area_custom.setObjectName('FieldLabel')
        self.region_code_input = QLineEdit()
        self.region_code_input.setValidator(QRegExpValidator(QRegExp(r'^[0-9]{0,10}$')))
        self.region_code_input.setStyleSheet('font-family:"Consolas","Cascadia Mono",monospace;font-weight:bold;letter-spacing:0.6px;')
        self.region_code_input.textChanged.connect(self._on_region_code_edited)
        rl.addWidget(self.lbl_area_custom)
        rl.addWidget(self.region_code_input)

        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        self.btn_read = QPushButton()
        self.btn_read.setObjectName('NormalBtn')
        self.btn_read.setCursor(Qt.PointingHandCursor)
        self.btn_read.clicked.connect(lambda: self.tv_action('read'))

        self.btn_verify = QPushButton()
        self.btn_verify.setObjectName('NormalBtn')
        self.btn_verify.setCursor(Qt.PointingHandCursor)
        self.btn_verify.clicked.connect(lambda: self.tv_action('verify'))

        act_row.addWidget(self.btn_read)
        act_row.addWidget(self.btn_verify)
        rl.addLayout(act_row)

        self.btn_write = QPushButton()
        self.btn_write.setObjectName('PrimaryBtn')
        self.btn_write.setCursor(Qt.PointingHandCursor)
        self.btn_write.clicked.connect(self.write_region)
        rl.addWidget(self.btn_write)

        self.warn_nvram = QLabel()
        self.warn_nvram.setObjectName('WarnBox')
        self.warn_nvram.setWordWrap(True)
        rl.addWidget(self.warn_nvram)
        left.addWidget(reg_card)

        # Card 3: Belet TV App (belet.tm)
        belet_card, bl = self._make_card('lbl_belet', 3)
        bh = QHBoxLayout()
        bh.setSpacing(10)
        b_ico = QLabel()
        b_ico.setFixedSize(40, 40)
        belet_px_path = resource('assets/belet_ui.png')
        if os.path.isfile(belet_px_path):
            b_ico.setPixmap(QPixmap(belet_px_path).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.belet_desc = QLabel()
        self.belet_desc.setObjectName('HintLabel')
        self.belet_desc.setWordWrap(True)
        bh.addWidget(b_ico, 0, Qt.AlignTop)
        bh.addWidget(self.belet_desc, 1)
        bl.addLayout(bh)

        self.btn_belet_install = QPushButton()
        self.btn_belet_install.setObjectName('PrimaryBtn')
        self.btn_belet_install.setCursor(Qt.PointingHandCursor)
        self.btn_belet_install.clicked.connect(self.install_belet)
        bl.addWidget(self.btn_belet_install)
        b_row = QHBoxLayout()
        b_row.setSpacing(8)
        self.btn_belet_launch = QPushButton()
        self.btn_belet_launch.setObjectName('NormalBtn')
        self.btn_belet_launch.setCursor(Qt.PointingHandCursor)
        self.btn_belet_launch.clicked.connect(self.launch_belet)
        self.btn_belet_remove = QPushButton()
        self.btn_belet_remove.setObjectName('DangerBtn')
        self.btn_belet_remove.setCursor(Qt.PointingHandCursor)
        self.btn_belet_remove.clicked.connect(self.remove_belet)
        b_row.addWidget(self.btn_belet_launch, 1)
        b_row.addWidget(self.btn_belet_remove, 1)
        bl.addLayout(b_row)
        left.addWidget(belet_card)

        # Card 4: Alem TV App (telecom.alemtv.tm)
        alem_card, al = self._make_card('lbl_alem', 4)
        ah = QHBoxLayout()
        ah.setSpacing(10)
        a_ico = QLabel()
        a_ico.setFixedSize(40, 40)
        alem_px_path = resource('assets/alemtv_ui.png')
        if os.path.isfile(alem_px_path):
            a_ico.setPixmap(QPixmap(alem_px_path).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.alem_desc = QLabel()
        self.alem_desc.setObjectName('HintLabel')
        self.alem_desc.setWordWrap(True)
        ah.addWidget(a_ico, 0, Qt.AlignTop)
        ah.addWidget(self.alem_desc, 1)
        al.addLayout(ah)

        self.lbl_alem_portal = QLabel()
        self.lbl_alem_portal.setObjectName('FieldLabel')
        al.addWidget(self.lbl_alem_portal)

        self.alem_portal_combo = QComboBox()
        for p in ALEMTV_PORTALS:
            self.alem_portal_combo.addItem(p['name'], p['url'])
        self.alem_portal_combo.setCurrentIndex(self.alem_portal_combo.findData(ALEMTV_DEFAULT_PORTAL))
        al.addWidget(self.alem_portal_combo)

        self.btn_alem_install = QPushButton()
        self.btn_alem_install.setObjectName('PrimaryBtn')
        self.btn_alem_install.setCursor(Qt.PointingHandCursor)
        self.btn_alem_install.clicked.connect(self.install_alem)
        al.addWidget(self.btn_alem_install)

        a_row = QHBoxLayout()
        a_row.setSpacing(8)
        self.btn_alem_launch = QPushButton()
        self.btn_alem_launch.setObjectName('NormalBtn')
        self.btn_alem_launch.setCursor(Qt.PointingHandCursor)
        self.btn_alem_launch.clicked.connect(self.launch_alem)
        self.btn_alem_remove = QPushButton()
        self.btn_alem_remove.setObjectName('DangerBtn')
        self.btn_alem_remove.setCursor(Qt.PointingHandCursor)
        self.btn_alem_remove.clicked.connect(self.remove_alem)
        a_row.addWidget(self.btn_alem_launch, 1)
        a_row.addWidget(self.btn_alem_remove, 1)
        al.addLayout(a_row)
        left.addWidget(alem_card)
        local_card, local_layout = self._make_card('lbl_local_lg_app', 5)
        self.lbl_local_lg_app.setText('УСТАНОВКА IPK ИЗ ФАЙЛА')
        note = QLabel('Только проверенные файлы webOS IPK. Требуется подключение к LG по SSH / Developer Mode. IPK устанавливается без переподписания.')
        note.setObjectName('HintLabel')
        note.setWordWrap(True)
        local_layout.addWidget(note)
        btn = QPushButton('ВЫБРАТЬ IPK И УСТАНОВИТЬ')
        btn.setObjectName('NormalBtn')
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self.install_local_lg_ipk)
        local_layout.addWidget(btn)
        left.addWidget(local_card)
        left.addStretch(1)

        grid.addWidget(self._wrap_scroll(left_inner, fixed_width=392))

        # Right column: KPIs + Terminal + Reboot & System Setup
        right = QVBoxLayout()
        right.setSpacing(12)

        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)
        self.kpi_tv = KpiCard('TV CONNECTION', 'Awaiting device', 'SSH 9922')
        self.kpi_area = KpiCard('SELECTED CODE', '22282', 'US · Verified')
        self.kpi_vpn = KpiCard('CONF & HOTSPOT', 'Idle', 'Conf Engine')
        kpi_row.addWidget(self.kpi_tv, 1)
        kpi_row.addWidget(self.kpi_area, 1)
        kpi_row.addWidget(self.kpi_vpn, 1)
        right.addLayout(kpi_row)

        self.tv_term = NeonTerminal(on_save=self._save_log_dialog)
        right.addWidget(self.tv_term, 1)

        bottom_strip = QHBoxLayout()
        bottom_strip.setSpacing(10)
        self.btn_sys_tune = QPushButton()
        self.btn_sys_tune.setObjectName('NormalBtn')
        self.btn_sys_tune.setCursor(Qt.PointingHandCursor)
        self.btn_sys_tune.clicked.connect(self.open_system_tune)
        self.btn_reboot = QPushButton()
        self.btn_reboot.setObjectName('DangerBtn')
        self.btn_reboot.setCursor(Qt.PointingHandCursor)
        self.btn_reboot.clicked.connect(self.reboot_tv)
        self.reboot_hint = QLabel()
        self.reboot_hint.setObjectName('HintLabel')
        self.reboot_hint.setWordWrap(True)
        bottom_strip.addWidget(self.btn_sys_tune)
        bottom_strip.addWidget(self.btn_reboot)
        bottom_strip.addWidget(self.reboot_hint, 1)
        right.addLayout(bottom_strip)

        grid.addLayout(right, 1)
        self.stack.addWidget(page)

    def _build_samsung_page(self):
        page = QWidget()
        grid = QHBoxLayout(page)
        grid.setContentsMargins(20, 16, 20, 12)
        grid.setSpacing(16)

        left_inner = QWidget()
        left_inner.setObjectName('ScrollContent')
        left_inner.setMaximumWidth(406)
        left = QVBoxLayout(left_inner)
        left.setContentsMargins(0, 0, 6, 0)
        left.setSpacing(12)

        # Card 1: Samsung LAN Connection
        c_conn, cl = self._make_card('lbl_sam_conn', 1)
        self.sam_lbl_ip = QLabel()
        self.sam_lbl_ip.setObjectName('FieldLabel')
        cl.addWidget(self.sam_lbl_ip)

        ip_row = QHBoxLayout()
        ip_row.setSpacing(8)
        self.sam_ip = QLineEdit()
        self.sam_ip.setPlaceholderText('192.168.1.101')
        self.sam_ip.editingFinished.connect(self._update_sam_host_ip_label)
        self.btn_sam_scan = QPushButton()
        self.btn_sam_scan.setObjectName('NormalBtn')
        self.btn_sam_scan.setCursor(Qt.PointingHandCursor)
        self.btn_sam_scan.clicked.connect(self.discover_samsung)
        ip_row.addWidget(self.sam_ip, 1)
        ip_row.addWidget(self.btn_sam_scan)
        cl.addLayout(ip_row)

        self.sam_found_box = QFrame()
        fl = QVBoxLayout(self.sam_found_box)
        fl.setContentsMargins(0, 2, 0, 0)
        fl.setSpacing(4)
        self.sam_lbl_found = QLabel()
        self.sam_lbl_found.setObjectName('FieldLabel')
        self.sam_found_combo = QComboBox()
        self.sam_found_combo.currentIndexChanged.connect(self._on_sam_found_selected)
        fl.addWidget(self.sam_lbl_found)
        fl.addWidget(self.sam_found_combo)
        self.sam_found_box.setVisible(False)
        cl.addWidget(self.sam_found_box)

        self.sam_lbl_proto = QLabel()
        self.sam_lbl_proto.setObjectName('FieldLabel')
        self.sam_port_combo = QComboBox()
        self.sam_port_combo.addItem('Auto (8002 WSS → 8001 WS → 55000)', 'auto')
        self.sam_port_combo.addItem('Tizen 2018–2026 · WSS (8002)', '8002')
        self.sam_port_combo.addItem('Tizen 2016–2018 · WS (8001)', '8001')
        self.sam_port_combo.addItem('Legacy Orsay <2014 · TCP (55000)', '55000')
        cl.addWidget(self.sam_lbl_proto)
        cl.addWidget(self.sam_port_combo)

        self.sam_hint_pair = QLabel()
        self.sam_hint_pair.setObjectName('HintLabel')
        self.sam_hint_pair.setWordWrap(True)
        cl.addWidget(self.sam_hint_pair)

        sbtn_row = QHBoxLayout()
        sbtn_row.setSpacing(8)
        self.btn_sam_conn = QPushButton()
        self.btn_sam_conn.setObjectName('PrimaryBtn')
        self.btn_sam_conn.setCursor(Qt.PointingHandCursor)
        self.btn_sam_conn.clicked.connect(self.connect_samsung)
        self.btn_sam_disc = QPushButton()
        self.btn_sam_disc.setObjectName('GhostBtn')
        self.btn_sam_disc.setCursor(Qt.PointingHandCursor)
        self.btn_sam_disc.setEnabled(False)
        self.btn_sam_disc.clicked.connect(self.disconnect_samsung)
        sbtn_row.addWidget(self.btn_sam_conn, 2)
        sbtn_row.addWidget(self.btn_sam_disc, 1)
        cl.addLayout(sbtn_row)
        left.addWidget(c_conn)

        # Card 2: Service Menu & MRT Option / Local Set
        c_svc, sl = self._make_card('lbl_sam_svc', 2)
        self.sam_svc_hint = QLabel()
        self.sam_svc_hint.setObjectName('HintLabel')
        self.sam_svc_hint.setWordWrap(True)
        sl.addWidget(self.sam_svc_hint)

        self.btn_sam_sm = QPushButton()
        self.btn_sam_sm.setObjectName('PrimaryBtn')
        self.btn_sam_sm.setCursor(Qt.PointingHandCursor)
        self.btn_sam_sm.clicked.connect(lambda: self.sam_action('service_menu_factory'))
        sl.addWidget(self.btn_sam_sm)

        sm_row1 = QHBoxLayout()
        sm_row1.setSpacing(8)
        self.btn_sam_3spd = QPushButton()
        self.btn_sam_3spd.setObjectName('NormalBtn')
        self.btn_sam_3spd.setCursor(Qt.PointingHandCursor)
        self.btn_sam_3spd.clicked.connect(lambda: self.sam_action('service_menu_3speed'))
        self.btn_sam_adv = QPushButton()
        self.btn_sam_adv.setObjectName('NormalBtn')
        self.btn_sam_adv.setCursor(Qt.PointingHandCursor)
        self.btn_sam_adv.clicked.connect(lambda: self.sam_action('unlock_advanced_tv'))
        sm_row1.addWidget(self.btn_sam_3spd, 1)
        sm_row1.addWidget(self.btn_sam_adv, 1)
        sl.addLayout(sm_row1)

        sm_row2 = QHBoxLayout()
        sm_row2.setSpacing(8)
        self.btn_sam_opt = QPushButton()
        self.btn_sam_opt.setObjectName('NormalBtn')
        self.btn_sam_opt.setCursor(Qt.PointingHandCursor)
        self.btn_sam_opt.clicked.connect(lambda: self.sam_action('open_option'))
        self.btn_sam_exit = QPushButton()
        self.btn_sam_exit.setObjectName('NormalBtn')
        self.btn_sam_exit.setCursor(Qt.PointingHandCursor)
        self.btn_sam_exit.clicked.connect(lambda: self.sam_action('exit_service_menu'))
        sm_row2.addWidget(self.btn_sam_opt, 1)
        sm_row2.addWidget(self.btn_sam_exit, 1)
        sl.addLayout(sm_row2)

        self.sam_lbl_hosp = QLabel()
        self.sam_lbl_hosp.setObjectName('FieldLabel')
        sl.addWidget(self.sam_lbl_hosp)
        hosp_row = QHBoxLayout()
        hosp_row.setSpacing(6)
        b_h1 = QPushButton('Mute+119')
        b_h1.setObjectName('GhostBtn')
        b_h1.setCursor(Qt.PointingHandCursor)
        b_h1.clicked.connect(lambda: self.sam_action('hospitality_step1'))
        b_h2 = QPushButton('1234+Up')
        b_h2.setObjectName('GhostBtn')
        b_h2.setCursor(Qt.PointingHandCursor)
        b_h2.clicked.connect(lambda: self.sam_action('hospitality_step2'))
        b_h3 = QPushButton('Vol+ 5s')
        b_h3.setObjectName('GhostBtn')
        b_h3.setCursor(Qt.PointingHandCursor)
        b_h3.clicked.connect(lambda: self.sam_action('hospitality_step3'))
        for _b in (b_h1, b_h2, b_h3):
            _b.setStyleSheet('padding: 0 6px; font-size: 11px;')
            hosp_row.addWidget(_b, 1)
        sl.addLayout(hosp_row)

        self.sam_lbl_localset = QLabel()
        self.sam_lbl_localset.setObjectName('FieldLabel')
        sl.addWidget(self.sam_lbl_localset)
        self.sam_localset_combo = QComboBox()
        for code, _en, _ru in SAMSUNG_LOCAL_SETS:
            self.sam_localset_combo.addItem(code, code)
        self.sam_localset_combo.currentIndexChanged.connect(self._on_sam_localset_changed)
        sl.addWidget(self.sam_localset_combo)

        self.sam_localset_hint = QLabel()
        self.sam_localset_hint.setObjectName('HintLabel')
        self.sam_localset_hint.setWordWrap(True)
        sl.addWidget(self.sam_localset_hint)
        left.addWidget(c_svc)

        # Card 3: Belet TV App (Tizen SDB 26101)
        c_sam_belet, sbl = self._make_card('lbl_sam_belet', 3)
        sbh = QHBoxLayout()
        sbh.setSpacing(10)
        sb_ico = QLabel()
        sb_ico.setFixedSize(40, 40)
        sam_belet_px = resource('assets/belet_ui.png')
        if os.path.isfile(sam_belet_px):
            sb_ico.setPixmap(QPixmap(sam_belet_px).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.sam_belet_desc = QLabel()
        self.sam_belet_desc.setObjectName('HintLabel')
        self.sam_belet_desc.setWordWrap(True)
        sbh.addWidget(sb_ico, 0, Qt.AlignTop)
        sbh.addWidget(self.sam_belet_desc, 1)
        sbl.addLayout(sbh)

        self.sam_host_ip_lbl = QLabel()
        self.sam_host_ip_lbl.setStyleSheet(
            'background:#0a181e;color:#7de8ff;border:1px solid #1e4252;border-radius:8px;'
            'padding:6px 10px;font-family:"Consolas",monospace;font-size:11px;font-weight:bold;'
        )
        self.sam_host_ip_lbl.setWordWrap(True)
        sbl.addWidget(self.sam_host_ip_lbl)

        sdb_row = QHBoxLayout()
        sdb_row.setSpacing(8)
        self.btn_sam_sdb_check = QPushButton()
        self.btn_sam_sdb_check.setObjectName('NormalBtn')
        self.btn_sam_sdb_check.setCursor(Qt.PointingHandCursor)
        self.btn_sam_sdb_check.clicked.connect(self.check_samsung_sdb)
        self.btn_sam_cert = QPushButton()
        self.btn_sam_cert.setObjectName('GhostBtn')
        self.btn_sam_cert.setCursor(Qt.PointingHandCursor)
        self.btn_sam_cert.clicked.connect(self.auth_samsung_cert)
        sdb_row.addWidget(self.btn_sam_sdb_check, 1)
        sdb_row.addWidget(self.btn_sam_cert, 1)
        sbl.addLayout(sdb_row)

        self.btn_sam_belet_install = QPushButton()
        self.btn_sam_belet_install.setObjectName('PrimaryBtn')
        self.btn_sam_belet_install.setCursor(Qt.PointingHandCursor)
        self.btn_sam_belet_install.clicked.connect(self.install_samsung_belet)
        sbl.addWidget(self.btn_sam_belet_install)
        btn_belet_client = QPushButton('УСТАНОВИТЬ BELET SAMSUNG · НОВЫЙ КЛИЕНТ')
        btn_belet_client.setObjectName('NormalBtn')
        btn_belet_client.setCursor(Qt.PointingHandCursor)
        btn_belet_client.setToolTip('Клиент Belet для Tizen: вход, каталог, поиск, сезоны, плеер и прогресс. Обычный Belet не изменяется.')
        btn_belet_client.clicked.connect(self.install_samsung_belet_client)
        sbl.addWidget(btn_belet_client)
        sbl_row = QHBoxLayout()
        sbl_row.setSpacing(8)
        self.btn_sam_belet_launch = QPushButton()
        self.btn_sam_belet_launch.setObjectName('NormalBtn')
        self.btn_sam_belet_launch.setCursor(Qt.PointingHandCursor)
        self.btn_sam_belet_launch.clicked.connect(self.launch_samsung_belet)
        self.btn_sam_belet_remove = QPushButton()
        self.btn_sam_belet_remove.setObjectName('DangerBtn')
        self.btn_sam_belet_remove.setCursor(Qt.PointingHandCursor)
        self.btn_sam_belet_remove.clicked.connect(self.remove_samsung_belet)
        sbl_row.addWidget(self.btn_sam_belet_launch, 1)
        sbl_row.addWidget(self.btn_sam_belet_remove, 1)
        sbl.addLayout(sbl_row)
        left.addWidget(c_sam_belet)

        # Card 4: Alem TV App (Tizen)
        c_sam_alem, sal = self._make_card('lbl_sam_alem', 4)
        sah = QHBoxLayout()
        sah.setSpacing(10)
        sa_ico = QLabel()
        sa_ico.setFixedSize(40, 40)
        sam_alem_px = resource('assets/alemtv_ui.png')
        if os.path.isfile(sam_alem_px):
            sa_ico.setPixmap(QPixmap(sam_alem_px).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.sam_alem_desc = QLabel()
        self.sam_alem_desc.setObjectName('HintLabel')
        self.sam_alem_desc.setWordWrap(True)
        sah.addWidget(sa_ico, 0, Qt.AlignTop)
        sah.addWidget(self.sam_alem_desc, 1)
        sal.addLayout(sah)

        self.lbl_sam_alem_portal = QLabel()
        self.lbl_sam_alem_portal.setObjectName('FieldLabel')
        sal.addWidget(self.lbl_sam_alem_portal)

        self.sam_alem_portal_combo = QComboBox()
        for p in ALEMTV_PORTALS:
            self.sam_alem_portal_combo.addItem(p['name'], p['url'])
        self.sam_alem_portal_combo.setCurrentIndex(self.sam_alem_portal_combo.findData(ALEMTV_DEFAULT_PORTAL))
        sal.addWidget(self.sam_alem_portal_combo)

        self.btn_sam_alem_install = QPushButton()
        self.btn_sam_alem_install.setObjectName('PrimaryBtn')
        self.btn_sam_alem_install.setCursor(Qt.PointingHandCursor)
        self.btn_sam_alem_install.clicked.connect(self.install_samsung_alemtv)
        sal.addWidget(self.btn_sam_alem_install)

        sal_row = QHBoxLayout()
        sal_row.setSpacing(8)
        self.btn_sam_alem_launch = QPushButton()
        self.btn_sam_alem_launch.setObjectName('NormalBtn')
        self.btn_sam_alem_launch.setCursor(Qt.PointingHandCursor)
        self.btn_sam_alem_launch.clicked.connect(self.launch_samsung_alemtv)
        self.btn_sam_alem_remove = QPushButton()
        self.btn_sam_alem_remove.setObjectName('DangerBtn')
        self.btn_sam_alem_remove.setCursor(Qt.PointingHandCursor)
        self.btn_sam_alem_remove.clicked.connect(self.remove_samsung_alemtv)
        sal_row.addWidget(self.btn_sam_alem_launch, 1)
        sal_row.addWidget(self.btn_sam_alem_remove, 1)
        sal.addLayout(sal_row)
        left.addWidget(c_sam_alem)
        local_card, local_layout = self._make_card('lbl_local_sam_app', 5)
        self.lbl_local_sam_app.setText('УСТАНОВКА WGT ИЗ ФАЙЛА')
        note = QLabel('Только проверенные Samsung Tizen web WGT. Пакет будет переподписан; текущая подпись автора заменяется.')
        note.setObjectName('HintLabel')
        note.setWordWrap(True)
        local_layout.addWidget(note)
        btn = QPushButton('ВЫБРАТЬ WGT И УСТАНОВИТЬ')
        btn.setObjectName('NormalBtn')
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self.install_local_samsung_wgt)
        local_layout.addWidget(btn)
        left.addWidget(local_card)
        left.addStretch(1)

        grid.addWidget(self._wrap_scroll(left_inner, fixed_width=428))

        # Right column: KPIs + Virtual Service Remote + Live Console
        right = QVBoxLayout()
        right.setSpacing(12)

        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)
        self.kpi_sam = KpiCard('SAMSUNG TV', 'Awaiting TV', 'LAN 8002 / 8001')
        self.kpi_sam_proto = KpiCard('TRANSPORT / TOKEN', 'Not paired', 'Auto detect')
        self.kpi_sam_country = KpiCard('TV COUNTRY / LANG', '—', 'Read via /api/v2/')
        kpi_row.addWidget(self.kpi_sam, 1)
        kpi_row.addWidget(self.kpi_sam_proto, 1)
        kpi_row.addWidget(self.kpi_sam_country, 1)
        right.addLayout(kpi_row)

        # Virtual Service Remote Card
        rem_card = QFrame()
        rem_card.setObjectName('GlassCard')
        add_elevation(rem_card, blur=20, alpha=70)
        rem_lay = QVBoxLayout(rem_card)
        rem_lay.setContentsMargins(14, 10, 14, 12)
        rem_lay.setSpacing(8)

        r_head = QHBoxLayout()
        r_badge = QLabel('◈')
        r_badge.setObjectName('StepBadge')
        r_badge.setAlignment(Qt.AlignCenter)
        self.sam_remote_title = QLabel()
        self.sam_remote_title.setObjectName('CardTitle')
        self.sam_kb_toggle = ToggleSwitch('', checked=False)
        self.sam_kb_toggle.setMinimumWidth(130)
        r_head.addWidget(r_badge)
        r_head.addWidget(self.sam_remote_title)
        r_head.addStretch(1)
        r_head.addWidget(self.sam_kb_toggle)
        rem_lay.addLayout(r_head)

        pad_split = QHBoxLayout()
        pad_split.setSpacing(14)

        dpad = QGridLayout()
        dpad.setSpacing(5)
        dpad_buttons = [
            (0, 0, 'MENU', 'KEY_MENU', 'SamKeyBtn'),
            (0, 1, '▲', 'KEY_UP', 'SamKeyPrimary'),
            (0, 2, 'INFO', 'KEY_INFO', 'SamKeyBtn'),
            (1, 0, '◀', 'KEY_LEFT', 'SamKeyPrimary'),
            (1, 1, 'OK', 'KEY_ENTER', 'SamKeyPrimary'),
            (1, 2, '▶', 'KEY_RIGHT', 'SamKeyPrimary'),
            (2, 0, 'RETURN', 'KEY_RETURN', 'SamKeyBtn'),
            (2, 1, '▼', 'KEY_DOWN', 'SamKeyPrimary'),
            (2, 2, 'EXIT', 'KEY_EXIT', 'SamKeyBtn'),
        ]
        for r, c, lbl, kcode, obj in dpad_buttons:
            b = QPushButton(lbl)
            b.setObjectName(obj)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=kcode: self.sam_key(k))
            dpad.addWidget(b, r, c)

        svc_pad = QGridLayout()
        svc_pad.setSpacing(5)
        svc_buttons = [
            (0, 0, 'FACTORY', lambda: self.sam_key('KEY_FACTORY'), 'SamKeyPrimary'),
            (0, 1, '3SPEED', lambda: self.sam_key('KEY_3SPEED'), 'SamKeyBtn'),
            (0, 2, 'MUTE', lambda: self.sam_key('KEY_MUTE'), 'SamKeyBtn'),
            (1, 0, 'VOL +', lambda: self.sam_key('KEY_VOLUP'), 'SamKeyBtn'),
            (1, 1, 'VOL -', lambda: self.sam_key('KEY_VOLDOWN'), 'SamKeyBtn'),
            (1, 2, 'CH +', lambda: self.sam_key('KEY_CHUP'), 'SamKeyBtn'),
            (2, 0, '0098', lambda: self.sam_action('unlock_advanced_tv'), 'SamKeyBtn'),
            (2, 1, '0038', lambda: self.sam_action('unlock_advanced_mon'), 'SamKeyBtn'),
            (2, 2, 'HOME', lambda: self.sam_key('KEY_HOME'), 'SamKeyBtn'),
            (3, 0, '0', lambda: self.sam_key('KEY_0'), 'SamKeyBtn'),
            (3, 1, '1', lambda: self.sam_key('KEY_1'), 'SamKeyBtn'),
            (3, 2, 'POWER', lambda: self.sam_key('KEY_POWER'), 'SamKeyWarn'),
        ]
        for r, c, lbl, fn, obj in svc_buttons:
            b = QPushButton(lbl)
            b.setObjectName(obj)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            svc_pad.addWidget(b, r, c)

        pad_split.addLayout(dpad, 1)
        pad_split.addLayout(svc_pad, 1)
        rem_lay.addLayout(pad_split)
        right.addWidget(rem_card)

        self.sam_term = NeonTerminal(on_save=self._save_log_dialog)
        right.addWidget(self.sam_term, 1)

        sam_strip = QGridLayout()
        sam_strip.setSpacing(8)
        self.btn_sam_sys_tune = QPushButton()
        self.btn_sam_sys_tune.setObjectName('NormalBtn')
        self.btn_sam_sys_tune.setCursor(Qt.PointingHandCursor)
        self.btn_sam_sys_tune.clicked.connect(self.open_samsung_system_tune)
        self.btn_sam_smarthub = QPushButton()
        self.btn_sam_smarthub.setObjectName('NormalBtn')
        self.btn_sam_smarthub.setCursor(Qt.PointingHandCursor)
        self.btn_sam_smarthub.clicked.connect(lambda: self.sam_action('smart_hub_region_unlock'))
        self.btn_sam_reset = QPushButton()
        self.btn_sam_reset.setObjectName('DangerBtn')
        self.btn_sam_reset.setCursor(Qt.PointingHandCursor)
        self.btn_sam_reset.clicked.connect(self.sam_factory_reset)
        self.btn_sam_power = QPushButton()
        self.btn_sam_power.setObjectName('NormalBtn')
        self.btn_sam_power.setCursor(Qt.PointingHandCursor)
        self.btn_sam_power.clicked.connect(lambda: self.sam_action('reboot_tv'))
        sam_strip.addWidget(self.btn_sam_sys_tune, 0, 0)
        sam_strip.addWidget(self.btn_sam_smarthub, 0, 1)
        sam_strip.addWidget(self.btn_sam_reset, 1, 0)
        sam_strip.addWidget(self.btn_sam_power, 1, 1)
        sam_strip.setColumnStretch(0, 1)
        sam_strip.setColumnStretch(1, 1)
        right.addLayout(sam_strip)

        grid.addLayout(right, 1)
        self.stack.addWidget(page)

    def _build_vpn_page(self):
        page = QWidget()
        grid = QHBoxLayout(page)
        grid.setContentsMargins(20, 16, 20, 12)
        grid.setSpacing(16)

        left_inner = QWidget()
        left_inner.setObjectName('ScrollContent')
        left = QVBoxLayout(left_inner)
        left.setContentsMargins(0, 0, 6, 0)
        left.setSpacing(12)

        # Card 1: OpenVPN
        c1, l1 = self._make_card('lbl_vpn1', 1)
        self.vpn_card1_desc = QLabel()
        self.vpn_card1_desc.setObjectName('HintLabel')
        self.vpn_card1_desc.setWordWrap(True)
        l1.addWidget(self.vpn_card1_desc)

        self.lbl_profile = QLabel()
        self.lbl_profile.setObjectName('FieldLabel')
        l1.addWidget(self.lbl_profile)
        self.profile_combo = QComboBox()
        self.profile_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.profile_combo.setMinimumContentsLength(10)
        self.profile_combo.currentTextChanged.connect(self._on_profile_changed)
        l1.addWidget(self.profile_combo)

        pr_row = QHBoxLayout()
        pr_row.setSpacing(8)
        self.btn_browse = QPushButton()
        self.btn_browse.setObjectName('GhostBtn')
        self.btn_browse.setCursor(Qt.PointingHandCursor)
        self.btn_browse.clicked.connect(self.browse_profile)

        self.btn_refresh_prof = QPushButton()
        self.btn_refresh_prof.setObjectName('NormalBtn')
        self.btn_refresh_prof.setCursor(Qt.PointingHandCursor)
        self.btn_refresh_prof.clicked.connect(self.refresh_profiles)

        pr_row.addWidget(self.btn_browse)
        pr_row.addWidget(self.btn_refresh_prof)
        l1.addLayout(pr_row)

        self.vpn_auth_box = QFrame()
        self.vpn_auth_box.setObjectName('AuthBox')
        self.vpn_auth_box.setVisible(False)
        al = QVBoxLayout(self.vpn_auth_box)
        al.setContentsMargins(12, 10, 12, 10)
        al.setSpacing(6)
        self.lbl_vpn_user = QLabel()
        self.lbl_vpn_user.setObjectName('FieldLabel')
        self.vpn_user = QLineEdit()
        self.lbl_vpn_pass = QLabel()
        self.lbl_vpn_pass.setObjectName('FieldLabel')
        vp_row = QHBoxLayout()
        vp_row.setSpacing(6)
        self.vpn_pass = QLineEdit()
        self.vpn_pass.setEchoMode(QLineEdit.Password)
        self.vpn_pass_eye = QPushButton('SHOW')
        self.vpn_pass_eye.setObjectName('EyeBtn')
        self.vpn_pass_eye.setCursor(Qt.PointingHandCursor)
        self.vpn_pass_eye.clicked.connect(lambda: self._toggle_echo(self.vpn_pass, self.vpn_pass_eye))
        vp_row.addWidget(self.vpn_pass, 1)
        vp_row.addWidget(self.vpn_pass_eye)
        al.addWidget(self.lbl_vpn_user)
        al.addWidget(self.vpn_user)
        al.addWidget(self.lbl_vpn_pass)
        al.addLayout(vp_row)
        l1.addWidget(self.vpn_auth_box)

        vc_row = QHBoxLayout()
        vc_row.setSpacing(8)
        self.btn_vpn_conn = QPushButton()
        self.btn_vpn_conn.setObjectName('PrimaryBtn')
        self.btn_vpn_conn.setCursor(Qt.PointingHandCursor)
        self.btn_vpn_conn.clicked.connect(self.connect_vpn)

        self.btn_vpn_disc = QPushButton()
        self.btn_vpn_disc.setObjectName('DangerBtn')
        self.btn_vpn_disc.setCursor(Qt.PointingHandCursor)
        self.btn_vpn_disc.clicked.connect(self.disconnect_vpn)

        vc_row.addWidget(self.btn_vpn_conn, 2)
        vc_row.addWidget(self.btn_vpn_disc, 1)
        l1.addLayout(vc_row)

        stat_row = QHBoxLayout()
        self.vpn_stat_lbl = QLabel()
        self.vpn_stat_lbl.setStyleSheet('color:#7d94a1;font-size:11px;font-weight:600;')
        self.vpn_stat_val = QLabel('DISCONNECTED')
        self.vpn_stat_val.setStyleSheet('color:#97ad9e;font-family:"Consolas",monospace;font-size:11px;font-weight:bold;')
        stat_row.addWidget(self.vpn_stat_lbl)
        stat_row.addSpacing(6)
        stat_row.addWidget(self.vpn_stat_val)
        stat_row.addStretch(1)
        l1.addLayout(stat_row)
        left.addWidget(c1)

        # Card 2: Custom DNS for Wi-Fi Hotspot & Ethernet Sharing
        c_dns, l_dns = self._make_card('lbl_dns', 2)
        self.dns_desc = QLabel()
        self.dns_desc.setObjectName('HintLabel')
        self.dns_desc.setWordWrap(True)
        l_dns.addWidget(self.dns_desc)

        self.lbl_dns_preset = QLabel()
        self.lbl_dns_preset.setObjectName('FieldLabel')
        self.dns_preset_combo = QComboBox()
        for pid, p in DNS_PRESETS.items():
            self.dns_preset_combo.addItem(p['label_en'], pid)
        self.dns_preset_combo.currentIndexChanged.connect(self._on_dns_preset_changed)
        l_dns.addWidget(self.lbl_dns_preset)
        l_dns.addWidget(self.dns_preset_combo)

        dns_ip_row = QHBoxLayout()
        dns_ip_row.setSpacing(8)
        dns_p_col = QVBoxLayout()
        dns_p_col.setSpacing(4)
        self.lbl_dns_primary = QLabel()
        self.lbl_dns_primary.setObjectName('FieldLabel')
        self.dns_primary = QLineEdit()
        self.dns_primary.setValidator(QRegExpValidator(QRegExp(r'^[0-9.]{0,15}$')))
        self.dns_primary.setStyleSheet('font-family:"Consolas",monospace;font-weight:bold;')
        self.dns_primary.textEdited.connect(self._on_dns_text_edited)
        dns_p_col.addWidget(self.lbl_dns_primary)
        dns_p_col.addWidget(self.dns_primary)

        dns_s_col = QVBoxLayout()
        dns_s_col.setSpacing(4)
        self.lbl_dns_secondary = QLabel()
        self.lbl_dns_secondary.setObjectName('FieldLabel')
        self.dns_secondary = QLineEdit()
        self.dns_secondary.setValidator(QRegExpValidator(QRegExp(r'^[0-9.]{0,15}$')))
        self.dns_secondary.setStyleSheet('font-family:"Consolas",monospace;font-weight:bold;')
        self.dns_secondary.textEdited.connect(self._on_dns_text_edited)
        dns_s_col.addWidget(self.lbl_dns_secondary)
        dns_s_col.addWidget(self.dns_secondary)

        dns_ip_row.addLayout(dns_p_col, 1)
        dns_ip_row.addLayout(dns_s_col, 1)
        l_dns.addLayout(dns_ip_row)

        self.chk_dns_uplink = ToggleSwitch('', on_change=lambda _c: self._sync_dns_status_label())
        self.chk_dns_uplink.lbl.setWordWrap(True)
        l_dns.addWidget(self.chk_dns_uplink)

        dns_btn_row = QHBoxLayout()
        dns_btn_row.setSpacing(8)
        self.btn_dns_apply = QPushButton()
        self.btn_dns_apply.setObjectName('PrimaryBtn')
        self.btn_dns_apply.setCursor(Qt.PointingHandCursor)
        self.btn_dns_apply.clicked.connect(self.apply_dns)

        self.btn_dns_reset = QPushButton()
        self.btn_dns_reset.setObjectName('GhostBtn')
        self.btn_dns_reset.setCursor(Qt.PointingHandCursor)
        self.btn_dns_reset.clicked.connect(self.reset_dns)

        dns_btn_row.addWidget(self.btn_dns_apply, 2)
        dns_btn_row.addWidget(self.btn_dns_reset, 1)
        l_dns.addLayout(dns_btn_row)

        dns_stat_row = QHBoxLayout()
        self.dns_stat_lbl = QLabel()
        self.dns_stat_lbl.setStyleSheet('color:#7d94a1;font-size:11px;font-weight:600;')
        self.dns_stat_val = QLabel('Auto (Conf / DHCP)')
        self.dns_stat_val.setStyleSheet('color:#97ad9e;font-family:"Consolas",monospace;font-size:11px;font-weight:bold;')
        dns_stat_row.addWidget(self.dns_stat_lbl)
        dns_stat_row.addSpacing(6)
        dns_stat_row.addWidget(self.dns_stat_val, 1)
        l_dns.addLayout(dns_stat_row)
        left.addWidget(c_dns)

        # Card 3: Ethernet Cable Sharing (Wi-Fi + Conf -> Ethernet)
        c_eth, l_eth = self._make_card('lbl_eth', 3)
        self.eth_desc = QLabel()
        self.eth_desc.setObjectName('HintLabel')
        self.eth_desc.setWordWrap(True)
        l_eth.addWidget(self.eth_desc)

        self.lbl_eth_iface = QLabel()
        self.lbl_eth_iface.setObjectName('FieldLabel')
        l_eth.addWidget(self.lbl_eth_iface)

        eth_sel_row = QHBoxLayout()
        eth_sel_row.setSpacing(6)
        self.eth_combo = QComboBox()
        self.eth_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.eth_combo.setMinimumContentsLength(10)
        self.eth_combo.addItem('Auto (Physical Ethernet)', '')
        self.btn_eth_refresh = QPushButton('⟳')
        self.btn_eth_refresh.setObjectName('NormalBtn')
        self.btn_eth_refresh.setFixedWidth(40)
        self.btn_eth_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_eth_refresh.clicked.connect(self.refresh_adapters)
        eth_sel_row.addWidget(self.eth_combo, 1)
        eth_sel_row.addWidget(self.btn_eth_refresh)
        l_eth.addLayout(eth_sel_row)

        self.eth_auto_hint = QLabel()
        self.eth_auto_hint.setObjectName('HintLabel')
        self.eth_auto_hint.setWordWrap(True)
        l_eth.addWidget(self.eth_auto_hint)

        eth_btn_row = QHBoxLayout()
        eth_btn_row.setSpacing(8)
        self.btn_eth_start = QPushButton()
        self.btn_eth_start.setObjectName('PrimaryBtn')
        self.btn_eth_start.setCursor(Qt.PointingHandCursor)
        self.btn_eth_start.clicked.connect(self.start_ethernet_share)

        self.btn_eth_stop = QPushButton()
        self.btn_eth_stop.setObjectName('DangerBtn')
        self.btn_eth_stop.setCursor(Qt.PointingHandCursor)
        self.btn_eth_stop.clicked.connect(self.stop_ethernet_share)

        eth_btn_row.addWidget(self.btn_eth_start, 2)
        eth_btn_row.addWidget(self.btn_eth_stop, 1)
        l_eth.addLayout(eth_btn_row)
        self.btn_eth_direct = QPushButton()
        self.btn_eth_direct.setObjectName('NormalBtn')
        self.btn_eth_direct.setCursor(Qt.PointingHandCursor)
        self.btn_eth_direct.clicked.connect(self.start_direct_ethernet_share)
        l_eth.addWidget(self.btn_eth_direct)
        left.addWidget(c_eth)

        # Card 4: Hotspot
        c2, l2 = self._make_card('lbl_hs', 4)
        self.hs_desc = QLabel()
        self.hs_desc.setObjectName('HintLabel')
        self.hs_desc.setWordWrap(True)
        l2.addWidget(self.hs_desc)

        self.lbl_ssid = QLabel()
        self.lbl_ssid.setObjectName('FieldLabel')
        self.hs_ssid = QLineEdit('NEON-NET')
        self.hs_ssid.setMaxLength(32)
        l2.addWidget(self.lbl_ssid)
        l2.addWidget(self.hs_ssid)

        self.lbl_hs_pass = QLabel()
        self.lbl_hs_pass.setObjectName('FieldLabel')
        l2.addWidget(self.lbl_hs_pass)
        hp_row = QHBoxLayout()
        hp_row.setSpacing(6)
        self.hs_pass = QLineEdit()
        self.hs_pass.setMaxLength(63)
        self.hs_pass.setEchoMode(QLineEdit.Password)
        self.hs_pass_eye = QPushButton('SHOW')
        self.hs_pass_eye.setObjectName('EyeBtn')
        self.hs_pass_eye.setCursor(Qt.PointingHandCursor)
        self.hs_pass_eye.clicked.connect(lambda: self._toggle_echo(self.hs_pass, self.hs_pass_eye))
        hp_row.addWidget(self.hs_pass, 1)
        hp_row.addWidget(self.hs_pass_eye)
        l2.addLayout(hp_row)

        hb_row = QHBoxLayout()
        hb_row.setSpacing(8)
        self.btn_hs_start = QPushButton()
        self.btn_hs_start.setObjectName('PrimaryBtn')
        self.btn_hs_start.setCursor(Qt.PointingHandCursor)
        self.btn_hs_start.clicked.connect(self.start_hotspot)

        self.btn_hs_stop = QPushButton()
        self.btn_hs_stop.setObjectName('DangerBtn')
        self.btn_hs_stop.setCursor(Qt.PointingHandCursor)
        self.btn_hs_stop.clicked.connect(self.stop_hotspot)

        hb_row.addWidget(self.btn_hs_start, 2)
        hb_row.addWidget(self.btn_hs_stop, 1)
        l2.addLayout(hb_row)
        self.btn_combined_vpn = QPushButton()
        self.btn_combined_vpn.setObjectName('NormalBtn')
        self.btn_combined_vpn.clicked.connect(lambda: self.start_combined_share(True))
        l2.addWidget(self.btn_combined_vpn)
        self.btn_combined_direct = QPushButton()
        self.btn_combined_direct.setObjectName('NormalBtn')
        self.btn_combined_direct.clicked.connect(lambda: self.start_combined_share(False))
        l2.addWidget(self.btn_combined_direct)

        self.btn_hs_win = QPushButton()
        self.btn_hs_win.setObjectName('GhostBtn')
        self.btn_hs_win.setCursor(Qt.PointingHandCursor)
        self.btn_hs_win.clicked.connect(self.open_hotspot_settings)
        l2.addWidget(self.btn_hs_win)
        left.addWidget(c2)

        # Card 5: ICS Manual Fallback
        c3, l3 = self._make_card('lbl_ics', 5)
        self.ics_desc = QLabel()
        self.ics_desc.setObjectName('HintLabel')
        self.ics_desc.setWordWrap(True)
        l3.addWidget(self.ics_desc)

        self.btn_adapters = QPushButton()
        self.btn_adapters.setObjectName('NormalBtn')
        self.btn_adapters.setCursor(Qt.PointingHandCursor)
        self.btn_adapters.clicked.connect(self.refresh_adapters)
        l3.addWidget(self.btn_adapters)

        self.lbl_pub = QLabel()
        self.lbl_pub.setObjectName('FieldLabel')
        self.pub_combo = QComboBox()
        self.pub_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.pub_combo.setMinimumContentsLength(10)
        l3.addWidget(self.lbl_pub)
        l3.addWidget(self.pub_combo)

        self.lbl_priv = QLabel()
        self.lbl_priv.setObjectName('FieldLabel')
        self.priv_combo = QComboBox()
        self.priv_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.priv_combo.setMinimumContentsLength(10)
        l3.addWidget(self.lbl_priv)
        l3.addWidget(self.priv_combo)

        ir_row = QHBoxLayout()
        ir_row.setSpacing(8)
        self.btn_share = QPushButton()
        self.btn_share.setObjectName('NormalBtn')
        self.btn_share.setCursor(Qt.PointingHandCursor)
        self.btn_share.clicked.connect(self.share_vpn)

        self.btn_stop_ics = QPushButton()
        self.btn_stop_ics.setObjectName('DangerBtn')
        self.btn_stop_ics.setCursor(Qt.PointingHandCursor)
        self.btn_stop_ics.clicked.connect(self.stop_sharing)

        ir_row.addWidget(self.btn_share, 2)
        ir_row.addWidget(self.btn_stop_ics, 1)
        l3.addLayout(ir_row)

        self.failsafe_warn = QLabel()
        self.failsafe_warn.setObjectName('WarnBox')
        self.failsafe_warn.setWordWrap(True)
        l3.addWidget(self.failsafe_warn)
        left.addWidget(c3)
        left.addStretch(1)

        grid.addWidget(self._wrap_scroll(left_inner, fixed_width=428))

        self.vpn_term = NeonTerminal(on_save=self._save_log_dialog)
        grid.addWidget(self.vpn_term, 1)
        self.stack.addWidget(page)

    def _build_diag_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 12)
        lay.setSpacing(14)

        top_card = QFrame()
        top_card.setObjectName('GlassCard')
        add_elevation(top_card, blur=20, alpha=75)
        tl = QHBoxLayout(top_card)
        tl.setContentsMargins(18, 14, 18, 14)
        tl.setSpacing(12)

        info_col = QVBoxLayout()
        info_col.setSpacing(3)
        self.diag_title = QLabel()
        self.diag_title.setStyleSheet('color:#eef6fa;font-size:15px;font-weight:bold;')
        self.diag_sub = QLabel()
        self.diag_sub.setObjectName('HintLabel')
        self.diag_sub.setWordWrap(True)
        info_col.addWidget(self.diag_title)
        info_col.addWidget(self.diag_sub)
        tl.addLayout(info_col, 1)

        self.btn_diag_run = QPushButton()
        self.btn_diag_run.setObjectName('PrimaryBtn')
        self.btn_diag_run.setCursor(Qt.PointingHandCursor)
        self.btn_diag_run.clicked.connect(self.run_diagnostics)

        self.btn_diag_copy = QPushButton()
        self.btn_diag_copy.setObjectName('NormalBtn')
        self.btn_diag_copy.setCursor(Qt.PointingHandCursor)
        self.btn_diag_copy.clicked.connect(self.copy_diag_report)

        self.btn_diag_logs = QPushButton()
        self.btn_diag_logs.setObjectName('GhostBtn')
        self.btn_diag_logs.setCursor(Qt.PointingHandCursor)
        self.btn_diag_logs.clicked.connect(self.open_data_folder)

        tl.addWidget(self.btn_diag_run)
        tl.addWidget(self.btn_diag_copy)
        tl.addWidget(self.btn_diag_logs)
        lay.addWidget(top_card)

        cards_grid = QGridLayout()
        cards_grid.setSpacing(12)
        self.diag_os_card = KpiCard('OPERATING SYSTEM', '...', '')
        self.diag_admin_card = KpiCard('PRIVILEGES (UAC)', '...', '')
        self.diag_ovpn_card = KpiCard('CONF ENGINE', '...', '')
        self.diag_wifi_card = KpiCard('HOTSPOT DRIVER', '...', '')
        cards_grid.addWidget(self.diag_os_card, 0, 0)
        cards_grid.addWidget(self.diag_admin_card, 0, 1)
        cards_grid.addWidget(self.diag_ovpn_card, 1, 0)
        cards_grid.addWidget(self.diag_wifi_card, 1, 1)
        lay.addLayout(cards_grid)

        self.diag_adapters_lbl = QLabel()
        self.diag_adapters_lbl.setStyleSheet('color:#d8e6ee;font-size:13px;font-weight:bold;')
        lay.addWidget(self.diag_adapters_lbl)

        self.diag_table = QTableWidget(0, 4)
        self.diag_table.setAlternatingRowColors(True)
        self.diag_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.diag_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.diag_table.verticalHeader().setVisible(False)
        self.diag_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.diag_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.diag_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.diag_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        lay.addWidget(self.diag_table, 1)

        self.stack.addWidget(page)

    def _install_callback_lg(self):
        if not self.tv_connected:
            self.toast.show_toast('Сначала подключите LG на странице LG', 'warn')
            return
        self._run_async('tv', self.api.install_lg_callback,
                        lambda r: self.toast.show_toast('MSB Callback установлен на LG', 'info') if r.get('ok') else None)

    def _install_callback_samsung(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast('Введите IP Samsung на странице Samsung', 'warn')
            return
        self._run_async('sam', lambda: self.api.install_samsung_callback(ip),
                        lambda r: self.toast.show_toast('MSB Callback установлен на Samsung', 'info') if r.get('ok') else None)

    def _build_callback_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 24, 30, 20)
        heading = QLabel('MSB  /  CALLBACK')
        heading.setStyleSheet('color:#8fffb3;font-size:30px;font-weight:700;letter-spacing:2px;')
        layout.addWidget(heading)
        subtitle = QLabel('TV Repair by Meryosab · Ремонт телевизоров и электроники в Ашхабаде')
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet('color:#9fb7aa;font-size:15px;margin-bottom:10px;')
        layout.addWidget(subtitle)
        actions = QHBoxLayout()
        for label, handler in [('УСТАНОВИТЬ НА LG', self._install_callback_lg), ('УСТАНОВИТЬ НА SAMSUNG', self._install_callback_samsung)]:
            button = QPushButton(label)
            button.setObjectName('PrimaryBtn')
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(handler)
            actions.addWidget(button)
        layout.addLayout(actions)
        panels = QHBoxLayout()
        panels.setSpacing(18)
        details = QFrame()
        details.setObjectName('GlassCard')
        dl = QVBoxLayout(details)
        dl.setContentsMargins(24, 22, 24, 22)
        for title, value in [('СВЯЗАТЬСЯ', '+993 63 70 59 53'), ('E-MAIL', 'tvrepairmsb@gmail.com'), ('АДРЕС', 'Ашхабад, Парахат 3/2, дом 14'), ('ОНЛАЙН', 'tvrepair.meryosab.com'), ('МЕССЕНДЖЕРЫ', 'WhatsApp · Telegram @meryosab_shop')]:
            cap = QLabel(title)
            cap.setStyleSheet('color:#7a9a87;font-size:11px;letter-spacing:1px;')
            text = QLabel(value)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            text.setStyleSheet('color:#e8f4eb;font-size:17px;font-weight:600;')
            dl.addWidget(cap)
            dl.addWidget(text)
        dl.addStretch(1)
        panels.addWidget(details, 1)
        for caption, image, helper in [('САЙТ TV REPAIR', 'msb_website_qr.png', 'Открыть сайт'), ('КОНТАКТНАЯ КАРТОЧКА', 'msb_contact_qr.png', 'Сканируйте и подтвердите сохранение контакта')]:
            card = QFrame()
            card.setObjectName('GlassCard')
            cl = QVBoxLayout(card)
            cl.setContentsMargins(18, 18, 18, 18)
            lbl = QLabel(caption)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet('color:#a5e7b2;font-weight:bold;font-size:12px;')
            qr = QLabel()
            qr.setAlignment(Qt.AlignCenter)
            qr.setPixmap(QPixmap(resource('assets/' + image)).scaled(210, 210, Qt.KeepAspectRatio, Qt.FastTransformation))
            note = QLabel(helper)
            note.setWordWrap(True)
            note.setAlignment(Qt.AlignCenter)
            note.setStyleSheet('color:#9fb7aa;font-size:12px;')
            cl.addWidget(lbl)
            cl.addWidget(qr)
            cl.addWidget(note)
            cl.addStretch(1)
            panels.addWidget(card)
        layout.addLayout(panels)
        footer = QLabel('Приём техники — по предварительному контакту. Данные с tvrepair.meryosab.com.')
        footer.setWordWrap(True)
        footer.setStyleSheet('color:#819a89;font-size:12px;padding-top:14px;')
        layout.addWidget(footer)
        layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_footer(self):
        foot = QFrame()
        foot.setFixedHeight(26)
        foot.setStyleSheet('background:rgba(9,14,18,220);border-top:1px solid #18232b;')
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(20, 0, 6, 0)
        self.footer_lbl = QLabel()
        self.footer_lbl.setStyleSheet('color:#5d7380;font-size:10px;')
        fl.addWidget(self.footer_lbl)
        fl.addStretch(1)
        grip = QSizeGrip(foot)
        grip.setFixedSize(16, 16)
        grip.setStyleSheet('background:transparent;')
        fl.addWidget(grip)
        self.right_layout.addWidget(foot)

    # ------------------------------------------------------------------
    # Translations & Persistence
    # ------------------------------------------------------------------
    def _apply_translations(self):
        self.brand_sub.setText(self.t('app_sub'))
        self.tab_buttons[0].setText('▣   ' + self.t('nav_tv'))
        self.tab_buttons[1].setText('◈   ' + self.t('nav_sam'))
        self.tab_buttons[2].setText('◉   ' + self.t('nav_vpn'))
        self.tab_buttons[3].setText('✦   ' + self.t('nav_diag'))
        self.tab_buttons[4].setText('☎   MSB CALLBACK')
        self.side_status_cap.setText(self.t('side_status'))
        self.side_tv_lbl.setText(self.t('side_tv'))
        self.side_sam_lbl.setText(self.t('side_sam'))
        self.side_vpn_lbl.setText(self.t('side_vpn'))
        self.side_hs_lbl.setText(self.t('side_hs'))
        self.side_eth_lbl.setText(self.t('side_eth'))
        self.fx_switch.setText(self.t('side_fx'))

        self._update_page_header()

        self.lbl_card_conn.setText(self.t('card_conn'))
        self.lbl_ip.setText(self.t('lbl_ip'))
        self.lbl_tv_pass.setText(self.t('lbl_tv_pass'))
        self.tv_pass.setPlaceholderText(self.t('ph_tv_pass'))
        self.chk_os26.setText(self.t('chk_os26'))
        self.hint_tv.setText(self.t('hint_tv'))
        self.btn_connect_tv.setText(self.t('btn_connect_tv'))
        self.btn_disconnect_tv.setText(self.t('btn_disconnect_tv'))

        self.lbl_card_reg.setText(self.t('card_reg'))
        self.lbl_area.setText(self.t('lbl_area'))
        self.lbl_area_custom.setText(self.t('lbl_area_custom'))
        self.region_code_input.setPlaceholderText(self.t('ph_area_custom'))

        cur_code = self.region_code_input.text().strip()
        cur_idx = max(0, self.region_combo.currentIndex())
        self.region_combo.blockSignals(True)
        self.region_combo.clear()
        for en_lbl, ru_lbl, _code, _verified in REGIONS:
            self.region_combo.addItem(ru_lbl if self.lang == 'ru' else en_lbl)
        self.region_combo.addItem(self.t('region_custom_item'))
        if cur_code:
            matched = next((i for i, (_e, _r, c, _v) in enumerate(REGIONS) if c == cur_code), None)
            self.region_combo.setCurrentIndex(matched if matched is not None else len(REGIONS))
        else:
            self.region_combo.setCurrentIndex(min(cur_idx, len(REGIONS)))
        self.region_combo.blockSignals(False)

        self.btn_read.setText(self.t('btn_read'))
        self.btn_verify.setText(self.t('btn_verify'))
        self.btn_write.setText(self.t('btn_write'))
        self.warn_nvram.setText(self.t('warn_nvram'))
        self.lbl_belet.setText(self.t('card_belet'))
        self.belet_desc.setText(self.t('belet_desc'))
        self.btn_belet_install.setText(self.t('btn_belet_install'))
        self.btn_belet_launch.setText(self.t('btn_belet_launch'))
        self.btn_belet_remove.setText(self.t('btn_belet_remove'))

        self.lbl_alem.setText(self.t('card_alemtv'))
        self.alem_desc.setText(self.t('alemtv_desc'))
        self.lbl_alem_portal.setText(self.t('lbl_alem_portal'))
        self.btn_alem_install.setText(self.t('btn_alemtv_install'))
        self.btn_alem_launch.setText(self.t('btn_alemtv_launch'))
        self.btn_alem_remove.setText(self.t('btn_alemtv_remove'))

        self.kpi_tv.lbl.setText(self.t('kpi_tv'))
        if not self.tv_connected:
            backup = self.settings.get('last_tv_area_backup')
            sub = (self.t('backup_prefix') + backup) if backup else self.t('backup_none')
            self.kpi_tv.set_data(self.t('kpi_tv_idle'), '#edf5fa', sub)
        self.kpi_area.lbl.setText(self.t('kpi_area'))
        self.kpi_vpn.lbl.setText(self.t('kpi_vpn'))
        if cur_code:
            self._update_area_kpi(cur_code)
        else:
            self._on_region_changed()

        for term in (self.tv_term, self.sam_term, self.vpn_term):
            term.set_labels(self.t('term_title'), self.t('btn_copy'), self.t('btn_save'), self.t('btn_clear'))

        self.lbl_sam_conn.setText(self.t('sam_card_conn'))
        self.sam_lbl_ip.setText(self.t('sam_lbl_ip'))
        self.btn_sam_scan.setText(self.t('sam_btn_scan'))
        self.sam_lbl_found.setText(self.t('sam_lbl_found'))
        self.sam_lbl_proto.setText(self.t('sam_lbl_proto'))
        self.sam_hint_pair.setText(self.t('sam_hint_pair'))
        self.btn_sam_conn.setText(self.t('sam_btn_conn'))
        self.btn_sam_disc.setText(self.t('sam_btn_disc'))

        self.lbl_sam_svc.setText(self.t('sam_card_svc'))
        self.sam_svc_hint.setText(self.t('sam_svc_hint'))
        self.btn_sam_sm.setText(self.t('sam_btn_sm'))
        self.btn_sam_3spd.setText(self.t('sam_btn_3spd'))
        self.btn_sam_adv.setText(self.t('sam_btn_adv'))
        self.btn_sam_opt.setText(self.t('sam_btn_opt'))
        self.btn_sam_exit.setText(self.t('sam_btn_exit'))
        self.sam_lbl_hosp.setText(self.t('sam_lbl_hosp'))
        self.sam_lbl_localset.setText(self.t('sam_lbl_localset'))
        self._on_sam_localset_changed()
        self.btn_sam_sys_tune.setText(self.t('btn_sys_tune'))
        self.btn_sam_smarthub.setText(self.t('sam_btn_smarthub'))
        self.btn_sam_reset.setText(self.t('sam_btn_reset'))
        self.btn_sam_power.setText(self.t('sam_btn_power'))
        self.lbl_sam_belet.setText(self.t('sam_card_belet'))
        self.sam_belet_desc.setText(self.t('sam_belet_desc'))
        self.btn_sam_sdb_check.setText(self.t('sam_btn_sdb_check'))
        self.btn_sam_cert.setText(self.t('sam_btn_cert'))
        self.btn_sam_belet_install.setText(self.t('sam_btn_belet_install'))
        self.btn_sam_belet_launch.setText(self.t('sam_btn_belet_launch'))
        self.btn_sam_belet_remove.setText(self.t('sam_btn_belet_remove'))

        self.lbl_sam_alem.setText(self.t('sam_card_alemtv'))
        self.sam_alem_desc.setText(self.t('sam_alemtv_desc'))
        self.lbl_sam_alem_portal.setText(self.t('sam_lbl_alem_portal'))
        self.btn_sam_alem_install.setText(self.t('sam_btn_alemtv_install'))
        self.btn_sam_alem_launch.setText(self.t('sam_btn_alemtv_launch'))
        self.btn_sam_alem_remove.setText(self.t('sam_btn_alemtv_remove'))
        self._update_sam_host_ip_label()

        self.kpi_sam.lbl.setText(self.t('sam_kpi_tv'))
        self.kpi_sam_proto.lbl.setText(self.t('sam_kpi_proto'))
        self.kpi_sam_country.lbl.setText(self.t('sam_kpi_country'))
        if not self.sam_connected:
            self.kpi_sam.set_data(self.t('sam_kpi_idle'), '#edf5fa', 'LAN 8002 / 8001')
            self.kpi_sam_proto.set_data(self.t('sam_kpi_proto_idle'), '#8fa4b2', self.sam_port_combo.currentData() or 'auto')
        self.sam_remote_title.setText(self.t('sam_remote_title'))
        self.sam_kb_toggle.setText(self.t('sam_kb_nav'))

        self.btn_sys_tune.setText(self.t('btn_sys_tune'))
        self.btn_reboot.setText(self.t('btn_reboot'))
        self.reboot_hint.setText(self.t('reboot_hint'))

        self.lbl_vpn1.setText(self.t('vpn_card1'))
        self.vpn_card1_desc.setText(self.t('vpn_card1_desc'))
        self.lbl_profile.setText(self.t('lbl_profile'))
        self.btn_browse.setText(self.t('btn_browse'))
        self.btn_refresh_prof.setText(self.t('btn_refresh'))
        self.lbl_vpn_user.setText(self.t('lbl_vpn_user'))
        self.vpn_user.setPlaceholderText(self.t('ph_vpn_user'))
        self.lbl_vpn_pass.setText(self.t('lbl_vpn_pass'))
        self.vpn_pass.setPlaceholderText(self.t('ph_vpn_pass'))
        self.btn_vpn_conn.setText(self.t('btn_vpn_conn'))
        self.btn_vpn_disc.setText(self.t('btn_vpn_disc'))
        self.vpn_stat_lbl.setText(self.t('vpn_stat_lbl'))

        self.lbl_dns.setText(self.t('dns_card'))
        self.dns_desc.setText(self.t('dns_desc'))
        self.lbl_dns_preset.setText(self.t('lbl_dns_preset'))
        cur_dns_mode = self.dns_preset_combo.currentData() or 'auto'
        self.dns_preset_combo.blockSignals(True)
        self.dns_preset_combo.clear()
        for pid, p in DNS_PRESETS.items():
            self.dns_preset_combo.addItem(p['label_ru'] if self.lang == 'ru' else p['label_en'], pid)
        d_idx = self.dns_preset_combo.findData(cur_dns_mode)
        if d_idx >= 0:
            self.dns_preset_combo.setCurrentIndex(d_idx)
        self.dns_preset_combo.blockSignals(False)
        self.lbl_dns_primary.setText(self.t('lbl_dns_primary'))
        self.lbl_dns_secondary.setText(self.t('lbl_dns_secondary'))
        is_auto_dns = (self.dns_preset_combo.currentData() or 'auto') == 'auto'
        self.dns_primary.setPlaceholderText(self.t('ph_dns_auto') if is_auto_dns else self.t('ph_dns_primary'))
        self.dns_secondary.setPlaceholderText(self.t('ph_dns_auto') if is_auto_dns else self.t('ph_dns_secondary'))
        self.chk_dns_uplink.setText(self.t('chk_dns_uplink'))
        self.btn_dns_apply.setText(self.t('btn_dns_apply'))
        self.btn_dns_reset.setText(self.t('btn_dns_reset'))
        self.dns_stat_lbl.setText(self.t('dns_stat_lbl'))
        self._sync_dns_status_label()

        self.lbl_eth.setText(self.t('eth_card'))
        self.eth_desc.setText(self.t('eth_desc'))
        self.lbl_eth_iface.setText(self.t('lbl_eth_iface'))
        self.eth_auto_hint.setText(self.t('eth_auto_hint'))
        self.btn_eth_start.setText(self.t('btn_eth_start'))
        self.btn_eth_direct.setText('Share internet without VPN' if self.lang == 'en' else 'Раздать интернет без VPN')
        self.btn_eth_stop.setText(self.t('btn_eth_stop'))
        if self.eth_combo.count() > 0 and self.eth_combo.itemData(0) == '':
            self.eth_combo.setItemText(0, 'Автовыбор (физический Ethernet)' if self.lang == 'ru' else 'Auto (Physical Ethernet)')

        self.lbl_hs.setText(self.t('hs_card'))
        self.hs_desc.setText(self.t('hs_desc'))
        self.lbl_ssid.setText(self.t('lbl_ssid'))
        self.lbl_hs_pass.setText(self.t('lbl_hs_pass'))
        self.hs_pass.setPlaceholderText(self.t('ph_hs_pass'))
        self.btn_hs_start.setText(self.t('btn_hs_start'))
        self.btn_combined_vpn.setText('Ethernet + hotspot via VPN' if self.lang == 'en' else 'Ethernet + точка через VPN')
        self.btn_combined_direct.setText('Ethernet + hotspot without VPN' if self.lang == 'en' else 'Ethernet + точка без VPN')
        self.btn_hs_stop.setText(self.t('btn_hs_stop'))
        self.btn_hs_win.setText(self.t('btn_hs_win'))

        self.lbl_ics.setText(self.t('ics_card'))
        self.ics_desc.setText(self.t('ics_desc'))
        self.btn_adapters.setText(self.t('btn_adapters'))
        self.lbl_pub.setText(self.t('lbl_pub'))
        self.lbl_priv.setText(self.t('lbl_priv'))
        self.btn_share.setText(self.t('btn_share'))
        self.btn_stop_ics.setText(self.t('btn_stop_ics'))
        self.failsafe_warn.setText(self.t('failsafe_warn'))

        self.diag_title.setText(self.t('diag_title'))
        self.diag_sub.setText(self.t('diag_sub'))
        self.btn_diag_run.setText(self.t('btn_diag_run'))
        self.btn_diag_copy.setText(self.t('btn_diag_copy'))
        self.btn_diag_logs.setText(self.t('btn_diag_logs'))
        self.diag_os_card.lbl.setText(self.t('diag_os'))
        self.diag_admin_card.lbl.setText(self.t('diag_admin'))
        self.diag_ovpn_card.lbl.setText(self.t('diag_ovpn'))
        self.diag_wifi_card.lbl.setText(self.t('diag_wifi'))
        self.diag_adapters_lbl.setText(self.t('diag_adapters'))
        self.diag_table.setHorizontalHeaderLabels([
            self.t('col_name'),
            self.t('col_device'),
            self.t('col_status'),
            self.t('col_ics'),
        ])

        self.footer_lbl.setText(self.t('footer'))
        self._refresh_env_badge()
        self._sync_status_badges()

    def _update_page_header(self):
        titles = [
            ('page_tv_title', 'page_tv_sub'),
            ('page_sam_title', 'page_sam_sub'),
            ('page_vpn_title', 'page_vpn_sub'),
            ('page_diag_title', 'page_diag_sub'),
        ]
        if self.current_tab == 4:
            self.page_title.setText('MSB CALLBACK')
            self.page_sub.setText('TV Repair by Meryosab · Ашхабад')
            return
        tk, sk = titles[self.current_tab]
        self.page_title.setText(self.t(tk))
        self.page_sub.setText(self.t(sk))

    def _restore_saved_inputs(self):
        self.tv_ip.setText(self.settings.get('tv_ip', '192.168.1.100'))
        self.chk_os26.setChecked(bool(self.settings.get('webos26', False)))
        self.sam_ip.setText(self.settings.get('samsung_ip', '192.168.1.101'))
        saved_sam_port = str(self.settings.get('samsung_port', 'auto') or 'auto')
        p_idx = self.sam_port_combo.findData(saved_sam_port)
        if p_idx >= 0:
            self.sam_port_combo.setCurrentIndex(p_idx)
        self.hs_ssid.setText(self.settings.get('hotspot_ssid', 'NEON-NET') or 'NEON-NET')
        self._apply_dns_ui_state(
            self.settings.get('dns_mode', 'auto'),
            self.settings.get('dns_primary', ''),
            self.settings.get('dns_secondary', ''),
            self.settings.get('dns_apply_uplink', False),
        )
        saved_code = str(self.settings.get('region_code', '22282') or '22282').strip()
        self.region_code_input.blockSignals(True)
        self.region_code_input.setText(saved_code)
        self.region_code_input.blockSignals(False)
        matched = next((idx for idx, (_e, _r, code, _v) in enumerate(REGIONS) if code == saved_code), None)
        self.region_combo.blockSignals(True)
        self.region_combo.setCurrentIndex(matched if matched is not None else len(REGIONS))
        self.region_combo.blockSignals(False)
        self._update_area_kpi(saved_code)

    def _refresh_env_badge(self):
        win = self.api.windows_info()
        adm = self.api.is_admin()
        os_tag = win['label']
        if win['is_windows']:
            adm_tag = self.t('admin_ok') if adm else self.t('admin_no')
            self.env_badge.setText(f'{os_tag} · {adm_tag}')
            self.env_badge.setProperty('warn', 'false' if adm else 'true')
        else:
            self.env_badge.setText(f'{os_tag} · Preview')
            self.env_badge.setProperty('warn', 'true')
        self.env_badge.style().unpolish(self.env_badge)
        self.env_badge.style().polish(self.env_badge)

    # ------------------------------------------------------------------
    # Navigation, Drag/Resize & Tray
    # ------------------------------------------------------------------
    def switch_tab(self, index):
        self.current_tab = index
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.tab_buttons):
            btn.setProperty('active', 'true' if i == index else 'false')
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        if hasattr(self, 'page_title'):
            self._update_page_header()

    def _on_fx_toggled(self, enabled):
        self.backdrop.set_fx_enabled(enabled)

    def toggle_fx(self):
        new_state = not self.backdrop.fx_enabled
        self.fx_switch.setChecked(new_state)
        self.backdrop.set_fx_enabled(new_state)

    def toggle_language(self):
        self.lang = 'en' if self.lang == 'ru' else 'ru'
        self.api.save_settings({'language': self.lang})
        self._apply_translations()
        if hasattr(self, '_last_diag'):
            self._render_diagnostics(self._last_diag)

    def _toggle_echo(self, line_edit, btn):
        is_pw = line_edit.echoMode() == QLineEdit.Password
        line_edit.setEchoMode(QLineEdit.Normal if is_pw else QLineEdit.Password)
        btn.setText('HIDE' if is_pw else 'SHOW')

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            self.max_btn.setText('□')
        else:
            self.showMaximized()
            self.max_btn.setText('❐')

    def eventFilter(self, obj, event):
        topbar = getattr(self, 'topbar', None)
        sidebar = getattr(self, 'sidebar', None)
        if obj in (topbar, sidebar):
            if obj is topbar and event.type() == event.MouseButtonDblClick and event.button() == Qt.LeftButton:
                self._toggle_maximize()
                return True
            if event.type() == event.MouseButtonPress and event.button() == Qt.LeftButton:
                child = obj.childAt(event.pos())
                if not isinstance(child, (QPushButton, ToggleSwitch)):
                    wh = self.windowHandle()
                    if wh and hasattr(wh, 'startSystemMove') and wh.startSystemMove():
                        return True
                    self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
                    return True
            if event.type() == event.MouseMove and self._drag_pos is not None:
                if not self.isMaximized():
                    self.move(event.globalPos() - self._drag_pos)
                return True
            if event.type() == event.MouseButtonRelease:
                self._drag_pos = None
        return super().eventFilter(obj, event)

    def _detect_edges(self, pos):
        if self.isMaximized():
            return Qt.Edges()
        m = self.RESIZE_MARGIN
        edges = Qt.Edges()
        if pos.x() <= m:
            edges |= Qt.LeftEdge
        elif pos.x() >= self.width() - m:
            edges |= Qt.RightEdge
        if pos.y() <= m:
            edges |= Qt.TopEdge
        elif pos.y() >= self.height() - m:
            edges |= Qt.BottomEdge
        return edges

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edges = self._detect_edges(event.pos())
            if edges:
                wh = self.windowHandle()
                if wh and hasattr(wh, 'startSystemResize') and wh.startSystemResize(edges):
                    return
                self._resize_edges = edges
                self._resize_origin = event.globalPos()
                self._resize_geom = self.geometry()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_edges:
            delta = event.globalPos() - self._resize_origin
            g = QRectF(self._resize_geom).toRect()
            min_w, min_h = self.minimumWidth(), self.minimumHeight()
            if self._resize_edges & Qt.RightEdge:
                g.setWidth(max(min_w, g.width() + delta.x()))
            if self._resize_edges & Qt.BottomEdge:
                g.setHeight(max(min_h, g.height() + delta.y()))
            if self._resize_edges & Qt.LeftEdge:
                g.setLeft(min(g.right() - min_w, g.left() + delta.x()))
            if self._resize_edges & Qt.TopEdge:
                g.setTop(min(g.bottom() - min_h, g.top() + delta.y()))
            self.setGeometry(g)
            return
        edges = self._detect_edges(event.pos())
        if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeBDiagCursor)
        elif edges & (Qt.LeftEdge | Qt.RightEdge):
            self.setCursor(Qt.SizeHorCursor)
        elif edges & (Qt.TopEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._resize_edges = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if getattr(self, 'current_tab', 0) == 1 and getattr(self, 'sam_kb_toggle', None) and self.sam_kb_toggle.isChecked():
            fw = QApplication.focusWidget()
            if not isinstance(fw, (QLineEdit, QComboBox)):
                key_map = {
                    Qt.Key_Up: 'KEY_UP',
                    Qt.Key_Down: 'KEY_DOWN',
                    Qt.Key_Left: 'KEY_LEFT',
                    Qt.Key_Right: 'KEY_RIGHT',
                    Qt.Key_Return: 'KEY_ENTER',
                    Qt.Key_Enter: 'KEY_ENTER',
                    Qt.Key_Backspace: 'KEY_RETURN',
                    Qt.Key_Escape: 'KEY_EXIT',
                    Qt.Key_0: 'KEY_0',
                    Qt.Key_1: 'KEY_1',
                    Qt.Key_2: 'KEY_2',
                    Qt.Key_3: 'KEY_3',
                    Qt.Key_4: 'KEY_4',
                    Qt.Key_5: 'KEY_5',
                    Qt.Key_6: 'KEY_6',
                    Qt.Key_7: 'KEY_7',
                    Qt.Key_8: 'KEY_8',
                    Qt.Key_9: 'KEY_9',
                }
                sam_code = key_map.get(event.key())
                if sam_code:
                    self.sam_key(sam_code)
                    event.accept()
                    return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'toast') and self.toast.isVisible():
            self.toast.move(
                max(16, self.backdrop.width() - self.toast.width() - 24),
                max(72, self.backdrop.height() - self.toast.height() - 36),
            )

    def _init_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable() or self._app_icon.isNull():
            return
        tray = QSystemTrayIcon(self._app_icon, self)
        menu = QMenu()
        menu.setStyleSheet(
            'QMenu{background:#0f171d;color:#e6eff4;border:1px solid #26493d;padding:4px;}'
            'QMenu::item{padding:6px 16px;}'
            'QMenu::item:selected{background:#183a2c;color:#72ffab;}'
        )
        act_show = menu.addAction('Show NEON')
        act_show.triggered.connect(self._restore_from_tray)
        act_stop_eth = menu.addAction('Stop Ethernet Sharing')
        act_stop_eth.triggered.connect(self.stop_ethernet_share)
        act_stop_hs = menu.addAction('Stop Hotspot')
        act_stop_hs.triggered.connect(self.stop_hotspot)
        act_disc_vpn = menu.addAction('Disconnect Conf')
        act_disc_vpn.triggered.connect(self.disconnect_vpn)
        menu.addSeparator()
        act_exit = menu.addAction('Exit NEON')
        act_exit.triggered.connect(self._exit_completely)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self._restore_from_tray() if reason == QSystemTrayIcon.Trigger else None)
        tray.setToolTip('NEON — Smart TV & Conf Console')
        tray.show()
        self.tray = tray

    def _restore_from_tray(self):
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _exit_completely(self):
        self._force_exit = True
        self.close()

    # ------------------------------------------------------------------
    # Logging, Async Queue & Status Sync
    # ------------------------------------------------------------------
    def log(self, msg, kind=None):
        for line in str(msg).splitlines():
            self.tv_term.append_line(line, kind=kind)
            self.sam_term.append_line(line, kind=kind)
            self.vpn_term.append_line(line, kind=kind)

    def _save_log_dialog(self, text):
        path, _ = QFileDialog.getSaveFileName(self, 'Save NEON Log', 'neon-log.txt', 'Text files (*.txt);;All files (*.*)')
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
                self.toast.show_toast('Log saved: ' + os.path.basename(path), 'info')
            except Exception as exc:
                self.toast.show_toast(str(exc), 'error')

    def _run_async(self, channel, fn, done=None, scan_title=None):
        if channel == 'tv' and self.tv_busy:
            return
        if channel == 'sam' and self.sam_busy:
            return
        if channel == 'net' and self.net_busy:
            return
        if channel == 'tv':
            self.tv_busy = True
            self.tv_term.scanner.start(scan_title or self.t('scanner_title'), self.t('scanner_tag'))
        elif channel == 'sam':
            self.sam_busy = True
            self.sam_term.scanner.start(scan_title or self.t('scanner_title'), self.t('scanner_tag'))
        elif channel == 'net':
            self.net_busy = True
            self.vpn_term.scanner.start(scan_title or self.t('scanner_title'), self.t('scanner_tag'))

        self._sync_status_badges()

        def worker():
            try:
                res = fn()
                self.events.put((channel, res, None, done))
            except Exception as exc:
                self.events.put((channel, None, str(exc), done))

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self):
        try:
            while True:
                channel, result, error, done = self.events.get_nowait()
                if channel == 'tv':
                    self.tv_busy = False
                    self.tv_term.scanner.stop()
                elif channel == 'sam':
                    self.sam_busy = False
                    self.sam_term.scanner.stop()
                elif channel == 'net':
                    self.net_busy = False
                    self.vpn_term.scanner.stop()

                if error:
                    self.log('[!] ' + error, 'bad')
                    self.toast.show_toast(error, 'error')
                elif isinstance(result, dict):
                    out = result.get('output', '')
                    if out:
                        for line in out.splitlines():
                            self.log(line)
                    if result.get('error'):
                        self.log('[!] ' + result['error'], 'bad')
                        self.toast.show_toast(result['error'], 'error')
                    if result.get('disconnected'):
                        self.tv_connected = False
                        self.btn_disconnect_tv.setEnabled(False)
                    if done:
                        done(result)
                elif result is not None and done:
                    done(result)
                self._sync_status_badges()
        except queue.Empty:
            pass

        try:
            sam = self.api.read_samsung_logs()
            for line in sam.get('logs', []):
                self.sam_term.append_line(line)
        except Exception:
            pass

        try:
            vpn = self.api.read_vpn_logs()
            for line in vpn.get('logs', []):
                self.log('[CONF] ' + line)
            new_state = vpn.get('state', 'DISCONNECTED')
            new_share = (bool(vpn.get('hotspot_active')), bool(vpn.get('ethernet_active')))
            prev_share = getattr(self, '_last_share_sig', (False, False))
            if new_state != self.vpn_state or new_share != prev_share:
                if new_state != self.vpn_state:
                    if new_state == 'CONNECTED':
                        self.toast.show_toast('Conf active (Initialization Sequence Completed).', 'info')
                        self.refresh_adapters()
                    elif new_state == 'ERROR':
                        self.toast.show_toast('Conf engine stopped with error. Check live log.', 'error')
                self.vpn_state = new_state
                self._last_share_sig = new_share
                self._sync_status_badges()
        except Exception:
            pass

    def _sync_status_badges(self):
        hs = getattr(self.api, 'hotspot_active', False)
        eth = getattr(self.api, 'ethernet_active', False)
        if self.tv_busy or self.sam_busy or self.net_busy:
            self.status_pill.set_state('CONNECTING', self.t('status_conn_ing'))
        elif eth:
            self.status_pill.set_state('ETHERNET', self.t('status_eth'))
        elif self.vpn_state == 'CONNECTED' and hs:
            self.status_pill.set_state('HOTSPOT', self.t('status_hot'))
        elif self.vpn_state == 'CONNECTED':
            self.status_pill.set_state('VPN', self.t('status_vpn'))
        elif self.vpn_state == 'CONNECTING':
            self.status_pill.set_state('CONNECTING', self.t('status_vpn_ing'))
        elif self.vpn_state == 'ERROR':
            self.status_pill.set_state('ERROR', self.t('status_err'))
        elif self.tv_connected or self.sam_connected:
            self.status_pill.set_state('CONNECTED', self.t('status_conn'))
        else:
            self.status_pill.set_state('DISCONNECTED', self.t('status_disc'))

        # Sidebar dots
        self.side_tv_dot.setStyleSheet(
            f'color:{"#3ee68f" if self.tv_connected else "#5a6e7a"};font-size:9px;background:transparent;'
        )
        self.side_sam_dot.setStyleSheet(
            f'color:{"#36e2ff" if self.sam_connected else "#5a6e7a"};font-size:9px;background:transparent;'
        )
        vpn_dot_c = {
            'CONNECTED': '#5bff9d',
            'CONNECTING': '#ffd166',
            'ERROR': '#ff6b7d',
            'DISCONNECTED': '#5a6e7a',
        }.get(self.vpn_state, '#5a6e7a')
        self.side_vpn_dot.setStyleSheet(f'color:{vpn_dot_c};font-size:9px;background:transparent;')
        self.side_hs_dot.setStyleSheet(
            f'color:{"#a3ff70" if hs else "#5a6e7a"};font-size:9px;background:transparent;'
        )
        self.side_eth_dot.setStyleSheet(
            f'color:{"#45f0ff" if eth else "#5a6e7a"};font-size:9px;background:transparent;'
        )

        # VPN card label & KPI
        vpn_colors = {
            'CONNECTED': '#5ef2a0',
            'CONNECTING': '#ffd166',
            'ERROR': '#ff8292',
            'DISCONNECTED': '#8fa4b2',
        }
        v_col = vpn_colors.get(self.vpn_state, '#8fa4b2')
        extra = ' + ETHERNET' if eth else (' + HOTSPOT' if hs else '')
        v_txt = f'{self.vpn_state}{extra}'
        self.vpn_stat_val.setText(v_txt)
        self.vpn_stat_val.setStyleSheet(f'color:{v_col};font-family:"Consolas",monospace;font-size:11px;font-weight:bold;')

        if self.vpn_state == 'DISCONNECTED' and not hs and not eth:
            self.kpi_vpn.set_data(self.t('kpi_vpn_idle'), '#edf5fa', 'Conf Engine')
        else:
            self.kpi_vpn.set_data(v_txt, v_col, self.api.vpn_profile or 'Conf Engine')

    # ------------------------------------------------------------------
    # TV Actions
    # ------------------------------------------------------------------
    def _lg_remote_connect(self):
        ip = self.tv_ip.text().strip()
        self.lg_remote_status.setText('Подтвердите запрос на телевизоре…')
        def done(result):
            self.lg_remote_status.setText('Пульт: подключён' if result.get('ok') else 'Пульт: отключён')
        self._run_async('tv', lambda: self.api.connect_lg_remote(ip), done, 'Подключение пульта LG')

    def _lg_remote_disconnect(self):
        self._run_async('tv', self.api.disconnect_lg_remote,
                        lambda result: self.lg_remote_status.setText('Пульт: отключён'))

    def _lg_remote_key(self, key):
        def done(result):
            if not result.get('ok'):
                self.lg_remote_status.setText('Пульт: отключён')
        self._run_async('tv', lambda: self.api.lg_remote_send_key(key), done)

    def _update_area_kpi(self, code):
        clean = str(code or '').strip()
        if not clean:
            self.kpi_area.set_data('—', '#8fa4b2', self.t('err_area_code'))
            return
        matched = next((r for r in REGIONS if r[2] == clean), None)
        if matched is not None:
            _en, _ru, pcode, verified = matched
            tag = 'US · Verified' if verified else 'Unverified / Best-effort'
            if self.lang == 'ru':
                tag = 'США · Проверен' if verified else 'Не проверен / Best-effort'
            self.kpi_area.set_data(pcode, '#5ef2a0', tag)
            return
        if clean.isdigit() and 1 <= int(clean) <= 0xFFFFFFFF:
            val = int(clean)
            ci = val & 0x7F
            lc = (val >> 7) & 0x1F
            hw = (val >> 12) & 0xF
            self.kpi_area.set_data(clean, '#ffd166', f"{self.t('custom_tag')} · ci={ci} lc={lc} hw={hw}")
        else:
            self.kpi_area.set_data(clean, '#ff8292', self.t('err_area_code'))

    def _on_region_changed(self):
        idx = self.region_combo.currentIndex()
        if 0 <= idx < len(REGIONS):
            _en, _ru, code, _verified = REGIONS[idx]
            self.region_code_input.blockSignals(True)
            self.region_code_input.setText(code)
            self.region_code_input.blockSignals(False)
            self._update_area_kpi(code)
        else:
            self._update_area_kpi(self.region_code_input.text().strip())
            if self.region_code_input.isVisible():
                self.region_code_input.setFocus()
                self.region_code_input.selectAll()

    def _on_region_code_edited(self, text):
        clean = str(text or '').strip()
        matched = next((i for i, (_e, _r, code, _v) in enumerate(REGIONS) if code == clean), None)
        self.region_combo.blockSignals(True)
        self.region_combo.setCurrentIndex(matched if matched is not None else len(REGIONS))
        self.region_combo.blockSignals(False)
        self._update_area_kpi(clean)

    def connect_tv(self):
        ip = self.tv_ip.text().strip()
        pw = self.tv_pass.text()
        os26 = self.chk_os26.isChecked()
        if not ip or not pw:
            self.toast.show_toast('Enter TV IP address and Developer Mode passphrase.', 'warn')
            return
        self.log(f'$ GET http://{ip}:9991/webos_rsa  (SSH 9922)')

        def done(r):
            if r.get('ok'):
                self.tv_connected = True
                self.btn_disconnect_tv.setEnabled(True)
                clean_ip = r.get('ip', ip)
                self.tv_ip.setText(clean_ip)
                backup = self.settings.get('last_tv_area_backup')
                sub = (self.t('backup_prefix') + backup) if backup else r.get('remote_script', 'SSH 9922')
                self.kpi_tv.set_data(clean_ip, '#5ef2a0', sub)
                self.toast.show_toast(f'Connected to LG webOS ({clean_ip})', 'info')
            else:
                self.tv_connected = False
                self.btn_disconnect_tv.setEnabled(False)
                self.kpi_tv.set_data('Connection failed', '#ff8292', 'Check IP / Key Server')

        self._run_async('tv', lambda: self.api.connect(ip, pw, os26), done)

    def disconnect_tv(self):
        self._run_async(
            'tv',
            self.api.disconnect_tv,
            lambda _r: (
                setattr(self, 'tv_connected', False),
                self.btn_disconnect_tv.setEnabled(False),
                self.kpi_tv.set_data(self.t('kpi_tv_idle'), '#edf5fa', 'SSH 9922'),
            ),
        )

    def tv_action(self, cmd):
        if not self.tv_connected:
            self.toast.show_toast('Connect to the TV first.', 'warn')
            return
        self.log('$ sh change_region.sh ' + cmd)

        def done(r):
            if r.get('current_area'):
                area = r['current_area']
                self.settings['last_tv_area_backup'] = area
                self.kpi_tv.sub.setText(self.t('backup_prefix') + area)
                self.toast.show_toast(f'Current NVRAM area option: {area}', 'info')
            elif r.get('disconnected'):
                self.kpi_tv.set_data(self.t('kpi_tv_idle'), '#edf5fa', 'Session closed')
            elif r.get('ok'):
                self.toast.show_toast('TV command completed.', 'info')

        self._run_async('tv', lambda: self.api.action(cmd), done)

    def write_region(self):
        if not self.tv_connected:
            self.toast.show_toast('Connect to the TV first.', 'warn')
            return
        raw_code = self.region_code_input.text().strip()
        if not raw_code.isdigit() or not (1 <= int(raw_code) <= 0xFFFFFFFF):
            self.toast.show_toast(self.t('err_area_code'), 'warn')
            self.region_code_input.setFocus()
            return
        code = str(int(raw_code))
        if code != raw_code:
            self.region_code_input.setText(code)

        matched = next((r for r in REGIONS if r[2] == code), None)
        if matched is not None:
            label = matched[1] if self.lang == 'ru' else matched[0]
        else:
            label = self.t('region_custom_item')
        backup = self.settings.get('last_tv_area_backup') or 'unknown (run Read Current first)'
        body = (
            f'Target area option: {code} ({label})\n'
            f'Last read NVRAM value: {backup}\n\n'
            'Writing to NVRAM can affect TV apps and regional features. Continue?'
        )
        if self.lang == 'ru':
            body = (
                f'Целевой код региона: {code} ({label})\n'
                f'Последний прочитанный код NVRAM: {backup}\n\n'
                'Запись в память NVRAM влияет на приложения и региональные функции ТВ. Продолжить?'
            )
        dlg = ConfirmDialog(
            self,
            'Write NVRAM Region' if self.lang == 'en' else 'Запись региона в NVRAM',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
            danger=True,
        )
        if dlg.exec_() == ConfirmDialog.Accepted:
            self.settings['region_code'] = code
            self.tv_action(code)

    def reboot_tv(self):
        if not self.tv_connected:
            self.toast.show_toast('Connect to the TV first.', 'warn')
            return
        body = (
            'Reboot the television now? The SSH session will disconnect.'
            if self.lang == 'en'
            else 'Перезагрузить телевизор сейчас? SSH-сессия будет закрыта.'
        )
        dlg = ConfirmDialog(
            self,
            'Reboot TV' if self.lang == 'en' else 'Перезагрузка ТВ',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
            danger=True,
        )
        if dlg.exec_() == ConfirmDialog.Accepted:
            self.tv_action('reboot')

    def open_system_tune(self):
        dlg = SystemTuneDialog(self, lang=self.lang)
        dlg.exec_()

    def install_local_lg_ipk(self):
        if not self.tv_connected:
            self.toast.show_toast('Сначала подключите LG по SSH', 'warn')
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Выберите приложение для LG', '', 'webOS IPK (*.ipk)')
        if not path:
            return
        body = ('Установить файл: ' + os.path.basename(path) + '?\n'
                'Используйте только доверенные приложения. Существующее приложение с тем же ID может быть обновлено. IPK не переподписывается.')
        if ConfirmDialog(self, 'Установка IPK на LG', body, 'Установить', 'Отмена').exec_() != ConfirmDialog.Accepted:
            return
        self._run_async('tv', lambda: self.api.install_local_lg_ipk(path),
                        lambda r: self.toast.show_toast('LG подтвердил установку', 'info') if r.get('ok') else None)

    def install_belet(self):
        if not self.tv_connected:
            self.toast.show_toast(
                'Сначала подключитесь к телевизору.' if self.lang == 'ru' else 'Connect to the TV first.',
                'warn',
            )
            return
        self.log('$ luna-send-pub dev/install tm.belet.webos.ipk')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Belet установлен на ТВ! Нажмите «Запустить» или откройте с пульта.'
                    if self.lang == 'ru'
                    else 'Belet installed on TV! Click Launch or open from Home.',
                    'info',
                )

        self._run_async('tv', self.api.install_belet_app, done)

    def launch_belet(self):
        if not self.tv_connected:
            self.toast.show_toast(
                'Сначала подключитесь к телевизору.' if self.lang == 'ru' else 'Connect to the TV first.',
                'warn',
            )
            return
        self.log('$ luna-send-pub launch tm.belet.webos')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Запрос на запуск Belet отправлен на ТВ.'
                    if self.lang == 'ru'
                    else 'Belet launch command sent to TV.',
                    'info',
                )

        self._run_async('tv', self.api.launch_belet_app, done)

    def remove_belet(self):
        if not self.tv_connected:
            self.toast.show_toast(
                'Сначала подключитесь к телевизору.' if self.lang == 'ru' else 'Connect to the TV first.',
                'warn',
            )
            return
        body = (
            'Удалить приложение Belet (tm.belet.webos) с телевизора?'
            if self.lang == 'ru'
            else 'Uninstall Belet (tm.belet.webos) from the TV?'
        )
        dlg = ConfirmDialog(
            self,
            'Удаление Belet' if self.lang == 'ru' else 'Remove Belet App',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
            danger=True,
        )
        if dlg.exec_() != ConfirmDialog.Accepted:
            return
        self.log('$ luna-send-pub dev/remove tm.belet.webos')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Приложение Belet удалено с ТВ.' if self.lang == 'ru' else 'Belet removed from TV.',
                    'info',
                )

        self._run_async('tv', self.api.remove_belet_app, done)

    def install_alem(self):
        if not self.tv_connected:
            self.toast.show_toast(
                'Сначала подключитесь к телевизору.' if self.lang == 'ru' else 'Connect to the TV first.',
                'warn',
            )
            return
        portal = self.alem_portal_combo.currentData() or ALEMTV_DEFAULT_PORTAL
        self.log(f'$ luna-send-pub dev/install tm.alemtv.webos.ipk ({portal})')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Alem TV установлен на ТВ! Нажмите «Запустить» или откройте с пульта.'
                    if self.lang == 'ru'
                    else 'Alem TV installed on TV! Click Launch or open from Home.',
                    'info',
                )

        self._run_async('tv', lambda: self.api.install_alemtv_app(target_url=portal), done)

    def launch_alem(self):
        if not self.tv_connected:
            self.toast.show_toast(
                'Сначала подключитесь к телевизору.' if self.lang == 'ru' else 'Connect to the TV first.',
                'warn',
            )
            return
        self.log('$ luna-send-pub launch tm.alemtv.webos')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Запрос на запуск Alem TV отправлен на ТВ.'
                    if self.lang == 'ru'
                    else 'Alem TV launch command sent to TV.',
                    'info',
                )

        self._run_async('tv', self.api.launch_alemtv_app, done)

    def remove_alem(self):
        if not self.tv_connected:
            self.toast.show_toast(
                'Сначала подключитесь к телевизору.' if self.lang == 'ru' else 'Connect to the TV first.',
                'warn',
            )
            return
        body = (
            'Удалить приложение Alem TV (tm.alemtv.webos) с телевизора?'
            if self.lang == 'ru'
            else 'Uninstall Alem TV (tm.alemtv.webos) from the TV?'
        )
        dlg = ConfirmDialog(
            self,
            'Удаление Alem TV' if self.lang == 'ru' else 'Remove Alem TV App',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
            danger=True,
        )
        if dlg.exec_() != ConfirmDialog.Accepted:
            return
        self.log('$ luna-send-pub dev/remove tm.alemtv.webos')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Приложение Alem TV удалено с ТВ.' if self.lang == 'ru' else 'Alem TV removed from TV.',
                    'info',
                )

        self._run_async('tv', self.api.remove_alemtv_app, done)

    # ------------------------------------------------------------------
    # Samsung Smart TV Actions
    # ------------------------------------------------------------------
    def _on_sam_localset_changed(self):
        idx = self.sam_localset_combo.currentIndex()
        if 0 <= idx < len(SAMSUNG_LOCAL_SETS):
            code, en_desc, ru_desc = SAMSUNG_LOCAL_SETS[idx]
            desc = ru_desc if self.lang == 'ru' else en_desc
            if self.lang == 'ru':
                self.sam_localset_hint.setText(
                    f'{code}: {desc}. Выберите в Service Menu: Option → MRT Option → Local Set (или Option → Local Set).'
                )
            else:
                self.sam_localset_hint.setText(
                    f'{code}: {desc}. Select inside Service Menu: Option → MRT Option → Local Set.'
                )

    def _on_sam_found_selected(self):
        ip = self.sam_found_combo.currentData()
        if ip:
            self.sam_ip.setText(str(ip))
            self._update_sam_host_ip_label()

    def discover_samsung(self):
        self.log('$ SSDP M-SEARCH Samsung TVs on LAN...')

        def done(r):
            devs = r.get('devices') or []
            self._sam_found_devices = devs
            self.sam_found_combo.blockSignals(True)
            self.sam_found_combo.clear()
            for d in devs:
                label = f"{d.get('name', 'Samsung')} ({d.get('model', '')}) · {d.get('ip')} [{d.get('port')}]"
                self.sam_found_combo.addItem(label, d.get('ip'))
            self.sam_found_combo.blockSignals(False)
            self.sam_found_box.setVisible(bool(devs))
            if devs:
                self.sam_ip.setText(devs[0].get('ip', ''))
                self._update_sam_host_ip_label()
                self.log(f'[+] Found {len(devs)} Samsung TV(s): ' + ', '.join(d.get('ip', '') for d in devs), 'ok')
                self.toast.show_toast(
                    f'Найдено ТВ: {len(devs)}' if self.lang == 'ru' else f'Found {len(devs)} Samsung TV(s)',
                    'info',
                )
            else:
                self.log('[!] No Samsung TV responded to SSDP; enter IP manually and ensure TV is ON.', 'warn')
                self.toast.show_toast(
                    'ТВ не найден по SSDP — введите IP вручную и включите ТВ.'
                    if self.lang == 'ru'
                    else 'No Samsung TV found via SSDP; enter IP manually with TV powered ON.',
                    'warn',
                )

        self._run_async('sam', self.api.discover_samsung, done)

    def connect_samsung(self):
        ip = self.sam_ip.text().strip()
        port_mode = self.sam_port_combo.currentData() or 'auto'
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        self.log(f'$ Samsung connect {ip} (mode={port_mode}) — accept prompt on TV if shown...')

        def done(r):
            if r.get('ok'):
                self.sam_connected = True
                self.btn_sam_disc.setEnabled(True)
                clean_ip = r.get('ip', ip)
                self.sam_ip.setText(clean_ip)
                self._update_sam_host_ip_label()
                info = r.get('info') or {}
                tv_name = info.get('name') or 'Samsung TV'
                tv_model = info.get('model') or ''
                tv_os = info.get('os') or 'Tizen'
                self.kpi_sam.set_data(clean_ip, '#36e2ff', f'{tv_name} · {tv_model} ({tv_os})')
                tok_note = 'Token saved' if r.get('token_saved') else 'Session open'
                if self.lang == 'ru':
                    tok_note = 'Токен сохранён' if r.get('token_saved') else 'Сессия открыта'
                self.kpi_sam_proto.set_data(str(r.get('protocol', '')).upper(), '#5ef2a0', tok_note)
                country = info.get('country') or '—'
                lang_code = info.get('language') or ''
                pwr = info.get('power_state') or 'on'
                self.kpi_sam_country.set_data(
                    f'{country}' + (f' / {lang_code}' if lang_code else ''),
                    '#72ffab',
                    f'PowerState: {pwr}',
                )
                self.toast.show_toast(
                    f'Подключено к Samsung ({clean_ip})' if self.lang == 'ru' else f'Connected to Samsung ({clean_ip})',
                    'info',
                )
            else:
                self.sam_connected = False
                self.btn_sam_disc.setEnabled(False)
                self.kpi_sam.set_data('Connection failed', '#ff8292', 'Verify TV is ON & same LAN')

        self._run_async('sam', lambda: self.api.connect_samsung(ip, port=port_mode), done)

    def disconnect_samsung(self):
        self._run_async(
            'sam',
            self.api.disconnect_samsung,
            lambda _r: (
                setattr(self, 'sam_connected', False),
                self.btn_sam_disc.setEnabled(False),
                self.kpi_sam.set_data(self.t('sam_kpi_idle'), '#edf5fa', 'LAN 8002 / 8001'),
                self.kpi_sam_proto.set_data(self.t('sam_kpi_proto_idle'), '#8fa4b2', 'Disconnected'),
            ),
        )

    def sam_key(self, key_code):
        if not self.sam_connected:
            self.toast.show_toast(
                'Сначала подключитесь к Samsung ТВ.' if self.lang == 'ru' else 'Connect to Samsung TV first.',
                'warn',
            )
            return
        self._run_async('sam', lambda: self.api.samsung_send_key(key_code))

    def sam_action(self, action_id):
        if not self.sam_connected:
            self.toast.show_toast(
                'Сначала подключитесь к Samsung ТВ.' if self.lang == 'ru' else 'Connect to Samsung TV first.',
                'warn',
            )
            return
        if action_id == 'exit_service_menu':
            self.toast.show_toast(
                'Выход (~3 сек, экран моргнёт цветами — не нажимайте повторно)...'
                if self.lang == 'ru'
                else 'Exiting Service Menu (~3s RGB cycle — wait, do not double-click)...',
                'info',
            )
        self.log(f'$ samsung_action {action_id}')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    f'Команда выполнена: {action_id}' if self.lang == 'ru' else f'Executed: {action_id}',
                    'info',
                )

        self._run_async('sam', lambda: self.api.samsung_action(action_id), done)

    def sam_factory_reset(self):
        if not self.sam_connected:
            self.toast.show_toast(
                'Сначала подключитесь к Samsung ТВ.' if self.lang == 'ru' else 'Connect to Samsung TV first.',
                'warn',
            )
            return
        if self.lang == 'ru':
            body = (
                'Выполнить сервисный сброс (Factory Reset)?\n\n'
                '• Убедитесь, что открыто главное окно Service Menu и курсор стоит на «Option»\n'
                '  (пункт «Factory Reset» находится первым внутри Option).\n'
                '• Если меню «Option» уже открыто на экране ТВ — можно нажать «OK» на пульте справа.\n\n'
                'Внимание: будут сброшены настройки, каналы, приложения и токен сопряжения ТВ!'
            )
            title = 'Factory Reset Samsung ТВ'
        else:
            body = (
                'Execute Service Menu Factory Reset?\n\n'
                '• Ensure the top Service Menu is open with "Option" highlighted\n'
                '  (Factory Reset is item #1 inside Option).\n'
                '• All settings, apps, channels, and pairing tokens will be erased!'
            )
            title = 'Samsung TV Factory Reset'
        dlg = ConfirmDialog(
            self,
            title,
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
            danger=True,
        )
        if dlg.exec_() == ConfirmDialog.Accepted:
            self.sam_action('factory_reset_from_option')

    def open_samsung_system_tune(self):
        dlg = SystemTuneDialog(self, lang=self.lang, mode='samsung')
        dlg.exec_()

    def _update_sam_host_ip_label(self):
        try:
            res = self.api.get_local_ip_for_tv(self.sam_ip.text().strip())
            pc_ip = res.get('pc_ip', '192.168.1.x')
        except Exception:
            pc_ip = '192.168.1.x'
        self.sam_host_ip_lbl.setText(self.t('sam_belet_host_prefix') + pc_ip)

    def check_samsung_sdb(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        self.log(f'$ sdb connect {ip}:26101 && sdb capability && 0 getduid')

        def done(r):
            if r.get('ok'):
                duid = r.get('duid', '')
                ver = r.get('platform_version', '?')
                self.toast.show_toast(
                    f'SDB 26101 открыт · Tizen {ver} · DUID {duid}'
                    if self.lang == 'ru'
                    else f'SDB 26101 open · Tizen {ver} · DUID {duid}',
                    'info',
                )
            self._update_sam_host_ip_label()

        self._run_async('sam', lambda: self.api.check_samsung_dev_mode(ip), done)

    def auth_samsung_cert(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        self.log(f'$ samsung-cert provision {ip} (OAuth port 4794)')
        self.toast.show_toast(
            'Откроется вход в Samsung Account в браузере (если нет готового сертификата)...'
            if self.lang == 'ru'
            else 'Checking saved cert or opening Samsung Account sign-in in browser...',
            'info',
        )

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    f'Сертификат Samsung готов (DUID {r.get("duid", "")})'
                    if self.lang == 'ru'
                    else f'Samsung certificate ready (DUID {r.get("duid", "")})',
                    'info',
                )

        self._run_async('sam', lambda: self.api.samsung_auth_create_cert(ip), done)

    def install_local_samsung_wgt(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast('Введите IP Samsung', 'warn')
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Выберите приложение для Samsung', '', 'Tizen WGT (*.wgt)')
        if not path:
            return
        body = ('Подписать и установить файл: ' + os.path.basename(path) + '?\n'
                'Только доверенные приложения. Старая подпись автора будет заменена. Установка может обновить приложение с тем же ID или быть отклонена телевизором.')
        if ConfirmDialog(self, 'Установка WGT на Samsung', body, 'Установить', 'Отмена').exec_() != ConfirmDialog.Accepted:
            return
        self._run_async('sam', lambda: self.api.install_local_samsung_wgt(ip, path),
                        lambda r: self.toast.show_toast('Samsung подтвердил установку', 'info') if r.get('ok') else None)

    def install_samsung_belet_client(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast('Введите IP Samsung', 'warn')
            return
        self.log('[+] Подписываем и устанавливаем отдельный Belet Samsung (BeletFilm1).')
        self._run_async('sam', lambda: self.api.install_samsung_belet_client(ip),
                        lambda r: self.toast.show_toast('Belet Samsung установлен; проверьте запуск на ТВ', 'info') if r.get('ok') else None)

    def install_samsung_belet(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        self.log(f'$ sdb {ip}:26101 install BeletTV001.wgt (belet.tm)')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Belet установлен на Samsung ТВ и запущен!'
                    if self.lang == 'ru'
                    else 'Belet installed on Samsung TV & launched!',
                    'info',
                )

        self._run_async('sam', lambda: self.api.install_samsung_belet(ip), done)

    def launch_samsung_belet(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        self.log(f'$ sdb {ip}:26101 0 was_execute BeletTV001.Belet')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Команда запуска Belet отправлена на Samsung ТВ.'
                    if self.lang == 'ru'
                    else 'Belet launch command sent to Samsung TV.',
                    'info',
                )

        self._run_async('sam', lambda: self.api.launch_samsung_belet(ip), done)

    def remove_samsung_belet(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        body = (
            'Удалить приложение Belet (BeletTV001) с Samsung ТВ?'
            if self.lang == 'ru'
            else 'Uninstall Belet (BeletTV001) from Samsung TV?'
        )
        dlg = ConfirmDialog(
            self,
            'Удаление Belet (Samsung)' if self.lang == 'ru' else 'Remove Belet (Samsung)',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
            danger=True,
        )
        if dlg.exec_() != ConfirmDialog.Accepted:
            return
        self.log(f'$ sdb {ip}:26101 0 vd_appuninstall BeletTV001')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Belet удалён с Samsung ТВ.' if self.lang == 'ru' else 'Belet removed from Samsung TV.',
                    'info',
                )

        self._run_async('sam', lambda: self.api.remove_samsung_belet(ip), done)

    def install_samsung_alemtv(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        portal = self.sam_alem_portal_combo.currentData() or ALEMTV_DEFAULT_PORTAL
        self.log(f'$ sdb {ip}:26101 install AlemTV0001.wgt ({portal})')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Alem TV установлен на Samsung ТВ и запущен!'
                    if self.lang == 'ru'
                    else 'Alem TV installed on Samsung TV & launched!',
                    'info',
                )

        self._run_async('sam', lambda: self.api.install_samsung_alemtv(ip, target_url=portal), done)

    def launch_samsung_alemtv(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        self.log(f'$ sdb {ip}:26101 0 was_execute AlemTV0001.AlemTV')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Команда запуска Alem TV отправлена на Samsung ТВ.'
                    if self.lang == 'ru'
                    else 'Alem TV launch command sent to Samsung TV.',
                    'info',
                )

        self._run_async('sam', lambda: self.api.launch_samsung_alemtv(ip), done)

    def remove_samsung_alemtv(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        body = (
            'Удалить приложение Alem TV (AlemTV0001) с Samsung ТВ?'
            if self.lang == 'ru'
            else 'Uninstall Alem TV (AlemTV0001) from Samsung TV?'
        )
        dlg = ConfirmDialog(
            self,
            'Удаление Alem TV (Samsung)' if self.lang == 'ru' else 'Remove Alem TV (Samsung)',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
            danger=True,
        )
        if dlg.exec_() != ConfirmDialog.Accepted:
            return
        self.log(f'$ sdb {ip}:26101 0 vd_appuninstall AlemTV0001')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Alem TV удалён с Samsung ТВ.' if self.lang == 'ru' else 'Alem TV removed from Samsung TV.',
                    'info',
                )

        self._run_async('sam', lambda: self.api.remove_samsung_alemtv(ip), done)
    # ------------------------------------------------------------------
    def _on_profile_changed(self, profile_name):
        if not profile_name:
            self.vpn_auth_box.setVisible(False)
            return
        info = self.api.inspect_profile(profile_name)
        self.vpn_auth_box.setVisible(bool(info.get('needs_auth')))

    def browse_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Conf Profile', '', 'Conf profile (*.ovpn)')
        if not path:
            return
        res = self.api.remember_profile(path)
        if res.get('error'):
            self.toast.show_toast(res['error'], 'error')
            return
        name = res['profile']
        if self.profile_combo.findText(name) == -1:
            self.profile_combo.addItem(name)
        self.profile_combo.setCurrentText(name)
        self._on_profile_changed(name)
        self.toast.show_toast('Profile selected: ' + name, 'info')

    def refresh_profiles(self):
        def done(r):
            vals = r.get('profiles', [])
            current = self.profile_combo.currentText() or self.settings.get('last_profile', '')
            self.profile_combo.blockSignals(True)
            self.profile_combo.clear()
            self.profile_combo.addItems(vals)
            if current and current in vals:
                self.profile_combo.setCurrentText(current)
            self.profile_combo.blockSignals(False)
            self._on_profile_changed(self.profile_combo.currentText())

        self._run_async('net', self.api.list_profiles, done)

    def connect_vpn(self):
        profile = self.profile_combo.currentText()
        if not profile:
            self.toast.show_toast('Choose a Conf profile (.ovpn) first.', 'warn')
            return
        info = self.api.inspect_profile(profile)
        username = self.vpn_user.text().strip()
        password = self.vpn_pass.text()
        if info.get('needs_auth'):
            self.vpn_auth_box.setVisible(True)
            if not username or not password:
                self.toast.show_toast('This Conf profile requires username and password.', 'warn')
                return
        if not self._collect_and_validate_dns(persist=True):
            return
        self.log(f'$ start conf --config {profile}')

        def done(r):
            if r.get('needs_auth'):
                self.vpn_auth_box.setVisible(True)
            elif r.get('ok'):
                self.toast.show_toast('Conf engine started in background.', 'info')

        self._run_async('net', lambda: self.api.connect_vpn(profile, username, password), done)

    def disconnect_vpn(self):
        self.log('$ disconnect conf engine + stop NEON sharing')
        self._run_async(
            'net',
            self.api.disconnect_vpn,
            lambda r: self.toast.show_toast('Conf disconnected.', 'info') if r.get('ok') else None,
        )

    def _apply_dns_ui_state(self, mode, primary, secondary, apply_uplink=False):
        mode = str(mode or 'auto').strip().lower()
        idx = self.dns_preset_combo.findData(mode)
        if idx < 0:
            idx = 0
            mode = 'auto'
        self.dns_preset_combo.blockSignals(True)
        self.dns_preset_combo.setCurrentIndex(idx)
        self.dns_preset_combo.blockSignals(False)

        self.dns_primary.blockSignals(True)
        self.dns_secondary.blockSignals(True)
        if mode == 'auto':
            self.dns_primary.setText('')
            self.dns_secondary.setText('')
            self.dns_primary.setEnabled(False)
            self.dns_secondary.setEnabled(False)
            self.dns_primary.setPlaceholderText(self.t('ph_dns_auto'))
            self.dns_secondary.setPlaceholderText(self.t('ph_dns_auto'))
        else:
            self.dns_primary.setText(str(primary or '').strip())
            self.dns_secondary.setText(str(secondary or '').strip())
            self.dns_primary.setEnabled(True)
            self.dns_secondary.setEnabled(True)
            self.dns_primary.setPlaceholderText(self.t('ph_dns_primary'))
            self.dns_secondary.setPlaceholderText(self.t('ph_dns_secondary'))
        self.dns_primary.blockSignals(False)
        self.dns_secondary.blockSignals(False)
        self.chk_dns_uplink.setChecked(bool(apply_uplink))
        self._sync_dns_status_label()

    def _sync_dns_status_label(self):
        mode = self.dns_preset_combo.currentData() or 'auto'
        p = self.dns_primary.text().strip()
        s = self.dns_secondary.text().strip()
        servers = [x for x in (p, s) if x]
        if mode == 'auto' or not servers:
            self.dns_stat_val.setText('Авто (Conf / DHCP)' if self.lang == 'ru' else 'Auto (Conf / DHCP)')
            self.dns_stat_val.setStyleSheet('color:#97ad9e;font-family:"Consolas",monospace;font-size:11px;font-weight:bold;')
        else:
            up_tag = ' + Uplink' if self.chk_dns_uplink.isChecked() else ''
            self.dns_stat_val.setText(f"{', '.join(servers)}{up_tag}")
            self.dns_stat_val.setStyleSheet('color:#5ef2a0;font-family:"Consolas",monospace;font-size:11px;font-weight:bold;')

    def _on_dns_preset_changed(self):
        mode = self.dns_preset_combo.currentData() or 'auto'
        preset = DNS_PRESETS.get(mode)
        if mode == 'auto':
            self._apply_dns_ui_state('auto', '', '', self.chk_dns_uplink.isChecked())
            return
        if preset and preset.get('servers'):
            sv = preset['servers']
            self._apply_dns_ui_state(
                mode,
                sv[0],
                sv[1] if len(sv) > 1 else '',
                self.chk_dns_uplink.isChecked(),
            )
            return
        # Custom mode: keep existing values if any, enable inputs
        self._apply_dns_ui_state(
            'custom',
            self.dns_primary.text().strip(),
            self.dns_secondary.text().strip(),
            self.chk_dns_uplink.isChecked(),
        )
        self.dns_primary.setFocus()

    def _on_dns_text_edited(self):
        p = self.dns_primary.text().strip()
        s = self.dns_secondary.text().strip()
        matched_mode = 'custom'
        if p:
            pair = [x for x in (p, s) if x]
            for pid, pr in DNS_PRESETS.items():
                if pid not in ('auto', 'custom') and pr.get('servers') == pair:
                    matched_mode = pid
                    break
        idx = self.dns_preset_combo.findData(matched_mode)
        if idx >= 0 and self.dns_preset_combo.currentIndex() != idx:
            self.dns_preset_combo.blockSignals(True)
            self.dns_preset_combo.setCurrentIndex(idx)
            self.dns_preset_combo.blockSignals(False)
        self._sync_dns_status_label()

    def _collect_and_validate_dns(self, persist=False):
        mode = self.dns_preset_combo.currentData() or 'auto'
        p = self.dns_primary.text().strip()
        s = self.dns_secondary.text().strip()
        uplink = self.chk_dns_uplink.isChecked()
        res = self.api.validate_dns_config(mode, p, s, uplink)
        if not res.get('ok'):
            self.toast.show_toast(res.get('error') or 'Invalid DNS configuration.', 'warn')
            return None
        if persist:
            self.api.save_settings({
                'dns_mode': res['mode'],
                'dns_primary': res['primary'],
                'dns_secondary': res['secondary'],
                'dns_apply_uplink': res['apply_uplink'],
            })
            self.settings.update({
                'dns_mode': res['mode'],
                'dns_primary': res['primary'],
                'dns_secondary': res['secondary'],
                'dns_apply_uplink': res['apply_uplink'],
            })
        return res

    def apply_dns(self):
        cfg = self._collect_and_validate_dns(persist=False)
        if not cfg:
            return
        desc = ', '.join(cfg['servers']) if cfg['servers'] else 'Auto (DHCP)'
        self.log(f"$ apply sharing DNS ({cfg['mode']}): {desc}")

        def done(r):
            if r.get('ok'):
                self._apply_dns_ui_state(
                    r.get('dns_mode', r.get('mode', cfg['mode'])),
                    r.get('dns_primary', r.get('primary', cfg['primary'])),
                    r.get('dns_secondary', r.get('secondary', cfg['secondary'])),
                    r.get('apply_uplink', cfg['apply_uplink']),
                )
                self.toast.show_toast(
                    (f'DNS применён: {desc}' if self.lang == 'ru' else f'DNS applied: {desc}')
                    if cfg['servers']
                    else ('DNS сброшен на Авто (DHCP).' if self.lang == 'ru' else 'DNS reset to Auto (DHCP).'),
                    'info',
                )

        self._run_async(
            'net',
            lambda: self.api.apply_sharing_dns(cfg['mode'], cfg['primary'], cfg['secondary'], cfg['apply_uplink']),
            done,
        )

    def reset_dns(self):
        self.log('$ reset sharing DNS -> Auto (DHCP)')

        def done(r):
            if r.get('ok'):
                self._apply_dns_ui_state('auto', '', '', False)
                self.toast.show_toast(
                    'DNS сброшен на Авто (от Conf / DHCP).' if self.lang == 'ru' else 'DNS reset to Auto (Conf / DHCP).',
                    'info',
                )

        self._run_async('net', self.api.reset_sharing_dns, done)

    def start_ethernet_share(self):
        if not self._collect_and_validate_dns(persist=True):
            return
        eth_name = self.eth_combo.currentData() or ''
        disp = eth_name or ('auto-detected physical Ethernet' if self.lang == 'en' else 'физический Ethernet')
        body = (
            f'Share the active Conf connection out through "{disp}"?\n\n'
            'Windows ICS will assign 192.168.137.1 on Ethernet and route plugged-in TV/device traffic (DHCP) through Conf.'
            if self.lang == 'en'
            else f'Раздавать активное подключение Conf через кабель Ethernet («{disp}»)?\n\n'
            'Windows ICS назначит адрес 192.168.137.1 на Ethernet-порт и направит трафик подключённого ТВ/устройства (DHCP) через Conf.'
        )
        dlg = ConfirmDialog(
            self,
            'Start Ethernet Conf Sharing' if self.lang == 'en' else 'Раздача Conf по Ethernet',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
        )
        if dlg.exec_() != ConfirmDialog.Accepted:
            return
        self.log(f'$ enable Ethernet ICS: Conf -> {disp}')

        def done(r):
            if r.get('ok'):
                eth_used = r.get('ethernet_adapter') or disp
                self._update_sam_host_ip_label()
                self.toast.show_toast(
                    f'Conf shared via Ethernet ({eth_used}) · 192.168.137.1'
                    if self.lang == 'en'
                    else f'Conf раздаётся по Ethernet ({eth_used}) · 192.168.137.1',
                    'info',
                )
            elif r.get('error'):
                self.refresh_adapters()

        self._run_async('net', lambda: self.api.start_vpn_ethernet(eth_name or None), done)

    def start_direct_ethernet_share(self):
        if not self._collect_and_validate_dns(persist=True):
            return
        eth_name = self.eth_combo.currentData() or ''
        body = ('Share the regular internet connection over Ethernet without NEON VPN? '
                'Traffic is not protected by NEON VPN. Stop other sharing first. '
                'Other VPN software may affect routing.' if self.lang == 'en' else
                'Раздать обычный интернет по Ethernet без VPN NEON? '
                'Трафик не защищён VPN NEON. Сначала остановите другую раздачу. '
                'Другие VPN могут влиять на маршрутизацию.')
        dlg = ConfirmDialog(self, 'Direct Ethernet' if self.lang == 'en' else 'Ethernet без VPN',
                            body, self.t('confirm_ok'), self.t('confirm_cancel'))
        if dlg.exec_() != ConfirmDialog.Accepted:
            return
        self.log('$ enable direct Ethernet ICS (no NEON VPN)')
        def done(r):
            if r.get('ok'):
                self._update_sam_host_ip_label()
                self.toast.show_toast('Ethernet: no NEON VPN' if self.lang == 'en' else 'Ethernet: без VPN NEON', 'info')
            elif r.get('error'):
                self.refresh_adapters()
        self._run_async('net', lambda: self.api.start_direct_ethernet(eth_name or None), done)

    def stop_ethernet_share(self):
        self.log('$ stop Ethernet Conf ICS sharing')
        self._run_async(
            'net',
            self.api.stop_vpn_ethernet,
            lambda r: self.toast.show_toast(
                ('Stop failed: ' + r['error']) if r.get('error') else
                ('Ethernet stopped (combined mode stops both outputs).' if self.lang == 'en' else 'Ethernet остановлен (в совместном режиме — оба выхода).'),
                'warn' if r.get('error') else 'info',
            ),
        )

    def start_combined_share(self, use_vpn):
        if not self._collect_and_validate_dns(persist=True):
            return
        ssid, password = self.hs_ssid.text().strip(), self.hs_pass.text()
        eth = self.eth_combo.currentData() or None
        body = ('Share over Ethernet and Wi-Fi together? Requires Hosted Network support. '
                + ('Traffic should use NEON VPN; verify IP and DNS on BOTH devices. This is not a guaranteed kill switch.'
                   if use_vpn else 'Traffic is NOT protected by NEON VPN. Other VPN software may affect routing.')
                if self.lang == 'en' else
                'Раздать интернет одновременно по Ethernet и Wi-Fi? Требуется поддержка Hosted Network. '
                + ('Трафик должен идти через VPN NEON: проверьте IP и DNS на ОБОИХ устройствах. Это не гарантированный kill switch.'
                   if use_vpn else 'Трафик НЕ защищён VPN NEON. Другие VPN могут влиять на маршрутизацию.'))
        dlg = ConfirmDialog(self, 'Ethernet + Wi-Fi', body, self.t('confirm_ok'), self.t('confirm_cancel'))
        if dlg.exec_() != ConfirmDialog.Accepted:
            return
        self.log('$ start combined Ethernet + hotspot (' + ('Conf' if use_vpn else 'direct') + ')')
        def done(result):
            if result.get('ok'):
                self.toast.show_toast('Ethernet + Wi-Fi: check both clients' if self.lang == 'en' else 'Ethernet + Wi-Fi: проверьте оба устройства', 'info')
            else:
                self.refresh_adapters()
        self._run_async('net', lambda: self.api.start_combined_sharing(ssid, password, eth, use_vpn), done)

    def start_hotspot(self):
        if not self._collect_and_validate_dns(persist=True):
            return
        ssid = self.hs_ssid.text().strip()
        password = self.hs_pass.text()
        if len(password) < 8:
            self.toast.show_toast('Hotspot password must be at least 8 characters.', 'warn')
            return
        body = (
            f'Start Wi-Fi hotspot "{ssid}" and route traffic through the active Conf channel?'
            if self.lang == 'en'
            else f'Запустить Wi-Fi точку доступа «{ssid}» и направить трафик через модуль Conf?'
        )
        dlg = ConfirmDialog(
            self,
            'Start Conf Hotspot' if self.lang == 'en' else 'Запуск Conf-точки',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
        )
        if dlg.exec_() != ConfirmDialog.Accepted:
            return
        self.log(f'$ start hotspot "{ssid}" -> Conf ICS')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(f'Hotspot "{ssid}" active. Verify client IP.', 'info')
            elif r.get('hotspot_started'):
                self.refresh_adapters()
                self.toast.show_toast('Hotspot started; select adapters manually below.', 'warn')

        self._run_async('net', lambda: self.api.start_vpn_hotspot(ssid, password), done)

    def stop_hotspot(self):
        self.log('$ stop Wi-Fi hotspot + NEON ICS route')
        self._run_async(
            'net',
            self.api.stop_vpn_hotspot,
            lambda r: self.toast.show_toast(('Stop failed: ' + r['error']) if r.get('error') else ('Hotspot stopped (combined mode stops both outputs).' if self.lang == 'en' else 'Точка остановлена (в совместном режиме — оба выхода).'), 'warn' if r.get('error') else 'info'),
        )

    def open_hotspot_settings(self):
        res = self.api.open_hotspot_settings()
        if res.get('error'):
            self.toast.show_toast(res['error'], 'error')
        elif res.get('output'):
            self.log(res['output'])

    def refresh_adapters(self):
        def done(r):
            adapters = r.get('adapters', [])
            self.adapter_map = {}
            labels = []
            for a in adapters:
                name = str(a.get('name', ''))
                dev = str(a.get('device', ''))
                cat = a.get('category') or self.api._classify_adapter(a)
                st = '[Connected]' if a.get('status') == 2 else '[Disconnected]'
                ics = ' [ICS]' if a.get('shared') else ''
                role = ' [Uplink]' if a.get('has_default_gw') else (' [VPN]' if cat == 'vpn' else '')
                disp = f'{name}  —  {dev}  {st}{role}{ics}' if dev and dev != name else f'{name}  {st}{role}{ics}'
                labels.append(disp)
                self.adapter_map[disp] = name

            self.pub_combo.clear()
            self.priv_combo.clear()
            self.pub_combo.addItems(labels)
            self.priv_combo.addItems(labels)

            prev_eth = self.eth_combo.currentData() or self.settings.get('ethernet_adapter', '')
            self.eth_combo.blockSignals(True)
            self.eth_combo.clear()
            self.eth_combo.addItem(
                'Автовыбор (физический Ethernet)' if self.lang == 'ru' else 'Auto (Physical Ethernet)',
                '',
            )
            eth_items = []
            other_items = []
            for a in adapters:
                name = str(a.get('name', ''))
                dev = str(a.get('device', ''))
                cat = a.get('category') or self.api._classify_adapter(a)
                if cat in ('vpn', 'hotspot', 'wifi'):
                    continue
                is_uplink = bool(a.get('has_default_gw'))
                st = '[Connected]' if a.get('status') == 2 else '[Unplugged]'
                tag = ' [Internet Uplink]' if is_uplink else ''
                disp = f'{name} — {dev} {st}{tag}' if dev and dev != name else f'{name} {st}{tag}'
                entry = (a, name, disp, cat, is_uplink)
                if cat == 'ethernet' and not is_uplink:
                    eth_items.append(entry)
                else:
                    other_items.append(entry)
            eth_items.sort(key=lambda e: 0 if e[0].get('status') == 2 else 1)
            for _a, name, disp, _cat, _up in eth_items + other_items:
                self.eth_combo.addItem(disp, name)
            target_idx = 0
            valid_eth_names = {e[1] for e in eth_items}
            if prev_eth and prev_eth in valid_eth_names:
                idx = self.eth_combo.findData(prev_eth)
                if idx > 0:
                    target_idx = idx
            elif eth_items:
                idx = self.eth_combo.findData(eth_items[0][1])
                if idx > 0:
                    target_idx = idx
            self.eth_combo.setCurrentIndex(target_idx)
            self.eth_combo.blockSignals(False)

            best_pub = self.api._select_vpn_public_adapter(adapters)
            if best_pub:
                for i, a in enumerate(adapters):
                    if a.get('name') == best_pub.get('name'):
                        self.pub_combo.setCurrentIndex(i)
                        break
            for i, a in enumerate(adapters):
                txt = (str(a.get('name', '')) + ' ' + str(a.get('device', ''))).lower()
                if any(k in txt for k in (
                    'microsoft hosted network virtual adapter',
                    'wi-fi direct',
                    'hosted network',
                    'размещенной сети',
                    'размещённой сети',
                    'local area connection*',
                    'подключение по локальной сети*',
                )):
                    self.priv_combo.setCurrentIndex(i)
                    if a.get('status') == 2:
                        break

        self._run_async('net', self.api.network_connections, done)

    def share_vpn(self):
        if not self._collect_and_validate_dns(persist=True):
            return
        pub_disp = self.pub_combo.currentText()
        priv_disp = self.priv_combo.currentText()
        pub = self.adapter_map.get(pub_disp, pub_disp)
        priv = self.adapter_map.get(priv_disp, priv_disp)
        if not pub or not priv or pub == priv:
            self.toast.show_toast('Select two different adapters.', 'warn')
            return
        body = (
            f'Route ICS from "{pub}" to "{priv}"?'
            if self.lang == 'en'
            else f'Направить трафик ICS из «{pub}» → «{priv}»?'
        )
        dlg = ConfirmDialog(
            self,
            'ICS Routing' if self.lang == 'en' else 'Маршрутизация ICS',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
        )
        if dlg.exec_() == ConfirmDialog.Accepted:
            self._run_async(
                'net',
                lambda: self.api.set_sharing(pub, priv),
                lambda r: self.toast.show_toast('ICS route enabled.', 'info') if r.get('ok') else None,
            )

    def stop_sharing(self):
        body = (
            'Disable ALL active Windows Internet Connection Sharing on this PC?'
            if self.lang == 'en'
            else 'Отключить все активные общие подключения ICS на этом ПК?'
        )
        dlg = ConfirmDialog(
            self,
            'Disable All ICS' if self.lang == 'en' else 'Остановка ICS',
            body,
            self.t('confirm_ok'),
            self.t('confirm_cancel'),
            danger=True,
        )
        if dlg.exec_() == ConfirmDialog.Accepted:
            self._run_async(
                'net',
                self.api.stop_sharing,
                lambda r: self.toast.show_toast('All ICS sharing disabled.', 'info') if r.get('ok') else None,
            )

    # ------------------------------------------------------------------
    # Diagnostics Tab
    # ------------------------------------------------------------------
    def run_diagnostics(self):
        self._run_async('diag', self.api.system_diagnostics, self._render_diagnostics)

    def _render_diagnostics(self, diag):
        if not isinstance(diag, dict):
            return
        self._last_diag = diag
        os_info = diag.get('os', {})
        build = os_info.get('build', 0)
        os_label = os_info.get('label', 'Unknown')
        hs_engine = 'WinRT + HostedNet' if os_info.get('supports_winrt_hotspot') else 'Hosted Network'
        self.diag_os_card.set_data(os_label, '#5ef2a0', f'Build {build} · {hs_engine}')

        is_adm = bool(diag.get('is_admin'))
        self.diag_admin_card.set_data(
            'ELEVATED' if is_adm else 'STANDARD USER',
            '#5ef2a0' if is_adm else '#ffd166',
            'Full network control' if is_adm else 'Restart as Admin for ICS/Hotspot',
        )

        ovpn = diag.get('openvpn_path', '')
        self.diag_ovpn_card.set_data(
            'INSTALLED' if ovpn else 'NOT FOUND',
            '#5ef2a0' if ovpn else '#ff8292',
            ovpn or 'Install Conf runtime package once',
        )

        hosted = diag.get('hosted_network_supported')
        if hosted is True:
            wifi_val, wifi_col = 'SUPPORTED (HostedNet)', '#5ef2a0'
        elif hosted is False and os_info.get('supports_winrt_hotspot'):
            wifi_val, wifi_col = 'WIN10/11 MOBILE HOTSPOT', '#5ef2a0'
        elif hosted is False:
            wifi_val, wifi_col = 'DRIVER UNSUPPORTED', '#ff8292'
        else:
            wifi_val, wifi_col = 'N/A ON THIS OS', '#9cb3a3'
        vpn_cnt = len(diag.get('vpn_adapters', []))
        ics_cnt = len(diag.get('active_ics', []))
        self.diag_wifi_card.set_data(wifi_val, wifi_col, f'Conf adapters: {vpn_cnt} · Active ICS: {ics_cnt}')

        adapters = diag.get('adapters', [])
        self.diag_table.setRowCount(len(adapters))
        for row, a in enumerate(adapters):
            name = str(a.get('name', ''))
            dev = str(a.get('device', ''))
            status = 'Connected' if a.get('status') == 2 else 'Disconnected'
            if a.get('shared'):
                stype = a.get('sharing_type')
                ics = 'Public (0)' if stype == 0 else ('Private (1)' if stype == 1 else 'Active')
            else:
                ics = 'Off'
            for col, val in enumerate((name, dev, status, ics)):
                item = QTableWidgetItem(val)
                if col == 2:
                    item.setForeground(QColor('#5ef2a0' if status == 'Connected' else '#7d9284'))
                elif col == 3 and ics != 'Off':
                    item.setForeground(QColor('#e4ff82'))
                self.diag_table.setItem(row, col, item)

    def copy_diag_report(self):
        diag = getattr(self, '_last_diag', None) or self.api.system_diagnostics()
        lines = [
            f"OS: {diag.get('os', {}).get('label')} (build {diag.get('os', {}).get('build')})",
            f"Admin: {diag.get('is_admin')}",
            f"Conf Engine: {diag.get('openvpn_path') or 'NOT FOUND'}",
            f"Hosted Network: {diag.get('hosted_network_supported')}",
            f"Active ICS: {diag.get('active_ics')}",
            f"Conf State: {diag.get('vpn_state')} | Hotspot: {diag.get('hotspot_active')} | Ethernet: {diag.get('ethernet_active')} ({diag.get('ethernet_adapter') or '-'})",
            f"DNS Config: {diag.get('dns_config')}",
            f"Log File: {diag.get('log_path')}",
        ]
        QApplication.clipboard().setText('\n'.join(lines))
        self.toast.show_toast('Diagnostic report copied to clipboard.', 'info')

    def open_data_folder(self):
        folder = os.path.dirname(self.api._log_path())
        os.makedirs(folder, exist_ok=True)
        if platform.system() == 'Windows':
            os.startfile(folder)
        else:
            self.toast.show_toast('Data folder: ' + folder, 'info')

    # ------------------------------------------------------------------
    # Clean Shutdown
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        vpn_or_hs = self.vpn_state in ('CONNECTED', 'CONNECTING') or getattr(self.api, 'hotspot_active', False)
        if not self._force_exit and vpn_or_hs and self.tray is not None:
            body = (
                'Conf or Hotspot is currently active. Stop Conf/Hotspot and exit, or Cancel to keep running?'
                if self.lang == 'en'
                else 'Сейчас активен модуль Conf или точка доступа. Остановить их и выйти?'
            )
            dlg = ConfirmDialog(
                self,
                'Active Conf Session' if self.lang == 'en' else 'Активный модуль Conf',
                body,
                'Stop & Exit' if self.lang == 'en' else 'Остановить и выйти',
                self.t('confirm_cancel'),
                danger=True,
            )
            if dlg.exec_() != ConfirmDialog.Accepted:
                event.ignore()
                return
        self._pump_timer.stop()
        self.backdrop._timer.stop()
        try:
            self.api.close()
        except Exception:
            pass
        if self.tray is not None:
            self.tray.hide()
        event.accept()


def main():
    _setup_windows_environment()
    app = QApplication(sys.argv)
    win = NeonWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
