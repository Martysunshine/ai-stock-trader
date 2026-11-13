"""The deterministic risk engine: the only component that can approve new exposure.

``evaluate_entry`` is a pure function of its inputs. It fails closed (instructions.md §5) on
pauses, unknown capabilities, stale data, excessive spreads, missing exit plans, unknown costs
and non-positive conservative net edge, then sizes exactly (``trader.risk.sizing``). Model
output can only *reduce* activity: size never depends on the edge estimate, and a model cannot
raise limits, change permissions or bypass any check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from trader.accounting.fees import FeeSchedule
from trader.config.policy import LoadedPolicy
from trader.config.track_profiles import TrackProfile
from trader.domain.account import RuleProfileKind
from trader.domain.candidates import EdgeEvidence, RejectReason, SignalScore, TradeCandidate
from trader.domain.capabilities import (
    FRACTIONAL_LIMIT_DAY,
    FRACTIONAL_MARKET_DAY,
    NATIVE_PROTECTIVE_STOP,
    BrokerCapabilities,
)
from trader.domain.certification import (
    CERTIFICATION_MAX_ORDER_NOTIONAL_USD,
    CertificationGrant,
)
from trader.domain.instruments import AnySpec
from trader.domain.market import Quote
from trader.domain.orders import OrderType
from trader.domain.types import (
    BPS,
    ZERO,
    Direction,
    Mode,
    ProductType,
    Side,
    Track,
    round_down_to_step,
)
from trader.ops.pause import PauseFlag
from trader.risk.account_rules import entry_rule_problems
from trader.risk.sizing import ExposureState, SizingRequest, SizingResult, size_order

#: Evidence each mode accepts for an edge estimate. Synthetic fixtures are replay-only; live
#: requires a validated, frozen bundle (strategy_and_profitability.md §9). CERTIFICATION is in
#: no set: :func:`evaluate_entry` never accepts it, in any mode. PRACTICE (owner practice mode,
#: ADR-0040) is paper and replay only: never shadow, never live.
ACCEPTED_EVIDENCE: dict[Mode, frozenset[EdgeEvidence]] = {
    Mode.REPLAY: frozenset(
        {
            EdgeEvidence.SYNTHETIC_FIXTURE,
            EdgeEvidence.BACKTEST_ONLY,
            EdgeEvidence.VALIDATED_BUNDLE,
            EdgeEvidence.PRACTICE,
        }
    ),
    Mode.SHADOW: frozenset({EdgeEvidence.BACKTEST_ONLY, EdgeEvidence.VALIDATED_BUNDLE}),
    Mode.PAPER: frozenset(
        {EdgeEvidence.BACKTEST_ONLY, EdgeEvidence.VALIDATED_BUNDLE, EdgeEvidence.PRACTICE}
    ),
    Mode.LIVE: frozenset({EdgeEvidence.VALIDATED_BUNDLE}),
}
#: Evidence the certification path (:func:`evaluate_certification_order`) accepts, per mode:
#: only paper, and there only together with a :class:`CertificationGrant` for the run.
CERTIFICATION_EVIDENCE: dict[Mode, frozenset[EdgeEvidence]] = {
    Mode.REPLAY: frozenset(),
    Mode.SHADOW: frozenset(),
    Mode.PAPER: frozenset({EdgeEvidence.CERTIFICATION}),
    Mode.LIVE: frozenset(),
}
#: A certification market buy is bounded as quantity x ask x (1 + this buffer).
CERTIFICATION_MARKET_PRICE_BUFFER = Decimal("0.05")
#: A certification limit buy must rest at least this far below the bid (it must never fill).
CERTIFICATION_MIN_PASSIVE_DISCOUNT = Decimal("0.10")
#: Spread limit for a certification market buy. The ordinary entry limit (policy, 10 bps) prices
#: a strategy's edge; a USD 1-2 demonstration order is bounded by the notional cap instead.
CERTIFICATION_MAX_SPREAD_BPS = Decimal("50")
#: Unknown pass-through fees of one tiny order are reserved as this allowance.
CERTIFICATION_FEE_ALLOWANCE_USD = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class EntryContext:
    mode: Mode
    now: datetime
    policy: LoadedPolicy
    track_profile: TrackProfile
    spec: AnySpec
    capabilities: BrokerCapabilities
    fees: FeeSchedule
    candidate: TradeCandidate
    score: SignalScore | None
    quote: Quote | None
    fx_to_usd: Decimal | None
    effective_equity_usd: Decimal
    portfolio_spendable_usd: Decimal
    account_spendable_usd: Decimal
    exposure: ExposureState
    active_flags: tuple[PauseFlag, ...]
    entries_in_session: int
    in_entry_window: bool
    halted: bool
    rule_profile: RuleProfileKind
    day_trades_remaining: int | None = None
    account_snapshot_age: timedelta | None = None
    clock_skew: timedelta = timedelta(0)
    unknown_orders_outstanding: bool = False
    instrument_busy: bool = False
    financing_nights: int = 0
    #: Full-market spread estimate in bps when ``quote`` is one venue's book (ADR-0061).
    market_spread_bps: Decimal | None = None


#: A one-venue quote wider than this is not trusted even with a full-market estimate: the
#: estimate is 16 minutes old and the venue may be showing a real disruption (ADR-0061). IEX
#: books of liquid stocks were seen up to 291 bps wide while the market was 5 bps; the entry
#: limit (venue ask plus offset) and the sizing on that limit bound the price paid below this.
VENUE_SPREAD_CEILING_BPS = Decimal("500")


def entry_spread_bps(ctx: EntryContext) -> Decimal:
    """The spread the entry checks and costs use.

    A one-venue book is never tighter than the consolidated one, so with a full-market estimate
    the smaller of the two is used (ADR-0061); otherwise the quote's own spread.
    """
    if ctx.quote is None:
        raise ValueError("no quote")
    own = ctx.quote.spread_bps
    if ctx.market_spread_bps is None:
        return own
    return min(own, ctx.market_spread_bps)


@dataclass(frozen=True, slots=True)
class EntryDecision:
    approved: bool
    reasons: tuple[RejectReason, ...]
    details: tuple[str, ...]
    limit_price: Decimal | None = None
    sizing: SizingResult | None = None
    model_hash: str = "none"
    evidence: EdgeEvidence = EdgeEvidence.NONE
    checks: tuple[str, ...] = field(default_factory=tuple)


def required_entry_capability(spec: AnySpec) -> str:
    """The broker capability an entry order for this instrument depends on.

    An ETF share is ordered exactly like an ordinary share (same order types, fractional rules
    and certification evidence), so it needs the same capability. Whether an ETF may be traded
    at all is decided earlier, by the track profile's product list.
    """
    product = spec.identity.product_type
    if product in (ProductType.ORDINARY_EQUITY, ProductType.ETF):
        return FRACTIONAL_LIMIT_DAY if spec.fractionable else "whole.limit.day"
    if product is ProductType.CRYPTO_SPOT:
        return "spot.limit"
    if product is ProductType.CFD:
        return "cfd.open_position"
    return "etc.limit.day"


def _entry_limit_price(ctx: EntryContext) -> Decimal:
    """Bounded marketable buy limit: ask plus the frozen offset, rounded down to the tick."""
    if ctx.quote is None:
        raise ValueError("no quote to bound the entry price")
    raw = ctx.quote.ask * (1 + ctx.candidate.entry_limit_offset_bps * BPS)
    return round_down_to_step(raw, ctx.spec.price_tick)


def evaluate_entry(ctx: EntryContext) -> EntryDecision:
    reasons: list[RejectReason] = []
    details: list[str] = []
    policy = ctx.policy.policy
    cand = ctx.candidate

    def reject(reason: RejectReason, detail: str) -> None:
        if reason not in reasons:
            reasons.append(reason)
        details.append(detail)

    # 1. pauses and unresolved state -----------------------------------------------------
    if ctx.active_flags:
        causes = sorted({f.cause.value for f in ctx.active_flags})
        reject(RejectReason.PAUSED, f"entries paused: {', '.join(causes)}")
    if ctx.unknown_orders_outstanding and policy.execution.unknown_order_blocks_new_entries:
        reject(RejectReason.PAUSED, "an order with unknown outcome is outstanding")

    # 2. mode, track, product and direction ----------------------------------------------
    profile = ctx.track_profile
    if policy.mode is not ctx.mode:
        reject(RejectReason.MODE_NOT_PERMITTED, "policy mode does not match runtime mode")
    if not profile.enabled:
        reject(RejectReason.MODE_NOT_PERMITTED, f"track {profile.track} is disabled")
    if ctx.mode is Mode.LIVE and not policy.live_enabled:
        reject(RejectReason.MODE_NOT_PERMITTED, "live trading is not enabled by policy")
    if ctx.spec.identity.track is not profile.track or cand.track is not profile.track:
        reject(RejectReason.FORBIDDEN_PRODUCT, "instrument/candidate track mismatch")
    if ctx.spec.identity.product_type not in profile.allowed_product_types:
        reject(RejectReason.FORBIDDEN_PRODUCT, f"{ctx.spec.identity.product_type} not allowed")
    allowed_dirs = profile.live_directions if ctx.mode is Mode.LIVE else profile.replay_directions
    if cand.direction not in allowed_dirs or cand.direction not in ctx.spec.allowed_directions:
        reject(RejectReason.FORBIDDEN_PRODUCT, f"direction {cand.direction} not permitted")
    if cand.direction is Direction.SHORT:
        reject(RejectReason.FORBIDDEN_PRODUCT, "short entries are not implemented in this pass")

    # 3. capabilities and account rules --------------------------------------------------
    cert = ctx.spec.certify()
    if not cert.certified:
        reject(RejectReason.CAPABILITY, "instrument not certified: " + "; ".join(cert.problems))
    needed = required_entry_capability(ctx.spec)
    if not ctx.capabilities.usable(needed):
        reject(RejectReason.CAPABILITY, f"capability {needed} is not usable (unknown = disabled)")
    if (
        ctx.mode is Mode.LIVE
        and policy.execution.native_protection_required_for_live
        and not ctx.capabilities.live_confirmed(NATIVE_PROTECTIVE_STOP)
    ):
        reject(RejectReason.NO_EXIT_PLAN, "live requires certified broker-held protection")
    for problem in entry_rule_problems(
        ctx.rule_profile,
        mode=ctx.mode,
        capabilities=ctx.capabilities,
        day_trades_remaining=ctx.day_trades_remaining,
        plans_same_day_exit=True,
    ):
        reject(RejectReason.ACCOUNT_RULE, problem)

    # 4. timing --------------------------------------------------------------------------
    if not ctx.in_entry_window:
        reject(RejectReason.OUTSIDE_ENTRY_WINDOW, "outside the calendar-relative entry window")
    if ctx.now >= cand.expires_at:
        reject(RejectReason.STALE_DATA, "candidate expired")
    if ctx.now < cand.decision_time:
        reject(RejectReason.STALE_DATA, "candidate not yet available (look-ahead guard)")

    # 5. data quality --------------------------------------------------------------------
    q = policy.quality
    if ctx.halted:
        reject(RejectReason.STALE_DATA, "instrument is halted")
    if ctx.quote is None:
        reject(RejectReason.STALE_DATA, "no quote")
    else:
        age = ctx.now - ctx.quote.event_time
        if age > timedelta(seconds=q.max_quote_age_seconds) or age < timedelta(0):
            reject(RejectReason.STALE_DATA, f"quote age {age.total_seconds():.3f}s exceeds limit")
        if q.require_feed_parity and ctx.quote.feed != cand.feed:
            reject(
                RejectReason.FEED_MISMATCH,
                f"quote feed {ctx.quote.feed} != signal feed {cand.feed}",
            )
        spread = entry_spread_bps(ctx)
        if spread > q.max_entry_spread_bps:
            if ctx.market_spread_bps is None:
                detail = f"spread {spread:.2f} bps > {q.max_entry_spread_bps} bps"
            else:
                detail = (
                    f"market spread {spread:.2f} bps > {q.max_entry_spread_bps} bps "
                    f"(venue quote {ctx.quote.spread_bps:.2f} bps)"
                )
            reject(RejectReason.SPREAD_TOO_WIDE, detail)
        elif ctx.market_spread_bps is not None and ctx.quote.spread_bps > VENUE_SPREAD_CEILING_BPS:
            reject(
                RejectReason.SPREAD_TOO_WIDE,
                f"venue quote spread {ctx.quote.spread_bps:.2f} bps > "
                f"{VENUE_SPREAD_CEILING_BPS} bps ceiling",
            )
    if ctx.fx_to_usd is None:
        reject(RejectReason.STALE_DATA, "no fresh FX conversion")
    if ctx.account_snapshot_age is not None and ctx.account_snapshot_age > timedelta(
        seconds=q.max_account_snapshot_age_seconds
    ):
        reject(RejectReason.STALE_DATA, "account snapshot is stale")
    if abs(ctx.clock_skew) > timedelta(milliseconds=q.max_clock_skew_milliseconds):
        reject(RejectReason.STALE_DATA, "clock skew exceeds limit")

    # 6. frequency and position limits ---------------------------------------------------
    x = policy.exposure
    if ctx.entries_in_session >= x.max_new_entries_per_session:
        reject(RejectReason.ENTRY_LIMIT_REACHED, "max new entries per session reached")
    if ctx.exposure.pending_entries >= x.max_pending_entry_orders:
        reject(RejectReason.ENTRY_LIMIT_REACHED, "a pending entry already exists")
    if ctx.exposure.open_positions + ctx.exposure.pending_entries >= policy.max_open_positions_for(
        ctx.mode
    ):
        reject(RejectReason.ENTRY_LIMIT_REACHED, "max open positions reached")

    if ctx.instrument_busy:
        reject(
            RejectReason.RISK_LIMIT, "a position or pending entry already exists in this instrument"
        )

    # 7. edge evidence ---------------------------------------------------------------------
    score = ctx.score
    model_hash = score.model_hash if score else "none"
    evidence = score.evidence if score else EdgeEvidence.NONE
    if score is None:
        reject(RejectReason.NO_EDGE, "no edge estimate: rule-only candidates do not trade")
    elif score.candidate_id != cand.candidate_id:
        reject(RejectReason.NO_EDGE, "score does not belong to this candidate")
    elif score.evidence not in ACCEPTED_EVIDENCE[ctx.mode]:
        reject(
            RejectReason.NO_EDGE, f"{score.evidence} evidence is not accepted in {ctx.mode.value}"
        )

    if reasons:
        return EntryDecision(
            approved=False,
            reasons=tuple(reasons),
            details=tuple(details),
            model_hash=model_hash,
            evidence=evidence,
        )

    # 8. exact sizing and conservative net edge -------------------------------------------
    if ctx.quote is None or ctx.fx_to_usd is None or score is None:  # pragma: no cover
        raise RuntimeError("pre-checks passed without quote, FX or score")
    limit_price = _entry_limit_price(ctx)
    if limit_price <= cand.stop_price:
        return EntryDecision(
            approved=False,
            reasons=(RejectReason.NO_EXIT_PLAN,),
            details=("entry bound is at or below the planned stop",),
            model_hash=model_hash,
            evidence=evidence,
        )
    gap = max(policy.gap_allowance_floor_fraction, cand.gap_allowance_fraction)
    sizing = size_order(
        SizingRequest(
            policy=policy,
            spec=ctx.spec,
            direction=cand.direction,
            entry_price=limit_price,
            stop_price=cand.stop_price,
            target_price=cand.target_price,
            fx_to_usd=ctx.fx_to_usd,
            fees=ctx.fees,
            gap_allowance_fraction=gap,
            equity_usd=ctx.effective_equity_usd,
            portfolio_spendable_usd=ctx.portfolio_spendable_usd,
            account_spendable_usd=ctx.account_spendable_usd,
            exposure=ctx.exposure,
            expected_gross_return=score.expected_gross_return,
            uncertainty_margin=score.uncertainty_margin,
            basis=score.basis,
            half_spread_fraction=(
                ctx.quote.half_spread_fraction
                if entry_spread_bps(ctx) == ctx.quote.spread_bps
                else entry_spread_bps(ctx) / 2 * BPS
            ),
            financing_nights=ctx.financing_nights,
        )
    )
    if not sizing.approved:
        return EntryDecision(
            approved=False,
            reasons=sizing.reasons,
            details=sizing.details,
            limit_price=limit_price,
            sizing=sizing,
            model_hash=model_hash,
            evidence=evidence,
        )
    if policy.quality.require_positive_conservative_net_edge and sizing.expected_net_usd <= ZERO:
        return EntryDecision(
            approved=False,
            reasons=(RejectReason.TOO_COSTLY,),
            details=("conservative net edge is not positive",),
            limit_price=limit_price,
            sizing=sizing,
            model_hash=model_hash,
            evidence=evidence,
        )
    return EntryDecision(
        approved=True,
        reasons=(),
        details=(),
        limit_price=limit_price,
        sizing=sizing,
        model_hash=model_hash,
        evidence=evidence,
    )


# --------------------------------------------------------------------------- certification


@dataclass(frozen=True, slots=True)
class CertificationOrderContext:
    """One proposed ``trader paper certify`` buy (domain/certification.py)."""

    mode: Mode
    now: datetime
    policy: LoadedPolicy
    grant: CertificationGrant | None
    evidence: EdgeEvidence
    spec: AnySpec
    capabilities: BrokerCapabilities
    rule_profile: RuleProfileKind
    side: Side
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    quote: Quote | None
    fx_to_usd: Decimal | None
    market_open: bool
    active_flags: tuple[PauseFlag, ...]
    unknown_orders_outstanding: bool
    instrument_busy: bool
    pending_entries: int
    orders_used: int
    account_spendable_usd: Decimal


@dataclass(frozen=True, slots=True)
class CertificationDecision:
    approved: bool
    reasons: tuple[RejectReason, ...]
    details: tuple[str, ...]
    capability: str
    #: Most the order can spend (limit: quantity x limit; market: quantity x ask x buffer), USD.
    notional_bound_usd: Decimal = ZERO
    fee_allowance_usd: Decimal = CERTIFICATION_FEE_ALLOWANCE_USD


def certification_capability(spec: AnySpec, order_type: OrderType) -> str:
    """The capability a certification buy of this shape exercises."""
    kind = "limit" if order_type is OrderType.LIMIT else "market"
    if spec.fractionable:
        return FRACTIONAL_LIMIT_DAY if order_type is OrderType.LIMIT else FRACTIONAL_MARKET_DAY
    return f"whole.{kind}.day"


def certification_notional_bound(
    order_type: OrderType, quantity: Decimal, limit_price: Decimal | None, quote: Quote | None
) -> Decimal | None:
    """Most a certification buy can spend in quote currency (``None`` when not computable)."""
    if order_type is OrderType.LIMIT:
        return None if limit_price is None else quantity * limit_price
    if quote is None:
        return None
    return quantity * quote.ask * (1 + CERTIFICATION_MARKET_PRICE_BUFFER)


def evaluate_certification_order(ctx: CertificationOrderContext) -> CertificationDecision:
    """Approve or refuse one certification buy; a pure function of its inputs (fails closed).

    ``EdgeEvidence.CERTIFICATION`` is accepted only in paper mode, only with an active grant for
    the run, and only for a buy whose notional bound is at most the grant's cap, itself at most
    USD 5 (:data:`~trader.domain.certification.CERTIFICATION_MAX_ORDER_NOTIONAL_USD`). A capability
    the profile does not make usable is allowed only when the grant lists it as under test.
    Limit buys must rest far below the bid; market buys need the market open and a fresh quote.
    The round trip's sell is an ordinary reducing exit (:func:`approve_reducing_exit`).
    """
    reasons: list[RejectReason] = []
    details: list[str] = []

    def reject(reason: RejectReason, detail: str) -> None:
        if reason not in reasons:
            reasons.append(reason)
        details.append(detail)

    spec, grant, quote = ctx.spec, ctx.grant, ctx.quote
    capability = certification_capability(spec, ctx.order_type)

    # 1. mode, grant and evidence -------------------------------------------------------
    if ctx.evidence not in CERTIFICATION_EVIDENCE[ctx.mode]:
        reject(
            RejectReason.MODE_NOT_PERMITTED,
            f"{ctx.evidence} evidence is not accepted by the certification path in "
            f"{ctx.mode.value}",
        )
    if ctx.policy.policy.mode is not ctx.mode:
        reject(RejectReason.MODE_NOT_PERMITTED, "policy mode does not match runtime mode")
    if grant is None:
        reject(
            RejectReason.MODE_NOT_PERMITTED,
            "no certification grant: only `trader paper certify` places certification orders",
        )
    else:
        if grant.mode is not ctx.mode:
            reject(RejectReason.MODE_NOT_PERMITTED, "the grant is for another mode")
        if not grant.is_active(ctx.now):
            reject(RejectReason.MODE_NOT_PERMITTED, "the certification grant has expired")
        if ctx.orders_used >= grant.max_orders:
            reject(
                RejectReason.ENTRY_LIMIT_REACHED,
                f"certification order budget used ({ctx.orders_used}/{grant.max_orders})",
            )

    # 2. pauses and unresolved state -----------------------------------------------------
    if ctx.active_flags:
        causes = sorted({f.cause.value for f in ctx.active_flags})
        reject(RejectReason.PAUSED, f"entries paused: {', '.join(causes)}")
    if ctx.unknown_orders_outstanding:
        reject(RejectReason.PAUSED, "an order with unknown outcome is outstanding")
    if ctx.pending_entries > 0:
        reject(RejectReason.ENTRY_LIMIT_REACHED, "an entry is already pending")
    if ctx.instrument_busy:
        reject(RejectReason.RISK_LIMIT, "a position or pending entry exists in this instrument")

    # 3. product, shape and capability ---------------------------------------------------
    if ctx.side is not Side.BUY:
        reject(RejectReason.FORBIDDEN_PRODUCT, "certification orders are buys (sells are exits)")
    if (
        spec.identity.product_type is not ProductType.ORDINARY_EQUITY
        or spec.identity.track is not Track.STOCKS
    ):
        reject(RejectReason.FORBIDDEN_PRODUCT, "certification covers ordinary US equities only")
    cert = spec.certify()
    if not cert.certified:
        reject(RejectReason.CAPABILITY, "instrument not certified: " + "; ".join(cert.problems))
    under_test = grant is not None and capability in grant.capabilities_under_test
    if not ctx.capabilities.usable(capability) and not under_test:
        reject(RejectReason.CAPABILITY, f"capability {capability} is neither usable nor under test")
    for problem in entry_rule_problems(
        ctx.rule_profile,
        mode=ctx.mode,
        capabilities=ctx.capabilities,
        day_trades_remaining=None,
        plans_same_day_exit=ctx.order_type is OrderType.MARKET,
    ):
        reject(RejectReason.ACCOUNT_RULE, problem)
    quantity = ctx.quantity
    if (
        quantity <= 0
        or quantity < spec.min_quantity
        or round_down_to_step(quantity, spec.quantity_step) != quantity
    ):
        reject(RejectReason.SIZE_BELOW_MINIMUM, f"quantity {quantity} is not a valid order size")
    if ctx.order_type is OrderType.LIMIT:
        price = ctx.limit_price
        if price is None or price <= 0 or round_down_to_step(price, spec.price_tick) != price:
            reject(RejectReason.RISK_LIMIT, "a certification limit buy needs a limit on the tick")
    elif ctx.limit_price is not None:
        reject(RejectReason.RISK_LIMIT, "a market order carries no limit price")

    # 4. market and data quality ---------------------------------------------------------
    q = ctx.policy.policy.quality
    if not ctx.market_open:
        reject(RejectReason.OUTSIDE_ENTRY_WINDOW, "the market is closed (broker clock)")
    if quote is None:
        reject(RejectReason.STALE_DATA, "no quote")
    else:
        age = ctx.now - quote.event_time
        if age > timedelta(seconds=q.max_quote_age_seconds) or age < timedelta(0):
            reject(RejectReason.STALE_DATA, f"quote age {age.total_seconds():.3f}s exceeds limit")
        if quote.instrument_id != spec.instrument_id:
            reject(RejectReason.STALE_DATA, "the quote is for another instrument")
        if ctx.order_type is OrderType.LIMIT and ctx.limit_price is not None:
            ceiling = quote.bid * (1 - CERTIFICATION_MIN_PASSIVE_DISCOUNT)
            if ctx.limit_price > ceiling:
                reject(
                    RejectReason.RISK_LIMIT,
                    f"limit {ctx.limit_price} is not at least "
                    f"{CERTIFICATION_MIN_PASSIVE_DISCOUNT * 100}% below the bid {quote.bid}",
                )
        if ctx.order_type is OrderType.MARKET and quote.spread_bps > CERTIFICATION_MAX_SPREAD_BPS:
            reject(
                RejectReason.SPREAD_TOO_WIDE,
                f"spread {quote.spread_bps:.2f} bps > {CERTIFICATION_MAX_SPREAD_BPS} bps",
            )
    if ctx.fx_to_usd is None:
        reject(RejectReason.STALE_DATA, "no fresh FX conversion")

    # 5. the notional cap (hard) and funds -----------------------------------------------
    raw_bound = certification_notional_bound(ctx.order_type, quantity, ctx.limit_price, quote)
    bound = ZERO
    if raw_bound is None or ctx.fx_to_usd is None:
        reject(RejectReason.RISK_LIMIT, "the order's notional bound cannot be computed")
    else:
        bound = raw_bound * ctx.fx_to_usd
        cap = min(
            grant.max_order_notional_usd if grant is not None else ZERO,
            CERTIFICATION_MAX_ORDER_NOTIONAL_USD,
        )
        if bound > cap:
            reject(
                RejectReason.RISK_LIMIT,
                f"notional bound {bound:.4f} USD exceeds the certification cap {cap} USD",
            )
        reference = ctx.limit_price if ctx.order_type is OrderType.LIMIT else None
        if reference is None and quote is not None:
            reference = quote.ask
        if reference is not None and quantity * reference < spec.min_notional:
            reject(
                RejectReason.SIZE_BELOW_MINIMUM,
                f"notional below the instrument minimum {spec.min_notional}",
            )
        if bound + CERTIFICATION_FEE_ALLOWANCE_USD > ctx.account_spendable_usd:
            reject(RejectReason.INSUFFICIENT_FUNDS, "not enough spendable subledger cash")

    return CertificationDecision(
        approved=not reasons,
        reasons=tuple(reasons),
        details=tuple(details),
        capability=capability,
        notional_bound_usd=bound,
    )


@dataclass(frozen=True, slots=True)
class ExitApproval:
    quantity: Decimal
    reason: str


def approve_reducing_exit(
    *,
    spec: AnySpec,
    owned_quantity: Decimal,
    committed_sell_quantity: Decimal,
    requested_quantity: Decimal,
) -> ExitApproval | None:
    """Approve a sell that can only reduce an existing long position.

    Never blocked by pauses, spreads or stale quotes (exits must not deadlock), but the quantity
    can never exceed what is owned minus what other open sells already cover, so overlapping
    exits cannot create an accidental short (architecture.md §7).
    """
    available = owned_quantity - committed_sell_quantity
    if available <= 0 or requested_quantity <= 0:
        return None
    quantity = round_down_to_step(min(available, requested_quantity), spec.quantity_step)
    if quantity <= 0:
        return None
    return ExitApproval(quantity=quantity, reason="reducing exit within owned quantity")
