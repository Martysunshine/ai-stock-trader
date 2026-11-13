"""M3 model study: walk-forward evaluation of candidate-filter models (implementation_roadmap.md
§1 M3 and §2; strategy_and_profitability.md §6-§7; risk_and_validation.md §5;
docs/ml_research_guide.md §5).

A model study references a registered *base* study (``research/study.py``) for its dataset,
universe, instrument specifications, fee schedule, fill assumptions, policy and split plan, and
lists model trials. Each trial is one frozen rule (a research rule or the runtime Candidate A)
plus one candidate-filter recipe: model type, hyperparameter grid, calibration, threshold grid,
minimum accepts, controls and seed. A trial is registered **before** it runs
(:func:`build_model_registration`); a run refuses a trial whose registration differs.

Protocol per walk-forward fold (``splits.folds``; :data:`MODEL_SELECTION_RULE`):

1. **Labels** (``labels.build_labels``) for the training window use only bars before the
   training end; training rows are then purged and embargoed (``training.purge``). Validation
   labels use bars up to the test start (the embargo lets their trades resolve) and never a
   test-window bar. Test-window labels exist only for calibration diagnostics.
2. **Training** (``training.train_fold``): scaler and model on training rows; the grid point by
   validation log-loss; the calibration map on validation scores; the threshold on validation by
   the net PnL per session of an engine replay of the validation window with the bundle as a
   :class:`~trader.research.filters.ModelThresholdFilter` (fresh USD 50 ledger, same costs).
   A fold that cannot be trained honestly (one outcome class, too few candidates, no threshold
   with enough accepts) is recorded as ``UNTRAINABLE`` and its filter vetoes every candidate in
   the test window - it fails closed, never back to the unfiltered rule.
3. **Test window** (each a fresh USD 50 deployment with the study's costs): rule only; rule plus
   the model filter; random rejection at the model's realised veto rate in that window over N
   salts (``HashRandomReject``); and the equal-turnover control, which accepts exactly as many
   candidates as the model did (``TurnoverMatchedReject``), over N salts.

The test windows are concatenated into one walk-forward series per variant and summarised with
``study.summarize``. Selection-aware statistics: the bootstrap reality check across every variant
of every trial, the deflated Sharpe ratio with N = all registered trials, and paired
model-minus-control intervals. The study's own walk-forward series deploys, per fold, the trial
with the highest positive validation objective (else cash); Gate G2 is assessed on it exactly as
for the baseline (``study._g2``). The locked final test period is never loaded.

Hashed parts of the result (run hashes, filter logs, bundle checksums, fold records' money and
thresholds) are strings or Decimals; summary statistics are floats and the whole result is
digested with ``study.result_digest`` like the baseline.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import Field, model_validator

from trader.config.policy import load_yaml_strict
from trader.data.manifest import DataManifest
from trader.domain.base import ConfigModel
from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import canonical_json, content_hash
from trader.domain.types import ZERO, Fraction, canon_decimal, iso_utc
from trader.features.candidate_features import FEATURE_NAMES, FEATURE_VERSION
from trader.models.bundle import ModelBundle
from trader.research.calibration_stats import (
    brier,
    expected_calibration_error,
    log_loss,
    reliability_bins,
)
from trader.research.engine import (
    EngineConfig,
    EngineResult,
    InstrumentSetup,
    ResearchEngine,
    session_dates,
)
from trader.research.filters import (
    CandidateFilter,
    FilterDecision,
    HashRandomReject,
    ModelThresholdFilter,
    TurnoverMatchedReject,
    candidate_feature_fn,
)
from trader.research.labels import LABEL_RULE, LabelSet, build_labels
from trader.research.registry import ExperimentRegistry, TrialRegistration
from trader.research.splits import Fold, folds, guard_window
from trader.research.stats import (
    as_array,
    bootstrap_mean,
    deflated_sharpe,
    drawdown_stats,
    reality_check,
    sharpe,
)
from trader.research.study import (
    WARMUP,
    AnyStudySpec,
    LoadedData,
    SessionSeries,
    StudyContext,
    TrialCfg,
    _dataset_rows,
    _fills,
    _g2,
    _jsonable,
    _slice,
    build_registration,
    build_strategy,
    load_development_data,
    load_study,
    open_study,
    require_known_costs,
    resolve_rule,
    session_series,
    spec_family,
    spec_uses_source_vwap,
    summarize,
    warmup_start,
)
from trader.research.training import (
    TrainingError,
    TrainResult,
    _grid_point,
    float_at_least,
    fold_data_from_labelset,
    library_versions,
    purge,
    train_fold,
)

MODEL_STUDY_KIND = "model_study"
CAPITAL = Decimal(50)
OBJECTIVE_QUANTUM = Decimal("0.000000000001")  # 12 dp, USD per session
RATE_QUANTUM = Decimal("0.000001")
CALIBRATION_BINS = 10
VARIANTS = ("rule_only", "model_filter", "random_control", "turnover_control")
FILTER_FAMILY_SUFFIX = {"logistic": "LOGIT_FILTER", "lightgbm": "LGBM_FILTER"}

INNER_SELECTION_METRIC = (
    "validation log-loss of the uncalibrated probability, scored by the exported numpy bundle "
    "(lowest wins; ties: the first grid point)"
)
THRESHOLD_RULE = (
    "on validation only: among thresholds whose calibrated probability accepts at least "
    "min_accepts validation candidates, the one with the highest net PnL per session of an engine "
    "replay of the validation window (fresh USD 50 ledger, the study's policy, fees and fills; "
    "the bundle as ModelThresholdFilter accepting p >= threshold; outcome bars up to the test "
    "start); ties go to the lower threshold"
)
UNTRAINABLE_RULE = (
    "a fold without a trainable model (one outcome class, too few candidates, or no threshold "
    "with enough accepts) vetoes every candidate in its test window (fail closed: cash)"
)
CONTROLS_RULE = (
    "every test window, fresh USD 50 ledger, same costs: rule only; random rejection at the "
    "model's realised veto rate in that window (HashRandomReject, one salt per seed); equal "
    "accepted-candidate count per window (TurnoverMatchedReject, one salt per seed)"
)
MODEL_SELECTION_RULE = (
    "Per walk-forward fold (purged and embargoed by the maximum hold): the scaler and model are "
    "fitted on training-window labels only; the grid point with the lowest validation log-loss is "
    "kept; the calibration map is fitted on validation scores; the threshold is chosen on "
    "validation by the net PnL per session of an engine replay of the validation window with the "
    "model filter (at least min_accepts accepted validation candidates; ties to the lower "
    "threshold). A fold that cannot be trained vetoes every candidate in its test window. The test "
    "window is never an input to training or selection. Each test window is replayed for: rule "
    "only, rule + model filter, random rejection at the model's realised veto rate (N salts) and "
    "equal accepted-candidate counts (N salts). Study walk-forward: per fold deploy the trial with "
    "the highest positive validation objective, else hold cash. Selection-aware checks: bootstrap "
    "reality check across every variant of every trial on the test windows and the deflated "
    "Sharpe ratio with N = all registered trials. The locked final test period is not used."
)


class ModelStudyError(ValueError):
    """A model study configuration or run request that cannot be honoured (fail closed)."""


# ----------------------------------------------------------------------------- configuration


class ControlsCfg(ConfigModel):
    random_seeds: int = Field(ge=1, le=1000)
    turnover_matched: bool = True


class ModelTrialCfg(ConfigModel):
    trial_id: str = Field(pattern=r"^T-\d{4}$")
    family: Literal["CANDIDATE_A", "CANDIDATE_B"]
    spec_file: str
    overrides: dict[str, str] = Field(default_factory=dict)
    model_type: Literal["logistic", "lightgbm"]
    grid: tuple[dict[str, str], ...] = Field(min_length=1)
    calibration: Literal["platt", "isotonic", "none"] = "platt"
    threshold_grid: tuple[Fraction, ...] = Field(min_length=1)
    min_accepts: int = Field(ge=1)
    controls: ControlsCfg
    seed: int = Field(ge=0)
    hypothesis: str = Field(min_length=20)

    @model_validator(mode="after")
    def _grids(self) -> ModelTrialCfg:
        if len(set(self.threshold_grid)) != len(self.threshold_grid):
            raise ValueError(f"{self.trial_id}: the threshold grid holds duplicates")
        try:
            points = [_grid_point(self.model_type, raw) for raw in self.grid]
        except TrainingError as exc:
            raise ValueError(f"{self.trial_id}: {exc}") from exc
        if len({canonical_json(p) for p in points}) != len(points):
            raise ValueError(f"{self.trial_id}: the hyperparameter grid holds duplicates")
        return self

    def canonical_grid(self) -> list[dict[str, str]]:
        return [_grid_point(self.model_type, raw) for raw in self.grid]

    def rule_trial(self) -> TrialCfg:
        """The frozen rule alone, as a base-study trial (same resolution and cost basis)."""
        return TrialCfg(
            trial_id=self.trial_id,
            spec_file=self.spec_file,
            overrides=dict(self.overrides),
            hypothesis=self.hypothesis,
        )

    def salts(self, study_id: str, fold_index: int, control: str) -> list[str]:
        return [
            f"{study_id}:{self.trial_id}:f{fold_index:02d}:{control}:{k:02d}"
            for k in range(self.controls.random_seeds)
        ]


class ModelStudyConfig(ConfigModel):
    kind: Literal["model_study"]
    study_id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str
    description: str
    base_study: str
    trials: tuple[ModelTrialCfg, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> ModelStudyConfig:
        ids = [t.trial_id for t in self.trials]
        if len(set(ids)) != len(ids):
            raise ValueError("trial IDs must be unique")
        return self


def is_model_study(raw: Mapping[str, Any]) -> bool:
    """Whether a study YAML is a model study (``kind: model_study``) or a baseline study."""
    return raw.get("kind") == MODEL_STUDY_KIND


def load_model_study(path: Path) -> ModelStudyConfig:
    raw = load_yaml_strict(path)
    if not is_model_study(raw):
        raise ModelStudyError(f"{path.name} is not a model study (kind: {MODEL_STUDY_KIND})")
    return ModelStudyConfig.model_validate(raw)


@dataclass(frozen=True, slots=True)
class ModelStudyContext:
    repo_root: Path
    config: ModelStudyConfig
    base: StudyContext

    def trial(self, trial_id: str) -> ModelTrialCfg:
        for trial in self.config.trials:
            if trial.trial_id == trial_id:
                return trial
        raise ModelStudyError(f"{self.config.study_id} has no trial {trial_id}")


def open_model_study(
    repo_root: Path, config: ModelStudyConfig, manifest: DataManifest
) -> ModelStudyContext:
    """Load the base study the model study references and check the combination."""
    base_cfg = load_study(repo_root / config.base_study)
    if base_cfg.study_id == config.study_id:
        raise ModelStudyError("a model study needs its own study_id, not the base study's")
    wf = base_cfg.splits.walk_forward
    if wf.step_months != wf.test_months:
        raise ModelStudyError(
            "the walk-forward test windows must not overlap (step_months == test_months), so "
            "their concatenation is one out-of-sample series"
        )
    return ModelStudyContext(
        repo_root=repo_root, config=config, base=open_study(repo_root, base_cfg, manifest)
    )


# ----------------------------------------------------------------------------- registration


def _rule_spec(ctx: ModelStudyContext, trial: ModelTrialCfg) -> AnyStudySpec:
    spec = resolve_rule(ctx.base, trial.rule_trial())
    if spec_family(spec) != trial.family:
        raise ModelStudyError(
            f"{trial.trial_id}: declared family {trial.family} but {trial.spec_file} is "
            f"{spec_family(spec)}"
        )
    return spec


def filter_definition(trial: ModelTrialCfg) -> dict[str, Any]:
    """The registered candidate-filter recipe (all strings; bound into the registration hash)."""
    return {
        "model_type": trial.model_type,
        "library_versions": library_versions(trial.model_type),
        "feature_set": list(FEATURE_NAMES),
        "feature_version": FEATURE_VERSION,
        "label_definition": LABEL_RULE,
        "label_target": "win = net USD after all costs > 0 (candidate simulated alone)",
        "grid": trial.canonical_grid(),
        "inner_selection_metric": INNER_SELECTION_METRIC,
        "calibration": trial.calibration,
        "threshold_rule": THRESHOLD_RULE,
        "threshold_grid": [canon_decimal(t) for t in trial.threshold_grid],
        "min_accepts": str(trial.min_accepts),
        "untrainable_fold": UNTRAINABLE_RULE,
        "seeds": {
            "training": str(trial.seed),
            "control_salts": "<study_id>:<trial_id>:f<fold>:<random|turnover>:<k>",
        },
        "controls": {
            "rule": CONTROLS_RULE,
            "random_seeds": str(trial.controls.random_seeds),
            "turnover_matched": "true" if trial.controls.turnover_matched else "false",
        },
    }


def build_model_registration(ctx: ModelStudyContext, trial: ModelTrialCfg) -> TrialRegistration:
    """The trial as registered: the base rule registration plus the frozen filter recipe."""
    spec = _rule_spec(ctx, trial)
    base = build_registration(ctx.base, trial.rule_trial())
    definition = filter_definition(trial)
    parameters = dict(base.parameters)
    parameters.update(
        {
            "filter.model_type": trial.model_type,
            "filter.grid": canonical_json(definition["grid"]),
            "filter.calibration": trial.calibration,
            "filter.threshold_grid": ",".join(definition["threshold_grid"]),
            "filter.min_accepts": str(trial.min_accepts),
            "filter.seed": str(trial.seed),
            "filter.controls.random_seeds": str(trial.controls.random_seeds),
            "filter.controls.turnover_matched": definition["controls"]["turnover_matched"],
            "filter.feature_version": FEATURE_VERSION,
            "filter.library_versions": canonical_json(definition["library_versions"]),
        }
    )
    return TrialRegistration(
        trial_id=trial.trial_id,
        study_id=ctx.config.study_id,
        family=f"{spec_family(spec)}+{FILTER_FAMILY_SUFFIX[trial.model_type]}",
        track=base.track,
        hypothesis=trial.hypothesis,
        universe=base.universe,
        universe_selection=base.universe_selection,
        horizon=base.horizon,
        rule={
            **base.rule,
            "base_study": ctx.base.config.study_id,
            "base_study_file": ctx.config.base_study,
            "filter": definition,
        },
        features=FEATURE_NAMES,
        feature_version=FEATURE_VERSION,
        entry_exit_timing=base.entry_exit_timing,
        cost_basis=base.cost_basis,
        parameters=parameters,
        splits=base.splits,
        metrics=(
            "net USD PnL per session of rule, rule + filter and both controls on the test windows",
            "net expectancy per trade, win rate, drawdown, exposure",
            "realised veto rate and accepted-candidate counts per test window",
            "Brier score, log-loss, expected calibration error and reliability bins",
            "full tuning history: grid log-loss, calibration map, threshold objectives",
        ),
        selection_correction=MODEL_SELECTION_RULE,
        datasets=base.datasets,
    )


# ----------------------------------------------------------------------------- engine runs


class RejectAll:
    """The model-filter variant of a fold without a trained model: veto every candidate."""

    filter_id = "no-trained-model"

    def decide(self, candidate: TradeCandidate, features: Mapping[str, float]) -> FilterDecision:
        del candidate, features
        return FilterDecision(accept=False, score=None, reason="no_trained_model")


@dataclass(frozen=True, slots=True)
class _Rule:
    """A trial's frozen rule; every run builds a fresh strategy from it."""

    trial: ModelTrialCfg
    spec: AnyStudySpec
    ticks: dict[str, Decimal]


def _engine_config(base: StudyContext, start: datetime, end: datetime) -> EngineConfig:
    cfg = base.config
    return EngineConfig(
        policy=base.policy.policy,
        fees=base.fees,
        fills=_fills(cfg, "base"),
        track=cfg.track,
        calendar=base.calendar,
        start=start,
        end=end,
        loss_windows=cfg.loss_windows,
        session_basis=cfg.session_basis,
    )


def _setups(data: LoadedData, lo: datetime, hi: datetime) -> dict[str, InstrumentSetup]:
    return {
        iid: InstrumentSetup(spec=s.spec, bars=_slice(s.bars, lo, hi), splits=s.splits)
        for iid, s in data.setups.items()
    }


def _run(
    base: StudyContext,
    rule: _Rule,
    data: LoadedData,
    start: datetime,
    end: datetime,
    outcome_end: datetime,
    candidate_filter: CandidateFilter | None = None,
    *,
    model_features: bool = False,
) -> EngineResult:
    """One deployment from a fresh USD 50 ledger: decisions in ``[start, end)``, warm-up bars
    from ``warmup_start`` (``WARMUP`` before by default), outcome bars up to ``outcome_end``."""
    strategy = build_strategy(base, rule.spec, rule.ticks)
    feature_fn = candidate_feature_fn(strategy, rule.trial.family) if model_features else None
    return ResearchEngine(
        _engine_config(base, start, end),
        strategy,
        _setups(data, warmup_start(base, start), outcome_end),
        candidate_filter,
        feature_fn,
    ).run()


def _labels(
    base: StudyContext,
    rule: _Rule,
    data: LoadedData,
    start: datetime,
    end: datetime,
    outcome_end: datetime,
    *,
    unlocked: bool,
    source: Mapping[str, str],
) -> LabelSet:
    return build_labels(
        build_strategy(base, rule.spec, rule.ticks),
        family=rule.trial.family,
        config=_engine_config(base, start, end),
        instruments=_setups(data, warmup_start(base, start), outcome_end),
        plan=base.config.splits,
        unlocked=unlocked,
        source=source,
    )


def _label_source(registration: TrialRegistration) -> dict[str, str]:
    out = {"registered_trial": registration.trial_id}
    for d in registration.datasets:
        out["dataset"] = d["dataset"]
        out[f"content_sha256:{d['instrument_id']}"] = d["content_sha256"]
    return out


def _rule_for(ctx: ModelStudyContext, trial: ModelTrialCfg, data: LoadedData) -> _Rule:
    return _Rule(
        trial=trial,
        spec=_rule_spec(ctx, trial),
        ticks={iid: s.spec.price_tick for iid, s in data.setups.items()},
    )


def _net(result: EngineResult) -> Decimal:
    return sum((t.net_pnl() for t in result.trades), ZERO)


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


def _cash(days: Sequence[date]) -> SessionSeries:
    zeros = tuple(0 for _ in days)
    flat = tuple(0.0 for _ in days)
    return SessionSeries(tuple(days), flat, tuple(50.0 for _ in days), zeros, flat, zeros, flat)


@dataclass(slots=True)
class _Variant:
    """One variant's test windows, fold by fold."""

    parts: list[SessionSeries] = field(default_factory=list)
    trades: list[list[dict[str, Any]]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    hashes: list[str] = field(default_factory=list)
    filter_hashes: list[str] = field(default_factory=list)

    def add(self, result: EngineResult, days: Sequence[date]) -> None:
        self.parts.append(session_series(result, days, CAPITAL))
        self.trades.append(_jsonable([t.as_dict() for t in result.trades]))
        self.candidates += _jsonable([c.as_dict() for c in result.candidates])
        self.hashes.append(result.result_hash)
        self.filter_hashes.append(result.filter_hash)

    def series(self) -> SessionSeries:
        return _concat(self.parts)

    def all_trades(self) -> list[dict[str, Any]]:
        return [t for fold in self.trades for t in fold]


def _window_record(result: EngineResult) -> dict[str, str]:
    accepted = sum(1 for r in result.filter_log if r.accept)
    return {
        "net_usd": canon_decimal(_net(result)),
        "trades": str(len(result.trades)),
        "result_hash": result.result_hash,
        "filter_hash": result.filter_hash,
        "filter_decisions": str(len(result.filter_log)),
        "filter_accepts": str(accepted),
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
    labels: LabelSet, bundle: ModelBundle
) -> tuple[dict[str, Any] | None, np.ndarray, np.ndarray]:
    """Calibration and acceptance quality of the bundle on the test window's labelled candidates
    (each simulated alone): out-of-sample diagnostics, never inputs to any choice."""
    x, y = labels.matrix()
    if len(y) == 0:
        return None, np.zeros(0), np.zeros(0, dtype=np.int64)
    p = bundle.predict_proba_matrix(x)
    out = _calibration(p, y)
    assert out is not None
    accepted = p >= float_at_least(bundle.threshold)
    net = np.asarray([r.net_return for r in labels.rows], dtype=np.float64)
    out |= {
        "accepted": int(accepted.sum()),
        "accepted_win_rate": float(y[accepted].mean()) if accepted.any() else None,
        "mean_net_return_all": float(net.mean()),
        "mean_net_return_accepted": float(net[accepted].mean()) if accepted.any() else None,
    }
    return out, p, y


# ----------------------------------------------------------------------------- one fold


@dataclass(slots=True)
class _TrialRun:
    rule_only: _Variant = field(default_factory=_Variant)
    model_filter: _Variant = field(default_factory=_Variant)
    random: list[_Variant] = field(default_factory=list)
    turnover: list[_Variant] = field(default_factory=list)
    folds: list[dict[str, Any]] = field(default_factory=list)
    bundles: list[ModelBundle] = field(default_factory=list)
    objectives: dict[int, Decimal] = field(default_factory=dict)
    pooled_p: list[np.ndarray] = field(default_factory=list)
    pooled_y: list[np.ndarray] = field(default_factory=list)


def _fold(
    ctx: ModelStudyContext,
    rule: _Rule,
    data: LoadedData,
    fold: Fold,
    run: _TrialRun,
    *,
    unlocked: bool,
    source: Mapping[str, str],
    progress: Callable[[str], None] | None,
) -> None:
    base = ctx.base
    cfg = base.config
    plan = cfg.splits
    trial = rule.trial
    tr_start, tr_end = fold.train
    v_start, v_end = fold.validation
    t_start, t_end = fold.test
    guard_window(plan, warmup_start(base, tr_start), t_end, unlocked=unlocked)
    say = progress or (lambda _m: None)
    say(f"{trial.trial_id} fold {fold.index}: labels")
    train_set = _labels(
        base, rule, data, tr_start, tr_end, tr_end, unlocked=unlocked, source=source
    )
    val_set = _labels(base, rule, data, v_start, v_end, t_start, unlocked=unlocked, source=source)
    test_set = _labels(base, rule, data, t_start, t_end, t_end, unlocked=unlocked, source=source)
    train_raw = fold_data_from_labelset(train_set, family=trial.family)
    train = purge(train_raw, train_end=tr_end, embargo_minutes=plan.embargo_minutes)
    val = fold_data_from_labelset(val_set, family=trial.family)
    val_sessions = max(len(_days(base, v_start, v_end)), 1)
    replays: list[dict[str, str]] = []

    def objective(bundle: ModelBundle) -> Decimal:
        result = _run(
            base,
            rule,
            data,
            v_start,
            v_end,
            t_start,
            ModelThresholdFilter(bundle, bundle.threshold),
            model_features=True,
        )
        value = (_net(result) / Decimal(val_sessions)).quantize(
            OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN
        )
        replays.append(
            {
                "threshold": canon_decimal(bundle.threshold),
                "bundle_id": bundle.bundle_id,
                **_window_record(result),
                "objective_usd_per_session": canon_decimal(value),
            }
        )
        return value

    say(f"{trial.trial_id} fold {fold.index}: training")
    trained: TrainResult | None = None
    reason = ""
    failed_log: tuple[dict[str, str], ...] = ()
    try:
        trained = train_fold(
            train,
            val,
            model_type=trial.model_type,
            grid=trial.canonical_grid(),
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
        reason = str(exc)
        failed_log = exc.selection_log

    say(f"{trial.trial_id} fold {fold.index}: test window")
    days = _days(base, t_start, t_end)
    rule_result = _run(base, rule, data, t_start, t_end, t_end)
    run.rule_only.add(rule_result, days)
    model_filter: CandidateFilter
    if trained is not None:
        bundle = trained.bundle
        run.bundles.append(bundle)
        run.objectives[fold.index] = Decimal(trained.val_metrics["objective"])
        model_filter = ModelThresholdFilter(bundle, bundle.threshold)
    else:
        model_filter = RejectAll()
    model_result = _run(
        base, rule, data, t_start, t_end, t_end, model_filter, model_features=trained is not None
    )
    run.model_filter.add(model_result, days)
    decisions = len(model_result.filter_log)
    vetoes = sum(1 for r in model_result.filter_log if not r.accept)
    rate = (
        (Decimal(vetoes) / Decimal(decisions)).quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
        if decisions
        else ZERO
    )
    random_records = []
    for k, salt in enumerate(trial.salts(ctx.config.study_id, fold.index, "random")):
        result = _run(base, rule, data, t_start, t_end, t_end, HashRandomReject(rate, salt))
        run.random[k].add(result, days)
        random_records.append({"salt": salt, **_window_record(result)})
    turnover_records = []
    if trial.controls.turnover_matched:
        for k, salt in enumerate(trial.salts(ctx.config.study_id, fold.index, "turnover")):
            control = TurnoverMatchedReject.matching(
                model_result.filter_log, windows=[(t_start, t_end)], salt=salt
            )
            result = _run(base, rule, data, t_start, t_end, t_end, control)
            run.turnover[k].add(result, days)
            turnover_records.append({"salt": salt, **_window_record(result)})

    calibration: dict[str, Any] | None = None
    if trained is not None:
        calibration, p, y = _test_diagnostics(test_set, trained.bundle)
        run.pooled_p.append(p)
        run.pooled_y.append(y)
    run.folds.append(
        {
            "fold": fold.index,
            "train": [iso_utc(tr_start), iso_utc(tr_end)],
            "validation": [iso_utc(v_start), iso_utc(v_end)],
            "test": [iso_utc(t_start), iso_utc(t_end)],
            "labels": {
                "train_rows": len(train_raw),
                "train_rows_after_purge": len(train),
                "train_positives": int(train.y.sum()),
                "validation_rows": len(val),
                "validation_positives": int(val.y.sum()),
                "test_rows": len(test_set.rows),
                "test_positives": sum(1 for r in test_set.rows if r.win),
            },
            "status": "TRAINED" if trained is not None else "UNTRAINABLE",
            "reason": reason,
            "bundle_id": trained.bundle.bundle_id if trained else "",
            "bundle_checksum": trained.bundle.checksum if trained else "",
            "threshold": canon_decimal(trained.bundle.threshold) if trained else "",
            "selection_log": list(trained.selection_log if trained else failed_log),
            "val_metrics": dict(trained.val_metrics) if trained else {},
            "threshold_replays": replays,
            "runs": {
                "rule_only": _window_record(rule_result),
                "model_filter": {
                    **_window_record(model_result),
                    "filter_id": model_filter.filter_id,
                    "vetoes": str(vetoes),
                    "veto_rate": canon_decimal(rate),
                },
                "random_control": {"rate": canon_decimal(rate), "salts": random_records},
                "turnover_control": {"salts": turnover_records},
            },
            "calibration_test": calibration,
        }
    )


def _days(base: StudyContext, start: datetime, end: datetime) -> list[date]:
    return session_dates(base.config.session_basis, base.calendar, start, end)


# ----------------------------------------------------------------------------- summaries


def _control_summary(
    variants: Sequence[_Variant], model_net: float, annualisation: int
) -> dict[str, Any] | None:
    if not variants:
        return None
    rows: list[dict[str, Any]] = []
    for k, v in enumerate(variants):
        series = v.series()
        nets = [float(t["net_pnl"]) for t in v.all_trades()]
        x = as_array(series.pnl)
        rows.append(
            {
                "seed_index": k,
                "net_pnl_usd": float(x.sum()) if len(x) else 0.0,
                "trades": len(nets),
                "wins": sum(1 for n in nets if n > 0),
                "realized_net_usd": sum(nets),
                "max_drawdown_usd": drawdown_stats(50.0 + np.concatenate([[0.0], np.cumsum(x)]))[
                    "max_drawdown_usd"
                ],
                "exposure_minutes": sum(series.exposure_minutes),
                "result_hash": content_hash(v.hashes),
                "filter_hash": content_hash(v.filter_hashes),
            }
        )
    nets = [r["net_pnl_usd"] for r in rows]
    trades = sum(r["trades"] for r in rows)
    at_least = sum(1 for n in nets if n >= model_net)
    mean = _mean_pnl(variants)
    return {
        "seeds": len(rows),
        "salts": rows,
        "mean_net_pnl_usd": float(np.mean(nets)),
        "median_net_pnl_usd": float(np.median(nets)),
        "min_net_pnl_usd": float(np.min(nets)),
        "max_net_pnl_usd": float(np.max(nets)),
        "mean_trades": trades / len(rows),
        "pooled_win_rate": sum(r["wins"] for r in rows) / trades if trades else None,
        "pooled_net_per_trade_usd": sum(r["realized_net_usd"] for r in rows) / trades
        if trades
        else None,
        "mean_max_drawdown_usd": float(np.mean([r["max_drawdown_usd"] for r in rows])),
        "mean_session_pnl_usd": float(mean.mean()) if len(mean) else 0.0,
        "sharpe_annualised_of_mean": sharpe(mean) * float(np.sqrt(annualisation))
        if len(mean) > 1
        else 0.0,
        "salts_at_least_model": at_least,
        "p_value_model_not_better": (1 + at_least) / (1 + len(rows)),
    }


def _mean_pnl(variants: Sequence[_Variant]) -> np.ndarray:
    return np.mean(np.vstack([as_array(v.series().pnl) for v in variants]), axis=0)


#: The comparisons a model filter must win, each with a positive one-sided lower bound.
MODEL_GATE_COMPARISONS = ("rule_only", "random_control", "turnover_control")


def model_gate(
    wf_summary: Mapping[str, Any],
    rc: Mapping[str, Any] | None,
    *,
    row_names: Sequence[str],
    model_rows: Sequence[int],
    trials: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Gate G2 for a candidate filter (risk_and_validation.md §8; strategy §6; ADR-0034).

    The baseline checks on the walk-forward series are necessary but not enough for a filter:
    trading less, or trading a random subset, must not pass as skill. So the reality check's
    best row must itself be a model-filter row (its selection-adjusted bound is then the
    filter's), and that trial's filter must beat the rule alone, the random-rejection control
    and the equal-turnover control, each with a positive one-sided paired lower bound. A missing
    control or comparison fails the gate.
    """
    base = _g2(wf_summary, rc)
    checks: dict[str, bool] = dict(base["checks"])
    best_index = int(rc["best_index"]) if rc else -1
    best_is_model = best_index in set(model_rows)
    checks["best_reality_check_row_is_a_model_filter"] = best_is_model
    checks["selection_adjusted_lower_bound_positive"] = (
        best_is_model and float((rc or {}).get("selection_adjusted_lower_bound", -1)) > 0
    )
    trial_id = row_names[best_index].split("/")[0] if best_is_model else ""
    paired = (trials.get(trial_id) or {}).get("paired_differences") or {}
    for name in MODEL_GATE_COMPARISONS:
        diff: Mapping[str, Any] | None = paired.get(f"model_minus_{name}")
        checks[f"filter_beats_{name}_lower_bound_positive"] = (
            diff is not None and float(diff["one_sided_lower"]) > 0
        )
    return {
        "status": "PASSED" if all(checks.values()) else "NOT_PASSED",
        "checks": checks,
        "gated_trial": trial_id,
        "note": "Backtest evidence only. A filter passes only if it beats the rule alone and "
        "both controls; G2 also requires review, and G3 forward evidence follows.",
    }


def _paired(
    base: StudyContext, model: np.ndarray, other: np.ndarray, seed: int
) -> dict[str, float] | None:
    if len(model) < 10:
        return None
    boot = base.config.bootstrap
    return bootstrap_mean(
        model - other,
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
class ModelStudyRun:
    result: dict[str, Any]
    bundles: tuple[ModelBundle, ...]


def require_registered_model_study(
    ctx: ModelStudyContext, registry: ExperimentRegistry
) -> list[TrialRegistration]:
    """Every trial exactly as registered (risk_and_validation.md §5), checked before any data."""
    require_known_costs(ctx.base)
    registrations = [build_model_registration(ctx, t) for t in ctx.config.trials]
    for registration in registrations:
        registry.require_registered(registration)
    return registrations


def run_model_study(
    ctx: ModelStudyContext,
    registry: ExperimentRegistry,
    *,
    progress: Callable[[str], None] | None = None,
) -> ModelStudyRun:
    """Run every registered trial over the walk-forward folds of the development period."""
    cfg = ctx.config
    base = ctx.base
    bcfg = base.config
    plan = bcfg.splits
    boot = bcfg.bootstrap
    registrations = require_registered_model_study(ctx, registry)
    unlocked = registry.holdout_unlocked(cfg.study_id)
    fold_list = folds(plan)
    data_cache: dict[bool, LoadedData] = {}
    runs: dict[str, _TrialRun] = {}
    for trial, registration in zip(cfg.trials, registrations, strict=True):
        spec = _rule_spec(ctx, trial)
        needs_vwap = spec_uses_source_vwap(spec)
        if needs_vwap not in data_cache:
            data_cache[needs_vwap] = load_development_data(
                base, uses_source_vwap=needs_vwap, unlocked=unlocked
            )
        data = data_cache[needs_vwap]
        rule = _rule_for(ctx, trial, data)
        run = _TrialRun(
            random=[_Variant() for _ in range(trial.controls.random_seeds)],
            turnover=[_Variant() for _ in range(trial.controls.random_seeds)]
            if trial.controls.turnover_matched
            else [],
        )
        for fold in fold_list:
            _fold(
                ctx,
                rule,
                data,
                fold,
                run,
                unlocked=unlocked,
                source=_label_source(registration),
                progress=progress,
            )
        runs[trial.trial_id] = run

    trials_out: dict[str, Any] = {}
    rows: list[np.ndarray] = []
    row_names: list[str] = []
    model_rows: list[int] = []
    for i, (trial, registration) in enumerate(zip(cfg.trials, registrations, strict=True)):
        run = runs[trial.trial_id]
        rule_series = run.rule_only.series()
        model_series = run.model_filter.series()
        model_x = as_array(model_series.pnl)
        model_net = float(model_x.sum()) if len(model_x) else 0.0
        variants: dict[str, Any] = {
            "rule_only": summarize(
                base,
                rule_series,
                run.rule_only.all_trades(),
                run.rule_only.candidates,
                seed_offset=100 * i,
            ),
            "model_filter": summarize(
                base,
                model_series,
                run.model_filter.all_trades(),
                run.model_filter.candidates,
                seed_offset=100 * i + 10,
            ),
            "random_control": _control_summary(run.random, model_net, bcfg.annualisation_sessions),
            "turnover_control": _control_summary(
                run.turnover, model_net, bcfg.annualisation_sessions
            ),
        }
        variants["model_filter"]["filter_vetoes"] = sum(
            int(f["runs"]["model_filter"]["vetoes"]) for f in run.folds
        )
        variants["model_filter"]["filter_decisions"] = sum(
            int(f["runs"]["model_filter"]["filter_decisions"]) for f in run.folds
        )
        rule_x = as_array(rule_series.pnl)
        series_rows = {"rule_only": rule_x, "model_filter": model_x}
        if run.random:
            series_rows["random_control"] = _mean_pnl(run.random)
        if run.turnover:
            series_rows["turnover_control"] = _mean_pnl(run.turnover)
        for name, x in series_rows.items():
            if name == "model_filter":
                model_rows.append(len(rows))
            rows.append(x)
            row_names.append(f"{trial.trial_id}/{name}")
        seed = boot.seed + 6000 + 10 * i
        paired = {
            f"model_minus_{name}": _paired(base, model_x, x, seed + j)
            for j, (name, x) in enumerate(series_rows.items())
            if name != "model_filter"
        }
        pooled = None
        if run.pooled_y:
            pooled = _calibration(np.concatenate(run.pooled_p), np.concatenate(run.pooled_y))
        folds_trained = sum(1 for f in run.folds if f["status"] == "TRAINED")
        trials_out[trial.trial_id] = {
            "registration_hash": registration.content_hash,
            "family": registration.family,
            "rule_family": trial.family,
            "spec_file": trial.spec_file,
            "overrides": dict(trial.overrides),
            "spec_hash": str(registration.rule["spec_hash"]),
            "model_type": trial.model_type,
            "grid": trial.canonical_grid(),
            "calibration": trial.calibration,
            "threshold_grid": [canon_decimal(t) for t in trial.threshold_grid],
            "min_accepts": trial.min_accepts,
            "controls": trial.controls.model_dump(mode="json"),
            "seed": trial.seed,
            "hypothesis": trial.hypothesis,
            "library_versions": registration.rule["filter"]["library_versions"],
            "folds": run.folds,
            "folds_trained": folds_trained,
            "variants": variants,
            "paired_differences": paired,
            "calibration_pooled_test": pooled,
            "result_hash": content_hash(run.model_filter.hashes),
            "filter_hash": content_hash(run.model_filter.filter_hashes),
            "verdict": _verdict(model_net, folds_trained),
        }

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
    best_model = ""
    if sessions > 2 and model_rows:
        best = max(model_rows, key=lambda r: (float(matrix[r].mean()), -r))
        best_model = row_names[best]
        dsr = deflated_sharpe(matrix[best], trial_sharpes=trial_sharpes, n_trials=n_registered)

    wf_series, wf_trades, wf_log = _walk_forward(cfg, fold_list, runs, base)
    wf_summary = summarize(base, wf_series, wf_trades, seed_offset=1000)
    gate = model_gate(wf_summary, rc, row_names=row_names, model_rows=model_rows, trials=trials_out)
    bundles = tuple(b for t in cfg.trials for b in runs[t.trial_id].bundles)
    result: dict[str, Any] = {
        "kind": MODEL_STUDY_KIND,
        "study_id": cfg.study_id,
        "title": cfg.title,
        "description": cfg.description,
        "base_study_id": bcfg.study_id,
        "base_study_file": cfg.base_study,
        "track": bcfg.track.value,
        "universe": [m.execution_instrument_id for m in bcfg.instruments],
        "disclosures": {
            "universe_selection": bcfg.universe_selection,
            "horizon": bcfg.horizon,
            "entry_exit_timing": bcfg.entry_exit_timing,
        },
        "trial_budget": registry.budget(),
        "evidence_class": "BACKTEST (bar-based historical replay) - not paper, shadow or live evidence",
        "policy_hash": base.policy.policy_hash,
        "fee_schedule": base.fees.schedule_id,
        "fee_schedule_description": base.fees.config.description,
        "fills": {"base": _fills(bcfg, "base").describe()},
        "splits": plan.describe(),
        "holdout_unlocked": unlocked,
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "label_rule": LABEL_RULE,
        "selection_rule": MODEL_SELECTION_RULE,
        "threshold_rule": THRESHOLD_RULE,
        "controls_rule": CONTROLS_RULE,
        "untrainable_rule": UNTRAINABLE_RULE,
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
            "deflated_sharpe_row": best_model,
            "registered_trials": n_registered,
        },
        "g2": gate,
        "bundles": [{"bundle_id": b.bundle_id, "checksum": b.checksum} for b in bundles],
        "datasets": _dataset_rows(base),
    }
    return ModelStudyRun(result=result, bundles=bundles)


def _walk_forward(
    cfg: ModelStudyConfig,
    fold_list: Sequence[Fold],
    runs: Mapping[str, _TrialRun],
    base: StudyContext,
) -> tuple[SessionSeries, list[dict[str, Any]], list[dict[str, Any]]]:
    """Per fold, deploy the trial with the highest positive validation objective, else cash."""
    order = [t.trial_id for t in cfg.trials]
    parts: list[SessionSeries] = []
    trades: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    for k, fold in enumerate(fold_list):
        scores = {
            tid: runs[tid].objectives[fold.index]
            for tid in order
            if fold.index in runs[tid].objectives
        }
        best = max(scores, key=lambda t: (scores[t], -order.index(t))) if scores else None
        choice = best if best is not None and scores[best] > 0 else "CASH"
        if choice == "CASH":
            parts.append(_cash(_days(base, *fold.test)))
        else:
            parts.append(runs[choice].model_filter.parts[k])
            trades += runs[choice].model_filter.trades[k]
        log.append(
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
    return _concat(parts), trades, log


# ----------------------------------------------------------------------------- outputs


def save_model_outputs(run: ModelStudyRun, run_dir: Path) -> Path:
    """``model_results.json`` and ``bundles/<bundle_id>.json`` under ``run_dir``."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "model_results.json"
    path.write_text(
        json.dumps(run.result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for bundle in run.bundles:
        bundle.save(run_dir / "bundles" / f"{bundle.bundle_id}.json")
    return path


def record_model_results(
    registry: ExperimentRegistry, result: Mapping[str, Any], *, digest: str, now: datetime
) -> list[tuple[str, str]]:
    """Append one RESULT event per trial (failures and rejections exactly like successes)."""
    run_id = f"{result['study_id']}-{digest[:12]}"
    out = []
    for tid, trial in result["trials"].items():
        variants = trial["variants"]
        model = variants["model_filter"]
        random = variants.get("random_control") or {}
        turnover = variants.get("turnover_control") or {}
        summary = {
            "trades": str(model["trades"]),
            "net_pnl_usd": f"{model['net_pnl_usd']:.4f}",
            "rule_only_net_pnl_usd": f"{variants['rule_only']['net_pnl_usd']:.4f}",
            "random_control_mean_net_pnl_usd": f"{random['mean_net_pnl_usd']:.4f}"
            if random
            else "",
            "turnover_control_mean_net_pnl_usd": f"{turnover['mean_net_pnl_usd']:.4f}"
            if turnover
            else "",
            "folds_trained": f"{trial['folds_trained']}/{len(trial['folds'])}",
            "g2_study_status": result["g2"]["status"],
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


# ----------------------------------------------------------------------------- labels helper


def build_trial_labels(
    ctx: ModelStudyContext,
    trial: ModelTrialCfg,
    *,
    start: datetime,
    end: datetime,
    unlocked: bool = False,
) -> LabelSet:
    """An exploration label set for one trial's rule over ``[start, end)``.

    The window must lie inside the development period; the locked final test period is refused
    before any data is read. Outcome bars run at most ``WARMUP`` past ``end`` (never into the
    holdout); a position still open then is marked at the last bar (``END_OF_DATA``).
    """
    plan = ctx.base.config.splits
    guard_window(plan, start, end, unlocked=unlocked)
    if not plan.development_start <= start < end <= plan.holdout_start:
        raise ModelStudyError(
            f"labels are built inside the development period {iso_utc(plan.development_start)}"
            f"..{iso_utc(plan.holdout_start)}"
        )
    require_known_costs(ctx.base)
    registration = build_model_registration(ctx, trial)
    spec = _rule_spec(ctx, trial)
    data = load_development_data(
        ctx.base, uses_source_vwap=spec_uses_source_vwap(spec), unlocked=unlocked
    )
    rule = _rule_for(ctx, trial, data)
    outcome_end = min(end + WARMUP, plan.holdout_start)
    return _labels(
        ctx.base,
        rule,
        data,
        start,
        end,
        outcome_end,
        unlocked=unlocked,
        source=_label_source(registration),
    )
