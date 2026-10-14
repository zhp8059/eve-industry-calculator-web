"""蓝图研究模拟：材料效率（ME）与时间效率（TE）在各类建筑里需要多久。

────────────────────────────────────────────────────────────────
等级模型（已用官方 SDE 交叉验证）
────────────────────────────────────────────────────────────────
  * ME 与 TE **都是 10 个研究等级**：
        ME 每级 −1%  → 蓝图显示 0–10（%）
        TE 每级 −2%  → 蓝图显示 0–20（%）
    所以「TE 显示值 ÷ 2 = TE 研究等级」。
  * 两者共用同一张等级表。SDE 里 4344 个蓝图的
        research_material.time == research_time.time
    恒成立，正说明 CCP 只存了一个基准（即「第 1 级的累计秒数」）。
  * 等级表里的数值是**累计秒数**（rank = 1 时）：
        第 n 级的累计 = table[n] × rank，第 n 级**本身**耗时 = 累计(n) − 累计(n−1)
    依据是研究任务费用公式同时给出了两种写法：
        从 n−1 级研究到 n 级：(BaseDuration_n − BaseDuration_{n−1}) × 0.02/105 × EIV
        从 0 级直接研究到 n 级：BaseDuration_n × 0.02/105 × EIV
    可见 BaseDuration_n 是「0 → n」的累计值。
  * 与 rank 的关系：多数蓝图满足 `SDE 研究时长 = rank × 105`
    （官方 rank 属性 1955，允许小数，例如 7.8 → 819 秒）。
    但有 154 个蓝图不满足（CCP 直接给了时长），因此本模块
    **一律以 SDE 的研究时长作为第 1 级基准，按等级表比例外推**，
    rank 只用于展示与一致性提示。

────────────────────────────────────────────────────────────────
时间与费用
────────────────────────────────────────────────────────────────
    第 n 级耗时 = 第 n 级基准秒数
                 × Π(结构时间倍率 2602 / 反应用 2721)
                 × Π(1 + 改装件时间减免 2593 × 安等倍率 / 100)
                 × Π(1 − 技能每级减免 × 等级)
        ME 研究受「冶金学」影响，TE 研究受「研究概论」影响，两者都受「高级工业理论」影响。

    第 n 级任务费用 = 计费基数 × 成本指数 × 费用倍率 + SCC + 设施税 + Alpha 税
        其中计费基数 = EIV × 2% × 第 n 级基准秒数 / 105
    （研究任务的费用公式为社区推算，官方未公开；界面上会标注）

实测/官方锚点（用于回归测试）：
    裂谷级蓝图 691 → SDE 研究时长 2100 秒，rank = 20
        第 1 级 = 2100 秒（1 级累计 = 2100 = 105 × 20 ✔ 与社区已知值一致）
        0 → 10 级累计 = 256000 × 20 = 5,120,000 秒 ≈ 59.3 天
"""

from __future__ import annotations

from . import config as cfgmod
from . import industry, utils

LEVEL_TABLE = list(cfgmod.RESEARCH_CUMULATIVE_SECONDS)   # rank=1 时的累计秒数
MAX_LEVEL = len(LEVEL_TABLE)
TABLE_BASE = LEVEL_TABLE[0]                              # = 105


# --------------------------------------------------------------------------
# 纯计算
# --------------------------------------------------------------------------

def cumulative_seconds(base_level1: float, level: int) -> int:
    """达到第 level 级所需的**累计**秒数（含前面所有等级）。"""
    if level <= 0:
        return 0
    level = min(int(level), MAX_LEVEL)
    return int(round(utils.num(base_level1) * LEVEL_TABLE[level - 1] / TABLE_BASE))


def level_increment_seconds(base_level1: float, level: int) -> int:
    """第 level 级这一级本身需要的秒数。"""
    return cumulative_seconds(base_level1, level) - cumulative_seconds(base_level1, level - 1)


def me_percent(level: int) -> float:
    """ME 研究等级 → 蓝图显示的材料效率百分比。"""
    return utils.clamp(utils.as_int(level), 0, MAX_LEVEL) * cfgmod.ME_PERCENT_PER_LEVEL


def te_percent(level: int) -> float:
    """TE 研究等级 → 蓝图显示的时间效率百分比。"""
    return utils.clamp(utils.as_int(level), 0, MAX_LEVEL) * cfgmod.TE_PERCENT_PER_LEVEL


def te_level_from_percent(percent) -> int:
    """蓝图显示的 TE 百分比 → 研究等级。"""
    return utils.clamp(int(round(utils.num(percent) / cfgmod.TE_PERCENT_PER_LEVEL)), 0, MAX_LEVEL)


# --------------------------------------------------------------------------
# 从 SDE 读取蓝图的研究基准
# --------------------------------------------------------------------------

def blueprint_info(store, blueprint_type_id: int) -> dict:
    bp = int(blueprint_type_id)
    activities = store.blueprint_activities(bp)
    me_time = utils.as_int((activities.get("research_material") or {}).get("time"))
    te_time = utils.as_int((activities.get("research_time") or {}).get("time"))
    rank = store.blueprint_rank(bp)
    type_row = store.type_by_id(bp) or {}
    product = store.q1(
        "SELECT product_type_id FROM activity_products WHERE blueprint_type_id=?"
        " ORDER BY CASE activity WHEN 'manufacturing' THEN 0 WHEN 'reaction' THEN 1 ELSE 2 END"
        " LIMIT 1", (bp,))
    product_id = product["product_type_id"] if product else None
    product_row = store.type_by_id(product_id) if product_id else None
    base = me_time or te_time
    implied = (base / TABLE_BASE) if base else None
    return {
        "blueprint_type_id": bp,
        "name": type_row.get("name"),
        "product_type_id": product_id,
        "product_name": (product_row or {}).get("name"),
        "product_group_id": (product_row or {}).get("group_id"),
        "product_category_id": (product_row or {}).get("category_id"),
        "has_research": bool(base),
        "base_me_seconds": me_time,
        "base_te_seconds": te_time,
        "base_seconds": base,
        "base_equal": (me_time == te_time) if (me_time and te_time) else None,
        "rank": rank,
        "rank_implied": implied,
        "rank_consistent": bool(rank is not None and base
                               and abs(utils.num(rank) * TABLE_BASE - base) <= 1),
        "max_level": MAX_LEVEL,
        "me_percent_per_level": cfgmod.ME_PERCENT_PER_LEVEL,
        "te_percent_per_level": cfgmod.TE_PERCENT_PER_LEVEL,
        "level_table": LEVEL_TABLE,
        "level_table_note": "表内为 rank=1 时达到第 n 级的累计秒数",
    }


# --------------------------------------------------------------------------
# 单个设施的模拟
# --------------------------------------------------------------------------

def _activity_plan(store, info: dict, activity: str, from_level: int, to_level: int,
                   time_factor: float, skill_factor: float, config: dict,
                   cost_factor: float, cost_index: float, eiv: float) -> dict:
    base = info["base_seconds"]
    from_level = utils.clamp(utils.as_int(from_level), 0, MAX_LEVEL)
    to_level = utils.clamp(utils.as_int(to_level), 0, MAX_LEVEL)
    if to_level < from_level:
        from_level, to_level = to_level, from_level
    rows = []
    total_seconds = 0
    total_base_seconds = 0
    total_cost = 0.0
    for level in range(from_level + 1, to_level + 1):
        base_increment = level_increment_seconds(base, level)
        seconds = industry.job_time_seconds(base_increment, 1, 0.0, time_factor, skill_factor)
        breakdown = industry.job_cost_breakdown(
            eiv, cost_index, activity, base_increment, 1,
            scc_surcharge=utils.num(config.get("scc_surcharge", 0.04)),
            cost_factor=cost_factor,
            # 研究类默认不含 0.25% 任务税（游戏研究窗口的税收总额只有 SCC）；用户显式改过 job_tax_rate 则以他为准
            tax_rate=(0.0 if abs(utils.num(config.get("job_tax_rate", 0.0)) - 0.0025) < 1e-12 else utils.num(config.get("job_tax_rate", 0.0))),
            alpha_tax=utils.num(config.get("alpha_clone_tax", 0.0)),
            include_tax=bool(config.get("include_job_tax", True)),
            include_cost=bool(config.get("include_job_cost", True)))
        total_seconds += seconds
        total_base_seconds += base_increment
        total_cost += breakdown["total"]
        rows.append({
            "level": level,
            "base_increment_seconds": base_increment,
            "base_cumulative_seconds": cumulative_seconds(base, level),
            "seconds": seconds,
            "time_text": utils.fmt_duration(seconds),
            "cost": breakdown["total"],
            "job_cost_base": breakdown["job_cost_base"],
        })
    return {
        "activity": activity,
        "activity_name": cfgmod.ACTIVITIES.get(activity, activity),
        "from_level": from_level,
        "to_level": to_level,
        "levels": rows,
        "total_seconds": total_seconds,
        "total_text": utils.fmt_duration(total_seconds),
        "total_base_seconds": total_base_seconds,
        "total_base_text": utils.fmt_duration(total_base_seconds),
        "total_cost": total_cost,
        "saving_seconds": total_base_seconds - total_seconds,
        "saving_ratio": ((total_base_seconds - total_seconds) / total_base_seconds)
                        if total_base_seconds else 0.0,
        "percent_from": (me_percent(from_level) if activity == "research_material"
                         else te_percent(from_level)),
        "percent_to": (me_percent(to_level) if activity == "research_material"
                       else te_percent(to_level)),
        "skill_factor": skill_factor,
        "time_factor": time_factor,
        "cost_factor": cost_factor,
    }


def simulate(store, blueprint_type_id: int, facilities, config: dict = None,
             skill_levels: dict = None, from_me: int = 0, to_me: int = None,
             from_te: int = 0, to_te: int = None, system_id=None, security: str = None,
             cost_index: float = 0.0, eiv: float = 0.0) -> dict:
    """对每个候选设施算出 ME / TE 研究的耗时与费用。

    facilities: [{'label', 'structure_type_id', 'rig_type_ids', 'note'}, ...]
                structure_type_id 为 None 表示 NPC 空间站（无加成）
    """
    config = config or {}
    info = blueprint_info(store, blueprint_type_id)
    if not info["has_research"]:
        return {"ok": False, "blueprint": info,
                "message": "该蓝图在 SDE 里没有研究活动（可能是反应配方或非可研究蓝图）"}
    to_me = MAX_LEVEL if to_me is None else to_me
    to_te = MAX_LEVEL if to_te is None else to_te

    skill_me, details_me = industry.skill_time_factor("research_material", skill_levels or {}, config)
    skill_te, details_te = industry.skill_time_factor("research_time", skill_levels or {}, config)

    rows = []
    for facility in facilities or []:
        structure_type_id = facility.get("structure_type_id")
        rig_type_ids = facility.get("rig_type_ids") or []
        result = {}
        for activity, key in (("research_material", "me"), ("research_time", "te")):
            bonuses = industry.facility_bonuses(
                store, structure_type_id, rig_type_ids, activity,
                system_id=system_id, security_override=security,
                product_category_id=info.get("product_category_id"),
                product_group_id=info.get("product_group_id"),
                product_type_id=info.get("product_type_id"))
            skill_factor = skill_me if activity == "research_material" else skill_te
            result[key] = _activity_plan(
                store, info, activity,
                from_me if key == "me" else from_te,
                to_me if key == "me" else to_te,
                utils.num(bonuses.get("time_factor"), 1.0), skill_factor, config,
                utils.num(bonuses.get("cost_factor"), 1.0), cost_index, eiv)
            result[key]["bonuses"] = {
                "time_factor": bonuses.get("time_factor"),
                "cost_factor": bonuses.get("cost_factor"),
                "material_factor": bonuses.get("material_factor"),
                "sources": bonuses.get("sources") or [],
                "notes": bonuses.get("notes") or [],
                "security": bonuses.get("security"),
            }
        rows.append({
            "label": facility.get("label"),
            "structure_type_id": structure_type_id,
            "structure_name": facility.get("structure_name"),
            "rig_type_ids": rig_type_ids,
            "rig_names": facility.get("rig_names") or [],
            "note": facility.get("note"),
            "me": result["me"],
            "te": result["te"],
        })

    # 以「最快的那个」为基准算相对节省
    def fastest(key):
        values = [r[key]["total_seconds"] for r in rows if r[key]["total_seconds"]]
        return min(values) if values else 0

    best_me, best_te = fastest("me"), fastest("te")
    for row in rows:
        row["me"]["vs_best_ratio"] = ((row["me"]["total_seconds"] - best_me) / best_me
                                     if best_me else 0.0)
        row["te"]["vs_best_ratio"] = ((row["te"]["total_seconds"] - best_te) / best_te
                                     if best_te else 0.0)

    rows.sort(key=lambda r: r["me"]["total_seconds"])

    return {
        "ok": True,
        "blueprint": info,
        "facilities": rows,
        "best_me_seconds": best_me,
        "best_te_seconds": best_te,
        "skill_details": {"me": details_me, "te": details_te},
        "skill_factors": {"me": skill_me, "te": skill_te},
        "eiv": eiv,
        "cost_index": cost_index,
        "notes": [
            "等级表为「累计秒数」，每级本身耗时 = 本等级累计 − 上一等级累计。",
            "ME 与 TE 各有 10 个研究等级：ME 每级 −1%（显示 0–10），TE 每级 −2%（显示 0–20）。",
            "研究任务的费用公式为社区推算（官方未公开），仅供量级参考。",
        ],
    }


def default_facilities(store, rig_type_ids=None, activity="research_material") -> list:
    """默认对比清单：NPC 空间站 + 所有提供研究时间加成的建筑（可选再叠加改装件）。"""
    facilities = [{
        "label": "NPC 空间站（无建筑加成）",
        "structure_type_id": None, "rig_type_ids": [], "rig_names": [],
        "note": "只有技能减免",
    }]
    # 只接受数字（字符串会被按字符迭代 → int('a') 抛错，v0.8.29 就是这么坏的）
    _clean = []
    for _i in (rig_type_ids or []):
        try:
            _clean.append(int(_i))
        except (TypeError, ValueError):
            utils.log("忽略非数字的改装件参数：%r", _i)
    rig_type_ids = _clean
    rig_names = store.type_names(rig_type_ids) if rig_type_ids else {}
    structures = store.q(
        "SELECT DISTINCT t.type_id, t.name, t.group_id, g.name AS group_name"
        " FROM type_bonus_attributes b"
        " JOIN types t ON t.type_id = b.type_id"
        " LEFT JOIN groups g ON g.group_id = t.group_id"
        " WHERE b.attribute_id IN (2601, 2602, 2721)"
        " ORDER BY g.name, t.name")
    for row in structures:
        label = row["name"] or str(row["type_id"])
        # 不再为每个建筑单独出一档（用户要求：去掉「无改装件」的裸建筑行）；
        # 只保留下面的 6 档「建筑 + 改装件」，以及 NPC 空间站基准。
        pass
        if rig_type_ids and rig_type_ids != "auto":
            # 旧接口兼容：调用方显式指定改装件时按原样给一档
            facilities.append({
                "label": "%s + %s" % (label, "、".join(rig_names.get(i, str(i)) for i in rig_type_ids)),
                "structure_type_id": row["type_id"],
                "rig_type_ids": list(rig_type_ids),
                "rig_names": [rig_names.get(i, str(i)) for i in rig_type_ids],
                "structure_name": label,
                "note": "含改装件",
            })

    # ------------------------------------------------------------------
    # 默认对比档（用户指定，已按尺寸核对）：
    #   索迪约(XL) + 屹立超大型实验室优化 II / I
    #   阿兹贝尔(L) + 屹立大型时间效率研究优化 II / I
    #   莱塔卢(M)  + 屹立中型时间效率研究加速 II / 加速器 I
    # 说明：用户最初把第 4 条写成「索迪约 + 大型」，已确认是笔误（索迪约是 XL 建筑）。
    # ------------------------------------------------------------------
    if not rig_type_ids:
        for sname, rnames in (
            ("索迪约", ("屹立超大型实验室优化 II", "屹立超大型实验室优化 I")),
            ("阿兹贝尔", ("屹立大型时间效率研究优化 II", "屹立大型时间效率研究优化 I")),
            ("莱塔卢", ("屹立中型时间效率研究加速 II", "屹立中型时间效率研究加速器 I")),
        ):
            srow = store.q1("SELECT type_id, name FROM types WHERE name=?", (sname,))
            if not srow:
                utils.log("默认对比档：找不到建筑 %s，跳过", sname)
                continue
            srow = dict(srow)
            for rname in rnames:
                rrow = store.q1("SELECT type_id, name FROM types WHERE name=?", (rname,))
                if not rrow:
                    utils.log("默认对比档：找不到改装件 %s，跳过", rname)
                    continue
                rrow = dict(rrow)
                facilities.append({
                    "label": "%s + %s" % (srow["name"], rrow["name"]),
                    "structure_type_id": int(srow["type_id"]),
                    "rig_type_ids": [int(rrow["type_id"])],
                    "rig_names": [rrow["name"]],
                    "structure_name": srow["name"],
                    "note": "含改装件",
                })
    return facilities


# --------------------------------------------------------------------------
# 已拥有蓝图的研究规划
# --------------------------------------------------------------------------

def owned_research(store, scope: str, facilities, config: dict = None,
                   skill_levels: dict = None, system_id=None, security: str = None,
                   cost_index: float = 0.0, eiv_map: dict = None) -> dict:
    """列出某 scope（角色/军团）里所有蓝图的研究现状与「研究到满级」的耗时。"""
    config = config or {}
    eiv_map = eiv_map or {}
    blueprints = store.blueprints_of(scope)
    out = []
    for row in blueprints:
        bp = row.get("type_id")
        info = blueprint_info(store, bp)
        if not info["has_research"]:
            continue
        me_level = utils.clamp(utils.as_int(row.get("material_efficiency")), 0, MAX_LEVEL)
        te_value = utils.as_int(row.get("time_efficiency"))
        te_level = te_level_from_percent(te_value)
        sim = simulate(store, bp, facilities, config, skill_levels,
                       from_me=me_level, to_me=MAX_LEVEL,
                       from_te=te_level, to_te=MAX_LEVEL,
                       system_id=system_id, security=security,
                       cost_index=cost_index, eiv=utils.num(eiv_map.get(bp)))
        if not sim.get("ok"):
            continue
        best = sim["facilities"][0] if sim["facilities"] else None
        out.append({
            "item_id": row.get("item_id"),
            "blueprint_type_id": bp,
            "name": info["name"],
            "product_type_id": info["product_type_id"],
            "product_name": info["product_name"],
            "runs": row.get("runs"),
            "quantity": row.get("quantity"),
            "material_efficiency": me_level,
            "time_efficiency_value": te_value,
            "me_level": me_level,
            "te_level": te_level,
            "me_percent": me_percent(me_level),
            "te_percent": te_percent(te_level),
            "to_max_me_seconds": best["me"]["total_seconds"] if best else 0,
            "to_max_te_seconds": best["te"]["total_seconds"] if best else 0,
            "to_max_me_text": utils.fmt_duration(best["me"]["total_seconds"]) if best else "-",
            "to_max_te_text": utils.fmt_duration(best["te"]["total_seconds"]) if best else "-",
            "at_me_max": me_level >= MAX_LEVEL,
            "at_te_max": te_level >= MAX_LEVEL,
            "best_facility": best["label"] if best else None,
            "rank": info["rank"],
        })
    out.sort(key=lambda r: (-(r["to_max_me_seconds"] + r["to_max_te_seconds"])))
    return {
        "ok": True,
        "scope": scope,
        "count": len(out),
        "blueprints": out,
        "max_level": MAX_LEVEL,
        "facility_count": len(facilities),
    }
