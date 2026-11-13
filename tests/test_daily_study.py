"""Daily rules in registered studies: the rule registry, warm-up sessions, independent
three-month windows and walk-forward on daily bars (risk_and_validation.md §5; ADR-0020).

A toy daily family (TEST_DAILY_DIP, test-only, never registered as a trial) is added through
``strategies/registry.py`` and runs the whole study path on the SYNTHETIC daily fixture:
``resolve_rule`` -> ``build_strategy`` -> ``run_windows`` -> ``walk_forward`` -> ``summarize``.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

from tests.daily_fixtures import CALENDAR, daily_history, daily_study, study_config
from trader.data.calendars import ExchangeCalendar
from trader.data.manifest import load_manifest
from trader.domain.base import ConfigModel
from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.ids import deterministic_id
from trader.domain.market import Bar
from trader.domain.types import BPS, Direction, Track
from trader.features.daily import FEATURE_VERSION, DailyFeatures, DailyWindows
from trader.research.engine import session_dates
from trader.research.registry import ExperimentRegistry
from trader.research.study import (
    WARMUP,
    TrialCfg,
    _g2,
    build_registration,
    build_strategy,
    deployment_windows,
    load_study,
    open_study,
    resolve_rule,
    run_windows,
    summarize,
    walk_forward,
    warmup_start,
)
from trader.strategies import registry
from trader.strategies.research_rules import MeanReversion, TrendBreakout, load_rule_spec

REPO = Path(__file__).resolve().parents[1]
SPECS = REPO / "src" / "trader" / "strategies" / "specs"
FAMILY = "TEST_DAILY_DIP"


# ----------------------------------------------------------------------------- toy family


class ToyDipSpec(ConfigModel):
    strategy_id: str
    version: int
    family: Literal["TEST_DAILY_DIP"]
    track: Track
    feature_version: Literal["daily-v1"]
    holding: Literal["OVERNIGHT"]
    max_hold_sessions: int
    target_role: Literal["COST_GATE"]
    trend_sma: int
    exit_sma: int
    rsi_period: int
    rsi_below: Decimal
    atr_period: int
    stop_atr: Decimal
    entry_limit_offset_bps: Decimal
    gap_allowance_bps: Decimal
    research_status: str

    @property
    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="python"))


CREATED: list[ToyDipRule] = []


class ToyDipRule:
    """Test-only daily rule: a close above SMA(trend) with RSI below a level; exits on a close
    above SMA(exit). Parameters are arbitrary: it exercises the machinery, nothing else."""

    uses_source_vwap = False

    def __init__(
        self, spec: ToyDipSpec, calendar: ExchangeCalendar, ticks: Mapping[str, Decimal]
    ) -> None:
        self.spec = spec
        self.calendar = calendar
        self.ticks = dict(ticks)
        self.windows = DailyWindows(
            sma_periods=(spec.exit_sma, spec.trend_sma),
            rsi_period=spec.rsi_period,
            atr_period=spec.atr_period,
        )
        self.state: dict[str, DailyFeatures] = {}
        self.counts: list[tuple[str, date | None, int]] = []
        CREATED.append(self)

    @property
    def strategy_id(self) -> str:
        return self.spec.strategy_id

    @property
    def spec_hash(self) -> str:
        return self.spec.spec_hash

    def features(self, iid: str) -> DailyFeatures:
        if iid not in self.state:
            self.state[iid] = DailyFeatures(self.windows, self.calendar)
        return self.state[iid]

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        f = self.features(bar.instrument_id)
        f.update(bar)
        self.counts.append((bar.instrument_id, f.session, f.count))
        s = self.spec
        if not f.ready or f.close <= f.sma(s.trend_sma) or f.rsi() >= s.rsi_below:
            return None
        tick = self.ticks.get(bar.instrument_id, Decimal("0.01"))
        stop = ((f.close - s.stop_atr * f.atr()) / tick).to_integral_value(ROUND_FLOOR) * tick
        target = (f.sma(s.exit_sma) / tick).to_integral_value(ROUND_CEILING) * tick
        if stop <= 0 or target <= f.close:
            return None
        return TradeCandidate(
            candidate_id=deterministic_id("cand", s.strategy_id, bar.instrument_id, bar.end),
            strategy_id=s.strategy_id,
            strategy_version=s.version,
            spec_hash=s.spec_hash,
            instrument_id=bar.instrument_id,
            track=s.track,
            direction=Direction.LONG,
            feed=bar.feed,
            bar_end=bar.end,
            decision_time=bar.available_time,
            reference_price=bar.close,
            stop_price=stop,
            target_price=target,
            max_hold_minutes=1440 * 30,
            entry_limit_offset_bps=s.entry_limit_offset_bps,
            entry_ttl_seconds=60,
            gap_allowance_fraction=s.gap_allowance_bps * BPS,
            snapshot_hash=content_hash({"version": FEATURE_VERSION, "features": f.snapshot()}),
            expires_at=bar.available_time + timedelta(minutes=5),
        )

    def exit_signal(self, instrument_id: str, bar: Bar) -> str | None:
        f = self.state[instrument_id]
        return "close_above_sma" if f.ready and f.close > f.sma(self.spec.exit_sma) else None

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        self.features(instrument_id).rescale(divisor)

    def data_ok(self, instrument_id: str) -> bool:
        return True

    def gaps(self) -> int:
        return sum(f.gaps for f in self.state.values())


def _build(spec: Any, calendar: Any, ticks: Mapping[str, Decimal]) -> ToyDipRule:
    return ToyDipRule(spec, calendar, ticks)


TOY = registry.RuleFamily(
    family=FAMILY,
    label="Test daily dip (toy)",
    parse=ToyDipSpec.model_validate,
    build=_build,
    features=("SMA(trend)", "SMA(exit)", "Wilder RSI", "Wilder ATR"),
)

TOY_SPEC = {
    "strategy_id": "test_daily_dip",
    "version": 1,
    "family": FAMILY,
    "track": "STOCKS",
    "feature_version": "daily-v1",
    "holding": "OVERNIGHT",
    "max_hold_sessions": 5,
    "target_role": "COST_GATE",
    "trend_sma": 50,
    "exit_sma": 5,
    "rsi_period": 2,
    "rsi_below": "15",
    "atr_period": 10,
    "stop_atr": "3",
    "entry_limit_offset_bps": "5",
    "gap_allowance_bps": "10",
    "research_status": "TEST ONLY - never a registered trial",
}


@pytest.fixture
def toy(tmp_path: Path) -> Iterator[TrialCfg]:
    registry.register(TOY)
    path = tmp_path / "specs" / "toy.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(TOY_SPEC), encoding="utf-8")
    CREATED.clear()
    try:
        yield TrialCfg(trial_id="T-9901", spec_file="specs/toy.yaml", hypothesis="toy " * 6)
    finally:
        registry.unregister(FAMILY)
        CREATED.clear()


@pytest.fixture(scope="module")
def fixture_data() -> Any:
    return daily_history()


# ----------------------------------------------------------------------------- registry


@pytest.mark.parametrize(
    ("spec_file", "overrides", "cls"),
    [
        ("candidate_a_crypto_v1.yaml", {}, TrendBreakout),
        ("candidate_a_crypto_v1.yaml", {"atr_stop_multiplier": "1.0"}, TrendBreakout),
        ("candidate_b_crypto_v1.yaml", {}, MeanReversion),
        ("candidate_b_stocks_v1.yaml", {}, MeanReversion),
    ],
)
def test_built_in_families_resolve_exactly_as_before(
    spec_file: str, overrides: dict[str, str], cls: type
) -> None:
    before = load_rule_spec(SPECS / spec_file, overrides)
    after = registry.load_spec(SPECS / spec_file, overrides)
    assert type(after) is type(before) and after == before
    assert after.spec_hash == before.spec_hash
    assert type(registry.build(after, CALENDAR, {})) is cls


def test_registry_refuses_unknown_and_built_in_replacements(tmp_path: Path) -> None:
    assert {"CANDIDATE_A", "CANDIDATE_B"} <= set(registry.families())
    raw = dict(TOY_SPEC)
    path = tmp_path / "x.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(registry.UnknownRuleFamilyError, match="unknown rule family"):
        registry.load_spec(path)
    with pytest.raises(ValueError, match="built-in"):
        registry.register(registry.RuleFamily("CANDIDATE_A", "x", dict, _build))
    registry.register(TOY)
    try:
        with pytest.raises(ValueError, match="already registered"):
            registry.register(TOY)
        assert registry.family_label(FAMILY) == "Test daily dip (toy)"
    finally:
        registry.unregister(FAMILY)
    assert registry.family_label("NOT_A_FAMILY") == "NOT_A_FAMILY"


def test_a_family_module_is_imported_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("tests._lazy_daily_family")
    module.RULE_FAMILY = registry.RuleFamily("TEST_LAZY", "Lazy", ToyDipSpec.model_validate, _build)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(registry.FAMILY_MODULES, "TEST_LAZY", module.__name__)
    monkeypatch.setitem(registry.FAMILY_MODULES, "TEST_WRONG", module.__name__)
    try:
        assert registry.rule_family("TEST_LAZY") is module.RULE_FAMILY  # type: ignore[attr-defined]
        assert "TEST_LAZY" in registry.families()
        with pytest.raises(registry.UnknownRuleFamilyError, match="does not define"):
            registry.rule_family("TEST_WRONG")
    finally:
        registry.unregister("TEST_LAZY")


def test_the_registered_trials_still_match_their_registry_entries() -> None:
    """Read-only: every committed REGISTER of the two baseline studies verifies unchanged."""
    manifest = load_manifest(REPO / "research" / "data_manifest.json")
    reg = ExperimentRegistry(REPO / "research" / "experiment_registry.jsonl")
    for name in ("crypto_baseline_v1", "stocks_baseline_v1"):
        ctx = open_study(REPO, load_study(REPO / "configs" / "research" / f"{name}.yaml"), manifest)
        if ctx.manifest.dataset(ctx.config.price_dataset).status != "ACQUIRED":
            continue  # registrations need the dataset's content hashes
        for trial in ctx.config.trials:
            reg.require_registered(build_registration(ctx, trial))


def test_warmup_and_capitals_are_bound_only_when_declared() -> None:
    manifest = load_manifest(REPO / "research" / "data_manifest.json")
    cfg = load_study(REPO / "configs" / "research" / "crypto_baseline_v1.yaml")
    trial = cfg.trials[0]
    plain = build_registration(open_study(REPO, cfg, manifest), trial)
    assert "warmup_sessions" not in plain.splits
    assert "diagnostic_capitals_usd" not in plain.cost_basis
    warm = build_registration(
        open_study(REPO, cfg.model_copy(update={"warmup_sessions": 30}), manifest), trial
    )
    assert warm.splits == {**plain.splits, "warmup_sessions": 30}
    assert warm.content_hash != plain.content_hash
    capitals = build_registration(
        open_study(
            REPO, cfg.model_copy(update={"diagnostic_capitals": (Decimal(1000),)}), manifest
        ),
        trial,
    )
    assert capitals.cost_basis == {**plain.cost_basis, "diagnostic_capitals_usd": ["1000"]}
    assert capitals.content_hash != plain.content_hash


# ----------------------------------------------------------------------------- warm-up


def test_warmup_start_counts_exchange_sessions_or_keeps_two_days(fixture_data: Any) -> None:
    start = datetime(2018, 4, 1, tzinfo=UTC)
    ctx, _ = daily_study(study_config([]), fixture_data)
    assert warmup_start(ctx, start) == start - WARMUP
    ctx, _ = daily_study(study_config([], warmup_sessions=3), fixture_data)
    # sessions before 1 April 2018: 29 March (Good Friday the 30th is a holiday), 28, 27
    assert warmup_start(ctx, start) == CALENDAR.session_for_date(date(2018, 3, 27)).open  # type: ignore[union-attr]
    utc_ctx, _ = daily_study(
        study_config([], warmup_sessions=3, session_basis="UTC_DAY"), fixture_data
    )
    assert warmup_start(utc_ctx, start) == start - timedelta(days=3)


def test_warmup_sessions_make_long_lookbacks_warm_at_every_window_start(
    toy: TrialCfg, tmp_path: Path, fixture_data: Any
) -> None:
    cfg = study_config([toy], ["ARCX:SPY"], warmup_sessions=60)
    ctx, data = daily_study(cfg, fixture_data, repo_root=tmp_path)
    windows = deployment_windows(cfg.splits)[:3]
    run_windows(ctx, toy, data, "base", windows)
    assert len(CREATED) == len(windows)  # a fresh rule (fresh state) per window
    for rule, (start, _) in zip(CREATED, windows, strict=True):
        first = next(c for c in rule.counts if c[1] is not None and c[1] >= start.date())
        assert first[2] == 61  # 60 warm-up sessions, then the window's first session
    CREATED.clear()
    cold = study_config([toy], ["ARCX:SPY"])
    ctx, data = daily_study(cold, fixture_data, repo_root=tmp_path)
    run_windows(ctx, toy, data, "base", windows)
    for rule, (start, _) in zip(CREATED, windows, strict=True):
        first = next(c for c in rule.counts if c[1] is not None and c[1] >= start.date())
        assert first[2] <= 3  # ADR-0020's two calendar days hold at most two sessions


# ----------------------------------------------------------------------------- study path


def test_a_registered_daily_family_runs_the_study_path_on_daily_bars(
    toy: TrialCfg, tmp_path: Path, fixture_data: Any
) -> None:
    cfg = study_config([toy], warmup_sessions=60)
    ctx, data = daily_study(cfg, fixture_data, repo_root=tmp_path)
    spec = resolve_rule(ctx, toy)
    assert isinstance(spec, ToyDipSpec)
    assert isinstance(build_strategy(ctx, spec, {}), ToyDipRule)
    windows = deployment_windows(cfg.splits)
    assert len(windows) == 14  # 2017-01 .. 2020-07 in three-month steps
    run = run_windows(ctx, toy, data, "base", windows)
    expected = [
        d for start, end in windows for d in session_dates("XNYS", ctx.calendar, start, end)
    ]
    assert list(run.series.days) == expected  # one value per XNYS session
    assert len(run.series.days) == len(set(run.series.days))
    assert run.trades, "the fixture's dips must produce trades"
    reasons = {t["exit_reason"] for t in run.trades}
    assert "SIGNAL:close_above_sma" in reasons
    assert reasons <= {"SIGNAL:close_above_sma", "TIME_EXIT", "STOP", "STOP_GAP", "END_OF_DATA"}
    opens = {CALENDAR.session_on(d).open for d in expected}  # type: ignore[union-attr]
    for t in run.trades:
        entry = datetime.fromisoformat(str(t["entry"]["time"]).replace("Z", "+00:00"))
        assert entry in opens  # every entry at a session open
        assert Decimal(str(t["planned_loss_usd"])) <= Decimal("0.125")
    # every window is an independent USD 50 deployment
    assert len(run.window_nets) == len(windows)
    summary = summarize(ctx, run.series, run.trades, run.candidates)
    assert summary["sessions"] == len(expected) and summary["trades"] == len(run.trades)
    assert summary["exit_reasons"].get("SIGNAL:close_above_sma", 0) > 0
    wf, selected, log = walk_forward(
        cfg.splits, [toy.trial_id], {toy.trial_id: run.series}, {toy.trial_id: run.trades}
    )
    assert len(log) == 9 and all(f["selected"] in (toy.trial_id, "CASH") for f in log)
    assert set(wf.days) <= set(expected)
    gate = _g2(summarize(ctx, wf, selected, seed_offset=1000), None)
    assert gate["status"] == "NOT_PASSED"  # synthetic data, no reality check: never a pass
    again = run_windows(ctx, toy, data, "base", windows)
    assert again.result_hash == run.result_hash
