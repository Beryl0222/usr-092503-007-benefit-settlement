"""不可变领域值对象。金额一律使用最小货币单位的非负（或带符号）整数。"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import CostCategory


@dataclass(frozen=True, slots=True)
class Enrollment:
    """参保关系（参保地统筹区）。"""

    enrollment_id: str
    person_id: str
    home_region: str
    insured_from: str  # ISO 日期，含
    insured_to: str | None = None  # None 表示在保
    job_kind: str = "flexible"  # flexible_employment / regular


@dataclass(frozen=True, slots=True)
class FeeLine:
    """一条费用明细。claim_id 标识同一批上传（重试同 claim 即同一事实）。"""

    claim_id: str
    item_code: str
    category: CostCategory
    amount: int
    service_date: str
    remote_confirmed: bool = False        # 异地回执是否确认
    remote_accepted: int | None = None   # 回执核定额（None=未回执/全额）
    remote_note: str = ""

    def effective_amount(self) -> int:
        """异地回执核定后的金额；未回执按上传金额。"""
        if self.remote_accepted is None:
            return self.amount
        return min(self.amount, self.remote_accepted)

    def receipt_key(self) -> tuple[str, str]:
        return (self.claim_id, self.item_code)


@dataclass(frozen=True, slots=True)
class SettledLine:
    """逐行可解释结算结果。"""

    claim_id: str
    item_code: str
    claimed_category: CostCategory
    resolved_category: CostCategory
    in_scope: bool
    charge_amount: int          # 参与计算的费用（回执核定后）
    fund_payable: int
    personal_payable: int
    rule_id: str                # 命中的规则标识，可回溯
    reason: str


@dataclass(frozen=True, slots=True)
class SettlementResult:
    case_id: str
    package_code: str
    package_version: int
    catalog_code: str
    catalog_version: int
    lines: tuple[SettledLine, ...]
    fund_total: int
    personal_total: int
    charge_total: int
    by_category: dict[str, tuple[int, int]]  # category -> (fund, personal)

    def explain(self) -> list[dict]:
        return [
            {
                "claim_id": ln.claim_id,
                "item_code": ln.item_code,
                "claimed_category": ln.claimed_category.value,
                "resolved_category": ln.resolved_category.value,
                "in_scope": ln.in_scope,
                "charge_amount": ln.charge_amount,
                "fund_payable": ln.fund_payable,
                "personal_payable": ln.personal_payable,
                "rule_id": ln.rule_id,
                "reason": ln.reason,
            }
            for ln in self.lines
        ]
