"""Is the system clock in step with a reference clock (the broker's)?

Market data is stamped by its source. When the system clock is behind, every quote looks as if
it came from the future; beyond the policy limit (``quality.max_clock_skew_milliseconds``) the
paper runner raises a ``CLOCK_SKEW`` pause again and again, so no entry is taken. On 2026-11-10
a clock 0.47 s slow did that for the first 30 minutes of a session before anyone noticed. The
paper runner therefore measures the offset once at startup and says so loudly.

The measurement is one request: the system time before it (``sent_at``), the reference time in
the answer, and the system time after it (``received_at``). The reference is compared with the
middle of the round trip, which cancels the network delay when both directions take equally
long. A slow round trip (more than :data:`MAX_ROUND_TRIP`) says nothing reliable and is not
used. Nothing here reads the wall clock itself: the clock is injected (``trader.ops.clock``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from trader.domain.types import require_utc
from trader.ops.clock import Clock

#: A round trip longer than this is not used: the reference time could be off by half of it.
MAX_ROUND_TRIP = timedelta(seconds=2)
_ZERO = timedelta(0)
_MILLISECOND = timedelta(milliseconds=1)


class ClockOffsetUnknown(Exception):
    """The offset could not be measured (the message says why, in a few words)."""


@dataclass(frozen=True, slots=True)
class ClockOffset:
    """How far the reference clock is ahead of the system clock (negative: it is behind)."""

    offset: timedelta
    round_trip: timedelta

    @property
    def milliseconds(self) -> int:
        """The size of the offset in whole milliseconds, without its sign."""
        return abs(self.offset) // _MILLISECOND

    @property
    def system_clock_is_behind(self) -> bool:
        return self.offset > _ZERO

    def exceeds(self, limit: timedelta) -> bool:
        return abs(self.offset) > limit

    def describe(self, reference: str) -> str:
        """Where the system clock stands, e.g. ``470 ms behind the broker's clock``."""
        if self.milliseconds == 0:
            return f"within 1 ms of {reference}"
        side = "behind" if self.system_clock_is_behind else "ahead of"
        return f"{self.milliseconds} ms {side} {reference}"


def clock_offset(sent_at: datetime, reference_time: datetime, received_at: datetime) -> ClockOffset:
    """The offset from one request: the reference time against the middle of the round trip."""
    sent_at, received_at = require_utc(sent_at), require_utc(received_at)
    round_trip = received_at - sent_at
    middle = sent_at + round_trip / 2
    return ClockOffset(offset=require_utc(reference_time) - middle, round_trip=round_trip)


def measure_clock_offset(
    clock: Clock,
    read_reference: Callable[[], datetime],
    *,
    max_round_trip: timedelta = MAX_ROUND_TRIP,
) -> ClockOffset:
    """Measure once. Raises :class:`ClockOffsetUnknown` when the reference cannot be read, the
    round trip was too slow to trust, or the system clock itself moved during the request."""
    sent_at = clock.now()
    try:
        reference_time = read_reference()
    except Exception as exc:  # a diagnostic: whatever went wrong, the caller only notes it
        raise ClockOffsetUnknown(
            f"the reference clock could not be read ({type(exc).__name__})"
        ) from exc
    measured = clock_offset(sent_at, reference_time, clock.now())
    if measured.round_trip < _ZERO:
        raise ClockOffsetUnknown("the system clock moved backwards during the measurement")
    if measured.round_trip > max_round_trip:
        raise ClockOffsetUnknown(
            f"the round trip took {measured.round_trip.total_seconds():.1f} s "
            f"(more than {max_round_trip.total_seconds():g} s)"
        )
    return measured


__all__ = [
    "MAX_ROUND_TRIP",
    "ClockOffset",
    "ClockOffsetUnknown",
    "clock_offset",
    "measure_clock_offset",
]
