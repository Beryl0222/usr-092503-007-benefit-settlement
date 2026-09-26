"""双人授权与规则追溯验证：账户变更、高额调整、追溯差额不改旧账。"""

from src.benefits.contracts import TicketStatus, TicketType
from src.benefits.errors import DomainError

from tests.helpers import AppCase, bill_lines


class ApprovalTests(AppCase):
    def setUp(self):
        super().setUp()
        self.configure_world()
        res = self.app.ingest_bill(
            hospital_id="H-B01", upload_id="U1", person_id="P-1001",
            delivery_date="2026-09-20", lines=bill_lines(), actor="hosp",
        )
        self.case_id = res["case_id"]

    def test_first_account_register_is_direct(self):
        out = self.app.register_account(
            case_id=self.case_id, account_no="62220001", account_name="张某",
            bank_code="ICBC", actor="clerk1",
        )
        self.assertIsNone(out["ticket_id"])

    def test_account_change_requires_dual_control(self):
        self.test_first_account_register_is_direct()
        out = self.app.register_account(
            case_id=self.case_id, account_no="62220002", account_name="张某",
            bank_code="ICBC", actor="clerk1",
        )
        self.assertIsNotNone(out["ticket_id"])
        # 未审批前账户不变
        self.assertEqual(
            self.app.explain(self.case_id)["case"]["case_id"], self.case_id
        )
        # 发起人不能自审
        with self.assertRaises(DomainError) as ctx:
            self.app.approve_ticket(ticket_id=out["ticket_id"], approver="clerk1")
        self.assertEqual(ctx.exception.code, "dual_control_required")
        # 另一经办人审批后生效
        self.app.approve_ticket(ticket_id=out["ticket_id"], approver="clerk2")
        with self.app.db.read_tx() as conn:
            row = conn.execute(
                "SELECT account_no FROM accounts WHERE case_id = ?", (self.case_id,)
            ).fetchone()
        self.assertEqual(row["account_no"], "62220002")

    def test_rejected_account_change_has_no_effect(self):
        self.test_first_account_register_is_direct()
        out = self.app.register_account(
            case_id=self.case_id, account_no="62229999", account_name="张某",
            bank_code="CCB", actor="clerk1",
        )
        self.app.reject_ticket(
            ticket_id=out["ticket_id"], approver="clerk2", reason="核实非本人申请"
        )
        with self.app.db.read_tx() as conn:
            row = conn.execute(
                "SELECT account_no FROM accounts WHERE case_id = ?", (self.case_id,)
            ).fetchone()
        self.assertEqual(row["account_no"], "62220001")

    def test_low_adjustment_direct_high_adjustment_ticketed(self):
        low = self.app.manual_adjustment(
            case_id=self.case_id, amount=1000_00, note="零星补退", actor="clerk1",
        )
        self.assertIsNotNone(low["ledger_entry_id"])
        high = self.app.manual_adjustment(
            case_id=self.case_id, amount=6000_00, note="大额补付", actor="clerk1",
        )
        self.assertIsNotNone(high["ticket_id"])
        ticket = self.app.get_ticket(high["ticket_id"])
        self.assertEqual(ticket["ticket_type"], TicketType.MANUAL_ADJUSTMENT.value)
        self.assertEqual(ticket["status"], TicketStatus.PENDING.value)
        # 审批前高额差额未入账
        ex_before = self.app.explain(self.case_id)
        self.app.approve_ticket(ticket_id=high["ticket_id"], approver="clerk2")
        ex_after = self.app.explain(self.case_id)
        self.assertEqual(len(ex_after["ledger"]), len(ex_before["ledger"]) + 1)
        high_entry = next(
            e for e in ex_after["ledger"]
            if e["ref_type"] == "approval_ticket" and e["ref_id"] == high["ticket_id"]
        )
        self.assertEqual(high_entry["amount"], 6000_00)


class RetroTests(AppCase):
    def test_retro_package_produces_delta_without_rewriting_history(self):
        self.configure_world()
        res = self.app.ingest_bill(
            hospital_id="H-B01", upload_id="U1", person_id="P-1001",
            delivery_date="2026-09-20", lines=bill_lines(), actor="hosp",
        )
        case_id = res["case_id"]
        ex = self.app.explain(case_id)
        v1 = ex["settlement_runs"][-1]
        v1_payable = v1["payable_total"]
        v1_run_id = v1["run_id"]

        # 追溯发布：镇痛支付比例 70%→90%，生效日回溯到 2026-01-01（同生效日新版本）
        from tests.helpers import standard_rules
        from src.benefits.policy import CategoryRule

        new_rules = standard_rules(
            analgesia=CategoryRule(cap=3000_00, rate_bp=9000),
        )
        pkg = self.app.publish_package("330100", "2026-01-01", new_rules, actor="admin")
        scheduled = self.app.schedule_retro(
            region_code="330100", package_id=pkg["package_id"],
            effective_from="2026-01-01", actor="admin",
        )
        self.assertIn(case_id, scheduled)

        from src.benefits.batch import DailySettlement
        report = DailySettlement(self.app.db, self.app.clock).run("2026-09-26")
        self.assertTrue(
            any(item["case_id"] == case_id for item in report["stages"]["retro"])
        )

        ex2 = self.app.explain(case_id)
        runs = ex2["settlement_runs"]
        self.assertEqual(len(runs), 2)
        v2 = runs[-1]
        self.assertEqual(v2["reason"], "retro")
        self.assertEqual(v2["version"], 2)
        self.assertGreater(v2["payable_total"], v1_payable)

        # 旧版本与旧账行原样保留
        self.assertEqual(runs[0]["run_id"], v1_run_id)
        self.assertEqual(runs[0]["payable_total"], v1_payable)
        medical = [e for e in ex2["ledger"] if e["subject"] == "medical"]
        types = [e["action"] for e in medical]
        self.assertEqual(types.count("payable"), 1)
        self.assertEqual(types.count("adjustment"), 1)
        # 差额 = 新版本应付 - 旧版本应付
        delta = next(e for e in medical if e["action"] == "adjustment")["amount"]
        self.assertEqual(delta, v2["payable_total"] - v1_payable)
        # 账本累计应付 == 新版本应付
        self.assertEqual(
            sum(e["amount"] for e in medical), v2["payable_total"]
        )

    def test_cross_policy_date_picks_package_by_delivery_date(self):
        from tests.helpers import standard_rules
        from src.benefits.policy import CategoryRule

        self.configure_world()
        # 2027-01-01 起新服务包：并发症比例提高到 90%
        new_rules = standard_rules(
            complication=CategoryRule(cap=30000_00, rate_bp=9000, deductible=800_00),
        )
        self.app.publish_package("330100", "2027-01-01", new_rules, actor="admin")

        res_old = self.app.ingest_bill(
            hospital_id="H-B01", upload_id="U-OLD", person_id="P-1001",
            delivery_date="2026-12-20",
            lines=bill_lines()[:4], actor="hosp",
        )
        res_new = self.app.ingest_bill(
            hospital_id="H-B01", upload_id="U-NEW", person_id="P-1002",
            delivery_date="2027-01-05",
            lines=bill_lines()[:4], actor="hosp",
        )
        old = self.app.explain(res_old["case_id"])["settlement_runs"][-1]
        new = self.app.explain(res_new["case_id"])["settlement_runs"][-1]
        old_comp = next(l for l in old["lines"] if l["category"] == "complication")
        new_comp = next(l for l in new["lines"] if l["category"] == "complication")
        self.assertEqual(old_comp["payable"], 3360_00)
        self.assertEqual(new_comp["payable"], 3780_00)


if __name__ == "__main__":
    unittest.main()
