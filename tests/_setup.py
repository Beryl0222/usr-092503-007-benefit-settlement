"""测试共享装配工具。"""

from __future__ import annotations

import os
import tempfile

from src.benefits.service import BenefitService
from src.benefits.store import Store


def make_service() -> tuple[BenefitService, str]:
    tmp = tempfile.mkdtemp(prefix="benefit-test-")
    store = Store(os.path.join(tmp, "db.sqlite"))
    return BenefitService(store), tmp


def package(region: str = "A市", version: int = 1,
            effective_from: str = "2026-01-01",
            effective_to: str | None = None,
            cap: int = 400000, comp_num: int = 8, comp_denom: int = 10,
            include_flexible: bool = True, min_months: int = 6,
            allowance_days: dict | None = None,
            published_at: str = "2025-12-01T00:00:00Z") -> dict:
    return {
        "code": f"PKG-{region}", "region": region, "version": version,
        "effective_from": effective_from, "effective_to": effective_to,
        "include_flexible": include_flexible,
        "min_insured_months": min_months,
        "allowance_days": allowance_days or {"normal": 98, "dystocia": 113},
        "published_at": published_at,
    }


def package_rules(cap: int = 400000, comp_num: int = 8,
                  comp_denom: int = 10) -> list[dict]:
    return [
        {"category": "basic_delivery", "mode": "zero_copay", "cap": cap},
        {"category": "complication", "mode": "ratio",
         "ratio_num": comp_num, "ratio_denom": comp_denom},
        {"category": "analgesia", "mode": "none"},
        {"category": "prenatal_exam", "mode": "ratio",
         "ratio_num": 7, "ratio_denom": 10},
    ]


def catalog(region: str = "B市", version: int = 1,
            effective_from: str = "2026-01-01",
            effective_to: str | None = None,
            items: dict | None = None) -> dict:
    return {
        "code": f"CAT-{region}", "region": region, "version": version,
        "effective_from": effective_from, "effective_to": effective_to,
        "items": items if items is not None else {
            "D001": "basic_delivery",
            "C001": "complication",
            "A001": "analgesia",
        },
        "published_at": "2025-12-01T00:00:00Z",
    }


def seed_basic(store: Store, *, region_a: str = "A市", region_b: str = "B市",
               pkg_overrides: dict | None = None,
               cat_items: dict | None = None,
               rules_overrides: dict | None = None) -> None:
    store.upsert_enrollment({
        "enrollment_id": "E1", "person_id": "P1",
        "home_region": region_a, "insured_from": "2025-01-15",
        "job_kind": "flexible",
    })
    pkg = package(region_a, **(pkg_overrides or {}))
    rules = package_rules(**(rules_overrides or {}))
    store.put_package(pkg, rules)
    store.put_catalog(catalog(region_b, items=cat_items))


ACCOUNT = {"account_name": "张某", "bank_code": "ICBC", "account_no": "62220001"}
