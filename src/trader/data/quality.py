"""Integrity checks for historical bars (instructions.md §6 task 4; CRYPTO §5).

* Bars still in progress at retrieval time are excluded (Kraken's final OHLC row is always
  uncommitted; CRYPTO §5).
* Exact duplicates from overlapping pages are dropped; *conflicting* duplicates abort ingestion,
  because silently picking one version would hide a revision.
* Rows with impossible OHLC values are excluded and counted; the hole they leave is reported as
  missing data, so research fails closed on it.
* Missing intervals are measured against an explicit expectation: every interval for a 24/7
  venue, every regular-session interval for an exchange calendar. Sources that omit intervals
  without trades cannot distinguish "no trade" from "no data"; the report says so rather than
  guessing.
* Daily bars (``session_calendar``) are checked against the calendar instead of a fixed grid:
  each must be exactly one regular session ``[open, close)``, one bar per expected session;
  a missing session is reported, never filled (``data/daily_bars.py``).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trader.data.calendars import ExchangeCalendar
from trader.data.daily_bars import classify_session_bar, session_ranges
from trader.data.history import DataQualityError, HistoryBar
from trader.domain.types import iso_utc, require_utc

MAX_EXAMPLES = 20


@dataclass(frozen=True, slots=True)
class QualityReport:
    rows: int
    first_start: datetime | None
    last_start: datetime | None
    duplicates_dropped: int
    invalid_rows_excluded: int
    misaligned_rows_excluded: int
    unfinished_excluded: int
    outside_session_excluded: int
    expected_intervals: int
    missing_intervals: int
    missing_ranges: tuple[str, ...]
    missing_sessions: tuple[str, ...]
    zero_volume_bars: int
    gap_semantics: str
    problems: tuple[str, ...] = field(default=())

    @property
    def coverage(self) -> float:
        if self.expected_intervals == 0:
            return 0.0
        return 1.0 - self.missing_intervals / self.expected_intervals

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "first_start": None if self.first_start is None else iso_utc(self.first_start),
            "last_start": None if self.last_start is None else iso_utc(self.last_start),
            "duplicates_dropped": self.duplicates_dropped,
            "invalid_rows_excluded": self.invalid_rows_excluded,
            "misaligned_rows_excluded": self.misaligned_rows_excluded,
            "unfinished_excluded": self.unfinished_excluded,
            "outside_session_excluded": self.outside_session_excluded,
            "expected_intervals": self.expected_intervals,
            "missing_intervals": self.missing_intervals,
            "coverage": f"{self.coverage:.6f}",
            "missing_ranges_first": list(self.missing_ranges),
            "missing_sessions": list(self.missing_sessions),
            "zero_volume_bars": self.zero_volume_bars,
            "gap_semantics": self.gap_semantics,
            "problems": list(self.problems),
        }


def expected_starts_24_7(start: datetime, end: datetime, minutes: int) -> list[datetime]:
    start, end = require_utc(start), require_utc(end)
    step = timedelta(minutes=minutes)
    out: list[datetime] = []
    t = start
    while t < end:
        out.append(t)
        t += step
    return out


def expected_starts_sessions(
    calendar: ExchangeCalendar, start: datetime, end: datetime, minutes: int
) -> tuple[list[datetime], dict[str, list[datetime]]]:
    """Regular-session bar starts in ``[start, end)``, also grouped by session date."""
    start, end = require_utc(start), require_utc(end)
    step = timedelta(minutes=minutes)
    out: list[datetime] = []
    by_session: dict[str, list[datetime]] = {}
    day = calendar.session_date_of(start)
    last_day = calendar.session_date_of(end)
    while day <= last_day:
        session = calendar.session_for_date(day)
        if session is not None:
            t = session.open
            while t + step <= session.close:
                if start <= t < end:
                    out.append(t)
                    by_session.setdefault(day.isoformat(), []).append(t)
                t += step
        day += timedelta(days=1)
    return out, by_session


def clean_and_check(
    raw: Iterable[HistoryBar],
    *,
    minutes: int | None,
    retrieved_at: datetime,
    expected: Sequence[datetime],
    sessions: dict[str, list[datetime]] | None = None,
    gap_semantics: str,
    finalization_delay: timedelta = timedelta(seconds=1),
    session_calendar: ExchangeCalendar | None = None,
) -> tuple[list[HistoryBar], QualityReport]:
    """Clean raw bars and report on them.

    Fixed-interval bars pass ``minutes``; daily bars pass ``session_calendar`` instead (and
    ``minutes=None``), so each bar is checked against its own session's open and length.
    """
    if (minutes is None) == (session_calendar is None):
        raise ValueError("pass `minutes` for fixed-interval bars or `session_calendar` for daily")
    retrieved_at = require_utc(retrieved_at)
    expected_set = set(expected)
    by_start: dict[datetime, HistoryBar] = {}
    duplicates = invalid = misaligned = unfinished = outside = 0
    problems: list[str] = []
    for bar in raw:
        if minutes is not None and bar.minutes != minutes:
            raise DataQualityError(f"bar interval {bar.minutes} != requested {minutes}")
        if bar.end + finalization_delay > retrieved_at:
            unfinished += 1
            continue
        if session_calendar is not None:
            verdict = classify_session_bar(bar, session_calendar)
            if verdict == "MISALIGNED":
                misaligned += 1
                continue
            if verdict == "OUTSIDE_SESSION":
                outside += 1
                continue
        elif minutes is not None:
            epoch_minutes = int(bar.start.timestamp()) // 60
            if bar.start.second or bar.start.microsecond or epoch_minutes % minutes:
                misaligned += 1
                continue
        if bar.ohlc_problems():
            invalid += 1
            if len(problems) < MAX_EXAMPLES:
                problems.append(f"{iso_utc(bar.start)}: {', '.join(bar.ohlc_problems())}")
            continue
        if expected_set and bar.start not in expected_set:
            outside += 1
            continue
        existing = by_start.get(bar.start)
        if existing is not None:
            if existing != bar:
                raise DataQualityError(
                    f"conflicting duplicate bar at {iso_utc(bar.start)}: refusing to choose one"
                )
            duplicates += 1
            continue
        by_start[bar.start] = bar
    bars = [by_start[k] for k in sorted(by_start)]
    missing = sorted(expected_set - by_start.keys())
    missing_sessions: list[str] = []
    if sessions:
        present = by_start.keys()
        missing_sessions = [
            d for d, starts in sessions.items() if not any(s in present for s in starts)
        ]
    report = QualityReport(
        rows=len(bars),
        first_start=bars[0].start if bars else None,
        last_start=bars[-1].start if bars else None,
        duplicates_dropped=duplicates,
        invalid_rows_excluded=invalid,
        misaligned_rows_excluded=misaligned,
        unfinished_excluded=unfinished,
        outside_session_excluded=outside,
        expected_intervals=len(expected_set),
        missing_intervals=len(missing),
        missing_ranges=tuple(
            (
                _ranges(missing, minutes)
                if minutes is not None
                else session_ranges(missing, sorted(expected_set))
            )[:MAX_EXAMPLES]
        ),
        missing_sessions=tuple(missing_sessions),
        zero_volume_bars=sum(1 for b in bars if b.volume == 0),
        gap_semantics=gap_semantics,
        problems=tuple(problems),
    )
    return bars, report


def _ranges(missing: Sequence[datetime], minutes: int) -> list[str]:
    if not missing:
        return []
    step = timedelta(minutes=minutes)
    out: list[str] = []
    run_start = prev = missing[0]
    count = 1
    for t in missing[1:]:
        if t - prev == step:
            prev = t
            count += 1
            continue
        out.append(f"{iso_utc(run_start)} +{count} bars")
        run_start = prev = t
        count = 1
    out.append(f"{iso_utc(run_start)} +{count} bars")
    return out


def is_contiguous(starts: Sequence[datetime], minutes: int) -> bool:
    step = timedelta(minutes=minutes)
    return all(b - a == step for a, b in itertools.pairwise(starts))
