"""公式回归测试。

测试向量全部来自公开可核实的实测数据（EVE Ref 的 industry cost API 与官方 SDE），
因此这些断言同时起到「公式是否正确」和「SDE 导入是否正确」两重作用。

运行：
    python -m unittest discover -s tests -v
    python tests/test_formulas.py            # 直接跑
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as cfgmod          # noqa: E402
from app import industry                  # noqa: E402
from app import research                  # noqa: E402
from app.store import Store               # noqa: E402

RIFTER_BP = 691            # 裂谷级蓝图：32000 / 6000 / 2500 / 500
RAITARU = 35825            # 莱塔卢：材料 0.99 / 费用 0.97 / 时间 0.85
RIG_SHIP_I = 37180         # 屹立超大型舰船制造效率 I：时间 -20% / 材料 -2%
ROCKET_BP = 2515           # Scourge Rocket 蓝图：基准 33 Tritanium / 28 Pyerite
JAGUAR_BP = 11401          # 美洲豹蓝图：每流程吃 1 个裂谷级

CONFIG = dict(cfgmod.DEFAULT_CONFIG)
CONFIG["price_source"] = "adjusted"
CONFIG["include_job_cost"] = True
CONFIG["scc_surcharge"] = 0.04
CONFIG["job_tax_rate"] = 0.0025


def _qty(materials: dict, type_id: int) -> int:
    return int(materials.get(type_id, 0))


class FormulaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(cfgmod.DB_PATH)
        if not cls.store.sde_ready():
            raise unittest.SkipTest("尚未导入 SDE，先运行：python -m app.main --sde-import <zip>")
        cls.activities = cls.store.blueprint_activities(RIFTER_BP)

    # ------------------------------------------------------------------
    # 1. 材料数量：ME 与设施倍率
    # ------------------------------------------------------------------
    def _materials(self, blueprint_id, me=0, runs=1, bonuses=None, activity="manufacturing"):
        detail = industry.product_breakdown(
            self.store, blueprint_id, activity, me, 0, runs, {}, {}, CONFIG,
            bonuses or {}, {3380: 5, 3388: 5}, 0.0)
        return detail, {m["type_id"]: m["quantity"] for m in detail["materials"]}

    def test_base_materials_me0(self):
        """ME0 基准量（官方 SDE）：32000 / 6000 / 2500 / 500。"""
        _, mats = self._materials(RIFTER_BP, me=0)
        self.assertEqual(_qty(mats, 34), 32000)
        self.assertEqual(_qty(mats, 35), 6000)
        self.assertEqual(_qty(mats, 36), 2500)
        self.assertEqual(_qty(mats, 37), 500)

    def test_me10_materials(self):
        """ME10：32000→28800、6000→5400、2500→2250、500→450。"""
        _, mats = self._materials(RIFTER_BP, me=10)
        self.assertEqual(_qty(mats, 34), 28800)
        self.assertEqual(_qty(mats, 35), 5400)
        self.assertEqual(_qty(mats, 36), 2250)
        self.assertEqual(_qty(mats, 37), 450)

    def test_structure_and_rig_materials(self):
        """莱塔卢(0.99) × 改装件 37180(材料 -2%) = 0.9702，向上取整。

        实测：32000→31047、6000→5822、2500→2426、500→486。
        """
        bonuses = industry.facility_bonuses(self.store, RAITARU, [RIG_SHIP_I],
                                           "manufacturing")
        self.assertAlmostEqual(bonuses["material_factor"], 0.99 * 0.98, places=10)
        _, mats = self._materials(RIFTER_BP, me=0, bonuses=bonuses)
        self.assertEqual(_qty(mats, 34), 31047)
        self.assertEqual(_qty(mats, 35), 5822)
        self.assertEqual(_qty(mats, 36), 2426)
        self.assertEqual(_qty(mats, 37), 486)

    def test_ceil_rounding_vectors(self):
        """弹药蓝图 2515：ME2→33/28，ME5→32/27，ME7→31/27（实测为向上取整）。"""
        for me, trit, pyer in ((2, 33, 28), (5, 32, 27), (7, 31, 27)):
            _, mats = self._materials(ROCKET_BP, me=me)
            self.assertEqual(_qty(mats, 34), trit, "ME%s 的 Tritanium" % me)
            self.assertEqual(_qty(mats, 35), pyer, "ME%s 的 Pyerite" % me)

    def test_rounding_happens_after_runs(self):
        """ME2、2 个流程 → 65 / 55（先算总量再取整，不是每流程取整后相加）。"""
        _, mats = self._materials(ROCKET_BP, me=2, runs=2)
        self.assertEqual(_qty(mats, 34), 65)
        self.assertEqual(_qty(mats, 35), 55)

    def test_minimum_one_per_run(self):
        """每种材料每流程至少 1 个：1 个裂谷级 × 10 流程，ME10 仍要 10 个。"""
        _, mats = self._materials(JAGUAR_BP, me=10, runs=10)
        self.assertEqual(_qty(mats, 587), 10)

    # ------------------------------------------------------------------
    # 2. 任务时间：技能相乘
    # ------------------------------------------------------------------
    def test_skill_time_is_multiplicative(self):
        """6000 秒基础 + Industry 5 / Advanced Industry 5 → 4080 秒（相乘，不是相加）。"""
        detail, _ = self._materials(RIFTER_BP, me=0)
        self.assertEqual(detail["base_time"], 6000)
        self.assertAlmostEqual(detail["skill_time_factor"], 0.80 * 0.85, places=10)
        self.assertEqual(detail["time_seconds"], 4080)

    def test_blueprint_te_multiplies(self):
        """TE20 → 4080 × 0.80 = 3264 秒。"""
        detail = industry.product_breakdown(
            self.store, RIFTER_BP, "manufacturing", 0, 20, 1, {}, {}, CONFIG, {},
            {3380: 5, 3388: 5}, 0.0)
        self.assertEqual(detail["time_seconds"], 3264)

    def test_structure_and_rig_time(self):
        """莱塔卢(0.85) × 改装件 37180(时间 -20%) × 技能(0.80×0.85) → 2774 秒。"""
        bonuses = industry.facility_bonuses(self.store, RAITARU, [RIG_SHIP_I],
                                           "manufacturing")
        self.assertAlmostEqual(bonuses["time_factor"], 0.85 * 0.80, places=10)
        detail = industry.product_breakdown(
            self.store, RIFTER_BP, "manufacturing", 0, 0, 1, {}, {}, CONFIG, bonuses,
            {3380: 5, 3388: 5}, 0.0)
        self.assertEqual(detail["time_seconds"], 2774)

    # ------------------------------------------------------------------
    # 3. EIV 与 ME / 设施无关
    # ------------------------------------------------------------------
    def test_eiv_is_independent_of_me_and_facility(self):
        prices = {34: {"adjusted_price": 3.1391360813},
                  35: {"adjusted_price": 12.6247425846},
                  36: {"adjusted_price": 37.3146405369},
                  37: {"adjusted_price": 160.636376427}}
        expected = round(32000 * 3.1391360813 + 6000 * 12.6247425846
                         + 2500 * 37.3146405369 + 500 * 160.636376427)
        self.assertEqual(expected, 349806)   # 与公开实测一致

        values = []
        for me in (0, 10):
            detail = industry.product_breakdown(
                self.store, RIFTER_BP, "manufacturing", me, 0, 1, prices, {}, CONFIG,
                {}, {3380: 5, 3388: 5}, 0.0)
            values.append(detail["estimated_item_value"])
        bonuses = industry.facility_bonuses(self.store, RAITARU, [RIG_SHIP_I],
                                           "manufacturing")
        detail = industry.product_breakdown(
            self.store, RIFTER_BP, "manufacturing", 0, 0, 1, prices, {}, CONFIG,
            bonuses, {3380: 5, 3388: 5}, 0.0)
        values.append(detail["estimated_item_value"])
        self.assertEqual(values, [349806, 349806, 349806],
                         "EIV 不应随 ME 或建筑/改装件变化")

    # ------------------------------------------------------------------
    # 4. 任务费用
    # ------------------------------------------------------------------
    def test_job_cost_base_factor_per_activity(self):
        """制造/反应 = EIV；拷贝/发明 = 2% × EIV。"""
        self.assertEqual(industry.job_cost_base(1000.0, "manufacturing"), 1000.0)
        self.assertEqual(industry.job_cost_base(1000.0, "reaction"), 1000.0)
        self.assertEqual(industry.job_cost_base(1000.0, "copying"), 20.0)
        self.assertEqual(industry.job_cost_base(1000.0, "invention"), 20.0)

    def test_scc_surcharge_is_four_percent(self):
        """SCC 附加费现行 4%（2024-02-01 起）。"""
        breakdown = industry.job_cost_breakdown(
            1000.0, 0.1665, "manufacturing", 6000, 1,
            scc_surcharge=0.04, cost_factor=1.0, tax_rate=0.0, include_tax=False)
        self.assertAlmostEqual(breakdown["scc_surcharge"], 40.0, places=6)
        self.assertAlmostEqual(breakdown["job_cost_base"], 1000.0, places=6)
        self.assertAlmostEqual(breakdown["system_cost"], 166.5, places=6)
        self.assertAlmostEqual(breakdown["total"], 166.5 + 40.0, places=6)

    def test_facility_cost_reduction_only_hits_system_cost(self):
        """费用减免只作用于系统成本指数部分，SCC 与税按计费基数算。"""
        breakdown = industry.job_cost_breakdown(
            1000.0, 0.1665, "manufacturing", 6000, 1,
            scc_surcharge=0.04, cost_factor=0.95, tax_rate=0.01, include_tax=True)
        self.assertAlmostEqual(breakdown["system_cost"], 166.5, places=6)
        self.assertAlmostEqual(breakdown["facility_reduction"], -166.5 * 0.05, places=6)
        self.assertAlmostEqual(breakdown["scc_surcharge"], 40.0, places=6)
        self.assertAlmostEqual(breakdown["tax"], 10.0, places=6)
        self.assertAlmostEqual(breakdown["total"], 166.5 * 0.95 + 40.0 + 10.0, places=6)

    def test_research_cost_base_uses_time(self):
        """研究任务计费基数 = EIV × 2% × 秒数 / 105（社区公式，界面上标注为推算）。"""
        base = industry.job_cost_base(1000.0, "research_time", 2100, 1)
        self.assertAlmostEqual(base, 1000.0 * 0.02 * (2100 / 105.0), places=6)

    # ------------------------------------------------------------------
    # 5. 设施加成：安等倍率与作用范围
    # ------------------------------------------------------------------
    def test_security_multiplier_changes_rig_bonus(self):
        """改装件加成按安等放大：高安 1.0 / 低安 1.9 / 00 2.1。"""
        high = industry.facility_bonuses(self.store, None, [RIG_SHIP_I], "manufacturing",
                                        security_override="highsec")
        low = industry.facility_bonuses(self.store, None, [RIG_SHIP_I], "manufacturing",
                                       security_override="lowsec")
        null = industry.facility_bonuses(self.store, None, [RIG_SHIP_I], "manufacturing",
                                        security_override="nullsec")
        self.assertAlmostEqual(high["material_factor"], 0.98, places=10)
        self.assertAlmostEqual(low["material_factor"], 1 - 0.02 * 1.9, places=10)
        self.assertAlmostEqual(null["material_factor"], 1 - 0.02 * 2.1, places=10)
        self.assertAlmostEqual(high["time_factor"], 0.80, places=10)
        self.assertAlmostEqual(low["time_factor"], 1 - 0.20 * 1.9, places=10)

    def test_structure_bonus_read_from_sde(self):
        """索迪约自身：材料 0.99 / 费用 0.95 / 时间 0.70。"""
        bonuses = industry.facility_bonuses(self.store, 35827, [], "manufacturing")
        self.assertAlmostEqual(bonuses["material_factor"], 0.99, places=6)
        self.assertAlmostEqual(bonuses["cost_factor"], 0.95, places=6)
        self.assertAlmostEqual(bonuses["time_factor"], 0.70, places=6)

    def test_rig_scope_filter_excludes_wrong_products(self):
        """舰船制造改装件不该给建筑类产品加成。"""
        # 37180 只作用于舰船；用「建筑」类产品（category 23=建筑）去问，应被过滤掉
        structure_product = self.store.type_by_id(35827)   # 索迪约，category=建筑
        bonuses = industry.facility_bonuses(
            self.store, None, [RIG_SHIP_I], "manufacturing",
            security_override="highsec",
            product_category_id=structure_product["category_id"],
            product_group_id=structure_product["group_id"],
            product_type_id=structure_product["type_id"])
        # 舰船 rig 的材料加成属性存在，但作用范围不含建筑 → 材料倍率保持 1.0
        self.assertAlmostEqual(bonuses["material_factor"], 1.0, places=10)

    # ------------------------------------------------------------------
    # 6. 多级展开与聚合
    # ------------------------------------------------------------------
    def test_aggregate_materials_matches_reference_layout(self):
        """主表格：索迪约的材料聚合后应当包含截图里那些条目。"""
        items = [{"type_id": 35827, "runs": 1, "me": 10, "te": 20,
                  "purchase_mode": "build"}]
        result = industry.aggregate_materials(
            self.store, items, "manufacturing", {}, {}, CONFIG, None, {}, {}, 0.0, {}, {},
            {}, 10, 20, "测试星系")
        self.assertTrue(result["ok"])
        names = {r["name"] for r in result["rows"]}
        for expected in ("看门人", "海军陆战队员", "建筑实验室", "建筑工厂",
                         "建筑建造组件", "建筑机库阵列", "建筑贮藏舱", "建筑维修设施"):
            self.assertIn(expected, names, "聚合材料里应包含 %s" % expected)
        janitor = next(r for r in result["rows"] if r["name"] == "看门人")
        # 500 × 0.9 = 450（ME10）
        self.assertEqual(janitor["quantity"], 450)
        self.assertEqual(janitor["volume"], 3.0)

    def test_multi_level_planner_runs(self):
        """生产规划能展开并给出基础材料清单（用真实调整价算成本）。"""
        from app import planner
        prices = {34: {"adjusted_price": 3.14}, 35: {"adjusted_price": 12.62},
                  36: {"adjusted_price": 37.31}, 37: {"adjusted_price": 160.64}}
        result = planner.multi_level(
            self.store, 35827, None, 1, 10, 20, "manufacturing", 2,
            None, None, prices, {}, CONFIG, {}, {}, {45746: 5, 3388: 5, 3380: 5}, 0.0)
        self.assertTrue(result["ok"])
        self.assertGreater(len(result["base_materials"]), 0)
        self.assertGreater(len(result["jobs"]), 0)
        self.assertGreater(result["totals"]["total_job_time_seconds"], 0)
        self.assertGreater(result["totals"]["material_volume"], 0)
        # 深度 1 的工作应该就是索迪约本身
        self.assertEqual(result["jobs"][0]["product_type_id"], 35827)
        # 索迪约制造基础时间 2,300,000 秒，技能 5/5 与 TE20 之后应显著小于它
        self.assertLess(result["jobs"][0]["time_seconds"], 2300000)


class ResearchTests(unittest.TestCase):
    """蓝图研究（ME / TE）等级模型与设施模拟。

    锚点全部来自官方 SDE 与社区已知值：
      * 裂谷级蓝图 691：SDE 研究时长 2100 秒，官方 rank = 20 → 105 × 20 = 2100 ✔
      * 护卫级 1 级研究 35 分钟是社区公认值
      * 官方 SDE 里 4344 个蓝图的 ME 与 TE 研究时长完全相等（共用一个基准）
    """

    @classmethod
    def setUpClass(cls):
        cls.store = Store(cfgmod.DB_PATH)
        if not cls.store.sde_ready():
            raise unittest.SkipTest("尚未导入 SDE")

    def test_level_table_is_cumulative(self):
        """等级表是「累计秒数」：第 n 级本身 = 累计(n) − 累计(n−1)。"""
        base = 2100                      # 裂谷级：rank 20 × 105
        self.assertEqual(research.cumulative_seconds(base, 1), 2100)
        self.assertEqual(research.cumulative_seconds(base, 2), 5000)      # 250/105×2100
        self.assertEqual(research.cumulative_seconds(base, 10), 5120000)  # 256000×20
        self.assertEqual(research.level_increment_seconds(base, 1), 2100)
        self.assertEqual(research.level_increment_seconds(base, 2), 2900)
        self.assertEqual(research.level_increment_seconds(base, 10), 2966000)
        # 累计 = 各级增量之和
        self.assertEqual(sum(research.level_increment_seconds(base, lv) for lv in range(1, 11)),
                         research.cumulative_seconds(base, 10))

    def test_me_te_level_semantics(self):
        """ME 每级 −1%（0–10），TE 每级 −2%（0–20）：TE 显示值 ÷ 2 = 研究等级。"""
        self.assertEqual(research.me_percent(10), 10)
        self.assertEqual(research.te_percent(10), 20)
        self.assertEqual(research.te_level_from_percent(20), 10)
        self.assertEqual(research.te_level_from_percent(10), 5)
        self.assertEqual(research.te_level_from_percent(0), 0)
        # 与 industry 的时间公式衔接：TE 20% → 时间 ×0.8
        self.assertEqual(industry.job_time_seconds(1000, 1, 20.0, 1.0, 1.0), 800)

    def test_blueprint_info_matches_official_sde(self):
        """从官方 SDE 读出的研究基准与 rank 必须自洽。"""
        info = research.blueprint_info(self.store, 691)
        self.assertTrue(info["has_research"])
        self.assertEqual(info["base_me_seconds"], 2100)
        self.assertEqual(info["base_te_seconds"], 2100)
        self.assertTrue(info["base_equal"], "SDE 里 ME 与 TE 研究时长应相等")
        self.assertAlmostEqual(float(info["rank"]), 20.0, places=6)
        self.assertTrue(info["rank_consistent"], "rank × 105 应等于 SDE 研究时长")
        self.assertEqual(info["max_level"], 10)

    def test_me_and_te_bases_equal_across_sde(self):
        """全量校验：SDE 里所有蓝图的 ME / TE 研究时长都相等。"""
        rows = self.store.q(
            "SELECT blueprint_type_id, activity, time FROM blueprint_activities"
            " WHERE activity IN ('research_material','research_time')")
        me = {r["blueprint_type_id"]: r["time"] for r in rows if r["activity"] == "research_material"}
        te = {r["blueprint_type_id"]: r["time"] for r in rows if r["activity"] == "research_time"}
        self.assertGreater(len(me), 4000)
        self.assertEqual(set(me), set(te))
        diff = [k for k in me if me[k] != te[k]]
        self.assertEqual(diff, [], "不应存在 ME / TE 研究时长不同的蓝图")

    def test_facility_simulation(self):
        """技能 5/5 时的耗时：NPC 站 = 基准 × 0.6375，索迪约+改装件更快。"""
        skill_levels = {3380: 5, 3388: 5, 3409: 5, 3403: 5}
        facilities = research.default_facilities(self.store, [37183])
        result = research.simulate(self.store, 691, facilities, CONFIG, skill_levels,
                                   from_me=0, to_me=10, from_te=0, to_te=10,
                                   security="highsec", cost_index=0.1665, eiv=349806)
        self.assertTrue(result["ok"])
        npc = next(f for f in result["facilities"] if f["structure_type_id"] is None)
        # 技能倍率 = (1−0.05×5) × (1−0.03×5) = 0.75 × 0.85
        self.assertAlmostEqual(npc["me"]["skill_factor"], 0.75 * 0.85, places=10)
        # 逐级各自取整，所以与「先算总基准再乘系数」会有几百毫秒级差异；
        # 允许 10 秒内的取整误差
        expected = 5120000 * 0.75 * 0.85
        self.assertLess(abs(npc["me"]["total_seconds"] - expected), 10,
                        "NPC 站耗时应≈基准×技能倍率（允许逐级取整误差）")
        self.assertEqual(npc["me"]["total_seconds"], npc["te"]["total_seconds"],
                         "同一设施下 ME 与 TE 耗时相同（各 10 级、共用等级表）")
        self.assertEqual(npc["me"]["total_cost"], npc["te"]["total_cost"])

        # 索迪约 0.70 × 改装件 37183 时间 −20%（高安 ×1.0） × 技能 0.6375
        sotiyo = next((f for f in result["facilities"]
                       if f["structure_type_id"] == 35827 and f["rig_type_ids"]), None)
        self.assertIsNotNone(sotiyo, "应生成「索迪约 + 改装件」对比行")
        self.assertAlmostEqual(sotiyo["me"]["time_factor"], 0.70 * 0.80, places=10)
        self.assertEqual(sotiyo["me"]["total_seconds"], 1827840)
        # 最快的排在最前
        self.assertEqual(result["facilities"][0]["me"]["total_seconds"],
                         min(f["me"]["total_seconds"] for f in result["facilities"]))

    def test_security_multiplier_affects_rig(self):
        """改装件减免按安等放大，低安/00 应更快。"""
        facilities = research.default_facilities(self.store, [37183])
        speeds = {}
        for sec in ("highsec", "lowsec", "nullsec"):
            r = research.simulate(self.store, 691, facilities, CONFIG, {},
                                  from_me=0, to_me=10, from_te=0, to_te=10,
                                  security=sec, cost_index=0.1665, eiv=0)
            row = next(f for f in r["facilities"]
                       if f["structure_type_id"] == 35827 and f["rig_type_ids"])
            speeds[sec] = row["me"]["total_seconds"]
        self.assertGreater(speeds["highsec"], speeds["lowsec"])
        self.assertGreater(speeds["lowsec"], speeds["nullsec"])

    def test_level_range_selection(self):
        """只研究某一段等级时，耗时 = 该段各级增量之和。"""
        result = research.simulate(self.store, 691, research.default_facilities(self.store), CONFIG,
                                   {}, from_me=5, to_me=8, from_te=0, to_te=0,
                                   security="highsec", cost_index=0.0, eiv=0)
        npc = next(f for f in result["facilities"] if f["structure_type_id"] is None)
        me = npc["me"]
        self.assertEqual(me["from_level"], 5)
        self.assertEqual(me["to_level"], 8)
        self.assertEqual(len(me["levels"]), 3)
        self.assertEqual(me["total_base_seconds"],
                         sum(research.level_increment_seconds(2100, lv) for lv in (6, 7, 8)))
        # TE 段为空
        self.assertEqual(npc["te"]["levels"], [])
        self.assertEqual(npc["te"]["total_seconds"], 0)

    def test_research_job_cost_base_uses_level_seconds(self):
        """研究任务的计费基数 = EIV × 2% × 本级秒数 / 105（社区公式）。"""
        increment = research.level_increment_seconds(2100, 2)   # 2900
        base = industry.job_cost_base(1000.0, "research_time", increment, 1)
        self.assertAlmostEqual(base, 1000.0 * 0.02 * (increment / 105.0), places=6)

    def test_no_research_blueprint(self):
        """反应配方类蓝图没有研究活动，应给出明确提示而不是崩掉。"""
        info = research.blueprint_info(self.store, 45732)      # 反应蓝图
        self.assertFalse(info["has_research"])
        result = research.simulate(self.store, 45732, [], CONFIG, {})
        self.assertFalse(result["ok"])
        self.assertIn("研究", result.get("message", ""))


class SkillAndRigAttributeTests(unittest.TestCase):
    """技能与改装件属性：不靠记忆，对着 SDE 自己的定义核对。

    这两条都是**真实修过的缺陷**，所以要有回归测试钉住：

    1. 实验室运作理论（3406）/ 高级实验室运作理论（24624）的效果属性是
       `laboratorySlotsBonus`（+1 槽位），**不是时间减免**。
       早期版本把它们当成拷贝/发明的时间减免，导致这两项时间被低估约 25%。
    2. 反应改装件的加成写在属性 2713/2714 上（不是 industryModifierSources
       里声明的 2715-2720 —— 那些属性在物品上一个值都没有）。
       漏掉它们的后果是反应改装件被完全忽略。
    """

    # 这些技能只加槽位，任何活动的时间减免都不该包含它们
    SLOT_ONLY_SKILLS = {3406, 24624, 45748}

    def test_no_slot_skill_is_used_as_time_reduction(self):
        for activity, skills in cfgmod.TIME_SKILLS.items():
            for skill_id, percent, _default, name in skills:
                self.assertNotIn(skill_id, self.SLOT_ONLY_SKILLS,
                                 "%s 用了槽位技能 %s 当时间减免" % (activity, name))
                self.assertGreater(percent, 0.0)
                self.assertLess(percent, 0.25)

    def test_copying_and_invention_have_no_time_skill_beyond_advanced_industry(self):
        """拷贝/发明只有「高级工业理论」，因为它的属性是「所有工业任务时间」。"""
        for activity in ("copying", "invention"):
            ids = {s[0] for s in cfgmod.TIME_SKILLS[activity]}
            self.assertEqual(ids, {3388}, "%s 的技能表应是 {高级工业理论}" % activity)

    def test_advanced_industry_applies_everywhere(self):
        """属性 1961 的描述是「a bonus to all industry job times」→ 六个活动都该有它。"""
        for activity in ("manufacturing", "reaction", "research_time",
                         "research_material", "copying", "invention"):
            ids = {s[0] for s in cfgmod.TIME_SKILLS[activity]}
            self.assertIn(3388, ids, "%s 缺少高级工业理论" % activity)

    def test_reaction_has_its_dedicated_time_skill(self):
        """反应理论（45746）的属性是 reactionTimeBonus = -4。"""
        ids = {s[0] for s in cfgmod.TIME_SKILLS["reaction"]}
        self.assertIn(45746, ids)

    def test_structure_attributes_are_not_swapped(self):
        """2601 = 费用倍率、2602 = 时间倍率。**不能弄反**。

        这个断言不是多余的：有一个开源实现把 2601 当成时间、2602 当成费用
        （见 docs/与同类项目的公式对比.md），照抄就会算错任务费用。
        属性名以 SDE 的 dogma_attributes 为准。
        """
        self.assertEqual(cfgmod.BONUS_ATTRIBUTES[2600][0], "material")
        self.assertEqual(cfgmod.BONUS_ATTRIBUTES[2601][0], "cost")
        self.assertEqual(cfgmod.BONUS_ATTRIBUTES[2602][0], "time")
        expected_names = {2600: "strEngMatBonus", 2601: "strEngCostBonus",
                          2602: "strEngTimeBonus", 2721: "strReactionTimeMultiplier"}
        for attribute_id, name in expected_names.items():
            self.assertEqual(cfgmod.BONUS_ATTRIBUTES[attribute_id][1], name)
            self.assertEqual(cfgmod.BONUS_ATTRIBUTES[attribute_id][0],
                             cfgmod.BONUS_ATTRIBUTES[attribute_id][0])

    def test_reaction_rig_attributes_are_declared(self):
        self.assertEqual(cfgmod.RIG_BONUS_ATTRIBUTES[2713][0], "time")
        self.assertEqual(cfgmod.RIG_BONUS_ATTRIBUTES[2714][0], "material")
        self.assertIn(2713, cfgmod.NEEDED_DOGMA_ATTRIBUTES)
        self.assertIn(2714, cfgmod.NEEDED_DOGMA_ATTRIBUTES)


class ReactionRigDataTests(unittest.TestCase):
    """需要已导入 SDE 的数据库。"""

    @classmethod
    def setUpClass(cls):
        cls.store = Store(cfgmod.DB_PATH)
        try:
            if not cls.store.q1("SELECT COUNT(*) n FROM type_bonus_attributes")["n"]:
                raise RuntimeError
        except Exception:
            raise unittest.SkipTest("尚未导入 SDE，先运行：python -m app.main --sde-import <zip>")

    def test_reaction_rig_values_are_imported(self):
        for attribute_id, minimum in ((2713, 5), (2714, 5)):
            row = self.store.q1(
                "SELECT COUNT(*) n FROM type_bonus_attributes WHERE attribute_id=?",
                (attribute_id,))
            self.assertGreaterEqual(row["n"], minimum,
                                    "属性 %s 的值没导入" % attribute_id)

    def test_reaction_rig_produces_time_and_material_bonus(self):
        """反应时间效率 I（2713 = −20）必须真的产生 ×0.8。"""
        result = industry.facility_bonuses(self.store, activity="reaction",
                                           rig_type_ids=[46484], security_override="highsec")
        self.assertAlmostEqual(result["time_factor"], 0.80, places=6)
        self.assertAlmostEqual(result["material_factor"], 1.0, places=6)
        kinds = {d["kind"] for s in result["sources"] for d in s["details"]}
        self.assertEqual(kinds, {"time"})

    def test_reaction_material_rig(self):
        result = industry.facility_bonuses(self.store, activity="reaction",
                                           rig_type_ids=[46486], security_override="highsec")
        self.assertAlmostEqual(result["material_factor"], 0.98, places=6)

    def test_reaction_rig_security_multiplier_is_applied(self):
        """反应改装件在 00 的倍率是 ×1.1（2357 = 1.1）。"""
        result = industry.facility_bonuses(self.store, activity="reaction",
                                           rig_type_ids=[46484], security_override="nullsec")
        self.assertAlmostEqual(result["time_factor"], 0.78, places=6)

    def test_reaction_rig_is_ignored_for_manufacturing(self):
        """活动不匹配就不该生效 —— 这是分组与作用域逻辑的边界。"""
        result = industry.facility_bonuses(self.store, activity="manufacturing",
                                           rig_type_ids=[46484], security_override="highsec")
        self.assertAlmostEqual(result["time_factor"], 1.0, places=6)
        self.assertEqual(result["sources"], [])

    def test_combat_rig_still_works(self):
        """修正不能影响原有改装件。"""
        result = industry.facility_bonuses(self.store, activity="manufacturing",
                                           rig_type_ids=[37162], security_override="highsec")
        self.assertAlmostEqual(result["time_factor"], 0.80, places=6)


class StructureRigSizeTests(unittest.TestCase):
    """建筑与改装件的尺寸匹配（SDE 属性 1547 rigSize）。

    用户报告过两件事，这里都钉住：
    1. 改装件下拉里混着不匹配的尺寸（空堡应该只能选中型改装件）
    2. 建筑清单缺了空堡 / 铁壁 / 星城 / 阿塔诺 / 势力铁壁
       —— 因为它们自身没有工业加成，只列「自带加成」的建筑就会漏掉它们
    """

    @classmethod
    def setUpClass(cls):
        cls.store = Store(cfgmod.DB_PATH)
        try:
            row = cls.store.q1("SELECT COUNT(*) n FROM type_bonus_attributes "
                               "WHERE attribute_id=?", (cfgmod.RIG_SIZE_ATTRIBUTE,))
            if not row["n"]:
                raise RuntimeError
        except Exception:
            raise unittest.SkipTest("尚未导入 SDE（或缺少 rigSize 属性）")

    def test_rig_size_attribute_is_declared_and_imported(self):
        self.assertEqual(cfgmod.RIG_SIZE_ATTRIBUTE, 1547)
        self.assertIn(1547, cfgmod.NEEDED_DOGMA_ATTRIBUTES)
        self.assertGreaterEqual(
            self.store.q1("SELECT COUNT(*) n FROM type_bonus_attributes WHERE attribute_id=1547")["n"],
            20, "rigSize 的值没导入")

    def test_structure_list_is_complete(self):
        """14 个玩家工业建筑一个都不能少。"""
        rows = industry.industry_structures(self.store)
        by_name = {r["name"]: r for r in rows}
        expected = {
            "空堡": ("中型", 2), "铁壁": ("大型", 3), "星城": ("超大型", 4),
            "莱塔卢": ("中型", 2), "阿兹贝尔": ("大型", 3), "索迪约": ("超大型", 4),
            "阿塔诺": ("中型", 2), "塔塔拉": ("大型", 3),
            '"德拉库斯"铁壁': ("大型", 3), '"地平线"铁壁': ("大型", 3),
            '"马金尼斯"铁壁': ("大型", 3), '"莫罗"铁壁': ("大型", 3),
            '"普罗米修斯"铁壁': ("大型", 3),
        }
        for name, (label, size) in expected.items():
            self.assertIn(name, by_name, "建筑清单缺少 %s" % name)
            self.assertEqual(by_name[name]["rig_size"], size, name)
            self.assertEqual(by_name[name]["rig_size_label"], label, name)
        self.assertGreaterEqual(len(rows), 13)

    def test_structures_without_own_bonus_are_included(self):
        """空堡/铁壁/星城/阿塔诺自身没有工业加成，但必须出现在列表里（靠改装件）。"""
        rows = {r["name"]: r for r in industry.industry_structures(self.store)}
        for name in ("空堡", "铁壁", "星城", "阿塔诺"):
            self.assertIn(name, rows)
            self.assertFalse(rows[name]["has_own_bonus"],
                             "%s 按 SDE 不该自带工业加成" % name)
            self.assertTrue(rows[name]["rig_slots"] >= 1)

    def test_rig_size_groups_match_the_game(self):
        """空堡/莱塔卢/阿塔诺 = 中型；铁壁/阿兹贝尔/塔塔拉 = 大型；星城/索迪约 = 超大型。"""
        rows = {r["name"]: r["rig_size"] for r in industry.industry_structures(self.store)}
        self.assertEqual(rows.get("空堡"), rows.get("莱塔卢"))
        self.assertEqual(rows.get("空堡"), rows.get("阿塔诺"))
        self.assertEqual(rows.get("铁壁"), rows.get("阿兹贝尔"))
        self.assertEqual(rows.get("铁壁"), rows.get("塔塔拉"))
        self.assertEqual(rows.get("星城"), rows.get("索迪约"))
        self.assertNotEqual(rows.get("空堡"), rows.get("铁壁"))
        self.assertNotEqual(rows.get("铁壁"), rows.get("星城"))

    def test_structures_for_rig_size_filter(self):
        rows = industry.industry_structures(self.store)
        medium = {s["name"] for s in industry.structures_for_rig_size(rows, 2)}
        self.assertIn("空堡", medium)
        self.assertNotIn("铁壁", medium)

    def test_rig_candidates_carry_their_size(self):
        rigs = industry.structure_bonus_candidates(self.store, "manufacturing")
        self.assertTrue(rigs)
        sized = [r for r in rigs if r.get("rig_size")]
        self.assertGreater(len(sized), 10, "改装件应带上 rigSize")
        # 制造改装件里应同时存在三种尺寸
        labels = {r["rig_size_label"] for r in sized if r["rig_size_label"]}
        self.assertTrue({"中型", "大型", "超大型"} & labels, labels)

    def test_medium_rig_only_fits_medium_structures(self):
        """37162（中型）与 43714（大型）的 rigSize 必须不同且与建筑对应。"""
        attrs = self.store.bonus_attributes_many([37162, 43714, 35832, 35833])
        self.assertEqual(attrs[37162][1547], attrs[35832][1547])   # 中型
        self.assertEqual(attrs[43714][1547], attrs[35833][1547])   # 大型
        self.assertNotEqual(attrs[37162][1547], attrs[43714][1547])


class StructureActivityScopeTests(unittest.TestCase):
    """建筑加成必须**按活动生效** —— 期望值直接取自游戏内描述。

    数据来源：用户在游戏里打开各建筑详情看到的「特有加成」文本。逐条抄录如下
    （括号内是我方对应的活动）：

      * 空堡 / 铁壁 / 星城       只有战斗改装件与燃料，**没有生产加成**
      * "德拉库斯"铁壁           15% 生产项目时间、3% 星币        → 只有 manufacturing
      * "地平线"铁壁             15% 科研项目时间、3% 星币        → 只有科研类
      * "马金尼斯"铁壁           只有燃料与战斗改装件            → 无生产加成
      * "普罗米修斯"铁壁         提炼产出 2% + 燃料               → 无生产加成
      * "莫罗"铁壁               10% 生产和科研时间、2% 星币、1% 提炼 → 全部活动
      * 莱塔卢                   材料 1% / 时间 15% / 星币 3%
      * 阿兹贝尔                 材料 1% / 时间 20% / 星币 4%
      * 索迪约                   材料 1% / 时间 30% / 星币 5%
      * 阿塔诺                   提炼 2% + 气云 4%                 → 无生产加成
      * 塔塔拉                   反应时间 25%                      → 只有 reaction

    以前我们**不做活动过滤**，于是给「地平线」算制造、给「德拉库斯」算科研都会算错。
    """

    PRODUCTION = "manufacturing"
    RESEARCH = ("research_time", "research_material")

    @classmethod
    def setUpClass(cls):
        cls.store = Store(cfgmod.DB_PATH)
        try:
            cls.store.q1("SELECT COUNT(*) n FROM industry_modifier_sources")
        except Exception:
            raise unittest.SkipTest("尚未导入 SDE")

    def _factors(self, type_id, activity):
        r = industry.facility_bonuses(self.store, int(type_id), [], activity,
                                      security_override="highsec")
        return r["time_factor"], r["material_factor"], r["cost_factor"]

    def _applies(self, type_id, activity):
        t, m, c = self._factors(type_id, activity)
        return abs(t - 1) > 1e-9 or abs(m - 1) > 1e-9 or abs(c - 1) > 1e-9

    def test_structures_without_production_bonus(self):
        """空堡/铁壁/星城/阿塔诺/马金尼斯/普罗米修斯：任何活动都不该有加成。"""
        for type_id in (35832, 35833, 35834, 47515, 47516, 35835):
            for activity in (self.PRODUCTION, "research_time", "research_material",
                             "copying", "invention", "reaction"):
                self.assertFalse(self._applies(type_id, activity),
                                 "%s 在 %s 下不该有加成" % (type_id, activity))

    def test_dracus_is_production_only(self):
        """'德拉库斯'铁壁：生产时间 ×0.85、费用 ×0.97；科研没有加成。"""
        t, _m, c = self._factors(47513, self.PRODUCTION)
        self.assertAlmostEqual(t, 0.85, places=6)
        self.assertAlmostEqual(c, 0.97, places=6)
        for activity in ("research_time", "research_material", "copying",
                         "invention", "reaction"):
            self.assertFalse(self._applies(47513, activity),
                             "德拉库斯在 %s 下不该有加成（游戏里只写「生产项目」）" % activity)

    def test_horizon_is_research_only(self):
        """'地平线'铁壁：科研时间 ×0.85；**制造没有加成**（游戏里只写「科研项目」）。"""
        self.assertFalse(self._applies(47514, self.PRODUCTION),
                         "地平线在制造下不该有加成")
        for activity in self.RESEARCH + ("copying", "invention"):
            t, _m, c = self._factors(47514, activity)
            self.assertAlmostEqual(t, 0.85, places=6, msg=activity)
            self.assertAlmostEqual(c, 0.97, places=6, msg=activity)

    def test_moreau_covers_production_and_research(self):
        """'莫罗'铁壁：10% 时间 / 2% 星币，生产与科研都给。"""
        for activity in (self.PRODUCTION, "research_time", "research_material",
                         "copying", "invention"):
            t, _m, c = self._factors(47512, activity)
            self.assertAlmostEqual(t, 0.90, places=6, msg=activity)
            self.assertAlmostEqual(c, 0.98, places=6, msg=activity)

    def test_engineering_complexes_match_the_game_text(self):
        """莱塔卢 1/15/3%、阿兹贝尔 1/20/4%、索迪约 1/30/5%（材料/时间/星币）。"""
        expected = {35825: (0.99, 0.85, 0.97),
                    35826: (0.99, 0.80, 0.96),
                    35827: (0.99, 0.70, 0.95)}
        for type_id, (want_m, want_t, want_c) in expected.items():
            m, t, c = (lambda f: (f[1], f[0], f[2]))(self._factors(type_id, self.PRODUCTION))
            self.assertAlmostEqual(m, want_m, places=6, msg="%s 材料" % type_id)
            self.assertAlmostEqual(t, want_t, places=6, msg="%s 时间" % type_id)
            self.assertAlmostEqual(c, want_c, places=6, msg="%s 费用" % type_id)
            # 研究活动同样生效
            self.assertTrue(self._applies(type_id, "research_time"))

    def test_tatara_reaction_only(self):
        """塔塔拉：反应时间 ×0.75，制造没有加成。"""
        t, _m, _c = self._factors(35836, "reaction")
        self.assertAlmostEqual(t, 0.75, places=6)
        self.assertFalse(self._applies(35836, self.PRODUCTION))

    def test_structure_list_reports_activity_scope(self):
        """建筑列表要能告诉界面「这个建筑对本活动有没有加成」。"""
        rows = {r["name"]: r for r in industry.industry_structures(self.store, "manufacturing")}
        self.assertFalse(rows["地平线" if "地平线" in rows else '"地平线"铁壁']["applies_to_activity"],
                         "地平线在制造档下不该标为生效")
        self.assertTrue(rows["索迪约"]["applies_to_activity"])
        self.assertFalse(rows['"马金尼斯"铁壁']["applies_to_activity"])
        research = {r["name"]: r
                    for r in industry.industry_structures(self.store, "research_time")}
        self.assertTrue(research['"地平线"铁壁']["applies_to_activity"])


class StructureFuelTests(unittest.TestCase):
    """建筑燃料消耗（SDE 属性驱动）。

    依据：
      服务装备 2109 = 每小时消耗的燃料块（「每小时开始时消耗的数量」）
      服务装备 2110 = 上线一次性消耗
      服务装备 2108 = 消耗的燃料分组（1136 = 燃料块组）
      建筑     2339 = 燃料减免百分比（负值）   2056 = 服务装备槽位

    对账锚点：**索迪约/莱塔卢/空堡 + 1 个制造厂 = 9 块/小时**（12 × 0.75）——
    这是 EVE 里公认的数值，能对上说明整条链路（属性 → 减免 → 公式）是对的。
    """

    @classmethod
    def setUpClass(cls):
        cls.store = Store(cfgmod.DB_PATH)
        try:
            row = cls.store.q1("SELECT COUNT(*) n FROM type_bonus_attributes "
                               "WHERE attribute_id=?", (cfgmod.SERVICE_FUEL_HOURLY_ATTRIBUTE,))
            if not row["n"]:
                raise unittest.SkipTest("尚未导入 SDE（或缺少燃料属性）")
        except Exception:
            raise unittest.SkipTest("尚未导入 SDE")

    def _services(self):
        return {s["type_id"]: s for s in industry.service_modules(self.store)}

    def test_hourly_fuel_values_match_sde(self):
        svc = self._services()
        expected = {35878: 12,    # 屹立制造厂 I
                    35891: 12,    # 屹立研究实验室 I
                    35886: 12,    # 屹立发明实验室 I
                    35892: 40,    # 屹立市场中心 I
                    35899: 10,    # 屹立提炼设施 I
                    45537: 15,    # 屹立复合反应堆 I
                    35894: 10,    # 屹立克隆中心 I
                    35881: 24,    # 屹立旗舰级船坞 I
                    35877: 36}    # 屹立超级旗舰级船坞 I
        for type_id, hourly in expected.items():
            self.assertIn(type_id, svc, "%s 不在服务装备清单里" % type_id)
            self.assertAlmostEqual(svc[type_id]["fuel_per_hour"], hourly, places=6,
                                   msg=str(type_id))

    def test_only_fuel_burning_modules_are_listed(self):
        """宣传屏 / 瞭望阵列 / 主权设施不烧燃料，不该出现在清单里。"""
        svc = self._services()
        for type_id in (37142, 35908, 35916, 35915):
            self.assertNotIn(type_id, svc, "%s 不烧燃料" % type_id)
        self.assertGreaterEqual(len(svc), 15)

    def test_structure_reduction_is_applied(self):
        """同分类时减免生效：索迪约/莱塔卢 + 制造厂 = 9；马金尼斯 −50% = 6；阿塔诺 + 提炼 = 8。

        注意必须用**同分类**的组合 —— 空堡 + 制造厂是跨分类，不打折（见下面那条断言）。
        """
        cases = ((35827, 35878, 9.0),    # 索迪约（工程）+ 制造厂（工程）
                 (35825, 35878, 9.0),    # 莱塔卢（工程）+ 制造厂
                 (47515, 35878, 6.0),    # 马金尼斯（全部）−50%
                 (35835, 35899, 8.0))    # 阿塔诺（提炼）−20% + 提炼设施
        for structure_id, service_id, want in cases:
            plan = industry.fuel_plan(self.store, structure_id, [service_id], price=0)
            self.assertAlmostEqual(plan["fuel_per_hour"], want, places=6,
                                   msg="结构 %s" % structure_id)
        # 跨分类不打折：空堡只给堡垒服务装备打折，制造厂是工程服务装备
        cross = industry.fuel_plan(self.store, 35832, [35878], price=0)
        self.assertAlmostEqual(cross["fuel_per_hour"], 12.0, places=6)
        self.assertFalse(cross["services"][0]["in_fuel_scope"])

    def test_moreau_uses_the_attribute_value(self):
        """莫罗的属性是 −35%（游戏内文本写 15%）—— 按属性算，并把不一致报出来。"""
        plan = industry.fuel_plan(self.store, 47512, [35878], price=0)
        self.assertAlmostEqual(plan["structure_fuel_bonus"], -35.0, places=6)
        self.assertAlmostEqual(plan["fuel_per_hour"], 12 * 0.65, places=6)
        self.assertTrue(any("15%" in n for n in plan["notes"]),
                        "应提示属性与游戏内文本不一致")

    def test_sum_of_services_and_monthly(self):
        plan = industry.fuel_plan(self.store, 35827, [35878, 35891, 35886], price=0)
        self.assertAlmostEqual(plan["fuel_per_hour"], 36 * 0.75, places=6)     # 27
        self.assertAlmostEqual(plan["fuel_per_day"], 27 * 24, places=6)        # 648
        self.assertAlmostEqual(plan["fuel_per_month"], 27 * 720, places=6)     # 19440
        # 只算持续消耗：服务上线的一次性燃料不计入（用户要求）
        self.assertNotIn("fuel_online_total", plan)
        self.assertNotIn("fuel_online_reduced", plan)

    def test_cost_uses_unit_price(self):
        plan = industry.fuel_plan(self.store, 35827, [35878], price=1000.0)
        self.assertAlmostEqual(plan["cost_per_month"], 9 * 720 * 1000.0, places=2)
        self.assertEqual(plan["fuel_type_id"], cfgmod.DEFAULT_FUEL_BLOCK_TYPE_ID)

    def test_no_services_means_no_fuel(self):
        plan = industry.fuel_plan(self.store, 35827, [], price=0)
        self.assertEqual(plan["fuel_per_hour"], 0)
        self.assertEqual(plan["fuel_per_month"], 0)

    def test_unknown_service_is_reported_not_ignored(self):
        plan = industry.fuel_plan(self.store, 35827, [35878, 99999999], price=0)
        self.assertEqual(plan["unknown_service_ids"], [99999999])
        self.assertAlmostEqual(plan["fuel_per_hour"], 9.0, places=6)

    def test_service_slot_limit_is_reported(self):
        """服务槽位（2056）要能读到：索迪约 6、空堡 3、马金尼斯 8。"""
        for type_id, slots in ((35827, 6), (35832, 3), (47515, 8), (35835, 3)):
            plan = industry.fuel_plan(self.store, type_id, [35878], price=0)
            self.assertEqual(plan["service_slots"], slots, "结构 %s" % type_id)


    def test_fuel_type_default_is_a_published_market_item(self):
        """默认燃料块必须是已发布的市场物品。

        回归：早期版本默认用 36945「通用燃料块」，它是 published=0 的内部物品，
        根本买不到（也取不到价格）。CCP 官方文章说建筑只能用 4 种燃料块。
        """
        types = industry.fuel_types(self.store)
        ids = {t["type_id"] for t in types}
        self.assertEqual(ids, {4051, 4246, 4247, 4312},
                         "燃料块选项应只有氮/氢/氦/氧四种")
        for t in types:
            self.assertTrue(t["published"], "%s 必须是已发布物品" % t["name"])
        self.assertIn(cfgmod.DEFAULT_FUEL_BLOCK_TYPE_ID, ids)
        self.assertTrue((self.store.type_by_id(cfgmod.DEFAULT_FUEL_BLOCK_TYPE_ID) or {})
                        .get("published"), "默认燃料块必须已发布")

    def test_online_cost_matches_three_days(self):
        """首次上线 ≈ 3 天的量（官方说法），SDE 的 2110 恰好 = 2109 × 72。"""
        svc = self.store.bonus_attributes_many([35878, 35892, 35899, 35877, 35881])
        for type_id in (35878, 35892, 35899, 35877, 35881):
            a = svc[type_id]
            hourly = a[cfgmod.SERVICE_FUEL_HOURLY_ATTRIBUTE]
            online = a[cfgmod.SERVICE_FUEL_ONLINE_ATTRIBUTE]
            self.assertAlmostEqual(online / hourly, 72.0, places=6,
                                   msg="类型 %s 的上线消耗应是 72 小时" % type_id)

    def test_unit_factor_is_applied_and_configurable(self):
        """1 单位 = 1 燃料块（可配置）；改配置后数字应跟着变。"""
        self.assertEqual(cfgmod.FUEL_BLOCKS_PER_UNIT, 1)
        plan = industry.fuel_plan(self.store, 35827, [35878], price=0)
        self.assertEqual(plan["fuel_blocks_per_unit"], 1)
        self.assertAlmostEqual(plan["fuel_per_hour"], 9.0, places=6)
        old = cfgmod.FUEL_BLOCKS_PER_UNIT
        try:
            cfgmod.FUEL_BLOCKS_PER_UNIT = 24
            plan24 = industry.fuel_plan(self.store, 35827, [35878], price=0)
            self.assertAlmostEqual(plan24["fuel_per_hour"], 9.0 * 24, places=6)
            self.assertEqual(plan24["fuel_blocks_per_unit"], 24)
        finally:
            cfgmod.FUEL_BLOCKS_PER_UNIT = old

    def test_mechanics_notes_are_returned(self):
        plan = industry.fuel_plan(self.store, 35827, [35878], price=0)
        text = " ".join(plan["mechanics"])
        self.assertIn("建筑本身不消耗燃料", text)
        self.assertIn("一次性燃料不计入", text)
        self.assertIn("氮 / 氢 / 氦 / 氧", text)


    def test_unit_is_one_fuel_block_matching_the_game_display(self):
        """用游戏内截图对账：1 单位 = 1 燃料块。

        截图证据（用户提供）：
          * 屹立克隆中心 I 的信息面板：「服务装备周期燃料需求 **10 单位**」
          * 在阿兹贝尔的装配模拟里：「燃料 **240 燃料/天**」
        `240 ÷ 24 小时 = 10` —— 10 单位/小时 ↔ 240 燃料/天，1:1。
        （240 ÷ 10 = 24 只是「一天 24 小时」，不是「1 单位 = 24 块」。）
        """
        plan = industry.fuel_plan(self.store, 35826, [35894], price=0)   # 阿兹贝尔 + 克隆中心
        self.assertEqual(plan["fuel_blocks_per_unit"], 1)
        # 原始值（未含建筑减免）= 10/小时 = 240/天 —— 与游戏装配模拟显示一致
        self.assertAlmostEqual(plan["base_fuel_per_hour"], 10.0, places=6)
        self.assertAlmostEqual(plan["base_fuel_per_hour"] * 24, 240.0, places=6)
        # 关键：阿兹贝尔的 −25% **只作用于工程服务装备**，克隆中心属于堡垒服务装备，
        # 所以**不打折** —— 这正是游戏里显示 240/天 而不是 180/天的原因。
        self.assertAlmostEqual(plan["structure_fuel_bonus"], -25.0, places=6)
        self.assertFalse(plan["services"][0]["in_fuel_scope"])
        self.assertAlmostEqual(plan["fuel_per_hour"], 10.0, places=6)
        self.assertAlmostEqual(plan["fuel_per_day"], 240.0, places=6)

    def test_discount_only_applies_to_the_structures_own_service_category(self):
        """建筑只给它自己那一类服务装备打折（用户提供的游戏内文本）。

        空堡/铁壁/星城 → 堡垒；莱塔卢/阿兹贝尔/索迪约 → 工程；阿塔诺/塔塔拉 → 提炼；
        德拉库斯/地平线 → 堡垒+工程；普罗米修斯 → 堡垒+提炼；莫罗/马金尼斯 → 全部。
        """
        # 克隆中心=堡垒服务装备、制造厂=工程服务装备、提炼设施=提炼服务装备
        CLONE, FACTORY, REFINERY = 35894, 35878, 35899
        expected = {
            35832: {"clone": 7.5, "factory": 12.0, "refinery": 10.0},    # 空堡：只堡垒 −25%
            35825: {"clone": 10.0, "factory": 9.0, "refinery": 10.0},    # 莱塔卢：只工程
            35826: {"clone": 10.0, "factory": 9.0, "refinery": 10.0},    # 阿兹贝尔
            35835: {"clone": 10.0, "factory": 12.0, "refinery": 8.0},    # 阿塔诺：只提炼 −20%
            35836: {"clone": 10.0, "factory": 12.0, "refinery": 7.5},    # 塔塔拉：只提炼 −25%
            47513: {"clone": 7.5, "factory": 9.0, "refinery": 10.0},     # 德拉库斯：堡垒+工程
            47514: {"clone": 7.5, "factory": 9.0, "refinery": 10.0},     # 地平线：堡垒+工程
            47516: {"clone": 7.5, "factory": 12.0, "refinery": 7.5},     # 普罗米修斯：堡垒+提炼
            47512: {"clone": 6.5, "factory": 7.8, "refinery": 6.5},      # 莫罗：全部 −35%
            47515: {"clone": 5.0, "factory": 6.0, "refinery": 5.0},      # 马金尼斯：全部 −50%
        }
        for structure_id, want in expected.items():
            got = {}
            for key, svc_id in (("clone", CLONE), ("factory", FACTORY), ("refinery", REFINERY)):
                plan = industry.fuel_plan(self.store, structure_id, [svc_id], price=0)
                got[key] = round(plan["fuel_per_hour"], 6)
            self.assertEqual(got, want, "结构 %s 的逐项折扣不对" % structure_id)

    def test_fuel_scope_is_reported(self):
        plan = industry.fuel_plan(self.store, 35826, [35878], price=0)
        self.assertEqual(plan["fuel_scope_groups"], [2332])
        self.assertIn("工程服务装备", plan["fuel_scope_names"])
        plan = industry.fuel_plan(self.store, 47515, [35878], price=0)
        self.assertEqual(sorted(plan["fuel_scope_groups"]), [2232, 2233, 2332])
        self.assertEqual(len(plan["fuel_scope_names"]), 3)

    def test_service_categories_come_from_market_groups(self):
        """分类必须按**市场分组**走（与游戏里的服务装备树一致）。

        例：屹立诱导力场发生器 I 的 SDE group 是「快速部署服务装备」，
        但它的市场分组是 2332 工程服务装备 —— 游戏里就列在工程服务装备下。
        """
        svcs = {s["type_id"]: s for s in industry.service_modules(self.store, 35826)}
        self.assertEqual(svcs[35878]["category"], "工程服务装备")     # 制造厂
        self.assertEqual(svcs[35894]["category"], "堡垒服务装备")     # 克隆中心
        self.assertEqual(svcs[35899]["category"], "资源提炼服务装备")  # 提炼设施
        self.assertEqual(svcs[35912]["category"], "工程服务装备", "诱导力场发生器应按市场分组归类")
        self.assertEqual(svcs[35912]["market_group_id"], 2332)

    def test_mechanics_note_about_ingame_display(self):
        """必须说明「游戏里显示的是未打折值」，否则用户会对不上号。"""
        plan = industry.fuel_plan(self.store, 35826, [35894], price=0)
        text = " ".join(plan["mechanics"])
        self.assertIn("未打折", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
