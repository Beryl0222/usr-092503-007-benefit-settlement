"""应用门面：组装存储、时钟与各领域服务，统一事务边界。

每个公开方法在一个 BEGIN IMMEDIATE 写事务内执行，保证案件归并、
账本登记、审计写入原子可见。
"""

from __future__ import annotations

from pathlib import Path

from .allowance import AllowanceService
from .approvals import ApprovalService
from .cases import CaseService
from .clock import Clock, SystemClock
from .db import Database
from .payments import PaymentService
from .policy import PackageRules, PolicyRepository
from .retro import RetroService


class BenefitsApp:
    def __init__(self, db: Database, clock: Clock | None = None):
        self.db = db
        self.clock = clock or SystemClock()

    @classmethod
    def open(cls, path: str | Path, *, clock: Clock | None = None) -> "BenefitsApp":
        db = Database(path)
        db.initialize()
        return cls(db, clock)

    # -- 事务助手 --
    def with_services(self, fn):
        with self.db.write_tx() as conn:
            return fn(_Bundle(conn, self.clock))

    def with_read(self, fn):
        with self.db.read_tx() as conn:
            return fn(_Bundle(conn, self.clock))

    # -- 政策配置（管理侧） --
    def publish_package(self, region_code, effective_from, rules: PackageRules,
                        *, actor="admin", effective_to=None):
        def op(b):
            pkg = b.policy.publish_package(
                region_code, effective_from, rules,
                created_at=self.clock.now().isoformat(timespec="seconds"),
                effective_to=effective_to,
            )
            return {"package_id": pkg.package_id, "version": pkg.version}
        return self.with_services(op)

    def upsert_catalog(self, region_code, item_code, category, in_scope,
                       effective_from, effective_to=None):
        return self.with_services(
            lambda b: b.policy.upsert_catalog_entry(
                region_code, item_code, category, in_scope,
                effective_from, effective_to
            )
            or {"ok": True}
        )

    def add_enrollment(self, person_id, region_code, start_date, **kw):
        return self.with_services(
            lambda b: b.policy.add_enrollment(person_id, region_code, start_date, **kw)
            or {"ok": True}
        )

    def upsert_hospital(self, hospital_id, name, region_code):
        return self.with_services(
            lambda b: b.policy.upsert_hospital(hospital_id, name, region_code) or {"ok": True}
        )

    def upsert_person(self, person_id, name, id_number):
        return self.with_services(
            lambda b: b.policy.upsert_person(person_id, name, id_number) or {"ok": True}
        )

    # -- 医院上传 / 补正 --
    def ingest_bill(self, **kwargs):
        return self.with_services(lambda b: b.cases.ingest_bill(**kwargs))

    # -- 异地回执 --
    def ingest_receipt(self, **kwargs):
        return self.with_services(lambda b: b.cases.ingest_receipt(**kwargs))

    # -- 津贴 --
    def submit_allowance(self, **kwargs):
        return self.with_services(lambda b: b.allowance.submit(**kwargs))

    def approve_allowance(self, **kwargs):
        return self.with_services(lambda b: b.allowance.approve(**kwargs))

    def reject_allowance(self, **kwargs):
        return self.with_services(lambda b: b.allowance.reject(**kwargs))

    # -- 账户与授权 --
    def register_account(self, **kwargs):
        return self.with_services(lambda b: b.approvals.register_account(**kwargs))

    def approve_ticket(self, **kwargs):
        return self.with_services(lambda b: b.approvals.approve(**kwargs))

    def reject_ticket(self, **kwargs):
        return self.with_services(lambda b: b.approvals.reject(**kwargs))

    def manual_adjustment(self, **kwargs):
        return self.with_services(lambda b: b.approvals.manual_adjustment(**kwargs))

    # -- 资金 --
    def create_payment(self, **kwargs):
        return self.with_services(lambda b: b.payments.create_order(**kwargs))

    def send_payment(self, order_id, *, actor):
        return self.with_services(lambda b: b.payments.send_order(order_id, actor=actor))

    def ack_payment(self, order_id, *, actor):
        return self.with_services(lambda b: b.payments.ack_order(order_id, actor=actor))

    def fail_payment(self, order_id, *, reason, actor):
        return self.with_services(lambda b: b.payments.fail_order(order_id, reason=reason, actor=actor))

    def reverse_payment(self, order_id, *, reason, actor):
        return self.with_services(
            lambda b: b.payments.reverse_order(order_id, reason=reason, actor=actor)
        )

    # -- 追溯 --
    def schedule_retro(self, *, region_code, package_id, effective_from,
                       effective_to=None, actor="admin"):
        return self.with_services(
            lambda b: b.retro.schedule_for_package(
                region_code=region_code, package_id=package_id,
                effective_from=effective_from, effective_to=effective_to, actor=actor,
            )
        )

    # -- 查询 --
    def get_case(self, case_id):
        return self.with_read(lambda b: b.cases.get_case(case_id))

    def explain(self, case_id):
        return self.with_read(lambda b: b.cases.explain(case_id))

    def get_allowance_by_case(self, case_id):
        return self.with_read(lambda b: b.allowance.get_by_case(case_id))

    def get_ticket(self, ticket_id):
        return self.with_read(lambda b: b.approvals.get_ticket(ticket_id))

    def get_payment(self, order_id):
        return self.with_read(lambda b: b.payments.get_order(order_id))

    def list_payments(self, case_id):
        return self.with_read(lambda b: b.payments.orders_for_case(case_id))

    def close(self):
        self.db.close()


class _Bundle:
    """单次事务内的服务集合。"""

    def __init__(self, conn, clock):
        self.policy = PolicyRepository(conn)
        self.cases = CaseService(conn, clock)
        self.allowance = AllowanceService(conn, clock)
        self.approvals = ApprovalService(conn, clock)
        self.payments = PaymentService(conn, clock)
        self.retro = RetroService(conn, clock)
