"""Daily bars in the research engine (risk_and_validation.md §6; strategy_and_profitability.md
§4-5; docs/decisions.md "Daily bars, overnight holds, exit signals and capital views").

A scripted overnight rule on hand-made session bars checks each opt-in rule declaration:

* a next-open entry fills at the next session's open, never at the signal close, and a missing
  next-session bar is a missed entry;
* overnight holding disables the session-close exit; the default INTRADAY rule is unchanged;
* ``exit_signal`` schedules a reducing exit at the next open (``SIGNAL:<reason>``);
* ``max_hold_sessions`` counts exchange sessions (holidays skipped, missing bars tolerated);
* gaps through the stop sell at the open, also on the entry bar;
* a candidate is admitted when its next open is inside the deployment window;
* ``target_role = COST_GATE`` keeps the target out of the exits;
* splits, early closes and determinism.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.daily_fixtures import (
    CALENDAR,
    Plan,
    ScriptedDailyRule,
    daily_history,
    daily_specs,
    session_bar,
    sessions,
)
from trader.accounting.fees import load_fee_schedule
from trader.config.policy import load_policy
from trader.data.calendars import Session
from trader.data.corporate_actions import SplitEvent
from trader.domain.market import Bar
from trader.domain.types import Track
from trader.research.engine import (
    EngineConfig,
    EngineResult,
    FillAssumptions,
    InstrumentSetup,
    ResearchEngine,
    RuleTiming,
)

REPO = Path(__file__).resolve().parents[1]
SPY, QQQ = "ARCX:SPY", "XNAS:QQQ"
SPECS = daily_specs()
FEES = load_fee_schedule(REPO / "configs" / "fees" / "alpaca_us_equities_v2.yaml")
POLICY = load_policy(REPO / "configs" / "policy" / "base.yaml").policy
FILLS = FillAssumptions("base", Decimal("2"), Decimal("2"), Decimal("0.10"))
JAN = {s.session_date.day: s for s in sessions(date(2019, 1, 1), date(2019, 2, 1))}
STOP, TARGET = Decimal("97"), Decimal("106")


def d(day: int) -> date:
    return date(2019, 1, day)


def flat(session: Session, price: str = "100", iid: str = SPY) -> Bar:
    p = Decimal(price)
    return session_bar(iid, session, p, p + 1, p - 1, p)


def bar(session: Session, o: str, h: str, low: str, c: str, iid: str = SPY) -> Bar:
    return session_bar(iid, session, Decimal(o), Decimal(h), Decimal(low), Decimal(c))


def january(skip: tuple[int, ...] = (), last: int = 31, **custom: Bar) -> list[Bar]:
    out = []
    for day, session in sorted(JAN.items()):
        if day in skip or day > last:
            continue
        out.append(custom.get(f"d{day}", flat(session)))
    return out


def run(
    bars: list[Bar],
    rule: object,
    *,
    start: datetime = datetime(2019, 1, 1, tzinfo=UTC),
    end: datetime = datetime(2019, 2, 1, tzinfo=UTC),
    splits: tuple[SplitEvent, ...] = (),
) -> EngineResult:
    ids = sorted({b.instrument_id for b in bars})
    setups = {
        iid: InstrumentSetup(
            spec=SPECS[iid],
            bars=[b for b in bars if b.instrument_id == iid],
            splits=tuple(s for s in splits if s.instrument_id == iid),
        )
        for iid in ids
    }
    cfg = EngineConfig(
        policy=POLICY,
        fees=FEES,
        fills=FILLS,
        track=Track.STOCKS,
        calendar=CALENDAR,
        start=start,
        end=end,
        loss_windows=("NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"),
        session_basis="XNYS",
    )
    return ResearchEngine(cfg, rule, setups).run()  # type: ignore[arg-type]


def plan(day: int, iid: str = SPY, **kw: object) -> dict[tuple[str, date], Plan]:
    return {(iid, d(day)): Plan(stop=STOP, target=TARGET, **kw)}  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- calendar


def test_session_table_agrees_with_the_exchange_calendar() -> None:
    day = date(2016, 1, 1)
    while day < date(2021, 1, 1):
        session = CALENDAR.session_for_date(day)
        assert CALENDAR.session_on(day) == session
        assert CALENDAR.sessions_after(day) == CALENDAR.next_session(day)
        day += timedelta(days=5)
    assert CALENDAR.sessions_after(d(17), 3).session_date == d(23)  # MLK day skipped
    ts = datetime(2019, 1, 22, 0, tzinfo=UTC)  # before the open of the 22nd
    assert CALENDAR.session_open_before(ts, 1) == JAN[18].open
    assert CALENDAR.session_open_before(ts, 3) == JAN[16].open
    with pytest.raises(ValueError):
        CALENDAR.sessions_after(d(17), 0)


# ----------------------------------------------------------------------------- declarations


def test_rule_declarations_default_to_todays_behaviour_and_are_validated() -> None:
    class Plain:
        pass

    timing = RuleTiming.of(Plain())
    assert timing == RuleTiming()  # INTRADAY, minutes, target exit, no exit signal
    assert not timing.overnight

    class FromSpec:
        class spec:  # mimics a frozen spec attribute
            holding = "OVERNIGHT"
            max_hold_sessions = 10
            target_role = "COST_GATE"

    timing = RuleTiming.of(FromSpec())
    assert timing.overnight and timing.max_hold_sessions == 10
    assert timing.target_role == "COST_GATE"
    for bad in (
        {"holding": "SWING"},
        {"max_hold_sessions": 0},
        {"max_hold_sessions": True},
        {"target_role": "LIMIT"},
    ):
        with pytest.raises(ValueError):
            RuleTiming.of(type("Bad", (), bad)())


# ----------------------------------------------------------------------------- entries


def test_a_next_open_entry_fills_at_the_next_session_open_never_at_the_signal_close() -> None:
    rule = ScriptedDailyRule(plan(3))
    r = run(january(last=11), rule)
    (trade,) = r.trades
    assert trade.entry.time == JAN[4].open  # the next session's open, not the decision time
    assert trade.entry.price == Decimal("100.04")  # ceil_tick(100 x 1.0004)
    assert trade.entry.mid_reference == Decimal("100")
    assert trade.entry_session == "2019-01-04"
    assert [f.reason for f in trade.exits] == ["END_OF_DATA"]  # held overnight, no session exit
    assert r.candidates[0].status == "FILLED"
    assert trade.planned_loss_usd <= Decimal("0.125")


def test_the_default_intraday_rule_on_daily_bars_behaves_as_before() -> None:
    # entry TTL of 60 s: the next session's bar is long after it, so the entry expires
    r = run(january(last=11), ScriptedDailyRule(plan(3), holding="INTRADAY"))
    assert not r.trades and r.candidates[0].reasons == ("EXPIRED_NO_BAR_WITHIN_TTL",)
    # a TTL spanning the night: filled in the next bar, then the session-close exit at the open
    r = run(january(last=11), ScriptedDailyRule(plan(3, ttl_seconds=4 * 86400), holding="INTRADAY"))
    (trade,) = r.trades
    assert trade.exits[-1].reason == "SESSION_CLOSE" and trade.exits[-1].time == JAN[7].open


def test_a_missing_next_session_bar_is_a_missed_entry_not_a_later_fill() -> None:
    r = run(january(skip=(4,), last=11), ScriptedDailyRule(plan(3)))
    assert not r.trades
    assert r.candidates[0].status == "UNFILLED"
    assert r.candidates[0].reasons == ("EXPIRED_NO_BAR_AT_NEXT_OPEN",)


def test_a_candidate_is_admitted_when_its_next_open_is_inside_the_window() -> None:
    plans = {**plan(3), **plan(4), **plan(11)}
    rule = ScriptedDailyRule(plans)
    start, end = datetime(2019, 1, 7, tzinfo=UTC), datetime(2019, 1, 14, tzinfo=UTC)
    r = run(january(last=11), rule, start=start, end=end)
    # 3rd: next open 4th (before the window); 4th: next open 7th (inside); 11th: next open the
    # 14th, outside - it belongs to the next window
    assert [c.candidate_id for c in r.candidates] == [f"c-{SPY}-2019-01-04"]
    assert r.trades[0].entry.time == JAN[7].open
    assert r.diagnostics["candidates"] == 1


# ----------------------------------------------------------------------------- exits


def test_an_exit_signal_sells_at_the_next_open_and_the_first_reason_wins() -> None:
    rule = ScriptedDailyRule(plan(3), {(SPY, d(7)): "close_above_sma", (SPY, d(8)): "later"})
    custom = {"d9": bar(JAN[9], "101", "102", "100", "101")}
    r = run(january(last=11, **custom), rule)
    (trade,) = r.trades
    exit_fill = trade.exits[-1]
    assert exit_fill.reason == "SIGNAL:close_above_sma"
    assert exit_fill.time == JAN[8].open
    assert exit_fill.price == Decimal("99.96")  # floor_tick(100 x 0.9996)
    assert sum(f.quantity for f in trade.exits) == trade.quantity  # reducing, never more
    # called after every completed bar while held, the entry bar included; never when flat
    assert rule.signal_calls == [(SPY, d(4)), (SPY, d(7))]
    assert r.diagnostics["exit_signals"] == 1


def test_rules_without_exit_signal_are_not_asked_and_report_no_signal_counter() -> None:
    rule = ScriptedDailyRule(plan(3), {(SPY, d(7)): "ignored"}, with_exit_signal=False)
    r = run(january(last=11), rule)
    assert rule.signal_calls == [] and "exit_signals" not in r.diagnostics
    assert r.trades[0].exits[-1].reason == "END_OF_DATA"


def test_max_hold_counts_exchange_sessions_and_ignores_minutes() -> None:
    # entry Thursday 17th; sessions after it: 18th, 22nd (21st is a holiday), 23rd
    rule = ScriptedDailyRule(plan(16, max_hold_minutes=60), max_hold_sessions=3)
    r = run(january(), rule)
    exit_fill = r.trades[0].exits[-1]
    assert r.trades[0].entry.time == JAN[17].open
    assert exit_fill.reason == "TIME_EXIT" and exit_fill.time == JAN[23].open
    # a missing bar at the deadline: the time exit happens at the next available open
    r = run(january(skip=(23,)), ScriptedDailyRule(plan(16), max_hold_sessions=3))
    assert r.trades[0].exits[-1].reason == "TIME_EXIT"
    assert r.trades[0].exits[-1].time == JAN[24].open


def test_a_gap_through_the_stop_sells_at_the_open() -> None:
    custom = {"d7": bar(JAN[7], "95", "96", "94", "95.5")}
    r = run(january(last=11, **custom), ScriptedDailyRule(plan(3)))
    exit_fill = r.trades[0].exits[-1]
    assert exit_fill.reason == "STOP_GAP" and exit_fill.time == JAN[7].open
    assert exit_fill.price == Decimal("94.96")  # floor_tick(95 x 0.9996), below the stop
    assert r.trades[0].net_pnl() < -r.trades[0].planned_loss_usd  # reported honestly


def test_an_entry_bar_that_opens_through_the_stop_is_sold_at_that_open() -> None:
    custom = {"d4": bar(JAN[4], "96", "99", "95", "98")}
    r = run(january(last=11, **custom), ScriptedDailyRule(plan(3)))
    trade = r.trades[0]
    assert trade.entry.price == Decimal("96.04") and trade.entry.time == JAN[4].open
    assert trade.exits[-1].reason == "STOP_GAP" and trade.exits[-1].price == Decimal("95.96")
    assert trade.exits[-1].time == JAN[4].open and trade.stopped_in_entry_bar


def test_the_stop_inside_a_later_bar_and_the_target_exit_still_apply() -> None:
    stop_bar = {"d8": bar(JAN[8], "99", "100", "96.5", "98")}
    r = run(january(last=11, **stop_bar), ScriptedDailyRule(plan(3)))
    assert r.trades[0].exits[-1].reason == "STOP" and r.trades[0].exits[-1].time == JAN[8].close
    through = {"d8": bar(JAN[8], "100", "108", "99.5", "107")}
    r = run(january(last=11, **through), ScriptedDailyRule(plan(3)))
    assert r.trades[0].exits[-1].reason == "TARGET" and r.trades[0].exits[-1].price == TARGET


def test_a_cost_gate_target_is_never_an_exit() -> None:
    through = {"d8": bar(JAN[8], "100", "108", "99.5", "107")}
    rule = ScriptedDailyRule(plan(3), {(SPY, d(9)): "close_above_sma"}, target_role="COST_GATE")
    r = run(january(last=11, **through), rule)
    trade = r.trades[0]
    assert trade.target == TARGET  # still recorded (it fed the cost-coverage gate)
    assert [f.reason for f in trade.exits] == ["SIGNAL:close_above_sma"]
    assert trade.exits[-1].time == JAN[10].open
    # the gate still refuses a target that cannot cover the round-trip costs
    tight = {(SPY, d(3)): Plan(stop=STOP, target=Decimal("100.10"))}
    r = run(january(last=11), ScriptedDailyRule(tight, target_role="COST_GATE"))
    assert not r.trades and r.candidates[0].status == "REJECTED"


def test_one_pending_entry_and_the_entry_limit_count_in_the_entry_session() -> None:
    bars = january(last=11) + [flat(s, "50", QQQ) for day, s in JAN.items() if day <= 11]
    plans = {**plan(3), (QQQ, d(3)): Plan(stop=Decimal("48"), target=Decimal("54"))}
    r = run(bars, ScriptedDailyRule(plans))
    by_id = {c.instrument_id: c for c in r.candidates}
    # both decided at the same close: the first (instrument order) takes the one pending slot
    assert by_id[SPY].status == "FILLED"
    assert by_id[QQQ].reasons == ("RISK_LIMIT",) and by_id[QQQ].binding == "pending_entry"


def test_a_next_open_fill_frees_the_pending_slot_before_its_sessions_decisions() -> None:
    # QQQ (sorted after SPY) is decided on the 3rd and fills at the open of the 4th; SPY's
    # decision at the close of the 4th must see the slot free although its bar sorts first
    bars = january(last=11) + [flat(s, "50", QQQ) for day, s in JAN.items() if day <= 11]
    plans = {(QQQ, d(3)): Plan(stop=Decimal("48"), target=Decimal("54")), **plan(4)}
    r = run(bars, ScriptedDailyRule(plans))
    assert [c.status for c in r.candidates] == ["FILLED", "FILLED"]
    assert [t.entry.time for t in r.trades] == [JAN[4].open, JAN[7].open]


# ----------------------------------------------------------------------------- data events


def test_a_position_held_over_a_split_is_rescaled_not_booked_as_a_loss() -> None:
    split = SplitEvent(SPY, d(8), Decimal(2), Decimal(1), "test")
    after = {f"d{day}": flat(JAN[day], "50") for day in JAN if day >= 8}
    rule = ScriptedDailyRule(plan(3))
    r = run(january(last=11, **after), rule, splits=(split,))
    trade = r.trades[0]
    assert r.diagnostics["splits_applied"] == 1 and rule.splits == [(SPY, Decimal(2))]
    assert trade.stop == STOP / 2 and trade.quantity == trade.exits[-1].quantity
    assert abs(trade.gross_pnl()) < Decimal("0.02")


def test_an_early_close_session_decides_at_its_close_and_enters_at_the_next_open() -> None:
    days = sessions(date(2018, 11, 19), date(2018, 12, 1))
    friday = next(s for s in days if s.session_date == date(2018, 11, 23))
    assert friday.is_early_close
    bars = [flat(s) for s in days]
    assert next(b for b in bars if b.start == friday.open).minutes == 210
    rule = ScriptedDailyRule({(SPY, friday.session_date): Plan(stop=STOP, target=TARGET)})
    r = run(
        bars,
        rule,
        start=datetime(2018, 11, 1, tzinfo=UTC),
        end=datetime(2018, 12, 1, tzinfo=UTC),
    )
    assert r.trades[0].entry.time == CALENDAR.sessions_after(date(2018, 11, 23)).open
    assert r.trades[0].entry.time == datetime(2018, 11, 26, 14, 30, tzinfo=UTC)


def test_reruns_are_identical_and_the_future_never_changes_a_past_decision() -> None:
    fixture = daily_history([SPY, QQQ], missing=(), splits=())
    plans = {
        (b.instrument_id, CALENDAR.session_date_of(b.start)): Plan(
            stop=b.close * Decimal("0.95"), target=b.close * Decimal("1.08")
        )
        for bars in fixture.bars.values()
        for b in bars[::9]
    }
    exits = {
        (b.instrument_id, CALENDAR.session_date_of(b.start)): "scripted"
        for bars in fixture.bars.values()
        for b in bars[4::9]
    }
    all_bars = [b for bars in fixture.bars.values() for b in bars]
    window = {"start": datetime(2016, 3, 1, tzinfo=UTC), "end": datetime(2017, 1, 1, tzinfo=UTC)}

    def go(bars: list[Bar]) -> EngineResult:
        rule = ScriptedDailyRule(plans, exits, max_hold_sessions=6)
        return run(bars, rule, **window)

    full = go(all_bars)
    assert full.result_hash == go(all_bars).result_hash
    assert len(full.trades) > 10
    assert {t.exits[-1].reason for t in full.trades} >= {"SIGNAL:scripted"}
    cutoff = datetime(2016, 9, 1, tzinfo=UTC)
    truncated = go([b for b in all_bars if b.start < cutoff])
    safe = cutoff - timedelta(days=5)

    def past(res: EngineResult) -> list[tuple[str, str, tuple[str, ...]]]:
        return [
            (c.candidate_id, c.status, c.reasons) for c in res.candidates if c.decision_time < safe
        ]

    assert past(full) == past(truncated) and past(full)
