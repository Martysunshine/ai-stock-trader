"""The dashboard's market feed: prices for three watchlist groups, written as files (ADR-0045).

A data tool, not the trading runtime: it reads market data and writes ``data/market/`` only. The
Trader Console reads those files and never calls a broker itself (docs/observability.md §6).

* ``data/market/<group>/<SYMBOL>.json``: ``{"id", "series": {"5m": [[t, "o", "h", "l", "c",
  "v"], ...], "1h": [...], "1d": [...]}}``. ``t`` is the bar start in epoch seconds; prices and
  volume are decimal strings, exactly as the source gave them (mids for Capital.com, whose volume
  is ``"0"``). The last bar of a series may still be forming; the next refresh replaces it.
* ``data/market/index.json``, written last: version, ``generated_at``, ``every_minutes``, and per
  group its id, title, source, day rule, status (``ok``, ``partial`` or ``error``) with the error,
  ``updated_at`` (the last refresh that succeeded), ``checked_at`` and ``retry_after``. Each
  instrument carries its id (``stocks:AAPL``), symbol, name, file, display precision, volume flag,
  price basis, ``first``/``last``/``count`` per series and its newest close (``last_price``,
  ``last_at``).
* Each refresh merges new bars into the stored ones by bar start (new values win), trims each
  series to its retention (``configs/market/watchlists.yaml``) and replaces each file atomically
  (temporary file + ``os.replace``), so a reader never sees a partial file.
* One failing group never stops the others: it is marked ``error`` in the index and its files keep
  the last good bars. A refused Capital.com session backs off for 30 minutes, across restarts
  (``retry_after``).
* Nothing is written under ``data/history`` or to ``research/data_manifest.json``: those are
  checksummed research inputs. The output folder must be named ``market``.

Light by design (it runs beside the console all day): no pandas, numpy, DuckDB, SQLAlchemy or
exchange calendar is imported (``tests/test_market_feed.py`` checks this).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from trader.data.history import HistoryBar
from trader.domain.types import canon_decimal, iso_utc, require_utc
from trader.market.sources import (
    AlpacaIexSource,
    CapitalDemoSource,
    FetchResult,
    GroupSource,
    KrakenPublicSource,
    Row,
    short_error,
)
from trader.market.watchlists import (
    DEFAULT_WATCHLISTS,
    REPO_ROOT,
    SERIES,
    GroupConfig,
    InstrumentConfig,
    SeriesName,
    Watchlists,
    load_watchlists,
)

if TYPE_CHECKING:
    from trader.ops.clock import Clock, SleepingClock

INDEX_VERSION = 1
DEFAULT_ROOT = REPO_ROOT / "data" / "market"
INDEX_NAME = "index.json"
#: A watch refresh runs this long after each cadence boundary, once the bar has closed.
SETTLE_SECONDS = 15
GroupStatus = Literal["ok", "partial", "error"]
Echo = Callable[[str], None]


class FeedError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- files
def check_root(root: Path) -> Path:
    """The output folder must be a ``market`` folder (never research data such as data/history)."""
    if root.name != "market" or "history" in root.parts:
        raise FeedError(f"refusing to write market files outside a data/market folder: {root}")
    return root


def instrument_file(group_id: str, symbol: str) -> str:
    """The file of one instrument, relative to the feed root (forward slashes)."""
    return f"{group_id}/{symbol}.json"


def write_json_atomic(path: Path, payload: Any, *, indent: int | None = None) -> None:
    """Write JSON (LF, UTF-8) to a temporary file beside ``path``, then ``os.replace`` it."""
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=indent,
        separators=(",", ": ") if indent is not None else (",", ":"),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace(Path(tmp), path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _replace(src: Path, dst: Path, attempts: int = 5) -> None:
    """``os.replace``, retried briefly: on Windows a reader can hold the target for a moment."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _valid_row(row: Any) -> bool:
    return (
        isinstance(row, list)
        and len(row) == 6
        and isinstance(row[0], int)
        and all(isinstance(v, str) for v in row[1:])
    )


def read_series(path: Path, instrument_id: str) -> dict[SeriesName, list[Row]]:
    """Stored bars of one instrument; empty when the file is missing, unreadable or foreign."""
    data = _read_json(path)
    if not isinstance(data, dict) or data.get("id") != instrument_id:
        return {s: [] for s in SERIES}
    series = data.get("series")
    out: dict[SeriesName, list[Row]] = {}
    for name in SERIES:
        rows = series.get(name) if isinstance(series, dict) else None
        out[name] = [r for r in rows if _valid_row(r)] if isinstance(rows, list) else []
    return out


def read_index(root: Path) -> dict[str, Any]:
    data = _read_json(root / INDEX_NAME)
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------------------- bars
def bar_row(bar: HistoryBar) -> Row:
    return [
        int(require_utc(bar.start).timestamp()),
        canon_decimal(bar.open),
        canon_decimal(bar.high),
        canon_decimal(bar.low),
        canon_decimal(bar.close),
        canon_decimal(bar.volume),
    ]


def merge_rows(
    stored: Sequence[Row], fetched: Sequence[HistoryBar], *, horizon: datetime
) -> list[Row]:
    """Stored and fetched bars by start time (fetched wins), without bars before ``horizon``."""
    cutoff = int(require_utc(horizon).timestamp())
    by_start: dict[int, Row] = {int(r[0]): list(r) for r in stored}
    for bar in fetched:
        row = bar_row(bar)
        by_start[int(row[0])] = row
    return [by_start[t] for t in sorted(by_start) if t >= cutoff]


def _series_summary(rows: Sequence[Row]) -> dict[str, int | None]:
    return {
        "first": int(rows[0][0]) if rows else None,
        "last": int(rows[-1][0]) if rows else None,
        "count": len(rows),
    }


def _instrument_entry(
    group: GroupConfig, inst: InstrumentConfig, series: Mapping[SeriesName, Sequence[Row]]
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": f"{group.id}:{inst.symbol}",
        "symbol": inst.symbol,
        "name": inst.name,
        "source_symbol": inst.source_symbol,
        "file": instrument_file(group.id, inst.symbol),
        "precision": inst.precision,
        "has_volume": group.has_volume,
        "basis": group.basis,
        "series": {name: _series_summary(series.get(name, ())) for name in SERIES},
    }
    if inst.min_deal is not None:
        entry["min_deal"] = canon_decimal(inst.min_deal)
    # The newest close of the finest series that has one (a row's headline price).
    latest = next((rows[-1] for name in SERIES if (rows := series.get(name))), None)
    entry["last_price"] = latest[4] if latest else None
    entry["last_at"] = latest[0] if latest else None
    return entry


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


# ----------------------------------------------------------------------------- refresh
def _refresh_group(
    group: GroupConfig,
    config: Watchlists,
    root: Path,
    source: GroupSource | None,
    previous: Mapping[str, Any],
    now: datetime,
    *,
    skip: bool = False,
) -> dict[str, Any]:
    stored = {
        inst.symbol: read_series(
            root / instrument_file(group.id, inst.symbol), f"{group.id}:{inst.symbol}"
        )
        for inst in group.instruments
    }
    status: GroupStatus = "ok"
    error: str | None = None
    updated_at = previous.get("updated_at") if isinstance(previous.get("updated_at"), str) else None
    retry_after = _parse_time(previous.get("retry_after"))
    if skip:
        status, error = "error", "not refreshed yet (this run was limited with --group)"
    elif retry_after is not None and now < retry_after:
        status = "error"
        error = previous.get("error") or f"backing off until {iso_utc(retry_after)}"
    elif source is None:
        status, error, retry_after = "error", f"no source configured for {group.source}", None
    else:
        retention = {name: config.retention(name) for name in SERIES}
        try:
            result = source.fetch(group, stored, now=now, retention=retention)
        except Exception as exc:  # one group failing must not stop the others
            result = None
            status, error = "error", short_error(exc)
            until = getattr(exc, "retry_after", None)
            retry_after = until if isinstance(until, datetime) and until > now else None
        if result is not None:
            retry_after = _store(group, config, root, result, stored, now)
            failed = [i.symbol for i in group.instruments if i.symbol in result.errors]
            if len(failed) == len(group.instruments):
                status = "error"
            elif failed:
                status = "partial"
            if failed:
                error = short_error(
                    ValueError("; ".join(f"{s}: {result.errors[s]}" for s in failed)), 600
                )
    if status != "error":
        updated_at = iso_utc(now)
    return {
        "id": group.id,
        "title": group.title,
        "source": group.source_label,
        "source_kind": group.source,
        "day_rule": group.day_rule,
        "basis": group.basis,
        "status": status,
        "error": error,
        "updated_at": updated_at,
        "checked_at": iso_utc(now),
        "retry_after": iso_utc(retry_after) if retry_after is not None else None,
        "instruments": [
            _instrument_entry(group, inst, stored[inst.symbol]) for inst in group.instruments
        ],
    }


def _store(
    group: GroupConfig,
    config: Watchlists,
    root: Path,
    result: FetchResult,
    stored: dict[str, dict[SeriesName, list[Row]]],
    now: datetime,
) -> datetime | None:
    for inst in group.instruments:
        fetched = result.bars.get(inst.symbol)
        if fetched is None or inst.symbol in result.errors:
            continue
        merged: dict[SeriesName, list[Row]] = {
            name: merge_rows(
                stored[inst.symbol].get(name, []),
                fetched.get(name, []),
                horizon=now - config.retention(name),
            )
            for name in SERIES
        }
        write_json_atomic(
            root / instrument_file(group.id, inst.symbol),
            {"id": f"{group.id}:{inst.symbol}", "series": merged},
        )
        stored[inst.symbol] = merged
    until = result.retry_after
    return until if until is not None and until > now else None


def refresh(
    config: Watchlists,
    root: Path,
    sources: Mapping[str, GroupSource],
    *,
    clock: Clock,
    every_minutes: int | None = None,
    groups: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Fetch every group (or only ``groups``), update its files, then write the index."""
    check_root(root)
    wanted = set(groups) if groups else None
    if wanted is not None:
        unknown = wanted - set(config.ids())
        if unknown:
            raise FeedError(f"unknown group(s): {', '.join(sorted(unknown))}")
    now = require_utc(clock.now()).replace(microsecond=0)
    previous = read_index(root)
    before = {
        g.get("id"): g for g in previous.get("groups", []) if isinstance(g, dict) and g.get("id")
    }
    out_groups: list[dict[str, Any]] = []
    for group in config.groups:
        skip = wanted is not None and group.id not in wanted
        if skip and group.id in before:
            out_groups.append(before[group.id])  # untouched this run
            continue
        out_groups.append(
            _refresh_group(
                group,
                config,
                root,
                sources.get(group.source),
                before.get(group.id, {}),
                now,
                skip=skip,
            )
        )
    index = {
        "version": INDEX_VERSION,
        "generated_at": iso_utc(now),
        "every_minutes": every_minutes or config.every_minutes,
        "retention_days": dict(config.retention_days),
        "groups": out_groups,
    }
    write_json_atomic(root / INDEX_NAME, index, indent=1)
    return index


def summary_line(index: Mapping[str, Any]) -> str:
    parts = []
    for g in index.get("groups", []):
        have = sum(1 for i in g.get("instruments", []) if i["series"]["5m"]["count"])
        text = f"{g['id']} {g['status']} ({have}/{len(g.get('instruments', []))} with bars)"
        if g.get("error"):
            text += f": {g['error']}"
        parts.append(text)
    return f"{index.get('generated_at')} market feed: " + " · ".join(parts)


def default_sources(clock: Clock) -> dict[str, GroupSource]:
    """Real sources: keys are read when a group is fetched, so a missing key fails that group."""
    from trader.config.secrets import require_alpaca, require_capital_demo
    from trader.data.sources.capital_prices import CapitalPrices
    from trader.data.sources.http import UrllibTransport

    return {
        "alpaca_iex": AlpacaIexSource(UrllibTransport(min_interval_seconds=0.35), require_alpaca),
        "kraken_public": KrakenPublicSource(UrllibTransport(min_interval_seconds=1.0)),
        "capital_demo": CapitalDemoSource(
            CapitalPrices(
                UrllibTransport(min_interval_seconds=0.2, max_attempts=3),
                require_capital_demo,
                clock=clock,
            )
        ),
    }


def seconds_to_next_run(now: datetime, every_minutes: int) -> float:
    """Seconds until the next cadence boundary plus :data:`SETTLE_SECONDS`."""
    period = every_minutes * 60
    ts = require_utc(now).timestamp()
    due = (ts // period + 1) * period + SETTLE_SECONDS
    if due - ts > period:  # still inside this boundary's settle window
        due -= period
    return max(1.0, due - ts)


def run_refresh(
    *,
    groups: Sequence[str] | None = None,
    root: Path = DEFAULT_ROOT,
    config_path: Path = DEFAULT_WATCHLISTS,
    echo: Echo = print,
) -> int:
    """One refresh with the real sources. Exit code 0 unless a group ended in ``error``."""
    from trader.ops.clock import SystemClock

    clock = SystemClock()
    index = refresh(
        load_watchlists(config_path), root, default_sources(clock), clock=clock, groups=groups
    )
    echo(summary_line(index))
    return 1 if any(g["status"] == "error" for g in index["groups"]) else 0


def run_watch(
    *,
    every_minutes: int = 5,
    root: Path = DEFAULT_ROOT,
    config_path: Path = DEFAULT_WATCHLISTS,
    echo: Echo = print,
    clock: SleepingClock | None = None,
    sources: Mapping[str, GroupSource] | None = None,
    iterations: int | None = None,
) -> int:
    """Refresh now, then shortly after every ``every_minutes`` boundary, until interrupted."""
    if not 1 <= every_minutes <= 60:
        raise FeedError("--every must be between 1 and 60 minutes")
    from trader.ops.clock import SystemClock

    clk: SleepingClock = clock or SystemClock()
    config = load_watchlists(config_path)
    live_sources = sources or default_sources(clk)
    echo(
        f"market feed: {len(config.groups)} groups, every {every_minutes} min, writing "
        f"{root} (data only; no order authority)"
    )
    done = 0
    try:
        while iterations is None or done < iterations:
            try:
                index = refresh(config, root, live_sources, clock=clk, every_minutes=every_minutes)
                echo(summary_line(index))
            except Exception as exc:  # keep running; the index shows the last good state
                echo(f"market feed: refresh failed: {short_error(exc)}")
            done += 1
            if iterations is not None and done >= iterations:
                break
            clk.sleep(seconds_to_next_run(clk.now(), every_minutes))
    except KeyboardInterrupt:
        echo("market feed: stopped")
    return 0
