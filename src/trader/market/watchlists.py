"""The dashboard's watchlists: three groups of instruments and where their prices come from.

``configs/market/watchlists.yaml`` (ADR-0045). A group names one source; the source fixes the
price basis (traded prices or bid/ask mids) and whether volume is meaningful, so the file cannot
claim volume for a CFD mid price.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from trader.domain.base import ConfigModel

SeriesName = Literal["5m", "1h", "1d"]
SERIES: tuple[SeriesName, ...] = ("5m", "1h", "1d")
SERIES_MINUTES: dict[SeriesName, int] = {"5m": 5, "1h": 60, "1d": 1440}
SourceKind = Literal["alpaca_iex", "kraken_public", "capital_demo"]
DayRule = Literal["nyse", "utc"]
Basis = Literal["trade", "mid"]

#: Per source: the price basis and whether bars carry real traded volume.
SOURCE_TRAITS: dict[SourceKind, tuple[Basis, bool]] = {
    "alpaca_iex": ("trade", True),
    "kraken_public": ("trade", True),
    "capital_demo": ("mid", False),
}
#: Display decimals are the source's decimals clamped to this range (the console's chart limits).
PRECISION_RANGE = (2, 6)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WATCHLISTS = REPO_ROOT / "configs" / "market" / "watchlists.yaml"


class InstrumentConfig(ConfigModel):
    symbol: str = Field(pattern=r"^[A-Z0-9_]{1,16}$")
    name: str = Field(min_length=1, max_length=80)
    #: The source's own name (Kraken pair, Capital.com epic); the symbol when omitted.
    source_id: str | None = Field(default=None, pattern=r"^[A-Z0-9_.]{1,40}$")
    decimals: int = Field(default=2, ge=0, le=12)
    min_deal: Decimal | None = Field(default=None, gt=0)

    @property
    def source_symbol(self) -> str:
        return self.source_id or self.symbol

    @property
    def precision(self) -> int:
        low, high = PRECISION_RANGE
        return max(low, min(high, self.decimals))


class GroupConfig(ConfigModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9]{0,15}$")
    title: str = Field(min_length=1, max_length=40)
    source: SourceKind
    source_label: str = Field(min_length=1, max_length=60)
    day_rule: DayRule
    instruments: tuple[InstrumentConfig, ...] = Field(min_length=1)

    @field_validator("instruments")
    @classmethod
    def _unique_symbols(cls, value: tuple[InstrumentConfig, ...]) -> tuple[InstrumentConfig, ...]:
        symbols = [i.symbol for i in value]
        if len(set(symbols)) != len(symbols):
            raise ValueError("instrument symbols must be unique within a group")
        return value

    @property
    def basis(self) -> Basis:
        return SOURCE_TRAITS[self.source][0]

    @property
    def has_volume(self) -> bool:
        return SOURCE_TRAITS[self.source][1]


class Watchlists(ConfigModel):
    version: Literal[1] = 1
    every_minutes: int = Field(default=5, ge=1, le=60)
    retention_days: dict[SeriesName, int]
    groups: tuple[GroupConfig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Watchlists:
        if set(self.retention_days) != set(SERIES):
            raise ValueError(f"retention_days needs exactly {', '.join(SERIES)}")
        if any(days < 1 for days in self.retention_days.values()):
            raise ValueError("retention must be at least one day")
        ids = [g.id for g in self.groups]
        if len(set(ids)) != len(ids):
            raise ValueError("group ids must be unique")
        return self

    def retention(self, series: SeriesName) -> timedelta:
        return timedelta(days=self.retention_days[series])

    def group(self, group_id: str) -> GroupConfig:
        for g in self.groups:
            if g.id == group_id:
                return g
        raise KeyError(f"no watchlist group {group_id!r}; known: {', '.join(self.ids())}")

    def ids(self) -> list[str]:
        return [g.id for g in self.groups]


def load_watchlists(path: Path = DEFAULT_WATCHLISTS) -> Watchlists:
    return Watchlists.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
