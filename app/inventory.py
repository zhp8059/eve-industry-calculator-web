"""角色 / 军团的库存、蓝图、工业项目拉取。

对应 ESI：
    GET /characters/{id}/assets/                  esi-assets.read_assets.v1
    GET /corporations/{id}/assets/                esi-assets.read_corporation_assets.v1
    GET /characters/{id}/blueprints/              esi-characters.read_blueprints.v1
    GET /corporations/{id}/blueprints/            esi-corporations.read_blueprints.v1
    GET /characters/{id}/industry/jobs/           esi-industry.read_character_jobs.v1
    GET /corporations/{id}/industry/jobs/         esi-industry.read_corporation_jobs.v1
                                                   （需要 总监 或 工厂经理 角色）

地点名解析：星系用本地 SDE，NPC 空间站用 /universe/names/，
玩家建筑用 /universe/structures/{id}/（需要 esi-universe.read_structures.v1
且角色对该建筑有停靠权限，否则会 403 —— 这时界面上显示为「未知建筑」）。
"""

from __future__ import annotations

from . import config as cfgmod
from . import store as storemod
from . import utils


class IndustryData:
    def __init__(self, store, esi, config: dict):
        self.store = store
        self.esi = esi
        self.config = config

    # ------------------------------------------------------------------
    # scope 帮助
    # ------------------------------------------------------------------
    @staticmethod
    def char_scope(character_id) -> str:
        return "char:%d" % int(character_id)

    @staticmethod
    def corp_scope(corporation_id) -> str:
        return "corp:%d" % int(corporation_id)

    def character(self, character_id):
        row = self.store.get_character(int(character_id))
        if row is None:
            raise ValueError("角色 %d 尚未登录" % int(character_id))
        return row

    # ------------------------------------------------------------------
    # 工业项目
    # ------------------------------------------------------------------
    def fetch_character_jobs(self, character_id: int) -> dict:
        character_id = int(character_id)
        data = self.esi.get_paged("/characters/%d/industry/jobs/" % character_id,
                                  params={"include_completed": "true"},
                                  character_id=character_id)
        rows, dropped = self._drop_terminal_jobs(data)
        scope = self.char_scope(character_id)
        self.store.replace_jobs(scope, rows)
        utils.log("角色 %s：%d 个工业项目%s", character_id, len(rows),
                  ("（已自动清除 %d 个已交付项目）" % dropped) if dropped else "")
        return {"scope": scope, "count": len(rows), "jobs": self.store.jobs_of(scope)}

    def fetch_corporation_jobs(self, corporation_id: int, character_id: int) -> dict:
        corporation_id = int(corporation_id)
        data = self.esi.get_paged("/corporations/%d/industry/jobs/" % corporation_id,
                                  params={"include_completed": "true"},
                                  character_id=int(character_id))
        rows, dropped = self._drop_terminal_jobs(data)
        scope = self.corp_scope(corporation_id)
        self.store.replace_jobs(scope, rows)
        utils.log("军团 %s：%d 个工业项目%s", corporation_id, len(rows),
                  ("（已自动清除 %d 个已交付项目）" % dropped) if dropped else "")
        return {"scope": scope, "count": len(rows), "jobs": self.store.jobs_of(scope)}

    # 这些状态属于「历史」，不再存进本地（用户要求：已经完成的项目自动清除）。
    #
    # 只清 delivered，**故意保留 ready** —— ready 是「已经做完但还没领取」的项目，
    # 你不去点它就一直占着工位，必须留在列表里提醒你去收。
    TERMINAL_JOB_STATUSES = ("delivered",)

    def _drop_terminal_jobs(self, data) -> tuple:
        """把已交付的项目滤掉，返回 (保留的行, 滤掉的数量)。"""
        all_rows = [self._normalize_job(item) for item in (data or [])]
        kept = [r for r in all_rows
                if (r.get("status") or "").lower() not in self.TERMINAL_JOB_STATUSES]
        return kept, len(all_rows) - len(kept)

    @staticmethod
    def _normalize_job(item: dict) -> dict:
        activity_id = item.get("activity_id")
        return {
            "job_id": item.get("job_id"),
            "installer_id": item.get("installer_id"),
            "facility_id": item.get("facility_id"),
            "station_id": item.get("station_id"),
            "activity_id": activity_id,
            "activity": storemod.ACTIVITY_ID_TO_KEY.get(activity_id, "activity_%s" % activity_id),
            "blueprint_id": item.get("blueprint_id"),
            "blueprint_type_id": item.get("blueprint_type_id"),
            "blueprint_location_id": item.get("blueprint_location_id"),
            "product_type_id": item.get("product_type_id"),
            "runs": item.get("runs"),
            "licensed_runs": item.get("licensed_runs"),
            "successful_runs": item.get("successful_runs"),
            "probability": item.get("probability"),
            "status": item.get("status"),
            "duration": item.get("duration"),
            "cost": item.get("cost"),
            "start_date": item.get("start_date"),
            "end_date": item.get("end_date"),
            "completed_date": item.get("completed_date"),
            "completed_character_id": item.get("completed_character_id"),
        }

    # ------------------------------------------------------------------
    # 蓝图
    # ------------------------------------------------------------------
    def fetch_character_blueprints(self, character_id: int) -> dict:
        character_id = int(character_id)
        data = self.esi.get_paged("/characters/%d/blueprints/" % character_id,
                                  character_id=character_id)
        scope = self.char_scope(character_id)
        self.store.replace_blueprints(scope, data or [])
        return {"scope": scope, "count": len(data or [])}

    def fetch_corporation_blueprints(self, corporation_id: int, character_id: int) -> dict:
        corporation_id = int(corporation_id)
        data = self.esi.get_paged("/corporations/%d/blueprints/" % corporation_id,
                                  character_id=int(character_id))
        scope = self.corp_scope(corporation_id)
        self.store.replace_blueprints(scope, data or [])
        return {"scope": scope, "count": len(data or [])}

    # ------------------------------------------------------------------
    # 库存
    # ------------------------------------------------------------------
    def fetch_character_assets(self, character_id: int) -> dict:
        character_id = int(character_id)
        data = self.esi.get_paged("/characters/%d/assets/" % character_id,
                                  character_id=character_id)
        scope = self.char_scope(character_id)
        self.store.replace_assets(scope, data or [])
        utils.log("角色 %s：%d 件库存物品", character_id, len(data or []))
        return {"scope": scope, "count": len(data or [])}

    def fetch_corporation_assets(self, corporation_id: int, character_id: int) -> dict:
        corporation_id = int(corporation_id)
        data = self.esi.get_paged("/corporations/%d/assets/" % corporation_id,
                                  character_id=int(character_id))
        scope = self.corp_scope(corporation_id)
        self.store.replace_assets(scope, data or [])
        utils.log("军团 %s：%d 件库存物品", corporation_id, len(data or []))
        return {"scope": scope, "count": len(data or [])}

    # ------------------------------------------------------------------
    # 库存汇总
    # ------------------------------------------------------------------
    def inventory_totals(self, scopes=None, character_id=None) -> dict:
        """把若干 scope 的库存按 type_id 汇总。"""
        if scopes is None:
            scopes = []
            if character_id is not None:
                scopes.append(self.char_scope(character_id))
        totals = {}
        for scope in scopes:
            for type_id, quantity in self.store.asset_totals(scope).items():
                totals[type_id] = totals.get(type_id, 0) + utils.as_int(quantity)
        return totals

    def inventory_view(self, scope: str, query: str = "", limit: int = 500,
                       location_id=None) -> dict:
        sql = ("SELECT a.type_id, SUM(a.quantity) AS quantity, COUNT(*) AS stacks,"
               " MAX(a.location_id) AS location_id, MIN(a.location_flag) AS location_flag"
               " FROM assets a")
        params = [scope]
        where = ["a.scope=?"]
        if query:
            where.append("EXISTS (SELECT 1 FROM types t WHERE t.type_id=a.type_id AND t.name LIKE ?)")
            params.append("%%%s%%" % query.replace("%", ""))
        if location_id:
            where.append("a.location_id=?")
            params.append(int(location_id))
        sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY a.type_id ORDER BY SUM(a.quantity) DESC LIMIT ?"
        params.append(int(limit))
        rows = [dict(r) for r in self.store.q(sql, params)]
        names = self.store.type_names([r["type_id"] for r in rows])
        types = self.store.types_by_ids([r["type_id"] for r in rows])
        locations = [r["location_id"] for r in rows if r.get("location_id")]
        location_names = self.location_names(locations)
        # 资产所在地点常是玩家建筑（id ≥ 1e12），/universe/names/ 不认它们，
        # 得走 /universe/structures/{id}/（需要停靠权限）。以前这里漏了这一步，
        # 库存的「位置」列就一直显示成一串裸 ID。
        unresolved = {int(i) for i in locations
                      if i and int(i) >= 1_000_000_000_000 and not location_names.get(int(i))}
        if unresolved:
            viewer = None
            if scope and str(scope).startswith("char:"):
                try:
                    viewer = int(str(scope).split(":", 1)[1])
                except ValueError:
                    viewer = None
            if viewer is None:
                active = self.store.active_character()
                viewer = active["character_id"] if active else None
            if viewer is not None:
                try:
                    resolved = self.resolve_structures(unresolved, character_id=viewer)
                    location_names.update({k: v for k, v in resolved.items() if v})
                except Exception as exc:
                    utils.log("解析库存建筑名失败（不影响其余数据）：%s", exc)
        total_volume = 0.0
        for row in rows:
            type_row = types.get(row["type_id"]) or {}
            row["name"] = names.get(row["type_id"])
            row["volume"] = utils.num(type_row.get("volume"))
            row["packaged_volume"] = utils.num(type_row.get("packaged_volume"))
            row["group_id"] = type_row.get("group_id")
            row["category_id"] = type_row.get("category_id")
            row["total_volume"] = row["volume"] * utils.as_int(row["quantity"])
            total_volume += row["total_volume"]
            row["location_name"] = location_names.get(row.get("location_id")) \
                or self._fallback_location_label(row.get("location_id"))
        return {"scope": scope, "rows": rows, "total_volume": total_volume,
                "meta": self.store.assets_meta(scope)}

    @staticmethod
    def _fallback_location_label(location_id):
        """地点名解析不出来时的显示：别丢一个裸 ID，说清是「未解析」。

        物资放在**集装箱里**时 location_id 是那个集装箱的物品 ID（不是建筑/空间站），
        这类 ID 本来就查不到名字 —— 所以文案也覆盖这种情况。
        """
        if not location_id:
            return None
        try:
            value = int(location_id)
        except (TypeError, ValueError):
            return str(location_id)
        if value >= 1_000_000_000_000:
            return "建筑 %d（未解析）" % value
        return "位置 %d（未解析，可能是集装箱内的物品）" % value

    # ------------------------------------------------------------------
    # 地点名
    # ------------------------------------------------------------------
    def location_names(self, ids) -> dict:
        ids = [int(i) for i in (ids or []) if i]
        if not ids:
            return {}
        known = self.store.location_names(ids)
        missing = [i for i in ids if i not in known]
        if not missing:
            return known
        # NPC 空间站 / 星系名可以走 /universe/names/
        resolved = self.esi.names_for(missing)
        for key, value in resolved.items():
            known[int(key)] = value
        self.store.kv_set("location_names", {str(k): v for k, v in known.items()})
        return known

    def refresh_corp_structures(self, corporation_id: int, character_id: int = None) -> dict:
        """从 ESI 读取**军团建筑**：在线服务装备、燃料到期时间、状态。

        需要 esi-corporations.read_structures.v1（已包含在授权里），
        且角色需有 总监 / 空间站经理 之类职位，否则 ESI 返回 403。

        读到的服务装备是**英文名**，按关键词映射回 SDE 里的服务装备，
        映射不到的会原样列出来（不静默忽略，否则燃料会被少算）。
        """
        cid = int(corporation_id)
        rows = self.esi.get_paged("/corporations/%d/structures/" % cid,
                                  character_id=character_id)
        if not isinstance(rows, list):
            return {"ok": False, "message": "ESI 没有返回建筑列表（可能缺少职位或 scope）",
                    "structures": []}
        store_rows = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            services = []
            for svc in item.get("services") or []:
                if not isinstance(svc, dict):
                    continue
                name = str(svc.get("name") or "")
                services.append({"name": name, "state": svc.get("state") or "",
                                 "module_type_id": cfgmod.match_service_module(name)})
            store_rows.append((
                utils.as_int(item.get("structure_id")), cid,
                item.get("name"), utils.as_int(item.get("type_id")),
                utils.as_int(item.get("system_id")), utils.jdumps(services),
                item.get("fuel_expires"), item.get("state"),
                item.get("state_timer_end"), item.get("unanchors_at"),
            ))
        self.store.put_corp_structures(store_rows)
        return {"ok": True, "count": len(store_rows),
                "structures": self.store.corp_structures(cid)}

    def resolve_structures(self, structure_ids, character_id=None) -> dict:
        """逐个解析玩家建筑名（无权限会 403，安静跳过）。

        ⚠️ 这一行 `def` 曾经被误删过（v0.8.0 加入 refresh_corp_structures 时），
        于是整个函数体变成上一个方法里 `return` 之后的死代码 —— 调用处抛
        AttributeError，却被 jobs_view 的 except 吞掉，表现为**工业项目的地点
        永远显示成「设施 1051…」**。tests/test_tokens.py 里有一条测试专门守着它。
        """
        out = {}
        rows = []
        for structure_id in {int(i) for i in (structure_ids or []) if i}:
            try:
                data = self.esi.get_data("/universe/structures/%d/" % structure_id,
                                         character_id=character_id)
            except Exception as exc:
                utils.log("解析建筑 %s 失败：%s", structure_id, exc)
                continue
            out[structure_id] = data.get("name")
            rows.append((structure_id, data.get("name"), data.get("solar_system_id"),
                         data.get("type_id"), data.get("owner_id")))
        if rows:
            self.store.put_structures(rows)
        return out

    def resolve_stations(self, station_ids) -> dict:
        ids = [int(i) for i in (station_ids or []) if i]
        if not ids:
            return {}
        out = {}
        rows = []
        for station_id in ids:
            try:
                data = self.esi.get_data("/universe/stations/%d/" % station_id)
            except Exception:
                continue
            out[station_id] = data.get("name")
            rows.append((station_id, data.get("name"), data.get("system_id"),
                         data.get("type_id")))
        if rows:
            self.store.put_stations(rows)
        return out

    def ensure_system_names(self, system_ids) -> dict:
        """星系名优先用本地 SDE，缺失的走 ESI。"""
        system_ids = [int(i) for i in (system_ids or []) if i]
        if not system_ids:
            return {}
        local = self.store.systems_by_ids(system_ids)
        out = {sid: row.get("name") for sid, row in local.items()}
        missing = [i for i in system_ids if i not in out]
        for system_id in missing:
            try:
                data = self.esi.get_data("/universe/systems/%d/" % system_id)
                out[system_id] = data.get("name")
                self.store.insert_solar_systems([(
                    system_id, data.get("name"), data.get("name"),
                    utils.num(data.get("security_status")), None,
                    data.get("constellation_id"))])
            except Exception:
                pass
        return out

    # ------------------------------------------------------------------
    # 军团角色检查
    # ------------------------------------------------------------------
    def character_roles(self, character_id: int) -> dict:
        """读取角色在军团中的职位，用于提前告诉用户军团数据为什么拿不到。

        已核实的角色要求（ESI OpenAPI）：
            军团工业项目 /corporations/{id}/industry/jobs/  -> Factory_Manager
            军团蓝图     /corporations/{id}/blueprints/     -> Director
            军团资产     /corporations/{id}/assets/         -> Director
        """
        try:
            data = self.esi.get_data("/characters/%d/roles/" % int(character_id),
                                     character_id=int(character_id))
        except Exception as exc:
            return {"ok": False, "message": str(exc), "roles": [], "can_read_corp_industry": None}
        roles = list(data.get("roles") or [])
        role_set = set(roles)
        is_director = "Director" in role_set
        is_factory = "Factory_Manager" in role_set
        can_industry = is_director or is_factory
        can_assets = is_director
        missing = []
        if not can_industry:
            missing.append("军团工业项目需要「总监」或「工厂经理」")
        if not can_assets:
            missing.append("军团资产/蓝图需要「总监」")
        return {
            "ok": True,
            "roles": roles,
            "is_director": is_director,
            "is_factory_manager": is_factory,
            "can_read_corp_industry": can_industry,
            "can_read_corp_assets": can_assets,
            "can_read_corp_blueprints": is_director,
            "missing": missing,
            "note": ("；".join(missing) + "。"
                     if missing else "该角色具备读取军团工业与资产数据的职位。"),
            "requirements": {
                "corp_industry_jobs": "Factory_Manager 或 Director",
                "corp_blueprints": "Director",
                "corp_assets": "Director",
            },
        }

    def character_skills(self, character_id: int) -> dict:
        """读取技能等级（用于自动填充工业技能减免）。没有该 scope 时返回空。"""
        try:
            data = self.esi.get_data("/characters/%d/skills/" % int(character_id),
                                     character_id=int(character_id))
        except Exception as exc:
            return {"ok": False, "message": str(exc), "levels": {}}
        levels = {}
        for skill in data.get("skills") or []:
            levels[skill.get("skill_id")] = skill.get("active_skill_level")
        return {"ok": True, "levels": levels, "total_sp": data.get("total_sp")}

    def character_location(self, character_id: int) -> dict:
        try:
            return {"ok": True, "data": self.esi.get_data(
                "/characters/%d/location/" % int(character_id), character_id=int(character_id))}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    # ------------------------------------------------------------------
    # 汇总：把所有相关数据一次性拉全
    # ------------------------------------------------------------------
    def refresh_all(self, character_id: int, include_corporation: bool = True,
                    include_assets: bool = True) -> dict:
        character_id = int(character_id)
        character = self.character(character_id)
        report = {"character_id": character_id, "steps": []}

        def step(name, fn):
            try:
                result = fn()
                report["steps"].append({"name": name, "ok": True, "result": _brief(result)})
                return result
            except Exception as exc:
                report["steps"].append({"name": name, "ok": False, "error": str(exc)})
                return None

        step("角色工业项目", lambda: self.fetch_character_jobs(character_id))
        step("角色蓝图", lambda: self.fetch_character_blueprints(character_id))
        if include_assets:
            step("角色库存", lambda: self.fetch_character_assets(character_id))
        step("角色职位", lambda: self.character_roles(character_id))
        step("角色技能", lambda: self.character_skills(character_id))

        corporation_id = character.get("corporation_id")
        if include_corporation and corporation_id:
            step("军团工业项目", lambda: self.fetch_corporation_jobs(corporation_id, character_id))
            step("军团蓝图", lambda: self.fetch_corporation_blueprints(corporation_id, character_id))
            if include_assets:
                step("军团库存", lambda: self.fetch_corporation_assets(corporation_id, character_id))
        return report

    # ------------------------------------------------------------------
    def scope_summary(self, character_id: int) -> dict:
        character_id = int(character_id)
        character = self.store.get_character(character_id) or {}
        corp_id = character.get("corporation_id")
        out = {
            "character": {
                "scope": self.char_scope(character_id),
                "jobs": self.store.jobs_meta(self.char_scope(character_id)),
                "blueprints": self.store.blueprints_meta(self.char_scope(character_id)),
                "assets": self.store.assets_meta(self.char_scope(character_id)),
            },
            "corporation": None,
        }
        if corp_id:
            out["corporation"] = {
                "corporation_id": corp_id,
                "name": character.get("corporation_name"),
                "scope": self.corp_scope(corp_id),
                "jobs": self.store.jobs_meta(self.corp_scope(corp_id)),
                "blueprints": self.store.blueprints_meta(self.corp_scope(corp_id)),
                "assets": self.store.assets_meta(self.corp_scope(corp_id)),
            }
        return out

    # ------------------------------------------------------------------
    def jobs_view(self, scope: str, status: str = None) -> dict:
        """工业项目列表（带名称、地点、剩余时间）。"""
        jobs = self.store.jobs_of(scope)
        if status and status != "all":
            jobs = [j for j in jobs if (j.get("status") or "").lower() == status.lower()]
        type_ids = set()
        for job in jobs:
            for key in ("blueprint_type_id", "product_type_id"):
                if job.get(key):
                    type_ids.add(job[key])
        names = self.store.type_names(type_ids)
        types = self.store.types_by_ids(type_ids)
        location_ids = set()
        for job in jobs:
            if job.get("facility_id"):
                location_ids.add(job["facility_id"])
            if job.get("station_id"):
                location_ids.add(job["station_id"])
        location_names = self.location_names(location_ids)
        # /universe/names/ 不认玩家建筑（id ≥ 1e12），得走 /universe/structures/{id}/，
        # 需要 esi-universe.read_structures.v1 且**该角色对建筑有停靠权限**。
        # 没有权限就 403，安静跳过（保持「设施 1029…」这种降级显示）。
        unresolved = {int(i) for i in location_ids
                      if i and int(i) >= 1_000_000_000_000 and not location_names.get(int(i))}
        if unresolved:
            # 解析建筑名要用一个**对建筑有权限**的角色令牌：个人 scope 用自己，
            # 军团 scope 用当前登录的那位（军团建筑通常只有成员的令牌能读到）。
            viewer = None
            if scope.startswith("char:"):
                try:
                    viewer = int(scope.split(":", 1)[1])
                except ValueError:
                    viewer = None
            if viewer is None:
                active = self.store.active_character()
                viewer = active["character_id"] if active else None
            if viewer is not None:
                try:
                    resolved = self.resolve_structures(unresolved, character_id=viewer)
                    location_names.update({k: v for k, v in resolved.items() if v})
                except Exception as exc:
                    utils.log("解析玩家建筑名失败（不影响其余数据）：%s", exc)
        now = utils.now()
        out = []
        for job in jobs:
            type_row = types.get(job.get("product_type_id")) or {}
            blueprint_row = types.get(job.get("blueprint_type_id")) or {}
            end_ts = utils.parse_esi_time(job.get("end_date"))
            remaining = (end_ts - now) if end_ts else None
            out.append({
                **job,
                "blueprint_name": blueprint_row.get("name") or names.get(job.get("blueprint_type_id")),
                "product_name": type_row.get("name") or names.get(job.get("product_type_id")),
                "product_volume": utils.num(type_row.get("volume")),
                "facility_name": location_names.get(job.get("facility_id"))
                                 or location_names.get(job.get("station_id"))
                                 # 读不到名字时，别只丢一个裸 ID 出来 —— 说清是「没解析到」
                                 # 以及为什么（ESI 对没停靠权限的建筑会 403）。
                                 or ("建筑 %s（未解析）" % (job.get("facility_id") or job.get("station_id"))),
                "facility_resolved": bool(location_names.get(job.get("facility_id"))
                                           or location_names.get(job.get("station_id"))),
                "facility_id": job.get("facility_id") or job.get("station_id"),
                "remaining_seconds": remaining,
                "remaining_text": utils.fmt_duration(remaining) if remaining and remaining > 0 else None,
                "runs": job.get("runs"),
                "status": job.get("status"),
                "activity_name": _activity_label(job.get("activity")),
            })
        out.sort(key=lambda j: (j.get("end_date") or ""))
        summary = {}
        for job in out:
            key = job.get("status") or "unknown"
            summary[key] = summary.get(key, 0) + 1
        return {"scope": scope, "jobs": out, "count": len(out), "summary": summary}


def _activity_label(activity: str) -> str:
    return cfgmod.ACTIVITIES.get(activity, activity or "未知")


def _brief(result) -> dict:
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if k in ("count", "scope", "ok", "roles",
                                                         "can_read_corp_industry", "total_sp")}
    return {"value": result}
