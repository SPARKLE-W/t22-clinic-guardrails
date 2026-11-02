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
    appendChild() {},
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
  const captured = [];
  const handlers = {};
  const document = {
    cookie: '', body: { style: {} },
    getElementById: (id) => nodes[id] || null,
    querySelector: (sel) => sel === '#view' ? nodes.view : sel === '.main' ? nodes.main : null,
    querySelectorAll: () => [],
    createElement: () => makeNode(),
    addEventListener: (name, fn) => { handlers[name] = fn; }
  };
  const sessionStorage = {
    setItem(k, v) { storage.set(k, String(v)); sessionStorage[k] = String(v); },
    getItem(k) { return Object.prototype.hasOwnProperty.call(sessionStorage, k) ? sessionStorage[k] : null; },
    removeItem(k) { delete sessionStorage[k]; storage.delete(k); }
  };
  for (const [k, v] of storage) sessionStorage[k] = v;
  const c = vm.createContext({
    document, window: { crypto, addEventListener() {} }, crypto,
    navigator: { clipboard: { writeText: async (v) => { captured.push(v); } } },
    location: { hash: '' }, history: { replaceState() {} },
    sessionStorage, captured, handlers,
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

  {
    const c = context();
    session(c, 'doctor', 'A', 'a_doctor');
    run(c, `api=async()=>({items:[{alert_ref:'a1',handling_state:'unhandled',rule_ref:'SR-01',hit_field:'reason'},{alert_ref:'a2',handling_state:'handled',rule_ref:'SR-02',hit_field:'amount'}]});`);
    await run(c, `viewAlerts(nodes.view)`);
    const html = run(c, 'nodes.view.innerHTML');
    assert.ok(html.includes('待处理<span class="seg-count">1</span>'));
    assert.ok(html.includes('显示 1 / 2 条'));
    results.push('alert tabs expose unhandled/handled/all counts');
  }

  {
    const c = context();
    session(c, 'manager', 'A', 'a_manager');
    run(c, `api=async()=>({items:[
      {event_at:'2026-10-23T10:00:00+08:00',operator_ref:1,operator_role:'manager',action:'identity.login',outcome:'success',target_ref:null},
      {event_at:'2026-10-23T10:01:00+08:00',operator_ref:2,operator_role:'doctor',action:'record.write',outcome:'refused',normalized_reason:'PERMISSION_DENIED',target_ref:null},
      {event_at:'2026-10-23T10:02:00+08:00',operator_ref:2,operator_role:'doctor',action:'record.write',outcome:'validation_failed',normalized_reason:'INPUT_INVALID',target_ref:null}
    ]});`);
    await run(c, `viewAudit(nodes.view)`);
    let html = run(c, 'nodes.view.innerHTML');
    assert.ok(html.includes('成功<span class="seg-count">1</span>'));
    assert.ok(html.includes('拒绝<span class="seg-count">1</span>'));
    assert.ok(html.includes('校验失败<span class="seg-count">1</span>'));
    assert.ok(html.includes('显示 3 / 3 条'));
    run(c, `state.auditOutcome='refused';`);
    await run(c, `viewAudit(nodes.view)`);
    html = run(c, 'nodes.view.innerHTML');
    assert.ok(html.includes('显示 1 / 3 条'));
    results.push('audit tabs expose success/refused/validation counts and filter');
  }

  {
    const c = context();
    session(c, 'manager', 'A', 'a_manager');
    run(c, `state.summaryRange='all'; state.summaryGroup='rule'; api=async()=>({
      time_range:null, encounter_created_count:12, error_marked_count:2, hit_count:7, alert_count:6, unhandled_count:3,
      grouped_counts:{group_by:'rule', items:[{rule_ref:'SR-02',hit_count:4,alert_count:3,unhandled_count:1}]}
    });`);
    await run(c, `viewSummary(nodes.view)`);
    let html = run(c, 'nodes.view.innerHTML');
    assert.ok(html.includes('本店汇总'));
    assert.ok(html.includes('建档接诊'));
    assert.ok(html.includes('12'));
    assert.ok(html.includes('SR-02'));
    assert.ok(html.includes('按规则'));
    run(c, `state.summaryGroup='handling_state'; api=async()=>({time_range:null, encounter_created_count:0,error_marked_count:0,hit_count:0,alert_count:0,unhandled_count:0, grouped_counts:{group_by:'handling_state', items:[{handling_state:'unhandled',alert_count:0}]}});`);
    await run(c, `viewSummary(nodes.view)`);
    html = run(c, 'nodes.view.innerHTML');
    assert.ok(html.includes('按处理状态'));
    assert.ok(html.includes('未处理'));
    results.push('management summary renders five counts and both grouping modes');
  }

  {
    const c = context();
    session(c, 'manager', 'A', 'a_manager');
    for (const payload of [{}, { encounter_created_count: 0 }, {
      time_range: null, encounter_created_count: 0, error_marked_count: 0,
      hit_count: 0, alert_count: 0, unhandled_count: 0,
      grouped_counts: { group_by: 'rule', items: [{ rule_ref: 'SR-02', hit_count: 'bad', alert_count: 0, unhandled_count: 0 }] }
    }]) {
      run(c, `api=async()=>(${JSON.stringify(payload)});`);
      let error;
      try { await run(c, 'viewSummary(nodes.view)'); } catch (e) { error = e; }
      assert.equal(error && error.code, 'UNPARSEABLE');
      assert.ok(!run(c, 'nodes.view.innerHTML').includes('空集合是真实零'));
    }
    results.push('malformed HTTP 200 summary never becomes a true zero');
  }

  {
    const c = context();
    assert.deepEqual(JSON.parse(run(c, `JSON.stringify(exportRangeFromValues('', ''))`)), { ok: true, range: null });
    assert.equal(JSON.parse(run(c, `JSON.stringify(exportRangeFromValues('', '2026-10-23'))`)).ok, false);
    assert.equal(JSON.parse(run(c, `JSON.stringify(exportRangeFromValues('2026-10-23', '2026-10-23'))`)).ok, true);
    const sha = 'a'.repeat(64);
    assert.equal(run(c, `exportVerifyHeaders({get:(k)=>(({ 'content-length':'3','x-byte-count':'3','x-content-sha256':'${sha}' })[k])}, 3)`), sha);
    assert.throws(() => run(c, `exportVerifyHeaders({get:()=>null}, 3)`), /未完整接收/);
    assert.throws(() => run(c, `exportVerifyHeaders({get:(k)=>(({ 'content-length':'3','x-byte-count':'4','x-content-sha256':'${sha}' })[k])}, 3)`), /未完整接收/);
    assert.throws(() => run(c, `exportVerifyHeaders({get:(k)=>(({ 'content-length':'3','x-byte-count':'3','x-content-sha256':'bad' })[k])}, 3)`), /未完整接收/);
    results.push('F-04 partial dates and export metadata are rejected');
  }

  {
    const c = context();
    const item = {
      work_item_ref: '11111111-1111-4111-8111-111111111111',
      stage_ref: 'rule_review',
      plan_version_ref: '22222222-2222-4222-8222-222222222222',
      actual_version_ref: '33333333-3333-4333-8333-333333333333',
      plan_dimensions: { content_generation: null, data_entry: 'manager', human_decision: 'manager' },
      source_event_refs: ['44444444-4444-4444-8444-444444444444'],
      actual_execution_metadata: [{ source_event_ref: '44444444-4444-4444-8444-444444444444', stage_ref: 'rule_review', actor_ref: 2, actor_role: 'manager', occurred_at: '2026-10-23T10:00:00+08:00', result: 'approve', dimensions: { content_generation: 'human', data_entry: 'manager', human_decision: 'manager' } }],
      manual_evidence_provenance: [],
      comparison: { state: 'pending_review', missing_dimensions: [], differences: [{ dimension: 'human_decision', planned: 'manager', actual: ['doctor'] }] },
      maintenance_history: [{ plan_version_ref: '22222222-2222-4222-8222-222222222222', actual_version_ref: '33333333-3333-4333-8333-333333333333', actor_ref: 1, created_at: '2026-10-23T10:00:00+08:00', reason: '登记', plan_dimensions: {}, source_event_refs: [], actual_execution_metadata: [], manual_evidence_provenance: [], comparison: { state: 'pending_review', missing_dimensions: [], differences: [] } }],
      review_history: []
    };
    const html = run(c, `workDetailHtml(${JSON.stringify(item)})`);
    assert.ok(html.includes('待复核'));
    assert.ok(html.includes('维护新版本'));
    assert.ok(html.includes('人工复核'));
    assert.ok(html.includes('#2'));
    assert.ok(!html.includes('诊断正文'));
    assert.ok(run(c, `workEvidenceHtml(${JSON.stringify(item.actual_execution_metadata)}).includes('账号 #2')`));
    const evidenceHtml = run(c, `workEvidenceHtml(${JSON.stringify([...item.actual_execution_metadata, { source_event_ref: '55555555-5555-4555-8555-555555555555', stage_ref: 'rule_review', actor_ref: null, actor_role: null, occurred_at: '2026-10-23T10:03:00+08:00', result: 'new', dimensions: { content_generation: 'ai', data_entry: 'doctor', human_decision: null } }])}, ${JSON.stringify(item.source_event_refs)})`);
    assert.ok(evidenceHtml.includes('44444444-4444-4444-8444-444444444444'));
    assert.ok(evidenceHtml.includes('55555555-5555-4555-8555-555555555555'));
    const reviewed = { ...item, review_history: [
      { plan_version_ref: item.plan_version_ref, actual_version_ref: item.actual_version_ref, actor_ref: 1, created_at: '2026-10-23T10:01:00+08:00', conclusion: 'confirmed_difference', reason: '旧结论' },
      { plan_version_ref: item.plan_version_ref, actual_version_ref: item.actual_version_ref, actor_ref: 3, created_at: '2026-10-23T10:02:00+08:00', conclusion: 'confirmed_consistent', reason: '新结论' }
    ] };
    assert.equal(run(c, `workReviewFor(${JSON.stringify(reviewed)}).conclusion`), 'confirmed_consistent');
    assert.ok(run(c, `workManualRowHtml({dimension:'data_entry',value:'manager',source_ref:'REF-1'})`).includes('REF-1'));
    assert.ok(source.includes('重新加载业务证据'));
    run(c, `state.workDivisionItems=[]; state.workDivisionSelected=null; api=async()=>({items:[${JSON.stringify(item)}],page:1,page_size:50,total:1});`);
    await run(c, `viewWorkDivisions(nodes.view)`);
    const listHtml = run(c, 'nodes.view.innerHTML');
    assert.ok(listHtml.includes('人机分工'));
    assert.ok(listHtml.includes('规则审批'));
    assert.ok(listHtml.includes('待复核'));
    results.push('F-03 work division detail renders comparison, evidence and review action');
  }

  {
    const c = context();
    const full = '11111111-2222-4333-8444-555555555555';
    assert.ok(run(c, `copyRef('${full}', '候选编号')`).includes(`data-value="${full}"`));
    assert.equal((source.match(/case 'copy-ref'/g) || []).length, 1);
    await run(c, `handlers.click({ target: { closest: () => ({ dataset: { action: 'copy-ref', value: '${full}' } }) } })`);
    assert.equal(run(c, 'captured[0]'), full);
    const before = run(c, 'captured.length');
    await run(c, `handlers.click({ target: { closest: () => ({ dataset: { action: 'copy-ref', ref: '${full}' } }) } })`);
    assert.equal(run(c, 'captured.length'), before);
    results.push('F-01 copy-ref uses data-value, one handler case, empty data-ref does not report success');
  }

  {
    const c = context();
    assert.equal(run(c, `exportFilename('attachment; filename="clinic.csv"')`), 'clinic.csv');
    assert.equal(run(c, `exportFilename("attachment; filename*=UTF-8''%E7%97%85%E5%8E%86.csv")`), '病历.csv');
    assert.deepEqual(JSON.parse(run(c, `JSON.stringify(refsFromText('11111111-1111-4111-8111-111111111111, 22222222-2222-4222-8222-222222222222'))`)), ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222']);
    results.push('F-04 export filename and reference parsing');
  }

  console.log(JSON.stringify({ ok: true, results }, null, 2));
})().catch((e) => {
  console.error(e.stack || e);
  process.exit(1);
});
