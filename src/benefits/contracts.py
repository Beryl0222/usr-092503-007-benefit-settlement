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


class CaseStatus(StrEnum):
    """案件整体状态。"""

    OPEN = "open"
    SETTLED = "settled"
    CLOSED = "closed"


class BillStatus(StrEnum):
    """费用上传单状态：补正产生的新单取代旧单。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


class ReceiptStage(StrEnum):
    """异地结算回执阶段。"""

    INTERIM = "interim"
    FINAL = "final"


class ReceiptStatus(StrEnum):
    """回执在案件上的合并结果。"""

    RECEIVED = "received"
    CONFIRMED = "confirmed"
    DUPLICATE = "duplicate"


class SettlementRunStatus(StrEnum):
    """结算版本状态。"""

    CURRENT = "current"
    SUPERSEDED = "superseded"


class SettlementReason(StrEnum):
    """触发一次结算版本的原因。"""

    INTAKE = "intake"
    CORRECTION = "correction"
    RETRO = "retro"
    MANUAL = "manual"


class AllowanceStatus(StrEnum):
    """生育津贴申请状态机。"""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"


class PaymentOrderStatus(StrEnum):
    """资金指令状态。已发出的指令只能经冲正链处理。"""

    PENDING = "pending"
    SENT = "sent"
    ACKED = "acked"
    FAILED = "failed"
    REVERSED = "reversed"


class PaymentOrderType(StrEnum):
    """资金指令类型：正向拨付或冲正。"""

    PAYMENT = "payment"
    REVERSAL = "reversal"


class TicketType(StrEnum):
    """双人授权工单类型。"""

    ACCOUNT_CHANGE = "account_change"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    ALLOWANCE_APPROVAL = "allowance_approval"


class TicketStatus(StrEnum):
    """双人授权工单状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class AuditAction(StrEnum):
    """审计动作分类。"""

    INTAKE = "intake"
    CORRECTION = "correction"
    RECEIPT = "receipt"
    SETTLEMENT = "settlement"
    ALLOWANCE = "allowance"
    ACCOUNT = "account"
    TICKET = "ticket"
    PAYMENT = "payment"
    RETRO = "retro"
    BATCH = "batch"


def require_minor_units(value: int) -> int:
    """金额必须使用非负的最小货币单位。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("金额必须是非负整数")
    return value
