// Frontend guardrail regression: drafts are account-scoped, pending writes keep
// their idempotency key, adopted examination text is escaped, and a misregistered
// record still exposes medication correction. Synthetic data only.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const source = fs.readFileSync(path.resolve(__dirname, '../assets/app.js'), 'utf8')
  .replace(/\nboot\(\);\s*\n/, '\n// boot disabled by test harness\n');

function makeNode() {
  return {
    innerHTML: '', className: '', style: {},
    get childNodes() { return this.innerHTML ? [{ __html: this.innerHTML }] : []; },
    querySelector: () => null,
    replaceChildren(...nodes) { this.innerHTML = nodes.map((n) => n.__html || '').join(''); },
    addEventListener() {},
    getClientRects() { return []; },
    focus() {}
  };
}

function context(storage = new Map()) {
  const nodes = {
    root: makeNode(), 'modal-root': makeNode(), 'toast-root': makeNode(),
    view: makeNode(), main: makeNode()
  };
  const document = {
    cookie: '', body: { style: {} },
    getElementById: (id) => nodes[id] || null,
    querySelector: (sel) => sel === '#view' ? nodes.view : sel === '.main' ? nodes.main : null,
    querySelectorAll: () => [],
    createElement: () => makeNode(),
    addEventListener() {}
  };
  const sessionStorage = {
    setItem(k, v) { storage.set(k, String(v)); sessionStorage[k] = String(v); },
    getItem(k) { return Object.prototype.hasOwnProperty.call(sessionStorage, k) ? sessionStorage[k] : null; },
    removeItem(k) { delete sessionStorage[k]; storage.delete(k); }
  };
  for (const [k, v] of storage) sessionStorage[k] = v;
  const c = vm.createContext({
    document, window: { crypto, addEventListener() {} }, crypto,
    location: { hash: '' }, history: { replaceState() {} },
    sessionStorage,
    FormData: class {
      constructor(data) { this.data = data; }
      forEach(fn) { Object.entries(this.data).forEach(([k, v]) => fn(v, k)); }
    },
    setTimeout() {}, URLSearchParams, console, nodes
  });
  vm.runInContext(source, c);
  return c;
}

const run = (c, code) => vm.runInContext(code, c);
const session = (c, role, store, username) => {
  run(c, `state.session=${JSON.stringify({ role, store_ref: store, store_name: store, username })};`);
};

(async () => {
  const results = [];

  // R1: draft namespace is store + account + role; a new identity cannot read it.
  {
    const c = context();
    session(c, 'frontdesk', 'A', 'a_frontdesk');
    run(c, `saveDraft('new-reception', { complaint: 'A-PRIVATE' });`);
    assert.equal(run(c, `loadDraft('new-reception').complaint`), 'A-PRIVATE');
    session(c, 'frontdesk', 'A', 'another_frontdesk');
    assert.equal(run(c, `loadDraft('new-reception')`), null);
    session(c, 'doctor', 'A', 'a_doctor');
    run(c, `saveDraft('diagnosis', { diagnosis: 'DOCTOR-PRIVATE' });`);
    session(c, 'frontdesk', 'A', 'a_frontdesk');
    assert.equal(run(c, `loadDraft('diagnosis')`), null);
    session(c, 'frontdesk', 'B', 'b_frontdesk');
    assert.equal(run(c, `loadDraft('new-reception')`), null);
    run(c, `purgeForeignDrafts();`);
    session(c, 'frontdesk', 'A', 'a_frontdesk');
    assert.equal(run(c, `loadDraft('new-reception')`), null);
    results.push('R1 draft isolated by store/account/role and purged on identity switch');
  }

  // R2 + R5: escaped adopted examination; med correction stays reachable on a
  // misregistered record while new independent treatment is still hidden.
  {
    const c = context();
    session(c, 'doctor', 'A', 'a_doctor');
    const payload = {
      reception: {
        encounter_ref: 'enc-1', pet_name: '测试宠物', species: '猫',
        complaint: '合成主诉', created_at: '2026-10-23T10:00:00+08:00',
        doctor_ref: 2, receptionist_ref: 3, store_ref: 'A',
        error_mark: true, error_reason: '合成误建', related_encounter_refs: [],
        reception_versions: [{ version: 1, species: '猫', complaint: '合成主诉', created_at: '2026-10-23T10:00:00+08:00', actor_ref: 3 }]
      },
      clinical: {
        record_ref: 'rec-1', clinical_versions: [{
          version: 1, record_ref: 'rec-1', diagnosis: '合成诊断',
          examination: '合成检查', created_at: '2026-10-23T10:01:00+08:00', actor_ref: 2
        }],
        medication_chain: [{
          entry_ref: 'med-1', drug: '合成药', amount: '1', unit: '片',
          created_at: '2026-10-23T10:02:00+08:00', actor_ref: 2,
          reason: '合成理由', original_entry_ref: null
        }],
        adoptions: [{
          suggestion_ref: 's-1', medication_ref: null,
          created_at: '2026-10-23T10:03:00+08:00',
          source: { label: '合成演示建议' },
          adopted_content: { diagnosis: '合成诊断', examination: '<img src=x onerror="window.__probe=1">' }
        }]
      }
    };
    run(c, `
      api = async (p) => {
        if (p.includes('/suggestions')) return { items: [] };
        if (p.includes('/alerts')) return { items: [] };
        return ${JSON.stringify(payload)};
      };
    `);
    await run(c, `viewClinicalDetail(nodes.view, 'enc-1')`);
    const html = run(c, 'nodes.view.innerHTML');
    assert.ok(html.includes('data-action="med-correct"'), 'med correction should remain on error-marked record');
    assert.ok(!html.includes('<img src=x'), 'adopted examination must not create an element');
    assert.ok(html.includes('&lt;img'), 'adopted examination must render as text');
    results.push('R2 adopted examination escaped; R5 med correction visible on misregistered record');
  }

  // R3: an uncertain write keeps the same key after a fresh context (refresh),
  // and a completed write releases it.
  {
    const storage = new Map();
    const first = context(storage);
    session(first, 'doctor', 'A', 'a_doctor');
    run(first, `state.modal={draftKey:'med-add:enc-1'}; keys=[]; api=async (p,o)=>{ keys.push(o.key); throw new ApiError('NETWORK',0,null); };`);
    await run(first, `write('/api/clinical/encounters/enc-1/medications',{drug:'合成药'}).catch(()=>{})`);
    await run(first, `write('/api/clinical/encounters/enc-1/medications',{drug:'合成药'}).catch(()=>{})`);
    assert.equal(run(first, 'keys[0]'), run(first, 'keys[1]'));

    const reload = context(storage);
    session(reload, 'doctor', 'A', 'a_doctor');
    run(reload, `state.modal={draftKey:'med-add:enc-1'}; keys=[]; api=async (p,o)=>{ keys.push(o.key); return {ok:true}; };`);
    await run(reload, `write('/api/clinical/encounters/enc-1/medications',{drug:'合成药'})`);
    assert.equal(run(reload, 'keys[0]'), run(first, 'keys[0]'));
    assert.equal(run(reload, `pendingFor('med-add:enc-1')`), null);
    await run(reload, `write('/api/clinical/encounters/enc-1/medications',{drug:'合成药'})`);
    assert.notEqual(run(reload, 'keys[1]'), run(reload, 'keys[0]'));
    results.push('R3 uncertain key survives refresh; success releases it for the next intent');
  }

  // R7: HTTP 200 with an unparseable body is still an uncertain write; keep the key.
  {
    const storage = new Map();
    const first = context(storage);
    session(first, 'doctor', 'A', 'a_doctor');
    run(first, `state.modal={draftKey:'med-add:enc-7'}; keys=[]; api=async (p,o)=>{ keys.push(o.key); throw new ApiError('UNPARSEABLE',200,null); };`);
    await run(first, `write('/api/clinical/encounters/enc-7/medications',{drug:'合成药'}).catch(()=>{})`);
    await run(first, `write('/api/clinical/encounters/enc-7/medications',{drug:'合成药'}).catch(()=>{})`);
    assert.equal(run(first, 'keys[0]'), run(first, 'keys[1]'));
    assert.notEqual(run(first, `pendingFor('med-add:enc-7')`), null);

    const reload = context(storage);
    session(reload, 'doctor', 'A', 'a_doctor');
    run(reload, `state.modal={draftKey:'med-add:enc-7'}; keys=[]; api=async (p,o)=>{ keys.push(o.key); return {ok:true}; };`);
    await run(reload, `write('/api/clinical/encounters/enc-7/medications',{drug:'合成药'})`);
    assert.equal(run(reload, 'keys[0]'), run(first, 'keys[0]'));
    results.push('R7 HTTP 200 unparseable response keeps the pending key');
  }

  // R6: a late A response must not overwrite a newer B route.
  {
    const c = context();
    session(c, 'doctor', 'A', 'a_doctor');
    run(c, `
      setActiveNav=()=>{}; updateShell=()=>{};
      state.__late={};
      viewClinicalDetail=(target,id)=>new Promise((resolve)=>{
        state.__late[id]=()=>{ target.innerHTML='DETAIL-'+id; resolve(); };
      });
    `);
    run(c, `location.hash='#/clinical/A';`);
    const a = run(c, 'renderRoute()');
    run(c, `location.hash='#/clinical/B';`);
    const b = run(c, 'renderRoute()');
    run(c, `state.__late.B()`);
    await b;
    run(c, `state.__late.A()`);
    await a;
    assert.ok(run(c, `nodes.view.innerHTML.includes('DETAIL-B')`));
    assert.ok(!run(c, `nodes.view.innerHTML.includes('DETAIL-A')`));
    results.push('R6 late A response discarded; current B route stays rendered');
  }

  // R4: a 51st record is reachable through the queue pagination control.
  {
    const c = context();
    session(c, 'doctor', 'A', 'a_doctor');
    run(c, `api=async()=>({items:[],total:51,page:1,page_size:50});`);
    await run(c, `viewClinicalList(nodes.view)`);
    const html = run(c, 'nodes.view.innerHTML');
    assert.ok(html.includes('data-action="clinical-page"'));
    assert.ok(html.includes('data-page="2"'));
    results.push('R4 clinical queue exposes next-page control for total 51');
  }

  {
    const c = context();
    assert.ok(run(c, `loadingView('合成','list')`).includes('sk-list'));
    assert.ok(run(c, `loadingView('合成','detail')`).includes('sk-sheet'));
  }

  console.log(JSON.stringify({ ok: true, results }, null, 2));
})().catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
