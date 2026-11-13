"""Small dialect-neutral helpers."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from trader.domain.hashing import canonical_json


def insert_ignore(
    conn: Connection, table: Table, values: dict[str, Any], conflict_columns: list[str]
) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING RETURNING. True when a row was inserted.

    ``rowcount`` is unreliable for this statement on PostgreSQL/psycopg (it reports -1), so the
    insert is detected through ``RETURNING`` on both dialects. Used for idempotent posting
    (fills, events) without aborting the enclosing PostgreSQL transaction on a duplicate.
    """
    key = table.c[conflict_columns[0]]
    if conn.dialect.name == "postgresql":
        pg_stmt = (
            pg_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=conflict_columns)
            .returning(key)
        )
        return conn.execute(pg_stmt).first() is not None
    lite_stmt = (
        sqlite_insert(table)
        .values(**values)
        .on_conflict_do_nothing(index_elements=conflict_columns)
        .returning(key)
    )
    return conn.execute(lite_stmt).first() is not None


def to_json(value: Any) -> str:
    return canonical_json(value)


def from_json(value: str) -> Any:
    return json.loads(value)
