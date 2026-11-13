"""Recorded sessions: a manifest plus a JSONL event file verified by SHA-256.

The manifest records feed, provenance, whether the data is synthetic, the generator and seed,
known limitations and (for synthetic fixtures only) declared edge assumptions. A checksum
mismatch, out-of-order receipt times or duplicate sequence numbers reject the session.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, TypeAdapter

from trader.domain.base import ConfigModel
from trader.domain.candidates import ReturnBasis
from trader.domain.hashing import content_hash
from trader.domain.market import AnyMarketEvent, MarketEvent
from trader.domain.types import Dec, DomainError, NonNegDec

_EVENT = TypeAdapter(MarketEvent)


class EdgeAssumptions(ConfigModel):
    """A declared, *unvalidated* edge used only to exercise the cost-aware path in replay."""

    expected_gross_return: Dec
    uncertainty_margin: NonNegDec
    basis: ReturnBasis
    label: str = Field(min_length=10)


class SessionManifest(ConfigModel):
    name: str
    description: str
    synthetic: bool
    feed: str
    session_date: str
    instruments: tuple[str, ...]
    events_file: str
    events_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_count: int = Field(ge=1)
    generator: dict[str, str] = Field(default_factory=dict)
    edge_assumptions: EdgeAssumptions | None = None
    known_limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordedSession:
    manifest: SessionManifest
    manifest_hash: str
    events: tuple[AnyMarketEvent, ...]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_session(directory: Path) -> RecordedSession:
    manifest = SessionManifest.model_validate_json((directory / "manifest.json").read_text("utf-8"))
    events_path = directory / manifest.events_file
    digest = sha256_file(events_path)
    if digest != manifest.events_sha256:
        raise DomainError(
            f"event file checksum mismatch for {manifest.name}: {digest} != {manifest.events_sha256}"
        )
    events: list[AnyMarketEvent] = []
    seen: set[tuple[str, str, int, int]] = set()
    last_receipt = None
    with events_path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            event = _EVENT.validate_json(line)
            key = (event.feed, event.instrument_id, event.sequence, event.correction_version)
            if key in seen:
                raise DomainError(f"duplicate event sequence at line {line_no}")
            seen.add(key)
            if last_receipt is not None and event.receipt_time < last_receipt:
                raise DomainError(f"events out of receipt order at line {line_no}")
            last_receipt = event.receipt_time
            if event.instrument_id not in manifest.instruments:
                raise DomainError(f"unexpected instrument {event.instrument_id} at line {line_no}")
            events.append(event)
    if len(events) != manifest.event_count:
        raise DomainError("event count does not match the manifest")
    return RecordedSession(
        manifest=manifest,
        manifest_hash=content_hash(manifest.model_dump(mode="python")),
        events=tuple(events),
    )
