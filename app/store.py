"""SQLite 数据层。

线程安全策略：每个线程一个连接 + WAL 模式，读并发、写串行。
所有表都在首次启动时自动建立。

存储内容：
  * 角色令牌（刷新令牌经 DPAPI 加密）
  * ESI 响应缓存（ETag / Expires）
  * SDE 静态数据（物品类型、蓝图活动、建筑加成属性、星系名）
  * 市场（调整价、盘口快照、历史）
  * 系统成本指数、工业设施
  * 角色/军团库存与蓝图
  * 工业项目快照、用户方案
"""

from __future__ import annotations

import os
import sqlite3
import threading

from . import config as cfgmod
from . import secure, utils

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS characters (
    character_id     INTEGER PRIMARY KEY,
    name             TEXT,
    corporation_id   INTEGER,
    corporation_name TEXT,
    alliance_id      INTEGER,
    scopes           TEXT,
    owner_hash       TEXT,
    refresh_token    BLOB,
    access_token     TEXT,
    access_expires   INTEGER DEFAULT 0,
    token_type       TEXT,
    encrypted        INTEGER DEFAULT 0,
    added_at         INTEGER,
    last_used        INTEGER,
    is_active        INTEGER DEFAULT 0,
    note             TEXT,
    -- 关联健康状态：ok / owner_changed / token_dead（见 config.LINK_*）
    -- 只有 ok（或空，兼容旧行）才允许同步；其它状态等用户重新登录确认。
    link_state       TEXT DEFAULT '',
    link_state_at    INTEGER
);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key  TEXT PRIMARY KEY,
    url        TEXT,
    etag       TEXT,
    expires    INTEGER DEFAULT 0,
    fetched_at INTEGER,
    status     INTEGER,
    body       TEXT
);

-- ---------------- SDE ----------------
CREATE TABLE IF NOT EXISTS types (
    type_id         INTEGER PRIMARY KEY,
    name            TEXT,
    name_en         TEXT,
    group_id        INTEGER,
    category_id     INTEGER,
    market_group_id INTEGER,
    meta_group_id   INTEGER,
    tech_level      INTEGER,
    volume          REAL DEFAULT 0,
    packaged_volume REAL DEFAULT 0,
    portion_size    INTEGER DEFAULT 1,
    base_price      REAL DEFAULT 0,
    published       INTEGER DEFAULT 0,
    icon_id         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_types_name ON types(name);
CREATE INDEX IF NOT EXISTS idx_types_group ON types(group_id);
CREATE INDEX IF NOT EXISTS idx_types_market_group ON types(market_group_id);

CREATE TABLE IF NOT EXISTS groups (
    group_id    INTEGER PRIMARY KEY,
    name        TEXT,
    name_en     TEXT,
    category_id INTEGER,
    published   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    category_id INTEGER PRIMARY KEY,
    name        TEXT,
    name_en     TEXT,
    published   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS market_groups (
    market_group_id INTEGER PRIMARY KEY,
    name            TEXT,
    name_en         TEXT,
    parent_id       INTEGER,
    has_types       INTEGER DEFAULT 0,
    icon_id         INTEGER
);

CREATE TABLE IF NOT EXISTS meta_groups (
    meta_group_id INTEGER PRIMARY KEY,
    name          TEXT,
    name_en       TEXT
);

CREATE TABLE IF NOT EXISTS blueprint_activities (
    blueprint_type_id     INTEGER,
    activity              TEXT,
    time                  INTEGER DEFAULT 0,
    max_production_limit  INTEGER DEFAULT 1,
    PRIMARY KEY (blueprint_type_id, activity)
);

CREATE TABLE IF NOT EXISTS activity_materials (
    blueprint_type_id INTEGER,
    activity          TEXT,
    material_type_id  INTEGER,
    quantity          INTEGER,
    PRIMARY KEY (blueprint_type_id, activity, material_type_id)
);
CREATE INDEX IF NOT EXISTS idx_mat_material ON activity_materials(material_type_id);

CREATE TABLE IF NOT EXISTS activity_products (
    blueprint_type_id INTEGER,
    activity          TEXT,
    product_type_id   INTEGER,
    quantity          INTEGER,
    probability       REAL,
    PRIMARY KEY (blueprint_type_id, activity, product_type_id)
);
CREATE INDEX IF NOT EXISTS idx_prod_product ON activity_products(product_type_id);

CREATE TABLE IF NOT EXISTS activity_skills (
    blueprint_type_id INTEGER,
    activity          TEXT,
    skill_type_id     INTEGER,
    level             INTEGER,
    PRIMARY KEY (blueprint_type_id, activity, skill_type_id)
);

-- 精炼产出（typeMaterials.jsonl），用于「可用矿石替代」提示
CREATE TABLE IF NOT EXISTS type_materials (
    type_id          INTEGER,
    material_type_id INTEGER,
    quantity         INTEGER,
    PRIMARY KEY (type_id, material_type_id)
);

-- 工业加成属性（strEngMatBonus 等），来自 typeDogma.jsonl
CREATE TABLE IF NOT EXISTS type_bonus_attributes (
    type_id      INTEGER,
    attribute_id INTEGER,
    value        REAL,
    PRIMARY KEY (type_id, attribute_id)
);
CREATE INDEX IF NOT EXISTS idx_bonus_attr ON type_bonus_attributes(attribute_id);

-- 加成属性的名字（dogmaAttributes.jsonl），用于界面展示
CREATE TABLE IF NOT EXISTS dogma_attributes (
    attribute_id INTEGER PRIMARY KEY,
    name         TEXT,
    display_name TEXT,
    unit_id      INTEGER
);

-- 结构/改装件的工业加成来源（industryModifierSources.jsonl）
-- filter_id 指向 target_filters：说明这条加成作用在哪一类物品上
CREATE TABLE IF NOT EXISTS industry_modifier_sources (
    type_id      INTEGER,
    activity     TEXT,
    kind         TEXT,   -- cost / time / material
    attribute_id INTEGER,
    filter_id    INTEGER,
    PRIMARY KEY (type_id, activity, kind, attribute_id, filter_id)
);

-- 加成作用目标（industryTargetFilters.jsonl）
CREATE TABLE IF NOT EXISTS target_filters (
    filter_id    INTEGER PRIMARY KEY,
    name         TEXT,
    category_ids TEXT,   -- JSON 数组
    group_ids    TEXT,
    type_ids     TEXT
);

CREATE TABLE IF NOT EXISTS solar_systems (
    system_id       INTEGER PRIMARY KEY,
    name            TEXT,
    name_en         TEXT,
    security_status REAL DEFAULT 0,
    region_id       INTEGER,
    constellation_id INTEGER
);

CREATE TABLE IF NOT EXISTS regions (
    region_id INTEGER PRIMARY KEY,
    name      TEXT,
    name_en   TEXT
);

-- ---------------- 市场 ----------------
CREATE TABLE IF NOT EXISTS prices (
    type_id        INTEGER PRIMARY KEY,
    adjusted_price REAL,
    average_price  REAL,
    updated_at     INTEGER
);

CREATE TABLE IF NOT EXISTS market_stats (
    type_id     INTEGER,
    region_id   INTEGER,
    buy         REAL,
    sell        REAL,
    buy_volume  INTEGER,
    sell_volume INTEGER,
    updated_at  INTEGER,
    source      TEXT,
    PRIMARY KEY (type_id, region_id)
);

CREATE TABLE IF NOT EXISTS market_history (
    type_id     INTEGER,
    region_id   INTEGER,
    date        TEXT,
    highest     REAL,
    lowest      REAL,
    average     REAL,
    volume      INTEGER,
    order_count INTEGER,
    PRIMARY KEY (type_id, region_id, date)
);

-- ---------------- 工业环境 ----------------
CREATE TABLE IF NOT EXISTS cost_indices (
    system_id  INTEGER,
    activity   TEXT,
    cost_index REAL,
    PRIMARY KEY (system_id, activity)
);

CREATE TABLE IF NOT EXISTS industry_facilities (
    facility_id     INTEGER PRIMARY KEY,
    type_id         INTEGER,
    solar_system_id INTEGER,
    region_id       INTEGER
);

CREATE TABLE IF NOT EXISTS stations (
    station_id      INTEGER PRIMARY KEY,
    name            TEXT,
    solar_system_id INTEGER,
    type_id         INTEGER
);

CREATE TABLE IF NOT EXISTS structures (
    structure_id    INTEGER PRIMARY KEY,
    name            TEXT,
    solar_system_id INTEGER,
    type_id         INTEGER,
    owner_id        INTEGER,
    fetched_at      INTEGER
);

-- 军团建筑（来自 ESI /corporations/{id}/structures/）：
-- services 是 JSON 数组 [{name, state}]，fuel_expires 是燃料耗尽的 UTC 时间。
-- 用途：读到你**实际**开了哪些服务装备，据此算燃料消耗（而不是靠猜）。
CREATE TABLE IF NOT EXISTS corp_structures (
    structure_id    INTEGER PRIMARY KEY,
    corporation_id  INTEGER,
    name            TEXT,
    type_id         INTEGER,
    solar_system_id INTEGER,
    services        TEXT,
    fuel_expires    TEXT,
    state           TEXT,
    state_timer_end TEXT,
    unanchors_at    TEXT,
    fetched_at      INTEGER
);

-- ---------------- 角色/军团数据 ----------------
CREATE TABLE IF NOT EXISTS assets (
    scope           TEXT,
    item_id         INTEGER,
    type_id         INTEGER,
    quantity        INTEGER,
    location_id     INTEGER,
    location_flag   TEXT,
    location_type   TEXT,
    is_singleton    INTEGER DEFAULT 0,
    fetched_at      INTEGER,
    PRIMARY KEY (scope, item_id)
);
CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(scope, type_id);

CREATE TABLE IF NOT EXISTS blueprints (
    scope               TEXT,
    item_id             INTEGER,
    type_id             INTEGER,
    material_efficiency INTEGER DEFAULT 0,
    time_efficiency     INTEGER DEFAULT 0,
    runs                INTEGER DEFAULT -1,
    quantity            INTEGER DEFAULT -1,
    location_id         INTEGER,
    location_flag       TEXT,
    fetched_at          INTEGER,
    PRIMARY KEY (scope, item_id)
);
CREATE INDEX IF NOT EXISTS idx_blueprints_type ON blueprints(scope, type_id);

CREATE TABLE IF NOT EXISTS industry_jobs (
    scope                  TEXT,
    job_id                 INTEGER,
    installer_id           INTEGER,
    facility_id            INTEGER,
    station_id             INTEGER,
    activity_id            INTEGER,
    activity               TEXT,
    blueprint_id           INTEGER,
    blueprint_type_id      INTEGER,
    blueprint_location_id  INTEGER,
    product_type_id        INTEGER,
    runs                   INTEGER,
    licensed_runs          INTEGER,
    successful_runs        INTEGER,
    probability            REAL,
    status                 TEXT,
    duration               INTEGER,
    cost                   REAL,
    start_date             TEXT,
    end_date               TEXT,
    completed_date         TEXT,
    completed_character_id INTEGER,
    fetched_at             INTEGER,
    PRIMARY KEY (scope, job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON industry_jobs(scope, status);

CREATE TABLE IF NOT EXISTS plans (
    plan_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE,
    created_at INTEGER,
    updated_at INTEGER,
    data       TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

# ESI /industry/activities 的 activity_id -> SDE 活动键
ACTIVITY_ID_TO_KEY = {
    1: "manufacturing",
    3: "research_time",
    4: "research_material",
    5: "copying",
    7: "reverse_engineering",
    8: "invention",
    9: "reaction",
}


class Store:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        utils.ensure_dir(os.path.dirname(self.path))
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self.integrity = (True, "ok")
        self.init_error = None
        try:
            with self.conn() as c:
                c.executescript(SCHEMA)
                self._migrate(c)
                c.commit()
        except Exception as exc:
            # 数据库文件**完全不可用**（例如「file is not a database」）。
            # 这时不能直接崩：把它挪到一边、建一个新的，并记下来告诉用户
            # （SDE / 物价都是缓存，能重新导入；令牌会丢，需要重新登录）。
            self.init_error = str(exc)
            utils.log("⚠ 数据库无法打开（%s），已把旧文件移到一边并重建一个新库", exc)
            try:
                moved = self.rebuild()
                utils.log("   旧文件已保留为：%s", "、".join(moved.get("moved") or []) or "（无）")
            except Exception as exc2:
                utils.log("✗ 重建数据库也失败了：%s", exc2)
                raise
        # 部分页损坏时文件本身能打开，只有个别读取会报 malformed —— quick_check 能查出来
        self.integrity = self.quick_check()
        if not self.integrity[0]:
            utils.log("⚠ 数据库自检未通过：%s", self.integrity[1])

    # ------------------------------------------------------------------
    @staticmethod
    def _migrate(conn) -> None:
        """给旧库补上后来新增的列（本地工具会不断迭代，做最轻量的迁移）。"""
        additions = {
            "industry_modifier_sources": [("filter_id", "INTEGER")],
            "characters": [("link_state", "TEXT DEFAULT ''"), ("link_state_at", "INTEGER")],
            "index_notes": [],
        }
        for table, columns in additions.items():
            if not columns:
                continue
            try:
                existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
            except Exception:
                continue
            if not existing:
                continue
            for name, decl in columns:
                if name not in existing:
                    try:
                        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def q(self, sql: str, params=()):
        return self.conn().execute(sql, params).fetchall()

    def q1(self, sql: str, params=()):
        return self.conn().execute(sql, params).fetchone()

    def x(self, sql: str, params=()) -> int:
        c = self.conn()
        cur = c.execute(sql, params)
        c.commit()
        return cur.rowcount

    def xmany(self, sql: str, rows) -> int:
        c = self.conn()
        cur = c.executemany(sql, rows)
        c.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # kv / meta
    # ------------------------------------------------------------------
    def kv_get(self, key, default=None):
        row = self.q1("SELECT v FROM kv WHERE k=?", (key,))
        return default if row is None else utils.jloads(row["v"], default)

    def kv_set(self, key, value):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (key, utils.jdumps(value)))

    def meta_get(self, key, default=None):
        row = self.q1("SELECT v FROM meta WHERE k=?", (key,))
        return default if row is None else utils.jloads(row["v"], default)

    def meta_set(self, key, value):
        self.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (key, utils.jdumps(value)))

    # ------------------------------------------------------------------
    # 角色令牌
    # ------------------------------------------------------------------
    def list_characters(self) -> list:
        return [dict(r) for r in self.q("SELECT * FROM characters ORDER BY name")]

    def public_characters(self) -> list:
        out = []
        for row in self.list_characters():
            out.append({
                "character_id": row["character_id"],
                "name": row["name"],
                "corporation_id": row["corporation_id"],
                "corporation_name": row["corporation_name"],
                "alliance_id": row["alliance_id"],
                "scopes": utils.jloads(row["scopes"], []),
                "encrypted": bool(row["encrypted"]),
                "has_refresh_token": bool(row["refresh_token"]),
                "is_active": bool(row["is_active"]),
                "added_at": row["added_at"],
                "last_used": row["last_used"],
                "note": row["note"],
                "link_state": row["link_state"] or "",
                "link_state_label": cfgmod.LINK_LABELS.get(row["link_state"] or "", ""),
                "syncs": self.state_allows_sync(row["link_state"]),
                "owner_hash_present": bool(row["owner_hash"]),
                "token_valid_until": row["access_expires"],
            })
        return out

    def get_character(self, character_id: int):
        row = self.q1("SELECT * FROM characters WHERE character_id=?", (int(character_id),))
        return dict(row) if row else None

    def active_character(self):
        row = self.q1("SELECT * FROM characters WHERE is_active=1 ORDER BY last_used DESC LIMIT 1")
        if row is None:
            row = self.q1("SELECT * FROM characters ORDER BY last_used DESC LIMIT 1")
        return dict(row) if row else None

    def upsert_character(self, data: dict) -> None:
        cols = ["character_id", "name", "corporation_id", "corporation_name", "alliance_id",
                "scopes", "owner_hash", "refresh_token", "access_token", "access_expires",
                "token_type", "encrypted", "added_at", "last_used", "note",
                "link_state", "link_state_at"]
        values = []
        for col in cols:
            v = data.get(col)
            if col == "scopes" and not isinstance(v, (str, type(None))):
                v = utils.jdumps(v)
            values.append(v)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join("%s=excluded.%s" % (c, c) for c in cols if c != "character_id")
        self.x(
            "INSERT INTO characters(%s) VALUES(%s) ON CONFLICT(character_id) DO UPDATE SET %s"
            % (",".join(cols), placeholders, updates),
            values,
        )

    def update_tokens(self, character_id: int, access_token: str, expires_at: int,
                      refresh_token=None, scopes=None, token_type="Bearer") -> None:
        """写入（已加密的）令牌。注意：access 与 refresh 都要作为 BLOB 存入。"""
        sets = ["access_token=?", "access_expires=?", "token_type=?", "last_used=?"]
        params = [secure.seal(access_token.encode(), character_id, "access"),
                  int(expires_at), token_type, utils.now()]
        if refresh_token:
            sets.append("refresh_token=?")
            params.append(secure.seal(refresh_token.encode(), character_id, "refresh"))
            sets.append("encrypted=?")
            params.append(1 if secure.available() else 0)
        if scopes:
            sets.append("scopes=?")
            params.append(utils.jdumps(scopes))
        params.append(int(character_id))
        self.x("UPDATE characters SET %s WHERE character_id=?" % ",".join(sets), params)

    # ---------------- 关联健康状态 ----------------
    def set_link_state(self, character_id: int, state: str, only_from_ok: bool = False) -> bool:
        """设置关联状态。only_from_ok=True 时只覆盖 ok/空，不覆盖更强的信号。"""
        if only_from_ok:
            row = self.q1("SELECT link_state FROM characters WHERE character_id=?",
                          (int(character_id),))
            if row is None:
                return False
            current = (row["link_state"] or "")
            if current not in ("", cfgmod.LINK_OK):
                return False
        self.x("UPDATE characters SET link_state=?, link_state_at=? WHERE character_id=?",
               (state, utils.now(), int(character_id)))
        return True

    def link_state(self, character_id: int) -> str:
        row = self.q1("SELECT link_state FROM characters WHERE character_id=?",
                      (int(character_id),))
        return (row["link_state"] or "") if row else ""

    @staticmethod
    def state_allows_sync(state) -> bool:
        """只有 ok（或旧行的空值）允许拉数据；其余等用户重新登录。"""
        return (state or "") in ("", cfgmod.LINK_OK)

    def access_token(self, character_id: int):
        """解密后的访问令牌（可能是旧行里的明文）。解不开返回 None。"""
        row = self.q1("SELECT access_token FROM characters WHERE character_id=?",
                      (int(character_id),))
        if row is None or row["access_token"] is None:
            return None
        try:
            return secure.unseal(row["access_token"], character_id, "access").decode("utf-8")
        except Exception as exc:
            utils.log("解密 %s 的访问令牌失败（不记录令牌内容）：%s", character_id, type(exc).__name__)
            return None

    def set_active(self, character_id: int) -> None:
        c = self.conn()
        c.execute("UPDATE characters SET is_active=0")
        c.execute("UPDATE characters SET is_active=1 WHERE character_id=?", (int(character_id),))
        c.commit()

    def delete_character(self, character_id: int) -> int:
        return self.x("DELETE FROM characters WHERE character_id=?", (int(character_id),))

    def refresh_token(self, character_id: int):
        row = self.q1("SELECT refresh_token FROM characters WHERE character_id=?", (int(character_id),))
        if row is None or row["refresh_token"] is None:
            return None
        try:
            return secure.unseal(row["refresh_token"], character_id, "refresh").decode("utf-8")
        except secure.TokenKeyMissing as exc:
            utils.log("角色 %s 的刷新令牌无法解密（缺少密钥）：%s", character_id, exc)
            return None
        except Exception as exc:
            # 只记异常类型，绝不记录令牌内容
            utils.log("角色 %s 的刷新令牌无法解密：%s", character_id, type(exc).__name__)
            return None

    # ---------------- 令牌格式迁移 ----------------
    def prepare_stored_tokens(self) -> dict:
        """启动时把明文/旧信封的令牌就地换成当前后端，并报告解不开的行。

        幂等：已经是当前格式的行逐字节不动。
        解不开的行**不删**、也不静默忽略 —— 标成 token_dead 让用户重新登录，
        同时把问题报给调用方（会显示在「关于 → 环境自检」里）。
        """
        report = {"checked": 0, "migrated": 0, "unreadable": [], "encrypted": 0}
        rows = self.q("SELECT character_id, name, refresh_token, access_token FROM characters")
        for row in rows:
            character_id = row["character_id"]
            report["checked"] += 1
            new_values = {}
            broken = False
            for column, field in (("refresh_token", "refresh"), ("access_token", "access")):
                raw = row[column]
                if raw is None or raw == b"" or raw == "":
                    continue
                if secure.is_encrypted(raw):
                    report["encrypted"] += 1
                if secure.is_current(raw):
                    continue                       # 已是当前格式，跳过（保证幂等）
                try:
                    plain = secure.unseal(raw, character_id, field)
                except Exception as exc:
                    broken = True
                    report["unreadable"].append({
                        "character_id": character_id, "name": row["name"], "field": field,
                        "reason": type(exc).__name__,
                    })
                    utils.log("角色 %s 的 %s 令牌解不开（%s），标记为需重新登录",
                              character_id, field, type(exc).__name__)
                    continue
                if plain:
                    new_values[column] = secure.seal(plain, character_id, field)
            if new_values:
                sets = ",".join("%s=?" % c for c in new_values)
                sets += ",encrypted=?"
                self.x("UPDATE characters SET %s WHERE character_id=?" % sets,
                       list(new_values.values()) + [1 if secure.available() else 0, character_id])
                report["migrated"] += 1
            if broken:
                self.set_link_state(character_id, cfgmod.LINK_TOKEN_DEAD, only_from_ok=True)
        if report["migrated"]:
            utils.log("已把 %d 个角色的令牌迁移到当前加密格式（%s）",
                      report["migrated"], secure.backend())
        return report

    # ------------------------------------------------------------------
    # ESI 响应缓存
    # ------------------------------------------------------------------
    def cache_get(self, cache_key: str):
        row = self.q1("SELECT * FROM http_cache WHERE cache_key=?", (cache_key,))
        return dict(row) if row else None

    def cache_put(self, cache_key: str, url: str, body: str, etag=None,
                  expires=None, status=200) -> None:
        self.x(
            "INSERT INTO http_cache(cache_key,url,etag,expires,fetched_at,status,body)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET"
            " url=excluded.url, etag=excluded.etag, expires=excluded.expires,"
            " fetched_at=excluded.fetched_at, status=excluded.status, body=excluded.body",
            (cache_key, url, etag, int(expires or 0), utils.now(), int(status), body),
        )

    def cache_touch(self, cache_key: str, expires=None) -> None:
        if expires:
            self.x("UPDATE http_cache SET expires=?, fetched_at=? WHERE cache_key=?",
                   (int(expires), utils.now(), cache_key))

    def cache_clear(self, like: str = None) -> int:
        if like:
            return self.x("DELETE FROM http_cache WHERE cache_key LIKE ?", (like,))
        return self.x("DELETE FROM http_cache")

    def cache_stats(self) -> dict:
        row = self.q1("SELECT COUNT(*) n, MAX(fetched_at) last, SUM(LENGTH(body)) bytes FROM http_cache")
        return {"entries": row["n"] or 0, "last_fetch": row["last"], "bytes": row["bytes"] or 0}

    # ------------------------------------------------------------------
    # SDE：物品类型
    # ------------------------------------------------------------------
    def sde_build(self):
        return self.meta_get("sde_build")

    def sde_ready(self) -> bool:
        row = self.q1("SELECT COUNT(*) n FROM types")
        return bool(row and row["n"] > 1000)

    def insert_types(self, rows) -> int:
        return self.xmany(
            "INSERT INTO types(type_id,name,name_en,group_id,category_id,market_group_id,"
            "meta_group_id,tech_level,volume,packaged_volume,portion_size,base_price,published,icon_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(type_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en,"
            " group_id=excluded.group_id,category_id=excluded.category_id,"
            " market_group_id=excluded.market_group_id,meta_group_id=excluded.meta_group_id,"
            " tech_level=excluded.tech_level,volume=excluded.volume,"
            " packaged_volume=excluded.packaged_volume,portion_size=excluded.portion_size,"
            " base_price=excluded.base_price,published=excluded.published,icon_id=excluded.icon_id",
            rows,
        )

    def insert_groups(self, rows) -> int:
        return self.xmany(
            "INSERT INTO groups(group_id,name,name_en,category_id,published) VALUES(?,?,?,?,?)"
            " ON CONFLICT(group_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en,"
            " category_id=excluded.category_id,published=excluded.published", rows)

    def insert_categories(self, rows) -> int:
        return self.xmany(
            "INSERT INTO categories(category_id,name,name_en,published) VALUES(?,?,?,?)"
            " ON CONFLICT(category_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en,"
            " published=excluded.published", rows)

    def insert_market_groups(self, rows) -> int:
        return self.xmany(
            "INSERT INTO market_groups(market_group_id,name,name_en,parent_id,has_types,icon_id)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(market_group_id) DO UPDATE SET"
            " name=excluded.name,name_en=excluded.name_en,parent_id=excluded.parent_id,"
            " has_types=excluded.has_types,icon_id=excluded.icon_id", rows)

    def insert_meta_groups(self, rows) -> int:
        return self.xmany(
            "INSERT INTO meta_groups(meta_group_id,name,name_en) VALUES(?,?,?)"
            " ON CONFLICT(meta_group_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en", rows)

    def insert_solar_systems(self, rows) -> int:
        return self.xmany(
            "INSERT INTO solar_systems(system_id,name,name_en,security_status,region_id,constellation_id)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(system_id) DO UPDATE SET name=excluded.name,"
            " name_en=excluded.name_en,security_status=excluded.security_status,"
            " region_id=excluded.region_id,constellation_id=excluded.constellation_id", rows)

    def insert_regions(self, rows) -> int:
        return self.xmany(
            "INSERT INTO regions(region_id,name,name_en) VALUES(?,?,?)"
            " ON CONFLICT(region_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en", rows)

    def insert_blueprint_activities(self, rows) -> int:
        return self.xmany(
            "INSERT INTO blueprint_activities(blueprint_type_id,activity,time,max_production_limit)"
            " VALUES(?,?,?,?) ON CONFLICT(blueprint_type_id,activity) DO UPDATE SET"
            " time=excluded.time,max_production_limit=excluded.max_production_limit", rows)

    def insert_activity_materials(self, rows) -> int:
        return self.xmany(
            "INSERT INTO activity_materials(blueprint_type_id,activity,material_type_id,quantity)"
            " VALUES(?,?,?,?) ON CONFLICT(blueprint_type_id,activity,material_type_id)"
            " DO UPDATE SET quantity=excluded.quantity", rows)

    def insert_activity_products(self, rows) -> int:
        return self.xmany(
            "INSERT INTO activity_products(blueprint_type_id,activity,product_type_id,quantity,probability)"
            " VALUES(?,?,?,?,?) ON CONFLICT(blueprint_type_id,activity,product_type_id)"
            " DO UPDATE SET quantity=excluded.quantity,probability=excluded.probability", rows)

    def insert_activity_skills(self, rows) -> int:
        return self.xmany(
            "INSERT INTO activity_skills(blueprint_type_id,activity,skill_type_id,level)"
            " VALUES(?,?,?,?) ON CONFLICT(blueprint_type_id,activity,skill_type_id)"
            " DO UPDATE SET level=excluded.level", rows)

    def insert_type_materials(self, rows) -> int:
        return self.xmany(
            "INSERT INTO type_materials(type_id,material_type_id,quantity) VALUES(?,?,?)"
            " ON CONFLICT(type_id,material_type_id) DO UPDATE SET quantity=excluded.quantity", rows)

    def insert_bonus_attributes(self, rows) -> int:
        return self.xmany(
            "INSERT INTO type_bonus_attributes(type_id,attribute_id,value) VALUES(?,?,?)"
            " ON CONFLICT(type_id,attribute_id) DO UPDATE SET value=excluded.value", rows)

    def insert_dogma_attributes(self, rows) -> int:
        return self.xmany(
            "INSERT INTO dogma_attributes(attribute_id,name,display_name,unit_id) VALUES(?,?,?,?)"
            " ON CONFLICT(attribute_id) DO UPDATE SET name=excluded.name,"
            " display_name=excluded.display_name, unit_id=excluded.unit_id", rows)

    def dogma_attribute_names(self, ids) -> dict:
        ids = [int(i) for i in ids]
        out = {}
        for part in utils.chunked(ids, 400):
            marks = ",".join("?" for _ in part)
            for row in self.q("SELECT * FROM dogma_attributes WHERE attribute_id IN (%s)" % marks,
                              part):
                out[row["attribute_id"]] = dict(row)
        return out

    def insert_modifier_sources(self, rows) -> int:
        return self.xmany(
            "INSERT INTO industry_modifier_sources(type_id,activity,kind,attribute_id,filter_id)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(type_id,activity,kind,attribute_id,filter_id) DO NOTHING", rows)

    def insert_target_filters(self, rows) -> int:
        return self.xmany(
            "INSERT INTO target_filters(filter_id,name,category_ids,group_ids,type_ids)"
            " VALUES(?,?,?,?,?) ON CONFLICT(filter_id) DO UPDATE SET name=excluded.name,"
            " category_ids=excluded.category_ids, group_ids=excluded.group_ids,"
            " type_ids=excluded.type_ids", rows)

    def target_filters(self, ids) -> dict:
        ids = [int(i) for i in ids]
        out = {}
        for part in utils.chunked(ids, 400):
            marks = ",".join("?" for _ in part)
            for row in self.q("SELECT * FROM target_filters WHERE filter_id IN (%s)" % marks, part):
                out[row["filter_id"]] = dict(row)
        return out

    def modifier_rules(self, type_ids, activity: str) -> list:
        """取这些类型在某活动下的加成规则：[{type_id, kind, attribute_id, filter_id}]"""
        type_ids = [int(i) for i in type_ids]
        out = []
        for part in utils.chunked(type_ids, 400):
            marks = ",".join("?" for _ in part)
            for row in self.q(
                    "SELECT type_id, kind, attribute_id, filter_id FROM industry_modifier_sources"
                    " WHERE activity=? AND type_id IN (%s)" % marks,
                    [activity] + part):
                out.append(dict(row))
        return out

    def clear_sde_tables(self) -> None:
        c = self.conn()
        for table in ("types", "groups", "categories", "market_groups", "meta_groups",
                      "blueprint_activities", "activity_materials", "activity_products",
                      "activity_skills", "type_materials", "type_bonus_attributes",
                      "dogma_attributes", "industry_modifier_sources", "target_filters",
                      "solar_systems", "regions"):
            c.execute("DELETE FROM %s" % table)
        c.commit()

    def type_by_id(self, type_id: int):
        row = self.q1("SELECT * FROM types WHERE type_id=?", (int(type_id),))
        return dict(row) if row else None

    def types_by_ids(self, ids) -> dict:
        ids = [int(i) for i in ids]
        out = {}
        for part in utils.chunked(ids, 900):
            marks = ",".join("?" for _ in part)
            for row in self.q("SELECT * FROM types WHERE type_id IN (%s)" % marks, part):
                out[row["type_id"]] = dict(row)
        return out

    def type_names(self, ids) -> dict:
        ids = [int(i) for i in ids]
        out = {}
        for part in utils.chunked(ids, 900):
            marks = ",".join("?" for _ in part)
            for row in self.q("SELECT type_id,name FROM types WHERE type_id IN (%s)" % marks, part):
                out[row["type_id"]] = row["name"] or ("类型 %d" % row["type_id"])
        return out

    def search_types(self, query: str, limit: int = 40, published_only: bool = True):
        query = (query or "").strip()
        if not query:
            return []
        like = "%%%s%%" % query.replace("%", "")
        sql = ("SELECT type_id,name,name_en,group_id,category_id,market_group_id,volume,"
               "packaged_volume,portion_size,published FROM types WHERE name LIKE ?")
        params = [like]
        if published_only:
            sql += " AND published=1"
        # 前缀匹配优先
        sql += " ORDER BY CASE WHEN name LIKE ? THEN 0 ELSE 1 END, LENGTH(name), name LIMIT ?"
        params.extend([query.replace("%", "") + "%", int(limit)])
        return [dict(r) for r in self.q(sql, params)]

    def blueprint_activities(self, blueprint_type_id: int) -> dict:
        """返回 {activity: {time, materials:[{type_id,quantity}], products:[...], skills:[...]}}"""
        bp = int(blueprint_type_id)
        out = {}
        for row in self.q("SELECT * FROM blueprint_activities WHERE blueprint_type_id=?", (bp,)):
            out[row["activity"]] = {
                "activity": row["activity"],
                "time": row["time"] or 0,
                "max_production_limit": row["max_production_limit"] or 1,
                "materials": [], "products": [], "skills": [],
            }
        for row in self.q("SELECT * FROM activity_materials WHERE blueprint_type_id=?", (bp,)):
            act = out.setdefault(row["activity"], {"activity": row["activity"], "time": 0,
                                                   "max_production_limit": 1, "materials": [],
                                                   "products": [], "skills": []})
            act["materials"].append({"type_id": row["material_type_id"], "quantity": row["quantity"]})
        for row in self.q("SELECT * FROM activity_products WHERE blueprint_type_id=?", (bp,)):
            act = out.setdefault(row["activity"], {"activity": row["activity"], "time": 0,
                                                   "max_production_limit": 1, "materials": [],
                                                   "products": [], "skills": []})
            act["products"].append({"type_id": row["product_type_id"], "quantity": row["quantity"],
                                    "probability": row["probability"]})
        for row in self.q("SELECT * FROM activity_skills WHERE blueprint_type_id=?", (bp,)):
            act = out.setdefault(row["activity"], {"activity": row["activity"], "time": 0,
                                                   "max_production_limit": 1, "materials": [],
                                                   "products": [], "skills": []})
            act["skills"].append({"type_id": row["skill_type_id"], "level": row["level"]})
        return out

    def blueprint_of_product(self, product_type_id: int, activity: str = "manufacturing"):
        """由产物反查蓝图（用于「多种产品」里直接加成品）。"""
        rows = self.q(
            "SELECT blueprint_type_id, quantity, probability FROM activity_products"
            " WHERE product_type_id=? AND activity=?", (int(product_type_id), activity))
        return [dict(r) for r in rows]

    def blueprint_for_product(self, product_type_id: int):
        """由产物找蓝图，优先制造，其次反应。返回 (blueprint_type_id, activity, quantity)。"""
        for activity in ("manufacturing", "reaction"):
            row = self.q1(
                "SELECT blueprint_type_id, quantity FROM activity_products"
                " WHERE product_type_id=? AND activity=? LIMIT 1",
                (int(product_type_id), activity))
            if row:
                return row["blueprint_type_id"], activity, row["quantity"] or 1
        return None, None, 0

    def activity_of_blueprint(self, blueprint_type_id: int):
        rows = self.q(
            "SELECT activity FROM blueprint_activities WHERE blueprint_type_id=?",
            (int(blueprint_type_id),))
        keys = [r["activity"] for r in rows]
        for preferred in ("manufacturing", "reaction", "invention", "copying",
                          "research_material", "research_time"):
            if preferred in keys:
                return preferred
        return keys[0] if keys else None

    def has_activity(self, blueprint_type_id: int, activity: str) -> bool:
        row = self.q1("SELECT 1 FROM blueprint_activities WHERE blueprint_type_id=? AND activity=?",
                      (int(blueprint_type_id), activity))
        return row is not None

    def products_of(self, blueprint_type_id: int, activity: str):
        return [dict(r) for r in self.q(
            "SELECT product_type_id, quantity, probability FROM activity_products"
            " WHERE blueprint_type_id=? AND activity=?", (int(blueprint_type_id), activity))]

    def category_tree(self):
        """左侧分类树的原始数据。"""
        cats = [dict(r) for r in self.q("SELECT * FROM categories WHERE published=1 ORDER BY name")]
        groups = [dict(r) for r in self.q("SELECT * FROM groups WHERE published=1 ORDER BY name")]
        return {"categories": cats, "groups": groups}

    def search_blueprints(self, query: str, limit: int = 40, activity: str = "manufacturing"):
        """按产物名搜索可制造物品（蓝图市场组 2 = 蓝图和反应，排除之）。"""
        query = (query or "").strip()
        if not query:
            return []
        like = "%%%s%%" % query.replace("%", "")
        sql = (
            "SELECT t.type_id, t.name, t.name_en, t.group_id, t.category_id, t.market_group_id,"
            " t.volume, t.portion_size, b.blueprint_type_id, b.quantity AS product_quantity"
            " FROM types t JOIN activity_products b ON b.product_type_id = t.type_id"
            " WHERE b.activity=? AND t.published=1 AND t.name LIKE ?"
            " ORDER BY CASE WHEN t.name LIKE ? THEN 0 ELSE 1 END, LENGTH(t.name), t.name LIMIT ?"
        )
        params = [activity, like, query.replace("%", "") + "%", int(limit)]
        return [dict(r) for r in self.q(sql, params)]

    def sde_stats(self) -> dict:
        def one(sql):
            row = self.q1(sql)
            return row[0] if row else 0
        return {
            "build": self.sde_build(),
            "types": one("SELECT COUNT(*) FROM types"),
            "blueprints": one("SELECT COUNT(*) FROM blueprint_activities"),
            "manufacturing": one("SELECT COUNT(*) FROM blueprint_activities WHERE activity='manufacturing'"),
            "reactions": one("SELECT COUNT(*) FROM blueprint_activities WHERE activity='reaction'"),
            "materials": one("SELECT COUNT(*) FROM activity_materials"),
            "systems": one("SELECT COUNT(*) FROM solar_systems"),
            "modifier_sources": one("SELECT COUNT(*) FROM industry_modifier_sources"),
        }

    # ------------------------------------------------------------------
    # 市场
    # ------------------------------------------------------------------
    def put_prices(self, rows) -> int:
        ts = utils.now()
        return self.xmany(
            "INSERT INTO prices(type_id,adjusted_price,average_price,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(type_id) DO UPDATE SET adjusted_price=excluded.adjusted_price,"
            " average_price=excluded.average_price, updated_at=excluded.updated_at",
            [(r[0], r[1], r[2], ts) for r in rows])

    def get_price(self, type_id: int):
        row = self.q1("SELECT * FROM prices WHERE type_id=?", (int(type_id),))
        return dict(row) if row else None

    def prices_meta(self):
        row = self.q1("SELECT COUNT(*) n, MAX(updated_at) ts FROM prices")
        return {"entries": row["n"] or 0, "updated_at": row["ts"]}

    def put_market_stats(self, rows) -> int:
        """rows: (type_id, region_id, buy, sell, buy_volume, sell_volume, source)"""
        ts = utils.now()
        return self.xmany(
            "INSERT INTO market_stats(type_id,region_id,buy,sell,buy_volume,sell_volume,updated_at,source)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(type_id,region_id) DO UPDATE SET"
            " buy=excluded.buy, sell=excluded.sell, buy_volume=excluded.buy_volume,"
            " sell_volume=excluded.sell_volume, updated_at=excluded.updated_at, source=excluded.source",
            [(r[0], r[1], r[2], r[3], r[4], r[5], ts, r[6] if len(r) > 6 else None) for r in rows])

    def get_market_stats(self, type_ids, region_id: int) -> dict:
        ids = [int(i) for i in type_ids]
        out = {}
        for part in utils.chunked(ids, 900):
            marks = ",".join("?" for _ in part)
            for row in self.q(
                    "SELECT * FROM market_stats WHERE region_id=? AND type_id IN (%s)" % marks,
                    [int(region_id)] + part):
                out[row["type_id"]] = dict(row)
        return out

    def put_market_history(self, type_id: int, region_id: int, rows) -> int:
        return self.xmany(
            "INSERT INTO market_history(type_id,region_id,date,highest,lowest,average,volume,order_count)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(type_id,region_id,date) DO UPDATE SET"
            " highest=excluded.highest, lowest=excluded.lowest, average=excluded.average,"
            " volume=excluded.volume, order_count=excluded.order_count",
            [(int(type_id), int(region_id), r.get("date"), r.get("highest"), r.get("lowest"),
              r.get("average"), r.get("volume"), r.get("order_count")) for r in rows])

    def get_market_history(self, type_id: int, region_id: int, days: int = 30):
        return [dict(r) for r in self.q(
            "SELECT * FROM market_history WHERE type_id=? AND region_id=?"
            " ORDER BY date DESC LIMIT ?", (int(type_id), int(region_id), int(days)))]

    def history_average(self, type_id: int, region_id: int, days: int = 7):
        """近 N 天加权平均价（按成交量加权）。"""
        row = self.q1(
            "SELECT SUM(average*volume) sw, SUM(volume) sv FROM ("
            " SELECT average, volume FROM market_history WHERE type_id=? AND region_id=?"
            " ORDER BY date DESC LIMIT ?)", (int(type_id), int(region_id), int(days)))
        if row and row["sw"] and row["sv"]:
            return float(row["sw"]) / float(row["sv"])
        return None

    # ------------------------------------------------------------------
    # 工业环境
    # ------------------------------------------------------------------
    def put_cost_indices(self, rows) -> int:
        return self.xmany(
            "INSERT INTO cost_indices(system_id,activity,cost_index) VALUES(?,?,?)"
            " ON CONFLICT(system_id,activity) DO UPDATE SET cost_index=excluded.cost_index", rows)

    def cost_indices_for(self, system_id: int) -> dict:
        return {r["activity"]: r["cost_index"] for r in self.q(
            "SELECT activity, cost_index FROM cost_indices WHERE system_id=?", (int(system_id),))}

    def cost_indices_all(self):
        return [dict(r) for r in self.q("SELECT * FROM cost_indices")]

    def put_facilities(self, rows) -> int:
        return self.xmany(
            "INSERT INTO industry_facilities(facility_id,type_id,solar_system_id,region_id)"
            " VALUES(?,?,?,?) ON CONFLICT(facility_id) DO UPDATE SET type_id=excluded.type_id,"
            " solar_system_id=excluded.solar_system_id, region_id=excluded.region_id", rows)

    def facility(self, facility_id: int):
        row = self.q1("SELECT * FROM industry_facilities WHERE facility_id=?", (int(facility_id),))
        return dict(row) if row else None

    def put_stations(self, rows) -> int:
        return self.xmany(
            "INSERT INTO stations(station_id,name,solar_system_id,type_id) VALUES(?,?,?,?)"
            " ON CONFLICT(station_id) DO UPDATE SET name=excluded.name,"
            " solar_system_id=excluded.solar_system_id, type_id=excluded.type_id", rows)

    def put_corp_structures(self, rows) -> int:
        """写入军团建筑（含在线服务装备与燃料到期时间）。"""
        ts = utils.now()
        return self.xmany(
            "INSERT INTO corp_structures(structure_id,corporation_id,name,type_id,"
            " solar_system_id,services,fuel_expires,state,state_timer_end,unanchors_at,"
            " fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(structure_id) DO UPDATE SET corporation_id=excluded.corporation_id,"
            " name=excluded.name, type_id=excluded.type_id,"
            " solar_system_id=excluded.solar_system_id, services=excluded.services,"
            " fuel_expires=excluded.fuel_expires, state=excluded.state,"
            " state_timer_end=excluded.state_timer_end, unanchors_at=excluded.unanchors_at,"
            " fetched_at=excluded.fetched_at",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], ts) for r in rows])

    def corp_structures(self, corporation_id: int = None) -> list:
        """读回军团建筑（services 解析成对象）。"""
        sql = "SELECT * FROM corp_structures"
        params = ()
        if corporation_id:
            sql += " WHERE corporation_id=?"
            params = (int(corporation_id),)
        sql += " ORDER BY name"
        out = []
        for row in self.q(sql, params):
            item = dict(row)
            try:
                item["services"] = utils.jloads(item.get("services"), []) or []
            except Exception:
                item["services"] = []
            item["online_services"] = [s for s in item["services"]
                                       if (s.get("state") or "").lower() == "online"]
            out.append(item)
        return out

    def delete_corp_structures(self, corporation_id: int = None) -> int:
        if corporation_id:
            return self.x("DELETE FROM corp_structures WHERE corporation_id=?",
                          (int(corporation_id),))
        return self.x("DELETE FROM corp_structures")

    def put_structures(self, rows) -> int:
        ts = utils.now()
        return self.xmany(
            "INSERT INTO structures(structure_id,name,solar_system_id,type_id,owner_id,fetched_at)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(structure_id) DO UPDATE SET name=excluded.name,"
            " solar_system_id=excluded.solar_system_id, type_id=excluded.type_id,"
            " owner_id=excluded.owner_id, fetched_at=excluded.fetched_at",
            [(r[0], r[1], r[2], r[3], r[4], ts) for r in rows])

    def location_names(self, ids) -> dict:
        """统一的「地点 ID -> 名称」，覆盖星系 / NPC 空间站 / 玩家建筑。"""
        ids = [int(i) for i in ids]
        out = {}
        for part in utils.chunked(ids, 900):
            marks = ",".join("?" for _ in part)
            for row in self.q("SELECT structure_id k, name v FROM structures WHERE structure_id IN (%s)" % marks, part):
                out[row["k"]] = row["v"]
            # 军团建筑表（「读取军团建筑」写进来的）也带名字，而且**不需要停靠权限** ——
            # 逐栋查 /universe/structures/{id}/ 在没权限时会 403，这里先兜一层。
            for row in self.q("SELECT structure_id k, name v FROM corp_structures WHERE structure_id IN (%s)" % marks, part):
                out.setdefault(row["k"], row["v"])
            for row in self.q("SELECT station_id k, name v FROM stations WHERE station_id IN (%s)" % marks, part):
                out.setdefault(row["k"], row["v"])
            for row in self.q("SELECT system_id k, name v FROM solar_systems WHERE system_id IN (%s)" % marks, part):
                out.setdefault(row["k"], row["v"])
        return {k: v for k, v in out.items() if v}

    def system_name(self, system_id: int):
        row = self.q1("SELECT name, security_status FROM solar_systems WHERE system_id=?", (int(system_id),))
        return dict(row) if row else None

    def systems_by_ids(self, ids) -> dict:
        ids = [int(i) for i in ids]
        out = {}
        for part in utils.chunked(ids, 900):
            marks = ",".join("?" for _ in part)
            for row in self.q("SELECT * FROM solar_systems WHERE system_id IN (%s)" % marks, part):
                out[row["system_id"]] = dict(row)
        return out

    def modifier_sources(self, activity: str = None):
        """可在工业建筑上安装、且提供加成的类型（改装件/建筑）。"""
        if activity:
            return [dict(r) for r in self.q(
                "SELECT DISTINCT type_id FROM industry_modifier_sources WHERE activity=?", (activity,))]
        return [dict(r) for r in self.q("SELECT DISTINCT type_id FROM industry_modifier_sources")]

    def bonus_attributes(self, type_id: int) -> dict:
        return {r["attribute_id"]: r["value"] for r in self.q(
            "SELECT attribute_id, value FROM type_bonus_attributes WHERE type_id=?", (int(type_id),))}

    def blueprint_rank(self, type_id: int):
        """官方 industryBlueprintRank（属性 1955）。可能为小数，例如 7.8。"""
        row = self.q1("SELECT value FROM type_bonus_attributes WHERE type_id=? AND attribute_id=?",
                      (int(type_id), cfgmod.BLUEPRINT_RANK_ATTRIBUTE))
        return row["value"] if row else None

    def bonus_attributes_many(self, type_ids) -> dict:
        ids = [int(i) for i in type_ids]
        out = {}
        for part in utils.chunked(ids, 400):
            marks = ",".join("?" for _ in part)
            for row in self.q(
                    "SELECT type_id, attribute_id, value FROM type_bonus_attributes"
                    " WHERE type_id IN (%s)" % marks, part):
                out.setdefault(row["type_id"], {})[row["attribute_id"]] = row["value"]
        return out

    # ------------------------------------------------------------------
    # 库存 / 蓝图 / 工业项目
    # ------------------------------------------------------------------
    def replace_assets(self, scope: str, rows) -> int:
        ts = utils.now()
        c = self.conn()
        c.execute("DELETE FROM assets WHERE scope=?", (scope,))
        c.executemany(
            "INSERT OR REPLACE INTO assets(scope,item_id,type_id,quantity,location_id,"
            "location_flag,location_type,is_singleton,fetched_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [(scope, r.get("item_id"), r.get("type_id"), r.get("quantity"), r.get("location_id"),
              r.get("location_flag"), r.get("location_type"), 1 if r.get("is_singleton") else 0, ts)
             for r in rows])
        c.commit()
        return len(rows)

    def assets_of(self, scope: str):
        return [dict(r) for r in self.q("SELECT * FROM assets WHERE scope=?", (scope,))]

    def asset_totals(self, scope: str):
        """按 type_id 汇总数量。"""
        return {r["type_id"]: r["qty"] for r in self.q(
            "SELECT type_id, SUM(quantity) qty FROM assets WHERE scope=? GROUP BY type_id", (scope,))}

    def assets_meta(self, scope: str):
        row = self.q1("SELECT COUNT(*) n, MAX(fetched_at) ts FROM assets WHERE scope=?", (scope,))
        return {"entries": row["n"] or 0, "fetched_at": row["ts"]}

    def replace_blueprints(self, scope: str, rows) -> int:
        ts = utils.now()
        c = self.conn()
        c.execute("DELETE FROM blueprints WHERE scope=?", (scope,))
        c.executemany(
            "INSERT OR REPLACE INTO blueprints(scope,item_id,type_id,material_efficiency,"
            "time_efficiency,runs,quantity,location_id,location_flag,fetched_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(scope, r.get("item_id"), r.get("type_id"), r.get("material_efficiency", 0),
              r.get("time_efficiency", 0), r.get("runs", -1), r.get("quantity", -1),
              r.get("location_id"), r.get("location_flag"), ts) for r in rows])
        c.commit()
        return len(rows)

    def blueprints_of(self, scope: str):
        return [dict(r) for r in self.q(
            "SELECT * FROM blueprints WHERE scope=? ORDER BY type_id, material_efficiency DESC", (scope,))]

    def blueprint_best(self, scope: str, blueprint_type_id: int):
        """取该蓝图在库存里效率最高的一份（ME 优先，其次 TE）。"""
        row = self.q1(
            "SELECT * FROM blueprints WHERE scope=? AND type_id=?"
            " ORDER BY material_efficiency DESC, time_efficiency DESC LIMIT 1",
            (scope, int(blueprint_type_id)))
        return dict(row) if row else None

    def blueprints_meta(self, scope: str):
        row = self.q1("SELECT COUNT(*) n, MAX(fetched_at) ts FROM blueprints WHERE scope=?", (scope,))
        return {"entries": row["n"] or 0, "fetched_at": row["ts"]}

    def replace_jobs(self, scope: str, rows) -> int:
        ts = utils.now()
        c = self.conn()
        c.execute("DELETE FROM industry_jobs WHERE scope=?", (scope,))
        c.executemany(
            "INSERT OR REPLACE INTO industry_jobs(scope,job_id,installer_id,facility_id,station_id,"
            "activity_id,activity,blueprint_id,blueprint_type_id,blueprint_location_id,product_type_id,"
            "runs,licensed_runs,successful_runs,probability,status,duration,cost,start_date,end_date,"
            "completed_date,completed_character_id,fetched_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(scope, r.get("job_id"), r.get("installer_id"), r.get("facility_id"), r.get("station_id"),
              r.get("activity_id"), r.get("activity"), r.get("blueprint_id"), r.get("blueprint_type_id"),
              r.get("blueprint_location_id"), r.get("product_type_id"), r.get("runs"),
              r.get("licensed_runs"), r.get("successful_runs"), r.get("probability"), r.get("status"),
              r.get("duration"), r.get("cost"), r.get("start_date"), r.get("end_date"),
              r.get("completed_date"), r.get("completed_character_id"), ts) for r in rows])
        c.commit()
        return len(rows)

    def jobs_of(self, scope: str):
        return [dict(r) for r in self.q(
            "SELECT * FROM industry_jobs WHERE scope=? ORDER BY end_date", (scope,))]

    def jobs_meta(self, scope: str):
        row = self.q1("SELECT COUNT(*) n, MAX(fetched_at) ts FROM industry_jobs WHERE scope=?", (scope,))
        return {"entries": row["n"] or 0, "fetched_at": row["ts"]}

    def clear_scope(self, scope: str) -> None:
        c = self.conn()
        for table in ("assets", "blueprints", "industry_jobs"):
            c.execute("DELETE FROM %s WHERE scope=?" % table, (scope,))
        c.commit()

    # ------------------------------------------------------------------
    # 方案
    # ------------------------------------------------------------------
    def list_plans(self):
        return [dict(r) for r in self.q("SELECT plan_id,name,created_at,updated_at FROM plans ORDER BY name")]

    def get_plan(self, plan_id: int):
        row = self.q1("SELECT * FROM plans WHERE plan_id=?", (int(plan_id),))
        if not row:
            return None
        out = dict(row)
        out["data"] = utils.jloads(out["data"], {})
        return out

    def save_plan(self, name: str, data: dict, plan_id=None):
        ts = utils.now()
        payload = utils.jdumps(data)
        if plan_id:
            self.x("UPDATE plans SET name=?,data=?,updated_at=? WHERE plan_id=?",
                   (name, payload, ts, int(plan_id)))
            return int(plan_id)
        cur = self.conn().execute(
            "INSERT INTO plans(name,created_at,updated_at,data) VALUES(?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
            (name, ts, ts, payload))
        self.conn().commit()
        return cur.lastrowid

    def delete_plan(self, plan_id: int) -> int:
        return self.x("DELETE FROM plans WHERE plan_id=?", (int(plan_id),))

    # ------------------------------------------------------------------
    # 运维
    # ------------------------------------------------------------------
    def vacuum(self) -> None:
        self.conn().execute("VACUUM")
        self.conn().commit()

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                total += os.path.getsize(path)
        return total

    def quick_check(self) -> tuple:
        """自检数据库是否损坏，返回 (是否正常, 说明)。

        为什么需要：SQLite 损坏时报的是 `database disk image is malformed`，
        而各处的读取失败大多被 try/except 静默吞掉 —— 用户只会看到「某些数据一直
        刷不出来」，很难联想到是数据库文件坏了（例如程序在写库时被强杀/断电）。
        启动时自检一次，就能把这件事明确说出来并提供重建入口。
        """
        try:
            row = self.conn().execute("PRAGMA quick_check").fetchone()
            result = (row[0] if row else "") or ""
        except Exception as exc:
            return False, "无法读取数据库：%s" % exc
        if str(result).lower() == "ok":
            return True, "ok"
        return False, str(result)[:200]

    def rebuild(self) -> dict:
        """把损坏的数据库挪到一边，重建一个空库（SDE / 物价需要重新导入）。

        数据库在这里**只是缓存**：SDE 能重新下载、物价能重新拉、令牌在令牌表里
        （重建会丢令牌 → 需要重新登录，这点必须告诉用户）。
        """
        import time
        self.close_all()
        moved = []
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                target = "%s.corrupt-%s%s" % (self.path, stamp, suffix)
                try:
                    os.replace(path, target)
                    moved.append(os.path.basename(target))
                except OSError:
                    try:
                        os.remove(path)
                        moved.append(os.path.basename(path) + "（已删除）")
                    except OSError:
                        pass
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            c.commit()
        return {"moved": moved, "path": self.path}

    def close_all(self) -> None:
        """关掉本线程的连接（重建 / 退出前调用）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

