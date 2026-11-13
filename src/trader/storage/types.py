"""Column types that behave identically on PostgreSQL and SQLite.

* ``ExactDecimal`` – NUMERIC(38,18) on PostgreSQL, canonical TEXT on SQLite (SQLite has no
  decimal type; SQLAlchemy's ``Numeric`` would round-trip through binary float). Values are
  quantised to 18 decimal places on bind on *both* backends so a replay produces the same
  ledger hash regardless of storage engine.
* ``UtcDateTime`` – timezone-aware UTC in, timezone-aware UTC out. Naive datetimes are rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Any

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

from trader.domain.types import canon_decimal

SCALE = Decimal("1E-18")
_CTX = Context(prec=60, rounding=ROUND_HALF_EVEN)
_LIMIT = Decimal("1E20")


def quantize_storage(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("non-finite decimal cannot be stored")
    q = value.quantize(SCALE, context=_CTX)
    if abs(q) >= _LIMIT:
        raise ValueError(f"decimal {value} exceeds NUMERIC(38,18)")
    return q


class ExactDecimal(TypeDecorator[Decimal]):
    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(38, 18, asdecimal=True))
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("binary floats cannot be stored in an ExactDecimal column")
        d = quantize_storage(Decimal(value))
        if dialect.name == "postgresql":
            return d
        return canon_decimal(d)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("database returned a float for an ExactDecimal column")
        return Decimal(value)


class UtcDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("UtcDateTime expects a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime rejected by UtcDateTime column")
        value = value.astimezone(UTC)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("database returned a non-datetime value")
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
