"""追加式资金账本：任何应付、实付、冲正与差额都以新行表达。

金额符号约定（对基金应付余额而言）：
- payable      +X  确认应付
- adjustment   ±d  追溯/补正/人工产生的差额（不修改旧行）
- disbursement -X  实际拨付
- reversal     +X  冲正一笔已拨付，恢复应付；reversal_of 指向被冲正行

案件净应付余额 = Σ amount。
"""

from __future__ import annotations

import sqlite3

from .contracts import LedgerAction
from .ids import new_id


def post_entry(
    conn: sqlite3.Connection,
    case_id: str,
    entry_type: LedgerAction,
    amount: int,
    *,
    ref_type: str,
    ref_id: str,
    subject: str = "medical",
    reversal_of: str | None = None,
    note: str | None = None,
    created_at: str,
) -> str:
    entry_id = new_id()
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM ledger_entries WHERE case_id = ?",
        (case_id,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO ledger_entries
            (id, seq, case_id, subject, entry_type, amount, ref_type, ref_id,
             reversal_of, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_id,
            row["next_seq"],
            case_id,
            subject,
            entry_type.value,
            amount,
            ref_type,
            ref_id,
            reversal_of,
            note,
            created_at,
        ),
    )
    return entry_id


def case_balance(
    conn: sqlite3.Connection, case_id: str, *, subject: str = "medical"
) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS bal FROM ledger_entries
        WHERE case_id = ? AND subject = ?
        """,
        (case_id, subject),
    ).fetchone()
    return row["bal"]


def entries_for_case(
    conn: sqlite3.Connection, case_id: str, *, subject: str | None = None
) -> list[sqlite3.Row]:
    if subject is None:
        return conn.execute(
            "SELECT * FROM ledger_entries WHERE case_id = ? ORDER BY seq",
            (case_id,),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM ledger_entries
        WHERE case_id = ? AND subject = ?
        ORDER BY seq
        """,
        (case_id, subject),
    ).fetchall()


def explain_ledger(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    """逐行解释账本：每条分录给出类型、金额、来源与累计余额（按科目分列）。"""

    running: dict[str, int] = {}
    explanation = []
    for row in entries_for_case(conn, case_id):
        subject = row["subject"]
        running[subject] = running.get(subject, 0) + row["amount"]
        explanation.append(
            {
                "entry_id": row["id"],
                "subject": subject,
                "action": row["entry_type"],
                "amount": row["amount"],
                "ref_type": row["ref_type"],
                "ref_id": row["ref_id"],
                "reversal_of": row["reversal_of"],
                "note": row["note"],
                "created_at": row["created_at"],
                "running_balance": running[subject],
            }
        )
    return explanation
