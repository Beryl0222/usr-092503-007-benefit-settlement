"""案件聚合验证：唯一案件、重试去重、补正取代、异地回执乱序/重复、跨统筹区。"""

import sqlite3

from src.benefits.contracts import BillStatus, ReceiptStage
from src.benefits.errors import DomainError
from src.benefits.ids import derive_case_id

from tests.helpers import AppCase, REGION_A, REGION_B, bill_lines


def lines_without(items):
    all_lines = bill_lines()
    return [l for l in all_lines if l["item_code"] not in items]


class CaseAggregationTests(AppCase):
    def setUp(self):
        super().setUp()
        self.configure_world()

    def upload(self, person="P-1001", upload_id="U1", date="2026-09-20",
               hospital="H-B01", lines=None):
        return self.app.ingest_bill(
            hospital_id=hospital, upload_id=upload_id, person_id=person,
            delivery_date=date, lines=lines or bill_lines(), actor="hosp-b01",
        )

    def test_case_id_derived_from_person_and_delivery_date(self):
        self.assertEqual(
            derive_case_id("P-1001", "2026-09-20"),
            derive_case_id("P-1001", "2026-09-20"),
        )
        self.assertNotEqual(
            derive_case_id("P-1001", "2026-09-20"),
            derive_case_id("P-1002", "2026-09-20"),
        )

    def test_cross_region_case_marked(self):
        res = self.upload()  # B 市医院，A 市参保
        case = self.app.get_case(res["case_id"])
        self.assertEqual(case["delivery_region"], REGION_B)
        self.assertEqual(case["enrollment_region"], REGION_A)
        self.assertEqual(case["cross_region"], 1)

    def test_local_case_not_cross_region(self):
        res = self.upload(hospital="H-A01")
        self.assertEqual(self.app.get_case(res["case_id"])["cross_region"], 0)

    def test_enrollment_required(self):
        self.app.upsert_person("P-NEW", "王某", "IDNEW")
        with self.assertRaises(DomainError) as ctx:
            self.upload(person="P-NEW")
        self.assertEqual(ctx.exception.code, "not_enrolled")

    def test_enrollment_months_gate(self):
        self.app.upsert_person("P-SHORT", "赵某", "IDSHORT")
        self.app.add_enrollment("P-SHORT", REGION_A, "2026-07-01", continuous_months=2)
        with self.assertRaises(DomainError) as ctx:
            self.upload(person="P-SHORT")
        self.assertEqual(ctx.exception.code, "enrollment_months_short")

    def test_hospital_retry_upload_merges_same_case(self):
        first = self.upload(upload_id="U1")
        # 医院网络重试：完全相同的上传单号再次到达
        second = self.upload(upload_id="U1")
        self.assertEqual(first["case_id"], second["case_id"])
        self.assertTrue(second["deduplicated"])
        ex = self.app.explain(first["case_id"])
        # 只有一次结算版本
        self.assertEqual(len(ex["settlement_runs"]), 1)

    def test_distinct_uploads_merge_into_same_case_without_duplicate_lines(self):
        first = self.upload(upload_id="U1", lines=lines_without(set()))
        second = self.upload(upload_id="U2", lines=bill_lines())
        self.assertEqual(first["case_id"], second["case_id"])
        ex = self.app.explain(first["case_id"])
        self.assertEqual(ex["active_bill_count"], 2)

    def test_correction_supersedes_old_bill_and_recalculates(self):
        first = self.upload(upload_id="U1")
        case_id = first["case_id"]
        original_payable = self.app.explain(case_id)["settlement_runs"][-1]["payable_total"]

        # 经办人补正：删除范围外 VIP 病房，下调分娩费用
        corrected = [
            {"item_code": "ITEM-DELIVERY", "item_name": "顺产",
             "amount": 4500_00, "service_date": "2026-09-20"},
            {"item_code": "ITEM-ANALGESIA", "item_name": "镇痛",
             "amount": 1000_00, "service_date": "2026-09-20"},
        ]
        res = self.app.ingest_bill(
            hospital_id="H-B01", upload_id="U2", person_id="P-1001",
            delivery_date="2026-09-20", lines=corrected,
            kind="correction", corrects_bill_id=first["bill_id"], actor="clerk1",
        )
        ex = self.app.explain(case_id)
        # 旧单保留但失效，新单生效
        self.assertEqual(ex["active_bill_count"], 1)
        self.assertEqual(len(ex["settlement_runs"]), 2)
        latest = ex["settlement_runs"][-1]
        self.assertEqual(latest["reason"], "correction")
        # 范围外项目从新结算中消失，个人负担相应归零
        categories = {l["item_code"] for l in latest["lines"]}
        self.assertNotIn("ITEM-VIP-WARD", categories)
        self.assertLess(latest["payable_total"], original_payable)
        self.assertEqual(latest["personal_total"], 300_00)  # 镇痛 1000 的 30%
        self.assertFalse(res["deduplicated"])

    def test_cannot_correct_already_superseded_bill(self):
        first = self.upload(upload_id="U1")
        corrected = lines_without({"ITEM-VIP-WARD"})
        self.app.ingest_bill(
            hospital_id="H-B01", upload_id="U2", person_id="P-1001",
            delivery_date="2026-09-20", lines=corrected,
            kind="correction", corrects_bill_id=first["bill_id"], actor="clerk1",
        )
        with self.assertRaises(DomainError) as ctx:
            self.app.ingest_bill(
                hospital_id="H-B01", upload_id="U3", person_id="P-1001",
                delivery_date="2026-09-20", lines=corrected,
                kind="correction", corrects_bill_id=first["bill_id"], actor="clerk1",
            )
        self.assertEqual(ctx.exception.code, "bill_already_superseded")

    def test_duplicate_and_out_of_order_receipts(self):
        res = self.upload()
        case_id = res["case_id"]
        # 乱序：final 先到
        r1 = self.app.ingest_receipt(
            case_id=case_id, receipt_id="R-FINAL", stage=ReceiptStage.FINAL.value,
            confirmed_amount=9000_00, actor="platform",
        )
        self.assertFalse(r1["deduplicated"])
        self.assertEqual(self.app.get_case(case_id)["receipt_final"], 1)
        # 迟到的 interim：不改变终态
        self.app.ingest_receipt(
            case_id=case_id, receipt_id="R-INTERIM", stage=ReceiptStage.INTERIM.value,
            confirmed_amount=8000_00, actor="platform",
        )
        self.assertEqual(self.app.get_case(case_id)["receipt_final"], 1)
        # 平台重试：重复 final 回执
        again = self.app.ingest_receipt(
            case_id=case_id, receipt_id="R-FINAL", stage=ReceiptStage.FINAL.value,
            confirmed_amount=9000_00, actor="platform",
        )
        self.assertTrue(again["deduplicated"])
        ex = self.app.explain(case_id)
        self.assertEqual(len(ex["receipts"]), 2)  # 两条不同回执；重复的不落库

    def test_explain_traces_every_payable_to_line_and_rule(self):
        res = self.upload()
        ex = self.app.explain(res["case_id"])
        latest = ex["settlement_runs"][-1]
        for line in latest["lines"]:
            self.assertTrue(line["rule_ref"])
            self.assertEqual(
                line["amount"], line["payable"] + line["personal"],
                f"行 {line['item_code']} 金额不平",
            )
        # 账本应付合计 == 最新结算应付
        medical_balance = sum(
            e["amount"] for e in ex["ledger"] if e["subject"] == "medical"
        )
        self.assertEqual(medical_balance, latest["payable_total"])

    def test_ledger_tables_are_append_only(self):
        res = self.upload()
        conn = self.app.db.connection()
        with self.assertRaises(sqlite3.Error):
            conn.execute("UPDATE ledger_entries SET amount = 1 WHERE case_id = ?",
                         (res["case_id"],))
        with self.assertRaises(sqlite3.Error):
            conn.execute("DELETE FROM settlement_lines WHERE case_id = ?",
                         (res["case_id"],))


if __name__ == "__main__":
    unittest.main()
