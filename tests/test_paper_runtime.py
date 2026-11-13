"""Paper runtime (runtime/paper.py): refusals, bar-boundary scheduling on an injected clock,
clock-skew pauses, graceful shutdown and the CLI wiring. No test touches the network or a real
broker; PostgreSQL-only behaviour is marked ``pg``."""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from alembic import command
from sqlalchemy import func, select
from typer.testing import CliRunner

from tests.conftest import pg_url
from trader.accounting.fees import load_fee_schedule
from trader.brokers.base import BrokerTimeout, OrderScope
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.brokers.replay.broker import ReplayBroker
from trader.cli.main import app
from trader.config import secrets
from trader.config.host import HostRole
from trader.config.policy import scale_policy
from trader.config.runtime_config import ResolvedConfig, load_config
from trader.data.history import HistoryBar
from trader.data.sources.alpaca_history import AlpacaCredentials, LatestQuote
from trader.data.sources.http import TransportError
from trader.domain.account import BrokerPosition
from trader.domain.market import Quote
from trader.domain.orders import BrokerOrder, BrokerOrderStatus, FeeStatus, Fill, OrderType
from trader.domain.types import Mode, Side
from trader.ops.leadership import LeadershipManager, LeadershipUnavailable
from trader.runtime import broker_factory, paper
from trader.runtime.paper import (
    DEFAULT_FEED,
    PaperEnvironment,
    PaperRunner,
    PaperRunSummary,
    PaperSchedule,
    PaperStartRefused,
    RecordedBaseline,
    build_paper_environment,
    check_account_state,
    close_paper_positions,
    paper_namespace,
    reconcile_paper,
)
from trader.runtime.paper_data import bar_from_source, quote_from_source
from trader.storage.db import Database
from trader.storage.schema import accounts, alerts, bars, order_intents
from trader.strategies.edge import EdgeEstimator, PracticeEdgeEstimator

ROOT = Path(__file__).resolve().parents[1]
PAPER_YAML = ROOT / "configs" / "paper.yaml"
_PAPER = yaml.safe_load(PAPER_YAML.read_text(encoding="utf-8"))
ACCOUNT = _PAPER["accounts"][0]["account_id"]  # follows configs/paper.yaml (ADR-0043)
PORTFOLIO = _PAPER["portfolio_id"]
DB_ENV = _PAPER["database"]["url_env"]  # TRADER_PRACTICE_DATABASE_URL since ADR-0043
CAPITAL = Decimal(
    yaml.safe_load((ROOT / "configs" / _PAPER["policy_file"]).read_text(encoding="utf-8"))[
        "initial_trading_capital"
    ]
)
#: Monday 2026-11-09 (EST): regular hours 14:30-21:00 UTC, entries 15:00-20:30 UTC.
DAY = datetime(2026, 11, 9, tzinfo=UTC)
OPEN = DAY.replace(hour=14, minute=30)
CLOSE = DAY.replace(hour=21)
BASE = {
    "AAPL": Decimal("200"),
    "AMZN": Decimal("180"),
    "GOOGL": Decimal("150"),
    "MSFT": Decimal("400"),
    "NVDA": Decimal("120"),
}
FAST = PaperSchedule(quote_interval_seconds=60, reconcile_interval_seconds=60)
runner_cli = CliRunner()


class FakeClock:
    """``sleep`` advances simulated time: scheduling is deterministic and instant."""

    def __init__(self, start: datetime) -> None:
        self._now = start
        self.sleeps = 0
        self.interrupt_after: int | None = None

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        if self.interrupt_after is not None and self.sleeps >= self.interrupt_after:
            raise KeyboardInterrupt
        self._now += timedelta(seconds=seconds)


class FakePaperBroker(ReplayBroker):
    """The replay broker standing in for the paper adapter (plus its account stream)."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.streaming = False
        self.stops = 0

    def start_stream(self) -> None:
        self.streaming = True

    def stop(self) -> None:
        self.streaming = False
        self.stops += 1


class FakeMarketData:
    """Rising synthetic IEX-like bars (published ``lag`` after their end) and tight quotes."""

    feed = DEFAULT_FEED

    def __init__(
        self,
        clock: FakeClock,
        *,
        lag: timedelta = timedelta(seconds=2),
        lag_by_symbol: dict[str, timedelta] | None = None,
        quote_skew: timedelta = timedelta(milliseconds=-100),
        on_quotes: Callable[[int], None] | None = None,
        fail_quotes: bool = False,
    ) -> None:
        self.clock = clock
        self.lag = lag
        self.lag_by_symbol = lag_by_symbol or {}
        self.quote_skew = quote_skew
        self.on_quotes = on_quotes
        self.fail_quotes = fail_quotes
        self.bar_calls: list[tuple[datetime, tuple[str, ...], datetime, datetime]] = []
        self.quote_calls: list[datetime] = []

    def _bar(self, symbol: str, start: datetime) -> HistoryBar:
        i = int((start - OPEN).total_seconds() // 300)
        low = BASE[symbol] + Decimal(i) * Decimal("0.05") - Decimal("0.01")
        close = low + Decimal("0.05")
        high = close + Decimal("0.01")
        return HistoryBar(
            start=start,
            minutes=5,
            open=low + Decimal("0.01"),
            high=high,
            low=low,
            close=close,
            volume=Decimal(1000),
            vwap=((high + low + close) / 3).quantize(Decimal("0.0001")),
            trade_count=10,
        )

    def completed_bars(
        self, symbols: Sequence[str], *, minutes: int, start: datetime, end: datetime
    ) -> dict[str, list[HistoryBar]]:
        now = self.clock.now()
        self.bar_calls.append((now, tuple(symbols), start, end))
        out: dict[str, list[HistoryBar]] = {}
        for symbol in symbols:
            lag = self.lag_by_symbol.get(symbol, self.lag)
            t = start
            while t <= end:
                if t + timedelta(minutes=minutes) + lag <= now:
                    out.setdefault(symbol, []).append(self._bar(symbol, t))
                t += timedelta(minutes=minutes)
        return out

    def latest_quotes(self, symbols: Sequence[str]) -> dict[str, LatestQuote]:
        now = self.clock.now()
        self.quote_calls.append(now)
        if self.on_quotes is not None:
            self.on_quotes(len(self.quote_calls))
        if self.fail_quotes:
            raise TransportError("HTTP 503 from data.alpaca.markets")
        out = {}
        for symbol in symbols:
            last = self._bar(symbol, max(OPEN, now - timedelta(minutes=5)))
            out[symbol] = LatestQuote(
                symbol=symbol,
                bid=last.close - Decimal("0.01"),
                ask=last.close + Decimal("0.01"),
                bid_size=Decimal(100),
                ask_size=Decimal(100),
                event_time=now + self.quote_skew,
            )
        return out


def _config() -> ResolvedConfig:
    return load_config(PAPER_YAML, host_role=HostRole.DEV)


def _broker(clock: FakeClock, config: ResolvedConfig | None = None) -> FakePaperBroker:
    config = config or _config()
    acct = config.runtime.accounts[0]
    return FakePaperBroker(
        account_id=acct.account_id,
        clock=clock,
        capabilities=load_capabilities(config.resolve(acct.capability_profile)),
        # the simulated venue charges a known (demo) schedule; Alpaca's is still unverified
        fees=load_fee_schedule(config.resolve("fees/replay_demo.yaml")),
        specs=load_instruments(config.resolve(acct.instruments)),
    )


def _env(
    db: Database,
    clock: FakeClock,
    broker: FakePaperBroker | None = None,
    edge: EdgeEstimator | None = None,
) -> PaperEnvironment:
    """A wired paper environment over SQLite (the production builder refuses SQLite)."""
    config = _config()
    acct = config.runtime.accounts[0]
    return paper._assemble(
        config,
        acct,
        capabilities=load_capabilities(config.resolve(acct.capability_profile)),
        clock=clock,
        adapter=broker or _broker(clock, config),
        database=db,
        edge=edge,
        holder="test-worker",
        market_feed=DEFAULT_FEED,
    )


def _alpaca_env(db: Database, clock: FakeClock) -> tuple[PaperEnvironment, Any]:
    """A paper environment over the real Alpaca adapter and the in-memory fake API (SQLite)."""
    from tests.alpaca_fake import PAPER_KEY, PAPER_SECRET, FakeAlpaca
    from trader.brokers.alpaca.adapter import AlpacaPaperAdapter

    fake = FakeAlpaca(clock)  # type: ignore[arg-type]
    config = _config()
    acct = config.runtime.accounts[0]
    adapter = AlpacaPaperAdapter(
        account_id=acct.account_id,
        credentials=AlpacaCredentials(PAPER_KEY, PAPER_SECRET),
        capabilities=load_capabilities(config.resolve(acct.capability_profile)),
        instruments=load_instruments(config.resolve(acct.instruments)),
        clock=clock,
        mode=Mode.PAPER,
        since=clock.now(),
        transport=fake,
        sleep=lambda _s: None,
    )
    env = paper._assemble(
        config,
        acct,
        capabilities=adapter.capabilities(),
        clock=clock,
        adapter=adapter,
        database=db,
        edge=None,
        holder="test-worker",
        market_feed=DEFAULT_FEED,
    )
    return env, fake


@pytest.fixture
def lite(tmp_path: Path) -> Iterator[Database]:
    db = Database(f"sqlite:///{(tmp_path / 'paper.db').as_posix()}")
    db.create_schema()
    yield db
    db.dispose()


# --------------------------------------------------------------------------- scheduling


def test_one_session_polls_each_bar_boundary_and_reconciles_after_the_close(
    lite: Database,
) -> None:
    clock = FakeClock(DAY.replace(hour=20))  # 15:00 New York, one hour before the close
    broker = _broker(clock)
    env = _env(lite, clock, broker)
    data = FakeMarketData(clock)
    logs: list[str] = []
    runner = PaperRunner(env, market_data=data, schedule=FAST, log=logs.append)
    summary = runner.run(sessions=1)
    env.close()

    assert any(
        line.endswith("bars ending 20:05: 5 decided, missing none; candidates 5, approved 0")
        for line in logs
    )
    assert any("warm-up: 325 completed bar(s)" in line for line in logs)
    warm_up, *polls = data.bar_calls
    assert warm_up[0] == DAY.replace(hour=20)
    assert (warm_up[2], warm_up[3]) == (OPEN, DAY.replace(hour=19, minute=59, second=59))
    boundaries = [DAY.replace(hour=20, minute=m) for m in range(5, 60, 5)]
    assert [p[0] for p in polls] == [b + timedelta(seconds=5) for b in boundaries]
    assert [p[3] for p in polls] == [b - timedelta(seconds=1) for b in boundaries]
    assert all(len(p[1]) == 5 for p in polls)
    # quotes at least every minute through the session
    gaps = [b - a for a, b in zip(data.quote_calls, data.quote_calls[1:], strict=False)]
    assert max(gaps) <= timedelta(seconds=60)
    assert summary.sessions_completed == 1
    assert summary.missing_bars == 0
    assert summary.final_reconciliation == "PASSED"
    assert clock.now() >= CLOSE + timedelta(seconds=FAST.close_grace_seconds)
    assert summary.reconciliations["PASSED"] >= 55  # every 60 s, at the close and at shutdown
    assert "FAILED" not in summary.reconciliations
    # 65 warm-up bars (the 19:55 bar was not published yet), then 19:55 late + 11 boundaries
    with lite.tx() as conn:
        assert conn.execute(select(func.count()).select_from(bars)).scalar_one() == 77 * 5
    # rule candidates inside the entry window are all rejected: no validated edge
    stats = runner.loop.stats
    assert stats.candidates == 25 and stats.approved == 0
    assert all(d.reasons and "NO_EDGE" in d.reasons for d in stats.decisions or [])
    assert env.allocator.namespace.startswith(f"paper/{ACCOUNT}/2026-11-09/")
    assert not broker.streaming and broker.stops == 1
    assert broker.list_orders(OrderScope.ALL) == []
    # rejections without an edge are not operational: a summary line, no ENTRIES_BLOCKED alert
    assert not any(line.startswith("!!") or "!! entries blocked" in line for line in logs)
    assert any(
        "session 2026-11-09 summary: candidates 25, approved 0, strategy entries 0, "
        "rejections {'NO_EDGE': 25}, operational none" in line
        for line in logs
    )
    assert any("summary: candidates 25" in line for line in summary.lines(stats))
    with lite.tx() as conn:
        assert conn.execute(select(alerts.c.code)).scalars().all() == []


def test_operational_rejections_are_logged_each_bar_and_alerted_twice(lite: Database) -> None:
    """A session whose quotes fail rejects every candidate PAUSED/STALE_DATA (ADR-0043)."""
    clock = FakeClock(DAY.replace(hour=20))
    env = _env(lite, clock, _broker(clock))
    logs: list[str] = []
    runner = PaperRunner(
        env, market_data=FakeMarketData(clock, fail_quotes=True), schedule=FAST, log=logs.append
    )
    runner.run(sessions=1)
    env.close()
    blocked = [line for line in logs if "!! entries blocked (operational):" in line]
    assert blocked and "PAUSED" in blocked[0]
    summary = next(line for line in logs if "session 2026-11-09 summary:" in line)
    assert "operational {'PAUSED':" in summary
    with lite.tx() as conn:
        codes = conn.execute(select(alerts.c.code)).scalars().all()
    assert codes.count("ENTRIES_BLOCKED") == 2  # the first occurrence and the session summary


def test_the_paper_runner_drives_the_alpaca_adapter_through_a_session(lite: Database) -> None:
    """Integration of the paper runtime with the real Alpaca adapter (fake transport, no
    network): startup recovery, the account stream, periodic and end-of-session reconciliation
    all pass, every candidate is still rejected NO_EDGE and shutdown stops the stream."""
    from tests.alpaca_fake import PAPER_KEY, PAPER_SECRET, FakeAlpaca
    from trader.brokers.alpaca.adapter import AlpacaPaperAdapter
    from trader.domain.types import Mode

    clock = FakeClock(DAY.replace(hour=20))  # 15:00 New York, inside the entry window
    fake = FakeAlpaca(clock)  # type: ignore[arg-type]
    config = _config()
    acct = config.runtime.accounts[0]
    adapter = AlpacaPaperAdapter(
        account_id=acct.account_id,
        credentials=AlpacaCredentials(PAPER_KEY, PAPER_SECRET),
        capabilities=load_capabilities(config.resolve(acct.capability_profile)),
        instruments=load_instruments(config.resolve(acct.instruments)),
        clock=clock,
        mode=Mode.PAPER,
        since=clock.now(),
        transport=fake,
        sleep=lambda _s: None,
    )
    env = paper._assemble(
        config,
        acct,
        capabilities=adapter.capabilities(),
        clock=clock,
        adapter=adapter,
        database=lite,
        edge=None,
        holder="test-worker",
        market_feed=DEFAULT_FEED,
    )
    runner = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST)
    summary = runner.run(sessions=1)
    env.close()
    assert summary.sessions_completed == 1
    assert summary.final_reconciliation == "PASSED"
    assert set(summary.reconciliations) == {"PASSED"}
    stats = runner.loop.stats
    assert stats.candidates > 0 and stats.approved == 0
    assert all(d.reasons and "NO_EDGE" in d.reasons for d in stats.decisions or [])
    assert len(fake.sessions) == 1 and fake.sessions[0].ended.is_set()  # stream stopped
    assert "submit_order" not in fake.calls


def test_the_adapter_factory_builds_the_alpaca_adapter_with_the_contract_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_class(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(broker_factory, "alpaca_adapter_class", lambda: fake_class)
    config = _config()
    acct = config.runtime.accounts[0]
    creds = AlpacaCredentials("PKTEST", "secret")
    clock = FakeClock(DAY)
    broker_factory.paper_adapter(config, acct, credentials=creds, clock=clock, since=DAY)
    assert set(seen) == {
        "account_id",
        "credentials",
        "capabilities",
        "instruments",
        "clock",
        "mode",
        "since",
    }
    assert seen["account_id"] == ACCOUNT and seen["since"] == DAY


@pytest.mark.parametrize(("lag_seconds", "missing"), [(12, 0), (40, 2)])
def test_a_late_bar_is_retried_then_reported_missing(
    lite: Database, lag_seconds: int, missing: int
) -> None:
    clock = FakeClock(DAY.replace(hour=20, minute=45))
    env = _env(lite, clock)
    data = FakeMarketData(clock, lag_by_symbol={"NVDA": timedelta(seconds=lag_seconds)})
    runner = PaperRunner(env, market_data=data, schedule=FAST)
    summary = runner.run(sessions=1)
    env.close()
    polls = [(p[0], p[1]) for p in data.bar_calls[1:]]
    first = DAY.replace(hour=20, minute=50, second=5)
    every = ("AAPL", "AMZN", "GOOGL", "MSFT", "NVDA")
    if missing == 0:
        assert polls[:2] == [(first, every), (first + timedelta(seconds=10), ("NVDA",))]
    else:  # three attempts, then the bar is reported missing (never fabricated)
        assert polls[:3] == [
            (first, every),
            (first + timedelta(seconds=10), ("NVDA",)),
            (first + timedelta(seconds=20), ("NVDA",)),
        ]
    assert summary.missing_bars == missing
    assert summary.final_reconciliation == "PASSED"


def test_waits_for_the_open_and_skips_weekends(lite: Database) -> None:
    clock = FakeClock(datetime(2026, 11, 7, 18, tzinfo=UTC))  # Saturday
    env = _env(lite, clock)
    runner = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST)
    assert runner.session_for(clock.now()).session_date.isoformat() == "2026-11-09"
    assert runner.session_for(DAY.replace(hour=13)).open == OPEN  # before today's open
    assert runner.session_for(CLOSE).session_date.isoformat() == "2026-11-10"  # after close
    data = FakeMarketData(clock, on_quotes=lambda n: runner.request_stop("test"))
    runner = PaperRunner(env, market_data=data, schedule=FAST)
    runner.run()
    env.close()
    assert data.quote_calls[0] == OPEN  # nothing polled before Monday's open
    assert data.bar_calls == []  # no warm-up bars exist at the open


# --------------------------------------------------------------------------- clock skew


def test_source_time_ahead_of_the_local_clock_pauses_entries_instead_of_crashing(
    lite: Database,
) -> None:
    clock = FakeClock(DAY.replace(hour=16))
    env = _env(lite, clock)
    data = FakeMarketData(clock, quote_skew=timedelta(milliseconds=100))
    runner = PaperRunner(env, market_data=data, schedule=FAST)
    runner.start()
    runner.refresh_quotes()  # 100 ms: within the 250 ms policy limit
    quote = env.market.quotes["XNAS:AAPL"]
    assert quote.receipt_time == quote.event_time == clock.now() + timedelta(milliseconds=100)
    with lite.tx() as conn:
        assert env.pauses.all_active(conn) == []
    data.quote_skew = timedelta(seconds=2)
    runner.refresh_quotes()
    with lite.tx() as conn:
        flags = env.pauses.all_active(conn)
    assert [f.cause.value for f in flags] == ["CLOCK_SKEW"]
    assert "2000 ms ahead" in flags[0].details
    assert runner.summary.clock_skew_events == 5
    clock.sleep(1)
    data.quote_skew = timedelta(0)
    runner.refresh_quotes()
    runner.shutdown()  # a PASSED reconciliation clears the health pause once the skew is gone
    with lite.tx() as conn:
        assert env.pauses.all_active(conn) == []
    env.close()


def _startup_clock_check(
    db: Database, *, broker_ahead_ms: int = 0, fault: BaseException | None = None
) -> tuple[list[str], list[tuple[str, ...]]]:
    """Start a runner over the Alpaca adapter whose broker clock is ``broker_ahead_ms`` ahead of
    the system clock; returns the clock lines of the log and the alerts written."""
    clock = FakeClock(DAY.replace(hour=16))
    env, fake = _alpaca_env(db, clock)
    fake.clock_ahead = timedelta(milliseconds=broker_ahead_ms)
    if fault is not None:
        fake.fail_next["get_clock"] = [fault]
    logs: list[str] = []
    runner = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST, log=logs.append)
    runner.start()  # never refused or delayed by the clock check
    assert runner.summary.reconciliations == {"PASSED": 1}
    with db.tx() as conn:
        assert env.pauses.all_active(conn) == []  # the check itself pauses nothing
        rows = conn.execute(
            select(alerts.c.severity, alerts.c.code, alerts.c.account_id, alerts.c.message)
        ).all()
    runner.shutdown()
    env.close()
    stamp = "2026-11-09 16:00:00Z "
    return [line.removeprefix(stamp) for line in logs if "system clock" in line], [
        tuple(row) for row in rows
    ]


SYNC_HINT = (
    "until the clock is synchronised (Windows: Settings > Time & language > Date & time > "
    "Sync now, or run w32tm /resync as administrator)"
)


def test_a_slow_system_clock_is_announced_at_startup_with_an_alert(lite: Database) -> None:
    """2026-11-10: a clock 0.47 s slow raised CLOCK_SKEW on every quote from the open, and
    nobody saw why for 30 minutes. Now the first lines of the session say so."""
    lines, raised = _startup_clock_check(lite, broker_ahead_ms=470)
    message = (
        "system clock is 470 ms behind the broker's clock (limit 250 ms): new entries will be "
        f"paused {SYNC_HINT}"
    )
    assert lines == [f"!! {message}"]
    assert raised == [("HIGH", "SYSTEM_CLOCK_OFFSET", ACCOUNT, message)]


def test_a_fast_system_clock_is_announced_too(lite: Database) -> None:
    lines, raised = _startup_clock_check(lite, broker_ahead_ms=-400)
    message = (
        "system clock is 400 ms ahead of the broker's clock (limit 250 ms): quotes will look "
        f"older than they are and entries may be refused as stale {SYNC_HINT}"
    )
    assert lines == [f"!! {message}"]
    assert raised == [("HIGH", "SYSTEM_CLOCK_OFFSET", ACCOUNT, message)]


@pytest.mark.parametrize(
    ("broker_ahead_ms", "line"),
    [
        (100, "system clock is 100 ms behind the broker's clock (limit 250 ms)"),
        (250, "system clock is 250 ms behind the broker's clock (limit 250 ms)"),
        (-30, "system clock is 30 ms ahead of the broker's clock (limit 250 ms)"),
        (0, "system clock is within 1 ms of the broker's clock (limit 250 ms)"),
    ],
)
def test_a_clock_within_the_limit_is_one_plain_log_line(
    lite: Database, broker_ahead_ms: int, line: str
) -> None:
    lines, raised = _startup_clock_check(lite, broker_ahead_ms=broker_ahead_ms)
    assert lines == [line]
    assert raised == []


def test_a_clock_check_that_fails_is_only_noted(lite: Database) -> None:
    lines, raised = _startup_clock_check(lite, fault=BrokerTimeout("fake: no answer"))
    assert lines == [
        "system clock not checked: the reference clock could not be read (BrokerTimeout)"
    ]
    assert raised == []


def test_a_broker_without_a_clock_skips_the_check(lite: Database) -> None:
    clock = FakeClock(DAY.replace(hour=16))
    env = _env(lite, clock)  # the replay broker stands in: it reports no clock
    logs: list[str] = []
    runner = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST, log=logs.append)
    runner.start()
    runner.shutdown()
    env.close()
    assert any(
        line.endswith("system clock not checked: this broker adapter reports no clock")
        for line in logs
    )
    with lite.tx() as conn:
        assert conn.execute(select(alerts.c.code)).scalars().all() == []


def test_conversions_drop_unusable_observations_and_report_skew() -> None:
    now = DAY.replace(hour=16)

    def latest(bid: str, ask: str, ahead_ms: int = 0) -> LatestQuote:
        return LatestQuote(
            "AAPL", Decimal(bid), Decimal(ask), Decimal(1), Decimal(1),
            now + timedelta(milliseconds=ahead_ms),
        )  # fmt: skip

    kw = {"instrument_id": "XNAS:AAPL", "feed": DEFAULT_FEED, "received_at": now, "sequence": 1}
    assert quote_from_source(latest("0", "200.01"), **kw)[0] is None  # one-sided book
    assert quote_from_source(latest("200.02", "200.01"), **kw)[0] is None  # crossed
    quote, skew = quote_from_source(latest("200", "200.01", ahead_ms=400), **kw)
    assert quote is not None and skew == timedelta(milliseconds=400)
    source = FakeMarketData(FakeClock(now))._bar("AAPL", DAY.replace(hour=15, minute=55))
    bar, skew = bar_from_source(
        source, instrument_id="XNAS:AAPL", feed=DEFAULT_FEED, received_at=now
    )
    assert bar is not None and skew == timedelta(0) and bar.available_time == now
    early = DAY.replace(hour=15, minute=59, second=58)
    bar, skew = bar_from_source(
        source, instrument_id="XNAS:AAPL", feed=DEFAULT_FEED, received_at=early
    )
    assert bar is not None and bar.available_time == source.end and skew == timedelta(seconds=2)
    no_vwap = replace(source, vwap=None)
    assert (
        bar_from_source(no_vwap, instrument_id="X", feed=DEFAULT_FEED, received_at=now)[0] is None
    )


def test_market_data_outage_pauses_entries_and_keeps_running(lite: Database) -> None:
    clock = FakeClock(DAY.replace(hour=16))
    env = _env(lite, clock)
    runner = PaperRunner(env, market_data=FakeMarketData(clock, fail_quotes=True), schedule=FAST)
    runner.start()
    runner.tick()
    with lite.tx() as conn:
        assert [f.cause.value for f in env.pauses.all_active(conn)] == ["FEED_GAP"]
    assert runner.summary.feed_errors == 1
    runner.shutdown()
    env.close()


def test_a_broker_read_failure_fails_reconciliation_instead_of_stopping_the_loop(
    lite: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adapter error during reconciliation (an unmapped Alpaca status, a timeout) is a FAILED
    run with an alert and a pause; the paper loop keeps running and a later run clears it."""
    clock = FakeClock(DAY.replace(hour=16))
    broker = _broker(clock)
    env = _env(lite, clock, broker)
    runner = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST)
    runner.start()

    def unavailable(scope: OrderScope) -> list[BrokerOrder]:
        raise BrokerTimeout("fake: the broker did not answer")

    with monkeypatch.context() as patch:
        patch.setattr(broker, "list_orders", unavailable)
        result = runner._reconcile("paper-periodic")
    assert result.status == "FAILED"
    assert result.problems == [
        "broker unavailable during reconciliation: BrokerTimeout: fake: the broker did not answer"
    ]
    with lite.tx() as conn:
        codes = list(conn.execute(select(alerts.c.code)).scalars())
        causes = [f.cause.value for f in env.pauses.all_active(conn)]
    assert "RECONCILIATION_BROKER_UNAVAILABLE" in codes
    assert causes == ["RECONCILIATION_FAILED"]
    assert runner._reconcile("paper-periodic").status == "PASSED"
    with lite.tx() as conn:
        assert env.pauses.all_active(conn) == []
    runner.shutdown()
    env.close()


# --------------------------------------------------------------------------- shutdown


def test_stop_request_ends_the_session_with_a_final_reconciliation(lite: Database) -> None:
    clock = FakeClock(DAY.replace(hour=16))
    broker = _broker(clock)
    env = _env(lite, clock, broker)
    holder: list[PaperRunner] = []
    data = FakeMarketData(
        clock, on_quotes=lambda n: holder[0].request_stop("owner Ctrl+C") if n == 3 else None
    )
    runner = PaperRunner(env, market_data=data, schedule=FAST)
    holder.append(runner)
    summary = runner.run()
    env.close()
    assert summary.stopped == "owner Ctrl+C"
    assert summary.sessions_completed == 0
    assert summary.final_reconciliation == "PASSED"
    assert len(data.quote_calls) == 3 and clock.now() < DAY.replace(hour=16, minute=5)
    assert broker.stops == 1


def test_keyboard_interrupt_is_a_graceful_stop(lite: Database) -> None:
    clock = FakeClock(DAY.replace(hour=16))
    clock.interrupt_after = 30
    broker = _broker(clock)
    env = _env(lite, clock, broker)
    summary = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST).run()
    env.close()
    assert summary.stopped == "interrupted (Ctrl+C)"
    assert summary.final_reconciliation == "PASSED"
    assert broker.stops == 1


def test_a_day_order_still_live_after_the_close_raises_an_alert(lite: Database) -> None:
    clock = FakeClock(CLOSE - timedelta(minutes=1))
    env = _env(lite, clock)
    runner = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST)
    runner.start()
    env.set_session(DAY.date())
    with lite.tx() as conn:
        conn.execute(
            order_intents.insert().values(
                intent_id="intent-test",
                seq=1,
                portfolio_id=PORTFOLIO,
                account_id=ACCOUNT,
                client_order_id="co-test",
                instrument_id="XNAS:AAPL",
                track="STOCKS",
                side="BUY",
                purpose="ENTRY",
                order_type="LIMIT",
                quantity=Decimal("0.1"),
                limit_price=Decimal(200),
                time_in_force="DAY",
                state="PENDING_SUBMIT",
                intent_hash="h",
                plan_json="{}",
                plan_digest="d",
                valid_until=clock.now(),
                cum_filled_qty=Decimal(0),
                created_at=clock.now(),
                updated_at=clock.now(),
            )
        )
    session = runner.session_for(clock.now())
    assert runner.close_session(session)
    with lite.tx() as conn:
        codes = conn.execute(select(alerts.c.code)).scalars().all()
    assert "DAY_ORDER_NOT_EXPIRED" in codes
    assert runner.summary.live_after_close == ["co-test PENDING_SUBMIT"]
    env.close()


def test_close_positions_claims_closure_only_after_confirmed_fills(lite: Database) -> None:
    clock = FakeClock(DAY.replace(hour=16))
    broker = _broker(clock)
    env = _env(lite, clock, broker)
    qty, price = Decimal("0.1"), Decimal("200")
    broker.inject_manual_position("XNAS:AAPL", qty, price)  # the same holding on both sides
    with lite.tx() as conn:
        env.oms._post_fill(
            conn,
            Fill(
                broker=broker.broker,
                account_id=ACCOUNT,
                execution_id="seed-1",
                broker_order_id="seed",
                client_order_id="seed",
                instrument_id="XNAS:AAPL",
                side=Side.BUY,
                quantity=qty,
                price=price,
                currency="USD",
                fee=Decimal(0),
                fee_currency="USD",
                fee_status=FeeStatus.FINAL,
                exec_time=clock.now(),
                receipt_time=clock.now(),
            ),
            intent_id=None,
        )
    # no market activity reaches the broker: the sell stays open, nothing is claimed closed
    pending = close_paper_positions(env, timeout_seconds=30, poll_seconds=10)
    assert pending.requested == {f"{ACCOUNT}:XNAS:AAPL": qty}
    assert pending.remaining == pending.requested
    sells = broker.list_orders(OrderScope.OPEN)
    assert [(o.side, o.quantity) for o in sells] == [(Side.SELL, qty)]

    def quote_then_sleep(seconds: float) -> None:
        FakeClock.sleep(clock, seconds)
        at = clock.now()
        broker.on_market_event(
            Quote(
                instrument_id="XNAS:AAPL",
                feed=DEFAULT_FEED,
                event_time=at,
                receipt_time=at,
                sequence=1,
                bid=Decimal("201"),
                ask=Decimal("201.02"),
                bid_size=Decimal(100),
                ask_size=Decimal(100),
            )
        )

    clock.sleep = quote_then_sleep
    closed = close_paper_positions(env, timeout_seconds=60, poll_seconds=10)
    assert closed.remaining == {} and closed.reconciliation == "PASSED"
    assert len(broker.list_orders(OrderScope.ALL)) == 1  # the open sell was reused, not doubled
    env.close()


def test_a_second_worker_is_refused_before_startup_recovery_pauses_the_leader(
    lite: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup recovery raises RECOVERY_PENDING before taking leadership; the probe refuses first
    so a second `paper run` or a `close-positions` never pauses the running worker."""
    clock = FakeClock(DAY.replace(hour=16))
    broker = _broker(clock)
    env = _env(lite, clock, broker)

    class HeldElsewhere(LeadershipManager):
        def acquire(self) -> int:
            raise LeadershipUnavailable(f"another worker leads account {self.account_id}")

    monkeypatch.setattr(paper, "LeadershipManager", HeldElsewhere)
    with pytest.raises(LeadershipUnavailable):
        PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST).run(sessions=1)
    with pytest.raises(LeadershipUnavailable):
        close_paper_positions(env)
    with lite.tx() as conn:
        assert env.pauses.all_active(conn) == []  # no RECOVERY_PENDING left behind
    assert not broker.streaming and broker.list_orders(OrderScope.ALL) == []
    env.close()


# --------------------------------------------------------------------------- refusals


def test_paper_environment_refuses_sqlite_replay_configs_and_non_paper_adapters(
    lite: Database,
) -> None:
    clock = FakeClock(DAY.replace(hour=16))
    config = _config()
    with pytest.raises(PaperStartRefused, match="PostgreSQL"):
        build_paper_environment(config, clock=clock, adapter=_broker(clock), database=lite)
    replay_cfg = load_config(ROOT / "configs" / "replay.yaml", host_role=HostRole.DEV)
    with pytest.raises(PaperStartRefused, match="paper configuration"):
        build_paper_environment(replay_cfg, clock=clock, adapter=_broker(clock), database=lite)
    replay_caps = load_capabilities(ROOT / "configs" / "capabilities" / "replay.yaml")
    not_paper = _broker(clock)
    not_paper._caps = replay_caps
    with pytest.raises(PaperStartRefused, match="not a paper adapter"):
        build_paper_environment(config, clock=clock, adapter=not_paper, database=lite)
    other = _broker(clock)
    other.account_id = "acct-someone-else"
    with pytest.raises(PaperStartRefused, match="differs"):
        build_paper_environment(config, clock=clock, adapter=other, database=lite)


def _position() -> BrokerPosition:
    return BrokerPosition(
        instrument_id="XNAS:AAPL", quantity=Decimal(1), avg_entry_price=Decimal(200), currency="USD"
    )


def _order() -> BrokerOrder:
    at = DAY.replace(hour=16)
    return BrokerOrder(
        broker="alpaca",
        account_id=ACCOUNT,
        broker_order_id="b-1",
        client_order_id="manual",
        instrument_id="XNAS:AAPL",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal(1),
        limit_price=Decimal(1),
        status=BrokerOrderStatus.ACCEPTED,
        filled_quantity=Decimal(0),
        broker_version=1,
        submitted_at=at,
        updated_at=at,
    )


def test_a_fresh_baseline_requires_a_flat_paper_account() -> None:
    ok: dict[str, object] = {"account_id": ACCOUNT, "recorded": None}
    check_account_state(**ok, positions=[], open_orders=[])
    with pytest.raises(PaperStartRefused, match="not flat"):
        check_account_state(**ok, positions=[_position()], open_orders=[])
    with pytest.raises(PaperStartRefused, match="1 open order"):
        check_account_state(**ok, positions=[], open_orders=[_order()])
    with pytest.raises(PaperStartRefused, match="no paper baseline"):
        check_account_state(**ok, positions=[], open_orders=[], require_baseline=True)
    at = DAY.replace(hour=16)
    recorded = RecordedBaseline(mode="paper", broker_cash_baseline=Decimal(100000), created_at=at)
    # a restart of the same deployment: its own positions are reconciled, not refused
    check_account_state(
        account_id=ACCOUNT, recorded=recorded, positions=[_position()], open_orders=[_order()]
    )
    with pytest.raises(PaperStartRefused, match="replay mode"):
        check_account_state(
            account_id=ACCOUNT,
            recorded=replace(recorded, mode="replay"),
            positions=[],
            open_orders=[],
        )


def test_the_alembic_head_is_reached_programmatically(tmp_path: Path) -> None:
    """Offline (SQL generation, no connection): the -x url/schema plumbing and the script
    location the paper builder uses reach the head revision."""
    buffer = io.StringIO()
    cfg = paper.alembic_config(
        "postgresql+psycopg://u:p@127.0.0.1:1/offline", schema="paper_x", output_buffer=buffer
    )
    command.upgrade(cfg, "head", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE accounts" in sql and "CREATE TABLE alembic_version" in sql
    with pytest.raises(PaperStartRefused, match="migrations not found"):
        paper.alembic_config("postgresql+psycopg://x", script_location=tmp_path)


def test_id_namespace_survives_a_database_reset() -> None:
    first = DAY.replace(hour=14, minute=31)
    a = paper_namespace(ACCOUNT, DAY.date(), first)
    assert a == paper_namespace(ACCOUNT, DAY.date(), first)  # deterministic across restarts
    assert a.startswith(f"paper/{ACCOUNT}/2026-11-09/")
    reset = paper_namespace(ACCOUNT, DAY.date(), first + timedelta(hours=1))  # new baseline
    next_day = paper_namespace(ACCOUNT, DAY.date() + timedelta(days=1), first)
    assert len({a, reset, next_day}) == 3


def test_adapter_factory_refuses_without_the_adapter_or_a_paper_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    creds = AlpacaCredentials("PKTEST", "secret")
    clock = FakeClock(DAY)
    monkeypatch.setattr(broker_factory, "ALPACA_ADAPTER_MODULE", "trader.brokers.no_such_adapter")
    with pytest.raises(broker_factory.AdapterUnavailable, match="not available"):
        broker_factory.paper_adapter(
            config, config.runtime.accounts[0], credentials=creds, clock=clock, since=DAY
        )
    replay_cfg = load_config(ROOT / "configs" / "replay.yaml", host_role=HostRole.DEV)
    with pytest.raises(broker_factory.AdapterUnavailable, match="replay"):
        broker_factory.paper_adapter(
            replay_cfg, replay_cfg.runtime.accounts[0], credentials=creds, clock=clock, since=DAY
        )


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def no_paper_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No database URL, no keys, DEV host - whatever the developer's shell or .env holds."""
    monkeypatch.delenv(DB_ENV, raising=False)
    monkeypatch.delenv(secrets.KEY_ENV, raising=False)
    monkeypatch.delenv(secrets.SECRET_ENV, raising=False)
    monkeypatch.setenv("TRADER_HOST_ROLE", "DEV")
    monkeypatch.setattr(secrets, "_get", lambda service, user: None)
    return monkeypatch


def test_paper_run_refuses_each_missing_prerequisite(no_paper_env: pytest.MonkeyPatch) -> None:
    mp = no_paper_env
    replay = runner_cli.invoke(app, ["paper", "run", "--config", str(ROOT / "configs/replay.yaml")])
    assert replay.exit_code == 2 and "no fallback" in replay.output
    no_db = runner_cli.invoke(app, ["paper", "run"])
    assert no_db.exit_code == 2 and DB_ENV in no_db.output
    mp.setenv(DB_ENV, "sqlite:///paper.db")
    lite = runner_cli.invoke(app, ["paper", "run"])
    assert lite.exit_code == 2 and "PostgreSQL" in lite.output
    mp.setenv(DB_ENV, "postgresql+psycopg://u:p@127.0.0.1:1/never-contacted")
    no_keys = runner_cli.invoke(app, ["paper", "run"])
    assert no_keys.exit_code == 2 and "trader secrets set alpaca-paper" in no_keys.output
    mp.setattr(secrets, "require_alpaca", lambda service: AlpacaCredentials("PKTEST", "s3cr3t"))
    mp.setattr(broker_factory, "ALPACA_ADAPTER_MODULE", "trader.brokers.no_such_adapter")
    no_adapter = runner_cli.invoke(app, ["paper", "run", "--sessions", "1"])
    assert no_adapter.exit_code == 2 and "not available" in no_adapter.output
    assert "s3cr3t" not in no_adapter.output
    mp.setenv("TRADER_HOST_ROLE", "LIVE")
    live_host = runner_cli.invoke(app, ["paper", "run"])
    assert live_host.exit_code == 2 and "host role LIVE" in live_host.output


def test_paper_check_reports_every_gap_without_connecting(
    no_paper_env: pytest.MonkeyPatch,
) -> None:
    no_paper_env.setattr(broker_factory, "ALPACA_ADAPTER_MODULE", "trader.brokers.no_such_adapter")
    result = runner_cli.invoke(app, ["paper", "check"])
    assert result.exit_code == 2
    assert "paper setup incomplete: database, paper keys, adapter" in result.output


def test_reconcile_and_close_positions_need_a_paper_broker_connection(
    no_paper_env: pytest.MonkeyPatch,
) -> None:
    paper_cfg = str(PAPER_YAML)
    rec = runner_cli.invoke(app, ["reconcile", "--config", paper_cfg])
    assert rec.exit_code == 2 and DB_ENV in rec.output
    close = runner_cli.invoke(app, ["close-positions", "--config", paper_cfg])
    assert close.exit_code == 2 and DB_ENV in close.output
    replay_cfg = str(ROOT / "configs" / "replay.yaml")
    assert runner_cli.invoke(app, ["close-positions", "--config", replay_cfg]).exit_code == 2
    assert runner_cli.invoke(app, ["close-positions"]).exit_code == 2
    replay_rec = runner_cli.invoke(app, ["reconcile", "--config", replay_cfg])
    assert replay_rec.exit_code == 0 and "Nothing to do offline" in replay_rec.output


# --------------------------------------------------------------------------- PostgreSQL


@pytest.fixture
def pg_schema() -> Iterator[Callable[[], Database]]:
    created: list[Database] = []

    def make() -> Database:
        db = Database(pg_url(), schema=f"t_paper_{uuid.uuid4().hex[:12]}")
        db.create_namespace()
        created.append(db)
        return db

    yield make
    for db in created:
        db.drop_postgres_schema()


@pytest.mark.pg
def test_paper_environment_migrates_and_records_the_practice_subledger_once(
    pg_schema: Callable[[], Database],
) -> None:
    db = pg_schema()
    clock = FakeClock(DAY.replace(hour=16))
    broker = _broker(clock)
    env = build_paper_environment(_config(), clock=clock, adapter=broker, database=db)
    with db.tx() as conn:
        row = conn.execute(select(accounts).where(accounts.c.account_id == ACCOUNT)).one()
        assert env.ledger.cash(conn, ACCOUNT, "USD") == CAPITAL
    assert row.mode == "paper" and row.allocated_capital == CAPITAL
    assert row.broker_cash_baseline == Decimal(100000)
    assert env.baseline_at == row.created_at
    env.close()
    # restart of the same deployment: the baseline is recorded, so held positions are accepted
    # (startup reconciliation then compares them with the ledger) and nothing is re-created
    broker.inject_manual_position("XNAS:AAPL", Decimal(1), Decimal(200))
    again = build_paper_environment(_config(), clock=clock, adapter=broker, database=db)
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(accounts)).scalar_one() == 1
        assert again.ledger.cash(conn, ACCOUNT, "USD") == CAPITAL
    assert again.baseline_at == row.created_at
    again.close()


@pytest.mark.pg
def test_a_non_flat_account_is_refused_on_a_fresh_database(
    pg_schema: Callable[[], Database],
) -> None:
    db = pg_schema()
    clock = FakeClock(DAY.replace(hour=16))
    broker = _broker(clock)
    broker.inject_manual_position("XNAS:AAPL", Decimal(1), Decimal(200))
    with pytest.raises(PaperStartRefused, match="not flat"):
        build_paper_environment(_config(), clock=clock, adapter=broker, database=db)
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(accounts)).scalar_one() == 0
    db.dispose()


@pytest.mark.pg
def test_owner_reconcile_needs_leadership_and_runs_a_paper_session(
    pg_schema: Callable[[], Database],
) -> None:
    db = pg_schema()
    clock = FakeClock(DAY.replace(hour=20, minute=40))
    env = build_paper_environment(_config(), clock=clock, adapter=_broker(clock), database=db)
    assert reconcile_paper(env).status == "PASSED"
    worker = LeadershipManager(db, account_id=ACCOUNT, holder="other-worker", clock=clock)
    worker.acquire()
    with pytest.raises(LeadershipUnavailable):
        reconcile_paper(env)
    with pytest.raises(LeadershipUnavailable):  # a second worker is refused by the lock probe
        PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST).run(sessions=1)
    with db.tx() as conn:
        assert env.pauses.all_active(conn) == []  # ... without pausing the leader's entries
    worker.release()
    summary = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST).run(sessions=1)
    assert summary.sessions_completed == 1 and summary.final_reconciliation == "PASSED"
    env.close()


def _certification_entry(db: Database, n: int, state: str) -> None:
    """An ENTRY intent as ``trader paper certify`` leaves it: sent, no candidate."""
    at = OPEN + timedelta(minutes=11 + n)
    with db.tx() as conn:
        conn.execute(
            order_intents.insert().values(
                intent_id=f"cert-{n}",
                seq=9000 + n,
                portfolio_id=PORTFOLIO,
                account_id=ACCOUNT,
                client_order_id=f"cert-co-{n}",
                instrument_id="XNAS:NVDA",
                track="STOCKS",
                side="BUY",
                purpose="ENTRY",
                order_type="LIMIT",
                quantity=Decimal("0.01"),
                limit_price=Decimal(100),
                time_in_force="DAY",
                state=state,
                candidate_id=None,
                intent_hash="h",
                plan_json="{}",
                plan_digest="d",
                valid_until=at + timedelta(minutes=5),
                cum_filled_qty=Decimal(0),
                final_cum_qty=Decimal(0),
                created_at=at,
                updated_at=at,
            )
        )


def _practice_session(
    db: Database, *, fill_entries: bool
) -> tuple[PaperRunner, PaperRunSummary, Any, list[str]]:
    """A practice session through the real Alpaca adapter over a paper-like fake API."""
    from tests.alpaca_fake import PAPER_KEY, PAPER_SECRET, FakeAlpaca
    from trader.brokers.alpaca.adapter import AlpacaPaperAdapter

    class PaperLikeAlpaca(FakeAlpaca):
        """Marketable orders fill at once at their limit (sells at the bid); cancels confirm."""

        def submit_order(self, payload: Mapping[str, str]) -> dict[str, Any]:
            response = dict(super().submit_order(payload))
            sell = payload["side"] == "sell"
            if sell or fill_entries:
                symbol = payload["symbol"]
                quote = FakeMarketData(clock).latest_quotes([symbol])[symbol]
                price = quote.bid if sell else Decimal(payload.get("limit_price") or quote.ask)
                self.fill(str(response["id"]), Decimal(payload["qty"]), price)
            return response

        def cancel_order(self, order_id: str) -> None:
            super().cancel_order(order_id)
            self.confirm_cancel(order_id)

    clock = FakeClock(DAY.replace(hour=15, minute=2))
    fake = PaperLikeAlpaca(clock)  # type: ignore[arg-type]
    # Test-only capital: the synthetic bars' tiny targets would not cover the cent fees of a
    # USD 20 position (TOO_COSTLY); this test is about the order path, not the economics.
    config = _config()
    config = replace(config, policy=scale_policy(config.policy, Decimal(1000)))
    acct = config.runtime.accounts[0]
    adapter = AlpacaPaperAdapter(
        account_id=acct.account_id,
        credentials=AlpacaCredentials(PAPER_KEY, PAPER_SECRET),
        capabilities=load_capabilities(config.resolve(acct.capability_profile)),
        instruments=load_instruments(config.resolve(acct.instruments)),
        clock=clock,
        mode=Mode.PAPER,
        since=clock.now(),
        transport=fake,
        sleep=lambda _s: None,
    )
    env = paper._assemble(
        config,
        acct,
        capabilities=adapter.capabilities(),
        clock=clock,
        adapter=adapter,
        database=db,
        edge=PracticeEdgeEstimator(Mode.PAPER),
        holder="test-worker",
        market_feed=DEFAULT_FEED,
    )
    _certification_entry(db, 1, "CANCELED")
    _certification_entry(db, 2, "FILLED")
    logs: list[str] = []
    runner = PaperRunner(env, market_data=FakeMarketData(clock), schedule=FAST, log=logs.append)
    summary = runner.run(sessions=1)
    env.close()
    return runner, summary, fake, logs


def _entry_states(db: Database) -> list[str]:
    with db.tx() as conn:
        return list(
            conn.execute(
                select(order_intents.c.state)
                .where(
                    order_intents.c.candidate_id.is_not(None), order_intents.c.purpose == "ENTRY"
                )
                .order_by(order_intents.c.seq)
            ).scalars()
        )


def test_practice_entries_fill_after_a_same_day_certification_run(lite: Database) -> None:
    """End to end, the 2026-11-09 failure: certification used both session entries and every
    practice candidate was rejected ENTRY_LIMIT_REACHED. Now the strategy keeps its allowance:
    entries are approved, sent through the Alpaca adapter and filled, up to the policy limit."""
    runner, summary, _fake, logs = _practice_session(lite, fill_entries=True)
    stats = runner.loop.stats
    limit = runner.env.config.policy.policy.exposure.max_new_entries_per_session
    states = _entry_states(lite)
    assert stats.approved == len(states) == limit, (stats.approved, states)
    assert states.count("FILLED") == limit
    with lite.tx() as conn:
        codes = conn.execute(select(alerts.c.code)).scalars().all()
    assert "ENTRIES_BLOCKED" not in codes
    assert not any("!! entries blocked" in line for line in logs)
    assert summary.final_reconciliation == "PASSED" and set(summary.reconciliations) == {"PASSED"}
    assert summary.open_positions == {}  # every practice position was exited by the close


def test_an_unfilled_practice_entry_is_cancelled_and_frees_the_next_one(lite: Database) -> None:
    """An entry that does not fill inside its time limit is cancelled at the broker; once the
    cancel is confirmed the pending slot is free and the next candidate is sent."""
    runner, summary, _fake, _logs = _practice_session(lite, fill_entries=False)
    states = _entry_states(lite)
    limit = runner.env.config.policy.policy.exposure.max_new_entries_per_session
    assert states == ["CANCELED"] * limit, states  # sent, timed out, cancelled; then the next
    assert summary.final_reconciliation == "PASSED"
