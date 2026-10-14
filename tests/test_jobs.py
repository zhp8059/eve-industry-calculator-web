"""工业项目（industry jobs）的行为测试。

这里守的是三条**真实踩过的坑**：

    * 已交付（delivered）的项目以前会被存进本地、一直挂在列表里 —— 现在自动清除，
      但 **ready 必须保留**（做完还没领取，要去收）。
    * `resolve_structures` 这个方法曾经被误删（v0.8.0 加入 refresh_corp_structures
      时，`def` 那一行被吃掉，函数体变成上一个方法 return 之后的死代码）。
      调用处抛 AttributeError，却被 jobs_view 的 except 吞掉 ——
      表现是**工业项目的地点永远显示成「设施 1051…」**。
    * 读不到建筑名时要明确写「（未解析）」并带上原因，而不是丢一个裸 ID。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import inventory                    # noqa: E402
from app.store import Store                  # noqa: E402


class FakeESI:
    """只实现被用到的那几个方法。"""

    def __init__(self, jobs=None, names=None, structures=None):
        self._jobs = jobs or []
        self._names = names or {}
        self._structures = structures or {}

    def get_paged(self, path, params=None, **kwargs):
        return list(self._jobs)

    def get_data(self, path, **kwargs):
        for sid, info in self._structures.items():
            if str(sid) in path:
                return info
        raise RuntimeError("403 Forbidden")

    def names_for(self, ids):
        return {int(i): self._names[int(i)] for i in ids if int(i) in self._names}


def job(job_id, status, **extra):
    base = {"job_id": job_id, "status": status, "activity": "copying",
            "blueprint_type_id": 681, "product_type_id": None,
            "facility_id": 60003760, "runs": 1,
            "start_date": "2026-09-02T11:56:00Z", "end_date": "2026-10-12T13:11:00Z"}
    base.update(extra)
    return base


class TerminalJobCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eve-jobs-test-")
        self.store = Store(os.path.join(self.tmp, "jobs.db"))

    def test_delivered_jobs_are_dropped_ready_is_kept(self):
        esi = FakeESI(jobs=[job(1, "delivered"), job(2, "ready"), job(3, "active"),
                            job(4, "paused"), job(5, "cancelled")])
        data = inventory.IndustryData(self.store, esi, {})
        kept, dropped = data._drop_terminal_jobs(esi._jobs)
        self.assertEqual(dropped, 1, "只该滤掉 delivered")
        self.assertEqual([j["status"] for j in kept],
                         ["ready", "active", "paused", "cancelled"])
        self.assertNotIn("delivered", [j["status"] for j in kept])
        # ready 必须留着：做完还没领取，要去收
        self.assertIn("ready", [j["status"] for j in kept])

    def test_fetch_does_not_store_delivered_jobs(self):
        esi = FakeESI(jobs=[job(1, "delivered"), job(2, "active")])
        data = inventory.IndustryData(self.store, esi, {})
        data.fetch_character_jobs(12345)
        stored = self.store.jobs_of("char:12345")
        self.assertEqual([j["job_id"] for j in stored], [2],
                         "已交付的项目不该进本地库")

    def test_refetch_clears_previously_stored_delivered_rows(self):
        """老库里已有 delivered 行时，下次刷新也要清掉（不是只挡新的）。"""
        esi = FakeESI(jobs=[job(1, "delivered"), job(2, "active")])
        data = inventory.IndustryData(self.store, esi, {})
        self.store.replace_jobs("char:12345", data._drop_terminal_jobs(esi._jobs)[0]
                                + [data._normalize_job(job(9, "delivered"))])
        self.assertEqual(len(self.store.jobs_of("char:12345")), 2)
        data.fetch_character_jobs(12345)          # 再刷一次
        self.assertEqual([j["job_id"] for j in self.store.jobs_of("char:12345")], [2])


class FacilityNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eve-jobs-fac-")
        self.store = Store(os.path.join(self.tmp, "fac.db"))

    def test_resolve_structures_method_exists(self):
        """回归：这个方法曾经被误删，导致地点永远是「设施 <id>」。"""
        self.assertTrue(hasattr(inventory.IndustryData, "resolve_structures"),
                        "resolve_structures 必须存在 —— 它被误删过一次，"
                        "症状是工业项目的地点永远显示成「设施 1051…」")

    def test_jobs_view_resolves_names_from_local_cache(self):
        esi = FakeESI(jobs=[])
        data = inventory.IndustryData(self.store, esi, {})
        self.store.put_structures([(1051671530146, "我的索迪约", 30000142, 35827, 1)])
        self.store.replace_jobs("char:12345", [data._normalize_job(
            job(1, "active", facility_id=1051671530146))])
        view = data.jobs_view("char:12345")
        row = view["jobs"][0]
        self.assertEqual(row["facility_name"], "我的索迪约")
        self.assertTrue(row["facility_resolved"])

    def test_unresolved_facility_is_labelled_and_explained(self):
        """读不到名字时要写「（未解析）」，并给出 facility_resolved=False。"""
        esi = FakeESI(jobs=[])
        data = inventory.IndustryData(self.store, esi, {})
        self.store.replace_jobs("char:12345", [data._normalize_job(
            job(1, "active", facility_id=1051671530146))])
        view = data.jobs_view("char:12345")
        row = view["jobs"][0]
        self.assertIn("未解析", row["facility_name"])
        self.assertIn("1051671530146", row["facility_name"])
        self.assertFalse(row["facility_resolved"])

    def test_corp_structures_cache_is_used_without_docking_rights(self):
        """军团建筑表里的名字不需要停靠权限，应当优先用上。"""
        esi = FakeESI(jobs=[])
        data = inventory.IndustryData(self.store, esi, {})
        self.store.put_corp_structures([(1051671530146, 987654, "军团索迪约", 35827,
                                         30000142, "[]", None, "shield_vulnerable",
                                         None, None)])
        names = data.location_names([1051671530146])
        self.assertEqual(names.get(1051671530146), "军团索迪约")


if __name__ == "__main__":
    unittest.main(verbosity=2)
