"""A trained veto filter on a frozen CFD rule (``kind: cfd_filter_study``, trial T-0026).

**Why a study kind of its own.** The filter trial names a registered CFD study and one of its
trials and adds a training protocol, a feature set and three controls. Folding that into
``kind: cfd_study`` as an optional ``filter:`` block would put a second protocol inside the code
and configuration that produced, and must keep reproducing, the registered results of T-0018,
T-0019 and T-0021 to T-0025; a separate kind leaves ``cfd_study.py`` and every registered file
untouched and mirrors how ``kind: model_study`` stands beside the stock baseline studies. It is
run with ``trader research cfd-filter --study ...`` and registered with ``trader research
register`` like any other study.

**What it reuses unchanged.** From the base CFD study: the dataset, instruments, specs, fees,
policy, cost views, the development period and its locked final test, the warm-up, the bootstrap
settings, and the rule exactly as its trial registered it (the base trial's registration must
still match the registry). From the stock model study: ``training.train_fold`` (purge, grid by
validation log-loss, calibration on validation, threshold on validation by an engine replay, the
sealed ``ModelBundle``), ``model_study.model_gate`` and the paired bootstrap. From the CFD study:
its summaries, views, ``walk_forward``-style selection, ``_g2`` and ``promotion``.

**What it adapts.** Labels come from the CFD engine (``research/cfd_filter.label_window``): an
unfiltered base-view run emits every signal with its ``cfd-breakout-v1`` features, a taken signal
is labelled by its realised trade and any other by the same signal traded alone. The folds are
the filter study's own rolling train / validation / test months over the base study's development
period, with an embargo of the rule's longest hold.

Protocol per fold (``MODEL_SELECTION_RULE``):

1. Training labels: signals decided in ``[train start, train end)``, outcome bars before the
   training end, then purged: a row decided or closed within ``embargo_minutes`` of the training
   end is dropped (``training.purge``). Validation labels: signals decided in ``[train end,
   validation end - embargo)``, outcome bars before the test start. No bar of the test window is
   read by either.
2. The primary model (logistic) is trained with ``train_fold``; the threshold objective is the net
   USD per session of a base-view replay of the validation window with the bundle as the filter.
   When a challenger (LightGBM) is configured it is trained the same way and replaces the primary
   in that fold only if its validation objective is strictly higher. A fold that cannot be
   trained refuses every signal in its test window (cash).
3. Each test window is a fresh deployment of the sleeve's capital, in the base, stressed and
   diagnostic views, four ways: the rule alone; the rule with the filter; random refusal at the
   filter's own refusal rate in that window (one hash salt per seed); and equal turnover, refusing
   as many signals as the filter did, the smallest break first. The filter decides on the raw
   quotes, so its decisions are the same in every view.

The filter "works" only if its net result beats all three controls in the base and in the
stressed view (``comparison``). The practice rule is ADR-0052's ``promotion`` on the filtered
rule's views and the study's walk forward, which per fold deploys the filtered rule when its
validation objective is positive and holds cash otherwise. G2 is ``model_gate``: the baseline
checks on the walk forward plus a positive lower bound for the filter over each control.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import Field, model_validator

from trader.config.policy import load_yaml_strict
from trader.data.manifest import DataManifest
from trader.domain.base import ConfigModel
from trader.domain.hashing import canonical_json, content_hash
from trader.domain.types import ZERO, Fraction, canon_decimal, iso_utc
from trader.features.cfd_breakout import (
    FEATURE_DEFINITIONS,
    FEATURE_NAMES,
    FEATURE_VERSION,
    BreakoutFeatureBook,
)
from trader.models.bundle import ModelBundle
from trader.research.calibration_stats import (
    brier,
    expected_calibration_error,
    log_loss,
    reliability_bins,
)
from trader.research.cfd_engine import (
    CfdCosts,
    CfdEngineConfig,
    CfdEngineResult,
    CfdInstrument,
    CfdRule,
    CfdSignal,
    CfdSignalFilter,
    run_cfd_engine,
)
from trader.research.cfd_filter import (
    CfdLabel,
    CfdModelFilter,
    DecisionWindow,
    LabelWindow,
    RandomRefusal,
    RefuseAll,
    SignalFeatures,
    SmallestBreakRefusal,
    fold_data,
    label_window,
)
from trader.research.cfd_study import (
    VIEWS,
    CfdData,
    CfdStudyContext,
    CfdStudyError,
    CfdTrialCfg,
    View,
    _summary,
    build_cfd_registration,
    load_cfd_data,
    load_cfd_study,
    open_cfd_study,
    promotion,
    session_days,
    view_costs,
)
from trader.research.model_study import model_gate
from trader.research.registry import ExperimentRegistry, TrialRegistration
from trader.research.splits import Fold, SplitPlan, WalkForward, folds, guard_window
from trader.research.stats import as_array, bootstrap_mean, deflated_sharpe, reality_check, sharpe
from trader.research.study import SessionSeries, _g2, session_series
from trader.research.training import (
    FoldData,
    TrainingError,
    TrainResult,
    _grid_point,
    float_at_least,
    library_versions,
    purge,
    train_fold,
)
from trader.strategies.cfd_rules import CfdHourlyChannelSpec, build_cfd_rule

KIND = "cfd_filter_study"
OBJECTIVE_QUANTUM = Decimal("0.000000000001")  # 12 dp, USD per session
RATE_QUANTUM = Decimal("0.000001")
CALIBRATION_BINS = 10
VARIANTS = ("rule_only", "filter", "random_control", "turnover_control")
CONTROLS = ("rule_only", "random_control", "turnover_control")
FILTER_FAMILY_SUFFIX = {"logistic": "LOGIT_FILTER", "lightgbm": "LGBM_FILTER"}

LABEL_RULE = (
    "an unfiltered base-view run of the frozen rule over the window emits every signal with its "
    "cfd-breakout-v1 features at its decision time; a signal the run took is labelled by its "
    "realised trade, any other by the same signal traded alone from the sleeve's capital with "
    "the same costs; win = net USD after spread, slippage and financing > 0; net return = net "
    "USD / notional. A signal still open when the outcome bars end, refused even alone, or with "
    "less than a week of earlier bars has no label."
)
INNER_SELECTION_METRIC = (
    "validation log-loss of the uncalibrated probability, scored by the exported numpy bundle "
    "(lowest wins; ties: the first grid point)"
)
THRESHOLD_RULE = (
    "on validation only: among thresholds whose calibrated probability accepts at least "
    "min_accepts validation signals, the one with the highest net USD per session of a base-view "
    "replay of the validation window (fresh sleeve capital, the base study's policy and costs, "
    "the bundle as the filter; outcome bars up to the test start); ties go to the lower threshold"
)
CHALLENGER_RULE = (
    "per fold, the challenger replaces the primary model only if its validation objective is "
    "strictly higher; a model that cannot be trained loses to one that can"
)
UNTRAINABLE_RULE = (
    "a fold without a trainable model (one outcome class, too few signals, or no threshold with "
    "enough accepts) refuses every signal in its test window (fail closed: cash)"
)
CONTROLS_RULE = (
    "every test window, fresh sleeve capital, base, stressed and diagnostic views: the rule alone; "
    "random refusal at the filter's realised refusal rate in that window (hash draw, one salt per "
    "seed); equal turnover, refusing as many signals as the filter did, the smallest break beyond "
    "the channel (in mean true ranges) first, ties by signal id"
)
WORKS_RULE = (
    "the filter works only if its net USD beats the rule alone, the mean of the random control's "
    "seeds and the equal-turnover control, in the base view and in the stressed view"
)
MODEL_SELECTION_RULE = (
    "Rolling walk-forward folds over the base study's development period, purged and embargoed "
    "by the rule's longest hold: training labels decided in the training window with outcomes "
    "before its end, purged within the embargo; validation labels decided in the validation "
    "window (less the embargo) with outcomes before the test start. Per fold the primary model "
    "is trained with training.train_fold (grid by validation log-loss, calibration on "
    "validation, threshold on validation by the net USD per session of a validation replay) and "
    "a challenger replaces it only with a strictly higher validation objective. A fold that "
    "cannot be trained refuses every test signal. The test window is never an input to training "
    "or selection. Each test window is replayed for the rule alone, the rule with the filter, "
    "random refusal at the filter's refusal rate (N salts) and equal turnover (smallest break "
    "refused first), in three cost views. Study walk forward: per fold the filtered rule when its "
    "validation objective is positive, else cash. Selection-aware checks: a reality check across "
    "the four variants and the deflated Sharpe ratio with N = all registered trials. The locked "
    "final test period is not used."
)


class CfdFilterStudyError(ValueError):
    """The filter study cannot be registered or run as written."""


# ----------------------------------------------------------------------------- configuration


class FilterControlsCfg(ConfigModel):
    random_seeds: int = Field(ge=1, le=1000)
    equal_turnover: Literal["smallest_break_first"] = "smallest_break_first"


class ChallengerCfg(ConfigModel):
    model_type: Literal["lightgbm", "logistic"]
    grid: tuple[dict[str, str], ...] = Field(min_length=1)


class CfdFilterTrialCfg(ConfigModel):
    trial_id: str = Field(pattern=r"^T-\d{4}$")
    model_type: Literal["logistic", "lightgbm"]
    grid: tuple[dict[str, str], ...] = Field(min_length=1)
    challenger: ChallengerCfg | None = None
    calibration: Literal["platt", "isotonic", "none"] = "platt"
    threshold_grid: tuple[Fraction, ...] = Field(min_length=1)
    min_accepts: int = Field(ge=1)
    controls: FilterControlsCfg
    seed: int = Field(ge=0)
    hypothesis: str = Field(min_length=20)

    @model_validator(mode="after")
    def _grids(self) -> CfdFilterTrialCfg:
        if len(set(self.threshold_grid)) != len(self.threshold_grid):
            raise ValueError(f"{self.trial_id}: the threshold grid holds duplicates")
        if self.challenger is not None and self.challenger.model_type == self.model_type:
            raise ValueError(f"{self.trial_id}: the challenger must be another model family")
        for model_type, grid in self.models():
            try:
                points = [_grid_point(model_type, raw) for raw in grid]
            except TrainingError as exc:
                raise ValueError(f"{self.trial_id}: {exc}") from exc
            if len({canonical_json(p) for p in points}) != len(points):
                raise ValueError(f"{self.trial_id}: the {model_type} grid holds duplicates")
        return self

    def models(self) -> list[tuple[str, tuple[dict[str, str], ...]]]:
        """(model type, grid) of the primary model, then of the challenger."""
        out: list[tuple[str, tuple[dict[str, str], ...]]] = [(self.model_type, self.grid)]
        if self.challenger is not None:
            out.append((self.challenger.model_type, self.challenger.grid))
        return out

    def canonical_grid(self, model_type: str) -> list[dict[str, str]]:
        grid = dict(self.models())[model_type]
        return [_grid_point(model_type, raw) for raw in grid]

    def salts(self, study_id: str, fold_index: int) -> list[str]:
        return [
            f"{study_id}:{self.trial_id}:f{fold_index:02d}:random:{k:02d}"
            for k in range(self.controls.random_seeds)
        ]


class CfdFilterStudyConfig(ConfigModel):
    kind: Literal["cfd_filter_study"]
    study_id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str
    description: str
    base_study: str
    base_trial: str = Field(pattern=r"^T-\d{4}$")
    walk_forward: WalkForward
    #: Training rows decided or closed this close to the training end are purged; the
    #: validation window ends this much before the test window starts.
    embargo_minutes: int = Field(ge=0)
    known_before_registration: str
    trials: tuple[CfdFilterTrialCfg, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> CfdFilterStudyConfig:
        ids = [t.trial_id for t in self.trials]
        if len(set(ids)) != len(ids):
            raise ValueError("trial IDs must be unique")
        if self.walk_forward.step_months != self.walk_forward.test_months:
            raise ValueError(
                "the test windows must not overlap (step_months == test_months), so their "
                "concatenation is one out-of-sample series"
            )
        return self


def is_cfd_filter_study(path: Path) -> bool:
    try:
        raw = load_yaml_strict(path)
    except Exception:  # an unreadable file is not this kind; the caller reports it
        return False
    return isinstance(raw, dict) and raw.get("kind") == KIND


def load_cfd_filter_study(path: Path) -> CfdFilterStudyConfig:
    return CfdFilterStudyConfig.model_validate(load_yaml_strict(path))


@dataclass(frozen=True, slots=True)
class CfdFilterStudyContext:
    repo_root: Path
    config: CfdFilterStudyConfig
    base: CfdStudyContext
    base_trial: CfdTrialCfg
    rule_spec: CfdHourlyChannelSpec
    plan: SplitPlan

    def trial(self, trial_id: str) -> CfdFilterTrialCfg:
        for trial in self.config.trials:
            if trial.trial_id == trial_id:
                return trial
        raise CfdFilterStudyError(f"{self.config.study_id} has no trial {trial_id}")


def open_cfd_filter_study(
    repo_root: Path, config: CfdFilterStudyConfig, manifest: DataManifest
) -> CfdFilterStudyContext:
    """Open the base CFD study and check that its trial can carry this filter. Reads no bars."""
    try:
        base = open_cfd_study(repo_root, load_cfd_study(repo_root / config.base_study), manifest)
    except (CfdStudyError, KeyError) as exc:
        raise CfdFilterStudyError(f"base study {config.base_study}: {exc}") from exc
    bcfg = base.config
    if bcfg.study_id == config.study_id:
        raise CfdFilterStudyError("a filter study needs its own study_id, not the base study's")
    trial = next((t for t in bcfg.trials if t.trial_id == config.base_trial), None)
    if trial is None:
        raise CfdFilterStudyError(f"{bcfg.study_id} has no trial {config.base_trial}")
    if any(t.trial_id == trial.trial_id for t in config.trials):
        raise CfdFilterStudyError("a filter trial needs its own trial id")
    spec = base.rules[trial.trial_id]
    if not isinstance(spec, CfdHourlyChannelSpec):
        raise CfdFilterStudyError(
            f"{trial.trial_id} is {spec.family}: the cfd-breakout-v1 features describe the "
            "hourly channel rule only"
        )
    if bcfg.bars.decision_minutes != 60:
        raise CfdFilterStudyError(f"{bcfg.study_id} does not decide on hourly bars")
    if config.embargo_minutes < spec.max_hold_hours * 60:
        raise CfdFilterStudyError(
            f"the embargo ({config.embargo_minutes} min) is shorter than the rule's longest hold "
            f"({spec.max_hold_hours * 60} min)"
        )
    plan = SplitPlan(
        development_start=bcfg.splits.development_start,
        holdout_start=bcfg.splits.holdout_start,
        holdout_end=bcfg.splits.holdout_end,
        walk_forward=config.walk_forward,
        embargo_minutes=config.embargo_minutes,
    )
    if not folds(plan):
        raise CfdFilterStudyError("the development period holds no complete fold")
    return CfdFilterStudyContext(
        repo_root=repo_root,
        config=config,
        base=base,
        base_trial=trial,
        rule_spec=spec,
        plan=plan,
    )


# ----------------------------------------------------------------------------- registration


def filter_definition(trial: CfdFilterTrialCfg) -> dict[str, Any]:
    """The registered filter recipe (strings; bound into the registration hash)."""
    models = []
    for model_type, _ in trial.models():
        models.append(
            {
                "model_type": model_type,
                "role": "primary" if model_type == trial.model_type else "challenger",
                "library_versions": library_versions(model_type),
                "grid": trial.canonical_grid(model_type),
            }
        )
    return {
        "models": models,
        "challenger_rule": CHALLENGER_RULE,
        "feature_set": list(FEATURE_NAMES),
        "feature_version": FEATURE_VERSION,
        "feature_definitions": dict(FEATURE_DEFINITIONS),
        "label_definition": LABEL_RULE,
        "label_target": "win = net USD after all costs > 0",
        "inner_selection_metric": INNER_SELECTION_METRIC,
        "calibration": trial.calibration,
        "threshold_rule": THRESHOLD_RULE,
        "threshold_grid": [canon_decimal(t) for t in trial.threshold_grid],
        "min_accepts": str(trial.min_accepts),
        "untrainable_fold": UNTRAINABLE_RULE,
        "seeds": {
            "training": str(trial.seed),
            "control_salts": "<study_id>:<trial_id>:f<fold>:random:<k>",
        },
        "controls": {
            "rule": CONTROLS_RULE,
            "random_seeds": str(trial.controls.random_seeds),
            "equal_turnover": trial.controls.equal_turnover,
        },
        "works_rule": WORKS_RULE,
    }


def build_cfd_filter_registration(
    ctx: CfdFilterStudyContext, trial: CfdFilterTrialCfg
) -> TrialRegistration:
    """The trial as registered: the base trial's registration (rule, data, costs) plus the
    frozen filter recipe and the filter study's folds."""
    cfg = ctx.config
    base = build_cfd_registration(ctx.base, ctx.base_trial)
    definition = filter_definition(trial)
    parameters = dict(base.parameters)
    parameters.update(
        {
            "filter.model_type": trial.model_type,
            "filter.grid": canonical_json(definition["models"][0]["grid"]),
            "filter.challenger": (
                trial.challenger.model_type if trial.challenger is not None else "none"
            ),
            "filter.challenger_grid": (
                canonical_json(definition["models"][1]["grid"])
                if trial.challenger is not None
                else ""
            ),
            "filter.calibration": trial.calibration,
            "filter.threshold_grid": ",".join(definition["threshold_grid"]),
            "filter.min_accepts": str(trial.min_accepts),
            "filter.seed": str(trial.seed),
            "filter.controls.random_seeds": str(trial.controls.random_seeds),
            "filter.controls.equal_turnover": trial.controls.equal_turnover,
            "filter.feature_version": FEATURE_VERSION,
            "filter.library_versions": canonical_json(
                {m["model_type"]: m["library_versions"] for m in definition["models"]}
            ),
        }
    )
    family = f"{base.family}+{FILTER_FAMILY_SUFFIX[trial.model_type]}"
    if trial.challenger is not None:
        family += f"/{FILTER_FAMILY_SUFFIX[trial.challenger.model_type]}"
    return TrialRegistration(
        trial_id=trial.trial_id,
        study_id=cfg.study_id,
        family=family,
        track=base.track,
        hypothesis=trial.hypothesis,
        universe=base.universe,
        universe_selection=base.universe_selection,
        horizon=base.horizon,
        rule={
            **base.rule,
            "base_study": ctx.base.config.study_id,
            "base_study_file": cfg.base_study,
            "base_trial": ctx.base_trial.trial_id,
            "base_registration_hash": base.content_hash,
            "filter": definition,
        },
        features=FEATURE_NAMES,
        feature_version=FEATURE_VERSION,
        entry_exit_timing=base.entry_exit_timing,
        cost_basis={
            **base.cost_basis,
            "filter_known_before_registration": cfg.known_before_registration,
        },
        parameters=parameters,
        splits={
            **base.splits,
            "filter_folds": ctx.plan.describe(),
            "filter_folds_count": str(len(folds(ctx.plan))),
        },
        metrics=(
            "net USD of the rule alone, the rule with the filter and both controls on the test "
            "windows, in the base, stressed and diagnostic views",
            "realised refusal rate and refusals per test window",
            "walk forward net, G2 with the filter-over-control lower bounds, ADR-0052 promotion",
            "Brier score, log-loss, expected calibration error and reliability bins on test labels",
            "full tuning history: grid log-loss, calibration map, threshold objectives",
        ),
        selection_correction=MODEL_SELECTION_RULE,
        datasets=base.datasets,
    )


def require_registered_filter_study(
    ctx: CfdFilterStudyContext, registry: ExperimentRegistry
) -> list[TrialRegistration]:
    """The base trial and every filter trial exactly as registered, before any bar is read."""
    registry.require_registered(build_cfd_registration(ctx.base, ctx.base_trial))
    registrations = [build_cfd_filter_registration(ctx, t) for t in ctx.config.trials]
    for registration in registrations:
        registry.require_registered(registration)
    return registrations


# ----------------------------------------------------------------------------- engine runs


def _rule(ctx: CfdFilterStudyContext, data: CfdData) -> CfdRule:
    ticks = {iid: s.price_tick for iid, s in ctx.base.specs.items()}
    return build_cfd_rule(ctx.rule_spec, data.calendars, ticks)


def _config(
    ctx: CfdFilterStudyContext, data: CfdData, costs: CfdCosts, start: datetime, end: datetime
) -> CfdEngineConfig:
    """The base study's engine for ``[start, end)``, fed from the warm-up before ``start``
    (``cfd_study._engine``, unchanged arithmetic)."""
    bcfg = ctx.base.config
    feed_from = start - timedelta(days=bcfg.warmup_days)

    def window(iid: str) -> tuple[Any, ...]:
        return tuple(b for b in data.daily[iid] if feed_from <= b.start < end)

    return CfdEngineConfig(
        policy=ctx.base.policy.policy,
        fees=ctx.base.fees,
        instruments={
            i.instrument_id: CfdInstrument(
                ctx.base.specs[i.instrument_id], window(i.instrument_id), i.fx_instrument_id
            )
            for i in bcfg.instruments
        },
        costs=costs,
        start=start,
        end=end,
        capital=ctx.base.policy.policy.initial_trading_capital,
        fx_bars={
            i.fx_instrument_id: window(i.fx_instrument_id)
            for i in bcfg.instruments
            if i.fx_instrument_id
        },
    )


def _run(
    ctx: CfdFilterStudyContext,
    data: CfdData,
    view: View,
    start: datetime,
    end: datetime,
    signal_filter: CfdSignalFilter | None = None,
    *,
    decide_until: datetime | None = None,
    record: bool = False,
) -> CfdEngineResult:
    """One deployment from the sleeve's capital: decisions in ``[start, decide_until)`` (default
    ``end``), outcome bars before ``end``."""
    rule = _rule(ctx, data)
    if decide_until is not None and decide_until != end:
        rule = DecisionWindow(rule, start, decide_until)
    config = _config(ctx, data, view_costs(ctx.base.config, view), start, end)
    return run_cfd_engine(config, rule, signal_filter, record_candidates=record)


def _net(result: CfdEngineResult, capital: Decimal) -> Decimal:
    return result.final_equity - capital


# ----------------------------------------------------------------------------- variants


@dataclass(slots=True)
class _Variant:
    """One variant's test windows in one view, fold by fold."""

    parts: list[SessionSeries] = field(default_factory=list)
    trades: list[list[dict[str, Any]]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    nets: list[float] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)
    pauses: list[dict[str, str]] = field(default_factory=list)
    hashes: list[str] = field(default_factory=list)
    filter_hashes: list[str] = field(default_factory=list)
    diagnostics: dict[str, int] = field(default_factory=dict)

    def add(
        self, result: CfdEngineResult, days: Sequence[date], capital: Decimal, start: datetime
    ) -> None:
        self.parts.append(session_series(result, days, capital))
        self.trades.append(_jsonable([t.as_dict() for t in result.trades]))
        self.candidates += _jsonable([c.as_dict() for c in result.candidates])
        self.nets.append(float(result.final_equity - capital))
        self.counts.append(len(result.trades))
        self.pauses += [{**p, "window": iso_utc(start)} for p in result.pauses]
        self.hashes.append(result.result_hash)
        self.filter_hashes.append(result.filter_hash)
        for k, v in result.diagnostics.items():
            self.diagnostics[k] = self.diagnostics.get(k, 0) + int(v)  # type: ignore[call-overload]

    def series(self) -> SessionSeries:
        return _concat(self.parts)

    def all_trades(self) -> list[dict[str, Any]]:
        return [t for fold in self.trades for t in fold]


def _jsonable(rows: Sequence[dict[str, object]]) -> list[dict[str, Any]]:
    return [json.loads(canonical_json(r)) for r in rows]


def _concat(parts: Sequence[SessionSeries]) -> SessionSeries:
    return SessionSeries(
        days=tuple(d for p in parts for d in p.days),
        pnl=tuple(v for p in parts for v in p.pnl),
        equity=tuple(v for p in parts for v in p.equity),
        trades_closed=tuple(v for p in parts for v in p.trades_closed),
        realized=tuple(v for p in parts for v in p.realized),
        exposure_minutes=tuple(v for p in parts for v in p.exposure_minutes),
        turnover=tuple(v for p in parts for v in p.turnover),
    )


def _cash(days: Sequence[date], capital: float) -> SessionSeries:
    zeros = tuple(0 for _ in days)
    flat = tuple(0.0 for _ in days)
    return SessionSeries(tuple(days), flat, tuple(capital for _ in days), zeros, flat, zeros, flat)


@dataclass(slots=True)
class _TrialRun:
    #: variant -> view -> accumulated test windows (random: one per seed)
    rule_only: dict[str, _Variant] = field(default_factory=dict)
    filter: dict[str, _Variant] = field(default_factory=dict)
    random: dict[str, list[_Variant]] = field(default_factory=dict)
    turnover: dict[str, _Variant] = field(default_factory=dict)
    folds: list[dict[str, Any]] = field(default_factory=list)
    bundles: list[ModelBundle] = field(default_factory=list)
    objectives: dict[int, Decimal] = field(default_factory=dict)
    pooled_p: list[np.ndarray] = field(default_factory=list)
    pooled_y: list[np.ndarray] = field(default_factory=list)


def _window_record(result: CfdEngineResult, capital: Decimal) -> dict[str, str]:
    return {
        "net_usd": canon_decimal(_net(result, capital)),
        "trades": str(len(result.trades)),
        "result_hash": result.result_hash,
        "filter_hash": result.filter_hash,
        "filter_decisions": str(len(result.filter_log)),
        "filter_refusals": str(sum(1 for r in result.filter_log if not r.accept)),
    }


# ----------------------------------------------------------------------------- calibration


def _calibration(p: np.ndarray, y: np.ndarray) -> dict[str, Any] | None:
    if len(y) == 0:
        return None
    return {
        "rows": len(y),
        "positives": int(y.sum()),
        "base_rate": float(y.mean()),
        "mean_predicted": float(p.mean()),
        "brier": brier(y, p),
        "log_loss": log_loss(y, p),
        "ece": expected_calibration_error(y, p, CALIBRATION_BINS),
        "bins": [
            {
                "lower": b.lower,
                "upper": b.upper,
                "count": b.count,
                "mean_predicted": b.mean_predicted,
                "observed_rate": b.observed_rate,
            }
            for b in reliability_bins(y, p, CALIBRATION_BINS)
        ],
    }


def _test_diagnostics(
    labels: Sequence[CfdLabel], bundle: ModelBundle
) -> tuple[dict[str, Any] | None, np.ndarray, np.ndarray]:
    """Calibration of the bundle on the test window's labels: out-of-sample diagnostics, never an
    input to any choice."""
    if not labels:
        return None, np.zeros(0), np.zeros(0, dtype=np.int64)
    x = np.asarray([[r.features[n] for n in FEATURE_NAMES] for r in labels], dtype=np.float64)
    y = np.asarray([int(r.win) for r in labels], dtype=np.int64)
    p = bundle.predict_proba_matrix(x)
    out = _calibration(p, y)
    assert out is not None
    accepted = p >= float_at_least(bundle.threshold)
    net = np.asarray([r.net_return for r in labels], dtype=np.float64)
    out |= {
        "accepted": int(accepted.sum()),
        "accepted_win_rate": float(y[accepted].mean()) if accepted.any() else None,
        "mean_net_return_all": float(net.mean()),
        "mean_net_return_accepted": float(net[accepted].mean()) if accepted.any() else None,
    }
    return out, p, y


# ----------------------------------------------------------------------------- one fold


def _train_models(
    ctx: CfdFilterStudyContext,
    trial: CfdFilterTrialCfg,
    data: CfdData,
    features: SignalFeatures,
    fold: Fold,
    train: Any,
    val: Any,
    replays: list[dict[str, str]],
) -> tuple[TrainResult | None, str, list[dict[str, Any]]]:
    """Train the primary model and the challenger; keep the one with the higher validation
    objective (ties: the primary)."""
    capital = ctx.base.policy.policy.initial_trading_capital
    tr_start, tr_end = fold.train
    v_start, v_end = fold.validation
    t_start = fold.test[0]
    ids = [i.instrument_id for i in ctx.base.config.instruments]
    val_sessions = max(len(session_days(data, ids, v_start, v_end)), 1)
    attempts: list[dict[str, Any]] = []
    best: TrainResult | None = None
    for model_type, _ in trial.models():

        def objective(bundle: ModelBundle, model_type: str = model_type) -> Decimal:
            result = _run(
                ctx,
                data,
                "base",
                v_start,
                t_start,
                CfdModelFilter(bundle, features),
                decide_until=v_end,
            )
            value = (_net(result, capital) / Decimal(val_sessions)).quantize(
                OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN
            )
            replays.append(
                {
                    "model_type": model_type,
                    "threshold": canon_decimal(bundle.threshold),
                    "bundle_id": bundle.bundle_id,
                    **_window_record(result, capital),
                    "objective_usd_per_session": canon_decimal(value),
                }
            )
            return value

        try:
            trained = train_fold(
                train,
                val,
                model_type=model_type,
                grid=trial.canonical_grid(model_type),
                seed=trial.seed,
                fold_index=fold.index,
                training_start=tr_start,
                training_cutoff=tr_end,
                calibration=trial.calibration,
                threshold_grid=trial.threshold_grid,
                threshold_objective=objective,
                min_accepts=trial.min_accepts,
            )
        except TrainingError as exc:
            attempts.append(
                {
                    "model_type": model_type,
                    "status": "UNTRAINABLE",
                    "reason": str(exc),
                    "selection_log": list(exc.selection_log),
                }
            )
            continue
        objective_value = Decimal(trained.val_metrics["objective"])
        attempts.append(
            {
                "model_type": model_type,
                "status": "TRAINED",
                "reason": "",
                "bundle_id": trained.bundle.bundle_id,
                "bundle_checksum": trained.bundle.checksum,
                "threshold": canon_decimal(trained.bundle.threshold),
                "objective_usd_per_session": canon_decimal(objective_value),
                "selection_log": list(trained.selection_log),
                "val_metrics": dict(trained.val_metrics),
            }
        )
        if best is None or objective_value > Decimal(best.val_metrics["objective"]):
            best = trained
    reason = "" if best is not None else "; ".join(a["reason"] for a in attempts)
    return best, reason, attempts


@dataclass(frozen=True, slots=True)
class FoldTraining:
    """One fold's labels and model, before its test window is touched."""

    train_set: LabelWindow
    val_set: LabelWindow
    train_raw: FoldData
    train: FoldData
    val: FoldData
    trained: TrainResult | None
    reason: str
    attempts: list[dict[str, Any]]
    replays: list[dict[str, str]]


def train_for_fold(
    ctx: CfdFilterStudyContext,
    trial: CfdFilterTrialCfg,
    data: CfdData,
    features: SignalFeatures,
    fold: Fold,
    *,
    source: Mapping[str, str],
) -> FoldTraining:
    """Training labels (purged and embargoed), validation labels and the selected model of one
    fold. Reads no bar at or after the fold's test start."""
    plan = ctx.plan
    base_costs = view_costs(ctx.base.config, "base")
    family = ctx.rule_spec.family
    tr_start, tr_end = fold.train
    v_start, v_end = fold.validation
    t_start = fold.test[0]
    train_set = label_window(
        _config(ctx, data, base_costs, tr_start, tr_end),
        _rule(ctx, data),
        features,
        decide_until=tr_end,
    )
    val_set = label_window(
        _config(ctx, data, base_costs, v_start, t_start),
        _rule(ctx, data),
        features,
        decide_until=v_end,
    )
    train_raw = fold_data(train_set.labels, family=family, source=source)
    train = purge(train_raw, train_end=tr_end, embargo_minutes=plan.embargo_minutes)
    val = fold_data(val_set.labels, family=family, source=source)
    replays: list[dict[str, str]] = []
    trained, reason, attempts = _train_models(ctx, trial, data, features, fold, train, val, replays)
    return FoldTraining(
        train_set, val_set, train_raw, train, val, trained, reason, attempts, replays
    )


def _fold(
    ctx: CfdFilterStudyContext,
    trial: CfdFilterTrialCfg,
    data: CfdData,
    features: SignalFeatures,
    fold: Fold,
    run: _TrialRun,
    *,
    unlocked: bool,
    source: Mapping[str, str],
    progress: Callable[[str], None] | None,
) -> None:
    bcfg = ctx.base.config
    capital = ctx.base.policy.policy.initial_trading_capital
    tr_start, tr_end = fold.train
    v_start, v_end = fold.validation
    t_start, t_end = fold.test
    guard_window(ctx.plan, tr_start, t_end, unlocked=unlocked)
    say = progress or (lambda _m: None)
    ids = [i.instrument_id for i in bcfg.instruments]

    say(f"{trial.trial_id} fold {fold.index}: labels and training")
    fitted = train_for_fold(ctx, trial, data, features, fold, source=source)
    trained, reason, attempts = fitted.trained, fitted.reason, fitted.attempts
    train_set, val_set = fitted.train_set, fitted.val_set
    train_raw, train, val = fitted.train_raw, fitted.train, fitted.val
    replays = fitted.replays
    # test-window labels: out-of-sample calibration diagnostics only, after training
    test_set = label_window(
        _config(ctx, data, view_costs(bcfg, "base"), t_start, t_end),
        _rule(ctx, data),
        features,
        decide_until=t_end,
    )

    say(f"{trial.trial_id} fold {fold.index}: test window")
    days = session_days(data, ids, t_start, t_end)
    model_filter: CfdSignalFilter
    if trained is not None:
        run.bundles.append(trained.bundle)
        run.objectives[fold.index] = Decimal(trained.val_metrics["objective"])
        model_filter = CfdModelFilter(trained.bundle, features)
    else:
        model_filter = RefuseAll()
    records: dict[str, Any] = {}
    rate = ZERO
    refusals = 0
    seen: tuple[CfdSignal, ...] = ()
    for view in VIEWS:
        rule_result = _run(ctx, data, view, t_start, t_end)
        run.rule_only.setdefault(view, _Variant()).add(rule_result, days, capital, t_start)
        filtered = _run(ctx, data, view, t_start, t_end, model_filter, record=True)
        run.filter.setdefault(view, _Variant()).add(filtered, days, capital, t_start)
        if view == "base":
            decisions = len(filtered.filter_log)
            refusals = sum(1 for r in filtered.filter_log if not r.accept)
            rate = (
                (Decimal(refusals) / Decimal(decisions)).quantize(
                    RATE_QUANTUM, rounding=ROUND_HALF_EVEN
                )
                if decisions
                else ZERO
            )
            seen = filtered.signals
        random_records = []
        seeds = run.random.setdefault(
            view, [_Variant() for _ in range(trial.controls.random_seeds)]
        )
        for k, salt in enumerate(trial.salts(ctx.config.study_id, fold.index)):
            result = _run(ctx, data, view, t_start, t_end, RandomRefusal(rate, salt))
            seeds[k].add(result, days, capital, t_start)
            random_records.append({"salt": salt, **_window_record(result, capital)})
        control = SmallestBreakRefusal.matching(seen, refusals)
        turnover = _run(ctx, data, view, t_start, t_end, control)
        run.turnover.setdefault(view, _Variant()).add(turnover, days, capital, t_start)
        records[view] = {
            "rule_only": _window_record(rule_result, capital),
            "filter": {
                **_window_record(filtered, capital),
                "filter_id": model_filter.filter_id,
                "refusal_rate": canon_decimal(rate),
            },
            "random_control": {"rate": canon_decimal(rate), "salts": random_records},
            "turnover_control": {
                **_window_record(turnover, capital),
                "refused": sorted(control.refused),
            },
        }

    calibration: dict[str, Any] | None = None
    if trained is not None:
        calibration, p, y = _test_diagnostics(test_set.labels, trained.bundle)
        run.pooled_p.append(p)
        run.pooled_y.append(y)
    run.folds.append(
        {
            "fold": fold.index,
            "train": [iso_utc(tr_start), iso_utc(tr_end)],
            "validation": [iso_utc(v_start), iso_utc(v_end)],
            "test": [iso_utc(t_start), iso_utc(t_end)],
            "labels": {
                "train": train_set.counts,
                "train_rows": len(train_raw),
                "train_rows_after_purge": len(train),
                "train_positives": int(train.y.sum()),
                "validation": val_set.counts,
                "validation_rows": len(val),
                "validation_positives": int(val.y.sum()),
                "test": test_set.counts,
                "test_positives": sum(1 for r in test_set.labels if r.win),
            },
            "status": "TRAINED" if trained is not None else "UNTRAINABLE",
            "reason": reason,
            "model_type": trained.bundle.model_type if trained else "",
            "bundle_id": trained.bundle.bundle_id if trained else "",
            "bundle_checksum": trained.bundle.checksum if trained else "",
            "threshold": canon_decimal(trained.bundle.threshold) if trained else "",
            "validation_objective_usd_per_session": (
                trained.val_metrics["objective"] if trained else ""
            ),
            "models": attempts,
            "threshold_replays": replays,
            "runs": records,
            "calibration_test": calibration,
        }
    )


# ----------------------------------------------------------------------------- summaries


def _view_summary(
    ctx: CfdFilterStudyContext,
    variant: _Variant,
    windows: Sequence[str],
    seed_offset: int,
) -> dict[str, Any]:
    out = _summary(
        ctx.base, variant.series(), variant.all_trades(), variant.candidates, seed_offset
    )
    out.update(
        result_hash=content_hash(variant.hashes),
        filter_hash=content_hash(variant.filter_hashes),
        diagnostics=dict(variant.diagnostics),
        pauses=list(variant.pauses),
        window_nets_usd=list(variant.nets),
        negative_windows=sum(1 for v in variant.nets if v < 0),
        window_trades=list(variant.counts),
        windows=list(windows),
    )
    return out


def _random_summary(seeds: Sequence[_Variant], filter_net: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    nets: list[float] = []
    trades = wins = 0
    for k, v in enumerate(seeds):
        x = as_array(v.series().pnl)
        done = v.all_trades()
        net = float(x.sum()) if len(x) else 0.0
        won = sum(1 for t in done if float(t["net_pnl"]) > 0)
        nets.append(net)
        trades += len(done)
        wins += won
        rows.append(
            {
                "seed_index": k,
                "net_pnl_usd": net,
                "trades": len(done),
                "wins": won,
                "window_nets_usd": list(v.nets),
                "result_hash": content_hash(v.hashes),
                "filter_hash": content_hash(v.filter_hashes),
            }
        )
    at_least = sum(1 for n in nets if n >= filter_net)
    return {
        "seeds": len(rows),
        "salts": rows,
        "net_pnl_usd": float(np.mean(nets)),
        "median_net_pnl_usd": float(np.median(nets)),
        "min_net_pnl_usd": float(np.min(nets)),
        "max_net_pnl_usd": float(np.max(nets)),
        "trades": trades / len(rows),
        "win_rate": wins / trades if trades else None,
        "seeds_at_least_filter": at_least,
        "p_value_filter_not_better": (1 + at_least) / (1 + len(rows)),
    }


def _mean_pnl(seeds: Sequence[_Variant]) -> np.ndarray:
    return np.mean(np.vstack([as_array(v.series().pnl) for v in seeds]), axis=0)


def comparison(trial: Mapping[str, Any]) -> dict[str, Any]:
    """``WORKS_RULE``: the filter's net USD against each control, in the base and stressed views."""
    variants = trial["variants"]
    checks: dict[str, bool] = {}
    table: dict[str, dict[str, float]] = {}
    for view in ("base", "stressed"):
        mine = float(variants["filter"]["views"][view]["net_pnl_usd"])
        table[view] = {"filter": mine}
        for control in CONTROLS:
            theirs = float(variants[control]["views"][view]["net_pnl_usd"])
            table[view][control] = theirs
            checks[f"{view}_beats_{control}"] = mine > theirs
    return {"rule": WORKS_RULE, "works": all(checks.values()), "checks": checks, "net_usd": table}


def _paired(
    ctx: CfdFilterStudyContext, mine: np.ndarray, other: np.ndarray, seed: int
) -> dict[str, float] | None:
    if len(mine) < 10:
        return None
    boot = ctx.base.config.bootstrap
    return bootstrap_mean(
        mine - other,
        mean_block=float(boot.mean_block_sessions),
        replicates=boot.replicates,
        seed=seed,
    ).as_dict()


def _verdict(net: float, folds_trained: int) -> str:
    if folds_trained == 0:
        return "UNTRAINABLE"
    return "NEGATIVE_NET" if net <= 0 else "POSITIVE_UNCORRECTED"


# ----------------------------------------------------------------------------- study run


@dataclass(frozen=True, slots=True)
class CfdFilterStudyRun:
    result: dict[str, Any]
    bundles: tuple[ModelBundle, ...]


def _source(registration: TrialRegistration) -> dict[str, str]:
    out = {"registered_trial": registration.trial_id}
    for d in registration.datasets:
        out["dataset"] = d["dataset"]
        out[f"content_sha256:{d['instrument_id']}"] = d["content_sha256"]
    return out


def run_cfd_filter_study(
    ctx: CfdFilterStudyContext,
    registry: ExperimentRegistry,
    *,
    progress: Callable[[str], None] | None = None,
) -> CfdFilterStudyRun:
    """Run every registered filter trial over the folds of the development period."""
    cfg = ctx.config
    bcfg = ctx.base.config
    plan = ctx.plan
    boot = bcfg.bootstrap
    capital = ctx.base.policy.policy.initial_trading_capital
    registrations = require_registered_filter_study(ctx, registry)
    unlocked = registry.holdout_unlocked(cfg.study_id)
    end = plan.holdout_end if unlocked else plan.holdout_start
    guard_window(
        plan, plan.development_start - timedelta(days=bcfg.warmup_days), end, unlocked=unlocked
    )
    data = load_cfd_data(ctx.base, end=end)
    book = BreakoutFeatureBook(
        data.daily, channel_hours=ctx.rule_spec.channel_hours, atr_hours=ctx.rule_spec.atr_hours
    )
    features = SignalFeatures(book)
    fold_list = folds(plan)
    windows = [iso_utc(f.test[0]) for f in fold_list]
    ids = [i.instrument_id for i in bcfg.instruments]
    trials_out: dict[str, Any] = {}
    bundles: list[ModelBundle] = []
    rows: list[np.ndarray] = []
    row_names: list[str] = []
    model_rows: list[int] = []
    wf_parts: list[SessionSeries] = []
    wf_trades: list[dict[str, Any]] = []
    wf_log: list[dict[str, Any]] = []
    runs: dict[str, _TrialRun] = {}
    for i, (trial, registration) in enumerate(zip(cfg.trials, registrations, strict=True)):
        run = _TrialRun()
        for fold in fold_list:
            _fold(
                ctx,
                trial,
                data,
                features,
                fold,
                run,
                unlocked=unlocked,
                source=_source(registration),
                progress=progress,
            )
        bundles += run.bundles
        filter_views = {
            view: _view_summary(ctx, run.filter[view], windows, 100 * i + 10 + j)
            for j, view in enumerate(VIEWS)
        }
        variants: dict[str, Any] = {
            "rule_only": {
                "views": {
                    view: _view_summary(ctx, run.rule_only[view], windows, 100 * i + j)
                    for j, view in enumerate(VIEWS)
                }
            },
            "filter": {"views": filter_views},
            "random_control": {
                "views": {
                    view: _random_summary(
                        run.random[view], float(filter_views[view]["net_pnl_usd"])
                    )
                    for view in VIEWS
                }
            },
            "turnover_control": {
                "views": {
                    view: _view_summary(ctx, run.turnover[view], windows, 100 * i + 20 + j)
                    for j, view in enumerate(VIEWS)
                }
            },
        }
        filter_x = as_array(run.filter["base"].series().pnl)
        series_rows = {
            "rule_only": as_array(run.rule_only["base"].series().pnl),
            "filter": filter_x,
            "random_control": _mean_pnl(run.random["base"]),
            "turnover_control": as_array(run.turnover["base"].series().pnl),
        }
        for name, x in series_rows.items():
            if name == "filter":
                model_rows.append(len(rows))
            rows.append(x)
            row_names.append(f"{trial.trial_id}/{name}")
        seed = boot.seed + 6000 + 10 * i
        paired = {
            f"model_minus_{name}": _paired(ctx, filter_x, x, seed + j)
            for j, (name, x) in enumerate(series_rows.items())
            if name != "filter"
        }
        pooled = (
            _calibration(np.concatenate(run.pooled_p), np.concatenate(run.pooled_y))
            if run.pooled_y
            else None
        )
        folds_trained = sum(1 for f in run.folds if f["status"] == "TRAINED")
        trial_out: dict[str, Any] = {
            "registration_hash": registration.content_hash,
            "family": registration.family,
            "base_trial": ctx.base_trial.trial_id,
            "spec_hash": ctx.rule_spec.spec_hash,
            "model_type": trial.model_type,
            "challenger": trial.challenger.model_type if trial.challenger else None,
            "calibration": trial.calibration,
            "threshold_grid": [canon_decimal(t) for t in trial.threshold_grid],
            "min_accepts": trial.min_accepts,
            "controls": trial.controls.model_dump(mode="json"),
            "seed": trial.seed,
            "hypothesis": trial.hypothesis,
            "universe": ids,
            "folds": run.folds,
            "folds_trained": folds_trained,
            # the filtered rule, read by cfd_study.promotion like a CFD trial
            "views": filter_views,
            "variants": variants,
            "paired_differences": paired,
            "calibration_pooled_test": pooled,
            "result_hash": filter_views["base"]["result_hash"],
            "filter_hash": filter_views["base"]["filter_hash"],
            "verdict": _verdict(float(filter_views["base"]["net_pnl_usd"]), folds_trained),
        }
        trial_out["comparison"] = comparison(trial_out)
        trials_out[trial.trial_id] = trial_out
        runs[trial.trial_id] = run

    # the study's walk forward: per fold, the filtered rule of the trial with the highest
    # positive validation objective, else cash (model_study._walk_forward)
    order = [t.trial_id for t in cfg.trials]
    for k, fold in enumerate(fold_list):
        scores = {
            tid: runs[tid].objectives[fold.index]
            for tid in order
            if fold.index in runs[tid].objectives
        }
        best_tid = max(scores, key=lambda t: (scores[t], -order.index(t))) if scores else None
        choice = best_tid if best_tid is not None and scores[best_tid] > 0 else "CASH"
        if choice == "CASH":
            wf_parts.append(_cash(session_days(data, ids, *fold.test), float(capital)))
        else:
            wf_parts.append(runs[choice].filter["base"].parts[k])
            wf_trades += runs[choice].filter["base"].trades[k]
        wf_log.append(
            {
                "fold": fold.index,
                "validation": [iso_utc(fold.validation[0]), iso_utc(fold.validation[1])],
                "test": [iso_utc(fold.test[0]), iso_utc(fold.test[1])],
                "validation_objective_usd_per_session": {
                    tid: canon_decimal(v) for tid, v in scores.items()
                },
                "selected": choice,
            }
        )

    matrix = np.vstack(rows) if rows else np.zeros((0, 0))
    sessions = matrix.shape[1] if matrix.size else 0
    rc = (
        reality_check(
            matrix,
            mean_block=float(boot.mean_block_sessions),
            replicates=boot.replicates,
            seed=boot.seed + 3000,
        )
        if sessions >= 10
        else None
    )
    trial_sharpes = [sharpe(row) for row in matrix] if sessions > 1 else []
    n_registered = len(registry.registrations())
    dsr = None
    if sessions > 2 and model_rows:
        best = max(model_rows, key=lambda r: (float(matrix[r].mean()), -r))
        dsr = deflated_sharpe(matrix[best], trial_sharpes=trial_sharpes, n_trials=n_registered)
    wf_series = _concat(wf_parts)
    wf_summary = _summary(ctx.base, wf_series, wf_trades, None, 1000)
    gate = model_gate(wf_summary, rc, row_names=row_names, model_rows=model_rows, trials=trials_out)
    result: dict[str, Any] = {
        "kind": KIND,
        "study_id": cfg.study_id,
        "title": cfg.title,
        "description": cfg.description,
        "base_study_id": bcfg.study_id,
        "base_study_file": cfg.base_study,
        "base_trial": ctx.base_trial.trial_id,
        "track": bcfg.track,
        "universe": ids,
        "capital_usd": float(capital),
        "disclosures": {
            "universe_selection": bcfg.universe_selection,
            "horizon": bcfg.horizon,
            "entry_exit_timing": bcfg.entry_exit_timing,
            "known_before_registration": cfg.known_before_registration,
        },
        "trial_budget": registry.budget(),
        "evidence_class": "BACKTEST (bar-based historical replay on the broker's demo quotes) - "
        "not paper, demo, shadow or live evidence",
        "policy_hash": ctx.base.policy.policy_hash,
        "fee_schedule": ctx.base.fees.schedule_id,
        "cost_model": {
            view: {
                "slippage_bps": str(c.slippage_bps),
                "spread_multiplier": str(c.spread_multiplier),
                "mid_fills": c.mid_fills,
                "financing": c.financing,
                "financing_charge_multiplier": str(c.financing_charge_multiplier),
                "financing_credits": c.financing_credits,
                "missed_entry_percent": c.missed_entry_percent,
            }
            for view in VIEWS
            for c in [view_costs(bcfg, view)]
        },
        "splits": plan.describe(),
        "holdout_unlocked": unlocked,
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "feature_definitions": dict(FEATURE_DEFINITIONS),
        "label_rule": LABEL_RULE,
        "selection_rule": MODEL_SELECTION_RULE,
        "threshold_rule": THRESHOLD_RULE,
        "challenger_rule": CHALLENGER_RULE,
        "controls_rule": CONTROLS_RULE,
        "untrainable_rule": UNTRAINABLE_RULE,
        "works_rule": WORKS_RULE,
        "folds": [
            {
                "fold": f.index,
                "train": [iso_utc(f.train[0]), iso_utc(f.train[1])],
                "validation": [iso_utc(f.validation[0]), iso_utc(f.validation[1])],
                "test": [iso_utc(f.test[0]), iso_utc(f.test[1])],
            }
            for f in fold_list
        ],
        "trials": trials_out,
        "walk_forward": {"folds": wf_log, "summary": wf_summary},
        "selection_aware": {
            "rows": row_names,
            "reality_check": rc,
            "reality_check_best_row": row_names[int(rc["best_index"])] if rc else "",
            "deflated_sharpe": dsr,
            "registered_trials": n_registered,
        },
        "g2": gate,
        "g2_baseline_checks": _g2(wf_summary, rc),
        "bundles": [{"bundle_id": b.bundle_id, "checksum": b.checksum} for b in bundles],
    }
    for tid in trials_out:
        trials_out[tid]["promotion"] = promotion(result, tid)
    return CfdFilterStudyRun(result=result, bundles=tuple(bundles))


def verdict(result: Mapping[str, Any], trial_id: str) -> str:
    return str(result["trials"][trial_id]["verdict"])


# ----------------------------------------------------------------------------- outputs


def save_filter_outputs(run: CfdFilterStudyRun, run_dir: Path) -> Path:
    """``results.json`` and ``bundles/<bundle_id>.json`` under ``run_dir``."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "results.json"
    path.write_text(
        json.dumps(run.result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for bundle in run.bundles:
        bundle.save(run_dir / "bundles" / f"{bundle.bundle_id}.json")
    return path


def record_filter_results(
    registry: ExperimentRegistry, result: Mapping[str, Any], *, digest: str, now: datetime
) -> list[tuple[str, str]]:
    """One RESULT event per trial (failures and rejections exactly like successes)."""
    run_id = f"{result['study_id']}-{digest[:12]}"
    out = []
    for tid, trial in result["trials"].items():
        views = trial["views"]
        variants = trial["variants"]
        summary = {
            "trades": str(views["base"]["trades"]),
            "net_pnl_usd": f"{views['base']['net_pnl_usd']:.4f}",
            "stressed_net_pnl_usd": f"{views['stressed']['net_pnl_usd']:.4f}",
            "mid_no_financing_net_pnl_usd": f"{views['diagnostic']['net_pnl_usd']:.4f}",
            "rule_only_net_pnl_usd": (
                f"{variants['rule_only']['views']['base']['net_pnl_usd']:.4f}"
            ),
            "random_control_mean_net_pnl_usd": (
                f"{variants['random_control']['views']['base']['net_pnl_usd']:.4f}"
            ),
            "turnover_control_net_pnl_usd": (
                f"{variants['turnover_control']['views']['base']['net_pnl_usd']:.4f}"
            ),
            "walk_forward_net_pnl_usd": f"{result['walk_forward']['summary']['net_pnl_usd']:.4f}",
            "folds_trained": f"{trial['folds_trained']}/{len(trial['folds'])}",
            "filter_works": "YES" if trial["comparison"]["works"] else "NO",
            "g2_study_status": result["g2"]["status"],
            "practice": "TRADE" if trial["promotion"]["trade_in_practice"] else "NO_TRADE",
        }
        registry.record_result(
            tid,
            run_id=run_id,
            result_hash=trial["result_hash"],
            verdict=trial["verdict"],
            summary=summary,
            now=now,
        )
        out.append((tid, trial["verdict"]))
    return out


__all__ = [
    "KIND",
    "CfdFilterStudyConfig",
    "CfdFilterStudyContext",
    "CfdFilterStudyError",
    "CfdFilterStudyRun",
    "build_cfd_filter_registration",
    "comparison",
    "is_cfd_filter_study",
    "load_cfd_filter_study",
    "open_cfd_filter_study",
    "record_filter_results",
    "require_registered_filter_study",
    "run_cfd_filter_study",
    "save_filter_outputs",
    "verdict",
]
