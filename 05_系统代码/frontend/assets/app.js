'use strict';
/* T22 门诊护栏 · 前端
   设计方向：病历纸（可审计的记录）。无构建工具，原生 ES2020。 */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const root = document.getElementById('root');
const modalRoot = document.getElementById('modal-root');
const toastRoot = document.getElementById('toast-root');

const state = {
  session: null,
  sidebar: null,
  route: null,
  range: 'all',
  alertFilter: 'unhandled',
  selectedCandidate: '',
  auditFilters: { record_ref: '', operator_ref: '' },
  pending: new Map(),
  modal: null,
  reloadLatest: null,
  bootError: null
};

/* ---------------- 文案与映射 ---------------- */

const ERRORS = {
  AUTH_REQUIRED: '登录已失效，请重新登录。',
  INVALID_CREDENTIALS: '账号或密码不正确。',
  PERMISSION_DENIED: '当前账号没有此操作权限。',
  TARGET_UNAVAILABLE: '目标不存在，或不在当前账号获准范围内。',
  INPUT_INVALID: '输入格式不符合要求，请检查后重试。',
  INPUT_FIELDS_INVALID: '提交的字段与接口要求不一致。',
  INVALID_REFERENCE: '引用编号格式无效。',
  REASON_REQUIRED: '请填写操作理由。',
  STATE_CONFLICT: '记录状态已变化，请刷新后核对。',
  STATE_OR_VERSION_CONFLICT: '记录版本已变化，请刷新后核对；不要覆盖旧版本。',
  OPERATION_CONTENT_CONFLICT: '同一操作标识下提交了不同内容。请刷新后重新发起一次操作。',
  GUARD_BLOCKED: '本次写入被护栏阻断，病历未改变。',
  TEMPLATE_UNAVAILABLE: '本店暂无可用模板，或原模板无法解释。',
  INVALID_TEMPLATE: '所选模板已变化，请刷新后核对。',
  DOCTOR_UNAVAILABLE: '请选择本店有效的接诊医生。',
  PET_UNAVAILABLE: '该宠物编号无法在本店关联。',
  REFERENCE_CONFLICT: '编号已被使用，请核验已有编号或重新生成。',
  RECORD_UNAVAILABLE: '病历版本暂无法解释，请联系管理核对。',
  POLICY_UNAVAILABLE: '当前规则或原模板无法核验，请联系管理检查生效版本。',
  RULE_EXECUTION_UNAVAILABLE: '当前规则或模板字段暂不能执行，尚未写入病历。',
  RULE_FACT_UNAVAILABLE: '本次规则缺少可核验依据，尚未写入病历。',
  SUGGESTION_SOURCE_UNAVAILABLE: '建议来源无法核验，不能采纳。',
  DECISION_FINAL: '该建议已有终态决定，不能再次决定。',
  APPROVAL_REQUIRED: '该候选尚未获批，不能启用。',
  CONTENT_CHANGED: '候选内容已变化，原批准不可继续使用。',
  POLICY_VERSION_CONFLICT: '本店生效版本已变化，请刷新后核对。',
  SCOPE_DENIED: '定义不属于当前门店。',
  PERSISTENCE_UNCONFIRMED: '保存结果暂无法确认。请保留输入，稍后重试核对。',
  AUDIT_UNAVAILABLE: '审计暂不可用，操作未确认。',
  AUTH_FACILITY_UNAVAILABLE: '登录服务暂不可用，请稍后重试。',
  METHOD_NOT_ALLOWED: '请求方式不被允许。',
  MISSING_PERMISSION_ENTRY: '接口未登记权限入口，已拒绝。',
  CSRF: '页面会话已失效，请刷新后重试。',
  NETWORK: '网络中断，结果暂无法确认。请保留输入，使用同一操作重试核对。',
  UNPARSEABLE: '响应暂无法确认，请保留输入后重试核对。'
};

const ROLES = { frontdesk: '前台', doctor: '医生', manager: '管理' };
const ROLE_HOME = { frontdesk: 'receptions', doctor: 'clinical', manager: 'governance' };
const CANDIDATE_STATES = { draft: '草案', submitted: '待审批', approved: '已批准', rejected: '已驳回' };
const OUTCOMES = { success: '成功', refused: '拒绝', validation_failed: '校验失败', request_refused: '请求拒绝' };
const ACTIONS = {
  'identity.login': '登录', 'identity.logout': '退出',
  'clinical.read': '读取病历', 'encounter.read': '读取接待',
  'encounter.create': '新建接诊', 'encounter.correct': '更正接待', 'encounter.mark_error': '标记误建',
  'clinical.diagnosis.write': '诊断写入', 'clinical.medication.write': '用药写入',
  'clinical.alert.handle': '处理告警', 'clinical.suggestion.adopt': '采纳建议', 'clinical.suggestion.decline': '不采纳建议',
  'governance.candidate.generate': '生成候选', 'governance.candidate.generate.submit': '提交候选',
  'governance.candidate.review': '审批候选', 'governance.candidate.review.review': '审批候选',
  'governance.candidate.review.read': '读取候选', 'governance.candidate.activate.activate': '启用候选',
  'governance.candidate.activate': '启用候选', 'governance.candidate.generate.generate': '生成候选',
  'audit.metadata.read': '读取审计'
};
const DIFF_LABELS = {
  safety_redlines: '安全红线', coding_standards: '编码约束', authorization: '角色与数据范围',
  templates: '字段与模板', quality_gates: '质量目标', collaboration: '人机分工', additional_rules: '附加规则',
  srs: '需求基线', scope: '所属门店', version: '定义版本', demand_id: '需求编号', format_version: '定义格式'
};
const RULE_TEXT = {
  'SR-01': '模板引用无法解释：本次写入使用的字段模板不可用。',
  'SR-02': '必填内容缺失：诊断或用药三要素不完整。',
  'SR-03': '关联更正不成立：缺少原条目或更正理由。',
  'SR-04': '建议采纳条件不足：缺少理由或来源保留。',
  'SR-05': '批准内容与候选摘要不一致，不能启用。',
  'SR-06': '审计或授权导出条件未满足。',
  'SR-08': '凭据或参数化查询条件不成立。',
  'SR-09': '告警持久化与幂等条件未满足。',
  'SR-10': '强制控制段缺失。'
};
const FIELD_TEXT = {
  diagnosis: '诊断', examination: '检查', reason: '更正 / 采纳理由', amount: '剂量', unit: '单位',
  drug: '药品', original_entry_ref: '关联原条目', content_digest: '内容摘要', template: '字段模板'
};

/* ---------------- 工具 ---------------- */

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const shortRef = (v, n = 8) => (v ? String(v).slice(0, n) : '—');
const refCode = (v, n = 8) => `<code title="${esc(v)}">${esc(shortRef(v, n))}</code>`;

function cookie(name) {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}

function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function isUuid(v) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(String(v || '').trim());
}

const dtf = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const dtfFull = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const dateFmt = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });

function parseTime(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}
function fmtTime(iso) {
  const d = parseTime(iso);
  if (!d) return '—';
  return dtf.format(d).replace(/\//g, '-');
}
function fmtFull(iso) {
  const d = parseTime(iso);
  if (!d) return '—';
  return dtfFull.format(d).replace(/\//g, '-');
}
function fmtSlot(iso) {
  const d = parseTime(iso);
  if (!d) return '—';
  const today = dateFmt.format(new Date());
  return dateFmt.format(d) === today ? dtf.format(d).replace(/\//g, '-').slice(-5) : dtf.format(d).replace(/\//g, '-');
}

function addDays(ymd, n) {
  const d = new Date(ymd + 'T00:00:00+08:00');
  d.setUTCDate(d.getUTCDate() + n);
  return dateFmt.format(d);
}
function rangeParam(range) {
  if (range === 'all') return null;
  const today = dateFmt.format(new Date());
  const from = range === 'today' ? today : addDays(today, -6);
  const to = addDays(today, 1);
  return JSON.stringify({ from: from + 'T00:00:00+08:00', to: to + 'T00:00:00+08:00' });
}

function qs(params) {
  const out = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') out.set(k, v);
  });
  return out.toString();
}

function chip(text, kind = '') {
  return `<span class="chip ${kind}">${esc(text)}</span>`;
}
function statusChip(ok, text, okText) {
  return ok ? chip(text || '正常', 'ok') : chip(text || '异常', 'danger');
}

function saveDraft(key, form) {
  try {
    const data = {};
    new FormData(form).forEach((v, k) => { if (typeof v === 'string') data[k] = v; });
    sessionStorage.setItem('t22:' + key, JSON.stringify(data));
  } catch (e) { /* 隐私模式忽略 */ }
}
function loadDraft(key) {
  try {
    const raw = sessionStorage.getItem('t22:' + key);
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
}
function clearDraft(key) {
  try { sessionStorage.removeItem('t22:' + key); } catch (e) { /* ignore */ }
}

/* ---------------- API ---------------- */

class ApiError extends Error {
  constructor(code, status, payload, message) {
    super(message || ERRORS[code] || `操作未完成（${code}）。`);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.payload = payload || null;
    this.eventRef = payload && payload.event_ref ? payload.event_ref : null;
    this.uncertain = code === 'NETWORK' || code === 'UNPARSEABLE' || status === 503;
  }
}

async function api(path, options = {}) {
  const { method = 'GET', body, key } = options;
  const headers = {};
  if (method !== 'GET') {
    headers['X-CSRFToken'] = cookie('csrftoken');
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (key) headers['Idempotency-Key'] = key;
  }
  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store'
    });
  } catch (e) {
    throw new ApiError('NETWORK', 0, null);
  }
  const type = res.headers.get('content-type') || '';
  let data = null;
  if (type.includes('application/json')) {
    try { data = await res.json(); } catch (e) { throw new ApiError('UNPARSEABLE', res.status, null); }
  }
  if (!res.ok || (data && data.ok === false)) {
    if (!data) throw new ApiError(res.status === 403 ? 'CSRF' : 'UNPARSEABLE', res.status, null);
    const code = (data.error && data.error.code) || 'UNKNOWN';
    throw new ApiError(code, res.status, data);
  }
  return data;
}

function opKey(signature) {
  if (!state.pending.has(signature)) state.pending.set(signature, uuid());
  return state.pending.get(signature);
}
function endOp(signature) { state.pending.delete(signature); }

function signature(path, body) {
  return path + '|' + JSON.stringify(body);
}

async function write(path, body, options = {}) {
  const sig = signature(path, body);
  const key = opKey(sig);
  try {
    const data = await api(path, { method: 'POST', body, key });
    endOp(sig);
    return data;
  } catch (e) {
    if (e.code === 'GUARD_BLOCKED' || e.status === 401 || e.status === 403 || e.status === 409) endOp(sig);
    throw e;
  }
}

/* ---------------- 会话与路由 ---------------- */

function homeFor() {
  return '#/' + (state.session ? ROLE_HOME[state.session.role] : '');
}

function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, '');
  const parts = raw.split('/').filter(Boolean);
  return { name: parts[0] || (state.session ? ROLE_HOME[state.session.role] : ''), id: parts[1] || '' };
}

function navigate(path, replace = false) {
  if (replace) {
    history.replaceState(null, '', path);
    return renderRoute();
  }
  if (location.hash === path) return renderRoute();
  location.hash = path;
}

function routeAllowed(route) {
  const role = state.session && state.session.role;
  if (!role) return false;
  const allowed = {
    frontdesk: ['receptions'],
    doctor: ['clinical', 'alerts'],
    manager: ['governance', 'audit']
  }[role] || [];
  return allowed.includes(route.name);
}

async function refreshSession() {
  try {
    const s = await api('/api/session');
    state.session = s && s.authenticated ? s : null;
  } catch (e) {
    state.session = null;
    state.bootError = e;
  }
  return state.session;
}

/* ---------------- 提示与弹窗 ---------------- */

function toast(message, kind = 'ok', detail = '') {
  const box = document.createElement('div');
  box.className = 'toast ' + kind;
  box.innerHTML = `<span class="mark2">${kind === 'err' ? '!' : '✓'}</span><div><b>${esc(message)}</b>${detail ? `<p>${esc(detail)}</p>` : ''}</div>`;
  toastRoot.appendChild(box);
  setTimeout(() => { box.style.opacity = '0'; box.style.transition = 'opacity .25s'; setTimeout(() => box.remove(), 260); }, 4800);
}

function openModal({ title, subtitle = '', body = '', foot = '', wide = false, kind = '', draftKey = '' }) {
  state.modal = { kind, draftKey };
  state.reloadLatest = null;
  modalRoot.innerHTML = `
    <div class="modal-backdrop">
      <div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-head">
          <div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ''}</div>
          <button class="modal-close" data-action="close-modal" aria-label="关闭">×</button>
        </div>
        <div class="modal-body">
          <div class="modal-error hide" id="modal-error" role="alert"></div>
          ${body}
        </div>
        ${foot ? `<div class="modal-foot">${foot}</div>` : ''}
      </div>
    </div>`;
  const bd = $('.modal-backdrop', modalRoot);
  bd.addEventListener('mousedown', (e) => { if (e.target === bd) closeModal(); });
  document.body.style.overflow = 'hidden';
  if (draftKey) {
    const form = $('form[data-draft]', modalRoot);
    const draft = loadDraft(draftKey);
    if (form && draft) {
      Object.entries(draft).forEach(([k, v]) => {
        const field = form.elements[k];
        if (field && field.type !== 'hidden') field.value = v;
      });
    }
  }
  setTimeout(() => {
    const first = $('.modal-body input:not([type=hidden]):not([disabled]), .modal-body textarea:not([disabled]), .modal-body select:not([disabled])', modalRoot);
    if (first) first.focus();
  }, 40);
}

function closeModal() {
  state.modal = null;
  state.reloadLatest = null;
  modalRoot.innerHTML = '';
  document.body.style.overflow = '';
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && modalRoot.innerHTML) closeModal();
});

function modalError(message) {
  const box = $('#modal-error');
  if (!box) return;
  box.classList.remove('hide');
  box.textContent = message;
  box.scrollIntoView({ block: 'nearest' });
}
function clearModalError() {
  const box = $('#modal-error');
  if (box) { box.classList.add('hide'); box.textContent = ''; }
}

function blockedBanner(err, extra = '') {
  const box = $('#modal-error');
  if (!box) return;
  box.classList.remove('hide');
  box.innerHTML = `
    <div class="blocked-banner">
      <div>
        <b>写入已阻断，病历未改变</b>
        <p>护栏命中，本次操作没有生成或覆盖临床内容；事件与告警已保留。${extra ? ' ' + esc(extra) : ''}</p>
        <div class="blocked-ref">校验事件 ${esc(err.eventRef || '—')}</div>
      </div>
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <button type="button" class="btn sm danger" data-action="go-alerts">查看告警</button>
      <button type="button" class="btn sm ghost" data-action="stay">保留输入，继续核对</button>
    </div>`;
  box.scrollIntoView({ block: 'nearest' });
}

function conflictBanner(message) {
  const box = $('#modal-error');
  if (!box) return;
  box.classList.remove('hide');
  box.innerHTML = `
    <div class="blocked-banner" style="border-color:var(--rust-line);background:var(--rust-wash)">
      <div><b style="color:var(--rust-deep)">记录版本已变化</b>
        <p style="color:var(--rust-deep)">${esc(message)} 本次输入已保留。取回最新版本后，可在此基础上继续提交。</p></div>
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <button type="button" class="btn sm" data-action="reload-latest">取回最新版本并保留已填内容</button>
    </div>`;
  box.scrollIntoView({ block: 'nearest' });
}

function formError(form, message) {
  const box = $('#modal-error');
  if (box) { modalError(message); return; }
  let existing = $('.form-error', form);
  if (!existing) {
    existing = document.createElement('div');
    existing.className = 'form-error';
    existing.setAttribute('role', 'alert');
    form.prepend(existing);
  }
  existing.textContent = message;
  existing.scrollIntoView({ block: 'nearest' });
}
function clearFormErrors(form) {
  $$('.field.invalid', form).forEach((f) => f.classList.remove('invalid'));
  $$('.field .err', form).forEach((e) => e.remove());
  const local = $('.form-error', form);
  if (local) local.remove();
  clearModalError();
}
function markField(form, name, message) {
  const input = form.elements[name];
  if (!input) return;
  const field = input.closest('.field') || input.parentElement;
  if (!field) return;
  field.classList.add('invalid');
  let err = $('.err', field);
  if (!err) { err = document.createElement('p'); err.className = 'err'; field.appendChild(err); }
  err.textContent = message;
}
function fieldWrap(label, name, input, hint = '', required = false) {
  return `<div class="field"><label for="f-${name}">${esc(label)}${required ? ' <b>*</b>' : ''}</label>${input}${hint ? `<p class="hint">${esc(hint)}</p>` : ''}</div>`;
}
function inputHtml(name, value, opts = {}) {
  return `<input id="f-${name}" name="${name}" type="${opts.type || 'text'}" value="${esc(value || '')}" ${opts.placeholder ? `placeholder="${esc(opts.placeholder)}"` : ''} ${opts.maxlength ? `maxlength="${opts.maxlength}"` : ''} ${opts.readonly ? 'readonly' : ''} ${opts.autocomplete ? `autocomplete="${opts.autocomplete}"` : ''}>`;
}
function textareaHtml(name, value, opts = {}) {
  return `<textarea id="f-${name}" name="${name}" ${opts.maxlength ? `maxlength="${opts.maxlength}"` : ''} ${opts.placeholder ? `placeholder="${esc(opts.placeholder)}"` : ''}>${esc(value || '')}</textarea>`;
}

/* ---------------- 登录 ---------------- */

function renderLogin() {
  closeModal();
  const notice = state.bootError ? `<div class="login-error">无法核对会话：${esc(state.bootError.message)} 仍可直接尝试登录。</div>` : '';
  root.innerHTML = `
  <div class="login">
    <section class="login-brand">
      <div class="login-top">
        <span class="mark">护</span>
        <span class="brand-text"><b>门诊护栏</b><small>CLINIC GUARDRAILS</small></span>
      </div>
      <div class="login-quote">
        <p class="login-title">让每一次写入都有边界，<br>让每一次读取都有留痕。</p>
        <p class="login-line">接诊、病历、用药与治理共用一个受控工作台。前台只写接待，医生只写本人接诊，管理只读治理与脱敏审计；更正保留原项，阻断不写病历。</p>
        <ol class="principles">
          <li class="principle"><b>01</b><div><strong>角色边界</strong><span>字段与数据范围由账号、门店和接待/接诊归属判定，页面隐藏按钮不代替服务端鉴权。</span></div></li>
          <li class="principle"><b>02</b><div><strong>版本不可覆盖</strong><span>更正生成新版本，原项与旧版本保留；历史按当时模板解释。</span></div></li>
          <li class="principle"><b>03</b><div><strong>阻断即留痕</strong><span>护栏命中时病历不改变，校验事件与告警保留，由负责医生核对处理。</span></div></li>
        </ol>
      </div>
      <div class="login-foot"><span>T22 · 门店内部工作台</span><span>授权账号登录</span></div>
    </section>
    <section class="login-side">
      <div class="login-card">
        <h1>登录工作台</h1>
        <p class="hint">使用门店分配的内部账号。宠物主人没有登录端。</p>
        ${notice}
        <form class="login-form" data-form="login" novalidate>
          <div class="field">
            <label for="login-username">账号</label>
            <input id="login-username" name="username" type="text" autocomplete="username" maxlength="150" required>
          </div>
          <div class="field">
            <label for="login-password">密码</label>
            <input id="login-password" name="password" type="password" autocomplete="current-password" maxlength="256" required>
          </div>
          <div class="login-error hide" id="login-error" role="alert"></div>
          <button class="btn primary" type="submit">登录</button>
        </form>
        <p class="login-notice">会话 Cookie 为 HttpOnly，页面不保存口令。登录后按当前账号重新核验角色与数据范围；读取和写入都会记录审计事件。</p>
      </div>
    </section>
  </div>`;
  const input = $('#login-username');
  if (input) setTimeout(() => input.focus(), 60);
}

async function doLogin(form) {
  const username = form.elements.username.value.trim();
  const password = form.elements.password.value;
  const box = $('#login-error');
  box.classList.add('hide');
  if (!username || !password) {
    box.textContent = '请输入账号与密码。';
    box.classList.remove('hide');
    return;
  }
  const btn = $('button[type=submit]', form);
  btn.disabled = true;
  btn.textContent = '正在登录…';
  try {
    await api('/api/login', { method: 'POST', body: { username, password } });
    await refreshSession();
    if (!state.session) throw new Error('登录后未能取得当前账号身份。');
    await enterApp();
  } catch (e) {
    box.textContent = e.code === 'INVALID_CREDENTIALS' ? '账号或密码不正确。' : e.message;
    box.classList.remove('hide');
    box.scrollIntoView({ block: 'nearest' });
  } finally {
    btn.disabled = false;
    btn.textContent = '登录';
  }
}

/* ---------------- 骨架 ---------------- */

function navConfig() {
  const role = state.session.role;
  if (role === 'frontdesk') {
    return [
      { key: 'receptions', num: '01', label: '接诊台', href: '#/receptions' },
      { key: 'new', num: '02', label: '新建接诊', href: '#/receptions/new' }
    ];
  }
  if (role === 'doctor') {
    const unhandled = unhandledCount();
    return [
      { key: 'clinical', num: '01', label: '诊疗队列', href: '#/clinical' },
      { key: 'alerts', num: '02', label: '告警', href: '#/alerts', badge: unhandled }
    ];
  }
  return [
    { key: 'governance', num: '01', label: '治理台', href: '#/governance' },
    { key: 'audit', num: '02', label: '脱敏审计', href: '#/audit' }
  ];
}

function unhandledCount() {
  const sb = state.sidebar;
  if (!sb || !Array.isArray(sb.alerts)) return 0;
  return sb.alerts.filter((a) => a.handling_state === 'unhandled').length;
}

function sidebarItems() {
  const sb = state.sidebar;
  if (!sb) return [];
  if (state.session.role === 'manager') return [];
  return Array.isArray(sb.items) ? sb.items : [];
}

function railListHtml() {
  const role = state.session.role;
  if (role === 'manager') {
    const sb = state.sidebar || {};
    const items = Array.isArray(sb.items) ? sb.items.slice(0, 5) : [];
    return `
      <div class="rail-title">治理摘要 <span>GEN ${esc(sb.generation ?? '—')}</span></div>
      <div class="rail-list">
        <div class="case-mini"><b>当前生效</b><p>${sb.active_candidate_ref ? shortRef(sb.active_candidate_ref) : '尚无生效候选'}</p><time>候选 ${items.length} 个</time></div>
        ${items.map((c) => `<a class="case-mini ${state.selectedCandidate === c.candidate_ref ? 'on' : ''}" href="#/governance/${esc(c.candidate_ref)}">
          <b><i class="${c.state === 'approved' ? '' : c.state === 'rejected' ? 'err' : 'wait'}"></i>${esc(CANDIDATE_STATES[c.state] || c.state)} · ${esc(c.version || '')}</b>
          <p>${esc(shortRef(c.candidate_ref))}</p><time>${esc(fmtTime(c.created_at))}</time>
        </a>`).join('')}
      </div>`;
  }
  const items = sidebarItems();
  const title = role === 'doctor' ? '今日接诊' : '最近接待';
  const rows = items.slice(0, 6).map((item) => {
    const r = role === 'doctor' ? item.reception : item;
    if (!r) return '';
    const id = r.encounter_ref;
    const active = state.route && state.route.id === id;
    const wait = role === 'doctor' && item.clinical === null;
    const err = r.error_mark;
    return `<a class="case-mini ${active ? 'on' : ''}" href="#/${role === 'doctor' ? 'clinical' : 'receptions'}/${esc(id)}">
      <b><i class="${err ? 'err' : wait ? 'wait' : ''}"></i>${esc(r.pet_name || '未命名宠物')}</b>
      <p>${esc(r.species || '')} · ${esc(r.complaint || '')}</p>
      <time>${esc(fmtSlot(r.created_at))}${role === 'doctor' ? (item.clinical ? ' · 病历' : ' · 待诊断') : ''}</time>
    </a>`;
  }).join('');
  return `<div class="rail-title">${title} <span>${esc(state.sidebar && state.sidebar.total != null ? state.sidebar.total : items.length)}</span></div>
    <div class="rail-list">${rows || '<div class="case-mini"><p>当前范围暂无记录</p></div>'}</div>`;
}

function navHtml() {
  const route = state.route || parseRoute();
  return navConfig().map((n) => {
    const on = route.name === n.key || (n.key === 'receptions' && route.name === 'receptions') ||
      (n.key === 'clinical' && route.name === 'clinical');
    return `<a class="nav-item ${on ? 'on' : ''}" href="${n.href}" data-nav="${n.key}" title="${esc(n.label)}">
      <span class="nav-num">${n.num}</span><span class="nav-label">${esc(n.label)}</span>
      ${n.badge ? `<span class="nav-badge">${n.badge}</span>` : ''}
    </a>`;
  }).join('');
}

function renderShell() {
  const s = state.session;
  const sb = state.sidebar || {};
  root.innerHTML = `
  <div class="app">
    <header class="masthead">
      <div class="brand" style="display:flex;align-items:center;gap:11px">
        <span class="mark">护</span>
        <span class="brand-text"><b>门诊护栏</b><small>CLINIC GUARDRAILS</small></span>
      </div>
      <div class="mast-meta">
        <span class="store">${esc(s.store_name || s.store_ref)}</span>
        <span class="sep"></span>
        <span class="userline"><span class="avatar">${esc((s.username || '?').slice(0, 1).toUpperCase())}</span>${esc(s.username)}</span>
        <span class="role">${esc(ROLES[s.role] || s.role)}</span>
        <span class="sep"></span>
        <button class="btn ghost sm" data-action="logout">退出</button>
      </div>
    </header>
    <div class="shell">
      <aside class="rail">
        <nav class="rail-nav">${navHtml()}</nav>
        <div class="rail-block">${railListHtml()}</div>
        <div class="rail-foot">
          <span id="rail-policy">${s.role === 'manager' ? '生效 ' + esc(sb.generation ?? '—') : '受控读取'}</span>
          <span>会话 8 小时</span>
        </div>
      </aside>
      <main class="main"><div class="page" id="view"></div></main>
    </div>
  </div>`;
}

function updateShell() {
  if (!state.session) return;
  const nav = $('.rail-nav');
  if (nav) nav.innerHTML = navHtml();
  const block = $('.rail-block');
  if (block) block.innerHTML = railListHtml();
  const store = $('.mast-meta .store');
  if (store) store.textContent = state.session.store_name || state.session.store_ref;
  const policy = $('#rail-policy');
  if (policy) policy.textContent = state.session.role === 'manager'
    ? '生效 ' + ((state.sidebar && state.sidebar.generation) ?? '—')
    : '受控读取';
}

async function enterApp() {
  state.bootError = null;
  state.receptionPage = 1;
  state.selectedCandidate = '';
  await loadSidebar();
  renderShell();
  const route = parseRoute();
  if (!routeAllowed(route)) history.replaceState(null, '', homeFor());
  await renderRoute();
}

async function loadSidebar() {
  const role = state.session.role;
  try {
    if (role === 'frontdesk') {
      const d = await api('/api/receptions');
      state.sidebar = { items: d.items || [], total: d.total };
    } else if (role === 'doctor') {
      const [c, a] = await Promise.all([api('/api/clinical/encounters'), api('/api/clinical/alerts')]);
      state.sidebar = { items: c.items || [], total: c.total, alerts: a.items || [] };
    } else {
      const d = await api('/api/candidates');
      state.sidebar = { ...d, items: d.items || [] };
    }
  } catch (e) {
    state.sidebar = { items: [], total: 0, alerts: [] };
  }
}

/* ---------------- 路由渲染 ---------------- */

function setActiveNav() {
  const route = state.route;
  $$('.nav-item').forEach((a) => {
    const key = a.dataset.nav;
    const on = (key === 'receptions' && route.name === 'receptions') ||
      (key === 'clinical' && route.name === 'clinical') ||
      (key === 'alerts' && route.name === 'alerts') ||
      (key === 'governance' && route.name === 'governance') ||
      (key === 'audit' && route.name === 'audit');
    a.classList.toggle('on', on);
  });
}

function loadingView(label = '正在读取获准范围') {
  return `<div class="view" aria-busy="true">
    <div class="sk-head"><div style="flex:1"><div class="sk-title"></div><div class="sk-line" style="width:56%"></div></div><div class="sk-line" style="width:118px;height:28px"></div></div>
    <div class="detail-grid">
      <div class="sk-sheet">
        <div class="sk-line" style="width:30%;height:16px;margin-bottom:18px"></div>
        <div class="sk-line" style="width:92%"></div><div class="sk-line" style="width:74%"></div>
        <div class="sk-line" style="width:30%;height:16px;margin-top:26px"></div>
        <div class="sk-line" style="width:88%"></div><div class="sk-line" style="width:64%"></div>
        <div class="sk-line" style="width:30%;height:16px;margin-top:26px"></div>
        <div class="sk-line" style="width:84%"></div><div class="sk-line" style="width:70%"></div>
      </div>
      <aside class="side sk-side"><div class="sk-line" style="width:100%;height:16px"></div><div class="sk-line" style="width:80%"></div><div class="sk-line" style="width:88%"></div><div class="sk-line" style="width:60%"></div></aside>
    </div>
    <span class="sr-only">${esc(label)}</span>
  </div>`;
}

function viewError(e) {
  const message = e instanceof ApiError ? e.message : (e && e.message) || '页面暂无法载入。';
  const title = e && e.status === 403 ? '无权读取该目标' : e && e.status === 401 ? '登录已失效' : '页面暂无法载入';
  const action = e && e.status === 401 ? '<button class="btn" data-action="relogin">重新登录</button>' : `<button class="btn ghost" data-action="reload">刷新</button><a class="btn ghost" href="${homeFor()}">返回工作区</a>`;
  return `<div class="view"><section class="error-sheet">
    <div class="error-head"><h1>${esc(title)}</h1><span class="stamp blocked">${e && e.status === 403 ? '拒绝读取' : '未完成'}</span></div>
    <p>${esc(message)}</p>
    <p class="read-note">服务端已拒绝或未能完成本次读取；页面不展示任何未获准的正文。可以返回工作区，或刷新后重新核对当前账号范围。</p>
    <div class="error-actions">${action}</div>
  </section></div>`;
}

async function renderRoute() {
  if (!state.session) { renderLogin(); return; }
  const route = parseRoute();
  if (!routeAllowed(route)) {
    history.replaceState(null, '', homeFor());
    return renderRoute();
  }
  state.route = route;
  const main = $('.main');
  if (main) main.scrollTop = 0;
  setActiveNav();
  const view = $('#view');
  if (!view) { renderShell(); return renderRoute(); }
  view.innerHTML = loadingView();
  document.title = '门诊护栏 · ' + (ROLES[state.session.role] || '');
  try {
    if (route.name === 'receptions' && !route.id) await viewReceptions(view);
    else if (route.name === 'receptions' && route.id === 'new') await viewNewReception(view);
    else if (route.name === 'receptions') await viewReceptionDetail(view, route.id);
    else if (route.name === 'clinical' && !route.id) await viewClinicalList(view);
    else if (route.name === 'clinical') await viewClinicalDetail(view, route.id);
    else if (route.name === 'alerts') await viewAlerts(view);
    else if (route.name === 'governance' && !route.id) await viewGovernance(view);
    else if (route.name === 'governance') await viewGovernance(view, route.id);
    else if (route.name === 'audit') await viewAudit(view);
    else view.innerHTML = viewError(new Error('未知页面。'));
  } catch (e) {
    view.innerHTML = viewError(e);
    if (e instanceof ApiError && e.status === 401) toast('登录已失效', 'err', '请重新登录后继续。');
  }
  updateShell();
}

/* ---------------- 前台 ---------------- */

function doctorName(ref) {
  return ref == null ? '—' : '账号 #' + ref;
}

function receptionVersion(r) {
  const vs = r.reception_versions || [];
  return vs.length ? vs[vs.length - 1].version : 1;
}

async function viewReceptions(view) {
  const params = {};
  const tr = rangeParam(state.range);
  if (tr) params.time_range = tr;
  if (state.receptionPage && state.receptionPage > 1) params.page = String(state.receptionPage);
  const [data, doctorsData] = await Promise.all([
    api('/api/receptions' + (Object.keys(params).length ? '?' + qs(params) : '')),
    api('/api/encounters/create-options/doctors').catch(() => ({ doctor_options: [] }))
  ]);
  const doctors = {};
  (doctorsData.doctor_options || []).forEach((d) => { doctors[d.doctor_ref] = d.display_name; });
  const items = data.items || [];
  const total = data.total != null ? data.total : items.length;
  const page = data.page || 1;
  const pages = Math.max(1, Math.ceil(total / (data.page_size || 50)));

  const rows = items.map((r) => {
    const name = r.pet_name || '未命名宠物';
    const v = receptionVersion(r);
    return `<a class="row" href="#/receptions/${esc(r.encounter_ref)}">
      <div class="row-main">
        <div class="row-title">${esc(name)}<span class="species">${esc(r.species || '—')}</span></div>
        <div class="row-sub">宠物 ${esc(shortRef(r.pet_ref, 8))}</div>
      </div>
      <div class="row-mid">${esc(r.complaint || '')}</div>
      <div class="row-time"><b>${esc(doctors[r.doctor_ref] || '账号 #' + r.doctor_ref)}</b></div>
      <div class="row-time"><b>${esc(fmtTime(r.created_at))}</b></div>
      <div class="row-status">${r.error_mark ? chip('误建', 'danger') : chip('v' + v, v > 1 ? 'warn' : 'muted')}${v > 1 && !r.error_mark ? chip('已更正', 'muted') : ''}</div>
      <div class="row-arrow">→</div>
    </a>`;
  }).join('');

  view.innerHTML = `<div class="view">
    <header class="page-head">
      <div>
        <h1>接诊台</h1>
        <p class="lede">仅显示 ${esc(state.session.store_name)} 中由当前账号接待的资料。前台读取范围内不含诊断、检查与处方正文。</p>
      </div>
      <div class="head-side"><div class="head-actions"><a class="btn primary" href="#/receptions/new">新建接诊</a></div></div>
    </header>
    <div class="toolbar">
      <div class="seg" role="group" aria-label="时间范围">
        ${['today', '7d', 'all'].map((r) => `<button type="button" class="${state.range === r ? 'on' : ''}" data-action="range" data-range="${r}">${r === 'today' ? '今日' : r === '7d' ? '近 7 天' : '全部'}</button>`).join('')}
      </div>
      <form class="lookup" data-form="lookup-reception" novalidate>
        <label for="lookup-ref">按完整编号定位</label>
        <input id="lookup-ref" name="ref" placeholder="粘贴完整 UUID（36 位）" autocomplete="off">
        <button class="btn sm" type="submit">定位</button>
      </form>
      <span class="count">共 ${total} 条 · 第 ${page} / ${pages} 页</span>
    </div>
    <div class="sheet list">
      <div class="list-head"><span>宠物</span><span>主诉</span><span>接诊医生</span><span>建档时间</span><span>状态</span><span>打开</span></div>
      ${rows || `<div class="list-empty"><b>当前范围暂无接待记录</b><p>可以切换时间范围，或新建一次接诊。</p></div>`}
    </div>
    ${pages > 1 ? `<div style="display:flex;justify-content:flex-end;align-items:center;gap:8px;margin-top:12px">
      <button class="btn sm ghost" data-action="reception-page" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
      <span class="count">第 ${page} / ${pages} 页</span>
      <button class="btn sm ghost" data-action="reception-page" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>下一页</button>
    </div>` : ''}
    <footer class="page-foot"><span>前台只处理本店由其接待的资料 · 读取已记录审计</span><span>第 ${page} 页</span></footer>
  </div>`;
}

async function viewNewReception(view) {
  let doctors = [];
  try {
    const d = await api('/api/encounters/create-options/doctors');
    doctors = d.doctor_options || [];
  } catch (e) {
    view.innerHTML = viewError(e);
    return;
  }
  const draft = loadDraft('new-reception') || {};
  const identity = draft.identity === 'known' ? 'known' : 'new';
  const petRef = draft.pet_ref || uuid();
  const species = draft.species || '';
  const complaint = draft.complaint || '';
  const petName = draft.pet_name || '';
  const knownRef = draft.known_ref || '';
  const doctorRef = draft.doctor_ref || (doctors[0] ? String(doctors[0].doctor_ref) : '');
  const noDoctor = doctors.length === 0;

  view.innerHTML = `<div class="view">
    <div class="crumbs"><a href="#/receptions">接诊台</a><span>/</span>新建接诊</div>
    <header class="page-head">
      <div>
        <h1>建立接诊</h1>
        <p class="lede">先确认宠物身份，再填写物种、主诉和接诊医生。接待资料一经建立即固定归属，后续更正保留原版本。</p>
      </div>
      <div class="head-side"><div class="head-actions"><button class="btn primary" type="submit" form="new-reception-form" ${noDoctor ? 'disabled' : ''}>建立接诊</button><span class="stamp">归属固定</span></div></div>
    </header>
    <div class="detail-grid">
      <form id="new-reception-form" class="sheet form-sheet" data-form="new-reception" data-draft="new-reception" novalidate>
        <div class="form-grid">
          <div class="field wide">
            <span class="field-label">宠物身份 <b style="color:var(--rust)">*</b></span>
            <div class="choice-row">
              <label class="choice ${identity === 'new' ? 'on' : ''}">
                <input type="radio" name="identity" value="new" ${identity === 'new' ? 'checked' : ''}>
                <b>新宠物</b><span>由系统生成本店宠物编号，建立新的身份档案。</span>
              </label>
              <label class="choice ${identity === 'known' ? 'on' : ''}">
                <input type="radio" name="identity" value="known" ${identity === 'known' ? 'checked' : ''}>
                <b>已知宠物</b><span>输入已有编号并核验可关联，不返回历史病历。</span>
              </label>
            </div>
          </div>

          <div class="field wide ${identity === 'new' ? '' : 'hide'}" id="new-pet-block">
            <label for="f-pet_ref">新宠物编号</label>
            <div class="input-row">
              <input id="f-pet_ref" name="pet_ref" class="mono" type="text" value="${esc(petRef)}" readonly>
              <button class="btn sm ghost" type="button" data-action="regen-pet">重新生成</button>
            </div>
            <p class="hint">编号由浏览器生成并提交，作为本店宠物档案标识；不包含主人信息。</p>
          </div>

          <div class="field wide ${identity === 'known' ? '' : 'hide'}" id="known-pet-block">
            <label for="f-known_ref">已知宠物编号</label>
            <div class="input-row">
              <input id="f-known_ref" name="known_ref" class="mono" type="text" value="${esc(knownRef)}" placeholder="完整 UUID" autocomplete="off">
              <button class="btn sm ghost" type="button" data-action="verify-pet">核验编号</button>
            </div>
            <p class="verify-line" id="verify-line">核验只在本店范围内判断编号是否可关联，不返回旧病历。</p>
          </div>

          <div class="field">
            <label for="f-species">物种 <b style="color:var(--rust)">*</b></label>
            <input id="f-species" name="species" type="text" maxlength="100" value="${esc(species)}" placeholder="如：猫、犬">
            <div class="quick">${['猫', '犬', '兔', '鸟'].map((s) => `<button type="button" data-action="species" data-species="${s}">${s}</button>`).join('')}</div>
          </div>

          <div class="field">
            <label for="f-pet_name">宠物名称（可选）</label>
            <input id="f-pet_name" name="pet_name" type="text" maxlength="100" value="${esc(petName)}" placeholder="现场称呼，可留空">
          </div>

          <div class="field wide">
            <label for="f-complaint">主诉 <b style="color:var(--rust)">*</b><span class="char-count" id="complaint-count">${complaint.length} / 10000</span></label>
            <textarea id="f-complaint" name="complaint" maxlength="10000" placeholder="记录到院时的主要诉求，不写诊断结论">${esc(complaint)}</textarea>
          </div>

          <div class="field wide">
            <label for="f-doctor_ref">接诊医生 <b style="color:var(--rust)">*</b></label>
            <select id="f-doctor_ref" name="doctor_ref" ${noDoctor ? 'disabled' : ''}>
              ${doctors.map((d) => `<option value="${esc(d.doctor_ref)}" ${String(d.doctor_ref) === String(doctorRef) ? 'selected' : ''}>${esc(d.display_name)} · 账号 #${esc(d.doctor_ref)}</option>`).join('')}
            </select>
            <p class="hint">${noDoctor ? '本店暂无可用接诊医生，暂时不能建档。' : '列表来自本店当前有效医生账号，提交时由服务端再次核验。'}</p>
          </div>
        </div>
        <div class="form-actions">
          <button class="btn primary" type="submit" ${noDoctor ? 'disabled' : ''}>建立接诊</button>
          <a class="btn ghost" href="#/receptions">取消</a>
          <span class="form-note">提交成功后生成接待记录 v1，并记录审计事件。</span>
        </div>
      </form>
      <aside class="side">
        <section class="panel"><div class="panel-head"><span class="panel-label">建档规则</span></div>
          <div class="aside-note"><ol>
            <li><b>身份先定</b>新宠物生成新编号；已知宠物必须在本店可关联，核验不返回旧病历。</li>
            <li><b>归属固定</b>建档后接待人与接诊医生固定，复诊或参数修改不能改写历史归属。</li>
            <li><b>更正留痕</b>资料有误由前台提交更正，生成新版本并保留原版本。</li>
            <li><b>正文隔离</b>前台不读取临床诊断、检查与处方正文。</li>
          </ol></div>
        </section>
      </aside>
    </div>
  </div>`;
  const ta = $('#f-complaint');
  if (ta) {
    ta.addEventListener('input', () => { const c = $('#complaint-count'); if (c) c.textContent = ta.value.length + ' / 10000'; });
  }
  const sel = $('#f-doctor_ref');
  if (sel && draft.doctor_ref) {
    const exists = Array.from(sel.options).some((o) => o.value === String(draft.doctor_ref));
    if (exists) sel.value = String(draft.doctor_ref);
  }
}

async function viewReceptionDetail(view, id) {
  const r = await api('/api/receptions/' + encodeURIComponent(id));
  const vs = (r.reception_versions || []).slice().sort((a, b) => a.version - b.version);
  const latest = vs[vs.length - 1] || null;
  const version = latest ? latest.version : 1;
  const created = fmtTime(r.created_at);

  view.innerHTML = `<div class="view">
    <div class="crumbs"><a href="#/receptions">接诊台</a><span>/</span>接诊 ${esc(shortRef(r.encounter_ref, 8))}</div>
    <header class="page-head">
      <div>
        <h1>${esc(r.pet_name || '未命名宠物')}</h1>
        <p class="lede">${esc(r.species || '未记录物种')} · 接待资料由 ${esc(state.session.username)} 读取。</p>
      </div>
      <div class="head-side">
        <div class="head-meta">
          <div class="meta-item"><span>接诊编号</span>${refCode(r.encounter_ref, 8)}</div>
          <div class="meta-item"><span>建档时间</span><code>${esc(created)}</code></div>
          <div class="meta-item"><span>当前版本</span><code>v${esc(version)}</code></div>
        </div>
        <div class="head-actions">
          <a class="btn ghost sm" href="#/receptions">返回</a>
          <button class="btn sm" data-action="reception-correct" data-id="${esc(r.encounter_ref)}" ${latest ? '' : 'disabled'}>更正资料</button>
          <button class="btn danger sm" data-action="reception-misreg" data-id="${esc(r.encounter_ref)}" ${r.error_mark ? 'disabled' : ''}>标记误建</button>
        </div>
      </div>
    </header>

    <div class="detail-grid">
      <section class="sheet">
        <header class="sheet-head">
          <div>
            <h2 class="sheet-title">接待记录 <span class="chip muted">v${esc(version)}</span>${r.error_mark ? chip('误建', 'danger') : ''}</h2>
            <span class="sheet-sub">RECEPTION · 归属固定 · 版本保留</span>
          </div>
          <span class="stamp ${r.error_mark ? 'blocked' : ''}">${r.error_mark ? '误建 · 保留' : '已建档'}</span>
        </header>

        <div class="block">
          <span class="idx">01</span>
          <div class="block-body">
            <div class="block-head"><h2>接待资料</h2><em>前台可更正；医生只读</em></div>
            <dl class="fields">
              <div><dt>宠物名称</dt><dd>${esc(r.pet_name || '未填写')}</dd></div>
              <div><dt>物种</dt><dd>${esc(r.species || '—')}</dd></div>
              <div class="wide"><dt>主诉</dt><dd>${esc(r.complaint || '')}</dd></div>
              <div><dt>宠物编号</dt><dd>${refCode(r.pet_ref)}</dd></div>
              <div><dt>接诊医生</dt><dd>${esc(doctorName(r.doctor_ref))}</dd></div>
              <div><dt>接待账号</dt><dd>${esc(doctorName(r.receptionist_ref))}</dd></div>
              <div><dt>所属门店</dt><dd>${refCode(r.store_ref)}</dd></div>
            </dl>
            ${r.error_mark ? `<div class="panel" style="margin-top:14px;box-shadow:none">
              <div class="panel-head"><span class="panel-label">误建标记</span><span class="panel-count">记录保留</span></div>
              <div class="panel-body"><p class="read-note"><b style="color:var(--rust)">${esc(r.error_reason || '未填写原因')}</b><br>原记录与原告警保留，不因标记删除；${(r.related_encounter_refs || []).length ? '关联重建接诊 ' + refCode(r.related_encounter_refs[0]) : '未关联重建接诊'}。</p></div>
            </div>` : ''}
          </div>
        </div>

        <div class="block">
          <span class="idx">02</span>
          <div class="block-body">
            <div class="block-head"><h2>版本留痕</h2><em>${vs.length} 个版本 · 原版本不可覆盖</em></div>
            <ol class="versions">
              ${vs.slice().reverse().map((v) => `<li class="ver ${v.version === version ? 'now' : ''}">
                <span class="vno">v${esc(v.version)}</span>
                <div><p>${esc(v.species)} · ${esc(v.complaint)}</p><small>${v.pet_name ? '宠物名 ' + esc(v.pet_name) + ' · ' : ''}${v.reason ? '理由：' + esc(v.reason) : '建档或更正未填写理由'} · 操作账号 #${esc(v.actor_ref)}</small></div>
                <time>${esc(fmtTime(v.created_at))}</time>
              </li>`).join('') || '<li class="ver"><div><p>暂无版本记录</p></div></li>'}
            </ol>
          </div>
        </div>

        <div class="block">
          <span class="idx">03</span>
          <div class="block-body">
            <div class="block-head"><h2>状态与关联</h2></div>
            <dl class="fields">
              <div><dt>记录状态</dt><dd>${r.error_mark ? '误建（原记录保留）' : '正常接待'}</dd></div>
              <div><dt>关联重建接诊</dt><dd>${(r.related_encounter_refs || []).length ? (r.related_encounter_refs || []).map((x) => refCode(x, 8)).join(' ') : '无'}</dd></div>
            </dl>
            <p class="note">误建标记不删除记录，也不改写历史归属；如已建立替代接诊，可在此核对关联编号。</p>
          </div>
        </div>
      </section>

      <aside class="side">
        <section class="panel"><div class="panel-head"><span class="panel-label">前台读取范围</span></div>
          <div class="panel-body"><p class="read-note">本页只返回接待资料与版本。临床诊断、检查、用药与建议正文由接诊医生范围读取，前台页面不提供入口，直接请求也会被服务端拒绝。</p></div>
        </section>
        <section class="panel"><div class="panel-head"><span class="panel-label">读取留痕</span></div>
          <div class="panel-body"><div class="read-line"><i></i><span>encounter.read · 成功</span></div><p class="read-note">本次读取已写入审计事件；审计只记录获准元数据，不复制主诉正文。</p></div>
        </section>
        <section class="panel"><div class="panel-head"><span class="panel-label">更正规则</span></div>
          <div class="panel-body"><p class="read-note">更正必须绑定当前版本并填写理由；提交后生成新版本，旧版本仍可核对。误建标记与更正分开操作，均不删除记录。</p></div>
        </section>
      </aside>
    </div>
    <footer class="page-foot"><span>接诊归属固定 · 前台只处理本店由其接待的资料</span><span>${esc(fmtFull(r.created_at))}</span></footer>
  </div>`;
}

/* ---------------- 医生 ---------------- */

function clinicalState(item) {
  if (!item || !item.reception) return { label: '未知', kind: 'muted' };
  if (item.reception.error_mark) return { label: '误建', kind: 'danger' };
  if (!item.clinical) return { label: '待诊断', kind: 'warn' };
  const vs = item.clinical.clinical_versions || [];
  const v = vs.length ? vs[vs.length - 1].version : 1;
  return { label: '病历 v' + v, kind: 'ok' };
}

async function viewClinicalList(view) {
  const params = {};
  const tr = rangeParam(state.range);
  if (tr) params.time_range = tr;
  const data = await api('/api/clinical/encounters' + (Object.keys(params).length ? '?' + qs(params) : ''));
  const items = data.items || [];
  const total = data.total != null ? data.total : items.length;

  const rows = items.map((item) => {
    const r = item.reception;
    const c = item.clinical;
    const st = clinicalState(item);
    const meds = c && c.medication_chain ? c.medication_chain.length : 0;
    const adopts = c && c.adoptions ? c.adoptions.length : 0;
    return `<a class="row" href="#/clinical/${esc(r.encounter_ref)}">
      <div class="row-main">
        <div class="row-title">${esc(r.pet_name || '未命名宠物')}<span class="species">${esc(r.species || '—')}</span></div>
        <div class="row-sub">接诊 ${esc(shortRef(r.encounter_ref, 8))}${c ? ' · 病历 ' + esc(shortRef(c.record_ref, 8)) : ''}</div>
      </div>
      <div class="row-mid">${esc(r.complaint || '')}</div>
      <div class="row-time"><b>${esc(fmtTime(r.created_at))}</b>建档</div>
      <div class="row-status">${chip(st.label, st.kind)}${meds ? chip(meds + ' 条用药', 'muted') : ''}${adopts ? chip('采纳 ' + adopts, 'muted') : ''}</div>
      <div class="row-arrow">→</div>
    </a>`;
  }).join('');

  view.innerHTML = `<div class="view">
    <header class="page-head">
      <div>
        <h1>诊疗队列</h1>
        <p class="lede">仅显示 ${esc(state.session.store_name)} 由当前账号负责接诊的临床内容。未处理的护栏告警持续保留，查看不会自动标记完成。</p>
      </div>
      <div class="head-side"><div class="head-actions">
        <button class="btn ghost sm" data-action="reload">刷新</button>
        <a class="btn sm" href="#/alerts">查看告警${unhandledCount() ? ' · ' + unhandledCount() : ''}</a>
      </div></div>
    </header>
    <div class="toolbar">
      <div class="seg" role="group" aria-label="时间范围">
        ${['today', '7d', 'all'].map((r) => `<button type="button" class="${state.range === r ? 'on' : ''}" data-action="range" data-range="${r}">${r === 'today' ? '今日' : r === '7d' ? '近 7 天' : '全部'}</button>`).join('')}
      </div>
      <span class="count">共 ${total} 条接诊 · 每人每页 50 条</span>
    </div>
    <div class="sheet list clinical">
      <div class="list-head"><span>宠物</span><span>主诉</span><span>建档时间</span><span>状态</span><span>打开</span></div>
      ${rows || `<div class="list-empty"><b>当前范围暂无接诊</b><p>前台建档并由本账号负责后，会出现在这里。</p></div>`}
    </div>
    <footer class="page-foot"><span>接诊归属固定 · 只读取本人负责范围 · 读取已记录审计</span><span>${esc(fmtFull(new Date().toISOString()))}</span></footer>
  </div>`;
}

function suggestionText(content) {
  const labels = { diagnosis: '诊断', examination: '检查', medication: '用药' };
  const parts = [];
  Object.entries(content || {}).forEach(([k, v]) => {
    if (v == null || v === '') return;
    if (k === 'medication' && typeof v === 'object') {
      parts.push(`<p><span>用药</span>${esc(v.drug || '')} ${esc(v.amount || '')}${esc(v.unit || '')}</p>`);
    } else if (typeof v === 'string') {
      parts.push(`<p><span>${esc(labels[k] || k)}</span>${esc(v)}</p>`);
    }
  });
  return parts.join('') || '<p class="muted">建议内容为空。</p>';
}

function sourceText(source) {
  if (!source) return '来源未标注';
  if (typeof source === 'string') return source;
  const label = source.label || source.type || '来源未标注';
  const version = source.version ? ' · ' + source.version : '';
  return label + version;
}

async function viewClinicalDetail(view, id) {
  const [item, sugRes, alertRes] = await Promise.all([
    api('/api/clinical/encounters/' + encodeURIComponent(id)),
    api('/api/clinical/encounters/' + encodeURIComponent(id) + '/suggestions').catch((e) => {
      if (e.status === 403) throw e;
      return { items: [] };
    }),
    api('/api/clinical/alerts').catch(() => ({ items: [] }))
  ]);
  const r = item.reception;
  const c = item.clinical;
  const suggestions = sugRes.items || [];
  const alerts = alertRes.items || [];
  const versions = c ? (c.clinical_versions || []).slice().sort((a, b) => a.version - b.version) : [];
  const current = versions[versions.length - 1] || null;
  const versionNo = current ? current.version : 0;
  const recAlerts = c ? alerts.filter((a) => a.record_ref === c.record_ref) : [];
  const unhandled = alerts.filter((a) => a.handling_state === 'unhandled');
  const chain = c ? (c.medication_chain || []) : [];
  const adoptions = c ? (c.adoptions || []) : [];
  const locked = r.error_mark;

  const medHtml = chain.length ? `<ol class="meds">${chain.map((m) => {
    const corr = !!m.original_entry_ref;
    return `<li class="med ${corr ? 'corr' : ''}">
      <div class="med-main">
        <span class="med-name">${esc(m.drug || '未填写药品')}</span>
        <span class="med-amt">${esc(m.amount || '—')} ${esc(m.unit || '')}</span>
        ${corr ? '<span class="med-tag">更正</span>' : ''}
        <span class="med-time">${esc(fmtTime(m.created_at))}</span>
      </div>
      <p class="med-why">${corr ? '关联 ' + esc(shortRef(m.original_entry_ref, 8)) + ' · ' + esc(m.reason || '未填写理由') : '原始项 ' + esc(shortRef(m.entry_ref, 8)) + (m.reason ? ' · ' + esc(m.reason) : '')} · 账号 #${esc(m.actor_ref)}</p>
      ${locked ? '' : `<div class="med-actions"><button class="btn sm ghost" data-action="med-correct" data-id="${esc(r.encounter_ref)}" data-entry="${esc(m.entry_ref)}">更正</button></div>`}
    </li>`;
  }).join('')}</ol>` : `<p class="note">尚无用药记录。追加用药须已存在正式病历，且必须随诊断版本一起解释。</p>`;

  const suggestionsHtml = suggestions.length ? suggestions.map((s) => {
    const pending = s.status === 'pending';
    return `<div class="sugg">
      <div class="src">来源 · ${esc(sourceText(s.source))}</div>
      ${suggestionText(s.content)}
      <div style="display:flex;align-items:center;gap:6px;margin-top:6px">
        ${s.status === 'pending' ? chip('待决定', 'warn') : s.status === 'adopted' ? chip('已采纳', 'ok') : chip('未采纳', 'muted')}
        ${s.decision && s.decision.created_at ? `<span class="med-time">${esc(fmtTime(s.decision.created_at))}</span>` : ''}
      </div>
      ${s.decision && s.decision.reason ? `<p class="note">决定理由：${esc(s.decision.reason)}</p>` : ''}
      ${pending && !locked ? `<div class="row"><button class="btn primary sm" data-action="adopt" data-id="${esc(r.encounter_ref)}" data-suggestion="${esc(s.suggestion_ref)}">采纳并写入</button><button class="btn sm" data-action="decline" data-id="${esc(r.encounter_ref)}" data-suggestion="${esc(s.suggestion_ref)}">不采纳</button></div>` : ''}
    </div>`;
  }).join('') : `<p class="note">本接诊暂无已保存建议。建议不会自动写入病历。</p>`;

  const alertPanel = unhandled.length ? unhandled.slice(0, 3).map((a) => `<div class="alert-item" style="border-bottom:1px dotted var(--line);padding:7px 0">
      <div class="rule"><b>${esc(a.rule_ref)}</b><span>命中 ${esc(FIELD_TEXT[a.hit_field] || a.hit_field)}（${esc(a.hit_field)}）</span></div>
      <p>${a.record_ref ? '关联病历 ' + esc(shortRef(a.record_ref, 8)) : '关联首次写入校验'} · 待处理</p>
      <a class="btn sm danger" href="#/alerts" style="margin-bottom:4px">去告警处理</a>
    </div>`).join('') : `<p class="read-note">当前没有未处理告警。查看告警或读取病历都不会自动把告警标为已处理。</p>`;

  view.innerHTML = `<div class="view">
    <div class="crumbs"><a href="#/clinical">诊疗队列</a><span>/</span>接诊 ${esc(shortRef(r.encounter_ref, 8))}</div>
    <header class="page-head">
      <div>
        <h1>${esc(r.pet_name || '未命名宠物')}</h1>
        <p class="lede">${esc(r.species || '未记录物种')} · ${esc(r.complaint || '未记录主诉')}</p>
      </div>
      <div class="head-side">
        <div class="head-meta">
          <div class="meta-item"><span>接诊编号</span>${refCode(r.encounter_ref, 8)}</div>
          <div class="meta-item"><span>病历编号</span>${c ? refCode(c.record_ref, 8) : '<code>—</code>'}</div>
          <div class="meta-item"><span>建档时间</span><code>${esc(fmtTime(r.created_at))}</code></div>
        </div>
        <div class="head-actions">
          <a class="btn ghost sm" href="#/clinical">返回</a>
          ${locked ? '<span class="chip danger">误建 · 仅可纠错</span>' : c
            ? `<button class="btn sm" data-action="diagnosis-correct" data-id="${esc(r.encounter_ref)}">更正诊断</button>
               <button class="btn primary sm" data-action="med-append" data-id="${esc(r.encounter_ref)}">追加用药</button>`
            : `<button class="btn primary sm" data-action="diagnosis-first" data-id="${esc(r.encounter_ref)}">首次诊断</button>`}
        </div>
      </div>
    </header>

    <div class="detail-grid">
      <section class="sheet">
        <header class="sheet-head">
          <div>
            <h2 class="sheet-title">病历记录 ${c ? `<span class="chip ${locked ? 'danger' : 'ok'}">正式病历 v${esc(versionNo)}</span>` : `<span class="chip warn">待诊断</span>`}${locked ? chip('误建', 'danger') : ''}${recAlerts.some((a) => a.handling_state === 'unhandled') ? chip('本记录有未处理告警', 'danger') : ''}</h2>
            <span class="sheet-sub">CLINICAL RECORD · 旧版本不可覆盖 · 采纳须理由</span>
          </div>
          <span class="stamp ${locked ? 'blocked' : c ? '' : 'muted'}">${locked ? '误建 · 保留' : c ? '正式病历' : '待诊断'}</span>
        </header>

        <div class="block">
          <span class="idx">01</span>
          <div class="block-body">
            <div class="block-head"><h2>接诊资料</h2><em>前台录入 · 临床侧只读</em></div>
            <dl class="fields">
              <div><dt>物种</dt><dd>${esc(r.species || '—')}</dd></div>
              <div><dt>宠物名称</dt><dd>${esc(r.pet_name || '未填写')}</dd></div>
              <div class="wide"><dt>主诉</dt><dd>${esc(r.complaint || '')}</dd></div>
              <div><dt>宠物编号</dt><dd>${refCode(r.pet_ref)}</dd></div>
              <div><dt>接诊归属</dt><dd>${esc(state.session.username)} · 本店负责</dd></div>
            </dl>
            <p class="note">接待资料不在此处修改；需要更正时由建档前台提交，并保留原版本。</p>
          </div>
        </div>

        <div class="block">
          <span class="idx">02</span>
          <div class="block-body">
            ${c ? `<div class="block-head"><h2>诊断与检查</h2><em>v${esc(versionNo)} · 账号 #${esc(current ? current.actor_ref : '—')} · ${esc(fmtTime(current && current.created_at))}</em>
              <div class="block-actions"><button class="btn sm" data-action="diagnosis-correct" data-id="${esc(r.encounter_ref)}">更正诊断</button></div></div>
              <p class="diagnosis">${esc(c.diagnosis || '未填写诊断')}</p>
              <div class="exam"><b>检查记录</b><span>${esc(c.examination || '未填写')}</span></div>`
            : `<div class="block-head"><h2>诊断与检查</h2><em>尚无正式病历</em></div>
              <div class="empty-block"><b>${locked ? '误建记录没有正式病历' : '尚无正式病历'}</b>
                <p>${locked ? '误建记录只允许对已有内容纠错，不能新增独立治疗。' : '首次诊断提交后生成 v1；诊断为空会被护栏阻断，病历不会生成。'}</p>
                ${locked ? '' : `<button class="btn primary" data-action="diagnosis-first" data-id="${esc(r.encounter_ref)}">填写首次诊断</button>`}
              </div>`}
          </div>
        </div>

        <div class="block">
          <span class="idx">03</span>
          <div class="block-body">
            <div class="block-head"><h2>用药链</h2><em>${chain.length} 条 · 原项保留 · 更正关联</em>
              ${locked ? '' : `<div class="block-actions"><button class="btn sm" data-action="med-append" data-id="${esc(r.encounter_ref)}">追加用药</button></div>`}
            </div>
            ${c ? medHtml : '<p class="note">没有正式病历，不能追加独立用药。</p>'}
          </div>
        </div>

        <div class="block">
          <span class="idx">04</span>
          <div class="block-body">
            <div class="block-head"><h2>版本留痕</h2><em>${versions.length} 个正式版本 · 不可覆盖</em></div>
            ${versions.length ? `<ol class="versions">${versions.slice().reverse().map((v) => `<li class="ver ${v.version === versionNo ? 'now' : ''}">
              <span class="vno">v${esc(v.version)}</span>
              <div><p>${esc(v.diagnosis || '（空诊断）')}</p><small>${v.examination ? '检查：' + esc(v.examination) + ' · ' : ''}${v.reason ? '理由：' + esc(v.reason) : '无更正理由'} · 账号 #${esc(v.actor_ref)}</small></div>
              <time>${esc(fmtTime(v.created_at))}</time>
            </li>`).join('')}</ol>` : '<p class="note">首次诊断后生成版本链。更正与采纳都会追加版本，不覆盖旧版本。</p>'}
          </div>
        </div>

        <div class="block">
          <span class="idx">05</span>
          <div class="block-body">
            <div class="block-head"><h2>采纳记录</h2><em>${adoptions.length} 条 · 来源保留</em></div>
            ${adoptions.length ? `<ul class="adopts">${adoptions.map((a) => `<li class="adopt">
              <span class="src">来源 · ${esc(sourceText(a.source))}</span>
              <p>${esc(a.adopted_content && a.adopted_content.diagnosis ? '诊断：' + a.adopted_content.diagnosis : '')}${a.adopted_content && a.adopted_content.examination ? ' · 检查：' + a.adopted_content.examination : ''}${a.medication_ref ? ' · 含用药' : ''}</p>
              <time>${esc(fmtTime(a.created_at))} · 理由：${esc(a.reason || '未填写')} · 版本 ${esc(shortRef(a.version_ref, 8))}</time>
            </li>`).join('')}</ul>` : '<p class="note">尚无建议被采纳。未采纳建议只登记终态，不写病历。</p>'}
          </div>
        </div>
      </section>

      <aside class="side">
        <section class="panel"><div class="panel-head"><span class="panel-label">本接诊状态</span><span class="panel-count">规则 0.2</span></div>
          <div class="panel-body">
            <div class="check"><i class="st ${c ? '' : 'idle'}"></i>正式病历<span>${c ? 'v' + esc(versionNo) : '未生成'}</span></div>
            <div class="check"><i class="st ${chain.length ? '' : 'idle'}"></i>用药条目<span>${chain.length}</span></div>
            <div class="check ${recAlerts.some((a) => a.handling_state === 'unhandled') ? 'bad' : ''}"><i class="st ${recAlerts.some((a) => a.handling_state === 'unhandled') ? 'miss' : 'idle'}"></i>本记录未处理告警<span>${recAlerts.filter((a) => a.handling_state === 'unhandled').length}</span></div>
            <div class="check"><i class="st ${adoptions.length ? '' : 'idle'}"></i>已采纳建议<span>${adoptions.length}</span></div>
            <p class="read-note" style="margin-top:9px">首次诊断须有诊断内容；更正须绑定当前版本并填写理由；护栏命中时病历不改变并生成告警。</p>
          </div>
        </section>

        <section class="panel"><div class="panel-head"><span class="panel-label">我的未处理告警</span><span class="panel-count">${unhandled.length}</span></div>
          <div class="panel-body">${alertPanel}</div>
        </section>

        <section class="panel"><div class="panel-head"><span class="panel-label">已保存建议</span><span class="panel-count">${suggestions.length}</span></div>
          <div class="panel-body">${suggestionsHtml}</div>
        </section>

        <section class="panel"><div class="panel-head"><span class="panel-label">读取留痕</span></div>
          <div class="panel-body"><div class="read-line"><i></i><span>clinical.read · 成功</span></div><p class="read-note">本次读取已记录审计目标；审计不复制诊断正文。</p></div>
        </section>
      </aside>
    </div>
    <footer class="page-foot"><span>临床读取只向负责医生开放 · 采纳与更正同事务记录责任</span><span>${esc(fmtFull(new Date().toISOString()))}</span></footer>
  </div>`;
}

async function viewAlerts(view) {
  const params = state.alertFilter === 'all' ? {} : { handling_state: state.alertFilter };
  const data = await api('/api/clinical/alerts' + (Object.keys(params).length ? '?' + qs(params) : ''));
  const items = data.items || [];
  const cards = items.map((a) => {
    const handled = a.handling_state === 'handled';
    const ruleText = RULE_TEXT[a.rule_ref] || '规则命中，请核对对应字段。';
    const fieldText = FIELD_TEXT[a.hit_field] || a.hit_field;
    return `<article class="alert-card ${handled ? '' : 'unhandled'}">
      <div class="alert-head">
        <span class="rule">${esc(a.rule_ref)}</span>
        <span class="field">命中字段 ${esc(a.hit_field)}</span>
        ${handled ? chip('已处理', 'ok') : chip('待处理', 'danger')}
        <span class="when">${handled ? esc(fmtTime(a.handled_at)) : '尚未处理'}</span>
      </div>
      <div class="alert-body">
        <div>
          <div class="txt"><b>${esc(ruleText)}</b>
            <div class="alert-specific">命中字段 <code>${esc(a.hit_field)}</code>（${esc(fieldText)}）${a.record_ref ? ' · 关联病历 ' + `<code>${esc(shortRef(a.record_ref, 8))}</code>` : ' · 关联首次写入校验'}${handled ? ' · 处理结果：' + esc(a.handling_result || '未填写') : ''}</div>
            <p class="note">${handled ? '告警已登记处理；命中事件与原始阻断保留。' : '告警列表不展开字段值；打开病历核对被拦内容，再登记处理结果。查看告警不会自动标记完成。'}</p>
          </div>
          <div class="alert-actions">
            <button class="btn sm" data-action="locate-alert" data-alert="${esc(a.alert_ref)}">打开病历核对</button>
            ${handled ? '' : `<button class="btn primary sm" data-action="resolve-alert" data-alert="${esc(a.alert_ref)}">登记处理结果</button>`}
          </div>
        </div>
        <div class="alert-meta">
          告警 <code>${esc(shortRef(a.alert_ref, 8))}</code><br>
          事件 <code>${esc(shortRef(a.event_ref, 8))}</code><br>
          病历 ${a.record_ref ? `<code>${esc(shortRef(a.record_ref, 8))}</code>` : '（首次写入，尚未生成）'}<br>
          处理人 ${a.handler_ref ? '账号 #' + esc(a.handler_ref) : '—'}
        </div>
      </div>
    </article>`;
  }).join('');

  view.innerHTML = `<div class="view">
    <header class="page-head">
      <div>
        <h1>告警队列</h1>
        <p class="lede">护栏命中的写入事件。未处理告警持续保留；处理须由负责医生登记结果，记录真实账号与时间。</p>
      </div>
      <div class="head-side"><div class="head-actions"><button class="btn ghost sm" data-action="reload">刷新</button></div></div>
    </header>
    <div class="toolbar">
      <div class="seg" role="group" aria-label="处理状态">
        ${[['unhandled', '待处理'], ['handled', '已处理'], ['all', '全部']].map(([k, l]) => `<button type="button" class="${state.alertFilter === k ? 'on' : ''}" data-action="alert-filter" data-filter="${k}">${l}</button>`).join('')}
      </div>
      <span class="count">共 ${items.length} 条</span>
    </div>
    ${cards || '<div class="list-empty"><b>当前筛选下没有告警</b><p>护栏命中才会生成告警；查看病历不会产生告警。</p></div>'}
    <footer class="page-foot"><span>处理告警不会删除命中事件 · 同一告警只登记一次终态</span><span>${esc(fmtFull(new Date().toISOString()))}</span></footer>
  </div>`;
}

/* ---------------- 管理 ---------------- */

function stateChip(state) {
  return chip(CANDIDATE_STATES[state] || state, state === 'approved' ? 'ok' : state === 'rejected' ? 'danger' : state === 'submitted' ? 'warn' : 'muted');
}

function definitionBody(detail) {
  const d = detail.control_definition || {};
  const redlines = Array.isArray(d.safety_redlines) ? d.safety_redlines : [];
  const coding = Array.isArray(d.coding_standards) ? d.coding_standards : [];
  const templates = d.templates || {};
  const clinical = templates.clinical || {};
  const reception = templates.reception || {};
  const auth = d.authorization || {};
  const quality = d.quality_gates || {};
  const roles = Array.isArray(auth.roles) ? auth.roles : Object.keys(auth.roles || {});
  const qualityText = quality.status === 'targets_not_results' ? '目标，未作为实测结果' : (quality.status || '未标注');
  const load = quality.load || {};
  const projections = auth.projections || {};
  const issues = (detail.validation_results && detail.validation_results.issues) || [];
  const diffs = detail.definition_differences || [];
  const history = detail.governance_history || {};

  const PHASES = { candidate: '候选', clinical_change: '临床写入', decision_change: '决定变更', delivery: '投递', read: '读取', reception_change: '接待变更', activation: '启用', persistence: '持久化', release: '发布', export: '导出' };
  const redlineRows = redlines.map((r) => `<li><span class="rid">${esc(r.id)}</span>${esc(r.checker || '未登记检查器')}<span class="stage">${(r.phases || []).map((p) => esc(PHASES[p] || p)).join(' / ')}</span></li>`).join('');

  return `
    <div class="block">
      <div class="block-body">
        <div class="block-head"><h2>定义摘要</h2><em>内容摘要 ${esc(shortRef(detail.content_version_ref, 8))}</em></div>
        <p class="definition-meta"><span><b>${redlines.length}</b> 条安全红线</span><span><b>${coding.length}</b> 项编码约束</span><span><b>${Object.keys(projections).length}</b> 组字段投影</span><span>定义 <b>${esc(d.version || '—')}</b></span></p>
        ${diffs.length ? `<p class="note">相对当前生效：${diffs.map((k) => chip(DIFF_LABELS[k] || k, 'warn')).join(' ')}</p>` : ''}
        <ul class="rule-list" style="margin-top:8px">${redlineRows || '<li>未登记安全红线</li>'}</ul>
        ${redlines.length && redlines.every((r) => r.may_weaken === false) ? `<p class="note">以上 ${redlines.length} 条均标记为不可削弱；逐条列出便于核对，不再重复挂标签。</p>` : ''}
      </div>
    </div>

    <div class="block">
      <div class="block-body">
        <div class="block-head"><h2>字段与模板</h2><em>定义由受信任发行资产编译</em></div>
        <div class="fields">
          <div><dt>接诊必填</dt><dd>${(reception.required || []).map(esc).join('、') || '—'}</dd></div>
          <div><dt>接诊可选</dt><dd>${(reception.optional || []).map(esc).join('、') || '—'}</dd></div>
          <div><dt>临床必填</dt><dd>${(clinical.required || []).map(esc).join('、') || '—'}</dd></div>
          <div><dt>临床可选</dt><dd>${(clinical.optional || []).map(esc).join('、') || '—'}</dd></div>
          <div><dt>用药存储</dt><dd>${esc((clinical.medication && clinical.medication.storage) || '—')}</dd></div>
          <div><dt>更正要求</dt><dd>${((clinical.medication && clinical.medication.correction_requires) || []).map(esc).join('、') || '—'}</dd></div>
        </div>
        <details class="details" style="margin-top:10px"><summary>展开完整模板定义<em>JSON</em></summary><div class="inner"><pre class="json">${esc(JSON.stringify(templates, null, 2))}</pre></div></details>
      </div>
    </div>

    <div class="block">
      <div class="block-body">
        <div class="block-head"><h2>角色与数据范围</h2><em>${Object.keys(projections).length} 组字段投影</em></div>
        <p class="note">角色：${roles.map(esc).join('、') || '—'}。投影按角色与查询/写入方向分配字段，未登记字段默认拒绝。</p>
        <details class="details" style="margin-top:8px"><summary>展开字段投影与身份约束<em>JSON</em></summary><div class="inner"><pre class="json">${esc(JSON.stringify(auth, null, 2))}</pre></div></details>
      </div>
    </div>

    <div class="block">
      <div class="block-body">
        <div class="block-head"><h2>编码约束与质量目标</h2><em>目标不是实测结果</em></div>
        <ul class="rule-list">${coding.map((c) => `<li>${esc(typeof c === 'string' ? c : (c.id || c.rule || JSON.stringify(c)))}</li>`).join('') || '<li>未登记编码约束</li>'}</ul>
        <div class="quality-row">
          <div><b>≤ ${esc(load.p95_ms_max || '—')} ms</b><span>P95</span></div>
          <div><b>${esc(load.concurrency || '—')}</b><span>并发</span></div>
          <div><b>${esc(load.duration_seconds ? Math.round(load.duration_seconds / 60) : '—')} 分钟</b><span>持续</span></div>
          <div><b>≥ ${esc(load.throughput_per_minute_min || '—')}</b><span>次 / 分钟</span></div>
        </div>
        <p class="note">当前状态：${esc(qualityText)}。以上是题定目标，不是实测结果；本页不以目标冒充实测。</p>
        <details class="details" style="margin-top:8px"><summary>展开质量与浏览器约束<em>JSON</em></summary><div class="inner"><pre class="json">${esc(JSON.stringify(quality, null, 2))}</pre></div></details>
      </div>
    </div>

    <div class="block">
      <div class="block-body">
        <div class="block-head"><h2>校验结果</h2><em>${issues.length ? issues.length + ' 项问题' : '通过'}</em></div>
        ${issues.length ? `<ul class="rule-list">${issues.map((i) => `<li><span class="rid">${esc(i.path || '—')}</span>${esc(i.message || '')}</li>`).join('')}</ul>` : '<p class="note">编译与底线校验通过。校验通过不等于已批准或已启用，三个步骤分别记录。</p>'}
      </div>
    </div>

    <div class="block">
      <div class="block-body">
        <div class="block-head"><h2>操作历史</h2><em>生成、提交、审批、启用分离</em></div>
        <div class="history">
          <div><b>生成</b> · 账号 #${esc(history.generated_by ?? '—')} · ${esc(fmtTime(history.generated_at))} · ${esc(history.generation_reason || '未填写理由')}</div>
          <div><b>提交</b> · ${history.submitted_by ? '账号 #' + esc(history.submitted_by) : '尚未提交'} · ${esc(fmtTime(history.submitted_at))} · ${esc(history.submit_reason || '—')}</div>
          <div><b>审批</b> · ${history.review ? (history.review.decision === 'approve' ? '批准' : '驳回') + ' · 账号 #' + esc(history.review.actor) + ' · ' + esc(fmtFull(history.review.at)) + ' · ' + esc(history.review.reason) : '尚无审批记录'}</div>
          ${(history.activations || []).map((a) => `<div><b>启用</b> · 第 ${esc(a.generation)} 代 · 账号 #${esc(a.actor_id)} · ${esc(fmtFull(a.created_at))} · ${esc(a.reason)}</div>`).join('')}
        </div>
      </div>
    </div>`;
}

function candidateActions(detail) {
  if (detail.is_active) return '<span class="read-note">当前生效，无待办操作。</span>';
  if (detail.state === 'draft') return `<button class="btn primary sm" data-action="candidate-action" data-cmd="submit" data-id="${esc(detail.candidate_ref)}">提交审批</button>`;
  if (detail.state === 'submitted') return `<button class="btn primary sm" data-action="candidate-action" data-cmd="approve" data-id="${esc(detail.candidate_ref)}">批准</button><button class="btn danger sm" data-action="candidate-action" data-cmd="reject" data-id="${esc(detail.candidate_ref)}">驳回</button>`;
  if (detail.state === 'approved') return `<button class="btn primary sm" data-action="candidate-action" data-cmd="activate" data-id="${esc(detail.candidate_ref)}">手动启用</button>`;
  return '';
}

async function viewGovernance(view, id) {
  const list = await api('/api/candidates');
  const items = list.items || [];
  state.sidebar = { ...list, items };
  let selected = id || state.selectedCandidate || (items[0] && items[0].candidate_ref) || '';
  if (!selected) {
    view.innerHTML = `<div class="view">
      <header class="page-head"><div><h1>治理台</h1><p class="lede">本店规则与模板的生成、提交、审批和启用是四个独立步骤；批准不会自动启用。</p></div>
      <div class="head-side"><div class="head-actions"><button class="btn primary" data-action="generate-candidate">生成候选</button></div></div></header>
      <div class="list-empty"><b>本店暂无候选</b><p>导入控制定义并生成候选，随后仍需提交、人工审批和手动启用。</p><div style="margin-top:14px"><button class="btn" data-action="generate-candidate">生成候选</button></div></div>
    </div>`;
    return;
  }
  const detail = await api('/api/candidates/' + encodeURIComponent(selected));
  state.selectedCandidate = selected;
  const displayState = detail.is_active ? '当前生效' : (CANDIDATE_STATES[detail.state] || detail.state);

  const listHtml = items.map((c) => `<a class="cand ${c.candidate_ref === selected ? 'on' : ''}" href="#/governance/${esc(c.candidate_ref)}">
    <div class="top"><span class="ref">${esc(shortRef(c.candidate_ref, 8))}</span>${c.candidate_ref === list.active_candidate_ref ? chip('当前生效', 'ok') : stateChip(c.state)}</div>
    <div class="sub">${esc(fmtTime(c.created_at))} · v${esc(c.version || '—')}${c.candidate_ref === list.active_candidate_ref ? ' · 生效' : ''}</div>
  </a>`).join('');

  view.innerHTML = `<div class="view">
    <header class="page-head">
      <div><h1>治理台</h1><p class="lede">本店候选的生成、提交、审批和启用分别记录。批准绑定具体内容摘要；启用前再次核对生效序号。</p></div>
      <div class="head-side"><div class="head-actions"><button class="btn primary" data-action="generate-candidate">生成候选</button></div></div>
    </header>
    <div class="stat-strip">
      <div class="stat"><b>${esc(list.generation ?? '—')}</b><span>生效序号</span></div>
      <div class="stat"><b>${list.active_candidate_ref ? esc(shortRef(list.active_candidate_ref, 8)) : '—'}</b><span>当前生效</span></div>
      <div class="stat"><b>${esc(displayState)}</b><span>候选状态</span></div>
      <div class="stat"><b>${(detail.definition_differences || []).length ? detail.definition_differences.length + ' 项' : '无变化'}</b><span>相对生效</span></div>
    </div>
    <div class="gov-grid">
      <aside class="sheet">
        <div class="panel-head"><span class="panel-label">本店候选</span><span class="panel-count">${items.length} 个</span></div>
        <div class="cand-list">${listHtml}</div>
      </aside>
      <section class="sheet">
        <header class="sheet-head">
          <div><h2 class="sheet-title">候选 ${esc(shortRef(detail.candidate_ref, 8))}</h2>
          <span class="sheet-sub">控制包 · 生成于 ${esc(fmtTime(detail.governance_history && detail.governance_history.generated_at))}</span></div>
          <span class="stamp ${detail.state === 'rejected' ? 'blocked' : detail.is_active ? '' : 'muted'}">${esc(displayState)}</span>
        </header>
        <div class="definition">
          ${definitionBody(detail)}
          <div class="block"><div class="block-body">
            <div class="block-head"><h2>本次操作</h2><em>每次操作单独填写理由</em></div>
            <div class="head-actions">${candidateActions(detail)}<button class="btn sm ghost" data-action="copy-ref" data-ref="${esc(detail.candidate_ref)}">复制候选编号</button></div>
            <p class="note">批准绑定当前内容摘要 ${esc(shortRef(detail.content_version_ref, 8))}；内容变化后原批准不可继续使用。启用需当前生效序号 ${esc(detail.generation)}。</p>
          </div></div>
        </div>
      </section>
    </div>
    <footer class="page-foot"><span>批准不等于启用 · 不伪造他人审批 · 定义与批准内容绑定</span><span>GEN ${esc(list.generation)}</span></footer>
  </div>`;
}

async function viewAudit(view) {
  const f = state.auditFilters;
  const params = {};
  if (f.record_ref) params.record_ref = f.record_ref;
  if (f.operator_ref) params.operator_ref = f.operator_ref;
  const data = await api('/api/audit' + (Object.keys(params).length ? '?' + qs(params) : ''));
  const items = data.items || [];
  const rows = items.map((e) => `<tr>
    <td class="nowrap"><code>${esc(fmtFull(e.event_at))}</code></td>
    <td class="nowrap">${e.operator_ref ? '#' + esc(e.operator_ref) : '—'} <span class="muted">/ ${esc(ROLES[e.operator_role] || e.operator_role || '—')}</span></td>
    <td class="nowrap">${esc(ACTIONS[e.action] || e.action)}<br><code>${esc(e.action)}</code></td>
    <td class="nowrap"><span class="out-${esc(e.outcome)}">${esc(OUTCOMES[e.outcome] || e.outcome)}</span>${e.normalized_reason && e.normalized_reason !== 'COMPLETED' ? `<br><code>${esc(e.normalized_reason)}</code>` : ''}</td>
    <td>${e.target_ref ? refCode(e.target_ref, 8) : '—'}</td>
  </tr>`).join('');

  view.innerHTML = `<div class="view">
    <header class="page-head">
      <div><h1>脱敏审计</h1><p class="lede" style="max-width:64ch">最近 100 条获准事件元数据。只含账号、动作、结果、原因与目标编号，不含临床正文或自由理由。</p></div>
      <div class="head-side"><span class="chip muted">只读</span></div>
    </header>
    <form class="toolbar" data-form="audit" novalidate>
      <div class="field" style="min-width:260px"><label for="audit-record">按病历编号</label><input id="audit-record" name="record_ref" class="mono" type="text" value="${esc(f.record_ref)}" placeholder="UUID（可选）" autocomplete="off"></div>
      <div class="field" style="min-width:200px"><label for="audit-operator">按操作者编号</label><input id="audit-operator" name="operator_ref" class="mono" type="text" value="${esc(f.operator_ref)}" placeholder="数字（可选）" autocomplete="off"></div>
      <button class="btn primary sm" type="submit" style="align-self:flex-end;margin-bottom:2px">查询</button>
      <button class="btn ghost sm" type="button" data-action="audit-reset" style="align-self:flex-end;margin-bottom:2px">重置</button>
      <span class="count">共 ${items.length} 条</span>
    </form>
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>结果 / 标准原因</th><th>目标</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5"><div class="list-empty"><b>没有符合条件的事件</b><p>审计只返回本店范围，最多 100 条。</p></div></td></tr>'}</tbody>
      </table>
    </div>
    <footer class="page-foot"><span>元数据只读 · 不含临床正文 · 事件不可在此修改</span><span>${esc(fmtFull(new Date().toISOString()))}</span></footer>
  </div>`;
}

/* ---------------- 弹窗构造 ---------------- */

async function openReceptionCorrect(id, prefill = {}) {
  const r = await api('/api/receptions/' + encodeURIComponent(id));
  const vs = (r.reception_versions || []).slice().sort((a, b) => a.version - b.version);
  const latest = vs[vs.length - 1];
  if (!latest) { toast('该接待没有可更正的版本。', 'err'); return; }
  const species = prefill.species !== undefined ? prefill.species : r.species;
  const petName = prefill.pet_name !== undefined ? prefill.pet_name : r.pet_name;
  const complaint = prefill.complaint !== undefined ? prefill.complaint : r.complaint;
  const reason = prefill.reason || '';
  openModal({
    kind: 'reception-correct',
    title: '更正接待资料',
    subtitle: `接诊 ${shortRef(id, 8)} · 提交后生成 v${latest.version + 1}，原版本保留`,
    draftKey: 'reception-correct:' + id,
    body: `<form id="modal-form" data-form="reception-correct" data-draft="reception-correct:${esc(id)}" novalidate>
      <input type="hidden" name="encounter_ref" value="${esc(id)}">
      <input type="hidden" name="original_version_ref" value="${esc(latest.version_ref)}">
      <div class="form-grid">
        ${fieldWrap('物种', 'species', inputHtml('species', species, { maxlength: 100 }), '必填，最多 100 字。', true)}
        ${fieldWrap('宠物名称', 'pet_name', inputHtml('pet_name', petName, { maxlength: 100 }), '可选，最多 100 字。')}
        <div class="field wide"><label for="f-complaint">主诉 <b style="color:var(--rust)">*</b></label>${textareaHtml('complaint', complaint, { maxlength: 10000 })}</div>
        <div class="field wide"><label for="f-reason">更正理由 <b style="color:var(--rust)">*</b></label>${textareaHtml('reason', reason, { maxlength: 2000, placeholder: '说明需要更正的内容与依据' })}<p class="hint">理由与操作账号一并保留，可在版本链核对。</p></div>
      </div>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="modal-form">保存更正</button>`
  });
  state.reloadLatest = () => openReceptionCorrect(id, {
    species: ($('#f-species') || {}).value,
    pet_name: ($('#f-pet_name') || {}).value,
    complaint: ($('#f-complaint') || {}).value,
    reason: ($('#f-reason') || {}).value
  });
}

async function openReceptionMisreg(id) {
  openModal({
    title: '标记为误建',
    subtitle: '记录不会删除；原版本与原告警保留',
    draftKey: 'reception-misreg:' + id,
    body: `<form id="modal-form" data-form="reception-misreg" data-draft="reception-misreg:${esc(id)}" novalidate>
      <input type="hidden" name="encounter_ref" value="${esc(id)}">
      <div class="blocked-banner" style="border-color:var(--amber-line);background:var(--amber-wash)">
        <div><b style="color:var(--amber)">误建标记会改变该接诊的后续写入能力</b>
        <p style="color:var(--amber)">标记后只能对已有内容纠错，不能新增独立治疗；如需重建，可填写关联的新接诊编号。原记录、原版本和原告警不会被删除。</p></div>
      </div>
      <div class="form-grid">
        <div class="field wide"><label for="f-reason">误建原因 <b style="color:var(--rust)">*</b></label>${textareaHtml('reason', '', { maxlength: 2000, placeholder: '说明误建的依据' })}</div>
        ${fieldWrap('关联重建接诊（可选）', 'rebuilt_encounter_ref', inputHtml('rebuilt_encounter_ref', '', { placeholder: '完整 UUID', autocomplete: 'off' }), '必须是当前账号获准范围内的接诊。')}
      </div>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn danger" type="submit" form="modal-form">确认标记</button>`
  });
}

function openDiagnosisFirst(id) {
  openModal({
    title: '填写首次诊断',
    subtitle: '提交成功生成正式病历 v1；诊断为空会被护栏阻断',
    draftKey: 'diagnosis-first:' + id,
    body: `<form id="modal-form" data-form="diagnosis-first" data-draft="diagnosis-first:${esc(id)}" novalidate>
      <input type="hidden" name="encounter_ref" value="${esc(id)}">
      <div class="form-grid">
        <div class="field wide"><label for="f-diagnosis">诊断 <b style="color:var(--rust)">*</b></label>${textareaHtml('diagnosis', '', { maxlength: 10000, placeholder: '记录本次诊断结论' })}<p class="hint">诊断是首次病历的必填内容；护栏阻断时不会生成病历，也不会覆盖任何旧内容。</p></div>
        <div class="field wide"><label for="f-examination">检查记录（可选）</label>${textareaHtml('examination', '', { maxlength: 10000, placeholder: '可留空' })}</div>
      </div>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="modal-form">生成病历 v1</button>`
  });
}

async function openDiagnosisCorrect(id, prefill = {}) {
  const item = await api('/api/clinical/encounters/' + encodeURIComponent(id));
  const c = item.clinical;
  if (!c) { toast('该接诊尚无正式病历，不能更正。', 'err'); return; }
  const vs = (c.clinical_versions || []).slice().sort((a, b) => a.version - b.version);
  const current = vs[vs.length - 1];
  const diagnosis = prefill.diagnosis !== undefined ? prefill.diagnosis : c.diagnosis;
  const examination = prefill.examination !== undefined ? prefill.examination : c.examination;
  openModal({
    kind: 'diagnosis-correct',
    title: '更正诊断与检查',
    subtitle: `基于 v${current.version} 追加新版本，旧版本保留`,
    draftKey: 'diagnosis-correct:' + id,
    body: `<form id="modal-form" data-form="diagnosis-correct" data-draft="diagnosis-correct:${esc(id)}" novalidate>
      <input type="hidden" name="encounter_ref" value="${esc(id)}">
      <input type="hidden" name="original_content_ref" value="${esc(c.record_ref)}">
      <input type="hidden" name="original_version_ref" value="${esc(current.version_ref)}">
      <div class="form-grid">
        <div class="field wide"><label for="f-diagnosis">诊断 <b style="color:var(--rust)">*</b></label>${textareaHtml('diagnosis', diagnosis, { maxlength: 10000 })}</div>
        <div class="field wide"><label for="f-examination">检查记录</label>${textareaHtml('examination', examination, { maxlength: 10000 })}</div>
        <div class="field wide"><label for="f-reason">更正理由 <b style="color:var(--rust)">*</b></label>${textareaHtml('reason', prefill.reason || '', { maxlength: 2000, placeholder: '说明更正依据；理由会随新版本保留' })}</div>
      </div>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="modal-form">保存新版本 v${current.version + 1}</button>`
  });
  state.reloadLatest = () => openDiagnosisCorrect(id, {
    diagnosis: ($('#f-diagnosis') || {}).value,
    examination: ($('#f-examination') || {}).value,
    reason: ($('#f-reason') || {}).value
  });
}

function openMedicationAppend(id) {
  openModal({
    title: '追加用药',
    subtitle: '追加为独立条目；不覆盖既有用药',
    draftKey: 'med-append:' + id,
    body: `<form id="modal-form" data-form="med-append" data-draft="med-append:${esc(id)}" novalidate>
      <input type="hidden" name="encounter_ref" value="${esc(id)}">
      <div class="form-grid">
        ${fieldWrap('药品', 'drug', inputHtml('drug', '', { maxlength: 2000, placeholder: '药品名称' }), '', true)}
        ${fieldWrap('剂量', 'amount', inputHtml('amount', '', { maxlength: 256, placeholder: '如 1' }), '', true)}
        ${fieldWrap('单位', 'unit', inputHtml('unit', '', { maxlength: 100, placeholder: '如 片、ml' }), '', true)}
      </div>
      <p class="note">须已有正式病历。追加后通过受控读取刷新用药链，响应本身不内嵌正文。</p>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="modal-form">追加</button>`
  });
}

async function openMedicationCorrect(id, entryRef, prefill = {}) {
  const item = await api('/api/clinical/encounters/' + encodeURIComponent(id));
  const c = item.clinical;
  const entry = c && (c.medication_chain || []).find((m) => m.entry_ref === entryRef);
  if (!entry) { toast('未找到要更正的用药条目。', 'err'); return; }
  openModal({
    kind: 'med-correct',
    title: '更正用药',
    subtitle: '关联原条目追加，原项保留',
    draftKey: 'med-correct:' + entryRef,
    body: `<form id="modal-form" data-form="med-correct" data-draft="med-correct:${esc(entryRef)}" novalidate>
      <input type="hidden" name="encounter_ref" value="${esc(id)}">
      <input type="hidden" name="original_entry_ref" value="${esc(entryRef)}">
      <div class="form-grid">
        ${fieldWrap('药品', 'drug', inputHtml('drug', prefill.drug !== undefined ? prefill.drug : entry.drug, { maxlength: 2000 }), '', true)}
        ${fieldWrap('剂量', 'amount', inputHtml('amount', prefill.amount !== undefined ? prefill.amount : entry.amount, { maxlength: 256 }), '', true)}
        ${fieldWrap('单位', 'unit', inputHtml('unit', prefill.unit !== undefined ? prefill.unit : entry.unit, { maxlength: 100 }), '', true)}
        <div class="field wide"><label for="f-reason">更正理由 <b style="color:var(--rust)">*</b></label>${textareaHtml('reason', prefill.reason || '', { maxlength: 2000, placeholder: '说明更正依据' })}</div>
      </div>
      <p class="note">原条目 ${esc(shortRef(entryRef, 8))} 保留，新条目以“关联更正”出现在用药链中。</p>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="modal-form">保存更正</button>`
  });
  state.reloadLatest = () => openMedicationCorrect(id, entryRef, {
    drug: ($('#f-drug') || {}).value,
    amount: ($('#f-amount') || {}).value,
    unit: ($('#f-unit') || {}).value,
    reason: ($('#f-reason') || {}).value
  });
}

async function openAdopt(id, suggestionRef) {
  const [item, sugRes] = await Promise.all([
    api('/api/clinical/encounters/' + encodeURIComponent(id)),
    api('/api/clinical/encounters/' + encodeURIComponent(id) + '/suggestions')
  ]);
  const suggestion = (sugRes.items || []).find((s) => s.suggestion_ref === suggestionRef);
  if (!suggestion) { toast('建议不存在或已被移除。', 'err'); return; }
  const c = item.clinical;
  const versions = c ? (c.clinical_versions || []).slice().sort((a, b) => a.version - b.version) : [];
  const current = versions[versions.length - 1];
  const content = suggestion.content || {};
  const med = content.medication || {};
  openModal({
    wide: true,
    title: '采纳建议并写入',
    subtitle: '采纳会生成新版本或首次病历，并经过当前护栏；建议原文保留',
    draftKey: 'adopt:' + suggestionRef,
    body: `<form id="modal-form" data-form="adopt" data-draft="adopt:${esc(suggestionRef)}" novalidate>
      <input type="hidden" name="encounter_ref" value="${esc(id)}">
      <input type="hidden" name="suggestion_ref" value="${esc(suggestionRef)}">
      ${c ? `<input type="hidden" name="record_ref" value="${esc(c.record_ref)}">` : ''}
      ${current ? `<input type="hidden" name="original_version_ref" value="${esc(current.version_ref)}">` : ''}
      <div class="sugg" style="margin-bottom:14px">
        <div class="src">来源 · ${esc(sourceText(suggestion.source))}</div>
        ${suggestionText(suggestion.content)}
      </div>
      <div class="form-grid">
        <div class="field wide"><label for="f-diagnosis">写入的诊断（可修改）</label>${textareaHtml('diagnosis', content.diagnosis || '', { maxlength: 10000 })}</div>
        <div class="field wide"><label for="f-examination">写入的检查（可修改）</label>${textareaHtml('examination', content.examination || '', { maxlength: 10000 })}</div>
        <div class="wide med-fields">
          ${fieldWrap('用药 · 药品（可选）', 'drug', inputHtml('drug', med.drug || '', { maxlength: 2000 }), '三项要么全填，要么全部留空。')}
          ${fieldWrap('用药 · 剂量', 'amount', inputHtml('amount', med.amount || '', { maxlength: 256 }))}
          ${fieldWrap('用药 · 单位', 'unit', inputHtml('unit', med.unit || '', { maxlength: 100 }))}
        </div>
        <div class="field wide"><label for="f-reason">采纳理由 <b style="color:var(--rust)">*</b></label>${textareaHtml('reason', '', { maxlength: 2000, placeholder: '说明为何采纳；未填写会被护栏阻断' })}</div>
      </div>
      <p class="note">${c ? '将基于当前版本 v' + current.version + ' 追加新版本。' : '本接诊尚无正式病历，采纳将生成首次病历。'}推送不修改建议原文。</p>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="modal-form">采纳并写入</button>`
  });
}

async function openDecline(id, suggestionRef) {
  const sugRes = await api('/api/clinical/encounters/' + encodeURIComponent(id) + '/suggestions');
  const suggestion = (sugRes.items || []).find((s) => s.suggestion_ref === suggestionRef);
  if (!suggestion) { toast('建议不存在或已被移除。', 'err'); return; }
  openModal({
    title: '不采纳建议',
    subtitle: '建议与原文保留，不写病历',
    draftKey: 'decline:' + suggestionRef,
    body: `<form id="modal-form" data-form="decline" data-draft="decline:${esc(suggestionRef)}" novalidate>
      <input type="hidden" name="encounter_ref" value="${esc(id)}">
      <input type="hidden" name="suggestion_ref" value="${esc(suggestionRef)}">
      <div class="sugg" style="margin-bottom:14px">
        <div class="src">来源 · ${esc(sourceText(suggestion.source))}</div>
        ${suggestionText(suggestion.content)}
      </div>
      <div class="form-grid">
        <div class="field wide"><label for="f-reason">不采纳理由（可选）</label>${textareaHtml('reason', '', { maxlength: 2000, placeholder: '可留空' })}</div>
      </div>
      <p class="note">决定为终态，提交后不能再次决定；未采纳内容不会写入正式病历。</p>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn" type="submit" form="modal-form">确认不采纳</button>`
  });
}

function openResolveAlert(alertRef) {
  openModal({
    title: '登记告警处理结果',
    subtitle: '处理人、时间与结果由服务端记录；同一告警只能登记一次',
    draftKey: 'resolve:' + alertRef,
    body: `<form id="modal-form" data-form="resolve-alert" data-draft="resolve:${esc(alertRef)}" novalidate>
      <input type="hidden" name="alert_ref" value="${esc(alertRef)}">
      <div class="form-grid">
        <div class="field wide"><label for="f-handling_result">处理结果 <b style="color:var(--rust)">*</b></label>${textareaHtml('handling_result', '', { maxlength: 2000, placeholder: '说明核对到的真实情况与处理方式' })}</div>
      </div>
      <p class="note">查看告警不会自动标记完成；登记后告警仍保留在审计中。</p>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="modal-form">登记处理</button>`
  });
}

function openGenerateCandidate() {
  openModal({
    wide: true,
    title: '生成候选',
    subtitle: '只导入规则、字段和模板定义，不导入真实病历或诊断正文；生成后仍需提交、审批和手动启用',
    body: `<form id="modal-form" data-form="generate-candidate" novalidate>
      <div class="field">
        <span class="field-label">控制定义 <b style="color:var(--rust)">*</b></span>
        <div class="file-row">
          <input id="definition-file" class="file-hidden" type="file" accept=".json,application/json" data-file="definition">
          <label class="btn sm ghost" for="definition-file">选择 JSON 文件</label>
          <span class="file-name" id="definition-file-name">未选择文件</span>
        </div>
        <div class="file-alt">没有文件？<button class="btn-link" type="button" data-action="example-definition">载入本店演示定义</button></div>
        <div class="definition-summary" id="definition-summary">尚未载入定义。选择 JSON 文件，或载入本店演示定义；这里会先显示这次要生成什么。</div>
        <p class="hint">定义中的 scope.store_id 必须等于当前门店；客户端不做信任判断，由服务端编译和校验。</p>
      </div>
      <details class="details" style="margin:10px 0 12px"><summary>查看或编辑定义 JSON<em id="definition-size"></em></summary><div class="inner"><textarea id="definition-json" name="definition" style="min-height:180px" placeholder="粘贴定义 JSON，或选择文件"></textarea></div></details>
      <div class="field"><label for="f-reason">生成理由 <b style="color:var(--rust)">*</b></label>${textareaHtml('reason', '', { maxlength: 2000, placeholder: '说明规则或模板调整的来源与目的' })}</div>
      <p class="note">生成只产生草案；不会自动提交、批准或启用。</p>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="modal-form">校验并生成</button>`
  });
  setTimeout(updateDefinitionSummary, 40);
}

function updateDefinitionSummary() {
  const ta = $('#definition-json');
  const box = $('#definition-summary');
  const size = $('#definition-size');
  if (!ta || !box) return;
  const raw = ta.value.trim();
  if (size) size.textContent = raw ? raw.length + ' 字符' : '';
  box.classList.remove('bad');
  if (!raw) {
    box.textContent = '尚未载入定义。选择 JSON 文件，或载入本店演示定义；这里会先显示这次要生成什么。';
    return;
  }
  let d;
  try { d = JSON.parse(raw); } catch (e) {
    box.classList.add('bad');
    box.textContent = '定义 JSON 无法解析，请检查括号与引号。';
    return;
  }
  const scope = d.scope || {};
  const redlines = Array.isArray(d.safety_redlines) ? d.safety_redlines.length : '—';
  const coding = Array.isArray(d.coding_standards) ? d.coding_standards.length : '—';
  const templates = d.templates && typeof d.templates === 'object' ? Object.keys(d.templates).length : '—';
  const storeLabel = scope.store_id && state.session && scope.store_id === state.session.store_ref
    ? state.session.store_name
    : shortRef(scope.store_id, 8);
  box.innerHTML = `<div><b>定义 ${esc(d.version || '—')}</b> · 门店 <b>${esc(storeLabel)}</b></div>
    <div style="margin-top:4px">${esc(redlines)} 条安全红线 · ${esc(coding)} 项编码约束 · ${esc(templates)} 组模板</div>
    <div class="muted" style="margin-top:4px">这次只生成草案；不会提交、批准或启用。</div>`;
}

async function openCandidateAction(cmd, id) {
  const d = await api('/api/candidates/' + encodeURIComponent(id));
  const cfg = {
    submit: { title: '提交候选审批', verb: '确认提交', note: '提交后状态变为待审批，审批人须核对内容摘要。' },
    approve: { title: '批准候选', verb: '确认批准', note: '批准绑定当前内容摘要；批准不等于启用。' },
    reject: { title: '驳回候选', verb: '确认驳回', note: '驳回后该候选不能直接启用；保留定义与审批记录。' },
    activate: { title: '手动启用候选', verb: '确认启用', note: '启用会切换本店生效版本并递增生效序号；旧定义保留供历史解释。' }
  }[cmd];
  if (!cfg) return;
  openModal({
    title: cfg.title,
    subtitle: `候选 ${shortRef(id, 8)} · 内容摘要 ${shortRef(d.content_version_ref, 8)}`,
    draftKey: 'candidate:' + cmd + ':' + id,
    body: `<form id="modal-form" data-form="candidate-action" data-draft="candidate:${esc(cmd)}:${esc(id)}" novalidate>
      <input type="hidden" name="command" value="${esc(cmd)}">
      <input type="hidden" name="candidate_ref" value="${esc(id)}">
      <input type="hidden" name="content_digest" value="${esc(d.content_version_ref)}">
      <input type="hidden" name="expected_generation" value="${esc(d.generation)}">
      <div class="blocked-banner" style="border-color:var(--line);background:var(--paper)">
        <div><b style="color:var(--ink)">${esc(cfg.note)}</b>
        <p style="color:var(--muted)">当前状态：${esc(CANDIDATE_STATES[d.state] || d.state)}${d.is_active ? ' · 当前生效' : ''} · 生效序号 ${esc(d.generation)}</p></div>
      </div>
      <div class="field"><label for="f-reason">操作理由 <b style="color:var(--rust)">*</b></label>${textareaHtml('reason', '', { maxlength: 2000, placeholder: '说明本次判断的依据' })}</div>
    </form>`,
    foot: `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn ${cmd === 'reject' ? 'danger' : 'primary'}" type="submit" form="modal-form">${esc(cfg.verb)}</button>`
  });
}

/* ---------------- 表单处理 ---------------- */

function submitButton(form) {
  return document.querySelector(`button[form="${form.id}"][type="submit"]`) || form.querySelector('button[type="submit"]');
}

async function runForm(form, fn) {
  clearFormErrors(form);
  const btn = submitButton(form);
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '处理中…'; }
  try {
    await fn();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

function writeError(e, form) {
  if (e.code === 'GUARD_BLOCKED') {
    if ($('#modal-error')) blockedBanner(e);
    else formError(form, '本次写入被护栏阻断，病历未改变。校验事件 ' + (e.eventRef || '—') + '。请查看告警并核对输入。');
    return;
  }
  if (e.code === 'STATE_OR_VERSION_CONFLICT' && state.reloadLatest && $('#modal-error')) {
    conflictBanner(e.message);
    return;
  }
  if (e.code === 'AUTH_REQUIRED') {
    formError(form, '登录已失效，请先刷新页面重新登录；当前输入已保留。');
    return;
  }
  if (e.uncertain) {
    formError(form, e.message + ' 本次输入和操作标识已保留，可直接重试核对。');
    return;
  }
  formError(form, e.message);
}

function afterWrite(message, detail, redirect) {
  closeModal();
  toast(message, 'ok', detail);
  loadSidebar()
    .then(() => (redirect ? navigate(redirect) : renderRoute()))
    .catch(() => {});
}

async function submitNewReception(form) {
  const f = form.elements;
  const identity = f.identity.value;
  const species = f.species.value.trim();
  const complaint = f.complaint.value.trim();
  const petName = f.pet_name.value.trim();
  const doctorRef = f.doctor_ref.value;
  let petRef = identity === 'new' ? f.pet_ref.value.trim() : f.known_ref.value.trim();
  let valid = true;
  if (!isUuid(petRef)) { markField(form, identity === 'new' ? 'pet_ref' : 'known_ref', '宠物编号必须是完整 UUID。'); valid = false; }
  if (!species) { markField(form, 'species', '请填写物种。'); valid = false; }
  if (!complaint) { markField(form, 'complaint', '请填写主诉。'); valid = false; }
  if (!doctorRef) { markField(form, 'doctor_ref', '请选择接诊医生。'); valid = false; }
  if (!valid) return;
  if (identity === 'known') {
    const check = await api('/api/encounters/create-options/pet-reference-check', { method: 'POST', body: { pet_ref: petRef } });
    if (!check.associable) { markField(form, 'known_ref', '该编号无法在本店关联，请核对或改用新宠物。'); return; }
  }
  const body = { pet_identity_choice: identity, pet_ref: petRef, species, complaint, doctor_ref: String(doctorRef) };
  if (petName) body.pet_name = petName;
  const result = await write('/api/encounters', body);
  clearDraft('new-reception');
  state.receptionPage = 1;
  afterWrite('接诊已建立', '编号 ' + shortRef(result.encounter_ref, 8) + '，可继续核对接待资料。', '#/receptions/' + result.encounter_ref);
}

async function submitReceptionCorrect(form) {
  const f = form.elements;
  const id = f.encounter_ref.value;
  const reason = f.reason.value.trim();
  const species = f.species.value.trim();
  const complaint = f.complaint.value.trim();
  let valid = true;
  if (!species) { markField(form, 'species', '请填写物种。'); valid = false; }
  if (!complaint) { markField(form, 'complaint', '请填写主诉。'); valid = false; }
  if (!reason) { markField(form, 'reason', '请填写更正理由。'); valid = false; }
  if (!valid) return;
  const body = { species, complaint, pet_name: f.pet_name.value.trim(), original_version_ref: f.original_version_ref.value, reason };
  await write('/api/encounters/' + encodeURIComponent(id) + '/corrections', body);
  clearDraft('reception-correct:' + id);
  afterWrite('接待资料已更正', '已生成新版本，原版本保留。');
}

async function submitReceptionMisreg(form) {
  const f = form.elements;
  const id = f.encounter_ref.value;
  const reason = f.reason.value.trim();
  const rebuilt = f.rebuilt_encounter_ref.value.trim();
  if (!reason) { markField(form, 'reason', '请填写误建原因。'); return; }
  if (rebuilt && !isUuid(rebuilt)) { markField(form, 'rebuilt_encounter_ref', '关联接诊编号必须是完整 UUID。'); return; }
  const body = { reason };
  if (rebuilt) body.rebuilt_encounter_ref = rebuilt;
  await write('/api/encounters/' + encodeURIComponent(id) + '/misregistration', body);
  clearDraft('reception-misreg:' + id);
  afterWrite('已标记为误建', '原记录、版本与告警保留。');
}

async function submitDiagnosis(form, mode) {
  const f = form.elements;
  const id = f.encounter_ref.value;
  const diagnosis = f.diagnosis.value.trim();
  const examination = f.examination.value.trim();
  let valid = true;
  if (!diagnosis && mode === 'first') { markField(form, 'diagnosis', '首次诊断必须有诊断内容。'); valid = false; }
  if (!valid) return;
  if (mode === 'first') {
    await write('/api/clinical/encounters/' + encodeURIComponent(id) + '/diagnosis', { diagnosis, examination });
    clearDraft('diagnosis-first:' + id);
    afterWrite('正式病历 v1 已生成', '诊断与检查已写入，版本链从 v1 开始。');
  } else {
    const reason = f.reason.value.trim();
    if (!reason) { markField(form, 'reason', '请填写更正理由。'); return; }
    const body = {
      diagnosis, examination,
      original_content_ref: f.original_content_ref.value,
      original_version_ref: f.original_version_ref.value,
      reason
    };
    await write('/api/clinical/encounters/' + encodeURIComponent(id) + '/diagnosis/corrections', body);
    clearDraft('diagnosis-correct:' + id);
    afterWrite('诊断已更正', '新版本已生成，旧版本仍可核对。');
  }
}

async function submitMedication(form, mode) {
  const f = form.elements;
  const id = f.encounter_ref.value;
  const drug = f.drug.value.trim();
  const amount = f.amount.value.trim();
  const unit = f.unit.value.trim();
  let valid = true;
  if (!drug) { markField(form, 'drug', '请填写药品。'); valid = false; }
  if (!amount) { markField(form, 'amount', '请填写剂量。'); valid = false; }
  if (!unit) { markField(form, 'unit', '请填写单位。'); valid = false; }
  if (mode === 'correct' && !f.reason.value.trim()) { markField(form, 'reason', '请填写更正理由。'); valid = false; }
  if (!valid) return;
  const body = { drug, amount, unit };
  if (mode === 'correct') {
    body.original_entry_ref = f.original_entry_ref.value;
    body.reason = f.reason.value.trim();
    await write('/api/clinical/encounters/' + encodeURIComponent(id) + '/medications/corrections', body);
    clearDraft('med-correct:' + f.original_entry_ref.value);
    afterWrite('用药已更正', '原条目保留，新条目已关联。');
  } else {
    await write('/api/clinical/encounters/' + encodeURIComponent(id) + '/medications', body);
    clearDraft('med-append:' + id);
    afterWrite('用药已追加', '用药链已刷新，原条目未被覆盖。');
  }
}

async function submitAdopt(form) {
  const f = form.elements;
  const id = f.encounter_ref.value;
  const suggestionRef = f.suggestion_ref.value;
  const reason = f.reason.value.trim();
  const diagnosis = f.diagnosis.value.trim();
  const examination = f.examination.value.trim();
  const drug = f.drug.value.trim();
  const amount = f.amount.value.trim();
  const unit = f.unit.value.trim();
  const medAny = !!(drug || amount || unit);
  let valid = true;
  if (!reason) { markField(form, 'reason', '采纳必须填写理由。'); valid = false; }
  if (!diagnosis && !examination && !medAny) { markField(form, 'diagnosis', '至少填写一项要写入的内容。'); valid = false; }
  if (medAny && !(drug && amount && unit)) { markField(form, medAny && !drug ? 'drug' : !amount ? 'amount' : 'unit', '用药三项要么全填，要么全部留空。'); valid = false; }
  if (!f.record_ref && !diagnosis) { markField(form, 'diagnosis', '本接诊尚无正式病历，采纳时必须有诊断内容才能生成首次病历。'); valid = false; }
  if (!valid) return;
  const content = {};
  if (diagnosis) content.diagnosis = diagnosis;
  if (examination) content.examination = examination;
  if (medAny) content.medication = { drug, amount, unit };
  const body = { encounter_ref: id, suggestion_ref: suggestionRef, adopted_content: content, reason };
  if (f.record_ref) {
    body.record_ref = f.record_ref.value;
    content.original_version_ref = f.original_version_ref.value;
  }
  await write('/api/clinical/suggestions/' + encodeURIComponent(suggestionRef) + '/adoption', body);
  clearDraft('adopt:' + suggestionRef);
  afterWrite('建议已采纳并写入', '来源保留；新版本已记录责任账号与理由。');
}

async function submitDecline(form) {
  const f = form.elements;
  const id = f.encounter_ref.value;
  const suggestionRef = f.suggestion_ref.value;
  const result = await write('/api/clinical/suggestions/' + encodeURIComponent(suggestionRef) + '/decline', {
    encounter_ref: id,
    suggestion_ref: suggestionRef,
    decision: 'declined',
    reason: f.reason.value.trim()
  });
  clearDraft('decline:' + suggestionRef);
  afterWrite('已登记不采纳', '建议与原文保留，未写入病历。');
}

async function submitResolveAlert(form) {
  const f = form.elements;
  const alertRef = f.alert_ref.value;
  const result = f.handling_result.value.trim();
  if (!result) { markField(form, 'handling_result', '请填写处理结果。'); return; }
  await write('/api/clinical/alerts/' + encodeURIComponent(alertRef) + '/resolution', { alert_ref: alertRef, handling_result: result });
  clearDraft('resolve:' + alertRef);
  afterWrite('告警已登记处理结果', '处理人、时间与结果已保留。');
}

async function submitGenerate(form) {
  const f = form.elements;
  const raw = f.definition.value.trim();
  const reason = f.reason.value.trim();
  let definition;
  try { definition = JSON.parse(raw); } catch (e) { markField(form, 'definition', '定义不是有效 JSON。'); return; }
  if (!reason) { markField(form, 'reason', '请填写生成理由。'); return; }
  const result = await write('/api/candidates/generate', { definition, reason });
  if (result.validation_results && !result.validation_results.ok) {
    const issues = (result.validation_results.issues || []).map((i) => (i.path || '') + ' ' + (i.message || '')).join('\n');
    formError(form, '定义未通过校验：' + issues);
    return;
  }
  closeModal();
  toast('候选已生成', '仍为草案；请继续提交、审批并手动启用。');
  state.selectedCandidate = result.candidate_ref;
  await loadSidebar();
  navigate('#/governance/' + result.candidate_ref);
}

async function submitCandidateAction(form) {
  const f = form.elements;
  const cmd = f.command.value;
  const id = f.candidate_ref.value;
  const reason = f.reason.value.trim();
  if (!reason) { markField(form, 'reason', '请填写操作理由。'); return; }
  if (cmd === 'submit') {
    await write('/api/candidates/submit', { candidate_ref: id, reason }, {});
  } else if (cmd === 'approve' || cmd === 'reject') {
    await write('/api/candidates/review', {
      candidate_ref: id, content_digest: f.content_digest.value,
      decision: cmd === 'approve' ? 'approve' : 'reject', reason
    });
  } else if (cmd === 'activate') {
    await write('/api/candidates/activate', {
      candidate_ref: id, expected_generation: Number(f.expected_generation.value), reason
    });
  }
  clearDraft('candidate:' + cmd + ':' + id);
  const labels = { submit: '候选已提交审批', approve: '候选已批准', reject: '候选已驳回', activate: '候选已启用' };
  closeModal();
  toast(labels[cmd] || '操作完成', cmd === 'activate' ? '本店生效版本已切换，旧定义保留。' : '步骤已记录，下一环节仍需人工操作。');
  await loadSidebar();
  await renderRoute();
}

async function handleForm(form) {
  const name = form.dataset.form;
  if (name === 'login') return doLogin(form);
  if (name === 'lookup-reception') {
    const ref = form.elements.ref.value.trim();
    if (!isUuid(ref)) { toast('请输入完整的接诊 UUID。', 'err'); return; }
    navigate('#/receptions/' + ref);
    return;
  }
  if (name === 'audit') {
    state.auditFilters = { record_ref: form.elements.record_ref.value.trim(), operator_ref: form.elements.operator_ref.value.trim() };
    await renderRoute();
    return;
  }
  if (name === 'new-reception') return runForm(form, () => submitNewReception(form).catch((e) => writeError(e, form)));
  if (name === 'reception-correct') return runForm(form, () => submitReceptionCorrect(form).catch((e) => writeError(e, form)));
  if (name === 'reception-misreg') return runForm(form, () => submitReceptionMisreg(form).catch((e) => writeError(e, form)));
  if (name === 'diagnosis-first') return runForm(form, () => submitDiagnosis(form, 'first').catch((e) => writeError(e, form)));
  if (name === 'diagnosis-correct') return runForm(form, () => submitDiagnosis(form, 'correct').catch((e) => writeError(e, form)));
  if (name === 'med-append') return runForm(form, () => submitMedication(form, 'append').catch((e) => writeError(e, form)));
  if (name === 'med-correct') return runForm(form, () => submitMedication(form, 'correct').catch((e) => writeError(e, form)));
  if (name === 'adopt') return runForm(form, () => submitAdopt(form).catch((e) => writeError(e, form)));
  if (name === 'decline') return runForm(form, () => submitDecline(form).catch((e) => writeError(e, form)));
  if (name === 'resolve-alert') return runForm(form, () => submitResolveAlert(form).catch((e) => writeError(e, form)));
  if (name === 'generate-candidate') return runForm(form, () => submitGenerate(form).catch((e) => writeError(e, form)));
  if (name === 'candidate-action') return runForm(form, () => submitCandidateAction(form).catch((e) => writeError(e, form)));
}

/* ---------------- 事件 ---------------- */

document.addEventListener('submit', (e) => {
  const form = e.target.closest('form[data-form]');
  if (!form) return;
  e.preventDefault();
  if (form.dataset.form === 'candidate-action') {
    // 防止双击重复提交；runForm 内会恢复
  }
  handleForm(form).catch((err) => {
    if (err instanceof ApiError) writeError(err, form);
    else toast(err.message || '操作未完成。', 'err');
  });
});

document.addEventListener('input', (e) => {
  const form = e.target.closest('form[data-draft]');
  if (form) saveDraft(form.dataset.draft, form);
  if (e.target.id === 'definition-json') updateDefinitionSummary();
});

document.addEventListener('change', (e) => {
  const form = e.target.closest('form[data-draft]');
  if (form) saveDraft(form.dataset.draft, form);
  if (e.target.matches('input[name="identity"]')) {
    const f = e.target.form;
    if (!f) return;
    const known = e.target.value === 'known';
    $('#new-pet-block', f).classList.toggle('hide', known);
    $('#known-pet-block', f).classList.toggle('hide', !known);
    $$('.choice', f).forEach((c) => c.classList.toggle('on', $('input', c).checked));
  }
  if (e.target.dataset.file === 'definition') {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    if (file.size > 1048576) { toast('定义文件超过 1 MiB，请缩小后重试。', 'err'); return; }
    file.text().then((text) => {
      const ta = $('#definition-json');
      if (ta) ta.value = text;
      const nm = $('#definition-file-name');
      if (nm) nm.textContent = file.name;
      updateDefinitionSummary();
    }).catch(() => toast('文件读取失败。', 'err'));
  }
});

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const run = async () => {
    switch (action) {
      case 'close-modal': closeModal(); return;
      case 'stay': { const box = $('#modal-error'); if (box) box.classList.add('hide'); return; }
      case 'reload-latest': {
        const fn = state.reloadLatest;
        if (!fn) return;
        state.reloadLatest = null;
        closeModal();
        await fn();
        await renderRoute();
        return;
      }
      case 'go-alerts': {
        const form = $('form[data-draft]');
        if (form) saveDraft(form.dataset.draft, form);
        closeModal(); navigate('#/alerts'); return;
      }
      case 'logout': {
        await api('/api/logout', { method: 'POST', body: {} }).catch(() => {});
        state.session = null; state.sidebar = null; state.route = null;
        history.replaceState(null, '', '#/');
        renderLogin(); return;
      }
      case 'relogin': location.reload(); return;
      case 'reload': await renderRoute(); return;
      case 'range': state.range = btn.dataset.range; state.receptionPage = 1; await renderRoute(); return;
      case 'reception-page': state.receptionPage = Number(btn.dataset.page) || 1; await renderRoute(); return;
      case 'alert-filter': state.alertFilter = btn.dataset.filter; await renderRoute(); return;
      case 'audit-reset': state.auditFilters = { record_ref: '', operator_ref: '' }; await renderRoute(); return;
      case 'regen-pet': {
        const input = $('#f-pet_ref');
        if (input) { input.value = uuid(); const form = input.form; if (form) saveDraft(form.dataset.draft, form); }
        return;
      }
      case 'species': {
        const input = $('#f-species');
        if (input) { input.value = btn.dataset.species; input.dispatchEvent(new Event('input', { bubbles: true })); }
        return;
      }
      case 'verify-pet': {
        const input = $('#f-known_ref'); const line = $('#verify-line');
        const value = input ? input.value.trim() : '';
        if (!isUuid(value)) { line.className = 'verify-line bad'; line.textContent = '请输入完整 UUID 后再核验。'; return; }
        line.className = 'verify-line'; line.textContent = '正在核验本店范围…';
        try {
          const res = await api('/api/encounters/create-options/pet-reference-check', { method: 'POST', body: { pet_ref: value } });
          line.className = 'verify-line ' + (res.associable ? 'ok' : 'bad');
          line.textContent = res.associable ? '可关联：该编号属于本店宠物，可在本店建档。' : '不可关联：本店没有该宠物编号。';
        } catch (err) { line.className = 'verify-line bad'; line.textContent = err.message; }
        return;
      }
      case 'copy-ref': {
        try { await navigator.clipboard.writeText(btn.dataset.ref || ''); toast('编号已复制', 'ok'); }
        catch (err) { toast('复制失败，请手动选择编号。', 'err'); }
        return;
      }
      case 'reception-correct': await openReceptionCorrect(btn.dataset.id); return;
      case 'reception-misreg': await openReceptionMisreg(btn.dataset.id); return;
      case 'diagnosis-first': openDiagnosisFirst(btn.dataset.id); return;
      case 'diagnosis-correct': await openDiagnosisCorrect(btn.dataset.id); return;
      case 'med-append': openMedicationAppend(btn.dataset.id); return;
      case 'med-correct': await openMedicationCorrect(btn.dataset.id, btn.dataset.entry); return;
      case 'adopt': await openAdopt(btn.dataset.id, btn.dataset.suggestion); return;
      case 'decline': await openDecline(btn.dataset.id, btn.dataset.suggestion); return;
      case 'resolve-alert': openResolveAlert(btn.dataset.alert); return;
      case 'locate-alert': {
        btn.disabled = true;
        try {
          const item = await api('/api/clinical/alerts/' + encodeURIComponent(btn.dataset.alert) + '/encounter');
          const ref = item.reception && item.reception.encounter_ref;
          if (!ref) throw new Error('未能定位真实父接诊。');
          navigate('#/clinical/' + ref);
        } catch (err) { toast(err.message || '定位失败。', 'err'); }
        finally { btn.disabled = false; }
        return;
      }
      case 'generate-candidate': openGenerateCandidate(); return;
      case 'example-definition': {
        btn.disabled = true;
        try {
          const d = await api('/api/example-definition');
          const ta = $('#definition-json');
          if (ta) ta.value = JSON.stringify(d.definition, null, 2);
          const nm = $('#definition-file-name');
          if (nm) nm.textContent = '演示定义已载入';
          updateDefinitionSummary();
          toast('已载入本店演示定义', 'ok', '尚未生成候选。');
        } catch (err) { toast(err.message, 'err'); }
        finally { btn.disabled = false; }
        return;
      }
      case 'candidate-action': await openCandidateAction(btn.dataset.cmd, btn.dataset.id); return;
      default: return;
    }
  };
  try { await run(); } catch (err) { toast(err.message || '操作未完成。', 'err'); }
});

/* ---------------- 启动 ---------------- */

async function boot() {
  await refreshSession();
  if (state.session) await enterApp();
  else renderLogin();
}

boot();

window.addEventListener('hashchange', () => {
  if (state.session) renderRoute();
});
