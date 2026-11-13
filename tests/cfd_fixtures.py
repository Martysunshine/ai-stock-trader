"""Synthetic two-sided bars and a scripted rule for the CFD engine and study tests (ADR-0052)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.accounting.fees import FeeSchedule, load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import RiskPolicy, load_policy
from trader.data.history import HistoryBar
from trader.data.quote_bars import QuoteBar
from trader.domain.instruments import CommodityInstrumentSpec
from trader.domain.types import Direction
from trader.research.cfd_engine import CfdSignal

ROOT = Path(__file__).resolve().parents[1]
SPECS = load_instruments(ROOT / "configs/instruments/capital_demo_cfds_v1.yaml")
FEES: FeeSchedule = load_fee_schedule(ROOT / "configs/fees/capital_com_cfd_demo_v1.yaml")
CFD_POLICY: RiskPolicy = load_policy(ROOT / "configs/policy/cfd_sleeve_v1.yaml").policy
CRYPTO_POLICY: RiskPolicy = load_policy(ROOT / "configs/policy/crypto_cfd_sleeve_v1.yaml").policy
#: Monday 5 January 2026, 00:00 UTC.
MONDAY = datetime(2026, 1, 5, tzinfo=UTC)


def spec(epic: str) -> CommodityInstrumentSpec:
    s = SPECS[f"CAPITAL:{epic}"]
    assert isinstance(s, CommodityInstrumentSpec)
    return s


def day(n: int) -> datetime:
    return MONDAY + timedelta(days=n)


def qbar(
    start: datetime,
    o: str,
    h: str,
    low: str,
    c: str,
    *,
    half: str = "1",
    minutes: int = 1440,
) -> QuoteBar:
    """A bar from its mid prices and a half-spread."""
    d = Decimal(half)
    mids = [Decimal(v) for v in (o, h, low, c)]

    def side(offset: Decimal) -> HistoryBar:
        return HistoryBar(start, minutes, *(m + offset for m in mids), volume=Decimal(10))

    return QuoteBar(side(-d), side(d))


def flat_days(first: int, count: int, price: str, *, half: str = "1") -> list[QuoteBar]:
    """Quiet daily bars: open, high, low and close one point around ``price``."""
    p = Decimal(price)
    return [
        qbar(day(n), str(p), str(p + 1), str(p - 1), str(p), half=half)
        for n in range(first, first + count)
    ]


@dataclass
class ScriptedRule:
    """Emits the signals and exit reasons it is given, keyed by (instrument, bar start)."""

    signals: Mapping[tuple[str, datetime], dict[str, object]] = field(default_factory=dict)
    exits: Mapping[tuple[str, datetime], str] = field(default_factory=dict)
    seen: list[tuple[str, datetime]] = field(default_factory=list)

    @property
    def strategy_id(self) -> str:
        return "scripted"

    @property
    def spec_hash(self) -> str:
        return "0" * 64

    def on_bar(self, instrument_id: str, bar: QuoteBar) -> CfdSignal | None:
        self.seen.append((instrument_id, bar.start))
        params = self.signals.get((instrument_id, bar.start))
        if params is None:
            return None
        values: dict[str, object] = {
            "signal_id": f"{instrument_id}@{bar.start.date()}",
            "instrument_id": instrument_id,
            "decided_at": bar.end,
            "reference": bar.mid_close,
            "target_role": "EXIT",
            "max_hold_minutes": 7 * 1440,
        }
        values.update(params)
        values["stop"] = Decimal(str(values["stop"]))
        values["target"] = Decimal(str(values["target"]))
        if "priority" in values:
            values["priority"] = Decimal(str(values["priority"]))
        return CfdSignal(**values)  # type: ignore[arg-type]

    def exit_signal(self, instrument_id: str, bar: QuoteBar, direction: Direction) -> str | None:
        return self.exits.get((instrument_id, bar.start))


def long(stop: str, target: str, **kw: object) -> dict[str, object]:
    return {"direction": Direction.LONG, "stop": stop, "target": target, **kw}


def short(stop: str, target: str, **kw: object) -> dict[str, object]:
    return {"direction": Direction.SHORT, "stop": stop, "target": target, **kw}


def bars_by(instrument: str, bars: Sequence[QuoteBar]) -> dict[str, tuple[QuoteBar, ...]]:
    return {instrument: tuple(bars)}
