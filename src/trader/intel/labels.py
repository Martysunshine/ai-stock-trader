"""Catalyst labels: why an instrument is moving, written down before its session (ADR-0051).

A label is one reader's judgement of the intel feed for one instrument and one session: the
kind of catalyst, the direction it points, how sure the reader is, and the stored records it
rests on. The reader is the assistant during the daily session (or a person); the feed's
keyword classifier is not a labeller.

Labels exist to become a testable input later. That needs each one to be provably older than
the prices it might explain, so:

* ``first_seen_at`` is set here from the clock, never by the caller;
* a label is refused for a session that is already past;
* every reference it cites must be a record or snapshot the store already holds;
* a label is never rewritten: a change of mind is appended as the next revision of the same
  ``id``, and both stay on file.

Under ``data/intel/labels/<YYYY-MM>.jsonl``, filed by the month the row was first seen. Nothing
that decides a trade reads these files (``tests/test_boundaries.py``); a rule that uses them
needs its own registered trial and decision record first (ADR-0047).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from trader.domain.hashing import content_hash
from trader.domain.types import iso_utc, require_utc
from trader.intel.brief import is_evidence_ref
from trader.intel.feed import DEFAULT_ROOT, Echo, read_index
from trader.intel.records import Kind, clean_text, parse_time
from trader.intel.store import LOAD_WINDOW, IntelStore, StoreError, append_lines, read_jsonl

SCHEMA_VERSION = 1
#: The rubric a label was written under (docs/runbook.md section 14). A change of the rubric is
#: a new method name, so a later study can tell the two apart.
METHOD = "catalyst-label:v1"
CATALYSTS = (
    "earnings",  # results or guidance from the company
    "analyst",  # a rating or price-target change
    "corporate",  # merger, buyback, offering, split, management change
    "product",  # product, contract, customer or approval news about the company
    "legal",  # lawsuit, investigation or regulatory action
    "sector",  # news about a peer or the industry
    "macro",  # a market-wide move: rates, data, politics
    "none",  # nothing in the feed explains a move
    "unclear",  # there is news, and its bearing cannot be judged
)
DIRECTIONS = ("up", "down", "unclear")
CONFIDENCES = ("low", "medium", "high")
NOTE_LIMIT = 200
MAX_EVIDENCE = 8
#: A label is for today's session or one of the next few (Friday evening for Monday).
MAX_DAYS_AHEAD = 4
#: How far back existing revisions of a label are looked for.
REVISION_WINDOW = timedelta(days=40)
LABELLER_PATTERN = r"^[a-z0-9][a-z0-9._:-]{2,63}$"
TARGET_PATTERN = r"^[a-z][a-z0-9]{0,15}:[A-Z0-9_]{1,16}$"
LOCK_NAME = ".labels.lock"


class LabelError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Label:
    target: str
    session_date: date
    catalyst: str
    direction: str
    confidence: str
    evidence: tuple[str, ...]
    note: str
    labeller: str
    method: str = METHOD

    @property
    def id(self) -> str:
        return f"{self.target}@{self.session_date.isoformat()}"

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "session_date": self.session_date.isoformat(),
            "catalyst": self.catalyst,
            "direction": self.direction,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "note": self.note,
            "labeller": self.labeller,
            "method": self.method,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Label:
        return cls(
            target=str(data["target"]),
            session_date=date.fromisoformat(str(data["session_date"])),
            catalyst=str(data["catalyst"]),
            direction=str(data["direction"]),
            confidence=str(data["confidence"]),
            evidence=tuple(str(e) for e in data.get("evidence", [])),
            note=str(data.get("note", "")),
            labeller=str(data["labeller"]),
            method=str(data.get("method", "")),
        )


@dataclass(frozen=True, slots=True)
class StoredLabel:
    """A label as one line of the store: the judgement, and when this machine first held it."""

    id: str
    rev: int
    first_seen_at: datetime
    content_hash: str
    label: Label

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "rev": self.rev,
            "first_seen_at": iso_utc(self.first_seen_at),
            "content_hash": self.content_hash,
            "label": self.label.to_json(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> StoredLabel:
        seen = parse_time(data.get("first_seen_at"))
        if data.get("schema_version") != SCHEMA_VERSION or seen is None:
            raise ValueError("not a label row of this schema version")
        return cls(
            id=str(data["id"]),
            rev=int(data["rev"]),
            first_seen_at=seen,
            content_hash=str(data["content_hash"]),
            label=Label.from_json(data["label"]),
        )


def labels_file(root: Path, month: str) -> Path:
    return root / "labels" / f"{month}.jsonl"


def _months(since: datetime, until: datetime) -> list[str]:
    year, month = since.year, since.month
    out: list[str] = []
    while (year, month) <= (until.year, until.month):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def load_labels(root: Path, *, since: datetime, until: datetime) -> list[StoredLabel]:
    """Every stored revision first seen from ``since`` to ``until``, oldest first."""
    since, until = require_utc(since), require_utc(until)
    out: list[StoredLabel] = []
    for month in _months(since, until):
        for raw in read_jsonl(labels_file(root, month)):
            try:
                row = StoredLabel.from_json(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if since <= row.first_seen_at <= until:
                out.append(row)
    return sorted(out, key=lambda r: (r.first_seen_at, r.id, r.rev))


def latest_labels(rows: Sequence[StoredLabel]) -> list[StoredLabel]:
    """The highest revision of each label, ordered by session and instrument."""
    latest: dict[str, StoredLabel] = {}
    for row in rows:
        held = latest.get(row.id)
        if held is None or row.rev > held.rev:
            latest[row.id] = row
    return sorted(latest.values(), key=lambda r: (r.label.session_date, r.label.target))


def _known_targets(root: Path) -> set[str]:
    index = read_index(root)
    return {str(t.get("id")) for t in index.get("targets", []) if isinstance(t, dict)}


def _resolves(store: IntelStore, ref: str, now: datetime) -> bool:
    """Whether ``ref`` names a record or snapshot the store held at ``now``."""
    kind, rest = ref.split(":", 1)
    if kind == "snap":
        target, _, as_of = rest.partition("@")
        at = parse_time(as_of)
        if at is None or at > now or not re.fullmatch(TARGET_PATTERN, target):
            return False
        rows = read_jsonl(store.snapshot_file(target, at.strftime("%Y-%m")))
        return any(row.get("id") == target and row.get("as_of") == as_of for row in rows)
    record_kind = cast(Kind, kind)
    held = store.load(record_kind, since=now - LOAD_WINDOW[record_kind], until=now)
    return any(row.id == rest for row in held)


def check_label(root: Path, label: Label, now: datetime) -> None:
    """Refuse a label that is malformed, late, or rests on something the store does not hold."""
    now = require_utc(now)
    if not re.fullmatch(TARGET_PATTERN, label.target):
        raise LabelError(f"target must look like stocks:NVDA, not {label.target!r}")
    known = _known_targets(root)
    if not known:
        raise LabelError(
            f"no intel files in {root}; run `python -m trader.intel refresh` (or start the console)"
        )
    if label.target not in known:
        raise LabelError(f"{label.target} is not an instrument of the intel feed")
    if label.catalyst not in CATALYSTS:
        raise LabelError(f"catalyst must be one of {', '.join(CATALYSTS)}")
    if label.direction not in DIRECTIONS:
        raise LabelError(f"direction must be one of {', '.join(DIRECTIONS)}")
    if label.confidence not in CONFIDENCES:
        raise LabelError(f"confidence must be one of {', '.join(CONFIDENCES)}")
    if label.catalyst == "none" and label.direction != "unclear":
        raise LabelError("a label without a catalyst has no direction: use --direction unclear")
    if not re.fullmatch(LABELLER_PATTERN, label.labeller):
        raise LabelError("labeller must be a short lower-case id, e.g. claude-fable-5")
    today = now.date()
    if label.session_date.weekday() >= 5:
        raise LabelError(f"{label.session_date} is a weekend day, not a session")
    if label.session_date < today:
        raise LabelError(
            f"session {label.session_date} is past: a label is written before or during its "
            "session, never afterwards"
        )
    if label.session_date > today + timedelta(days=MAX_DAYS_AHEAD):
        raise LabelError(f"session {label.session_date} is more than {MAX_DAYS_AHEAD} days ahead")
    if not 1 <= len(label.evidence) <= MAX_EVIDENCE:
        raise LabelError(
            f"cite 1 to {MAX_EVIDENCE} references from `trader intel brief` (the instrument's "
            "snapshot reference when there is no news)"
        )
    if len(set(label.evidence)) != len(label.evidence):
        raise LabelError("a reference is cited twice")
    store = IntelStore(root)
    for ref in label.evidence:
        if not is_evidence_ref(ref):
            raise LabelError(f"{ref!r} is not an intel reference")
        if not _resolves(store, ref, now):
            raise LabelError(f"{ref} is not in the intel store: cite what the brief printed")


def add_label(root: Path, label: Label, *, now: datetime) -> StoredLabel | None:
    """Store ``label`` as the next revision of its id; ``None`` when it repeats the latest one.

    The note is reduced to one plain line first, as every other text in the store.
    """
    now = require_utc(now)
    label = Label(
        target=label.target,
        session_date=label.session_date,
        catalyst=label.catalyst,
        direction=label.direction,
        confidence=label.confidence,
        evidence=tuple(label.evidence),
        note=clean_text(label.note, NOTE_LIMIT),
        labeller=label.labeller,
        method=METHOD,
    )
    check_label(root, label, now)
    digest = content_hash(label.to_json())
    store = IntelStore(root)
    with store.writer_lock(now, name=LOCK_NAME):
        held = [
            row
            for row in load_labels(root, since=now - REVISION_WINDOW, until=now)
            if row.id == label.id
        ]
        last = max(held, key=lambda r: r.rev, default=None)
        if last is not None and last.content_hash == digest:
            return None
        row = StoredLabel(
            id=label.id,
            rev=last.rev + 1 if last is not None else 1,
            first_seen_at=now,
            content_hash=digest,
            label=label,
        )
        append_lines(labels_file(root, now.strftime("%Y-%m")), [row.to_json()])
    return row


def render_labels(rows: Sequence[StoredLabel]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        label = row.label
        lines.append(
            f"{label.session_date.isoformat()}  {label.target:<14} {label.catalyst:<9} "
            f"{label.direction:<7} {label.confidence:<6} rev {row.rev}, seen "
            f"{iso_utc(row.first_seen_at)} by {label.labeller}"
        )
        if label.note:
            lines.append(f"    {label.note}")
        lines.append(f"    [{'] ['.join(label.evidence)}]")
    return lines


def run_label(
    *,
    target: str,
    session: str,
    catalyst: str,
    direction: str,
    confidence: str,
    evidence: Sequence[str],
    labeller: str,
    note: str = "",
    root: Path | None = None,
    echo: Echo = print,
) -> int:
    """Store one label now. Raises :class:`LabelError` when it is refused."""
    from trader.ops.clock import SystemClock

    try:
        session_date = date.fromisoformat(session)
    except ValueError:
        raise LabelError(f"--date must be YYYY-MM-DD, not {session!r}") from None
    label = Label(
        target=target,
        session_date=session_date,
        catalyst=catalyst,
        direction=direction,
        confidence=confidence,
        evidence=tuple(evidence),
        note=note,
        labeller=labeller,
    )
    try:
        stored = add_label(root or DEFAULT_ROOT, label, now=SystemClock().now())
    except StoreError as exc:
        raise LabelError(str(exc)) from None
    if stored is None:
        echo(f"unchanged: {label.id} already carries this label")
    else:
        echo(f"stored {stored.id} rev {stored.rev} at {iso_utc(stored.first_seen_at)}")
    return 0


def run_labels(*, session: str | None = None, root: Path | None = None, echo: Echo = print) -> int:
    """Print the latest revision of each label of ``session`` (default: the last 40 days)."""
    from trader.ops.clock import SystemClock

    now = SystemClock().now()
    wanted: date | None = None
    if session is not None:
        try:
            wanted = date.fromisoformat(session)
        except ValueError:
            raise LabelError(f"--date must be YYYY-MM-DD, not {session!r}") from None
    rows = latest_labels(load_labels(root or DEFAULT_ROOT, since=now - REVISION_WINDOW, until=now))
    if wanted is not None:
        rows = [row for row in rows if row.label.session_date == wanted]
    if not rows:
        echo("no labels" + (f" for {wanted}" if wanted else ""))
        return 0
    for line in render_labels(rows):
        echo(line)
    return 0
