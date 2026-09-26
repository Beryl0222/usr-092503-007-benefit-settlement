"""结算引擎：纯函数、确定性计算，不触碰数据库。

输入：案件上下文 + 有效费用行 + 服务包规则 + 就医地目录查询函数。
输出：逐行结算明细（应付/个人负担/规则引用）与合计。

规则语义：
- 政策范围内项目按服务包规则结算；住院分娩范围内费用在 zero-copay 模式下
  由基金全额承担（个人零自付），但封顶线以上的部分仍由个人负担；
- 范围外项目（OUT_OF_SCOPE）无论任何政策均全额个人负担，
  零自付政策不得掩盖；
- 并发症费用按独立规则（起付线 + 比例 + 封顶）结算；
- 所有金额均为最小货币单位整数，乘法后整除截断，保证确定性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .contracts import CostCategory
from .policy import CatalogEntry, PackageRules

BP = 10_000  # 万分比基数


@dataclass(frozen=True)
class BillLineInput:
    bill_line_id: str
    item_code: str
    item_name: str
    amount: int
    service_date: str
    line_no: int


@dataclass(frozen=True)
class SettlementLineResult:
    bill_line_id: str
    item_code: str
    category: str
    in_scope: bool
    amount: int
    payable: int
    personal: int
    rule_ref: str


@dataclass(frozen=True)
class SettlementResult:
    lines: tuple[SettlementLineResult, ...]
    payable_total: int
    personal_total: int
    category_payable: dict = field(default_factory=dict)


def _rate_amount(amount: int, rate_bp: int) -> int:
    return amount * rate_bp // BP


def compute_settlement(
    lines: list[BillLineInput],
    rules: PackageRules,
    catalog_lookup: Callable[[str], Optional[CatalogEntry]],
    *,
    package_id: str,
) -> SettlementResult:
    """对一组有效费用行执行确定性结算。"""

    results: list[SettlementLineResult] = []
    # 类别合计封顶需要按确定性顺序累计：按行号排序
    ordered = sorted(lines, key=lambda l: (l.line_no, l.bill_line_id))
    category_used: dict[str, int] = {}
    complication_pool_used = 0  # 起付线按类别一次性扣除

    for line in ordered:
        entry = catalog_lookup(line.item_code)
        if entry is None or not entry.in_scope:
            # 目录未收录或标记范围外：全额个人负担，零自付不适用
            results.append(
                SettlementLineResult(
                    bill_line_id=line.bill_line_id,
                    item_code=line.item_code,
                    category=CostCategory.OUT_OF_SCOPE.value,
                    in_scope=False,
                    amount=line.amount,
                    payable=0,
                    personal=line.amount,
                    rule_ref=f"{package_id}:out_of_scope",
                )
            )
            continue

        category = entry.category
        rule = rules.category_rule(category)
        cat_used = category_used.get(category, 0)

        if category == CostCategory.COMPLICATION.value:
            payable, complication_pool_used = _complication_payable(
                line.amount, rule, complication_pool_used
            )
        else:
            payable = _rate_amount(line.amount, rule.rate_bp)

        # 类别合计封顶：超出部分转个人负担
        if rule.cap is not None:
            room = max(0, rule.cap - cat_used)
            payable = min(payable, room)
        category_used[category] = cat_used + payable

        personal = line.amount - payable
        results.append(
            SettlementLineResult(
                bill_line_id=line.bill_line_id,
                item_code=line.item_code,
                category=category,
                in_scope=True,
                amount=line.amount,
                payable=payable,
                personal=personal,
                rule_ref=(
                    f"{package_id}:{category}"
                    f"/rate={rule.rate_bp}"
                    f"/cap={rule.cap if rule.cap is not None else 'none'}"
                    + (
                        f"/deductible={rule.deductible}"
                        if category == CostCategory.COMPLICATION.value
                        else ""
                    )
                ),
            )
        )

    payable_total = sum(r.payable for r in results)
    personal_total = sum(r.personal for r in results)
    return SettlementResult(
        lines=tuple(results),
        payable_total=payable_total,
        personal_total=personal_total,
        category_payable=dict(category_used),
    )


def _complication_payable(
    amount: int, rule, pool_used: int
) -> tuple[int, int]:
    """并发症：先扣类别起付线，再按比例支付。返回 (应付, 起付线已用累计)。"""

    remaining_deductible = max(0, rule.deductible - pool_used)
    applied_deductible = min(remaining_deductible, amount)
    base = amount - applied_deductible
    payable = _rate_amount(base, rule.rate_bp)
    return payable, pool_used + applied_deductible


def compute_allowance(base_salary: int, leave_days: int, divisor: int) -> int:
    """生育津贴 = 月缴费基数 / 计发除数 × 产假天数（整数截断）。"""

    return base_salary // divisor * leave_days
