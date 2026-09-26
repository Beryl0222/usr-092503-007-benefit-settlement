"""领域错误。"""


class DomainError(Exception):
    """业务规则错误（HTTP 层映射为 4xx）。"""

    code = "domain_error"
    http_status = 422


class NotFound(DomainError):
    code = "not_found"
    http_status = 404


class Conflict(DomainError):
    """事实冲突或乐观锁版本不匹配。"""

    code = "conflict"
    http_status = 409


class DuplicateIdempotency(Conflict):
    code = "duplicate_idempotency"
    http_status = 409


class RuleNotInEffect(DomainError):
    code = "rule_not_in_effect"
    http_status = 422


class InsuredPeriodError(DomainError):
    code = "insured_period_error"
    http_status = 422


class SettlementError(DomainError):
    code = "settlement_error"
    http_status = 422


class PaymentError(DomainError):
    code = "payment_error"
    http_status = 422


class AuthorizationRequired(DomainError):
    """需要第二位授权人（双人授权）。"""

    code = "authorization_required"
    http_status = 403


class AuthorizationRejected(DomainError):
    code = "authorization_rejected"
    http_status = 403
