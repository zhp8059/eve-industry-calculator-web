"""单实例保护（同一份数据库只能被一个进程写）。"""
import os

_lock_handle = None          # 必须保持引用，锁才不会被 GC 释放


def _msgbox(text, title="EVE 工业计算器"):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)
    except Exception:
        pass


def _log(msg, *args):
    try:
        from app import utils
        utils.log(msg, *args)
    except Exception:
        pass


def acquire(lock_path: str = None) -> dict:
    """尝试独占。返回 {ok, pid, path}；ok=False 表示已有实例在运行。"""
    global _lock_handle
    if lock_path is None:
        try:
            from app import config as cfgmod
            lock_path = os.path.join(os.path.dirname(cfgmod.DB_PATH), "app.lock")
        except Exception:
            return {"ok": True, "pid": None, "path": None}
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "a+", encoding="utf-8")
    except Exception as exc:
        _log("单实例锁不可用（跳过保护）：%s", exc)
        return {"ok": True, "pid": None, "path": lock_path}

    try:
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)      # 抢不到会抛 OSError
    except OSError:
        pid = ""
        try:
            fh.seek(0)
            pid = (fh.read() or "").strip()
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass
        return {"ok": False, "pid": pid, "path": lock_path}
    except Exception as exc:
        _log("单实例锁异常（跳过保护）：%s", exc)
        return {"ok": True, "pid": None, "path": lock_path}

    _lock_handle = fh                # 保持引用 → 锁随进程存活
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except Exception:
        pass
    return {"ok": True, "pid": os.getpid(), "path": lock_path}


def guard_or_exit() -> bool:
    """供启动入口调用：已在运行则弹窗+日志+退出。"""
    r = acquire()
    if r.get("ok"):
        return True
    pid = r.get("pid") or "未知"
    text = ("EVE 工业计算器已经在运行了（进程号 %s）。\n\n"
            "同时开两份会同时写同一个数据库文件，容易把数据库写坏\n"
            "（你已经遇到过三次 database disk image is malformed）。\n\n"
            "请先用「停止.bat」停掉正在运行的那一份，再启动这个。" % pid)
    _log("✗ 已有实例在运行（pid=%s），本次启动被拒绝，避免两个实例同时写库", pid)
    _msgbox(text)
    print("[!] 已有实例在运行（pid=%s），已退出。请先运行 停止.bat。" % pid)
    return False
