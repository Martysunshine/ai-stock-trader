"""Portfolio-wide state read inside the allocation transaction (CRYPTO §3, COMMODITIES §4).

The exposure snapshot is rebuilt from the database inside the same locked transaction that
reserves capital: positions plus every active reservation across all tracks and accounts.
Cached broker polls are never used to allocate (polling another bot cannot prevent it from
spending the same risk budget).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Connection, func, select

from trader.accounting.equity import (
    AccountEquity,
    PortfolioEquity,
    account_equity,
    portfolio_equity,
)
from trader.accounting.fees import FeeSchedule
from trader.accounting.fx import FxBook
from trader.accounting.ledger import Ledger
from trader.config.track_profiles import TrackProfile
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.domain.account import RuleProfileKind
from trader.domain.capabilities import BrokerCapabilities
from trader.domain.instruments import AnySpec
from trader.domain.orders import LIVE_STATES, OrderPurpose, OrderState
from trader.domain.types import ZERO, Side, Track
from trader.risk.account_rules import FundsInputs, spendable_cash
from trader.risk.loss_limits import window_key_and_start
from trader.risk.sizing import ExposureState
from trader.storage.schema import exit_plans, order_intents, risk_reservations


@dataclass(frozen=True)
class AccountRuntime:
    account_id: str
    alias: str
    portfolio_id: str
    track: Track
    broker: str
    currency: str
    rule_profile: RuleProfileKind
    capabilities: BrokerCapabilities
    fees: FeeSchedule
    specs: Mapping[str, AnySpec]
    profile: TrackProfile
    calendar: ExchangeCalendar | Utc247Calendar


@dataclass(frozen=True)
class PortfolioSnapshot:
    exposure: ExposureState
    equity: PortfolioEquity
    account_spendable_usd: Mapping[str, Decimal]
    portfolio_spendable_usd: Decimal
    busy_instruments: frozenset[str]
    unknown_orders: int


@dataclass(frozen=True)
class ReservationView:
    reservation_id: str
    account_id: str
    instrument_id: str
    remaining_fraction: Decimal
    notional_usd: Decimal
    fees_usd: Decimal
    planned_loss_usd: Decimal
    tags: tuple[str, ...]


def active_entry_reservations(conn: Connection, portfolio_id: str) -> list[ReservationView]:
    rows = conn.execute(
        select(risk_reservations).where(
            risk_reservations.c.portfolio_id == portfolio_id,
            risk_reservations.c.kind == "ENTRY",
            risk_reservations.c.state == "ACTIVE",
        )
    ).all()
    out = []
    for r in rows:
        reserved = Decimal(r.reserved_qty)
        remaining = reserved - Decimal(r.consumed_qty) - Decimal(r.released_qty)
        fraction = remaining / reserved if reserved > 0 else ZERO
        tags = tuple(t for t in str(r.exposure_tags).split(",") if t)
        out.append(
            ReservationView(
                reservation_id=r.reservation_id,
                account_id=r.account_id,
                instrument_id=r.instrument_id,
                remaining_fraction=max(fraction, ZERO),
                notional_usd=Decimal(r.notional_usd),
                fees_usd=Decimal(r.fees_usd),
                planned_loss_usd=Decimal(r.planned_loss_usd),
                tags=tags,
            )
        )
    return out


def count_unknown_orders(conn: Connection, account_ids: list[str]) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(order_intents)
            .where(
                order_intents.c.account_id.in_(account_ids),
                order_intents.c.state.in_(
                    (
                        OrderState.SUBMITTING.value,
                        OrderState.UNKNOWN_OUTCOME.value,
                        OrderState.UNRESOLVED.value,
                    )
                ),
            )
        ).scalar_one()
    )


def committed_sell_quantity(conn: Connection, account_id: str, instrument_id: str) -> Decimal:
    rows = conn.execute(
        select(order_intents.c.quantity, order_intents.c.cum_filled_qty).where(
            order_intents.c.account_id == account_id,
            order_intents.c.instrument_id == instrument_id,
            order_intents.c.side == Side.SELL.value,
            order_intents.c.state.in_(tuple(s.value for s in LIVE_STATES)),
        )
    ).all()
    return sum((Decimal(q) - Decimal(c) for q, c in rows), start=ZERO)


def entries_since(conn: Connection, account_ids: list[str], since: datetime) -> int:
    """Strategy entries sent since ``since`` (the per-session entry limit, ADR-0004).

    Only intents that come from a strategy candidate count. ``trader paper certify`` orders
    carry no candidate (``allocator.reserve_certification_order``); they are bounded by their
    own grant and must not use up the strategy's session allowance (ADR-0043). They still
    count wherever exposure matters: pending-entry and open-position checks read the portfolio
    snapshot, not this count.
    """
    return int(
        conn.execute(
            select(func.count())
            .select_from(order_intents)
            .where(
                order_intents.c.account_id.in_(account_ids),
                order_intents.c.purpose == OrderPurpose.ENTRY.value,
                order_intents.c.state != OrderState.NOT_SENT.value,
                order_intents.c.candidate_id.is_not(None),
                order_intents.c.created_at >= since,
            )
        ).scalar_one()
    )


def entries_in_session(
    conn: Connection, *, account: AccountRuntime, all_account_ids: list[str], now: datetime
) -> int:
    """Max of the track's own session count and the portfolio's UTC-day count (ADR-0004)."""
    counts = []
    for kind in account.profile.loss_windows:
        if kind in ("NY_SESSION", "UTC_DAY"):
            _key, start = window_key_and_start(kind, now)
            ids = [account.account_id] if kind == "NY_SESSION" else all_account_ids
            counts.append(entries_since(conn, ids, start))
    return max(counts, default=0)


class PortfolioReader:
    def __init__(
        self, ledger: Ledger, initial_capital_usd: Decimal, gap_floor_fraction: Decimal
    ) -> None:
        self.ledger = ledger
        self.initial_capital_usd = initial_capital_usd
        self.gap_floor_fraction = gap_floor_fraction

    def snapshot(
        self,
        conn: Connection,
        *,
        accounts: Mapping[str, AccountRuntime],
        marks: Mapping[str, Decimal],
        fx: FxBook,
        now: datetime,
        settlement_day: date,
    ) -> PortfolioSnapshot:
        account_ids = sorted(accounts)
        portfolio_ids = {a.portfolio_id for a in accounts.values()}
        if len(portfolio_ids) != 1:
            raise ValueError("all accounts must belong to one portfolio")
        portfolio_id = portfolio_ids.pop()

        equities: list[AccountEquity] = []
        by_instrument: dict[str, Decimal] = {}
        by_tag: dict[str, Decimal] = {}
        gross = ZERO
        planned = ZERO
        busy: set[str] = set()
        open_positions = 0
        for acc_id in account_ids:
            acc = accounts[acc_id]
            equities.append(
                account_equity(
                    conn,
                    self.ledger,
                    account_id=acc_id,
                    currency=acc.currency,
                    marks=marks,
                    fees=acc.fees,
                )
            )
        positions = self.ledger.open_positions(conn, account_ids)
        stops = {
            (r.account_id, r.instrument_id): Decimal(r.stop_price)
            for r in conn.execute(select(exit_plans).where(exit_plans.c.active.is_(True))).all()
        }
        gap_floor = self.gap_floor_fraction
        for pos in positions:
            acc = accounts[pos.account_id]
            spec = acc.specs.get(pos.instrument_id)
            rate = fx.cost_rate(pos.currency, now)
            mark = marks.get(pos.instrument_id, pos.avg_price)
            unit = spec.units_per_quantity() if spec else Decimal(1)
            value = abs(pos.quantity) * unit * max(mark, pos.avg_price) * rate
            gross += value
            by_instrument[pos.instrument_id] = by_instrument.get(pos.instrument_id, ZERO) + value
            for tag in spec.exposure_tags if spec else ():
                by_tag[tag] = by_tag.get(tag, ZERO) + value
            stop = stops.get((pos.account_id, pos.instrument_id))
            if stop is None:
                planned += value  # unplanned position: the whole value is at risk
            else:
                planned += (
                    abs(pos.quantity) * unit * max(pos.avg_price - stop, ZERO) * rate
                    + value * gap_floor
                )
            busy.add(pos.instrument_id)
            open_positions += 1

        reservations = active_entry_reservations(conn, portfolio_id)
        reserved_cash: dict[str, Decimal] = {}
        for res in reservations:
            notional = res.notional_usd * res.remaining_fraction
            gross += notional
            by_instrument[res.instrument_id] = by_instrument.get(res.instrument_id, ZERO) + notional
            for tag in res.tags:
                by_tag[tag] = by_tag.get(tag, ZERO) + notional
            planned += res.planned_loss_usd * res.remaining_fraction
            reserved_cash[res.account_id] = reserved_cash.get(res.account_id, ZERO) + (
                notional + res.fees_usd * res.remaining_fraction
            )
            busy.add(res.instrument_id)

        spendable: dict[str, Decimal] = {}
        for acc_id in account_ids:
            acc = accounts[acc_id]
            cash = self.ledger.cash(conn, acc_id, acc.currency)
            settled = self.ledger.settled_cash(conn, acc_id, acc.currency, settlement_day)
            rate = fx.asset_rate(acc.currency, now)
            spend_quote = spendable_cash(
                acc.rule_profile,
                FundsInputs(cash=cash, settled_cash=settled, active_entry_reservations=ZERO),
            )
            spendable[acc_id] = max(spend_quote * rate - reserved_cash.get(acc_id, ZERO), ZERO)

        equity = portfolio_equity(
            equities, fx=fx, now=now, initial_capital_usd=self.initial_capital_usd
        )
        return PortfolioSnapshot(
            exposure=ExposureState(
                gross_usd=gross,
                by_instrument_usd=by_instrument,
                by_tag_usd=by_tag,
                planned_risk_usd=planned,
                open_positions=open_positions,
                pending_entries=len(reservations),
            ),
            equity=equity,
            account_spendable_usd=spendable,
            portfolio_spendable_usd=sum(spendable.values(), start=ZERO),
            busy_instruments=frozenset(busy),
            unknown_orders=count_unknown_orders(conn, account_ids),
        )
