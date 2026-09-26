"""案件自然键与事件流折叠：重试、补正、乱序/重复回执归并。"""

import unittest

from src.benefits.events import Event, EventType, fold
from src.benefits.identity import case_natural_key


def opened(seq=1, delivery="2026-08-01"):
    return Event(seq, EventType.CASE_OPENED, {
        "case_id": "C", "person_id": "P1", "enrollment_id": "E1",
        "home_region": "A市", "care_region": "B市",
        "delivery_date": delivery}, "sys", "t")


class IdentityTests(unittest.TestCase):
    def test_natural_key_deterministic(self):
        a = case_natural_key("P1", "E1", "B市", "2026-08-01")
        b = case_natural_key(" p1", "e1 ", " b市", "2026-08-01")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("CASE-"))

    def test_natural_key_distinguishes_business_facts(self):
        base = ("P1", "E1", "B市", "2026-08-01")
        for i in range(4):
            parts = list(base)
            parts[i] = "OTHER" if i < 3 else "2026-08-02"
            self.assertNotEqual(case_natural_key(*base),
                                case_natural_key(*parts))

    def test_empty_field_rejected(self):
        with self.assertRaises(ValueError):
            case_natural_key("P1", "", "B市", "2026-08-01")


class FoldTests(unittest.TestCase):
    def _fee_event(self, seq, claim, item, amount, kind=EventType.FEES_REPORTED):
        return Event(seq, kind, {"claim_id": claim, "lines": [{
            "item_code": item, "category": "complication",
            "amount": amount, "service_date": "2026-08-01"}]},
            "hosp", f"t{seq}")

    def test_hospital_retry_collapses_to_one_fact(self):
        evs = [opened(), self._fee_event(2, "CL1", "C001", 100000),
               self._fee_event(3, "CL1", "C001", 100000),
               self._fee_event(4, "CL1", "C001", 100000)]
        st = fold(evs)
        self.assertEqual(len(st.all_fee_lines()), 1)

    def test_correction_overwrites_old_line(self):
        evs = [opened(), self._fee_event(2, "CL1", "C001", 100000),
               self._fee_event(3, "CL1", "C001", 90000,
                               EventType.FEE_CORRECTED)]
        st = fold(evs)
        self.assertEqual(st.all_fee_lines()[0].amount, 90000)

    def test_receipt_before_upload_merges_on_arrival(self):
        receipt = Event(2, EventType.REMOTE_RECEIPT, {
            "claim_id": "CL1", "receipts": [{
                "item_code": "C001", "accepted_amount": 80000,
                "batch_seq": 1}]}, "remote", "t0")
        st = fold([opened(), receipt,
                   self._fee_event(3, "CL1", "C001", 100000)])
        ln = st.all_fee_lines()[0]
        self.assertTrue(ln.remote_confirmed)
        self.assertEqual(ln.effective_amount(), 80000)

    def test_out_of_order_duplicate_receipts_latest_batch_wins(self):
        r1 = Event(2, EventType.REMOTE_RECEIPT, {
            "claim_id": "CL1", "receipts": [{
                "item_code": "C001", "accepted_amount": 80000,
                "batch_seq": 2}]}, "remote", "t1")
        r2 = Event(3, EventType.REMOTE_RECEIPT, {
            "claim_id": "CL1", "receipts": [{
                "item_code": "C001", "accepted_amount": 90000,
                "batch_seq": 1}]}, "remote", "t2")  # 乱序旧批次
        st = fold([opened(), r1, r2,
                   self._fee_event(4, "CL1", "C001", 100000)])
        self.assertEqual(st.all_fee_lines()[0].effective_amount(), 80000)

    def test_receipt_after_upload_also_merges(self):
        st = fold([opened(), self._fee_event(2, "CL1", "C001", 100000),
                   Event(3, EventType.REMOTE_RECEIPT, {
                       "claim_id": "CL1", "receipts": [{
                           "item_code": "C001", "accepted_amount": 70000,
                           "batch_seq": 1}]}, "remote", "t3")])
        self.assertEqual(st.all_fee_lines()[0].effective_amount(), 70000)

    def test_withdrawal_removes_fact_and_receipt(self):
        st = fold([
            opened(),
            self._fee_event(2, "CL1", "C001", 100000),
            Event(3, EventType.REMOTE_RECEIPT, {
                "claim_id": "CL1", "receipts": [{
                    "item_code": "C001", "accepted_amount": 1,
                    "batch_seq": 1}]}, "remote", "t"),
            Event(4, EventType.FEE_WITHDRAWN, {
                "claim_id": "CL1", "item_codes": ["C001"]}, "agent", "t"),
        ])
        self.assertEqual(st.all_fee_lines(), [])

    def test_fold_is_order_independent_of_receipt_arrival(self):
        fees = self._fee_event(9, "CL1", "C001", 100000)
        receipt = Event(8, EventType.REMOTE_RECEIPT, {
            "claim_id": "CL1", "receipts": [{
                "item_code": "C001", "accepted_amount": 80000,
                "batch_seq": 1}]}, "remote", "t")
        # 无论事件如何分配 seq，最终只看 seq 顺序折叠
        st1 = fold([opened(1), receipt, fees])
        st2 = fold([opened(1), Event(
            2, EventType.REMOTE_RECEIPT, receipt.payload, "remote", "t"),
            self._fee_event(3, "CL1", "C001", 100000)])
        self.assertEqual(st1.all_fee_lines()[0].effective_amount(),
                         st2.all_fee_lines()[0].effective_amount())


if __name__ == "__main__":
    unittest.main()
