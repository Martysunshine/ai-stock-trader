"""Candidate features ``candidate-v1`` (docs/ml_research_guide.md §3-§4; strategy §6-§7).

* The snapshot behind every candidate equals a direct batch recomputation from the bars, and the
  candidate features follow from it; every value is finite and the column layout is fixed.
* The accessor refactor leaves every candidate ID and snapshot hash exactly as before.
* Unknown schemas, mismatched snapshots and other feature versions are refused.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.test_research_engine import BTC, ETH, random_walk
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.data.history import to_domain_bars
from trader.data.synthetic_history import generate
from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.market import Bar
from trader.domain.types import BPS, Direction, Track
from trader.features import momentum
from trader.features.candidate_features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    FeatureError,
    FeatureVersionError,
    compute,
    require_feature_version,
    snapshot_hash,
    snapshot_version,
    verify_snapshot,
)
from trader.research.adapters import SessionWindowAdapter
from trader.strategies.candidate_a import CandidateA, load_candidate_a_spec
from trader.strategies.research_rules import AnyRuleSpec, build_rule, load_rule_spec

REPO = Path(__file__).resolve().parents[1]
SPECS = REPO / "src" / "trader" / "strategies" / "specs"

#: (candidate_id, snapshot_hash) digests recorded before the features accessor existed.
PINNED_SNAPSHOTS = {
    "candidate_a_crypto_v1.yaml": (
        890,
        "1545b10577f02f1e704b136d542ef682818c3fc6800c44979b2a004d4a4925a3",
    ),
    "candidate_b_crypto_v1.yaml": (
        475,
        "24f05d3f17809678937cc87ff55f128c56009c6d3449db0350ee210e2ac4c235",
    ),
    "candidate_a_v1 (runtime, synthetic equities)": (
        394,
        "18cd030eebc7a590b6c44f8e6c2c344ae6d986e6b95188ae5643d35f8df4ab2c",
    ),
}


@pytest.fixture(scope="module")
def walk() -> list[Bar]:
    bars = random_walk(BTC, 50000, 4000, 1) + random_walk(ETH, 2500, 4000, 2)
    return sorted(bars, key=lambda b: (b.available_time, b.instrument_id))


@pytest.fixture(scope="module")
def equities() -> list[Bar]:
    raw = generate(datetime(2026, 10, 26, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    bars = [
        b
        for iid, history in raw.items()
        for b in to_domain_bars(
            history,
            instrument_id=iid,
            feed="SYNTHETIC",
            finalization_delay=timedelta(seconds=1),
            require_source_vwap=True,
        )
    ]
    return sorted(bars, key=lambda b: (b.available_time, b.instrument_id))


def batch_snapshot(bars: list[Bar], spec: AnyRuleSpec) -> dict[str, Decimal]:
    """The streaming-v1 snapshot recomputed directly from its definitions (no running sums)."""
    n = len(bars)
    ref = bars[-(spec.reference_window_bars or n) :]
    num = sum(((b.high + b.low + b.close) * b.volume for b in ref), Decimal(0))
    den = sum((b.volume * 3 for b in ref), Decimal(0))

    def tr(i: int) -> Decimal:
        b, p = bars[i], bars[i - 1]
        return max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))

    atr = sum((tr(i) for i in range(n - spec.atr_period_bars, n)), Decimal(0))
    long_atr = sum((tr(i) for i in range(n - spec.long_atr_period_bars, n)), Decimal(0))
    closes = [b.close for b in bars[-spec.efficiency_window_bars - 1 :]]
    path = sum((abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))), Decimal(0))
    context = [
        b.close for b in bars if int(b.end.timestamp()) // 60 % spec.context_bar_minutes == 0
    ]
    return {
        "close": bars[-1].close,
        "reference": num / den,
        "atr": atr / spec.atr_period_bars,
        "long_atr": long_atr / spec.long_atr_period_bars,
        "efficiency_ratio": abs(closes[-1] - closes[0]) / path if path else Decimal(0),
        "prior_high": max(b.high for b in bars[-spec.breakout_lookback_bars - 1 : -1]),
        "context_return": context[-1] / context[-1 - spec.context_lookback_bars] - 1,
    }


@pytest.mark.parametrize(
    ("spec_file", "family"),
    [("candidate_a_crypto_v1.yaml", "CANDIDATE_A"), ("candidate_b_crypto_v1.yaml", "CANDIDATE_B")],
)
def test_streaming_rule_features_equal_batch_recomputation(
    walk: list[Bar], spec_file: str, family: str
) -> None:
    spec = load_rule_spec(SPECS / spec_file)
    rule = build_rule(spec, Utc247Calendar())
    history: dict[str, list[Bar]] = {}
    checked = 0
    for bar in walk:
        history.setdefault(bar.instrument_id, []).append(bar)
        cand = rule.on_bar(bar, bar.available_time)
        if cand is None:
            continue
        snap = rule.features(bar.instrument_id)
        assert rule.snapshot_version == "streaming-v1" == snapshot_version(snap)
        verify_snapshot(snap, cand, rule.snapshot_version)  # exactly the hashed values
        values = compute(snap, cand, family=family)
        assert tuple(values) == FEATURE_NAMES
        assert all(isinstance(v, float) and math.isfinite(v) for v in values.values())
        if checked < 40 or checked % 25 == 0:  # the batch recomputation is O(window) per call
            batch = batch_snapshot(history[bar.instrument_id], spec)
            assert batch == snap
            assert compute(batch, cand, family=family) == values
            assert values["ref_distance_atr"] == float(
                (snap["close"] - snap["reference"]) / snap["atr"]
            )
            assert values["atr_ratio"] == float(snap["atr"] / snap["long_atr"])
            assert values["stop_distance_atr"] == float(
                (cand.reference_price - cand.stop_price) / snap["atr"]
            )
            assert values["streaming_missing"] == 0.0 and values["session_missing"] == 1.0
            assert values["vwap_distance_atr"] == 0.0 and values["trailing_return_3"] == 0.0
        checked += 1
    assert checked > 100


def test_runtime_candidate_a_features_equal_batch_recomputation(equities: list[Bar]) -> None:
    cal = ExchangeCalendar("XNYS")
    spec = load_candidate_a_spec()
    adapter = SessionWindowAdapter(CandidateA(spec, cal), cal)
    sessions: dict[tuple[str, object], list[Bar]] = {}
    seen = 0
    for bar in equities:
        session = cal.session_at(bar.start)
        assert session is not None
        bars = sessions.setdefault((bar.instrument_id, session.session_date), [])
        bars.append(bar)
        cand = adapter.on_bar(bar, bar.available_time)
        if cand is None:
            continue
        snap = adapter.features(bar.instrument_id)
        assert adapter.snapshot_version == "momentum-v1" == snapshot_version(snap)
        verify_snapshot(snap, cand, adapter.snapshot_version)
        assert snap == {
            "close": bars[-1].close,
            "prior_high": momentum.prior_high(bars, spec.breakout_lookback_bars),
            "session_vwap": momentum.session_vwap(bars),
            "atr": momentum.atr(bars, spec.atr_period_bars),
            "trailing_return_3": momentum.trailing_return(bars, 3),
        }
        values = compute(snap, cand, family="CANDIDATE_A")
        assert tuple(values) == FEATURE_NAMES
        assert all(math.isfinite(v) for v in values.values())
        assert values["vwap_distance_atr"] == float(
            (snap["close"] - snap["session_vwap"]) / snap["atr"]
        )
        assert values["trailing_return_3"] == float(snap["trailing_return_3"])
        assert values["streaming_missing"] == 1.0 and values["session_missing"] == 0.0
        assert values["ref_distance_atr"] == 0.0 and values["atr_ratio"] == 1.0
        seen += 1
    assert seen > 100


def test_the_accessor_refactor_keeps_candidate_ids_and_snapshot_hashes(
    walk: list[Bar], equities: list[Bar]
) -> None:
    def digest(strategy: object, bars: list[Bar]) -> tuple[int, str]:
        pairs = []
        for b in bars:
            c = strategy.on_bar(b, b.available_time)  # type: ignore[attr-defined]
            if c is not None:
                pairs.append([c.candidate_id, c.snapshot_hash])
        return len(pairs), content_hash(pairs)

    for spec_file in ("candidate_a_crypto_v1.yaml", "candidate_b_crypto_v1.yaml"):
        rule = build_rule(load_rule_spec(SPECS / spec_file), Utc247Calendar())
        assert digest(rule, walk) == PINNED_SNAPSHOTS[spec_file]
    cal = ExchangeCalendar("XNYS")
    adapter = SessionWindowAdapter(CandidateA(load_candidate_a_spec(), cal), cal)
    assert (
        digest(adapter, equities)
        == PINNED_SNAPSHOTS["candidate_a_v1 (runtime, synthetic equities)"]
    )


def _candidate(decision: datetime, track: Track, close: str = "100") -> TradeCandidate:
    snap = streaming_snapshot(close)
    return TradeCandidate(
        candidate_id="c-test",
        strategy_id="test",
        strategy_version=1,
        spec_hash="0" * 64,
        instrument_id="XNYS:SYNA" if track is Track.STOCKS else BTC,
        track=track,
        direction=Direction.LONG,
        feed="T",
        bar_end=decision - timedelta(seconds=1),
        decision_time=decision,
        reference_price=Decimal(close),
        stop_price=Decimal("98"),
        target_price=Decimal("104"),
        max_hold_minutes=60,
        entry_limit_offset_bps=Decimal(5),
        entry_ttl_seconds=120,
        gap_allowance_fraction=Decimal(10) * BPS,
        snapshot_hash=snapshot_hash(snap, "streaming-v1"),
        expires_at=decision + timedelta(seconds=60),
    )


def streaming_snapshot(close: str = "100") -> dict[str, Decimal]:
    return {
        "close": Decimal(close),
        "reference": Decimal("99"),
        "atr": Decimal("1"),
        "long_atr": Decimal("2"),
        "efficiency_ratio": Decimal("0.25"),
        "prior_high": Decimal("99.5"),
        "context_return": Decimal("0.01"),
    }


def test_time_features_use_exchange_time_for_stocks_and_utc_otherwise() -> None:
    decision = datetime(2026, 11, 10, 15, 0, 1, tzinfo=UTC)  # 10:00:01 in New York, a Tuesday
    stock = compute(streaming_snapshot(), _candidate(decision, Track.STOCKS), family="CANDIDATE_A")
    crypto = compute(streaming_snapshot(), _candidate(decision, Track.CRYPTO), family="CANDIDATE_A")
    ny = 2 * math.pi * (10 * 3600 + 1) / 86400
    utc = 2 * math.pi * (15 * 3600 + 1) / 86400
    assert (stock["hour_sin"], stock["hour_cos"]) == (math.sin(ny), math.cos(ny))
    assert (crypto["hour_sin"], crypto["hour_cos"]) == (math.sin(utc), math.cos(utc))
    assert stock["weekday"] == crypto["weekday"] == 1.0
    assert stock["reward_risk"] == 2.0 and stock["stop_distance_atr"] == 2.0
    assert stock["breakout_atr"] == 0.5 and stock["atr_to_close"] == 0.01


def test_unknown_schemas_mismatched_snapshots_and_other_versions_are_refused() -> None:
    decision = datetime(2026, 11, 10, 15, 0, 1, tzinfo=UTC)
    cand = _candidate(decision, Track.CRYPTO)
    with pytest.raises(FeatureError, match="unknown feature snapshot schema"):
        compute({**streaming_snapshot(), "extra": Decimal(1)}, cand, family="CANDIDATE_A")
    with pytest.raises(FeatureError, match="reference price"):
        compute(streaming_snapshot("101"), cand, family="CANDIDATE_A")
    with pytest.raises(FeatureError, match="unknown rule family"):
        compute(streaming_snapshot(), cand, family="CANDIDATE_C")
    with pytest.raises(FeatureError, match="positive"):
        compute({**streaming_snapshot(), "atr": Decimal(0)}, cand, family="CANDIDATE_B")
    momentum_snap = {
        "close": Decimal(100),
        "prior_high": Decimal(99),
        "session_vwap": Decimal(99),
        "atr": Decimal(1),
        "trailing_return_3": Decimal("0.001"),
    }
    with pytest.raises(FeatureError, match="runtime Candidate A"):
        compute(momentum_snap, cand, family="CANDIDATE_B")
    with pytest.raises(FeatureError, match="snapshot hash"):
        verify_snapshot({**streaming_snapshot(), "atr": Decimal(2)}, cand, "streaming-v1")
    with pytest.raises(FeatureError, match="declared version"):
        verify_snapshot(streaming_snapshot(), cand, "momentum-v1")
    require_feature_version(FEATURE_VERSION)
    with pytest.raises(FeatureVersionError):
        require_feature_version("candidate-v0")
    with pytest.raises(LookupError):
        build_rule(load_rule_spec(SPECS / "candidate_b_crypto_v1.yaml"), Utc247Calendar()).features(
            BTC
        )
