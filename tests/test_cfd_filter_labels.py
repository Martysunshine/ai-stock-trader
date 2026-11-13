"""Building blocks of the CFD filter trial (``research/cfd_filter.py``, T-0026) on SYNTHETIC
hourly quotes: labels from an unfiltered run, a signal traded alone, purge and embargo, the
model filter and the two refusal controls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from trader.data.quote_bars import QuoteBar
from trader.domain.types import Direction
from trader.features.cfd_breakout import FEATURE_NAMES, BreakoutFeatureBook
from trader.models.bundle import ModelBundle
from trader.research.cfd_engine import (
    CfdCosts,
    CfdEngineConfig,
    CfdInstrument,
    CfdSignal,
    run_cfd_engine,
)
from trader.research.cfd_filter import (
    CfdLabel,
    CfdModelFilter,
    DecisionWindow,
    RandomRefusal,
    RefuseAll,
    SignalFeatures,
    SmallestBreakRefusal,
    fold_data,
    label_window,
    trade_alone,
)
from trader.research.training import DEFAULT_LOGISTIC_GRID, purge, to_datetime64, train_fold
from trader.strategies.cfd_rules import CfdHourlyChannel, CfdHourlyChannelSpec, load_cfd_spec

from .cfd_filter_fixtures import quote_bars
from .cfd_fixtures import CRYPTO_POLICY, FEES
from .cfd_fixtures import spec as cfd_spec

ROOT = Path(__file__).resolve().parents[1]
BTC, ETH = "CAPITAL:BTCUSD", "CAPITAL:ETHUSD"
FIRST = datetime(2024, 12, 1, tzinfo=UTC)
JAN, FEB, MAR, APR = (datetime(2025, m, 1, tzinfo=UTC) for m in (1, 2, 3, 4))
Bars = dict[str, list[QuoteBar]]
COSTS = CfdCosts(
    label="base",
    slippage_bps=Decimal(1),
    spread_floor_bps={BTC: Decimal("6.8"), ETH: Decimal("8.1")},
)


def rule_spec() -> CfdHourlyChannelSpec:
    spec = load_cfd_spec(ROOT / "src/trader/strategies/specs/cfd_crypto_hourly_breakout_v1.yaml")
    assert isinstance(spec, CfdHourlyChannelSpec)
    return spec


@pytest.fixture(scope="module")
def bars() -> Bars:
    return {
        BTC: quote_bars("BTCUSD", FIRST, APR + timedelta(days=30), seed=11),
        ETH: quote_bars("ETHUSD", FIRST, APR + timedelta(days=30), seed=12),
    }


def config(bars: Bars, start: datetime, end: datetime) -> CfdEngineConfig:
    return CfdEngineConfig(
        policy=CRYPTO_POLICY,
        fees=FEES,
        instruments={
            iid: CfdInstrument(
                cfd_spec(iid.split(":")[1]),
                tuple(b for b in series if start - timedelta(days=30) <= b.start < end),
            )
            for iid, series in bars.items()
        },
        costs=COSTS,
        start=start,
        end=end,
        capital=Decimal(100),
    )


def rule() -> CfdHourlyChannel:
    return CfdHourlyChannel(rule_spec(), {BTC: Decimal("0.01"), ETH: Decimal("0.01")})


def features(bars: Bars) -> SignalFeatures:
    return SignalFeatures(BreakoutFeatureBook(bars, channel_hours=24, atr_hours=24))


# ----------------------------------------------------------------------------- labels


def test_every_signal_gets_a_label_from_its_trade_or_from_trading_it_alone(
    bars: Bars,
) -> None:
    window = label_window(config(bars, JAN, FEB), rule(), features(bars), decide_until=FEB)
    counts = window.counts
    assert counts["signals"] > 50
    assert counts["realised"] > 0 and counts["alone"] > 0
    assert counts["signals"] == sum(
        counts[k] for k in ("realised", "alone", "no_features", "no_outcome")
    )
    assert len(window.labels) == counts["realised"] + counts["alone"]
    for label in window.labels:
        assert JAN <= label.decided_at < FEB and label.exit_time <= FEB
        assert label.decided_at < label.exit_time
        assert label.win == (label.net_usd > 0)
        assert list(label.features) == list(FEATURE_NAMES)
        assert (label.source == "realised") == (label.status_in_run == "FILLED")


def test_a_taken_signal_traded_alone_has_its_realised_result(
    bars: Bars,
) -> None:
    cfg = config(bars, JAN, FEB)
    result = run_cfd_engine(cfg, rule(), record_candidates=True)
    signals = {s.signal_id: s for s in result.signals}
    assert len(result.trades) >= 10
    for trade in result.trades[:10]:
        alone = trade_alone(cfg, signals[trade.signal_id])
        assert alone is not None
        assert (alone.exit_time, alone.exit_reason) == (trade.exit_time, trade.exit_reason)
        assert (alone.net_usd > 0) == (trade.net_pnl() > 0)
        assert float(alone.net_usd / alone.notional_usd) == pytest.approx(
            float(trade.net_pnl() / trade.notional_usd), rel=1e-9, abs=1e-12
        )


def test_the_decision_window_keeps_only_its_own_signals(
    bars: Bars,
) -> None:
    until = JAN + timedelta(days=10)
    windowed = run_cfd_engine(
        config(bars, JAN, FEB), DecisionWindow(rule(), JAN, until), record_candidates=True
    )
    plain = run_cfd_engine(config(bars, JAN, FEB), rule(), record_candidates=True)
    kept = [s.signal_id for s in windowed.signals]
    assert kept and all(JAN <= s.decided_at < until for s in windowed.signals)
    assert kept == [s.signal_id for s in plain.signals if s.decided_at < until]


def _label(decided: datetime, exit_: datetime, net: str, n: int) -> CfdLabel:
    return CfdLabel(
        signal_id=f"s{n}",
        instrument_id=BTC,
        decided_at=decided,
        direction="LONG",
        break_atr=Decimal("0.5"),
        features=dict.fromkeys(FEATURE_NAMES, float(n)),
        status_in_run="FILLED",
        source="realised",
        net_usd=Decimal(net),
        notional_usd=Decimal(100),
        exit_time=exit_,
        exit_reason="TARGET",
    )


def test_purge_drops_every_label_decided_or_closed_within_the_embargo() -> None:
    end = datetime(2025, 3, 1, tzinfo=UTC)
    embargo = 21 * 60
    cutoff = end - timedelta(minutes=embargo)
    labels = [
        _label(cutoff - timedelta(hours=10), cutoff - timedelta(hours=1), "1", 1),  # kept
        _label(cutoff - timedelta(hours=10), cutoff, "-1", 2),  # closes at the cutoff
        _label(cutoff - timedelta(hours=2), cutoff + timedelta(hours=3), "1", 3),  # closes later
        _label(cutoff, cutoff + timedelta(hours=1), "-1", 4),  # decided at the cutoff
        _label(cutoff - timedelta(days=3), cutoff - timedelta(days=2), "-1", 5),  # kept
    ]
    data = fold_data(labels, family="CFD_HOURLY_CHANNEL", source={})
    kept = purge(data, train_end=end, embargo_minutes=embargo)
    assert len(data) == 5 and len(kept) == 2
    assert sorted(kept.X[:, 0].tolist()) == [1.0, 5.0]
    assert (kept.exit_time < to_datetime64(cutoff)).all()
    assert (kept.decision_time < to_datetime64(cutoff)).all()
    # without an embargo only what closes at or after the end itself goes
    assert len(purge(data, train_end=end, embargo_minutes=0)) == 5
    assert len(purge(data, train_end=cutoff, embargo_minutes=0)) == 2


def test_labels_become_training_rows_in_decision_order() -> None:
    t = datetime(2025, 1, 10, tzinfo=UTC)
    labels = [
        _label(t + timedelta(hours=2), t + timedelta(hours=5), "-0.1", 2),
        _label(t, t + timedelta(hours=3), "0.4", 1),
    ]
    data = fold_data(labels, family="CFD_HOURLY_CHANNEL", source={"dataset": "x"})
    assert data.X[:, 0].tolist() == [1.0, 2.0] and data.y.tolist() == [1, 0]
    assert data.net_return.tolist() == pytest.approx([0.004, -0.001])
    assert data.feature_names == FEATURE_NAMES and data.feature_version == "cfd-breakout-v1"


# ----------------------------------------------------------------------------- filters


def _signal(sid: str, priority: str, direction: Direction = Direction.LONG) -> CfdSignal:
    return CfdSignal(
        signal_id=sid,
        instrument_id=BTC,
        direction=direction,
        decided_at=datetime(2025, 1, 1, 9, tzinfo=UTC),
        reference=Decimal(100),
        stop=Decimal(99),
        target=Decimal(102),
        target_role="EXIT",
        max_hold_minutes=60,
        priority=Decimal(priority),
    )


def test_the_equal_turnover_control_refuses_the_smallest_breaks_first() -> None:
    seen = [_signal("a", "0.9"), _signal("b", "0.1"), _signal("c", "0.5"), _signal("d", "0.1")]
    control = SmallestBreakRefusal.matching(seen, 2)
    assert control.refused == {"b", "d"}  # equal breaks: both, by id
    assert [control.decide(s).accept for s in seen] == [True, False, True, False]
    assert SmallestBreakRefusal.matching(seen, 0).refused == frozenset()
    assert SmallestBreakRefusal.matching(seen, 4).refused == {"a", "b", "c", "d"}
    assert SmallestBreakRefusal.matching(seen[:3], 1).refused == {"b"}
    with pytest.raises(ValueError, match="not seen"):
        control.decide(_signal("e", "0.2"))
    with pytest.raises(ValueError, match="cannot refuse"):
        SmallestBreakRefusal.matching(seen, 5)
    with pytest.raises(ValueError, match="twice"):
        SmallestBreakRefusal.matching([*seen, seen[0]], 1)


def test_random_refusal_is_seeded_and_refuses_at_its_rate() -> None:
    signals = [_signal(f"sig-{n}", "1", Direction.SHORT) for n in range(4000)]
    a = [RandomRefusal(Decimal("0.3"), "salt-1").decide(s).accept for s in signals]
    b = [RandomRefusal(Decimal("0.3"), "salt-1").decide(s).accept for s in signals]
    c = [RandomRefusal(Decimal("0.3"), "salt-2").decide(s).accept for s in signals]
    assert a == b and a != c
    assert abs(1 - float(np.mean(a)) - 0.3) < 0.03
    assert all(RandomRefusal(Decimal(0), "s").decide(s).accept for s in signals[:50])
    assert not any(RandomRefusal(Decimal(1), "s").decide(s).accept for s in signals[:50])
    assert not RefuseAll().decide(signals[0]).accept
    with pytest.raises(ValueError, match="salt"):
        RandomRefusal(Decimal("0.3"), "")


def test_the_model_filter_accepts_at_or_above_its_threshold_and_only_refuses(
    bars: Bars,
) -> None:
    fx = features(bars)
    train = label_window(config(bars, JAN, MAR), rule(), fx, decide_until=MAR)
    val = label_window(config(bars, MAR, APR), rule(), fx, decide_until=APR)
    family = "CFD_HOURLY_CHANNEL"
    trained = train_fold(
        purge(
            fold_data(train.labels, family=family, source={}), train_end=MAR, embargo_minutes=1260
        ),
        fold_data(val.labels, family=family, source={}),
        model_type="logistic",
        grid=DEFAULT_LOGISTIC_GRID[:2],
        seed=1,
        fold_index=1,
        training_start=JAN,
        training_cutoff=MAR,
        threshold_grid=[Decimal("0.3"), Decimal("0.4"), Decimal("0.5")],
        min_accepts=5,
    )
    bundle = trained.bundle
    model = CfdModelFilter(bundle, fx)
    test_cfg = config(bars, APR, APR + timedelta(days=30))
    plain = run_cfd_engine(test_cfg, rule(), record_candidates=True)
    filtered = run_cfd_engine(test_cfg, rule(), model)
    assert len(filtered.filter_log) == len(plain.signals) > 0
    for signal, decision in zip(plain.signals, filtered.filter_log, strict=True):
        p = bundle.predict_proba(fx(signal))
        assert decision.candidate_id == signal.signal_id
        assert decision.accept == (Decimal(p) >= bundle.threshold)
    taken = {t.signal_id for t in filtered.trades}
    refused = {r.candidate_id for r in filtered.filter_log if not r.accept}
    assert not taken & refused
    assert taken <= {s.signal_id for s in plain.signals}
    # the same data, grid and seed seal the same bundle
    again = train_fold(
        purge(
            fold_data(train.labels, family=family, source={}), train_end=MAR, embargo_minutes=1260
        ),
        fold_data(val.labels, family=family, source={}),
        model_type="logistic",
        grid=DEFAULT_LOGISTIC_GRID[:2],
        seed=1,
        fold_index=1,
        training_start=JAN,
        training_cutoff=MAR,
        threshold_grid=[Decimal("0.3"), Decimal("0.4"), Decimal("0.5")],
        min_accepts=5,
    )
    assert again.bundle.checksum == bundle.checksum
    wrong = bundle.model_dump(mode="python", exclude={"checksum"})
    wrong["feature_version"] = "candidate-v1"
    with pytest.raises(ValueError, match="uses features"):
        CfdModelFilter(ModelBundle.seal(**wrong), fx)
