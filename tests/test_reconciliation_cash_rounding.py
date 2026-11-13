"""Cash reconciliation and the broker's cent rounding (execution/reconciliation.py).

2026-11-10 19:12 UTC: after four fractional AMZN fills the paper broker's cash had moved by
-0.21 USD and the ledger's by -0.198125 USD. The broker books each fill's traded value rounded
to the cent, the ledger keeps the exact product, and the differences had added up to more than
the one-cent tolerance. Every later reconciliation failed and new entries stayed paused.

The comparison now applies the broker's rounding to each fill, so the tolerance stays one cent
however many fills there are, and a real difference still fails.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.test_paper_runtime import ACCOUNT, DAY, FAST, FakeClock, FakeMarketData, _alpaca_env
from trader.domain.orders import FeeStatus, Fill
from trader.domain.types import Side
from trader.execution.reconciliation import broker_cent_rounding
from trader.runtime.paper import PaperEnvironment, PaperRunner
from trader.storage.db import Database

#: The four fills of 2026-11-10: side, quantity, price, and the cash the broker booked.
AMZN_FILLS = [
    ("BUY", Decimal("0.073359498"), Decimal("272.494"), Decimal("-19.99")),
    ("SELL", Decimal("0.073359498"), Decimal("270.24"), Decimal("19.82")),
    ("BUY", Decimal("0.073812167"), Decimal("270.374"), Decimal("-19.96")),
    ("SELL", Decimal("0.073812167"), Decimal("269.93"), Decimal("19.92")),
]


def test_the_rounding_is_the_broker_cash_minus_the_exact_cash() -> None:
    exact = sum((-q * p if side == "BUY" else q * p) for side, q, p, _ in AMZN_FILLS)
    booked = sum(cash for *_, cash in AMZN_FILLS)
    assert exact == Decimal("-0.198124910640")
    assert booked == Decimal("-0.21")
    rounding = broker_cent_rounding((side, q, p) for side, q, p, _ in AMZN_FILLS)
    assert exact + rounding == booked


def test_whole_cent_fills_and_no_fills_need_no_rounding() -> None:
    assert broker_cent_rounding([]) == 0
    assert (
        broker_cent_rounding(
            [("BUY", Decimal("0.1"), Decimal("200")), ("SELL", Decimal("2"), Decimal("10.25"))]
        )
        == 0
    )


def test_half_a_cent_rounds_away_from_zero_on_the_traded_value() -> None:
    # 0.5 x 10.01 = 5.005 is booked as 5.01: a buy costs 0.005 more, a sell brings 0.005 more
    assert broker_cent_rounding([("BUY", Decimal("0.5"), Decimal("10.01"))]) == Decimal("-0.005")
    assert broker_cent_rounding([("SELL", Decimal("0.5"), Decimal("10.01"))]) == Decimal("0.005")


@pytest.fixture
def started(tmp_path: Path) -> Iterator[tuple[PaperEnvironment, Any, PaperRunner]]:
    db = Database(f"sqlite:///{(tmp_path / 'cash_rounding.db').as_posix()}")
    db.create_schema()
    clock = FakeClock(DAY.replace(hour=16))
    env, fake = _alpaca_env(db, clock)
    runner = PaperRunner(
        env, market_data=FakeMarketData(clock), schedule=FAST, log=lambda _line: None
    )
    runner.start()
    assert fake.stream_ready.wait(5), "fake trade stream did not start"
    with db.tx() as conn:
        for n, (side, quantity, price, _cash) in enumerate(AMZN_FILLS):
            env.oms._post_fill(
                conn,
                Fill(
                    broker=env.broker.broker,
                    account_id=ACCOUNT,
                    execution_id=f"amzn-{n}",
                    broker_order_id=f"order-{n}",
                    client_order_id=f"client-{n}",
                    instrument_id="XNAS:AMZN",
                    side=Side(side),
                    quantity=quantity,
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
    yield env, fake, runner
    runner.shutdown()
    env.close()
    db.dispose()


def test_four_fractional_fills_reconcile_against_cash_booked_in_cents(
    started: tuple[PaperEnvironment, Any, PaperRunner],
) -> None:
    _env, fake, runner = started
    fake.cash += sum(cash for *_, cash in AMZN_FILLS)  # the broker's books: -0.21
    result = runner._reconcile("paper-periodic")
    assert result.status == "PASSED", result.problems
    assert any("broker cent rounding of the fills -0.011875089360" in note for note in result.notes)


def test_a_difference_beyond_the_rounding_still_fails(
    started: tuple[PaperEnvironment, Any, PaperRunner],
) -> None:
    _env, fake, runner = started
    fake.cash += sum(cash for *_, cash in AMZN_FILLS) - Decimal(
        "0.02"
    )  # two cents nobody can explain
    result = runner._reconcile("paper-periodic")
    assert result.status == "FAILED"
    assert any(
        problem.startswith("cash delta mismatch: ledger -0.198124910640")
        for problem in result.problems
    )
