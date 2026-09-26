"""应用服务集成：跨统筹区、跨政策日期、冲正链、追溯差额、双人授权、账本可回溯。"""

import unittest

from src.benefits.errors import (
    AuthorizationRequired,
    Conflict,
    DomainError,
    PaymentError,
    SettlementError,
)
from tests._setup import (
    ACCOUNT,
    catalog,
    make_service,
    package,
    package_rules,
    seed_basic,
)


def fee(claim, item, amount, cat="basic_delivery", date="2026-08-01"):
    return {"item_code": item, "category": cat, "amount": amount,
            "service_date": date}


class ServiceFlowTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.tmp = make_service()
        self.store = self.svc.store
        seed_basic(self.store)

    def _open_case(self, *, person="P1", enr="E1", care="B市",
                   delivery="2026-08-01", account=ACCOUNT):
        return self.svc.open_case({
            "person_id": person, "enrollment_id": enr, "care_region": care,
            "delivery_date": delivery, "account": account})["case_id"]

    def _standard_fees(self, cid, claim="CL1"):
        self.svc.report_fees(cid, claim, [
            fee(claim, "D001", 450000),                 # 零自付定额 400000
            fee(claim, "C001", 100000, "complication"), # 80% 报销
            fee(claim, "X999", 5000),                   # 目录外全自付
        ], idem_key=f"report-{claim}")

    # ------------------------------------------------------ 跨统筹区
    def test_catalog_chosen_by_care_region(self):
        # B市目录没有 X999；D市目录把 X999 纳入基础分娩
        self.store.put_catalog(catalog(
            "D市", items={"D001": "basic_delivery",
                          "C001": "complication",
                          "X999": "basic_delivery"}))
        cid_b = self._open_case(care="B市")
        cid_d = self._open_case(care="D市")
        fees = [
            fee("CL", "D001", 300000),   # 低于定额 400000
            fee("CL", "C001", 100000, "complication"),
            fee("CL", "X999", 5000),     # B市目录外 / D市纳入基础分娩
        ]
        self.svc.report_fees(cid_b, "CL-B", fees, idem_key="rb")
        self.svc.report_fees(cid_d, "CL-D", fees, idem_key="rd")
        rb = self.svc.settle_medical(cid_b, idem_key="s-b")
        rd = self.svc.settle_medical(cid_d, idem_key="s-d")
        # B市：X999 范围外全额自付 → 基金 300000+80000，个人 20000+5000
        self.assertEqual((rb["fund_total"], rb["personal_total"]),
                         (380000, 25000))
        # D市：X999 在定额剩余额度内零自付 → 基金 +5000，个人 20000
        self.assertEqual((rd["fund_total"], rd["personal_total"]),
                         (385000, 20000))

    def test_package_chosen_by_home_region(self):
        self.store.upsert_enrollment({
            "enrollment_id": "E2", "person_id": "P2", "home_region": "C市",
            "insured_from": "2025-01-15", "job_kind": "flexible"})
        self.store.put_package(
            package("C市", cap=300000, min_months=12),
            package_rules(cap=300000))
        cid = self._open_case(person="P2", enr="E2")
        self.svc.report_fees(cid, "CL1", [fee("CL1", "D001", 450000)])
        r = self.svc.settle_medical(cid)
        self.assertEqual(r["fund_total"], 300000)
        # C市要求12个月在保，参保人 2025-01 起保 → 2026-08 满足；津贴天数沿用包
        alw = self.svc.decide_allowance(cid, "normal", 1200000)
        self.assertEqual(alw["status"], "approved")

    # ------------------------------------------------------ 跨政策日期
    def test_policy_boundary_selects_versions_by_delivery_date(self):
        # 旧版本 2025 年不覆盖灵活就业；新版本 2026 年覆盖
        self.store.put_package(package(
            "A市", version=0, effective_from="2025-01-01",
            effective_to="2026-01-01", include_flexible=False,
            min_months=0, published_at="2024-12-01T00:00:00Z"),
            package_rules())
        old = self._open_case(delivery="2025-12-31", account=None)
        new = self._open_case(delivery="2026-01-01", account=None)
        a_old = self.svc.decide_allowance(old, "normal", 1200000,
                                          idem_key="a-old")
        a_new = self.svc.decide_allowance(new, "normal", 1200000,
                                          idem_key="a-new")
        self.assertEqual(a_old["status"], "rejected")
        self.assertEqual(a_new["status"], "approved")

    # ------------------------------------------------------ 结算/幂等
    def test_settlement_line_trace_and_zero_copay_boundary(self):
        cid = self._open_case()
        self._standard_fees(cid)
        r = self.svc.settle_medical(cid, idem_key="s1")
        self.assertEqual((r["fund_total"], r["personal_total"],
                          r["charge_total"]), (480000, 75000, 555000))
        out = [ln for ln in r["lines"] if ln["item_code"] == "X999"][0]
        self.assertFalse(out["in_scope"])
        self.assertEqual(out["fund_payable"], 0)
        for ln in r["lines"]:
            self.assertTrue(ln["rule_id"])

    def test_double_settlement_blocked_use_retro_instead(self):
        cid = self._open_case()
        self._standard_fees(cid)
        self.svc.settle_medical(cid, idem_key="s1")
        with self.assertRaises(SettlementError):
            self.svc.settle_medical(cid, idem_key="s2")

    def test_report_retry_and_command_idempotency(self):
        cid = self._open_case()
        a = self.svc.report_fees(cid, "CL1", [fee("CL1", "D001", 100)],
                                 idem_key="k")
        b = self.svc.report_fees(cid, "CL1", [fee("CL1", "D001", 100)],
                                 idem_key="k")
        self.assertEqual(a["seq"], b["seq"])
        # 不同明细内容复用同键：事件层幂等键直接重放（凭证不区分载荷）
        c = self.svc.report_fees(cid, "CL1", [fee("CL1", "D001", 200)],
                                 idem_key="k")
        self.assertEqual(c["seq"], a["seq"])

    # ------------------------------------------------------ 津贴独立
    def test_allowance_independent_of_medical_settlement(self):
        cid = self._open_case()
        # 尚无费用也可审批津贴
        alw = self.svc.decide_allowance(cid, "normal", 1200000,
                                        idem_key="alw")
        self.assertEqual(alw["amount"], 1200000 * 98 // 30)
        expl = self.svc.explain_case(cid)
        self.assertEqual(expl["balances"]["allowance"], alw["amount"])
        self.assertNotIn("medical_fund", expl["balances"])

    def test_allowance_decision_is_terminal(self):
        cid = self._open_case()
        self.svc.decide_allowance(cid, "normal", 1200000, idem_key="a1")
        with self.assertRaises(Conflict):
            self.svc.decide_allowance(cid, "dystocia", 1200000,
                                      idem_key="a2")

    # ------------------------------------------------------ 冲正链
    def test_payment_reversal_chain_and_repay(self):
        cid = self._open_case()
        self._standard_fees(cid)
        self.svc.settle_medical(cid)
        pay = self.svc.issue_payment(cid, "medical_fund", idem_key="p1")
        rev = self.svc.reverse_payment(pay["payment_id"], reason="账户有误",
                                       idem_key="r1")
        expl = self.svc.explain_case(cid)
        self.assertEqual(expl["balances"]["medical_fund"], 480000)
        # 冲正链指针完整
        rev_entry = [e for e in expl["ledger"]
                     if e["action"] == "reversal"][0]
        self.assertEqual(rev_entry["reverses_entry_id"], pay["entry_id"])
        self.assertEqual(rev["reverses_payment_id"], pay["payment_id"])
        # 原指令保留且标记 reversed，不允许二次冲正
        with self.assertRaises(PaymentError):
            self.svc.reverse_payment(pay["payment_id"], reason="再次",
                                     idem_key="r2")
        # 冲正后余额恢复，可重新发起新指令（而不是修改旧指令）
        pay2 = self.svc.issue_payment(cid, "medical_fund", idem_key="p2")
        self.assertNotEqual(pay["payment_id"], pay2["payment_id"])
        self.assertEqual(self.svc.explain_case(cid)
                         ["balances"]["medical_fund"], 0)

    def test_payment_requires_account_and_positive_balance(self):
        cid = self._open_case(account=None)
        self._standard_fees(cid)
        self.svc.settle_medical(cid)
        with self.assertRaises(PaymentError):
            self.svc.issue_payment(cid, "medical_fund")
        # 无应付余额
        cid2 = self._open_case()
        with self.assertRaises(PaymentError):
            self.svc.issue_payment(cid2, "medical_fund")

    # ------------------------------------------------------ 追溯差额
    def test_retro_generates_delta_without_touching_old_records(self):
        import json
        cid = self._open_case()
        self._standard_fees(cid)
        first = self.svc.settle_medical(cid, idem_key="s1")
        old_entries_before = len(self.svc.explain_case(cid)["ledger"])
        # 新版本生效（2026-06-01 起）：定额升至 450000，并发症 90%
        self.store.put_package(package(
            "A市", version=2, effective_from="2026-06-01",
            published_at="2026-05-01T00:00:00Z"),
            package_rules(cap=450000, comp_num=9))
        retro = self.svc.retro_recompute(cid, reason="目录调标")
        # 新基金 = 450000 + 90000 = 540000；旧 480000 → 差额 +60000
        self.assertEqual(retro["fund_total"], 540000)
        self.assertEqual(retro["delta_fund"], 60000)
        expl = self.svc.explain_case(cid)
        # 旧批次原样保留
        old_batch = [s for s in expl["settlements"]
                     if s["settle_id"] == first["settle_id"]][0]
        self.assertEqual(old_batch["fund_total"], 480000)
        self.assertEqual(old_batch["origin"], "settlement")
        # 差额分录引用新旧批次与规则版本
        delta = [e for e in expl["ledger"] if e["origin"] == "retro"][0]
        ref_names = {r["name"]: r["value"] for r in delta["refs"]}
        self.assertEqual(ref_names["prior_settle_id"], first["settle_id"])
        self.assertEqual(ref_names["new_settle_id"], retro["settle_id"])
        self.assertEqual(int(ref_names["delta"]), 60000)
        self.assertIn("PKG-A市@v2", ref_names["package"])
        # 总账：全额重算入账 540000，冲回旧批次 -480000 → 净增 60000
        self.assertEqual(expl["balances"]["medical_fund"],
                         480000 + 60000)
        self.assertGreater(len(expl["ledger"]), old_entries_before)
        # 追溯后从事件日志重建投影必须逐字节稳定
        before = json.dumps(expl, sort_keys=True, default=str,
                            ensure_ascii=False)
        self.store.rebuild()
        after = json.dumps(self.svc.explain_case(cid), sort_keys=True,
                           default=str, ensure_ascii=False)
        self.assertEqual(before, after)

    def test_retro_requires_prior_settlement(self):
        cid = self._open_case()
        self._standard_fees(cid)
        with self.assertRaises(SettlementError):
            self.svc.retro_recompute(cid)

    def test_retro_with_zero_delta_still_reverses_old_batch(self):
        # 新版本规则金额完全相同：差额为 0，但必须冲回旧批次，
        # 否则全额重算会让应付余额虚增一倍
        cid = self._open_case()
        self._standard_fees(cid)
        first = self.svc.settle_medical(cid, idem_key="s0")
        self.store.put_package(package(
            "A市", version=2, effective_from="2026-06-01",
            published_at="2026-06-01T00:00:00Z"),
            package_rules(cap=400000, comp_num=8))
        retro = self.svc.retro_recompute(cid)
        self.assertEqual(retro["delta_fund"], 0)
        expl = self.svc.explain_case(cid)
        # 余额仍等于首次应付，不翻倍
        self.assertEqual(expl["balances"]["medical_fund"],
                         first["fund_total"])
        retro_entry = [e for e in expl["ledger"] if e["origin"] == "retro"]
        self.assertEqual(len(retro_entry), 1)
        self.assertEqual(retro_entry[0]["amount"], -first["fund_total"])
        # 旧批次保留，新旧两批都可查
        origins = [s["origin"] for s in expl["settlements"]]
        self.assertEqual(origins, ["settlement", "retro"])

    # ------------------------------------------------------ 双人授权
    def test_account_change_requires_second_person(self):
        cid = self._open_case()
        new_account = dict(ACCOUNT, bank_code="CCB", account_no="6223")
        t = self.svc.propose_account_change(cid, new_account, "agent1")
        # 发起人不能自批
        with self.assertRaises(Conflict):
            self.svc.approve_ticket(t["ticket_id"], "agent1", True)
        # 批准前不能执行
        with self.assertRaises(Conflict):
            self.svc.execute_account_change(t["ticket_id"], "agent2",
                                            idem_key="exec1")
        self.svc.approve_ticket(t["ticket_id"], "agent2", True)
        self.svc.execute_account_change(t["ticket_id"], "agent2",
                                        idem_key="exec1")
        self.assertEqual(self.svc.get_case(cid)["account"]["bank_code"],
                         "CCB")
        # 票据一次性
        with self.assertRaises(Conflict):
            self.svc.execute_account_change(t["ticket_id"], "agent2",
                                            idem_key="exec2")

    def test_rejected_account_ticket_cannot_execute(self):
        cid = self._open_case()
        t = self.svc.propose_account_change(
            cid, dict(ACCOUNT, account_no="9"), "agent1")
        self.svc.approve_ticket(t["ticket_id"], "agent2", False, "账号可疑")
        with self.assertRaises(Conflict):
            self.svc.execute_account_change(t["ticket_id"], "agent2")

    def test_high_adjustment_dual_authorization_and_tamper_rejection(self):
        cid = self._open_case()
        with self.assertRaises(AuthorizationRequired):
            self.svc.manual_adjustment(cid, "medical_fund", 6_000_000, "特批")
        t = self.svc.propose_manual_adjustment(
            cid, "medical_fund", 6_000_000, "特批", "agent1")
        self.svc.approve_ticket(t["ticket_id"], "agent2", True)
        # 执行时金额被篡改 → 哈希不符
        with self.assertRaises(Conflict):
            self.svc.manual_adjustment(cid, "medical_fund", 6_000_001,
                                      "特批", auth_ticket_id=t["ticket_id"])
        self.svc.manual_adjustment(cid, "medical_fund", 6_000_000, "特批",
                                   auth_ticket_id=t["ticket_id"])
        expl = self.svc.explain_case(cid)
        self.assertEqual(expl["balances"]["medical_fund"], 6_000_000)

    def test_low_adjustment_direct_but_threshold_boundary_is_dual(self):
        cid = self._open_case()
        self.svc.manual_adjustment(cid, "medical_fund", 4_999_999, "普通调账")
        with self.assertRaises(AuthorizationRequired):
            self.svc.manual_adjustment(cid, "medical_fund", 5_000_000, "达阈值")

    # ------------------------------------------------------ 可解释账本
    def test_every_payable_traces_to_fee_and_rule(self):
        cid = self._open_case()
        self._standard_fees(cid)
        self.svc.settle_medical(cid)
        expl = self.svc.explain_case(cid)
        payable = [e for e in expl["ledger"]
                   if e["action"] == "payable" and e["kind"] == "medical_fund"]
        self.assertEqual(len(payable), 2)  # D001 与 C001
        for e in payable:
            names = {r["name"] for r in e["refs"]}
            self.assertIn("item_code", names)
            self.assertIn("rule_id", names)
            self.assertIn("settle_id", names)
        # 个人负担逐行可查
        personal = sum(ln["personal_payable"]
                       for s in expl["settlements"] for ln in s["lines"])
        self.assertEqual(personal, 75000)

    def test_version_optimistic_lock(self):
        cid = self._open_case()
        self.svc.report_fees(cid, "CL1", [fee("CL1", "D001", 100)],
                             expected_version=1)
        with self.assertRaises(Conflict):
            self.svc.report_fees(cid, "CL2", [fee("CL2", "D001", 100)],
                                 expected_version=1)


if __name__ == "__main__":
    unittest.main()
