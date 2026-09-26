"""SQLite 持久层：连接管理、schema 初始化与追加只读保护。

设计要点：
- 所有金额以最小货币单位整数存储；
- 账本、审计、幂等凭证、结算版本等表通过触发器禁止 UPDATE/DELETE，
  任何规则追溯与资金纠错都只能以新行（差额/冲正）表达；
- 写事务使用 BEGIN IMMEDIATE，避免并发写升级死锁。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    region_code TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persons (
    person_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    id_number TEXT NOT NULL
);

-- 参保关系：分段记录参保地与连续缴费月数
CREATE TABLE IF NOT EXISTS enrollment_periods (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    region_code TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,                -- NULL 表示至今有效
    continuous_months INTEGER NOT NULL DEFAULT 0,
    UNIQUE (person_id, region_code, start_date)
);

-- 政策服务包：按参保地 + 生效日期确定版本，永久保存供追溯
CREATE TABLE IF NOT EXISTS policy_packages (
    package_id TEXT PRIMARY KEY,
    region_code TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,            -- NULL 表示至今有效
    version INTEGER NOT NULL,
    rules_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (region_code, version)
);

-- 就医地目录：决定项目类别与是否在政策范围内
CREATE TABLE IF NOT EXISTS policy_catalog (
    id TEXT PRIMARY KEY,
    region_code TEXT NOT NULL,
    item_code TEXT NOT NULL,
    category TEXT NOT NULL,
    in_scope INTEGER NOT NULL,    -- 1 政策范围内 / 0 范围外
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    UNIQUE (region_code, item_code, effective_from)
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    delivery_date TEXT NOT NULL,
    delivery_region TEXT NOT NULL,     -- 就医地（统筹区）
    enrollment_region TEXT NOT NULL,   -- 参保地（待遇审核地）
    cross_region INTEGER NOT NULL,
    package_id TEXT NOT NULL,          -- 按分娩日期选定的服务包版本
    status TEXT NOT NULL,
    medical_status TEXT NOT NULL,      -- pending/settled
    allowance_status TEXT NOT NULL,    -- none/draft/submitted/approved/rejected/paid
    needs_settlement INTEGER NOT NULL DEFAULT 1,
    needs_retro INTEGER NOT NULL DEFAULT 0,
    receipt_final INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_events (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_events_case ON case_events(case_id, created_at);

CREATE TABLE IF NOT EXISTS bills (
    bill_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    hospital_id TEXT NOT NULL,
    upload_id TEXT NOT NULL,           -- 医院上传幂等键
    kind TEXT NOT NULL,                -- original/correction
    corrects_bill_id TEXT,             -- 补正指向的原单
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (case_id, upload_id)
);
CREATE INDEX IF NOT EXISTS idx_bills_case ON bills(case_id);

CREATE TABLE IF NOT EXISTS bill_lines (
    id TEXT PRIMARY KEY,
    bill_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    item_code TEXT NOT NULL,
    item_name TEXT NOT NULL,
    amount INTEGER NOT NULL,
    service_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bill_lines_case ON bill_lines(case_id);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,          -- 异地平台回执幂等键
    stage TEXT NOT NULL,               -- interim/final
    status TEXT NOT NULL,
    confirmed_amount INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL,
    UNIQUE (case_id, receipt_id)
);

CREATE TABLE IF NOT EXISTS settlement_runs (
    run_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    package_id TEXT NOT NULL,
    payable_total INTEGER NOT NULL,
    personal_total INTEGER NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (case_id, version)
);

CREATE TABLE IF NOT EXISTS settlement_lines (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    bill_line_id TEXT NOT NULL,
    item_code TEXT NOT NULL,
    category TEXT NOT NULL,
    in_scope INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    payable INTEGER NOT NULL,
    personal INTEGER NOT NULL,
    rule_ref TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlement_lines_run ON settlement_lines(run_id);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,              -- 案件内单调序号，保证同秒入账的因果顺序
    case_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT 'medical',  -- medical/allowance
    entry_type TEXT NOT NULL,          -- payable/disbursement/reversal/adjustment
    amount INTEGER NOT NULL,           -- 带符号：增加应付为正，减少为负
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    reversal_of TEXT,                  -- 冲正链：指向被冲正的账本行
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (case_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ledger_case ON ledger_entries(case_id, seq);

CREATE TABLE IF NOT EXISTS payment_orders (
    order_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    order_type TEXT NOT NULL,          -- payment/reversal
    subject TEXT NOT NULL,             -- medical/allowance
    amount INTEGER NOT NULL,
    payee_account TEXT NOT NULL,
    status TEXT NOT NULL,
    idem_key TEXT UNIQUE,
    reverses_order_id TEXT,
    ledger_entry_id TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    acked_at TEXT
);

CREATE TABLE IF NOT EXISTS allowance_applications (
    allowance_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE,      -- 一个案件一份津贴申请
    status TEXT NOT NULL,
    base_salary INTEGER NOT NULL,
    leave_days INTEGER NOT NULL,
    computed_amount INTEGER NOT NULL,
    approved_amount INTEGER,
    version INTEGER NOT NULL DEFAULT 1,
    submitted_by TEXT,
    decided_by TEXT,
    reject_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_tickets (
    ticket_id TEXT PRIMARY KEY,
    ticket_type TEXT NOT NULL,
    case_id TEXT,
    payload_json TEXT NOT NULL,
    threshold INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    approved_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    case_id TEXT PRIMARY KEY,
    account_no TEXT NOT NULL,
    account_name TEXT NOT NULL,
    bank_code TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payout_accounts (
    person_id TEXT PRIMARY KEY,
    account_no TEXT NOT NULL,
    account_name TEXT NOT NULL,
    bank_code TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotent_requests (
    idem_key TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    case_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_log(case_id, created_at);

CREATE TABLE IF NOT EXISTS batch_runs (
    batch_id TEXT PRIMARY KEY,
    as_of_date TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,              -- running/done/failed
    report_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS batch_progress (
    batch_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT,
    PRIMARY KEY (batch_id, case_id, stage)
);

CREATE TABLE IF NOT EXISTS retro_jobs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE,
    package_id TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending/done
    created_at TEXT NOT NULL,
    processed_at TEXT
);
"""

# 追加只读表：禁止 UPDATE 与 DELETE，历史不可改写
_APPEND_ONLY_TABLES = (
    "ledger_entries",
    "audit_log",
    "case_events",
    "settlement_runs",
    "settlement_lines",
    "idempotent_requests",
    "bill_lines",
    "receipts",
)

_TRIGGERS = "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
BEFORE UPDATE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} 为追加只读表，禁止 UPDATE');
END;
CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} 为追加只读表，禁止 DELETE');
END;
"""
    for table in _APPEND_ONLY_TABLES
)


class Database:
    """持有数据库路径，按线程提供连接。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def connection(self) -> sqlite3.Connection:
        """当前线程的共享连接（惰性创建）。"""

        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self.connect()
            self._local.conn = conn
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(_DDL)
            conn.executescript(_TRIGGERS)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE，提交或回滚。"""

        conn = self.connection()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def read_tx(self) -> Iterator[sqlite3.Connection]:
        yield self.connection()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
