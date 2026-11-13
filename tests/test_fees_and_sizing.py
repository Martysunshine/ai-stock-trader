"""Fee schedules and exact sizing (risk_and_validation.md §3; strategy_and_profitability.md §2)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trader.accounting.fees import UnknownFeesError, load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import LoadedPolicy
from trader.domain.candidates import RejectReason, ReturnBasis
from trader.domain.types import Direction, Side
from trader.risk.sizing import ExposureState, Sizer, SizingRequest, size_order

ROOT = Path(__file__).resolve().parents[1]
FEES = ROOT / "configs" / "fees"
SYNA = load_instruments(ROOT / "configs/instruments/synthetic_equities.yaml")["XNYS:SYNA"]


def request(policy: LoadedPolicy, **overrides: object) -> SizingRequest:
    base = SizingRequest(
        policy=policy.policy,
        spec=SYNA,
        direction=Direction.LONG,
        entry_price=Decimal("101.29"),
        stop_price=Decimal("100.93"),
        target_price=Decimal("101.77"),
        fx_to_usd=Decimal(1),
        fees=load_fee_schedule(FEES / "replay_demo.yaml"),
        gap_allowance_fraction=Decimal("0.001"),
        equity_usd=Decimal(50),
        portfolio_spendable_usd=Decimal(50),
        account_spendable_usd=Decimal(50),
        exposure=ExposureState(),
        expected_gross_return=Decimal("0.006"),
        uncertainty_margin=Decimal("0.0015"),
        basis=ReturnBasis.MID_TO_MID,
        half_spread_fraction=Decimal("0.0002"),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- fees


def test_documented_fee_examples() -> None:
    ibkr = load_fee_schedule(FEES / "ibkr_us_fractional.yaml")
    assert ibkr.fee(Side.BUY, Decimal("0.05"), Decimal("5")) == Decimal(
        "0.05"
    )  # IBKR footnote example
    assert ibkr.fee(Side.BUY, Decimal("0.001"), Decimal("0.10")) == Decimal(
        "0.01"
    )  # minimum applies
    kraken = load_fee_schedule(FEES / "kraken_spot_tier1.yaml")
    assert kraken.round_trip(Decimal(1), Decimal(10), Decimal(10)) == Decimal("0.16")  # CRYPTO §6
    assert kraken.round_trip(Decimal(1), Decimal(5), Decimal(5)) == Decimal("0.08")


def test_unknown_fee_schedule_never_means_zero() -> None:
    alpaca = load_fee_schedule(FEES / "alpaca_us_equities.yaml")
    assert not alpaca.known
    with pytest.raises(UnknownFeesError):
        alpaca.fee(Side.BUY, Decimal(1), Decimal(10))


@given(q1=st.decimals("0.000001", "5", places=6), q2=st.decimals("0.000001", "5", places=6))
@settings(max_examples=150, deadline=None)
def test_fee_models_are_monotone_in_quantity(q1: Decimal, q2: Decimal) -> None:
    lo, hi = sorted((q1, q2))
    for name in ("replay_demo.yaml", "ibkr_us_fractional.yaml", "kraken_spot_tier1.yaml"):
        sched = load_fee_schedule(FEES / name)
        for side in Side:
            assert sched.fee(side, lo, lo * 100) <= sched.fee(side, hi, hi * 100)


# ----------------------------------------------------------------------------- sizing


def test_section3_example_five_dollars_at_2_5_percent_is_infeasible(policy: LoadedPolicy) -> None:
    """A USD 5 position with a 2.5% stop already uses the whole USD 0.125 budget before costs."""
    sizer = Sizer(request(policy, entry_price=Decimal(100), stop_price=Decimal("97.5")))
    five_dollars = sizer.evaluate(Decimal("0.05"))
    assert "per_trade_risk" in five_dollars.failures
    result = size_order(request(policy, entry_price=Decimal(100), stop_price=Decimal("97.5")))
    assert result.notional_usd < 5  # a smaller position, or
    assert not result.approved  # no trade, once costs are charged
    assert result.planned_loss_usd <= Decimal("0.125")


def test_demo_sizing_respects_every_usd50_limit(policy: LoadedPolicy) -> None:
    r = size_order(request(policy))
    assert r.approved
    assert r.notional_usd <= Decimal(10)  # symbol cap 20%
    assert r.planned_loss_usd <= Decimal("0.125")
    assert Decimal(50) - r.cash_required_usd >= Decimal(30)  # reserve
    assert r.quantity == r.quantity.quantize(Decimal("0.000000001"))
    assert r.expected_net_usd > 0


def test_commission_minimum_exceeding_budget_rejects_before_submission(
    policy: LoadedPolicy,
) -> None:
    r = size_order(request(policy, fees=load_fee_schedule(FEES / "ibkr_us_fractional.yaml")))
    assert not r.approved
    assert r.reasons == (RejectReason.TOO_COSTLY,)


def test_unknown_costs_reject(policy: LoadedPolicy) -> None:
    r = size_order(request(policy, fees=load_fee_schedule(FEES / "alpaca_us_equities.yaml")))
    assert r.reasons == (RejectReason.UNKNOWN_COSTS,)


def test_edge_below_uncertainty_margin_is_no_edge(policy: LoadedPolicy) -> None:
    r = size_order(
        request(policy, expected_gross_return=Decimal("0.001"), uncertainty_margin=Decimal("0.001"))
    )
    assert r.reasons == (RejectReason.NO_EDGE,)


def test_size_never_depends_on_model_confidence(policy: LoadedPolicy) -> None:
    modest = size_order(request(policy))
    enormous = size_order(request(policy, expected_gross_return=Decimal("1000000")))
    assert modest.quantity == enormous.quantity


def test_large_paper_balance_still_sizes_against_usd50(policy: LoadedPolicy) -> None:
    """Paper account shows USD 100,000: the internal USD 50 budget still governs (§9 last row)."""
    r = size_order(
        request(
            policy, portfolio_spendable_usd=Decimal(100000), account_spendable_usd=Decimal(100000)
        )
    )
    assert r.notional_usd <= Decimal(10)


def test_reserve_and_pending_exposure_bind(policy: LoadedPolicy) -> None:
    pending = ExposureState(
        gross_usd=Decimal(15),
        by_instrument_usd={},
        planned_risk_usd=Decimal("0.2"),
        pending_entries=1,
    )
    r = size_order(request(policy, exposure=pending))
    assert r.notional_usd <= Decimal(5) or not r.approved  # gross 40% = 20 incl. pending
    poor = size_order(
        request(policy, portfolio_spendable_usd=Decimal(31), account_spendable_usd=Decimal(31))
    )
    assert not poor.approved
    assert (
        RejectReason.INSUFFICIENT_FUNDS in poor.reasons
        or RejectReason.SIZE_BELOW_MINIMUM in poor.reasons
    )


def test_broker_minimum_is_never_reached_by_rounding_up(policy: LoadedPolicy) -> None:
    spec = SYNA.model_copy(update={"min_notional": Decimal("25")})
    r = size_order(request(policy, spec=spec))
    assert not r.approved
    assert r.quantity == 0  # never rounded up to qualify


@given(
    stop_bps=st.integers(min_value=5, max_value=500),
    price=st.decimals("1", "500", places=2),
    gross=st.decimals("0", "15", places=2),
    planned=st.decimals("0", "0.3", places=3),
    spend=st.decimals("20", "60", places=2),
    fee_file=st.sampled_from(["replay_demo.yaml", "ibkr_us_fractional.yaml"]),
)
@settings(max_examples=120, deadline=None)
def test_binary_search_equals_brute_force_and_is_maximal(
    policy: LoadedPolicy,
    stop_bps: int,
    price: Decimal,
    gross: Decimal,
    planned: Decimal,
    spend: Decimal,
    fee_file: str,
) -> None:
    spec = SYNA.model_copy(
        update={"quantity_step": Decimal("0.01"), "min_quantity": Decimal("0.01")}
    )
    req = request(
        policy,
        spec=spec,
        entry_price=price,
        stop_price=price * (1 - Decimal(stop_bps) / 10000),
        exposure=ExposureState(gross_usd=gross, planned_risk_usd=planned),
        portfolio_spendable_usd=spend,
        account_spendable_usd=spend,
        fees=load_fee_schedule(FEES / fee_file),
    )
    sizer = Sizer(req)
    k_hi = sizer.max_steps()
    feasible = [
        k for k in range(min(k_hi, 4000) + 1) if not sizer.evaluate(k * Decimal("0.01")).failures
    ]
    best = sizer.largest_feasible_steps()
    if k_hi <= 4000:
        assert best == (max(feasible) if feasible else 0)
    if best > 0:
        assert not sizer.evaluate(best * Decimal("0.01")).failures
        ev = sizer.evaluate(best * Decimal("0.01"))
        assert ev.planned_loss_usd <= Decimal("0.125")
        assert gross + ev.notional_usd <= Decimal(20)
    if best < k_hi:
        assert sizer.evaluate((best + 1) * Decimal("0.01")).failures
