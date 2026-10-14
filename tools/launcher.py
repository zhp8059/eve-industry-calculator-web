"""EVE 工业计算器 · 启动器（被 PyInstaller 打成 启动.exe）。

只做一件事：用程序自带的 Python 运行时启动本地服务，然后自己退出。
* 不显示任何黑窗口（CREATE_NO_WINDOW + DETACHED_PROCESS）
* 找不到运行时时弹一个 Windows 消息框（窗口化 exe 没有控制台可打印）
* 自身动作记录到 logs\\launcher.log，方便排错
"""
import os
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def base_dir():
    """启动器所在目录（打包后 = 便携包根目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def note(text):
    try:
        d = os.path.join(base_dir(), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "launcher.log"), "a", encoding="utf-8", errors="replace") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def msgbox(text, title="EVE 工业计算器"):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


def pick_python(root):
    """优先自带运行时；其次 pythonw / python；最后 py 启动器。"""
    cands = [
        os.path.join(root, "runtime", "pythonw.exe"),
        os.path.join(root, "runtime", "python.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c, "内置运行时"
    for name in ("pythonw.exe", "python.exe"):
        for p in (os.environ.get("PATH") or "").split(os.pathsep):
            c = os.path.join(p.strip('"'), name)
            if os.path.isfile(c):
                return c, "系统 Python"
    return None, ""


def main():
    root = base_dir()
    note("[%s] 启动器运行，目录=%s" % (__import__("datetime").datetime.now()
                                     .strftime("%Y-%m-%d %H:%M:%S"), root))
    py, kind = pick_python(root)
    if not py:
        note("找不到 Python 运行时")
        msgbox("没有找到可用的 Python 运行时。\n\n"
               "请确认便携包里有 runtime\\python.exe，\n"
               "或安装 Python 3.8+（安装时勾选 Add python.exe to PATH）。")
        return 1
    if not os.path.isfile(os.path.join(root, "app", "main.py")):
        note("缺少 app\\main.py（目录不对？）")
        msgbox("这个启动器必须放在程序目录里（与 app、runtime 同级）。")
        return 1
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    try:
        subprocess.Popen([py, "-m", "app.main"], cwd=root, env=env,
                         creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                         close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        note("已启动：%s（%s），浏览器会自动打开" % (py, kind))
        return 0
    except Exception as exc:
        note("启动失败：%r" % exc)
        msgbox("启动失败：%s\n\n详情见 logs\\launcher.log" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
