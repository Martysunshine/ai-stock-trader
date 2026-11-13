"""One minimum-size round trip on the Capital.com demo account (ADR-0056).

``python -m trader.practice certify`` proves the practice trader's order path before the trader
runs: it reads the account's hedging setting and the Bitcoin CFD's quote, journals a PLAN, opens
the smallest Bitcoin position with a stop and a target held at the broker, reads the
confirmation and the position, closes it, reads the close and the activity record, and journals
the OPEN and the CLOSE at the broker's levels. The trade is labelled a certification, not a
setup's trade. Every broker answer is written, without account identifiers, to
``runs/practice/certification/<time>.json``; the parsers' tests are built from those answers.

If a step fails after the order was accepted, the position keeps its stop and target at the
broker, and the error says so.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from trader.data.sources.http import TransportError
from trader.domain.types import iso_utc
from trader.ops.clock import SleepingClock
from trader.practice.capital_orders import CapitalOrders, Confirmation, OrderError
from trader.practice.journal import REPO_ROOT, PracticeJournal

EPIC = "BTCUSD"
SETUP = "certification"
CONFIRM_WAIT_SECONDS = 10
_PRIVATE = ("accountid", "accountname", "clientid", "account")


def _scrub(value: Any) -> Any:
    """The answer without account identifiers."""
    if isinstance(value, Mapping):
        return {k: _scrub(v) for k, v in value.items() if str(k).lower() not in _PRIVATE}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def wait_confirm(
    orders: CapitalOrders, reference: str, clock: SleepingClock, seconds: int = CONFIRM_WAIT_SECONDS
) -> Confirmation | None:
    """Poll the confirmation for up to ``seconds``; ``None`` if it never arrives."""
    for _ in range(seconds):
        try:
            return orders.confirm(reference)
        except TransportError as exc:
            if exc.status not in (404, 400):  # not yet known to the broker: wait and ask again
                raise
            clock.sleep(1)
    return None


def certify(
    orders: CapitalOrders,
    journal: PracticeJournal,
    clock: SleepingClock,
    echo: Callable[[str], None] = print,
    out_dir: Path | None = None,
) -> Path:
    record: dict[str, Any] = {"epic": EPIC, "started_at": iso_utc(clock.now())}
    prefs = orders.preferences()
    record["preferences"] = _scrub(prefs)
    echo(f"hedging mode: {'on' if prefs.get('hedgingMode') else 'off'}")
    quote = orders.market(EPIC)
    record["market"] = _scrub(quote.raw)
    echo(
        f"{EPIC} bid {quote.bid} offer {quote.offer} ({quote.spread_bps:.1f} bps) {quote.status}, "
        f"minimum size {quote.min_size}"
    )
    if not quote.tradeable:
        raise OrderError(f"{EPIC} is not tradeable now ({quote.status})")
    rule = journal.rules.epic(EPIC)
    size = max(rule.min_size, quote.min_size or rule.min_size)
    step = Decimal(1).scaleb(-rule.decimals)
    entry = quote.offer
    stop = (entry * Decimal("0.97")).quantize(step, rounding=ROUND_FLOOR)
    target = (entry * Decimal("1.02")).quantize(step, rounding=ROUND_CEILING)
    now = clock.now()
    trade_id = f"cert.{EPIC}.{now.strftime('%Y%m%dT%H%M%SZ')}"
    journal.append(
        "PLAN",
        {
            "trade_id": trade_id,
            "epic": EPIC,
            "direction": "BUY",
            "size": str(size),
            "entry": str(entry),
            "stop": str(stop),
            "target": str(target),
            "thesis": "certification of the practice trader's order path, not a trading view",
            "setup": "order-path certification (ADR-0056)",
            "timeframe": "minutes",
            "invalidation": "closed at once by design",
            "max_hold_hours": 1,
            "setup_key": SETUP,
        },
        now,
        actor="trader",
    )
    reference = orders.open_position(EPIC, "BUY", size, stop=stop, target=target)
    record["open_reference"] = reference
    journal.append(
        "NOTE",
        {"text": f"order sent, deal reference {reference}", "trade_id": trade_id},
        clock.now(),
        actor="trader",
    )
    confirmation = wait_confirm(orders, reference, clock)
    if confirmation is None:
        raise OrderError(
            f"no confirmation for {reference} after {CONFIRM_WAIT_SECONDS} s: check the demo "
            "account's positions; the trade is journalled as planned, not open"
        )
    record["open_confirm"] = _scrub(confirmation.raw)
    if not confirmation.accepted or confirmation.deal_id is None or confirmation.level is None:
        journal.append(
            "CANCEL",
            {"trade_id": trade_id, "reason": f"broker rejected: {confirmation.reason}"},
            clock.now(),
            actor="trader",
        )
        raise OrderError(f"the broker rejected the order: {confirmation.reason}")
    deal_id = confirmation.deal_id
    journal.append(
        "OPEN",
        {
            "trade_id": trade_id,
            "deal_id": deal_id,
            "deal_reference": reference,
            "fill_level": str(confirmation.level),
            "size": str(confirmation.size or size),
        },
        clock.now(),
        actor="trader",
    )
    echo(f"opened {deal_id} at {confirmation.level}")
    positions = orders.positions()
    record["positions_after_open"] = [
        _scrub(
            {
                "dealId": p.deal_id,
                "dealReference": p.deal_reference,
                "epic": p.epic,
                "direction": p.direction,
                "size": str(p.size),
                "level": str(p.level),
                "stopLevel": str(p.stop),
                "profitLevel": str(p.target),
                "createdDateUTC": p.created_utc,
            }
        )
        for p in positions
    ]
    mine = [p for p in positions if p.deal_id == deal_id]
    if not mine:
        raise OrderError(f"position {deal_id} is not in the broker's list; it keeps its stop")
    echo(f"broker holds stop {mine[0].stop} target {mine[0].target}")
    closing = orders.close_position(deal_id)
    record["close_reference"] = closing
    closed = wait_confirm(orders, closing, clock)
    if closed is None or closed.level is None:
        raise OrderError(f"no level for the close {closing}: check the demo account")
    record["close_confirm"] = _scrub(closed.raw)
    pnl = (closed.level - confirmation.level) * (confirmation.size or size)
    record["activity"] = _scrub(
        orders.activity(now - timedelta(minutes=5), clock.now() + timedelta(minutes=1))
    )
    journal.append(
        "CLOSE",
        {
            "trade_id": trade_id,
            "deal_id": deal_id,
            "level": str(closed.level),
            "reason": "MANUAL",
            "pnl_usd": str(pnl.quantize(Decimal("0.0001"))),
            "notes": "certification round trip; the result is the spread",
        },
        clock.now(),
        actor="trader",
    )
    echo(f"closed at {closed.level}: {pnl:+.4f} USD (the spread)")
    record["finished_at"] = iso_utc(clock.now())
    folder = out_dir or REPO_ROOT / "runs" / "practice" / "certification"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    echo(f"broker answers written to {out}")
    return out


def run_certify(echo: Callable[[str], None] = print) -> int:
    from trader.config.secrets import require_capital_demo
    from trader.data.sources.capital_prices import CapitalPrices
    from trader.data.sources.http import UrllibTransport
    from trader.ops.clock import SystemClock

    clock = SystemClock()
    transport = UrllibTransport(min_interval_seconds=0.25, max_attempts=3)
    prices = CapitalPrices(transport, require_capital_demo, clock=clock)
    orders = CapitalOrders(prices, transport)
    certify(orders, PracticeJournal(venue="crypto-cfd"), clock, echo)
    return 0


__all__ = ["certify", "run_certify", "wait_confirm"]
