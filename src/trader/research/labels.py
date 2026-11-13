"""Labelled candidate dataset for the M3 candidate filter (docs/ml_research_guide.md §4).

:func:`build_labels` runs a frozen rule (a research rule or the runtime Candidate A through its
adapter) over historical bars and keeps **every** candidate whose decision time lies in the
labelled window, together with the feature snapshot at its decision time:

1. **Candidates and features.** The rule is driven by the research engine's own timeline (bars
   in availability order, splits applied first), so it emits exactly the candidates a research
   run sees. Right after each candidate the rule's snapshot is read through its ``features``
   accessor and checked against ``candidate.snapshot_hash``
   (``content_hash({"version": snapshot_version, "features": snapshot})``, the formula both the
   streaming rules and Candidate A use), then turned into ``candidate-v1`` features. Nothing
   later than the decision time enters a feature.
2. **Labels.** Each candidate is simulated **alone** (:data:`LABEL_RULE`): a single-candidate
   strategy emits it at its decision bar into a :class:`~trader.research.engine.ResearchEngine`
   with a fresh USD 50 ledger and the study's policy, fee schedule and fill assumptions, so one
   trade's outcome never depends on another's portfolio limits. The bars after the decision are
   only the label's *outcome*; ``exit_time`` records when it was known, for purging.
3. **Kept as is.** A candidate rejected even alone (stale window, too costly, below a minimum)
   or never filled keeps its status with ``net_usd = 0``.

The labelled window and every bar supplied are checked against the locked final test period
(``splits.guard_window``); the holdout is refused unless the registry records an unlock.

A :class:`LabelSet` is stored as one Parquet file written through DuckDB: money as
``DECIMAL(38,12)``, features and returns as ``DOUBLE``, times as epoch microseconds, and the
feature names, feature version and provenance in the file's key-value metadata. Writing reads the
file back and refuses anything that does not round-trip exactly. Label files belong under
``data/`` (gitignored).
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import tempfile
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Protocol

import duckdb
import numpy as np

from trader.domain.candidates import TradeCandidate
from trader.domain.hashing import canonical_json, content_hash
from trader.domain.market import Bar
from trader.domain.types import ZERO, canon_decimal, iso_utc
from trader.features.candidate_features import (
    FAMILIES,
    FEATURE_NAMES,
    FEATURE_VERSION,
    FeatureVersionError,
    compute,
    verify_snapshot,
)
from trader.research.engine import (
    CandidateOutcome,
    EngineConfig,
    InstrumentSetup,
    ResearchEngine,
    ResearchStrategy,
    Trade,
)
from trader.research.filters import SnapshotSource
from trader.research.splits import SplitPlan, guard_window

LABEL_CAPITAL = Decimal(50)
NET_QUANTUM = Decimal("0.000000000001")  # 12 dp, the DECIMAL(38,12) storage scale
STATUSES = ("FILLED", "PARTIAL", "UNFILLED", "REJECTED")
FILE_FORMAT = "candidate-labels-v1"
#: Bars simulated after the first bar at or beyond the latest possible time exit, so that an exit
#: continuing over several bars (participation cap) still completes inside the slice. A label
#: whose position is still open at the slice end is recomputed over all remaining bars.
EXIT_SLACK_BARS = 24

LABEL_RULE = (
    "each candidate simulated alone through ResearchEngine from a fresh USD 50 ledger with the "
    "study policy, fee schedule and fill assumptions: marketable limit at the estimated ask plus "
    "the frozen offset, filled at the next bar open plus half-spread and slippage only within the "
    "limit and the TTL; stop before target inside a bar; time, session-close and stop-gap exits "
    "at the open; fees on both legs; a position open at the end of the supplied bars is closed at "
    "the last bar's estimated bid (END_OF_DATA). net_usd = net PnL rounded half-even to 12 dp "
    "(0 when not filled); net_return = net_usd / entry notional; win = net_usd > 0"
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_US = timedelta(microseconds=1)
_META_KEY = "trader_candidate_labels"


class LabelError(ValueError):
    """A label set is inconsistent, or a file does not round-trip (fail closed)."""


def _require_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise LabelError(f"{name} must be a timezone-aware UTC datetime")


# ----------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class LabelRow:
    candidate_id: str
    family: str  # "CANDIDATE_A" | "CANDIDATE_B" (rule family)
    instrument_id: str
    decision_time: datetime  # tz-aware UTC
    exit_time: datetime | None  # None when never filled; used for purging
    status: str  # FILLED | PARTIAL | UNFILLED | REJECTED
    net_usd: Decimal  # Decimal("0") when not filled
    net_return: float  # net_usd / entry notional; 0.0 when not filled
    win: bool  # net_usd > 0
    features: Mapping[str, float]  # keys == LabelSet.feature_names, all finite

    def __post_init__(self) -> None:
        if not (self.candidate_id and self.instrument_id):
            raise LabelError("a label row needs a candidate ID and an instrument")
        if self.family not in FAMILIES:
            raise LabelError(f"unknown rule family {self.family!r}")
        _require_utc("decision_time", self.decision_time)
        if self.status not in STATUSES:
            raise LabelError(f"unknown label status {self.status!r}")
        if not isinstance(self.net_usd, Decimal) or not self.net_usd.is_finite():
            raise LabelError("net_usd must be a finite Decimal")
        if not isinstance(self.net_return, float) or not math.isfinite(self.net_return):
            raise LabelError("net_return must be a finite float")
        if self.win != (self.net_usd > 0):
            raise LabelError("win must equal net_usd > 0")
        filled = self.status in ("FILLED", "PARTIAL")
        if filled:
            if self.exit_time is None:
                raise LabelError("a filled label records its exit time")
            _require_utc("exit_time", self.exit_time)
            if self.exit_time < self.decision_time:
                raise LabelError("a label cannot exit before its decision")
        elif self.exit_time is not None or self.net_usd != 0 or self.net_return != 0.0:
            raise LabelError("an unfilled or rejected label has no exit and zero net")
        for name, value in self.features.items():
            if not isinstance(value, float) or not math.isfinite(value):
                raise LabelError(f"feature {name} of {self.candidate_id} is not a finite float")


@dataclass(frozen=True, slots=True)
class LabelSet:
    rows: tuple[LabelRow, ...]  # sorted by (decision_time, candidate_id)
    feature_names: tuple[str, ...]
    feature_version: str
    source: Mapping[str, str]  # dataset name, content hashes, spec hash, label rule

    def __post_init__(self) -> None:
        names = set(self.feature_names)
        if len(names) != len(self.feature_names):
            raise LabelError("feature names must be unique")
        for name, value in self.source.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise LabelError("label provenance is a mapping of strings")
        previous: tuple[datetime, str] | None = None
        for row in self.rows:
            order = (row.decision_time, row.candidate_id)
            if previous is not None and order <= previous:
                raise LabelError("label rows must be strictly ordered by (decision time, ID)")
            previous = order
            if len(row.features) != len(names) or set(row.features) != names:
                raise LabelError(f"features of {row.candidate_id} do not match feature_names")

    def between(self, start: datetime, end: datetime) -> LabelSet:
        """Rows whose decision time lies in ``[start, end)``."""
        return LabelSet(
            rows=tuple(r for r in self.rows if start <= r.decision_time < end),
            feature_names=self.feature_names,
            feature_version=self.feature_version,
            source=self.source,
        )

    def matrix(self) -> tuple[np.ndarray, np.ndarray]:
        """``X`` (float64, n x k, columns = feature_names) and ``y`` (int64, 1 = win)."""
        x = np.empty((len(self.rows), len(self.feature_names)), dtype=np.float64)
        for i, row in enumerate(self.rows):
            x[i] = [row.features[name] for name in self.feature_names]
        y = np.fromiter(
            (1 if r.win else 0 for r in self.rows), dtype=np.int64, count=len(self.rows)
        )
        return x, y

    # ------------------------------------------------------------------ storage
    def _metadata(self) -> str:
        return json.dumps(
            {
                "format": FILE_FORMAT,
                "feature_names": list(self.feature_names),
                "feature_version": self.feature_version,
                "source": dict(self.source),
            },
            sort_keys=True,
            ensure_ascii=True,
        )

    def save(self, path: Path) -> None:
        """Write Parquet through DuckDB, then read it back and refuse any difference."""
        for name in self.feature_names:
            if not name.isidentifier():
                raise LabelError(f"feature name {name!r} is not a plain identifier")
        columns = ", ".join(
            [
                "row_index BIGINT",
                "candidate_id VARCHAR",
                "family VARCHAR",
                "instrument_id VARCHAR",
                "decision_time_us BIGINT",
                "exit_time_us BIGINT",
                "status VARCHAR",
                "net_usd DECIMAL(38,12)",
                "net_return DOUBLE",
                "win BOOLEAN",
                *(f'"feat_{name}" DOUBLE' for name in self.feature_names),
            ]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "labels.csv"
            with staging.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh, lineterminator="\n")
                for index, row in enumerate(self.rows):
                    net = row.net_usd.quantize(NET_QUANTUM)
                    if net != row.net_usd:
                        raise LabelError(f"net_usd of {row.candidate_id} has more than 12 dp")
                    writer.writerow(
                        [
                            index,
                            row.candidate_id,
                            row.family,
                            row.instrument_id,
                            _to_us(row.decision_time),
                            "" if row.exit_time is None else _to_us(row.exit_time),
                            row.status,
                            canon_decimal(net),
                            repr(row.net_return),
                            "true" if row.win else "false",
                            *(repr(row.features[name]) for name in self.feature_names),
                        ]
                    )
            con = duckdb.connect()
            try:
                con.execute(f"CREATE TABLE labels ({columns})")
                if self.rows:  # the CSV sniffer refuses an empty file
                    con.execute(
                        "COPY labels FROM ? (FORMAT CSV, HEADER false, NULLSTR '', DELIM ',', "
                        "QUOTE '\"', ESCAPE '\"')",
                        [staging.as_posix()],
                    )
                target = path.as_posix().replace("'", "''")
                meta = self._metadata().replace("'", "''")
                con.execute(
                    f"COPY (SELECT * FROM labels ORDER BY row_index) TO '{target}' "
                    f"(FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{{_META_KEY}: '{meta}'}})"
                )
            finally:
                con.close()
        if LabelSet.load(path, expected_version=None) != self:
            raise LabelError(f"read-back of {path.name} does not reproduce the label set")

    @classmethod
    def load(cls, path: Path, *, expected_version: str | None = FEATURE_VERSION) -> LabelSet:
        """Read a label file; a different feature version is refused unless ``None`` is passed."""
        if not path.exists():
            raise FileNotFoundError(f"label file missing: {path}")
        source = path.as_posix().replace("'", "''")
        con = duckdb.connect()
        try:
            kv = con.execute(f"SELECT key, value FROM parquet_kv_metadata('{source}')").fetchall()
            meta_raw = {bytes(k).decode("utf-8"): bytes(v).decode("utf-8") for k, v in kv}
            if _META_KEY not in meta_raw:
                raise LabelError(f"{path.name} is not a candidate label file")
            meta = json.loads(meta_raw[_META_KEY])
            if meta.get("format") != FILE_FORMAT:
                raise LabelError(f"{path.name} has label format {meta.get('format')!r}")
            names = tuple(str(n) for n in meta["feature_names"])
            feature_cols = "".join(f', "feat_{name}"' for name in names)
            records = con.execute(
                "SELECT candidate_id, family, instrument_id, decision_time_us, exit_time_us, "
                f"status, net_usd, net_return, win{feature_cols} "
                f"FROM read_parquet('{source}') ORDER BY row_index"
            ).fetchall()
        finally:
            con.close()
        version = str(meta["feature_version"])
        if expected_version is not None and version != expected_version:
            raise FeatureVersionError(
                f"{path.name} holds features {version!r}, not {expected_version}"
            )
        rows = []
        for r in records:
            net = r[6]
            if not isinstance(net, Decimal):
                raise LabelError(f"expected an exact decimal from storage, got {type(net)}")
            rows.append(
                LabelRow(
                    candidate_id=str(r[0]),
                    family=str(r[1]),
                    instrument_id=str(r[2]),
                    decision_time=_from_us(int(r[3])),
                    exit_time=None if r[4] is None else _from_us(int(r[4])),
                    status=str(r[5]),
                    net_usd=net.normalize(),
                    net_return=float(r[7]),
                    win=bool(r[8]),
                    features={name: float(v) for name, v in zip(names, r[9:], strict=True)},
                )
            )
        return cls(
            rows=tuple(rows),
            feature_names=names,
            feature_version=version,
            source={str(k): str(v) for k, v in meta["source"].items()},
        )


def _to_us(ts: datetime) -> int:
    return (ts - _EPOCH) // _ONE_US


def _from_us(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=us)


# ----------------------------------------------------------------------------- building


class LabelStrategy(ResearchStrategy, SnapshotSource, Protocol):
    """A frozen rule that exposes the snapshot behind its most recent candidate."""


@dataclass(frozen=True, slots=True)
class _Seen:
    candidate: TradeCandidate
    snapshot: dict[str, Decimal]
    data_ok: bool


class _Recorder:
    """Drives the rule on the engine's timeline and records every in-window candidate.

    It returns no candidate to the engine: this pass only generates candidates and snapshots.
    ``data_ok`` is read at the same point the engine's admission would read it.
    """

    def __init__(self, inner: LabelStrategy, start: datetime, end: datetime) -> None:
        self.inner = inner
        self.start = start
        self.end = end
        self.seen: list[_Seen] = []

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def spec_hash(self) -> str:
        return self.inner.spec_hash

    @property
    def uses_source_vwap(self) -> bool:
        return self.inner.uses_source_vwap

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        candidate = self.inner.on_bar(bar, now)
        if candidate is not None and self.start <= now < self.end:
            iid = candidate.instrument_id
            snapshot = dict(self.inner.features(iid))
            verify_snapshot(snapshot, candidate, self.inner.snapshot_version)
            self.seen.append(_Seen(candidate, snapshot, self.inner.data_ok(iid)))
        return None

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        self.inner.on_split(instrument_id, divisor)

    def data_ok(self, instrument_id: str) -> bool:
        return self.inner.data_ok(instrument_id)


class _Alone:
    """Emits one recorded candidate at its decision bar and nothing else."""

    uses_source_vwap = False

    def __init__(self, strategy_id: str, spec_hash: str, seen: _Seen) -> None:
        self.strategy_id = strategy_id
        self.spec_hash = spec_hash
        self.seen = seen

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        c = self.seen.candidate
        if bar.instrument_id == c.instrument_id and bar.end == c.bar_end:
            if now != c.decision_time:
                raise LabelError(f"decision bar of {c.candidate_id} is not at its decision time")
            return c
        return None

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        del instrument_id, divisor

    def data_ok(self, instrument_id: str) -> bool:
        del instrument_id
        return self.seen.data_ok


@dataclass(frozen=True, slots=True)
class _Series:
    setup: InstrumentSetup
    starts: list[datetime]
    ends: list[datetime]


def _simulate(
    seen: _Seen, config: EngineConfig, series: _Series, strategy_id: str, spec_hash: str
) -> tuple[CandidateOutcome, Trade | None]:
    c = seen.candidate
    bars = series.setup.bars
    first = bisect_left(series.ends, c.bar_end)
    if first >= len(bars) or bars[first].end != c.bar_end:
        raise LabelError(f"decision bar of {c.candidate_id} is not in the supplied bars")
    latest_exit = c.decision_time + max(
        timedelta(seconds=c.entry_ttl_seconds),
        timedelta(milliseconds=config.fills.latency_ms, minutes=c.max_hold_minutes),
    )
    stop = min(len(bars), bisect_left(series.starts, latest_exit) + 1 + EXIT_SLACK_BARS)
    alone = replace(
        config, start=c.decision_time, end=c.decision_time + _ONE_US, initial_capital=LABEL_CAPITAL
    )
    while True:
        setup = InstrumentSetup(
            spec=series.setup.spec, bars=bars[first:stop], splits=series.setup.splits
        )
        result = ResearchEngine(
            alone, _Alone(strategy_id, spec_hash, seen), {c.instrument_id: setup}
        ).run()
        if len(result.candidates) != 1 or result.candidates[0].candidate_id != c.candidate_id:
            raise LabelError(f"the label run of {c.candidate_id} did not see exactly it")
        outcome = result.candidates[0]
        trade = result.trades[0] if result.trades else None
        cut_short = (trade is not None and trade.exits[-1].reason == "END_OF_DATA") or (
            outcome.reasons == ("END_OF_DATA",)
        )
        if not cut_short or stop == len(bars):
            return outcome, trade
        stop = len(bars)  # still open at the slice end: use every remaining bar


def _label(seen: _Seen, family: str, outcome: CandidateOutcome, trade: Trade | None) -> LabelRow:
    c = seen.candidate
    features = compute(seen.snapshot, c, family=family)
    if trade is None:
        net, net_return, exit_time = ZERO, 0.0, None
    else:
        net = trade.net_pnl().quantize(NET_QUANTUM, rounding=ROUND_HALF_EVEN)
        net_return = float(net / outcome.notional_usd)
        exit_time = trade.exits[-1].time
    return LabelRow(
        candidate_id=c.candidate_id,
        family=family,
        instrument_id=c.instrument_id,
        decision_time=c.decision_time,
        exit_time=exit_time,
        status=outcome.status,
        net_usd=net,
        net_return=net_return,
        win=net > 0,
        features=features,
    )


def _bars_digest(instruments: Mapping[str, InstrumentSetup]) -> str:
    """SHA-256 over the canonical text of every bar and split supplied (label provenance)."""
    h = hashlib.sha256()
    for iid in sorted(instruments):
        setup = instruments[iid]
        for b in setup.bars:
            fields = [b.open, b.high, b.low, b.close, b.volume, b.vwap]
            text = "|".join([iid, iso_utc(b.start), str(b.minutes), *map(canon_decimal, fields)])
            h.update(f"{text}|{b.trade_count}\n".encode())
        for s in setup.splits:
            ratio = f"{canon_decimal(s.new_rate)}:{canon_decimal(s.old_rate)}"
            h.update(f"split|{s.instrument_id}|{s.ex_date.isoformat()}|{ratio}\n".encode())
    return h.hexdigest()


def build_labels(
    strategy: LabelStrategy,
    *,
    family: str,
    config: EngineConfig,
    instruments: Mapping[str, InstrumentSetup],
    plan: SplitPlan,
    unlocked: bool = False,
    source: Mapping[str, str] | None = None,
) -> LabelSet:
    """Label every candidate ``strategy`` emits with a decision time in ``[config.start, end)``.

    ``strategy`` must be fresh (not yet fed bars). ``instruments`` may include warm-up bars
    before the window and outcome bars after it, but no bar may overlap the locked final test
    period of ``plan`` unless ``unlocked``. ``source`` adds provenance such as the dataset name
    and content hashes; the builder adds the rule, costs, window, label rule and a digest of the
    supplied bars, and refuses a caller key that collides with those.
    """
    if family not in FAMILIES:
        raise LabelError(f"unknown rule family {family!r}")
    guard_window(plan, config.start, config.end, unlocked=unlocked)
    for setup in instruments.values():
        if setup.bars:
            first = min(b.start for b in setup.bars)
            last = max(b.end for b in setup.bars)
            guard_window(plan, first, last, unlocked=unlocked)
    series = {
        iid: _Series(
            setup=setup,
            starts=[b.start for b in setup.bars],
            ends=[b.end for b in setup.bars],
        )
        for iid, setup in instruments.items()
    }
    for s in series.values():
        if any(later <= earlier for earlier, later in itertools.pairwise(s.starts)):
            raise LabelError("bars must be strictly increasing in start time")
    recorder = _Recorder(strategy, config.start, config.end)
    ResearchEngine(config, recorder, instruments).run()
    rows: list[LabelRow] = []
    for seen in recorder.seen:
        outcome, trade = _simulate(
            seen,
            config,
            series[seen.candidate.instrument_id],
            strategy.strategy_id,
            strategy.spec_hash,
        )
        rows.append(_label(seen, family, outcome, trade))
    rows.sort(key=lambda r: (r.decision_time, r.candidate_id))
    provenance = {
        "strategy_id": strategy.strategy_id,
        "spec_hash": strategy.spec_hash,
        "family": family,
        "snapshot_version": strategy.snapshot_version,
        "window": f"{iso_utc(config.start)} .. {iso_utc(config.end)}",
        "label_rule": LABEL_RULE,
        "label_capital_usd": canon_decimal(LABEL_CAPITAL),
        "fee_schedule": config.fees.schedule_id,
        "fills": canonical_json(config.fills.describe()),
        "policy_hash": content_hash(config.policy),
        "bars_sha256": _bars_digest(instruments),
    }
    extra = dict(source or {})
    clash = sorted(set(extra) & set(provenance))
    if clash:
        raise LabelError(f"source keys {clash} are set by the label builder")
    return LabelSet(
        rows=tuple(rows),
        feature_names=FEATURE_NAMES,
        feature_version=FEATURE_VERSION,
        source={**extra, **provenance},
    )
