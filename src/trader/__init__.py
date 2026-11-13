"""ai-stock-trader: an evidence-gated automated trading system.

Replay is the default mode. Nothing in this package can submit a live order: the live entry
point is guarded (see ``trader.runtime.live_guard``) and no live adapter is certified.
"""

__version__ = "0.1.0"
