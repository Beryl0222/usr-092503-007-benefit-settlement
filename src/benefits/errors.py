"""领域错误类型：所有业务校验失败统一以 DomainError 抛出。"""

from __future__ import annotations


class DomainError(Exception):
    """业务规则校验失败。code 供 API 层映射为稳定的错误码。"""

    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def bad_request(message: str, code: str = "bad_request") -> DomainError:
    return DomainError(code, message, status=400)


def not_found(message: str, code: str = "not_found") -> DomainError:
    return DomainError(code, message, status=404)


def conflict(message: str, code: str = "conflict") -> DomainError:
    return DomainError(code, message, status=409)


def forbidden(message: str, code: str = "forbidden") -> DomainError:
    return DomainError(code, message, status=403)
