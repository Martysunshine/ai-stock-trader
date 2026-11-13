"""Honest run reports (instructions.md §5 "Reporting"; strategy_and_profitability.md §1, §10).

A report states its evidence class (synthetic replay / backtest / shadow / paper / live) and
separates gross trading P&L, fees, net trading P&L, operating costs, external cash flows and
total project net P&L. It lists every rejection reason. It never claims profitability.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from sqlalchemy import Connection, select

from trader.accounting.ledger import Ledger
from trader.domain.types import ZERO, canon_decimal
from trader.ops.pause import PauseService
from trader.reporting.hashes import decision_hash, ledger_hash
from trader.storage.schema import (
    fills,
    operating_expenses,
    order_intents,
    risk_decisions,
    risk_reservations,
)

SYNTHETIC_WATERMARK = (
    "SYNTHETIC REPLAY - generated data and a declared fixture edge assumption. "
    "This is a software demonstration, NOT evidence of profitability."
)


@dataclass
class RunReport:
    run_id: str
    scenario: str
    evidence_class: str
    watermark: str
    decisions: int
    approved: int
    rejection_reasons: dict[str, int]
    orders: int
    order_states: dict[str, int]
    fills: int
    gross_trading_pnl: str
    fees: str
    net_trading_pnl: str
    operating_costs: str
    external_cash_flows: str
    total_project_net_pnl: str
    ending_cash: str
    open_positions: dict[str, str]
    active_reservations: int
    active_pauses: list[str]
    decision_hash: str
    ledger_hash: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_markdown(self) -> str:
        lines = [
            f"# Run report - {self.run_id} ({self.scenario})",
            "",
            f"> **{self.watermark}**",
            "",
            f"Evidence class: `{self.evidence_class}`",
            "",
            "| Measure | Value |",
            "| --- | --- |",
            f"| Decisions (approved) | {self.decisions} ({self.approved}) |",
            f"| Orders / fills | {self.orders} / {self.fills} |",
            f"| Gross trading P&L (USD) | {self.gross_trading_pnl} |",
            f"| Trading fees (USD) | {self.fees} |",
            f"| Net trading P&L (USD) | {self.net_trading_pnl} |",
            f"| Operating costs (USD) | {self.operating_costs} |",
            f"| External cash flows (USD) | {self.external_cash_flows} |",
            f"| **Total project net P&L (USD)** | **{self.total_project_net_pnl}** |",
            f"| Ending cash (USD) | {self.ending_cash} |",
            f"| Active reservations | {self.active_reservations} |",
            f"| Decision hash | `{self.decision_hash[:16]}` |",
            f"| Ledger hash | `{self.ledger_hash[:16]}` |",
            "",
            "## Rejection reasons",
            "",
        ]
        if self.rejection_reasons:
            lines += [f"- `{k}`: {v}" for k, v in sorted(self.rejection_reasons.items())]
        else:
            lines.append("- none")
        lines += ["", "## Order states", ""]
        lines += [f"- `{k}`: {v}" for k, v in sorted(self.order_states.items())] or ["- none"]
        lines += ["", "## Open positions", ""]
        lines += [f"- {k}: {v}" for k, v in self.open_positions.items()] or ["- none (flat)"]
        lines += ["", "## Active pauses", ""]
        lines += [f"- {p}" for p in self.active_pauses] or ["- none"]
        if self.notes:
            lines += ["", "## Notes", ""] + [f"- {n}" for n in self.notes]
        return "\n".join(lines) + "\n"


def _fmt(value: Decimal) -> str:
    return canon_decimal(value.quantize(Decimal("0.0001")))


def build_report(
    conn: Connection,
    *,
    ledger: Ledger,
    pauses: PauseService,
    run_id: str,
    scenario: str,
    account_id: str,
    currency: str,
    evidence_class: str,
    notes: list[str] | None = None,
) -> RunReport:
    decisions = conn.execute(select(risk_decisions.c.approved, risk_decisions.c.reasons)).all()
    reasons: Counter[str] = Counter()
    for approved, rs in decisions:
        if not approved:
            for r in str(rs).split(","):
                if r:
                    reasons[r] += 1
    intents = conn.execute(select(order_intents.c.state)).scalars().all()
    fill_rows = conn.execute(select(fills.c.realized_gross, fills.c.fee)).all()
    gross = sum((Decimal(g) for g, _ in fill_rows if g is not None), start=ZERO)
    fees = ledger.fee_totals(conn, account_id)
    net = ledger.realized_net_total(conn, account_id)
    op_costs = sum(
        (Decimal(r) for r in conn.execute(select(operating_expenses.c.amount)).scalars()),
        start=ZERO,
    )
    flows = ledger.flows(conn, account_id)
    external = sum((v for k, v in flows.items() if k != "FLOW_INITIAL_CAPITAL"), start=ZERO)
    positions = {
        p.instrument_id: canon_decimal(p.quantity)
        for p in ledger.open_positions(conn, [account_id])
    }
    active_res = conn.execute(
        select(risk_reservations.c.reservation_id).where(risk_reservations.c.state == "ACTIVE")
    ).all()
    return RunReport(
        run_id=run_id,
        scenario=scenario,
        evidence_class=evidence_class,
        watermark=SYNTHETIC_WATERMARK if evidence_class == "SYNTHETIC_REPLAY" else evidence_class,
        decisions=len(decisions),
        approved=sum(1 for a, _ in decisions if a),
        rejection_reasons=dict(reasons),
        orders=len(intents),
        order_states=dict(Counter(intents)),
        fills=len(fill_rows),
        gross_trading_pnl=_fmt(gross),
        fees=_fmt(fees),
        net_trading_pnl=_fmt(net),
        operating_costs=_fmt(op_costs),
        external_cash_flows=_fmt(external),
        total_project_net_pnl=_fmt(net - op_costs),
        ending_cash=_fmt(ledger.cash(conn, account_id, currency)),
        open_positions=positions,
        active_reservations=len(active_res),
        active_pauses=[
            f"{f.category.value}:{f.cause.value} - {f.details}" for f in pauses.all_active(conn)
        ],
        decision_hash=decision_hash(conn),
        ledger_hash=ledger_hash(conn),
        notes=notes or [],
    )
