"""进程恢复：重开数据库、事件重放、投影重建、规则/凭证/审计持久化。"""

import json
import unittest

from src.benefits.service import BenefitService
from src.benefits.store import Store
from tests._setup import ACCOUNT, seed_basic


def fee(claim, item, amount, cat="basic_delivery"):
    return {"item_code": item, "category": cat, "amount": amount,
            "service_date": "2026-08-01"}


class PersistenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="benefit-restart-")
        self.path = self.tmp + "/db.sqlite"

    def _svc(self):
        return BenefitService(Store(self.path))

    def _build_history(self):
        svc = self._svc()
        seed_basic(svc.store)
        cid = svc.open_case({
            "person_id": "P1", "enrollment_id": "E1", "care_region": "B市",
            "delivery_date": "2026-08-01", "account": ACCOUNT})["case_id"]
        svc.remote_receipt(cid, "CL1", [{
            "item_code": "C001", "accepted_amount": 80000,
            "batch_seq": 1}])
        svc.report_fees(cid, "CL1", [
            fee("CL1", "D001", 450000),
            fee("CL1", "C001", 100000, "complication"),
            fee("CL1", "X999", 5000),
        ])
        svc.settle_medical(cid, idem_key="settle-1")
        svc.decide_allowance(cid, "normal", 1200000, idem_key="alw-1")
        svc.issue_payment(cid, "medical_fund", idem_key="pay-1")
        return cid

    def test_reopen_process_sees_same_facts_and_balances(self):
        cid = self._build_history()
        before = self._svc().explain_case(cid)
        # 模拟新进程：重新打开数据库
        after = self._svc().explain_case(cid)
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["balances"], before["balances"])
        self.assertEqual(
            [(e["seq"], e["action"], e["amount"]) for e in after["ledger"]],
            [(e["seq"], e["action"], e["amount"]) for e in before["ledger"]])
        self.assertEqual(len(after["fee_facts"]), 3)
        merged = [f for f in after["fee_facts"]
                  if f["item_code"] == "C001"][0]
        self.assertEqual(merged["charge_amount"], 80000)

    def test_rebuild_from_event_log_is_byte_stable(self):
        cid = self._build_history()
        svc = self._svc()
        before = json.dumps(svc.explain_case(cid), sort_keys=True,
                            default=str, ensure_ascii=False)
        counts = svc.store.rebuild()
        self.assertGreater(counts["events"], 0)
        after = json.dumps(svc.explain_case(cid), sort_keys=True,
                           default=str, ensure_ascii=False)
        self.assertEqual(before, after)
        # 再次重建结果不变（投影幂等）
        svc.store.rebuild()
        after2 = json.dumps(svc.explain_case(cid), sort_keys=True,
                            default=str, ensure_ascii=False)
        self.assertEqual(before, after2)

    def test_event_idempotency_key_persists_across_restart(self):
        cid = self._build_history()
        svc = self._svc()
        # 重启后用同一命令幂等键结算，必须重放而非重复入账
        r = svc.settle_medical(cid, idem_key="settle-1")
        self.assertTrue(r["replayed"])
        self.assertEqual(len(svc.explain_case(cid)["settlements"]), 1)

    def test_policy_versions_persist_across_restart(self):
        cid = self._build_history()
        svc = self._svc()
        registry = svc.store.load_registry()
        pkg = registry.package_for("A市", "2026-08-01")
        self.assertEqual(pkg.code, "PKG-A市")
        self.assertIn("complication",
                      [c.value for c in pkg.category_rules])
        cat = registry.catalog_for("B市", "2026-08-01")
        self.assertEqual(cat.items["D001"].value, "basic_delivery")
        _ = cid

    def test_audit_trail_persists(self):
        self._build_history()
        svc = self._svc()
        actions = {e["action"] for e in svc.store.audit_tail(1000)}
        for required in ("case_opened", "fees_reported",
                         "medical_settled", "allowance_decided",
                         "payment_issued", "remote_receipt",
                         "package_put", "catalog_put"):
            self.assertIn(required, actions)

    def test_replay_supports_late_receipt_after_restart(self):
        cid = self._build_history()
        svc = self._svc()
        # 进程恢复后又来一条乱序回执（另一项目）
        svc.remote_receipt(cid, "CL1", [{
            "item_code": "D001", "accepted_amount": 440000,
            "batch_seq": 1}])
        facts = svc.explain_case(cid)["fee_facts"]
        d001 = [f for f in facts if f["item_code"] == "D001"][0]
        self.assertTrue(d001["remote_confirmed"])
        self.assertEqual(d001["charge_amount"], 440000)


if __name__ == "__main__":
    unittest.main()
