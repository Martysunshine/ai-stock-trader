"""Alembic environment: migrates the runtime schema defined in trader.storage.schema."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from trader.storage.schema import metadata

config = context.config
target_metadata = metadata


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or os.environ.get(
        "TRADER_DATABASE_URL"
    )
    if not url:
        raise RuntimeError("pass -x url=... or set TRADER_DATABASE_URL")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    schema = context.get_x_argument(as_dictionary=True).get("schema")
    connect_args = {"options": f"-c search_path={schema}"} if schema else {}
    engine = create_engine(_url(), poolclass=pool.NullPool, connect_args=connect_args)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
