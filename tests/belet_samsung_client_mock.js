const fs = require('fs'), vm = require('vm'), assert = require('assert');
const files = process.argv[2];
let store = {}, calls = [], authMode = 'normal', listeners = {}, doc, timers = [], timerId = 0;
function Element(tag) {
  this.style = {}; this.tagName = (tag || 'div').toUpperCase(); this.children = []; this.value = ''; this.textContent = ''; this.disabled = false;
  this.offsetWidth = 20; this.offsetHeight = 20;
  const set = new Set(); this.classList = {add(s){set.add(s)},remove(s){set.delete(s)},contains(s){return set.has(s)},toggle(s,on){if(on)set.add(s);else set.delete(s)}};
  this.appendChild = e => { this.children.push(e); if(this.tagName==='SELECT' && this.children.length===1)this.value=e.value; };
  this.querySelector = () => null;
  this.querySelectorAll = () => [];
  this.focus = () => { doc.activeElement = this; };
  this.removeAttribute = () => {};
  this.paused = false; this.currentTime = 20; this.duration = 100; this.pause = () => { this.paused = true; }; this.load = () => {}; this.play = () => { this.paused = false; return {catch(){}}; }; this.click = () => { if (this.onclick) this.onclick(); };
  Object.defineProperty(this,'options',{get:()=>this.children});
}
let ids = {};
for(const id of ['login','home','player','logout','status','sessionNote','phone','otp','send','codeSection','code','verify','rows','video','playerTitle','back','openQr','qrSection','phoneSection','qrImage','qrStatus','refreshQr','closeQr']) ids[id] = new Element(id==='otp'?'select':'div');
doc={getElementById:id=>ids[id],createElement:tag=>new Element(tag),addEventListener:(type,fn)=>{listeners[type]=fn},activeElement:null};
let responses=[];
class XHR {
  constructor(){this.headers={};this.readyState=0;this.status=0}
  open(method,url){this.method=method;this.url=url}
  setRequestHeader(key,value){this.headers[key]=value}
  send(body){
    calls.push({method:this.method,url:this.url,body:body?JSON.parse(body):null,headers:this.headers});
    let r=responses.shift(); assert(r,'unexpected request '+this.url); assert(this.url.indexOf(r.url)>=0,this.url+' expected '+r.url);
    this.status=r.status; this.responseText=JSON.stringify(r.json); this.readyState=4; this.onreadystatechange();
  }
}
function queue(url,status,json){responses.push({url,status,json})}
const ctx={document:doc, XMLHttpRequest:XHR, localStorage:{getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=v},removeItem:k=>{delete store[k]}},
  window:{crypto:{getRandomValues(a){a.fill(7);return a}},confirm:()=>true}, Uint8Array, setTimeout:(fn,delay)=>{ if(delay===30){fn();return 0;} const id=++timerId; timers.push({id,fn}); return id; }, clearTimeout:id=>{timers=timers.filter(t=>t.id!==id);}, console, Number, Array, JSON, String, Math, encodeURIComponent};
queue('/available-otp',200,{otp:[{name:'SMS',type:2}]});
vm.runInNewContext(fs.readFileSync(files,'utf8'),ctx);
assert.strictEqual(ids.otp.value,'2');
ids.phone.value='99361234567';
queue('/auth/sign-in?sign_in_type=2',200,{msg:'ok',token:'temporary'}); ids.send.onclick();
assert.deepStrictEqual(calls[1].body,{phone:99361234567}); assert(!ids.codeSection.classList.contains('hidden'));
ids.code.value='12345';queue('/check-code-mobile',200,{token:'access',refresh_token:'refresh'});
queue('/api/v1/home_page',200,{result:[{title:'Cinema',movies:[{id:42,type_id:1,title:'Movie',images:{vertical:{medium:{url:'https://example.com/poster.jpg'}}}}]}]});
ids.verify.onclick(); assert.strictEqual(calls[2].body.code,'12345'); assert.strictEqual(calls[3].headers.Authorization,'access');
assert.strictEqual(ids.rows.children.length,2); assert.strictEqual(ids.rows.children[1].children[0].children[0].children[0].src,'https://example.com/poster.jpg'); let saved=JSON.parse(store.neon_belet_samsung_v1);assert.strictEqual(saved.token,'access');
queue('/api/v3/files/42?type=1',200,{sources:[{filename:'https://cdn.example.com/movie.m3u8'}]}); ids.rows.children[1].children[0].onclick();
assert.strictEqual(ids.video.src,'https://cdn.example.com/movie.m3u8');assert(!ids.player.classList.contains('hidden'));
function key(k){let prevented=false; listeners.keydown({keyCode:k,preventDefault(){prevented=true}}); return prevented;}
assert(key(19)); assert(ids.video.paused); assert(key(415)); assert(!ids.video.paused); assert(key(37)); assert.strictEqual(ids.video.currentTime,10); assert(key(10009)); assert(!ids.home.classList.contains('hidden')); assert.strictEqual(doc.activeElement,ids.rows.children[1].children[0]);
// A 401 refreshes once, persists the replacement token, then retries the same request.
queue('/api/v3/files/42?type=1',401,{});queue('/api/v2/auth/refresh-mobile',200,{token:'access2',refresh_token:'refresh2'});
queue('/api/v3/files/42?type=1',200,{sources:[{filename:'https://cdn.example.com/second.m3u8'}]});
ids.rows.children[1].children[0].onclick();
assert.strictEqual(calls[calls.length-2].body.refresh_token,'refresh');
assert.strictEqual(calls[calls.length-1].headers.Authorization,'access2');
assert.strictEqual(JSON.parse(store.neon_belet_samsung_v1).refresh_token,'refresh2');
ids.back.onclick();
queue('/api/v3/files/42?type=1',402,{}); ids.rows.children[1].children[0].onclick(); assert(ids.status.textContent.indexOf('HTTP 402') >= 0); assert(!ids.home.classList.contains('hidden'));
queue('/api/v1/auth/log-out',200,{});queue('/available-otp',200,{otp:[{name:'SMS',type:2}]});ids.logout.onclick();
assert(!store.neon_belet_samsung_v1);assert.strictEqual(calls[calls.length-2].headers.Authorization,'access2');
assert.strictEqual(responses.length,0);
// Expired session is removed only on server-confirmed 401; the OTP selector is restored.
ids.phone.value='99361234567';queue('/auth/sign-in?sign_in_type=2',200,{token:'temporary2'});ids.send.onclick();
ids.code.value='98765';queue('/check-code-mobile',200,{token:'access3',refresh_token:'refresh3'});
queue('/api/v1/home_page',200,{result:[{title:'Cinema',movies:[{id:42,type_id:1,title:'Movie'}]}]});ids.verify.onclick();
queue('/api/v3/files/42?type=1',401,{});queue('/api/v2/auth/refresh-mobile',401,{});queue('/available-otp',200,{otp:[{name:'SMS',type:2}]});
ids.rows.children[1].children[0].onclick();
assert(!store.neon_belet_samsung_v1);assert(!ids.login.classList.contains('hidden'));assert.strictEqual(responses.length,0);

// QR is fetched from Belet; pending and cancelled attempts never persist credentials.
queue('/auth/get-qrcode',200,{code:'https://film.belet.me/register/mock-id-123',qr_png:'iVBORw0KGgoAAA='}); ids.openQr.onclick();
assert(ids.qrImage.src.startsWith('data:image/png;base64,'));
queue('/auth/check-qrcode-web',400,{msg:'waiting'}); timers.shift().fn();
assert.deepStrictEqual(calls[calls.length-1].body,{code:'https://film.belet.me/register/mock-id-123',fingerprint:'07070707070707070707070707070707'});
assert(!store.neon_belet_samsung_v1); assert.strictEqual(timers.length,1);
queue('/auth/check-qrcode-web',200,{token:'qr-access',refresh_token:'qr-refresh'});
queue('/api/v1/home_page',200,{result:[]}); timers.shift().fn();
assert.strictEqual(JSON.parse(store.neon_belet_samsung_v1).token,'qr-access'); assert(!ids.home.classList.contains('hidden')); assert.strictEqual(timers.length,0);
queue('/api/v1/auth/log-out',200,{});queue('/available-otp',200,{otp:[{name:'SMS',type:2}]});ids.logout.onclick();
queue('/auth/get-qrcode',200,{code:'https://film.belet.me/register/mock-other',qr_png:'iVBORw0KGgoAAA='});ids.openQr.onclick();
assert.strictEqual(timers.length,1);ids.closeQr.onclick();assert.strictEqual(timers.length,0);assert(!store.neon_belet_samsung_v1);
queue('/auth/get-qrcode',200,{code:'https://malicious.example/register/id',qr_png:'iVBORw0KGgoAAA='});ids.openQr.onclick();assert.strictEqual(timers.length,0);assert(ids.qrStatus.textContent.indexOf('Не удалось')>=0);
