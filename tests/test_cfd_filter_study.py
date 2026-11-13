"""The CFD filter study (T-0026) end to end on SYNTHETIC two-sided hourly data: registration,
training that never reads its test window, the filter that only refuses, the three controls, the
views, walk forward, G2, the practice rule, the report and determinism. No test reads ``data/`` or
``runs/`` or the real registry, and none runs the shipped T-0026 study."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from trader.data.history import HistoryBar
from trader.data.ingest import IngestContext, ingest
from trader.data.manifest import DataManifest, HistoryDataset
from trader.data.sources.capital_prices import MAX_BARS, QuotePage
from trader.features.cfd_breakout import BreakoutFeatureBook
from trader.research.cfd_filter import SignalFeatures
from trader.research.cfd_filter_report import render_cfd_filter_report
from trader.research.cfd_filter_study import (
    CfdFilterStudyContext,
    CfdFilterStudyError,
    build_cfd_filter_registration,
    load_cfd_filter_study,
    open_cfd_filter_study,
    run_cfd_filter_study,
    train_for_fold,
)
from trader.research.cfd_study import (
    _engine,
    build_cfd_registration,
    load_cfd_data,
    view_costs,
)
from trader.research.registry import ExperimentRegistry, RegistryError
from trader.research.splits import folds
from trader.research.study import result_digest
from trader.research.training import to_datetime64

from .cfd_filter_fixtures import EPICS, hourly_walk

ROOT = Path(__file__).resolve().parents[1]
FIRST = datetime(2024, 11, 20, tzinfo=UTC)
LAST = datetime(2025, 8, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 11, tzinfo=UTC)


class Quotes:
    """The synthetic walks as the Capital.com price source would page them."""

    def __init__(self, scramble_from: datetime | None = None) -> None:
        self.rows: dict[str, list[tuple[HistoryBar, HistoryBar]]] = {}
        for k, epic in enumerate(EPICS):
            rows = hourly_walk(epic, FIRST, LAST, seed=11 + k)
            if scramble_from is not None:
                keep = [r for r in rows if r[0].start < scramble_from]
                rest = hourly_walk(epic, scramble_from, LAST, seed=99 + k)
                rows = keep + rest
            self.rows[epic] = rows

    def fetch_quote_bars(
        self, epic: str, *, minutes: int, max_bars: int = MAX_BARS, to: datetime | None = None
    ) -> QuotePage:
        held = [r for r in self.rows[epic] if to is None or r[0].start <= to][-max_bars:]
        return QuotePage(bid=tuple(r[0] for r in held), ask=tuple(r[1] for r in held))


def workspace(root: Path, *, scramble_from: datetime | None = None) -> CfdFilterStudyContext:
    for rel in (
        "configs/instruments/capital_demo_cfds_v1.yaml",
        "configs/fees/capital_com_cfd_demo_v1.yaml",
        "configs/policy/crypto_cfd_sleeve_v1.yaml",
        "src/trader/strategies/specs/cfd_crypto_hourly_breakout_v1.yaml",
        "src/trader/strategies/specs/cfd_crypto_hourly_fade_v1.yaml",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, root / rel)
    request = {
        "instruments": [
            {"instrument_id": f"CAPITAL:{e}{side}", "source_symbol": e}
            for e in EPICS
            for side in (".BID", ".ASK")
        ],
        "bar_minutes": 60,
        "start": FIRST,
        "end": LAST,
        "calendar": "UTC_24_7",
    }
    ds = HistoryDataset.model_validate(
        {
            "name": "synthetic_crypto_cfd_1h_v1",
            "kind": "HISTORICAL_BARS",
            "source": "capital_demo_prices",
            "venue": "CAPITAL_COM_DEMO",
            "feed": "CAPITAL_DEMO_BID_ASK",
            "entitlement": "synthetic",
            "request": request,
            "path": "data/history/synthetic_crypto_cfd_1h_v1",
            "availability_rule": "synthetic",
            "corrections": "synthetic",
            "adjustment_basis": "RAW",
            "status": "REQUESTED",
        }
    )
    acquired = ingest(ds, IngestContext(root, None, NOW, capital=Quotes(scramble_from)))  # type: ignore[arg-type]
    manifest = DataManifest(
        manifest_version=2, prepared="2026-11-11", note="test", history=(acquired,)
    )
    base = yaml.safe_load(
        (ROOT / "configs/research/cfd_crypto_hourly_channel_v1.yaml").read_text("utf-8")
    )
    base.update(study_id="synthetic_crypto_hourly", quote_dataset="synthetic_crypto_cfd_1h_v1")
    base["splits"].update(
        development_start="2025-01-01T00:00:00Z",
        holdout_start="2025-07-01T00:00:00Z",
        holdout_end="2025-08-01T00:00:00Z",
    )
    base["bootstrap"]["replicates"] = 200
    (root / "configs/research").mkdir(parents=True, exist_ok=True)
    (root / "configs/research/synthetic_crypto_hourly.yaml").write_text(
        yaml.safe_dump(base), encoding="utf-8"
    )
    study = {
        "kind": "cfd_filter_study",
        "study_id": "synthetic_crypto_breakout_filter",
        "title": "Synthetic filter study",
        "description": "A veto filter on the synthetic hourly breakout, for tests only.",
        "base_study": "configs/research/synthetic_crypto_hourly.yaml",
        "base_trial": "T-0023",
        "walk_forward": {
            "train_months": 2,
            "validation_months": 1,
            "test_months": 1,
            "step_months": 1,
        },
        "embargo_minutes": 21 * 60,
        "known_before_registration": "Synthetic data; nothing is known.",
        "trials": [
            {
                "trial_id": "T-0026",
                "model_type": "logistic",
                "grid": [
                    {"C": "0.1", "class_weight": "none"},
                    {"C": "1", "class_weight": "balanced"},
                ],
                "challenger": {
                    "model_type": "lightgbm",
                    "grid": [
                        {
                            "num_leaves": "3",
                            "min_child_samples": "20",
                            "learning_rate": "0.05",
                            "n_estimators": "30",
                        }
                    ],
                },
                "calibration": "platt",
                "threshold_grid": ["0.30", "0.36", "0.42", "0.48"],
                "min_accepts": 10,
                "controls": {"random_seeds": 3, "equal_turnover": "smallest_break_first"},
                "seed": 20261126,
                "hypothesis": "A veto filter beats the synthetic rule and its controls.",
            }
        ],
    }
    path = root / "configs/research/synthetic_crypto_breakout_filter.yaml"
    path.write_text(yaml.safe_dump(study), encoding="utf-8")
    return open_cfd_filter_study(root, load_cfd_filter_study(path), manifest)


def register(ctx: CfdFilterStudyContext, path: Path) -> ExperimentRegistry:
    registry = ExperimentRegistry(path)
    registry.register(build_cfd_registration(ctx.base, ctx.base_trial), NOW)
    for trial in ctx.config.trials:
        registry.register(build_cfd_filter_registration(ctx, trial), NOW)
    return registry


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("cfd_filter")
    ctx = workspace(root)
    registry = register(ctx, root / "registry.jsonl")
    run = run_cfd_filter_study(ctx, registry)
    return {"root": root, "ctx": ctx, "registry": registry, "run": run}


# ----------------------------------------------------------------------------- registration


def test_the_shipped_draft_names_t0023_and_fixes_every_parameter() -> None:
    """Only the YAML is read: no test opens the draft's data or runs it."""
    cfg = load_cfd_filter_study(ROOT / "configs/research/cfd_crypto_breakout_filter_v1.yaml")
    assert cfg.kind == "cfd_filter_study" and cfg.study_id == "cfd_crypto_breakout_filter_v1"
    assert cfg.base_study == "configs/research/cfd_crypto_hourly_channel_v1.yaml"
    assert cfg.base_trial == "T-0023" and cfg.embargo_minutes == 21 * 60
    wf = cfg.walk_forward
    assert (wf.train_months, wf.validation_months, wf.test_months, wf.step_months) == (6, 3, 3, 3)
    (trial,) = cfg.trials
    assert trial.trial_id == "T-0026" and trial.model_type == "logistic"
    assert trial.challenger is not None and trial.challenger.model_type == "lightgbm"
    assert len(trial.grid) == 6 and len(trial.challenger.grid) == 4
    grid = trial.threshold_grid
    assert (min(grid), max(grid), len(grid)) == (Decimal("0.30"), Decimal("0.54"), 9)
    assert trial.calibration == "platt" and trial.min_accepts == 30
    assert trial.controls.random_seeds == 20 and trial.seed == 20261126


def test_the_command_line_runs_only_registered_filter_studies(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from trader.cli.main import app

    runner = CliRunner()
    draft = str(ROOT / "configs/research/cfd_crypto_breakout_filter_v1.yaml")
    empty = str(tmp_path / "registry.jsonl")
    # refused at the registry check, before any bar is read
    refused = runner.invoke(app, ["research", "cfd-filter", "--study", draft, "--registry", empty])
    assert refused.exit_code == 2 and "not registered" in refused.output
    base = str(ROOT / "configs/research/cfd_crypto_hourly_channel_v1.yaml")
    wrong_kind = runner.invoke(
        app, ["research", "cfd-filter", "--study", base, "--registry", empty]
    )
    assert wrong_kind.exit_code == 2 and "not a CFD filter study" in wrong_kind.output
    baseline = runner.invoke(app, ["research", "baseline", "--study", draft, "--registry", empty])
    assert baseline.exit_code == 2 and "is a CFD filter study" in baseline.output
    assert not (tmp_path / "registry.jsonl").exists()


def test_a_filter_trial_binds_its_base_trial_and_recipe(env: dict[str, Any]) -> None:
    ctx: CfdFilterStudyContext = env["ctx"]
    reg = build_cfd_filter_registration(ctx, ctx.config.trials[0])
    base = build_cfd_registration(ctx.base, ctx.base_trial)
    assert reg.trial_id == "T-0026" and reg.study_id == "synthetic_crypto_breakout_filter"
    assert reg.family == "CFD_HOURLY_CHANNEL+LOGIT_FILTER/LGBM_FILTER"
    assert reg.rule["base_trial"] == "T-0023"
    assert reg.rule["base_registration_hash"] == base.content_hash
    assert reg.rule["spec_hash"] == base.rule["spec_hash"]
    assert reg.datasets == base.datasets and reg.feature_version == "cfd-breakout-v1"
    assert reg.parameters["filter.challenger"] == "lightgbm"
    assert [m["role"] for m in reg.rule["filter"]["models"]] == ["primary", "challenger"]
    assert build_cfd_filter_registration(ctx, ctx.config.trials[0]).content_hash == (
        reg.content_hash
    )


def test_a_filter_study_refuses_to_run_unregistered_or_on_a_changed_base(
    env: dict[str, Any], tmp_path: Path
) -> None:
    ctx: CfdFilterStudyContext = env["ctx"]
    with pytest.raises(RegistryError, match="not registered"):
        run_cfd_filter_study(ctx, ExperimentRegistry(tmp_path / "empty.jsonl"))
    only_filter = ExperimentRegistry(tmp_path / "only_filter.jsonl")
    only_filter.register(build_cfd_filter_registration(ctx, ctx.config.trials[0]), NOW)
    with pytest.raises(RegistryError, match="T-0023 is not registered"):
        run_cfd_filter_study(ctx, only_filter)


def test_a_filter_study_refuses_a_short_embargo_or_another_rule(env: dict[str, Any]) -> None:
    ctx: CfdFilterStudyContext = env["ctx"]
    root = env["root"]
    manifest = DataManifest(
        manifest_version=2, prepared="2026-11-11", note="test", history=(ctx.base.dataset,)
    )
    short = ctx.config.model_copy(update={"embargo_minutes": 60})
    with pytest.raises(CfdFilterStudyError, match="shorter than the rule's longest hold"):
        open_cfd_filter_study(root, short, manifest)
    missing = ctx.config.model_copy(update={"base_trial": "T-0099"})
    with pytest.raises(CfdFilterStudyError, match="no trial T-0099"):
        open_cfd_filter_study(root, missing, manifest)


# ----------------------------------------------------------------------------- the run


def test_the_study_runs_end_to_end_on_synthetic_data(env: dict[str, Any]) -> None:
    result = env["run"].result
    assert result["kind"] == "cfd_filter_study" and result["base_trial"] == "T-0023"
    trial = result["trials"]["T-0026"]
    assert len(trial["folds"]) == 3 and trial["folds_trained"] >= 1
    assert [f["test"][0][:10] for f in trial["folds"]] == ["2025-04-01", "2025-05-01", "2025-06-01"]
    for variant in ("rule_only", "filter", "random_control", "turnover_control"):
        assert set(trial["variants"][variant]["views"]) == {"base", "stressed", "diagnostic"}
    rule = trial["variants"]["rule_only"]["views"]
    assert rule["base"]["trades"] > 20
    assert rule["stressed"]["net_pnl_usd"] < rule["base"]["net_pnl_usd"]
    assert rule["base"]["net_pnl_usd"] < rule["diagnostic"]["net_pnl_usd"]
    assert set(trial["comparison"]["checks"]) == {
        f"{v}_beats_{c}"
        for v in ("base", "stressed")
        for c in ("rule_only", "random_control", "turnover_control")
    }
    assert set(trial["promotion"]["checks"]) == {
        "base_net_positive",
        "stressed_net_positive",
        "walk_forward_net_positive",
    }
    assert len(result["walk_forward"]["folds"]) == 3
    assert {f["selected"] for f in result["walk_forward"]["folds"]} <= {"T-0026", "CASH"}
    assert result["g2"]["status"] in ("PASSED", "NOT_PASSED")
    assert "filter_beats_turnover_control_lower_bound_positive" in result["g2"]["checks"]
    # nothing from the locked final test was read
    for f in trial["folds"]:
        assert f["test"][1] <= "2025-07-01T00:00:00Z"
    report = render_cfd_filter_report(result, generated_at="2026-11-11 00:00 UTC", digest="0" * 64)
    assert "## Verdict" in report and "## The filter against its controls" in report
    assert "## Folds" in report and "## Limits" in report and "T-0026" in report
    # every bundle on record belongs to a trained fold, with its checksum
    trained = [f for f in trial["folds"] if f["status"] == "TRAINED"]
    assert [b["checksum"] for b in result["bundles"]] == [f["bundle_checksum"] for f in trained]


def test_the_rule_alone_is_the_base_studys_own_window(env: dict[str, Any]) -> None:
    ctx: CfdFilterStudyContext = env["ctx"]
    trial = env["run"].result["trials"]["T-0026"]
    data = load_cfd_data(ctx.base, end=ctx.plan.holdout_start)
    for f, fold in zip(trial["folds"], folds(ctx.plan), strict=True):
        for view in ("base", "stressed", "diagnostic"):
            own = _engine(ctx.base, data, "T-0023", view_costs(ctx.base.config, view), *fold.test)
            assert f["runs"][view]["rule_only"]["result_hash"] == own.result_hash


def test_the_filter_only_refuses_and_the_controls_match_it(env: dict[str, Any]) -> None:
    trial = env["run"].result["trials"]["T-0026"]
    for f in trial["folds"]:
        for view in ("base", "stressed", "diagnostic"):
            runs = f["runs"][view]
            flt = runs["filter"]
            # the filter is asked about every signal the rule alone saw, never trades more
            # than were offered, and its decisions do not depend on the cost view
            assert int(flt["filter_decisions"]) == int(
                f["runs"]["base"]["filter"]["filter_decisions"]
            )
            refused = int(flt["filter_refusals"])
            assert refused == int(f["runs"]["base"]["filter"]["filter_refusals"])
            # random refusal at the filter's own rate, one salt per seed
            assert runs["random_control"]["rate"] == flt["refusal_rate"]
            assert len(runs["random_control"]["salts"]) == 3
            # equal turnover: exactly as many refusals, the smallest breaks
            turnover = runs["turnover_control"]
            assert int(turnover["filter_refusals"]) == refused == len(turnover["refused"])
            assert int(turnover["filter_decisions"]) == int(flt["filter_decisions"])
            if f["status"] != "TRAINED":
                assert refused == int(flt["filter_decisions"]) and flt["trades"] == "0"
        decisions = int(f["runs"]["base"]["filter"]["filter_decisions"])
        refusals = int(f["runs"]["base"]["filter"]["filter_refusals"])
        rate = Decimal(f["runs"]["base"]["filter"]["refusal_rate"])
        assert rate == (
            Decimal(refusals) / Decimal(decisions) if decisions else Decimal(0)
        ).quantize(Decimal("0.000001"))


def test_the_same_inputs_give_the_same_bundles_and_results(
    env: dict[str, Any], tmp_path: Path
) -> None:
    ctx: CfdFilterStudyContext = env["ctx"]
    first = env["run"]
    again = run_cfd_filter_study(ctx, env["registry"])
    assert result_digest(again.result) == result_digest(first.result)
    assert [b.checksum for b in again.bundles] == [b.checksum for b in first.bundles]


def test_training_reads_no_bar_of_its_test_window(tmp_path: Path) -> None:
    """Replace every bar from the first fold's test start with an unrelated walk: that fold's
    labels, bundle and threshold do not change."""
    clean = workspace(tmp_path / "clean")
    fold = folds(clean.plan)[0]
    scrambled = workspace(tmp_path / "scrambled", scramble_from=fold.test[0])
    out = []
    for ctx in (clean, scrambled):
        data = load_cfd_data(ctx.base, end=ctx.plan.holdout_start)
        book = BreakoutFeatureBook(data.daily, channel_hours=24, atr_hours=24)
        fitted = train_for_fold(
            ctx, ctx.config.trials[0], data, SignalFeatures(book), fold, source={}
        )
        assert (fitted.train.exit_time < to_datetime64(fold.train[1])).all()
        assert (fitted.val.exit_time <= to_datetime64(fold.test[0])).all()
        out.append(fitted)
    a, b = out
    assert a.train.digest() == b.train.digest() and a.val.digest() == b.val.digest()
    assert (a.trained is None) == (b.trained is None)
    if a.trained is not None and b.trained is not None:
        assert a.trained.bundle.checksum == b.trained.bundle.checksum
    # the scrambled test window is really different
    da = load_cfd_data(clean.base, end=clean.plan.holdout_start)
    db = load_cfd_data(scrambled.base, end=scrambled.plan.holdout_start)
    iid = "CAPITAL:BTCUSD"
    after = [b for b in da.daily[iid] if b.start >= fold.test[0]][:5]
    other = [b for b in db.daily[iid] if b.start >= fold.test[0]][:5]
    assert [b.mid_close for b in after] != [b.mid_close for b in other]
