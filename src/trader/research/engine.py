"""Bar-based research simulator (risk_and_validation.md §6; instructions.md §6 task 6).

The simulator replays completed bars in availability order through a strategy and the *same*
policy, sizer, fee schedules and instrument specifications as the runtime, on a USD 50 ledger.
It exists because historical research needs years of bars, which the database-backed replay
runner (M1) is not built to process quickly; the decision rules are shared, and parity tests
compare the two paths on recorded sessions.

Execution assumptions (bar data has no quotes, so they are explicit and conservative):

* **Decision and order timing.** A bar is used only at its availability time ``end + 1 s``; the
  order is live ``latency`` later. It can execute only in the instrument's next bar, and only if
  that bar starts before the order's TTL expires (a missing bar means a missed entry).
* **Entry.** A marketable buy limit is set at the estimated ask at decision time (signal close
  plus the assumed half-spread) plus the frozen offset, rounded down to the tick. It fills at the
  next bar's open plus half-spread plus slippage, rounded up to the tick, **only if** that price
  is within the limit; otherwise the entry is missed. Sub-bar paths are never assumed, and the
  signal bar's close is never a fill price.
* **Participation.** At most ``participation`` of a bar's volume fills; a partial entry keeps its
  filled part and cancels the rest.
* **Exits** (reducing only, never larger than the open quantity): at a bar's open a time exit,
  session-close exit or gap through the stop sells at open minus half-spread minus slippage;
  within a bar the stop is assumed hit when the low reaches it (sell at stop minus half-spread
  minus slippage) and **takes precedence over the target** when both are touched; the target
  (a limit) fills only when the estimated bid trades strictly through it.
* **Fees** use the configured schedule at its own rounding granularity, both legs.
* **Equity** is marked at the estimated bid less the estimated exit fee (M1 conservative
  equity); sizing uses ``min(capital base, conservative equity)``.
* **Loss limits** follow the runtime windows. Because research has no owner, a breached daily
  window is assumed reviewed at the start of the next window, a weekly one at the next week;
  drawdown and capital-floor breaches stop new entries for the rest of the run. Reports state
  this assumption.
* **Splits** (equities) rescale feature state and multiply held quantities; outstanding entry
  orders on the instrument are cancelled.
* **Candidate filter** (optional, M3; strategy_and_profitability.md §6). It is asked first after
  the stale-data check - before pauses, entry limits, busy instruments and sizing - so it sees
  every in-window candidate that is not stale whatever the portfolio state, and its decisions do
  not depend on its earlier vetoes. A veto rejects the candidate as ``NO_EDGE`` with binding
  constraint ``filter:<filter_id>``; an accepted candidate continues through exactly the same
  checks and sizing as without a filter (a filter cannot size). Every decision is logged in
  ``EngineResult.filter_log`` with its own hash; ``result_hash`` is unchanged by the log, so a
  run with no filter or an accept-all filter reproduces the pre-filter hashes.

Daily-bar and multi-day rules (all opt-in; a rule that declares none of the following behaves
exactly as before, which the pinned result hashes of the registered trials prove). The engine
reads each declaration from the rule object or, failing that, from its ``spec``:

* ``holding = "OVERNIGHT"`` - positions are carried across sessions (no session-close exit) and
  the entry is a **next-open order**: it may fill only in the instrument's bar that starts at
  the next tradable time after the decision bar (the next session's open when the decision bar
  ends at a session close, as every daily bar does; otherwise the decision bar's end). That bar
  missing means a missed entry (``EXPIRED_NO_BAR_AT_NEXT_OPEN``); ``entry_ttl_seconds`` is not
  used. The fill time is that bar's open, the entry counts against that session's entry limit
  and its fees and turnover are booked there. A candidate is admitted when its next open lies
  in ``[start, end)``, so the signal of the last session before a deployment window is acted on
  in that window (at its first open) and the last session's signal inside a window belongs to
  the next one: each signal is traded in exactly one window. An entry bar that opens at or
  below the stop is sold at that open (``STOP_GAP``; a gap through the stop at a later open is
  the existing rule). Bars available at the same time are processed in two phases - first
  what happened during them (fills, exits) for every instrument, then the decisions at their
  close - so a next-open fill always precedes its session's decisions, whatever the sort order
  of the instruments (one pending entry at a time is the policy, ADR-0010).
* ``max_hold_sessions = N`` - the time exit is counted in sessions of the engine's calendar: a
  position entered in session ``S0`` is sold at the first open at or after the open of the
  ``N``-th session after ``S0`` (held through ``N`` sessions); ``max_hold_minutes`` is then not
  used.
* ``exit_signal(instrument_id, bar) -> str | None`` - called after ``on_bar`` for every
  completed bar of an instrument the engine holds; the first non-``None`` reason schedules a
  reducing exit at that instrument's next bar open, reported as ``SIGNAL:<reason>``.
* ``target_role = "COST_GATE"`` - the candidate's target feeds only the sizer's cost-coverage
  gate (ADR-0019) and is never a resting exit, for published rules whose profit exit is a
  signal at the close rather than a limit; the default ``"EXIT"`` keeps the target exit.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Literal, Protocol

from trader.accounting.fees import FeeSchedule
from trader.config.policy import RiskPolicy
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.data.corporate_actions import SplitEvent, split_effective_time
from trader.domain.candidates import RejectReason, ReturnBasis, TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.instruments import AnySpec
from trader.domain.market import Bar
from trader.domain.types import BPS, ZERO, Direction, Side, Track, iso_utc
from trader.research.filters import CandidateFilter, FilterDecision, FilterRecord, record
from trader.risk.loss_limits import window_key_and_start
from trader.risk.sizing import ExposureState, SizingRequest, size_order

ONE = Decimal(1)


class ResearchStrategy(Protocol):
    @property
    def strategy_id(self) -> str: ...

    @property
    def spec_hash(self) -> str: ...

    @property
    def uses_source_vwap(self) -> bool: ...

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None: ...

    def on_split(self, instrument_id: str, divisor: Decimal) -> None: ...

    def data_ok(self, instrument_id: str) -> bool: ...


Holding = Literal["INTRADAY", "OVERNIGHT"]
TargetRole = Literal["EXIT", "COST_GATE"]
ExitSignal = Callable[[str, Bar], str | None]
#: Exit reasons that sell at a bar's open (a scheduled ``SIGNAL:<reason>`` exit is one too).
OPEN_EXIT_REASONS = ("TIME_EXIT", "SESSION_CLOSE", "STOP_GAP", "EXIT_CONTINUES")
SIGNAL_PREFIX = "SIGNAL:"


def rule_declaration(strategy: object, name: str, default: Any) -> Any:
    """A rule's optional declaration: the rule's own attribute, else its ``spec``'s, else
    ``default`` (``None`` counts as absent)."""
    value = getattr(strategy, name, None)
    if value is None:
        value = getattr(getattr(strategy, "spec", None), name, None)
    return default if value is None else value


@dataclass(frozen=True, slots=True)
class RuleTiming:
    """The holding, time-exit, target and exit-signal declarations of a rule (opt-in)."""

    holding: Holding = "INTRADAY"
    max_hold_sessions: int | None = None
    target_role: TargetRole = "EXIT"
    exit_signal: ExitSignal | None = None

    @property
    def overnight(self) -> bool:
        return self.holding == "OVERNIGHT"

    @classmethod
    def of(cls, strategy: object) -> RuleTiming:
        holding = rule_declaration(strategy, "holding", "INTRADAY")
        if holding not in ("INTRADAY", "OVERNIGHT"):
            raise ValueError(f"holding must be INTRADAY or OVERNIGHT, not {holding!r}")
        sessions = rule_declaration(strategy, "max_hold_sessions", None)
        if sessions is not None and (
            isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 1
        ):
            raise ValueError(f"max_hold_sessions must be a positive integer, not {sessions!r}")
        role = rule_declaration(strategy, "target_role", "EXIT")
        if role not in ("EXIT", "COST_GATE"):
            raise ValueError(f"target_role must be EXIT or COST_GATE, not {role!r}")
        signal = getattr(strategy, "exit_signal", None)
        return cls(
            holding=holding,
            max_hold_sessions=sessions,
            target_role=role,
            exit_signal=signal if callable(signal) else None,
        )


@dataclass(frozen=True, slots=True)
class FillAssumptions:
    label: str
    half_spread_bps: Decimal
    slippage_bps: Decimal
    participation: Decimal
    latency_ms: int = 500
    multiplier: Decimal = ONE
    missed_entry_percent: int = 0

    @property
    def half_spread(self) -> Decimal:
        return self.half_spread_bps * self.multiplier * BPS

    @property
    def adverse(self) -> Decimal:
        return (self.half_spread_bps + self.slippage_bps) * self.multiplier * BPS

    def stressed(self, multiplier: Decimal, label: str) -> FillAssumptions:
        return FillAssumptions(
            label=label,
            half_spread_bps=self.half_spread_bps,
            slippage_bps=self.slippage_bps,
            participation=self.participation,
            latency_ms=self.latency_ms,
            multiplier=self.multiplier * multiplier,
            missed_entry_percent=self.missed_entry_percent,
        )

    def describe(self) -> dict[str, str]:
        return {
            "label": self.label,
            "half_spread_bps": str(self.half_spread_bps * self.multiplier),
            "slippage_bps": str(self.slippage_bps * self.multiplier),
            "participation": str(self.participation),
            "latency_ms": str(self.latency_ms),
            "missed_entry_percent": str(self.missed_entry_percent),
        }


@dataclass(frozen=True, slots=True)
class InstrumentSetup:
    spec: AnySpec
    bars: Sequence[Bar]
    splits: Sequence[SplitEvent] = ()


@dataclass(frozen=True, slots=True)
class EngineConfig:
    policy: RiskPolicy
    fees: FeeSchedule
    fills: FillAssumptions
    track: Track
    calendar: ExchangeCalendar | Utc247Calendar
    start: datetime
    end: datetime
    loss_windows: tuple[str, ...]
    session_basis: Literal["UTC_DAY", "XNYS"]
    initial_capital: Decimal = Decimal(50)
    finalization_delay: timedelta = timedelta(seconds=1)


# ----------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    candidate_id: str
    instrument_id: str
    decision_time: datetime
    status: Literal["FILLED", "PARTIAL", "UNFILLED", "REJECTED"]
    reasons: tuple[str, ...]
    quantity: Decimal
    notional_usd: Decimal
    planned_loss_usd: Decimal
    expected_net_usd: Decimal
    binding: str

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "instrument_id": self.instrument_id,
            "decision_time": iso_utc(self.decision_time),
            "status": self.status,
            "reasons": list(self.reasons),
            "quantity": self.quantity,
            "notional_usd": self.notional_usd,
            "planned_loss_usd": self.planned_loss_usd,
            "expected_net_usd": self.expected_net_usd,
            "binding": self.binding,
        }


@dataclass(frozen=True, slots=True)
class ExecFill:
    time: datetime
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    reason: str
    mid_reference: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "time": iso_utc(self.time),
            "side": self.side.value,
            "quantity": self.quantity,
            "price": self.price,
            "fee": self.fee,
            "reason": self.reason,
            "mid_reference": self.mid_reference,
        }


@dataclass(slots=True)
class Trade:
    trade_id: str
    candidate_id: str
    instrument_id: str
    entry: ExecFill
    stop: Decimal
    target: Decimal | None
    planned_loss_usd: Decimal
    deadline: datetime
    session_end: datetime | None
    entry_session: str
    quantity: Decimal
    exits: list[ExecFill] = field(default_factory=list)
    exiting_reason: str | None = None
    dust: Decimal = ZERO
    ambiguous_bar: bool = False  # stop and target both touched; pessimistic rule decided
    stopped_in_entry_bar: bool = False
    #: ``SIGNAL:<reason>`` once the rule's exit signal fired: sold at the next bar's open.
    scheduled_exit: str | None = None
    #: False when the rule declared ``target_role = "COST_GATE"`` (the target is never an exit).
    target_exit: bool = True

    @property
    def open_quantity(self) -> Decimal:
        return self.quantity - sum((f.quantity for f in self.exits), start=ZERO) - self.dust

    @property
    def closed(self) -> bool:
        return self.open_quantity <= 0

    def gross_pnl(self) -> Decimal:
        proceeds = sum((f.quantity * f.price for f in self.exits), start=ZERO)
        cost = sum((f.quantity for f in self.exits), start=ZERO) * self.entry.price
        return proceeds - cost

    def fees(self) -> Decimal:
        return self.entry.fee + sum((f.fee for f in self.exits), start=ZERO)

    def net_pnl(self) -> Decimal:
        return self.gross_pnl() - self.fees()

    def execution_cost(self) -> Decimal:
        """Spread and slippage paid relative to the estimated mid (already inside the prices)."""
        entry = self.entry.quantity * (self.entry.price - self.entry.mid_reference)
        exits = sum((f.quantity * (f.mid_reference - f.price) for f in self.exits), start=ZERO)
        return entry + exits

    def as_dict(self) -> dict[str, object]:
        last = self.exits[-1] if self.exits else None
        return {
            "trade_id": self.trade_id,
            "candidate_id": self.candidate_id,
            "instrument_id": self.instrument_id,
            "entry": self.entry.as_dict(),
            "exits": [f.as_dict() for f in self.exits],
            "exit_reason": last.reason if last else "OPEN",
            "stop": self.stop,
            "target": self.target,
            "quantity": self.quantity,
            "planned_loss_usd": self.planned_loss_usd,
            "gross_pnl": self.gross_pnl(),
            "fees": self.fees(),
            "net_pnl": self.net_pnl(),
            "execution_cost": self.execution_cost(),
            "dust": self.dust,
            "ambiguous_bar": self.ambiguous_bar,
            "stopped_in_entry_bar": self.stopped_in_entry_bar,
            "entry_session": self.entry_session,
            "holding_minutes": (
                int((last.time - self.entry.time).total_seconds() // 60) if last else None
            ),
        }


@dataclass(slots=True)
class _Pending:
    candidate: TradeCandidate
    quantity: Decimal
    limit: Decimal
    live_from: datetime
    expires: datetime
    planned_loss: Decimal
    notional: Decimal
    cash_reserved: Decimal
    expected_net: Decimal
    binding: str
    session: str
    #: Overnight (next-open) entries only: the open of the one bar the order may fill in.
    entry_open: datetime | None = None
    expiry_reason: str = "EXPIRED_NO_BAR_WITHIN_TTL"


@dataclass(slots=True)
class SessionStats:
    session: str
    end_equity: Decimal = ZERO
    fees: Decimal = ZERO
    realized_net: Decimal = ZERO
    trades_closed: int = 0
    entries: int = 0
    exposure_minutes: int = 0
    turnover_usd: Decimal = ZERO
    breaches: list[str] = field(default_factory=list)
    has_data: bool = False


@dataclass(frozen=True, slots=True)
class EngineResult:
    trades: tuple[Trade, ...]
    candidates: tuple[CandidateOutcome, ...]
    sessions: tuple[SessionStats, ...]
    pauses: tuple[dict[str, str], ...]
    diagnostics: dict[str, object]
    final_equity: Decimal
    final_cash: Decimal
    result_hash: str
    #: Every candidate filter decision in call order (empty without a filter); not part of
    #: ``result_hash``, which covers trades, candidate outcomes and sessions only.
    filter_log: tuple[FilterRecord, ...] = ()
    #: ``content_hash`` of the filter log, or "" when the run had no filter.
    filter_hash: str = ""


# ----------------------------------------------------------------------------- engine


def _down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _up(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


class ResearchEngine:
    def __init__(
        self,
        config: EngineConfig,
        strategy: ResearchStrategy,
        instruments: Mapping[str, InstrumentSetup],
        candidate_filter: CandidateFilter | None = None,
        feature_fn: Callable[[TradeCandidate], Mapping[str, float]] | None = None,
    ) -> None:
        """``feature_fn`` supplies the filter's feature values (see
        ``research.filters.candidate_feature_fn``); without a filter it is never called."""
        self.cfg = config
        self.strategy = strategy
        self.instruments = dict(instruments)
        self.candidate_filter = candidate_filter
        self.feature_fn = feature_fn
        self.filter_log: list[FilterRecord] = []
        policy = config.policy
        self.policy = policy
        self.cash = config.initial_capital
        self.realized_net = ZERO
        self.hwm = config.initial_capital
        self.trades: list[Trade] = []
        self.open: dict[str, Trade] = {}
        self.pending: _Pending | None = None
        self.outcomes: list[CandidateOutcome] = []
        self.last_price: dict[str, Decimal] = {}
        self.window_start: dict[str, tuple[str, Decimal]] = {}
        self.flags: dict[str, str] = {}
        self.pause_log: list[dict[str, str]] = []
        self.sessions: dict[str, SessionStats] = {}
        self.session_entries: dict[str, int] = {}
        self.splits_pending: dict[str, list[tuple[datetime, SplitEvent]]] = {}
        self.last_equity = config.initial_capital
        self._prior_equity = config.initial_capital
        self.diag: dict[str, int] = {
            "bars": 0,
            "candidates": 0,
            "stale_candidates": 0,
            "splits_applied": 0,
            "entry_orders_cancelled_by_split": 0,
        }
        if candidate_filter is not None:
            self.diag["filter_vetoes"] = 0
        self.timing = RuleTiming.of(strategy)
        if self.timing.exit_signal is not None:
            self.diag["exit_signals"] = 0
        for iid, setup in self.instruments.items():
            if setup.spec.instrument_id != iid:
                raise ValueError(f"instrument key {iid} does not match its spec")
            if isinstance(config.calendar, ExchangeCalendar):
                self.splits_pending[iid] = sorted(
                    (split_effective_time(s, config.calendar), s) for s in setup.splits
                )
            elif setup.splits:
                raise ValueError("splits need an exchange calendar")

    # ------------------------------------------------------------------ helpers
    def _session_key(self, ts: datetime) -> str:
        if self.cfg.session_basis == "UTC_DAY":
            return ts.date().isoformat()
        if not isinstance(self.cfg.calendar, ExchangeCalendar):
            raise TypeError("an XNYS session basis needs an exchange calendar")
        return self.cfg.calendar.session_date_of(ts).isoformat()

    def _session(self, key: str) -> SessionStats:
        stats = self.sessions.get(key)
        if stats is None:
            stats = SessionStats(session=key, end_equity=self.last_equity)
            self.sessions[key] = stats
        return stats

    def _spec(self, iid: str) -> AnySpec:
        return self.instruments[iid].spec

    def _exit_fee(self, iid: str, qty: Decimal, price: Decimal) -> Decimal:
        if qty <= 0:
            return ZERO
        units = self._spec(iid).units_per_quantity()
        return self.cfg.fees.fee(Side.SELL, qty, qty * units * price)

    def conservative_equity(self) -> Decimal:
        equity = self.cash
        for iid, trade in self.open.items():
            qty = trade.open_quantity + trade.dust
            bid = self.last_price.get(iid, trade.entry.price) * (ONE - self.cfg.fills.half_spread)
            units = self._spec(iid).units_per_quantity()
            equity += qty * units * bid - self._exit_fee(iid, trade.open_quantity, bid)
        return equity

    def capital_base(self) -> Decimal:
        return self.cfg.initial_capital + self.realized_net

    def _next_open(self, bar: Bar) -> datetime:
        """The next tradable time after ``bar``: the next session's open when the bar ends at
        its session's close (every daily bar), otherwise the bar's end."""
        calendar = self.cfg.calendar
        if isinstance(calendar, ExchangeCalendar):
            session = calendar.session_on(calendar.session_date_of(bar.start))
            if session is not None and session.open <= bar.start and bar.end >= session.close:
                return calendar.sessions_after(session.session_date).open
        return bar.end

    def _session_open_after(self, ts: datetime, sessions: int) -> datetime:
        """The open of the ``sessions``-th session after the session containing ``ts``."""
        calendar = self.cfg.calendar
        if isinstance(calendar, ExchangeCalendar):
            return calendar.sessions_after(calendar.session_date_of(ts), sessions).open
        day_start = datetime(ts.year, ts.month, ts.day, tzinfo=UTC)
        return day_start + timedelta(days=sessions)

    def _check_exit_signal(self, iid: str, bar: Bar) -> None:
        signal = self.timing.exit_signal
        trade = self.open.get(iid)
        if signal is None or trade is None:
            return
        reason = signal(iid, bar)
        if reason is None:
            return
        if not isinstance(reason, str) or not reason:
            raise TypeError(f"exit_signal must return a non-empty str or None, not {reason!r}")
        if trade.scheduled_exit is None and trade.exiting_reason is None:
            trade.scheduled_exit = SIGNAL_PREFIX + reason
            self.diag["exit_signals"] += 1

    # ------------------------------------------------------------------ main loop
    def run(self) -> EngineResult:
        timeline = sorted(
            (bar for setup in self.instruments.values() for bar in setup.bars),
            key=lambda b: (b.available_time, b.instrument_id),
        )
        if self.timing.overnight:
            # Bars available at the same time (one session's daily bars) are processed in two
            # phases: everything that happened during the bars (next-open fills, exits) for all
            # of them, then every decision at their close. A next-open fill therefore always
            # precedes the decisions of its session, whatever the instruments' sort order.
            for _, group in itertools.groupby(timeline, key=lambda b: b.available_time):
                bars = list(group)
                for bar in bars:
                    self._during_bar(bar)
                for bar in bars:
                    self._after_bar(bar)
        else:
            for bar in timeline:
                self._during_bar(bar)
                self._after_bar(bar)
        self._close_at_end(timeline)
        return self._result()

    def _during_bar(self, bar: Bar) -> None:
        """What happens inside a bar: splits, entry fills and expiries, exits, the last price."""
        self.diag["bars"] += 1
        iid = bar.instrument_id
        stats = self._session(self._session_key(bar.start))
        stats.has_data = True
        self._apply_splits(iid, bar.start)
        if self.pending is not None and bar.start > self.pending.expires:
            self._cancel_pending(self.pending.expiry_reason)
        self._process_pending(bar)
        self._process_exits(bar, stats)
        self.last_price[iid] = bar.close

    def _after_bar(self, bar: Bar) -> None:
        """What happens once a bar is available: loss checks, the rule, exit signals, entries."""
        now = bar.available_time
        iid = bar.instrument_id
        session_key = self._session_key(bar.start)
        stats = self._session(session_key)
        self._loss_checks(now, stats)
        if iid in self.open:
            stats.exposure_minutes += bar.minutes
        candidate = self.strategy.on_bar(bar, now)
        if self.timing.exit_signal is not None:
            self._check_exit_signal(iid, bar)
        if candidate is not None:
            if self.timing.overnight:
                entry_open = self._next_open(bar)
                if self.cfg.start <= entry_open < self.cfg.end:
                    self.diag["candidates"] += 1
                    entry_session = self._session_key(entry_open)
                    self._admit(candidate, bar, now, entry_session, entry_open)
            elif self.cfg.start <= now < self.cfg.end:
                self.diag["candidates"] += 1
                self._admit(candidate, bar, now, session_key)
        stats.end_equity = self.last_equity

    # ------------------------------------------------------------------ splits
    def _apply_splits(self, iid: str, bar_start: datetime) -> None:
        queue = self.splits_pending.get(iid)
        while queue and queue[0][0] <= bar_start:
            _, split = queue.pop(0)
            self.diag["splits_applied"] += 1
            self.strategy.on_split(iid, split.ratio)
            trade = self.open.get(iid)
            if trade is not None:
                trade.quantity *= split.ratio
                trade.entry = ExecFill(
                    time=trade.entry.time,
                    side=trade.entry.side,
                    quantity=trade.entry.quantity * split.ratio,
                    price=trade.entry.price / split.ratio,
                    fee=trade.entry.fee,
                    reason=trade.entry.reason,
                    mid_reference=trade.entry.mid_reference / split.ratio,
                )
                trade.stop /= split.ratio
                if trade.target is not None:
                    trade.target /= split.ratio
            if iid in self.last_price:
                self.last_price[iid] /= split.ratio
            if self.pending is not None and self.pending.candidate.instrument_id == iid:
                self._cancel_pending("ORDER_INVALIDATED_BY_SPLIT")
                self.diag["entry_orders_cancelled_by_split"] += 1

    # ------------------------------------------------------------------ entries
    def _cancel_pending(self, reason: str) -> None:
        p = self.pending
        if p is None:
            return
        self.outcomes.append(
            CandidateOutcome(
                candidate_id=p.candidate.candidate_id,
                instrument_id=p.candidate.instrument_id,
                decision_time=p.candidate.decision_time,
                status="UNFILLED",
                reasons=(reason,),
                quantity=ZERO,
                notional_usd=p.notional,
                planned_loss_usd=p.planned_loss,
                expected_net_usd=p.expected_net,
                binding=p.binding,
            )
        )
        self.pending = None

    def _process_pending(self, bar: Bar) -> None:
        p = self.pending
        if p is None or p.candidate.instrument_id != bar.instrument_id:
            return
        if bar.start < (p.candidate.bar_end if p.entry_open is None else p.entry_open):
            return
        fills = self.cfg.fills
        if fills.missed_entry_percent:
            digest = hashlib.sha256(f"{p.candidate.candidate_id}|{fills.label}".encode()).digest()
            if int.from_bytes(digest[:4], "big") % 100 < fills.missed_entry_percent:
                self._cancel_pending("SIMULATED_OUTAGE")
                return
        spec = self._spec(bar.instrument_id)
        price = _up(bar.open * (ONE + fills.adverse), spec.price_tick)
        if price > p.limit:
            self._cancel_pending("LIMIT_NOT_MARKETABLE_AT_NEXT_OPEN")
            return
        cap = spec.round_quantity_down(fills.participation * bar.volume / spec.units_per_quantity())
        qty = min(p.quantity, cap)
        if qty <= 0 or spec.minimum_violations(qty, price):
            self._cancel_pending("PARTICIPATION_CAP")
            return
        units = spec.units_per_quantity()
        notional = qty * units * price
        fee = self.cfg.fees.fee(Side.BUY, qty, notional)
        self.cash -= notional + fee
        candidate = p.candidate
        fill_time = candidate.decision_time + timedelta(milliseconds=fills.latency_ms)
        if p.entry_open is not None:  # a next-open order trades at its bar's open
            fill_time = max(fill_time, bar.start)
        entry = ExecFill(
            time=fill_time,
            side=Side.BUY,
            quantity=qty,
            price=price,
            fee=fee,
            reason="ENTRY",
            mid_reference=bar.open,
        )
        session_end = None
        if isinstance(self.cfg.calendar, ExchangeCalendar) and not self.timing.overnight:
            session = self.cfg.calendar.session_at(bar.start)
            if session is not None:
                session_end = self.cfg.calendar.close_procedure_start(session)
        sessions = self.timing.max_hold_sessions
        deadline = (
            fill_time + timedelta(minutes=candidate.max_hold_minutes)
            if sessions is None
            else self._session_open_after(bar.start, sessions)
        )
        trade = Trade(
            trade_id=f"trade-{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            instrument_id=candidate.instrument_id,
            entry=entry,
            stop=candidate.stop_price,
            target=candidate.target_price,
            planned_loss_usd=p.planned_loss * qty / p.quantity,
            deadline=deadline,
            session_end=session_end,
            entry_session=p.session,
            quantity=qty,
            target_exit=self.timing.target_role == "EXIT",
        )
        self.trades.append(trade)
        self.open[candidate.instrument_id] = trade
        stats = self._session(p.session)
        stats.fees += fee
        stats.turnover_usd += notional
        self.outcomes.append(
            CandidateOutcome(
                candidate_id=candidate.candidate_id,
                instrument_id=candidate.instrument_id,
                decision_time=candidate.decision_time,
                status="FILLED" if qty == p.quantity else "PARTIAL",
                reasons=() if qty == p.quantity else ("PARTIAL_PARTICIPATION_REMAINDER_CANCELLED",),
                quantity=qty,
                notional_usd=notional,
                planned_loss_usd=trade.planned_loss_usd,
                expected_net_usd=p.expected_net,
                binding=p.binding,
            )
        )
        self.pending = None

    def _reject(
        self,
        candidate: TradeCandidate,
        reasons: Sequence[str],
        binding: str = "",
        *,
        quantity: Decimal = ZERO,
        planned: Decimal = ZERO,
        expected: Decimal = ZERO,
    ) -> None:
        self.outcomes.append(
            CandidateOutcome(
                candidate_id=candidate.candidate_id,
                instrument_id=candidate.instrument_id,
                decision_time=candidate.decision_time,
                status="REJECTED",
                reasons=tuple(reasons),
                quantity=quantity,
                notional_usd=ZERO,
                planned_loss_usd=planned,
                expected_net_usd=expected,
                binding=binding,
            )
        )

    def _exposure(self) -> ExposureState:
        gross = ZERO
        by_inst: dict[str, Decimal] = {}
        by_tag: dict[str, Decimal] = {}
        planned = ZERO
        for iid, trade in self.open.items():
            spec = self._spec(iid)
            notional = (
                trade.open_quantity
                * spec.units_per_quantity()
                * self.last_price.get(iid, trade.entry.price)
            )
            gross += notional
            by_inst[iid] = by_inst.get(iid, ZERO) + notional
            for tag in spec.exposure_tags:
                by_tag[tag] = by_tag.get(tag, ZERO) + notional
            planned += trade.planned_loss_usd
        if self.pending is not None:
            p = self.pending
            spec = self._spec(p.candidate.instrument_id)
            gross += p.notional
            by_inst[spec.instrument_id] = by_inst.get(spec.instrument_id, ZERO) + p.notional
            for tag in spec.exposure_tags:
                by_tag[tag] = by_tag.get(tag, ZERO) + p.notional
            planned += p.planned_loss
        return ExposureState(
            gross_usd=gross,
            by_instrument_usd=by_inst,
            by_tag_usd=by_tag,
            planned_risk_usd=planned,
            open_positions=len(self.open),
            pending_entries=1 if self.pending is not None else 0,
        )

    def _filter_accepts(self, flt: CandidateFilter, candidate: TradeCandidate) -> bool:
        """Ask the research filter; a veto is recorded as ``NO_EDGE`` (accept/veto only)."""
        features = self.feature_fn(candidate) if self.feature_fn is not None else {}
        decision = flt.decide(candidate, features)
        if not isinstance(decision, FilterDecision):
            raise TypeError(f"filter {flt.filter_id} returned {type(decision).__name__}")
        self.filter_log.append(record(flt.filter_id, candidate, decision))
        if decision.accept:
            return True
        self.diag["filter_vetoes"] += 1
        self._reject(candidate, [RejectReason.NO_EDGE.value], f"filter:{flt.filter_id}")
        return False

    def _admit(
        self,
        candidate: TradeCandidate,
        bar: Bar,
        now: datetime,
        session: str,
        entry_open: datetime | None = None,
    ) -> None:
        """Admit or reject a candidate. ``entry_open`` is set for a next-open (overnight) order;
        ``session`` is then the session of that open."""
        policy = self.policy
        iid = candidate.instrument_id
        if not self.strategy.data_ok(iid):
            self.diag["stale_candidates"] += 1
            self._reject(candidate, [RejectReason.STALE_DATA.value], "non_contiguous_window")
            return
        flt = self.candidate_filter
        if flt is not None and not self._filter_accepts(flt, candidate):
            return
        if self.flags:
            self._reject(candidate, [RejectReason.PAUSED.value], ",".join(sorted(self.flags)))
            return
        if self.session_entries.get(session, 0) >= policy.exposure.max_new_entries_per_session:
            self._reject(candidate, [RejectReason.ENTRY_LIMIT_REACHED.value], "session_entries")
            return
        if self.pending is not None:
            self._reject(candidate, [RejectReason.RISK_LIMIT.value], "pending_entry")
            return
        if iid in self.open:
            self._reject(candidate, [RejectReason.RISK_LIMIT.value], "instrument_busy")
            return
        if len(self.open) >= policy.exposure.max_open_positions:
            self._reject(candidate, [RejectReason.RISK_LIMIT.value], "max_open_positions")
            return
        fills = self.cfg.fills
        if 2 * fills.half_spread / BPS > policy.quality.max_entry_spread_bps:
            self._reject(candidate, [RejectReason.SPREAD_TOO_WIDE.value], "assumed_spread")
            return
        spec = self._spec(iid)
        est_ask = bar.close * (ONE + fills.half_spread)
        limit = _down(est_ask * (ONE + candidate.entry_limit_offset_bps * BPS), spec.price_tick)
        if candidate.stop_price >= limit:
            self._reject(candidate, [RejectReason.NO_EXIT_PLAN.value], "stop_above_entry")
            return
        target = candidate.target_price
        if target is None or target <= limit:
            self._reject(candidate, [RejectReason.TOO_COSTLY.value], "target_below_entry_bound")
            return
        gap = max(candidate.gap_allowance_fraction, policy.quality.gap_allowance_floor_bps * BPS)
        equity = min(self.capital_base(), self.conservative_equity())
        reserved = self.pending.cash_reserved if self.pending is not None else ZERO
        spendable = self.cash - reserved
        result = size_order(
            SizingRequest(
                policy=policy,
                spec=spec,
                direction=Direction.LONG,
                entry_price=limit,
                stop_price=candidate.stop_price,
                target_price=target,
                fx_to_usd=ONE,
                fees=self.cfg.fees,
                gap_allowance_fraction=gap,
                equity_usd=equity,
                portfolio_spendable_usd=spendable,
                account_spendable_usd=spendable,
                exposure=self._exposure(),
                # Research cost gate: the planned target distance must cover the round-trip
                # costs at the exact size. It is a cost-coverage check, not an edge estimate.
                expected_gross_return=(target - limit) / limit,
                uncertainty_margin=ZERO,
                basis=ReturnBasis.EXECUTABLE,
                half_spread_fraction=fills.half_spread,
            )
        )
        if not result.approved:
            self._reject(
                candidate,
                [r.value for r in result.reasons],
                result.binding_constraint,
                quantity=result.quantity,
                planned=result.planned_loss_usd,
                expected=result.expected_net_usd,
            )
            return
        live_from = now + timedelta(milliseconds=fills.latency_ms)
        self.pending = _Pending(
            candidate=candidate,
            quantity=result.quantity,
            limit=limit,
            live_from=live_from if entry_open is None else max(live_from, entry_open),
            expires=(
                now + timedelta(seconds=candidate.entry_ttl_seconds)
                if entry_open is None
                else entry_open
            ),
            planned_loss=result.planned_loss_usd,
            notional=result.notional_usd,
            cash_reserved=result.cash_required_usd,
            expected_net=result.expected_net_usd,
            binding=result.binding_constraint,
            session=session,
            entry_open=entry_open,
            expiry_reason=(
                "EXPIRED_NO_BAR_WITHIN_TTL" if entry_open is None else "EXPIRED_NO_BAR_AT_NEXT_OPEN"
            ),
        )
        self.session_entries[session] = self.session_entries.get(session, 0) + 1
        self._session(session).entries += 1

    # ------------------------------------------------------------------ exits
    def _sell(
        self, trade: Trade, bar: Bar, price: Decimal, reason: str, stats: SessionStats
    ) -> None:
        """Sell up to the participation cap; never more than the open quantity."""
        spec = self._spec(trade.instrument_id)
        fills = self.cfg.fills
        cap = spec.round_quantity_down(fills.participation * bar.volume / spec.units_per_quantity())
        qty = min(trade.open_quantity, cap)
        if qty <= 0:
            trade.exiting_reason = trade.exiting_reason or reason
            return
        units = spec.units_per_quantity()
        notional = qty * units * price
        fee = self.cfg.fees.fee(Side.SELL, qty, notional)
        at_open = reason in OPEN_EXIT_REASONS or reason.startswith(SIGNAL_PREFIX)
        mid = price if reason == "TARGET" else price / (ONE - fills.adverse)
        trade.exits.append(
            ExecFill(
                time=max(bar.start if at_open else bar.end, trade.entry.time),
                side=Side.SELL,
                quantity=qty,
                price=price,
                fee=fee,
                reason=reason,
                mid_reference=mid,
            )
        )
        self.cash += notional - fee
        stats.fees += fee
        stats.turnover_usd += notional
        remaining = trade.open_quantity
        if 0 < remaining < spec.min_quantity:
            trade.dust = remaining  # below the venue minimum: reported, marked, never oversold
        elif remaining > 0:
            trade.exiting_reason = "EXIT_CONTINUES"
        if trade.closed:
            net = trade.net_pnl()
            self.realized_net += net
            stats.realized_net += net
            stats.trades_closed += 1
            del self.open[trade.instrument_id]

    def _process_exits(self, bar: Bar, stats: SessionStats) -> None:
        trade = self.open.get(bar.instrument_id)
        if trade is None:
            return
        spec = self._spec(trade.instrument_id)
        fills = self.cfg.fills
        tick = spec.price_tick
        entry_bar = bar.start <= trade.entry.time < bar.end

        def at_open() -> Decimal:
            return _down(bar.open * (ONE - fills.adverse), tick)

        if entry_bar and self.timing.overnight and bar.open <= trade.stop:
            # A next-open entry whose bar opens through the stop: the protective stop placed
            # after the fill is already marketable, so it sells at that open.
            trade.stopped_in_entry_bar = True
            self._sell(trade, bar, at_open(), "STOP_GAP", stats)
            return
        if not entry_bar:
            if trade.exiting_reason is not None:
                self._sell(trade, bar, at_open(), "EXIT_CONTINUES", stats)
                return
            if trade.scheduled_exit is not None:
                self._sell(trade, bar, at_open(), trade.scheduled_exit, stats)
                return
            session_over = trade.session_end is not None and bar.start >= trade.session_end
            if session_over:
                self._sell(trade, bar, at_open(), "SESSION_CLOSE", stats)
                return
            if bar.start >= trade.deadline:
                self._sell(trade, bar, at_open(), "TIME_EXIT", stats)
                return
            if bar.open <= trade.stop:
                self._sell(trade, bar, at_open(), "STOP_GAP", stats)
                return
        target = trade.target if trade.target_exit else None
        if bar.low <= trade.stop:
            target_touched = target is not None and bar.high * (ONE - fills.half_spread) > target
            trade.ambiguous_bar = trade.ambiguous_bar or target_touched
            trade.stopped_in_entry_bar = trade.stopped_in_entry_bar or entry_bar
            self._sell(trade, bar, _down(trade.stop * (ONE - fills.adverse), tick), "STOP", stats)
            return
        if target is not None and bar.high * (ONE - fills.half_spread) > target:
            self._sell(trade, bar, target, "TARGET", stats)

    def _close_at_end(self, timeline: Sequence[Bar]) -> None:
        if self.pending is not None:
            self._cancel_pending("END_OF_DATA")
        last_bar: dict[str, Bar] = {}
        for bar in timeline:
            last_bar[bar.instrument_id] = bar
        for iid in sorted(self.open):
            trade = self.open[iid]
            bar = last_bar[iid]
            spec = self._spec(iid)
            price = _down(bar.close * (ONE - self.cfg.fills.adverse), spec.price_tick)
            stats = self._session(self._session_key(bar.start))
            qty = trade.open_quantity
            fee = self._exit_fee(iid, qty, price)
            trade.exits.append(
                ExecFill(bar.end, Side.SELL, qty, price, fee, "END_OF_DATA", bar.close)
            )
            self.cash += qty * spec.units_per_quantity() * price - fee
            net = trade.net_pnl()
            self.realized_net += net
            stats.realized_net += net
            stats.fees += fee
            stats.trades_closed += 1
            del self.open[iid]
        self.last_equity = self.conservative_equity()
        if self.sessions:
            last_key = max(self.sessions)
            self.sessions[last_key].end_equity = self.last_equity

    # ------------------------------------------------------------------ loss limits
    def _loss_checks(self, now: datetime, stats: SessionStats) -> None:
        equity = self.conservative_equity()
        self.last_equity = equity
        limits = self.policy.loss_limits
        for kind in self.cfg.loss_windows:
            key, _ = window_key_and_start(kind, now)
            current = self.window_start.get(kind)
            if current is None or current[0] != key:
                start_equity = equity if current is None else max(equity, self._prior_equity)
                self.window_start[kind] = (key, start_equity)
                self._clear_flag(kind, now)
                current = self.window_start[kind]
            fraction = (
                limits.daily_loss_fraction
                if kind in ("NY_SESSION", "UTC_DAY")
                else limits.weekly_loss_fraction
            )
            if current[1] - equity >= fraction * current[1] and kind not in self.flags:
                loss = current[1] - equity
                self._raise(kind, now, f"loss {loss:.4f} >= {fraction * current[1]:.4f}", stats)
        self.hwm = max(self.hwm, equity)
        drawdown_hit = self.hwm - equity >= limits.max_high_water_drawdown_fraction * self.hwm
        if drawdown_hit and "DRAWDOWN" not in self.flags:
            self._raise("DRAWDOWN", now, f"drawdown from {self.hwm:.4f}", stats)
        if equity <= limits.initial_capital_pause_floor and "CAPITAL_FLOOR" not in self.flags:
            self._raise("CAPITAL_FLOOR", now, f"equity {equity:.4f}", stats)
        self._prior_equity = equity

    def _raise(self, cause: str, now: datetime, detail: str, stats: SessionStats) -> None:
        self.flags[cause] = iso_utc(now)
        stats.breaches.append(cause)
        self.pause_log.append({"cause": cause, "raised": iso_utc(now), "detail": detail})
        if self.pending is not None:
            self._cancel_pending("PAUSED_BEFORE_FILL")

    def _clear_flag(self, kind: str, now: datetime) -> None:
        if kind in self.flags:
            del self.flags[kind]
            self.pause_log.append(
                {
                    "cause": kind,
                    "cleared": iso_utc(now),
                    "detail": "research assumption: owner review at the next window",
                }
            )

    # ------------------------------------------------------------------ result
    def _result(self) -> EngineResult:
        trades = tuple(self.trades)
        candidates = tuple(sorted(self.outcomes, key=lambda o: (o.decision_time, o.candidate_id)))
        sessions = tuple(self.sessions[k] for k in sorted(self.sessions))
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
        diagnostics: dict[str, object] = dict(self.diag)
        diagnostics["feature_gap_resets"] = getattr(self.strategy, "gaps", lambda: 0)()
        diagnostics["open_at_end_force_closed"] = sum(
            1 for t in trades if t.exits and t.exits[-1].reason == "END_OF_DATA"
        )
        return EngineResult(
            trades=trades,
            candidates=candidates,
            sessions=sessions,
            pauses=tuple(self.pause_log),
            diagnostics=diagnostics,
            final_equity=self.last_equity,
            final_cash=self.cash,
            result_hash=content_hash(payload),
            filter_log=tuple(self.filter_log),
            filter_hash=(
                content_hash([r.as_dict() for r in self.filter_log])
                if self.candidate_filter is not None
                else ""
            ),
        )


def session_dates(
    basis: Literal["UTC_DAY", "XNYS"],
    calendar: ExchangeCalendar | Utc247Calendar,
    start: datetime,
    end: datetime,
) -> list[date]:
    """Every session in ``[start, end)``, including sessions without data or trades."""
    days: list[date] = []
    day = start.date()
    while datetime(day.year, day.month, day.day, tzinfo=start.tzinfo) < end:
        if basis == "UTC_DAY" or (
            isinstance(calendar, ExchangeCalendar) and calendar.is_session_day(day)
        ):
            days.append(day)
        day += timedelta(days=1)
    return days
