"""Feature parity and rule behaviour (instructions.md §4 "features ... parity checks", §6 task 4).

* Streaming features equal a direct batch recomputation over the same window.
* The research path reproduces the runtime replay's Candidate A candidates exactly.
* Candidate B and the crypto Candidate A follow their frozen definitions.
* Calendar handling across DST, the early close, missing data, a split and a symbol change on
  the deterministic synthetic history.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.helpers import harness, session
from trader.accounting.fees import load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import load_policy
from trader.data.calendars import NEW_YORK, ExchangeCalendar, Utc247Calendar
from trader.data.corporate_actions import SplitEvent, point_in_time_adjusted, symbol_on
from trader.data.history import HistoryBar, content_sha256, to_domain_bars
from trader.data.manifest import load_manifest
from trader.data.market_view import MarketView
from trader.data.synthetic_history import RENAME, SPLIT, generate
from trader.domain.market import Bar, Quote
from trader.domain.types import Track
from trader.features.streaming import FeatureWindows, RollingBarFeatures
from trader.research.adapters import SessionWindowAdapter
from trader.research.engine import EngineConfig, FillAssumptions, InstrumentSetup, ResearchEngine
from trader.storage.db import Database
from trader.storage.schema import candidates
from trader.strategies.candidate_a import CandidateA, load_candidate_a_spec
from trader.strategies.research_rules import MeanReversion, build_rule, load_rule_spec

REPO = Path(__file__).resolve().parents[1]
SPECS = REPO / "src" / "trader" / "strategies" / "specs"
T0 = datetime(2026, 3, 2, tzinfo=UTC)


def mk(
    i: int, o: float, h: float, low: float, c: float, v: float = 10.0, iid: str = "KRAKEN:BTC/USD"
) -> Bar:
    start = T0 + timedelta(minutes=15 * i)
    q = Decimal("0.01")
    return Bar(
        instrument_id=iid,
        feed="T",
        minutes=15,
        start=start,
        end=start + timedelta(minutes=15),
        available_time=start + timedelta(minutes=15, seconds=1),
        complete=True,
        open=Decimal(str(o)).quantize(q),
        high=Decimal(str(max(h, o, c))).quantize(q),
        low=Decimal(str(min(low, o, c))).quantize(q),
        close=Decimal(str(c)).quantize(q),
        volume=Decimal(str(v)).quantize(Decimal("0.0001")),
        trade_count=1,
        vwap=Decimal(str(c)).quantize(q),
    )


WINDOWS = FeatureWindows(
    minutes=15,
    reference_window_bars=96,
    use_source_vwap=False,
    atr_period=14,
    long_atr_period=96,
    efficiency_window=16,
    breakout_lookback=6,
    context_minutes=60,
    context_lookback=3,
)


def batch(bars: list[Bar]) -> dict[str, Decimal]:
    """Direct recomputation from the definitions, without running sums."""
    w = WINDOWS
    ref = bars[-(w.reference_window_bars or len(bars)) :]
    num = sum(((b.high + b.low + b.close) * b.volume for b in ref), Decimal(0))
    den = sum((b.volume * 3 for b in ref), Decimal(0))

    def tr(i: int) -> Decimal:
        b, p = bars[i], bars[i - 1]
        return max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))

    n = len(bars)
    atr = sum((tr(i) for i in range(n - w.atr_period, n)), Decimal(0)) / w.atr_period
    long_atr = sum((tr(i) for i in range(n - w.long_atr_period, n)), Decimal(0)) / w.long_atr_period
    closes = [b.close for b in bars[-w.efficiency_window - 1 :]]
    path = sum((abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))), Decimal(0))
    er = abs(closes[-1] - closes[0]) / path if path else Decimal(0)
    hourly = [b.close for b in bars if int(b.end.timestamp()) // 60 % 60 == 0]
    return {
        "reference": num / den,
        "atr": atr,
        "long_atr": long_atr,
        "efficiency_ratio": er,
        "prior_high": max(b.high for b in bars[-w.breakout_lookback - 1 : -1]),
        "context_return": hourly[-1] / hourly[-1 - w.context_lookback] - 1,
    }


def test_streaming_features_equal_batch_recomputation_and_reset_on_gaps() -> None:
    rng = random.Random(7)
    price = 100.0
    bars = []
    for i in range(400):
        o = price
        c = o * (1 + rng.gauss(0, 0.003))
        bars.append(mk(i, o, max(o, c) * 1.001, min(o, c) * 0.999, c, rng.uniform(1, 50)))
        price = c
    feats = RollingBarFeatures(WINDOWS)
    for i, b in enumerate(bars):
        feats.update(b)
        if i >= 150 and i % 37 == 0:
            snap = feats.snapshot()
            expected = batch(bars[: i + 1])
            for key, value in expected.items():
                assert snap[key] == value, key
    assert feats.ready
    gap = mk(402, price, price + 1, price - 1, price)  # bars 400 and 401 are missing
    feats.update(gap)
    assert feats.gaps == 1 and not feats.ready  # fail closed: warm-up starts again


def _runtime_candidates(db: Database) -> dict[str, str]:
    h = harness(db)
    h.run_to_end()
    with h.env.db.tx() as conn:
        rows = conn.execute(select(candidates)).all()
    return {r.candidate_id: json.loads(r.payload_json)["snapshot_hash"] for r in rows}


def test_research_path_reproduces_runtime_candidate_a_exactly(sqlite_db: Database) -> None:
    runtime = _runtime_candidates(sqlite_db)
    assert runtime, "the synthetic session must produce candidates"
    sess = session()
    market = MarketView(feed="SYNTHETIC", decision_minutes=5)
    cal = ExchangeCalendar("XNYS")
    adapter = SessionWindowAdapter(CandidateA(load_candidate_a_spec(), cal), cal)
    research: dict[str, str] = {}
    for event in sess.events:
        market.on_event(event)
        if isinstance(event, Quote):
            for b in market.finalize(event.receipt_time, event.instrument_id):
                cand = adapter.on_bar(b, event.receipt_time)
                if cand is not None:
                    research[cand.candidate_id] = cand.snapshot_hash
    assert research == runtime


# ----------------------------------------------------------------------------- rule families


def _choppy(n: int) -> list[Bar]:
    bars = []
    for i in range(n):
        c = 100.2 if i % 2 else 99.8
        o = 99.8 if i % 2 else 100.2
        bars.append(mk(i, o, max(o, c) + 0.1, min(o, c) - 0.1, c))
    return bars


def _run_rule(spec_file: str, bars: list[Bar]) -> object:
    rule = build_rule(load_rule_spec(SPECS / spec_file), Utc247Calendar())
    out = None
    for b in bars:
        out = rule.on_bar(b, b.available_time)
    return out, rule


def test_mean_reversion_needs_a_quiet_regime_a_deep_excursion_and_a_stabilising_bar() -> None:
    base = _choppy(130)
    drop = mk(130, 99.8, 99.9, 97.9, 98.0)
    stabilise = mk(131, 98.0, 98.3, 97.95, 98.2)
    cand, rule = _run_rule("candidate_b_crypto_v1.yaml", [*base, drop, stabilise])
    assert isinstance(rule, MeanReversion)
    assert cand is not None
    state = rule.state("KRAKEN:BTC/USD")
    assert cand.target_price == (state.reference_price() / Decimal("0.01")).to_integral_value(
        ROUND_FLOOR
    ) * Decimal("0.01")
    assert cand.stop_price < Decimal("98.2") < cand.target_price
    # no stabilising bar: the same excursion is not enough
    cand2, _ = _run_rule(
        "candidate_b_crypto_v1.yaml", [*base, drop, mk(131, 98.0, 98.1, 97.8, 97.9)]
    )
    assert cand2 is None


def test_mean_reversion_refuses_a_trending_collapse() -> None:
    bars = _choppy(110)
    price = 100.0
    for i in range(110, 130):  # a steady slide: high efficiency ratio
        bars.append(mk(i, price, price + 0.05, price - 0.25, price - 0.2))
        price -= 0.2
    bars.append(mk(130, price, price + 0.15, price - 0.05, price + 0.1))  # stabilising bar
    cand, rule = _run_rule("candidate_b_crypto_v1.yaml", bars)
    state = rule.state("KRAKEN:BTC/USD")  # type: ignore[attr-defined]
    assert (state.last.close - state.reference_price()) / state.atr() <= -2  # deep enough...
    assert state.efficiency_ratio() > Decimal("0.30")  # ...but trending
    assert cand is None


def test_crypto_trend_breakout_needs_a_breakout_above_the_reference_with_positive_context() -> None:
    bars = _choppy(120)
    price = 100.2
    for i in range(120, 132):
        bars.append(mk(i, price, price + 0.15, price - 0.05, price + 0.1))
        price += 0.1
    bars.append(mk(132, price, price + 0.8, price - 0.05, price + 0.7))
    cand, _ = _run_rule("candidate_a_crypto_v1.yaml", bars)
    assert cand is not None
    assert cand.stop_price < cand.reference_price < cand.target_price
    falling = _choppy(120)
    price = 100.0
    for i in range(120, 132):
        falling.append(mk(i, price, price + 0.05, price - 0.15, price - 0.1))
        price -= 0.1
    falling.append(mk(132, price, price + 0.3, price - 0.05, price + 0.25))
    cand2, _ = _run_rule("candidate_a_crypto_v1.yaml", falling)
    assert cand2 is None


# ----------------------------------------------------------------------------- synthetic equities


@pytest.fixture(scope="module")
def synthetic() -> dict[str, list[HistoryBar]]:
    return generate(datetime(2026, 10, 26, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))


def test_synthetic_history_is_deterministic_and_matches_the_manifest(
    synthetic: dict[str, list[HistoryBar]],
) -> None:
    ds = load_manifest(REPO / "research" / "data_manifest.json").dataset("synthetic_equities_5m_v1")
    for iid, bars in synthetic.items():
        assert content_sha256(bars) == ds.file_for(iid).content_sha256
    assert {a["type"] for a in ds.corporate_actions} == {"SPLIT", "SYMBOL_CHANGE"}


def test_point_in_time_split_adjustment_never_uses_a_future_split(
    synthetic: dict[str, list[HistoryBar]],
) -> None:
    cal = ExchangeCalendar("XNYS")
    bars = synthetic["XNYS:SYNB"]
    before = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)
    after = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
    assert point_in_time_adjusted(bars, [SPLIT], as_of=before, calendar=cal) == bars
    adjusted = point_in_time_adjusted(bars, [SPLIT], as_of=after, calendar=cal)
    friday_last = [b for b in bars if b.start.date().isoformat() == "2026-11-13"][-1]
    monday_first = next(b for b in bars if b.start.date().isoformat() == "2026-11-16")
    raw_jump = monday_first.open / friday_last.close
    adj_friday = next(b for b in adjusted if b.start == friday_last.start)
    assert raw_jump < Decimal("0.6")  # the raw series halves at the split
    assert Decimal("0.97") < monday_first.open / adj_friday.close < Decimal("1.03")
    assert (
        friday_last.close == next(b for b in bars if b.start == friday_last.start).close
    )  # raw kept
    assert symbol_on([RENAME], "XNYS:SYNB", date(2026, 11, 19), "SYNB") == "SYNB"
    assert symbol_on([RENAME], "XNYS:SYNB", date(2026, 11, 20), "SYNB") == "SYNC"


def test_stock_calendar_handling_across_dst_early_close_gaps_and_splits(
    synthetic: dict[str, list[HistoryBar]],
) -> None:
    cal = ExchangeCalendar("XNYS")
    specs = load_instruments(REPO / "configs" / "instruments" / "synthetic_equities.yaml")
    setups = {
        iid: InstrumentSetup(
            spec=specs[iid],
            bars=to_domain_bars(
                bars,
                instrument_id=iid,
                feed="SYNTHETIC",
                finalization_delay=timedelta(seconds=1),
                require_source_vwap=True,
            ),
            splits=(SPLIT,) if iid == SPLIT.instrument_id else (),
        )
        for iid, bars in synthetic.items()
    }
    for spec_name in ("candidate_a_v1", "candidate_b_stocks_v1"):
        if spec_name == "candidate_a_v1":
            strategy: object = SessionWindowAdapter(CandidateA(load_candidate_a_spec(), cal), cal)
        else:
            strategy = build_rule(load_rule_spec(SPECS / f"{spec_name}.yaml"), cal)
        engine = ResearchEngine(
            EngineConfig(
                policy=load_policy(REPO / "configs" / "policy" / "base.yaml").policy,
                fees=load_fee_schedule(REPO / "configs" / "fees" / "replay_demo.yaml"),
                fills=FillAssumptions("base", Decimal("1"), Decimal("2"), Decimal("0.10")),
                track=Track.STOCKS,
                calendar=cal,
                start=datetime(2026, 10, 26, tzinfo=UTC),
                end=datetime(2026, 12, 1, tzinfo=UTC),
                loss_windows=("NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"),
                session_basis="XNYS",
            ),
            strategy,  # type: ignore[arg-type]
            setups,
        )
        result = engine.run()
        assert result.diagnostics["splits_applied"] == 1
        assert result.candidates, spec_name
        for c in result.candidates:
            sess = cal.session_at(c.decision_time - timedelta(seconds=2))
            assert sess is not None
            start, end = cal.entry_window(sess)
            assert start <= c.decision_time < end  # DST-correct, early-close-correct window
        for t in result.trades:
            sess = cal.session_at(t.entry.time)
            assert sess is not None
            # flat by the calendar-relative close procedure (15:45, or 12:45 on the early close)
            assert t.exits[-1].time <= cal.close_procedure_start(sess) + timedelta(minutes=5)
            assert t.entry.time.astimezone(NEW_YORK).date() != date(2026, 11, 26)
        # the SYNB -> SYNC rename keeps one instrument identity throughout
        assert {t.instrument_id for t in result.trades} <= {"XNYS:SYNA", "XNYS:SYNB"}


def test_session_adapter_flags_non_contiguous_windows(
    synthetic: dict[str, list[HistoryBar]],
) -> None:
    cal = ExchangeCalendar("XNYS")
    adapter = SessionWindowAdapter(CandidateA(load_candidate_a_spec(), cal), cal)
    bars = to_domain_bars(
        [b for b in synthetic["XNYS:SYNA"] if b.start.date().isoformat() == "2026-11-10"],
        instrument_id="XNYS:SYNA",
        feed="SYNTHETIC",
        finalization_delay=timedelta(seconds=1),
        require_source_vwap=True,
    )
    states = []
    for b in bars:
        adapter.on_bar(b, b.available_time)
        states.append(adapter.data_ok("XNYS:SYNA"))
    assert states[0] and not states[-1]  # contiguous before the 15:00-15:30 gap, not after
    assert SplitEvent("XNYS:SYNB", SPLIT.ex_date, Decimal(2), Decimal(1)).ratio == 2
