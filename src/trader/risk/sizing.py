"""Exact position sizing (risk_and_validation.md §3; CRYPTO §6; COMMODITIES §5).

For quantity ``q`` (broker units), entry bound ``P``, stop ``S``, multiplier ``m`` (including
price scale), conservative FX ``x``, gap allowance ``g`` and round-trip fees ``F(q)``::

    planned_loss(q) = q*m*|P-S|*x + q*m*P*x*g + F(q)*x  <=  risk_per_trade_fraction * E

plus aggregate planned risk, per-symbol and per-concentration-tag notional, gross notional,
cash reserve ``max(0.60*E, USD 30)`` after reserved fees, broker minimums and a positive expected
net value **at the exact rounded size**. Every left-hand side is non-decreasing in ``q`` (fee
models are monotone), so the largest feasible step count is found by binary search. Quantities
round down, never up to reach a minimum; fees round up. Margin is never used as exposure.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from trader.accounting.fees import FeeSchedule
from trader.config.policy import RiskPolicy
from trader.domain.candidates import RejectReason, ReturnBasis
from trader.domain.instruments import AnySpec, CommodityInstrumentSpec, CryptoSpotSpec
from trader.domain.types import ZERO, Direction, DomainError, ProductType, Side


@dataclass(frozen=True, slots=True)
class ExposureState:
    """Portfolio-wide exposure across every track and account, in USD."""

    gross_usd: Decimal = ZERO
    by_instrument_usd: Mapping[str, Decimal] = field(default_factory=dict)
    by_tag_usd: Mapping[str, Decimal] = field(default_factory=dict)
    planned_risk_usd: Decimal = ZERO
    open_positions: int = 0
    pending_entries: int = 0


@dataclass(frozen=True, slots=True)
class SizingRequest:
    policy: RiskPolicy
    spec: AnySpec
    direction: Direction
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    fx_to_usd: Decimal
    fees: FeeSchedule
    gap_allowance_fraction: Decimal
    equity_usd: Decimal
    portfolio_spendable_usd: Decimal
    account_spendable_usd: Decimal
    exposure: ExposureState
    expected_gross_return: Decimal | None
    uncertainty_margin: Decimal
    basis: ReturnBasis
    half_spread_fraction: Decimal
    financing_nights: int = 0


@dataclass(frozen=True, slots=True)
class SizingResult:
    approved: bool
    quantity: Decimal
    notional_usd: Decimal
    planned_loss_usd: Decimal
    round_trip_fees_usd: Decimal
    spread_cost_usd: Decimal
    financing_usd: Decimal
    expected_gross_usd: Decimal
    expected_net_usd: Decimal
    cash_required_usd: Decimal
    reasons: tuple[RejectReason, ...]
    binding_constraint: str
    details: tuple[str, ...]

    @property
    def estimated_costs_usd(self) -> Decimal:
        return self.round_trip_fees_usd + self.spread_cost_usd + self.financing_usd


@dataclass(frozen=True, slots=True)
class _Evaluation:
    quantity: Decimal
    notional_usd: Decimal
    planned_loss_usd: Decimal
    fees_usd: Decimal
    cash_required_usd: Decimal
    failures: tuple[str, ...]


class Sizer:
    def __init__(self, request: SizingRequest) -> None:
        self.r = request
        spec = request.spec
        if request.entry_price <= 0 or request.stop_price <= 0 or request.fx_to_usd <= 0:
            raise DomainError("prices and FX must be positive")
        if request.direction is Direction.LONG and request.stop_price >= request.entry_price:
            raise DomainError("a long stop must be below the entry bound")
        if request.direction is Direction.SHORT and request.stop_price <= request.entry_price:
            raise DomainError("a short stop must be above the entry bound")
        self.units = spec.units_per_quantity()
        self.unit_notional_usd = self.units * request.entry_price * request.fx_to_usd
        policy = request.policy
        e = request.equity_usd
        self.risk_budget = policy.exposure.risk_per_trade_fraction * e
        self.aggregate_budget = policy.exposure.max_total_planned_risk_fraction * e
        self.symbol_cap = policy.exposure.max_symbol_exposure_fraction * e
        self.gross_cap = policy.exposure.max_gross_exposure_fraction * e
        self.reserve = policy.cash_reserve_usd(e)
        self.is_cfd = spec.identity.product_type is ProductType.CFD
        entry_side = Side.BUY if request.direction is Direction.LONG else Side.SELL
        self.entry_side = entry_side
        self.exit_side = Side.SELL if entry_side is Side.BUY else Side.BUY

    # ------------------------------------------------------------------ core maths
    def _fees_usd(self, q: Decimal, exit_price: Decimal) -> tuple[Decimal, Decimal]:
        r = self.r
        entry_notional_quote = q * self.units * r.entry_price
        exit_notional_quote = q * self.units * exit_price
        entry_fee = r.fees.fee(self.entry_side, q, entry_notional_quote)
        exit_fee = r.fees.fee(self.exit_side, q, exit_notional_quote)
        return entry_fee * r.fx_to_usd, exit_fee * r.fx_to_usd

    def evaluate(self, q: Decimal) -> _Evaluation:
        r = self.r
        spec = r.spec
        notional = q * self.unit_notional_usd
        price_risk = q * self.units * abs(r.entry_price - r.stop_price) * r.fx_to_usd
        gap = notional * r.gap_allowance_fraction
        # The loss exit's fee is bounded by the fee at the entry price (fees are monotone).
        entry_fee, exit_fee = self._fees_usd(q, r.entry_price)
        fees = entry_fee + exit_fee
        planned_loss = price_risk + gap + fees
        if self.is_cfd and isinstance(spec, CommodityInstrumentSpec) and spec.margin is not None:
            # Margin is collateral, not exposure; the cash check still keeps the margin plus the
            # planned loss outside the reserve (COMMODITIES §4).
            cash_required = notional * spec.margin.initial_margin_fraction + planned_loss
        else:
            cash_required = notional + entry_fee + exit_fee
        failures: list[str] = []
        if planned_loss > self.risk_budget:
            failures.append("per_trade_risk")
        if r.exposure.planned_risk_usd + planned_loss > self.aggregate_budget:
            failures.append("aggregate_risk")
        if r.exposure.by_instrument_usd.get(spec.instrument_id, ZERO) + notional > self.symbol_cap:
            failures.append("symbol_notional")
        for tag in spec.exposure_tags:
            if r.exposure.by_tag_usd.get(tag, ZERO) + notional > self.symbol_cap:
                failures.append(f"tag_notional:{tag}")
        if r.exposure.gross_usd + notional > self.gross_cap:
            failures.append("gross_notional")
        if r.portfolio_spendable_usd - cash_required < self.reserve:
            failures.append("cash_reserve")
        if r.account_spendable_usd - cash_required < 0:
            failures.append("account_funds")
        return _Evaluation(q, notional, planned_loss, fees, cash_required, tuple(failures))

    def max_steps(self) -> int:
        """Upper bound on step count from the linear notional and cash caps."""
        r = self.r
        spec = r.spec
        rooms = [
            self.symbol_cap - r.exposure.by_instrument_usd.get(spec.instrument_id, ZERO),
            self.gross_cap - r.exposure.gross_usd,
        ]
        rooms += [self.symbol_cap - r.exposure.by_tag_usd.get(t, ZERO) for t in spec.exposure_tags]
        if not self.is_cfd:
            # Fully paid routes spend the notional; margined routes are bounded by the
            # (non-linear but monotone) margin-plus-loss cash check inside ``evaluate``.
            rooms += [r.portfolio_spendable_usd - self.reserve, r.account_spendable_usd]
        room = min(rooms)
        if room <= 0:
            return 0
        q_max = room / self.unit_notional_usd
        if spec.max_quantity is not None:
            q_max = min(q_max, spec.max_quantity)
        return int((q_max / spec.quantity_step).to_integral_value(rounding=ROUND_FLOOR))

    def largest_feasible_steps(self) -> int:
        step = self.r.spec.quantity_step
        lo, hi = 0, self.max_steps()
        if hi == 0:
            return 0
        if not self.evaluate(hi * step).failures:
            return hi
        # invariant: ok(lo) and not ok(hi)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.evaluate(mid * step).failures:
                hi = mid
            else:
                lo = mid
        return lo

    # ------------------------------------------------------------------ decision
    def size(self) -> SizingResult:
        r = self.r
        spec = r.spec
        if not r.fees.known:
            return _reject(
                (RejectReason.UNKNOWN_COSTS,),
                "fee_schedule",
                (f"fee schedule {r.fees.schedule_id} is unknown: costs cannot be established",),
            )
        if r.expected_gross_return is None:
            return _reject((RejectReason.NO_EDGE,), "edge", ("no edge estimate available",))
        edge_after_margin = r.expected_gross_return - r.uncertainty_margin
        if edge_after_margin <= 0:
            return _reject(
                (RejectReason.NO_EDGE,),
                "edge",
                (
                    f"expected gross return {r.expected_gross_return} does not exceed the "
                    f"uncertainty margin {r.uncertainty_margin}",
                ),
            )

        steps = self.largest_feasible_steps()
        q = steps * spec.quantity_step
        minimum_problems = spec.minimum_violations(q, r.entry_price) if q > 0 else ["zero"]
        if q <= 0 or minimum_problems:
            return self._explain_infeasible_minimum()

        if isinstance(spec, CryptoSpotSpec):
            sellable = spec.sellable_after_fees(q, ZERO)
            if sellable < spec.min_quantity:
                return _reject(
                    (RejectReason.SIZE_BELOW_MINIMUM,),
                    "exit_minimum",
                    (f"quantity {q} could not be sold back above the exit minimum",),
                )

        ev = self.evaluate(q)
        target = r.target_price if r.target_price is not None else r.entry_price
        favourable_exit = (
            max(target, r.entry_price)
            if r.direction is Direction.LONG
            else min(target, r.entry_price)
        )
        entry_fee, exit_fee = self._fees_usd(q, favourable_exit)
        fees_for_ev = max(entry_fee + exit_fee, ev.fees_usd)
        spread_cost = (
            ev.notional_usd * 2 * r.half_spread_fraction
            if r.basis is ReturnBasis.MID_TO_MID
            else ZERO
        )
        financing = self._financing_usd(ev.notional_usd)
        expected_gross = ev.notional_usd * r.expected_gross_return
        margin_cost = ev.notional_usd * r.uncertainty_margin
        expected_net = expected_gross - fees_for_ev - spread_cost - financing - margin_cost
        binding = self._binding_at(steps + 1)
        if expected_net <= 0:
            return SizingResult(
                approved=False,
                quantity=q,
                notional_usd=ev.notional_usd,
                planned_loss_usd=ev.planned_loss_usd,
                round_trip_fees_usd=fees_for_ev,
                spread_cost_usd=spread_cost,
                financing_usd=financing,
                expected_gross_usd=expected_gross,
                expected_net_usd=expected_net,
                cash_required_usd=ev.cash_required_usd,
                reasons=(RejectReason.TOO_COSTLY,),
                binding_constraint=binding,
                details=(
                    f"at the largest permitted size {q} (notional {ev.notional_usd:.4f} USD) "
                    f"expected gross {expected_gross:.4f} minus costs "
                    f"{fees_for_ev + spread_cost + financing:.4f} and uncertainty margin "
                    f"{margin_cost:.4f} is {expected_net:.4f} USD <= 0",
                ),
            )
        return SizingResult(
            approved=True,
            quantity=q,
            notional_usd=ev.notional_usd,
            planned_loss_usd=ev.planned_loss_usd,
            round_trip_fees_usd=fees_for_ev,
            spread_cost_usd=spread_cost,
            financing_usd=financing,
            expected_gross_usd=expected_gross,
            expected_net_usd=expected_net,
            cash_required_usd=ev.cash_required_usd,
            reasons=(),
            binding_constraint=binding,
            details=(),
        )

    def _financing_usd(self, notional_usd: Decimal) -> Decimal:
        spec = self.r.spec
        if not isinstance(spec, CommodityInstrumentSpec) or spec.financing is None:
            return ZERO
        nights = self.r.financing_nights
        if nights <= 0:
            return ZERO
        rate = (
            spec.financing.long_daily_charge_fraction
            if self.r.direction is Direction.LONG
            else spec.financing.short_daily_charge_fraction
        )
        return max(rate, ZERO) * notional_usd * nights

    def _binding_at(self, steps: int) -> str:
        spec = self.r.spec
        if spec.max_quantity is not None and steps * spec.quantity_step > spec.max_quantity:
            return "max_quantity"
        failures = self.evaluate(steps * spec.quantity_step).failures
        return ",".join(failures) if failures else "linear_cap"

    def _explain_infeasible_minimum(self) -> SizingResult:
        r = self.r
        spec = r.spec
        q_min = max(spec.min_quantity, spec.quantity_step)
        if spec.min_notional > 0:
            needed = spec.min_notional / (self.units * r.entry_price)
            steps = (needed / spec.quantity_step).to_integral_value(rounding=ROUND_CEILING)
            q_min = max(q_min, steps * spec.quantity_step)
        ev = self.evaluate(q_min)
        details = [
            f"smallest legal order {q_min} (notional {ev.notional_usd:.4f} USD) has planned loss "
            f"{ev.planned_loss_usd:.4f} USD against a per-trade budget of {self.risk_budget:.4f} USD",
        ]
        reasons: list[RejectReason] = []
        fails = set(ev.failures)
        if fails & {"per_trade_risk", "aggregate_risk"}:
            if ev.fees_usd >= self.risk_budget:
                reasons.append(RejectReason.TOO_COSTLY)
                details.append(
                    f"round-trip fees alone ({ev.fees_usd:.4f} USD) exceed the risk budget"
                )
            else:
                reasons.append(RejectReason.RISK_LIMIT)
        if any(f.endswith("notional") or f.startswith("tag_notional") for f in fails):
            reasons.append(RejectReason.RISK_LIMIT)
            details.append(f"notional limits violated at minimum size: {sorted(fails)}")
        if fails & {"cash_reserve", "account_funds"}:
            reasons.append(RejectReason.INSUFFICIENT_FUNDS)
        if not reasons:
            reasons.append(RejectReason.SIZE_BELOW_MINIMUM)
        unique = tuple(dict.fromkeys(reasons))
        return SizingResult(
            approved=False,
            quantity=ZERO,
            notional_usd=ZERO,
            planned_loss_usd=ev.planned_loss_usd,
            round_trip_fees_usd=ev.fees_usd,
            spread_cost_usd=ZERO,
            financing_usd=ZERO,
            expected_gross_usd=ZERO,
            expected_net_usd=ZERO,
            cash_required_usd=ev.cash_required_usd,
            reasons=unique,
            binding_constraint=",".join(ev.failures) or "broker_minimum",
            details=tuple(details),
        )


def _reject(
    reasons: tuple[RejectReason, ...], binding: str, details: tuple[str, ...]
) -> SizingResult:
    return SizingResult(
        approved=False,
        quantity=ZERO,
        notional_usd=ZERO,
        planned_loss_usd=ZERO,
        round_trip_fees_usd=ZERO,
        spread_cost_usd=ZERO,
        financing_usd=ZERO,
        expected_gross_usd=ZERO,
        expected_net_usd=ZERO,
        cash_required_usd=ZERO,
        reasons=reasons,
        binding_constraint=binding,
        details=details,
    )


def size_order(request: SizingRequest) -> SizingResult:
    return Sizer(request).size()
