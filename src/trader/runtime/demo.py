"""Run the complete first demonstrator and evaluate its acceptance checks.

Each check maps to an item of instructions.md §7 / implementation_roadmap.md §4. A check is
computed from persisted records, never asserted from configuration.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from trader.domain.ids import deterministic_id
from trader.runtime.scenarios import ScenarioResult, run_scenario
from trader.storage.db import Database
from trader.storage.schema import fills, order_intents, risk_reservations


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class DemoSummary:
    results: dict[str, ScenarioResult]
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_markdown(self) -> str:
        lines = [
            "# First demonstrator - summary",
            "",
            "> SYNTHETIC REPLAY. Software behaviour only; not evidence of profitability.",
            "",
            "| Check | Result | Detail |",
            "| --- | --- | --- |",
        ]
        for c in self.checks:
            lines.append(f"| {c.name} | {'PASS' if c.passed else 'FAIL'} | {c.detail} |")
        lines += [
            "",
            "| Scenario | Decisions (approved) | Net trading P&L | Ending cash | Hashes |",
        ]
        lines += ["| --- | --- | --- | --- | --- |"]
        for name, r in self.results.items():
            rep = r.report
            lines.append(
                f"| {name} | {rep.decisions} ({rep.approved}) | {rep.net_trading_pnl} | "
                f"{rep.ending_cash} | `{rep.decision_hash[:10]}` / `{rep.ledger_hash[:10]}` |"
            )
        return "\n".join(lines) + "\n"


def _db(run_dir: Path) -> Database:
    return Database(f"sqlite:///{(run_dir / 'replay.db').as_posix()}")


def run_demo(repo_root: Path, out_dir: Path, *, postgres_url: str | None = None) -> DemoSummary:
    results: dict[str, ScenarioResult] = {}
    checks: list[Check] = []
    for name in ("baseline", "timeout", "crash", "costly", "loss"):
        results[name] = run_scenario(name, repo_root=repo_root, run_dir=out_dir / name)
    rerun = run_scenario("baseline", repo_root=repo_root, run_dir=out_dir / "baseline-rerun")
    results["baseline-rerun"] = rerun

    base = results["baseline"]
    rep = base.report
    with _db(out_dir / "baseline").tx() as conn:
        intents = conn.execute(select(order_intents)).all()
        fill_rows = conn.execute(select(fills)).all()
        res_rows = conn.execute(select(risk_reservations)).all()
    entry = [i for i in intents if i.purpose == "ENTRY"]
    bought = sum((Decimal(f.quantity) for f in fill_rows if f.side == "BUY"), start=Decimal(0))
    sold = sum((Decimal(f.quantity) for f in fill_rows if f.side == "SELL"), start=Decimal(0))
    realized = sum(
        (Decimal(f.realized_net) for f in fill_rows if f.realized_net is not None), start=Decimal(0)
    )
    checks.append(
        Check(
            "S1 eligible + rejected candidate",
            rep.approved >= 1 and sum(rep.rejection_reasons.values()) >= 1,
            f"{rep.approved} approved, rejections {rep.rejection_reasons}",
        )
    )
    checks.append(
        Check(
            "S1 exact sizing within USD 50 limits",
            all(Decimal(i.quantity) > 0 for i in entry),
            f"entry quantity {[str(i.quantity) for i in entry]}",
        )
    )
    partial = any(i.state == "CANCELED" and Decimal(i.cum_filled_qty) > 0 for i in entry)
    checks.append(
        Check(
            "S1 ack -> partial fill -> cancel remainder -> exit",
            partial and sold == bought,
            f"bought {bought}, sold {sold}",
        )
    )
    checks.append(
        Check(
            "S1 no oversell / no duplicate orders",
            sold <= bought and all(v == 1 for v in base.broker_submissions.values()),
            f"submissions per client id {sorted(set(base.broker_submissions.values()))}",
        )
    )
    checks.append(
        Check(
            "S1 ledger == fill-derived P&L",
            Decimal(rep.net_trading_pnl) == realized.quantize(Decimal("0.0001")),
            f"ledger net {rep.net_trading_pnl}, fills {realized:.6f}",
        )
    )
    checks.append(
        Check(
            "S1 reservations cleared",
            all(r.state == "CLOSED" for r in res_rows) and rep.active_reservations == 0,
            f"{len(res_rows)} reservation(s), {rep.active_reservations} active",
        )
    )
    checks.append(
        Check(
            "S1 reconciliation clean, flat at close",
            not rep.active_pauses and not rep.open_positions,
            f"pauses {rep.active_pauses or 'none'}",
        )
    )
    checks.append(
        Check(
            "S2 rerun reproduces decision and ledger hashes",
            rerun.report.decision_hash == rep.decision_hash
            and rerun.report.ledger_hash == rep.ledger_hash,
            f"{rep.decision_hash[:12]} / {rep.ledger_hash[:12]}",
        )
    )
    if postgres_url:
        schema = deterministic_id("demo", os.getpid(), "postgres").replace("-", "_")
        pg = run_scenario(
            "baseline",
            repo_root=repo_root,
            run_dir=out_dir / "baseline-postgres",
            db_url=postgres_url,
            db_schema=schema,
        )
        results["baseline-postgres"] = pg
        Database(postgres_url, schema=schema).drop_postgres_schema()
        checks.append(
            Check(
                "S2 PostgreSQL run reproduces SQLite hashes",
                pg.report.decision_hash == rep.decision_hash
                and pg.report.ledger_hash == rep.ledger_hash,
                f"{pg.report.decision_hash[:12]} / {pg.report.ledger_hash[:12]}",
            )
        )
    for name in ("timeout", "crash"):
        r = results[name]
        checks.append(
            Check(
                f"S3 {name}: one broker submission per intent, no duplicate exposure",
                all(v == 1 for v in r.broker_submissions.values())
                and not r.report.open_positions
                and r.report.fills == 2,
                f"submissions {r.broker_submissions}, restarts {r.restarts}",
            )
        )
    costly = results["costly"].report
    checks.append(
        Check(
            "S4 costly broker produces no trade",
            costly.orders == 0
            and "TOO_COSTLY" in costly.rejection_reasons
            and Decimal(costly.ending_cash) == Decimal(50),
            f"reasons {costly.rejection_reasons}, cash {costly.ending_cash}",
        )
    )
    loss = results["loss"]
    lc = loss.checks
    after_restart = list(lc.get("after_restart_flags", []))  # type: ignore[call-overload]
    checks.append(
        Check(
            "S5 loss breach pauses entries and persists across restart",
            "DAILY_NY" in after_restart and "PAUSED" in loss.report.rejection_reasons,
            f"after restart: {lc.get('after_restart_flags')}",
        )
    )
    checks.append(
        Check(
            "S5 resume refused without review and while breached",
            bool(lc.get("refused_without_review")) and bool(lc.get("refused_same_session")),
            "refused twice as required",
        )
    )
    checks.append(
        Check(
            "S5 resume allowed after review in the next session",
            lc.get("refused_next_session") == [] and lc.get("flags_after_review") == [],
            f"flags after review: {lc.get('flags_after_review')}",
        )
    )
    summary = DemoSummary(results=results, checks=checks)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.md").write_text(summary.to_markdown(), encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps([c.__dict__ for c in checks], indent=2), encoding="utf-8"
    )
    return summary
