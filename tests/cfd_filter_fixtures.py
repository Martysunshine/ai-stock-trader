"""SYNTHETIC hourly bid/ask bars for the CFD filter tests (T-0026). Seeded random walks; they
carry no information about any real market and no test here reads ``data/`` or ``runs/``."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np

from trader.data.history import HistoryBar
from trader.data.quote_bars import QuoteBar

#: (start price, half spread in bps of the price) per synthetic epic
EPICS: dict[str, tuple[float, float]] = {"BTCUSD": (60000.0, 3.0), "ETHUSD": (3000.0, 4.0)}


def hourly_walk(
    epic: str, start: datetime, end: datetime, *, seed: int
) -> list[tuple[HistoryBar, HistoryBar]]:
    """Hourly (bid, ask) bars every hour from ``start`` to ``end``: a random walk with bursts,
    so the 24-hour channel breaks a few times a day, and a quoted spread that varies by bar."""
    price, half_bps = EPICS[epic]
    rng = np.random.default_rng(seed)
    rows: list[tuple[HistoryBar, HistoryBar]] = []
    t = start
    while t < end:
        burst = 3.0 if rng.random() < 0.08 else 1.0
        o = price
        c = o * (1 + rng.normal(0.0, 0.004 * burst))
        h = max(o, c) * (1 + abs(rng.normal(0.0, 0.0015)))
        low = min(o, c) * (1 - abs(rng.normal(0.0, 0.0015)))
        half = price * half_bps * 1e-4 * (1 + rng.random())
        pair = []
        for sign in (-1, 1):
            off = sign * half
            pair.append(
                HistoryBar(
                    start=t,
                    minutes=60,
                    open=Decimal(f"{o + off:.2f}"),
                    high=Decimal(f"{h + off:.2f}"),
                    low=Decimal(f"{low + off:.2f}"),
                    close=Decimal(f"{c + off:.2f}"),
                    volume=Decimal(int(rng.integers(50, 500))),
                )
            )
        rows.append((pair[0], pair[1]))
        price = float(Decimal(f"{c:.2f}"))
        t += timedelta(hours=1)
    return rows


def quote_bars(epic: str, start: datetime, end: datetime, *, seed: int) -> list[QuoteBar]:
    return [QuoteBar(b, a) for b, a in hourly_walk(epic, start, end, seed=seed)]
