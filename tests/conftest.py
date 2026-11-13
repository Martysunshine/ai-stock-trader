"""Shared fixtures.

PostgreSQL tests (marker ``pg``) use an isolated schema in the ``trader_test`` database from
``docker compose``. If the database is unreachable they FAIL loudly instead of skipping, so a
green run always means the PostgreSQL invariants were exercised. Deselect them explicitly with
``-m "not pg"`` when working without Docker.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis.configuration import set_hypothesis_home_dir

from trader.config.policy import LoadedPolicy, load_policy
from trader.storage.db import Database

REPO_ROOT = Path(__file__).resolve().parents[1]

# All tool caches live under one hidden folder; pytest, mypy and ruff are set in pyproject.toml.
set_hypothesis_home_dir(REPO_ROOT / ".cache" / "hypothesis")


def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def policy() -> LoadedPolicy:
    return load_policy(REPO_ROOT / "configs" / "policy" / "base.yaml")


@pytest.fixture
def sqlite_db(tmp_path: Path) -> Iterator[Database]:
    db = Database(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    db.create_schema()
    yield db
    db.dispose()


def pg_url() -> str:
    url = os.environ.get("TRADER_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "TRADER_TEST_DATABASE_URL is not set: start `docker compose up -d` (see .env.example)"
        )
    return url


@pytest.fixture
def pg_db() -> Iterator[Database]:
    url = pg_url()
    schema = f"t_{uuid.uuid5(uuid.NAMESPACE_OID, str(os.getpid()) + str(id(object()))).hex[:16]}"
    db = Database(url, schema=schema)
    try:
        db.create_schema()
    except Exception as exc:  # pragma: no cover - infrastructure failure must be loud
        pytest.fail(f"PostgreSQL test database unreachable: {exc}")
    yield db
    db.drop_postgres_schema()


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.pg)])
def any_db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Database]:
    if request.param == "sqlite":
        db = Database(f"sqlite:///{(tmp_path / 'any.db').as_posix()}")
        db.create_schema()
        yield db
        db.dispose()
    else:
        url = pg_url()
        schema = f"t_{uuid.uuid5(uuid.NAMESPACE_OID, str(tmp_path)).hex[:16]}"
        db = Database(url, schema=schema)
        db.create_schema()
        yield db
        db.drop_postgres_schema()
