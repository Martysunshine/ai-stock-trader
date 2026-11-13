"""64-bit broker order versions

The Alpaca paper adapter reports ``broker_version`` as the order's ``updated_at`` in epoch
microseconds (about 1.8e15), which overflows a 32-bit INTEGER. Widens every broker-version
column to BIGINT. A PostgreSQL int -> bigint change is a lossless cast and keeps the server
default. Downgrading fails if any stored version exceeds the 32-bit range (by design: never
truncate a version).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ('order_intents', 'last_broker_version'),
    ('broker_orders', 'broker_version'),
    ('broker_order_events', 'broker_version'),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
