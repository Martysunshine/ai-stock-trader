"""Labelled candidate dataset (docs/ml_research_guide.md §4; risk_and_validation.md §5).

* Every in-window candidate is kept, including those rejected or unfilled even when simulated
  alone, with the snapshot behind its hash turned into candidate features.
* A label equals the research engine's outcome for the same trade simulated alone.
* No lookahead: truncating future bars changes no earlier feature or resolved label.
* The locked final test period is refused; Parquet storage round-trips exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from tests.test_research_engine import (
    BTC,
    ETH,
    FEES,
    FILLS,
    KRAKEN,
    PLAN,
    POLICY,
    T0,
    Scripted,
    at,
    flat,
    mk,
    random_walk,
)
from tests.test_research_engine import run as scripted_engine_run
from trader.accounting.fees import load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.data.history import to_domain_bars
from trader.data.synthetic_history import generate
from trader.domain.candidates import TradeCandidate
from trader.domain.market import Bar
from trader.domain.types import Track
from trader.features.candidate_features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    FeatureError,
    FeatureVersionError,
    snapshot_hash,
)
from trader.research.adapters import SessionWindowAdapter
from trader.research.engine import EngineConfig, InstrumentSetup, ResearchEngine
from trader.research.labels import NET_QUANTUM, LabelError, LabelRow, LabelSet, build_labels
from trader.research.splits import HoldoutLockedError, SplitPlan
from trader.strategies.candidate_a import CandidateA, load_candidate_a_spec
from trader.strategies.research_rules import (
    MeanReversion,
    TrendBreakout,
    build_rule,
    load_rule_spec,
)

REPO = Path(__file__).resolve().parents[1]
SPECS = REPO / "src" / "trader" / "strategies" / "specs"
OPEN_PLAN = SplitPlan(
    development_start=datetime(2026, 1, 1, tzinfo=UTC),
    holdout_start=datetime(2027, 1, 1, tzinfo=UTC),
    holdout_end=datetime(2027, 7, 1, tzinfo=UTC),
    embargo_minutes=360,
)


def crypto_config(start: datetime = T0, end: datetime = T0 + timedelta(days=120)) -> EngineConfig:
    return EngineConfig(
        policy=POLICY,
        fees=FEES,
        fills=FILLS,
        track=Track.CRYPTO,
        calendar=Utc247Calendar(),
        start=start,
        end=end,
        loss_windows=("UTC_DAY", "UTC_WEEK"),
        session_basis="UTC_DAY",
    )


def setups_of(bars: list[Bar]) -> dict[str, InstrumentSetup]:
    return {
        iid: InstrumentSetup(spec=KRAKEN[iid], bars=[b for b in bars if b.instrument_id == iid])
        for iid in sorted({b.instrument_id for b in bars})
    }


def mean_reversion(setups: dict[str, InstrumentSetup]) -> TrendBreakout | MeanReversion:
    ticks = {iid: s.spec.price_tick for iid, s in setups.items()}
    return build_rule(load_rule_spec(SPECS / "candidate_b_crypto_v1.yaml"), Utc247Calendar(), ticks)


def label_walk(bars: list[Bar]) -> LabelSet:
    setups = setups_of(bars)
    return build_labels(
        mean_reversion(setups),
        family="CANDIDATE_B",
        config=crypto_config(),
        instruments=setups,
        plan=OPEN_PLAN,
        source={"dataset": "random-walk-test"},
    )


@pytest.fixture(scope="module")
def walk() -> list[Bar]:
    return random_walk(BTC, 50000, 4000, 1) + random_walk(ETH, 2500, 4000, 2)


@pytest.fixture(scope="module")
def labels(walk: list[Bar]) -> LabelSet:
    return label_walk(walk)


# ----------------------------------------------------------------------------- scripted trades


class SnapshotScripted(Scripted):
    """The engine-test scripted strategy, with a streaming-shaped snapshot behind each hash."""

    snapshot_version = "streaming-v1"

    def __init__(self, plans: dict[tuple[str, datetime], tuple[str, str, int, int]]) -> None:
        super().__init__(plans)
        self.last: dict[str, dict[str, Decimal]] = {}

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        cand = super().on_bar(bar, now)
        if cand is None:
            return None
        snap = {
            "close": bar.close,
            "reference": bar.close - 10,
            "atr": Decimal(100),
            "long_atr": Decimal(120),
            "efficiency_ratio": Decimal("0.2"),
            "prior_high": bar.high - 1,
            "context_return": Decimal("0.001"),
        }
        self.last[bar.instrument_id] = snap
        return cand.model_copy(update={"snapshot_hash": snapshot_hash(snap, "streaming-v1")})

    def features(self, instrument_id: str) -> dict[str, Decimal]:
        return dict(self.last[instrument_id])


class StaleSnapshotScripted(SnapshotScripted):
    def data_ok(self, instrument_id: str) -> bool:
        return False


def scripted_labels(
    bars: list[Bar],
    plans: dict[tuple[str, datetime], tuple[str, str, int, int]],
    strategy: SnapshotScripted | None = None,
) -> LabelSet:
    return build_labels(
        strategy or SnapshotScripted(plans),
        family="CANDIDATE_A",
        config=crypto_config(end=T0 + timedelta(days=30)),
        instruments=setups_of(bars),
        plan=OPEN_PLAN,
    )


def test_each_label_equals_the_engine_outcome_of_its_trade_alone() -> None:
    bars = [flat(BTC, i) for i in range(4)]
    bars.append(mk(BTC, at(4), "50000", "51500", "49990", "51000"))  # through the first target
    bars += [flat(BTC, i, "51000") for i in range(5, 12)]
    bars.append(mk(BTC, at(12), "51000", "51010", "49000", "49100"))  # through the second stop
    bars += [flat(BTC, i, "49100") for i in range(13, 20)]
    first = (BTC, at(1))
    second = (BTC, at(6))
    plans = {first: PLAN, second: ("50500", "52500", 360, 120)}
    portfolio = scripted_engine_run(bars, plans)
    labels = scripted_labels(bars, plans)
    assert len(labels.rows) == 2
    for key, row in zip((first, second), labels.rows, strict=True):
        alone = scripted_engine_run(bars, {key: plans[key]})
        outcome = alone.candidates[0]
        trade = alone.trades[0]
        assert row.candidate_id == outcome.candidate_id
        assert row.status == outcome.status == "FILLED"
        assert row.net_usd == trade.net_pnl().quantize(NET_QUANTUM) == trade.net_pnl()
        assert row.exit_time == trade.exits[-1].time
        assert row.net_return == float(row.net_usd / outcome.notional_usd)
        assert row.win == (trade.net_pnl() > 0)
    assert [r.win for r in labels.rows] == [True, False]  # target, then stop
    assert len(portfolio.trades) == 2  # the scripted trades do not overlap in time
    # an overlapping second candidate is refused in the portfolio but still labelled alone
    overlapping = {first: PLAN, (BTC, at(2)): PLAN}
    assert scripted_engine_run(bars, overlapping).candidates[1].reasons == ("RISK_LIMIT",)
    assert [r.status for r in scripted_labels(bars, overlapping).rows] == ["FILLED", "FILLED"]


def test_candidates_rejected_or_unfilled_even_alone_are_kept_with_their_status() -> None:
    bars = [flat(BTC, 0), flat(BTC, 1), mk(BTC, at(2), "50100", "50200", "50090", "50150")]
    bars += [flat(BTC, i) for i in range(3, 6)]
    plans = {(BTC, at(2)): PLAN, (BTC, at(4)): ("49500", "50010", 360, 120)}
    rows = scripted_labels(bars, plans).rows
    assert [(r.status, r.net_usd, r.exit_time, r.win) for r in rows] == [
        ("UNFILLED", Decimal(0), None, False),  # next open beyond the limit
        ("REJECTED", Decimal(0), None, False),  # target below the entry bound
    ]
    stale = scripted_labels(bars, plans, StaleSnapshotScripted(plans)).rows
    assert [r.status for r in stale] == ["REJECTED", "REJECTED"]
    assert all(r.net_return == 0.0 and tuple(r.features) == FEATURE_NAMES for r in stale)


def test_the_locked_final_test_period_is_refused() -> None:
    bars = [flat(BTC, i) for i in range(6)]
    plans = {(BTC, at(1)): PLAN}
    locked = SplitPlan(
        development_start=datetime(2026, 1, 1, tzinfo=UTC),
        holdout_start=datetime(2026, 3, 1, tzinfo=UTC),
        holdout_end=datetime(2026, 6, 1, tzinfo=UTC),
        embargo_minutes=360,
    )

    def attempt(unlocked: bool) -> LabelSet:
        return build_labels(
            SnapshotScripted(plans),
            family="CANDIDATE_A",
            config=crypto_config(end=T0 + timedelta(days=30)),
            instruments=setups_of(bars),
            plan=locked,
            unlocked=unlocked,
        )

    with pytest.raises(HoldoutLockedError):
        attempt(unlocked=False)
    assert len(attempt(unlocked=True).rows) == 1  # only after the registry records an unlock
    # a window before the holdout is still refused when the supplied bars reach into it
    late = SplitPlan(
        development_start=datetime(2026, 1, 1, tzinfo=UTC),
        holdout_start=at(3),
        holdout_end=datetime(2026, 6, 1, tzinfo=UTC),
        embargo_minutes=360,
    )
    with pytest.raises(HoldoutLockedError):
        build_labels(
            SnapshotScripted(plans),
            family="CANDIDATE_A",
            config=crypto_config(end=at(2)),
            instruments=setups_of(bars),
            plan=late,
        )
    with pytest.raises(LabelError, match="set by the label builder"):
        build_labels(
            SnapshotScripted(plans),
            family="CANDIDATE_A",
            config=crypto_config(end=T0 + timedelta(days=30)),
            instruments=setups_of(bars),
            plan=OPEN_PLAN,
            source={"spec_hash": "forged"},
        )


def test_a_snapshot_that_does_not_reproduce_the_candidate_hash_is_refused() -> None:
    class Forged(SnapshotScripted):
        def features(self, instrument_id: str) -> dict[str, Decimal]:
            return {**self.last[instrument_id], "atr": Decimal(99)}

    bars = [flat(BTC, i) for i in range(6)]
    plans = {(BTC, at(1)): PLAN}
    with pytest.raises(FeatureError, match="snapshot hash"):
        scripted_labels(bars, plans, Forged(plans))


# ----------------------------------------------------------------------------- frozen rules


class OnlyOne:
    """A fresh rule over the full history that passes through a single candidate."""

    def __init__(self, rule: TrendBreakout | MeanReversion, candidate_id: str) -> None:
        self.rule = rule
        self.candidate_id = candidate_id
        self.strategy_id = rule.strategy_id
        self.spec_hash = rule.spec_hash
        self.uses_source_vwap = rule.uses_source_vwap

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        cand = self.rule.on_bar(bar, now)
        return cand if cand is not None and cand.candidate_id == self.candidate_id else None

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        self.rule.on_split(instrument_id, divisor)

    def data_ok(self, instrument_id: str) -> bool:
        return self.rule.data_ok(instrument_id)


def test_every_candidate_is_kept_and_labelled_like_a_full_run_alone(
    walk: list[Bar], labels: LabelSet
) -> None:
    setups = setups_of(walk)
    portfolio = ResearchEngine(crypto_config(), mean_reversion(setups), setups).run()
    assert [r.candidate_id for r in labels.rows] == [c.candidate_id for c in portfolio.candidates]
    statuses = {r.status for r in labels.rows}
    assert {"FILLED", "REJECTED"} <= statuses
    filled = [r for r in labels.rows if r.status in ("FILLED", "PARTIAL")]
    assert len(filled) >= len(portfolio.trades)  # alone, no candidate waits for another
    assert labels.feature_names == FEATURE_NAMES and labels.feature_version == FEATURE_VERSION
    assert labels.source["dataset"] == "random-walk-test"
    assert labels.source["spec_hash"] == mean_reversion(setups).spec_hash
    sample = filled[:3] + filled[-2:] + [r for r in labels.rows if r.status == "REJECTED"][:2]
    for row in sample:
        bars = [
            b
            for b in walk
            if b.instrument_id == row.instrument_id
            and b.start < row.decision_time + timedelta(days=2)
        ]
        alone_setups = setups_of(bars)
        rule = OnlyOne(mean_reversion(alone_setups), row.candidate_id)
        result = ResearchEngine(crypto_config(), rule, alone_setups).run()  # type: ignore[arg-type]
        (outcome,) = result.candidates
        assert row.status == outcome.status
        if result.trades:
            trade = result.trades[0]
            assert row.net_usd == trade.net_pnl().quantize(NET_QUANTUM)
            assert row.exit_time == trade.exits[-1].time
        else:
            assert row.net_usd == 0 and row.exit_time is None


def test_truncating_future_bars_changes_no_earlier_feature_or_label(
    walk: list[Bar], labels: LabelSet
) -> None:
    cutoff = at(2500)
    truncated = label_walk([b for b in walk if b.start < cutoff]).between(T0, cutoff)
    before = labels.between(T0, cutoff)
    assert [r.candidate_id for r in truncated.rows] == [r.candidate_id for r in before.rows]
    resolved = 0
    for full, cut in zip(before.rows, truncated.rows, strict=True):
        assert full.features == cut.features  # features never look past the decision
        if full.decision_time < cutoff - timedelta(days=1):  # >> TTL + 6 h maximum hold
            assert full == cut  # a label resolved before the cut does not change
            resolved += 1
    assert resolved > 50


def test_parquet_round_trip_is_exact_and_other_versions_are_refused(
    labels: LabelSet, tmp_path: Path
) -> None:
    path = tmp_path / "labels" / "candidate_b.parquet"
    labels.save(path)
    loaded = LabelSet.load(path)
    assert loaded == labels
    assert dict(loaded.source) == dict(labels.source)
    x, y = labels.matrix()
    lx, ly = loaded.matrix()
    assert x.dtype == np.float64 and y.dtype == np.int64
    assert x.shape == (len(labels.rows), len(FEATURE_NAMES))
    assert np.array_equal(x, lx) and np.array_equal(y, ly)
    assert int(y.sum()) == sum(1 for r in labels.rows if r.win)
    old = LabelSet(
        rows=(), feature_names=("a",), feature_version="candidate-v0", source={"note": "it's old"}
    )
    old_path = tmp_path / "old.parquet"
    old.save(old_path)
    with pytest.raises(FeatureVersionError):
        LabelSet.load(old_path)
    assert LabelSet.load(old_path, expected_version=None) == old
    empty_x, empty_y = old.matrix()
    assert empty_x.shape == (0, 1) and empty_y.shape == (0,)


def test_label_rows_and_sets_fail_closed() -> None:
    decision = datetime(2026, 3, 2, 0, 15, 1, tzinfo=UTC)
    good = {
        "candidate_id": "c1",
        "family": "CANDIDATE_A",
        "instrument_id": BTC,
        "decision_time": decision,
        "exit_time": decision + timedelta(hours=1),
        "status": "FILLED",
        "net_usd": Decimal("0.01"),
        "net_return": 0.001,
        "win": True,
        "features": {"a": 1.0},
    }
    LabelRow(**good)  # type: ignore[arg-type]
    for bad in (
        {"win": False},
        {"net_usd": 0.01},
        {"status": "UNFILLED"},
        {"features": {"a": float("inf")}},
        {"decision_time": decision.replace(tzinfo=None)},
        {"family": "CANDIDATE_X"},
    ):
        with pytest.raises(LabelError):
            LabelRow(**{**good, **bad})  # type: ignore[arg-type]
    row = LabelRow(**good)  # type: ignore[arg-type]
    with pytest.raises(LabelError, match="ordered"):
        LabelSet(rows=(row, row), feature_names=("a",), feature_version="x", source={})
    with pytest.raises(LabelError, match="feature_names"):
        LabelSet(rows=(row,), feature_names=("b",), feature_version="x", source={})


# ----------------------------------------------------------------------------- stocks


def test_runtime_candidate_a_is_labelled_with_its_momentum_snapshot() -> None:
    cal = ExchangeCalendar("XNYS")
    specs = load_instruments(REPO / "configs" / "instruments" / "synthetic_equities.yaml")
    raw = generate(datetime(2026, 11, 2, tzinfo=UTC), datetime(2026, 11, 7, tzinfo=UTC))
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
        )
        for iid, bars in raw.items()
    }
    config = EngineConfig(
        policy=POLICY,
        fees=load_fee_schedule(REPO / "configs" / "fees" / "replay_demo.yaml"),
        fills=FILLS,
        track=Track.STOCKS,
        calendar=cal,
        start=datetime(2026, 11, 2, tzinfo=UTC),
        end=datetime(2026, 11, 7, tzinfo=UTC),
        loss_windows=("NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"),
        session_basis="XNYS",
    )
    adapter = SessionWindowAdapter(CandidateA(load_candidate_a_spec(), cal), cal)
    result = build_labels(
        adapter, family="CANDIDATE_A", config=config, instruments=setups, plan=OPEN_PLAN
    )
    assert result.source["snapshot_version"] == "momentum-v1"
    assert len(result.rows) > 20
    assert any(r.status == "FILLED" for r in result.rows)
    for row in result.rows:
        assert row.features["session_missing"] == 0.0 and row.features["streaming_missing"] == 1.0
        if row.exit_time is not None:  # flat by the calendar-relative close procedure
            session = cal.session_at(row.decision_time)
            assert session is not None
            assert row.exit_time <= cal.close_procedure_start(session) + timedelta(minutes=5)
