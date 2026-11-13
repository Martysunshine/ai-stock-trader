"""M3 model study (implementation_roadmap.md M3 and §2; strategy_and_profitability.md §6-§7;
risk_and_validation.md §5; docs/ml_research_guide.md §5).

* The two crypto filter trials register the base rule unchanged plus the frozen filter recipe
  (model, grid, calibration, threshold rule, controls, seeds, feature and label definitions,
  library versions); a run refuses a trial that is unregistered or whose recipe changed, before
  any market data is read.
* End to end on SYNTHETIC bars only (never real data): register -> run -> report. Training sees
  no test-window information, the random and equal-turnover controls are deterministic and
  matched to the model, an untrainable fold fails closed (cash), the locked final test period is
  never read, and the report renders every roadmap section.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests import model_study_fixtures as fx
from trader.data.manifest import load_manifest
from trader.features.candidate_features import FEATURE_NAMES, FEATURE_VERSION
from trader.models.bundle import ModelBundle
from trader.research import markdown, report
from trader.research.labels import LABEL_RULE
from trader.research.model_report import render_model_report
from trader.research.model_study import (
    ModelStudyConfig,
    ModelStudyContext,
    ModelStudyError,
    build_model_registration,
    build_trial_labels,
    load_model_study,
    open_model_study,
    record_model_results,
    run_model_study,
    save_model_outputs,
)
from trader.research.registry import ExperimentRegistry, RegistryError
from trader.research.splits import HoldoutLockedError
from trader.research.study import TrialCfg, build_registration, result_digest

REPO = Path(__file__).resolve().parents[1]
CRYPTO_FILTER = REPO / "configs" / "research" / "crypto_filter_v1.yaml"
MANIFEST = REPO / "research" / "data_manifest.json"


def crypto_filter() -> ModelStudyContext:
    """The committed crypto filter study (registrations read the manifest, never the bars)."""
    return open_model_study(REPO, load_model_study(CRYPTO_FILTER), load_manifest(MANIFEST))


def no_bars(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("market data was read before the registration check")


# ----------------------------------------------------------------------------- report helpers


def test_report_helpers_live_in_markdown_and_name_the_roadmap_reports() -> None:
    assert report._f is markdown.fmt and report._pct is markdown.pct
    assert report._ci is markdown.ci and report._table is markdown.table
    assert markdown.fmt(None) == "n/a" and markdown.fmt(True) == "yes"
    assert markdown.fmt(1.23456, 2) == "1.23" and markdown.pct(0.125) == "12.5%"
    assert markdown.report_file_name("crypto_baseline_v1") == "baseline_report.md"
    assert markdown.report_file_name("crypto_filter_v1") == "model_report.md"
    assert markdown.report_file_name("stocks_filter_v1") == "stocks_filter_v1_report.md"
    from trader.cli.main import REPO_ROOT, default_report_path

    reports = REPO_ROOT / "research" / "reports"
    assert default_report_path("crypto_filter_v1") == reports / "model_report.md"


# ----------------------------------------------------------------------------- registration


def test_crypto_filter_study_registers_exactly_two_filter_trials(tmp_path: Path) -> None:
    ctx = crypto_filter()
    assert ctx.base.config.study_id == "crypto_baseline_v1"
    regs = [build_model_registration(ctx, t) for t in ctx.config.trials]
    assert [r.trial_id for r in regs] == ["T-0011", "T-0012"]
    assert [r.family for r in regs] == ["CANDIDATE_A+LOGIT_FILTER", "CANDIDATE_B+LOGIT_FILTER"]
    assert {r.study_id for r in regs} == {"crypto_filter_v1"}
    # Candidate A at its spec defaults is the rule of baseline trial T-0004 (1.5 ATR, 2.0R)
    t4 = build_registration(
        ctx.base,
        TrialCfg(
            trial_id="T-0004",
            spec_file="src/trader/strategies/specs/candidate_a_crypto_v1.yaml",
            overrides={"atr_stop_multiplier": "1.5", "reward_risk_multiple": "2.0"},
            hypothesis="spec hash comparison only, never registered",
        ),
    )
    assert regs[0].rule["spec_hash"] == t4.rule["spec_hash"]
    for r in regs:
        assert r.cost_basis == t4.cost_basis and r.datasets == t4.datasets
        assert r.splits == t4.splits and r.universe == t4.universe
        assert r.features == FEATURE_NAMES and r.feature_version == FEATURE_VERSION
        assert all(isinstance(v, str) for v in r.parameters.values())
        assert r.rule["base_study"] == "crypto_baseline_v1"
        definition = r.rule["filter"]
        assert definition["model_type"] == "logistic"
        assert definition["calibration"] == "platt"
        assert definition["grid"] == [
            {"C": "0.1", "class_weight": "none"},
            {"C": "0.1", "class_weight": "balanced"},
            {"C": "1", "class_weight": "none"},
            {"C": "1", "class_weight": "balanced"},
        ]
        assert definition["threshold_grid"] == [
            "0.3",
            "0.35",
            "0.4",
            "0.45",
            "0.5",
            "0.55",
            "0.6",
            "0.65",
            "0.7",
        ]
        assert definition["min_accepts"] == "20"
        assert definition["controls"]["random_seeds"] == "20"
        assert definition["controls"]["turnover_matched"] == "true"
        assert definition["label_definition"] == LABEL_RULE
        assert definition["feature_set"] == list(FEATURE_NAMES)
        assert {"python", "numpy", "scikit-learn"} <= set(definition["library_versions"])
        assert "validation log-loss" in definition["inner_selection_metric"]
        assert "validation" in definition["threshold_rule"]
        assert "N = all registered trials" in r.selection_correction
        assert len(r.content_hash) == 64  # canonical JSON: no binary float anywhere
    reg = ExperimentRegistry(tmp_path / "registry.jsonl")
    assert all(reg.register(r, fx.NOW) for r in regs)
    assert not reg.register(regs[0], fx.NOW)  # identical: already registered


def test_register_cli_dispatches_on_the_study_kind(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from trader.cli.main import app

    runner = CliRunner()
    registry = tmp_path / "registry.jsonl"
    out = runner.invoke(
        app, ["research", "register", "--study", str(CRYPTO_FILTER), "--registry", str(registry)]
    )
    assert out.exit_code == 0, out.output
    assert "T-0011: registered" in out.output and "T-0012: registered" in out.output
    assert set(ExperimentRegistry(registry).registrations()) == {"T-0011", "T-0012"}
    baseline = runner.invoke(
        app, ["research", "baseline", "--study", str(CRYPTO_FILTER), "--registry", str(registry)]
    )
    assert baseline.exit_code == 2 and "research models" in baseline.output


def test_model_study_configuration_fails_closed() -> None:
    raw = fx.model_study_raw()
    with pytest.raises(ValidationError, match="duplicates"):
        ModelStudyConfig.model_validate(fx.model_study_raw(threshold_grid=["0.5", "0.50"]))
    with pytest.raises(ValidationError, match="unknown logistic grid keys"):
        ModelStudyConfig.model_validate(fx.model_study_raw(grid=[{"penalty": "l1"}]))
    with pytest.raises(ValidationError, match="unique"):
        ModelStudyConfig.model_validate({**raw, "trials": [raw["trials"][0]] * 2})
    with pytest.raises(ModelStudyError, match="not a model study"):
        load_model_study(REPO / "configs" / "research" / "crypto_baseline_v1.yaml")
    ctx = crypto_filter()
    wrong = ctx.config.trials[0].model_copy(update={"family": "CANDIDATE_B"})
    with pytest.raises(ModelStudyError, match="declared family"):
        build_model_registration(ctx, wrong)


def test_run_and_label_refuse_unregistered_or_changed_trials_before_reading_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("trader.research.model_study.load_development_data", no_bars)
    ctx = crypto_filter()
    reg = ExperimentRegistry(tmp_path / "registry.jsonl")
    with pytest.raises(RegistryError, match="not registered"):
        run_model_study(ctx, reg)
    for trial in ctx.config.trials:
        reg.register(build_model_registration(ctx, trial), fx.NOW)
    raw = ctx.config.model_dump(mode="json")
    raw["trials"][1]["min_accepts"] = 25  # the recipe changed after registration
    changed = ModelStudyContext(ctx.repo_root, ModelStudyConfig.model_validate(raw), ctx.base)
    with pytest.raises(RegistryError, match="differs"):
        run_model_study(changed, reg)
    holdout = ctx.base.config.splits
    with pytest.raises(HoldoutLockedError):
        build_trial_labels(
            ctx,
            ctx.config.trials[0],
            start=holdout.holdout_start - timedelta(days=10),
            end=holdout.holdout_start + timedelta(days=10),
        )


def test_models_and_labels_cli_refuse_before_reading_data(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from trader.cli.main import app

    runner = CliRunner()
    registry = str(tmp_path / "empty.jsonl")
    models = runner.invoke(
        app, ["research", "models", "--study", str(CRYPTO_FILTER), "--registry", registry]
    )
    assert models.exit_code == 2 and "not registered" in models.output
    common = ["research", "labels", "--study", str(CRYPTO_FILTER), "--registry", registry]
    out = str(tmp_path / "labels.parquet")
    holdout = runner.invoke(
        app,
        [*common, "--out", out, "--start", "2026-08-15T00:00:00Z", "--end", "2026-09-01T00:00:00Z"],
    )
    assert holdout.exit_code == 2 and "locked final test" in holdout.output
    unregistered = runner.invoke(
        app,
        [*common, "--out", out, "--start", "2025-01-01T00:00:00Z", "--end", "2025-02-01T00:00:00Z"],
    )
    assert unregistered.exit_code == 2 and "not registered" in unregistered.output
    assert not Path(out).exists()


# ----------------------------------------------------------------------------- end to end


def registered_study(tmp: Path, *, scramble_from: datetime | None = None) -> dict[str, Any]:
    """A SYNTHETIC workspace whose model study is registered in a fresh registry."""
    root = fx.workspace(tmp)
    manifest = fx.synthetic_manifest(root, scramble_from=scramble_from)
    fx.write_base_study(root)
    ctx = open_model_study(root, load_model_study(fx.write_model_study(root)), manifest)
    reg = ExperimentRegistry(tmp / "registry.jsonl")
    for trial in ctx.config.trials:
        assert reg.register(build_model_registration(ctx, trial), fx.NOW)
    return {"tmp": tmp, "root": root, "manifest": manifest, "ctx": ctx, "reg": reg}


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One registered run of the SYNTHETIC study (pipeline check, not evidence)."""
    study = registered_study(tmp_path_factory.mktemp("model_study"))
    study["run"] = run_model_study(study["ctx"], study["reg"])
    return study


@pytest.mark.slow
def test_synthetic_model_study_runs_end_to_end_and_reports(synthetic: dict[str, Any]) -> None:
    ctx: ModelStudyContext = synthetic["ctx"]
    reg: ExperimentRegistry = synthetic["reg"]
    first = synthetic["run"]
    second = run_model_study(ctx, reg)
    result = first.result
    # deterministic end to end: bundles, both controls' salts, summaries and statistics
    assert result_digest(result) == result_digest(second.result)
    assert [b.checksum for b in first.bundles] == [b.checksum for b in second.bundles]

    plan = ctx.base.config.splits
    assert [f["fold"] for f in result["folds"]] == [1]
    assert result["folds"][0]["test"] == ["2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z"]
    assert not result["holdout_unlocked"]

    a, b = result["trials"]["T-0011"], result["trials"]["T-0012"]
    fold_a, fold_b = a["folds"][0], b["folds"][0]

    # -- trained fold: training stops at the fold's training end, selection uses validation only
    assert fold_a["status"] == "TRAINED" and a["verdict"] in (
        "NEGATIVE_NET",
        "POSITIVE_UNCORRECTED",
    )
    (bundle,) = first.bundles
    assert bundle.bundle_id == fold_a["bundle_id"] and bundle.checksum == fold_a["bundle_checksum"]
    assert bundle.training_cutoff == plan.development_start.replace(month=2)
    assert bundle.threshold in {Decimal(t) for t in a["threshold_grid"]}
    assert fold_a["labels"]["train_rows_after_purge"] <= fold_a["labels"]["train_rows"]
    assert {"train_rows_sha256", "validation_rows_sha256"} <= set(bundle.data_hashes)
    stages = [e["stage"] for e in fold_a["selection_log"]]
    assert stages.count("grid") == 2 and "calibration" in stages and "threshold_selected" in stages
    replayed = {r["threshold"] for r in fold_a["threshold_replays"]}
    eligible = {e["threshold"] for e in fold_a["selection_log"] if e.get("eligible") == "true"}
    assert replayed == eligible  # the objective is a validation-window engine replay
    assert fold_a["val_metrics"]["objective_source"] == "callback"

    # -- the model filter saw every non-stale candidate; controls are matched to it
    runs = fold_a["runs"]
    model = runs["model_filter"]
    decisions, vetoes = int(model["filter_decisions"]), int(model["vetoes"])
    assert decisions > 0 and int(model["filter_accepts"]) == decisions - vetoes
    rate = (Decimal(vetoes) / Decimal(decisions)).quantize(Decimal("0.000001"))
    assert Decimal(model["veto_rate"]) == rate == Decimal(runs["random_control"]["rate"])
    assert len(runs["random_control"]["salts"]) == 2
    assert len({s["salt"] for s in runs["random_control"]["salts"]}) == 2
    for salt in runs["turnover_control"]["salts"]:
        assert salt["filter_accepts"] == model["filter_accepts"]  # equal accepted count
        assert salt["filter_decisions"] == model["filter_decisions"]  # the same candidates
    assert runs["rule_only"]["filter_hash"] == "" and runs["rule_only"]["filter_decisions"] == "0"

    # -- untrainable fold: full tuning history kept, every candidate vetoed, controls trade nothing
    assert fold_b["status"] == "UNTRAINABLE" and b["verdict"] == "UNTRAINABLE"
    assert "no threshold accepts" in fold_b["reason"]
    assert [e["stage"] for e in fold_b["selection_log"]].count("grid") == 2
    assert fold_b["runs"]["model_filter"]["filter_accepts"] == "0"
    assert fold_b["runs"]["model_filter"]["trades"] == "0"
    assert fold_b["runs"]["random_control"]["rate"] == "1"
    assert all(s["trades"] == "0" for s in fold_b["runs"]["random_control"]["salts"])

    # -- aggregation, selection-aware statistics and the gate
    variants = a["variants"]
    assert variants["rule_only"]["sessions"] == variants["model_filter"]["sessions"] == 31
    assert variants["random_control"]["seeds"] == variants["turnover_control"]["seeds"] == 2
    sel = result["selection_aware"]
    assert len(sel["rows"]) == 8 and sel["registered_trials"] == 2
    assert sel["reality_check"]["strategies"] == 8 and sel["reality_check"]["sessions"] == 31
    assert set(a["paired_differences"]) == {
        "model_minus_rule_only",
        "model_minus_random_control",
        "model_minus_turnover_control",
    }
    assert [f["selected"] for f in result["walk_forward"]["folds"]] in (
        ["CASH"],
        ["T-0011"],
    )  # T-0012 had no trained model, so it is never eligible
    assert result["g2"]["status"] == "NOT_PASSED"

    # -- report
    digest = result_digest(result)
    text = render_model_report(result, generated_at="test", digest=digest)
    for heading in (
        "## 1. Verdict",
        "## 2. Chronological splits",
        "## 4. Tuning history (validation only)",
        "## 5. Calibration",
        "## 6. Economic ablation (test windows)",
        "## 8. Selection-aware uncertainty",
        "## 9. Bias and limitations",
        "## 10. Gate G2 assessment",
    ):
        assert heading in text
    assert "Evidence class: BACKTEST" in text and "G2 status: NOT_PASSED" in text
    assert "documented rejection" in text and "UNTRAINABLE" in text
    assert "can only select from the rule's candidates" in text
    assert "research/reports/baseline_report.md" in text
    assert fold_a["bundle_id"] in text

    # -- outputs and RESULT events
    run_dir = synthetic["tmp"] / "runs" / "synthetic_crypto_filter"
    assert save_model_outputs(first, run_dir).exists()
    loaded = ModelBundle.load(run_dir / "bundles" / f"{bundle.bundle_id}.json")
    assert loaded.checksum == bundle.checksum
    recorded = record_model_results(reg, result, digest=digest, now=fx.NOW)
    assert recorded == [("T-0011", a["verdict"]), ("T-0012", "UNTRAINABLE")]
    assert reg.results("T-0012")[-1]["payload"]["summary"]["folds_trained"] == "0/1"
    assert len(reg.results()) == 2
    record_model_results(reg, result, digest=digest, now=fx.NOW)
    assert len(reg.results()) == 2  # the same run is recorded once


@pytest.mark.slow
def test_synthetic_study_refuses_the_holdout_and_a_changed_registration(
    synthetic: dict[str, Any],
) -> None:
    ctx: ModelStudyContext = synthetic["ctx"]
    reg: ExperimentRegistry = synthetic["reg"]
    trial = ctx.config.trials[0]
    with pytest.raises(HoldoutLockedError):
        build_trial_labels(ctx, trial, start=fx.HOLDOUT, end=fx.END)
    with pytest.raises(ModelStudyError, match="development period"):
        build_trial_labels(ctx, trial, start=fx.START - timedelta(days=5), end=fx.START)
    labels = build_trial_labels(ctx, trial, start=fx.START, end=fx.START + timedelta(days=7))
    assert labels.rows and labels.source["registered_trial"] == "T-0011"
    assert max(r.decision_time for r in labels.rows) < fx.START + timedelta(days=7)
    assert labels.feature_version == FEATURE_VERSION

    changed_path = fx.write_model_study(synthetic["root"], fx.model_study_raw(seed=8))
    changed = open_model_study(
        synthetic["root"], load_model_study(changed_path), synthetic["manifest"]
    )
    with pytest.raises(RegistryError, match="differs"):
        run_model_study(changed, reg)
    fx.write_model_study(synthetic["root"])  # restore the registered recipe


@pytest.mark.slow
def test_test_window_bars_never_reach_training_or_selection(
    synthetic: dict[str, Any], tmp_path: Path
) -> None:
    """Replacing every bar from the test start on (the test window and the holdout) with an
    unrelated random walk changes the test-window results but nothing the model learned or
    chose: scaler, coefficients, calibration, threshold, tuning history and validation replays."""
    test_start = datetime(2026, 3, 1, tzinfo=UTC)
    scrambled = registered_study(tmp_path, scramble_from=test_start)
    other_run = run_model_study(scrambled["ctx"], scrambled["reg"])
    other, other_bundles = other_run.result, other_run.bundles
    base = synthetic["run"].result
    for tid in ("T-0011", "T-0012"):
        f0, f1 = base["trials"][tid]["folds"][0], other["trials"][tid]["folds"][0]
        assert f0["labels"]["train_rows"] == f1["labels"]["train_rows"]
        assert f0["labels"]["validation_rows"] == f1["labels"]["validation_rows"]
        assert f0["selection_log"] == f1["selection_log"]
        assert f0["val_metrics"] == f1["val_metrics"]
        # replay hashes differ only through the bundle ID in each veto's binding (the ID covers
        # the label provenance, which names the dataset's whole-file content hash)
        keys = ("threshold", "trades", "net_usd", "filter_decisions", "filter_accepts")
        assert [{k: r[k] for k in keys} for r in f0["threshold_replays"]] == [
            {k: r[k] for k in keys} for r in f1["threshold_replays"]
        ]
        assert f0["status"] == f1["status"] and f0["threshold"] == f1["threshold"]
        assert f0["runs"]["rule_only"]["result_hash"] != f1["runs"]["rule_only"]["result_hash"]
    (b0,) = synthetic["run"].bundles
    (b1,) = other_bundles
    # the models are identical; only the dataset content hash in the provenance differs
    for name in ("preprocessing", "parameters", "calibration", "threshold", "hyperparameters"):
        assert getattr(b0, name) == getattr(b1, name), name
    assert b0.data_hashes["train_rows_sha256"] == b1.data_hashes["train_rows_sha256"]
    assert b0.data_hashes["validation_rows_sha256"] == b1.data_hashes["validation_rows_sha256"]


# --------------------------------------------------------------------------- model gate (review)
def _gate_inputs(
    *, best: str, rule: float, random: float, turnover: float
) -> tuple[dict[str, object], dict[str, object], list[str], list[int], dict[str, object]]:

    wf: dict[str, Any] = {
        "trades": 150,
        "sessions": 200,
        "effective_sessions": 180,
        "mean_session_ci": {"one_sided_lower": 0.01},
        "per_trade_ci": {"one_sided_lower": 0.01},
    }
    row_names = [
        "T-0011/rule_only",
        "T-0011/model_filter",
        "T-0011/random_control",
        "T-0011/turnover_control",
    ]
    rc = {"best_index": row_names.index(f"T-0011/{best}"), "selection_adjusted_lower_bound": 0.02}
    paired = {
        f"model_minus_{n}": {"one_sided_lower": v}
        for n, v in (
            ("rule_only", rule),
            ("random_control", random),
            ("turnover_control", turnover),
        )
    }
    return wf, rc, row_names, [1], {"T-0011": {"paired_differences": paired}}


def test_model_gate_passes_only_when_the_filter_beats_the_rule_and_both_controls() -> None:
    from trader.research.model_study import model_gate

    wf, rc, names, rows, trials = _gate_inputs(
        best="model_filter", rule=0.01, random=0.01, turnover=0.01
    )
    gate = model_gate(wf, rc, row_names=names, model_rows=rows, trials=trials)
    assert gate["status"] == "PASSED" and gate["gated_trial"] == "T-0011"


@pytest.mark.parametrize(
    ("best", "rule", "random", "turnover", "failing"),
    [
        ("rule_only", 0.01, 0.01, 0.01, "best_reality_check_row_is_a_model_filter"),
        ("random_control", 0.01, 0.01, 0.01, "selection_adjusted_lower_bound_positive"),
        ("model_filter", -0.01, 0.01, 0.01, "filter_beats_rule_only_lower_bound_positive"),
        ("model_filter", 0.01, 0.0, 0.01, "filter_beats_random_control_lower_bound_positive"),
        ("model_filter", 0.01, 0.01, -0.5, "filter_beats_turnover_control_lower_bound_positive"),
    ],
)
def test_model_gate_rejects_rule_or_control_driven_results(
    best: str, rule: float, random: float, turnover: float, failing: str
) -> None:
    from trader.research.model_study import model_gate

    wf, rc, names, rows, trials = _gate_inputs(
        best=best, rule=rule, random=random, turnover=turnover
    )
    gate = model_gate(wf, rc, row_names=names, model_rows=rows, trials=trials)
    assert gate["status"] == "NOT_PASSED"
    assert gate["checks"][failing] is False


def test_model_gate_fails_closed_without_controls_or_reality_check() -> None:
    from trader.research.model_study import model_gate

    wf, rc, names, rows, trials = _gate_inputs(
        best="model_filter", rule=0.01, random=0.01, turnover=0.01
    )
    no_controls = {
        "T-0011": {"paired_differences": {"model_minus_rule_only": {"one_sided_lower": 1}}}
    }
    assert model_gate(wf, rc, row_names=names, model_rows=rows, trials=no_controls)["status"] == (
        "NOT_PASSED"
    )
    assert model_gate(wf, None, row_names=names, model_rows=rows, trials=trials)["status"] == (
        "NOT_PASSED"
    )
