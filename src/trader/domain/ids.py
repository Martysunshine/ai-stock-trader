"""Deterministic identifiers.

Replay must reproduce the same decisions and ledger on a second run (instructions.md §7.7), so
identifiers are derived with UUIDv5 from their logical inputs instead of random UUIDs. The
client order ID is persisted *before* submission and is the key for timeout reconciliation;
it is never regenerated for the same intent (architecture.md §7).
"""

from __future__ import annotations

import uuid

_NAMESPACE = uuid.UUID("6f1c2a0e-7d3b-5c9e-9a41-2b8f3e6d5c10")

#: Alpaca accepts up to 128 characters; we stay far below common broker limits.
MAX_CLIENT_ORDER_ID_LENGTH = 40


def deterministic_id(prefix: str, *parts: object) -> str:
    if not prefix.isidentifier():
        raise ValueError("prefix must be a short identifier")
    key = "|".join(str(p) for p in parts)
    return f"{prefix}-{uuid.uuid5(_NAMESPACE, key).hex[:24]}"


def client_order_id(run_or_account: str, intent_key: str) -> str:
    cid = f"co-{uuid.uuid5(_NAMESPACE, run_or_account + '|' + intent_key).hex[:32]}"
    assert len(cid) <= MAX_CLIENT_ORDER_ID_LENGTH
    return cid


def advisory_lock_key(name: str) -> int:
    """Stable signed 63-bit key for PostgreSQL advisory locks."""
    value = uuid.uuid5(_NAMESPACE, "lock|" + name).int >> 65
    return int(value)
