"""Deterministic replay loop (architecture.md §6 decision cycle, instructions.md §7).

For every recorded event, in receipt order:

1. advance the replay clock to the event's receipt time;
2. update the market view and let the simulated broker match working orders;
3. run the shared decision cycle (``runtime/decision_loop.py``): apply broker events, mark
   positions and evaluate loss windows, evaluate exits, cancel expired entry remainders, and -
   on a quote - finalise bars that have become *available*, generate candidates from completed
   bars only, score them and pass them to the shared allocator; finally resolve unknown
   outcomes by reconciliation (never by resubmission).

Paper mode runs the same cycle (``runtime/paper.py``); only steps 1 and 2 are replay-specific.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from sqlalchemy import insert

from trader.data.calendars import ExchangeCalendar
from trader.domain.hashing import canonical_json
from trader.domain.market import AnyMarketEvent, Bar, Quote, TradingStatus
from trader.runtime.decision_loop import (
    HEALTH_RECONCILE_SECONDS,
    DecisionLoop,
    StepStats,
    no_bars,
)
from trader.runtime.environment import ReplayEnvironment
from trader.storage.helpers import insert_ignore
from trader.storage.schema import events as events_table


class ReplayRunner(DecisionLoop):
    env: ReplayEnvironment

    def __init__(self, env: ReplayEnvironment) -> None:
        super().__init__(env)
        self.env = env

    # ------------------------------------------------------------------ lifecycle
    def journal(self, events: Iterable[AnyMarketEvent]) -> int:
        """Persist the raw event journal once (idempotent)."""
        count = 0
        with self.env.db.tx() as conn:
            for ev in events:
                inserted = insert_ignore(
                    conn,
                    events_table,
                    {
                        "feed": ev.feed,
                        "instrument_id": ev.instrument_id,
                        "kind": ev.kind,
                        "sequence": ev.sequence,
                        "correction_version": ev.correction_version,
                        "event_time": ev.event_time,
                        "receipt_time": ev.receipt_time,
                        "payload_json": canonical_json(ev),
                    },
                    ["feed", "instrument_id", "sequence", "correction_version"],
                )
                count += int(inserted)
        return count

    def run(self, events: Iterable[AnyMarketEvent], *, stop_before: datetime | None = None) -> int:
        processed = 0
        for event in events:
            if stop_before is not None and event.receipt_time >= stop_before:
                break
            self.step(event)
            processed += 1
        return processed

    def finish(self, close_time: datetime) -> None:
        env = self.env
        if close_time > env.clock.now():
            env.clock.advance_to(close_time)
        env.broker.end_of_session(close_time)
        self.pump()
        env.reconciler.reconcile(self.account_id, cleared_by="end-of-session")

    # ------------------------------------------------------------------ one event
    def step(self, event: AnyMarketEvent) -> None:
        env = self.env
        env.clock.advance_to(event.receipt_time)
        env.market.on_event(event)
        env.broker.on_market_event(event)
        bars: Callable[[], Iterable[Bar]] = no_bars
        if isinstance(event, Quote):
            instrument_id = event.instrument_id

            def finalized() -> list[Bar]:
                # Decisions for an instrument are taken on its own fresh quote.
                return env.market.finalize(env.clock.now(), instrument_id)

            bars = finalized
        self.cycle(market_event=isinstance(event, Quote | TradingStatus), bars=bars)


def session_close_for(env: ReplayEnvironment, session_date: str) -> datetime:
    from datetime import date

    cal = ExchangeCalendar("XNYS")
    session = cal.session_for_date(date.fromisoformat(session_date))
    if session is None:
        raise ValueError(f"{session_date} is not an NYSE session")
    return session.close


__all__ = [
    "HEALTH_RECONCILE_SECONDS",
    "ReplayRunner",
    "StepStats",
    "insert",
    "session_close_for",
]
