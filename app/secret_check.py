"""启动自检：把「环境是否可用」的结论集中在一处，便于界面上直接展示。"""

from __future__ import annotations

import platform
import ssl
import sys

from . import APP_ID, __version__, secure


def scope_audit(config: dict) -> dict:
    """检查配置里请求的 scope 是否有「能代替用户做事」的高危项。"""
    from . import config as cfgmod
    requested = list(config.get("scopes") or cfgmod.ALL_SCOPES)
    risky = cfgmod.audit_scopes(requested)
    return {
        "requested": len(requested),
        "risky": risky,
        "clean": not risky,
        "default_clean": cfgmod.scope_audit_report()["clean"],
        "warning": (None if not risky else
                    "配置里的 scopes 包含 %d 项高危权限（%s）。本程序只需要读权限，"
                    "建议改回默认值。" % (len(risky), "、".join(risky))),
    }


def encryption_info() -> dict:
    """令牌落盘加密的状态。"""
    info = secure.selftest()
    backend = secure.backend()
    info["backend"] = {"dpapi": "Windows DPAPI（与当前 Windows 用户绑定）",
                       "aes-gcm": "AES-256-GCM（密钥来自 %s 或 data/token.key）"
                                  % secure.KEY_ENV,
                       "plaintext": "未加密（明文）"}.get(backend, backend)
    info["parseable"] = backend != "plaintext"
    info["warning"] = (None if backend != "plaintext"
                       else "当前没有可用的加密后端，刷新令牌会以明文保存。"
                            "请勿把 data 目录分享给他人，并检查 data 目录是否可写。")
    return info


def environment() -> dict:
    try:
        openssl = ssl.OPENSSL_VERSION
    except Exception as exc:  # pragma: no cover
        openssl = repr(exc)
    return {
        "app": APP_ID,
        "version": __version__,
        "python": sys.version.split()[0],
        "python_path": sys.executable,
        "platform": platform.platform(),
        "openssl": openssl,
        "dependencies": "仅使用 Python 标准库（无需 pip 安装）",
    }


def check_store(store) -> dict:
    """数据库与 SDE 的状态。"""
    sde = store.sde_build() or {}
    return {
        "db_ready": True,
        "db_bytes": store.size_bytes(),
        "sde_ready": store.sde_ready(),
        "sde_build": sde.get("build_number"),
        "sde_imported_at": sde.get("imported_at"),
        "types": (store.sde_stats() or {}).get("types"),
    }


def full_report(store, config: dict, sso_configured: bool) -> dict:
    """界面「关于 / 环境自检」用的一站式报告。"""
    problems = []
    if not store.sde_ready():
        problems.append("尚未导入 SDE 静态数据 —— 无法查询蓝图与材料（点「导入 SDE」）。")
    if not sso_configured:
        problems.append("没有可用的 client_id —— 无法登录（在「设置」填入自己的 Client ID 即可）。")
    encryption = encryption_info()
    if not encryption.get("encrypted"):
        problems.append("刷新令牌未加密存储（当前系统不支持 DPAPI）。")
    return {
        "environment": environment(),
        "store": check_store(store),
        "encryption": encryption,
        "esi_base": config.get("esi_base_hint"),
        "compatibility_date": config.get("compatibility_date"),
        "problems": problems,
        "ok": not problems,
    }
