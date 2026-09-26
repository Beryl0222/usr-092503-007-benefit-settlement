"""演示种子数据：两个统筹区、跨政策日期服务包、跨区就医案件。

仅用于本地演示与测试：重复执行安全（依赖唯一约束，重复会报错，
因此调用方应使用新数据库）。
"""

from __future__ import annotations

from .app import BenefitsApp
from .policy import CategoryRule, PackageRules

# 统筹区：A 市（参保地）、B 市（就医地）
REGION_A = "330100"
REGION_B = "330200"


def seed(app: BenefitsApp) -> None:
    # 主体
    app.upsert_hospital("H-B01", "B市妇幼保健院", REGION_B)
    app.upsert_hospital("H-A01", "A市人民医院", REGION_A)
    app.upsert_person("P-1001", "张某", "ID1001")
    app.upsert_person("P-1002", "李某", "ID1002")

    # 参保关系：P-1001 在 A 市连续缴费 12 个月；P-1002 同
    app.add_enrollment("P-1001", REGION_A, "2025-01-01", continuous_months=18)
    app.add_enrollment("P-1002", REGION_A, "2025-01-01", continuous_months=20)

    # A 市 2026-01-01 起服务包 v1：住院分娩零自付（100%）、
    # 镇痛 70%、并发症起付 800 元后 80%、产前检查封顶 1200 元
    rules_v1 = PackageRules(
        enrollment_months_required=6,
        basic=CategoryRule(cap=None, rate_bp=10000),
        analgesia=CategoryRule(cap=2000_00, rate_bp=7000),
        complication=CategoryRule(cap=20000_00, rate_bp=8000, deductible=800_00),
        prenatal=CategoryRule(cap=1200_00, rate_bp=10000),
        allowance_divisor=30,
        high_adjustment_threshold=5000_00,
    )
    app.publish_package(REGION_A, "2026-01-01", rules_v1)

    # 就医地 B 市目录
    catalog_b = [
        ("ITEM-DELIVERY", "basic_delivery", True),
        ("ITEM-ANALGESIA", "analgesia", True),
        ("ITEM-COMP-HEMORRHAGE", "complication", True),
        ("ITEM-PRENATAL", "prenatal_exam", True),
        ("ITEM-VIP-WARD", "out_of_scope", False),  # 范围外：零自付不覆盖
    ]
    for item_code, category, in_scope in catalog_b:
        app.upsert_catalog(REGION_B, item_code, category, in_scope, "2026-01-01")
    # A 市本地目录
    for item_code, category, in_scope in catalog_b:
        app.upsert_catalog(REGION_A, item_code, category, in_scope, "2026-01-01")
