"""The intel feed: news, insider filings and pre-market activity, written as files (ADR-0047).

A data tool beside the market feed, not part of the trading runtime. It reads public and entitled
sources and writes ``data/intel/`` only. The runtime, strategies and risk never import it
(``tests/test_boundaries.py``): a rule that uses this information needs a registered trial and its
own decision record first.
"""
