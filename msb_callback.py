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

HTML = '''<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>MSB Callback</title>
<style>
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:#07100d;color:#edf9ef;font-family:Arial,sans-serif}body{padding:3.5vw 5vw;background:radial-gradient(ellipse at 85% 0,#153d29 0,#07100d 60%)}.brand{color:#a8ffad;font-size:1.2vw;letter-spacing:.3em;font-weight:bold}.head{font-size:3.4vw;font-weight:bold;margin:1.1vw 0 .4vw}.sub{font-size:1.4vw;color:#a5bfac}.grid{display:flex;gap:1.5vw;margin-top:2.4vw}.panel{background:#101d16;border:1px solid #284c35;border-radius:1.2vw;padding:2vw}.details{flex:1}.codes{flex:1.15;display:flex;gap:1.4vw;justify-content:space-around}.label{font-size:1vw;letter-spacing:.17em;color:#92cba0;margin-top:1.4vw}.value{font-size:1.6vw;margin-top:.45vw;line-height:1.4}.code{flex:1;text-align:center}.code img{display:block;width:100%;max-width:19vw;margin:1vw auto;background:white}.code p{font-size:1vw;color:#b0c5b5}.footer{color:#779483;font-size:1vw;margin-top:2vw}
</style></head><body><div class="brand">MSB / CALLBACK</div><div class="head">TV Repair <span style="color:#9dffad">by Meryosab</span></div>
<div class="sub">Ремонт телевизоров, мониторов и электроники · Ашхабад</div><div class="grid"><div class="panel details"><div class="label">ТЕЛЕФОН / WHATSAPP</div><div class="value">+993 63 70 59 53</div><div class="label">E-MAIL</div><div class="value">tvrepairmsb@gmail.com</div><div class="label">АДРЕС</div><div class="value">Ашхабад, Парахат 3/2, дом 14</div><div class="label">САЙТ И TELEGRAM</div><div class="value">tvrepair.meryosab.com<br>@meryosab_shop</div></div>
<div class="panel codes"><div class="code"><div class="label">САЙТ TV REPAIR</div><img src="website.png" alt="QR сайта"><p>Сканируйте, чтобы открыть сайт</p></div><div class="code"><div class="label">КОНТАКТЫ</div><img src="contact.png" alt="QR контактной карточки"><p>Сканируйте и подтвердите сохранение контакта</p></div></div></div><div class="footer">Контакты доступны без интернета · Приём техники по предварительной связи</div>
<script>document.addEventListener('keydown',function(e){if(e.keyCode===10009||e.keyCode===27||e.keyCode===8){try{if(window.tizen)tizen.application.getCurrentApplication().exit();else if(window.PalmSystem)window.close();}catch(x){}}});</script></body></html>'''


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
<feature name="http://tizen.org/feature/screen.size.all"/><tizen:profile name="tv-samsung"/><tizen:setting screen-orientation="landscape" context-menu="disable" background-support="disable" hwkey-event="enable"/>
</widget>''' % (SAMSUNG_APP_ID, SAMSUNG_PKG_ID)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('config.xml', config.encode('utf-8'))
        for name, data in _files().items():
            z.writestr(name, data)
    return out.getvalue()
