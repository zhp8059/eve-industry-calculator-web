/* EVE 工业计算器 — 前端（原生 JS，无任何依赖）
 * 视图：蓝图计算 / 工业项目 / 利润排行 / 库存 / 生产规划 / 价格对比 / 设置 / 关于
 */
'use strict';

/* ==================================================================
 * 工具
 * ================================================================== */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtISK(v, digits = 2) {
  if (v === null || v === undefined || v === '') return '-';
  const n = Number(v);
  if (!isFinite(n)) return '-';
  return n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtCompact(v) {
  const n = Number(v);
  if (!isFinite(n)) return '-';
  const a = Math.abs(n), s = n < 0 ? '-' : '';
  if (a >= 1e12) return s + (a / 1e12).toFixed(2) + ' 万亿';
  if (a >= 1e8) return s + (a / 1e8).toFixed(2) + ' 亿';
  if (a >= 1e4) return s + (a / 1e4).toFixed(2) + ' 万';
  return s + a.toLocaleString('en-US', { maximumFractionDigits: 2 });
}
function fmtNum(v, digits = 0) {
  const n = Number(v);
  if (!isFinite(n)) return '-';
  return n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtPct(v, digits = 2) {
  const n = Number(v);
  if (!isFinite(n)) return '-';
  return (n * 100).toFixed(digits) + '%';
}
function fmtDur(sec) {
  if (sec === null || sec === undefined) return '-';
  let s = Math.max(0, Math.floor(Number(sec)));
  if (!isFinite(s)) return '-';
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (d) return `${d}天${h}小时`;
  if (h) return `${h}小时${m}分`;
  if (m) return `${m}分${s}秒`;
  return `${s}秒`;
}
function fmtTime(ts) {
  if (!ts) return '-';
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
}
function iconUrl(typeId, size = 64, variant = 'icon') {
  return `https://images.evetech.net/types/${typeId}/${variant}?size=${size}`;
}

/* 物品图标（带兜底）
 * ------------------------------------------------------------------
 * CCP 的图片服务器对同一 type_id 提供不同**变体**，而哪种能用取决于这个类型本身
 * （对 images.evetech.net 实测）：
 *     普通物品：/icon = 200，/bp = 400
 *     蓝图类  ：/icon = 400，/bp = 200   ← 蓝图没有 icon 图，只有 bp 渲染图
 * 所以这里统一用「icon 加载失败就换成 bp」，这样不管传进来的是物品还是蓝图都能出图。
 * （以前工业项目里的拷贝/研究任务没有产物 type_id，就整行没有图标 —— 那是另一个 bug，
 *   见 viewJobs：现在会退回用 blueprint_type_id。）
 */
function typeIconHtml(typeId, size = 32, cls = '') {
  if (!typeId) return '';
  const attrs = cls ? ` class="${cls}"` : '';
  return `<img${attrs} src="${iconUrl(typeId, size)}"`
    + ` data-bp="${iconUrl(typeId, size, 'bp')}"`
    + ` onerror="this.onerror=null;this.src=this.dataset.bp"`
    + ` alt="" loading="lazy">`;
}
function debounce(fn, ms) {
  let t; return function (...a) { clearTimeout(t); t = setTimeout(() => fn.apply(this, a), ms); };
}
function cls(...names) { return names.filter(Boolean).join(' '); }
function pctClass(v) { return Number(v) > 0 ? 'pos' : (Number(v) < 0 ? 'neg' : 'dim'); }

/* ==================================================================
 * API
 * ================================================================== */
async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { ok: false, message: text }; }
  if (!data) data = { ok: false, message: 'HTTP ' + res.status + '（空响应）' };
  if (data.ok === undefined) data.ok = res.ok;
  return data;
}
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });
const del = (p) => api(p, { method: 'DELETE' });

/* ==================================================================
 * 全局状态
 * ================================================================== */
const LS_KEY = 'eve-industry-plan-v1';

const S = {
  view: 'blueprint',
  ready: false,
  status: null,
  sso: {},
  plan: [],                     // [{type_id, blueprint_type_id, name, runs, me, te, purchase_mode, activity}]
  facility: { structure_type_id: null, rig_type_ids: [], system_id: null, system_name: '', security: null },
  // 建筑燃料消耗：已选服务装备 + 最近一次计算结果 + 读取到的军团建筑
  fuel: { serviceTypeIds: [], plan: null, corp: null, message: '' },
  fuelServices: [],
  industry: { scc_surcharge: 0.04, job_tax_rate: 0.0025, alpha_clone_tax: 0,
              material_rounding: 'ceil', include_job_cost: true, include_job_tax: true,
              structure_material_bonus: 0, structure_time_bonus: 0, structure_cost_bonus: 0 },
  price: { source: 'jita_sell', market_scope: 'products' },
  inventory: { use: true, scope: null, include_corporation: true },
  build_modes: {},              // {"type_id": "build"|"purchase"}
  material_me: 10, material_te: 20,
  result: null,                 // /api/calc/aggregate 的结果
  bonusPreview: null,
  plans: [],
  searchResults: [],
  categories: [],
  structures: [],
  rigs: [],
  jobs: null,
  ranking: null,
  inventory_rows: null,
  planner: null,
  // 研究模拟
  research: {
    target: null,        // {type_id, name}
    info: null,          // /api/research/blueprint 的返回
    result: null,        // /api/research/simulate 的返回
    owned: null,         // /api/research/owned 的返回
    fromMe: 0, toMe: 10, fromTe: 0, toTe: 10,
    security: null, rigTypeIds: [],
    rigCandidates: [],
  },
  // 物价刷新
  market: {
    local: null,         // /api/market/local 的返回
    scope: 'essentials',
    running: false,
    lastTask: null,
  },
  lastError: null,
  // 登录相关
  loginInfo: null,          // /api/auth/login 返回的授权 URL / state / scopes
  loginTier: null,          // character | corporation（授权档位）
  savedFacility: null,      // config.json 里保存的生产设施（含 summary）
  lastSsoUrl: '',
  bannerDismissed: false,
  autoLoginTimer: null,
};

function savePlanLocal() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      plan: S.plan, facility: S.facility, industry: S.industry, price: S.price,
      inventory: S.inventory, build_modes: S.build_modes,
      material_me: S.material_me, material_te: S.material_te,
    }));
  } catch (e) { /* 忽略 */ }
}
function loadPlanLocal() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    const d = JSON.parse(raw);
    Object.assign(S, {
      plan: d.plan || [], facility: Object.assign(S.facility, d.facility || {}),
      industry: Object.assign(S.industry, d.industry || {}),
      price: Object.assign(S.price, d.price || {}),
      inventory: Object.assign(S.inventory, d.inventory || {}),
      build_modes: d.build_modes || {},
      material_me: d.material_me ?? 10, material_te: d.material_te ?? 20,
    });
  } catch (e) { /* 忽略 */ }
}

/* ==================================================================
 * 提示
 * ================================================================== */
function toast(message, kind = 'info', ms = 4200) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = esc(message).replace(/\n/g, '<br>');
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, ms);
}
function fail(res, fallback) {
  const msg = (res && (res.message || res.error)) || fallback || '操作失败';
  toast(msg, 'err', 6500);
  return msg;
}

/* ==================================================================
 * 导航
 * ================================================================== */
const VIEWS = [
  ['blueprint', '蓝图计算'], ['research', '研究模拟'], ['jobs', '工业项目'],
  ['ranking', '利润排行'], ['inventory', '库存'], ['planner', '生产规划'],
  ['compare', '价格对比'], ['settings', '设置'], ['about', '关于'],
];

function renderNav() {
  $('#nav').innerHTML = VIEWS.map(([key, label]) =>
    `<button data-view="${key}" class="${S.view === key ? 'active' : ''}">${esc(label)}</button>`).join('');
}

function go(view) {
  S.view = view;
  location.hash = '#/' + view;
  renderNav();
  render();
}

/* ==================================================================
 * 顶栏状态
 * ================================================================== */
function renderTopbar() {
  const st = S.status || {};
  const sdeChip = $('#sdeChip');
  const sde = st.sde || {};
  if (sde.running) {
    sdeChip.className = 'chip chip-warn';
    sdeChip.textContent = 'SDE 导入中 ' + (sde.progress && sde.progress.percent || 0) + '%';
  } else if (sde.ready) {
    sdeChip.className = 'chip chip-ok';
    sdeChip.textContent = 'SDE ' + (sde.local_build || '');
  } else {
    sdeChip.className = 'chip chip-warn';
    sdeChip.textContent = 'SDE 未导入';
  }
  const chars = (st.characters || []);
  const chip = $('#charChip');
  const active = chars.find(c => c.is_active) || chars[0];
  const configured = !!(st.sso && st.sso.configured);
  if (active) {
    const bad = active.link_state && active.link_state !== 'ok';
    chip.className = bad ? 'chip chip-warn' : 'chip chip-ok';
    chip.textContent = active.name + (active.corporation_name ? ' · ' + active.corporation_name : '')
      + (bad ? ' ⚠' : '');
    if (bad) chip.title = active.link_state_label || active.link_state;
  } else {
    chip.className = configured ? 'chip chip-warn' : 'chip';
    chip.textContent = '未登录';
  }
  const sel = $('#charSelect');
  sel.innerHTML = '<option value="">选择角色…</option>' +
    chars.map(c => `<option value="${c.character_id}" ${c.is_active ? 'selected' : ''}>${esc(c.name)}</option>`).join('') +
    '<option value="__login">+ 登录新角色</option>';
  sel.style.display = chars.length ? '' : 'none';

  // 顶栏登录按钮：未登录时高亮，登录后变成「账户」入口
  const btn = $('#loginBtn');
  if (btn) {
    if (active) {
      btn.className = 'btn btn-sm';
      btn.textContent = '账户';
      btn.title = '管理已授权角色';
    } else if (configured) {
      btn.className = 'btn btn-sm btn-green';
      btn.textContent = '登录 EVE';
      btn.title = '前往 EVE 官方页面授权你的角色';
    } else {
      btn.className = 'btn btn-sm';
      btn.textContent = '配置 Client ID';
      btn.title = '还没填 EVE 应用的 Client ID，点这里去设置';
    }
  }

  // 顶栏「物价」按钮：显示上次更新时间，数据过旧时高亮
  const priceBtn = $('#priceBtn');
  if (priceBtn) {
    const L = S.market.local;
    const ts = L && L.prices && L.prices.updated_at;
    const ageHours = ts ? (Date.now() / 1000 - ts) / 3600 : null;
    const stale = !ts || ageHours > 12;
    priceBtn.className = 'btn btn-sm' + (stale ? ' btn-green' : '');
    priceBtn.textContent = '物价 ' + (ts ? (stale ? '⚠' : '✓') : '—');
    priceBtn.title = ts
      ? '本地物价上次更新：' + new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
        + '（' + (ageHours < 1 ? Math.round(ageHours * 60) + ' 分钟前'
                              : Math.round(ageHours) + ' 小时前') + '）\n点击刷新'
      : '本地还没有物价数据，点击从 ESI 拉取并保存';
  }
  renderLoginBanner();
}

/* 军团授权是否缺失 —— 缺了就不用去请求军团接口，直接给出可操作的提示 */
function corpScopeMissing() {
  const chars = (S.status && S.status.characters) || [];
  if (!chars.length) return false;
  return chars.some(c => c.corp_scopes_missing && c.corp_scopes_missing.length);
}

function corpScopeNotice() {
  const chars = (S.status && S.status.characters) || [];
  const missing = chars.filter(c => c.corp_scopes_missing && c.corp_scopes_missing.length);
  if (!missing.length) return '';
  const names = missing.map(c => esc(c.name)).join('、');
  return `
    <div class="banner-inner" style="background:#1a2440; border:1px solid #2a3f66;
         border-radius:8px; margin-bottom:12px; align-items:flex-start">
      <span class="banner-icon">🔑</span>
      <div class="banner-text">
        <strong style="color:var(--blue)">军团数据尚未授权</strong>
        <span>${names} 登录时选的是「只授权个人数据」，所以军团工业项目 / 蓝图 / 资产目前读不到。
          这不是错误 —— 需要的话点右侧重新授权一次即可（只追加军团权限，个人数据不受影响）。</span>
      </div>
      <button class="btn btn-sm btn-green" data-action="relink-corp">追加军团授权</button>
    </div>`;
}

/* 关联状态：ok / owner_changed（角色换主人）/ token_dead（EVE 已拒绝这组令牌）
   —— 后两种都会暂停该角色的数据同步，必须在界面上说清楚「现在为什么不动了」。 */
function linkProblemChars() {
  return ((S.status && S.status.characters) || [])
    .filter(c => c.link_state && c.link_state !== 'ok');
}

function linkStateBadge(c) {
  const state = c.link_state || '';
  if (!state || state === 'ok') return '<span class="badge green">正常</span>';
  if (state === 'owner_changed') return '<span class="badge amber">角色已转手</span>';
  if (state === 'token_dead') return '<span class="badge red">登录已失效</span>';
  return `<span class="badge amber">${esc(c.link_state_label || state)}</span>`;
}

function renderLinkProblems() {
  const bad = linkProblemChars();
  if (!bad.length) return '';
  return `
    <div class="banner-inner" style="background:#2a2211; border:1px solid #5c4a1e;
         border-radius:8px; margin-bottom:12px; align-items:flex-start">
      <span class="banner-icon">⚠</span>
      <div class="banner-text">
        <strong style="color:var(--amber)">有 ${bad.length} 个角色需要你处理</strong>
        <span>${bad.map(c => esc(c.name) + '：' + esc(c.link_state_label || c.link_state)).join('；')}
          —— 处理前这些角色的数据不会被拉取，也不会用它们的令牌发请求。</span>
      </div>
    </div>`;
}

/* 页面自带的版本号（服务端把 index.html 里的 __APP_VERSION__ 换成真实版本） */
const PAGE_VERSION = (() => {
  const el = document.querySelector && document.querySelector('meta[name="app-version"]');
  const v = el && el.getAttribute ? (el.getAttribute('content') || '') : '';
  return v === '__APP_VERSION__' ? '' : v;      // 没被替换过就当作未知
})();

/* 页面版本与服务端版本不一致 = 浏览器还跑着旧的 app.js（换版本后没刷新页面）。
 * 必须主动说出来 —— 否则用户会奇怪「我明明更新了怎么还是旧行为」。
 * pageVersion 参数只是为了可测（默认取页面里的 meta）。 */
function pageVersionMismatch(pageVersion) {
  const page = pageVersion || PAGE_VERSION;
  const server = ((S.status || {}).app || {}).version || '';
  if (!page || !server) return null;
  if (String(page) === String(server)) return null;
  return { page: page, server: server };
}

/* 未登录时在内容区顶部挂一条提示条（不遮挡操作，可关闭） */
function renderLoginBanner() {
  const box = $('#banner');
  if (!box) return;
  const st = S.status || {};
  const chars = st.characters || [];
  const configured = !!(st.sso && st.sso.configured);

  // 数据库损坏优先提示：它会让各种读取莫名其妙地失败（例如「读取 xx 的挂单失败：
  // database disk image is malformed」），不说出来用户根本猜不到是数据文件坏了
  const db = st.db || {};
  if ((db.init_error || db.integrity_ok === false) && !S.bannerDismissed) {
    // SQLite 的自检报告（例如「*** in database main *** Tree 6 page 6 cell 0 ...」）
    // 对用户没有可读性 —— 正文给人话，原文放进悬停提示。
    const raw = String(db.init_error || db.integrity_message || '');
    const human = db.init_error
      ? '数据库文件无法打开（' + (raw.split(':').pop() || raw).trim().slice(0, 60) + '）'
      : '自检发现数据页损坏（B 树结构不一致）';
    box.hidden = false;
    box.innerHTML = `
      <div class="banner-inner">
        <span class="banner-icon">🛠</span>
        <div class="banner-text">
          <strong style="color:var(--red,#ff5c5c)">本地数据库损坏，部分数据会刷不出来</strong>
          <span title="${esc(raw.slice(0, 400))}">${esc(human)}
            <span class="dim mono">（鼠标悬停看自检原文）</span>
            <br>研究模拟、ESI 拉取（工业项目/蓝图/库存）都会因此失败 —— 数据库在这里只是缓存，
            点右边「重建数据库」重新导入 SDE 并重新登录即可恢复。
            <b>你会需要重新登录一次 EVE</b>。</span>
        </div>
        <button class="btn btn-sm" data-action="db-rebuild">重建数据库</button>
        <button class="btn btn-sm btn-ghost" data-action="banner-close" title="本次不再提示">×</button>
      </div>`;
    return;
  }

  // 版本不一致优先提示：它会导致「明明修好了却还是旧行为」这种最费解的困惑
  const mismatch = pageVersionMismatch();
  if (mismatch) {
    box.hidden = false;
    box.innerHTML = `
      <div class="banner-inner">
        <span class="banner-icon">♻</span>
        <div class="banner-text">
          <strong style="color:var(--amber)">页面代码是旧版本（v${esc(mismatch.page)}），
            程序已经是 v${esc(mismatch.server)}</strong>
          <span>浏览器还在用缓存的旧页面，所以新修的东西看不到 ——
            按 <b>Ctrl+F5</b>（Mac 是 Cmd+Shift+R）强制刷新即可。
            <span class="dim">若刷新后仍是旧版本，说明你运行的是另一个目录里的旧副本。</span></span>
        </div>
        <button class="btn btn-sm btn-ghost" data-action="banner-close" title="本次不再提示">×</button>
      </div>`;
    return;
  }

  // 有关联状态异常的角色时，这条提示优先级最高（它在解释「为什么数据不动了」）
  const problems = chars.filter(c => c.link_state && c.link_state !== 'ok');
  if (problems.length) {
    box.hidden = false;
    box.innerHTML = `
      <div class="banner-inner">
        <span class="banner-icon">⚠</span>
        <div class="banner-text">
          <strong style="color:var(--amber)">有 ${problems.length} 个角色的授权需要你确认</strong>
          <span>${problems.map(c => esc(c.name) + '：' + esc(c.link_state_label || c.link_state)).join('；')}
            —— 到「设置 → 角色与缓存」点「确认已转手」或重新登录即可恢复。</span>
        </div>
        <button class="btn btn-sm" data-action="goto-settings">去处理</button>
        <button class="btn btn-sm btn-ghost" data-action="banner-close" title="本次不再提示">×</button>
      </div>`;
    return;
  }

  if (chars.length || S.bannerDismissed) { box.hidden = true; box.innerHTML = ''; return; }
  if (!configured) {
    box.hidden = false;
    box.innerHTML = `
      <div class="banner-inner">
        <span class="banner-icon">⚙</span>
        <div class="banner-text">
          <strong>还没有授权任何 EVE 角色</strong>
          <span>读取工业项目 / 蓝图 / 库存需要你本人的 ESI 授权。程序已内置应用凭据，点一下就能登录。</span>
        </div>
        <button class="btn btn-sm" data-action="goto-settings">去设置</button>
        <button class="btn btn-sm btn-ghost" data-action="banner-close" title="本次不再提示">×</button>
      </div>`;
    return;
  }
  box.hidden = false;
  box.innerHTML = `
    <div class="banner-inner">
      <span class="banner-icon">🔑</span>
      <div class="banner-text">
        <strong>还没有授权任何 EVE 角色（不影响计算）</strong>
        <span><b>未登录也能用</b>：蓝图材料 / 成本 / 利润 / 研究模拟都只依赖本地 SDE 数据。
          登录后<b>额外</b>能读到你自己的工业项目、蓝图 ME/TE 与库存，用来自动抵扣材料。
          点右侧按钮会跳到 EVE 官方登录页（login.eveonline.com），同意权限后自动回到本程序。</span>
      </div>
      <button class="btn btn-sm btn-green" data-action="login">立即登录 EVE</button>
      <button class="btn btn-sm btn-ghost" data-action="banner-close" title="本次不再提示">×</button>
    </div>`;
}

/* ==================================================================
 * 侧栏
 * ================================================================== */
function renderSidebar() {
  renderPlanList();
}

function renderPlanList() {
  const box = $('#planList');
  if (!S.plan.length) {
    box.innerHTML = '<div class="empty" style="padding:20px 6px">还没有产品。<br>在「所有物品」里搜索并点击加入。</div>';
    return;
  }
  box.innerHTML = S.plan.map((p, i) => `
    <div class="search-result" data-plan-index="${i}" title="点击定位到该产品卡">
      ${typeIconHtml(p.type_id, 32, '')}
      <div class="nm">${esc(p.name || ('类型 ' + p.type_id))}
        <small>ME ${p.me} · TE ${p.te} · ${fmtNum(p.runs)} 流程</small></div>
      <button class="btn btn-xs btn-ghost" data-action="plan-remove" data-index="${i}">×</button>
    </div>`).join('');
}

async function doSearch(query) {
  if (!query.trim()) { showCategoryTree(); return; }
  const res = await api('/api/sde/search-blueprint?q=' + encodeURIComponent(query) + '&limit=40');
  if (!res.ok) return fail(res, '搜索失败');
  S.searchResults = res.results || [];
  S.browsingCategory = null;
  renderSearchResults();
  $('#categoryTree').style.display = 'none';
}

function renderSearchResults() {
  const box = $('#searchResults');
  if (!S.searchResults.length) { box.innerHTML = ''; box.style.display = 'none'; return; }
  box.style.display = '';
  box.innerHTML = S.searchResults.map(r => `
    <div class="search-result" data-add-product="${r.type_id}"
         data-blueprint="${r.blueprint_type_id}" data-name="${esc(r.name)}">
      ${typeIconHtml(r.type_id, 32, '')}
      <div class="nm">${esc(r.name)}<small>每流程产出 ${r.product_quantity}</small></div>
      <span class="badge green">+</span>
    </div>`).join('');
}

async function loadCategories() {
  const res = await api('/api/sde/categories');
  if (!res.ok) return;
  S.categories = res.categories || [];
  S.marketRoots = res.market_groups || [];        // 顶层市场分组（与游戏侧栏第一层一致）
  if (!S.marketRoots.length) {
    if (!S.categories.length) {
      $('#categoryTree').innerHTML =
        '<div class="hint" style="padding:10px">还没有 SDE 数据。到「设置」里点「下载并导入最新 SDE」。</div>';
      return;
    }
    // 兜底：万一 market_groups 没导入，退回原来的扁平分类列表
    renderMarketNodes(S.categories.map(c => ({ market_group_id: c.category_id, name: c.name,
                                               types: c.types, _category: true })), '所有物品', 0);
    return;
  }
  renderMarketNodes(S.marketRoots, '所有物品', 0);
}

/* 市场树：把"当前层"的子分组渲染进 #categoryTree，物品渲染进 #searchResults。
 * 数据全部来自本地 SDE（market_groups 2114 个分组 + types.market_group_id），
 * 所以**不需要 ESI、不需要登录**就能浏览全部物品。 */
function renderMarketNodes(children, title, count) {
  const tree = $('#categoryTree');
  if (!tree) return;
  const path = S.marketPath || [];
  const back = path.length
    ? `<div class="tree-item" data-action="mg-back" style="color:var(--green)">← 返回上级</div>`
    : '';
  const rows = (children || []).filter(c => !c.types || c.types > 0 || c._category).map(c => `
    <div class="tree-item${c._category && !c.types ? '' : ''}" data-mg="${c.market_group_id}"
         data-name="${esc(c.name || '')}">
      <span>${esc(c.name || '')}</span>
      <span class="dim">${c.types ? fmtNum(c.types) : (c._category ? '' : '▸')}</span>
    </div>`).join('') || '<div class="hint-tiny" style="padding:8px">这个分组下面没有子分组</div>';
  tree.innerHTML = back +
    `<div class="hint-tiny" style="padding:4px 6px">${esc(title || '')}${
      count ? ' · 共 ' + fmtNum(count) + ' 个物品' : ''}</div>` + rows;
  tree.style.display = '';
}

function renderMarketItems(rows, title) {
  const box = $('#searchResults');
  if (!box) return;
  if (!rows || !rows.length) {
    box.innerHTML = `<div class="hint" style="padding:6px">${esc(title || '')}：这个分组里没有直接列出的物品，
      请点上面的子分组继续往下展开。</div>`;
    box.style.display = '';
    return;
  }
  box.style.display = '';
  box.innerHTML =
    `<div class="hint-tiny" style="padding:2px 6px 6px">${esc(title || '')} · ${rows.length} 个物品</div>` +
    rows.map(r => {
      const isBlueprint = Number(r.category_id) === 9 || /蓝图$/.test(r.name || '');
      const bp = r.blueprint_type_id
        ? `<button class="btn btn-xs btn-ghost" data-research-bp="${r.blueprint_type_id}"
             data-name="${esc(r.blueprint_name || r.name + ' 蓝图')}"
             title="把这个物品的蓝图原本载入研究模拟（纯本地计算，无需登录）">研究蓝图</button>`
        : '';
      return `
      <div class="search-result" data-add-product="${r.type_id}" data-name="${esc(r.name)}">
        ${typeIconHtml(r.type_id, 32, '')}
        <div class="nm">${esc(r.name)}
          <small>${isBlueprint ? '蓝图物品' : '物品'}${
            r.blueprint_name ? ' · 蓝图原本：' + esc(r.blueprint_name) : ''}</small></div>
        ${isBlueprint ? '' : '<span class="badge green">+</span>'}
        ${bp}
      </div>`;
    }).join('');
}

async function showMarketGroup(groupId, label) {
  const res = await api('/api/sde/market-group/' + encodeURIComponent(groupId) + '?limit=300');
  if (!res.ok) return fail(res, '读取市场分组失败');
  const name = label || (res.group || {}).name || ('分组 ' + groupId);
  const path = (S.marketPath || []).filter(x => Number(x.id) !== Number(groupId));
  path.push({ id: Number(groupId), name: name });
  S.marketPath = path;
  S.browsingCategory = name;
  renderMarketNodes(res.children || [], name, res.count || 0);
  renderMarketItems(res.types || [], name);
}

function marketBack() {
  const path = S.marketPath || [];
  path.pop();                                  // 丢掉当前层
  S.marketPath = path;
  const parent = path[path.length - 1];
  if (!parent) {
    S.browsingCategory = null;
    S.searchResults = [];
    S.marketPath = [];
    loadCategories();
    const box = $('#searchResults');
    if (box) { box.innerHTML = ''; box.style.display = 'none'; }
    return;
  }
  showMarketGroup(parent.id, parent.name);
}

function showCategoryTree() {
  // 回到「所有物品」顶层：清掉搜索/浏览状态，重新渲染市场树第一层
  S.searchResults = [];
  S.browsingCategory = null;
  S.marketPath = [];
  const box = $('#searchResults');
  if (box) { box.innerHTML = ''; box.style.display = 'none'; }
  const tree = $('#categoryTree');
  if (tree) tree.style.display = '';
  if ((S.marketRoots || []).length) {
    renderMarketNodes(S.marketRoots, '所有物品', 0);
  } else {
    loadCategories();
  }
}

function addProduct(typeId, blueprintTypeId, name) {
  if (S.plan.some(p => Number(p.type_id) === Number(typeId))) {
    toast('「' + name + '」已经在计划里了', 'warn');
    return;
  }
  S.plan.push({
    type_id: Number(typeId),
    blueprint_type_id: blueprintTypeId ? Number(blueprintTypeId) : null,
    name: name || ('类型 ' + typeId),
    runs: 1, me: 10, te: 20, purchase_mode: 'build', activity: 'manufacturing',
  });
  savePlanLocal();
  renderSidebar();
  if (S.view !== 'blueprint') go('blueprint'); else calculate();
  toast('已加入计划：' + (name || typeId), 'ok', 2200);
}

/* ==================================================================
 * 计算
 * ================================================================== */
function planPayload() {
  return {
    items: S.plan.map(p => ({
      type_id: p.type_id, blueprint_type_id: p.blueprint_type_id,
      runs: Number(p.runs) || 1, me: Number(p.me), te: Number(p.te),
      purchase_mode: p.purchase_mode, activity: p.activity,
    })),
    facility: S.facility,
    industry: S.industry,
    price: S.price,
    inventory: S.inventory,
    market_scope: S.price.market_scope,
    build_modes: S.build_modes,
    material_me: S.material_me, material_te: S.material_te,
  };
}

let calcTimer = null;
async function calculate(immediate) {
  if (!S.plan.length) { S.result = null; render(); return; }
  clearTimeout(calcTimer);
  const run = async () => {
    const box = $('#main');
    const res = await post('/api/calc/aggregate', planPayload());
    if (!res.ok) { S.lastError = fail(res, '计算失败'); return; }
    S.result = res;
    if (res.warnings && res.warnings.length) {
      res.warnings.slice(0, 3).forEach(w => toast(w, 'warn', 5200));
    }
    render();
  };
  if (immediate) return run();
  calcTimer = setTimeout(run, 350);
}

/* ==================================================================
 * 视图：蓝图计算（主界面）
 * ================================================================== */
function viewBlueprint() {
  if (!S.status || !(S.status.sde || {}).ready) {
    return `<div class="card"><div class="card-title"><strong>还没有导入静态数据（SDE）</strong></div>
      <div class="hint">蓝图、材料、建筑加成等数据都来自 CCP 官方 SDE。首次使用需要下载一次（约 95 MB，通常 10 秒内完成）。</div>
      <div class="row" style="margin-top:12px">
        <button class="btn btn-primary" data-action="sde-update">下载并导入 SDE</button>
        <span class="hint" id="sdeProgress"></span></div></div>`;
  }
  const r = S.result;
  const totals = (r && r.product_totals) || { total_cost: 0, total_price: 0, margin: 0, total_profit: 0 };
  const mtot = (r && r.totals) || { material_cost: 0, market_value: 0, profit: 0, volume: 0, margin: 0 };
  const jobCost = (r && r.products ? r.products.reduce((s, x) => s + (x.job_cost || 0), 0) : 0);
  const materialOnlyMargin = mtot.material_cost
    ? (totals.total_price - mtot.material_cost) / mtot.material_cost : 0;

  return `
  <div class="summary">
    <div class="card"><div class="label">总成本（材料 + 任务费用）</div>
      <div class="value">${fmtCompact(totals.total_cost)} <span class="dim" style="font-size:12px">ISK</span></div>
      <div class="sub">材料 ${fmtCompact(mtot.material_cost)} + 任务费用 ${fmtCompact(jobCost)}</div>
      <div class="sub mono">${fmtISK(totals.total_cost)} ISK</div></div>
    <div class="card"><div class="label">总价格（产品市值）</div>
      <div class="value blue">${fmtCompact(totals.total_price)} <span class="dim" style="font-size:12px">ISK</span></div>
      <div class="sub">仅按材料计的成本对应利润率 ${fmtPct(materialOnlyMargin)}
        <span class="dim">（部分第三方站点只用材料成本）</span></div></div>
    <div class="card"><div class="label">利润率（利润 ÷ 成本）</div>
      <div class="value ${pctClass(totals.margin)}">${fmtPct(totals.margin)}</div>
      <div class="sub">总利润 <span class="${pctClass(totals.total_profit)}">${fmtCompact(totals.total_profit)} ISK</span></div></div>
    <div class="card"><div class="label">材料总成本 / 体积</div>
      <div class="value" style="font-size:17px">${fmtCompact(mtot.material_cost)}</div>
      <div class="sub">${fmtNum(mtot.volume, 0)} m³ · ${(r && r.rows ? r.rows.length : 0)} 种原料</div></div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>生产设施</strong>
      <span class="hint-tiny">加成按官方 SDE 属性逐项读取；改装件加成会按生产星系安等放大（高安 ×1.0 / 低安 ×1.9 / 00 ×2.1）</span></div>
    ${viewFacilityForm()}
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>多种产品</strong>
      <span class="row" style="gap:6px">
        <span class="hint-tiny">ME/TE/流程数可逐个调整</span>
        <button class="btn btn-xs" data-action="plan-add-open">+ 添加产品</button>
      </span></div>
    ${S.plan.length ? `<div class="products">${S.plan.map(viewProductCard).join('')}</div>`
                    : '<div class="empty">还没有产品。用左侧搜索，或点「+ 添加产品」。</div>'}
  </div>

  ${r ? viewResultTables(r) : ''}`;
}

/* 建筑自身加成的可读描述（很多建筑没有自身加成，靠改装件） */
function structureBonusText(s) {
  if (!s) return '';
  const parts = [];
  const add = (label, factor) => {
    const pct = (1 - Number(factor || 1)) * 100;
    if (Math.abs(pct) > 0.005) parts.push(`${label}${pct > 0 ? '-' : '+'}${Math.abs(pct).toFixed(2)}%`);
  };
  add('材料', s.material_factor);
  add('时间', s.time_factor);
  add('费用', s.cost_factor);
  if (!parts.length) {
    // 有自身加成属性、但只对别的活动生效（例如"地平线"铁壁只给科研）
    if (s.has_own_bonus && s.applies_to_activity === false && s.activity_scope) {
      return `仅对「${s.activity_scope}」生效，本活动无加成`;
    }
    return '无自身加成（靠改装件）';
  }
  return parts.join('/');
}

/* 已保存的生产设施：提示 + 开机恢复 */
function savedFacilityHint() {
  const res = S.savedFacility;
  if (!res || !res.summary) return '';
  const when = res.saved_at ? new Date(res.saved_at * 1000).toLocaleString() : '';
  return `<div class="hint-tiny" style="margin-top:6px">
    ✔ 已保存的生产设施：<b>${esc(res.summary)}</b>${when ? '（' + esc(when) + '）' : ''}
    <button class="btn btn-xs btn-ghost" data-action="facility-clear">清除</button></div>`;
}

/* 开机时把 config.json 里保存的设施套用到当前选择（本机草稿优先） */
function restoreSavedFacility() {
  const saved = (S.status && S.status.facility) || null;
  const hasSummary = !!(saved && saved.summary);
  S.savedFacility = hasSummary ? saved : null;
  if (!hasSummary) return;
  // 本地草稿（localStorage）里已经有选择时不动它 —— 用户可能正在试别的配置
  const draft = S.facility || {};
  const empty = !draft.structure_type_id
    && !(draft.rig_type_ids || []).length && !draft.system_id;
  if (!empty) return;
  S.facility = Object.assign({}, draft, {
    structure_type_id: saved.structure_type_id || null,
    rig_type_ids: (saved.rig_type_ids || []).map(Number),
    system_id: saved.system_id || null,
    system_name: saved.system_name || '',
    security: saved.security || null,
  });
  if ((saved.service_type_ids || []).length) {
    S.fuel = S.fuel || { serviceTypeIds: [], plan: null, corp: null, message: '' };
    S.fuel.serviceTypeIds = saved.service_type_ids.map(Number);
  }
}

async function saveFacility() {
  const res = await post('/api/config', { facility: Object.assign({}, S.facility, {
    service_type_ids: (S.fuel || {}).serviceTypeIds || [],
  }) });
  if (!res.ok) return fail(res, '保存生产设施失败');
  await refreshStatus(true);
  restoreSavedFacility();
  render();
  toast('已保存生产设施：' + ((S.savedFacility && S.savedFacility.summary) || '当前配置'), 'ok', 4200);
}

async function clearSavedFacility() {
  const res = await post('/api/config', { facility: {} });
  if (!res.ok) return fail(res, '清除失败');
  S.savedFacility = null;
  await refreshStatus(true);
  render();
  toast('已清除保存的生产设施（当前界面上的选择保持不变）', 'ok', 3600);
}

/* ==================================================================
 * 建筑燃料消耗
 * ------------------------------------------------------------------
 * 数据全部来自 SDE 属性：
 *   服务装备 2109 = 每小时消耗的燃料块、2110 = 上线一次性消耗
 *   建筑     2339 = 燃料减免百分比（负值）、2056 = 服务装备槽位
 * 「读取军团建筑」会走 ESI 读你军团建筑的**实际在线服务**，
 * 免得靠猜（需要军团授权 + 总监/空间站经理职位）。
 * ================================================================== */
/* 服务装备在当前建筑里能不能吃到燃料减免（按市场分组分类匹配） */
function serviceDiscountHint(s) {
  const cat = s.category ? '（' + s.category + '）' : '';
  if (s.in_fuel_scope) return `${cat} ✓ 减免后 ${fmtNum((s.fuel_per_hour || 0) * (S.fuelPlanMultiplier || 0.75), 2)}/小时`;
  return `${cat} · 不适用减免`;
}

function fuelSectionHtml() {
  const f = S.fuel || (S.fuel = { serviceTypeIds: [], plan: null, corp: null, message: '' });
  const svc = S.fuelServices || [];
  const byId = {};
  svc.forEach(s => { byId[Number(s.type_id)] = s; });
  const plan = f.plan;
  const picked = (f.serviceTypeIds || []).map(id => byId[Number(id)] || { type_id: id, name: '类型 ' + id });
  const structure = (S.structures || []).find(s => Number(s.type_id) === Number(S.facility.structure_type_id));
  const slots = structure ? (structure.rig_slots && 0) || serviceSlotsOf(plan) : 0;
  return `
  <div style="grid-column: 1 / -1">
  <div class="card-title" style="margin-top:14px"><strong>燃料消耗</strong>
    <span class="hint-tiny">每小时燃料块 = Σ(在线服务装备) × (1 + 建筑减免)</span></div>
  <div class="row" style="gap:6px; flex-wrap:wrap; align-items:center">
    <select id="fuelServiceSelect" style="flex:1; min-width:260px">
      <option value="">＋ 添加服务装备…${svc.length ? '（共 ' + svc.length + ' 种）' : '（先导入 SDE）'}</option>
      ${svc.map(s => `<option value="${s.type_id}">${s.recommended ? '★ ' : ''}${esc(s.name)}
        　${s.fuel_per_hour}/小时${serviceDiscountHint(s)}</option>`).join('')}
    </select>
    <label class="hint-tiny" style="margin:0">燃料</label>
    <select id="fuelTypeSelect" style="width:auto">
      ${(S.fuelTypes || []).map(t => `<option value="${t.type_id}"${Number(f.fuelTypeId || defaultFuelTypeId()) === Number(t.type_id) ? ' selected' : ''}
        >${esc(t.name)}${t.price ? '（' + fmtISK(t.price) + '）' : '（无本地价）'}</option>`).join('')}
    </select>
    <button class="btn btn-ghost btn-xs" data-action="corp-structures-read"
      title="从 ESI 读取你军团建筑的实际在线服务装备（需要军团授权 + 总监/空间站经理职位）">读取军团建筑</button>
    <button class="btn btn-ghost btn-xs" data-action="fuel-clear">清空</button>
  </div>
  ${picked.length ? `<div class="rigs" style="margin-top:8px">${picked.map(s => `
      <span class="rig-chip" title="${esc(s.category || '未分类')}${s.in_fuel_scope ? '：可享建筑减免' : '：不适用建筑减免'}">${esc(s.name)}
        <span class="dim">${s.fuel_per_hour}/h${s.in_fuel_scope ? ' ✓' : ''}</span>
        <button data-action="fuel-remove" data-id="${s.type_id}" title="移除">×</button></span>`).join('')}</div>`
    : '<div class="hint-tiny" style="margin-top:8px">还没有选择服务装备。没开服务装备的建筑不烧燃料。</div>'}
  ${plan ? `
    <div class="kv" style="margin-top:10px">
      <dt>建筑减免</dt><dd>${plan.structure_fuel_bonus ? plan.structure_fuel_bonus.toFixed(0) + '%'
        + (structure ? '（' + esc(structure.name) + '）' : '') : '无（未选建筑）'}
        ${(plan.fuel_scope_names || []).length
          ? `<span class="dim">· 只对「${esc(plan.fuel_scope_names.join('、'))}」生效</span>`
          : '<span class="dim">· 该建筑没有燃料减免</span>'}</dd>
      <dt>消耗量</dt><dd><b>${fmtNum(plan.fuel_per_hour, 2)}</b> 块/小时 ·
        ${fmtNum(plan.fuel_per_day, 1)} 块/天 · <b>${fmtNum(plan.fuel_per_month, 0)}</b> 块/30 天
        <span class="dim">（原始 ${fmtNum(plan.base_fuel_per_hour, 2)}/小时 = ${fmtNum(plan.base_fuel_per_hour * 24, 0)}/天，
        再乘建筑减免 ×${Number(plan.fuel_multiplier || 1).toFixed(2)}）</span></dd>
      <dt>燃料</dt><dd>${esc(plan.fuel_type_name || '通用燃料块')}
        ${plan.unit_price ? '单价 ' + fmtISK(plan.unit_price) : '<span class="dim">（本地没有物价，点「刷新物价」后可算钱）</span>'}</dd>
      <dt>费用</dt><dd>${plan.unit_price
        ? `<b>${fmtISK(plan.cost_per_month)} ISK</b> / 30 天（${fmtISK(plan.cost_total)} / ${plan.hours} 小时）`
        : '—'}</dd>
      ${plan.unit_price ? '' : ''}
    </div>` : ''}
  ${(plan && plan.unknown_service_ids && plan.unknown_service_ids.length)
    ? `<div class="hint-tiny" style="color:var(--amber)">有 ${plan.unknown_service_ids.length} 个服务装备
       id 不在本地数据里（${esc(plan.unknown_service_ids.join('、'))}），它们没有计入消耗。</div>` : ''}
  ${(plan && plan.notes || []).map(n => `<div class="hint-tiny" style="color:var(--amber)">${esc(n)}</div>`).join('')}
  ${fuelCorpHtml()}
  <div class="hint-tiny" style="margin-top:8px; line-height:1.7">
    ${(plan && plan.mechanics ? plan.mechanics : [
      '建筑本身不消耗燃料，只有在线的服务装备消耗（CCP 官方《建筑管理》）。',
      '服务首次上线时一次性消耗约 3 天的量；之后每小时消耗一次。',
      '可用燃料块只有 4 种：氮 / 氢 / 氦 / 氧燃料块。',
    ]).map(t => '· ' + esc(t)).join('<br>')}
    ${(S.fuelBlocksPerUnit && S.fuelBlocksPerUnit !== 1)
      ? '<br>· 当前换算系数：1 单位 = ' + S.fuelBlocksPerUnit + ' 个燃料块' : ''}
  </div>
  </div>`;
}

function defaultFuelTypeId() {
  const types = S.fuelTypes || [];
  const withPrice = types.find(t => t.price);
  return (withPrice || types[0] || {}).type_id || 4051;
}

function serviceSlotsOf(plan) {
  return plan ? plan.service_slots : 0;
}

function fuelCorpHtml() {
  const f = S.fuel || {};
  if (!f.message && !(f.corp || []).length) return '';
  const rows = f.corp || [];
  return `
  <div style="margin-top:10px; padding:8px 10px; border:1px solid #2a3f66; border-radius:8px; background:#16203a">
    ${f.message ? `<div class="hint-tiny">${esc(f.message)}</div>` : ''}
    ${rows.length ? `<div class="hint-tiny" style="margin-bottom:6px">军团建筑 ${rows.length} 个
      ${f.unmatched && f.unmatched.length ? '· 有 ' + f.unmatched.length + ' 个服务名没能匹配（' + esc(f.unmatched.join('、')) + '）' : ''}</div>
      <div class="table-wrap"><table class="grid"><thead><tr><th class="name">建筑</th><th>在线服务</th>
      <th>燃料到期</th><th class="center">操作</th></tr></thead><tbody>
      ${rows.map(r => `<tr>
        <td class="name">${esc(r.name || ('结构 ' + r.structure_id))}</td>
        <td class="dim">${(r.online_services || []).map(s => esc(s.name)
          + (s.module_type_id ? '' : ' ⚠')).join('、') || '（没有在线服务）'}</td>
        <td class="dim">${esc(r.fuel_expires || '—')}</td>
        <td class="center"><button class="btn btn-xs btn-green" data-action="corp-structure-apply"
          data-structure="${r.structure_id}" data-type="${r.type_id || ''}">套用到燃料计算</button></td>
      </tr>`).join('')}
      </tbody></table></div>` : ''}
  </div>`;
}

async function loadFuelServices() {
  const activity = (S.facility && S.facility.activity) || 'manufacturing';
  const res = await api('/api/fuel/services?structure_type_id='
    + encodeURIComponent(S.facility.structure_type_id || '') + '&activity=' + encodeURIComponent(activity));
  if (res.ok) {
    S.fuelServices = res.services || [];
    S.fuelTypes = res.fuel_types || [];
    S.fuelBlocksPerUnit = res.fuel_blocks_per_unit || 1;
    const f = S.fuel || (S.fuel = { serviceTypeIds: [], plan: null, corp: null, message: '' });
    if (!f.fuelTypeId) {
      const withPrice = (S.fuelTypes || []).find(t => t.price);
      f.fuelTypeId = (withPrice || S.fuelTypes[0] || {}).type_id
        || res.fuel_type_id || 4051;
    }
  }
}

async function calcFuel() {
  const f = S.fuel || (S.fuel = { serviceTypeIds: [], plan: null, corp: null, message: '' });
  if (!(f.serviceTypeIds || []).length) { f.plan = null; render(); return; }
  const res = await post('/api/fuel/plan', {
    structure_type_id: S.facility.structure_type_id,
    service_type_ids: f.serviceTypeIds,
    fuel_type_id: f.fuelTypeId || defaultFuelTypeId(),
    hours: 720,
  });
  f.plan = res.ok ? res : null;
  if (!res.ok) toast(res.message || '燃料计算失败', 'err');
  render();
}

async function readCorpStructures() {
  const f = S.fuel || (S.fuel = { serviceTypeIds: [], plan: null, corp: null, message: '' });
  f.message = '正在从 ESI 读取军团建筑…';
  render();
  const res = await post('/api/corp/structures/refresh', {});
  f.corp = res.structures || [];
  f.unmatched = res.unmatched_services || [];
  f.message = res.ok
    ? `已读取 ${res.count || 0} 个军团建筑。点「套用到燃料计算」会把该建筑与其在线服务填进来。`
    : (res.message || '读取失败') + (res.hint ? '（' + res.hint + '）' : '');
  render();
}

function applyCorpStructure(structureId, typeId) {
  const f = S.fuel || (S.fuel = { serviceTypeIds: [], plan: null, corp: null, message: '' });
  const row = (f.corp || []).find(r => Number(r.structure_id) === Number(structureId));
  if (!row) return;
  if (typeId) S.facility.structure_type_id = Number(typeId);
  const ids = (row.online_services || [])
    .map(s => Number(s.module_type_id)).filter(Boolean);
  f.serviceTypeIds = ids;
  f.message = ids.length
    ? `已套用「${row.name}」的 ${ids.length} 个在线服务`
    : `「${row.name}」没有可识别的在线服务（服务名可能需要更新映射）`;
  if (S.facility.structure_type_id) S.facility.rig_type_ids = S.facility.rig_type_ids || [];
  render();
  calcFuel();
}

function viewFacilityForm() {
  const f = S.facility;
  const structures = S.structures || [];
  const rigs = S.rigs || [];
  const current = structures.find(s => Number(s.type_id) === Number(f.structure_type_id));
  // 改装件尺寸必须与建筑一致（SDE 属性 1547：建筑与改装件都带 rigSize）
  const rigSize = current ? Number(current.rig_size) : 0;
  const rigsForSize = rigs.filter(g => !g.is_structure
    && (!rigSize || Number(g.rig_size) === rigSize));
  const sec = f.security || '';
  return `
  <div class="facility">
    <div class="field"><label>生产星系（决定成本指数与安等倍率）</label>
      <div class="row" style="gap:6px">
        <input id="systemSearch" type="text" placeholder="输入星系名搜索，如 Jita"
               value="${esc(f.system_name || '')}" style="flex:1">
        <button class="btn btn-sm" data-action="system-search">查</button>
        <button class="btn btn-sm btn-ghost" data-action="system-best" title="找成本指数最低的高安星系">最低</button>
      </div>
      ${f.system_id ? `<div class="hint-tiny mono">系统 ${f.system_id} · 成本指数 ${Number(S.bonusPreview && S.bonusPreview.cost_index || 0).toFixed(6)} · 安等 ${f.security_status !== undefined && f.security_status !== null ? Number(f.security_status).toFixed(2) : '?'}</div>` : '<div class="hint-tiny">未选择时按高安（×1.0）计算，成本指数为 0</div>'}
    </div>
    <div class="field"><label>工业建筑</label>
      <select id="structureSelect">
        <option value="">（不指定建筑）</option>
        ${Object.entries((S.status && S.status.config && S.status.config.structure_groups) || {1657:'堡垒',1404:'工程复合体',1406:'精炼厂'})
          .map(([gid, gname]) => {
            const items = structures.filter(s => String(s.group_id) === String(gid));
            if (!items.length) return '';
            return `<optgroup label="${esc(gname)}">` + items.map(s =>
              `<option value="${s.type_id}" ${Number(f.structure_type_id) === Number(s.type_id) ? 'selected' : ''}
                >${esc(s.name)}（${esc(s.rig_size_label || '?')}）　${structureBonusText(s)}</option>`).join('')
              + `</optgroup>`;
          }).join('')}
      </select>
      ${current ? `<div class="hint-tiny">建筑自身加成：${structureBonusText(current)}
        · 可装 ${esc(current.rig_size_label || '?')}改装件 ${current.rig_slots || 3} 个
        · 校准值 ${Math.round(Number(current.upgrade_capacity || 0))}</div>`
        : '<div class="hint-tiny">不指定建筑 = 按 NPC 空间站算（只有技能减免，无改装件）</div>'}
    </div>
    <div class="field"><label>安等倍率（改装件加成系数）</label>
      <select id="securitySelect">
        <option value="" ${!sec ? 'selected' : ''}>自动（按生产星系）</option>
        <option value="highsec" ${sec === 'highsec' ? 'selected' : ''}>高安 ×1.0</option>
        <option value="lowsec" ${sec === 'lowsec' ? 'selected' : ''}>低安 ×1.9</option>
        <option value="nullsec" ${sec === 'nullsec' ? 'selected' : ''}>00 / 虫洞 ×2.1</option>
      </select>
    </div>
    <div class="field" style="grid-column: span 2"><label>改装件（最多 3 个，且必须与建筑尺寸一致）</label>
      <div class="row" style="gap:6px; margin-bottom:6px">
        <select id="rigSelect" style="flex:1">
          <option value="">＋ 添加改装件…${rigSize
            ? `（${esc(current.rig_size_label)}，${rigsForSize.length} 个可选）`
            : `（先选建筑即可按尺寸筛选，共 ${rigsForSize.length} 个）`}</option>
          ${rigsForSize.map(g => `<option value="${g.type_id}">${esc(g.group_name || '')} · ${esc(g.name)}
            　${g.material_percent ? '材料' + g.material_percent + '%' : ''} ${g.time_percent ? '时间' + g.time_percent + '%' : ''} ${g.cost_percent ? '费用' + g.cost_percent + '%' : ''}</option>`).join('')}
        </select>
        <button class="btn btn-ghost btn-xs" data-action="facility-save" title="把当前建筑 / 改装件 / 星系 / 计费参数存进 config.json">保存设施</button>
      </div>
      ${(rigSize && !rigsForSize.length) ? `<div class="hint-tiny" style="color:var(--amber)">
        改装件列表还是空的（候选数据没载入成功），这个下拉现在选了也没用 ——
        <button class="btn btn-xs" data-action="rigs-reload">重新载入改装件数据</button></div>` : ''}
      <div class="rigs">${(f.rig_type_ids || []).map(id => {
        const g = rigs.find(x => Number(x.type_id) === Number(id));
        const mismatched = rigSize && g && Number(g.rig_size) !== rigSize;
        return `<span class="rig-chip${mismatched ? ' rig-chip-warn' : ''}" title="${mismatched
          ? '尺寸是 ' + esc(g.rig_size_label || '?') + '，与所选建筑的 ' + esc(current.rig_size_label) + ' 不匹配'
          : ''}">${esc(g ? g.name : id)}${mismatched ? ' ⚠' : ''}
          <button data-action="rig-remove" data-id="${id}" title="移除">×</button></span>`;
      }).join('') || '<span class="hint-tiny">未安装改装件</span>'}</div>
      ${savedFacilityHint()}
    </div>
    ${fuelSectionHtml()}
    <div class="field"><label>任务计费参数</label>
      <div class="row" style="gap:6px">
        <span class="hint-tiny">SCC 附加费</span>
        <input type="number" step="0.001" min="0" max="0.2" data-industry="scc_surcharge"
               value="${S.industry.scc_surcharge}" style="width:76px">
        <span class="hint-tiny">设施税率</span>
        <input type="number" step="0.0025" min="0" max="0.1" data-industry="job_tax_rate"
               value="${S.industry.job_tax_rate}" style="width:76px">
      </div>
      <div class="hint-tiny">NPC 空间站固定 0.25%，玩家建筑上限 10%</div>
    </div>
    <div class="field"><label>手动覆盖减免（留 0 表示用自动值）</label>
      <div class="row" style="gap:6px">
        <input type="number" step="0.01" min="0" max="0.9" data-industry="structure_material_bonus"
               value="${S.industry.structure_material_bonus}" title="材料减免" style="width:70px">
        <input type="number" step="0.01" min="0" max="0.9" data-industry="structure_time_bonus"
               value="${S.industry.structure_time_bonus}" title="时间减免" style="width:70px">
        <input type="number" step="0.01" min="0" max="0.9" data-industry="structure_cost_bonus"
               value="${S.industry.structure_cost_bonus}" title="费用减免" style="width:70px">
      </div>
      <div class="hint-tiny">左=材料 中=时间 右=费用（0.2 表示减免 20%）</div>
    </div>
  </div>
  ${S.bonusPreview ? `<div class="hint" style="margin-top:8px">
    当前设施倍率：材料 ×${Number(S.bonusPreview.material_factor).toFixed(4)}（减免 ${fmtPct(S.bonusPreview.material)}）·
    时间 ×${Number(S.bonusPreview.time_factor).toFixed(4)}（减免 ${fmtPct(S.bonusPreview.time)}）·
    费用 ×${Number(S.bonusPreview.cost_factor).toFixed(4)}（减免 ${fmtPct(S.bonusPreview.cost)}）
    ${(S.bonusPreview.notes || []).map(n => '<br>· ' + esc(n)).join('')}
  </div>` : ''}`;
}

function viewProductCard(p, i) {
  const badge = p.purchase_mode === 'purchase'
    ? '<span class="badge amber">收购</span>' : '<span class="badge green">自造</span>';
  return `
  <div class="product-card" data-card-index="${i}">
    <div class="pc-head">
      ${typeIconHtml(p.type_id, 64, '')}
      <div class="pc-name">${esc(p.name)}<small>${esc(p.activity_label || '制造')} ${badge}</small></div>
      <button class="btn btn-xs btn-ghost" data-action="plan-remove" data-index="${i}" title="移除">×</button>
    </div>
    <div class="pc-inputs">
      <div><label>ME %</label><input type="number" min="0" max="10" step="1"
        data-plan-field="me" data-index="${i}" value="${p.me}"></div>
      <div><label>TE %</label><input type="number" min="0" max="20" step="2"
        data-plan-field="te" data-index="${i}" value="${p.te}"></div>
      <div><label>流程数</label><input type="number" min="1" step="1"
        data-plan-field="runs" data-index="${i}" value="${p.runs}"></div>
    </div>
    <div class="row" style="margin-top:7px; gap:6px">
      <button class="btn btn-xs" data-action="toggle-mode" data-index="${i}">
        ${p.purchase_mode === 'purchase' ? '改为自造' : '改为收购'}</button>
      <button class="btn btn-xs btn-ghost" data-action="product-detail" data-index="${i}">明细</button>
      <button class="btn btn-xs btn-ghost" data-action="product-blueprint" data-index="${i}">配方</button>
    </div>
  </div>`;
}

let tableSort = { key: 'cost_total', dir: -1 };
function sortRows(rows) {
  const k = tableSort.key, d = tableSort.dir;
  return rows.slice().sort((a, b) => {
    const x = a[k], y = b[k];
    if (typeof x === 'string' || typeof y === 'string') {
      return String(x).localeCompare(String(y), 'zh') * d;
    }
    return ((Number(x) || 0) - (Number(y) || 0)) * d;
  });
}

function viewResultTables(r) {
  const tab = S.resultTab || 'materials';
  const tabs = [['materials', '材料明细'], ['products', '产品明细'], ['breakdown', '费用与时间']];
  return `
  <div class="card" style="margin-top:12px">
    <div class="tabs">
      ${tabs.map(([k, l]) => `<button data-result-tab="${k}" class="${tab === k ? 'active' : ''}">${l}</button>`).join('')}
      <span class="spacer"></span>
      ${tab === 'materials' ? `
        <button class="btn btn-xs" data-action="build-all">全部自造</button>
        <button class="btn btn-xs" data-action="purchase-all">全部收购</button>
        <button class="btn btn-xs btn-green" data-action="market-all"
          title="对全部原料查询吉他卖单（首次较慢，之后 5 分钟内走缓存）">刷新材料盘口</button>
        <span class="hint-tiny">「来源」列可逐行切换：自造 = 用子材料自己生产</span>` : ''}
    </div>
    ${tab === 'materials' ? viewMaterialsTable(r) : ''}
    ${tab === 'products' ? viewProductsTable(r) : ''}
    ${tab === 'breakdown' ? viewBreakdown(r) : ''}
  </div>`;
}

function viewMaterialsTable(r) {
  const rows = (r.rows || []).filter(x => !x.error);
  if (!rows.length) return '<div class="empty">这个计划没有需要采购/自造的原料（可能全部选择了「收购」成品）。</div>';
  const sorted = sortRows(rows);
  const t = r.totals || {};
  const th = (key, label, cls2) =>
    `<th class="${cls2 || ''} ${tableSort.key === key ? 'sorted' : ''}" data-sort="${key}">${label}${tableSort.key === key ? (tableSort.dir < 0 ? ' ▼' : ' ▲') : ''}</th>`;
  return `
  <div class="table-wrap" style="max-height:62vh">
  <table class="grid">
    <thead><tr>
      ${th('name', '原料名称', 'name')}
      ${th('category_name', '原料类别')}
      <th class="center">来源</th>
      ${th('location_name', '生产地点')}
      ${th('quantity', '平均消耗数量')}
      ${th('inventory', '可用库存数量')}
      ${th('unit_cost', '单件成本')}
      ${th('unit_price', '单价')}
      ${th('total_price', '总价')}
      ${th('total_profit', '总利润')}
      ${th('margin', '利润率')}
      ${th('total_volume', '体积(m³)')}
    </tr></thead>
    <tbody>
    ${sorted.map(row => `
      <tr>
        <td class="name"><div class="item-cell">
          ${typeIconHtml(row.type_id, 32, 'item-icon')}
          <div class="nm">${esc(row.name)}
            <small>${row.can_build ? '可自造' : '只能采购'}${(row.used_by || []).length ? ' · 用于 ' + esc(row.used_by.slice(0, 2).join('、')) + ((row.used_by.length > 2) ? ' 等' : '') : ''}</small>
          </div></div></td>
        <td>${esc(row.category_name || row.group_name || '-')}</td>
        <td class="center">
          <button class="btn btn-xs ${row.source === 'build' ? 'btn-green' : 'btn-ghost'}"
                  data-action="toggle-material" data-type-id="${row.type_id}"
                  ${row.can_build ? '' : 'disabled'}>
            ${row.source === 'build' ? '自造' : '收购'}</button></td>
        <td>${esc(row.location_name || '-')}</td>
        <td class="num">${fmtNum(row.quantity)}</td>
        <td class="num ${row.shortfall > 0 ? 'neg' : 'dim'}">${fmtNum(row.inventory)}
          ${row.shortfall > 0 ? `<small>缺 ${fmtNum(row.shortfall)}</small>` : ''}</td>
        <td class="num">${fmtISK(row.unit_cost)}</td>
        <td class="num">${fmtISK(row.unit_price)}</td>
        <td class="num">${fmtISK(row.total_price)}</td>
        <td class="num ${pctClass(row.total_profit)}">${fmtISK(row.total_profit)}</td>
        <td class="num ${pctClass(row.margin)}">${fmtPct(row.margin)}</td>
        <td class="num">${fmtNum(row.total_volume, 1)}</td>
      </tr>`).join('')}
    </tbody>
    <tfoot><tr>
      <td class="name">合计 ${rows.length} 种原料</td>
      <td colspan="3"></td>
      <td class="num">${fmtNum(rows.reduce((s, x) => s + (x.quantity || 0), 0))}</td>
      <td></td>
      <td></td><td></td>
      <td class="num">${fmtISK(t.market_value)}</td>
      <td class="num ${pctClass(t.profit)}">${fmtISK(t.profit)}</td>
      <td class="num ${pctClass(t.margin)}">${fmtPct(t.margin)}</td>
      <td class="num">${fmtNum(t.volume, 1)}</td>
    </tr></tfoot>
  </table></div>
  <div class="hint" style="margin-top:8px">
    材料成本合计 <b class="mono">${fmtISK(t.material_cost)}</b> ISK ·
    「自造」<b>${t.build_items || 0}</b> 项 / 「收购」<b>${t.purchase_items || 0}</b> 项。
    单件成本 = 自造时的（子材料 + 任务费用）单价；收购时等于市场单价，因此利润为 0。
    ${S.price.source === 'adjusted' ? '当前价源为「调整价」。' : ''}
  </div>`;
}

function viewProductsTable(r) {
  const rows = (r.products || []).filter(x => !x.error);
  const t = r.product_totals || {};
  if (!rows.length) return '<div class="empty">没有产品。</div>';
  return `
  <div class="table-wrap" style="max-height:62vh">
  <table class="grid">
    <thead><tr>
      <th class="name">产品名称</th><th>活动</th><th>ME</th><th>TE</th><th>流程数</th>
      <th>产出数量</th><th>单件成本</th><th>单价</th><th>总成本</th><th>总价</th>
      <th>总利润</th><th>利润率</th><th>体积(m³)</th><th>耗时</th>
    </tr></thead>
    <tbody>
    ${rows.map(row => `
      <tr>
        <td class="name"><div class="item-cell">
          ${typeIconHtml(row.type_id, 32, 'item-icon')}
          <div class="nm">${esc(row.name)}<small>${row.purchase_mode === 'purchase' ? '直接收购' : esc(row.price_source || '')}</small></div>
        </div></td>
        <td>${esc(row.activity_name || '')}</td>
        <td class="num">${row.me}</td><td class="num">${row.te}</td>
        <td class="num">${fmtNum(row.runs)}</td>
        <td class="num">${fmtNum(row.quantity)}</td>
        <td class="num">${fmtISK(row.unit_cost)}</td>
        <td class="num">${fmtISK(row.unit_price)}</td>
        <td class="num">${fmtISK(row.total_cost)}</td>
        <td class="num">${fmtISK(row.total_price)}</td>
        <td class="num ${pctClass(row.total_profit)}">${fmtISK(row.total_profit)}</td>
        <td class="num ${pctClass(row.margin)}">${fmtPct(row.margin)}</td>
        <td class="num">${fmtNum(row.total_volume, 1)}</td>
        <td>${esc(row.time_text || '-')}</td>
      </tr>`).join('')}
    </tbody>
    <tfoot><tr>
      <td class="name">合计 ${rows.length} 个产品</td><td colspan="7"></td>
      <td class="num">${fmtISK(t.total_cost)}</td>
      <td class="num">${fmtISK(t.total_price)}</td>
      <td class="num ${pctClass(t.total_profit)}">${fmtISK(t.total_profit)}</td>
      <td class="num ${pctClass(t.margin)}">${fmtPct(t.margin)}</td>
      <td class="num">${fmtNum(t.total_volume, 1)}</td><td></td>
    </tr></tfoot>
  </table></div>`;
}

function viewBreakdown(r) {
  const rows = (r.products || []).filter(x => !x.error);
  if (!rows.length) return '<div class="empty">没有产品。</div>';
  return rows.map(row => {
    const jb = row.job_cost_breakdown || {};
    const fb = row.facility_bonuses || {};
    return `
    <details class="block" ${rows.length === 1 ? 'open' : ''}>
      <summary>${esc(row.name)} · ${fmtNum(row.runs)} 流程 · ME${row.me}/TE${row.te} · 耗时 ${esc(row.time_text)}</summary>
      <div class="kv">
        <dt>估算物品价值 EIV</dt><dd>${fmtISK(row.estimated_item_value)} ISK <span class="dim">（按 ME0 基准材料 × 调整价，与 ME/建筑无关）</span></dd>
        <dt>计费基数</dt><dd>${fmtISK(jb.job_cost_base)} ISK <span class="dim">（本活动系数 ${Number(jb.job_cost_base_factor || 0).toFixed(4)}）</span></dd>
        <dt>系统成本指数</dt><dd>${Number(jb.cost_index || 0).toFixed(6)} → 系统费用 ${fmtISK(jb.system_cost)} ISK</dd>
        <dt>设施费用减免</dt><dd class="pos">${fmtISK(jb.facility_reduction)} ISK <span class="dim">（费用倍率 ×${Number(fb.cost_factor || 1).toFixed(4)}）</span></dd>
        <dt>SCC 附加费</dt><dd>${fmtISK(jb.scc_surcharge)} ISK</dd>
        <dt>设施税</dt><dd>${fmtISK(jb.tax)} ISK</dd>
        <dt>任务费用合计</dt><dd><b>${fmtISK(jb.total)} ISK</b></dd>
        <dt>材料成本</dt><dd>${fmtISK(row.material_cost)} ISK</dd>
        <dt>总成本 / 单件</dt><dd><b>${fmtISK(row.total_cost)}</b> ISK / ${fmtISK(row.unit_cost)} ISK</dd>
        <dt>基础时间 / 实际耗时</dt><dd>${fmtDur(row.base_time)} → ${esc(row.time_text)} <span class="dim">（时间倍率 ×${Number(fb.time_factor || 1).toFixed(4)}）</span></dd>
        <dt>设施加成来源</dt><dd>${(fb.sources || []).map(s =>
          `${esc(s.name)}${s.role === 'rig' ? `（安等 ${s.security_label} ×${s.security_multiplier}）` : ''}`).join('、') || '无'}</dd>
      </div>
    </details>`;
  }).join('') + `
  <div class="hint" style="margin-top:10px">
    公式与数据来源见「关于」页：材料数量为乘完流程数后向上取整；技能时间减免为相乘；
    SCC 附加费现行 4%；EIV 使用调整价且不受 ME/建筑影响。现有任务的真实费用以 ESI 的 cost 字段为准。
  </div>`;
}

/* ==================================================================
 * 视图：工业项目
 * ================================================================== */
function viewJobs() {
  const j = S.jobs;
  if (!S.status || !(S.status.characters || []).length) {
    return `<div class="card"><div class="card-title"><strong>还没有登录角色</strong></div>
      <div class="hint">工业项目需要你自己角色的授权才能读取。军团项目还需要「总监」或「工厂经理」职位。</div>
      <div class="row" style="margin-top:12px"><button class="btn btn-primary" data-action="login">登录 EVE 角色</button>
      <button class="btn btn-ghost" data-action="goto-settings">去设置 client_id</button></div></div>`;
  }
  if (!j) {
    return `<div class="card"><div class="hint">正在读取本地缓存…</div></div>
      <div class="card" style="margin-top:12px"><button class="btn btn-primary" data-action="jobs-refresh">从 ESI 刷新工业项目</button>
      <span class="hint" style="margin-left:10px">角色工业项目 + 军团工业项目会一起刷新</span></div>`;
  }
  const jobs = j.jobs || [];
  const tl = j.timeline || { jobs: [] };
  const byStatus = j.summary || {};
  return `
  ${corpScopeNotice()}
  <div class="row" style="margin-bottom:12px">
    <button class="btn btn-primary" data-action="jobs-refresh">从 ESI 刷新</button>
    <select id="jobStatusFilter" style="width:150px">
      ${['all', 'active', 'ready', 'paused']
        .map(s => `<option value="${s}">${s === 'all' ? '全部状态' : s}</option>`).join('')}
    </select>
    <span class="hint-tiny">已交付（delivered）的项目会自动清除；
      <b>ready</b> 会保留 —— 那是做完但还没领取的，需要你去收</span>
    <span class="spacer"></span>
    ${Object.entries(byStatus).map(([k, v]) => `<span class="badge">${esc(k)} ${v}</span>`).join('')}
    <span class="badge blue">共 ${jobs.length} 个</span>
  </div>

  <div class="card"><div class="card-title"><strong>进行中任务时间线</strong>
    <span class="hint-tiny">同层并行、跨层串行的估算不适用于现有任务；这里按结束时间排序</span></div>
    ${timelineHtml(tl.jobs || [])}</div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>工业项目列表</strong><span class="hint-tiny">${esc(j.scope || '')}</span></div>
    ${jobs.length ? `
    <div class="table-wrap" style="max-height:60vh">
    <table class="grid">
      <thead><tr><th class="name">产品</th><th class="name">蓝图</th><th>活动</th><th>状态</th>
        <th>流程数</th><th>概率</th><th>地点</th><th>开始</th><th>结束</th>
        <th>剩余</th><th>费用(ESI)</th></tr></thead>
      <tbody>${jobs.map(row => `
        <tr>
          <td class="name"><div class="item-cell">
            ${typeIconHtml(row.product_type_id || row.blueprint_type_id, 32, 'item-icon')}
            <div class="nm">${esc(row.product_name || row.blueprint_name || ('类型 ' + (row.product_type_id || row.blueprint_type_id || '?')))}</div></div></td>
          <td class="name">${esc(row.blueprint_name || '-')}</td>
          <td>${esc(row.activity_name || '')}</td>
          <td><span class="badge ${row.status === 'active' ? 'green' : (row.status === 'ready' ? 'blue' : '')}">${esc(row.status || '')}</span></td>
          <td class="num">${fmtNum(row.runs)}</td>
          <td class="num">${row.probability ? fmtPct(row.probability, 1) : '-'}</td>
          <td${row.facility_resolved === false
                ? ` title="读不到这个建筑的名称：ESI 要求角色对该建筑有停靠权限，否则返回 403。&#10;若它是军团建筑，可到「设置 → 燃料消耗」点「读取军团建筑」把名字缓存下来。"` : ''}>${esc(row.facility_name || '-')}</td>
          <td class="dim">${esc((row.start_date || '').replace('T', ' ').slice(0, 16))}</td>
          <td class="dim">${esc((row.end_date || '').replace('T', ' ').slice(0, 16))}</td>
          <td class="num ${row.remaining_seconds > 0 ? '' : 'dim'}">${esc(row.remaining_text || '-')}</td>
          <td class="num">${row.cost ? fmtISK(row.cost) : '-'}</td>
        </tr>`).join('')}</tbody>
    </table></div>` : '<div class="empty">没有工业项目记录。点上方「从 ESI 刷新」。</div>'}
  </div>`;
}

function timelineHtml(jobs) {
  if (!jobs.length) return '<div class="empty" style="padding:18px">没有进行中的任务。</div>';
  const starts = jobs.map(j => j.start_ts).filter(Boolean);
  const ends = jobs.map(j => j.end_ts).filter(Boolean);
  const t0 = Math.min.apply(null, starts.concat([Date.now() / 1000]));
  const t1 = Math.max.apply(null, ends.concat([t0 + 3600]));
  const span = Math.max(1, t1 - t0);
  const now = Date.now() / 1000;
  return `<div class="timeline">${jobs.slice(0, 40).map(j => {
    const left = ((j.start_ts || t0) - t0) / span * 100;
    const width = Math.max(0.6, ((j.end_ts || t1) - (j.start_ts || t0)) / span * 100);
    const progress = Math.max(0, Math.min(1, (now - (j.start_ts || t0)) / ((j.end_ts || t1) - (j.start_ts || t0) || 1)));
    return `<div class="tl-row">
      <div class="nm">${esc(j.product_name || j.blueprint_name || j.job_id)}</div>
      <div class="tl-track">
        <div class="tl-fill" style="left:${left}%;width:${width}%;opacity:.45"></div>
        <div class="tl-fill" style="left:${left}%;width:${width * progress}%"></div>
      </div>
      <div class="dim" style="text-align:right">${esc(j.remaining_text || '-')}</div>
    </div>`;
  }).join('')}</div>`;
}

/* ==================================================================
 * 视图：利润排行
 * ================================================================== */
function viewRanking() {
  const r = S.ranking;
  return `
  <div class="card">
    <div class="card-title"><strong>利润排行</strong>
      <span class="hint-tiny">成本按自造计算（ME/TE 可调），价格按当前价源；材料默认用调整价以免逐件请求盘口</span></div>
    <div class="facility">
      <div class="field"><label>排序</label>
        <select id="rankSort">
          ${[['margin', '利润率'], ['profit', '单件利润'], ['isk_per_hour', '每小时收益'],
             ['volume', '市场挂单量'], ['price', '单价']]
            .map(([k, l]) => `<option value="${k}" ${(r && r.sort) === k ? 'selected' : ''}>${l}</option>`).join('')}
        </select></div>
      <div class="field"><label>物品分类</label>
        <select id="rankCategory"><option value="">全部分类</option>
          ${(S.categories || []).filter(c => c.types > 0).map(c =>
            `<option value="${c.category_id}">${esc(c.name)}</option>`).join('')}</select></div>
      <div class="field"><label>ME / TE</label>
        <div class="row" style="gap:6px">
          <input id="rankMe" type="number" value="10" min="0" max="10" style="width:70px">
          <input id="rankTe" type="number" value="20" min="0" max="20" style="width:70px">
        </div></div>
      <div class="field"><label>刷新盘口（更准但更慢）</label>
        <label class="row" style="gap:6px; font-size:12px">
          <input type="checkbox" id="rankRefresh" style="width:auto"> 拉取候选产品的吉他卖单</label></div>
      <div class="field" style="display:flex; align-items:flex-end">
        <button class="btn btn-primary btn-block" data-action="rank-run">开始扫描</button></div>
    </div>
    ${r ? `<div class="hint" style="margin-top:10px">扫描了 ${r.scanned} 个候选，命中 ${r.matched} 个。${esc(r.price_note || '')}</div>` : ''}
  </div>
  ${r && (r.rows || []).length ? `
  <div class="card" style="margin-top:12px">
    <div class="table-wrap" style="max-height:66vh">
    <table class="grid">
      <thead><tr><th class="name">产品</th><th>单件成本</th><th>单价</th><th>价源</th>
        <th>单件利润</th><th>利润率</th><th>每小时收益</th><th>耗时</th><th>挂单量</th><th>体积(m³)</th><th class="center">操作</th></tr></thead>
      <tbody>${r.rows.map(row => `
        <tr>
          <td class="name"><div class="item-cell">
            ${typeIconHtml(row.type_id, 32, 'item-icon')}
            <div class="nm">${esc(row.name)}</div></div></td>
          <td class="num">${fmtISK(row.unit_cost)}</td>
          <td class="num">${fmtISK(row.unit_price)}</td>
          <td class="dim">${esc(row.price_source || '')}</td>
          <td class="num ${pctClass(row.profit)}">${fmtISK(row.profit)}</td>
          <td class="num ${pctClass(row.margin)}">${fmtPct(row.margin)}</td>
          <td class="num">${fmtCompact(row.isk_per_hour)}</td>
          <td>${esc(row.time_text || '-')}</td>
          <td class="num">${fmtNum(row.daily_volume)}</td>
          <td class="num">${fmtNum(row.volume, 2)}</td>
          <td class="center"><button class="btn btn-xs" data-action="rank-add"
            data-type-id="${row.type_id}" data-blueprint="${row.blueprint_type_id}"
            data-name="${esc(row.name)}">加入计划</button></td>
        </tr>`).join('')}</tbody>
    </table></div>
  </div>` : (r ? '<div class="card" style="margin-top:12px"><div class="empty">没有命中任何产品，试试放宽条件或先刷新盘口。</div></div>' : '')}`;
}

/* ==================================================================
 * 视图：库存
 * ================================================================== */
function viewInventory() {
  const inv = S.inventory_rows;
  const chars = (S.status && S.status.characters) || [];
  if (!chars.length) {
    return `<div class="card"><div class="hint">库存需要角色授权。先登录一个角色。</div>
      <div class="row" style="margin-top:12px"><button class="btn btn-primary" data-action="login">登录 EVE 角色</button></div></div>`;
  }
  const active = chars.find(c => c.is_active) || chars[0];
  const scope = S.inventory.scope || ('char:' + active.character_id);
  return `
  ${scope.startsWith('corp:') ? corpScopeNotice() : ''}
  <div class="card">
    <div class="card-title"><strong>库存</strong><span class="hint-tiny">数据来自 ESI assets；本地按 type_id 汇总</span></div>
    <div class="facility">
      <div class="field"><label>查看范围</label>
        <select id="invScope">
          <option value="char:${active.character_id}" ${scope === 'char:' + active.character_id ? 'selected' : ''}>角色：${esc(active.name)}</option>
          ${active.corporation_id ? `<option value="corp:${active.corporation_id}" ${scope === 'corp:' + active.corporation_id ? 'selected' : ''}>军团：${esc(active.corporation_name || active.corporation_id)}</option>` : ''}
        </select></div>
      <div class="field"><label>搜索</label><input id="invSearch" type="text" placeholder="物品名称关键词"></div>
      <div class="field" style="display:flex; align-items:flex-end; gap:6px">
        <button class="btn btn-primary" data-action="inv-load">读取库存</button>
        <button class="btn" data-action="inv-refresh">从 ESI 刷新</button>
      </div>
    </div>
    ${inv ? `<div class="hint" style="margin-top:8px">${(inv.rows || []).length} 种物品 · 总体积 ${fmtNum(inv.total_volume, 1)} m³
       ${inv.meta && inv.meta.fetched_at ? ' · 本地缓存于 ' + fmtTime(inv.meta.fetched_at) : ''}</div>` : ''}
  </div>
  ${inv && (inv.rows || []).length ? `
  <div class="card" style="margin-top:12px">
    <div class="table-wrap" style="max-height:66vh">
    <table class="grid">
      <thead><tr><th class="name">物品</th><th>数量</th><th>堆叠数</th><th>单件体积</th><th>总体积(m³)</th><th class="name">位置</th></tr></thead>
      <tbody>${inv.rows.map(row => `
        <tr>
          <td class="name"><div class="item-cell">
            ${typeIconHtml(row.type_id, 32, 'item-icon')}
            <div class="nm">${esc(row.name || ('类型 ' + row.type_id))}</div></div></td>
          <td class="num">${fmtNum(row.quantity)}</td>
          <td class="num">${fmtNum(row.stacks)}</td>
          <td class="num">${fmtNum(row.volume, 2)}</td>
          <td class="num">${fmtNum(row.total_volume, 1)}</td>
          <td class="name dim">${esc(row.location_name || row.location_id || '-')}</td>
        </tr>`).join('')}</tbody>
    </table></div>
  </div>` : ''}`;
}

/* ==================================================================
 * 视图：研究模拟（ME / TE 研究在各建筑里需要多久）
 * ================================================================== */
function viewResearch() {
  const R = S.research;
  const info = R.info && R.info.blueprint;
  const res = R.result;
  const rigs = (R.rigCandidates || []);

  const bpCard = !R.target ? `
    <div class="card">
      <div class="card-title"><strong>选择要研究的蓝图</strong>
        <span class="hint-tiny">也可以从下方「我的蓝图」直接点进来</span></div>
      <div class="search-row">
        <input id="researchSearch" type="search" placeholder="输入蓝图或产物名称，例如 裂谷级 / 索迪约">
        <button class="btn" data-action="research-search">搜索</button>
      </div>
      <div id="researchResults" class="list-scroll" style="max-height:280px; margin-top:10px"></div>
    </div>` : `
    <div class="card">
      <div class="card-title"><strong>${esc(info ? info.name : R.target.name)}</strong>
        <span class="row" style="gap:6px">
          <button class="btn btn-xs btn-ghost" data-action="research-reset">换一个蓝图</button>
        </span></div>
      ${info ? `
      <div class="kv">
        <dt>产物</dt><dd>${esc(info.product_name || '-')}</dd>
        <dt>研发等级 rank</dt><dd>${info.rank === null || info.rank === undefined ? '（SDE 未提供）' : info.rank}
          ${info.rank_consistent === false ? '<span class="badge amber" title="该蓝图的官方 rank × 105 与 SDE 研究时长不一致，说明 CCP 直接指定了研究时长。本程序一律以 SDE 时长为准。">与 SDE 时长不一致，以 SDE 为准</span>' : ''}</dd>
        <dt>第 1 级研究时长</dt><dd>${esc(fmtDur(info.base_seconds))} <span class="dim">（来自官方 SDE，ME 与 TE 共用同一个值）</span></dd>
        <dt>等级模型</dt><dd>ME / TE 各 <b>${info.max_level}</b> 级 ·
          ME 每级 −${info.me_percent_per_level}%（显示 0–${info.max_level * info.me_percent_per_level}）·
          TE 每级 −${info.te_percent_per_level}%（显示 0–${info.max_level * info.te_percent_per_level}）</dd>
        <dt>满级累计（NPC 站、无技能）</dt><dd>${esc(fmtDur(info.base_seconds && cumulativeOf(info, info.max_level)))}</dd>
      </div>` : '<div class="hint">正在读取蓝图数据…</div>'}
    </div>`;

  return `
  ${bpCard}

  ${R.target && info ? `
  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>研究范围与设施</strong>
      <span class="hint-tiny">等级表是「累计秒数」，逐级耗时 = 本级累计 − 上一级累计</span></div>
    <div class="facility">
      <div class="field"><label>ME 研究（材料效率）</label>
        <div class="hint-tiny">下面两个数字是<b>自定义区间</b>：从第几级研究到第几级。
          只想研究 ME 就把 TE 的「从」「到」填成同一个数字。</div>
        <div class="row" style="gap:6px">
          <span class="hint-tiny">从</span>
          <input id="rsFromMe" type="number" min="0" max="${info.max_level}" value="${R.fromMe}" style="width:64px">
          <span class="hint-tiny">到</span>
          <input id="rsToMe" type="number" min="0" max="${info.max_level}" value="${R.toMe}" style="width:64px">
          <span class="hint-tiny">级 → 材料效率 ${R.fromMe * info.me_percent_per_level}% → ${R.toMe * info.me_percent_per_level}%</span>
        </div></div>
      <div class="field"><label>TE 研究（时间效率）</label>
        <div class="row" style="gap:6px">
          <span class="hint-tiny">从</span>
          <input id="rsFromTe" type="number" min="0" max="${info.max_level}" value="${R.fromTe}" style="width:64px">
          <span class="hint-tiny">到</span>
          <input id="rsToTe" type="number" min="0" max="${info.max_level}" value="${R.toTe}" style="width:64px">
          <span class="hint-tiny">级 → 时间效率 ${R.fromTe * info.te_percent_per_level}% → ${R.toTe * info.te_percent_per_level}%</span>
        </div></div>
      <div class="field"><label>安等倍率（改装件加成系数）</label>
        <select id="rsSecurity">
          <option value="" ${!R.security ? 'selected' : ''}>自动（按生产星系）</option>
          <option value="highsec" ${R.security === 'highsec' ? 'selected' : ''}>高安 ×1.0</option>
          <option value="lowsec" ${R.security === 'lowsec' ? 'selected' : ''}>低安 ×1.9</option>
          <option value="nullsec" ${R.security === 'nullsec' ? 'selected' : ''}>00 / 虫洞 ×2.1</option>
        </select></div>
      <div class="field" style="grid-column: span 2"><label>叠加改装件（可选，会额外多出一档「建筑 + 改装件」对比）</label>
        <div class="row" style="gap:6px; margin-bottom:6px">
          <select id="rsRigSelect" style="flex:1">
            <option value="">＋ 添加实验室/研究类改装件…</option>
            ${rigs.filter(g => g.time_percent || g.cost_percent || g.material_percent).map(g =>
              `<option value="${g.type_id}">${esc(g.name)}　时间${g.time_percent}% 费用${g.cost_percent}%</option>`).join('')}
          </select>
        </div>
        <div class="rigs">${(R.rigTypeIds || []).map(id => {
          const g = rigs.find(x => Number(x.type_id) === Number(id));
          return `<span class="rig-chip">${esc(g ? g.name : id)}
            <button data-action="rs-rig-remove" data-id="${id}" title="移除">×</button></span>`;
        }).join('') || '<span class="hint-tiny">未叠加改装件</span>'}</div>
      </div>
      <div class="field" style="display:flex; align-items:flex-end; gap:6px">
        <button class="btn btn-primary btn-block" data-action="research-run">开始模拟</button>
      </div>
    </div>
    <div class="hint" style="margin-top:8px">
      技能减免自动读取你角色的「冶金学 / 研究概论 / 高级工业理论」等级；未登录时按满级 5 计算。
      建筑与改装件加成按官方 SDE 属性读取，改装件减免会按安等放大。
    </div>
  </div>` : ''}

  ${res && res.ok ? viewResearchResult(res) : ''}

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>我的蓝图</strong>
      <span class="hint-tiny">读取你角色（或军团）持有的蓝图，算出「研究到满级」还要多久</span></div>
    <div class="row" style="gap:8px; margin-bottom:8px">
      <button class="btn" data-action="research-owned">读取我的蓝图</button>
      ${R.owned ? `<span class="hint-tiny">${R.owned.count} 张可研究蓝图 · 按耗时从多到少排序</span>` : ''}
    </div>
    ${R.owned && R.owned.blueprints && R.owned.blueprints.length ? `
    <div class="table-wrap" style="max-height:46vh">
    <table class="grid">
      <thead><tr><th class="name">蓝图</th><th>rank</th><th>当前 ME / TE</th>
        <th>ME 到满级</th><th>TE 到满级</th><th class="name">最快设施</th><th class="center">操作</th></tr></thead>
      <tbody>${R.owned.blueprints.slice(0, 300).map((b, i) => `
        <tr>
          <td class="name"><div class="item-cell">
            ${typeIconHtml(b.product_type_id || b.blueprint_type_id, 32, 'item-icon')}
            <div class="nm">${esc(b.name || '')}<small>${esc(b.product_name || '')}</small></div></div></td>
          <td class="num dim">${b.rank === null || b.rank === undefined ? '-' : b.rank}</td>
          <td class="num">${b.me_percent}% / ${b.te_percent}%
            <small class="dim">(级 ${b.me_level}/${b.te_level}${b.at_me_max ? ' · ME满' : ''}${b.at_te_max ? ' · TE满' : ''})</small></td>
          <td class="num ${b.at_me_max ? 'dim' : ''}">${b.at_me_max ? '已满' : esc(b.to_max_me_text)}</td>
          <td class="num ${b.at_te_max ? 'dim' : ''}">${b.at_te_max ? '已满' : esc(b.to_max_te_text)}</td>
          <td class="name dim">${esc(b.best_facility || '-')}</td>
          <td class="center"><button class="btn btn-xs" data-action="research-load-owned"
            data-bp="${b.blueprint_type_id}" data-name="${esc(b.name || '')}"
            data-me="${b.me_level}" data-te="${b.te_level}">载入模拟</button></td>
        </tr>`).join('')}</tbody>
    </table></div>` : (R.owned ? '<div class="empty">没有可研究的蓝图（或本地还没有蓝图数据，请先到「工业项目」刷新）。</div>' : '')}
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>关于这个模拟的依据</strong></div>
    <div class="kv">
      <dt>等级表</dt><dd>105 / 250 / 595 / 1414 / 3360 / 8000 / 19000 / 45255 / 107700 / 256000 秒
        —— <b>达到第 n 级的累计值</b>（rank = 1 时）</dd>
      <dt>为什么是累计值</dt><dd>研究任务费用公式同时给出「从 n−1 到 n 用两个累计值之差」与「从 0 到 n 用累计值」两种写法</dd>
      <dt>怎么验证的</dt><dd>官方 SDE 里全部 4344 个蓝图的 ME 与 TE 研究时长<b>完全相等</b>（说明共用一个基准）；
        裂谷级蓝图 rank=20、SDE 时长 2100 秒 = 105 × 20，与社区已知的「护卫级 1 级研究 35 分钟」吻合</dd>
      <dt>为什么以 SDE 为准</dt><dd>多数蓝图满足「时长 = rank × 105」（rank 可为小数，如 7.8），
        但有 154 个不满足（CCP 直接指定了时长），所以一律以 SDE 的时长作基准按比例外推</dd>
      <dt>费用部分</dt><dd>研究任务的费用公式官方未公开，这里是社区推算（EIV × 2% × 秒数 / 105），仅供量级参考</dd>
    </div>
  </div>`;
}

/* 本地复算累计时长，仅用于展示（与服务端同一套算法） */
function cumulativeOf(info, level) {
  const T = info.level_table || [];
  if (!T.length || !info.base_seconds) return 0;
  const idx = Math.min(Math.max(level, 1), T.length) - 1;
  return Math.round(info.base_seconds * T[idx] / T[0]);
}

function viewResearchResult(res) {
  const info = res.blueprint || {};
  const best = res.facilities && res.facilities[0];
  const noSystem = !(Number(res.cost_index) > 0);
  const cost = (v) => (noSystem ? '<span class="dim">—</span>' : fmtISK(v, 0));
  return `
  <div class="summary" style="margin-top:12px">
    <div class="card"><div class="label">最快设施</div>
      <div class="value" style="font-size:15px">${esc(best ? best.label : '-')}</div>
      <div class="sub">ME 0→10 与 TE 0→10 都在这里算</div></div>
    <div class="card"><div class="label">ME 研究耗时（${res.facilities[0].me.percent_from}% → ${res.facilities[0].me.percent_to}%）</div>
      <div class="value green">${esc(best ? best.me.total_text : '-')}</div>
      <div class="sub">无加成基准 ${esc(best ? best.me.total_base_text : '-')}</div></div>
    <div class="card"><div class="label">TE 研究耗时（${res.facilities[0].te.percent_from}% → ${res.facilities[0].te.percent_to}%）</div>
      <div class="value green">${esc(best ? best.te.total_text : '-')}</div>
      <div class="sub">无加成基准 ${esc(best ? best.te.total_base_text : '-')}</div></div>
    <div class="card"><div class="label">相对 NPC 空间站节省</div>
      ${(() => {
        const npc = res.facilities.find(f => f.structure_type_id === null);
        if (!npc || !best) return '<div class="value">-</div>';
        const save = 1 - best.me.total_seconds / (npc.me.total_seconds || 1);
        return `<div class="value green">${fmtPct(save)}</div>
          <div class="sub">${esc(npc.me.total_text)} → ${esc(best.me.total_text)}</div>`;
      })()}</div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>各设施对比</strong>
      <span class="hint-tiny">按 ME 耗时排序 · 技能倍率 ME ×${Number(res.skill_factors.me).toFixed(4)} / TE ×${Number(res.skill_factors.te).toFixed(4)} · 成本指数 ${Number(res.cost_index).toFixed(4)}</span></div>
    <div class="table-wrap" style="max-height:56vh">
    <table class="grid">
      <thead><tr><th class="name">设施</th><th>时间倍率</th>
        <th>ME 耗时</th><th>ME 费用</th><th>TE 耗时</th><th>TE 费用</th>
        <th>相对最快</th><th class="center">逐级明细</th></tr></thead>
      <tbody>${res.facilities.map((f, i) => `
        <tr>
          <td class="name">${esc(f.label)}
            ${f.note ? `<small class="dim">${esc(f.note)}</small>` : ''}</td>
          <td class="num">×${Number(f.me.time_factor).toFixed(4)}</td>
          <td class="num">${esc(f.me.total_text)}</td>
          <td class="num">${cost(f.me.total_cost)}</td>
          <td class="num">${esc(f.te.total_text)}</td>
          <td class="num">${cost(f.te.total_cost)}</td>
          <td class="num ${f.me.vs_best_ratio > 0 ? 'dim' : 'pos'}">${f.me.vs_best_ratio > 0 ? '+' + fmtPct(f.me.vs_best_ratio) : '最快'}</td>
          <td class="center"><button class="btn btn-xs btn-ghost" data-action="research-detail" data-index="${i}">查看</button></td>
        </tr>`).join('')}</tbody>
    </table></div>
    ${noSystem ? `<div class="hint" style="margin-top:8px; color:var(--amber)">
      ⚠ 还没有选择生产星系，所以成本指数为 0、费用列无法估算（<b>研究时间不受影响</b>）。
      到「蓝图计算」页选一个星系，这里就会一起算上系统成本指数、SCC 与设施税。</div>` : ''}
    <div class="hint" style="margin-top:8px">
      EIV（估算物品价值，按 ME0 材料 × 调整价）${fmtISK(res.eiv)} ISK ·
      费用构成：系统成本指数 ${Number(res.cost_index).toFixed(6)} × 计费基数
      + SCC ${fmtPct(S.industry.scc_surcharge)} + 设施税 ${fmtPct(S.industry.job_tax_rate)}。
      研究任务的费用公式为社区推算，仅供量级参考；时间部分来自官方 SDE 与官方属性，可信度更高。
    </div>
  </div>`;
}

function researchDetailModal(res, index) {
  const f = res.facilities[index];
  if (!f) return;
  const block = (plan, title) => `
    <div class="card-title" style="margin-top:14px"><strong>${title}</strong>
      <span class="hint-tiny">合计 ${esc(plan.total_text)} · 费用 ${fmtISK(plan.total_cost, 0)} ISK
        · 相对无加成节省 ${fmtPct(plan.saving_ratio)}</span></div>
    <table class="grid"><thead><tr><th>等级</th><th>效率</th><th>本级基准</th><th>本级实际</th><th>累计</th><th>本级费用</th></tr></thead>
    <tbody>${plan.levels.map(l => `
      <tr><td class="num">第 ${l.level} 级</td>
        <td class="num">${title.indexOf('ME') === 0 ? (l.level * res.blueprint.me_percent_per_level) + '%' : (l.level * res.blueprint.te_percent_per_level) + '%'}</td>
        <td class="num dim">${esc(fmtDur(l.base_increment_seconds))}</td>
        <td class="num">${esc(fmtDur(l.seconds))}</td>
        <td class="num dim">${esc(fmtDur(l.base_cumulative_seconds))}</td>
        <td class="num">${fmtISK(l.cost, 0)}</td></tr>`).join('')}
    </tbody></table>`;
  showModal(f.label + ' · 逐级明细', `
    <div class="hint">时间倍率 ×${Number(f.me.time_factor).toFixed(4)} · 费用倍率 ×${Number(f.me.cost_factor).toFixed(4)}
      ${(f.me.bonuses.sources || []).length ? ' · 加成来源：' + esc(f.me.bonuses.sources.map(s => s.name).join('、')) : ''}</div>
    ${block(f.me, 'ME 研究（材料效率）')}
    ${block(f.te, 'TE 研究（时间效率）')}
    ${(f.me.bonuses.notes || []).length ? '<div class="hint" style="margin-top:10px">' +
      f.me.bonuses.notes.map(n => '· ' + esc(n)).join('<br>') + '</div>' : ''}`);
}

/* ==================================================================
 * 视图：生产规划（多级 BOM）
 * ================================================================== */
function viewPlanner() {
  const p = S.planner;
  return `
  <div class="card">
    <div class="card-title"><strong>生产规划（多级物料展开）</strong>
      <span class="hint-tiny">按层展开，自动用库存抵扣中间产物，并给出采购清单</span></div>
    <div class="facility">
      <div class="field"><label>目标产品</label>
        <input id="planTarget" type="text" placeholder="搜索并选择目标产品" value="${esc(S.plannerTarget ? S.plannerTarget.name : '')}">
        <div id="planTargetResults"></div>
        ${S.plannerTarget ? `<div class="hint-tiny">已选：${esc(S.plannerTarget.name)}（${S.plannerTarget.type_id}）</div>` : ''}
      </div>
      <div class="field"><label>流程数 / ME / TE</label>
        <div class="row" style="gap:6px">
          <input id="planRuns" type="number" value="${S.plannerRuns || 1}" min="1" style="width:80px">
          <input id="planMe" type="number" value="${S.plannerMe ?? 10}" min="0" max="10" style="width:70px">
          <input id="planTe" type="number" value="${S.plannerTe ?? 20}" min="0" max="20" style="width:70px">
        </div></div>
      <div class="field"><label>最大展开层数</label>
        <input id="planDepth" type="number" value="${S.plannerDepth ?? 3}" min="1" max="6"></div>
      <div class="field" style="display:flex; align-items:flex-end">
        <button class="btn btn-primary btn-block" data-action="planner-run">展开并计算</button></div>
    </div>
    ${S.plannerFacilitySummary ? `<div class="hint" style="margin-top:8px">${esc(S.plannerFacilitySummary)}</div>` : ''}
  </div>
  ${p ? `
  <div class="summary" style="margin-top:12px">
    <div class="card"><div class="label">基础材料成本</div><div class="value">${fmtCompact(p.totals.material_cost)}</div>
      <div class="sub mono">${fmtISK(p.totals.material_cost)} ISK</div></div>
    <div class="card"><div class="label">任务费用合计</div><div class="value">${fmtCompact(p.totals.job_cost)}</div>
      <div class="sub">${p.totals.jobs} 个任务 · ${p.totals.materials} 种材料</div></div>
    <div class="card"><div class="label">总成本</div><div class="value blue">${fmtCompact(p.totals.total_cost)}</div>
      <div class="sub">缺口采购成本 ${fmtCompact(p.totals.shortfall_cost)} ISK</div></div>
    <div class="card"><div class="label">预计总耗时（串行估算）</div><div class="value">${esc(p.totals.time_text)}</div>
      <div class="sub">任务工时合计 ${esc(p.totals.total_job_time_text)}</div></div>
  </div>
  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>逐层任务</strong></div>
    <div class="table-wrap" style="max-height:34vh">
    <table class="grid">
      <thead><tr><th>层</th><th class="name">产品</th><th>活动</th><th>需要数量</th><th>流程数</th>
        <th>产出</th><th>多余</th><th>耗时</th><th>任务费用</th></tr></thead>
      <tbody>${(p.jobs || []).map(j => `
        <tr><td class="num">L${j.depth}</td>
          <td class="name"><div class="item-cell">
            ${typeIconHtml(j.product_type_id, 32, 'item-icon')}
            <div class="nm">${esc(j.product_name || '')}</div></div></td>
          <td>${esc(j.activity_name || '')}</td>
          <td class="num">${fmtNum(j.quantity_needed)}</td>
          <td class="num">${fmtNum(j.runs)}</td>
          <td class="num">${fmtNum(j.quantity_produced)}</td>
          <td class="num dim">${j.surplus ? fmtNum(j.surplus) : '-'}</td>
          <td>${esc(j.time_text)}</td>
          <td class="num">${fmtISK(j.job_cost)}</td>
        </tr>`).join('')}</tbody>
    </table></div>
  </div>
  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>采购清单（基础材料）</strong>
      <span class="hint-tiny">已扣减库存；缺口 = 需要 − 库存</span></div>
    <div class="table-wrap" style="max-height:44vh">
    <table class="grid">
      <thead><tr><th class="name">材料</th><th>需要</th><th>库存</th><th>需采购</th>
        <th>单价</th><th>采购成本</th><th>体积(m³)</th></tr></thead>
      <tbody>${(p.shopping_list ? p.shopping_list.rows : []).map(row => `
        <tr>
          <td class="name"><div class="item-cell">
            ${typeIconHtml(row.type_id, 32, 'item-icon')}
            <div class="nm">${esc(row.name || '')}</div></div></td>
          <td class="num">${fmtNum(row.needed)}</td>
          <td class="num dim">${fmtNum(row.inventory)}</td>
          <td class="num ${row.buy > 0 ? 'neg' : 'dim'}">${fmtNum(row.buy)}</td>
          <td class="num">${fmtISK(row.unit_price)}</td>
          <td class="num">${fmtISK(row.cost)}</td>
          <td class="num">${fmtNum(row.volume, 1)}</td>
        </tr>`).join('')}</tbody>
      <tfoot><tr><td class="name">合计</td><td colspan="4"></td>
        <td class="num">${fmtISK(p.shopping_list ? p.shopping_list.total_cost : 0)}</td>
        <td class="num">${fmtNum(p.shopping_list ? p.shopping_list.total_volume : 0, 1)}</td></tr></tfoot>
    </table></div>
  </div>
  ${(p.notes || []).length || (p.warnings || []).length ? `<div class="card" style="margin-top:12px">
    <div class="card-title"><strong>说明与提示</strong></div>
    <div class="hint">${(p.notes || []).concat((p.warnings || []).slice(0, 15)).map(x => '· ' + esc(x)).join('<br>')}</div>
  </div>` : ''}` : '<div class="card" style="margin-top:12px"><div class="empty">选择目标产品与层数，然后点「展开并计算」。</div></div>'}`;
}

/* ==================================================================
 * 视图：价格对比
 * ================================================================== */
function viewCompare() {
  const L = S.market.local || {};
  const priceTs = L.prices && L.prices.updated_at;
  return `
  <div class="card">
    <div class="card-title"><strong>价格与盘口</strong>
      <span class="hint-tiny">本地物价更新于 ${esc(fmtLocalTime(priceTs))}
        · 调整价 ${fmtNum(L.prices ? L.prices.entries : 0)} 条
        · 盘口 ${fmtNum(L.market_stats ? L.market_stats.entries : 0)} 条
        · 历史 ${fmtNum(L.history ? L.history.rows : 0)} 行</span></div>
    <div class="facility">
      <div class="field"><label>物品名称</label>
        <input id="cmpSearch" type="text" placeholder="搜索物品，例如 Tritanium / 三钛合金">
        <div id="cmpResults"></div>
        ${S.cmpType ? `<div class="hint-tiny">已选：${esc(S.cmpType.name)}（${S.cmpType.type_id}）</div>` : ''}</div>
      <div class="field" style="display:flex; align-items:flex-end; gap:6px">
        <button class="btn btn-primary" data-action="cmp-load">查询盘口</button>
        <button class="btn" data-action="cmp-history">近 30 天历史</button>
        <button class="btn btn-green" data-action="price-open"
          title="从 ESI 拉取并保存到本地">刷新物价…</button>
      </div>
    </div>
  </div>
  ${S.orderbook ? `
  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>盘口 · ${esc(S.cmpType ? S.cmpType.name : '')}</strong>
      <span class="hint-tiny">星域 ${S.orderbook.region_id} · 共 ${S.orderbook.orders} 条挂单</span></div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:14px">
      <div><div class="card-title"><strong>卖单（自低到高）</strong></div>
        <table class="grid"><thead><tr><th>价格</th><th>数量</th><th class="name">地点</th></tr></thead>
        <tbody>${(S.orderbook.sell || []).map(o => `<tr><td class="num">${fmtISK(o.price)}</td>
          <td class="num">${fmtNum(o.volume)}</td><td class="name dim">${esc(o.location_id)}</td></tr>`).join('')}</tbody></table></div>
      <div><div class="card-title"><strong>买单（自高到低）</strong></div>
        <table class="grid"><thead><tr><th>价格</th><th>数量</th><th class="name">地点</th></tr></thead>
        <tbody>${(S.orderbook.buy || []).map(o => `<tr><td class="num">${fmtISK(o.price)}</td>
          <td class="num">${fmtNum(o.volume)}</td><td class="name dim">${esc(o.location_id)}</td></tr>`).join('')}</tbody></table></div>
    </div>
  </div>` : ''}
  ${S.history ? `
  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>近 ${(S.history.rows || []).length} 天历史</strong>
      <span class="hint-tiny">7 日成交量加权均价：${fmtISK(S.history.average_7d)} ISK</span></div>
    <div class="table-wrap" style="max-height:40vh">
    <table class="grid"><thead><tr><th>日期</th><th>最高</th><th>最低</th><th>均价</th><th>成交量</th><th>订单数</th></tr></thead>
    <tbody>${(S.history.rows || []).map(r => `<tr><td>${esc(r.date)}</td>
      <td class="num">${fmtISK(r.highest)}</td><td class="num">${fmtISK(r.lowest)}</td>
      <td class="num">${fmtISK(r.average)}</td><td class="num">${fmtNum(r.volume)}</td>
      <td class="num">${fmtNum(r.order_count)}</td></tr>`).join('')}</tbody></table></div>
  </div>` : ''}`;
}

/* ==================================================================
 * 视图：设置
 * ================================================================== */
function viewSettings() {
  const cfg = (S.status && S.status.config) || {};
  const sso = (S.status && S.status.sso) || {};
  const tiers = cfg.scope_tiers || {};
  return `
  <div class="card">
    <div class="card-title"><strong>登录 EVE</strong>
      <span class="hint-tiny">程序已内置应用凭据，直接点下面的按钮授权即可</span></div>
    ${portWarning()}
    ${scopeAuditWarning()}
    <div class="sso-launch">
      <div class="row" style="gap:8px; align-items:center; flex-wrap:wrap">
        <button class="btn btn-primary sso-go" data-action="sso-go">
          <span class="sso-go-icon">➜</span> 一键登录 EVE
        </button>
        <label class="hint-tiny" style="margin:0">授权范围</label>
        <select id="setScopeTier" style="width:auto; min-width:210px">
          ${Object.entries(tiers).map(([key, tier]) =>
            `<option value="${key}"${(cfg.default_scope_tier || 'corporation') === key ? ' selected' : ''}
              >${esc(tier.label)}（${tier.count} 项）</option>`).join('')}
        </select>
        <button class="btn btn-ghost btn-sm" data-action="auth-manual"
          title="授权后浏览器没能自动跳回本机时，用这个手动粘贴回调地址">手动粘贴授权码</button>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>市场与价源</strong></div>
    <div class="facility">
      <div class="field"><label>市场星域</label>
        <select id="setRegion">
          ${Object.entries(cfg.regions || {}).map(([k, v]) =>
            `<option value="${k}" ${cfg.region_key === k ? 'selected' : ''}>${esc(v.name)}</option>`).join('')}
        </select></div>
      <div class="field"><label>采购价来源</label>
        <select id="setPriceSource">
          ${[['jita_sell', '吉他最低卖单（需要查询盘口）'], ['jita_buy', '吉他最高买单'],
             ['adjusted', 'CCP 调整价（每日更新，零额外请求）'],
             ['average', 'CCP 平均价'], ['history', '近 7 日成交均价']]
            .map(([k, l]) => `<option value="${k}" ${cfg.price_source === k ? 'selected' : ''}>${l}</option>`).join('')}
        </select></div>
      <div class="field"><label>盘口查询范围</label>
        <select id="setMarketScope">
          ${[['products', '只查产品（省请求）'], ['all', '产品与材料都查（更准）'], ['none', '完全不查（只用调整价）']]
            .map(([k, l]) => `<option value="${k}" ${S.price.market_scope === k ? 'selected' : ''}>${l}</option>`).join('')}
        </select></div>
      <div class="field" style="display:flex; align-items:flex-end">
        <button class="btn btn-primary btn-block" data-action="save-market">保存</button></div>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>工业计算参数</strong>
      <span class="hint-tiny">这里只影响本机计算，不会改变游戏内数值</span></div>
    <div class="facility">
      <div class="field"><label>SCC 附加费</label>
        <input type="number" step="0.001" data-industry="scc_surcharge" value="${S.industry.scc_surcharge}">
        <div class="hint-tiny">现行 4%（2024-02-01 起）</div></div>
      <div class="field"><label>设施税率</label>
        <input type="number" step="0.0025" data-industry="job_tax_rate" value="${S.industry.job_tax_rate}">
        <div class="hint-tiny">NPC 空间站 0.25%；玩家建筑上限 10%</div></div>
      <div class="field"><label>Alpha 角色附加税</label>
        <input type="number" step="0.0025" data-industry="alpha_clone_tax" value="${S.industry.alpha_clone_tax}">
        <div class="hint-tiny">Alpha 为 0.25%，Omega 填 0</div></div>
      <div class="field"><label>材料取整方式</label>
        <select data-industry="material_rounding">
          <option value="ceil" ${S.industry.material_rounding === 'ceil' ? 'selected' : ''}>向上取整（CCP 实际行为）</option>
          <option value="floor" ${S.industry.material_rounding === 'floor' ? 'selected' : ''}>截断（部分第三方站点用法）</option>
          <option value="round" ${S.industry.material_rounding === 'round' ? 'selected' : ''}>四舍五入</option>
        </select>
        <div class="hint-tiny">两种取整在个别材料上会相差 1 个</div></div>
      <div class="field"><label>是否计入任务费用</label>
        <label class="row" style="font-size:12px"><input type="checkbox" data-industry="include_job_cost"
          ${S.industry.include_job_cost ? 'checked' : ''} style="width:auto"> 计入系统费用 / SCC / 税</label></div>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>静态数据（SDE）</strong>
      <span class="hint-tiny">${S.status && S.status.sde && S.status.sde.ready
        ? `已导入 build ${S.status.sde.local_build} · ${fmtNum((S.status.sde.stats || {}).types)} 个物品 · ${fmtNum((S.status.sde.stats || {}).manufacturing)} 个制造蓝图`
        : '未导入'}</span></div>
    <div class="row">
      <button class="btn btn-primary" data-action="sde-update">下载并导入最新 SDE</button>
      <button class="btn" data-action="sde-force">强制重新导入</button>
      <span class="hint-tiny" id="sdeProgress2">${esc((S.status && S.status.sde && S.status.sde.progress && S.status.sde.progress.message) || '')}</span>
    </div>
    <div class="hint" style="margin-top:8px">来源：CCP 官方 <span class="mono">eve-online-static-data-latest-jsonl.zip</span>（约 95 MB，支持断点与 ETag 缓存）</div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>本地物价数据</strong>
      <span class="hint-tiny">从 ESI 拉取后写入 data/industry.db，离线也能用</span></div>
    ${(() => {
      const L = S.market.local || {};
      return `<div class="kv">
        <dt>调整价 / 平均价</dt><dd>${fmtNum(L.prices ? L.prices.entries : 0)} 条 ·
          更新于 ${esc(fmtLocalTime(L.prices && L.prices.updated_at))}</dd>
        <dt>吉他盘口</dt><dd>${fmtNum(L.market_stats ? L.market_stats.entries : 0)} 条 ·
          更新于 ${esc(fmtLocalTime(L.market_stats && L.market_stats.updated_at))}</dd>
        <dt>历史成交</dt><dd>${fmtNum(L.history ? L.history.rows : 0)} 行 /
          ${fmtNum(L.history ? L.history.types : 0)} 个物品</dd>
        <dt>系统成本指数</dt><dd>${esc(fmtLocalTime(L.cost_indices_at))}</dd>
        <dt>单次盘口上限</dt><dd>${fmtNum(L.max_items || 250)} 个物品
          <span class="dim">（改 config.json 的 market_refresh_max_items）</span></dd>
      </div>`;
    })()}
    <div class="row" style="margin-top:10px; gap:6px">
      <button class="btn btn-primary" data-action="price-open">刷新物价…</button>
      <button class="btn btn-ghost" data-action="market-local-reload">重新读取本地概览</button>
      <span class="hint-tiny">点一下就会真的去 ESI 拉取并覆盖本地记录</span>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>角色与缓存</strong>
      <span class="hint-tiny">「关联状态」不是 ok 时，该角色的数据同步会暂停，等重新登录或确认</span></div>
    ${renderLinkProblems()}
    <div class="table-wrap">
    <table class="grid"><thead><tr><th class="name">角色</th><th>军团</th><th>关联状态</th><th>授权范围</th>
      <th>令牌加密</th><th>军团授权</th><th>缺失 scope</th><th class="center">操作</th></tr></thead>
    <tbody>${((S.status && S.status.characters) || []).map(c => `
      <tr><td class="name">${esc(c.name)} <span class="dim mono">${c.character_id}</span></td>
        <td>${esc(c.corporation_name || '-')}</td>
        <td>${linkStateBadge(c)}</td>
        <td class="num">${(c.scopes || []).length}</td>
        <td>${c.encrypted ? '<span class="badge green">已加密</span>' : '<span class="badge amber">明文</span>'}</td>
        <td>${c.corp_ready === false
              ? `<span class="badge amber" title="缺 ${esc((c.corp_scopes_missing || []).join(', '))}">缺军团授权</span>`
              : '<span class="badge green">完整</span>'}</td>
        <td class="dim">${(c.missing_scopes || []).length ? esc((c.missing_scopes || []).join(', ')) : '无'}</td>
        <td class="center">
          ${c.corp_ready === false
            ? `<button class="btn btn-xs btn-green" data-action="relink-corp">追加军团授权</button>`
            : ''}
          ${c.link_state === 'owner_changed'
            ? `<button class="btn btn-xs btn-green" data-action="relink-confirm" data-id="${c.character_id}">确认已转手</button>`
            : ''}
          ${c.link_state === 'token_dead'
            ? `<button class="btn btn-xs btn-green" data-action="login">重新登录</button>`
            : ''}
          <button class="btn btn-xs" data-action="char-refresh" data-id="${c.character_id}">刷新数据</button>
          <button class="btn btn-xs btn-ghost" data-action="char-roles" data-id="${c.character_id}">职位检查</button>
          <button class="btn btn-xs btn-ghost" data-action="char-skills" data-id="${c.character_id}">技能</button>
          <button class="btn btn-xs btn-danger" data-action="char-remove" data-id="${c.character_id}">移除</button>
        </td></tr>`).join('') || `<tr><td colspan="8" class="dim" style="text-align:center">还没有登录角色</td></tr>`}
    </tbody></table></div>
    <div class="row" style="margin-top:10px">
      <button class="btn btn-ghost" data-action="cache-clear">清空 ESI 响应缓存</button>
      <button class="btn btn-ghost" data-action="cost-refresh">刷新系统成本指数</button>
      <button class="btn btn-ghost" data-action="goto-about">查看环境自检</button>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>本地方案</strong></div>
    <div class="row">${(S.plans || []).map(p =>
      `<span class="rig-chip">${esc(p.name)}
        <button data-action="plan-load" data-id="${p.plan_id}" title="载入">载入</button>
        <button data-action="plan-del" data-id="${p.plan_id}" title="删除">×</button></span>`).join('')
      || '<span class="hint-tiny">还没有保存的方案</span>'}</div>
  </div>`;
}

/* ==================================================================
 * 视图：关于
 * ================================================================== */
function viewAbout() {
  const rep = S.selfTest && S.selfTest.report;
  const env = (rep && rep.environment) || {};
  return `
  <div class="card">
    <div class="card-title"><strong>EVE 工业计算器 · 本地版</strong><span class="hint-tiny">仅欧服 Tranquility</span></div>
    <div class="hint">
      一个不需要安装任何第三方库的本地工业计算工具：直接读取你自己的 ESI 数据（角色 + 军团工业项目、
      蓝图、库存），并结合 CCP 官方静态数据（SDE）做材料、任务费用、时间与利润计算。<br><br>
      <b>数据来源</b>：<br>
      · ESI 接口规范与 SSO：<a href="https://developers.eveonline.com/" target="_blank" rel="noreferrer">developers.eveonline.com</a> ·
        <a href="https://developers.eveonline.com/api-explorer" target="_blank" rel="noreferrer">API Explorer</a> ·
        <a href="https://github.com/esi" target="_blank" rel="noreferrer">github.com/esi</a><br>
      · 静态数据：<a href="https://developers.eveonline.com/static-data/" target="_blank" rel="noreferrer">官方 SDE（JSON Lines）</a><br>
      · 物品图标：CCP 图片服务器 images.evetech.net
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>计算公式与依据</strong>
      <span class="hint-tiny">官方 SDE 属性 + 公开实测数据，逐项可核对</span></div>
    <div class="kv">
      <dt>材料数量</dt><dd>基准量 × 流程数 × (1−ME/100) × 结构材料倍率 × Π(1 + 改装件材料减免 × 安等倍率 / 100)，<b>最后向上取整</b>，且每流程至少 1 个；反应不受 ME 影响</dd>
      <dt>任务时间</dt><dd>基础时间 × 流程数 × (1−TE/100) × 结构时间倍率 × 改装件时间倍率 × Π(1−技能每级减免×等级)，全部相乘</dd>
      <dt>估算物品价值</dt><dd>Σ(ME0 基准材料量 × 流程数 × CCP 调整价) —— 与 ME、建筑、改装件无关</dd>
      <dt>任务费用</dt><dd>计费基数 × 成本指数 × 费用倍率 + SCC 附加费(4%) + 设施税 + Alpha 税；制造/反应基数 = EIV，拷贝/发明 = 2%×EIV，研究 = 2%×EIV×秒数/105</dd>
      <dt>利润率</dt><dd>(总价 − 总成本) ÷ 总成本</dd>
      <dt>安等倍率</dt><dd>高安 ×1.0 / 低安 ×1.9 / 00 与虫洞 ×2.1（读取改装件自带的 2355/2356/2357 属性）</dd>
      <dt>改装件作用范围</dt><dd>按 SDE 的 industryTargetFilters 判断该改装件是否适用于当前产品类别</dd>
      <dt>已知不确定处</dt><dd>研究类任务费用公式来自社区推算；ESI 的成本指数只给 4 位小数，装任务时约有 0.004% 误差；材料取整方式可在设置里切换以对齐其它站点</dd>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>环境自检</strong>
      <button class="btn btn-xs" data-action="selftest">重新检测</button></div>
    ${rep ? `
      <div class="kv">
        <dt>程序版本</dt><dd>${esc(env.app || '')} v${esc(env.version || '')}</dd>
        <dt>Python</dt><dd>${esc(env.python || '')} · ${esc(env.python_path || '')}</dd>
        <dt>依赖</dt><dd>${esc(env.dependencies || '')}</dd>
        <dt>平台</dt><dd>${esc(env.platform || '')}</dd>
        <dt>OpenSSL</dt><dd>${esc(env.openssl || '')}</dd>
        <dt>令牌加密</dt><dd>${esc((rep.encryption || {}).backend || '')}
          ${(rep.encryption || {}).roundtrip ? '<span class="badge green">往返正常</span>' : '<span class="badge red">异常</span>'}</dd>
        <dt>数据库</dt><dd>${fmtNum((rep.store || {}).db_bytes / 1048576, 2)} MB ·
          SDE ${rep.store && rep.store.sde_ready ? '已导入 build ' + esc(rep.store.sde_build) : '未导入'}</dd>
        <dt>兼容日期</dt><dd>${esc(rep.compatibility_date || '（不发送该头）')}</dd>
        <dt>ESI 请求统计</dt><dd>请求 ${fmtNum((S.selfTest.esi_limits.stats || {}).requests)} 次 ·
          缓存命中 ${fmtNum((S.selfTest.esi_limits.stats || {}).cache_hits)} 次 ·
          304 复用 ${fmtNum((S.selfTest.esi_limits.stats || {}).not_modified)} 次 ·
          错误限额剩余 ${S.selfTest.esi_limits.error_limit_remain ?? '—'}</dd>
      </div>
      ${(rep.problems || []).length ? `<div class="hint" style="margin-top:10px; color:var(--amber)">
        待处理：<br>${rep.problems.map(p => '· ' + esc(p)).join('<br>')}</div>`
        : '<div class="hint" style="margin-top:10px; color:var(--green)">一切就绪。</div>'}` : '<div class="hint">点「重新检测」获取环境信息。</div>'}
  </div>

  <div class="card" style="margin-top:12px">
    <div class="card-title"><strong>安全提示</strong></div>
    <div class="hint">
      · 刷新令牌保存在本地数据库 <span class="mono">data/industry.db</span>，在 Windows 上用 <b>DPAPI</b> 加密，
        只有当前 Windows 用户能解密；整个 <span class="mono">data</span> 目录不要分享给别人。<br>
      · 访问令牌只在内存与本地数据库中短期缓存，所有请求都从本机直接发往 ESI，不经过任何第三方服务器。<br>
      · Client Secret 不是必需的（使用 PKCE 公共客户端）。如果填了，它会明文保存在 <span class="mono">config.json</span> 里。
    </div>
  </div>`;
}

/* ==================================================================
 * 交互动作
 * ================================================================== */
async function refreshStatus(quiet) {
  const res = await api('/api/status');
  if (!res.ok) { if (!quiet) fail(res, '无法读取程序状态'); return res; }
  S.status = res;
  S.sso = res.sso || {};
  renderTopbar();
  return res;
}

async function loadReferenceData() {
  const [s, r, rf] = await Promise.all([
    api('/api/industry/structures'),
    api('/api/sde/bonus-sources?activity=manufacturing'),
    api('/api/research/facilities'),
  ]);
  if (s.ok) S.structures = s.structures || [];
  if (r.ok) S.rigs = (r.candidates || []).filter(x => !x.is_structure || x.time_percent || x.material_percent);
  if (rf.ok) {
    S.research.rigCandidates = rf.rigs || [];
    S.research.maxLevel = rf.max_level || 10;
  }
  await loadFuelServices();
  if ((S.fuel || {}).serviceTypeIds && S.fuel.serviceTypeIds.length) calcFuel();
}

/* ---------------- 研究模拟 ---------------- */
async function loadResearchInfo() {
  const R = S.research;
  if (!R.target) return;
  const res = await api('/api/research/blueprint/' + R.target.type_id);
  if (res.ok) R.info = res;
  else fail(res, '读取蓝图研究信息失败');
}

async function runResearch() {
  const R = S.research;
  if (!R.target) return toast('请先选择要研究的蓝图', 'warn');
  const numOr = (sel, fallback) => {
    const v = Number(($(sel) || {}).value);
    return isFinite(v) ? v : fallback;
  };
  R.fromMe = numOr('#rsFromMe', 0);
  R.toMe = numOr('#rsToMe', R.maxLevel || 10);
  R.fromTe = numOr('#rsFromTe', 0);
  R.toTe = numOr('#rsToTe', R.maxLevel || 10);
  toast('正在模拟各设施的研究耗时…', 'info', 1600);
  const res = await post('/api/research/simulate', {
    blueprint_type_id: R.target.type_id,
    from_me: R.fromMe, to_me: R.toMe, from_te: R.fromTe, to_te: R.toTe,
    security: R.security, rig_type_ids: R.rigTypeIds,
    facility: S.facility, industry: S.industry, price: S.price,
  });
  if (!res.ok) return fail(res, '模拟失败');
  R.result = res;
  if (!R.info) R.info = { blueprint: res.blueprint };
  render();
}

/* ==================================================================
 * 刷新物价：从 ESI 拉取市场数据并保存到本地数据库
 * ================================================================== */
async function loadMarketLocal() {
  const res = await api('/api/market/local');
  if (res.ok) {
    S.market.local = res;
    renderTopbar();
  }
  return res;
}

/* 当前「涉及到的物品」：计划里的产品与原料、生产规划的材料、研究目标、正在查价的物品 */
function collectMarketItems() {
  const ids = new Set();
  const add = (v) => { const n = Number(v); if (isFinite(n) && n > 0) ids.add(n); };
  (S.plan || []).forEach(p => add(p.type_id));
  if (S.result) {
    (S.result.rows || []).forEach(r => add(r.type_id));
    (S.result.products || []).forEach(r => add(r.type_id));
  }
  if (S.planner) {
    (S.planner.base_materials || []).forEach(r => add(r.type_id));
    (S.planner.jobs || []).forEach(j => add(j.product_type_id));
    add(S.planner.root_type_id);
  }
  if (S.research.target) add(S.research.target.type_id);
  if (S.research.result) {
    ((S.research.result.blueprint || {}).product_type_id) &&
      add(S.research.result.blueprint.product_type_id);
  }
  if (S.research.owned) {
    (S.research.owned.blueprints || []).forEach(b => add(b.product_type_id));
  }
  if (S.cmpType) add(S.cmpType.type_id);
  return Array.from(ids);
}

const PRICE_SCOPES = [
  ['prices', '只刷新 CCP 调整价 / 平均价',
   '1 次请求就覆盖全部物品的调整价与平均价（估算物品价值 EIV 用的就是调整价）。最快，适合每天点一下。'],
  ['essentials', '调整价 + 系统成本指数 + 工业设施',
   '合计 3 次请求。成本指数决定任务费用，设施清单用于显示生产地点。日常推荐。'],
  ['orders', '上面全部 + 当前涉及物品的吉他盘口',
   '再按物品逐个查询最低卖单 / 最高买单（每个物品 1~2 次请求），这样才能算出真实市价与利润率。'],
  ['history', '上面全部 + 历史成交（近 30 天）',
   '再为每个物品各拉一次历史，用于「近 7 日成交均价」这个价源。'],
  ['all', '全都刷新（盘口 + 历史）',
   '最完整也最慢，首次使用建议选这个。'],
];

function fmtLocalTime(ts) {
  if (!ts) return '（从未）';
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
}

function priceModalHtml(task, finished) {
  const L = S.market.local || {};
  const items = collectMarketItems();
  const maxItems = L.max_items || 250;
  const capped = Math.min(items.length, maxItems);
  const localBox = `
    <div class="card-title" style="margin-top:16px"><strong>本地已保存的数据</strong>
      <button class="btn btn-xs btn-ghost" data-action="market-local-reload">重新读取</button></div>
    <div class="kv">
      <dt>调整价 / 平均价</dt><dd>${fmtNum(L.prices ? L.prices.entries : 0)} 条 ·
        更新于 ${esc(fmtLocalTime(L.prices && L.prices.updated_at))}</dd>
      <dt>吉他盘口</dt><dd>${fmtNum(L.market_stats ? L.market_stats.entries : 0)} 条 ·
        更新于 ${esc(fmtLocalTime(L.market_stats && L.market_stats.updated_at))}</dd>
      <dt>历史成交</dt><dd>${fmtNum(L.history ? L.history.rows : 0)} 行 /
        ${fmtNum(L.history ? L.history.types : 0)} 个物品
        ${L.history && L.history.from ? '（' + esc(L.history.from) + ' → ' + esc(L.history.to) + '）' : ''}</dd>
      <dt>系统成本指数</dt><dd>${esc(fmtLocalTime(L.cost_indices_at))}</dd>
      <dt>本地数据库</dt><dd>${(Number(L.db_bytes || 0) / 1048576).toFixed(1)} MB
        <span class="dim">（data/industry.db，可直接备份）</span></dd>
    </div>`;

  if (S.market.running || (task && task.running)) {
    const done = (task && task.result && task.result.steps) || [];
    const current = (task && task.progress) || '正在连接 ESI…';
    return `
      <div class="hint">正在从 ESI 拉取市场数据并写入本地数据库，请稍候…</div>
      <div class="hint mono" style="margin-top:10px">${esc(current)}</div>
      <div class="bar" style="margin-top:10px"><i style="width:${Math.min(96, 12 + done.length * 18)}%"></i></div>
      ${done.length ? `<div class="kv" style="margin-top:12px">${done.map(s =>
        `<dt>${esc(s.name)}</dt><dd class="${s.ok ? 'pos' : 'neg'}">${s.ok
          ? esc(JSON.stringify(s.detail || {})) : '失败：' + esc(s.error || '')}</dd>`).join('')}</div>` : ''}
      ${localBox}`;
  }

  const steps = (task && task.result && task.result.steps) || [];
  const failed = steps.filter(s => !s.ok);
  const resultBox = steps.length ? `
    <div class="card-title" style="margin-top:16px"><strong>上次刷新结果</strong>
      <span class="hint-tiny">${steps.length} 步，失败 ${failed.length} 步</span></div>
    <div class="kv">${steps.map(s => `<dt>${esc(s.name)}</dt>
      <dd class="${s.ok ? 'pos' : 'neg'}">${s.ok
        ? esc(JSON.stringify(s.detail || {}) || '完成') : '失败：' + esc(s.error || '')}</dd>`).join('')}</div>
    ${task && task.result && task.result.truncated ? `<div class="hint" style="color:var(--amber); margin-top:8px">
      ⚠ 涉及物品超过单次上限 ${maxItems} 个，只刷新了前 ${maxItems} 个。可在 config.json 里调大
      market_refresh_max_items。</div>` : ''}` : '';

  return `
    <div class="hint">选择要拉取的范围。所有数据都会写入 <span class="mono">data/industry.db</span>，
      下次启动直接可用（ESI 本身的缓存时间也会被遵守，避免被判定为绕过缓存）。</div>
    <div style="margin-top:14px">
      ${PRICE_SCOPES.map(([key, title, desc]) => `
        <label class="row" style="gap:9px; align-items:flex-start; margin-bottom:9px; cursor:pointer">
          <input type="radio" name="mktScope" value="${key}" style="width:auto; margin-top:3px"
            ${S.market.scope === key ? 'checked' : ''}>
          <span style="flex:1">
            <b style="font-size:12.5px">${esc(title)}</b>
            <span class="hint-tiny" style="display:block">${esc(desc)}</span>
          </span>
        </label>`).join('')}
    </div>
    <div class="hint" style="margin-top:6px">
      当前涉及 <b>${items.length}</b> 个物品（计划里的产品与原料、生产规划的材料、研究目标、正在查价的物品），
      盘口/历史会按这个清单查询${items.length > maxItems ? `，单次上限 ${maxItems} 个` : ''}。
    </div>
    ${resultBox}
    <div class="row end" style="margin-top:16px; gap:8px">
      <button class="btn btn-ghost" data-action="modal-close">关闭</button>
      <button class="btn btn-green" data-action="price-start">开始刷新</button>
    </div>
    ${localBox}`;
}

async function openPriceModal() {
  await loadMarketLocal();
  showModal('刷新物价（ESI 市场数据）', priceModalHtml(S.market.lastTask, false));
}

let marketPoll = null;

async function startMarketRefresh() {
  const chosen = $('input[name="mktScope"]:checked');
  const scope = (chosen && chosen.value) || S.market.scope;
  S.market.scope = scope;
  const typeIds = collectMarketItems();
  const regionId = (S.status && S.status.config && S.status.config.region_id) || null;
  if (scope !== 'prices' && scope !== 'essentials' && !typeIds.length) {
    return toast('当前没有涉及任何物品，无法刷新盘口/历史。先到「蓝图计算」加几个产品，或改用「只刷新调整价」。',
                 'warn', 7000);
  }
  S.market.running = true;
  renderPriceModal(null, false);
  const res = await post('/api/market/refresh', { scope, type_ids: typeIds, region_id: regionId });
  if (!res.ok) {
    S.market.running = false;
    return fail(res, '无法开始刷新');
  }
  // 服务端明确说「没有物品要刷新」时，别再装作在跑 —— 以前这里照样开始轮询，
  // 用户看到的就是「点了没反应」。
  if (!res.items) {
    S.market.running = false;
    renderPriceModal({ stage: 'done', percent: 100,
                       message: '没有需要刷新的物品（当前生产计划里还没有产品）' }, true);
    toast('没有需要刷新的物品：当前计划里还没有产品。'
        + '想更新全市场调整价，请在弹窗里选「只刷新调整价 / 平均价」。', 'warn', 8000);
    return;
  }
  toast('已开始刷新物价（涉及 ' + res.items + ' 个物品）', 'ok', 3200);
  startMarketPolling();
}

function renderPriceModal(task, finished) {
  if ($('#modal').hidden) return;
  $('#modalBody').innerHTML = priceModalHtml(task, finished);
}

function startMarketPolling() {
  clearInterval(marketPoll);
  marketPoll = setInterval(async () => {
    const res = await api('/api/status');
    if (!res.ok) return;
    S.status = res;
    const task = (res.tasks || {})['market-refresh'];
    renderTopbar();
    renderPriceModal(task, false);
    if (!task || !task.running) {
      clearInterval(marketPoll);
      S.market.running = false;
      S.market.lastTask = task || null;
      await loadMarketLocal();
      renderPriceModal(task, true);
      const steps = (task && task.result && task.result.steps) || [];
      const failed = steps.filter(s => !s.ok).length;
      toast(failed ? `物价刷新完成，但有 ${failed} 步失败（见弹窗）` : '物价已刷新并保存到本地',
            failed ? 'warn' : 'ok', failed ? 8000 : 3600);
      // 刷新后让当前的计算用上新价格
      if (S.view === 'blueprint' && S.plan.length) calculate(true);
      if (S.planner) render();
    }
  }, 1200);
}

async function loadOwnedResearch() {
  const res = await api('/api/research/owned');
  if (!res.ok) return fail(res, '读取我的蓝图失败');
  S.research.owned = res;
  render();
  toast('共 ' + res.count + ' 张可研究的蓝图', 'ok', 2600);
}

async function previewBonus() {
  const res = await post('/api/calc/facility', {
    facility: S.facility, industry: S.industry,
    product_type_id: S.plan.length ? S.plan[0].type_id : null,
    activity: S.plan.length ? S.plan[0].activity : 'manufacturing',
  });
  if (res.ok) { S.bonusPreview = res.bonuses; render(); }
}

/* 重新拉取「当前视图」的数据并重绘。
 * 换角色 / 换授权范围之后必须走这一条 —— 只 render() 会把上一个角色的缓存
 * 数据原样重画，表现就是「换了角色页面却不变」。 */
async function reloadCurrentView() {
  try {
    if (S.view === 'jobs') {
      const r = await api('/api/jobs' + (S.jobStatus && S.jobStatus !== 'all'
        ? '?status=' + encodeURIComponent(S.jobStatus) : ''));
      if (r.ok) S.jobs = r;
    } else if (S.view === 'inventory') {
      const scope = S.inventory.scope || '';
      const q = ($('#invSearch') || {}).value || '';
      const r = await api(`/api/inventory?scope=${encodeURIComponent(scope)}`
        + `&q=${encodeURIComponent(q)}&limit=500`);
      if (r.ok) S.inventory_rows = r;
    } else if (S.view === 'research') {
      await loadOwnedResearch();
      return;
    } else if (S.view === 'blueprint' || S.view === 'planner') {
      await calculate(true);
      return;
    }
  } catch (e) {
    console.warn('刷新当前视图失败', e);
  }
  render();
}

async function loadPlans() {
  const res = await api('/api/plans');
  if (res.ok) S.plans = res.plans || [];
}

/* ---------------- 事件绑定 ---------------- */
document.addEventListener('click', async (ev) => {
  const nav = ev.target.closest('[data-view]');
  if (nav) { go(nav.dataset.view); return; }

  const sideTab = ev.target.closest('.side-tab');
  if (sideTab) {
    $$('.side-tab').forEach(b => b.classList.toggle('active', b === sideTab));
    const all = sideTab.dataset.side === 'all';
    $('#sideAll').style.display = all ? '' : 'none';
    $('#sidePlan').style.display = all ? 'none' : '';
    return;
  }

  // 市场树节点（与游戏侧栏一致的分组树）
  const mg = ev.target.closest('[data-mg]');
  if (mg) {
    showMarketGroup(mg.dataset.mg, mg.dataset.name || '');
    return;
  }
  // 兼容旧的扁平分类入口（万一 market_groups 没导入）
  const tree = ev.target.closest('[data-category]');
  if (tree) {
    showMarketGroup(tree.dataset.category, (tree.textContent || '').trim());
    return;
  }
  // 物品行上的「研究蓝图」：直接从本地 SDE 载入研究模拟（无需 ESI / 无需登录）
  const rb = ev.target.closest('[data-research-bp]');
  if (rb) {
    S.research.target = { type_id: Number(rb.dataset.researchBp), name: rb.dataset.name || '' };
    S.research.result = null;
    S.research.info = null;
    S.research.fromMe = 0;
    S.research.fromTe = 0;
    S.research.toMe = S.research.maxLevel || 10;
    S.research.toTe = S.research.maxLevel || 10;
    go('research');
    loadResearchInfo().then(() => runResearch());
    return;
  }

  const addBtn = ev.target.closest('[data-add-product]');
  if (addBtn) {
    addProduct(addBtn.dataset.addProduct, addBtn.dataset.blueprint, addBtn.dataset.name);
    return;
  }

  const planIdx = ev.target.closest('[data-plan-index]');
  if (planIdx && !ev.target.closest('[data-action]')) {
    const card = $(`[data-card-index="${planIdx.dataset.planIndex}"]`);
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
    return;
  }

  const sortTh = ev.target.closest('[data-sort]');
  if (sortTh) {
    const key = sortTh.dataset.sort;
    if (tableSort.key === key) tableSort.dir *= -1; else tableSort = { key, dir: -1 };
    render();
    return;
  }

  const resTab = ev.target.closest('[data-result-tab]');
  if (resTab) { S.resultTab = resTab.dataset.resultTab; render(); return; }

  const action = ev.target.closest('[data-action]');
  if (!action) return;
  const a = action.dataset.action;
  const idx = action.dataset.index !== undefined ? Number(action.dataset.index) : null;

  switch (a) {
    case 'plan-remove':
      S.plan.splice(idx, 1); savePlanLocal(); renderSidebar(); calculate(true); break;
    case 'toggle-mode': {
      const p = S.plan[idx];
      p.purchase_mode = p.purchase_mode === 'purchase' ? 'build' : 'purchase';
      savePlanLocal(); calculate(true); break;
    }
    case 'plan-clear':
      if (confirm('确定清空当前生产计划？')) {
        S.plan = []; S.build_modes = {}; S.result = null;
        savePlanLocal(); renderSidebar(); render();
      }
      break;
    case 'toggle-material': {
      const tid = action.dataset.typeId;
      const row = (S.result.rows || []).find(x => String(x.type_id) === String(tid));
      S.build_modes[tid] = (row && row.source === 'build') ? 'purchase' : 'build';
      calculate(true); break;
    }
    case 'build-all': {
      (S.result.rows || []).forEach(r => { if (r.can_build) S.build_modes[r.type_id] = 'build'; });
      calculate(true); break;
    }
    case 'purchase-all':
      (S.result.rows || []).forEach(r => { S.build_modes[r.type_id] = 'purchase'; });
      calculate(true); break;
    case 'market-all': {
      S.price.market_scope = 'all';
      S.price.source = S.price.source === 'adjusted' ? 'jita_sell' : S.price.source;
      savePlanLocal();
      toast('正在为全部材料查询吉他盘口，首次可能需要几十秒（之后 5 分钟内走本地缓存）', 'info', 6000);
      await post('/api/config', { price_source: S.price.source });
      calculate(true); break;
    }
    case 'rig-remove': {
      const id = Number(action.dataset.id);
      S.facility.rig_type_ids = (S.facility.rig_type_ids || []).filter(x => Number(x) !== id);
      savePlanLocal(); previewBonus(); calculate(); break;
    }
    case 'system-search': {
      const q = ($('#systemSearch') || {}).value || '';
      if (!q.trim()) break;
      const res = await api('/api/industry/systems/search?q=' + encodeURIComponent(q));
      if (!res.ok || !(res.results || []).length) { toast('没有找到匹配的星系', 'warn'); break; }
      const list = res.results;
      const pick = list.length === 1 ? list[0] : list[0];
      S.facility.system_id = pick.system_id;
      S.facility.system_name = pick.name;
      S.facility.security_status = pick.security_status;
      savePlanLocal(); await previewBonus();
      api('/api/industry/cost-indices?system_id=' + pick.system_id);
      calculate(); break;
    }
    case 'system-best': {
      const res = await api('/api/industry/cheapest-systems?activity=manufacturing&min_security=0.45&limit=5');
      if (!res.ok || !(res.rows || []).length) {
        toast('还没有成本指数数据，正在刷新…', 'warn');
        await api('/api/industry/cost-indices?refresh=1');
        break;
      }
      const best = res.rows[0];
      S.facility.system_id = best.system_id;
      S.facility.system_name = best.name;
      S.facility.security_status = best.security_status;
      toast(`已选择成本指数最低的高安星系：${best.name}（${Number(best.cost_index).toFixed(5)}）`, 'ok', 5200);
      savePlanLocal(); await previewBonus(); calculate(); break;
    }
    case 'sde-update': case 'sde-force': {
      const force = a === 'sde-force';
      const res = await post('/api/sde/update' + (force ? '?force=1' : ''), { force });
      if (!res.ok) fail(res, '无法启动 SDE 更新');
      else {
        // 用服务端自己的话（而不是另编一句），随后交给 startSdePolling 报结果：
        // 它在任务结束时会把「SDE 已是最新（build）」或失败原因弹出来。
        // 以前这里只弹「已在后台开始下载」，任务其实瞬间就结束、又是「已是最新」，
        // 用户什么都没看到 —— 就成了「点了没反应」。
        toast((res.message || '已在后台开始下载并导入 SDE') + '，稍候会报结果', 'ok', 5000);
        startSdePolling();
      }
      break;
    }
    case 'facility-save': {
      await saveFacility();
      break;
    }
    case 'fuel-remove': {
      const f = S.fuel || {};
      f.serviceTypeIds = (f.serviceTypeIds || []).filter(x => Number(x) !== Number(action.dataset.id));
      await calcFuel();
      break;
    }
    case 'fuel-clear': {
      if (S.fuel) { S.fuel.serviceTypeIds = []; S.fuel.plan = null; }
      render();
      break;
    }
    case 'corp-structures-read': {
      await readCorpStructures();
      break;
    }
    case 'corp-structure-apply': {
      applyCorpStructure(action.dataset.structure, action.dataset.type);
      break;
    }
    case 'facility-clear': {
      await clearSavedFacility();
      break;
    }
    case 'db-rebuild': {
      if (!confirm('重建数据库？\n\n' +
          '· 旧文件会保留成 industry.db.corrupt-…（不会直接删）\n' +
          '· 需要重新导入 SDE（约 95 MB）\n' +
          '· 需要重新登录一次 EVE（令牌表也会重建）')) break;
      const res = await post('/api/db/rebuild', {});
      if (!res.ok) return fail(res, '重建数据库失败');
      toast(res.message || '数据库已重建', 'ok', 12000);
      await refreshStatus(true);
      break;
    }
    case 'mg-back': {
      marketBack();
      break;
    }
    case 'cat-back': {
      showCategoryTree();
      break;
    }
    case 'rigs-reload': {
      // 改装件候选没载入成功时的补救入口（以前下拉是空的，选了没反应也无处可点）
      toast('正在重新载入改装件数据…', 'ok', 2000);
      await loadReferenceData();
      toast('改装件数据已重新载入', 'ok', 2600);
      break;
    }
    case 'save-market': {
      const body = {
        region_key: ($('#setRegion') || {}).value,
        price_source: ($('#setPriceSource') || {}).value,
      };
      S.price.market_scope = ($('#setMarketScope') || {}).value || 'products';
      const res = await post('/api/config', body);
      if (!res.ok) fail(res, '保存失败');
      else {
        S.price.source = body.price_source;
        toast('已保存市场设置', 'ok');
        await refreshStatus(true); calculate(true);
      }
      break;
    }
    case 'sso-go': {
      // 一键登录：程序已内置应用凭据，所以不用再保存任何东西，
      // 直接拿授权 URL 并跳到 EVE 官方授权页 —— 不再经过中间的弹窗。
      const tierSelect = $('#setScopeTier');
      const tier = (tierSelect && tierSelect.value) || S.loginTier || 'corporation';
      S.loginTier = tier;
      const info = await prepareLogin(tier);
      if (!info) return;
      goToSso(info);          // 同标签页直接跳转，无需再点一次
      break;
    }
    case 'login':
      await startLogin();
      break;
    case 'login-go': {
      await startLoginFromModal();
      break;
    }
    case 'relink-corp': {
      // 追加军团授权：用完整档重新走一次 SSO（同一角色会追加 scope）
      await prepareLogin('corporation');
      if (S.loginInfo) goToSso(S.loginInfo);
      break;
    }
    case 'wait-reopen': {
      const info = S.waitingInfo;
      if (info && info.url) window.open(info.url, '_blank', 'noopener');
      break;
    }
    case 'wait-cancel': {
      clearInterval(waitPoll);
      const res = await post('/api/auth/cancel', {});
      closeModal();
      toast(res.ok ? (res.message || '已取消') : '取消失败', res.ok ? 'ok' : 'err', 3200);
      break;
    }
    case 'auth-manual-inline': {
      const box = $('#waitCode');
      const raw = box ? String(box.value || '').trim() : '';
      if (!raw) return toast('请先粘贴授权码或整条回调 URL', 'warn');
      const res = await post('/api/auth/manual', { code: raw });
      if (!res.ok) return fail(res, '授权失败');
      clearInterval(waitPoll);
      closeModal();
      toast('授权成功：' + res.name + '，正在拉取数据…', 'ok', 6000);
      await afterLogin(res.character_id);
      break;
    }
    case 'open-guide':
      // 注册指南已移除（Client ID 内置，用户无需注册应用）——
      // 保留这个 action 只为了不炸掉可能残留的旧按钮，点它直接跳到设置页。
      await go('settings');
      break;
    case 'banner-close':
      S.bannerDismissed = true;
      renderLoginBanner();
      break;
    case 'auto-login-cancel':
      cancelAutoLogin();
      break;
    case 'research-search': {
      const q = ($('#researchSearch') || {}).value || '';
      const res = await api('/api/sde/search-blueprint?q=' + encodeURIComponent(q) + '&limit=40');
      if (!res.ok) return fail(res, '搜索失败');
      $('#researchResults').innerHTML = (res.results || []).map(r =>
        `<div class="search-result" data-research-pick="${r.type_id}" data-bp="${r.blueprint_type_id}"
           data-name="${esc(r.name)}">
          ${typeIconHtml(r.type_id, 32, '')}
          <div class="nm">${esc(r.name)}<small>蓝图 #${r.blueprint_type_id} · 每流程产出 ${r.product_quantity}</small></div>
          <span class="badge green">选择</span></div>`).join('') || '<div class="empty">没有结果</div>';
      break;
    }
    case 'research-reset':
      S.research.target = null; S.research.info = null; S.research.result = null;
      render();
      break;
    case 'research-run':
      await runResearch();
      break;
    case 'relink-confirm': {
      const id = Number(action.dataset.id);
      const res = await post(`/api/characters/${id}/confirm-relink`, {});
      if (!res.ok) return fail(res, '无法确认');
      toast('已确认该角色的所有权变更，同步已恢复', 'ok', 4000);
      await refreshStatus(true);
      render();
      break;
    }
    case 'price-open':
      await openPriceModal();
      break;
    case 'price-start':
      await startMarketRefresh();
      break;
    case 'market-local-reload':
      await loadMarketLocal();
      renderPriceModal(S.market.lastTask, false);
      break;
    case 'research-detail':
      researchDetailModal(S.research.result, Number(action.dataset.index));
      break;
    case 'research-owned':
      await loadOwnedResearch();
      break;
    case 'research-load-owned': {
      const R = S.research;
      R.target = { type_id: Number(action.dataset.bp), name: action.dataset.name };
      R.fromMe = Number(action.dataset.me) || 0;
      R.fromTe = Number(action.dataset.te) || 0;
      R.toMe = R.maxLevel || 10;
      R.toTe = R.maxLevel || 10;
      R.result = null; R.info = null;
      await loadResearchInfo();
      await runResearch();
      break;
    }
    case 'rs-rig-remove': {
      const id = Number(action.dataset.id);
      S.research.rigTypeIds = (S.research.rigTypeIds || []).filter(x => Number(x) !== id);
      if (S.research.result) await runResearch(); else render();
      break;
    }
    case 'auth-manual': {
      const raw = prompt('把浏览器地址栏里那段（code=… 或整条回调 URL）粘贴到这里：',
                         S.lastSsoUrl || '');
      if (!raw) break;
      const res = await post('/api/auth/manual', { code: raw, state: S.loginInfo && S.loginInfo.state });
      if (!res.ok) fail(res, '授权失败');
      else {
        toast('登录成功：' + res.name + '，正在拉取数据…', 'ok', 6000);
        closeModal();
        await afterLogin(res.character_id);
      }
      break;
    }
    case 'char-refresh': {
      const res = await post(`/api/characters/${action.dataset.id}/refresh`, {});
      if (!res.ok) fail(res, '无法启动刷新');
      else toast('正在后台拉取该角色的 ESI 数据…', 'ok');
      break;
    }
    case 'char-roles': {
      const res = await api(`/api/characters/${action.dataset.id}/roles`);
      if (!res.ok) return fail(res, '读取职位失败');
      showModal('军团职位检查', `<div class="kv">
        <dt>是否总监</dt><dd>${res.is_director ? '是' : '否'}</dd>
        <dt>是否工厂经理</dt><dd>${res.is_factory_manager ? '是' : '否'}</dd>
        <dt>可读军团工业项目</dt><dd>${res.can_read_corp_industry ? '可以' : '不可以'}</dd>
        <dt>可读军团蓝图 / 资产</dt><dd>${res.can_read_corp_assets ? '可以' : '不可以'}</dd>
        <dt>说明</dt><dd>${esc(res.note || '')}</dd>
        <dt>全部职位</dt><dd>${esc((res.roles || []).join('、') || '（无）')}</dd>
      </div>`);
      break;
    }
    case 'char-skills': {
      const res = await api(`/api/characters/${action.dataset.id}/skills`);
      if (!res.ok) return fail(res, '读取技能失败');
      const lv = res.levels || {};
      const want = [[3380, '工业理论 Industry'], [3388, '高级工业理论 Advanced Industry'],
                    [45746, '反应理论 Reactions'], [3403, '研究概论 Research'],
                    [3409, '冶金学 Metallurgy'], [3406, '实验室运作理论 Laboratory Operation'],
                    [24624, '高级实验室运作理论 Advanced Laboratory Operation']];
      showModal('工业相关技能等级', `<table class="grid"><thead><tr><th class="name">技能</th><th>ID</th><th>等级</th></tr></thead>
        <tbody>${want.map(([id, name]) => `<tr><td class="name">${esc(name)}</td><td class="num">${id}</td>
          <td class="num ${lv[id] ? 'pos' : 'dim'}">${lv[id] === undefined ? '未训练' : lv[id]}</td></tr>`).join('')}</tbody></table>
        <div class="hint" style="margin-top:8px">总技能点 ${fmtNum(res.total_sp)}。这些等级会自动用于时间计算。</div>`);
      break;
    }
    case 'char-remove': {
      if (!confirm('移除该角色的本地授权与缓存数据？')) break;
      const res = await del(`/api/auth/characters/${action.dataset.id}?revoke=1`);
      if (!res.ok) fail(res, '移除失败');
      else { toast('已移除角色', 'ok'); await refreshStatus(true); render(); }
      break;
    }
    case 'cache-clear': {
      const res = await post('/api/cache/clear', {});
      if (res.ok) toast('已清空 ' + res.cleared + ' 条 ESI 缓存', 'ok');
      break;
    }
    case 'cost-refresh': {
      const res = await api('/api/industry/cost-indices?refresh=1');
      if (res.ok) {
        toast('成本指数已更新' + (res.systems ? `（${res.systems} 个星系）` : ''), 'ok');
        await previewBonus();
      } else fail(res, '刷新失败');
      break;
    }
    case 'prices-refresh': {
      const res = await post('/api/market/prices/refresh', {});
      if (res.ok) toast(`调整价已更新：${fmtNum(res.updated || res.entries)} 条`, 'ok');
      else fail(res, '刷新失败');
      break;
    }
    case 'jobs-refresh': {
      const chars = (S.status && S.status.characters) || [];
      const active = chars.find(c => c.is_active) || chars[0];
      if (!active) return toast('请先登录角色', 'warn');
      const res = await post(`/api/characters/${active.character_id}/refresh?corporation=1&assets=1`, {});
      if (!res.ok) fail(res, '无法刷新');
      else {
        toast('正在从 ESI 拉取工业项目与库存…', 'ok');
        setTimeout(async () => { await loadJobs(); }, 2500);
      }
      break;
    }
    case 'rank-run': {
      const params = new URLSearchParams({
        sort: ($('#rankSort') || {}).value || 'margin',
        me: ($('#rankMe') || {}).value || 10,
        te: ($('#rankTe') || {}).value || 20,
        limit: 60, scan_limit: 1500,
      });
      const cat = ($('#rankCategory') || {}).value;
      if (cat) params.set('category_id', cat);
      if (($('#rankRefresh') || {}).checked) params.set('refresh_market', '1');
      toast('正在扫描…', 'info', 1800);
      const res = await api('/api/market/ranking?' + params.toString());
      if (!res.ok) return fail(res, '扫描失败');
      S.ranking = res; render();
      break;
    }
    case 'rank-add':
      addProduct(action.dataset.typeId, action.dataset.blueprint, action.dataset.name);
      break;
    case 'inv-load': {
      const scope = ($('#invScope') || {}).value;
      const q = ($('#invSearch') || {}).value || '';
      S.inventory.scope = scope;
      const res = await api(`/api/inventory?scope=${encodeURIComponent(scope)}&q=${encodeURIComponent(q)}&limit=500`);
      if (!res.ok) return fail(res, '读取库存失败');
      S.inventory_rows = res; render();
      break;
    }
    case 'inv-refresh': {
      const chars = (S.status && S.status.characters) || [];
      const active = chars.find(c => c.is_active) || chars[0];
      const res = await post(`/api/characters/${active.character_id}/refresh?assets=1&corporation=0`, {});
      if (!res.ok) fail(res, '无法刷新');
      else { toast('正在后台刷新库存，几秒后再点「读取库存」', 'ok'); }
      break;
    }
    case 'planner-run': runPlanner(); break;
    case 'cmp-load': {
      if (!S.cmpType) return toast('请先搜索并选择物品', 'warn');
      const res = await api('/api/market/orderbook?type_id=' + S.cmpType.type_id);
      if (!res.ok) return fail(res, '查询盘口失败');
      S.orderbook = res; render();
      break;
    }
    case 'cmp-history': {
      if (!S.cmpType) return toast('请先搜索并选择物品', 'warn');
      const res = await api('/api/market/history?type_id=' + S.cmpType.type_id + '&days=30&refresh=1');
      if (!res.ok) return fail(res, '查询历史失败');
      S.history = res; render();
      break;
    }
    case 'selftest': {
      const res = await api('/api/selftest');
      if (!res.ok) return fail(res, '自检失败');
      S.selfTest = res; render();
      break;
    }
    case 'goto-settings': go('settings'); break;
    case 'goto-about': go('about'); break;
    case 'modal-close': closeModal(); break;
    case 'plan-add-open': openProductPicker(); break;
    case 'plan-save': {
      const name = prompt('方案名称：', '我的生产计划 ' + new Date().toLocaleDateString('zh-CN'));
      if (!name) break;
      const res = await post('/api/plans', { name, data: planPayload() });
      if (!res.ok) fail(res, '保存失败');
      else { toast('方案已保存', 'ok'); await loadPlans(); }
      break;
    }
    case 'plan-load': {
      const res = await api('/api/plans/' + action.dataset.id);
      if (!res.ok) return fail(res, '载入失败');
      const d = res.plan.data || {};
      S.plan = d.items || [];
      S.facility = Object.assign(S.facility, d.facility || {});
      S.industry = Object.assign(S.industry, d.industry || {});
      savePlanLocal(); renderSidebar(); go('blueprint'); calculate(true);
      toast('已载入方案：' + res.plan.name, 'ok');
      break;
    }
    case 'plan-del': {
      if (!confirm('删除该方案？')) break;
      await del('/api/plans/' + action.dataset.id);
      await loadPlans(); render();
      break;
    }
    case 'product-detail': {
      const row = (S.result && S.result.products || [])[idx];
      if (!row) return;
      showModal(row.name + ' · 材料明细', `
        <div class="kv" style="margin-bottom:10px">
          <dt>活动 / ME / TE</dt><dd>${esc(row.activity_name)} · ME${row.me} · TE${row.te}</dd>
          <dt>流程数 / 产出</dt><dd>${fmtNum(row.runs)} → ${fmtNum(row.quantity)} 个</dd>
          <dt>单件成本 / 单价</dt><dd>${fmtISK(row.unit_cost)} / ${fmtISK(row.unit_price)} ISK</dd>
          <dt>总成本 / 总价 / 利润</dt><dd>${fmtISK(row.total_cost)} / ${fmtISK(row.total_price)} /
            <span class="${pctClass(row.total_profit)}">${fmtISK(row.total_profit)}</span></dd>
          <dt>耗时</dt><dd>${esc(row.time_text)}</dd>
        </div>
        <table class="grid"><thead><tr><th class="name">材料</th><th>基准量</th><th>实际数量</th>
          <th>单价</th><th>成本</th><th>库存</th></tr></thead>
        <tbody>${(row.materials || []).map(m => `<tr>
          <td class="name">${esc(m.name)}</td><td class="num dim">${fmtNum(m.base_quantity)}</td>
          <td class="num">${fmtNum(m.quantity)}</td><td class="num">${fmtISK(m.unit_price)}</td>
          <td class="num">${fmtISK(m.cost)}</td><td class="num dim">${fmtNum(m.inventory)}</td></tr>`).join('')}
        </tbody></table>`);
      break;
    }
    case 'product-blueprint': {
      const p = S.plan[idx];
      if (!p || !p.blueprint_type_id) return toast('该物品没有对应蓝图', 'warn');
      const res = await api('/api/sde/blueprint/' + p.blueprint_type_id);
      if (!res.ok) return fail(res, '读取配方失败');
      const acts = res.activities || {};
      showModal((p.name || '') + ' · 全部活动配方', Object.entries(acts).map(([k, v]) => `
        <details class="block"><summary>${esc(k)} · 基础时间 ${fmtDur(v.time)} · 材料 ${(v.materials || []).length} 项</summary>
        <table class="grid"><thead><tr><th class="name">材料</th><th>基准量</th></tr></thead>
        <tbody>${(v.materials || []).map(m => `<tr><td class="name">${esc(m.name || m.type_id)}</td>
          <td class="num">${fmtNum(m.quantity)}</td></tr>`).join('') || '<tr><td colspan="2" class="dim">无材料</td></tr>'}
        </tbody></table></details>`).join(''));
      break;
    }
  }
});

/* 输入变化 */
document.addEventListener('change', async (ev) => {
  const t = ev.target;

  if (t.id === 'charSelect') {
    if (t.value === '__login') {
      // 「+ 登录新角色」= 再授权一个角色。以前这里去点 [data-action="login"]，
      // 而那个按钮在 v0.8.8 换成「登录 EVE」卡片时就被删了 —— 于是这个选项
      // 什么都不做（用户报过：添加第二个角色时点登录没反应）。
      // 现在直接走一键登录：拿授权 URL → 同标签页跳转，回来后新角色自动出现。
      t.value = '';
      const info = await prepareLogin(S.loginTier || 'corporation');
      if (info) goToSso(info);
      return;
    }
    if (!t.value) return;
    const res = await post('/api/auth/active', { character_id: Number(t.value) });
    if (!res.ok) return fail(res, '切换角色失败');
    toast('已切换角色', 'ok', 1800);
    await refreshStatus(true);
    S.inventory.scope = 'char:' + t.value;
    // 换角色后必须**重新拉取当前视图的数据** —— 只 render() 只是把旧角色的
    // 缓存数据重画一遍，看起来就像「换了角色但页面没变」。
    await reloadCurrentView();
    return;
  }
  if (t.id === 'structureSelect') {
    S.facility.structure_type_id = t.value ? Number(t.value) : null;
    // 换建筑时清掉尺寸不匹配的改装件（SDE 属性 1547：建筑与改装件的 rigSize 必须相等）
    const st = (S.structures || []).find(x => Number(x.type_id) === Number(t.value));
    if (st) {
      const size = Number(st.rig_size);
      const limit = Number(st.rig_slots) || 3;
      const before = (S.facility.rig_type_ids || []).map(Number);
      const keep = before.filter(id => {
        const rig = (S.rigs || []).find(x => Number(x.type_id) === id);
        return !rig || Number(rig.rig_size) === size;
      }).slice(0, limit);
      if (keep.length !== before.length) {
        S.facility.rig_type_ids = keep;
        toast(`已移除 ${before.length - keep.length} 个不匹配的改装件`
          + `（${st.name} 只能装 ${st.rig_size_label}改装件，最多 ${limit} 个）`, 'warn', 5200);
      }
    }
    savePlanLocal(); await previewBonus(); calculate(); return;
  }
  if (t.id === 'securitySelect') {
    S.facility.security = t.value || null;
    // 换建筑后重新算燃料（减免跟着建筑走）
    if ((S.fuel || {}).serviceTypeIds && S.fuel.serviceTypeIds.length) calcFuel();
    else render();
    S.facility.security = t.value || null;
    savePlanLocal(); await previewBonus(); calculate(); return;
  }
  if (t.id === 'rigSelect') {
    const id = Number(t.value);
    if (id && !(S.facility.rig_type_ids || []).map(Number).includes(id)) {
      S.facility.rig_type_ids = (S.facility.rig_type_ids || []).concat([id]).slice(0, 3);
      savePlanLocal(); await previewBonus(); calculate();
    }
    t.value = '';
    return;
  }
  if (t.id === 'jobStatusFilter') {
    const res = await api('/api/jobs?status=' + t.value);
    if (res.ok) { S.jobs = res; render(); }
    return;
  }
  if (t.id === 'fuelServiceSelect') {
    // 添加服务装备 → 重算燃料
    const id = Number(t.value);
    const f = S.fuel || (S.fuel = { serviceTypeIds: [], plan: null, corp: null, message: '' });
    if (id && !(f.serviceTypeIds || []).map(Number).includes(id)) {
      const svc = (S.fuelServices || []).find(s => Number(s.type_id) === id);
      const plan = f.plan || {};
      const slots = plan.service_slots || 0;
      if (slots && (f.serviceTypeIds || []).length >= slots) {
        toast(`这个建筑最多同时开 ${slots} 个服务装备`, 'warn', 4200);
      } else {
        f.serviceTypeIds = (f.serviceTypeIds || []).concat([id]);
        if (svc) toast(`已加入 ${svc.name}（${svc.fuel_per_hour}/小时）`, 'ok', 2600);
        t.value = '';
        calcFuel();
        return;
      }
    }
    t.value = '';
    return;
  }
  if (t.id === 'fuelTypeSelect') {
    const f = S.fuel || (S.fuel = { serviceTypeIds: [], plan: null, corp: null, message: '' });
    f.fuelTypeId = Number(t.value) || null;
    await calcFuel();
    return;
  }
  if (t.id === 'invScope') {
    // 换「查看范围」必须真的按新范围取一次数据 —— 以前这里只改了状态变量，
    // 既不请求也不重绘，所以看起来「库存页面切不了角色」。
    S.inventory.scope = t.value;
    savePlanLocal();
    const q = ($('#invSearch') || {}).value || '';
    const res = await api(`/api/inventory?scope=${encodeURIComponent(t.value)}`
      + `&q=${encodeURIComponent(q)}&limit=500`);
    if (!res.ok) return fail(res, '读取库存失败');
    S.inventory_rows = res;
    render();
    return;
  }
  if (t.id === 'rsSecurity') {
    S.research.security = t.value || null;
    if (S.research.result) await runResearch();
    return;
  }
  if (t.id === 'rsRigSelect') {
    const id = Number(t.value);
    t.value = '';
    if (id && !(S.research.rigTypeIds || []).map(Number).includes(id)) {
      S.research.rigTypeIds = (S.research.rigTypeIds || []).concat([id]).slice(0, 2);
      if (S.research.result) await runResearch(); else render();
    }
    return;
  }
  if (['rsFromMe', 'rsToMe', 'rsFromTe', 'rsToTe'].includes(t.id)) {
    if (S.research.result) await runResearch();
    return;
  }

  const planField = t.dataset.planField;
  if (planField) {
    const p = S.plan[Number(t.dataset.index)];
    if (p) {
      p[planField] = Number(t.value) || 0;
      if (planField === 'runs' && !p[planField]) p[planField] = 1;
      savePlanLocal(); renderPlanList(); calculate();
    }
    return;
  }
  const ind = t.dataset.industry;
  if (ind) {
    if (t.type === 'checkbox') S.industry[ind] = t.checked;
    else if (ind === 'material_rounding') S.industry[ind] = t.value;
    else S.industry[ind] = Number(t.value) || 0;
    savePlanLocal();
    if (ind.startsWith('structure_')) { await previewBonus(); }
    calculate(); 
    return;
  }
});

/* 搜索框 */
const doSearchDebounced = debounce(() => doSearch($('#itemSearch').value), 320);
const doInvLoadDebounced = debounce(() => $('[data-action="inv-load"]')?.click(), 420);
const doCompareSearchDebounced = debounce((q) => searchCompare(q), 320);
const doPlannerSearchDebounced = debounce((q) => searchPlannerTarget(q), 320);

document.addEventListener('input', (ev) => {
  const t = ev.target;
  if (t.id === 'itemSearch') { $('#categoryTree').style.display = ''; doSearchDebounced(); }
  if (t.id === 'invSearch') doInvLoadDebounced();
  if (t.id === 'cmpSearch') doCompareSearchDebounced(t.value);
  if (t.id === 'planTarget') doPlannerSearchDebounced(t.value);
});

async function searchCompare(q) {
  if (!q.trim()) return;
  const res = await api('/api/sde/search?q=' + encodeURIComponent(q) + '&limit=12');
  if (!res.ok) return;
  $('#cmpResults').innerHTML = (res.results || []).map(r =>
    `<div class="search-result" data-cmp-pick="${r.type_id}" data-name="${esc(r.name)}">
      ${typeIconHtml(r.type_id, 32, '')}
      <div class="nm">${esc(r.name)}<small>${r.type_id}</small></div></div>`).join('');
}

async function searchPlannerTarget(q) {
  if (!q.trim()) return;
  const res = await api('/api/sde/search-blueprint?q=' + encodeURIComponent(q) + '&limit=12');
  if (!res.ok) return;
  $('#planTargetResults').innerHTML = (res.results || []).map(r =>
    `<div class="search-result" data-planner-pick="${r.type_id}" data-name="${esc(r.name)}">
      ${typeIconHtml(r.type_id, 32, '')}
      <div class="nm">${esc(r.name)}</div></div>`).join('');
}

document.addEventListener('click', (ev) => {
  const cmp = ev.target.closest('[data-cmp-pick]');
  if (cmp) { S.cmpType = { type_id: Number(cmp.dataset.cmpPick), name: cmp.dataset.name };
             $('#cmpSearch').value = cmp.dataset.name; $('#cmpResults').innerHTML = ''; render(); return; }
  const pl = ev.target.closest('[data-planner-pick]');
  if (pl) { S.plannerTarget = { type_id: Number(pl.dataset.plannerPick), name: pl.dataset.name };
            $('#planTargetResults').innerHTML = ''; render(); return; }
  const rs = ev.target.closest('[data-research-pick]');
  if (rs) {
    S.research.target = { type_id: Number(rs.dataset.bp) || Number(rs.dataset.researchPick),
                          name: rs.dataset.name };
    S.research.result = null; S.research.info = null;
    S.research.fromMe = 0; S.research.fromTe = 0;
    S.research.toMe = S.research.maxLevel || 10;
    S.research.toTe = S.research.maxLevel || 10;
    render();
    loadResearchInfo().then(() => runResearch());
    return;
  }
});

/* ---------------- 后台任务轮询 ---------------- */
/* ==================================================================
 * 登录流程（EVE SSO）
 * ------------------------------------------------------------------
 * 为什么不用 window.open：现代浏览器要求「弹窗必须发生在用户手势的同一个任务里」，
 * 而取得授权 URL 需要先 await 一次接口调用，此时手势已经失效，弹窗会被静默拦掉。
 * 所以这里改成**同标签页跳转**：点一下直接把当前页导航到 EVE 授权页，
 * 授权完成后 EVE 把浏览器重定向回本机 /callback，服务端换好令牌再送回首页。
 * 全程不需要用户做任何额外操作，也就不会遇到弹窗拦截。
 * ================================================================== */
const LS_AUTO_LOGIN = 'eve-auto-login';     // '1' = 启动时直接跳转，不再询问

async function prepareLogin(tier) {
  if (tier) S.loginTier = tier;
  const info = await post('/api/auth/login', { tier: S.loginTier || undefined });
  if (!info.ok) {
    fail(info, '无法开始登录');
    if (info.need_setup) go('settings');
    return null;
  }
  S.loginInfo = info;
  S.loginTier = info.tier || S.loginTier;
  return info;
}

/* 授权档位选择器：请求的 scope 越少，交给程序的权限就越小 */
function scopeTierOptions() {
  const tiers = (S.status && S.status.config && S.status.config.scope_tiers) || {};
  const order = ['corporation', 'character'];
  return order.filter(k => tiers[k]).map(key => {
    const tier = tiers[key];
    const checked = (S.loginTier || 'corporation') === key ? 'checked' : '';
    const desc = key === 'corporation'
      ? '除了个人数据，还能读取你所在军团的工业项目 / 蓝图 / 资产 / 建筑。'
      : '只读取你自己的工业项目、蓝图、库存、技能。军团相关的页面会提示需要追加授权。';
    return `<label class="row" style="gap:9px; align-items:flex-start; margin-bottom:8px; cursor:pointer">
      <input type="radio" name="loginTier" value="${key}" style="width:auto; margin-top:3px" ${checked}>
      <span style="flex:1">
        <b style="font-size:12.5px">${esc(tier.label)}</b>
        <span class="hint-tiny" style="display:block">共 ${tier.count} 项权限 —— ${esc(desc)}</span>
      </span></label>`;
  }).join('');
}

/* scope 审计：配置里如果混进了「能代替你做事」的权限（发邮件 / 控制客户端 / 改舰队…），
   明确告警。本程序只需要读权限。 */
/* 端口与回调地址的一致性提示
 * ------------------------------------------------------------------
 * SSO 的 redirect_uri 必须与「应用在 EVE 门户登记的 Callback URL」**完全一致**（含端口）。
 * 程序内置的那个应用登记的就是默认端口 8420。
 * 一旦用 --port 换了端口，就必须回门户把 Callback URL 改成新的，
 * 否则授权时 EVE 会停在自己的错误页、**根本不会跳回本机**，看起来像「登录没反应」。
 * 这种情况下界面必须主动说出来，不能等用户猜。
 */
function portWarning() {
  const sso = (S.status || {}).sso || {};
  if (!sso.port || sso.port_is_default !== false) return '';
  return `<div class="hint" style="margin-top:8px; color:var(--amber)">
    ⚠ 本次运行端口是 <b>${Number(sso.port)}</b>（默认 ${Number(sso.default_port || 8420)}）。
    SSO 回调地址会变成 <span class="mono">${esc(sso.callback || '')}</span>，
    <b>必须回开发者门户把 Callback URL 改成完全一样的值</b>，否则点击登录后
    EVE 会停在自己的错误页、不会跳回本程序（看起来像「登录没反应」）。<br>
    想省事就把端口改回默认：不加 <span class="mono">--port</span> 启动即可。</div>`;
}

function scopeAuditWarning() {
  const audit = (S.selfTest && S.selfTest.scope_audit) || null;
  const configAudit = (S.status && S.status.config && S.status.config.scope_audit) || null;
  const risky = (audit && audit.risky) || [];
  if (!risky.length) {
    return configAudit && configAudit.clean === false
      ? `<div class="hint" style="color:var(--amber)">默认 scope 定义里检测到高危权限，请检查 app/config.py。</div>`
      : '';
  }
  return `
    <div class="hint" style="margin-top:8px; color:var(--amber)">
      ⚠ 配置里的 scopes 包含 ${risky.length} 项<b>能代替你做事</b>的权限：
      <span class="mono">${esc(risky.join('、'))}</span><br>
      本程序只需要读权限（工业项目 / 蓝图 / 库存 / 技能），
      发邮件、控制客户端 UI、改组舰队、改行星工厂这些能力它一个都用不到。
      建议把 config.json 里的 <span class="mono">scopes</span> 删掉以恢复默认值。
    </div>`;
}

/* ==================================================================
 * 「正在等待授权完成」窗口
 * ------------------------------------------------------------------
 * 用户点完「前往授权」就被带到 EVE 页面了。如果回调没回到本机
 * （端口被占、换了浏览器、回调地址被拒…），他回到本程序时不该只看到
 * 一个安静的界面 —— 那样他根本不知道去哪手动粘贴授权码。
 * 所以只要服务端还有待处理的登录尝试，就把窗口弹出来：
 * 既能重新打开授权页，也能直接在里面粘贴授权码。
 * ================================================================== */
let waitPoll = null;

function waitingModalHtml(info) {
  const minutes = Math.max(1, Math.round((info.expires_in || 0) / 60));
  return `
    <div class="hint" style="line-height:1.9">
      已经打开了 <b>EVE 官方授权页</b>，正在等待你完成登录并点 <b>AUTHORIZE</b>。
      <br>完成后浏览器会自动回到本程序，这里会自己关掉并开始拉取数据 —— 不用管它。
    </div>
    <div class="kv" style="margin-top:12px">
      <dt>本次请求权限</dt><dd>${(info.scopes || []).length} 项</dd>
      <dt>回调地址</dt><dd>${esc(info.redirect_uri || '')}</dd>
      <dt>还有效</dt><dd>约 ${minutes} 分钟（过期后重新点登录即可）</dd>
    </div>
    ${info.attempts > 1 ? `<div class="hint" style="margin-top:6px">共有 ${info.attempts} 次待完成的登录，完成后任意一次都算成功。</div>` : ''}
    <div class="row" style="margin-top:14px; gap:8px">
      ${info.url ? `<button class="btn" data-action="wait-reopen">重新打开授权页</button>` : ''}
      <span class="spacer"></span>
      <button class="btn btn-ghost" data-action="wait-cancel">取消这次登录</button>
    </div>
    <div class="card-title" style="margin-top:18px"><strong>授权页打不开 / 回调被拒？</strong>
      <span class="hint-tiny">把浏览器地址栏里那一整条 URL 贴到下面</span></div>
    <textarea id="waitCode" rows="3" placeholder="http://localhost:8420/callback?code=...&state=...  （或只贴 code 的值）"
      style="width:100%; box-sizing:border-box; margin-top:6px; font-family:Consolas,monospace; font-size:12px"></textarea>
    <div class="row" style="margin-top:8px">
      <span class="hint-tiny" style="flex:1">
        程序会从里面取出 code 与 state（state 是必需的：PKCE 的校验器要靠它配对）。
        如果 EVE 提示回调地址不合法，这一步就是最快的解法。</span>
      <button class="btn btn-green" data-action="auth-manual-inline">提交授权码</button>
    </div>`;
}

async function openWaitingModal(info) {
  S.waitingInfo = info;
  showModal('正在等待角色通过 EVE 登录', waitingModalHtml(info));
  startWaitingPolling();
}

function startWaitingPolling() {
  clearInterval(waitPoll);
  const before = ((S.status && S.status.characters) || []).length;
  let ticks = 0;
  waitPoll = setInterval(async () => {
    ticks += 1;
    if (ticks > 200) { clearInterval(waitPoll); return; }   // 最多约 10 分钟
    const res = await api('/api/status');
    if (res.ok) {
      S.status = res;
      renderTopbar();
      const after = (res.characters || []).length;
      if (after > before) {
        // 授权在别处完成了：收工
        clearInterval(waitPoll);
        const addedId = ((res.characters || []).slice(-1)[0] || {}).character_id;
        closeModal();
        toast('授权成功，正在拉取该角色的 ESI 数据…', 'ok', 6000);
        if (addedId) await afterLogin(addedId); else { render(); }
        return;
      }
    }
    // 待处理的登录没了（用户取消 / 过期）→ 关窗
    const pending = await api('/api/auth/pending');
    if (pending.ok && !pending.pending) {
      clearInterval(waitPoll);
      if (modalOpen()) closeModal();
      renderLoginBanner();
    }
  }, 3000);
}

/* 启动时：如果上一次点了授权但还没完成，把等待窗口补回来 */
async function checkPendingLogin() {
  const info = await api('/api/auth/pending');
  if (!info.ok || !info.pending || !info.configured) return false;
  await openWaitingModal(info);
  return true;
}

function loginModalHtml(info) {
  if (!info) {
    return `<div class="hint">还没有配置 Client ID，无法登录。</div>
      <div class="row end" style="margin-top:12px">
        <button class="btn" data-action="modal-close">关闭</button>
        <button class="btn btn-primary" data-action="goto-settings">去设置</button></div>`;
  }
  const auto = localStorage.getItem(LS_AUTO_LOGIN) === '1';
  const st = S.status || {};
  const chars = st.characters || [];
  return `
    <div class="hint" style="line-height:1.9">
      点下面的按钮会跳到 <b>EVE 官方登录页</b>（<span class="mono">login.eveonline.com</span>）：
      <br>&nbsp;&nbsp;① 用 EVE 账号登录 &nbsp;&nbsp;② 选择要授权的角色 &nbsp;&nbsp;③ 确认权限列表
      <br>同意之后浏览器会<b>自动回到本程序</b>，并立刻开始拉取该角色的工业项目、蓝图与库存 ——
      整个过程不需要你再做别的操作。
    </div>
    <div class="card-title" style="margin-top:16px"><strong>授权范围</strong>
      <span class="hint-tiny">按最小权限原则，不需要军团数据就选上面那档</span></div>
    ${scopeTierOptions()}
    <div class="kv" style="margin-top:6px">
      <dt>当前将请求</dt><dd>${info.scopes ? info.scopes.length : '?'} 项权限
        ${info.tier_label ? '（' + esc(info.tier_label) + '）' : ''}</dd>
      <dt>回调地址</dt><dd>${esc(info.redirect_uri || '')}
        <span class="dim">（必须与 EVE 应用里登记的一致）</span></dd>
      <dt>客户端类型</dt><dd>${info.pkce ? 'PKCE 公共客户端（无需 Client Secret）' : '机密客户端（用 Client Secret）'}</dd>
      <dt>已授权角色</dt><dd>${chars.length ? chars.map(c => esc(c.name)).join('、') : '（还没有）'}</dd>
    </div>
    <label class="row" style="margin-top:14px; gap:7px; font-size:12.5px">
      <input type="checkbox" id="loginRemember" ${auto ? 'checked' : ''} style="width:auto">
      以后启动时自动跳转到 EVE 授权页（不再询问）
    </label>
    <div class="row end" style="margin-top:16px; gap:8px">
      <button class="btn btn-ghost" data-action="auth-manual"
        title="如果 EVE 提示回调地址不合法，授权后浏览器的地址栏里会有 code=…，粘到这里即可">回调被拒？手动粘贴授权码</button>
      <button class="btn btn-ghost" data-action="modal-close">稍后再说</button>
      <button class="btn btn-green" data-action="login-go">前往 EVE 授权页面 →</button>
    </div>`;
}

async function startLogin(skipModal) {
  let info = S.loginInfo;
  if (!info || !info.url) info = await prepareLogin();
  if (!info) return;
  if (skipModal) { goToSso(info); return; }
  showModal('登录 EVE 角色', loginModalHtml(info));
}

function goToSso(info) {
  const remember = $('#loginRemember');
  try { localStorage.setItem(LS_AUTO_LOGIN, (remember && remember.checked) ? '1' : '0'); } catch (e) {}
  S.lastSsoUrl = '';
  toast('正在跳转到 EVE 官方授权页…', 'info', 2500);
  // 同标签页跳转：不受弹窗拦截影响，授权完成后回跳也更顺
  location.href = info.url;
}

/* 从登录弹窗点「前往授权」：先看用户选了哪一档 scope，变了就重新申请一次 URL
   （scope 是服务端拼进 URL 的，换档必须换 URL；顺带也会换掉 state 与 PKCE） */
async function startLoginFromModal() {
  const picked = $('input[name="loginTier"]:checked');
  const tier = (picked && picked.value) || S.loginTier || 'corporation';
  let info = S.loginInfo;
  if (!info || info.tier !== tier) {
    info = await prepareLogin(tier);
    if (!info) return;
  }
  goToSso(info);
}

/* 启动时若发现是从 EVE 授权页回来的，自动接管后续：拉数据 → 跳到工业项目 */
async function handleLoginReturn() {
  const params = new URLSearchParams(location.search || '');
  const flag = params.get('login');
  if (!flag) return false;
  const cid = Number(params.get('character_id')) || null;
  try { history.replaceState({}, '', location.pathname + (location.hash || '')); } catch (e) {}
  if (flag !== 'ok') {
    await refreshStatus(true);
    toast('登录没有完成（' + flag + '）。可以再点一次「登录 EVE」。', 'warn', 8000);
    return false;
  }
  toast('授权成功，正在拉取该角色的 ESI 数据…', 'ok', 7000);
  await refreshStatus(true);
  if (!cid) { render(); return true; }
  await afterLogin(cid);
  return true;
}

/* 登录成功后：自动拉取该项目全部数据，然后跳到「工业项目」让用户直接看到结果 */
async function afterLogin(characterId) {
  await refreshStatus(true);
  showModal('正在获取 ESI 数据…', `
    <div class="hint">正在拉取角色的工业项目、蓝图、库存、职位与技能，以及所属军团的数据。</div>
    <div class="hint mono" id="pullProgress" style="margin-top:10px">准备中…</div>
    <div class="bar" style="margin-top:10px"><i id="pullBar" style="width:8%"></i></div>`);
  const res = await post(`/api/characters/${characterId}/refresh?corporation=1&assets=1`, {});
  if (!res.ok) { closeModal(); fail(res, '无法开始拉取数据'); return; }
  const task = await pollTask('refresh-' + characterId, 90, (t) => {
    const el = $('#pullProgress');
    const bar = $('#pullBar');
    if (el) el.textContent = t.progress || '拉取中…';
    if (bar && t.progress) bar.style.width = '60%';
  });
  closeModal();
  await refreshStatus(true);
  await loadJobs();
  if (S.view === 'jobs' || !S.result) go('jobs'); else render();
  const steps = (task && task.result && task.result.steps) || [];
  const failed = steps.filter(s => !s.ok);
  if (failed.length) {
    // 不要猜原因（以前写死「多为军团职位权限不足」，数据库损坏时就会误导用户）——
    // 直接把每一步的真实错误摆出来。
    const detail = failed.map(s => s.name + '：' + String(s.message || s.error || '失败').slice(0, 90));
    const dbBroken = ((S.status || {}).db || {}).integrity_ok === false
      || !!(((S.status || {}).db || {}).init_error);
    toast('数据已拉取，但有 ' + failed.length + ' 步失败 —— ' + detail.join('；')
          + (dbBroken ? '（本地数据库损坏是主因：点顶部「重建数据库」）' : ''), 'err', 12000);
  } else {
    toast('数据就绪：' + steps.length + ' 项已更新', 'ok', 4000);
  }
}

/* 轮询后台任务直到结束。onTick 用于展示进度 */
async function pollTask(name, maxSeconds, onTick) {
  for (let i = 0; i < (maxSeconds || 60); i++) {
    await new Promise(r => setTimeout(r, 1200));
    const res = await api('/api/status');
    if (!res.ok) continue;
    S.status = res;
    renderTopbar();
    const t = (res.tasks || {})[name];
    if (onTick) onTick(t || {});
    if (!t || !t.running) return t || null;
  }
  return null;
}

/* 启动时若还没授权任何角色，主动把登录入口推到用户面前 */
async function maybeAutoLogin() {
  const st = S.status || {};
  if ((st.characters || []).length) return false;      // 已登录，什么都不做
  if (!(st.sso && st.sso.configured)) return false;     // 没配 client_id，登录按钮也点不动
  const auto = (() => { try { return localStorage.getItem(LS_AUTO_LOGIN) === '1'; } catch (e) { return false; } })();
  if (!auto) {
    // 关键：**不要**自动弹登录窗。
    // 蓝图计算、研究模拟、生产规划全都只依赖本地 SDE，不需要登录；
    // 以前这里会弹出模态窗盖住整个界面，用户会以为「没登录就没法用」。
    // 现在只在顶部显示提示条（里面就有登录按钮），界面照常可用。
    return false;
  }
  const info = await prepareLogin();
  if (!info) return false;
  // 用户之前勾了「以后自动跳转」：给 5 秒可见的倒计时，仍然可以取消
  let left = 5;
  const drawCountdown = () => showModal('即将前往 EVE 授权页', `
    <div class="hint">你之前选择了「启动时自动跳转」。<b id="autoLeft">${left}</b> 秒后自动前往
      EVE 官方登录页完成角色授权。</div>
    <div class="hint" style="margin-top:8px">如果不想现在登录，点「取消」即可；也可以在下面的选项里关掉自动跳转。</div>
    <div class="kv" style="margin-top:12px">
      <dt>回调地址</dt><dd>${esc(info.redirect_uri || '')}</dd>
    </div>
    <label class="row" style="margin-top:12px; gap:7px; font-size:12.5px">
      <input type="checkbox" id="loginRemember" checked style="width:auto"> 继续自动跳转
    </label>
    <div class="row end" style="margin-top:14px; gap:8px">
      <button class="btn btn-ghost" data-action="auto-login-cancel">取消</button>
      <button class="btn btn-green" data-action="login-go">现在就前往</button>
    </div>`);
  drawCountdown();
  const timer = setInterval(() => {
    left -= 1;
    const el = $('#autoLeft');
    if (el) el.textContent = String(Math.max(0, left));
    if (left <= 0) {
      clearInterval(timer);
      S.autoLoginCancelled = false;
      goToSso(info);
    }
  }, 1000);
  S.autoLoginTimer = timer;
  return true;
}

function cancelAutoLogin() {
  if (S.autoLoginTimer) clearInterval(S.autoLoginTimer);
  S.autoLoginTimer = null;
  const remember = $('#loginRemember');
  if (remember && !remember.checked) { try { localStorage.setItem(LS_AUTO_LOGIN, '0'); } catch (e) {} }
  closeModal();
  renderLoginBanner();
}

let sdePoll = null, loginPoll = null;

function startSdePolling() {
  clearInterval(sdePoll);
  sdePoll = setInterval(async () => {
    const res = await api('/api/sde/status');
    if (!res.ok) return;
    const p = res.progress || {};
    const el = $('#sdeProgress') || $('#sdeProgress2');
    if (el) el.textContent = `${p.message || ''} ${p.detail || ''} ${p.percent ? p.percent + '%' : ''}`;
    renderTopbar();
    if (!res.running) {
      clearInterval(sdePoll);
      S.status.sde = res;
      if (p.stage === 'error') toast('SDE 导入失败：' + (p.error || ''), 'err', 9000);
      else toast(p.message || 'SDE 导入完成', 'ok', 5200);
      await loadCategories();
      await loadReferenceData();
      render();
    }
  }, 900);
}

/* 兜底：用「新标签页」方式授权时（例如用户手动开了一个标签），靠轮询发现新角色 */
function startLoginPolling() {
  clearInterval(loginPoll);
  let tries = 0;
  loginPoll = setInterval(async () => {
    tries += 1;
    const before = ((S.status && S.status.characters) || []).length;
    const res = await refreshStatus(true);
    const after = ((res && res.characters) || []).length;
    if (after > before) {
      clearInterval(loginPoll);
      closeModal();
      toast('登录成功！', 'ok');
      render();
    }
    if (tries > 60) clearInterval(loginPoll);
  }, 2500);
}

/* ---------------- 弹窗 ---------------- */
function showModal(title, html) {
  $('#modalTitle').textContent = title || '';
  $('#modalBody').innerHTML = html || '';
  $('#modal').hidden = false;
}
function closeModal() { $('#modal').hidden = true; }
function modalOpen() {
  const box = $('#modal');
  return !!box && box.hidden === false;
}

/* 三条退路，避免任何情况下关不掉弹窗：
   ① 右上角「关闭」按钮（data-action="modal-close"）
   ② 点遮罩层空白处
   ③ 按 Esc                                                              */
document.addEventListener('click', (ev) => {
  const box = $('#modal');
  if (box && box.hidden === false && ev.target === box) closeModal();
});
document.addEventListener('keydown', (ev) => {
  if ((ev.key === 'Escape' || ev.key === 'Esc') && modalOpen()) closeModal();
});

function openProductPicker() {
  showModal('添加产品', `
    <div class="search-row"><input id="pickerSearch" type="search" placeholder="输入物品名称（例如 索迪约 / Sotiyo）">
      <button class="btn" data-action="picker-go">搜索</button></div>
    <div id="pickerResults" class="list-scroll" style="max-height:52vh; margin-top:10px"></div>`);
}

document.addEventListener('click', async (ev) => {
  if (!ev.target.closest('[data-action="picker-go"]')) return;
  const q = ($('#pickerSearch') || {}).value || '';
  const res = await api('/api/sde/search-blueprint?q=' + encodeURIComponent(q) + '&limit=50');
  if (!res.ok) return fail(res, '搜索失败');
  $('#pickerResults').innerHTML = (res.results || []).map(r =>
    `<div class="search-result" data-add-product="${r.type_id}" data-blueprint="${r.blueprint_type_id}"
       data-name="${esc(r.name)}" data-close-modal="1">
      ${typeIconHtml(r.type_id, 32, '')}
      <div class="nm">${esc(r.name)}<small>每流程产出 ${r.product_quantity}</small></div>
      <span class="badge green">+</span></div>`).join('') || '<div class="empty">没有结果</div>';
});

document.addEventListener('click', (ev) => {
  if (ev.target.closest('[data-close-modal]')) closeModal();
});

/* ---------------- 生产规划 / 库存 / 工业项目 加载 ---------------- */
async function runPlanner() {
  if (!S.plannerTarget) return toast('请先选择目标产品', 'warn');
  S.plannerRuns = Number(($('#planRuns') || {}).value) || 1;
  S.plannerMe = Number(($('#planMe') || {}).value) || 0;
  S.plannerTe = Number(($('#planTe') || {}).value) || 0;
  S.plannerDepth = Number(($('#planDepth') || {}).value) || 3;
  toast('正在展开物料清单…', 'info', 1600);
  const res = await post('/api/calc/multi', {
    type_id: S.plannerTarget.type_id,
    runs: S.plannerRuns, me: S.plannerMe, te: S.plannerTe,
    max_depth: S.plannerDepth,
    facility: S.facility, industry: S.industry, price: S.price,
    inventory: S.inventory, market_scope: S.price.market_scope,
  });
  if (!res.ok) return fail(res, '展开失败');
  S.planner = res;
  const fb = (res.context || {}).bonuses || {};
  S.plannerFacilitySummary = `设施倍率：材料 ×${Number(fb.material_factor || 1).toFixed(4)} · `
    + `时间 ×${Number(fb.time_factor || 1).toFixed(4)} · 费用 ×${Number(fb.cost_factor || 1).toFixed(4)}`
    + (fb.security ? ` · 安等 ${fb.security.label} ×${fb.security.multiplier}` : '');
  render();
}

async function loadJobs() {
  const res = await api('/api/jobs');
  if (res.ok) S.jobs = res;
  else S.jobs = { ok: true, jobs: [], summary: {}, timeline: { jobs: [] }, message: res.message };
  if (S.view === 'jobs') render();
}

/* ==================================================================
 * 渲染入口
 * ================================================================== */
function render() {
  const main = $('#main');
  try {
    switch (S.view) {
      case 'blueprint': main.innerHTML = viewBlueprint(); break;
      case 'research': main.innerHTML = viewResearch(); break;
      case 'jobs': main.innerHTML = viewJobs(); break;
      case 'ranking': main.innerHTML = viewRanking(); break;
      case 'inventory': main.innerHTML = viewInventory(); break;
      case 'planner': main.innerHTML = viewPlanner(); break;
      case 'compare': main.innerHTML = viewCompare(); break;
      case 'settings': main.innerHTML = viewSettings(); break;
      case 'about': main.innerHTML = viewAbout(); break;
      default: main.innerHTML = '<div class="empty">未知视图</div>';
    }
  } catch (e) {
    // 渲染失败绝不静默：把错误原文 + 可操作的动作摆出来。
    // （以前只写「界面渲染出错」，用户看到白屏只知道要刷新，不知道发生了什么。）
    const ver = (S.status && S.status.app && S.status.app.version) || '';
    main.innerHTML = `<div class="card"><div class="card-title"><strong>界面渲染出错</strong></div>
      <div class="hint mono">${esc((e && e.message) || e)}</div>
      <div class="hint">${ver ? '程序版本 ' + esc(ver) + '。' : ''}
        按 <b>Ctrl+F5</b> 强制刷新一次；若仍出错，请把上面这行错误文字发给我。</div></div>`;
    console.error(e);
  }
  // 保持系统搜索框的即时反馈
  const sc = $('#systemSearch');
  if (sc) sc.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('[data-action="system-search"]')?.click?.(); }
  });
}

/* ==================================================================
 * 启动
 * ================================================================== */
async function boot() {
  loadPlanLocal();
  const hash = (location.hash || '').replace(/^#\/?/, '');
  if (VIEWS.some(([k]) => k === hash)) S.view = hash;
  renderNav();
  await refreshStatus();
  renderTopbar();
  S.loginTier = (S.status && S.status.config && S.status.config.default_scope_tier) || 'corporation';
  restoreSavedFacility();
  await loadMarketLocal();
  await loadReferenceData();
  await loadCategories();
  await loadPlans();
  const sde = (S.status && S.status.sde) || {};
  if (sde.running) startSdePolling();
  S.selfTest = await api('/api/selftest');
  renderSidebar();
  render();
  $('#boot').classList.add('gone');

  // 1) 是不是刚从 EVE 授权页跳回来？是的话自动接管：拉数据 → 跳到工业项目
  const returned = await handleLoginReturn();
  if (returned) return;

  // 2) 上一次点了授权但还没完成？把等待窗口补回来（里面就有手动粘贴的入口）
  if (await checkPendingLogin()) return;

  // 3) 没登录就主动把登录入口推到面前（未配置 client_id 时只显示提示条）
  if (!sde.ready) toast('还没有导入 SDE 静态数据，请到「设置」里点「下载并导入 SDE」', 'warn', 8000);
  maybeAutoLogin();

  if (S.plan.length) calculate(true);
  if ((S.status.characters || []).length) loadJobs();
}

window.addEventListener('hashchange', () => {
  const hash = (location.hash || '').replace(/^#\/?/, '');
  if (VIEWS.some(([k]) => k === hash) && hash !== S.view) { S.view = hash; renderNav(); render(); }
});
window.addEventListener('error', (e) => console.error('前端异常', e.error || e.message));

let booted = false;
function bootOnce() {
  if (booted) return;
  booted = true;
  boot().catch((e) => {
    console.error(e);
    const main = $('#main');
    if (main) main.innerHTML = `<div class="card"><div class="card-title"><strong>启动失败</strong></div>
      <div class="hint mono">${esc(e && e.message || e)}</div></div>`;
    const bootEl = $('#boot');
    if (bootEl) bootEl.classList.add('gone');
  });
}
if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', bootOnce);
} else {
  bootOnce();
}


/* ==================================================================
 * 全局进度条：所有"下载 / ESI 刷新"自动显示
 * ------------------------------------------------------------------
 * 用户要求：所有涉及下载和 ESI 刷新的按钮都要有进度条。
 * 这里不去逐个按钮接线（网页版有 13 个相关 action，逐个接容易漏），
 * 而是在**所有请求的公共出口**（window.fetch）统一拦截：
 *   - 命中规则 → 顶部显示进度条 + 文案
 *   - 响应里带 progress / percent → 切换为确定模式（真实百分比）
 *   - 全部结束 → 短暂显示"完成"再隐藏
 * ================================================================== */
(function () {
  'use strict';
  if (window.__globalProgressInstalled) return;
  window.__globalProgressInstalled = true;

  // 命中即视为"下载 / ESI 刷新"（按 URL 关键字，顺序即优先级）
  var RULES = [
    [/\/api\/sde\/(download|import|update|force)/, '正在下载 / 导入 SDE'],
    [/\/api\/market\/refresh/, '正在刷新物价（ESI）'],
    [/\/api\/characters\/\d+\/refresh/, '正在从 ESI 拉取角色数据'],
    [/\/api\/corporations\/[^/]+\/refresh/, '正在从 ESI 拉取军团数据'],
    [/\/api\/characters\/\d+\/(roles|skills)/, '正在读取角色信息（ESI）'],
    [/\/api\/corporations\/\d+\/(structures|divisions)/, '正在读取军团建筑 / 部门（ESI）'],
    [/\/api\/market\/(orderbook|order-book|history)/, '正在查询市场数据（ESI）'],
    [/\/api\/market\/(local|stats)/, '正在读取市场数据（ESI）'],
    [/\/api\/industry\/cost-indices/, '正在刷新系统成本指数（ESI）'],
    [/\/api\/fuel\//, '正在读取建筑 / 燃料数据（ESI）'],
    [/\/api\/industry\/structures/, '正在读取建筑与改装件数据（ESI）'],
    [/\/api\/research\/(facilities|owned)/, '正在读取研究数据'],
    [/\/api\/sde\/(search|category|blueprint)/, '正在读取静态数据'],
    [/\/api\/tasks\//, '正在等待后台任务']
  ];

  var active = 0, doneTimer = null, mode = 'indet', fill = 0, startedAt = 0;

  var el = document.createElement('div');
  el.id = 'globalProgress';
  el.className = 'gprog';
  el.hidden = true;
  el.innerHTML = '<span class="gprog-text"></span>' +
                 '<span class="gprog-track"><i class="gprog-fill"></i></span>';
  var textEl, fillEl;
  function ensureDom() {
    if (!document.body) return;
    if (!el.parentNode) document.body.appendChild(el);
    if (!textEl) textEl = el.querySelector('.gprog-text');
    if (!fillEl) fillEl = el.querySelector('.gprog-fill');
  }

  function labelFor(url) {
    for (var i = 0; i < RULES.length; i++) {
      if (RULES[i][0].test(url)) return RULES[i][1];
    }
    return '';
  }

  function render() {
    ensureDom();
    if (!el || !textEl || !fillEl) return;
    el.hidden = false;
    el.classList.toggle('gprog-indet', mode === 'indet');
    fillEl.style.width = (mode === 'indet' ? 100 : Math.max(2, Math.min(100, fill))) + '%';
  }

  function start(label) {
    ensureDom();
    if (doneTimer) { clearTimeout(doneTimer); doneTimer = null; }
    if (active === 0) startedAt = Date.now();
    active += 1;
    if (label && textEl) textEl.textContent = label;
    if (mode !== 'det') { mode = 'indet'; fill = 0; }
    render();
  }

  function setPct(pct, label) {
    if (pct === null || pct === undefined || isNaN(pct)) return;
    mode = 'det';
    fill = Number(pct);
    if (label && textEl) textEl.textContent = label;
    render();
  }

  function end(label) {
    active = Math.max(0, active - 1);
    if (active > 0) return;
    // 只有**耗时较长**的操作才把文案改成"完成"；本地请求往往几十毫秒就结束，
    // 若立刻改成"完成"，用户看到的是"完成"而不是"正在…"（真浏览器探针发现的）。
    var elapsed = Date.now() - startedAt;
    if (textEl && label && elapsed > 1500) textEl.textContent = label;
    if (textEl && mode === 'det' && fill < 100) fill = 100;
    render();
    doneTimer = setTimeout(function () {
      if (active === 0) { el.hidden = true; mode = 'indet'; fill = 0; }
    }, 900);
  }

  // 暴露给其他地方（例如 pollTask）手动驱动
  window.progressStart = function (label) { start(label || '处理中…'); };
  window.progressSet = setPct;
  window.progressEnd = function (label) { end(label || '完成'); };

  // ---- 包一层 fetch ----
  var origFetch = window.fetch ? window.fetch.bind(window) : null;
  if (!origFetch) return;
  window.fetch = function (input, init) {
    var url = (typeof input === 'string') ? input : (input && input.url) || '';
    var label = labelFor(url);
    if (!label) return origFetch(input, init);

    start(label);
    var pctFromBody = null;
    return origFetch(input, init).then(function (res) {
      try {
        // 只读副本，绝不影响调用方拿到的响应
        res.clone().json().then(function (data) {
          if (!data || typeof data !== 'object') return;
          var p = (data.progress && (data.progress.percent !== undefined
                    ? data.progress.percent : data.progress))
                  || (data.percent !== undefined ? data.percent : null)
                  || (data.task && data.task.progress && (data.task.progress.percent !== undefined
                        ? data.task.progress.percent : data.task.progress));
          if (p !== null && p !== undefined && !isNaN(p)) {
            setPct(Number(p) <= 1 ? Number(p) * 100 : Number(p),
                   data.progress && data.progress.message ? data.progress.message : label);
          }
        }).catch(function () {});
      } catch (e) {}
      return res;
    }).then(function (res) {
      end('完成');
      return res;
    }, function (err) {
      end('失败（详见日志）');
      throw err;
    });
  };
})();
