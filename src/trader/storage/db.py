"""Database access: engines, transactions and portfolio-level serialisation.

PostgreSQL is the runtime store (instructions.md §3). SQLite is accepted for isolated replay runs
only (ADR-0001); every SQLite transaction begins with ``BEGIN IMMEDIATE`` so writers serialise.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import NullPool

from trader.domain.ids import advisory_lock_key
from trader.storage.schema import counters, metadata

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Database:
    def __init__(self, url: str, *, schema: str | None = None, echo: bool = False) -> None:
        self.url = url
        self.schema = schema
        if url.startswith("sqlite"):
            self.dialect = "sqlite"
            self.engine: Engine = create_engine(
                url, echo=echo, pool_size=1, max_overflow=4, connect_args={"timeout": 30}
            )
            _configure_sqlite(self.engine)
        elif url.startswith("postgresql"):
            self.dialect = "postgresql"
            connect_args: dict[str, Any] = {}
            if schema is not None:
                if not _SCHEMA_RE.match(schema):
                    raise ValueError(f"invalid schema name {schema!r}")
                connect_args["options"] = f"-c search_path={schema}"
            self.engine = create_engine(
                url, echo=echo, pool_pre_ping=True, connect_args=connect_args
            )
        else:
            raise ValueError("only sqlite and postgresql URLs are supported")

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgresql"

    # ------------------------------------------------------------------ schema
    def create_namespace(self) -> None:
        """Create the isolated PostgreSQL schema only (tables come from Alembic)."""
        if not (self.is_postgres and self.schema):
            raise RuntimeError("only an isolated PostgreSQL schema can be created")
        admin = create_engine(self.url, poolclass=NullPool)
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
        admin.dispose()

    def create_schema(self) -> None:
        if self.is_postgres and self.schema is not None:
            with self.engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
        metadata.create_all(self.engine)

    def drop_postgres_schema(self) -> None:
        if not (self.is_postgres and self.schema):
            raise RuntimeError("only an isolated PostgreSQL schema can be dropped")
        self.engine.dispose()
        admin = create_engine(self.url, poolclass=NullPool)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
        admin.dispose()

    def dispose(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------------------------ transactions
    @contextmanager
    def tx(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    def lock_portfolio(self, conn: Connection, portfolio_id: str) -> None:
        """Serialise all allocation decisions for a portfolio (transaction scoped).

        PostgreSQL: ``pg_advisory_xact_lock``. SQLite: the transaction already holds the
        database write lock (``BEGIN IMMEDIATE``).
        """
        if self.is_postgres:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": advisory_lock_key(f"portfolio:{portfolio_id}")},
            )

    def next_counter(self, conn: Connection, name: str) -> int:
        """Gap-free per-database sequence used for deterministic ordering in hashes."""
        ins = pg_insert(counters) if self.is_postgres else sqlite_insert(counters)
        conn.execute(ins.values(name=name, value=0).on_conflict_do_nothing(index_elements=["name"]))
        conn.execute(
            update(counters).where(counters.c.name == name).values(value=counters.c.value + 1)
        )
        value = conn.execute(select(counters.c.value).where(counters.c.name == name)).scalar_one()
        return int(value)


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        # Replay databases are disposable per run: WAL + NORMAL keeps commits durable against
        # process crashes while avoiding an fsync per transaction.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
