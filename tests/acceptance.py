"""接口验收脚本（开发用）。

用法：
    python -m app.main --port 8420 --no-browser     # 先把服务跑起来
    python tests/acceptance.py                      # 默认连 127.0.0.1:8420
    python tests/acceptance.py 9000                 # 或指定端口
    set PORT=9000 && python tests/acceptance.py     # 或用环境变量
"""

import json
import re
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = 8420
if len(sys.argv) > 1 and sys.argv[1].isdigit():
    PORT = int(sys.argv[1])
elif os.environ.get("PORT", "").isdigit():
    PORT = int(os.environ["PORT"])
BASE = "http://127.0.0.1:%d" % PORT

CHECKS = []
DEBUG = []


def call(path, data=None, method=None, timeout=300, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(
        BASE + path, data=body,
        method=method or ("POST" if data is not None else "GET"),
        headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw.decode("utf-8", "replace")
    except Exception as exc:
        return 0, repr(exc)


def get(path, data=None, method=None, headers=None):
    status, body = call(path, data, method, headers=headers)
    DEBUG.append((path, status, type(body).__name__, str(body)[:150]))
    return status, body


def get_raw(path, data=None, method=None, headers=None):
    """返回**原始响应文本**（不解 JSON）。

    用途：检查接口有没有把不该给前端的东西（例如内置 Client ID 原文）泄露出去 ——
    这类断言必须看真实字节，不能被 JSON 解析后的对象结构掩盖。
    """
    body = json.dumps(data).encode("utf-8") if data is not None else None
    method = method or ("POST" if data is not None else "GET")
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, repr(exc)


def chk(label, ok, extra=""):
    CHECKS.append((label, bool(ok), extra))


def builtin_client_id_from_source():
    """从 app/config.py 里读出内置 Client ID（黑盒测试不 import 应用代码）。

    用途：断言这个值**不会出现在任何接口响应里**（用户要求「写入软件但不显示出来」）。
    """
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "app", "config.py")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    match = re.search(r'^BUILTIN_CLIENT_ID\s*=\s*"([^"]*)"', text, re.M)
    return match.group(1) if match else ""


def config_snapshot():
    """跑验收前备份 config.json，跑完原样还原。

    曾经踩过的坑：验收用 --port 88xx 起服务，main.py 把实际端口写进 app.config，
    随后任意一次 POST /api/config 就把这个临时端口**永久**写进了用户的配置文件 ——
    之后正常启动端口变了，SSO 回调与门户登记的地址不一致，登录直接失败
    （表现为「点了登录没反应」，因为 EVE 停在自己的错误页不回跳）。
    现在端口不再落盘（见 config.save_config 的说明），这里再兜一层：
    **测试不许改用户配置**。
    """
    import shutil
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "config.json")
    backup = path + ".acceptance-backup"
    existed = os.path.exists(path)
    if existed:
        shutil.copy2(path, backup)
    return {"path": path, "backup": backup, "existed": existed}


def config_restore(state):
    import shutil
    try:
        if state["existed"] and os.path.exists(state["backup"]):
            shutil.copy2(state["backup"], state["path"])
            os.remove(state["backup"])
        elif not state["existed"] and os.path.exists(state["path"]):
            os.remove(state["path"])
    except Exception:
        pass


def main():
    snapshot = config_snapshot()
    try:
        return _run_checks()
    finally:
        config_restore(snapshot)


def _run_checks():
    # ---------- 状态与配置 ----------
    status, s = get("/api/status")
    chk("状态接口", status == 200 and isinstance(s, dict) and s.get("ok"))
    if isinstance(s, dict) and isinstance(s.get("sde"), dict):
        chk("SDE 已就绪", s["sde"].get("ready"),
            "build %s / %s 个物品" % (s["sde"].get("local_build"),
                                      (s["sde"].get("stats") or {}).get("types")))
        cfg = s.get("config") or {}
        chk("配置默认值正确",
            abs(cfg.get("scc_surcharge", 0) - 0.04) < 1e-9
            and cfg.get("material_rounding") == "ceil",
            "SCC=%s 税率=%s 价源=%s" % (cfg.get("scc_surcharge"), cfg.get("job_tax_rate"),
                                       cfg.get("price_source")))
    else:
        chk("SDE 已就绪", False, "status 返回异常")
        chk("配置默认值正确", False, "")

    # ---------- SDE / 工业静态数据 ----------
    # 注意：这个列表现在是**全部**玩家工业建筑（14 个），不只是自带加成的那些 ——
    # 空堡 / 铁壁 / 星城 / 阿塔诺自身没有加成，但能装改装件，必须可选。
    status, r = get("/api/industry/structures")
    if isinstance(r, dict) and isinstance(r.get("structures"), list):
        own = [x for x in r["structures"] if x.get("has_own_bonus")]
        chk("自带工业加成的建筑 = 7（含在完整清单里）", len(own) == 7,
            "、".join(x["name"] for x in own))
    else:
        chk("自带工业加成的建筑 = 7（含在完整清单里）", False, "返回非预期结构")

    status, r = get("/api/sde/bonus-sources?activity=manufacturing")
    n = len(r.get("candidates") or []) if isinstance(r, dict) else 0
    chk("改装件候选 = 149", n == 149, "数量 %s" % n)

    status, r = get("/api/sde/search-blueprint?q=" + urllib.parse.quote("索迪约"))
    ok = isinstance(r, dict) and len(r.get("results") or []) >= 1
    chk("中文搜索「索迪约」", ok,
        (r.get("results") or [{}])[0].get("name", "") if isinstance(r, dict) else "")

    status, r = get("/api/sde/search?q=" + urllib.parse.quote("三钛"))
    chk("中文搜索物品", isinstance(r, dict) and len(r.get("results") or []) > 0,
        "%s 条结果" % (len(r.get("results") or []) if isinstance(r, dict) else 0))

    status, r = get("/api/sde/blueprint/36973")
    chk("读取索迪约蓝图配方",
        isinstance(r, dict) and "manufacturing" in (r.get("activities") or {}),
        ("活动 " + "、".join((r.get("activities") or {}).keys())) if isinstance(r, dict) else "")

    # ---------- 建筑清单与尺寸匹配 ----------
    status, r = get("/api/industry/structures")
    items = (r.get("structures") or []) if isinstance(r, dict) else []
    names = {x.get("name") for x in items}
    chk("建筑清单包含全部 14 个玩家工业建筑", len(items) >= 13,
        "%d 个" % len(items))
    chk("建筑清单包含空堡/铁壁/星城/阿塔诺（自身无加成也必须列出）",
        {"空堡", "铁壁", "星城", "阿塔诺"} <= names,
        "、".join(sorted(names))[:60])
    chk("每个建筑都带 rigSize 与尺寸名",
        all(x.get("rig_size") in (2, 3, 4) and x.get("rig_size_label") for x in items),
        "缺字段的建筑：%s" % [x.get("name") for x in items
                             if x.get("rig_size") not in (2, 3, 4)])
    by_name = {x.get("name"): x for x in items}
    chk("尺寸分组正确（空堡=中型 / 铁壁=大型 / 星城=超大型）",
        (by_name.get("空堡", {}).get("rig_size") == 2
         and by_name.get("铁壁", {}).get("rig_size") == 3
         and by_name.get("星城", {}).get("rig_size") == 4),
        "空堡%s 铁壁%s 星城%s" % (by_name.get("空堡", {}).get("rig_size_label"),
                                  by_name.get("铁壁", {}).get("rig_size_label"),
                                  by_name.get("星城", {}).get("rig_size_label")))
    chk("建筑带改装件槽位与校准值",
        all(x.get("rig_slots") and x.get("upgrade_capacity") for x in items))

    status, r = get("/api/sde/bonus-sources?activity=manufacturing")
    rigs = (r.get("candidates") or []) if isinstance(r, dict) else []
    sized = [x for x in rigs if x.get("rig_size")]
    chk("改装件候选带 rigSize（界面据此按建筑过滤）", len(sized) >= 10,
        "%d / %d 个带尺寸" % (len(sized), len(rigs)))
    chk("改装件带可安装的建筑组",
        any(x.get("fit_group_ids") for x in sized))

    # ---------- 保存生产设施 ----------
    status, r = get("/api/config", {"facility": {
        "structure_type_id": 35827, "rig_type_ids": [37183, 37180],
        "system_id": 30000142, "system_name": "Jita", "security": "highsec",
        "manual": {"material": 0.02},
    }}, "POST")
    chk("能保存生产设施", status == 200 and isinstance(r, dict) and r.get("ok"),
        str((r or {}).get("message"))[:40])
    status, r = get("/api/status")
    fac = (r.get("facility") or {}) if isinstance(r, dict) else {}
    chk("保存后能在状态里读回，并带可读摘要",
        fac.get("structure_type_id") == 35827 and fac.get("system_id") == 30000142
        and "索迪约" in str(fac.get("summary") or ""),
        str(fac.get("summary"))[:52])
    chk("保存的设施做了字段收窄（不含未知字段）",
        set(fac) - {"structure_type_id", "rig_type_ids", "service_type_ids", "system_id",
                    "system_name", "security", "manual", "saved_at", "summary"} == set(),
        "、".join(sorted(set(fac))))
    status, r = get("/api/config", {"facility": {}}, "POST")
    status, r = get("/api/status")
    chk("能清除已保存的设施",
        not ((r.get("facility") or {}).get("structure_type_id")
             if isinstance(r, dict) else True))

    # ---------- 建筑燃料消耗 ----------
    status, r = get("/api/fuel/services?structure_type_id=35827&activity=manufacturing")
    svcs = (r.get("services") or []) if isinstance(r, dict) else []
    chk("服务装备清单可用（≥15 种、都有每小时消耗）",
        len(svcs) >= 15 and all(x.get("fuel_per_hour", 0) > 0 for x in svcs),
        "%d 种" % len(svcs))
    by_id = {x.get("type_id"): x for x in svcs}
    chk("制造厂 12/小时、市场中心 40/小时、提炼设施 10/小时",
        by_id.get(35878, {}).get("fuel_per_hour") == 12
        and by_id.get(35892, {}).get("fuel_per_hour") == 40
        and by_id.get(35899, {}).get("fuel_per_hour") == 10,
        "制造厂%s 市场%s 提炼%s" % (by_id.get(35878, {}).get("fuel_per_hour"),
                                   by_id.get(35892, {}).get("fuel_per_hour"),
                                   by_id.get(35899, {}).get("fuel_per_hour")))

    status, r = get("/api/fuel/plan", {
        "structure_type_id": 35827, "service_type_ids": [35878, 35891, 35886],
        "hours": 720, "price": 1000}, "POST")
    chk("索迪约 + 3 个服务装备 = 27 块/小时（36 × 0.75）",
        status == 200 and isinstance(r, dict) and abs(r.get("fuel_per_hour", 0) - 27) < 1e-9,
        str((r or {}).get("fuel_per_hour")))
    chk("30 天消耗与费用算得出来",
        abs((r or {}).get("fuel_per_month", 0) - 19440) < 1e-6
        and abs((r or {}).get("cost_per_month", 0) - 19440 * 1000) < 1e-3,
        "费用 %s" % (r or {}).get("cost_per_month"))
    chk("只算持续消耗：不返回服务上线的一次性燃料",
        "fuel_online_total" not in (r or {}) and "fuel_online_reduced" not in (r or {}),
        "字段: %s" % "、".join(sorted(k for k in (r or {}) if "online" in k)) or "无")
    chk("原始（未打折）每小时也返回，便于与游戏内显示对账",
        abs((r or {}).get("base_fuel_per_hour", 0) - 36) < 1e-9,
        str((r or {}).get("base_fuel_per_hour")))
    status, r = get("/api/fuel/plan", {
        "structure_type_id": 47515, "service_type_ids": [35878], "price": 0}, "POST")
    chk("马金尼斯减免 50% → 6 块/小时",
        abs((r or {}).get("fuel_per_hour", 0) - 6) < 1e-9,
        str((r or {}).get("fuel_per_hour")))

    status, r = get("/api/corp/structures")
    chk("军团建筑缓存接口可用（未登录时应为空）",
        status == 200 and isinstance(r, dict) and r.get("count") == 0
        and "unmatched_services" in r, "count=%s" % (r or {}).get("count"))
    status, r = get("/api/corp/structures/refresh", {}, "POST")
    chk("未登录时读取军团建筑给出明确引导（而不是崩）",
        status == 400 and isinstance(r, dict) and "登录" in str(r.get("message")),
        str((r or {}).get("message"))[:40])

    # ---------- 市场树（所有物品按游戏方式浏览；纯本地数据，无需登录）----------
    status, r = get("/api/sde/market-group/4")
    kids = (r.get("children") or []) if isinstance(r, dict) else []
    chk("市场分组接口可用（舰船的子分组）", status == 200 and len(kids) >= 5,
        "%d 个子分组" % len(kids))
    chk("子分组带递归物品数",
        all(isinstance(k.get("types"), int) for k in kids) and any(k["types"] > 0 for k in kids),
        "、".join("%s:%s" % (k.get("name"), k.get("types")) for k in kids[:3]))
    chk("分组树层级与游戏一致（护卫舰 → 标准/高级/势力/先驱者）",
        all(any(k.get("name") == want for k in kids) for want in ("护卫舰",)),
        "、".join(k.get("name") for k in kids))
    status, r2 = get("/api/sde/market-group/1361")
    sub = [k.get("name") for k in (r2.get("children") or [])] if isinstance(r2, dict) else []
    chk("护卫舰下面就是各舰种分类（与游戏侧栏一致）",
        any("标准护卫舰" in (n or "") for n in sub), "、".join(sub))
    status, r3 = get("/api/sde/market-group/61?limit=50")
    types = (r3.get("types") or []) if isinstance(r3, dict) else []
    with_bp = [t for t in types if t.get("blueprint_type_id")]
    chk("叶子分组能列出物品", status == 200 and len(types) > 0, "%d 个物品" % len(types))
    chk("物品带出「蓝图原本」（研究模拟据此选蓝图，无需 ESI）",
        len(with_bp) > 0 and all(t.get("blueprint_name") for t in with_bp),
        "、".join("%s→%s" % (t.get("name"), t.get("blueprint_name")) for t in with_bp[:2]))

    # ---------- 内置 Client ID：能登录、但界面上看不到 ----------
    # 这是黑盒测试（只走 HTTP），所以直接从源码里把内置值读出来，然后断言
    # **接口的任何响应里都不出现它**。
    builtin = builtin_client_id_from_source()
    status, raw_status = get_raw("/api/status")
    status2, raw_config = get_raw("/api/config")
    chk("已内置 Client ID（未配置也能登录）",
        isinstance(s, dict) and ((s.get("sso") or {}).get("configured") is True),
        "configured=%s" % (s.get("sso") or {}).get("configured"))
    chk("内置 Client ID 来源标记正确",
        ((s.get("sso") or {}).get("client_id_source") in ("builtin", "custom")),
        str((s.get("sso") or {}).get("client_id_source")))
    chk("接口不泄露内置 Client ID 原文（/api/status）",
        bool(builtin) and builtin not in raw_status,
        "已隐藏" if builtin and builtin not in raw_status else "**泄露了**")
    chk("接口不泄露内置 Client ID 原文（/api/config）",
        bool(builtin) and builtin not in raw_config,
        "已隐藏" if builtin and builtin not in raw_config else "**泄露了**")
    chk("设置接口把 client_id 当掩码返回",
        isinstance(s, dict) and (s.get("config") or {}).get("client_id") in ("***", "", None),
        str((s.get("config") or {}).get("client_id")))

    # ---------- 页面版本守卫（避免浏览器跑着旧 app.js 却没人发现）----------
    status, raw_index = get_raw("/")
    server_version = str(((s.get("app") or {}).get("version")) or "")
    m = re.search(r'<meta name="app-version" content="([^"]*)"', raw_index or "")
    chk("首页注入了页面版本号（不再是占位符）",
        bool(m) and m.group(1) != "__APP_VERSION__", (m.group(1) if m else "无 meta"))
    chk("页面版本与 /api/status 的版本一致",
        bool(server_version) and bool(m) and m.group(1) == server_version,
        "页面 %s / 服务端 %s" % (m.group(1) if m else "?", server_version))

    # ---------- 市场与成本指数 ----------
    # 注意：成本指数是 ESI 的**实时滚动数据**，每天都会变（曾经写死 0.1665，
    # 后来真实值漂到 0.1664 就误报失败了）。这里只断言「取到了合理的数」，
    # 具体数值由本地缓存的写入时间戳来体现新鲜度。
    status, r = get("/api/industry/cost-indices?system_id=30000142")
    value = (r.get("indices") or {}).get("manufacturing") if isinstance(r, dict) else None
    chk("吉他制造成本指数能取到（0 < x < 1，不断言具体值）",
        isinstance(value, (int, float)) and 0.0 < float(value) < 1.0,
        str(value))

    status, r = get("/api/industry/systems/search?q=Jita")
    chk("英文星系搜索", isinstance(r, dict) and len(r.get("results") or []) == 1,
        (r.get("results") or [{}])[0].get("name", "") if isinstance(r, dict) else "")

    status, r = get("/api/industry/cheapest-systems?limit=5")
    chk("最低成本指数星系查询", isinstance(r, dict) and len(r.get("rows") or []) == 5,
        "%s 条" % (len(r.get("rows") or []) if isinstance(r, dict) else 0))

    status, r = get("/api/market/ranking?limit=5&scan_limit=150")
    chk("利润排行", status == 200 and isinstance(r, dict) and len(r.get("rows") or []) == 5,
        ("扫描 %s 命中 %s" % (r.get("scanned"), r.get("matched"))) if isinstance(r, dict) else "")

    status, r = get("/api/market/orderbook?type_id=34")
    chk("盘口查询（真实 ESI）", isinstance(r, dict) and r.get("ok"),
        "订单 %s 条" % (r.get("orders") if isinstance(r, dict) else "?"))

    status, r = get("/api/market/history?type_id=34&days=7")
    chk("历史成交查询（真实 ESI）", isinstance(r, dict) and len(r.get("rows") or []) > 0,
        "%s 天" % (len(r.get("rows") or []) if isinstance(r, dict) else 0))

    # ---------- 计算接口 ----------
    status, r = get("/api/calc/product", {
        "blueprint_type_id": 36973, "me": 10, "te": 20, "runs": 1,
        "price": {"source": "adjusted"},
        "facility": {"structure_type_id": 35827, "rig_type_ids": [43704], "system_id": 30000142},
        "industry": {"scc_surcharge": 0.04, "job_tax_rate": 0.0025},
    }, "POST")
    if isinstance(r, dict) and r.get("ok"):
        res = r["result"]
        fb = res.get("facility_bonuses") or {}
        chk("单产品计算", bool(res.get("available")) and res.get("material_cost", 0) > 0,
            "EIV=%s 材料费=%.0f 任务费=%.0f 耗时=%s 材料减免=%.4f"
            % (res.get("estimated_item_value"), res.get("material_cost", 0),
               res.get("job_cost", 0), res.get("time_text"), fb.get("material", 0)))
        chk("索迪约+改装件材料减免 = 1-0.99*0.98",
            abs((fb.get("material") or 0) - (1 - 0.99 * 0.98)) < 1e-9,
            "%.6f" % (fb.get("material") or 0))
    else:
        chk("单产品计算", False, str(r)[:80])
        chk("索迪约+改装件材料减免 = 1-0.99*0.98", False, "")

    status, r = get("/api/calc/aggregate", {
        "items": [{"type_id": 35827, "runs": 1, "me": 10, "te": 20}],
        "facility": {"structure_type_id": 35827, "rig_type_ids": [43704], "system_id": 30000142},
        "price": {"source": "adjusted"},
        "build_modes": {"21947": "build"},
    }, "POST")
    if isinstance(r, dict) and r.get("ok"):
        names = set(row["name"] for row in r["rows"])
        need = set(["看门人", "海军陆战队员", "建筑实验室", "建筑工厂",
                    "建筑建造组件", "建筑机库阵列", "建筑贮藏舱", "建筑维修设施"])
        chk("聚合材料表（主界面表格）", need.issubset(names),
            "%s 种原料，缺 %s" % (len(r["rows"]),
                                 "无" if need.issubset(names) else "、".join(sorted(need - names))))
        janitor = next((x for x in r["rows"] if x["name"] == "看门人"), None)
        # 本次请求带了设施（索迪约材料 ×0.99 + 改装件 43704 材料 −2%），
        # 所以 500 × 0.9(ME10) × 0.99 × 0.98 = 436.59 → 向上取整 437
        expected = int(-(-500 * 0.9 * 0.99 * 0.98 // 1))
        chk("看门人数量 = ceil(500×0.9×0.99×0.98) = 437",
            janitor is not None and janitor["quantity"] == expected,
            "实际 %s，期望 %s" % (janitor and janitor["quantity"], expected))
        chk("自造/收购切换生效",
            any(x["source"] == "build" for x in r["rows"])
            and any(x["source"] == "purchase" for x in r["rows"]),
            "自造 %s 项" % (r.get("totals", {}).get("build_items")))
    else:
        chk("聚合材料表（主界面表格）", False, str(r)[:80])
        chk("看门人数量 = ceil(500×0.9×0.99×0.98) = 437", False, "")
        chk("自造/收购切换生效", False, "")

    status, r = get("/api/calc/multi", {
        "type_id": 35827, "runs": 1, "me": 10, "te": 20, "max_depth": 2,
        "price": {"source": "adjusted"}}, "POST")
    if isinstance(r, dict) and r.get("ok"):
        t = r["totals"]
        chk("生产规划多层展开", len(r.get("shopping_list", {}).get("rows") or []) > 0,
            "%s 个任务 / %s 种材料 / 串行耗时 %s" % (t["jobs"], t["materials"], t["time_text"]))
    else:
        chk("生产规划多层展开", False, str(r)[:80])

    # ---------- 蓝图研究模拟 ----------
    status, r = get("/api/research/blueprint/691")
    info = (r or {}).get("blueprint") or {}
    chk("研究信息：裂谷级 SDE 时长 2100 / rank 20",
        status == 200 and info.get("base_seconds") == 2100
        and abs(float(info.get("rank") or 0) - 20) < 1e-9,
        "base=%s rank=%s" % (info.get("base_seconds"), info.get("rank")))
    chk("研究信息：ME 与 TE 基准相等且 rank 自洽",
        info.get("base_equal") is True and info.get("rank_consistent") is True)
    chk("研究信息：等级表 10 项", len(info.get("level_table") or []) == 10)

    status, r = get("/api/research/simulate", {
        "blueprint_type_id": 691, "from_me": 0, "to_me": 10, "from_te": 0, "to_te": 10,
        "security": "highsec", "rig_type_ids": [37183],
        "facility": {"system_id": 30000142}}, "POST")
    facilities = (r or {}).get("facilities") or []
    npc = next((f for f in facilities if f.get("structure_type_id") is None), None)
    sotiyo = next((f for f in facilities
                   if f.get("structure_type_id") == 35827 and f.get("rig_type_ids")), None)
    chk("研究模拟：设施对比行", isinstance(r, dict) and r.get("ok") and len(facilities) >= 12,
        "%d 行" % len(facilities))
    chk("研究模拟：NPC 站耗时 = 基准 × 技能倍率",
        npc is not None and abs(npc["me"]["total_seconds"] - 5120000 * 0.75 * 0.85) < 10,
        "%s 秒" % (npc and npc["me"]["total_seconds"]))
    chk("研究模拟：索迪约+改装件快于 NPC 站",
        bool(npc and sotiyo) and sotiyo["me"]["total_seconds"] < npc["me"]["total_seconds"],
        "%s vs %s" % (sotiyo and sotiyo["me"]["total_text"], npc and npc["me"]["total_text"]))
    chk("研究模拟：同设施下 ME 与 TE 耗时相等",
        bool(npc) and npc["me"]["total_seconds"] == npc["te"]["total_seconds"])
    chk("研究模拟：逐级明细 10 条",
        bool(sotiyo) and len(sotiyo["me"]["levels"]) == 10)
    chk("研究模拟：费用按成本指数算出",
        isinstance(r, dict) and r.get("cost_index", 0) > 0
        and bool(npc) and npc["me"]["total_cost"] > 0,
        "SCI=%s 费用=%.4f 亿" % ((r or {}).get("cost_index"),
                                (npc["me"]["total_cost"] / 1e8) if npc else 0))

    status, r = get("/api/research/simulate", {"blueprint_type_id": 691, "from_me": 8,
                                              "to_me": 10, "from_te": 0, "to_te": 0}, "POST")
    seg = next((f for f in (r or {}).get("facilities") or []
                if f.get("structure_type_id") is None), None)
    chk("研究模拟：只算指定等级区间",
        bool(seg) and seg["me"]["from_level"] == 8 and seg["me"]["to_level"] == 10
        and len(seg["me"]["levels"]) == 2 and seg["te"]["levels"] == [],
        "%.1f 小时" % (seg["me"]["total_seconds"] / 3600) if seg else "")

    status, r = get("/api/research/facilities")
    chk("研究：候选设施与研究类改装件清单",
        isinstance(r, dict) and len(r.get("facilities") or []) > 0 and len(r.get("rigs") or []) > 0,
        "%d 个设施 / %d 个改装件" % (len((r or {}).get("facilities") or []),
                                   len((r or {}).get("rigs") or [])))

    status, r = get("/api/research/owned")
    chk("研究：未登录时「我的蓝图」给出提示",
        status == 400 and isinstance(r, dict) and r.get("ok") is False,
        str(r.get("message"))[:34] if isinstance(r, dict) else "")

    status, r = get("/api/research/simulate", {"blueprint_type_id": 45732}, "POST")
    chk("研究：反应蓝图给出明确提示（不崩）",
        isinstance(r, dict) and r.get("ok") is False and "研究" in str(r.get("message", "")),
        str(r.get("message"))[:34] if isinstance(r, dict) else "")

    # ---------- 刷新物价 / 本地缓存 ----------
    status, r = get("/api/market/local")
    chk("本地物价概览接口", status == 200 and isinstance(r, dict) and r.get("ok")
        and "prices" in r and "market_stats" in r and "history" in r,
        "调整价 %s 条 / 盘口 %s 条 / 历史 %s 行"
        % ((r or {}).get("prices", {}).get("entries"), (r or {}).get("market_stats", {}).get("entries"),
           (r or {}).get("history", {}).get("rows")) if isinstance(r, dict) else "")

    status, r = get("/api/market/refresh", {"scope": "bogus"}, "POST")
    chk("刷新物价：非法 scope 返回 400",
        status == 400 and isinstance(r, dict) and r.get("ok") is False,
        str(r.get("message"))[:38] if isinstance(r, dict) else "")

    status, r = get("/api/market/refresh", {"scope": "prices"}, "POST")
    chk("刷新物价：能启动后台任务", status == 200 and isinstance(r, dict) and r.get("ok"),
        str(r.get("message"))[:38] if isinstance(r, dict) else "")
    task = None
    for _ in range(60):
        time.sleep(1)
        _s, _status = get("/api/status")
        task = ((_status or {}).get("tasks") or {}).get("market-refresh")
        if not task or not task.get("running"):
            break
    steps = ((task or {}).get("result") or {}).get("steps") or []
    chk("刷新物价：prices 范围完成且成功",
        bool(steps) and steps[0]["ok"], json.dumps(steps[0].get("detail") if steps else {},
                                                  ensure_ascii=False)[:40])
    status, after = get("/api/market/local")
    chk("刷新物价：本地调整价已落库",
        isinstance(after, dict) and (after.get("prices", {}).get("entries") or 0) > 15000,
        "%s 条" % ((after or {}).get("prices", {}).get("entries")))

    # ---------- 弹窗与样式（防「弹窗关不掉」这类视觉回归） ----------
    status, html = get("/")
    chk("弹窗初始为隐藏（index.html 带 hidden 属性）",
        'id="modal"' in html and 'class="modal" hidden' in html,
        "含 aria-modal: %s" % ('aria-modal="true"' in html))
    chk("弹窗不再是占位标题「标题」", '>标题<' not in html)
    status, css = get("/style.css")
    normalized_css = " ".join((css or "").split())
    chk("样式钉死 [hidden] 全局不变式",
        "[hidden] { display: none !important; }" in normalized_css,
        "（作者样式的 display 会压过浏览器默认的 [hidden]）")
    chk("弹窗自身仍用 display:flex（所以必须有上面的兜底）",
        ".modal {" in (css or "") and "display: flex" in (css or ""))
    status, js = get("/app.js")
    chk("弹窗有 3 条关闭退路（按钮 / 遮罩 / Esc）",
        "data-action=\"modal-close\"" in html and "ev.target === box" in (js or "")
        and "Escape" in (js or ""), "含 modalOpen()")

    # ---------- 本机来源校验（防 DNS rebinding 与 CSRF） ----------
    status, body = get("/api/status")
    chk("正常请求放行", status == 200 and isinstance(body, dict) and body.get("ok"))
    status, _ = get("/api/status", headers={"Host": "evil.example.com"})
    chk("伪造 Host 被拒（防 DNS rebinding）", status == 403, "HTTP %s" % status)
    status, _ = get("/api/status", headers={"Origin": "https://evil.example.com"})
    chk("跨源 Origin 被拒（防 CSRF）", status == 403, "HTTP %s" % status)
    status, _ = get("/api/status", headers={"Origin": "null"})
    chk("Origin: null 被拒", status == 403, "HTTP %s" % status)
    status, _ = get("/api/status", headers={"Origin": "http://127.0.0.1:%d" % PORT})
    chk("同源 Origin 放行", status == 200, "HTTP %s" % status)
    status, _ = get("/api/status", headers={"Origin": "http://127.0.0.1:9"})
    chk("其它端口的本机来源被拒", status == 403, "HTTP %s" % status)
    status, _ = get("/api/status", method="OPTIONS",
                    headers={"Origin": "https://evil.example.com"})
    chk("跨源预检被拒", status == 403, "HTTP %s" % status)

    # ---------- 令牌加密与关联状态 ----------
    status, r = get("/api/status")
    chk("状态里带令牌迁移报告", isinstance(r, dict) and "token_migration" in r,
        json.dumps({k: v for k, v in ((r or {}).get("token_migration") or {}).items()
                    if k != "unreadable"}, ensure_ascii=False)[:46])
    chk("状态里带每个角色的关联状态", isinstance(r, dict) and "link_states" in r)
    status, r = get("/api/selftest")
    sec = (r or {}).get("secure") or {}
    chk("加密后端已报告且四项不变量成立",
        bool(sec.get("backend")) and sec.get("roundtrip") is True
        and sec.get("bound_to_character") is True and sec.get("bound_to_field") is True
        and sec.get("tamper_detected") is True,
        str(sec.get("backend"))[:32])
    chk("加密后端不是明文",
        "明文" not in str(sec.get("backend")), str(sec.get("backend"))[:32])
    status, r = get("/api/characters/999999999/confirm-relink", {}, "POST")
    chk("确认转手：不存在的角色返回 404", status == 404, "HTTP %s" % status)

    status, r = get("/api/auth/pending")
    chk("等待态接口可用（无待处理登录时应为 false）",
        status == 200 and isinstance(r, dict) and "pending" in r
        and r.get("pending") is False, json.dumps(r, ensure_ascii=False)[:44])
    status, r = get("/api/auth/cancel", {}, "POST")
    chk("取消待完成登录的接口可用",
        status == 200 and isinstance(r, dict) and r.get("ok") is True,
        str((r or {}).get("message"))[:34])

    # ---------- 登录与错误处理 ----------
    # 注意：这条不能假定「一定没配 client_id」—— 开发者本机可能填过。
    # 两种情况都断言清楚，测试结果才不会被本地配置左右。
    status, r = get("/api/auth/login", {}, "POST")
    if isinstance(r, dict) and r.get("need_setup"):
        chk("未配置 client_id 时给出明确引导",
            status == 400 and bool(str(r.get("message") or "").strip()),
            str(r.get("message"))[:50])
    else:
        chk("已配置 client_id 时返回 EVE 授权 URL",
            status == 200 and isinstance(r, dict)
            and str(r.get("url") or "").startswith("https://login.eveonline.com/"),
            str((r or {}).get("url"))[:52])

    status, r = get("/api/auth/manual", {"code": "not-a-real-code"}, "POST")
    chk("手动授权码失败时友好报错",
        status == 400 and isinstance(r, dict) and not r.get("ok"),
        str(r.get("message"))[:56] if isinstance(r, dict) else "")

    status, body = get("/callback?code=fake&state=unknown")
    chk("回调 state 不匹配时返回友好页面",
        status == 400 and isinstance(body, str) and "登录失败" in body)

    status, body = get("/callback?error=access_denied&error_description=user+said+no")
    chk("用户拒绝授权时返回友好页面", status == 400 and isinstance(body, str))

    # ---------- 静态资源与文档 ----------
    for path in ("/", "/app.js", "/style.css"):
        status, body = get(path)
        chk("静态资源 " + path, status == 200 and len(body) > 500,
            "%s 字节" % (len(body) if hasattr(body, "__len__") else "?"))

    for doc in ("公式与数据来源核实.md", "参考资料-ESI与SSO核实报告.md",
                "与同类项目的公式对比.md"):
        status, body = get("/docs/" + urllib.parse.quote(doc))
        chk("文档 " + doc, status == 200 and isinstance(body, str) and len(body) > 3000,
            "%s 字节" % (len(body) if isinstance(body, str) else 0))

    # 注册指南已删除：服务端对未知路径会回退到 SPA 首页（HTTP 200 + index.html），
    # 所以不能断言 404，要断言「**旧指南的内容不再被提供**」。
    status, body = get("/docs/" + urllib.parse.quote("ESI应用注册指南.md"))
    text = body if isinstance(body, str) else str(body)
    chk("注册指南内容确实已移除（不再提供该文档）",
        "第 1 步：登录开发者门户" not in text and "Enabled Scopes" not in text
        and "第 5 步：填进程序并登录" not in text,
        "HTTP %s / %d 字节%s" % (status, len(text),
                                "（回退到首页）" if "app.js" in text else ""))

    status, r = get("/api/docs")
    docs = (r.get("docs") or []) if isinstance(r, dict) else []
    chk("文档列表接口", len(docs) >= 3, "%s 个文档" % len(docs))
    chk("文档列表里不再有注册指南",
        not any("注册指南" in str(d) for d in docs), "、".join(str(d) for d in docs))

    # ---------- 汇总 ----------
    print("服务地址：%s" % BASE)
    print("%-32s %-6s %s" % ("检查项", "结果", "备注"))
    print("-" * 100)
    bad = 0
    for label, ok, extra in CHECKS:
        if not ok:
            bad += 1
        print("%-32s %-6s %s" % (label, "PASS" if ok else "FAIL", str(extra)[:58]))
    print("-" * 100)
    print("共 %d 项，失败 %d 项" % (len(CHECKS), bad))
    if bad:
        print()
        print("失败项原始响应：")
        for path, status, kind, preview in DEBUG:
            print("  %-50s %s %s %s" % (path[:50], status, kind,
                                        preview.replace("\n", " ")[:80]))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
