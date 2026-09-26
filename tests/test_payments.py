"""资金指令与冲正链验证。"""

from src.benefits.contracts import LedgerAction, PaymentOrderStatus, PaymentOrderType
from src.benefits.errors import DomainError
from src.benefits.ledger import case_balance

from tests.helpers import AppCase, bill_lines


class PaymentFlowTests(AppCase):
    def setUp(self):
        super().setUp()
        self.configure_world()
        self.res = self.app.ingest_bill(
            hospital_id="H-B01", upload_id="U1", person_id="P-1001",
            delivery_date="2026-09-20", lines=bill_lines(), actor="hosp",
        )
        self.case_id = self.res["case_id"]
        self.app.register_account(
            case_id=self.case_id, account_no="62220001",
            account_name="张某", bank_code="ICBC", actor="clerk1",
        )

    def test_cannot_pay_more_than_balance(self):
        balance = case_balance(self.app.db.connection(), self.case_id)
        with self.assertRaises(DomainError) as ctx:
            self.app.create_payment(
                case_id=self.case_id, subject="medical",
                amount=balance + 1, payee_account="62220001",
                idem_key="P1", actor="clerk1",
            )
        self.assertEqual(ctx.exception.code, "insufficient_balance")

    def test_sent_order_cannot_be_edited_or_sent_again(self):
        order = self.app.create_payment(
            case_id=self.case_id, subject="medical", amount=5000_00,
            payee_account="62220001", idem_key="P1", actor="clerk1",
        )
        self.app.send_payment(order["order_id"], actor="clerk1")
        with self.assertRaises(DomainError) as ctx:
            self.app.send_payment(order["order_id"], actor="clerk1")
        self.assertEqual(ctx.exception.code, "order_not_pending")
        # 无删除/作废接口；历史指令只能冲正
        self.assertEqual(
            self.app.get_payment(order["order_id"])["status"],
            PaymentOrderStatus.SENT.value,
        )

    def test_reversal_chain_restores_balance_and_links_entries(self):
        balance_before = case_balance(self.app.db.connection(), self.case_id)
        order = self.app.create_payment(
            case_id=self.case_id, subject="medical", amount=5000_00,
            payee_account="62220001", idem_key="P1", actor="clerk1",
        )
        self.app.send_payment(order["order_id"], actor="clerk1")
        self.assertEqual(
            case_balance(self.app.db.connection(), self.case_id),
            balance_before - 5000_00,
        )

        reversal = self.app.reverse_payment(
            order["order_id"], reason="账户信息有误", actor="clerk2",
        )
        self.assertEqual(reversal["order_type"], PaymentOrderType.REVERSAL.value)
        # 重复冲正请求归并到同一冲正指令
        again = self.app.reverse_payment(
            order["order_id"], reason="账户信息有误", actor="clerk2",
        )
        self.assertEqual(again["order_id"], reversal["order_id"])

        self.app.send_payment(reversal["order_id"], actor="clerk2")
        # 余额恢复；原指令标记 reversed
        self.assertEqual(
            case_balance(self.app.db.connection(), self.case_id), balance_before
        )
        self.assertEqual(
            self.app.get_payment(order["order_id"])["status"],
            PaymentOrderStatus.REVERSED.value,
        )
        # 冲正链可回溯：reversal 分录指向原拨付分录
        ex = self.app.explain(self.case_id)
        disbursements = [e for e in ex["ledger"] if e["action"] == LedgerAction.DISBURSEMENT.value]
        reversals = [e for e in ex["ledger"] if e["action"] == LedgerAction.REVERSAL.value]
        self.assertEqual(len(disbursements), 1)
        self.assertEqual(len(reversals), 1)
        self.assertEqual(reversals[0]["reversal_of"], disbursements[0]["entry_id"])

        # 冲正指令不能再被冲正
        with self.assertRaises(DomainError) as ctx:
            self.app.reverse_payment(reversal["order_id"], reason="x", actor="clerk2")
        self.assertEqual(ctx.exception.code, "cannot_reverse_reversal")

    def test_payment_idempotency_key_survives_retry(self):
        o1 = self.app.create_payment(
            case_id=self.case_id, subject="medical", amount=1000_00,
            payee_account="62220001", idem_key="IDEM-1", actor="clerk1",
        )
        o2 = self.app.create_payment(
            case_id=self.case_id, subject="medical", amount=1000_00,
            payee_account="62220001", idem_key="IDEM-1", actor="clerk1",
        )
        self.assertEqual(o1["order_id"], o2["order_id"])
        orders = self.app.list_payments(self.case_id)
        self.assertEqual(len(orders), 1)


if __name__ == "__main__":
    unittest.main()
