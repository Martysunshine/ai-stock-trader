"""Acquire, validate, store and record history datasets (``trader ingest-history``).

Each manifest entry is processed independently. A dataset that cannot be acquired (no
credentials, no owner-downloaded archive) is recorded as ``BLOCKED`` with the exact owner action
instead of failing silently or being substituted by another source.
"""

from __future__ import annotations

import csv
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from trader.data.calendars import ExchangeCalendar
from trader.data.corporate_actions import SplitEvent
from trader.data.daily_bars import DAILY_FINALIZATION, expected_session_opens
from trader.data.history import (
    DataQualityError,
    HistoryBar,
    content_sha256,
    file_sha256,
    read_bars,
    safe_file_name,
    write_bars,
)
from trader.data.manifest import FileRecord, HistoryDataset, InstrumentRequest
from trader.data.quality import (
    QualityReport,
    clean_and_check,
    expected_starts_24_7,
    expected_starts_sessions,
)
from trader.data.sources.alpaca_history import (
    KEY_ENV,
    SECRET_ENV,
    AlpacaCredentials,
    fetch_alpaca_bars,
    fetch_alpaca_corporate_actions,
    fetch_alpaca_daily_bars,
)
from trader.data.sources.capital_history import (
    QuoteHistory,
    QuoteSource,
    fetch_capital_quote_history,
)
from trader.data.sources.http import JsonTransport
from trader.data.sources.public_crypto import (
    fetch_coinbase_candles,
    fetch_coinbase_product,
    fetch_kraken_ohlc,
    fetch_kraken_pairs,
    sample_kraken_spreads,
)
from trader.domain.types import iso_utc

GAP_COINBASE = (
    "Coinbase omits buckets without trades, so a missing bucket is either an outage or an "
    "interval without trades; research treats it as missing data (fail closed)."
)
GAP_KRAKEN = "Kraken emits rows only for committed frames; the uncommitted final row is dropped."
GAP_ALPACA = (
    "Regular-session intervals without a bar are missing data or no-trade intervals; Alpaca does "
    "not distinguish them. Extended-hours bars are excluded."
)
GAP_ALPACA_DAILY = (
    "One bar per XNYS regular session is expected; Alpaca emits no daily bar for a session "
    "without qualifying trades, so a missing session is reported and never filled. Daily open, "
    "high, low and close are regular-session prices; volume, trade count and VWAP also count "
    "pre-market and after-hours trades."
)


@dataclass(frozen=True, slots=True)
class IngestContext:
    repo_root: Path
    transport: JsonTransport
    now: datetime
    alpaca: AlpacaCredentials | None = None
    archive_dir: Path | None = None
    #: A logged-in reader of Capital.com DEMO prices; ``None`` blocks those datasets.
    capital: QuoteSource | None = None


def ingest(ds: HistoryDataset, ctx: IngestContext) -> HistoryDataset:
    if ds.source == "coinbase_exchange_public":
        return _ingest_coinbase(ds, ctx)
    if ds.source == "kraken_public_ohlc":
        return _ingest_kraken_recent(ds, ctx)
    if ds.source == "alpaca_stock_bars":
        return _ingest_alpaca(ds, ctx)
    if ds.source == "kraken_ohlcvt_archive":
        return _ingest_kraken_archive(ds, ctx)
    if ds.source == "capital_demo_prices":
        return _ingest_capital(ds, ctx)
    if ds.source == "synthetic_history":
        from trader.data.synthetic_history import write_synthetic_history

        return write_synthetic_history(ds, ctx.repo_root)
    raise ValueError(f"unsupported source {ds.source}")  # pragma: no cover


def session_calendar_of(ds: HistoryDataset) -> ExchangeCalendar | None:
    """The calendar that defines a daily dataset's bars; ``None`` for fixed intervals."""
    return ExchangeCalendar(ds.request.calendar) if ds.request.is_daily else None


def store_instrument(
    ds: HistoryDataset,
    repo_root: Path,
    request: InstrumentRequest,
    raw: Sequence[HistoryBar],
    *,
    retrieved_at: datetime,
    expected: Sequence[datetime],
    sessions: dict[str, list[datetime]] | None,
    gap_semantics: str,
) -> FileRecord:
    calendar = session_calendar_of(ds)
    if calendar is None:
        bars, report = clean_and_check(
            raw,
            minutes=ds.request.bar_minutes,
            retrieved_at=retrieved_at,
            expected=expected,
            sessions=sessions,
            gap_semantics=gap_semantics,
        )
    else:
        bars, report = clean_and_check(
            raw,
            minutes=None,
            retrieved_at=retrieved_at,
            expected=expected,
            sessions=sessions,
            gap_semantics=gap_semantics,
            finalization_delay=DAILY_FINALIZATION,
            session_calendar=calendar,
        )
    if not bars:
        raise DataQualityError(f"{ds.name}/{request.instrument_id}: no usable bars")
    rel = Path(ds.path) / safe_file_name(request.instrument_id)
    file_hash, content = write_bars(repo_root / rel, bars, session_calendar=calendar)
    return _record(request, rel, bars, file_hash, content, report)


def _record(
    request: InstrumentRequest,
    rel: Path,
    bars: Sequence[HistoryBar],
    file_hash: str,
    content: str,
    report: QualityReport,
) -> FileRecord:
    return FileRecord(
        instrument_id=request.instrument_id,
        source_symbol=request.source_symbol,
        file=rel.name,
        rows=len(bars),
        first_start=bars[0].start,
        last_start=bars[-1].start,
        file_sha256=file_hash,
        content_sha256=content,
        quality=report.as_dict(),
    )


def _acquired(
    ds: HistoryDataset, files: list[FileRecord], now: datetime, **extra: object
) -> HistoryDataset:
    update: dict[str, object] = {
        "status": "ACQUIRED",
        "blocker": "",
        "retrieved_at": now,
        "files": tuple(files),
    }
    update.update(extra)
    return ds.model_copy(update=update)


def _blocked(ds: HistoryDataset, reason: str) -> HistoryDataset:
    return ds.model_copy(update={"status": "BLOCKED", "blocker": reason})


# ----------------------------------------------------------------------------- sources


def _ingest_coinbase(ds: HistoryDataset, ctx: IngestContext) -> HistoryDataset:
    req = ds.request
    expected = expected_starts_24_7(req.start, req.end, req.bar_minutes)
    files: list[FileRecord] = []
    products: dict[str, object] = {}
    for inst in req.instruments:
        raw = fetch_coinbase_candles(
            ctx.transport, inst.source_symbol, minutes=req.bar_minutes, start=req.start, end=req.end
        )
        files.append(
            store_instrument(
                ds,
                ctx.repo_root,
                inst,
                raw,
                retrieved_at=ctx.now,
                expected=expected,
                sessions=None,
                gap_semantics=GAP_COINBASE,
            )
        )
        products[inst.source_symbol] = fetch_coinbase_product(ctx.transport, inst.source_symbol)
    meta = dict(ds.source_metadata)
    meta["products"] = products
    return _acquired(ds, files, ctx.now, source_metadata=meta)


def _ingest_kraken_recent(ds: HistoryDataset, ctx: IngestContext) -> HistoryDataset:
    req = ds.request
    oldest_available = ctx.now - timedelta(minutes=req.bar_minutes * 719)
    if req.start < oldest_available:
        return _blocked(
            ds,
            f"Kraken's recent OHLC endpoint only covers ~720 rows (from about "
            f"{iso_utc(oldest_available)}); the requested start {iso_utc(req.start)} is older. "
            "Deep Kraken history requires the downloadable OHLCVT archive.",
        )
    expected = expected_starts_24_7(req.start, req.end, req.bar_minutes)
    files: list[FileRecord] = []
    for inst in req.instruments:
        raw = fetch_kraken_ohlc(ctx.transport, inst.source_symbol, minutes=req.bar_minutes)
        files.append(
            store_instrument(
                ds,
                ctx.repo_root,
                inst,
                raw,
                retrieved_at=ctx.now,
                expected=expected,
                sessions=None,
                gap_semantics=GAP_KRAKEN,
            )
        )
    pairs = [i.source_symbol for i in req.instruments]
    meta = dict(ds.source_metadata)
    meta["asset_pairs"] = {
        key: {
            k: str(v)
            for k, v in m.items()
            if k
            in (
                "altname",
                "wsname",
                "base",
                "quote",
                "lot_decimals",
                "pair_decimals",
                "cost_decimals",
                "ordermin",
                "costmin",
                "tick_size",
                "status",
            )
        }
        for key, m in fetch_kraken_pairs(ctx.transport, pairs).items()
    }
    meta["spread_samples"] = sample_kraken_spreads(ctx.transport, pairs, samples=3, now=ctx.now)
    return _acquired(ds, files, ctx.now, source_metadata=meta)


def _ingest_alpaca(ds: HistoryDataset, ctx: IngestContext) -> HistoryDataset:
    if ctx.alpaca is None:
        return _blocked(
            ds,
            "owner action: store Alpaca paper keys with `trader secrets set alpaca-paper` (OS "
            f"keyring), or set {KEY_ENV} and {SECRET_ENV} in the local shell "
            "(docs/owner_setup.md section 5); keys are never stored in the repository",
        )
    req = ds.request
    calendar = ExchangeCalendar("XNYS")
    symbols = [i.source_symbol for i in req.instruments]
    if req.is_daily:
        if req.feed_param not in ("iex", "sip"):
            raise ValueError(f"{ds.name}: a daily Alpaca request must name its feed (iex or sip)")
        expected, sessions = expected_session_opens(calendar, req.start, req.end)
        by_symbol = fetch_alpaca_daily_bars(
            ctx.transport,
            ctx.alpaca,
            symbols,
            start=req.start,
            end=req.end,
            feed=req.feed_param,
            calendar=calendar,
        )
        gap_semantics = GAP_ALPACA_DAILY
    else:
        expected, sessions = expected_starts_sessions(calendar, req.start, req.end, req.bar_minutes)
        by_symbol = fetch_alpaca_bars(
            ctx.transport,
            ctx.alpaca,
            symbols,
            minutes=req.bar_minutes,
            start=req.start,
            end=req.end,
            feed=req.feed_param or "iex",
        )
        gap_semantics = GAP_ALPACA
    files = [
        store_instrument(
            ds,
            ctx.repo_root,
            inst,
            by_symbol.get(inst.source_symbol, []),
            retrieved_at=ctx.now,
            expected=expected,
            sessions=sessions,
            gap_semantics=gap_semantics,
        )
        for inst in req.instruments
    ]
    splits, changes = fetch_alpaca_corporate_actions(
        ctx.transport,
        ctx.alpaca,
        symbols,
        start=req.start.date(),
        end=req.end.date(),
        instrument_ids={i.source_symbol: i.instrument_id for i in req.instruments},
    )
    actions = tuple(
        [
            {
                "type": "SPLIT",
                "instrument_id": s.instrument_id,
                "ex_date": s.ex_date.isoformat(),
                "new_rate": str(s.new_rate),
                "old_rate": str(s.old_rate),
                "source": s.source,
            }
            for s in splits
        ]
        + [
            {
                "type": "SYMBOL_CHANGE",
                "instrument_id": c.instrument_id,
                "effective_date": c.effective_date.isoformat(),
                "old_symbol": c.old_symbol,
                "new_symbol": c.new_symbol,
                "source": c.source,
            }
            for c in changes
        ]
    )
    return _acquired(ds, files, ctx.now, corporate_actions=actions)


def _ingest_kraken_archive(ds: HistoryDataset, ctx: IngestContext) -> HistoryDataset:
    req = ds.request
    archive = ctx.archive_dir or (ctx.repo_root / "data" / "raw" / "kraken_ohlcvt")
    expected = expected_starts_24_7(req.start, req.end, req.bar_minutes)
    files: list[FileRecord] = []
    for inst in req.instruments:
        path = archive / f"{inst.source_symbol}_{req.bar_minutes}.csv"
        if not path.exists():
            return _blocked(
                ds,
                f"owner action: download Kraken's OHLCVT archive and place {path.name} in "
                f"{archive.relative_to(ctx.repo_root).as_posix() if archive.is_relative_to(ctx.repo_root) else archive}"
                " (docs/owner_setup.md section 5)",
            )
        raw = list(_read_kraken_archive(path, req.bar_minutes))
        record = store_instrument(
            ds,
            ctx.repo_root,
            inst,
            raw,
            retrieved_at=ctx.now,
            expected=expected,
            sessions=None,
            gap_semantics="Kraken archive rows exist only for intervals with trades.",
        )
        files.append(
            record.model_copy(
                update={"quality": {**record.quality, "archive_sha256": file_sha256(path)}}
            )
        )
    return _acquired(ds, files, ctx.now)


def _read_kraken_archive(path: Path, minutes: int) -> list[HistoryBar]:
    out: list[HistoryBar] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].strip().isdigit():
                continue
            ts, o, h, low, c, v, n = row[:7]
            out.append(
                HistoryBar(
                    start=datetime.fromtimestamp(int(ts), tz=UTC),
                    minutes=minutes,
                    open=Decimal(o),
                    high=Decimal(h),
                    low=Decimal(low),
                    close=Decimal(c),
                    volume=Decimal(v),
                    trade_count=int(n),
                )
            )
    return out


# ----------------------------------------------------------------------------- reading


def verify_dataset(ds: HistoryDataset, repo_root: Path) -> list[str]:
    problems: list[str] = []
    if ds.status != "ACQUIRED":
        return [f"{ds.name}: status {ds.status} {ds.blocker}".strip()]
    for record in ds.files:
        path = repo_root / ds.path / record.file
        if not path.exists():
            problems.append(
                f"{ds.name}/{record.file}: file missing (run `trader ingest-history` to restore it)"
            )
            continue
        bars = _read_dataset_file(ds, path)
        if content_sha256(bars) != record.content_sha256:
            problems.append(f"{ds.name}/{record.file}: content checksum mismatch")
        if len(bars) != record.rows:
            problems.append(f"{ds.name}/{record.file}: row count mismatch")
    return problems


# ----------------------------------------------------------------------------- Capital.com
GAP_CAPITAL = (
    "The bars that exist are the record of when the market was open: a closed market (nights, "
    "weekends, holidays) and an hour without a quote both leave no bar, and the source does not "
    "distinguish them. No expected grid is built, so coverage is reported as 0 by construction."
)
BID_SUFFIX, ASK_SUFFIX = ".BID", ".ASK"
MAX_QUALITY_EXAMPLES = 20


def _ingest_capital(ds: HistoryDataset, ctx: IngestContext) -> HistoryDataset:
    """Two-sided Capital.com DEMO bars (ADR-0052): each epic's bid and ask are stored as two
    ordinary history files, ``<id>.BID`` and ``<id>.ASK``, with exactly the same bar starts."""
    if ctx.capital is None:
        return _blocked(
            ds,
            "owner action: the Capital.com DEMO keys (CAP_API_KEY, CAP_IDENTIFIER, "
            "CAP_API_PASSWORD in the repository .env, or `trader secrets set capital-demo`); "
            "research ingest reads demo prices only",
        )
    req = ds.request
    by_epic: dict[str, dict[str, InstrumentRequest]] = {}
    for inst in req.instruments:
        side = inst.instrument_id[-len(BID_SUFFIX) :]
        if side not in (BID_SUFFIX, ASK_SUFFIX):
            raise ValueError(f"{ds.name}: {inst.instrument_id} must end in .BID or .ASK")
        by_epic.setdefault(inst.source_symbol, {})[side] = inst
    files: list[FileRecord] = []
    for epic, sides in by_epic.items():
        if set(sides) != {BID_SUFFIX, ASK_SUFFIX}:
            raise ValueError(f"{ds.name}: {epic} needs one .BID and one .ASK instrument")
        history = fetch_capital_quote_history(
            ctx.capital, epic, minutes=req.bar_minutes, start=req.start, end=req.end
        )
        pairs = [
            (b, a)
            for b, a in zip(history.bid, history.ask, strict=True)
            if not b.ohlc_problems() and not a.ohlc_problems()
        ]
        quotes = _quote_quality(history, pairs, calendar=req.calendar)
        for side, column in ((BID_SUFFIX, 0), (ASK_SUFFIX, 1)):
            record = store_instrument(
                ds,
                ctx.repo_root,
                sides[side],
                [p[column] for p in pairs],
                retrieved_at=ctx.now,
                expected=[],
                sessions=None,
                gap_semantics=GAP_CAPITAL,
            )
            files.append(
                record.model_copy(update={"quality": {**record.quality, "quotes": quotes}})
            )
        by_start = {f.instrument_id: (f.rows, f.first_start, f.last_start) for f in files[-2:]}
        if len(set(by_start.values())) != 1:
            raise DataQualityError(f"{ds.name}/{epic}: bid and ask files differ: {by_start}")
    return _acquired(ds, files, ctx.now)


def _quote_quality(
    history: QuoteHistory,
    pairs: Sequence[tuple[HistoryBar, HistoryBar]],
    *,
    calendar: str,
) -> dict[str, object]:
    """What a study must know about the quotes before it registers: how the walk ended, the
    spread by year, how many rows carry no spread at all, and the days without a bar."""
    by_year: dict[int, list[float]] = {}
    flat: dict[int, int] = {}
    for bid, ask in pairs:
        mid = (bid.close + ask.close) / 2
        by_year.setdefault(bid.start.year, []).append(float((ask.close - bid.close) / mid * 10000))
        flat[bid.start.year] = flat.get(bid.start.year, 0) + (ask.close == bid.close)
    spread = {
        str(year): {
            "rows": len(values),
            "median_bps": round(statistics.median(values), 3),
            "p95_bps": round(sorted(values)[min(len(values) - 1, int(len(values) * 0.95))], 3),
            "rows_bid_equals_ask": flat[year],
        }
        for year, values in sorted(by_year.items())
    }
    days = {b.start.date() for b, _ in pairs}
    missing_days: list[str] = []
    if days:
        day, last = min(days), max(days)
        while day <= last:
            open_day = calendar == "UTC_24_7" or day.weekday() < 5
            if open_day and day not in days:
                missing_days.append(day.isoformat())
            day += timedelta(days=1)
    weekdays = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    per_weekday = dict.fromkeys(weekdays, 0)
    for bid, _ in pairs:
        per_weekday[weekdays[bid.start.weekday()]] += 1
    return {
        "walk": history.report(),
        "rows_excluded_invalid_ohlc": len(history.bid) - len(pairs),
        "spread_by_year": spread,
        "bars_per_weekday": per_weekday,
        "days_without_a_bar": len(missing_days),
        "days_without_a_bar_first": missing_days[:MAX_QUALITY_EXAMPLES],
        "volume": "the source's tick count per bar, not traded volume",
    }


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    status: Literal["OK", "RESTORED", "NOT_RESTORABLE", "NOT_ACQUIRED", "MISMATCH", "FAILED"]
    message: str

    @property
    def failed(self) -> bool:
        return self.status in ("MISMATCH", "FAILED")


def verify_or_restore(ds: HistoryDataset, ctx: IngestContext) -> RestoreOutcome:
    """Verify a dataset's files; on a fresh checkout, restore them bit for bit.

    Data files are never in git. When files are merely missing, the dataset is re-acquired
    into place and must reproduce exactly the recorded content checksums; the manifest record is
    never overwritten, so results registered against the data stay bound to it. A mismatch is a
    failure: research results bound to the dataset could no longer be reproduced.
    """
    if ds.status != "ACQUIRED":
        return RestoreOutcome("NOT_ACQUIRED", f"{ds.status} {ds.blocker}".strip())
    problems = verify_dataset(ds, ctx.repo_root)
    if not problems:
        return RestoreOutcome("OK", "checksums ok")
    missing = [p for p in problems if "file missing" in p]
    if len(missing) != len(problems):
        return RestoreOutcome("FAILED", "; ".join(problems))
    if not ds.restorable:
        return RestoreOutcome(
            "NOT_RESTORABLE",
            f"one-time capture the source no longer serves; copy {ds.path}/ from a teammate if "
            "needed (only optional checks such as the venue basis use it)",
        )
    restored = ingest(ds, ctx)
    recorded = {f.instrument_id: f.content_sha256 for f in ds.files}
    got = {f.instrument_id: f.content_sha256 for f in restored.files}
    if restored.status != "ACQUIRED" or got != recorded:
        return RestoreOutcome(
            "MISMATCH",
            "could NOT be restored to the recorded checksums "
            f"({restored.blocker or 'the source now returns different data'}); results bound to "
            "this dataset cannot be reproduced. Investigate before acquiring a new version with "
            "--refresh.",
        )
    return RestoreOutcome("RESTORED", "restored; content identical to the recorded checksums")


def load_bars(
    ds: HistoryDataset,
    instrument_id: str,
    repo_root: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[HistoryBar]:
    """Load one instrument, verifying the content checksum before any use."""
    if ds.status != "ACQUIRED":
        raise DataQualityError(f"{ds.name} is {ds.status}: {ds.blocker}")
    record = ds.file_for(instrument_id)
    bars = _read_dataset_file(ds, repo_root / ds.path / record.file)
    if content_sha256(bars) != record.content_sha256:
        raise DataQualityError(f"{ds.name}/{record.file}: content checksum mismatch")
    return [
        b for b in bars if (start is None or b.start >= start) and (end is None or b.start < end)
    ]


def _read_dataset_file(ds: HistoryDataset, path: Path) -> list[HistoryBar]:
    calendar = session_calendar_of(ds)
    if calendar is None:
        return read_bars(path, minutes=ds.request.bar_minutes)
    return read_bars(path, minutes=None, session_calendar=calendar)


def split_events(ds: HistoryDataset) -> list[SplitEvent]:
    return [
        SplitEvent(
            instrument_id=a["instrument_id"],
            ex_date=date.fromisoformat(a["ex_date"]),
            new_rate=Decimal(a["new_rate"]),
            old_rate=Decimal(a["old_rate"]),
            source=a.get("source", ""),
        )
        for a in ds.corporate_actions
        if a.get("type") == "SPLIT"
    ]
