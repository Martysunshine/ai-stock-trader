"""The only factory for :class:`~trader.domain.orders.ApprovedPlan` (architecture.md §2).

``tests/test_boundaries.py`` asserts that no other module constructs ``ApprovedPlan``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trader.domain.orders import ApprovedPlan, OrderIntent, plan_digest


def issue_plan(
    *,
    intent: OrderIntent,
    decision_id: str,
    account_version: int,
    policy_hash: str,
    model_hash: str,
    quote_time: datetime | None,
    approved_at: datetime,
    valid_until: datetime,
    reserved_cash_usd: Decimal,
    reserved_risk_usd: Decimal,
    exit_method: str,
) -> ApprovedPlan:
    if valid_until <= approved_at:
        raise ValueError("an approval must have a positive validity window")
    intent_hash = intent.intent_hash()
    digest = plan_digest(
        intent_hash=intent_hash,
        account_version=account_version,
        policy_hash=policy_hash,
        model_hash=model_hash,
        quote_time=quote_time,
        valid_until=valid_until,
        client_order_id=intent.client_order_id,
        reserved_cash_usd=reserved_cash_usd,
        reserved_risk_usd=reserved_risk_usd,
    )
    return ApprovedPlan(
        intent=intent,
        intent_hash=intent_hash,
        decision_id=decision_id,
        account_version=account_version,
        policy_hash=policy_hash,
        model_hash=model_hash,
        quote_time=quote_time,
        approved_at=approved_at,
        valid_until=valid_until,
        reserved_cash_usd=reserved_cash_usd,
        reserved_risk_usd=reserved_risk_usd,
        exit_method=exit_method,
        digest=digest,
    )
