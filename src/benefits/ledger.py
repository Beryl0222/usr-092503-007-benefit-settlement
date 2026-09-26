"""逐案可解释账本。

账本是追加式的：资金指令一旦发出就不可修改，只能通过指向原指令的冲正分录处理；
规则追溯只生成差额分录，绝不改写旧账。每条分录都带来源指针
（费用行 / 规则版本 / 授权票据 / 冲正父链），使应付、个人负担、差额均可回溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .contracts import LedgerAction
from .errors import PaymentError


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    entry_id: str
    case_id: str
    seq: int
    action: str                       # LedgerAction
    kind: str                         # medical_fund / allowance / delta ...
    amount: int                       # 带符号：应付为正，冲正/扣减为负
    payment_id: str | None
    reverses_entry_id: str | None     # 冲正链：指向被冲正分录
    origin: str                       # human / rule / reversal / retro
    refs: tuple[tuple[str, str], ...] # 来源指针，如 (item_code, ...)/(rule_id,...)
    note: str
    created_at: str
    auth_ticket_id: str | None = None

    def explain(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "seq": self.seq,
            "action": self.action,
            "kind": self.kind,
            "amount": self.amount,
            "payment_id": self.payment_id,
            "reverses_entry_id": self.reverses_entry_id,
            "origin": self.origin,
            "refs": [{"name": n, "value": v} for n, v in self.refs],
            "note": self.note,
            "created_at": self.created_at,
            "auth_ticket_id": self.auth_ticket_id,
        }


@dataclass(slots=True)
class CaseLedger:
    case_id: str
    entries: list[LedgerEntry] = field(default_factory=list)

    def _next_seq(self) -> int:
        return len(self.entries) + 1

    def append(self, action: str, kind: str, amount: int, *,
               origin: str, note: str, payment_id: str | None = None,
               reverses_entry_id: str | None = None,
               refs: tuple[tuple[str, str], ...] = (),
               auth_ticket_id: str | None = None,
               entry_id: str | None = None,
               created_at: str | None = None) -> LedgerEntry:
        seq = self._next_seq()
        eid = entry_id or f"{self.case_id}:L{seq:06d}"
        entry = LedgerEntry(
            entry_id=eid, case_id=self.case_id, seq=seq, action=action,
            kind=kind, amount=amount, payment_id=payment_id,
            reverses_entry_id=reverses_entry_id, origin=origin, refs=tuple(refs),
            note=note, created_at=created_at or utcnow(),
            auth_ticket_id=auth_ticket_id,
        )
        self.entries.append(entry)
        return entry

    def get(self, entry_id: str) -> LedgerEntry:
        for e in self.entries:
            if e.entry_id == entry_id:
                return e
        raise PaymentError(f"账本分录不存在: {entry_id}")

    def is_reversed(self, entry_id: str) -> bool:
        return any(e.reverses_entry_id == entry_id for e in self.entries)

    def reversal_chain(self, entry_id: str) -> list[str]:
        """返回从某分录开始的冲正链（含其自身）。"""
        chain = [entry_id]
        by_target: dict[str, str] = {}
        for e in self.entries:
            if e.reverses_entry_id:
                by_target[e.reverses_entry_id] = e.entry_id
        cur = entry_id
        while cur in by_target:
            cur = by_target[cur]
            chain.append(cur)
        return chain

    def balance(self, kind: str | None = None) -> int:
        return sum(e.amount for e in self.entries
                   if kind is None or e.kind == kind)

    def payable_balance(self) -> int:
        """应付未付：PAYABLE/ADJUSTMENT 与 DISBURSEMENT/REVERSAL 轧差。"""
        return self.balance()

    def disbursed_total(self) -> int:
        return sum(e.amount for e in self.entries
                   if e.action in (LedgerAction.DISBURSEMENT,
                                   LedgerAction.REVERSAL))

    def explain(self) -> list[dict]:
        return [e.explain() for e in self.entries]

    def validate_reversal(self, target: LedgerEntry) -> int:
        """校验冲正：目标必须是资金侧分录且未被冲正过，返回冲正绝对值。"""
        if target.action not in (LedgerAction.DISBURSEMENT, LedgerAction.REVERSAL):
            raise PaymentError("只能冲正资金侧分录（拨付/冲正）")
        if self.is_reversed(target.entry_id):
            raise PaymentError(f"分录 {target.entry_id} 已在冲正链上被冲正")
        return abs(target.amount)
