"""Deterministic SYNTHETIC session generator for the replay demonstrator.

The generated data is not market history and carries no evidence about any real instrument. It is
shaped so the demonstrator exercises: an eligible breakout candidate, a candidate rejected for a
wide spread, an early-close calendar day (2026-11-27, NYSE closes 13:00 New York), and - in the
``gap`` variant - a trading halt followed by a gap through the protective stop.

Prices follow piecewise-linear anchors with small seeded noise on trade prints. Every 30 seconds
each symbol prints one trade (at :00/:30) and one quote (at :15/:45), received 120 ms later.
"""

from __future__ import annotations

import itertools
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.data.recorded_session import EdgeAssumptions, SessionManifest, sha256_file
from trader.domain.candidates import ReturnBasis
from trader.domain.market import Quote, Trade, TradingStatus

GENERATOR_VERSION = "synthetic-v1"
FEED = "SYNTHETIC"
SESSION_OPEN = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)  # 09:30 New York
SESSION_CLOSE = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # 13:00 New York (early close)
RECEIPT_LATENCY = timedelta(milliseconds=120)
TICK = timedelta(seconds=30)

# (minutes after the open, mid price)
SYNA_ANCHORS: list[tuple[float, str]] = [
    (0, "100.00"),
    (10, "100.30"),
    (20, "100.58"),
    (26, "100.42"),
    (30, "100.38"),
    (36, "100.50"),
    (41, "100.36"),
    (47, "100.49"),
    (52, "100.35"),
    (57, "100.46"),
    (60, "100.44"),
    (61, "100.60"),
    (64.5, "101.20"),
    (65.25, "101.22"),
    (65.75, "101.24"),
    (66.25, "101.33"),
    (70, "101.45"),
    (75, "101.36"),
    (85, "101.30"),
    (95, "101.38"),
    (110, "101.28"),
    (125, "101.34"),
    (140, "101.22"),
    (160, "101.30"),
    (185, "101.18"),
    (210, "101.10"),
]
SYNB_ANCHORS: list[tuple[float, str]] = [
    (0, "50.00"),
    (12, "50.12"),
    (24, "50.20"),
    (36, "50.16"),
    (48, "50.22"),
    (60, "50.18"),
    (74, "50.20"),
    (75, "50.24"),
    (79.5, "50.70"),
    (90, "50.62"),
    (120, "50.50"),
    (135, "50.46"),
    (139.5, "50.98"),
    (150, "50.92"),
    (210, "50.80"),
]
# Gap variant: SYNA is halted at 15:50 and resumes at 16:10 six percent lower, then recovers.
GAP_HALT_MINUTE = 80
GAP_RESUME_MINUTE = 100
SYNA_GAP_AFTER: list[tuple[float, str]] = [
    (100, "95.15"),
    (115, "95.40"),
    (130, "95.30"),
    (140, "95.60"),
    (150, "95.90"),
    (160, "96.30"),
    (175, "96.10"),
    (210, "96.00"),
]


@dataclass(frozen=True)
class SymbolSpec:
    instrument_id: str
    anchors: list[tuple[float, str]]
    spread: Decimal
    seed: int


def _interpolate(anchors: list[tuple[float, str]], minute: float) -> Decimal:
    pts = [(Decimal(str(m)), Decimal(p)) for m, p in anchors]
    m = Decimal(str(minute))
    if m <= pts[0][0]:
        return pts[0][1]
    for (m0, p0), (m1, p1) in itertools.pairwise(pts):
        if m0 <= m <= m1:
            return p0 + (p1 - p0) * (m - m0) / (m1 - m0)
    return pts[-1][1]


def _tick(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def generate_events(variant: str = "base") -> list[dict[str, object]]:
    if variant not in ("base", "gap"):
        raise ValueError("variant must be 'base' or 'gap'")
    symbols = [
        SymbolSpec("XNYS:SYNA", SYNA_ANCHORS, Decimal("0.04"), 11),
        SymbolSpec("XNYS:SYNB", SYNB_ANCHORS, Decimal("0.30"), 23),
    ]
    events: list[tuple[datetime, int, dict[str, object]]] = []
    for order, sym in enumerate(symbols):
        rng = random.Random(sym.seed)
        seq = 0
        t = SESSION_OPEN
        gap = variant == "gap" and sym.instrument_id == "XNYS:SYNA"
        while t < SESSION_CLOSE:
            minute = (t - SESSION_OPEN).total_seconds() / 60
            halted = gap and GAP_HALT_MINUTE <= minute < GAP_RESUME_MINUTE
            if gap and minute == GAP_HALT_MINUTE:
                seq += 1
                events.append((t, order, _status(sym.instrument_id, t, seq, True)))
            if gap and minute == GAP_RESUME_MINUTE:
                seq += 1
                events.append((t, order, _status(sym.instrument_id, t, seq, False)))
            anchors = sym.anchors
            if gap and minute >= GAP_RESUME_MINUTE:
                anchors = SYNA_GAP_AFTER
            noise = Decimal(rng.randint(-1, 1)) / 100
            size = Decimal(rng.randint(50, 300))
            if not halted:
                mid = _interpolate(anchors, minute)
                seq += 1
                events.append(
                    (
                        t + RECEIPT_LATENCY,
                        order,
                        Trade(
                            instrument_id=sym.instrument_id,
                            feed=FEED,
                            event_time=t,
                            receipt_time=t + RECEIPT_LATENCY,
                            sequence=seq,
                            price=_tick(mid + noise),
                            size=size,
                        ).model_dump(mode="json"),
                    )
                )
                qt = t + timedelta(seconds=15)
                qmid = _interpolate(anchors, minute + 0.25)
                half = sym.spread / 2
                seq += 1
                events.append(
                    (
                        qt + RECEIPT_LATENCY,
                        order,
                        Quote(
                            instrument_id=sym.instrument_id,
                            feed=FEED,
                            event_time=qt,
                            receipt_time=qt + RECEIPT_LATENCY,
                            sequence=seq,
                            bid=_tick(qmid - half),
                            ask=_tick(qmid + half),
                            bid_size=Decimal(rng.randint(100, 500)),
                            ask_size=Decimal(rng.randint(100, 500)),
                        ).model_dump(mode="json"),
                    )
                )
            t += TICK
    events.sort(key=lambda e: (e[0], e[1]))
    return [e[2] for e in events]


def _status(instrument_id: str, t: datetime, seq: int, halted: bool) -> dict[str, object]:
    return TradingStatus(
        instrument_id=instrument_id,
        feed=FEED,
        event_time=t,
        receipt_time=t + RECEIPT_LATENCY,
        sequence=seq,
        halted=halted,
        reason="synthetic volatility halt" if halted else "synthetic resume",
    ).model_dump(mode="json")


def write_session(directory: Path, variant: str = "base") -> SessionManifest:
    directory.mkdir(parents=True, exist_ok=True)
    events = generate_events(variant)
    events_file = directory / "events.jsonl"
    with events_file.open("w", encoding="utf-8", newline="\n") as fh:
        for event in events:
            fh.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    manifest = SessionManifest(
        name=f"synthetic_{variant}_v1",
        description=(
            "SYNTHETIC replay fixture (not market data) for the first demonstrator; "
            + ("includes a halt and a 6% gap through the stop" if variant == "gap" else "base path")
        ),
        synthetic=True,
        feed=FEED,
        session_date="2026-11-27",
        instruments=("XNYS:SYNA", "XNYS:SYNB"),
        events_file="events.jsonl",
        events_sha256=sha256_file(events_file),
        event_count=len(events),
        generator={
            "module": "trader.data.synthetic",
            "version": GENERATOR_VERSION,
            "variant": variant,
        },
        edge_assumptions=EdgeAssumptions(
            expected_gross_return=Decimal("0.0060"),
            uncertainty_margin=Decimal("0.0015"),
            basis=ReturnBasis.MID_TO_MID,
            label=(
                "DECLARED FIXTURE ASSUMPTION for exercising the cost-aware path; it is not an "
                "estimated or validated edge and is not evidence of profitability"
            ),
        ),
        known_limitations=(
            "synthetic prices: no information about any real instrument",
            "bar-only/30-second granularity cannot certify execution realism",
            "single session; no corporate actions, no split events (M2 dataset)",
        ),
    )
    (directory / "manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest
