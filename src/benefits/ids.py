"""标识符派生：案件号由业务事实确定派生，其余实体使用随机主键。"""

from __future__ import annotations

import hashlib
import uuid


def new_id() -> str:
    """生成随机主键（费用单、结算版本、工单等）。"""

    return uuid.uuid4().hex


def derive_case_id(person_id: str, delivery_date: str) -> str:
    """以参保关系与分娩日期派生唯一案件号。

    同一参保人在同一分娩日期只存在一个业务事实，医院重试上传、
    经办人补正、异地回执乱序到达都必须归并到该案件。
    """

    key = f"case|{person_id}|{delivery_date}"
    return "C" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:23]


def idem_scope(*parts: str) -> str:
    """拼接幂等键作用域。"""

    return "|".join(parts)
