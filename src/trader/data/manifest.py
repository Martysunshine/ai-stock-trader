"""``research/data_manifest.json`` (implementation_roadmap.md §2).

For every dataset the manifest records source and feed, entitlement, the requested range,
availability and correction rules, the adjustment basis, which fields the source does *not*
supply, per-file checksums and quality findings, and - when a dataset cannot be acquired - the
blocker. Recorded replay sessions (M1) are listed under ``datasets``; bar histories under
``history``. The manifest is committed; the data files are not.

A request's ``timeframe`` is omitted for fixed-interval bars of ``bar_minutes`` (every dataset
recorded before 2026-11-09), so those entries serialise byte for byte as before. ``"1Day"``
requests one bar per XNYS regular session (``data/daily_bars.py``); its ``bar_minutes`` is the
nominal regular session, 390, while each bar carries its own session's length.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from trader.domain.base import ConfigModel
from trader.domain.types import UtcDatetime

HistorySource = Literal[
    "coinbase_exchange_public",
    "kraken_public_ohlc",
    "kraken_ohlcvt_archive",
    "alpaca_stock_bars",
    "synthetic_history",
    "capital_demo_prices",
]


class InstrumentRequest(ConfigModel):
    instrument_id: str
    source_symbol: str


#: Session daily bars; ``None`` (omitted) means fixed intervals of ``bar_minutes``.
Timeframe = Literal["1Day"]
DAILY_TIMEFRAME: Final = "1Day"
#: Nominal ``bar_minutes`` of a daily request: the regular XNYS session (early closes are shorter).
DAILY_BAR_MINUTES: Final = 390


class HistoryRequest(ConfigModel):
    instruments: tuple[InstrumentRequest, ...] = Field(min_length=1)
    bar_minutes: int = Field(ge=1)
    start: UtcDatetime
    end: UtcDatetime
    #: ``BROKER_CFD``: a CFD broker's own trading hours (about 23 hours on weekdays, a
    #: Sunday-evening open, closed on Saturday). No expected-bar grid is built for it:
    #: the bars that exist are the record of when the market was open (ADR-0052).
    calendar: Literal["UTC_24_7", "XNYS", "BROKER_CFD"]
    feed_param: str | None = None
    timeframe: Timeframe | None = Field(
        default=None,
        description="'1Day' = one bar per XNYS regular session; omitted = bars of bar_minutes",
    )

    @property
    def is_daily(self) -> bool:
        return self.timeframe == DAILY_TIMEFRAME

    @model_validator(mode="after")
    def _daily_shape(self) -> HistoryRequest:
        if self.is_daily:
            if self.calendar != "XNYS":
                raise ValueError("daily bars are defined per XNYS regular session")
            if self.bar_minutes != DAILY_BAR_MINUTES:
                raise ValueError(
                    "a daily request's bar_minutes is the regular session, 390 (each bar "
                    "carries its own session's length, shorter on early closes)"
                )
        return self

    @model_serializer(mode="wrap")
    def _omit_fixed_interval_timeframe(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Leave ``timeframe`` out unless set, so entries recorded before it existed stay
        byte-identical (their content hashes and registrations do not move)."""
        data: dict[str, Any] = handler(self)
        if self.timeframe is None:
            data.pop("timeframe", None)
        return data


class FileRecord(ConfigModel):
    instrument_id: str
    source_symbol: str
    file: str
    rows: int
    first_start: UtcDatetime | None
    last_start: UtcDatetime | None
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality: dict[str, Any]


class HistoryDataset(ConfigModel):
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    kind: Literal["HISTORICAL_BARS", "SYNTHETIC_BARS"]
    source: HistorySource
    venue: str
    feed: str
    entitlement: str
    request: HistoryRequest
    path: str
    availability_rule: str
    corrections: str
    adjustment_basis: Literal["RAW"]
    fields_not_supplied: tuple[str, ...] = ()
    status: Literal["REQUESTED", "ACQUIRED", "BLOCKED"]
    blocker: str = ""
    retrieved_at: UtcDatetime | None = None
    files: tuple[FileRecord, ...] = ()
    corporate_actions: tuple[dict[str, str], ...] = ()
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    restorable: bool = Field(
        default=True,
        description="False for one-time captures the source no longer serves (e.g. the Kraken 720-row endpoint); a fresh checkout cannot re-download them",
    )

    def file_for(self, instrument_id: str) -> FileRecord:
        for record in self.files:
            if record.instrument_id == instrument_id:
                return record
        raise KeyError(f"{self.name} has no file for {instrument_id}")


class DataManifest(ConfigModel):
    manifest_version: int
    prepared: str
    note: str
    datasets: tuple[dict[str, Any], ...] = ()
    history: tuple[HistoryDataset, ...] = ()

    def dataset(self, name: str) -> HistoryDataset:
        for ds in self.history:
            if ds.name == name:
                return ds
        raise KeyError(f"no history dataset named {name!r} in the manifest")

    def replace(self, updated: HistoryDataset) -> DataManifest:
        history = tuple(updated if d.name == updated.name else d for d in self.history)
        return self.model_copy(update={"history": history})


def load_manifest(path: Path) -> DataManifest:
    return DataManifest.model_validate_json(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, manifest: DataManifest) -> None:
    payload = manifest.model_dump(mode="json")
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
