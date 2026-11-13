"""Practice journal for the assistant's Capital.com DEMO trades (ADR-0044).

Every demo trade is planned, opened and closed through this append-only, hash-chained JSONL file
(the same chain as ``research/registry.py``). The journal is the practice database: why each
trade was taken, its stop and risk, what the broker filled, how it ended and what was learned.
It holds no credentials and no account numbers. Demo results are practice, never evidence.

Events: PLAN (before any order preview; validated against configs/practice/capital_demo.yaml),
OPEN, AMEND, CLOSE, CANCEL, NOTE, REVIEW.

Short-term rules (ADR-0049): every PLAN carries a target no further than a fixed multiple of its
planned risk, every open trade has a time stop, and an overdue trade blocks the next PLAN.

Two writers (ADR-0056): a chat (`trader journal add`) and the practice trader
(``trader.practice.trader``). Every append takes the journal's lock file, reads the chain again
and only then checks and writes, so neither can write on a chain the other has moved. A PLAN of
the trader carries its setup's key; open trades, entries a day and the one-trade-per-group rule
are counted per setup, and the trades planned by hand count together as one.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trader.domain.hashing import canonical_json, sha256_hex
from trader.domain.types import iso_utc
from trader.ops.clock import SleepingClock, SystemClock

REPO_ROOT = Path(__file__).resolve().parents[3]
RULES_DIR = REPO_ROOT / "configs" / "practice"
#: Practice sleeves (ADR-0046): name -> rules file. Each started at USD 100. Both trade on the
#: Capital.com demo account since ADR-0049; an epic belongs to exactly one sleeve.
VENUES = {"capital-demo": "capital_demo.yaml", "crypto-cfd": "crypto_cfd.yaml"}
DEFAULT_VENUE = "capital-demo"
GENESIS = "0" * 64
EventKind = Literal["PLAN", "OPEN", "AMEND", "CLOSE", "CANCEL", "NOTE", "REVIEW"]
#: What a PLAN may cite as evidence (ADR-0047): the same shape ``trader.intel.brief`` prints.
#: Kept here, not imported: the journal must not depend on the intel feed.
EVIDENCE_REF = re.compile(r"^(news|insider|premarket|snap):[A-Za-z0-9_.:@+-]{3,120}$")
MAX_EVIDENCE = 12
#: A setup's key in configs/practice/setups.yaml, as a trader's PLAN carries it.
SETUP_KEY = r"^[a-z][a-z0-9_]{2,47}$"
#: The journal's lock file is held for one append. One older than this was left by a writer that
#: died and is taken over; a live one is waited for this long before the append is refused.
LOCK_STALE_AFTER = timedelta(seconds=30)
LOCK_WAIT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05


class JournalError(ValueError):
    """A journal entry breaks the practice rules or the chain."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EpicRule(_Model):
    name: str
    quote_currency: Literal["USD", "EUR"]
    decimals: int = Field(ge=0, le=6)
    min_size: Decimal = Field(gt=0)
    max_size: Decimal = Field(gt=0)


class PracticeRules(_Model):
    venue: str
    account_currency: Literal["USD"]
    sleeve_start_usd: Decimal = Field(gt=0)
    risk_fraction: Decimal = Field(gt=0, le=Decimal("0.02"))
    floor_fraction: Decimal = Field(gt=0, lt=1)
    #: Open trades in the whole sleeve.
    max_open_trades: int = Field(ge=1)
    #: Open trades of one setup, and of the trades planned by hand taken together (ADR-0056).
    #: Left out, it is the sleeve's own limit.
    max_open_trades_per_setup: int | None = Field(default=None, ge=1)
    #: Trades opened in one UTC day by one setup, and by hand taken together.
    max_opens_per_utc_day: int = Field(ge=1)
    plan_window_minutes: int = Field(ge=1)
    long_only: bool
    #: Fee per side as a fraction of the traded value, where a venue charges one (zero on
    #: Capital.com: its cost is the spread). The console uses it to value open trades as if
    #: closed now; a CLOSE event still states the real result.
    taker_fee_rate: Decimal = Field(default=Decimal(0), ge=0, lt=Decimal("0.05"))
    #: Short-term trading (ADR-0049). The time stop: a trade is due this many hours after it
    #: opened (a PLAN may set a shorter ``max_hold_hours``), and no new trade is planned while
    #: one is overdue. ``None`` switches the check off.
    max_hold_hours: int | None = Field(default=None, ge=1)
    #: Every PLAN needs a target, at most this multiple of its planned risk from the entry.
    max_reward_risk: Decimal | None = Field(default=None, gt=0)
    journal: str
    epics: dict[str, EpicRule]
    #: Epics that move together, by group name. A sleeve holds one open trade per group: two
    #: indices bought on the same dip are one bet at twice the size.
    groups: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _groups(self) -> PracticeRules:
        seen: set[str] = set()
        for name, members in self.groups.items():
            for epic in members:
                if epic not in self.epics:
                    raise ValueError(f"group {name}: {epic} is not one of the sleeve's epics")
                if epic in seen:
                    raise ValueError(f"{epic} is in more than one group")
                seen.add(epic)
        return self

    def epic(self, epic: str) -> EpicRule:
        if epic not in self.epics:
            raise JournalError(f"{epic} is not a practice epic ({', '.join(self.epics)})")
        return self.epics[epic]

    def group_of(self, epic: str) -> str | None:
        return next((name for name, members in self.groups.items() if epic in members), None)

    @property
    def open_trades_per_setup(self) -> int:
        return min(self.max_open_trades_per_setup or self.max_open_trades, self.max_open_trades)


def load_rules(venue: str = DEFAULT_VENUE) -> PracticeRules:
    if venue not in VENUES:
        raise JournalError(f"unknown practice venue {venue!r} ({', '.join(VENUES)})")
    path = RULES_DIR / VENUES[venue]
    return PracticeRules.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class Plan(_Model):
    trade_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{2,64}$")
    epic: str
    direction: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "STOP"] = "MARKET"
    size: Decimal = Field(gt=0)
    entry: Decimal = Field(gt=0)
    stop: Decimal = Field(gt=0)
    target: Decimal | None = None
    fx_to_usd: Decimal = Field(default=Decimal(1), gt=0)
    thesis: str = Field(min_length=10)
    setup: str = Field(min_length=3)
    timeframe: str = Field(min_length=2)
    invalidation: str = Field(min_length=5)
    #: The plan's own time stop in hours after the open, when shorter than the sleeve's
    #: (ADR-0049). Left out of the journal line when not given.
    max_hold_hours: int | None = Field(default=None, ge=1)
    #: Optional references to what the plan relied on (ADR-0047): intel record ids and snapshot
    #: references as ``trader intel brief`` prints them. Never required for an order, and a plan
    #: without it is stored exactly as before (``None`` is left out of the journal line).
    evidence: tuple[str, ...] | None = Field(default=None, max_length=MAX_EVIDENCE)
    #: The practice trader's plans only (ADR-0056): the setup that produced the plan, as keyed in
    #: configs/practice/setups.yaml. A plan made by hand has none and is stored as before.
    setup_key: str | None = Field(default=None, pattern=SETUP_KEY)
    #: The price the rule measured its stop and target from (the decision bar's close), when the
    #: order is entered a little later at another price. The target's distance is then judged
    #: from here; the planned risk is always the entry's distance to the stop.
    reference: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _evidence(self) -> Plan:
        for ref in self.evidence or ():
            if not EVIDENCE_REF.fullmatch(ref):
                raise JournalError(
                    "evidence must be references such as news:alpaca_news:63087291 or "
                    "snap:stocks:NVDA@2026-11-10T15:01:07Z"
                )
        if self.evidence is not None and len(set(self.evidence)) != len(self.evidence):
            raise JournalError("evidence references must not repeat")
        return self

    @model_validator(mode="after")
    def _sides(self) -> Plan:
        if self.direction == "BUY" and self.stop >= self.entry:
            raise JournalError("a BUY needs its stop below the entry")
        if self.direction == "SELL" and self.stop <= self.entry:
            raise JournalError("a SELL needs its stop above the entry")
        if self.target is not None:
            if self.direction == "BUY" and self.target <= self.entry:
                raise JournalError("a BUY target must be above the entry")
            if self.direction == "SELL" and self.target >= self.entry:
                raise JournalError("a SELL target must be below the entry")
        return self

    @property
    def risk_usd(self) -> Decimal:
        return abs(self.entry - self.stop) * self.size * self.fx_to_usd


class Open(_Model):
    trade_id: str
    deal_id: str
    deal_reference: str | None = None
    fill_level: Decimal = Field(gt=0)
    size: Decimal = Field(gt=0)
    status: str = "ACCEPTED"


class Amend(_Model):
    trade_id: str
    deal_id: str
    changes: dict[str, str]
    reason: str = Field(min_length=5)


class Close(_Model):
    trade_id: str
    deal_id: str
    level: Decimal = Field(gt=0)
    #: SIGNAL: the setup's own exit rule closed the trade (ADR-0056).
    reason: Literal["TARGET", "STOP", "MANUAL", "TIME", "INVALIDATED", "SIGNAL", "OTHER"]
    #: The trade's net result in USD: after fees and financing. The sleeve compounds on this.
    pnl_usd: Decimal
    #: Informational parts of that result: commissions/fees paid and overnight financing.
    fees_usd: Decimal = Field(default=Decimal(0), ge=0)
    financing_usd: Decimal = Decimal(0)
    notes: str = ""


class Cancel(_Model):
    trade_id: str
    reason: str = Field(min_length=3)


class Note(_Model):
    text: str = Field(min_length=3)
    trade_id: str | None = None


class Review(_Model):
    trade_id: str
    lesson: str = Field(min_length=10)
    followed_plan: bool


MODELS: dict[str, type[_Model]] = {
    "PLAN": Plan,
    "OPEN": Open,
    "AMEND": Amend,
    "CLOSE": Close,
    "CANCEL": Cancel,
    "NOTE": Note,
    "REVIEW": Review,
}


def _num(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _amount(value: str, what: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise JournalError(f"{what} {value!r} is not a number") from exc
    if not amount.is_finite() or amount <= 0:
        raise JournalError(f"{what} {value!r} must be a positive number")
    return amount


def _hours(value: str | int) -> int:
    try:
        hours = int(str(value))
    except ValueError as exc:
        raise JournalError(f"max_hold_hours {value!r} is not a whole number of hours") from exc
    if hours < 1:
        raise JournalError("max_hold_hours must be at least 1")
    return hours


class PracticeJournal:
    """The hash-chained journal file; every append is validated first."""

    def __init__(
        self,
        path: Path | None = None,
        rules: PracticeRules | None = None,
        venue: str = DEFAULT_VENUE,
        clock: SleepingClock | None = None,
    ) -> None:
        self.rules = rules or load_rules(venue)
        self.path = path or (REPO_ROOT / self.rules.journal)
        #: Wall time for the lock file only; an event's time is always the caller's ``now``.
        self._clock: SleepingClock = clock or SystemClock()
        self.events: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------ chain
    def _load(self) -> None:
        events: list[dict[str, Any]] = []
        if not self.path.exists():
            self.events = events
            return
        prev = GENESIS
        for line_no, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            event = json.loads(raw)
            body = {k: v for k, v in event.items() if k != "record_hash"}
            if body.get("prev_hash") != prev:
                raise JournalError(f"journal chain broken at line {line_no}")
            if sha256_hex(canonical_json(body)) != event.get("record_hash"):
                raise JournalError(f"journal record altered at line {line_no}")
            if body.get("seq") != len(events) + 1:
                raise JournalError(f"journal sequence gap at line {line_no}")
            prev = event["record_hash"]
            events.append(event)
        self.events = events

    def append(
        self, kind: str, payload: Mapping[str, Any], now: datetime, actor: str = "claude"
    ) -> dict[str, Any]:
        if kind not in MODELS:
            raise JournalError(f"unknown journal event {kind!r} ({', '.join(MODELS)})")
        model = MODELS[kind].model_validate(dict(payload))
        with self._writer_lock():
            self._load()  # another writer may have moved the chain since this object read it
            extra = self._check(kind, model, now)
            data = json.loads(canonical_json(model.model_dump(mode="json", exclude_none=True)))
            body = {
                "seq": len(self.events) + 1,
                "event": kind,
                "at": iso_utc(now),
                "actor": actor,
                "venue": self.rules.venue,
                "account_currency": self.rules.account_currency,
                "payload": {**data, **extra},
                "prev_hash": self.events[-1]["record_hash"] if self.events else GENESIS,
            }
            record = {**body, "record_hash": sha256_hex(canonical_json(body))}
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(canonical_json(record) + "\n")
            self.events.append(record)
        return record

    def refresh(self) -> None:
        """Read the file again: what another writer appended since this object was built."""
        self._load()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        """Hold the journal for one append. A lock another writer holds is waited for; one left
        by a writer that died is taken over; a journal that stays busy refuses the append."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        path = self.lock_path
        waited = 0.0
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    taken = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                except OSError:
                    continue  # released between the two calls
                if self._clock.now() - taken >= LOCK_STALE_AFTER:
                    path.unlink(missing_ok=True)  # a writer that died; take over
                    continue
            except PermissionError:
                pass  # Windows: the other writer is deleting its lock this instant
            if waited >= LOCK_WAIT_SECONDS:
                raise JournalError(
                    f"the journal {self.path.name} is busy: another writer holds its lock"
                )
            self._clock.sleep(LOCK_POLL_SECONDS)
            waited += LOCK_POLL_SECONDS
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps({"pid": os.getpid()}) + "\n")
            yield
        finally:
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ rules
    def _check(self, kind: str, model: _Model, now: datetime) -> dict[str, str]:
        if isinstance(model, Plan):
            rule = self.rules.epic(model.epic)
            if not rule.min_size <= model.size <= rule.max_size:
                raise JournalError(
                    f"{model.epic} size {model.size} outside {rule.min_size}..{rule.max_size}"
                )
            if rule.quote_currency == "USD" and model.fx_to_usd != 1:
                raise JournalError(f"{model.epic} is quoted in USD: fx_to_usd must be 1")
            if rule.quote_currency != "USD" and model.fx_to_usd == 1:
                raise JournalError(
                    f"{model.epic} is quoted in {rule.quote_currency}: give fx_to_usd"
                )
            if self.rules.long_only and model.direction != "BUY":
                raise JournalError(f"{self.rules.venue} is long only")
            equity = self.sleeve_equity()
            if equity <= self.floor_usd:
                raise JournalError(
                    f"sleeve equity USD {_num(equity)} is at or below its floor "
                    f"USD {_num(self.floor_usd)}: no new trades (owner review)"
                )
            max_risk = equity * self.rules.risk_fraction
            if model.risk_usd > max_risk:
                raise JournalError(
                    f"planned risk USD {_num(model.risk_usd)} exceeds {_num(max_risk)} "
                    f"({self.rules.risk_fraction:%} of sleeve equity USD {_num(equity)})"
                )
            if self.plan(model.trade_id) is not None:
                raise JournalError(f"trade {model.trade_id} is already planned")
            # the trader's rule measured stop and target from its decision price (ADR-0056)
            basis = model.reference if model.reference is not None else model.entry
            if (model.stop - basis) * (model.stop - model.entry) <= 0:
                raise JournalError(
                    "the reference and the entry must be on the same side of the stop"
                )
            self._check_target(model.target, basis, abs(basis - model.stop))
            self._check_hold(model.max_hold_hours)
            overdue = self.overdue(now)
            if overdue:
                late, due = overdue[0]
                raise JournalError(
                    f"trade {late} is past its time stop ({iso_utc(due)}): "
                    "close it before planning another"
                )
            self._check_group(model.epic, model.setup_key)
            return {"risk_usd": _num(model.risk_usd), "sleeve_equity_usd": _num(equity)}
        trade_id = getattr(model, "trade_id", None)
        plan = self.plan(trade_id)
        if kind in ("OPEN", "AMEND", "CLOSE", "CANCEL", "REVIEW") and plan is None:
            raise JournalError(f"trade {trade_id} has no PLAN")
        if isinstance(model, Amend) and plan is not None:
            # A changed target or time stop stays inside the short-term rules, measured against
            # the entry and risk as planned (a stop moved to break-even must not shrink them).
            planned = plan["payload"]
            entry = Decimal(planned["entry"])
            if "target" in model.changes:
                self._check_target(
                    _amount(model.changes["target"], "target"),
                    entry,
                    abs(entry - Decimal(planned["stop"])),
                )
            if "max_hold_hours" in model.changes:
                self._check_hold(_hours(model.changes["max_hold_hours"]))
        if isinstance(model, Open):
            if trade_id in self.open_trades():
                raise JournalError(f"trade {trade_id} is already open")
            key = plan["payload"].get("setup_key") if plan is not None else None
            whose = f" of {key}" if key else ""
            if len(self.open_trades_of(key)) >= self.rules.open_trades_per_setup:
                raise JournalError(
                    f"{self.rules.open_trades_per_setup} practice trades{whose} are already open"
                )
            if len(self.open_trades()) >= self.rules.max_open_trades:
                raise JournalError(
                    f"the sleeve's {self.rules.max_open_trades} open trades are all in use"
                )
            if self.opens_on(now.date(), key) >= self.rules.max_opens_per_utc_day:
                raise JournalError(f"{self.rules.max_opens_per_utc_day} trades{whose} opened today")
            if plan is not None:
                self._check_group(plan["payload"]["epic"], key)
        if isinstance(model, Close | Amend) and trade_id not in self.open_trades():
            raise JournalError(f"trade {trade_id} is not open")
        return {}

    # ------------------------------------------------------------------ one trade per group
    def group_conflict(self, epic: str, setup_key: str | None = None) -> tuple[str, str] | None:
        """The group ``epic`` belongs to and the open trade of the same setup already in it, if
        there is one. ``setup_key`` ``None`` means the trades planned by hand (ADR-0056)."""
        group = self.rules.group_of(epic)
        if group is None:
            return None
        for trade_id in self.open_trades_of(setup_key):
            plan = self.plan(trade_id)
            if plan is not None and plan["payload"]["epic"] in self.rules.groups[group]:
                return group, trade_id
        return None

    def _check_group(self, epic: str, setup_key: str | None = None) -> None:
        conflict = self.group_conflict(epic, setup_key)
        if conflict is not None:
            group, trade_id = conflict
            raise JournalError(
                f"{epic} is in the group {group} with the open trade {trade_id}: "
                "one open trade per group"
            )

    # ------------------------------------------------------------------ short-term rules
    def _check_target(self, target: Decimal | None, entry: Decimal, risk: Decimal) -> None:
        """ADR-0049: a target is required, no further than ``max_reward_risk`` times the planned
        risk."""
        rules = self.rules
        if rules.max_reward_risk is None:
            return
        if target is None:
            raise JournalError("a short-term plan needs a target")
        reward = abs(target - entry)
        if reward > rules.max_reward_risk * risk:
            raise JournalError(
                f"target {_num(target)} is {_num((reward / risk).quantize(Decimal('0.01')))} x "
                f"the planned risk: the most is {_num(rules.max_reward_risk)} x"
            )

    def _check_hold(self, hours: int | None) -> None:
        limit = self.rules.max_hold_hours
        if hours is not None and limit is not None and hours > limit:
            raise JournalError(f"max_hold_hours {hours} is over the sleeve's {limit} hours")

    def due_at(self, trade_id: str) -> datetime | None:
        """When an open trade reaches its time stop: its OPEN time plus the hold in force (the
        newest AMEND's, else the PLAN's, else the sleeve's). ``None`` without a time stop."""
        opened = next((e for e in self._of("OPEN") if e["payload"]["trade_id"] == trade_id), None)
        plan = self.plan(trade_id)
        if opened is None or plan is None:
            return None
        hours: int | None = plan["payload"].get("max_hold_hours")
        for e in self._of("AMEND"):
            changes = e["payload"]["changes"]
            if e["payload"]["trade_id"] == trade_id and "max_hold_hours" in changes:
                hours = _hours(changes["max_hold_hours"])
        if hours is None:
            hours = self.rules.max_hold_hours
        if hours is None:
            return None
        return datetime.fromisoformat(opened["at"].replace("Z", "+00:00")) + timedelta(hours=hours)

    def in_force(self, trade_id: str) -> dict[str, str | None]:
        """A trade's stop and target now: as planned, then as each AMEND changed them."""
        plan = self.plan(trade_id)
        payload = plan["payload"] if plan else {}
        terms: dict[str, str | None] = {k: payload.get(k) for k in ("stop", "target")}
        for e in self._of("AMEND"):
            if e["payload"]["trade_id"] == trade_id:
                terms.update({k: v for k, v in e["payload"]["changes"].items() if k in terms})
        return terms

    def overdue(self, now: datetime) -> list[tuple[str, datetime]]:
        """Open trades past their time stop with the time they were due, oldest first."""
        due = [(self.due_at(t), t) for t in self.open_trades()]
        return [(t, at) for at, t in sorted((d, t) for d, t in due if d is not None) if at <= now]

    # ------------------------------------------------------------------ sleeve
    @property
    def floor_usd(self) -> Decimal:
        return self.rules.sleeve_start_usd * self.rules.floor_fraction

    def sleeve_equity(self) -> Decimal:
        """The sleeve's realized equity: its start plus every closed trade's net result
        (``pnl_usd`` is net of fees and financing). No deposits exist; profits compound."""
        closed = sum((Decimal(e["payload"]["pnl_usd"]) for e in self._of("CLOSE")), Decimal(0))
        return self.rules.sleeve_start_usd + closed

    def summary(self) -> dict[str, Any]:
        closes = self._of("CLOSE")
        wins = sum(1 for e in closes if Decimal(e["payload"]["pnl_usd"]) > 0)
        return {
            "venue": self.rules.venue,
            "start_usd": _num(self.rules.sleeve_start_usd),
            "equity_usd": _num(self.sleeve_equity()),
            "closed_trades": len(closes),
            "winning_trades": wins,
            "open_trades": len(self.open_trades()),
            "last_event_at": self.events[-1]["at"] if self.events else None,
        }

    # ------------------------------------------------------------------ queries
    def _of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == kind]

    def plan(self, trade_id: str | None) -> dict[str, Any] | None:
        return next((e for e in self._of("PLAN") if e["payload"]["trade_id"] == trade_id), None)

    def open_trades(self) -> list[str]:
        opened = [e["payload"]["trade_id"] for e in self._of("OPEN")]
        closed = {e["payload"]["trade_id"] for e in self._of("CLOSE")}
        return [t for t in opened if t not in closed]

    def setup_of(self, trade_id: str | None) -> str | None:
        """The setup key of a trade's PLAN; ``None`` for a trade planned by hand."""
        plan = self.plan(trade_id)
        return plan["payload"].get("setup_key") if plan is not None else None

    def open_trades_of(self, setup_key: str | None) -> list[str]:
        """The open trades of one setup; of the trades planned by hand for ``None``."""
        return [t for t in self.open_trades() if self.setup_of(t) == setup_key]

    def opens_on(self, day: date, setup_key: str | None = None) -> int:
        """Trades one setup opened on a UTC day; those planned by hand for ``None``."""
        return sum(
            1
            for e in self._of("OPEN")
            if e["at"][:10] == day.isoformat()
            and self.setup_of(e["payload"]["trade_id"]) == setup_key
        )

    def latest_plan(self, epic: str, since: datetime) -> dict[str, Any] | None:
        """The newest PLAN made by hand for ``epic`` at or after ``since`` that is not yet opened
        or cancelled. The trader's plans are left out: the connector guard asks this before a
        chat's order, and a chat's order may not use a plan the trader is about to fill."""
        done = {e["payload"]["trade_id"] for e in self.events if e["event"] in ("OPEN", "CANCEL")}
        for e in reversed(self._of("PLAN")):
            if (
                e["payload"]["epic"] == epic
                and e["payload"].get("setup_key") is None
                and e["payload"]["trade_id"] not in done
                and e["at"] >= iso_utc(since)
            ):
                return e
        return None

    def recent_amend(self, since: datetime) -> bool:
        return any(e["at"] >= iso_utc(since) for e in self._of("AMEND"))

    def window_start(self, now: datetime) -> datetime:
        return now - timedelta(minutes=self.rules.plan_window_minutes)
