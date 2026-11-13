"""The Markdown report of a CFD study (ADR-0052), written from its ``results.json``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from trader.research.cfd_study import promotion


def _usd(value: object) -> str:
    return "-" if value is None else f"{float(value):+.2f}"  # type: ignore[arg-type]


def _pct(value: object) -> str:
    return "-" if value is None else f"{100 * float(value):.0f}%"  # type: ignore[arg-type]


def _num(value: object, places: int = 2) -> str:
    return "-" if value is None else f"{float(value):.{places}f}"  # type: ignore[arg-type]


def _table(head: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return [*out, ""]


def render_cfd_report(result: Mapping[str, Any], *, generated_at: str, digest: str) -> str:
    #: a study whose rule decides on the hourly bars (ADR-0055); older results carry no key
    hourly = result.get("decision_bar") == "hourly"
    bar = "hourly" if hourly else "daily"
    lines: list[str] = [
        f"# {result['title']}",
        "",
        f"Generated {generated_at}. Study `{result['study_id']}`, results digest `{digest[:16]}`. "
        f"{result['evidence_class']}.",
        "",
        "## Verdict",
        "",
    ]
    for tid, trial in result["trials"].items():
        p = promotion(result, tid)
        base = trial["views"]["base"]
        lines.append(
            f"- **{tid}** ({trial['family']}): {base['trades']} trades, net "
            f"{_usd(base['net_pnl_usd'])} USD on USD {result['capital_usd']:.0f} windows, stressed "
            f"{_usd(trial['views']['stressed']['net_pnl_usd'])} USD. Practice rule of ADR-0052: "
            + ("**traded**." if p["trade_in_practice"] else "**not traded**.")
            + " Checks: "
            + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in p["checks"].items())
            + "."
        )
    wf = result["walk_forward"]["summary"]
    lines += [
        f"- Walk forward: {wf['trades']} trades over {wf['sessions']} sessions, net "
        f"{_usd(wf['net_pnl_usd'])} USD. G2: **{result['g2']['status']}** "
        + "("
        + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in result["g2"]["checks"].items())
        + ").",
        "",
        "## Data",
        "",
        f"Dataset `{result['data']['dataset']}`: the broker's hourly bid and ask, "
        + (
            "used as hourly bars (sessions are UTC days) "
            if hourly
            else "built into one bar per UTC day "
        )
        + f"on the `{result['session_calendar']}` calendar. Warm-up "
        f"{result['warmup_days']} days before each window. Splits: development "
        f"{result['splits']['development']}, locked final test "
        f"{result['splits']['locked_final_test']} (unlocked: {result['holdout_unlocked']}).",
        "",
    ]
    years = sorted({y for v in result["data"]["spread_bps_median_by_year"].values() for y in v})
    rows = [
        [
            iid,
            str(result["data"]["daily_bars"][iid]),
            str(result["closed_weekdays"].get(iid, 0)),
            *(_num(v.get(y), 1) for y in years),
        ]
        for iid, v in result["data"]["spread_bps_median_by_year"].items()
    ]
    lines += [f"Median quoted spread at the {bar} close, bps of the mid:", ""]
    lines += _table(["instrument", f"{bar} bars", "closed weekdays", *years], rows)
    lines += ["## Costs", ""]
    rows = [
        [
            view,
            c["slippage_bps"],
            c["spread_multiplier"],
            "mid" if c["mid_fills"] else "bid/ask",
            ("yes x" + c["financing_charge_multiplier"]) if c["financing"] else "no",
            "yes" if c["financing_credits"] else "no",
            str(c["missed_entry_percent"]),
        ]
        for view, c in result["cost_model"].items()
    ]
    lines += _table(
        ["view", "slippage bps", "spread x", "fills", "financing", "credits", "missed %"], rows
    )
    for tid, trial in result["trials"].items():
        lines += [f"## {tid} by view", ""]
        rows = []
        for view in ("base", "stressed", "diagnostic"):
            v = trial["views"][view]
            rows.append(
                [
                    view,
                    str(v["trades"]),
                    _usd(v["net_pnl_usd"]),
                    _usd(v["gross_pnl_usd"]),
                    _num(v["execution_cost_usd"]),
                    _num(v["financing_usd"]),
                    _pct(v["win_rate"]),
                    _num(v["profit_factor"]),
                    _pct(v.get("max_drawdown_fraction")),
                    f"{v['negative_windows']}/{len(v['window_nets_usd'])}",
                    str(v.get("not_taken_size_over_limit", "-")),
                ]
            )
        lines += _table(
            [
                "view",
                "trades",
                "net USD",
                "gross USD",
                "spread+slip USD",
                "financing USD",
                "win",
                "PF",
                "max DD",
                "losing windows",
                "too large",
            ],
            rows,
        )
        lines += ["Long and short (base view):", ""]
        rows = [
            [
                d,
                str(s["trades"]),
                _usd(s["net_pnl_usd"]),
                _pct(s["win_rate"]),
                _usd(s["avg_win_usd"]),
                _usd(s["avg_loss_usd"]),
                _num(s["financing_usd"]),
            ]
            for d, s in trial["views"]["base"]["by_direction"].items()
        ]
        lines += _table(
            ["side", "trades", "net USD", "win", "avg win", "avg loss", "financing"], rows
        )
        reasons = trial["views"]["base"].get("candidates", {}).get("by_reason", {})
        status = trial["views"]["base"].get("candidates", {}).get("by_status", {})
        lines += [
            f"Signals (base view): {status}. Not taken by reason: {reasons or 'none'}. "
            f"Exit reasons: {trial['views']['base']['exit_reasons']}.",
            "",
            f"Continuous run over the development period from USD {result['capital_usd']:.0f}: "
            f"{trial['views']['continuous']['trades']} trades, net "
            f"{_usd(trial['views']['continuous']['net_pnl_usd'])} USD.",
            "",
        ]
    bh = result["comparators"]["buy_and_hold"]
    sa = result["selection_aware"]
    rc = sa.get("reality_check") or {}
    dsr = sa.get("deflated_sharpe") or {}
    lines += [
        "## Comparators and selection",
        "",
        f"- Buy and hold (equal notional, long, financing paid) over the walk-forward test "
        f"sessions: net {_usd(bh['net_pnl_usd'])} USD, max drawdown "
        f"{_pct(bh.get('max_drawdown_fraction'))}.",
        "- Cash: 0.00 USD.",
        f"- Reality check p-value {_num(rc.get('p_value_no_positive_mean'), 3)}, "
        f"selection-adjusted lower bound {_num(rc.get('selection_adjusted_lower_bound'), 4)}; "
        f"deflated Sharpe {_num(dsr.get('dsr'), 3)} with {sa['registered_trials']} registered trials.",
        "",
        "## Limits",
        "",
        "- Financing rates are those read on 2026-11-10, applied to every year.",
        (
            "- Demo quotes may be tighter than live fills; entries are at the open of the hour "
            "after the decision."
            if hourly
            else "- Demo quotes may be tighter than live fills; entries are at the daily bar's "
            "open (00:00 UTC, or the Sunday-evening open), where spreads are often wider than "
            "in the US session."
        ),
        "- A stop is assumed to fill at its level plus slippage unless the market opens "
        f"through it; a stop and a target touched in one {bar} bar count as the stop.",
        "- Backtest only. Practice results on the demo account are a separate record; neither "
        "is G2 or G3 evidence on its own.",
        "",
    ]
    return "\n".join(lines)
