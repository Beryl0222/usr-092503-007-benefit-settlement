"""案件聚合：唯一业务事实的归并、费用上传/补正、异地回执与重结算。

唯一性：案件号由 (参保人, 分娩日期) 确定派生。医院重试上传按
(case_id, upload_id) 去重；经办人补正以新单据取代旧单据；异地回执按
receipt_id 去重且阶段单调（final 到达后 interim 不再改变终态）。
"""

from __future__ import annotations

import json
import sqlite3

from .clock import Clock
from .contracts import (
    AuditAction,
    BillStatus,
    CaseStatus,
    LedgerAction,
    ReceiptStage,
    ReceiptStatus,
    SettlementReason,
)
from .engine import BillLineInput, compute_settlement
from .errors import bad_request, conflict, not_found
from .ids import derive_case_id, new_id
from .ledger import post_entry
from .policy import PolicyRepository


class CaseService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock
        self.policy = PolicyRepository(conn)

    # ------------------------------------------------------------ 上传/补正

    def ingest_bill(
        self,
        *,
        hospital_id: str,
        upload_id: str,
        person_id: str,
        delivery_date: str,
        lines: list[dict],
        kind: str = "original",
        corrects_bill_id: str | None = None,
        actor: str,
    ) -> dict:
        if not lines:
            raise bad_request("费用明细不能为空", "empty_lines")
        hospital = self.policy.get_hospital(hospital_id)
        self.policy.get_person(person_id)
        now = self.clock.now().isoformat(timespec="seconds")
        case_id = derive_case_id(person_id, delivery_date)

        case = self.conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if case is None:
            case = self._create_case(
                case_id=case_id,
                person_id=person_id,
                delivery_date=delivery_date,
                hospital_region=hospital["region_code"],
                now=now,
                actor=actor,
            )
        elif hospital["region_code"] != case["delivery_region"]:
            raise conflict(
                f"就医地 {hospital['region_code']} 与案件就医地 "
                f"{case['delivery_region']} 不一致",
                "region_mismatch",
            )

        # 幂等：同一上传单号重复到达直接返回已归并结果
        existing = self.conn.execute(
            "SELECT bill_id, status FROM bills WHERE case_id = ? AND upload_id = ?",
            (case_id, upload_id),
        ).fetchone()
        if existing:
            return {
                "case_id": case_id,
                "bill_id": existing["bill_id"],
                "deduplicated": True,
            }

        if kind == "correction":
            self._supersede_corrected(case_id, corrects_bill_id)
        elif corrects_bill_id is not None:
            raise bad_request("非补正单不得携带 corrects_bill_id", "invalid_correction")

        bill_id = new_id()
        self.conn.execute(
            """
            INSERT INTO bills
                (bill_id, case_id, hospital_id, upload_id, kind,
                 corrects_bill_id, status, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bill_id,
                case_id,
                hospital_id,
                upload_id,
                kind,
                corrects_bill_id,
                BillStatus.ACTIVE.value,
                now,
            ),
        )
        for line_no, line in enumerate(lines, start=1):
            self.conn.execute(
                """
                INSERT INTO bill_lines
                    (id, bill_id, case_id, line_no, item_code, item_name,
                     amount, service_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    bill_id,
                    case_id,
                    line_no,
                    line["item_code"],
                    line["item_name"],
                    line["amount"],
                    line["service_date"],
                ),
            )

        reason = (
            SettlementReason.CORRECTION if kind == "correction" else SettlementReason.INTAKE
        )
        run_id = self.recalculate(case_id, reason=reason, note=None, actor=actor)
        self._event(case_id, "bill_ingested", {"bill_id": bill_id, "kind": kind}, actor, now)
        self._audit(
            case_id,
            actor,
            AuditAction.CORRECTION if kind == "correction" else AuditAction.INTAKE,
            {"bill_id": bill_id, "upload_id": upload_id, "lines": len(lines)},
            now,
        )
        return {
            "case_id": case_id,
            "bill_id": bill_id,
            "deduplicated": False,
            "settlement_run_id": run_id,
        }

    def _create_case(
        self, *, case_id, person_id, delivery_date, hospital_region, now, actor
    ) -> sqlite3.Row:
        enrollment = self.policy.enrollment_on(person_id, delivery_date)
        if enrollment is None:
            raise conflict(
                f"参保人 {person_id} 在分娩日 {delivery_date} 无有效参保关系",
                "not_enrolled",
            )
        enrollment_region, continuous_months = enrollment
        package = self.policy.package_for(enrollment_region, delivery_date)
        required = package.rules.enrollment_months_required
        if continuous_months < required:
            raise conflict(
                f"连续缴费 {continuous_months} 个月，不足政策要求的 {required} 个月",
                "enrollment_months_short",
            )
        cross = 1 if hospital_region != enrollment_region else 0
        self.conn.execute(
            """
            INSERT INTO cases
                (case_id, person_id, delivery_date, delivery_region,
                 enrollment_region, cross_region, package_id, status,
                 medical_status, allowance_status, needs_settlement,
                 needs_retro, receipt_final, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'none', 1, 0, 0, ?, ?)
            """,
            (
                case_id,
                person_id,
                delivery_date,
                hospital_region,
                enrollment_region,
                cross,
                package.package_id,
                CaseStatus.OPEN.value,
                now,
                now,
            ),
        )
        self._event(
            case_id,
            "case_created",
            {
                "person_id": person_id,
                "delivery_date": delivery_date,
                "delivery_region": hospital_region,
                "enrollment_region": enrollment_region,
                "package_id": package.package_id,
                "package_version": package.version,
                "cross_region": bool(cross),
            },
            actor,
            now,
        )
        return self.conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()

    def _supersede_corrected(self, case_id: str, corrects_bill_id: str | None) -> None:
        if corrects_bill_id is None:
            raise bad_request("补正单必须指明 corrects_bill_id", "missing_corrects")
        row = self.conn.execute(
            "SELECT status FROM bills WHERE bill_id = ? AND case_id = ?",
            (corrects_bill_id, case_id),
        ).fetchone()
        if row is None:
            raise not_found(f"被补正单据不存在: {corrects_bill_id}", "bill_not_found")
        if row["status"] != BillStatus.ACTIVE.value:
            raise conflict("被补正单据已失效，不能重复补正", "bill_already_superseded")
        self.conn.execute(
            "UPDATE bills SET status = ? WHERE bill_id = ?",
            (BillStatus.SUPERSEDED.value, corrects_bill_id),
        )

    # ------------------------------------------------------------ 异地回执

    def ingest_receipt(
        self,
        *,
        case_id: str,
        receipt_id: str,
        stage: str,
        confirmed_amount: int,
        actor: str,
    ) -> dict:
        self._require_case(case_id)
        now = self.clock.now().isoformat(timespec="seconds")
        existing = self.conn.execute(
            "SELECT id, stage FROM receipts WHERE case_id = ? AND receipt_id = ?",
            (case_id, receipt_id),
        ).fetchone()
        if existing:
            # 重复回执：不改变任何状态，返回已归并结果
            return {
                "case_id": case_id,
                "receipt_id": receipt_id,
                "deduplicated": True,
                "stage": existing["stage"],
            }

        status = (
            ReceiptStatus.CONFIRMED if stage == ReceiptStage.FINAL.value
            else ReceiptStatus.RECEIVED
        )
        self.conn.execute(
            """
            INSERT INTO receipts
                (id, case_id, receipt_id, stage, status, confirmed_amount, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id(), case_id, receipt_id, stage, status.value, confirmed_amount, now),
        )
        if stage == ReceiptStage.FINAL.value:
            # 终态单调：final 一旦到达即锁定；乱序的 interim 不再改变终态
            self.conn.execute(
                "UPDATE cases SET receipt_final = 1, updated_at = ? WHERE case_id = ?",
                (now, case_id),
            )
        self._event(
            case_id,
            "receipt_ingested",
            {"receipt_id": receipt_id, "stage": stage, "confirmed_amount": confirmed_amount},
            actor,
            now,
        )
        self._audit(
            case_id,
            actor,
            AuditAction.RECEIPT,
            {"receipt_id": receipt_id, "stage": stage},
            now,
        )
        return {"case_id": case_id, "receipt_id": receipt_id, "deduplicated": False}

    # ------------------------------------------------------------ 重结算

    def recalculate(
        self,
        case_id: str,
        *,
        reason: SettlementReason,
        note: str | None,
        actor: str,
        package_id: str | None = None,
    ) -> str:
        """生成新的结算版本并登记账本差额；旧版本与旧账一律保留。"""

        case = self._require_case(case_id)
        now = self.clock.now().isoformat(timespec="seconds")
        if package_id is None:
            package_id = case["package_id"]
        package = self.policy.get_package(package_id)

        lines = self.conn.execute(
            """
            SELECT bl.id, bl.item_code, bl.item_name, bl.amount,
                   bl.service_date, bl.line_no
            FROM bill_lines bl
            JOIN bills b ON b.bill_id = bl.bill_id
            WHERE bl.case_id = ? AND b.status = ?
            ORDER BY bl.line_no, bl.id
            """,
            (case_id, BillStatus.ACTIVE.value),
        ).fetchall()
        inputs = [
            BillLineInput(
                bill_line_id=r["id"],
                item_code=r["item_code"],
                item_name=r["item_name"],
                amount=r["amount"],
                service_date=r["service_date"],
                line_no=r["line_no"],
            )
            for r in lines
        ]
        lookup = self.policy.catalog_lookup(case["delivery_region"], case["delivery_date"])
        result = compute_settlement(inputs, package.rules, lookup, package_id=package_id)

        prev = self.conn.execute(
            """
            SELECT run_id, version, payable_total FROM settlement_runs
            WHERE case_id = ? ORDER BY version DESC LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        version = 1 if prev is None else prev["version"] + 1
        run_id = new_id()
        self.conn.execute(
            """
            INSERT INTO settlement_runs
                (run_id, case_id, version, reason, package_id, payable_total,
                 personal_total, status, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'current', ?, ?)
            """,
            (
                run_id,
                case_id,
                version,
                reason.value,
                package_id,
                result.payable_total,
                result.personal_total,
                note,
                now,
            ),
        )
        for line in result.lines:
            self.conn.execute(
                """
                INSERT INTO settlement_lines
                    (id, run_id, case_id, bill_line_id, item_code, category,
                     in_scope, amount, payable, personal, rule_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    run_id,
                    case_id,
                    line.bill_line_id,
                    line.item_code,
                    line.category,
                    1 if line.in_scope else 0,
                    line.amount,
                    line.payable,
                    line.personal,
                    line.rule_ref,
                ),
            )

        # 账本：首版确认应付；其后版本只登记差额，旧账不改写
        if prev is None:
            if result.payable_total > 0:
                post_entry(
                    self.conn,
                    case_id,
                    LedgerAction.PAYABLE,
                    result.payable_total,
                    ref_type="settlement_run",
                    ref_id=run_id,
                    note=f"首次结算 v{version}",
                    created_at=now,
                )
        else:
            delta = result.payable_total - prev["payable_total"]
            if delta != 0:
                post_entry(
                    self.conn,
                    case_id,
                    LedgerAction.ADJUSTMENT,
                    delta,
                    ref_type="settlement_run",
                    ref_id=run_id,
                    note=f"重结算 v{prev['version']}→v{version}（{reason.value}）差额",
                    created_at=now,
                )

        self.conn.execute(
            """
            UPDATE cases
            SET needs_settlement = 0, package_id = ?, updated_at = ?
            WHERE case_id = ?
            """,
            (package_id, now, case_id),
        )
        self._event(
            case_id,
            "settlement_recalculated",
            {
                "run_id": run_id,
                "version": version,
                "reason": reason.value,
                "payable_total": result.payable_total,
                "personal_total": result.personal_total,
            },
            actor,
            now,
        )
        self._audit(
            case_id,
            actor,
            AuditAction.SETTLEMENT,
            {"run_id": run_id, "version": version, "reason": reason.value},
            now,
        )
        return run_id

    # ------------------------------------------------------------ 查询/解释

    def get_case(self, case_id: str) -> dict:
        case = self._require_case(case_id)
        return dict(case)

    def explain(self, case_id: str) -> dict:
        """逐案可解释视图：案件、有效费用、各结算版本、账本与回执。"""

        case = self._require_case(case_id)
        bills = self.conn.execute(
            "SELECT COUNT(*) AS c FROM bills WHERE case_id = ? AND status = ?",
            (case_id, BillStatus.ACTIVE.value),
        ).fetchone()["c"]
        runs = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM settlement_runs WHERE case_id = ? ORDER BY version",
                (case_id,),
            ).fetchall()
        ]
        for run in runs:
            run["lines"] = [
                dict(l)
                for l in self.conn.execute(
                    "SELECT * FROM settlement_lines WHERE run_id = ? ORDER BY item_code",
                    (run["run_id"],),
                ).fetchall()
            ]
        from .ledger import explain_ledger

        return {
            "case": dict(case),
            "active_bill_count": bills,
            "settlement_runs": runs,
            "ledger": explain_ledger(self.conn, case_id),
            "receipts": [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM receipts WHERE case_id = ? ORDER BY received_at",
                    (case_id,),
                ).fetchall()
            ],
        }

    # ------------------------------------------------------------ 内部

    def _require_case(self, case_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise not_found(f"案件不存在: {case_id}", "case_not_found")
        return row

    def _event(self, case_id, event_type, payload, actor, now) -> None:
        self.conn.execute(
            """
            INSERT INTO case_events (id, case_id, event_type, payload_json, actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id(), case_id, event_type, json.dumps(payload, ensure_ascii=False), actor, now),
        )

    def _audit(self, case_id, actor, action: AuditAction, detail: dict, now) -> None:
        self.conn.execute(
            """
            INSERT INTO audit_log (id, case_id, actor, action, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id(), case_id, actor, action.value, json.dumps(detail, ensure_ascii=False), now),
        )
