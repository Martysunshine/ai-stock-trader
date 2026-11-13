"""CFD studies on the broker's own quotes (``kind: cfd_study``, ADR-0052).

A study file names a two-sided dataset (``data/quote_bars.py``), the instruments, the instrument
specs, fee schedule and research policy, the cost views and the trials. ``trader research
register`` registers its trials in the shared registry (same budget, same chain);
``trader research cfd`` runs them and writes ``runs/research/<study_id>/results.json`` and a
report.

The run mirrors ``study.run_baseline_study``: each three-month deployment window of the
development period is an independent run from the sleeve's capital, in a base, a stressed and a
diagnostic view; one continuous run compounds over the whole period; walk forward chooses per
fold on the validation window and keeps the test windows; the selection-aware statistics and the
G2 checks are the shared ones. Daily bars are built from the hourly dataset (one bar per UTC day
with the weekend hours joined to Monday where the market closes then). The locked final test
period is never read unless the registry records it unlocked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import Field

from trader.accounting.fees import FeeSchedule, load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import LoadedPolicy, load_policy, load_yaml_strict
from trader.data.calendars import (
    BrokerCfdCalendar,
    SessionCalendar,
    Utc247Calendar,
    closed_weekdays,
)
from trader.data.manifest import DataManifest, HistoryDataset
from trader.data.quote_bars import QuoteBar, daily_from_hourly, load_quote_bars
from trader.domain.base import ConfigModel
from trader.domain.hashing import content_hash
from trader.domain.instruments import CommodityInstrumentSpec
from trader.domain.types import NonNegDec, PosDec, Track, iso_utc
from trader.research.cfd_engine import (
    CfdCosts,
    CfdEngineConfig,
    CfdEngineResult,
    CfdInstrument,
    charge_instants,
    run_cfd_engine,
    session_of,
)
from trader.research.registry import ExperimentRegistry, TrialRegistration
from trader.research.splits import SplitPlan, guard_window
from trader.research.stats import as_array, deflated_sharpe, reality_check, sharpe
from trader.research.study import (
    SELECTION_RULE,
    BootstrapCfg,
    SessionSeries,
    _g2,
    _regimes,
    deployment_windows,
    session_series,
    summarize_series,
    walk_forward,
)
from trader.strategies.cfd_rules import (
    CfdHourlyChannelSpec,
    CfdSpec,
    CfdWeeklyMomentumSpec,
    build_cfd_rule,
    load_cfd_spec,
)

View = Literal["base", "stressed", "diagnostic"]
VIEWS: tuple[View, ...] = ("base", "stressed", "diagnostic")
KIND = "cfd_study"


class CfdStudyError(ValueError):
    """The study cannot be registered or run as written."""


# ----------------------------------------------------------------------------- configuration


class CfdInstrumentCfg(ConfigModel):
    instrument_id: str
    #: Instrument id (without side suffix) whose bars convert the quote currency to USD.
    fx_instrument_id: str | None = None


class CfdCostsCfg(ConfigModel):
    slippage_bps: NonNegDec
    #: Full spread per instrument, in bps of the mid, below which a quoted spread is not believed.
    spread_floor_bps: dict[str, NonNegDec]


class CfdStressCfg(ConfigModel):
    spread_multiplier: PosDec
    slippage_multiplier: PosDec
    financing_charge_multiplier: PosDec
    financing_credits: bool
    missed_entry_percent: int = Field(ge=0, le=50)


class CfdBarsCfg(ConfigModel):
    #: The dataset's hourly bars. Without ``decision_minutes`` they are aggregated to one bar
    #: per UTC day, on which a daily rule decides.
    source_minutes: Literal[60]
    calendar: Literal["BROKER_CFD", "UTC_24_7"]
    #: Weekdays without a bar in runs of at most this many are the market's closures.
    closed_run_max_weekdays: int = Field(ge=0, le=5)
    #: 60: the rule decides on the hourly bars themselves (ADR-0055). Left out of a daily
    #: study's registration, which therefore reads as it did before this field existed.
    decision_minutes: Literal[60] | None = None


class CfdTrialCfg(ConfigModel):
    trial_id: str = Field(pattern=r"^T-\d{4}$")
    spec_file: str
    hypothesis: str = Field(min_length=20)


class CfdStudyConfig(ConfigModel):
    kind: Literal["cfd_study"]
    study_id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str
    track: Literal["COMMODITIES"]
    description: str
    quote_dataset: str
    bars: CfdBarsCfg
    instruments: tuple[CfdInstrumentCfg, ...] = Field(min_length=1)
    instrument_specs: str
    fee_schedule: str
    policy: str
    costs: CfdCostsCfg
    stress: CfdStressCfg
    #: Calendar days of bars fed to the rule before each window starts (indicator warm-up).
    warmup_days: int = Field(ge=0)
    splits: SplitPlan
    bootstrap: BootstrapCfg
    annualisation_sessions: int = Field(ge=1)
    universe_selection: str
    horizon: str
    entry_exit_timing: str
    known_before_registration: str
    selection_correction: str = SELECTION_RULE
    trials: tuple[CfdTrialCfg, ...] = Field(min_length=1)


def is_cfd_study(path: Path) -> bool:
    try:
        raw = load_yaml_strict(path)
    except Exception:  # an unreadable file is not this kind; the caller reports it
        return False
    return isinstance(raw, dict) and raw.get("kind") == KIND


def load_cfd_study(path: Path) -> CfdStudyConfig:
    return CfdStudyConfig.model_validate(load_yaml_strict(path))


@dataclass(frozen=True, slots=True)
class CfdStudyContext:
    repo_root: Path
    config: CfdStudyConfig
    dataset: HistoryDataset
    specs: Mapping[str, CommodityInstrumentSpec]
    fees: FeeSchedule
    policy: LoadedPolicy
    rules: Mapping[str, CfdSpec]


def open_cfd_study(
    repo_root: Path, config: CfdStudyConfig, manifest: DataManifest
) -> CfdStudyContext:
    dataset = manifest.dataset(config.quote_dataset)
    if dataset.source != "capital_demo_prices":
        raise CfdStudyError(f"{dataset.name} is not a two-sided Capital.com dataset")
    if dataset.status != "ACQUIRED":
        raise CfdStudyError(f"{dataset.name} is {dataset.status}: acquire it first")
    if dataset.request.bar_minutes != config.bars.source_minutes:
        raise CfdStudyError(f"{dataset.name} has {dataset.request.bar_minutes}-minute bars")
    if dataset.request.calendar != config.bars.calendar:
        raise CfdStudyError(
            f"{dataset.name} is on {dataset.request.calendar}, not {config.bars.calendar}"
        )
    catalogue = load_instruments(repo_root / config.instrument_specs)
    specs: dict[str, CommodityInstrumentSpec] = {}
    for inst in config.instruments:
        for iid in (inst.instrument_id, inst.fx_instrument_id):
            if iid is None:
                continue
            spec = catalogue.get(iid)
            if not isinstance(spec, CommodityInstrumentSpec):
                raise CfdStudyError(f"{iid} has no CFD spec in {config.instrument_specs}")
            specs[iid] = spec
        spec = specs[inst.instrument_id]
        if spec.currency != "USD" and inst.fx_instrument_id is None:
            raise CfdStudyError(
                f"{inst.instrument_id} is quoted in {spec.currency}: name its FX bars"
            )
        if spec.financing is None:
            raise CfdStudyError(f"{inst.instrument_id}: financing unknown (NO_TRADE)")
        if inst.instrument_id not in config.costs.spread_floor_bps:
            raise CfdStudyError(f"{inst.instrument_id} has no registered spread floor")
    fees = load_fee_schedule(repo_root / config.fee_schedule)
    if not fees.known:
        raise CfdStudyError(f"fee schedule {fees.schedule_id} is unknown: NO_TRADE")
    rules = {t.trial_id: load_cfd_spec(repo_root / t.spec_file) for t in config.trials}
    return CfdStudyContext(
        repo_root=repo_root,
        config=config,
        dataset=dataset,
        specs=specs,
        fees=fees,
        policy=load_policy(repo_root / config.policy),
        rules=rules,
    )


# ----------------------------------------------------------------------------- registration


def _specs_hash(ctx: CfdStudyContext) -> str:
    return content_hash({k: v.model_dump(mode="json") for k, v in sorted(ctx.specs.items())})


def build_cfd_registration(ctx: CfdStudyContext, trial: CfdTrialCfg) -> TrialRegistration:
    """What a CFD trial binds: its rule, the bid and ask data of every instrument it reads, the
    parsed instrument specs (financing with its date), fees, policy, costs and stress, the bar
    construction, the warm-up and the splits."""
    cfg, spec = ctx.config, ctx.rules[trial.trial_id]
    ids = [i.instrument_id for i in cfg.instruments]
    fx_ids = [i.fx_instrument_id for i in cfg.instruments if i.fx_instrument_id]
    datasets: list[dict[str, str]] = []
    for iid in [*ids, *fx_ids]:
        for side in (".BID", ".ASK"):
            record = ctx.dataset.file_for(iid + side)
            datasets.append(
                {
                    "dataset": ctx.dataset.name,
                    "instrument_id": record.instrument_id,
                    "content_sha256": record.content_sha256,
                }
            )
    financing = {
        iid: s.financing.model_dump(mode="json")  # type: ignore[union-attr]
        for iid, s in sorted(ctx.specs.items())
    }
    if isinstance(spec, CfdHourlyChannelSpec):
        features = [
            f"channel({spec.channel_hours} hourly bars)",
            f"mean_true_range({spec.atr_hours} hourly bars)",
        ]
    else:
        windows = spec.windows()
        features = [f"sma({n})" for n in windows.sma_periods]
        if windows.rsi_period:
            features.append(f"wilder_rsi({windows.rsi_period})")
        if windows.atr_period:
            features.append(f"wilder_atr({windows.atr_period})")
        if isinstance(spec, CfdWeeklyMomentumSpec):
            features.append(f"return({spec.lookback_days} days)")
    return TrialRegistration(
        trial_id=trial.trial_id,
        study_id=cfg.study_id,
        family=spec.family,
        track=Track.COMMODITIES,
        hypothesis=trial.hypothesis,
        universe=tuple(ids),
        universe_selection=cfg.universe_selection,
        horizon=cfg.horizon,
        rule={
            "strategy_id": spec.strategy_id,
            "spec_file": trial.spec_file,
            "spec_hash": spec.spec_hash,
            "engine": "research/cfd_engine.py",
        },
        features=tuple(features),
        feature_version=spec.feature_version,
        entry_exit_timing=cfg.entry_exit_timing,
        cost_basis={
            "fee_schedule": ctx.fees.schedule_id,
            "fee_schedule_file": cfg.fee_schedule,
            "fee_schedule_hash": content_hash(ctx.fees.config.model_dump(mode="json")),
            "instrument_specs_file": cfg.instrument_specs,
            "instrument_specs_hash": _specs_hash(ctx),
            "financing": financing,
            "policy_file": cfg.policy,
            "policy_hash": ctx.policy.policy_hash,
            "costs": cfg.costs.model_dump(mode="json"),
            "stress": cfg.stress.model_dump(mode="json"),
            "return_basis": "EXECUTABLE (fills on the quoted bid and ask; spread charged once)",
            "known_before_registration": cfg.known_before_registration,
        },
        parameters={
            k: str(v) for k, v in spec.model_dump(mode="json").items() if k != "research_status"
        },
        splits={
            **cfg.splits.describe(),
            "warmup_days": str(cfg.warmup_days),
            "bars": cfg.bars.model_dump(mode="json", exclude_none=True),
            "fx": {
                i.instrument_id: i.fx_instrument_id for i in cfg.instruments if i.fx_instrument_id
            },
        },
        metrics=(
            "net_pnl_usd",
            "net_by_direction",
            "financing_usd",
            "execution_cost_usd",
            "win_rate",
            "profit_factor",
            "max_drawdown",
            "walk_forward_net",
            "G2",
        ),
        selection_correction=cfg.selection_correction,
        datasets=tuple(datasets),
    )


# ----------------------------------------------------------------------------- data


@dataclass(frozen=True, slots=True)
class CfdData:
    daily: Mapping[str, tuple[QuoteBar, ...]]
    calendars: Mapping[str, SessionCalendar]
    closed_days: Mapping[str, int]


def load_cfd_data(ctx: CfdStudyContext, *, end: datetime) -> CfdData:
    """Daily two-sided bars of every instrument (and FX pair) from the warm-up before the
    development start to ``end``, built from the hourly dataset one instrument at a time."""
    cfg = ctx.config
    start = cfg.splits.development_start - timedelta(days=cfg.warmup_days)
    weekend_closed = cfg.bars.calendar == "BROKER_CFD"
    daily: dict[str, tuple[QuoteBar, ...]] = {}
    calendars: dict[str, SessionCalendar] = {}
    closed: dict[str, int] = {}
    for iid in ctx.specs:
        hourly = load_quote_bars(ctx.dataset, iid, ctx.repo_root, start=start, end=end)
        if cfg.bars.decision_minutes == 60:
            bars = tuple(hourly)  # the rule decides on the hourly bars themselves (ADR-0055)
        else:
            bars = tuple(daily_from_hourly(hourly, weekend_closed=weekend_closed))
        del hourly
        daily[iid] = bars
        if weekend_closed:
            sessions = {BrokerCfdCalendar().session_date_of(b.start) for b in bars}
            days = closed_weekdays(sessions, longest=cfg.bars.closed_run_max_weekdays)
            calendars[iid] = BrokerCfdCalendar(days)
            closed[iid] = len(days)
        else:
            calendars[iid] = Utc247Calendar()
            closed[iid] = 0
    return CfdData(daily=daily, calendars=calendars, closed_days=closed)


# ----------------------------------------------------------------------------- runs


def view_costs(cfg: CfdStudyConfig, view: View) -> CfdCosts:
    floors = dict(cfg.costs.spread_floor_bps)
    if view == "base":
        return CfdCosts(label="base", slippage_bps=cfg.costs.slippage_bps, spread_floor_bps=floors)
    if view == "stressed":
        s = cfg.stress
        return CfdCosts(
            label="stressed",
            slippage_bps=cfg.costs.slippage_bps * s.slippage_multiplier,
            spread_multiplier=s.spread_multiplier,
            spread_floor_bps=floors,
            financing_charge_multiplier=s.financing_charge_multiplier,
            financing_credits=s.financing_credits,
            missed_entry_percent=s.missed_entry_percent,
        )
    return CfdCosts(label="diagnostic", slippage_bps=Decimal(0), mid_fills=True, financing=False)


def _engine(
    ctx: CfdStudyContext,
    data: CfdData,
    trial_id: str,
    costs: CfdCosts,
    start: datetime,
    end: datetime,
) -> CfdEngineResult:
    cfg = ctx.config
    feed_from = start - timedelta(days=cfg.warmup_days)

    def window(iid: str) -> tuple[QuoteBar, ...]:
        return tuple(b for b in data.daily[iid] if feed_from <= b.start < end)

    instruments = {
        i.instrument_id: CfdInstrument(
            ctx.specs[i.instrument_id], window(i.instrument_id), i.fx_instrument_id
        )
        for i in cfg.instruments
    }
    fx = {
        i.fx_instrument_id: window(i.fx_instrument_id)
        for i in cfg.instruments
        if i.fx_instrument_id
    }
    ticks = {iid: s.price_tick for iid, s in ctx.specs.items()}
    rule = build_cfd_rule(ctx.rules[trial_id], data.calendars, ticks)
    config = CfdEngineConfig(
        policy=ctx.policy.policy,
        fees=ctx.fees,
        instruments=instruments,
        costs=costs,
        start=start,
        end=end,
        capital=ctx.policy.policy.initial_trading_capital,
        fx_bars=fx,
    )
    return run_cfd_engine(config, rule)


def session_days(data: CfdData, ids: Sequence[str], start: datetime, end: datetime) -> list[date]:
    days = {
        date.fromisoformat(session_of(b))
        for iid in ids
        for b in data.daily[iid]
        if start <= b.start < end
    }
    return sorted(days)


@dataclass(frozen=True, slots=True)
class CfdTrialRun:
    series: SessionSeries
    trades: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    window_nets: list[float]
    window_trades: list[int]
    pauses: list[dict[str, str]]
    result_hash: str
    diagnostics: dict[str, int]


def run_windows(
    ctx: CfdStudyContext,
    data: CfdData,
    trial_id: str,
    view: View,
    windows: Sequence[tuple[datetime, datetime]],
) -> CfdTrialRun:
    ids = [i.instrument_id for i in ctx.config.instruments]
    capital = ctx.policy.policy.initial_trading_capital
    parts: list[SessionSeries] = []
    trades: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    nets: list[float] = []
    counts: list[int] = []
    pauses: list[dict[str, str]] = []
    hashes: list[str] = []
    diag: dict[str, int] = {}
    for start, end in windows:
        result = _engine(ctx, data, trial_id, view_costs(ctx.config, view), start, end)
        parts.append(session_series(result, session_days(data, ids, start, end), capital))
        trades += [t.as_dict() for t in result.trades]
        candidates += [c.as_dict() for c in result.candidates]
        nets.append(float(result.final_equity - capital))
        counts.append(len(result.trades))
        pauses += [{**p, "window": iso_utc(start)} for p in result.pauses]
        hashes.append(result.result_hash)
        for k, v in result.diagnostics.items():
            diag[k] = diag.get(k, 0) + int(v)  # type: ignore[call-overload]
    series = SessionSeries(
        tuple(d for p in parts for d in p.days),
        tuple(v for p in parts for v in p.pnl),
        tuple(v for p in parts for v in p.equity),
        tuple(v for p in parts for v in p.trades_closed),
        tuple(v for p in parts for v in p.realized),
        tuple(v for p in parts for v in p.exposure_minutes),
        tuple(v for p in parts for v in p.turnover),
    )
    return CfdTrialRun(series, trades, candidates, nets, counts, pauses, content_hash(hashes), diag)


def _summary(
    ctx: CfdStudyContext,
    series: SessionSeries,
    trades: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]] | None,
    seed_offset: int,
) -> dict[str, Any]:
    cfg = ctx.config
    out = summarize_series(
        series,
        trades,
        candidates,
        bootstrap=cfg.bootstrap,
        session_minutes=1440,
        sessions_per_month=21.0 if cfg.bars.calendar == "BROKER_CFD" else 30.4375,
        annualisation_sessions=cfg.annualisation_sessions,
        operating_costs_usd_per_month=Decimal(0),
        seed_offset=seed_offset,
        capital=ctx.policy.policy.initial_trading_capital,
    )
    out["financing_usd"] = sum(float(t["financing_usd"]) for t in trades)
    out["by_direction"] = _by_direction(trades)
    if candidates is not None:
        out["not_taken_size_over_limit"] = sum(
            1
            for c in candidates
            if c["status"] == "REJECTED" and "per_trade_risk" in str(c["binding"])
        )
    return out


def _by_direction(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for direction in ("LONG", "SHORT"):
        nets = [float(t["net_pnl"]) for t in trades if t["direction"] == direction]
        wins = [v for v in nets if v > 0]
        losses = [v for v in nets if v <= 0]
        out[direction.lower()] = {
            "trades": len(nets),
            "net_pnl_usd": sum(nets),
            "win_rate": len(wins) / len(nets) if nets else None,
            "avg_win_usd": sum(wins) / len(wins) if wins else None,
            "avg_loss_usd": sum(losses) / len(losses) if losses else None,
            "financing_usd": sum(
                float(t["financing_usd"]) for t in trades if t["direction"] == direction
            ),
        }
    return out


def buy_and_hold(
    ctx: CfdStudyContext, data: CfdData, days: Sequence[date], start: datetime, end: datetime
) -> SessionSeries:
    """Equal notional of the sleeve's capital in every instrument of the universe, held long at
    the mid from the first session, paying each instrument's long financing: what holding the
    market at the same size would have earned. A comparator, never a trial."""
    ids = [i.instrument_id for i in ctx.config.instruments]
    capital = float(ctx.policy.policy.initial_trading_capital)
    notional = capital / len(ids)
    pnl_by_day: dict[date, float] = dict.fromkeys(days, 0.0)
    for iid in ids:
        spec = ctx.specs[iid]
        rate = float(spec.financing.long_daily_charge_fraction) if spec.financing else 0.0
        bars = [b for b in data.daily[iid] if start <= b.start < end]
        prev: float | None = None
        for bar in bars:
            close = float(bar.mid_close)
            day = date.fromisoformat(session_of(bar))
            if prev is not None and day in pnl_by_day:
                nights = sum(n for _, n in charge_instants(spec, bar.start, bar.end))
                pnl_by_day[day] += notional * (close / prev - 1) - notional * rate * nights
            prev = close
    equity = capital
    pnl, eq = [], []
    for d in days:
        equity += pnl_by_day[d]
        pnl.append(pnl_by_day[d])
        eq.append(equity)
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


def run_cfd_study(
    ctx: CfdStudyContext,
    registry: ExperimentRegistry,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    cfg = ctx.config
    plan = cfg.splits
    registrations = [build_cfd_registration(ctx, t) for t in cfg.trials]
    for registration in registrations:
        registry.require_registered(registration)
    unlocked = registry.holdout_unlocked(cfg.study_id)
    end = plan.holdout_end if unlocked else plan.holdout_start
    guard_window(
        plan, plan.development_start - timedelta(days=cfg.warmup_days), end, unlocked=unlocked
    )
    data = load_cfd_data(ctx, end=end)
    windows = deployment_windows(plan)
    capital = ctx.policy.policy.initial_trading_capital
    ids = [i.instrument_id for i in cfg.instruments]
    results: dict[str, dict[str, Any]] = {}
    series: dict[str, SessionSeries] = {}
    trades: dict[str, list[dict[str, Any]]] = {}
    for i, trial in enumerate(cfg.trials):
        views: dict[str, Any] = {}
        for view in VIEWS:
            if progress:
                progress(f"{trial.trial_id} {view} ({len(windows)} windows)")
            run = run_windows(ctx, data, trial.trial_id, view, windows)
            summary = _summary(ctx, run.series, run.trades, run.candidates, 10 * i)
            summary.update(
                result_hash=run.result_hash,
                diagnostics=run.diagnostics,
                pauses=run.pauses,
                window_nets_usd=run.window_nets,
                negative_windows=sum(1 for v in run.window_nets if v < 0),
                window_trades=run.window_trades,
                windows=[iso_utc(w[0]) for w in windows],
            )
            views[view] = summary
            if view == "base":
                series[trial.trial_id] = run.series
                trades[trial.trial_id] = run.trades
        if progress:
            progress(f"{trial.trial_id} continuous deployment")
        continuous = _engine(
            ctx,
            data,
            trial.trial_id,
            view_costs(cfg, "base"),
            plan.development_start,
            plan.holdout_start,
        )
        views["continuous"] = {
            "net_pnl_usd": float(continuous.final_equity - capital),
            "trades": len(continuous.trades),
            "pauses": list(continuous.pauses),
            "result_hash": continuous.result_hash,
        }
        spec = ctx.rules[trial.trial_id]
        results[trial.trial_id] = {
            "registration_hash": registrations[i].content_hash,
            "spec_hash": spec.spec_hash,
            "family": spec.family,
            "universe": ids,
            "views": views,
        }
    trial_ids = [t.trial_id for t in cfg.trials]
    wf_series, wf_trades, selection_log = walk_forward(
        plan, trial_ids, series, trades, capital=float(capital)
    )
    test_start = (
        datetime.combine(
            wf_series.days[0], datetime.min.time(), tzinfo=plan.development_start.tzinfo
        )
        if wf_series.days
        else plan.development_start
    )
    wf_summary = _summary(ctx, wf_series, wf_trades, None, 1000)
    bh = buy_and_hold(ctx, data, wf_series.days, test_start, plan.holdout_start)
    bh_summary = _summary(ctx, bh, [], None, 2000)
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
    best_row = matrix[int(np.argmax(matrix.mean(axis=1)))] if matrix.shape[1] > 1 else as_array([])
    n_registered = len(registry.registrations())
    dsr = (
        deflated_sharpe(best_row, trial_sharpes=trial_sharpes, n_trials=n_registered)
        if len(best_row) > 2
        else None
    )
    return {
        "kind": KIND,
        "study_id": cfg.study_id,
        "title": cfg.title,
        "track": cfg.track,
        "universe": ids,
        "capital_usd": float(capital),
        "disclosures": {
            "universe_selection": cfg.universe_selection,
            "horizon": cfg.horizon,
            "entry_exit_timing": cfg.entry_exit_timing,
            "known_before_registration": cfg.known_before_registration,
        },
        "trial_budget": registry.budget(),
        "evidence_class": "BACKTEST (bar-based historical replay on the broker's demo quotes) - not paper, demo, shadow or live evidence",
        "policy_hash": ctx.policy.policy_hash,
        "fee_schedule": ctx.fees.schedule_id,
        "cost_model": {
            view: {
                "slippage_bps": str(c.slippage_bps),
                "spread_multiplier": str(c.spread_multiplier),
                "spread_floor_bps": {k: str(v) for k, v in c.spread_floor_bps.items()},
                "mid_fills": c.mid_fills,
                "financing": c.financing,
                "financing_charge_multiplier": str(c.financing_charge_multiplier),
                "financing_credits": c.financing_credits,
                "missed_entry_percent": c.missed_entry_percent,
            }
            for view in VIEWS
            for c in [view_costs(cfg, view)]
        },
        "session_calendar": cfg.bars.calendar,
        "decision_bar": "hourly" if cfg.bars.decision_minutes == 60 else "daily",
        "closed_weekdays": dict(data.closed_days),
        "data": {
            "dataset": ctx.dataset.name,
            "daily_bars": {iid: len(b) for iid, b in data.daily.items()},
            "first_bar": {iid: iso_utc(b[0].start) for iid, b in data.daily.items() if b},
            "spread_bps_median_by_year": _spread_by_year(data),
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
        "comparators": {
            "buy_and_hold": bh_summary,
            "cash": {"net_pnl_usd": 0.0, "sessions": len(wf_series.days), "trades": 0},
        },
        "selection_aware": {
            "registered_trials": n_registered,
            "reality_check": rc,
            "deflated_sharpe": dsr,
        },
        "regimes": _regimes(bh, wf_series),
        "g2": _g2(wf_summary, rc),
        "warmup_days": cfg.warmup_days,
    }


def _spread_by_year(data: CfdData) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for iid, bars in data.daily.items():
        by_year: dict[int, list[float]] = {}
        for b in bars:
            by_year.setdefault(b.start.year, []).append(float(b.spread_bps_at_close))
        out[iid] = {str(y): float(np.median(v)) for y, v in sorted(by_year.items())}
    return out


def verdict(result: Mapping[str, Any], trial_id: str) -> str:
    """The verdict recorded with a RESULT event, as for baseline studies."""
    base = result["trials"][trial_id]["views"]["base"]
    return "POSITIVE_UNCORRECTED" if base["net_pnl_usd"] > 0 else "NEGATIVE_NET"


def promotion(result: Mapping[str, Any], trial_id: str) -> dict[str, Any]:
    """ADR-0052's rule for the practice sleeves: net positive in the base and the stressed views
    and in the walk-forward summary. G2 is reported, not required for practice."""
    views = result["trials"][trial_id]["views"]
    checks = {
        "base_net_positive": views["base"]["net_pnl_usd"] > 0,
        "stressed_net_positive": views["stressed"]["net_pnl_usd"] > 0,
        "walk_forward_net_positive": result["walk_forward"]["summary"]["net_pnl_usd"] > 0,
    }
    return {"trade_in_practice": all(checks.values()), "checks": checks}


__all__ = [
    "CfdStudyConfig",
    "CfdStudyContext",
    "CfdStudyError",
    "build_cfd_registration",
    "is_cfd_study",
    "load_cfd_study",
    "open_cfd_study",
    "promotion",
    "run_cfd_study",
    "verdict",
]
