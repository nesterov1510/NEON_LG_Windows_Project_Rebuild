"""Behavioral tests for the generated TV app, using upstream EPG fixtures."""
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from alemtv_app import build_alemtv_js


def test_samsung_remote_epg_and_archive_using_upstream_fixtures():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is not available')
    root = Path(__file__).resolve().parent.parent
    with zipfile.ZipFile(root / 'alemtv-master.zip') as archive:
        base = 'alemtv-master/Packages/AlemCore/Tests/AlemCoreTests/Fixtures/'
        day = json.loads(archive.read(base + 'get_epg_day.json'))['result']
        now = json.loads(archive.read(base + 'get_epg_now.json'))['result']
    runner = r'''
const fixtureDay = __DAY__, fixtureNow = __NOW__;
const RealDate = Date;
Date.now = () => RealDate.parse('2026-10-03T16:53:38Z');
const timers = [];
global.setTimeout = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
global.clearTimeout = () => {};
global.setInterval = () => 1;
global.clearInterval = () => {};
const listeners = {};
global.window = global;
window.addEventListener = (type, fn) => { listeners[type] = fn; };
const elements = {};
function element(id, tag = 'DIV') {
  let html = '';
  const e = { id, tagName: tag, style: {}, className: '', dataset: {}, children: [],
    textContent: '', value: '', offsetParent: {}, selectedIndex: 0, options: [],
    classList: { add(){}, remove(){} },
    focus(){ document.activeElement = this; },
    appendChild(child){ this.children.push(child); return child; },
    querySelector(){ return element('', 'SPAN'); },
    querySelectorAll(){ return this.children; },
    getBoundingClientRect(){ let n = this._index || 0; return {left:n*120, top:0, width:100, height:50}; },
    scrollIntoView(){}, dispatchEvent(){},
    click(){ if (this.onclick) this.onclick.call(this, {}); },
    pause(){ this.paused = true; }, play(){ this.paused = false; return Promise.resolve(); }, load(){},
    paused: true, currentTime: 0, duration: 0, src: ''
  };
  Object.defineProperty(e, 'innerHTML', {get(){return html;}, set(v){html=v; this.children=[];}});
  return e;
}
global.document = {readyState:'loading', activeElement:null, addEventListener(){},
  getElementById(id){return elements[id] || (elements[id]=element(id));},
  createElement(tag){return element('', tag.toUpperCase());}, querySelectorAll(){return [];}};
['loginModal','epgModal','settingsModal','zapDrawer','portalIframe','playerError'].forEach(id => document.getElementById(id).style.display = 'none');
global.localStorage = { getItem(){return null;}, setItem(){}, removeItem(){} };
const requests=[];
global.XMLHttpRequest = class {
  open(method, url){this.url=url;}
  setRequestHeader(){}
  send(body){
    const req=JSON.parse(body); requests.push(req);
    this.status=200;
    this.responseText=JSON.stringify({result:Array.isArray(req.params.media_id) ? fixtureNow : fixtureDay});
    this.onload();
  }
};
__JS__
function key(code, name, target) {
  let prevented=false;
  listeners.keydown({keyCode:code, key:name, target:target || document.activeElement,
    preventDefault(){prevented=true;}});
  return prevented;
}
const ch={id:33,index:1,title:'Test',stream_url:'http://h/live.m3u8',
  timeshift_url:'http://h/archive/index.m3u8',timeshift_archive_length:259200,timeshift_offset:0};
window.handleAuthSuccess({channels:[ch], services:[{id:1,name:'timeshift'}], services_enabled:{'1':true}});
if (!document.getElementById('channelGrid').children.length) throw Error('channels did not render');
// The upstream bulk fixture is wrapped as {programs: ...}, and must render current titles.
if (!document.getElementById('channelGrid').children[0].innerHTML.includes('Новости')) throw Error('current EPG missing');
window.playChannel(ch);
const osd=document.getElementById('playerOsd');
const one=element('one','BUTTON'), two=element('two','BUTTON'); one._index=0; two._index=1;
let activated=0; two.onclick=()=>activated++; osd.children=[one,two];
key(13,'Enter'); // Show the OSD if hidden, or activate its first button.
key(39,'ArrowRight');
key(13,'Enter');
if (activated !== 1) throw Error('D-pad cannot reach OSD control');
const video=document.getElementById('videoElement');
key(19, 'MediaPause');
if (!video.paused) throw Error('Samsung pause key did not pause');
key(415, 'MediaPlay');
if (video.paused) throw Error('Samsung play key did not resume');
const input=element('loginUsername','INPUT');
document.getElementById('loginModal').style.display='flex';
if (key(49,'1',input)) throw Error('numeric input intercepted by channel tuning');
document.getElementById('loginModal').style.display='none';
window.openCurrentEpg();
const list=document.getElementById('epgListContainer');
if (list.children.length < 2) throw Error('upstream EPG day response not parsed');
const archived=list.children.find(item => item.innerHTML.includes('Запись'));
if (!archived || !archived.onclick) throw Error('archive program is not selectable');
archived.click();
if (document.getElementById('epgModal').style.display !== 'none') throw Error('EPG not closed on playback');
const state=window.getPlayerState();
if (state.mode !== 'archive') throw Error('archive mode was not selected: '+state.mode);
const url=document.getElementById('videoElement').src;
const dur=Math.floor((state.archive.stop-state.archive.start)/1000);
if (!url.includes('/index-'+Math.floor(state.archive.start/1000)+'-'+dur+'.m3u8')) throw Error('incorrect archive URL: '+url);
// Recent recordings use the rolling timeshift URL, matching the reference player.
const t=Date.now();
window.playArchiveProgram({start:t-1800000, stop:t-300000, title:'recent'});
if (window.getPlayerState().mode !== 'timeshift' || !video.src.includes('video-timeshift_rel-')) throw Error('recent recording must use timeshift');
const lastSrc=video.src;
window.playArchiveProgram({start:t+60000, stop:t+120000, title:'future'});
if (video.src !== lastSrc) throw Error('future show incorrectly playable');
// Day buttons use relative offsets, including across month boundaries.
window.openCurrentEpg();
const nav=document.getElementById('epgDayNav');
const prev=nav.children.find(x => x.dataset.offset === -3);
if (!prev || !prev.textContent) throw Error('missing labelled previous day');
prev.click();
const last=requests[requests.length-1];
if (last.params.start !== '2026-09-30 00:00:00') throw Error('incorrect cross-month day: '+last.params.start);
'''.replace('__JS__', build_alemtv_js()).replace('__DAY__', json.dumps(day, ensure_ascii=False)).replace('__NOW__', json.dumps(now, ensure_ascii=False))
    result = subprocess.run([node], input=runner, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
