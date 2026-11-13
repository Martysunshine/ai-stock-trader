"""The practice watch (ADR-0049): the scan and the sleeves' equity as files for the console.

``python -m trader.practice watch`` runs beside the console (started by ``npm run dev``) and every
few minutes replaces ``runs/practice/scan.json`` (the index dip reading and every open practice
trade against its stop, target and time stop) and ``runs/practice/sleeves.json`` (each sleeve's
equity). It reads the market feed's files and the practice journals. It places no order, journals
nothing and holds no keys.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.domain.types import iso_utc
from trader.ops.clock import SleepingClock, SystemClock
from trader.practice import journal as practice
from trader.practice import setups


def scan_path() -> Path:
    return practice.REPO_ROOT / "runs" / "practice" / "scan.json"


def run_scan(market_root: Path | None, now: datetime) -> dict[str, Any]:
    """Run the scan now and replace runs/practice/scan.json with it."""
    journals = {v: practice.PracticeJournal(venue=v) for v in practice.VENUES}
    doc = setups.scan(setups.load_spec(), journals, market_root or setups.MARKET_ROOT, now)
    setups.write_scan(doc, scan_path())
    return doc


def write_sleeves(now: datetime) -> dict[str, Any]:
    """runs/practice/sleeves.json: each practice sleeve's equity (ADR-0046)."""
    sleeves = {v: practice.PracticeJournal(venue=v).summary() for v in practice.VENUES}
    doc = {"updated_at": now.isoformat(), "sleeves": sleeves}
    out = practice.REPO_ROOT / "runs" / "practice" / "sleeves.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, out)
    return doc


def run_watch(
    *,
    every_minutes: int = 15,
    market_root: Path | None = None,
    echo: Callable[[str], None] = print,
    clock: SleepingClock | None = None,
    iterations: int | None = None,
) -> int:
    """Scan now and then every ``every_minutes`` until stopped (``iterations`` bounds a test).
    A scan that cannot run is itself the news: its reason replaces the file, so the console
    shows why instead of an old reading."""
    if not 1 <= every_minutes <= 240:
        raise setups.SetupError("--every takes 1 to 240 minutes")
    timer = clock or SystemClock()
    done = 0
    try:
        while iterations is None or done < iterations:
            now = timer.now()
            try:
                doc = run_scan(market_root, now)
                signals = sum(1 for r in doc["readings"] if r["ticket"] is not None)
                echo(
                    f"scan {doc['generated_at']}: {signals} signal(s), "
                    f"{len(doc['open_trades'])} open trade(s)"
                    + ("  FEED STALE" if doc["feed"]["stale"] else "")
                )
                write_sleeves(now)
            except (setups.SetupError, practice.JournalError, ValueError, OSError) as exc:
                failed = {
                    "version": setups.SCAN_VERSION,
                    "generated_at": iso_utc(now),
                    "error": str(exc),
                }
                setups.write_scan(failed, scan_path())
                echo(f"scan failed: {exc}")
            done += 1
            if iterations is None or done < iterations:
                timer.sleep(every_minutes * 60)
    except KeyboardInterrupt:
        echo("practice watch stopped")
    return 0
