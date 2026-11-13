// Loaded ONLY from the disposable acceptance template, never production HTML.
(() => {
  const nativeFetch = window.fetch.bind(window);
  const mode = location.hash.slice(1) || 'control';
  // This script runs in <head>, before the trailing fixture meta is parsed.
  const controlledOnly = () => !!document.querySelector('meta[name="guide-acceptance-controlled-only"]');
  const endpoints = new Set(['https://www.wikidata.org/w/api.php','https://zh.wikipedia.org/w/api.php','https://en.wikipedia.org/w/api.php']);
  const evidence = window.guideAcceptance = {mode,network:[],controlled:[]};
  const showDiagnostics = () => {
    if (!controlledOnly()) return;
    const script = document.querySelector('[data-acceptance-script-state]');
    const count = document.querySelector('[data-acceptance-request-count]');
    if (script) script.textContent = '脚本已执行（本地控制fixture）';
    if (count) count.textContent = `受控API请求：${evidence.controlled.length}；公开透传：关闭`;
  };
  document.addEventListener('DOMContentLoaded',showDiagnostics,{once:true});
  window.fetch = async (input, options) => {
    const url = new URL(input,location.href);
    if (!endpoints.has(url.origin+url.pathname)) return nativeFetch(input,options);
    if (mode === 'live') {
      if (controlledOnly()) throw new Error('remaining_acceptance_disallows_public_replay');
      const used = Number(sessionStorage.getItem('guide-live-count') || 0);
      if (used >= 4) throw new Error('acceptance_public_budget');
      sessionStorage.setItem('guide-live-count',String(used+1));
      const row = {host:url.host,public_fields:Object.fromEntries(url.searchParams),
        credentials:options.credentials,referrerPolicy:options.referrerPolicy,mode:options.mode,
        started:performance.now()}; evidence.network.push(row);
      try {
        const response = await nativeFetch(input,options);
        Object.assign(row,{http:response.status,type:response.type,elapsed:performance.now()-row.started});
        const data = await response.clone().json();
        row.qid = data.entities ? Object.keys(data.entities)[0] : data.query?.pages?.[0]?.pageprops?.wikibase_item;
        const text = data.query?.pages?.[0]?.extract;
        if (typeof text === 'string') {
          row.summary_length = text.length;
          row.summary_sha256 = [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text)))].map(x=>x.toString(16).padStart(2,'0')).join('');
        }
        return response;
      } catch(error) { row.error = error.name;row.elapsed=performance.now()-row.started;throw error; }
    }
    evidence.controlled.push({host:url.host,mode});
    showDiagnostics();
    if (mode === 'error') throw new TypeError('controlled network failure');
    if (mode === 'slow') return new Promise((resolve,reject) => options.signal.addEventListener('abort',()=>reject(new DOMException('cancelled','AbortError'))));
    if (url.host === 'www.wikidata.org') return new Response(JSON.stringify({entities:{Q726389:{id:'Q726389',
      claims:{P225:[{mainsnak:{datavalue:{value:'Cardinalis cardinalis'}}}]},sitelinks:{zhwiki:{title:'北美紅雀'}}}}}));
    if (url.searchParams.has('list')) return new Response(JSON.stringify({query:{search:[]}}));
    return new Response(JSON.stringify({query:{pages:[{pageid:123,title:'北美紅雀',pageprops:{wikibase_item:'Q726389'},
      extract:mode === 'empty' ? '' : '【受控布局样例，非真实联网摘要】北美红雀的公开资料用于核对长中文内容、来源署名和窄屏换行。',
      revisions:[{revid:456,timestamp:'2026-10-01T00:00:00Z'}]}]}}));
  };
})();
