"""配置：默认值 + config.json 覆盖。

所有路径都相对本文件推导，因此整个目录可以随意挪动。
"""

from __future__ import annotations

import os

from . import utils

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
WEB_DIR = os.path.join(APP_DIR, "web")
DATA_DIR = os.path.join(ROOT, "data")
SDE_DIR = os.path.join(DATA_DIR, "sde")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
CONFIG_PATH = os.path.join(ROOT, "config.json")
DB_PATH = os.path.join(DATA_DIR, "industry.db")

# --------------------------------------------------------------------------
# 欧服固定值
# --------------------------------------------------------------------------
# 官方现已把 /latest、/vN、/dev 合并，推荐「无前缀 + 兼容日期头」的形式。
ESI_BASE = "https://esi.evetech.net"
ESI_DATASOURCE = "tranquility"          # 欧服 = Tranquility
# 兼容日期：留空表示不发送该头（始终跟随最新行为）；
# 填一个过去的日期可把行为固定在那一版，避免被 CCP 的破坏性改动影响。
# 实测规则：最早 2020-01-01，不能是未来日期（未来会返回 400）。
COMPATIBILITY_DATE = "2026-08-18"
IMAGE_SERVER = "https://images.evetech.net"

SSO_METADATA_URL = "https://login.eveonline.com/.well-known/oauth-authorization-server"
SSO_AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
SSO_JWKS_URL = "https://login.eveonline.com/oauth/jwks"

SDE_LATEST_URL = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
SDE_ZIP_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"

# 常见市场区域（全部为欧服 Tranquility）
REGIONS = {
    "the_forge": {"id": 10000002, "name": "The Forge（吉他 Jita）", "station_id": 60003760},
    "domain": {"id": 10000043, "name": "Domain（艾玛 Amarr）", "station_id": 60008494},
    "sinq_laison": {"id": 10000032, "name": "Sinq Laison（多德ixie Dodixie）", "station_id": 60011866},
    "heimatar": {"id": 10000030, "name": "Heimatar（伦斯 Rens）", "station_id": 60004588},
    "metropolis": {"id": 10000042, "name": "Metropolis（赫克 Hek）", "station_id": 60005686},
    "vale_of_the_silent": {"id": 10000003, "name": "Vale of the Silent（欧服工业带）"},
    "lonetrek": {"id": 10000016, "name": "Lonetrek（欧服工业带）"},
    "the_citadel": {"id": 10000033, "name": "The Citadel（欧服工业带）"},
}

# 需要的 ESI scope。**注册 EVE 应用时必须把下面全部勾上**（SSO 按并集校验，
# 请求未勾选的 scope 会以 invalid_scope 拒绝）。
#
# 但**登录时按需请求**，分两档 —— 参考 ceve-industry.cn 的做法（它的「授权人物」
# 与「授权人物+军团」是两个不同长度的 scope 列表）：
#   character    只拿个人数据，权限最小，能用的功能也够日常用
#   corporation  额外拿军团数据（军团工业项目 / 蓝图 / 资产 / 建筑 / 部门）
# 这样「只想算算自己蓝图」的人不必把军团的读取权限也交出来。
SCOPES_CHARACTER = [
    # 工业项目（个人）
    "esi-industry.read_character_jobs.v1",
    # 蓝图（个人）
    "esi-characters.read_blueprints.v1",
    # 库存（个人）
    "esi-assets.read_assets.v1",
    # 解析玩家建筑名（/universe/structures/{id}/ 需要它，否则只能显示「设施 1029…」）
    "esi-universe.read_structures.v1",
    # 判断该角色有没有军团职位（决定军团功能能不能用，以及给出可读的提示）
    "esi-characters.read_corporation_roles.v1",
    # 技能（工业时间减免）与当前所在位置
    "esi-skills.read_skills.v1",
    "esi-location.read_location.v1",
    # 个人市场订单
    "esi-markets.read_character_orders.v1",
]

SCOPES_CORPORATION = [
    "esi-industry.read_corporation_jobs.v1",     # 需要 总监 / 工厂经理 角色
    "esi-corporations.read_blueprints.v1",       # 需要 总监 / 工厂经理 角色
    "esi-assets.read_corporation_assets.v1",     # 需要 总监 / 其他 角色
    "esi-corporations.read_structures.v1",       # 军团建筑清单
    "esi-corporations.read_divisions.v1",
]

SCOPE_TIERS = {
    "character": list(SCOPES_CHARACTER),
    "corporation": list(SCOPES_CHARACTER) + list(SCOPES_CORPORATION),
}
SCOPE_TIER_LABELS = {
    "character": "只授权个人数据（最小权限）",
    "corporation": "授权个人 + 军团数据（推荐）",
}

# 并集：注册应用时要勾的就是这一组
ALL_SCOPES = list(SCOPE_TIERS["corporation"])


def scopes_for_tier(tier: str):
    """取某一档的 scope 列表；未知档位回退到完整档。"""
    return list(SCOPE_TIERS.get((tier or "").strip().lower(),
                                SCOPE_TIERS["corporation"]))


# --------------------------------------------------------------------------
# scope 审计：这些权限能「代替你做事」，本程序一个都用不到
# --------------------------------------------------------------------------
# 参考 natemsz/evesynapse：它从 ESI 的 securitySchemes 里剔除所有
# write_ / send_ / respond_ / organize_ / manage_ / open_window 的 scope。
# 而同类工具里也有一次要 60 项权限的（见 docs/令牌与登录安全.md 的对比），
# 其中包含：
#     esi-mail.send_mail.v1        以你的名义发邮件
#     esi-ui.open_window.v1        在你客户端里打开窗口
#     esi-ui.write_waypoint.v1     往你的自动导航里写路径点
#     esi-fleets.write_fleet.v1    改组你的舰队
#     esi-planets.manage_planets.v1 改你的行星工厂
# config.json 里可以自定义 scopes，所以这里做一次审计：
# 一旦请求了这类权限就明确告警，而不是安静地替用户交出去。
DANGEROUS_SCOPE_MARKERS = ("send_", "write_", "respond_", "organize_",
                           "manage_", "open_window")


def audit_scopes(scopes) -> list:
    """挑出「能代替用户做事」的 scope。本程序的默认集合应当永远是空的。"""
    risky = []
    for scope in scopes or []:
        name = str(scope)
        if any(marker in name for marker in DANGEROUS_SCOPE_MARKERS):
            risky.append(name)
    return sorted(set(risky))


def scope_audit_report() -> dict:
    """并集与两档各自的审计结果（给自检与界面用）。"""
    return {
        "markers": list(DANGEROUS_SCOPE_MARKERS),
        "all_scopes_risky": audit_scopes(ALL_SCOPES),
        "character_risky": audit_scopes(SCOPES_CHARACTER),
        "corporation_risky": audit_scopes(SCOPES_CORPORATION),
        "clean": not audit_scopes(ALL_SCOPES),
        "counts": {tier: len(names) for tier, names in SCOPE_TIERS.items()},
    }

# 任务活动名 -> 中文名（与 SDE blueprints.jsonl 的 activities 键一致）
ACTIVITIES = {
    "manufacturing": "制造",
    "reaction": "反应",
    "copying": "拷贝",
    "invention": "发明",
    "research_material": "材料效率研究",
    "research_time": "时间效率研究",
}

# ESI /industry/systems/ 的 cost_indices 活动名 -> 中文名
COST_INDEX_ACTIVITIES = {
    "manufacturing": "制造",
    "reaction": "反应",
    "copying": "拷贝",
    "invention": "发明",
    "researching_material_efficiency": "材料效率研究",
    "researching_time_efficiency": "时间效率研究",
    "reverse_engineering": "逆向工程",
}

# SDE 里的工业建筑加成属性（industryModifierSources.jsonl 引用）
BONUS_ATTRIBUTES = {
    2600: ("material", "strEngMatBonus", "材料消耗倍率"),
    2601: ("cost", "strEngCostBonus", "任务费用倍率"),
    2602: ("time", "strEngTimeBonus", "任务时间倍率"),
    2721: ("time", "strReactionTimeMultiplier", "反应时间倍率"),
}

# 改装件的减免属性（数值为百分点，如 -20 表示减免 20%）
# 已对照官方 SDE 与实测数据核验：
#   材料效率 I / II  -> 2594 = -2.0 / -2.4
#   时间效率 I / II  -> 2593 = -20 / -24
#   费用（实验室优化）-> 2595 = -10 / ...
RIG_BONUS_ATTRIBUTES = {
    2593: ("time", "attributeEngRigTimeBonus", "时间减免"),
    2594: ("material", "attributeEngRigMatBonus", "材料减免"),
    2595: ("cost", "attributeEngRigCostBonus", "费用减免"),
    # 反应改装件：**实际物品带的是这两个**，而不是 industryModifierSources 里声明的
    # 2715-2720（那几个属性在物品上一个值都没有）。已逐个对照 SDE 的 typeDogma 核实：
    #   46484 屹立中型复合反应堆时间效率 I  → 2713 = -20
    #   46486 屹立中型复合反应堆材料效率 I  → 2714 = -2
    # 漏掉它们的后果：反应改装件的加成被完全忽略（反应时间/材料偏大）。
    2713: ("time", "RefRigTimeBonus", "反应时间减免"),
    2714: ("material", "RefRigMatBonus", "反应材料减免"),
}

# 安等倍率属性：高安 1.0 / 低安 1.9 / 00 与虫洞 2.1
SECURITY_MULTIPLIERS = {
    2355: ("highsec", 1.0, "高安倍率"),
    2356: ("lowsec", 1.9, "低安倍率"),
    2357: ("nullsec", 2.1, "00/虫洞倍率"),
}

# 蓝图的研究等级（industryBlueprintRank，用于展示与一致性提示）
BLUEPRINT_RANK_ATTRIBUTE = 1955

# 角色关联的健康状态。只有 ok（或旧行的空值）允许同步；
# owner_changed 表示「这个角色换了主人」，token_dead 表示「EVE 已拒绝这组令牌」，
# 两者都必须等用户重新登录确认，绝不静默继续用。
LINK_OK = "ok"
LINK_OWNER_CHANGED = "owner_changed"
LINK_TOKEN_DEAD = "token_dead"
LINK_LABELS = {
    LINK_OK: "正常",
    LINK_OWNER_CHANGED: "角色已转手，需确认",
    LINK_TOKEN_DEAD: "登录已失效，需重新登录",
    "": "正常",
}

# --------------------------------------------------------------------------
# 建筑与改装件的尺寸匹配（属性 1547 rigSize）
# --------------------------------------------------------------------------
# **建筑和改装件都带 1547 = rigSize**，两者相等才能安装。这就是
# 「空堡只能装中型改装件、铁壁只能装大型、星城只能装超大型」的实现依据。
# 已逐个对照 SDE 的 typeDogma 核实：
#   空堡 35832 = 2   铁壁 35833 = 3   星城 35834 = 4
#   莱塔卢 35825 = 2 阿兹贝尔 35826 = 3 索迪约 35827 = 4
#   阿塔诺 35835 = 2 塔塔拉 35836 = 3
#   中型改装件 37162 = 2  大型 43714 = 3  反应中型 46484 = 2
RIG_SIZE_ATTRIBUTE = 1547
RIG_SIZE_LABELS = {1: "小型", 2: "中型", 3: "大型", 4: "超大型"}

# 改装件「能装在哪些建筑组」：canFitShipGroup01/02/03 = 1298/1299/1300
# 例：37162（建筑工程中型）→ 1657 堡垒 / 1404 工程复合体 / 1406 精炼厂
#     46484（复合反应堆中型）→ 只有 1406 精炼厂（反应堆只建在精炼厂）
RIG_FIT_GROUP_ATTRIBUTES = (1298, 1299, 1300)

# 建筑自身的改装件槽位（1137 rigSlots）与校准值上限（1132 upgradeCapacity），
# 改装件消耗的校准值（1153 upgradeCost）。用于界面提示与「同组只能装一个」校验。
STRUCTURE_RIG_SLOTS_ATTRIBUTE = 1137
STRUCTURE_UPGRADE_CAPACITY_ATTRIBUTE = 1132
RIG_UPGRADE_COST_ATTRIBUTE = 1153

# 玩家可部署的工业建筑分组（SDE groups.jsonl 里的分组名）
#   1657 堡垒        —— 空堡 / 铁壁 / 星城 / 昇威豪华星城 / 5 个势力铁壁
#   1404 工程复合体  —— 莱塔卢 / 阿兹贝尔 / 索迪约
#   1406 精炼厂      —— 阿塔诺 / 塔塔拉
INDUSTRY_STRUCTURE_GROUPS = {1657: "堡垒", 1404: "工程复合体", 1406: "精炼厂"}
# 建筑的改装件槽位上限（EVE 里所有可装改装件的建筑都是 3 个）
STRUCTURE_RIG_SLOT_LIMIT = 3

# --------------------------------------------------------------------------
# 建筑燃料消耗（服务装备的燃料 + 建筑的燃料减免）
# --------------------------------------------------------------------------
# 服务装备（Service Module）身上的燃料属性：
#   2108 serviceModuleFuelConsumptionGroup  消耗哪种燃料 —— 值是**分组 ID**
#                                           1136 = 燃料块组（四种势力燃料块与通用燃料块同组）
#   2109 serviceModuleFuelAmount            「每小时开始时消耗的数量」← 就是每小时消耗
#   2110 serviceModuleFuelOnlineAmount      上线时一次性消耗
# 建筑自身：
#   2339 structureServiceRoleBonus          负值百分比（−25 = 燃料消耗少 25%）
#   2056 serviceSlots                       可同时在线的服务装备数量上限
#
# 核对记录（与游戏内建筑详情文本对照）：空堡/铁壁/星城/莱塔卢/阿兹贝尔/索迪约/
# 塔塔拉/德拉库斯/地平线/普罗米修斯 = 2339 的 −25 ↔ 文本「25% 服务装备的燃料消耗降低」；
# 阿塔诺 −20 ↔ 「20%」；马金尼斯 −50 ↔ 「50%」。
# ⚠️ 唯一不一致：「莫罗」铁壁的属性是 **−35**，而游戏内文本写的是 15%。
#    我们按**属性**计算（属性才是真正生效的数值，描述文本可能过期），并在界面上标注这一点。
FUEL_BLOCK_GROUP_ID = 1136
SERVICE_FUEL_GROUP_ATTRIBUTE = 2108
SERVICE_FUEL_HOURLY_ATTRIBUTE = 2109
SERVICE_FUEL_ONLINE_ATTRIBUTE = 2110
STRUCTURE_FUEL_BONUS_ATTRIBUTE = 2339
STRUCTURE_SERVICE_SLOTS_ATTRIBUTE = 2056
# 燃料块类型：**只有这 4 种是已发布（published=1）的市场物品**，建筑用其中一种。
# （36945「通用燃料块」是 published=0 的内部物品，不能作为燃料来源 —— 早期版本
#   把它当成默认值，导致取不到价格，已修正。）
# 依据：CCP 官方支持文章《建筑管理》：「建筑现可使用以下 4 种燃料块中的一种：
#       氦燃料块 / 氧燃料块 / 氮燃料块 / 氢燃料块」
FUEL_BLOCK_TYPE_IDS = (4051, 4246, 4247, 4312)     # 氮 / 氢 / 氦 / 氧
DEFAULT_FUEL_BLOCK_TYPE_ID = 4051                   # 氮燃料块

# 「服务装备周期燃料需求」的单位 → 燃料块 的换算系数。
#
# 取 1 的依据（可自行核实）：
#   1. SDE 的属性 2110（上线一次性消耗）几乎恒等于 2109（每小时）× 72，
#      而 CCP 官方文章明确说「服务首次启用时需要消耗 **3 天的燃料**」，
#      72 小时 = 3 天 —— 说明 2109/2110 与「燃料块」是同一计量单位。
#   2. 若系数是 24，则「莱塔卢 + 1 个制造厂」每小时要烧 288 块 =
#      约 5,000 万 ISK/天，比莱塔卢自身造价还高，经济上不可能成立。
# 所以 24 极可能是「一天 24 小时」被误当成「1 单位 = 24 块」。
# 这里做成可配置，是为了让任何人都能一键改成自己验证出的值。
FUEL_BLOCKS_PER_UNIT = 1

# --------------------------------------------------------------------------
# 燃料减免的**作用域**：建筑只给「某几类」服务装备打折
# --------------------------------------------------------------------------
# 游戏里的服务装备树是按**市场分组**分的（不是 SDE 的 group）：
#   2232 堡垒服务装备     克隆中心 / 市场中心
#   2332 工程服务装备     制造厂 / 研究实验室 / 发明实验室 / 三岛研究实验室 /
#                         旗舰级船坞 / 超级旗舰级船坞 / 诱导力场发生器 /
#                         诱导系统干扰器 / 通道发生器
#   2233 资源提炼服务装备 提炼设施 / 复合・混合・生化反应堆 / 卫星钻探
# 注意「诱导力场发生器」的 SDE group 是「快速部署服务装备」，但市场分组是
# **工程服务装备** —— 与游戏内显示一致，所以分类必须按 market_group_id 走。
SERVICE_CATEGORY_GROUPS = {
    2232: "堡垒服务装备",
    2332: "工程服务装备",
    2233: "资源提炼服务装备",
}

# 每种建筑，它的燃料减免**对哪些服务分类生效**。
# ⚠️ CCP 没有把这个作用域做成 dogma 属性（`2339 structureServiceRoleBonus` 只是个
# 百分比，作用范围只写在角色加成的文本里），所以这里**按游戏内文本逐条录入**
# （来源：用户在游戏里打开建筑/服务装备详情看到的「特有加成」，已逐条核对）。
#   空堡/铁壁/星城/昇威豪华星城  「25% 堡垒服务装备的燃料消耗降低」
#   莱塔卢/阿兹贝尔/索迪约        「25% 工程服务装备的燃料消耗降低」
#   阿塔诺                        「20% 提炼和反应服务装备的燃料消耗降低」
#   塔塔拉                        「25% 提炼和反应服务装备的燃料消耗降低」
#   德拉库斯 / 地平线              「25% 堡垒和工程服务装备的燃料消耗降低」
#   普罗米修斯                    「25% 堡垒和提炼服务装备的燃料消耗降低」
#   莫罗 / 马金尼斯               「所有服务装备的燃料消耗降低」
STRUCTURE_FUEL_SCOPE = {
    35832: (2232,),                  # 空堡
    35833: (2232,),                  # 铁壁
    35834: (2232,),                  # 星城
    40340: (2232,),                  # 昇威豪华星城
    35825: (2332,),                  # 莱塔卢
    35826: (2332,),                  # 阿兹贝尔
    35827: (2332,),                  # 索迪约
    35835: (2233,),                  # 阿塔诺
    35836: (2233,),                  # 塔塔拉
    47513: (2232, 2332),             # "德拉库斯"铁壁
    47514: (2232, 2332),             # "地平线"铁壁
    47516: (2232, 2233),             # "普罗米修斯"铁壁
    47512: (2232, 2332, 2233),       # "莫罗"铁壁：所有
    47515: (2232, 2332, 2233),       # "马金尼斯"铁壁：所有
}

# 表里没收录的建筑按所属分组兜底（堡垒 / 工程复合体 / 精炼厂）
STRUCTURE_GROUP_FUEL_SCOPE = {1657: (2232,), 1404: (2332,), 1406: (2233,)}


def fuel_scope_for(store, structure_type_id) -> tuple:
    """取某建筑的燃料减免作用域（服务分类的市场分组 ID 集合）。"""
    if not structure_type_id:
        return ()
    sid = int(structure_type_id)
    if sid in STRUCTURE_FUEL_SCOPE:
        return tuple(STRUCTURE_FUEL_SCOPE[sid])
    row = store.q1("SELECT group_id FROM types WHERE type_id=?", (sid,))
    group_id = (row or {}) and row["group_id"]
    return tuple(STRUCTURE_GROUP_FUEL_SCOPE.get(group_id, ()))

# 与服务装备相关的活动（用于界面推荐「这个活动该开哪些服务」）
#   键：我们的活动名；值：服务装备名称里出现的关键词
ACTIVITY_SERVICE_HINTS = {
    "manufacturing": ("制造厂", "船坞"),
    "research_material": ("材料效率实验室", "研究实验室"),
    "research_time": ("时间效率实验室", "研究实验室"),
    "copying": ("研究实验室", "三岛研究实验室"),
    "invention": ("发明实验室", "三岛研究实验室"),
    "reaction": ("反应堆", "提炼设施"),
}

# ESI /corporations/{id}/structures/ 的 services 只给**英文名**，
# 这里按关键词映射回 SDE 里的服务装备 typeID（映射不到的会原样列出并提示）。
# 值取自 SDE（括号内是每小时燃料块数）。
ESI_SERVICE_KEYWORDS = (
    ("manufacturing", 35878),          # 屹立制造厂 I（12）
    ("invention", 35886),              # 屹立发明实验室 I（12）
    ("time efficiency", 35889),        # 建筑时间效率实验室（5）
    ("material efficiency", 35890),    # 建筑材料效率实验室（10）
    ("research", 35891),               # 屹立研究实验室 I（12）
    ("three island", 45550),           # 屹立三岛研究实验室（10）
    ("reprocessing", 35899),           # 屹立提炼设施 I（10）
    ("composite", 45537),              # 屹立复合反应堆 I（15）
    ("hybrid", 45538),                 # 屹立混合反应堆 I（15）
    ("biochemical", 45539),            # 屹立生化反应堆 I（15）
    ("market", 35892),                 # 屹立市场中心 I（40）
    ("clone", 35894),                  # 屹立克隆中心 I（10）
    ("supercapital", 35877),           # 屹立超级旗舰级船坞 I（36）
    ("shipyard", 35881),               # 屹立旗舰级船坞 I（24）
    ("moon drill", 45009),             # 屹立卫星钻探 I（5）
    ("metenox", 82941),                # 屹立梅特诺克斯卫星钻探（5）
    ("cynosural", 35912),              # 屹立诱导力场发生器 I（15）
    ("jammer", 35914),                 # 屹立诱导系统干扰器（40）
    ("jump gate", 35913),              # 屹立通道发生器 I（30）
)


def match_service_module(name: str):
    """把 ESI 给的英文服务名映射到 SDE 里的服务装备 typeID（匹配不到返回 None）。"""
    low = str(name or "").lower()
    for keyword, type_id in ESI_SERVICE_KEYWORDS:
        if keyword in low:
            return type_id
    return None

# 需要从 typeDogma 里抓取的属性集合
NEEDED_DOGMA_ATTRIBUTES = (set(BONUS_ATTRIBUTES) | set(RIG_BONUS_ATTRIBUTES)
                           | set(SECURITY_MULTIPLIERS) | {2095, 2096}
                           | {BLUEPRINT_RANK_ATTRIBUTE, RIG_SIZE_ATTRIBUTE,
                              STRUCTURE_RIG_SLOTS_ATTRIBUTE,
                              STRUCTURE_UPGRADE_CAPACITY_ATTRIBUTE,
                              RIG_UPGRADE_COST_ATTRIBUTE}
                           | {SERVICE_FUEL_GROUP_ATTRIBUTE, SERVICE_FUEL_HOURLY_ATTRIBUTE,
                              SERVICE_FUEL_ONLINE_ATTRIBUTE, STRUCTURE_FUEL_BONUS_ATTRIBUTE,
                              STRUCTURE_SERVICE_SLOTS_ATTRIBUTE}
                           | set(RIG_FIT_GROUP_ATTRIBUTES))

# 蓝图研究（ME / TE）等级表
# 表内数值是「rank = 1 时，达到第 n 级所需的**累计**秒数」。
# 依据与验证：
#   * 官方 SDE 里 4344 个蓝图的 research_material.time == research_time.time 恒成立，
#     说明 CCP 只存了一个基准（即第 1 级的累计秒数）。
#   * rank=20 的裂谷级蓝图(691) SDE 研究时长 = 2100 = 105 × 20 ✔ 与社区已知值一致。
#   * 研究任务费用公式同时给出「从 n−1 到 n 用差值」与「从 0 到 n 用累计值」两种写法，
#     可见 BaseDuration_n 是累计值 ⇒ 第 n 级本身耗时 = 累计(n) − 累计(n−1)。
#   * ME 与 TE 都是 10 个等级：ME 每级 −1%（显示 0–10），TE 每级 −2%（显示 0–20）。
RESEARCH_CUMULATIVE_SECONDS = [105, 250, 595, 1414, 3360, 8000, 19000, 45255, 107700, 256000]
ME_PERCENT_PER_LEVEL = 1
TE_PERCENT_PER_LEVEL = 2

# 任务费用基数系数：制造与反应直接用 EIV，拷贝/发明用 2%×EIV，研究另算（见 industry.py）
JOB_COST_BASE_FACTOR = {
    "manufacturing": 1.0,
    "reaction": 1.0,
    "copying": 0.02,
    "invention": 0.02,
    "research_material": None,   # 特殊：0.02 × 时间/105
    "research_time": None,
}

# SDE 活动键 -> ESI /industry/systems/ 的 cost_indices 键
ACTIVITY_TO_COST_INDEX = {
    "manufacturing": "manufacturing",
    "reaction": "reaction",
    "copying": "copying",
    "invention": "invention",
    "research_material": "researching_material_efficiency",
    "research_time": "researching_time_efficiency",
}

# SDE 活动键 -> industryModifierSources.jsonl 里的活动名（驼峰）
ACTIVITY_TO_MODIFIER = {
    "manufacturing": "manufacturing",
    "reaction": "reaction",
    "copying": "copying",
    "invention": "invention",
    "research_material": "researchMaterial",
    "research_time": "researchTime",
}

# 影响「任务时间」的技能：(技能 typeID, 每级减免比例, 默认等级, 中文名)
# 技能 ID 与名称已从官方 SDE 的「技能」分类逐一核对。
#
# 减免比例不再靠记忆，而是对着 SDE 里技能自己的**效果属性**确定：
#   3380 Industry          attr 440  manufacturingTimeBonus                    = -4
#   3388 Advanced Industry attr 1961 advancedIndustrySkillIndustryJobTimeBonus  = -3
#        ↑ 描述原文「A bonus to **all industry job times**」→ 适用于**每一个**活动
#   3403 Research          attr 453  blueprintmanufactureTimeBonus             = -5
#   3409 Metallurgy        attr 468  mineralNeedResearchBonus                  = -5
#   45746 Reactions        attr 2660 reactionTimeBonus                        = -4
#   3406 Laboratory Operation      attr 471 laboratorySlotsBonus = +1  ← **只加槽位，不减时间**
#   24624 Advanced Lab Operation   attr 471 laboratorySlotsBonus = +1  ← 同上
#   45748 Reaction Efficiency      attr 2661 reactionSlotBonus    = +1  ← 加槽位
# 也就是说：拷贝与发明**没有任何技能能缩短时间**，只有「高级工业理论」因为
# 「所有工业任务时间」这层含义而适用。实验室运作那两个技能是槽位技能，
# 早期版本曾把它们当成时间减免 —— 那是错的，会让拷贝/发明时间被低估约 25%。
TIME_SKILLS = {
    "manufacturing": [
        (3380, 0.04, 5, "工业理论 Industry"),
        (3388, 0.03, 5, "高级工业理论 Advanced Industry"),
    ],
    "reaction": [
        (45746, 0.04, 5, "反应理论 Reactions"),
        # 高级工业理论：其属性描述是「所有工业任务时间」，因此反应也适用
        (3388, 0.03, 5, "高级工业理论 Advanced Industry"),
    ],
    # 时间效率研究：Research 每级 5%
    "research_time": [
        (3403, 0.05, 5, "研究概论 Research"),
        (3388, 0.03, 5, "高级工业理论 Advanced Industry"),
    ],
    # 材料效率研究：Metallurgy 每级 5%
    "research_material": [
        (3409, 0.05, 5, "冶金学 Metallurgy"),
        (3388, 0.03, 5, "高级工业理论 Advanced Industry"),
    ],
    # 拷贝：只有高级工业理论（「所有工业任务时间」）
    "copying": [
        (3388, 0.03, 5, "高级工业理论 Advanced Industry"),
    ],
    # 发明：同上。科学/加密技能影响的是**成功率**，不是时间。
    "invention": [
        (3388, 0.03, 5, "高级工业理论 Advanced Industry"),
    ],
}

# 与军团工业数据相关的角色（ESI /characters/{id}/roles/ 返回的名字，SDE 里的 shortName）
CORP_ROLES = {
    "Director": "总监",
    "Factory_Manager": "工厂经理",
    "roleDirector": "总监",
    "roleFactoryManager": "工厂经理",
}

# --------------------------------------------------------------------------
# 默认配置
# --------------------------------------------------------------------------

# SCC（商业安全委员会）附加费按活动区分 —— 用户提供的游戏规则：
#   科研（材料/时间效率）= 2%  ·  制造 / 发明 = 4%
# 游戏内实证：研究任务显示「商业安全委员会追加费用 +2.00% PTV」。
# 拷贝(copying) 用户未明确 → 先按科研族 2%（待确认）。
SCC_BY_ACTIVITY = {
    "research_material": 0.02,
    "research_time": 0.02,
    "copying": 0.02,
    "manufacturing": 0.04,
    "invention": 0.04,
}


def scc_default_for(activity: str, fallback: float = 0.04) -> float:
    """该活动的 SCC 附加费率。"""
    return float(SCC_BY_ACTIVITY.get(str(activity or ""), fallback))


DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8420,
    "open_browser": True,

    # EVE 应用凭据（在 https://developers.eveonline.com/ 注册后得到）
    "client_id": "",
    "client_secret": "",            # 公共客户端(PKCE)可留空
    "use_pkce": True,
    "callback_path": "/callback",
    "scopes": list(ALL_SCOPES),
    # 登录时默认请求哪一档：character（最小权限）/ corporation（个人 + 军团）
    "scope_tier": "corporation",

    # ESI 行为
    "user_agent": "eve-industry-local/0.1 (+https://developers.eveonline.com/)",
    "concurrency": 4,
    "request_timeout": 30,
    "max_retries": 3,
    "compatibility_date": COMPATIBILITY_DATE,   # 留空则不发送该头
    "respect_expires": True,        # 遵守 ESI 的 Expires，避免无谓请求
    "cache_ttl_override": {},       # 例如 {"market_orders": 300}

    # 市场
    "region_key": "the_forge",
    "region_id": 10000002,
    "station_id": 60003760,
    "price_source": "jita_sell",    # adjusted / jita_sell / jita_buy / average / custom
    # 「刷新物价」一次最多抓多少个物品的盘口 / 历史（每个物品 1~2 次请求）
    "market_refresh_max_items": 250,
    "market_refresh_scope": "essentials",   # 默认的刷新范围
    "auto_refresh_on_start": False,         # 启动时若物价过旧是否自动刷新

    # 令牌落盘加密：auto / dpapi / aes-gcm / none
    #   auto    Windows 用 DPAPI（零配置、与当前用户绑定），其它平台用 AES-256-GCM
    #   dpapi   强制 DPAPI（仅 Windows）
    #   aes-gcm 强制 AES-256-GCM（密钥来自 EVE_TOKEN_ENCRYPTION_KEY 或 data/token.key）
    #   none    不加密（仅为排障保留，界面会告警）
    "token_encryption": "auto",

    # 工业成本/时间参数（可被界面覆盖）
    # SCC 附加费现行 4%（0.25% → 0.75% → 1.5% → 2024-02-01 起 4%）
    "scc_surcharge": 0.04,
    "job_tax_rate": 0.0025,         # 建筑所有者税率；NPC 空间站固定 0.25%，玩家建筑上限 10%
    "alpha_clone_tax": 0.0,         # Alpha 角色额外 0.25%，Omega 填 0
    "structure_material_bonus": 0.0,  # 手动覆盖：材料减免（0.02 = 减免 2%）
    "structure_time_bonus": 0.0,
    "structure_cost_bonus": 0.0,
    "industry_skill_level": 5,      # Industry 技能（每级 -4% 时间）
    "advanced_industry_level": 5,   # Advanced Industry（每级 -3% 时间）
    "reaction_skill_level": 5,      # Reactions 技能（每级 -4% 时间）
    # 材料取整：CCP 实际行为是「乘完流程数后向上取整」，
    # 参考站点用的是截断，两者在个别材料上会差 1 个，可在这里切换。
    "material_rounding": "ceil",    # ceil / floor / round
    "include_job_cost": True,
    "include_job_tax": True,
    "security_modifier": None,      # None = 按生产星系安等自动判定；也可强制 highsec/lowsec/nullsec

    # 采购价来源
    "custom_prices": {},            # {"34": 5.5}  按 type_id 指定单价

    # SDE
    "sde_url": SDE_ZIP_URL,
    "sde_auto_update_hours": 24,
    "sde_languages": ["zh", "en"],
}

_SECRET_KEYS = {"client_id", "client_secret"}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    ensure_dirs()
    raw = utils.read_json(CONFIG_PATH, {}) or {}
    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    # region_id 跟随 region_key，避免两者不一致
    region = REGIONS.get(cfg.get("region_key") or "")
    if region:
        cfg["region_id"] = region["id"]
        if region.get("station_id"):
            cfg["station_id"] = region["station_id"]
    return cfg


DEFAULT_PORT = 8420
# 运行时端口（用 --port 临时换端口时放在这里，**不落盘**）
RUNTIME_PORT_KEY = "_runtime_port"

# --------------------------------------------------------------------------
# 内置的 EVE 应用 Client ID
# --------------------------------------------------------------------------
# 这样**普通用户完全不用自己注册应用**：下载 → 双击 → 直接登录。
#
# 几点说明（都写在这里，免得以后再纠结）：
#   * Client ID **不是机密** —— 它本来就会出现在 SSO 授权地址里，任何被授权的
#     用户都能看到；它只用来标识「是哪个第三方应用」。
#   * 因此它**能在源代码/发布包里被看到**，这是必然的（程序要用它），
#     但**界面上不显示、配置里也不存**（resolve_client_id 直接用内置值）。
#   * 所有用户共用这一个 Client ID 是正常做法：令牌仍然按角色签发，
#     互相看不到对方的数据；回调地址对每个人都是本机
#     http://localhost:8420/callback，所以也不冲突。
#   * 想用自己的应用（例如军团内部自建、或不想走这个共享应用）：
#     在「设置」里填自己的 Client ID 覆盖即可，会存进本地 config.json。
#
# 【维护者备注】万一需要**重新注册**这个内置应用（例如被 CCP 封禁、或要换 owner），
# 需要的材料都在代码里，不必另找文档：
#   * Callback URL：http://localhost:8420/callback
#     （必须正好是默认端口 DEFAULT_PORT + /callback；改了端口就登不上，
#      界面在端口非默认时会主动警示）
#   * Scopes：见本文件的 ALL_SCOPES（13 项），分档见 SCOPE_TIERS
#   * 注册后用 `python tools/verify_scopes.py` 对着 ESI 实时规范核对一遍
#   * ⚠ 开发者门户里那个 `esi-structures` 分类**不用勾**（read_character /
#     read_corporation 当前没有任何 ESI 接口需要）；我们真正要的是
#     `esi-corporations.read_structures.v1`（读军团建筑）与
#     `esi-universe.read_structures.v1`（解析建筑名）
#   * 新应用的 Client ID 填回下面这个常量即可
BUILTIN_CLIENT_ID = "974ee42083934a099d9d11bc720a24cb"


def resolve_client_id(cfg: dict) -> str:
    """实际使用的 Client ID：配置里的优先，否则用内置的。"""
    value = str((cfg or {}).get("client_id") or "").strip()
    if value and value != "***":          # "***" 是接口回显的掩码，不是真值
        return value
    return BUILTIN_CLIENT_ID


def client_id_source(cfg: dict) -> str:
    """Client ID 来自哪里：custom（用户自己填的）/ builtin（内置）。"""
    value = str((cfg or {}).get("client_id") or "").strip()
    if value and value != "***" and value != BUILTIN_CLIENT_ID:
        return "custom"
    return "builtin"


def save_config(cfg: dict) -> None:
    """把配置落盘。

    **以 `_` 开头的键不落盘** —— 那些是「只在本次运行有效的运行时状态」
    （例如 `_runtime_port`：用 `--port` 临时换端口时，绝不能把它永久写进配置，
    否则下次正常启动就会用错端口，SSO 回调与门户登记的地址不一致，
    授权完成后浏览器回不到本机服务）。
    """
    utils.write_json(CONFIG_PATH, {k: v for k, v in cfg.items()
                                   if not str(k).startswith("_")})


def runtime_port(cfg: dict) -> int:
    """本次运行实际使用的端口：优先 `--port` 指定的运行时端口，其次配置里的。"""
    try:
        return int(cfg.get(RUNTIME_PORT_KEY) or cfg.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def public_config(cfg: dict) -> dict:
    """给前端用的配置（隐去密钥）。"""
    out = {}
    for key, value in cfg.items():
        if key in _SECRET_KEYS:
            out[key] = "***" if value else ""
            out["has_" + key] = bool(value)
        else:
            out[key] = value
    out["regions"] = REGIONS
    out["activities"] = ACTIVITIES
    out["all_scopes"] = ALL_SCOPES
    out["scope_tiers"] = {key: {"label": SCOPE_TIER_LABELS.get(key, key),
                               "scopes": value, "count": len(value)}
                          for key, value in SCOPE_TIERS.items()}
    out["default_scope_tier"] = cfg.get("scope_tier") or "corporation"
    out["scope_audit"] = scope_audit_report()
    return out


def ensure_dirs() -> None:
    for path in (DATA_DIR, SDE_DIR, CACHE_DIR):
        utils.ensure_dir(path)


def callback_url(cfg: dict, host: str = "localhost") -> str:
    port = runtime_port(cfg)
    path = cfg.get("callback_path", "/callback")
    return "http://%s:%d%s" % (host, int(port), path)
