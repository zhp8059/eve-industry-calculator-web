#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对着 ESI 的**实时**规范核对授权范围（scopes）。

用途：CCP 会调整接口所需的 scope。这个脚本会：

  1. 拉取 https://esi.evetech.net/meta/openapi.json
  2. 检查我们登记的每一项 scope 是否仍然存在
  3. 扫出程序里实际调用的 ESI 路径，检查每个**需授权**的接口
     是否都被我们登记的 scope 覆盖（缺一个就会 403）
  4. 报告「登记了但当前没用上」的 scope（可能对应可选功能，也可能是冗余）

只依赖标准库。用法：

    python tools/verify_scopes.py            # 联网核对
    python tools/verify_scopes.py --json     # 输出 JSON（便于脚本消费）

退出码：0 = 全部通过；1 = 有问题（缺 scope / 路径未覆盖）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app import config as cfgmod      # noqa: E402

SPEC_URL = "https://esi.evetech.net/meta/openapi.json"
SOURCE_FILES = ("esi.py", "inventory.py", "market.py", "server.py", "industry.py", "sso.py")
PATH_RE = re.compile(
    r"""["'](/[a-z0-9_{}%./\-]*(?:characters|corporations|universe|markets|industry"""
    r"""|skills|location|alliances|route|status)[a-z0-9_{}%./\-]*)["']""")


def fetch_spec(url: str = SPEC_URL) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "eve-industry-local"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def scopes_of(op: dict) -> list:
    out = []
    for block in (op.get("security") or []):
        for _name, values in (block or {}).items():
            out.extend(values or [])
    return out


def normalise(path: str) -> str:
    """把我们的 f-string / %d 路径归一成规范里的 {param} 形式。"""
    path = str(path).split("?")[0].rstrip("/")
    path = re.sub(r"\{[a-zA-Z_]+\}", "{x}", path)
    path = re.sub(r"%[ds]", "{x}", path)
    return path


def collect_calls() -> set:
    """从源码里扫出所有 ESI 路径模板。"""
    found = set()
    for name in SOURCE_FILES:
        full = os.path.join(ROOT, "app", name)
        if not os.path.exists(full):
            continue
        with open(full, encoding="utf-8") as fh:
            found.update(PATH_RE.findall(fh.read()))
    return found


def verify(spec: dict) -> dict:
    paths = spec.get("paths") or {}
    spec_scopes = set()
    for _p, ops in paths.items():
        for _m, op in ops.items():
            if isinstance(op, dict):
                spec_scopes.update(scopes_of(op))

    by_norm = {}
    for real in paths:
        by_norm.setdefault(normalise(real), real)

    missing = [s for s in cfgmod.ALL_SCOPES if s not in spec_scopes]

    calls, secured, uncovered, public = [], [], [], []
    for raw in sorted(collect_calls()):
        real = by_norm.get(normalise(raw))
        if not real:
            continue
        need = []
        for method, op in paths[real].items():
            if method == "get" and isinstance(op, dict):
                need = scopes_of(op)
        calls.append({"call": raw, "spec_path": real, "scopes": need})
        if need:
            secured.append(raw)
            if not set(need) & set(cfgmod.ALL_SCOPES):
                uncovered.append({"call": raw, "needs": need})
        else:
            public.append(raw)

    used = set()
    for item in calls:
        used.update(item["scopes"])
    unused = sorted(set(cfgmod.ALL_SCOPES) - used)

    return {
        "spec_version": (spec.get("info") or {}).get("version"),
        "spec_scope_count": len(spec_scopes),
        "registered": list(cfgmod.ALL_SCOPES),
        "missing_from_spec": missing,
        "calls": calls,
        "secured_calls": len(secured),
        "public_calls": len(public),
        "uncovered_calls": uncovered,
        "registered_but_unused": unused,
        "ok": not missing and not uncovered,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="核对 ESI 授权范围（scopes）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--url", default=SPEC_URL, help="规范地址")
    args = parser.parse_args(argv)

    try:
        spec = fetch_spec(args.url)
    except Exception as exc:                      # 网络问题不该报「权限有问题」
        print("拉取 ESI 规范失败：%s" % exc)
        print("（这是网络问题，不代表 scope 有问题）")
        return 2

    result = verify(spec)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    print("ESI 规范版本 %s，共 %d 个 scope" % (result["spec_version"],
                                              result["spec_scope_count"]))
    print()
    print("① 登记的 %d 项 scope 是否都存在" % len(result["registered"]))
    if result["missing_from_spec"]:
        for s in result["missing_from_spec"]:
            print("   ✗ %s —— 规范里没有！" % s)
    else:
        print("   ✓ 全部存在")
    print()
    print("② 程序调用：%d 个需授权接口、%d 个公开接口"
          % (result["secured_calls"], result["public_calls"]))
    for item in result["calls"]:
        if item["scopes"]:
            print("   %-46s → %s" % (item["call"], "、".join(item["scopes"])))
    if result["uncovered_calls"]:
        print()
        print("   ✗ 以下接口所需的 scope **没登记**（会 403）：")
        for item in result["uncovered_calls"]:
            print("      %s 需要 %s" % (item["call"], "、".join(item["needs"])))
    print()
    print("③ 登记了但当前代码没用上（可选功能 / 冗余）")
    for s in result["registered_but_unused"]:
        print("   · %s" % s)
    print()
    print("结论：%s" % ("全部通过 ✓" if result["ok"] else "有问题，见上 ✗"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
