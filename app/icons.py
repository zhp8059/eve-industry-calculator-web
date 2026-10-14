"""物品图标的本地缓存（原生版与网页版都可用）。

为什么需要：以前每次开界面都要向 images.evetech.net 逐个取图标 —— 慢、且离线不可用。
现在**下载一次就落盘**到 `data/icons/<type_id>_<size>.png`，之后直接读本地文件。

实测 CCP 图片服务器的两种变体（同一 type_id 只有一种能取到）：

    普通物品：/icon = 200，/bp = 400
    蓝图类  ：/icon = 400，/bp = 200      ← 蓝图没有 icon 图，只有 bp 渲染图

所以先试 icon、失败再试 bp，与网页版的兜底逻辑保持一致。

只依赖标准库（urllib + 文件系统）；GUI 相关的 tk.PhotoImage 由调用方传入 root。
"""

from __future__ import annotations

import os
import threading
import urllib.error
import urllib.request

BASE = "https://images.evetech.net/types/%d/%s?size=%d"
USER_AGENT = "eve-industry-local/1.0 (+local desktop app)"
VARIANTS = ("icon", "bp")


class IconCache:
    def __init__(self, directory: str):
        self.dir = os.path.abspath(directory)
        os.makedirs(self.dir, exist_ok=True)
        self._photos = {}          # (type_id, size) -> PhotoImage，避免被垃圾回收
        self._lock = threading.Lock()
        self._failed = set()

    # ------------------------------------------------------------------
    def path_for(self, type_id: int, size: int = 32) -> str:
        return os.path.join(self.dir, "%d_%d.png" % (int(type_id), int(size)))

    def has(self, type_id: int, size: int = 32) -> bool:
        path = self.path_for(type_id, size)
        return os.path.isfile(path) and os.path.getsize(path) > 0

    def cached_count(self) -> int:
        try:
            return len([f for f in os.listdir(self.dir) if f.endswith(".png")])
        except OSError:
            return 0

    # ------------------------------------------------------------------
    def download(self, type_id: int, size: int = 32, timeout: int = 15) -> str:
        """确保本地有这个图标；返回路径，失败返回空字符串。"""
        path = self.path_for(type_id, size)
        if self.has(type_id, size):
            return path
        key = (int(type_id), int(size))
        with self._lock:
            if key in self._failed:
                return ""
        for variant in VARIANTS:
            url = BASE % (int(type_id), variant, int(size))
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                if not data:
                    continue
                tmp = path + ".part"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)          # 原子替换，避免半截文件被当成缓存
                return path
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
                continue
        with self._lock:
            self._failed.add(key)
        return ""

    def prefetch(self, type_ids, size: int = 32) -> None:
        """后台把一批图标补齐（不阻塞界面）。"""
        def worker():
            for tid in type_ids:
                self.download(tid, size)
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    def photo(self, tkroot, type_id: int, size: int = 32, download: bool = True):
        """给 tkinter 用的 PhotoImage。

        download=False 时**只读本地缓存、绝不联网** —— 渲染表格必须用这个模式：
        否则每行一次同步下载（单个超时 15 秒）会让主线程卡死几分钟（真实事故：
        拉到 4127 件库存后窗口「未响应」）。缺的图标交给后台 prefetch + 回填。
        """
        key = (int(type_id), int(size))
        cached = self._photos.get(key)
        if cached is not None:
            return cached
        if not download and not self.has(type_id, size):
            return None
        path = self.download(type_id, size)
        if not path:
            return None
        try:
            import tkinter as tk
            image = tk.PhotoImage(master=tkroot, file=path)
        except Exception:
            return None
        self._photos[key] = image
        return image
