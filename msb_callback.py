"""Standalone offline MSB Callback packages for webOS and Tizen."""
import io
import json
import os
import tarfile
import time
import zipfile

from alemtv_app import _ar_member

LG_ID = 'com.neon.msbcallback'
SAMSUNG_PKG_ID = 'MSBCall001'
SAMSUNG_APP_ID = SAMSUNG_PKG_ID + '.Callback'

HTML = r'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meryosab Callback</title>
<style>
*{box-sizing:border-box}
html,body{
  margin:0;
  padding:0;
  width:100%;
  height:100%;
  background:#050b09;
  color:#ebf7ee;
  font-family:'Segoe UI','Roboto','DejaVu Sans',Arial,sans-serif;
  overflow:hidden;
}
body{
  padding:2.4vh 2.8vw 2vh;
  background-color:#050b09;
  background-image:
    radial-gradient(ellipse at 86% 4%,rgba(34,158,94,0.28) 0,transparent 46%),
    radial-gradient(ellipse at 12% 94%,rgba(28,138,168,0.22) 0,transparent 48%),
    linear-gradient(rgba(128,255,176,0.035) 1px,transparent 1px),
    linear-gradient(90deg,rgba(128,255,176,0.035) 1px,transparent 1px);
  background-size:auto,auto,3.2vw 3.2vw,3.2vw 3.2vw;
  display:flex;
  flex-direction:column;
}
.topbar{
  display:flex;
  align-items:center;
  justify-content:space-between;
  padding:1.5vh 1.8vw;
  border-radius:1.1vw;
  border:1px solid rgba(114,228,154,0.22);
  background:linear-gradient(125deg,rgba(16,34,25,0.92),rgba(8,16,13,0.94));
  box-shadow:0 12px 36px rgba(0,0,0,0.45),inset 0 1px 0 rgba(190,255,212,0.1);
  flex:0 0 auto;
}
.brand-wrap{
  display:flex;
  align-items:center;
}
.emblem{
  width:3.8vw;
  height:3.8vw;
  min-width:44px;
  min-height:44px;
  border-radius:0.95vw;
  border:1px solid rgba(132,255,168,0.5);
  background:linear-gradient(145deg,#194c31,#0b1c14);
  box-shadow:0 0 24px rgba(84,244,142,0.25),inset 0 0 14px rgba(132,255,168,0.16);
  display:flex;
  align-items:center;
  justify-content:center;
  font-weight:800;
  font-size:1.15vw;
  letter-spacing:0.12em;
  color:#b6ffaa;
  margin-right:1.3vw;
  flex:0 0 auto;
}
.title-col{
  min-width:0;
}
.kicker{
  font-size:0.78vw;
  letter-spacing:0.24em;
  text-transform:uppercase;
  color:#80d69b;
  font-weight:700;
}
.head{
  font-size:2.35vw;
  font-weight:800;
  letter-spacing:-0.02em;
  margin:0.25vh 0 0.2vh;
  line-height:1.08;
  color:#f3fbf5;
}
.head span{
  color:#9cff9a;
}
.sub{
  font-size:1.02vw;
  color:#a8c4b2;
  line-height:1.3;
}
.tv-pill{
  display:flex;
  align-items:center;
  padding:0.9vh 1.25vw;
  border-radius:999px;
  border:1px solid rgba(114,228,154,0.24);
  background:rgba(8,19,14,0.88);
  flex:0 0 auto;
  margin-left:1.5vw;
}
.tv-pill-group{
  text-align:right;
  margin-right:1.1vw;
}
.tv-model{
  font-size:0.95vw;
  font-weight:700;
  color:#e4f7ea;
}
.tv-summary{
  font-size:0.78vw;
  letter-spacing:0.1em;
  text-transform:uppercase;
  color:#79c998;
  margin-top:0.25vh;
}
.clock-box{
  padding-left:1.1vw;
  border-left:1px solid rgba(114,228,154,0.2);
  font-family:'Consolas','DejaVu Sans Mono','Courier New',monospace;
  font-size:1.55vw;
  font-weight:700;
  color:#b7ffad;
  letter-spacing:0.04em;
}
.main{
  display:flex;
  flex:1 1 auto;
  min-height:0;
  margin-top:1.7vh;
}
.col{
  display:flex;
  flex-direction:column;
  min-height:0;
}
.col-net{
  flex:1.32;
  margin-right:1.25vw;
}
.col-contact{
  flex:1.1;
  margin-right:1.25vw;
}
.col-qr{
  flex:0.86;
}
.panel{
  border-radius:1.05vw;
  border:1px solid rgba(98,198,136,0.2);
  background:linear-gradient(155deg,rgba(14,29,22,0.92),rgba(8,16,13,0.95));
  box-shadow:0 10px 30px rgba(0,0,0,0.36),inset 0 1px 0 rgba(185,255,205,0.06);
  padding:1.6vh 1.45vw;
}
.sec-head{
  display:flex;
  align-items:center;
  justify-content:space-between;
  margin-bottom:1.1vh;
}
.sec-title{
  font-size:0.82vw;
  letter-spacing:0.2em;
  text-transform:uppercase;
  color:#85d29c;
  font-weight:700;
}
.sec-note{
  font-size:0.78vw;
  color:#759684;
}
.net-card{
  position:relative;
  flex:1 1 0;
  display:flex;
  flex-direction:column;
  justify-content:space-between;
  border-radius:1vw;
  border:1px solid rgba(95,152,120,0.24);
  background:linear-gradient(145deg,rgba(12,24,19,0.94),rgba(7,14,11,0.96));
  padding:1.45vh 1.35vw;
  margin-bottom:1.15vh;
  overflow:hidden;
}
.net-card.is-wired.is-on{
  border-color:rgba(78,214,255,0.52);
  background:linear-gradient(140deg,rgba(12,35,44,0.92),rgba(8,18,22,0.96));
  box-shadow:0 0 26px rgba(62,202,255,0.14),inset 0 1px 0 rgba(155,235,255,0.14);
}
.net-card.is-wifi.is-on{
  border-color:rgba(102,244,148,0.54);
  background:linear-gradient(140deg,rgba(14,42,27,0.92),rgba(8,19,14,0.96));
  box-shadow:0 0 26px rgba(84,244,142,0.15),inset 0 1px 0 rgba(176,255,194,0.14);
}
.net-card.is-off{
  opacity:0.76;
}
.net-top{
  display:flex;
  align-items:center;
  justify-content:space-between;
}
.net-id{
  display:flex;
  align-items:center;
}
.ico-box{
  width:2.35vw;
  height:2.35vw;
  min-width:30px;
  min-height:30px;
  border-radius:0.65vw;
  display:flex;
  align-items:center;
  justify-content:center;
  margin-right:0.8vw;
  background:rgba(120,170,140,0.1);
  border:1px solid rgba(120,170,140,0.22);
  color:#95bda4;
}
.is-wired.is-on .ico-box{
  background:rgba(70,210,255,0.14);
  border-color:rgba(92,222,255,0.45);
  color:#78e2ff;
}
.is-wifi.is-on .ico-box{
  background:rgba(95,245,146,0.14);
  border-color:rgba(116,250,160,0.45);
  color:#8effac;
}
.net-name{
  font-size:0.96vw;
  font-weight:700;
  color:#e8f6ec;
  letter-spacing:0.03em;
}
.net-sub{
  font-size:0.76vw;
  color:#85a594;
  margin-top:0.15vh;
}
.badge{
  display:inline-flex;
  align-items:center;
  padding:0.42vh 0.8vw;
  border-radius:999px;
  font-size:0.76vw;
  font-weight:700;
  letter-spacing:0.06em;
  text-transform:uppercase;
  background:rgba(120,146,132,0.16);
  border:1px solid rgba(120,146,132,0.28);
  color:#9bb3a5;
}
.badge-dot{
  width:0.52vw;
  height:0.52vw;
  min-width:7px;
  min-height:7px;
  border-radius:50%;
  background:#6c8476;
  margin-right:0.45vw;
}
.is-on .badge{
  background:rgba(72,226,138,0.14);
  border-color:rgba(116,246,166,0.42);
  color:#a8ffb4;
}
.is-wired.is-on .badge{
  background:rgba(66,204,255,0.14);
  border-color:rgba(112,224,255,0.44);
  color:#95ebff;
}
.is-on .badge-dot{
  background:#6dff9b;
  box-shadow:0 0 10px #6dff9b;
}
.is-wired.is-on .badge-dot{
  background:#6ae3ff;
  box-shadow:0 0 10px #6ae3ff;
}
.ip-row{
  display:flex;
  align-items:baseline;
  justify-content:space-between;
  margin:1vh 0 0.9vh;
  padding:0.9vh 1vw;
  border-radius:0.75vw;
  background:rgba(4,9,7,0.68);
  border:1px solid rgba(110,192,144,0.16);
}
.ip-cap{
  font-size:0.75vw;
  letter-spacing:0.16em;
  text-transform:uppercase;
  color:#799b87;
  font-weight:700;
}
.ip-val{
  font-family:'Consolas','DejaVu Sans Mono','Courier New',monospace;
  font-size:1.95vw;
  font-weight:800;
  letter-spacing:0.03em;
  color:#f2fbf4;
}
.is-wired.is-on .ip-val{color:#8be9ff;}
.is-wifi.is-on .ip-val{color:#a8ff9e;}
.is-off .ip-val{color:#6a8375;}
.specs{
  display:flex;
  flex-wrap:wrap;
  margin:0 -0.4vw;
}
.spec{
  width:50%;
  padding:0.35vh 0.4vw;
}
.spec-box{
  padding:0.55vh 0.7vw;
  border-radius:0.55vw;
  background:rgba(255,255,255,0.025);
  border:1px solid rgba(120,185,146,0.1);
}
.spec-k{
  font-size:0.68vw;
  letter-spacing:0.12em;
  text-transform:uppercase;
  color:#729480;
}
.spec-v{
  font-family:'Consolas','DejaVu Sans Mono','Courier New',monospace;
  font-size:0.92vw;
  color:#d8ece0;
  margin-top:0.15vh;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
}
.net-actions{
  display:flex;
  align-items:center;
  justify-content:space-between;
  padding-top:0.2vh;
  flex:0 0 auto;
}
.btn-refresh{
  appearance:none;
  border:1px solid rgba(126,252,158,0.45);
  background:linear-gradient(120deg,#9cff8f,#55df9a);
  color:#05140b;
  font-weight:800;
  font-size:0.86vw;
  letter-spacing:0.05em;
  text-transform:uppercase;
  padding:0.9vh 1.25vw;
  border-radius:0.65vw;
  cursor:pointer;
  outline:none;
  box-shadow:0 4px 18px rgba(90,238,142,0.22);
}
.btn-refresh:focus,.btn-refresh:hover{
  box-shadow:0 0 0 3px rgba(176,255,156,0.35),0 6px 24px rgba(112,255,156,0.35);
}
.net-updated{
  font-size:0.8vw;
  color:#84a792;
  text-align:right;
}
.contact-panel{
  flex:1 1 auto;
  display:flex;
  flex-direction:column;
  justify-content:space-between;
}
.hero-phone{
  padding:1.5vh 1.2vw;
  border-radius:0.9vw;
  border:1px solid rgba(132,255,158,0.42);
  background:linear-gradient(135deg,rgba(24,62,40,0.88),rgba(10,24,17,0.94));
  box-shadow:0 0 28px rgba(96,245,142,0.14);
  margin-bottom:1.1vh;
}
.hero-phone-top{
  display:flex;
  align-items:center;
  justify-content:space-between;
}
.hero-phone-k{
  font-size:0.78vw;
  letter-spacing:0.18em;
  text-transform:uppercase;
  color:#97f0aa;
  font-weight:700;
}
.tag-chip{
  font-size:0.72vw;
  font-weight:700;
  color:#08180f;
  background:#a6ff96;
  padding:0.28vh 0.65vw;
  border-radius:999px;
}
.hero-phone-v{
  font-family:'Consolas','DejaVu Sans Mono','Courier New',monospace;
  font-size:2.15vw;
  font-weight:800;
  color:#ffffff;
  letter-spacing:0.03em;
  margin-top:0.65vh;
}
.contact-item{
  display:flex;
  align-items:center;
  padding:1vh 1vw;
  border-radius:0.75vw;
  background:rgba(6,13,10,0.62);
  border:1px solid rgba(110,192,144,0.16);
  margin-bottom:0.85vh;
}
.contact-ico{
  width:2.2vw;
  height:2.2vw;
  min-width:28px;
  min-height:28px;
  border-radius:0.6vw;
  background:rgba(116,240,158,0.1);
  border:1px solid rgba(116,240,158,0.24);
  color:#9cf7ab;
  display:flex;
  align-items:center;
  justify-content:center;
  margin-right:0.85vw;
  flex:0 0 auto;
}
.contact-k{
  font-size:0.7vw;
  letter-spacing:0.15em;
  text-transform:uppercase;
  color:#79a48a;
  font-weight:700;
}
.contact-v{
  font-size:1.12vw;
  font-weight:600;
  color:#ebf8ef;
  margin-top:0.2vh;
  word-break:break-word;
}
.support-banner{
  padding:1.1vh 1vw;
  border-radius:0.75vw;
  background:linear-gradient(125deg,rgba(20,44,36,0.82),rgba(11,24,22,0.9));
  border:1px dashed rgba(128,238,164,0.34);
  font-size:0.9vw;
  line-height:1.42;
  color:#c5e4d0;
}
.support-banner b{
  color:#a8ff9e;
}
.qr-panel{
  flex:1 1 auto;
  display:flex;
  flex-direction:column;
  justify-content:space-between;
}
.qr-card{
  flex:1 1 0;
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  text-align:center;
  padding:1.1vh 1vw;
  border-radius:0.85vw;
  background:rgba(6,13,10,0.68);
  border:1px solid rgba(112,202,148,0.2);
}
.qr-card.first{
  margin-bottom:1.1vh;
}
.qr-k{
  font-size:0.78vw;
  letter-spacing:0.18em;
  text-transform:uppercase;
  color:#98f2ac;
  font-weight:700;
}
.qr-frame{
  margin:0.9vh auto;
  padding:0.55vw;
  border-radius:0.85vw;
  background:#ffffff;
  box-shadow:0 8px 24px rgba(0,0,0,0.45),0 0 20px rgba(130,255,168,0.16);
}
.qr-frame img{
  display:block;
  width:10.2vw;
  height:10.2vw;
  max-width:22vh;
  max-height:22vh;
  image-rendering:pixelated;
}
.qr-hint{
  font-size:0.82vw;
  color:#acc8b6;
  line-height:1.3;
  max-width:18vw;
}
.footer{
  display:flex;
  align-items:center;
  justify-content:space-between;
  margin-top:1.3vh;
  padding:0 0.3vw;
  font-size:0.82vw;
  color:#799c88;
  flex:0 0 auto;
}
.footer b{
  color:#a9dcb8;
}
@media (max-width:980px){
  html,body{height:auto;overflow:auto;}
  .main,.topbar,.footer{flex-direction:column;align-items:stretch;}
  .col-net,.col-contact{margin-right:0;margin-bottom:16px;}
  .tv-pill{margin-left:0;margin-top:12px;justify-content:space-between;}
  .head{font-size:24px;}.sub,.tv-model,.net-name,.contact-v{font-size:14px;}
  .ip-val,.hero-phone-v{font-size:22px;}
  .kicker,.sec-title,.ip-cap,.contact-k,.qr-k,.tv-summary{font-size:11px;}
  .spec-k,.spec-v,.net-sub,.qr-hint,.footer,.sec-note,.net-updated{font-size:12px;}
  .btn-refresh{font-size:13px;}
  .qr-frame img{width:150px;height:150px;}
}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand-wrap">
    <div class="emblem">MSB</div>
    <div class="title-col">
      <div class="kicker">MERYOSAB · ЦЕНТР ПОДДЕРЖКИ И КОНТАКТОВ</div>
      <div class="head">Meryosab <span>Callback</span></div>
      <div class="sub">По всем вопросам обращайтесь по номеру телефона, в мессенджеры или на сайт · Ашхабад</div>
    </div>
  </div>
  <div class="tv-pill">
    <div class="tv-pill-group">
      <div class="tv-model" id="tvModel">Smart TV</div>
      <div class="tv-summary" id="netSummary">Определяем сеть…</div>
    </div>
    <div class="clock-box" id="tvClock">--:--</div>
  </div>
</header>

<main class="main">
  <section class="col col-net panel">
    <div class="sec-head">
      <div class="sec-title">Сетевые параметры телевизора</div>
      <div class="sec-note">Проводная и беспроводная сеть</div>
    </div>

    <div class="net-card is-wired is-off" id="lanCard">
      <div class="net-top">
        <div class="net-id">
          <div class="ico-box">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M7 18v2M12 18v2M17 18v2M8 9h8M9 13h6"/></svg>
          </div>
          <div>
            <div class="net-name">Проводное подключение (LAN)</div>
            <div class="net-sub">Кабель Ethernet</div>
          </div>
        </div>
        <div class="badge"><i class="badge-dot"></i><span id="lanStatus">Проверка…</span></div>
      </div>
      <div class="ip-row">
        <span class="ip-cap">IP-адрес</span>
        <span class="ip-val" id="lanIp">—</span>
      </div>
      <div class="specs">
        <div class="spec"><div class="spec-box"><div class="spec-k">Маска подсети</div><div class="spec-v" id="lanMask">—</div></div></div>
        <div class="spec"><div class="spec-box"><div class="spec-k">Шлюз</div><div class="spec-v" id="lanGw">—</div></div></div>
        <div class="spec"><div class="spec-box"><div class="spec-k">DNS</div><div class="spec-v" id="lanDns">—</div></div></div>
        <div class="spec"><div class="spec-box"><div class="spec-k">MAC-адрес</div><div class="spec-v" id="lanMac">—</div></div></div>
      </div>
    </div>

    <div class="net-card is-wifi is-off" id="wifiCard">
      <div class="net-top">
        <div class="net-id">
          <div class="ico-box">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><circle cx="12" cy="20" r="1"/></svg>
          </div>
          <div>
            <div class="net-name">Беспроводное подключение (Wi-Fi)</div>
            <div class="net-sub" id="wifiSsid">Точка доступа: —</div>
          </div>
        </div>
        <div class="badge"><i class="badge-dot"></i><span id="wifiStatus">Проверка…</span></div>
      </div>
      <div class="ip-row">
        <span class="ip-cap">IP-адрес</span>
        <span class="ip-val" id="wifiIp">—</span>
      </div>
      <div class="specs">
        <div class="spec"><div class="spec-box"><div class="spec-k">Маска подсети</div><div class="spec-v" id="wifiMask">—</div></div></div>
        <div class="spec"><div class="spec-box"><div class="spec-k">Шлюз</div><div class="spec-v" id="wifiGw">—</div></div></div>
        <div class="spec"><div class="spec-box"><div class="spec-k">DNS</div><div class="spec-v" id="wifiDns">—</div></div></div>
        <div class="spec"><div class="spec-box"><div class="spec-k">MAC-адрес</div><div class="spec-v" id="wifiMac">—</div></div></div>
      </div>
    </div>

    <div class="net-actions">
      <button type="button" class="btn-refresh" id="refreshBtn">Обновить IP (OK)</button>
      <div class="net-updated" id="netUpdated">Автообновление каждые 5 с</div>
    </div>
  </section>

  <section class="col col-contact panel contact-panel">
    <div>
      <div class="sec-head">
        <div class="sec-title">Контакты для связи</div>
        <div class="sec-note">Meryosab</div>
      </div>

      <div class="hero-phone">
        <div class="hero-phone-top">
          <span class="hero-phone-k">Телефон / WhatsApp</span>
          <span class="tag-chip">Звонки · Сообщения</span>
        </div>
        <div class="hero-phone-v">+993 63 70 59 53</div>
      </div>

      <div class="contact-item">
        <div class="contact-ico">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
        </div>
        <div>
          <div class="contact-k">Официальный сайт</div>
          <div class="contact-v">tvrepair.meryosab.com</div>
        </div>
      </div>

      <div class="contact-item">
        <div class="contact-ico">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        </div>
        <div>
          <div class="contact-k">Telegram</div>
          <div class="contact-v">@meryosab_shop</div>
        </div>
      </div>

      <div class="contact-item">
        <div class="contact-ico">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
        </div>
        <div>
          <div class="contact-k">Электронная почта</div>
          <div class="contact-v">tvrepairmsb@gmail.com</div>
        </div>
      </div>

      <div class="contact-item">
        <div class="contact-ico">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
        </div>
        <div>
          <div class="contact-k">Адрес</div>
          <div class="contact-v">Ашхабад, Парахат 3/2, дом 14</div>
        </div>
      </div>
    </div>

    <div class="support-banner">
      <b>Есть вопросы по подключению или настройке?</b> Обращайтесь по номеру телефона, пишите в WhatsApp / Telegram или переходите на сайт.
    </div>
  </section>

  <section class="col col-qr panel qr-panel">
    <div class="qr-card first">
      <div class="qr-k">Открыть сайт</div>
      <div class="qr-frame"><img src="website.png" alt="QR сайта"></div>
      <div class="qr-hint">Наведите камеру телефона, чтобы перейти на сайт</div>
    </div>
    <div class="qr-card">
      <div class="qr-k">Сохранить контакт</div>
      <div class="qr-frame"><img src="contact.png" alt="QR контактной карточки"></div>
      <div class="qr-hint">Сканируйте и подтвердите сохранение номера</div>
    </div>
  </section>
</main>

<footer class="footer">
  <div><b>Контакты и сетевые данные ТВ работают без интернета</b> · Meryosab</div>
  <div>Нажмите <b>OK</b> — обновить IP &nbsp;·&nbsp; <b>RETURN / BACK</b> — выход</div>
</footer>

<script>
(function () {
  'use strict';

  var doc = document;
  var webapisRequested = false;
  var rtcActive = false;

  var state = {
    model: '',
    wired: { known: false, connected: false, ip: '', mask: '', gw: '', dns: '', mac: '' },
    wifi: { known: false, connected: false, ip: '', mask: '', gw: '', dns: '', mac: '', ssid: '' },
    fallbackIp: ''
  };

  function $(id) { return doc.getElementById(id); }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function cleanStr(v) {
    if (v === undefined || v === null) return '';
    var s = String(v).replace(/^\s+|\s+$/g, '');
    if (!s || s === 'undefined' || s === 'null') return '';
    return s;
  }

  function validIp(v) {
    var s = cleanStr(v);
    if (!s || s === '0.0.0.0' || s === '127.0.0.1' || s === '::' || s === '::1') return '';
    return s;
  }

  function validMac(v) {
    var s = cleanStr(v);
    if (!s || /^0+([:-]0+)*$/.test(s)) return '';
    return s.toUpperCase();
  }

  function mergeAdapter(target, patch, authoritative) {
    if (!patch) return;
    target.known = true;
    if (typeof patch.connected === 'boolean') target.connected = patch.connected;
    if (patch.ip) target.ip = patch.ip;
    else if (authoritative && !patch.connected) target.ip = '';
    if (patch.mask) target.mask = patch.mask;
    else if (authoritative && !patch.connected) target.mask = '';
    if (patch.gw) target.gw = patch.gw;
    else if (authoritative && !patch.connected) target.gw = '';
    if (patch.dns) target.dns = patch.dns;
    else if (authoritative && !patch.connected) target.dns = '';
    if (patch.mac) target.mac = patch.mac;
    if (patch.ssid) target.ssid = patch.ssid;
    else if (authoritative && !patch.connected) target.ssid = '';
    if (target.ip && target.connected === false && patch.connected !== false) {
      target.connected = true;
    }
  }

  function updateClock() {
    var now = new Date();
    var node = $('tvClock');
    if (node) node.textContent = pad2(now.getHours()) + ':' + pad2(now.getMinutes());
  }

  function renderAdapter(prefix, info, isWifi) {
    var card = $(prefix + 'Card');
    var statusNode = $(prefix + 'Status');
    var ipNode = $(prefix + 'Ip');
    var maskNode = $(prefix + 'Mask');
    var gwNode = $(prefix + 'Gw');
    var dnsNode = $(prefix + 'Dns');
    var macNode = $(prefix + 'Mac');
    var ssidNode = isWifi ? $('wifiSsid') : null;
    var active = !!(info.connected || info.ip);
    var displayIp = info.ip;

    if (!displayIp && !state.wired.ip && !state.wifi.ip && state.fallbackIp && active) {
      displayIp = state.fallbackIp;
    }

    if (card) {
      card.className = 'net-card ' + (isWifi ? 'is-wifi ' : 'is-wired ') + (active ? 'is-on' : 'is-off');
    }
    if (statusNode) {
      statusNode.textContent = active ? 'Подключён' : (info.known ? 'Не подключён' : 'Определение…');
    }
    if (ipNode) {
      ipNode.textContent = displayIp || (active ? 'Активен' : '—');
    }
    if (maskNode) maskNode.textContent = info.mask || '—';
    if (gwNode) gwNode.textContent = info.gw || '—';
    if (dnsNode) dnsNode.textContent = info.dns || '—';
    if (macNode) macNode.textContent = info.mac || '—';
    if (ssidNode) {
      ssidNode.textContent = info.ssid ? ('Wi-Fi сеть: ' + info.ssid) : (active ? 'Беспроводная сеть активна' : 'Точка доступа: не подключено');
    }
  }

  function render() {
    var lanOn = !!(state.wired.connected || state.wired.ip);
    var wifiOn = !!(state.wifi.connected || state.wifi.ip);
    var summary = [];
    if (lanOn) summary.push('LAN');
    if (wifiOn) summary.push('WI-FI');
    if (!summary.length && state.fallbackIp) summary.push('IP: ' + state.fallbackIp);

    if ($('tvModel')) $('tvModel').textContent = state.model || detectPlatformLabel();
    if ($('netSummary')) {
      $('netSummary').textContent = summary.length
        ? ('Активно: ' + summary.join(' + '))
        : ((state.wired.known || state.wifi.known) ? 'Нет активной сети' : 'Проверка сети…');
    }

    renderAdapter('lan', state.wired, false);
    renderAdapter('wifi', state.wifi, true);

    if (!lanOn && !wifiOn && state.fallbackIp && $('netUpdated')) {
      $('netUpdated').textContent = 'Локальный IP: ' + state.fallbackIp;
    }
  }

  function detectPlatformLabel() {
    var ua = '';
    try { ua = String(navigator.userAgent || ''); } catch (e) {}
    if (window.PalmSystem || /Web0S|webOS/i.test(ua)) return 'LG webOS TV';
    if (window.tizen || /Tizen/i.test(ua)) return 'Samsung Tizen TV';
    return 'Smart TV';
  }

  function parseWebosConnection(res) {
    if (!res || typeof res !== 'object') return;
    function readBlock(blk) {
      if (!blk || typeof blk !== 'object') return null;
      var st = cleanStr(blk.state).toLowerCase();
      var ip = validIp(blk.ipAddress || blk.ip);
      var dnsList = [];
      if (validIp(blk.dns1)) dnsList.push(validIp(blk.dns1));
      if (validIp(blk.dns2)) dnsList.push(validIp(blk.dns2));
      return {
        connected: st === 'connected' || (!!ip && st !== 'disconnected' && st !== 'offline'),
        ip: ip,
        mask: validIp(blk.netmask || blk.subnetMask),
        gw: validIp(blk.gateway),
        dns: dnsList.join(', '),
        mac: validMac(blk.macAddress || blk.mac),
        ssid: cleanStr(blk.ssid)
      };
    }
    var wired = readBlock(res.wired || res.wiredInfo);
    var wifi = readBlock(res.wifi || res.wifiInfo);
    if (wired) mergeAdapter(state.wired, wired, true);
    if (wifi) mergeAdapter(state.wifi, wifi, true);
    render();
  }

  function callLuna(uri, params, onOk, onErr) {
    try {
      if (window.webOS && window.webOS.service && window.webOS.service.request) {
        var idx = uri.lastIndexOf('/');
        var svc = uri.slice(0, idx);
        var method = uri.slice(idx + 1);
        window.webOS.service.request(svc, {
          method: method,
          parameters: params || {},
          onSuccess: function (res) { if (onOk) onOk(res); },
          onFailure: function (err) { if (onErr) onErr(err); }
        });
        return true;
      }
    } catch (e) {}
    try {
      if (window.PalmServiceBridge) {
        var bridge = new window.PalmServiceBridge();
        bridge.onservicecallback = function (msg) {
          var parsed = null;
          try { parsed = JSON.parse(msg); } catch (e) {}
          if (parsed && parsed.returnValue !== false) {
            if (onOk) onOk(parsed);
          } else if (onErr) {
            onErr(parsed);
          }
        };
        bridge.call(uri, JSON.stringify(params || {}));
        return true;
      }
    } catch (e) {}
    return false;
  }

  function probeWebos() {
    try {
      if (window.PalmSystem && window.PalmSystem.deviceInfo) {
        var d = typeof window.PalmSystem.deviceInfo === 'string'
          ? JSON.parse(window.PalmSystem.deviceInfo)
          : window.PalmSystem.deviceInfo;
        if (d && d.modelName) state.model = 'LG ' + cleanStr(d.modelName);
      }
    } catch (e) {}

    callLuna('luna://com.webos.service.tv.systeminfo/getSystemInfo', { keys: ['modelName'] }, function (res) {
      if (res && res.modelName) {
        state.model = 'LG ' + cleanStr(res.modelName);
        render();
      }
    });

    callLuna('luna://com.webos.service.connectionmanager/getStatus', {}, parseWebosConnection, function () {
      callLuna('luna://com.webos.service.connectionmanager/getstatus', {}, parseWebosConnection, function () {
        callLuna('luna://com.palm.connectionmanager/getStatus', {}, parseWebosConnection);
      });
    });

    callLuna('luna://com.webos.service.connectionmanager/getInfo', {}, parseWebosConnection, function () {
      callLuna('luna://com.palm.connectionmanager/getInfo', {}, parseWebosConnection);
    });
  }

  function loadTizenWebApis() {
    if (!window.tizen || window.webapis || webapisRequested) return;
    webapisRequested = true;
    try {
      var s = doc.createElement('script');
      s.src = '$WEBAPIS' + '/webapis/webapis.js';
      s.onload = function () { probeTizen(); };
      s.onerror = function () {};
      (doc.head || doc.body || doc.documentElement).appendChild(s);
    } catch (e) {}
  }

  function probeTizen() {
    var net;
    try {
      if (window.webapis && window.webapis.productinfo && window.webapis.productinfo.getModel) {
        var pm = cleanStr(window.webapis.productinfo.getModel());
        if (pm) state.model = 'Samsung ' + pm;
      }
    } catch (e) {}
    if (!state.model) {
      try {
        if (window.tizen && window.tizen.systeminfo && window.tizen.systeminfo.getCapability) {
          var sm = cleanStr(window.tizen.systeminfo.getCapability('http://tizen.org/system/model_name'));
          if (sm) state.model = 'Samsung ' + sm;
        }
      } catch (e) {}
    }

    try {
      if (window.tizen && window.tizen.systeminfo && window.tizen.systeminfo.getPropertyValue) {
        window.tizen.systeminfo.getPropertyValue('ETHERNET_NETWORK', function (val) {
          if (!val) return;
          var st = cleanStr(val.status).toUpperCase();
          var ip = validIp(val.ipAddress || val.ip);
          mergeAdapter(state.wired, {
            connected: st === 'CONNECTED' || (!!ip && st !== 'DISCONNECTED'),
            ip: ip,
            mac: validMac(val.macAddress)
          }, true);
          render();
        }, function () {});

        window.tizen.systeminfo.getPropertyValue('WIFI_NETWORK', function (val) {
          if (!val) return;
          var st = cleanStr(val.status).toUpperCase();
          var ip = validIp(val.ipAddress || val.ip);
          mergeAdapter(state.wifi, {
            connected: st === 'CONNECTED' || (!!ip && st !== 'DISCONNECTED'),
            ip: ip,
            ssid: cleanStr(val.ssid),
            mac: validMac(val.macAddress)
          }, true);
          render();
        }, function () {});
      }
    } catch (e) {}

    try {
      net = window.webapis && window.webapis.network;
      if (net) {
        var cType = null;
        try { if (net.getActiveConnectionType) cType = net.getActiveConnectionType(); } catch (e) {}
        var wIp = '';
        var wMask = '';
        var wGw = '';
        var wDns = '';
        var wMac = '';
        var wSsid = '';
        try { if (net.getIp) wIp = validIp(net.getIp()); } catch (e) {}
        try { if (net.getSubnetMask) wMask = validIp(net.getSubnetMask()); } catch (e) {}
        try { if (net.getGateway) wGw = validIp(net.getGateway()); } catch (e) {}
        try { if (net.getDns) wDns = validIp(net.getDns()); } catch (e) {}
        try { if (net.getMac) wMac = validMac(net.getMac()); } catch (e) {}
        try { if (net.getWiFiSsid) wSsid = cleanStr(net.getWiFiSsid()); } catch (e) {}

        if (cType === 3) {
          mergeAdapter(state.wired, { connected: true, ip: wIp, mask: wMask, gw: wGw, dns: wDns, mac: wMac }, false);
          if (!state.wifi.known) state.wifi.known = true;
        } else if (cType === 1) {
          mergeAdapter(state.wifi, { connected: true, ip: wIp, mask: wMask, gw: wGw, dns: wDns, mac: wMac, ssid: wSsid }, false);
          if (!state.wired.known) state.wired.known = true;
        } else if (cType === 0) {
          mergeAdapter(state.wired, { connected: false, ip: '', mask: '', gw: '', dns: '' }, true);
          mergeAdapter(state.wifi, { connected: false, ip: '', mask: '', gw: '', dns: '', ssid: '' }, true);
        } else if (wIp) {
          state.fallbackIp = wIp;
        }
        render();
      }
    } catch (e) {}
  }

  function probeWebRtc() {
    if (rtcActive || state.wired.ip || state.wifi.ip) return;
    try {
      var PC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
      if (!PC) return;
      rtcActive = true;
      var pc = new PC({ iceServers: [] });
      if (pc.createDataChannel) pc.createDataChannel('');
      pc.onicecandidate = function (ev) {
        var cand = ev && ev.candidate && ev.candidate.candidate;
        if (!cand) return;
        var m = cand.match(/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/);
        if (m && validIp(m[1])) {
          state.fallbackIp = m[1];
          render();
          try { pc.close(); } catch (e) {}
        }
      };
      var offerP = pc.createOffer();
      if (offerP && typeof offerP.then === 'function') {
        offerP.then(function (offer) { return pc.setLocalDescription(offer); })['catch'](function () {});
      }
    } catch (e) {}
  }

  function refreshNetwork(manual) {
    if (manual && $('netUpdated')) $('netUpdated').textContent = 'Проверяем сетевые интерфейсы…';
    if (window.PalmSystem || window.PalmServiceBridge || (window.webOS && window.webOS.service)) probeWebos();
    if (window.tizen || window.webapis) {
      loadTizenWebApis();
      probeTizen();
    }
    if (!window.PalmSystem && !window.PalmServiceBridge && !window.tizen && !window.webapis) {
      state.wired.known = true;
      state.wifi.known = true;
    }
    probeWebRtc();
    render();
    if (manual && $('netUpdated')) {
      var d = new Date();
      $('netUpdated').textContent = 'Обновлено в ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
    }
  }

  function exitApp() {
    try {
      if (window.tizen && window.tizen.application) {
        window.tizen.application.getCurrentApplication().exit();
        return;
      }
    } catch (e) {}
    try {
      if (window.PalmSystem && window.PalmSystem.platformBack) {
        window.PalmSystem.platformBack();
        return;
      }
    } catch (e) {}
    try { window.close(); } catch (e) {}
  }

  function wire() {
    var btn = $('refreshBtn');
    if (btn) {
      btn.onclick = function () { refreshNetwork(true); };
    }
    doc.addEventListener('keydown', function (e) {
      var code = e.keyCode;
      if (code === 13) {
        refreshNetwork(true);
        return;
      }
      if (code === 10009 || code === 461 || code === 27 || code === 8) {
        exitApp();
      }
    });
    if (window.addEventListener) {
      window.addEventListener('online', function () { refreshNetwork(true); }, false);
      window.addEventListener('offline', function () { refreshNetwork(true); }, false);
    }
  }

  updateClock();
  setInterval(updateClock, 15000);
  wire();
  refreshNetwork(false);
  setInterval(function () { refreshNetwork(false); }, 5000);
  if ($('refreshBtn') && $('refreshBtn').focus) {
    try { $('refreshBtn').focus(); } catch (e) {}
  }
}());
</script>
</body>
</html>'''


def _asset(name):
    root = getattr(__import__('sys'), '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'assets', name), 'rb') as f:
        return f.read()


def _files():
    return {'index.html': HTML.encode('utf-8'), 'website.png': _asset('msb_website_qr.png'),
            'contact.png': _asset('msb_contact_qr.png'), 'icon.png': _asset('msb_icon.png')}


def build_ipk():
    now = int(time.time())
    files = _files()
    files['appinfo.json'] = json.dumps({'id': LG_ID, 'version': '1.0.0', 'vendor': 'Meryosab',
        'type': 'web', 'main': 'index.html', 'title': 'MSB Callback', 'icon': 'icon.png',
        'resolution': '1920x1080', 'uiRevision': 2}, ensure_ascii=False).encode('utf-8')
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz', format=tarfile.USTAR_FORMAT) as tar:
        for name, body in files.items():
            info = tarfile.TarInfo('usr/palm/applications/' + LG_ID + '/' + name)
            info.mode, info.size, info.mtime = 0o644, len(body), now
            tar.addfile(info, io.BytesIO(body))
    text = ('Package: %s\nVersion: 1.0.0\nSection: misc\nPriority: optional\nArchitecture: all\n'
            'Installed-Size: %d\nMaintainer: Meryosab\nDescription: Offline MSB Callback\n'
            'webOS-Package-Format-Version: 2\nwebOS-Packager-Version: 2.0.0\n') % (LG_ID, max(1, (sum(map(len, files.values())) + 1023) // 1024))
    control = io.BytesIO()
    with tarfile.open(fileobj=control, mode='w:gz', format=tarfile.USTAR_FORMAT) as tar:
        body = text.encode('utf-8')
        info = tarfile.TarInfo('control')
        info.mode, info.size, info.mtime = 0o644, len(body), now
        tar.addfile(info, io.BytesIO(body))
    return b'!<arch>\n' + _ar_member('debian-binary', b'2.0\n', now) + _ar_member('control.tar.gz', control.getvalue(), now) + _ar_member('data.tar.gz', data.getvalue(), now)


def build_wgt():
    config = '''<?xml version="1.0" encoding="UTF-8"?>
<widget xmlns="http://www.w3.org/ns/widgets" xmlns:tizen="http://tizen.org/ns/widgets" id="https://tvrepair.meryosab.com/msbcallback" version="1.0.0" viewmodes="fullscreen">
<tizen:application id="%s" package="%s" required_version="3.0"/><content src="index.html"/><icon src="icon.png"/><name>MSB Callback</name>
<feature name="http://tizen.org/feature/screen.size.all"/>
<tizen:privilege name="http://developer.samsung.com/privilege/network.public"/>
<tizen:profile name="tv-samsung"/><tizen:setting screen-orientation="landscape" context-menu="disable" background-support="disable" hwkey-event="enable"/>
</widget>''' % (SAMSUNG_APP_ID, SAMSUNG_PKG_ID)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('config.xml', config.encode('utf-8'))
        for name, data in _files().items():
            z.writestr(name, data)
    return out.getvalue()
