"""Injected clocks. Only this module may read the wall clock (enforced by ruff TID251)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol

from trader.domain.types import require_utc


class Clock(Protocol):
    def now(self) -> datetime: ...


class SleepingClock(Clock, Protocol):
    """A clock a real-time loop can wait on (paper mode). Tests inject a clock whose ``sleep``
    simply advances simulated time, so scheduling is deterministic."""

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ReplayClock:
    """Monotonic simulated clock driven by the replay event stream."""

    def __init__(self, start: datetime) -> None:
        self._now = require_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, when: datetime) -> None:
        when = require_utc(when)
        if when < self._now:
            raise ValueError(f"replay clock cannot move backwards: {when} < {self._now}")
        self._now = when
