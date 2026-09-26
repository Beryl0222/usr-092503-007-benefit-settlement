"""确定性政策规则引擎。

两类规则按各自的适用维度选取：
- 服务包 PolicyPackage：按**参保地**与**分娩日期**选择生效版本，决定分类支付规则、
  零自付范围、津贴天数与灵活就业是否纳入；
- 就医地目录 ItemCatalog：按**就医地**与**分娩日期**选择生效版本，
  决定项目编码属于哪个费用分类；目录里没有的项目一律按政策范围外处理，
  医院自报分类不能扩大政策范围（零自付不得掩盖范围外项目）。

所有金额计算只用整数运算，结果与执行顺序无关。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .contracts import CostCategory
from .errors import RuleNotInEffect
from .models import Enrollment, FeeLine, SettledLine, SettlementResult


def _iso(d: str) -> date:
    return date.fromisoformat(d)


def _active_on(effective_from: str, effective_to: str | None, on: date) -> bool:
    if _iso(effective_from) > on:
        return False
    if effective_to is not None and _iso(effective_to) <= on:
        return False
    return True


@dataclass(frozen=True, slots=True)
class CategoryRule:
    """单个费用分类的支付规则。

    mode:
      - "zero_copay"：政策范围内零自付，基金按定额上限支付，超标准部分个人负担；
      - "ratio"：基金按 num/denom 比例支付，其余个人负担（并发症按各自规则）；
      - "none"：不在支付范围，全额个人负担（如基础包未覆盖的镇痛）。
    """

    category: CostCategory
    mode: str
    cap: int | None = None
    ratio_num: int = 0
    ratio_denom: int = 1
    note: str = ""

    def fund_share(self, charge: int) -> tuple[int, str]:
        if self.mode == "zero_copay":
            if self.cap is None:
                return charge, "政策范围内零自付，基金全额支付"
            if charge <= self.cap:
                return charge, "政策范围内零自付，未超定额"
            return self.cap, f"政策范围内零自付定额 {self.cap}，超标准部分个人负担"
        if self.mode == "ratio":
            fund = charge * self.ratio_num // self.ratio_denom
            return fund, (
                f"按 {self.ratio_num}/{self.ratio_denom} 比例支付{self.note}"
            )
        return 0, "不在本服务包支付范围，全额个人负担"


@dataclass(frozen=True, slots=True)
class PolicyPackage:
    """参保地服务包版本（含有效期，有效期规则持久保存）。"""

    code: str
    version: int
    region: str
    effective_from: str
    effective_to: str | None
    include_flexible: bool
    min_insured_months: int
    allowance_days: dict[str, int]       # normal / dystocia / multiple
    category_rules: dict[CostCategory, CategoryRule]
    published_at: str

    def rule_id(self, category: CostCategory) -> str:
        return f"{self.code}@v{self.version}:{category.value}"

    def active_on(self, on_date: str) -> bool:
        return _active_on(self.effective_from, self.effective_to, _iso(on_date))


@dataclass(frozen=True, slots=True)
class ItemCatalog:
    """就医地费用目录版本：项目编码 -> 费用分类。"""

    code: str
    version: int
    region: str
    effective_from: str
    effective_to: str | None
    items: dict[str, CostCategory]
    published_at: str = ""

    def entry_id(self, item_code: str) -> str:
        return f"{self.code}@v{self.version}:item:{item_code}"

    def active_on(self, on_date: str) -> bool:
        return _active_on(self.effective_from, self.effective_to, _iso(on_date))


def select_version(packages: list, region: str, on_date: str):
    """选择统筹区在指定日期生效的最高版本；无生效版本则报错。"""

    on = _iso(on_date)
    candidates = [
        p for p in packages
        if p.region == region and _active_on(p.effective_from, p.effective_to, on)
    ]
    if not candidates:
        raise RuleNotInEffect(f"{region} 在 {on_date} 没有生效的规则版本")
    return max(candidates, key=lambda p: (p.version, p.published_at))


@dataclass(slots=True)
class PolicyRegistry:
    packages: list[PolicyPackage] = field(default_factory=list)
    catalogs: list[ItemCatalog] = field(default_factory=list)

    def add_package(self, pkg: PolicyPackage) -> None:
        for other in self.packages:
            if (other.code == pkg.code and other.region == pkg.region
                    and other.version == pkg.version):
                raise ValueError(f"服务包版本重复: {pkg.code} v{pkg.version}")
        self.packages.append(pkg)

    def add_catalog(self, cat: ItemCatalog) -> None:
        for other in self.catalogs:
            if (other.code == cat.code and other.region == cat.region
                    and other.version == cat.version):
                raise ValueError(f"目录版本重复: {cat.code} v{cat.version}")
        self.catalogs.append(cat)

    def package_for(self, home_region: str, delivery_date: str) -> PolicyPackage:
        return select_version(self.packages, home_region, delivery_date)

    def catalog_for(self, care_region: str, delivery_date: str) -> ItemCatalog:
        return select_version(self.catalogs, care_region, delivery_date)


def settle_lines(case_id: str, lines: list[FeeLine], pkg: PolicyPackage,
                 cat: ItemCatalog) -> SettlementResult:
    """对一批费用明细逐行结算，每行都保留命中规则与原因。

    - 比例规则（并发症等）逐行按整数向下取整计算；
    - 零自付定额是**分类级**标准：同一分类合计后只套用一次定额，
      超额部分按明细顺序确定分摊，防止拆分项目重复享受定额；
    - 目录外项目全额个人负担，绝不并入零自付。
    """
    # 1) 逐行解析分类与政策范围
    resolved: list[tuple[FeeLine, int, CostCategory, str]] = []
    for ln in lines:
        charge = ln.effective_amount()
        if charge < 0:
            raise ValueError("费用金额不能为负")
        if ln.item_code in cat.items:
            rcat = cat.items[ln.item_code]
            scope_rule_id = cat.entry_id(ln.item_code)
        else:
            rcat = CostCategory.OUT_OF_SCOPE
            scope_rule_id = cat.entry_id(ln.item_code)
        resolved.append((ln, charge, rcat, scope_rule_id))

    # 2) 分类级零自付定额的剩余额度（按明细顺序确定性分摊）
    cap_remaining: dict[CostCategory, int] = {}
    for rcat, rule in pkg.category_rules.items():
        if rule.mode == "zero_copay" and rule.cap is not None:
            cap_remaining[rcat] = rule.cap

    settled: list[SettledLine] = []
    by_cat: dict[str, list[int]] = {}
    fund_total = personal_total = charge_total = 0

    for ln, charge, rcat, scope_rule_id in resolved:
        if rcat == CostCategory.OUT_OF_SCOPE:
            fund, reason = 0, "项目不在就医地政策目录内，全额个人负担（零自付不覆盖范围外项目）"
        elif rcat not in pkg.category_rules:
            rcat = CostCategory.OUT_OF_SCOPE
            fund, reason = 0, "服务包未配置该分类规则，按范围外处理"
        else:
            rule = pkg.category_rules[rcat]
            if rule.mode == "zero_copay" and rule.cap is not None:
                left = cap_remaining.get(rcat, 0)
                if charge <= left:
                    fund = charge
                    reason = "政策范围内零自付，分类定额未超"
                else:
                    fund = max(left, 0)
                    reason = (f"政策范围内零自付，分类定额 {rule.cap}"
                              "已用尽/超额部分个人负担")
                cap_remaining[rcat] = left - charge
            elif rule.mode == "zero_copay":
                fund, reason = charge, "政策范围内零自付，基金全额支付"
            else:
                fund, reason = rule.fund_share(charge)
            if ln.remote_confirmed:
                reason = "异地回执核定后：" + reason

        fund = max(0, min(fund, charge))
        personal = charge - fund
        rule_id = scope_rule_id if rcat == CostCategory.OUT_OF_SCOPE else (
            scope_rule_id + "|" + pkg.rule_id(rcat)
        )

        settled.append(SettledLine(
            claim_id=ln.claim_id,
            item_code=ln.item_code,
            claimed_category=ln.category,
            resolved_category=rcat,
            in_scope=rcat != CostCategory.OUT_OF_SCOPE,
            charge_amount=charge,
            fund_payable=fund,
            personal_payable=personal,
            rule_id=rule_id,
            reason=reason,
        ))
        totals = by_cat.setdefault(rcat.value, [0, 0])
        totals[0] += fund
        totals[1] += personal
        fund_total += fund
        personal_total += personal
        charge_total += charge

    return SettlementResult(
        case_id=case_id,
        package_code=pkg.code,
        package_version=pkg.version,
        catalog_code=cat.code,
        catalog_version=cat.version,
        lines=tuple(settled),
        fund_total=fund_total,
        personal_total=personal_total,
        charge_total=charge_total,
        by_category={k: (v[0], v[1]) for k, v in sorted(by_cat.items())},
    )


# ---------------------------------------------------------------- 津贴

def insured_months(enr: Enrollment, on_date: str) -> int:
    """计算截至某日的连续参保月数（含当月，退保后不再增长）。"""

    start = _iso(enr.insured_from[:10])
    end = _iso(on_date)
    cap_end = _iso(enr.insured_to[:10]) if enr.insured_to else end
    end = min(end, cap_end)
    if end < start:
        return 0
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def allowance_eligibility(enr: Enrollment, pkg: PolicyPackage,
                          delivery_date: str, allowance_kind: str
                          ) -> tuple[bool, str]:
    if enr.job_kind == "flexible" and not pkg.include_flexible:
        return False, "该服务包版本未将灵活就业人员纳入生育保险"
    months = insured_months(enr, delivery_date)
    if months < pkg.min_insured_months:
        return False, (
            f"连续参保 {months} 个月，不足 {pkg.min_insured_months} 个月"
        )
    if allowance_kind not in pkg.allowance_days:
        return False, f"未知津贴类型: {allowance_kind}"
    return True, f"连续参保 {months} 个月，符合条件"


def allowance_amount(monthly_base: int, days: int) -> int:
    """生育津贴 = 月计发基数 × 天数 ÷ 30，整数向下取整（确定性）。"""

    if monthly_base < 0 or days < 0:
        raise ValueError("计发基数与天数不能为负")
    return monthly_base * days // 30
