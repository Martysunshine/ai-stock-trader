"""CFD research simulator on the broker's own quotes (ADR-0052).

A sibling of :mod:`trader.research.engine`, not a mode of it: that engine is long only, pays each
position in full from cash, charges no overnight financing and assumes one constant spread, and
its result hashes are pinned for the registered trials. This one replays two-sided bars
(:class:`~trader.data.quote_bars.QuoteBar`) through a rule and the same sizer, fee schedules and
instrument specifications as the runtime. Its records reuse the shared structures
(:class:`CandidateOutcome`, :class:`SessionStats` and the trade dictionary that
``study.summarize_series`` and ``study.walk_forward`` read), so the study's statistics, walk
forward and gate apply unchanged.

Execution model (all of it declared, none of it tuned):

* **Bars and decisions.** Bars of all instruments are processed in order of their start; bars
  that start together form one step. In each step, in this order: overnight financing due by the
  step's start is charged; reducing exits at the open; next-open entries; what happened inside
  the bars (stops and targets); then the rule's decisions at the bars' close. A decision is only
  ever acted on at the instrument's next bar.
* **Entry** is a market order at the next bar's open: a long buys at the ask, a short sells at
  the bid, each moved by the slippage and rounded to the tick against the trader. It is sized at
  that price by :func:`trader.risk.sizing.size_order` (planned loss at the rule's stop, notional
  caps, margin as a cash check only); a size the sleeve's limit cannot carry is recorded as not
  taken, and the stop is never moved to make a contract fit. An open already beyond the stop
  refuses the entry (the broker refuses such a stop). The spread at the open is checked against
  the policy's ``max_entry_spread_bps``.
* **Exits.** At a bar's open: a scheduled signal exit, the time exit (first open at or after the
  deadline: ``max_hold_minutes`` after the entry, or the signal's own fixed ``deadline``, a day
  trade's flat time, at or after which an entry is not taken) and a gap through the stop, each at the open on the side that would fill. Inside a
  bar: a long's stop when the bid's low reaches it, a short's when the ask's high does; a target
  (when the rule uses it as an exit) only when the filling side trades strictly through it. When
  both are touched in one bar the stop is assumed first. A position still open at the end of the
  run is closed at its last bar's close.
* **Financing** is charged once per charge instant (the spec's ``charge_time_utc``) that a
  position is held through, on the notional at entry: every night for a market open every day,
  Monday to Friday nights for one closed at the weekend with the Friday night counted
  ``weekend_multiplier_days`` times. A position closed inside a bar is charged up to that bar's
  end. A negative rate is a credit.
* **Equity** is cash (start, realised results, fees and financing as they are charged) plus the
  open positions valued at the side that would close them. Each window starts from ``capital``;
  sizing uses current equity, so results compound within a window.
* **Pauses.** At or below the policy's capital floor (scaled to ``capital``), or after a drawdown
  of ``max_high_water_drawdown_fraction`` from the window's high, no new entry is taken for the
  rest of the window. Open positions keep their exits.
* **Costs** are applied by transforming the quotes before the run (:func:`apply_costs`): one
  half-spread per bar, the larger of the quoted one and a registered floor, times a multiplier;
  or the mid on both sides (the diagnostic view).
* **Signal filter** (optional; trial T-0026, ``research/cfd_filter.py``). A filter may only
  refuse a signal. It is asked once per signal, at the entry step, right after the window check
  and before the pause, the stress view's missed entry, the flat time, the portfolio limits and
  sizing, so it sees every in-window signal whatever the portfolio state and its decisions do not
  depend on its earlier refusals. It is handed the frozen :class:`CfdSignal` and returns a
  :class:`~trader.research.filters.FilterDecision`; it has no channel to add a signal or change
  its size, stop, target or timing. A refusal is a ``REJECTED`` candidate with reason
  ``NO_EDGE`` and binding ``filter:<filter_id>``; an accepted signal continues through exactly
  the checks and sizing it meets without a filter. Every decision is logged in
  ``CfdEngineResult.filter_log`` with its own hash; ``result_hash`` does not include the log.
  Without a filter (the default) nothing of this runs and every output is as before.
* **Candidates for labels** (optional, ``record_candidates``): the engine keeps every in-window
  signal it was handed, and :func:`emit_candidates` joins them, for an unfiltered run, with
  their outcome, the features at their decision time and the realised trade when one was made.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal, Protocol

from trader.accounting.fees import FeeSchedule
from trader.config.policy import RiskPolicy
from trader.data.history import HistoryBar
from trader.data.quote_bars import QuoteBar
from trader.domain.candidates import RejectReason, ReturnBasis
from trader.domain.hashing import content_hash
from trader.domain.instruments import CommodityInstrumentSpec
from trader.domain.types import BPS, ZERO, Direction, Side, canon_decimal, iso_utc
from trader.research.engine import CandidateOutcome, SessionStats
from trader.research.filters import FilterDecision, FilterRecord
from trader.risk.sizing import ExposureState, SizingRequest, size_order

ONE = Decimal(1)
TWO = Decimal(2)
TargetRole = Literal["EXIT", "COST_GATE"]


# ----------------------------------------------------------------------------- rules


@dataclass(frozen=True, slots=True)
class CfdSignal:
    """A rule's decision at a bar's close; acted on at the instrument's next bar."""

    signal_id: str
    instrument_id: str
    direction: Direction
    decided_at: datetime
    #: The decision bar's mid close (the level the stop and target were measured from).
    reference: Decimal
    stop: Decimal
    target: Decimal
    #: ``EXIT``: the target is a resting exit. ``COST_GATE``: it only feeds the sizer's
    #: cost-coverage gate, for rules that take profit by a signal at the close.
    target_role: TargetRole
    max_hold_minutes: int
    #: Same-time signals are taken in order of priority, highest first, then instrument id.
    priority: Decimal = ZERO
    #: A fixed time by which the trade is closed (a day trade's flat time). ``None``: the trade
    #: is closed ``max_hold_minutes`` after its entry. An entry at or after it is not taken.
    deadline: datetime | None = None


class CfdRule(Protocol):
    @property
    def strategy_id(self) -> str: ...

    @property
    def spec_hash(self) -> str: ...

    def on_bar(self, instrument_id: str, bar: QuoteBar) -> CfdSignal | None: ...

    def exit_signal(
        self, instrument_id: str, bar: QuoteBar, direction: Direction
    ) -> str | None: ...


class CfdSignalFilter(Protocol):
    """A veto on a rule's signals (module docstring, "Signal filter"). It sees the frozen signal
    only and answers accept or refuse; whatever it needs beyond the signal (features) it computes
    itself from data that ended at or before ``signal.decided_at``."""

    @property
    def filter_id(self) -> str: ...

    def decide(self, signal: CfdSignal) -> FilterDecision: ...


# ----------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class CfdCosts:
    """One cost view. ``spread_floor_bps`` is a full spread per instrument, in basis points of the
    mid, below which a quoted spread is not believed (old bars that carry none)."""

    label: str
    slippage_bps: Decimal
    spread_multiplier: Decimal = ONE
    spread_floor_bps: Mapping[str, Decimal] = field(default_factory=dict)
    mid_fills: bool = False
    financing: bool = True
    financing_charge_multiplier: Decimal = ONE
    financing_credits: bool = True
    missed_entry_percent: int = 0


@dataclass(frozen=True, slots=True)
class CfdInstrument:
    spec: CommodityInstrumentSpec
    bars: tuple[QuoteBar, ...]
    #: The key of ``CfdEngineConfig.fx_bars`` that converts the quote currency to USD (USD per
    #: unit); ``None`` for an instrument quoted in USD.
    fx_pair: str | None = None


@dataclass(frozen=True, slots=True)
class CfdEngineConfig:
    policy: RiskPolicy
    fees: FeeSchedule
    instruments: Mapping[str, CfdInstrument]
    costs: CfdCosts
    start: datetime
    end: datetime
    capital: Decimal = Decimal(100)
    fx_bars: Mapping[str, tuple[QuoteBar, ...]] = field(default_factory=dict)


_FIELDS = ("open", "high", "low", "close")


def apply_costs(bars: Sequence[QuoteBar], costs: CfdCosts, floor_bps: Decimal) -> list[QuoteBar]:
    """The quotes one view trades on. Each bar gets one half-spread around its mid prices: the
    wider of the quoted half-spreads at its open and its close (the two quotes that were
    simultaneous; a bar's bid high and ask high can come from different moments), at least half
    of ``floor_bps`` of the mid close, times ``spread_multiplier``; zero for ``mid_fills``. One
    spread per bar keeps every side a valid bar."""
    out: list[QuoteBar] = []
    for bar in bars:
        mids = {f: (getattr(bar.bid, f) + getattr(bar.ask, f)) / TWO for f in _FIELDS}
        if costs.mid_fills:
            half = ZERO
        else:
            quoted = max(
                (getattr(bar.ask, f) - getattr(bar.bid, f)) / TWO for f in ("open", "close")
            )
            floor = mids["close"] * floor_bps * BPS / TWO
            half = max(quoted, floor) * costs.spread_multiplier
        out.append(QuoteBar(_side(bar.bid, mids, -half), _side(bar.ask, mids, half)))
    return out


def _side(bar: HistoryBar, mids: Mapping[str, Decimal], offset: Decimal) -> HistoryBar:
    return HistoryBar(
        start=bar.start,
        minutes=bar.minutes,
        open=mids["open"] + offset,
        high=mids["high"] + offset,
        low=mids["low"] + offset,
        close=mids["close"] + offset,
        volume=bar.volume,
    )


# ----------------------------------------------------------------------------- records


@dataclass(slots=True)
class CfdTrade:
    trade_id: str
    signal_id: str
    instrument_id: str
    direction: Direction
    quantity: Decimal
    entry_time: datetime
    entry_price: Decimal
    entry_mid: Decimal
    entry_fee_usd: Decimal
    fx_entry: Decimal
    stop: Decimal
    target: Decimal
    target_exit: bool
    deadline: datetime
    planned_loss_usd: Decimal
    notional_usd: Decimal
    margin_usd: Decimal
    entry_session: str
    exit_time: datetime | None = None
    exit_price: Decimal | None = None
    exit_mid: Decimal | None = None
    exit_fee_usd: Decimal = ZERO
    fx_exit: Decimal | None = None
    exit_reason: str | None = None
    financing_usd: Decimal = ZERO
    nights_charged: int = 0
    charged_until: datetime | None = None
    scheduled_exit: str | None = None
    ambiguous_bar: bool = False
    stopped_in_entry_bar: bool = False

    @property
    def sign(self) -> Decimal:
        return ONE if self.direction is Direction.LONG else -ONE

    @property
    def closed(self) -> bool:
        return self.exit_time is not None

    def unrealised_usd(self, close_side_price: Decimal, fx: Decimal) -> Decimal:
        return self.sign * (close_side_price - self.entry_price) * self.quantity * fx

    def gross_pnl(self) -> Decimal:
        if self.exit_price is None or self.fx_exit is None:
            return ZERO
        return self.unrealised_usd(self.exit_price, self.fx_exit)

    def fees(self) -> Decimal:
        return self.entry_fee_usd + self.exit_fee_usd

    def net_pnl(self) -> Decimal:
        return self.gross_pnl() - self.fees() - self.financing_usd

    def execution_cost(self) -> Decimal:
        """Spread and slippage paid against the mid, in USD (already inside the prices)."""
        cost = abs(self.entry_price - self.entry_mid) * self.quantity * self.fx_entry
        if self.exit_price is not None and self.exit_mid is not None and self.fx_exit:
            cost += abs(self.exit_mid - self.exit_price) * self.quantity * self.fx_exit
        return cost

    def as_dict(self) -> dict[str, object]:
        entry_side = Side.BUY if self.direction is Direction.LONG else Side.SELL
        exit_side = Side.SELL if entry_side is Side.BUY else Side.BUY
        exits: list[dict[str, object]] = []
        if self.exit_time is not None:
            exits.append(
                {
                    "time": iso_utc(self.exit_time),
                    "side": exit_side.value,
                    "quantity": self.quantity,
                    "price": self.exit_price,
                    "fee": self.exit_fee_usd,
                    "reason": self.exit_reason,
                    "mid_reference": self.exit_mid,
                }
            )
        return {
            "trade_id": self.trade_id,
            "candidate_id": self.signal_id,
            "instrument_id": self.instrument_id,
            "direction": self.direction.value,
            "entry": {
                "time": iso_utc(self.entry_time),
                "side": entry_side.value,
                "quantity": self.quantity,
                "price": self.entry_price,
                "fee": self.entry_fee_usd,
                "reason": "ENTRY",
                "mid_reference": self.entry_mid,
            },
            "exits": exits,
            "exit_reason": self.exit_reason or "OPEN",
            "stop": self.stop,
            "target": self.target,
            "quantity": self.quantity,
            "notional_usd": self.notional_usd,
            "planned_loss_usd": self.planned_loss_usd,
            "gross_pnl": self.gross_pnl(),
            "fees": self.fees(),
            "financing_usd": self.financing_usd,
            "nights_charged": self.nights_charged,
            "net_pnl": self.net_pnl(),
            "execution_cost": self.execution_cost(),
            "dust": ZERO,
            "ambiguous_bar": self.ambiguous_bar,
            "stopped_in_entry_bar": self.stopped_in_entry_bar,
            "entry_session": self.entry_session,
            "holding_minutes": (
                int((self.exit_time - self.entry_time).total_seconds() // 60)
                if self.exit_time
                else None
            ),
            "fx_entry": self.fx_entry,
            "fx_exit": self.fx_exit,
        }


@dataclass(frozen=True, slots=True)
class CfdEngineResult:
    trades: tuple[CfdTrade, ...]
    candidates: tuple[CandidateOutcome, ...]
    sessions: tuple[SessionStats, ...]
    pauses: tuple[dict[str, str], ...]
    final_equity: Decimal
    result_hash: str
    diagnostics: dict[str, object]
    #: Every decision of the signal filter, in order; empty without a filter.
    filter_log: tuple[FilterRecord, ...] = ()
    #: Hash of ``filter_log``; ``""`` without a filter. Not part of ``result_hash``.
    filter_hash: str = ""
    #: Every in-window signal handed to the entry step (only with ``record_candidates``).
    signals: tuple[CfdSignal, ...] = ()


@dataclass(frozen=True, slots=True)
class CfdCandidate:
    """One in-window signal of an unfiltered run, for labels: the signal, the features at its
    decision time, what the engine did with it, and the realised trade when it was taken."""

    signal: CfdSignal
    features: Mapping[str, float]
    status: str
    reasons: tuple[str, ...]
    binding: str
    trade: CfdTrade | None


def emit_candidates(
    result: CfdEngineResult, feature_fn: Callable[[CfdSignal], Mapping[str, float]]
) -> tuple[CfdCandidate, ...]:
    """Every in-window signal of an unfiltered run with ``record_candidates``, in decision order,
    with ``feature_fn``'s features and its realised trade (``None`` when it was not taken).
    ``feature_fn`` must read only data that ended at or before the signal's decision time; the
    trade is what happened after it and is only ever a label."""
    if result.filter_log:
        raise ValueError("candidates for labels come from an unfiltered run")
    outcomes = {o.candidate_id: o for o in result.candidates}
    trades = {t.signal_id: t for t in result.trades}
    out: list[CfdCandidate] = []
    for signal in sorted(result.signals, key=lambda s: (s.decided_at, s.signal_id)):
        outcome = outcomes.get(signal.signal_id)
        if outcome is None:
            raise ValueError(f"{signal.signal_id} has no recorded outcome")
        out.append(
            CfdCandidate(
                signal=signal,
                features=dict(feature_fn(signal)),
                status=outcome.status,
                reasons=outcome.reasons,
                binding=outcome.binding,
                trade=trades.get(signal.signal_id),
            )
        )
    return tuple(out)


# ----------------------------------------------------------------------------- helpers


def _down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _up(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def session_of(bar: QuoteBar) -> str:
    """A bar's session: the UTC day its last minute falls on."""
    return (bar.end - timedelta(seconds=1)).date().isoformat()


def charge_instants(
    spec: CommodityInstrumentSpec, after: datetime, until: datetime
) -> list[tuple[datetime, int]]:
    """The financing instants in ``(after, until]`` with how many nights each one charges."""
    terms = spec.financing
    if terms is None or until <= after:
        return []
    hh, mm = (int(x) for x in terms.charge_time_utc.split(":"))
    every_day = terms.weekend_multiplier_days == 1
    out: list[tuple[datetime, int]] = []
    day = after.date()
    while True:
        instant = datetime.combine(day, time(hh, mm), tzinfo=UTC)
        if instant > until:
            return out
        if instant > after:
            weekday = day.weekday()
            if every_day or weekday < 4:
                out.append((instant, 1))
            elif weekday == 4:
                out.append((instant, terms.weekend_multiplier_days))
        day += timedelta(days=1)


# ----------------------------------------------------------------------------- engine


class CfdEngine:
    def __init__(
        self,
        config: CfdEngineConfig,
        rule: CfdRule,
        signal_filter: CfdSignalFilter | None = None,
        *,
        record_candidates: bool = False,
    ) -> None:
        self.c = config
        self.rule = rule
        self.signal_filter = signal_filter
        self.filter_log: list[FilterRecord] = []
        self.record_candidates = record_candidates
        self.seen: list[CfdSignal] = []
        policy = config.policy
        self.policy = policy
        self.slip = config.costs.slippage_bps * BPS
        self.cash = config.capital
        self.equity = config.capital
        self.high_water = config.capital
        scale = config.capital / policy.initial_trading_capital
        self.floor = policy.loss_limits.initial_capital_pause_floor * scale
        self.paused: str | None = None
        self.pauses: list[dict[str, str]] = []
        self.open: dict[str, CfdTrade] = {}
        self.trades: list[CfdTrade] = []
        self.pending: dict[str, CfdSignal] = {}
        self.outcomes: list[CandidateOutcome] = []
        self.sessions: dict[str, SessionStats] = {}
        self.last_bar: dict[str, QuoteBar] = {}
        self.fx: dict[str, dict[datetime, QuoteBar]] = {
            k: {b.start: b for b in v} for k, v in config.fx_bars.items()
        }
        self.bars: dict[str, list[QuoteBar]] = {
            iid: apply_costs(
                inst.bars,
                config.costs,
                config.costs.spread_floor_bps.get(iid, ZERO),
            )
            for iid, inst in config.instruments.items()
        }
        self.diag: dict[str, int] = {
            "signals": 0,
            "signals_outside_window": 0,
            "missed_entries": 0,
            "open_at_end_closed": 0,
        }

    # ------------------------------------------------------------------ run
    def run(self) -> CfdEngineResult:
        steps: dict[datetime, list[tuple[str, QuoteBar]]] = {}
        for iid, bars in self.bars.items():
            for bar in bars:
                steps.setdefault(bar.start, []).append((iid, bar))
        for t in sorted(steps):
            if t >= self.c.end:
                break
            group = sorted(steps[t], key=lambda x: x[0])
            self._charge_until(t)
            for iid, bar in group:
                self._exits_at_open(iid, bar)
            self._fill(t, group)
            for iid, bar in group:
                self._inside(iid, bar)
            for iid, bar in group:
                self._decide(iid, bar)
            if t >= self.c.start:
                self._mark(group)
        self._close_at_end()
        return self._result()

    # ------------------------------------------------------------------ financing
    def _charge_until(self, until: datetime) -> None:
        for trade in self.open.values():
            self._charge(trade, until)

    def _charge(self, trade: CfdTrade, until: datetime) -> None:
        spec = self.c.instruments[trade.instrument_id].spec
        after = trade.charged_until or trade.entry_time
        costs = self.c.costs
        if until <= after or spec.financing is None:
            return
        trade.charged_until = until
        if not costs.financing:
            return
        rate = (
            spec.financing.long_daily_charge_fraction
            if trade.direction is Direction.LONG
            else spec.financing.short_daily_charge_fraction
        )
        if rate > 0:
            rate *= costs.financing_charge_multiplier
        elif not costs.financing_credits:
            rate = ZERO
        for _instant, nights in charge_instants(spec, after, until):
            amount = rate * trade.notional_usd * nights
            trade.financing_usd += amount
            trade.nights_charged += nights
            self.cash -= amount

    # ------------------------------------------------------------------ exits
    def _close(
        self,
        trade: CfdTrade,
        bar: QuoteBar,
        *,
        price: Decimal,
        mid: Decimal,
        when: datetime,
        reason: str,
        fx: Decimal,
    ) -> None:
        self._charge(trade, when)
        exit_side = Side.SELL if trade.direction is Direction.LONG else Side.BUY
        fee = self.c.fees.fee(exit_side, trade.quantity, trade.quantity * price) * fx
        trade.exit_time, trade.exit_price, trade.exit_mid = when, price, mid
        trade.exit_fee_usd, trade.fx_exit, trade.exit_reason = fee, fx, reason
        self.cash += trade.gross_pnl() - fee
        del self.open[trade.instrument_id]
        session = self._session(session_of(bar))
        session.fees += fee
        session.realized_net += trade.net_pnl()
        session.trades_closed += 1
        session.turnover_usd += trade.quantity * price * fx

    def _exit_price(self, trade: CfdTrade, bid: Decimal, ask: Decimal) -> Decimal:
        tick = self.c.instruments[trade.instrument_id].spec.price_tick
        if trade.direction is Direction.LONG:
            return _down(bid * (ONE - self.slip), tick)
        return _up(ask * (ONE + self.slip), tick)

    def _exits_at_open(self, iid: str, bar: QuoteBar) -> None:
        trade = self.open.get(iid)
        if trade is None:
            return
        long = trade.direction is Direction.LONG
        reason: str | None = trade.scheduled_exit
        if reason is None and trade.deadline <= bar.start:
            reason = "TIME_EXIT"
        if reason is None and (
            (long and bar.bid.open <= trade.stop) or (not long and bar.ask.open >= trade.stop)
        ):
            reason = "STOP_GAP"
        if reason is None:
            return
        self._close(
            trade,
            bar,
            price=self._exit_price(trade, bar.bid.open, bar.ask.open),
            mid=bar.mid_open,
            when=bar.start,
            reason=reason,
            fx=self._fx(iid, bar, "open") or trade.fx_entry,
        )

    def _inside(self, iid: str, bar: QuoteBar) -> None:
        trade = self.open.get(iid)
        if trade is None:
            return
        tick = self.c.instruments[iid].spec.price_tick
        if trade.direction is Direction.LONG:
            stop_hit = bar.bid.low <= trade.stop
            target_hit = trade.target_exit and bar.bid.high > trade.target
            stop_price = _down(trade.stop * (ONE - self.slip), tick)
        else:
            stop_hit = bar.ask.high >= trade.stop
            target_hit = trade.target_exit and bar.ask.low < trade.target
            stop_price = _up(trade.stop * (ONE + self.slip), tick)
        if not stop_hit and not target_hit:
            return
        fx = self._fx(iid, bar, "close") or trade.fx_entry
        if stop_hit:
            trade.ambiguous_bar = bool(target_hit)
            trade.stopped_in_entry_bar = trade.entry_time == bar.start
            self._close(
                trade, bar, price=stop_price, mid=trade.stop, when=bar.end, reason="STOP", fx=fx
            )
            return
        self._close(
            trade, bar, price=trade.target, mid=trade.target, when=bar.end, reason="TARGET", fx=fx
        )

    def _close_at_end(self) -> None:
        for iid, trade in list(self.open.items()):
            bar = self.last_bar[iid]
            self._close(
                trade,
                bar,
                price=self._exit_price(trade, bar.bid.close, bar.ask.close),
                mid=bar.mid_close,
                when=bar.end,
                reason="END_OF_DATA",
                fx=self._fx(iid, bar, "close") or trade.fx_entry,
            )
            self.diag["open_at_end_closed"] += 1
        self.equity = self.cash
        last = max(self.last_bar.values(), key=lambda b: b.end, default=None)
        if last is not None and last.start >= self.c.start:
            self._session(session_of(last)).end_equity = self.equity

    # ------------------------------------------------------------------ decisions
    def _decide(self, iid: str, bar: QuoteBar) -> None:
        self.last_bar[iid] = bar
        signal = self.rule.on_bar(iid, bar)
        trade = self.open.get(iid)
        if trade is not None and trade.scheduled_exit is None:
            reason = self.rule.exit_signal(iid, bar, trade.direction)
            if reason is not None:
                trade.scheduled_exit = f"SIGNAL:{reason}"
        if signal is not None:
            self.diag["signals"] += 1
            self.pending[iid] = signal

    # ------------------------------------------------------------------ entries
    def _fill(self, t: datetime, group: Sequence[tuple[str, QuoteBar]]) -> None:
        due = [(self.pending.pop(iid), bar) for iid, bar in group if iid in self.pending]
        due.sort(key=lambda x: (-x[0].priority, x[0].instrument_id))
        for signal, bar in due:
            self._enter(t, signal, bar)

    def _reject(
        self,
        signal: CfdSignal,
        status: Literal["UNFILLED", "REJECTED"],
        reason: str,
        binding: str,
        *,
        planned_loss: Decimal = ZERO,
    ) -> None:
        self.outcomes.append(
            CandidateOutcome(
                candidate_id=signal.signal_id,
                instrument_id=signal.instrument_id,
                decision_time=signal.decided_at,
                status=status,
                reasons=(reason,),
                quantity=ZERO,
                notional_usd=ZERO,
                planned_loss_usd=planned_loss,
                expected_net_usd=ZERO,
                binding=binding,
            )
        )

    def _enter(self, t: datetime, signal: CfdSignal, bar: QuoteBar) -> None:
        c, policy = self.c, self.policy
        iid = signal.instrument_id
        inst = c.instruments[iid]
        spec = inst.spec
        if not c.start <= t < c.end:
            self.diag["signals_outside_window"] += 1
            return
        if self.record_candidates:
            self.seen.append(signal)
        if self.signal_filter is not None and not self._accepted(signal):
            return self._reject(
                signal,
                "REJECTED",
                RejectReason.NO_EDGE.value,
                f"filter:{self.signal_filter.filter_id}",
            )
        session_key = session_of(bar)
        session = self._session(session_key)
        if self.paused is not None:
            return self._reject(signal, "REJECTED", RejectReason.PAUSED.value, self.paused)
        if c.costs.missed_entry_percent > 0:
            digest = hashlib.sha256(f"{signal.signal_id}|{c.costs.label}".encode()).digest()
            if int.from_bytes(digest[:8], "big") % 100 < c.costs.missed_entry_percent:
                self.diag["missed_entries"] += 1
                return self._reject(signal, "UNFILLED", "MISSED_ENTRY", "stress")
        if signal.deadline is not None and t >= signal.deadline:
            return self._reject(
                signal, "REJECTED", RejectReason.NO_EXIT_PLAN.value, "open_after_flat_time"
            )
        if iid in self.open:
            return self._reject(signal, "REJECTED", RejectReason.RISK_LIMIT.value, "busy")
        if len(self.open) >= policy.exposure.max_open_positions:
            return self._reject(
                signal, "REJECTED", RejectReason.RISK_LIMIT.value, "max_open_positions"
            )
        tags = set(spec.exposure_tags)
        for other in self.open.values():
            if tags & set(c.instruments[other.instrument_id].spec.exposure_tags):
                return self._reject(
                    signal, "REJECTED", RejectReason.RISK_LIMIT.value, f"group:{other.trade_id}"
                )
        if session.entries >= policy.exposure.max_new_entries_per_session:
            return self._reject(
                signal, "REJECTED", RejectReason.ENTRY_LIMIT_REACHED.value, "entries"
            )
        if c.costs.financing and spec.financing is None:
            return self._reject(signal, "REJECTED", RejectReason.UNKNOWN_COSTS.value, "financing")
        spread = (bar.ask.open - bar.bid.open) / bar.mid_open
        if spread > policy.max_entry_spread_fraction:
            return self._reject(signal, "REJECTED", RejectReason.SPREAD_TOO_WIDE.value, "spread")
        fx = ONE
        if inst.fx_pair is not None:
            rate = self._fx(iid, bar, "open")
            if rate is None:
                return self._reject(signal, "REJECTED", RejectReason.UNKNOWN_COSTS.value, "fx")
            fx = rate
        long = signal.direction is Direction.LONG
        tick = spec.price_tick
        price = (
            _up(bar.ask.open * (ONE + self.slip), tick)
            if long
            else _down(bar.bid.open * (ONE - self.slip), tick)
        )
        if (long and price <= signal.stop) or (not long and price >= signal.stop):
            return self._reject(
                signal, "REJECTED", RejectReason.NO_EXIT_PLAN.value, "stop_beyond_open"
            )
        expected = (signal.target - price) / price * (ONE if long else -ONE)
        margin_used = sum((tr.margin_usd for tr in self.open.values()), start=ZERO)
        spendable = self.equity - margin_used
        if signal.deadline is not None:
            # a day trade: the nights it is actually held through, none when flat before the charge
            nights = sum(n for _, n in charge_instants(spec, t, signal.deadline))
        else:
            nights = -(-signal.max_hold_minutes // 1440)
        sized = size_order(
            SizingRequest(
                policy=policy,
                spec=spec,
                direction=signal.direction,
                entry_price=price,
                stop_price=signal.stop,
                target_price=signal.target,
                fx_to_usd=fx,
                fees=c.fees,
                gap_allowance_fraction=policy.gap_allowance_floor_fraction,
                equity_usd=self.equity,
                portfolio_spendable_usd=spendable,
                account_spendable_usd=spendable,
                exposure=self._exposure(),
                expected_gross_return=expected,
                uncertainty_margin=ZERO,
                basis=ReturnBasis.EXECUTABLE,
                half_spread_fraction=ZERO,
                financing_nights=nights,
            )
        )
        if not sized.approved:
            return self._reject(
                signal,
                "REJECTED",
                ",".join(r.value for r in sized.reasons),
                sized.binding_constraint,
                planned_loss=sized.planned_loss_usd,
            )
        q = sized.quantity
        entry_side = Side.BUY if long else Side.SELL
        fee = c.fees.fee(entry_side, q, q * price) * fx
        margin = sized.notional_usd * spec.margin.initial_margin_fraction if spec.margin else ZERO
        trade = CfdTrade(
            trade_id=f"{signal.signal_id}#{len(self.trades) + 1}",
            signal_id=signal.signal_id,
            instrument_id=iid,
            direction=signal.direction,
            quantity=q,
            entry_time=t,
            entry_price=price,
            entry_mid=bar.mid_open,
            entry_fee_usd=fee,
            fx_entry=fx,
            stop=signal.stop,
            target=signal.target,
            target_exit=signal.target_role == "EXIT",
            deadline=(
                signal.deadline
                if signal.deadline is not None
                else t + timedelta(minutes=signal.max_hold_minutes)
            ),
            planned_loss_usd=sized.planned_loss_usd,
            notional_usd=sized.notional_usd,
            margin_usd=margin,
            entry_session=session_key,
        )
        self.cash -= fee
        self.open[iid] = trade
        self.trades.append(trade)
        session.entries += 1
        session.fees += fee
        session.turnover_usd += sized.notional_usd
        self.outcomes.append(
            CandidateOutcome(
                candidate_id=signal.signal_id,
                instrument_id=iid,
                decision_time=signal.decided_at,
                status="FILLED",
                reasons=(),
                quantity=q,
                notional_usd=sized.notional_usd,
                planned_loss_usd=sized.planned_loss_usd,
                expected_net_usd=sized.expected_net_usd,
                binding=sized.binding_constraint,
            )
        )
        return None

    def _accepted(self, signal: CfdSignal) -> bool:
        """Ask the signal filter (accept or refuse only) and log its decision."""
        flt = self.signal_filter
        assert flt is not None
        decision = flt.decide(signal)
        if not isinstance(decision, FilterDecision):
            raise TypeError("a signal filter answers with a FilterDecision")
        self.filter_log.append(
            FilterRecord(
                candidate_id=signal.signal_id,
                instrument_id=signal.instrument_id,
                decision_time=signal.decided_at,
                filter_id=flt.filter_id,
                accept=decision.accept,
                score="" if decision.score is None else canon_decimal(decision.score),
                reason=decision.reason,
            )
        )
        return decision.accept

    def _exposure(self) -> ExposureState:
        by_instrument: dict[str, Decimal] = {}
        by_tag: dict[str, Decimal] = {}
        gross = risk = ZERO
        for trade in self.open.values():
            spec = self.c.instruments[trade.instrument_id].spec
            gross += trade.notional_usd
            risk += trade.planned_loss_usd
            by_instrument[trade.instrument_id] = trade.notional_usd
            for tag in spec.exposure_tags:
                by_tag[tag] = by_tag.get(tag, ZERO) + trade.notional_usd
        return ExposureState(
            gross_usd=gross,
            by_instrument_usd=by_instrument,
            by_tag_usd=by_tag,
            planned_risk_usd=risk,
            open_positions=len(self.open),
        )

    # ------------------------------------------------------------------ marks
    def _fx(self, iid: str, bar: QuoteBar, at: Literal["open", "close"]) -> Decimal | None:
        pair = self.c.instruments[iid].fx_pair
        if pair is None:
            return ONE
        fx_bar = self.fx.get(pair, {}).get(bar.start)
        if fx_bar is None:
            return None
        return fx_bar.mid_open if at == "open" else fx_bar.mid_close

    def _mark(self, group: Sequence[tuple[str, QuoteBar]]) -> None:
        unrealised = ZERO
        for iid, trade in self.open.items():
            bar = self.last_bar[iid]
            side = bar.bid.close if trade.direction is Direction.LONG else bar.ask.close
            unrealised += trade.unrealised_usd(side, self._fx(iid, bar, "close") or trade.fx_entry)
        self.equity = self.cash + unrealised
        minutes = max(bar.minutes for _, bar in group)
        session = self._session(session_of(group[0][1]))
        session.end_equity = self.equity
        session.has_data = True
        if self.open:
            session.exposure_minutes = min(1440, session.exposure_minutes + minutes)
        self.high_water = max(self.high_water, self.equity)
        if self.paused is None:
            drawdown = self.policy.loss_limits.max_high_water_drawdown_fraction
            if self.equity <= self.floor:
                self._pause("capital_floor", group[0][1])
            elif self.equity <= self.high_water * (ONE - drawdown):
                self._pause("high_water_drawdown", group[0][1])

    def _pause(self, why: str, bar: QuoteBar) -> None:
        self.paused = why
        self.pauses.append({"at": iso_utc(bar.end), "reason": why, "equity": str(self.equity)})
        self._session(session_of(bar)).breaches.append(why)

    def _session(self, key: str) -> SessionStats:
        session = self.sessions.get(key)
        if session is None:
            session = SessionStats(session=key, end_equity=self.equity)
            self.sessions[key] = session
        return session

    # ------------------------------------------------------------------ result
    def _result(self) -> CfdEngineResult:
        trades = tuple(self.trades)
        candidates = tuple(sorted(self.outcomes, key=lambda o: (o.decision_time, o.candidate_id)))
        sessions = tuple(
            self.sessions[k]
            for k in sorted(self.sessions)
            if self.c.start.date().isoformat() <= k < self.c.end.date().isoformat()
        )
        payload = {
            "trades": [t.as_dict() for t in trades],
            "candidates": [c.as_dict() for c in candidates],
            "sessions": [
                {
                    "session": s.session,
                    "end_equity": s.end_equity,
                    "fees": s.fees,
                    "realized_net": s.realized_net,
                    "trades_closed": s.trades_closed,
                    "entries": s.entries,
                }
                for s in sessions
            ],
        }
        return CfdEngineResult(
            trades=trades,
            candidates=candidates,
            sessions=sessions,
            pauses=tuple(self.pauses),
            final_equity=self.equity,
            result_hash=content_hash(payload),
            diagnostics=dict(self.diag),
            filter_log=tuple(self.filter_log),
            filter_hash=(
                content_hash([r.as_dict() for r in self.filter_log])
                if self.signal_filter is not None
                else ""
            ),
            signals=tuple(self.seen),
        )


def run_cfd_engine(
    config: CfdEngineConfig,
    rule: CfdRule,
    signal_filter: CfdSignalFilter | None = None,
    *,
    record_candidates: bool = False,
) -> CfdEngineResult:
    return CfdEngine(config, rule, signal_filter, record_candidates=record_candidates).run()
