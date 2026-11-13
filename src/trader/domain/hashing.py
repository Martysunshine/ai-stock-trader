"""Canonical serialisation and hashing.

Every hash in the system (policy, intents, approved plans, decisions, ledger) goes through
:func:`canonical_json` so that the same logical content always yields the same digest,
independent of database representation (e.g. PostgreSQL NUMERIC padding) or dict ordering.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from trader.domain.types import canon_decimal, iso_utc


def _normalise(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalise(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return _normalise(value.value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return canon_decimal(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("floats are not allowed in hashed content")
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, Sequence | set | frozenset):
        items = [_normalise(v) for v in value]
        if isinstance(value, set | frozenset):
            items.sort(key=lambda x: json.dumps(x, sort_keys=True))
        return items
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))
