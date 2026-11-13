// Controlled fetch/DOM with production app.js. No real browser or public HTTP.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync('archive/static/archive/app.js', 'utf8');
function element(fields = {}) {
  const listeners = new Map();
  return {dataset:{}, textContent:'', children:[], isConnected:true, disabled:false, ...fields,
    addEventListener(type, fn) { listeners.set(type, fn); },
    emit(type) { return listeners.get(type)?.(); },
    setAttribute() {}, removeAttribute() {},
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
    querySelector() { return null; }, querySelectorAll() { return []; }, ...fields};
}
function fixture(config, responses, options = {}) {
  const status = element(), output = element(), retry = element(), cancel = element();
  const slots = {'[data-guide-online-status]':status, '[data-guide-online-result]':output,
    '[data-guide-online-retry]':retry, '[data-guide-online-cancel]':cancel};
  const region = element({dataset:{configId:'guide-online-config'}, querySelector:s => slots[s] || null});
  const document = element({visibilityState:'visible', createElement:() => element(),
    getElementById:() => ({textContent:JSON.stringify(config)}),
    querySelectorAll:s => s === '[data-guide-online]' ? [region] : []});
  const calls = [];
  let now = 0;
  const window = element({location:{origin:'http://testserver',href:'http://testserver/guide/'}});
  const context = vm.createContext({window, document, URL, URLSearchParams, AbortController, AbortSignal,
    Uint8Array, TextDecoder, Date, performance:{now:() => now},
    setInterval:() => 0, setTimeout, clearTimeout,
    fetch:async (url, request) => {
      calls.push({url, request});
      const item = responses.shift();
      if (item instanceof Error) throw item;
      if (typeof item === 'function') return item(url, request);
      if (item === undefined) throw new Error('unexpected_fetch');
      return new Response(JSON.stringify(item));
    }});
  vm.runInContext(source, context);
  return {context, region, status, output, retry, cancel, calls,
    text:() => { const text = node => node.textContent + node.children.map(text).join(''); return text(output); },
    advance:amount => { now += amount; },
    settled:async () => { for (let i=0; i<20 && retry.disabled; i++) await new Promise(resolve => setTimeout(resolve, 1)); }};
}
const config = {species_id:'public-dom-id', name:'北美红雀', scientific_name:'Cardinalis cardinalis',
  qid:'Q726389', scope:'verified range', concept_key:'verified concept', mapping_url:'https://avibase.bsc-eoc.org/species.jsp'};
function wd(extra = {}) { return {entities:{Q726389:{id:'Q726389', claims:{P225:[{rank:'normal',mainsnak:{datavalue:{value:'Cardinalis cardinalis'}}}]},
  sitelinks:{zhwiki:{title:'北美紅雀'}}, ...extra}}}; }
function page(extra = {}) { return {query:{pages:[{pageid:123,title:'北美紅雀',pageprops:{wikibase_item:'Q726389'},
  extract:'北美红雀受控摘要 <script>remote()</script>', revisions:[{revid:456,timestamp:'2026-10-01T00:00:00Z'}], ...extra}]}}; }
async function tracer(input) {
  const f = fixture(input, [wd(), page()]); await f.settled();
  assert.equal(f.calls.length, 2); assert.equal(f.status.textContent, '已取得中文摘要。');
  assert.match(f.text(), /北美红雀受控摘要/); assert.match(f.text(), /维基百科贡献者/);
  assert.match(f.text(), /CC BY-SA 4.0/); assert.match(f.text(), /原站阅读/);
  assert.match(f.text(), /<script>remote/); // Literal textContent, never parsed HTML.
  assert.equal(f.output.children.filter(child => child.href?.includes('curid=123')).length, 1);
  for (const {url, request} of f.calls) {
    const parsed = new URL(url); assert.equal(parsed.searchParams.get('origin'), '*');
    assert.equal(request.credentials, 'omit'); assert.equal(request.referrerPolicy, 'no-referrer');
    assert.equal(request.mode, 'cors'); assert.equal(request.redirect, 'error'); assert.equal(request.cache, 'no-store');
    assert.equal(request.headers, undefined); assert.equal(parsed.searchParams.has('species_id'), false);
  }
}
if (process.argv.includes('--slow')) {
  let release;
  const f = fixture(config, [() => new Promise(resolve => { release = resolve; })]);
  console.log('BROWSER FETCH PENDING');
  process.stdin.once('data', async () => {
    f.cancel.emit('click'); release(new Response(JSON.stringify(wd())));
    await f.settled(); assert.equal(f.text(), ''); console.log('CANCELLED SAFELY');
  });
} else if (process.argv.includes('--tracer')) {
  tracer(JSON.parse(fs.readFileSync(0, 'utf8'))).then(() => console.log('TRACER PASS')).catch(error => {console.error(error);process.exitCode=1;});
} else {
  test('bounded transport and safe production DOM', () => tracer(config));
  test('empty, foreign, disambiguation key and QID mismatch terminate honestly', async () => {
    for (const [responses, expected] of [
      [[wd(),page({extract:''})], '中文摘要暂缺'],
      [[wd({sitelinks:{enwiki:{title:'Northern cardinal'}}}),page()], '仅有外文来源'],
      [[wd(),page({pageprops:{wikibase_item:'Q726389',disambiguation:''}})], '消歧'],
      [[wd(),page({pageprops:{wikibase_item:'Q1'}})], '资料待匹配'],
      [[wd(),{error:{code:'bad'}}], '检索失败']]) {
      const f = fixture(config,responses); await f.settled(); assert.match(f.status.textContent,new RegExp(expected));
      if (expected === '仅有外文来源') assert.doesNotMatch(f.text(), /受控摘要/);
    }
  });
  test('unknown names search without binding and empty search has a final state', async () => {
    for (const rows of [[],[{pageid:3,title:'同名词'}]]) {
      const f = fixture({...config,qid:null},[{query:{search:rows}}]); await f.settled();
      assert.equal(f.calls.length,1); assert.equal(new URL(f.calls[0].url).searchParams.get('srprop'),'');
      assert.match(f.status.textContent, rows.length ? /资料待匹配/ : /未找到/);
      assert.doesNotMatch(f.text(), /受控摘要/);
    }
  });
  test('stream limit enforced before JSON parse and server error cooldown prevents retry', async () => {
    const large = fixture(config,[() => new Response('x'.repeat(262145))]); await large.settled();
    assert.match(large.status.textContent,/检索失败/);
    const f = fixture(config,[() => new Response('', {status:429})]); await f.settled();
    await f.retry.emit('click'); assert.equal(f.calls.length,1); assert.match(f.status.textContent,/冷却/);
  });
  test('same-species promise merges, cached result reuses, and failed retry preserves text', async () => {
    const f = fixture(config,[wd(),page(),new Error('offline')]);
    const again = vm.runInContext('guideLoad',f.context)(f.region);
    await again; await f.settled(); assert.equal(f.calls.length,2);
    await vm.runInContext('guideLoad',f.context)(f.region); assert.equal(f.calls.length,2);
    await f.retry.emit('click'); assert.equal(f.calls.length,3); assert.match(f.text(),/受控摘要/);
  });
  test('cancelled late response cannot render or poison the cache', async () => {
    let release;
    const f = fixture(config,[() => new Promise(resolve => {release = resolve;})]);
    f.cancel.emit('click'); release(new Response(JSON.stringify(wd()))); await f.settled();
    assert.match(f.status.textContent,/已取消/); assert.equal(f.text(),''); assert.equal(f.calls.length,1);
  });
  test('monotonic TTL expires and page cache retains at most 32 species', async () => {
    const f = fixture(config,[wd(),page(),wd(),page()]); await f.settled();
    f.advance(60001); await vm.runInContext('guideLoad',f.context)(f.region);
    assert.equal(f.calls.length,4);
    for (let index=0; index<34; index++) {
      f.context.nextConfig = {...config,species_id:'public-'+index,qid:null};
      f.context.document.getElementById = () => ({textContent:JSON.stringify(f.context.nextConfig)});
      // Search returns an empty, final response; no identity is promoted.
      f.context.fetch = async () => new Response(JSON.stringify({query:{search:[]}}));
      await vm.runInContext('guideLoad',f.context)(f.region,true);
    }
    assert.equal(vm.runInContext('guideOnlineCache.size',f.context),32);
  });
  test('total monotonic budget and manual cancellation reject delayed content', async () => {
    const f = fixture(config,[() => { f.advance(10001); return new Response(JSON.stringify(wd())); }]);
    await f.settled(); assert.match(f.status.textContent,/检索失败/); assert.equal(f.calls.length,1);
    assert.equal(f.text(),'');
    const empty = fixture({...config,name:'',scientific_name:''},[]); await empty.settled();
    assert.match(empty.status.textContent,/缺少公开鸟名/); assert.equal(empty.calls.length,0);
  });
  test('readable Retry-After blocks the provider across species and API maxlag has no automatic retry', async () => {
    const f = fixture(config,[() => new Response('',{status:503,headers:{'Retry-After':'120'}})]); await f.settled();
    f.advance(60001); await f.retry.emit('click'); assert.equal(f.calls.length,1);
    f.context.document.getElementById = () => ({textContent:JSON.stringify({...config,species_id:'other-public-species'})});
    await f.retry.emit('click'); assert.equal(f.calls.length,1);
    f.advance(60000); await f.retry.emit('click'); assert.equal(f.calls.length,2);
    const maxlag = fixture(config,[{error:{code:'maxlag'}}]); await maxlag.settled();
    await maxlag.retry.emit('click'); assert.equal(maxlag.calls.length,1); assert.match(maxlag.status.textContent,/冷却/);
  });
  test('superseded region and conflicting P225 do not show an incorrect species summary', async () => {
    let release;
    const f = fixture(config,[() => new Promise(resolve => {release = resolve;})]);
    f.region.guideSequence++; f.region.isConnected = false;
    release(new Response(JSON.stringify(wd()))); await f.settled(); assert.equal(f.text(),'');
    const conflict = fixture(config,[wd({claims:{P225:[{mainsnak:{datavalue:{value:'Other species'}}}]}})]);
    await conflict.settled(); assert.match(conflict.status.textContent,/资料待匹配/); assert.equal(conflict.calls.length,1);
  });
}
if (!process.argv.includes('--tracer') && !process.argv.includes('--slow')) {
  test('online boundaries: malformed API envelopes and incomplete identity never become prose', async () => {
    for (const replies of [
      [{success:0,...wd()},page()], [{warnings:{claims:'incomplete'},...wd()},page()], [{entities:{}}],
      [wd({missing:''}),page()], [wd({invalid:''}),page()], [wd({claims:{}})], [wd(),{query:{pages:[{missing:true}]}}],
      [wd(),{query:{pages:[{invalid:true}]}}], [wd(),page({pageprops:{}})],
      [wd(),{warnings:{extracts:'disabled'}}], [wd(),{error:{code:'badvalue'}}]
    ]) {
      const f = fixture(config,replies); await f.settled();
      assert.equal(f.retry.disabled,false); assert.doesNotMatch(f.text(),/受控摘要/);
      assert.doesNotMatch(f.status.textContent,/正在检索|已取得/);
    }
  });
  test('online boundaries: verified range is required and requests contain public whitelist only', async () => {
    for (const field of ['scope','concept_key','mapping_url']) {
      const f = fixture({...config,[field]:''},[{query:{search:[{pageid:3,title:'<img onerror=remote()>'}]}}]);
      await f.settled(); assert.equal(f.calls.length,1);
      const url = new URL(f.calls[0].url);
      assert.equal(url.origin,'https://zh.wikipedia.org'); assert.equal(url.pathname,'/w/api.php');
      assert.deepEqual([...url.searchParams.keys()].sort(), ['action','format','formatversion','list','origin','srlimit','srnamespace','srprop','srsearch'].sort());
      assert.equal(url.searchParams.get('srsearch'),config.scientific_name);
      assert.match(f.status.textContent,/资料待匹配/); assert.match(f.text(),/<img onerror/);
      assert.equal(f.calls[0].request.headers,undefined);
    }
  });
  test('online boundaries: oversized decoded stream is cancelled before parsing', async () => {
    let cancelled = false;
    const f = fixture(config,[() => ({ok:true,status:200,body:{getReader:() => ({
      read:async () => ({done:false,value:new Uint8Array(262145)}),
      cancel:async () => {cancelled=true;}, releaseLock() {}
    })}})]);
    await f.settled(); assert.equal(cancelled,true); assert.equal(f.calls.length,1);
    assert.equal(f.retry.disabled,false); assert.match(f.status.textContent,/检索失败/);
  });
}
module.exports = {fixture, config, wd, page};
