"""The intel feed's configuration: which sources run how often, and what each instrument follows.

``configs/intel/sources.yaml`` (ADR-0047). Targets reuse the ids of the dashboard watchlists
(``stocks:AAPL``), so the console can place intel beside the same instrument's prices.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from trader.domain.base import ConfigModel
from trader.market.watchlists import DEFAULT_WATCHLISTS, REPO_ROOT, Watchlists, load_watchlists

SourceId = Literal["alpaca_news", "alpaca_premarket", "edgar_form4"]
SOURCE_IDS: tuple[SourceId, ...] = ("alpaca_news", "alpaca_premarket", "edgar_form4")
#: The kind of record each source writes.
SOURCE_KIND: dict[SourceId, str] = {
    "alpaca_news": "news",
    "alpaca_premarket": "premarket",
    "edgar_form4": "insider",
}
DEFAULT_CONFIG = REPO_ROOT / "configs" / "intel" / "sources.yaml"


class SourceConfig(ConfigModel):
    label: str = Field(min_length=1, max_length=60)
    every_minutes: int = Field(ge=1, le=240)
    #: News: how far back the first run reads.
    lookback_hours: int = Field(default=48, ge=1, le=336)
    #: Insiders: how far back filings are read and summed.
    lookback_days: int = Field(default=90, ge=1, le=365)
    #: Insiders: Form 4 documents fetched per run (the rest follow on later runs).
    max_documents_per_run: int = Field(default=25, ge=1, le=200)
    #: Pre-market: how old the newest requested bar must be (the free plan's SIP delay plus one).
    delay_minutes: int = Field(default=16, ge=0, le=60)


class Target(ConfigModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9]{0,15}:[A-Z0-9_]{1,16}$")
    news: tuple[str, ...] = ()
    news_proxy: str | None = Field(default=None, min_length=1, max_length=60)
    cik: int | None = Field(default=None, gt=0)
    premarket: bool = False

    @field_validator("news")
    @classmethod
    def _news_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for symbol in value:
            if not symbol or not symbol.isascii() or not symbol.isalnum() or not symbol.isupper():
                raise ValueError(f"news symbol {symbol!r} must be upper-case letters and digits")
        if len(set(value)) != len(value):
            raise ValueError("news symbols must be unique within a target")
        return value

    @property
    def group(self) -> str:
        return self.id.split(":", 1)[0]

    @property
    def symbol(self) -> str:
        return self.id.split(":", 1)[1]


class IntelConfig(ConfigModel):
    version: Literal[1] = 1
    sources: dict[SourceId, SourceConfig]
    targets: tuple[Target, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> IntelConfig:
        ids = [t.id for t in self.targets]
        if len(set(ids)) != len(ids):
            raise ValueError("target ids must be unique")
        for target in self.targets:
            if target.news_proxy and not target.news:
                raise ValueError(f"{target.id}: news_proxy needs news symbols")
            if (target.cik is not None or target.premarket) and target.group != "stocks":
                raise ValueError(f"{target.id}: insiders and pre-market apply to stocks only")
        return self

    def target(self, target_id: str) -> Target:
        for target in self.targets:
            if target.id == target_id:
                return target
        raise KeyError(f"no intel target {target_id!r}")

    def check_against(self, watchlists: Watchlists) -> None:
        """Every target must be an instrument of the dashboard watchlists."""
        known = {f"{g.id}:{i.symbol}" for g in watchlists.groups for i in g.instruments}
        unknown = [t.id for t in self.targets if t.id not in known]
        if unknown:
            raise ValueError(f"intel targets not in the watchlists: {', '.join(unknown)}")

    def names(self, watchlists: Watchlists) -> dict[str, str]:
        """Display name per target id, from the watchlists."""
        return {f"{g.id}:{i.symbol}": i.name for g in watchlists.groups for i in g.instruments}


def load_intel_config(
    path: Path = DEFAULT_CONFIG, watchlists_path: Path = DEFAULT_WATCHLISTS
) -> tuple[IntelConfig, Watchlists]:
    config = IntelConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    watchlists = load_watchlists(watchlists_path)
    config.check_against(watchlists)
    return config, watchlists
