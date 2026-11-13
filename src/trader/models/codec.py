"""Exact decimal-string encoding of model numbers (architecture.md §3 ``ModelBundle``).

A model bundle is hashed through :func:`trader.domain.hashing.canonical_json`, which refuses
binary floats (instructions.md §3). Every coefficient, threshold and leaf value is therefore
stored as a plain decimal string that is the *exact* value of the float's shortest round-trip
representation, so ``float(encode_float(x)) == x`` for every finite ``x``: inference from the
stored bundle reproduces the trained model bit for bit.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from trader.domain.hashing import canonical_json
from trader.domain.types import canon_decimal


class EncodingError(ValueError):
    """A stored model number is missing, malformed, a binary float or not finite."""


def encode_float(value: float) -> str:
    """Exact, exponent-free decimal string for a finite float (round-trips through float())."""
    x = float(value)
    if not math.isfinite(x):
        raise EncodingError(f"model numbers must be finite, got {x!r}")
    return canon_decimal(Decimal(repr(x)))


def decode_number(value: object, *, where: str) -> float:
    """Read a stored number: a decimal string or an int, never a binary float or a bool."""
    if isinstance(value, bool | float):
        raise EncodingError(f"{where}: numbers are stored as decimal strings, got {value!r}")
    if isinstance(value, int):
        return float(value)
    if not isinstance(value, str):
        raise EncodingError(f"{where}: expected a decimal string, got {type(value).__name__}")
    try:
        number = Decimal(value.strip())
    except ArithmeticError as exc:
        raise EncodingError(f"{where}: not a decimal number: {value!r}") from exc
    if not number.is_finite():
        raise EncodingError(f"{where}: NaN and infinity are not valid model numbers")
    return float(number)


def decode_vector(value: object, *, where: str, length: int | None = None) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise EncodingError(f"{where}: expected a list of decimal strings")
    if length is not None and len(value) != length:
        raise EncodingError(f"{where}: expected {length} values, got {len(value)}")
    return [decode_number(v, where=f"{where}[{i}]") for i, v in enumerate(value)]


def encode_floats(value: Any) -> Any:
    """Recursively replace every float in a JSON-like structure by its exact decimal string.

    Used for LightGBM's ``booster.dump_model()`` output; ints, bools, strings and None are kept.
    """
    if isinstance(value, bool) or value is None or isinstance(value, int | str):
        return value
    if isinstance(value, float):
        return encode_float(value)
    if isinstance(value, Mapping):
        return {str(k): encode_floats(v) for k, v in value.items()}
    if isinstance(value, Sequence):
        return [encode_floats(v) for v in value]
    raise EncodingError(f"cannot encode {type(value).__name__} in a model bundle")


def frozen_json(value: Any, *, where: str) -> Any:
    """Normalise a float-free JSON-like structure to plain dicts/lists (canonical form).

    Raises :class:`EncodingError` for any binary float, so a bundle can always be hashed.
    """
    try:
        return json.loads(canonical_json(value))
    except TypeError as exc:
        raise EncodingError(
            f"{where}: {exc}; store model numbers as decimal strings (encode_float)"
        ) from exc
