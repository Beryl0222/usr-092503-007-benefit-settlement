"""测试辅助：临时数据库、冻结时钟与标准测试世界。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.benefits.app import BenefitsApp
from src.benefits.clock import FrozenClock
from src.benefits.policy import CategoryRule, PackageRules

REGION_A = "330100"
REGION_B = "330200"

FROZEN_NOW = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)


def standard_rules(**overrides) -> PackageRules:
    defaults = dict(
        enrollment_months_required=6,
        basic=CategoryRule(cap=None, rate_bp=10000),
        analgesia=CategoryRule(cap=2000_00, rate_bp=7000),
        complication=CategoryRule(cap=20000_00, rate_bp=8000, deductible=800_00),
        prenatal=CategoryRule(cap=1200_00, rate_bp=10000),
        allowance_divisor=30,
        high_adjustment_threshold=5000_00,
    )
    defaults.update(overrides)
    return PackageRules(**defaults)


CATALOG = [
    ("ITEM-DELIVERY", "basic_delivery", True),
    ("ITEM-ANALGESIA", "analgesia", True),
    ("ITEM-COMP-HEMORRHAGE", "complication", True),
    ("ITEM-PRENATAL", "prenatal_exam", True),
    ("ITEM-VIP-WARD", "out_of_scope", False),
]


def bill_lines():
    return [
        {"item_code": "ITEM-DELIVERY", "item_name": "顺产",
         "amount": 5000_00, "service_date": "2026-09-20"},
        {"item_code": "ITEM-VIP-WARD", "item_name": "VIP病房",
         "amount": 3000_00, "service_date": "2026-09-20"},
        {"item_code": "ITEM-ANALGESIA", "item_name": "镇痛分娩",
         "amount": 3000_00, "service_date": "2026-09-20"},
        {"item_code": "ITEM-COMP-HEMORRHAGE", "item_name": "产后出血处理",
         "amount": 5000_00, "service_date": "2026-09-20"},
        {"item_code": "ITEM-PRENATAL", "item_name": "产前检查",
         "amount": 1500_00, "service_date": "2026-09-10"},
    ]


class AppCase(unittest.TestCase):
    frozen = FROZEN_NOW

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = Path(tmp.name)
        self.app = BenefitsApp.open(
            self.db_path,
            clock=FrozenClock(self.frozen),
        )

    def tearDown(self):
        self.app.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)

    def configure_world(self, *, rules=None, package_from="2026-01-01"):
        rules = rules or standard_rules()
        self.app.upsert_hospital("H-B01", "B市妇幼保健院", REGION_B)
        self.app.upsert_hospital("H-A01", "A市人民医院", REGION_A)
        self.app.upsert_person("P-1001", "张某", "ID1001")
        self.app.upsert_person("P-1002", "李某", "ID1002")
        self.app.add_enrollment("P-1001", REGION_A, "2025-01-01", continuous_months=18)
        self.app.add_enrollment("P-1002", REGION_A, "2025-01-01", continuous_months=20)
        self.app.publish_package(REGION_A, package_from, rules, actor="admin")
        for item_code, category, in_scope in CATALOG:
            self.app.upsert_catalog(REGION_B, item_code, category, in_scope, "2026-01-01")
            self.app.upsert_catalog(REGION_A, item_code, category, in_scope, "2026-01-01")
        return rules
