"""确定性批量日结：分阶段、可恢复、可复算。

阶段（顺序固定）：
1. retro     处理全部待办规则追溯（按案件号排序）；
2. finalize  异地终态回执已到的案件，医疗结算置 settled 锁定；
3. disburse  已核定且登记账户的医疗/津贴应付生成并发出资金指令。

特性：
- batch_id 由结账日确定派生，同日重跑幂等；
- 每个 (案件, 阶段) 的进度持久化，进程崩溃后重跑只处理未完成部分；
- 资金指令使用确定性幂等键，重跑绝不重复拨付；
- 报告 JSON 按案件号排序，任意机器、任意次数结果一致。
"""

from __future__ import annotations

import json

from .app import _Bundle
from .clock import Clock
from .contracts import (
    AllowanceStatus,
    PaymentOrderStatus,
    SettlementReason,
)


class DailySettlement:
    def __init__(self, db, clock: Clock):
        self.db = db
        self.clock = clock

    def run(self, as_of_date: str, *, actor: str = "batch") -> dict:
        batch_id = f"BATCH-{as_of_date}"
        now = self.clock.now().isoformat(timespec="seconds")
        with self.db.write_tx() as conn:
            row = conn.execute(
                "SELECT status FROM batch_runs WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO batch_runs
                        (batch_id, as_of_date, stage, status, started_at)
                    VALUES (?, ?, 'retro', 'running', ?)
                    """,
                    (batch_id, as_of_date, now),
                )
                conn.commit()
            elif row["status"] == "done":
                # 已完成的日结：直接返回已持久化报告（确定性复算）
                return json.loads(
                    conn.execute(
                        "SELECT report_json FROM batch_runs WHERE batch_id = ?",
                        (batch_id,),
                    ).fetchone()["report_json"]
                )

        report = {"batch_id": batch_id, "as_of_date": as_of_date, "stages": {}}
        report["stages"]["retro"] = self._stage_retro(batch_id, actor)
        report["stages"]["finalize"] = self._stage_finalize(batch_id)
        report["stages"]["disburse"] = self._stage_disburse(batch_id, actor)

        totals = {
            "retro_processed": len(report["stages"]["retro"]),
            "finalized": len(report["stages"]["finalize"]),
            "orders_sent": len(report["stages"]["disburse"]),
            "amount_sent": sum(o["amount"] for o in report["stages"]["disburse"]),
        }
        report["totals"] = totals
        with self.db.write_tx() as conn:
            conn.execute(
                """
                UPDATE batch_runs
                SET status = 'done', stage = 'disburse', report_json = ?, finished_at = ?
                WHERE batch_id = ?
                """,
                (json.dumps(report, ensure_ascii=False, sort_keys=True),
                 self.clock.now().isoformat(timespec="seconds"), batch_id),
            )
        return report

    # ------------------------------------------------------------ 阶段

    def _stage_retro(self, batch_id: str, actor: str) -> list[dict]:
        with self.db.write_tx() as conn:
            bundle = _Bundle(conn, self.clock)
            jobs = bundle.retro.pending_jobs()
        done = []
        for job in jobs:
            if self._progress_done(batch_id, job["case_id"], "retro"):
                done.append({"case_id": job["case_id"], "resumed": True})
                continue
            with self.db.write_tx() as conn:
                bundle = _Bundle(conn, self.clock)
                run_id = bundle.cases.recalculate(
                    job["case_id"],
                    reason=SettlementReason.RETRO,
                    note=f"规则追溯切换至服务包 {job['package_id']}",
                    actor=actor,
                    package_id=job["package_id"],
                )
                now = self.clock.now().isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE retro_jobs SET status = 'done', processed_at = ? WHERE id = ?",
                    (now, job["id"]),
                )
                conn.execute(
                    "UPDATE cases SET needs_retro = 0, updated_at = ? WHERE case_id = ?",
                    (now, job["case_id"]),
                )
                self._mark_progress(
                    conn, batch_id, job["case_id"], "retro",
                    {"run_id": run_id},
                )
            done.append({"case_id": job["case_id"], "run_id": run_id})
        return sorted(done, key=lambda d: d["case_id"])

    def _stage_finalize(self, batch_id: str) -> list[dict]:
        with self.db.write_tx() as conn:
            rows = conn.execute(
                """
                SELECT case_id FROM cases
                WHERE (receipt_final = 1 OR cross_region = 0)
                  AND medical_status != 'settled'
                ORDER BY case_id
                """
            ).fetchall()
        finalized = []
        now = self.clock.now().isoformat(timespec="seconds")
        for row in rows:
            if self._progress_done(batch_id, row["case_id"], "finalize"):
                finalized.append({"case_id": row["case_id"], "resumed": True})
                continue
            with self.db.write_tx() as conn:
                conn.execute(
                    "UPDATE cases SET medical_status = 'settled', updated_at = ? "
                    "WHERE case_id = ? "
                    "AND (receipt_final = 1 OR cross_region = 0)",
                    (now, row["case_id"]),
                )
                self._mark_progress(conn, batch_id, row["case_id"], "finalize", {})
            finalized.append({"case_id": row["case_id"]})
        return sorted(finalized, key=lambda d: d["case_id"])

    def _stage_disburse(self, batch_id: str, actor: str) -> list[dict]:
        with self.db.write_tx() as conn:
            # 已结算医疗 + 已核定津贴，且均有可拨付余额与收款账户
            cases = conn.execute(
                """
                SELECT c.case_id FROM cases c
                JOIN accounts a ON a.case_id = c.case_id
                WHERE c.medical_status = 'settled'
                   OR c.allowance_status = 'approved'
                ORDER BY c.case_id
                """
            ).fetchall()
            case_ids = [r["case_id"] for r in cases]
        orders = []
        for case_id in case_ids:
            if self._progress_done(batch_id, case_id, "disburse"):
                continue
            with self.db.write_tx() as conn:
                bundle = _Bundle(conn, self.clock)
                sent = self._disburse_case(conn, bundle, batch_id, case_id, actor)
                self._mark_progress(conn, batch_id, case_id, "disburse", {"orders": sent})
            orders.extend(sent)
        return sorted(orders, key=lambda o: (o["case_id"], o["subject"]))

    def _disburse_case(self, conn, bundle, batch_id, case_id, actor) -> list[dict]:
        account = bundle.approvals.get_account(case_id)
        if account is None:
            return []
        out = []
        # 医疗应付
        med = conn.execute(
            "SELECT medical_status FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if med["medical_status"] == "settled":
            out += self._pay_subject(
                bundle, batch_id, case_id, "medical", account["account_no"], actor
            )
        # 津贴
        allowance = bundle.allowance.get_by_case(case_id)
        if allowance and allowance["status"] == AllowanceStatus.APPROVED.value:
            paid = self._pay_subject(
                bundle, batch_id, case_id, "allowance", account["account_no"], actor
            )
            if paid:
                bundle.allowance.mark_paid(
                    allowance_id=allowance["allowance_id"], actor=actor
                )
            out += paid
        return out

    def _pay_subject(self, bundle, batch_id, case_id, subject, account_no, actor):
        from .ledger import case_balance

        balance = case_balance(bundle.payments.conn, case_id, subject=subject)
        if balance <= 0:
            return []
        idem_key = f"{batch_id}:{case_id}:{subject}"
        existing = bundle.payments.conn.execute(
            "SELECT * FROM payment_orders WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        if existing is not None:
            if existing["status"] == PaymentOrderStatus.PENDING.value:
                bundle.payments.send_order(existing["order_id"], actor=actor)
            return [{
                "case_id": case_id,
                "subject": subject,
                "amount": existing["amount"],
                "order_id": existing["order_id"],
                "resumed": True,
            }]
        order = bundle.payments.create_order(
            case_id=case_id,
            subject=subject,
            amount=balance,
            payee_account=account_no,
            idem_key=idem_key,
            actor=actor,
        )
        bundle.payments.send_order(order["order_id"], actor=actor)
        return [{
            "case_id": case_id,
            "subject": subject,
            "amount": balance,
            "order_id": order["order_id"],
        }]

    # ------------------------------------------------------------ 进度

    def _progress_done(self, batch_id, case_id, stage) -> bool:
        with self.db.write_tx() as conn:
            row = conn.execute(
                """
                SELECT status FROM batch_progress
                WHERE batch_id = ? AND case_id = ? AND stage = ?
                """,
                (batch_id, case_id, stage),
            ).fetchone()
        return row is not None and row["status"] == "done"

    def _mark_progress(self, conn, batch_id, case_id, stage, detail) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO batch_progress
                (batch_id, case_id, stage, status, detail_json)
            VALUES (?, ?, ?, 'done', ?)
            """,
            (batch_id, case_id, stage, json.dumps(detail, ensure_ascii=False)),
        )
