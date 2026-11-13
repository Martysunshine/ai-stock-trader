"""Exchange and settlement calendars.

Session times come from ``exchange_calendars`` (XNYS), never from fixed Berlin offsets
(architecture.md §8). Entry and close windows are calendar-relative so early closes work
(ADR-0012): entries are permitted in ``[open + 30 min, close - 30 min)`` and the close procedure
starts at ``close - 15 min`` (15:30 / 15:45 New York on a regular day).

US equity settlement uses its own business-day rule (ADR-0008): a settlement day must be an NYSE
session *and* not a Federal Reserve holiday (Columbus Day and Veterans Day are Fed holidays on
which NYSE trades). Broker-reported settled cash takes precedence when available.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Protocol
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from trader.domain.types import require_utc

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class Session:
    session_date: date
    open: datetime
    close: datetime

    @property
    def is_early_close(self) -> bool:
        return (self.close - self.open) < timedelta(hours=6, minutes=30)

    @property
    def minutes(self) -> int:
        """Whole minutes of regular trading: 390 on a regular day, 210 on a 13:00 close."""
        return int((self.close - self.open).total_seconds()) // 60


class TradingCalendar(Protocol):
    name: str

    def session_at(self, ts: datetime) -> Session | None: ...

    def entry_window(self, session: Session) -> tuple[datetime, datetime]: ...

    def close_procedure_start(self, session: Session) -> datetime: ...


class ExchangeCalendar:
    """Wrapper around an ``exchange_calendars`` calendar (regular hours only in v1)."""

    def __init__(
        self,
        code: str = "XNYS",
        *,
        entry_exclusion_open: timedelta = timedelta(minutes=30),
        entry_exclusion_close: timedelta = timedelta(minutes=30),
        close_procedure_lead: timedelta = timedelta(minutes=15),
    ) -> None:
        self.name = code
        self._cal = _calendar(code)
        self.entry_exclusion_open = entry_exclusion_open
        self.entry_exclusion_close = entry_exclusion_close
        self.close_procedure_lead = close_procedure_lead

    def session_for_date(self, day: date) -> Session | None:
        ts = pd.Timestamp(day)
        if not self._cal.is_session(ts):
            return None
        return Session(
            session_date=day,
            open=self._cal.session_open(ts).to_pydatetime().astimezone(UTC),
            close=self._cal.session_close(ts).to_pydatetime().astimezone(UTC),
        )

    def session_at(self, ts: datetime) -> Session | None:
        """The session whose regular hours contain ``ts`` (open inclusive, close exclusive)."""
        ts = require_utc(ts)
        session = self.session_for_date(ts.astimezone(NEW_YORK).date())
        if session is None or not (session.open <= ts < session.close):
            return None
        return session

    def session_date_of(self, ts: datetime) -> date:
        """Local exchange date used for session-scoped counters (even outside hours)."""
        return require_utc(ts).astimezone(NEW_YORK).date()

    def session_opening_at(self, ts: datetime) -> Session | None:
        """The session whose regular open is exactly ``ts``; daily bars are keyed by it."""
        session = self.session_for_date(self.session_date_of(ts))
        if session is None or session.open != require_utc(ts):
            return None
        return session

    def next_session(self, day: date) -> Session:
        candidate = day + timedelta(days=1)
        for _ in range(15):
            session = self.session_for_date(candidate)
            if session is not None:
                return session
            candidate += timedelta(days=1)
        raise RuntimeError(f"no session found within 15 days after {day}")

    def entry_window(self, session: Session) -> tuple[datetime, datetime]:
        return (
            session.open + self.entry_exclusion_open,
            session.close - self.entry_exclusion_close,
        )

    def close_procedure_start(self, session: Session) -> datetime:
        return session.close - self.close_procedure_lead

    def is_session_day(self, day: date) -> bool:
        return self.session_for_date(day) is not None

    # ---------------------------------------------------------------- session arithmetic
    # Daily-bar research counts sessions often (warm-up, month ends, holds in sessions), so
    # these use a table of every session built once per calendar code; they agree with
    # ``session_for_date`` and ``next_session`` (tests/test_daily_engine.py).
    def session_on(self, day: date) -> Session | None:
        """The session on ``day`` (table lookup), or ``None`` when ``day`` is not a session."""
        table = _session_table(self.name)
        i = table.index.get(day)
        return None if i is None else table.session(i)

    def sessions_after(self, day: date, count: int = 1) -> Session:
        """The ``count``-th session after ``day`` (``day`` itself need not be a session)."""
        if count < 1:
            raise ValueError("count must be at least 1")
        table = _session_table(self.name)
        i = bisect_right(table.days, day) + count - 1
        if i >= len(table.days):
            raise RuntimeError(f"no session {count} after {day} in the {self.name} calendar")
        return table.session(i)

    def session_open_before(self, ts: datetime, count: int) -> datetime:
        """The open of the ``count``-th session, counting back, whose open is before ``ts``."""
        if count < 1:
            raise ValueError("count must be at least 1")
        table = _session_table(self.name)
        i = bisect_left(table.opens, require_utc(ts)) - count
        if i < 0:
            raise RuntimeError(f"no session {count} before {ts} in the {self.name} calendar")
        return table.opens[i]


@dataclass(frozen=True, slots=True)
class _SessionTable:
    days: tuple[date, ...]
    opens: tuple[datetime, ...]
    closes: tuple[datetime, ...]
    index: dict[date, int]

    def session(self, i: int) -> Session:
        return Session(session_date=self.days[i], open=self.opens[i], close=self.closes[i])


@lru_cache(maxsize=8)
def _session_table(code: str) -> _SessionTable:
    cal = _calendar(code)
    days = tuple(ts.date() for ts in cal.sessions)
    opens = tuple(ts.to_pydatetime().astimezone(UTC) for ts in cal.opens)
    closes = tuple(ts.to_pydatetime().astimezone(UTC) for ts in cal.closes)
    if not len(days) == len(opens) == len(closes):
        raise RuntimeError(f"{code}: session, open and close tables differ in length")
    return _SessionTable(days, opens, closes, {d: i for i, d in enumerate(days)})


class Utc247Calendar:
    """Continuous crypto calendar: a "session" is a UTC calendar day (CRYPTO §3).

    Every date is a session ``[00:00, 24:00)`` UTC of 1440 minutes; there are no holidays, early
    closes or weekends. The session-arithmetic methods mirror :class:`ExchangeCalendar`'s, so
    daily features and rules count sessions the same way on either calendar
    (``features/daily.py``): the next session is the next UTC day, and a month's last session is
    its last calendar day.
    """

    name = "UTC_24_7"

    def session_at(self, ts: datetime) -> Session:
        ts = require_utc(ts)
        start = datetime(ts.year, ts.month, ts.day, tzinfo=UTC)
        return Session(session_date=start.date(), open=start, close=start + timedelta(days=1))

    def entry_window(self, session: Session) -> tuple[datetime, datetime]:
        return session.open, session.close

    def close_procedure_start(self, session: Session) -> datetime:
        return session.close

    # ---------------------------------------------------------------- session arithmetic
    def session_for_date(self, day: date) -> Session:
        """The UTC day ``day`` as a session (every date is one)."""
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return Session(session_date=day, open=start, close=start + timedelta(days=1))

    def session_on(self, day: date) -> Session:
        return self.session_for_date(day)

    def session_date_of(self, ts: datetime) -> date:
        """The UTC date of ``ts``."""
        return require_utc(ts).date()

    def session_opening_at(self, ts: datetime) -> Session | None:
        """The session whose open (00:00 UTC) is exactly ``ts``."""
        session = self.session_for_date(self.session_date_of(ts))
        return session if session.open == require_utc(ts) else None

    def is_session_day(self, day: date) -> bool:
        del day
        return True

    def next_session(self, day: date) -> Session:
        return self.session_for_date(day + timedelta(days=1))

    def sessions_after(self, day: date, count: int = 1) -> Session:
        """The ``count``-th UTC day after ``day``."""
        if count < 1:
            raise ValueError("count must be at least 1")
        return self.session_for_date(day + timedelta(days=count))

    def session_open_before(self, ts: datetime, count: int) -> datetime:
        """The open of the ``count``-th UTC day, counting back, whose open is before ``ts``."""
        if count < 1:
            raise ValueError("count must be at least 1")
        ts = require_utc(ts)
        latest = datetime(ts.year, ts.month, ts.day, tzinfo=UTC)
        if latest == ts:  # a day opening exactly at ts is not before it
            latest -= timedelta(days=1)
        return latest - timedelta(days=count - 1)


class BrokerCfdCalendar:
    """A CFD broker's trading days for daily bars built from its own quotes (ADR-0052).

    A session is a weekday; the Saturday and Sunday hours (the Sunday-evening open) belong to
    Monday's session, as ``data/quote_bars.daily_from_hourly`` builds the bars. ``closed_days``
    are weekdays the market did not quote (holidays): the next session skips them, so a holiday
    does not reset a 200-day average. A study derives them from the instrument's own bars,
    counting only short runs as closures; a longer hole stays missing data.
    """

    name = "BROKER_CFD"

    def __init__(self, closed_days: frozenset[date] = frozenset()) -> None:
        self.closed_days = closed_days

    def session_date_of(self, ts: datetime) -> date:
        day = require_utc(ts).date()
        if day.weekday() >= 5:
            day += timedelta(days=7 - day.weekday())
        return day

    def is_session_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.closed_days

    def session_for_date(self, day: date) -> Session:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return Session(session_date=day, open=start, close=start + timedelta(days=1))

    def sessions_after(self, day: date, count: int = 1) -> Session:
        """The ``count``-th trading day after ``day``."""
        if count < 1:
            raise ValueError("count must be at least 1")
        found = 0
        while found < count:
            day += timedelta(days=1)
            if self.is_session_day(day):
                found += 1
        return self.session_for_date(day)


def closed_weekdays(days: Iterable[date], *, longest: int = 2) -> frozenset[date]:
    """Weekdays without a bar between the first and last of ``days``, in runs of at most
    ``longest`` weekdays: the market's holidays. Longer runs are missing data, not closures."""
    present = set(days)
    if not present:
        return frozenset()
    gaps: list[date] = []
    closed: set[date] = set()
    day, last = min(present), max(present)
    while day <= last:
        if day.weekday() < 5:
            if day in present:
                if len(gaps) <= longest:
                    closed.update(gaps)
                gaps = []
            else:
                gaps.append(day)
        day += timedelta(days=1)
    return frozenset(closed)


#: A calendar that daily-bar features and rules can count sessions on: XNYS regular sessions, UTC
#: days, or a CFD broker's weekdays.
SessionCalendar = ExchangeCalendar | Utc247Calendar | BrokerCfdCalendar


@lru_cache(maxsize=8)
def _calendar(code: str) -> xcals.ExchangeCalendar:
    return xcals.get_calendar(code, start="2015-01-01")


# --------------------------------------------------------------------------- settlement


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def federal_reserve_only_holidays(year: int) -> set[date]:
    """Fed holidays on which NYSE is normally open (Columbus Day, Veterans Day)."""
    holidays = {_nth_weekday(year, 10, 0, 2)}  # second Monday of October
    veterans = date(year, 11, 11)
    if veterans.weekday() == 6:  # Sunday -> observed Monday
        veterans += timedelta(days=1)
    if veterans.weekday() < 5:  # Saturday holidays are not observed by the Fed
        holidays.add(veterans)
    return holidays


def is_us_settlement_day(day: date, calendar: ExchangeCalendar | None = None) -> bool:
    cal = calendar or ExchangeCalendar("XNYS")
    return cal.is_session_day(day) and day not in federal_reserve_only_holidays(day.year)


def us_equity_settlement_date(
    trade_date: date, *, cycle_days: int = 1, calendar: ExchangeCalendar | None = None
) -> date:
    """T+``cycle_days`` on US settlement business days (T+1 since May 2024)."""
    cal = calendar or ExchangeCalendar("XNYS")
    day = trade_date
    remaining = cycle_days
    for _ in range(30):
        day += timedelta(days=1)
        if is_us_settlement_day(day, cal):
            remaining -= 1
            if remaining == 0:
                return day
    raise RuntimeError("settlement date search did not converge")
