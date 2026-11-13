"""The forward ledger (ADR-0056): each practice setup's demo trades beside its trial.

From the owner's review of open-source trading systems: a transparent forward record compared
with the frozen backtest, not a strategy changed along the way. For every setup in
``trader_setups`` it counts the closed trades the trader journalled for it, their net result,
wins and the average per trade, and sets them beside the trial's own per-trade averages in the
base and the stressed view (``runs/research/<study>/results.json``, written when the trial ran).
A forward test's loss cap and review count are shown with where the setup stands.

``python -m trader.practice forward`` prints it and writes ``runs/practice/forward.json``. It
reads the journals and the trial results and changes neither.

**The league** (docs/strategy_playbook.md section 5, ADR-0062). Every setup stands in one state,
decided by rule from its own closed demo trades, so nobody has to read the numbers by eye:

* ``OFF``: switched off in ``configs/practice/setups.yaml``.
* ``STOPPED``: its result is below its loss cap; the trader takes no more entries (owner review).
* ``COLLECTING``: fewer closed trades than its review count (60 unless the setup says otherwise);
  a result on so few trades says nothing.
* ``CANDIDATE``: at the review count with a positive net result and a positive average a trade.
  Worth a registered trial; **not** a pass, and never a step toward real money by itself.
* ``DROP``: at the review count with a result at or below zero; remove it from the book.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from trader.domain.types import iso_utc
from trader.practice.journal import REPO_ROOT, VENUES, PracticeJournal

SETUPS_FILE = REPO_ROOT / "configs" / "practice" / "setups.yaml"
#: Closed trades before a forward result is read, for a setup that does not say otherwise.
DEFAULT_REVIEW_TRADES = 60


def league_state(row: Mapping[str, Any]) -> tuple[str, str]:
    """The league state of a ledger row and the one-line reason (module docstring)."""
    trades = int(row["trades"])
    review = int(row["review_after_trades"] or DEFAULT_REVIEW_TRADES)
    net = Decimal(str(row["net_usd"]))
    if not row["enabled"]:
        return "OFF", "switched off in configs/practice/setups.yaml"
    if row["switched_off"]:
        return "STOPPED", f"net {net} USD is below its loss cap of -{row['loss_cap_usd']} USD"
    if trades < review:
        return "COLLECTING", f"{trades} of {review} trades"
    if net > 0:
        return "CANDIDATE", f"net {net} USD over {trades} trades: worth a registered trial"
    return "DROP", f"net {net} USD over {trades} trades: remove it from the book"


def _setups(path: Path) -> list[dict[str, Any]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = doc.get("trader_setups") if isinstance(doc, dict) else None
    return [r for r in rows or [] if isinstance(r, dict)]


def trial_baseline(repo_root: Path, study: str, trial: str) -> dict[str, Any] | None:
    """The trial's per-trade averages and trade counts, base and stressed; ``None`` if the
    study's results are not on this machine."""
    try:
        study_id = yaml.safe_load((repo_root / study).read_text(encoding="utf-8"))["study_id"]
        path = repo_root / "runs" / "research" / study_id / "results.json"
        views = json.loads(path.read_text(encoding="utf-8"))["trials"][trial]["views"]
    except (OSError, KeyError, ValueError, TypeError):
        return None
    out: dict[str, Any] = {}
    for name in ("base", "stressed"):
        v = views.get(name) or {}
        out[name] = {
            "trades": v.get("trades"),
            "net_usd": v.get("net_pnl_usd"),
            "per_trade_usd": v.get("net_expectancy_per_trade_usd"),
            "win_rate": v.get("win_rate"),
        }
    return out


def ledger(
    journals: Mapping[str, PracticeJournal],
    setups: Sequence[Mapping[str, Any]],
    now: datetime,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    rows = []
    for s in setups:
        j = journals[str(s["sleeve"])]
        key = str(s["key"])
        closes = [
            e["payload"]
            for e in j.events
            if e["event"] == "CLOSE" and j.setup_of(e["payload"]["trade_id"]) == key
        ]
        net = sum((Decimal(c["pnl_usd"]) for c in closes), Decimal(0))
        wins = sum(1 for c in closes if Decimal(c["pnl_usd"]) > 0)
        cap = Decimal(str(s["loss_cap_usd"])) if s.get("loss_cap_usd") is not None else None
        review = int(s.get("review_after_trades") or DEFAULT_REVIEW_TRADES)
        rows.append(
            {
                "key": key,
                "title": s.get("title"),
                "sleeve": s["sleeve"],
                "status": s["status"],
                "trial": s.get("trial") or "practice",
                "enabled": bool(s.get("enabled")),
                "trades": len(closes),
                "open_trades": len(j.open_trades_of(key)),
                "wins": wins,
                "net_usd": f"{net:.2f}",
                "per_trade_usd": f"{net / len(closes):.4f}" if closes else None,
                "by_reason": {
                    r: sum(1 for c in closes if c["reason"] == r)
                    for r in sorted({c["reason"] for c in closes})
                },
                "loss_cap_usd": str(cap) if cap is not None else None,
                "switched_off": cap is not None and net < -cap,
                "review_after_trades": review,
                "review_due": len(closes) >= review,
                "trial_baseline": (
                    trial_baseline(repo_root, str(s["study"]), str(s["trial"]))
                    if s.get("study") and s.get("trial")
                    else None
                ),
            }
        )
    for row in rows:
        row["league"], row["league_note"] = league_state(row)
    return {"version": 1, "generated_at": iso_utc(now), "setups": rows}


def render(doc: Mapping[str, Any]) -> list[str]:
    lines = [f"Forward ledger {doc['generated_at']} (demo trades of each setup against its trial)"]
    for r in doc["setups"]:
        status = r["status"].replace("_", " ")
        lines.append(
            f"{r['key']} ({status}, {r['trial']}) {r['league']}, {r['league_note']}: "
            f"{r['trades']} closed, "
            f"{r['wins']} won, net {r['net_usd']} USD"
            + (f", {r['per_trade_usd']} a trade" if r["per_trade_usd"] else "")
            + (f", {r['open_trades']} open" if r["open_trades"] else "")
        )
        b = r["trial_baseline"]
        if b:
            lines.append(
                f"  trial: base {b['base']['per_trade_usd']:+.4f} a trade over "
                f"{b['base']['trades']}, stressed {b['stressed']['per_trade_usd']:+.4f} over "
                f"{b['stressed']['trades']}"
            )
        if r["loss_cap_usd"]:
            lines.append(
                f"  loss cap -{r['loss_cap_usd']} USD; review after {r['review_after_trades']} "
                f"trades{' (DUE)' if r['review_due'] else ''}"
            )
    return lines


def write_ledger(doc: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def run_forward(now: datetime, echo: Any = print) -> int:
    journals = {v: PracticeJournal(venue=v) for v in VENUES}
    doc = ledger(journals, _setups(SETUPS_FILE), now)
    write_ledger(doc, REPO_ROOT / "runs" / "practice" / "forward.json")
    for line in render(doc):
        echo(line)
    return 0
