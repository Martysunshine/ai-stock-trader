"""CFD studies end to end on synthetic two-sided data (ADR-0052): ingest, registration, the
windowed runs in three cost views, walk forward, the gate, the practice rule and the report. No
test touches the network or the real registry."""

from __future__ import annotations

import math
import shutil
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from trader.data.history import HistoryBar
from trader.data.ingest import IngestContext, ingest
from trader.data.manifest import DataManifest, HistoryDataset
from trader.data.sources.capital_prices import MAX_BARS, QuotePage
from trader.research.cfd_report import render_cfd_report
from trader.research.cfd_study import (
    CfdStudyError,
    build_cfd_registration,
    load_cfd_study,
    open_cfd_study,
    promotion,
    run_cfd_study,
    verdict,
)
from trader.research.registry import ExperimentRegistry, RegistryError
from trader.research.study import result_digest

ROOT = Path(__file__).resolve().parents[1]
FIRST = datetime(2020, 1, 1, tzinfo=UTC)
LAST = datetime(2023, 4, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 11, tzinfo=UTC)
EPICS = {"US500": Decimal(3000), "US100": Decimal(9000)}


def _mid(epic: str, d: int) -> Decimal:
    """A rising market with sharp two-day dips every few weeks."""
    base = float(EPICS[epic]) * (1 + 0.0005 * d) + 40 * math.sin(d / 2.5)
    if d % 23 in (21, 22):
        base -= 60
    return Decimal(str(round(base, 1)))


class HourlyQuotes:
    """Hourly bid/ask on weekdays and the Sunday-evening hour, half a point either side."""

    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[HistoryBar, HistoryBar]]] = {}
        for epic in EPICS:
            rows = []
            day = FIRST
            n = 0
            while day < LAST:
                open_mid, close_mid = _mid(epic, n - 1), _mid(epic, n)
                hours = (
                    range(24)
                    if day.weekday() < 5
                    else (range(23, 24) if day.weekday() == 6 else ())
                )
                for h in hours:
                    start = day + timedelta(hours=h)
                    frac = Decimal(h + 1) / 24
                    o = open_mid + (close_mid - open_mid) * (frac - Decimal(1) / 24)
                    c = open_mid + (close_mid - open_mid) * frac
                    o, c = o.quantize(Decimal("0.1")), c.quantize(Decimal("0.1"))
                    hi, lo = max(o, c) + 2, min(o, c) - 2
                    pair = []
                    for off in (Decimal("-0.5"), Decimal("0.5")):
                        pair.append(
                            HistoryBar(start, 60, o + off, hi + off, lo + off, c + off, Decimal(5))
                        )
                    rows.append((pair[0], pair[1]))
                if day.weekday() < 5:
                    n += 1
                day += timedelta(days=1)
            self.rows[epic] = rows

    def fetch_quote_bars(
        self, epic: str, *, minutes: int, max_bars: int = MAX_BARS, to: datetime | None = None
    ) -> QuotePage:
        held = [r for r in self.rows[epic] if to is None or r[0].start <= to][-max_bars:]
        return QuotePage(bid=tuple(r[0] for r in held), ask=tuple(r[1] for r in held))


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("cfd_study")
    for rel in (
        "configs/instruments/capital_demo_cfds_v1.yaml",
        "configs/fees/capital_com_cfd_demo_v1.yaml",
        "configs/policy/cfd_sleeve_v1.yaml",
        "src/trader/strategies/specs/cfd_index_dip_v1.yaml",
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
        "calendar": "BROKER_CFD",
    }
    ds = HistoryDataset.model_validate(
        {
            "name": "synthetic_cfd_1h_v1",
            "kind": "HISTORICAL_BARS",
            "source": "capital_demo_prices",
            "venue": "CAPITAL_COM_DEMO",
            "feed": "CAPITAL_DEMO_BID_ASK",
            "entitlement": "synthetic",
            "request": request,
            "path": "data/history/synthetic_cfd_1h_v1",
            "availability_rule": "synthetic",
            "corrections": "synthetic",
            "adjustment_basis": "RAW",
            "status": "REQUESTED",
        }
    )
    acquired = ingest(ds, IngestContext(root, None, NOW, capital=HourlyQuotes()))  # type: ignore[arg-type]
    manifest = DataManifest(
        manifest_version=2, prepared="2026-11-11", note="test", history=(acquired,)
    )
    study = yaml.safe_load((ROOT / "configs/research/cfd_index_dip_v1.yaml").read_text("utf-8"))
    study.update(
        study_id="synthetic_cfd_dip",
        quote_dataset="synthetic_cfd_1h_v1",
        instruments=[{"instrument_id": "CAPITAL:US500"}, {"instrument_id": "CAPITAL:US100"}],
        warmup_days=400,
    )
    study["costs"]["spread_floor_bps"] = {"CAPITAL:US500": "0.8", "CAPITAL:US100": "0.6"}
    study["splits"].update(
        development_start="2021-01-01T00:00:00Z",
        holdout_start="2023-01-01T00:00:00Z",
        holdout_end="2023-04-01T00:00:00Z",
    )
    study["bootstrap"]["replicates"] = 200
    path = root / "configs/research/synthetic_cfd_dip.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(study), encoding="utf-8")
    ctx = open_cfd_study(root, load_cfd_study(path), manifest)
    registry = ExperimentRegistry(root / "registry.jsonl")
    return {
        "root": root,
        "ctx": ctx,
        "registry": registry,
        "dataset": acquired,
        "manifest": manifest,
    }


def test_a_trial_binds_its_quotes_specs_costs_and_splits(env: dict[str, Any]) -> None:
    ctx = env["ctx"]
    reg = build_cfd_registration(ctx, ctx.config.trials[0])
    assert reg.trial_id == "T-0018" and reg.family == "CFD_INDEX_DIP"
    assert reg.universe == ("CAPITAL:US500", "CAPITAL:US100")
    sides = [d["instrument_id"] for d in reg.datasets]
    assert sides == [
        "CAPITAL:US500.BID",
        "CAPITAL:US500.ASK",
        "CAPITAL:US100.BID",
        "CAPITAL:US100.ASK",
    ]
    assert {d["content_sha256"] for d in reg.datasets} == {
        f.content_sha256 for f in env["dataset"].files
    }
    basis = reg.cost_basis
    assert basis["financing"]["CAPITAL:US500"]["long_daily_charge_fraction"] == "0.000222069"
    assert basis["policy_file"] == "configs/policy/cfd_sleeve_v1.yaml"
    assert basis["costs"]["spread_floor_bps"]["CAPITAL:US100"] == "0.6"
    assert reg.splits["warmup_days"] == "400" and reg.splits["bars"]["calendar"] == "BROKER_CFD"
    assert reg.parameters["rsi_entry_below"] == "10"
    assert build_cfd_registration(ctx, ctx.config.trials[0]).content_hash == reg.content_hash


def test_a_study_refuses_to_run_before_registration_and_registers_once(env: dict[str, Any]) -> None:
    fresh = ExperimentRegistry(env["root"] / "unregistered.jsonl")
    with pytest.raises(RegistryError, match="not registered"):
        run_cfd_study(env["ctx"], fresh)
    reg = build_cfd_registration(env["ctx"], env["ctx"].config.trials[0])
    assert env["registry"].register(reg, NOW) is True
    assert env["registry"].register(reg, NOW) is False  # identical: a no-op


def test_the_study_runs_three_views_walk_forward_and_the_gate(env: dict[str, Any]) -> None:
    registry = env["registry"]
    if not registry.registrations():
        registry.register(build_cfd_registration(env["ctx"], env["ctx"].config.trials[0]), NOW)
    result = run_cfd_study(env["ctx"], registry)
    trial = result["trials"]["T-0018"]
    base, stressed, mid = (trial["views"][v] for v in ("base", "stressed", "diagnostic"))
    assert base["trades"] > 10, "the synthetic dips should be traded"
    assert stressed["net_pnl_usd"] < base["net_pnl_usd"] < mid["net_pnl_usd"]
    assert mid["financing_usd"] == 0 and base["financing_usd"] > 0
    assert mid["execution_cost_usd"] == 0 < base["execution_cost_usd"]
    assert base["by_direction"]["short"]["trades"] == 0  # the index dip is long only
    assert len(base["window_nets_usd"]) == 8 and base["windows"][0] == "2021-01-01T00:00:00Z"
    assert set(base["exit_reasons"]) <= {
        "SIGNAL:close_above_sma_5",
        "STOP",
        "STOP_GAP",
        "TIME_EXIT",
        "END_OF_DATA",
    }
    assert trial["views"]["continuous"]["trades"] > 0
    assert result["capital_usd"] == 100.0 and result["session_calendar"] == "BROKER_CFD"
    # nothing from the locked final test is read
    days = [d for v in result["data"]["first_bar"].values() for d in [v]]
    assert all(d < "2021-01-01" for d in days)
    wf = result["walk_forward"]
    assert len(wf["folds"]) == 3 and wf["summary"]["sessions"] > 0
    assert result["g2"]["status"] in ("PASSED", "NOT_PASSED")
    assert set(result["g2"]["checks"]) >= {
        "oos_trades_at_least_100",
        "selection_adjusted_lower_bound_positive",
    }
    assert result["comparators"]["buy_and_hold"]["sessions"] == wf["summary"]["sessions"]
    p = promotion(result, "T-0018")
    assert set(p["checks"]) == {
        "base_net_positive",
        "stressed_net_positive",
        "walk_forward_net_positive",
    }
    assert verdict(result, "T-0018") in ("POSITIVE_UNCORRECTED", "NEGATIVE_NET")
    # the same inputs give the same result
    again = run_cfd_study(env["ctx"], registry)
    assert result_digest(again) == result_digest(result)
    report = render_cfd_report(
        result, generated_at="2026-11-11 00:00 UTC", digest=result_digest(result)
    )
    assert "## Verdict" in report and "T-0018" in report and "Long and short" in report
    assert "## Limits" in report


def test_a_study_on_the_wrong_data_is_refused(env: dict[str, Any]) -> None:
    config = env["ctx"].config
    wrong = config.model_copy(update={"quote_dataset": "nope"})
    with pytest.raises(KeyError):
        open_cfd_study(env["root"], wrong, env["manifest"])
    floors = config.costs.model_copy(update={"spread_floor_bps": {"CAPITAL:US500": Decimal(1)}})
    with pytest.raises(CfdStudyError, match="no registered spread floor"):
        open_cfd_study(env["root"], config.model_copy(update={"costs": floors}), env["manifest"])


def test_the_command_line_runs_only_registered_cfd_studies(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from trader.cli.main import app

    runner = CliRunner()
    study = str(ROOT / "configs/research/cfd_index_dip_v1.yaml")
    empty = str(tmp_path / "registry.jsonl")
    refused = runner.invoke(app, ["research", "cfd", "--study", study, "--registry", empty])
    assert refused.exit_code == 2 and "not registered" in refused.output
    wrong_kind = runner.invoke(app, ["research", "baseline", "--study", study, "--registry", empty])
    assert wrong_kind.exit_code == 2 and "is a CFD study" in wrong_kind.output
    not_cfd = str(ROOT / "configs/research/etf_dip_sized_v1.yaml")
    assert runner.invoke(app, ["research", "cfd", "--study", not_cfd]).exit_code == 2


def test_the_shipped_studies_are_the_trials_of_the_decision_record() -> None:
    dip = load_cfd_study(ROOT / "configs/research/cfd_index_dip_v1.yaml")
    momentum = load_cfd_study(ROOT / "configs/research/cfd_crypto_momentum_v1.yaml")
    assert [t.trial_id for t in dip.trials] == ["T-0018"]
    assert [t.trial_id for t in momentum.trials] == ["T-0019"]
    assert [i.instrument_id for i in dip.instruments] == [
        "CAPITAL:US500",
        "CAPITAL:US100",
        "CAPITAL:US30",
        "CAPITAL:RTY",
    ]
    assert dip.splits.development_start == datetime(2019, 1, 1, tzinfo=UTC)
    assert momentum.splits.development_start == datetime(2018, 3, 1, tzinfo=UTC)
    for study in (dip, momentum):
        assert study.splits.holdout_start == datetime(2026, 8, 1, tzinfo=UTC)
        assert study.costs.slippage_bps == 1 and study.stress.missed_entry_percent == 5
    assert date(2026, 11, 1) == momentum.splits.holdout_end.date()
