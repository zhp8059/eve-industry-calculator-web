"""本地 HTTP 服务：提供 JSON API 与前端静态文件。

只用标准库 http.server（ThreadingHTTPServer），因此不需要任何依赖安装。
所有接口都在 /api/ 下；静态文件来自 app/web/。
CORS 不需要 —— 前端由同一个服务提供，同源。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import APP_NAME, APP_ID, __version__
from . import config as cfgmod
from . import industry, market as marketmod, planner, research, secret_check, secure
from . import utils
from .esi import ESI, ESIError
from .inventory import IndustryData
from .market import MarketData
from .planner import multi_level
from .sde import SDE
from .sso import SSOError, TokenManager
from .store import Store

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json",
}


class Request:
    def __init__(self, method, path, query, body_bytes, headers, client):
        self.method = method
        self.path = path
        self.query = query
        self.raw_body = body_bytes or b""
        self.headers = headers
        self.client = client
        self.json = {}
        if self.raw_body:
            try:
                self.json = json.loads(self.raw_body.decode("utf-8")) or {}
            except Exception:
                self.json = {}

    def q(self, key, default=None):
        values = self.query.get(key)
        if not values:
            return default
        return values[0]

    def qi(self, key, default=None):
        value = self.q(key)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except Exception:
            return default

    def qf(self, key, default=None):
        value = self.q(key)
        if value is None or value == "":
            return default
        try:
            return float(value)
        except Exception:
            return default

    def qbool(self, key, default=False):
        value = self.q(key)
        if value is None:
            return default
        return str(value).lower() in ("1", "true", "yes", "on")

    def body(self, key, default=None):
        return self.json.get(key, default)


class App:
    """把各个模块组装在一起。"""

    def __init__(self, config: dict):
        self.config = config
        cfgmod.ensure_dirs()
        # 令牌加密要先配置好，再做任何落库/迁移动作
        secure.configure(config)
        self.store = Store(cfgmod.DB_PATH)
        # 启动迁移：把明文/旧信封的令牌换成当前加密格式，并把解不开的行标出来
        try:
            self.token_migration = self.store.prepare_stored_tokens()
        except Exception as exc:
            utils.log("令牌格式迁移失败（不影响启动）：%r", exc)
            self.token_migration = {"error": repr(exc)}
        self.esi = ESI(self.store, config)
        self.sso = TokenManager(self.store, config)
        self.esi.set_token_manager(self.sso)
        self.sde = SDE(self.store, config)
        self.market = MarketData(self.store, self.esi, config)
        self.data = IndustryData(self.store, self.esi, config)
        self.tasks = {}
        self._task_lock = threading.Lock()
        self.started = utils.now()

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------
    def run_task(self, name: str, fn, *args, **kwargs) -> dict:
        with self._task_lock:
            current = self.tasks.get(name)
            if current and current.get("running"):
                return {"ok": False, "message": "任务「%s」正在运行中" % name}

            state = {"name": name, "running": True, "started": utils.now(),
                     "finished": None, "result": None, "error": None, "progress": ""}

            def progress(message):
                state["progress"] = message

            self.tasks[name] = state

        def worker():
            try:
                state["result"] = fn(*args, progress=progress, **kwargs)
            except Exception as exc:
                state["error"] = "%s: %s" % (type(exc).__name__, exc)
                utils.log("任务 %s 失败：%s\n%s", name, exc, traceback.format_exc())
            finally:
                state["running"] = False
                state["finished"] = utils.now()

        threading.Thread(target=worker, name="task-%s" % name, daemon=True).start()
        return {"ok": True, "task": name, "state": self.task_state(name)}

    def task_state(self, name: str = None):
        if name:
            return self.tasks.get(name)
        return {k: v for k, v in self.tasks.items()}

    # ------------------------------------------------------------------
    # 上下文构建（价格 / 设施加成 / 库存 / 技能）
    # ------------------------------------------------------------------
    def effective_config(self, payload: dict) -> dict:
        cfg = dict(self.config)
        industry_over = (payload or {}).get("industry") or {}
        for key in ("scc_surcharge", "job_tax_rate", "structure_material_bonus",
                    "structure_time_bonus", "structure_cost_bonus", "industry_skill_level",
                    "advanced_industry_level", "reaction_skill_level",
                    "material_rounding", "include_job_cost", "include_job_tax"):
            if industry_over.get(key) is not None:
                cfg[key] = industry_over[key]
        price_over = (payload or {}).get("price") or {}
        if price_over.get("source"):
            cfg["price_source"] = price_over["source"]
        if price_over.get("custom_prices"):
            cfg["custom_prices"] = price_over["custom_prices"]
        region = (payload or {}).get("region_id")
        if region:
            cfg["region_id"] = int(region)
        return cfg

    def facility_context(self, payload: dict, activity: str, cfg: dict,
                         product_row: dict = None) -> dict:
        """把界面上的「生产设施」设置转成加成倍率。"""
        facility = (payload or {}).get("facility") or {}
        structure_type_id = facility.get("structure_type_id")
        rig_type_ids = facility.get("rig_type_ids") or []
        system_id = facility.get("system_id")
        if system_id is None:
            system_id = self.store.kv_get("current_system_id")
        industry_over = (payload or {}).get("industry") or {}
        manual = {
            "material": industry_over.get("structure_material_bonus"),
            "time": industry_over.get("structure_time_bonus"),
            "cost": industry_over.get("structure_cost_bonus"),
        }
        bonuses = industry.facility_bonuses(
            self.store, structure_type_id, rig_type_ids, activity,
            system_id=system_id,
            security_override=facility.get("security") or cfg.get("security_modifier"),
            manual=manual,
            product_category_id=(product_row or {}).get("category_id"),
            product_group_id=(product_row or {}).get("group_id"),
            product_type_id=(product_row or {}).get("type_id"))
        cost_index = industry.cost_index_for(self.store, system_id, activity)
        if industry_over.get("cost_index") is not None:
            cost_index = utils.num(industry_over["cost_index"])
        bonuses["system_id"] = system_id
        bonuses["cost_index"] = cost_index
        return bonuses

    def bonus_resolver(self, payload: dict, cfg: dict):
        """返回一个函数：按活动 + 产品算出对应的设施加成（改装件作用范围随产品变化）。"""
        cache = {}

        def resolve(activity: str, product_row: dict = None):
            key = (activity, (product_row or {}).get("type_id"),
                   (product_row or {}).get("group_id"), (product_row or {}).get("category_id"))
            if key not in cache:
                cache[key] = self.facility_context(payload, activity, cfg, product_row)
            return cache[key]

        return resolve

    def skill_levels(self, payload: dict, character_id=None) -> dict:
        explicit = ((payload or {}).get("industry") or {}).get("skill_levels")
        if isinstance(explicit, dict) and explicit:
            return {int(k): v for k, v in explicit.items()}
        character_id = character_id or (payload or {}).get("character_id")
        if not character_id:
            active = self.store.active_character()
            character_id = active["character_id"] if active else None
        if not character_id:
            return {}
        cached = self.store.kv_get("skills:%s" % character_id)
        if cached and utils.now() - cached.get("_at", 0) < 3600:
            return {int(k): v for k, v in (cached.get("levels") or {}).items()}
        result = self.data.character_skills(int(character_id))
        if result.get("ok"):
            payload_out = {"levels": result["levels"], "_at": utils.now()}
            self.store.kv_set("skills:%s" % character_id, payload_out)
            return {int(k): v for k, v in result["levels"].items()}
        return {}

    def inventory_context(self, payload: dict, cfg: dict) -> tuple:
        inv_cfg = (payload or {}).get("inventory") or {}
        if inv_cfg.get("use") is False:
            return {}, None
        scopes = inv_cfg.get("scopes")
        if not scopes:
            scope = inv_cfg.get("scope")
            if scope:
                scopes = [scope]
        if not scopes:
            character_id = (payload or {}).get("character_id")
            if character_id:
                scopes = [self.data.char_scope(character_id)]
                character = self.store.get_character(character_id)
                if character and character.get("corporation_id") and inv_cfg.get("include_corporation", True):
                    scopes.append(self.data.corp_scope(character["corporation_id"]))
        if not scopes:
            active = self.store.active_character()
            if active:
                scopes = [self.data.char_scope(active["character_id"])]
        if not scopes:
            return {}, None
        return self.data.inventory_totals(scopes), scopes

    def price_context(self, type_ids, cfg: dict, market_scope: str = "products",
                      product_ids=None) -> dict:
        """准备价格与盘口。返回 {prices, market, history, warnings}。"""
        type_ids = sorted({int(i) for i in type_ids if i})
        warnings = []
        self.market.ensure_prices()
        prices = self.market.price_rows(type_ids)
        region_id = int(cfg.get("region_id") or 10000002)
        market = {}
        source = cfg.get("price_source") or "jita_sell"
        wants_market = source in ("jita_sell", "jita_buy", "history")
        if wants_market:
            if market_scope == "all":
                targets = type_ids
            elif market_scope == "none":
                targets = []
            else:
                targets = sorted({int(i) for i in (product_ids or []) if i})
            cap = 160
            if len(targets) > cap:
                warnings.append("需要查询盘口的物品有 %d 个，超出单次上限 %d 个，"
                                "超出部分改用调整价（可分批刷新）。" % (len(targets), cap))
                targets = targets[:cap]
            if targets:
                result = self.market.ensure_market_stats(targets, region_id=region_id)
                if result.get("errors"):
                    warnings.append("部分物品盘口查询失败：%s" % "; ".join(result["errors"][:3]))
            market = self.store.get_market_stats(type_ids, region_id)
        history = {}
        if source == "history":
            history = self.market.history_prices(type_ids, region_id, 7)
            if not history:
                warnings.append("本地没有 7 日历史数据，已回退到其它价源；"
                                "可在「价格对比」里刷新历史。")
        return {"prices": prices, "market": market, "history": history,
                "warnings": warnings, "region_id": region_id}

    # ------------------------------------------------------------------
    def build_calc_context(self, payload: dict, activity: str, type_ids, product_ids,
                           cfg: dict, product_rows=None) -> dict:
        market_scope = (payload or {}).get("market_scope") or "products"
        price_ctx = self.price_context(type_ids, cfg, market_scope, product_ids)
        first_product = None
        if product_rows:
            first_product = next((r for r in product_rows if r), None)
        bonuses = self.facility_context(payload, activity, cfg, first_product)
        inventory_totals, scopes = self.inventory_context(payload, cfg)
        skills = self.skill_levels(payload)
        return {
            "prices": price_ctx["prices"],
            "market": price_ctx["market"],
            "history": price_ctx["history"],
            "bonuses": bonuses,
            "inventory": inventory_totals,
            "scopes": scopes,
            "skill_levels": skills,
            "cost_index": bonuses.get("cost_index") or 0.0,
            "warnings": price_ctx["warnings"],
            "region_id": price_ctx["region_id"],
        }


# ==========================================================================
# 路由
# ==========================================================================
ROUTES = []


def route(method: str, pattern: str):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def decorator(fn):
        ROUTES.append((method.upper(), regex, fn))
        return fn
    return decorator


def json_response(data, status=200):
    return status, data, "application/json; charset=utf-8"


def text_response(text, status=200, content_type="text/plain; charset=utf-8"):
    return status, text, content_type


# --------------------------------------------------------------------------
# 状态 / 配置
# --------------------------------------------------------------------------

@route("GET", "/api/status")
def api_status(app: App, req: Request):
    sde = app.sde.status()
    return json_response({
        "ok": True,
        "app": {"name": APP_NAME, "id": APP_ID, "version": __version__,
                "uptime": utils.now() - app.started,
                "now": utils.now()},
        "config": cfgmod.public_config(app.config),
        "sde": sde,
        "prices": app.store.prices_meta(),
            # 数据库健康状态：损坏时要让界面明说，别让用户对着「刷不出数据」猜
            "db": {"integrity_ok": bool(app.store.integrity[0]),
                   "integrity_message": app.store.integrity[1],
                   "init_error": app.store.init_error},
        "cache": app.store.cache_stats(),
        "db_bytes": app.store.size_bytes(),
        "characters": app.store.public_characters(),
        "esi": app.esi.limits(),
        "tasks": app.task_state(),
        "sso": {"configured": app.sso.configured(),
                "callback": app.sso.redirect_uri(),
            # 端口相关：界面用它提示「改过端口就必须回门户改回调地址」
            "port": cfgmod.runtime_port(app.config),
            "default_port": cfgmod.DEFAULT_PORT,
            "port_is_default": cfgmod.runtime_port(app.config) == cfgmod.DEFAULT_PORT,
                # Client ID 不往前端送原值（内置时前端只显示「已内置」）
            "client_id": "***",
            "client_id_source": cfgmod.client_id_source(app.config),
            "has_client_id": True,   # 内置值永远可用，登录不会因缺 client_id 被挡
                "secret_encrypted": secret_check.encryption_info()},
        "token_migration": app.token_migration,
        "link_states": {row["character_id"]: row["link_state"]
                        for row in app.store.list_characters()},
        # 已保存的生产设施（建筑 / 改装件 / 星系 / 安等 / 计费参数）
        "facility": _saved_facility(app),
    })


def _saved_facility(app: App) -> dict:
    saved = app.config.get("facility")
    if not isinstance(saved, dict) or not saved:
        return {}
    return {**saved, "summary": facility_summary(app, saved)}


@route("POST", "/api/characters/{character_id}/confirm-relink")
def api_confirm_relink(app: App, req: Request, character_id):
    """确认「这个角色确实换了主人，我接受现状」。

    owner_hash 变化时程序会暂停该角色的同步（避免把旧主人的缓存数据当自己的用）。
    重新登录一次（此时 hash 已一致）会自动恢复；这个接口是给它一个不用重登的快捷方式。
    """
    character_id = int(character_id)
    row = app.store.get_character(character_id)
    if row is None:
        return json_response({"ok": False, "message": "没有这个角色"}, 404)
    state = row.get("link_state") or ""
    if state != cfgmod.LINK_OWNER_CHANGED:
        return json_response({"ok": False,
                              "message": "该角色当前状态是「%s」，不需要确认"
                                         % cfgmod.LINK_LABELS.get(state, state)}, 400)
    app.store.set_link_state(character_id, cfgmod.LINK_OK)
    utils.log("用户已确认角色 %s 的所有权变更，恢复同步", character_id)
    return json_response({"ok": True, "character_id": character_id,
                          "state": app.sso.token_state(character_id),
                          "characters": app.store.public_characters()})


@route("GET", "/api/selftest")
def api_selftest(app: App, req: Request):
    return json_response({
        "ok": True,
        "report": secret_check.full_report(app.store, app.config, app.sso.configured()),
        "secure": secret_check.encryption_info(),
        "scope_audit": secret_check.scope_audit(app.config),
        "esi_limits": app.esi.limits(),
    })


@route("POST", "/api/config")
def api_config_save(app: App, req: Request):
    updates = req.json or {}
    allowed = {"client_id", "client_secret", "use_pkce", "region_key", "region_id",
               "station_id", "price_source", "scc_surcharge", "job_tax_rate",
               "structure_material_bonus", "structure_time_bonus", "structure_cost_bonus",
               "industry_skill_level", "advanced_industry_level", "reaction_skill_level",
               "material_rounding", "include_job_cost", "custom_prices", "scopes",
               "concurrency", "open_browser", "user_agent", "sde_url", "port",
               "facility"}
    applied = {}
    for key, value in updates.items():
        if key not in allowed:
            continue
        if key == "facility":
            value = _clean_facility(value)
        elif key in ("scc_surcharge", "job_tax_rate", "structure_material_bonus",
                     "structure_time_bonus", "structure_cost_bonus"):
            value = utils.num(value)
        elif key in ("industry_skill_level", "advanced_industry_level",
                     "reaction_skill_level", "concurrency", "port"):
            value = utils.as_int(value)
        elif key == "use_pkce":
            value = bool(value)
        if isinstance(value, str) and value.strip() == "***":
            # 这是接口回显的掩码，不是真值 —— 别拿它当新值写进去
            # （客户端拿不到真值时会把 "***" 原样回传）
            continue
        if key == "client_id" and str(value or "").strip() == cfgmod.BUILTIN_CLIENT_ID:
            # 填的正好是内置值 → 等同于「用内置」，不落盘
            value = ""
        app.config[key] = value
        applied[key] = "***" if key == "client_secret" else value
    if app.config.get("region_key"):
        region = cfgmod.REGIONS.get(app.config["region_key"])
        if region:
            app.config["region_id"] = region["id"]
            if region.get("station_id"):
                app.config["station_id"] = region["station_id"]
    cfgmod.save_config(app.config)
    app.esi.config = app.config
    app.market.config = app.config
    app.data.config = app.config
    return json_response({"ok": True, "applied": applied,
                          "message": "配置已保存到 config.json"})


def _clean_facility(value) -> dict:
    """把界面送来的「生产设施」收窄成可安全写入 config.json 的形状。

    只保留已知字段并做类型转换 —— 避免任意 JSON 被塞进配置文件。
    """
    src = value if isinstance(value, dict) else {}
    if not any(src.get(k) for k in ("structure_type_id", "rig_type_ids", "system_id")):
        return {}          # 空设施 = 清除已保存的配置
    rig_ids = []
    for raw in (src.get("rig_type_ids") or [])[:cfgmod.STRUCTURE_RIG_SLOT_LIMIT]:
        try:
            rid = int(raw)
        except (TypeError, ValueError):
            continue
        if rid > 0 and rid not in rig_ids:
            rig_ids.append(rid)
    out = {
        "structure_type_id": utils.as_int(src.get("structure_type_id")) or None,
        "rig_type_ids": rig_ids,
        "service_type_ids": [utils.as_int(i) for i in (src.get("service_type_ids") or [])[:16]
                             if utils.as_int(i) > 0],
        "system_id": utils.as_int(src.get("system_id")) or None,
        "system_name": str(src.get("system_name") or "")[:80],
        "security": (str(src.get("security") or "")
                     if src.get("security") in ("highsec", "lowsec", "nullsec") else ""),
        "saved_at": utils.now(),
    }
    manual = src.get("manual")
    if isinstance(manual, dict):
        out["manual"] = {k: utils.num(manual.get(k)) for k in ("material", "time", "cost")
                         if manual.get(k) is not None}
    return out


def facility_summary(app: "App", facility: dict) -> str:
    """给界面显示的一行摘要（存的是 ID，展示要名字）。"""
    if not facility:
        return ""
    ids = [facility.get("structure_type_id")] + list(facility.get("rig_type_ids") or [])
    names = app.store.type_names([i for i in ids if i])
    parts = [names.get(facility.get("structure_type_id")) or "NPC 空间站"]
    for rig_id in facility.get("rig_type_ids") or []:
        parts.append(names.get(rig_id) or str(rig_id))
    if facility.get("system_name"):
        parts.append(facility["system_name"])
    if facility.get("security"):
        parts.append({"highsec": "高安", "lowsec": "低安",
                      "nullsec": "00/虫洞"}.get(facility["security"], ""))
    return " + ".join(p for p in parts if p)


@route("POST", "/api/config/reload")
def api_config_reload(app: App, req: Request):
    app.config = cfgmod.load_config()
    app.esi.config = app.config
    app.market.config = app.config
    app.data.config = app.config
    return json_response({"ok": True, "config": cfgmod.public_config(app.config)})


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------

@route("POST", "/api/auth/login")
def api_login(app: App, req: Request):
    if not app.sso.configured():
        return json_response({"ok": False, "need_setup": True,
                              "message": "没有可用的 client_id。程序已内置一个，"
                                         "若它被清空，请在「设置 → EVE 应用凭据」"
                                         "填入自己的 Client ID。"}, 400)
    host = req.body("host") or "localhost"
    redirect_uri = req.body("redirect_uri") or app.sso.redirect_uri(host)
    # 按需授权：默认完整档；想最小权限可以只请求个人数据那一档
    tier = (req.body("tier") or app.config.get("scope_tier") or "corporation").strip().lower()
    if tier not in cfgmod.SCOPE_TIERS:
        return json_response({"ok": False,
                              "message": "未知的授权档位：%s（可选 %s）"
                                         % (tier, " / ".join(cfgmod.SCOPE_TIERS))}, 400)
    try:
        info = app.sso.login_url(redirect_uri, tier=tier)
    except SSOError as exc:
        return json_response({"ok": False, "message": str(exc)}, 400)
    return json_response({"ok": True, **info, "tier": tier,
                          "tier_label": cfgmod.SCOPE_TIER_LABELS.get(tier, tier),
                          "hint": "若浏览器没有自动打开，请手动访问上面的 url"})


@route("GET", "/api/auth/pending")
def api_login_pending(app: App, req: Request):
    """有没有「点了授权但还没完成」的登录尝试（界面据此显示等待窗口）。"""
    return json_response({"ok": True, "configured": app.sso.configured(),
                          **app.sso.pending_login()})


@route("POST", "/api/auth/cancel")
def api_login_cancel(app: App, req: Request):
    dropped = app.sso.cancel_pending()
    return json_response({"ok": True, "dropped": dropped,
                          "message": "已取消 %d 次待完成的登录尝试" % dropped})


def _scope_status(scopes) -> dict:
    """把一个角色已授权的 scope 归纳成「能用哪些功能」。"""
    granted = set(scopes or [])
    char_missing = set(cfgmod.SCOPES_CHARACTER) - granted
    corp_missing = sorted(set(cfgmod.SCOPES_CORPORATION) - granted)
    if not char_missing and not corp_missing:
        tier = "corporation"
    elif not char_missing:
        tier = "character"        # 个人权限齐了，只是没给军团
    else:
        tier = "custom"           # 连个人权限都不全
    return {
        "missing_scopes": sorted(set(cfgmod.ALL_SCOPES) - granted),
        "corp_scopes_missing": corp_missing,
        "corp_ready": not corp_missing,
        "scope_tier": tier,
        "scope_tier_label": cfgmod.SCOPE_TIER_LABELS.get(tier, "自定义"),
    }


@route("GET", "/api/auth/characters")
def api_characters(app: App, req: Request):
    active = app.store.active_character()
    out = []
    for character in app.store.public_characters():
        out.append({**character,
                    "is_active": bool(active and active["character_id"] == character["character_id"]),
                    **_scope_status(character.get("scopes")),
                    "summary": app.data.scope_summary(character["character_id"])})
    return json_response({"ok": True, "characters": out,
                          "active": active["character_id"] if active else None})


@route("POST", "/api/auth/active")
def api_set_active(app: App, req: Request):
    character_id = req.body("character_id")
    if not character_id:
        return json_response({"ok": False, "message": "缺少 character_id"}, 400)
    app.store.set_active(int(character_id))
    return json_response({"ok": True})


@route("DELETE", "/api/auth/characters/{character_id}")
def api_delete_character(app: App, req: Request, character_id):
    character_id = int(character_id)
    token = app.store.refresh_token(character_id)
    if token and req.qbool("revoke", False):
        app.sso.revoke(token)
    scope = app.data.char_scope(character_id)
    app.store.clear_scope(scope)
    app.store.delete_character(character_id)
    return json_response({"ok": True, "message": "已移除角色及其本地缓存"})


@route("POST", "/api/auth/logout_all")
def api_logout_all(app: App, req: Request):
    for character in app.store.list_characters():
        token = app.store.refresh_token(character["character_id"])
        if token:
            app.sso.revoke(token)
    app.store.cache_clear()
    return json_response({"ok": True})


@route("POST", "/api/auth/manual")
def api_manual_code(app: App, req: Request):
    """回调地址被拒时的兜底：手动粘贴授权码（或整条回调 URL）。"""
    raw = (req.body("code") or "").strip()
    if not raw:
        return json_response({"ok": False, "message": "缺少授权码 code"}, 400)
    code = raw
    state = req.body("state")
    # 允许直接粘贴整条回调地址，从中取出 code / state
    if "code=" in raw:
        parsed = parse_qs(urlsplit(raw).query)
        code = (parsed.get("code") or [raw])[0]
        if not state:
            state = (parsed.get("state") or [None])[0]
    try:
        # 必须带上 state（或由服务端按唯一待处理尝试配对），
        # 否则 PKCE 的 code_verifier 会丢失，令牌交换必然失败。
        result = app.sso.exchange_code(code, state=state, verifier=req.body("verifier"))
    except SSOError as exc:
        return json_response({"ok": False, "message": str(exc)}, 400)
    try:
        app.data.character_roles(result["character_id"])
    except Exception:
        pass
    return json_response({"ok": True, **result})


@route("POST", "/api/auth/refresh")
def api_refresh_token(app: App, req: Request):
    character_id = req.body("character_id") or (app.store.active_character() or {}).get("character_id")
    if not character_id:
        return json_response({"ok": False, "message": "没有已登录的角色"}, 400)
    try:
        token = app.sso.token_for(int(character_id), force=True)
    except (SSOError, Exception) as exc:
        return json_response({"ok": False, "message": str(exc)}, 400)
    return json_response({"ok": True, "state": app.sso.token_state(int(character_id)),
                          "token_preview": token[:12] + "..."})


# --------------------------------------------------------------------------
# SDE
# --------------------------------------------------------------------------

@route("GET", "/api/sde/status")
def api_sde_status(app: App, req: Request):
    return json_response({"ok": True, **app.sde.status()})


@route("POST", "/api/sde/update")
def api_sde_update(app: App, req: Request):
    force = req.qbool("force", False) or bool(req.body("force"))
    if app.sde.busy():
        return json_response({"ok": False, "message": "SDE 更新已在运行中",
                              "progress": app.sde.progress()}, 409)
    app.run_task("sde", lambda progress: app.sde.update(force=force))
    return json_response({"ok": True, "message": "SDE 更新已在后台开始，可查看进度"})


@route("GET", "/api/sde/latest")
def api_sde_latest(app: App, req: Request):
    try:
        return json_response({"ok": True, **app.sde.latest_build()})
    except Exception as exc:
        return json_response({"ok": False, "message": str(exc)}, 502)


@route("GET", "/api/sde/search")
def api_sde_search(app: App, req: Request):
    query = req.q("q", "")
    limit = req.qi("limit", 30)
    results = app.store.search_types(query, limit)
    return json_response({"ok": True, "query": query, "results": results})


@route("GET", "/api/sde/search-blueprint")
def api_sde_search_blueprint(app: App, req: Request):
    query = req.q("q", "")
    activity = req.q("activity", "manufacturing")
    limit = req.qi("limit", 30)
    results = app.store.search_blueprints(query, limit, activity)
    return json_response({"ok": True, "query": query, "activity": activity, "results": results})


@route("GET", "/api/sde/market-group/{group_id}")
def api_sde_market_group(app: App, req: Request, group_id: str):
    """某个市场分组的子分组与物品（含每个物品的蓝图原本）。纯本地数据，无需登录。"""
    limit = req.qi("limit") or 300
    return json_response(app.market.market_group_detail(int(group_id), limit))


@route("GET", "/api/sde/categories")
def api_sde_categories(app: App, req: Request):
    return json_response({"ok": True, "categories": app.market.category_summary(),
                          "market_groups": app.market.market_group_tree()["roots"]})


@route("GET", "/api/sde/category/{category_id}")
def api_sde_category_types(app: App, req: Request, category_id):
    return json_response({"ok": True, "types": app.market.types_in_category(
        int(category_id), req.qi("limit", 200))})


@route("GET", "/api/sde/type/{type_id}")
def api_sde_type(app: App, req: Request, type_id):
    row = app.store.type_by_id(int(type_id))
    if not row:
        return json_response({"ok": False, "message": "SDE 中没有这个物品（可能未导入 SDE）"}, 404)
    blueprint_id, activity, per_run = app.store.blueprint_for_product(int(type_id))
    return json_response({"ok": True, "type": row, "blueprint_type_id": blueprint_id,
                          "activity": activity, "quantity_per_run": per_run})


@route("GET", "/api/sde/types")
def api_sde_types(app: App, req: Request):
    ids = [int(i) for i in (req.q("ids", "") or "").split(",") if i.strip().isdigit()]
    return json_response({"ok": True, "types": app.store.types_by_ids(ids)})


@route("GET", "/api/sde/blueprint/{blueprint_type_id}")
def api_sde_blueprint(app: App, req: Request, blueprint_type_id):
    activities = app.store.blueprint_activities(int(blueprint_type_id))
    if not activities:
        return json_response({"ok": False, "message": "SDE 中没有这个蓝图"}, 404)
    ids = set()
    for payload in activities.values():
        ids.update(m["type_id"] for m in payload.get("materials") or [])
        ids.update(p["type_id"] for p in payload.get("products") or [])
        ids.update(s["type_id"] for s in payload.get("skills") or [])
    names = app.store.type_names(ids)
    for activity, payload in activities.items():
        for key in ("materials", "products", "skills"):
            for entry in payload.get(key) or []:
                entry["name"] = names.get(entry["type_id"])
    return json_response({"ok": True, "blueprint_type_id": int(blueprint_type_id),
                          "activities": activities})


@route("GET", "/api/sde/bonus-sources")
def api_sde_bonus_sources(app: App, req: Request):
    activity = req.q("activity", "manufacturing")
    return json_response({"ok": True,
                          "candidates": industry.structure_bonus_candidates(app.store, activity)})


@route("GET", "/api/sde/bonus/{type_id}")
def api_sde_bonus(app: App, req: Request, type_id):
    activity = req.q("activity", "manufacturing")
    system_id = req.qi("system_id")
    bonuses = industry.facility_bonuses(app.store, int(type_id), [], activity,
                                       system_id=system_id)
    return json_response({"ok": True, "bonuses": bonuses,
                          "name": (app.store.type_by_id(int(type_id)) or {}).get("name")})


# --------------------------------------------------------------------------
# 角色数据
# --------------------------------------------------------------------------

@route("GET", "/api/characters/{character_id}/roles")
def api_roles(app: App, req: Request, character_id):
    return json_response({"ok": True, **app.data.character_roles(int(character_id))})


@route("GET", "/api/characters/{character_id}/skills")
def api_skills(app: App, req: Request, character_id):
    return json_response({"ok": True, **app.data.character_skills(int(character_id))})


@route("GET", "/api/characters/{character_id}/summary")
def api_char_summary(app: App, req: Request, character_id):
    return json_response({"ok": True, **app.data.scope_summary(int(character_id))})


@route("POST", "/api/characters/{character_id}/refresh")
def api_char_refresh(app: App, req: Request, character_id):
    include_corp = req.qbool("corporation", True)
    include_assets = req.qbool("assets", True)
    app.run_task("refresh-%s" % character_id,
                 lambda progress: _refresh_report(app, int(character_id), include_corp,
                                                  include_assets, progress))
    return json_response({"ok": True, "message": "正在后台拉取该角色的 ESI 数据"})


def _refresh_report(app, character_id, include_corp, include_assets, progress):
    steps = []

    def run(name, fn):
        progress(name)
        try:
            fn()
            steps.append({"name": name, "ok": True})
        except ESIError as exc:
            steps.append({"name": name, "ok": False, "error": str(exc),
                          "status": exc.status})
        except Exception as exc:
            steps.append({"name": name, "ok": False, "error": str(exc)})

    character = app.store.get_character(character_id) or {}
    run("角色工业项目", lambda: app.data.fetch_character_jobs(character_id))
    run("角色蓝图", lambda: app.data.fetch_character_blueprints(character_id))
    if include_assets:
        run("角色库存", lambda: app.data.fetch_character_assets(character_id))
    run("角色职位", lambda: app.data.character_roles(character_id))
    run("角色技能", lambda: app.data.character_skills(character_id))
    if include_corp and character.get("corporation_id"):
        corp_id = character["corporation_id"]
        run("军团工业项目", lambda: app.data.fetch_corporation_jobs(corp_id, character_id))
        run("军团蓝图", lambda: app.data.fetch_corporation_blueprints(corp_id, character_id))
        if include_assets:
            run("军团库存", lambda: app.data.fetch_corporation_assets(corp_id, character_id))
    # 顺便补齐军团名
    if character.get("corporation_id") and not character.get("corporation_name"):
        names = app.esi.names_for([character["corporation_id"]])
        if names.get(character["corporation_id"]):
            app.store.upsert_character({**character,
                                        "corporation_name": names[character["corporation_id"]]})
    return {"character_id": character_id, "steps": steps}


# --------------------------------------------------------------------------
# 工业项目
# --------------------------------------------------------------------------

@route("GET", "/api/jobs")
def api_jobs(app: App, req: Request):
    scope = req.q("scope")
    if not scope:
        character_id = req.qi("character_id")
        if not character_id:
            active = app.store.active_character()
            if not active:
                return json_response({"ok": False, "message": "还没有登录任何角色"}, 400)
            character_id = active["character_id"]
        scope = app.data.char_scope(character_id)
    view = app.data.jobs_view(scope, req.q("status"))
    timeline = planner.active_job_timeline(app.store, [scope])
    return json_response({"ok": True, **view, "timeline": timeline,
                          "meta": app.store.jobs_meta(scope)})


@route("GET", "/api/jobs/timeline")
def api_jobs_timeline(app: App, req: Request):
    scopes = [s for s in (req.q("scopes", "") or "").split(",") if s]
    if not scopes:
        active = app.store.active_character()
        if active:
            scopes = [app.data.char_scope(active["character_id"])]
            if active.get("corporation_id") and req.qbool("corporation", True):
                scopes.append(app.data.corp_scope(active["corporation_id"]))
    return json_response({"ok": True, **planner.active_job_timeline(app.store, scopes)})


@route("GET", "/api/blueprints")
def api_blueprints(app: App, req: Request):
    scope = req.q("scope")
    if not scope:
        active = app.store.active_character()
        if not active:
            return json_response({"ok": False, "message": "还没有登录任何角色"}, 400)
        scope = app.data.char_scope(active["character_id"])
    rows = app.store.blueprints_of(scope)
    query = (req.q("q") or "").strip()
    names = app.store.type_names([r["type_id"] for r in rows])
    types = app.store.types_by_ids([r["type_id"] for r in rows])
    out = []
    for row in rows:
        name = names.get(row["type_id"]) or ""
        if query and query.lower() not in name.lower():
            continue
        blueprint_id, activity, per_run = app.store.blueprint_for_product(
            _product_of_blueprint(app, row["type_id"]))
        out.append({**row, "name": name,
                    "volume": utils.num((types.get(row["type_id"]) or {}).get("volume")),
                    "activity": activity,
                    "produces_type_id": _product_of_blueprint(app, row["type_id"])})
    return json_response({"ok": True, "scope": scope, "blueprints": out, "count": len(out),
                          "meta": app.store.blueprints_meta(scope)})


def _product_of_blueprint(app, blueprint_type_id):
    rows = app.store.q(
        "SELECT product_type_id FROM activity_products WHERE blueprint_type_id=?"
        " ORDER BY CASE activity WHEN 'manufacturing' THEN 0 WHEN 'reaction' THEN 1 ELSE 2 END"
        " LIMIT 1", (int(blueprint_type_id),))
    return rows[0]["product_type_id"] if rows else None


@route("GET", "/api/inventory")
def api_inventory(app: App, req: Request):
    scope = req.q("scope")
    if not scope:
        active = app.store.active_character()
        if not active:
            return json_response({"ok": False, "message": "还没有登录任何角色"}, 400)
        scope = app.data.char_scope(active["character_id"])
    view = app.data.inventory_view(scope, req.q("q", ""), req.qi("limit", 400),
                                  req.qi("location_id"))
    return json_response({"ok": True, **view})


@route("GET", "/api/inventory/summary")
def api_inventory_summary(app: App, req: Request):
    scopes = [s for s in (req.q("scopes", "") or "").split(",") if s]
    if not scopes:
        active = app.store.active_character()
        if not active:
            return json_response({"ok": False, "message": "还没有登录任何角色"}, 400)
        scopes = [app.data.char_scope(active["character_id"])]
        if active.get("corporation_id"):
            scopes.append(app.data.corp_scope(active["corporation_id"]))
    totals = app.data.inventory_totals(scopes)
    types = app.store.types_by_ids(list(totals.keys()))
    value = 0.0
    volume = 0.0
    for type_id, quantity in totals.items():
        row = types.get(type_id) or {}
        volume += utils.num(row.get("volume")) * quantity
    return json_response({"ok": True, "scopes": scopes, "types": len(totals),
                          "units": sum(totals.values()), "volume": volume})


# --------------------------------------------------------------------------
# 市场
# --------------------------------------------------------------------------

@route("POST", "/api/market/prices/refresh")
def api_market_prices(app: App, req: Request):
    result = app.market.ensure_prices(force=True)
    return json_response({"ok": result.get("ok", False), **result})


@route("GET", "/api/market/local")
def api_market_local(app: App, req: Request):
    """本地已缓存的物价数据概览（证明「已经存在本地」）。"""
    return json_response(app.market.local_summary(req.qi("region_id")))


def _collect_refresh_items(app: App, payload: dict) -> list:
    """算出「当前涉及哪些物品」——用于刷新盘口/历史。

    来源：请求里显式给的 type_ids + 已登录角色的蓝图产物 + 军团蓝图产物。
    前端通常还会把自己计划里的物品一并发过来。
    """
    ids = {int(i) for i in (payload.get("type_ids") or []) if str(i).isdigit()}
    if payload.get("include_blueprint_products", True):
        scopes = []
        character_id = payload.get("character_id")
        if character_id:
            character = app.store.get_character(int(character_id))
            if character:
                scopes.append(app.data.char_scope(character_id))
                if character.get("corporation_id"):
                    scopes.append(app.data.corp_scope(character["corporation_id"]))
        else:
            active = app.store.active_character()
            if active:
                scopes.append(app.data.char_scope(active["character_id"]))
                if active.get("corporation_id"):
                    scopes.append(app.data.corp_scope(active["corporation_id"]))
        for scope in scopes:
            for row in app.store.q(
                    "SELECT DISTINCT p.product_type_id FROM blueprints b"
                    " JOIN activity_products p ON p.blueprint_type_id = b.type_id"
                    " WHERE b.scope=? AND p.activity='manufacturing' LIMIT 800", (scope,)):
                if row["product_type_id"]:
                    ids.add(row["product_type_id"])
    return sorted(ids)


@route("POST", "/api/market/refresh")
def api_market_refresh(app: App, req: Request):
    """统一刷新物价：拉取 ESI 的市场数据并写入本地数据库（后台任务 + 进度）。"""
    payload = req.json or {}
    scope = payload.get("scope") or app.config.get("market_refresh_scope") or "essentials"
    if scope not in ("prices", "essentials", "orders", "history", "all"):
        return json_response({"ok": False,
                              "message": "scope 只能是 prices / essentials / orders / history / all"}, 400)
    existing = app.task_state("market-refresh")
    if existing and existing.get("running"):
        return json_response({"ok": False, "message": "已有物价刷新任务在运行中",
                              "state": existing}, 409)
    type_ids = _collect_refresh_items(app, payload)
    region_id = payload.get("region_id") or app.config.get("region_id")
    max_items = payload.get("max_items") or app.config.get("market_refresh_max_items", 250)
    app.run_task("market-refresh", lambda progress: app.market.refresh_all(
        scope=scope, type_ids=type_ids, region_id=region_id, progress=progress,
        max_items=max_items))
    return json_response({"ok": True, "scope": scope, "items": len(type_ids),
                          "region_id": region_id,
                          "message": "已在后台开始刷新物价，可在弹窗或顶栏查看进度"})


@route("POST", "/api/market/stats")
def api_market_stats(app: App, req: Request):
    type_ids = req.body("type_ids") or []
    if not type_ids:
        return json_response({"ok": False, "message": "缺少 type_ids"}, 400)
    region_id = req.body("region_id") or app.config.get("region_id")
    result = app.market.ensure_market_stats(type_ids, region_id=region_id,
                                            force=bool(req.body("force")))
    stats = result.pop("stats", {})
    return json_response({"ok": result.get("ok", False), **result,
                          "stats": {str(k): v for k, v in stats.items()}})


@route("GET", "/api/market/orderbook")
def api_orderbook(app: App, req: Request):
    type_id = req.qi("type_id")
    if not type_id:
        return json_response({"ok": False, "message": "缺少 type_id"}, 400)
    return json_response({"ok": True, **app.market.order_book_full(
        type_id, req.qi("region_id"), req.qi("max_pages", 10))})


@route("GET", "/api/market/history")
def api_history(app: App, req: Request):
    type_id = req.qi("type_id")
    if not type_id:
        return json_response({"ok": False, "message": "缺少 type_id"}, 400)
    days = req.qi("days", 30)
    refresh = req.qbool("refresh", False)
    if refresh:
        app.market.ensure_history(type_id, req.qi("region_id"))
    region_id = req.qi("region_id") or app.config.get("region_id")
    rows = app.store.get_market_history(type_id, region_id, days)
    if not rows and not refresh:
        app.market.ensure_history(type_id, region_id)
        rows = app.store.get_market_history(type_id, region_id, days)
    return json_response({"ok": True, "type_id": type_id, "region_id": region_id,
                          "rows": rows,
                          "average_7d": app.store.history_average(type_id, region_id, 7)})


@route("GET", "/api/market/ranking")
def api_ranking(app: App, req: Request):
    result = app.market.profit_ranking(
        market_group_id=req.qi("market_group_id"),
        category_id=req.qi("category_id"),
        group_id=req.qi("group_id"),
        limit=req.qi("limit", 60),
        min_price=req.qf("min_price", 0) or 0,
        min_volume=req.qi("min_volume", 0) or 0,
        sort=req.q("sort", "margin"),
        me=req.qf("me", 10), te=req.qf("te", 20),
        activity=req.q("activity", "manufacturing"),
        refresh_market=req.qbool("refresh_market", False),
        region_id=req.qi("region_id"),
        scan_limit=req.qi("scan_limit", 1500),
    )
    return json_response(result)


@route("GET", "/api/industry/structures")
def api_structures(app: App, req: Request):
    """玩家可部署的**全部**工业建筑，供界面下拉选择。

    不只是「自带工业加成」的建筑 —— 空堡 / 铁壁 / 星城 / 阿塔诺自身没有加成，
    但能装改装件（主要加成来源），必须出现在列表里；只列自带加成的建筑
    会让这些建筑从下拉里消失（用户报过这个问题）。
    同时返回 rigSize（中型/大型/超大型），界面据此只显示匹配的改装件。
    """
    rows = industry.industry_structures(app.store, req.q("activity", "manufacturing"))
    return json_response({"ok": True, "structures": rows,
                          "activity": req.q("activity", "manufacturing"),
                          "groups": cfgmod.INDUSTRY_STRUCTURE_GROUPS,
                          "rig_size_labels": cfgmod.RIG_SIZE_LABELS,
                          "rig_slot_limit": cfgmod.STRUCTURE_RIG_SLOT_LIMIT})


# --------------------------------------------------------------------------
# 蓝图研究模拟（ME / TE 在各建筑里的耗时与费用）
# --------------------------------------------------------------------------

def _research_context(app: App, payload: dict, cfg: dict, blueprint_type_id: int) -> dict:
    """准备研究模拟需要的上下文：EIV、成本指数、技能、设施清单。"""
    activities = app.store.blueprint_activities(int(blueprint_type_id))
    manufacturing = activities.get("manufacturing") or {}
    material_ids = [m["type_id"] for m in industry.eiv_materials(activities, "manufacturing")]
    product_ids = [p["type_id"] for p in manufacturing.get("products") or []]
    # 只取调整价（EIV 的定义就是 ME0 材料 × 调整价），因此不需要查询盘口
    price_ctx = app.price_context(material_ids + product_ids, cfg, "none")
    eiv = 0.0
    for material in industry.eiv_materials(activities, "manufacturing"):
        adjusted = utils.num((price_ctx["prices"].get(material["type_id"]) or {}).get("adjusted_price"))
        eiv += adjusted * utils.as_int(material["quantity"])
    eiv = round(eiv)
    eiv_warning = None
    if eiv <= 0:
        # 费用 = f(EIV, 成本指数)；EIV 来自材料**调整价**。
        # 本地没拉过调整价时 EIV=0，费用会恒为 0 —— 必须告诉用户原因，
        # 否则界面只显示一个 0，看起来像计算坏了。
        eiv_warning = ("本地缺少材料调整价（EIV = 0），所以 ME/TE 费用会按 0 计。" "请先到「价格对比」页点「刷新物价」，再回到本页重新模拟。")

    facility = (payload or {}).get("facility") or {}
    system_id = facility.get("system_id")
    if system_id is None:
        system_id = app.store.kv_get("current_system_id")
    security = payload.get("security") or facility.get("security")
    if not security and system_id:
        security = industry.security_context(app.store, system_id).get("class")
    cost_index = industry.cost_index_for(app.store, system_id, "research_material")
    if ((payload or {}).get("industry") or {}).get("cost_index") is not None:
        cost_index = utils.num(payload["industry"]["cost_index"])

    rig_type_ids = payload.get("rig_type_ids")
    if rig_type_ids is None:
        rig_type_ids = (facility.get("rig_type_ids") or [])
    facilities = payload.get("facilities")
    if not facilities:
        facilities = research.default_facilities(app.store, rig_type_ids)

    return {"eiv_warning": eiv_warning,
        "eiv": eiv,
        "cost_index": cost_index,
        "system_id": system_id,
        "security": security,
        "facilities": facilities,
        "skill_levels": app.skill_levels(payload),
        "prices_meta": app.store.prices_meta(),
        "warnings": price_ctx["warnings"],
    }


@route("GET", "/api/research/blueprint/{blueprint_type_id}")
def api_research_blueprint(app: App, req: Request, blueprint_type_id):
    bp = int(blueprint_type_id)
    info = research.blueprint_info(app.store, bp)
    owned = []
    scope = req.q("scope")
    if not scope:
        active = app.store.active_character()
        if active:
            scope = app.data.char_scope(active["character_id"])
    if scope:
        for row in app.store.q(
                "SELECT * FROM blueprints WHERE scope=? AND type_id=? ORDER BY material_efficiency DESC",
                (scope, bp)):
            owned.append(dict(row))
    return json_response({"ok": True, "blueprint": info, "owned": owned, "scope": scope,
                          "max_level": research.MAX_LEVEL,
                          "me_percent_per_level": cfgmod.ME_PERCENT_PER_LEVEL,
                          "te_percent_per_level": cfgmod.TE_PERCENT_PER_LEVEL})


@route("POST", "/api/research/simulate")
def api_research_simulate(app: App, req: Request):
    payload = req.json or {}
    blueprint_type_id = payload.get("blueprint_type_id")
    product_type_id = payload.get("type_id") or payload.get("product_type_id")
    if not blueprint_type_id and product_type_id:
        blueprint_type_id, _, _ = app.store.blueprint_for_product(int(product_type_id))
    if not blueprint_type_id:
        return json_response({"ok": False, "message": "缺少 blueprint_type_id 或 type_id"}, 400)
    cfg = app.effective_config(payload)
    context = _research_context(app, payload, cfg, int(blueprint_type_id))
    result = research.simulate(
        app.store, int(blueprint_type_id), context["facilities"], cfg,
        context["skill_levels"],
        from_me=utils.as_int(payload.get("from_me"), 0),
        to_me=utils.as_int(payload.get("to_me"), research.MAX_LEVEL),
        from_te=utils.as_int(payload.get("from_te"), 0),
        to_te=utils.as_int(payload.get("to_te"), research.MAX_LEVEL),
        system_id=context["system_id"], security=context["security"],
        cost_index=context["cost_index"], eiv=context["eiv"])
    return json_response({**result,
                          "warnings": ((result.get("notes") or []) + context["warnings"]
                          + ([context["eiv_warning"]] if context.get("eiv_warning") else [])),
                          "context": {"eiv": context["eiv"],
                                      "cost_index": context["cost_index"],
                                      "system_id": context["system_id"],
                                      "security": context["security"],
                                      "skill_levels": context["skill_levels"],
                                      "prices_updated_at": context["prices_meta"].get("updated_at")}})


@route("GET", "/api/research/owned")
def api_research_owned(app: App, req: Request):
    """已拥有蓝图的研究现状 + 研究到满级需要多久。"""
    scope = req.q("scope")
    if not scope:
        active = app.store.active_character()
        if not active:
            return json_response({"ok": False, "message": "还没有登录任何角色"}, 400)
        scope = app.data.char_scope(active["character_id"])
    if not app.store.blueprints_meta(scope)["entries"]:
        return json_response({"ok": False, "scope": scope,
                              "message": "本地还没有该角色的蓝图数据，请先到「工业项目」页点「从 ESI 刷新」"})
    cfg = app.effective_config(req.json or {})
    rig_type_ids = [int(i) for i in (req.q("rig_type_ids", "") or "").split(",") if i.strip().isdigit()]
    facility = {"system_id": req.qi("system_id"),
                "security": req.q("security")}
    context = _research_context(app, {"facility": facility, "rig_type_ids": rig_type_ids}, cfg, 0)
    result = research.owned_research(app.store, scope, context["facilities"], cfg,
                                    context["skill_levels"],
                                    system_id=context["system_id"],
                                    security=context["security"],
                                    cost_index=context["cost_index"])
    return json_response({**result, "context": {"system_id": context["system_id"],
                                                "security": context["security"],
                                                "skill_levels": context["skill_levels"]}})


@route("GET", "/api/research/facilities")
def api_research_facilities(app: App, req: Request):
    """候选设施清单（默认对比用），以及可用于研究的改装件。"""
    rig_type_ids = [int(i) for i in (req.q("rig_type_ids", "") or "").split(",") if i.strip().isdigit()]
    facilities = research.default_facilities(app.store, rig_type_ids)
    rigs = industry.structure_bonus_candidates(app.store, "research_time", limit=400)
    return json_response({"ok": True, "facilities": facilities, "rigs": rigs,
                          "max_level": research.MAX_LEVEL,
                          "level_table": cfgmod.RESEARCH_CUMULATIVE_SECONDS,
                          "me_percent_per_level": cfgmod.ME_PERCENT_PER_LEVEL,
                          "te_percent_per_level": cfgmod.TE_PERCENT_PER_LEVEL})


@route("GET", "/api/fuel/services")
def api_fuel_services(app: App, req: Request):
    """服务装备清单（含每小时/上线燃料），供界面勾选。"""
    structure_type_id = req.qi("structure_type_id")
    activity = req.q("activity", "manufacturing")
    services = industry.service_modules(app.store, structure_type_id, activity)
    return json_response({"ok": True, "services": services, "activity": activity,
                          "fuel_block_group": cfgmod.FUEL_BLOCK_GROUP_ID,
                          "fuel_types": industry.fuel_types(app.store),
                          "fuel_type_id": cfgmod.DEFAULT_FUEL_BLOCK_TYPE_ID,
                          "fuel_blocks_per_unit": cfgmod.FUEL_BLOCKS_PER_UNIT})


@route("POST", "/api/fuel/plan")
def api_fuel_plan(app: App, req: Request):
    """算建筑燃料消耗（量与钱）。"""
    body = req.json or {}
    plan = industry.fuel_plan(
        app.store,
        structure_type_id=body.get("structure_type_id"),
        service_type_ids=body.get("service_type_ids") or [],
        hours=utils.as_int(body.get("hours"), 720),
        price=body.get("price"),
        fuel_type_id=body.get("fuel_type_id"),
    )
    # available_services 只是给界面下拉用的，不必随每次计算返回
    plan.pop("available_services", None)
    return json_response({"ok": True, **plan})


@route("GET", "/api/corp/structures")
def api_corp_structures(app: App, req: Request):
    """读回已缓存的军团建筑（含在线服务装备与燃料到期时间）。"""
    corporation_id = req.qi("corporation_id")
    rows = app.store.corp_structures(corporation_id)
    return json_response({"ok": True, "structures": rows,
                          "count": len(rows),
                          "unmatched_services": sorted({
                              s.get("name") for r in rows for s in (r.get("services") or [])
                              if s.get("name") and not s.get("module_type_id")})})


@route("POST", "/api/corp/structures/refresh")
def api_corp_structures_refresh(app: App, req: Request):
    """从 ESI 读取军团建筑（需要军团授权 + 总监/空间站经理职位）。"""
    body = req.json or {}
    character_id = utils.as_int(body.get("character_id")) or None
    if not character_id:
        active = app.store.active_character()
        character_id = active["character_id"] if active else None
    if not character_id:
        return json_response({"ok": False, "message": "需要先登录一个角色"}, 400)
    character = app.store.character(character_id) or {}
    corporation_id = utils.as_int(body.get("corporation_id")) or \
        utils.as_int(character.get("corporation_id"))
    if not corporation_id:
        return json_response({"ok": False, "message": "该角色没有军团信息"}, 400)
    try:
        result = app.data.refresh_corp_structures(corporation_id, character_id)
    except ESIError as exc:
        return json_response({"ok": False,
                              "message": "读取军团建筑失败：%s" % exc,
                              "hint": "需要 esi-corporations.read_structures.v1 且角色有"
                                      "总监 / 空间站经理职位"}, 400)
    return json_response({"ok": bool(result.get("ok")), "corporation_id": corporation_id,
                          **result})


@route("POST", "/api/db/rebuild")
def api_db_rebuild(app: App, req: Request):
    """重建数据库（数据都是可重新获取的缓存）。

    会丢的东西：登录令牌（要重新登录）、已导入的 SDE（重新下载）、本地物价。
    旧文件会保留成 `industry.db.corrupt-<时间戳>`，不会直接删掉。
    """
    try:
        result = app.store.rebuild()
    except Exception as exc:
        return json_response({"ok": False, "message": "重建数据库失败：%s" % exc}, 500)
    return json_response({
        "ok": True, **result,
        "message": "数据库已重建。请到「设置 → 静态数据」重新下载导入 SDE"
                   "（约 95 MB），然后重新登录一次 EVE（令牌表也一起重建了）。"
                   "旧的损坏文件已保留为 industry.db.corrupt-… 供排查。",
    })


@route("GET", "/api/industry/cost-indices")
def api_cost_indices(app: App, req: Request):
    force = req.qbool("refresh", False)
    result = app.market.ensure_cost_indices(force=force)
    system_id = req.qi("system_id")
    out = {"ok": result.get("ok", False), **result}
    if system_id:
        out["system_id"] = system_id
        out["indices"] = app.store.cost_indices_for(system_id)
        out["system"] = app.store.system_name(system_id)
    out["activity_names"] = cfgmod.COST_INDEX_ACTIVITIES
    return json_response(out)


@route("GET", "/api/industry/systems/search")
def api_system_search(app: App, req: Request):
    query = (req.q("q") or "").strip()
    if not query:
        return json_response({"ok": True, "results": []})
    like = "%%%s%%" % query
    rows = app.store.q(
        "SELECT system_id, name, name_en, security_status, region_id FROM solar_systems"
        " WHERE name LIKE ? OR name_en LIKE ?"
        " ORDER BY CASE WHEN name LIKE ? OR name_en LIKE ? THEN 0 ELSE 1 END,"
        " LENGTH(name), name LIMIT ?",
        (like, like, query + "%", query + "%", req.qi("limit", 20)))
    regions = {r["region_id"]: r["name"] for r in app.store.q("SELECT * FROM regions")}
    results = []
    for row in rows:
        item = dict(row)
        item["region_name"] = regions.get(row["region_id"])
        results.append(item)
    if not results:
        # 本地 SDE 里没有（或还没导入），退回 ESI 直接查
        try:
            search = app.esi.get_data("/search/", params={"categories": "solar_system",
                                                          "search": query, "strict": "false"})
            ids = (search or {}).get("solar_system") or []
            if ids:
                names = app.esi.names_for(ids[:20])
                for system_id in ids[:20]:
                    info = app.store.system_name(system_id) or {}
                    results.append({"system_id": system_id,
                                    "name": names.get(system_id) or info.get("name"),
                                    "security_status": info.get("security_status"),
                                    "region_id": info.get("region_id")})
        except Exception as exc:
            utils.log("ESI 星系搜索失败：%s", exc)
    return json_response({"ok": True, "results": results})


@route("GET", "/api/industry/cheapest-systems")
def api_cheapest_systems(app: App, req: Request):
    """按成本指数找最便宜的星系（工业搬砖常用的查询）。"""
    activity = req.q("activity", "manufacturing")
    key = cfgmod.ACTIVITY_TO_COST_INDEX.get(activity, activity)
    min_sec = req.qf("min_security", 0.45)
    limit = req.qi("limit", 30)
    rows = app.store.q(
        "SELECT c.system_id, c.cost_index, s.name, s.security_status, s.region_id"
        " FROM cost_indices c JOIN solar_systems s ON s.system_id=c.system_id"
        " WHERE c.activity=? AND (s.security_status IS NULL OR s.security_status>=?)"
        " ORDER BY c.cost_index ASC LIMIT ?", (key, utils.num(min_sec), int(limit)))
    regions = {r["region_id"]: r["name"] for r in app.store.q("SELECT * FROM regions")}
    return json_response({"ok": True, "activity": activity, "rows": [
        {**dict(r), "region_name": regions.get(r["region_id"])} for r in rows]})


@route("GET", "/api/industry/facilities")
def api_facilities(app: App, req: Request):
    result = app.market.ensure_facilities(force=req.qbool("refresh", False))
    system_id = req.qi("system_id")
    rows = []
    if system_id:
        for row in app.store.q(
                "SELECT * FROM industry_facilities WHERE solar_system_id=? LIMIT 500",
                (system_id,)):
            rows.append(dict(row))
    return json_response({"ok": result.get("ok", False), **result,
                          "system_id": system_id, "facilities": rows})


# --------------------------------------------------------------------------
# 计算
# --------------------------------------------------------------------------

def _collect_plan_type_ids(app: App, items, activity):
    blueprint_ids = []
    for item in items or []:
        if item.get("blueprint_type_id"):
            blueprint_ids.append(int(item["blueprint_type_id"]))
        elif item.get("type_id"):
            bp, act, _ = app.store.blueprint_for_product(int(item["type_id"]))
            if bp:
                blueprint_ids.append(bp)
    material_ids = set()
    product_ids = set()
    for part in utils.chunked(sorted(set(blueprint_ids)), 400):
        marks = ",".join("?" for _ in part)
        for row in app.store.q(
                "SELECT DISTINCT material_type_id FROM activity_materials"
                " WHERE activity=? AND blueprint_type_id IN (%s)" % marks,
                [activity] + part):
            material_ids.add(row["material_type_id"])
        for row in app.store.q(
                "SELECT DISTINCT product_type_id FROM activity_products"
                " WHERE activity=? AND blueprint_type_id IN (%s)" % marks,
                [activity] + part):
            product_ids.add(row["product_type_id"])
    return sorted(material_ids | product_ids), sorted(product_ids), blueprint_ids


@route("POST", "/api/calc/product")
def api_calc_product(app: App, req: Request):
    payload = req.json or {}
    blueprint_type_id = payload.get("blueprint_type_id")
    type_id = payload.get("type_id")
    if not blueprint_type_id and type_id:
        blueprint_type_id, _, _ = app.store.blueprint_for_product(int(type_id))
    if not blueprint_type_id:
        return json_response({"ok": False, "message": "缺少 blueprint_type_id 或 type_id"}, 400)
    activity = payload.get("activity") or app.store.activity_of_blueprint(
        int(blueprint_type_id)) or "manufacturing"
    cfg = app.effective_config(payload)
    activities = app.store.blueprint_activities(int(blueprint_type_id))
    payload_activity = activities.get(activity) or {}
    material_ids = [m["type_id"] for m in payload_activity.get("materials") or []]
    product_ids = [p["type_id"] for p in payload_activity.get("products") or []]
    product_rows = [app.store.type_by_id(i) for i in product_ids]
    context = app.build_calc_context(payload, activity, material_ids + product_ids,
                                     product_ids, cfg, product_rows)
    result = industry.product_breakdown(
        app.store, int(blueprint_type_id), activity,
        utils.num(payload.get("me"), 10),
        utils.num(payload.get("te"), 20),
        max(1, utils.as_int(payload.get("runs"), 1)),
        context["prices"], context["market"], cfg, context["bonuses"],
        context["skill_levels"], context["cost_index"], context["history"],
        context["inventory"])
    return json_response({"ok": True, "result": result, "warnings": context["warnings"],
                          "context": {"region_id": context["region_id"],
                                      "scopes": context["scopes"],
                                      "bonuses": context["bonuses"]}})


@route("POST", "/api/calc/plan")
def api_calc_plan(app: App, req: Request):
    """「多种产品」主界面：一次算一批产品，并给出汇总。"""
    payload = req.json or {}
    items = payload.get("items") or []
    if not items:
        return json_response({"ok": False, "message": "请先添加至少一个产品"}, 400)
    activity = payload.get("activity") or "manufacturing"
    cfg = app.effective_config(payload)
    material_ids, product_ids, _ = _collect_plan_type_ids(app, items, activity)
    product_rows = [app.store.type_by_id(i) for i in product_ids]
    context = app.build_calc_context(payload, activity, material_ids + product_ids,
                                     product_ids, cfg, product_rows)
    resolver = app.bonus_resolver(payload, cfg)
    result = industry.plan_products(
        app.store, items, activity, context["prices"], context["market"], cfg,
        context["bonuses"], context["skill_levels"], context["cost_index"],
        context["inventory"], context["history"], resolver)
    return json_response({"ok": True, **result, "warnings": context["warnings"],
                          "context": {"region_id": context["region_id"],
                                      "scopes": context["scopes"],
                                      "bonuses": context["bonuses"],
                                      "price_source": cfg.get("price_source"),
                                      "skill_levels": context["skill_levels"]}})


@route("POST", "/api/calc/aggregate")
def api_calc_aggregate(app: App, req: Request):
    """主表格：把一批产品需要的原料聚合成一张表（含自造/收购对比）。"""
    payload = req.json or {}
    items = payload.get("items") or []
    if not items:
        return json_response({"ok": False, "message": "请先添加至少一个产品"}, 400)
    activity = payload.get("activity") or "manufacturing"
    cfg = app.effective_config(payload)
    material_ids, product_ids, _ = _collect_plan_type_ids(app, items, activity)

    # 材料如果自身也能造，它的子材料也要一并询价，因此先做一次浅层展开
    deeper = set()
    for part in utils.chunked(sorted(set(material_ids)), 300):
        marks = ",".join("?" for _ in part)
        for row in app.store.q(
                "SELECT DISTINCT material_type_id FROM activity_materials"
                " WHERE blueprint_type_id IN (SELECT blueprint_type_id FROM activity_products"
                "   WHERE product_type_id IN (%s))" % marks, part):
            deeper.add(row["material_type_id"])
    all_ids = sorted(set(material_ids) | deeper | set(product_ids))

    product_rows = [app.store.type_by_id(i) for i in product_ids]
    context = app.build_calc_context(payload, activity, all_ids, product_ids, cfg, product_rows)
    resolver = app.bonus_resolver(payload, cfg)
    facility = (payload or {}).get("facility") or {}
    system_id = facility.get("system_id") or app.store.kv_get("current_system_id")
    location_name = None
    if system_id:
        info = app.store.system_name(int(system_id))
        if info:
            location_name = info.get("name")

    build_modes = {str(k): v for k, v in ((payload.get("build_modes") or {}).items())}
    result = industry.aggregate_materials(
        app.store, items, activity, context["prices"], context["market"], cfg,
        resolver, context["bonuses"], context["skill_levels"], context["cost_index"],
        context["inventory"], context["history"], build_modes,
        utils.num(payload.get("material_me"), 10),
        utils.num(payload.get("material_te"), 20),
        location_name)
    return json_response({"ok": True, **result, "warnings": context["warnings"],
                          "context": {"region_id": context["region_id"],
                                      "scopes": context["scopes"],
                                      "bonuses": context["bonuses"],
                                      "facility": facility,
                                      "system_id": system_id,
                                      "price_source": cfg.get("price_source"),
                                      "skill_levels": context["skill_levels"]}})


@route("POST", "/api/calc/multi")
def api_calc_multi(app: App, req: Request):
    """生产规划：多级 BOM 展开 + 库存缺口。"""
    payload = req.json or {}
    if not payload.get("type_id") and not payload.get("blueprint_type_id"):
        return json_response({"ok": False, "message": "请指定目标产品"}, 400)
    activity = payload.get("activity") or "manufacturing"
    if payload.get("type_id") and not payload.get("blueprint_type_id"):
        bp, act, _ = app.store.blueprint_for_product(int(payload["type_id"]))
        activity = act or activity
    cfg = app.effective_config(payload)

    # 第一遍：只为了收集需要用到的全部 type_id（不依赖价格）
    probe = multi_level(app.store, payload.get("type_id"), payload.get("blueprint_type_id"),
                        max(1, utils.as_int(payload.get("runs"), 1)),
                        utils.num(payload.get("me"), 10), utils.num(payload.get("te"), 20),
                        activity, min(6, utils.as_int(payload.get("max_depth"), 3)),
                        payload.get("build_types"), payload.get("buy_types"),
                        {}, {}, cfg, {}, {}, {}, 0.0,
                        payload.get("use_inventory_intermediates", True))
    if not probe.get("ok"):
        return json_response({"ok": False, "message": probe.get("message")}, 400)

    involved = {r["type_id"] for r in probe.get("base_materials") or []}
    involved |= {j["product_type_id"] for j in probe.get("jobs") or []}
    involved.add(probe.get("root_type_id"))
    involved.discard(None)
    product_ids = [j["product_type_id"] for j in probe.get("jobs") or []]
    context = app.build_calc_context(payload, activity, sorted(involved), product_ids, cfg)

    result = multi_level(
        app.store, payload.get("type_id"), payload.get("blueprint_type_id"),
        max(1, utils.as_int(payload.get("runs"), 1)),
        utils.num(payload.get("me"), 10), utils.num(payload.get("te"), 20),
        activity, min(6, utils.as_int(payload.get("max_depth"), 3)),
        payload.get("build_types"), payload.get("buy_types"),
        context["prices"], context["market"], cfg, context["bonuses"],
        context["inventory"], context["skill_levels"], context["cost_index"],
        payload.get("use_inventory_intermediates", True))
    names = app.store.type_names([r["type_id"] for r in result.get("base_materials") or []])
    for row in result.get("base_materials") or []:
        row["name"] = row.get("name") or names.get(row["type_id"])
    result["shopping_list"] = planner.shopping_list(result)
    return json_response({"ok": True, **result, "warnings":
                          (result.get("warnings") or []) + context["warnings"],
                          "context": {"region_id": context["region_id"],
                                      "scopes": context["scopes"],
                                      "bonuses": context["bonuses"]}})


@route("POST", "/api/calc/bom")
def api_calc_bom(app: App, req: Request):
    """材料清单（单层，不递归）。"""
    payload = req.json or {}
    blueprint_type_id = payload.get("blueprint_type_id")
    activity = payload.get("activity") or "manufacturing"
    if not blueprint_type_id:
        return json_response({"ok": False, "message": "缺少 blueprint_type_id"}, 400)
    cfg = app.effective_config(payload)
    activities = app.store.blueprint_activities(int(blueprint_type_id))
    body = activities.get(activity) or {}
    material_ids = [m["type_id"] for m in body.get("materials") or []]
    product_ids = [p["type_id"] for p in body.get("products") or []]
    material_ids += product_ids
    context = app.build_calc_context(payload, activity, material_ids, product_ids, cfg,
                                     [app.store.type_by_id(i) for i in product_ids])
    result = industry.product_breakdown(
        app.store, int(blueprint_type_id), activity,
        utils.num(payload.get("me"), 10), utils.num(payload.get("te"), 20),
        max(1, utils.as_int(payload.get("runs"), 1)),
        context["prices"], context["market"], cfg, context["bonuses"],
        context["skill_levels"], context["cost_index"], context["history"],
        context["inventory"])
    return json_response({"ok": True, "result": result, "warnings": context["warnings"]})


@route("POST", "/api/calc/facility")
def api_calc_facility(app: App, req: Request):
    """只算设施加成（界面上改建筑/改装件时实时预览）。"""
    payload = req.json or {}
    activity = payload.get("activity") or "manufacturing"
    cfg = app.effective_config(payload)
    product_type_id = payload.get("product_type_id")
    product_row = app.store.type_by_id(int(product_type_id)) if product_type_id else None
    bonuses = app.facility_context(payload, activity, cfg, product_row)
    return json_response({"ok": True, "bonuses": bonuses})


# --------------------------------------------------------------------------
# 方案
# --------------------------------------------------------------------------

@route("GET", "/api/plans")
def api_plans(app: App, req: Request):
    return json_response({"ok": True, "plans": app.store.list_plans()})


@route("GET", "/api/plans/{plan_id}")
def api_plan_get(app: App, req: Request, plan_id):
    plan = app.store.get_plan(int(plan_id))
    if not plan:
        return json_response({"ok": False, "message": "方案不存在"}, 404)
    return json_response({"ok": True, "plan": plan})


@route("POST", "/api/plans")
def api_plan_save(app: App, req: Request):
    name = (req.body("name") or "").strip()
    if not name:
        return json_response({"ok": False, "message": "请填写方案名称"}, 400)
    plan_id = app.store.save_plan(name, req.body("data") or {}, req.body("plan_id"))
    return json_response({"ok": True, "plan_id": plan_id, "message": "已保存"})


@route("DELETE", "/api/plans/{plan_id}")
def api_plan_delete(app: App, req: Request, plan_id):
    app.store.delete_plan(int(plan_id))
    return json_response({"ok": True})


# --------------------------------------------------------------------------
# 杂项
# --------------------------------------------------------------------------

@route("POST", "/api/cache/clear")
def api_cache_clear(app: App, req: Request):
    like = req.body("like")
    count = app.store.cache_clear(like)
    return json_response({"ok": True, "cleared": count})


@route("GET", "/api/docs")
def api_docs_list(app: App, req: Request):
    """列出 docs 目录下的说明文档。"""
    docs_dir = os.path.join(cfgmod.ROOT, "docs")
    items = []
    if os.path.isdir(docs_dir):
        for name in sorted(os.listdir(docs_dir)):
            if name.lower().endswith((".md", ".txt")):
                path = os.path.join(docs_dir, name)
                items.append({"name": name, "bytes": os.path.getsize(path)})
    return json_response({"ok": True, "docs": items})


def serve_doc(path):
    """把 docs/<文件名> 当纯文本返回（前端用 <pre> 展示）。"""
    name = path[len("/docs/"):]
    if not name or "/" in name or ".." in name:
        return None
    full = os.path.join(cfgmod.ROOT, "docs", name)
    if not os.path.isfile(full):
        return None
    with open(full, "r", encoding="utf-8") as fh:
        return fh.read()


@route("GET", "/api/images/{type_id}")
def api_image(app: App, req: Request, type_id):
    kind = req.q("kind", "icon")
    size = req.qi("size", 64)
    url = "%s/types/%s/%s?size=%d" % (cfgmod.IMAGE_SERVER, type_id, kind, size)
    return json_response({"ok": True, "url": url})


@route("POST", "/api/open")
def api_open(app: App, req: Request):
    url = req.body("url") or ""
    if url.startswith("http://") or url.startswith("https://"):
        webbrowser.open(url)
        return json_response({"ok": True})
    return json_response({"ok": False, "message": "只允许打开 http/https 链接"}, 400)


# ==========================================================================
# HTTP 处理
# ==========================================================================

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "EveIndustryLocal/%s" % __version__
    app: App = None

    # ---- 基础工具 ----
    def log_message(self, fmt, *args):
        utils.log("%s - %s", self.address_string(), fmt % args)

    def _send(self, status, body, content_type):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str)
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status, payload):
        self._send(status, payload, "application/json; charset=utf-8")

    def _read_body(self) -> bytes:
        length = utils.as_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return b""
        if length > 64 * 1024 * 1024:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    # ---- 分发 ----
    def _handle(self, method):
        if self._guard_request():
            return
        parts = urlsplit(self.path)
        # 浏览器会把非 ASCII 路径百分号编码，这里必须解回来
        try:
            path = urllib.parse.unquote(parts.path, errors="replace")
        except Exception:
            path = parts.path
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        query = parse_qs(parts.query, keep_blank_values=True)
        try:
            body = self._read_body()
        except Exception as exc:
            return self._send_json(413, {"ok": False, "message": str(exc)})

        # SSO 回调
        if path == self.app.config.get("callback_path", "/callback"):
            return self._handle_callback(query)

        for route_method, regex, fn in ROUTES:
            if route_method != method:
                continue
            match = regex.match(path)
            if not match:
                continue
            request = Request(method, path, query, body, self.headers,
                              self.client_address[0] if self.client_address else None)
            try:
                result = fn(self.app, request, **match.groupdict())
                if isinstance(result, tuple) and len(result) == 3:
                    return self._send(result[0], result[1], result[2])
                return self._send_json(200, result)
            except ESIError as exc:
                return self._send_json(502 if exc.status in (0, 500, 502, 503, 504) else 400,
                                       {"ok": False, "esi_error": True, **exc.to_dict()})
            except SSOError as exc:
                return self._send_json(400, {"ok": False, "sso_error": True, "message": str(exc)})
            except ValueError as exc:
                return self._send_json(400, {"ok": False, "message": str(exc)})
            except Exception as exc:
                utils.log("接口 %s %s 出错：%s\n%s", method, path, exc, traceback.format_exc())
                # 数据库损坏会让各种读取莫名其妙地 500（真实案例：
                # 「GET /api/market/local 出错: database disk image is malformed」）。
                # 一旦看到 SQLite 层面的错误就立刻把库标成不健康 —— 界面顶部马上会出现
                # 「重建数据库」横幅，不必等下次重启自检。
                self._note_db_problem(exc)
                return self._send_json(500, {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc),
                                             "path": path})
        if path.startswith("/api/"):
            return self._send_json(404, {"ok": False, "message": "接口不存在：%s" % path})
        if path.startswith("/docs/"):
            text = serve_doc(path)
            if text is not None:
                return self._send(200, text, "text/plain; charset=utf-8")
        return self._serve_static(path)

    def _note_db_problem(self, exc) -> None:
        """SQLite 层面的错误 → 把数据库标成不健康，并给出人话提示。"""
        message = str(exc)
        if not (isinstance(exc, sqlite3.DatabaseError) or "malformed" in message
                or "not a database" in message or "encrypted" in message):
            return
        try:
            self.app.store.integrity = (False, message[:200])
        except Exception:
            pass
        utils.log("  ⚠ 这是本地数据库损坏引起的：界面顶部会出现「重建数据库」按钮；"
                  "也可以手动删掉 data/industry.db（连同 -wal / -shm）后重启")

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        # 跨源预检一律拒绝：本程序不对外提供 API，浏览器不该从别的源来问
        if not self._origin_allowed():
            return self._send(403, "拒绝跨源请求", "text/plain; charset=utf-8")
        self._send(204, b"", "text/plain")

    # ---- 本机来源校验 ----
    # 这个服务没有任何登录态，任何能访问 127.0.0.1:端口 的东西都能读你的 ESI 数据。
    # 两道闸门：
    #   ① Host 必须是 localhost / 127.0.0.1 / [::1]
    #      —— 防 DNS rebinding：攻击者把自己的域名解析到 127.0.0.1，
    #         浏览器就以为请求是同源的，于是能读到返回内容。
    #   ② 带 Origin 头的请求，Origin 必须就是本机这个端口
    #      —— 防 CSRF：你访问的恶意页面替你向本程序发请求。
    # 没有 Origin 头（curl、PowerShell、本机脚本）放行，否则命令行没法用。
    _LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        if host.startswith("["):                      # [::1]:8420
            name = host.split("]")[0] + "]"
        elif ":" in host:
            name = host.rsplit(":", 1)[0]
        else:
            name = host
        return name in self._LOCAL_HOSTS

    def _origin_allowed(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True                               # 非浏览器来源
        if origin.lower() == "null":
            return False                              # 沙箱 iframe / file:// 一律拒绝
        try:
            parts = urlsplit(origin)
        except Exception:
            return False
        if parts.scheme not in ("http", "https"):
            return False
        name = (parts.hostname or "").lower()
        if name not in self._LOCAL_HOSTS:
            return False
        own_port = self.server.server_address[1]
        try:
            return (parts.port or (443 if parts.scheme == "https" else 80)) == own_port
        except ValueError:
            return False

    def _guard_request(self) -> bool:
        """返回 True 表示已拦截（响应已发出）。"""
        if not self._host_allowed():
            utils.log("已拒绝 Host=%r 的请求（只接受本机来源）", self.headers.get("Host"))
            self._send(403, "只接受来自本机的访问（Host 校验未通过）",
                       "text/plain; charset=utf-8")
            return True
        if not self._origin_allowed():
            utils.log("已拒绝 Origin=%r 的跨源请求", self.headers.get("Origin"))
            self._send(403, "拒绝跨源请求（Origin 校验未通过）",
                       "text/plain; charset=utf-8")
            return True
        return False

    # ---- SSO 回调 ----
    def _handle_callback(self, query):
        error = (query.get("error") or [None])[0]
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [None])[0]
        if error:
            description = (query.get("error_description") or [""])[0]
            return self._send(400, _result_page(False, "登录失败", "%s %s" % (error, description)), MIME[".html"])
        if not code:
            return self._send(400, _result_page(False, "登录失败", "没有收到授权码"), MIME[".html"])
        try:
            result = self.app.sso.exchange_code(code, state=state)
        except SSOError as exc:
            return self._send(400, _result_page(False, "登录失败", str(exc)), MIME[".html"])
        except Exception as exc:
            utils.log("登录回调出错：%s\n%s", exc, traceback.format_exc())
            return self._send(500, _result_page(False, "登录失败", str(exc)), MIME[".html"])
        try:
            self.app.data.character_roles(result["character_id"])
        except Exception:
            pass
        body = _result_page(True, "登录成功",
                            "角色 %s（%d）已添加，正在返回应用…" % (result["name"], result["character_id"]),
                            redirect="/?login=ok&character_id=%d" % result["character_id"])
        return self._send(200, body, MIME[".html"])

    # ---- 静态文件 ----
    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        relative = path.lstrip("/").replace("\\", "/")
        if ".." in relative.split("/"):
            return self._send(403, "非法路径", "text/plain; charset=utf-8")
        full = os.path.join(cfgmod.WEB_DIR, relative.replace("/", os.sep))
        if not os.path.isfile(full):
            # SPA 回退
            index = os.path.join(cfgmod.WEB_DIR, "index.html")
            if os.path.isfile(index):
                with open(index, "rb") as fh:
                    return self._send(200, self._render_index(fh.read()), MIME[".html"])
            return self._send(404, "未找到 app/web/index.html", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            data = fh.read()
        if ext == ".html":
            # index.html 里的 __APP_VERSION__ 换成真实版本号 ——
            # 前端据此比较「页面自带的版本」与「服务端版本」，不一致就提示刷新。
            # 这样一旦浏览器还跑着旧 app.js（换版本后没刷新页面），用户能立刻看出来，
            # 而不是奇怪「我明明更新了怎么还是旧行为」。
            data = self._render_index(data)
        return self._send(200, data, MIME.get(ext, mimetypes.guess_type(full)[0]
                                              or "application/octet-stream"))

    @staticmethod
    def _render_index(data: bytes) -> bytes:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        return text.replace("__APP_VERSION__", __version__).encode("utf-8")


def _result_page(ok: bool, title: str, message: str, redirect: str = None) -> str:
    color = "#3ddc84" if ok else "#ff5c5c"
    script = ""
    if redirect:
        script = ("<script>setTimeout(function(){location.href=%r;},900);</script>"
                  % redirect)
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>%s</title>
<style>
 body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
      background:#0d1220;color:#e8eefc;font-family:"Microsoft YaHei",system-ui,sans-serif}
 .card{background:#141c2f;border:1px solid #24304a;border-radius:14px;padding:34px 44px;
       text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.5)}
 h1{color:%s;font-size:22px;margin:0 0 10px}
 p{color:#9fb0cd;margin:0;font-size:14px;line-height:1.7}
 a{color:#5aa9ff}
</style></head><body><div class="card"><h1>%s</h1><p>%s</p>
<p style="margin-top:14px"><a href="/">如果没有自动跳转，点这里返回应用</a></p>
</div>%s</body></html>""" % (title, color, title, message, script)


# ==========================================================================
# 启动
# ==========================================================================

def create_server(config: dict, host: str = None, port: int = None):
    app = App(config)
    handler = type("BoundHandler", (Handler,), {"app": app})
    host = host or config.get("host", "127.0.0.1")
    port = int(port or config.get("port", 8420))
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server, app


def serve(config: dict, host: str = None, port: int = None, open_browser: bool = True):
    server, app = create_server(config, host, port)
    host, port = server.server_address[:2]
    url = "http://%s:%d/" % (host if host != "0.0.0.0" else "127.0.0.1", port)
    utils.log("=" * 62)
    utils.log(" %s v%s 已启动", APP_NAME, __version__)
    utils.log(" 界面地址：%s", url)
    utils.log(" 数据目录：%s", cfgmod.DATA_DIR)
    if not app.store.sde_ready():
        utils.log(" ⚠ 还没有导入 SDE 静态数据，界面里点「导入 SDE」即可（约 95 MB）")
    if not app.sso.configured():
        utils.log(" ⚠ 还没有配置 EVE 应用的 client_id，暂不能登录")
    utils.log(" 按 Ctrl+C 停止")
    utils.log("=" * 62)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        utils.log("正在停止…")
    finally:
        server.shutdown()
        app.store.close()
    return url
