"""Alem TV (IPTVPORTAL) Application generator and packager for LG webOS and Samsung Tizen.

Provides:
- Complete standalone Smart TV web application for Alem TV (HTML5, JS, CSS)
  with EPG, Flussonic stream URL builder, AresCipher (RSA-1024 + AES-128-CBC),
  D-pad spatial navigation, Player OSD, Timeshift, Channel list, and Settings.
- LG webOS .ipk builder (tm.alemtv.webos.ipk).
- Samsung Tizen .wgt builder (AlemTV0001.wgt) with W3C XML-DSig signature support.
"""

import io
import json
import os
import re
import sys
import tarfile
import time
import zipfile
from xml.sax.saxutils import escape as xml_escape

ALEMTV_WEBOS_ID = 'tm.alemtv.webos'
ALEMTV_PKG_ID = 'AlemTV0001'
ALEMTV_APP_ID = 'AlemTV0001.AlemTV'
ALEMTV_DEFAULT_PORTAL = 'http://telecom.alemtv.tm'

ALEMTV_PORTALS = [
    {'name': 'Turkmentelekom (telecom.alemtv.tm)', 'host': 'telecom.alemtv.tm', 'url': 'http://telecom.alemtv.tm'},
    {'name': 'AGTS / ASTU (astu.tmalem.tv)', 'host': 'astu.tmalem.tv', 'url': 'http://astu.tmalem.tv'},
    {'name': 'Arkadag (ark.alemtv.tm)', 'host': 'ark.alemtv.tm', 'url': 'http://ark.alemtv.tm'},
    {'name': 'ASTU Local (local-astu.alemtv.tm)', 'host': 'local-astu.alemtv.tm', 'url': 'http://local-astu.alemtv.tm'},
    {'name': 'IPTV TM Alem (iptv.tmalem.tv)', 'host': 'iptv.tmalem.tv', 'url': 'http://iptv.tmalem.tv'},
    {'name': 'SilkyOpenChannel (Бесплатные тест-каналы)', 'host': 'iptv-org.github.io', 'url': 'https://iptv-org.github.io/iptv/index.m3u8'},
    {'name': 'IPTVPORTAL Cloud (go.iptvportal.ru)', 'host': 'go.iptvportal.ru', 'url': 'http://go.iptvportal.ru'},
    {'name': 'IPTVPORTAL Demo (nextouch.iptvportal.ru)', 'host': 'nextouch.iptvportal.ru', 'url': 'http://nextouch.iptvportal.ru'},
]

def _resource_path(*parts):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)

def get_alemtv_assets():
    """Load or generate default icons for Alem TV packaging."""
    icons = {}
    for key, filename in (
        ('icon', 'alemtv_icon.png'),
        ('large_icon', 'alemtv_large_icon.png'),
        ('splash', 'alemtv_splash.png'),
        ('ui', 'alemtv_ui.png'),
    ):
        p = _resource_path('assets', filename)
        if os.path.isfile(p):
            with open(p, 'rb') as f:
                icons[key] = f.read()
    return icons

def build_alemtv_css():
    """Generate modern, TV-optimized stylesheet compatible with WebKit / Blink."""
    return """
* { box-sizing: border-box; margin: 0; padding: 0; user-select: none; -webkit-user-select: none; }
html, body {
  width: 100%; height: 100%;
  width: 1920px; height: 1080px;
  overflow: hidden;
  background: #080c11 !important;
  color: #f0f4f8 !important;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans", sans-serif;
}

button, .btn {
  -webkit-appearance: none;
  -moz-appearance: none;
  appearance: none;
  background: #1a2634;
  color: #ffffff;
  border: 2px solid #2e4157;
  border-radius: 12px;
  padding: 12px 28px;
  font-size: 20px;
  font-weight: 600;
  display: inline-block;
  cursor: pointer;
  outline: none;
  font-family: inherit;
  transition: transform 0.12s ease, border-color 0.12s ease, box-shadow 0.12s ease, background 0.12s ease;
}
.btn-primary {
  background: #ff8800;
  background: -webkit-linear-gradient(135deg, #ff8800, #e05e00);
  background: linear-gradient(135deg, #ff8800, #e05e00);
  border-color: #ff8800;
  color: #ffffff;
}

.focusable { outline: none; cursor: pointer; transition: transform 0.12s ease, border-color 0.12s ease, box-shadow 0.12s ease; }
.focusable:focus, .focused {
  outline: 3px solid #ff8800 !important;
  outline-offset: 3px;
  box-shadow: 0 0 20px rgba(255, 136, 0, 0.7) !important;
  border-color: #ff8800 !important;
  transform: scale(1.03);
  z-index: 10;
}

/* Screens */
.screen {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  width: 1920px; height: 1080px;
  display: none;
  overflow: hidden;
  background: #080c11;
}
.screen.active {
  display: flex !important;
}

/* Splash Screen */
#splashScreen {
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: radial-gradient(circle at 50% 45%, #152230 0%, #080c11 75%);
}
.splash-logo { width: 140px; height: 140px; border-radius: 28px; box-shadow: 0 10px 30px rgba(0,0,0,0.6); margin-bottom: 24px; }
.splash-title { font-size: 40px; font-weight: 700; letter-spacing: 1px; color: #ffffff; margin-bottom: 12px; }
.splash-sub { font-size: 20px; color: #ff8800; font-weight: 600; margin-bottom: 30px; }
.spinner {
  width: 44px; height: 44px;
  border: 4px solid rgba(255,136,0,0.25);
  border-top-color: #ff8800;
  border-radius: 50%;
  -webkit-animation: spin 0.8s linear infinite;
  animation: spin 0.8s linear infinite;
}
@-webkit-keyframes spin { to { -webkit-transform: rotate(360deg); } }
@keyframes spin { to { transform: rotate(360deg); } }

.splash-err { display: none; margin-top: 30px; text-align: center; max-width: 600px; }
.splash-err-msg { font-size: 20px; color: #ff7485; margin-bottom: 20px; }

/* Main UI */
#mainScreen { flex-direction: row; }
.sidebar { width: 340px; height: 1080px; background: #0c1219; border-right: 1px solid #1a2533; display: flex; flex-direction: column; padding: 24px 18px; }
.brand-box { display: flex; align-items: center; gap: 16px; margin-bottom: 28px; padding-left: 8px; }
.brand-box img { width: 48px; height: 48px; border-radius: 12px; }
.brand-text { font-size: 24px; font-weight: 800; color: #22c55e; letter-spacing: 1px; }
.nav-menu { display: flex; flex-direction: column; gap: 6px; flex: 1; overflow-y: auto; }
.nav-item { display: flex; align-items: center; gap: 14px; padding: 14px 18px; border-radius: 12px; font-size: 20px; color: #94a3b8; font-weight: 600; background: transparent; border: 2px solid transparent; text-align: left; }
.nav-item.active { background: #162230; color: #ffffff; border-color: #ff8800; }
.nav-item .icon { font-size: 20px; width: 26px; text-align: center; }
.sidebar-foot { padding: 14px 8px 0; border-top: 1px solid #1a2533; display: flex; flex-direction: column; gap: 6px; font-size: 14px; color: #64748b; }

/* Content Area */
.content-pane { flex: 1; height: 1080px; display: flex; flex-direction: column; padding: 24px 36px; overflow: hidden; }
.content-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-title { font-size: 32px; font-weight: 700; color: #ffffff; }
.top-info { display: flex; align-items: center; gap: 24px; font-size: 20px; color: #94a3b8; }
.portal-badge { background: #162230; border: 1px solid #283a4f; color: #ff8800; font-size: 15px; font-weight: 600; padding: 6px 14px; border-radius: 20px; }
.clock-text { font-weight: 700; color: #e2e8f0; }

/* Channel Grid */
.channel-grid { flex: 1; display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; overflow-y: auto; padding: 8px 4px 24px; align-content: start; }
.channel-card { background: #101822; border: 2px solid #1b2838; border-radius: 16px; padding: 18px; display: flex; flex-direction: column; gap: 10px; height: 190px; position: relative; }
.card-top { display: flex; align-items: center; gap: 14px; }
.channel-logo { width: 56px; height: 56px; border-radius: 10px; background: #172332; object-fit: contain; padding: 4px; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #ff8800; font-size: 18px; flex-shrink: 0; }
.channel-num { font-size: 14px; font-weight: 700; color: #ff8800; margin-bottom: 2px; }
.channel-name { font-size: 20px; font-weight: 700; color: #ffffff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 230px; }
.card-fav { position: absolute; top: 16px; right: 16px; font-size: 20px; color: #64748b; }
.card-fav.active { color: #ff8800; }
.program-title { font-size: 16px; color: #cbd5e1; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 4px; }
.progress-bar-box { width: 100%; height: 6px; background: #1c2a3b; border-radius: 3px; overflow: hidden; margin-top: 4px; }
.progress-fill { width: 0%; height: 100%; background: #ff8800; border-radius: 3px; transition: width 0.3s ease; }
.card-foot { display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: #64748b; margin-top: auto; }

/* Hints Footer */
.hints-bar { height: 50px; display: flex; align-items: center; gap: 32px; font-size: 16px; color: #64748b; border-top: 1px solid #1a2533; padding-top: 12px; }
.hint-chip { display: flex; align-items: center; gap: 8px; }
.hint-key { background: #1b2737; color: #cbd5e1; font-size: 13px; font-weight: 700; padding: 4px 8px; border-radius: 6px; border: 1px solid #2a3d54; }

/* Fullscreen Video Player Screen */
#playerScreen { background: #000000; position: absolute; top: 0; left: 0; right: 0; bottom: 0; width: 1920px; height: 1080px; overflow: hidden; }
#videoElement { width: 100%; height: 100%; object-fit: contain; background: #000; }
.player-osd { position: absolute; top: 0; left: 0; right: 0; bottom: 0; display: flex; flex-direction: column; justify-content: space-between; padding: 40px 60px; pointer-events: none; transition: opacity 0.25s ease; opacity: 1; }
.player-osd.hidden { opacity: 0; }
.osd-hints { display: flex; flex-wrap: wrap; gap: 10px 22px; justify-content: center; padding: 10px 0 0; opacity: 0.85; }
.osd-hints .hint-chip { font-size: 14px; padding: 3px 10px; }
.osd-top { display: flex; justify-content: space-between; align-items: center; background: linear-gradient(180deg, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0) 100%); padding: 24px 30px; margin: -40px -60px 0; }
.osd-channel { display: flex; align-items: center; gap: 18px; }
.osd-logo { width: 64px; height: 64px; border-radius: 12px; background: rgba(255,255,255,0.08); padding: 4px; object-fit: contain; }
.osd-ch-title { font-size: 32px; font-weight: 800; color: #ffffff; }
.osd-badge { background: #ff8800; color: #000; font-size: 14px; font-weight: 800; padding: 4px 10px; border-radius: 6px; letter-spacing: 1px; }
.osd-bottom { display: flex; flex-direction: column; gap: 14px; background: linear-gradient(0deg, rgba(0,0,0,0.92) 0%, rgba(0,0,0,0) 100%); padding: 30px 40px; margin: 0 -60px -40px; pointer-events: auto; }
.osd-prog-title { font-size: 26px; font-weight: 700; color: #ffffff; }
.osd-timeline { display: flex; align-items: center; gap: 20px; }
.osd-track { flex: 1; height: 8px; background: rgba(255,255,255,0.2); border-radius: 4px; overflow: hidden; }
.osd-fill { width: 0%; height: 100%; background: #ff8800; border-radius: 4px; }
.osd-time { font-size: 18px; font-weight: 600; color: #cbd5e1; min-width: 60px; }
.osd-ctrls { display: flex; align-items: center; gap: 18px; margin-top: 6px; }
.osd-btn { background: rgba(255,255,255,0.12); color: #fff; border: 2px solid rgba(255,255,255,0.2); border-radius: 10px; padding: 10px 22px; font-size: 17px; font-weight: 600; }

/* Quick Channel Zapping Drawer */
.zap-drawer { position: absolute; left: 0; top: 0; bottom: 0; width: 440px; background: rgba(10, 16, 24, 0.94); border-right: 2px solid #ff8800; display: none; flex-direction: column; padding: 30px 20px; z-index: 20; }
.zap-drawer.active { display: flex; }
.zap-title { font-size: 24px; font-weight: 700; color: #ff8800; margin-bottom: 20px; padding-left: 8px; }
.zap-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
.zap-item { display: flex; align-items: center; gap: 14px; padding: 12px 14px; border-radius: 10px; background: #121d2b; border: 2px solid transparent; }
.zap-item.current { border-color: #ff8800; }
.zap-logo { width: 44px; height: 44px; border-radius: 8px; background: #1a2738; object-fit: contain; }
.zap-name { font-size: 18px; font-weight: 700; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* Direct Channel Tuning Banner */
.digit-banner { position: absolute; top: 40px; right: 50px; background: rgba(12, 18, 26, 0.92); border: 3px solid #ff8800; border-radius: 14px; padding: 12px 28px; font-size: 32px; font-weight: 800; color: #ff8800; display: none; z-index: 30; box-shadow: 0 4px 25px rgba(0,0,0,0.8); }

/* Modals */
.modal-overlay { position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: rgba(4, 7, 10, 0.88); display: none; align-items: center; justify-content: center; z-index: 50; }
.modal-overlay.active { display: flex !important; }
.modal-box { width: 1100px; max-height: 860px; background: #0f1722; border: 2px solid #233448; border-radius: 20px; padding: 36px 44px; display: flex; flex-direction: column; gap: 20px; box-shadow: 0 10px 40px rgba(0,0,0,0.8); }
.modal-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1f2f42; padding-bottom: 16px; }
.modal-title { font-size: 28px; font-weight: 700; color: #ffffff; }
.epg-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; max-height: 600px; padding-right: 8px; }
.epg-item { display: flex; align-items: flex-start; gap: 20px; padding: 16px 20px; border-radius: 12px; background: #141f2d; border: 2px solid transparent; }
.epg-time { font-size: 20px; font-weight: 700; color: #ff8800; min-width: 140px; }
.epg-prog-name { font-size: 20px; font-weight: 600; color: #ffffff; }
.epg-desc { font-size: 15px; color: #8fa0b5; margin-top: 4px; }
.epg-badge-live { display:inline-block; margin-left:8px; padding:1px 8px; border-radius:10px; background:#16a34a; color:#fff; font-size:11px; font-weight:700; letter-spacing:.06em; }
.epg-badge-rec { background: #22c55e; color: #000; font-size: 12px; font-weight: 700; padding: 3px 8px; border-radius: 4px; margin-left: 10px; }

/* Settings Screen */
.settings-box { max-width: 900px; margin: 20px auto 0; background: #0e1520; border: 2px solid #1d2a3a; border-radius: 20px; padding: 36px 44px; display: flex; flex-direction: column; gap: 26px; }
.form-group { display: flex; flex-direction: column; gap: 8px; }
.form-label { font-size: 17px; font-weight: 700; color: #cbd5e1; text-transform: uppercase; letter-spacing: 0.5px; }
.form-select, .form-input { background: #162232; border: 2px solid #283a4f; color: #ffffff; border-radius: 10px; padding: 14px 18px; font-size: 20px; font-weight: 600; width: 100%; }
.form-row { display: flex; gap: 16px; align-items: center; }
.info-row { display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid #192535; font-size: 18px; }
.info-lbl { color: #8fa0b5; }
.info-val { color: #ffffff; font-weight: 700; }
"""

def build_alemtv_js(default_portal=ALEMTV_DEFAULT_PORTAL):
    """Generate the complete Alem TV Smart TV client JavaScript (compatible with ES5+ engines)."""
    safe_default_portal = json.dumps(str(default_portal or ALEMTV_DEFAULT_PORTAL).rstrip('/'))
    safe_portals_json = json.dumps(ALEMTV_PORTALS)

    template = r"""(function(){
  var DEFAULT_PORTAL = __DEFAULT_PORTAL__;
  var PORTAL_LIST = __PORTAL_LIST__;
  var RSA_N_HEX = "cf2cde3395737e94f0f0ca7474797d285170eb7acd41628a946f92a00138225a23c2b7307da8c182536bfa325d7a111dc82eb0825c85614d807b46bdc0bea8231dde941b2d435782dabed3381b48f30aa19bff261eec4086b56d5c45ea394b8f386d1ad75b7e8c5d37d517e83e598719afb2b05b790e41f132858321f693c025";
  var IV_STR = "arescrypt       ";

  // Check if target portal was configured or changed during installation
  var bakedDefault = DEFAULT_PORTAL;
  var lastBaked = localStorage.getItem("alem_last_baked");
  var currentPortal = localStorage.getItem("alem_portal");
  var normalizePortalKey = function(url) {
    return String(url || "").replace(/\/+$/, "").toLowerCase();
  };
  var hadPortalBinding = !!localStorage.getItem("alem_mac");
  var portalBindingChanged = hadPortalBinding && (
    !currentPortal || (lastBaked !== bakedDefault && normalizePortalKey(currentPortal) !== normalizePortalKey(bakedDefault))
  );
  if (!currentPortal || lastBaked !== bakedDefault) {
    currentPortal = bakedDefault;
    localStorage.setItem("alem_portal", bakedDefault);
    localStorage.setItem("alem_last_baked", bakedDefault);
    localStorage.removeItem("alem_sid");
    if (portalBindingChanged) localStorage.removeItem("alem_mac");
    sessionId = null;
  }

  // Ensure current portal exists in PORTAL_LIST
  var foundBaked = false;
  for (var bi = 0; bi < PORTAL_LIST.length; bi++) {
    if (PORTAL_LIST[bi].url.replace(/\/+$/, "") === currentPortal.replace(/\/+$/, "")) {
      foundBaked = true;
      break;
    }
  }
  if (!foundBaked && currentPortal) {
    var cleanHost = currentPortal.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    PORTAL_LIST.unshift({
      name: "Пользовательский (" + cleanHost + ")",
      url: currentPortal,
      host: cleanHost
    });
  }

  var currentMac = localStorage.getItem("alem_mac") || null;
  var sessionId = localStorage.getItem("alem_sid") || null;
  if (portalBindingChanged) {
    currentMac = null;
    sessionId = null;
  }
  function clearTerminalBinding() {
    localStorage.removeItem("alem_mac");
    localStorage.removeItem("alem_sid");
    currentMac = null;
    sessionId = null;
  }
  function setCurrentPortal(url) {
    var nextPortal = String(url || "");
    if (normalizePortalKey(nextPortal) !== normalizePortalKey(currentPortal)) {
      clearTerminalBinding();
    }
    currentPortal = nextPortal;
    localStorage.setItem("alem_portal", nextPortal);
  }
  var serverTimeOffset = 0;
  var domainConfig = null;
  var authData = { channels: [], playlists: [], tokens: {}, domain: "tmalem" };
  var epgNow = {};
  var currentCategory = "all";
  var currentPlayingChannel = null;
  // Playback state (Swift PlayerModel): live | timeshift | archive.
  var playerMode = "live";
  var timeshiftBehind = 0; // seconds behind live edge (timeshift mode)
  var archiveProgram = null; // {startMs, stopMs, title} in archive mode
  var contentStartMs = 0; // server time (ms) the current stream corresponds to
  var pausedAtMs = 0;
  var epgDayOffset = 0; // EPG modal day shift vs server today
  var osdTimer = null;
  var osdHidden = true; // OSD visibility for overlay logic (panel vs none)
  var digitBuffer = "";
  var digitTimer = null;
  var updateInterval = null;

  var currentFocusIndex = 0;
  var currentScreen = "splash";

  // Elements
  var splashScreen = document.getElementById("splashScreen");
  var mainScreen = document.getElementById("mainScreen");
  var playerScreen = document.getElementById("playerScreen");
  var videoElement = document.getElementById("videoElement");
  var playerOsd = document.getElementById("playerOsd");

  if (videoElement) {
    videoElement.onerror = function() {
      var err = videoElement.error;
      var code = err ? err.code : 0;
      console.warn("Video playback error code:", code);
      var progEl = document.getElementById("osdProgTitle");
      if (progEl) progEl.textContent = "Ошибка воспроизведения (Код " + code + "). Нажмите [Return] для возврата.";
      showOsd();
    };
  }

  function pad2(n) {
    var s = String(n || 0);
    return s.length < 2 ? "0" + s : s;
  }

  // Crypto: safe modPow without BigInt literals (supports Chromium 47+ without SyntaxError)
  function modPowSafe(b, e, m) {
    if (typeof BigInt === "function") {
      try {
        var r = BigInt(1);
        b = BigInt(b) % BigInt(m);
        e = BigInt(e);
        m = BigInt(m);
        var zero = BigInt(0);
        var one = BigInt(1);
        var two = BigInt(2);
        while (e > zero) {
          if (e % two === one) r = (r * b) % m;
          e = e / two;
          b = (b * b) % m;
        }
        return r;
      } catch(x){}
    }
    return null;
  }

  function rsaEncrypt(blockBytes) {
    if (typeof BigInt !== "function") return "";
    var N = BigInt("0x" + RSA_N_HEX);
    var E = BigInt(65537);
    var k = 128; // 1024 bit = 128 bytes
    if (blockBytes.length > k - 11) return "";
    var em = new Uint8Array(k);
    em[0] = 0x00; em[1] = 0x02;
    var psLen = k - blockBytes.length - 3;
    for (var i = 0; i < psLen; i++) {
      var r = 0;
      while (r === 0) r = Math.floor(Math.random() * 256);
      em[2 + i] = r;
    }
    em[2 + psLen] = 0x00;
    em.set(blockBytes, 3 + psLen);

    var m = BigInt(0);
    for (var j = 0; j < k; j++) m = (m * BigInt(256)) + BigInt(em[j]);
    var c = modPowSafe(m, E, N);
    if (c === null) return "";
    var hex = c.toString(16);
    while (hex.length < k * 2) hex = "0" + hex;
    var res = new Uint8Array(k);
    for (var h = 0; h < k; h++) res[h] = parseInt(hex.substr(h * 2, 2), 16);
    var bin = "";
    for (var b = 0; b < res.length; b++) bin += String.fromCharCode(res[b]);
    return btoa(bin);
  }

  function buildAresHeader(keyBytes, ts) {
    var buf = new Uint8Array(78);
    var view = new DataView(buf.buffer);
    view.setUint16(0, 1, true);
    for (var i = 0; i < 3; i++) buf[2 + i] = "aes".charCodeAt(i);
    for (var i = 0; i < 3; i++) buf[18 + i] = "cbc".charCodeAt(i);
    for (var i = 0; i < 6; i++) buf[26 + i] = "base64".charCodeAt(i);
    view.setUint16(34, 128, true);
    view.setUint16(36, 16, true);
    view.setUint16(38, 46, true);
    view.setUint16(40, 62, true);
    view.setUint32(42, ts, true);
    buf.set(keyBytes, 46);
    for (var iv = 0; iv < 16; iv++) buf[62 + iv] = IV_STR.charCodeAt(iv);
    return rsaEncrypt(buf);
  }

  // MAC is assigned by the server (macaddr:null + create_macaddr:true on
  // register_terminal/login). Never invent a random MAC locally: an
  // unregistered random MAC makes the server answer -32002 "not found".
  function getStoredMac() {
    var m = localStorage.getItem("alem_mac") || currentMac || null;
    if (m && m.length >= 12) return m;
    return null;
  }

  // ---- AresCipher: AES-128-CBC (pure JS, ES5) ----
  // Matches Swift AresCipher / tools/probe.py: IV "arescrypt       ",
  // PKCS7 padding, base64 body, RSA-1024 header in "Ares-Cipher".
  var AES_SBOX = [
  0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
  0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
  0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
  0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
  0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
  0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
  0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
  0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
  0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
  0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
  0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
  0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
  0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
  0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
  0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
  0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16];
  var AES_INV_SBOX = [
  0x52,0x09,0x6a,0xd5,0x30,0x36,0xa5,0x38,0xbf,0x40,0xa3,0x9e,0x81,0xf3,0xd7,0xfb,
  0x7c,0xe3,0x39,0x82,0x9b,0x2f,0xff,0x87,0x34,0x8e,0x43,0x44,0xc4,0xde,0xe9,0xcb,
  0x54,0x7b,0x94,0x32,0xa6,0xc2,0x23,0x3d,0xee,0x4c,0x95,0x0b,0x42,0xfa,0xc3,0x4e,
  0x08,0x2e,0xa1,0x66,0x28,0xd9,0x24,0xb2,0x76,0x5b,0xa2,0x49,0x6d,0x8b,0xd1,0x25,
  0x72,0xf8,0xf6,0x64,0x86,0x68,0x98,0x16,0xd4,0xa4,0x5c,0xcc,0x5d,0x65,0xb6,0x92,
  0x6c,0x70,0x48,0x50,0xfd,0xed,0xb9,0xda,0x5e,0x15,0x46,0x57,0xa7,0x8d,0x9d,0x84,
  0x90,0xd8,0xab,0x00,0x8c,0xbc,0xd3,0x0a,0xf7,0xe4,0x58,0x05,0xb8,0xb3,0x45,0x06,
  0xd0,0x2c,0x1e,0x8f,0xca,0x3f,0x0f,0x02,0xc1,0xaf,0xbd,0x03,0x01,0x13,0x8a,0x6b,
  0x3a,0x91,0x11,0x41,0x4f,0x67,0xdc,0xea,0x97,0xf2,0xcf,0xce,0xf0,0xb4,0xe6,0x73,
  0x96,0xac,0x74,0x22,0xe7,0xad,0x35,0x85,0xe2,0xf9,0x37,0xe8,0x1c,0x75,0xdf,0x6e,
  0x47,0xf1,0x1a,0x71,0x1d,0x29,0xc5,0x89,0x6f,0xb7,0x62,0x0e,0xaa,0x18,0xbe,0x1b,
  0xfc,0x56,0x3e,0x4b,0xc6,0xd2,0x79,0x20,0x9a,0xdb,0xc0,0xfe,0x78,0xcd,0x5a,0xf4,
  0x1f,0xdd,0xa8,0x33,0x88,0x07,0xc7,0x31,0xb1,0x12,0x10,0x59,0x27,0x80,0xec,0x5f,
  0x60,0x51,0x7f,0xa9,0x19,0xb5,0x4a,0x0d,0x2d,0xe5,0x7a,0x9f,0x93,0xc9,0x9c,0xef,
  0xa0,0xe0,0x3b,0x4d,0xae,0x2a,0xf5,0xb0,0xc8,0xeb,0xbb,0x3c,0x83,0x53,0x99,0x61,
  0x17,0x2b,0x04,0x7e,0xba,0x77,0xd6,0x26,0xe1,0x69,0x14,0x63,0x55,0x21,0x0c,0x7d];
  var AES_RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36];

  function aesMul(a, b) {
    var r = 0;
    while (b) { if (b & 1) r ^= a; a = ((a << 1) ^ ((a & 0x80) ? 0x1b : 0)) & 0xff; b >>= 1; }
    return r & 0xff;
  }
  function aesKeyExpand(key) {
    var w = new Uint8Array(176);
    for (var i = 0; i < 16; i++) w[i] = key[i] & 0xff;
    var rconIdx = 0;
    for (var i = 16; i < 176; i += 4) {
      var t0 = w[i - 4], t1 = w[i - 3], t2 = w[i - 2], t3 = w[i - 1];
      if (i % 16 === 0) {
        var tmp = t0;
        t0 = AES_SBOX[t1] ^ AES_RCON[rconIdx++]; t1 = AES_SBOX[t2]; t2 = AES_SBOX[t3]; t3 = AES_SBOX[tmp];
      }
      w[i] = w[i - 16] ^ t0; w[i + 1] = w[i - 15] ^ t1; w[i + 2] = w[i - 14] ^ t2; w[i + 3] = w[i - 13] ^ t3;
    }
    return w;
  }
  function aesAddRoundKey(s, rk, round) {
    for (var i = 0; i < 16; i++) s[i] ^= rk[round * 16 + i];
  }
  function aesSubBytes(s) { for (var i = 0; i < 16; i++) s[i] = AES_SBOX[s[i]]; }
  function aesInvSubBytes(s) { for (var i = 0; i < 16; i++) s[i] = AES_INV_SBOX[s[i]]; }
  function aesShiftRows(s) {
    var t;
    t = s[1]; s[1] = s[5]; s[5] = s[9]; s[9] = s[13]; s[13] = t;
    t = s[2]; s[2] = s[10]; s[10] = t; t = s[6]; s[6] = s[14]; s[14] = t;
    t = s[3]; s[3] = s[15]; s[15] = s[11]; s[11] = s[7]; s[7] = t;
  }
  function aesInvShiftRows(s) {
    var t;
    t = s[13]; s[13] = s[9]; s[9] = s[5]; s[5] = s[1]; s[1] = t;
    t = s[2]; s[2] = s[10]; s[10] = t; t = s[6]; s[6] = s[14]; s[14] = t;
    t = s[7]; s[7] = s[11]; s[11] = s[15]; s[15] = s[3]; s[3] = t;
  }
  function aesMixColumns(s) {
    for (var c = 0; c < 4; c++) {
      var i = c * 4;
      var a0 = s[i], a1 = s[i + 1], a2 = s[i + 2], a3 = s[i + 3];
      s[i] = aesMul(a0, 2) ^ aesMul(a1, 3) ^ a2 ^ a3;
      s[i + 1] = a0 ^ aesMul(a1, 2) ^ aesMul(a2, 3) ^ a3;
      s[i + 2] = a0 ^ a1 ^ aesMul(a2, 2) ^ aesMul(a3, 3);
      s[i + 3] = aesMul(a0, 3) ^ a1 ^ a2 ^ aesMul(a3, 2);
    }
  }
  function aesInvMixColumns(s) {
    for (var c = 0; c < 4; c++) {
      var i = c * 4;
      var a0 = s[i], a1 = s[i + 1], a2 = s[i + 2], a3 = s[i + 3];
      s[i] = aesMul(a0, 14) ^ aesMul(a1, 11) ^ aesMul(a2, 13) ^ aesMul(a3, 9);
      s[i + 1] = aesMul(a0, 9) ^ aesMul(a1, 14) ^ aesMul(a2, 11) ^ aesMul(a3, 13);
      s[i + 2] = aesMul(a0, 13) ^ aesMul(a1, 9) ^ aesMul(a2, 14) ^ aesMul(a3, 11);
      s[i + 3] = aesMul(a0, 11) ^ aesMul(a1, 13) ^ aesMul(a2, 9) ^ aesMul(a3, 14);
    }
  }
  function aesEncryptBlock(block, rk) {
    var s = new Uint8Array(16);
    for (var i = 0; i < 16; i++) s[i] = block[i];
    aesAddRoundKey(s, rk, 0);
    for (var r = 1; r < 10; r++) { aesSubBytes(s); aesShiftRows(s); aesMixColumns(s); aesAddRoundKey(s, rk, r); }
    aesSubBytes(s); aesShiftRows(s); aesAddRoundKey(s, rk, 10);
    return s;
  }
  function aesDecryptBlock(block, rk) {
    var s = new Uint8Array(16);
    for (var i = 0; i < 16; i++) s[i] = block[i];
    aesAddRoundKey(s, rk, 10);
    for (var r = 9; r >= 1; r--) { aesInvShiftRows(s); aesInvSubBytes(s); aesAddRoundKey(s, rk, r); aesInvMixColumns(s); }
    aesInvShiftRows(s); aesInvSubBytes(s); aesAddRoundKey(s, rk, 0);
    return s;
  }
  function aesCbcEncrypt(plain, key, iv) {
    var rk = aesKeyExpand(key);
    var pad = 16 - (plain.length % 16);
    var total = plain.length + pad;
    var out = new Uint8Array(total);
    var prev = new Uint8Array(16);
    for (var i = 0; i < 16; i++) prev[i] = iv[i];
    for (var off = 0; off < total; off += 16) {
      var blk = new Uint8Array(16);
      for (var j = 0; j < 16; j++) {
        var b = (off + j < plain.length) ? plain[off + j] : pad;
        blk[j] = b ^ prev[j];
      }
      var enc = aesEncryptBlock(blk, rk);
      for (var k = 0; k < 16; k++) { out[off + k] = enc[k]; prev[k] = enc[k]; }
    }
    return out;
  }
  function aesCbcDecrypt(cipher, key, iv) {
    if (cipher.length === 0 || cipher.length % 16 !== 0) throw new Error("bad ciphertext length");
    var rk = aesKeyExpand(key);
    var out = new Uint8Array(cipher.length);
    var prev = new Uint8Array(16);
    for (var i = 0; i < 16; i++) prev[i] = iv[i];
    for (var off = 0; off < cipher.length; off += 16) {
      var blk = new Uint8Array(16);
      for (var j = 0; j < 16; j++) blk[j] = cipher[off + j];
      var dec = aesDecryptBlock(blk, rk);
      for (var k = 0; k < 16; k++) { out[off + k] = dec[k] ^ prev[k]; prev[k] = blk[k]; }
    }
    var pad = out[out.length - 1];
    if (pad < 1 || pad > 16) throw new Error("bad padding");
    var res = new Uint8Array(out.length - pad);
    for (var n = 0; n < res.length; n++) res[n] = out[n];
    return res;
  }
  function utf8Encode(str) {
    var utf8 = unescape(encodeURIComponent(str));
    var out = new Uint8Array(utf8.length);
    for (var i = 0; i < utf8.length; i++) out[i] = utf8.charCodeAt(i) & 0xff;
    return out;
  }
  function utf8Decode(bytes) {
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i] & 0xff);
    return decodeURIComponent(escape(bin));
  }
  function bytesToBase64(bytes) {
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i] & 0xff);
    return btoa(bin);
  }
  function base64ToBytes(b64) {
    var bin = atob(String(b64).replace(/\\s/g, ""));
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i) & 0xff;
    return out;
  }
  var ARES_IV = utf8Encode(IV_STR);
  function aresEncrypt(plainText, keyBytes) {
    return bytesToBase64(aesCbcEncrypt(utf8Encode(plainText), keyBytes, ARES_IV));
  }
  function aresDecrypt(b64, keyBytes) {
    return utf8Decode(aesCbcDecrypt(base64ToBytes(b64), keyBytes, ARES_IV));
  }
  function randomAresKey() {
    var k = new Uint8Array(16);
    for (var i = 0; i < 16; i++) k[i] = Math.floor(Math.random() * 256);
    return k;
  }
  window.aresEncrypt = aresEncrypt;
  window.aresDecrypt = aresDecrypt;
  window.aesCbcEncrypt = aesCbcEncrypt;
  window.aesCbcDecrypt = aesCbcDecrypt;
  window.buildAresHeader = buildAresHeader;
  window.utf8Encode = utf8Encode;
  window.utf8Decode = utf8Decode;

  // JSON-RPC errors: -32003 = unauthorized, -32002 = not found (terminal/session).
  // Both mean "sign in required" (Swift PortalError.requiresSignIn).
  function isRequiresSignIn(err) {
    if (!err) return false;
    if (err.code === -32003 || err.code === -32002) return true;
    var msg = (err.message || "").toLowerCase();
    return msg.indexOf("not registered") !== -1 || msg === "not found";
  }
  function isTerminalNotFound(err) {
    if (!err) return false;
    if (err.code === -32002) return true;
    var msg = (err.message || "").toLowerCase();
    return msg === "not found" || msg.indexOf("terminal not found") !== -1 || msg.indexOf("not registered") !== -1;
  }
  function friendlyLoginError(err) {
    if (!err) return "Неверные данные или доступ запрещён.";
    if (err.code === -32003) return "Доступ запрещён: неверный логин или пароль.";
    if (err.code === -32002) return "Терминал не найден на сервере. Проверьте логин/пароль или выберите другой сервер.";
    if (err.message && err.message.indexOf("HTTP ") === 0) {
      return "Сервер не найден (" + err.message + "). Проверьте адрес портала и сеть.";
    }
    if (err.message) return "Ошибка входа: " + err.message;
    return "Неверные данные или доступ запрещён.";
  }
  window.isRequiresSignIn = isRequiresSignIn;
  window.friendlyLoginError = friendlyLoginError;

  var rpcId = 1;
  function jsonRpc(method, params, encrypted, callback) {
    if (typeof XMLHttpRequest === "undefined") {
      if (callback) callback(new Error("XMLHttpRequest not available"), null);
      return;
    }
    var url = currentPortal.replace(/\/+$/, "") + "/jsonrpc/" + method;
    params = params || {};
    // Swift PortalClient.call: inject session_id/macaddr only when the key
    // is absent (explicit macaddr:null must be preserved for create_macaddr).
    if (!("macaddr" in params) && !("session_id" in params)) {
      if (sessionId) params.session_id = sessionId;
      else {
        var stored = getStoredMac();
        if (stored) params.macaddr = stored;
      }
    }
    var payload = JSON.stringify({ jsonrpc: "2.0", id: rpcId++, method: method, params: params });
    var cipherKey = null;
    var aresHeader = "";
    var bodyText = payload;
    if (encrypted) {
      cipherKey = randomAresKey();
      try { aresHeader = buildAresHeader(cipherKey, Math.floor(Date.now() / 1000)); } catch (e) { aresHeader = ""; }
      if (!aresHeader) {
        if (callback) callback(new Error("Ares-Cipher header failed"), null);
        return;
      }
      try {
        bodyText = aresEncrypt(payload, cipherKey);
      } catch (e) {
        if (callback) callback(e, null);
        return;
      }
    }
    var xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8");
    if (encrypted && aresHeader) xhr.setRequestHeader("Ares-Cipher", aresHeader);
    xhr.timeout = 5000;

    xhr.onload = function() {
      if (xhr.status >= 200 && xhr.status < 300) {
        var text = xhr.responseText;
        if (encrypted && cipherKey) {
          try { text = aresDecrypt(text, cipherKey); } catch (e) { callback(e, null); return; }
        }
        try {
          var res = JSON.parse(text);
          if (res.error) callback(res.error, null);
          else callback(null, res.result);
        } catch(e) { callback(e, null); }
      } else {
        var httpErr = new Error("HTTP " + xhr.status);
        httpErr.code = xhr.status;
        callback(httpErr, null);
      }
    };
    xhr.onerror = function() { callback(new Error("Network Error"), null); };
    xhr.ontimeout = function() { callback(new Error("Timeout"), null); };
    try {
      xhr.send(bodyText);
    } catch (e) {
      callback(e, null);
    }
  }
  window.jsonRpc = jsonRpc;

  function parseCsvChannels(csvText) {
    var channels = [];
    if (!csvText || typeof csvText !== "string") return channels;
    var lines = csvText.split("\n");
    for (var i = 0; i < lines.length; i++) {
      var l = lines[i].trim();
      if (!l) continue;
      var p = l.split(",");
      if (p.length >= 4) {
        channels.push({
          id: parseInt(p[0]) || (i + 1),
          title: (p[1] || "").replace(/^"|"$/g, ""),
          stream_url: (p[2] || "").replace(/^"|"$/g, ""),
          logo_url: (p[3] || "").replace(/^"|"$/g, ""),
          category: (p[4] || "Общие").replace(/^"|"$/g, ""),
          index: i + 1,
        });
      }
    }
    return channels;
  }

  function parseMedialistTable(table) {
    if (!Array.isArray(table) || table.length < 2) return [];
    var headers = table[0];
    if (!Array.isArray(headers)) return [];
    var channels = [];
    for (var i = 1; i < table.length; i++) {
      var row = table[i];
      if (!Array.isArray(row)) continue;
      var obj = {};
      for (var h = 0; h < headers.length && h < row.length; h++) {
        var k = headers[h];
        if (k) obj[k] = row[h];
      }
      if (obj.is_tv === false || obj.is_tv === 0 || obj.is_tv === "0") continue;
      var chId = parseInt(obj.id) || (channels.length + 1);
      var chTitle = obj.title || obj.name || ("Канал " + chId);
      channels.push({
        id: chId,
        index: parseInt(obj.index) || (channels.length + 1),
        title: chTitle,
        scheme: obj.protocol || "http",
        host: obj.host || "",
        port: obj.port || null,
        path: obj.path || "",
        auth: obj.auth || "",
        key: obj.key || null,
        encrypted: !!obj.encrypted,
        stream_url: obj.stream_url || "",
        timeshift_url: obj.timeshift_url || "",
        timeshift_archive_length: obj.timeshift_archive_length || 0,
        timeshift_offset: obj.timeshift_offset || 0,
        timeshift_auth: obj.timeshift_auth || obj.auth || "",
        logo_url: obj.logo_url || (currentPortal.replace(/\/+$/, "") + "/resources/portal/images/logo96/" + chId + ".png"),
        category: obj.category || "Общие",
      });
    }
    return channels;
  }

  function parsePortalDate(str) {
    if (!str || typeof str !== "string") return null;
    var m = str.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:([+-])(\d{2})(\d{2}))?/);
    if (!m) return null;
    var y = parseInt(m[1], 10);
    var mo = parseInt(m[2], 10) - 1;
    var d = parseInt(m[3], 10);
    var h = parseInt(m[4], 10);
    var mi = parseInt(m[5], 10);
    var s = parseInt(m[6], 10);
    if (m[7]) {
      var sign = (m[7] === "-") ? 1 : -1;
      var tzH = parseInt(m[8], 10);
      var tzM = parseInt(m[9], 10);
      return new Date(Date.UTC(y, mo, d, h, mi, s) + sign * (tzH * 3600 + tzM * 60) * 1000);
    }
    // Default server timezone UTC+5 (Ashgabat / Turkmenistan)
    return new Date(Date.UTC(y, mo, d, h, mi, s) - (5 * 3600 * 1000));
  }

  function getServerNow() {
    return Date.now() + (serverTimeOffset || 0);
  }

  // result_as:"csv" table: first row = headers, rest = data rows (Swift CSVTable).
  function parseCsvTable(table) {
    if (!Array.isArray(table) || table.length < 1) return [];
    var headers = table[0];
    if (!Array.isArray(headers)) return [];
    var rows = [];
    for (var i = 1; i < table.length; i++) {
      var row = table[i];
      if (!Array.isArray(row)) continue;
      var obj = {};
      for (var h = 0; h < headers.length && h < row.length; h++) {
        var k = headers[h];
        if (k) obj[k] = row[h];
      }
      rows.push(obj);
    }
    return rows;
  }

  // "YYYY-MM-DD" in server timezone UTC+5 (Swift PortalDate.dayString).
  function portalDayString(ms) {
    var d = new Date((ms || getServerNow()) + 5 * 3600 * 1000);
    return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate());
  }

  function hasArchive(ch) {
    return !!(ch && ch.timeshift_url);
  }

  // Swift StreamURLBuilder.isAvailableInArchive.
  function isInArchive(ch, progStartMs, nowMs) {
    if (!hasArchive(ch)) return false;
    if (!authData.timeshiftEnabled) return false;
    var depth = ch.timeshift_archive_length || 0;
    if (!depth) return false;
    return progStartMs >= (nowMs || getServerNow()) - depth * 1000;
  }
  window.parseCsvTable = parseCsvTable;
  window.portalDayString = portalDayString;
  window.isInArchive = isInArchive;

  function liveBase(channel) {
    if (!channel) return "";
    if (channel.stream_url) return channel.stream_url;
    if (channel.path && (channel.path.indexOf("http://") === 0 || channel.path.indexOf("https://") === 0)) {
      return channel.path;
    }
    var host = channel.host || currentPortal.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    if (!host) return "";
    var scheme = channel.scheme ? channel.scheme : "http";
    var port = channel.port ? (":" + channel.port) : "";
    var path = (channel.path || "").replace(/^\/+/, "");
    return scheme + "://" + host + port + "/" + path;
  }

  function timeshiftBase(channel) {
    var base = (channel && (channel.timeshift_url || channel.timeshiftURL)) || "";
    if (!base) return "";
    var q = base.indexOf("?");
    if (q !== -1) base = base.substring(0, q);
    if (base.indexOf(".m3u8") !== -1) {
      var slash = base.lastIndexOf("/");
      if (slash !== -1) base = base.substring(0, slash);
    }
    while (base.charAt(base.length - 1) === "/") base = base.substring(0, base.length - 1);
    return base;
  }

  function buildTokenQuery(channel, auth) {
    if (typeof auth !== "string") {
      auth = (channel && (channel.auth || channel.timeshift_auth)) || "";
    }
    if (!auth || typeof auth !== "string") return "";
    var tokens = (authData && authData.tokens) || {};
    var sessionAuth = auth.indexOf("iptvportal") !== -1 || auth.indexOf("flussonic") !== -1;
    var parts = [];
    if (sessionAuth && sessionId) {
      parts.push("token=" + sessionId);
    }
    var key = channel && channel.key;
    if ((auth.indexOf("iptvportal") !== -1 || auth.indexOf("arescrypt") !== -1) && key && tokens[key]) {
      var tok = tokens[key];
      var domainName = (domainConfig && domainConfig.name) || (authData && authData.domain) || "tmalem";
      var nowSec = Math.floor(getServerNow() / 1000);
      parts.push("token1=" + (tok.token1 || ""));
      parts.push("token2=" + (tok.token2 || ""));
      parts.push("domain=" + domainName);
      parts.push("key=" + key);
      parts.push("media_id=" + channel.id);
      parts.push("timestamp=" + nowSec);

      if (auth.indexOf("iptvportal") !== -1) {
        return "token=" + encodeURIComponent(parts.join("&"));
      }
    }
    return parts.join("&");
  }

  function resolveStreamUrl(channel, auth, mode, extra) {
    if (!channel) return "";
    mode = mode || "live";
    var streamAuth = "";
    if (typeof auth === "string") {
      streamAuth = auth;
    } else if (mode === "live") {
      streamAuth = (channel.auth && typeof channel.auth === "string") ? channel.auth : "";
    } else if (mode === "archive" || mode === "timeshift") {
      var ta = channel.timeshift_auth || channel.timeshiftAuth || channel.auth || "";
      streamAuth = (typeof ta === "string") ? ta : "";
    }

    var base = "";
    if (mode === "live") {
      base = liveBase(channel);
    } else if (mode === "archive") {
      var tsBase = timeshiftBase(channel);
      if (!tsBase) return liveBase(channel);
      var startSec = Math.floor(((extra && extra.start) || getServerNow()) / 1000) - (channel.timeshift_offset || 0);
      var durSec = Math.max(0, Math.floor(((extra && extra.duration) || 3600) / 1000));
      base = tsBase + "/index-" + startSec + "-" + durSec + ".m3u8";
    } else if (mode === "timeshift") {
      var tsBaseRel = timeshiftBase(channel);
      if (!tsBaseRel) return liveBase(channel);
      var behindSec = Math.max(0, Math.floor(((extra && extra.behind) || 300)));
      base = tsBaseRel + "/video-timeshift_rel-" + behindSec + ".m3u8";
    }
    if (!base) base = liveBase(channel);
    if (!base) base = channel.stream_url || channel.url || channel.path || "";
    var q = buildTokenQuery(channel, streamAuth);
    if (!q) return base;
    return base + (base.indexOf("?") !== -1 ? "&" : "?") + q;
  }

  window.resolveStreamUrl = resolveStreamUrl;
  window.buildTokenQuery = buildTokenQuery;
  window.parsePortalDate = parsePortalDate;
  window.authData = authData;
  window.getSilkyOpenChannelsList = getSilkyOpenChannelsList;
  window.parseM3uChannels = parseM3uChannels;
  window.handleAuthSuccess = handleAuthSuccess;

  // Screen Switching
  function showScreen(name) {
    currentScreen = name;
    var screens = ["splashScreen", "mainScreen", "playerScreen", "portalFallbackScreen"];
    for (var i = 0; i < screens.length; i++) {
      var el = document.getElementById(screens[i]);
      if (el) {
        if (screens[i] === name + "Screen" || (name === "splash" && screens[i] === "splashScreen") || (name === "main" && screens[i] === "mainScreen") || (name === "player" && screens[i] === "playerScreen") || (name === "portalFallback" && screens[i] === "portalFallbackScreen")) {
          el.className = "screen active";
          el.style.display = (screens[i] === "playerScreen" ? "block" : "flex");
        } else {
          el.className = "screen";
          el.style.display = "none";
        }
      }
    }
    if (window.closeEpgModal) window.closeEpgModal();
    if (window.closeSettings) window.closeSettings();
    updateFocus();
  }

  var bootWatchdog = null;
  // Boot Alem TV client
  window.bootAlemTV = function() {
    showScreen("splash");
    var statusEl = document.getElementById("splashStatus");
    var spinnerEl = document.getElementById("splashSpinner");
    var errEl = document.getElementById("splashErr");
    var macEl = document.getElementById("sidebarMac");

    updateSplashSelector();
    if (statusEl) statusEl.textContent = "Подключение к " + currentPortal.replace(/^https?:\/\//, "") + "…";
    if (spinnerEl) spinnerEl.style.display = "block";
    if (errEl) errEl.style.display = "none";
    var curMac = getStoredMac();
    if (macEl) macEl.textContent = "MAC: " + (curMac || "—");

    if (currentPortal.indexOf("iptv-org") !== -1 || currentPortal.indexOf("silky") !== -1 || currentPortal.indexOf(".m3u") !== -1) {
      loadSilkyOpenChannels();
      return;
    }

    if (bootWatchdog) clearTimeout(bootWatchdog);
    bootWatchdog = setTimeout(function() {
      if (currentScreen === "splash" && (!authData.channels || authData.channels.length === 0)) {
        showBootError("Сервер " + currentPortal.replace(/^https?:\/\//, "") + " не ответил вовремя. Выберите другой сервер или запустите автопоиск.");
      }
    }, 7000);

    // 1. Get Domain Config (Sync time & fetch portal settings)
    jsonRpc("get_domain_config", { terminal_type: "smarttv" }, false, function(err, res) {
      if (err) {
        console.warn("Domain config notice:", err);
      } else if (res) {
        domainConfig = res;
        var badge = document.getElementById("topPortalBadge");
        if (badge) badge.textContent = domainConfig.name || "AlemTV";
        if (domainConfig.now) {
          var sNow = parsePortalDate(domainConfig.now);
          if (sNow) serverTimeOffset = sNow.getTime() - Date.now();
        }
      }

      // 2. Cold start (Swift SessionStore.start): no bound MAC -> credentials.
      // Never try login with an invented MAC: the server answers -32002
      // "not found" for unregistered terminals.
      var storedMac = getStoredMac();
      if (!storedMac) {
        if (bootWatchdog) { clearTimeout(bootWatchdog); bootWatchdog = null; }
        showBootError("Терминал ещё не привязан на сервере " + currentPortal.replace(/^https?:\/\//, "") + ". Введите логин и пароль абонента (номер договора) для активации.");
        showLoginModal();
        return;
      }

      // 3. loginAndAuthorize: clear stale session, login by MAC (encrypted).
      if (statusEl) statusEl.textContent = "Авторизация терминала…";
      sessionId = null;
      localStorage.removeItem("alem_sid");
      var loginParams = {
        macaddr: storedMac,
        create_macaddr: false,
        require_session: true,
        require_user_id: true,
        terminal_type: "smarttv",
        firmware_version: "2.0 (SmartTV)"
      };

      jsonRpc("login", loginParams, true, function(lErr, lRes) {
        if (lErr || lRes === false || !lRes || !lRes.session_id) {
          if (lErr && lErr.extra && lErr.extra.macaddr) {
            currentMac = lErr.extra.macaddr;
            localStorage.setItem("alem_mac", currentMac);
            if (macEl) macEl.textContent = "MAC: " + currentMac;
          }
          if (isRequiresSignIn(lErr) || lRes === false) {
            if (bootWatchdog) { clearTimeout(bootWatchdog); bootWatchdog = null; }
            if (isTerminalNotFound(lErr)) clearTerminalBinding();
            showBootError("Терминал не зарегистрирован на сервере " + currentPortal.replace(/^https?:\/\//, "") + ". Введите логин и пароль абонента для активации или выберите открытый сервер.");
            showLoginModal();
          } else {
            var detail = (lErr && lErr.message) || "Сервер не вернул сессию";
            showBootError("Сервер " + currentPortal.replace(/^https?:\/\//, "") + ": " + detail);
          }
          return;
        }
        sessionId = lRes.session_id;
        localStorage.setItem("alem_sid", sessionId);
        if (lRes.macaddr) {
          currentMac = lRes.macaddr;
          localStorage.setItem("alem_mac", currentMac);
          if (macEl) macEl.textContent = "MAC: " + currentMac;
        }

        // 4. Authorize terminal to load channels and media tokens.
        if (statusEl) statusEl.textContent = "Загрузка каналов Alem TV…";
        var authParams = {
          version: 2,
          require_media_tokens: true,
          require_media_keys: false,
          result_as: "csv"
        };

        jsonRpc("authorize_terminal", authParams, false, function(aErr, aRes) {
          if (!aErr && aRes && (aRes.medialist || aRes.channels || aRes.channels_csv)) {
            handleAuthSuccess(aRes);
            return;
          }
          if (isRequiresSignIn(aErr)) {
            if (isTerminalNotFound(aErr)) clearTerminalBinding();
            sessionId = null;
            localStorage.removeItem("alem_sid");
            if (bootWatchdog) { clearTimeout(bootWatchdog); bootWatchdog = null; }
            showBootError("Сессия истекла или терминал не авторизован на сервере " + currentPortal.replace(/^https?:\/\//, "") + ". Введите логин и пароль абонента для повторной активации.");
            showLoginModal();
            return;
          }
          var detail = (aErr && aErr.message) || "Сервер не вернул список каналов";
          showBootError("Сервер " + currentPortal.replace(/^https?:\/\//, "") + ": " + detail);
        });
      });
    });
  };

  function showBootError(msg) {
    if (bootWatchdog) { clearTimeout(bootWatchdog); bootWatchdog = null; }
    var spinnerEl = document.getElementById("splashSpinner");
    var errEl = document.getElementById("splashErr");
    var errMsgEl = document.getElementById("splashErrMsg");
    var statusEl = document.getElementById("splashStatus");
    if (statusEl) statusEl.textContent = "Ошибка подключения к порталу";
    if (spinnerEl) spinnerEl.style.display = "none";
    if (errMsgEl) errMsgEl.textContent = msg;
    if (errEl) errEl.style.display = "block";
    updateSplashSelector();
    updateFocus();
  }

  function updateSplashSelector() {
    // 1. Update dropdown if present (legacy compatibility)
    var sel = document.getElementById("splashPortalSelector");
    if (sel) {
      sel.innerHTML = "";
      var foundMatch = false;
      PORTAL_LIST.forEach(function(p){
        var opt = document.createElement("option");
        opt.value = p.url;
        opt.textContent = p.name;
        if (p.url.replace(/\/+$/, "") === currentPortal.replace(/\/+$/, "")) {
          opt.selected = true;
          foundMatch = true;
        }
        sel.appendChild(opt);
      });
      if (!foundMatch && currentPortal) {
        var optCustom = document.createElement("option");
        optCustom.value = currentPortal;
        optCustom.textContent = "Пользовательский (" + currentPortal + ")";
        optCustom.selected = true;
        sel.appendChild(optCustom);
      }
    }

    // 2. Render TV-remote friendly focusable server buttons
    var btnBox = document.getElementById("splashServerButtons");
    if (btnBox) {
      btnBox.innerHTML = "";
      PORTAL_LIST.forEach(function(p){
        var btn = document.createElement("button");
        var isCur = (p.url.replace(/\/+$/, "") === currentPortal.replace(/\/+$/, ""));
        btn.className = "btn focusable " + (isCur ? "btn-primary" : "");
        btn.style.padding = "10px 18px";
        btn.style.fontSize = "16px";
        btn.style.margin = "4px";
        btn.textContent = (isCur ? "● " : "") + p.name;
        btn.onclick = function() {
          onSplashPortalSelected(p.url);
        };
        btnBox.appendChild(btn);
      });

      var customBtn = document.createElement("button");
      customBtn.className = "btn focusable";
      customBtn.style.padding = "10px 18px";
      customBtn.style.fontSize = "16px";
      customBtn.style.margin = "4px";
      customBtn.textContent = "✎ Свой URL/IP";
      customBtn.onclick = function() {
        showCustomPortalModal();
      };
      btnBox.appendChild(customBtn);
    }
  }

  window.showCustomPortalModal = function() {
    showSettings();
    setTimeout(function(){
      var inp = document.getElementById("customPortalInput");
      if (inp) {
        try { inp.focus(); } catch(e){}
      }
    }, 150);
  };

  window.onSplashPortalSelected = function(url) {
    if (!url) return;
    setCurrentPortal(url);
    bootAlemTV();
  };

  window.scanAndConnectPortal = function() {
    var spinnerEl = document.getElementById("splashSpinner");
    var errEl = document.getElementById("splashErr");
    var errMsgEl = document.getElementById("splashErrMsg");
    var statusEl = document.getElementById("splashStatus");
    if (spinnerEl) spinnerEl.style.display = "block";
    if (statusEl) statusEl.textContent = "Автопоиск доступного сервера...";
    if (errMsgEl) errMsgEl.textContent = "Сканирование серверов Alem TV...";

    var idx = 0;
    function tryNext() {
      if (idx >= PORTAL_LIST.length) {
        if (spinnerEl) spinnerEl.style.display = "none";
        if (errEl) errEl.style.display = "block";
        if (statusEl) statusEl.textContent = "Ошибка подключения к Alem TV";
        if (errMsgEl) errMsgEl.textContent = "Ни один из серверов Alem TV не ответил. Проверьте сеть или выберите сервер вручную.";
        updateSplashSelector();
        updateFocus();
        return;
      }
      var target = PORTAL_LIST[idx++];
      if (statusEl) statusEl.textContent = "Проверка: " + target.name + "...";
      var xhr = new XMLHttpRequest();
      var testUrl = target.url.replace(/\/+$/, "") + "/jsonrpc/check_update";
      xhr.open("POST", testUrl, true);
      xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8");
      xhr.timeout = 3500;
      xhr.onreadystatechange = function() {
        if (xhr.readyState === 4) {
          if (xhr.status >= 200 && xhr.status < 300) {
            setCurrentPortal(target.url);
            if (statusEl) statusEl.textContent = "Сервер найден: " + target.name;
            setTimeout(function() {
              bootAlemTV();
            }, 300);
          } else {
            tryNext();
          }
        }
      };
      xhr.ontimeout = function() { tryNext(); };
      xhr.onerror = function() { tryNext(); };
      try {
        var payload = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "check_update", params: { current_version: "2.0" } });
        xhr.send(payload);
      } catch (e) {
        tryNext();
      }
    }
    tryNext();
  };

  function parseM3uChannels(text) {
    if (!text) return [];
    var lines = text.split(/\r?\n/);
    var channels = [];
    var cur = null;
    for (var i = 0; i < lines.length; i++) {
      var l = lines[i].trim();
      if (!l) continue;
      if (l.indexOf("#EXTINF:") === 0) {
        cur = { id: channels.length + 1, index: channels.length + 1, category: "Общие", title: "Канал" };
        var nameMatch = l.match(/,(.+)$/);
        if (nameMatch) cur.title = nameMatch[1].trim();
        var groupMatch = l.match(/group-title="([^"]+)"/i);
        if (groupMatch) cur.category = groupMatch[1].trim();
        var logoMatch = l.match(/tvg-logo="([^"]+)"/i);
        if (logoMatch) cur.logo_url = logoMatch[1].trim();
      } else if (l.indexOf("#") !== 0 && cur) {
        cur.stream_url = l;
        channels.push(cur);
        cur = null;
        if (channels.length >= 100) break;
      }
    }
    return channels;
  }

  function getSilkyOpenChannelsList() {
    return [
      { id: 1, index: 1, title: "Mux HD Video Test", stream_url: "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8", category: "Тест", logo_url: "icon.png" },
      { id: 2, index: 2, title: "NASA TV HD Live", stream_url: "https://ntv1.akamaized.net/hls/live/2014075/NASA-NTV1-HLS/master.m3u8", category: "Наука", logo_url: "icon.png" },
      { id: 3, index: 3, title: "Red Bull TV Live", stream_url: "https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8", category: "Спорт", logo_url: "icon.png" },
      { id: 4, index: 4, title: "Euronews Russian", stream_url: "https://euronews-euronews-world-1-ru.samsung.wurl.tv/playlist.m3u8", category: "Новости", logo_url: "icon.png" },
      { id: 5, index: 5, title: "Al Jazeera English HD", stream_url: "https://live-hls-web-aje.getaj.net/AJE/01.m3u8", category: "Новости", logo_url: "icon.png" },
      { id: 6, index: 6, title: "France 24 English", stream_url: "https://f24hls-i.akamaihd.net/hls/live/221193/F24_EN_LO_HLS/master_500.m3u8", category: "Новости", logo_url: "icon.png" },
      { id: 7, index: 7, title: "DW News Live HD", stream_url: "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/index.m3u8", category: "Новости", logo_url: "icon.png" },
      { id: 8, index: 8, title: "CGTN News HD", stream_url: "https://news.cgtn.com/resource/live/english/cgtn-news.m3u8", category: "Новости", logo_url: "icon.png" },
      { id: 9, index: 9, title: "Sintel 4K HLS Test", stream_url: "https://bitmovin-a.akamaihd.net/content/sintel/hls/playlist.m3u8", category: "Фильмы", logo_url: "icon.png" },
      { id: 10, index: 10, title: "Tears of Steel HLS", stream_url: "https://test-streams.mux.dev/test_001/stream.m3u8", category: "Тест", logo_url: "icon.png" }
    ];
  }

  window.loadSilkyOpenChannels = function() {
    if (bootWatchdog) { clearTimeout(bootWatchdog); bootWatchdog = null; }
    var statusEl = document.getElementById("splashStatus");
    var spinnerEl = document.getElementById("splashSpinner");
    var errEl = document.getElementById("splashErr");
    if (statusEl) statusEl.textContent = "Загрузка каналов SilkyOpenChannel…";
    if (spinnerEl) spinnerEl.style.display = "block";
    if (errEl) errEl.style.display = "none";

    var badge = document.getElementById("topPortalBadge");
    if (badge) badge.textContent = "SilkyOpenChannel";

    if (typeof XMLHttpRequest === "undefined") {
      handleAuthSuccess({ channels: getSilkyOpenChannelsList(), session_id: "silky_open" });
      return;
    }

    var m3uUrl = (currentPortal && currentPortal.indexOf(".m3u") !== -1) ? currentPortal : "https://iptv-org.github.io/iptv/index.m3u8";
    var xhr = new XMLHttpRequest();
    xhr.open("GET", m3uUrl, true);
    xhr.timeout = 3500;
    xhr.onreadystatechange = function() {
      if (xhr.readyState === 4) {
        if (xhr.status >= 200 && xhr.status < 400 && xhr.responseText && xhr.responseText.indexOf("#EXTINF") !== -1) {
          var parsed = parseM3uChannels(xhr.responseText);
          if (parsed && parsed.length > 0) {
            handleAuthSuccess({ channels: parsed, session_id: "silky_open" });
            return;
          }
        }
        handleAuthSuccess({ channels: getSilkyOpenChannelsList(), session_id: "silky_open" });
      }
    };
    xhr.ontimeout = function() {
      handleAuthSuccess({ channels: getSilkyOpenChannelsList(), session_id: "silky_open" });
    };
    xhr.onerror = function() {
      handleAuthSuccess({ channels: getSilkyOpenChannelsList(), session_id: "silky_open" });
    };
    try {
      xhr.send();
    } catch(e) {
      handleAuthSuccess({ channels: getSilkyOpenChannelsList(), session_id: "silky_open" });
    }
  };

  function handleAuthSuccess(res) {
    if (bootWatchdog) { clearTimeout(bootWatchdog); bootWatchdog = null; }
    if (res.session_id) {
      sessionId = res.session_id;
      localStorage.setItem("alem_sid", sessionId);
    }
    // authorize_terminal also returns macaddr (Swift fixture): persist it as
    // a fallback so the terminal stays bound even if login/register_terminal
    // did not return the MAC. Without it, every relaunch would ask for
    // login/password again.
    if (res.macaddr && !getStoredMac()) {
      currentMac = res.macaddr;
      localStorage.setItem("alem_mac", currentMac);
    }
    if (res.now) {
      var sNow = parsePortalDate(res.now);
      if (sNow) serverTimeOffset = sNow.getTime() - Date.now();
    }
    authData.tokens = res.media_tokens || res.tokens || {};

    var channels = [];
    if (res.medialist && Array.isArray(res.medialist)) {
      channels = parseMedialistTable(res.medialist);
    } else if (res.channels_csv) {
      channels = parseCsvChannels(res.channels_csv);
    } else if (Array.isArray(res.channels)) {
      channels = res.channels;
    } else if (Array.isArray(res.media)) {
      channels = res.media;
    }

    if (res.medialist_enabled && typeof res.medialist_enabled === "object") {
      channels = channels.filter(function(ch) {
        return res.medialist_enabled[String(ch.id)] !== undefined;
      });
    }
    channels.sort(function(a, b) {
      return (a.index || 0) - (b.index || 0);
    });

    if (Array.isArray(res.playlists) && channels.length > 0) {
      authData.playlists = res.playlists;
      var chMap = {};
      channels.forEach(function(c) { chMap[c.id] = c; });
      res.playlists.forEach(function(pl) {
        if (Array.isArray(pl.playitems) && pl.name !== "tv" && pl.name !== "tv-favorites") {
          var catName = pl.title || pl.name;
          pl.playitems.forEach(function(mid) {
            if (chMap[mid] && (chMap[mid].category === "Общие" || !chMap[mid].category)) {
              chMap[mid].category = catName;
            }
          });
        }
      });
    }

    var tsServices = (res.services || []).filter(function(s){ return s.name === "timeshift"; }).map(function(s){ return s.id; });
    var srvEnabled = res.services_enabled || {};
    authData.timeshiftEnabled = tsServices.some(function(id){ return srvEnabled[String(id)] !== undefined; });

    if (!channels || channels.length === 0) {
      // Built-in emergency channel list
      channels = [
        { id: 1, title: "Altyn Asyr", stream_url: "http://telecom.alemtv.tm/hls/altynasyr/index.m3u8", category: "Туркменистан", index: 1 },
        { id: 2, title: "Yashlyk", stream_url: "http://telecom.alemtv.tm/hls/yashlyk/index.m3u8", category: "Туркменистан", index: 2 },
        { id: 3, title: "Miras", stream_url: "http://telecom.alemtv.tm/hls/miras/index.m3u8", category: "Туркменистан", index: 3 },
        { id: 4, title: "Turkmenistan", stream_url: "http://telecom.alemtv.tm/hls/turkmenistan/index.m3u8", category: "Туркменистан", index: 4 },
        { id: 5, title: "Asgabat", stream_url: "http://telecom.alemtv.tm/hls/asgabat/index.m3u8", category: "Туркменистан", index: 5 },
        { id: 6, title: "Turkmen Owazy", stream_url: "http://telecom.alemtv.tm/hls/turkmenowazy/index.m3u8", category: "Музыка", index: 6 },
        { id: 7, title: "Mux HD Video Test", stream_url: "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8", category: "SilkyOpenChannel", index: 7 },
        { id: 8, title: "NASA TV HD", stream_url: "https://ntv1.akamaized.net/hls/live/2014075/NASA-NTV1-HLS/master.m3u8", category: "SilkyOpenChannel", index: 8 },
        { id: 9, title: "Red Bull TV Live", stream_url: "https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8", category: "SilkyOpenChannel", index: 9 },
        { id: 10, title: "Euronews Russian", stream_url: "https://euronews-euronews-world-1-ru.samsung.wurl.tv/playlist.m3u8", category: "SilkyOpenChannel", index: 10 },
        { id: 11, title: "Turkmenistan Sport", stream_url: "http://telecom.alemtv.tm/hls/sport/index.m3u8", category: "Спорт", index: 11 },
        { id: 12, title: "Arkadag", stream_url: "http://telecom.alemtv.tm/hls/arkadag/index.m3u8", category: "Туркменистан", index: 12 }
      ];
    }

    authData.channels = channels;
    renderCategories();
    renderChannels();
    loadEpgNow();
    showScreen("main");

    if (updateInterval) clearInterval(updateInterval);
    updateInterval = setInterval(loadEpgNow, 120000);
  }

  function renderCategories() {
    var menu = document.getElementById("categoryMenu");
    if (!menu) return;
    menu.innerHTML = "";
    var cats = [
      { id: "all", name: "Все каналы", icon: "☰" },
      { id: "fav", name: "Избранное", icon: "★" }
    ];
    var known = { "all": true, "fav": true };
    authData.channels.forEach(function(ch) {
      var c = ch.category || "Общие";
      if (!known[c]) {
        known[c] = true;
        var icon = "◈";
        if (/спорт/i.test(c)) icon = "⚽";
        else if (/фильм|кино/i.test(c)) icon = "🎬";
        else if (/детск/i.test(c)) icon = "🧸";
        else if (/музык/i.test(c)) icon = "♫";
        else if (/познават|наука/i.test(c)) icon = "🔬";
        else if (/новост/i.test(c)) icon = "📰";
        cats.push({ id: c, name: c, icon: icon });
      }
    });
    cats.forEach(function(cat){
      var btn = document.createElement("button");
      btn.className = "nav-item focusable" + (cat.id === currentCategory ? " active" : "");
      btn.dataset.cat = cat.id;
      btn.innerHTML = '<span class="icon">' + cat.icon + '</span><span>' + cat.name + '</span>';
      btn.onclick = function(){
        currentCategory = cat.id;
        document.querySelectorAll(".nav-item").forEach(function(b){ b.classList.remove("active"); });
        btn.classList.add("active");
        var titleEl = document.getElementById("contentTitle");
        if (titleEl) titleEl.textContent = cat.name;
        renderChannels();
      };
      menu.appendChild(btn);
    });
  }

  function getFavorites() {
    try { return JSON.parse(localStorage.getItem("alem_favs") || "[]"); } catch(e){ return []; }
  }

  function toggleFavorite(id) {
    var favs = getFavorites();
    var idx = favs.indexOf(id);
    if (idx !== -1) favs.splice(idx, 1);
    else favs.push(id);
    localStorage.setItem("alem_favs", JSON.stringify(favs));
    renderChannels();
  }

  function renderChannels() {
    var grid = document.getElementById("channelGrid");
    if (!grid) return;
    grid.innerHTML = "";
    var favs = getFavorites();

    var list = authData.channels.filter(function(ch){
      if (currentCategory === "all") return true;
      if (currentCategory === "fav") return favs.indexOf(ch.id) !== -1;
      return (ch.category || "").toLowerCase().indexOf(currentCategory.toLowerCase()) !== -1;
    });

    if (list.length === 0) {
      grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:80px;font-size:24px;color:#64748b;">Каналов в этой категории пока нет.</div>';
      return;
    }

    list.forEach(function(ch, idx){
      var isFav = favs.indexOf(ch.id) !== -1;
      var card = document.createElement("div");
      card.className = "channel-card focusable";
      card.tabIndex = 0;
      card.dataset.channelId = ch.id;

      var logoSrc = ch.logo_url || "icon.png";
      var epgInfo = epgNow[ch.id];
      var progTitle = (epgInfo && epgInfo.title) || "Прямой эфир";
      var timeStr = (epgInfo && epgInfo.time) || "";

      card.innerHTML =
        '<div class="card-top">' +
          '<img class="channel-logo" src="' + logoSrc + '" onerror="this.onerror=null;this.src=\'icon.png\'" alt="">' +
          '<div style="overflow:hidden;">' +
            '<div class="channel-num">КАНАЛ ' + (ch.index || (idx + 1)) + '</div>' +
            '<div class="channel-name">' + (ch.title || ch.name || "Канал") + '</div>' +
          '</div>' +
          '<span class="card-fav' + (isFav ? ' active' : '') + '">★</span>' +
        '</div>' +
        '<div class="program-title">' + progTitle + '</div>' +
        '<div class="progress-bar-box"><div class="progress-fill" style="width: ' + epgProgressPct(epgInfo) + '%;"></div></div>' +
        '<div class="card-foot">' +
          '<span>' + timeStr + '</span>' +
          '<span>HD</span>' +
        '</div>';

      card.onclick = function(){ playChannel(ch); };
      card.onkeydown = function(ke){
        if (ke.keyCode === 13 || ke.keyCode === 65385 || ke.key === "Enter" || ke.key === "Select") {
          ke.preventDefault();
          playChannel(ch);
        }
      };
      grid.appendChild(card);
    });
    updateFocus();
  }

  // EPGStore.nowPlaying (Swift): schedules for today (then yesterday for a
  // program crossing midnight) from get_epg result_as:"csv"; the response is a
  // CSV table (header row + data rows), not a plain CSV dump. Missing channel in
  // the response means "no program now" — kept as-is (not "Прямой эфир").
  function programFromRow(row, fallbackId) {
    var mid = parseInt(row.media_id, 10);
    if (!mid) mid = parseInt(row.id, 10) || fallbackId || 0;
    var start = parsePortalDate(row.start);
    var stop = parsePortalDate(row.stop);
    if (!start || !stop) return null;
    var title = (row.title || "").replace(/\(\d+\+\)$/, "").trim();
    return {
      id: mid,
      media_id: mid,
      title: title,
      description: row.description || "",
      start: start.getTime(),
      stop: stop.getTime()
    };
  }

  // All programs for one media_id/day from a get_epg response (any shape).
  function programsFromResp(res, mediaId) {
    var out = [];
    if (!res) return out;
    var rows = [];
    if (Array.isArray(res)) rows = parseCsvTable(res);
    else if (typeof res === "string") {
      // Plain CSV dump fallback: media_id,title,start,stop,...
      res.split("\n").forEach(function(l){
        var p = l.split(",");
        if (p.length >= 3) rows.push({ media_id: p[0], title: p[1], start: p[2], stop: p[3] });
      });
    } else if (Array.isArray(res.media)) {
      rows = parseCsvTable([Object.keys(res.media[0] || {}), res.media[0]].concat(res.media));
    }
    rows.forEach(function(r){
      var prog = programFromRow(r, mediaId);
      if (prog) out.push(prog);
    });
    return out;
  }

  function loadEpgNow() {
    var ids = authData.channels.map(function(c){ return c.id; });
    if (ids.length === 0) return;
    var nowMs = getServerNow();
    var today = portalDayString(nowMs);
    var yesterday = portalDayString(nowMs - 86400000);

    // Day schedules for all channels in chunks of 50 (EPGStore.chunkSize).
    var done = { count: 0 };
    var chunkSize = 50;
    function finishChunk(err) {
      done.count++;
      if (done.count === Math.ceil(ids.length / chunkSize)) {
        renderChannels();
        scheduleEpgRefresh(nowMs);
      }
    }
    for (var start = 0; start < ids.length; start += chunkSize) {
      (function(chunk) {
        jsonRpc("get_epg", { version: 2, result_as: "csv", media_id: chunk, start: today + " 00:00:00", stop: today + " 23:59:59" }, false, function(err, res){
          if (!err && res) {
            parseCsvTable(Array.isArray(res) ? res : []).forEach(function(row){
              var prog = programFromRow(row);
              if (prog && prog.start <= nowMs && nowMs < prog.stop) {
                epgNow[prog.media_id] = { title: prog.title, start: prog.start, stop: prog.stop, description: prog.description };
              }
            });
          }
          finishChunk(err);
        });
      })(ids.slice(start, start + chunkSize));
    }

    // Channels with no program in today's bulk response: try the bulk "now"
    // query shape, then yesterday for overnight programs (EPGStore.lookup).
    var missing = ids.filter(function(id){ return !epgNow[id]; });
    if (missing.length > 0) {
      jsonRpc("get_epg", { version: 2, result_as: "csv", media_id: missing }, false, function(err, res){
        if (!err && res) {
          var table = Array.isArray(res) ? res : null;
          if (table) {
            parseCsvTable(table).forEach(function(row){
              var prog = programFromRow(row);
              if (prog && prog.start <= nowMs && nowMs < prog.stop) {
                epgNow[prog.media_id] = { title: prog.title, start: prog.start, stop: prog.stop, description: prog.description };
              }
            });
          }
        }
        missing = missing.filter(function(id){ return !epgNow[id]; });
        if (missing.length > 0) fetchEpgDay(missing, yesterday, nowMs);
      });
    } else {
      scheduleEpgRefresh(nowMs);
    }
  }

  // Progress within the currently airing program (0..100), for channel cards.
  function epgProgressPct(info) {
    if (!info || !info.start || !info.stop) return 0;
    var nowMs = getServerNow();
    if (nowMs < info.start || nowMs >= info.stop) return 0;
    return Math.min(100, Math.max(0, 100 * (nowMs - info.start) / Math.max(1, info.stop - info.start)));
  }

  function fetchEpgDay(ids, day, nowMs) {
    var chunkSize = 50;
    var done = { count: 0, total: Math.ceil(ids.length / chunkSize) };
    function tick() {
      done.count++;
      if (done.count >= done.total) renderChannels();
    }
    for (var start = 0; start < ids.length; start += chunkSize) {
      (function(chunk) {
        jsonRpc("get_epg", { version: 2, result_as: "csv", media_id: chunk, start: day + " 00:00:00", stop: day + " 23:59:59" }, false, function(err, res){
          if (!err && res && Array.isArray(res)) {
            parseCsvTable(res).forEach(function(row){
              var prog = programFromRow(row);
              if (prog && prog.start <= nowMs && nowMs < prog.stop) {
                epgNow[prog.media_id] = { title: prog.title, start: prog.start, stop: prog.stop, description: prog.description };
              }
            });
          }
          tick();
        });
      })(ids.slice(start, start + chunkSize));
    }
  }

  // NowPlayingRefresh: re-check right after the earliest shown program ends,
  // but at least every minute.
  function scheduleEpgRefresh(nowMs) {
    if (updateInterval) clearInterval(updateInterval);
    var delays = [60000];
    Object.keys(epgNow).forEach(function(k){
      var p = epgNow[k];
      if (p && p.stop) {
        var d = (p.stop - nowMs + 2000) / 1000;
        if (d > 5 && d < 60) delays.push(d * 1000);
      }
    });
    var next = Math.min(Math.max.apply(null, delays));
    updateInterval = setInterval(loadEpgNow, next);
  }

  // Channel zapping (Swift PlayerModel.previousChannel/nextChannel).
  function channelIndex(ch) {
    var list = authData.channels || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === ch.id) return i;
    }
    return -1;
  }

  function previousChannel() {
    if (!currentPlayingChannel) {
      if (authData.channels && authData.channels.length) playChannel(authData.channels[0]);
      return;
    }
    var idx = channelIndex(currentPlayingChannel);
    if (idx > 0) playChannel(authData.channels[idx - 1]);
  }

  function nextChannel() {
    if (!currentPlayingChannel) {
      if (authData.channels && authData.channels.length) playChannel(authData.channels[0]);
      return;
    }
    var idx = channelIndex(currentPlayingChannel);
    if (idx >= 0 && idx < authData.channels.length - 1) playChannel(authData.channels[idx + 1]);
  }
  window.previousChannel = previousChannel;
  window.nextChannel = nextChannel;

  // Swift PlayerAction.showChannelList — quick zap drawer over the player.
  function renderZapList() {
    var list = document.getElementById("zapListContainer");
    if (!list) return;
    list.innerHTML = "";
    (authData.channels || []).forEach(function(ch, idx) {
      var isFav = getFavorites().indexOf(ch.id) !== -1;
      var card = document.createElement("div");
      card.className = "channel-card focusable";
      card.tabIndex = 0;
      card.dataset.channelId = ch.id;
      var epgInfo = epgNow[ch.id];
      card.innerHTML =
        '<div class="card-top">' +
          '<img class="channel-logo" src="' + (ch.logo_url || "icon.png") + '" alt="">' +
          '<div style="overflow:hidden;">' +
            '<div class="channel-num">КАНАЛ ' + (ch.index || (idx + 1)) + '</div>' +
            '<div class="channel-name">' + (ch.title || ch.name || "Канал") + '</div>' +
          '</div>' +
          '<span class="card-fav' + (isFav ? ' active' : '') + '">★</span>' +
        '</div>' +
        '<div class="program-title">' + ((epgInfo && epgInfo.title) || "Прямой эфир") + '</div>';
      card.onclick = function(){ playChannel(ch); closeChannelList(); };
      card.onkeydown = function(ke){
        if (ke.keyCode === 13 || ke.keyCode === 65385 || ke.key === "Enter" || ke.key === "Select") {
          ke.preventDefault();
          playChannel(ch);
          closeChannelList();
        }
      };
      list.appendChild(card);
    });
  }

  function showChannelList() {
    if (currentScreen !== "player") return;
    renderZapList();
    var drawer = document.getElementById("zapDrawer");
    if (drawer) {
      drawer.style.display = "block";
      drawer.className = "zap-drawer active";
    }
    currentFocusIndex = 0;
    updateFocus();
    hideOsd();
  }
  window.showChannelList = showChannelList;

  function closeChannelList() {
    var drawer = document.getElementById("zapDrawer");
    if (drawer) {
      drawer.style.display = "none";
      drawer.className = "zap-drawer";
    }
    if (currentScreen === "player") updateFocus();
  }
  window.closeChannelList = closeChannelList;

  // Player Functions
  // load(mode): Swift PlayerModel.load — live, timeshift(behind), archive(program).
  // For timeshift/archive the URL itself encodes the position: rewinding or
  // fast-forwarding the live stream reloads a shifted timeshift URL.
  function loadStream(mode) {
    if (!currentPlayingChannel || !videoElement) return;
    var ch = currentPlayingChannel;
    var nowMs = getServerNow();
    var stream;
    if (mode === "archive" && archiveProgram) {
      stream = resolveStreamUrl(ch, ch.timeshift_auth || ch.auth, "archive", archiveProgram);
      contentStartMs = archiveProgram.start;
    } else if (mode === "timeshift") {
      stream = resolveStreamUrl(ch, ch.timeshift_auth || ch.auth, "timeshift", { behind: timeshiftBehind });
      contentStartMs = nowMs - timeshiftBehind * 1000;
    } else {
      stream = resolveStreamUrl(ch, ch.auth, "live");
      if (!stream) stream = ch.stream_url || ch.url || ch.path || "";
      contentStartMs = nowMs;
    }
    if (!stream) {
      window.showPlayerError("Поток для канала недоступен.");
      return;
    }
    try {
      videoElement.pause();
      videoElement.src = stream;
      videoElement.load();
      var p = videoElement.play();
      if (p && typeof p.catch === "function") {
        p.catch(function(e){ console.warn("Autoplay notice:", e); });
      }
    } catch(ve) {
      console.warn("Playback error:", ve);
    }
    updateOsdTimeline();
  }

  function playChannel(channel) {
    if (!channel) return;
    currentPlayingChannel = channel;
    playerMode = "live";
    timeshiftBehind = 0;
    archiveProgram = null;
    pausedAtMs = 0;
    showScreen("player");
    window.hidePlayerError();

    var titleEl = document.getElementById("osdChTitle");
    if (titleEl) titleEl.textContent = (channel.index ? (channel.index + ". ") : "") + (channel.title || channel.name || "Канал");
    var progEl = document.getElementById("osdProgTitle");
    if (progEl) progEl.textContent = (epgNow[channel.id] && epgNow[channel.id].title) || "Прямой эфир";

    loadStream("live");
    showOsd();
  }
  window.playChannel = playChannel;

  // Play an archived program from the EPG (Swift ScheduleView.select →
  // player.play(program, on: channel)).
  window.playArchiveProgram = function(program) {
    if (!currentPlayingChannel) return;
    if (!program || !program.start || !program.stop) return;
    archiveProgram = { start: program.start, stop: program.stop, title: program.title };
    playerMode = "archive";
    timeshiftBehind = 0;
    var progEl = document.getElementById("osdProgTitle");
    if (progEl) progEl.textContent = archiveProgram.title + " (запись)";
    loadStream("archive");
    showOsd();
  };

  // Player state accessor (debugging/testing; mirrors Swift PlayerModel state).
  window.getPlayerState = function() {
    return {
      mode: playerMode,
      timeshiftBehind: timeshiftBehind,
      archive: archiveProgram ? { start: archiveProgram.start, stop: archiveProgram.stop, title: archiveProgram.title } : null,
      contentStartMs: contentStartMs,
      channel: currentPlayingChannel ? {
        id: currentPlayingChannel.id,
        index: currentPlayingChannel.index,
        title: currentPlayingChannel.title
      } : null,
      channelPosition: currentPlayingChannel ? channelIndex(currentPlayingChannel) : -1
    };
  };

  // Remote overlay accessor (Swift PlayerOverlay) for testing.
  window.currentOverlay = currentOverlay;
  window.getCurrentScreen = function() { return currentScreen; };
  window.getFocusableElements = getFocusableElements;
  window.getCurrentFocusIndex = function() { return currentFocusIndex; };

  window.showPlayerError = function(msg) {
    var el = document.getElementById("playerError");
    if (el) {
      el.textContent = msg || "Ошибка воспроизведения.";
      el.style.display = "flex";
    }
  };

  window.hidePlayerError = function() {
    var el = document.getElementById("playerError");
    if (el) el.style.display = "none";
  };

  function hideOsd() {
    osdHidden = true;
    if (playerOsd) playerOsd.classList.add("hidden");
    if (osdTimer) { clearTimeout(osdTimer); osdTimer = null; }
  }
  window.hideOsd = hideOsd;

  function showOsd() {
    if (!playerOsd) return;
    osdHidden = false;
    playerOsd.classList.remove("hidden");
    if (osdTimer) clearTimeout(osdTimer);
    osdTimer = setTimeout(function(){
      if (currentScreen === "player") hideOsd();
    }, 5000);
  }

  // Swift PlayerModel.pause/resume: remember the server time of the pause;
  // resume reloads from the same position (archive → same URL, timeshift → same behind).
  window.togglePlayPause = function() {
    if (!videoElement) return;
    var btn = document.getElementById("btnOsdPlay");
    if (videoElement.paused) {
      if (playerMode === "archive" || playerMode === "timeshift") {
        pausedAtMs = 0; // stream is still bound to contentStartMs; resume it
      } else {
        pausedAtMs = getServerNow();
      }
      videoElement.play();
      if (btn) btn.textContent = "Пауза";
    } else {
      if (playerMode === "live") {
        pausedAtMs = getServerNow();
      }
      videoElement.pause();
      if (btn) btn.textContent = "Воспр.";
    }
    showOsd();
  };

  // Swift PlaybackPolicy.skip: backwards — always timeshift (not deeper than the
  // archive); forwards — live again once within liveSnap of the live edge.
  window.seekRelative = function(sec) {
    if (!currentPlayingChannel || !videoElement) return;
    var nowMs = getServerNow();
    if (playerMode === "archive") {
      var dur = (archiveProgram.stop - archiveProgram.start) / 1000;
      var pos = (nowMs - contentStartMs) / 1000;
      pos = Math.min(Math.max(0, pos + sec), Math.max(0, dur - 1));
      var t = videoElement.currentTime;
      if (t !== undefined && t !== null && !isNaN(t)) {
        var maxT = videoElement.duration || 0;
        var target = t + sec;
        if (maxT && target >= 0 && target <= maxT) {
          videoElement.currentTime = target;
          showOsd();
          return;
        }
      }
      archiveProgram.start = nowMs - pos * 1000;
      archiveProgram.stop = archiveProgram.start + dur * 1000;
      loadStream("archive");
      showOsd();
      return;
    }
    var contentMs = (playerMode === "timeshift") ? contentStartMs : (pausedAtMs || nowMs);
    var behind = nowMs - (contentMs + sec * 1000);
    var depth = currentPlayingChannel.timeshift_archive_length || 0;
    if (depth) behind = Math.min(behind, depth * 1000);
    if (sec > 0 && behind <= 45000) {
      // Close enough to the live edge → back to live.
      playerMode = "live";
      archiveProgram = null;
      loadStream("live");
      showOsd();
      return;
    }
    playerMode = "timeshift";
    archiveProgram = null;
    timeshiftBehind = Math.max(10, Math.round(behind / 1000));
    loadStream("timeshift");
    showOsd();
  };

  // Back to the live edge (green key / «Эфир»).
  window.jumpToLive = function() {
    if (!currentPlayingChannel || !videoElement) return;
    playerMode = "live";
    timeshiftBehind = 0;
    archiveProgram = null;
    loadStream("live");
    showOsd();
  };

  window.cycleAspectRatio = function() {
    if (!videoElement) return;
    var cur = videoElement.style.objectFit || "contain";
    videoElement.style.objectFit = (cur === "contain" ? "fill" : (cur === "fill" ? "cover" : "contain"));
    showOsd();
  };

  window.backToChannelList = function() {
    if (videoElement) {
      videoElement.pause();
      videoElement.src = "";
    }
    showScreen("main");
  };

  // EPG Modal
  window.openCurrentEpg = function() {
    var modal = document.getElementById("epgModal");
    if (modal) {
      modal.className = "modal-overlay active";
      modal.style.display = "flex";
      renderEpgModal();
      updateFocus();
    }
  };

  window.closeEpgModal = function() {
    var modal = document.getElementById("epgModal");
    if (modal) {
      modal.className = "modal-overlay";
      modal.style.display = "none";
      updateFocus();
    }
  };

  // OSD timeline: live → full "LIVE" fill; timeshift → position by behind;
  // archive → position/duration within the recorded program.
  function updateOsdTimeline() {
    if (!currentPlayingChannel) return;
    var nowMs = getServerNow();
    var cur = document.getElementById("osdCurrentTime");
    var fill = document.getElementById("osdProgressFill");
    if (playerMode === "archive" && archiveProgram) {
      var pos = Math.min(Math.max(0, (nowMs - contentStartMs) / 1000), (archiveProgram.stop - archiveProgram.start) / 1000);
      var pct = 100 * pos / Math.max(1, (archiveProgram.stop - archiveProgram.start) / 1000);
      if (cur) cur.textContent = fmtHms(pos);
      if (fill) fill.style.width = pct + "%";
      return;
    }
    var behind = playerMode === "timeshift" ? timeshiftBehind : 0;
    if (cur) cur.textContent = behind > 0 ? ("-" + fmtHms(behind)) : "LIVE";
    if (fill) fill.style.width = (behind > 0 ? Math.max(2, 100 - behind / 86400 * 100) : 100) + "%";
  }

  function fmtHms(sec) {
    sec = Math.max(0, Math.round(sec));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    return (h > 0 ? h + ":" : "") + pad2(m) + ":" + pad2(s);
  }

  // ScheduleView (Swift): day panel (back = archive depth, forward = available
  // EPG days) + program list; already-archived programs are clickable and
  // start playback, the current program is marked "LIVE".
  function renderEpgModal() {
    var titleEl = document.getElementById("epgModalTitle");
    var list = document.getElementById("epgListContainer");
    var nav = document.getElementById("epgDayNav");
    if (!list) return;
    list.innerHTML = "";
    var ch = currentPlayingChannel || authData.channels[0];
    if (titleEl) titleEl.textContent = "Программа передач — " + ((ch && ch.title) || "");
    if (!ch) return;

    // Day navigation (ScheduleView.days).
    var nowMs = getServerNow();
    var today = portalDayString(nowMs);
    var backDays = hasArchive(ch) ? Math.ceil((ch.timeshift_archive_length || 0) / 86400) : 0;
    if (nav) {
      nav.innerHTML = "";
      for (var d = -backDays; d <= 7; d++) {
        var dayVal = portalDayString(nowMs + d * 86400000);
        var btn = document.createElement("button");
        btn.className = "btn focusable" + (dayVal === portalDayString(nowMs + epgDayOffset * 86400000) ? " btn-primary" : "");
        btn.textContent = (d === 0 ? "Сегодня" : (d === -1 ? "Вчера" : ""));
        btn.dataset.day = dayVal;
        btn.onclick = function(ev){
          epgDayOffset = portalDayString(nowMs) === this.dataset.day ? 0 : (parseInt(this.dataset.day.slice(8, 10), 10) - parseInt(portalDayString(nowMs).slice(8, 10), 10));
          renderEpgModal();
        };
        nav.appendChild(btn);
      }
      updateFocus();
    }

    var dayStr = portalDayString(nowMs + epgDayOffset * 86400000);
    list.innerHTML = '<div style="padding:24px;text-align:center;color:#64748b;">Загрузка программы…</div>';

    jsonRpc("get_epg", { version: 2, result_as: "csv", media_id: ch.id, start: dayStr + " 00:00:00", stop: dayStr + " 23:59:59" }, false, function(err, res){
      if (err || !res) {
        list.innerHTML = '<div style="padding:40px;text-align:center;color:#64748b;">Программа передач недоступна для этого канала.</div>';
        return;
      }
      var programs = programsFromResp(res, ch.id);
      programs.sort(function(a, b){ return a.start - b.start; });
      if (programs.length === 0) {
        list.innerHTML = '<div style="padding:40px;text-align:center;color:#64748b;">На этот день передач не найдено.</div>';
        return;
      }
      programs.forEach(function(p){
        var isOnAir = p.start <= nowMs && nowMs < p.stop;
        var inArch = isInArchive(ch, p.start, nowMs);
        var item = document.createElement("div");
        item.className = "epg-item focusable";
        item.tabIndex = 0;
        var progMs = nowMs - p.start;
        var durMs = p.stop - p.start;
        var pct = isOnAir ? Math.min(100, Math.max(0, 100 * progMs / Math.max(1, durMs))) : 0;
        var badge = isOnAir ? '<span class="epg-badge-live">LIVE</span>'
          : (inArch ? '<span class="epg-badge-rec">Запись</span>' : '');
        var clickable = isOnAir || inArch;
        if (clickable) {
          item.onclick = function(){
            if (isOnAir) { playChannel(ch); }
            else { playArchiveProgram(p); }
          };
          item.onkeydown = function(ke){
            if (ke.keyCode === 13 || ke.keyCode === 65385 || ke.key === "Enter" || ke.key === "Select") {
              ke.preventDefault();
              if (isOnAir) { playChannel(ch); } else { playArchiveProgram(p); }
            }
          };
        }
        item.innerHTML =
          '<div class="epg-time"></div>' +
          '<div style="flex:1;">' +
            '<div class="epg-prog-name">' + (p.title || "Передача") + badge + '</div>' +
            (p.description ? '<div class="epg-desc">' + p.description + '</div>' : '') +
            '<div class="progress-bar-box"><div class="progress-fill" style="width: ' + pct + '%;"></div></div>' +
          '</div>';
        // Wall-clock start/stop in server timezone (UTC+5).
        var sD = new Date(p.start + 5 * 3600 * 1000);
        var eD = new Date(p.stop + 5 * 3600 * 1000);
        item.querySelector(".epg-time").textContent =
          pad2(sD.getUTCHours()) + ":" + pad2(sD.getUTCMinutes()) + " – " + pad2(eD.getUTCHours()) + ":" + pad2(eD.getUTCMinutes());
        list.appendChild(item);
      });
      updateFocus();
    });
  }

  // Settings Screen
  window.showSettings = function() {
    var modal = document.getElementById("settingsModal");
    if (modal) {
      modal.className = "modal-overlay active";
      modal.style.display = "flex";
      populateSettings();
      updateFocus();
    }
  };

  window.closeSettings = function() {
    var modal = document.getElementById("settingsModal");
    if (modal) {
      modal.className = "modal-overlay";
      modal.style.display = "none";
      updateFocus();
    }
  };

  function populateSettings() {
    var sel = document.getElementById("portalSelector");
    if (sel) {
      sel.innerHTML = "";
      var foundMatch = false;
      PORTAL_LIST.forEach(function(p){
        var opt = document.createElement("option");
        opt.value = p.url;
        opt.textContent = p.name;
        if (p.url === currentPortal) {
          opt.selected = true;
          foundMatch = true;
        }
        sel.appendChild(opt);
      });
      if (!foundMatch && currentPortal) {
        var optCustom = document.createElement("option");
        optCustom.value = currentPortal;
        optCustom.textContent = "Пользовательский (" + currentPortal + ")";
        optCustom.selected = true;
        sel.appendChild(optCustom);
      }
    }
    var customInp = document.getElementById("customPortalInput");
    if (customInp) customInp.value = currentPortal || "";

    var macInp = document.getElementById("customMacInput");
    if (macInp) macInp.value = getStoredMac() || "";

    var macVal = document.getElementById("settingsMacVal");
    var sidVal = document.getElementById("settingsSessionVal");
    var countVal = document.getElementById("settingsChCount");
    if (macVal) macVal.textContent = getStoredMac() || "—";
    if (sidVal) sidVal.textContent = sessionId ? (sessionId.substr(0, 16) + "…") : "Не авторизован";
    if (countVal) countVal.textContent = authData.channels.length + " каналов";
  }

  window.applyCustomMac = function() {
    var inp = document.getElementById("customMacInput");
    if (!inp || !inp.value.trim()) return;
    var mac = inp.value.trim().toUpperCase();
    currentMac = mac;
    localStorage.setItem("alem_mac", mac);
    localStorage.removeItem("alem_sid");
    sessionId = null;
    closeSettings();
    showScreen("splash");
    bootAlemTV();
  };

  window.showLoginModal = function() {
    closeSettings();
    var m = document.getElementById("loginModal");
    if (m) {
      m.className = "modal-overlay active";
      m.style.display = "flex";
      var err = document.getElementById("loginErrMsg");
      if (err) err.textContent = "";
      updateFocus();
    }
  };

  window.closeLoginModal = function() {
    var m = document.getElementById("loginModal");
    if (m) {
      m.className = "modal-overlay";
      m.style.display = "none";
      updateFocus();
    }
  };

  window.submitSubscriberLogin = function() {
    var uEl = document.getElementById("loginUsername");
    var pEl = document.getElementById("loginPassword");
    var errMsg = document.getElementById("loginErrMsg");
    var u = (uEl ? uEl.value : "").trim();
    var p = (pEl ? pEl.value : "").trim();
    if (!u) {
      if (errMsg) errMsg.textContent = "Введите номер договора или логин.";
      return;
    }
    if (!p) {
      if (errMsg) errMsg.textContent = "Введите пароль абонента.";
      return;
    }
    if (errMsg) errMsg.textContent = "Регистрация терминала в Alem TV...";
    // Swift SessionStore.signIn: macaddr:null + create_macaddr:true when no
    // bound terminal yet; the server assigns the MAC. Encrypted call.
    var storedMac = getStoredMac();
    jsonRpc("register_terminal", {
      username: u,
      password: p,
      macaddr: storedMac,
      create_macaddr: !storedMac,
      require_session: true,
      require_user_id: true,
      terminal_type: "smarttv",
      firmware_version: "2.0 (SmartTV)"
    }, true, function(err, res) {
      if (err || res === false || !res || !res.session_id) {
        if (errMsg) errMsg.textContent = friendlyLoginError(err);
        return;
      }
      sessionId = res.session_id;
      localStorage.setItem("alem_sid", sessionId);
      if (res.macaddr) {
        currentMac = res.macaddr;
        localStorage.setItem("alem_mac", currentMac);
      }
      closeLoginModal();
      showScreen("splash");
      // Swift signIn: authorize directly with the new session (no 2nd login).
      var statusEl = document.getElementById("splashStatus");
      if (statusEl) statusEl.textContent = "Загрузка каналов Alem TV…";
      jsonRpc("authorize_terminal", {
        version: 2,
        require_media_tokens: true,
        require_media_keys: false,
        result_as: "csv"
      }, false, function(aErr, aRes) {
        if (!aErr && aRes && (aRes.medialist || aRes.channels || aRes.channels_csv)) {
          handleAuthSuccess(aRes);
          return;
        }
        if (isRequiresSignIn(aErr)) {
          sessionId = null;
          localStorage.removeItem("alem_sid");
          showBootError("Терминал зарегистрирован, но сессия не авторизована. Попробуйте войти ещё раз.");
          showLoginModal();
          return;
        }
        var detail = (aErr && aErr.message) || "Сервер не вернул список каналов";
        showBootError("Сервер " + currentPortal.replace(/^https?:\/\//, "") + ": " + detail);
        showLoginModal();
      });
    });
  };

  window.applyCustomPortal = function() {
    var inp = document.getElementById("customPortalInput");
    if (!inp || !inp.value.trim()) return;
    var u = inp.value.trim();
    if (!/^https?:\/\//i.test(u)) u = "http://" + u;
    setCurrentPortal(u);
    reauthPortal();
  };

  window.onPortalSelected = function(url) {
    setCurrentPortal(url);
  };

  window.reauthPortal = function() {
    localStorage.removeItem("alem_sid");
    sessionId = null;
    closeSettings();
    showScreen("splash");
    bootAlemTV();
  };

  window.resetAlemCache = function() {
    localStorage.clear();
    location.reload();
  };

  window.launchOfficialPortal = function() {
    var portalUrl = currentPortal.replace(/\/+$/, "") + "/?screen=1920x1080&manufacturer=SmartTV&brand=AlemTV&product=TV&model=SmartTV&fullscreen=true";
    var frame = document.getElementById("portalIframe");
    if (frame) {
      frame.src = portalUrl;
      frame.style.display = "block";
      var fallback = document.getElementById("portalFallbackScreen");
      if (fallback) fallback.style.display = "none";
      var splash = document.getElementById("splashScreen");
      if (splash) splash.style.display = "none";
    } else {
      window.location.href = portalUrl;
    }
  };

  window.closePortalFallback = function() {
    var frame = document.getElementById("portalIframe");
    if (frame) frame.style.display = "none";
    showScreen("main");
  };

  // Spatial D-pad Focus Engine
  function getFocusableElements() {
    var root = document;
    if (isShown("loginModal")) {
      root = document.getElementById("loginModal");
    } else if (isShown("epgModal")) {
      root = document.getElementById("epgModal");
    } else if (isShown("settingsModal")) {
      root = document.getElementById("settingsModal");
    } else if (isShown("zapDrawer")) {
      root = document.getElementById("zapDrawer");
    } else if (currentScreen === "player") {
      root = document.getElementById("playerOsd");
    } else if (currentScreen === "splash") {
      root = document.getElementById("splashScreen");
    } else if (currentScreen === "portalFallback") {
      root = document.getElementById("portalFallbackScreen");
    } else {
      root = document.getElementById("mainScreen");
    }
    if (!root) return [];
    var list = root.querySelectorAll(".focusable, button, input, select");
    var res = [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].offsetParent !== null) res.push(list[i]);
    }
    return res;
  }

  function updateFocus() {
    var list = getFocusableElements();
    if (list.length === 0) return;
    if (currentFocusIndex >= list.length) currentFocusIndex = 0;
    list.forEach(function(el, idx){
      if (idx === currentFocusIndex) {
        el.classList.add("focused");
        try { el.focus(); } catch(e){}
      } else {
        el.classList.remove("focused");
      }
    });
  }

  function moveFocus(dir) {
    var list = getFocusableElements();
    if (list.length === 0) return;
    var cur = list[currentFocusIndex];
    if (!cur) {
      currentFocusIndex = 0;
      updateFocus();
      return;
    }
    var curRect = cur.getBoundingClientRect();
    var curCx = curRect.left + curRect.width / 2;
    var curCy = curRect.top + curRect.height / 2;

    var bestIndex = -1;
    var bestDist = Infinity;

    for (var i = 0; i < list.length; i++) {
      if (i === currentFocusIndex) continue;
      var r = list[i].getBoundingClientRect();
      var cx = r.left + r.width / 2;
      var cy = r.top + r.height / 2;
      var dx = cx - curCx;
      var dy = cy - curCy;

      var match = false;
      if (dir === "left" && dx < -10 && Math.abs(dy) <= Math.abs(dx) * 1.5) match = true;
      if (dir === "right" && dx > 10 && Math.abs(dy) <= Math.abs(dx) * 1.5) match = true;
      if (dir === "up" && dy < -10) match = true;
      if (dir === "down" && dy > 10) match = true;

      if (match) {
        var d = dx * dx + dy * dy;
        if (d < bestDist) {
          bestDist = d;
          bestIndex = i;
        }
      }
    }

    if (bestIndex !== -1) {
      currentFocusIndex = bestIndex;
      updateFocus();
      list[bestIndex].scrollIntoView({ behavior: "smooth", block: "nearest" });
    } else {
      if (dir === "down" && currentFocusIndex < list.length - 1) {
        currentFocusIndex++;
        updateFocus();
      } else if (dir === "up" && currentFocusIndex > 0) {
        currentFocusIndex--;
        updateFocus();
      }
    }
  }

  // Remote Control Key Handler
  // Overlay state for the remote key table (Swift PlayerOverlay, spec §4):
  // error > channelList > schedule > settings > login > panel > none.
  // Elements are shown by setting style.display to block/flex; anything else
  // (none, missing) counts as hidden.
  function isShown(id) {
    var el = document.getElementById(id);
    return !!el && el.style && (el.style.display === "block" || el.style.display === "flex");
  }

  function currentOverlay() {
    if (isShown("loginModal")) return "login";
    if (isShown("epgModal")) return "schedule";
    if (isShown("settingsModal")) return "settings";
    if (isShown("zapDrawer")) return "channelList";
    if (currentScreen === "player") {
      if (isShown("playerError")) return "error";
      return osdHidden ? "none" : "panel";
    }
    return "main";
  }

  function stepSelect(el, delta) {
    if (!el || el.tagName !== "SELECT" || !el.options || el.options.length === 0) return false;
    el.selectedIndex = (el.selectedIndex + delta + el.options.length) % el.options.length;
    if (el.onchange) el.onchange();
    el.dispatchEvent(new Event("change"));
    return true;
  }

  function activateFocused() {
    var list = getFocusableElements();
    var activeEl = list[currentFocusIndex];
    if (!activeEl) return false;
    if (activeEl.tagName === "SELECT") return stepSelect(activeEl, 1);
    if (activeEl.tagName === "INPUT") {
      try { activeEl.focus(); } catch(err){}
      return true;
    }
    activeEl.click();
    return true;
  }

  window.addEventListener("keydown", function(e){
    var code = e.keyCode;
    var key = e.key;

    // Numbers 0..9 (direct channel tuning)
    if (code >= 48 && code <= 57) {
      var digit = String(code - 48);
      digitBuffer += digit;
      var banner = document.getElementById("digitBanner");
      if (banner) {
        banner.textContent = digitBuffer;
        banner.style.display = "block";
      }
      if (digitTimer) clearTimeout(digitTimer);
      digitTimer = setTimeout(function(){
        var targetNum = parseInt(digitBuffer);
        var found = authData.channels.filter(function(c){ return c.index === targetNum || c.id === targetNum; })[0];
        if (found) playChannel(found);
        digitBuffer = "";
        if (banner) banner.style.display = "none";
      }, 1500);
      return;
    }

    var overlay = currentOverlay();
    var onPlayer = (currentScreen === "player");

    // Arrow keys — Swift PlaybackPolicy.action table.
    // Overlay first: inside a list/guide/modal arrows move focus. Only on the
    // bare player (panel/none/error) do arrows act on playback:
    // UP/DOWN switch channels, LEFT opens the channel list, RIGHT seeks +10
    // (archive: LEFT seeks back). Any arrow press hides the OSD first so the
    // action is immediate (the panel is transient).
    var inList = overlay === "channelList" || overlay === "schedule" || overlay === "settings" || overlay === "login" || overlay === "main";
    if (code === 37 || key === "ArrowLeft") {
      e.preventDefault();
      if (inList) {
        var curElL = getFocusableElements()[currentFocusIndex];
        if (!stepSelect(curElL, -1)) moveFocus("left");
      } else if (onPlayer) {
        hideOsd();
        if (playerMode === "archive") seekRelative(-10);
        else showChannelList();
      } else {
        moveFocus("left");
      }
    } else if (code === 39 || key === "ArrowRight") {
      e.preventDefault();
      if (inList) {
        var curElR = getFocusableElements()[currentFocusIndex];
        if (!stepSelect(curElR, 1)) moveFocus("right");
      } else if (onPlayer) {
        hideOsd();
        if (playerMode !== "live") seekRelative(10);
      } else {
        moveFocus("right");
      }
    } else if (code === 38 || key === "ArrowUp" || key === "ChannelUp" || code === 28) {
      e.preventDefault();
      if (inList) {
        moveFocus("up");
      } else if (onPlayer) {
        hideOsd();
        if (playerMode !== "archive") previousChannel();
      } else {
        moveFocus("up");
      }
    } else if (code === 40 || key === "ArrowDown" || key === "ChannelDown" || code === 29) {
      e.preventDefault();
      if (inList) {
        moveFocus("down");
      } else if (onPlayer) {
        hideOsd();
        if (playerMode !== "archive") nextChannel();
      } else {
        moveFocus("down");
      }
    } else if (code === 13 || code === 65385 || key === "Enter" || key === "Select") {
      e.preventDefault();
      if (overlay === "login" || overlay === "settings" || overlay === "schedule" || overlay === "channelList" || overlay === "main") {
        activateFocused();
      } else if (onPlayer) {
        if (overlay === "error") {
          hidePlayerError();
          showOsd();
        } else if (osdHidden) {
          showOsd();
          var osdList = getFocusableElements();
          if (osdList.length) { currentFocusIndex = 0; updateFocus(); }
        } else {
          activateFocused();
        }
      }
    } else if (code === 10009 || code === 461 || code === 27 || key === "Escape" || key === "Return" || key === "Back") {
      // Samsung Tizen 10009 / webOS 461 / Escape — close overlay, then exit.
      e.preventDefault();
      var frame = document.getElementById("portalIframe");
      if (frame && frame.style.display !== "none") {
        frame.style.display = "none";
        frame.src = "about:blank";
        showScreen("main");
        return;
      }
      if (document.getElementById("loginModal") && document.getElementById("loginModal").style.display !== "none") {
        closeLoginModal();
      } else if (document.getElementById("epgModal") && document.getElementById("epgModal").style.display !== "none") {
        closeEpgModal();
      } else if (document.getElementById("settingsModal") && document.getElementById("settingsModal").style.display !== "none") {
        closeSettings();
      } else if (document.getElementById("zapDrawer") && document.getElementById("zapDrawer").style.display !== "none") {
        closeChannelList();
      } else if (onPlayer) {
        backToChannelList();
      } else if (currentScreen === "main") {
        showSettings();
      }
    } else if (code === 192 || key === "Menu") { // MENU — open guide while watching
      if (onPlayer) { e.preventDefault(); openCurrentEpg(); }
    } else if (code === 403 || key === "ColorF0Red" || key === "MediaPlayPause" || code === 411) { // RED / PLAY
      togglePlayPause();
    } else if (code === 404 || key === "ColorF1Green") { // GREEN — back to live edge
      jumpToLive();
    } else if (code === 405 || key === "ColorF2Yellow") { // YELLOW — favorite
      if (currentPlayingChannel) toggleFavorite(currentPlayingChannel.id);
    } else if (code === 406 || key === "ColorF3Blue") { // BLUE — settings
      showSettings();
    } else if (code === 457 || key === "Info") { // INFO — program guide
      openCurrentEpg();
    } else if (key === "MediaRewind") { // rewind 10s (player)
      if (onPlayer) { e.preventDefault(); seekRelative(-10); }
    } else if (key === "MediaFastForward") { // forward 10s (player)
      if (onPlayer) { e.preventDefault(); seekRelative(10); }
    }
  });

  // Clock ticker + OSD timeline refresh
  setInterval(function(){
    var d = new Date();
    var s = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    var c1 = document.getElementById("topClock");
    var c2 = document.getElementById("osdClock");
    if (c1) c1.textContent = s;
    if (c2) c2.textContent = s;
    if (currentScreen === "player" && currentPlayingChannel) {
      updateOsdTimeline();
    }
  }, 1000);

  // Tizen TV key registration
  if (window.tizen && tizen.tvinputdevice) {
    try {
      var keys = [
        "MediaPlayPause", "MediaPlay", "MediaPause", "MediaFastForward", "MediaRewind",
        "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
        "ChannelUp", "ChannelDown", "ColorF0Red", "ColorF1Green", "ColorF2Yellow", "ColorF3Blue", "Info",
        "Enter", "Select"
      ];
      keys.forEach(function(k){
        try { tizen.tvinputdevice.registerKey(k); } catch(e){}
      });
    } catch(e){}
  }

  // Boot on startup
  var booted = false;
  function startBoot() {
    if (booted) return;
    booted = true;
    updateSplashSelector();
    try {
      bootAlemTV();
    } catch (e) {
      console.error("boot error:", e);
      showBootError("Ошибка запуска: " + (e.message || e));
    }
  }

  if (document.readyState === "complete" || document.readyState === "interactive") {
    setTimeout(startBoot, 30);
  } else {
    window.addEventListener("load", startBoot);
    document.addEventListener("DOMContentLoaded", startBoot);
  }
})();"""
    return template.replace('__DEFAULT_PORTAL__', safe_default_portal).replace('__PORTAL_LIST__', safe_portals_json)

def build_alemtv_html(default_portal=ALEMTV_DEFAULT_PORTAL):
    """Generate the self-contained Alem TV Smart TV client HTML with embedded CSS and JS."""
    css = build_alemtv_css()
    js = build_alemtv_js(default_portal=default_portal)

    template = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920,height=1080,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta http-equiv="Content-Security-Policy" content="default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src * http: https: ws: wss: data: blob:; media-src * http: https: data: blob:; frame-src * http: https: data: blob:; img-src * http: https: data: blob:; style-src * 'unsafe-inline' data: blob:; script-src * 'unsafe-inline' 'unsafe-eval' data: blob:;">
<title>Alem TV</title>
<link rel="stylesheet" href="style.css">
<style type="text/css">
__CSS__
</style>
</head>
<body style="width:1920px;height:1080px;background:#080c11;color:#f0f4f8;margin:0;padding:0;overflow:hidden;">

<!-- SPLASH / BOOT SCREEN -->
<div id="splashScreen" class="screen active" style="display:flex;position:absolute;top:0;left:0;right:0;bottom:0;width:1920px;height:1080px;background:#080c11;flex-direction:column;align-items:center;justify-content:center;">
  <img class="splash-logo" src="icon.png" alt="Alem TV" style="width:140px;height:140px;border-radius:28px;margin-bottom:24px;">
  <div class="splash-title" style="font-size:40px;font-weight:700;color:#ffffff;margin-bottom:12px;">ALEM TV</div>
  <div id="splashStatus" class="splash-sub" style="font-size:20px;color:#ff8800;font-weight:600;margin-bottom:20px;">Подключение к сервису Alem TV…</div>
  <div id="splashSpinner" class="spinner"></div>

  <!-- TV-remote friendly Server Selector (Focusable buttons with D-pad navigation) -->
  <div style="margin-top:20px;text-align:center;max-width:1300px;">
    <div style="font-size:15px;color:#94a3b8;font-weight:600;margin-bottom:10px;">Выберите сервер портала (пультом ◀ ▶ и ОК):</div>
    <div id="splashServerButtons" style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;">
      <!-- Generated by JS: <button class="btn focusable">...</button> -->
    </div>
  </div>

  <select id="splashPortalSelector" style="display:none;" onchange="onSplashPortalSelected(this.value)"></select>

  <div id="splashErr" class="splash-err" style="display:none;margin-top:20px;text-align:center;">
    <div id="splashErrMsg" class="splash-err-msg" style="font-size:20px;color:#ff7485;margin-bottom:16px;">Не удалось подключиться к порталу. Проверьте интернет.</div>
    <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap;">
      <button id="btnRetry" class="btn btn-primary focusable" onclick="bootAlemTV()">Повторить</button>
      <button id="btnAutoScan" class="btn focusable" onclick="scanAndConnectPortal()">Автопоиск сервера</button>
      <button id="btnSilkyOpenSplash" class="btn focusable" onclick="onSplashPortalSelected('https://iptv-org.github.io/iptv/index.m3u8')">SilkyOpenChannel (Тест)</button>
      <button id="btnLoginSplash" class="btn focusable" onclick="showLoginModal()">Вход по договору</button>
      <button id="btnCustomPortalSplash" class="btn focusable" onclick="showCustomPortalModal()">Свой адрес URL/IP</button>
      <button id="btnOpenPortal" class="btn focusable" onclick="launchOfficialPortal()">Открыть веб-портал</button>
      <button id="btnSettingsSplash" class="btn focusable" onclick="showSettings()">Настройки</button>
    </div>
  </div>
</div>

<!-- MAIN GUIDE SCREEN -->
<div id="mainScreen" class="screen" style="display:none;position:absolute;top:0;left:0;right:0;bottom:0;width:1920px;height:1080px;">
  <!-- SIDEBAR -->
  <div class="sidebar">
    <div class="brand-box">
      <img src="icon.png" alt="Alem TV">
      <div class="brand-text">ALEM TV</div>
    </div>
    <div class="nav-menu" id="categoryMenu">
      <!-- Categories rendered by JS -->
    </div>
    <div class="sidebar-foot">
      <div>Платформа: IPTVPORTAL</div>
      <div id="sidebarMac">MAC: —</div>
    </div>
  </div>

  <!-- CONTENT PANE -->
  <div class="content-pane">
    <div class="content-header">
      <div id="contentTitle" class="page-title">Все каналы</div>
      <div class="top-info">
        <span id="topPortalBadge" class="portal-badge">Turkmentelekom</span>
        <span id="topClock" class="clock-text">--:--</span>
        <button class="btn focusable" style="padding: 6px 18px; font-size: 15px;" onclick="showSettings()">⚙ Настройки</button>
      </div>
    </div>

    <!-- CHANNEL GRID -->
    <div class="channel-grid" id="channelGrid">
      <!-- Cards rendered by JS -->
    </div>

    <!-- FOOTER HINTS -->
    <div class="hints-bar">
      <div class="hint-chip"><span class="hint-key">OK</span> Смотреть</div>
      <div class="hint-chip"><span class="hint-key">INFO</span> Программа передач</div>
      <div class="hint-chip"><span class="hint-key">★ / ЖЕЛТАЯ</span> В избранное</div>
      <div class="hint-chip"><span class="hint-key">0-9</span> Номер канала</div>
      <div class="hint-chip"><span class="hint-key">СИНИЙ</span> Настройки</div>
      <div class="hint-chip"><span class="hint-key">RETURN</span> Назад / Выход</div>
    </div>
  </div>
</div>

<!-- FULLSCREEN VIDEO PLAYER SCREEN -->
<div id="playerScreen" class="screen" style="display:none;position:absolute;top:0;left:0;right:0;bottom:0;width:1920px;height:1080px;background:#000;">
  <video id="videoElement" autoplay playsinline></video>

  <!-- PLAYER ERROR OVERLAY -->
  <div id="playerError" style="display:none;position:absolute;top:0;left:0;right:0;bottom:0;align-items:center;justify-content:center;background:rgba(0,0,0,0.85);color:#f87171;font-size:28px;z-index:20;">Ошибка</div>

  <!-- PLAYER OSD OVERLAY -->
  <div class="player-osd" id="playerOsd">
    <div class="osd-top">
      <div class="osd-channel">
        <img class="osd-logo" src="icon.png" alt="">
        <div>
          <div class="osd-ch-title" id="osdChTitle">Канал</div>
          <div style="font-size: 18px; color: #94a3b8;" id="osdClock">--:--</div>
        </div>
      </div>
      <div class="osd-badge">HLS FLUSSONIC</div>
    </div>

    <div class="osd-bottom">
      <div class="osd-prog-title" id="osdProgTitle">Прямой эфир</div>
      <div class="osd-timeline">
        <span class="osd-time" id="osdCurrentTime">00:00</span>
        <div class="osd-track"><div class="osd-fill" id="osdProgressFill" style="width: 100%;"></div></div>
        <span class="osd-time">LIVE</span>
      </div>
      <div class="osd-ctrls">
        <button class="btn focusable" id="btnOsdRwd" onclick="seekRelative(-10)">-10 сек</button>
        <button class="btn btn-primary focusable" id="btnOsdPlay" onclick="togglePlayPause()">Пауза</button>
        <button class="btn focusable" id="btnOsdFwd" onclick="seekRelative(10)">+10 сек</button>
        <button class="btn focusable" id="btnOsdLive" onclick="jumpToLive()">Эфир</button>
        <button class="btn focusable" id="btnOsdEpg" onclick="openCurrentEpg()">Программа</button>
        <button class="btn focusable" id="btnOsdAspect" onclick="cycleAspectRatio()">16:9</button>
        <button class="btn focusable" id="btnOsdBack" onclick="backToChannelList()">В список</button>
      </div>
      <div class="osd-hints">
        <div class="hint-chip"><span class="hint-key">ВВЕРХ / ВНИЗ</span> Смена канала</div>
        <div class="hint-chip"><span class="hint-key">ВЛЕВО</span> Список каналов</div>
        <div class="hint-chip"><span class="hint-key">ВПРАВО</span> +10 сек</div>
        <div class="hint-chip"><span class="hint-key">OK</span> Панель / выбор</div>
        <div class="hint-chip"><span class="hint-key">ИНФО</span> Программа</div>
        <div class="hint-chip"><span class="hint-key">ЗЕЛЕНЫЙ</span> Эфир</div>
        <div class="hint-chip"><span class="hint-key">0-9</span> Номер канала</div>
        <div class="hint-chip"><span class="hint-key">RETURN</span> Назад</div>
      </div>
    </div>
  </div>
</div>

<!-- QUICK CHANNEL ZAPPING DRAWER -->
<div class="zap-drawer" id="zapDrawer" style="display:none;">
  <div class="zap-title">Быстрое переключение</div>
  <div class="zap-list" id="zapListContainer"></div>
</div>

<!-- DIRECT CHANNEL TUNING BANNER (e.g. typing 1 4 on remote) -->
<div class="digit-banner" id="digitBanner" style="display:none;"></div>

<!-- EPG SCHEDULE MODAL -->
<div id="epgModal" class="modal-overlay" style="display:none;position:absolute;top:0;left:0;right:0;bottom:0;">
  <div class="modal-box">
    <div class="modal-header">
      <div id="epgDayNav" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px;"></div>
      <div class="modal-title" id="epgModalTitle">Программа передач</div>
      <button class="btn focusable" onclick="closeEpgModal()">Закрыть [Back]</button>
    </div>
    <div class="epg-list" id="epgListContainer">
      <!-- EPG schedule rendered here -->
    </div>
  </div>
</div>

<!-- SETTINGS MODAL / SCREEN -->
<div id="settingsModal" class="modal-overlay" style="display:none;position:absolute;top:0;left:0;right:0;bottom:0;">
  <div class="modal-box" style="width: 860px;">
    <div class="modal-header">
      <div class="modal-title">Настройки Alem TV</div>
      <button class="btn focusable" onclick="closeSettings()">Закрыть [Back]</button>
    </div>
    <div style="display:flex;flex-direction:column;gap:18px;padding-top:10px;">
      <div class="form-group">
        <label class="form-label">Сервер портала (Оператор)</label>
        <select id="portalSelector" class="form-select focusable" onchange="onPortalSelected(this.value)">
          <!-- Rendered in JS -->
        </select>
      </div>
      <div class="form-group">
        <label class="form-label">Свой URL сервера портала (IP или домен)</label>
        <div style="display:flex;gap:10px;">
          <input id="customPortalInput" class="form-input focusable" type="text" placeholder="http://telecom.alemtv.tm" style="flex:1;">
          <button class="btn focusable" onclick="applyCustomPortal()">Задать</button>
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">Идентификатор терминала (MAC-адрес привязки)</label>
        <div style="display:flex;gap:10px;">
          <input id="customMacInput" class="form-input focusable" type="text" placeholder="00:1A:79:XX:XX:XX" style="flex:1;">
          <button class="btn focusable" onclick="applyCustomMac()">Применить MAC</button>
        </div>
      </div>
      <div class="info-row">
        <span class="info-lbl">Текущий MAC устройства:</span>
        <span class="info-val" id="settingsMacVal">—</span>
      </div>
      <div class="info-row">
        <span class="info-lbl">Сессия IPTVPORTAL:</span>
        <span class="info-val" id="settingsSessionVal">Не авторизован</span>
      </div>
      <div class="info-row">
        <span class="info-lbl">Доступных каналов:</span>
        <span class="info-val" id="settingsChCount">0</span>
      </div>
      <div style="display:flex;gap:12px;margin-top:10px;flex-wrap:wrap;">
        <button class="btn btn-primary focusable" onclick="showLoginModal()">Вход по договору</button>
        <button class="btn focusable" onclick="onSplashPortalSelected('https://iptv-org.github.io/iptv/index.m3u8')">SilkyOpenChannel (Тест)</button>
        <button class="btn focusable" onclick="reauthPortal()">Переподключиться</button>
        <button class="btn focusable" onclick="scanAndConnectPortal()">Автопоиск сервера</button>
        <button class="btn focusable" onclick="launchOfficialPortal()">Официальный веб-портал</button>
        <button class="btn focusable" onclick="resetAlemCache()">Сбросить кэш</button>
      </div>
    </div>
  </div>
</div>

<!-- SUBSCRIBER LOGIN MODAL -->
<div id="loginModal" class="modal-overlay" style="display:none;position:absolute;top:0;left:0;right:0;bottom:0;">
  <div class="modal-box" style="width: 720px;">
    <div class="modal-header">
      <div class="modal-title">Вход абонента Alem TV</div>
      <button class="btn focusable" onclick="closeLoginModal()">Закрыть [Back]</button>
    </div>
    <div style="display:flex;flex-direction:column;gap:18px;padding-top:12px;">
      <div style="font-size:16px;color:#94a3b8;line-height:1.4;">
        Введите номер договора / логин и пароль абонента Alem TV (выдаются оператором при подключении) для регистрации устройства и загрузки полного каталога из более 250 каналов.
      </div>
      <div class="form-group">
        <label class="form-label">Номер договора / Логин абонента</label>
        <input id="loginUsername" class="form-input focusable" type="text" placeholder="Например: 12345678">
      </div>
      <div class="form-group">
        <label class="form-label">Пароль</label>
        <input id="loginPassword" class="form-input focusable" type="password" placeholder="Пароль договора">
      </div>
      <div id="loginErrMsg" style="font-size:16px;color:#ff7485;min-height:22px;"></div>
      <div style="display:flex;gap:12px;margin-top:6px;">
        <button class="btn btn-primary focusable" onclick="submitSubscriberLogin()">Войти и получить 250+ каналов</button>
        <button class="btn focusable" onclick="closeLoginModal()">Отмена</button>
      </div>
    </div>
  </div>
</div>

<!-- WEB PORTAL FULLSCREEN IFRAME -->
<iframe id="portalIframe" src="about:blank" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;width:100%;height:100%;border:0;background:#000;z-index:9999;" allowfullscreen></iframe>

<script>
__JS__
</script>
</body>
</html>"""
    return template.replace('__CSS__', css).replace('__JS__', js)

def _ar_member(name, content, mtime=None):
    """Create a BSD/Debian ar archive member header + payload (aligned to 2 bytes)."""
    if mtime is None:
        mtime = int(time.time())
    header = (
        f'{name:<16}'
        f'{int(mtime):<12}'
        f'{"0":<6}'
        f'{"0":<6}'
        f'{"100644":<8}'
        f'{len(content):<10}'
        '`\n'
    ).encode('ascii')
    padding = b'\n' if (len(content) % 2) != 0 else b''
    return header + content + padding

def build_alemtv_ipk(target_url=ALEMTV_DEFAULT_PORTAL, icon_bytes=None, large_icon_bytes=None, splash_bytes=None):
    """Package the Alem TV application into a standard webOS .ipk package."""
    app_id = ALEMTV_WEBOS_ID
    version = '1.0.0'
    mtime = int(time.time())

    appinfo = {
        'id': app_id,
        'version': version,
        'vendor': 'AlemTV',
        'type': 'web',
        'main': 'index.html',
        'title': 'Alem TV',
        'icon': 'icon.png',
        'largeIcon': 'largeIcon.png',
        'splashBackground': 'splash.png',
        'resolution': '1920x1080',
        'uiRevision': 2,
        'appDescription': 'Alem TV Fullscreen Smart TV App for LG webOS',
        'checkUpdateURL': 'http://telecom.alemtv.tm',
        'inspectable': True,
        'handlesRelaunch': True,
        'disableBackHistoryAPI': True,
    }

    assets = get_alemtv_assets()
    icon_bytes = icon_bytes or assets.get('icon')
    large_icon_bytes = large_icon_bytes or assets.get('large_icon') or icon_bytes
    splash_bytes = splash_bytes or assets.get('splash') or icon_bytes

    if not icon_bytes:
        raise FileNotFoundError('Alem TV icon asset missing for .ipk build')

    index_html = build_alemtv_html(default_portal=target_url).encode('utf-8')
    style_css = build_alemtv_css().encode('utf-8')
    app_js = build_alemtv_js(default_portal=target_url).encode('utf-8')

    app_files = [
        ('appinfo.json', json.dumps(appinfo, ensure_ascii=False, indent=2).encode('utf-8')),
        ('index.html', index_html),
        ('style.css', style_css),
        ('app.js', app_js),
        ('icon.png', icon_bytes),
        ('largeIcon.png', large_icon_bytes),
        ('splash.png', splash_bytes),
    ]

    data_buf = io.BytesIO()
    with tarfile.open(fileobj=data_buf, mode='w:gz', format=tarfile.USTAR_FORMAT) as tf:
        for name, payload in app_files:
            fi = tarfile.TarInfo(f'usr/palm/applications/{app_id}/{name}')
            fi.type = tarfile.REGTYPE
            fi.mode = 0o644
            fi.size = len(payload)
            fi.uid = 0
            fi.gid = 0
            fi.uname = 'root'
            fi.gname = 'root'
            fi.mtime = mtime
            tf.addfile(fi, io.BytesIO(payload))
    data_targz = data_buf.getvalue()

    installed_kb = max(1, (sum(len(p) for _, p in app_files) + 1023) // 1024)
    control_txt = (
        f'Package: {app_id}\n'
        f'Version: {version}\n'
        'Section: misc\n'
        'Priority: optional\n'
        'Architecture: all\n'
        f'Installed-Size: {installed_kb}\n'
        'Maintainer: AlemTV <support@alemtv.tm>\n'
        'Description: Alem TV Fullscreen Smart TV App\n'
        'webOS-Package-Format-Version: 2\n'
        'webOS-Packager-Version: 2.0.0\n'
    ).encode('utf-8')

    ctrl_buf = io.BytesIO()
    with tarfile.open(fileobj=ctrl_buf, mode='w:gz', format=tarfile.USTAR_FORMAT) as tf:
        ci = tarfile.TarInfo('control')
        ci.type = tarfile.REGTYPE
        ci.mode = 0o644
        ci.size = len(control_txt)
        ci.uid = 0
        ci.gid = 0
        ci.uname = 'root'
        ci.gname = 'root'
        ci.mtime = mtime
        tf.addfile(ci, io.BytesIO(control_txt))
    ctrl_targz = ctrl_buf.getvalue()

    ipk_buf = io.BytesIO()
    ipk_buf.write(b'!<arch>\n')
    ipk_buf.write(_ar_member('debian-binary', b'2.0\n', mtime))
    ipk_buf.write(_ar_member('control.tar.gz', ctrl_targz, mtime))
    ipk_buf.write(_ar_member('data.tar.gz', data_targz, mtime))
    return ipk_buf.getvalue()

def build_alemtv_wgt(target_url=ALEMTV_DEFAULT_PORTAL, pkg_id=ALEMTV_PKG_ID, app_name='AlemTV', icon_bytes=None, large_icon_bytes=None, splash_bytes=None):
    """Build an unsigned Samsung Tizen .wgt package for Alem TV."""
    if not re.fullmatch(r'[A-Za-z0-9]{10}', str(pkg_id)):
        raise ValueError(f'Tizen package ID must be exactly 10 alphanumeric chars, got {pkg_id!r}')

    app_id = f'{pkg_id}.{app_name}'
    safe_app_id = xml_escape(app_id, {'"': '&quot;'})
    safe_pkg = xml_escape(str(pkg_id), {'"': '&quot;'})
    safe_app_name = xml_escape(str(app_name or 'AlemTV'))

    assets = get_alemtv_assets()
    icon_bytes = icon_bytes or assets.get('large_icon') or assets.get('icon')
    large_icon_bytes = large_icon_bytes or assets.get('large_icon') or icon_bytes
    splash_bytes = splash_bytes or assets.get('splash') or icon_bytes

    if not icon_bytes:
        raise FileNotFoundError('Alem TV icon asset missing for .wgt build')

    config_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<widget xmlns="http://www.w3.org/ns/widgets"\n'
        '        xmlns:tizen="http://tizen.org/ns/widgets"\n'
        f'        id="http://yourdomain/{safe_pkg}"\n'
        '        version="1.0.1"\n'
        '        viewmodes="fullscreen">\n'
        f'    <tizen:application id="{safe_app_id}"\n'
        f'                       package="{safe_pkg}"\n'
        '                       required_version="3.0"\n'
        '                       hw-accelerated="true"/>\n'
        '    <content src="index.html"/>\n'
        '    <feature name="http://tizen.org/feature/screen.size.all"/>\n'
        '    <icon src="icon.png"/>\n'
        f'    <name>{safe_app_name}</name>\n'
        '    <access origin="*" subdomains="true"/>\n'
        '    <tizen:allow-navigation>*</tizen:allow-navigation>\n'
        '    <tizen:allow-mixed-content>true</tizen:allow-mixed-content>\n'
        '    <tizen:content-security-policy>'
        "default-src 'self' * 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "frame-src * http: https: data: blob:; "
        "connect-src * http: https: ws: wss: data: blob:; "
        "media-src * http: https: data: blob:; "
        "img-src * http: https: data: blob:; "
        "style-src * 'unsafe-inline' data: blob:; "
        "script-src * 'unsafe-inline' 'unsafe-eval' data: blob:;"
        '</tizen:content-security-policy>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/application.launch"/>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/internet"/>\n'
        '    <tizen:privilege name="http://developer.samsung.com/privilege/network.public"/>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/tv.inputdevice"/>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/tv.audio"/>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/tv.window"/>\n'
        '    <tizen:privilege name="http://tizen.org/privilege/content.read"/>\n'
        '    <tizen:profile name="tv-samsung"/>\n'
        '    <tizen:setting screen-orientation="landscape" context-menu="disable" background-support="enable" encryption="disable" install-location="auto" hwkey-event="enable"/>\n'
        '</widget>\n'
    ).encode('utf-8')

    index_html = build_alemtv_html(default_portal=target_url).encode('utf-8')
    style_css = build_alemtv_css().encode('utf-8')
    app_js = build_alemtv_js(default_portal=target_url).encode('utf-8')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('config.xml', config_xml)
        zf.writestr('index.html', index_html)
        zf.writestr('style.css', style_css)
        zf.writestr('app.js', app_js)
        zf.writestr('icon.png', icon_bytes)
        if large_icon_bytes:
            zf.writestr('largeIcon.png', large_icon_bytes)
        if splash_bytes:
            zf.writestr('splash.png', splash_bytes)
    return buf.getvalue()
