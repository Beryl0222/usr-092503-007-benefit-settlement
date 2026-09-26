"""生育津贴：与医疗费用结算关联（同案件）但可独立推进。

状态机：draft → submitted → approved/rejected；approved 后由资金模块拨付，
拨付完成置 paid。审批采用乐观并发控制：同一案件只有一份申请，
审批时校验版本号，并发审批只有一方成功。
"""

from __future__ import annotations

import json
import sqlite3

from .clock import Clock
from .contracts import AllowanceStatus, AuditAction, LedgerAction
from .engine import compute_allowance
from .errors import conflict, not_found
from .ids import new_id
from .ledger import post_entry
from .policy import PolicyRepository


class AllowanceService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock
        self.policy = PolicyRepository(conn)

    def submit(
        self,
        *,
        case_id: str,
        base_salary: int,
        leave_days: int,
        actor: str,
    ) -> dict:
        case = self._require_case(case_id)
        existing = self.conn.execute(
            "SELECT allowance_id, status FROM allowance_applications WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if existing and existing["status"] not in (
            AllowanceStatus.REJECTED.value,
        ):
            raise conflict("该案件已有在办津贴申请", "allowance_exists")

        package = self.policy.get_package(case["package_id"])
        computed = compute_allowance(
            base_salary, leave_days, package.rules.allowance_divisor
        )
        now = self.clock.now().isoformat(timespec="seconds")

        if existing:
            # 驳回后重新提交：复用记录并重置状态
            self.conn.execute(
                """
                UPDATE allowance_applications
                SET status = ?, base_salary = ?, leave_days = ?, computed_amount = ?,
                    approved_amount = NULL, submitted_by = ?, decided_by = NULL,
                    reject_reason = NULL, version = version + 1, updated_at = ?
                WHERE allowance_id = ?
                """,
                (
                    AllowanceStatus.SUBMITTED.value,
                    base_salary,
                    leave_days,
                    computed,
                    actor,
                    now,
                    existing["allowance_id"],
                ),
            )
            allowance_id = existing["allowance_id"]
        else:
            allowance_id = new_id()
            self.conn.execute(
                """
                INSERT INTO allowance_applications
                    (allowance_id, case_id, status, base_salary, leave_days,
                     computed_amount, submitted_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    allowance_id,
                    case_id,
                    AllowanceStatus.SUBMITTED.value,
                    base_salary,
                    leave_days,
                    computed,
                    actor,
                    now,
                    now,
                ),
            )
        self._sync_case_status(case_id, AllowanceStatus.SUBMITTED.value, now)
        self._audit(case_id, actor, "submit", {"allowance_id": allowance_id}, now)
        return self.get(allowance_id)

    def approve(
        self,
        *,
        allowance_id: str,
        actor: str,
        approved_amount: int | None = None,
        expected_version: int,
    ) -> dict:
        """审批通过。并发安全：版本号 + 状态双重校验，仅一个事务生效。"""

        row = self._require(allowance_id)
        if row["status"] != AllowanceStatus.SUBMITTED.value:
            raise conflict(
                f"津贴状态 {row['status']} 不允许审批", "allowance_not_submitted"
            )
        if row["submitted_by"] == actor:
            raise conflict("提交人与审批人不得为同一人", "self_approval")
        amount = approved_amount if approved_amount is not None else row["computed_amount"]
        now = self.clock.now().isoformat(timespec="seconds")
        cursor = self.conn.execute(
            """
            UPDATE allowance_applications
            SET status = ?, approved_amount = ?, decided_by = ?,
                version = version + 1, updated_at = ?
            WHERE allowance_id = ? AND version = ? AND status = ?
            """,
            (
                AllowanceStatus.APPROVED.value,
                amount,
                actor,
                now,
                allowance_id,
                expected_version,
                AllowanceStatus.SUBMITTED.value,
            ),
        )
        if cursor.rowcount != 1:
            raise conflict("并发审批冲突：该申请已被他人处理", "concurrent_approval")
        post_entry(
            self.conn,
            row["case_id"],
            LedgerAction.PAYABLE,
            amount,
            ref_type="allowance",
            ref_id=allowance_id,
            subject="allowance",
            note="生育津贴核定",
            created_at=now,
        )
        self._sync_case_status(row["case_id"], AllowanceStatus.APPROVED.value, now)
        self._audit(
            row["case_id"], actor, "approve", {"allowance_id": allowance_id, "amount": amount}, now
        )
        return self.get(allowance_id)

    def reject(
        self, *, allowance_id: str, actor: str, reason: str, expected_version: int
    ) -> dict:
        row = self._require(allowance_id)
        if row["status"] != AllowanceStatus.SUBMITTED.value:
            raise conflict(
                f"津贴状态 {row['status']} 不允许驳回", "allowance_not_submitted"
            )
        if row["submitted_by"] == actor:
            raise conflict("提交人与审批人不得为同一人", "self_approval")
        now = self.clock.now().isoformat(timespec="seconds")
        cursor = self.conn.execute(
            """
            UPDATE allowance_applications
            SET status = ?, decided_by = ?, reject_reason = ?,
                version = version + 1, updated_at = ?
            WHERE allowance_id = ? AND version = ? AND status = ?
            """,
            (
                AllowanceStatus.REJECTED.value,
                actor,
                reason,
                now,
                allowance_id,
                expected_version,
                AllowanceStatus.SUBMITTED.value,
            ),
        )
        if cursor.rowcount != 1:
            raise conflict("并发审批冲突：该申请已被他人处理", "concurrent_approval")
        self._sync_case_status(row["case_id"], AllowanceStatus.REJECTED.value, now)
        self._audit(row["case_id"], actor, "reject", {"allowance_id": allowance_id}, now)
        return self.get(allowance_id)

    def mark_paid(self, *, allowance_id: str, actor: str) -> dict:
        row = self._require(allowance_id)
        if row["status"] != AllowanceStatus.APPROVED.value:
            raise conflict("仅已核定津贴可标记拨付完成", "allowance_not_approved")
        now = self.clock.now().isoformat(timespec="seconds")
        self.conn.execute(
            """
            UPDATE allowance_applications
            SET status = ?, version = version + 1, updated_at = ?
            WHERE allowance_id = ?
            """,
            (AllowanceStatus.PAID.value, now, allowance_id),
        )
        self._sync_case_status(row["case_id"], AllowanceStatus.PAID.value, now)
        return self.get(allowance_id)

    def get(self, allowance_id: str) -> dict:
        return dict(self._require(allowance_id))

    def get_by_case(self, case_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM allowance_applications WHERE case_id = ?", (case_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ 内部

    def _require_case(self, case_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise not_found(f"案件不存在: {case_id}", "case_not_found")
        return row

    def _require(self, allowance_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM allowance_applications WHERE allowance_id = ?",
            (allowance_id,),
        ).fetchone()
        if row is None:
            raise not_found(f"津贴申请不存在: {allowance_id}", "allowance_not_found")
        return row

    def _sync_case_status(self, case_id: str, status: str, now: str) -> None:
        self.conn.execute(
            "UPDATE cases SET allowance_status = ?, updated_at = ? WHERE case_id = ?",
            (status, now, case_id),
        )

    def _audit(self, case_id, actor, verb, detail, now) -> None:
        self.conn.execute(
            """
            INSERT INTO audit_log (id, case_id, actor, action, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                case_id,
                actor,
                AuditAction.ALLOWANCE.value,
                json.dumps({"verb": verb, **detail}, ensure_ascii=False),
                now,
            ),
        )
