"""The intel feed's files: append-only records, snapshots and a run log (ADR-0047).

Under ``data/intel/`` (gitignored):

* ``records/<kind>/<YYYY-MM>.jsonl``: one stored record per line, filed by the month it was first
  seen. A record is never rewritten. When a source changes an item, the new content is appended
  as the next revision (``rev``) of the same ``id``; the same content seen again adds nothing.
* ``snapshots/<group>/<SYMBOL>/<YYYY-MM>.jsonl``: what was known about one instrument at a moment
  (``trader.intel.snapshot``), appended when it differs from the previous one.
* ``runs/<YYYY-MM>.jsonl``: one line per refresh, so a gap in the feed is visible afterwards.
* ``index.json``: the current state for the console, replaced atomically (``trader.intel.feed``).
* ``labels/<YYYY-MM>.jsonl``: catalyst labels (``trader.intel.labels``), under their own lock.

One writer at a time: a refresh holds ``.writer.lock``. Appends are one write per batch followed
by a flush to disk; a reader that meets an unfinished last line skips it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trader.domain.types import iso_utc, require_utc
from trader.intel.records import KINDS, IntelRecord, Kind, StoredRecord, headline_key, parse_time

LOCK_NAME = ".writer.lock"
#: A lock older than this belongs to a writer that died; a refresh never takes this long.
LOCK_STALE_AFTER = timedelta(minutes=15)
#: How far back each kind is loaded for revisions and snapshots.
LOAD_WINDOW: dict[Kind, timedelta] = {
    "news": timedelta(days=8),
    "premarket": timedelta(days=8),
    "insider": timedelta(days=400),
}


class StoreError(RuntimeError):
    pass


class StoreBusy(StoreError):
    """Another refresh holds the writer lock."""


def check_root(root: Path) -> Path:
    """The output folder must be an ``intel`` folder (never research data or market files)."""
    if root.name != "intel" or "history" in root.parts:
        raise StoreError(f"refusing to write intel files outside a data/intel folder: {root}")
    return root


def _month(when: datetime) -> str:
    return require_utc(when).strftime("%Y-%m")


def _months(since: datetime, until: datetime) -> list[str]:
    first, last = require_utc(since), require_utc(until)
    year, month = first.year, first.month
    out: list[str] = []
    while (year, month) <= (last.year, last.month):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every complete JSON object line of ``path``; a torn or foreign line is skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_lines(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Append ``rows`` as JSON lines (LF, UTF-8) in one write, then flush to disk."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        for row in rows
    )
    torn = False
    try:
        with path.open("rb") as existing:
            existing.seek(0, os.SEEK_END)
            if existing.tell() > 0:
                existing.seek(-1, os.SEEK_END)
                torn = existing.read(1) != b"\n"
    except OSError:
        torn = False
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(("\n" if torn else "") + text)
        fh.flush()
        os.fsync(fh.fileno())


def latest_by_id(rows: Iterable[StoredRecord]) -> dict[str, StoredRecord]:
    """The highest revision of each record id."""
    latest: dict[str, StoredRecord] = {}
    for row in rows:
        held = latest.get(row.id)
        if held is None or row.rev > held.rev:
            latest[row.id] = row
    return latest


class IntelStore:
    def __init__(self, root: Path) -> None:
        self.root = check_root(root)

    # ------------------------------------------------------------------ records
    def records_file(self, kind: Kind, month: str) -> Path:
        return self.root / "records" / kind / f"{month}.jsonl"

    def load(self, kind: Kind, *, since: datetime, until: datetime) -> list[StoredRecord]:
        """Stored records of ``kind`` first seen from ``since`` on, oldest first, without exact
        duplicates (the same id and content twice, which two writers could once have produced)."""
        out: list[StoredRecord] = []
        seen: set[tuple[str, str]] = set()
        for month in _months(since, until):
            for raw in read_jsonl(self.records_file(kind, month)):
                try:
                    row = StoredRecord.from_json(raw)
                except (KeyError, TypeError, ValueError):
                    continue
                key = (row.id, row.content_hash)
                if row.first_seen_at < since or key in seen:
                    continue
                seen.add(key)
                out.append(row)
        return sorted(out, key=lambda r: (r.first_seen_at, r.id, r.rev))

    def load_all(self, now: datetime) -> list[StoredRecord]:
        """Every kind over its own window, for revisions and snapshots."""
        rows: list[StoredRecord] = []
        for kind in KINDS:
            rows += self.load(kind, since=now - LOAD_WINDOW[kind], until=now)
        return rows

    def append(
        self,
        records: Sequence[IntelRecord],
        *,
        seen_at: datetime,
        latest: dict[str, StoredRecord],
    ) -> list[StoredRecord]:
        """Store what is new in ``records`` and return it. ``latest`` (the highest stored
        revision per id) is updated in place. Unchanged content adds nothing."""
        seen_at = require_utc(seen_at)
        added: list[StoredRecord] = []
        for record in records:
            digest = record.content_hash()
            held = latest.get(record.id)
            if held is not None and held.content_hash == digest:
                continue
            row = StoredRecord(
                id=record.id,
                rev=held.rev + 1 if held is not None else 1,
                first_seen_at=seen_at,
                content_hash=digest,
                headline_key=headline_key(record.title) if record.kind == "news" else "",
                record=record,
            )
            latest[record.id] = row
            added.append(row)
        by_file: dict[Path, list[dict[str, Any]]] = {}
        for row in added:
            by_file.setdefault(self.records_file(row.record.kind, _month(seen_at)), []).append(
                row.to_json()
            )
        for path, rows in by_file.items():
            append_lines(path, rows)
        return added

    # ------------------------------------------------------------------ snapshots and runs
    def snapshot_file(self, target_id: str, month: str) -> Path:
        group, symbol = target_id.split(":", 1)
        return self.root / "snapshots" / group / symbol / f"{month}.jsonl"

    def append_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        at = parse_time(snapshot.get("as_of"))
        if at is None:
            raise StoreError("a snapshot needs an as_of time")
        append_lines(self.snapshot_file(str(snapshot["id"]), _month(at)), [snapshot])

    def append_run(self, run: Mapping[str, Any]) -> None:
        at = parse_time(run.get("at"))
        if at is None:
            raise StoreError("a run needs an at time")
        append_lines(self.root / "runs" / f"{_month(at)}.jsonl", [run])

    # ------------------------------------------------------------------ one writer
    @staticmethod
    def _lock_time(path: Path) -> datetime | None:
        """When the lock was taken: its own record, else the file's time (a lock caught between
        being created and being written is still someone's lock). ``None`` once it is gone."""
        held = read_jsonl(path)
        stated = parse_time(held[0].get("at")) if held else None
        if stated is not None:
            return stated
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            return None

    @contextmanager
    def writer_lock(self, now: datetime, *, name: str = LOCK_NAME) -> Iterator[None]:
        """Hold the store for one refresh. Raises :class:`StoreBusy` when another refresh has
        held it for less than :data:`LOCK_STALE_AFTER`.

        ``name`` is the lock file: a writer of other files (the catalyst labels) takes its own,
        so it neither waits for a refresh nor blocks one."""
        now = require_utc(now)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        for attempt in (1, 2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                since = self._lock_time(path)
                if attempt == 2 or (since is not None and now - since < LOCK_STALE_AFTER):
                    what = "refresh" if name == LOCK_NAME else "writer"
                    raise StoreBusy(
                        f"another intel {what} is running"
                        + (f" (since {iso_utc(since)})" if since else "")
                    ) from None
                path.unlink(missing_ok=True)  # a writer that died; take over
                continue
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "at": iso_utc(now)}) + "\n")
            break
        try:
            yield
        finally:
            path.unlink(missing_ok=True)
