"""规则追溯：新服务包对历史分娩日期生效时，生成差额而非改写旧账。

追溯以作业（retro_jobs）形式排队，按案件号确定性顺序处理；
处理时调用重结算生成新版本，账本上只追加 adjustment 差额行。
"""

from __future__ import annotations

import json
import sqlite3

from .clock import Clock
from .contracts import AuditAction, SettlementReason
from .ids import new_id


class RetroService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    def schedule_for_package(
        self, *, region_code: str, package_id: str, effective_from: str,
        effective_to: str | None, actor: str,
    ) -> list[str]:
        """为受新生效区间影响的存量案件排队追溯作业。"""

        now = self.clock.now().isoformat(timespec="seconds")
        end = effective_to or "9999-12-31"
        cases = self.conn.execute(
            """
            SELECT case_id FROM cases
            WHERE enrollment_region = ?
              AND delivery_date >= ?
              AND delivery_date < ?
              AND package_id != ?
            ORDER BY case_id
            """,
            (region_code, effective_from, end, package_id),
        ).fetchall()
        scheduled = []
        for row in cases:
            exists = self.conn.execute(
                "SELECT id FROM retro_jobs WHERE case_id = ?", (row["case_id"],)
            ).fetchone()
            if exists:
                continue
            self.conn.execute(
                """
                INSERT INTO retro_jobs (id, case_id, package_id, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (new_id(), row["case_id"], package_id, now),
            )
            self.conn.execute(
                "UPDATE cases SET needs_retro = 1, updated_at = ? WHERE case_id = ?",
                (now, row["case_id"]),
            )
            scheduled.append(row["case_id"])
        if scheduled:
            self.conn.execute(
                """
                INSERT INTO audit_log (id, case_id, actor, action, detail_json, created_at)
                VALUES (?, NULL, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    actor,
                    AuditAction.RETRO.value,
                    json.dumps(
                        {"package_id": package_id, "cases": scheduled}, ensure_ascii=False
                    ),
                    now,
                ),
            )
        return scheduled

    def pending_jobs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM retro_jobs
            WHERE status = 'pending'
            ORDER BY case_id
            """
        ).fetchall()

    def process_pending(self, case_service, *, actor: str) -> list[dict]:
        """按案件号顺序处理全部待办追溯。可在崩溃后安全重跑。"""

        results = []
        now = self.clock.now().isoformat(timespec="seconds")
        for job in self.pending_jobs():
            run_id = case_service.recalculate(
                job["case_id"],
                reason=SettlementReason.RETRO,
                note=f"规则追溯切换至服务包 {job['package_id']}",
                actor=actor,
                package_id=job["package_id"],
            )
            self.conn.execute(
                """
                UPDATE retro_jobs
                SET status = 'done', processed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, job["id"]),
            )
            self.conn.execute(
                "UPDATE cases SET needs_retro = 0, updated_at = ? WHERE case_id = ?",
                (now, job["case_id"]),
            )
            results.append({"case_id": job["case_id"], "run_id": run_id})
        return results
