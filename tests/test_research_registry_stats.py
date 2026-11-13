"""Experiment registry, walk-forward splits, statistics and the study runner
(risk_and_validation.md §5, §7, §8; roadmap §2)."""

from __future__ import annotations

import itertools
import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from trader.data.calendars import ExchangeCalendar
from trader.data.ingest import IngestContext, ingest
from trader.data.manifest import load_manifest
from trader.research.engine import session_dates
from trader.research.registry import ExperimentRegistry, RegistryError, TrialRegistration
from trader.research.report import render_baseline_report
from trader.research.splits import HoldoutLockedError, SplitPlan, WalkForward, folds, guard_window
from trader.research.stats import (
    bootstrap_mean,
    deflated_sharpe,
    effective_sample_size,
    reality_check,
    sessions_for_power,
)
from trader.research.study import (
    SessionSeries,
    StudyConfig,
    build_registration,
    open_study,
    result_digest,
    run_baseline_study,
    walk_forward,
)

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def trial(n: int, **kw: object) -> TrialRegistration:
    fields: dict[str, object] = {
        "trial_id": f"T-{n:04d}",
        "study_id": "s",
        "family": "CANDIDATE_A",
        "track": "CRYPTO",
        "hypothesis": "a sufficiently long hypothesis statement",
        "universe": ("KRAKEN:BTC/USD",),
        "universe_selection": "fixed",
        "horizon": "6h",
        "rule": {"spec_hash": "x"},
        "features": ("atr",),
        "feature_version": "v",
        "entry_exit_timing": "t",
        "cost_basis": {"fees": "k"},
        "parameters": {"a": "1"},
        "splits": {"d": "1"},
        "metrics": ("net",),
        "selection_correction": "declared",
        "datasets": ({"dataset": "d", "content_sha256": "c"},),
    }
    fields.update(kw)
    return TrialRegistration.model_validate(fields)


# ----------------------------------------------------------------------------- registry


def test_registration_precedes_runs_and_changes_are_refused(tmp_path: Path) -> None:
    reg = ExperimentRegistry(tmp_path / "r.jsonl")
    assert reg.register(trial(1), NOW)
    assert not reg.register(trial(1), NOW)  # identical: no-op
    with pytest.raises(RegistryError, match="different content"):
        reg.register(trial(1, horizon="1h"), NOW)
    reg.require_registered(trial(1))
    with pytest.raises(RegistryError, match="differs"):
        reg.require_registered(trial(1, cost_basis={"fees": "cheaper"}))
    with pytest.raises(RegistryError, match="not registered"):
        reg.require_registered(trial(2))
    assert reg.record_result(
        "T-0001", run_id="r1", result_hash="h", verdict="NEGATIVE_NET", summary={}, now=NOW
    )
    assert not reg.record_result(
        "T-0001", run_id="r1", result_hash="h", verdict="NEGATIVE_NET", summary={}, now=NOW
    )


def test_budget_of_ten_needs_a_recorded_decision_to_grow(tmp_path: Path) -> None:
    reg = ExperimentRegistry(tmp_path / "r.jsonl")
    for n in range(1, 11):
        reg.register(trial(n), NOW)
    with pytest.raises(RegistryError, match="budget exhausted"):
        reg.register(trial(11), NOW)
    with pytest.raises(RegistryError, match="written reason"):
        reg.decide_budget(12, reason="more", by="owner", now=NOW)
    reg.decide_budget(
        12, reason="reviewed evidence and data adequacy after M2", by="owner", now=NOW
    )
    assert reg.register(trial(11), NOW)
    reloaded = ExperimentRegistry(tmp_path / "r.jsonl")
    assert reloaded.budget() == 12 and len(reloaded.registrations()) == 11


def test_an_edited_or_deleted_registry_line_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    reg = ExperimentRegistry(path)
    for n in (1, 2, 3):
        reg.register(trial(n), NOW)
    lines = path.read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[1])
    edited["payload"]["trial"]["hypothesis"] = "a quietly improved hypothesis statement"
    path.write_text("\n".join([lines[0], json.dumps(edited), lines[2]]) + "\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="altered"):
        ExperimentRegistry(path)
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    with pytest.raises(RegistryError):
        ExperimentRegistry(path)


def test_holdout_unlock_is_recorded_once(tmp_path: Path) -> None:
    reg = ExperimentRegistry(tmp_path / "r.jsonl")
    assert not reg.holdout_unlocked("s")
    reg.unlock_holdout(
        "s", reason="final evaluation after the frozen selection", by="owner", now=NOW
    )
    assert reg.holdout_unlocked("s")
    with pytest.raises(RegistryError, match="already"):
        reg.unlock_holdout(
            "s", reason="final evaluation after the frozen selection", by="owner", now=NOW
        )


# ----------------------------------------------------------------------------- splits


def crypto_plan() -> SplitPlan:
    return SplitPlan(
        development_start=datetime(2023, 11, 1, tzinfo=UTC),
        holdout_start=datetime(2026, 8, 1, tzinfo=UTC),
        holdout_end=datetime(2026, 11, 1, tzinfo=UTC),
        walk_forward=WalkForward(),
        embargo_minutes=360,
    )


def test_walk_forward_folds_roll_and_never_touch_the_holdout() -> None:
    plan = crypto_plan()
    fs = folds(plan)
    assert len(fs) == 6
    assert fs[0].train == (datetime(2023, 11, 1, tzinfo=UTC), datetime(2024, 11, 1, tzinfo=UTC))
    assert fs[0].validation[1] == datetime(2025, 2, 1, tzinfo=UTC) - timedelta(minutes=360)
    assert fs[-1].test[1] == plan.holdout_start
    for a, b in itertools.pairwise(fs):
        assert a.test[1] == b.test[0]  # contiguous out-of-sample test windows
    with pytest.raises(HoldoutLockedError):
        guard_window(
            plan, plan.development_start, plan.holdout_start + timedelta(days=1), unlocked=False
        )
    guard_window(plan, plan.development_start, plan.holdout_start, unlocked=False)
    guard_window(plan, plan.development_start, plan.holdout_end, unlocked=True)


def test_walk_forward_selection_uses_validation_only_and_holds_cash_without_an_edge() -> None:
    plan = crypto_plan()
    days = [
        date(2023, 11, 1) + timedelta(days=i)
        for i in range((date(2026, 8, 1) - date(2023, 11, 1)).days)
    ]

    def series(daily: float) -> SessionSeries:
        pnl = tuple(daily for _ in days)
        eq = tuple(50 + daily * (i + 1) for i in range(len(days)))
        z = tuple(0 for _ in days)
        return SessionSeries(
            tuple(days), pnl, eq, z, tuple(0.0 for _ in days), z, tuple(0.0 for _ in days)
        )

    def trade(day: date, net: float) -> dict[str, object]:
        t = datetime(day.year, day.month, day.day, 12, tzinfo=UTC).isoformat()
        return {"entry": {"time": t}, "exits": [{"time": t}], "net_pnl": str(net)}

    good_trades = [trade(d, 0.01) for d in days]
    bad_trades = [trade(d, -0.01) for d in days]
    wf, selected, log = walk_forward(
        plan,
        ["T-A", "T-B"],
        {"T-A": series(-0.01), "T-B": series(0.01)},
        {"T-A": bad_trades, "T-B": good_trades},
    )
    assert {f["selected"] for f in log} == {"T-B"}
    assert len(wf.days) == (date(2026, 8, 1) - date(2025, 2, 1)).days
    assert all(
        datetime.fromisoformat(str(t["entry"]["time"])).date() >= date(2025, 2, 1) for t in selected
    )
    wf2, _, log2 = walk_forward(plan, ["T-A"], {"T-A": series(-0.01)}, {"T-A": bad_trades})
    assert {f["selected"] for f in log2} == {"CASH"} and sum(wf2.pnl) == 0


# ----------------------------------------------------------------------------- statistics


def test_block_bootstrap_interval_covers_a_known_mean() -> None:
    rng = np.random.default_rng(3)
    x = rng.normal(0.05, 1.0, 600)
    ci = bootstrap_mean(x, mean_block=5, replicates=2000, seed=1)
    assert ci.lower < 0.05 < ci.upper
    assert ci.one_sided_lower > ci.lower


def test_reality_check_bound_is_below_the_naive_best_and_flags_pure_noise() -> None:
    rng = np.random.default_rng(4)
    noise = rng.normal(0, 1, (8, 500))
    rc = reality_check(noise, mean_block=5, replicates=1000, seed=2)
    naive = bootstrap_mean(noise[int(rc["best_index"])], mean_block=5, replicates=1000, seed=2)
    assert rc["selection_adjusted_lower_bound"] < naive.one_sided_lower
    assert rc["selection_adjusted_lower_bound"] < 0 and rc["p_value_no_positive_mean"] > 0.05


def test_deflated_sharpe_penalises_more_trials_and_autocorrelation_shrinks_the_sample() -> None:
    rng = np.random.default_rng(5)
    x = rng.normal(0.08, 1, 400)
    sharpes = list(rng.normal(0, 0.05, 10))
    one = deflated_sharpe(x, trial_sharpes=sharpes, n_trials=1)["dsr"]
    many = deflated_sharpe(x, trial_sharpes=sharpes, n_trials=10)["dsr"]
    assert many < one
    ar = np.zeros(1000)
    for t in range(1, 1000):
        ar[t] = 0.6 * ar[t - 1] + rng.normal()
    n_eff, vif = effective_sample_size(ar)
    assert n_eff < 500 and vif > 2
    assert sessions_for_power(1.0, 0.1, vif=2.0) == pytest.approx(2 * sessions_for_power(1.0, 0.1))
    assert sessions_for_power(1.0, 0.0) == float("inf")


# ----------------------------------------------------------------------------- study end to end


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(REPO / "configs", root / "configs")
    shutil.copytree(
        REPO / "src" / "trader" / "strategies" / "specs",
        root / "src" / "trader" / "strategies" / "specs",
    )
    return root


def _study(root: Path) -> StudyConfig:
    return StudyConfig.model_validate(
        {
            "study_id": "synthetic_stock_study",
            "title": "SYNTHETIC pipeline check",
            "track": "STOCKS",
            "description": "pipeline test on synthetic bars",
            "price_dataset": "synthetic_equities_5m_v1",
            "instruments": [
                {"price_instrument_id": "XNYS:SYNA", "execution_instrument_id": "XNYS:SYNA"},
                {"price_instrument_id": "XNYS:SYNB", "execution_instrument_id": "XNYS:SYNB"},
            ],
            "instrument_specs": "configs/instruments/synthetic_equities.yaml",
            "fee_schedule": "configs/fees/replay_demo.yaml",
            "policy": "configs/policy/base.yaml",
            "fills": {
                "half_spread_bps": "1",
                "slippage_bps": "2",
                "participation": "0.10",
                "latency_ms": 500,
            },
            "stress": {"multiplier": "2", "missed_entry_percent": 5},
            "loss_windows": ["NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"],
            "session_basis": "XNYS",
            "annualisation_sessions": 252,
            "splits": {
                "development_start": "2026-10-01T00:00:00Z",
                "holdout_start": "2026-12-01T00:00:00Z",
                "holdout_end": "2027-01-01T00:00:00Z",
                "walk_forward": {
                    "train_months": 1,
                    "validation_months": 1,
                    "test_months": 1,
                    "step_months": 1,
                },
                "embargo_minutes": 60,
            },
            "operating_costs_usd_per_month": "0",
            "bootstrap": {"replicates": 200, "mean_block_sessions": "3", "seed": 1},
            "universe_selection": "synthetic",
            "horizon": "intraday",
            "entry_exit_timing": "next open",
            "trials": [
                {
                    "trial_id": "T-0001",
                    "spec_file": "src/trader/strategies/specs/candidate_a_v1.yaml",
                    "hypothesis": "synthetic pipeline check for Candidate A",
                },
                {
                    "trial_id": "T-0002",
                    "spec_file": "src/trader/strategies/specs/candidate_b_stocks_v1.yaml",
                    "hypothesis": "synthetic pipeline check for Candidate B",
                },
            ],
        }
    )


@pytest.mark.slow
def test_study_runs_only_registered_trials_reproducibly_and_reports_honestly(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    manifest = load_manifest(REPO / "research" / "data_manifest.json")
    ds = ingest(
        manifest.dataset("synthetic_equities_5m_v1"),
        IngestContext(repo_root=root, transport=None, now=NOW),
    )  # type: ignore[arg-type]
    manifest = manifest.replace(ds)
    ctx = open_study(root, _study(root), manifest)
    reg = ExperimentRegistry(tmp_path / "registry.jsonl")
    with pytest.raises(RegistryError, match="not registered"):
        run_baseline_study(ctx, reg)
    for t in ctx.config.trials:
        reg.register(build_registration(ctx, t), NOW)
    first = run_baseline_study(ctx, reg)
    second = run_baseline_study(ctx, reg)
    assert result_digest(first) == result_digest(second)
    assert set(first["trials"]) == {"T-0001", "T-0002"}
    base = first["trials"]["T-0001"]["views"]["base"]
    expected_sessions = session_dates(
        "XNYS",
        ExchangeCalendar(),
        ctx.config.splits.development_start,
        ctx.config.splits.holdout_start,
    )
    assert base["sessions"] == len(expected_sessions) == 42  # zero-trade sessions included
    assert first["g2"]["status"] == "NOT_PASSED"
    assert first["walk_forward"]["folds"] == []  # too little data for a fold: nothing is claimed
    report = render_baseline_report(
        first,
        generated_at="test",
        digest=result_digest(first),
        stock_status={
            "dataset": "alpaca_us_equities_5m_v1",
            "status": "BLOCKED",
            "detail": "no keys",
        },
    )
    assert "Evidence class: BACKTEST" in report and "G2 status: NOT_PASSED" in report
    # editing the cost basis after registration is refused
    changed = ctx.config.model_copy(update={"fee_schedule": "configs/fees/ibkr_us_fractional.yaml"})
    with pytest.raises(RegistryError, match="differs"):
        run_baseline_study(open_study(root, changed, manifest), reg)


# ----------------------------------------------------------------------------- CLI


def test_research_cli_owner_decisions_are_recorded_and_the_committed_registry_verifies(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    from trader.cli.main import app

    runner = CliRunner()
    committed = runner.invoke(app, ["research", "status"])
    assert committed.exit_code == 0, committed.output
    assert "chain verified" in committed.output and "T-0005" in committed.output
    path = str(tmp_path / "r.jsonl")
    ExperimentRegistry(tmp_path / "r.jsonl").register(trial(1), NOW)
    short = runner.invoke(
        app,
        ["research", "decide-budget", "--new-budget", "12", "--reason", "x", "--registry", path],
    )
    assert short.exit_code != 0  # a decision needs a written reason
    ok = runner.invoke(
        app,
        [
            "research",
            "decide-budget",
            "--new-budget",
            "12",
            "--reason",
            "reviewed the M2 baseline evidence",
            "--registry",
            path,
        ],
    )
    assert ok.exit_code == 0 and "budget is now 12" in ok.output
    unlock = runner.invoke(
        app,
        [
            "research",
            "unlock-holdout",
            "--study-id",
            "s",
            "--reason",
            "final evaluation after freezing",
            "--registry",
            path,
        ],
    )
    assert unlock.exit_code == 0
    assert ExperimentRegistry(tmp_path / "r.jsonl").holdout_unlocked("s")
