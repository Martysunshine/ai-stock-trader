"""Candidates, scores and decision records (architecture.md §3, instructions.md §5 "Reporting")."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from trader.domain.base import DomainModel
from trader.domain.types import (
    Dec,
    Direction,
    DomainError,
    NonNegDec,
    PosDec,
    Track,
    UtcDatetime,
)


class RejectReason(StrEnum):
    """Why a candidate did not become an order. Cash/no trade is a legitimate outcome."""

    NO_EDGE = "NO_EDGE"
    TOO_COSTLY = "TOO_COSTLY"
    UNKNOWN_COSTS = "UNKNOWN_COSTS"
    ACCOUNT_RULE = "ACCOUNT_RULE"
    STALE_DATA = "STALE_DATA"
    RISK_LIMIT = "RISK_LIMIT"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    SIZE_BELOW_MINIMUM = "SIZE_BELOW_MINIMUM"
    PAUSED = "PAUSED"
    CAPABILITY = "CAPABILITY"
    FORBIDDEN_PRODUCT = "FORBIDDEN_PRODUCT"
    OUTSIDE_ENTRY_WINDOW = "OUTSIDE_ENTRY_WINDOW"
    ENTRY_LIMIT_REACHED = "ENTRY_LIMIT_REACHED"
    NO_EXIT_PLAN = "NO_EXIT_PLAN"
    FEED_MISMATCH = "FEED_MISMATCH"
    MODE_NOT_PERMITTED = "MODE_NOT_PERMITTED"
    LEADERSHIP = "LEADERSHIP"


class ReturnBasis(StrEnum):
    """Price basis of an expected return (strategy_and_profitability.md §1).

    MID_TO_MID predictions must be charged both spread crossings; EXECUTABLE (ask-to-bid)
    predictions already contain them. Spread is therefore charged exactly once.
    """

    MID_TO_MID = "MID_TO_MID"
    EXECUTABLE = "EXECUTABLE"


class EdgeEvidence(StrEnum):
    NONE = "NONE"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    BACKTEST_ONLY = "BACKTEST_ONLY"
    VALIDATED_BUNDLE = "VALIDATED_BUNDLE"
    #: Not an edge: a ``trader paper certify`` order that demonstrates a broker capability.
    #: Accepted only by ``risk.engine.evaluate_certification_order`` (paper, with a grant).
    CERTIFICATION = "CERTIFICATION"
    #: Not an edge: ``trader paper run --practice`` assumes the strategy's planned target is
    #: reached, so only trades whose target covers round-trip costs are taken (ADR-0040).
    #: Paper and replay only; never evidence for G2/G3 or any promotion.
    PRACTICE = "PRACTICE"


class TradeCandidate(DomainModel):
    candidate_id: str
    strategy_id: str
    strategy_version: int
    spec_hash: str
    instrument_id: str
    track: Track
    direction: Direction
    feed: str
    bar_end: UtcDatetime
    decision_time: UtcDatetime
    reference_price: PosDec
    stop_price: PosDec
    target_price: PosDec | None
    max_hold_minutes: int = Field(gt=0)
    entry_limit_offset_bps: NonNegDec
    entry_ttl_seconds: int = Field(gt=0)
    gap_allowance_fraction: NonNegDec
    snapshot_hash: str
    expires_at: UtcDatetime

    @model_validator(mode="after")
    def _exit_rule(self) -> TradeCandidate:
        if self.direction is Direction.LONG and self.stop_price >= self.reference_price:
            raise DomainError("long candidate stop must be below the reference price")
        if self.direction is Direction.SHORT and self.stop_price <= self.reference_price:
            raise DomainError("short candidate stop must be above the reference price")
        if self.expires_at <= self.decision_time:
            raise DomainError("candidate expires before it is available")
        return self


class SignalScore(DomainModel):
    candidate_id: str
    model_hash: str
    expected_gross_return: Dec
    uncertainty_margin: NonNegDec
    basis: ReturnBasis
    evidence: EdgeEvidence
    edge_source_hash: str


class DecisionRecord(DomainModel):
    decision_id: str
    candidate_id: str
    instrument_id: str
    track: Track
    decided_at: UtcDatetime
    approved: bool
    reasons: tuple[RejectReason, ...]
    details: tuple[str, ...] = ()
    policy_hash: str
    model_hash: str
    evidence: EdgeEvidence
    quantity: NonNegDec
    entry_limit_price: PosDec | None = None
    notional_usd: NonNegDec
    planned_loss_usd: NonNegDec
    estimated_costs_usd: NonNegDec
    expected_net_usd: Dec
    effective_sizing_equity_usd: NonNegDec
    binding_constraint: str = ""
    intent_id: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DecisionRecord:
        if self.approved and self.reasons:
            raise DomainError("an approved decision carries no rejection reasons")
        if not self.approved and not self.reasons:
            raise DomainError("a rejected decision must state at least one reason")
        if self.approved and (self.quantity <= 0 or self.intent_id is None):
            raise DomainError("an approved decision needs a positive quantity and an intent")
        return self
