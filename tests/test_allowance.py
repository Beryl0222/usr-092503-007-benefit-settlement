"""生育津贴验证：独立推进与并发审批只有一方成功。"""

import threading

from src.benefits.contracts import AllowanceStatus
from src.benefits.errors import DomainError
from src.benefits.ledger import case_balance

from tests.helpers import AppCase, bill_lines


class AllowanceTests(AppCase):
    def setUp(self):
        super().setUp()
        self.configure_world()
        self.res = self.app.ingest_bill(
            hospital_id="H-B01", upload_id="U1", person_id="P-1001",
            delivery_date="2026-09-20", lines=bill_lines(), actor="hosp",
        )
        self.case_id = self.res["case_id"]

    def submit(self):
        return self.app.submit_allowance(
            case_id=self.case_id, base_salary=1_500_000,
            leave_days=158, actor="clerk1",
        )

    def test_allowance_independent_from_medical_settlement(self):
        # 医疗尚未终态回执，津贴仍可提交、核定
        allowance = self.submit()
        self.assertEqual(allowance["status"], AllowanceStatus.SUBMITTED.value)
        self.assertEqual(allowance["computed_amount"], 7_900_000)
        case = self.app.get_case(self.case_id)
        self.assertEqual(case["receipt_final"], 0)
        self.assertEqual(case["allowance_status"], AllowanceStatus.SUBMITTED.value)

    def test_self_approval_rejected(self):
        allowance = self.submit()
        with self.assertRaises(DomainError) as ctx:
            self.app.approve_allowance(
                allowance_id=allowance["allowance_id"], actor="clerk1",
                expected_version=allowance["version"],
            )
        self.assertEqual(ctx.exception.code, "self_approval")

    def test_concurrent_approval_only_one_succeeds(self):
        allowance = self.submit()
        version = allowance["version"]
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def approve(actor: str):
            barrier.wait()
            try:
                self.app.approve_allowance(
                    allowance_id=allowance["allowance_id"], actor=actor,
                    expected_version=version,
                )
                outcomes.append(f"{actor}:ok")
            except DomainError as e:
                outcomes.append(f"{actor}:{e.code}")

        t1 = threading.Thread(target=approve, args=("clerk2",))
        t2 = threading.Thread(target=approve, args=("clerk3",))
        t1.start(); t2.start(); t1.join(); t2.join()

        ok = [o for o in outcomes if o.endswith(":ok")]
        lost = [o for o in outcomes if not o.endswith(":ok")]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(lost), 1)
        # 落败方可能在状态变更前或后撞入：两类码都表示并发失败
        self.assertIn(lost[0].split(":")[1],
                      ("concurrent_approval", "allowance_not_submitted"))
        final = self.app.get_allowance_by_case(self.case_id)
        self.assertEqual(final["status"], AllowanceStatus.APPROVED.value)
        self.assertIn(final["decided_by"], ("clerk2", "clerk3"))
        # 津贴科目只入账一次
        self.assertEqual(
            case_balance(self.app.db.connection(), self.case_id, subject="allowance"),
            7_900_000,
        )

    def test_reject_then_resubmit(self):
        allowance = self.submit()
        self.app.reject_allowance(
            allowance_id=allowance["allowance_id"], actor="clerk2",
            reason="材料不全", expected_version=allowance["version"],
        )
        again = self.submit()
        self.assertEqual(again["status"], AllowanceStatus.SUBMITTED.value)
        approved = self.app.approve_allowance(
            allowance_id=again["allowance_id"], actor="clerk3",
            expected_version=again["version"],
        )
        self.assertEqual(approved["status"], AllowanceStatus.APPROVED.value)


if __name__ == "__main__":
    unittest.main()
