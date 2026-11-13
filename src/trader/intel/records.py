"""The intel feed's record: one piece of outside information, with when it was published and
when this machine first saw it (ADR-0047; ``NewsAnnotation`` in docs/specs/architecture.md).

Titles and summaries are untrusted text written by someone else. ``clean_text`` reduces them to
one plain line of bounded length before they are stored: no markup, no control or invisible
characters. An article body is never part of a record.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from trader.domain.hashing import content_hash, sha256_hex
from trader.domain.types import iso_utc, require_utc

SCHEMA_VERSION = 1
Kind = Literal["news", "insider", "premarket"]
KINDS: tuple[Kind, ...] = ("news", "insider", "premarket")
TITLE_LIMIT = 200
SUMMARY_LIMIT = 400

_TAG = re.compile(r"<[^>]{0,500}>")
_WORD = re.compile(r"[a-z0-9]+")


def clean_text(value: object, limit: int) -> str:
    """One plain line: entities decoded, tags and invisible characters removed, length capped."""
    if not isinstance(value, str):
        return ""
    text = _TAG.sub(" ", html.unescape(value[: limit * 20]))
    # Cc = control, Cf = format (zero-width and bidirectional marks), Co/Cs = private/surrogate.
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or unicodedata.category(ch) not in {"Cc", "Cf", "Co", "Cs"}
    )
    line = " ".join(kept.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def headline_key(title: str) -> str:
    """The same headline from two outlets gets the same key (case, spacing and punctuation
    ignored), so a syndicated story counts once."""
    words = " ".join(_WORD.findall(title.lower()))
    return sha256_hex(words)[:16] if words else ""


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class IntelRecord:
    """What a source reported. ``facts`` holds JSON values only, numbers as decimal strings."""

    kind: Kind
    source: str
    source_id: str
    targets: tuple[str, ...]
    title: str
    category: str
    extractor_version: str
    url: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    summary: str = ""
    tags: tuple[str, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "source_id": self.source_id,
            "url": self.url,
            "targets": list(self.targets),
            "published_at": iso_utc(self.published_at) if self.published_at else None,
            "updated_at": iso_utc(self.updated_at) if self.updated_at else None,
            "category": self.category,
            "tags": list(self.tags),
            "extractor_version": self.extractor_version,
            "title": self.title,
            "summary": self.summary,
            "facts": dict(self.facts),
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> IntelRecord:
        kind = data.get("kind")
        if kind not in KINDS:
            raise ValueError(f"unknown intel record kind {kind!r}")
        return cls(
            kind=kind,
            source=str(data["source"]),
            source_id=str(data["source_id"]),
            url=data.get("url") if isinstance(data.get("url"), str) else None,
            targets=tuple(str(t) for t in data.get("targets", [])),
            published_at=parse_time(data.get("published_at")),
            updated_at=parse_time(data.get("updated_at")),
            category=str(data.get("category", "unknown")),
            tags=tuple(str(t) for t in data.get("tags", [])),
            extractor_version=str(data.get("extractor_version", "")),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            facts=dict(data.get("facts") or {}),
            provenance={str(k): str(v) for k, v in (data.get("provenance") or {}).items()},
        )

    def content_hash(self) -> str:
        """Identity of the content: a later copy with the same hash is not a revision."""
        return content_hash(self.to_json())


@dataclass(frozen=True, slots=True)
class StoredRecord:
    """A record as one line of the store: what was reported, and when it was first seen here."""

    id: str
    rev: int
    first_seen_at: datetime
    content_hash: str
    headline_key: str
    record: IntelRecord

    @property
    def effective_at(self) -> datetime:
        """When the item counts as published: the source's time, never later than first seen."""
        published = self.record.published_at
        if published is None or published > self.first_seen_at:
            return self.first_seen_at
        return published

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "rev": self.rev,
            "first_seen_at": iso_utc(self.first_seen_at),
            "content_hash": self.content_hash,
            "headline_key": self.headline_key,
            "record": self.record.to_json(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> StoredRecord:
        seen = parse_time(data.get("first_seen_at"))
        if data.get("schema_version") != SCHEMA_VERSION or seen is None:
            raise ValueError("not an intel store row of this schema version")
        return cls(
            id=str(data["id"]),
            rev=int(data["rev"]),
            first_seen_at=seen,
            content_hash=str(data["content_hash"]),
            headline_key=str(data.get("headline_key", "")),
            record=IntelRecord.from_json(data["record"]),
        )
