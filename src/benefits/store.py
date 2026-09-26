"""SQLite 持久化层。

设计要点：
- ``events`` 事件日志是唯一业务真相：只追加、不修改；案件事实、账本、支付都从事件投影。
- 投影表（账本、费用物化、结算结果）与事件在**同一事务**内更新；
  ``rebuild`` 可清空投影并重放事件，用于进程恢复与一致性校验。
- 所有写事务使用 ``BEGIN IMMEDIATE`` 串行化；案件版本乐观锁
  （UNIQUE(case_id, seq)）防止并发审批互相覆盖。
- 有效期规则、API 幂等凭证、双人授权票据、审计记录均持久保存。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .contracts import CostCategory
from .errors import Conflict, DuplicateIdempotency, NotFound
from .events import Event, EventType
from .ledger import utcnow

SCHEMA_VERSION = 1


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(_j(payload).encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    enrollment_id TEXT NOT NULL,
    home_region TEXT NOT NULL,
    care_region TEXT NOT NULL,
    delivery_date TEXT NOT NULL,
    account_json TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (person_id, enrollment_id, care_region, delivery_date)
);

CREATE TABLE IF NOT EXISTS enrollments (
    enrollment_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    home_region TEXT NOT NULL,
    insured_from TEXT NOT NULL,
    insured_to TEXT,
    job_kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_packages (
    code TEXT NOT NULL,
    region TEXT NOT NULL,
    version INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    include_flexible INTEGER NOT NULL,
    min_insured_months INTEGER NOT NULL,
    allowance_days_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (code, region, version)
);

CREATE TABLE IF NOT EXISTS package_category_rules (
    code TEXT NOT NULL,
    region TEXT NOT NULL,
    version INTEGER NOT NULL,
    category TEXT NOT NULL,
    mode TEXT NOT NULL,
    cap INTEGER,
    ratio_num INTEGER NOT NULL,
    ratio_denom INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (code, region, version, category)
);

CREATE TABLE IF NOT EXISTS item_catalogs (
    code TEXT NOT NULL,
    region TEXT NOT NULL,
    version INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    published_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (code, region, version)
);

CREATE TABLE IF NOT EXISTS catalog_items (
    code TEXT NOT NULL,
    region TEXT NOT NULL,
    version INTEGER NOT NULL,
    item_code TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (code, region, version, item_code)
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT,
    auth_ticket_id TEXT,
    UNIQUE (case_id, seq),
    UNIQUE (case_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_events_case ON events (case_id, seq);

-- 物化投影：费用上传事实
CREATE TABLE IF NOT EXISTS fee_facts (
    case_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    item_code TEXT NOT NULL,
    category TEXT NOT NULL,
    amount INTEGER NOT NULL,
    service_date TEXT NOT NULL,
    origin TEXT NOT NULL,
    updated_seq INTEGER NOT NULL,
    PRIMARY KEY (case_id, claim_id, item_code)
);

-- 物化投影：异地回执（乱序安全）
CREATE TABLE IF NOT EXISTS receipts (
    case_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    item_code TEXT NOT NULL,
    accepted_amount INTEGER NOT NULL,
    batch_seq INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_seq INTEGER NOT NULL,
    PRIMARY KEY (case_id, claim_id, item_code)
);

-- 医疗费用结算（每次结算一批；追溯不覆盖旧批次）
CREATE TABLE IF NOT EXISTS medical_settlements (
    settle_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    package_code TEXT NOT NULL,
    package_version INTEGER NOT NULL,
    catalog_code TEXT NOT NULL,
    catalog_version INTEGER NOT NULL,
    fund_total INTEGER NOT NULL,
    personal_total INTEGER NOT NULL,
    charge_total INTEGER NOT NULL,
    origin TEXT NOT NULL DEFAULT 'settlement',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settle_case ON medical_settlements (case_id);

CREATE TABLE IF NOT EXISTS settled_lines (
    settle_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    item_code TEXT NOT NULL,
    claimed_category TEXT NOT NULL,
    resolved_category TEXT NOT NULL,
    in_scope INTEGER NOT NULL,
    charge_amount INTEGER NOT NULL,
    fund_payable INTEGER NOT NULL,
    personal_payable INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (settle_id, claim_id, item_code)
);

-- 津贴审批
CREATE TABLE IF NOT EXISTS allowance_decisions (
    case_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    days INTEGER NOT NULL,
    monthly_base INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

-- 账本投影（追加式；可从事件重建）
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    action TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    payment_id TEXT,
    reverses_entry_id TEXT,
    origin TEXT NOT NULL,
    refs_json TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    auth_ticket_id TEXT,
    PRIMARY KEY (case_id, seq),
    UNIQUE (entry_id)
);
CREATE INDEX IF NOT EXISTS idx_ledger_case ON ledger_entries (case_id, seq);
CREATE INDEX IF NOT EXISTS idx_ledger_payment ON ledger_entries (payment_id);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    account_json TEXT NOT NULL,
    status TEXT NOT NULL,              -- issued / reversed
    entry_id TEXT NOT NULL,
    reversal_of TEXT,
    run_date TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pay_case ON payments (case_id);

-- 双人授权票据
CREATE TABLE IF NOT EXISTS auth_tickets (
    ticket_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    threshold INTEGER NOT NULL,
    proposer TEXT NOT NULL,
    authorizer TEXT,
    status TEXT NOT NULL,              -- proposed / approved / rejected / consumed
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

-- HTTP API 幂等凭证
CREATE TABLE IF NOT EXISTS api_idempotency (
    scope TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, idem_key)
);

-- 日结批次（确定性、可恢复）
CREATE TABLE IF NOT EXISTS daily_batches (
    run_date TEXT PRIMARY KEY,
    status TEXT NOT NULL,              -- running / completed
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS daily_batch_cases (
    run_date TEXT NOT NULL,
    case_id TEXT NOT NULL,
    status TEXT NOT NULL,              -- claimed / done / skipped / failed
    detail TEXT NOT NULL DEFAULT '',
    claimed_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (run_date, case_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    case_id TEXT,
    result TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""

PROJECTION_TABLES = [
    "fee_facts", "receipts", "medical_settlements", "settled_lines",
    "allowance_decisions", "ledger_entries", "payments", "cases",
]


class Store:
    def __init__(self, path: str | Path, clock=utcnow):
        self.path = str(path)
        self.clock = clock
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------ 基础
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    class _Tx:
        def __init__(self, store: "Store"):
            self.store = store
            self.conn = None

        def __enter__(self) -> sqlite3.Connection:
            self.conn = self.store.connect()
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb) -> None:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
            self.conn.close()

    def tx(self) -> "Store._Tx":
        """串行化写事务；服务层在其中完成读校验与事件追加。"""
        return self._Tx(self)

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executescript(SCHEMA)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                             (str(SCHEMA_VERSION),))

    def audit(self, conn, actor: str, action: str, case_id: str | None,
              result: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(at,actor,action,case_id,result,detail_json)"
            " VALUES(?,?,?,?,?,?)",
            (self.clock(), actor, action, case_id, result, _j(detail)))

    # ------------------------------------------------------------ 主数据
    def upsert_enrollment(self, enr: dict) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO enrollments(enrollment_id,person_id,home_region,"
                "insured_from,insured_to,job_kind) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(enrollment_id) DO UPDATE SET person_id=excluded.person_id,"
                "home_region=excluded.home_region,insured_from=excluded.insured_from,"
                "insured_to=excluded.insured_to,job_kind=excluded.job_kind",
                (enr["enrollment_id"], enr["person_id"], enr["home_region"],
                 enr["insured_from"], enr.get("insured_to"),
                 enr.get("job_kind", "flexible")))
            self.audit(conn, enr.get("_actor", "system"), "enrollment_upsert",
                       None, "ok", {"enrollment_id": enr["enrollment_id"]})
            conn.execute("COMMIT")

    def get_enrollment(self, enrollment_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM enrollments WHERE enrollment_id=?",
                (enrollment_id,)).fetchone()
        if row is None:
            raise NotFound(f"参保关系不存在: {enrollment_id}")
        return dict(row)

    def put_package(self, pkg: dict, rules: list[dict], actor: str = "admin") -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO policy_packages(code,region,version,effective_from,"
                    "effective_to,include_flexible,min_insured_months,"
                    "allowance_days_json,published_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (pkg["code"], pkg["region"], int(pkg["version"]),
                     pkg["effective_from"], pkg.get("effective_to"),
                     1 if pkg["include_flexible"] else 0,
                     int(pkg["min_insured_months"]),
                     _j(pkg["allowance_days"]), pkg["published_at"]))
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise Conflict(
                    f"服务包版本已存在: {pkg['code']} {pkg['region']} v{pkg['version']}"
                ) from exc
            for r in rules:
                conn.execute(
                    "INSERT INTO package_category_rules(code,region,version,"
                    "category,mode,cap,ratio_num,ratio_denom,note)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (pkg["code"], pkg["region"], int(pkg["version"]),
                     r["category"], r["mode"], r.get("cap"),
                     int(r.get("ratio_num", 0)), int(r.get("ratio_denom", 1)),
                     r.get("note", "")))
            self.audit(conn, actor, "package_put", None, "ok",
                       {"code": pkg["code"], "version": pkg["version"]})
            conn.execute("COMMIT")

    def put_catalog(self, cat: dict, actor: str = "admin") -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO item_catalogs(code,region,version,effective_from,"
                    "effective_to,published_at) VALUES(?,?,?,?,?,?)",
                    (cat["code"], cat["region"], int(cat["version"]),
                     cat["effective_from"], cat.get("effective_to"),
                     cat.get("published_at", "")))
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise Conflict(
                    f"目录版本已存在: {cat['code']} {cat['region']} v{cat['version']}"
                ) from exc
            for item_code, category in cat["items"].items():
                conn.execute(
                    "INSERT INTO catalog_items(code,region,version,item_code,category)"
                    " VALUES(?,?,?,?,?)",
                    (cat["code"], cat["region"], int(cat["version"]),
                     item_code, category))
            self.audit(conn, actor, "catalog_put", None, "ok",
                       {"code": cat["code"], "version": cat["version"]})
            conn.execute("COMMIT")

    # ------------------------------------------------------------ 事件
    def case_exists(self, conn, case_id: str) -> bool:
        return conn.execute("SELECT 1 FROM cases WHERE case_id=?",
                            (case_id,)).fetchone() is not None

    def case_row(self, conn, case_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM cases WHERE case_id=?",
                           (case_id,)).fetchone()
        if row is None:
            raise NotFound(f"案件不存在: {case_id}")
        return row

    def get_case(self, case_id: str) -> dict:
        with self.connect() as conn:
            row = self.case_row(conn, case_id)
            data = dict(row)
            data["account"] = json.loads(data["account_json"]) if data["account_json"] else None
            return data

    def append_event(self, case_id: str, ev_type: str, payload: dict, actor: str,
                     *, occurred_at: str | None = None,
                     idempotency_key: str | None = None,
                     expected_version: int | None = None,
                     auth_ticket_id: str | None = None) -> Event:
        """在一个事务中追加事件并更新全部投影。"""

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ev = self._append_event_tx(
                    conn, case_id, ev_type, payload, actor,
                    occurred_at=occurred_at, idempotency_key=idempotency_key,
                    expected_version=expected_version,
                    auth_ticket_id=auth_ticket_id)
                self.audit(conn, actor, ev_type, case_id, "ok",
                           {"seq": ev.seq, "payload": payload})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return ev

    def _append_event_tx(self, conn, case_id, ev_type, payload, actor, *,
                         occurred_at, idempotency_key, expected_version,
                         auth_ticket_id) -> Event:
        ts = occurred_at or self.clock()
        is_open = ev_type == EventType.CASE_OPENED
        if is_open:
            seq = 1
            existing = conn.execute(
                "SELECT case_id FROM cases WHERE person_id=? AND enrollment_id=?"
                " AND care_region=? AND delivery_date=?",
                (payload["person_id"], payload["enrollment_id"],
                 payload["care_region"], payload["delivery_date"])).fetchone()
            if existing is not None:
                raise Conflict(f"案件已存在: {existing['case_id']}")
            conn.execute(
                "INSERT INTO cases(case_id,person_id,enrollment_id,home_region,"
                "care_region,delivery_date,account_json,version,created_at)"
                " VALUES(?,?,?,?,?,?,?,1,?)",
                (case_id, payload["person_id"], payload["enrollment_id"],
                 payload["home_region"], payload["care_region"],
                 payload["delivery_date"],
                 _j(payload["account"]) if payload.get("account") else None,
                 ts))
        else:
            row = self.case_row(conn, case_id)
            current = row["version"]
            if expected_version is not None and expected_version != current:
                raise Conflict(
                    f"案件版本冲突：期望 {expected_version}，当前 {current}")
            seq = current + 1

        try:
            conn.execute(
                "INSERT INTO events(case_id,seq,type,payload_json,actor,"
                "occurred_at,idempotency_key,auth_ticket_id)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (case_id, seq, ev_type, _j(payload), actor,
                 ts, idempotency_key, auth_ticket_id))
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdempotency(
                f"幂等键重复: {idempotency_key}") from exc

        event = Event(
            seq=seq, type=ev_type, payload=payload, actor=actor,
            occurred_at=ts, idempotency_key=idempotency_key,
            auth_ticket_id=auth_ticket_id)
        self._project_event(conn, case_id, event)

        if not is_open:
            conn.execute("UPDATE cases SET version=? WHERE case_id=?",
                         (seq, case_id))
        return event

    # ---------------------------------------------------------- 投影
    def _project_event(self, conn, case_id: str, ev: Event) -> None:
        """把单条事件应用到全部投影表（重建时同样调用，必须幂等/确定）。"""
        p = ev.payload
        t = ev.type

        # 先记账本分录（确定性分配 seq/entry_id），供资金行反查 entry_id
        payment_entry_ids = self._insert_ledger_entries_bulk(
            conn, case_id, ev, p.get("ledger_entries", ()))

        if t == EventType.CASE_OPENED:
            conn.execute(
                "INSERT OR IGNORE INTO cases(case_id,person_id,enrollment_id,"
                "home_region,care_region,delivery_date,account_json,version,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (case_id, p["person_id"], p["enrollment_id"], p["home_region"],
                 p["care_region"], p["delivery_date"],
                 _j(p["account"]) if p.get("account") else None, ev.seq,
                 ev.occurred_at))
        elif t == EventType.FEES_REPORTED:
            for raw in p["lines"]:
                conn.execute(
                    "INSERT OR IGNORE INTO fee_facts(case_id,claim_id,item_code,"
                    "category,amount,service_date,origin,updated_seq)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (case_id, p["claim_id"], raw["item_code"], raw["category"],
                     int(raw["amount"]), raw["service_date"], "report", ev.seq))
        elif t == EventType.FEE_CORRECTED:
            for raw in p["lines"]:
                conn.execute(
                    "INSERT INTO fee_facts(case_id,claim_id,item_code,category,"
                    "amount,service_date,origin,updated_seq) VALUES(?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(case_id,claim_id,item_code) DO UPDATE SET"
                    " category=excluded.category,amount=excluded.amount,"
                    "service_date=excluded.service_date,origin='correction',"
                    "updated_seq=excluded.updated_seq",
                    (case_id, p["claim_id"], raw["item_code"], raw["category"],
                     int(raw["amount"]), raw["service_date"], "correction",
                     ev.seq))
        elif t == EventType.FEE_WITHDRAWN:
            for item_code in p["item_codes"]:
                conn.execute(
                    "DELETE FROM fee_facts WHERE case_id=? AND claim_id=? AND item_code=?",
                    (case_id, p["claim_id"], item_code))
                conn.execute(
                    "DELETE FROM receipts WHERE case_id=? AND claim_id=? AND item_code=?",
                    (case_id, p["claim_id"], item_code))
        elif t == EventType.REMOTE_RECEIPT:
            for r in p["receipts"]:
                existing = conn.execute(
                    "SELECT batch_seq FROM receipts WHERE case_id=? AND claim_id=? AND item_code=?",
                    (case_id, p["claim_id"], r["item_code"])).fetchone()
                if existing is None or int(r.get("batch_seq", 0)) >= existing["batch_seq"]:
                    conn.execute(
                        "INSERT INTO receipts(case_id,claim_id,item_code,"
                        "accepted_amount,batch_seq,note,updated_seq)"
                        " VALUES(?,?,?,?,?,?,?)"
                        " ON CONFLICT(case_id,claim_id,item_code) DO UPDATE SET"
                        " accepted_amount=excluded.accepted_amount,"
                        "batch_seq=excluded.batch_seq,note=excluded.note,"
                        "updated_seq=excluded.updated_seq",
                        (case_id, p["claim_id"], r["item_code"],
                         int(r["accepted_amount"]), int(r.get("batch_seq", 0)),
                         r.get("note", ""), ev.seq))
        elif t == EventType.ACCOUNT_CHANGED:
            conn.execute("UPDATE cases SET account_json=? WHERE case_id=?",
                         (_j(p["account"]), case_id))
        elif t == EventType.MEDICAL_SETTLED:
            conn.execute(
                "INSERT OR IGNORE INTO medical_settlements(settle_id,case_id,"
                "event_seq,package_code,package_version,catalog_code,"
                "catalog_version,fund_total,personal_total,charge_total,origin,"
                "created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["settle_id"], case_id, ev.seq, p["package_code"],
                 int(p["package_version"]), p["catalog_code"],
                 int(p["catalog_version"]), int(p["fund_total"]),
                 int(p["personal_total"]), int(p["charge_total"]),
                 p.get("origin", "settlement"), ev.occurred_at))
            for ln in p["lines"]:
                conn.execute(
                    "INSERT OR IGNORE INTO settled_lines(settle_id,claim_id,"
                    "item_code,claimed_category,resolved_category,in_scope,"
                    "charge_amount,fund_payable,personal_payable,rule_id,reason)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (p["settle_id"], ln["claim_id"], ln["item_code"],
                     ln["claimed_category"], ln["resolved_category"],
                     1 if ln["in_scope"] else 0, int(ln["charge_amount"]),
                     int(ln["fund_payable"]), int(ln["personal_payable"]),
                     ln["rule_id"], ln["reason"]))
        elif t == EventType.ALLOWANCE_DECIDED:
            conn.execute(
                "INSERT INTO allowance_decisions(case_id,event_seq,status,kind,"
                "days,monthly_base,amount,reason,decided_by,decided_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(case_id) DO UPDATE SET event_seq=excluded.event_seq,"
                "status=excluded.status,kind=excluded.kind,days=excluded.days,"
                "monthly_base=excluded.monthly_base,amount=excluded.amount,"
                "reason=excluded.reason,decided_by=excluded.decided_by,"
                "decided_at=excluded.decided_at",
                (case_id, ev.seq, p["status"], p["kind"], int(p["days"]),
                 int(p["monthly_base"]), int(p["amount"]), p["reason"],
                 ev.actor, ev.occurred_at))
        elif t == EventType.PAYMENT_ISSUED:
            pay = p["payment"]
            conn.execute(
                "INSERT OR IGNORE INTO payments(payment_id,case_id,kind,amount,"
                "account_json,status,entry_id,reversal_of,run_date,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pay["payment_id"], case_id, pay["kind"], int(pay["amount"]),
                 _j(pay["account"]), "issued",
                 payment_entry_ids[pay["payment_id"]], None,
                 pay.get("run_date"), ev.occurred_at))
        elif t == EventType.PAYMENT_REVERSED:
            rev = p["reversal"]
            conn.execute(
                "INSERT OR IGNORE INTO payments(payment_id,case_id,kind,amount,"
                "account_json,status,entry_id,reversal_of,run_date,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rev["payment_id"], case_id, rev["kind"],
                 -int(rev["amount"]), _j(rev["account"]), "issued",
                 payment_entry_ids[rev["payment_id"]],
                 rev["reverses_payment_id"], rev.get("run_date"),
                 ev.occurred_at))
            conn.execute("UPDATE payments SET status='reversed' WHERE payment_id=?",
                         (rev["reverses_payment_id"],))
        elif t == EventType.RETRO_DELTA:
            conn.execute(
                "INSERT OR IGNORE INTO medical_settlements(settle_id,case_id,"
                "event_seq,package_code,package_version,catalog_code,"
                "catalog_version,fund_total,personal_total,charge_total,origin,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["settle_id"], case_id, ev.seq, p["package_code"],
                 int(p["package_version"]), p["catalog_code"],
                 int(p["catalog_version"]), int(p["fund_total"]),
                 int(p["personal_total"]), int(p["charge_total"]),
                 p.get("origin", "retro"), ev.occurred_at))
            for ln in p["lines"]:
                conn.execute(
                    "INSERT OR IGNORE INTO settled_lines(settle_id,claim_id,"
                    "item_code,claimed_category,resolved_category,in_scope,"
                    "charge_amount,fund_payable,personal_payable,rule_id,reason)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (p["settle_id"], ln["claim_id"], ln["item_code"],
                     ln["claimed_category"], ln["resolved_category"],
                     1 if ln["in_scope"] else 0, int(ln["charge_amount"]),
                     int(ln["fund_payable"]), int(ln["personal_payable"]),
                     ln["rule_id"], ln["reason"]))
        elif t == EventType.LEDGER_ADJUSTMENT:
            pass  # 仅账本投影
        else:
            raise ValueError(f"未知事件类型: {t}")

    def _insert_ledger_entries_bulk(self, conn, case_id: str, ev: Event,
                                    entries: list | tuple) -> dict:
        """在同一事件内按顺序分配确定性账本序号；返回 payment_id -> entry_id。"""
        payment_entry_ids: dict[str, str] = {}
        for le in entries:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM ledger_entries WHERE case_id=?",
                (case_id,)).fetchone()["s"]
            entry_id = le.get("entry_id") or f"{case_id}:L{nxt:06d}"
            conn.execute(
                "INSERT OR IGNORE INTO ledger_entries(entry_id,case_id,seq,action,"
                "kind,amount,payment_id,reverses_entry_id,origin,refs_json,note,"
                "created_at,auth_ticket_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, case_id, nxt, le["action"], le["kind"], int(le["amount"]),
                 le.get("payment_id"), le.get("reverses_entry_id"), le["origin"],
                 _j([{"name": n, "value": v} for n, v in le.get("refs", ())]),
                 le.get("note", ""), le.get("created_at", ev.occurred_at),
                 le.get("auth_ticket_id", ev.auth_ticket_id)))
            if le.get("payment_id"):
                payment_entry_ids[le["payment_id"]] = entry_id
        return payment_entry_ids

    def load_events(self, case_id: str) -> list[Event]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE case_id=? ORDER BY seq",
                (case_id,)).fetchall()
        return [self._row_to_event(r) for r in rows]

    # -------------------------------------------------- 事务内读取辅助
    def append_event_locked(self, conn, case_id: str, ev_type: str, payload: dict,
                            actor: str, *, occurred_at: str | None = None,
                            idempotency_key: str | None = None,
                            expected_version: int | None = None,
                            auth_ticket_id: str | None = None) -> Event:
        """在已持有的写事务内追加事件（不自行提交）。"""
        return self._append_event_tx(
            conn, case_id, ev_type, payload, actor,
            occurred_at=occurred_at, idempotency_key=idempotency_key,
            expected_version=expected_version,
            auth_ticket_id=auth_ticket_id)

    def tx_find_event_idem(self, conn, case_id: str,
                           idem_key: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM events WHERE case_id=? AND idempotency_key=?",
            (case_id, idem_key)).fetchone()

    def tx_case(self, conn, case_id: str) -> dict:
        row = self.case_row(conn, case_id)
        data = dict(row)
        data["account"] = json.loads(data["account_json"]) if data["account_json"] else None
        return data

    def tx_fee_lines_dicts(self, conn, case_id: str) -> list[dict]:
        """物化费用事实套用最新回执（与折叠语义一致）。"""
        rows = conn.execute(
            "SELECT f.claim_id,f.item_code,f.category,f.amount,f.service_date,"
            "r.accepted_amount,r.note AS remote_note FROM fee_facts f"
            " LEFT JOIN receipts r ON r.case_id=f.case_id"
            " AND r.claim_id=f.claim_id AND r.item_code=f.item_code"
            " WHERE f.case_id=? ORDER BY f.service_date,f.claim_id,f.item_code",
            (case_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["remote_confirmed"] = d["accepted_amount"] is not None
            d["remote_accepted"] = d["accepted_amount"]
            out.append(d)
        return out

    def tx_ledger_entries(self, conn, case_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM ledger_entries WHERE case_id=? ORDER BY seq",
            (case_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["refs"] = [(x["name"], x["value"])
                         for x in json.loads(d.pop("refs_json"))]
            out.append(d)
        return out

    def tx_ledger_balance(self, conn, case_id: str, kind: str | None = None) -> int:
        if kind is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM ledger_entries WHERE case_id=?",
                (case_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM ledger_entries"
                " WHERE case_id=? AND kind=?", (case_id, kind)).fetchone()
        return row["s"]

    def tx_payment(self, conn, payment_id: str) -> dict:
        row = conn.execute("SELECT * FROM payments WHERE payment_id=?",
                           (payment_id,)).fetchone()
        if row is None:
            raise NotFound(f"支付指令不存在: {payment_id}")
        d = dict(row)
        d["account"] = json.loads(d.pop("account_json"))
        return d

    def tx_allowance_decision(self, conn, case_id: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM allowance_decisions WHERE case_id=?",
            (case_id,)).fetchone()
        return dict(row) if row else None

    def all_case_ids(self) -> list[str]:
        with self.connect() as conn:
            return [r["case_id"] for r in conn.execute(
                "SELECT case_id FROM cases ORDER BY case_id")]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            seq=row["seq"], type=row["type"],
            payload=json.loads(row["payload_json"]),
            actor=row["actor"], occurred_at=row["occurred_at"],
            idempotency_key=row["idempotency_key"],
            auth_ticket_id=row["auth_ticket_id"])

    # ---------------------------------------------------------- 规则装载
    def load_registry(self):
        from .policy import CategoryRule, ItemCatalog, PolicyPackage, PolicyRegistry
        registry = PolicyRegistry()
        with self.connect() as conn:
            for r in conn.execute("SELECT * FROM policy_packages"):
                rules_rows = conn.execute(
                    "SELECT * FROM package_category_rules WHERE code=? AND region=? AND version=?",
                    (r["code"], r["region"], r["version"])).fetchall()
                rules = {
                    CostCategory(rr["category"]): CategoryRule(
                        category=CostCategory(rr["category"]), mode=rr["mode"],
                        cap=rr["cap"], ratio_num=rr["ratio_num"],
                        ratio_denom=rr["ratio_denom"], note=rr["note"])
                    for rr in rules_rows
                }
                registry.add_package(PolicyPackage(
                    code=r["code"], version=r["version"], region=r["region"],
                    effective_from=r["effective_from"],
                    effective_to=r["effective_to"],
                    include_flexible=bool(r["include_flexible"]),
                    min_insured_months=r["min_insured_months"],
                    allowance_days=json.loads(r["allowance_days_json"]),
                    category_rules=rules, published_at=r["published_at"]))
            for r in conn.execute("SELECT * FROM item_catalogs"):
                items = {
                    ir["item_code"]: CostCategory(ir["category"])
                    for ir in conn.execute(
                        "SELECT item_code,category FROM catalog_items"
                        " WHERE code=? AND region=? AND version=?",
                        (r["code"], r["region"], r["version"]))
                }
                registry.add_catalog(ItemCatalog(
                    code=r["code"], version=r["version"], region=r["region"],
                    effective_from=r["effective_from"],
                    effective_to=r["effective_to"], items=items,
                    published_at=r["published_at"]))
        return registry

    # ---------------------------------------------------------- 账本查询
    def ledger_entries(self, case_id: str) -> list[dict]:
        with self.connect() as conn:
            self.case_row(conn, case_id)
            rows = conn.execute(
                "SELECT * FROM ledger_entries WHERE case_id=? ORDER BY seq",
                (case_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["refs"] = [(x["name"], x["value"])
                         for x in json.loads(d.pop("refs_json"))]
            out.append(d)
        return out

    def ledger_entry_by_payment(self, payment_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ledger_entries WHERE payment_id=?",
                (payment_id,)).fetchone()
        if row is None:
            raise NotFound(f"支付指令不存在: {payment_id}")
        d = dict(row)
        d["refs"] = [(x["name"], x["value"]) for x in json.loads(d.pop("refs_json"))]
        return d

    def latest_settlement_id(self, conn, case_id: str) -> str | None:
        row = conn.execute(
            "SELECT settle_id FROM medical_settlements WHERE case_id=?"
            " ORDER BY event_seq DESC LIMIT 1", (case_id,)).fetchone()
        return row["settle_id"] if row else None

    def settlements(self, case_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM medical_settlements WHERE case_id=? ORDER BY event_seq",
                (case_id,)).fetchall()
            return [dict(r) for r in rows]

    def allowance_decision(self, case_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM allowance_decisions WHERE case_id=?",
                (case_id,)).fetchone()
            return dict(row) if row else None

    def payment(self, payment_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM payments WHERE payment_id=?",
                               (payment_id,)).fetchone()
        if row is None:
            raise NotFound(f"支付指令不存在: {payment_id}")
        d = dict(row)
        d["account"] = json.loads(d.pop("account_json"))
        return d

    def payments(self, case_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM payments WHERE case_id=? ORDER BY rowid",
                (case_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["account"] = json.loads(d.pop("account_json"))
            out.append(d)
        return out

    # ---------------------------------------------------------- 双人授权
    def create_auth_ticket(self, ticket: dict) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO auth_tickets(ticket_id,case_id,action,payload_hash,"
                "payload_json,threshold,proposer,status,reason,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ticket["ticket_id"], ticket["case_id"], ticket["action"],
                 ticket["payload_hash"], _j(ticket["payload"]),
                 int(ticket["threshold"]), ticket["proposer"], "proposed",
                 ticket.get("reason", ""), self.clock()))
            self.audit(conn, ticket["proposer"], "auth_propose",
                       ticket["case_id"], "proposed",
                       {"ticket_id": ticket["ticket_id"],
                        "action": ticket["action"]})
            conn.execute("COMMIT")

    def get_auth_ticket(self, ticket_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM auth_tickets WHERE ticket_id=?",
                               (ticket_id,)).fetchone()
        if row is None:
            raise NotFound(f"授权票据不存在: {ticket_id}")
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def decide_auth_ticket(self, ticket_id: str, authorizer: str,
                           approve: bool, reason: str = "") -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM auth_tickets WHERE ticket_id=?",
                (ticket_id,)).fetchone()
            if row is None:
                raise NotFound(f"授权票据不存在: {ticket_id}")
            if row["status"] != "proposed":
                raise Conflict(f"授权票据已结束: {row['status']}")
            if authorizer == row["proposer"]:
                raise Conflict("授权人不能是发起人本人（双人授权）")
            status = "approved" if approve else "rejected"
            conn.execute(
                "UPDATE auth_tickets SET status=?,authorizer=?,reason=?,decided_at=?"
                " WHERE ticket_id=?",
                (status, authorizer, reason, self.clock(), ticket_id))
            self.audit(conn, authorizer, "auth_decide", row["case_id"], status,
                       {"ticket_id": ticket_id, "action": row["action"]})
            conn.execute("COMMIT")
        return self.get_auth_ticket(ticket_id)

    def consume_auth_ticket_tx(self, conn, ticket_id: str) -> None:
        row = conn.execute("SELECT * FROM auth_tickets WHERE ticket_id=?",
                           (ticket_id,)).fetchone()
        if row is None:
            raise NotFound(f"授权票据不存在: {ticket_id}")
        if row["status"] != "approved":
            raise Conflict(f"授权票据未批准: {row['status']}")
        conn.execute("UPDATE auth_tickets SET status='consumed' WHERE ticket_id=?",
                     (ticket_id,))

    # ---------------------------------------------------------- API 幂等
    def get_idem(self, scope: str, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_idempotency WHERE scope=? AND idem_key=?",
                (scope, key)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["response_body"] = json.loads(d["response_body"])
        return d

    def put_idem(self, scope: str, key: str, request_hash: str,
                 status_code: int, body: dict) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO api_idempotency(scope,idem_key,request_hash,"
                "status_code,response_body,created_at) VALUES(?,?,?,?,?,?)",
                (scope, key, request_hash, status_code, _j(body), self.clock()))
            conn.execute("COMMIT")

    # ---------------------------------------------------------- 日结批次
    def begin_batch(self, run_date: str) -> bool:
        """创建日结批次；已存在（含未完成）返回 False 以便恢复续跑。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM daily_batches WHERE run_date=?",
                (run_date,)).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return False
            conn.execute(
                "INSERT INTO daily_batches(run_date,status,started_at) VALUES(?,'running',?)",
                (run_date, self.clock()))
            conn.execute("COMMIT")
            return True

    def batch_status(self, run_date: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM daily_batches WHERE run_date=?",
                               (run_date,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            cases = conn.execute(
                "SELECT status,COUNT(*) AS n FROM daily_batch_cases WHERE run_date=?"
                " GROUP BY status", (run_date,)).fetchall()
            d["counts"] = {r["status"]: r["n"] for r in cases}
            if d.get("summary_json"):
                d["summary"] = json.loads(d["summary_json"])
            return d

    def claim_batch_case(self, run_date: str, case_id: str) -> bool:
        """认领日结案件。新案件或此前失败的案件可认领；已完成的不可重复。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO daily_batch_cases(run_date,case_id,status,claimed_at)"
                " VALUES(?,?,'claimed',?)"
                " ON CONFLICT(run_date,case_id) DO UPDATE SET"
                " status='claimed',claimed_at=excluded.claimed_at,"
                " finished_at=NULL,detail=''"
                " WHERE daily_batch_cases.status='failed'",
                (run_date, case_id, self.clock()))
            won = cur.rowcount == 1
            conn.execute("COMMIT")
            return won

    def finish_batch_case(self, run_date: str, case_id: str, status: str,
                          detail: str = "") -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE daily_batch_cases SET status=?,detail=?,finished_at=?"
                " WHERE run_date=? AND case_id=?",
                (status, detail, self.clock(), run_date, case_id))
            conn.execute("COMMIT")

    def pending_batch_cases(self, run_date: str) -> list[str]:
        """崩溃恢复：返回已 claim 但未 finish 的案件，按案件号确定性排序。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT case_id FROM daily_batch_cases WHERE run_date=? AND status='claimed'"
                " ORDER BY case_id", (run_date,)).fetchall()
            return [r["case_id"] for r in rows]

    def complete_batch(self, run_date: str, summary: dict) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE daily_batches SET status='completed',finished_at=?,"
                "summary_json=? WHERE run_date=?",
                (self.clock(), _j(summary), run_date))
            conn.execute("COMMIT")

    def unsettled_cases(self, as_of_date: str) -> list[str]:
        """日结候选：有费用事实、尚无医疗结算、分娩日期不晚于跑批日。按案件号排序。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT c.case_id FROM cases c"
                " JOIN fee_facts f ON f.case_id=c.case_id"
                " WHERE c.delivery_date<=?"
                " AND NOT EXISTS (SELECT 1 FROM medical_settlements m"
                "   WHERE m.case_id=c.case_id)"
                " ORDER BY c.case_id", (as_of_date,)).fetchall()
            return [r["case_id"] for r in rows]

    def audit_tail(self, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY audit_id DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out

    # ---------------------------------------------------------- 重建
    def rebuild(self) -> dict:
        """清空投影、按 (case_id, seq) 重放全部事件。用于进程恢复后校验/修复。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for tbl in PROJECTION_TABLES:
                conn.execute(f"DELETE FROM {tbl}")
            case_ids = [r["case_id"] for r in conn.execute(
                "SELECT DISTINCT case_id FROM events ORDER BY case_id")]
            counts = {"events": 0, "ledger": 0}
            for cid in case_ids:
                rows = conn.execute(
                    "SELECT * FROM events WHERE case_id=? ORDER BY seq",
                    (cid,)).fetchall()
                max_seq = 0
                for row in rows:
                    ev = self._row_to_event(row)
                    self._project_event(conn, cid, ev)
                    counts["events"] += 1
                    max_seq = ev.seq
                conn.execute("UPDATE cases SET version=? WHERE case_id=?",
                             (max_seq, cid))
            counts["ledger"] = conn.execute(
                "SELECT COUNT(*) AS n FROM ledger_entries").fetchone()["n"]
            self.audit(conn, "system", "projection_rebuild", None, "ok", counts)
            conn.execute("COMMIT")
            return counts

