"""令牌存储、刷新并发、关联状态（link_state）与所有权变更的回归测试。

这些是「照着 evesynapse 的做法修完，确认真的修对了」的凭证。重点：

    * 令牌在库里必须是密文，且**换一行就解不开**（AAD 绑定）
    * 启动迁移必须幂等，并且把解不开的行标成需重新登录而不是静默忽略
    * 同一个角色并发取令牌**只允许刷新一次**（EVE 每次刷新都会轮换 refresh token，
      并发刷新会让先写回的那个作废 —— 这正是修之前的真 bug）
    * 只有 EVE 明确说 invalid_grant 才标 token_dead；网络错误绝不能标
    * owner_hash 变化要暂停同步，且绝不用空值覆盖已知值
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as cfgmod        # noqa: E402
from app import secure, sso             # noqa: E402
from app.store import Store             # noqa: E402

CID = 900000001
OTHER = 900000002


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eve-token-test-")
        self._old_config = secure._config
        self._old_key = secure._key_cache
        self._old_keyfile = secure.key_file_path
        secure.key_file_path = lambda: os.path.join(self.tmp, "token.key")
        secure.configure({"token_encryption": "aes-gcm"})
        secure._key_cache = None
        self.store = Store(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        secure.key_file_path = self._old_keyfile
        secure.configure(self._old_config or {})
        secure._key_cache = self._old_key
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, character_id=CID, refresh=b"REFRESH-SECRET", access=b"ACCESS-SECRET",
             owner="hash-one", state=cfgmod.LINK_OK):
        self.store.upsert_character({
            "character_id": character_id, "name": "角色 %d" % character_id,
            "owner_hash": owner,
            "refresh_token": secure.seal(refresh, character_id, "refresh"),
            "access_token": secure.seal(access, character_id, "access"),
            "access_expires": 0, "scopes": "[]", "added_at": 1, "last_used": 1,
            "link_state": state, "encrypted": 1,
        })

    # ---------------- 加密落库 ----------------
    def test_tokens_are_stored_sealed(self):
        self._add()
        row = self.store.get_character(CID)
        self.assertTrue(secure.is_encrypted(row["refresh_token"]))
        self.assertTrue(secure.is_encrypted(row["access_token"]),
                        "访问令牌也必须是密文，不能明文入库")
        self.assertNotIn(b"REFRESH-SECRET", bytes(row["refresh_token"]))
        self.assertNotIn(b"ACCESS-SECRET", bytes(row["access_token"]))
        self.assertEqual(self.store.refresh_token(CID), "REFRESH-SECRET")
        self.assertEqual(self.store.access_token(CID), "ACCESS-SECRET")

    def test_ciphertext_cannot_be_moved_to_another_row(self):
        """AAD 绑定角色：把密文搬到另一行必须解不开。"""
        self._add()
        stolen = self.store.get_character(CID)["refresh_token"]
        self.store.upsert_character({
            "character_id": OTHER, "name": "另一个角色",
            "refresh_token": stolen, "access_expires": 0, "scopes": "[]",
            "added_at": 1, "last_used": 1, "link_state": cfgmod.LINK_OK,
        })
        self.assertIsNone(self.store.refresh_token(OTHER))
        self.assertEqual(self.store.refresh_token(CID), "REFRESH-SECRET")

    def test_ciphertext_cannot_be_swapped_between_fields(self):
        """AAD 绑定字段：把 refresh 的密文放到 access 列必须解不开。"""
        self._add()
        row = self.store.get_character(CID)
        self.store.x("UPDATE characters SET access_token=? WHERE character_id=?",
                     (row["refresh_token"], CID))
        self.assertIsNone(self.store.access_token(CID))

    # ---------------- 启动迁移 ----------------
    def test_migration_encrypts_plaintext_in_place(self):
        self.store.x(
            "INSERT INTO characters(character_id,name,refresh_token,access_token,"
            "access_expires,scopes,added_at,last_used,link_state)"
            " VALUES(?,?,?,?,0,'[]',1,1,?)",
            (OTHER, "旧角色", b"PLAIN1\x00plain-refresh", b"plain-access", cfgmod.LINK_OK))
        report = self.store.prepare_stored_tokens()
        self.assertGreaterEqual(report["migrated"], 1)
        row = self.store.get_character(OTHER)
        self.assertTrue(secure.is_encrypted(row["refresh_token"]))
        self.assertTrue(secure.is_encrypted(row["access_token"]))
        self.assertEqual(self.store.refresh_token(OTHER), "plain-refresh")
        self.assertEqual(self.store.access_token(OTHER), "plain-access")

    def test_migration_is_idempotent(self):
        self._add()
        self.store.prepare_stored_tokens()
        first = self.store.get_character(CID)
        self.store.prepare_stored_tokens()
        second = self.store.get_character(CID)
        # 已是当前格式的行必须逐字节不变
        self.assertEqual(bytes(first["refresh_token"]), bytes(second["refresh_token"]))
        self.assertEqual(bytes(first["access_token"]), bytes(second["access_token"]))

    def test_migration_marks_unreadable_rows(self):
        """解不开的令牌不能静默忽略：要标成 token_dead 并报出来。

        真实场景是「换了加密后端」或「密钥丢了」：这行是**另一个后端**的信封，
        当前后端解不开。这里用 DPAPI 信封模拟（在哪种平台上都解不开这段垃圾）。
        """
        self.store.x(
            "INSERT INTO characters(character_id,name,refresh_token,access_expires,"
            "scopes,added_at,last_used,link_state) VALUES(?,?,?,0,'[]',1,1,?)",
            (OTHER, "坏角色", secure.DPAPI_MAGIC + b"not-a-real-dpapi-blob",
             cfgmod.LINK_OK))
        report = self.store.prepare_stored_tokens()
        self.assertTrue(report["unreadable"], "应当报告解不开的行")
        self.assertEqual(report["unreadable"][0]["character_id"], OTHER)
        self.assertEqual(self.store.link_state(OTHER), cfgmod.LINK_TOKEN_DEAD)

    def test_migration_skips_rows_already_current(self):
        """已经是当前格式的行必须原样跳过 —— 否则会拿错误的密钥反复重封。"""
        self.store.x(
            "INSERT INTO characters(character_id,name,refresh_token,access_expires,"
            "scopes,added_at,last_used,link_state) VALUES(?,?,?,0,'[]',1,1,?)",
            (OTHER, "坏角色",
             secure.AESGCM_MAGIC + b"bm90LWEtcmVhbC1jaXBoZXJ0ZXh0LWF0LWFsbA==",
             cfgmod.LINK_OK))
        report = self.store.prepare_stored_tokens()
        self.assertEqual(report["unreadable"], [],
                         "当前格式的行不该被当作坏行（迁移不负责重新验证它）")

    # ---------------- 关联状态 ----------------
    def test_state_gate(self):
        self.assertTrue(self.store.state_allows_sync(""))
        self.assertTrue(self.store.state_allows_sync(cfgmod.LINK_OK))
        self.assertFalse(self.store.state_allows_sync(cfgmod.LINK_TOKEN_DEAD))
        self.assertFalse(self.store.state_allows_sync(cfgmod.LINK_OWNER_CHANGED))

    def test_mark_token_dead_does_not_overwrite_owner_changed(self):
        self._add(state=cfgmod.LINK_OWNER_CHANGED)
        marked = self.store.set_link_state(CID, cfgmod.LINK_TOKEN_DEAD, only_from_ok=True)
        self.assertFalse(marked)
        self.assertEqual(self.store.link_state(CID), cfgmod.LINK_OWNER_CHANGED)
        # 反过来：从 ok 可以被标记
        self._add(character_id=OTHER, state=cfgmod.LINK_OK)
        self.assertTrue(self.store.set_link_state(OTHER, cfgmod.LINK_TOKEN_DEAD,
                                                 only_from_ok=True))
        self.assertEqual(self.store.link_state(OTHER), cfgmod.LINK_TOKEN_DEAD)

    def test_public_characters_exposes_link_state(self):
        self._add(state=cfgmod.LINK_OWNER_CHANGED)
        entry = [c for c in self.store.public_characters() if c["character_id"] == CID][0]
        self.assertEqual(entry["link_state"], cfgmod.LINK_OWNER_CHANGED)
        self.assertFalse(entry["syncs"])
        self.assertTrue(entry["link_state_label"])


class OwnerHashTests(unittest.TestCase):
    """owner_hash 是角色的产权凭证：变化意味着「换主人了」。"""

    def test_first_seen(self):
        changed, stored = sso.owner_change_state(None, "hash-a")
        self.assertFalse(changed)
        self.assertEqual(stored, "hash-a")

    def test_same_owner(self):
        changed, stored = sso.owner_change_state("hash-a", "hash-a")
        self.assertFalse(changed)
        self.assertEqual(stored, "hash-a")

    def test_transferred(self):
        changed, stored = sso.owner_change_state("hash-a", "hash-b")
        self.assertTrue(changed)
        self.assertEqual(stored, "hash-b")

    def test_empty_never_overwrites_known(self):
        """CCP 偶尔不给 owner：绝不能把已知的产权记录抹成空。"""
        self.assertEqual(sso.owner_change_state("hash-a", "")[1], "hash-a")
        self.assertEqual(sso.owner_change_state("hash-a", None)[1], "hash-a")
        self.assertFalse(sso.owner_change_state("hash-a", "")[0])

    def test_both_empty_is_not_a_change(self):
        changed, stored = sso.owner_change_state("", None)
        self.assertFalse(changed)
        self.assertEqual(stored, "")


class SSOErrorClassificationTests(unittest.TestCase):
    def test_definitive_only_for_eve_vocabulary(self):
        self.assertTrue(sso.SSOError("x", code="invalid_grant").definitive)
        self.assertTrue(sso.SSOError("x", code="invalid_token").definitive)
        # 401 不算：可能是 client_id 配错，那是配置问题不是登录死了
        self.assertFalse(sso.SSOError("x", status=401).definitive)
        self.assertFalse(sso.SSOError("x", status=503).definitive)
        self.assertFalse(sso.SSOError("x").definitive)


class RefreshConcurrencyTests(unittest.TestCase):
    """同一个角色的刷新必须串行 —— 修之前两个线程会同时刷同一个 refresh token。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eve-refresh-test-")
        self._old_config = secure._config
        self._old_key = secure._key_cache
        self._old_keyfile = secure.key_file_path
        secure.key_file_path = lambda: os.path.join(self.tmp, "token.key")
        secure.configure({"token_encryption": "aes-gcm", "client_id": "testclient"})
        secure._key_cache = None
        self.store = Store(os.path.join(self.tmp, "test.db"))
        self.manager = sso.TokenManager(self.store, {"client_id": "testclient"})

    def tearDown(self):
        secure.key_file_path = self._old_keyfile
        secure.configure(self._old_config or {})
        secure._key_cache = self._old_key
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _expired_character(self, character_id=CID, state=cfgmod.LINK_OK):
        self.store.upsert_character({
            "character_id": character_id, "name": "角色", "owner_hash": "h",
            "refresh_token": secure.seal(b"old-refresh", character_id, "refresh"),
            "access_token": secure.seal(b"stale-access", character_id, "access"),
            "access_expires": 0, "scopes": "[]", "added_at": 1, "last_used": 1,
            "link_state": state, "encrypted": 1,
        })

    def _fake_refresh(self, counter, delay=0.15, fail=None):
        def fake(refresh_token, character_id=None):
            counter.append((character_id, refresh_token))
            time.sleep(delay)                      # 放大竞态窗口
            if fail is not None:
                raise fail
            token = "access-%d" % len(counter)
            with self.manager._lock:
                self.manager._memory[int(character_id)] = (token, int(time.time()) + 1200)
            return {"character_id": int(character_id), "name": "角色"}
        return fake

    def test_concurrent_token_for_refreshes_once(self):
        self._expired_character()
        calls = []
        self.manager.refresh = self._fake_refresh(calls)
        results = []
        errors = []

        def worker():
            try:
                results.append(self.manager.token_for(CID))
            except Exception as exc:               # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1,
                         "8 个线程只应触发 1 次刷新，实际 %d 次" % len(calls))
        self.assertEqual(len(results), 8)
        self.assertEqual(set(results), {results[0]}, "所有线程应拿到同一个令牌")
        self.assertEqual(calls[0][1], "old-refresh", "传给 EVE 的应是解密后的令牌")

    def test_different_characters_refresh_in_parallel(self):
        """不同角色不该互相阻塞（锁是按角色的）。"""
        self._expired_character(CID)
        self._expired_character(OTHER)
        calls = []
        self.manager.refresh = self._fake_refresh(calls, delay=0.2)
        started = time.time()
        threads = [threading.Thread(target=lambda c=c: self.manager.token_for(c))
                   for c in (CID, OTHER)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.time() - started
        self.assertEqual(len(calls), 2)
        self.assertLess(elapsed, 0.38, "两个角色应当并行刷新（串行会 >0.4 秒）")

    def test_fresh_token_does_not_refresh(self):
        self._expired_character()
        self.store.x("UPDATE characters SET access_expires=? WHERE character_id=?",
                     (int(time.time()) + 1200, CID))
        calls = []
        self.manager.refresh = self._fake_refresh(calls)
        token = self.manager.token_for(CID)
        self.assertEqual(token, "stale-access")
        self.assertEqual(calls, [], "没过期就不该打扰 EVE")

    def _patch_post_form(self, error):
        """替换真正发请求的那一层，这样 refresh() 里的分类逻辑才会被执行到。"""
        original = sso._post_form

        def fake(url, fields, user_agent, basic_auth=None, timeout=10):
            raise error

        sso._post_form = fake
        self.addCleanup(lambda: setattr(sso, "_post_form", original))

    def test_definitive_failure_marks_token_dead(self):
        self._expired_character()
        self._patch_post_form(sso.SSOError("bad", status=400, code="invalid_grant"))
        with self.assertRaises(sso.SSOError):
            self.manager.token_for(CID)
        self.assertEqual(self.store.link_state(CID), cfgmod.LINK_TOKEN_DEAD)

    def test_transient_failure_does_not_mark(self):
        """网络错误绝不能把角色标死 —— 否则断一次网就要用户重新登录。"""
        self._expired_character()
        self._patch_post_form(sso.SSOError("无法连接 EVE SSO：timed out"))
        with self.assertRaises(sso.SSOError):
            self.manager.token_for(CID)
        self.assertEqual(self.store.link_state(CID), cfgmod.LINK_OK)

    def test_client_config_error_does_not_mark(self):
        """401（client_id 配错）属于配置问题，也不该标死角色。"""
        self._expired_character()
        self._patch_post_form(sso.SSOError("bad client", status=401, code=None))
        with self.assertRaises(sso.SSOError):
            self.manager.token_for(CID)
        self.assertEqual(self.store.link_state(CID), cfgmod.LINK_OK)

    def test_definitive_failure_keeps_owner_changed(self):
        """更强的信号（角色已转手）不该被 token_dead 覆盖。"""
        self._expired_character()
        self.store.set_link_state(CID, cfgmod.LINK_OWNER_CHANGED)
        self.store.x("UPDATE characters SET link_state=? WHERE character_id=?",
                     (cfgmod.LINK_OWNER_CHANGED, CID))
        self._patch_post_form(sso.SSOError("bad", status=400, code="invalid_grant"))
        # 状态是 owner_changed，token_for 会先被闸门拦住（连刷新都不会发）
        with self.assertRaises(sso.SSOError):
            self.manager.token_for(CID)
        self.assertEqual(self.store.link_state(CID), cfgmod.LINK_OWNER_CHANGED)

    def test_link_state_gate_blocks_token_use(self):
        self._expired_character(state=cfgmod.LINK_TOKEN_DEAD)
        calls = []
        self.manager.refresh = self._fake_refresh(calls)
        with self.assertRaises(sso.SSOError) as ctx:
            self.manager.token_for(CID)
        self.assertIn("重新登录", str(ctx.exception))
        self.assertEqual(calls, [], "被暂停的角色不该再发任何请求")

    def test_owner_changed_blocks_token_use(self):
        self._expired_character(state=cfgmod.LINK_OWNER_CHANGED)
        calls = []
        self.manager.refresh = self._fake_refresh(calls)
        with self.assertRaises(sso.SSOError):
            self.manager.token_for(CID)
        self.assertEqual(calls, [])

    def test_token_state_reports_link_state(self):
        self._expired_character(state=cfgmod.LINK_OWNER_CHANGED)
        state = self.manager.token_state(CID)
        self.assertEqual(state["link_state"], cfgmod.LINK_OWNER_CHANGED)
        self.assertFalse(state["syncs"])
        self.assertTrue(state["link_state_label"])


class ScopeTierTests(unittest.TestCase):
    """分层授权：个人档 vs 个人+军团档。

    参考 ceve-industry.cn 的做法 —— 它的「授权人物」与「授权人物+军团」是两个
    长度不同的 scope 列表。按最小权限原则，只想算自己蓝图的人不必交出军团读取权。
    """

    def test_tier_definitions_are_consistent(self):
        self.assertEqual(len(cfgmod.ALL_SCOPES), len(set(cfgmod.ALL_SCOPES)),
                         "并集里不应有重复")
        self.assertLess(set(cfgmod.SCOPES_CHARACTER), set(cfgmod.ALL_SCOPES),
                        "个人档必须是完整档的真子集")
        self.assertEqual(set(cfgmod.ALL_SCOPES),
                         set(cfgmod.SCOPES_CHARACTER) | set(cfgmod.SCOPES_CORPORATION))
        self.assertEqual(set(cfgmod.SCOPES_CHARACTER) & set(cfgmod.SCOPES_CORPORATION),
                         set(), "两档不应重叠")

    def test_scopes_for_tier(self):
        self.assertEqual(cfgmod.scopes_for_tier("character"), cfgmod.SCOPES_CHARACTER)
        self.assertEqual(cfgmod.scopes_for_tier("corporation"), cfgmod.ALL_SCOPES)
        # 未知档位回退到完整档（宁多授权也不要静默少给），且返回的是副本
        self.assertEqual(cfgmod.scopes_for_tier("nonsense"), cfgmod.ALL_SCOPES)
        self.assertEqual(cfgmod.scopes_for_tier(None), cfgmod.ALL_SCOPES)
        listing = cfgmod.scopes_for_tier("character")
        listing.append("esi-bogus.v1")
        self.assertNotIn("esi-bogus.v1", cfgmod.SCOPES_CHARACTER, "不能改动全局定义")

    def test_character_tier_has_no_corporation_scope(self):
        for scope in cfgmod.SCOPES_CHARACTER:
            self.assertFalse(scope.startswith("esi-corporations."),
                             "个人档不该包含军团级 scope：%s" % scope)
        # esi-characters.read_corporation_roles.v1 是**角色**级（读自己在军团里的职位），
        # 名字里带 corporation 但属于个人档 —— 这里钉住这个反直觉的点
        self.assertIn("esi-characters.read_corporation_roles.v1", cfgmod.SCOPES_CHARACTER)
        self.assertNotIn("esi-characters.read_corporation_roles.v1",
                         cfgmod.SCOPES_CORPORATION)
        # 但要包含解析玩家建筑名所需的 scope（否则只能显示「设施 1029…」）
        self.assertIn("esi-universe.read_structures.v1", cfgmod.SCOPES_CHARACTER)
        self.assertNotIn("esi-assets.read_corporation_assets.v1", cfgmod.SCOPES_CHARACTER)
        self.assertNotIn("esi-industry.read_corporation_jobs.v1", cfgmod.SCOPES_CHARACTER)

    def test_login_url_requests_only_the_chosen_tier(self):
        tmp = tempfile.mkdtemp(prefix="eve-scope-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = Store(os.path.join(tmp, "test.db"))
        manager = sso.TokenManager(store, {"client_id": "test-client-id", "use_pkce": True})
        info = manager.login_url("http://localhost:9/callback", tier="character")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(info["url"]).query)
        scopes = query["scope"][0].split()
        self.assertEqual(sorted(scopes), sorted(cfgmod.SCOPES_CHARACTER))
        self.assertNotIn("esi-industry.read_corporation_jobs.v1", scopes)
        self.assertNotIn("esi-assets.read_corporation_assets.v1", scopes)
        # PKCE 仍然照常带上
        self.assertEqual(query["code_challenge_method"][0], "S256")
        self.assertTrue(query["code_challenge"][0])
        self.assertEqual(query["redirect_uri"][0], "http://localhost:9/callback")
        # 完整档
        info2 = manager.login_url("http://localhost:9/callback", tier="corporation")
        query2 = urllib.parse.parse_qs(urllib.parse.urlsplit(info2["url"]).query)
        self.assertEqual(len(query2["scope"][0].split()), len(cfgmod.ALL_SCOPES))
        # 两次请求的 state 必须是新的（否则等于重放窗口）
        self.assertNotEqual(query["state"][0], query2["state"][0])

    def test_scope_status_summary(self):
        from app.server import _scope_status
        character_only = _scope_status(cfgmod.SCOPES_CHARACTER)
        self.assertFalse(character_only["corp_ready"])
        self.assertEqual(character_only["scope_tier"], "character")
        self.assertEqual(len(character_only["corp_scopes_missing"]),
                         len(cfgmod.SCOPES_CORPORATION))
        self.assertEqual(character_only["missing_scopes"], character_only["corp_scopes_missing"])

        full = _scope_status(cfgmod.ALL_SCOPES)
        self.assertTrue(full["corp_ready"])
        self.assertEqual(full["scope_tier"], "corporation")
        self.assertEqual(full["missing_scopes"], [])

        nothing = _scope_status([])
        self.assertFalse(nothing["corp_ready"])
        self.assertEqual(nothing["scope_tier"], "custom")
        self.assertEqual(len(nothing["missing_scopes"]), len(cfgmod.ALL_SCOPES))


class ScopeAuditTests(unittest.TestCase):
    """scope 审计：本程序只需要读权限。

    用一份**真实世界的反面样本**做测试：某同类工具的授权链接一次请求了 60 项权限，
    里面包含以用户名义发邮件、控制客户端 UI、写舰队、改行星工厂等能力。
    （样本来自 https://login.eveonline.com/v2/oauth/authorize/ 的一条真实链接，
    见 docs/令牌与登录安全.md。）
    """

    # 真实链接里的 scope 列表（原文照抄）
    REAL_WORLD_HEAVY_SCOPES = (
        "publicData esi-calendar.read_calendar_events.v1 esi-location.read_location.v1 "
        "esi-location.read_ship_type.v1 esi-mail.organize_mail.v1 esi-mail.read_mail.v1 "
        "esi-mail.send_mail.v1 esi-skills.read_skills.v1 esi-skills.read_skillqueue.v1 "
        "esi-wallet.read_character_wallet.v1 esi-search.search_structures.v1 "
        "esi-clones.read_clones.v1 esi-characters.read_contacts.v1 "
        "esi-universe.read_structures.v1 esi-killmails.read_killmails.v1 "
        "esi-corporations.read_corporation_membership.v1 esi-assets.read_assets.v1 "
        "esi-planets.manage_planets.v1 esi-fleets.read_fleet.v1 esi-fleets.write_fleet.v1 "
        "esi-ui.open_window.v1 esi-ui.write_waypoint.v1 esi-fittings.read_fittings.v1 "
        "esi-markets.structure_markets.v1 esi-corporations.read_structures.v1 "
        "esi-characters.read_loyalty.v1 esi-characters.read_chat_channels.v1 "
        "esi-characters.read_medals.v1 esi-characters.read_standings.v1 "
        "esi-characters.read_agents_research.v1 esi-industry.read_character_jobs.v1 "
        "esi-markets.read_character_orders.v1 esi-characters.read_blueprints.v1 "
        "esi-characters.read_corporation_roles.v1 esi-location.read_online.v1 "
        "esi-contracts.read_character_contracts.v1 esi-clones.read_implants.v1 "
        "esi-characters.read_fatigue.v1 esi-corporations.track_members.v1 "
        "esi-characters.read_notifications.v1 esi-corporations.read_divisions.v1 "
        "esi-corporations.read_contacts.v1 esi-corporations.read_titles.v1 "
        "esi-corporations.read_blueprints.v1 esi-contracts.read_corporation_contracts.v1 "
        "esi-corporations.read_standings.v1 esi-corporations.read_starbases.v1 "
        "esi-industry.read_corporation_jobs.v1 esi-markets.read_corporation_orders.v1 "
        "esi-corporations.read_container_logs.v1 esi-industry.read_character_mining.v1 "
        "esi-planets.read_customs_offices.v1 esi-corporations.read_facilities.v1 "
        "esi-corporations.read_medals.v1 esi-characters.read_titles.v1 "
        "esi-alliances.read_contacts.v1 esi-characters.read_fw_stats.v1 "
        "esi-corporations.read_fw_stats.v1 esi-corporations.read_projects.v1"
    ).split()

    def test_our_default_scopes_are_clean(self):
        report = cfgmod.scope_audit_report()
        self.assertEqual(report["all_scopes_risky"], [],
                         "本程序的默认 scope 里不该有任何写/发送/UI 控制权限")
        self.assertTrue(report["clean"])
        self.assertEqual(report["character_risky"], [])
        self.assertEqual(report["corporation_risky"], [])

    def test_real_world_heavy_scope_list_is_flagged(self):
        """那份 60 项的真实链接必须被审计挑出问题。"""
        risky = cfgmod.audit_scopes(self.REAL_WORLD_HEAVY_SCOPES)
        self.assertGreaterEqual(len(risky), 5, "至少应挑出发邮件/UI/舰队/行星工厂等")
        for expected in ("esi-mail.send_mail.v1", "esi-ui.open_window.v1",
                         "esi-ui.write_waypoint.v1", "esi-fleets.write_fleet.v1",
                         "esi-planets.manage_planets.v1"):
            self.assertIn(expected, risky)
        # 只读权限不该被误报
        for safe in ("esi-assets.read_assets.v1", "esi-industry.read_character_jobs.v1",
                     "esi-characters.read_corporation_roles.v1",
                     "esi-universe.read_structures.v1"):
            self.assertNotIn(safe, risky)

    def test_our_scopes_are_far_fewer_and_all_read_only(self):
        """我们的权限数量远少于那份样本，且多出来的那一项也必须是只读。"""
        heavy = set(self.REAL_WORLD_HEAVY_SCOPES)
        ours = set(cfgmod.ALL_SCOPES)
        self.assertLess(len(ours), len(heavy) / 3,
                        "我们的权限数量应远少于那份 60 项的样本（%d vs %d）"
                        % (len(ours), len(heavy)))
        # 相对样本多出来的部分（军团资产是样本没要的读权限）
        extra = ours - heavy
        self.assertEqual(extra, {"esi-assets.read_corporation_assets.v1"},
                         "多出来的应当只有这一项读权限")
        for scope in extra:
            self.assertEqual(cfgmod.audit_scopes([scope]), [],
                             "多出来的也必须是只读：%s" % scope)

    def test_scope_audit_in_selftest_shape(self):
        from app.secret_check import scope_audit
        clean = scope_audit({"scopes": cfgmod.ALL_SCOPES})
        self.assertTrue(clean["clean"])
        self.assertIsNone(clean["warning"])
        dirty = scope_audit({"scopes": ["esi-mail.send_mail.v1"]})
        self.assertFalse(dirty["clean"])
        self.assertIn("esi-mail.send_mail.v1", dirty["risky"])
        self.assertIsNotNone(dirty["warning"])


class PendingLoginTests(unittest.TestCase):
    """「点了授权但还没完成」的等待态。

    参考 PyFA 的登录窗口：它一边显示「正在等待角色通过 EVE 登录」，
    一边把「粘贴令牌」的兜底放在同一个窗口里。我们照做 ——
    用户点完授权就被带走，回调失败时他回到程序必须**一眼看到**手动入口。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eve-pending-test-")
        self._old_config = secure._config
        self._old_key = secure._key_cache
        self._old_keyfile = secure.key_file_path
        secure.key_file_path = lambda: os.path.join(self.tmp, "token.key")
        secure.configure({"token_encryption": "aes-gcm", "client_id": "testclient"})
        secure._key_cache = None
        self.store = Store(os.path.join(self.tmp, "test.db"))
        self.manager = sso.TokenManager(self.store,
                                        {"client_id": "testclient", "use_pkce": True})

    def tearDown(self):
        secure.key_file_path = self._old_keyfile
        secure.configure(self._old_config or {})
        secure._key_cache = self._old_key
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_pending_initially(self):
        self.assertFalse(self.manager.pending_login()["pending"])

    def test_login_url_creates_pending_with_url(self):
        info = self.manager.login_url("http://localhost:9/callback", tier="character")
        pending = self.manager.pending_login()
        self.assertTrue(pending["pending"])
        self.assertEqual(pending["url"], info["url"],
                         "等待窗口要能重新给出授权链接")
        self.assertEqual(len(pending["scopes"]), len(cfgmod.SCOPES_CHARACTER))
        self.assertEqual(pending["redirect_uri"], "http://localhost:9/callback")
        self.assertGreater(pending["expires_in"], 500)
        self.assertEqual(pending["attempts"], 1)

    def test_multiple_attempts_counted(self):
        self.manager.login_url("http://localhost:9/callback", tier="character")
        second = self.manager.login_url("http://localhost:9/callback", tier="corporation")
        pending = self.manager.pending_login()
        self.assertEqual(pending["attempts"], 2)
        self.assertEqual(pending["url"], second["url"], "应展示最近一次")

    def test_cancel_clears_pending(self):
        self.manager.login_url("http://localhost:9/callback", tier="character")
        dropped = self.manager.cancel_pending()
        self.assertEqual(dropped, 1)
        self.assertFalse(self.manager.pending_login()["pending"])

    def test_expired_attempt_is_ignored(self):
        info = self.manager.login_url("http://localhost:9/callback", tier="character")
        # 手工把创建时间推到 11 分钟前（超过 10 分钟窗口）
        with self.manager._lock:
            self.manager._pending[info["state"]]["created_at"] -= 660
        self.assertFalse(self.manager.pending_login()["pending"],
                         "过期的登录尝试不该再让界面显示等待窗口")

    def test_exchange_consumes_pending(self):
        """回调真的用掉之后，等待窗口就该消失（否则会一直弹）。"""
        info = self.manager.login_url("http://localhost:9/callback", tier="character")
        self.assertTrue(self.manager.pending_login()["pending"])
        with self.manager._lock:
            self.manager._pending.pop(info["state"], None)   # 模拟 exchange_code 消费掉
        self.assertFalse(self.manager.pending_login()["pending"])


class CallbackPortTests(unittest.TestCase):
    """SSO 回调地址的端口处理（踩过的真坑）。

    背景：用 --port 临时换端口跑一次 + 随后任意一次保存设置，就把那个临时端口
    **永久**写进了 config.json；之后正常启动时端口与门户登记的 Callback URL
    不一致 —— EVE 会停在自己的错误页、**不跳回本机**，表现为「点登录没反应」。
    修法：运行时端口放在 `_runtime_port` 且 `save_config` 不落盘下划线键。
    """

    def test_runtime_port_does_not_persist(self):
        import json
        cfg = {"port": cfgmod.DEFAULT_PORT, cfgmod.RUNTIME_PORT_KEY: 9999}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            original = cfgmod.CONFIG_PATH
            try:
                cfgmod.CONFIG_PATH = path
                cfgmod.save_config(cfg)
                with open(path, encoding="utf-8") as fh:
                    saved = json.load(fh)
                self.assertNotIn(cfgmod.RUNTIME_PORT_KEY, saved,
                                 "运行时端口绝不能落盘")
                self.assertEqual(saved["port"], cfgmod.DEFAULT_PORT,
                                 "配置里的端口不该被本次运行改掉")
            finally:
                cfgmod.CONFIG_PATH = original

    def test_callback_url_uses_runtime_port(self):
        base = {"port": cfgmod.DEFAULT_PORT, "callback_path": "/callback"}
        self.assertEqual(cfgmod.callback_url(base),
                         "http://localhost:%d/callback" % cfgmod.DEFAULT_PORT)
        temp = dict(base)
        temp[cfgmod.RUNTIME_PORT_KEY] = 9999
        self.assertEqual(cfgmod.callback_url(temp),
                         "http://localhost:9999/callback")
        self.assertEqual(cfgmod.runtime_port(temp), 9999)

    def test_default_callback_uses_the_default_port(self):
        """默认配置生成的回调地址必须就是内置应用登记的那个（含端口）。

        （注册指南文档已移除：Client ID 内置后用户不再需要注册应用，
        所以这条不变量改成对着**默认配置**断言，而不是对着某份文档。）
        """
        default = dict(cfgmod.DEFAULT_CONFIG)
        self.assertEqual(cfgmod.runtime_port(default), cfgmod.DEFAULT_PORT)
        self.assertEqual(cfgmod.callback_url(default),
                         "http://localhost:%d/callback" % cfgmod.DEFAULT_PORT)

    def test_no_doc_tells_users_to_register_an_application(self):
        """文档里不该再留「去申请 Client ID」那套引导（内置后已无必要）。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertFalse(
            os.path.exists(os.path.join(root, "docs", "ESI应用注册指南.md")),
            "注册指南文件应已删除")
        for name in ("README.md",):
            pass  # 下面逐项检查；CHANGELOG 是历史记录，允许提到旧指南
        readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
        self.assertNotIn("ESI应用注册指南", readme,
                         "README 不该再链接已删除的注册指南")

    def test_shipped_config_uses_the_default_port(self):
        """随包发布的 config.json 不能带着别的端口（否则用户一启动就踩坑）。"""
        import json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "config.json")
        if not os.path.exists(path):
            self.skipTest("本地没有 config.json（干净检出）")
        with open(path, encoding="utf-8") as fh:
            shipped = json.load(fh)
        self.assertEqual(shipped.get("port"), cfgmod.DEFAULT_PORT,
                         "config.json 里的端口必须是默认 %d" % cfgmod.DEFAULT_PORT)
        self.assertNotIn("client_secret", {k: v for k, v in shipped.items() if v},
                         "随包配置里不该有 client_secret")


class BuiltinClientIdTests(unittest.TestCase):
    """内置 Client ID：普通用户不用注册应用就能登录。

    要求（用户提出的）：
      * Client ID **写进程序**，所以配置里不填也能登录
      * 界面上**不显示**它 —— 接口也不能把原值发给前端
      * 想用自己的应用时能被覆盖
    """

    def test_resolves_to_builtin_when_config_is_empty(self):
        self.assertTrue(cfgmod.BUILTIN_CLIENT_ID, "内置值不能为空")
        for cfg in ({}, {"client_id": ""}, {"client_id": None}, {"client_id": "***"}):
            self.assertEqual(cfgmod.resolve_client_id(cfg), cfgmod.BUILTIN_CLIENT_ID,
                             "配置为 %r 时应回落到内置值" % (cfg,))
            self.assertEqual(cfgmod.client_id_source(cfg), "builtin")

    def test_custom_value_overrides_builtin(self):
        cfg = {"client_id": "abc123def456"}
        self.assertEqual(cfgmod.resolve_client_id(cfg), "abc123def456")
        self.assertEqual(cfgmod.client_id_source(cfg), "custom")

    def test_builtin_value_in_config_counts_as_builtin(self):
        """配置里存了内置值本身 → 仍算「内置」，不该显示成「用户自定义」。"""
        cfg = {"client_id": cfgmod.BUILTIN_CLIENT_ID}
        self.assertEqual(cfgmod.resolve_client_id(cfg), cfgmod.BUILTIN_CLIENT_ID)
        self.assertEqual(cfgmod.client_id_source(cfg), "builtin")

    def test_sso_is_configured_without_any_config(self):
        """没有任何配置也必须算「已配置」，否则登录会被自己挡住。"""
        manager = self._manager({})
        self.assertTrue(manager.configured())
        self.assertEqual(manager.client_id, cfgmod.BUILTIN_CLIENT_ID)
        self.assertEqual(manager.client_id_source, "builtin")

    def test_authorize_url_carries_the_builtin_id(self):
        """内置值必须真的进到授权地址里（否则等于没内置）。"""
        manager = self._manager({})
        result = manager.login_url(tier="character")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(result["url"]).query)
        self.assertEqual((query.get("client_id") or [""])[0], cfgmod.BUILTIN_CLIENT_ID)
        self.assertEqual((query.get("code_challenge_method") or [""])[0], "S256")
        manager.cancel_pending()

    def _manager(self, config):
        """给个干净的内存库 + 临时密钥，避免碰到真实配置。"""
        tmp = tempfile.mkdtemp(prefix="eve-builtin-id-")
        self.addCleanup(shutil.rmtree, tmp, True)
        return sso.TokenManager(Store(os.path.join(tmp, "builtin.db")), config)

    def test_plaintext_secret_is_never_confused_with_a_client_id(self):
        """secret 与 client_id 是两回事：只填 secret 不该被当成 client_id。"""
        cfg = {"client_secret": "eat_whatever"}
        self.assertEqual(cfgmod.resolve_client_id(cfg), cfgmod.BUILTIN_CLIENT_ID)
        self.assertEqual(cfgmod.client_id_source(cfg), "builtin")


class DatabaseHealthTests(unittest.TestCase):
    """数据库损坏的自检与重建。

    真实案例：用户控制台里出现
        「读取 12733 的 sell 挂单失败：database disk image is malformed」
    这是 SQLite 文件损坏。问题是各处的读取失败都被 try/except 静默吞掉，
    用户只看到「某些数据刷不出来」，根本想不到是数据库坏了。
    """

    def test_healthy_database_reports_ok(self):
        tmp = tempfile.mkdtemp(prefix="eve-db-ok-")
        self.addCleanup(shutil.rmtree, tmp, True)   # Windows 下文件句柄可能还没释放，忽略
        store = Store(os.path.join(tmp, "ok.db"))
        ok, message = store.quick_check()
        self.assertTrue(ok, message)
        self.assertEqual(message, "ok")
        self.assertIsNone(store.init_error)

    def test_broken_file_is_moved_aside_and_rebuilt(self):
        """完全不是数据库时：不能崩，要把旧文件保留下来并建一个新库。"""
        tmp = tempfile.mkdtemp(prefix="eve-db-bad-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "broken.db")
        with open(path, "wb") as fh:
            fh.write(b"SQLite format 3\x00" + os.urandom(4096))
        store = Store(path)                      # 不应该抛异常
        self.addCleanup(store.close_all)
        self.assertIsNotNone(store.init_error, "应当记下打不开的原因")
        self.assertTrue(store.quick_check()[0], "重建后应当是可用的库")
        self.assertIsNotNone(store.q1("SELECT COUNT(*) n FROM types"))
        kept = [f for f in os.listdir(tmp) if "corrupt" in f]
        self.assertTrue(kept, "损坏的旧文件必须保留下来备查（不能直接删）")

    def test_rebuild_keeps_old_file_and_returns_schema(self):
        tmp = tempfile.mkdtemp(prefix="eve-db-rb-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = Store(os.path.join(tmp, "r.db"))
        store.kv_set("probe", {"a": 1})
        self.assertEqual(store.kv_get("probe"), {"a": 1})
        result = store.rebuild()
        self.assertTrue(result.get("path"))
        self.assertIsNone(store.kv_get("probe"), "重建后旧数据应当没了")
        self.assertIsNotNone(store.q1("SELECT COUNT(*) n FROM kv"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
