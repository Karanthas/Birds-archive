// Native Node events/storage/FormData in a VM DOM fixture; not real browser evidence.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const KEY = 'birds.review.selection.v1';
const id = n => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`;
const source = fs.readFileSync('archive/static/archive/app.js', 'utf8');
function storage(initial = null) {
  return {value: initial, getItem(key) { assert.equal(key, KEY); return this.value; },
    setItem(key, value) { assert.equal(key, KEY); this.value = value; },
    removeItem(key) { assert.equal(key, KEY); this.value = null; }};
}
function node(fields = {}) {
  const listeners = new Map();
  return {dataset:{}, hidden:false, disabled:false, textContent:'', ...fields,
    addEventListener(type, listener) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(listener); },
    async emit(type, event = {}) { for (const listener of listeners.get(type) || []) await listener(event); },
    setAttribute() {}, removeAttribute() {}};
}
function fixture(ids = [id(1), id(2)], store = storage()) {
  const count = node(), message = node(), tools = node(), all = node({checked:false}), clear = node();
  const boxes = ids.map(value => node({name:'photo_ids', value, type:'checkbox', checked:false}));
  const fields = [node({name:'mode', value:'own'}), node({name:'page_snapshot',value:'signed-current-page'})];
  function button(mode) {
    return node({name:'csrfmiddlewaretoken', value:'csrf-only-post', dataset:{reviewSpecies:mode},
      getAttribute:key => key === 'formmethod' ? 'post' : key === 'formaction' ? `/review/batch/preview/?mode=${mode}` : null});
  }
  const own = button('own'), uniform = button('uniform_species');
  const location = node({getAttribute:() => null});
  const form = node({dataset:{reviewSelection:''}, method:'get', action:'http://testserver/location/',
    elements:[...fields,...boxes,own,uniform,location], reportValidity:() => true,
    hasAttribute:key => ['data-selection','data-review-selection'].includes(key),
    querySelectorAll:selector => selector.includes('photo_ids') ? boxes.filter(box => !selector.includes(':checked') || box.checked) : [],
    querySelector:selector => selector.includes('photo_ids') ? boxes.find(box => !selector.includes(':checked') || box.checked) || null :
      ({'[data-selection-count]':count, '[data-selection-tools]':tools,
        '[data-selection-message]':message, '[data-selection-all]':all,
        '[data-selection-clear]':clear})[selector] || null});
  class FormData extends global.FormData {
    constructor(target) {
      super();
      for (const field of target.elements) {
        if (field.name && !field.disabled && ![own,uniform,location].includes(field) &&
            (field.type !== 'checkbox' || field.checked)) this.append(field.name,field.value);
      }
      // Browser fires synchronously while building its successful-control entry list.
      target.emit('formdata', {formData:this});
    }
  }
  const window = node({sessionStorage:store, location:{origin:'http://testserver',href:'http://testserver/review/'}});
  const document = node({visibilityState:'visible', activeElement:boxes[0],
    querySelector:selector => selector === '[data-selection]' || selector === '[data-review-selection]' ? form : null,
    querySelectorAll:selector => selector === 'form' || selector === '[data-review-selection]' ? [form] :
      selector.includes('photo_ids') ? boxes : []});
  let sends = 0;
  const context = vm.createContext({document, window, FormData, URL, URLSearchParams, AbortController, AbortSignal,
    setInterval:() => {}, setTimeout:() => 1, clearTimeout:() => {}, fetch:() => { sends++; throw new Error('unexpected_fetch'); }});
  vm.runInContext(source, context);
  const change = async (box, checked) => { box.checked = checked; await box.emit('change', {target:box}); };
  const submit = async submitter => {
    let prevented = false;
    await form.emit('submit', {submitter, preventDefault() { prevented = true; }});
    const body = new FormData(form);
    if (submitter.name && !submitter.disabled) body.append(submitter.name, submitter.value);
    return {prevented, body, submitter};
  };
  return {context,form,boxes,all,clear,count,message,tools,store,own,uniform,location,document,window,change,submit,
    get sends() {return sends;}};
}
test('selection survives two pages, filtering, returning and refreshing as explicit UUIDs', async () => {
  const store = storage();
  let page = fixture([id(1),id(2)],store);
  await page.change(page.boxes[0],true);
  assert.notEqual(store.value,null,'change must persist explicit selection for the next page');
  assert.deepEqual(JSON.parse(store.value).photo_ids,[id(1)]);
  page = fixture([id(3)],store); await page.change(page.boxes[0],true);
  for (const ids of [[id(9)], [id(1),id(2)], [id(1),id(2)]]) {
    page = fixture(ids,store);
    assert.match(page.count.textContent,/已选 2 张/);
    assert.deepEqual(page.boxes.map(box=>box.checked),ids.map(value=>[id(1),id(3)].includes(value)));
  }
  const saved = JSON.parse(store.value);
  assert.deepEqual(Object.keys(saved).sort(),['photo_ids','updated_at','version']);
  assert.equal(saved.version,1); assert.ok(Number.isFinite(Date.parse(saved.updated_at)));
});
test('select all and unselect all change visible IDs only and show partial selection', async () => {
  const store=storage(); let page=fixture([id(1)],store); await page.change(page.boxes[0],true);
  page=fixture([id(2),id(3)],store); await page.change(page.boxes[0],true);
  assert.equal(page.all.indeterminate,true);
  await page.change(page.all,true);
  assert.deepEqual(JSON.parse(store.value).photo_ids,[id(1),id(2),id(3)]);
  await page.change(page.all,false);
  assert.deepEqual(JSON.parse(store.value).photo_ids,[id(1)]);
  assert.match(page.count.textContent,/本页 0 张/); assert.equal(page.tools.hidden,false);
});
test('200 limit rejects whole added page without truncating or losing existing selection', async () => {
  const existing=Array.from({length:199},(_,n)=>id(n+1));
  const store=storage(JSON.stringify({version:1,photo_ids:existing,updated_at:'2026-10-17T00:00:00Z'}));
  const page=fixture([id(200),id(201)],store);
  await page.change(page.all,true);
  assert.deepEqual(JSON.parse(store.value).photo_ids,existing);
  assert.deepEqual(page.boxes.map(box=>box.checked),[false,false]);
  assert.match(page.message.textContent,/200/);
  await page.change(page.boxes[0],true); await page.change(page.boxes[1],true);
  assert.equal(JSON.parse(store.value).photo_ids.length,200);
  assert.equal(page.boxes[1].checked,false);
});
test('both species actions serialize the full selection and retain CSRF successful submitter', async () => {
  for (const mode of ['own','uniform']) {
    const store=storage(); let page=fixture([id(1)],store); await page.change(page.boxes[0],true);
    page=fixture([id(2)],store); await page.change(page.boxes[0],true);
    const result=await page.submit(page[mode]);
    assert.equal(result.prevented,false); assert.equal(result.submitter.disabled,false);
    assert.deepEqual(result.body.getAll('photo_ids'),[id(1),id(2)]);
    assert.equal(result.body.get('csrfmiddlewaretoken'),'csrf-only-post');
    assert.equal(page.sends,0);
    assert.match(page[mode].getAttribute('formaction'),new RegExp(mode==='own'?'mode=own$':'mode=uniform_species$'));
    assert.equal((await page.submit(page[mode])).prevented,true);
  }
});
test('location GET serializes only visible checked IDs and no CSRF', async () => {
  const store=storage(); let page=fixture([id(1)],store); await page.change(page.boxes[0],true);
  page=fixture([id(2)],store); await page.change(page.boxes[0],true);
  const result=await page.submit(page.location);
  assert.deepEqual(result.body.getAll('photo_ids'),[id(2)]);
  assert.equal(result.body.get('csrfmiddlewaretoken'),null);
  assert.equal(result.body.get('page_snapshot'),'signed-current-page');
});
test('off-page selections permit species preview but never an empty current-page location GET', async () => {
  const store=storage(); let page=fixture([id(1)],store); await page.change(page.boxes[0],true);
  page=fixture([id(2)],store); assert.equal((await page.submit(page.location)).prevented,true);
  assert.equal((await page.submit(page.uniform)).prevented,false);
});
test('corrupt schemas, extra data, invalid UUIDs, duplicates and oversized storage are visibly rejected', async () => {
  const good={version:1,photo_ids:[id(1)],updated_at:'2026-10-17T00:00:00Z'};
  for (const bad of ['{', 'null', JSON.stringify({...good,version:2}), JSON.stringify({...good,path:'/private/photo.jpg'}),
    JSON.stringify({...good,photo_ids:['../../photo.jpg']}), JSON.stringify({...good,photo_ids:[id(1),id(1)]}),
    JSON.stringify({...good,photo_ids:Array.from({length:201},(_,n)=>id(n+1))}), JSON.stringify({...good,updated_at:'bad'})]) {
    const page=fixture([id(1)],storage(bad));
    assert.equal(page.boxes[0].checked,false);
    assert.match(page.message.textContent,/损坏|不可用/);
    await page.clear.emit('click',{preventDefault(){}});
    assert.equal(page.store.value,null); assert.match(page.count.textContent,/已选 0 张/);
  }
});
test('denied reads and quota writes degrade visibly while retaining current selection and allow clear', async () => {
  for (const operation of ['getItem','setItem','property']) {
    const store=storage();
    if (operation!=='property') store[operation]=()=>{throw new Error('storage_disabled');};
    const page=fixture([id(1)],store);
    if (operation==='property') {
      Object.defineProperty(page.window,'sessionStorage',{get(){throw new Error('disabled');}});
    }
    await page.change(page.boxes[0],true);
    assert.equal(page.boxes[0].checked,true);
    assert.match(page.message.textContent,/仅在本页|跨页.*不可用/);
    await page.clear.emit('click',{preventDefault(){}}); assert.equal(page.boxes[0].checked,false);
  }
});
test('bfcache pageshow restores latest selection and permits native action again', async () => {
  const store=storage(); const page=fixture([id(1),id(2)],store);
  await page.change(page.boxes[0],true); await page.submit(page.own);
  const other=fixture([id(2)],store); await other.change(other.boxes[0],true);
  await page.window.emit('pageshow',{persisted:true});
  assert.deepEqual(page.boxes.map(box=>box.checked),[true,true]);
  assert.equal((await page.submit(page.uniform)).prevented,false);
});
function recognition(page) {
  const text = () => node();
  const regionFields=Object.fromEntries(['count','last-success','refresh-message'].map(name=>[`[data-recognition-${name}]`,text()]));
  const region={dataset:{recognitionPoll:'/recognition/status/'}, isConnected:true,
    querySelector:selector=>regionFields[selector]};
  const nodes=page.boxes.map((box,index)=>({dataset:{photoId:box.value, metadataVersion:'1',generation:'1',runId:id(500+index),
    activeGeneration:'1',activeRunId:id(500+index)},isConnected:true,
    message:text(),querySelector(selector){return selector==='[data-recognition-message]'?this.message:null;}}));
  const query=page.document.querySelector, queryAll=page.document.querySelectorAll;
  page.document.querySelector=selector=>selector==='[data-recognition-poll]'?region:query(selector);
  page.document.querySelectorAll=selector=>selector==='[data-recognition-status]'?nodes:queryAll(selector);
  const requests=[];
  page.context.fetch=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
  const result=(generation=1)=>({photos:nodes.map((row,index)=>({photo_id:row.dataset.photoId,
    metadata_version:1, generation,run_id:generation===1?id(500+index):id(600+index),
    active_generation:1,active_run_id:id(500+index),state:'succeeded',message:`结果${index}`,
    retry_allowed:false,rerun_allowed:true})).reverse(),
    pending_count:0,review_count:2,checked_at:'2026-10-17T00:00:00Z'});
  const resolve=(request,payload)=>request.resolve({status:200,redirected:false,
    url:'http://testserver/recognition/status/',headers:{get:()=> 'application/json'},json:async()=>payload});
  return {nodes,regionFields,requests,result,resolve};
}
test('slow reordered photo results merge by UUID without changing selection, draft, focus or storage', async () => {
  const page=fixture(); await page.change(page.boxes[0],true);
  const saved=page.store.value; const draft={value:'未保存鸟名'}; page.document.activeElement=draft;
  const poll=recognition(page); const pending=page.context.pollRecognitionStatus();
  assert.equal(page.boxes[0].checked,true);
  poll.resolve(poll.requests[0],poll.result()); await pending;
  assert.deepEqual(poll.nodes.map(row=>row.message.textContent),['结果0','结果1']);
  assert.equal(page.document.activeElement,draft); assert.equal(draft.value,'未保存鸟名');
  assert.equal(page.store.value,saved); assert.equal(page.boxes[0].checked,true);
});
test('superseded and stale run polls cannot roll back counts, drafts or cross-page selections', async () => {
  const page=fixture(); await page.change(page.boxes[0],true); const saved=page.store.value;
  const poll=recognition(page); const old=page.context.pollRecognitionStatus();
  const newer=page.context.pollRecognitionStatus(true);
  poll.resolve(poll.requests[1],poll.result(2)); await newer;
  const counts=poll.regionFields['[data-recognition-count]'].textContent;
  poll.resolve(poll.requests[0],poll.result()); await old;
  assert.deepEqual(poll.nodes.map(row=>row.dataset.generation),['2','2']);
  const stale=page.context.pollRecognitionStatus(true);
  poll.resolve(poll.requests[2],poll.result()); await stale;
  assert.equal(poll.regionFields['[data-recognition-count]'].textContent,counts);
  assert.equal(page.store.value,saved); assert.equal(page.boxes[0].checked,true);
  const mismatch=page.context.pollRecognitionStatus(true); const wrong=poll.result(2); wrong.photos[0].run_id=id(999);
  poll.resolve(poll.requests[3],wrong); await mismatch;
  assert.equal(poll.nodes[1].dataset.runId,id(601)); assert.equal(page.store.value,saved);
});
test('failed status GET retains selection and draft and never sends a POST replay', async () => {
  const page=fixture(); await page.change(page.boxes[0],true); const saved=page.store.value;
  const draft={value:'未保存地点'}; page.document.activeElement=draft;
  const poll=recognition(page); const pending=page.context.pollRecognitionStatus();
  poll.requests[0].reject(new Error('network_lost')); await pending;
  assert.match(poll.regionFields['[data-recognition-refresh-message]'].textContent,/刷新状态/);
  assert.equal(page.store.value,saved); assert.equal(page.document.activeElement,draft);
  assert.equal(draft.value,'未保存地点'); assert.equal(poll.requests.length,1);
  assert.equal(poll.requests[0].options.method,undefined);
});
test('template preserves ordinary no-JS page-only actions and exposes independent uniform POST', () => {
  const html=fs.readFileSync('archive/templates/archive/workbench.html','utf8');
  assert.match(html,/<noscript>.*仅.*本页.*<\/noscript>/s);
  assert.match(html,/data-review-selection/);
  assert.match(html,/data-review-species="uniform_species"[^>]*|[^>]*data-review-species="uniform_species"/);
  assert.match(html,/\?mode=uniform_species/);
  assert.match(html,/核对全部/);
});
