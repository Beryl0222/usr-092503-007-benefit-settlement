"""双人授权（四眼原则）与收款账户管理。

- 收款账户首次登记直接生效；之后任何账户变更必须发起授权工单，
  由另一名经办人审批通过后才生效；
- 人工调整：金额达到服务包高额门槛时必须双人授权；
  低于门槛可由经办人直接登记，但仍写审计。
"""

from __future__ import annotations

import json
import sqlite3

from .clock import Clock
from .contracts import AuditAction, LedgerAction, TicketStatus, TicketType
from .errors import bad_request, conflict, forbidden, not_found
from .ids import new_id
from .ledger import post_entry
from .policy import PolicyRepository


class ApprovalService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock
        self.policy = PolicyRepository(conn)

    # ------------------------------------------------------------ 收款账户

    def register_account(
        self,
        *,
        case_id: str,
        account_no: str,
        account_name: str,
        bank_code: str,
        actor: str,
    ) -> dict:
        self._require_case(case_id)
        existing = self.conn.execute(
            "SELECT account_no FROM accounts WHERE case_id = ?", (case_id,)
        ).fetchone()
        now = self.clock.now().isoformat(timespec="seconds")
        if existing:
            # 已有账户：变更必须走双人授权
            return self.propose(
                ticket_type=TicketType.ACCOUNT_CHANGE,
                case_id=case_id,
                payload={
                    "account_no": account_no,
                    "account_name": account_name,
                    "bank_code": bank_code,
                    "previous_account_no": existing["account_no"],
                },
                amount=0,
                threshold=0,
                proposed_by=actor,
            )
        self.conn.execute(
            """
            INSERT INTO accounts (case_id, account_no, account_name, bank_code, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (case_id, account_no, account_name, bank_code, now),
        )
        self._audit(case_id, actor, {"first_register": True, "account_no": account_no}, now)
        return {"case_id": case_id, "account_no": account_no, "ticket_id": None}

    def get_account(self, case_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE case_id = ?", (case_id,)
        ).fetchone()
        return dict(row) if row else None

    def require_account(self, case_id: str) -> dict:
        account = self.get_account(case_id)
        if account is None:
            raise bad_request(f"案件 {case_id} 尚未登记收款账户", "account_missing")
        return account

    # ------------------------------------------------------------ 人工调整

    def manual_adjustment(
        self,
        *,
        case_id: str,
        amount: int,
        note: str,
        actor: str,
    ) -> dict:
        """登记人工差额。amount 带符号（正=补发，负=追回）。

        达到服务包高额门槛时改走双人授权工单，审批通过后才入账。
        """

        if amount == 0:
            raise bad_request("调整金额不能为零", "zero_adjustment")
        case = self._require_case(case_id)
        package = self.policy.get_package(case["package_id"])
        threshold = package.rules.high_adjustment_threshold
        now = self.clock.now().isoformat(timespec="seconds")

        if abs(amount) >= threshold:
            return self.propose(
                ticket_type=TicketType.MANUAL_ADJUSTMENT,
                case_id=case_id,
                payload={"amount": amount, "note": note},
                amount=abs(amount),
                threshold=threshold,
                proposed_by=actor,
            )

        entry_id = post_entry(
            self.conn,
            case_id,
            LedgerAction.ADJUSTMENT,
            amount,
            ref_type="manual_adjustment",
            ref_id=actor,
            note=f"低额人工调整：{note}",
            created_at=now,
        )
        self._audit(
            case_id,
            actor,
            {"entry_id": entry_id, "amount": amount, "high_value": False},
            now,
        )
        return {"ticket_id": None, "ledger_entry_id": entry_id}

    # ------------------------------------------------------------ 工单

    def propose(
        self,
        *,
        ticket_type: TicketType,
        case_id: str | None,
        payload: dict,
        amount: int,
        threshold: int,
        proposed_by: str,
    ) -> dict:
        now = self.clock.now().isoformat(timespec="seconds")
        ticket_id = new_id()
        self.conn.execute(
            """
            INSERT INTO approval_tickets
                (ticket_id, ticket_type, case_id, payload_json, threshold, amount,
                 status, proposed_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                ticket_type.value,
                case_id,
                json.dumps(payload, ensure_ascii=False),
                threshold,
                amount,
                TicketStatus.PENDING.value,
                proposed_by,
                now,
            ),
        )
        if case_id:
            self._audit(
                case_id,
                proposed_by,
                {"ticket_id": ticket_id, "type": ticket_type.value, "proposed": True},
                now,
            )
        return {"ticket_id": ticket_id, "status": TicketStatus.PENDING.value}

    def approve(self, *, ticket_id: str, approver: str) -> dict:
        ticket = self._require_ticket(ticket_id)
        if ticket["status"] != TicketStatus.PENDING.value:
            raise conflict(f"工单状态 {ticket['status']} 不可审批", "ticket_not_pending")
        if ticket["proposed_by"] == approver:
            raise forbidden("发起人与审批人不得为同一人", "dual_control_required")
        payload = json.loads(ticket["payload_json"])
        now = self.clock.now().isoformat(timespec="seconds")

        if ticket["ticket_type"] == TicketType.ACCOUNT_CHANGE.value:
            self.conn.execute(
                """
                UPDATE accounts
                SET account_no = ?, account_name = ?, bank_code = ?, updated_at = ?
                WHERE case_id = ?
                """,
                (
                    payload["account_no"],
                    payload["account_name"],
                    payload["bank_code"],
                    now,
                    ticket["case_id"],
                ),
            )
        elif ticket["ticket_type"] == TicketType.MANUAL_ADJUSTMENT.value:
            post_entry(
                self.conn,
                ticket["case_id"],
                LedgerAction.ADJUSTMENT,
                payload["amount"],
                ref_type="approval_ticket",
                ref_id=ticket_id,
                note=f"高额人工调整（双人授权）：{payload.get('note', '')}",
                created_at=now,
            )

        self.conn.execute(
            """
            UPDATE approval_tickets
            SET status = ?, approved_by = ?, decided_at = ?
            WHERE ticket_id = ?
            """,
            (TicketStatus.APPROVED.value, approver, now, ticket_id),
        )
        self._audit(
            ticket["case_id"],
            approver,
            {"ticket_id": ticket_id, "approved": True},
            now,
        )
        return self.get_ticket(ticket_id)

    def reject(self, *, ticket_id: str, approver: str, reason: str) -> dict:
        ticket = self._require_ticket(ticket_id)
        if ticket["status"] != TicketStatus.PENDING.value:
            raise conflict(f"工单状态 {ticket['status']} 不可审批", "ticket_not_pending")
        if ticket["proposed_by"] == approver:
            raise forbidden("发起人与审批人不得为同一人", "dual_control_required")
        now = self.clock.now().isoformat(timespec="seconds")
        self.conn.execute(
            """
            UPDATE approval_tickets
            SET status = ?, approved_by = ?, decided_at = ?
            WHERE ticket_id = ?
            """,
            (TicketStatus.REJECTED.value, approver, now, ticket_id),
        )
        self._audit(
            ticket["case_id"],
            approver,
            {"ticket_id": ticket_id, "rejected": True, "reason": reason},
            now,
        )
        return self.get_ticket(ticket_id)

    def get_ticket(self, ticket_id: str) -> dict:
        return dict(self._require_ticket(ticket_id))

    # ------------------------------------------------------------ 内部

    def _require_case(self, case_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise not_found(f"案件不存在: {case_id}", "case_not_found")
        return row

    def _require_ticket(self, ticket_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM approval_tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise not_found(f"授权工单不存在: {ticket_id}", "ticket_not_found")
        return row

    def _audit(self, case_id, actor, detail, now) -> None:
        self.conn.execute(
            """
            INSERT INTO audit_log (id, case_id, actor, action, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                case_id,
                actor,
                AuditAction.TICKET.value,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
