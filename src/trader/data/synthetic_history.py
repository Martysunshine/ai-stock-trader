"""Deterministic synthetic equity bar history (instructions.md §6 task 4).

SYNTHETIC - carries no information about any real instrument. It exists to test calendar,
corporate-action and data-quality handling end to end:

* 5-minute regular-session bars for ``XNYS:SYNA`` and ``XNYS:SYNB`` from 2026-10-26 to 2026-11-30,
  spanning the US daylight-saving change (1 November), the Thanksgiving holiday (26 November)
  and the early close (27 November, 13:00 New York);
* a 2-for-1 forward split of SYNB effective 2026-11-16 (raw prices halve at that open);
* a symbol change SYNB -> SYNC effective 2026-11-20 (same instrument identity);
* a feed gap: SYNA has no bars 15:00-15:30 New York on 2026-11-10;
* a missing session: SYNB has no bars at all on 2026-11-12;
* a VWAP and trade count on every bar (the stock strategies need a source VWAP).

The generator is seeded and writes through the normal history store, so its content hash is
stable and pinned by a test.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from trader.data.calendars import NEW_YORK, ExchangeCalendar
from trader.data.corporate_actions import SplitEvent, SymbolChange
from trader.data.history import HistoryBar
from trader.data.manifest import HistoryDataset
from trader.data.quality import expected_starts_sessions

SEED = 20261107
CENT = Decimal("0.01")
SPLIT = SplitEvent("XNYS:SYNB", date(2026, 11, 16), Decimal(2), Decimal(1), source="synthetic")
RENAME = SymbolChange("XNYS:SYNB", date(2026, 11, 20), "SYNB", "SYNC", source="synthetic")
GAP_DAY = date(2026, 11, 10)
GAP_LOCAL = (time(15, 0), time(15, 30))
MISSING_SESSION = date(2026, 11, 12)
START_PRICES = {"XNYS:SYNA": 100.0, "XNYS:SYNB": 40.0}


def _price(value: float) -> Decimal:
    return Decimal(str(round(max(value, 0.05), 2)))


def generate(start: datetime, end: datetime, minutes: int = 5) -> dict[str, list[HistoryBar]]:
    calendar = ExchangeCalendar("XNYS")
    _, sessions = expected_starts_sessions(calendar, start, end, minutes)
    rng = random.Random(SEED)
    out: dict[str, list[HistoryBar]] = {iid: [] for iid in START_PRICES}
    last_close = dict(START_PRICES)
    split_open = calendar.session_for_date(SPLIT.ex_date)
    for day_iso in sorted(sessions):
        day = date.fromisoformat(day_iso)
        starts = sessions[day_iso]
        for iid in sorted(START_PRICES):
            price = last_close[iid] * (1 + rng.gauss(0, 0.004))  # overnight gap
            if (
                iid == SPLIT.instrument_id
                and split_open is not None
                and starts[0] == split_open.open
            ):
                price /= float(SPLIT.ratio)
            # intraday regime blocks of one hour: trend up, trend down or mean-reverting chop
            regime_drift = 0.0
            for i, bar_start in enumerate(starts):
                if i % 12 == 0:
                    regime_drift = rng.choice([0.0006, -0.0006, 0.0, 0.0, 0.0002])
                ticks = [price]
                for _ in range(4):
                    ticks.append(ticks[-1] * (1 + regime_drift / 4 + rng.gauss(0, 0.0007)))
                o, c = ticks[0], ticks[-1]
                hi = max(ticks) * (1 + abs(rng.gauss(0, 0.0003)))
                lo = min(ticks) * (1 - abs(rng.gauss(0, 0.0003)))
                op, cp, hp, lp = _price(o), _price(c), _price(hi), _price(lo)
                hp = max(hp, op, cp)
                lp = min(lp, op, cp)
                vwap = min(max(_price(sum(ticks) / len(ticks)), lp), hp)
                shares = rng.randint(800, 6000) * (
                    2 if iid == SPLIT.instrument_id and day >= SPLIT.ex_date else 1
                )
                price = c
                local = bar_start.astimezone(NEW_YORK)
                if (
                    iid == "XNYS:SYNA"
                    and day == GAP_DAY
                    and GAP_LOCAL[0] <= local.time() < GAP_LOCAL[1]
                ):
                    continue
                if iid == "XNYS:SYNB" and day == MISSING_SESSION:
                    continue
                out[iid].append(
                    HistoryBar(
                        start=bar_start,
                        minutes=minutes,
                        open=op,
                        high=hp,
                        low=lp,
                        close=cp,
                        volume=Decimal(shares),
                        vwap=vwap,
                        trade_count=rng.randint(20, 200),
                    )
                )
            last_close[iid] = price
    return out


def write_synthetic_history(ds: HistoryDataset, repo_root: Path) -> HistoryDataset:
    from trader.data.ingest import GAP_ALPACA, store_instrument

    req = ds.request
    calendar = ExchangeCalendar("XNYS")
    expected, sessions = expected_starts_sessions(calendar, req.start, req.end, req.bar_minutes)
    generated = generate(req.start, req.end, req.bar_minutes)
    retrieved = req.end + timedelta(days=1)
    files = [
        store_instrument(
            ds,
            repo_root,
            inst,
            generated[inst.instrument_id],
            retrieved_at=retrieved,
            expected=expected,
            sessions=sessions,
            gap_semantics="SYNTHETIC: " + GAP_ALPACA,
        )
        for inst in req.instruments
    ]
    actions = (
        {
            "type": "SPLIT",
            "instrument_id": SPLIT.instrument_id,
            "ex_date": SPLIT.ex_date.isoformat(),
            "new_rate": str(SPLIT.new_rate),
            "old_rate": str(SPLIT.old_rate),
            "source": SPLIT.source,
        },
        {
            "type": "SYMBOL_CHANGE",
            "instrument_id": RENAME.instrument_id,
            "effective_date": RENAME.effective_date.isoformat(),
            "old_symbol": RENAME.old_symbol,
            "new_symbol": RENAME.new_symbol,
            "source": RENAME.source,
        },
    )
    return ds.model_copy(
        update={
            "status": "ACQUIRED",
            "blocker": "",
            "retrieved_at": retrieved,
            "files": tuple(files),
            "corporate_actions": actions,
        }
    )
