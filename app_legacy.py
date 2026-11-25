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
from samsung_time_override import DEFAULT_SOURCE_DOMAINS as SAMSUNG_TIME_DEFAULT_SOURCES, TIME_TARGET_PRESETS
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
        self.his_connected = False
        self.tv_busy = False
        self.sam_busy = False
        self.his_busy = False
        self.net_busy = False
        self.vpn_state = 'DISCONNECTED'
        self.adapter_map = {}
        self._sam_found_devices = []
        self._his_found_devices = []
        self._his_pairing_id = ''
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
        icons = ['▣', '◈', '◆', '◉', '✦', '☎']
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
        self.side_his_lbl, self.side_his_dot = self._make_telemetry_row(tl)
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
        self._build_hisense_page()
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

        # New native Belet client (Tizen UI packaged for webOS, tm.belet.neonclient)
        self.belet_lg_hint = QLabel()
        self.belet_lg_hint.setObjectName('HintLabel')
        self.belet_lg_hint.setWordWrap(True)
        bl.addWidget(self.belet_lg_hint)
        self.btn_belet_lg_install = QPushButton()
        self.btn_belet_lg_install.setObjectName('NormalBtn')
        self.btn_belet_lg_install.setCursor(Qt.PointingHandCursor)
        self.btn_belet_lg_install.clicked.connect(self.install_lg_belet_client)
        bl.addWidget(self.btn_belet_lg_install)
        blg_row = QHBoxLayout()
        blg_row.setSpacing(8)
        self.btn_belet_lg_launch = QPushButton()
        self.btn_belet_lg_launch.setObjectName('NormalBtn')
        self.btn_belet_lg_launch.setCursor(Qt.PointingHandCursor)
        self.btn_belet_lg_launch.clicked.connect(self.launch_lg_belet_client)
        self.btn_belet_lg_remove = QPushButton()
        self.btn_belet_lg_remove.setObjectName('DangerBtn')
        self.btn_belet_lg_remove.setCursor(Qt.PointingHandCursor)
        self.btn_belet_lg_remove.clicked.connect(self.remove_lg_belet_client)
        blg_row.addWidget(self.btn_belet_lg_launch, 1)
        blg_row.addWidget(self.btn_belet_lg_remove, 1)
        bl.addLayout(blg_row)
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
        self.btn_sam_belet_install.setToolTip('Новый клиент Belet (BeletFilm1) со встроенным обходом сброшенных часов 1970 и HTTPS (ClockProxy).')
        self.btn_sam_belet_install.clicked.connect(self.install_samsung_belet_client)
        sbl.addWidget(self.btn_sam_belet_install)
        self.btn_sam_belet_direct = QPushButton()
        self.btn_sam_belet_direct.setObjectName('NormalBtn')
        self.btn_sam_belet_direct.setCursor(Qt.PointingHandCursor)
        self.btn_sam_belet_direct.setToolTip('Новый клиент Belet (BeletFilm1) без обхода: прямой HTTPS без ClockProxy (для ТВ с точным временем).')
        self.btn_sam_belet_direct.clicked.connect(self.install_samsung_belet_direct_client)
        sbl.addWidget(self.btn_sam_belet_direct)
        sbl_row = QHBoxLayout()
        sbl_row.setSpacing(8)
        self.btn_sam_belet_launch = QPushButton()
        self.btn_sam_belet_launch.setObjectName('NormalBtn')
        self.btn_sam_belet_launch.setCursor(Qt.PointingHandCursor)
        self.btn_sam_belet_launch.clicked.connect(self.launch_samsung_belet_client)
        self.btn_sam_belet_remove = QPushButton()
        self.btn_sam_belet_remove.setObjectName('DangerBtn')
        self.btn_sam_belet_remove.setCursor(Qt.PointingHandCursor)
        self.btn_sam_belet_remove.clicked.connect(self.remove_samsung_belet_client)
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

        # Card 6: Samsung Time Domain Override (DNS + /openapi/timesync + NTP)
        c_sam_time, stl = self._make_card('lbl_sam_time', 6)
        self.sam_time_desc = QLabel()
        self.sam_time_desc.setObjectName('HintLabel')
        self.sam_time_desc.setWordWrap(True)
        stl.addWidget(self.sam_time_desc)

        self.sam_time_lbl_mode = QLabel()
        self.sam_time_lbl_mode.setObjectName('FieldLabel')
        stl.addWidget(self.sam_time_lbl_mode)
        self.sam_time_mode_combo = QComboBox()
        self.sam_time_mode_combo.addItem('NEON Time Gateway (DNS :53 + HTTP :80 + NTP :123)', 'gateway')
        self.sam_time_mode_combo.addItem('Direct DNS A-record rewrite (DNS :53 -> Target IP)', 'dns_direct')
        stl.addWidget(self.sam_time_mode_combo)

        self.sam_time_lbl_target = QLabel()
        self.sam_time_lbl_target.setObjectName('FieldLabel')
        stl.addWidget(self.sam_time_lbl_target)
        self.sam_time_preset_combo = QComboBox()
        for p in TIME_TARGET_PRESETS:
            self.sam_time_preset_combo.addItem(p['label_ru'], p['id'])
        self.sam_time_preset_combo.addItem('Свой домен или IPv4...', 'custom')
        self.sam_time_preset_combo.currentIndexChanged.connect(self._on_sam_time_preset_changed)
        stl.addWidget(self.sam_time_preset_combo)

        self.sam_time_target_input = QLineEdit('time.cloudflare.com')
        self.sam_time_target_input.setPlaceholderText('time.cloudflare.com / pool.ntp.org / 192.168.1.50 / local')
        self.sam_time_target_input.textChanged.connect(self._on_sam_time_target_edited)
        stl.addWidget(self.sam_time_target_input)

        self.sam_time_lbl_sources = QLabel()
        self.sam_time_lbl_sources.setObjectName('FieldLabel')
        stl.addWidget(self.sam_time_lbl_sources)
        self.sam_time_sources_input = QLineEdit(', '.join(SAMSUNG_TIME_DEFAULT_SOURCES))
        self.sam_time_sources_input.setPlaceholderText('time.samsungcloudsolution.com, time.samsungcloudsolution.net')
        stl.addWidget(self.sam_time_sources_input)

        self.sam_time_chk_check = ToggleSwitch('', checked=True)
        self.sam_time_chk_check.lbl.setWordWrap(True)
        stl.addWidget(self.sam_time_chk_check)

        self.sam_time_chk_autostop = ToggleSwitch('', checked=False)
        self.sam_time_chk_autostop.lbl.setWordWrap(True)
        stl.addWidget(self.sam_time_chk_autostop)

        self.sam_time_lbl_upstream = QLabel()
        self.sam_time_lbl_upstream.setObjectName('FieldLabel')
        stl.addWidget(self.sam_time_lbl_upstream)
        self.sam_time_upstream_input = QLineEdit()
        self.sam_time_upstream_input.setPlaceholderText('Auto (1.1.1.1, 8.8.8.8 / NEON DNS)')
        stl.addWidget(self.sam_time_upstream_input)

        self.sam_time_dns_hint_lbl = QLabel()
        self.sam_time_dns_hint_lbl.setStyleSheet(
            'background:#0a181e;color:#7de8ff;border:1px solid #1e4252;border-radius:8px;'
            'padding:6px 10px;font-family:"Consolas",monospace;font-size:11px;font-weight:bold;'
        )
        self.sam_time_dns_hint_lbl.setWordWrap(True)
        stl.addWidget(self.sam_time_dns_hint_lbl)

        self.sam_time_status_lbl = QLabel()
        self.sam_time_status_lbl.setObjectName('HintLabel')
        self.sam_time_status_lbl.setWordWrap(True)
        stl.addWidget(self.sam_time_status_lbl)

        st_btn_row = QHBoxLayout()
        st_btn_row.setSpacing(8)
        self.btn_sam_time_start = QPushButton()
        self.btn_sam_time_start.setObjectName('PrimaryBtn')
        self.btn_sam_time_start.setCursor(Qt.PointingHandCursor)
        self.btn_sam_time_start.clicked.connect(self.start_samsung_time_override)
        self.btn_sam_time_stop = QPushButton()
        self.btn_sam_time_stop.setObjectName('DangerBtn')
        self.btn_sam_time_stop.setCursor(Qt.PointingHandCursor)
        self.btn_sam_time_stop.setEnabled(False)
        self.btn_sam_time_stop.clicked.connect(self.stop_samsung_time_override)
        st_btn_row.addWidget(self.btn_sam_time_start, 2)
        st_btn_row.addWidget(self.btn_sam_time_stop, 1)
        stl.addLayout(st_btn_row)

        st_diag_row = QHBoxLayout()
        st_diag_row.setSpacing(8)
        self.btn_sam_time_sync = QPushButton()
        self.btn_sam_time_sync.setObjectName('NormalBtn')
        self.btn_sam_time_sync.setCursor(Qt.PointingHandCursor)
        self.btn_sam_time_sync.clicked.connect(self.sync_samsung_time_now)
        self.btn_sam_time_diag = QPushButton()
        self.btn_sam_time_diag.setObjectName('NormalBtn')
        self.btn_sam_time_diag.setCursor(Qt.PointingHandCursor)
        self.btn_sam_time_diag.clicked.connect(self.diagnose_samsung_time_reachability)
        st_diag_row.addWidget(self.btn_sam_time_sync, 1)
        st_diag_row.addWidget(self.btn_sam_time_diag, 1)
        stl.addLayout(st_diag_row)

        self.sam_time_note_nopc = QLabel()
        self.sam_time_note_nopc.setObjectName('HintLabel')
        self.sam_time_note_nopc.setWordWrap(True)
        stl.addWidget(self.sam_time_note_nopc)

        self.sam_time_remote_title = QLabel()
        self.sam_time_remote_title.setObjectName('FieldLabel')
        stl.addWidget(self.sam_time_remote_title)

        self.sam_time_lbl_remote_host = QLabel()
        self.sam_time_lbl_remote_host.setObjectName('HintLabel')
        stl.addWidget(self.sam_time_lbl_remote_host)
        self.sam_time_remote_host = QLineEdit()
        self.sam_time_remote_host.setPlaceholderText('192.168.1.1 / 192.168.1.50 / VPS IP')
        stl.addWidget(self.sam_time_remote_host)

        self.sam_time_lbl_remote_user = QLabel()
        self.sam_time_lbl_remote_user.setObjectName('HintLabel')
        stl.addWidget(self.sam_time_lbl_remote_user)
        rem_ssh_row = QHBoxLayout()
        rem_ssh_row.setSpacing(6)
        self.sam_time_remote_user = QLineEdit('root')
        self.sam_time_remote_user.setPlaceholderText('root')
        self.sam_time_remote_port = QLineEdit('22')
        self.sam_time_remote_port.setFixedWidth(64)
        self.sam_time_remote_port.setPlaceholderText('22')
        rem_ssh_row.addWidget(self.sam_time_remote_user, 1)
        rem_ssh_row.addWidget(self.sam_time_remote_port)
        stl.addLayout(rem_ssh_row)

        self.sam_time_lbl_remote_pass = QLabel()
        self.sam_time_lbl_remote_pass.setObjectName('HintLabel')
        stl.addWidget(self.sam_time_lbl_remote_pass)
        rem_pw_row = QHBoxLayout()
        rem_pw_row.setSpacing(6)
        self.sam_time_remote_pass = QLineEdit()
        self.sam_time_remote_pass.setEchoMode(QLineEdit.Password)
        self.sam_time_remote_pass.setPlaceholderText('SSH password')
        btn_st_pw = QPushButton('SHOW')
        btn_st_pw.setObjectName('GhostBtn')
        btn_st_pw.setFixedWidth(62)
        btn_st_pw.clicked.connect(lambda _c=False, b=btn_st_pw: self._toggle_echo(self.sam_time_remote_pass, b))
        rem_pw_row.addWidget(self.sam_time_remote_pass, 1)
        rem_pw_row.addWidget(btn_st_pw)
        stl.addLayout(rem_pw_row)

        self.btn_sam_time_deploy = QPushButton()
        self.btn_sam_time_deploy.setObjectName('PrimaryBtn')
        self.btn_sam_time_deploy.setCursor(Qt.PointingHandCursor)
        self.btn_sam_time_deploy.clicked.connect(self.deploy_samsung_time_remote)
        stl.addWidget(self.btn_sam_time_deploy)

        st_exp_row = QHBoxLayout()
        st_exp_row.setSpacing(8)
        self.btn_sam_time_export = QPushButton()
        self.btn_sam_time_export.setObjectName('NormalBtn')
        self.btn_sam_time_export.setCursor(Qt.PointingHandCursor)
        self.btn_sam_time_export.clicked.connect(self.export_samsung_time_gateway)
        self.btn_sam_time_snippets = QPushButton()
        self.btn_sam_time_snippets.setObjectName('NormalBtn')
        self.btn_sam_time_snippets.setCursor(Qt.PointingHandCursor)
        self.btn_sam_time_snippets.clicked.connect(self.show_samsung_time_snippets)
        st_exp_row.addWidget(self.btn_sam_time_export, 1)
        st_exp_row.addWidget(self.btn_sam_time_snippets, 1)
        stl.addLayout(st_exp_row)

        left.addWidget(c_sam_time)

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

        txt_row = QHBoxLayout()
        txt_row.setSpacing(6)
        self.sam_text_input = QLineEdit()
        self.sam_text_input.setPlaceholderText('1.1.1.1')
        self.sam_text_input.returnPressed.connect(self.sam_send_text)
        self.btn_sam_send_text = QPushButton()
        self.btn_sam_send_text.setObjectName('NormalBtn')
        self.btn_sam_send_text.setCursor(Qt.PointingHandCursor)
        self.btn_sam_send_text.clicked.connect(self.sam_send_text)
        self.btn_sam_hotel = QPushButton()
        self.btn_sam_hotel.setObjectName('GhostBtn')
        self.btn_sam_hotel.setCursor(Qt.PointingHandCursor)
        self.btn_sam_hotel.clicked.connect(lambda: self.sam_action('hospitality_menu'))
        txt_row.addWidget(self.sam_text_input, 2)
        txt_row.addWidget(self.btn_sam_send_text, 1)
        txt_row.addWidget(self.btn_sam_hotel)
        rem_lay.addLayout(txt_row)

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

    def _build_hisense_page(self):
        page = QWidget()
        grid = QHBoxLayout(page)
        grid.setContentsMargins(20, 16, 20, 12)
        grid.setSpacing(16)

        left_inner = QWidget()
        left_inner.setObjectName('ScrollContent')
        left = QVBoxLayout(left_inner)
        left.setContentsMargins(0, 0, 6, 0)
        left.setSpacing(12)

        # Card 1: Connection & PIN Pairing
        c1, l1 = self._make_card('lbl_his_conn', 1)
        self.his_lbl_ip = QLabel()
        self.his_lbl_ip.setObjectName('FieldLabel')
        ip_row = QHBoxLayout()
        ip_row.setSpacing(6)
        self.his_ip = QLineEdit()
        self.his_ip.setPlaceholderText('192.168.1.102')
        self.his_ip.textChanged.connect(lambda: self._refresh_hisense_status_ui())
        self.btn_his_scan = QPushButton()
        self.btn_his_scan.setObjectName('NormalBtn')
        self.btn_his_scan.setCursor(Qt.PointingHandCursor)
        self.btn_his_scan.clicked.connect(self.his_scan)
        ip_row.addWidget(self.his_ip, 1)
        ip_row.addWidget(self.btn_his_scan)
        l1.addWidget(self.his_lbl_ip)
        l1.addLayout(ip_row)

        self.his_found_wrap = QWidget()
        fw_lay = QVBoxLayout(self.his_found_wrap)
        fw_lay.setContentsMargins(0, 2, 0, 2)
        fw_lay.setSpacing(4)
        self.his_lbl_found = QLabel()
        self.his_lbl_found.setObjectName('FieldLabel')
        self.his_found_combo = QComboBox()
        self.his_found_combo.currentIndexChanged.connect(self._on_his_found_selected)
        fw_lay.addWidget(self.his_lbl_found)
        fw_lay.addWidget(self.his_found_combo)
        self.his_found_wrap.setVisible(False)
        l1.addWidget(self.his_found_wrap)

        self.his_lbl_profile = QLabel()
        self.his_lbl_profile.setObjectName('FieldLabel')
        self.his_profile_combo = QComboBox()
        self.his_profile_combo.addItem('Auto (Static → Modern → Middle → RemoteNow)', 'auto')
        self.his_profile_combo.addItem('Static (Legacy / Toshiba)', 'static')
        self.his_profile_combo.addItem('Modern (VIDAA 2022–2026)', 'modern')
        self.his_profile_combo.addItem('Middle (VIDAA Mid)', 'middle')
        self.his_profile_combo.addItem('RemoteNow (Standard)', 'remotenow')
        l1.addWidget(self.his_lbl_profile)
        l1.addWidget(self.his_profile_combo)

        self.his_hint_pair = QLabel()
        self.his_hint_pair.setObjectName('HintLabel')
        self.his_hint_pair.setWordWrap(True)
        l1.addWidget(self.his_hint_pair)

        row_conn = QHBoxLayout()
        row_conn.setSpacing(8)
        self.btn_his_pair = QPushButton()
        self.btn_his_pair.setObjectName('PrimaryBtn')
        self.btn_his_pair.setCursor(Qt.PointingHandCursor)
        self.btn_his_pair.clicked.connect(self.his_start_pairing)
        self.btn_his_forget = QPushButton()
        self.btn_his_forget.setObjectName('GhostBtn')
        self.btn_his_forget.setCursor(Qt.PointingHandCursor)
        self.btn_his_forget.clicked.connect(self.his_forget_pairing)
        row_conn.addWidget(self.btn_his_pair, 2)
        row_conn.addWidget(self.btn_his_forget, 1)
        l1.addLayout(row_conn)

        self.his_pin_wrap = QFrame()
        self.his_pin_wrap.setStyleSheet('background:#0c171d;border:1px solid #2b6e52;border-radius:10px;')
        pw_lay = QVBoxLayout(self.his_pin_wrap)
        pw_lay.setContentsMargins(10, 9, 10, 9)
        pw_lay.setSpacing(6)
        self.his_lbl_pin = QLabel()
        self.his_lbl_pin.setObjectName('FieldLabel')
        pw_lay.addWidget(self.his_lbl_pin)
        pin_row = QHBoxLayout()
        pin_row.setSpacing(6)
        self.his_pin_input = QLineEdit()
        self.his_pin_input.setMaxLength(4)
        self.his_pin_input.setPlaceholderText('0000')
        self.his_pin_input.returnPressed.connect(self.his_confirm_pin)
        self.btn_his_confirm_pin = QPushButton()
        self.btn_his_confirm_pin.setObjectName('PrimaryBtn')
        self.btn_his_confirm_pin.setCursor(Qt.PointingHandCursor)
        self.btn_his_confirm_pin.clicked.connect(self.his_confirm_pin)
        self.btn_his_cancel_pin = QPushButton()
        self.btn_his_cancel_pin.setObjectName('GhostBtn')
        self.btn_his_cancel_pin.setCursor(Qt.PointingHandCursor)
        self.btn_his_cancel_pin.clicked.connect(self.his_cancel_pin)
        pin_row.addWidget(self.his_pin_input, 1)
        pin_row.addWidget(self.btn_his_confirm_pin, 1)
        pin_row.addWidget(self.btn_his_cancel_pin)
        pw_lay.addLayout(pin_row)
        self.his_pin_wrap.setVisible(False)
        l1.addWidget(self.his_pin_wrap)
        left.addWidget(c1)

        # Card 2: Belet TV
        c2, l2 = self._make_card('lbl_his_belet', 2)
        b_head = QHBoxLayout()
        b_head.setSpacing(10)
        b_ico = QLabel()
        b_ico.setFixedSize(36, 36)
        b_ico_path = resource('assets/belet_ui.png')
        if os.path.isfile(b_ico_path):
            b_ico.setPixmap(QPixmap(b_ico_path).scaled(36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.his_belet_desc = QLabel()
        self.his_belet_desc.setObjectName('HintLabel')
        self.his_belet_desc.setWordWrap(True)
        b_head.addWidget(b_ico)
        b_head.addWidget(self.his_belet_desc, 1)
        l2.addLayout(b_head)

        self.his_lbl_belet_mode = QLabel()
        self.his_lbl_belet_mode.setObjectName('FieldLabel')
        self.his_belet_mode_combo = QComboBox()
        self.his_belet_mode_combo.addItem('Полноценный клиент · Сервер NEON (LAN + Прокси)', 'lan_server')
        self.his_belet_mode_combo.addItem('Полноценный клиент · TV Flash (Память ТВ, без ПК)', 'tv_flash')
        self.his_belet_mode_combo.addItem('Полноценный клиент · Offline Data URI (HTML5)', 'data_url')
        self.his_belet_mode_combo.addItem('Веб-сайт / Свой URL (belet.tm)', 'custom_url')
        self.his_belet_mode_combo.currentIndexChanged.connect(self._on_his_belet_mode_changed)
        l2.addWidget(self.his_lbl_belet_mode)
        l2.addWidget(self.his_belet_mode_combo)

        self.his_belet_url_wrap = QWidget()
        bu_w = QVBoxLayout(self.his_belet_url_wrap)
        bu_w.setContentsMargins(0, 0, 0, 0)
        bu_w.setSpacing(4)
        self.his_lbl_belet_url = QLabel()
        self.his_lbl_belet_url.setObjectName('FieldLabel')
        self.his_belet_url = QLineEdit('https://belet.tm/')
        bu_w.addWidget(self.his_lbl_belet_url)
        bu_w.addWidget(self.his_belet_url)
        self.his_belet_url_wrap.setVisible(False)
        l2.addWidget(self.his_belet_url_wrap)

        b_btns = QHBoxLayout()
        b_btns.setSpacing(8)
        self.btn_his_belet_install = QPushButton()
        self.btn_his_belet_install.setObjectName('PrimaryBtn')
        self.btn_his_belet_install.setCursor(Qt.PointingHandCursor)
        self.btn_his_belet_install.clicked.connect(self.his_install_belet)
        self.btn_his_belet_launch = QPushButton()
        self.btn_his_belet_launch.setObjectName('NormalBtn')
        self.btn_his_belet_launch.setCursor(Qt.PointingHandCursor)
        self.btn_his_belet_launch.clicked.connect(self.his_launch_belet)
        b_btns.addWidget(self.btn_his_belet_install, 2)
        b_btns.addWidget(self.btn_his_belet_launch, 1)
        l2.addLayout(b_btns)
        left.addWidget(c2)

        # Card 3: Alem TV
        c3, l3 = self._make_card('lbl_his_alem', 3)
        a_head = QHBoxLayout()
        a_head.setSpacing(10)
        a_ico = QLabel()
        a_ico.setFixedSize(36, 36)
        a_ico_path = resource('assets/alemtv_ui.png')
        if os.path.isfile(a_ico_path):
            a_ico.setPixmap(QPixmap(a_ico_path).scaled(36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.his_alem_desc = QLabel()
        self.his_alem_desc.setObjectName('HintLabel')
        self.his_alem_desc.setWordWrap(True)
        a_head.addWidget(a_ico)
        a_head.addWidget(self.his_alem_desc, 1)
        l3.addLayout(a_head)

        self.his_lbl_alem_portal = QLabel()
        self.his_lbl_alem_portal.setObjectName('FieldLabel')
        self.his_alem_combo = QComboBox()
        for portal in ALEMTV_PORTALS:
            self.his_alem_combo.addItem(portal['name'], portal['url'])
        self.his_alem_combo.addItem('Свой адрес (URL / IP)', 'custom')
        def_alem_idx = self.his_alem_combo.findData(ALEMTV_DEFAULT_PORTAL)
        if def_alem_idx >= 0:
            self.his_alem_combo.setCurrentIndex(def_alem_idx)
        self.his_alem_combo.currentIndexChanged.connect(self._on_his_alem_portal_changed)
        self.his_alem_custom = QLineEdit()
        self.his_alem_custom.setPlaceholderText('http://192.168.1.50:8080 или http://portal.my.tm')
        self.his_alem_custom.setVisible(False)
        l3.addWidget(self.his_lbl_alem_portal)
        l3.addWidget(self.his_alem_combo)
        l3.addWidget(self.his_alem_custom)

        self.his_lbl_alem_mode = QLabel()
        self.his_lbl_alem_mode.setObjectName('FieldLabel')
        self.his_alem_mode_combo = QComboBox()
        self.his_alem_mode_combo.addItem('Локальный сервер NEON (LAN · Рекомендуется)', 'lan_server')
        self.his_alem_mode_combo.addItem('TV Flash / Браузер ТВ (Открыть / Память ТВ)', 'tv_flash')
        self.his_alem_mode_combo.addItem('Offline Data URI (Без ПК / HTML5)', 'data_url')
        self.his_alem_mode_combo.addItem('Прямой URL портала', 'portal_url')
        l3.addWidget(self.his_lbl_alem_mode)
        l3.addWidget(self.his_alem_mode_combo)

        a_btns = QHBoxLayout()
        a_btns.setSpacing(8)
        self.btn_his_alem_install = QPushButton()
        self.btn_his_alem_install.setObjectName('PrimaryBtn')
        self.btn_his_alem_install.setCursor(Qt.PointingHandCursor)
        self.btn_his_alem_install.clicked.connect(self.his_install_alemtv)
        self.btn_his_alem_launch = QPushButton()
        self.btn_his_alem_launch.setObjectName('NormalBtn')
        self.btn_his_alem_launch.setCursor(Qt.PointingHandCursor)
        self.btn_his_alem_launch.clicked.connect(self.his_launch_alemtv)
        a_btns.addWidget(self.btn_his_alem_install, 2)
        a_btns.addWidget(self.btn_his_alem_launch, 1)
        l3.addLayout(a_btns)
        left.addWidget(c3)

        # Card 4: MSB Callback
        c4, l4 = self._make_card('lbl_his_cb', 4)
        self.his_cb_desc = QLabel()
        self.his_cb_desc.setObjectName('HintLabel')
        self.his_cb_desc.setWordWrap(True)
        l4.addWidget(self.his_cb_desc)

        self.his_lbl_cb_mode = QLabel()
        self.his_lbl_cb_mode.setObjectName('FieldLabel')
        self.his_cb_mode_combo = QComboBox()
        self.his_cb_mode_combo.addItem('Offline Data URI (Без ПК и интернета)', 'data_url')
        self.his_cb_mode_combo.addItem('TV Flash / Браузер ТВ (Память ТВ)', 'tv_flash')
        self.his_cb_mode_combo.addItem('Локальный сервер NEON (LAN)', 'lan_server')
        self.his_cb_mode_combo.addItem('Свой HTTPS / LAN URL', 'custom_url')
        self.his_cb_mode_combo.currentIndexChanged.connect(self._on_his_cb_mode_changed)
        l4.addWidget(self.his_lbl_cb_mode)
        l4.addWidget(self.his_cb_mode_combo)

        self.his_cb_url_wrap = QWidget()
        cb_uw = QVBoxLayout(self.his_cb_url_wrap)
        cb_uw.setContentsMargins(0, 0, 0, 0)
        cb_uw.setSpacing(4)
        self.his_lbl_cb_url = QLabel()
        self.his_lbl_cb_url.setObjectName('FieldLabel')
        self.his_cb_custom_url = QLineEdit()
        self.his_cb_custom_url.setPlaceholderText('https://tvrepair.meryosab.com/callback.html')
        cb_uw.addWidget(self.his_lbl_cb_url)
        cb_uw.addWidget(self.his_cb_custom_url)
        self.his_cb_url_wrap.setVisible(False)
        l4.addWidget(self.his_cb_url_wrap)

        cb_btns = QHBoxLayout()
        cb_btns.setSpacing(8)
        self.btn_his_cb_install = QPushButton()
        self.btn_his_cb_install.setObjectName('PrimaryBtn')
        self.btn_his_cb_install.setCursor(Qt.PointingHandCursor)
        self.btn_his_cb_install.clicked.connect(self.his_install_callback)
        self.btn_his_cb_launch = QPushButton()
        self.btn_his_cb_launch.setObjectName('NormalBtn')
        self.btn_his_cb_launch.setCursor(Qt.PointingHandCursor)
        self.btn_his_cb_launch.clicked.connect(self.his_launch_callback)
        cb_btns.addWidget(self.btn_his_cb_install, 2)
        cb_btns.addWidget(self.btn_his_cb_launch, 1)
        l4.addLayout(cb_btns)
        left.addWidget(c4)

        # Card 5: Custom App & Installed Apps
        c5, l5 = self._make_card('lbl_his_custom', 5)
        self.his_lbl_custom_id = QLabel()
        self.his_lbl_custom_id.setObjectName('FieldLabel')
        self.his_custom_id = QLineEdit('neon.customapp')
        self.his_lbl_custom_title = QLabel()
        self.his_lbl_custom_title.setObjectName('FieldLabel')
        self.his_custom_title = QLineEdit('My TV App')
        self.his_lbl_custom_url = QLabel()
        self.his_lbl_custom_url.setObjectName('FieldLabel')
        self.his_custom_url = QLineEdit()
        self.his_custom_url.setPlaceholderText('https://example.com/')
        self.his_lbl_custom_icon = QLabel()
        self.his_lbl_custom_icon.setObjectName('FieldLabel')
        self.his_custom_icon = QLineEdit()
        self.his_custom_icon.setPlaceholderText('https://example.com/icon.png')
        l5.addWidget(self.his_lbl_custom_id)
        l5.addWidget(self.his_custom_id)
        l5.addWidget(self.his_lbl_custom_title)
        l5.addWidget(self.his_custom_title)
        l5.addWidget(self.his_lbl_custom_url)
        l5.addWidget(self.his_custom_url)
        l5.addWidget(self.his_lbl_custom_icon)
        l5.addWidget(self.his_custom_icon)

        cu_btns = QHBoxLayout()
        cu_btns.setSpacing(8)
        self.btn_his_custom_install = QPushButton()
        self.btn_his_custom_install.setObjectName('NormalBtn')
        self.btn_his_custom_install.setCursor(Qt.PointingHandCursor)
        self.btn_his_custom_install.clicked.connect(self.his_install_custom)
        self.btn_his_custom_launch = QPushButton()
        self.btn_his_custom_launch.setObjectName('GhostBtn')
        self.btn_his_custom_launch.setCursor(Qt.PointingHandCursor)
        self.btn_his_custom_launch.clicked.connect(self.his_launch_custom)
        cu_btns.addWidget(self.btn_his_custom_install, 2)
        cu_btns.addWidget(self.btn_his_custom_launch, 1)
        l5.addLayout(cu_btns)

        self.his_apps_combo = QComboBox()
        l5.addWidget(self.his_apps_combo)
        ap_btns = QHBoxLayout()
        ap_btns.setSpacing(8)
        self.btn_his_list_apps = QPushButton()
        self.btn_his_list_apps.setObjectName('NormalBtn')
        self.btn_his_list_apps.setCursor(Qt.PointingHandCursor)
        self.btn_his_list_apps.clicked.connect(self.his_list_apps)
        self.btn_his_launch_sel = QPushButton()
        self.btn_his_launch_sel.setObjectName('GhostBtn')
        self.btn_his_launch_sel.setCursor(Qt.PointingHandCursor)
        self.btn_his_launch_sel.clicked.connect(self.his_launch_selected_app)
        ap_btns.addWidget(self.btn_his_list_apps, 1)
        ap_btns.addWidget(self.btn_his_launch_sel, 1)
        l5.addLayout(ap_btns)
        left.addWidget(c5)

        # Card 6: Browser Self-Installer & vidaahub.com Bridge
        c6, l6 = self._make_card('lbl_his_portal', 6)
        self.his_portal_desc = QLabel()
        self.his_portal_desc.setObjectName('HintLabel')
        self.his_portal_desc.setWordWrap(True)
        l6.addWidget(self.his_portal_desc)

        self.his_portal_status_lbl = QLabel()
        self.his_portal_status_lbl.setStyleSheet('color:#7de8ff;font-family:"Consolas",monospace;font-size:11px;')
        self.his_portal_status_lbl.setWordWrap(True)
        self.his_portal_status_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l6.addWidget(self.his_portal_status_lbl)

        pt_row1 = QHBoxLayout()
        pt_row1.setSpacing(8)
        self.btn_his_portal_start = QPushButton()
        self.btn_his_portal_start.setObjectName('NormalBtn')
        self.btn_his_portal_start.setCursor(Qt.PointingHandCursor)
        self.btn_his_portal_start.clicked.connect(lambda: self.his_start_portal(False))
        self.btn_his_portal_vidaahub = QPushButton()
        self.btn_his_portal_vidaahub.setObjectName('NormalBtn')
        self.btn_his_portal_vidaahub.setCursor(Qt.PointingHandCursor)
        self.btn_his_portal_vidaahub.clicked.connect(lambda: self.his_start_portal(True))
        pt_row1.addWidget(self.btn_his_portal_start, 1)
        pt_row1.addWidget(self.btn_his_portal_vidaahub, 1)
        l6.addLayout(pt_row1)

        pt_row2 = QHBoxLayout()
        pt_row2.setSpacing(8)
        self.btn_his_portal_open_tv = QPushButton()
        self.btn_his_portal_open_tv.setObjectName('PrimaryBtn')
        self.btn_his_portal_open_tv.setCursor(Qt.PointingHandCursor)
        self.btn_his_portal_open_tv.clicked.connect(self.his_open_portal_on_tv)
        self.btn_his_portal_diag = QPushButton()
        self.btn_his_portal_diag.setObjectName('NormalBtn')
        self.btn_his_portal_diag.setCursor(Qt.PointingHandCursor)
        self.btn_his_portal_diag.clicked.connect(self.his_diagnose_portal)
        self.btn_his_portal_stop = QPushButton()
        self.btn_his_portal_stop.setObjectName('DangerBtn')
        self.btn_his_portal_stop.setCursor(Qt.PointingHandCursor)
        self.btn_his_portal_stop.clicked.connect(self.his_stop_portal)
        pt_row2.addWidget(self.btn_his_portal_open_tv, 2)
        pt_row2.addWidget(self.btn_his_portal_diag, 1)
        pt_row2.addWidget(self.btn_his_portal_stop, 1)
        l6.addLayout(pt_row2)
        left.addWidget(c6)

        left.addStretch(1)
        grid.addWidget(self._wrap_scroll(left_inner, fixed_width=490))

        # Right Column: KPIs + VIDAA Remote + Terminal
        right = QVBoxLayout()
        right.setSpacing(12)

        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)
        self.kpi_his_tv = KpiCard('HISENSE VIDAA', '—', 'MQTT :36669')
        self.kpi_his_pair = KpiCard('PAIRING', '—', 'PIN 4 digits')
        self.kpi_his_portal = KpiCard('PORTAL', '—', 'HTTP / vidaahub')
        kpi_row.addWidget(self.kpi_his_tv)
        kpi_row.addWidget(self.kpi_his_pair)
        kpi_row.addWidget(self.kpi_his_portal)
        right.addLayout(kpi_row)

        rem_card = QFrame()
        rem_card.setObjectName('GlassCard')
        add_elevation(rem_card, blur=20, alpha=70)
        rem_lay = QVBoxLayout(rem_card)
        rem_lay.setContentsMargins(14, 10, 14, 12)
        rem_lay.setSpacing(8)
        r_head = QHBoxLayout()
        r_badge = QLabel('◆')
        r_badge.setObjectName('StepBadge')
        r_badge.setAlignment(Qt.AlignCenter)
        self.his_remote_title = QLabel()
        self.his_remote_title.setObjectName('CardTitle')
        r_head.addWidget(r_badge)
        r_head.addWidget(self.his_remote_title)
        r_head.addStretch(1)
        rem_lay.addLayout(r_head)

        pad_split = QHBoxLayout()
        pad_split.setSpacing(14)
        dpad = QGridLayout()
        dpad.setSpacing(5)
        for r, c, lbl, kcode, obj in [
            (0, 0, 'MENU', 'KEY_MENU', 'SamKeyBtn'),
            (0, 1, '▲', 'KEY_UP', 'SamKeyPrimary'),
            (0, 2, 'INFO', 'KEY_INFO', 'SamKeyBtn'),
            (1, 0, '◀', 'KEY_LEFT', 'SamKeyPrimary'),
            (1, 1, 'OK', 'KEY_OK', 'SamKeyPrimary'),
            (1, 2, '▶', 'KEY_RIGHT', 'SamKeyPrimary'),
            (2, 0, 'BACK', 'KEY_RETURNS', 'SamKeyBtn'),
            (2, 1, '▼', 'KEY_DOWN', 'SamKeyPrimary'),
            (2, 2, 'EXIT', 'KEY_EXIT', 'SamKeyBtn'),
        ]:
            b = QPushButton(lbl)
            b.setObjectName(obj)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=kcode: self.his_send_key(k))
            dpad.addWidget(b, r, c)

        svc_pad = QGridLayout()
        svc_pad.setSpacing(5)
        for r, c, lbl, kcode, obj in [
            (0, 0, 'HOME', 'KEY_HOME', 'SamKeyPrimary'),
            (0, 1, 'APPS', 'KEY_APPS', 'SamKeyBtn'),
            (0, 2, 'MUTE', 'KEY_MUTE', 'SamKeyBtn'),
            (1, 0, 'VOL +', 'KEY_VOLUMEUP', 'SamKeyBtn'),
            (1, 1, 'VOL -', 'KEY_VOLUMEDOWN', 'SamKeyBtn'),
            (1, 2, 'CH +', 'KEY_CHANNELUP', 'SamKeyBtn'),
            (2, 0, 'PLAY', 'KEY_PLAY', 'SamKeyBtn'),
            (2, 1, 'PAUSE', 'KEY_PAUSE', 'SamKeyBtn'),
            (2, 2, 'CH -', 'KEY_CHANNELDOWN', 'SamKeyBtn'),
            (3, 0, 'SOURCE', 'KEY_SOURCE', 'SamKeyBtn'),
            (3, 1, 'STOP', 'KEY_STOP', 'SamKeyBtn'),
            (3, 2, 'POWER', 'KEY_POWER', 'SamKeyWarn'),
        ]:
            b = QPushButton(lbl)
            b.setObjectName(obj)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=kcode: self.his_send_key(k))
            svc_pad.addWidget(b, r, c)

        pad_split.addLayout(dpad, 1)
        pad_split.addLayout(svc_pad, 1)
        rem_lay.addLayout(pad_split)
        right.addWidget(rem_card)

        self.his_term = NeonTerminal(on_save=self._save_log_dialog)
        right.addWidget(self.his_term, 1)

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
        pass_row_widget = QWidget()
        vp_row = QHBoxLayout(pass_row_widget)
        vp_row.setContentsMargins(0, 0, 0, 0)
        vp_row.setSpacing(6)
        self.vpn_pass = QLineEdit()
        self.vpn_pass.setEchoMode(QLineEdit.Password)
        self.vpn_pass_eye = QPushButton('SHOW')
        self.vpn_pass_eye.setObjectName('EyeBtn')
        self.vpn_pass_eye.setCursor(Qt.PointingHandCursor)
        self.vpn_pass_eye.clicked.connect(lambda: self._toggle_echo(self.vpn_pass, self.vpn_pass_eye))
        vp_row.addWidget(self.vpn_pass, 1)
        vp_row.addWidget(self.vpn_pass_eye)
        # Some .ovpn profiles ship a passphrase protected private key: the key password is
        # asked for here and handed to the Conf engine over the local credential pipe.
        self.lbl_vpn_key = QLabel()
        self.lbl_vpn_key.setObjectName('FieldLabel')
        key_row_widget = QWidget()
        vk_row = QHBoxLayout(key_row_widget)
        vk_row.setContentsMargins(0, 0, 0, 0)
        vk_row.setSpacing(6)
        self.vpn_key_pass = QLineEdit()
        self.vpn_key_pass.setEchoMode(QLineEdit.Password)
        self.vpn_key_eye = QPushButton('SHOW')
        self.vpn_key_eye.setObjectName('EyeBtn')
        self.vpn_key_eye.setCursor(Qt.PointingHandCursor)
        self.vpn_key_eye.clicked.connect(lambda: self._toggle_echo(self.vpn_key_pass, self.vpn_key_eye))
        vk_row.addWidget(self.vpn_key_pass, 1)
        vk_row.addWidget(self.vpn_key_eye)
        self.vpn_auth_hint = QLabel()
        self.vpn_auth_hint.setObjectName('HintLabel')
        self.vpn_auth_hint.setWordWrap(True)
        al.addWidget(self.lbl_vpn_user)
        al.addWidget(self.vpn_user)
        al.addWidget(self.lbl_vpn_pass)
        al.addWidget(pass_row_widget)
        al.addWidget(self.lbl_vpn_key)
        al.addWidget(key_row_widget)
        al.addWidget(self.vpn_auth_hint)
        self.vpn_pass_row = pass_row_widget
        self.vpn_key_row = key_row_widget
        self._vpn_auth_info = {}
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
        self.vless_link = QLineEdit()
        self.vless_link.setEchoMode(QLineEdit.Password)
        self.vless_link.setPlaceholderText('vless://…')
        l1.addWidget(self.vless_link)
        self.btn_vless = QPushButton()
        self.btn_vless.setObjectName('PrimaryBtn')
        self.btn_vless.clicked.connect(self.connect_vless)
        l1.addWidget(self.btn_vless)
        self.vless_hint = QLabel()
        self.vless_hint.setObjectName('HintLabel')
        self.vless_hint.setWordWrap(True)
        l1.addWidget(self.vless_hint)
        self.vless_strict = ToggleSwitch(
            '', checked=self.settings.get('vless_strict_route', True) is not False,
            on_change=self._on_vless_strict_toggled)
        l1.addWidget(self.vless_strict)
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

    def _install_callback_hisense(self):
        ip = self.his_ip.text().strip()
        if not ip:
            self.toast.show_toast('Введите IP Hisense на странице Hisense VIDAA', 'warn')
            return
        mode = self.his_cb_mode_combo.currentData() or 'data_url'
        custom_url = self.his_cb_custom_url.text().strip()
        self._run_async(
            'his',
            lambda: self.api.install_hisense_callback(ip, mode=mode, custom_url=custom_url, launch=True),
            lambda r: self.toast.show_toast('MSB Callback установлен на Hisense VIDAA', 'info') if r.get('ok') else None,
        )

    def _build_callback_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 24, 30, 20)
        heading = QLabel('MSB  /  CALLBACK')
        heading.setStyleSheet('color:#8fffb3;font-size:30px;font-weight:700;letter-spacing:2px;')
        layout.addWidget(heading)
        subtitle = QLabel('Meryosab Callback · По всем вопросам обращайтесь по номеру телефона или на сайт (Ашхабад) · Показывает LAN и Wi-Fi IP на ТВ')
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet('color:#9fb7aa;font-size:15px;margin-bottom:10px;')
        layout.addWidget(subtitle)
        actions = QHBoxLayout()
        for label, handler in [
            ('УСТАНОВИТЬ НА LG', self._install_callback_lg),
            ('УСТАНОВИТЬ НА SAMSUNG', self._install_callback_samsung),
            ('УСТАНОВИТЬ НА HISENSE', self._install_callback_hisense),
        ]:
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
        for caption, image, helper in [('ОФИЦИАЛЬНЫЙ САЙТ', 'msb_website_qr.png', 'Открыть сайт'), ('КОНТАКТНАЯ КАРТОЧКА', 'msb_contact_qr.png', 'Сканируйте и подтвердите сохранение контакта')]:
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
        footer = QLabel('По всем вопросам обращайтесь по номеру телефона, в мессенджеры или на сайт tvrepair.meryosab.com. После установки на ТВ отображаются LAN и Wi-Fi IP-адреса.')
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
        self.tab_buttons[2].setText('◆   ' + self.t('nav_his'))
        self.tab_buttons[3].setText('◉   ' + self.t('nav_vpn'))
        self.tab_buttons[4].setText('✦   ' + self.t('nav_diag'))
        self.tab_buttons[5].setText('☎   MSB CALLBACK')
        self.side_status_cap.setText(self.t('side_status'))
        self.side_tv_lbl.setText(self.t('side_tv'))
        self.side_sam_lbl.setText(self.t('side_sam'))
        self.side_his_lbl.setText(self.t('side_his'))
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
        self.belet_lg_hint.setText(self.t('belet_lg_hint'))
        self.btn_belet_lg_install.setText(self.t('btn_belet_lg_install'))
        self.btn_belet_lg_launch.setText(self.t('btn_belet_lg_launch'))
        self.btn_belet_lg_remove.setText(self.t('btn_belet_lg_remove'))

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

        for term in (self.tv_term, self.sam_term, self.his_term, self.vpn_term):
            term.set_labels(self.t('term_title'), self.t('btn_copy'), self.t('btn_save'), self.t('btn_clear'))

        self.lbl_his_conn.setText(self.t('his_card_conn'))
        self.his_lbl_ip.setText(self.t('his_lbl_ip'))
        self.btn_his_scan.setText(self.t('his_btn_scan'))
        self.his_lbl_found.setText(self.t('his_lbl_found'))
        self.his_lbl_profile.setText(self.t('his_lbl_profile'))
        self.his_hint_pair.setText(self.t('his_hint_pair'))
        self.btn_his_pair.setText(self.t('his_btn_pair'))
        self.btn_his_forget.setText(self.t('his_btn_forget'))
        self.his_lbl_pin.setText(self.t('his_lbl_pin'))
        self.btn_his_confirm_pin.setText(self.t('his_btn_confirm_pin'))
        self.btn_his_cancel_pin.setText(self.t('his_btn_cancel_pin'))

        self.lbl_his_belet.setText(self.t('his_card_belet'))
        self.his_belet_desc.setText(self.t('his_belet_desc'))
        self.his_lbl_belet_mode.setText(self.t('his_lbl_belet_mode'))
        self.his_lbl_belet_url.setText(self.t('his_lbl_belet_url'))
        self.btn_his_belet_install.setText(self.t('his_btn_belet_install'))
        self.btn_his_belet_launch.setText(self.t('his_btn_belet_launch'))

        self.lbl_his_alem.setText(self.t('his_card_alem'))
        self.his_alem_desc.setText(self.t('his_alem_desc'))
        self.his_lbl_alem_portal.setText(self.t('his_lbl_alem_portal'))
        self.his_lbl_alem_mode.setText(self.t('his_lbl_alem_mode'))
        self.btn_his_alem_install.setText(self.t('his_btn_alem_install'))
        self.btn_his_alem_launch.setText(self.t('his_btn_alem_launch'))

        self.lbl_his_cb.setText(self.t('his_card_cb'))
        self.his_cb_desc.setText(self.t('his_cb_desc'))
        self.his_lbl_cb_mode.setText(self.t('his_lbl_cb_mode'))
        self.his_lbl_cb_url.setText(self.t('his_lbl_cb_url'))
        self.btn_his_cb_install.setText(self.t('his_btn_cb_install'))
        self.btn_his_cb_launch.setText(self.t('his_btn_cb_launch'))

        self.lbl_his_custom.setText(self.t('his_card_custom'))
        self.his_lbl_custom_id.setText(self.t('his_lbl_custom_id'))
        self.his_lbl_custom_title.setText(self.t('his_lbl_custom_title'))
        self.his_lbl_custom_url.setText(self.t('his_lbl_custom_url'))
        self.his_lbl_custom_icon.setText(self.t('his_lbl_custom_icon'))
        self.btn_his_custom_install.setText(self.t('his_btn_custom_install'))
        self.btn_his_custom_launch.setText(self.t('his_btn_custom_launch'))
        self.btn_his_list_apps.setText(self.t('his_btn_list_apps'))
        self.btn_his_launch_sel.setText(self.t('his_btn_launch_sel'))

        self.lbl_his_portal.setText(self.t('his_card_portal'))
        self.his_portal_desc.setText(self.t('his_portal_desc'))
        self.btn_his_portal_start.setText(self.t('his_btn_portal_start'))
        self.btn_his_portal_vidaahub.setText(self.t('his_btn_portal_vidaahub'))
        self.btn_his_portal_open_tv.setText(self.t('his_btn_portal_open_tv'))
        self.btn_his_portal_diag.setText(self.t('his_btn_portal_diag'))
        self.btn_his_portal_stop.setText(self.t('his_btn_portal_stop'))

        self.kpi_his_tv.lbl.setText(self.t('his_kpi_tv'))
        self.kpi_his_pair.lbl.setText(self.t('his_kpi_pair'))
        self.kpi_his_portal.lbl.setText(self.t('his_kpi_portal'))
        self.his_remote_title.setText(self.t('his_remote_title'))
        self._refresh_hisense_status_ui()

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
        self.btn_sam_belet_direct.setText(self.t('sam_btn_belet_direct'))
        self.btn_sam_belet_launch.setText(self.t('sam_btn_belet_launch'))
        self.btn_sam_belet_remove.setText(self.t('sam_btn_belet_remove'))

        self.lbl_sam_alem.setText(self.t('sam_card_alemtv'))
        self.sam_alem_desc.setText(self.t('sam_alemtv_desc'))
        self.lbl_sam_alem_portal.setText(self.t('sam_lbl_alem_portal'))
        self.btn_sam_alem_install.setText(self.t('sam_btn_alemtv_install'))
        self.btn_sam_alem_launch.setText(self.t('sam_btn_alemtv_launch'))
        self.btn_sam_alem_remove.setText(self.t('sam_btn_alemtv_remove'))

        self.lbl_sam_time.setText(self.t('sam_card_time'))
        self.sam_time_desc.setText(self.t('sam_time_desc'))
        self.sam_time_lbl_mode.setText(self.t('sam_time_lbl_mode'))
        cur_stm_mode = self.sam_time_mode_combo.currentData() or 'gateway'
        self.sam_time_mode_combo.blockSignals(True)
        self.sam_time_mode_combo.setItemText(
            0,
            'Шлюз времени NEON (DNS :53 + /openapi/timesync :80 + NTP :123)'
            if self.lang == 'ru'
            else 'NEON Time Gateway (DNS :53 + /openapi/timesync :80 + NTP :123)',
        )
        self.sam_time_mode_combo.setItemText(
            1,
            'Прямая DNS A-запись (DNS :53 → IP целевого сервера)'
            if self.lang == 'ru'
            else 'Direct DNS A-record rewrite (DNS :53 → Target IP)',
        )
        m_idx = self.sam_time_mode_combo.findData(cur_stm_mode)
        if m_idx >= 0:
            self.sam_time_mode_combo.setCurrentIndex(m_idx)
        self.sam_time_mode_combo.blockSignals(False)
        self.sam_time_lbl_target.setText(self.t('sam_time_lbl_target'))
        cur_target_txt = self.sam_time_target_input.text().strip()
        self.sam_time_preset_combo.blockSignals(True)
        self.sam_time_preset_combo.clear()
        for p in TIME_TARGET_PRESETS:
            self.sam_time_preset_combo.addItem(p['label_ru'] if self.lang == 'ru' else p['label_en'], p['id'])
        self.sam_time_preset_combo.addItem(
            'Свой домен или IPv4...' if self.lang == 'ru' else 'Custom domain or IPv4...',
            'custom',
        )
        p_match = self.sam_time_preset_combo.findData(cur_target_txt)
        self.sam_time_preset_combo.setCurrentIndex(p_match if p_match >= 0 else self.sam_time_preset_combo.count() - 1)
        self.sam_time_preset_combo.blockSignals(False)
        self.sam_time_lbl_sources.setText(self.t('sam_time_lbl_sources'))
        self.sam_time_chk_check.setText(self.t('sam_time_chk_check'))
        self.sam_time_chk_autostop.setText(self.t('sam_time_chk_autostop'))
        self.sam_time_lbl_upstream.setText(self.t('sam_time_lbl_upstream'))
        self.btn_sam_time_start.setText(self.t('sam_time_btn_start'))
        self.btn_sam_time_stop.setText(self.t('sam_time_btn_stop'))
        self.btn_sam_time_sync.setText(self.t('sam_time_btn_sync'))
        self.btn_sam_time_diag.setText(self.t('sam_time_btn_diag'))
        self.sam_time_note_nopc.setText(self.t('sam_time_note_nopc'))
        self.sam_time_remote_title.setText(self.t('sam_time_remote_title'))
        self.sam_time_lbl_remote_host.setText(self.t('sam_time_lbl_remote_host'))
        self.sam_time_lbl_remote_user.setText(self.t('sam_time_lbl_remote_user'))
        self.sam_time_lbl_remote_pass.setText(self.t('sam_time_lbl_remote_pass'))
        self.btn_sam_time_deploy.setText(self.t('sam_time_btn_deploy'))
        self.btn_sam_time_export.setText(self.t('sam_time_btn_export'))
        self.btn_sam_time_snippets.setText(self.t('sam_time_btn_snippets'))
        self.sam_text_input.setPlaceholderText(self.t('sam_txt_placeholder'))
        self.btn_sam_send_text.setText(self.t('sam_btn_send_text'))
        self.btn_sam_hotel.setText(self.t('sam_btn_hotel'))
        self._refresh_sam_time_status()

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

        self.btn_vless.setText('Подключить VLESS' if self.lang == 'ru' else 'Connect VLESS')
        self.vless_strict.setText(self.t('vless_strict'))
        self.vless_hint.setText(
            'После запуска NEON проверяет, проходят ли интернет-трафик и DNS действительно через туннель: результат и '
            'ошибки движка видны в терминале. Для точки доступа/ICS делится именно NEON-VLESS, а не аплинк.'
            if self.lang == 'ru' else
            'After start, NEON checks that internet traffic and DNS really flow through the tunnel: the result and any '
            'engine errors appear in the terminal. For hotspot/ICS sharing, share NEON-VLESS itself — not the uplink.')
        self.lbl_vpn1.setText(self.t('vpn_card1'))
        self.vpn_card1_desc.setText(self.t('vpn_card1_desc'))
        self.lbl_profile.setText(self.t('lbl_profile'))
        self.btn_browse.setText(self.t('btn_browse'))
        self.btn_refresh_prof.setText(self.t('btn_refresh'))
        self.lbl_vpn_user.setText(self.t('lbl_vpn_user'))
        self.vpn_user.setPlaceholderText(self.t('ph_vpn_user'))
        self.lbl_vpn_pass.setText(self.t('lbl_vpn_pass'))
        self.vpn_pass.setPlaceholderText(self.t('ph_vpn_pass'))
        self.lbl_vpn_key.setText(self.t('lbl_vpn_key'))
        self.vpn_key_pass.setPlaceholderText(self.t('ph_vpn_key'))
        self._apply_vpn_auth_state(getattr(self, '_vpn_auth_info', {}))
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
            ('page_his_title', 'page_his_sub'),
            ('page_vpn_title', 'page_vpn_sub'),
            ('page_diag_title', 'page_diag_sub'),
        ]
        if self.current_tab == 5:
            self.page_title.setText('MSB CALLBACK')
            self.page_sub.setText('Meryosab Callback · Ашхабад')
            return
        tk, sk = titles[self.current_tab]
        self.page_title.setText(self.t(tk))
        self.page_sub.setText(self.t(sk))

    def _restore_saved_inputs(self):
        self.tv_ip.setText(self.settings.get('tv_ip', '192.168.1.100'))
        self.chk_os26.setChecked(bool(self.settings.get('webos26', False)))
        self.sam_ip.setText(self.settings.get('samsung_ip', '192.168.1.101'))
        self.his_ip.setText(self.settings.get('hisense_ip', '192.168.1.102'))
        hp_idx = self.his_profile_combo.findData(str(self.settings.get('hisense_auth_profile', 'auto') or 'auto'))
        if hp_idx >= 0:
            self.his_profile_combo.setCurrentIndex(hp_idx)
        hbm_idx = self.his_belet_mode_combo.findData(str(self.settings.get('hisense_belet_mode') or 'lan_server'))
        if hbm_idx >= 0:
            self.his_belet_mode_combo.setCurrentIndex(hbm_idx)
        self.his_belet_url.setText(str(self.settings.get('hisense_belet_url') or 'https://belet.tm/'))
        self._on_his_belet_mode_changed()
        saved_ha_portal = str(self.settings.get('hisense_alem_portal') or ALEMTV_DEFAULT_PORTAL)
        ha_idx = self.his_alem_combo.findData(saved_ha_portal)
        if ha_idx >= 0:
            self.his_alem_combo.setCurrentIndex(ha_idx)
        else:
            self.his_alem_combo.setCurrentIndex(self.his_alem_combo.count() - 1)
            self.his_alem_custom.setText(saved_ha_portal)
            self.his_alem_custom.setVisible(True)
        ham_idx = self.his_alem_mode_combo.findData(str(self.settings.get('hisense_alem_mode') or 'lan_server'))
        if ham_idx >= 0:
            self.his_alem_mode_combo.setCurrentIndex(ham_idx)
        hcb_idx = self.his_cb_mode_combo.findData(str(self.settings.get('hisense_callback_mode') or 'data_url'))
        if hcb_idx >= 0:
            self.his_cb_mode_combo.setCurrentIndex(hcb_idx)
        self.his_cb_custom_url.setText(str(self.settings.get('hisense_callback_custom_url') or ''))
        self._on_his_cb_mode_changed()
        self.his_custom_id.setText(str(self.settings.get('hisense_custom_id') or 'neon.customapp'))
        self.his_custom_title.setText(str(self.settings.get('hisense_custom_title') or 'My TV App'))
        self.his_custom_url.setText(str(self.settings.get('hisense_custom_url') or ''))
        self.his_custom_icon.setText(str(self.settings.get('hisense_custom_icon') or ''))
        self._refresh_hisense_status_ui()
        saved_sam_port = str(self.settings.get('samsung_port', 'auto') or 'auto')
        p_idx = self.sam_port_combo.findData(saved_sam_port)
        if p_idx >= 0:
            self.sam_port_combo.setCurrentIndex(p_idx)
        saved_st_mode = str(self.settings.get('samsung_time_mode', 'gateway') or 'gateway')
        stm_idx = self.sam_time_mode_combo.findData(saved_st_mode)
        if stm_idx >= 0:
            self.sam_time_mode_combo.setCurrentIndex(stm_idx)
        saved_st_target = str(self.settings.get('samsung_time_target', 'time.cloudflare.com') or 'time.cloudflare.com')
        self.sam_time_target_input.blockSignals(True)
        self.sam_time_target_input.setText(saved_st_target)
        self.sam_time_target_input.blockSignals(False)
        self._on_sam_time_target_edited(saved_st_target)
        self.sam_time_sources_input.setText(
            str(self.settings.get('samsung_time_sources') or ', '.join(SAMSUNG_TIME_DEFAULT_SOURCES))
        )
        self.sam_time_chk_check.setChecked(bool(self.settings.get('samsung_time_intercept_check', True)))
        self.sam_time_chk_autostop.setChecked(bool(self.settings.get('samsung_time_autostop', False)))
        self.sam_time_upstream_input.setText(str(self.settings.get('samsung_time_upstream_dns', '') or ''))
        self.sam_time_remote_host.setText(str(self.settings.get('samsung_time_remote_host', '') or ''))
        self.sam_time_remote_user.setText(str(self.settings.get('samsung_time_remote_user', 'root') or 'root'))
        self.sam_time_remote_port.setText(str(self.settings.get('samsung_time_remote_port', 22) or 22))
        self._refresh_sam_time_status()
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
            self.his_term.append_line(line, kind=kind)
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
        if channel == 'his' and self.his_busy:
            return
        if channel == 'net' and self.net_busy:
            return
        if channel == 'tv':
            self.tv_busy = True
            self.tv_term.scanner.start(scan_title or self.t('scanner_title'), self.t('scanner_tag'))
        elif channel == 'sam':
            self.sam_busy = True
            self.sam_term.scanner.start(scan_title or self.t('scanner_title'), self.t('scanner_tag'))
        elif channel == 'his':
            self.his_busy = True
            self.his_term.scanner.start(scan_title or self.t('scanner_title'), self.t('scanner_tag'))
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
                elif channel == 'his':
                    self.his_busy = False
                    self.his_term.scanner.stop()
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
            his = self.api.read_hisense_logs()
            for line in his.get('logs', []):
                self.his_term.append_line(line)
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

        try:
            srv = getattr(self.api, '_samsung_time_server', None)
            if srv is not None:
                is_run = bool(getattr(srv, '_running', False))
                was_run = bool(getattr(self, '_sam_time_was_running', False))
                if is_run:
                    tick = getattr(self, '_sam_time_tick', 0) + 1
                    self._sam_time_tick = tick
                    if (not was_run) or (tick % 12 == 0):
                        self._refresh_sam_time_status(srv.get_status())
                elif was_run:
                    self._refresh_sam_time_status(srv.get_status())
                    self.toast.show_toast(
                        'ТВ синхронизировал время — одноразовый сервер ПК остановлен. Верните DNS на ТВ (Авто / 1.1.1.1) перед выключением ПК!'
                        if self.lang == 'ru'
                        else 'TV time synced — one-time PC server auto-stopped. Restore TV DNS (Auto / 1.1.1.1) before turning PC off!',
                        'info',
                    )
                self._sam_time_was_running = is_run
        except Exception:
            pass

    def _sync_status_badges(self):
        hs = getattr(self.api, 'hotspot_active', False)
        eth = getattr(self.api, 'ethernet_active', False)
        if self.tv_busy or self.sam_busy or self.his_busy or self.net_busy:
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
        elif self.tv_connected or self.sam_connected or self.his_connected:
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
        self.side_his_dot.setStyleSheet(
            f'color:{"#ffba5a" if self.his_connected else "#5a6e7a"};font-size:9px;background:transparent;'
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

    def install_lg_belet_client(self):
        if not self.tv_connected:
            self.toast.show_toast(
                'Сначала подключитесь к телевизору.' if self.lang == 'ru' else 'Connect to the TV first.',
                'warn',
            )
            return
        self.log('$ luna-send-pub dev/install tm.belet.neonclient.ipk')
        self._run_async('tv', self.api.install_belet_lg_client, lambda r: self.toast.show_toast(
            'Новый клиент Belet установлен на LG; откройте его с пульта.'
            if self.lang == 'ru'
            else 'New Belet client installed on the LG TV; open it from Home.',
            'info',
        ) if r.get('ok') else None)

    def launch_lg_belet_client(self):
        if not self.tv_connected:
            self.toast.show_toast(
                'Сначала подключитесь к телевизору.' if self.lang == 'ru' else 'Connect to the TV first.',
                'warn',
            )
            return
        self.log('$ luna-send-pub launch tm.belet.neonclient')
        self._run_async('tv', self.api.launch_belet_lg_client, lambda r: self.toast.show_toast(
            'Новый клиент Belet запущен на LG.' if self.lang == 'ru' else 'New Belet client launched on the LG TV.',
            'info',
        ) if r.get('ok') else None)

    def remove_lg_belet_client(self):
        if not self.tv_connected:
            self.toast.show_toast(
                'Сначала подключитесь к телевизору.' if self.lang == 'ru' else 'Connect to the TV first.',
                'warn',
            )
            return
        body = (
            'Удалить новый клиент Belet (tm.belet.neonclient) с телевизора?'
            if self.lang == 'ru'
            else 'Uninstall the new Belet client (tm.belet.neonclient) from the TV?'
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
        self.log('$ luna-send-pub dev/remove tm.belet.neonclient')
        self._run_async('tv', self.api.remove_belet_lg_client, lambda r: self.toast.show_toast(
            'Новый клиент Belet удалён с ТВ.' if self.lang == 'ru' else 'New Belet client removed from the TV.',
            'info',
        ) if r.get('ok') else None)

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
        if hasattr(self, 'sam_time_dns_hint_lbl'):
            self.sam_time_dns_hint_lbl.setText(self.t('sam_time_dns_prefix') + pc_ip)

    def _on_sam_time_preset_changed(self):
        val = self.sam_time_preset_combo.currentData()
        if val and val != 'custom':
            self.sam_time_target_input.blockSignals(True)
            self.sam_time_target_input.setText(str(val))
            self.sam_time_target_input.blockSignals(False)
        elif val == 'custom':
            self.sam_time_target_input.setFocus()

    def _on_sam_time_target_edited(self, text):
        clean = str(text or '').strip()
        idx = self.sam_time_preset_combo.findData(clean)
        self.sam_time_preset_combo.blockSignals(True)
        self.sam_time_preset_combo.setCurrentIndex(idx if idx >= 0 else self.sam_time_preset_combo.count() - 1)
        self.sam_time_preset_combo.blockSignals(False)

    def _refresh_sam_time_status(self, st=None):
        if not hasattr(self, 'sam_time_status_lbl'):
            return
        try:
            st = st or self.api.get_samsung_time_status(self.sam_ip.text().strip())
        except Exception:
            return
        running = bool(st.get('running'))
        pc_ip = st.get('pc_ip') or '192.168.1.x'
        if hasattr(self, 'sam_time_dns_hint_lbl'):
            self.sam_time_dns_hint_lbl.setText(self.t('sam_time_dns_prefix') + pc_ip)
        self.btn_sam_time_stop.setEnabled(running)
        if running:
            stats = st.get('stats') or {}
            dns_hits = stats.get('dns_overridden', 0)
            http_hits = stats.get('http_timesync', 0)
            chk_hits = stats.get('http_check', 0)
            ntp_hits = stats.get('ntp_hits', 0)
            off = st.get('offset_ms', 0)
            method = st.get('sync_method') or st.get('mode')
            ports_txt = f"DNS:{st.get('dns_port') or 53}"
            if st.get('http_port'):
                ports_txt += f" · HTTP:{st.get('http_port')}"
            if st.get('ntp_port'):
                ports_txt += f" · NTP:{st.get('ntp_port')}"
            auto_tag = ' · AUTO-STOP' if st.get('auto_stop_after_sync') else ''
            self.sam_time_status_lbl.setStyleSheet('color:#5ef2a0;font-weight:bold;')
            self.sam_time_status_lbl.setText(
                f"{self.t('sam_time_status_active')} ({ports_txt} | {method} | {off:+d}ms{auto_tag}) · "
                f"DNS:{dns_hits} · timesync:{http_hits} · check:{chk_hits} · NTP:{ntp_hits}"
            )
        else:
            self.sam_time_status_lbl.setStyleSheet('')
            extra = ''
            if st.get('auto_stopped_reason'):
                extra = f" ({st.get('auto_stopped_reason')})"
            self.sam_time_status_lbl.setText(self.t('sam_time_status_idle') + extra)

    def start_samsung_time_override(self):
        target = self.sam_time_target_input.text().strip()
        mode = self.sam_time_mode_combo.currentData() or 'gateway'
        sources = self.sam_time_sources_input.text().strip()
        chk = self.sam_time_chk_check.isChecked()
        autostop = self.sam_time_chk_autostop.isChecked()
        upstream = self.sam_time_upstream_input.text().strip()
        tv_ip = self.sam_ip.text().strip()
        self.log(f'$ start samsung_time_override mode={mode} target={target} auto_stop={autostop}')

        def done(r):
            self._refresh_sam_time_status(r if r.get('ok') else None)
            if r.get('ok'):
                self.toast.show_toast(
                    'Перехват времени Samsung запущен! Укажите DNS ПК на ТВ.'
                    if self.lang == 'ru'
                    else 'Samsung Time Override active! Set TV DNS to PC IP.',
                    'info',
                )

        self._run_async(
            'sam',
            lambda: self.api.start_samsung_time_override(
                target=target,
                mode=mode,
                source_domains=sources,
                intercept_check=chk,
                upstream_dns=upstream,
                tv_ip=tv_ip,
                auto_stop_after_sync=autostop,
            ),
            done,
        )

    def stop_samsung_time_override(self):
        self.log('$ stop samsung_time_override')

        def done(r):
            self._refresh_sam_time_status(r)
            if r.get('ok'):
                self.toast.show_toast(
                    'Перехват времени Samsung остановлен.' if self.lang == 'ru' else 'Samsung Time Override stopped.',
                    'info',
                )

        self._run_async('sam', self.api.stop_samsung_time_override, done)

    def sync_samsung_time_now(self):
        target = self.sam_time_target_input.text().strip()
        self.log(f'$ sync samsung_time_now target={target}')

        def done(r):
            if r.get('status'):
                self._refresh_sam_time_status(r['status'])
            if r.get('ok'):
                self.toast.show_toast(
                    f"Время синхронизировано ({r.get('offset_ms', 0):+d} ms)"
                    if self.lang == 'ru'
                    else f"Time synced ({r.get('offset_ms', 0):+d} ms)",
                    'info',
                )

        self._run_async('sam', lambda: self.api.sync_samsung_time_now(target=target), done)

    def diagnose_samsung_time_reachability(self):
        self.log('$ diagnose time.samsungcloudsolution.com via 1.1.1.1 / 8.8.8.8')

        def done(r):
            if r.get('ok'):
                best = r.get('best_public_dns')
                if best:
                    self.toast.show_toast(
                        f'Хорошая новость: DNS {best} отвечает без ПК! Введите {best} в DNS ТВ.'
                        if self.lang == 'ru'
                        else f'Direct DNS {best} works without PC! Enter {best} as TV DNS.',
                        'info',
                    )
                else:
                    self.toast.show_toast(
                        'Публичный timesync не отвечает напрямую — нужен 24/7 шлюз (роутер/VPS) или Belet/Alem с обходом.'
                        if self.lang == 'ru'
                        else 'Public timesync failed directly — deploy 24/7 gateway on router/VPS or use Belet/Alem bypass.',
                        'warn',
                    )

        self._run_async('sam', self.api.diagnose_samsung_time_reachability, done)

    def deploy_samsung_time_remote(self):
        host = self.sam_time_remote_host.text().strip()
        user = self.sam_time_remote_user.text().strip() or 'root'
        pw = self.sam_time_remote_pass.text()
        try:
            port = int(self.sam_time_remote_port.text().strip() or '22')
        except ValueError:
            self.toast.show_toast('Invalid SSH port', 'error')
            return
        if not host:
            self.toast.show_toast(
                'Введите IP роутера / Linux / VPS' if self.lang == 'ru' else 'Enter Router / Linux / VPS IP',
                'warn',
            )
            return
        target = self.sam_time_target_input.text().strip()
        sources = self.sam_time_sources_input.text().strip()
        chk = self.sam_time_chk_check.isChecked()
        upstream = self.sam_time_upstream_input.text().strip()
        self.log(f'$ ssh deploy 24/7 samsung_time_gateway -> {user}@{host}:{port}')

        def done(r):
            if r.get('ok'):
                dns_ip = r.get('tv_dns_ip') or host
                self.sam_text_input.setText(dns_ip)
                self.toast.show_toast(
                    f'Установлено 24/7 ({r.get("platform", "")})! Введите на ТВ DNS: {dns_ip} (ПК можно выкл.)'
                    if self.lang == 'ru'
                    else f'24/7 installed ({r.get("platform", "")})! Set TV DNS: {dns_ip} (PC not needed)',
                    'info',
                )

        self._run_async(
            'sam',
            lambda: self.api.deploy_samsung_time_remote(
                host=host,
                username=user,
                password=pw,
                port=port,
                target=target,
                source_domains=sources,
                intercept_check=chk,
                upstream_dns=upstream,
                gateway_ip=host,
            ),
            done,
        )

    def export_samsung_time_gateway(self):
        default_name = 'neon_samsung_time_gateway.py'
        path, _ = QFileDialog.getSaveFileName(
            self,
            'Сохранить 24/7 скрипт шлюза' if self.lang == 'ru' else 'Save 24/7 Gateway Script',
            default_name,
            'Python scripts (*.py);;All files (*.*)',
        )
        if not path:
            return
        target = self.sam_time_target_input.text().strip()
        sources = self.sam_time_sources_input.text().strip()
        chk = self.sam_time_chk_check.isChecked()
        upstream = self.sam_time_upstream_input.text().strip()

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    f'Скрипт сохранён: {os.path.basename(r.get("path", path))}'
                    if self.lang == 'ru'
                    else f'Script saved: {os.path.basename(r.get("path", path))}',
                    'info',
                )

        self._run_async(
            'sam',
            lambda: self.api.export_samsung_time_gateway(
                save_path=path,
                target=target,
                source_domains=sources,
                intercept_check=chk,
                upstream_dns=upstream,
            ),
            done,
        )

    def show_samsung_time_snippets(self):
        gw_ip = self.sam_time_remote_host.text().strip()
        sources = self.sam_time_sources_input.text().strip()
        chk = self.sam_time_chk_check.isChecked()

        def done(r):
            if r.get('ok'):
                self.log('=== OpenWrt /etc/dnsmasq.conf ===\n' + r.get('openwrt_dnsmasq', ''))
                self.log('=== MikroTik RouterOS CLI ===\n' + r.get('mikrotik_cli', ''))
                self.log('=== Keenetic / dnsmasq ===\n' + r.get('keenetic_cli', ''))
                self.log('=== systemd service ===\n' + r.get('linux_systemd', ''))
                self.toast.show_toast(
                    'Правила роутеров выведены в консоль справа.'
                    if self.lang == 'ru'
                    else 'Router rules printed in console.',
                    'info',
                )

        self._run_async(
            'sam',
            lambda: self.api.get_samsung_time_router_snippets(
                gateway_ip=gw_ip,
                source_domains=sources,
                intercept_check=chk,
            ),
            done,
        )

    def sam_send_text(self):
        txt = self.sam_text_input.text().strip()
        if not txt:
            self.toast.show_toast(
                'Введите текст или IP (напр. 1.1.1.1)' if self.lang == 'ru' else 'Enter text or IP (e.g. 1.1.1.1)',
                'warn',
            )
            return
        self.log(f'$ samsung_send_text "{txt}"')
        self._run_async('sam', lambda: self.api.samsung_send_text(txt))

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
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        self.log(f'$ sdb {ip}:26101 install BeletFilm1.wgt (with bypass)')
        self._run_async(
            'sam',
            lambda: self.api.install_samsung_belet_client(ip),
            lambda r: self.toast.show_toast(
                'Belet TV (с обходом) установлен на Samsung ТВ!'
                if self.lang == 'ru'
                else 'Belet TV (with bypass) installed on Samsung TV!',
                'info',
            ) if r.get('ok') else None,
        )

    def install_samsung_belet_direct_client(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        self.log(f'$ sdb {ip}:26101 install BeletFilm1.wgt (direct, without bypass)')
        self._run_async(
            'sam',
            lambda: self.api.install_samsung_belet_direct_client(ip),
            lambda r: self.toast.show_toast(
                'Belet TV (без обхода) установлен на Samsung ТВ!'
                if self.lang == 'ru'
                else 'Belet TV (without bypass) installed on Samsung TV!',
                'info',
            ) if r.get('ok') else None,
        )

    def launch_samsung_belet_client(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        self.log(f'$ sdb {ip}:26101 0 was_execute BeletFilm1.BeletClient')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Команда запуска Belet отправлена на Samsung ТВ.'
                    if self.lang == 'ru'
                    else 'Belet launch command sent to Samsung TV.',
                    'info',
                )

        self._run_async('sam', lambda: self.api.launch_samsung_belet_client(ip), done)

    def remove_samsung_belet_client(self):
        ip = self.sam_ip.text().strip()
        if not ip:
            self.toast.show_toast(
                'Введите IP-адрес Samsung ТВ.' if self.lang == 'ru' else 'Enter Samsung TV IP address.',
                'warn',
            )
            return
        body = (
            'Удалить приложение Belet (BeletFilm1) с Samsung ТВ?'
            if self.lang == 'ru'
            else 'Uninstall Belet (BeletFilm1) from Samsung TV?'
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
        self.log(f'$ sdb {ip}:26101 0 vd_appuninstall BeletFilm1')

        def done(r):
            if r.get('ok'):
                self.toast.show_toast(
                    'Belet удалён с Samsung ТВ.' if self.lang == 'ru' else 'Belet removed from Samsung TV.',
                    'info',
                )

        self._run_async('sam', lambda: self.api.remove_samsung_belet_client(ip), done)

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
            self._vpn_auth_info = {}
            self.vpn_auth_box.setVisible(False)
            return
        info = self.api.inspect_profile(profile_name)
        self._apply_vpn_auth_state(info)

    def _apply_vpn_auth_state(self, info):
        """Show only the fields the selected profile actually asks for."""
        self._vpn_auth_info = dict(info or {})
        needs_auth = bool(self._vpn_auth_info.get('needs_auth'))
        needs_key = bool(self._vpn_auth_info.get('needs_keypass'))
        for widget in (self.vpn_user, self.lbl_vpn_user, self.vpn_pass_row, self.lbl_vpn_pass):
            widget.setVisible(needs_auth)
        for widget in (self.vpn_key_pass, self.lbl_vpn_key, self.vpn_key_eye, self.vpn_key_row):
            widget.setVisible(needs_key)
        hints = []
        if needs_auth:
            hints.append(self.t('vpn_auth_hint_userpass'))
        if needs_key:
            hints.append(self.t('vpn_auth_hint_keypass'))
        self.vpn_auth_hint.setText(' '.join(hints))
        self.vpn_auth_box.setVisible(bool(needs_auth or needs_key))

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

    # ------------------------------------------------------------------
    # Hisense VIDAA Smart TV Actions
    # ------------------------------------------------------------------
    def _refresh_hisense_status_ui(self):
        if not hasattr(self, 'his_ip'):
            return
        ip = self.his_ip.text().strip()
        try:
            st = self.api.get_hisense_status(ip)
        except Exception:
            return
        paired = bool(st.get('paired'))
        self.his_connected = paired
        if paired:
            title = st.get('friendly_name') or st.get('model_name') or ip
            self.kpi_his_tv.set_data(title, '#5ef2a0', ip)
            self.kpi_his_pair.set_data(
                (st.get('auth_profile') or 'paired').upper(),
                '#36e2ff',
                f"brand={st.get('brand', 'his')}",
            )
        else:
            self.kpi_his_tv.set_data(self.t('his_kpi_idle'), '#edf5fa', ip or 'MQTT :36669')
            self.kpi_his_pair.set_data(self.t('his_kpi_pair_idle'), '#8fa4b2', 'PIN 4 digits')

        if st.get('portal_running'):
            purl = st.get('portal_url') or ''
            surl = st.get('short_url') or ''
            vh = st.get('vidaahub_running')
            self.kpi_his_portal.set_data(
                'VIDAAHUB + LAN' if vh else 'LAN ACTIVE',
                '#5ef2a0',
                'https://vidaahub.com' if vh else (surl or purl),
            )
            lan_txt = f'LAN: {surl}  |  {purl}' if surl else f'LAN: {purl}'
            alts = st.get('alt_ips') or []
            if alts:
                lan_txt += '\nAlt IP: ' + ', '.join(alts)
            if vh:
                lan_txt += '\nvidaahub.com: https://vidaahub.com (DNS -> ' + str(st.get('pc_ip')) + ')'
            self.his_portal_status_lbl.setText(lan_txt)
        else:
            self.kpi_his_portal.set_data(self.t('his_kpi_portal_idle'), '#8fa4b2', 'HTTP / vidaahub')
            alts = st.get('alt_ips') or []
            alt_note = (' · Alt: ' + ', '.join(alts)) if alts else ''
            self.his_portal_status_lbl.setText(f"PC IP: {st.get('pc_ip', '192.168.1.x')}{alt_note}")
        self._sync_status_badges()

    def _on_his_found_selected(self, idx):
        ip = self.his_found_combo.itemData(idx)
        if ip:
            self.his_ip.setText(str(ip))

    def _on_his_alem_portal_changed(self):
        val = self.his_alem_combo.currentData()
        self.his_alem_custom.setVisible(val == 'custom')

    def _selected_his_alem_portal(self):
        val = self.his_alem_combo.currentData()
        if val == 'custom':
            return self.his_alem_custom.text().strip() or ALEMTV_DEFAULT_PORTAL
        return str(val or ALEMTV_DEFAULT_PORTAL)

    def _on_his_belet_mode_changed(self):
        mode = self.his_belet_mode_combo.currentData()
        self.his_belet_url_wrap.setVisible(mode == 'custom_url')

    def _on_his_cb_mode_changed(self):
        mode = self.his_cb_mode_combo.currentData()
        self.his_cb_url_wrap.setVisible(mode == 'custom_url')

    def his_scan(self):
        def done(r):
            devs = r.get('devices') or []
            self._his_found_devices = devs
            self.his_found_combo.blockSignals(True)
            self.his_found_combo.clear()
            for d in devs:
                self.his_found_combo.addItem(f"{d['name']} ({d['model']}) · {d['ip']}", d['ip'])
            self.his_found_combo.blockSignals(False)
            self.his_found_wrap.setVisible(bool(devs))
            if devs:
                self.his_ip.setText(devs[0]['ip'])
                self.toast.show_toast(
                    f'Найдено Hisense VIDAA: {len(devs)}' if self.lang == 'ru' else f'Found {len(devs)} VIDAA TV(s)',
                    'info',
                )
            else:
                self.toast.show_toast(
                    'Hisense VIDAA по SSDP не ответил — введите IP вручную.' if self.lang == 'ru' else 'No VIDAA TV answered SSDP — enter IP manually.',
                    'warn',
                )
        self._run_async('his', self.api.discover_hisense, done, 'Поиск Hisense VIDAA')

    def his_start_pairing(self):
        ip = self.his_ip.text().strip()
        prof = self.his_profile_combo.currentData() or 'auto'
        def done(r):
            if r.get('ok'):
                if r.get('status') == 'paired':
                    self.his_pin_wrap.setVisible(False)
                    self._refresh_hisense_status_ui()
                    self.toast.show_toast('Hisense VIDAA сопряжён!' if self.lang == 'ru' else 'Hisense paired!', 'info')
                else:
                    self._his_pairing_id = r.get('pairing_id', '')
                    self.his_pin_wrap.setVisible(True)
                    self.his_pin_input.clear()
                    self.his_pin_input.setFocus()
                    self.toast.show_toast(
                        'Введите 4-значный PIN с экрана телевизора' if self.lang == 'ru' else 'Enter 4-digit PIN from TV screen',
                        'info',
                    )
        self._run_async('his', lambda: self.api.start_hisense_pairing(ip, prof), done, 'Запрос PIN Hisense')

    def his_confirm_pin(self):
        pin = self.his_pin_input.text().strip()
        pid = self._his_pairing_id
        def done(r):
            if r.get('ok'):
                self.his_pin_wrap.setVisible(False)
                self._his_pairing_id = ''
                self._refresh_hisense_status_ui()
                self.toast.show_toast('Hisense VIDAA успешно подключён!' if self.lang == 'ru' else 'Hisense VIDAA paired!', 'info')
        self._run_async('his', lambda: self.api.confirm_hisense_pairing(pid, pin), done, 'Подтверждение PIN')

    def his_cancel_pin(self):
        pid = self._his_pairing_id
        self._his_pairing_id = ''
        self.his_pin_wrap.setVisible(False)
        self.api.cancel_hisense_pairing(pid)

    def his_forget_pairing(self):
        ip = self.his_ip.text().strip()
        self.api.forget_hisense_pairing(ip)
        self._refresh_hisense_status_ui()
        self.toast.show_toast('Сессия Hisense удалена' if self.lang == 'ru' else 'Hisense pairing cleared', 'info')

    def _his_report_install(self, app_label, r):
        self._refresh_hisense_status_ui()
        if not isinstance(r, dict):
            return
        for w in (r.get('warnings') or []):
            self.log('[!] ' + w, 'bad')
        if not r.get('ok'):
            return
        if r.get('mode') == 'tv_flash':
            msg = (
                ('Установщик открыт в браузере ТВ!' if self.lang == 'ru' else 'Installer opened in TV Browser!')
                if r.get('launched')
                else (('Откройте в браузере ТВ: ' if self.lang == 'ru' else 'Open in TV Browser: ') + str(r.get('portal_url', '')))
            )
            self.toast.show_toast(msg, 'info')
            return
        if r.get('status') == 'sent_unconfirmed':
            if r.get('browser_opened'):
                self.toast.show_toast(
                    f'{app_label}: открыто через браузер ТВ (плитка не подтверждена)'
                    if self.lang == 'ru' else f'{app_label}: opened in TV Browser (tile unconfirmed)',
                    'info',
                )
            else:
                self.toast.show_toast(
                    f'{app_label}: команда отправлена, но ТВ не добавил плитку — откройте установщик на ТВ'
                    if self.lang == 'ru' else f'{app_label}: sent, but TV did not confirm tile — open TV Installer',
                    'warn',
                )
            return
        self.toast.show_toast(
            f'{app_label} установлен на Hisense!' if self.lang == 'ru' else f'{app_label} installed on Hisense!',
            'info',
        )

    def _his_report_launch(self, r):
        self._refresh_hisense_status_ui()
        if isinstance(r, dict) and r.get('ok') and r.get('browser_opened'):
            self.toast.show_toast(
                'Открыто в браузере ТВ!' if self.lang == 'ru' else 'Opened in TV Browser!',
                'info',
            )

    def his_install_belet(self):
        ip = self.his_ip.text().strip()
        mode = self.his_belet_mode_combo.currentData() or 'lan_server'
        url = self.his_belet_url.text().strip() or 'https://belet.tm/'
        self._run_async(
            'his',
            lambda: self.api.install_hisense_belet(ip, mode=mode, target_url=url, launch=True),
            lambda r: self._his_report_install('Belet', r),
            'Установка Belet',
        )

    def his_launch_belet(self):
        ip = self.his_ip.text().strip()
        mode = self.his_belet_mode_combo.currentData() or 'lan_server'
        url = self.his_belet_url.text().strip() or 'https://belet.tm/'
        self._run_async('his', lambda: self.api.launch_hisense_belet(ip, mode=mode, target_url=url), self._his_report_launch)

    def his_install_alemtv(self):
        ip = self.his_ip.text().strip()
        portal = self._selected_his_alem_portal()
        mode = self.his_alem_mode_combo.currentData() or 'lan_server'
        self._run_async(
            'his',
            lambda: self.api.install_hisense_alemtv(ip, target_url=portal, mode=mode, launch=True),
            lambda r: self._his_report_install('Alem TV', r),
            'Установка Alem TV',
        )

    def his_launch_alemtv(self):
        ip = self.his_ip.text().strip()
        portal = self._selected_his_alem_portal()
        mode = self.his_alem_mode_combo.currentData() or 'lan_server'
        self._run_async('his', lambda: self.api.launch_hisense_alemtv(ip, target_url=portal, mode=mode), self._his_report_launch)

    def his_install_callback(self):
        ip = self.his_ip.text().strip()
        mode = self.his_cb_mode_combo.currentData() or 'data_url'
        c_url = self.his_cb_custom_url.text().strip()
        self._run_async(
            'his',
            lambda: self.api.install_hisense_callback(ip, mode=mode, custom_url=c_url, launch=True),
            lambda r: self._his_report_install('MSB Callback', r),
            'Установка MSB Callback',
        )

    def his_launch_callback(self):
        ip = self.his_ip.text().strip()
        mode = self.his_cb_mode_combo.currentData() or 'data_url'
        c_url = self.his_cb_custom_url.text().strip()
        self._run_async('his', lambda: self.api.launch_hisense_callback(ip, mode=mode, custom_url=c_url), self._his_report_launch)

    def his_install_custom(self):
        ip = self.his_ip.text().strip()
        app_id = self.his_custom_id.text().strip()
        title = self.his_custom_title.text().strip()
        url = self.his_custom_url.text().strip()
        icon = self.his_custom_icon.text().strip()
        self._run_async(
            'his',
            lambda: self.api.install_hisense_custom_app(ip, app_id=app_id, title=title, url=url, icon_url=icon, launch=True),
            lambda r: self._his_report_install(title or 'App', r),
            'Установка приложения',
        )

    def his_launch_custom(self):
        ip = self.his_ip.text().strip()
        app_id = self.his_custom_id.text().strip()
        title = self.his_custom_title.text().strip()
        url = self.his_custom_url.text().strip()
        self._run_async('his', lambda: self.api.launch_hisense_app(ip, app_id=app_id, title=title, url=url), self._his_report_launch)

    def his_list_apps(self):
        ip = self.his_ip.text().strip()
        def done(r):
            apps = r.get('apps') or []
            self.his_apps_combo.clear()
            for a in apps:
                self.his_apps_combo.addItem(f"{a.get('name') or a.get('appId')} ({a.get('appId')})", a)
            if r.get('ok'):
                self.toast.show_toast(
                    f'Загружено приложений: {len(apps)}' if self.lang == 'ru' else f'Loaded {len(apps)} apps',
                    'info',
                )
        self._run_async('his', lambda: self.api.list_hisense_apps(ip), done, 'Список приложений Hisense')

    def his_launch_selected_app(self):
        data = self.his_apps_combo.currentData()
        if not isinstance(data, dict):
            return
        ip = self.his_ip.text().strip()
        self._run_async(
            'his',
            lambda: self.api.launch_hisense_app(ip, app_id=data.get('appId', ''), title=data.get('name', ''), url=data.get('url', '')),
            self._his_report_launch,
        )

    def his_start_portal(self, vidaahub=False):
        ip = self.his_ip.text().strip()
        portal = self._selected_his_alem_portal()
        def done(r):
            self._refresh_hisense_status_ui()
            for w in (r.get('warnings') or []):
                self.log('[!] ' + w, 'bad')
            if r.get('ok'):
                shown = (
                    r.get('vidaahub_url') if r.get('vidaahub_running')
                    else (r.get('short_url') or r.get('url', ''))
                )
                self.toast.show_toast('Портал Hisense запущен: ' + str(shown), 'info')
        self._run_async('his', lambda: self.api.start_hisense_portal_server(ip, start_vidaahub=vidaahub, alem_portal=portal), done, 'Запуск установщика VIDAA')

    def his_open_portal_on_tv(self):
        ip = self.his_ip.text().strip()
        portal = self._selected_his_alem_portal()
        def done(r):
            self._refresh_hisense_status_ui()
            for w in (r.get('warnings') or []):
                self.log('[!] ' + w, 'bad')
            if r.get('ok'):
                msg = (
                    'Установщик открыт на ТВ!' if r.get('launched')
                    else ('Откройте в браузере ТВ: ' + str(r.get('short_url') or r.get('portal_url', '')))
                )
                self.toast.show_toast(msg, 'info')
        self._run_async('his', lambda: self.api.open_hisense_portal_on_tv(ip, alem_portal=portal), done, 'Открытие на ТВ')

    def his_diagnose_portal(self):
        ip = self.his_ip.text().strip()
        def done(r):
            self._refresh_hisense_status_ui()
            if isinstance(r, dict):
                issues = r.get('issues') or []
                if issues:
                    self.toast.show_toast(issues[0], 'warn')
                else:
                    urls = ' · '.join(r.get('urls') or [])
                    self.toast.show_toast(
                        ('Портал готов: ' + urls) if self.lang == 'ru' else ('Portal ready: ' + urls),
                        'info',
                    )
        self._run_async('his', lambda: self.api.diagnose_hisense_connection(ip), done, 'Диагностика сети Hisense')

    def his_stop_portal(self):
        self.api.stop_hisense_portal_server()
        self._refresh_hisense_status_ui()
        self.toast.show_toast('Сервер Hisense остановлен' if self.lang == 'ru' else 'Hisense server stopped', 'info')

    def his_send_key(self, key):
        ip = self.his_ip.text().strip()
        self._run_async('his', lambda: self.api.hisense_send_key(ip, key))

    def _on_vless_strict_toggled(self, on):
        """Strict host routing is a leak blocker, and on some drivers a sharing problem:
        it can be switched off per machine without touching config files."""
        self.api.save_settings({'vless_strict_route': bool(on)})
        self.log('[+] VLESS strict host routing: ' + ('ON' if on else 'OFF'))

    def connect_vless(self):
        link = self.vless_link.text().strip()
        if not link:
            self.toast.show_toast(
                'Вставьте ссылку vless://…' if self.lang == 'ru' else 'Paste a vless:// link first.',
                'warn',
            )
            return

        def done(r):
            if r.get('ok'):
                self.vless_link.clear()
                self.toast.show_toast(
                    'VLESS поднимается; проверка интернета через туннель — в терминале.'
                    if self.lang == 'ru' else 'VLESS starting; the internet check through the tunnel is logged below.',
                    'info',
                )
            elif r.get('error'):
                self.toast.show_toast(r['error'], 'error')

        self._run_async('net', lambda: self.api.connect_vless(link), done)

    def connect_vpn(self):
        profile = self.profile_combo.currentText()
        if not profile:
            self.toast.show_toast('Choose a Conf profile (.ovpn) first.', 'warn')
            return
        info = self.api.inspect_profile(profile)
        self._apply_vpn_auth_state(info)
        username = self.vpn_user.text().strip()
        password = self.vpn_pass.text()
        key_password = self.vpn_key_pass.text()
        if info.get('needs_auth') and (not username or not password):
            self.toast.show_toast('This Conf profile requires username and password.', 'warn')
            (self.vpn_user if not username else self.vpn_pass).setFocus()
            return
        if info.get('needs_keypass') and not key_password:
            self.toast.show_toast(
                'Ключ в этом профиле защищён паролем — введите пароль ключа.'
                if self.lang == 'ru' else 'This profile\'s private key is password protected — enter the key password.',
                'warn',
            )
            self.vpn_key_pass.setFocus()
            return
        if not self._collect_and_validate_dns(persist=True):
            return
        self.log(f'$ start conf --config {profile}')

        def done(r):
            if r.get('needs_auth'):
                self.vpn_auth_box.setVisible(True)
                for widget in (self.vpn_user, self.lbl_vpn_user, self.vpn_pass_row, self.lbl_vpn_pass):
                    widget.setVisible(True)
                self.vpn_user.setFocus()
            elif r.get('needs_keypass'):
                self.vpn_auth_box.setVisible(True)
                for widget in (self.vpn_key_pass, self.lbl_vpn_key, self.vpn_key_eye, self.vpn_key_row):
                    widget.setVisible(True)
                self.vpn_key_pass.setFocus()
            elif r.get('ok'):
                # Do not keep the typed secrets in the widgets once the engine has them.
                self.vpn_pass.clear()
                self.vpn_key_pass.clear()
                self.toast.show_toast('Conf engine started in background.', 'info')

        self._run_async('net', lambda: self.api.connect_vpn(profile, username, password, key_password), done)

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
