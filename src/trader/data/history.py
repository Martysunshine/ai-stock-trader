"""Historical bar datasets for research (instructions.md §3: DuckDB and Parquet).

A dataset is a directory holding one Parquet file per instrument; its provenance, checksums and
quality findings live in ``research/data_manifest.json`` (roadmap §2). Market datasets stay out
of git (``data/`` is ignored); only the manifest is committed.

* Prices, volumes and VWAPs are exact decimals (``DECIMAL(38,12)``). Values are quantised to 12
  decimal places when parsed, which is exact for every supported source.
* Timestamps are bar *start* times in epoch milliseconds, UTC. A bar ``[start, end)`` becomes
  usable only at ``end + finalization_delay`` (its availability time), never earlier.
* Two hashes are recorded: the SHA-256 of the Parquet file and a *content* SHA-256 over a
  canonical text rendering of the rows. The content hash is independent of the Parquet writer
  version and is what experiments bind to.
* Sources that do not supply a VWAP or trade count leave them ``None``. Nothing is fabricated:
  a strategy that needs a source VWAP refuses such a dataset (see ``require_source_vwap``).
* Daily (session) bars are stored the same way, keyed by their session open; their length is
  not stored but taken from the exchange calendar on read (``session_calendar``), and a stored
  start that is not a session open fails the read (``data/daily_bars.py``).
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from trader.domain.market import Bar
from trader.domain.types import DomainError, canon_decimal, iso_utc, require_utc

if TYPE_CHECKING:
    from trader.data.calendars import ExchangeCalendar

STORAGE_QUANTUM = Decimal("0.000000000001")  # 12 decimal places
_COLUMNS = "start_ms BIGINT, open DECIMAL(38,12), high DECIMAL(38,12), low DECIMAL(38,12), close DECIMAL(38,12), volume DECIMAL(38,12), vwap DECIMAL(38,12), trade_count BIGINT"


class DataQualityError(DomainError):
    """A dataset failed an integrity check; it must not be used for research."""


def to_storage_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise DataQualityError("non-finite value in market data")
    quantised = value.quantize(STORAGE_QUANTUM)
    if quantised != value:
        raise DataQualityError(f"value {value} has more than 12 decimal places")
    return quantised


def epoch_ms(ts: datetime) -> int:
    ts = require_utc(ts)
    return int(ts.timestamp()) * 1000 + ts.microsecond // 1000


def from_epoch_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=UTC) + timedelta(milliseconds=ms % 1000)


@dataclass(frozen=True, slots=True)
class HistoryBar:
    """One historical bar exactly as supplied by its source (no derived fields)."""

    start: datetime
    minutes: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None = None
    trade_count: int | None = None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / 3

    def ohlc_problems(self) -> list[str]:
        problems: list[str] = []
        if min(self.open, self.high, self.low, self.close) <= 0:
            problems.append("non-positive price")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            problems.append("inconsistent OHLC")
        if self.volume < 0:
            problems.append("negative volume")
        if self.vwap is not None and not (self.low <= self.vwap <= self.high):
            problems.append("vwap outside the bar range")
        return problems

    def canonical_row(self) -> str:
        parts = [
            str(epoch_ms(self.start)),
            canon_decimal(self.open),
            canon_decimal(self.high),
            canon_decimal(self.low),
            canon_decimal(self.close),
            canon_decimal(self.volume),
            "" if self.vwap is None else canon_decimal(self.vwap),
            "" if self.trade_count is None else str(self.trade_count),
        ]
        return "|".join(parts)

    def scaled(self, price_divisor: Decimal) -> HistoryBar:
        """Point-in-time split adjustment: prices divided, volume multiplied."""
        return HistoryBar(
            start=self.start,
            minutes=self.minutes,
            open=self.open / price_divisor,
            high=self.high / price_divisor,
            low=self.low / price_divisor,
            close=self.close / price_divisor,
            volume=self.volume * price_divisor,
            vwap=None if self.vwap is None else self.vwap / price_divisor,
            trade_count=self.trade_count,
        )


def content_sha256(bars: Sequence[HistoryBar]) -> str:
    h = hashlib.sha256()
    for bar in bars:
        h.update(bar.canonical_row().encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_bars(
    path: Path,
    bars: Sequence[HistoryBar],
    *,
    session_calendar: ExchangeCalendar | None = None,
) -> tuple[str, str]:
    """Write bars (strictly increasing starts) to Parquet; return (file_sha256, content_sha256).

    With ``session_calendar`` every bar must be exactly one regular session of that calendar
    (daily bars, whose lengths differ on early closes); otherwise all bars share one interval.
    """
    _require_sorted(bars, session_calendar)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "bars.csv"
        with staging.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            for bar in bars:
                writer.writerow(
                    [
                        epoch_ms(bar.start),
                        canon_decimal(to_storage_decimal(bar.open)),
                        canon_decimal(to_storage_decimal(bar.high)),
                        canon_decimal(to_storage_decimal(bar.low)),
                        canon_decimal(to_storage_decimal(bar.close)),
                        canon_decimal(to_storage_decimal(bar.volume)),
                        "" if bar.vwap is None else canon_decimal(to_storage_decimal(bar.vwap)),
                        "" if bar.trade_count is None else bar.trade_count,
                    ]
                )
        import duckdb  # lazy: the light market feed imports this module without DuckDB

        con = duckdb.connect()
        try:
            con.execute(f"CREATE TABLE bars ({_COLUMNS})")
            con.execute(
                "COPY bars FROM ? (FORMAT CSV, HEADER false, NULLSTR '')",
                [staging.as_posix()],
            )
            target = path.as_posix().replace("'", "''")
            con.execute(
                f"COPY (SELECT * FROM bars ORDER BY start_ms) TO '{target}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            con.close()
    content = content_sha256(bars)
    readback = (
        read_bars(path, minutes=None, session_calendar=session_calendar)
        if session_calendar is not None
        else read_bars(path, minutes=bars[0].minutes if bars else 1)
    )
    if content_sha256(readback) != content:
        raise DataQualityError(f"read-back of {path.name} does not reproduce the written content")
    return file_sha256(path), content


def read_bars(
    path: Path,
    *,
    minutes: int | None,
    start: datetime | None = None,
    end: datetime | None = None,
    session_calendar: ExchangeCalendar | None = None,
) -> list[HistoryBar]:
    """Read bars with ``start <= bar.start < end`` (both optional), in time order.

    Fixed-interval bars take ``minutes``. Daily bars take ``session_calendar`` instead: each
    bar's length is its session's, and a stored start that is no session open is refused.
    """
    if (minutes is None) == (session_calendar is None):
        raise ValueError("pass `minutes` for fixed-interval bars or `session_calendar` for daily")
    if not path.exists():
        raise DataQualityError(f"dataset file missing: {path}")
    clauses: list[str] = []
    params: list[int] = []
    if start is not None:
        clauses.append("start_ms >= ?")
        params.append(epoch_ms(start))
    if end is not None:
        clauses.append("start_ms < ?")
        params.append(epoch_ms(end))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    import duckdb  # lazy, as in write_bars

    con = duckdb.connect()
    try:
        source = path.as_posix().replace("'", "''")
        rows = con.execute(
            "SELECT start_ms, open, high, low, close, volume, vwap, trade_count "
            f"FROM read_parquet('{source}') {where} ORDER BY start_ms",
            params,
        ).fetchall()
    finally:
        con.close()
    out: list[HistoryBar] = []
    for r in rows:
        bar_start = from_epoch_ms(int(r[0]))
        out.append(
            HistoryBar(
                start=bar_start,
                minutes=(
                    minutes
                    if minutes is not None
                    else _session_minutes(bar_start, session_calendar)
                ),
                open=_dec(r[1]),
                high=_dec(r[2]),
                low=_dec(r[3]),
                close=_dec(r[4]),
                volume=_dec(r[5]),
                vwap=None if r[6] is None else _dec(r[6]),
                trade_count=None if r[7] is None else int(r[7]),
            )
        )
    return out


def _dec(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise DataQualityError(f"expected an exact decimal from storage, got {type(value)}")
    return value.normalize()


def _session_minutes(start: datetime, session_calendar: ExchangeCalendar | None) -> int:
    if session_calendar is None:
        raise ValueError("a daily bar needs a session calendar")
    session = session_calendar.session_opening_at(start)
    if session is None:
        raise DataQualityError(f"stored daily bar at {iso_utc(start)} is not a session open")
    return session.minutes


def _require_sorted(
    bars: Sequence[HistoryBar], session_calendar: ExchangeCalendar | None = None
) -> None:
    for prev, cur in itertools.pairwise(bars):
        if cur.start <= prev.start:
            raise DataQualityError(f"bars not strictly increasing at {cur.start.isoformat()}")
        if session_calendar is None and cur.minutes != prev.minutes:
            raise DataQualityError("mixed bar intervals in one file")
    if session_calendar is not None:
        for bar in bars:
            session = session_calendar.session_opening_at(bar.start)
            if session is None or session.minutes != bar.minutes:
                raise DataQualityError(
                    f"{iso_utc(bar.start)} +{bar.minutes} min is not one regular session"
                )


def to_domain_bars(
    bars: Iterable[HistoryBar],
    *,
    instrument_id: str,
    feed: str,
    finalization_delay: timedelta,
    require_source_vwap: bool,
    adjustment_basis: Literal["RAW", "SPLIT_ADJUSTED"] = "RAW",
) -> list[Bar]:
    """Convert stored bars to domain bars with explicit availability times.

    When the source supplies no VWAP, the domain bar carries the bar's typical price in its
    ``vwap`` slot *only* for strategies that declared they do not use a source VWAP; a strategy
    that requires one gets a ``DataQualityError`` instead of a silently substituted value.
    """
    out: list[Bar] = []
    for hb in bars:
        if hb.vwap is None and require_source_vwap:
            raise DataQualityError(
                f"{instrument_id}: the source supplies no VWAP but the strategy requires it"
            )
        vwap = hb.vwap if hb.vwap is not None else hb.typical_price
        vwap = min(max(vwap, hb.low), hb.high)
        out.append(
            Bar.model_construct(
                instrument_id=instrument_id,
                feed=feed,
                minutes=hb.minutes,
                start=hb.start,
                end=hb.end,
                available_time=hb.end + finalization_delay,
                complete=True,
                open=hb.open,
                high=hb.high,
                low=hb.low,
                close=hb.close,
                volume=hb.volume,
                trade_count=hb.trade_count or 0,
                vwap=vwap,
                adjustment_basis=adjustment_basis,
                schema_version=1,
            )
        )
    return out


def safe_file_name(instrument_id: str) -> str:
    return instrument_id.replace(":", "__").replace("/", "_") + ".parquet"
