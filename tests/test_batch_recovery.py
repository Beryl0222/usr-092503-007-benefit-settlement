"""确定性批量日结：幂等、崩溃恢复续跑、追溯差额次日拨付、进程级互斥。"""

import threading
import unittest
from unittest import mock

from src.benefits.errors import Conflict
from src.benefits.service import BenefitService
from tests._setup import (
    ACCOUNT,
    make_service,
    package,
    package_rules,
    seed_basic,
)


def fee(claim, item, amount, cat="basic_delivery"):
    return {"item_code": item, "category": cat, "amount": amount,
            "service_date": "2026-08-01"}


class DailyCloseTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        seed_basic(self.svc.store)

    def _seed_distinct_cases(self, n=3):
        ids = []
        for i in range(1, n + 1):
            pid, eid = f"P{i}", f"E{i}"
            self.svc.store.upsert_enrollment({
                "enrollment_id": eid, "person_id": pid, "home_region": "A市",
                "insured_from": "2025-01-15", "job_kind": "flexible"})
            cid = self.svc.open_case({
                "person_id": pid, "enrollment_id": eid, "care_region": "B市",
                "delivery_date": "2026-08-01", "account": ACCOUNT})["case_id"]
            self.svc.report_fees(cid, f"CL{i}", [
                fee(f"CL{i}", "D001", 450000),
                fee(f"CL{i}", "C001", 100000, "complication"),
                fee(f"CL{i}", "X999", 3000),
            ])
            ids.append(cid)
        return sorted(ids)

    def test_daily_close_full_run_and_idempotent_rerun(self):
        ids = self._seed_distinct_cases(3)
        summary = self.svc.daily_close("2026-08-31")
        self.assertEqual(summary["counts"]["settled"], 3)
        self.assertEqual(summary["counts"]["failed"], 0)
        for cid in ids:
            expl = self.svc.explain_case(cid)
            self.assertEqual(expl["balances"]["medical_fund"], 0)
            issued = [p for p in expl["payments"] if p["status"] == "issued"]
            self.assertEqual(len(issued), 1)
        # 同日重跑：确定性空操作，不产生新拨付
        again = self.svc.daily_close("2026-08-31")
        self.assertEqual(again["counts"]["settled"], 0)
        self.assertEqual(again["counts"]["paid"], 0)
        for cid in ids:
            expl = self.svc.explain_case(cid)
            self.assertEqual(len([p for p in expl["payments"]
                                  if p["status"] == "issued"]), 1)
        self.assertEqual(self.svc.store.batch_status("2026-08-31")
                         ["counts"]["done"], 3)

    def test_crash_resume_finishes_only_claimed_case(self):
        ids = self._seed_distinct_cases(3)
        crash_case = ids[1]
        original = BenefitService._daily_process_case

        def flaky(self_, run_date, case_id, actor, detail):
            original(self_, run_date, case_id, actor, detail)
            if case_id == crash_case:
                raise RuntimeError("模拟进程在 finish 前崩溃")

        with mock.patch.object(BenefitService, "_daily_process_case", flaky):
            with self.assertRaises(RuntimeError):
                self.svc.daily_close("2026-08-31")

        # 崩溃现场：崩溃案停在 claimed
        pending = self.svc.store.pending_batch_cases("2026-08-31")
        self.assertEqual(pending, [crash_case])

        # 恢复续跑（含尚未结算的第三案）
        summary = self.svc.daily_close("2026-08-31")
        done = self.svc.store.batch_status("2026-08-31")["counts"]
        self.assertEqual(done.get("done", 0), 3)
        self.assertEqual(done.get("failed", 0), 0)
        # 崩溃案的结算/拨付均只发生一次（幂等键重放）
        expl = self.svc.explain_case(crash_case)
        self.assertEqual(len(expl["settlements"]), 1)
        self.assertEqual(expl["balances"]["medical_fund"], 0)
        self.assertEqual(summary["counts"]["failed"], 0)

    def test_failed_case_does_not_block_batch_and_is_reported(self):
        ids = self._seed_distinct_cases(2)
        original = BenefitService._daily_process_case

        def failing(self_, run_date, case_id, actor, detail):
            if case_id == ids[0]:
                from src.benefits.errors import SettlementError
                raise SettlementError("模拟该案结算失败")
            original(self_, run_date, case_id, actor, detail)

        with mock.patch.object(BenefitService, "_daily_process_case", failing):
            summary = self.svc.daily_close("2026-08-31")
        self.assertEqual(summary["counts"]["settled"], 1)
        self.assertEqual(len(summary["failed"]), 1)
        self.assertEqual(summary["failed"][0]["case_id"], ids[0])
        # 失败案未拨付，成功案已结清
        self.assertEqual(self.svc.explain_case(ids[1])
                         ["balances"]["medical_fund"], 0)

        # 同日重跑：失败案被重新认领并成功；成功案已结算不在候选中
        again = self.svc.daily_close("2026-08-31")
        self.assertEqual(again["counts"]["settled"], 1)
        self.assertEqual(again["counts"]["failed"], 0)
        self.assertEqual(again["counts"]["skipped"], 0)
        self.assertEqual(self.svc.explain_case(ids[0])
                         ["balances"]["medical_fund"], 0)
        self.assertEqual(self.svc.store.batch_status("2026-08-31")
                         ["counts"].get("done", 0), 2)

    def test_retro_delta_is_disbursed_on_next_day_batch(self):
        ids = self._seed_distinct_cases(1)
        cid = ids[0]
        self.svc.daily_close("2026-08-31")
        self.assertEqual(self.svc.explain_case(cid)["balances"]
                         ["medical_fund"], 0)
        # 9 月政策调标：定额 450000、并发症 90%
        self.svc.store.put_package(
            package("A市", version=2, effective_from="2026-06-01",
                    published_at="2026-05-01T00:00:00Z"),
            package_rules(cap=450000, comp_num=9))
        retro = self.svc.retro_recompute(cid)
        self.assertGreater(retro["delta_fund"], 0)
        # 旧账不动，出现正余额等待次日拨付
        self.assertEqual(self.svc.explain_case(cid)["balances"]
                         ["medical_fund"], retro["delta_fund"])
        summary = self.svc.daily_close("2026-09-01")
        self.assertGreaterEqual(summary["counts"]["paid"], 1)
        self.assertEqual(self.svc.explain_case(cid)["balances"]
                         ["medical_fund"], 0)
        # 当日批次里产生且只产生一笔差额拨付
        expl = self.svc.explain_case(cid)
        sept_pays = [p for p in expl["payments"]
                     if p["run_date"] == "2026-09-01" and p["status"] == "issued"]
        self.assertEqual(len(sept_pays), 1)
        self.assertEqual(sept_pays[0]["amount"], retro["delta_fund"])

    def test_concurrent_batch_invocation_rejected(self):
        self._seed_distinct_cases(1)
        errors = []

        def run():
            try:
                self.svc.daily_close("2026-08-31")
            except Conflict as exc:
                errors.append(exc)

        gate = threading.Event()

        def slow(self_, run_date, case_id, actor, detail):
            gate.set()
            import time
            time.sleep(0.3)

        with mock.patch.object(BenefitService, "_daily_process_case", slow):
            t1 = threading.Thread(target=run)
            t1.start()
            gate.wait()
            t2 = threading.Thread(target=run)
            t2.start()
            t2.join(timeout=5)
            t1.join(timeout=5)
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
