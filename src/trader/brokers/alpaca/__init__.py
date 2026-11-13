"""Alpaca PAPER broker adapter (milestone M4; ADR-0025).

The only package that imports the alpaca-py SDK and truststore (tests/test_boundaries.py).

* :mod:`~trader.brokers.alpaca.transport` - SDK calls, error classification, paper endpoint only.
* :mod:`~trader.brokers.alpaca.mapping` - Alpaca JSON -> domain objects, exact decimals.
* :mod:`~trader.brokers.alpaca.stream` - background ``trade_updates`` thread and queue.
* :mod:`~trader.brokers.alpaca.pacing` - token bucket on the injected clock.
* :mod:`~trader.brokers.alpaca.adapter` - :class:`AlpacaPaperAdapter` (``BrokerAdapter``).
"""

from trader.brokers.alpaca.adapter import BROKER, AlpacaPaperAdapter, PaperOnlyError

__all__ = ["BROKER", "AlpacaPaperAdapter", "PaperOnlyError"]
