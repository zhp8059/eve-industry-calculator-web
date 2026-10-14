"""加密相关的回归测试：AES-256-GCM、HKDF、以及令牌信封。

这一组测试是「重构 secure.py 时的安全网」，重点覆盖四类不变量：

    1. **正确性**：与权威实现对拍（Node 的 crypto），而不是只信任自己的实现。
       AES 的 AddRoundKey 下标写反过一次 —— 代码看着没问题，FIPS 向量立刻就不对。
    2. **AAD 绑定**：把密文搬到另一行、当作另一个字段来解，必须失败。
    3. **篡改检测**：改一个 bit 就必须解不开。
    4. **失败即报错**：缺密钥 / 解不开一律抛异常，绝不静默返回空。

对拍需要 Node；没有 Node 时自动跳过对拍部分，其余测试照跑。
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import aesgcm                      # noqa: E402
from app import secure                      # noqa: E402


def _find_node():
    """找 Node —— 只用于「与权威实现对拍」，找不到就跳过对拍部分。"""
    import glob
    env = os.environ.get("NODE_EXE")
    if env and os.path.exists(env):
        return env
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ]
    local = os.environ.get("LOCALAPPDATA") or ""
    home = os.path.expanduser("~")
    if local:
        candidates.append(os.path.join(local, "Programs", "nodejs", "node.exe"))
    # 便携运行时（DSH 自带等）可能藏在用户目录或 AppData 下的 .dsh 里，做有界搜索
    for root in (home, local):
        if not root:
            continue
        for pattern in (".dsh/**/node/bin/node.exe", ".dsh/**/nodejs/node.exe",
                        "**/node/bin/node.exe"):
            candidates.extend(glob.glob(os.path.join(root, pattern), recursive=True))
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


NODE = _find_node()


class AESBlockTests(unittest.TestCase):
    """AES-256 单分组：先用官方向量卡死，再与 Node 对拍。"""

    def test_fips_197_c3_vector(self):
        """FIPS-197 附录 C.3 的 AES-256 向量。"""
        key = bytes(range(32))
        plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(
            aesgcm._encrypt_block(aesgcm._expand_key(key), plaintext).hex(),
            "8ea2b7ca516745bfeafc49904b496089")

    def test_key_schedule_shape(self):
        rounds = aesgcm._expand_key(bytes(32))
        self.assertEqual(len(rounds), 60)          # AES-256：4×(14+1) 个字
        self.assertTrue(all(len(word) == 4 for word in rounds))

    def test_sbox_known_entries(self):
        # 生成式 S 盒的关键位置抽查，防止仿射变换写错
        self.assertEqual(aesgcm.SBOX[0x00], 0x63)
        self.assertEqual(aesgcm.SBOX[0x01], 0x7C)
        self.assertEqual(aesgcm.SBOX[0x53], 0xED)


class GCMTests(unittest.TestCase):
    def test_roundtrip_various_lengths(self):
        key, nonce = os.urandom(32), os.urandom(12)
        for length in (0, 1, 15, 16, 17, 31, 32, 33, 200, 1000):
            plaintext = os.urandom(length)
            blob = aesgcm.gcm_encrypt(key, nonce, plaintext, b"aad")
            self.assertEqual(aesgcm.gcm_decrypt(key, nonce, blob, b"aad"), plaintext,
                             "%d 字节往返失败" % length)
            self.assertEqual(len(blob), length + aesgcm.TAG_BYTES)

    def test_random_nonce_means_different_ciphertext(self):
        key = os.urandom(32)
        a = aesgcm.gcm_encrypt(key, os.urandom(12), b"same", b"")
        b = aesgcm.gcm_encrypt(key, os.urandom(12), b"same", b"")
        self.assertNotEqual(a, b, "nonce 每次必须随机，否则会重用密钥流")

    def test_aad_mismatch_fails(self):
        key, nonce = os.urandom(32), os.urandom(12)
        blob = aesgcm.gcm_encrypt(key, nonce, b"token", b"character:1:refresh")
        with self.assertRaises(aesgcm.InvalidTag):
            aesgcm.gcm_decrypt(key, nonce, blob, b"character:2:refresh")
        with self.assertRaises(aesgcm.InvalidTag):
            aesgcm.gcm_decrypt(key, nonce, blob, b"character:1:access")

    def test_tamper_detected(self):
        key, nonce = os.urandom(32), os.urandom(12)
        blob = bytearray(aesgcm.gcm_encrypt(key, nonce, b"token-value", b"aad"))
        for index in (0, len(blob) // 2, len(blob) - 1):
            mutated = bytearray(blob)
            mutated[index] ^= 0x01
            with self.assertRaises(aesgcm.InvalidTag):
                aesgcm.gcm_decrypt(key, nonce, bytes(mutated), b"aad")

    def test_wrong_key_fails(self):
        nonce = os.urandom(12)
        blob = aesgcm.gcm_encrypt(os.urandom(32), nonce, b"token", b"")
        with self.assertRaises(aesgcm.InvalidTag):
            aesgcm.gcm_decrypt(os.urandom(32), nonce, blob, b"")

    def test_short_ciphertext_rejected(self):
        with self.assertRaises(aesgcm.InvalidTag):
            aesgcm.gcm_decrypt(os.urandom(32), os.urandom(12), b"short", b"")

    def test_bad_key_or_nonce_length(self):
        with self.assertRaises(ValueError):
            aesgcm._expand_key(bytes(16))
        with self.assertRaises(ValueError):
            aesgcm.gcm_encrypt(os.urandom(32), os.urandom(8), b"x", b"")


class HKDFTests(unittest.TestCase):
    def test_rfc5869_test_case_1(self):
        """RFC 5869 附录 A.1（SHA-256）。"""
        okm = aesgcm.hkdf_sha256(bytes.fromhex("0b" * 22),
                                 bytes.fromhex("000102030405060708090a0b0c"),
                                 bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"), 42)
        self.assertEqual(
            okm.hex(),
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865")

    def test_empty_salt_and_info(self):
        self.assertEqual(len(aesgcm.hkdf_sha256(b"ikm", b"", b"", 32)), 32)

    def test_deterministic(self):
        a = aesgcm.hkdf_sha256(b"secret", b"salt", b"info", 32)
        b = aesgcm.hkdf_sha256(b"secret", b"salt", b"info", 32)
        self.assertEqual(a, b)


@unittest.skipIf(NODE is None, "没有 Node，跳过与权威实现的对拍")
class DifferentialTests(unittest.TestCase):
    """与 Node 内置 crypto 对拍 —— 不依赖我抄的测试向量是否正确。"""

    def test_against_node_crypto(self):
        node_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "_node_crypto_ref.js")
        if not os.path.exists(node_script):
            self.skipTest("缺少 _node_crypto_ref.js")

        random.seed(1234567)
        cases = []
        cases.append({"kind": "ecb", "key": bytes(range(32)).hex(),
                      "pt": bytes.fromhex("00112233445566778899aabbccddeeff").hex()})
        for _ in range(20):
            cases.append({"kind": "ecb", "key": os.urandom(32).hex(),
                          "pt": os.urandom(16).hex()})
        for length in (0, 1, 15, 16, 17, 32, 64, 160):
            for aad_length in (0, 1, 16, 20):
                cases.append({"kind": "gcm", "key": os.urandom(32).hex(),
                              "nonce": os.urandom(12).hex(),
                              "pt": os.urandom(length).hex(),
                              "aad": os.urandom(aad_length).hex()})
        for _ in range(20):
            cases.append({"kind": "hkdf", "ikm": os.urandom(24).hex(),
                          "salt": os.urandom(16).hex(), "info": os.urandom(10).hex(),
                          "length": random.choice([16, 32, 42, 64])})

        with tempfile.TemporaryDirectory() as tmp:
            cases_path = os.path.join(tmp, "cases.json")
            out_path = os.path.join(tmp, "out.json")
            with open(cases_path, "w", encoding="utf-8") as handle:
                json.dump(cases, handle)
            result = subprocess.run([NODE, node_script, cases_path, out_path],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                self.skipTest("Node 对拍脚本未能运行：%s" % (result.stderr or "")[:200])
            with open(out_path, encoding="utf-8") as handle:
                expected = json.load(handle)

        self.assertEqual(len(expected), len(cases))
        mismatched = []
        for case, want in zip(cases, expected):
            if case["kind"] == "ecb":
                got = aesgcm._encrypt_block(
                    aesgcm._expand_key(bytes.fromhex(case["key"])),
                    bytes.fromhex(case["pt"])).hex()
            elif case["kind"] == "gcm":
                got = aesgcm.gcm_encrypt(
                    bytes.fromhex(case["key"]), bytes.fromhex(case["nonce"]),
                    bytes.fromhex(case["pt"]), bytes.fromhex(case["aad"])).hex()
            else:
                got = aesgcm.hkdf_sha256(
                    bytes.fromhex(case["ikm"]), bytes.fromhex(case["salt"]),
                    bytes.fromhex(case["info"]), case["length"]).hex()
            if got != want:
                mismatched.append((case["kind"], got[:32], want[:32]))
        self.assertEqual(mismatched, [], "与 Node 的 crypto 结果不一致：%s" % mismatched[:3])


class EnvelopeTests(unittest.TestCase):
    """secure.seal / unseal 的信封、AAD 绑定与失败语义。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eve-secure-test-")
        self._old_config = secure._config
        self._old_key = secure._key_cache
        secure.configure({"token_encryption": "aes-gcm"})
        # 让密钥文件落在临时目录里，绝不碰真实 data/ 下的密钥
        self._old_keyfile = secure.key_file_path
        secure.key_file_path = lambda: os.path.join(self.tmp, "token.key")
        secure._key_cache = None

    def tearDown(self):
        secure.key_file_path = self._old_keyfile
        secure.configure(self._old_config or {})
        secure._key_cache = self._old_key
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_and_aad_binding(self):
        blob = secure.seal(b"refresh-abc", 42, "refresh")
        self.assertTrue(secure.is_encrypted(blob))
        self.assertNotIn(b"refresh-abc", blob, "密文里不能出现明文片段")
        self.assertEqual(secure.unseal(blob, 42, "refresh"), b"refresh-abc")
        with self.assertRaises(Exception):
            secure.unseal(blob, 43, "refresh")          # 换行
        with self.assertRaises(Exception):
            secure.unseal(blob, 42, "access")           # 换列

    def test_tamper_and_wrong_key(self):
        blob = bytearray(secure.seal(b"refresh-abc", 1, "refresh"))
        blob[-1] ^= 0x01
        with self.assertRaises(secure.TokenUnreadable):
            secure.unseal(bytes(blob), 1, "refresh")

    def test_empty_stays_empty(self):
        self.assertEqual(secure.seal(b"", 1, "refresh"), b"")
        self.assertEqual(secure.unseal(b"", 1, "refresh"), b"")

    def test_legacy_plaintext_still_readable(self):
        self.assertEqual(secure.unseal(b"PLAIN1\x00old-token", 1, "refresh"), b"old-token")
        self.assertEqual(secure.unseal(b"bare-old-token", 1, "refresh"), b"bare-old-token")

    def test_key_is_derived_deterministically(self):
        secure._key_cache = None
        first = secure.seal(b"x", 1, "refresh")
        secure._key_cache = None
        self.assertEqual(secure.unseal(first, 1, "refresh"), b"x")
        self.assertEqual(len(secure._key()), 32)

    def test_short_passphrase_rejected(self):
        os.environ[secure.KEY_ENV] = "too-short"
        try:
            secure._key_cache = None
            with self.assertRaises(secure.TokenKeyMissing):
                secure.seal(b"x", 1, "refresh")
        finally:
            os.environ.pop(secure.KEY_ENV, None)
            secure._key_cache = None

    def test_missing_key_is_reported_not_silent(self):
        """有密文但拿不到密钥：必须报 TokenKeyMissing，不能静默返回空。"""
        blob = secure.seal(b"secret-value", 7, "refresh")
        old_key = secure._key_cache
        old_file = secure._old_key_file if hasattr(secure, "_old_key_file") else None
        try:
            # 把密钥文件挪走并清缓存，模拟「换了机器 / 丢了密钥」
            os.rename(secure.key_file_path(), secure.key_file_path() + ".bak")
            secure._key_cache = None
            with self.assertRaises((secure.TokenKeyMissing, secure.TokenUnreadable)):
                secure.unseal(blob, 7, "refresh")
        finally:
            os.rename(secure.key_file_path() + ".bak", secure.key_file_path())
            secure._key_cache = old_key
        self.assertEqual(secure.unseal(blob, 7, "refresh"), b"secret-value")

    def test_selftest_reports_all_invariants(self):
        info = secure.selftest(42)
        self.assertTrue(info["roundtrip"])
        self.assertTrue(info["encrypted"])
        self.assertTrue(info["bound_to_character"])
        self.assertTrue(info["bound_to_field"])
        self.assertTrue(info["tamper_detected"])
        self.assertNotIn("warning", info, "自检不应把「故意的失败」报成告警")


if __name__ == "__main__":
    unittest.main(verbosity=2)
