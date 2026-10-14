"""OAuth 令牌的本地落盘加密。

三套后端，按可用性自动选择，**读的时候按信封前缀自动分派**（所以换过后端
仍能解开旧数据）：

    DPAPI1\\0 + 密文      Windows DPAPI。零配置，且密文与「当前 Windows 用户」绑定，
                         把 data/ 整个目录拷走也解不开 —— Windows 上的默认选择。
    enc:v2: + base64     AES-256-GCM。纯标准库实现（见 app/aesgcm.py），跨平台，
                         密钥来自「操作员口令」或自动生成的 data/token.key。
    PLAIN1\\0 + 明文      仅在后端都不可用时兜底，并在界面上明确告警。

设计参考了 natemsz/evesynapse 的 auth_tokencrypt.go（Go）：

    * AES-256-GCM，12 字节随机 nonce，布局 nonce ‖ 密文 ‖ 标签（16 字节）
    * 密钥 = HKDF-SHA256(口令, 固定盐, 固定 info) → 32 字节
    * AAD 绑定「哪一行、哪一列」：character:<id>:<access|refresh>
      ⇒ 把 A 角色的令牌搬到 B 角色行、或把 refresh 当 access 用，都会解密失败
    * 信封带版本前缀，便于以后换格式
    * 混淆/失败一律**报错**，绝不静默返回空串
    * 绝不把令牌值写进日志

与它的两点有意差异：

    1. Windows 上默认用 DPAPI 而不是口令派生密钥 —— 本程序是单机自用工具，
       「零配置且与用户绑定」比「可跨机迁移」更重要。想要可迁移就配
       token_encryption = "aes-gcm"（或设 EVE_TOKEN_ENCRYPTION_KEY）。
    2. 解密失败不会拒绝启动。evesynapse 是常驻服务，拒绝启动是对的；
       本程序是桌面工具，起不来比带着告警运行更糟 —— 所以改成把该角色
       标为「需重新登录」并在自检里报出来。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as _wt
import os
import sys

from . import aesgcm

DPAPI_MAGIC = b"DPAPI1\x00"
AESGCM_MAGIC = b"enc:v2:"
PLAIN_MAGIC = b"PLAIN1\x00"

_ENTROPY = b"eve-industry-local/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01

# 与 evesynapse 对齐：口令至少 32 个字符，经 HKDF 派生 32 字节密钥
KEY_MIN_CHARS = 32
KDF_SALT = b"eve-industry token encryption salt"
KDF_INFO = b"eve-industry/token-encryption/v2"
KEY_FILE_NAME = "token.key"
KEY_ENV = "EVE_TOKEN_ENCRYPTION_KEY"

_LAST_ERROR = ""
_EXPLICIT_KEY_ERROR = ""      # 用户显式配了口令但它不可用 —— 绝不静默降级为明文
_config = None
_key_cache = None


class TokenKeyMissing(Exception):
    """数据是加过密的，但当前拿不到可用的密钥。"""


class TokenUnreadable(Exception):
    """密钥不对、密文被篡改，或 AAD（行/列）对不上。"""


# --------------------------------------------------------------------------
# 配置注入
# --------------------------------------------------------------------------

def configure(config: dict) -> None:
    """由 main/server 在启动时注入配置（避免这里反复读 config.json）。"""
    global _config, _key_cache
    _config = config
    _key_cache = None


def _cfg() -> dict:
    global _config
    if _config is None:
        from . import config as cfgmod
        try:
            _config = cfgmod.load_config()
        except Exception:
            _config = {}
    return _config


def key_file_path() -> str:
    from . import config as cfgmod
    return os.path.join(cfgmod.DATA_DIR, KEY_FILE_NAME)


# --------------------------------------------------------------------------
# 后端选择
# --------------------------------------------------------------------------

def dpapi_available() -> bool:
    return sys.platform == "win32"


def backend() -> str:
    """当前会用哪个后端加密：dpapi / aes-gcm / plaintext。"""
    preference = str(_cfg().get("token_encryption") or "auto").lower()
    if preference == "none":
        return "plaintext"
    if preference == "dpapi":
        return "dpapi" if dpapi_available() else _fallback_backend()
    if preference == "aes-gcm":
        return "aes-gcm" if _load_secret(create=False) else _fallback_backend()
    # auto：Windows 用 DPAPI（零配置、与用户绑定），其它平台用 AES-GCM
    return "dpapi" if dpapi_available() else _fallback_backend()


def _fallback_backend() -> str:
    return "aes-gcm" if _load_secret(create=True) else "plaintext"


def available() -> bool:
    """是否至少有一层真正的加密（供界面判断要不要告警）。"""
    return backend() != "plaintext"


def last_error() -> str:
    return _LAST_ERROR


def _clear_error() -> None:
    global _LAST_ERROR
    _LAST_ERROR = ""


# --------------------------------------------------------------------------
# 口令 / 密钥
# --------------------------------------------------------------------------

def _read_secret_file() -> str:
    path = key_file_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _load_secret(create: bool = True) -> str:
    """取加密口令：环境变量优先，其次 data/token.key（不存在则生成）。

    注意：**显式**配置了 EVE_TOKEN_ENCRYPTION_KEY 但它不合法时，这里只记下错误，
    由 seal() 抛出 —— 用户明确要求加密，就绝不能悄悄退化成明文存储。
    """
    global _LAST_ERROR, _EXPLICIT_KEY_ERROR
    env = (os.environ.get(KEY_ENV) or "").strip()
    if env:
        if len(env) < KEY_MIN_CHARS:
            _LAST_ERROR = ("%s 只有 %d 个字符，至少需要 %d 个"
                           % (KEY_ENV, len(env), KEY_MIN_CHARS))
            _EXPLICIT_KEY_ERROR = _LAST_ERROR
            return ""
        _EXPLICIT_KEY_ERROR = ""
        return env

    secret = _read_secret_file()
    if len(secret) >= KEY_MIN_CHARS:
        return secret
    if not create:
        return ""
    try:
        os.makedirs(os.path.dirname(key_file_path()), exist_ok=True)
        secret = base64.b16encode(os.urandom(32)).decode("ascii")   # 64 个十六进制字符
        with open(key_file_path(), "w", encoding="utf-8") as handle:
            handle.write(secret + "\n")
        _restrict_permissions(key_file_path())
        return secret
    except OSError as exc:
        _LAST_ERROR = "无法创建密钥文件：%s" % exc
        return ""


def _restrict_permissions(path: str) -> None:
    """尽量把密钥文件限制为「仅当前用户可读写」。"""
    try:
        if sys.platform == "win32":
            import subprocess
            user = os.environ.get("USERNAME") or ""
            if user:
                subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:F" % user],
                               capture_output=True, timeout=10)
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass


def derive_key(secret: str) -> bytes:
    """口令 → 32 字节 AES 密钥（HKDF-SHA256，盐与 info 固定，属于存储格式的一部分）。"""
    return aesgcm.hkdf_sha256(secret.encode("utf-8"), KDF_SALT, KDF_INFO, 32)


def _key() -> bytes:
    global _key_cache
    if _key_cache is None:
        secret = _load_secret(create=True)
        if not secret:
            raise TokenKeyMissing(_LAST_ERROR or "没有可用的加密口令")
        _key_cache = derive_key(secret)
    return _key_cache


# --------------------------------------------------------------------------
# AAD：绑定到「哪一行、哪一列」
# --------------------------------------------------------------------------

def aad(character_id=None, field: str = "refresh") -> bytes:
    """把密文绑定到具体角色与字段，防止令牌在行/列之间被搬用。"""
    if character_id is None:
        return b"character:unknown:%s" % field.encode("ascii")
    return b"character:%d:%s" % (int(character_id), field.encode("ascii"))


# --------------------------------------------------------------------------
# DPAPI
# --------------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", _wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _load_dlls():
    if not dpapi_available():
        return None, None
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.restype = _wt.BOOL
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob), _wt.LPCWSTR, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, _wt.DWORD, ctypes.POINTER(_Blob),
    ]
    crypt32.CryptUnprotectData.restype = _wt.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.POINTER(_wt.LPWSTR), ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, _wt.DWORD, ctypes.POINTER(_Blob),
    ]
    # 关键：必须声明 argtypes，否则 64 位下指针参数会被截断成 int。
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return crypt32, kernel32


def _make_blob(data: bytes):
    if not data:
        return _Blob(0, None), None
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(protect: bool, data: bytes, entropy: bytes) -> bytes:
    global _LAST_ERROR
    crypt32, kernel32 = _load_dlls()
    if crypt32 is None:
        raise TokenUnreadable("DPAPI 仅在 Windows 上可用")
    blob_in, _keep_in = _make_blob(data)
    blob_ent, _keep_ent = _make_blob(entropy)
    blob_out = _Blob()
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "eve-industry-local",
                                      ctypes.byref(blob_ent), None, None,
                                      _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        what = "CryptProtectData"
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None,
                                        ctypes.byref(blob_ent), None, None,
                                        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        what = "CryptUnprotectData"
    if not ok:
        _LAST_ERROR = "%s 失败: %s" % (what, ctypes.WinError(ctypes.get_last_error()))
        raise TokenUnreadable(_LAST_ERROR)
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def _dpapi_entropy(character_id, field) -> bytes:
    # 把 AAD 混进 DPAPI 的附加熵，这样在 Windows 上也能获得同等的「行/列绑定」
    return _ENTROPY + b"|" + aad(character_id, field)


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def seal(data: bytes, character_id=None, field: str = "refresh") -> bytes:
    """加密。返回带信封前缀的字节串。

    只有「什么都没配、且密钥文件也建不出来」才会退化为明文（并在界面上告警）。
    用户**显式**配置了口令却不可用时一律抛异常 —— 悄悄存明文是最糟的结果。
    """
    global _LAST_ERROR
    data = bytes(data)
    if not data:
        return data                                   # 空值保持空，不制造无意义的密文
    kind = backend()
    if kind == "plaintext" and _EXPLICIT_KEY_ERROR:
        raise TokenKeyMissing(_EXPLICIT_KEY_ERROR)
    if kind == "dpapi":
        try:
            return DPAPI_MAGIC + _dpapi(True, data, _dpapi_entropy(character_id, field))
        except Exception as exc:
            _LAST_ERROR = "DPAPI 加密失败，改用 AES-GCM：%r" % (exc,)
            kind = "aes-gcm" if _load_secret(create=True) else "plaintext"
    if kind == "aes-gcm":
        try:
            nonce = aesgcm.random_bytes(aesgcm.NONCE_BYTES)
            blob = aesgcm.gcm_encrypt(_key(), nonce, data, aad(character_id, field))
            return AESGCM_MAGIC + base64.b64encode(nonce + blob)
        except TokenKeyMissing:
            raise
        except Exception as exc:
            _LAST_ERROR = "AES-GCM 加密失败，退化为明文：%r" % (exc,)
    if _EXPLICIT_KEY_ERROR:
        raise TokenKeyMissing(_EXPLICIT_KEY_ERROR)
    _LAST_ERROR = _LAST_ERROR or "没有可用的加密后端"
    return PLAIN_MAGIC + data


def unseal(blob, character_id=None, field: str = "refresh") -> bytes:
    """解密。按信封前缀分派；解不开就抛异常，绝不静默返回空。"""
    if blob is None:
        return b""
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    blob = bytes(blob)
    if blob.startswith(DPAPI_MAGIC):
        try:
            return _dpapi(False, blob[len(DPAPI_MAGIC):], _dpapi_entropy(character_id, field))
        except TokenUnreadable:
            raise
        except Exception as exc:
            raise TokenUnreadable("DPAPI 解密失败：%r" % (exc,))
    if blob.startswith(AESGCM_MAGIC):
        try:
            raw = base64.b64decode(blob[len(AESGCM_MAGIC):], validate=False)
        except Exception as exc:
            raise TokenUnreadable("base64 解码失败：%r" % (exc,))
        if len(raw) < aesgcm.NONCE_BYTES + aesgcm.TAG_BYTES:
            raise TokenUnreadable("密文长度不足")
        nonce, body = raw[:aesgcm.NONCE_BYTES], raw[aesgcm.NONCE_BYTES:]
        try:
            if not _load_secret(create=False):
                raise TokenKeyMissing(
                    "数据是用 AES-GCM 加密的，但找不到密钥（设置 %s 或恢复 %s）"
                    % (KEY_ENV, key_file_path()))
            return aesgcm.gcm_decrypt(_key(), nonce, body, aad(character_id, field))
        except TokenKeyMissing:
            raise
        except aesgcm.InvalidTag:
            raise TokenUnreadable("认证标签校验失败（密钥不对、被篡改，或行列不匹配）")
    if blob.startswith(PLAIN_MAGIC):
        return blob[len(PLAIN_MAGIC):]
    # 没有任何前缀：本程序最早期的裸明文，继续兼容
    return blob


def is_encrypted(blob) -> bool:
    if blob is None:
        return False
    if isinstance(blob, str):
        blob = blob.encode("utf-8", "replace")
    blob = bytes(blob)
    return blob.startswith(DPAPI_MAGIC) or blob.startswith(AESGCM_MAGIC)


def is_current(blob) -> bool:
    """是否已经是「当前后端 + 当前信封」的格式（用于启动迁移判断）。"""
    if blob is None:
        return False
    if isinstance(blob, str):
        blob = blob.encode("utf-8", "replace")
    blob = bytes(blob)
    kind = backend()
    if kind == "dpapi":
        return blob.startswith(DPAPI_MAGIC)
    if kind == "aes-gcm":
        return blob.startswith(AESGCM_MAGIC)
    return blob.startswith(PLAIN_MAGIC)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def selftest(character_id: int = 42) -> dict:
    """自检：往返 + AAD 绑定 + 篡改检测。"""
    sample = b"refresh-token-selftest-\xe4\xb8\xad\xe6\x96\x87"
    info = {"platform": sys.platform, "backend": backend(),
            "dpapi_available": dpapi_available(),
            "key_file": key_file_path(),
            "key_file_exists": os.path.exists(key_file_path())}
    try:
        sealed = seal(sample, character_id, "refresh")
        info["encrypted"] = is_encrypted(sealed)
        info["roundtrip"] = unseal(sealed, character_id, "refresh") == sample
        info["sealed_bytes"] = len(sealed)
        info["envelope"] = "DPAPI1" if sealed.startswith(DPAPI_MAGIC) else \
                           ("enc:v2" if sealed.startswith(AESGCM_MAGIC) else "PLAIN1")
        # 下面几项是**故意**让它失败，所以先把错误状态清掉，免得报成告警
        _clear_error()
        # AAD 绑定：换成另一个角色或另一列必须解不开
        for label, kwargs in (("bound_to_character", {"character_id": character_id + 1}),
                              ("bound_to_field", {"field": "access"})):
            try:
                unseal(sealed, **kwargs)
                info[label] = False
            except Exception:
                info[label] = True
        # 篡改检测
        tampered = bytearray(sealed)
        tampered[-1] ^= 0x01
        try:
            unseal(bytes(tampered), character_id, "refresh")
            info["tamper_detected"] = False
        except Exception:
            info["tamper_detected"] = True
        _clear_error()          # 上述失败都是预期的
    except Exception as exc:  # pragma: no cover
        info["error"] = repr(exc)
        info["roundtrip"] = False
    if _LAST_ERROR:
        info["warning"] = _LAST_ERROR
    return info


if __name__ == "__main__":  # pragma: no cover
    import json
    print(json.dumps(selftest(), ensure_ascii=False, indent=2))
