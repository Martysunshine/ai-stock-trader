"""The practice trader (ADR-0056) against a fake demo broker: a signal becomes PLAN, order and
OPEN; a restart sends nothing twice; the flat hour, the broker's stop and a lost confirmation
are journalled; an unknown broker position stops entries; a dry run writes nothing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.capital_fakes import Clock, FakeBroker, orders_for
from trader.data.sources.capital_prices import CapitalPrices
from trader.practice.journal import VENUES, PracticeJournal, load_rules
from trader.practice.trader import (
    PracticeTrader,
    Running,
    TraderError,
    TraderLock,
    load_running,
    load_trader_setups,
    next_run,
    trade_id_for,
)

HOUR = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
KEY = "crypto_hourly_breakout_v1"


def _row(start: datetime, o: str, h: str, lo: str, c: str, half: str = "5") -> dict[str, Any]:
    def side(p: str) -> dict[str, Decimal]:
        return {"bid": Decimal(p) - Decimal(half), "ask": Decimal(p) + Decimal(half)}

    return {
        "snapshotTimeUTC": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "openPrice": side(o),
        "highPrice": side(h),
        "lowPrice": side(lo),
        "closePrice": side(c),
        "lastTradedVolume": 100,
    }


def hourly_rows(end: datetime, *, last_close: str, count: int = 60) -> list[dict[str, Any]]:
    """``count`` complete hours ending at ``end`` inside 73800..74200, the last one closing at
    ``last_close``, then the hour still forming."""
    rows = []
    for n in range(count, 0, -1):
        start = end - timedelta(hours=n)
        if n == 1:
            rows.append(_row(start, "74000", max("74200", last_close), "73950", last_close))
        else:
            rows.append(_row(start, "74000", "74200", "73800", "74050"))
    rows.append(_row(end, last_close, last_close, last_close, last_close))  # forming
    return rows


@pytest.fixture
def hourly() -> Running:
    [setup] = [s for s in load_trader_setups() if s.key == KEY]
    return load_running(setup)


def make(
    tmp_path: Path, hourly: Running, *, live: bool = True, at: datetime | None = None
) -> tuple[PracticeTrader, FakeBroker, dict[str, PracticeJournal]]:
    clock = Clock(at or HOUR + timedelta(seconds=20))
    broker = FakeBroker(clock)
    orders = orders_for(broker)
    journals = {v: PracticeJournal(tmp_path / f"{v}.jsonl", venue=v, clock=clock) for v in VENUES}
    trader = PracticeTrader(
        prices=orders._prices,
        orders=orders,
        clock=clock,
        setups=[hourly],
        journals=journals,
        live=live,
        state_dir=tmp_path / "state",
        echo=lambda _: None,
    )
    return trader, broker, journals


def breakout(broker: FakeBroker, epic: str = "BTCUSD", close: str = "74500") -> None:
    broker.bars[epic] = hourly_rows(HOUR, last_close=close)
    broker.bars.setdefault("ETHUSD", [])
    broker.quotes[epic] = (Decimal(close) + 15, Decimal(close) + 25)


def test_the_setups_name_registered_trials_and_their_study_files() -> None:
    setups = {s.key: s for s in load_trader_setups()}
    assert setups["weekly_momentum_v1"].status == "tested"
    assert setups[KEY].status == "forward_test" and setups[KEY].loss_cap_usd == 5
    assert setups["index_dip_v1"].enabled and setups["index_dip_v1"].trial == "T-0018"
    for key, trial in (("weekly_momentum_v1", "T-0019"), (KEY, "T-0023")):
        run = load_running(setups[key])
        assert setups[key].trial == trial
        assert run.setup.label == trial and run.setup.study is not None
        assert set(run.instruments) == {"BTCUSD", "ETHUSD"}
        assert run.policy.exposure.risk_per_trade_fraction == load_rules("crypto-cfd").risk_fraction


def test_a_breakout_becomes_plan_order_and_open(tmp_path: Path, hourly: Running) -> None:
    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    decisions = trader.decide(HOUR)
    opened = [d for d in decisions if d.outcome == "OPENED"]
    assert len(opened) == 1 and opened[0].epic == "BTCUSD" and opened[0].direction == "BUY"
    j = journals["crypto-cfd"]
    assert [e["event"] for e in j.events] == ["PLAN", "NOTE", "OPEN"]
    plan = j.events[0]["payload"]
    assert plan["setup_key"] == KEY and plan["reference"] == "74500"
    assert plan["trade_id"] == trade_id_for(KEY, "BTCUSD", HOUR)
    assert all(e["actor"] == "trader" for e in j.events)
    [(method, _, body)] = broker.sent
    assert method == "POST" and body is not None
    assert body["stopLevel"] == plan["stop"] and body["profitLevel"] == plan["target"]
    assert Decimal(plan["stop"]) < 74500 < Decimal(plan["target"])
    assert Decimal(plan["risk_usd"]) <= Decimal("1")  # 1% of the USD 100 sleeve
    # the same hour again (a restart): nothing is sent twice
    again = trader.decide(HOUR)
    assert {d.outcome for d in again if d.epic == "BTCUSD"} <= {"DONE", "GROUP_BUSY", "BUSY"}
    assert len(broker.sent) == 1


def test_the_flat_hour_closes_the_trade_at_the_broker(tmp_path: Path, hourly: Running) -> None:
    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    trader.decide(HOUR)
    trader.close_due(HOUR + timedelta(hours=5))
    assert journals["crypto-cfd"].open_trades() != []  # not yet: flat at 21:00 UTC
    broker.clock.t = HOUR.replace(hour=21) + timedelta(seconds=20)
    trader.close_due(broker.clock.t)
    j = journals["crypto-cfd"]
    close = j.events[-1]
    assert close["event"] == "CLOSE" and close["payload"]["reason"] == "TIME"
    assert j.open_trades() == [] and broker.positions == {}


def test_a_stop_hit_at_the_broker_is_journalled_at_its_level(
    tmp_path: Path, hourly: Running
) -> None:
    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    trader.decide(HOUR)
    [deal] = broker.positions
    stop = broker.positions[deal]["position"]["stopLevel"]
    broker.close(deal, "SL", level=stop)
    trader.poll()
    close = journals["crypto-cfd"].events[-1]["payload"]
    assert close["reason"] == "STOP" and Decimal(close["level"]) == stop
    assert Decimal(close["pnl_usd"]) < 0


def test_a_lost_confirmation_is_found_by_its_position(tmp_path: Path, hourly: Running) -> None:
    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    broker.lose_confirms = True
    [d] = [d for d in trader.decide(HOUR) if d.epic == "BTCUSD"]
    assert d.outcome == "OPENED"
    assert journals["crypto-cfd"].open_trades() == [d.trade_id]
    assert len(broker.sent) == 1


def test_a_rejected_order_is_cancelled(tmp_path: Path, hourly: Running) -> None:
    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    broker.reject = True
    [d] = [d for d in trader.decide(HOUR) if d.epic == "BTCUSD"]
    assert d.outcome == "REJECTED"
    assert [e["event"] for e in journals["crypto-cfd"].events] == ["PLAN", "NOTE", "CANCEL"]


def test_a_lost_order_answer_is_never_resent_and_the_restart_settles_it(
    tmp_path: Path, hourly: Running
) -> None:
    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    broker.fail_send = True
    [d] = [d for d in trader.decide(HOUR) if d.epic == "BTCUSD"]
    assert d.outcome == "ORDER_UNKNOWN" and trader.entries_paused
    assert len(broker.sent) == 1
    # at the restart the PLAN has no order reference: cancelled, nothing sent
    trader.reconcile()
    assert journals["crypto-cfd"].events[-1]["event"] == "CANCEL"
    assert trader.entries_paused is None and len(broker.sent) == 1


def test_a_position_no_journal_knows_stops_new_entries(tmp_path: Path, hourly: Running) -> None:
    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    orders_for(broker).open_position(
        "ETHUSD", "SELL", Decimal("0.01"), stop=Decimal("2100"), target=None
    )
    trader.reconcile()
    assert trader.entries_paused and "ETHUSD SELL" in trader.entries_paused
    decisions = trader.decide(HOUR)
    assert [d.outcome for d in decisions if d.epic == "BTCUSD"] == ["PAUSED"]
    assert journals["crypto-cfd"].events == []


def test_hedging_off_keeps_one_position_per_epic(tmp_path: Path, hourly: Running) -> None:
    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    # a chat's own BTCUSD trade, journalled by hand
    hand = journals["crypto-cfd"]
    plan = {
        "trade_id": "M1",
        "epic": "BTCUSD",
        "direction": "SELL",
        "size": "0.0001",
        "entry": "74000",
        "stop": "75000",
        "target": "72000",
        "thesis": "a hand trade held by a chat",
        "setup": "manual",
        "timeframe": "1d",
        "invalidation": "above 75000",
    }
    hand.append("PLAN", plan, broker.clock.now())
    ref = orders_for(broker).open_position(
        "BTCUSD", "SELL", Decimal("0.0001"), stop=Decimal("75000"), target=Decimal("72000")
    )
    deal = broker.confirms[ref]["affectedDeals"][0]["dealId"]
    hand.append(
        "OPEN",
        {"trade_id": "M1", "deal_id": deal, "fill_level": "74000", "size": "0.0001"},
        broker.clock.now(),
    )
    trader.reconcile()
    assert trader.entries_paused is None
    [d] = [d for d in trader.decide(HOUR) if d.epic == "BTCUSD"]
    assert d.outcome == "HEDGING_OFF"
    broker.hedging = True
    trader.reconcile()
    [d] = [d for d in trader.decide(HOUR) if d.epic == "BTCUSD"]
    assert d.outcome == "OPENED"


def test_a_missing_hour_or_no_break_trades_nothing(tmp_path: Path, hourly: Running) -> None:
    trader, broker, _journals = make(tmp_path, hourly)
    # the broker has not published the hour ending at HOUR yet: the newest bar ends an hour ago
    broker.bars["BTCUSD"] = hourly_rows(HOUR - timedelta(hours=1), last_close="74500")[:-1]
    broker.bars["ETHUSD"] = hourly_rows(HOUR, last_close="74050")
    trader.reconcile()
    outcomes = {d.epic: d.outcome for d in trader.decide(HOUR)}
    assert outcomes == {"BTCUSD": "NO_BAR", "ETHUSD": "NO_SIGNAL"}
    assert broker.sent == []


def test_a_dry_run_decides_but_writes_and_sends_nothing(tmp_path: Path, hourly: Running) -> None:
    trader, broker, journals = make(tmp_path, hourly, live=False)
    breakout(broker)
    trader.reconcile()
    [d] = [d for d in trader.decide(HOUR) if d.epic == "BTCUSD"]
    assert d.outcome == "DRY_RUN" and "would BUY" in d.detail
    assert broker.sent == [] and journals["crypto-cfd"].events == []


def test_a_forward_test_below_its_loss_cap_takes_no_entry(tmp_path: Path, hourly: Running) -> None:
    trader, broker, _journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    trader.decide(HOUR)
    [deal] = broker.positions
    broker.close(deal, "SL", level=Decimal("60000"))  # a 0.001 BTC trade losing about 14 USD
    trader.poll()
    result, trades = trader.forward_result(hourly)
    assert trades == 1 and result < -5
    later = HOUR + timedelta(hours=1)
    broker.bars["BTCUSD"] = hourly_rows(later, last_close="74500")
    broker.clock.t = later + timedelta(seconds=20)
    [d] = [d for d in trader.decide(later) if d.epic == "BTCUSD"]
    assert d.outcome == "SWITCHED_OFF"


def test_the_state_file_and_the_decision_log(tmp_path: Path, hourly: Running) -> None:
    trader, broker, _ = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    trader.write_state(broker.clock.now(), trader.decide(HOUR))
    state = json.loads((tmp_path / "state" / "trader.json").read_text(encoding="utf-8"))
    assert state["live"] and state["next_decision_at"] == "2026-11-12T11:00:20Z"
    [setup] = state["setups"]
    assert setup["key"] == KEY and setup["status"] == "forward_test"
    assert setup["last_decision"]["epic"] in ("BTCUSD", "ETHUSD")
    logged = (tmp_path / "state" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["outcome"] == "OPENED" for line in logged)


def test_next_run_is_twenty_seconds_after_the_hour() -> None:
    assert next_run(HOUR) == HOUR + timedelta(seconds=20)
    assert next_run(HOUR + timedelta(seconds=21)) == HOUR + timedelta(hours=1, seconds=20)


def test_one_trader_at_a_time(tmp_path: Path) -> None:
    clock = Clock(HOUR)
    state = tmp_path / "state"
    state.mkdir()
    (state / "trader.json").write_text(
        json.dumps({"updated_at": "2026-11-12T09:59:00Z"}), encoding="utf-8"
    )
    with TraderLock(state, clock), pytest.raises(TraderError, match="another practice trader"):
        TraderLock(state, clock).__enter__()
    clock.t = HOUR + timedelta(hours=1)  # a lock left by a trader whose heartbeat is old
    (state / "trader.lock").write_text("{}", encoding="utf-8")
    with TraderLock(state, clock):
        pass


def test_the_order_path_reuses_one_logged_in_session(tmp_path: Path, hourly: Running) -> None:
    trader, broker, _ = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    trader.decide(HOUR)
    prices = trader.prices
    assert isinstance(prices, CapitalPrices) and prices.logins == 1


def test_the_weekly_rule_decides_on_monday_from_daily_bars_built_from_hours(
    tmp_path: Path,
) -> None:
    """T-0019 through the trader: on Monday 00:00 UTC the week that just ended decides; the
    trade is closed a week later, before the next decision."""
    [setup] = [s for s in load_trader_setups() if s.key == "weekly_momentum_v1"]
    weekly = load_running(setup)
    monday = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
    assert monday.weekday() == 0
    trader, broker, journals = make(tmp_path, weekly, at=monday + timedelta(seconds=20))
    rows = []
    for n in range(999, -1, -1):  # 1000 complete hours rising about 1 USD an hour, then forming
        start = monday - timedelta(hours=n + 1)
        level = Decimal(73000) + (999 - n)
        rows.append(_row(start, str(level), str(level + 30), str(level - 30), str(level + 1)))
    rows.append(_row(monday, "74001", "74001", "74001", "74001"))
    broker.bars["BTCUSD"] = rows
    broker.bars["ETHUSD"] = []
    broker.quotes["BTCUSD"] = (Decimal("74010"), Decimal("74020"))
    trader.reconcile()
    # Sunday 23:00: the week has not ended, so the rule reads Saturday's session and waits
    early = trader.decide(monday - timedelta(hours=1))
    assert {d.outcome for d in early} <= {"NO_SIGNAL", "NO_BAR"} and broker.sent == []
    decisions = trader.decide(monday)
    [btc] = [d for d in decisions if d.epic == "BTCUSD"]
    assert btc.outcome == "OPENED" and btc.direction == "BUY", btc
    plan = journals["crypto-cfd"].events[0]["payload"]
    assert plan["setup_key"] == "weekly_momentum_v1" and plan["timeframe"] == "1d"
    assert plan["max_hold_hours"] == 168
    later = monday + timedelta(days=7)
    trader.close_due(later - timedelta(minutes=1))
    assert journals["crypto-cfd"].open_trades() != []
    broker.clock.t = later + timedelta(seconds=20)
    trader.close_due(broker.clock.t)
    assert journals["crypto-cfd"].events[-1]["payload"]["reason"] == "TIME"


def test_the_forward_ledger_counts_each_setups_trades_against_its_cap(
    tmp_path: Path, hourly: Running
) -> None:
    from trader.practice.forward import ledger, render

    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    trader.decide(HOUR)
    [deal] = broker.positions
    broker.close(deal, "TP", level=broker.positions[deal]["position"]["profitLevel"])
    trader.poll()
    setups = [s.model_dump(mode="json") for s in load_trader_setups()]
    doc = ledger(journals, setups, broker.clock.now(), repo_root=tmp_path)
    row = next(r for r in doc["setups"] if r["key"] == KEY)
    assert row["trades"] == 1 and row["wins"] == 1 and Decimal(row["net_usd"]) > 0
    assert row["by_reason"] == {"TARGET": 1} and not row["switched_off"]
    assert row["league"] == "COLLECTING" and row["league_note"] == "1 of 60 trades"
    assert row["trial_baseline"] is None  # no trial results under tmp_path
    weekly = next(r for r in doc["setups"] if r["key"] == "weekly_momentum_v1")
    assert weekly["trades"] == 0 and weekly["loss_cap_usd"] is None
    assert any(
        "crypto_hourly_breakout_v1 (forward test, T-0023) COLLECTING, 1 of 60 trades: 1 closed"
        in line
        for line in render(doc)
    )


def _index_hours(friday: datetime) -> list[dict[str, Any]]:
    """About 300 weekday sessions of an index rising one point a session, quoted Sunday 23:00 to
    Friday 22:00 UTC, then three sessions that each fall 25 points, the last one on ``friday``:
    a close above its 200-day average with a two-day RSI near zero, the index dip's signal."""
    sessions: list[datetime] = []
    day = friday
    while len(sessions) < 300:
        if day.weekday() < 5:
            sessions.append(day)
        day -= timedelta(days=1)
    sessions.reverse()
    rows: list[dict[str, Any]] = []
    level = Decimal(5000)
    for n, session in enumerate(sessions):
        move = Decimal(-25) if n >= len(sessions) - 3 else Decimal(1)
        hours = ([session - timedelta(hours=1)] if session.weekday() == 0 else []) + [
            session + timedelta(hours=h) for h in range(22 if session.weekday() == 4 else 24)
        ]
        step = move / len(hours)
        for h in hours:
            o, c = level, level + step
            rows.append(_row(h, str(o), str(max(o, c) + 3), str(min(o, c) - 3), str(c), "0.2"))
            level = c
    return rows


def test_the_index_dip_enters_at_the_sunday_open_and_exits_on_its_own_signal(
    tmp_path: Path,
) -> None:
    [setup] = [s for s in load_trader_setups() if s.key == "index_dip_v1"]
    assert setup.enabled and setup.status == "forward_test" and setup.trial == "T-0018"
    dip = load_running(setup)
    assert set(dip.instruments) == {"US500", "US100", "US30", "RTY"}
    friday = datetime(2026, 11, 13, tzinfo=UTC)
    saturday = friday + timedelta(days=1)
    trader, broker, journals = make(tmp_path, dip, at=saturday + timedelta(seconds=20))
    broker.bars = {"US500": _index_hours(friday), "US100": [], "US30": [], "RTY": []}
    close = Decimal(broker.bars["US500"][-1]["closePrice"]["bid"]) + Decimal("0.2")
    broker.quotes["US500"] = (close - Decimal("0.2"), close + Decimal("0.2"))
    broker.min_size["US500"] = Decimal("0.01")
    broker.status["US500"] = "CLOSED"
    trader.reconcile()
    [sat] = [d for d in trader.decide(saturday) if d.epic == "US500"]
    assert sat.outcome == "CLOSED" and sat.direction == "BUY"  # Friday's signal, market shut
    assert broker.sent == []
    # Sunday 23:00 UTC: the market opens; the signal is still for this open
    sunday = datetime(2026, 11, 15, 23, 0, tzinfo=UTC)
    broker.clock.t = sunday + timedelta(seconds=20)
    broker.status["US500"] = "TRADEABLE"
    [sun] = [d for d in trader.decide(sunday) if d.epic == "US500"]
    assert sun.outcome == "OPENED", sun
    [(_, _, body)] = broker.sent
    assert body is not None and "profitLevel" not in body  # the dip's target is a cost gate
    j = journals["capital-demo"]
    plan = j.events[0]["payload"]
    assert plan["setup_key"] == "index_dip_v1" and plan["epic"] == "US500"
    assert plan["trade_id"] == "index_dip_v1.US500.20261113T2200Z"
    # Monday closes well above its 5-day average: closed at Tuesday's first open
    monday = sunday + timedelta(hours=1)
    rows = broker.bars["US500"]
    rows += [
        _row(
            monday + timedelta(hours=h),
            str(close + 2 * h),
            str(close + 2 * h + 3),
            str(close + 2 * h - 3),
            str(close + 2 * h + 2),
            "0.2",
        )
        for h in range(24)
    ]
    tuesday = monday + timedelta(hours=24)
    broker.clock.t = tuesday + timedelta(seconds=20)
    broker.quotes["US500"] = (close + 48, close + Decimal("48.4"))
    trader.decide(tuesday)
    close_event = j.events[-1]
    assert close_event["event"] == "CLOSE" and close_event["payload"]["reason"] == "SIGNAL"
    assert Decimal(close_event["payload"]["pnl_usd"]) > 0
    assert broker.positions == {}


# ---------------------------------------------------------------- practice setups (ADR-0060)
def _copper_rows(end: datetime) -> list[dict[str, Any]]:
    rows = [
        _row(end - timedelta(hours=n), "6.35", "6.37", "6.33", "6.35", "0.0001")
        for n in range(60, 1, -1)
    ]
    rows.append(_row(end - timedelta(hours=1), "6.36", "6.40", "6.35", "6.40", "0.0001"))
    rows.append(_row(end, "6.40", "6.40", "6.40", "6.40", "0.0001"))  # forming
    return rows


def test_practice_setups_name_their_own_files_and_no_trial() -> None:
    setups = {s.key: s for s in load_trader_setups()}
    commodity = setups["practice_commodity_breakout_v1"]
    assert commodity.status == "practice" and commodity.trial is None
    assert commodity.label == "practice" and commodity.loss_cap_usd == 5
    assert commodity.epics is not None and "COPPER" in commodity.epics
    run = load_running(commodity)
    assert run.hourly and run.calendar == "BROKER_CFD"
    assert run.policy.exposure.risk_per_trade_fraction == load_rules("capital-demo").risk_fraction
    crypto = load_running(setups["practice_crypto_breakout_v1"])
    assert set(crypto.instruments) == {"BTCUSD", "ETHUSD"} and crypto.calendar == "UTC_24_7"
    # a practice setup without its files, or with a trial, is refused
    from trader.practice.trader import TraderSetup

    good = commodity.model_dump()
    for bad in (
        {**good, "spec": None},
        {**good, "trial": "T-0021"},
        {**good, "loss_cap_usd": None},
    ):
        path = Path(tempfile_dir()) / "setups.yaml"
        path.write_text(
            json.dumps(
                {"trader_setups": [TraderSetup.model_validate(bad).model_dump(mode="json")]}
            ),
            encoding="utf-8",
        )
        with pytest.raises(TraderError):
            load_trader_setups(path)


def tempfile_dir() -> str:
    import tempfile

    return tempfile.mkdtemp()


def test_a_practice_commodity_break_trades_outside_the_tested_hours(tmp_path: Path) -> None:
    [setup] = [s for s in load_trader_setups() if s.key == "practice_commodity_breakout_v1"]
    practice = load_running(setup)
    hour = HOUR.replace(hour=19)  # after the tested rule's last decision hour (17:00 UTC)
    trader, broker, journals = make(tmp_path, practice, at=hour + timedelta(seconds=20))
    broker.bars = {epic: [] for epic in practice.instruments}
    broker.bars["COPPER"] = _copper_rows(hour)
    broker.quotes["COPPER"] = (Decimal("6.4000"), Decimal("6.4002"))
    broker.min_size["COPPER"] = Decimal("10")
    trader.reconcile()
    [d] = [d for d in trader.decide(hour) if d.epic == "COPPER"]
    assert d.outcome == "OPENED" and d.direction == "BUY", d
    plan = journals["capital-demo"].events[0]["payload"]
    assert plan["setup_key"] == "practice_commodity_breakout_v1"
    assert "practice" in plan["setup"] and plan["epic"] == "COPPER"
    assert Decimal("10") <= Decimal(plan["size"]) <= Decimal("15")
    [(_, _, body)] = broker.sent
    assert body is not None and "profitLevel" in body  # a day trade: stop and target at the broker
    broker.clock.t = hour.replace(hour=21) + timedelta(seconds=20)
    trader.close_due(broker.clock.t)
    assert journals["capital-demo"].events[-1]["payload"]["reason"] == "TIME"


def test_the_league_reads_a_setups_standing_by_rule() -> None:
    from trader.practice.forward import league_state

    row = {
        "enabled": True,
        "switched_off": False,
        "trades": 12,
        "net_usd": "0.40",
        "review_after_trades": None,
        "loss_cap_usd": "5",
    }
    assert league_state(row) == ("COLLECTING", "12 of 60 trades")
    assert league_state({**row, "enabled": False})[0] == "OFF"
    stopped = league_state({**row, "switched_off": True, "net_usd": "-5.20"})
    assert stopped[0] == "STOPPED" and "-5 USD" in stopped[1]
    # at the review count the sign of the net result decides, and zero is not a pass
    assert league_state({**row, "trades": 60})[0] == "CANDIDATE"
    assert league_state({**row, "trades": 60, "net_usd": "0.00"})[0] == "DROP"
    assert league_state({**row, "trades": 60, "net_usd": "-1.00"})[0] == "DROP"
    # a setup's own review count wins over the default
    assert league_state({**row, "trades": 30, "review_after_trades": 30})[0] == "CANDIDATE"


# ---------------------------------------------------------------- slots by family (ADR-0063)
def test_the_cfd_break_runs_as_three_family_setups_with_their_own_caps() -> None:
    setups = {s.key: s for s in load_trader_setups()}
    keys = [
        "practice_commodity_breakout_v1",
        "practice_energy_breakout_v1",
        "practice_macro_breakout_v1",
    ]
    families = [setups[k] for k in keys]
    assert {s.spec for s in families} == {
        "src/trader/strategies/specs/practice_cfd_hourly_breakout_v1.yaml"
    }
    epics = [e for s in families for e in (s.epics or ())]
    assert len(epics) == len(set(epics)) == 11  # no market in two families
    assert all(s.sleeve == "capital-demo" and s.loss_cap_usd == 5 and s.enabled for s in families)
    # the sleeve holds four trades: the index dip and three families fit
    assert load_rules("capital-demo").max_open_trades >= len(families) + 1


def _fill_the_crypto_sleeve(journal: PracticeJournal, now: datetime) -> None:
    """Four open trades of four setups (one by hand): the sleeve's limit."""
    for n, key in enumerate((None, "setup_one", "setup_two", "setup_three"), 1):
        plan: dict[str, Any] = {
            "trade_id": f"X{n}",
            "epic": "BTCUSD",
            "direction": "BUY",
            "size": "0.0001",
            "entry": "74000",
            "stop": "72500",
            "target": "77000",
            "thesis": "a trade that fills the sleeve for a test",
            "setup": "test",
            "timeframe": "1h",
            "invalidation": "below 72500",
        }
        if key is not None:
            plan["setup_key"] = key
        journal.append("PLAN", plan, now)
        journal.append(
            "OPEN",
            {"trade_id": f"X{n}", "deal_id": f"D{n}", "fill_level": "74000", "size": "0.0001"},
            now,
        )


def test_a_full_sleeve_refuses_before_any_order_is_sent(tmp_path: Path, hourly: Running) -> None:
    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    _fill_the_crypto_sleeve(journals["crypto-cfd"], broker.clock.now())
    [d] = [d for d in trader.decide(HOUR) if d.epic == "BTCUSD"]
    assert d.outcome == "SLEEVE_FULL", d
    assert broker.sent == [] and len(journals["crypto-cfd"].open_trades()) == 4


def test_a_fill_the_journal_refuses_is_closed_at_once(
    tmp_path: Path, hourly: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the journal refuses an OPEN after the broker filled the order, the position is closed
    and the journal says why: no position is ever held that nothing tracks."""
    from trader.practice.journal import JournalError

    trader, broker, journals = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    j = journals["crypto-cfd"]
    real = j.append

    def refuse(kind: str, payload: Mapping[str, Any], now: datetime, actor: str = "claude") -> Any:
        if kind == "OPEN":
            raise JournalError("test: refused")
        return real(kind, payload, now, actor)

    monkeypatch.setattr(j, "append", refuse)
    decisions = trader.decide(HOUR)
    [d] = [d for d in decisions if d.outcome == "ERROR"]  # one setup's failure, logged as such
    assert "test: refused" in d.detail and d.setup == "crypto_hourly_breakout_v1"
    assert broker.positions == {} and len(broker.sent) == 2  # the order, then its close
    last = j.events[-1]
    assert last["event"] == "CANCEL" and "refused the OPEN" in last["payload"]["reason"]
    assert j.open_trades() == [] and trader.entries_paused is None


def test_the_log_starts_a_new_file_each_utc_day(tmp_path: Path) -> None:
    from trader.practice.trader import day_logger

    clock = Clock(datetime(2026, 11, 11, 23, 59, 50, tzinfo=UTC))
    shown: list[str] = []
    say = day_logger(clock, tmp_path, shown.append)
    say("before midnight")
    clock.t += timedelta(seconds=20)
    say("after midnight")
    first = (tmp_path / "trader-20261111.log").read_text(encoding="utf-8")
    second = (tmp_path / "trader-20261112.log").read_text(encoding="utf-8")
    assert first == "2026-11-11 23:59:50Z before midnight\n"
    assert second == "2026-11-12 00:00:10Z after midnight\n"
    assert shown == ["2026-11-11 23:59:50Z before midnight", "2026-11-12 00:00:10Z after midnight"]


def test_an_unexpected_error_does_not_end_the_loop(
    tmp_path: Path, hourly: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flat-time close needs the loop: an error nobody foresaw is logged, and the next
    minute runs."""
    trader, broker, _ = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    lines: list[str] = []
    trader.echo = lines.append

    def boom() -> None:
        raise KeyError("something nobody foresaw")

    monkeypatch.setattr(trader, "poll", boom)
    trader.step()
    assert any("unexpected KeyError" in line for line in lines)
    assert "UNEXPECTED KeyError" in trader.errors[-1]
    monkeypatch.undo()
    trader.step()  # the next minute works, and the hour's decision is taken
    assert trader.last_decision_hour == HOUR and len(broker.sent) == 1


def test_a_state_file_the_console_holds_open_is_written_next_minute(
    tmp_path: Path, hourly: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    trader, broker, _ = make(tmp_path, hourly)
    breakout(broker)
    trader.reconcile()
    lines: list[str] = []
    trader.echo = lines.append

    def locked(*_: Any, **__: Any) -> None:
        raise PermissionError(13, "the file is in use")

    monkeypatch.setattr(trader, "write_state", locked)
    trader.step()
    assert any("state file not written" in line for line in lines)
