"""Instrument identities and product-specific specifications for all three tracks.

* ``EquitySpec`` – ordinary stocks (STOCKS track, long only, v1 rules unchanged).
* ``EtfSpec`` – US-listed exchange-traded funds and trusts on the stock route (research only:
  the STOCKS profile admits ``ORDINARY_EQUITY`` alone, markets_and_brokers.md §7).
* ``CryptoSpotSpec`` – fully paid spot pairs (CRYPTO_TRADING_TRACK.md §4 ``CryptoInstrumentSpec``).
* ``CommodityInstrumentSpec`` – ETC/fund or CFD routes (COMMODITIES_AND_CFD_TRACK.md §5).

All sizing maths uses the dimensionally explicit notional ``q * m * P * price_scale * fx``.
Margin is collateral, never exposure: nothing here derives exposure from margin.

``exposure_tags`` are *concentration* tags for a shared underlying economic exposure (``GOLD``,
``BTC``...). Each tag is capped like a single symbol (20% of effective equity), so a gold miner, a
gold ETC and a gold CFD are never assumed to diversify one another (COMMODITIES §4). Broad
labels such as a sector are deliberately not tags; sector concentration is disclosed in reports.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from trader.domain.base import DomainModel
from trader.domain.types import (
    ONE,
    ZERO,
    CurrencyCode,
    Dec,
    Direction,
    DomainError,
    EvidenceLevel,
    Fraction,
    NonNegDec,
    PosDec,
    ProductType,
    Track,
    UtcDatetime,
    round_down_to_step,
)


class InstrumentIdentity(DomainModel):
    """A ticker alone is insufficient across venues or symbol changes (architecture.md §3)."""

    instrument_id: str = Field(min_length=3, pattern=r"^[A-Z0-9_]+:[A-Z0-9_./-]+$")
    broker: str = Field(min_length=1)
    broker_asset_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    venue_mic: str | None = None
    currency: CurrencyCode
    product_type: ProductType
    track: Track
    lineage: tuple[str, ...] = ()


class CertificationExample(DomainModel):
    """A broker statement observation used to certify unit and price-scale interpretation."""

    direction: Direction
    quantity: PosDec
    entry_price: PosDec
    exit_price: PosDec
    statement_pnl_quote: Dec
    source: str = Field(min_length=1)


class CertificationResult(DomainModel):
    certified: bool
    problems: tuple[str, ...] = ()


class _SpecBase(DomainModel):
    identity: InstrumentIdentity
    quantity_step: PosDec
    min_quantity: NonNegDec
    max_quantity: PosDec | None = None
    min_notional: NonNegDec = ZERO
    price_tick: PosDec
    contract_multiplier: PosDec = ONE
    price_scale: PosDec = ONE
    exposure_tags: tuple[str, ...] = ()
    allowed_directions: tuple[Direction, ...] = (Direction.LONG,)
    tradable: bool = True
    fractionable: bool = False
    evidence: EvidenceLevel
    as_of: UtcDatetime
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_tags(self) -> _SpecBase:
        for tag in self.exposure_tags:
            if not tag.replace("_", "").isalnum() or not tag.isupper():
                raise DomainError(f"exposure tag must be UPPER_SNAKE: {tag!r}")
        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            raise DomainError("max_quantity below min_quantity")
        return self

    # ----------------------------------------------------------------- arithmetic
    @property
    def instrument_id(self) -> str:
        return self.identity.instrument_id

    @property
    def currency(self) -> str:
        return self.identity.currency

    def units_per_quantity(self) -> Decimal:
        """Underlying units (with price scaling) represented by one broker quantity unit."""
        return self.contract_multiplier * self.price_scale

    def notional_quote(self, quantity: Decimal, price: Decimal) -> Decimal:
        return abs(quantity) * self.units_per_quantity() * price

    def notional_usd(self, quantity: Decimal, price: Decimal, fx_to_usd: Decimal) -> Decimal:
        return self.notional_quote(quantity, price) * fx_to_usd

    def price_risk_usd(
        self, quantity: Decimal, entry: Decimal, stop: Decimal, fx_to_usd: Decimal
    ) -> Decimal:
        return abs(quantity) * self.units_per_quantity() * abs(entry - stop) * fx_to_usd

    def gross_pnl_quote(
        self, direction: Direction, quantity: Decimal, entry: Decimal, exit_: Decimal
    ) -> Decimal:
        sign = ONE if direction is Direction.LONG else -ONE
        return sign * quantity * self.units_per_quantity() * (exit_ - entry)

    def round_quantity_down(self, quantity: Decimal) -> Decimal:
        q = round_down_to_step(max(quantity, ZERO), self.quantity_step)
        if self.max_quantity is not None and q > self.max_quantity:
            q = round_down_to_step(self.max_quantity, self.quantity_step)
        return q

    def minimum_violations(self, quantity: Decimal, price: Decimal) -> list[str]:
        problems: list[str] = []
        if quantity <= 0:
            problems.append("quantity rounds to zero")
        if quantity < self.min_quantity:
            problems.append(f"quantity {quantity} below broker minimum {self.min_quantity}")
        notional = self.notional_quote(quantity, price)
        if notional < self.min_notional:
            problems.append(f"notional {notional} below broker minimum {self.min_notional}")
        if not self.fractionable and quantity != quantity.to_integral_value():
            problems.append("fractional quantity on a non-fractionable instrument")
        return problems

    def certify(self) -> CertificationResult:
        problems: list[str] = []
        if self.evidence is EvidenceLevel.UNKNOWN:
            problems.append("instrument metadata evidence is UNKNOWN")
        if not self.tradable:
            problems.append("instrument is not tradable")
        return CertificationResult(certified=not problems, problems=tuple(problems))


class EquitySpec(_SpecBase):
    kind: Literal["equity"] = "equity"

    @model_validator(mode="after")
    def _equity_rules(self) -> EquitySpec:
        if self.identity.product_type is not ProductType.ORDINARY_EQUITY:
            raise DomainError("EquitySpec requires ORDINARY_EQUITY")
        if self.identity.track is not Track.STOCKS:
            raise DomainError("ordinary equities execute on the STOCKS track")
        if self.contract_multiplier != ONE or self.price_scale != ONE:
            raise DomainError("ordinary equities have unit multiplier and price scale")
        if self.allowed_directions != (Direction.LONG,):
            raise DomainError("v1 stock profile is long only (instructions.md §5)")
        return self


class EtfSpec(_SpecBase):
    """A US-listed exchange-traded fund or trust, bought fully paid through the stock route.

    An ETF is not an ordinary share. markets_and_brokers.md §7 warns that European retail users
    may be barred from US ETFs and that observing a price series is not permission to buy the
    product; "V1 uses ordinary shares". The STOCKS track profile admits ``ORDINARY_EQUITY``
    only, so the risk engine rejects every ETF entry as ``FORBIDDEN_PRODUCT`` in every mode, and
    the paper certification path (``EquitySpec`` only) never accepts one. ETF specs therefore
    serve research until an owner-approved profile names ``ProductType.ETF``.

    The arithmetic is that of a share: one quantity unit is one fund share (unit multiplier and
    price scale), long only and fully paid (instructions.md §5: no leverage, no shorting).
    Leveraged and inverse funds are excluded by instructions.md §5 and must not be configured;
    their daily-reset leverage would break the unit-multiplier exposure model used here.
    """

    kind: Literal["etf"] = "etf"

    @model_validator(mode="after")
    def _etf_rules(self) -> EtfSpec:
        if self.identity.product_type is not ProductType.ETF:
            raise DomainError("EtfSpec requires product type ETF")
        if self.identity.track is not Track.STOCKS:
            raise DomainError("US-listed ETFs use the stock execution route (STOCKS track)")
        if self.contract_multiplier != ONE or self.price_scale != ONE:
            raise DomainError("an ETF share has unit multiplier and price scale")
        if self.allowed_directions != (Direction.LONG,):
            raise DomainError("ETFs are bought fully paid and long only (instructions.md §5)")
        return self


class CryptoSpotSpec(_SpecBase):
    """CRYPTO_TRADING_TRACK.md §4 ``CryptoInstrumentSpec``: fully paid spot, long only."""

    kind: Literal["crypto_spot"] = "crypto_spot"
    base_asset: str = Field(min_length=2)
    quote_asset: CurrencyCode
    fee_asset: Literal["QUOTE", "BASE", "OTHER"] = "QUOTE"
    market_status: Literal["online", "cancel_only", "post_only", "limit_only", "halted"] = "online"

    @model_validator(mode="after")
    def _crypto_rules(self) -> CryptoSpotSpec:
        if self.identity.product_type is not ProductType.CRYPTO_SPOT:
            raise DomainError("CryptoSpotSpec requires CRYPTO_SPOT")
        if self.identity.track is not Track.CRYPTO:
            raise DomainError("crypto spot belongs to the CRYPTO track")
        if self.quote_asset != self.identity.currency:
            raise DomainError("instrument currency must equal the quote asset")
        if self.allowed_directions != (Direction.LONG,):
            raise DomainError("spot crypto is long only and fully paid")
        if self.contract_multiplier != ONE or self.price_scale != ONE:
            raise DomainError("spot pairs have unit multiplier and price scale")
        return self

    def sellable_after_fees(self, filled_quantity: Decimal, base_asset_fee: Decimal) -> Decimal:
        """Quantity that can actually be sold once base-asset fees are deducted."""
        if base_asset_fee < 0 or filled_quantity < 0:
            raise DomainError("negative quantity or fee")
        held = filled_quantity - (base_asset_fee if self.fee_asset == "BASE" else ZERO)
        return self.round_quantity_down(max(held, ZERO))

    def dust_after_exit(self, held_quantity: Decimal) -> Decimal:
        """Residual quantity that cannot be sold because it is below the exit minimum."""
        sellable = self.round_quantity_down(held_quantity)
        if sellable < self.min_quantity:
            return held_quantity
        return held_quantity - sellable

    def certify(self) -> CertificationResult:
        base = super().certify()
        problems = list(base.problems)
        if self.market_status != "online":
            problems.append(f"market status {self.market_status}")
        if self.fee_asset == "OTHER":
            problems.append("fees charged in a third asset are not supported (no fee-token buying)")
        return CertificationResult(certified=not problems, problems=tuple(problems))


class MarginTerms(DomainModel):
    initial_margin_fraction: Fraction
    maintenance_margin_fraction: Fraction
    closeout_level_fraction: Fraction | None = None
    netting_mode: Literal["NETTING", "HEDGING", "UNKNOWN"] = "UNKNOWN"


class FinancingTerms(DomainModel):
    """Overnight funding as a signed daily fraction of notional; positive = charge to us."""

    long_daily_charge_fraction: Dec
    short_daily_charge_fraction: Dec
    charge_time_utc: str = Field(pattern=r"^\d{2}:\d{2}$")
    weekend_multiplier_days: int = Field(ge=1, le=5, default=3)
    as_of: UtcDatetime
    source: str


class CommodityInstrumentSpec(_SpecBase):
    """COMMODITIES_AND_CFD_TRACK.md §5 normalised contract model."""

    kind: Literal["commodity"] = "commodity"
    underlying: str = Field(min_length=2)
    quantity_unit: str = Field(min_length=1)
    expiring: bool = False
    margin: MarginTerms | None = None
    financing: FinancingTerms | None = None
    native_stop_supported: bool | None = None
    guaranteed_stop_premium_fraction: Fraction | None = None
    certification_examples: tuple[CertificationExample, ...] = ()

    @model_validator(mode="after")
    def _commodity_rules(self) -> CommodityInstrumentSpec:
        product = self.identity.product_type
        if product not in (ProductType.ETC, ProductType.CFD):
            raise DomainError("commodity route must be an ETC/fund or a CFD")
        if self.identity.track is not Track.COMMODITIES:
            raise DomainError("commodity instruments belong to the COMMODITIES track")
        if product is ProductType.ETC and self.allowed_directions != (Direction.LONG,):
            raise DomainError("the fully paid securities route is long only")
        if product is ProductType.CFD and self.margin is None:
            raise DomainError("a CFD spec must declare its margin terms")
        return self

    def certify(self) -> CertificationResult:
        base = super().certify()
        problems = list(base.problems)
        if self.expiring:
            problems.append("expiring/rolling contracts are not supported in this MVP")
        if self.identity.product_type is ProductType.CFD:
            if self.financing is None:
                problems.append("CFD financing terms unknown")
            if not self.certification_examples:
                problems.append(
                    "no broker-statement certification example for multiplier/price scale"
                )
            tick_value = self.units_per_quantity() * self.price_tick
            for ex in self.certification_examples:
                expected = self.gross_pnl_quote(
                    ex.direction, ex.quantity, ex.entry_price, ex.exit_price
                )
                tolerance = ex.quantity * tick_value / 2
                if abs(expected - ex.statement_pnl_quote) > tolerance:
                    problems.append(
                        f"unit interpretation mismatch: model P&L {expected} vs statement "
                        f"{ex.statement_pnl_quote} ({ex.source}); multiplier or price scale wrong"
                    )
        return CertificationResult(certified=not problems, problems=tuple(problems))


InstrumentSpec = Annotated[
    EquitySpec | EtfSpec | CryptoSpotSpec | CommodityInstrumentSpec, Field(discriminator="kind")
]
AnySpec = EquitySpec | EtfSpec | CryptoSpotSpec | CommodityInstrumentSpec
