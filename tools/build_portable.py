"""构建「便携版」：把官方免安装的 Python 运行时打进项目，生成一个解压即用的包。

产出：
    runtime/                       内置 Python 运行时（之后 start.bat 会优先用它）
    build/eve-industry-portable.zip 可直接分发的便携包

原理：
    python.org 提供官方的 **embeddable package**（免安装、不写注册表、无需管理员权限），
    约 11 MB。把它解包到 runtime/ 并改一行 python3xx._pth 让它能找到本项目的 app 包，
    就得到一个不依赖系统 Python 的完整运行环境。

用法：
    python tools/build_portable.py                       # 默认：3.13.16 + 带上已导入的 SDE 数据库
    python tools/build_portable.py --no-data             # 不带数据库（首次启动时自行下载 SDE）
    python tools/build_portable.py --version 3.14.8
    python tools/build_portable.py --zip-only            # 只打包，不重新下载运行时
    python tools/build_portable.py --out D:\\x.zip
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.join(ROOT, "runtime")
BUILD_DIR = os.path.join(ROOT, "build")
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "industry.db")

DEFAULT_VERSION = "3.13.16"
DEFAULT_OUT = os.path.join(BUILD_DIR, "eve-industry-portable.zip")
PKG_NAME = "eve-industry-portable"

EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv", ".idea", ".vscode", "build"}
EXCLUDE_FILES = {"config.json"}
# data/ 里只带数据库，不带 SDE 压缩包（94 MB）与 ESI 缓存
DATA_KEEP = {"industry.db"}
CRLF_EXTS = {".bat", ".cmd"}


def log(msg=""):
    print(msg, flush=True)


# --------------------------------------------------------------------------
def embed_url(version: str) -> str:
    return "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (version, version)


def latest_embed_version() -> str:
    """从 python.org 的目录列表里找最新的、确实提供 embed-amd64.zip 的版本。"""
    import re
    req = urllib.request.Request("https://www.python.org/ftp/python/",
                                 headers={"User-Agent": "eve-industry-build"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        html = resp.read().decode("utf-8", "replace")
    versions = sorted({v for v in re.findall(r'href="(3\.\d+\.\d+)/"', html)},
                      key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    for version in versions:
        try:
            probe = urllib.request.Request(embed_url(version), method="HEAD",
                                          headers={"User-Agent": "eve-industry-build"})
            with urllib.request.urlopen(probe, timeout=25):
                return version
        except Exception:
            continue
    raise RuntimeError("没有找到可用的 embed-amd64.zip")


def download(url: str, dest: str) -> str:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
        log("   已有缓存：%s（%.2f MB）" % (dest, os.path.getsize(dest) / 1048576))
        return dest
    log("   下载 %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "eve-industry-build"})
    done = 0
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(1 << 18)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total and done % (4 << 20) < (1 << 18):
                log("      %5.1f%%  %.1f / %.1f MB" % (done * 100.0 / total,
                                                      done / 1048576, total / 1048576))
    log("   完成：%.2f MB" % (os.path.getsize(dest) / 1048576))
    return dest


def install_runtime(version: str, cache: str, force: bool = False) -> None:
    """解包 embeddable Python 到 runtime/，并修正 ._pth 的搜索路径。"""
    if os.path.isdir(RUNTIME_DIR):
        if not force:
            log("✔ runtime/ 已存在，跳过解包（要重建请加 --force）")
            return
        shutil.rmtree(RUNTIME_DIR)
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with zipfile.ZipFile(cache) as zf:
        zf.extractall(RUNTIME_DIR)

    pths = [n for n in os.listdir(RUNTIME_DIR) if n.endswith("._pth")]
    if not pths:
        raise RuntimeError("运行时里没有找到 python3xx._pth，无法修正搜索路径")
    pth_path = os.path.join(RUNTIME_DIR, pths[0])
    tag = pths[0][: -len("._pth")]                     # 例如 python313
    content = (
        "%s.zip\n" % tag
        + ".\n"
        + "..\n"                                        # .. = 项目根目录 → 能 import app
        + "\n# Uncomment to run site.main() automatically\n#import site\n"
    )
    with open(pth_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    log("✔ 已解包运行时到 runtime/（%d 个文件）" % len(os.listdir(RUNTIME_DIR)))
    log("   已把 .. 写入 %s，使 python 能找到本项目的 app 包" % pths[0])


def checkpoint_db() -> None:
    """把 WAL 合并回主库，保证复制出来的 industry.db 是完整自洽的。"""
    if not os.path.isfile(DB_PATH):
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=20)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        conn.close()
    except Exception as exc:
        log("   （检查点失败，忽略：%s）" % exc)


def selftest(version_expected: str = None) -> bool:
    """用内置运行时跑一次关键能力自检。"""
    import subprocess
    exe = os.path.join(RUNTIME_DIR, "python.exe")
    if not os.path.isfile(exe):
        log("   ✘ 找不到 runtime/python.exe")
        return False
    code = (
        "import sys, ssl, sqlite3, ctypes, hashlib, zipfile, urllib.request\n"
        "print('   Python      :', sys.version.split()[0])\n"
        "print('   OpenSSL     :', ssl.OPENSSL_VERSION)\n"
        "print('   SQLite      :', sqlite3.sqlite_version)\n"
        "import app.secure as s, app.config\n"
        "info = s.selftest()\n"
        "print('   DPAPI 加密  :', info.get('encrypted'), '/ 往返', info.get('roundtrip'))\n"
        "req = urllib.request.Request('https://esi.evetech.net/latest/status/',\n"
        "                             headers={'User-Agent':'build-selftest'})\n"
        "with urllib.request.urlopen(req, timeout=30) as r:\n"
        "    print('   ESI 连通    : HTTP', r.status)\n"
        "assert info.get('roundtrip'), 'DPAPI 往返失败'\n"
        "print('   SELFTEST_OK')\n"
    )
    result = subprocess.run([exe, "-X", "utf8", "-c", code], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180)
    out = (result.stdout or "") + (result.stderr or "")
    print(out.rstrip())
    return "SELFTEST_OK" in out


# --------------------------------------------------------------------------
def iter_files():
    """要打进便携包的源文件（不含 runtime/ 与 data/，这两块单独处理）。"""
    root = os.path.abspath(ROOT)
    skip = set(EXCLUDE_DIRS) | {"runtime", "data"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if name in EXCLUDE_FILES or name.endswith((".pyc", ".pyo", ".tmp", ".zip")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if any(part in skip for part in rel.split("/")):
                continue
            yield rel, full


def read_normalized(path: str) -> bytes:
    with open(path, "rb") as fh:
        raw = fh.read().replace(b"\r\n", b"\n")
    if os.path.splitext(path)[1].lower() in CRLF_EXTS:
        raw = raw.replace(b"\n", b"\r\n")
    return raw


def build_zip(out_path: str, include_data: bool) -> tuple:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    n_src = n_rt = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, full in iter_files():
            zf.writestr("%s/%s" % (PKG_NAME, rel), read_normalized(full))
            n_src += 1
        if os.path.isdir(RUNTIME_DIR):
            pth_fixed = None
            pths = [f for f in os.listdir(RUNTIME_DIR) if f.endswith("._pth")]
            if pths:
                pth_fixed = open(os.path.join(RUNTIME_DIR, pths[0]), "rb").read()
            for name in sorted(os.listdir(RUNTIME_DIR)):
                full = os.path.join(RUNTIME_DIR, name)
                if not os.path.isfile(full):
                    continue
                data = pth_fixed if name.endswith("._pth") and pth_fixed is not None \
                    else open(full, "rb").read()
                zf.writestr("%s/runtime/%s" % (PKG_NAME, name), data)
                n_rt += 1
        if include_data and os.path.isfile(DB_PATH):
            checkpoint_db()
            zf.write(DB_PATH, "%s/data/industry.db" % PKG_NAME)
    return n_src, n_rt, os.path.getsize(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建免安装的便携版")
    parser.add_argument("--version", default=DEFAULT_VERSION,
                        help="Python 版本（默认 %s）" % DEFAULT_VERSION)
    parser.add_argument("--latest", action="store_true",
                        help="自动选用 python.org 上最新提供 embed 包的版本")
    parser.add_argument("--force", action="store_true", help="重新下载并解包运行时")
    parser.add_argument("--no-data", action="store_true", help="不打包已导入的 SDE 数据库")
    parser.add_argument("--zip-only", action="store_true", help="只打包，不碰运行时")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出的 zip 路径")
    parser.add_argument("--no-selftest", action="store_true", help="跳过内置运行时自检")
    args = parser.parse_args()

    log("=" * 70)
    log(" 构建 EVE 工业计算器 · 便携版（内置免安装 Python）")
    log("=" * 70)
    log("")

    if not args.zip_only:
        version = latest_embed_version() if args.latest else args.version
        log("[1/4] 准备内置 Python 运行时（%s）" % version)
        cache = os.path.join(BUILD_DIR, "python-%s-embed-amd64.zip" % version)
        download(embed_url(version), cache)
        install_runtime(version, cache, force=args.force or args.latest)
    else:
        log("[1/4] --zip-only：沿用现有 runtime/")

    log("")
    log("[2/4] 校验内置运行时能力")
    exe = os.path.join(RUNTIME_DIR, "python.exe")
    if not os.path.isfile(exe):
        log("   ✘ runtime/python.exe 不存在，请先去掉 --zip-only 重新构建")
        return 2
    if args.no_selftest:
        log("   （已跳过）")
    elif not selftest():
        log("   ✘ 自检未通过，请检查上面的输出")
        return 3

    log("")
    log("[3/4] 打包（%s）" % ("含已导入的 SDE 数据库" if not args.no_data else "不含数据库"))
    n_src, n_rt, size = build_zip(args.out, include_data=not args.no_data)
    log("   ✔ %s" % args.out)
    log("     源码 %d 个 + 运行时 %d 个文件，共 %.1f MB" % (n_src, n_rt, size / 1048576))

    log("")
    log("[4/4] 完成")
    log("   把 %s 解压到任意目录（路径不要有中文以外的特殊字符），" % os.path.basename(args.out))
    log("   双击里面的 start.bat 即可运行 —— 不需要安装 Python。")
    log("")
    log("   解压后的目录结构：")
    log("     %s/" % PKG_NAME)
    log("       start.bat          ← 双击这个")
    log("       runtime/python.exe ← 内置 Python（start.bat 会优先使用它）")
    log("       app/ docs/ tests/ tools/")
    if not args.no_data:
        log("       data/industry.db   ← 已导入的静态数据（首次启动无需再下载 95 MB）")
    return 0



    # 若 CI 构建过「启动.exe」，一并放进便携包（没有则保留 启动.vbs 兜底）
    _exe = os.path.join(ROOT, "EVE-Launcher.exe")
    if not os.path.isfile(_exe):
        _exe = os.path.join(ROOT, "启动.exe")
    if os.path.isfile(_exe):
        shutil.copy2(_exe, os.path.join(dest, "启动.exe"))

if __name__ == "__main__":
    sys.exit(main())
