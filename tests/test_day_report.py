"""The paper account's day report (``trader paper day``): round trips, fees and losing sessions.

The first case is the practice session of 2026-11-10: three AMZN round trips whose fills the
ledger holds with fee 0 while the fee is pending, because Alpaca's paper account charges none.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import insert, select
from typer.testing import CliRunner

from trader.accounting.fees import load_fee_schedule
from trader.accounting.ledger import Ledger
from trader.cli.main import app
from trader.data.calendars import ExchangeCalendar
from trader.domain.account import CashFlowKind
from trader.domain.orders import FeeStatus, Fill
from trader.domain.types import Side, Track
from trader.reporting.day_report import (
    FillRow,
    day_report,
    fold_round_trips,
    latest_session_with_fills,
    render_day_report,
)
from trader.runtime import paper as paper_runtime
from trader.storage.db import Database
from trader.storage.schema import accounts, fills, portfolios

ROOT = Path(__file__).resolve().parents[1]
_PAPER = yaml.safe_load((ROOT / "configs" / "paper.yaml").read_text(encoding="utf-8"))
ACCT = _PAPER["accounts"][0]["account_id"]
SCHEDULE = load_fee_schedule(ROOT / "configs" / "fees" / "alpaca_us_equities_v2.yaml")
SESSION_OF = ExchangeCalendar("XNYS").session_date_of
AMZN = "XNAS:AMZN"
#: Tuesday 2026-11-10, 15:05 UTC = 10:05 in New York.
T0 = datetime(2026, 11, 10, 15, 5, tzinfo=UTC)
DAY = date(2026, 11, 10)
#: (minutes after T0, side, quantity, price) of the session's six fills.
SESSION_10_NOV = (
    (0, Side.BUY, "0.073359", "272.4940"),
    (45, Side.SELL, "0.073359", "270.2400"),
    (245, Side.BUY, "0.073812", "270.3740"),
    (247, Side.SELL, "0.073812", "269.9300"),
    (270, Side.BUY, "0.073674", "270.7160"),
    (293, Side.SELL, "0.073674", "272.2140"),
)


def _open(db: Database, ledger: Ledger) -> None:
    with db.tx() as conn:
        conn.execute(
            insert(portfolios).values(
                portfolio_id="pf",
                base_currency="USD",
                initial_capital=Decimal(100),
                version=0,
                created_at=T0,
            )
        )
        conn.execute(
            insert(accounts).values(
                account_id=ACCT,
                portfolio_id="pf",
                alias="practice",
                track="STOCKS",
                broker="alpaca_paper",
                mode="paper",
                currency="USD",
                rule_profile="UNVERIFIED",
                legal_type="SIMULATED",
                allocated_capital=Decimal(100),
                broker_cash_baseline=None,
                version=0,
                created_at=T0,
            )
        )
        ledger.record_cash_flow(
            conn,
            account_id=ACCT,
            kind=CashFlowKind.INITIAL_CAPITAL,
            amount=Decimal(100),
            currency="USD",
            at=T0,
            ref="init",
        )


def _post(
    db: Database,
    ledger: Ledger,
    n: int,
    at: datetime,
    side: Side,
    qty: str,
    price: str,
    *,
    fee: str = "0",
    status: FeeStatus = FeeStatus.PENDING,
    order: str | None = None,
) -> None:
    fill = Fill(
        broker="alpaca_paper",
        account_id=ACCT,
        execution_id=f"x{n}",
        broker_order_id=order or f"o{n}",
        client_order_id=f"c{n}",
        instrument_id=AMZN,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        currency="USD",
        fee=Decimal(fee),
        fee_currency="USD",
        fee_status=status,
        exec_time=at,
        receipt_time=at,
    )
    with db.tx() as conn:
        ledger.post_fill(
            conn,
            fill,
            intent_id=None,
            owner_track=Track.STOCKS,
            settles_on=SESSION_OF(at) + timedelta(days=1),
            trade_date=SESSION_OF(at),
        )


def _post_session(db: Database, ledger: Ledger) -> None:
    for n, (minutes, side, qty, price) in enumerate(SESSION_10_NOV):
        _post(db, ledger, n, T0 + timedelta(minutes=minutes), side, qty, price)


def _row(
    minutes: int,
    side: Side,
    qty: str,
    price: str,
    *,
    order: str,
    fee: str = "0",
    status: FeeStatus = FeeStatus.PENDING,
    purpose: str | None = None,
) -> FillRow:
    return FillRow(
        instrument_id=AMZN,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_status=status,
        exec_time=T0 + timedelta(minutes=minutes),
        broker_order_id=order,
        purpose=purpose,
    )


def test_the_10_november_session_reads_gross_as_the_ledger_and_adds_the_unbooked_fees(
    sqlite_db: Database,
) -> None:
    ledger = Ledger(sqlite_db)
    _open(sqlite_db, ledger)
    _post_session(sqlite_db, ledger)
    with sqlite_db.tx() as conn:
        report = day_report(
            conn, account_id=ACCT, session_date=DAY, session_date_of=SESSION_OF, schedule=SCHEDULE
        )
        ledger_gross = sum(
            Decimal(g)
            for g in conn.execute(
                select(fills.c.realized_gross).where(fills.c.realized_gross.is_not(None))
            ).scalars()
        )
    assert len(report.round_trips) == 3
    assert report.gross == ledger_gross
    assert report.gross.quantize(Decimal("0.0001")) == Decimal("-0.0878")
    assert report.fees_booked == 0
    # each round trip: buy 0.01 (CAT), sell 0.03 (SEC, TAF and CAT, each rounded up to a cent)
    assert report.fees_modelled == Decimal("0.12")
    assert report.net == report.gross - Decimal("0.12")
    assert (report.winners, report.losers) == (1, 2)
    assert report.open_positions == ()
    assert report.losing_streak == 1
    text = "\n".join(render_day_report(report))
    assert "round trips 3: 1 won, 2 lost" in text
    assert "gross          -0.0878" in text
    assert "fees modelled  0.1200 (not booked; schedule " + SCHEDULE.schedule_id in text
    assert "net            -0.2078" in text
    assert "open at the close: none" in text
    assert "losing sessions in a row: 1" in text


def test_an_order_filled_in_parts_is_priced_once() -> None:
    rows = [
        _row(0, Side.BUY, "0.04", "270", order="buy"),
        _row(1, Side.BUY, "0.06", "270.10", order="buy"),
        _row(30, Side.SELL, "0.05", "271", order="sell", purpose="EXIT_TARGET"),
        _row(31, Side.SELL, "0.05", "271.10", order="sell", purpose="EXIT_TARGET"),
    ]
    closed, still_open = fold_round_trips(rows, SCHEDULE)
    assert still_open == []
    (trip,) = closed
    assert trip.fees_modelled == Decimal("0.04")  # one buy order and one sell order, not four
    assert trip.quantity == Decimal("0.10")
    assert trip.entry_price == (Decimal("0.04") * 270 + Decimal("0.06") * Decimal("270.10")) / (
        Decimal("0.10")
    )
    assert trip.gross == Decimal("0.05") * 271 + Decimal("0.05") * Decimal("271.10") - (
        Decimal("0.04") * 270 + Decimal("0.06") * Decimal("270.10")
    )
    assert trip.exit_purposes == ("EXIT_TARGET",)


def test_a_fee_the_ledger_holds_is_never_modelled_a_second_time() -> None:
    accrued = [
        _row(0, Side.BUY, "0.07", "270", order="b", fee="0.01"),
        _row(30, Side.SELL, "0.07", "271", order="s", fee="0.03"),
    ]
    (trip,), _ = fold_round_trips(accrued, SCHEDULE)
    assert (trip.fees_booked, trip.fees_modelled) == (Decimal("0.04"), Decimal(0))
    assert trip.net == trip.gross - Decimal("0.04")
    # a final zero fee is the broker's word that nothing is charged (the replay broker)
    final = [
        _row(0, Side.BUY, "0.07", "270", order="b", status=FeeStatus.FINAL),
        _row(30, Side.SELL, "0.07", "271", order="s", status=FeeStatus.FINAL),
    ]
    (free,), _ = fold_round_trips(final, SCHEDULE)
    assert (free.fees_booked, free.fees_modelled) == (Decimal(0), Decimal(0))
    # without a schedule nothing can be modelled
    (bare,), _ = fold_round_trips(
        [
            _row(0, Side.BUY, "0.07", "270", order="b"),
            _row(30, Side.SELL, "0.07", "271", order="s"),
        ],
        None,
    )
    assert bare.fees_modelled == 0


def test_a_round_trip_belongs_to_the_session_of_its_close(sqlite_db: Database) -> None:
    ledger = Ledger(sqlite_db)
    _open(sqlite_db, ledger)
    _post(sqlite_db, ledger, 1, T0, Side.BUY, "0.05", "270")
    _post(sqlite_db, ledger, 2, T0 + timedelta(days=1), Side.SELL, "0.05", "280")
    with sqlite_db.tx() as conn:
        first = day_report(
            conn, account_id=ACCT, session_date=DAY, session_date_of=SESSION_OF, schedule=SCHEDULE
        )
        second = day_report(
            conn,
            account_id=ACCT,
            session_date=DAY + timedelta(days=1),
            session_date_of=SESSION_OF,
            schedule=SCHEDULE,
        )
        assert latest_session_with_fills(conn, ACCT, SESSION_OF) == DAY + timedelta(days=1)
        assert latest_session_with_fills(conn, "acct-unknown", SESSION_OF) is None
    assert first.round_trips == ()
    (held,) = first.open_positions
    assert (held.quantity, held.entry_price) == (Decimal("0.05"), Decimal(270))
    assert "no round trip closed in this session" in render_day_report(first)
    assert any("0.05 at 270.0000 since 2026-11-10 15:05 UTC" in s for s in render_day_report(first))
    (trip,) = second.round_trips
    assert trip.gross == Decimal("0.5") and second.open_positions == ()
    assert second.losing_streak == 0


def test_the_losing_run_skips_idle_sessions_and_ends_at_a_winning_one(
    sqlite_db: Database,
) -> None:
    ledger = Ledger(sqlite_db)
    _open(sqlite_db, ledger)
    # Tue 10 Nov wins, Wed 11 loses, Thu 12 has no trade, Fri 13 loses
    plan = ((0, "270", "280"), (1, "270", "269"), (3, "270", "268"))
    n = 0
    for offset, entry, exit_ in plan:
        at = T0 + timedelta(days=offset)
        _post(sqlite_db, ledger, n, at, Side.BUY, "0.05", entry)
        _post(sqlite_db, ledger, n + 1, at + timedelta(minutes=30), Side.SELL, "0.05", exit_)
        n += 2
    streaks = {}
    with sqlite_db.tx() as conn:
        for offset in range(4):
            day = DAY + timedelta(days=offset)
            streaks[offset] = day_report(
                conn,
                account_id=ACCT,
                session_date=day,
                session_date_of=SESSION_OF,
                schedule=SCHEDULE,
            ).losing_streak
    assert streaks == {0: 0, 1: 1, 2: 1, 3: 2}


def test_a_small_gain_that_fees_eat_counts_as_a_losing_trade() -> None:
    rows = [
        _row(0, Side.BUY, "0.07", "270", order="b"),
        _row(30, Side.SELL, "0.07", "270.20", order="s"),
    ]
    (trip,), _ = fold_round_trips(rows, SCHEDULE)
    assert trip.gross == Decimal("0.014") and trip.net == Decimal("-0.026")


def test_paper_day_prints_the_session_without_a_broker_or_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{(tmp_path / 'practice.db').as_posix()}"
    db = Database(url)
    db.create_schema()
    ledger = Ledger(db)
    _open(db, ledger)
    monkeypatch.setenv("TRADER_HOST_ROLE", "DEV")
    monkeypatch.setattr(paper_runtime, "paper_database_url", lambda cfg: url)
    runner = CliRunner()
    empty = runner.invoke(app, ["paper", "day"])
    assert empty.exit_code == 0 and "has no fills yet" in empty.output
    _post_session(db, ledger)
    db.dispose()
    latest = runner.invoke(app, ["paper", "day"])
    assert latest.exit_code == 0, latest.output
    assert f"account {ACCT}, session 2026-11-10" in latest.output
    assert "AMZN   15:05-15:50 UTC" in latest.output
    assert "net            -0.2078" in latest.output
    idle = runner.invoke(app, ["paper", "day", "--date", "2026-11-11"])
    assert idle.exit_code == 0 and "no round trip closed in this session" in idle.output
    assert "losing sessions in a row: 1" in idle.output
    bad = runner.invoke(app, ["paper", "day", "--date", "10.11.2026"])
    assert bad.exit_code == 2 and "YYYY-MM-DD" in bad.output
    replay = runner.invoke(app, ["paper", "day", "--config", str(ROOT / "configs/replay.yaml")])
    assert replay.exit_code == 2 and "no fallback" in replay.output
