"""领域规则引擎：结算分类、零自付边界、目录外项目、津贴资格。"""

import unittest

from src.benefits import (
    CostCategory,
    CategoryRule,
    ItemCatalog,
    PolicyPackage,
    PolicyRegistry,
    FeeLine,
    allowance_amount,
    allowance_eligibility,
    insured_months,
    settle_lines,
)
from src.benefits.errors import RuleNotInEffect
from src.benefits.models import Enrollment


PKG = PolicyPackage(
    code="PKG", version=1, region="A市", effective_from="2026-01-01",
    effective_to=None, include_flexible=True, min_insured_months=6,
    allowance_days={"normal": 98, "dystocia": 113},
    category_rules={
        CostCategory.BASIC_DELIVERY: CategoryRule(
            CostCategory.BASIC_DELIVERY, "zero_copay", cap=400000),
        CostCategory.COMPLICATION: CategoryRule(
            CostCategory.COMPLICATION, "ratio", ratio_num=8, ratio_denom=10),
        CostCategory.ANALGESIA: CategoryRule(CostCategory.ANALGESIA, "none"),
    },
    published_at="2025-12-01T00:00:00Z")

CAT = ItemCatalog(
    code="CAT", version=1, region="B市", effective_from="2026-01-01",
    effective_to=None,
    items={"D001": CostCategory.BASIC_DELIVERY,
           "C001": CostCategory.COMPLICATION},
    published_at="2025-12-01T00:00:00Z")


def line(code, amount, claimed=CostCategory.BASIC_DELIVERY,
         accepted=None, confirmed=False):
    return FeeLine("CL1", code, claimed, amount, "2026-08-01",
                   remote_confirmed=confirmed, remote_accepted=accepted)


class SettlementRuleTests(unittest.TestCase):
    def test_zero_copay_within_cap_is_fully_funded(self):
        r = settle_lines("X", [line("D001", 300000)], PKG, CAT)
        self.assertEqual(r.fund_total, 300000)
        self.assertEqual(r.personal_total, 0)

    def test_zero_copay_does_not_cover_above_cap(self):
        r = settle_lines("X", [line("D001", 450000)], PKG, CAT)
        self.assertEqual(r.fund_total, 400000)
        self.assertEqual(r.personal_total, 50000)

    def test_complication_uses_its_own_ratio_rule(self):
        r = settle_lines("X", [line("C001", 100000,
                                    CostCategory.COMPLICATION)], PKG, CAT)
        self.assertEqual(r.fund_total, 80000)
        self.assertEqual(r.personal_total, 20000)

    def test_out_of_catalog_item_is_fully_personal_even_if_claimed_basic(self):
        # 医院自报为基础分娩，但目录里没有该项目：零自付不得掩盖范围外项目
        r = settle_lines("X", [line("UNKNOWN", 9999,
                                    CostCategory.BASIC_DELIVERY)], PKG, CAT)
        self.assertEqual(r.fund_total, 0)
        self.assertEqual(r.personal_total, 9999)
        self.assertFalse(r.lines[0].in_scope)
        self.assertIn("不在就医地政策目录", r.lines[0].reason)

    def test_analgesia_not_in_package_is_personal(self):
        r = settle_lines(
            "X", [FeeLine("CL1", "A001", CostCategory.ANALGESIA, 2000,
                          "2026-08-01")],
            PolicyPackage(
                code="P2", version=1, region="A市",
                effective_from="2026-01-01", effective_to=None,
                include_flexible=True, min_insured_months=6,
                allowance_days={"normal": 98},
                category_rules={
                    CostCategory.BASIC_DELIVERY: CategoryRule(
                        CostCategory.BASIC_DELIVERY, "zero_copay", cap=1)},
                published_at="t"),
            ItemCatalog(code="C2", version=1, region="B市",
                        effective_from="2026-01-01", effective_to=None,
                        items={"A001": CostCategory.ANALGESIA},
                        published_at="t"))
        self.assertEqual(r.fund_total, 0)
        self.assertEqual(r.personal_total, 2000)

    def test_every_line_traces_to_rule(self):
        r = settle_lines("X", [line("D001", 100), line("X1", 10)], PKG, CAT)
        for ln in r.lines:
            self.assertTrue(ln.rule_id)
            self.assertIn("CAT@v1", ln.rule_id)

    def test_remote_receipt_caps_charge(self):
        r = settle_lines("X", [line("C001", 100000,
                                    CostCategory.COMPLICATION,
                                    accepted=50000, confirmed=True)],
                         PKG, CAT)
        self.assertEqual(r.charge_total, 50000)
        self.assertEqual(r.fund_total, 40000)

    def test_remote_accepted_above_claim_does_not_inflate(self):
        r = settle_lines("X", [line("C001", 100000,
                                    CostCategory.COMPLICATION,
                                    accepted=120000, confirmed=True)],
                         PKG, CAT)
        self.assertEqual(r.charge_total, 100000)


class VersionSelectionTests(unittest.TestCase):
    def _registry(self):
        v1 = PolicyPackage(
            code="PKG", version=1, region="A市",
            effective_from="2025-01-01", effective_to="2026-01-01",
            include_flexible=True, min_insured_months=6,
            allowance_days={"normal": 90}, category_rules={},
            published_at="2024-12-01T00:00:00Z")
        v2 = PolicyPackage(
            code="PKG", version=2, region="A市",
            effective_from="2026-01-01", effective_to=None,
            include_flexible=True, min_insured_months=6,
            allowance_days={"normal": 98}, category_rules={},
            published_at="2025-12-01T00:00:00Z")
        return PolicyRegistry([v1, v2], [])

    def test_effective_to_is_exclusive_boundary(self):
        reg = self._registry()
        self.assertEqual(reg.package_for("A市", "2025-12-31").version, 1)
        self.assertEqual(reg.package_for("A市", "2026-01-01").version, 2)

    def test_no_version_in_effect(self):
        reg = self._registry()
        with self.assertRaises(RuleNotInEffect):
            reg.package_for("A市", "2024-12-31")

    def test_region_isolation(self):
        reg = self._registry()
        with self.assertRaises(RuleNotInEffect):
            reg.package_for("C市", "2026-06-01")


class AllowanceRuleTests(unittest.TestCase):
    ENR = Enrollment("E1", "P1", "A市", "2026-01-10")

    def test_insured_months_inclusive(self):
        self.assertEqual(insured_months(self.ENR, "2026-06-30"), 6)
        self.assertEqual(insured_months(self.ENR, "2026-05-31"), 5)

    def test_short_insurance_rejected(self):
        ok, _ = allowance_eligibility(self.ENR, PKG, "2026-05-31", "normal")
        self.assertFalse(ok)

    def test_flexible_excluded_version(self):
        old = PolicyPackage(
            code="PKG", version=0, region="A市",
            effective_from="2020-01-01", effective_to=None,
            include_flexible=False, min_insured_months=0,
            allowance_days={"normal": 98}, category_rules={},
            published_at="t")
        ok, msg = allowance_eligibility(self.ENR, old, "2026-08-01", "normal")
        self.assertFalse(ok)
        self.assertIn("灵活就业", msg)

    def test_amount_integer_floor(self):
        self.assertEqual(allowance_amount(1200000, 98), 1200000 * 98 // 30)


if __name__ == "__main__":
    unittest.main()
