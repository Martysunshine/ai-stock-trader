"""The daily ETF study ``etf_daily_v1`` (configs/research/etf_daily_v1.yaml;
strategy_and_profitability.md sections 3-5; risk_and_validation.md sections 5-7).

* The study declares exactly two trials before any data: T-0013 (Candidate C on the 13 equity
  ETFs) and T-0014 (Candidate D on all 16), with the shared daily-bar design's dataset, a warm-up
  long enough for both rules, a diagnostic USD 1000 capital view and honest disclosures.
* A trial may narrow the study universe (``TrialCfg.instruments``, opt-in): its registration and
  runs then cover exactly those instruments; absent, nothing changes.
* The buy-and-hold comparator carries a split through instead of marking it as a loss.
* End to end (SYNTHETIC): the study, with only the dataset and split dates moved to the
  deterministic daily fixture ingested through the daily Alpaca path from a fake transport,
  registers in a temporary registry, runs, and renders a stock-worded report with the capital
  sensitivity section. Nothing here reads or evaluates real market data.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.daily_fixtures import (
    CALENDAR,
    EQUITY_ETFS,
    UNIVERSE,
    DailyFixture,
    daily_history,
    daily_study,
    study_config,
)
from trader.config.policy import load_yaml_strict
from trader.data.daily_bars import source_day_stamp
from trader.data.ingest import IngestContext, ingest
from trader.data.manifest import load_manifest
from trader.data.sources.alpaca_history import AlpacaCredentials
from trader.domain.types import iso_utc
from trader.research.engine import session_dates
from trader.research.registry import ExperimentRegistry
from trader.research.report import render_baseline_report
from trader.research.splits import folds
from trader.research.study import (
    StudyConfig,
    TrialCfg,
    build_registration,
    buy_and_hold,
    deployment_windows,
    load_development_data,
    load_study,
    open_study,
    resolve_rule,
    result_digest,
    run_baseline_study,
    run_trial,
    trial_instruments,
)
from trader.strategies.dip_buy import DipBuySpec
from trader.strategies.trend_timing import TrendTimingSpec

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO / "configs" / "research" / "etf_daily_v1.yaml"
MANIFEST = REPO / "research" / "data_manifest.json"
DATASET = "alpaca_us_etfs_daily_sip_v1"
NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
CREDS = AlpacaCredentials("key-id-test", "secret-test")
CRYPTO_WORDS = re.compile(r"kraken|coinbase|\bbtc\b|\beth\b|crypto|ten-configuration", re.I)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ----------------------------------------------------------------------------- declaration


def test_the_study_declares_two_published_rules_on_the_shared_daily_design() -> None:
    cfg = load_study(STUDY)
    manifest = load_manifest(MANIFEST)
    ds = manifest.dataset(DATASET)
    assert (cfg.study_id, cfg.track.value, cfg.session_basis) == ("etf_daily_v1", "STOCKS", "XNYS")
    assert cfg.price_dataset == DATASET and ds.request.is_daily and ds.feed == "ALPACA_SIP"
    ids = [m.execution_instrument_id for m in cfg.instruments]
    assert ids == list(UNIVERSE) == [i.instrument_id for i in ds.request.instruments]
    assert all(m.price_instrument_id == m.execution_instrument_id for m in cfg.instruments)
    assert cfg.instrument_specs == "configs/instruments/etfs_research.yaml"
    assert cfg.fee_schedule == "configs/fees/alpaca_us_equities_v2.yaml"
    assert cfg.policy == "configs/policy/base.yaml"
    assert (cfg.fills.half_spread_bps, cfg.fills.slippage_bps) == (Decimal("1.0"), Decimal("1.0"))
    assert (cfg.stress.multiplier, cfg.stress.missed_entry_percent) == (Decimal(2), 5)
    assert cfg.annualisation_sessions == 252 and cfg.diagnostic_capitals == (Decimal(1000),)
    assert (cfg.bootstrap.replicates, cfg.bootstrap.mean_block_sessions) == (2000, Decimal(5))

    ctx = open_study(REPO, cfg, manifest)
    assert ctx.fees.known
    assert [t.trial_id for t in cfg.trials] == ["T-0013", "T-0014"]
    c_trial, d_trial = cfg.trials
    c_spec, d_spec = resolve_rule(ctx, c_trial), resolve_rule(ctx, d_trial)
    assert isinstance(c_spec, DipBuySpec) and c_spec.family == "CANDIDATE_C"
    assert isinstance(d_spec, TrendTimingSpec) and d_spec.family == "CANDIDATE_D"
    assert c_trial.overrides == {} and d_trial.overrides == {}  # frozen published parameters
    # T-0013 trades exactly the 13 equity ETFs its rule covers; T-0014 all 16.
    assert c_trial.instruments == EQUITY_ETFS
    assert [i.split(":")[1] for i in EQUITY_ETFS] == list(c_spec.universe_symbols)
    assert d_trial.instruments is None and list(d_spec.universe) == ids
    assert all(len(t.hypothesis) >= 20 for t in cfg.trials)
    # The warm-up covers both rules' lookbacks, and the first window's warm-up is inside the data.
    assert cfg.warmup_sessions is not None
    assert cfg.warmup_sessions >= max(c_spec.min_warmup_sessions, d_spec.warmup_sessions)
    first = CALENDAR.session_open_before(cfg.splits.development_start, cfg.warmup_sessions)
    assert first >= ds.request.start
    # Whole quarters up to the locked final test, which ends where the dataset ends.
    plan = cfg.splits
    assert (plan.holdout_start, plan.holdout_end) == (_utc(2026, 8, 1), ds.request.end)
    windows = deployment_windows(plan)
    assert windows[-1][1] == plan.holdout_start
    assert all(e.month - s.month in (3, -9) for s, e in windows)
    assert len(windows) == 38 and len(folds(plan)) == 33 and plan.embargo_minutes == 0
    # Disclosures the registration must carry.
    text = " ".join((cfg.universe_selection, cfg.horizon, cfg.entry_exit_timing)).lower()
    for phrase in (
        "survivorship",
        "next open",
        "close",
        "iex",
        "sip",
        "disaster stop",
        "not in the published rules",
        "distributions are neither credited nor adjusted",
        "instrument-id order",
        "end_of_data",
    ):
        assert phrase in text, phrase


def test_the_study_cannot_register_before_the_daily_data_is_acquired(tmp_path: Path) -> None:
    # The real dataset is ACQUIRED since 2026-11-09; the guard is checked on a copy of the
    # manifest with the dataset put back into its pre-acquisition state.
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entry = next(d for d in raw["history"] if d["name"] == DATASET)
    entry.update(status="BLOCKED", blocker="owner action: store keys", files=[], retrieved_at=None)
    blocked = tmp_path / "data_manifest.json"
    blocked.write_text(json.dumps(raw), encoding="utf-8")
    manifest = load_manifest(blocked)
    ctx = open_study(REPO, load_study(STUDY), manifest)
    assert manifest.dataset(DATASET).status == "BLOCKED"
    with pytest.raises(ValueError, match="acquire the data before registering"):
        build_registration(ctx, ctx.config.trials[0])


def test_trial_instruments_must_be_distinct_members_of_the_study() -> None:
    raw: dict[str, Any] = load_study(STUDY).model_dump(mode="json")
    trial = dict(raw["trials"][0])
    for bad, message in (
        (["ARCX:SPY", "ARCX:SPY"], "distinct"),
        (["ARCX:SPY", "XNYS:AAPL"], "not in the study"),
        ([], "at least 1"),
    ):
        with pytest.raises(ValidationError, match=message):
            StudyConfig.model_validate({**raw, "trials": [{**trial, "instruments": bad}]})
    cfg = StudyConfig.model_validate(
        {**raw, "trials": [{**trial, "instruments": ["ARCX:GLD", "ARCX:SPY"]}]}
    )
    # The study's order is kept, whatever the order of the trial's list.
    assert [m.execution_instrument_id for m in trial_instruments(cfg, cfg.trials[0])] == [
        "ARCX:SPY",
        "ARCX:GLD",
    ]
    absent = TrialCfg.model_validate({k: v for k, v in trial.items() if k != "instruments"})
    assert absent.instruments is None
    assert trial_instruments(cfg, absent) == cfg.instruments


# ----------------------------------------------------------------------------- comparator


def test_buy_and_hold_carries_a_split_instead_of_marking_a_loss() -> None:
    """``ARCX:XLK`` splits 2-for-1 on 2018-06-04 in the fixture. With the split applied the raw
    series matches the same prices without a split, cent roundings aside; ignoring it would
    mark half the holding (about USD 5) as lost on the ex-date."""
    trial = [TrialCfg(trial_id="T-SYN", spec_file="unused.yaml", hypothesis="comparator only")]
    cfg = study_config(trial, instruments=["ARCX:XLK"])
    start = _utc(2018, 3, 1)
    days = session_dates("XNYS", CALENDAR, start, _utc(2018, 9, 1))
    raw_ctx, raw = daily_study(cfg, daily_history(["ARCX:XLK"], missing=()))
    split_free = daily_history(["ARCX:XLK"], missing=(), splits=())
    plain_ctx, plain = daily_study(cfg, split_free)
    assert raw.setups["ARCX:XLK"].splits and not plain.setups["ARCX:XLK"].splits
    with_split = buy_and_hold(raw_ctx, raw, days, start)
    without = buy_and_hold(plain_ctx, plain, days, start)
    assert max(abs(a - b) for a, b in zip(with_split.equity, without.equity, strict=True)) < 0.05
    assert min(with_split.pnl) > -1.0  # no USD 5 cliff on the ex-date


# ----------------------------------------------------------------------------- end to end


class FixtureAlpaca:
    """Serves the SYNTHETIC fixture as Alpaca daily rows (00:00 New York stamps) and its splits
    as corporate actions; first page the first symbol, second page the rest."""

    def __init__(self, fixture: DailyFixture) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        for iid, bars in fixture.bars.items():
            self.rows[iid.split(":")[1]] = [
                {
                    "t": iso_utc(source_day_stamp(CALENDAR.session_date_of(b.start))),
                    "o": b.open,
                    "h": b.high,
                    "l": b.low,
                    "c": b.close,
                    "v": b.volume,
                    "n": b.trade_count,
                    "vw": b.vwap,
                }
                for b in bars
            ]
        self.splits = [
            {
                "symbol": s.instrument_id.split(":")[1],
                "new_rate": s.new_rate,
                "old_rate": s.old_rate,
                "ex_date": s.ex_date.isoformat(),
            }
            for s in fixture.splits
        ]

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        p = dict(params or {})
        if url.endswith("/v2/stocks/bars"):
            assert (p["timeframe"], p["feed"], p["adjustment"]) == ("1Day", "sip", "raw")
            symbols = p["symbols"].split(",")
            if "page_token" not in p:
                return {"bars": {symbols[0]: self.rows[symbols[0]]}, "next_page_token": "p2"}
            return {"bars": {s: self.rows[s] for s in symbols[1:]}, "next_page_token": None}
        if url.endswith("/v1/corporate-actions"):
            return {"corporate_actions": {"forward_splits": self.splits}, "next_page_token": None}
        raise AssertionError(f"unexpected request {url}")


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(REPO / "configs", root / "configs")
    shutil.copytree(
        REPO / "src" / "trader" / "strategies" / "specs",
        root / "src" / "trader" / "strategies" / "specs",
    )
    return root


def _synthetic_etf_study() -> StudyConfig:
    """``etf_daily_v1`` as declared, with only the split dates moved onto the five-year
    SYNTHETIC fixture (2016-2020) and fewer bootstrap replicates (a pipeline check, not
    evidence). The development start is the declared one: 2017-02-01."""
    raw: dict[str, Any] = load_yaml_strict(STUDY)
    raw["splits"] = {
        **raw["splits"],
        "holdout_start": "2020-08-01T00:00:00Z",
        "holdout_end": "2021-01-01T00:00:00Z",
    }
    raw["bootstrap"] = {**raw["bootstrap"], "replicates": 200}
    return StudyConfig.model_validate(raw)


@pytest.mark.slow
def test_the_etf_study_runs_end_to_end_on_the_synthetic_daily_fixture(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    fixture = daily_history()  # splits, early closes, a 6% gap and one missing XLB session
    manifest = load_manifest(MANIFEST)
    entry = manifest.dataset(DATASET)
    request = entry.request.model_copy(update={"start": _utc(2016, 1, 1), "end": _utc(2021, 1, 1)})
    ds = ingest(
        entry.model_copy(update={"request": request}),
        IngestContext(repo_root=root, transport=FixtureAlpaca(fixture), now=NOW, alpaca=CREDS),
    )
    assert ds.status == "ACQUIRED", ds.blocker
    by_id = {f.instrument_id: f for f in ds.files}
    assert by_id["ARCX:XLB"].quality["missing_sessions"] == ["2018-01-18"]
    manifest = manifest.replace(ds)

    cfg = _synthetic_etf_study()
    ctx = open_study(root, cfg, manifest)
    regs = {t.trial_id: build_registration(ctx, t) for t in cfg.trials}
    c_reg, d_reg = regs["T-0013"], regs["T-0014"]
    assert c_reg.universe == EQUITY_ETFS and len(c_reg.datasets) == 13
    assert d_reg.universe == UNIVERSE and len(d_reg.datasets) == 16
    assert (c_reg.family, d_reg.family) == ("CANDIDATE_C", "CANDIDATE_D")
    for reg in (c_reg, d_reg):
        assert reg.splits["warmup_sessions"] == 260
        assert reg.cost_basis["diagnostic_capitals_usd"] == ["1000"]
        assert reg.feature_version == "daily-v1"
    registry = ExperimentRegistry(tmp_path / "registry.jsonl")
    for reg in regs.values():
        assert registry.register(reg, NOW)

    result = run_baseline_study(ctx, registry)
    assert result["track"] == "STOCKS" and result["warmup_sessions"] == 260
    assert result["fee_schedule"] == "alpaca-us-equities-conservative-2026-11-08"
    trials = result["trials"]
    assert list(trials) == ["T-0013", "T-0014"]
    assert trials["T-0013"]["universe"] == list(EQUITY_ETFS) and "universe" not in trials["T-0014"]
    assert trials["T-0013"]["timing"] == {
        "holding": "OVERNIGHT",
        "max_hold_sessions": "10",
        "target_role": "COST_GATE",
        "entry_limit_offset_bps": "5",
    }
    assert trials["T-0014"]["timing"]["entry_limit_offset_bps"] == "100"
    for tid, trial in trials.items():
        base = trial["views"]["base"]
        assert base["trades"] > 0, tid
        # every round trip pays at least the CAT buy cent and the SEC + TAF + CAT sell cents
        assert base["fees_usd"] >= 0.04 * base["trades"] - 1e-9
        assert trial["views"]["diagnostic"]["fees_usd"] == 0
        assert len(base["window_nets_usd"]) == 14
    # Narrowing T-0013 to the equity ETFs feeds fewer bars but cannot change its trades: the
    # rule ignores bonds and gold by symbol.
    data = load_development_data(ctx, uses_source_vwap=False, unlocked=False)
    c_trial = cfg.trials[0]
    narrowed = run_trial(ctx, c_trial, data, "base")
    whole = run_trial(ctx, c_trial.model_copy(update={"instruments": None}), data, "base")
    assert narrowed.diagnostics["bars"] < whole.diagnostics["bars"]
    assert [t.as_dict() for t in narrowed.trades] == [t.as_dict() for t in whole.trades]
    assert narrowed.trades and {t.instrument_id for t in narrowed.trades} <= set(EQUITY_ETFS)
    exits = set(trials["T-0013"]["views"]["base"]["exit_reasons"])
    assert "SIGNAL:close_above_sma_5" in exits and "TARGET" not in exits
    capital = result["capital_views"]
    assert capital["capitals_usd"] == ["1000"] and set(capital["trials"]) == set(trials)
    assert result["g2"]["status"] == "NOT_PASSED"
    assert len(result["walk_forward"]["folds"]) == 9

    report = render_baseline_report(result, generated_at="test", digest=result_digest(result))
    assert "Evidence class: BACKTEST" in report and "G2 status: NOT_PASSED" in report
    assert not CRYPTO_WORDS.search(report), CRYPTO_WORDS.findall(report)
    section3 = report.split("## 3. Execution and cost assumptions")[1].split("## 4.")[0]
    assert "Entries (daily bars, positions carried overnight)" in section3
    assert "T-0013 5 bps, at most 10 sessions, target is a cost gate only" in section3
    assert "T-0014 100 bps, at most 260 sessions" in section3
    assert "bars of the 260 preceding exchange sessions feed the features only" in section3
    assert "two preceding calendar days" not in report
    assert "T-0013 universe (13 of 16): SPY, QQQ" in report
    assert "T-0014 universe (16 of 16)" in report
    assert "Buy-and-hold SPY+QQQ+IWM+DIA" in report
    assert "## 14. Capital sensitivity (diagnostic only)" in report
    section14 = report.split("## 14. Capital sensitivity (diagnostic only)")[1]
    assert "| T-0013 | 1000 |" in section14 and "| T-0014 | 1000 |" in section14
    section11 = report.split("## 11. Stocks track")[1].split("## 12.")[0]
    assert f"Evaluated on `{DATASET}`" in section11
    assert "T-0013" in section11 and "T-0014" in section11
    section12 = report.split("## 12. Bias and limitations")[1].split("## 13.")[0].lower()
    for phrase in ("survivorship", "distributions are neither credited", "instrument-id order"):
        assert phrase in section12, phrase
