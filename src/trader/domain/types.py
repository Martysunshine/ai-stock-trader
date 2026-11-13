"""Primitive value types shared by every layer.

Money, prices and quantities are :class:`decimal.Decimal`. Binary floats are rejected at every
boundary (instructions.md §3) and NaN/Infinity are never valid values (architecture.md §3).
Timestamps must be timezone-aware and are normalised to UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, PlainSerializer

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal("0.0001")
CENT = Decimal("0.01")


class DomainError(ValueError):
    """Raised when a domain invariant is violated."""


# --------------------------------------------------------------------------- decimals


def to_decimal(value: object) -> Decimal:
    """Convert an external value to a finite Decimal, refusing binary floats."""
    if isinstance(value, bool):
        raise DomainError("booleans are not numeric values")
    if isinstance(value, float):
        raise DomainError(
            "binary floating point is not accepted for money, price or quantity; "
            "pass a string or Decimal"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise DomainError(f"not a decimal number: {value!r}") from exc
    else:
        raise DomainError(f"unsupported numeric type {type(value).__name__}")
    if not result.is_finite():
        raise DomainError("NaN and infinite values are rejected")
    return result


def canon_decimal(value: Decimal) -> str:
    """Canonical, exponent-free string form used for hashing and serialisation."""
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round a non-negative quantity down to a multiple of ``step``."""
    if step <= 0:
        raise DomainError("step must be positive")
    if value < 0:
        raise DomainError("cannot round a negative quantity")
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round a non-negative amount up to a multiple of ``step`` (fees, costs)."""
    if step <= 0:
        raise DomainError("step must be positive")
    if value < 0:
        raise DomainError("cannot round a negative amount up")
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return units * step


def _non_negative(value: Decimal) -> Decimal:
    if value < 0:
        raise DomainError("value must not be negative")
    return value


def _positive(value: Decimal) -> Decimal:
    if value <= 0:
        raise DomainError("value must be positive")
    return value


def _fraction(value: Decimal) -> Decimal:
    if value < 0 or value > 1:
        raise DomainError("fraction must be within [0, 1]")
    return value


_DecimalSerializer = PlainSerializer(canon_decimal, return_type=str, when_used="json")

Dec = Annotated[Decimal, BeforeValidator(to_decimal), _DecimalSerializer]
NonNegDec = Annotated[
    Decimal, BeforeValidator(to_decimal), AfterValidator(_non_negative), _DecimalSerializer
]
PosDec = Annotated[
    Decimal, BeforeValidator(to_decimal), AfterValidator(_positive), _DecimalSerializer
]
Fraction = Annotated[
    Decimal, BeforeValidator(to_decimal), AfterValidator(_fraction), _DecimalSerializer
]


# --------------------------------------------------------------------------- time


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainError("naive datetimes are rejected; supply a timezone-aware UTC time")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(require_utc)]


def iso_utc(value: datetime) -> str:
    return require_utc(value).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- enums


class Track(StrEnum):
    """Economic research tracks. CFD is a product type, never a fourth capital pool."""

    STOCKS = "STOCKS"
    CRYPTO = "CRYPTO"
    COMMODITIES = "COMMODITIES"


class Mode(StrEnum):
    REPLAY = "replay"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


class ProductType(StrEnum):
    """What the instrument legally is; a track profile names the products it may trade.

    ``ETF`` is a US-listed exchange-traded fund or trust bought fully paid on the stock route.
    It is deliberately not ``ORDINARY_EQUITY``: markets_and_brokers.md §7 separates US ETFs
    (product-document restrictions for European retail investors) from the ordinary shares v1
    trades, so the STOCKS profile never admits one by accident.
    """

    ORDINARY_EQUITY = "ORDINARY_EQUITY"
    CRYPTO_SPOT = "CRYPTO_SPOT"
    ETC = "ETC"
    CFD = "CFD"
    ETF = "ETF"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class EvidenceLevel(StrEnum):
    """architecture.md §4: documented vs demonstrated in paper vs confirmed live."""

    UNKNOWN = "UNKNOWN"
    REPLAY_SIMULATED = "REPLAY_SIMULATED"
    DOCUMENTED = "DOCUMENTED"
    PAPER_DEMONSTRATED = "PAPER_DEMONSTRATED"
    LIVE_CONFIRMED = "LIVE_CONFIRMED"


class Liquidity(StrEnum):
    TAKER = "TAKER"
    MAKER = "MAKER"


def _currency(value: str) -> str:
    if len(value) != 3 or not value.isalpha() or not value.isupper():
        raise DomainError(f"invalid ISO currency code {value!r}")
    return value


CurrencyCode = Annotated[str, AfterValidator(_currency)]
