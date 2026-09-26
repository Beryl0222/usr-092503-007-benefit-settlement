"""HTTP API 端到端验证：角色控制、幂等键、医院重试、日结接口。"""

import json
import threading
import urllib.error
import urllib.request

from src.benefits.api import serve

from tests.helpers import AppCase, bill_lines


class HttpCase(AppCase):
    def setUp(self):
        super().setUp()
        self.configure_world()
        self.server = serve(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, *, actor="clerk1", role="agency",
                idem_key=None, expect_error=False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Actor-Id": actor, "X-Actor-Role": role}
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            payload = json.loads(e.read())
            if expect_error:
                return e.code, payload
            raise AssertionError(f"HTTP {e.code}: {payload}") from e

    def hospital_upload(self, upload_id, *, idem_key=None, lines=None):
        return self.request(
            "POST", "/api/v1/hospital/bills",
            {
                "hospital_id": "H-B01", "upload_id": upload_id,
                "person_id": "P-1001", "delivery_date": "2026-09-20",
                "lines": lines or bill_lines(),
            },
            actor="hosp-b01", role="hospital", idem_key=idem_key,
        )

    def test_role_enforcement(self):
        code, payload = self.request(
            "POST", "/api/v1/hospital/bills", {},
            actor="hosp", role="agency", expect_error=True,
        )
        self.assertEqual(code, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")

    def test_missing_actor(self):
        url = f"http://127.0.0.1:{self.port}/api/v1/hospital/bills"
        req = urllib.request.Request(url, data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

    def test_hospital_retry_with_idempotency_header(self):
        status1, r1 = self.hospital_upload("UP-1", idem_key="KEY-1")
        status2, r2 = self.hospital_upload("UP-1", idem_key="KEY-1")
        self.assertEqual(status1, 200)
        self.assertEqual(r1["case_id"], r2["case_id"])
        # 同键不同请求体
        code, payload = self.request(
            "POST", "/api/v1/hospital/bills",
            {
                "hospital_id": "H-B01", "upload_id": "UP-1",
                "person_id": "P-1001", "delivery_date": "2026-09-20",
                "lines": bill_lines()[:1],  # 体不同
            },
            actor="hosp-b01", role="hospital", idem_key="KEY-1", expect_error=True,
        )
        self.assertEqual(code, 409)
        self.assertEqual(payload["error"]["code"], "idempotency_mismatch")

    def test_full_case_flow_via_http(self):
        _, upload = self.hospital_upload("UP-9")
        case_id = upload["case_id"]
        # 异地终态回执
        self.request(
            "POST", "/api/v1/agency/receipts",
            {"case_id": case_id, "receipt_id": "R1", "stage": "final",
             "confirmed_amount": 9000_00},
        )
        # 津贴提交 + 另一经办人核定
        _, submitted = self.request(
            "POST", f"/api/v1/agency/cases/{case_id}/allowance",
            {"base_salary": 1_500_000, "leave_days": 158},
        )
        self.request(
            "POST", f"/api/v1/agency/allowances/{submitted['allowance_id']}/approve",
            {"expected_version": submitted["version"]}, actor="clerk2",
        )
        # 账户
        self.request(
            "POST", f"/api/v1/agency/cases/{case_id}/account",
            {"account_no": "62220001", "account_name": "张某", "bank_code": "ICBC"},
        )
        # 日结
        _, report = self.request(
            "POST", "/api/v1/agency/batch/daily-settlement",
            {"as_of_date": "2026-09-26"}, idem_key="BATCH-KEY",
        )
        self.assertEqual(report["totals"]["orders_sent"], 2)
        # 日结幂等：同键重放
        _, report2 = self.request(
            "POST", "/api/v1/agency/batch/daily-settlement",
            {"as_of_date": "2026-09-26"}, idem_key="BATCH-KEY",
        )
        self.assertEqual(report2, report)

        # explain 可回溯
        status, ex = self.request(
            "GET", f"/api/v1/agency/cases/{case_id}/explain", body={}
        )
        self.assertEqual(status, 200)
        self.assertTrue(ex["ledger"])
        latest = ex["settlement_runs"][-1]
        for line in latest["lines"]:
            self.assertEqual(line["amount"], line["payable"] + line["personal"])

    def test_account_change_dual_control_via_http(self):
        _, upload = self.hospital_upload("UP-2")
        case_id = upload["case_id"]
        self.request(
            "POST", f"/api/v1/agency/cases/{case_id}/account",
            {"account_no": "62220001", "account_name": "张某", "bank_code": "ICBC"},
        )
        _, change = self.request(
            "POST", f"/api/v1/agency/cases/{case_id}/account",
            {"account_no": "62220002", "account_name": "张某", "bank_code": "ICBC"},
        )
        ticket_id = change["ticket_id"]
        # 发起人自审 403
        code, payload = self.request(
            "POST", f"/api/v1/agency/tickets/{ticket_id}/approve", {},
            actor="clerk1", expect_error=True,
        )
        self.assertEqual(code, 403)
        self.request(
            "POST", f"/api/v1/agency/tickets/{ticket_id}/approve", {},
            actor="clerk2",
        )
        status, ex = self.request(
            "GET", f"/api/v1/hospital/cases/{case_id}", body={},
            actor="hosp-b01", role="hospital",
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
