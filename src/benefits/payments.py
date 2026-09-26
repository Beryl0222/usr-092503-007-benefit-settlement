"""资金指令与冲正链：指令发出后不可改写，纠错只能经冲正。

- 指令状态机：pending → sent → acked；失败置 failed；
  已发出指令不允许作废，只能生成 reversal 指令并在账本上记 reversal 分录，
  reversal_of 指向被冲正分录，形成可回溯的冲正链。
- 拨付分录记负数（减少基金应付余额），冲正分录记正数（恢复应付）。
"""

from __future__ import annotations

import json
import sqlite3

from .clock import Clock
from .contracts import AuditAction, LedgerAction, PaymentOrderStatus, PaymentOrderType
from .errors import bad_request, conflict, not_found
from .ids import new_id
from .ledger import case_balance, post_entry


class PaymentService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    # ------------------------------------------------------------ 指令生命周期

    def create_order(
        self,
        *,
        case_id: str,
        subject: str,
        amount: int,
        payee_account: str,
        idem_key: str | None,
        actor: str,
    ) -> dict:
        if amount <= 0:
            raise bad_request("拨付金额必须为正", "invalid_amount")
        if idem_key is not None:
            existing = self.conn.execute(
                "SELECT order_id FROM payment_orders WHERE idem_key = ?", (idem_key,)
            ).fetchone()
            if existing:
                return self.get_order(existing["order_id"]) | {"deduplicated": True}
        balance = case_balance(self.conn, case_id, subject=subject)
        if amount > balance:
            raise conflict(
                f"可拨付余额 {balance} 小于请求金额 {amount}", "insufficient_balance"
            )
        now = self.clock.now().isoformat(timespec="seconds")
        order_id = new_id()
        self.conn.execute(
            """
            INSERT INTO payment_orders
                (order_id, case_id, order_type, subject, amount, payee_account,
                 status, idem_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                case_id,
                PaymentOrderType.PAYMENT.value,
                subject,
                amount,
                payee_account,
                PaymentOrderStatus.PENDING.value,
                idem_key,
                now,
            ),
        )
        self._audit(case_id, actor, {"order_id": order_id, "amount": amount}, now)
        return self.get_order(order_id)

    def send_order(self, order_id: str, *, actor: str) -> dict:
        """发出指令：登记拨付分录并置为 sent。发出后不可修改。"""

        order = self._require_order(order_id)
        if order["status"] != PaymentOrderStatus.PENDING.value:
            raise conflict(f"指令状态 {order['status']} 不允许发出", "order_not_pending")
        now = self.clock.now().isoformat(timespec="seconds")
        if order["order_type"] == PaymentOrderType.PAYMENT.value:
            entry_id = post_entry(
                self.conn,
                order["case_id"],
                LedgerAction.DISBURSEMENT,
                -order["amount"],
                ref_type="payment_order",
                ref_id=order_id,
                subject=order["subject"],
                note="资金拨付",
                created_at=now,
            )
        else:
            # 冲正指令：恢复被冲正分录的应付
            original = self._require_order(order["reverses_order_id"])
            original_entry = self.conn.execute(
                """
                SELECT id FROM ledger_entries
                WHERE ref_type = 'payment_order' AND ref_id = ?
                  AND entry_type = 'disbursement'
                """,
                (original["order_id"],),
            ).fetchone()
            entry_id = post_entry(
                self.conn,
                order["case_id"],
                LedgerAction.REVERSAL,
                order["amount"],
                ref_type="payment_order",
                ref_id=order_id,
                subject=order["subject"],
                reversal_of=original_entry["id"] if original_entry else None,
                note=f"冲正指令 {original['order_id']}",
                created_at=now,
            )
            self.conn.execute(
                "UPDATE payment_orders SET status = ? WHERE order_id = ?",
                (PaymentOrderStatus.REVERSED.value, original["order_id"]),
            )
        self.conn.execute(
            """
            UPDATE payment_orders
            SET status = ?, sent_at = ?, ledger_entry_id = ?
            WHERE order_id = ?
            """,
            (PaymentOrderStatus.SENT.value, now, entry_id, order_id),
        )
        self._audit(order["case_id"], actor, {"order_id": order_id, "sent": True}, now)
        return self.get_order(order_id)

    def ack_order(self, order_id: str, *, actor: str) -> dict:
        order = self._require_order(order_id)
        if order["status"] != PaymentOrderStatus.SENT.value:
            raise conflict(f"指令状态 {order['status']} 不允许确认", "order_not_sent")
        now = self.clock.now().isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE payment_orders SET status = ?, acked_at = ? WHERE order_id = ?",
            (PaymentOrderStatus.ACKED.value, now, order_id),
        )
        return self.get_order(order_id)

    def fail_order(self, order_id: str, *, reason: str, actor: str) -> dict:
        order = self._require_order(order_id)
        if order["status"] != PaymentOrderStatus.PENDING.value:
            raise conflict("只有未发出的指令可以标记失败", "order_not_pending")
        self.conn.execute(
            "UPDATE payment_orders SET status = ?, failure_reason = ? WHERE order_id = ?",
            (PaymentOrderStatus.FAILED.value, reason, order_id),
        )
        return self.get_order(order_id)

    # ------------------------------------------------------------ 冲正链

    def reverse_order(
        self, order_id: str, *, reason: str, actor: str
    ) -> dict:
        """对已发出指令生成冲正指令。原指令保持历史，不删除不改写。"""

        original = self._require_order(order_id)
        if original["order_type"] != PaymentOrderType.PAYMENT.value:
            raise bad_request("冲正指令不能再被冲正", "cannot_reverse_reversal")
        if original["status"] not in (
            PaymentOrderStatus.SENT.value,
            PaymentOrderStatus.ACKED.value,
        ):
            raise conflict(
                f"指令状态 {original['status']} 不允许冲正", "order_not_reversible"
            )
        existing = self.conn.execute(
            """
            SELECT order_id FROM payment_orders
            WHERE reverses_order_id = ? AND status != ?
            """,
            (order_id, PaymentOrderStatus.FAILED.value),
        ).fetchone()
        if existing:
            return self.get_order(existing["order_id"]) | {"deduplicated": True}

        now = self.clock.now().isoformat(timespec="seconds")
        reversal_id = new_id()
        self.conn.execute(
            """
            INSERT INTO payment_orders
                (order_id, case_id, order_type, subject, amount, payee_account,
                 status, idem_key, reverses_order_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                reversal_id,
                original["case_id"],
                PaymentOrderType.REVERSAL.value,
                original["subject"],
                original["amount"],
                original["payee_account"],
                PaymentOrderStatus.PENDING.value,
                order_id,
                now,
            ),
        )
        self._audit(
            original["case_id"],
            actor,
            {"reversal_order_id": reversal_id, "reverses": order_id, "reason": reason},
            now,
        )
        return self.get_order(reversal_id)

    # ------------------------------------------------------------ 查询

    def get_order(self, order_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM payment_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise not_found(f"资金指令不存在: {order_id}", "order_not_found")
        return dict(row)

    def orders_for_case(self, case_id: str) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM payment_orders WHERE case_id = ? ORDER BY created_at",
                (case_id,),
            ).fetchall()
        ]

    def _require_order(self, order_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM payment_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise not_found(f"资金指令不存在: {order_id}", "order_not_found")
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
                AuditAction.PAYMENT.value,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
