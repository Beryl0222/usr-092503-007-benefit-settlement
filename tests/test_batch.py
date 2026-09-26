"""确定性批量日结与进程恢复验证。"""

import json

from src.benefits.batch import DailySettlement
from src.benefits.contracts import PaymentOrderStatus, ReceiptStage
from src.benefits.ledger import case_balance

from tests.helpers import AppCase, REGION_A, REGION_B, bill_lines


def _stripped(report):
    """去掉易变标识，保留可复算部分用于确定性比对。"""

    return {
        "totals": report["totals"],
        "retro": sorted((d["case_id"],) for d in report["stages"]["retro"]),
        "finalize": sorted((d["case_id"],) for d in report["stages"]["finalize"]),
        "disburse": sorted(
            (d["case_id"], d["subject"], d["amount"])
            for d in report["stages"]["disburse"]
        ),
    }


class BatchTests(AppCase):
    def _two_cases(self):
        # 本地案件（A 市医院）与跨区案件（B 市医院）
        local = self.app.ingest_bill(
            hospital_id="H-A01", upload_id="U-L", person_id="P-1001",
            delivery_date="2026-09-20",
            lines=bill_lines()[:2], actor="hosp-a",
        )
        cross = self.app.ingest_bill(
            hospital_id="H-B01", upload_id="U-C", person_id="P-1002",
            delivery_date="2026-09-21",
            lines=bill_lines()[:2], actor="hosp-b",
        )
        for case_id in (local["case_id"], cross["case_id"]):
            self.app.register_account(
                case_id=case_id, account_no="62220001", account_name="本人",
                bank_code="ICBC", actor="clerk1",
            )
        return local["case_id"], cross["case_id"]

    def test_local_finalizes_without_receipt_cross_region_waits(self):
        self.configure_world()
        local_id, cross_id = self._two_cases()
        report = DailySettlement(self.app.db, self.app.clock).run("2026-09-26")
        # 仅本地案件进入终态并拨付；跨区案件缺 final 回执，医疗不拨付
        self.assertEqual(
            [d["case_id"] for d in report["stages"]["finalize"]], [local_id]
        )
        self.assertEqual(
            [(d["case_id"], d["subject"]) for d in report["stages"]["disburse"]],
            [(local_id, "medical")],
        )
        self.assertEqual(
            case_balance(self.app.db.connection(), cross_id, subject="medical"),
            self.app.explain(cross_id)["settlement_runs"][-1]["payable_total"],
        )

        # 次日：跨区 final 回执到达后，新一天的日结完成拨付
        self.app.ingest_receipt(
            case_id=cross_id, receipt_id="R1", stage=ReceiptStage.FINAL.value,
            confirmed_amount=8000_00, actor="platform",
        )
        report2 = DailySettlement(self.app.db, self.app.clock).run("2026-09-27")
        self.assertEqual(
            [(d["case_id"], d["subject"]) for d in report2["stages"]["disburse"]],
            [(cross_id, "medical")],
        )
        self.assertEqual(case_balance(self.app.db.connection(), cross_id, subject="medical"), 0)

    def test_rerun_is_idempotent_and_deterministic(self):
        self.configure_world()
        self._two_cases()
        batch = DailySettlement(self.app.db, self.app.clock)
        r1 = batch.run("2026-09-26")
        orders_after_first = len(
            self.app.db.connection().execute(
                "SELECT * FROM payment_orders"
            ).fetchall()
        )
        r2 = batch.run("2026-09-26")
        # 完成后的日结返回同一份持久化报告，不产生新指令
        self.assertEqual(json.dumps(r1, sort_keys=True), json.dumps(r2, sort_keys=True))
        self.assertEqual(
            len(self.app.db.connection().execute("SELECT * FROM payment_orders").fetchall()),
            orders_after_first,
        )

    def test_crash_resumes_from_progress(self):
        self.configure_world()
        local_id, cross_id = self._two_cases()
        # 跨区案件 final 回执先到，使两个案件都进入当日拨付阶段
        self.app.ingest_receipt(
            case_id=cross_id, receipt_id="R1", stage=ReceiptStage.FINAL.value,
            confirmed_amount=8000_00, actor="platform",
        )
        batch = DailySettlement(self.app.db, self.app.clock)

        # 让跨区案件在拨付阶段抛出，模拟进程崩溃
        original = batch._disburse_case

        def flaky(conn, bundle, batch_id, case_id, actor):
            if case_id == cross_id:
                raise RuntimeError("模拟进程中断")
            return original(conn, bundle, batch_id, case_id, actor)

        batch._disburse_case = flaky
        with self.assertRaises(RuntimeError):
            batch.run("2026-09-26")
        # 批次处于 running，本地案件已完成并发出指令
        row = self.app.db.connection().execute(
            "SELECT status FROM batch_runs WHERE batch_id = ?", ("BATCH-2026-09-26",)
        ).fetchone()
        self.assertEqual(row["status"], "running")

        # 进程恢复：重跑只处理未完成部分
        batch2 = DailySettlement(self.app.db, self.app.clock)
        batch2._disburse_case = original
        report = batch2.run("2026-09-26")
        self.assertEqual(report["totals"]["orders_sent"], 2)
        # 本地案件只拨付一次（恢复未重复）
        orders = self.app.db.connection().execute(
            "SELECT case_id, COUNT(*) AS c FROM payment_orders GROUP BY case_id"
        ).fetchall()
        self.assertEqual({(r["case_id"], r["c"]) for r in orders},
                         {(local_id, 1), (cross_id, 1)})
        for case_id in (local_id, cross_id):
            self.assertEqual(case_balance(self.app.db.connection(), case_id, subject="medical"), 0)

    def test_allowance_disbursed_in_batch(self):
        self.configure_world()
        local_id, _ = self._two_cases()
        allowance = self.app.submit_allowance(
            case_id=local_id, base_salary=1_500_000, leave_days=158, actor="clerk1",
        )
        self.app.approve_allowance(
            allowance_id=allowance["allowance_id"], actor="clerk2",
            expected_version=allowance["version"],
        )
        report = DailySettlement(self.app.db, self.app.clock).run("2026-09-26")
        subjects = {(d["case_id"], d["subject"]) for d in report["stages"]["disburse"]}
        self.assertIn((local_id, "medical"), subjects)
        self.assertIn((local_id, "allowance"), subjects)
        final = self.app.get_allowance_by_case(local_id)
        self.assertEqual(final["status"], "paid")
        # 指令状态可查
        orders = self.app.list_payments(local_id)
        self.assertTrue(all(o["status"] == PaymentOrderStatus.SENT.value for o in orders))


if __name__ == "__main__":
    unittest.main()
