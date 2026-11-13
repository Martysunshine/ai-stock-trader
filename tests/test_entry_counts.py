"""The per-session entry count counts strategy entries only (ADR-0004, ADR-0043).

On 2026-11-09 two ``trader paper certify`` buys used up the policy's two entries per session,
and every practice candidate afterwards was rejected ENTRY_LIMIT_REACHED. Certification
intents carry no candidate; they are bounded by their own grant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import insert

from trader.domain.orders import OrderPurpose, OrderState
from trader.risk.loss_limits import window_key_and_start
from trader.risk.portfolio import entries_since
from trader.storage.db import Database
from trader.storage.schema import accounts, order_intents, portfolios

NOW = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)  # 12:00 New York, regular session
ACCOUNT, OTHER = "acct-a", "acct-b"


def _seed(db: Database) -> None:
    with db.tx() as conn:
        conn.execute(
            insert(portfolios).values(
                portfolio_id="pf",
                base_currency="USD",
                initial_capital=Decimal(1000),
                created_at=NOW,
            )
        )
        for acct in (ACCOUNT, OTHER):
            conn.execute(
                insert(accounts).values(
                    account_id=acct,
                    portfolio_id="pf",
                    alias=acct,
                    track="STOCKS",
                    broker="alpaca_paper",
                    mode="paper",
                    currency="USD",
                    rule_profile="UNVERIFIED",
                    legal_type="MARGIN",
                    allocated_capital=Decimal(500),
                    created_at=NOW,
                )
            )


def _intent(
    db: Database,
    n: int,
    *,
    account: str = ACCOUNT,
    purpose: OrderPurpose = OrderPurpose.ENTRY,
    state: OrderState = OrderState.FILLED,
    candidate: str | None = "cand",
    at: datetime = NOW,
) -> None:
    with db.tx() as conn:
        conn.execute(
            insert(order_intents).values(
                intent_id=f"i{n}",
                seq=n,
                portfolio_id="pf",
                account_id=account,
                client_order_id=f"c{n}",
                instrument_id="XNAS:NVDA",
                track="STOCKS",
                side="BUY" if purpose is OrderPurpose.ENTRY else "SELL",
                purpose=purpose.value,
                order_type="LIMIT",
                quantity=Decimal("0.01"),
                limit_price=Decimal(100),
                time_in_force="DAY",
                state=state.value,
                candidate_id=None if candidate is None else f"{candidate}-{n}",
                intent_hash="h",
                plan_json="{}",
                plan_digest="d",
                valid_until=at + timedelta(minutes=5),
                cum_filled_qty=Decimal(0),
                created_at=at,
                updated_at=at,
            )
        )


def test_only_strategy_entries_count_toward_the_session_limit(sqlite_db: Database) -> None:
    _seed(sqlite_db)
    _intent(sqlite_db, 1, candidate=None)  # certification buy (no candidate)
    _intent(sqlite_db, 2, candidate=None, state=OrderState.CANCELED)  # certification limit buy
    _intent(sqlite_db, 3)  # strategy entry, filled: counts
    _intent(sqlite_db, 4, state=OrderState.CANCELED)  # sent then cancelled: still counts
    _intent(sqlite_db, 5, state=OrderState.NOT_SENT)  # never sent: no
    _intent(sqlite_db, 6, purpose=OrderPurpose.EXIT_TARGET)  # exit: no
    _intent(sqlite_db, 7, account=OTHER)  # another account's strategy entry
    _intent(sqlite_db, 8, at=NOW - timedelta(days=1))  # yesterday: outside the window
    _, ny_start = window_key_and_start("NY_SESSION", NOW)
    _, utc_start = window_key_and_start("UTC_DAY", NOW)
    with sqlite_db.tx() as conn:
        assert entries_since(conn, [ACCOUNT], ny_start) == 2
        assert entries_since(conn, [ACCOUNT, OTHER], utc_start) == 3


def test_certification_entries_alone_leave_the_full_allowance(sqlite_db: Database) -> None:
    _seed(sqlite_db)
    for n in range(1, 5):  # the four orders of a certification run, none from a candidate
        _intent(sqlite_db, n, candidate=None)
    _, start = window_key_and_start("NY_SESSION", NOW)
    with sqlite_db.tx() as conn:
        assert entries_since(conn, [ACCOUNT], start) == 0
