"""工业计算核心。

公式已逐项对照官方 SDE 属性与公开实测数据核实，来源标注在每条注释里。

────────────────────────────────────────────────────────────────
材料数量（实测：向上取整，且取整发生在乘完流程数之后）
    raw   = 基准量 × 流程数 × (1 - ME/100)
            × Π(结构材料倍率 2600)
            × Π(1 + rig材料减免 2594 × 安等倍率 / 100)
    数量  = max(流程数, ceil(raw))            ← 每种材料每流程至少 1 个
    反应（reaction）不受 ME 影响，但仍受建筑/rig 影响。

实测对账（Raitaru 2600=0.99 + rig 37180 的 2594=-2）：
    32000 × 0.99 × 0.98 = 31046.4 → ceil = 31047 ✔

────────────────────────────────────────────────────────────────
任务时间（实测：技能、蓝图 TE、结构、rig 全部相乘）
    time = 基础时间 × 流程数
           × (1 - TE/100)
           × Π(结构时间倍率 2602，反应用 2721)
           × Π(1 + rig时间减免 2593 × 安等倍率 / 100)
           × Π(1 - 技能每级减免 × 等级)

实测对账（Raitaru 2602=0.85 + rig 37180 的 2593=-20，技能 5/5 级）：
    6000 × 0.80 × 0.85 × 0.85 × 0.80 = 2774 秒 ✔

────────────────────────────────────────────────────────────────
估算物品价值 EIV（实测：与 ME、建筑、rig 全都无关，用 ME0 基准量）
    EIV = Σ( 基准材料量 × 流程数 × adjusted_price )   ← 取整到整数 ISK

────────────────────────────────────────────────────────────────
任务费用（实测对账：系统费用 - 减免 + 设施税 + SCC = 总费用）
    计费基数 = EIV                        （制造 / 反应）
             = EIV × 2%                   （拷贝 / 发明）
             = EIV × 2% × 研究秒数 / 105   （ME/TE 研究）
    系统费用 = 计费基数 × 成本指数
    费用减免 = 系统费用 × (1 - Π(结构费用倍率 2601) × Π(1 + rig费用减免 2595 × 安等 / 100))
    SCC 附加 = 计费基数 × 4%              ← 现行 4%（2024-02-01 起）
    设施税   = 计费基数 × 税率            ← NPC 空间站 0.25%，玩家建筑上限 10%
    Alpha 税 = 计费基数 × 0.25%（仅 Alpha 角色）
    总费用   = 系统费用 - 费用减免 + SCC + 设施税 + Alpha税

现有任务的真实费用以 ESI 返回的 cost 字段为准，界面里两者并列显示。
"""

from __future__ import annotations

import json
import math

from . import config as cfgmod
from . import utils


# --------------------------------------------------------------------------
# 安等倍率
# --------------------------------------------------------------------------

def security_context(store, system_id=None, override: str = None) -> dict:
    """确定生产星系的安等与改装件加成倍率（高安 1.0 / 低安 1.9 / 00 与虫洞 2.1）。"""
    status = None
    name = None
    if system_id:
        row = store.system_name(int(system_id))
        if row:
            status = row.get("security_status")
            name = row.get("name")
    if override in ("highsec", "lowsec", "nullsec"):
        kind = override
    elif status is None:
        kind = "highsec"
    elif status >= 0.45:
        kind = "highsec"
    elif status > 0.0:
        kind = "lowsec"
    else:
        kind = "nullsec"
    multiplier = {"highsec": 1.0, "lowsec": 1.9, "nullsec": 2.1}[kind]
    labels = {"highsec": "高安", "lowsec": "低安", "nullsec": "00/虫洞"}
    return {"system_id": system_id, "system_name": name, "security_status": status,
            "class": kind, "label": labels[kind], "multiplier": multiplier}


# --------------------------------------------------------------------------
# 加成作用目标（filterID）
# --------------------------------------------------------------------------

def product_matches_filter(store, filter_row: dict, category_id=None, group_id=None,
                           type_id=None) -> bool:
    """判断产品是否落在某个 filter 的目标范围内。"""
    if not filter_row:
        return True
    try:
        categories = json.loads(filter_row.get("category_ids") or "[]")
        groups = json.loads(filter_row.get("group_ids") or "[]")
        types = json.loads(filter_row.get("type_ids") or "[]")
    except Exception:
        return True
    if not categories and not groups and not types:
        return True
    if type_id and type_id in types:
        return True
    if group_id and group_id in groups:
        return True
    if category_id and category_id in categories:
        return True
    return False


# --------------------------------------------------------------------------
# 设施加成
# --------------------------------------------------------------------------

def facility_bonuses(store, structure_type_id=None, rig_type_ids=None,
                     activity: str = "manufacturing", system_id=None,
                     security_override: str = None, manual: dict = None,
                     product_category_id=None, product_group_id=None,
                     product_type_id=None) -> dict:
    """算出结构与改装件带来的材料/时间/费用倍率。

    返回的 material_factor / time_factor / cost_factor 是**直接相乘的倍率**
    （1.0 = 无影响），material / time / cost 是等价的「减免比例」便于显示。
    """
    modifier_activity = cfgmod.ACTIVITY_TO_MODIFIER.get(activity, "manufacturing")
    rig_type_ids = [int(i) for i in (rig_type_ids or []) if i]
    all_ids = ([int(structure_type_id)] if structure_type_id else []) + rig_type_ids
    bonus_map = store.bonus_attributes_many(all_ids) if all_ids else {}
    names = store.type_names(all_ids) if all_ids else {}
    attr_names = {r["attribute_id"]: (r.get("name") or r.get("display_name") or "")
                  for r in store.dogma_attribute_names(
                      sorted(cfgmod.NEEDED_DOGMA_ATTRIBUTES)).values()}

    security = security_context(store, system_id, security_override)
    factors = {"material": 1.0, "time": 1.0, "cost": 1.0}
    sources = []
    notes = []
    rig_groups = {}

    # ---------------- 结构自身 ----------------
    if structure_type_id:
        attrs = bonus_map.get(int(structure_type_id)) or {}
        entry = {"role": "structure", "type_id": int(structure_type_id),
                 "name": names.get(int(structure_type_id)) or ("类型 %s" % structure_type_id),
                 "kind_factors": {}, "details": []}
        # **活动过滤**：SDE 的 industryModifierSources 声明了「这个建筑对哪些活动生效」。
        # 势力铁壁就是典型例子（已与游戏内描述逐条核对）：
        #   莫罗 47512   → 全部 5 个活动（生产 + 科研）
        #   德拉库斯 47513 → **只有 manufacturing**
        #   地平线 47514 → **只有科研类**（拷贝 / 发明 / ME / TE 研究），没有 manufacturing
        # 不做这个过滤，就会给「地平线」算制造加成、给「德拉库斯」算科研加成 —— 都是错的。
        # 规则：只要该建筑在表里出现过，就必须有匹配的 (活动, 类型) 声明才生效；
        # 表里完全没出现过的建筑保持宽松（避免因 SDE 缺数据而突然少算）。
        struct_rules = store.modifier_rules([int(structure_type_id)], modifier_activity)
        declared_kinds = {r["kind"] for r in struct_rules}
        any_activity_rows = bool(store.q1(
            "SELECT COUNT(*) n FROM industry_modifier_sources WHERE type_id=?",
            (int(structure_type_id),))["n"])
        for attribute_id, (kind, attr_name, label) in cfgmod.BONUS_ATTRIBUTES.items():
            if attribute_id == 2721 and activity != "reaction":
                continue
            if attribute_id == 2602 and activity == "reaction":
                continue
            if attribute_id not in attrs:
                continue
            if any_activity_rows and kind not in declared_kinds:
                notes.append("建筑「%s」的%s加成不适用于当前活动（%s），已跳过。"
                             % (entry["name"], label, activity))
                continue
            value = utils.num(attrs[attribute_id], 1.0)
            if value <= 0:
                continue
            factors[kind] *= value
            entry["kind_factors"][kind] = value
            entry["details"].append({
                "kind": kind, "attribute_id": attribute_id, "attribute_name": attr_name,
                "label": label, "value": value, "reduction": 1.0 - value,
                "mulitplier_source": "结构自身",
            })
        if entry["details"]:
            sources.append(entry)

    # ---------------- 改装件 ----------------
    def rig_efficiency(attrs: dict, kind: str):
        for attribute_id, (attr_kind, attr_name, label) in cfgmod.RIG_BONUS_ATTRIBUTES.items():
            if attr_kind != kind:
                continue
            if attribute_id in attrs:
                return attribute_id, attr_name, label, utils.num(attrs[attribute_id])
        return None, None, None, 0.0

    def rig_security_multiplier(attrs: dict) -> tuple:
        if security_override == "highsec" and 2355 in attrs:
            return utils.num(attrs.get(2355), 1.0), "高安"
        if security_override == "lowsec" and 2356 in attrs:
            return utils.num(attrs.get(2356), 1.0), "低安"
        if security_override == "nullsec" and 2357 in attrs:
            return utils.num(attrs.get(2357), 1.0), "00/虫洞"
        if system_id and 2355 in attrs:
            if security["class"] == "highsec":
                return utils.num(attrs.get(2355), 1.0), "高安"
            if security["class"] == "lowsec":
                return utils.num(attrs.get(2356), 1.0), "低安"
            return utils.num(attrs.get(2357), 1.0), "00/虫洞"
        return 1.0, "高安(默认)"

    for type_id in rig_type_ids:
        attrs = bonus_map.get(type_id) or {}
        if not attrs:
            notes.append("改装件「%s」在 SDE 里没有加成属性，已忽略。"
                         % (names.get(type_id) or type_id))
            continue
        # 记录改装件分组，用于「同组只能装一个」提示
        row = store.q1("SELECT group_id FROM types WHERE type_id=?", (type_id,))
        if row and row["group_id"]:
            rig_groups.setdefault(row["group_id"], []).append(names.get(type_id) or str(type_id))
        sec_mult, sec_label = rig_security_multiplier(attrs)
        entry = {"role": "rig", "type_id": type_id, "name": names.get(type_id) or str(type_id),
                 "kind_factors": {}, "details": [],
                 "security_multiplier": sec_mult, "security_label": sec_label}
        rules = store.modifier_rules([type_id], modifier_activity)
        for kind in ("material", "time", "cost"):
            attribute_id, attr_name, label, percent = rig_efficiency(attrs, kind)
            if attribute_id is None or not percent:
                continue
            kind_rules = [r for r in rules if r["kind"] == kind]
            if not kind_rules:
                continue
            # 作用目标过滤：产品不在目标范围内就不吃这条加成
            filters = {r["filter_id"] for r in kind_rules if r.get("filter_id")}
            applicable = True
            matched_names = []
            if filters and (product_category_id or product_group_id or product_type_id):
                filter_rows = store.target_filters(sorted(filters))
                applicable = False
                for filter_id in sorted(filters):
                    frow = filter_rows.get(filter_id)
                    if product_matches_filter(store, frow, product_category_id,
                                              product_group_id, product_type_id):
                        applicable = True
                        if frow:
                            matched_names.append(frow.get("name") or str(filter_id))
                if not applicable:
                    notes.append("改装件「%s」的%s加成不适用于当前产品，已跳过。"
                                 % (entry["name"], label))
                    continue
            factor = 1.0 + (percent * sec_mult) / 100.0
            factors[kind] *= factor
            entry["kind_factors"][kind] = factor
            entry["details"].append({
                "kind": kind, "attribute_id": attribute_id, "attribute_name": attr_name,
                "label": label, "base_percent": percent, "security_multiplier": sec_mult,
                "security_label": sec_label, "factor": factor,
                "reduction": 1.0 - factor, "targets": matched_names,
                "mulitplier_source": "改装件",
            })
        if entry["details"]:
            sources.append(entry)

    # ---------------- 手动覆盖 ----------------
    overrides = {}
    for kind in ("material", "time", "cost"):
        value = (manual or {}).get(kind)
        if value:
            factors[kind] = max(0.0, 1.0 - utils.clamp(utils.num(value), 0.0, 0.99))
            overrides[kind] = factors[kind]

    if len(rig_type_ids) > 3:
        notes.append("一个工业建筑最多安装 3 个改装件，当前选了 %d 个。" % len(rig_type_ids))
    for group_id, items in rig_groups.items():
        if len(items) > 1:
            notes.append("同一分组的改装件只能装一个，但你选了：%s。" % "、".join(items))

    return {
        "material_factor": factors["material"],
        "time_factor": factors["time"],
        "cost_factor": factors["cost"],
        "material": 1.0 - factors["material"],
        "time": 1.0 - factors["time"],
        "cost": 1.0 - factors["cost"],
        "sources": sources,
        "security": security,
        "overrides": overrides,
        "notes": notes,
        "modifier_activity": modifier_activity,
    }


def industry_structures(store, activity: str = "manufacturing") -> list:
    """**玩家可部署的全部工业建筑**（不只是自带加成的那些）。

    为什么要给全量：空堡 / 铁壁 / 星城 / 阿塔诺本身没有工业加成，
    但它们能装改装件，而改装件才是主要加成来源。只列「自带加成」的建筑
    会让这些建筑从下拉里消失（用户报过这个问题）。

    尺寸匹配依据：建筑与改装件都带属性 1547 rigSize，**两者相等才能安装**。

    **活动相关性**：SDE 的 industryModifierSources 声明了每个建筑对哪些活动生效。
    势力铁壁正是靠这个区分的（已与游戏内描述逐条核对）：
      "莫罗"铁壁     → 生产 + 科研（5 个活动全给）
      "德拉库斯"铁壁 → **只有生产**（manufacturing）
      "地平线"铁壁   → **只有科研**（拷贝 / 发明 / ME / TE 研究）
      "马金尼斯"/"普罗米修斯" → 没有生产加成（只有提炼与燃料）
    所以 `material_factor/time_factor/cost_factor` 是**针对 activity 这一档**算的，
    另给 `applies_to_activity` 让界面能提示「这个建筑对本活动没有加成」。
    """
    modifier_activity = cfgmod.ACTIVITY_TO_MODIFIER.get(activity, "manufacturing")
    groups = tuple(cfgmod.INDUSTRY_STRUCTURE_GROUPS)
    rows = store.q(
        "SELECT t.type_id, t.name, t.group_id, t.published FROM types t"
        " WHERE t.group_id IN (%s) AND t.published = 1" % ",".join("?" * len(groups)),
        groups)
    if not rows:
        return []
    ids = [r["type_id"] for r in rows]
    attrs_by_id = store.bonus_attributes_many(ids)
    # 每个建筑声明的 (活动, 类型) —— 活动过滤的依据
    sources = {}
    for row in store.q(
            "SELECT DISTINCT type_id, activity, kind FROM industry_modifier_sources"):
        sources.setdefault(row["type_id"], set()).add((row["activity"], row["kind"]))
    out = []
    for row in rows:
        attrs = attrs_by_id.get(row["type_id"]) or {}
        rig_size = utils.as_int(utils.num(attrs.get(cfgmod.RIG_SIZE_ATTRIBUTE)), 0)
        if rig_size < 2:
            # rigSize < 2 的不是可装改装件的工业建筑（例如服务装备）
            continue
        declared = sources.get(row["type_id"], set())
        declared_activities = sorted({a for a, _k in declared})
        declared_kinds = {k for a, k in declared if a == modifier_activity}
        # 表里出现过就必须有匹配声明；从未出现则宽松处理（避免 SDE 缺数据时少算）
        filter_by_activity = bool(declared)
        own = {}
        for attribute_id, (kind, _n, _l) in cfgmod.BONUS_ATTRIBUTES.items():
            if attribute_id not in attrs:
                continue
            if attribute_id == 2721 and activity != "reaction":
                continue
            if attribute_id == 2602 and activity == "reaction":
                continue
            if filter_by_activity and kind not in declared_kinds:
                continue
            own[kind] = utils.num(attrs.get(attribute_id), 1.0)
        group_id = row["group_id"]
        out.append({
            "type_id": row["type_id"],
            "name": row["name"],
            "group_id": group_id,
            "group_name": cfgmod.INDUSTRY_STRUCTURE_GROUPS.get(group_id, ""),
            "rig_size": rig_size,
            "rig_size_label": cfgmod.RIG_SIZE_LABELS.get(rig_size, str(rig_size)),
            "rig_slots": utils.as_int(utils.num(attrs.get(
                cfgmod.STRUCTURE_RIG_SLOTS_ATTRIBUTE)), cfgmod.STRUCTURE_RIG_SLOT_LIMIT),
            "upgrade_capacity": utils.num(attrs.get(
                cfgmod.STRUCTURE_UPGRADE_CAPACITY_ATTRIBUTE), 0.0),
            "material_factor": own.get("material", 1.0),
            "time_factor": own.get("time", 1.0),
            "cost_factor": own.get("cost", 1.0),
            "reaction_time_factor": own.get("time", 1.0) if activity == "reaction" else 1.0,
            # 「自带加成」= 带结构倍率属性（不问活动），用于统计与兼容旧口径
            "has_own_bonus": any(a in attrs for a in (2600, 2601, 2602, 2721)),
            # 「对本活动有加成」= 活动过滤后确实算出了倍率
            "applies_to_activity": bool(own),
            "own_attribute_ids": sorted(
                a for a in (2600, 2601, 2602, 2721) if a in attrs),
            "declared_activities": declared_activities,
            "activity_scope": ("全部活动" if len(declared_activities) >= 5
                               else "、".join(declared_activities) or "无"),
        })
    # 排序：分组（堡垒→工程复合体→精炼厂）→ 尺寸 → 名称
    order = {gid: i for i, gid in enumerate(cfgmod.INDUSTRY_STRUCTURE_GROUPS)}
    out.sort(key=lambda x: (order.get(x["group_id"], 99), x["rig_size"], x["name"] or ""))
    return out


def structures_for_rig_size(structures, rig_size: int) -> list:
    """能装该尺寸改装件的建筑。"""
    return [s for s in (structures or []) if int(s.get("rig_size") or 0) == int(rig_size or 0)]


def service_modules(store, structure_type_id=None, activity: str = None) -> list:
    """可装在建筑上的**服务装备**及其燃料消耗（SDE 属性驱动）。

    数据来源：
      * 2109 serviceModuleFuelAmount        「每小时开始时消耗的数量」= 每小时燃料块数
      * 2110 serviceModuleFuelOnlineAmount  上线时一次性消耗
      * 2108 serviceModuleFuelConsumptionGroup 消耗的燃料分组（1136 = 燃料块）
    只有带 2109 的类型才是真正烧燃料的服务装备（宣传屏、瞭望阵列、主权设施等不烧燃料）。
    """
    rows = store.q(
        "SELECT t.type_id, t.name, t.group_id, g.name AS group_name, t.market_group_id,"
        " mg.name AS market_group_name"
        " FROM types t LEFT JOIN groups g ON g.group_id = t.group_id"
        " LEFT JOIN groups mg ON mg.group_id = t.market_group_id"
        " WHERE t.published = 1 AND t.type_id IN"
        "   (SELECT DISTINCT type_id FROM type_bonus_attributes WHERE attribute_id IN (?, ?))"
        " ORDER BY t.market_group_id, t.name",
        (cfgmod.SERVICE_FUEL_HOURLY_ATTRIBUTE, cfgmod.SERVICE_FUEL_ONLINE_ATTRIBUTE))
    if not rows:
        return []
    ids = [r["type_id"] for r in rows]
    attrs_by_id = store.bonus_attributes_many(ids)
    slots = 0
    if structure_type_id:
        sattrs = store.bonus_attributes_many([int(structure_type_id)]).get(
            int(structure_type_id)) or {}
        slots = utils.as_int(utils.num(sattrs.get(
            cfgmod.STRUCTURE_SERVICE_SLOTS_ATTRIBUTE)), 0)
    scope = cfgmod.fuel_scope_for(store, structure_type_id)
    hints = cfgmod.ACTIVITY_SERVICE_HINTS.get(activity or "", ())
    out = []
    for row in rows:
        attrs = attrs_by_id.get(row["type_id"]) or {}
        hourly = utils.num(attrs.get(cfgmod.SERVICE_FUEL_HOURLY_ATTRIBUTE), 0.0)
        if hourly <= 0:
            continue
        name = row["name"] or ""
        market_group = utils.as_int(row["market_group_id"]) or 0
        out.append({
            "type_id": row["type_id"],
            "name": name,
            "group_id": row["group_id"],
            "group_name": row["group_name"] or "",
            # 分类按**市场分组**走（与游戏里的服务装备树一致）
            "market_group_id": market_group,
            "category": cfgmod.SERVICE_CATEGORY_GROUPS.get(market_group, ""),
            "category_group_name": row["market_group_name"] or "",
            # 这个服务装备在当前建筑里能不能吃到燃料减免
            "in_fuel_scope": bool(scope) and market_group in scope,
            "fuel_per_hour": hourly,
            "fuel_online": utils.num(attrs.get(cfgmod.SERVICE_FUEL_ONLINE_ATTRIBUTE), 0.0),
            "fuel_group_id": utils.as_int(utils.num(
                attrs.get(cfgmod.SERVICE_FUEL_GROUP_ATTRIBUTE)), 0),
            # 与当前活动相关（用于界面默认勾选推荐）
            "recommended": bool(hints) and any(h in name for h in hints),
            "is_industry": any(k in name for k in ("制造厂", "实验室", "船坞", "反应堆",
                                                   "提炼设施", "研究")),
        })
    out.sort(key=lambda x: (not x["recommended"], x["market_group_id"], x["name"]))
    return out


def fuel_plan(store, structure_type_id=None, service_type_ids=None, hours: int = 720,
              price: float = None, fuel_type_id: int = None, online: bool = True) -> dict:
    """算建筑烧燃料的量与钱。

    规则（依据 SDE 属性，见 config 里的说明）：
      每小时燃料块 = Σ(在线服务装备的 2109) × (1 + 建筑 2339/100)
      上线一次性   = Σ(2110)（首次上线才烧，可选）
    2339 是负值（−25 → 少 25%），所以乘的是 (1 + 2339/100)。
    """
    service_type_ids = [int(i) for i in (service_type_ids or []) if i]
    all_services = service_modules(store, structure_type_id)
    by_id = {s["type_id"]: s for s in all_services}
    chosen = [by_id[i] for i in service_type_ids if i in by_id]
    unknown = [i for i in service_type_ids if i not in by_id]

    reduction = 0.0
    slots = 0
    structure_name = None
    if structure_type_id:
        sid = int(structure_type_id)
        sattrs = store.bonus_attributes_many([sid]).get(sid) or {}
        reduction = utils.num(sattrs.get(cfgmod.STRUCTURE_FUEL_BONUS_ATTRIBUTE), 0.0)
        slots = utils.as_int(utils.num(
            sattrs.get(cfgmod.STRUCTURE_SERVICE_SLOTS_ATTRIBUTE)), 0)
        structure_name = (store.type_by_id(sid) or {}).get("name")
    # 减免是负的百分点；乘数 = 1 + 2339/100
    multiplier = max(0.0, 1.0 + reduction / 100.0)
    unit_factor = utils.num(cfgmod.FUEL_BLOCKS_PER_UNIT, 1.0) or 1.0
    # **逐项打折**：建筑只对它的作用域内的服务分类打折。
    # 例：阿兹贝尔（工程复合体）的 −25% 只作用于「工程服务装备」，
    # 装在它上面的克隆中心（堡垒服务装备）不打折 —— 这与游戏内装配模拟显示的
    # 240 燃料/天（= 10/小时 × 24，未打折）完全一致。
    scope = cfgmod.fuel_scope_for(store, structure_type_id)
    base_hourly = 0.0
    hourly = 0.0
    for svc in chosen:
        in_scope = bool(scope) and utils.as_int(svc.get("market_group_id")) in scope
        svc["in_fuel_scope"] = in_scope
        per_hour = utils.num(svc.get("fuel_per_hour"), 0.0) * unit_factor
        svc["effective_fuel_per_hour"] = per_hour * (multiplier if in_scope else 1.0)
        base_hourly += utils.num(svc.get("fuel_per_hour"), 0.0) * unit_factor
        hourly += svc["effective_fuel_per_hour"]
    hours = max(0, utils.as_int(hours, 720))
    total = hourly * hours

    fuel_id = int(fuel_type_id or cfgmod.DEFAULT_FUEL_BLOCK_TYPE_ID)
    unit_price = price
    if unit_price is None:
        # 本地物价库（「刷新物价」写进去的吉他盘口/调整价）
        row = {}
        try:
            row = store.get_price(fuel_id) or {}
        except Exception:
            row = {}
        unit_price = utils.num((row or {}).get("price"), 0.0)
    unit_price = utils.num(unit_price, 0.0)
    fuel_name = (store.type_by_id(fuel_id) or {}).get("name")

    return {
        "structure_type_id": int(structure_type_id) if structure_type_id else None,
        "structure_name": structure_name,
        "structure_fuel_bonus": reduction,
        "fuel_multiplier": multiplier,
        # 这个建筑的减免对哪些服务分类生效（市场分组 ID + 名称）
        "fuel_scope_groups": list(scope),
        "fuel_scope_names": [cfgmod.SERVICE_CATEGORY_GROUPS.get(g, str(g)) for g in scope],
        "fuel_blocks_per_unit": unit_factor,
        "service_slots": slots,
        "services": chosen,
        "unknown_service_ids": unknown,
        "base_fuel_per_hour": base_hourly,
        "fuel_per_hour": hourly,
        "fuel_per_day": hourly * 24,
        "fuel_per_month": hourly * 720,
        "hours": hours,
        "fuel_total": total,
        # 注意：**不计入「服务上线一次性燃料」（属性 2110）**。
        # 那是一次性开销（约等于 3 天的量），用户明确要求只关心持续消耗。
        # 原始数值仍留在服务装备数据里（service_modules() 的 fuel_online），需要时可查。
        "fuel_type_id": fuel_id,
        "fuel_type_name": fuel_name,
        "unit_price": unit_price,
        "cost_total": total * unit_price,
        "cost_per_month": hourly * 720 * unit_price,
        "fuel_expires_hours": (total / hourly) if hourly else None,
        "available_services": all_services,
        "mechanics": [
            "建筑本身不消耗燃料，只有在线的服务装备消耗（CCP 官方《建筑管理》）。",
            "只计算**持续消耗**（每小时一次）；服务首次上线的那笔一次性燃料不计入。",
            "关掉服务装备就停止消耗。",
            "可用燃料块只有 4 种：氮 / 氢 / 氦 / 氧燃料块。",
            "游戏里装配模拟显示的「燃料 N/天」是**未打折**的原始值；"
            "本程序另乘建筑减免后的才是预计实际消耗。",
        ],
        "notes": ([ "⚠「%s」的燃料减免属性是 %.0f%%，而游戏内文本写的是 15%%。"
                    "本程序按属性计算（属性才是生效值）。" % (structure_name, -reduction)]
                  if (structure_name and "莫罗" in structure_name and abs(reduction + 35) < 0.01)
                  else []),
    }


def fuel_types(store) -> list:
    """可用于建筑的燃料块（只列已发布的市场物品）+ 本地价格。"""
    out = []
    for type_id in cfgmod.FUEL_BLOCK_TYPE_IDS:
        row = store.type_by_id(type_id) or {}
        price_row = {}
        try:
            price_row = store.get_price(type_id) or {}
        except Exception:
            price_row = {}
        out.append({
            "type_id": type_id,
            "name": row.get("name") or str(type_id),
            "published": bool(row.get("published")),
            "volume": utils.num(row.get("volume"), 0.0),
            "price": utils.num((price_row or {}).get("price"), 0.0),
        })
    out.sort(key=lambda x: (not x["price"], x["name"]))
    return out


def structure_bonus_candidates(store, activity: str = "manufacturing", limit: int = 600):
    """可在工业建筑上安装、且提供该活动加成的类型（改装件 + 建筑）。

    返回里带上 rigSize 与可安装的建筑组，界面据此**只显示与所选建筑匹配的改装件**。
    """
    modifier_activity = cfgmod.ACTIVITY_TO_MODIFIER.get(activity, "manufacturing")
    rows = store.q(
        "SELECT DISTINCT s.type_id, t.name, t.group_id, g.name AS group_name,"
        " t.category_id FROM industry_modifier_sources s"
        " LEFT JOIN types t ON t.type_id = s.type_id"
        " LEFT JOIN groups g ON g.group_id = t.group_id"
        " WHERE s.activity = ? ORDER BY g.name, t.name LIMIT ?",
        (modifier_activity, int(limit)))
    out = []
    ids = [r["type_id"] for r in rows if r["type_id"]]
    bonus_map = store.bonus_attributes_many(ids) if ids else {}
    for row in rows:
        attrs = bonus_map.get(row["type_id"]) or {}
        item = dict(row)
        item["material_percent"] = utils.num(attrs.get(2594)) or utils.num(attrs.get(2714))
        item["time_percent"] = utils.num(attrs.get(2593)) or utils.num(attrs.get(2713))
        item["cost_percent"] = utils.num(attrs.get(2595))
        item["is_structure"] = any(a in attrs for a in (2600, 2601, 2602, 2721))
        rig_size = utils.as_int(utils.num(attrs.get(cfgmod.RIG_SIZE_ATTRIBUTE)), 0)
        item["rig_size"] = rig_size
        item["rig_size_label"] = cfgmod.RIG_SIZE_LABELS.get(rig_size, "")
        item["fit_group_ids"] = [utils.as_int(utils.num(attrs.get(a)), 0)
                                 for a in cfgmod.RIG_FIT_GROUP_ATTRIBUTES
                                 if attrs.get(a)]
        out.append(item)
    return out


# --------------------------------------------------------------------------
# 材料 / 时间
# --------------------------------------------------------------------------

def me_adjusted_quantity(base_quantity: int, runs: int = 1, me: float = 0.0,
                         material_factor: float = 1.0,
                         activity: str = "manufacturing",
                         rounding: str = "ceil") -> int:
    """按 ME 与设施材料倍率计算实际材料消耗量。

    material_factor 是设施（结构 + 改装件）带来的**乘法**倍率（0.9702 = 材料降到 97.02%）。
    """
    base_quantity = max(0, utils.as_int(base_quantity))
    runs = max(1, utils.as_int(runs, 1))
    if base_quantity == 0:
        return 0
    total = base_quantity * runs
    if activity == "reaction":
        # 反应配方不吃 ME，但仍受建筑/改装件影响
        raw = total * utils.clamp(utils.num(material_factor, 1.0), 0.0, 10.0)
    else:
        me = utils.clamp(utils.num(me), 0.0, 100.0)
        raw = total * (1.0 - me / 100.0) * utils.clamp(
            utils.num(material_factor, 1.0), 0.0, 10.0)
    if rounding == "round":
        adjusted = int(round(raw))
    elif rounding == "floor":
        adjusted = int(math.floor(raw))
    else:
        adjusted = int(math.ceil(raw))
    return max(adjusted, runs)


def job_time_seconds(base_time: int, runs: int = 1, te: float = 0.0,
                     time_factor: float = 1.0, skill_factor: float = 1.0) -> int:
    """基础时间 × 流程数 × TE × 设施倍率 × 技能倍率（全部相乘）。"""
    runs = max(1, utils.as_int(runs, 1))
    te = utils.clamp(utils.num(te), 0.0, 100.0)
    factor = ((1.0 - te / 100.0)
              * utils.clamp(utils.num(time_factor, 1.0), 0.0, 10.0)
              * utils.clamp(utils.num(skill_factor, 1.0), 0.0, 1.0))
    return max(1, int(round(utils.as_int(base_time) * runs * factor)))


def skill_time_factor(activity: str, skill_levels: dict, config: dict) -> tuple:
    """技能带来的时间**倍率**（各技能相乘，不是相加）。

    实测依据：Rifter（基础 6000 秒）在 Industry 5 级 + Advanced Industry 5 级下
    实际为 4080 秒 = 6000 × (1-0.04×5) × (1-0.03×5)，而不是 6000 × (1-0.35)。
    返回 (倍率, 明细列表)
    """
    table = cfgmod.TIME_SKILLS.get(activity) or cfgmod.TIME_SKILLS.get("manufacturing") or []
    factor = 1.0
    details = []
    for skill_id, per_level, fallback_level, name in table:
        level = skill_levels.get(skill_id)
        source = "已读取角色技能"
        if level is None:
            level = fallback_level
            source = "未读取技能，取默认等级"
        level = utils.clamp(utils.as_int(level), 0, 5)
        bonus = per_level * level
        factor *= (1.0 - bonus)
        details.append({"skill_id": skill_id, "name": name, "level": level,
                        "per_level": per_level, "bonus": bonus, "source": source})
    return max(factor, 0.1), details


# 兼容旧名字
def skill_time_reduction(activity: str, skill_levels: dict, config: dict) -> tuple:
    factor, details = skill_time_factor(activity, skill_levels, config)
    return 1.0 - factor, details


# --------------------------------------------------------------------------
# 任务费用
# --------------------------------------------------------------------------

def job_cost_base(eiv: float, activity: str, base_time: int = 0, runs: int = 1) -> float:
    """计费基数：制造/反应 = EIV；拷贝/发明 = 2%×EIV；研究 = 2%×EIV×秒数/105。"""
    eiv = max(0.0, utils.num(eiv))
    if activity in ("research_material", "research_time"):
        seconds = utils.as_int(base_time) * max(1, utils.as_int(runs, 1))
        return eiv * 0.02 * (seconds / 105.0)
    factor = cfgmod.JOB_COST_BASE_FACTOR.get(activity, 1.0)
    return eiv * utils.num(factor, 1.0)


def job_cost_breakdown(eiv: float, cost_index: float, activity: str = "manufacturing",
                       base_time: int = 0, runs: int = 1,
                       scc_surcharge: float = 0.04, cost_factor: float = 1.0,
                       tax_rate: float = 0.0025, alpha_tax: float = 0.0,
                       include_tax: bool = True, include_cost: bool = True) -> dict:
    """把任务费用拆成可核对的几块。"""
    # SCC 按活动解析：调用方传的若仍是旧的全局默认 0.04，则按活动取（研究 2%）。
    # 用户显式填过别的值时不覆盖。游戏内研究任务实证为 2%。
    try:
        if scc_surcharge is None or abs(float(scc_surcharge) - 0.04) < 1e-12:
            scc_surcharge = float(getattr(cfgmod, "SCC_BY_ACTIVITY", {}).get(
                str(activity or ""), scc_surcharge or 0.04))
    except Exception:
        pass
    base = job_cost_base(eiv, activity, base_time, runs)
    index = max(0.0, utils.num(cost_index))
    system_cost = base * index
    cost_factor = utils.clamp(utils.num(cost_factor, 1.0), 0.0, 1.0)
    reduction = system_cost * (1.0 - cost_factor)
    after = system_cost * cost_factor
    scc = base * utils.num(scc_surcharge)
    tax = base * utils.clamp(utils.num(tax_rate), 0.0, 1.0) if include_tax else 0.0
    alpha = base * utils.num(alpha_tax)
    total = (after + scc + tax + alpha) if include_cost else 0.0
    return {
        "estimated_item_value": eiv,
        "job_cost_base": base,
        "job_cost_base_factor": (base / eiv) if eiv else 0.0,
        "cost_index": index,
        "system_cost": system_cost if include_cost else 0.0,
        "facility_reduction": -reduction if include_cost else 0.0,
        "scc_surcharge": scc if include_cost else 0.0,
        "tax": tax,
        "alpha_tax": alpha,
        "total": total,
    }


def cost_index_for(store, system_id, activity: str) -> float:
    """取某星系某活动的成本指数（来自 ESI /industry/systems/）。"""
    if not system_id:
        return 0.0
    indices = store.cost_indices_for(int(system_id))
    key = cfgmod.ACTIVITY_TO_COST_INDEX.get(activity, activity)
    return utils.num(indices.get(key))


def best_cost_index_system(store, activity: str, min_security: float = 0.45):
    """找成本指数最低的星系（可按最低安等过滤）。"""
    key = cfgmod.ACTIVITY_TO_COST_INDEX.get(activity, activity)
    row = store.q1(
        "SELECT c.system_id, c.cost_index, s.name, s.security_status, s.region_id"
        " FROM cost_indices c JOIN solar_systems s ON s.system_id = c.system_id"
        " WHERE c.activity = ? AND (s.security_status IS NULL OR s.security_status >= ?)"
        " ORDER BY c.cost_index ASC LIMIT 1", (key, utils.num(min_security)))
    return dict(row) if row else None


# --------------------------------------------------------------------------
# 单价解析
# --------------------------------------------------------------------------

def resolve_price(price_row: dict, market_row: dict, config: dict, type_id: int,
                  history_price=None) -> tuple:
    """按配置的「采购价来源」决定单价，返回 (价格, 来源说明)。"""
    source = (config or {}).get("price_source") or "jita_sell"
    custom = ((config or {}).get("custom_prices") or {}).get(str(type_id))
    if custom:
        return utils.num(custom), "自定义"
    adjusted = utils.num((price_row or {}).get("adjusted_price"))
    average = utils.num((price_row or {}).get("average_price"))
    sell = utils.num((market_row or {}).get("sell"))
    buy = utils.num((market_row or {}).get("buy"))

    if source == "adjusted" and adjusted:
        return adjusted, "调整价"
    if source == "average" and average:
        return average, "平均价"
    if source == "jita_sell" and sell:
        return sell, "吉他卖单"
    if source == "jita_buy" and buy:
        return buy, "吉他买单"
    if source == "history" and history_price:
        return utils.num(history_price), "7日均价"
    for value, label in ((sell, "吉他卖单"), (adjusted, "调整价"),
                         (average, "平均价"), (buy, "吉他买单")):
        if value:
            return value, label + "(兜底)"
    return 0.0, "无价格"


def eiv_materials(activities: dict, activity: str) -> list:
    """取用于计算 EIV 的材料：本活动没有材料时（研究/拷贝）退回制造配方。"""
    payload = activities.get(activity) or {}
    materials = payload.get("materials") or []
    if materials:
        return materials
    fallback = activities.get("manufacturing") or {}
    return fallback.get("materials") or []


# --------------------------------------------------------------------------
# 单个产品的成本构成
# --------------------------------------------------------------------------

def product_breakdown(store, blueprint_type_id: int, activity: str = "manufacturing",
                      me: float = 10, te: float = 20, runs: int = 1,
                      prices: dict = None, market: dict = None, config: dict = None,
                      bonuses: dict = None, skill_levels: dict = None,
                      cost_index: float = 0.0, history_prices: dict = None,
                      inventory: dict = None) -> dict:
    """计算一个产品在给定 ME/TE/流程数下的完整成本构成。"""
    config = config or {}
    prices = prices or {}
    market = market or {}
    history_prices = history_prices or {}
    inventory = inventory or {}
    bonuses = bonuses or {}
    material_factor = utils.num(bonuses.get("material_factor"), 1.0)
    time_factor = utils.num(bonuses.get("time_factor"), 1.0)
    cost_factor = utils.num(bonuses.get("cost_factor"), 1.0)
    rounding = config.get("material_rounding", "ceil")

    activities = store.blueprint_activities(int(blueprint_type_id))
    payload = activities.get(activity)
    result = {
        "blueprint_type_id": int(blueprint_type_id),
        "activity": activity,
        "activity_name": cfgmod.ACTIVITIES.get(activity, activity),
        "me": utils.num(me), "te": utils.num(te), "runs": max(1, utils.as_int(runs, 1)),
        "available": payload is not None,
        "materials": [], "products": [], "warnings": [],
    }
    if payload is None:
        result["warnings"].append("SDE 中没有该蓝图在「%s」下的配方"
                                  % cfgmod.ACTIVITIES.get(activity, activity))
        return result

    # ---- 产物 ----
    products = []
    for product in payload.get("products") or []:
        products.append({
            "type_id": product["type_id"],
            "quantity_per_run": product["quantity"],
            "quantity": product["quantity"] * result["runs"],
            "probability": product.get("probability"),
        })
    result["products"] = products
    primary = products[0] if products else None
    if primary:
        type_row = store.type_by_id(primary["type_id"]) or {}
        primary["name"] = type_row.get("name")
        primary["volume"] = utils.num(type_row.get("volume"))
        primary["packaged_volume"] = utils.num(type_row.get("packaged_volume"))
        primary["group_id"] = type_row.get("group_id")
        primary["category_id"] = type_row.get("category_id")
        primary["total_volume"] = primary["volume"] * primary["quantity"]
    result["product"] = primary

    # ---- 材料（按 ME + 设施倍率调整）----
    material_rows = []
    material_cost = 0.0
    material_volume = 0.0
    for material in payload.get("materials") or []:
        type_id = material["type_id"]
        base_qty = material["quantity"]
        qty = me_adjusted_quantity(base_qty, result["runs"], me, material_factor,
                                  activity, rounding)
        price_row = prices.get(type_id) or {}
        market_row = market.get(type_id) or {}
        unit_price, price_source = resolve_price(price_row, market_row, config, type_id,
                                                history_prices.get(type_id))
        type_row = store.type_by_id(type_id) or {}
        volume = utils.num(type_row.get("volume"))
        cost = unit_price * qty
        inventory_qty = utils.as_int(inventory.get(type_id))
        material_cost += cost
        material_volume += volume * qty
        material_rows.append({
            "type_id": type_id,
            "name": type_row.get("name") or ("类型 %d" % type_id),
            "group_id": type_row.get("group_id"),
            "category_id": type_row.get("category_id"),
            "volume": volume,
            "base_quantity": base_qty,
            "quantity": qty,
            "quantity_per_run": me_adjusted_quantity(base_qty, 1, me, material_factor,
                                                    activity, rounding),
            "unit_price": unit_price,
            "price_source": price_source,
            "adjusted_price": utils.num(price_row.get("adjusted_price")),
            "cost": cost,
            "total_volume": volume * qty,
            "inventory": inventory_qty,
            "shortfall": max(0, qty - inventory_qty),
        })
    material_rows.sort(key=lambda item: -item["cost"])
    result["materials"] = material_rows

    # ---- EIV：用 ME0 基准量（已实测与 ME / 建筑 / rig 无关）----
    eiv = 0.0
    for material in eiv_materials(activities, activity):
        adjusted = utils.num((prices.get(material["type_id"]) or {}).get("adjusted_price"))
        eiv += adjusted * utils.as_int(material["quantity"]) * result["runs"]
    eiv = round(eiv)

    # ---- 费用与时间 ----
    skill_factor, skill_details = skill_time_factor(activity, skill_levels or {}, config)
    base_time = payload.get("time") or 0
    breakdown = job_cost_breakdown(
        eiv, cost_index, activity, base_time, result["runs"],
        scc_surcharge=utils.num(config.get("scc_surcharge", 0.04)),
        cost_factor=cost_factor,
        tax_rate=utils.num(config.get("job_tax_rate", 0.0)),
        alpha_tax=utils.num(config.get("alpha_clone_tax", 0.0)),
        include_tax=bool(config.get("include_job_tax", True)),
        include_cost=bool(config.get("include_job_cost", True)),
    )
    time_seconds = job_time_seconds(base_time, result["runs"], te, time_factor, skill_factor)
    total_cost = material_cost + breakdown["total"]
    units = primary["quantity"] if primary else 0
    unit_cost = (total_cost / units) if units else 0.0

    result.update({
        "material_cost": material_cost,
        "material_volume": material_volume,
        "job_cost": breakdown["total"],
        "job_cost_breakdown": breakdown,
        "total_cost": total_cost,
        "units": units,
        "unit_cost": unit_cost,
        "unit_material_cost": (material_cost / units) if units else 0.0,
        "unit_job_cost": (breakdown["total"] / units) if units else 0.0,
        "estimated_item_value": eiv,
        "time_seconds": time_seconds,
        "time_per_unit": (time_seconds / units) if units else 0.0,
        "time_text": utils.fmt_duration(time_seconds),
        "base_time": base_time,
        "max_production_limit": payload.get("max_production_limit") or 1,
        "skills_required": payload.get("skills") or [],
        "skill_time_factor": skill_factor,
        "skill_time_reduction": 1.0 - skill_factor,
        "skill_time_details": skill_details,
        "facility_bonuses": {
            "material_factor": material_factor, "time_factor": time_factor,
            "cost_factor": cost_factor, "material": 1 - material_factor,
            "time": 1 - time_factor, "cost": 1 - cost_factor,
            "sources": bonuses.get("sources") or [], "notes": bonuses.get("notes") or [],
            "security": bonuses.get("security"),
        },
        "price_config": {
            "price_source": config.get("price_source"),
            "scc_surcharge": config.get("scc_surcharge"),
            "job_tax_rate": config.get("job_tax_rate"),
            "material_rounding": rounding,
        },
    })
    return result


# --------------------------------------------------------------------------
# 多种产品：产品行 + 聚合材料行
# --------------------------------------------------------------------------

def plan_products(store, items, activity: str = "manufacturing", prices: dict = None,
                  market: dict = None, config: dict = None, bonuses: dict = None,
                  skill_levels: dict = None, cost_index: float = 0.0,
                  inventory: dict = None, history_prices: dict = None,
                  bonus_resolver=None) -> dict:
    """算一批产品，逐行给出「自造成本 / 市价 / 总价 / 利润 / 利润率 / 体积」。

    bonus_resolver(activity, product_type_row) -> bonuses
        用于按产品套用不同的改装件作用范围（不传则所有产品用同一套 bonuses）。
    purchase_mode='purchase' 表示直接收购（成本 = 市价，利润为 0）。
    """
    config = config or {}
    inventory = inventory or {}
    rows = []
    total_cost = 0.0
    total_revenue = 0.0
    total_volume = 0.0

    for item in items or []:
        blueprint_type_id = item.get("blueprint_type_id")
        product_type_id = item.get("type_id") or item.get("product_type_id")
        runs = max(1, utils.as_int(item.get("runs"), 1))
        me = utils.num(item.get("me"), 10)
        te = utils.num(item.get("te"), 20)
        mode = item.get("purchase_mode") or "build"
        item_activity = item.get("activity") or activity

        if not blueprint_type_id and product_type_id:
            blueprint_type_id, found_activity, _ = store.blueprint_for_product(int(product_type_id))
            if found_activity:
                item_activity = item.get("activity") or found_activity
            if not blueprint_type_id:
                rows.append({"type_id": product_type_id, "error": "找不到该物品的可用蓝图",
                             "name": (store.type_by_id(product_type_id) or {}).get("name")})
                continue

        product_row = store.type_by_id(int(product_type_id)) if product_type_id else None
        item_bonuses = bonuses
        if bonus_resolver is not None:
            item_bonuses = bonus_resolver(item_activity, product_row) or bonuses

        detail = product_breakdown(store, blueprint_type_id, item_activity, me, te, runs,
                                  prices, market, config, item_bonuses, skill_levels,
                                  cost_index, history_prices, inventory)
        primary = detail.get("product") or {}
        type_id = primary.get("type_id") or product_type_id
        units = detail.get("units") or 0
        available = utils.as_int(inventory.get(type_id))

        market_row = (market or {}).get(type_id) or {}
        price_row = (prices or {}).get(type_id) or {}
        unit_price, price_source = resolve_price(price_row, market_row, config, type_id,
                                                (history_prices or {}).get(type_id))
        unit_cost = detail.get("unit_cost") or 0.0
        if mode == "purchase":
            unit_cost = unit_price
        revenue = unit_price * units
        profit = revenue - unit_cost * units
        margin = (profit / (unit_cost * units)) if (unit_cost * units) else 0.0
        volume = utils.num(primary.get("volume"))
        type_row = store.type_by_id(type_id) or {}

        row = {
            "type_id": type_id,
            "name": type_row.get("name") or primary.get("name"),
            "blueprint_type_id": blueprint_type_id,
            "group_id": type_row.get("group_id"),
            "category_id": type_row.get("category_id"),
            "market_group_id": type_row.get("market_group_id"),
            "activity": item_activity,
            "activity_name": cfgmod.ACTIVITIES.get(item_activity, item_activity),
            "me": me, "te": te, "runs": runs,
            "quantity": units,
            "inventory": available,
            "shortfall": max(0, units - available),
            "purchase_mode": mode,
            "unit_cost": unit_cost,
            "unit_price": unit_price,
            "price_source": price_source,
            "material_cost": detail.get("material_cost", 0.0),
            "job_cost": detail.get("job_cost", 0.0),
            "total_cost": unit_cost * units,
            "total_price": revenue,
            "total_profit": profit,
            "margin": margin,
            "volume": volume,
            "packaged_volume": utils.num(type_row.get("packaged_volume")),
            "total_volume": volume * units,
            "time_seconds": detail.get("time_seconds", 0),
            "time_text": utils.fmt_duration(detail.get("time_seconds", 0)),
            "estimated_item_value": detail.get("estimated_item_value", 0.0),
            "warnings": detail.get("warnings") or [],
            "materials": detail.get("materials") or [],
            "job_cost_breakdown": detail.get("job_cost_breakdown") or {},
            "facility_bonuses": detail.get("facility_bonuses") or {},
        }
        rows.append(row)
        total_cost += row["total_cost"]
        total_revenue += revenue
        total_volume += row["total_volume"]

    total_profit = total_revenue - total_cost
    return {
        "ok": True,
        "rows": rows,
        "totals": {
            "total_cost": total_cost,
            "total_price": total_revenue,
            "total_profit": total_profit,
            "margin": (total_profit / total_cost) if total_cost else 0.0,
            "total_volume": total_volume,
            "items": len(rows),
        },
        "activity": activity,
        "activity_name": cfgmod.ACTIVITIES.get(activity, activity),
    }


def aggregate_materials(store, items, activity: str = "manufacturing", prices: dict = None,
                        market: dict = None, config: dict = None, bonus_resolver=None,
                        bonuses: dict = None, skill_levels: dict = None,
                        cost_index: float = 0.0, inventory: dict = None,
                        history_prices: dict = None, build_modes: dict = None,
                        material_me: float = 10, material_te: float = 20,
                        location_name: str = None) -> dict:
    """把一批产品需要的材料聚合成一张表（参考站点主界面的那张表）。

    每一行 = 一种原料：
        平均消耗数量  聚合后的总需求量
        可用库存数量  库存里该原料的数量
        单件成本      自造（含子材料 + 任务费用）的单价；选择「收购」时等于市价
        单价          市场单价
        总价          数量 × 单价
        总利润        (单价 - 单件成本) × 数量
        利润率        总利润 / (单件成本 × 数量)
        体积(m³)      数量 × 单件体积
    """
    config = config or {}
    prices = prices or {}
    market = market or {}
    inventory = inventory or {}
    history_prices = history_prices or {}
    build_modes = build_modes or {}
    plan = plan_products(store, items, activity, prices, market, config, bonuses,
                         skill_levels, cost_index, inventory, history_prices, bonus_resolver)

    aggregated = {}
    for row in plan["rows"]:
        if row.get("error"):
            continue
        if row.get("purchase_mode") == "purchase":
            # 直接收购成品，不消耗原料
            continue
        for material in row.get("materials") or []:
            key = material["type_id"]
            entry = aggregated.setdefault(key, {
                "type_id": key,
                "name": material["name"],
                "group_id": material.get("group_id"),
                "category_id": material.get("category_id"),
                "volume": material.get("volume") or 0.0,
                "quantity": 0,
                "used_by": [],
            })
            entry["quantity"] += material["quantity"]
            if row["name"] not in entry["used_by"]:
                entry["used_by"].append(row["name"])

    # 为需要「自造」判断的原料准备二级成本
    rows_out = []
    totals = {"material_cost": 0.0, "market_value": 0.0, "profit": 0.0,
              "volume": 0.0, "build_items": 0, "purchase_items": 0}
    for type_id, entry in aggregated.items():
        type_row = store.type_by_id(type_id) or {}
        price_row = prices.get(type_id) or {}
        market_row = market.get(type_id) or {}
        unit_price, price_source = resolve_price(price_row, market_row, config, type_id,
                                                history_prices.get(type_id))
        quantity = utils.as_int(entry["quantity"])
        blueprint_id, sub_activity, per_run = store.blueprint_for_product(type_id)
        can_build = blueprint_id is not None and sub_activity in ("manufacturing", "reaction")

        mode = build_modes.get(str(type_id))
        if mode is None:
            mode = "build" if can_build and build_modes.get("__default__") == "build" else "purchase"
        if not can_build:
            mode = "purchase"

        unit_build_cost = None
        build_detail = None
        if mode == "build" and can_build:
            sub_bonuses = bonuses
            if bonus_resolver is not None:
                sub_bonuses = bonus_resolver(sub_activity, type_row) or bonuses
            build_detail = product_breakdown(
                store, blueprint_id, sub_activity, material_me, material_te, 1,
                prices, market, config, sub_bonuses, skill_levels, cost_index,
                history_prices, inventory)
            sub_primary = build_detail.get("product") or {}
            if sub_primary.get("quantity"):
                unit_build_cost = build_detail["total_cost"] / sub_primary["quantity"]

        unit_cost = unit_build_cost if unit_build_cost is not None else unit_price
        inventory_qty = utils.as_int(inventory.get(type_id))
        cost_total = unit_cost * quantity
        market_total = unit_price * quantity
        profit = market_total - cost_total
        margin = (profit / cost_total) if cost_total else 0.0
        volume = utils.num(type_row.get("volume"))
        rows_out.append({
            "type_id": type_id,
            "name": entry["name"],
            "group_id": entry.get("group_id"),
            "category_id": entry.get("category_id"),
            "group_name": (store.q1("SELECT name FROM groups WHERE group_id=?",
                                    (entry.get("group_id"),)) or {"name": None})["name"],
            "category_name": (store.q1("SELECT name FROM categories WHERE category_id=?",
                                       (entry.get("category_id"),)) or {"name": None})["name"],
            "source": mode,                 # build / purchase（对应界面上的「自造 / 收购」开关）
            "source_label": "自造" if mode == "build" else "收购",
            "location_name": location_name,
            "quantity": quantity,
            "inventory": inventory_qty,
            "shortfall": max(0, quantity - inventory_qty),
            "unit_cost": unit_cost,
            "unit_build_cost": unit_build_cost,
            "unit_price": unit_price,
            "price_source": price_source,
            "cost_total": cost_total,
            "total_price": market_total,
            "total_profit": profit,
            "margin": margin,
            "volume": volume,
            "total_volume": volume * quantity,
            "can_build": can_build,
            "sub_blueprint_type_id": blueprint_id,
            "sub_activity": sub_activity,
            "used_by": entry["used_by"],
            "build_detail": {
                "job_cost": (build_detail or {}).get("job_cost"),
                "time_text": (build_detail or {}).get("time_text"),
                "me": material_me, "te": material_te,
            } if build_detail else None,
        })
        totals["material_cost"] += cost_total
        totals["market_value"] += market_total
        totals["profit"] += profit
        totals["volume"] += volume * quantity
        totals["build_items" if mode == "build" else "purchase_items"] += 1

    rows_out.sort(key=lambda r: (-r["cost_total"], r["name"] or ""))
    totals["margin"] = (totals["profit"] / totals["material_cost"]) if totals["material_cost"] else 0.0

    return {
        "ok": True,
        "rows": rows_out,
        "totals": totals,
        "products": plan["rows"],
        "product_totals": plan["totals"],
        "activity": activity,
        "activity_name": cfgmod.ACTIVITIES.get(activity, activity),
    }
