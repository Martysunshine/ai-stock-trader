"""ETF instruments: a product type of their own, research only (markets_and_brokers.md §7).

US ETFs are not the ordinary shares v1 trades, so ``ProductType.ETF`` is separate from
``ORDINARY_EQUITY``: the STOCKS profile can never admit one, the paper certification path refuses
one, and the research specs in ``configs/instruments/etfs_research.yaml`` follow the
``alpaca_us_etfs_daily_sip_v1`` manifest entry exactly.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trader.accounting.fees import load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import LoadedPolicy
from trader.config.track_profiles import TrackProfile, load_track_profiles
from trader.data.manifest import load_manifest
from trader.domain.candidates import RejectReason, ReturnBasis
from trader.domain.certification import AssetMetadata
from trader.domain.instruments import EquitySpec, EtfSpec
from trader.domain.types import Direction, EvidenceLevel, ProductType, Track
from trader.risk.engine import FRACTIONAL_LIMIT_DAY, evaluate_entry, required_entry_capability
from trader.risk.sizing import ExposureState, SizingRequest, size_order
from trader.runtime.certification import CertificationRefused, verified_spec

from .test_risk_engine import SPECS, ctx

ROOT = Path(__file__).resolve().parents[1]
ETFS = load_instruments(ROOT / "configs/instruments/etfs_research.yaml")
SPY = ETFS["ARCX:SPY"]
assert isinstance(SPY, EtfSpec)


def _etf_data(**identity: object) -> dict[str, Any]:
    data = SPY.model_dump(mode="json")
    data["identity"] = {**data["identity"], **identity}
    return data


def test_the_research_file_lists_the_sixteen_etfs_of_the_daily_dataset() -> None:
    ds = load_manifest(ROOT / "research" / "data_manifest.json").dataset(
        "alpaca_us_etfs_daily_sip_v1"
    )
    assert sorted(ETFS) == sorted(i.instrument_id for i in ds.request.instruments)
    for iid, spec in ETFS.items():
        assert isinstance(spec, EtfSpec), iid
        ident = spec.identity
        assert ident.product_type is ProductType.ETF and ident.track is Track.STOCKS
        assert iid == f"{ident.venue_mic}:{ident.symbol}"
        assert ident.broker == "alpaca" and len(ident.broker_asset_id) == 36
        assert spec.allowed_directions == (Direction.LONG,)
        assert spec.contract_multiplier == spec.price_scale == Decimal(1)
        assert spec.quantity_step == Decimal("0.000000001")
        assert spec.min_notional == Decimal("1.00") and spec.price_tick == Decimal("0.01")
        assert spec.tradable and spec.fractionable
        assert spec.evidence is EvidenceLevel.DOCUMENTED
        assert spec.certify().certified
    assert len({s.identity.broker_asset_id for s in ETFS.values()}) == 16


def test_concentration_tags_group_shared_exposures_only() -> None:
    tags = {iid: s.exposure_tags for iid, s in ETFS.items()}
    for iid in ("ARCX:SPY", "XNAS:QQQ", "ARCX:IWM", "ARCX:DIA"):
        assert tags[iid] == ("US_EQUITY_INDEX",)
    assert tags["XNAS:TLT"] == tags["XNAS:IEF"] == ("US_TREASURY",)
    assert tags["ARCX:GLD"] == ("GOLD",)  # the same tag as gold ETCs, CFDs and miners
    sectors = [iid for iid in ETFS if iid.split(":")[1].startswith("XL")]
    assert len(sectors) == 9 and all(tags[iid] == () for iid in sectors)  # ADR-0015


def test_etf_spec_is_long_only_unit_multiplier_and_stock_route() -> None:
    with pytest.raises(ValidationError, match="product type ETF"):
        EtfSpec.model_validate(_etf_data(product_type="ORDINARY_EQUITY"))
    with pytest.raises(ValidationError, match="STOCKS track"):
        EtfSpec.model_validate(_etf_data(track="COMMODITIES"))
    with pytest.raises(ValidationError, match="long only"):
        EtfSpec.model_validate({**_etf_data(), "allowed_directions": ["LONG", "SHORT"]})
    with pytest.raises(ValidationError, match="unit multiplier"):
        EtfSpec.model_validate({**_etf_data(), "contract_multiplier": "2"})
    # An ordinary-equity spec cannot carry an ETF either.
    equity = SPECS["XNYS:SYNA"].model_dump(mode="json")
    equity["identity"]["product_type"] = "ETF"
    with pytest.raises(ValidationError, match="ORDINARY_EQUITY"):
        EquitySpec.model_validate(equity)


def test_the_stocks_profile_can_never_admit_etfs() -> None:
    stocks = load_track_profiles(ROOT / "configs/tracks")[Track.STOCKS]
    assert stocks.allowed_product_types == (ProductType.ORDINARY_EQUITY,)
    data = stocks.model_dump(mode="json")
    with pytest.raises(ValidationError, match="ordinary equities only"):
        TrackProfile.model_validate({**data, "allowed_product_types": ["ORDINARY_EQUITY", "ETF"]})


def test_the_risk_engine_rejects_an_etf_entry_as_a_forbidden_product(policy: LoadedPolicy) -> None:
    syna = SPECS["XNYS:SYNA"].model_dump(mode="json")
    syna["kind"] = "etf"
    syna["identity"]["product_type"] = "ETF"
    etf = EtfSpec.model_validate(syna)
    decision = evaluate_entry(ctx(policy, spec=etf))
    assert not decision.approved
    assert RejectReason.FORBIDDEN_PRODUCT in decision.reasons
    assert any("ETF not allowed" in d for d in decision.details)
    assert evaluate_entry(ctx(policy)).approved  # the same context with the ordinary share


def test_an_etf_needs_the_same_order_capability_as_a_share() -> None:
    assert required_entry_capability(SPY) == FRACTIONAL_LIMIT_DAY
    whole = SPY.model_copy(update={"fractionable": False})
    assert required_entry_capability(whole) == "whole.limit.day"


def test_paper_certification_refuses_an_etf() -> None:
    asset = AssetMetadata(
        instrument_id="ARCX:SPY",
        symbol="SPY",
        broker_asset_id=SPY.identity.broker_asset_id,
        asset_class="us_equity",
        exchange="ARCA",
        status="active",
        tradable=True,
        fractionable=True,
        marginable=True,
        shortable=True,
    )
    with pytest.raises(CertificationRefused, match="not an ordinary equity"):
        verified_spec(SPY, asset, fetched_at=datetime(2026, 11, 9, tzinfo=UTC), run_id="r")


def test_etf_sizing_uses_share_arithmetic_and_the_shared_tag_cap(policy: LoadedPolicy) -> None:
    base = SizingRequest(
        policy=policy.policy,
        spec=SPY,
        direction=Direction.LONG,
        entry_price=Decimal("680.00"),
        stop_price=Decimal("660.00"),
        target_price=Decimal("720.00"),
        fx_to_usd=Decimal(1),
        fees=load_fee_schedule(ROOT / "configs/fees/alpaca_us_equities_v2.yaml"),
        gap_allowance_fraction=Decimal("0.001"),
        equity_usd=Decimal(50),
        portfolio_spendable_usd=Decimal(50),
        account_spendable_usd=Decimal(50),
        exposure=ExposureState(),
        expected_gross_return=Decimal("0.05"),
        uncertainty_margin=Decimal("0.001"),
        basis=ReturnBasis.MID_TO_MID,
        half_spread_fraction=Decimal("0.0001"),
    )
    result = size_order(base)
    assert result.approved, result.details
    assert result.notional_usd == result.quantity * Decimal("680.00")
    assert result.planned_loss_usd <= Decimal("0.125")
    # Another broad index fund already holds the tag's whole 20% cap: SPY gets no room.
    full = ExposureState(by_tag_usd={"US_EQUITY_INDEX": Decimal(10)})
    capped = size_order(replace(base, exposure=full))
    assert not capped.approved and capped.quantity == 0
    assert "US_EQUITY_INDEX" in capped.binding_constraint + " ".join(capped.details)
