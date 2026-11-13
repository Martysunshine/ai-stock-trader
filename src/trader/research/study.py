"""Registered research studies: configuration, trial registration and the baseline run.

A study fixes the data, universe, execution specifications, cost schedule, fill assumptions,
split plan, bootstrap settings and the list of trials **before** anything runs. Every trial is
registered in the experiment registry with the content hashes of its data, and a run refuses a
trial whose registration no longer matches (risk_and_validation.md §5).

A baseline run evaluates every trial on the development period only (the final test period stays
locked) under three cost views:

* ``base`` - the registered cost basis (fee schedule plus base spread/slippage assumptions);
* ``stressed`` - spread and slippage multiplied (at least 2x) plus deterministic missed entries;
* ``diagnostic`` - the same rules with zero commission, to locate the fee level at which the
  signal would break even. It is labelled as unachievable and never used for selection.

Walk-forward selection (declared in every registration): in each fold, the trial with the
highest validation net PnL per session (trades entered and exited inside the window) is chosen
for the next test window *if that figure is positive*; otherwise the fold holds cash. The
concatenated test windows form the walk-forward series on which G2 statistics are computed.

Two optional study settings (absent = the behaviour and registration hashes of every study
registered before they existed):

* ``warmup_sessions`` - each independent window (and the continuous deployment) is preceded by
  the bars of that many exchange sessions (UTC days for a UTC-day study), which feed the
  features only, so long lookbacks such as a 200-day average are warm at the window start. The
  default is ADR-0020's two calendar days. Bound into new registrations (``splits``).
* ``diagnostic_capitals`` - **capital views**, diagnostic only: each trial's base view is re-run
  from each listed capital with every USD amount of the policy scaled by
  ``capital / initial_trading_capital`` (``config.policy.scale_policy``). They answer "how much
  of the result is the USD 50 scale?" and are reported in their own section. They are computed
  after, and never read by, walk-forward selection, the selection-aware statistics or G2.
  Bound into new registrations (``cost_basis``).

A trial may also list ``instruments`` (a subset of the study's execution ids) when its published
rule covers part of a shared universe, e.g. the equity funds of a basket that also holds bond and
gold funds. Its registration then names exactly that universe and those data hashes, and its
runs see only those bars. Absent, the trial trades every study instrument, as before. The
buy-and-hold comparator stays the study's whole universe.
"""

from __future__ import annotations

import itertools
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
from pydantic import Field, model_validator

from trader.accounting.fees import (
    FeeSchedule,
    FeeScheduleConfig,
    fee_schedule_from_config,
    load_fee_schedule,
)
from trader.brokers.catalog import load_instruments
from trader.config.policy import (
    USD_AMOUNT_FIELDS,
    LoadedPolicy,
    load_policy,
    load_yaml_strict,
    scale_policy,
)
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.data.corporate_actions import split_effective_time
from trader.data.history import DataQualityError, to_domain_bars
from trader.data.ingest import load_bars, split_events
from trader.data.manifest import DataManifest
from trader.domain.base import ConfigModel
from trader.domain.hashing import content_hash
from trader.domain.instruments import AnySpec
from trader.domain.market import Bar
from trader.domain.types import EvidenceLevel, NonNegDec, PosDec, Side, Track, iso_utc
from trader.research.adapters import SessionWindowAdapter
from trader.research.engine import (
    EngineConfig,
    EngineResult,
    FillAssumptions,
    InstrumentSetup,
    ResearchEngine,
    SessionStats,
    session_dates,
)
from trader.research.registry import ExperimentRegistry, TrialRegistration
from trader.research.splits import Fold, SplitPlan, add_months, folds, guard_window
from trader.research.stats import (
    as_array,
    bootstrap_mean,
    bootstrap_ratio,
    deflated_sharpe,
    drawdown_stats,
    effective_sample_size,
    loss_trigger_probabilities,
    reality_check,
    sessions_for_power,
    sharpe,
)
from trader.strategies import registry as rule_registry
from trader.strategies.candidate_a import CandidateA, CandidateASpec
from trader.strategies.research_rules import AnyRuleSpec

SELECTION_RULE = (
    "Walk-forward: per fold choose the trial with the highest validation net PnL per session "
    "(trades entered and exited inside the window, embargo = max hold) if positive, else hold "
    "cash; evaluate on the next test window. G2 interval: one-sided 95% stationary block "
    "bootstrap over sessions of the walk-forward series. Selection-aware checks: bootstrap "
    "max-statistic (reality check) across all study trials on the test windows and the deflated "
    "Sharpe ratio with N = all registered trials. The locked final test period is not used."
)


class FillCfg(ConfigModel):
    label: str = "base"
    half_spread_bps: NonNegDec
    slippage_bps: NonNegDec
    participation: PosDec
    latency_ms: int = Field(ge=0)


class StressCfg(ConfigModel):
    multiplier: PosDec
    missed_entry_percent: int = Field(ge=0, le=50)


class BootstrapCfg(ConfigModel):
    replicates: int = Field(ge=200)
    mean_block_sessions: PosDec
    seed: int


class InstrumentMap(ConfigModel):
    price_instrument_id: str
    execution_instrument_id: str


class TrialCfg(ConfigModel):
    trial_id: str
    spec_file: str
    overrides: dict[str, str] = Field(default_factory=dict)
    hypothesis: str
    #: Execution instrument ids this trial trades, a subset of the study's instruments (opt-in).
    #: ``None`` = every study instrument, as for every trial registered before the field existed;
    #: set, it narrows the registration's universe and data hashes and the trial's runs.
    instruments: tuple[str, ...] | None = Field(default=None, min_length=1)


class StudyConfig(ConfigModel):
    study_id: str
    title: str
    track: Track
    description: str
    price_dataset: str
    basis_datasets: tuple[str, ...] = ()
    instruments: tuple[InstrumentMap, ...]
    instrument_specs: str
    fee_schedule: str
    policy: str
    fills: FillCfg
    stress: StressCfg
    loss_windows: tuple[str, ...]
    session_basis: Literal["UTC_DAY", "XNYS"]
    annualisation_sessions: int
    splits: SplitPlan
    operating_costs_usd_per_month: NonNegDec
    bootstrap: BootstrapCfg
    universe_selection: str
    horizon: str
    entry_exit_timing: str
    trials: tuple[TrialCfg, ...]
    #: Exchange sessions (UTC days for UTC_DAY studies) of feature warm-up before each window;
    #: ``None`` keeps ADR-0020's two calendar days.
    warmup_sessions: int | None = Field(default=None, ge=1)
    #: Capitals (USD, quoted decimals) of the diagnostic capital views; never used by G2.
    diagnostic_capitals: tuple[PosDec, ...] = ()

    @model_validator(mode="after")
    def _distinct_capitals(self) -> StudyConfig:
        if len(set(self.diagnostic_capitals)) != len(self.diagnostic_capitals):
            raise ValueError("diagnostic_capitals must be distinct")
        return self

    @model_validator(mode="after")
    def _trial_universes(self) -> StudyConfig:
        known = {m.execution_instrument_id for m in self.instruments}
        for trial in self.trials:
            if trial.instruments is None:
                continue
            if len(set(trial.instruments)) != len(trial.instruments):
                raise ValueError(f"{trial.trial_id}: instruments must be distinct")
            unknown = sorted(set(trial.instruments) - known)
            if unknown:
                raise ValueError(f"{trial.trial_id}: instruments not in the study: {unknown}")
        return self


def load_study(path: Path) -> StudyConfig:
    return StudyConfig.model_validate(load_yaml_strict(path))


def trial_instruments(cfg: StudyConfig, trial: TrialCfg) -> tuple[InstrumentMap, ...]:
    """The study instruments a trial trades, in the study's order (all of them by default)."""
    if trial.instruments is None:
        return cfg.instruments
    chosen = set(trial.instruments)
    return tuple(m for m in cfg.instruments if m.execution_instrument_id in chosen)


@dataclass(frozen=True, slots=True)
class StudyContext:
    repo_root: Path
    config: StudyConfig
    manifest: DataManifest
    policy: LoadedPolicy
    fees: FeeSchedule
    specs: dict[str, AnySpec]

    @property
    def calendar(self) -> ExchangeCalendar | Utc247Calendar:
        return Utc247Calendar() if self.config.session_basis == "UTC_DAY" else ExchangeCalendar()


def open_study(repo_root: Path, config: StudyConfig, manifest: DataManifest) -> StudyContext:
    specs = load_instruments(repo_root / config.instrument_specs)
    for m in config.instruments:
        if m.execution_instrument_id not in specs:
            raise ValueError(f"no execution spec for {m.execution_instrument_id}")
    return StudyContext(
        repo_root=repo_root,
        config=config,
        manifest=manifest,
        policy=load_policy(repo_root / config.policy),
        fees=load_fee_schedule(repo_root / config.fee_schedule),
        specs=specs,
    )


#: A trial's frozen specification. A family registered in ``strategies/registry.py`` may return
#: any frozen specification with the same named attributes (``strategy_id``, ``version``,
#: ``family``, ``feature_version``, ``spec_hash``, ``model_dump``); the two classes here are the
#: families that existed before the registry.
AnyStudySpec = AnyRuleSpec | CandidateASpec


def resolve_rule(ctx: StudyContext, trial: TrialCfg) -> AnyStudySpec:
    """The frozen specification of a trial: the runtime Candidate A (no ``family`` key) or a
    research rule resolved by its family through the rule registry."""
    path = ctx.repo_root / trial.spec_file
    raw = load_yaml_strict(path)
    if "family" not in raw:  # the runtime stock Candidate A specification
        return CandidateASpec.model_validate({**raw, **trial.overrides})
    spec: AnyStudySpec = rule_registry.load_spec(path, trial.overrides)
    return spec


def spec_family(spec: AnyStudySpec) -> str:
    return "CANDIDATE_A" if isinstance(spec, CandidateASpec) else spec.family


def spec_uses_source_vwap(spec: AnyStudySpec) -> bool:
    if isinstance(spec, CandidateASpec):
        return True
    return bool(getattr(spec, "use_source_vwap", False))


def build_strategy(ctx: StudyContext, spec: AnyStudySpec, ticks: Mapping[str, Decimal]) -> Any:
    calendar = ctx.calendar
    if isinstance(spec, CandidateASpec):
        if not isinstance(calendar, ExchangeCalendar):
            raise ValueError("Candidate A (runtime) needs an exchange calendar")
        return SessionWindowAdapter(CandidateA(spec, calendar), calendar)
    return rule_registry.build(spec, calendar, ticks)


def _registered_features(spec: AnyStudySpec) -> tuple[str, ...]:
    """The registration's feature description (unchanged text for the existing families)."""
    if isinstance(spec, CandidateASpec):
        return rule_registry.STREAMING_FEATURES
    return rule_registry.spec_features(spec)


class UnknownCostBasisError(ValueError):
    """A study whose fee schedule is unknown can be neither registered nor run."""


def require_known_costs(ctx: StudyContext) -> None:
    """Fail closed before registration or a run when the study's fee schedule is unknown.

    Unknown fees mean no trade, never zero fees (CRYPTO_TRADING_TRACK.md §6;
    risk_and_validation.md §3): every candidate would be rejected ``UNKNOWN_COSTS`` and the
    buy-and-hold comparator could not be priced, so a registered trial could never produce
    evidence. Registering it would only spend the trial budget (risk_and_validation.md §5).
    """
    fees = ctx.fees
    if not fees.known:
        raise UnknownCostBasisError(
            f"study {ctx.config.study_id}: fee schedule {fees.schedule_id} "
            f"({ctx.config.fee_schedule}) is unknown (known: false); document the costs in a "
            "verified schedule before registering or running the study"
        )


def build_registration(ctx: StudyContext, trial: TrialCfg) -> TrialRegistration:
    cfg = ctx.config
    require_known_costs(ctx)
    spec = resolve_rule(ctx, trial)
    ds = ctx.manifest.dataset(cfg.price_dataset)
    if ds.status != "ACQUIRED":
        raise ValueError(f"{ds.name} is {ds.status}; acquire the data before registering")
    instruments = trial_instruments(cfg, trial)
    datasets = tuple(
        {
            "dataset": ds.name,
            "instrument_id": m.price_instrument_id,
            "content_sha256": ds.file_for(m.price_instrument_id).content_sha256,
        }
        for m in instruments
    )
    parameters = {
        k: str(v) for k, v in spec.model_dump(mode="json").items() if k != "research_status"
    }
    cost_basis: dict[str, Any] = {
        "fee_schedule": ctx.fees.schedule_id,
        "fee_schedule_file": cfg.fee_schedule,
        "fills": cfg.fills.model_dump(mode="json"),
        "stress": cfg.stress.model_dump(mode="json"),
        "return_basis": "EXECUTABLE (fills cross the assumed spread; spread charged once)",
        "operating_costs_usd_per_month": str(cfg.operating_costs_usd_per_month),
    }
    if cfg.diagnostic_capitals:  # opt-in: absent keys keep earlier registration hashes
        cost_basis["diagnostic_capitals_usd"] = [str(c) for c in cfg.diagnostic_capitals]
    splits: dict[str, Any] = cfg.splits.model_dump(mode="json")
    if cfg.warmup_sessions is not None:
        splits["warmup_sessions"] = cfg.warmup_sessions
    return TrialRegistration(
        trial_id=trial.trial_id,
        study_id=cfg.study_id,
        family=spec_family(spec),
        track=cfg.track,
        hypothesis=trial.hypothesis,
        universe=tuple(m.execution_instrument_id for m in instruments),
        universe_selection=cfg.universe_selection,
        horizon=cfg.horizon,
        rule={
            "strategy_id": spec.strategy_id,
            "spec_file": trial.spec_file,
            "overrides": dict(trial.overrides),
            "spec_hash": spec.spec_hash,
        },
        features=_registered_features(spec),
        feature_version=spec.feature_version,
        entry_exit_timing=cfg.entry_exit_timing,
        cost_basis=cost_basis,
        parameters=parameters,
        splits=splits,
        metrics=(
            "net USD PnL per session (zero-return sessions included)",
            "net expectancy per trade",
            "drawdown and time underwater",
            "exposure, turnover, fees, execution cost",
            "win/loss distribution, payoff ratio, profit factor",
            "rejected and unfilled candidates by reason",
        ),
        selection_correction=SELECTION_RULE,
        datasets=datasets,
    )


# ----------------------------------------------------------------------------- running


def _zero_fees(currency: str) -> FeeSchedule:
    return fee_schedule_from_config(
        FeeScheduleConfig(
            schedule_id="diagnostic-zero-commission",
            description="DIAGNOSTIC ONLY - zero commission; not an achievable cost",
            evidence=EvidenceLevel.REPLAY_SIMULATED,
            known=True,
            source="research diagnostic",
            as_of="2026-11-07",
            currency=currency,
        )
    )


def _fills(cfg: StudyConfig, view: str) -> FillAssumptions:
    base = FillAssumptions(
        label="base",
        half_spread_bps=cfg.fills.half_spread_bps,
        slippage_bps=cfg.fills.slippage_bps,
        participation=cfg.fills.participation,
        latency_ms=cfg.fills.latency_ms,
    )
    if view == "stressed":
        stressed = base.stressed(cfg.stress.multiplier, "stressed")
        return FillAssumptions(
            label="stressed",
            half_spread_bps=stressed.half_spread_bps,
            slippage_bps=stressed.slippage_bps,
            participation=stressed.participation,
            latency_ms=stressed.latency_ms,
            multiplier=stressed.multiplier,
            missed_entry_percent=cfg.stress.missed_entry_percent,
        )
    return base


@dataclass(frozen=True, slots=True)
class LoadedData:
    setups: dict[str, InstrumentSetup]
    start: datetime
    end: datetime


def load_development_data(
    ctx: StudyContext, *, uses_source_vwap: bool, unlocked: bool
) -> LoadedData:
    """The development period's bars, plus ``warmup_sessions`` of earlier bars when the study
    declares them (features only: no decision before the development start is admitted)."""
    cfg = ctx.config
    plan = cfg.splits
    start, end = plan.development_start, plan.holdout_start
    first = start if cfg.warmup_sessions is None else warmup_start(ctx, start)
    guard_window(plan, first, end, unlocked=unlocked)
    ds = ctx.manifest.dataset(cfg.price_dataset)
    splits = split_events(ds)
    setups: dict[str, InstrumentSetup] = {}
    for m in cfg.instruments:
        raw = load_bars(ds, m.price_instrument_id, ctx.repo_root, start=first, end=end)
        bars = to_domain_bars(
            raw,
            instrument_id=m.execution_instrument_id,
            feed=ds.feed,
            finalization_delay=timedelta(seconds=1),
            require_source_vwap=uses_source_vwap,
        )
        setups[m.execution_instrument_id] = InstrumentSetup(
            spec=ctx.specs[m.execution_instrument_id],
            bars=bars,
            splits=tuple(s for s in splits if s.instrument_id == m.price_instrument_id),
        )
    return LoadedData(setups=setups, start=start, end=end)


WARMUP = timedelta(days=2)


def warmup_start(ctx: StudyContext, start: datetime) -> datetime:
    """Where the feature warm-up before a deployment starting at ``start`` begins.

    Without ``warmup_sessions``: ``start - WARMUP`` (ADR-0020). With it: the open of the
    ``warmup_sessions``-th exchange session before ``start`` (sessions whose open is before
    ``start``), so exactly that many sessions of bars precede the window; a UTC-day study counts
    UTC days.
    """
    sessions = ctx.config.warmup_sessions
    if sessions is None:
        return start - WARMUP
    calendar = ctx.calendar
    if not isinstance(calendar, ExchangeCalendar):
        day = datetime(start.year, start.month, start.day, tzinfo=start.tzinfo)
        return day - timedelta(days=sessions)
    return calendar.session_open_before(start, sessions)


def _slice(bars: Sequence[Bar], lo: datetime, hi: datetime) -> list[Bar]:
    """Bars with ``lo <= start`` and ``end <= hi`` (bars are sorted by start)."""
    starts = [b.start for b in bars]
    i = bisect_left(starts, lo)
    j = bisect_left(starts, hi)
    return [b for b in bars[i:j] if b.end <= hi]


def run_trial(
    ctx: StudyContext,
    trial: TrialCfg,
    data: LoadedData,
    view: Literal["base", "stressed", "diagnostic"],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    policy: LoadedPolicy | None = None,
) -> EngineResult:
    """One deployment of a trial from a fresh ledger over ``[start, end)``.

    The ledger starts at the policy's ``initial_trading_capital`` (USD 50; a capital view
    passes a scaled ``policy``). A trial that declares ``instruments`` sees only those
    instruments' bars. Bars from ``warmup_start`` feed the features only; entries are
    admitted only for decisions inside the window (an overnight rule: entries whose next open is
    inside it), and a position still open at the window end is closed at the last bar's
    estimated bid less slippage (``END_OF_DATA``).
    """
    cfg = ctx.config
    window_start = start or data.start
    window_end = end or data.end
    spec = resolve_rule(ctx, trial)
    ticks = {iid: s.spec.price_tick for iid, s in data.setups.items()}
    strategy = build_strategy(ctx, spec, ticks)
    fees = _zero_fees(ctx.fees.currency) if view == "diagnostic" else ctx.fees
    lo = warmup_start(ctx, window_start)
    traded = (
        None
        if trial.instruments is None
        else {m.execution_instrument_id for m in trial_instruments(cfg, trial)}
    )
    setups = {
        iid: InstrumentSetup(
            spec=setup.spec,
            bars=_slice(setup.bars, lo, window_end),
            splits=setup.splits,
        )
        for iid, setup in data.setups.items()
        if traded is None or iid in traded
    }
    config = EngineConfig(
        policy=ctx.policy.policy,
        fees=fees,
        fills=_fills(cfg, "stressed" if view == "stressed" else "base"),
        track=cfg.track,
        calendar=ctx.calendar,
        start=window_start,
        end=window_end,
        loss_windows=cfg.loss_windows,
        session_basis=cfg.session_basis,
    )
    if policy is not None:  # a diagnostic capital view
        config = replace(
            config, policy=policy.policy, initial_capital=policy.policy.initial_trading_capital
        )
    return ResearchEngine(config, strategy, setups).run()


def deployment_windows(plan: SplitPlan) -> list[tuple[datetime, datetime]]:
    """Consecutive windows of ``step_months`` covering the development period.

    With the default schedule every validation and test window of every fold is exactly one
    of these windows, so each out-of-sample window is evaluated as a fresh deployment.
    """
    out: list[tuple[datetime, datetime]] = []
    start = plan.development_start
    while start < plan.holdout_start:
        end = min(add_months(start, plan.walk_forward.step_months), plan.holdout_start)
        out.append((start, end))
        start = end
    return out


@dataclass(frozen=True, slots=True)
class TrialRun:
    series: SessionSeries
    trades: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    diagnostics: dict[str, int]
    pauses: list[dict[str, str]]
    window_nets: list[float]
    window_trades: list[int]
    result_hash: str


def run_windows(
    ctx: StudyContext,
    trial: TrialCfg,
    data: LoadedData,
    view: Literal["base", "stressed", "diagnostic"],
    windows: Sequence[tuple[datetime, datetime]],
    *,
    policy: LoadedPolicy | None = None,
) -> TrialRun:
    """Run each window as an independent deployment and concatenate the session series.

    ``policy`` (a capital view) replaces the study policy and starts every window from its
    ``initial_trading_capital`` instead of USD 50.
    """
    cfg = ctx.config
    capital = Decimal(50) if policy is None else policy.policy.initial_trading_capital
    parts: list[SessionSeries] = []
    trades: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    diagnostics: dict[str, int] = {}
    pauses: list[dict[str, str]] = []
    nets: list[float] = []
    counts: list[int] = []
    hashes: list[str] = []
    for start, end in windows:
        result = run_trial(ctx, trial, data, view, start=start, end=end, policy=policy)
        days = session_dates(cfg.session_basis, ctx.calendar, start, end)
        part = session_series(result, days, capital)
        parts.append(part)
        nets.append(sum(part.pnl))
        counts.append(len(result.trades))
        trades += _jsonable([t.as_dict() for t in result.trades])
        candidates += _jsonable([c.as_dict() for c in result.candidates])
        for key, value in result.diagnostics.items():
            if isinstance(value, int):
                diagnostics[key] = diagnostics.get(key, 0) + value
        pauses += [{**p, "window": iso_utc(start)} for p in result.pauses]
        hashes.append(result.result_hash)
    series = SessionSeries(
        days=tuple(d for p in parts for d in p.days),
        pnl=tuple(v for p in parts for v in p.pnl),
        equity=tuple(v for p in parts for v in p.equity),
        trades_closed=tuple(v for p in parts for v in p.trades_closed),
        realized=tuple(v for p in parts for v in p.realized),
        exposure_minutes=tuple(v for p in parts for v in p.exposure_minutes),
        turnover=tuple(v for p in parts for v in p.turnover),
    )
    return TrialRun(
        series, trades, candidates, diagnostics, pauses, nets, counts, content_hash(hashes)
    )


# ----------------------------------------------------------------------------- series


@dataclass(frozen=True, slots=True)
class SessionSeries:
    days: tuple[date, ...]
    pnl: tuple[float, ...]
    equity: tuple[float, ...]
    trades_closed: tuple[int, ...]
    realized: tuple[float, ...]
    exposure_minutes: tuple[int, ...]
    turnover: tuple[float, ...]

    def window(self, start: datetime, end: datetime) -> SessionSeries:
        keep = [i for i, d in enumerate(self.days) if start.date() <= d < end.date()]
        return SessionSeries(
            days=tuple(self.days[i] for i in keep),
            pnl=tuple(self.pnl[i] for i in keep),
            equity=tuple(self.equity[i] for i in keep),
            trades_closed=tuple(self.trades_closed[i] for i in keep),
            realized=tuple(self.realized[i] for i in keep),
            exposure_minutes=tuple(self.exposure_minutes[i] for i in keep),
            turnover=tuple(self.turnover[i] for i in keep),
        )


class HasSessions(Protocol):
    """An engine result as :func:`session_series` reads it: its per-session statistics."""

    @property
    def sessions(self) -> Sequence[SessionStats]: ...


def session_series(result: HasSessions, days: Sequence[date], capital: Decimal) -> SessionSeries:
    by_day = {s.session: s for s in result.sessions}
    equity = capital
    pnl: list[float] = []
    eq: list[float] = []
    closed: list[int] = []
    realized: list[float] = []
    exposure: list[int] = []
    turnover: list[float] = []
    for d in days:
        stats = by_day.get(d.isoformat())
        new_equity = stats.end_equity if stats is not None else equity
        pnl.append(float(new_equity - equity))
        eq.append(float(new_equity))
        equity = new_equity
        closed.append(stats.trades_closed if stats else 0)
        realized.append(float(stats.realized_net) if stats else 0.0)
        exposure.append(stats.exposure_minutes if stats else 0)
        turnover.append(float(stats.turnover_usd) if stats else 0.0)
    return SessionSeries(
        tuple(days),
        tuple(pnl),
        tuple(eq),
        tuple(closed),
        tuple(realized),
        tuple(exposure),
        tuple(turnover),
    )


def summarize(
    ctx: StudyContext,
    series: SessionSeries,
    trades: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]] | None = None,
    *,
    seed_offset: int = 0,
    capital: Decimal = Decimal(50),
) -> dict[str, Any]:
    """Session and trade statistics; ``capital`` is the starting equity of each deployment
    (USD 50; a capital view passes its own) for the turnover multiple and the drawdown."""
    cfg = ctx.config
    utc_day = cfg.session_basis == "UTC_DAY"
    return summarize_series(
        series,
        trades,
        candidates,
        bootstrap=cfg.bootstrap,
        session_minutes=1440 if utc_day else 390,
        sessions_per_month=30.4375 if utc_day else 21.0,
        annualisation_sessions=cfg.annualisation_sessions,
        operating_costs_usd_per_month=cfg.operating_costs_usd_per_month,
        seed_offset=seed_offset,
        capital=capital,
    )


def summarize_series(
    series: SessionSeries,
    trades: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]] | None = None,
    *,
    bootstrap: BootstrapCfg,
    session_minutes: int,
    sessions_per_month: float,
    annualisation_sessions: int,
    operating_costs_usd_per_month: Decimal,
    seed_offset: int = 0,
    capital: Decimal = Decimal(50),
) -> dict[str, Any]:
    """The statistics of :func:`summarize` from their plain inputs, so that a study kind with its
    own configuration (``research/cfd_study.py``) computes them with the same code.
    ``session_minutes`` is the length of one session for the exposure fraction and
    ``sessions_per_month`` converts the monthly operating cost into a cost per session."""
    boot = bootstrap
    x = as_array(series.pnl)
    n = len(x)
    nets = [float(t["net_pnl"]) for t in trades]
    wins = [v for v in nets if v > 0]
    losses = [v for v in nets if v <= 0]
    out: dict[str, Any] = {
        "sessions": n,
        "first_session": series.days[0].isoformat() if n else None,
        "last_session": series.days[-1].isoformat() if n else None,
        "active_sessions": sum(1 for c in series.trades_closed if c > 0),
        "trades": len(nets),
        "net_pnl_usd": float(x.sum()) if n else 0.0,
        "realized_net_usd": sum(nets),
        "gross_pnl_usd": sum(float(t["gross_pnl"]) for t in trades),
        "fees_usd": sum(float(t["fees"]) for t in trades),
        "execution_cost_usd": sum(float(t["execution_cost"]) for t in trades),
        "operating_costs_usd": float(operating_costs_usd_per_month) * n / sessions_per_month,
        "mean_session_pnl_usd": float(x.mean()) if n else 0.0,
        "sd_session_pnl_usd": float(x.std(ddof=1)) if n > 1 else 0.0,
        "win_rate": len(wins) / len(nets) if nets else None,
        "avg_win_usd": sum(wins) / len(wins) if wins else None,
        "avg_loss_usd": sum(losses) / len(losses) if losses else None,
        "payoff_ratio": (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
        if wins and losses and sum(losses) != 0
        else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else None,
        "net_expectancy_per_trade_usd": sum(nets) / len(nets) if nets else None,
        "exposure_fraction": sum(series.exposure_minutes) / (n * session_minutes) if n else 0.0,
        "turnover_multiple": sum(series.turnover) / float(capital) if n else 0.0,
        "exit_reasons": _histogram(t["exit_reason"] for t in trades),
        "ambiguous_bar_stops": sum(1 for t in trades if t.get("ambiguous_bar")),
        "entry_bar_stops": sum(1 for t in trades if t.get("stopped_in_entry_bar")),
        "sharpe_per_session": sharpe(x) if n > 1 else 0.0,
        "sharpe_annualised": sharpe(x) * float(np.sqrt(annualisation_sessions)) if n > 1 else 0.0,
        "annualisation": f"sqrt({annualisation_sessions}) sessions per year",
    }
    out["total_project_net_pnl_usd"] = out["net_pnl_usd"] - out["operating_costs_usd"]
    out.update(drawdown_stats(float(capital) + np.concatenate([[0.0], np.cumsum(x)])))
    if candidates is not None:
        out["candidates"] = _candidate_histogram(candidates)
    if n >= 10:
        mean_block = float(boot.mean_block_sessions)
        out["mean_session_ci"] = bootstrap_mean(
            x, mean_block=mean_block, replicates=boot.replicates, seed=boot.seed + seed_offset
        ).as_dict()
        ratio = bootstrap_ratio(
            as_array(series.realized),
            as_array([float(c) for c in series.trades_closed]),
            mean_block=mean_block,
            replicates=boot.replicates,
            seed=boot.seed + seed_offset + 1,
        )
        out["per_trade_ci"] = ratio.as_dict() if ratio else None
        n_eff, vif = effective_sample_size(x)
        out["effective_sessions"] = n_eff
        out["variance_inflation"] = vif
        sd = out["sd_session_pnl_usd"]
        out["power_sessions"] = {
            "observed_mean": sessions_for_power(sd, out["mean_session_pnl_usd"], vif=vif),
            "0.01_usd_per_session": sessions_for_power(sd, 0.01, vif=vif),
            "0.05_usd_per_session": sessions_for_power(sd, 0.05, vif=vif),
        }
    return out


def _histogram(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))


def _candidate_histogram(candidates: Sequence[dict[str, Any]]) -> dict[str, Any]:
    status = _histogram(c["status"] for c in candidates)
    reasons = _histogram(r for c in candidates for r in c["reasons"])
    return {"total": len(candidates), "by_status": status, "by_reason": reasons}


# ----------------------------------------------------------------------------- comparators


def buy_and_hold_notional(ctx: StudyContext, count: int) -> Decimal:
    """Per-symbol USD notional of the policy-capped buy-and-hold comparator over ``count`` names."""
    policy = ctx.policy.policy
    capital = Decimal(50)
    fee_rate_guess = Decimal("0.02")  # conservative cushion for the reserve calculation only
    return min(
        policy.exposure.max_symbol_exposure_fraction * capital,
        policy.exposure.max_gross_exposure_fraction * capital / count,
        (capital - policy.cash_reserve_usd(capital)) / (count * (1 + 2 * fee_rate_guess)),
    )


def buy_and_hold(
    ctx: StudyContext, data: LoadedData, days: Sequence[date], start: datetime
) -> SessionSeries:
    """Same-universe, policy-constrained buy-and-hold with the same fees and fill assumptions.

    Each instrument receives the same notional, bounded by the symbol cap, the gross cap and the
    cash reserve (after entry fees and reserved exit fees); bought at the first bar opening at or
    after ``start`` at the assumed ask plus slippage and marked daily at the bid less exit fee.

    Bars are raw, so a split takes effect point-in-time as in the engine: a holding is
    multiplied by the split ratio at the open of the ex-date session (before that bar is marked).
    Without this a 10-for-1 split would be marked as a 90% loss; studies without splits (every
    crypto study) are unaffected.
    """
    fills = _fills(ctx.config, "base")
    capital = Decimal(50)
    per_symbol = buy_and_hold_notional(ctx, len(data.setups))
    cash = capital
    holdings: dict[str, Decimal] = {}
    marks: dict[str, Decimal] = {}
    by_day: dict[str, Decimal] = {}
    calendar = ctx.calendar
    splits_due: dict[str, list[tuple[datetime, Decimal]]] = (
        {
            iid: sorted((split_effective_time(s, calendar), s.ratio) for s in setup.splits)
            for iid, setup in data.setups.items()
        }
        if isinstance(calendar, ExchangeCalendar)
        else {}
    )
    timeline = sorted(
        (b for s in data.setups.values() for b in s.bars if b.start >= start),
        key=lambda b: (b.available_time, b.instrument_id),
    )
    for bar in timeline:
        spec = data.setups[bar.instrument_id].spec
        due = splits_due.get(bar.instrument_id)
        while due and due[0][0] <= bar.start:
            _, ratio = due.pop(0)
            if bar.instrument_id in holdings:  # a split before the purchase is in its price
                holdings[bar.instrument_id] *= ratio
        if bar.instrument_id not in holdings:
            price = bar.open * (1 + fills.adverse)
            qty = spec.round_quantity_down(per_symbol / price)
            notional = qty * price
            fee = ctx.fees.fee(Side.BUY, qty, notional)
            cash -= notional + fee
            holdings[bar.instrument_id] = qty
        marks[bar.instrument_id] = bar.close * (1 - fills.half_spread)
        equity = cash
        for iid, qty in holdings.items():
            mark = marks.get(iid)
            if mark is not None:
                equity += qty * mark - ctx.fees.fee(Side.SELL, qty, qty * mark)
        day = (
            calendar.session_date_of(bar.start)
            if isinstance(calendar, ExchangeCalendar)
            else bar.start.date()
        )
        key = day.isoformat()
        by_day[key] = equity
    equity = capital
    pnl: list[float] = []
    eq: list[float] = []
    for d in days:
        new = by_day.get(d.isoformat(), equity)
        pnl.append(float(new - equity))
        eq.append(float(new))
        equity = new
    zeros = tuple(0 for _ in days)
    return SessionSeries(
        tuple(days),
        tuple(pnl),
        tuple(eq),
        zeros,
        tuple(0.0 for _ in days),
        zeros,
        tuple(0.0 for _ in days),
    )


def measure_basis(ctx: StudyContext) -> list[dict[str, Any]]:
    """Proxy-versus-venue price basis on a common recent window (no strategy is evaluated).

    Reports, per instrument, the venue-minus-proxy close difference in bps and the correlation of
    15-minute log returns over bars present in both datasets.
    """
    cfg = ctx.config
    if len(cfg.basis_datasets) != 2:
        return []
    proxy = ctx.manifest.dataset(cfg.basis_datasets[0])
    venue = ctx.manifest.dataset(cfg.basis_datasets[1])
    if proxy.status != "ACQUIRED" or venue.status != "ACQUIRED":
        return [{"status": "basis datasets not acquired"}]
    out: list[dict[str, Any]] = []
    for m in cfg.instruments:
        try:
            a = {b.start: b for b in load_bars(proxy, m.price_instrument_id, ctx.repo_root)}
            b = {b.start: b for b in load_bars(venue, m.execution_instrument_id, ctx.repo_root)}
        except DataQualityError as exc:
            # The recent Kraken capture cannot be re-downloaded later (720-row endpoint).
            out.append({"instrument": m.execution_instrument_id, "status": f"unavailable: {exc}"})
            continue
        common = sorted(a.keys() & b.keys())
        if len(common) < 10:
            out.append({"instrument": m.execution_instrument_id, "common_bars": len(common)})
            continue
        diffs = as_array([float((b[t].close - a[t].close) / a[t].close) * 1e4 for t in common])
        ra: list[float] = []
        rb: list[float] = []
        step = timedelta(minutes=proxy.request.bar_minutes)
        for prev, cur in itertools.pairwise(common):
            if cur - prev == step:
                ra.append(float(np.log(float(a[cur].close) / float(a[prev].close))))
                rb.append(float(np.log(float(b[cur].close) / float(b[prev].close))))
        out.append(
            {
                "instrument": m.execution_instrument_id,
                "proxy": f"{proxy.name}:{m.price_instrument_id}",
                "venue": f"{venue.name}:{m.execution_instrument_id}",
                "window": [iso_utc(common[0]), iso_utc(common[-1])],
                "common_bars": len(common),
                "mean_close_diff_bps": float(diffs.mean()),
                "mean_abs_close_diff_bps": float(np.abs(diffs).mean()),
                "p95_abs_close_diff_bps": float(np.quantile(np.abs(diffs), 0.95)),
                "return_correlation": float(np.corrcoef(ra, rb)[0, 1]) if len(ra) > 2 else None,
            }
        )
    return out


# ----------------------------------------------------------------------------- selection


def _trades_in(
    trades: Sequence[dict[str, Any]], start: datetime, end: datetime, *, purge: bool
) -> list[dict[str, Any]]:
    out = []
    for t in trades:
        entry = datetime.fromisoformat(str(t["entry"]["time"]).replace("Z", "+00:00"))
        if not start <= entry < end:
            continue
        if purge and t["exits"]:
            exit_time = datetime.fromisoformat(str(t["exits"][-1]["time"]).replace("Z", "+00:00"))
            if exit_time >= end:
                continue
        out.append(t)
    return out


def walk_forward(
    plan: SplitPlan,
    trial_ids: Sequence[str],
    series: Mapping[str, SessionSeries],
    trades: Mapping[str, Sequence[dict[str, Any]]],
    *,
    capital: float = 50.0,
) -> tuple[SessionSeries, list[dict[str, Any]], list[dict[str, Any]]]:
    """Per fold, the trial with the best positive validation result (else cash) supplies the
    test window. ``capital`` is the equity each deployment starts from (USD 50; another study
    kind passes its own) and only sets the level of the returned equity line."""
    chosen_days: list[date] = []
    chosen_pnl: list[float] = []
    chosen_closed: list[int] = []
    chosen_realized: list[float] = []
    chosen_exposure: list[int] = []
    chosen_turnover: list[float] = []
    log: list[dict[str, Any]] = []
    selected_trades: list[dict[str, Any]] = []
    fold_list: list[Fold] = folds(plan)
    for fold in fold_list:
        v_start, v_end = fold.validation
        scores: dict[str, float] = {}
        for tid in trial_ids:
            window = series[tid].window(v_start, v_end)
            sessions = max(len(window.days), 1)
            scores[tid] = (
                sum(
                    float(t["net_pnl"]) for t in _trades_in(trades[tid], v_start, v_end, purge=True)
                )
                / sessions
            )
        best = max(trial_ids, key=lambda t: (scores[t], -trial_ids.index(t)))
        choice = best if scores[best] > 0 else "CASH"
        t_start, t_end = fold.test
        log.append(
            {
                "fold": fold.index,
                "train": [iso_utc(fold.train[0]), iso_utc(fold.train[1])],
                "validation": [iso_utc(v_start), iso_utc(v_end)],
                "test": [iso_utc(t_start), iso_utc(t_end)],
                "validation_net_per_session": scores,
                "selected": choice,
            }
        )
        base = next(iter(series.values())).window(t_start, t_end)
        if choice == "CASH":
            chosen_days += base.days
            chosen_pnl += [0.0] * len(base.days)
            chosen_closed += [0] * len(base.days)
            chosen_realized += [0.0] * len(base.days)
            chosen_exposure += [0] * len(base.days)
            chosen_turnover += [0.0] * len(base.days)
            continue
        w = series[choice].window(t_start, t_end)
        chosen_days += w.days
        chosen_pnl += w.pnl
        chosen_closed += w.trades_closed
        chosen_realized += w.realized
        chosen_exposure += w.exposure_minutes
        chosen_turnover += w.turnover
        selected_trades += _trades_in(trades[choice], t_start, t_end, purge=False)
    equity = capital
    eq: list[float] = []
    for p in chosen_pnl:
        equity += p
        eq.append(equity)
    wf = SessionSeries(
        tuple(chosen_days),
        tuple(chosen_pnl),
        tuple(eq),
        tuple(chosen_closed),
        tuple(chosen_realized),
        tuple(chosen_exposure),
        tuple(chosen_turnover),
    )
    return wf, selected_trades, log


# ----------------------------------------------------------------------------- study run


def run_baseline_study(
    ctx: StudyContext, registry: ExperimentRegistry, *, progress: Any = None
) -> dict[str, Any]:
    cfg = ctx.config
    require_known_costs(ctx)
    registrations = [build_registration(ctx, t) for t in cfg.trials]
    for reg in registrations:
        registry.require_registered(reg)
    unlocked = registry.holdout_unlocked(cfg.study_id)
    plan = cfg.splits
    windows = deployment_windows(plan)
    capital = Decimal(50)
    results: dict[str, dict[str, Any]] = {}
    series: dict[str, SessionSeries] = {}
    trades: dict[str, list[dict[str, Any]]] = {}
    data_cache: dict[bool, LoadedData] = {}
    for i, trial in enumerate(cfg.trials):
        spec = resolve_rule(ctx, trial)
        needs_vwap = spec_uses_source_vwap(spec)
        if needs_vwap not in data_cache:
            data_cache[needs_vwap] = load_development_data(
                ctx, uses_source_vwap=needs_vwap, unlocked=unlocked
            )
        data = data_cache[needs_vwap]
        views: dict[str, Any] = {}
        views_order: tuple[Literal["base", "stressed", "diagnostic"], ...] = (
            "base",
            "stressed",
            "diagnostic",
        )
        for view in views_order:
            if progress:
                progress(f"{trial.trial_id} {view} ({len(windows)} windows)")
            run = run_windows(ctx, trial, data, view, windows)
            summary = summarize(ctx, run.series, run.trades, run.candidates, seed_offset=10 * i)
            summary["result_hash"] = run.result_hash
            summary["diagnostics"] = run.diagnostics
            summary["pauses"] = run.pauses
            summary["window_nets_usd"] = run.window_nets
            summary["negative_windows"] = sum(1 for v in run.window_nets if v < 0)
            summary["window_trades"] = run.window_trades
            summary["windows"] = [iso_utc(w[0]) for w in windows]
            views[view] = summary
            if view == "base":
                series[trial.trial_id] = run.series
                trades[trial.trial_id] = run.trades
        if progress:
            progress(f"{trial.trial_id} continuous deployment")
        continuous = run_trial(ctx, trial, data, "base")
        stop = next(
            (
                p["raised"]
                for p in continuous.pauses
                if p.get("cause") in ("DRAWDOWN", "CAPITAL_FLOOR") and "raised" in p
            ),
            None,
        )
        views["continuous"] = {
            "net_pnl_usd": float(continuous.final_equity - capital),
            "trades": len(continuous.trades),
            "permanent_pause_at": stop,
            "result_hash": continuous.result_hash,
        }
        views["break_even_commission_per_side_bps"] = _break_even_bps(views["diagnostic"])
        results[trial.trial_id] = {
            "registration_hash": registrations[i].content_hash,
            "spec_hash": spec.spec_hash,
            "family": spec_family(spec),
            "overrides": dict(trial.overrides),
            "views": views,
        }
        # Opt-in keys: results of trials without them keep their shape (and report text).
        if trial.instruments is not None:
            results[trial.trial_id]["universe"] = list(registrations[i].universe)
        timing = overnight_timing(spec)
        if timing is not None:
            results[trial.trial_id]["timing"] = timing
    trial_ids = [t.trial_id for t in cfg.trials]
    wf_series, wf_trades, selection_log = walk_forward(plan, trial_ids, series, trades)
    test_start = (
        datetime.combine(
            wf_series.days[0], datetime.min.time(), tzinfo=plan.development_start.tzinfo
        )
        if wf_series.days
        else plan.development_start
    )
    data_any = next(iter(data_cache.values()))
    bh = buy_and_hold(ctx, data_any, wf_series.days, test_start)
    wf_summary = summarize(ctx, wf_series, wf_trades, seed_offset=1000)
    bh_summary = summarize(ctx, bh, [], seed_offset=2000)
    bh_summary["per_symbol_notional_usd"] = float(buy_and_hold_notional(ctx, len(data_any.setups)))
    cash_summary = {"net_pnl_usd": 0.0, "sessions": len(wf_series.days), "trades": 0}
    # selection-aware checks over the union of test windows, all study trials
    matrix = (
        np.vstack(
            [as_array(series[tid].window(test_start, plan.holdout_start).pnl) for tid in trial_ids]
        )
        if wf_series.days
        else np.zeros((len(trial_ids), 0))
    )
    rc = (
        reality_check(
            matrix,
            mean_block=float(cfg.bootstrap.mean_block_sessions),
            replicates=cfg.bootstrap.replicates,
            seed=cfg.bootstrap.seed + 3000,
        )
        if matrix.shape[1] >= 10
        else None
    )
    trial_sharpes = [sharpe(row) for row in matrix] if matrix.shape[1] > 1 else []
    n_registered = len(registry.registrations())
    best_row = matrix[int(np.argmax(matrix.mean(axis=1)))] if matrix.shape[1] > 1 else as_array([])
    dsr = (
        deflated_sharpe(best_row, trial_sharpes=trial_sharpes, n_trials=n_registered)
        if len(best_row) > 2
        else None
    )
    policy = ctx.policy.policy

    def _triggers(pnl: Sequence[float], seed: int) -> dict[str, float] | None:
        if len(pnl) < 10:
            return None
        return loss_trigger_probabilities(
            as_array(pnl),
            capital=50.0,
            horizon=90,
            daily_limit=float(policy.loss_limits.daily_loss_fraction * capital),
            weekly_limit=float(policy.loss_limits.weekly_loss_fraction * capital),
            drawdown_fraction=float(policy.loss_limits.max_high_water_drawdown_fraction),
            floor=float(policy.loss_limits.initial_capital_pause_floor),
            week_sessions=7 if cfg.session_basis == "UTC_DAY" else 5,
            mean_block=float(cfg.bootstrap.mean_block_sessions),
            replicates=cfg.bootstrap.replicates,
            seed=seed,
        )

    for k, tid in enumerate(trial_ids):
        results[tid]["views"]["loss_triggers"] = _triggers(
            series[tid].pnl, cfg.bootstrap.seed + 5000 + k
        )
    triggers = (
        loss_trigger_probabilities(
            as_array(wf_series.pnl),
            capital=50.0,
            horizon=60,
            daily_limit=float(policy.loss_limits.daily_loss_fraction * capital),
            weekly_limit=float(policy.loss_limits.weekly_loss_fraction * capital),
            drawdown_fraction=float(policy.loss_limits.max_high_water_drawdown_fraction),
            floor=float(policy.loss_limits.initial_capital_pause_floor),
            week_sessions=7 if cfg.session_basis == "UTC_DAY" else 5,
            mean_block=float(cfg.bootstrap.mean_block_sessions),
            replicates=cfg.bootstrap.replicates,
            seed=cfg.bootstrap.seed + 4000,
        )
        if len(wf_series.pnl) >= 10
        else None
    )
    regimes = _regimes(bh, wf_series)
    gate = _g2(wf_summary, rc)
    # Diagnostic capital views run only after selection and the gate are final; nothing above
    # reads them (tests/test_capital_views.py).
    views_by_capital = (
        capital_views(ctx, lambda t: data_cache[spec_uses_source_vwap(resolve_rule(ctx, t))])
        if cfg.diagnostic_capitals
        else None
    )
    out: dict[str, Any] = {
        "study_id": cfg.study_id,
        "title": cfg.title,
        "track": cfg.track.value,
        "universe": [m.execution_instrument_id for m in cfg.instruments],
        "disclosures": {
            "universe_selection": cfg.universe_selection,
            "horizon": cfg.horizon,
            "entry_exit_timing": cfg.entry_exit_timing,
        },
        "trial_budget": registry.budget(),
        "evidence_class": "BACKTEST (bar-based historical replay) - not paper, shadow or live evidence",
        "policy_hash": ctx.policy.policy_hash,
        "fee_schedule": ctx.fees.schedule_id,
        "fee_schedule_description": ctx.fees.config.description,
        "fills": {
            "base": _fills(cfg, "base").describe(),
            "stressed": _fills(cfg, "stressed").describe(),
        },
        "splits": plan.describe(),
        "window_months": plan.walk_forward.step_months,
        "holdout_unlocked": unlocked,
        "trials": results,
        "walk_forward": {
            "selection_rule": SELECTION_RULE,
            "folds": selection_log,
            "summary": wf_summary,
        },
        "comparators": {"cash": cash_summary, "buy_and_hold": bh_summary},
        "selection_aware": {
            "reality_check": rc,
            "deflated_sharpe": dsr,
            "registered_trials": n_registered,
        },
        "loss_trigger_probabilities": triggers,
        "regimes": regimes,
        "g2": gate,
        "basis": measure_basis(ctx),
        "datasets": _dataset_rows(ctx),
    }
    if views_by_capital is not None:  # opt-in: a study without capitals keeps its result shape
        out["capital_views"] = views_by_capital
    if cfg.warmup_sessions is not None:  # opt-in, as above
        out["warmup_sessions"] = cfg.warmup_sessions
    return out


def overnight_timing(spec: AnyStudySpec) -> dict[str, str] | None:
    """A multi-day rule's declared timing for the report (``None`` for intraday rules).

    Read from the frozen specification, which is where the engine reads the declarations of
    the registered daily families (``RuleTiming``); the entry offset is the spec's own.
    """
    if getattr(spec, "holding", None) != "OVERNIGHT":
        return None
    sessions = getattr(spec, "max_hold_sessions", None)
    return {
        "holding": "OVERNIGHT",
        "max_hold_sessions": "" if sessions is None else str(sessions),
        "target_role": str(getattr(spec, "target_role", None) or "EXIT"),
        "entry_limit_offset_bps": str(getattr(spec, "entry_limit_offset_bps", "")),
    }


# ----------------------------------------------------------------------------- capital views

CAPITAL_VIEW_NOTE = (
    "DIAGNOSTIC ONLY - each trial's base view re-run from another starting capital, with every "
    "USD amount of the risk policy scaled by capital / initial_trading_capital (the fractions "
    "then give the per-trade risk, symbol and gross caps, cash reserve and loss-window limits "
    "at that capital). Same rules, data, windows, fees and fills. Never used by walk-forward "
    "selection, the selection-aware statistics or gate G2."
)

#: The statistics kept per capital view (no confidence intervals: a view is not evidence).
CAPITAL_VIEW_KEYS = (
    "sessions",
    "trades",
    "net_pnl_usd",
    "gross_pnl_usd",
    "fees_usd",
    "execution_cost_usd",
    "mean_session_pnl_usd",
    "win_rate",
    "profit_factor",
    "net_expectancy_per_trade_usd",
    "max_drawdown_usd",
    "max_drawdown_fraction",
    "exposure_fraction",
    "turnover_multiple",
    "sharpe_annualised",
    "exit_reasons",
    "candidates",
)


def capital_views(
    ctx: StudyContext,
    data_for: Callable[[TrialCfg], LoadedData],
    windows: Sequence[tuple[datetime, datetime]] | None = None,
) -> dict[str, Any]:
    """Every trial's base view at each ``diagnostic_capitals`` amount (diagnostic only).

    ``data_for(trial)`` returns the trial's loaded development data (the same object its base
    view used). The result is self-contained and is read by the report only.
    """
    cfg = ctx.config
    deployment = list(windows) if windows is not None else deployment_windows(cfg.splits)
    trials: dict[str, dict[str, Any]] = {}
    policies = {str(c): scale_policy(ctx.policy, c) for c in cfg.diagnostic_capitals}
    for i, trial in enumerate(cfg.trials):
        data = data_for(trial)
        per_capital: dict[str, Any] = {}
        for capital in cfg.diagnostic_capitals:
            policy = policies[str(capital)]
            run = run_windows(ctx, trial, data, "base", deployment, policy=policy)
            summary = summarize(
                ctx, run.series, run.trades, run.candidates, seed_offset=10 * i, capital=capital
            )
            view = {k: summary[k] for k in CAPITAL_VIEW_KEYS if k in summary}
            view["capital_usd"] = str(capital)
            view["net_return_on_capital"] = summary["net_pnl_usd"] / float(capital)
            view["fees_per_trade_usd"] = (
                summary["fees_usd"] / summary["trades"] if summary["trades"] else None
            )
            view["window_nets_usd"] = run.window_nets
            view["negative_windows"] = sum(1 for v in run.window_nets if v < 0)
            view["policy_hash"] = policy.policy_hash
            view["result_hash"] = run.result_hash
            per_capital[str(capital)] = view
        trials[trial.trial_id] = per_capital
    return {
        "note": CAPITAL_VIEW_NOTE,
        "base_capital_usd": str(ctx.policy.policy.initial_trading_capital),
        "capitals_usd": [str(c) for c in cfg.diagnostic_capitals],
        "scaled_policy_fields": [".".join(path) for path in USD_AMOUNT_FIELDS],
        "trials": trials,
    }


def _dataset_rows(ctx: StudyContext) -> list[dict[str, Any]]:
    names = [ctx.config.price_dataset, *ctx.config.basis_datasets]
    rows: list[dict[str, Any]] = []
    for name in names:
        ds = ctx.manifest.dataset(name)
        for f in ds.files:
            rows.append(
                {
                    "dataset": name,
                    "source": ds.source,
                    "feed": ds.feed,
                    "instrument": f.instrument_id,
                    "rows": f.rows,
                    "first": iso_utc(f.first_start) if f.first_start else None,
                    "last": iso_utc(f.last_start) if f.last_start else None,
                    "coverage": f.quality.get("coverage"),
                    "missing_intervals": f.quality.get("missing_intervals"),
                    "largest_gaps": f.quality.get("missing_ranges_first", [])[:3],
                    "content_sha256": f.content_sha256,
                    "fields_not_supplied": list(ds.fields_not_supplied),
                }
            )
    return rows


def _break_even_bps(diag: Mapping[str, Any]) -> float | None:
    """Per-side commission (bps of traded notional) at which the zero-commission run nets zero.

    Approximate: sizing would shrink as commissions grow, so this is an upper bound on the
    affordable fee level, never an estimate of achievable profit.
    """
    traded = float(diag["turnover_multiple"]) * 50.0
    if traded <= 0:
        return None
    return float(diag["realized_net_usd"]) / traded * 1e4


def _jsonable(rows: Sequence[dict[str, object]]) -> list[dict[str, Any]]:
    import json

    from trader.domain.hashing import canonical_json

    return [json.loads(canonical_json(r)) for r in rows]


def _regimes(benchmark: SessionSeries, strategy: SessionSeries) -> dict[str, Any]:
    """Descriptive (ex post) session regimes from the buy-and-hold benchmark's daily moves."""
    if len(benchmark.pnl) < 10:
        return {}
    bench = as_array(benchmark.pnl)
    absmove = np.abs(bench)
    hi_vol = float(np.quantile(absmove, 0.8))
    lo, hi = float(np.quantile(bench, 1 / 3)), float(np.quantile(bench, 2 / 3))
    labels = []
    for v in bench:
        if abs(v) >= hi_vol:
            labels.append("high_volatility")
        elif v <= lo:
            labels.append("adverse")
        elif v >= hi:
            labels.append("trending_up")
        else:
            labels.append("choppy")
    strat = as_array(strategy.pnl)
    out: dict[str, Any] = {
        "definition": "ex-post terciles/quintile of buy-and-hold daily PnL (descriptive only)"
    }
    for name in ("trending_up", "choppy", "adverse", "high_volatility"):
        mask = np.array([lab == name for lab in labels])
        out[name] = {
            "sessions": int(mask.sum()),
            "strategy_net_usd": float(strat[mask].sum()) if mask.any() else 0.0,
            "benchmark_net_usd": float(bench[mask].sum()) if mask.any() else 0.0,
        }
    return out


def _g2(wf: Mapping[str, Any], rc: Mapping[str, Any] | None) -> dict[str, Any]:
    ci = wf.get("mean_session_ci") or {}
    per_trade = wf.get("per_trade_ci") or {}
    checks = {
        "oos_trades_at_least_100": wf.get("trades", 0) >= 100,
        "sessions_at_least_120": wf.get("sessions", 0) >= 120,
        "effective_sessions_at_least_120": (wf.get("effective_sessions") or 0) >= 120,
        "positive_lower_bound_session_net": (ci.get("one_sided_lower") or -1) > 0,
        "positive_lower_bound_per_trade_net": (per_trade.get("one_sided_lower") or -1) > 0,
        "selection_adjusted_lower_bound_positive": bool(rc)
        and (rc or {}).get("selection_adjusted_lower_bound", -1) > 0,
    }
    return {
        "status": "PASSED" if all(checks.values()) else "NOT_PASSED",
        "checks": checks,
        "note": "Backtest evidence only; G2 also requires review, and G3 forward evidence follows.",
    }


def result_digest(result: Mapping[str, Any]) -> str:
    """SHA-256 of the result's JSON form (statistics are floats, so not ``content_hash``)."""
    import json

    from trader.domain.hashing import sha256_hex

    return sha256_hex(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str))
