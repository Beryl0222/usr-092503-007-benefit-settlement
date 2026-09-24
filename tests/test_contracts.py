import unittest

from src.benefits import CostCategory, LedgerAction, require_minor_units


class BenefitContractTests(unittest.TestCase):
    def test_amount_contract(self):
        self.assertEqual(require_minor_units(1600000), 1600000)

    def test_boolean_is_not_amount(self):
        with self.assertRaises(ValueError):
            require_minor_units(True)

    def test_reversal_action(self):
        self.assertEqual(LedgerAction.REVERSAL.value, "reversal")
        self.assertEqual(CostCategory.ANALGESIA.value, "analgesia")


if __name__ == "__main__":
    unittest.main()
