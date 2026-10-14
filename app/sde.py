"""静态数据（SDE）下载与导入。

数据源：CCP 官方 SDE 的 JSON Lines 导出
  * 最新版本号：  /static-data/tranquility/latest.jsonl
  * 直链下载：    /static-data/eve-online-static-data-latest-jsonl.zip  （约 95 MB）

导入方式：解压流式读取 JSONL，单事务 UPSERT。
因为用的是 WAL + 一个写事务，导入期间界面仍可读到旧数据，
导入结束一次性切换，不会出现「空库」窗口。

需要用到的文件：
  types.jsonl                    物品类型（含中文名、体积、市场组）
  groups.jsonl / categories.jsonl 分类
  marketGroups.jsonl / metaGroups.jsonl
  blueprints.jsonl               蓝图活动：材料 / 产物 / 技能 / 时间 / 概率
  typeMaterials.jsonl            精炼产出（判断材料能否由矿石替代）
  typeDogma.jsonl                工业加成属性（strEngMatBonus 等）
  industryModifierSources.jsonl  哪些类型提供工业加成、对应哪个属性
  mapSolarSystems.jsonl / mapRegions.jsonl  星系名与安等
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import zipfile

from . import config as cfgmod
from . import utils

# --------------------------------------------------------------------------
# SQL（与 store.py 的表结构对应）
# --------------------------------------------------------------------------
SQL = {
    "types": (
        "INSERT INTO types(type_id,name,name_en,group_id,category_id,market_group_id,"
        "meta_group_id,tech_level,volume,packaged_volume,portion_size,base_price,published,icon_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(type_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en,"
        " group_id=excluded.group_id,category_id=excluded.category_id,"
        " market_group_id=excluded.market_group_id,meta_group_id=excluded.meta_group_id,"
        " tech_level=excluded.tech_level,volume=excluded.volume,"
        " packaged_volume=excluded.packaged_volume,portion_size=excluded.portion_size,"
        " base_price=excluded.base_price,published=excluded.published,icon_id=excluded.icon_id"),
    "groups": (
        "INSERT INTO groups(group_id,name,name_en,category_id,published) VALUES(?,?,?,?,?)"
        " ON CONFLICT(group_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en,"
        " category_id=excluded.category_id,published=excluded.published"),
    "categories": (
        "INSERT INTO categories(category_id,name,name_en,published) VALUES(?,?,?,?)"
        " ON CONFLICT(category_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en,"
        " published=excluded.published"),
    "market_groups": (
        "INSERT INTO market_groups(market_group_id,name,name_en,parent_id,has_types,icon_id)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(market_group_id) DO UPDATE SET"
        " name=excluded.name,name_en=excluded.name_en,parent_id=excluded.parent_id,"
        " has_types=excluded.has_types,icon_id=excluded.icon_id"),
    "meta_groups": (
        "INSERT INTO meta_groups(meta_group_id,name,name_en) VALUES(?,?,?)"
        " ON CONFLICT(meta_group_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en"),
    "solar_systems": (
        "INSERT INTO solar_systems(system_id,name,name_en,security_status,region_id,constellation_id)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(system_id) DO UPDATE SET name=excluded.name,"
        " name_en=excluded.name_en,security_status=excluded.security_status,"
        " region_id=excluded.region_id,constellation_id=excluded.constellation_id"),
    "regions": (
        "INSERT INTO regions(region_id,name,name_en) VALUES(?,?,?)"
        " ON CONFLICT(region_id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en"),
    "blueprint_activities": (
        "INSERT INTO blueprint_activities(blueprint_type_id,activity,time,max_production_limit)"
        " VALUES(?,?,?,?) ON CONFLICT(blueprint_type_id,activity) DO UPDATE SET"
        " time=excluded.time,max_production_limit=excluded.max_production_limit"),
    "activity_materials": (
        "INSERT INTO activity_materials(blueprint_type_id,activity,material_type_id,quantity)"
        " VALUES(?,?,?,?) ON CONFLICT(blueprint_type_id,activity,material_type_id)"
        " DO UPDATE SET quantity=excluded.quantity"),
    "activity_products": (
        "INSERT INTO activity_products(blueprint_type_id,activity,product_type_id,quantity,probability)"
        " VALUES(?,?,?,?,?) ON CONFLICT(blueprint_type_id,activity,product_type_id)"
        " DO UPDATE SET quantity=excluded.quantity,probability=excluded.probability"),
    "activity_skills": (
        "INSERT INTO activity_skills(blueprint_type_id,activity,skill_type_id,level)"
        " VALUES(?,?,?,?) ON CONFLICT(blueprint_type_id,activity,skill_type_id)"
        " DO UPDATE SET level=excluded.level"),
    "type_materials": (
        "INSERT INTO type_materials(type_id,material_type_id,quantity) VALUES(?,?,?)"
        " ON CONFLICT(type_id,material_type_id) DO UPDATE SET quantity=excluded.quantity"),
    "bonus_attributes": (
        "INSERT INTO type_bonus_attributes(type_id,attribute_id,value) VALUES(?,?,?)"
        " ON CONFLICT(type_id,attribute_id) DO UPDATE SET value=excluded.value"),
    "dogma_attributes": (
        "INSERT INTO dogma_attributes(attribute_id,name,display_name,unit_id) VALUES(?,?,?,?)"
        " ON CONFLICT(attribute_id) DO UPDATE SET name=excluded.name,"
        " display_name=excluded.display_name, unit_id=excluded.unit_id"),
    "modifier_sources": (
        "INSERT INTO industry_modifier_sources(type_id,activity,kind,attribute_id,filter_id)"
        " VALUES(?,?,?,?,?)"
        " ON CONFLICT(type_id,activity,kind,attribute_id,filter_id) DO NOTHING"),
    "target_filters": (
        "INSERT INTO target_filters(filter_id,name,category_ids,group_ids,type_ids)"
        " VALUES(?,?,?,?,?) ON CONFLICT(filter_id) DO UPDATE SET name=excluded.name,"
        " category_ids=excluded.category_ids, group_ids=excluded.group_ids,"
        " type_ids=excluded.type_ids"),
}

TABLES = ["types", "groups", "categories", "market_groups", "meta_groups",
          "blueprint_activities", "activity_materials", "activity_products",
          "activity_skills", "type_materials", "type_bonus_attributes",
          "dogma_attributes", "industry_modifier_sources", "target_filters",
          "solar_systems", "regions"]

BATCH = 4000


class _Counted(io.RawIOBase):
    """统计已读字节数，用于进度显示。"""

    def __init__(self, fh):
        self._fh = fh
        self.count = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self._fh.read(len(buffer))
        if not chunk:
            return 0
        buffer[:len(chunk)] = chunk
        self.count += len(chunk)
        return len(chunk)


def _loc(value, langs=("zh", "en")):
    """从 SDE 的多语言字段里取名字。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    for lang in langs:
        if value.get(lang):
            return value[lang]
    for lang in ("en", "de", "fr", "ru", "ja", "ko", "es"):
        if value.get(lang):
            return value[lang]
    return next(iter(value.values()), None)


class SDE:
    def __init__(self, store, config: dict):
        self.store = store
        self.config = config
        self._lock = threading.RLock()
        self._running = False
        self._progress = {"stage": "idle", "message": "", "percent": 0,
                          "detail": "", "started": None, "finished": None,
                          "error": None, "build": None}
        self._cancel = False

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def busy(self) -> bool:
        with self._lock:
            return self._running

    def progress(self) -> dict:
        with self._lock:
            return dict(self._progress)

    def _set(self, **kwargs) -> None:
        with self._lock:
            self._progress.update(kwargs)

    def status(self) -> dict:
        stats = self.store.sde_stats()
        local = self.store.sde_build() or {}
        prog = self.progress()
        return {
            "ready": self.store.sde_ready(),
            "running": self.busy(),
            "local_build": local.get("build_number"),
            "local_release": local.get("release_date"),
            "imported_at": local.get("imported_at"),
            "source": local.get("source"),
            "stats": stats,
            "progress": prog,
        }

    # ------------------------------------------------------------------
    # 远端版本
    # ------------------------------------------------------------------
    def latest_build(self) -> dict:
        req = urllib.request.Request(
            cfgmod.SDE_LATEST_URL,
            headers={"User-Agent": self.config.get("user_agent") or "eve-industry-local",
                     "Accept": "application/jsonlines+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            for line in io.TextIOWrapper(resp, encoding="utf-8"):
                record = utils.jloads(line, None)
                if isinstance(record, dict) and record.get("_key") == "sde":
                    return {"build_number": record.get("buildNumber"),
                            "release_date": record.get("releaseDate")}
        raise RuntimeError("无法从 SDE 的 latest.jsonl 解析版本号")

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def download(self, url: str = None, force: bool = False) -> str:
        url = url or self.config.get("sde_url") or cfgmod.SDE_ZIP_URL
        dest = os.path.join(cfgmod.SDE_DIR, "sde-latest-jsonl.zip")
        utils.ensure_dir(os.path.dirname(dest))
        req = urllib.request.Request(url, headers={
            "User-Agent": self.config.get("user_agent") or "eve-industry-local"})
        started = time.time()
        with urllib.request.urlopen(req, timeout=180) as resp:
            total = utils.as_int(resp.headers.get("Content-Length"), 0)
            done = 0
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    percent = int(done * 100 / total) if total else 0
                    speed = done / 1048576 / max(time.time() - started, 0.01)
                    self._set(stage="download", percent=max(1, min(percent, 99)),
                              message="正在下载 SDE",
                              detail="%s / %s  (%.1f MB/s)" % (
                                  utils.size_human(done), utils.size_human(total), speed))
        self._set(stage="download", percent=100, message="SDE 下载完成",
                  detail=utils.size_human(os.path.getsize(dest)))
        return dest

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------
    def import_zip(self, zip_path: str, source: str = "ccp-sde") -> dict:
        langs = tuple(self.config.get("sde_languages") or ["zh", "en"])
        zf = zipfile.ZipFile(zip_path)
        names = set(zf.namelist())
        total_bytes = sum(i.file_size for i in zf.infolist())
        processed = [0]
        started = time.time()

        def stage(label, name):
            self._set(stage=name, message=label,
                      percent=int(processed[0] * 100 / max(total_bytes, 1)),
                      detail=name)

        conn = self.store.conn()
        # 单事务：导入期间其它连接读到的仍是旧数据
        conn.execute("BEGIN IMMEDIATE")
        counts = {}
        try:
            for table in TABLES:
                conn.execute("DELETE FROM %s" % table)

            # ---------- categories / groups（先建好，types 要引用 category） ----------
            stage("导入 分类", "categories")
            rows = []
            for record in self._iter(zf, "categories.jsonl", processed):
                rows.append((record.get("_key"), _loc(record.get("name"), langs),
                             _loc(record.get("name"), ("en",)), 1 if record.get("published") else 0))
            self._flush(conn, "categories", rows)
            counts["categories"] = len(rows)

            stage("导入 物品分组", "groups")
            group_category = {}
            rows = []
            for record in self._iter(zf, "groups.jsonl", processed):
                gid = record.get("_key")
                cid = record.get("categoryID")
                group_category[gid] = cid
                rows.append((gid, _loc(record.get("name"), langs), _loc(record.get("name"), ("en",)),
                             cid, 1 if record.get("published") else 0))
            self._flush(conn, "groups", rows)
            counts["groups"] = len(rows)

            stage("导入 市场分组", "market_groups")
            rows = []
            for record in self._iter(zf, "marketGroups.jsonl", processed):
                rows.append((record.get("_key"), _loc(record.get("name"), langs),
                             _loc(record.get("name"), ("en",)), record.get("parentGroupID"),
                             1 if record.get("hasTypes") else 0, record.get("iconID")))
            self._flush(conn, "market_groups", rows)
            counts["market_groups"] = len(rows)

            stage("导入 科技等级", "meta_groups")
            rows = []
            for record in self._iter(zf, "metaGroups.jsonl", processed):
                rows.append((record.get("_key"), _loc(record.get("name"), langs),
                             _loc(record.get("name"), ("en",))))
            self._flush(conn, "meta_groups", rows)
            counts["meta_groups"] = len(rows)

            # ---------- 物品类型（最大的文件） ----------
            if "types.jsonl" in names:
                stage("导入 物品类型（数据量最大，请稍候）", "types")
                rows = []
                for record in self._iter(zf, "types.jsonl", processed):
                    tid = record.get("_key")
                    gid = record.get("groupID")
                    rows.append((
                        tid,
                        _loc(record.get("name"), langs),
                        _loc(record.get("name"), ("en",)),
                        gid,
                        group_category.get(gid),
                        record.get("marketGroupID"),
                        record.get("metaGroupID"),
                        record.get("techLevel"),
                        utils.num(record.get("volume")),
                        utils.num(record.get("packagedVolume")),
                        utils.as_int(record.get("portionSize"), 1),
                        utils.num(record.get("basePrice")),
                        1 if record.get("published") else 0,
                        record.get("iconID"),
                    ))
                    if len(rows) >= BATCH:
                        self._flush(conn, "types", rows)
                        rows = []
                self._flush(conn, "types", rows)
                counts["types"] = conn.execute("SELECT COUNT(*) FROM types").fetchone()[0]

            # ---------- 蓝图活动 ----------
            stage("导入 蓝图与工业配方", "blueprints")
            act_rows, mat_rows, prod_rows, skill_rows = [], [], [], []
            for record in self._iter(zf, "blueprints.jsonl", processed):
                bp_id = record.get("blueprintTypeID") or record.get("_key")
                limit = utils.as_int(record.get("maxProductionLimit"), 1)
                for activity, payload in (record.get("activities") or {}).items():
                    payload = payload or {}
                    act_rows.append((bp_id, activity, utils.as_int(payload.get("time"), 0), limit))
                    for material in payload.get("materials") or []:
                        mat_rows.append((bp_id, activity, material.get("typeID"),
                                         utils.as_int(material.get("quantity"))))
                    for product in payload.get("products") or []:
                        prod_rows.append((bp_id, activity, product.get("typeID"),
                                          utils.as_int(product.get("quantity"), 1),
                                          product.get("probability")))
                    for skill in payload.get("skills") or []:
                        skill_rows.append((bp_id, activity, skill.get("typeID"),
                                           utils.as_int(skill.get("level"), 1)))
                if len(mat_rows) >= BATCH:
                    self._flush(conn, "blueprint_activities", act_rows); act_rows = []
                    self._flush(conn, "activity_materials", mat_rows); mat_rows = []
                    self._flush(conn, "activity_products", prod_rows); prod_rows = []
                    self._flush(conn, "activity_skills", skill_rows); skill_rows = []
            self._flush(conn, "blueprint_activities", act_rows)
            self._flush(conn, "activity_materials", mat_rows)
            self._flush(conn, "activity_products", prod_rows)
            self._flush(conn, "activity_skills", skill_rows)
            counts["blueprints"] = conn.execute(
                "SELECT COUNT(*) FROM blueprint_activities WHERE activity='manufacturing'").fetchone()[0]
            counts["materials"] = conn.execute("SELECT COUNT(*) FROM activity_materials").fetchone()[0]

            # ---------- 精炼产出 ----------
            stage("导入 精炼产出", "type_materials")
            rows = []
            for record in self._iter(zf, "typeMaterials.jsonl", processed):
                tid = record.get("_key")
                for material in record.get("materials") or []:
                    rows.append((tid, material.get("materialTypeID"),
                                 utils.as_int(material.get("quantity"))))
                if len(rows) >= BATCH:
                    self._flush(conn, "type_materials", rows); rows = []
            self._flush(conn, "type_materials", rows)

            # ---------- 加成来源（先读，它决定了要抓哪些属性） ----------
            # 结构与改装件的加成机制（已对照官方数据核实）：
            #   * industryModifierSources 声明「某类型在某活动下，哪些 kind 生效、作用目标 filterID 是谁」
            #   * 结构自身的倍率在 2600/2601/2602（材料/费用/时间），反应在 2721
            #   * 改装件的减免百分点在 2593/2594/2595（时间/材料/费用），且按 2355/2356/2357 的安等倍率放大
            stage("导入 加成来源", "modifier_sources")
            rows = []
            needed_attrs = set(cfgmod.NEEDED_DOGMA_ATTRIBUTES)
            for record in self._iter(zf, "industryModifierSources.jsonl", processed):
                tid = record.get("_key")
                for activity, kinds in record.items():
                    if activity in ("_key", "blueprintTypeID") or not isinstance(kinds, dict):
                        continue
                    for kind, entries in kinds.items():
                        for entry in entries or []:
                            aid = entry.get("dogmaAttributeID")
                            if aid:
                                rows.append((tid, activity, kind, aid, entry.get("filterID")))
                                needed_attrs.add(aid)
            self._flush(conn, "modifier_sources", rows)
            counts["modifier_sources"] = len(rows)
            counts["modifier_attribute_ids"] = len(needed_attrs)

            # ---------- 加成作用目标 ----------
            stage("导入 加成作用目标", "target_filters")
            rows = []
            for record in self._iter(zf, "industryTargetFilters.jsonl", processed):
                rows.append((record.get("_key"), record.get("name"),
                             utils.jdumps(record.get("categoryIDs") or []),
                             utils.jdumps(record.get("groupIDs") or []),
                             utils.jdumps(record.get("typeIDs") or [])))
            self._flush(conn, "target_filters", rows)
            counts["target_filters"] = len(rows)

            # ---------- 工业加成属性值 ----------
            # 只保留 industryModifierSources 真正引用到的属性，避免导入 27 MB 的全部 dogma
            stage("导入 建筑/改装件加成属性", "bonus_attributes")
            rows = []
            if "typeDogma.jsonl" in names:
                for record in self._iter(zf, "typeDogma.jsonl", processed):
                    tid = record.get("_key")
                    for attribute in record.get("dogmaAttributes") or []:
                        aid = attribute.get("attributeID")
                        if aid in needed_attrs:
                            rows.append((tid, aid, utils.num(attribute.get("value"))))
                    if len(rows) >= BATCH:
                        self._flush(conn, "bonus_attributes", rows); rows = []
                self._flush(conn, "bonus_attributes", rows)
            counts["bonus_attributes"] = conn.execute(
                "SELECT COUNT(*) FROM type_bonus_attributes").fetchone()[0]

            # ---------- 属性名（便于界面展示） ----------
            stage("导入 属性名称", "dogma_attributes")
            rows = []
            if "dogmaAttributes.jsonl" in names:
                for record in self._iter(zf, "dogmaAttributes.jsonl", processed):
                    aid = record.get("_key")
                    if aid in needed_attrs:
                        rows.append((aid, record.get("name"),
                                     _loc(record.get("displayName"), langs),
                                     record.get("unitID")))
            self._flush(conn, "dogma_attributes", rows)
            counts["dogma_attributes"] = len(rows)

            # ---------- 星系 / 星域 ----------
            stage("导入 星系与安等", "solar_systems")
            rows = []
            for record in self._iter(zf, "mapSolarSystems.jsonl", processed):
                rows.append((record.get("_key"), _loc(record.get("name"), langs),
                             _loc(record.get("name"), ("en",)),
                             utils.num(record.get("securityStatus")),
                             record.get("regionID"), record.get("constellationID")))
                if len(rows) >= BATCH:
                    self._flush(conn, "solar_systems", rows); rows = []
            self._flush(conn, "solar_systems", rows)

            stage("导入 星域", "regions")
            rows = []
            for record in self._iter(zf, "mapRegions.jsonl", processed):
                rows.append((record.get("_key"), _loc(record.get("name"), langs),
                             _loc(record.get("name"), ("en",))))
            self._flush(conn, "regions", rows)

            # ---------- 版本信息 ----------
            build = {}
            if "_sde.jsonl" in names:
                for record in self._iter(zf, "_sde.jsonl", processed):
                    build = {"build_number": record.get("buildNumber"),
                             "release_date": record.get("releaseDate")}
                    break
            build["imported_at"] = utils.now()
            build["source"] = source
            build["elapsed"] = round(time.time() - started, 1)
            conn.execute("INSERT INTO meta(k,v) VALUES('sde_build',?) "
                         "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                         (utils.jdumps(build),))
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

        counts["elapsed"] = round(time.time() - started, 1)
        counts["build"] = build
        return counts

    # ------------------------------------------------------------------
    def _iter(self, zf: zipfile.ZipFile, name: str, processed=None):
        if name not in zf.namelist():
            utils.log("SDE 中缺少 %s，跳过", name)
            return
        base = processed[0] if processed is not None else 0
        with zf.open(name) as raw:
            counted = _Counted(raw)
            stream = io.TextIOWrapper(counted, encoding="utf-8")
            last_report = time.time()
            for line in stream:
                if self._cancel:
                    raise RuntimeError("SDE 导入已取消")
                try:
                    yield json.loads(line)
                except Exception:
                    continue
                if processed is not None:
                    now = time.time()
                    if now - last_report > 0.5:
                        last_report = now
                        processed[0] = base + counted.count
            if processed is not None:
                processed[0] = base + counted.count

    def _flush(self, conn, key: str, rows) -> None:
        if not rows:
            return
        conn.executemany(SQL[key], rows)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def update(self, force: bool = False, source: str = None) -> dict:
        with self._lock:
            if self._running:
                return {"ok": False, "message": "已有导入任务在运行中"}
            self._running = True
            self._cancel = False
        self._set(stage="check", percent=0, message="检查 SDE 版本", detail="",
                  started=utils.now(), finished=None, error=None)
        try:
            local = self.store.sde_build() or {}
            latest = {}
            try:
                latest = self.latest_build()
            except Exception as exc:
                utils.log("查询 SDE 最新版本失败：%r", exc)
            self._set(build=latest)
            if (not force and latest.get("build_number")
                    and local.get("build_number") == latest.get("build_number")
                    and self.store.sde_ready()):
                self._set(stage="done", percent=100, message="SDE 已是最新（%s）" % latest.get("build_number"),
                          finished=utils.now())
                return {"ok": True, "up_to_date": True, "build": local,
                        "message": "SDE 已是最新版本，无需更新"}

            zip_path = self.download(source or self.config.get("sde_url"))
            self._set(stage="import", percent=0, message="正在解析并导入 SDE", detail="")
            counts = self.import_zip(zip_path, source="ccp-sde")
            self.store.kv_set("sde_checked_at", utils.now())
            self._set(stage="done", percent=100,
                      message="SDE 导入完成（版本 %s，用时 %s 秒）" % (
                          (counts.get("build") or {}).get("build_number"), counts.get("elapsed")),
                      finished=utils.now(), error=None)
            return {"ok": True, "counts": counts, "build": counts.get("build")}
        except BaseException as exc:
            utils.log("SDE 更新失败：%r", exc)
            self._set(stage="error", message="SDE 更新失败", error=str(exc), finished=utils.now())
            return {"ok": False, "message": str(exc)}
        finally:
            with self._lock:
                self._running = False

    def cancel(self) -> None:
        self._cancel = True
