"""Cash/position ledger, fee accruals, settlement lots and realised P&L.

Rules (architecture.md §5, strategy_and_profitability.md §1, §9):

* Positions, cost basis, fees and realised P&L change only from confirmed fills.
* A fill is posted at most once (unique ``(broker, account_id, execution_id)``).
* Reservations are not expenses: this module never touches reservations.
* External cash flows are recorded separately from trading P&L.
* ``realized_net = qty * (exit - entry) - entry_fees - exit_fees`` per closed quantity.
* ``capital_base = initial + contributions + realized_net + distributions - withdrawals``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Connection, insert, select, update

from trader.domain.account import CashFlowKind
from trader.domain.orders import FeeStatus, Fill
from trader.domain.types import ZERO, DomainError, Side, Track
from trader.storage.db import Database
from trader.storage.helpers import insert_ignore
from trader.storage.schema import (
    accounts,
    cash_lots,
    fee_accruals,
    fills,
    ledger_entries,
    positions,
)


class LedgerInvariantError(DomainError):
    """A confirmed broker fact contradicts the local ledger (e.g. selling more than owned)."""


@dataclass(frozen=True, slots=True)
class PositionView:
    account_id: str
    instrument_id: str
    owner_track: str
    currency: str
    quantity: Decimal
    cost_basis: Decimal
    open_entry_fees: Decimal
    realized_gross: Decimal
    realized_net: Decimal
    opened_at: datetime | None

    @property
    def avg_price(self) -> Decimal:
        return self.cost_basis / self.quantity if self.quantity > 0 else ZERO


@dataclass(frozen=True, slots=True)
class PostedFill:
    posted: bool
    realized_gross: Decimal | None = None
    realized_net: Decimal | None = None


class Ledger:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ accounts
    def bump_version(self, conn: Connection, account_id: str) -> int:
        conn.execute(
            update(accounts)
            .where(accounts.c.account_id == account_id)
            .values(version=accounts.c.version + 1)
        )
        return self.version(conn, account_id)

    def version(self, conn: Connection, account_id: str) -> int:
        return int(
            conn.execute(
                select(accounts.c.version).where(accounts.c.account_id == account_id)
            ).scalar_one()
        )

    def _entry(
        self,
        conn: Connection,
        *,
        account_id: str,
        at: datetime,
        kind: str,
        currency: str,
        cash_delta: Decimal,
        ref: str,
        instrument_id: str | None = None,
        quantity_delta: Decimal | None = None,
        price: Decimal | None = None,
        fee: Decimal | None = None,
        realized_net: Decimal | None = None,
    ) -> None:
        seq = self.db.next_counter(conn, f"ledger:{account_id}")
        conn.execute(
            insert(ledger_entries).values(
                seq=seq,
                account_id=account_id,
                entry_time=at,
                kind=kind,
                currency=currency,
                cash_delta=cash_delta,
                instrument_id=instrument_id,
                quantity_delta=quantity_delta,
                price=price,
                fee=fee,
                realized_net=realized_net,
                ref=ref,
            )
        )

    def record_cash_flow(
        self,
        conn: Connection,
        *,
        account_id: str,
        kind: CashFlowKind,
        amount: Decimal,
        currency: str,
        at: datetime,
        ref: str,
    ) -> None:
        if amount <= 0:
            raise DomainError("cash flow amounts are positive; the kind sets the direction")
        sign = Decimal(-1) if kind is CashFlowKind.WITHDRAWAL else Decimal(1)
        self._entry(
            conn,
            account_id=account_id,
            at=at,
            kind=f"FLOW_{kind.value}",
            currency=currency,
            cash_delta=sign * amount,
            ref=ref,
        )
        self.bump_version(conn, account_id)

    # ------------------------------------------------------------------ queries
    def cash(self, conn: Connection, account_id: str, currency: str) -> Decimal:
        """Exact cash balance: summed in Decimal (SQL SUM over SQLite TEXT would be a float)."""
        rows = conn.execute(
            select(ledger_entries.c.cash_delta).where(
                ledger_entries.c.account_id == account_id,
                ledger_entries.c.currency == currency,
            )
        ).scalars()
        return sum((Decimal(r) for r in rows), start=ZERO)

    def unsettled(self, conn: Connection, account_id: str, currency: str, on: date) -> Decimal:
        rows = conn.execute(
            select(cash_lots.c.amount).where(
                cash_lots.c.account_id == account_id,
                cash_lots.c.currency == currency,
                cash_lots.c.settles_on > on,
            )
        ).scalars()
        return sum((Decimal(r) for r in rows if Decimal(r) > 0), start=ZERO)

    def settled_cash(self, conn: Connection, account_id: str, currency: str, on: date) -> Decimal:
        return self.cash(conn, account_id, currency) - self.unsettled(
            conn, account_id, currency, on
        )

    def position(
        self, conn: Connection, account_id: str, instrument_id: str
    ) -> PositionView | None:
        row = conn.execute(
            select(positions).where(
                positions.c.account_id == account_id, positions.c.instrument_id == instrument_id
            )
        ).first()
        return _position_view(row) if row is not None else None

    def open_positions(self, conn: Connection, account_ids: list[str]) -> list[PositionView]:
        rows = conn.execute(
            select(positions)
            .where(positions.c.account_id.in_(account_ids))
            .order_by(positions.c.account_id, positions.c.instrument_id)
        ).all()
        views = [_position_view(r) for r in rows]
        return [v for v in views if v.quantity != 0]

    def all_positions(self, conn: Connection, account_id: str) -> list[PositionView]:
        rows = conn.execute(
            select(positions)
            .where(positions.c.account_id == account_id)
            .order_by(positions.c.instrument_id)
        ).all()
        return [_position_view(r) for r in rows]

    def flows(self, conn: Connection, account_id: str) -> dict[str, Decimal]:
        rows = conn.execute(
            select(ledger_entries.c.kind, ledger_entries.c.cash_delta).where(
                ledger_entries.c.account_id == account_id,
                ledger_entries.c.kind.like("FLOW_%"),
            )
        ).all()
        out: dict[str, Decimal] = {}
        for kind, delta in rows:
            out[kind] = out.get(kind, ZERO) + Decimal(delta)
        return out

    def net_external_flows(self, conn: Connection, account_id: str) -> Decimal:
        """Deposits/initial capital minus withdrawals (not trading P&L)."""
        return sum(self.flows(conn, account_id).values(), start=ZERO)

    def realized_net_total(self, conn: Connection, account_id: str) -> Decimal:
        rows = conn.execute(
            select(fills.c.realized_net).where(
                fills.c.account_id == account_id, fills.c.realized_net.is_not(None)
            )
        ).scalars()
        adjustments = conn.execute(
            select(ledger_entries.c.realized_net).where(
                ledger_entries.c.account_id == account_id,
                ledger_entries.c.kind == "FEE_ADJUSTMENT",
                ledger_entries.c.realized_net.is_not(None),
            )
        ).scalars()
        return sum((Decimal(r) for r in rows), start=ZERO) + sum(
            (Decimal(a) for a in adjustments), start=ZERO
        )

    def capital_base(self, conn: Connection, account_id: str) -> Decimal:
        flows = self.flows(conn, account_id)
        contributions = flows.get("FLOW_INITIAL_CAPITAL", ZERO) + flows.get("FLOW_DEPOSIT", ZERO)
        distributions = flows.get("FLOW_DISTRIBUTION", ZERO)
        withdrawals = -flows.get("FLOW_WITHDRAWAL", ZERO)
        return (
            contributions + self.realized_net_total(conn, account_id) + distributions - withdrawals
        )

    def fee_totals(self, conn: Connection, account_id: str) -> Decimal:
        rows = conn.execute(
            select(ledger_entries.c.cash_delta).where(
                ledger_entries.c.account_id == account_id,
                ledger_entries.c.kind.in_(("FEE", "FEE_ACCRUAL", "FEE_ADJUSTMENT")),
            )
        ).scalars()
        return -sum((Decimal(r) for r in rows), start=ZERO)

    def pending_fee_accruals(self, conn: Connection, account_id: str) -> Decimal:
        """Fees the ledger has taken from cash as estimates the broker has not charged yet.

        Reconciliation adds them back when it compares cash with the broker: an accrual is in
        the ledger from the fill, but never in the broker's cash as such. Alpaca books a day's
        fees the following night as its own activities, on the paper account too; those are
        read from the broker and subtracted separately (ADR-0054), and no accrual is finalised.
        """
        rows = conn.execute(
            select(fee_accruals.c.estimated).where(
                fee_accruals.c.account_id == account_id, fee_accruals.c.status == "PENDING"
            )
        ).scalars()
        return sum((Decimal(r) for r in rows), start=ZERO)

    # ------------------------------------------------------------------ posting
    def post_fill(
        self,
        conn: Connection,
        fill: Fill,
        *,
        intent_id: str | None,
        owner_track: Track,
        settles_on: date,
        trade_date: date,
    ) -> PostedFill:
        """Post one confirmed execution. Idempotent: a duplicate execution ID is ignored."""
        if fill.fee_currency != fill.currency:
            raise LedgerInvariantError("fee currency differs from trade currency (not supported)")
        inserted = insert_ignore(
            conn,
            fills,
            {
                "broker": fill.broker,
                "account_id": fill.account_id,
                "execution_id": fill.execution_id,
                "intent_id": intent_id,
                "broker_order_id": fill.broker_order_id,
                "client_order_id": fill.client_order_id,
                "instrument_id": fill.instrument_id,
                "side": fill.side.value,
                "quantity": fill.quantity,
                "price": fill.price,
                "currency": fill.currency,
                "fee": fill.fee,
                "fee_currency": fill.fee_currency,
                "fee_status": fill.fee_status.value,
                "exec_time": fill.exec_time,
                "receipt_time": fill.receipt_time,
            },
            ["broker", "account_id", "execution_id"],
        )
        if not inserted:
            return PostedFill(posted=False)

        current = self.position(conn, fill.account_id, fill.instrument_id)
        qty = current.quantity if current else ZERO
        basis = current.cost_basis if current else ZERO
        entry_fees = current.open_entry_fees if current else ZERO
        realized_gross_total = current.realized_gross if current else ZERO
        realized_net_total = current.realized_net if current else ZERO
        opened_at = current.opened_at if current else None
        trade_value = fill.quantity * fill.price
        realized_gross: Decimal | None = None
        realized_net: Decimal | None = None

        if fill.side is Side.BUY:
            new_qty = qty + fill.quantity
            new_basis = basis + trade_value
            new_entry_fees = entry_fees + fill.fee
            cash_delta = -trade_value
            if qty == 0:
                opened_at = fill.exec_time
        else:
            if fill.quantity > qty:
                raise LedgerInvariantError(
                    f"sell of {fill.quantity} exceeds owned {qty} for {fill.instrument_id}"
                )
            if fill.quantity == qty:
                sold_basis, fee_alloc = basis, entry_fees
            else:
                sold_basis = basis * fill.quantity / qty
                fee_alloc = entry_fees * fill.quantity / qty
            realized_gross = trade_value - sold_basis
            realized_net = realized_gross - fill.fee - fee_alloc
            new_qty = qty - fill.quantity
            new_basis = basis - sold_basis
            new_entry_fees = entry_fees - fee_alloc
            realized_gross_total += realized_gross
            realized_net_total += realized_net
            cash_delta = trade_value
            if new_qty == 0:
                new_basis = ZERO
                new_entry_fees = ZERO
                opened_at = None
            conn.execute(
                update(fills)
                .where(
                    fills.c.broker == fill.broker,
                    fills.c.account_id == fill.account_id,
                    fills.c.execution_id == fill.execution_id,
                )
                .values(realized_gross=realized_gross, realized_net=realized_net)
            )
            conn.execute(
                insert(cash_lots).values(
                    account_id=fill.account_id,
                    currency=fill.currency,
                    amount=trade_value - fill.fee,
                    trade_date=trade_date,
                    settles_on=settles_on,
                    source=f"fill:{fill.execution_id}",
                )
            )

        values = {
            "owner_track": owner_track.value,
            "currency": fill.currency,
            "quantity": new_qty,
            "cost_basis": new_basis,
            "open_entry_fees": new_entry_fees,
            "realized_gross": realized_gross_total,
            "realized_net": realized_net_total,
            "opened_at": opened_at,
            "updated_at": fill.receipt_time,
        }
        if current is None:
            conn.execute(
                insert(positions).values(
                    account_id=fill.account_id, instrument_id=fill.instrument_id, **values
                )
            )
        else:
            if current.owner_track != owner_track.value:
                raise LedgerInvariantError("a position has exactly one execution/P&L owner track")
            conn.execute(
                update(positions)
                .where(
                    positions.c.account_id == fill.account_id,
                    positions.c.instrument_id == fill.instrument_id,
                )
                .values(**values)
            )

        ref = f"fill:{fill.execution_id}"
        self._entry(
            conn,
            account_id=fill.account_id,
            at=fill.exec_time,
            kind=f"TRADE_{fill.side.value}",
            currency=fill.currency,
            cash_delta=cash_delta,
            ref=ref,
            instrument_id=fill.instrument_id,
            quantity_delta=fill.quantity if fill.side is Side.BUY else -fill.quantity,
            price=fill.price,
            realized_net=realized_net,
        )
        if fill.fee > 0 or fill.fee_status is FeeStatus.PENDING:
            self._entry(
                conn,
                account_id=fill.account_id,
                at=fill.exec_time,
                kind="FEE" if fill.fee_status is FeeStatus.FINAL else "FEE_ACCRUAL",
                currency=fill.fee_currency,
                cash_delta=-fill.fee,
                ref=ref,
                instrument_id=fill.instrument_id,
                fee=fill.fee,
            )
        if fill.fee_status is FeeStatus.PENDING:
            conn.execute(
                insert(fee_accruals).values(
                    broker=fill.broker,
                    account_id=fill.account_id,
                    execution_id=fill.execution_id,
                    estimated=fill.fee,
                    final=None,
                    status="PENDING",
                    updated_at=fill.receipt_time,
                )
            )
        self.bump_version(conn, fill.account_id)
        return PostedFill(posted=True, realized_gross=realized_gross, realized_net=realized_net)

    def finalize_fee(
        self,
        conn: Connection,
        *,
        broker: str,
        account_id: str,
        execution_id: str,
        final_fee: Decimal,
        currency: str,
        at: datetime,
    ) -> Decimal:
        """Replace an accrued estimate with the final charge; returns the posted difference."""
        row = conn.execute(
            select(fee_accruals).where(
                fee_accruals.c.broker == broker,
                fee_accruals.c.account_id == account_id,
                fee_accruals.c.execution_id == execution_id,
            )
        ).first()
        if row is None:
            raise LedgerInvariantError(f"no fee accrual for execution {execution_id}")
        if row.status == "FINAL":
            return ZERO
        difference = final_fee - Decimal(row.estimated)
        conn.execute(
            update(fee_accruals)
            .where(fee_accruals.c.id == row.id)
            .values(final=final_fee, status="FINAL", updated_at=at)
        )
        if difference != 0:
            # A sell's realised P&L absorbs its own fee difference; a buy's difference is
            # attributed when the position closes, so it is realised immediately here as well.
            self._entry(
                conn,
                account_id=account_id,
                at=at,
                kind="FEE_ADJUSTMENT",
                currency=currency,
                cash_delta=-difference,
                ref=f"fee-final:{execution_id}",
                fee=difference,
                realized_net=-difference,
            )
        self.bump_version(conn, account_id)
        return difference


def _position_view(row: object) -> PositionView:
    r = row._mapping  # type: ignore[attr-defined]
    return PositionView(
        account_id=r["account_id"],
        instrument_id=r["instrument_id"],
        owner_track=r["owner_track"],
        currency=r["currency"],
        quantity=Decimal(r["quantity"]),
        cost_basis=Decimal(r["cost_basis"]),
        open_entry_fees=Decimal(r["open_entry_fees"]),
        realized_gross=Decimal(r["realized_gross"]),
        realized_net=Decimal(r["realized_net"]),
        opened_at=r["opened_at"],
    )
