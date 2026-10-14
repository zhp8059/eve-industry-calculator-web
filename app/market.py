"""市场数据：调整价、盘口（最低卖/最高买）、历史成交、成本指数、工业设施。

设计取舍：
  * /markets/prices/ 一次请求就能拿到全部物品的调整价与平均价 —— 便宜，作为默认价源。
  * 吉他的「最低卖单 / 最高买单」必须按物品逐个查 /markets/{region}/orders/，
    因此默认只在需要时按需刷新，并把结果落到 market_stats 里缓存。
  * 利润排行如果对成千上万个物品逐个查盘口会非常费，所以
    默认只用调整价/平均价（零额外请求），需要精确时再点「刷新盘口」。
"""

from __future__ import annotations

from . import config as cfgmod
from . import industry, utils


def _brief(result) -> dict:
    """把各步返回压成一行摘要，用于界面展示。"""
    if isinstance(result, dict):
        return {k: v for k, v in result.items()
                if k in ("ok", "entries", "updated", "rows", "types", "systems",
                         "cached", "fetched", "count")}
    return {}


class MarketData:
    def __init__(self, store, esi, config: dict):
        self.store = store
        self.esi = esi
        self.config = config


    # ------------------------------------------------------------------
    # 调整价 / 平均价
    # ------------------------------------------------------------------
    def ensure_prices(self, force: bool = False, max_age: int = 3600) -> dict:
        meta = self.store.prices_meta()
        if (not force and meta["entries"] and meta["updated_at"]
                and utils.now() - meta["updated_at"] < max_age):
            return {"ok": True, "cached": True, **meta}
        try:
            data = self.esi.get_data("/markets/prices/", cache=not force,
                                     max_age=0 if force else None)
        except Exception as exc:
            return {"ok": False, "message": str(exc), **meta}
        rows = [(item.get("type_id"), item.get("adjusted_price"), item.get("average_price"))
                for item in (data or []) if item.get("type_id")]
        self.store.put_prices(rows)
        meta = self.store.prices_meta()
        utils.log("调整价已更新：%s 条", len(rows))
        return {"ok": True, "updated": len(rows), **meta}

    def price_rows(self, type_ids) -> dict:
        type_ids = [int(i) for i in type_ids]
        out = {}
        for part in utils.chunked(type_ids, 900):
            marks = ",".join("?" for _ in part)
            for row in self.store.q(
                    "SELECT * FROM prices WHERE type_id IN (%s)" % marks, part):
                out[row["type_id"]] = dict(row)
        return out

    # ------------------------------------------------------------------
    # 盘口
    # ------------------------------------------------------------------
    def ensure_market_stats(self, type_ids, region_id: int = None, force: bool = False,
                            max_age: int = 1200, max_pages: int = 3) -> dict:
        """为指定物品刷新最低卖价 / 最高买价，写入 market_stats。"""
        region_id = int(region_id or self.config.get("region_id") or 10000002)
        type_ids = sorted({int(i) for i in type_ids})
        cached = self.store.get_market_stats(type_ids, region_id)
        todo = [t for t in type_ids
                if force or t not in cached
                or (utils.now() - utils.as_int(cached[t].get("updated_at"))) > max_age]
        if not todo:
            return {"ok": True, "cached": len(cached), "fetched": 0, "stats": cached}

        results = {}
        errors = []

        def fetch(type_id):
            try:
                return type_id, self._order_book(type_id, region_id, max_pages)
            except Exception as exc:
                errors.append("%s: %s" % (type_id, exc))
                return type_id, None

        for type_id, book in self.esi.map_concurrent(fetch, todo):
            results[type_id] = book

        rows = []
        for type_id, book in results.items():
            if not book:
                continue
            rows.append((type_id, region_id, book["buy"], book["sell"],
                         book["buy_volume"], book["sell_volume"], "esi-orders"))
        if rows:
            try:
                self.store.put_market_stats(rows)
            except Exception as exc:
                utils.log("写入缓存失败（不影响本次计算）：%s", exc)
        cached = self.store.get_market_stats(type_ids, region_id)
        out = {"ok": True, "cached": len(cached), "fetched": len(rows), "stats": cached}
        if errors:
            out["errors"] = errors[:10]
        return out

    def _order_book(self, type_id: int, region_id: int, max_pages: int = 3) -> dict:
        """取某物品在一个星域的最低卖价、最高买价与挂单量。

        买卖分开请求（每侧最多 max_pages 页），保证不会把买单当成卖单。
        """
        best_sell = None
        best_buy = None
        sell_volume = 0
        buy_volume = 0
        for side in ("sell", "buy"):
            try:
                orders = self.esi.get_paged("/markets/%d/orders/" % region_id,
                                            params={"type_id": int(type_id),
                                                    "order_type": side},
                                            max_pages=max_pages)
            except Exception as exc:
                utils.log("读取 %s 的 %s 挂单失败：%s", type_id, side, exc)
                orders = []
            for order in orders or []:
                price = utils.num(order.get("price"))
                remaining = utils.as_int(order.get("volume_remain"))
                if not price:
                    continue
                if side == "buy":
                    if best_buy is None or price > best_buy:
                        best_buy = price
                    buy_volume += remaining
                else:
                    if best_sell is None or price < best_sell:
                        best_sell = price
                    sell_volume += remaining
        return {"sell": best_sell, "buy": best_buy,
                "sell_volume": sell_volume, "buy_volume": buy_volume}

    def order_book_full(self, type_id: int, region_id: int = None, max_pages: int = 10) -> dict:
        """完整盘口（供「价格对比」页使用）。"""
        region_id = int(region_id or self.config.get("region_id") or 10000002)
        params = {"type_id": int(type_id)}
        orders = []
        try:
            orders = self.esi.get_paged("/markets/%d/orders/" % region_id, params=params,
                                        max_pages=max_pages)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        sells = sorted([o for o in orders if not o.get("is_buy_order")],
                       key=lambda o: utils.num(o.get("price")))
        buys = sorted([o for o in orders if o.get("is_buy_order")],
                      key=lambda o: -utils.num(o.get("price")))
        return {
            "ok": True,
            "type_id": int(type_id),
            "region_id": region_id,
            "sell": [{"price": utils.num(o.get("price")),
                      "volume": utils.as_int(o.get("volume_remain")),
                      "location_id": o.get("location_id")} for o in sells[:25]],
            "buy": [{"price": utils.num(o.get("price")),
                     "volume": utils.as_int(o.get("volume_remain")),
                     "location_id": o.get("location_id")} for o in buys[:25]],
            "lowest_sell": utils.num(sells[0].get("price")) if sells else None,
            "highest_buy": utils.num(buys[0].get("price")) if buys else None,
            "orders": len(orders),
        }

    # ------------------------------------------------------------------
    # 历史成交
    # ------------------------------------------------------------------
    def ensure_history(self, type_id: int, region_id: int = None, force: bool = False) -> dict:
        region_id = int(region_id or self.config.get("region_id") or 10000002)
        try:
            data = self.esi.get_data("/markets/%d/history/" % region_id,
                                     params={"type_id": int(type_id)})
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        rows = data or []
        if rows:
            try:
                self.store.put_market_history(type_id, region_id, rows)
            except Exception as exc:
                utils.log("写入缓存失败（不影响本次计算）：%s", exc)
        return {"ok": True, "rows": len(rows)}

    def history_prices(self, type_ids, region_id: int = None, days: int = 7) -> dict:
        region_id = int(region_id or self.config.get("region_id") or 10000002)
        out = {}
        for type_id in type_ids:
            value = self.store.history_average(int(type_id), region_id, days)
            if value:
                out[int(type_id)] = value
        return out

    # ------------------------------------------------------------------
    # 成本指数 / 工业设施
    # ------------------------------------------------------------------
    def ensure_cost_indices(self, force: bool = False, max_age: int = 3600) -> dict:
        checked = self.store.kv_get("cost_indices_at") or 0
        if not force and checked and utils.now() - checked < max_age:
            return {"ok": True, "cached": True}
        try:
            data = self.esi.get_data("/industry/systems/", cache=not force)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        rows = []
        for system in data or []:
            system_id = system.get("solar_system_id")
            # /industry/systems/ 的 cost_indices 是数组：[{activity, cost_index}, ...]
            indices = system.get("cost_indices") or []
            if isinstance(indices, dict):          # 兼容万一返回字典的情况
                indices = [{"activity": k, "cost_index": v} for k, v in indices.items()]
            for entry in indices:
                activity = entry.get("activity")
                if activity is None:
                    continue
                rows.append((system_id, activity, utils.num(entry.get("cost_index"))))
        try:
            self.store.put_cost_indices(rows)
        except Exception as exc:
            utils.log("写入缓存失败（不影响本次计算）：%s", exc)
        self.store.kv_set("cost_indices_at", utils.now())
        utils.log("成本指数已更新：%s 个星系 / %s 条", len(data or []), len(rows))
        return {"ok": True, "systems": len(data or []), "rows": len(rows)}

    def ensure_facilities(self, force: bool = False, max_age: int = 86400) -> dict:
        checked = self.store.kv_get("facilities_at") or 0
        if not force and checked and utils.now() - checked < max_age:
            return {"ok": True, "cached": True}
        try:
            data = self.esi.get_data("/industry/facilities/", cache=not force)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        rows = [(item.get("facility_id"), item.get("type_id"), item.get("solar_system_id"),
                 item.get("region_id")) for item in (data or [])]
        try:
            self.store.put_facilities(rows)
        except Exception as exc:
            utils.log("写入缓存失败（不影响本次计算）：%s", exc)
        self.store.kv_set("facilities_at", utils.now())
        return {"ok": True, "rows": len(rows)}

    # ------------------------------------------------------------------
    # 批量抓取 / 本地缓存概览 / 统一刷新
    # ------------------------------------------------------------------
    def ensure_history_bulk(self, type_ids, region_id: int = None, progress=None) -> dict:
        """批量抓取历史成交（每个物品一次请求，带进度回调）。"""
        region_id = int(region_id or self.config.get("region_id") or 10000002)
        type_ids = sorted({int(i) for i in type_ids})
        rows_total = 0
        errors = []
        for index, type_id in enumerate(type_ids, 1):
            if progress and (index == 1 or index % 5 == 0 or index == len(type_ids)):
                progress("历史成交 %d/%d" % (index, len(type_ids)))
            try:
                data = self.esi.get_data("/markets/%d/history/" % region_id,
                                         params={"type_id": type_id})
                if data:
                    rows_total += self.store.put_market_history(type_id, region_id, data)
            except Exception as exc:
                errors.append("%s: %s" % (type_id, exc))
        return {"ok": True, "types": len(type_ids), "rows": rows_total, "errors": errors[:10]}

    def local_summary(self, region_id: int = None) -> dict:
        """本地已缓存的物价数据概览 —— 用来证明「确实存在本地了」。

        ⚠️ 这里的每个查询都**单独兜底**：数据库只要有一页损坏，这些统计查询就会抛
        `database disk image is malformed`，而它是界面启动时就要调的接口 ——
        以前会让整个接口 500、页面数据全刷不出来（用户报的「ESI 读取不成功」）。
        现在退化成「该项显示 0 + 一条警告」，页面照样能用，并在界面上说明数据库损坏。
        """
        region_id = int(region_id or self.config.get("region_id") or 10000002)
        warnings = []

        def safe(fn, label, default=None):
            try:
                return fn()
            except Exception as exc:
                warnings.append("%s 读取失败：%s" % (label, exc))
                return default

        prices = safe(self.store.prices_meta, "调整价") or {}
        # 注意：store.q1() 返回的是 sqlite3.Row（没有 .get()），必须先转成 dict
        stats_row = dict(safe(lambda: self.store.q1(
            "SELECT COUNT(*) n, MAX(updated_at) ts FROM market_stats WHERE region_id=?",
            (region_id,)), "盘口缓存") or {})
        hist_row = dict(safe(lambda: self.store.q1(
            "SELECT COUNT(*) n, COUNT(DISTINCT type_id) t, MIN(date) d0, MAX(date) d1"
            " FROM market_history WHERE region_id=?", (region_id,)), "历史成交") or {})
        region_name = None
        for item in cfgmod.REGIONS.values():
            if item["id"] == region_id:
                region_name = item["name"]
                break
        return {
            "ok": True,
            "region_id": region_id,
            "region_name": region_name,
            "now": utils.now(),
            "prices": {"entries": prices.get("entries") or 0,
                       "updated_at": prices.get("updated_at")},
            "market_stats": {"entries": stats_row.get("n") or 0,
                             "updated_at": stats_row.get("ts")},
            "history": {"rows": hist_row.get("n") or 0, "types": hist_row.get("t") or 0,
                        "from": hist_row.get("d0"), "to": hist_row.get("d1")},
            "cost_indices_at": safe(lambda: self.store.kv_get("cost_indices_at"), "成本指数时间"),
            "facilities_at": safe(lambda: self.store.kv_get("facilities_at"), "设施时间"),
            "db_bytes": self.store.size_bytes(),
            "cache": safe(self.store.cache_stats, "缓存统计") or {},
            "max_items": int(self.config.get("market_refresh_max_items", 250)),
            "warnings": warnings,
        }

    def refresh_all(self, scope: str = "essentials", type_ids=None, region_id: int = None,
                    progress=None, max_items: int = None) -> dict:
        """统一刷新入口：把 ESI 的市场物价拉下来并存到本地。

        scope:
            prices      只更新 CCP 调整价 / 平均价（1 次请求，最快）
            essentials  再加系统成本指数 + 工业设施
            orders      再加指定物品的吉他盘口（最低卖单 / 最高买单）
            history     再加指定物品的历史成交
            all         以上全部
        """
        region_id = int(region_id or self.config.get("region_id") or 10000002)
        limit = int(max_items or self.config.get("market_refresh_max_items", 250))
        type_ids = sorted({int(i) for i in (type_ids or [])})
        truncated = len(type_ids) > limit
        if truncated:
            type_ids = type_ids[:limit]
        steps = []

        def step(name, fn):
            if progress:
                progress(name)
            try:
                result = fn()
                steps.append({"name": name, "ok": True, "detail": _brief(result)})
                return result
            except Exception as exc:
                steps.append({"name": name, "ok": False, "error": str(exc)})
                return None

        if scope in ("prices", "essentials", "orders", "history", "all"):
            step("CCP 调整价 / 平均价（全物品）", lambda: self.ensure_prices(force=True))
        if scope in ("essentials", "all"):
            step("系统成本指数", lambda: self.ensure_cost_indices(force=True))
            step("工业设施", lambda: self.ensure_facilities(force=True))
        if scope in ("orders", "all") and type_ids:
            step("吉他盘口 · %d 个物品" % len(type_ids),
                 lambda: self.ensure_market_stats(type_ids, region_id=region_id, force=True))
        if scope in ("history", "all") and type_ids:
            step("历史成交 · %d 个物品" % len(type_ids),
                 lambda: self.ensure_history_bulk(type_ids, region_id, progress))
        for label in ("吉他盘口", "历史成交"):
            if scope in ("orders", "history", "all") and not type_ids:
                steps.append({"name": label, "ok": True, "skipped": True,
                              "detail": {"skipped": "当前没有涉及任何物品，已跳过"}})

        return {
            "ok": True,
            "scope": scope,
            "region_id": region_id,
            "steps": steps,
            "requested_items": len(type_ids),
            "truncated": truncated,
            "max_items": limit,
            "summary": self.local_summary(region_id),
        }

    # ------------------------------------------------------------------
    # 利润排行
    # ------------------------------------------------------------------
    def candidates(self, market_group_id=None, category_id=None, group_id=None,
                   limit: int = 4000, activity: str = "manufacturing"):
        """候选产品：有该活动配方、且已发布的物品。"""
        sql = ("SELECT DISTINCT t.type_id, t.name, t.group_id, t.category_id, t.market_group_id,"
               " t.volume, t.packaged_volume, b.blueprint_type_id"
               " FROM types t JOIN activity_products b ON b.product_type_id = t.type_id"
               " WHERE b.activity=? AND t.published=1 AND t.market_group_id IS NOT NULL")
        params = [activity]
        if market_group_id:
            sql += " AND t.market_group_id=?"
            params.append(int(market_group_id))
        if category_id:
            sql += " AND t.category_id=?"
            params.append(int(category_id))
        if group_id:
            sql += " AND t.group_id=?"
            params.append(int(group_id))
        sql += " LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self.store.q(sql, params)]

    def profit_ranking(self, market_group_id=None, category_id=None, group_id=None,
                       limit: int = 100, min_price: float = 0.0, min_volume: int = 0,
                       sort: str = "margin", me: float = 10, te: float = 20,
                       activity: str = "manufacturing", refresh_market: bool = False,
                       region_id: int = None, scan_limit: int = 1500) -> dict:
        """按「自造成本 vs 市场卖价」排序，找利润率最高的产品。"""
        region_id = int(region_id or self.config.get("region_id") or 10000002)
        rows = self.candidates(market_group_id, category_id, group_id,
                               limit=scan_limit, activity=activity)
        if not rows:
            return {"ok": True, "rows": [], "scanned": 0,
                    "note": "没有找到候选产品；请先导入 SDE 或换一个分类。"}

        type_ids = {r["type_id"] for r in rows}
        # 收集所有需要的材料类型
        blueprint_ids = {r["blueprint_type_id"] for r in rows}
        material_ids = set()
        for part in utils.chunked(sorted(blueprint_ids), 400):
            marks = ",".join("?" for _ in part)
            for row in self.store.q(
                    "SELECT DISTINCT material_type_id FROM activity_materials"
                    " WHERE activity=? AND blueprint_type_id IN (%s)" % marks,
                    [activity] + part):
                material_ids.add(row["material_type_id"])

        self.ensure_prices()
        all_ids = sorted(type_ids | material_ids)
        prices = self.price_rows(all_ids)
        market = self.store.get_market_stats(sorted(type_ids), region_id)

        if refresh_market:
            # 只刷新候选产品自身的盘口（材料用调整价，避免上千次请求）
            need = [r["type_id"] for r in rows[:max(1, min(len(rows), 400))]]
            self.ensure_market_stats(need, region_id=region_id, force=True)
            market = self.store.get_market_stats(sorted(type_ids), region_id)

        cost_index = self.default_cost_index(activity)

        results = []
        for row in rows:
            type_id = row["type_id"]
            price_row = prices.get(type_id) or {}
            market_row = market.get(type_id) or {}
            unit_price, source = industry.resolve_price(price_row, market_row,
                                                        self.config, type_id)
            if unit_price < min_price:
                continue
            volume_avail = utils.as_int(market_row.get("sell_volume"))
            if min_volume and volume_avail < min_volume:
                continue
            detail = industry.product_breakdown(
                self.store, row["blueprint_type_id"], activity, me, te, 1,
                prices, market, self.config, None, None, cost_index)
            unit_cost = detail.get("unit_cost") or 0.0
            if unit_cost <= 0 or unit_price <= 0:
                continue
            profit = unit_price - unit_cost
            margin = profit / unit_cost
            results.append({
                "type_id": type_id,
                "name": row["name"],
                "blueprint_type_id": row["blueprint_type_id"],
                "group_id": row["group_id"],
                "category_id": row["category_id"],
                "market_group_id": row["market_group_id"],
                "me": me, "te": te,
                "unit_cost": unit_cost,
                "unit_price": unit_price,
                "price_source": source,
                "profit": profit,
                "margin": margin,
                "daily_volume": utils.as_int(market_row.get("sell_volume")),
                "volume": utils.num(row.get("volume")),
                "time_seconds": detail.get("time_seconds") or 0,
                "time_text": utils.fmt_duration(detail.get("time_seconds") or 0),
                "material_cost": detail.get("material_cost") or 0.0,
                "job_cost": detail.get("job_cost") or 0.0,
            })

        keys = {
            "margin": lambda r: -r["margin"],
            "profit": lambda r: -r["profit"],
            "isk_per_hour": lambda r: -(r["profit"] / (r["time_seconds"] / 3600.0)
                                        if r["time_seconds"] else 0),
            "cost": lambda r: r["unit_cost"],
            "price": lambda r: -r["unit_price"],
            "volume": lambda r: -r["daily_volume"],
        }
        results.sort(key=keys.get(sort, keys["margin"]))
        for item in results:
            item["isk_per_hour"] = (item["profit"] / (item["time_seconds"] / 3600.0)
                                   if item["time_seconds"] else 0.0)
        return {"ok": True, "rows": results[:max(1, int(limit))],
                "scanned": len(rows), "matched": len(results),
                "region_id": region_id, "sort": sort,
                "price_note": ("材料按调整价计算，产品按「%s」；"
                               "调整价每日更新，适合快速筛选而非精确报价。"
                               % self.config.get("price_source"))}

    def default_cost_index(self, activity: str) -> float:
        """排行榜用：取所有星系里该活动成本指数的中位数，避免依赖具体星系。"""
        key = cfgmod.ACTIVITY_TO_COST_INDEX.get(activity, activity)
        rows = self.store.q("SELECT cost_index FROM cost_indices WHERE activity=?", (key,))
        values = sorted(utils.num(r["cost_index"]) for r in rows)
        if not values:
            return 0.0
        return values[len(values) // 2]

    # ------------------------------------------------------------------
    # 分类浏览
    # ------------------------------------------------------------------
    def market_group_detail(self, group_id: int, limit: int = 300) -> dict:
        """某个市场分组的：子分组（含递归物品数）+ 直接属于它的物品（含蓝图原本）。

        游戏里的市场树就是 market_groups 的层级（本地 2114 个分组、19957 个物品有归属），
        所以这里不需要任何 ESI 读取 —— 打开界面就能浏览。
        """
        group_id = int(group_id)
        groups = [dict(r) for r in self.store.q(
            "SELECT market_group_id, name, name_en, parent_id, icon_id FROM market_groups")]
        by_id = {g["market_group_id"]: g for g in groups}
        children = {}
        for g in groups:
            children.setdefault(g.get("parent_id") or 0, []).append(g)
        for kids in children.values():
            kids.sort(key=lambda x: x.get("name") or "")

        # 每个分组「自己 + 所有后代」的物品数：先把直接挂在分组上的数量查出来，
        # 再自底向上累加（2114 个分组，内存里算比递归 SQL 更简单也更快）。
        direct = {r["market_group_id"]: r["n"] for r in self.store.q(
            "SELECT market_group_id, COUNT(*) n FROM types"
            " WHERE published=1 AND market_group_id IS NOT NULL GROUP BY market_group_id")}

        def subtree_count(node_id, cache={}):
            if node_id in cache:
                return cache[node_id]
            total = direct.get(node_id, 0)
            for kid in children.get(node_id, []):
                total += subtree_count(kid["market_group_id"], cache)
            cache[node_id] = total
            return total

        kids = [{"market_group_id": k["market_group_id"], "name": k.get("name"),
                 "name_en": k.get("name_en"), "icon_id": k.get("icon_id"),
                 "types": subtree_count(k["market_group_id"])}
                for k in children.get(group_id, [])]

        rows = [dict(r) for r in self.store.q(
            "SELECT type_id, name, name_en, group_id, category_id, volume FROM types"
            " WHERE published=1 AND market_group_id=? ORDER BY name LIMIT ?",
            (group_id, int(limit)))]
        type_ids = [r["type_id"] for r in rows]
        blueprints = {}
        if type_ids:
            marks = ",".join("?" for _ in type_ids)
            for r in self.store.q(
                    "SELECT product_type_id p, MIN(blueprint_type_id) b FROM activity_products"
                    " WHERE activity='manufacturing' AND product_type_id IN (%s)"
                    " GROUP BY product_type_id" % marks, type_ids):
                blueprints[r["p"]] = r["b"]
        bp_ids = [v for v in blueprints.values() if v]
        bp_names = {}
        if bp_ids:
            marks = ",".join("?" for _ in bp_ids)
            for r in self.store.q(
                    "SELECT type_id, name FROM types WHERE type_id IN (%s)" % marks, bp_ids):
                bp_names[r["type_id"]] = r["name"]
        for row in rows:
            bp = blueprints.get(row["type_id"])
            row["blueprint_type_id"] = bp
            row["blueprint_name"] = bp_names.get(bp)

        group = by_id.get(group_id)
        return {
            "ok": True,
            "group": {"market_group_id": group_id,
                      "name": (group or {}).get("name"),
                      "name_en": (group or {}).get("name_en"),
                      "parent_id": (group or {}).get("parent_id")},
            "children": kids,
            "types": rows,
            "count": subtree_count(group_id),
            "total": len(rows),
        }

    def market_group_tree(self):
        rows = [dict(r) for r in self.store.q(
            "SELECT * FROM market_groups ORDER BY name")]
        roots = [r for r in rows if not r.get("parent_id")]
        return {"all": rows, "roots": roots}

    def types_in_category(self, category_id: int, limit: int = 200):
        return [dict(r) for r in self.store.q(
            "SELECT t.type_id, t.name, t.group_id, g.name AS group_name, t.volume"
            " FROM types t LEFT JOIN groups g ON g.group_id=t.group_id"
            " WHERE t.category_id=? AND t.published=1 ORDER BY g.name, t.name LIMIT ?",
            (int(category_id), int(limit)))]

    def category_summary(self):
        return [dict(r) for r in self.store.q(
            "SELECT c.category_id, c.name, COUNT(t.type_id) AS types"
            " FROM categories c LEFT JOIN types t ON t.category_id=c.category_id AND t.published=1"
            " WHERE c.published=1 GROUP BY c.category_id ORDER BY c.name")]
