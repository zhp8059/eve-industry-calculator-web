"""ESI HTTP 客户端（仅标准库 urllib）。

实现要点（严格按官方文档）：
  * base = https://esi.evetech.net/latest ，datasource=tranquility（欧服）
  * User-Agent 必带（官方要求，否则可能被封）
  * 缓存：先看 X-Expires，未过期直接用本地副本；过期则带 If-None-Match，
    收到 304 就复用本地副本（省 token、也避免被判定为绕过 ESI 缓存）
  * 限流：2XX=2 token / 3XX=1 / 4XX=5 / 5XX=0；429 与旧版 420 按 Retry-After 退避
  * 错误限额：监控 X-ESI-Error-Limit-Remain，过低时主动降速
  * 分页：按 X-Pages 头逐页抓取
"""

from __future__ import annotations

import email.utils
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config as cfgmod
from . import utils


def _error_message(text: str, status: int) -> str:
    """把 ESI 的错误响应压成一句人话。"""
    detail = ""
    data = utils.jloads(text, None)
    if isinstance(data, dict):
        detail = data.get("error_description") or data.get("error") or data.get("message") or ""
    elif text:
        detail = text.strip()[:200]
    hints = {
        400: "请求参数有误",
        401: "未授权：令牌无效或已过期",
        403: "禁止访问：缺少角色权限（军团数据需要 总监/工厂经理 等角色）或未授权该 scope",
        404: "资源不存在",
        422: "参数校验失败",
    }
    base = hints.get(status, "ESI 返回错误")
    return "%s%s" % (base, ("：" + detail) if detail else "")


class ESIError(Exception):
    def __init__(self, status, message, url=None, retry_after=None, body=None):
        super().__init__("ESI %s: %s" % (status, message))
        self.status = status
        self.message = message
        self.url = url
        self.retry_after = retry_after
        self.body = body

    def to_dict(self):
        return {"status": self.status, "message": self.message, "url": self.url}


class ESI:
    def __init__(self, store, config: dict):
        self.store = store
        self.config = config
        self._token_manager = None
        self._lock = threading.Lock()
        self._error_limit_remain = None
        self._error_limit_reset = None
        self._rate_remaining = None
        self._rate_limit = None
        self._rate_limit_group = None
        self._rate_until = {}         # character_id -> 该角色最早的可用时刻
        self._error_limit_until = 0.0  # 420：整个应用停到这个时刻
        self._last_request = 0.0
        self._min_interval = 0.0     # 由限流头动态调整
        self.stats = {"requests": 0, "cache_hits": 0, "not_modified": 0, "errors": 0,
                      "sleep_seconds": 0.0, "bytes": 0, "started": utils.now()}

    # ------------------------------------------------------------------
    def set_token_manager(self, manager) -> None:
        self._token_manager = manager

    # ------------------------------------------------------------------
    def _url(self, path: str, params: dict = None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = cfgmod.ESI_BASE + (path if path.startswith("/") else "/" + path)
        query = dict(params or {})
        query.setdefault("datasource", cfgmod.ESI_DATASOURCE)
        parts = urllib.parse.urlsplit(url)
        existing = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        merged = existing + [(k, str(v)) for k, v in query.items() if v is not None]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(merged), ""))

    def _headers(self, token=None, extra=None) -> dict:
        headers = {
            "User-Agent": self.config.get("user_agent") or "eve-industry-local/0.1",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        compatibility = self.config.get("compatibility_date", cfgmod.COMPATIBILITY_DATE)
        if compatibility:
            headers["X-Compatibility-Date"] = compatibility
        if token:
            headers["Authorization"] = token if token.lower().startswith("bearer ") else "Bearer " + token
        if extra:
            headers.update(extra)
        return headers

    # ------------------------------------------------------------------
    def _pace(self) -> None:
        """最简单的匀速节流：保证请求之间有最小间隔。"""
        with self._lock:
            now = time.time()
            wait = self._min_interval - (now - self._last_request)
            self._last_request = max(now, self._last_request + self._min_interval)
        if wait > 0:
            self.stats["sleep_seconds"] += wait
            time.sleep(min(wait, 5.0))

    def _expires_epoch(self, headers) -> int:
        raw = headers.get("Expires")
        if not raw:
            return 0
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt is None:
                return 0
            if dt.tzinfo is None:
                import datetime as _dt
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return 0

    def _track_limits(self, headers, character_id=None) -> None:
        remain = headers.get("X-ESI-Error-Limit-Remain")
        if remain is not None:
            try:
                self._error_limit_remain = int(remain)
                self._error_limit_reset = int(headers.get("X-ESI-Error-Limit-Reset") or 0)
                if self._error_limit_remain <= 5:
                    # 提前减速，而不是等 420 砸下来（阈值与 evesynapse 一致）
                    utils.log("⚠ ESI 错误限额仅剩 %s 次（%s 秒后重置），暂停 3 秒",
                              self._error_limit_remain, self._error_limit_reset)
                    time.sleep(3.0)
            except Exception:
                pass
        rate_remain = headers.get("X-Ratelimit-Remaining")
        if rate_remain is not None:
            try:
                self._rate_remaining = int(rate_remain)
                self._rate_limit = headers.get("X-Ratelimit-Limit")
                self._rate_limit_group = headers.get("X-Ratelimit-Group")
                # 余量不足时放慢到每 0.5 秒一次
                self._min_interval = 0.5 if self._rate_remaining < 20 else 0.0
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 限流状态
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_retry_after(raw, default: int = 60) -> int:
        """Retry-After 可能是秒数，也可能是 HTTP 日期。"""
        if raw is None:
            return default
        text = str(raw).strip()
        if text.isdigit():
            return max(0, int(text))
        try:
            parsed = email.utils.parsedate_to_datetime(text)
            if parsed is None:
                return default
            if parsed.tzinfo is None:
                import datetime as _dt
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return max(0, int(parsed.timestamp() - time.time()))
        except Exception:
            return default

    def note_rate_limited(self, character_id, headers) -> int:
        """429：**只停这一个角色**的预算，不是所有人。返回等待秒数。"""
        wait = self._parse_retry_after(headers.get("Retry-After"), 60)
        wait = min(max(wait, 1), 900)          # 上限 15 分钟：ESI 的窗口就是 15 分钟
        key = int(character_id) if character_id else 0
        with self._lock:
            until = time.time() + wait
            # 只延长、不缩短：并发请求里较短的那个不该把较长的限制解开
            if until > self._rate_until.get(key, 0):
                self._rate_until[key] = until
        utils.log("ESI 对角色 %s 限流，暂停该角色 %s 秒", character_id or "（未认证）", wait)
        return wait

    def note_error_limit(self, headers) -> int:
        """420：应用级错误预算被打穿 —— **停止一切请求**，等重置。"""
        wait = self._parse_retry_after(headers.get("Retry-After"), 60)
        reset = utils.as_int(headers.get("X-ESI-Error-Limit-Reset"), 0)
        wait = min(max(wait, reset or 60, 1), 900)
        with self._lock:
            self._error_limit_until = max(self._error_limit_until, time.time() + wait)
        utils.log("ESI 错误预算已打穿（420），全部请求暂停 %s 秒", wait)
        return wait

    def rate_limited_for(self, character_id=None) -> float:
        """该角色还要等多少秒；0 表示可以发。"""
        now = time.time()
        key = int(character_id) if character_id else 0
        with self._lock:
            until = max(self._rate_until.get(key, 0), self._error_limit_until)
            for stale in [k for k, v in self._rate_until.items() if v <= now]:
                self._rate_until.pop(stale, None)
        return max(0.0, until - now)

    def limits(self) -> dict:
        return {
            "error_limit_remain": self._error_limit_remain,
            "error_limit_reset": self._error_limit_reset,
            "error_limit_paused_for": max(0.0, self._error_limit_until - time.time()),
            "rate_remaining": self._rate_remaining,
            "rate_limit": self._rate_limit,
            "rate_limit_group": self._rate_limit_group,
            "rate_limited_characters": len(self._rate_until),
            "stats": dict(self.stats),
        }

    # ------------------------------------------------------------------
    def request(self, method: str, path: str, params: dict = None, body=None,
                character_id=None, token=None, cache: bool = True, force: bool = False,
                max_age: int = None, timeout: int = None, retries: int = None,
                expect_json: bool = True):
        """发起一次 ESI 请求。

        返回 dict: {data, status, headers, from_cache, url}
        """
        url = self._url(path, params)
        if token is None and character_id is not None and self._token_manager is not None:
            token = self._token_manager.token_for(character_id)

        method = method.upper()
        cacheable = cache and method == "GET"
        key = "%s|%s" % (url, character_id if character_id is not None else "")
        cached = self.store.cache_get(key) if cacheable else None
        headers_extra = {}
        now = utils.now()

        if cacheable and cached and not force:
            expires = cached.get("expires") or 0
            ttl_ok = (max_age is not None and cached.get("fetched_at", 0) + max_age > now)
            if (self.config.get("respect_expires", True) and expires > now) or ttl_ok:
                self.stats["cache_hits"] += 1
                data = utils.jloads(cached.get("body"), None) if expect_json else cached.get("body")
                return {"data": data, "status": cached.get("status") or 200,
                        "headers": {}, "from_cache": True, "url": url}
            if cached.get("etag"):
                headers_extra["If-None-Match"] = cached["etag"]

        attempts = retries if retries is not None else int(self.config.get("max_retries", 3))
        timeout = timeout or int(self.config.get("request_timeout", 30))
        # 只重试读请求。重复 POST 可能造成重复副作用（发邮件、下订单之类），
        # 宁可直接失败让调用方决定，也不要偷偷重发。
        retryable = method in ("GET", "HEAD")
        if not retryable:
            attempts = 0
        max_bytes = int(self.config.get("max_response_bytes", 8 * 1024 * 1024))
        payload = None
        if body is not None:
            payload = utils.jdumps(body).encode("utf-8") if not isinstance(body, bytes) else body
        last_error = None

        for attempt in range(attempts + 1):
            # 发之前先看限流状态：与其撞上去再退避，不如提前等/直接报错
            limited = self.rate_limited_for(character_id)
            if limited > 0:
                if limited <= 5:
                    self.stats["sleep_seconds"] += limited
                    utils.log("等待 %.1f 秒（ESI 限流窗口未过）", limited)
                    time.sleep(limited)
                else:
                    raise ESIError(429, "ESI 限流中，%.0f 秒后再试" % limited, url,
                                   retry_after=int(limited))
            self._pace()
            headers = self._headers(token=token, extra=headers_extra)
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                self.stats["requests"] += 1
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        raise ESIError(resp.status,
                                       "响应超过 %d MiB 上限，已中止（避免内存被吃满）"
                                       % (max_bytes // 1048576), url)
                    self.stats["bytes"] += len(raw)
                    self._track_limits(resp.headers, character_id)
                    text = raw.decode("utf-8", "replace")
                    etag = resp.headers.get("ETag")
                    expires = self._expires_epoch(resp.headers)
                    if cacheable:
                        self.store.cache_put(key, url, text, etag=etag, expires=expires,
                                             status=resp.status)
                    data = utils.jloads(text, None) if expect_json else text
                    return {"data": data, "status": resp.status,
                            "headers": dict(resp.headers), "from_cache": False, "url": url}
            except urllib.error.HTTPError as exc:
                status = exc.code
                raw = b""
                try:
                    raw = exc.read(max_bytes + 1)
                except Exception:
                    pass
                text = raw.decode("utf-8", "replace")
                hdrs = exc.headers
                self._track_limits(hdrs, character_id)

                if status == 304 and cached:
                    expires = self._expires_epoch(hdrs)
                    self.store.cache_touch(key, expires or (utils.now() + 300))
                    self.stats["not_modified"] += 1
                    data = utils.jloads(cached.get("body"), None) if expect_json else cached.get("body")
                    return {"data": data, "status": 304, "headers": dict(hdrs),
                            "from_cache": True, "url": url}

                if status == 420:
                    # 420 = 应用级错误预算被打穿：停**全部**请求，且不重试
                    # （重试只会继续消耗预算，让情况更糟）
                    self.stats["errors"] += 1
                    wait = self.note_error_limit(hdrs)
                    raise ESIError(status, "ESI 错误预算已打穿，全部请求暂停约 %d 秒" % wait,
                                   url, retry_after=wait, body=text)

                if status == 429:
                    # 429 = 只涉及**某个角色**的速率预算，换别人照样能跑
                    self.stats["errors"] += 1
                    wait = self.note_rate_limited(character_id, hdrs)
                    raise ESIError(status, "该角色的 ESI 速率预算已耗尽，约 %d 秒后恢复" % wait,
                                   url, retry_after=wait, body=text)

                if 500 <= status < 600:
                    self.stats["errors"] += 1
                    # 只重试读请求：重复 POST 可能造成重复副作用
                    if retryable and attempt < attempts:
                        wait = min(2 ** attempt + random.random(), 15)
                        retry_header = self._parse_retry_after(hdrs.get("Retry-After"), 0)
                        if retry_header:
                            wait = min(max(retry_header, 1), 10)   # Retry-After 上限 10 秒
                        self.stats["sleep_seconds"] += wait
                        utils.log("ESI %s 服务端错误，%.1f 秒后重试 (%s)", status, wait, path)
                        time.sleep(wait)
                        continue
                    raise ESIError(status, "ESI 服务端错误", url, body=text)

                # 401：令牌失效，交给 TokenManager 处理一次
                if status == 401 and character_id is not None and self._token_manager is not None \
                        and retryable and attempt < attempts:
                    utils.log("ESI 401，强制刷新 %s 的令牌后重试", character_id)
                    self._token_manager.invalidate(character_id)
                    token = self._token_manager.token_for(character_id, force=True)
                    continue

                self.stats["errors"] += 1
                message = _error_message(text, status)
                raise ESIError(status, message, url, body=text)
            except urllib.error.URLError as exc:
                self.stats["errors"] += 1
                last_error = exc
                if retryable and attempt < attempts:
                    wait = min(2 ** attempt + random.random(), 15)
                    self.stats["sleep_seconds"] += wait
                    utils.log("ESI 网络错误 %s，%.1f 秒后重试", exc.reason, wait)
                    time.sleep(wait)
                    continue
                raise ESIError(0, "网络错误：%s" % (exc.reason,), url)

        raise ESIError(0, "请求失败：%s" % (last_error,), url)

    # ------------------------------------------------------------------
    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def get_data(self, path, **kwargs):
        return self.request("GET", path, **kwargs)["data"]

    def get_paged(self, path: str, params: dict = None, max_pages: int = 200, **kwargs) -> list:
        """按 X-Pages 抓取全部分页。"""
        params = dict(params or {})
        params["page"] = 1
        first = self.request("GET", path, params=params, **kwargs)
        pages = utils.as_int((first.get("headers") or {}).get("X-Pages"), 1)
        pages = max(1, min(pages, max_pages))
        out = list(first.get("data") or [])
        if pages == 1:
            return out
        for page in range(2, pages + 1):
            params["page"] = page
            result = self.request("GET", path, params=params, **kwargs)
            chunk = result.get("data") or []
            if not isinstance(chunk, list):
                break
            out.extend(chunk)
        return out

    def post(self, path, body, **kwargs):
        return self.request("POST", path, body=body, cache=False, **kwargs)

    # ------------------------------------------------------------------
    def map_concurrent(self, fn, items, workers: int = None):
        """并发映射，保持顺序。"""
        items = list(items)
        if not items:
            return []
        workers = int(workers or self.config.get("concurrency", 4))
        workers = max(1, min(workers, 16, len(items)))
        if workers == 1:
            return [fn(item) for item in items]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(fn, items))

    # ------------------------------------------------------------------
    # 便捷封装
    # ------------------------------------------------------------------
    def names_for(self, ids) -> dict:
        """POST /universe/names/ （一次最多 1000 个 id），带本地长期缓存。"""
        ids = [int(i) for i in ids if i]
        if not ids:
            return {}
        out = {}
        todo = []
        cache_key = "names"
        mapping = self.store.kv_get(cache_key, {}) or {}
        for i in ids:
            key = str(i)
            if key in mapping:
                out[i] = mapping[key]
            else:
                todo.append(i)
        for part in utils.chunked(todo, 900):
            try:
                data = self.post("/universe/names/", part, expect_json=True)["data"] or []
                for item in data:
                    out[item["id"]] = item.get("name")
                    mapping[str(item["id"])] = item.get("name")
            except ESIError as exc:
                utils.log("解析名称失败 %s: %s", part[:3], exc)
        if todo:
            if len(mapping) > 200000:
                mapping = {}
            self.store.kv_set(cache_key, mapping)
        return out

    def esi_status(self) -> dict:
        try:
            return self.get_data("/status/", cache=False) or {}
        except ESIError as exc:
            return {"error": exc.message}
