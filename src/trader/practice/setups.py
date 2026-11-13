"""Daily setup scan for the assistant's practice sleeves (ADR-0049, ADR-0052).

One setup is traded: weekly momentum on the crypto CFDs, the rule of trial T-0019 (spec
``cfd_crypto_weekly_momentum_v1``), the only practice setup whose registered trial on the
broker's own quotes was net positive in the base and stressed views and in walk forward. Each
Sunday daily bar (it closes on Monday 00:00 UTC) decides the week: long when the close is above
the close seven days earlier, short when below; stop 3 x ATR(10) from the close, target twice
that distance on the other side, out after one week. One crypto trade at a time, the larger
move first. The index dip of ADR-0049 was removed: its trial T-0018 broke even under stressed
costs. The CFD sleeve has no setup and stays flat.

The scan reads the market feed's completed daily bars of the Capital.com crypto CFDs
(``data/market/cfd``, ADR-0045) and gives, per epic, the week's return and - on the decision
bar - the entry, stop, target and size that fit the sleeve's risk limit; and for every open
practice trade of both sleeves its stop, target and time stop against the feed's last price.
`trader journal scan` prints it and writes ``runs/practice/scan.json`` for the console. It
places nothing and journals nothing: an order still needs a PLAN and passes the guard. Readings
are practice aids, never research evidence.

The ATR is Wilder's, seeded with the mean of the first ten true ranges and each average
quantised to 12 decimal places, as ``features/daily.py`` (``daily-v1``) computes it for the
trial; it is computed here so that the watch beside the console loads no numeric stack.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trader.domain.types import iso_utc
from trader.practice.journal import REPO_ROOT, RULES_DIR, EpicRule, PracticeJournal

SETUPS_FILE = RULES_DIR / "setups.yaml"
MARKET_ROOT = REPO_ROOT / "data" / "market"
SETUP_KEY = "weekly_momentum_v1"
QUANTUM = Decimal("0.000000000001")
_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


class SetupError(ValueError):
    """The scan cannot read what it needs."""


class WeeklyMomentumSpec(BaseModel):
    """The registered rule's parameters (trial T-0019); nothing here was tuned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sleeve: str
    title: str
    #: epic -> the market feed's file for it, as ``group/SYMBOL``.
    epics: dict[str, str]
    lookback_days: int = Field(ge=1)
    #: Weekday of the decision bar's UTC day, Monday = 0 ... Sunday = 6.
    decision_weekday: int = Field(ge=0, le=6)
    atr_period: int = Field(ge=2)
    stop_atr_multiplier: Decimal = Field(gt=0)
    reward_risk: Decimal = Field(gt=0)
    max_hold_hours: int = Field(ge=1)


def load_spec(path: Path = SETUPS_FILE) -> WeeklyMomentumSpec:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or SETUP_KEY not in doc:
        raise SetupError(f"{path.name} has no {SETUP_KEY} setup")
    return WeeklyMomentumSpec.model_validate(doc[SETUP_KEY])


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def load_daily(path: Path, today: date, *, weekend_closed: bool = False) -> list[Bar]:
    """The feed's completed daily bars, oldest first; today's bar is still forming and is left
    out. For a market closed at the weekend (``weekend_closed``) the short Sunday-evening bar is
    folded into Monday's; the crypto CFDs quote every day and keep every bar."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["series"]["1d"]
    except (OSError, KeyError, ValueError) as exc:
        raise SetupError(f"no daily bars in {path}") from exc
    bars: list[Bar] = []
    stub: Bar | None = None
    for row in rows:
        day = datetime.fromtimestamp(int(row[0]), UTC).date()
        if day >= today:
            break
        o, h, low, c = (Decimal(str(v)) for v in row[1:5])
        bar = Bar(day, o, h, low, c)
        if weekend_closed and day.weekday() >= 5:
            stub = bar if stub is None else _join(stub, bar)
            continue
        bars.append(bar if stub is None else _join(stub, bar))
        stub = None
    return bars


def _join(first: Bar, second: Bar) -> Bar:
    return Bar(
        second.day,
        first.open,
        max(first.high, second.high),
        min(first.low, second.low),
        second.close,
    )


def _q(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN, context=_CTX)


def atr(bars: Sequence[Bar], period: int) -> Decimal:
    """Wilder's ATR as ``daily-v1`` computes it: seeded with the mean of the first ``period``
    true ranges, then ``(avg * (n - 1) + tr) / n``, each step quantised to 12 dp."""
    ranges = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)) for a, b in pairwise(bars)
    ]
    if len(ranges) < period:
        raise SetupError(f"{len(ranges)} true ranges, ATR({period}) needs {period}")
    average = _q(sum(ranges[:period], Decimal(0)) / period)
    for value in ranges[period:]:
        average = _q((average * (period - 1) + value) / period)
    return average


def contiguous_tail(bars: Sequence[Bar]) -> list[Bar]:
    """The newest run of bars on consecutive UTC days: a missing day restarts the indicators,
    as the trial's features do."""
    tail: list[Bar] = []
    for bar in reversed(bars):
        if tail and (tail[-1].day - bar.day).days != 1:
            break
        tail.append(bar)
    return list(reversed(tail))


@dataclass(frozen=True)
class Reading:
    """One epic on its last completed daily bar."""

    epic: str
    day: date
    close: Decimal
    #: The close against the close ``lookback_days`` earlier, minus one.
    change: Decimal
    atr: Decimal
    #: True when the bar is the decision bar (the week's Sunday): then ``direction`` is traded.
    decision_bar: bool
    direction: Literal["LONG", "SHORT"] | None


def read_epic(epic: str, bars: Sequence[Bar], spec: WeeklyMomentumSpec) -> Reading:
    run = contiguous_tail(bars)
    need = max(spec.lookback_days + 1, spec.atr_period + 1)
    if len(run) < need:
        raise SetupError(f"{epic}: {len(run)} consecutive daily bars, the rule needs {need}")
    close, then = run[-1].close, run[-1 - spec.lookback_days].close
    change = close / then - 1
    direction: Literal["LONG", "SHORT"] | None = (
        "LONG" if change > 0 else "SHORT" if change < 0 else None
    )
    return Reading(
        epic=epic,
        day=run[-1].day,
        close=close,
        change=change,
        atr=atr(run, spec.atr_period),
        decision_bar=run[-1].day.weekday() == spec.decision_weekday,
        direction=direction,
    )


@dataclass(frozen=True)
class Ticket:
    """What a signal becomes under the sleeve's limits."""

    status: Literal["TRADE", "TOO_LARGE", "GROUP_BUSY"]
    direction: Literal["LONG", "SHORT"]
    size: Decimal
    stop: Decimal
    target: Decimal
    risk_usd: Decimal
    note: str


def ticket(
    reading: Reading,
    rule: EpicRule,
    spec: WeeklyMomentumSpec,
    risk_budget_usd: Decimal,
) -> Ticket:
    """Size a signal: the rule's stop, the largest size inside the risk budget and the epic's
    range. When even the minimum size risks more than the budget the trade is not taken: the
    stop is the rule's and is not pulled in to make a contract fit."""
    if reading.direction is None:
        raise SetupError(f"{reading.epic}: no direction to trade")
    step = Decimal(1).scaleb(-rule.decimals)
    distance = spec.stop_atr_multiplier * reading.atr
    long = reading.direction == "LONG"
    # the stop rounds away from the close and the target towards it, as the trial's rule
    if long:
        stop = (reading.close - distance).quantize(step, rounding=ROUND_FLOOR)
        risk = reading.close - stop
        target = (reading.close + spec.reward_risk * risk).quantize(step, rounding=ROUND_FLOOR)
    else:
        stop = (reading.close + distance).quantize(step, rounding=ROUND_CEILING)
        risk = stop - reading.close
        target = (reading.close - spec.reward_risk * risk).quantize(step, rounding=ROUND_CEILING)
    size = (risk_budget_usd / risk).quantize(rule.min_size, rounding=ROUND_FLOOR)
    size = min(size, rule.max_size)
    if size < rule.min_size:
        planned = risk * rule.min_size
        return Ticket(
            "TOO_LARGE",
            reading.direction,
            rule.min_size,
            stop,
            target,
            planned,
            f"the minimum size {rule.min_size} risks USD {planned:.2f}, over the "
            f"USD {risk_budget_usd:.2f} limit",
        )
    return Ticket("TRADE", reading.direction, size, stop, target, risk * size, "inside the limit")


# --------------------------------------------------------------------------- the scan as a file
SCAN_VERSION = 2
#: A feed that has not refreshed for this many of its own intervals is stale.
STALE_AFTER_INTERVALS = 3


def next_decision(now: datetime, weekday: int) -> datetime:
    """The next decision time: the end of the next bar whose UTC day is ``weekday`` (for Sunday,
    the coming Monday 00:00 UTC). At the decision instant itself it is that instant."""
    day = (now - timedelta(seconds=1)).date()
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)


def feed_state(market_root: Path, now: datetime) -> tuple[dict[str, Any], dict[str, Decimal]]:
    """What the market feed says about itself (the CFD group's status and last refresh) and the
    last price of each of its symbols. A missing or unreadable index is a stale feed."""
    try:
        index = json.loads((market_root / "index.json").read_text(encoding="utf-8"))
        group = next(g for g in index["groups"] if g["id"] == "cfd")
        updated = datetime.fromisoformat(str(group["updated_at"]).replace("Z", "+00:00"))
        every = int(index.get("every_minutes", 5))
    except (OSError, KeyError, ValueError, StopIteration, TypeError):
        return {"status": "missing", "updated_at": None, "age_minutes": None, "stale": True}, {}
    age = int((now - updated).total_seconds() // 60)
    state = {
        "status": group.get("status"),
        "updated_at": group["updated_at"],
        "age_minutes": age,
        "stale": group.get("status") != "ok" or age > STALE_AFTER_INTERVALS * every,
    }
    prices: dict[str, Decimal] = {}
    for instrument in group.get("instruments", []):
        if instrument.get("last_price") is not None:
            prices[str(instrument["symbol"])] = Decimal(str(instrument["last_price"]))
    return state, prices


def scan(
    spec: WeeklyMomentumSpec,
    journals: Mapping[str, PracticeJournal],
    market_root: Path,
    now: datetime,
) -> dict[str, Any]:
    """The setup read on the last completed daily bars, and every open practice trade against
    its stop, target and time stop, as one JSON-ready document. ``journals`` maps each sleeve's
    name to its journal; the setup's own sleeve gives the equity and the risk limit."""
    journal = journals[spec.sleeve]
    equity = journal.sleeve_equity()
    budget = equity * journal.rules.risk_fraction
    today = now.date()
    expected = today - timedelta(days=1)
    feed, last_prices = feed_state(market_root, now)
    readings: list[Reading] = [
        read_epic(epic, load_daily(market_root / f"{feed_file}.json", today), spec)
        for epic, feed_file in spec.epics.items()
    ]
    rows: list[dict[str, Any]] = []
    taken = False
    for r in sorted(readings, key=lambda x: (-abs(x.change), x.epic)):
        rule = journal.rules.epic(r.epic)
        places = rule.decimals
        row: dict[str, Any] = {
            "epic": r.epic,
            "day": r.day.isoformat(),
            "stale": r.day < expected,
            "close": f"{r.close:.{places}f}",
            "change": f"{r.change:.6f}",
            "atr": f"{r.atr:.{places}f}",
            "direction": r.direction,
            "decision_bar": r.decision_bar,
            "signal": r.decision_bar and r.direction is not None,
            "ticket": None,
        }
        if row["signal"]:
            t = ticket(r, rule, spec, budget)
            status, note = t.status, t.note
            conflict = journal.group_conflict(r.epic)
            if t.status == "TRADE" and conflict:
                status = "GROUP_BUSY"
                note = (
                    f"the {conflict[0]} group holds the open trade {conflict[1]}: close it if "
                    "its week is over, then take this one"
                )
            elif t.status == "TRADE" and taken:
                status, note = (
                    "GROUP_BUSY",
                    "one crypto trade at a time: the larger move goes first",
                )
            taken = taken or t.status == "TRADE"
            row["ticket"] = {
                "status": status,
                "direction": t.direction,
                "size": str(t.size),
                "stop": str(t.stop),
                "target": str(t.target),
                "risk_usd": f"{t.risk_usd:.2f}",
                "max_hold_hours": spec.max_hold_hours,
                "note": note,
            }
        rows.append(row)
    rows.sort(key=lambda row: list(spec.epics).index(row["epic"]))
    open_trades: list[dict[str, Any]] = []
    for sleeve, book in journals.items():
        for trade_id in book.open_trades():
            plan = book.plan(trade_id)
            if plan is None:
                continue
            p = plan["payload"]
            terms = book.in_force(trade_id)
            due = book.due_at(trade_id)
            side = 1 if p["direction"] == "BUY" else -1
            last = last_prices.get(p["epic"])
            stop = Decimal(terms["stop"]) if terms["stop"] else None
            target = Decimal(terms["target"]) if terms["target"] else None
            open_trades.append(
                {
                    "sleeve": sleeve,
                    "trade_id": trade_id,
                    "epic": p["epic"],
                    "direction": p["direction"],
                    "size": p["size"],
                    "entry": p["entry"],
                    "stop": terms["stop"],
                    "target": terms["target"],
                    "due_at": iso_utc(due) if due is not None else None,
                    "overdue": due is not None and due <= now,
                    "last_price": str(last) if last is not None else None,
                    "beyond_stop": (
                        last is not None and stop is not None and (last - stop) * side <= 0
                    ),
                    "at_target": (
                        last is not None and target is not None and (last - target) * side >= 0
                    ),
                    "exit_signal": None,
                }
            )
    return {
        "version": SCAN_VERSION,
        "generated_at": iso_utc(now),
        "setup": SETUP_KEY,
        "title": spec.title,
        "sleeve": spec.sleeve,
        "equity_usd": f"{equity:.2f}",
        "risk_limit_usd": f"{budget:.2f}",
        "expected_bar_day": expected.isoformat(),
        "next_decision_at": iso_utc(next_decision(now, spec.decision_weekday)),
        "feed": feed,
        "params": {
            "lookback_days": spec.lookback_days,
            "atr_period": spec.atr_period,
            "stop_atr_multiplier": str(spec.stop_atr_multiplier),
            "reward_risk": str(spec.reward_risk),
        },
        "readings": rows,
        "open_trades": open_trades,
    }


def render(doc: Mapping[str, Any]) -> list[str]:
    """The scan as the lines `trader journal scan` prints."""
    params = doc["params"]
    lines = [
        f"{doc['title']}, sleeve {doc['sleeve']}: equity USD {doc['equity_usd']}, "
        f"risk limit USD {doc['risk_limit_usd']} a trade; next decision {doc['next_decision_at']}"
    ]
    feed = doc["feed"]
    if feed["stale"]:
        lines.append(
            f"MARKET FEED STALE: status {feed['status']}, last refresh {feed['updated_at']}; "
            "the readings below may be old"
        )
    for r in doc["readings"]:
        change = Decimal(r["change"]) * 100
        lines.append(
            f"{r['epic']:<7} {r['day']} close {r['close']}  {params['lookback_days']}-day "
            f"{change:+.2f}% ({(r['direction'] or 'flat').lower()})  ATR({params['atr_period']}) "
            f"{r['atr']}"
            + ("  DECISION BAR" if r["decision_bar"] else "")
            + (f"  OLD BAR: expected {doc['expected_bar_day']}" if r["stale"] else "")
        )
        t = r["ticket"]
        if t is None:
            continue
        verb = "buy" if t["direction"] == "LONG" else "sell short"
        head = f"  SIGNAL: {verb}" if t["status"] == "TRADE" else "  SIGNAL, not taken:"
        lines.append(
            f"{head} {t['size']} {r['epic']} near {r['close']}, stop {t['stop']}, "
            f"target {t['target']}, risk USD {t['risk_usd']}, hold at most "
            f"{t['max_hold_hours']} h ({t['note']})"
        )
    for o in doc["open_trades"]:
        flags = [
            name
            for name, on in (
                ("OVERDUE: close it", o["overdue"]),
                ("PRICE BEYOND STOP: check the broker", o["beyond_stop"]),
                ("PRICE AT TARGET: check the broker", o["at_target"]),
                ("exit signal: close", o["exit_signal"]),
            )
            if on
        ]
        lines.append(
            f"open {o['sleeve']} {o['trade_id']}: {o['direction']} {o['size']} {o['epic']} "
            f"stop {o['stop']} target {o['target'] or '-'} time stop {o['due_at'] or '-'}"
            + ("  " + "; ".join(flags) if flags else "")
        )
    return lines


def write_scan(doc: Mapping[str, Any], path: Path) -> None:
    """Replace the scan file atomically (the console reads it while the watcher writes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
