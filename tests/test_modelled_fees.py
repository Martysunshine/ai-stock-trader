"""Modelled fees on paper fills (ADR-0050): execution/oms.py, execution/reconciliation.py.

Alpaca's paper account reports every fill with fee 0 while the fee is pending, so the practice
ledger showed results production would not give: on 2026-11-10 three AMZN round trips read
-0.09 USD where the account's fee schedule takes another 0.12 USD.

With ``accrue_modelled_fees`` set on the account, each such fill is posted with the fee its
schedule charges, as a pending accrual. Reconciliation adds pending accruals back when it
compares cash, because the broker never charges them as such; a difference nobody can explain
still fails. The fees the broker does book, the night after, are the subject of ADR-0054 and
tests/test_broker_fee_charges.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from tests.test_paper_runtime import (
    ACCOUNT,
    CAPITAL,
    DAY,
    FAST,
    PAPER_YAML,
    ROOT,
    FakeClock,
    FakeMarketData,
    _alpaca_env,
    _config,
)
from trader.accounting.fees import load_fee_schedule
from trader.config.runtime_config import RuntimeConfig
from trader.domain.orders import FeeStatus, Fill
from trader.domain.types import Side
from trader.runtime.paper import PaperEnvironment, PaperRunner
from trader.storage.db import Database
from trader.storage.schema import fee_accruals, fills, ledger_entries

#: The first four fills of 2026-11-10: side, quantity, price, and the cash the broker booked.
AMZN_FILLS = [
    ("BUY", Decimal("0.073359498"), Decimal("272.494"), Decimal("-19.99")),
    ("SELL", Decimal("0.073359498"), Decimal("270.24"), Decimal("19.82")),
    ("BUY", Decimal("0.073812167"), Decimal("270.374"), Decimal("-19.96")),
    ("SELL", Decimal("0.073812167"), Decimal("269.93"), Decimal("19.92")),
]
EXACT_CASH = Decimal("-0.198124910640")
#: Two buys at 0.01 (CAT) and two sells at 0.03 (SEC, TAF and CAT, each rounded up to a cent).
FEES = Decimal("0.08")


def _fill(
    env: PaperEnvironment,
    n: int,
    side: str,
    quantity: Decimal,
    price: Decimal,
    *,
    order: str | None = None,
    fee: str = "0",
    status: FeeStatus = FeeStatus.PENDING,
) -> Fill:
    at = env.clock.now() + timedelta(seconds=n)
    return Fill(
        broker=env.broker.broker,
        account_id=ACCOUNT,
        execution_id=f"amzn-{n}",
        broker_order_id=order or f"order-{n}",
        client_order_id=f"client-{order or n}",
        instrument_id="XNAS:AMZN",
        side=Side(side),
        quantity=quantity,
        price=price,
        currency="USD",
        fee=Decimal(fee),
        fee_currency="USD",
        fee_status=status,
        exec_time=at,
        receipt_time=at,
    )


def _post(env: PaperEnvironment, *posted: Fill) -> None:
    with env.db.tx() as conn:
        for fill in posted:
            env.oms._post_fill(conn, fill, intent_id=None)


def _fees(env: PaperEnvironment) -> list[tuple[Decimal, str]]:
    with env.db.tx() as conn:
        rows = conn.execute(select(fills.c.fee, fills.c.fee_status).order_by(fills.c.id)).all()
    return [(Decimal(r.fee), r.fee_status) for r in rows]


@pytest.fixture
def started(tmp_path: Path) -> Iterator[tuple[PaperEnvironment, Any, PaperRunner]]:
    db = Database(f"sqlite:///{(tmp_path / 'modelled_fees.db').as_posix()}")
    db.create_schema()
    clock = FakeClock(DAY.replace(hour=16))
    env, fake = _alpaca_env(db, clock)
    runner = PaperRunner(
        env, market_data=FakeMarketData(clock), schedule=FAST, log=lambda _line: None
    )
    runner.start()
    assert fake.stream_ready.wait(5), "fake trade stream did not start"
    yield env, fake, runner
    runner.shutdown()
    env.close()
    db.dispose()


def _post_session(env: PaperEnvironment) -> None:
    _post(env, *(_fill(env, n, side, q, p) for n, (side, q, p, _cash) in enumerate(AMZN_FILLS)))


def test_the_paper_account_is_configured_to_accrue_modelled_fees() -> None:
    account = _config().runtime.accounts[0]
    assert account.accrue_modelled_fees
    raw = yaml.safe_load(PAPER_YAML.read_text(encoding="utf-8"))
    assert raw["accounts"][0]["accrue_modelled_fees"] is True


def test_a_fill_reported_without_a_fee_is_posted_with_the_schedule_fee(
    started: tuple[PaperEnvironment, Any, PaperRunner],
) -> None:
    env, _fake, _runner = started
    _post_session(env)
    assert _fees(env) == [
        (Decimal("0.01"), "PENDING"),
        (Decimal("0.03"), "PENDING"),
        (Decimal("0.01"), "PENDING"),
        (Decimal("0.03"), "PENDING"),
    ]
    with env.db.tx() as conn:
        assert env.ledger.cash(conn, ACCOUNT, "USD") == CAPITAL + EXACT_CASH - FEES
        assert env.ledger.realized_net_total(conn, ACCOUNT) == EXACT_CASH - FEES
        assert env.ledger.fee_totals(conn, ACCOUNT) == FEES
        assert env.ledger.pending_fee_accruals(conn, ACCOUNT) == FEES
        kinds = conn.execute(
            select(ledger_entries.c.kind).where(ledger_entries.c.fee.is_not(None))
        ).scalars()
        assert set(kinds) == {"FEE_ACCRUAL"}  # an estimate, never booked as a broker charge
        accruals = conn.execute(select(fee_accruals.c.status, fee_accruals.c.final)).all()
        assert [(a.status, a.final) for a in accruals] == [("PENDING", None)] * 4
        nets = conn.execute(
            select(fills.c.realized_gross, fills.c.realized_net).where(fills.c.side == "SELL")
        ).all()
    # each sell's net result carries its own 0.03 and the 0.01 of the buy it closes
    assert all(Decimal(n.realized_net) == Decimal(n.realized_gross) - Decimal("0.04") for n in nets)


def test_reconciliation_passes_against_a_broker_that_charges_nothing(
    started: tuple[PaperEnvironment, Any, PaperRunner],
) -> None:
    env, fake, runner = started
    _post_session(env)
    fake.cash += sum(cash for *_, cash in AMZN_FILLS)  # the paper broker's books: -0.21, no fee
    result = runner._reconcile("paper-periodic")
    assert result.status == "PASSED", result.problems
    assert any("fees accrued and not charged by the broker 0.08" in note for note in result.notes)
    assert any("broker cent rounding of the fills" in note for note in result.notes)


def test_two_cents_nobody_can_explain_still_fail(
    started: tuple[PaperEnvironment, Any, PaperRunner],
) -> None:
    env, fake, runner = started
    _post_session(env)
    fake.cash += sum(cash for *_, cash in AMZN_FILLS) - Decimal("0.02")
    result = runner._reconcile("paper-periodic")
    assert result.status == "FAILED"
    assert any(
        problem.startswith("cash delta mismatch") and "(accrued fees 0.08" in problem
        for problem in result.problems
    )


def test_an_order_filled_in_parts_costs_what_one_fill_would(
    started: tuple[PaperEnvironment, Any, PaperRunner],
) -> None:
    env, _fake, _runner = started
    half = Decimal("0.036")
    _post(
        env,
        _fill(env, 0, "BUY", half, Decimal("272.49"), order="buy"),
        _fill(env, 1, "BUY", half, Decimal("272.50"), order="buy"),
        _fill(env, 2, "SELL", half, Decimal("273.00"), order="sell"),
        _fill(env, 3, "SELL", half, Decimal("273.10"), order="sell"),
    )
    assert [fee for fee, _ in _fees(env)] == [
        Decimal("0.01"),
        Decimal(0),
        Decimal("0.03"),
        Decimal(0),
    ]
    with env.db.tx() as conn:
        assert env.ledger.pending_fee_accruals(conn, ACCOUNT) == Decimal("0.04")


def test_a_repeated_execution_is_posted_once_with_its_fee(
    started: tuple[PaperEnvironment, Any, PaperRunner],
) -> None:
    env, _fake, _runner = started
    buy = _fill(env, 0, "BUY", Decimal("0.07"), Decimal("272.49"))
    _post(env, buy)
    _post(env, buy)  # the stream and the REST activity both report it
    assert _fees(env) == [(Decimal("0.01"), "PENDING")]
    with env.db.tx() as conn:
        assert env.ledger.pending_fee_accruals(conn, ACCOUNT) == Decimal("0.01")


def test_a_fee_the_broker_reported_is_never_replaced(
    started: tuple[PaperEnvironment, Any, PaperRunner],
) -> None:
    env, _fake, _runner = started
    _post(
        env,
        # a broker's own pending estimate, and a final charge of nothing
        _fill(env, 0, "BUY", Decimal("0.07"), Decimal("272.49"), fee="0.02"),
        _fill(env, 1, "SELL", Decimal("0.07"), Decimal("273"), status=FeeStatus.FINAL),
    )
    assert _fees(env) == [(Decimal("0.02"), "PENDING"), (Decimal(0), "FINAL")]


def test_without_the_setting_a_fill_is_posted_as_the_broker_reported_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    accounts = config.runtime.accounts
    off = replace(
        config,
        runtime=config.runtime.model_copy(
            update={
                "accounts": type(accounts)(
                    [accounts[0].model_copy(update={"accrue_modelled_fees": False})]
                )
            }
        ),
    )
    monkeypatch.setattr("tests.test_paper_runtime._config", lambda: off)
    db = Database(f"sqlite:///{(tmp_path / 'off.db').as_posix()}")
    db.create_schema()
    env, _fake = _alpaca_env(db, FakeClock(DAY.replace(hour=16)))
    try:
        assert env.oms.modelled_fees == {}
        _post_session(env)
        assert _fees(env) == [(Decimal(0), "PENDING")] * 4
        with env.db.tx() as conn:
            assert env.ledger.cash(conn, ACCOUNT, "USD") == CAPITAL + EXACT_CASH
            assert env.ledger.pending_fee_accruals(conn, ACCOUNT) == 0
    finally:
        env.close()
        db.dispose()


def test_an_unknown_schedule_never_stops_a_fill_from_being_posted(
    started: tuple[PaperEnvironment, Any, PaperRunner],
) -> None:
    env, _fake, _runner = started
    unknown = load_fee_schedule(ROOT / "configs" / "fees" / "alpaca_us_equities.yaml")
    assert not unknown.known
    env.oms.modelled_fees[ACCOUNT] = unknown
    _post(env, _fill(env, 0, "BUY", Decimal("0.07"), Decimal("272.49")))
    assert _fees(env) == [(Decimal(0), "PENDING")]  # a fill is a fact; an unknown fee is not one


def test_the_setting_is_refused_outside_paper() -> None:
    replay = yaml.safe_load((ROOT / "configs" / "replay.yaml").read_text(encoding="utf-8"))
    RuntimeConfig.model_validate(replay)
    replay["accounts"][0]["accrue_modelled_fees"] = True
    # pydantic reports the rule's DomainError as a validation error, itself a ValueError
    with pytest.raises(ValueError, match="accrue_modelled_fees is a paper setting"):
        RuntimeConfig.model_validate(replay)
