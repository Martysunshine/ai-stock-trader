"""Stock baseline study preparation (implementation_roadmap.md M2; strategy_and_profitability.md
sections 3-5; risk_and_validation.md sections 3 and 5).

* The documented Alpaca fee schedule charges the verified pass-through fees, rounded up per order.
* ``stocks_baseline_v1`` declares the registered grid, cost basis, splits and disclosures.
* A study with an unknown fee schedule fails before registration and before a run.
* The report is track-aware: the crypto report re-renders byte for byte, a stock report carries
  no crypto wording and describes the stock results.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from trader.accounting.fees import load_fee_schedule
from trader.data.ingest import IngestContext, ingest
from trader.data.manifest import load_manifest
from trader.domain.types import EvidenceLevel, Side, Track
from trader.research.registry import ExperimentRegistry
from trader.research.report import G2_CHECK_ORDER, render_baseline_report, wording_for
from trader.research.splits import folds
from trader.research.study import (
    StudyConfig,
    UnknownCostBasisError,
    _g2,
    build_registration,
    load_study,
    open_study,
    resolve_rule,
    result_digest,
    run_baseline_study,
)
from trader.strategies.candidate_a import CandidateASpec

REPO = Path(__file__).resolve().parents[1]
FEES_V2 = REPO / "configs" / "fees" / "alpaca_us_equities_v2.yaml"
STOCKS = REPO / "configs" / "research" / "stocks_baseline_v1.yaml"
CRYPTO = REPO / "configs" / "research" / "crypto_baseline_v1.yaml"
SYMBOLS = ("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA")
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
CRYPTO_WORDS = re.compile(r"kraken|coinbase|\bbtc\b|\beth\b|crypto|ten-configuration", re.I)


# ----------------------------------------------------------------------------- fee schedule


def test_alpaca_v2_schedule_is_documented_from_dated_official_sources() -> None:
    fees = load_fee_schedule(FEES_V2)
    assert fees.known and fees.config.evidence is EvidenceLevel.DOCUMENTED
    assert fees.currency == "USD"
    official = ("sec.gov", "finra.org", "alpaca.markets", "catnmsplan.com")
    sources = fees.config.sources
    assert len(sources) >= 10
    for s in sources:
        host = s.url.split("/")[2]
        assert any(host == d or host.endswith("." + d) for d in official), s.url
        assert s.retrieved == "2026-11-08" and s.states
    hosts = {s.url.split("/")[2] for s in sources}
    assert {"www.sec.gov", "www.finra.org", "files.alpaca.markets"} <= hosts
    # the superseded record stays unknown (never zero fees)
    assert not load_fee_schedule(REPO / "configs" / "fees" / "alpaca_us_equities.yaml").known


def test_alpaca_v2_charges_the_highest_rates_rounded_up_per_order() -> None:
    fees = load_fee_schedule(FEES_V2)
    # tiny fractional sell: SEC, TAF and CAT each round up to one cent
    assert fees.fee(Side.SELL, Decimal("0.001"), Decimal("0.15")) == Decimal("0.03")
    # about USD 50 (0.25 share at USD 200): still one cent per fee type
    assert fees.fee(Side.SELL, Decimal("0.25"), Decimal("50.00")) == Decimal("0.03")
    # 100,000 shares at USD 50: SEC 27.80/million on 5,000,000 = 139.00; TAF 19.50 capped at
    # 9.79; CAT 0.000048 x 100,000 = 4.80
    big = fees.fee(Side.SELL, Decimal("100000"), Decimal("5000000"))
    assert big == Decimal("139.00") + Decimal("9.79") + Decimal("4.80")
    # buys pay only the CAT fee; zero commission
    assert fees.fee(Side.BUY, Decimal("0.25"), Decimal("50.00")) == Decimal("0.01")
    assert fees.fee(Side.BUY, Decimal("100000"), Decimal("5000000")) == Decimal("4.80")
    assert fees.round_trip(Decimal("0.05"), Decimal("10"), Decimal("10")) == Decimal("0.04")


@given(
    q1=st.decimals("0.000001", "200000", places=6), q2=st.decimals("0.000001", "200000", places=6)
)
@settings(max_examples=150, deadline=None)
def test_alpaca_v2_fees_are_monotone_in_quantity(q1: Decimal, q2: Decimal) -> None:
    lo, hi = sorted((q1, q2))
    fees = load_fee_schedule(FEES_V2)
    for side in Side:
        assert fees.fee(side, lo, lo * 100) <= fees.fee(side, hi, hi * 100)


# ----------------------------------------------------------------------------- study config


def test_stocks_study_declares_the_registered_grid_cost_basis_and_splits() -> None:
    cfg = load_study(STOCKS)
    assert cfg.study_id == "stocks_baseline_v1" and cfg.track is Track.STOCKS
    assert cfg.price_dataset == "alpaca_us_equities_5m_v1" and cfg.basis_datasets == ()
    assert [(m.price_instrument_id, m.execution_instrument_id) for m in cfg.instruments] == [
        (f"XNYS:{s}", f"XNAS:{s}") for s in SYMBOLS
    ]
    assert cfg.fee_schedule == "configs/fees/alpaca_us_equities_v2.yaml"
    assert (cfg.fills.half_spread_bps, cfg.fills.slippage_bps) == (Decimal("2.0"), Decimal("2.0"))
    assert (cfg.stress.multiplier, cfg.stress.missed_entry_percent) == (Decimal(2), 5)
    assert cfg.loss_windows == ("NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK")
    assert (cfg.session_basis, cfg.annualisation_sessions) == ("XNYS", 252)
    # Earliest Alpaca IEX 5-minute bars: 2020-07-27 (probed 2026-11-08); first whole month used.
    assert cfg.splits.development_start == datetime(2020, 8, 1, tzinfo=UTC)
    assert cfg.splits.holdout_start == datetime(2026, 8, 1, tzinfo=UTC)
    assert cfg.splits.holdout_end == datetime(2026, 11, 1, tzinfo=UTC)
    assert cfg.splits.embargo_minutes == 60
    assert len(folds(cfg.splits)) == 19  # 12/3/3, step 3, inside 2020-08 .. 2026-08
    manifest = json.loads((REPO / "research" / "data_manifest.json").read_text(encoding="utf-8"))
    request = next(d for d in manifest["history"] if d["name"] == cfg.price_dataset)["request"]
    assert request["start"] == "2020-08-01T00:00:00Z"  # must equal development_start
    assert cfg.operating_costs_usd_per_month == 0
    assert [t.trial_id for t in cfg.trials] == [f"T-{n:04d}" for n in range(6, 11)]
    grid = {
        (t.overrides["atr_stop_multiplier"], t.overrides["reward_risk_multiple"])
        for t in cfg.trials[:4]
    }
    assert grid == {("1.0", "1.5"), ("1.0", "2.0"), ("1.5", "1.5"), ("1.5", "2.0")}
    assert {t.spec_file for t in cfg.trials[:4]} == {
        "src/trader/strategies/specs/candidate_a_v1.yaml"
    }
    assert cfg.trials[4].spec_file == "src/trader/strategies/specs/candidate_b_stocks_v1.yaml"
    assert cfg.trials[4].overrides == {}
    disclosed = " ".join((cfg.universe_selection, cfg.horizon, cfg.entry_exit_timing)).lower()
    for phrase in ("survivorship", "point-in-time", "earnings", "iex", "fractional", "unverified"):
        assert phrase in disclosed, phrase


def test_stocks_study_opens_against_the_manifest_but_cannot_register_before_data() -> None:
    manifest = load_manifest(REPO / "research" / "data_manifest.json")
    ctx = open_study(REPO, load_study(STOCKS), manifest)
    assert ctx.fees.known and ctx.fees.schedule_id == "alpaca-us-equities-conservative-2026-11-08"
    ds = manifest.dataset("alpaca_us_equities_5m_v1")
    assert {i.instrument_id for i in ds.request.instruments} == {f"XNYS:{s}" for s in SYMBOLS}
    specs = [resolve_rule(ctx, t) for t in ctx.config.trials]
    assert all(isinstance(s, CandidateASpec) for s in specs[:4])
    assert len({s.spec_hash for s in specs}) == 5  # every trial is a distinct configuration
    if ds.status != "ACQUIRED":
        with pytest.raises(ValueError, match="acquire the data"):
            build_registration(ctx, ctx.config.trials[0])


def test_crypto_registrations_are_unchanged_by_the_new_fields() -> None:
    """The committed crypto trials still match their registry entries (read-only check)."""
    ctx = open_study(
        REPO, load_study(CRYPTO), load_manifest(REPO / "research" / "data_manifest.json")
    )
    registry = ExperimentRegistry(REPO / "research" / "experiment_registry.jsonl")
    for trial in ctx.config.trials:
        registry.require_registered(build_registration(ctx, trial))


# ----------------------------------------------------------------------------- fail closed


def test_unknown_fee_schedule_fails_before_registration_and_before_a_run(tmp_path: Path) -> None:
    manifest = load_manifest(REPO / "research" / "data_manifest.json")
    cfg = load_study(STOCKS).model_copy(
        update={"fee_schedule": "configs/fees/alpaca_us_equities.yaml"}
    )
    ctx = open_study(REPO, cfg, manifest)
    with pytest.raises(UnknownCostBasisError, match="alpaca-us-equities-unverified"):
        build_registration(ctx, cfg.trials[0])
    registry = ExperimentRegistry(tmp_path / "registry.jsonl")
    with pytest.raises(UnknownCostBasisError, match="is unknown"):
        run_baseline_study(ctx, registry)
    assert not (tmp_path / "registry.jsonl").exists()


def test_cli_refuses_an_unknown_cost_basis_and_defaults_report_paths(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from trader.cli.main import REPO_ROOT, app, default_report_path

    raw = yaml.safe_load(STOCKS.read_text(encoding="utf-8"))
    raw["study_id"] = "stocks_unknown_fees_check"
    raw["fee_schedule"] = "configs/fees/alpaca_us_equities.yaml"
    study = tmp_path / "study.yaml"
    study.write_text(yaml.safe_dump(raw), encoding="utf-8")
    registry = tmp_path / "registry.jsonl"
    runner = CliRunner()
    for command in ("register", "baseline"):
        out = runner.invoke(
            app, ["research", command, "--study", str(study), "--registry", str(registry)]
        )
        assert out.exit_code == 2, out.output
        assert "alpaca-us-equities-unverified" in out.output and "unknown" in out.output
    assert not registry.exists()
    assert not (REPO_ROOT / "runs" / "research" / "stocks_unknown_fees_check").exists()
    reports = REPO_ROOT / "research" / "reports"
    assert default_report_path("crypto_baseline_v1") == reports / "baseline_report.md"
    assert default_report_path("stocks_baseline_v1") == reports / "stocks_baseline_v1_report.md"


# ----------------------------------------------------------------------------- report


def test_g2_table_order_matches_the_gate_definition() -> None:
    assert tuple(_g2({}, None)["checks"]) == G2_CHECK_ORDER


def test_report_wording_follows_the_track() -> None:
    assert "Kraken" in wording_for({"track": "CRYPTO"}).fees_line
    assert wording_for({}) == wording_for({"track": "CRYPTO"})  # pre-track crypto results
    with pytest.raises(ValueError, match="COMMODITIES"):
        wording_for({"track": "COMMODITIES"})


def test_crypto_report_re_renders_byte_for_byte_from_its_results() -> None:
    """``research/reports/baseline_report.md`` is reproduced exactly from the run's results.json
    (gitignored; present in a checkout that ran or restored the crypto study)."""
    results = REPO / "runs" / "research" / "crypto_baseline_v1" / "results.json"
    if not results.exists():
        pytest.skip("runs/research/crypto_baseline_v1/results.json not present in this checkout")
    result = json.loads(results.read_text(encoding="utf-8"))
    committed = (REPO / "research" / "reports" / "baseline_report.md").read_text(encoding="utf-8")
    generated = re.search(r"^- Generated: (.+?); result digest `([0-9a-f]{16})`$", committed, re.M)
    stock = re.search(r"^- Dataset `([^`]+)`: \*\*([A-Z_]+)\*\* - (.+)$", committed, re.M)
    assert generated and stock
    digest = result_digest(result)
    assert digest[:16] == generated.group(2)
    rendered = render_baseline_report(
        result,
        generated_at=generated.group(1),
        digest=digest,
        stock_status={
            "dataset": stock.group(1),
            "status": stock.group(2),
            "detail": stock.group(3),
        },
    )
    assert rendered == committed


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(REPO / "configs", root / "configs")
    shutil.copytree(
        REPO / "src" / "trader" / "strategies" / "specs",
        root / "src" / "trader" / "strategies" / "specs",
    )
    return root


def _synthetic_stocks_study() -> StudyConfig:
    """``stocks_baseline_v1`` as registered, with only the data, universe and split dates moved to
    the short synthetic equities history (SYNTHETIC data: a pipeline check, not evidence)."""
    cfg = load_study(STOCKS)
    raw: dict[str, Any] = cfg.model_dump(mode="json")
    raw.update(
        {
            "price_dataset": "synthetic_equities_5m_v1",
            "instruments": [
                {"price_instrument_id": "XNYS:SYNA", "execution_instrument_id": "XNYS:SYNA"},
                {"price_instrument_id": "XNYS:SYNB", "execution_instrument_id": "XNYS:SYNB"},
            ],
            "instrument_specs": "configs/instruments/synthetic_equities.yaml",
            "splits": {  # one November window: DST end, split, rename, holiday, early close
                "development_start": "2026-11-01T00:00:00Z",
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
            "bootstrap": {"replicates": 200, "mean_block_sessions": "5", "seed": 20261107},
        }
    )
    return StudyConfig.model_validate(raw)


@pytest.mark.slow
def test_stocks_study_runs_end_to_end_on_synthetic_bars_and_reports_as_stocks(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    manifest = load_manifest(REPO / "research" / "data_manifest.json")
    ds = ingest(
        manifest.dataset("synthetic_equities_5m_v1"),
        IngestContext(repo_root=root, transport=None, now=NOW),
    )  # type: ignore[arg-type]
    manifest = manifest.replace(ds)
    cfg = _synthetic_stocks_study()
    ctx = open_study(root, cfg, manifest)
    registry = ExperimentRegistry(tmp_path / "registry.jsonl")
    for trial in cfg.trials:
        assert registry.register(build_registration(ctx, trial), NOW)
    result = run_baseline_study(ctx, registry)
    assert result["track"] == "STOCKS"
    assert result["fee_schedule"] == "alpaca-us-equities-conservative-2026-11-08"
    assert list(result["trials"]) == [f"T-{n:04d}" for n in range(6, 11)]
    assert [t["family"] for t in result["trials"].values()] == ["CANDIDATE_A"] * 4 + ["CANDIDATE_B"]
    for trial in result["trials"].values():
        base = trial["views"]["base"]
        # every round trip pays at least the CAT buy cent and the SEC + TAF + CAT sell cents
        assert base["fees_usd"] >= 0.04 * base["trades"] - 1e-9
        assert trial["views"]["diagnostic"]["fees_usd"] == 0
    assert result["g2"]["status"] == "NOT_PASSED"

    report = render_baseline_report(result, generated_at="test", digest=result_digest(result))
    assert "Evidence class: BACKTEST" in report and "G2 status: NOT_PASSED" in report
    assert not CRYPTO_WORDS.search(report), CRYPTO_WORDS.findall(report)
    assert "Buy-and-hold SYNA+SYNB (policy-capped)" in report
    assert "per NY session" in report
    section11 = report.split("## 11. Stocks track")[1].split("## 12.")[0]
    assert "Evaluated on `synthetic_equities_5m_v1`" in section11
    assert "Candidate A (trend continuation), 4 registered variants: T-0006" in section11
    assert "Candidate B (mean reversion), 1 registered variant: T-0010" in section11
    assert "alpaca-us-equities-conservative-2026-11-08" in section11
    assert "neither has been evaluated" not in report
    section12 = report.split("## 12. Bias and limitations")[1].split("## 13.")[0]
    assert "survivorship" in section12 and "earnings" in section12
