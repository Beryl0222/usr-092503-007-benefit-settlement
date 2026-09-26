"""案件事件日志与确定性折叠。

每一条事实都是追加事件，永不修改、永不删除；案件当前状态由事件按序号折叠得到。
医院重试（同 claim_id 同明细）幂等折叠；经办人补正覆盖旧明细；
异地回执与上传先后无关——折叠时先汇总上传事实再套用最新回执。
进程在任意时刻崩溃，重启后重放事件即可恢复全部业务事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .contracts import CostCategory
from .models import FeeLine


class EventType(StrEnum):
    CASE_OPENED = "case_opened"
    FEES_REPORTED = "fees_reported"           # 医院上传（含重试）
    FEE_CORRECTED = "fee_corrected"           # 经办人补正
    FEE_WITHDRAWN = "fee_withdrawn"           # 撤回报传错误明细
    REMOTE_RECEIPT = "remote_receipt"         # 异地就医地回执（可乱序）
    ACCOUNT_CHANGED = "account_changed"       # 收款账户变更（双人授权后）
    MEDICAL_SETTLED = "medical_settled"       # 医疗费用结算入账
    ALLOWANCE_DECIDED = "allowance_decided"   # 津贴审批结论
    PAYMENT_ISSUED = "payment_issued"         # 资金指令发出
    PAYMENT_REVERSED = "payment_reversed"     # 冲正指令
    LEDGER_ADJUSTMENT = "ledger_adjustment"   # 授权人工调整
    RETRO_DELTA = "retro_delta"               # 规则追溯差额（不改旧账）


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    type: str
    payload: dict
    actor: str
    occurred_at: str
    idempotency_key: str | None = None
    auth_ticket_id: str | None = None


def _line_from_payload(claim_id: str, raw: dict) -> FeeLine:
    return FeeLine(
        claim_id=claim_id,
        item_code=raw["item_code"],
        category=CostCategory(raw["category"]),
        amount=int(raw["amount"]),
        service_date=raw["service_date"],
        remote_confirmed=bool(raw.get("remote_confirmed", False)),
        remote_accepted=(int(raw["remote_accepted"])
                         if raw.get("remote_accepted") is not None else None),
        remote_note=raw.get("remote_note", ""),
    )


@dataclass(slots=True)
class CaseState:
    case_id: str
    person_id: str = ""
    enrollment_id: str = ""
    home_region: str = ""
    care_region: str = ""
    delivery_date: str = ""
    opened: bool = False
    account: dict | None = None
    fee_uploads: dict = None        # key -> 上传/补正后的明细载荷
    receipts: dict = None           # key -> 最新回执载荷
    medical_settlements: list = None
    retro_deltas: list = None
    allowance: dict | None = None
    payments: list = None
    reversals: list = None
    adjustments: list = None
    version: int = 0

    def __post_init__(self):
        self.fee_uploads = {}
        self.receipts = {}
        self.medical_settlements = []
        self.retro_deltas = []
        self.payments = []
        self.reversals = []
        self.adjustments = []

    def all_fee_lines(self) -> list[FeeLine]:
        """上传事实套用最新异地回执，输出当前费用明细（与回执到达顺序无关）。"""
        lines: list[FeeLine] = []
        for key, raw in self.fee_uploads.items():
            receipt = self.receipts.get(key)
            merged = dict(raw)
            if receipt is not None:
                merged["remote_confirmed"] = True
                merged["remote_accepted"] = receipt["accepted_amount"]
                merged["remote_note"] = receipt.get("note", "")
            lines.append(_line_from_payload(raw["claim_id"], merged))
        return sorted(lines, key=lambda x: (x.service_date, x.claim_id,
                                            x.item_code))


def fold(events: list[Event]) -> CaseState:
    """按 seq 顺序重放事件，折叠出案件当前事实。"""

    events = sorted(events, key=lambda e: e.seq)
    state: CaseState | None = None

    for ev in events:
        p = ev.payload
        if ev.type == EventType.CASE_OPENED:
            state = CaseState(case_id=p["case_id"])
            state.person_id = p["person_id"]
            state.enrollment_id = p["enrollment_id"]
            state.home_region = p["home_region"]
            state.care_region = p["care_region"]
            state.delivery_date = p["delivery_date"]
            state.opened = True
            state.account = p.get("account")
            continue

        if state is None:
            raise ValueError(f"案件未建立却收到事件 {ev.type}")

        if ev.type == EventType.FEES_REPORTED:
            for raw in p["lines"]:
                key = (p["claim_id"], raw["item_code"])
                uploaded = dict(raw)
                uploaded["claim_id"] = p["claim_id"]
                # 重试：同 claim+item 已存在则忽略（内容应一致；以首次为准）
                self_key = key
                state.fee_uploads.setdefault(self_key, uploaded)
        elif ev.type == EventType.FEE_CORRECTED:
            for raw in p["lines"]:
                key = (p["claim_id"], raw["item_code"])
                uploaded = dict(raw)
                uploaded["claim_id"] = p["claim_id"]
                state.fee_uploads[key] = uploaded  # 补正覆盖
        elif ev.type == EventType.FEE_WITHDRAWN:
            for item_code in p["item_codes"]:
                state.fee_uploads.pop((p["claim_id"], item_code), None)
                state.receipts.pop((p["claim_id"], item_code), None)
        elif ev.type == EventType.REMOTE_RECEIPT:
            for r in p["receipts"]:
                # 乱序/重发：只接受同一批次序号更新的回执
                key = (p["claim_id"], r["item_code"])
                old = state.receipts.get(key)
                if old is None or int(r.get("batch_seq", 0)) >= int(
                        old.get("batch_seq", 0)):
                    state.receipts[key] = r
        elif ev.type == EventType.ACCOUNT_CHANGED:
            state.account = p["account"]
        elif ev.type == EventType.MEDICAL_SETTLED:
            state.medical_settlements.append(p)
        elif ev.type == EventType.RETRO_DELTA:
            state.retro_deltas.append(p)
        elif ev.type == EventType.ALLOWANCE_DECIDED:
            state.allowance = p
        elif ev.type == EventType.PAYMENT_ISSUED:
            state.payments.append(p)
        elif ev.type == EventType.PAYMENT_REVERSED:
            state.reversals.append(p)
        elif ev.type == EventType.LEDGER_ADJUSTMENT:
            state.adjustments.append(p)
        else:
            raise ValueError(f"未知事件类型: {ev.type}")

        state.version = ev.seq

    if state is None:
        raise ValueError("空事件流无法折叠")
    return state
