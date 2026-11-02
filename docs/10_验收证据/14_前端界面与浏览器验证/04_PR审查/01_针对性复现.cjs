// Run the exact PR JavaScript in an isolated VM with synthetic data only.
// This is a targeted logic/rendering check, not a full browser or database test.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const source = fs.readFileSync(path.resolve(__dirname, '../../../frontend/assets/app.js'), 'utf8');
const results = [];

function context(storage = new Map()) {
  const handlers = {};
  const nodes = Object.fromEntries(['root', 'modal-root', 'toast-root', 'view'].map(k => [k, { innerHTML: '' }]));
  const doc = {
    cookie: '', body: { style: {} },
    getElementById: k => nodes[k],
    querySelector: q => q === '#view' ? nodes.view : null,
    querySelectorAll: () => [],
    addEventListener: (name, fn) => { handlers[name] = fn; }
  };
  const c = vm.createContext({
    document: doc, window: { crypto, addEventListener() {} }, crypto,
    location: { hash: '' }, history: { replaceState() {} },
    sessionStorage: {
      setItem: (k,v) => storage.set(k,v), getItem: k => storage.get(k) ?? null,
      removeItem: k => storage.delete(k)
    },
    FormData: class { constructor(data) { this.data = data; } forEach(fn) { Object.entries(this.data).forEach(([k,v]) => fn(v,k)); } },
    setTimeout() {}, URLSearchParams, console, handlers, nodes
  });
  vm.runInContext(source.replace(/\r?\nboot\(\);\r?\n/, '\n// Automatic boot disabled by review harness.\n'), c);
  return c;
}
const run = (c,s) => vm.runInContext(s,c);
const session = (c, role='doctor', store='A', username='a_doctor') => run(c, `state.session=${JSON.stringify({role, store_ref:store, store_name:store, username})}`);

(async () => {
  // 1. Use the real logout handler, then render a different store's new form.
  const c = context(); session(c,'frontdesk','A','a_frontdesk');
  run(c, `saveDraft('new-reception', {complaint:'STORE_A_PRIVATE_COMPLAINT', species:'cat'}); api=async()=>({}); renderLogin=()=>{};`);
  await c.handlers.click({target:{closest:()=>({dataset:{action:'logout'}})}});
  session(c,'frontdesk','B','b_frontdesk');
  run(c, `api=async()=>({doctor_options:[{doctor_ref:202,display_name:'b_doctor'}]});`);
  await run(c, 'viewNewReception(nodes.view)');
  assert(c.nodes.view.innerHTML.includes('STORE_A_PRIVATE_COMPLAINT'));
  results.push({case:'cross_store_draft_after_logout', reproduced:true, detail:'Store B new-reception form renders Store A complaint after real logout handler.'});

  // 2. The same uncertain write retains a key only until page reload.
  const storage = new Map(); const first = context(storage); session(first);
  run(first, `keys=[]; api=async(p,o)=>{keys.push(o.key);throw new ApiError('NETWORK',0,null)}; saveDraft('med-append:encounter', {drug:'SYNTHETIC',amount:'1',unit:'unit'});`);
  await run(first, `write('/api/clinical/encounters/encounter/medications',{drug:'SYNTHETIC',amount:'1',unit:'unit'}).catch(()=>{})`);
  await run(first, `write('/api/clinical/encounters/encounter/medications',{drug:'SYNTHETIC',amount:'1',unit:'unit'}).catch(()=>{})`);
  const before = run(first,'keys'); assert.equal(before[0],before[1]);
  const reload = context(storage); session(reload);
  run(reload, `keys=[];api=async(p,o)=>{keys.push(o.key);return {operation_state:'saved'}}`);
  await run(reload, `write('/api/clinical/encounters/encounter/medications',{drug:'SYNTHETIC',amount:'1',unit:'unit'})`);
  assert.notEqual(before[0],run(reload,'keys[0]'));
  assert(run(reload, `loadDraft('med-append:encounter').drug`));
  results.push({case:'uncertain_retry_after_reload',reproduced:true, detail:'Input survives in sessionStorage; Idempotency-Key changes on reload. Same-page retry correctly retains its key.'});

  // 3. Render actual clinical detail with a synthetic adopted examination.
  const x = context(); session(x);
  x.item = {
    reception:{encounter_ref:'encounter-A',species:'cat',complaint:'synthetic'},
    clinical:{record_ref:'record-A',diagnosis:'synthetic',examination:'safe',clinical_versions:[{version:1}],
      medication_chain:[],adoptions:[{adopted_content:{examination:'<img src=x onerror="window.__review_probe=1">'}}]}
  };
  run(x, `api=async p=>p.endsWith('/suggestions')||p.endsWith('/alerts')?{items:[]}:item`);
  await run(x,'viewClinicalDetail(nodes.view,"encounter-A")');
  assert(x.nodes.view.innerHTML.includes('<img src=x onerror="window.__review_probe=1">'));
  results.push({case:'unescaped_adopted_examination',reproduced:true, detail:'Stored adopted_content.examination reaches innerHTML as an executable img/onerror element, rather than text. VM verifies generated HTML, not event execution.'});

  // 4. The backend reports 51 records, but the doctor view exposes page 1 only.
  const p = context(); session(p);
  run(p, `requested=[];api=async url=>{requested.push(url);return {page:1,page_size:50,total:51,items:Array.from({length:50},(_,i)=>({reception:{encounter_ref:'encounter-'+i},clinical:null}))}}`);
  await run(p, 'viewClinicalList(nodes.view)');
  assert(!p.nodes.view.innerHTML.includes('下一页'));
  assert(!run(p,'requested').some(url=>url.includes('page=')));
  results.push({case:'doctor_page_2_inaccessible',reproduced:true, detail:'51 total; only first 50 rendered; no next-page or full-reference lookup control on doctor list.'});

  // 5. Error-marked encounters must still permit linked medication correction.
  const m = context(); session(m);
  m.item = {reception:{encounter_ref:'marked',error_mark:true},clinical:{record_ref:'r',clinical_versions:[{version:1}],medication_chain:[{entry_ref:'old',drug:'SYNTHETIC',amount:'1',unit:'unit'}],adoptions:[]}};
  run(m, `api=async p=>p.endsWith('/suggestions')||p.endsWith('/alerts')?{items:[]}:item`);
  await run(m,'viewClinicalDetail(nodes.view,"marked")');
  assert(!m.nodes.view.innerHTML.includes('data-action="med-correct"'));
  assert(m.nodes.view.innerHTML.includes('data-action="diagnosis-correct"'));
  results.push({case:'marked_encounter_medication_correction_hidden',reproduced:true,detail:'Error-marked encounter retains diagnosis correction but removes all linked medication correction controls, contrary to SRS C-07.'});

  // 6. Let a slow A route complete after a fast B route.
  const race = context(); session(race);
  run(race, `let releaseA; const waitA=new Promise(r=>releaseA=r); api=async url=>{if(url.endsWith('/suggestions')||url.endsWith('/alerts'))return {items:[]};const id=url.split('/').pop();if(id==='A')await waitA;return {reception:{encounter_ref:id,pet_name:'PATIENT_'+id},clinical:null}};location.hash='#/clinical/A';pendingA=renderRoute();`);
  await run(race, `location.hash='#/clinical/B';renderRoute()`);
  assert(race.nodes.view.innerHTML.includes('PATIENT_B'));
  await run(race,'releaseA();pendingA');
  assert(race.nodes.view.innerHTML.includes('PATIENT_A'));
  assert.equal(run(race,'location.hash'),'#/clinical/B');
  results.push({case:'stale_route_response',reproduced:true,detail:'Route remains B, but late A response overwrites body with patient A and A-targeting action buttons.'});

  // Optional actual-browser check; contains synthetic data and changes only its own isolated blank tab.
  const browserScript = `async () => {\n document.body.innerHTML='<div id="root"></div><div id="modal-root"></div><div id="toast-root"></div><div id="view"></div>';\n ${source.replace(/\r?\nboot\(\);\r?\n/,'\n')}\n state.session={role:'doctor',username:'synthetic',store_name:'A'};\n const syntheticItem=${JSON.stringify(x.item)};\n api=async p=>p.endsWith('/suggestions')||p.endsWith('/alerts')?{items:[]}:syntheticItem;\n await viewClinicalDetail(document.querySelector('#view'),'encounter-A');\n await new Promise(r=>setTimeout(r,150));\n return {probeExecuted:window.__review_probe===1, imageInserted:!!document.querySelector('#view img[onerror]'),syntheticOnly:true};\n}`;
  fs.writeFileSync(path.join(__dirname,'03_浏览器转义复现.js'),browserScript);

  const report={head:'9d059a087ff126d7429eff53746e1efae22066c5',source_sha256:crypto.createHash('sha256').update(source).digest('hex'),environment:process.version,scope:'Exact PR source in isolated Node VM with synthetic API/DOM/storage; no live DB writes.',results};
  fs.writeFileSync(path.join(__dirname,'02_复现结果.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report,null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
