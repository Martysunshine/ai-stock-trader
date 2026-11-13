"""Market events, bars and FX quotes (architecture.md §3, instructions.md §5 "Data and models").

Every observation carries its event time, its local receipt time, a feed identifier, a sequence
or de-duplication key and a correction version. A bar records its availability time separately
from its end: a bar may only be used once ``available_time`` has passed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from trader.domain.base import DomainModel
from trader.domain.types import (
    BPS,
    CurrencyCode,
    Dec,
    DomainError,
    NonNegDec,
    PosDec,
    UtcDatetime,
)


class _EventBase(DomainModel):
    instrument_id: str
    feed: str = Field(min_length=1)
    event_time: UtcDatetime
    receipt_time: UtcDatetime
    sequence: int = Field(ge=0)
    correction_version: int = Field(ge=0, default=0)

    @model_validator(mode="after")
    def _receipt_after_event(self) -> _EventBase:
        if self.receipt_time < self.event_time:
            raise DomainError("receipt_time precedes event_time")
        return self

    @property
    def dedup_key(self) -> str:
        return f"{self.feed}:{self.instrument_id}:{self.sequence}"

    @property
    def latency_ms(self) -> int:
        return int((self.receipt_time - self.event_time).total_seconds() * 1000)


class Quote(_EventBase):
    kind: Literal["quote"] = "quote"
    bid: PosDec
    ask: PosDec
    bid_size: NonNegDec
    ask_size: NonNegDec

    @model_validator(mode="after")
    def _uncrossed(self) -> Quote:
        if self.ask < self.bid:
            raise DomainError("crossed quote (ask < bid)")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid / BPS

    @property
    def half_spread_fraction(self) -> Decimal:
        return (self.ask - self.bid) / 2 / self.mid


class Trade(_EventBase):
    kind: Literal["trade"] = "trade"
    price: PosDec
    size: PosDec


class TradingStatus(_EventBase):
    """Halt/resume notification. A halted instrument cannot be exited immediately."""

    kind: Literal["status"] = "status"
    halted: bool
    reason: str = ""


MarketEvent = Annotated[Quote | Trade | TradingStatus, Field(discriminator="kind")]
AnyMarketEvent = Quote | Trade | TradingStatus


class Bar(DomainModel):
    instrument_id: str
    feed: str
    minutes: int = Field(ge=1)
    start: UtcDatetime
    end: UtcDatetime
    available_time: UtcDatetime
    complete: bool
    open: PosDec
    high: PosDec
    low: PosDec
    close: PosDec
    volume: NonNegDec
    trade_count: int = Field(ge=0)
    vwap: PosDec
    adjustment_basis: Literal["RAW", "SPLIT_ADJUSTED"] = "RAW"

    @model_validator(mode="after")
    def _shape(self) -> Bar:
        if not self.start < self.end <= self.available_time:
            raise DomainError("bar times must satisfy start < end <= available_time")
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise DomainError("inconsistent OHLC")
        return self

    def is_usable_at(self, now: datetime) -> bool:
        return self.complete and now >= self.available_time


class FxQuote(DomainModel):
    base: CurrencyCode
    quote: CurrencyCode
    bid: PosDec
    ask: PosDec
    event_time: UtcDatetime
    receipt_time: UtcDatetime
    source: str

    @model_validator(mode="after")
    def _uncrossed(self) -> FxQuote:
        if self.ask < self.bid:
            raise DomainError("crossed FX quote")
        return self


class FeatureSnapshot(DomainModel):
    instrument_id: str
    decision_time: UtcDatetime
    feature_version: str
    values: dict[str, Dec]
    data_watermark: UtcDatetime
    input_manifest_hash: str

    @model_validator(mode="after")
    def _causal(self) -> FeatureSnapshot:
        if self.data_watermark > self.decision_time:
            raise DomainError("feature snapshot uses data after its decision time")
        for name, value in self.values.items():
            if not value.is_finite():
                raise DomainError(f"feature {name} is not finite")
        return self
