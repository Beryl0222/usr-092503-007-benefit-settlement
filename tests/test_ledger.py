"""账本原语：余额、冲正链与不可变追加。"""

import unittest

from src.benefits import CaseLedger, LedgerAction
from src.benefits.errors import PaymentError


class LedgerPrimitiveTests(unittest.TestCase):
    def test_balance_nets_payable_disbursement_and_reversal(self):
        lg = CaseLedger("C")
        e1 = lg.append(LedgerAction.PAYABLE, "medical_fund", 10000,
                       origin="rule", note="应付")
        self.assertEqual(lg.balance(), 10000)
        e2 = lg.append(LedgerAction.DISBURSEMENT, "medical_fund", -10000,
                       payment_id="P1", origin="disbursement", note="拨付")
        self.assertEqual(lg.balance(), 0)
        e3 = lg.append(LedgerAction.REVERSAL, "medical_fund", 10000,
                       payment_id="R1", reverses_entry_id=e2.entry_id,
                       origin="reversal", note="冲正")
        self.assertEqual(lg.balance(), 10000)
        # 冲正链可追踪
        self.assertEqual(lg.reversal_chain(e2.entry_id),
                         [e2.entry_id, e3.entry_id])

    def test_only_disbursement_side_can_be_reversed(self):
        lg = CaseLedger("C")
        payable = lg.append(LedgerAction.PAYABLE, "medical_fund", 10000,
                            origin="rule", note="应付")
        with self.assertRaises(PaymentError):
            lg.validate_reversal(payable)

    def test_entry_ids_are_sequential_and_stable(self):
        lg = CaseLedger("C")
        e1 = lg.append(LedgerAction.PAYABLE, "medical_fund", 1,
                       origin="rule", note="")
        e2 = lg.append(LedgerAction.PAYABLE, "medical_fund", 2,
                       origin="rule", note="")
        self.assertEqual(e1.entry_id, "C:L000001")
        self.assertEqual(e2.entry_id, "C:L000002")
        # 已被冲正的分录不能再次冲正
        disb = lg.append(LedgerAction.DISBURSEMENT, "medical_fund", -3,
                         payment_id="P", origin="disbursement", note="")
        lg.validate_reversal(disb)
        lg.append(LedgerAction.REVERSAL, "medical_fund", 3,
                  payment_id="R", reverses_entry_id=disb.entry_id,
                  origin="reversal", note="")
        with self.assertRaises(PaymentError):
            lg.validate_reversal(disb)

    def test_explain_carries_refs(self):
        lg = CaseLedger("C")
        e = lg.append(LedgerAction.PAYABLE, "medical_fund", 500,
                      origin="rule", refs=(("item_code", "D001"),
                                           ("rule_id", "R@v1")),
                      note="")
        refs = {r["name"]: r["value"] for r in e.explain()["refs"]}
        self.assertEqual(refs["item_code"], "D001")
        self.assertEqual(refs["rule_id"], "R@v1")


if __name__ == "__main__":
    unittest.main()
