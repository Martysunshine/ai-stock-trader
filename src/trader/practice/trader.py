"""The practice trader (ADR-0056): registered trial rules on the Capital.com demo account.

``python -m trader.practice trade [--live]`` runs beside the stocks bot in its own window. No
language model, chat or connector is in its loop. Each setup in ``configs/practice/setups.yaml``
(``trader_setups``) runs the rule of a registered trial with that trial's own study file: the
rule specification, the instrument specifications, the sizing policy and the fee schedule.

**Each hour**, 20 seconds after the hour, it

1. closes every trade of its own whose rule says it is due (the hourly rule's flat hour, the
   weekly rule's week);
2. reads the broker's last hourly bid and ask bars for each epic, keeps the complete ones, and
   feeds them to a fresh rule. The hourly rule decides on the hour that just ended. A daily rule
   reads the sessions that have ended, built from the hours as the trial built them, and acts at
   the first tradeable open after its decision bar, as the trial filled at the next open: 00:00 UTC
   for the crypto CFDs, and for the indices the next open after the session, which for a Friday
   signal is the Sunday 23:00 UTC open. Its exit signal (the index dip's close above its 5-day
   average) closes the trade at the next open the same way. A rule's target that is only a cost
   gate (the index dip's) is not sent to the broker;
3. takes a signal on the bar that just ended through the same checks as the trial's engine, in
   the same order of priority: open trades and groups of the setup, entries a day, the quoted
   spread, the stop against the live price, and the trial's sizer at the live price. Then the
   practice journal checks the sleeve's rules (risk, floor, open trades, groups, time stop);
4. journals the PLAN, sends one market order with the stop and target held at the broker,
   journals a NOTE with the deal reference, waits for the confirmation and journals the OPEN.

**Every minute** it reads the broker's positions: a trade of its own that is no longer there was
closed by the broker at its stop or target and is journalled as such, at the broker's level.

**Safety.** One trader at a time (a lock file with its heartbeat). At start it compares every
open journal trade of both sleeves with the broker's positions; a PLAN left without an order is
cancelled, one whose order the broker confirmed is opened, a trade the broker no longer holds is
closed from the broker's record, and a broker position no journal knows stops all new entries.
An order is sent once: a lost answer is looked up, never sent again. Without ``--live`` it reads
and decides but journals and sends nothing.

Every decision, taken or not, goes to ``runs/practice/decisions.jsonl``; the state goes to
``runs/practice/trader.json`` for the console.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trader.accounting.fees import FeeSchedule, load_fee_schedule
from trader.brokers.catalog import load_instruments
from trader.config.policy import RiskPolicy, load_policy
from trader.data.calendars import BrokerCfdCalendar, SessionCalendar, Utc247Calendar
from trader.data.manifest import load_manifest
from trader.data.quote_bars import QuoteBar, daily_from_hourly
from trader.data.sources.capital_history import fetch_capital_quote_history
from trader.data.sources.capital_prices import CapitalPrices
from trader.data.sources.http import TransportError
from trader.domain.candidates import ReturnBasis
from trader.domain.instruments import CommodityInstrumentSpec
from trader.domain.types import Direction, iso_utc
from trader.ops.clock import SleepingClock
from trader.practice.capital_orders import CapitalOrders, Confirmation, OrderError, Position
from trader.practice.journal import (
    REPO_ROOT,
    RULES_DIR,
    VENUES,
    JournalError,
    PracticeJournal,
)
from trader.research.cfd_engine import CfdSignal, charge_instants
from trader.research.cfd_study import load_cfd_study, open_cfd_study
from trader.risk.sizing import ExposureState, SizingRequest, size_order
from trader.strategies.cfd_rules import (
    CfdHourlyChannelSpec,
    CfdSpec,
    build_cfd_rule,
    load_cfd_spec,
)

SETUPS_FILE = RULES_DIR / "setups.yaml"
STATE_DIR = REPO_ROOT / "runs" / "practice"
DECIDE_AFTER_SECONDS = 20
POLL_SECONDS = 60
CONFIRM_WAIT_SECONDS = 10
LOOKUP_WAIT_SECONDS = 60
#: The hourly rule needs 25 consecutive bars; more cost nothing and survive a missing hour.
HOURLY_BARS = 60
#: One page of the broker's prices.
PAGE_BARS = 1000
#: A lock whose heartbeat is older than this was left by a trader that died (it beats every
#: minute; the launcher restarts a killed trader and waits for this before it takes over).
LOCK_STALE_AFTER = timedelta(seconds=150)
ACTOR = "trader"


class TraderError(RuntimeError):
    """The trader cannot start or continue safely."""


# ----------------------------------------------------------------------------- setups
class TraderSetup(BaseModel):
    """A setup of ``trader_setups``. A ``tested`` or ``forward_test`` setup names a registered
    trial and its study file, which gives everything else. A ``practice`` setup (ADR-0060) runs a
    rule with no passing trial, as the stocks bot runs ``--practice``, and names its own rule,
    instrument, policy and fee files, its epics and its calendar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,47}$")
    title: str
    sleeve: str
    status: Literal["tested", "forward_test", "practice"]
    trial: str | None = Field(default=None, pattern=r"^T-\d{4}$")
    study: str | None = None
    enabled: bool
    loss_cap_usd: Decimal | None = Field(default=None, gt=0)
    review_after_trades: int | None = Field(default=None, ge=1)
    # practice setups only
    spec: str | None = None
    instruments: str | None = None
    policy: str | None = None
    fees: str | None = None
    calendar: Literal["BROKER_CFD", "UTC_24_7"] | None = None
    epics: tuple[str, ...] | None = None

    @property
    def label(self) -> str:
        """The trial it runs, or 'practice' (journal plans and the console say which)."""
        return self.trial or "practice"


def load_trader_setups(path: Path = SETUPS_FILE) -> list[TraderSetup]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = doc.get("trader_setups") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise TraderError(f"{path.name} has no trader_setups list")
    setups = [TraderSetup.model_validate(r) for r in rows]
    keys = [s.key for s in setups]
    if len(set(keys)) != len(keys):
        raise TraderError("a setup key appears twice")
    for s in setups:
        if s.sleeve not in VENUES:
            raise TraderError(f"{s.key}: unknown sleeve {s.sleeve}")
        if s.status != "tested" and s.loss_cap_usd is None:
            raise TraderError(f"{s.key}: a {s.status.replace('_', ' ')} needs a loss cap")
        practice = (s.spec, s.instruments, s.policy, s.fees, s.calendar, s.epics)
        if s.status == "practice":
            if s.trial is not None or s.study is not None or any(v is None for v in practice):
                raise TraderError(
                    f"{s.key}: a practice setup names spec, instruments, policy, fees, calendar "
                    "and epics, and no trial"
                )
        elif s.trial is None or s.study is None or any(v is not None for v in practice):
            raise TraderError(f"{s.key}: a {s.status} setup names its trial and study only")
    return setups


@dataclass
class Running:
    """A setup with its rule, instruments, sizing policy and fees loaded: from its trial's study
    file, or for a practice setup from the files it names."""

    setup: TraderSetup
    spec: CfdSpec
    #: epic -> the instrument specification
    instruments: dict[str, CommodityInstrumentSpec]
    hourly: bool
    calendar: Literal["BROKER_CFD", "UTC_24_7"]
    policy: RiskPolicy
    fees: FeeSchedule
    #: Days of hourly history a daily rule warms up on.
    warmup_days: int
    last: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.setup.key

    def iid(self, epic: str) -> str:
        return self.instruments[epic].identity.instrument_id

    def deadline(self, decided_at: datetime, entered_at: datetime) -> datetime:
        """When a trade must be closed, as the engine timed it: the hourly rule's flat hour on
        its decision day, else the hour it was entered plus the rule's hold."""
        spec = self.spec
        if isinstance(spec, CfdHourlyChannelSpec):
            return datetime.combine(decided_at.date(), time(spec.flat_hour_utc), tzinfo=UTC)
        return hour_floor(entered_at) + timedelta(hours=spec.max_hold_hours)

    def session(self, t: datetime) -> date:
        """The daily session an instant belongs to: its UTC day, with Saturday and Sunday joined
        to Monday on a calendar closed at the weekend (as ``daily_from_hourly`` builds bars)."""
        day = t.date()
        if self.calendar == "BROKER_CFD" and day.weekday() >= 5:
            day += timedelta(days=7 - day.weekday())
        return day


def load_running(setup: TraderSetup, repo_root: Path = REPO_ROOT) -> Running:
    if setup.status == "practice":
        return _load_practice(setup, repo_root)
    if setup.study is None or setup.trial is None:
        raise TraderError(f"{setup.key}: a {setup.status} setup names its trial and study")
    config = load_cfd_study(repo_root / setup.study)
    manifest = load_manifest(repo_root / "research" / "data_manifest.json")
    ctx = open_cfd_study(repo_root, config, manifest)
    trials = [t for t in config.trials if t.trial_id == setup.trial]
    if not trials:
        raise TraderError(f"{setup.key}: {setup.study} has no trial {setup.trial}")
    instruments = {
        ctx.specs[i.instrument_id].identity.broker_asset_id: ctx.specs[i.instrument_id]
        for i in config.instruments
    }
    return Running(
        setup=setup,
        spec=ctx.rules[setup.trial],
        instruments=instruments,
        hourly=config.bars.decision_minutes == 60,
        calendar=config.bars.calendar,
        policy=ctx.policy.policy,
        fees=ctx.fees,
        warmup_days=config.warmup_days,
    )


def _load_practice(setup: TraderSetup, repo_root: Path) -> Running:
    spec_file, inst_file, policy_file, fee_file = (
        setup.spec,
        setup.instruments,
        setup.policy,
        setup.fees,
    )
    if not (spec_file and inst_file and policy_file and fee_file and setup.epics):
        raise TraderError(f"{setup.key}: a practice setup names its rule, files and epics")
    if setup.calendar is None:
        raise TraderError(f"{setup.key}: a practice setup names its calendar")
    catalogue = load_instruments(repo_root / inst_file)
    instruments: dict[str, CommodityInstrumentSpec] = {}
    for epic in setup.epics:
        spec = catalogue.get(f"CAPITAL:{epic}")
        if not isinstance(spec, CommodityInstrumentSpec):
            raise TraderError(f"{setup.key}: {epic} has no CFD spec in {inst_file}")
        instruments[epic] = spec
    fees = load_fee_schedule(repo_root / fee_file)
    if not fees.known:
        raise TraderError(f"{setup.key}: fee schedule {fees.schedule_id} is unknown")
    rule = load_cfd_spec(repo_root / spec_file)
    return Running(
        setup=setup,
        spec=rule,
        instruments=instruments,
        hourly=isinstance(rule, CfdHourlyChannelSpec),
        calendar=setup.calendar,
        policy=load_policy(repo_root / policy_file).policy,
        fees=fees,
        warmup_days=30,
    )


# ----------------------------------------------------------------------------- helpers
def hour_floor(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def next_run(now: datetime) -> datetime:
    """The next decision instant: 20 seconds after the next hour (or this one, if not passed)."""
    this = hour_floor(now) + timedelta(seconds=DECIDE_AFTER_SECONDS)
    return this if now < this else this + timedelta(hours=1)


def _direction(signal: CfdSignal) -> Literal["BUY", "SELL"]:
    return "BUY" if signal.direction is Direction.LONG else "SELL"


def trade_id_for(key: str, epic: str, decided_at: datetime) -> str:
    return f"{key}.{epic}.{decided_at.strftime('%Y%m%dT%H%MZ')}"


@dataclass(frozen=True)
class Decision:
    """One signal and what became of it."""

    setup: str
    epic: str
    decided_at: datetime
    direction: Literal["BUY", "SELL"] | None
    outcome: str
    detail: str
    trade_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "setup": self.setup,
            "epic": self.epic,
            "decided_at": iso_utc(self.decided_at),
            "direction": self.direction,
            "outcome": self.outcome,
            "detail": self.detail,
            "trade_id": self.trade_id,
        }


# ----------------------------------------------------------------------------- the trader
class PracticeTrader:
    def __init__(
        self,
        *,
        prices: CapitalPrices,
        orders: CapitalOrders,
        clock: SleepingClock,
        setups: Sequence[Running],
        journals: Mapping[str, PracticeJournal],
        live: bool,
        state_dir: Path = STATE_DIR,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.prices = prices
        self.orders = orders
        self.clock = clock
        self.setups = list(setups)
        self.journals = dict(journals)
        self.live = live
        self.state_dir = state_dir
        self.echo = echo
        self.started_at = clock.now()
        self.hedging = False
        self.entries_paused: str | None = None
        self.last_poll: datetime | None = None
        self.last_decision_hour: datetime | None = None
        self.errors: list[str] = []
        #: epic -> hourly bars by start: a daily rule's warm-up, read once and then topped up
        self.history: dict[str, dict[datetime, QuoteBar]] = {}

    # ------------------------------------------------------------------ journals
    def journal(self, setup: Running) -> PracticeJournal:
        return self.journals[setup.setup.sleeve]

    def _mine(self) -> list[tuple[PracticeJournal, str]]:
        """Open trades of this trader's setups, in every sleeve."""
        keys = {s.key for s in self.setups}
        out = []
        for j in self.journals.values():
            j.refresh()
            out += [(j, t) for t in j.open_trades() if j.setup_of(t) in keys]
        return out

    def _append(self, j: PracticeJournal, kind: str, payload: Mapping[str, Any]) -> None:
        j.append(kind, payload, self.clock.now(), actor=ACTOR)

    def forward_result(self, setup: Running) -> tuple[Decimal, int]:
        j = self.journal(setup)
        closes = [
            e
            for e in j.events
            if e["event"] == "CLOSE" and j.setup_of(e["payload"]["trade_id"]) == setup.key
        ]
        return sum((Decimal(e["payload"]["pnl_usd"]) for e in closes), Decimal(0)), len(closes)

    def switched_off(self, setup: Running) -> str | None:
        cap = setup.setup.loss_cap_usd
        if cap is None:
            return None
        result, _trades = self.forward_result(setup)
        if result < -cap:
            return f"forward result {result:.2f} USD is below the loss cap -{cap} (owner review)"
        return None

    # ------------------------------------------------------------------ start-up
    def reconcile(self) -> None:
        """Make the journals and the broker agree before any entry (module docstring)."""
        now = self.clock.now()
        self.hedging = self.orders.hedging()
        positions = self.orders.positions()
        held = {p.deal_id: p for p in positions}
        known: set[str] = set()
        keys = {s.key for s in self.setups}
        for j in self.journals.values():
            j.refresh()
            done = {e["payload"]["trade_id"] for e in j.events if e["event"] in ("OPEN", "CANCEL")}
            for e in j.events:
                if e["event"] != "PLAN" or e["payload"]["trade_id"] in done:
                    continue
                p = e["payload"]
                if p.get("setup_key") not in keys:
                    continue
                self._settle_plan(j, p, positions)
            for trade in j.open_trades():
                opened = next(
                    e
                    for e in j.events
                    if e["event"] == "OPEN" and e["payload"]["trade_id"] == trade
                )
                deal = opened["payload"]["deal_id"]
                known.add(deal)
                if deal not in held and j.setup_of(trade) in keys:
                    self._closed_by_broker(j, trade, deal, now)
        unknown = [p for p in positions if p.deal_id not in known]
        if unknown:
            names = ", ".join(f"{p.epic} {p.direction} {p.size} ({p.deal_id})" for p in unknown)
            self.entries_paused = (
                f"the broker holds positions no journal knows: {names}. No new entries until "
                "they are journalled or closed"
            )
            self.echo(f"ENTRIES PAUSED: {self.entries_paused}")
        else:
            self.entries_paused = None
        self.echo(
            f"reconciled: {len(positions)} broker position(s), hedging "
            f"{'on' if self.hedging else 'off'}"
        )

    def _settle_plan(
        self, j: PracticeJournal, plan: Mapping[str, Any], positions: Sequence[Position]
    ) -> None:
        """A PLAN without OPEN or CANCEL: the trader stopped between the two."""
        trade = plan["trade_id"]
        ref = next(
            (
                e["payload"]["text"].rsplit(" ", 1)[-1]
                for e in j.events
                if e["event"] == "NOTE"
                and e["payload"].get("trade_id") == trade
                and e["payload"]["text"].startswith("order sent, deal reference ")
            ),
            None,
        )
        if ref is None:
            self._append(j, "CANCEL", {"trade_id": trade, "reason": "no order was sent (restart)"})
            return
        try:
            confirmation: Confirmation | None = self.orders.confirm(ref)
        except (TransportError, OrderError):
            confirmation = None
        if confirmation is not None and confirmation.accepted and confirmation.deal_id:
            if any(p.deal_id == confirmation.deal_id for p in positions):
                self._open(j, plan, ref, confirmation)
                return
            self._open(j, plan, ref, confirmation)
            self._closed_by_broker(j, trade, confirmation.deal_id, self.clock.now())
            return
        reason = confirmation.reason if confirmation is not None else "no confirmation found"
        self._append(j, "CANCEL", {"trade_id": trade, "reason": f"order {ref}: {reason}"})

    # ------------------------------------------------------------------ the minute
    def poll(self) -> None:
        """Journal trades the broker closed at their stop or target."""
        mine = self._mine()
        if not mine:
            return
        held = {p.deal_id for p in self.orders.positions()}
        now = self.clock.now()
        for j, trade in mine:
            deal = self._deal_of(j, trade)
            if deal not in held:
                self._closed_by_broker(j, trade, deal, now)

    def close_due(self, now: datetime) -> None:
        """Close every trade of this trader whose rule says it is due."""
        by_key = {s.key: s for s in self.setups}
        for j, trade in self._mine():
            setup = by_key[j.setup_of(trade) or ""]
            plan = j.plan(trade)
            assert plan is not None
            decided = datetime.strptime(trade.rsplit(".", 1)[-1], "%Y%m%dT%H%MZ").replace(
                tzinfo=UTC
            )
            if setup.deadline(decided, self._opened_at(j, trade)) <= now:
                self._close(j, trade, "TIME")

    def _opened_at(self, j: PracticeJournal, trade: str) -> datetime:
        at = next(
            e["at"] for e in j.events if e["event"] == "OPEN" and e["payload"]["trade_id"] == trade
        )
        return datetime.fromisoformat(str(at).replace("Z", "+00:00"))

    def _deal_of(self, j: PracticeJournal, trade: str) -> str:
        return str(
            next(
                e["payload"]["deal_id"]
                for e in j.events
                if e["event"] == "OPEN" and e["payload"]["trade_id"] == trade
            )
        )

    def _close(self, j: PracticeJournal, trade: str, reason: str) -> None:
        deal = self._deal_of(j, trade)
        if not self.live:
            self.echo(f"DRY RUN: would close {trade} ({reason})")
            return
        try:
            ref = self.orders.close_position(deal)
        except TransportError as exc:
            if exc.status == 404:  # already gone: the broker closed it
                self._closed_by_broker(j, trade, deal, self.clock.now())
                return
            raise
        closed = self._wait_confirm(ref)
        if closed is None or closed.level is None:
            raise TraderError(f"close of {trade} ({ref}) not confirmed: check the demo account")
        pnl = closed.raw.get("profit")
        self._append(
            j,
            "CLOSE",
            {
                "trade_id": trade,
                "deal_id": deal,
                "level": str(closed.level),
                "reason": reason,
                "pnl_usd": str(pnl if pnl is not None else self._pnl(j, trade, closed.level)),
                "notes": f"closed by the trader, deal reference {ref}",
            },
        )
        self.echo(f"closed {trade} at {closed.level} ({reason}), {pnl} USD")

    def _pnl(self, j: PracticeJournal, trade: str, level: Decimal) -> Decimal:
        plan = j.plan(trade)
        opened = next(
            e for e in j.events if e["event"] == "OPEN" and e["payload"]["trade_id"] == trade
        )
        assert plan is not None
        side = 1 if plan["payload"]["direction"] == "BUY" else -1
        fill, size = Decimal(opened["payload"]["fill_level"]), Decimal(opened["payload"]["size"])
        fx = Decimal(plan["payload"].get("fx_to_usd", "1"))
        return ((level - fill) * size * side * fx).quantize(Decimal("0.0001"))

    def _closed_by_broker(self, j: PracticeJournal, trade: str, deal: str, now: datetime) -> None:
        """The broker no longer holds the position: journal its close from the activity."""
        opened = next(
            e for e in j.events if e["event"] == "OPEN" and e["payload"]["trade_id"] == trade
        )
        since = datetime.fromisoformat(opened["at"].replace("Z", "+00:00")) - timedelta(minutes=1)
        rows = self.orders.activity(since, now + timedelta(minutes=1))
        closing = [
            r
            for r in rows
            if r.get("dealId") == deal
            and r.get("type") == "POSITION"
            and (r.get("details") or {}).get("openPrice") is not None
        ]
        if not closing:
            self.entries_paused = (
                f"{trade}: the broker no longer holds {deal} and its activity has no close yet"
            )
            self.echo(f"WARNING: {self.entries_paused}")
            return
        row = closing[0]
        level = Decimal(str(row["details"]["level"]))
        source = str(row.get("source") or "")
        reason = {"SL": "STOP", "TP": "TARGET"}.get(source, "OTHER")
        self._append(
            j,
            "CLOSE",
            {
                "trade_id": trade,
                "deal_id": deal,
                "level": str(level),
                "reason": reason,
                "pnl_usd": str(self._pnl(j, trade, level)),
                "notes": f"closed by the broker (source {source or 'unknown'})",
            },
        )
        self.echo(f"{trade} closed by the broker at {level} ({reason})")

    # ------------------------------------------------------------------ the hour
    def _bars(
        self, epic: str, days: int, cache: dict[tuple[str, int], list[QuoteBar]]
    ) -> list[QuoteBar]:
        """The complete hourly bars of ``epic`` over the last ``days`` days (at least the last
        60 hours). Up to one page is read whole each hour; a longer warm-up is paged back once
        and then topped up with the newest page."""
        key = (epic, days)
        if key in cache:
            return cache[key]
        now = self.clock.now()
        hours = max(HOURLY_BARS, days * 24)
        if hours <= PAGE_BARS:
            page = self.prices.fetch_quote_bars(epic, minutes=60, max_bars=hours)
            pairs = zip(page.bid, page.ask, strict=True)
            cache[key] = [QuoteBar(b, a) for b, a in pairs if b.end <= now]
            return cache[key]
        held = self.history.setdefault(epic, {})
        if not held:
            # paged back to the warm-up's start; the broker refuses a page that ends in the
            # future (HTTP 400), so the walk ends at the last full hour and the newest page
            # below adds the hours since
            got = fetch_capital_quote_history(
                self.prices,
                epic,
                minutes=60,
                start=hour_floor(now) - timedelta(days=days),
                end=hour_floor(now) - timedelta(hours=1),
            )
            for b, a in zip(got.bid, got.ask, strict=True):
                held[b.start] = QuoteBar(b, a)
        if held:
            page = self.prices.fetch_quote_bars(epic, minutes=60, max_bars=HOURLY_BARS)
            for b, a in zip(page.bid, page.ask, strict=True):
                held[b.start] = QuoteBar(b, a)
        oldest = hour_floor(now) - timedelta(days=days)
        for start in [t for t in held if t < oldest]:
            del held[start]
        cache[key] = [held[t] for t in sorted(held) if held[t].end <= now]
        return cache[key]

    def decide(self, hour: datetime) -> list[Decision]:
        """Every setup's decisions on the bars that ended at ``hour``."""
        cache: dict[tuple[str, int], list[QuoteBar]] = {}
        decisions: list[Decision] = []
        for setup in self.setups:
            if not setup.setup.enabled:
                continue
            try:
                decisions += self._decide_setup(setup, hour, cache)
            except (TransportError, OrderError, ValueError) as exc:
                # one setup's failure (a refused price read) never blocks the others
                self.errors.append(f"{iso_utc(self.clock.now())} {setup.key}: {exc}")
                decisions.append(Decision(setup.key, "-", hour, None, "ERROR", str(exc)))
        return decisions

    def _decide_setup(
        self, setup: Running, hour: datetime, cache: dict[tuple[str, int], list[QuoteBar]]
    ) -> list[Decision]:
        calendars: dict[str, SessionCalendar] = {}
        ticks = {}
        for epic in setup.instruments:
            iid = setup.iid(epic)
            calendars[iid] = (
                BrokerCfdCalendar() if setup.calendar == "BROKER_CFD" else Utc247Calendar()
            )
            ticks[iid] = setup.instruments[epic].price_tick
        rule = build_cfd_rule(setup.spec, calendars, ticks)
        signals: list[tuple[str, CfdSignal]] = []
        out: list[Decision] = []
        for epic in setup.instruments:
            iid = setup.iid(epic)
            hours = self._bars(epic, 0 if setup.hourly else setup.warmup_days + 2, cache)
            if setup.hourly:
                if not hours or hours[-1].end != hour:
                    last = iso_utc(hours[-1].end) if hours else "none"
                    out.append(
                        Decision(
                            setup.key,
                            epic,
                            hour,
                            None,
                            "NO_BAR",
                            f"newest complete bar ends {last}",
                        )
                    )
                    continue
                signal: CfdSignal | None = None
                for bar in hours:
                    signal = rule.on_bar(iid, bar)
                if signal is None or signal.decided_at != hour:
                    out.append(
                        Decision(setup.key, epic, hour, None, "NO_SIGNAL", "no signal on this bar")
                    )
                    continue
                signals.append((epic, signal))
                continue
            # a daily rule: the sessions that have ended, built from their hours
            current = setup.session(hour)
            ended = [b for b in hours if setup.session(b.start) < current]
            if not ended:
                out.append(Decision(setup.key, epic, hour, None, "NO_BAR", "no ended session"))
                continue
            days = daily_from_hourly(ended, weekend_closed=setup.calendar == "BROKER_CFD")
            signal = None
            for bar in days:
                signal = rule.on_bar(iid, bar)
            last_day = days[-1]
            # the next open after the last session has passed once an hour after it has ended
            opened = any(b.start >= last_day.end for b in hours)
            self._daily_exit(setup, rule, epic, last_day, opened)
            if signal is None or signal.decided_at != last_day.end:
                out.append(
                    Decision(
                        setup.key,
                        epic,
                        last_day.end,
                        None,
                        "NO_SIGNAL",
                        f"no signal on the session ending {iso_utc(last_day.end)}",
                    )
                )
                continue
            if opened:
                out.append(
                    Decision(
                        setup.key,
                        epic,
                        signal.decided_at,
                        _direction(signal),
                        "NO_SIGNAL",
                        f"the signal of {iso_utc(signal.decided_at)} was for an open that has passed",
                    )
                )
                continue
            signals.append((epic, signal))
        signals.sort(key=lambda x: (-x[1].priority, x[1].instrument_id))
        for epic, signal in signals:
            out.append(self._enter(setup, epic, signal))
        setup.last = out[-1].as_dict() if out else {}
        return out

    def _daily_exit(
        self, setup: Running, rule: Any, epic: str, last_day: QuoteBar, opened: bool
    ) -> None:
        """A daily rule's own exit (the index dip's close above its 5-day average): the trade is
        closed at the first tradeable open after the session that gave it, as the trial did."""
        if opened or not self.live:
            return
        j = self.journal(setup)
        for trade in j.open_trades_of(setup.key):
            plan = j.plan(trade)
            if plan is None or plan["payload"]["epic"] != epic:
                continue
            if self._opened_at(j, trade) >= last_day.end:
                continue  # entered after this session: its exit is read on the next one
            direction = Direction.LONG if plan["payload"]["direction"] == "BUY" else Direction.SHORT
            reason = rule.exit_signal(setup.iid(epic), last_day, direction)
            if reason is None:
                continue
            if not self.orders.market(epic).tradeable:
                continue  # closed now: tried again each hour until the market opens
            self._close(j, trade, "SIGNAL")

    def _enter(self, setup: Running, epic: str, signal: CfdSignal) -> Decision:
        j = self.journal(setup)
        j.refresh()
        direction = _direction(signal)
        trade_id = trade_id_for(setup.key, epic, signal.decided_at)

        def no(outcome: str, detail: str) -> Decision:
            return Decision(
                setup.key, epic, signal.decided_at, direction, outcome, detail, trade_id
            )

        policy = setup.policy
        spec = setup.instruments[epic]
        now = self.clock.now()
        if self.entries_paused:
            return no("PAUSED", self.entries_paused)
        off = self.switched_off(setup)
        if off:
            return no("SWITCHED_OFF", off)
        if j.plan(trade_id) is not None:
            return no("DONE", "already planned (a restart)")
        deadline = setup.deadline(signal.decided_at, now)
        if now >= deadline:
            return no("LATE", f"past the flat time {iso_utc(deadline)}")
        mine = j.open_trades_of(setup.key)
        if len(mine) >= min(policy.exposure.max_open_positions, j.rules.open_trades_per_setup):
            return no("BUSY", f"{len(mine)} trade(s) of the setup open")
        conflict = j.group_conflict(epic, setup.key)
        if conflict is not None:
            return no("GROUP_BUSY", f"the {conflict[0]} group holds {conflict[1]}")
        if any(j.plan(t)["payload"]["epic"] == epic for t in mine):  # type: ignore[index]
            return no("BUSY", f"{epic} is already held by the setup")
        if j.opens_on(now.date(), setup.key) >= min(
            policy.exposure.max_new_entries_per_session, j.rules.max_opens_per_utc_day
        ):
            return no("ENTRY_LIMIT", "entries for the day used")
        # the journal refuses an OPEN beyond the sleeve's limit; that must be known before the
        # order is sent, not after it (a filled order the journal will not record)
        if len(j.open_trades()) >= j.rules.max_open_trades:
            return no(
                "SLEEVE_FULL", f"the sleeve's {j.rules.max_open_trades} open trades are in use"
            )
        quote = self.orders.market(epic)
        if not quote.tradeable:
            return no("CLOSED", f"market {quote.status}")
        if quote.spread_bps / 10_000 > policy.max_entry_spread_fraction:
            return no("SPREAD", f"spread {quote.spread_bps:.1f} bps")
        if not self.hedging and any(p.epic == epic for p in self.orders.positions()):
            return no("HEDGING_OFF", f"another position holds {epic} and hedging mode is off")
        long = signal.direction is Direction.LONG
        price = quote.offer if long else quote.bid
        if (long and price <= signal.stop) or (not long and price >= signal.stop):
            return no("STOP_PASSED", f"price {price} is beyond the stop {signal.stop}")
        if (long and price >= signal.target) or (not long and price <= signal.target):
            return no("TARGET_PASSED", f"price {price} is beyond the target {signal.target}")
        equity = j.sleeve_equity()
        exposure, margin = self._exposure(setup, j)
        if signal.deadline is not None:
            nights = sum(n for _, n in charge_instants(spec, now, signal.deadline))
        else:
            nights = -(-signal.max_hold_minutes // 1440)
        sized = size_order(
            SizingRequest(
                policy=policy,
                spec=spec,
                direction=signal.direction,
                entry_price=price,
                stop_price=signal.stop,
                target_price=signal.target,
                fx_to_usd=Decimal(1),
                fees=setup.fees,
                gap_allowance_fraction=policy.gap_allowance_floor_fraction,
                equity_usd=equity,
                portfolio_spendable_usd=equity - margin,
                account_spendable_usd=equity - margin,
                exposure=exposure,
                expected_gross_return=(signal.target - price) / price * (1 if long else -1),
                uncertainty_margin=Decimal(0),
                basis=ReturnBasis.EXECUTABLE,
                half_spread_fraction=Decimal(0),
                financing_nights=nights,
            )
        )
        if not sized.approved:
            why = ",".join(r.value for r in sized.reasons)
            return no("NOT_SIZED", f"{why} ({sized.binding_constraint})")
        rule = j.rules.epic(epic)
        size = min(sized.quantity, rule.max_size)
        if size < rule.min_size:
            return no("TOO_SMALL", f"size {size} below the minimum {rule.min_size}")
        hold = max(1, math.ceil((deadline - now).total_seconds() / 3600))
        plan = {
            "trade_id": trade_id,
            "epic": epic,
            "direction": direction,
            "size": str(size),
            "entry": str(price),
            "stop": str(signal.stop),
            "target": str(signal.target),
            "reference": str(signal.reference),
            "thesis": f"{setup.setup.title}: {setup.setup.label} rule signal on the bar ending "
            f"{iso_utc(signal.decided_at)}",
            "setup": f"{setup.key} ({setup.setup.status.replace('_', ' ')}, {setup.setup.label})",
            "timeframe": "1h" if setup.hourly else "1d",
            "invalidation": f"stop {signal.stop}; closed by {iso_utc(deadline)}",
            "max_hold_hours": min(hold, j.rules.max_hold_hours or hold),
            "setup_key": setup.key,
        }
        if not self.live:
            return no(
                "DRY_RUN",
                f"would {direction} {size} at about {price}, stop {signal.stop}, target {signal.target}",
            )
        try:
            self._append(j, "PLAN", plan)
        except JournalError as exc:
            return no("JOURNAL_REFUSED", str(exc))
        try:
            # a target that is only the sizer's cost gate is not an exit (the index dip)
            target = signal.target if signal.target_role == "EXIT" else None
            ref = self.orders.open_position(epic, direction, size, stop=signal.stop, target=target)
        except TransportError as exc:
            # sent once: never again. The confirm, if any, is found at the next start-up.
            self._append(j, "NOTE", {"text": f"order answer lost: {exc}", "trade_id": trade_id})
            self.entries_paused = f"{trade_id}: order answer lost; restart to reconcile"
            return no("ORDER_UNKNOWN", str(exc))
        self._append(j, "NOTE", {"text": f"order sent, deal reference {ref}", "trade_id": trade_id})
        confirmation = self._wait_confirm(ref)
        if confirmation is None:
            confirmation = self._find_position(epic, direction, size, ref)
        if confirmation is None:
            self.entries_paused = f"{trade_id}: no confirmation for {ref}; restart to reconcile"
            return no("ORDER_UNKNOWN", f"no confirmation for {ref}")
        if not confirmation.accepted or confirmation.deal_id is None:
            self._append(
                j, "CANCEL", {"trade_id": trade_id, "reason": f"rejected: {confirmation.reason}"}
            )
            return no("REJECTED", confirmation.reason)
        self._open(j, plan, ref, confirmation)
        return Decision(
            setup.key,
            epic,
            signal.decided_at,
            direction,
            "OPENED",
            f"{direction} {confirmation.size or size} at {confirmation.level}, stop {signal.stop}, "
            f"target {signal.target}, out by {iso_utc(deadline)}",
            trade_id,
        )

    def _exposure(self, setup: Running, j: PracticeJournal) -> tuple[ExposureState, Decimal]:
        gross = risk = margin = Decimal(0)
        by_inst: dict[str, Decimal] = {}
        by_tag: dict[str, Decimal] = {}
        trades = j.open_trades_of(setup.key)
        for t in trades:
            p = j.plan(t)["payload"]  # type: ignore[index]
            spec = setup.instruments[p["epic"]]
            notional = Decimal(p["entry"]) * Decimal(p["size"])
            gross += notional
            risk += Decimal(p["risk_usd"])
            by_inst[spec.identity.instrument_id] = notional
            for tag in spec.exposure_tags:
                by_tag[tag] = by_tag.get(tag, Decimal(0)) + notional
            if spec.margin is not None:
                margin += notional * spec.margin.initial_margin_fraction
        return (
            ExposureState(
                gross_usd=gross,
                by_instrument_usd=by_inst,
                by_tag_usd=by_tag,
                planned_risk_usd=risk,
                open_positions=len(trades),
            ),
            margin,
        )

    def _open(
        self, j: PracticeJournal, plan: Mapping[str, Any], ref: str, confirmation: Confirmation
    ) -> None:
        try:
            self._append(
                j,
                "OPEN",
                {
                    "trade_id": plan["trade_id"],
                    "deal_id": confirmation.deal_id,
                    "deal_reference": ref,
                    "fill_level": str(confirmation.level),
                    "size": str(confirmation.size or plan["size"]),
                },
            )
        except JournalError as exc:
            # The broker filled an order the journal will not record. A position nobody tracks
            # must not stay open: close it now and say so in the journal.
            deal = confirmation.deal_id
            why = f"the broker filled {ref} but the journal refused the OPEN ({exc})"
            try:
                if deal is None:
                    raise TraderError("the fill has no deal id")
                self.orders.close_position(deal)
                self._append(j, "CANCEL", {"trade_id": plan["trade_id"], "reason": why[:240]})
                self.echo(f"{plan['trade_id']}: {why}; the position was closed")
            except (TransportError, OrderError, JournalError, TraderError) as again:
                self.entries_paused = f"{plan['trade_id']}: {why}; closing it failed ({again})"
                self.echo(f"ENTRIES PAUSED: {self.entries_paused}")
            raise

    def _wait_confirm(self, ref: str) -> Confirmation | None:
        for _ in range(CONFIRM_WAIT_SECONDS):
            try:
                return self.orders.confirm(ref)
            except TransportError as exc:
                if exc.status not in (400, 404):
                    raise
            self.clock.sleep(1)
        return None

    def _find_position(
        self, epic: str, direction: str, size: Decimal, ref: str
    ) -> Confirmation | None:
        """No confirmation: look for the position itself for a minute."""
        known = {d for j in self.journals.values() for d in self._open_deals(j)}
        for _ in range(LOOKUP_WAIT_SECONDS // 5):
            for p in self.orders.positions():
                if (
                    p.epic == epic
                    and p.direction == direction
                    and p.size == size
                    and p.deal_id not in known
                ):
                    return Confirmation(
                        ref, True, p.deal_id, p.level, p.size, "found by position", {}
                    )
            self.clock.sleep(5)
        return None

    def _open_deals(self, j: PracticeJournal) -> list[str]:
        return [self._deal_of(j, t) for t in j.open_trades()]

    # ------------------------------------------------------------------ state for the console
    def write_state(self, now: datetime, decisions: Sequence[Decision] = ()) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if decisions:
            with (self.state_dir / "decisions.jsonl").open(
                "a", encoding="utf-8", newline="\n"
            ) as fh:
                for d in decisions:
                    fh.write(
                        json.dumps({**d.as_dict(), "live": self.live, "at": iso_utc(now)}) + "\n"
                    )
        setups = []
        for s in self.setups:
            result, trades = self.forward_result(s)
            j = self.journal(s)
            setups.append(
                {
                    "key": s.key,
                    "title": s.setup.title,
                    "sleeve": s.setup.sleeve,
                    "status": s.setup.status,
                    "trial": s.setup.label,
                    "enabled": s.setup.enabled,
                    "decides": "hourly" if s.hourly else "daily at 00:00 UTC",
                    "open_trades": j.open_trades_of(s.key),
                    "forward_trades": trades,
                    "forward_result_usd": f"{result:.2f}",
                    "loss_cap_usd": str(s.setup.loss_cap_usd) if s.setup.loss_cap_usd else None,
                    "switched_off": self.switched_off(s),
                    "last_decision": s.last or None,
                }
            )
        doc = {
            "version": 1,
            "pid": os.getpid(),
            "live": self.live,
            "started_at": iso_utc(self.started_at),
            "updated_at": iso_utc(now),
            "next_decision_at": iso_utc(next_run(now)),
            "hedging": self.hedging,
            "entries_paused": self.entries_paused,
            "errors": self.errors[-5:],
            "setups": setups,
        }
        path = self.state_dir / "trader.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    # ------------------------------------------------------------------ the loop
    def step(self) -> None:
        """One pass: the hour's decisions when an hour has ended, else the minute's poll."""
        now = self.clock.now()
        hour = hour_floor(now)
        decisions: list[Decision] = []
        try:
            if self.live:
                self.close_due(now)
                self.poll()
            if self.last_decision_hour != hour and now >= hour + timedelta(
                seconds=DECIDE_AFTER_SECONDS
            ):
                decisions = self.decide(hour)
                self.last_decision_hour = hour
                quiet = [d for d in decisions if d.outcome == "NO_SIGNAL"]
                for d in decisions:
                    if d.outcome != "NO_SIGNAL":
                        self.echo(f"{iso_utc(hour)} {d.setup} {d.epic}: {d.outcome} {d.detail}")
                if quiet:
                    names = ", ".join(sorted({f"{d.setup} {d.epic}" for d in quiet}))
                    self.echo(f"{iso_utc(hour)} no signal: {names}")
        except (TransportError, OrderError, JournalError, TraderError) as exc:
            self.errors.append(f"{iso_utc(now)} {type(exc).__name__}: {exc}")
            self.echo(f"ERROR: {exc}")
        except Exception as exc:
            # Stops and targets are at the broker, but the flat-time close needs this loop: an
            # error nobody foresaw is logged and the next minute tries again.
            self.errors.append(f"{iso_utc(now)} UNEXPECTED {type(exc).__name__}: {exc}")
            self.echo(f"ERROR (unexpected {type(exc).__name__}): {exc}")
        try:
            self.write_state(self.clock.now(), decisions)
        except OSError as exc:
            # Windows refuses to replace a file another process is reading (the console): the
            # next minute writes it again
            self.echo(f"state file not written this minute: {exc}")

    def run(self, iterations: int | None = None) -> int:
        self.reconcile()
        now = self.clock.now()
        # the hour already decided when the trader starts after its decision instant
        self.last_decision_hour = (
            hour_floor(now)
            if now >= hour_floor(now) + timedelta(seconds=DECIDE_AFTER_SECONDS)
            else None
        )
        done = 0
        while iterations is None or done < iterations:
            self.step()
            done += 1
            now = self.clock.now()
            wait = min(
                POLL_SECONDS - now.second % POLL_SECONDS, (next_run(now) - now).total_seconds()
            )
            self.clock.sleep(max(1.0, wait))
        return 0


# ----------------------------------------------------------------------------- lock and wiring
class TraderLock:
    """One trader at a time: a lock file, taken over when its trader's heartbeat is stale."""

    def __init__(self, state_dir: Path, clock: SleepingClock) -> None:
        self.path = state_dir / "trader.lock"
        self.state = state_dir / "trader.json"
        self.clock = clock

    def __enter__(self) -> TraderLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            beat = None
            with contextlib.suppress(OSError, ValueError):
                beat = json.loads(self.state.read_text(encoding="utf-8")).get("updated_at")
            fresh = (
                beat is not None
                and self.clock.now() - datetime.fromisoformat(beat.replace("Z", "+00:00"))
                < LOCK_STALE_AFTER
            )
            if fresh:
                raise TraderError(
                    "another practice trader is running (runs/practice/trader.lock)"
                ) from None
            self.path.unlink(missing_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "at": iso_utc(self.clock.now())}))
        return self

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)


def day_logger(
    clock: SleepingClock, state_dir: Path, printer: Callable[[str], None]
) -> Callable[[str], None]:
    """A function that prints a line and appends it, stamped in UTC, to the log file of the line's
    own UTC day (``trader-<date>.log``): a trader that runs past midnight starts a new file."""

    def say(message: str) -> None:
        now = clock.now()
        line = f"{now:%Y-%m-%d %H:%M:%S}Z {message}"
        printer(line)
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / f"trader-{now:%Y%m%d}.log").open(
            "a", encoding="utf-8", newline="\n"
        ) as fh:
            fh.write(line + "\n")

    return say


def run_trader(*, live: bool, only: str | None = None, echo: Callable[[str], None] = print) -> int:
    from trader.config.secrets import require_capital_demo
    from trader.data.sources.http import UrllibTransport
    from trader.ops.clock import SystemClock

    clock = SystemClock()
    say = day_logger(clock, STATE_DIR, echo)
    transport = UrllibTransport(min_interval_seconds=0.25, max_attempts=3)
    prices = CapitalPrices(transport, require_capital_demo, clock=clock)
    orders = CapitalOrders(prices, transport)
    setups = [load_running(s) for s in load_trader_setups() if only is None or s.key == only]
    if not setups:
        raise TraderError(f"no setup named {only}")
    journals = {v: PracticeJournal(venue=v) for v in VENUES}
    trader = PracticeTrader(
        prices=prices,
        orders=orders,
        clock=clock,
        setups=setups,
        journals=journals,
        live=live,
        echo=say,
    )
    names = ", ".join(f"{s.key} ({'on' if s.setup.enabled else 'off'})" for s in setups)
    say(f"practice trader {'LIVE on the demo account' if live else 'DRY RUN'}: {names}")
    with TraderLock(STATE_DIR, clock):
        try:
            return trader.run()
        except KeyboardInterrupt:
            say("practice trader stopped; stops and targets stay at the broker")
            return 0
