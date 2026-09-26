"""结算引擎规则验证：零自付、范围外、封顶、起付线、确定性顺序。"""

import unittest

from src.benefits.engine import BillLineInput, compute_allowance, compute_settlement
from src.benefits.policy import CatalogEntry

from tests.helpers import standard_rules


def lookup_factory(mapping: dict):
    return lambda code: mapping.get(code)


FULL_CATALOG = {
    "D": CatalogEntry("basic_delivery", True),
    "A": CatalogEntry("analgesia", True),
    "C": CatalogEntry("complication", True),
    "P": CatalogEntry("prenatal_exam", True),
    "X": CatalogEntry("out_of_scope", False),
}


def line(code, amount, no):
    return BillLineInput(
        bill_line_id=f"L{no}", item_code=code, item_name=code,
        amount=amount, service_date="2026-09-20", line_no=no,
    )


class EngineRuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = standard_rules()

    def result_for(self, lines):
        return compute_settlement(
            lines, self.rules, lookup_factory(FULL_CATALOG), package_id="PKG1"
        )

    def by_code(self, result):
        return {l.item_code: l for l in result.lines}

    def test_zero_copay_basic_delivery(self):
        r = self.result_for([line("D", 5000_00, 1)])
        row = self.by_code(r)["D"]
        self.assertEqual(row.payable, 5000_00)
        self.assertEqual(row.personal, 0)

    def test_out_of_scope_not_masked_by_zero_copay(self):
        r = self.result_for([line("D", 5000_00, 1), line("X", 3000_00, 2)])
        rows = self.by_code(r)
        self.assertEqual(rows["D"].payable, 5000_00)
        self.assertEqual(rows["X"].payable, 0)
        self.assertEqual(rows["X"].personal, 3000_00)
        self.assertFalse(rows["X"].in_scope)
        self.assertIn("out_of_scope", rows["X"].rule_ref)
        # 个人负担总额必须暴露范围外项目
        self.assertEqual(r.personal_total, 3000_00)

    def test_unknown_item_treated_as_personal(self):
        r = self.result_for([line("MYSTERY", 800_00, 1)])
        row = r.lines[0]
        self.assertEqual((row.payable, row.personal), (0, 800_00))

    def test_analgesia_rate_and_cap(self):
        # 3000 * 70% = 2100，封顶 2000
        r = self.result_for([line("A", 3000_00, 1)])
        self.assertEqual((r.lines[0].payable, r.lines[0].personal), (2000_00, 1000_00))

    def test_analgesia_cap_accumulates_across_lines(self):
        r = self.result_for([line("A", 1500_00, 1), line("A", 1500_00, 2)])
        self.assertEqual(r.payable_total, 2000_00)

    def test_complication_deductible_then_rate(self):
        # (5000 - 800 起付) * 80% = 3360
        r = self.result_for([line("C", 5000_00, 1)])
        self.assertEqual(r.lines[0].payable, 3360_00)
        self.assertEqual(r.lines[0].personal, 1640_00)
        self.assertIn("deductible=80000", r.lines[0].rule_ref)

    def test_complication_deductible_only_once(self):
        # 两行各 500：第一行吃掉 500 起付，应付 0；第二行扣剩余 300 后按 80%
        r = self.result_for([line("C", 500_00, 1), line("C", 500_00, 2)])
        self.assertEqual([l.payable for l in r.lines], [0, 160_00])

    def test_prenatal_cap(self):
        r = self.result_for([line("P", 1500_00, 1)])
        self.assertEqual((r.lines[0].payable, r.lines[0].personal), (1200_00, 300_00))

    def test_every_total_traces_to_lines(self):
        r = self.result_for(
            [line("D", 5000_00, 1), line("X", 3000_00, 2),
             line("A", 3000_00, 3), line("C", 5000_00, 4), line("P", 1500_00, 5)]
        )
        self.assertEqual(sum(l.amount for l in r.lines),
                         r.payable_total + r.personal_total)
        self.assertEqual(r.payable_total, 5000_00 + 2000_00 + 3360_00 + 1200_00)

    def test_deterministic_regardless_of_input_order(self):
        lines = [line("D", 5000_00, 1), line("A", 1500_00, 2),
                 line("A", 1500_00, 3), line("C", 5000_00, 4)]
        r1 = self.result_for(lines)
        r2 = self.result_for(list(reversed(lines)))
        key = lambda r: sorted((l.bill_line_id, l.payable) for l in r.lines)
        self.assertEqual(key(r1), key(r2))
        self.assertEqual(r1.payable_total, r2.payable_total)

    def test_allowance_formula(self):
        # 月基数 15000 元 / 30 × 158 天
        self.assertEqual(compute_allowance(1_500_000, 158, 30), 7_900_000)


if __name__ == "__main__":
    unittest.main()
