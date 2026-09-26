"""真实并发：多线程下的终局操作与资金指令互斥。"""

import threading
import unittest

from src.benefits.errors import Conflict, DomainError, PaymentError
from tests._setup import ACCOUNT, make_service, seed_basic


def fee(claim, item, amount, cat="basic_delivery"):
    return {"item_code": item, "category": cat, "amount": amount,
            "service_date": "2026-08-01"}


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        seed_basic(self.svc.store)
        self.cid = self.svc.open_case({
            "person_id": "P1", "enrollment_id": "E1", "care_region": "B市",
            "delivery_date": "2026-08-01", "account": ACCOUNT})["case_id"]
        self.svc.report_fees(self.cid, "CL1", [
            fee("CL1", "D001", 450000), fee("CL1", "C001", 100000,
                                            "complication")])
        self.svc.settle_medical(self.cid)

    def _run_concurrent(self, target, n=8):
        barrier = threading.Barrier(n)
        results, errors = [], []

        def worker(i):
            barrier.wait()
            try:
                results.append((i, target(i)))
            except DomainError as exc:
                errors.append((i, type(exc).__name__))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results, errors

    def test_concurrent_allowance_approval_exactly_one_wins(self):
        def attempt(i):
            return self.svc.decide_allowance(
                self.cid, "normal", 1200000,
                actor=f"agent{i}", idem_key=None)
        ok, err = self._run_concurrent(attempt)
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(err), 7)
        self.assertTrue(all(e[1] == Conflict.__name__ for e in err))
        # 只产生一笔津贴应付
        expl = self.svc.explain_case(self.cid)
        self.assertEqual(expl["balances"]["allowance"],
                         1200000 * 98 // 30)

    def test_concurrent_payment_exactly_one_disbursement(self):
        def attempt(i):
            return self.svc.issue_payment(self.cid, "medical_fund",
                                          actor="treasury")
        ok, err = self._run_concurrent(attempt)
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(err), 7)
        self.assertTrue(all(e[1] == PaymentError.__name__ for e in err))
        expl = self.svc.explain_case(self.cid)
        self.assertEqual(expl["balances"]["medical_fund"], 0)
        self.assertEqual(len([p for p in expl["payments"]
                              if p["kind"] == "medical_fund"
                              and p["status"] == "issued"]), 1)

    def test_concurrent_settlement_exactly_one(self):
        # 新案件，无任何结算，8 个线程同时结算
        cid2 = self.svc.open_case({
            "person_id": "P1", "enrollment_id": "E1", "care_region": "B市",
            "delivery_date": "2026-08-02", "account": ACCOUNT})["case_id"]
        self.svc.report_fees(cid2, "CL9", [fee("CL9", "D001", 100)])

        def attempt(i):
            return self.svc.settle_medical(cid2, actor=f"agent{i}")
        ok, err = self._run_concurrent(attempt)
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(err), 7)

    def test_concurrent_optimistic_version_conflict(self):
        cid3 = self.svc.open_case({
            "person_id": "P1", "enrollment_id": "E1", "care_region": "B市",
            "delivery_date": "2026-08-03", "account": ACCOUNT})["case_id"]

        def attempt(i):
            return self.svc.report_fees(
                cid3, f"CL{i}", [fee(f"CL{i}", "D001", 100 + i)],
                expected_version=1)  # 所有人都拿版本 1
        ok, err = self._run_concurrent(attempt)
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(err), 7)


if __name__ == "__main__":
    unittest.main()
