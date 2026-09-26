"""生育待遇应用服务：用例编排、授权闸门、资金与账本规则。

所有写用例都在单个 ``BEGIN IMMEDIATE`` 事务中完成“校验 → 追加事件”，
事件同事务投影，因此并发审批由案件版本乐观锁串行化，崩溃时事务整体回滚。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .authz import (
    DEFAULT_HIGH_ADJUSTMENT_THRESHOLD,
    adjustment_requires_dual,
    hash_payload,
    new_payment_id,
    new_ticket_id,
)
from .contracts import CostCategory, LedgerAction
from .errors import (
    AuthorizationRequired,
    Conflict,
    DomainError,
    PaymentError,
    SettlementError,
)
from .events import EventType
from .identity import case_natural_key
from .models import Enrollment, FeeLine
from .policy import (
    allowance_amount,
    allowance_eligibility,
    settle_lines,
)


def _fee_line(raw: dict) -> FeeLine:
    return FeeLine(
        claim_id=raw["claim_id"], item_code=raw["item_code"],
        category=CostCategory(raw["category"]), amount=int(raw["amount"]),
        service_date=raw["service_date"],
        remote_confirmed=bool(raw.get("remote_confirmed", False)),
        remote_accepted=(int(raw["remote_accepted"])
                         if raw.get("remote_accepted") is not None else None),
        remote_note=raw.get("remote_note", ""))


def _enrollment(raw: dict) -> Enrollment:
    return Enrollment(
        enrollment_id=raw["enrollment_id"], person_id=raw["person_id"],
        home_region=raw["home_region"], insured_from=raw["insured_from"],
        insured_to=raw["insured_to"], job_kind=raw["job_kind"])


@dataclass(slots=True)
class BenefitService:
    store: object
    high_adjustment_threshold: int = DEFAULT_HIGH_ADJUSTMENT_THRESHOLD

    # ============================================================ 案件
    def open_case(self, body: dict, actor: str = "system",
                  idem_key: str | None = None) -> dict:
        enrollment = self.store.get_enrollment(body["enrollment_id"])
        if enrollment["person_id"] != body["person_id"]:
            raise DomainError("参保关系与参保人不一致")
        case_id = case_natural_key(
            body["person_id"], body["enrollment_id"],
            body["care_region"], body["delivery_date"])
        payload = {
            "case_id": case_id,
            "person_id": body["person_id"],
            "enrollment_id": body["enrollment_id"],
            "home_region": enrollment["home_region"],
            "care_region": body["care_region"],
            "delivery_date": body["delivery_date"],
            "account": body.get("account"),
        }
        try:
            ev = self.store.append_event(
                case_id, EventType.CASE_OPENED, payload, actor,
                idempotency_key=idem_key)
        except Conflict:
            return {"case_id": case_id, "replayed": True, "seq": self.store.get_case(case_id)["version"]}
        return {"case_id": case_id, "replayed": False, "seq": ev.seq}

    def get_case(self, case_id: str) -> dict:
        return self.store.get_case(case_id)

    # ====================================================== 费用事实流
    def _append_fact(self, case_id: str, ev_type: str, payload: dict, actor: str,
                     idem_key: str | None, expected_version: int | None = None):
        with self.store.tx() as conn:
            case = self.store.tx_case(conn, case_id)
            if idem_key:
                old = self.store.tx_find_event_idem(conn, case_id, idem_key)
                if old is not None:
                    return {"case_id": case_id, "replayed": True,
                            "seq": old["seq"], "version": case["version"]}
            ev = self.store.append_event_locked(
                conn, case_id, ev_type, payload, actor,
                idempotency_key=idem_key, expected_version=expected_version)
            self.store.audit(conn, actor, ev_type, case_id, "ok",
                             {"seq": ev.seq})
            return {"case_id": case_id, "replayed": False, "seq": ev.seq,
                    "version": ev.seq}

    def report_fees(self, case_id: str, claim_id: str, lines: list[dict],
                    actor: str = "hospital", idem_key: str | None = None,
                    expected_version: int | None = None) -> dict:
        """医院上传费用；同 (claim_id,item_code) 重试天然折叠。"""
        payload = {"claim_id": claim_id, "lines": self._clean_lines(lines)}
        return self._append_fact(case_id, EventType.FEES_REPORTED, payload,
                                 actor, idem_key, expected_version)

    def correct_fees(self, case_id: str, claim_id: str, lines: list[dict],
                     actor: str = "agent", idem_key: str | None = None,
                     expected_version: int | None = None) -> dict:
        """经办人补正：覆盖同项目旧明细。"""
        payload = {"claim_id": claim_id, "lines": self._clean_lines(lines)}
        return self._append_fact(case_id, EventType.FEE_CORRECTED, payload,
                                 actor, idem_key, expected_version)

    def withdraw_fees(self, case_id: str, claim_id: str, item_codes: list[str],
                      actor: str = "agent", idem_key: str | None = None) -> dict:
        payload = {"claim_id": claim_id, "item_codes": list(item_codes)}
        return self._append_fact(case_id, EventType.FEE_WITHDRAWN, payload,
                                 actor, idem_key)

    def remote_receipt(self, case_id: str, claim_id: str, receipts: list[dict],
                       actor: str = "remote_region",
                       idem_key: str | None = None) -> dict:
        """异地回执；可早于/晚于上传、可重复乱序到达。"""
        clean = []
        for r in receipts:
            clean.append({
                "item_code": r["item_code"],
                "accepted_amount": int(r["accepted_amount"]),
                "batch_seq": int(r.get("batch_seq", 0)),
                "note": r.get("note", ""),
            })
        payload = {"claim_id": claim_id, "receipts": clean}
        return self._append_fact(case_id, EventType.REMOTE_RECEIPT, payload,
                                 actor, idem_key)

    @staticmethod
    def _clean_lines(lines: list[dict]) -> list[dict]:
        out = []
        for raw in lines:
            amount = int(raw["amount"])
            if amount < 0:
                raise DomainError("费用金额不能为负")
            out.append({
                "item_code": raw["item_code"],
                "category": CostCategory(raw["category"]).value,
                "amount": amount,
                "service_date": raw["service_date"],
            })
        return out

    # ========================================================== 账户
    def propose_account_change(self, case_id: str, account: dict,
                               proposer: str, reason: str = "") -> dict:
        self.store.get_case(case_id)  # 存在性
        self._validate_account(account)
        ticket = {
            "ticket_id": new_ticket_id(),
            "case_id": case_id,
            "action": "account_change",
            "payload_hash": hash_payload({"account": account, "reason": reason}),
            "payload": {"account": account, "reason": reason},
            "threshold": 0,
            "proposer": proposer,
            "reason": reason,
        }
        self.store.create_auth_ticket(ticket)
        return {"ticket_id": ticket["ticket_id"], "status": "proposed",
                "requires": "另一位经办人授权"}

    def approve_ticket(self, ticket_id: str, authorizer: str,
                       approve: bool, reason: str = "") -> dict:
        return self.store.decide_auth_ticket(
            ticket_id, authorizer, approve, reason)

    def execute_account_change(self, ticket_id: str, actor: str,
                               idem_key: str | None = None) -> dict:
        ticket = self.store.get_auth_ticket(ticket_id)
        if ticket["action"] != "account_change":
            raise DomainError("授权票据类型不匹配")
        body = ticket["payload"]
        account, reason = body["account"], body.get("reason", "")
        expected_hash = hash_payload({"account": account, "reason": reason})
        if ticket["payload_hash"] != expected_hash:
            raise Conflict("授权载荷与票据不符")
        with self.store.tx() as conn:
            case = self.store.tx_case(conn, ticket["case_id"])
            if idem_key:
                old = self.store.tx_find_event_idem(
                    conn, ticket["case_id"], idem_key)
                if old is not None:
                    return {"case_id": ticket["case_id"], "replayed": True,
                            "seq": old["seq"]}
            self.store.consume_auth_ticket_tx(conn, ticket_id)
            ev = self.store.append_event_locked(
                conn, ticket["case_id"], EventType.ACCOUNT_CHANGED,
                {"account": account}, actor, idempotency_key=idem_key,
                auth_ticket_id=ticket_id)
            self.store.audit(conn, actor, EventType.ACCOUNT_CHANGED,
                             ticket["case_id"], "ok",
                             {"seq": ev.seq, "ticket_id": ticket_id})
            return {"case_id": ticket["case_id"], "seq": ev.seq,
                    "account": account}

    @staticmethod
    def _validate_account(account: dict) -> None:
        required = ("account_name", "bank_code", "account_no")
        missing = [k for k in required if not str(account.get(k, "")).strip()]
        if missing:
            raise DomainError(f"收款账户缺少字段: {','.join(missing)}")

    # ====================================================== 医疗费用结算
    def _current_rules(self, case: dict):
        registry = self.store.load_registry()
        pkg = registry.package_for(case["home_region"], case["delivery_date"])
        cat = registry.catalog_for(case["care_region"], case["delivery_date"])
        return registry, pkg, cat

    def _current_package(self, case: dict):
        registry = self.store.load_registry()
        return registry.package_for(case["home_region"], case["delivery_date"])

    def settle_medical(self, case_id: str, actor: str = "agent",
                       idem_key: str | None = None,
                       expected_version: int | None = None) -> dict:
        with self.store.tx() as conn:
            case = self.store.tx_case(conn, case_id)
            if idem_key:
                old = self.store.tx_find_event_idem(conn, case_id, idem_key)
                if old is not None:
                    data = json.loads(old["payload_json"])
                    data.pop("ledger_entries", None)
                    data["replayed"] = True
                    data["seq"] = old["seq"]
                    return data
            if self.store.latest_settlement_id(conn, case_id) is not None:
                raise SettlementError(
                    "案件已有医疗结算；事实或规则变化须通过规则追溯生成差额")
            return self._settle_medical_locked(
                conn, case, actor, idem_key, expected_version,
                origin="settlement")

    def _settle_medical_locked(self, conn, case: dict, actor: str,
                               idem_key: str | None,
                               expected_version: int | None,
                               origin: str,
                               settle_id: str | None = None) -> dict:
        case_id = case["case_id"]
        fee_dicts = self.store.tx_fee_lines_dicts(conn, case_id)
        if not fee_dicts:
            raise SettlementError("案件尚无费用明细，不能结算")
        _, pkg, cat = self._current_rules(case)
        result = settle_lines(
            case_id, [_fee_line(d) for d in fee_dicts], pkg, cat)

        settle_id = settle_id or f"STL-{case_id.split('-')[1]}-{case['version'] + 1:03d}"
        ledger_entries = []
        for ln in result.lines:
            if ln.fund_payable:
                ledger_entries.append({
                    "action": LedgerAction.PAYABLE,
                    "kind": "medical_fund",
                    "amount": ln.fund_payable,
                    "origin": origin,
                    "refs": (
                        ("settle_id", settle_id),
                        ("claim_id", ln.claim_id),
                        ("item_code", ln.item_code),
                        ("resolved_category", ln.resolved_category.value),
                        ("catalog", f"{cat.code}@v{cat.version}"),
                        ("rule_id", ln.rule_id),
                    ),
                    "note": ln.reason,
                })
        payload = {
            "settle_id": settle_id,
            "origin": origin,
            "package_code": pkg.code,
            "package_version": pkg.version,
            "catalog_code": cat.code,
            "catalog_version": cat.version,
            "fund_total": result.fund_total,
            "personal_total": result.personal_total,
            "charge_total": result.charge_total,
            "by_category": result.by_category,
            "lines": result.explain(),
            "ledger_entries": ledger_entries,
        }
        ev = self.store.append_event_locked(
            conn, case_id, EventType.MEDICAL_SETTLED, payload, actor,
            idempotency_key=idem_key, expected_version=expected_version)
        self.store.audit(conn, actor, EventType.MEDICAL_SETTLED, case_id, "ok",
                         {"settle_id": settle_id, "fund_total": result.fund_total,
                          "personal_total": result.personal_total})
        payload.pop("ledger_entries")
        return payload | {"seq": ev.seq, "replayed": False}

    # ========================================================== 津贴
    def decide_allowance(self, case_id: str, kind: str, monthly_base: int,
                         actor: str = "agent", idem_key: str | None = None,
                         expected_version: int | None = None) -> dict:
        """津贴审批：与医疗结算独立推进；并发审批以版本锁互斥。"""
        monthly_base = int(monthly_base)
        if monthly_base < 0:
            raise DomainError("计发基数不能为负")
        with self.store.tx() as conn:
            case = self.store.tx_case(conn, case_id)
            if idem_key:
                old = self.store.tx_find_event_idem(conn, case_id, idem_key)
                if old is not None:
                    data = json.loads(old["payload_json"])
                    data.pop("ledger_entries", None)
                    data["replayed"] = True
                    data["seq"] = old["seq"]
                    return data
            existing = self.store.tx_allowance_decision(conn, case_id)
            if existing is not None:
                raise Conflict(
                    f"津贴已有 {existing['status']} 结论，审批为终局操作；"
                    "变更须通过人工调整等授权流程")
            enr_raw = self.store.get_enrollment(case["enrollment_id"])
            pkg = self._current_package(case)
            ok, reason = allowance_eligibility(
                _enrollment(enr_raw), pkg, case["delivery_date"], kind)
            days = pkg.allowance_days.get(kind, 0)
            amount = allowance_amount(monthly_base, days) if ok else 0
            payload = {
                "status": "approved" if ok else "rejected",
                "kind": kind,
                "days": days,
                "monthly_base": monthly_base,
                "amount": amount,
                "reason": reason,
                "package": f"{pkg.code}@v{pkg.version}",
                "ledger_entries": [],
            }
            if ok:
                payload["ledger_entries"].append({
                    "action": LedgerAction.PAYABLE,
                    "kind": "allowance",
                    "amount": amount,
                    "origin": "allowance",
                    "refs": (
                        ("package", f"{pkg.code}@v{pkg.version}"),
                        ("kind", kind),
                        ("days", str(days)),
                        ("monthly_base", str(monthly_base)),
                        ("insured_from", enr_raw["insured_from"]),
                    ),
                    "note": reason,
                })
            ev = self.store.append_event_locked(
                conn, case_id, EventType.ALLOWANCE_DECIDED, payload, actor,
                idempotency_key=idem_key, expected_version=expected_version)
            self.store.audit(conn, actor, EventType.ALLOWANCE_DECIDED,
                             case_id, payload["status"],
                             {"amount": amount, "reason": reason})
            payload.pop("ledger_entries")
            return payload | {"seq": ev.seq, "replayed": False}

    # ========================================================== 资金
    def issue_payment(self, case_id: str, kind: str, actor: str = "treasury",
                      idem_key: str | None = None,
                      run_date: str | None = None) -> dict:
        """对某类待遇的应付余额发起拨付；余额为零不能发指令。"""
        if kind not in ("medical_fund", "allowance"):
            raise DomainError("未知待遇类型")
        with self.store.tx() as conn:
            case = self.store.tx_case(conn, case_id)
            if idem_key:
                old = self.store.tx_find_event_idem(conn, case_id, idem_key)
                if old is not None:
                    data = json.loads(old["payload_json"])
                    pay0 = data["payment"]
                    le = conn.execute(
                        "SELECT entry_id FROM ledger_entries WHERE payment_id=?",
                        (pay0["payment_id"],)).fetchone()
                    return {"payment_id": pay0["payment_id"],
                            "kind": pay0["kind"], "amount": pay0["amount"],
                            "entry_id": le["entry_id"] if le else None,
                            "seq": old["seq"], "replayed": True}
            if not case.get("account"):
                raise PaymentError("案件缺少收款账户")
            balance = self.store.tx_ledger_balance(conn, case_id, kind)
            if balance <= 0:
                raise PaymentError(f"{kind} 应付余额为 {balance}，无可拨付金额")
            payment_id = new_payment_id(
                "PAY" if kind == "medical_fund" else "ALW")
            ledger_entries = [{
                "action": LedgerAction.DISBURSEMENT,
                "kind": kind,
                "amount": -balance,
                "payment_id": payment_id,
                "origin": "disbursement",
                "refs": (("run_date", run_date or ""),),
                "note": f"拨付 {kind} {balance}",
            }]
            payload = {
                "payment": {
                    "payment_id": payment_id,
                    "kind": kind,
                    "amount": balance,
                    "account": case["account"],
                    "run_date": run_date,
                },
                "ledger_entries": ledger_entries,
            }
            ev = self.store.append_event_locked(
                conn, case_id, EventType.PAYMENT_ISSUED, payload, actor,
                idempotency_key=idem_key)
            entry_id = self.store.tx_ledger_entries(conn, case_id)[-1]["entry_id"]
            self.store.audit(conn, actor, EventType.PAYMENT_ISSUED, case_id,
                             "ok", {"payment_id": payment_id, "amount": balance})
            return {"payment_id": payment_id, "kind": kind, "amount": balance,
                    "entry_id": entry_id, "seq": ev.seq, "replayed": False}

    def reverse_payment(self, payment_id: str, actor: str = "treasury",
                        reason: str = "", idem_key: str | None = None,
                        run_date: str | None = None) -> dict:
        """冲正已发出的资金指令：只能追加冲正链分录，不能删改原指令。"""
        with self.store.tx() as conn:
            pay = self.store.tx_payment(conn, payment_id)
            case_id = pay["case_id"]
            if idem_key:
                old = self.store.tx_find_event_idem(conn, case_id, idem_key)
                if old is not None:
                    data = json.loads(old["payload_json"])["reversal"]
                    data["replayed"] = True
                    data["seq"] = old["seq"]
                    return data
            if pay["status"] == "reversed":
                raise PaymentError(f"支付指令 {payment_id} 已冲正")
            entries = self.store.tx_ledger_entries(conn, case_id)
            if any(e["reverses_entry_id"] == pay["entry_id"] for e in entries):
                raise PaymentError("该指令已在冲正链上")
            reversal_id = new_payment_id("REV")
            ledger_entries = [{
                "action": LedgerAction.REVERSAL,
                "kind": pay["kind"],
                "amount": pay["amount"],  # 拨付记负数，冲正记其相反数（正数）
                "payment_id": reversal_id,
                "reverses_entry_id": pay["entry_id"],
                "origin": "reversal",
                "refs": (
                    ("reverses_payment_id", payment_id),
                    ("run_date", run_date or ""),
                    ("reason", reason),
                ),
                "note": f"冲正 {payment_id}: {reason}",
            }]
            payload = {
                "reversal": {
                    "payment_id": reversal_id,
                    "reverses_payment_id": payment_id,
                    "kind": pay["kind"],
                    "amount": abs(pay["amount"]),
                    "account": pay["account"],
                    "run_date": run_date,
                    "reason": reason,
                },
                "ledger_entries": ledger_entries,
            }
            ev = self.store.append_event_locked(
                conn, case_id, EventType.PAYMENT_REVERSED, payload, actor,
                idempotency_key=idem_key)
            self.store.audit(conn, actor, EventType.PAYMENT_REVERSED, case_id,
                             "ok", {"payment_id": payment_id,
                                    "reversal_id": reversal_id, "reason": reason})
            payload["reversal"]["seq"] = ev.seq
            payload["reversal"]["replayed"] = False
            return payload["reversal"]

    # ==================================================== 人工调整
    def propose_manual_adjustment(self, case_id: str, kind: str, amount: int,
                                  reason: str, proposer: str) -> dict:
        amount = int(amount)
        if amount == 0:
            raise DomainError("调整金额不能为零")
        if kind not in ("medical_fund", "allowance"):
            raise DomainError("未知待遇类型")
        body = {"case_id": case_id, "kind": kind, "amount": amount,
                "reason": reason}
        if not adjustment_requires_dual(amount, self.high_adjustment_threshold):
            raise AuthorizationRequired(
                "该调整未达高额阈值，可直接执行（无需双人授权票据）")
        ticket = {
            "ticket_id": new_ticket_id(),
            "case_id": case_id,
            "action": "manual_adjustment",
            "payload_hash": hash_payload(body),
            "payload": body,
            "threshold": self.high_adjustment_threshold,
            "proposer": proposer,
            "reason": reason,
        }
        self.store.create_auth_ticket(ticket)
        return {"ticket_id": ticket["ticket_id"], "status": "proposed",
                "threshold": self.high_adjustment_threshold}

    def manual_adjustment(self, case_id: str, kind: str, amount: int,
                          reason: str, actor: str = "agent",
                          auth_ticket_id: str | None = None,
                          idem_key: str | None = None) -> dict:
        """登记人工调整；高额（绝对值达阈值）必须凭已批准的双人授权票据。"""
        amount = int(amount)
        if amount == 0:
            raise DomainError("调整金额不能为零")
        ticket = None
        if adjustment_requires_dual(amount, self.high_adjustment_threshold):
            if not auth_ticket_id:
                raise AuthorizationRequired("高额人工调整须先发起双人授权")
            ticket = self.store.get_auth_ticket(auth_ticket_id)
            body = {"case_id": case_id, "kind": kind, "amount": amount,
                    "reason": reason}
            if ticket["action"] != "manual_adjustment":
                raise DomainError("授权票据类型不匹配")
            if ticket["payload_hash"] != hash_payload(body):
                raise Conflict("授权载荷与申请内容不符")
        with self.store.tx() as conn:
            self.store.tx_case(conn, case_id)
            if idem_key:
                old = self.store.tx_find_event_idem(conn, case_id, idem_key)
                if old is not None:
                    return {"case_id": case_id,
                            "amount": json.loads(old["payload_json"])["amount"],
                            "kind": json.loads(old["payload_json"])["kind"],
                            "seq": old["seq"], "replayed": True}
            if ticket is not None:
                self.store.consume_auth_ticket_tx(conn, auth_ticket_id)
            ledger_entries = [{
                "action": LedgerAction.ADJUSTMENT,
                "kind": kind,
                "amount": amount,
                "origin": "human",
                "auth_ticket_id": auth_ticket_id,
                "refs": (("reason", reason),),
                "note": f"人工调整: {reason}",
            }]
            payload = {"kind": kind, "amount": amount, "reason": reason,
                       "ledger_entries": ledger_entries}
            ev = self.store.append_event_locked(
                conn, case_id, EventType.LEDGER_ADJUSTMENT, payload, actor,
                idempotency_key=idem_key, auth_ticket_id=auth_ticket_id)
            self.store.audit(conn, actor, EventType.LEDGER_ADJUSTMENT,
                             case_id, "ok",
                             {"amount": amount, "ticket": auth_ticket_id})
            return {"case_id": case_id, "amount": amount, "kind": kind,
                    "seq": ev.seq, "replayed": False}

    # ==================================================== 规则追溯
    def retro_recompute(self, case_id: str, actor: str = "policy-admin",
                        reason: str = "规则追溯重算") -> dict:
        """按当前生效规则重算：生成新结算批次与差额分录，旧账保持不变。"""
        with self.store.tx() as conn:
            case = self.store.tx_case(conn, case_id)
            prior_rows = conn.execute(
                "SELECT * FROM medical_settlements WHERE case_id=?"
                " ORDER BY event_seq DESC LIMIT 1", (case_id,)).fetchall()
            if not prior_rows:
                raise SettlementError("案件尚无历史结算，不能追溯；请先结算")
            prior = dict(prior_rows[0])
            fee_dicts = self.store.tx_fee_lines_dicts(conn, case_id)
            _, pkg, cat = self._current_rules(case)
            result = settle_lines(
                case_id, [_fee_line(d) for d in fee_dicts], pkg, cat)
            delta = result.fund_total - int(prior["fund_total"])
            new_settle_id = (
                f"RETRO-{case_id.split('-')[1]}-{case['version'] + 1:03d}")

            ledger_entries = []
            for ln in result.lines:
                if ln.fund_payable:
                    ledger_entries.append({
                        "action": LedgerAction.PAYABLE,
                        "kind": "medical_fund",
                        "amount": ln.fund_payable,
                        "origin": "retro_recompute_full",
                        "refs": (
                            ("settle_id", new_settle_id),
                            ("claim_id", ln.claim_id),
                            ("item_code", ln.item_code),
                            ("resolved_category", ln.resolved_category.value),
                            ("catalog", f"{cat.code}@v{cat.version}"),
                            ("rule_id", ln.rule_id),
                        ),
                        "note": "[追溯重算] " + ln.reason,
                    })
            # 无论差额正负（含为 0）都冲回旧批次：净额恰为 delta，
            # 保证重算事件入账后总账只变化差额，绝不让旧应付重复挂账
            ledger_entries.append({
                "action": LedgerAction.ADJUSTMENT,
                "kind": "medical_fund",
                "amount": -int(prior["fund_total"]),
                "origin": "retro",
                "refs": (
                    ("prior_settle_id", prior["settle_id"]),
                    ("new_settle_id", new_settle_id),
                    ("prior_fund_total", str(prior["fund_total"])),
                    ("new_fund_total", str(result.fund_total)),
                    ("delta", str(delta)),
                    ("package", f"{pkg.code}@v{pkg.version}"),
                    ("catalog", f"{cat.code}@v{cat.version}"),
                ),
                "note": f"追溯差额冲回旧批次，净差额 {delta}: {reason}",
            })
            payload = {
                "settle_id": new_settle_id,
                "origin": "retro",
                "package_code": pkg.code,
                "package_version": pkg.version,
                "catalog_code": cat.code,
                "catalog_version": cat.version,
                "fund_total": result.fund_total,
                "personal_total": result.personal_total,
                "charge_total": result.charge_total,
                "by_category": result.by_category,
                "lines": result.explain(),
                "prior_settle_id": prior["settle_id"],
                "delta_fund": delta,
                "reason": reason,
                "ledger_entries": ledger_entries,
            }
            ev = self.store.append_event_locked(
                conn, case_id, EventType.RETRO_DELTA, payload, actor)
            self.store.audit(conn, actor, EventType.RETRO_DELTA, case_id,
                             "ok" if delta == 0 else "delta",
                             {"prior": prior["settle_id"],
                              "new": new_settle_id, "delta": delta})
            payload.pop("ledger_entries")
            return payload | {"seq": ev.seq}

    # ========================================================== 日结
    def daily_close(self, run_date: str, actor: str = "batch") -> dict:
        """确定性批量日结：结算未结案件并拨付全部正余额。

        - 进程级文件锁保证同一跑批日只有一个运行者；
        - 按案件号排序、逐案 claim；崩溃重跑时恢复未完成案件；
        - 各步骤使用确定性幂等键，已完成的步骤自动重放；
        - 结算后再统一扫描正余额，规则追溯产生的差额在后续日结拨付。
        """
        lock_fh = self._acquire_batch_lock(run_date)
        try:
            return self._daily_close_locked(run_date, actor)
        finally:
            self._release_batch_lock(lock_fh)

    def _acquire_batch_lock(self, run_date: str):
        if self.store.path == ":memory:":
            return None
        import fcntl
        fh = open(self.store.path + f".settle-{run_date}.lock", "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise Conflict(f"日结 {run_date} 已在另一进程运行") from exc
        return fh

    @staticmethod
    def _release_batch_lock(fh) -> None:
        if fh is None:
            return
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

    def _daily_close_locked(self, run_date: str, actor: str) -> dict:
        created = self.store.begin_batch(run_date)
        # 恢复：补上崩溃前已 claim 未 finish 的案件
        candidates = self.store.unsettled_cases(run_date)
        pending = self.store.pending_batch_cases(run_date)
        queue = sorted(set(candidates) | set(pending))

        processed, paid, skipped, failed = [], [], [], []
        for case_id in queue:
            if not self.store.claim_batch_case(run_date, case_id):
                # 已登记：只处理仍是 claimed（崩溃恢复）的；done 的跳过
                status = self._batch_case_status(run_date, case_id)
                if status != "claimed":
                    skipped.append(case_id)
                    continue
            detail = {}
            try:
                self._daily_process_case(run_date, case_id, actor, detail)
                self.store.finish_batch_case(run_date, case_id, "done",
                                             json.dumps(detail, ensure_ascii=False))
                processed.append(case_id)
                if detail.get("payment"):
                    paid.append(detail["payment"])
            except DomainError as exc:
                self.store.finish_batch_case(run_date, case_id, "failed", str(exc))
                failed.append({"case_id": case_id, "error": str(exc)})

        # 余额扫描：拨付所有案件的正余额（含追溯差额、已审批津贴）
        for case_id in self.store.all_case_ids():
            for kind in ("medical_fund", "allowance"):
                idem = f"daily:{run_date}:{case_id}:pay:{kind}"
                try:
                    res = self.issue_payment(case_id, kind, actor=actor,
                                             idem_key=idem, run_date=run_date)
                    if not res.get("replayed"):
                        paid.append(res["payment_id"])
                except DomainError:
                    pass  # 无余额/无账户等：跳过

        summary = {
            "run_date": run_date,
            "created": created,
            "settled": processed,
            "paid": paid,
            "skipped": skipped,
            "failed": failed,
            "counts": {"settled": len(processed), "paid": len(paid),
                       "skipped": len(skipped), "failed": len(failed)},
        }
        self.store.complete_batch(run_date, summary)
        return summary

    def _batch_case_status(self, run_date: str, case_id: str) -> str:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT status FROM daily_batch_cases WHERE run_date=? AND case_id=?",
                (run_date, case_id)).fetchone()
            return row["status"] if row else ""

    def _daily_process_case(self, run_date, case_id, actor, detail) -> None:
        # 1) 医疗结算（幂等：已结算会报错，由 unsettled 候选集天然排除）
        settle_idem = f"daily:{run_date}:{case_id}:medical"
        stl = self.settle_medical(case_id, actor=actor, idem_key=settle_idem)
        detail["settle"] = stl["settle_id"]
        # 2) 医疗待遇拨付（余额为 0 时跳过）
        pay_idem = f"daily:{run_date}:{case_id}:pay:medical_fund"
        try:
            pay = self.issue_payment(case_id, "medical_fund", actor=actor,
                                     idem_key=pay_idem, run_date=run_date)
            if not pay.get("replayed"):
                detail["payment"] = pay["payment_id"]
            else:
                detail["payment_replayed"] = pay["payment_id"]
        except PaymentError as exc:
            detail["payment_skipped"] = str(exc)

    # ========================================================== 解释
    def explain_case(self, case_id: str) -> dict:
        case = self.store.get_case(case_id)
        events = self.store.load_events(case_id)
        settlements = self.store.settlements(case_id)
        ledger = self.store.ledger_entries(case_id)
        payments = self.store.payments(case_id)
        with self.store.connect() as conn:
            fee_lines = self.store.tx_fee_lines_dicts(conn, case_id)
        lines_out = []
        for f in fee_lines:
            lines_out.append({
                "claim_id": f["claim_id"], "item_code": f["item_code"],
                "category": f["category"], "amount": f["amount"],
                "service_date": f["service_date"],
                "remote_confirmed": f["remote_confirmed"],
                "remote_accepted": f["remote_accepted"],
                "charge_amount": (min(f["amount"], f["remote_accepted"])
                                  if f["remote_accepted"] is not None
                                  else f["amount"]),
            })
        settled_detail = []
        for s in settlements:
            with self.store.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM settled_lines WHERE settle_id=? ORDER BY"
                    " claim_id,item_code", (s["settle_id"],)).fetchall()
            settled_detail.append({
                "settle_id": s["settle_id"], "origin": s["origin"],
                "package": f"{s['package_code']}@v{s['package_version']}",
                "catalog": f"{s['catalog_code']}@v{s['catalog_version']}",
                "fund_total": s["fund_total"],
                "personal_total": s["personal_total"],
                "charge_total": s["charge_total"],
                "lines": [dict(r) for r in rows],
            })
        balances: dict[str, int] = {}
        for e in ledger:
            balances[e["kind"]] = balances.get(e["kind"], 0) + e["amount"]
        return {
            "case": case,
            "version": case["version"],
            "fee_facts": lines_out,
            "settlements": settled_detail,
            "allowance": self.store.allowance_decision(case_id),
            "ledger": [
                {k: v for k, v in e.items() if k != "refs"} |
                {"refs": [{"name": n, "value": v} for n, v in e["refs"]]}
                for e in ledger
            ],
            "balances": balances,
            "payments": payments,
            "event_chain": [
                {"seq": e.seq, "type": e.type, "actor": e.actor,
                 "occurred_at": e.occurred_at,
                 "idempotency_key": e.idempotency_key,
                 "auth_ticket_id": e.auth_ticket_id,
                 "payload": e.payload}
                for e in events
            ],
        }
