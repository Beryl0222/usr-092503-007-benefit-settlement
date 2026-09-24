"""结算规则和账本模块共用的稳定值。"""

from enum import StrEnum


class CostCategory(StrEnum):
    """待遇案件中的费用分类。"""

    BASIC_DELIVERY = "basic_delivery"
    ANALGESIA = "analgesia"
    COMPLICATION = "complication"
    PRENATAL_EXAM = "prenatal_exam"
    OUT_OF_SCOPE = "out_of_scope"


class LedgerAction(StrEnum):
    """资金账本允许记录的动作。"""

    PAYABLE = "payable"
    DISBURSEMENT = "disbursement"
    REVERSAL = "reversal"
    ADJUSTMENT = "adjustment"


def require_minor_units(value: int) -> int:
    """金额必须使用非负的最小货币单位。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("金额必须是非负整数")
    return value
