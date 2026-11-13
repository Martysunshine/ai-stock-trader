"""The Capital.com CFD instrument specs, fee schedule and research sleeve policies (ADR-0052):
they load, they agree with the practice sleeves, and the existing sizer sizes long and short CFD
trades with them, margin as a cash check only and notional never above equity."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from trader.accounting.fees import load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import load_policy
from trader.config.secrets import CAPITAL_DEMO_EPICS
from trader.domain.candidates import RejectReason, ReturnBasis
from trader.domain.instruments import CommodityInstrumentSpec
from trader.domain.types import Direction, ProductType, Track
from trader.practice.journal import load_rules
from trader.risk.sizing import ExposureState, SizingRequest, size_order

ROOT = Path(__file__).resolve().parents[1]
#: Version 2 (ADR-0060) is version 1, unchanged and pinned for the registered trials, plus the
#: practice commodities; every demo epic has a spec in it.
SPECS_V1 = load_instruments(ROOT / "configs/instruments/capital_demo_cfds_v1.yaml")
SPECS = load_instruments(ROOT / "configs/instruments/capital_demo_cfds_v2.yaml")
FEES = load_fee_schedule(ROOT / "configs/fees/capital_com_cfd_demo_v1.yaml")
CFD = load_policy(ROOT / "configs/policy/cfd_sleeve_v1.yaml").policy
CRYPTO = load_policy(ROOT / "configs/policy/crypto_cfd_sleeve_v1.yaml").policy


def spec(epic: str) -> CommodityInstrumentSpec:
    s = SPECS[f"CAPITAL:{epic}"]
    assert isinstance(s, CommodityInstrumentSpec)
    return s


def test_every_demo_epic_has_a_cfd_spec_with_margin_and_financing() -> None:
    assert {s.identity.symbol for s in SPECS.values()} == set(CAPITAL_DEMO_EPICS)
    assert all(SPECS[k] == v for k, v in SPECS_V1.items())  # version 1 carried over as it is
    assert set(SPECS) - set(SPECS_V1) == {
        "CAPITAL:COPPER",
        "CAPITAL:OIL_BRENT",
        "CAPITAL:HEATINGOIL",
        "CAPITAL:NATURALGAS",
    }
    for s in SPECS.values():
        assert isinstance(s, CommodityInstrumentSpec)
        assert s.identity.product_type is ProductType.CFD and s.identity.track is Track.COMMODITIES
        assert s.allowed_directions == (Direction.LONG, Direction.SHORT)
        assert s.margin is not None and s.financing is not None
        assert s.financing.charge_time_utc == "22:00"
    # the broker's quoted cash flow to the client, as a charge: indices pay on a long
    assert spec("US500").financing.long_daily_charge_fraction == Decimal("0.000222069")  # type: ignore[union-attr]
    assert spec("BTCUSD").financing.short_daily_charge_fraction < 0  # type: ignore[union-attr]
    assert spec("BTCUSD").financing.weekend_multiplier_days == 1  # type: ignore[union-attr]
    assert spec("GOLD").financing.weekend_multiplier_days == 3  # type: ignore[union-attr]
    assert spec("DE40").currency == "EUR"
    # one tag per correlation group, as the practice sleeves group their epics
    groups = {**load_rules("capital-demo").groups, **load_rules("crypto-cfd").groups}
    for members in groups.values():
        assert len({spec(e).exposure_tags for e in members}) == 1
    # sizes agree with what the practice journal accepts
    for venue in ("capital-demo", "crypto-cfd"):
        for epic, rule in load_rules(venue).epics.items():
            assert spec(epic).min_quantity == rule.min_size


def test_the_fee_schedule_is_known_and_charges_nothing_per_order() -> None:
    assert FEES.known and FEES.schedule_id == "capital-com-cfd-demo-2026-11-10"
    from trader.domain.types import Side

    assert FEES.fee(Side.BUY, Decimal("0.01"), Decimal(75)) == 0


def _size(
    policy: object,
    epic: str,
    direction: Direction,
    entry: str,
    stop: str,
    target: str,
    *,
    nights: int = 7,
    equity: str = "100",
) -> object:
    e = Decimal(entry)
    return size_order(
        SizingRequest(
            policy=policy,  # type: ignore[arg-type]
            spec=spec(epic),
            direction=direction,
            entry_price=e,
            stop_price=Decimal(stop),
            target_price=Decimal(target),
            fx_to_usd=Decimal(1),
            fees=FEES,
            gap_allowance_fraction=Decimal("0.001"),
            equity_usd=Decimal(equity),
            portfolio_spendable_usd=Decimal(equity),
            account_spendable_usd=Decimal(equity),
            exposure=ExposureState(),
            expected_gross_return=abs(Decimal(target) / e - 1),
            uncertainty_margin=Decimal(0),
            basis=ReturnBasis.EXECUTABLE,
            half_spread_fraction=Decimal(0),
            financing_nights=nights,
        )
    )


def test_the_sizer_sizes_long_and_short_cfd_trades_inside_the_sleeve_limits() -> None:
    long = _size(CFD, "US100", Direction.LONG, "27000", "26400", "27500")
    assert long.approved and long.quantity == Decimal("0.003")  # type: ignore[attr-defined]
    assert long.planned_loss_usd <= 2 and long.notional_usd <= 100  # type: ignore[attr-defined]
    # margin is a cash check only: 5% of the notional plus the planned loss
    assert long.cash_required_usd == (  # type: ignore[attr-defined]
        long.notional_usd * Decimal("0.05") + long.planned_loss_usd  # type: ignore[attr-defined]
    )
    assert long.financing_usd > 0  # type: ignore[attr-defined]
    short = _size(CRYPTO, "BTCUSD", Direction.SHORT, "73500", "76000", "68500")
    assert short.approved and short.planned_loss_usd <= 1  # type: ignore[attr-defined]
    assert short.financing_usd == 0  # type: ignore[attr-defined]  # a credit never pays for a trade


def test_a_smallest_contract_over_the_limit_is_not_taken_and_the_stop_is_not_moved() -> None:
    refused = _size(CFD, "US500", Direction.LONG, "7500", "7280", "7600")
    assert not refused.approved and refused.quantity == 0  # type: ignore[attr-defined]
    assert refused.reasons == (RejectReason.RISK_LIMIT,)  # type: ignore[attr-defined]
    assert "smallest legal order 0.01" in refused.details[0]  # type: ignore[attr-defined]
    # with twice the equity the same stop fits
    assert _size(CFD, "US500", Direction.LONG, "7500", "7280", "7600", equity="200").approved  # type: ignore[attr-defined]


def test_the_policies_are_the_sleeves_and_never_lever_beyond_equity() -> None:
    for policy, risk, rules in ((CFD, "0.02", "capital-demo"), (CRYPTO, "0.01", "crypto-cfd")):
        assert policy.initial_trading_capital == 100
        assert policy.exposure.risk_per_trade_fraction == Decimal(risk)
        assert policy.exposure.risk_per_trade_fraction == load_rules(rules).risk_fraction
        assert policy.exposure.max_gross_exposure_fraction == 1
        # each trial ran one setup: its open-trade limit is the sleeve's limit per setup
        assert policy.exposure.max_open_positions == load_rules(rules).open_trades_per_setup
        assert not policy.live_enabled
    assert (CFD.quality.max_entry_spread_bps, CRYPTO.quality.max_entry_spread_bps) == (20, 60)
