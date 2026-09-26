"""医院及经办机构使用的 HTTP API（标准库实现，零第三方依赖）。

- 全部写接口支持 ``Idempotency-Key``：同键重放返回首次结果；同键不同载荷返回 409。
- 经办人通过 ``X-Actor`` 头标识；并发修改可携带 ``expected_version``，版本不匹配返回 409。
- 线程化服务 + ``BEGIN IMMEDIATE`` 串行写事务，支持真实并发请求。
"""

from __future__ import annotations

import hashlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .errors import DomainError
from .service import BenefitService
from .store import Store

JSON = "application/json; charset=utf-8"


def _request_hash(body: dict) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "MaternityBenefit/1.0"

    # ------------------------------------------------------------ 框架
    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", JSON)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, body: dict, status: int = 200) -> None:
        self._send(status, {"ok": True, "data": body})

    def _fail(self, status: int, code: str, message: str) -> None:
        self._send(status, {"ok": False, "error": {"code": code,
                                                   "message": message}})

    def log_message(self, fmt, *args):  # 静默标准访问日志
        if self.server.access_log:
            super().log_message(fmt, *args)

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(read=False)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch(read=True)

    def _dispatch(self, read: bool) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        svc: BenefitService = self.server.service
        try:
            body = self._read_body() if read else {}
            actor = self.headers.get("X-Actor", "anonymous")
            idem = self.headers.get("Idempotency-Key")
            version = body.pop("expected_version", None)
            result = self._route(svc, path, body, actor, idem, version)
            if result is None:
                self._fail(404, "not_found", f"无此路径: {path}")
            else:
                status, out = result
                self._ok(out, status)
        except DomainError as exc:
            self._fail(exc.http_status, exc.code, str(exc))
        except (KeyError, TypeError) as exc:
            self._fail(400, "bad_request", f"请求字段错误: {exc}")
        except Exception as exc:  # 防御：不泄漏堆栈给调用方
            self.server.exception_log.append(repr(exc))
            self._fail(500, "internal_error", str(exc))

    def _with_idem(self, scope, idem, body, fn):
        """API 级幂等：命中则重放首次响应，不同载荷冲突。"""
        store = self.server.service.store
        if not idem:
            return 200, fn()
        prior = store.get_idem(scope, idem)
        h = _request_hash(body)
        if prior is not None:
            if prior["request_hash"] != h:
                from .errors import Conflict
                raise Conflict("幂等键已用于不同的请求载荷")
            return prior["status_code"], prior["response_body"]
        status, out = 200, fn()
        store.put_idem(scope, idem, h, status, out)
        return status, out

    def _case_scope(self, cid: str) -> str:
        return f"case:{cid}"

    # ------------------------------------------------------------ 端点
    def _route(self, svc: BenefitService, path: str, body: dict,
               actor: str, idem: str | None, version):
        if path == "/v1/cases" and self.command == "POST":
            return self._with_idem(
                "POST /v1/cases", idem, body,
                lambda: svc.open_case(body, actor=actor, idem_key=idem))

        m = re.fullmatch(r"/v1/cases/([^/]+)/reports", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body, lambda: svc.report_fees(
                    cid, body["claim_id"], body["lines"], actor=actor,
                    idem_key=idem, expected_version=version))

        m = re.fullmatch(r"/v1/cases/([^/]+)/corrections", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body, lambda: svc.correct_fees(
                    cid, body["claim_id"], body["lines"], actor=actor,
                    idem_key=idem, expected_version=version))

        m = re.fullmatch(r"/v1/cases/([^/]+)/withdrawals", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body, lambda: svc.withdraw_fees(
                    cid, body["claim_id"], body["item_codes"], actor=actor,
                    idem_key=idem))

        m = re.fullmatch(r"/v1/cases/([^/]+)/remote-receipts", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body, lambda: svc.remote_receipt(
                    cid, body["claim_id"], body["receipts"], actor=actor,
                    idem_key=idem))

        m = re.fullmatch(r"/v1/cases/([^/]+)/settlements", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body, lambda: svc.settle_medical(
                    cid, actor=actor, idem_key=idem,
                    expected_version=version))

        m = re.fullmatch(r"/v1/cases/([^/]+)/allowance-decisions", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body, lambda: svc.decide_allowance(
                    cid, body["kind"], int(body["monthly_base"]),
                    actor=actor, idem_key=idem, expected_version=version))

        m = re.fullmatch(r"/v1/cases/([^/]+)/payments", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body, lambda: svc.issue_payment(
                    cid, body["kind"], actor=actor, idem_key=idem))

        m = re.fullmatch(r"/v1/payments/([^/]+)/reversal", path)
        if m and self.command == "POST":
            pid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body, lambda: svc.reverse_payment(
                    pid, actor=actor, reason=body.get("reason", ""),
                    idem_key=idem))

        m = re.fullmatch(r"/v1/cases/([^/]+)/account-change/proposals", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return 200, svc.propose_account_change(
                cid, body["account"], actor, body.get("reason", ""))

        m = re.fullmatch(r"/v1/cases/([^/]+)/retro", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return 200, svc.retro_recompute(
                cid, actor=actor, reason=body.get("reason", "规则追溯重算"))

        m = re.fullmatch(r"/v1/cases/([^/]+)/adjustments/proposals", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return 200, svc.propose_manual_adjustment(
                cid, body["kind"], int(body["amount"]),
                body.get("reason", ""), actor)

        m = re.fullmatch(r"/v1/cases/([^/]+)/adjustments", path)
        if m and self.command == "POST":
            cid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body, lambda: svc.manual_adjustment(
                    cid, body["kind"], int(body["amount"]),
                    body.get("reason", ""), actor=actor,
                    auth_ticket_id=body.get("auth_ticket_id"),
                    idem_key=idem))

        m = re.fullmatch(r"/v1/cases/([^/]+)/explain", path)
        if m and self.command == "GET":
            return 200, svc.explain_case(m.group(1))

        m = re.fullmatch(r"/v1/cases/([^/]+)", path)
        if m and self.command == "GET":
            return 200, svc.get_case(m.group(1))

        m = re.fullmatch(r"/v1/auth-tickets/([^/]+)/decision", path)
        if m and self.command == "POST":
            return 200, svc.approve_ticket(
                m.group(1), actor, bool(body["approve"]),
                body.get("reason", ""))

        m = re.fullmatch(r"/v1/auth-tickets/([^/]+)/execute", path)
        if m and self.command == "POST":
            tid = m.group(1)
            return self._with_idem(
                f"POST {path}", idem, body,
                lambda: svc.execute_account_change(tid, actor,
                                                   idem_key=idem))

        if path == "/v1/admin/enrollments" and self.command == "POST":
            body.setdefault("_actor", actor)
            svc.store.upsert_enrollment(body)
            return 201, {"enrollment_id": body["enrollment_id"]}

        if path == "/v1/admin/packages" and self.command == "POST":
            svc.store.put_package(body["package"], body["rules"], actor=actor)
            return 201, {"code": body["package"]["code"],
                         "version": body["package"]["version"]}

        if path == "/v1/admin/catalogs" and self.command == "POST":
            svc.store.put_catalog(body["catalog"], actor=actor)
            return 201, {"code": body["catalog"]["code"],
                         "version": body["catalog"]["version"]}

        if path == "/v1/audit" and self.command == "GET":
            return 200, {"entries": svc.store.audit_tail(500)}

        if path == "/healthz":
            return 200, {"status": "ok"}

        return None


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8080,
                 access_log: bool = False) -> ThreadingHTTPServer:
    store = Store(db_path)
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.daemon_threads = True
    server.service = BenefitService(store)
    server.access_log = access_log
    server.exception_log = []
    return server


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = build_server(db_path, host, port, access_log=False)
    print(f"生育待遇服务监听 http://{host}:{port}  数据库 {db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
