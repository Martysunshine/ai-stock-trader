"""Research engine invariants (risk_and_validation.md §6, §9): conservative bar fills, USD 50
limits, loss pauses, splits, determinism and absence of lookahead."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader.accounting.fees import load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import load_policy
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.data.corporate_actions import SplitEvent
from trader.domain.candidates import TradeCandidate
from trader.domain.market import Bar
from trader.domain.types import BPS, Direction, Track, round_up_to_step
from trader.research.engine import (
    EngineConfig,
    EngineResult,
    FillAssumptions,
    InstrumentSetup,
    ResearchEngine,
)
from trader.strategies.research_rules import build_rule, load_rule_spec

REPO = Path(__file__).resolve().parents[1]
BTC, ETH = "KRAKEN:BTC/USD", "KRAKEN:ETH/USD"
T0 = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)  # a Monday
KRAKEN = load_instruments(REPO / "configs" / "instruments" / "kraken_spot_usd.yaml")
SYNTH = load_instruments(REPO / "configs" / "instruments" / "synthetic_equities.yaml")
FEES = load_fee_schedule(REPO / "configs" / "fees" / "kraken_spot_tier1_usd.yaml")
STOCK_FEES = load_fee_schedule(REPO / "configs" / "fees" / "replay_demo.yaml")
POLICY = load_policy(REPO / "configs" / "policy" / "base.yaml").policy
FILLS = FillAssumptions("base", Decimal("1"), Decimal("2"), Decimal("0.10"))


def mk(
    iid: str, start: datetime, o: str, h: str, low: str, c: str, v: str = "50", minutes: int = 15
) -> Bar:
    return Bar(
        instrument_id=iid,
        feed="TEST",
        minutes=minutes,
        start=start,
        end=start + timedelta(minutes=minutes),
        available_time=start + timedelta(minutes=minutes, seconds=1),
        complete=True,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(v),
        trade_count=1,
        vwap=Decimal(c),
    )


def at(i: int) -> datetime:
    return T0 + timedelta(minutes=15 * i)


class Scripted:
    """Emits pre-planned long candidates at given bar ends."""

    strategy_id = "scripted"
    spec_hash = "0" * 64
    uses_source_vwap = False

    def __init__(self, plans: dict[tuple[str, datetime], tuple[str, str, int, int]]) -> None:
        self.plans = plans  # (iid, bar_end) -> (stop, target, max_hold_minutes, ttl_seconds)
        self.splits: list[tuple[str, Decimal]] = []

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        plan = self.plans.get((bar.instrument_id, bar.end))
        if plan is None:
            return None
        stop, target, hold, ttl = plan
        return TradeCandidate(
            candidate_id=f"c-{bar.instrument_id}-{bar.end.isoformat()}",
            strategy_id="scripted",
            strategy_version=1,
            spec_hash="0" * 64,
            instrument_id=bar.instrument_id,
            track=Track.CRYPTO if bar.instrument_id.startswith("KRAKEN") else Track.STOCKS,
            direction=Direction.LONG,
            feed=bar.feed,
            bar_end=bar.end,
            decision_time=bar.available_time,
            reference_price=bar.close,
            stop_price=Decimal(stop),
            target_price=Decimal(target),
            max_hold_minutes=hold,
            entry_limit_offset_bps=Decimal(5),
            entry_ttl_seconds=ttl,
            gap_allowance_fraction=Decimal(10) * BPS,
            snapshot_hash="0" * 64,
            expires_at=bar.available_time + timedelta(seconds=60),
        )

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        self.splits.append((instrument_id, divisor))

    def data_ok(self, instrument_id: str) -> bool:
        return True


def run(
    bars: list[Bar],
    plans: dict[tuple[str, datetime], tuple[str, str, int, int]],
    *,
    fills: FillAssumptions = FILLS,
) -> EngineResult:
    setups = {
        iid: InstrumentSetup(spec=KRAKEN[iid], bars=[b for b in bars if b.instrument_id == iid])
        for iid in {b.instrument_id for b in bars}
    }
    cfg = EngineConfig(
        policy=POLICY,
        fees=FEES,
        fills=fills,
        track=Track.CRYPTO,
        calendar=Utc247Calendar(),
        start=T0,
        end=T0 + timedelta(days=30),
        loss_windows=("UTC_DAY", "UTC_WEEK"),
        session_basis="UTC_DAY",
    )
    return ResearchEngine(cfg, Scripted(plans), setups).run()


def flat(iid: str, i: int, price: str = "50000", v: str = "50") -> Bar:
    p = Decimal(price)
    return mk(iid, at(i), price, str(p + 5), str(p - 5), price, v)


PLAN = ("49500", "51000", 360, 120)


def test_entry_fills_at_the_next_open_plus_costs_never_at_the_signal_close() -> None:
    bars = [flat(BTC, 0), mk(BTC, at(1), "50010", "50020", "50000", "50015"), flat(BTC, 2)]
    r = run(bars, {(BTC, at(1)): PLAN})
    trade = r.trades[0]
    assert trade.entry.price == Decimal("50025.1")  # ceil_tick(50010 * 1.0003)
    assert trade.entry.price != Decimal("50000")  # the signal close
    expected_fee = round_up_to_step(
        Decimal("0.008") * trade.quantity * trade.entry.price, Decimal("0.0001")
    )
    assert trade.entry.fee == expected_fee
    assert trade.planned_loss_usd <= Decimal("0.125")
    assert trade.quantity * trade.entry.price <= Decimal(10)


def test_an_open_beyond_the_limit_is_a_missed_entry() -> None:
    bars = [flat(BTC, 0), mk(BTC, at(1), "50100", "50200", "50090", "50150"), flat(BTC, 2)]
    r = run(bars, {(BTC, at(1)): PLAN})
    assert not r.trades
    assert r.candidates[0].status == "UNFILLED"
    assert r.candidates[0].reasons == ("LIMIT_NOT_MARKETABLE_AT_NEXT_OPEN",)


def test_stop_takes_precedence_when_stop_and_target_touch_in_one_bar() -> None:
    bars = [flat(BTC, 0), flat(BTC, 1), mk(BTC, at(2), "50000", "51500", "49400", "50000")]
    r = run(bars, {(BTC, at(1)): PLAN})
    exit_fill = r.trades[0].exits[-1]
    assert exit_fill.reason == "STOP"
    assert exit_fill.price == Decimal("49485.1")  # floor_tick(49500 * 0.9997)


def test_gap_through_the_stop_fills_at_the_open_and_is_reported() -> None:
    bars = [
        flat(BTC, 0),
        flat(BTC, 1),
        flat(BTC, 2),
        mk(BTC, at(3), "49000", "49100", "48900", "49000"),
    ]
    r = run(bars, {(BTC, at(1)): PLAN})
    exit_fill = r.trades[0].exits[-1]
    assert exit_fill.reason == "STOP_GAP" and exit_fill.price == Decimal("48985.3")
    assert r.trades[0].net_pnl() < -r.trades[0].planned_loss_usd  # loss beyond the plan, honestly


def test_target_fills_only_when_the_estimated_bid_trades_through_it() -> None:
    touch = [
        flat(BTC, 0),
        flat(BTC, 1),
        flat(BTC, 2),
        mk(BTC, at(3), "50000", "51005", "49990", "50000"),
    ]
    assert run(touch, {(BTC, at(1)): PLAN}).trades[0].exits[-1].reason == "END_OF_DATA"
    through = [
        flat(BTC, 0),
        flat(BTC, 1),
        flat(BTC, 2),
        mk(BTC, at(3), "50000", "51010", "49990", "50000"),
    ]
    exit_fill = run(through, {(BTC, at(1)): PLAN}).trades[0].exits[-1]
    assert exit_fill.reason == "TARGET" and exit_fill.price == Decimal("51000")


def test_time_exit_happens_at_the_first_open_after_the_maximum_hold() -> None:
    bars = [flat(BTC, i) for i in range(7)]
    r = run(bars, {(BTC, at(1)): ("49500", "51000", 30, 120)})
    exit_fill = r.trades[0].exits[-1]
    assert exit_fill.reason == "TIME_EXIT"
    assert exit_fill.time == at(4)  # entry ~ at(2)+1.5 s, deadline +30 min -> next open at(4)


def test_participation_cap_gives_a_partial_entry_and_cancels_the_rest() -> None:
    bars = [flat(BTC, 0), flat(BTC, 1), flat(BTC, 2, v="0.0007"), flat(BTC, 3)]
    r = run(bars, {(BTC, at(2)): PLAN})
    assert r.candidates[0].status == "PARTIAL"
    assert r.trades[0].quantity == Decimal("0.00007")
    tiny = [flat(BTC, 0), flat(BTC, 1), flat(BTC, 2, v="0.0005"), flat(BTC, 3)]
    assert run(tiny, {(BTC, at(2)): PLAN}).candidates[0].reasons == ("PARTICIPATION_CAP",)


def test_a_missing_next_bar_expires_the_entry() -> None:
    bars = [
        flat(BTC, 0),
        flat(BTC, 1),
        flat(ETH, 1, "2000"),
        flat(ETH, 2, "2000"),
        flat(BTC, 3),
        flat(ETH, 3, "2000"),
    ]
    r = run(bars, {(BTC, at(2)): PLAN})
    assert not r.trades
    assert r.candidates[0].reasons == ("EXPIRED_NO_BAR_WITHIN_TTL",)


def test_daily_loss_breach_pauses_entries_until_the_next_utc_day() -> None:
    day2 = 96  # bars per day
    bars = [flat(BTC, i) for i in range(6)]
    bars.append(mk(BTC, at(6), "40000", "40100", "39900", "40000"))  # gap far through the stop
    bars += [flat(BTC, i, "40000") for i in range(7, 12)]
    bars += [flat(BTC, i, "40000") for i in range(day2, day2 + 4)]
    plans = {
        (BTC, at(1)): PLAN,
        (BTC, at(9)): ("39500", "41000", 360, 120),
        (BTC, at(day2 + 1)): ("39500", "41000", 360, 120),
    }
    r = run(bars, plans)
    assert r.trades[0].net_pnl() < Decimal("-0.50")
    assert [p["cause"] for p in r.pauses if "raised" in p] == ["UTC_DAY"]
    by_id = {c.candidate_id: c for c in r.candidates}
    assert by_id[f"c-{BTC}-{at(9).isoformat()}"].reasons == ("PAUSED",)
    assert by_id[f"c-{BTC}-{at(day2 + 1).isoformat()}"].status == "FILLED"


# ----------------------------------------------------------------------------- strategy runs


def random_walk(iid: str, start_price: float, n: int, seed: int) -> list[Bar]:
    rng = random.Random(seed)
    price = start_price
    out = []
    for i in range(n):
        o = price
        c = o * (1 + rng.gauss(0.0002, 0.004))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.001)))
        low = min(o, c) * (1 - abs(rng.gauss(0, 0.001)))
        out.append(
            mk(
                iid,
                at(i),
                f"{o:.2f}",
                f"{h:.2f}",
                f"{low:.2f}",
                f"{c:.2f}",
                f"{rng.uniform(5, 80):.4f}",
            )
        )
        price = c
    return out


def strategy_run(
    bars: list[Bar], spec: str, overrides: dict[str, str] | None = None
) -> EngineResult:
    rule = load_rule_spec(REPO / "src/trader/strategies/specs" / spec, overrides)
    setups = {
        iid: InstrumentSetup(spec=KRAKEN[iid], bars=[b for b in bars if b.instrument_id == iid])
        for iid in sorted({b.instrument_id for b in bars})
    }
    strategy = build_rule(rule, Utc247Calendar(), {i: s.spec.price_tick for i, s in setups.items()})
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
    return ResearchEngine(cfg, strategy, setups).run()


@pytest.fixture(scope="module")
def walk() -> list[Bar]:
    return random_walk(BTC, 50000, 4000, 1) + random_walk(ETH, 2500, 4000, 2)


@pytest.mark.parametrize("spec", ["candidate_a_crypto_v1.yaml", "candidate_b_crypto_v1.yaml"])
def test_usd_50_policy_holds_for_every_simulated_trade(walk: list[Bar], spec: str) -> None:
    zero = {"atr_stop_multiplier": "1.0"} if "a_crypto" in spec else None
    r = strategy_run(walk, spec, zero)
    assert r.candidates
    entries_per_day: dict[date, int] = {}
    for t in r.trades:
        assert t.entry.quantity * t.entry.price <= Decimal(10)
        assert t.planned_loss_usd <= Decimal("0.125")
        assert sum(f.quantity for f in t.exits) + t.dust == t.quantity  # never oversold
        d = t.entry.time.date()
        entries_per_day[d] = entries_per_day.get(d, 0) + 1
    assert all(v <= 2 for v in entries_per_day.values())
    assert sum(1 for c in r.candidates if c.status in ("FILLED", "PARTIAL")) == len(r.trades)


def test_rerun_is_identical_and_truncating_the_future_changes_no_past_decision(
    walk: list[Bar],
) -> None:
    full = strategy_run(walk, "candidate_b_crypto_v1.yaml")
    again = strategy_run(walk, "candidate_b_crypto_v1.yaml")
    assert full.result_hash == again.result_hash
    cutoff = at(2500)
    truncated = strategy_run([b for b in walk if b.start < cutoff], "candidate_b_crypto_v1.yaml")
    safe = cutoff - timedelta(days=1)

    def past(res: EngineResult) -> list[tuple[str, str, tuple[str, ...]]]:
        return [
            (c.candidate_id, c.status, c.reasons) for c in res.candidates if c.decision_time < safe
        ]

    assert past(full) == past(truncated)
    assert len(past(full)) > 0


# ----------------------------------------------------------------------------- equities


def stock_bar(iid: str, start: datetime, price: str, v: str = "100000") -> Bar:
    p = Decimal(price)
    return mk(
        iid, start, price, str(p + Decimal("0.05")), str(p - Decimal("0.05")), price, v, minutes=5
    )


def stock_engine(
    bars: list[Bar],
    plans: dict[tuple[str, datetime], tuple[str, str, int, int]],
    splits: list[SplitEvent],
) -> EngineResult:
    cal = ExchangeCalendar("XNYS")
    setups = {
        "XNYS:SYNB": InstrumentSetup(spec=SYNTH["XNYS:SYNB"], bars=bars, splits=tuple(splits)),
    }
    cfg = EngineConfig(
        policy=POLICY,
        fees=STOCK_FEES,
        fills=FILLS,
        track=Track.STOCKS,
        calendar=cal,
        start=datetime(2026, 11, 1, tzinfo=UTC),
        end=datetime(2026, 12, 1, tzinfo=UTC),
        loss_windows=("NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"),
        session_basis="XNYS",
    )
    return ResearchEngine(cfg, Scripted(plans), setups).run()


def test_a_position_held_over_a_split_is_rescaled_not_booked_as_a_loss() -> None:
    iid = "XNYS:SYNB"
    friday = datetime(2026, 11, 13, 19, 0, tzinfo=UTC)  # 14:00 New York; data stops at 14:10
    bars = [stock_bar(iid, friday + timedelta(minutes=5 * i), "40.00") for i in range(3)]
    monday_open = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
    bars += [stock_bar(iid, monday_open + timedelta(minutes=5 * i), "20.00") for i in range(3)]
    split = SplitEvent(iid, date(2026, 11, 16), Decimal(2), Decimal(1))
    r = stock_engine(
        bars, {(iid, friday + timedelta(minutes=5)): ("39.50", "41.00", 60, 120)}, [split]
    )
    trade = r.trades[0]
    assert r.diagnostics["splits_applied"] == 1
    assert trade.exits[-1].reason == "SESSION_CLOSE"  # held overnight only because data stopped
    assert trade.quantity == trade.exits[-1].quantity  # doubled quantity sold in full
    assert trade.entry.price < Decimal("20.10")  # basis restated per new share
    assert abs(trade.gross_pnl()) < Decimal("0.05")  # no fictitious 50% loss


def test_a_split_invalidates_an_outstanding_entry_order() -> None:
    iid = "XNYS:SYNB"
    friday_close = datetime(2026, 11, 13, 20, 50, tzinfo=UTC)  # last bar of the session
    bars = [
        stock_bar(iid, friday_close - timedelta(minutes=5), "40.00"),
        stock_bar(iid, friday_close, "40.00"),
    ]
    monday_open = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
    bars += [stock_bar(iid, monday_open, "20.00")]
    split = SplitEvent(iid, date(2026, 11, 16), Decimal(2), Decimal(1))
    # an (artificially) long-lived order placed before the split must not survive it
    plans = {(iid, friday_close + timedelta(minutes=5)): ("39.50", "41.00", 60, 4 * 86400)}
    r = stock_engine(bars, plans, [split])
    assert not r.trades
    assert r.candidates[0].reasons == ("ORDER_INVALIDATED_BY_SPLIT",)
