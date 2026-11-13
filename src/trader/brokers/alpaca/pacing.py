"""Request pacing for the Alpaca paper adapter (markets_and_brokers.md §2: 200 requests/minute).

A token bucket driven by the injected :class:`~trader.ops.clock.Clock` (never ``time.time()``).
Capacity ``C`` and refill rate ``r`` satisfy ``C + 60 r = limit``, so no 60-second window can
carry more than ``limit`` requests. When the bucket is empty the caller sleeps through the
injected ``sleep``; if the clock does not move while sleeping (a frozen or replay clock), the
slept time itself is credited, so pacing can never deadlock.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta

from trader.ops.clock import Clock

DEFAULT_RATE_LIMIT_PER_MINUTE = 200
_EPSILON = 1e-9


class TokenBucket:
    def __init__(
        self,
        *,
        limit_per_minute: int,
        clock: Clock,
        sleep: Callable[[float], None],
    ) -> None:
        if limit_per_minute < 2:
            raise ValueError("the request limit must allow at least two requests per minute")
        self.capacity = max(1, limit_per_minute // 20)
        self.per_second = (limit_per_minute - self.capacity) / 60.0
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self.capacity)
        self._t: datetime = clock.now()
        #: Number of times a request had to wait (observability and tests).
        self.waits = 0

    def _advance(self, to: datetime) -> None:
        if to > self._t:
            gained = (to - self._t).total_seconds() * self.per_second
            self._tokens = min(float(self.capacity), self._tokens + gained)
            self._t = to

    def acquire(self) -> None:
        """Take one request slot, sleeping until one is available."""
        while True:
            self._advance(self._clock.now())
            if self._tokens >= 1.0 - _EPSILON:
                self._tokens = max(0.0, self._tokens - 1.0)
                return
            # Whole microseconds, rounded up, so every wait makes measurable progress.
            wait = timedelta(microseconds=math.ceil((1.0 - self._tokens) / self.per_second * 1e6))
            self.waits += 1
            self._sleep(wait.total_seconds())
            self._advance(max(self._clock.now(), self._t + wait))
