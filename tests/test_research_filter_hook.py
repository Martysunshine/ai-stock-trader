"""The research engine's candidate-filter hook and its controls (strategy §6; ml guide §5).

* Without a filter, and with an accept-all filter, every result hash equals the value recorded
  before the hook existed.
* A veto is ``REJECTED`` / ``NO_EDGE`` with binding ``filter:<id>`` and never trades; a filter
  sees every non-stale candidate before any portfolio check and cannot change a size.
* The hash-random control is deterministic and rejects close to its rate; the equal-turnover
  control accepts exactly the reference filter's count in every evaluation window.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from trader.accounting.fees import load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import load_policy
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.data.history import to_domain_bars
from trader.data.synthetic_history import SPLIT, generate
from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import content_hash
from trader.domain.market import Bar
from trader.domain.types import Track
from trader.features.candidate_features import FEATURE_NAMES, FeatureVersionError
from trader.research.adapters import SessionWindowAdapter
from trader.research.engine import (
    EngineConfig,
    EngineResult,
    FillAssumptions,
    InstrumentSetup,
    ResearchEngine,
)
from trader.research.filters import (
    AcceptAll,
    CandidateFilter,
    FilterDecision,
    HashRandomReject,
    ModelThresholdFilter,
    TurnoverMatchedReject,
    candidate_feature_fn,
    quantize_score,
)
from trader.strategies.candidate_a import CandidateA, load_candidate_a_spec
from trader.strategies.research_rules import build_rule, load_rule_spec

REPO = Path(__file__).resolve().parents[1]
SPECS = REPO / "src" / "trader" / "strategies" / "specs"

#: result_hash values recorded before the filter hook existed (same fixtures as below).
PINNED = {
    "walk_b": "23013ea8e7ecfb6a753ea2312f79b07480227114d1c36b6ad4a4674fd3fb8840",
    "walk_a": "5a13169675f4233ba7308ab52d6059f5b415c603bfa8a846da0e72e07239cd37",
    "candidate_a_v1": "8da4e68bdad30759387dee419e98c18b18570d7ef0da7d421f2b5bedac47a05d",
    "candidate_b_stocks_v1": "36c84d6c325abedd280d3f06d3d819d3f8692e1f1898c95ec7032b155ea85cd2",
}

FeatureFn = Callable[[TradeCandidate], Mapping[str, float]]


@pytest.fixture(scope="module")
def walk() -> list[Bar]:
    return random_walk(BTC, 50000, 4000, 1) + random_walk(ETH, 2500, 4000, 2)


def crypto_run(
    bars: list[Bar],
    spec_file: str,
    overrides: dict[str, str] | None = None,
    candidate_filter: CandidateFilter | None = None,
    feature_fn: FeatureFn | None = None,
    *,
    with_features: str | None = None,
) -> EngineResult:
    """``tests/test_research_engine.py::strategy_run`` with an optional filter."""
    setups = {
        iid: InstrumentSetup(spec=KRAKEN[iid], bars=[b for b in bars if b.instrument_id == iid])
        for iid in sorted({b.instrument_id for b in bars})
    }
    ticks = {iid: s.spec.price_tick for iid, s in setups.items()}
    rule = build_rule(load_rule_spec(SPECS / spec_file, overrides), Utc247Calendar(), ticks)
    if with_features is not None:
        feature_fn = candidate_feature_fn(rule, with_features)
    cfg = EngineConfig(
        policy=POLICY,
        fees=FEES,
        fills=FILLS,
        track=Track.CRYPTO,
        calendar=Utc247Calendar(),
        start=T0,
        end=T0 + timedelta(days=365),
        loss_windows=("UTC_DAY", "UTC_WEEK"),
        session_basis="UTC_DAY",
    )
    return ResearchEngine(cfg, rule, setups, candidate_filter, feature_fn).run()


@functools.cache
def stock_setups() -> dict[str, InstrumentSetup]:
    specs = load_instruments(REPO / "configs" / "instruments" / "synthetic_equities.yaml")
    raw = generate(datetime(2026, 10, 26, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    return {
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
        for iid, bars in raw.items()
    }


def stock_run(spec_name: str, candidate_filter: CandidateFilter | None = None) -> EngineResult:
    cal = ExchangeCalendar("XNYS")
    setups = stock_setups()
    strategy: object
    if spec_name == "candidate_a_v1":
        strategy = SessionWindowAdapter(CandidateA(load_candidate_a_spec(), cal), cal)
    else:
        strategy = build_rule(load_rule_spec(SPECS / f"{spec_name}.yaml"), cal)
    cfg = EngineConfig(
        policy=load_policy(REPO / "configs" / "policy" / "base.yaml").policy,
        fees=load_fee_schedule(REPO / "configs" / "fees" / "replay_demo.yaml"),
        fills=FillAssumptions("base", Decimal("1"), Decimal("2"), Decimal("0.10")),
        track=Track.STOCKS,
        calendar=cal,
        start=datetime(2026, 10, 26, tzinfo=UTC),
        end=datetime(2026, 12, 1, tzinfo=UTC),
        loss_windows=("NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"),
        session_basis="XNYS",
    )
    return ResearchEngine(cfg, strategy, setups, candidate_filter).run()  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- unchanged hashes


def test_no_filter_and_accept_all_reproduce_the_pre_hook_result_hashes(walk: list[Bar]) -> None:
    runs = {
        "walk_b": lambda f: crypto_run(walk, "candidate_b_crypto_v1.yaml", None, f),
        "walk_a": lambda f: crypto_run(
            walk, "candidate_a_crypto_v1.yaml", {"atr_stop_multiplier": "1.0"}, f
        ),
        "candidate_a_v1": lambda f: stock_run("candidate_a_v1", f),
        "candidate_b_stocks_v1": lambda f: stock_run("candidate_b_stocks_v1", f),
    }
    for name, run in runs.items():
        plain = run(None)
        assert plain.result_hash == PINNED[name], name
        assert plain.filter_log == () and plain.filter_hash == ""
        assert "filter_vetoes" not in plain.diagnostics
        if name == "candidate_b_stocks_v1":
            continue  # the slowest fixture; accept-all is covered by the other three
        accept_all = run(AcceptAll())
        assert accept_all.result_hash == PINNED[name], name
        assert accept_all.diagnostics["filter_vetoes"] == 0
        assert len(accept_all.filter_log) == len(accept_all.candidates)  # none were stale
        assert accept_all.filter_hash == content_hash([r.as_dict() for r in accept_all.filter_log])


class ScoredAccept:
    """Accepts everything but reports an extreme score: it must not change any size."""

    filter_id = "scored-accept"

    def decide(self, candidate: TradeCandidate, features: Mapping[str, float]) -> FilterDecision:
        assert tuple(features) == FEATURE_NAMES
        return FilterDecision(accept=True, score=Decimal("0.999999"), reason="confident")


def test_a_filter_cannot_change_size_and_receives_verified_candidate_features(
    walk: list[Bar],
) -> None:
    plain = crypto_run(walk, "candidate_b_crypto_v1.yaml")
    scored = crypto_run(
        walk, "candidate_b_crypto_v1.yaml", None, ScoredAccept(), with_features="CANDIDATE_B"
    )
    assert scored.result_hash == plain.result_hash
    assert [t.quantity for t in scored.trades] == [t.quantity for t in plain.trades]
    assert {r.score for r in scored.filter_log} == {"0.999999"}


# ----------------------------------------------------------------------------- vetoes


def test_vetoed_candidates_are_rejected_as_no_edge_and_never_trade(walk: list[Bar]) -> None:
    control = HashRandomReject(Decimal("0.5"), "veto-test")
    r = crypto_run(walk, "candidate_b_crypto_v1.yaml", None, control)
    vetoed = {e.candidate_id for e in r.filter_log if not e.accept}
    assert vetoed and len(vetoed) < len(r.filter_log)
    assert r.diagnostics["filter_vetoes"] == len(vetoed)
    by_id = {c.candidate_id: c for c in r.candidates}
    for cid in vetoed:
        outcome = by_id[cid]
        assert outcome.status == "REJECTED"
        assert outcome.reasons == ("NO_EDGE",)
        assert outcome.binding == f"filter:{control.filter_id}"
        assert outcome.quantity == 0
    assert not vetoed & {t.candidate_id for t in r.trades}
    assert r.trades  # accepted candidates still trade through the unchanged checks


class StaleScripted(Scripted):
    def data_ok(self, instrument_id: str) -> bool:
        return False


class Recorder:
    filter_id = "recorder"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def decide(self, candidate: TradeCandidate, features: Mapping[str, float]) -> FilterDecision:
        self.seen.append(candidate.candidate_id)
        return FilterDecision(accept=True, score=None, reason="recorded")


def scripted_run(
    bars: list[Bar],
    plans: dict[tuple[str, datetime], tuple[str, str, int, int]],
    candidate_filter: CandidateFilter | None,
    strategy: Scripted | None = None,
) -> EngineResult:
    setups = {
        iid: InstrumentSetup(spec=KRAKEN[iid], bars=[b for b in bars if b.instrument_id == iid])
        for iid in {b.instrument_id for b in bars}
    }
    cfg = EngineConfig(
        policy=POLICY,
        fees=FEES,
        fills=FILLS,
        track=Track.CRYPTO,
        calendar=Utc247Calendar(),
        start=T0,
        end=T0 + timedelta(days=30),
        loss_windows=("UTC_DAY", "UTC_WEEK"),
        session_basis="UTC_DAY",
    )
    return ResearchEngine(cfg, strategy or Scripted(plans), setups, candidate_filter).run()


def test_the_filter_runs_after_the_stale_check_and_before_pauses_and_limits() -> None:
    bars = [flat(BTC, 0), flat(BTC, 1), flat(BTC, 2)]
    plans = {(BTC, at(1)): PLAN}
    stale = Recorder()
    r = scripted_run(bars, plans, stale, StaleScripted(plans))
    assert r.candidates[0].reasons == ("STALE_DATA",) and stale.seen == []
    # the daily-loss scenario of test_research_engine: the paused candidate is still shown
    day2 = 96
    bars = [flat(BTC, i) for i in range(6)]
    bars.append(mk(BTC, at(6), "40000", "40100", "39900", "40000"))
    bars += [flat(BTC, i, "40000") for i in range(7, 12)]
    bars += [flat(BTC, i, "40000") for i in range(day2, day2 + 4)]
    plans = {
        (BTC, at(1)): PLAN,
        (BTC, at(9)): ("39500", "41000", 360, 120),
        (BTC, at(day2 + 1)): ("39500", "41000", 360, 120),
    }
    rec = Recorder()
    r = scripted_run(bars, plans, rec)
    paused = f"c-{BTC}-{at(9).isoformat()}"
    assert paused in rec.seen and len(rec.seen) == 3
    assert {c.candidate_id: c for c in r.candidates}[paused].reasons == ("PAUSED",)
    assert r.result_hash == scripted_run(bars, plans, None).result_hash


class FixedScorer:
    bundle_id = "fixed-test"
    feature_version = "candidate-v1"

    def __init__(self, probability: float) -> None:
        self.probability = probability

    def predict_proba(self, features: Mapping[str, float]) -> float:
        assert tuple(features) == FEATURE_NAMES
        return self.probability


def test_model_threshold_filter_accepts_at_or_above_the_threshold() -> None:
    bars = [flat(BTC, 0), flat(BTC, 1), flat(BTC, 2)]
    plans = {(BTC, at(1)): PLAN}
    plain = scripted_run(bars, plans, None)
    features = dict.fromkeys(FEATURE_NAMES, 0.0)

    def run(p: float, threshold: str) -> EngineResult:
        flt = ModelThresholdFilter(FixedScorer(p), Decimal(threshold))
        setups = {BTC: InstrumentSetup(spec=KRAKEN[BTC], bars=bars)}
        cfg = EngineConfig(
            policy=POLICY,
            fees=FEES,
            fills=FILLS,
            track=Track.CRYPTO,
            calendar=Utc247Calendar(),
            start=T0,
            end=T0 + timedelta(days=30),
            loss_windows=("UTC_DAY", "UTC_WEEK"),
            session_basis="UTC_DAY",
        )
        return ResearchEngine(cfg, Scripted(plans), setups, flt, lambda c: features).run()

    at_threshold = run(0.5, "0.5")
    assert at_threshold.result_hash == plain.result_hash  # accepted: identical trade and size
    assert at_threshold.trades[0].quantity == plain.trades[0].quantity
    assert at_threshold.filter_log[0].score == "0.5"
    below = run(0.4999999, "0.5")
    assert not below.trades and below.candidates[0].reasons == ("NO_EDGE",)
    assert below.candidates[0].binding == "filter:model:fixed-test:0.5"
    assert below.filter_log[0].score == "0.5"  # rounded for the log; decided on the probability
    with pytest.raises(ValueError, match="feature_fn"):
        ModelThresholdFilter(FixedScorer(0.5), Decimal("0.5")).decide(
            Scripted(plans).on_bar(bars[0], bars[0].available_time),  # type: ignore[arg-type]
            {},
        )
    with pytest.raises(TypeError):
        ModelThresholdFilter(FixedScorer(0.5), 0.5)  # type: ignore[arg-type]
    stale_scorer = FixedScorer(0.5)
    stale_scorer.feature_version = "candidate-v0"
    with pytest.raises(FeatureVersionError):
        ModelThresholdFilter(stale_scorer, Decimal("0.5"))


def test_filter_decisions_are_hash_safe() -> None:
    assert quantize_score(0.25) == Decimal("0.25")
    assert quantize_score(1 / 3) == Decimal("0.333333")
    assert quantize_score(0.9999996) == Decimal("1.000000")
    with pytest.raises(TypeError):
        FilterDecision(accept=True, score=0.5, reason="float")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="6 decimal places"):
        FilterDecision(accept=True, score=Decimal("0.1234567"), reason="too fine")
    with pytest.raises(ValueError):
        quantize_score(float("nan"))
    with pytest.raises(ValueError):
        quantize_score(1.5)


# ----------------------------------------------------------------------------- controls


def test_hash_random_reject_is_deterministic_and_close_to_its_rate() -> None:
    ids = [f"cand-{i:05d}" for i in range(20000)]
    control = HashRandomReject(Decimal("0.3"), "salt-1")
    first = [control.rejects(i) for i in ids]
    assert first == [HashRandomReject(Decimal("0.3"), "salt-1").rejects(i) for i in ids]
    rate = sum(first) / len(ids)
    assert abs(rate - 0.3) < 4 * math.sqrt(0.3 * 0.7 / len(ids))
    other = [HashRandomReject(Decimal("0.3"), "salt-2").rejects(i) for i in ids]
    assert first != other
    assert not any(HashRandomReject(Decimal(0), "s").rejects(i) for i in ids[:500])
    assert all(HashRandomReject(Decimal(1), "s").rejects(i) for i in ids[:500])
    with pytest.raises(ValueError):
        HashRandomReject(Decimal("1.1"), "s")
    with pytest.raises(ValueError):
        HashRandomReject(0.3, "s")  # type: ignore[arg-type]


def test_turnover_matched_control_accepts_the_reference_count_in_every_window(
    walk: list[Bar],
) -> None:
    windows = [(T0, T0 + timedelta(days=20)), (T0 + timedelta(days=20), T0 + timedelta(days=365))]

    class Model:
        """Stand-in for a model filter: features in, deterministic probability out."""

        bundle_id = "stand-in"
        feature_version = "candidate-v1"

        def predict_proba(self, features: Mapping[str, float]) -> float:
            return 1.0 / (1.0 + math.exp(-features["ref_distance_atr"] - 2.5))

    reference_filter = ModelThresholdFilter(Model(), Decimal("0.4"))
    reference = crypto_run(
        walk, "candidate_b_crypto_v1.yaml", None, reference_filter, with_features="CANDIDATE_B"
    )
    log = reference.filter_log
    counts = [sum(1 for e in log if e.accept and s <= e.decision_time < e_) for s, e_ in windows]
    assert all(0 < c < len(log) for c in counts)
    control = TurnoverMatchedReject.matching(log, windows=windows, salt="control-1")
    matched = crypto_run(walk, "candidate_b_crypto_v1.yaml", None, control)
    assert [e.candidate_id for e in matched.filter_log] == [e.candidate_id for e in log]
    matched_counts = [
        sum(1 for e in matched.filter_log if e.accept and s <= e.decision_time < e_)
        for s, e_ in windows
    ]
    assert matched_counts == counts
    accepted = {e.candidate_id for e in matched.filter_log if e.accept}
    assert accepted != {e.candidate_id for e in log if e.accept}  # random, not a copy
    again = crypto_run(walk, "candidate_b_crypto_v1.yaml", None, control)
    assert again.filter_hash == matched.filter_hash and again.result_hash == matched.result_hash
    # explicit target counts, and fail-closed construction
    explicit = TurnoverMatchedReject(
        windows=windows,
        universe=[(e.candidate_id, e.decision_time) for e in log],
        accept_counts=counts,
        salt="control-1",
    )
    assert crypto_run(walk, "candidate_b_crypto_v1.yaml", None, explicit).filter_hash == (
        matched.filter_hash
    )
    with pytest.raises(ValueError, match="accepts"):
        TurnoverMatchedReject(
            windows=windows,
            universe=[(e.candidate_id, e.decision_time) for e in log],
            accept_counts=[len(log) + 1, 0],
            salt="s",
        )
    with pytest.raises(ValueError, match="overlap"):
        TurnoverMatchedReject(
            windows=[windows[1], windows[0]], universe=[], accept_counts=[0, 0], salt="s"
        )
    with pytest.raises(ValueError, match="outside every evaluation window"):
        TurnoverMatchedReject.matching(log, windows=windows[:1], salt="s")
    narrow = TurnoverMatchedReject(
        windows=windows,
        universe=[(e.candidate_id, e.decision_time) for e in log[:5]],
        accept_counts=[1, 0],
        salt="s",
    )
    with pytest.raises(ValueError, match="not seen by the reference filter"):
        crypto_run(walk, "candidate_b_crypto_v1.yaml", None, narrow)
