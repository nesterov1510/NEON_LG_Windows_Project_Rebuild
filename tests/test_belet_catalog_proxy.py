import json
import shutil
import subprocess

import pytest
from samsung_tizen import _build_belet_nav_js, _build_belet_proxy_js


def test_catalog_proxy_routing_and_cookie_isolation(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js not installed')
    nav = _build_belet_nav_js()
    proxy = _build_belet_proxy_js()
    runner = r'''
const assert = require('assert');
const http = require('http');
const vm = require('vm');
const nav = __NAV__;
const proxy = __PROXY__;
const fn = nav.slice(nav.indexOf('function rewriteForProxy('), nav.indexOf('function resolveForTarget('));
const scope = {IS_PROXY: true, TARGET_HOST: 'belet.tm', location: {origin:'http://127.0.0.1:18765'}, isTargetHost: h => h==='belet.tm' || h.endsWith('.belet.tm')};
vm.runInNewContext(fn, scope);
assert.strictEqual(scope.rewriteForProxy('https://homepage.belet.me/api/v2/home_page?limit=12'), 'http://127.0.0.1:18765/__neon_host/homepage.belet.me/api/v2/home_page?limit=12');
assert.strictEqual(scope.rewriteForProxy('https://homepage.belet.me.evil.test/a'), 'https://homepage.belet.me.evil.test/a');
assert.strictEqual(scope.rewriteForProxy('http://homepage.belet.me/a'), 'http://homepage.belet.me/a');
const seen=[];
const upstream=http.createServer((req,res)=>{seen.push({url:req.url,cookie:req.headers.cookie||'',auth:req.headers.authorization||''}); res.writeHead(200,{'Content-Type':'application/json','Set-Cookie':'catalog_session=ok; Path=/; HttpOnly'});res.end('{"status":"ok"}');});
function get(port,path,headers){return new Promise((resolve,reject)=>{http.get({host:'127.0.0.1',port,path,headers},res=>{let body='';res.on('data',b=>body+=b);res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body}));}).on('error',reject);});}
upstream.listen(0,'127.0.0.1',async()=>{
  let srv;
  try {
    const tlsOpts=[];
    const fakeHttps={Agent:http.Agent,request(opts,cb){tlsOpts.push({hostname:opts.hostname,rejectUnauthorized:opts.rejectUnauthorized}); return http.request({hostname:'127.0.0.1',port:upstream.address().port,path:opts.path,method:opts.method,headers:opts.headers,agent:false},cb)}};
    const req=name=>name==='https'?fakeHttps:require(name);
    const mod={exports:{}};
    const context={require:req,module:mod,exports:mod.exports,Buffer,console,process,setTimeout,clearTimeout};
    vm.runInNewContext(proxy.replace(/var PORT=\d+;/,'var PORT=0;'),context);
    srv=mod.exports.server; if(!srv.listening)await new Promise(r=>srv.once('listening',r));
    const port=srv.address().port;
    const path='/__neon_host/homepage.belet.me/api/v2/home_page?limit=12';
    const a=await get(port,path,{'Cookie':'private_belet=value','Authorization':'Bearer sample'});
    assert.strictEqual(a.status,200); assert.strictEqual(a.headers['set-cookie'],undefined);
    assert.strictEqual(seen[0].cookie,''); assert.strictEqual(seen[0].auth,'Bearer sample');
    assert.strictEqual(tlsOpts[0].hostname,'homepage.belet.me'); assert.strictEqual(tlsOpts[0].rejectUnauthorized,true);
    const b=await get(port,path,{'Cookie':'private_belet=value'});
    assert.strictEqual(b.status,200); assert.strictEqual(seen[1].cookie,'catalog_session=ok');
    const c=await get(port,'/__neon_host/homepage.belet.me.evil.test/a',{});
    assert.strictEqual(c.status,403); assert.strictEqual(seen.length,2);
    srv.close();upstream.close();
  } catch(e) { console.error(e);if(srv)srv.close();upstream.close();process.exitCode=1; }
});
'''.replace('__NAV__', json.dumps(nav)).replace('__PROXY__', json.dumps(proxy))
    script = tmp_path / 'test.js'
    script.write_text(runner, encoding='utf-8')
    run = subprocess.run([node, str(script)], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert run.returncode == 0, run.stderr
