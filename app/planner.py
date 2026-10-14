"""生产规划：多级物料清单（BOM）展开 + 库存扣减 + 排产时间线。

核心是「逐层展开」而不是递归累加，这样可以自然地把同一个中间产物
在不同分支上的需求汇总到同一次生产上：

    第 0 层：目标产品
    第 1 层：目标产品的材料（决定「自己造」还是「直接买」）
    第 2 层：上一层里决定自己造的那些东西的材料
    ...

每层同一时间只需要跑一次，因此总工时用「每层取最大值再求和」估算：
同一层的多个任务可以并行占用多个工业槽位，跨层必须串行。
"""

from __future__ import annotations

import math

from . import config as cfgmod
from . import industry, utils


# --------------------------------------------------------------------------
# 蓝图查找
# --------------------------------------------------------------------------

def find_blueprint(store, product_type_id: int, activity: str = "manufacturing"):
    """返回 (blueprint_type_id, activity, 每流程产出数量)，找不到返回 (None, None, 0)。"""
    row = store.q1(
        "SELECT blueprint_type_id, quantity FROM activity_products"
        " WHERE product_type_id=? AND activity=? LIMIT 1",
        (int(product_type_id), activity))
    if row:
        return row["blueprint_type_id"], activity, row["quantity"] or 1
    return None, None, 0


def candidates_for_activity(store, type_ids, activity: str) -> set:
    type_ids = [int(i) for i in type_ids]
    out = set()
    for part in utils.chunked(type_ids, 400):
        marks = ",".join("?" for _ in part)
        for row in store.q(
                "SELECT DISTINCT product_type_id FROM activity_products"
                " WHERE activity=? AND product_type_id IN (%s)" % marks,
                [activity] + part):
            out.add(row["product_type_id"])
    return out


# --------------------------------------------------------------------------
# 展开
# --------------------------------------------------------------------------

def multi_level(store, root_type_id: int = None, root_blueprint_type_id: int = None,
                runs: int = 1, me: float = 10, te: float = 20,
                activity: str = "manufacturing", max_depth: int = 4,
                build_types=None, buy_types=None, prices: dict = None,
                market: dict = None, config: dict = None, bonuses: dict = None,
                inventory: dict = None, skill_levels: dict = None,
                cost_index: float = 0.0, use_inventory_intermediates: bool = True) -> dict:
    """展开多级物料清单。

    build_types / buy_types：明确指定「自己造」/「直接买」的物品集合；
    未指定时按「有制造/反应蓝图且层数未超限」自动判断。
    """
    config = config or {}
    prices = prices or {}
    market = market or {}
    inventory = inventory or {}
    bonuses = bonuses or {}
    build_types = set(int(i) for i in build_types) if build_types else None
    buy_types = set(int(i) for i in (buy_types or []))

    # 定位根蓝图
    if root_blueprint_type_id:
        blueprint_type_id = int(root_blueprint_type_id)
        activity = store.activity_of_blueprint(blueprint_type_id) or activity
        products = store.products_of(blueprint_type_id, activity)
        root_type_id = products[0]["product_type_id"] if products else root_type_id
        per_run = products[0]["quantity"] if products else 1
    elif root_type_id:
        blueprint_type_id, activity, per_run = store.blueprint_for_product(int(root_type_id))
        if blueprint_type_id is None:
            return {"ok": False, "message": "该物品没有制造或反应配方", "base_materials": [],
                    "jobs": [], "tree": None}
    else:
        return {"ok": False, "message": "必须指定目标产品或蓝图", "base_materials": [],
                "jobs": [], "tree": None}

    runs = max(1, utils.as_int(runs, 1))
    rounding = config.get("material_rounding", "ceil")
    material_factor = utils.num(bonuses.get("material_factor"), 1.0)
    time_factor = utils.num(bonuses.get("time_factor"), 1.0)
    cost_factor = utils.num(bonuses.get("cost_factor"), 1.0)
    warnings = []

    # 层 -> {type_id: 需要的数量}
    current = {int(root_type_id): runs * max(1, per_run)}
    root_build_jobs = []
    jobs = []
    base = {}
    tree = {"type_id": int(root_type_id), "depth": 0, "needed": current[int(root_type_id)],
            "runs": runs, "blueprint_type_id": blueprint_type_id,
            "activity": activity, "children": [], "built": True}
    tree_by_type = {int(root_type_id): tree}
    depth_reports = []
    visited_depth = {}

    def decide_build(type_id: int, depth: int) -> bool:
        if type_id in buy_types:
            return False
        if build_types is not None:
            return type_id in build_types
        if depth >= max_depth:
            return False
        bp_id, act, per = store.blueprint_for_product(type_id)
        if bp_id is None:
            return False
        # 只在有制造或反应配方时才考虑自造
        return act in ("manufacturing", "reaction")

    def type_lookup(type_ids):
        return store.types_by_ids(type_ids)

    for depth in range(0, max_depth + 1):
        if not current:
            break
        next_level = {}
        level_jobs = []
        types_info = type_lookup(list(current.keys()))
        for type_id, needed in sorted(current.items()):
            needed = utils.as_int(needed)
            if needed <= 0:
                continue
            # 库存直接抵扣中间产物
            on_hand = utils.as_int(inventory.get(type_id)) if use_inventory_intermediates else 0
            if depth > 0 and on_hand > 0:
                used = min(on_hand, needed)
                needed -= used
                inventory[type_id] = on_hand - used
                if needed <= 0:
                    node = tree_by_type.get(type_id)
                    if node is not None:
                        node["from_inventory"] = used
                        node["runs"] = 0
                    continue

            should_build = decide_build(type_id, depth) if depth > 0 or True else False
            if depth == 0:
                should_build = True

            if not should_build:
                base[type_id] = base.get(type_id, 0) + needed
                continue

            bp_id, act, per_run_quantity = store.blueprint_for_product(type_id)
            if bp_id is None:
                base[type_id] = base.get(type_id, 0) + needed
                continue
            per_run_quantity = max(1, utils.as_int(per_run_quantity, 1))
            job_runs = int(math.ceil(needed / per_run_quantity))
            produced = job_runs * per_run_quantity
            surplus = produced - needed

            activities = store.blueprint_activities(bp_id)
            payload = activities.get(act) or {}
            skill_factor, skill_details = industry.skill_time_factor(
                act, skill_levels or {}, config)
            base_time = payload.get("time") or 0
            time_seconds = industry.job_time_seconds(
                base_time, job_runs, te, time_factor, skill_factor)

            materials = []
            eiv = 0.0
            for material in payload.get("materials") or []:
                qty = industry.me_adjusted_quantity(
                    material["quantity"], job_runs, me, material_factor, act, rounding)
                materials.append({"type_id": material["type_id"], "quantity": qty,
                                  "base_quantity": material["quantity"]})
                # EIV 用 ME0 基准量（实测与 ME / 建筑 / rig 无关）
                adjusted = utils.num((prices.get(material["type_id"]) or {}).get("adjusted_price"))
                eiv += adjusted * utils.as_int(material["quantity"]) * job_runs
                key = material["type_id"]
                next_level[key] = next_level.get(key, 0) + qty

            cost_breakdown = industry.job_cost_breakdown(
                round(eiv), cost_index, act, base_time, job_runs,
                scc_surcharge=utils.num(config.get("scc_surcharge", 0.04)),
                cost_factor=cost_factor,
                tax_rate=utils.num(config.get("job_tax_rate", 0.0)),
                alpha_tax=utils.num(config.get("alpha_clone_tax", 0.0)),
                include_tax=bool(config.get("include_job_tax", True)),
                include_cost=bool(config.get("include_job_cost", True)))

            job = {
                "depth": depth,
                "blueprint_type_id": bp_id,
                "product_type_id": type_id,
                "product_name": (types_info.get(type_id) or {}).get("name"),
                "activity": act,
                "activity_name": cfgmod.ACTIVITIES.get(act, act),
                "runs": job_runs,
                "quantity_needed": needed,
                "quantity_per_run": per_run_quantity,
                "quantity_produced": produced,
                "surplus": surplus,
                "me": me, "te": te,
                "time_seconds": time_seconds,
                "time_text": utils.fmt_duration(time_seconds),
                "job_cost": cost_breakdown["total"],
                "job_cost_breakdown": cost_breakdown,
                "estimated_item_value": eiv,
                "materials": materials,
                "skill_time_factor": skill_factor,
                "skill_time_reduction": 1.0 - skill_factor,
                "skill_time_details": skill_details,
            }
            jobs.append(job)
            level_jobs.append(job)
            node = tree_by_type.get(type_id)
            if node is None:
                node = {"type_id": type_id, "depth": depth, "needed": needed,
                        "blueprint_type_id": bp_id, "activity": act, "children": [],
                        "runs": job_runs, "built": True}
                tree_by_type[type_id] = node
            else:
                node.update({"needed": needed, "runs": job_runs, "built": True})
            for material in materials:
                child = tree_by_type.get(material["type_id"])
                if child is None:
                    child = {"type_id": material["type_id"], "depth": depth + 1,
                             "needed": material["quantity"], "children": [],
                             "built": None, "runs": None}
                    tree_by_type[material["type_id"]] = child
                    node["children"].append(child)
                else:
                    child["needed"] = child.get("needed", 0) + material["quantity"]
                    if child not in node["children"]:
                        node["children"].append(child)
            if surplus:
                warnings.append("「%s」多生产了 %d 个（每流程产出 %d，批次取整）"
                                % (job["product_name"] or type_id, surplus, per_run_quantity))

        depth_reports.append({
            "depth": depth,
            "jobs": len(level_jobs),
            "time_seconds": max([j["time_seconds"] for j in level_jobs], default=0),
            "job_cost": sum(j["job_cost"] for j in level_jobs),
        })
        current = next_level

    # 超过层数上限的剩余需求全部按采购处理
    for type_id, needed in current.items():
        base[type_id] = base.get(type_id, 0) + needed

    # ---------------- 基础材料明细 ----------------
    base_rows = []
    total_material_cost = 0.0
    total_material_volume = 0.0
    for type_id, quantity in base.items():
        type_row = store.type_by_id(type_id) or {}
        price_row = prices.get(type_id) or {}
        market_row = market.get(type_id) or {}
        unit_price, source = industry.resolve_price(price_row, market_row, config, type_id)
        on_hand = utils.as_int(inventory.get(type_id))
        shortfall = max(0, quantity - on_hand)
        cost = unit_price * quantity
        cost_shortfall = unit_price * shortfall
        volume = utils.num(type_row.get("volume"))
        total_material_cost += cost
        total_material_volume += volume * quantity
        base_rows.append({
            "type_id": type_id,
            "name": type_row.get("name") or ("类型 %d" % type_id),
            "group_id": type_row.get("group_id"),
            "category_id": type_row.get("category_id"),
            "quantity": quantity,
            "inventory": on_hand,
            "shortfall": shortfall,
            "unit_price": unit_price,
            "price_source": source,
            "cost": cost,
            "cost_shortfall": cost_shortfall,
            "volume": volume,
            "total_volume": volume * quantity,
        })
    base_rows.sort(key=lambda r: -r["cost"])

    total_job_cost = sum(j["job_cost"] for j in jobs)
    # 中间产物已经被上面计入 base 的排除，这里避免重复计算：
    # 自造部分只额外付出「任务费用」，材料费已在 base_rows 里。
    total_cost = total_material_cost + total_job_cost
    total_time = sum(r["time_seconds"] for r in depth_reports)
    total_job_time = sum(j["time_seconds"] for j in jobs)

    return {
        "ok": True,
        "root_type_id": int(root_type_id),
        "root_name": (store.type_by_id(int(root_type_id)) or {}).get("name"),
        "root_blueprint_type_id": blueprint_type_id,
        "activity": activity,
        "runs": runs,
        "me": me, "te": te,
        "base_materials": base_rows,
        "jobs": jobs,
        "depth_reports": depth_reports,
        "tree": tree,
        "totals": {
            "material_cost": total_material_cost,
            "job_cost": total_job_cost,
            "total_cost": total_cost,
            "materials": len(base_rows),
            "jobs": len(jobs),
            "material_volume": total_material_volume,
            "time_seconds": total_time,
            "time_text": utils.fmt_duration(total_time),
            "total_job_time_seconds": total_job_time,
            "total_job_time_text": utils.fmt_duration(total_job_time),
            "shortfall_cost": sum(r["cost_shortfall"] for r in base_rows),
        },
        "warnings": warnings[:40],
        "notes": [
            "总工时为「每层取最大耗时后求和」的估算（同层可并行、跨层必须串行）。",
            "批次取整会让部分中间产物多生产，多余数量已列在提示里。",
        ],
    }


# --------------------------------------------------------------------------
# 库存缺口
# --------------------------------------------------------------------------

def shopping_list(plan: dict, include_surplus: bool = True) -> dict:
    """把基础材料整理成采购清单（含缺口与体积）。"""
    rows = []
    for row in plan.get("base_materials") or []:
        if row["shortfall"] <= 0 and not include_surplus:
            continue
        rows.append({
            "type_id": row["type_id"],
            "name": row["name"],
            "needed": row["quantity"],
            "inventory": row["inventory"],
            "buy": row["shortfall"],
            "unit_price": row["unit_price"],
            "cost": row["cost_shortfall"],
            "volume": row["volume"] * row["shortfall"],
        })
    rows.sort(key=lambda r: -r["cost"])
    return {"rows": rows,
            "total_cost": sum(r["cost"] for r in rows),
            "total_volume": sum(r["volume"] for r in rows),
            "items": len(rows)}


# --------------------------------------------------------------------------
# 现有工业项目的排产视图
# --------------------------------------------------------------------------

def active_job_timeline(store, scopes=None, character_id=None) -> dict:
    """把所有未完成工业项目按结束时间排成时间线，并汇总占用与产出。"""
    scopes = list(scopes or [])
    if character_id is not None and not scopes:
        scopes = ["char:%d" % int(character_id)]
    jobs = []
    for scope in scopes:
        for job in store.jobs_of(scope):
            status = (job.get("status") or "").lower()
            if status not in ("active", "paused", "ready"):
                continue
            jobs.append({**job, "scope": scope})
    type_ids = set()
    for job in jobs:
        type_ids.add(job.get("product_type_id"))
        type_ids.add(job.get("blueprint_type_id"))
    type_ids.discard(None)
    names = store.type_names(type_ids)
    now = utils.now()
    out = []
    for job in jobs:
        end_ts = utils.parse_esi_time(job.get("end_date"))
        start_ts = utils.parse_esi_time(job.get("start_date"))
        remaining = (end_ts - now) if end_ts else None
        out.append({
            "job_id": job["job_id"],
            "scope": job["scope"],
            "product_type_id": job.get("product_type_id"),
            "product_name": names.get(job.get("product_type_id")),
            "blueprint_type_id": job.get("blueprint_type_id"),
            "blueprint_name": names.get(job.get("blueprint_type_id")),
            "activity": job.get("activity"),
            "activity_name": cfgmod.ACTIVITIES.get(job.get("activity"), job.get("activity")),
            "runs": job.get("runs"),
            "status": job.get("status"),
            "facility_id": job.get("facility_id"),
            "start_date": job.get("start_date"),
            "end_date": job.get("end_date"),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "remaining_seconds": remaining,
            "remaining_text": utils.fmt_duration(remaining) if remaining and remaining > 0 else None,
            "cost": job.get("cost"),
            "probability": job.get("probability"),
        })
    out.sort(key=lambda j: (j.get("end_ts") or 0))
    by_activity = {}
    for job in out:
        key = job["activity"] or "unknown"
        by_activity[key] = by_activity.get(key, 0) + 1
    return {
        "jobs": out,
        "count": len(out),
        "by_activity": by_activity,
        "last_end_ts": max([j["end_ts"] for j in out if j.get("end_ts")], default=None),
        "total_cost": sum(utils.num(j.get("cost")) for j in out),
    }
