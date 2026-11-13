"""The practice sleeves' daily setup scan (ADR-0049, ADR-0052): weekly momentum on the crypto
CFDs read on the market feed's daily bars, the trade it becomes under the sleeve's risk limit,
open trades against their stops and time stops, the file the console reads and the watch."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader.practice import journal as journal_mod
from trader.practice import setups
from trader.practice.journal import EpicRule, PracticeJournal, load_rules
from trader.practice.setups import Bar, WeeklyMomentumSpec, load_spec, read_epic, ticket
from trader.practice.watch import run_watch

ROOT = Path(__file__).resolve().parents[1]
SPEC = load_spec()
#: Monday 2 March 2026: day 6 of the series is a Sunday.
FIRST = date(2026, 3, 2)
BTC = EpicRule(
    name="Bitcoin",
    quote_currency="USD",
    decimals=2,
    min_size=Decimal("0.0001"),
    max_size=Decimal("0.0015"),
)


def _bars(closes: list[str], *, first: date = FIRST, spread: str = "300") -> list[Bar]:
    """One bar a day from ``first``, each ranging ``spread`` either side of its close."""
    half = Decimal(spread)
    return [
        Bar(first + timedelta(days=n), Decimal(c), Decimal(c) + half, Decimal(c) - half, Decimal(c))
        for n, c in enumerate(closes)
    ]


def _rising(days: int = 14, start: int = 70000, step: int = 200) -> list[str]:
    return [str(start + step * k) for k in range(days)]


def test_the_setup_is_the_registered_trial_rule() -> None:
    from trader.strategies.cfd_rules import load_cfd_spec

    trial = load_cfd_spec(ROOT / "src/trader/strategies/specs/cfd_crypto_weekly_momentum_v1.yaml")
    assert SPEC.sleeve == "crypto-cfd" and list(SPEC.epics) == ["BTCUSD", "ETHUSD"]
    assert SPEC.lookback_days == trial.lookback_days  # type: ignore[union-attr]
    assert SPEC.decision_weekday == trial.decision_weekday  # type: ignore[union-attr]
    assert SPEC.atr_period == trial.atr_period
    assert SPEC.stop_atr_multiplier == trial.stop_atr_multiplier
    assert SPEC.reward_risk == trial.reward_risk  # type: ignore[union-attr]
    assert SPEC.max_hold_hours == trial.max_hold_hours
    rules = load_rules(SPEC.sleeve)
    assert set(SPEC.epics) <= set(rules.epics) and not rules.long_only
    assert rules.max_hold_hours is not None and SPEC.max_hold_hours <= rules.max_hold_hours
    assert rules.max_reward_risk is not None and SPEC.reward_risk <= rules.max_reward_risk
    with pytest.raises(ValueError):
        WeeklyMomentumSpec.model_validate({**SPEC.model_dump(), "leverage": 20})


def test_the_atr_is_the_trials_wilder_average() -> None:
    from trader.data.calendars import Utc247Calendar
    from trader.domain.market import Bar as SessionBar
    from trader.features.daily import DailyFeatures, DailyWindows

    closes = [str(70000 + 150 * k + (k % 4) * 90) for k in range(30)]
    bars = _bars(closes)
    state = DailyFeatures(DailyWindows(atr_period=10), Utc247Calendar())
    for b in bars:
        start = datetime.combine(b.day, datetime.min.time(), UTC)
        state.update(
            SessionBar(
                instrument_id="CAPITAL:BTCUSD",
                feed="test",
                minutes=1440,
                start=start,
                end=start + timedelta(days=1),
                available_time=start + timedelta(days=1, seconds=1),
                complete=True,
                open=b.close,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=Decimal(0),
                trade_count=0,
                vwap=b.close,
            )
        )
    assert setups.atr(bars, 10) == state.atr()


def test_the_sunday_bar_decides_the_week_long_or_short() -> None:
    up = read_epic("BTCUSD", _bars(_rising()), SPEC)  # 14 days: the last is Sunday 15 March
    assert up.day == date(2026, 3, 15) and up.decision_bar and up.direction == "LONG"
    assert up.change == Decimal(72600) / Decimal(71200) - 1
    down = read_epic("BTCUSD", _bars(_rising(step=-200)), SPEC)
    assert down.direction == "SHORT" and down.decision_bar
    monday = read_epic("BTCUSD", _bars(_rising(15)), SPEC)
    assert not monday.decision_bar and monday.direction == "LONG"
    # a missing day restarts the run: too few consecutive bars to read
    bars = _bars(_rising(20))
    del bars[15]
    with pytest.raises(setups.SetupError, match="consecutive daily bars"):
        read_epic("BTCUSD", bars, SPEC)


def test_a_signal_is_sized_inside_the_risk_limit_or_not_taken() -> None:
    long = read_epic("BTCUSD", _bars(_rising()), SPEC)
    t = ticket(long, BTC, SPEC, Decimal("0.99"))
    distance = SPEC.stop_atr_multiplier * long.atr
    assert t.status == "TRADE" and t.direction == "LONG"
    assert t.stop == (long.close - distance).quantize(Decimal("0.01"), rounding="ROUND_FLOOR")
    assert t.target - long.close <= 2 * (long.close - t.stop)  # the journal's 2R limit holds
    assert t.risk_usd <= Decimal("0.99") and t.size >= BTC.min_size
    short = read_epic("BTCUSD", _bars(_rising(step=-200)), SPEC)
    s = ticket(short, BTC, SPEC, Decimal("0.99"))
    assert s.direction == "SHORT" and s.stop > short.close > s.target
    wide = read_epic("BTCUSD", _bars(_rising(), spread="9000"), SPEC)
    refused = ticket(wide, BTC, SPEC, Decimal("0.99"))
    assert refused.status == "TOO_LARGE" and refused.risk_usd > Decimal("0.99")
    assert "over the USD 0.99 limit" in refused.note


def _feed(path: Path, bars: list[Bar]) -> None:
    rows = [
        [
            int(datetime.combine(b.day, datetime.min.time(), UTC).timestamp()),
            str(b.open),
            str(b.high),
            str(b.low),
            str(b.close),
            "0",
        ]
        for b in bars
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": "cfd:X", "series": {"1d": rows}}), encoding="utf-8")


def test_today_is_left_out_and_a_weekend_market_folds_its_sunday_bar(tmp_path: Path) -> None:
    file = tmp_path / "cfd" / "BTCUSD.json"
    _feed(file, _bars(_rising(10), first=date(2026, 11, 6)))  # Fri 6 .. Sun 15
    crypto = setups.load_daily(file, date(2026, 11, 10))
    assert [b.day.day for b in crypto] == [6, 7, 8, 9]  # every day kept; today (10) left out
    folded = setups.load_daily(file, date(2026, 11, 10), weekend_closed=True)
    assert [b.day.day for b in folded] == [6, 9]
    with pytest.raises(setups.SetupError, match="no daily bars"):
        setups.load_daily(tmp_path / "cfd" / "missing.json", date(2026, 11, 10))


def _market(
    root: Path, now: datetime, *, btc: list[str], eth: list[str], age_minutes: int = 4
) -> None:
    _feed(root / "cfd" / "BTCUSD.json", _bars(btc))
    _feed(root / "cfd" / "ETHUSD.json", _bars(eth, spread="10"))
    updated = (now - timedelta(minutes=age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    instruments = [{"symbol": "BTCUSD", "last_price": "60000"}, {"symbol": "ETHUSD"}]
    group = {"id": "cfd", "status": "ok", "updated_at": updated, "instruments": instruments}
    (root / "index.json").write_text(
        json.dumps({"every_minutes": 5, "groups": [group]}), encoding="utf-8"
    )


def _journals(tmp_path: Path) -> dict[str, PracticeJournal]:
    return {
        "capital-demo": PracticeJournal(tmp_path / "cfd.jsonl", venue="capital-demo"),
        "crypto-cfd": PracticeJournal(tmp_path / "crypto.jsonl", venue="crypto-cfd"),
    }


#: Monday 16 March 2026, a few minutes after the Sunday bar closed.
MONDAY = datetime(2026, 3, 16, 0, 10, tzinfo=UTC)
ETH_FALLING = [str(2500 - 40 * k) for k in range(14)]


def test_the_scan_takes_the_larger_move_and_flags_a_stale_feed(tmp_path: Path) -> None:
    _market(tmp_path / "market", MONDAY, btc=_rising(), eth=ETH_FALLING)
    doc = setups.scan(SPEC, _journals(tmp_path), tmp_path / "market", MONDAY)
    assert doc["version"] == 2 and doc["setup"] == "weekly_momentum_v1"
    assert doc["risk_limit_usd"] == "1.00" and doc["next_decision_at"] == "2026-03-23T00:00:00Z"
    assert doc["feed"]["stale"] is False and doc["expected_bar_day"] == "2026-03-15"
    btc, eth = doc["readings"]
    assert (btc["epic"], eth["epic"]) == ("BTCUSD", "ETHUSD")
    assert btc["signal"] and eth["signal"] and not btc["stale"]
    # ETH fell 11% in the week, BTC rose 2%: the larger move is taken, the other waits
    assert eth["ticket"]["status"] == "TRADE" and eth["ticket"]["direction"] == "SHORT"
    assert btc["ticket"]["status"] == "GROUP_BUSY" and "larger move" in btc["ticket"]["note"]
    assert json.loads(json.dumps(doc)) == doc
    text = "\n".join(setups.render(doc))
    assert "SIGNAL: sell short" in text and "SIGNAL, not taken" in text
    later = MONDAY + timedelta(days=3)
    old = setups.scan(SPEC, _journals(tmp_path), tmp_path / "market", later)
    assert old["feed"]["stale"] and all(r["stale"] for r in old["readings"])
    assert old["next_decision_at"] == "2026-03-23T00:00:00Z"
    assert any("MARKET FEED STALE" in line for line in setups.render(old))


def test_the_scan_checks_open_trades_and_waits_for_an_open_crypto_trade(tmp_path: Path) -> None:
    _market(tmp_path / "market", MONDAY, btc=_rising(), eth=ETH_FALLING)
    journals = _journals(tmp_path)
    crypto = journals["crypto-cfd"]
    opened_at = MONDAY - timedelta(days=8)
    plan = {
        "trade_id": "B1",
        "epic": "BTCUSD",
        "direction": "BUY",
        "size": "0.0001",
        "entry": "70000",
        "stop": "64000",
        "target": "82000",
        "thesis": "weekly momentum: the week rose, long for the next week",
        "setup": "weekly_momentum_v1",
        "timeframe": "1w",
        "invalidation": "the stop, or the week's end",
    }
    crypto.append("PLAN", plan, opened_at)
    crypto.append(
        "OPEN",
        {"trade_id": "B1", "deal_id": "D1", "fill_level": "70010", "size": "0.0001"},
        opened_at,
    )
    doc = setups.scan(SPEC, journals, tmp_path / "market", MONDAY)
    (trade,) = doc["open_trades"]
    assert trade["sleeve"] == "crypto-cfd" and trade["overdue"] and trade["beyond_stop"]
    assert trade["exit_signal"] is None
    tickets = {r["epic"]: r["ticket"] for r in doc["readings"]}
    assert tickets["ETHUSD"]["status"] == "GROUP_BUSY"
    assert "close it if its week is over" in tickets["ETHUSD"]["note"]
    text = "\n".join(setups.render(doc))
    assert "OVERDUE: close it" in text and "PRICE BEYOND STOP" in text


def test_the_next_decision_is_the_coming_monday_midnight() -> None:
    assert setups.next_decision(datetime(2026, 11, 10, 23, 0, tzinfo=UTC), 6) == datetime(
        2026, 11, 16, tzinfo=UTC
    )
    assert setups.next_decision(datetime(2026, 11, 16, tzinfo=UTC), 6) == datetime(
        2026, 11, 16, tzinfo=UTC
    )
    assert setups.next_decision(datetime(2026, 11, 15, 12, tzinfo=UTC), 6) == datetime(
        2026, 11, 16, tzinfo=UTC
    )


def test_the_scan_command_prints_and_writes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from trader.cli.main import app

    monkeypatch.setattr(journal_mod, "REPO_ROOT", tmp_path)
    _market(tmp_path / "market", MONDAY, btc=_rising(), eth=ETH_FALLING)
    runner = CliRunner()
    out = runner.invoke(app, ["journal", "scan", "--market-root", str(tmp_path / "market")])
    assert out.exit_code == 0, out.output
    assert "Weekly momentum on the Bitcoin and Ether CFDs" in out.output
    written = json.loads((tmp_path / "runs" / "practice" / "scan.json").read_text("utf-8"))
    assert [r["epic"] for r in written["readings"]] == ["BTCUSD", "ETHUSD"]
    assert not (tmp_path / "runs" / "practice" / "scan.tmp").exists()
    empty = runner.invoke(app, ["journal", "scan", "--market-root", str(tmp_path / "none")])
    assert empty.exit_code == 2 and "scan refused: no daily bars" in empty.output


class _Clock:
    """A clock whose sleep only moves its own time."""

    def __init__(self, now: datetime) -> None:
        self._now = now
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)


def test_the_watch_rescans_and_reports_a_scan_that_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal_mod, "REPO_ROOT", tmp_path)  # empty journals: USD 100 each
    market = tmp_path / "market"
    _market(market, MONDAY, btc=_rising(), eth=ETH_FALLING)
    clock, lines = _Clock(MONDAY), []
    code = run_watch(
        every_minutes=15, market_root=market, echo=lines.append, clock=clock, iterations=2
    )
    assert code == 0 and clock.slept == [900]
    assert lines[0].startswith("scan 2026-03-16T00:10:00")
    assert "2 signal(s), 0 open trade(s)" in lines[0] and "FEED STALE" not in lines[0]
    assert "FEED STALE" in lines[1]  # a quarter of an hour on, the feed has not refreshed
    files = tmp_path / "runs" / "practice"
    assert len(json.loads((files / "scan.json").read_text("utf-8"))["readings"]) == 2
    sleeves = json.loads((files / "sleeves.json").read_text("utf-8"))["sleeves"]
    assert set(sleeves) == {"capital-demo", "crypto-cfd"}
    lines.clear()
    run_watch(
        every_minutes=15,
        market_root=tmp_path / "none",
        echo=lines.append,
        clock=clock,
        iterations=1,
    )
    failed = json.loads((files / "scan.json").read_text("utf-8"))
    assert "no daily bars" in failed["error"] and "readings" not in failed
    assert lines == [f"scan failed: {failed['error']}"]
    with pytest.raises(setups.SetupError, match="1 to 240 minutes"):
        run_watch(every_minutes=0, clock=clock, iterations=1)


def test_the_practice_watch_stays_off_the_heavy_stack_and_any_order_authority() -> None:
    """It runs beside the console all day, so it loads neither the CLI's database stack nor
    anything that can reach a broker."""
    probe = (
        "import sys, trader.practice.__main__; "
        "heavy = ('pandas', 'duckdb', 'exchange_calendars', 'sqlalchemy', 'numpy', 'typer'); "
        "authority = ('trader.brokers', 'trader.execution', 'trader.risk', 'trader.runtime', "
        "'trader.storage'); "
        "print(','.join(sorted(m for m in sys.modules if m.split('.')[0] in heavy "
        "or m.startswith(authority))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"the practice watch imported: {out}"


def test_the_light_entry_point_scans_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trader.practice.__main__ import main

    monkeypatch.setattr(journal_mod, "REPO_ROOT", tmp_path)
    _market(tmp_path / "market", MONDAY, btc=_rising(), eth=ETH_FALLING)
    assert main(["scan", "--market-root", str(tmp_path / "market")]) == 0
    assert "SIGNAL: sell short" in capsys.readouterr().out
    assert main(["scan", "--market-root", str(tmp_path / "none")]) == 2
    assert "no daily bars" in capsys.readouterr().err
