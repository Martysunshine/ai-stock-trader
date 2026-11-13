"""The CFD research simulator (ADR-0052): fills on the broker's own bid and ask, long and short,
stops, targets and time exits, nightly financing, the sleeve's limits and the cost views."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import pytest

from trader.config.policy import RiskPolicy
from trader.data.quote_bars import QuoteBar
from trader.domain.types import Direction
from trader.research.cfd_engine import (
    CfdCosts,
    CfdEngineConfig,
    CfdEngineResult,
    CfdInstrument,
    apply_costs,
    charge_instants,
    run_cfd_engine,
)

from .cfd_fixtures import (
    CFD_POLICY,
    CRYPTO_POLICY,
    FEES,
    MONDAY,
    ScriptedRule,
    day,
    flat_days,
    long,
    qbar,
    short,
    spec,
)

BASE = CfdCosts(label="base", slippage_bps=Decimal(1))
SLIP = Decimal("0.0001")


def up(x: Decimal, tick: str) -> Decimal:
    t = Decimal(tick)
    return (x / t).to_integral_value(rounding=ROUND_CEILING) * t


def down(x: Decimal, tick: str) -> Decimal:
    t = Decimal(tick)
    return (x / t).to_integral_value(rounding=ROUND_FLOOR) * t


def run(
    bars: Mapping[str, list[QuoteBar]],
    rule: ScriptedRule,
    *,
    policy: RiskPolicy = CFD_POLICY,
    costs: CfdCosts = BASE,
    start: datetime = MONDAY,
    end: datetime = MONDAY + timedelta(days=60),
    capital: str = "100",
    fx_bars: Mapping[str, tuple[QuoteBar, ...]] | None = None,
    fx_pairs: Mapping[str, str] | None = None,
) -> CfdEngineResult:
    instruments = {
        f"CAPITAL:{epic}": CfdInstrument(spec(epic), tuple(b), (fx_pairs or {}).get(epic))
        for epic, b in bars.items()
    }
    config = CfdEngineConfig(
        policy=policy,
        fees=FEES,
        instruments=instruments,
        costs=costs,
        start=start,
        end=end,
        capital=Decimal(capital),
        fx_bars=fx_bars or {},
    )
    return run_cfd_engine(config, rule)


def only(result: CfdEngineResult) -> dict[str, Any]:
    (trade,) = result.trades
    return trade.as_dict()


US100 = "CAPITAL:US100"


def test_a_long_buys_the_ask_at_the_next_open_and_sells_at_its_target() -> None:
    bars = [
        qbar(day(0), "20000", "20100", "19900", "20000"),
        qbar(day(1), "20010", "20500", "19950", "20300"),
        *flat_days(2, 3, "20300"),
    ]
    rule = ScriptedRule({(US100, day(0)): long("19500", "20400")})
    result = run({"US100": bars}, rule)
    t = only(result)
    entry = up(Decimal("20011") * (1 + SLIP), "0.1")  # the ask at the open, slipped, rounded up
    assert t["entry"]["price"] == entry == Decimal("20013.1")
    assert t["entry"]["time"] == "2026-01-06T00:00:00Z" and t["direction"] == "LONG"
    # planned loss per 0.001: 0.5131 at the stop + 0.1% gap allowance of 20.0131 -> 3 contracts
    assert t["quantity"] == Decimal("0.003") and t["planned_loss_usd"] <= 2
    assert t["exit_reason"] == "TARGET" and t["exits"][0]["price"] == Decimal("20400")
    gross = (Decimal("20400") - entry) * Decimal("0.003")
    notional = entry * Decimal("0.003")
    financing = Decimal("0.000222069") * notional  # held through Tuesday 22:00, once
    assert t["gross_pnl"] == gross and t["financing_usd"] == financing
    assert t["nights_charged"] == 1 and t["net_pnl"] == gross - financing
    assert t["execution_cost"] == (entry - Decimal("20010")) * Decimal("0.003")
    assert result.final_equity == 100 + gross - financing
    (outcome,) = result.candidates
    assert outcome.status == "FILLED" and outcome.decision_time == day(1)


def test_a_short_sells_the_bid_is_stopped_on_the_ask_and_earns_its_financing_credit() -> None:
    btc = "CAPITAL:BTCUSD"
    bars = [
        qbar(day(0), "40000", "40100", "39900", "40000", half="10"),
        qbar(day(1), "40000", "41100", "39800", "40900", half="10"),
        *flat_days(2, 3, "40900", half="10"),
    ]
    rule = ScriptedRule({(btc, day(0)): short("41000", "38000")})
    t = only(run({"BTCUSD": bars}, rule, policy=CRYPTO_POLICY))
    entry = down(Decimal("39990") * (1 - SLIP), "0.01")
    assert t["entry"]["price"] == entry and t["entry"]["side"] == "SELL"
    assert t["quantity"] == Decimal("0.0009")  # 1% of USD 100 at a 1,014-point stop
    stop_fill = up(Decimal("41000") * (1 + SLIP), "0.01")  # the ask reached the stop
    assert t["exit_reason"] == "STOP" and t["exits"][0]["price"] == stop_fill
    assert t["exits"][0]["side"] == "BUY"
    gross = -(stop_fill - entry) * Decimal("0.0009")
    credit = Decimal("-0.000136986") * entry * Decimal("0.0009")
    assert t["gross_pnl"] == gross < 0 and t["financing_usd"] == credit < 0
    assert t["net_pnl"] == gross - credit


def test_a_gap_through_the_stop_sells_at_the_open_and_both_touched_means_the_stop() -> None:
    gap = [
        qbar(day(0), "20000", "20050", "19950", "20000"),
        *flat_days(1, 1, "20000"),
        qbar(day(2), "19300", "19400", "19200", "19350"),
        *flat_days(3, 2, "19350"),
    ]
    rule = ScriptedRule({(US100, day(0)): long("19500", "20800")})
    t = only(run({"US100": gap}, rule))
    assert t["exit_reason"] == "STOP_GAP" and t["exits"][0]["time"] == "2026-01-07T00:00:00Z"
    assert t["exits"][0]["price"] == down(Decimal("19299") * (1 - SLIP), "0.1")

    both = [
        qbar(day(0), "20000", "20050", "19950", "20000"),
        qbar(day(1), "20000", "20900", "19400", "20100"),
        *flat_days(2, 2, "20100"),
    ]
    t = only(run({"US100": both}, ScriptedRule({(US100, day(0)): long("19500", "20800")})))
    assert t["exit_reason"] == "STOP" and t["ambiguous_bar"] and t["stopped_in_entry_bar"]


def test_the_time_exit_and_a_signal_exit_sell_at_an_open() -> None:
    bars = [qbar(day(0), "20000", "20050", "19950", "20000"), *flat_days(1, 12, "20000")]
    timed = ScriptedRule({(US100, day(0)): long("19500", "20800", max_hold_minutes=3 * 1440)})
    t = only(run({"US100": bars}, timed))
    assert t["exit_reason"] == "TIME_EXIT" and t["exits"][0]["time"] == "2026-01-09T00:00:00Z"
    assert t["nights_charged"] == 3 and t["holding_minutes"] == 3 * 1440

    signalled = ScriptedRule(
        {(US100, day(0)): long("19500", "20800", target_role="COST_GATE")},
        exits={(US100, day(2)): "close_above_sma_5"},
    )
    t = only(run({"US100": bars}, signalled))
    assert t["exit_reason"] == "SIGNAL:close_above_sma_5"
    assert t["exits"][0]["time"] == "2026-01-08T00:00:00Z"
    assert t["exits"][0]["price"] == down(Decimal("19999") * (1 - SLIP), "0.1")


def test_financing_counts_the_friday_night_three_times_unless_the_market_trades_daily() -> None:
    week = (MONDAY, MONDAY + timedelta(days=7))
    gold = charge_instants(spec("GOLD"), *week)
    assert [n for _, n in gold] == [1, 1, 1, 1, 3]
    assert gold[0][0] == MONDAY + timedelta(hours=22)
    assert sum(n for _, n in charge_instants(spec("BTCUSD"), *week)) == 7
    # a position that closes before 22:00 owes nothing; one held through the instant owes it
    assert charge_instants(spec("US500"), MONDAY, MONDAY + timedelta(hours=21)) == []
    assert len(charge_instants(spec("US500"), MONDAY, MONDAY + timedelta(hours=22))) == 1


def test_the_stressed_view_charges_more_credits_nothing_and_widens_the_spread() -> None:
    bars = [
        qbar(day(0), "40000", "40100", "39900", "40000", half="10"),
        *flat_days(1, 10, "40000", half="10"),
    ]
    rule = ScriptedRule(
        {("CAPITAL:BTCUSD", day(0)): short("41000", "38000", max_hold_minutes=4 * 1440)}
    )
    base = only(run({"BTCUSD": bars}, rule, policy=CRYPTO_POLICY))
    stressed = only(
        run(
            {"BTCUSD": bars},
            rule,
            policy=CRYPTO_POLICY,
            costs=CfdCosts(
                label="stressed",
                slippage_bps=Decimal(2),
                spread_multiplier=Decimal(2),
                financing_charge_multiplier=Decimal("1.5"),
                financing_credits=False,
            ),
        )
    )
    assert base["financing_usd"] < 0 and stressed["financing_usd"] == 0
    assert stressed["execution_cost"] > base["execution_cost"]
    assert stressed["net_pnl"] < base["net_pnl"]
    mid = only(
        run(
            {"BTCUSD": bars},
            rule,
            policy=CRYPTO_POLICY,
            costs=CfdCosts(
                label="diagnostic", slippage_bps=Decimal(0), mid_fills=True, financing=False
            ),
        )
    )
    assert mid["execution_cost"] == 0 and mid["financing_usd"] == 0
    assert mid["entry"]["price"] == Decimal("40000")


def test_a_zero_spread_bar_gets_the_floor_and_a_wide_spread_refuses_the_entry() -> None:
    flat = qbar(day(0), "100", "101", "99", "100", half="0")
    (floored,) = apply_costs([flat], BASE, Decimal(4))
    assert floored.ask.close - floored.bid.close == Decimal("0.04")  # 4 bps of 100
    assert floored.bid.low < floored.bid.open and not floored.ask.ohlc_problems()
    (quoted,) = apply_costs([qbar(day(0), "100", "101", "99", "100", half="0.5")], BASE, Decimal(4))
    assert quoted.ask.close - quoted.bid.close == 1  # the quoted spread is wider than the floor

    wide = [qbar(day(0), "20000", "20050", "19950", "20000"), *flat_days(1, 3, "20000", half="30")]
    result = run({"US100": wide}, ScriptedRule({(US100, day(0)): long("19500", "20800")}))
    assert result.trades == () and result.candidates[0].reasons == ("SPREAD_TOO_WIDE",)


def test_a_contract_too_large_for_the_limit_is_not_taken_and_the_stop_is_kept() -> None:
    bars = [qbar(day(0), "7500", "7520", "7480", "7500"), *flat_days(1, 3, "7500")]
    rule = ScriptedRule({("CAPITAL:US500", day(0)): long("7280", "7700")})
    result = run({"US500": bars}, rule)
    assert result.trades == ()
    (outcome,) = result.candidates
    assert outcome.status == "REJECTED" and outcome.reasons == ("RISK_LIMIT",)
    assert outcome.planned_loss_usd > 2 and "per_trade_risk" in outcome.binding
    beyond = run({"US500": bars}, ScriptedRule({("CAPITAL:US500", day(0)): long("7505", "7700")}))
    assert beyond.candidates[0].binding == "stop_beyond_open"


def test_one_trade_per_group_taken_by_priority() -> None:
    us100 = [qbar(day(0), "20000", "20050", "19950", "20000"), *flat_days(1, 5, "20000")]
    us500 = [
        qbar(day(0), "5000", "5010", "4990", "5000", half="0.2"),
        *flat_days(1, 5, "5000", half="0.2"),
    ]
    rule = ScriptedRule(
        {
            (US100, day(0)): long("19500", "20800", priority="1"),
            ("CAPITAL:US500", day(0)): long("4900", "5150", priority="2"),
        }
    )
    result = run({"US100": us100, "US500": us500}, rule)
    assert [t.instrument_id for t in result.trades] == ["CAPITAL:US500"]
    rejected = [c for c in result.candidates if c.status == "REJECTED"]
    assert [c.instrument_id for c in rejected] == [US100]
    assert rejected[0].binding.startswith("group:CAPITAL:US500@")


def test_signals_are_acted_on_only_inside_the_window_and_the_last_trade_is_closed() -> None:
    bars = [qbar(day(0), "20000", "20050", "19950", "20000"), *flat_days(1, 9, "20000")]
    rule = ScriptedRule(
        {
            (US100, day(0)): long(
                "19500", "20800"
            ),  # decided before the window, filled at its start
            (US100, day(8)): long("19500", "20800"),  # its open is the window's end: not taken
        }
    )
    result = run({"US100": bars}, rule, start=day(1), end=day(9))
    t = only(result)
    assert t["entry"]["time"] == "2026-01-06T00:00:00Z"
    assert t["exit_reason"] == "TIME_EXIT"
    sessions = [s.session for s in result.sessions]
    assert sessions[0] == "2026-01-06" and sessions[-1] == "2026-01-13"
    late = run({"US100": bars}, ScriptedRule({(US100, day(3)): long("19500", "20800")}), end=day(6))
    assert only(late)["exit_reason"] == "END_OF_DATA"


def test_a_window_below_its_floor_takes_no_new_entry() -> None:
    # a crash through the stop: the open gaps to 9,000 and the 0.003 contracts lose about USD 33
    bars = [
        qbar(day(0), "20000", "20050", "19950", "20000"),
        *flat_days(1, 1, "20000"),
        qbar(day(2), "9000", "9100", "8900", "9050"),
        *flat_days(3, 5, "9050"),
    ]
    rule = ScriptedRule(
        {
            (US100, day(0)): long("19500", "20800"),
            (US100, day(3)): long("8800", "9500"),
        }
    )
    result = run({"US100": bars}, rule)
    assert len(result.trades) == 1 and result.trades[0].exit_reason == "STOP_GAP"
    assert result.pauses[0]["reason"] == "capital_floor" and result.final_equity < 90
    assert result.candidates[-1].reasons == ("PAUSED",)


def test_a_euro_index_converts_at_the_rate_of_its_own_bar() -> None:
    de40 = [qbar(day(0), "20000", "20050", "19950", "20000"), *flat_days(1, 4, "20000")]
    fx = tuple(flat_days(0, 5, "1.1", half="0"))
    rule = ScriptedRule({("CAPITAL:DE40", day(0)): long("19500", "20800", max_hold_minutes=1440)})
    t = only(run({"DE40": de40}, rule, fx_bars={"EURUSD": fx}, fx_pairs={"DE40": "EURUSD"}))
    assert t["fx_entry"] == Decimal("1.1") and t["quantity"] == Decimal("0.003")
    assert t["notional_usd"] == t["entry"]["price"] * Decimal("0.003") * Decimal("1.1")
    no_rate = run({"DE40": de40}, rule, fx_pairs={"DE40": "EURUSD"})
    assert no_rate.candidates[0].binding == "fx"


def test_a_rerun_is_identical_and_the_future_does_not_change_the_past() -> None:
    bars = [qbar(day(0), "20000", "20050", "19950", "20000"), *flat_days(1, 30, "20000")]
    rule = {(US100, day(0)): long("19500", "20800", max_hold_minutes=2 * 1440)}
    later = {(US100, day(10)): long("19500", "20800", max_hold_minutes=2 * 1440)}
    a = run({"US100": bars}, ScriptedRule({**rule, **later}))
    b = run({"US100": bars}, ScriptedRule({**rule, **later}))
    assert a.result_hash == b.result_hash and len(a.trades) == 2
    cut = run({"US100": bars[:8]}, ScriptedRule({**rule, **later}))
    assert cut.trades[0].as_dict() == a.trades[0].as_dict()


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_the_equity_of_a_session_marks_open_positions_on_the_closing_side(
    direction: Direction,
) -> None:
    bars = [qbar(day(0), "20000", "20050", "19950", "20000"), *flat_days(1, 5, "20000")]
    params = long("19500", "20800") if direction is Direction.LONG else short("20500", "19200")
    result = run({"US100": bars}, ScriptedRule({(US100, day(0)): params}), end=day(4))
    first = next(s for s in result.sessions if s.session == "2026-01-06")
    t = result.trades[0]
    closing = Decimal("19999") if direction is Direction.LONG else Decimal("20001")
    expected = 100 + t.unrealised_usd(closing, Decimal(1)) - t.entry_fee_usd
    assert first.end_equity == expected - (t.financing_usd if t.nights_charged == 1 else 0)
