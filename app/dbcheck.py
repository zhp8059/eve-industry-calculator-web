"""数据库健康自检与自动隔离（供 app.main 在启动时调用）。

用户已三次遇到 `database disk image is malformed`。手工删 data/industry.db 太麻烦，
这里做成：启动先自检，坏了就把坏库**改名保留**，程序用新库继续启动。
"""
import os
import shutil
import sqlite3
import time

KEEP_SUFFIX = ("", "-wal", "-shm")


def _log(msg, *args):
    try:
        from app import utils
        utils.log(msg, *args)
    except Exception:
        pass


def _quarantine(path: str) -> str:
    """把坏库（含 -wal / -shm）改名保留，返回新的坏库路径前缀。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = "%s.bad-%s" % (path, stamp)
    for suf in KEEP_SUFFIX:
        src = path + suf
        if os.path.isfile(src):
            try:
                shutil.move(src, base + suf)
            except Exception as exc:
                _log("隔离损坏文件失败 %s：%s", src, exc)
    return base


def ensure_healthy_db(path: str = None) -> dict:
    """自检并在必要时隔离坏库。返回 {ok, action, quarantined, detail}。"""
    out = {"ok": True, "action": "none", "quarantined": None, "detail": ""}
    if not path:
        try:
            from app import config as cfgmod
            path = cfgmod.DB_PATH
        except Exception:
            return out
    if not os.path.isfile(path):
        return out                      # 还没建库：正常

    detail = ""
    healthy = False
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            detail = str(row[0] if row else "")
            healthy = (detail.lower() == "ok")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        detail = "打开/自检即失败：%s" % exc
        healthy = False
    except Exception as exc:
        detail = "自检异常（按健康处理）：%s" % exc
        healthy = True                  # 非损坏类异常不隔离，避免误伤

    if healthy:
        return out

    _log("⚠ 数据库自检未通过（%s）→ 自动隔离坏库并新建", detail[:120])
    q = _quarantine(path)
    out.update({"ok": False, "action": "quarantined", "quarantined": q, "detail": detail})
    try:
        d = os.path.dirname(path)
        rpt = os.path.join(d, "logs")
        os.makedirs(rpt, exist_ok=True)
        with open(os.path.join(rpt, "DB-损坏-%s.txt" % time.strftime("%Y%m%d-%H%M%S")),
                  "w", encoding="utf-8") as fh:
            fh.write("数据库损坏，已自动隔离。\n\n"
                     "自检结果：%s\n坏库已改名保留为：%s\n\n"
                     "接下来请：\n"
                     "1) 到「设置」重新导入最新 SDE\n"
                     "2) 重新登录一次（令牌在库里）\n"
                     "3) 到「价格对比」点「刷新物价」\n\n"
                     "损坏的旧文件可以留着排查，确认没问题后再删除。\n"
                     % (detail[:2000], q))
    except Exception:
        pass
    return out


def banner() -> str:
    """若本次启动隔离过坏库，返回给界面显示的提示文案（否则空串）。"""
    try:
        from app import config as cfgmod
        d = os.path.dirname(cfgmod.DB_PATH)
        if not os.path.isdir(d):
            return ""
        bad = [f for f in os.listdir(d) if ".bad-" in f and f.endswith(tuple(KEEP_SUFFIX) or ("",))]
        if bad:
            return ("检测到数据库损坏，已自动备份为 %s 并新建空库。"
                    "请重新导入 SDE 并重新登录。" % bad[0])
    except Exception:
        pass
    return ""
