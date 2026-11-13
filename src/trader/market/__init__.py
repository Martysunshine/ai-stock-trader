"""The dashboard's market feed (ADR-0045): watchlist prices written to ``data/market/``.

A data tool with no order authority: it imports no broker, execution, risk, runtime or storage
module (``tests/test_boundaries.py``). Run it with ``python -m trader.market refresh`` or
``watch --every 5``; ``npm run dev`` keeps a watch running beside the Trader Console.
"""
