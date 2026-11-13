"""Fee schedules with exact charging granularity.

Unknown fees mean ``NO_TRADE``, never zero fees (CRYPTO_TRADING_TRACK.md §6). Fees are rounded
*up* to their charging granularity; quantities are rounded *down* (risk_and_validation.md §3).
All models are monotone non-decreasing in quantity, which the sizing search relies on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from trader.config.policy import load_yaml_strict
from trader.domain.base import ConfigModel
from trader.domain.types import (
    ZERO,
    CurrencyCode,
    EvidenceLevel,
    Fraction,
    Liquidity,
    NonNegDec,
    PosDec,
    Side,
    round_up_to_step,
)


class FeeComponent(ABC):
    @abstractmethod
    def fee(
        self, side: Side, quantity: Decimal, notional: Decimal, liquidity: Liquidity
    ) -> Decimal:
        """Fee in the schedule currency for one order execution of ``quantity``/``notional``."""


@dataclass(frozen=True, slots=True)
class PercentWithMinimum(FeeComponent):
    sides: frozenset[Side]
    rate: Decimal
    minimum: Decimal
    granularity: Decimal

    def fee(
        self, side: Side, quantity: Decimal, notional: Decimal, liquidity: Liquidity
    ) -> Decimal:
        if side not in self.sides or quantity <= 0:
            return ZERO
        return round_up_to_step(max(self.rate * notional, self.minimum), self.granularity)


@dataclass(frozen=True, slots=True)
class MakerTaker(FeeComponent):
    sides: frozenset[Side]
    maker_rate: Decimal
    taker_rate: Decimal
    granularity: Decimal

    def fee(
        self, side: Side, quantity: Decimal, notional: Decimal, liquidity: Liquidity
    ) -> Decimal:
        if side not in self.sides or quantity <= 0:
            return ZERO
        rate = self.taker_rate if liquidity is Liquidity.TAKER else self.maker_rate
        return round_up_to_step(rate * notional, self.granularity)


@dataclass(frozen=True, slots=True)
class RegulatorySellFees(FeeComponent):
    """US sell-side pass-through fees (SEC Section 31 + FINRA TAF), each rounded up.

    The per-order minimum is a conservative modelling choice for fractional quantities where the
    exact broker rounding has not been verified.
    """

    sec_rate: Decimal
    taf_per_share: Decimal
    taf_max: Decimal
    minimum_per_order: Decimal
    granularity: Decimal

    def fee(
        self, side: Side, quantity: Decimal, notional: Decimal, liquidity: Liquidity
    ) -> Decimal:
        if side is not Side.SELL or quantity <= 0:
            return ZERO
        sec = round_up_to_step(self.sec_rate * notional, self.granularity)
        taf = round_up_to_step(min(self.taf_per_share * quantity, self.taf_max), self.granularity)
        return max(sec + taf, self.minimum_per_order)


@dataclass(frozen=True, slots=True)
class PerShare(FeeComponent):
    """A per-share charge on the chosen sides, rounded up (e.g. the FINRA CAT fee pass-through,
    charged per executed equivalent share on buys and sells)."""

    sides: frozenset[Side]
    rate_per_share: Decimal
    granularity: Decimal

    def fee(
        self, side: Side, quantity: Decimal, notional: Decimal, liquidity: Liquidity
    ) -> Decimal:
        if side not in self.sides or quantity <= 0:
            return ZERO
        return round_up_to_step(self.rate_per_share * quantity, self.granularity)


@dataclass(frozen=True, slots=True)
class FlatPerOrder(FeeComponent):
    sides: frozenset[Side]
    amount: Decimal

    def fee(
        self, side: Side, quantity: Decimal, notional: Decimal, liquidity: Liquidity
    ) -> Decimal:
        return self.amount if side in self.sides and quantity > 0 else ZERO


# ----------------------------------------------------------------------------- schema

_SidesField = Annotated[tuple[Side, ...], Field(min_length=1)]


class PercentWithMinimumCfg(ConfigModel):
    type: Literal["percent_with_minimum"]
    sides: _SidesField
    rate: Fraction
    minimum: NonNegDec
    granularity: PosDec


class MakerTakerCfg(ConfigModel):
    type: Literal["maker_taker"]
    sides: _SidesField
    maker_rate: Fraction
    taker_rate: Fraction
    granularity: PosDec


class RegulatorySellCfg(ConfigModel):
    type: Literal["regulatory_sell"]
    sec_rate: Fraction
    taf_per_share: NonNegDec
    taf_max: NonNegDec
    minimum_per_order: NonNegDec
    granularity: PosDec


class PerShareCfg(ConfigModel):
    type: Literal["per_share"]
    sides: _SidesField
    rate_per_share: NonNegDec
    granularity: PosDec


class FlatPerOrderCfg(ConfigModel):
    type: Literal["flat_per_order"]
    sides: _SidesField
    amount: NonNegDec


ComponentCfg = Annotated[
    PercentWithMinimumCfg | MakerTakerCfg | RegulatorySellCfg | PerShareCfg | FlatPerOrderCfg,
    Field(discriminator="type"),
]


class FeeSource(ConfigModel):
    """One official page behind a documented schedule: where, what it says, when it was read."""

    url: str = Field(pattern=r"^https://")
    states: str = Field(min_length=1)
    retrieved: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class FeeScheduleConfig(ConfigModel):
    schedule_id: str
    description: str
    evidence: EvidenceLevel
    known: bool = Field(description="False means the schedule is unknown: every trade is refused")
    source: str
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    currency: CurrencyCode
    default_liquidity: Liquidity = Liquidity.TAKER
    components: tuple[ComponentCfg, ...] = ()
    notes: str = ""
    sources: tuple[FeeSource, ...] = ()


def _build_component(cfg: ComponentCfg) -> FeeComponent:
    if isinstance(cfg, PercentWithMinimumCfg):
        return PercentWithMinimum(frozenset(cfg.sides), cfg.rate, cfg.minimum, cfg.granularity)
    if isinstance(cfg, MakerTakerCfg):
        return MakerTaker(frozenset(cfg.sides), cfg.maker_rate, cfg.taker_rate, cfg.granularity)
    if isinstance(cfg, RegulatorySellCfg):
        return RegulatorySellFees(
            cfg.sec_rate, cfg.taf_per_share, cfg.taf_max, cfg.minimum_per_order, cfg.granularity
        )
    if isinstance(cfg, PerShareCfg):
        return PerShare(frozenset(cfg.sides), cfg.rate_per_share, cfg.granularity)
    return FlatPerOrder(frozenset(cfg.sides), cfg.amount)


class UnknownFeesError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    config: FeeScheduleConfig
    components: tuple[FeeComponent, ...]

    @property
    def schedule_id(self) -> str:
        return self.config.schedule_id

    @property
    def known(self) -> bool:
        return self.config.known

    @property
    def currency(self) -> str:
        return self.config.currency

    def fee(
        self,
        side: Side,
        quantity: Decimal,
        notional: Decimal,
        liquidity: Liquidity | None = None,
    ) -> Decimal:
        if not self.config.known:
            raise UnknownFeesError(f"fee schedule {self.schedule_id} is unknown")
        liq = self.config.default_liquidity if liquidity is None else liquidity
        return sum((c.fee(side, quantity, notional, liq) for c in self.components), start=ZERO)

    def round_trip(
        self, quantity: Decimal, entry_notional: Decimal, exit_notional: Decimal
    ) -> Decimal:
        return self.fee(Side.BUY, quantity, entry_notional) + self.fee(
            Side.SELL, quantity, exit_notional
        )


def fee_schedule_from_config(cfg: FeeScheduleConfig) -> FeeSchedule:
    if not cfg.known and cfg.components:
        raise ValueError("an unknown schedule must not list components")
    if cfg.known and not cfg.components and cfg.evidence is EvidenceLevel.UNKNOWN:
        raise ValueError("a known schedule needs evidence")
    return FeeSchedule(cfg, tuple(_build_component(c) for c in cfg.components))


def load_fee_schedule(path: Path) -> FeeSchedule:
    return fee_schedule_from_config(FeeScheduleConfig.model_validate(load_yaml_strict(path)))
