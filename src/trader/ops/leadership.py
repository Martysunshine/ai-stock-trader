"""Account leadership: advisory lock plus fencing token (architecture.md §5).

Only the current leader may reserve or submit. Every reservation and submission transaction calls
:meth:`LeadershipManager.assert_current`, which checks (a) the PostgreSQL session advisory lock
connection is still alive and (b) the persisted fencing token still equals ours. If leadership is
uncertain we stop submitting; an expired lease alone is never trusted.

SQLite (replay only) has no advisory locks; the fencing token alone fences stale workers inside a
single replay process.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Connection, create_engine, insert, select, text, update
from sqlalchemy.pool import NullPool

from trader.domain.ids import advisory_lock_key
from trader.ops.clock import Clock
from trader.storage.db import Database
from trader.storage.schema import account_leases


class LeadershipLost(RuntimeError):
    pass


class LeadershipUnavailable(RuntimeError):
    pass


class LeadershipManager:
    def __init__(self, db: Database, *, account_id: str, holder: str, clock: Clock) -> None:
        self.db = db
        self.account_id = account_id
        self.holder = holder
        self.clock = clock
        self.token: int | None = None
        self._lock_conn: Connection | None = None

    def acquire(self) -> int:
        if self.db.is_postgres:
            connect_args = {}
            if self.db.schema:
                connect_args["options"] = f"-c search_path={self.db.schema}"
            engine = create_engine(self.db.url, poolclass=NullPool, connect_args=connect_args)
            conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": advisory_lock_key(f"account:{self.account_id}")},
            ).scalar_one()
            if not got:
                conn.close()
                engine.dispose()
                raise LeadershipUnavailable(f"another worker leads account {self.account_id}")
            self._lock_conn = conn
        with self.db.tx() as tx:
            self.token = self._bump(tx, self.clock.now())
        return self.token

    def _bump(self, conn: Connection, now: datetime) -> int:
        row = conn.execute(
            select(account_leases.c.fencing_token).where(
                account_leases.c.account_id == self.account_id
            )
        ).first()
        if row is None:
            token = 1
            conn.execute(
                insert(account_leases).values(
                    account_id=self.account_id,
                    holder=self.holder,
                    fencing_token=token,
                    acquired_at=now,
                )
            )
        else:
            token = int(row.fencing_token) + 1
            conn.execute(
                update(account_leases)
                .where(account_leases.c.account_id == self.account_id)
                .values(holder=self.holder, fencing_token=token, acquired_at=now)
            )
        return token

    def assert_current(self, conn: Connection) -> int:
        if self.token is None:
            raise LeadershipLost("not the account leader")
        if self.db.is_postgres:
            if self._lock_conn is None or self._lock_conn.closed:
                raise LeadershipLost("advisory lock connection is gone")
            try:
                self._lock_conn.execute(text("SELECT 1"))
            except Exception as exc:  # any driver error means leadership is uncertain
                raise LeadershipLost(f"advisory lock connection failed: {exc}") from exc
        row = conn.execute(
            select(account_leases.c.fencing_token, account_leases.c.holder).where(
                account_leases.c.account_id == self.account_id
            )
        ).first()
        if row is None or int(row.fencing_token) != self.token or row.holder != self.holder:
            raise LeadershipLost(
                f"fencing token mismatch for {self.account_id}: ours={self.token}, "
                f"current={None if row is None else row.fencing_token}"
            )
        return self.token

    def release(self) -> None:
        if self._lock_conn is not None:
            try:
                self._lock_conn.close()
            finally:
                self._lock_conn = None
        self.token = None

    def simulate_lock_loss(self) -> None:
        """Test hook: drop the lock connection without clearing the local token."""
        if self._lock_conn is not None:
            self._lock_conn.close()
