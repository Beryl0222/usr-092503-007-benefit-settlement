"""双人授权策略与凭证工具。

- 收款账户变更：一律双人授权（发起人之外的第二人批准）；
- 高额人工调整：金额绝对值达到阈值须双人授权；
- 授权票据与被授权载荷的哈希绑定，批准后只能按原载荷执行一次。
"""

from __future__ import annotations

import hashlib
import json
import uuid

# 默认高额人工调整阈值：50,000 元（最小货币单位为分 → 5,000,000）
DEFAULT_HIGH_ADJUSTMENT_THRESHOLD = 5_000_000


def new_ticket_id() -> str:
    return "AUTH-" + uuid.uuid4().hex[:16].upper()


def new_payment_id(prefix: str) -> str:
    return f"{prefix}-" + uuid.uuid4().hex[:16].upper()


def hash_payload(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def adjustment_requires_dual(amount: int,
                             threshold: int = DEFAULT_HIGH_ADJUSTMENT_THRESHOLD
                             ) -> bool:
    return abs(amount) >= threshold
