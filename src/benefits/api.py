"""HTTP API：医院端与经办机构端。

- 认证简化：请求头 X-Actor-Id（操作员）与 X-Actor-Role（hospital/agency），
  角色必须与路径前缀匹配；
- 幂等：写接口接受 Idempotency-Key 头。业务执行与幂等凭证在同一事务内
  落库：同键同请求体重放返回首个响应；同键不同请求体返回 409。
  进程重启后凭证仍有效；
- 批量日结按结账日天然幂等（见 batch.py），凭证仅缓存其报告；
- 所有响应 JSON；业务错误返回 {"error": {"code", "message"}}。
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .app import _Bundle
from .errors import DomainError


def _request_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class _Router:
    def __init__(self):
        self.routes: list[tuple[str, str, Callable]] = []

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        self.routes.append((method, pattern, handler))

    def match(self, method: str, path: str):
        for m, pattern, handler in self.routes:
            params = _match(pattern, path)
            if m == method and params is not None:
                return handler, params
        return None, None


def _match(pattern: str, path: str) -> dict | None:
    pp, sp = pattern.strip("/").split("/"), path.strip("/").split("/")
    if len(pp) != len(sp):
        return None
    params = {}
    for p, s in zip(pp, sp):
        if p.startswith("{") and p.endswith("}"):
            params[p[1:-1]] = s
        elif p != s:
            return None
    return params


# 动作签名：(bundle, path_params, body, actor) -> JSON 可序列化结果
def make_handler(app):
    router = _Router()
    idem_lock = threading.Lock()

    # ---------------- 医院端 ----------------
    def upload(b, p, body, actor):
        return b.cases.ingest_bill(
            hospital_id=body["hospital_id"],
            upload_id=body["upload_id"],
            person_id=body["person_id"],
            delivery_date=body["delivery_date"],
            lines=body["lines"],
            kind="original",
            actor=actor,
        )

    def correct(b, p, body, actor):
        return b.cases.ingest_bill(
            hospital_id=body["hospital_id"],
            upload_id=body["upload_id"],
            person_id=body["person_id"],
            delivery_date=body["delivery_date"],
            lines=body["lines"],
            kind="correction",
            corrects_bill_id=p["bill_id"],
            actor=actor,
        )

    router.add("POST", "/api/v1/hospital/bills", upload)
    router.add("POST", "/api/v1/hospital/bills/{bill_id}/corrections", correct)
    router.add("GET", "/api/v1/hospital/cases/{case_id}",
               lambda b, p, body, a: b.cases.explain(p["case_id"]))

    # ---------------- 经办端：写 ----------------
    router.add("POST", "/api/v1/agency/receipts",
               lambda b, p, body, a: b.cases.ingest_receipt(
                   case_id=body["case_id"], receipt_id=body["receipt_id"],
                   stage=body["stage"], confirmed_amount=body["confirmed_amount"], actor=a))
    router.add("POST", "/api/v1/agency/cases/{case_id}/allowance",
               lambda b, p, body, a: b.allowance.submit(
                   case_id=p["case_id"], base_salary=body["base_salary"],
                   leave_days=body["leave_days"], actor=a))
    router.add("POST", "/api/v1/agency/allowances/{allowance_id}/approve",
               lambda b, p, body, a: b.allowance.approve(
                   allowance_id=p["allowance_id"], actor=a,
                   approved_amount=body.get("approved_amount"),
                   expected_version=body["expected_version"]))
    router.add("POST", "/api/v1/agency/allowances/{allowance_id}/reject",
               lambda b, p, body, a: b.allowance.reject(
                   allowance_id=p["allowance_id"], actor=a,
                   reason=body["reason"], expected_version=body["expected_version"]))
    router.add("POST", "/api/v1/agency/cases/{case_id}/account",
               lambda b, p, body, a: b.approvals.register_account(
                   case_id=p["case_id"], account_no=body["account_no"],
                   account_name=body["account_name"], bank_code=body["bank_code"], actor=a))
    router.add("POST", "/api/v1/agency/tickets/{ticket_id}/approve",
               lambda b, p, body, a: b.approvals.approve(
                   ticket_id=p["ticket_id"], approver=a))
    router.add("POST", "/api/v1/agency/tickets/{ticket_id}/reject",
               lambda b, p, body, a: b.approvals.reject(
                   ticket_id=p["ticket_id"], approver=a, reason=body["reason"]))
    router.add("POST", "/api/v1/agency/cases/{case_id}/adjustments",
               lambda b, p, body, a: b.approvals.manual_adjustment(
                   case_id=p["case_id"], amount=body["amount"],
                   note=body["note"], actor=a))
    router.add("POST", "/api/v1/agency/cases/{case_id}/payments",
               lambda b, p, body, a: b.payments.create_order(
                   case_id=p["case_id"], subject=body["subject"], amount=body["amount"],
                   payee_account=body["payee_account"],
                   idem_key=body.get("idem_key"), actor=a))
    router.add("POST", "/api/v1/agency/payments/{order_id}/send",
               lambda b, p, body, a: b.payments.send_order(p["order_id"], actor=a))
    router.add("POST", "/api/v1/agency/payments/{order_id}/ack",
               lambda b, p, body, a: b.payments.ack_order(p["order_id"], actor=a))
    router.add("POST", "/api/v1/agency/payments/{order_id}/reverse",
               lambda b, p, body, a: b.payments.reverse_order(
                   p["order_id"], reason=body["reason"], actor=a))
    router.add("POST", "/api/v1/agency/retro/schedule",
               lambda b, p, body, a: b.retro.schedule_for_package(
                   region_code=body["region_code"], package_id=body["package_id"],
                   effective_from=body["effective_from"],
                   effective_to=body.get("effective_to"), actor=a))
    router.add("POST", "/api/v1/agency/batch/daily-settlement",
               lambda b, p, body, a: None)  # 在分发层特判（跨多事务）

    # ---------------- 经办端：读 ----------------
    router.add("GET", "/api/v1/agency/cases/{case_id}",
               lambda b, p, body, a: b.cases.get_case(p["case_id"]))
    router.add("GET", "/api/v1/agency/cases/{case_id}/explain",
               lambda b, p, body, a: b.cases.explain(p["case_id"]))
    router.add("GET", "/api/v1/agency/cases/{case_id}/allowance",
               lambda b, p, body, a: b.allowance.get_by_case(p["case_id"]) or {})
    router.add("GET", "/api/v1/agency/cases/{case_id}/payments",
               lambda b, p, body, a: {"orders": b.payments.orders_for_case(p["case_id"])})
    router.add("GET", "/api/v1/agency/tickets/{ticket_id}",
               lambda b, p, body, a: b.approvals.get_ticket(p["ticket_id"]))
    router.add("GET", "/api/v1/agency/payments/{order_id}",
               lambda b, p, body, a: b.payments.get_order(p["order_id"]))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            path = self.path.split("?", 1)[0]
            action, params = router.match(method, path)
            if action is None:
                return self._respond(404, {"error": {"code": "not_found", "message": "路由不存在"}})

            actor = self.headers.get("X-Actor-Id", "")
            role = self.headers.get("X-Actor-Role", "")
            if not actor:
                return self._respond(401, {"error": {"code": "unauthenticated", "message": "缺少 X-Actor-Id"}})
            required_role = "hospital" if path.startswith("/api/v1/hospital/") else "agency"
            if role != required_role:
                return self._respond(403, {"error": {"code": "forbidden",
                                                     "message": f"需要 {required_role} 角色"}})

            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return self._respond(400, {"error": {"code": "bad_json", "message": "请求体不是合法 JSON"}})

            idem_key = self.headers.get("Idempotency-Key")
            try:
                if method == "GET":
                    with app.db.read_tx() as conn:
                        result = action(_Bundle(conn, app.clock), params, body, actor)
                elif path.endswith("/batch/daily-settlement"):
                    # 日结自身按日期幂等且跨多事务，凭证只缓存报告
                    result = self._idempotent_batch(
                        idem_key, path, raw,
                        lambda: _run_batch(app, body["as_of_date"], actor=actor),
                    )
                elif idem_key:
                    result = self._idempotent_tx(idem_key, path, raw, action, params, body, actor)
                else:
                    with app.db.write_tx() as conn:
                        result = action(_Bundle(conn, app.clock), params, body, actor)
                self._respond(200, result)
            except DomainError as e:
                self._respond(e.status, e.to_dict())
            except (KeyError, TypeError) as e:
                self._respond(400, {"error": {"code": "bad_request",
                                              "message": f"缺少或非法字段: {e}"}})

        def _idempotent_tx(self, key, path, raw, action, params, body, actor):
            with idem_lock:
                with app.db.write_tx() as conn:
                    row = conn.execute(
                        "SELECT response_json, request_hash FROM idempotent_requests "
                        "WHERE idem_key = ?",
                        (key,),
                    ).fetchone()
                    if row:
                        if row["request_hash"] != _request_hash(raw):
                            raise DomainError("idempotency_mismatch",
                                              "幂等键已用于不同请求体", status=409)
                        return json.loads(row["response_json"])
                    result = action(_Bundle(conn, app.clock), params, body, actor)
                    conn.execute(
                        """
                        INSERT INTO idempotent_requests
                            (idem_key, endpoint, request_hash, response_json,
                             status_code, created_at)
                        VALUES (?, ?, ?, ?, 200, ?)
                        """,
                        (key, path, _request_hash(raw),
                         json.dumps(result, ensure_ascii=False),
                         app.clock.now().isoformat(timespec="seconds")),
                    )
                    return result

        def _idempotent_batch(self, key, path, raw, runner):
            if key is None:
                return runner()
            with idem_lock:
                with app.db.write_tx() as conn:
                    row = conn.execute(
                        "SELECT response_json FROM idempotent_requests WHERE idem_key = ?",
                        (key,),
                    ).fetchone()
                    if row:
                        return json.loads(row["response_json"])
                result = runner()
                with app.db.write_tx() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO idempotent_requests
                            (idem_key, endpoint, request_hash, response_json,
                             status_code, created_at)
                        VALUES (?, ?, ?, ?, 200, ?)
                        """,
                        (key, path, _request_hash(raw),
                         json.dumps(result, ensure_ascii=False),
                         app.clock.now().isoformat(timespec="seconds")),
                    )
                return result

        def _respond(self, status: int, payload):
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _run_batch(app, as_of_date: str, *, actor: str):
    from .batch import DailySettlement

    return DailySettlement(app.db, app.clock).run(as_of_date, actor=actor)


def serve(app, host: str = "127.0.0.1", port: int = 8080):
    return ThreadingHTTPServer((host, port), make_handler(app))
