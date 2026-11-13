"""Adapters that let runtime strategies run unchanged inside the research engine.

:class:`SessionWindowAdapter` feeds the *runtime* Candidate A (``strategies/candidate_a.py``)
with the current session's completed bars exactly as the replay runner does, so stock research
and the runtime share one implementation (feature parity by construction; see
``tests/test_research_parity.py``). Unlike the streaming rules, Candidate A recomputes over the
session window, so a window with a missing interval is still evaluated - and any resulting
candidate is rejected as ``STALE_DATA`` by the engine.

:meth:`SessionWindowAdapter.features` exposes the ``momentum-v1`` snapshot behind the most recent
candidate of an instrument, like the research rules' accessor, for labels and filters.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from trader.data.calendars import ExchangeCalendar
from trader.data.quality import is_contiguous
from trader.domain.candidates import TradeCandidate
from trader.domain.market import Bar
from trader.features.momentum import FEATURE_VERSION as MOMENTUM_VERSION
from trader.strategies.candidate_a import CandidateA


class SessionWindowAdapter:
    uses_source_vwap = True

    def __init__(self, rule: CandidateA, calendar: ExchangeCalendar) -> None:
        self.rule = rule
        self.calendar = calendar
        self._bars: dict[str, list[Bar]] = {}
        self._session: dict[str, date] = {}

    @property
    def strategy_id(self) -> str:
        return self.rule.spec.strategy_id

    @property
    def spec_hash(self) -> str:
        return self.rule.spec.spec_hash

    @property
    def snapshot_version(self) -> str:
        """Schema of the snapshots behind Candidate A's ``snapshot_hash`` values."""
        return MOMENTUM_VERSION

    def features(self, instrument_id: str) -> Mapping[str, Decimal]:
        """The snapshot behind the instrument's most recent candidate."""
        return self.rule.features(instrument_id)

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        session = self.calendar.session_at(bar.start)
        if session is None:
            return None
        if self._session.get(bar.instrument_id) != session.session_date:
            self._session[bar.instrument_id] = session.session_date
            self._bars[bar.instrument_id] = []
        bars = self._bars[bar.instrument_id]
        bars.append(bar)
        return self.rule.on_bar(bars, now)

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        del divisor
        self._bars[instrument_id] = []  # splits take effect at a session open: nothing to keep

    def data_ok(self, instrument_id: str) -> bool:
        bars = self._bars.get(instrument_id, [])
        minutes = bars[-1].minutes if bars else 1
        return is_contiguous([b.start for b in bars], minutes)

    def gaps(self) -> int:
        return 0
