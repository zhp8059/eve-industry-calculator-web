"""纯标准库实现的 AES-256-GCM 与 HKDF-SHA256。

为什么自己写：Python 标准库**没有 AES**（`hashlib` 只有 SHA/HMAC 系列）。
本项目的策略是「零第三方依赖」，而把刷新令牌以明文落盘是不可接受的，
所以这里只实现 GCM 真正需要的两个原语：

    * AES-256 分组加密（GCM 只用它的加密方向做计数器模式）
    * GHASH（GF(2^128) 上的乘法，用于认证标签）

设计取舍：
    * S 盒不是抄表，而是**按定义现算**（GF(2^8) 求逆 + 仿射变换）。
      抄写 256 个字节的表极易出错，现算反而更可靠。
    * GHASH 用逐位乘法而非查表，速度慢但代码短、易验证。
      令牌只有几十到几百字节，性能完全不是问题。
    * 正确性由 `tests/test_crypto.py` 保证：既跑标准测试向量，
      也与 Node.js 的 `crypto` 做随机差分对拍（见该文件的说明）。

对外的两个函数：
    gcm_encrypt(key, nonce, plaintext, aad) -> ciphertext + tag
    gcm_decrypt(key, nonce, ciphertext_and_tag, aad) -> plaintext（校验失败抛 InvalidTag）
"""

from __future__ import annotations

import hashlib
import hmac
import os

__all__ = ["gcm_encrypt", "gcm_decrypt", "InvalidTag", "hkdf_sha256",
           "AES_KEY_BYTES", "NONCE_BYTES", "TAG_BYTES"]

AES_KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16


class InvalidTag(Exception):
    """认证标签校验失败：密文被篡改、密钥不对，或 AAD 不匹配。"""


# --------------------------------------------------------------------------
# AES-256 基础（按定义现算 S 盒，避免抄表出错）
# --------------------------------------------------------------------------

def _gf_mul8(a: int, b: int) -> int:
    """GF(2^8) 乘法，模 AES 的不可约多项式 x^8+x^4+x^3+x+1 (0x11B)。"""
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return product


def _build_sbox():
    """按定义生成 S 盒：先求 GF(2^8) 乘法逆元，再做仿射变换。"""
    # 用暴力搜索求逆元（256×256 次乘法，只在导入时做一次）
    inverse = [0] * 256
    for value in range(1, 256):
        for candidate in range(1, 256):
            if _gf_mul8(value, candidate) == 1:
                inverse[value] = candidate
                break
    table = []
    for value in range(256):
        inv = inverse[value]
        # 仿射变换：b_i = inv_i ^ inv_(i+4) ^ inv_(i+5) ^ inv_(i+6) ^ inv_(i+7) ^ c_i
        x = inv
        result = x
        for shift in (1, 2, 3, 4):
            result ^= ((x << shift) | (x >> (8 - shift))) & 0xFF
        table.append(result ^ 0x63)
    return table


SBOX = _build_sbox()
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D]


def _expand_key(key: bytes) -> list:
    """AES-256 密钥扩展：返回 60 个 4 字节轮密钥（Nr=14）。"""
    if len(key) != AES_KEY_BYTES:
        raise ValueError("AES-256 需要 32 字节密钥")
    nk, nr = 8, 14
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]                      # RotWord
            temp = [SBOX[b] for b in temp]                  # SubWord
            temp[0] ^= RCON[i // nk - 1]                    # 异或轮常量
        elif i % nk == 4:
            temp = [SBOX[b] for b in temp]                  # AES-256 额外的一次 SubWord
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return words


def _add_round_key(state: list, round_keys: list, round_index: int) -> None:
    """AddRoundKey：state[row + 4*col] ^= 第 round_index 轮第 col 个字的第 row 字节。

    注意索引方向：轮密钥的第 k 个「字」对应**一列**，字里的第 r 个字节对应
    **第 r 行**。一开始把这两个下标写反了，FIPS 向量立刻就不对 —— 这也是
    为什么必须拿外部实现做对拍，而不是只看代码「像是对的」。
    """
    base = 4 * round_index
    for col in range(4):
        word = round_keys[base + col]
        for row in range(4):
            state[row + 4 * col] ^= word[row]


def _encrypt_block(round_keys: list, block: bytes) -> bytes:
    """加密一个 16 字节分组。状态按列主序存放（state[row + 4*col]）。"""
    state = list(block)
    _add_round_key(state, round_keys, 0)

    for round_index in range(1, 14):
        state = [SBOX[b] for b in state]                    # SubBytes
        state = _shift_rows(state)                          # ShiftRows
        state = _mix_columns(state)                         # MixColumns
        _add_round_key(state, round_keys, round_index)

    state = [SBOX[b] for b in state]
    state = _shift_rows(state)
    _add_round_key(state, round_keys, 14)
    return bytes(state)


def _shift_rows(state: list) -> list:
    """第 r 行循环左移 r 个字节。"""
    out = list(state)
    for row in range(1, 4):
        row_values = [state[row + 4 * col] for col in range(4)]
        row_values = row_values[row:] + row_values[:row]
        for col in range(4):
            out[row + 4 * col] = row_values[col]
    return out


def _xtime(value: int) -> int:
    value <<= 1
    return (value ^ 0x1B) & 0xFF if value & 0x100 else value


def _mix_columns(state: list) -> list:
    out = list(state)
    for col in range(4):
        a = [state[4 * col + row] for row in range(4)]
        total = a[0] ^ a[1] ^ a[2] ^ a[3]
        out[4 * col + 0] = a[0] ^ total ^ _xtime(a[0] ^ a[1])
        out[4 * col + 1] = a[1] ^ total ^ _xtime(a[1] ^ a[2])
        out[4 * col + 2] = a[2] ^ total ^ _xtime(a[2] ^ a[3])
        out[4 * col + 3] = a[3] ^ total ^ _xtime(a[3] ^ a[0])
    return out


# --------------------------------------------------------------------------
# GHASH / GCM
# --------------------------------------------------------------------------

_R = 0xE1000000000000000000000000000000


def _gf_mul128(x: int, y: int) -> int:
    """GF(2^128) 乘法（GCM 的位序：最高位是「第一位」）。"""
    result = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            result ^= v
        if v & 1:
            v = (v >> 1) ^ _R
        else:
            v >>= 1
    return result


def _ghash(h: int, data: bytes) -> int:
    """对 16 字节对齐的数据做 GHASH。"""
    y = 0
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        if len(block) < 16:
            block = block + b"\x00" * (16 - len(block))
        y = _gf_mul128(y ^ int.from_bytes(block, "big"), h)
    return y


def _pad16(data: bytes) -> bytes:
    remainder = len(data) % 16
    return data if remainder == 0 else data + b"\x00" * (16 - remainder)


def _gcm_core(key: bytes, nonce: bytes):
    if len(nonce) != NONCE_BYTES:
        raise ValueError("GCM 的 nonce 必须是 %d 字节" % NONCE_BYTES)
    round_keys = _expand_key(key)
    h = int.from_bytes(_encrypt_block(round_keys, b"\x00" * 16), "big")
    # 96 位 nonce：J0 = nonce || 0x00000001
    j0 = nonce + b"\x00\x00\x00\x01"
    return round_keys, h, j0


def _tag(round_keys, h: int, j0: bytes, aad: bytes, ciphertext: bytes) -> bytes:
    lengths = (len(aad) * 8).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(8, "big")
    s = _ghash(h, _pad16(aad) + _pad16(ciphertext) + lengths)
    mask = int.from_bytes(_encrypt_block(round_keys, j0), "big")
    return (s ^ mask).to_bytes(16, "big")


def gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """返回 ciphertext || tag(16)。"""
    round_keys, h, j0 = _gcm_core(key, nonce)
    counter = int.from_bytes(j0, "big")
    out = bytearray()
    for offset in range(0, len(plaintext), 16):
        counter = (counter & ~0xFFFFFFFF) | ((counter + 1) & 0xFFFFFFFF)
        keystream = _encrypt_block(round_keys, counter.to_bytes(16, "big"))
        block = plaintext[offset:offset + 16]
        out += bytes(a ^ b for a, b in zip(block, keystream))
    ciphertext = bytes(out)
    return ciphertext + _tag(round_keys, h, j0, aad, ciphertext)


def gcm_decrypt(key: bytes, nonce: bytes, data: bytes, aad: bytes = b"") -> bytes:
    """data = ciphertext || tag，校验失败抛 InvalidTag。"""
    if len(data) < TAG_BYTES:
        raise InvalidTag("密文长度不足，缺少认证标签")
    ciphertext, tag = data[:-TAG_BYTES], data[-TAG_BYTES:]
    round_keys, h, j0 = _gcm_core(key, nonce)
    expected = _tag(round_keys, h, j0, aad, ciphertext)
    # 恒时比较，避免通过时间差逐字节猜标签
    if not hmac.compare_digest(expected, tag):
        raise InvalidTag("认证标签校验失败")
    counter = int.from_bytes(j0, "big")
    out = bytearray()
    for offset in range(0, len(ciphertext), 16):
        counter = (counter & ~0xFFFFFFFF) | ((counter + 1) & 0xFFFFFFFF)
        keystream = _encrypt_block(round_keys, counter.to_bytes(16, "big"))
        block = ciphertext[offset:offset + 16]
        out += bytes(a ^ b for a, b in zip(block, keystream))
    return bytes(out)


# --------------------------------------------------------------------------
# HKDF-SHA256（RFC 5869）
# --------------------------------------------------------------------------

def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def random_bytes(count: int) -> bytes:
    return os.urandom(count)
