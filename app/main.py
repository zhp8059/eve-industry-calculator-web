"""启动入口。

用法：
    python -m app.main                 # 使用 config.json / 默认值启动
    python -m app.main --port 9000     # 换端口
    python -m app.main --no-browser    # 不自动打开浏览器
    python -m app.main --sde-update    # 启动前先更新 SDE（命令行用）
    python -m app.main --check         # 只做环境自检，不启动服务
"""

from __future__ import annotations

import argparse
import atexit
import os
import sys
import traceback

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "app"

from . import APP_NAME, __version__
from . import config as cfgmod
from . import secret_check, utils, server as servermod


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="eve-industry", description="%s v%s（本地版，欧服 Tranquility）" % (APP_NAME, __version__))
    parser.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认 8420")
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--quiet", action="store_true", help="减少日志输出")
    parser.add_argument("--sde-update", action="store_true", help="启动后立即在后台更新 SDE")
    parser.add_argument("--sde-import", metavar="ZIP", help="直接从本地 zip 导入 SDE")
    parser.add_argument("--check", action="store_true", help="只做环境自检后退出")
    parser.add_argument("--client-id", help="临时指定 EVE 应用的 client_id")
    parser.add_argument("--clear-cache", action="store_true", help="启动前清空 ESI 响应缓存")
    return parser.parse_args(argv)


def _shutdown_hooks(store=None):
    """无窗口运行时看不到控制台，所以：写 pid 文件、退出时写日志、尝试关库。"""
    import signal

    def _bye(*_a):
        try:
            utils.log("服务停止，已退出")
        except Exception:
            pass
        if store is not None:
            try:
                if hasattr(store, "close_all"):
                    store.close_all()
            except Exception:
                pass
        try:
            os.remove(getattr(utils, "_pid_path", "") or "")
        except Exception:
            pass

    atexit.register(_bye)
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            prev = signal.getsignal(sig)

            def _wrap(signum, frame, _prev=prev):
                _bye()
                if callable(_prev) and _prev not in (signal.SIG_DFL, signal.SIG_IGN):
                    _prev(signum, frame)
                else:
                    raise SystemExit(0)
            signal.signal(sig, _wrap)
        except Exception:
            pass



def _write_pid() -> str:
    """写 logs/server.pid，供「停止.bat」按进程号停止服务。"""
    try:
        path = os.path.join(utils.log_dir(), "server.pid")
        with open(path, "w", encoding="ascii", errors="replace") as fh:
            fh.write(str(os.getpid()))
        utils._pid_path = path
        utils.log("服务进程号 %s 已写入 logs/server.pid", os.getpid())
        return path
    except Exception as exc:
        utils.log("写 server.pid 失败（不影响运行）：%s", exc)
        return ""


def _write_port() -> str:
    """写出实际监听端口到 logs/server.port（供停止脚本用，避免写死 8420）。"""
    try:
        port = 8420
        argv = list(sys.argv)
        for i, a in enumerate(argv):
            if a == "--port" and i + 1 < len(argv):
                port = argv[i + 1]
            elif a.startswith("--port="):
                port = a.split("=", 1)[1]
        path = os.path.join(utils.log_dir(), "server.port")
        with open(path, "w", encoding="ascii", errors="replace") as fh:
            fh.write(str(port))
        return path
    except Exception as exc:
        utils.log("写 server.port 失败（不影响运行）：%s", exc)
        return ""


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.quiet:
        utils.set_quiet(True)

    cfgmod.ensure_dirs()
    config = cfgmod.load_config()
    if args.client_id:
        config["client_id"] = args.client_id
        cfgmod.save_config(config)

    if args.check:
        from .store import Store
        store = Store(cfgmod.DB_PATH)
        report = secret_check.full_report(store, config,
                                          bool(cfgmod.resolve_client_id(config)))
        utils.set_quiet(False)
        utils.log("%s v%s 环境自检", APP_NAME, __version__)
        for key, value in report["environment"].items():
            utils.log("  %-14s %s", key, value)
        utils.log("  数据库        %s（%.1f MB）", cfgmod.DB_PATH,
                  report["store"]["db_bytes"] / 1048576.0)
        utils.log("  SDE           %s", "已导入 build %s（%s 个物品）"
                  % (report["store"]["sde_build"], report["store"]["types"])
                  if report["store"]["sde_ready"] else "未导入")
        utils.log("  令牌加密      %s", report["encryption"].get("backend"))
        utils.log("  client_id     %s", config.get("client_id") or "未配置")
        utils.log("  兼容日期      %s", config.get("compatibility_date") or "（不发送该头）")
        utils.log("  数据目录      %s", cfgmod.DATA_DIR)
        if report["problems"]:
            utils.log("待处理：")
            for problem in report["problems"]:
                utils.log("  ⚠ %s", problem)
        else:
            utils.log("一切就绪。")
        return 0

    if args.clear_cache:
        from .store import Store
        store = Store(cfgmod.DB_PATH)
        utils.log("已清空 ESI 缓存：%s 条", store.cache_clear())

    if args.sde_import:
        from .store import Store
        from .sde import SDE
        store = Store(cfgmod.DB_PATH)
        sde = SDE(store, config)
        utils.log("从本地文件导入 SDE：%s", args.sde_import)
        counts = sde.import_zip(args.sde_import, source="local-zip")
        utils.log("导入完成：%s", counts)
        return 0

    server, app = servermod.create_server(config, args.host, args.port)

    # 实际监听端口可能与配置不同（--port）；SSO 回调地址必须与实际端口一致，
    # 否则授权完成后浏览器回不到本机服务。
    #
    # ⚠️ 只记在**运行时**（下划线键，save_config 不会落盘）。
    # 早期版本会把实际端口写进 config["port"]，于是「用 --port 换个端口跑一次 +
    # 随便保存一次设置」就把端口永久改了 —— 之后正常启动时端口与门户登记的
    # 回调地址不一致，登录会停在 EVE 的错误页而不返回。这是个真踩过的坑。
    actual_port = server.server_address[1]
    if cfgmod.runtime_port(config) != actual_port:
        utils.log("提示：本次以端口 %s 运行（配置里是 %s），已按实际端口生成 SSO 回调地址。",
                  actual_port, config.get("port"))
        utils.log("      请确认 EVE 应用里登记的回调地址是：%s",
                  cfgmod.callback_url({**config, cfgmod.RUNTIME_PORT_KEY: actual_port}))
        utils.log("      （这个临时端口**不会**写进配置文件）")
    config[cfgmod.RUNTIME_PORT_KEY] = actual_port
    app.config[cfgmod.RUNTIME_PORT_KEY] = actual_port

    if args.sde_update or not app.store.sde_ready():
        def _auto_sde():
            import time
            time.sleep(1.5)
            utils.log("检测到未导入 SDE，开始自动导入（约 95 MB，请稍候）…")
            result = app.sde.update()
            utils.log("SDE 自动导入结果：%s", result.get("message") or result)
        import threading
        threading.Thread(target=_auto_sde, daemon=True).start()

    host, port = server.server_address[:2]
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = "http://%s:%d/" % (display_host, port)
    utils.log("=" * 64)
    utils.log(" %s v%s", APP_NAME, __version__)
    utils.log(" 界面地址： %s", url)
    utils.log(" 数据目录： %s", cfgmod.DATA_DIR)
    utils.log(" 配置文件： %s", cfgmod.CONFIG_PATH)
    if not app.store.sde_ready():
        utils.log(" ⚠ 正在后台导入 SDE 静态数据（首次约需 1-3 分钟）")
    if not app.sso.configured():
        utils.log(" ⚠ 没有可用的 client_id（内置值被清空且配置里也没填），暂时无法登录 EVE")
        utils.log("   可在「设置 → EVE 应用凭据」填入自己的 Client ID，或恢复 app/config.py 的内置值")
    else:
        source = "内置" if app.sso.client_id_source == "builtin" else "自定义"
        utils.log(" EVE 应用： %s Client ID（登录无需再配置）", source)
    utils.log(" 按 Ctrl+C 停止")
    utils.log("=" * 64)

    if config.get("open_browser", True) and not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        utils.log("正在停止……")
    except Exception:
        utils.log("服务异常退出：\n%s", traceback.format_exc())
        return 1
    finally:
        try:
            server.shutdown()
            app.store.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    # 启动前先自检数据库：坏了就自动隔离（保留现场），程序用新库继续启动。
    # 必须在 main() 之前 —— 否则 Store 一 open 就抛 malformed。
    try:
        from app import dbcheck
        _r = dbcheck.ensure_healthy_db()
        if not _r.get("ok"):
            print("[!] 数据库损坏，已隔离为：%s" % _r.get("quarantined"))
    except Exception:
        pass
    # 作为程序启动时写 pid（跑测试不会走到这里，避免写入假 pid）
    try:
        _write_pid()
        try:
            _write_port()
        except Exception:
            pass
    except Exception:
        pass
    sys.exit(main())
