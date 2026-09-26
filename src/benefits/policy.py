"""政策服务包、就医地目录与参保关系的查询/装配。

- 服务包按 *参保地* 发布、带版本与生效区间，按分娩日期选定，永久保存；
- 目录按 *就医地* 发布，决定每个项目的类别与是否在政策范围内；
- 参保关系分段记录，结算时校验分娩当日是否在保、连续缴费月数是否达标。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

from .errors import not_found
from .ids import new_id

# ---------------------------------------------------------------- 规则结构


@dataclass(frozen=True)
class CategoryRule:
    cap: Optional[int] = None           # 类别合计封顶，None 不封顶
    rate_bp: int = 10000                # 范围内支付比例（万分比）
    deductible: int = 0                 # 起付线（并发症类别共用）


@dataclass(frozen=True)
class PackageRules:
    enrollment_months_required: int
    basic: CategoryRule
    analgesia: CategoryRule
    complication: CategoryRule
    prenatal: CategoryRule
    allowance_divisor: int              # 津贴日额 = 月缴费基数 / 该除数
    high_adjustment_threshold: int      # 高额人工调整双人授权门槛（分）

    def category_rule(self, category: str) -> CategoryRule:
        mapping = {
            "basic_delivery": self.basic,
            "analgesia": self.analgesia,
            "complication": self.complication,
            "prenatal_exam": self.prenatal,
        }
        return mapping[category]

    def to_json(self) -> str:
        return json.dumps(
            {
                "enrollment_months_required": self.enrollment_months_required,
                "basic": self.basic.__dict__,
                "analgesia": self.analgesia.__dict__,
                "complication": self.complication.__dict__,
                "prenatal": self.prenatal.__dict__,
                "allowance_divisor": self.allowance_divisor,
                "high_adjustment_threshold": self.high_adjustment_threshold,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "PackageRules":
        d = json.loads(raw)
        return cls(
            enrollment_months_required=d["enrollment_months_required"],
            basic=CategoryRule(**d["basic"]),
            analgesia=CategoryRule(**d["analgesia"]),
            complication=CategoryRule(**d["complication"]),
            prenatal=CategoryRule(**d["prenatal"]),
            allowance_divisor=d["allowance_divisor"],
            high_adjustment_threshold=d["high_adjustment_threshold"],
        )


@dataclass(frozen=True)
class CatalogEntry:
    category: str
    in_scope: bool


@dataclass(frozen=True)
class PolicyPackage:
    package_id: str
    region_code: str
    version: int
    effective_from: str
    effective_to: Optional[str]
    rules: PackageRules


# ---------------------------------------------------------------- 仓储


class PolicyRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- 服务包发布 --
    def publish_package(
        self,
        region_code: str,
        effective_from: str,
        rules: PackageRules,
        *,
        created_at: str,
        effective_to: Optional[str] = None,
    ) -> PolicyPackage:
        """发布新版本服务包。同地区版本号递增；生效区间不允许重叠。"""

        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM policy_packages WHERE region_code = ?",
            (region_code,),
        ).fetchone()
        version = row["v"] + 1
        # 生效区间规则：同生效日允许发布新版本（按版本号取高，供追溯）；
        # 更早生效的新追溯包会把在其之后的旧包区间截断到新生效日。
        overlappers = self.conn.execute(
            """
            SELECT package_id, effective_from FROM policy_packages
            WHERE region_code = ?
              AND effective_from < ?
              AND (effective_to IS NULL OR effective_to > ?)
            """,
            (region_code, effective_from, effective_from),
        ).fetchall()
        for old in overlappers:
            self.conn.execute(
                "UPDATE policy_packages SET effective_to = ? WHERE package_id = ?",
                (effective_from, old["package_id"]),
            )
        package_id = new_id()
        self.conn.execute(
            """
            INSERT INTO policy_packages
                (package_id, region_code, effective_from, effective_to, version,
                 rules_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                package_id,
                region_code,
                effective_from,
                effective_to,
                version,
                rules.to_json(),
                created_at,
            ),
        )
        return self.get_package(package_id)

    def get_package(self, package_id: str) -> PolicyPackage:
        row = self.conn.execute(
            "SELECT * FROM policy_packages WHERE package_id = ?", (package_id,)
        ).fetchone()
        if row is None:
            raise not_found(f"服务包不存在: {package_id}", "package_not_found")
        return self._to_package(row)

    def package_for(self, region_code: str, on_date: str) -> PolicyPackage:
        """按日期选择参保地有效服务包；同日多版本取最高版本。"""

        row = self.conn.execute(
            """
            SELECT * FROM policy_packages
            WHERE region_code = ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY version DESC
            LIMIT 1
            """,
            (region_code, on_date, on_date),
        ).fetchone()
        if row is None:
            raise not_found(
                f"参保地 {region_code} 在 {on_date} 无有效服务包", "package_not_found"
            )
        return self._to_package(row)

    def packages_covering(self, region_code: str, on_date: str) -> list[PolicyPackage]:
        rows = self.conn.execute(
            """
            SELECT * FROM policy_packages
            WHERE region_code = ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY version
            """,
            (region_code, on_date, on_date),
        ).fetchall()
        return [self._to_package(r) for r in rows]

    @staticmethod
    def _to_package(row: sqlite3.Row) -> PolicyPackage:
        return PolicyPackage(
            package_id=row["package_id"],
            region_code=row["region_code"],
            version=row["version"],
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            rules=PackageRules.from_json(row["rules_json"]),
        )

    # -- 就医地目录 --
    def upsert_catalog_entry(
        self,
        region_code: str,
        item_code: str,
        category: str,
        in_scope: bool,
        effective_from: str,
        effective_to: Optional[str] = None,
    ) -> None:
        existing = self.conn.execute(
            """
            SELECT id FROM policy_catalog
            WHERE region_code = ? AND item_code = ? AND effective_from = ?
            """,
            (region_code, item_code, effective_from),
        ).fetchone()
        if existing:
            raise ValueError("目录项目在同一生效日已存在；请发布新生效段")
        self.conn.execute(
            """
            INSERT INTO policy_catalog
                (id, region_code, item_code, category, in_scope,
                 effective_from, effective_to)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                region_code,
                item_code,
                category,
                1 if in_scope else 0,
                effective_from,
                effective_to,
            ),
        )

    def catalog_lookup(self, region_code: str, on_date: str):
        """返回 item_code -> CatalogEntry 的闭包，按服务日期查就医地目录。"""

        rows = self.conn.execute(
            """
            SELECT item_code, category, in_scope FROM policy_catalog
            WHERE region_code = ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            """,
            (region_code, on_date, on_date),
        ).fetchall()
        table = {r["item_code"]: CatalogEntry(r["category"], bool(r["in_scope"])) for r in rows}

        def lookup(item_code: str) -> Optional[CatalogEntry]:
            return table.get(item_code)

        return lookup

    # -- 参保关系 --
    def add_enrollment(
        self,
        person_id: str,
        region_code: str,
        start_date: str,
        *,
        end_date: Optional[str] = None,
        continuous_months: int = 0,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO enrollment_periods
                (id, person_id, region_code, start_date, end_date, continuous_months)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id(), person_id, region_code, start_date, end_date, continuous_months),
        )

    def enrollment_on(self, person_id: str, on_date: str):
        """返回分娩当日有效参保关系 (region_code, continuous_months)，无则 None。"""

        row = self.conn.execute(
            """
            SELECT region_code, continuous_months FROM enrollment_periods
            WHERE person_id = ?
              AND start_date <= ?
              AND (end_date IS NULL OR end_date >= ?)
            ORDER BY start_date DESC
            LIMIT 1
            """,
            (person_id, on_date, on_date),
        ).fetchone()
        if row is None:
            return None
        return row["region_code"], row["continuous_months"]

    # -- 基础主体 --
    def upsert_hospital(self, hospital_id: str, name: str, region_code: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO hospitals(hospital_id, name, region_code) VALUES(?,?,?)",
            (hospital_id, name, region_code),
        )

    def get_hospital(self, hospital_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM hospitals WHERE hospital_id = ?", (hospital_id,)
        ).fetchone()
        if row is None:
            raise not_found(f"医院不存在: {hospital_id}", "hospital_not_found")
        return row

    def upsert_person(self, person_id: str, name: str, id_number: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO persons(person_id, name, id_number) VALUES(?,?,?)",
            (person_id, name, id_number),
        )

    def get_person(self, person_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM persons WHERE person_id = ?", (person_id,)
        ).fetchone()
        if row is None:
            raise not_found(f"参保人不存在: {person_id}", "person_not_found")
        return row
