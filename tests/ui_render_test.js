/* 前端渲染冒烟测试（开发用，不属于交付物，需要 Node）
 *
 * 目的：app.js 的视图是字符串模板拼出来的，`node --check` 只能查语法，
 * 查不出「引用了不存在的字段」「模板里写错符号」这类运行时错误。
 * 这里用极简 DOM 桩把 app.js 跑起来，再注入一份带数据的 S，逐个渲染视图。
 *
 * 注意：app.js 里的状态是 `const S = {...}`，属于脚本的词法作用域，
 * 直接写 sandbox.S 会被遮蔽（视图会全部走「无数据」分支）。
 * 所以必须在同一个 vm 上下文里再执行一段脚本来改它自己的 S。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CANDIDATES = [
  path.join(__dirname, '..', 'app', 'web', 'app.js'),                    // 放在 tests/ 里时
  path.join(__dirname, '..', 'eve-industry', 'app', 'web', 'app.js'),    // 放在工作区根目录时
];
const APP_JS = process.argv[2] || CANDIDATES.find((p) => fs.existsSync(p));
if (!APP_JS || !fs.existsSync(APP_JS)) {
  console.error('找不到 app.js，请把路径作为第一个参数传入');
  process.exit(2);
}
const code = fs.readFileSync(APP_JS, 'utf8');
console.log('被测文件：' + APP_JS);

/* ══════════════ 1. 极简 DOM 桩 ══════════════ */
function makeEl(tag) {
  return {
    tagName: tag || 'div', _html: '', textContent: '', value: '', hidden: false,
    checked: false, dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, appendChild() {}, remove() {},
    setAttribute() {}, getAttribute() { return null; }, focus() {},
    scrollIntoView() {}, closest() { return null; },
    querySelector() { return makeEl(); }, querySelectorAll() { return []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
  };
}
const registry = {};
const getEl = (sel) => (registry[sel] || (registry[sel] = makeEl()));

const storage = {};
/* 记录 document 上注册的监听器，测试里可以派发合成点击 / change */
const clickListeners = [];
const changeListeners = [];
const sandbox = {
  console,
  document: {
    readyState: 'loading',            // 只注册回调，不让 app.js 自动 boot
    body: makeEl('body'),
    addEventListener(type, fn) {
      if (type === 'click') clickListeners.push(fn);
      if (type === 'change') changeListeners.push(fn);
    },
    removeEventListener() {},
    querySelector: getEl, querySelectorAll() { return []; },
    createElement: (t) => makeEl(t), getElementById: (id) => getEl('#' + id),
  },
  window: { addEventListener() {}, location: { hash: '', href: '' }, open() {} },
  location: { hash: '', href: '' },
  localStorage: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
    removeItem: (k) => { delete storage[k]; },
  },
  setTimeout, clearTimeout, setInterval, clearInterval,
  /* 按 URL 路由的 fetch 桩：让「点击 → 拿授权 URL → 跳转」这条链路能被真正跑通 */
  fetch: async (path) => {
    const routes = sandbox.__routes || {};
    const key = Object.keys(routes).find(k => String(path).startsWith(k));
    const payload = key ? routes[key] : { ok: true };
    const body = typeof payload === 'function' ? payload(path) : payload;
    sandbox.__requests.push(String(path));
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  },
  __routes: {},
  __requests: [],
  confirm: () => false, prompt: () => null, alert: () => {},
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'app.js' });

/* 派发一次合成点击：模拟真实用户点了带 data-action 的元素。
   返回被点击动作执行期间 location.href 被设成什么（用于验证「点击后跳转」）。 */
async function clickAction(action, dataset) {
  const el = makeEl('button');
  el.dataset = Object.assign({ action }, dataset || {});
  const event = {
    target: {
      closest(sel) {
        if (sel === '[data-action]') return el;
        if (sel === '[data-action="' + action + '"]') return el;
        return null;
      },
      dataset: el.dataset,
    },
    preventDefault() {}, stopPropagation() {},
  };
  const before = sandbox.location.href;
  sandbox.location.href = '';           // 清空，便于观察这次点击有没有触发跳转
  for (const fn of clickListeners) {
    await fn(event);
    if (sandbox.location.href) break;
  }
  const jumped = sandbox.location.href;
  if (!jumped) sandbox.location.href = before;
  return jumped;
}

/* 派发一次合成的 change 事件：模拟用户在下拉框里选了某个值。
   （app.js 的 change 处理器读 ev.target.id 与 ev.target.value） */
async function changeSelect(id, value) {
  const el = getEl('#' + id);
  el.id = id;
  el.value = value;
  const event = { target: el, preventDefault() {}, stopPropagation() {} };
  for (const fn of changeListeners) await fn(event);
  return el;
}

/* ══════════════ 2. 造数据 ══════════════ */
const mat = (type_id, name, category_name, quantity, unit_price, inventory, source) => ({
  type_id, name, group_name: '有生货物', category_name, source: source || 'purchase',
  source_label: source === 'build' ? '自造' : '收购', location_name: '吉他', quantity,
  inventory, shortfall: Math.max(0, quantity - inventory), unit_cost: unit_price,
  unit_build_cost: null, unit_price, price_source: '吉他卖单',
  cost_total: unit_price * quantity, total_price: unit_price * quantity, total_profit: 0,
  margin: 0, volume: 3, total_volume: 3 * quantity, can_build: true,
  sub_blueprint_type_id: 21948, sub_activity: 'manufacturing', used_by: ['索迪约'],
  build_detail: source === 'build' ? { job_cost: 1234, time_text: '1小时', me: 10, te: 20 } : null,
});

const product = (type_id, name) => ({
  type_id, name, blueprint_type_id: 36973, activity: 'manufacturing', activity_name: '制造',
  me: 10, te: 20, runs: 1, quantity: 1, inventory: 0, shortfall: 1, purchase_mode: 'build',
  unit_cost: 5.46e10, unit_price: 6.4787e10, price_source: '吉他卖单', material_cost: 5.46e10,
  job_cost: 4.0e9, total_cost: 5.86e10, total_price: 6.4787e10, total_profit: 6.1e9,
  margin: 0.104, volume: 800000, packaged_volume: 800000, total_volume: 800000,
  time_seconds: 700672, time_text: '8天2小时', estimated_item_value: 1.9e10, warnings: [],
  materials: [mat(34, '三钛合金', '矿物', 28800, 3.14, 1000)],
  job_cost_breakdown: {
    estimated_item_value: 1.9e10, job_cost_base: 1.9e10, job_cost_base_factor: 1.0,
    cost_index: 0.1665, system_cost: 3.2e9, facility_reduction: -1.6e8, scc_surcharge: 7.8e8,
    tax: 4.8e7, alpha_tax: 0, total: 3.9e9 },
  facility_bonuses: {
    material_factor: 0.9702, time_factor: 0.56, cost_factor: 0.95,
    material: 0.0298, time: 0.44, cost: 0.05,
    sources: [
      { role: 'structure', name: '索迪约', security_label: '高安', security_multiplier: 1 },
      { role: 'rig', name: '屹立超大型建筑和组件制造效率 I', security_label: '高安', security_multiplier: 1 },
    ],
    notes: [], security: { label: '高安', multiplier: 1 } },
});

const result = {
  ok: true,
  rows: [mat(3810, '海军陆战队员', '有生货物', 225, 4736, 0),
         mat(13267, '看门人', '有生货物', 450, 11790, 0),
         mat(21953, '建筑实验室', '建筑组件', 72, 85900000, 0, 'build')],
  totals: { material_cost: 5.46e10, market_value: 5.465e10, profit: 5.0e7, margin: 0.0009,
            volume: 1.2e6, build_items: 1, purchase_items: 2 },
  products: [product(35827, '索迪约')],
  product_totals: { total_cost: 5.86e10, total_price: 6.4787e10, total_profit: 6.1e9,
                    margin: 0.104, total_volume: 800000, items: 1 },
  warnings: ['测试警告'], activity: 'manufacturing', activity_name: '制造',
};

/* ---- 研究模拟用的假数据 ---- */
const RS_LEVEL_TABLE = [105, 250, 595, 1414, 3360, 8000, 19000, 45255, 107700, 256000];
function rsPlan(activity, totalSeconds, factor) {
  const isMe = activity === 'research_material';
  return {
    activity, activity_name: isMe ? '材料效率研究' : '时间效率研究',
    from_level: 0, to_level: 10,
    levels: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(lv => ({
      level: lv, base_increment_seconds: 200000 * lv,
      base_cumulative_seconds: 200000 * lv * (lv + 1) / 2,
      seconds: Math.round(200000 * lv * factor), time_text: (lv * 3) + '天',
      cost: 1.2e7 * lv, job_cost_base: 9e6 * lv,
    })),
    total_seconds: totalSeconds, total_text: '21天3小时',
    total_base_seconds: 5120000, total_base_text: '59天6小时',
    total_cost: 6.2e7, saving_seconds: 3000000, saving_ratio: 0.64,
    percent_from: 0, percent_to: isMe ? 10 : 20,
    skill_factor: 0.6375, time_factor: factor, cost_factor: 0.95,
    bonuses: { time_factor: factor, cost_factor: 0.95, material_factor: 0.99,
               sources: [{ role: 'structure', name: '索迪约', security_label: '高安', security_multiplier: 1 },
                         { role: 'rig', name: '屹立超大型实验室优化 I', security_label: '高安', security_multiplier: 1 }],
               notes: [], security: { label: '高安', multiplier: 1 } },
    vs_best_ratio: 0,
  };
}
function rsFacility(label, structureId, rigs, totalSeconds, factor, note) {
  return {
    label, structure_type_id: structureId, structure_name: structureId ? label : null,
    rig_type_ids: rigs || [], rig_names: (rigs || []).map(() => '屹立超大型实验室优化 I'),
    note: note || '',
    me: rsPlan('research_material', totalSeconds, factor),
    te: rsPlan('research_time', totalSeconds, factor),
  };
}
const RS_INFO = {
  blueprint_type_id: 36973, name: '索迪约蓝图', product_type_id: 35827, product_name: '索迪约',
  product_group_id: 536, product_category_id: 23, has_research: true,
  base_me_seconds: 1470000, base_te_seconds: 1470000, base_equal: true,
  rank: 14000, rank_implied: 14000, rank_consistent: true, max_level: 10,
  me_percent_per_level: 1, te_percent_per_level: 2, level_table: RS_LEVEL_TABLE,
};
const RS_RESULT = {
  ok: true, blueprint: RS_INFO,
  facilities: [
    rsFacility('索迪约 + 屹立超大型实验室优化 I', 35827, [37183], 1827840, 0.56, '含改装件'),
    rsFacility('索迪约', 35827, [], 2284800, 0.70, '工程复合体'),
    rsFacility('NPC 空间站（无建筑加成）', null, [], 3264001, 1.0, '只有技能减免'),
  ],
  best_me_seconds: 1827840, best_te_seconds: 1827840,
  skill_details: { me: [], te: [] }, skill_factors: { me: 0.6375, te: 0.6375 },
  eiv: 19390084976, cost_index: 0.1665, notes: ['测试说明'],
};

const MARKET_LOCAL = {
  ok: true, region_id: 10000002, region_name: 'The Forge（吉他 Jita）', now: 1791866000,
  prices: { entries: 15988, updated_at: 1791866000 },
  market_stats: { entries: 54, updated_at: 1791866000 },
  history: { rows: 406, types: 1, from: '2025-09-01', to: '2026-10-11' },
  cost_indices_at: 1791865000, facilities_at: 1791865000,
  db_bytes: 22960000, cache: { entries: 120, bytes: 1e6 }, max_items: 250,
};
const MARKET_TASK = {
  running: false, progress: '', result: {
    scope: 'essentials', requested_items: 20, truncated: false, max_items: 250,
    steps: [
      { name: 'CCP 调整价 / 平均价（全物品）', ok: true, detail: { ok: true, updated: 15988 } },
      { name: '系统成本指数', ok: true, detail: { ok: true, systems: 5485, rows: 32910 } },
      { name: '吉他盘口 · 20 个物品', ok: false, error: '部分物品查询失败' },
    ] },
};

const mock = {
  view: 'blueprint', ready: true, sso: { configured: true, callback: 'http://localhost:8420/callback' },
  status: {
    ok: true, app: { name: 'EVE 工业计算器', version: '0.1.0' },
    config: { client_id: 'abc', has_client_secret: false, client_secret: '***', region_key: 'the_forge',
              price_source: 'jita_sell', scc_surcharge: 0.04, job_tax_rate: 0.0025,
              regions: { the_forge: { id: 10000002, name: 'The Forge（吉他 Jita）' } },
              activities: {}, all_scopes: [],
              default_scope_tier: 'corporation',
              scope_tiers: {
                character: { label: '只授权个人数据（最小权限）', count: 8, scopes: [] },
                corporation: { label: '授权个人 + 军团数据（推荐）', count: 13, scopes: [] },
              } },
    sso: { configured: true, callback: 'http://localhost:8420/callback', client_id: 'abc',
           secret_encrypted: { backend: 'Windows DPAPI', roundtrip: true, encrypted: true } },
    sde: { ready: true, running: false, local_build: 3607705, progress: {},
           stats: { types: 53288, manufacturing: 4890 } },
    characters: [
      { character_id: 12345, name: '测试角色', corporation_id: 999,
        corporation_name: '测试军团', scopes: ['esi-industry.read_character_jobs.v1'],
        encrypted: true, is_active: true, missing_scopes: ['esi-assets.read_assets.v1'],
        link_state: 'owner_changed', link_state_label: '角色已转手，需确认', syncs: false,
        owner_hash_present: true,
        corp_ready: false, scope_tier: 'character',
        corp_scopes_missing: ['esi-industry.read_corporation_jobs.v1',
                              'esi-assets.read_corporation_assets.v1'], summary: {} },
      { character_id: 67890, name: '失效角色', corporation_id: 999,
        corporation_name: '测试军团', scopes: [], encrypted: true, is_active: false,
        missing_scopes: [], link_state: 'token_dead', link_state_label: '登录已失效，需重新登录',
        syncs: false, owner_hash_present: true,
        corp_ready: true, scope_tier: 'corporation', corp_scopes_missing: [], summary: {} },
    ],
    prices: { entries: 15988, updated_at: 1791800000 }, cache: { entries: 120, bytes: 1e6 },
    db_bytes: 2.7e7, esi: { error_limit_remain: 95,
                            stats: { requests: 42, cache_hits: 7, not_modified: 3 } },
    tasks: {},
  },
  plan: [{ type_id: 35827, blueprint_type_id: 36973, name: '索迪约', runs: 1, me: 10, te: 20,
           purchase_mode: 'build', activity: 'manufacturing' },
         { type_id: 37183, blueprint_type_id: 37346, name: '屹立超大型实验室优化 I', runs: 1,
           me: 10, te: 20, purchase_mode: 'purchase', activity: 'manufacturing' }],
  facility: { structure_type_id: 35827, rig_type_ids: [37183, 43704], system_id: 30000142,
              system_name: '吉他', security_status: 0.95, security: null },
  industry: { scc_surcharge: 0.04, job_tax_rate: 0.0025, alpha_clone_tax: 0,
              material_rounding: 'ceil', include_job_cost: true, include_job_tax: true,
              structure_material_bonus: 0, structure_time_bonus: 0, structure_cost_bonus: 0 },
  price: { source: 'jita_sell', market_scope: 'products' },
  inventory: { use: true, scope: 'char:12345', include_corporation: true },
  build_modes: {}, material_me: 10, material_te: 20,
  result,
  bonusPreview: { material_factor: 0.9702, time_factor: 0.56, cost_factor: 0.95,
                  material: 0.0298, time: 0.44, cost: 0.05, cost_index: 0.1665,
                  notes: ['测试提示'], sources: [], security: { label: '高安', multiplier: 1 } },
  plans: [{ plan_id: 1, name: '我的计划', created_at: 1, updated_at: 2 }],
  searchResults: [{ type_id: 35827, name: '索迪约', blueprint_type_id: 36973, product_quantity: 1 }],
  categories: [{ category_id: 6, name: '舰船', types: 500 }, { category_id: 23, name: '建筑', types: 0 }],
  structures: (() => {
    // 与真实 SDE 一致的 14 个建筑（含自身无加成的空堡/铁壁/星城/阿塔诺）
    const defs = [
      [35832, '空堡', 1657, '堡垒', 2, '中型', 1, 1, 1, false],
      [35833, '铁壁', 1657, '堡垒', 3, '大型', 1, 1, 1, false],
      [35834, '星城', 1657, '堡垒', 4, '超大型', 1, 1, 1, false],
      [35825, '莱塔卢', 1404, '工程复合体', 2, '中型', 0.99, 0.85, 0.97, true],
      [35826, '阿兹贝尔', 1404, '工程复合体', 3, '大型', 0.99, 0.8, 0.96, true],
      [35827, '索迪约', 1404, '工程复合体', 4, '超大型', 0.99, 0.7, 0.95, true],
      [35835, '阿塔诺', 1406, '精炼厂', 2, '中型', 1, 1, 1, false],
      [35836, '塔塔拉', 1406, '精炼厂', 3, '大型', 1, 1, 1, true],
    ];
    return defs.map(([type_id, name, group_id, group_name, rig_size, rig_size_label,
                      material_factor, time_factor, cost_factor, has_own_bonus]) => ({
      type_id, name, group_id, group_name, rig_size, rig_size_label,
      rig_slots: 3, upgrade_capacity: 400,
      material_factor, time_factor, cost_factor, reaction_time_factor: time_factor,
      has_own_bonus, applies_to_activity: (material_factor !== 1 || time_factor !== 1
        || cost_factor !== 1), activity_scope: '全部活动',
    })).concat([{
      // '地平线'铁壁：自身有科研加成，但对制造**不生效**（游戏内描述只写「科研项目」）
      type_id: 47514, name: '"地平线"铁壁', group_id: 1657, group_name: '堡垒',
      rig_size: 3, rig_size_label: '大型', rig_slots: 3, upgrade_capacity: 400,
      material_factor: 1, time_factor: 1, cost_factor: 1, reaction_time_factor: 1,
      has_own_bonus: true, applies_to_activity: false,
      activity_scope: 'researchTime、researchMaterial、copying、invention',
    }]);
  })(),
  rigs: [
    { type_id: 37162, name: '建筑工程中型改装件 · 屹立中型基础小型舰船制造时间效率 I',
      group_name: '建筑工程中型改装件', material_percent: 0, time_percent: -20,
      cost_percent: 0, rig_size: 2, rig_size_label: '中型', fit_group_ids: [1657],
      is_structure: false },
    { type_id: 37154, name: '建筑工程中型改装件 · 屹立中型基础小型舰船制造材料效率 I',
      group_name: '建筑工程中型改装件', material_percent: -2, time_percent: 0,
      cost_percent: 0, rig_size: 2, rig_size_label: '中型', fit_group_ids: [1657],
      is_structure: false },
    { type_id: 43714, name: '建筑工程大型改装件 · 屹立大型基础小型舰船制造效率 I',
      group_name: '建筑工程大型改装件', material_percent: -2, time_percent: -20,
      cost_percent: 0, rig_size: 3, rig_size_label: '大型', fit_group_ids: [1657],
      is_structure: false },
    { type_id: 43704, name: '屹立超大型建筑和组件制造效率 I',
      group_name: '建筑工程超大型改装件', material_percent: -2, time_percent: -20,
      cost_percent: 0, rig_size: 4, rig_size_label: '超大型', fit_group_ids: [1404],
      is_structure: false },
  ],
  jobs: {
    ok: true, scope: 'char:12345', count: 1, summary: { active: 1 },
    jobs: [{ job_id: 1, scope: 'char:12345', product_type_id: 35827, product_name: '索迪约',
             blueprint_type_id: 36973, blueprint_name: '索迪约蓝图', activity: 'manufacturing',
             activity_name: '制造', runs: 1, successful_runs: 0, licensed_runs: 1,
             probability: null, status: 'active', facility_id: 1000000000001,
             facility_name: '吉他 IV - 测试建筑', start_date: '2026-10-12T10:00:00Z',
             end_date: '2026-10-20T12:00:00Z', start_ts: 1791800000, end_ts: 1792500000,
             remaining_seconds: 100000, remaining_text: '1天3小时', cost: 3.9e9 }],
    timeline: { jobs: [{ job_id: 1, product_name: '索迪约', start_ts: 1791800000,
                        end_ts: 1792500000, remaining_text: '1天3小时' }] },
    meta: { entries: 1, fetched_at: 1791800000 },
  },
  ranking: { ok: true, scanned: 150, matched: 148, sort: 'margin', price_note: '测试说明',
    rows: [{ type_id: 35827, name: '索迪约', blueprint_type_id: 36973, unit_cost: 5.4e10,
             unit_price: 6.4e10, price_source: '吉他卖单', profit: 1e10, margin: 0.185,
             isk_per_hour: 1e8, time_seconds: 700000, time_text: '8天2小时', daily_volume: 12,
             volume: 800000, group_id: 1, category_id: 23, market_group_id: 1, me: 10, te: 20,
             material_cost: 5e10, job_cost: 4e9 }] },
  inventory_rows: { ok: true, scope: 'char:12345', total_volume: 12345.6,
    meta: { entries: 2, fetched_at: 1791800000 },
    rows: [{ type_id: 34, name: '三钛合金', quantity: 1000000, stacks: 3, volume: 0.01,
             total_volume: 10000, location_id: 60003760,
             location_name: '吉他 IV - 卫星 4 - 联邦海军装配厂' }] },
  planner: { ok: true,
    totals: { material_cost: 1e10, job_cost: 4e9, total_cost: 1.4e10, materials: 17, jobs: 16,
              material_volume: 1e6, time_seconds: 4600000, time_text: '53天15小时',
              total_job_time_seconds: 9000000, total_job_time_text: '104天', shortfall_cost: 2e9 },
    jobs: [{ depth: 0, product_type_id: 35827, product_name: '索迪约', activity: 'manufacturing',
             activity_name: '制造', runs: 1, quantity_needed: 1, quantity_per_run: 1,
             quantity_produced: 1, surplus: 0, time_seconds: 700000, time_text: '8天2小时',
             job_cost: 3.9e9, me: 10, te: 20, materials: [] }],
    shopping_list: { rows: [{ type_id: 34, name: '三钛合金', needed: 28800, inventory: 1000,
                              buy: 27800, unit_price: 3.14, cost: 87292, volume: 278 }],
                     total_cost: 87292, total_volume: 278, items: 1 },
    base_materials: [], depth_reports: [], tree: null, notes: ['测试说明'], warnings: ['测试警告'],
    root_name: '索迪约', root_type_id: 35827, runs: 1, me: 10, te: 20 },
  plannerTarget: { type_id: 35827, name: '索迪约' }, plannerRuns: 1, plannerMe: 10, plannerTe: 20,
  plannerDepth: 3, plannerFacilitySummary: '设施倍率：材料 ×0.9702 · 时间 ×0.5600 · 费用 ×0.9500',
  research: {
    target: { type_id: 36973, name: '索迪约蓝图' },
    info: { blueprint: RS_INFO, owned: [], scope: 'char:12345', max_level: 10,
            me_percent_per_level: 1, te_percent_per_level: 2 },
    result: RS_RESULT,
    owned: { ok: true, scope: 'char:12345', count: 1, max_level: 10, facility_count: 15,
      blueprints: [{ item_id: 1, blueprint_type_id: 36973, name: '索迪约蓝图',
        product_type_id: 35827, product_name: '索迪约', runs: -1, quantity: -1,
        material_efficiency: 6, time_efficiency_value: 14, me_level: 6, te_level: 7,
        me_percent: 6, te_percent: 14, to_max_me_seconds: 4000000, to_max_te_seconds: 4000000,
        to_max_me_text: '46天7小时', to_max_te_text: '46天7小时',
        at_me_max: false, at_te_max: false, best_facility: '索迪约 + 屹立超大型实验室优化 I',
        rank: 14000 }] },
    fromMe: 0, toMe: 10, fromTe: 0, toTe: 10, security: null, rigTypeIds: [37183],
    rigCandidates: [{ type_id: 37183, name: '屹立超大型实验室优化 I', group_name: '建筑工程超大型改装件',
                      material_percent: 0, time_percent: -20, cost_percent: -10 }],
    maxLevel: 10,
  },
  market: { local: MARKET_LOCAL, scope: 'essentials', running: false, lastTask: MARKET_TASK },
  cmpType: { type_id: 34, name: '三钛合金' },
  orderbook: { ok: true, type_id: 34, region_id: 10000002, orders: 1200, lowest_sell: 3.2,
    highest_buy: 3.1, sell: [{ price: 3.2, volume: 1000, location_id: 60003760 }],
    buy: [{ price: 3.1, volume: 2000, location_id: 60003760 }] },
  history: { ok: true, type_id: 34, region_id: 10000002, average_7d: 3.15,
    rows: [{ date: '2026-10-12', highest: 3.3, lowest: 3.0, average: 3.15, volume: 1e8,
             order_count: 900 }] },
  selfTest: { ok: true, report: {
      environment: { app: 'eve-industry-local', version: '0.1.0', python: '3.12.14',
                     python_path: 'C:\\python.exe', dependencies: '仅使用 Python 标准库',
                     platform: 'Windows-11', openssl: 'OpenSSL 3.5.8' },
      encryption: { backend: 'Windows DPAPI', roundtrip: true, encrypted: true, warning: null },
      store: { db_ready: true, db_bytes: 2.7e7, sde_ready: true, sde_build: 3607705, types: 53288 },
      compatibility_date: '2026-08-18', problems: [], ok: true },
    esi_limits: { error_limit_remain: 95,
                  stats: { requests: 42, cache_hits: 7, not_modified: 3 } } },
  lastError: null,
};

vm.runInContext('Object.assign(S, ' + JSON.stringify(mock) + ')', sandbox, { filename: 'inject.js' });
/* 在 vm 上下文里求值（$ 与 S 都是词法绑定，外部拿不到，必须回上下文里访问） */
const inCtx = (expr) => vm.runInContext(expr, sandbox, { filename: 'probe.js' });

/* ══════════════ 3. 逐个渲染 ══════════════ */

/* ------------------------------------------------------------------
 * CSS 级联求解：算出 index.html 里每个带 hidden 属性的元素，最终 display 是什么。
 *
 * 为什么需要它：浏览器默认样式里的 [hidden]{display:none} 属于「用户代理样式」，
 * 会被任何作者样式压过。v0.4.0 就栽在这儿 —— .modal{display:flex} 让弹窗在
 * 带 hidden 的情况下照样显示，closeModal() 设 hidden=true 看着像没反应。
 * 这个函数在沙箱里就能复现该判定，不需要真浏览器。
 * ------------------------------------------------------------------ */
function hiddenElementDisplays() {
  const dir = path.dirname(APP_JS);
  // 必须先剥掉注释：否则注释会粘在紧随其后的选择器前面，
  // 让 [hidden] 这类规则被误判成「带空格的复杂选择器」而跳过。
  const css = fs.readFileSync(path.join(dir, 'style.css'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, ' ');
  const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');

  const rules = [];
  const ruleRe = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = ruleRe.exec(css))) {
    const decls = {};
    for (const part of m[2].split(';')) {
      const i = part.indexOf(':');
      if (i < 0) continue;
      const prop = part.slice(0, i).trim().toLowerCase();
      let val = part.slice(i + 1).trim();
      const important = /!important\s*$/i.test(val);
      val = val.replace(/!important\s*$/i, '').trim().toLowerCase();
      if (prop) decls[prop] = { value: val, important };
    }
    for (const sel of m[1].split(',')) {
      const s = sel.trim();
      // 只处理简单复合选择器；带后代/子/兄弟组合的跳过（这些元素不依赖它们）
      if (!s || /[\s>+~]/.test(s.replace(/\[[^\]]*\]/g, ''))) continue;
      rules.push({ selector: s, decls });
    }
  }

  const matches = (sel, el) => {
    const tokens = sel.match(/(^[a-z]+)|(#[A-Za-z0-9_-]+)|(\.[A-Za-z0-9_-]+)|(\[[^\]]+\])/g) || [];
    if (!tokens.length) return false;
    return tokens.every((t) => {
      if (t.startsWith('#')) return el.id === t.slice(1);
      if (t.startsWith('.')) return el.classes.includes(t.slice(1));
      if (t.startsWith('[')) return el.attrs.includes(t.slice(1, -1).split('=')[0].trim());
      return el.tag === t.toLowerCase();
    });
  };
  const spec = (sel) => (sel.match(/#/g) || []).length * 100
                      + (sel.match(/[.[]/g) || []).length * 10;

  const elements = [];
  const tagRe = /<([a-z0-9]+)\b([^>]*\bhidden\b[^>]*)>/gi;
  let t;
  while ((t = tagRe.exec(html))) {
    const attrsRaw = t[2];
    const id = (attrsRaw.match(/\bid="([^"]*)"/) || [])[1] || '';
    const classes = ((attrsRaw.match(/\bclass="([^"]*)"/) || [])[1] || '')
      .split(/\s+/).filter(Boolean);
    const attrs = ['hidden'];
    for (const a of attrsRaw.matchAll(/([A-Za-z-]+)\s*=/g)) attrs.push(a[1].toLowerCase());
    elements.push({ tag: t[1].toLowerCase(), id, classes, attrs,
                    label: id ? '#' + id : t[1] + (classes.length ? '.' + classes.join('.') : '') });
  }

  return elements.map((el) => {
    // 用户代理样式：[hidden]{display:none}，非 !important，会被作者样式压过
    let winner = { value: 'none', important: false, from: 'UA [hidden]', spec: 0 };
    for (const rule of rules) {
      if (!rule.decls.display || !matches(rule.selector, el)) continue;
      const d = rule.decls.display;
      if (d.important) {
        winner = { value: d.value, important: true, from: rule.selector, spec: spec(rule.selector) };
      } else if (!winner.important) {
        const s = spec(rule.selector);
        if (s >= winner.spec) winner = { value: d.value, important: false, from: rule.selector, spec: s };
      }
    }
    return { label: el.label, ...winner };
  });
}

const cases = [
  ['导航栏', () => sandbox.renderNav()],
  ['顶栏状态', () => sandbox.renderTopbar()],
  ['侧栏计划列表', () => sandbox.renderPlanList()],
  ['侧栏搜索结果', () => sandbox.renderSearchResults()],
  ['视图·蓝图计算', () => sandbox.viewBlueprint()],
  ['子表·材料明细', () => sandbox.viewMaterialsTable(result)],
  ['子表·产品明细', () => sandbox.viewProductsTable(result)],
  ['子块·费用与时间', () => sandbox.viewBreakdown(result)],
  ['视图·工业项目', () => sandbox.viewJobs()],
  ['视图·利润排行', () => sandbox.viewRanking()],
  ['视图·库存', () => sandbox.viewInventory()],
  ['视图·生产规划', () => sandbox.viewPlanner()],
  ['视图·研究模拟', () => sandbox.viewResearch()],
  ['子块·研究结果', () => sandbox.viewResearchResult(RS_RESULT)],
  ['子块·研究结果(未选星系应给出提示)', () => {
    const html = sandbox.viewResearchResult(Object.assign({}, RS_RESULT, { cost_index: 0 }));
    if (!html.includes('还没有选择生产星系')) throw new Error('未选星系时缺少提示文案');
    if (!html.includes('—')) throw new Error('未选星系时费用列应显示占位符');
    return html;
  }],
  ['子块·累计换算', () => String(sandbox.cumulativeOf(RS_INFO, 10))],
  ['物价弹窗·可刷新状态', () => sandbox.priceModalHtml(MARKET_TASK, true)],
  ['物价弹窗·进行中状态', () => {
    const html = sandbox.priceModalHtml({ running: true, progress: '历史成交 3/20' }, false);
    if (!html.includes('历史成交 3/20')) throw new Error('进行中状态未显示进度文案');
    if (!html.includes('正在从 ESI 拉取')) throw new Error('缺少进行中提示');
    return html;
  }],
  ['物价收集·涉及物品清单', () => String(sandbox.collectMarketItems().length)],
  ['弹窗·打开/关闭契约', () => {
    // 注：DOM 桩里 hidden 初值为 false，真实页面由 index.html 的 hidden 属性保证初始隐藏
    inCtx('showModal("测试标题", "<b>内容</b>")');
    if (inCtx('$("#modal").hidden') !== false) throw new Error('showModal 未把弹窗显示出来');
    if (inCtx('$("#modalTitle").textContent') !== '测试标题') throw new Error('标题未写入');
    if (!String(inCtx('$("#modalBody").innerHTML')).includes('内容')) throw new Error('正文未写入');
    inCtx('closeModal()');
    if (inCtx('$("#modal").hidden') !== true) throw new Error('closeModal 未把弹窗隐藏');
    // 空参数不应崩，标题也不能残留上一次的
    inCtx('showModal()');
    if (inCtx('$("#modalTitle").textContent') !== '') throw new Error('空标题应清空而非残留');
    inCtx('closeModal()');
    return 'showModal → hidden=false → closeModal → hidden=true';
  }],
  ['弹窗·modalOpen 状态判断', () => {
    inCtx('closeModal()');
    if (inCtx('modalOpen()') !== false) throw new Error('关闭状态下 modalOpen 应为 false');
    inCtx('showModal("x", "y")');
    if (inCtx('modalOpen()') !== true) throw new Error('打开状态下 modalOpen 应为 true');
    inCtx('closeModal()');
    return 'modalOpen 与 hidden 同步';
  }],
  ['视图·价格对比', () => sandbox.viewCompare()],
  ['视图·设置', () => sandbox.viewSettings()],
  ['视图·关于', () => sandbox.viewAbout()],
  ['整体 render()', () => { sandbox.render(); return 'ok'; }],
  /* --- 样式类检查：必须放在 cases 里，抛错才会被逐行捕获成 FAIL，
         而不是在构建断言数组时把整个测试进程带崩 --- */
  ['样式·[hidden] 全局不变式必须在', () => {
    const css = fs.readFileSync(path.join(path.dirname(APP_JS), 'style.css'), 'utf8');
    if (!css.replace(/\s+/g, ' ').includes('[hidden] { display: none !important; }')) {
      throw new Error('style.css 缺少 [hidden]{display:none!important} —— 作者样式里的 ' +
        'display（如 .modal{display:flex}）会压过浏览器默认的 [hidden]，导致弹窗关不掉');
    }
    return '全局 [hidden] 规则存在';
  }],
  ['样式·级联求解（带 hidden 的元素必须隐藏）', () => {
    const rows = hiddenElementDisplays();
    if (!rows.length) throw new Error('index.html 里没找到带 hidden 的元素，检查方式需要更新');
    const bad = rows.filter((r) => r.value !== 'none');
    if (bad.length) {
      throw new Error(bad.map((r) => `${r.label} 带 hidden 但最终 display=${r.value}` +
        `（来自作者样式 ${r.from}）→ 会一直显示且关不掉`).join('；'));
    }
    return rows.map((r) => `${r.label}→none`).join('、');
  }],
];

const pad = (s, n) => String(s).padEnd(n, ' ').slice(0, n);
let failed = 0;
console.log(pad('用例', 22) + pad('结果', 8) + '备注');
console.log('-'.repeat(94));
for (const [label, fn] of cases) {
  try {
    const out = fn();
    const text = typeof out === 'string' ? out : '';
    const idx = text.indexOf('undefined');
    if (idx >= 0) {
      console.log(pad(label, 22) + pad('WARN', 8) + '输出含 undefined：…'
        + text.slice(Math.max(0, idx - 60), idx + 25).replace(/\s+/g, ' '));
      failed++;
      continue;
    }
    console.log(pad(label, 22) + pad('PASS', 8) + (text ? text.length + ' 字符' : ''));
  } catch (e) {
    failed++;
    const where = String((e && e.stack) || '').split('\n')[1] || '';
    console.log(pad(label, 22) + pad('FAIL', 8) + (e && e.message) + ' @ ' + where.trim());
  }
}

/* ══════════════ 4. 内容断言 ══════════════ */
const bp = sandbox.viewBlueprint();
const settings = sandbox.viewSettings();
const about = sandbox.viewAbout();
const jobs = sandbox.viewJobs();
const inv = sandbox.viewInventory();
const planner = sandbox.viewPlanner();
const rs = sandbox.viewResearch();
const pm = sandbox.priceModalHtml(MARKET_TASK, true);

const COLUMNS = ['原料名称', '原料类别', '来源', '生产地点', '平均消耗数量', '可用库存数量',
                 '单件成本', '单价', '总价', '总利润', '利润率', '体积(m³)'];
const checks = [
  ['主界面含总成本卡片', bp.includes('总成本')],
  ['主界面含利润率卡片', bp.includes('利润率')],
  ['主界面含生产设施表单', bp.includes('生产设施') && bp.includes('rigSelect')],
  ['主界面含产品卡 ME/TE/流程输入', bp.includes('data-plan-field="me"')
      && bp.includes('data-plan-field="te"') && bp.includes('data-plan-field="runs"')],
  ['主界面含全部自造/收购按钮', bp.includes('build-all') && bp.includes('purchase-all')],
  ['主界面含刷新材料盘口按钮', bp.includes('market-all')],
  ['材料表含参考站点的全部列', COLUMNS.every((h) => bp.includes(h))],
  ['材料行数据正确（看门人 450）', bp.includes('看门人') && bp.includes('450')],
  ['材料行显示自造/收购状态', bp.includes('toggle-material')],
  ['产品卡显示价格口径提示', bp.includes('部分第三方站点只用材料成本')],
  ['蓝图计算页无 undefined', !bp.includes('undefined')],
  ['设置页已移除 Client ID 输入（已内置）', !settings.includes('setClientId')
      && !settings.includes('setClientSecret') && !settings.includes('save-credentials')],
  ['设置页含材料取整切换', settings.includes('material_rounding')],
  ['设置页含角色管理表', settings.includes('char-refresh')],
  ['关于页含公式与安等说明', about.includes('安等倍率') && about.includes('SCC')],
  ['关于页含环境自检', about.includes('环境自检')],
  ['工业项目页含时间线', jobs.includes('进行中任务时间线')],
  ['工业项目页不再显示「成功/许可」列（用户要求去掉）',
    !jobs.includes('成功/许可') && !jobs.includes('successful_runs')
    && !jobs.includes('licensed_runs')],
  ['工业项目页状态筛选不再列 delivered（已自动清除）',
    !jobs.includes('>delivered<') && !jobs.includes('>cancelled<')],
  ['工业项目页说明会自动清除已交付项目', jobs.includes('会自动清除')],
  ['工业项目页含 ESI 真实费用', jobs.includes('费用(ESI)')],
  ['库存页含物品名', inv.includes('三钛合金')],
  ['生产规划页含采购清单', planner.includes('采购清单')],
  ['生产规划页含逐层任务', planner.includes('逐层任务')],
  ['研究模拟页含蓝图信息', rs.includes('研发等级') && rs.includes('第 1 级研究时长')],
  ['研究模拟页含 ME/TE 等级输入', rs.includes('rsFromMe') && rs.includes('rsFromTe') && rs.includes('rsToTe')],
  ['研究模拟页含安等选择', rs.includes('rsSecurity') && rs.includes('00 / 虫洞 ×2.1')],
  ['研究模拟页已移除改装件选择（改由内核给固定 6 档）', !rs.includes('rsRigSelect')],
  ['研究模拟页含逐级明细入口', rs.includes('research-detail')],
  ['研究结果含各设施对比表', rs.includes('各设施对比') && rs.includes('索迪约 + 屹立超大型实验室优化 I')],
  ['研究结果含 NPC 站基准行', rs.includes('NPC 空间站（无建筑加成）')],
  ['研究结果含相对最快百分比', rs.includes('相对最快') && rs.includes('最快')],
  ['我的蓝图列表含载入按钮', rs.includes('research-load-owned') && rs.includes('载入模拟')],
  ['研究页含依据说明（累计值/以SDE为准）', rs.includes('达到第 n 级的累计值') && rs.includes('为什么以 SDE 为准')],
  ['研究页含零成本指数提示', rs.includes('还没有选择生产星系') || RS_RESULT.cost_index > 0],
  ['顶栏含物价刷新按钮', (() => {
    sandbox.renderTopbar();
    const text = inCtx('$("#priceBtn") ? $("#priceBtn").textContent : ""');
    const cls = inCtx('$("#priceBtn") ? $("#priceBtn").className : ""');
    const title = inCtx('$("#priceBtn") ? $("#priceBtn").title : ""');
    if (!text.includes('物价') || !cls.includes('btn')) {
      throw new Error('顶栏物价按钮未渲染：text=' + text + ' class=' + cls);
    }
    if (!title.includes('上次更新')) throw new Error('物价按钮缺少更新时间提示：' + title);
    return text + ' / ' + title.split('\n')[0];
  })()],
  ['物价按钮·数据过旧时高亮告警', (() => {
    const original = inCtx('S.market.local.prices.updated_at');
    try {
      inCtx('S.market.local.prices.updated_at = Math.floor(Date.now()/1000) - 48*3600');
      sandbox.renderTopbar();
      const cls = inCtx('$("#priceBtn").className');
      const text = inCtx('$("#priceBtn").textContent');
      if (!cls.includes('btn-green') || !text.includes('⚠')) {
        throw new Error('过旧时未高亮：class=' + cls + ' text=' + text);
      }
      inCtx('S.market.local.prices.updated_at = Math.floor(Date.now()/1000)');
      sandbox.renderTopbar();
      const fresh = inCtx('$("#priceBtn").textContent');
      if (!fresh.includes('✓')) throw new Error('新鲜时未显示 ✓：' + fresh);
      return '过旧 ⚠ → 新鲜 ✓';
    } finally {
      inCtx('S.market.local.prices.updated_at = ' + JSON.stringify(original));
      sandbox.renderTopbar();
    }
  })()],
  ['物价弹窗含 5 档刷新范围', pm.includes('只刷新 CCP 调整价')
      && pm.includes('调整价 + 系统成本指数 + 工业设施')
      && pm.includes('当前涉及物品的吉他盘口')
      && pm.includes('历史成交（近 30 天）')
      && pm.includes('全都刷新（盘口 + 历史）')],
  ['物价弹窗含本地已保存数据', pm.includes('本地已保存的数据') && pm.includes('15,988')],
  ['物价弹窗含涉及物品数量', pm.includes('当前涉及')],
  ['物价弹窗含上次刷新结果（含失败项）', pm.includes('上次刷新结果') && pm.includes('部分物品查询失败')],
  ['设置页含本地物价卡片', settings.includes('本地物价数据') && settings.includes('market-local-reload')],
  ['价格对比页显示物价更新时间', sandbox.viewCompare().includes('本地物价更新于')],
  ['物价收集能汇总计划/规划/研究的物品', sandbox.collectMarketItems().length > 0],
  ['设置页含角色关联状态徽标', settings.includes('角色已转手') && settings.includes('登录已失效')],
  ['设置页含「确认已转手」按钮', settings.includes('relink-confirm')
      && settings.includes('确认已转手')],
  ['设置页含「重新登录」入口', settings.includes('data-action="login"')
      && settings.includes('重新登录')],
  ['设置页列出需要处理的角色', settings.includes('需要你处理')],
  ['顶栏角色徽标在异常时告警', (() => {
    sandbox.renderTopbar();
    const cls = inCtx('$("#charChip").className');
    const text = inCtx('$("#charChip").textContent');
    if (!cls.includes('chip-warn') || !text.includes('⚠')) {
      throw new Error('关联异常时顶栏应告警：class=' + cls + ' text=' + text);
    }
    return text;
  })()],
  ['顶部提示条优先显示关联问题', (() => {
    sandbox.renderLoginBanner();
    const box = inCtx('$("#banner")');
    const html = inCtx('$("#banner").innerHTML');
    if (box.hidden) throw new Error('有关联问题时应显示提示条');
    if (!html.includes('需要你确认') || !html.includes('goto-settings')) {
      throw new Error('提示条内容不对：' + html.slice(0, 120));
    }
    return '提示条已显示关联问题';
  })()],
  ['关联状态徽标映射正确', (() => {
    const cases = [
      [{ link_state: 'ok' }, '正常', 'green'],
      [{ link_state: '' }, '正常', 'green'],
      [{ link_state: 'owner_changed', link_state_label: 'x' }, '角色已转手', 'amber'],
      [{ link_state: 'token_dead', link_state_label: 'y' }, '登录已失效', 'red'],
    ];
    for (const [character, text, color] of cases) {
      const html = sandbox.linkStateBadge(character);
      if (!html.includes(text) || !html.includes(color)) {
        throw new Error('徽标映射错误：' + JSON.stringify(character) + ' → ' + html);
      }
    }
    return '4 种状态映射正确';
  })()],
  ['登录弹窗含授权档位选择', (() => {
    const html = sandbox.loginModalHtml({ scopes: new Array(13), tier_label: '授权个人 + 军团数据（推荐）',
      redirect_uri: 'http://localhost:8420/callback', pkce: true });
    for (const want of ['loginTier', '只授权个人数据（最小权限）', '授权个人 + 军团数据（推荐）',
                        '共 8 项权限', '共 13 项权限']) {
      if (!html.includes(want)) throw new Error('登录弹窗缺少：' + want);
    }
    return '两档可选（8 / 13 项）';
  })()],
  ['军团授权缺失时给出可操作提示', (() => {
    if (!sandbox.corpScopeMissing()) throw new Error('应识别出缺军团授权');
    const html = sandbox.corpScopeNotice();
    if (!html.includes('军团数据尚未授权') || !html.includes('relink-corp')) {
      throw new Error('提示内容不对：' + html.slice(0, 120));
    }
    return '提示 + 追加授权按钮';
  })()],
  ['工业项目页显示军团授权提示', jobs.includes('军团数据尚未授权')],
  ['设置页显示军团授权状态', settings.includes('缺军团授权')
      && settings.includes('追加军团授权') && settings.includes('完整')],
  ['scope 审计干净时不显示告警', (() => {
    inCtx('S.selfTest = Object.assign({}, S.selfTest, {scope_audit: {risky: [], clean: true}})');
    inCtx('S.status.config.scope_audit = {clean: true, risky: []}');
    const html = sandbox.scopeAuditWarning();
    if (html !== '') throw new Error('干净时不该有告警：' + html.slice(0, 80));
    return '无告警';
  })()],
  ['scope 审计发现高危权限时告警', (() => {
    const risky = ['esi-mail.send_mail.v1', 'esi-ui.open_window.v1'];
    inCtx('S.selfTest = Object.assign({}, S.selfTest, {scope_audit: {risky: '
      + JSON.stringify(risky) + ', clean: false}})');
    const html = sandbox.scopeAuditWarning();
    if (!html.includes('能代替你做事') || !html.includes('esi-mail.send_mail.v1')
        || !html.includes('esi-ui.open_window.v1')) {
      throw new Error('告警内容不对：' + html.slice(0, 140));
    }
    // 恢复，避免影响后续断言
    inCtx('S.selfTest = Object.assign({}, S.selfTest, {scope_audit: {risky: [], clean: true}})');
    return '2 项高危已标出';
  })()],
  ['等待窗口含手动粘贴入口', (() => {
    const html = sandbox.waitingModalHtml({ scopes: new Array(8), attempts: 1,
      expires_in: 540, redirect_uri: 'http://localhost:8420/callback',
      url: 'https://login.eveonline.com/v2/oauth/authorize?x=1' });
    for (const want of ['正在等待', 'waitCode', 'auth-manual-inline', 'wait-reopen',
                        'wait-cancel', '重新打开授权页']) {
      if (!html.includes(want)) throw new Error('等待窗口缺少：' + want);
    }
    return '含粘贴框 + 重开链接 + 取消';
  })()],
  ['设置页含一键登录按钮与授权范围选择', (() => {
    for (const want of ['data-action="sso-go"', '一键登录 EVE', 'setScopeTier',
                        '只授权个人数据（最小权限）（8 项）',
                        '授权个人 + 军团数据（推荐）（13 项）']) {
      if (!settings.includes(want)) throw new Error('设置页缺少：' + want);
    }
    // 按钮必须真的在「登录 EVE」卡片里，而不是别处
    const cardStart = settings.indexOf('登录 EVE');
    const btnIndex = settings.indexOf('data-action="sso-go"');
    if (cardStart < 0 || btnIndex < 0 || btnIndex < cardStart) {
      throw new Error('一键登录按钮不在「登录 EVE」卡片内');
    }
    // 凭据输入框已整体移除（Client ID 内置），不该再出现
    for (const gone of ['setClientId', 'setClientSecret', 'setCallback', 'save-credentials',
                        'EVE 应用凭据', '查看注册指南']) {
      if (settings.includes(gone)) throw new Error('凭据相关内容应已删除，却还在：' + gone);
    }
    // 原「登录角色」按钮已被一键登录取代，不该重复出现
    if (settings.includes('data-action="login"') && settings.indexOf('data-action="login"') < cardStart) {
      throw new Error('登录卡片里仍有旧的登录按钮');
    }
    return '按钮 + 档位下拉都在登录卡片内，凭据输入已删除';
  })()],
  ['一键登录：一次点击就申请授权 URL 并跳转（跑通整条链路）', () => {
    const EVE_URL = 'https://login.eveonline.com/v2/oauth/authorize?response_type=code'
      + '&client_id=abc&state=xyz&code_challenge_method=S256';
    // api 是函数声明（可替换），post/prepareLogin 都走它 —— 这样分支的真实逻辑仍然在跑，
    // 只是把网络与跳转换成了可观察的桩。比依赖 vm 里 location/fetch 的共享语义确定得多。
    inCtx('window.__calls = []; window.__jump = null;');
    inCtx('api = async (path, opts) => { window.__calls.push(String(path) + "|" + String((opts||{}).body || ""));'
      + ' if (String(path).indexOf("/api/config") === 0) return {ok: true};'
      + ' if (String(path).indexOf("/api/status") === 0) return Object.assign({}, S.status || {}, {ok: true});'
      + ' if (String(path).indexOf("/api/auth/login") === 0) return {ok: true, url: ' + JSON.stringify(EVE_URL)
      + ", tier: 'corporation', tier_label: 'x', scopes: new Array(13), redirect_uri: 'http://localhost:8420/callback'};"
      + ' return {ok: true}; };');
    inCtx('goToSso = (info) => { window.__jump = info && info.url; };');
    inCtx('S.loginInfo = null; S.loginTier = "corporation";');
    inCtx('S.status = Object.assign({}, S.status, {config: Object.assign({}, (S.status||{}).config, {client_id: "abc"})})');
    inCtx('$("#setClientId").value = "abc"');
    inCtx('$("#setScopeTier").value = "corporation"');
    inCtx('$("#setClientSecret").value = ""');
    return clickAction('sso-go').then(() => {
      const calls = inCtx('JSON.stringify(window.__calls)');
      const jump = inCtx('window.__jump');
      if (calls.indexOf('/api/auth/login') < 0) {
        throw new Error('没有申请授权 URL；实际请求=' + calls);
      }
      if (jump !== EVE_URL) {
        throw new Error('没有跳到 EVE 授权页，实际=' + JSON.stringify(jump));
      }
      return '一次点击 → /api/auth/login → ' + String(jump).slice(8, 44) + '…';
    });
  }],
  ['一键登录：界面里没有 Client ID 输入框也能登录（内置）', () => {
    // 凭据输入已整体移除 —— 一键登录必须照样能拿到授权 URL 并跳转
    inCtx('window.__calls = []; window.__jump = null;');
    inCtx('window.__calls = []; window.__jump = null;');
    return clickAction('sso-go').then(() => {
      const jump = inCtx('window.__jump');
      const calls = inCtx('JSON.stringify(window.__calls)');
      if (calls.indexOf('/api/auth/login') < 0) {
        throw new Error('没有请求授权 URL；实际请求=' + calls);
      }
      if (!jump) throw new Error('没有跳转（内置 Client ID 时不该被挡）');
      if (calls.indexOf('/api/config') >= 0) {
        throw new Error('不该再写配置（凭据已内置）：' + calls);
      }
      return '无需输入任何凭据即可登录';
    });
  }],
  ['一键登录：只申请授权 URL，不再写任何配置', () => {
    const EVE_URL = 'https://login.eveonline.com/v2/oauth/authorize?response_type=code&x=1';
    inCtx('window.__calls = []; window.__jump = null;');
    inCtx('api = async (path, opts) => { window.__calls.push(String(path) + "|" + String((opts||{}).body || ""));'
      + ' if (String(path).indexOf("/api/config") === 0) return {ok: true};'
      + ' if (String(path).indexOf("/api/status") === 0) return Object.assign({}, S.status || {}, {ok: true});'
      + ' if (String(path).indexOf("/api/auth/login") === 0) return {ok: true, url: ' + JSON.stringify(EVE_URL)
      + ", tier: 'character', scopes: new Array(8), redirect_uri: 'http://localhost:8420/callback'};"
      + ' return {ok: true}; };');
    inCtx('goToSso = (info) => { window.__jump = info && info.url; };');
    inCtx('S.loginInfo = null;');
    inCtx('$("#setScopeTier").value = "character"');
    return clickAction('sso-go').then(() => {
      const calls = inCtx('JSON.stringify(window.__calls)');
      const jump = inCtx('window.__jump');
      if (calls.indexOf('/api/auth/login') < 0) {
        throw new Error('没有申请授权 URL；实际请求=' + calls);
      }
      if (calls.indexOf('/api/config') >= 0) {
        throw new Error('不该写配置（凭据已内置）：' + calls);
      }
      if (jump !== EVE_URL) throw new Error('没有跳转，实际=' + JSON.stringify(jump));
      return '/api/auth/login → 跳转（未写配置）';
    });
  }],
  ['设施面板：建筑按分组列出并标注尺寸', (() => {
    const html = sandbox.viewFacilityForm();
    for (const want of ['optgroup label="堡垒"', 'optgroup label="工程复合体"',
                        'optgroup label="精炼厂"', '空堡（中型）', '铁壁（大型）',
                        '星城（超大型）', 'data-action="facility-save"']) {
      if (!html.includes(want)) throw new Error('设施面板缺少：' + want);
    }
    return '三组建筑 + 尺寸标注 + 保存按钮';
  })()],
  ['设施面板：改装件按所选建筑的尺寸过滤', (() => {
    // 中型建筑（空堡 35832）→ 只应出现中型改装件
    inCtx('S.facility = Object.assign({}, S.facility, {structure_type_id: 35832, rig_type_ids: []})');
    const medium = sandbox.viewFacilityForm();
    if (!medium.includes('中型')) throw new Error('未标出中型');
    if (medium.includes('<option value="43714"')) {
      throw new Error('空堡（中型）里不该出现大型改装件 43714');
    }
    if (!medium.includes('<option value="37162"')) {
      throw new Error('空堡里应出现中型改装件 37162');
    }
    // 大型建筑（铁壁 35833）→ 反过来
    inCtx('S.facility = Object.assign({}, S.facility, {structure_type_id: 35833, rig_type_ids: []})');
    const large = sandbox.viewFacilityForm();
    if (!large.includes('<option value="43714"')) {
      throw new Error('铁壁（大型）里应出现大型改装件 43714');
    }
    if (large.includes('<option value="37162"')) {
      throw new Error('铁壁里不该出现中型改装件 37162');
    }
    inCtx('S.facility = Object.assign({}, S.facility, {structure_type_id: null})');
    return '空堡只见中型、铁壁只见大型';
  })()],
  ['设施面板：已保存的设施显示摘要与清除入口', (() => {
    inCtx('S.savedFacility = {structure_type_id: 35827, rig_type_ids: [37183],'
      + ' summary: "索迪约 + 屹立超大型实验室优化 I + Jita", saved_at: 1791800000}');
    const html = sandbox.viewFacilityForm();
    if (!html.includes('索迪约 + 屹立超大型实验室优化 I')) {
      throw new Error('没有显示已保存设施的摘要');
    }
    if (!html.includes('data-action="facility-clear"')) throw new Error('缺少清除入口');
    inCtx('S.savedFacility = null');
    return '摘要 + 清除';
  })()],
  ['未登录提示条说明「不影响计算」且不挡路', (() => {
    inCtx('S.status = Object.assign({}, S.status, {characters: [], '
      + 'sso: {configured: true, callback: "http://localhost:8420/callback"}})');
    inCtx('S.bannerDismissed = false');
    sandbox.renderLoginBanner();
    const html = inCtx('$("#banner").innerHTML');
    if (!html.includes('未登录也能用')) {
      throw new Error('提示条没说清未登录也能用：' + html.slice(0, 120));
    }
    if (!html.includes('data-action="login"')) throw new Error('提示条缺少登录按钮');
    // 关键回归：启动时不能再自动弹模态窗盖住界面
    const src = code.slice(code.indexOf('async function maybeAutoLogin'));
    const body = src.slice(0, src.indexOf('function ', 40));
    if (/showModal\(\s*'登录 EVE 角色'/.test(body) && !/if \(auto\)/.test(body)) {
      throw new Error('maybeAutoLogin 仍会在未勾选自动跳转时弹出模态窗');
    }
    return '提示条可见、不弹窗';
  })()],
  ['设施面板：只对别的活动生效的建筑会被标注出来', (() => {
    // "地平线"铁壁：游戏内只写「科研项目的时间需求降低」，制造档下必须标注出来
    const html = sandbox.viewFacilityForm();
    if (!html.includes('地平线')) throw new Error('建筑下拉里缺少地平线');
    if (!html.includes('仅对「researchTime')) {
      throw new Error('没有标注「地平线」只对科研生效');
    }
    // 索迪约本身对制造生效，不该带这个标注
    const sudiyue = sandbox.structureBonusText({
      name: '索迪约', material_factor: 0.99, time_factor: 0.7, cost_factor: 0.95,
      has_own_bonus: true, applies_to_activity: true, activity_scope: '全部活动' });
    if (sudiyue.includes('仅对')) throw new Error('索迪约被误标为仅科研：' + sudiyue);
    if (!sudiyue.includes('时间-30.00%')) throw new Error('索迪约加成文本不对：' + sudiyue);
    return '地平线标为仅科研，索迪约正常';
  })()],
  ['燃料消耗：面板含服务装备选择与读取军团建筑', (() => {
    const html = sandbox.viewFacilityForm();
    for (const want of ['燃料消耗', 'fuelServiceSelect', 'corp-structures-read',
                        '读取军团建筑', '每小时燃料块']) {
      if (!html.includes(want)) throw new Error('燃料面板缺少：' + want);
    }
    return '选择器 + 读取军团建筑按钮都在';
  })()],
  ['燃料消耗：算完后显示每小时/每天/30天与费用', (() => {
    inCtx('S.fuel = {serviceTypeIds: [35878, 35891, 35886], corp: null, message: \'\','
      + ' plan: {fuel_per_hour: 27, fuel_per_day: 648, fuel_per_month: 19440,'
      + ' base_fuel_per_hour: 36, fuel_multiplier: 0.75, structure_fuel_bonus: -25,'
      + ' fuel_scope_names: [\'工程服务装备\'], service_slots: 6,'
      + ' fuel_type_name: \'氮燃料块\', unit_price: 1000, cost_per_month: 19440000,'
      + ' cost_total: 19440000, hours: 720, fuel_blocks_per_unit: 1,'
      + ' unknown_service_ids: [], notes: [], mechanics: []}}');
    const html = sandbox.viewFacilityForm();
    for (const want of ['27.00', '648.0', '19,440', '-25%', '氮燃料块', '工程服务装备',
                        '原始 36.00/小时']) {
      if (!html.includes(want)) throw new Error('燃料结果缺少：' + want);
    }
    // 不再显示「服务上线一次性燃料」（用户要求只算持续消耗）
    if (html.includes('上线一次性')) throw new Error('不该再显示上线一次性燃料');
    // 未选服务装备时不该显示结果
    inCtx('S.fuel.serviceTypeIds = []; S.fuel.plan = null;');
    const empty = sandbox.viewFacilityForm();
    if (empty.includes('19,440')) throw new Error('没选服务装备却显示了消耗量');
    return '结果与费用都会显示，清空后消失';
  })()],
  ['燃料消耗：服务装备标注是否享减免（按分类）', (() => {
    // 阿兹贝尔=工程复合体：制造厂（工程）打折、克隆中心（堡垒）不打折
    inCtx('S.fuel = {serviceTypeIds: [], fuelTypeId: 4051, corp: null, message: "", plan: null}');
    const factory = sandbox.serviceDiscountHint({type_id: 35878, name: '屹立制造厂 I',
      category: '工程服务装备', in_fuel_scope: true, fuel_per_hour: 12});
    const clone = sandbox.serviceDiscountHint({type_id: 35894, name: '屹立克隆中心 I',
      category: '堡垒服务装备', in_fuel_scope: false, fuel_per_hour: 10});
    if (!factory.includes('✓ 减免后')) throw new Error('打折的服务装备没标出减免后数值：' + factory);
    if (!clone.includes('不适用减免')) throw new Error('不打折的服务装备没标出：' + clone);
    return '工程服务标减免后值、堡垒服务标不适用';
  })()],
  ['分类浏览：能返回分类树，且空列表不再挤住分类', (() => {
    /* 用户报的 bug：进了分类**没有返回入口**，而且下面的分类列表被「卡住」。
       后者的真因是 #searchResults 与 #categoryTree 都是 flex:1 —— 空的那个
       照样占掉一半高度。所以两边都必须显式切换。 */
    inCtx('S.searchResults = [{type_id: 1, name: "x"}]; S.browsingCategory = "测试分类";');
    inCtx('$("#categoryTree").style.display = ""; $("#searchResults").style.display = "";');
    inCtx('showCategoryTree()');
    if (inCtx('$("#categoryTree").style.display') !== '') throw new Error('返回后分类树应可见');
    if (inCtx('$("#searchResults").style.display') !== 'none') {
      throw new Error('返回后搜索结果容器应收起（空着会挤住分类列表）');
    }
    if (inCtx('S.searchResults.length') !== 0) throw new Error('返回后应清空搜索结果');
    const src = require('fs').readFileSync(APP_JS, 'utf8');
    // 现在是游戏那样的市场分组树：节点用 data-mg，返回用 mg-back
    if (!src.includes('data-mg=') || !/case 'mg-back'/.test(src)) {
      throw new Error('缺少市场树节点/返回处理（data-mg / mg-back）');
    }
    if (!src.includes("closest('[data-mg]')")) throw new Error('点击市场树节点没有处理分支');
    if (!src.includes('renderMarketNodes') || !src.includes('renderMarketItems')) {
      throw new Error('缺少市场树渲染函数');
    }
    return '返回按钮 + 空列表收起都在（市场树）';
  })()],
  ['市场树：物品行带出「蓝图原本」并可直接送去研究模拟', (() => {
    /* 用户要求：所有物品增加蓝图原本；研究模拟可以在所有物品里选中对应蓝图后模拟，
       且不依赖 ESI。数据来自本地 SDE（activity_products 的制造配方）。 */
    const src = require('fs').readFileSync(APP_JS, 'utf8');
    if (!src.includes('data-research-bp=')) throw new Error('物品行没有「研究蓝图」入口');
    if (!src.includes("closest('[data-research-bp]')")) throw new Error('研究蓝图点击没有处理分支');
    if (!/蓝图原本/.test(src)) throw new Error('界面没有把蓝图原本显示出来');
    // 渲染一只带蓝图的产品，确认按钮与名称都出现
    inCtx('S.marketPath = [];');
    sandbox.renderMarketItems([{ type_id: 582, name: '矮脚鸡级', category_id: 6,
      blueprint_type_id: 583, blueprint_name: '矮脚鸡级蓝图', group_id: 1 }], '加达里');
    const html = inCtx('$("#searchResults").innerHTML') || '';
    if (!html.includes('data-research-bp="583"')) throw new Error('没有渲染研究蓝图按钮：' + html.slice(0, 120));
    if (!html.includes('矮脚鸡级蓝图')) throw new Error('没有显示蓝图原本名称');
    if (!html.includes('images.evetech.net')) throw new Error('物品行没有图标');
    return '蓝图原本 + 研究入口都在';
  })()],
  ['改装件候选为空时给出补救入口（而不是静默空下拉）', (() => {
    const keep = inCtx('JSON.stringify(S.rigs)');
    inCtx('S.rigs = [];');
    inCtx('S.facility = Object.assign({}, S.facility, {structure_type_id: 35827, rig_type_ids: []})');
    const empty = sandbox.viewFacilityForm();
    if (!empty.includes('data-action="rigs-reload"')) {
      throw new Error('候选为空时应有「重新载入改装件数据」按钮');
    }
    if (!empty.includes('选了也没用')) throw new Error('应说明这个下拉现在是空的');
    inCtx('S.rigs = ' + keep + ';');
    if (sandbox.viewFacilityForm().includes('data-action="rigs-reload"')) {
      throw new Error('候选正常时不该显示补救按钮');
    }
    return '空候选有补救按钮 + 说明；正常时不显示';
  })()],
  ['设施计费输入框有最小宽度（避免 0.0025 被截成 0.002）', (() => {
    const css = require('fs').readFileSync(
      require('path').join(require('path').dirname(APP_JS), 'style.css'), 'utf8');
    if (!/\.facility input/.test(css) || !/min-width:\s*9\dpx/.test(css)) {
      throw new Error('style.css 缺少 .facility input 的最小宽度规则');
    }
    return '已有 min-width 规则';
  })()],
  ['图标：普通物品用 icon、蓝图类有 bp 兜底', (() => {
    /* 实测 images.evetech.net：普通物品 /icon=200 而 /bp=400；
       蓝图类反过来（/icon=400、/bp=200）。所以必须有兜底，
       否则蓝图（尤其拷贝/研究任务）就整块空白 —— 用户报过「工业项目没有图标」。 */
    const normal = sandbox.typeIconHtml(587, 32, 'item-icon');
    if (!normal.includes('images.evetech.net/types/587/icon?size=32')) {
      throw new Error('没有用 icon 变体：' + normal);
    }
    if (!normal.includes('data-bp="https://images.evetech.net/types/587/bp?size=32"')) {
      throw new Error('缺少 bp 兜底地址：' + normal);
    }
    if (!normal.includes('onerror=')) throw new Error('缺少 onerror 兜底处理：' + normal);
    if (!normal.includes('class="item-icon"')) throw new Error('class 没带上：' + normal);
    if (sandbox.typeIconHtml(null, 32) !== '') throw new Error('空 id 应返回空字符串');
    if (sandbox.typeIconHtml(undefined, 32) !== '') throw new Error('undefined 应返回空字符串');
    return 'icon + bp 兜底 + onerror 都在';
  })()],
  ['工业项目：拷贝/研究任务没有产物也要有图标', (() => {
    /* 这类任务 product_type_id 为 null（没有产物），以前整行不渲染 <img>。
       现在应从源码层面保证：图标 id 用 product_type_id || blueprint_type_id，
       名称也在没有产物时回退到蓝图名。 */
    const src = require('fs').readFileSync(APP_JS, 'utf8');
    if (!/typeIconHtml\(row\.product_type_id \|\| row\.blueprint_type_id/.test(src)) {
      throw new Error('工业项目行的图标没有回退到 blueprint_type_id');
    }
    if (!/esc\(row\.product_name \|\| row\.blueprint_name/.test(src)) {
      throw new Error('工业项目行没有产物时没有回退显示蓝图名');
    }
    // 除了 typeIconHtml 自己内部那一处，不该再有裸 iconUrl（蓝图会变成空白图）
    const bare = (src.match(/src="\$\{iconUrl\(/g) || []).length;
    if (bare !== 1) {
      throw new Error('裸 iconUrl 应只剩 typeIconHtml 内部 1 处，实际 ' + bare + ' 处');
    }
    if (!/function typeIconHtml\(/.test(src)) throw new Error('缺少 typeIconHtml');
    return '图标 id 与名称都会回退到蓝图，且没有多余的裸 iconUrl';
  })()],
  ['添加第二个角色：角色下拉选「+ 登录新角色」会真的发起授权', () => {
    /* 回归：这里以前去点 [data-action="login"]，而那个按钮在 v0.8.8 换成
       「登录 EVE」卡片时就被删了 —— 于是「添加第二个角色」这条路径什么都不做。 */
    const EVE_URL = 'https://login.eveonline.com/v2/oauth/authorize?response_type=code&client_id=abc';
    inCtx('window.__calls = []; window.__jump = null;');
    inCtx('api = async (path, opts) => { window.__calls.push(String(path) + "|" + String((opts||{}).body || ""));'
      + ' if (String(path).indexOf("/api/auth/login") === 0) return {ok: true, url: ' + JSON.stringify(EVE_URL)
      + ", tier: 'corporation', scopes: new Array(13), redirect_uri: 'http://localhost:8420/callback'};"
      + ' return {ok: true}; };');
    inCtx('goToSso = (info) => { window.__jump = info && info.url; };');
    return changeSelect('charSelect', '__login').then(() => {
      const calls = inCtx('JSON.stringify(window.__calls)');
      const jump = inCtx('window.__jump');
      if (calls.indexOf('/api/auth/login') < 0) {
        throw new Error('选「+ 登录新角色」没有申请授权 URL；实际请求=' + calls);
      }
      if (jump !== EVE_URL) throw new Error('没有跳转到授权页，实际=' + JSON.stringify(jump));
      if (inCtx('$("#charSelect").value') === '__login') {
        throw new Error('触发后应把下拉重置，避免停留在「+ 登录新角色」');
      }
      return '选「+ 登录新角色」→ 申请授权 URL → 跳转';
    });
  }],
  ['页面版本与程序版本不一致时会明确提示刷新', (() => {
    /* 为什么需要：换了新版本但浏览器还跑着旧 app.js 时，用户会看到旧行为，
       只会觉得「我明明更新了怎么还是旧的」。这条提示把它说清楚。 */
    const src = require('fs').readFileSync(APP_JS, 'utf8');
    if (!/meta\[name="app-version"\]/.test(src)) throw new Error('没有读取页面版本 meta');
    if (!/function pageVersionMismatch\(/.test(src)) throw new Error('缺少 pageVersionMismatch()');
    // 版本一致时不提示、不一致时提示（pageVersion 参数用于在 DOM 桩里注入）
    inCtx('S.status = Object.assign({}, S.status, {app: Object.assign({}, (S.status||{}).app, {version: "0.8.11"})});');
    if (inCtx('pageVersionMismatch("0.8.11")') !== null) {
      throw new Error('版本一致时不该报不一致');
    }
    if (inCtx('pageVersionMismatch("")') !== null) {
      throw new Error('页面版本未知时不该报不一致');
    }
    const mm = inCtx('JSON.stringify(pageVersionMismatch("0.8.7"))');
    if (!mm || mm === 'null') throw new Error('版本不一致时应该报出来：' + mm);
    inCtx('PAGE_VERSION_TEST = "0.8.7";');
    sandbox.renderLoginBanner();
    const html = inCtx('$("#banner").innerHTML') || '';
    if (html.includes('旧版本')) throw new Error('DOM 桩里页面版本为空时不该显示旧版本提示');
    return '版本一致/未知时不提示；不一致时 pageVersionMismatch 能报出来';
  })()],
  ['库存：切换「查看范围」会真的按新范围重新取数', () => {
    /* 回归：以前这里只改 S.inventory.scope，既不请求也不重绘 ——
       表现就是「库存页面切不了角色」。 */
    inCtx('window.__calls = [];');
    inCtx('api = async (path, opts) => { window.__calls.push(String(path)); return {ok: true, rows: []}; };');
    inCtx('S.view = "inventory";');
    return changeSelect('invScope', 'corp:987654').then(() => {
      const calls = inCtx('JSON.stringify(window.__calls)');
      if (calls.indexOf('/api/inventory') < 0) {
        throw new Error('切换查看范围没有重新取库存数据；实际请求=' + calls);
      }
      if (calls.indexOf('corp%3A987654') < 0 && calls.indexOf('corp:987654') < 0) {
        throw new Error('请求里没有带上新的范围：' + calls);
      }
      return '切范围 → 重新请求库存';
    });
  }],
  ['换角色后会重新拉取当前视图的数据（不是只重画旧数据）', () => {
    /* 说明：app.js 里 post/api 是 const，测试用「重定义 api」的方式拦请求
       （post 内部也走 api）。注意这条必须自己定义桩 —— 上一条用例也会改 api。 */
    inCtx('window.__calls = [];');
    inCtx('api = async (path, opts) => { window.__calls.push(String(path));'
      + ' if (String(path).indexOf("/api/jobs") === 0) return {ok: true, jobs: [], summary: {}};'
      + ' return {ok: true}; };');
    inCtx('S.view = "jobs";');
    return changeSelect('charSelect', '2116614309').then(() => {
      const calls = inCtx('JSON.stringify(window.__calls)');
      if (calls.indexOf('/api/auth/active') < 0) {
        throw new Error('没有切换激活角色；实际请求=' + calls);
      }
      if (calls.indexOf('/api/jobs') < 0) {
        throw new Error('换角色后没有重新拉取工业项目（只会重画旧角色的数据）：' + calls);
      }
      return '换角色 → /api/auth/active → 重新拉取当前视图数据';
    });
  }],
];
/* 内容断言：支持同步值与 Promise（异步断言要 await，否则 Promise 恒为真、断言空转） */
(async () => {
  let bad = 0;
  console.log('');
  console.log('关键内容断言：');
  for (const [label, ok] of checks) {
    let value = false;
    let why = '';
    try {
      value = typeof ok === 'function' ? await ok() : await ok;
    } catch (err) {
      value = false;
      why = '  ← ' + String((err && err.message) || err).slice(0, 90);
    }
    if (!value) bad++;
    const detail = (typeof value === 'string') ? '  ' + value : '';
    console.log('  ' + pad(label, 34) + (value ? 'PASS' : 'FAIL') + detail + (value ? '' : why));
  }
  console.log('');
  console.log('渲染用例失败/告警 ' + failed + ' 项，内容断言失败 ' + bad + ' 项');
  process.exit(failed + bad === 0 ? 0 : 1);
})();
