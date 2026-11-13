"""Daily bars: one bar per XNYS regular session (ADR "Daily SIP bars for ETFs").

The shape every daily consumer (features, rules, research engine) relies on:

* **One bar per regular session.** ``start`` is the session open and ``end`` the session close,
  both UTC, taken from the exchange calendar, early closes included (architecture.md §8: session
  times come from the calendar, never from fixed offsets). A bar's ``minutes`` is its session's
  length - 390 on a regular day, 210 on a 13:00 early close - so ``end == start + minutes`` holds
  for every bar. Identify daily data by the dataset's ``timeframe``, never by
  ``minutes == 390``.
* **Availability.** Like every bar, a daily bar is usable from ``end + 1 s`` (instructions.md §5:
  features use only data available at decision time). A decision on a completed day is
  therefore filled at the next session's open by the ordinary next-bar-open fill model.
* **Source mapping (Alpaca).** A daily bar's ``t`` is 00:00 New York of the session date: "The
  (SIP) timestamp of the trade is truncated ... to the day (in New York) for daily bars" (Alpaca
  Market Data FAQ, https://docs.alpaca.markets/docs/market-data-faq). That is 05:00Z in winter
  and 04:00Z in summer; probes on 2026-11-09 returned ``2025-03-07T05:00:00Z`` and
  ``2025-03-10T04:00:00Z`` either side of the March 2025 clock change. A stamp that is not New
  York midnight is *misaligned*; a midnight stamp of a date without a session is *outside the
  session calendar*. Both are counted and excluded, never re-dated.
* **What the prices mean.** In Alpaca's condition table, extended-hours trades (conditions T and
  U) update only the *volume* of a daily bar. Open, high, low and close are regular-session
  prices: the open is the first trade with qualifying conditions (often, but not guaranteed to
  be, the opening auction), the close the last qualifying trade, normally the listing exchange's
  closing auction print reported just after the close (probe: SPY on 2025-03-10 closed at
  560.58 while its 15:59 minute bar closed at 560.75). Volume, trade count and VWAP also include
  pre-market (from 04:00 New York) and after-hours trades (to 20:00, four hours after any
  close), so they are final only four hours after the session close. Strategies that decide at
  the close and fill at the next open never trade on them before they are public; a rule that
  reads them at the close sees up to four hours of later trades and must say so.
* **Missing sessions are reported, never fabricated.** Alpaca emits no bar for a session without
  qualifying trades. Quality checks expect exactly one bar per session in the requested range.

**UTC-day bars (24/7 crypto markets, ADR-0042).** On the ``Utc247Calendar`` every date is a
session ``[00:00, 24:00)`` UTC, so a daily bar is ``start`` = 00:00 UTC, ``end`` = the next
00:00 UTC and ``minutes`` = 1440 on every day: a fixed interval. Such datasets are recorded as
fixed-interval requests (``bar_minutes: 1440``, ``calendar: UTC_24_7``, no ``timeframe``; the
``"1Day"`` timeframe stays the label of variable-length XNYS session bars), and the ordinary
fixed-interval checks - an epoch-aligned 1440-minute grid, one bar per expected interval,
missing days reported and never filled - are exactly the session checks below on that calendar
(``tests/test_crypto_daily_study.py`` proves they agree). Coinbase Exchange candles of
granularity 86400 s are stamped at their day's 00:00 UTC; a day without trades has no candle
and counts as missing. A UTC-day bar is usable from ``end + 1 s`` like every bar, so a decision
on a completed day fills at the next day's open; volume is final at the day's end (there is no
after-hours session). :func:`expected_session_opens` and :func:`classify_session_bar` accept
either calendar.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Final, Literal

from trader.data.calendars import NEW_YORK, ExchangeCalendar, Session, SessionCalendar
from trader.data.history import HistoryBar
from trader.data.manifest import DAILY_BAR_MINUTES, DAILY_TIMEFRAME
from trader.domain.types import iso_utc, require_utc

__all__ = [
    "DAILY_BAR_MINUTES",
    "DAILY_FINALIZATION",
    "DAILY_TIMEFRAME",
    "EXTENDED_HOURS_AFTER_CLOSE",
    "SOURCE_DAY_MINUTES",
    "SessionBarVerdict",
    "classify_session_bar",
    "expected_session_opens",
    "session_of_source_day",
    "session_ranges",
    "session_slot",
    "source_day_stamp",
]

#: A source-day row that does not map to a session keeps this nominal length (it is excluded).
SOURCE_DAY_MINUTES: Final = 1440
#: After-hours trading ends four hours after the close (20:00 New York, 17:00 on early closes).
EXTENDED_HOURS_AFTER_CLOSE: Final = timedelta(hours=4)
#: A daily bar is complete (its volume final) only after the after-hours session has ended.
DAILY_FINALIZATION: Final = EXTENDED_HOURS_AFTER_CLOSE + timedelta(seconds=1)

SessionBarVerdict = Literal["OK", "MISALIGNED", "OUTSIDE_SESSION"]


def source_day_stamp(day: date) -> datetime:
    """00:00 New York of ``day`` in UTC: the timestamp Alpaca gives that day's bar."""
    return require_utc(datetime.combine(day, time(0), tzinfo=NEW_YORK))


def session_of_source_day(calendar: ExchangeCalendar, stamp: datetime) -> Session | None:
    """The session a source-day timestamp labels, or ``None`` when it labels none."""
    local = require_utc(stamp).astimezone(NEW_YORK)
    if local.time() != time(0):
        return None
    return calendar.session_for_date(local.date())


def session_slot(calendar: ExchangeCalendar, stamp: datetime) -> tuple[datetime, int]:
    """``(start, minutes)`` of the session bar a source-day timestamp labels.

    A timestamp that labels no session keeps its own value and a nominal one-day length, so the
    quality check counts and excludes the row instead of re-dating it.
    """
    session = session_of_source_day(calendar, stamp)
    if session is None:
        return require_utc(stamp), SOURCE_DAY_MINUTES
    return session.open, session.minutes


def expected_session_opens(
    calendar: SessionCalendar, start: datetime, end: datetime
) -> tuple[list[datetime], dict[str, list[datetime]]]:
    """Opens of the sessions with ``start <= open < end``, also keyed by session date (on the
    UTC-day calendar: every 00:00 UTC in the range)."""
    start, end = require_utc(start), require_utc(end)
    opens: list[datetime] = []
    by_session: dict[str, list[datetime]] = {}
    day = calendar.session_date_of(start)
    last_day = calendar.session_date_of(end)
    while day <= last_day:
        session = calendar.session_for_date(day)
        if session is not None and start <= session.open < end:
            opens.append(session.open)
            by_session[day.isoformat()] = [session.open]
        day += timedelta(days=1)
    return opens, by_session


def classify_session_bar(bar: HistoryBar, calendar: SessionCalendar) -> SessionBarVerdict:
    """Whether ``bar`` is exactly one regular session ``[open, close)`` of ``calendar`` (on the
    UTC-day calendar: one whole UTC day, ``[00:00, 24:00)``, 1440 minutes)."""
    session = calendar.session_for_date(calendar.session_date_of(bar.start))
    if session is None:
        return "OUTSIDE_SESSION"
    if bar.start != session.open or bar.minutes != session.minutes:
        return "MISALIGNED"
    return "OK"


def session_ranges(missing: Sequence[datetime], expected: Sequence[datetime]) -> list[str]:
    """Runs of consecutive missing sessions, as ``"<open> +N sessions"``."""
    if not missing:
        return []
    position = {t: i for i, t in enumerate(sorted(expected))}
    ordered = sorted(missing, key=lambda t: position[t])
    out: list[str] = []
    run_start = prev = ordered[0]
    count = 1
    for t in ordered[1:]:
        if position[t] == position[prev] + 1:
            prev = t
            count += 1
            continue
        out.append(f"{iso_utc(run_start)} +{count} sessions")
        run_start = prev = t
        count = 1
    out.append(f"{iso_utc(run_start)} +{count} sessions")
    return out
