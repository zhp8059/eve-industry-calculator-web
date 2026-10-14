"""EVE SSO 登录（OAuth2 + PKCE）与访问令牌管理。

流程：
  1. 生成 state + PKCE code_verifier / code_challenge(S256)
  2. 浏览器打开 authorize 端点
  3. 回跳 http://localhost:端口/callback?code=...&state=...
  4. 用 code + code_verifier 换令牌（公共客户端；若填了 secret 则用 HTTP Basic）
  5. 校验 JWT（RS256 验签 + iss/aud/exp），取出 sub=CHARACTER:EVE:<id>
  6. 刷新令牌用 DPAPI 加密后落盘；访问令牌缓存在内存与数据库

JWT 验签用纯标准库实现 RSA PKCS#1 v1.5（RS256），不需要 PyJWT。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config as cfgmod
from . import secure, utils


class SSOError(Exception):
    """SSO 相关错误。

    `status` / `code` 用来区分两类失败，这个区分非常关键：

      * **确定性失败**（`invalid_grant` / `invalid_token`）：EVE 明确拒绝了这组令牌，
        重试一万次也没用 —— 应该把该角色标成「需重新登录」，停止继续消耗。
      * **暂时性失败**（网络不通、CCP 5xx、客户端配置写错）：下次再试就好 ——
        **绝不能**因此把角色标死，否则一次断网就会让用户白白重新登录一遍。
    """

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def definitive(self) -> bool:
        """是否属于「重试无意义」的确定性失败。

        只用 EVE SSO 自己的词汇表判断。**有意不把 HTTP 401 算进来**：
        401 也可能是 client_id/secret 配错造成的，那是配置问题而不是
        「这个角色的登录死了」，不该因此清掉用户的有效授权。
        """
        return self.code in ("invalid_grant", "invalid_token")


# --------------------------------------------------------------------------
# base64url / JWT
# --------------------------------------------------------------------------

def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    if isinstance(text, bytes):
        text = text.decode("ascii")
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _rsa_pkcs1_verify(n: int, e: int, signature: bytes, digest: bytes,
                      hash_name: str = "sha256") -> bool:
    """验证 RSA PKCS#1 v1.5 签名（纯整数运算）。"""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    try:
        m = pow(int.from_bytes(signature, "big"), e, n)
    except Exception:
        return False
    recovered = m.to_bytes(k, "big")
    prefixes = {
        "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
        "sha1": bytes.fromhex("3021300906052b0e03021a05000414"),
        "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
    }
    prefix = prefixes.get(hash_name)
    if prefix is None:
        return False
    digest_info = prefix + digest
    padding_len = k - len(digest_info) - 3
    if padding_len < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding_len + b"\x00" + digest_info
    return hmac.compare_digest(recovered, expected)


def decode_jwt_unverified(token: str) -> tuple:
    parts = token.split(".")
    if len(parts) != 3:
        raise SSOError("不是合法的 JWT")
    header = json.loads(b64u_decode(parts[0]))
    payload = json.loads(b64u_decode(parts[1]))
    return header, payload, parts


def verify_access_token(token: str, jwks: dict, client_id: str) -> dict:
    """校验访问令牌，返回其 claims。"""
    header, payload, parts = decode_jwt_unverified(token)
    alg = header.get("alg")
    kid = header.get("kid")

    if alg != "RS256":
        # EVE 也用 ES256 签署其它令牌；这里不做未实现算法的验签，
        # 但令牌是通过 TLS 直接从 login.eveonline.com 拿到并已做 iss/aud/exp 校验。
        utils.log("⚠ 访问令牌算法为 %s，跳过签名验签（仅做 iss/aud/exp 校验）", alg)
    else:
        key = _find_jwk(jwks, kid)
        if key is None:
            raise SSOError("JWKS 中找不到 kid=%s 的签名密钥" % kid)
        n = int.from_bytes(b64u_decode(key["n"]), "big")
        e = int.from_bytes(b64u_decode(key["e"]), "big")
        signature = b64u_decode(parts[2])
        digest = hashlib.sha256(parts[0].encode("ascii") + b"." + parts[1].encode("ascii")).digest()
        if not _rsa_pkcs1_verify(n, e, signature, digest, "sha256"):
            raise SSOError("访问令牌签名校验失败")

    now = utils.now()
    exp = payload.get("exp")
    if exp and int(exp) < now - 30:
        raise SSOError("访问令牌已过期")

    iss = (payload.get("iss") or "").rstrip("/")
    # 官方文档：iss 应为 https://login.eveonline.com/ ，但实测也可能是裸域名
    if iss not in ("https://login.eveonline.com", "login.eveonline.com", ""):
        raise SSOError("访问令牌签发者异常：%s" % payload.get("iss"))

    aud = payload.get("aud")
    if isinstance(aud, str):
        aud = [aud]
    if aud and client_id:
        if client_id not in aud or "EVE Online" not in aud:
            raise SSOError("访问令牌受众不匹配（aud=%s）" % (aud,))
    return payload


def _find_jwk(jwks: dict, kid):
    keys = (jwks or {}).get("keys") or []
    for key in keys:
        if key.get("kid") == kid:
            return key
    for key in keys:
        if key.get("alg") == "RS256":
            return key
    return None


def parse_subject(sub: str):
    """sub 形如 CHARACTER:EVE:1234567890"""
    if not sub:
        return None
    parts = str(sub).split(":")
    if len(parts) >= 3 and parts[0].upper() == "CHARACTER":
        try:
            return int(parts[2])
        except Exception:
            return None
    return None


def owner_change_state(previous_owner, new_owner):
    """判断角色是否换了主人，并给出应该落库的 owner_hash。

    EVE 的 `owner` claim 是角色的「产权凭证」：角色被卖掉/转手后它会变。
    规则（与 evesynapse 的 ownerHashChanged 一致）：

      * 两侧都**非空**且不同 → 换了主人（要打标暂停同步，等用户确认）
      * 任一侧为空 → 不判定为换主人（CCP 偶尔不给这个 claim，
        绝不能因为「这次没给」就把已知的产权记录抹掉）

    返回 (changed, stored_owner)。
    """
    previous_owner = previous_owner or ""
    new_owner = new_owner or ""
    changed = bool(previous_owner and new_owner and previous_owner != new_owner)
    return changed, (new_owner or previous_owner)


# --------------------------------------------------------------------------
# PKCE
# --------------------------------------------------------------------------

def generate_pkce() -> tuple:
    verifier = b64u_encode(os.urandom(32))
    challenge = b64u_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# --------------------------------------------------------------------------
# HTTP 表单请求
# --------------------------------------------------------------------------

def _clean_error_detail(text: str) -> str:
    """把 SSO 返回的错误正文压成一句人话（可能是 HTML 页面）。"""
    if not text:
        return ""
    stripped = text.strip()
    low = stripped[:400].lower()
    if low.startswith("<!doctype") or low.startswith("<html") or "<body" in low:
        import re as _re
        title = _re.search(r"<title[^>]*>(.*?)</title>", stripped, _re.I | _re.S)
        heading = _re.search(r"<h1[^>]*>(.*?)</h1>", stripped, _re.I | _re.S)
        piece = (title.group(1) if title else "") or (heading.group(1) if heading else "")
        piece = _re.sub(r"<[^>]+>", " ", piece)
        piece = " ".join(piece.split())[:120]
        return piece or "SSO 返回了一个 HTML 错误页（通常是 client_id 或回调地址不正确）"
    return " ".join(stripped.split())[:200]


def _post_form(url: str, fields: dict, user_agent: str, basic_auth=None, timeout: int = 10) -> dict:
    """向 SSO 端点发一次表单请求。

    默认超时 10 秒：不能依赖默认值 —— CCP 挂住时会一直等下去，
    界面就一直转圈，用户只能强杀进程。
    """
    body = urllib.parse.urlencode(fields).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    if basic_auth:
        client_id, client_secret = basic_auth
        raw = ("%s:%s" % (client_id, client_secret)).encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        text = raw.decode("utf-8", "replace")
        detail = _clean_error_detail(text)
        parsed = utils.jloads(text, None)
        code = None
        if isinstance(parsed, dict):
            code = parsed.get("error")
            detail = parsed.get("error_description") or parsed.get("error") or detail
        hint = ""
        if exc.code == 401:
            hint = "（请检查 client_id 是否正确；若选了 Confidential 客户端还需填 client_secret）"
        elif exc.code == 400:
            hint = "（授权码可能已失效或已使用过；每个 code 只能用一次）"
        # 令牌值绝不进日志，错误只带状态码与 SSO 给的错误码
        utils.log("SSO 令牌端点返回 %s（error=%s）", exc.code, code)
        raise SSOError("SSO 返回 %s：%s%s" % (exc.code, detail, hint),
                       status=exc.code, code=code)
    except urllib.error.URLError as exc:
        raise SSOError("无法连接 EVE SSO：%s" % (exc.reason,))
    data = utils.jloads(text, None)
    if not isinstance(data, dict):
        raise SSOError("SSO 响应无法解析：%s" % _clean_error_detail(text))
    if data.get("error"):
        raise SSOError("SSO 错误：%s %s" % (data.get("error"), data.get("error_description") or ""),
                       status=200, code=data.get("error"))
    return data


# --------------------------------------------------------------------------
# 令牌管理
# --------------------------------------------------------------------------

class TokenManager:
    """负责登录、刷新、以及向 ESI 客户端提供有效的访问令牌。"""

    def __init__(self, store, config: dict):
        self.store = store
        self.config = config
        self._memory = {}             # character_id -> (access_token, expires_at)
        self._pending = {}            # state -> {verifier, created_at}
        self._char_locks = {}         # character_id -> Lock（刷新串行化用）
        self._jwks = None
        self._jwks_at = 0
        self._metadata = None
        self._lock = threading.RLock()
        # 距离过期不足这个秒数就提前刷新
        self._refresh_margin = 60

    def _char_lock(self, character_id: int):
        """取某个角色的刷新锁（不同角色互不影响，可并行）。"""
        with self._lock:
            lock = self._char_locks.get(int(character_id))
            if lock is None:
                lock = threading.Lock()
                self._char_locks[int(character_id)] = lock
            return lock

    # ---------------- 元数据 / JWKS ----------------
    def metadata(self, force: bool = False) -> dict:
        with self._lock:
            cached = self.store.kv_get("sso_metadata")
            age = utils.now() - (cached or {}).get("_at", 0)
            if cached and not force and age < 86400:
                self._metadata = cached
                return cached
        try:
            req = urllib.request.Request(
                cfgmod.SSO_METADATA_URL,
                headers={"User-Agent": self.config.get("user_agent") or "eve-industry-local",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = utils.jloads(resp.read().decode("utf-8", "replace"), {}) or {}
            data["_at"] = utils.now()
            self.store.kv_set("sso_metadata", data)
            with self._lock:
                self._metadata = data
            return data
        except Exception as exc:
            utils.log("获取 SSO 元数据失败，改用内置地址：%r", exc)
            fallback = {
                "authorization_endpoint": cfgmod.SSO_AUTHORIZE_URL,
                "token_endpoint": cfgmod.SSO_TOKEN_URL,
                "jwks_uri": cfgmod.SSO_JWKS_URL,
                "revocation_endpoint": "https://login.eveonline.com/v2/oauth/revoke",
                "_at": utils.now(), "_fallback": True,
            }
            with self._lock:
                self._metadata = fallback
            return fallback

    def jwks(self, force: bool = False) -> dict:
        with self._lock:
            if self._jwks and not force and utils.now() - self._jwks_at < 86400:
                return self._jwks
        url = self.metadata().get("jwks_uri") or cfgmod.SSO_JWKS_URL
        req = urllib.request.Request(url, headers={
            "User-Agent": self.config.get("user_agent") or "eve-industry-local",
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = utils.jloads(resp.read().decode("utf-8", "replace"), {}) or {}
        with self._lock:
            self._jwks = data
            self._jwks_at = utils.now()
        return data

    # ---------------- 登录 ----------------
    @property
    def client_id(self) -> str:
        """实际使用的 Client ID：配置里的优先，否则用程序**内置**的那个。

        内置值让普通用户不用注册应用就能登录；想用自己的应用就在设置里覆盖。
        """
        return cfgmod.resolve_client_id(self.config)

    @property
    def client_id_source(self) -> str:
        return cfgmod.client_id_source(self.config)

    @property
    def client_secret(self) -> str:
        return (self.config.get("client_secret") or "").strip()

    def configured(self) -> bool:
        # 有内置 Client ID，所以永远是「已配置」——登录不会因为缺 client_id 被挡
        return bool(self.client_id)

    def redirect_uri(self, host: str = "localhost") -> str:
        return cfgmod.callback_url(self.config, host)

    def login_url(self, redirect_uri: str = None, scopes=None, tier: str = None) -> dict:
        """生成授权 URL。

        tier 决定请求哪一档 scope（见 config.SCOPE_TIERS）：
          character    只拿个人数据 —— 权限最小，够日常算蓝图用
          corporation   额外拿军团数据
        显式传 scopes 时以 scopes 为准。
        """
        if not self.configured():
            raise SSOError("没有可用的 client_id：程序已内置一个，"
                           "若被清空请在「设置」里填入自己的 Client ID")
        redirect_uri = redirect_uri or self.redirect_uri()
        if self.config.get("use_pkce", True):
            verifier, challenge = generate_pkce()
        else:
            verifier, challenge = None, None
        state = b64u_encode(os.urandom(24))
        scope_list = list(scopes) if scopes else cfgmod.scopes_for_tier(
            tier or self.config.get("scope_tier") or "corporation")
        # 审计：本程序只需要读权限。如果配置里混进了「能代替用户做事」的 scope，
        # 明确告警 —— 这类权限（发邮件、开窗口、改舰队、改行星工厂）我们一个都用不到。
        risky = cfgmod.audit_scopes(scope_list)
        if risky:
            utils.log("⚠ 本次授权请求包含 %d 项高危权限，本程序不需要它们：%s",
                      len(risky), "、".join(risky))
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scope_list),
        }
        if challenge:
            query["code_challenge"] = challenge
            query["code_challenge_method"] = "S256"
        endpoint = self.metadata().get("authorization_endpoint") or cfgmod.SSO_AUTHORIZE_URL
        url = endpoint + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        with self._lock:
            # 存下 url：万一回调没回到本机，界面要能重新给出这条链接
            self._pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri,
                                    "created_at": utils.now(), "scopes": scope_list,
                                    "url": url}
            # 清掉 10 分钟前的登录尝试
            for key in [k for k, v in self._pending.items()
                        if utils.now() - v["created_at"] > 600]:
                self._pending.pop(key, None)
        return {"url": url, "state": state, "redirect_uri": redirect_uri,
                "scopes": list(scope_list), "pkce": bool(challenge)}

    def pending_login(self) -> dict:
        """有没有「已经点了授权、但还没完成」的登录尝试。

        用途：用户点完授权被带到 EVE 页面；如果回调没成功（端口被占、
        浏览器换了、回调地址被拒），他回到本程序时应该看到一个
        「正在等待授权完成」的窗口，并且**在里面就能手动粘贴授权码** ——
        而不是自己去找那个入口。
        """
        with self._lock:
            now = utils.now()
            alive = {k: v for k, v in self._pending.items()
                     if now - v.get("created_at", 0) <= 600}
            if not alive:
                return {"pending": False}
            # 取最近的一条来展示：用**插入顺序**而不是比较 created_at ——
            # 时间戳只到秒，同一秒内点两次登录时 max() 会因并列而返回较旧的那条。
            # dict 保序，所以最后插入的就是最近创建的。
            state = next(reversed(alive))
            entry = alive[state]
        return {
            "pending": True,
            "age_seconds": int(now - entry.get("created_at", now)),
            "expires_in": max(0, 600 - int(now - entry.get("created_at", now))),
            "url": entry.get("url"),
            "redirect_uri": entry.get("redirect_uri"),
            "scopes": entry.get("scopes") or [],
            "attempts": len(alive),
        }

    def cancel_pending(self) -> int:
        """丢掉所有待处理的登录尝试（用户点了「取消」）。"""
        with self._lock:
            count = len(self._pending)
            self._pending.clear()
        return count

    def exchange_code(self, code: str, state: str = None, verifier: str = None,
                      redirect_uri: str = None) -> dict:
        pending = None
        if state:
            with self._lock:
                pending = self._pending.pop(state, None)
        if pending is None and verifier:
            pending = {"verifier": verifier, "redirect_uri": redirect_uri}
        if pending is None:
            # 兜底：只有一次待处理的登录尝试时直接采用它。
            # 用于「手动粘贴授权码」场景 —— 前端可能只拿到 code，没带 state，
            # 但 PKCE 的 code_verifier 必须与服务端存的那次尝试配对，不能丢。
            with self._lock:
                alive = {k: v for k, v in self._pending.items()
                         if utils.now() - v.get("created_at", 0) <= 600}
                if len(alive) == 1:
                    key = next(iter(alive))
                    pending = self._pending.pop(key)
                    utils.log("手动授权：按唯一未完成的登录尝试配对（state=%s…）", key[:8])
        if pending is None:
            raise SSOError("登录状态已失效（state 不匹配），请重新点击登录")
        fields = {
            "grant_type": "authorization_code",
            "code": code,
        }
        use_pkce = bool(pending.get("verifier"))
        if use_pkce:
            fields["code_verifier"] = pending["verifier"]
            fields["client_id"] = self.client_id
        endpoint = self.metadata().get("token_endpoint") or cfgmod.SSO_TOKEN_URL
        basic = None if use_pkce else (self.client_id, self.client_secret)
        if not use_pkce and not self.client_secret:
            # 没有 secret 又没用 PKCE：把 client_id 放进 body
            fields["client_id"] = self.client_id
            basic = None
        result = _post_form(endpoint, fields, self.config.get("user_agent") or "eve-industry-local",
                            basic_auth=basic)
        return self._store_tokens(result)

    def refresh(self, refresh_token: str, character_id=None) -> dict:
        fields = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        basic = None
        if self.client_secret:
            basic = (self.client_id, self.client_secret)
        else:
            fields["client_id"] = self.client_id
        endpoint = self.metadata().get("token_endpoint") or cfgmod.SSO_TOKEN_URL
        try:
            result = _post_form(endpoint, fields,
                                self.config.get("user_agent") or "eve-industry-local",
                                basic_auth=basic)
        except SSOError as exc:
            # EVE 明确说这组令牌无效 → 把角色标成需重新登录，别再反复撞墙。
            # 网络错误 / 配置错误绝不标记（否则断一次网就要用户重新登录）。
            if character_id and exc.definitive:
                marked = self.store.set_link_state(int(character_id), cfgmod.LINK_TOKEN_DEAD,
                                                  only_from_ok=True)
                utils.log("角色的刷新令牌已被 EVE 拒绝（%s），标记为需重新登录%s",
                          exc.code, "" if marked else "（原状态更强，未覆盖）")
            raise
        return self._store_tokens(result, previous_refresh=refresh_token,
                                  character_id=character_id)

    def revoke(self, token: str, token_type_hint: str = "refresh_token") -> bool:
        endpoint = self.metadata().get("revocation_endpoint") or "https://login.eveonline.com/v2/oauth/revoke"
        try:
            _post_form(endpoint, {"token": token, "token_type_hint": token_type_hint},
                       self.config.get("user_agent") or "eve-industry-local",
                       basic_auth=((self.client_id, self.client_secret)
                                   if self.client_secret else None))
            return True
        except Exception as exc:
            utils.log("吊销令牌失败：%r", exc)
            return False

    # ---------------- 令牌落库 ----------------
    def _store_tokens(self, result: dict, previous_refresh: str = None,
                      character_id=None) -> dict:
        access_token = result.get("access_token")
        if not access_token:
            raise SSOError("SSO 未返回访问令牌")
        refresh_token = result.get("refresh_token") or previous_refresh
        expires_in = utils.as_int(result.get("expires_in"), 1200)
        expires_at = utils.now() + expires_in

        # 验签失败 = 拒绝这次登录。绝不能退回到「未验签的 claims」继续：
        # 那样等于任何能伪造 JWT 的人都拿到你的角色身份。
        # kid 未命中先强制刷一次 JWKS（CCP 会轮换签名密钥），仍然失败就报错。
        try:
            claims = verify_access_token(access_token, self.jwks(), self.client_id)
        except SSOError as first_error:
            try:
                claims = verify_access_token(access_token, self.jwks(force=True),
                                             self.client_id)
            except SSOError:
                utils.log("令牌校验未通过，已拒绝本次登录：%s", first_error)
                raise SSOError("访问令牌校验未通过（%s），已拒绝登录" % first_error)

        parsed_id = parse_subject(claims.get("sub"))
        if not parsed_id:
            raise SSOError("令牌里的 sub 不是合法角色标识")
        if character_id is not None and int(character_id) != int(parsed_id):
            # 换刷新令牌时尤其要卡死这一点：令牌身份必须与请求的角色一致
            raise SSOError("令牌身份（%d）与预期角色（%s）不一致" % (parsed_id, character_id))
        character_id = int(parsed_id)

        scopes = claims.get("scp") or result.get("scp") or []
        if isinstance(scopes, str):
            scopes = scopes.split()

        character_name = claims.get("name") or ("角色 %d" % character_id)
        owner_hash = claims.get("owner") or ""
        corporation_id = claims.get("corporation_id")
        alliance_id = claims.get("alliance_id")

        existing = self.store.get_character(character_id) or {}
        # 两侧都非空且不同 ⇒ 这个角色换了主人（被卖掉/转手）。
        # 绝不能空值覆盖已知值，否则一次「CCP 没给 owner」就把产权记录抹掉了。
        owner_changed, stored_owner = owner_change_state(existing.get("owner_hash"), owner_hash)

        if owner_changed:
            # 存入新 hash 但同时打标暂停同步：要用户再登录一次（此时 hash 已一致）
            # 或显式点「确认已转手」才会恢复，避免把旧主人的缓存数据当自己的用。
            link_state = cfgmod.LINK_OWNER_CHANGED
            utils.log("角色 %s（%d）的 owner_hash 变了，已标记为需确认", character_name, character_id)
        else:
            link_state = cfgmod.LINK_OK

        with self._lock:
            self._memory[character_id] = (access_token, expires_at)

        self.store.upsert_character({
            "character_id": character_id,
            "name": character_name,
            "corporation_id": corporation_id if corporation_id is not None else existing.get("corporation_id"),
            "corporation_name": existing.get("corporation_name"),
            "alliance_id": alliance_id if alliance_id is not None else existing.get("alliance_id"),
            "scopes": scopes,
            "owner_hash": stored_owner,
            # 令牌按「角色 + 字段」绑定加密：搬到别的行/列都解不开
            "refresh_token": (secure.seal(refresh_token.encode(), character_id, "refresh")
                              if refresh_token else existing.get("refresh_token")),
            "access_token": secure.seal(access_token.encode(), character_id, "access"),
            "access_expires": expires_at,
            "token_type": result.get("token_type") or "Bearer",
            "encrypted": 1 if secure.available() else 0,
            "added_at": existing.get("added_at") or utils.now(),
            "last_used": utils.now(),
            "note": existing.get("note"),
            "link_state": link_state,
            "link_state_at": utils.now() if owner_changed else existing.get("link_state_at"),
        })
        if not existing:
            self.store.set_active(character_id)
        utils.log("✔ 已登录角色：%s (%d)，授权 %d 个 scope", character_name, character_id, len(scopes))
        return {"character_id": character_id, "name": character_name,
                "scopes": scopes, "expires_at": expires_at,
                "pending_pkce": bool(result.get("pending_pkce"))}

    # ---------------- 取用令牌 ----------------
    def token_for(self, character_id: int, force: bool = False) -> str:
        """取一个现在就能用的访问令牌，必要时先刷新。

        同一个角色的刷新**必须串行**：EVE 每次刷新都会轮换 refresh token，
        两个线程拿同一个旧 token 去换，后写回的那个会把先写回的作废，
        表现就是「偶尔莫名需要重新登录」。所以这里按角色加锁，
        并在锁内**重新读一次数据库**（另一个线程可能刚刷完，直接复用即可）。
        不同角色之间不互相阻塞。
        """
        character_id = int(character_id)
        with self._char_lock(character_id):
            return self._token_for_locked(character_id, force)

    def _token_for_locked(self, character_id: int, force: bool = False) -> str:
        margin = self._refresh_margin
        # 第一层：进程内缓存（另一个线程刚刷过就会命中）
        with self._lock:
            cached = self._memory.get(character_id)
        if cached and not force and cached[1] - margin > utils.now():
            return cached[0]

        row = self.store.get_character(character_id)
        if row is None:
            raise SSOError("角色 %d 未登录" % character_id)
        state = row.get("link_state") or ""
        if not self.store.state_allows_sync(state):
            label = cfgmod.LINK_LABELS.get(state, state)
            raise SSOError("角色 %s 当前状态为「%s」，请重新登录该角色后再操作"
                           % (row.get("name") or character_id, label))

        # 第二层：数据库里的访问令牌（锁内重读，可能已被别的线程刷新）
        # 注意 access_token 现在是加密存储的，必须解密后再判断
        if not force and (row.get("access_expires") or 0) - margin > utils.now():
            token = self.store.access_token(character_id)
            if token:
                with self._lock:
                    self._memory[character_id] = (token, row["access_expires"])
                return token

        refresh_token = self.store.refresh_token(character_id)
        if not refresh_token:
            raise SSOError("角色 %s 没有可用的刷新令牌，请重新登录"
                           % (row.get("name") or character_id))
        result = self.refresh(refresh_token, character_id=character_id)
        if int(result["character_id"]) != character_id:
            raise SSOError("刷新后拿到的令牌属于另一个角色，已丢弃")
        with self._lock:
            cached = self._memory.get(character_id)
        if not cached:
            raise SSOError("刷新令牌后仍未取得访问令牌")
        return cached[0]

    def invalidate(self, character_id: int) -> None:
        with self._lock:
            self._memory.pop(int(character_id), None)
        self.store.x("UPDATE characters SET access_expires=0 WHERE character_id=?", (int(character_id),))

    def token_state(self, character_id: int) -> dict:
        row = self.store.get_character(character_id)
        if not row:
            return {"logged_in": False}
        with self._lock:
            cached = self._memory.get(int(character_id))
        expires = cached[1] if cached else (row.get("access_expires") or 0)
        state = row.get("link_state") or ""
        return {
            "logged_in": True,
            "character_id": row["character_id"],
            "name": row["name"],
            "expires_at": expires,
            "valid_for": max(0, expires - utils.now()),
            "has_refresh": self.store.refresh_token(character_id) is not None,
            "scopes": utils.jloads(row.get("scopes"), []),
            "encrypted": bool(row.get("encrypted")),
            "link_state": state,
            "link_state_label": cfgmod.LINK_LABELS.get(state, state),
            "syncs": self.store.state_allows_sync(state),
            "owner_hash_present": bool(row.get("owner_hash")),
        }
