"""Order state machine (architecture.md §7).

Local states follow the broker's reported status but never regress: a terminal state stays
terminal, "cancel requested" is still live exposure until the broker confirms the cancel, and
stale broker versions are ignored.
"""

from __future__ import annotations

from trader.domain.orders import (
    LIVE_STATES,
    TERMINAL_STATES,
    BrokerOrderStatus,
    OrderState,
)

_FROM_BROKER: dict[BrokerOrderStatus, OrderState] = {
    BrokerOrderStatus.ACCEPTED: OrderState.ACKNOWLEDGED,
    BrokerOrderStatus.PARTIALLY_FILLED: OrderState.PARTIALLY_FILLED,
    BrokerOrderStatus.PENDING_CANCEL: OrderState.CANCEL_REQUESTED,
    BrokerOrderStatus.FILLED: OrderState.FILLED,
    BrokerOrderStatus.CANCELED: OrderState.CANCELED,
    BrokerOrderStatus.EXPIRED: OrderState.EXPIRED,
    BrokerOrderStatus.REJECTED: OrderState.REJECTED,
}


class IllegalTransition(ValueError):
    pass


def next_state(current: OrderState, broker_status: BrokerOrderStatus) -> OrderState:
    """Local state after applying a (non-stale) broker status."""
    target = _FROM_BROKER[broker_status]
    if current in TERMINAL_STATES:
        # Late events never resurrect a closed order; fills are still posted separately.
        return current
    if current is OrderState.PENDING_SUBMIT:
        raise IllegalTransition("broker reported an order the gateway never claimed")
    if current is OrderState.CANCEL_REQUESTED and target in (
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
    ):
        # A fill racing a cancel request: still cancel-requested (and still live exposure).
        return OrderState.CANCEL_REQUESTED
    if current is OrderState.PARTIALLY_FILLED and target is OrderState.ACKNOWLEDGED:
        return current
    return target


def is_live(state: OrderState) -> bool:
    return state in LIVE_STATES


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_STATES
