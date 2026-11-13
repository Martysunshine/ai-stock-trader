"""Crypto and commodity/CFD contract maths (CRYPTO §6, §9; COMMODITIES §5, §10)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from trader.accounting.fees import load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import LoadedPolicy
from trader.domain.candidates import RejectReason, ReturnBasis
from trader.domain.instruments import CertificationExample, CommodityInstrumentSpec, CryptoSpotSpec
from trader.domain.types import Direction, EvidenceLevel
from trader.reporting.feasibility import cfd_bounds
from trader.risk.sizing import ExposureState, SizingRequest, size_order

ROOT = Path(__file__).resolve().parents[1]
CFD = load_instruments(ROOT / "configs/instruments/commodity_fixtures.yaml")
CRYPTO = load_instruments(ROOT / "configs/instruments/crypto_examples.yaml")
GOLD = CFD["CFDFIX:XAUUSD"]
SILVER = CFD["CFDFIX:XAGUSD"]
assert isinstance(GOLD, CommodityInstrumentSpec) and isinstance(SILVER, CommodityInstrumentSpec)


def _cfd_request(
    policy: LoadedPolicy, spec: CommodityInstrumentSpec, price: str, **kw: object
) -> SizingRequest:
    p = Decimal(price)
    base = SizingRequest(
        policy=policy.policy,
        spec=spec,
        direction=Direction.LONG,
        entry_price=p,
        stop_price=p * Decimal("0.99"),
        target_price=p * Decimal("1.02"),
        fx_to_usd=Decimal(1),
        fees=load_fee_schedule(ROOT / "configs/fees/cfd_fixture_zero_commission.yaml"),
        gap_allowance_fraction=Decimal("0.001"),
        equity_usd=Decimal(50),
        portfolio_spendable_usd=Decimal(50),
        account_spendable_usd=Decimal(50),
        exposure=ExposureState(),
        expected_gross_return=Decimal("0.01"),
        uncertainty_margin=Decimal("0.001"),
        basis=ReturnBasis.MID_TO_MID,
        half_spread_fraction=Decimal("0.0002"),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_gold_minimum_lot_fits_margin_but_violates_notional_and_risk(policy: LoadedPolicy) -> None:
    """Fixture: USD 4,000/oz, 0.1 oz, 1% stop -> USD 400 notional, USD 4 price risk, USD 20 margin."""
    assert GOLD.notional_usd(Decimal("0.1"), Decimal(4000), Decimal(1)) == Decimal(400)
    assert GOLD.margin is not None
    assert Decimal(400) * GOLD.margin.initial_margin_fraction == Decimal(20)
    result = size_order(_cfd_request(policy, GOLD, "4000"))
    assert not result.approved and result.quantity == 0
    assert RejectReason.RISK_LIMIT in result.reasons
    bounds = cfd_bounds(GOLD, Decimal(4000), policy)
    assert bounds.by_symbol_cap == Decimal(2000)
    assert bounds.by_price_risk == Decimal(1600)


def test_margin_is_never_treated_as_exposure(policy: LoadedPolicy) -> None:
    result = size_order(_cfd_request(policy, GOLD, "4000"))
    assert "symbol_notional" in result.binding_constraint or "notional" in " ".join(result.details)


def test_multiplier_off_by_100x_fails_certification() -> None:
    assert GOLD.certify().certified
    wrong = GOLD.model_copy(update={"contract_multiplier": Decimal(100)})
    cert = wrong.certify()
    assert not cert.certified
    assert any("multiplier" in p for p in cert.problems)


def test_silver_units_and_short_pnl_are_independent_of_gold() -> None:
    assert SILVER.quantity_step != GOLD.quantity_step
    assert SILVER.gross_pnl_quote(
        Direction.SHORT, Decimal(2), Decimal(50), Decimal("49.5")
    ) == Decimal(1)
    assert SILVER.gross_pnl_quote(
        Direction.LONG, Decimal(2), Decimal(50), Decimal("49.5")
    ) == Decimal(-1)
    assert SILVER.certify().certified
    bounds = cfd_bounds(SILVER, Decimal(50), _policy_for_bounds())
    assert bounds.by_symbol_cap == Decimal(250)


def _policy_for_bounds() -> LoadedPolicy:
    from trader.config.policy import load_policy

    return load_policy(ROOT / "configs/policy/base.yaml")


def test_certification_example_with_wrong_statement_fails() -> None:
    bad = GOLD.model_copy(
        update={
            "certification_examples": (
                CertificationExample(
                    direction=Direction.LONG,
                    quantity=Decimal("0.1"),
                    entry_price=Decimal(4000),
                    exit_price=Decimal(4010),
                    statement_pnl_quote=Decimal(100),
                    source="statement",
                ),
            )
        }
    )
    assert not bad.certify().certified


def test_crypto_placeholders_are_not_tradable_until_metadata_is_fetched() -> None:
    btc = CRYPTO["KRAKEN:BTC/EUR"]
    assert btc.evidence is EvidenceLevel.UNKNOWN
    assert not btc.certify().certified


def test_base_asset_fee_reduces_sellable_quantity_and_dust_is_explicit() -> None:
    eth = CRYPTO["KRAKEN:ETH/EUR"]
    assert isinstance(eth, CryptoSpotSpec)
    base_fee_eth = eth.model_copy(update={"fee_asset": "BASE"})
    assert isinstance(base_fee_eth, CryptoSpotSpec)
    assert base_fee_eth.sellable_after_fees(Decimal("0.003"), Decimal("0.00002")) == Decimal(
        "0.00298"
    )
    # a fill barely above the minimum becomes unsellable dust after the base-asset fee
    assert (
        base_fee_eth.sellable_after_fees(Decimal("0.002"), Decimal("0.0000016")) < eth.min_quantity
    )
    assert eth.dust_after_exit(Decimal("0.0015")) == Decimal("0.0015")


def test_kraken_taker_round_trip_exceeds_the_risk_budget() -> None:
    kraken = load_fee_schedule(ROOT / "configs/fees/kraken_spot_tier1.yaml")
    assert kraken.round_trip(Decimal(1), Decimal(10), Decimal(10)) > Decimal("0.125")


def test_same_gold_exposure_through_two_routes_shares_one_concentration_cap(
    policy: LoadedPolicy,
) -> None:
    """A gold miner and a gold CFD do not diversify each other: the GOLD tag is capped like a symbol."""
    cheap_gold = GOLD.model_copy(
        update={"min_quantity": Decimal("0.001"), "quantity_step": Decimal("0.001")}
    )
    exposure = ExposureState(gross_usd=Decimal(9), by_tag_usd={"GOLD": Decimal(9)})
    r = size_order(_cfd_request(policy, cheap_gold, "4000", exposure=exposure))
    if r.approved:
        assert r.notional_usd <= Decimal(1)
    else:
        assert RejectReason.RISK_LIMIT in r.reasons or RejectReason.TOO_COSTLY in r.reasons
