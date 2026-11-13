"""Reproducibility hashes (instructions.md §7.7, roadmap §4.5).

Hashes are computed from persisted rows through canonical serialisation, so they are identical
across SQLite and PostgreSQL and exclude storage artefacts (autoincrement IDs, wall-clock times).
"""

from __future__ import annotations

from sqlalchemy import Connection, select

from trader.domain.hashing import canonical_json, sha256_hex
from trader.storage.schema import fills, ledger_entries, order_intents, positions, risk_decisions


def decision_hash(conn: Connection) -> str:
    rows = conn.execute(
        select(risk_decisions.c.seq, risk_decisions.c.payload_json).order_by(risk_decisions.c.seq)
    ).all()
    return sha256_hex(canonical_json([[int(seq), payload] for seq, payload in rows]))


def ledger_hash(conn: Connection) -> str:
    entries = conn.execute(
        select(
            ledger_entries.c.account_id,
            ledger_entries.c.seq,
            ledger_entries.c.entry_time,
            ledger_entries.c.kind,
            ledger_entries.c.currency,
            ledger_entries.c.cash_delta,
            ledger_entries.c.instrument_id,
            ledger_entries.c.quantity_delta,
            ledger_entries.c.price,
            ledger_entries.c.fee,
            ledger_entries.c.realized_net,
            ledger_entries.c.ref,
        ).order_by(ledger_entries.c.account_id, ledger_entries.c.seq)
    ).all()
    fill_rows = conn.execute(
        select(
            fills.c.account_id,
            fills.c.execution_id,
            fills.c.client_order_id,
            fills.c.side,
            fills.c.quantity,
            fills.c.price,
            fills.c.fee,
            fills.c.realized_net,
        ).order_by(fills.c.account_id, fills.c.execution_id)
    ).all()
    position_rows = conn.execute(
        select(
            positions.c.account_id,
            positions.c.instrument_id,
            positions.c.quantity,
            positions.c.cost_basis,
            positions.c.realized_net,
        ).order_by(positions.c.account_id, positions.c.instrument_id)
    ).all()
    intent_rows = conn.execute(
        select(
            order_intents.c.seq,
            order_intents.c.client_order_id,
            order_intents.c.purpose,
            order_intents.c.quantity,
            order_intents.c.state,
            order_intents.c.cum_filled_qty,
        ).order_by(order_intents.c.seq)
    ).all()
    payload = {
        "entries": [list(r) for r in entries],
        "fills": [list(r) for r in fill_rows],
        "positions": [list(r) for r in position_rows],
        "intents": [list(r) for r in intent_rows],
    }
    return sha256_hex(canonical_json(payload))
