"""The system clock against a reference clock (ops/clock_check.py): the offset arithmetic and
the measurements that must not be trusted. The paper runner's use of it at startup is tested in
``test_paper_runtime.py``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from trader.domain.types import DomainError
from trader.ops.clock_check import (
    MAX_ROUND_TRIP,
    ClockOffset,
    ClockOffsetUnknown,
    clock_offset,
    measure_clock_offset,
)

T0 = datetime(2026, 11, 10, 14, 30, tzinfo=UTC)
LIMIT = timedelta(milliseconds=250)
BROKER = "the broker's clock"


def ms(value: float) -> timedelta:
    return timedelta(milliseconds=value)


class SteppingClock:
    """Each ``now()`` returns the next scripted time (the request takes that long)."""

    def __init__(self, *times: datetime) -> None:
        self._times = list(times)

    def now(self) -> datetime:
        return self._times.pop(0)


def reference(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def test_a_slow_system_clock_is_behind_the_reference() -> None:
    """The 2026-11-10 case: sent at T0, answered 80 ms later, the broker says T0 + 510 ms."""
    measured = clock_offset(T0, T0 + ms(510), T0 + ms(80))
    assert measured == ClockOffset(offset=ms(470), round_trip=ms(80))
    assert measured.system_clock_is_behind and measured.milliseconds == 470
    assert measured.exceeds(LIMIT)
    assert measured.describe(BROKER) == "470 ms behind the broker's clock"


def test_a_fast_system_clock_is_ahead_of_the_reference() -> None:
    measured = clock_offset(T0, T0 - ms(360), T0 + ms(80))
    assert measured.offset == -ms(400) and not measured.system_clock_is_behind
    assert measured.exceeds(LIMIT)
    assert measured.describe(BROKER) == "400 ms ahead of the broker's clock"


@pytest.mark.parametrize(
    ("reference_ms", "text"),
    [
        (290, "250 ms behind the broker's clock"),  # exactly the limit is still within it
        (52, "12 ms behind the broker's clock"),
        (10, "30 ms ahead of the broker's clock"),
        (40, "within 1 ms of the broker's clock"),
        (40.4, "within 1 ms of the broker's clock"),
    ],
)
def test_an_offset_within_the_limit_is_not_a_problem(reference_ms: float, text: str) -> None:
    measured = clock_offset(T0, T0 + ms(reference_ms), T0 + ms(80))
    assert not measured.exceeds(LIMIT)
    assert measured.describe(BROKER) == text


def test_the_network_delay_cancels_out_at_the_middle_of_the_round_trip() -> None:
    """Clocks in step: whatever the (symmetric) delay, the offset is zero."""
    for delay in (ms(2), ms(150), ms(900)):
        measured = clock_offset(T0, T0 + delay, T0 + 2 * delay)
        assert measured.offset == timedelta(0) and measured.round_trip == 2 * delay


def test_measuring_reads_the_clock_before_and_after_the_request() -> None:
    clock = SteppingClock(T0, T0 + ms(80))
    measured = measure_clock_offset(clock, reference(T0 + ms(510)))
    assert measured == ClockOffset(offset=ms(470), round_trip=ms(80))


def test_a_slow_round_trip_is_not_used() -> None:
    slow = MAX_ROUND_TRIP + ms(1)
    with pytest.raises(ClockOffsetUnknown, match=r"round trip took 2\.0 s \(more than 2 s\)"):
        measure_clock_offset(SteppingClock(T0, T0 + slow), reference(T0 + slow / 2))
    # exactly the bound is still used
    measured = measure_clock_offset(
        SteppingClock(T0, T0 + MAX_ROUND_TRIP), reference(T0 + MAX_ROUND_TRIP / 2)
    )
    assert measured.offset == timedelta(0)


def test_a_reference_that_cannot_be_read_is_reported_not_raised_through() -> None:
    def unreachable() -> datetime:
        raise TimeoutError("no answer from the broker")

    with pytest.raises(ClockOffsetUnknown, match=r"could not be read \(TimeoutError\)"):
        measure_clock_offset(SteppingClock(T0, T0 + ms(80)), unreachable)


def test_a_system_clock_that_moved_during_the_request_is_not_used() -> None:
    """A time synchronisation in the middle of the measurement: the round trip is negative."""
    with pytest.raises(ClockOffsetUnknown, match="moved backwards"):
        measure_clock_offset(SteppingClock(T0, T0 - ms(300)), reference(T0))


def test_times_must_be_utc() -> None:
    with pytest.raises(DomainError, match="naive datetimes are rejected"):
        clock_offset(T0.replace(tzinfo=None), T0, T0)
