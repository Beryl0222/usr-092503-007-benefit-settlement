"""时钟抽象：服务层通过注入时钟获取当前时间，测试可冻结。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """固定时钟，用于确定性测试与演示。"""

    def __init__(self, moment: datetime):
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
