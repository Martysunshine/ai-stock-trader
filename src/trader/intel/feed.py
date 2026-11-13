"""The intel feed: read the sources, append what is new, write the console's files (ADR-0047).

A data tool, not the trading runtime: it reads news, filings and pre-market bars and writes
``data/intel/`` only (``trader.intel.store`` describes the append-only files). On each refresh:

1. every source that is due runs; one failing source never stops the others;
2. new records and revisions are appended;
3. a snapshot of what is known is computed per instrument and appended when it changed;
4. ``latest/<group>/<SYMBOL>.json`` (the newest items of one instrument) and ``index.json`` (the
   state of every source and instrument) are replaced atomically, the index last;
5. one line is added to the run log.

Each source keeps its own cadence (``configs/intel/sources.yaml``); the watcher ticks at the
shortest one. A source's cursor is saved in the index only after its records were appended, so a
crash in between repeats a request and never loses an item.

Light by design (it runs beside the console all day): no pandas, numpy, DuckDB, SQLAlchemy or
exchange calendar is imported (``tests/test_intel_refresh.py`` checks this).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trader.domain.types import iso_utc, require_utc
from trader.intel.config import (
    DEFAULT_CONFIG,
    SOURCE_IDS,
    SOURCE_KIND,
    IntelConfig,
    SourceConfig,
    SourceId,
    Target,
    load_intel_config,
)
from trader.intel.records import StoredRecord, parse_time
from trader.intel.snapshot import snapshot, visible
from trader.intel.sources import (
    AlpacaNewsSource,
    AlpacaPremarketSource,
    EdgarForm4Source,
    IntelSource,
    SourceResult,
)
from trader.intel.store import IntelStore, StoreBusy, latest_by_id
from trader.market.feed import seconds_to_next_run, write_json_atomic
from trader.market.sources import short_error
from trader.market.watchlists import DEFAULT_WATCHLISTS, REPO_ROOT, Watchlists

if TYPE_CHECKING:
    from trader.ops.clock import Clock, SleepingClock

INDEX_VERSION = 1
DEFAULT_ROOT = REPO_ROOT / "data" / "intel"
INDEX_NAME = "index.json"
#: Items kept in an instrument's ``latest`` file.
LATEST_NEWS = 30
LATEST_INSIDERS = 20
Echo = Callable[[str], None]


class FeedError(RuntimeError):
    pass


def latest_file(target: Target) -> str:
    """The ``latest`` file of one instrument, relative to the feed root (forward slashes)."""
    return f"latest/{target.group}/{target.symbol}.json"


def read_index(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------------------- sources
def _is_due(previous: Mapping[str, Any], config: SourceConfig, now: datetime) -> bool:
    retry_after = parse_time(previous.get("retry_after"))
    if retry_after is not None and now < retry_after:
        return False
    checked = parse_time(previous.get("checked_at"))
    if checked is None:
        return True
    # Once per cadence slot (clock-aligned: :00, :05, ... for five minutes), whenever in the slot
    # the last check fell. A watcher started mid-slot is then on the grid from its second run.
    period = config.every_minutes * 60
    return int(now.timestamp()) // period > int(checked.timestamp()) // period


def _cursor(previous: Mapping[str, Any]) -> dict[str, str]:
    raw = previous.get("cursor")
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, str)}


def _run_source(
    source_id: SourceId,
    config: SourceConfig,
    source: IntelSource | None,
    targets: Sequence[Target],
    previous: Mapping[str, Any],
    store: IntelStore,
    latest: dict[str, StoredRecord],
    now: datetime,
) -> tuple[dict[str, Any], list[StoredRecord]]:
    cursor = _cursor(previous)
    updated_at = previous.get("updated_at") if isinstance(previous.get("updated_at"), str) else None
    added: list[StoredRecord] = []
    error: str | None = None
    result = SourceResult(cursor=cursor)
    if source is None:
        result.status, error = "error", f"no source configured for {source_id}"
    else:
        try:
            result = source.fetch(targets, cursor, now=now)
            added = store.append(result.records, seen_at=now, latest=latest)
            cursor = dict(result.cursor)  # only once the records are on disk
            if result.errors:
                error = short_error(
                    ValueError("; ".join(f"{k}: {v}" for k, v in sorted(result.errors.items()))),
                    600,
                )
        except Exception as exc:  # one source failing must not stop the others
            result = SourceResult(status="error")
            error = short_error(exc)
    if result.status in ("ok", "partial"):
        updated_at = iso_utc(now)
    retry_after = result.retry_after
    entry = {
        "id": source_id,
        "kind": SOURCE_KIND[source_id],
        "label": config.label,
        "every_minutes": config.every_minutes,
        "status": result.status,
        "note": result.note,
        "error": error,
        "updated_at": updated_at,
        "checked_at": iso_utc(now),
        "retry_after": iso_utc(retry_after) if retry_after and retry_after > now else None,
        "new": len(added),
        "cursor": cursor,
    }
    return entry, added


def _carried(
    source_id: SourceId, config: SourceConfig, previous: Mapping[str, Any]
) -> dict[str, Any]:
    """The entry of a source that did not run this time: its last state, or a first placeholder."""
    if previous:
        return {**previous, "new": 0}
    return {
        "id": source_id,
        "kind": SOURCE_KIND[source_id],
        "label": config.label,
        "every_minutes": config.every_minutes,
        "status": "idle",
        "note": "not read yet",
        "error": None,
        "updated_at": None,
        "checked_at": None,
        "retry_after": None,
        "new": 0,
        "cursor": {},
    }


# ----------------------------------------------------------------------------- instruments
def _news_item(row: StoredRecord, proxy: str | None) -> dict[str, Any]:
    record = row.record
    return {
        "id": row.id,
        "rev": row.rev,
        "title": record.title,
        "summary": record.summary,
        "category": record.category,
        "tags": list(record.tags),
        "at": iso_utc(row.effective_at),
        "first_seen_at": iso_utc(row.first_seen_at),
        "url": record.url,
        "feed": record.provenance.get("feed", ""),
        "author": record.provenance.get("author", ""),
        "proxy": proxy,
    }


def _insider_item(row: StoredRecord) -> dict[str, Any]:
    record = row.record
    return {
        "id": row.id,
        "rev": row.rev,
        "title": record.title,
        "category": record.category,
        "at": iso_utc(row.effective_at),
        "first_seen_at": iso_utc(row.first_seen_at),
        "url": record.url,
        "facts": dict(record.facts),
    }


def latest_payload(
    target: Target, name: str, snap: Mapping[str, Any], current: Sequence[StoredRecord]
) -> dict[str, Any]:
    """The newest items of one instrument, newest first (``current`` = visible records)."""
    mine = [r for r in current if target.id in r.record.targets]
    stories: dict[str, StoredRecord] = {}
    for row in mine:
        if row.record.kind == "news":
            stories.setdefault(row.headline_key or row.id, row)  # a syndicated copy shows once
    news = sorted(stories.values(), key=lambda r: (r.effective_at, r.id), reverse=True)
    insiders = [r for r in reversed(mine) if r.record.kind == "insider"]
    return {
        "id": target.id,
        "name": name,
        "generated_at": snap["as_of"],
        "snapshot": dict(snap),
        "news": [_news_item(r, target.news_proxy) for r in news[:LATEST_NEWS]],
        "insiders": [_insider_item(r) for r in insiders[:LATEST_INSIDERS]],
    }


def _target_entry(
    target: Target, name: str, snap: Mapping[str, Any], snapshot_at: str
) -> dict[str, Any]:
    news, insiders, premarket = snap.get("news"), snap.get("insiders"), snap.get("premarket")
    return {
        "id": target.id,
        "group": target.group,
        "symbol": target.symbol,
        "name": name,
        "file": latest_file(target),
        "has": {
            "news": news is not None,
            "insiders": insiders is not None,
            "premarket": premarket is not None,
        },
        "news_proxy": target.news_proxy,
        "news_count_24h": news["count_24h"] if news else None,
        "last_news_at": news["last_at"] if news else None,
        "insider_net_value": insiders["net_value"] if insiders else None,
        "premarket_session": premarket.get("session") if premarket else None,
        "premarket_gap_pct": premarket.get("gap_pct") if premarket else None,
        "snapshot_hash": snap["hash"],
        "snapshot_at": snapshot_at,
    }


# ----------------------------------------------------------------------------- refresh
def refresh(
    config: IntelConfig,
    watchlists: Watchlists,
    root: Path,
    sources: Mapping[str, IntelSource],
    *,
    clock: Clock,
    only: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run every due source (or exactly ``only``), store what is new, then write the index."""
    store = IntelStore(root)
    wanted = set(only) if only else None
    if wanted is not None:
        unknown = wanted - set(config.sources)
        if unknown:
            raise FeedError(f"unknown source(s): {', '.join(sorted(unknown))}")
    now = require_utc(clock.now()).replace(microsecond=0)
    names = config.names(watchlists)
    with store.writer_lock(now):
        previous = read_index(root)
        before = {
            s.get("id"): s
            for s in previous.get("sources", [])
            if isinstance(s, dict) and s.get("id")
        }
        rows = store.load_all(now)
        latest = latest_by_id(rows)
        entries: list[dict[str, Any]] = []
        for source_id in SOURCE_IDS:
            source_config = config.sources.get(source_id)
            if source_config is None:
                continue
            last = before.get(source_id, {})
            run = source_id in wanted if wanted is not None else _is_due(last, source_config, now)
            if not run:
                entries.append(_carried(source_id, source_config, last))
                continue
            entry, added = _run_source(
                source_id,
                source_config,
                sources.get(source_id),
                config.targets,
                last,
                store,
                latest,
                now,
            )
            entries.append(entry)
            rows += added
        statuses = {str(e["id"]): str(e["status"]) for e in entries}
        form4 = config.sources.get("edgar_form4")
        insider_days = form4.lookback_days if form4 is not None else 90
        current = visible(rows, now)
        known = {
            t.get("id"): t
            for t in previous.get("targets", [])
            if isinstance(t, dict) and t.get("id")
        }
        targets: list[dict[str, Any]] = []
        for target in config.targets:
            snap = snapshot(rows, target, now, statuses=statuses, insider_days=insider_days)
            held = known.get(target.id, {})
            snapshot_at = held.get("snapshot_at")
            if held.get("snapshot_hash") != snap["hash"] or not isinstance(snapshot_at, str):
                store.append_snapshot(snap)
                snapshot_at = snap["as_of"]
            name = names.get(target.id, target.symbol)
            write_json_atomic(
                root / latest_file(target), latest_payload(target, name, snap, current)
            )
            targets.append(_target_entry(target, name, snap, snapshot_at))
        index = {
            "version": INDEX_VERSION,
            "generated_at": iso_utc(now),
            "every_minutes": min(c.every_minutes for c in config.sources.values()),
            "sources": entries,
            "targets": targets,
        }
        write_json_atomic(root / INDEX_NAME, index, indent=1)
        store.append_run(
            {
                "at": iso_utc(now),
                "sources": statuses,
                "new": sum(int(e["new"]) for e in entries),
            }
        )
    return index


def summary_line(index: Mapping[str, Any]) -> str:
    parts = []
    for source in index.get("sources", []):
        text = f"{source['id']} {source['status']}"
        if source.get("new"):
            text += f" (+{source['new']})"
        detail = source.get("error") or source.get("note")
        if detail and source["status"] != "ok":
            text += f": {detail}"
        parts.append(text)
    return f"{index.get('generated_at')} intel feed: " + " · ".join(parts)


def default_sources(config: IntelConfig) -> dict[str, IntelSource]:
    """Real sources: keys and the contact are read when a source runs, so a missing one fails
    (or parks) that source only."""
    from trader.config.secrets import require_alpaca, require_sec_edgar_contact
    from trader.data.sources.http import UrllibTransport

    out: dict[str, IntelSource] = {}
    if "alpaca_news" in config.sources:
        out["alpaca_news"] = AlpacaNewsSource(
            UrllibTransport(min_interval_seconds=0.35, max_attempts=3),
            require_alpaca,
            config.sources["alpaca_news"],
        )
    if "alpaca_premarket" in config.sources:
        out["alpaca_premarket"] = AlpacaPremarketSource(
            UrllibTransport(min_interval_seconds=0.35, max_attempts=3),
            require_alpaca,
            config.sources["alpaca_premarket"],
        )
    if "edgar_form4" in config.sources:
        # The SEC allows ten requests a second; this stays near three, with one retry.
        edgar = UrllibTransport(min_interval_seconds=0.35, max_attempts=2)
        out["edgar_form4"] = EdgarForm4Source(
            edgar, edgar, require_sec_edgar_contact, config.sources["edgar_form4"]
        )
    return out


def run_refresh(
    *,
    only: Sequence[str] | None = None,
    root: Path = DEFAULT_ROOT,
    config_path: Path = DEFAULT_CONFIG,
    watchlists_path: Path = DEFAULT_WATCHLISTS,
    echo: Echo = print,
) -> int:
    """One refresh with the real sources. Exit code 0 unless a source ended in ``error``."""
    from trader.ops.clock import SystemClock

    config, watchlists = load_intel_config(config_path, watchlists_path)
    try:
        index = refresh(
            config, watchlists, root, default_sources(config), clock=SystemClock(), only=only
        )
    except StoreBusy as exc:
        raise FeedError(f"{exc}; stop the watcher or wait for it to finish") from exc
    echo(summary_line(index))
    return 1 if any(s["status"] == "error" for s in index["sources"]) else 0


def run_watch(
    *,
    root: Path = DEFAULT_ROOT,
    config_path: Path = DEFAULT_CONFIG,
    watchlists_path: Path = DEFAULT_WATCHLISTS,
    echo: Echo = print,
    clock: SleepingClock | None = None,
    sources: Mapping[str, IntelSource] | None = None,
    iterations: int | None = None,
) -> int:
    """Refresh now, then shortly after every cadence boundary, until interrupted."""
    from trader.ops.clock import SystemClock

    clk: SleepingClock = clock or SystemClock()
    config, watchlists = load_intel_config(config_path, watchlists_path)
    live_sources = sources or default_sources(config)
    every = min(c.every_minutes for c in config.sources.values())
    echo(
        f"intel feed: {len(config.sources)} sources, {len(config.targets)} instruments, every "
        f"{every} min, writing {root} (data only; no order authority)"
    )
    done = 0
    try:
        while iterations is None or done < iterations:
            try:
                echo(summary_line(refresh(config, watchlists, root, live_sources, clock=clk)))
            except Exception as exc:  # keep running; the index shows the last good state
                echo(f"intel feed: refresh failed: {short_error(exc)}")
            done += 1
            if iterations is not None and done >= iterations:
                break
            clk.sleep(seconds_to_next_run(clk.now(), every))
    except KeyboardInterrupt:
        echo("intel feed: stopped")
    return 0
