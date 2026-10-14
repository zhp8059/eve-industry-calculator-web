"""通用工具函数（仅标准库）。"""

from __future__ import annotations

import datetime as _dt
import os as _os
import sys as _sys
import json
import os
import sys as _sys
import tempfile
import threading
import time

_log_lock = threading.Lock()
_quiet = False


def set_quiet(flag: bool) -> None:
    global _quiet
    _quiet = flag


# --------------------------------------------------------------------------
# 日志文件（logs/ 文件夹）
# --------------------------------------------------------------------------
_os = os
_LOG_DIR = None
_crash_lock = _log_lock


def log_dir():
    """日志目录：程序根目录下的 logs/（便携包与开发目录都对）。"""
    global _LOG_DIR
    if _LOG_DIR is None:
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        _LOG_DIR = _os.path.join(root, "logs")
        try:
            _os.makedirs(_LOG_DIR, exist_ok=True)
        except Exception:
            pass
    return _LOG_DIR


def log_file():
    """当天日志文件路径（logs/app-YYYYMMDD.log）。"""
    return _os.path.join(log_dir(),
                         "app-%s.log" % _dt.datetime.now().strftime("%Y%m%d"))


def _write_file(line: str) -> None:
    """把一行同时写进日志文件。写不进去也不能让程序崩（只在 stdout 提示一次）。"""
    try:
        with _crash_lock:
            with open(log_file(), "a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def log_exception(where: str = "") -> str:
    """把当前异常（含堆栈）写进 logs/crash-*.log，返回该文件路径。"""
    import traceback
    path = _os.path.join(log_dir(),
                         "crash-%s.log" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    text = "".join(traceback.format_exc())
    try:
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write("时间: %s\n位置: %s\n\n%s\n"
                     % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), where, text))
    except Exception:
        return ""
    return path


def install_hooks() -> None:
    """装崩溃钩子：无窗口启动时，报错必须落到 logs/，否则用户什么都看不到。"""
    import threading

    def _hook(exc_type, exc, tb):
        path = log_exception("未捕获异常")
        log("✗ 未捕获异常：%r（详情见 %s）", exc, path or "（写文件失败）")
        if _sys.stderr:
            try:
                _sys.__excepthook__(exc_type, exc, tb)
            except Exception:
                pass

    _sys.excepthook = _hook
    try:
        threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass


def log(msg, *args) -> None:
    """带时间戳打印到 stdout（避免 GBK 控制台崩溃）。"""
    if args:
        try:
            msg = msg % args
        except Exception:
            msg = "%s %r" % (msg, args)
    if _quiet:
        return
    ts = _dt.datetime.now().strftime("%H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    _write_file(line)
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            enc = getattr(__import__("sys").stdout, "encoding", None) or "ascii"
            print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------

def now() -> int:
    return int(time.time())


def iso_utc(ts=None) -> str:
    t = time.time() if ts is None else float(ts)
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_esi_time(value):
    """解析 ESI 的 ISO8601 时间（如 2026-10-13T10:59:00Z）为 unix 秒。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp())


def fmt_duration(seconds) -> str:
    """把秒数格式化为「3天4小时5分」。"""
    if seconds is None:
        return "-"
    try:
        s = int(seconds)
    except Exception:
        return "-"
    if s < 0:
        s = 0
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return "%d天%d小时" % (d, h)
    if h:
        return "%d小时%d分" % (h, m)
    if m:
        return "%d分%d秒" % (m, sec)
    return "%d秒" % sec


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------

def jloads(text, default=None):
    if text is None:
        return default
    if isinstance(text, (dict, list, int, float, bool)):
        return text
    try:
        return json.loads(text)
    except Exception:
        return default


def jdumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------
# 数值 / 格式化
# --------------------------------------------------------------------------

def num(value, default=0.0):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except Exception:
        return default


def as_int(value, default=0):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return default


def isk(value, digits: int = 2) -> str:
    """千分位格式化，与参考站点一致（54,517,073,605.81）。"""
    if value is None:
        return "-"
    try:
        f = float(value)
    except Exception:
        return str(value)
    return format(f, ",.%df" % digits)


def compact(value) -> str:
    """把 ISK 压缩为带 亿/万 的短字符串，用于汇总卡片。"""
    try:
        f = float(value)
    except Exception:
        return "-"
    sign = "-" if f < 0 else ""
    a = abs(f)
    if a >= 1e12:
        return "%s%.2f 万亿" % (sign, a / 1e12)
    if a >= 1e8:
        return "%s%.2f 亿" % (sign, a / 1e8)
    if a >= 1e4:
        return "%s%.2f 万" % (sign, a / 1e4)
    return "%s%s" % (sign, format(a, ",.2f"))


def pct(value, digits: int = 2) -> str:
    try:
        return "%.*f%%" % (digits, float(value) * 100.0)
    except Exception:
        return "-"


def chunked(seq, size: int):
    items = list(seq)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def clamp(value, low, high):
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# 文件
# --------------------------------------------------------------------------

def ensure_dir(path: str) -> str:
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def atomic_write(path: str, data) -> None:
    """先写临时文件再替换，避免中断留下半个文件。"""
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def read_text(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return default


def write_json(path: str, obj) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: str, default=None):
    text = read_text(path)
    if text is None:
        return default
    parsed = jloads(text, None)
    return default if parsed is None else parsed


def size_human(nbytes) -> str:
    try:
        n = float(nbytes)
    except Exception:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n
