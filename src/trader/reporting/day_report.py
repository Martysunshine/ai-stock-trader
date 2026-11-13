"""One session of an account, read from its recorded fills. Read-only.

A round trip runs from the fill that opens a position to the fill that brings it back to zero,
and it belongs to the session of that closing fill. Gross result uses the ledger's own method
(average cost), so it equals ``fills.realized_gross``.

Two kinds of fees are kept apart:

* **booked** - the fee recorded on the fill (a broker's charge, or an estimate the ledger
  accrued);
* **modelled, not booked** - what the account's fee schedule charges for an order whose fills
  carry no fee although the fee is still pending. Alpaca reports every fill without a fee
  (it books a day's fees the following night, ADR-0054), so before fee accrual was switched
  on every practice fill is of this kind.

The net result subtracts both, so a day reads the same before and after the switch.

The losing streak follows the console (``ui/shared/trades.ts``): sessions with at least one
closed round trip, counted back from the reported session while their net result is negative.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Connection, select

from trader.accounting.fees import FeeSchedule
from trader.domain.orders import FeeStatus
from trader.domain.types import ZERO, Side
from trader.storage.schema import fills, order_intents

MONEY = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class FillRow:
    """The columns of one recorded fill that the report needs."""

    instrument_id: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_status: FeeStatus
    exec_time: datetime
    broker_order_id: str
    purpose: str | None = None


@dataclass(frozen=True, slots=True)
class RoundTrip:
    instrument_id: str
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    gross: Decimal
    fees_booked: Decimal
    fees_modelled: Decimal
    exit_purposes: tuple[str, ...] = ()

    @property
    def net(self) -> Decimal:
        return self.gross - self.fees_booked - self.fees_modelled


@dataclass(frozen=True, slots=True)
class OpenPosition:
    instrument_id: str
    opened_at: datetime
    quantity: Decimal
    entry_price: Decimal


@dataclass(frozen=True, slots=True)
class DayReport:
    account_id: str
    session_date: date
    round_trips: tuple[RoundTrip, ...]
    open_positions: tuple[OpenPosition, ...]
    losing_streak: int
    fee_schedule_id: str | None

    @property
    def gross(self) -> Decimal:
        return sum((t.gross for t in self.round_trips), start=ZERO)

    @property
    def fees_booked(self) -> Decimal:
        return sum((t.fees_booked for t in self.round_trips), start=ZERO)

    @property
    def fees_modelled(self) -> Decimal:
        return sum((t.fees_modelled for t in self.round_trips), start=ZERO)

    @property
    def net(self) -> Decimal:
        return sum((t.net for t in self.round_trips), start=ZERO)

    @property
    def winners(self) -> int:
        return sum(1 for t in self.round_trips if t.net > 0)

    @property
    def losers(self) -> int:
        return sum(1 for t in self.round_trips if t.net <= 0)


@dataclass(slots=True)
class _Cycle:
    """A position being folded: from its opening fill until it is flat again."""

    opened_at: datetime
    quantity: Decimal = ZERO
    basis: Decimal = ZERO
    bought: Decimal = ZERO
    bought_value: Decimal = ZERO
    sold: Decimal = ZERO
    sold_value: Decimal = ZERO
    gross: Decimal = ZERO
    fees_booked: Decimal = ZERO
    orders: dict[str, list[FillRow]] = field(default_factory=dict)
    exit_purposes: list[str] = field(default_factory=list)


def _modelled_fees(orders: Mapping[str, list[FillRow]], schedule: FeeSchedule | None) -> Decimal:
    """Schedule fees of the orders none of whose fills carries a fee while it is still pending.

    A schedule charges per order, so an order filled in parts is priced once, on its total.
    """
    if schedule is None or not schedule.known:
        return ZERO
    total = ZERO
    for rows in orders.values():
        if any(r.fee != 0 or r.fee_status is not FeeStatus.PENDING for r in rows):
            continue
        quantity = sum((r.quantity for r in rows), start=ZERO)
        notional = sum((r.quantity * r.price for r in rows), start=ZERO)
        total += schedule.fee(rows[0].side, quantity, notional)
    return total


def fold_round_trips(
    rows: Iterable[FillRow], schedule: FeeSchedule | None
) -> tuple[list[RoundTrip], list[OpenPosition]]:
    """Fold fills (in execution order) into closed round trips and the positions still open."""
    cycles: dict[str, _Cycle] = {}
    closed: list[RoundTrip] = []
    for row in rows:
        cycle = cycles.get(row.instrument_id)
        value = row.quantity * row.price
        if row.side is Side.BUY:
            if cycle is None:
                cycle = cycles[row.instrument_id] = _Cycle(opened_at=row.exec_time)
            cycle.quantity += row.quantity
            cycle.basis += value
            cycle.bought += row.quantity
            cycle.bought_value += value
        else:
            if cycle is None or row.quantity > cycle.quantity:
                # The ledger refuses such a fill; a report must not invent a position for it.
                continue
            sold_basis = (
                cycle.basis
                if row.quantity == cycle.quantity
                else cycle.basis * row.quantity / cycle.quantity
            )
            cycle.gross += value - sold_basis
            cycle.basis -= sold_basis
            cycle.quantity -= row.quantity
            cycle.sold += row.quantity
            cycle.sold_value += value
            if row.purpose and row.purpose not in cycle.exit_purposes:
                cycle.exit_purposes.append(row.purpose)
        cycle.fees_booked += row.fee
        cycle.orders.setdefault(row.broker_order_id, []).append(row)
        if row.side is Side.SELL and cycle.quantity == 0:
            closed.append(
                RoundTrip(
                    instrument_id=row.instrument_id,
                    opened_at=cycle.opened_at,
                    closed_at=row.exec_time,
                    quantity=cycle.bought,
                    entry_price=cycle.bought_value / cycle.bought,
                    exit_price=cycle.sold_value / cycle.sold,
                    gross=cycle.gross,
                    fees_booked=cycle.fees_booked,
                    fees_modelled=_modelled_fees(cycle.orders, schedule),
                    exit_purposes=tuple(cycle.exit_purposes),
                )
            )
            del cycles[row.instrument_id]
    still_open = [
        OpenPosition(
            instrument_id=iid,
            opened_at=c.opened_at,
            quantity=c.quantity,
            entry_price=c.basis / c.quantity,
        )
        for iid, c in sorted(cycles.items())
    ]
    return closed, still_open


def losing_streak(
    round_trips: Iterable[RoundTrip], session_date_of: Callable[[datetime], date], upto: date
) -> int:
    """Sessions in a row, ending at ``upto``, whose closed round trips lost money in total.

    Sessions without a closed round trip neither add to the run nor end it.
    """
    by_session: dict[date, Decimal] = {}
    for trip in round_trips:
        day = session_date_of(trip.closed_at)
        if day <= upto:
            by_session[day] = by_session.get(day, ZERO) + trip.net
    streak = 0
    for day in sorted(by_session, reverse=True):
        if by_session[day] >= 0:
            break
        streak += 1
    return streak


def load_fill_rows(conn: Connection, account_id: str) -> list[FillRow]:
    """Every fill of the account in execution order, with the purpose of its order if known."""
    rows = conn.execute(
        select(
            fills.c.instrument_id,
            fills.c.side,
            fills.c.quantity,
            fills.c.price,
            fills.c.fee,
            fills.c.fee_status,
            fills.c.exec_time,
            fills.c.broker_order_id,
            order_intents.c.purpose,
        )
        .select_from(fills.outerjoin(order_intents, fills.c.intent_id == order_intents.c.intent_id))
        .where(fills.c.account_id == account_id)
        .order_by(fills.c.exec_time, fills.c.id)
    ).all()
    return [
        FillRow(
            instrument_id=r.instrument_id,
            side=Side(r.side),
            quantity=Decimal(r.quantity),
            price=Decimal(r.price),
            fee=Decimal(r.fee),
            fee_status=FeeStatus(r.fee_status),
            exec_time=r.exec_time,
            broker_order_id=r.broker_order_id,
            purpose=r.purpose,
        )
        for r in rows
    ]


def day_report(
    conn: Connection,
    *,
    account_id: str,
    session_date: date,
    session_date_of: Callable[[datetime], date],
    schedule: FeeSchedule | None,
) -> DayReport:
    """The account's round trips closed in ``session_date`` and its position at that close."""
    rows = [
        r for r in load_fill_rows(conn, account_id) if session_date_of(r.exec_time) <= session_date
    ]
    closed, still_open = fold_round_trips(rows, schedule)
    return DayReport(
        account_id=account_id,
        session_date=session_date,
        round_trips=tuple(t for t in closed if session_date_of(t.closed_at) == session_date),
        open_positions=tuple(still_open),
        losing_streak=losing_streak(closed, session_date_of, session_date),
        fee_schedule_id=schedule.schedule_id if schedule is not None and schedule.known else None,
    )


def latest_session_with_fills(
    conn: Connection, account_id: str, session_date_of: Callable[[datetime], date]
) -> date | None:
    """The session of the account's most recent fill, or ``None`` when it has none."""
    latest = conn.execute(
        select(fills.c.exec_time)
        .where(fills.c.account_id == account_id)
        .order_by(fills.c.exec_time.desc())
        .limit(1)
    ).scalar()
    return None if latest is None else session_date_of(latest)


def _money(value: Decimal) -> str:
    return f"{value.quantize(MONEY):+}"


def _plain(value: Decimal) -> str:
    return f"{value.quantize(MONEY)}"


def _quantity(value: Decimal) -> str:
    """Trailing zeros dropped, never in exponent form (``normalize`` alone prints 1E+2)."""
    return format(value.normalize(), "f")


def _symbol(instrument_id: str) -> str:
    return instrument_id.rsplit(":", 1)[-1]


def _exit_words(purposes: tuple[str, ...]) -> str:
    names = {
        "EXIT_STOP": "stop",
        "EXIT_TARGET": "target",
        "EXIT_TIME": "time limit",
        "EXIT_SESSION": "end of session",
        "EXIT_RISK_REDUCTION": "risk reduction",
        "EXIT_LIQUIDATION": "liquidation",
    }
    return ", ".join(names.get(p, p.lower()) for p in purposes) or "exit"


def render_day_report(report: DayReport) -> list[str]:
    """The report as plain lines: one per round trip, then the totals."""
    lines = [f"account {report.account_id}, session {report.session_date.isoformat()}"]
    if not report.round_trips:
        lines.append("no round trip closed in this session")
    for t in report.round_trips:
        lines.append(
            f"  {_symbol(t.instrument_id):<6} {t.opened_at:%H:%M}-{t.closed_at:%H:%M} UTC  "
            f"{_quantity(t.quantity)} at {t.entry_price.quantize(MONEY)} -> "
            f"{t.exit_price.quantize(MONEY)} ({_exit_words(t.exit_purposes)})  "
            f"gross {_money(t.gross)}  net {_money(t.net)}"
        )
    if report.round_trips:
        lines.append(
            f"round trips {len(report.round_trips)}: {report.winners} won, {report.losers} lost"
        )
        lines.append(f"gross          {_money(report.gross)}")
        lines.append(f"fees booked    {_plain(report.fees_booked)}")
        if report.fees_modelled:
            lines.append(
                f"fees modelled  {_plain(report.fees_modelled)} (not booked; schedule "
                f"{report.fee_schedule_id})"
            )
        lines.append(f"net            {_money(report.net)}")
    if report.open_positions:
        lines.append("open at the close:")
        for p in report.open_positions:
            lines.append(
                f"  {_symbol(p.instrument_id):<6} {_quantity(p.quantity)} at "
                f"{p.entry_price.quantize(MONEY)} since {p.opened_at:%Y-%m-%d %H:%M} UTC"
            )
    else:
        lines.append("open at the close: none")
    lines.append(f"losing sessions in a row: {report.losing_streak}")
    return lines
