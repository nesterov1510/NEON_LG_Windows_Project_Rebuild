"""Isolated experimental Tizen package opening the official Belet site directly."""
import io
import os
import sys
import zipfile

PACKAGE = 'BeletDir01'
APP = PACKAGE + '.BeletDirect'
URL = 'https://belet.tm/'


def build_wgt(icon_bytes=None):
    if icon_bytes is None:
        root = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'assets', 'belet_large_icon.png'), 'rb') as f:
            icon_bytes = f.read()
    config = '''<?xml version="1.0" encoding="UTF-8"?>
<widget xmlns="http://www.w3.org/ns/widgets" xmlns:tizen="http://tizen.org/ns/widgets" id="https://belet.tm/neon-direct-test" version="1.0.1" viewmodes="maximized">
<tizen:application id="BeletDir01.BeletDirect" package="BeletDir01" required_version="3.0"/>
<content src="index.html"/><icon src="icon.png"/><name>Belet Direct Test</name>
<feature name="http://tizen.org/feature/screen.size.all"/><access origin="*" subdomains="true"/>
<tizen:allow-navigation>*</tizen:allow-navigation><tizen:allow-mixed-content>true</tizen:allow-mixed-content>
<tizen:content-security-policy>default-src 'self' https: data: blob:; script-src 'self' https: 'unsafe-inline' 'unsafe-eval'; style-src 'self' https: 'unsafe-inline'; connect-src 'self' https: wss:; img-src 'self' https: data: blob:; media-src 'self' https: blob:; frame-src 'self' https:;</tizen:content-security-policy>
<tizen:privilege name="http://tizen.org/privilege/internet"/><tizen:privilege name="http://developer.samsung.com/privilege/network.public"/>
<tizen:profile name="tv-samsung"/><tizen:setting screen-orientation="landscape" context-menu="disable" background-support="enable" hwkey-event="enable"/>
</widget>'''
    html = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Belet Direct Test</title><link rel="stylesheet" href="style.css"></head><body><h1>Belet Direct Test</h1><p id="status">Открываем официальный сайт belet.tm без прокси…</p><button id="retry">Повторить</button><script src="boot.js"></script></body></html>'''
    css = "body{background:#080d13;color:#eee;font-family:Arial;text-align:center;padding:10vh}button{font-size:24px;padding:15px}"
    js = '''(function(){
  var button=document.getElementById('retry');
  var status=document.getElementById('status');
  function openBelet(){
    if(status)status.textContent='Открываем belet.tm…';
    try{window.location.replace('https://belet.tm/');}
    catch(e){if(status)status.textContent='Не удалось открыть сайт. Нажмите Повторить.';}
  }
  if(button){button.onclick=openBelet;try{button.focus();}catch(e){}}
  setTimeout(openBelet,100);
  setTimeout(function(){if(status)status.textContent='Сайт не открылся. Нажмите Повторить.';},8000);
})();'''
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('config.xml', config.encode('utf-8'))
        z.writestr('index.html', html.encode('utf-8'))
        z.writestr('style.css', css.encode('utf-8'))
        z.writestr('boot.js', js.encode('utf-8'))
        z.writestr('icon.png', icon_bytes)
    return out.getvalue()
