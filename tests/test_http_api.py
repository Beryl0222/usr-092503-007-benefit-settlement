"""HTTP API 端到端：真实端口、JSON 报文、幂等头、版本冲突、双人授权、解释账本。"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from src.benefits.api import build_server
from tests._setup import ACCOUNT, catalog, package, package_rules


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp(prefix="benefit-http-")
        cls.server = build_server(cls.tmp + "/db.sqlite", "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        # 基础主数据只播种一次（规则版本不可变）
        svc = cls.server.service
        svc.store.upsert_enrollment({
            "enrollment_id": "E1", "person_id": "P1", "home_region": "A市",
            "insured_from": "2025-01-15", "job_kind": "flexible"})
        svc.store.put_package(package("A市"), package_rules())
        svc.store.put_catalog(catalog("B市"))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None, *, idem=None, actor="hosp"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if idem:
            req.add_header("Idempotency-Key", idem)
        req.add_header("X-Actor", actor)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health(self):
        status, body = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_full_journey_with_idempotent_replay(self):
        case_body = {
            "person_id": "P1", "enrollment_id": "E1", "care_region": "B市",
            "delivery_date": "2026-09-10", "account": ACCOUNT}
        s1, b1 = self.call("POST", "/v1/cases", case_body, idem="open",
                           actor="hosp")
        s2, b2 = self.call("POST", "/v1/cases", case_body, idem="open",
                           actor="hosp")
        self.assertEqual(s1, s2)
        self.assertEqual(b1["data"]["case_id"], b2["data"]["case_id"])
        cid = b1["data"]["case_id"]

        # 同键不同载荷 → 409
        s3, b3 = self.call("POST", "/v1/cases",
                           dict(case_body, delivery_date="2026-09-11"),
                           idem="open", actor="hosp")
        self.assertEqual(s3, 409)
        self.assertEqual(b3["error"]["code"], "conflict")

        fees = {"claim_id": "CL1", "lines": [
            {"item_code": "D001", "category": "basic_delivery",
             "amount": 450000, "service_date": "2026-09-10"},
            {"item_code": "C001", "category": "complication",
             "amount": 100000, "service_date": "2026-09-10"},
            {"item_code": "ZZZ", "category": "basic_delivery",
             "amount": 7000, "service_date": "2026-09-10"}]}
        f1, _ = self.call("POST", f"/v1/cases/{cid}/reports", fees,
                          idem="rep", actor="hosp")
        f2, rb2 = self.call("POST", f"/v1/cases/{cid}/reports", fees,
                            idem="rep", actor="hosp")
        self.assertEqual(f1, f2)

        # 乱序异地回执
        st, _ = self.call("POST", f"/v1/cases/{cid}/remote-receipts", {
            "claim_id": "CL1", "receipts": [{
                "item_code": "C001", "accepted_amount": 80000,
                "batch_seq": 1}]}, idem="rcp", actor="remote")
        self.assertEqual(st, 200)

        st, stl = self.call("POST", f"/v1/cases/{cid}/settlements", {},
                            idem="stl", actor="agent")
        self.assertEqual(st, 200)
        # D001 定额 400000 + C001 回执核定 80000×80%=64000
        self.assertEqual(stl["data"]["fund_total"], 464000)
        # 个人：D001 超定额 50000 + C001 16000 + 目录外 ZZZ 7000
        self.assertEqual(stl["data"]["personal_total"], 73000)
        # 幂等重放结算
        _, stl2 = self.call("POST", f"/v1/cases/{cid}/settlements", {},
                            idem="stl", actor="agent")
        self.assertEqual(stl2["data"]["settle_id"],
                         stl["data"]["settle_id"])

        # 津贴独立审批
        st, alw = self.call(
            "POST", f"/v1/cases/{cid}/allowance-decisions",
            {"kind": "normal", "monthly_base": 1200000},
            idem="alw", actor="agent")
        self.assertEqual(alw["data"]["status"], "approved")

        # 解释账本
        st, expl = self.call("GET", f"/v1/cases/{cid}/explain")
        self.assertEqual(st, 200)
        self.assertEqual(expl["data"]["balances"]["medical_fund"], 464000)
        self.assertTrue(expl["data"]["ledger"])

        # 拨付
        st, pay = self.call("POST", f"/v1/cases/{cid}/payments",
                            {"kind": "medical_fund"}, idem="pay",
                            actor="treasury")
        self.assertEqual(pay["data"]["amount"], 464000)
        # 重复拨付 → 422
        st, err = self.call("POST", f"/v1/cases/{cid}/payments",
                            {"kind": "medical_fund"}, idem="pay2",
                            actor="treasury")
        self.assertEqual(st, 422)
        self.assertEqual(err["error"]["code"], "payment_error")

        # 冲正
        st, rev = self.call(
            "POST", f"/v1/payments/{pay['data']['payment_id']}/reversal",
            {"reason": "账户冻结"}, idem="rev", actor="treasury")
        self.assertEqual(st, 200)
        self.assertEqual(rev["data"]["amount"], 464000)

    def test_dual_authorization_over_http(self):
        _, b = self.call("POST", "/v1/cases", {
            "person_id": "P1", "enrollment_id": "E1", "care_region": "B市",
            "delivery_date": "2026-09-20", "account": ACCOUNT},
            idem="open2", actor="hosp")
        cid = b["data"]["case_id"]
        new_account = dict(ACCOUNT, bank_code="CCB", account_no="6223")

        st, prop = self.call(
            "POST", f"/v1/cases/{cid}/account-change/proposals",
            {"account": new_account, "reason": "换卡"}, actor="agent1")
        self.assertEqual(st, 200)
        tid = prop["data"]["ticket_id"]

        # 自批被拒
        st, err = self.call(
            "POST", f"/v1/auth-tickets/{tid}/decision",
            {"approve": True}, actor="agent1")
        self.assertEqual(st, 409)

        # 第二人批准并执行
        st, _ = self.call(
            "POST", f"/v1/auth-tickets/{tid}/decision",
            {"approve": True}, actor="agent2")
        self.assertEqual(st, 200)
        st, _ = self.call("POST", f"/v1/auth-tickets/{tid}/execute", {},
                          idem="exec", actor="agent2")
        self.assertEqual(st, 200)
        _, case_b = self.call("GET", f"/v1/cases/{cid}")
        self.assertEqual(case_b["data"]["account"]["bank_code"], "CCB")

    def test_expected_version_conflict_returns_409(self):
        _, b = self.call("POST", "/v1/cases", {
            "person_id": "P1", "enrollment_id": "E1", "care_region": "B市",
            "delivery_date": "2026-09-25", "account": ACCOUNT},
            idem="open3", actor="hosp")
        cid = b["data"]["case_id"]
        fees = {"claim_id": "CL1", "lines": [{
            "item_code": "D001", "category": "basic_delivery", "amount": 100,
            "service_date": "2026-09-25"}], "expected_version": 1}
        st, ok = self.call("POST", f"/v1/cases/{cid}/reports", fees,
                           actor="hosp")
        self.assertEqual(st, 200)
        st, err = self.call("POST", f"/v1/cases/{cid}/reports", fees,
                            idem="stale", actor="hosp")
        self.assertEqual(st, 409)
        self.assertEqual(err["error"]["code"], "conflict")

    def test_unknown_case_returns_404(self):
        st, err = self.call("GET", "/v1/cases/CASE-NOPE")
        self.assertEqual(st, 404)
        self.assertEqual(err["error"]["code"], "not_found")

    def test_bad_json_returns_422(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/cases",
            data=b"not-json", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            self.assertEqual(e.code, 422)
            self.assertEqual(body["error"]["code"], "domain_error")
            return
        self.fail("应当返回错误")


if __name__ == "__main__":
    unittest.main()
