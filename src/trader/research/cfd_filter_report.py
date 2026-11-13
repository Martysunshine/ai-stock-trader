"""The Markdown report of a CFD filter study (T-0026), written from its ``results.json``.

It reads like the CFD study's report (``research/cfd_report.py``): verdict, views, walk forward,
G2 and ADR-0052's practice rule, plus the comparison the filter must win against the rule alone,
random refusal at its own rate and equal turnover, in the base and the stressed view."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

VARIANT_NAMES = {
    "rule_only": "rule alone",
    "filter": "rule + filter",
    "random_control": "random refusal (mean of seeds)",
    "turnover_control": "equal turnover, smallest break refused",
}


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


def _yes(flag: object) -> str:
    return "yes" if flag else "no"


def _view_row(name: str, v: Mapping[str, Any]) -> list[str]:
    windows = v.get("window_nets_usd")
    return [
        name,
        str(v.get("trades")),
        _usd(v.get("net_pnl_usd")),
        _usd(v.get("gross_pnl_usd")),
        _num(v.get("execution_cost_usd")),
        _num(v.get("financing_usd")),
        _pct(v.get("win_rate")),
        _num(v.get("profit_factor")),
        _pct(v.get("max_drawdown_fraction")),
        f"{v['negative_windows']}/{len(windows)}" if windows is not None else "-",
    ]


def render_cfd_filter_report(result: Mapping[str, Any], *, generated_at: str, digest: str) -> str:
    lines: list[str] = [
        f"# {result['title']}",
        "",
        f"Generated {generated_at}. Study `{result['study_id']}` on base study "
        f"`{result['base_study_id']}` trial {result['base_trial']}, results digest "
        f"`{digest[:16]}`. {result['evidence_class']}.",
        "",
        "## Verdict",
        "",
    ]
    for tid, trial in result["trials"].items():
        cmp = trial["comparison"]
        p = trial["promotion"]
        base = trial["views"]["base"]
        lines.append(
            f"- **{tid}** ({trial['family']}): the filter "
            + ("**works**" if cmp["works"] else "**does not work**")
            + " by the rule fixed before the run ("
            + cmp["rule"]
            + "). Checks: "
            + ", ".join(f"{k} {_yes(v)}" for k, v in cmp["checks"].items())
            + "."
        )
        lines.append(
            f"- Filtered rule: {base['trades']} trades, net {_usd(base['net_pnl_usd'])} USD on "
            f"USD {result['capital_usd']:.0f} windows, stressed "
            f"{_usd(trial['views']['stressed']['net_pnl_usd'])} USD; "
            f"{trial['folds_trained']} of {len(trial['folds'])} folds trained. Practice rule of "
            "ADR-0052: "
            + ("**traded**." if p["trade_in_practice"] else "**not traded**.")
            + " Checks: "
            + ", ".join(f"{k} {_yes(v)}" for k, v in p["checks"].items())
            + "."
        )
    wf = result["walk_forward"]["summary"]
    g2 = result["g2"]
    lines += [
        f"- Walk forward: {wf['trades']} trades over {wf['sessions']} sessions, net "
        f"{_usd(wf['net_pnl_usd'])} USD. G2: **{g2['status']}** ("
        + ", ".join(f"{k} {_yes(v)}" for k, v in g2["checks"].items())
        + ").",
        "",
        "## The filter against its controls",
        "",
        "Net USD over the same test windows, each a fresh deployment of the sleeve's capital:",
        "",
    ]
    for tid, trial in result["trials"].items():
        table = trial["comparison"]["net_usd"]
        rows = [
            [
                VARIANT_NAMES[name],
                *(_usd(table[view][name]) for view in ("base", "stressed")),
                _usd(trial["variants"][name]["views"]["diagnostic"]["net_pnl_usd"]),
            ]
            for name in ("filter", "rule_only", "random_control", "turnover_control")
        ]
        lines += [f"### {tid}", ""]
        lines += _table(["variant", "base", "stressed", "mid, no financing"], rows)
        rnd = trial["variants"]["random_control"]["views"]
        lines += [
            f"Random refusal over {rnd['base']['seeds']} seeds: base view from "
            f"{_usd(rnd['base']['min_net_pnl_usd'])} to {_usd(rnd['base']['max_net_pnl_usd'])} "
            f"USD, median {_usd(rnd['base']['median_net_pnl_usd'])}; "
            f"{rnd['base']['seeds_at_least_filter']} seeds at least as good as the filter "
            f"(p {_num(rnd['base']['p_value_filter_not_better'], 3)}). Stressed view: "
            f"{rnd['stressed']['seeds_at_least_filter']} seeds at least as good "
            f"(p {_num(rnd['stressed']['p_value_filter_not_better'], 3)}).",
            "",
        ]
        paired = trial.get("paired_differences") or {}
        bits = []
        for name, diff in paired.items():
            if diff:
                bits.append(
                    f"{name.replace('model_minus_', 'filter minus ')}: mean "
                    f"{_num(diff.get('estimate'), 4)} USD a session, one-sided lower bound "
                    f"{_num(diff.get('one_sided_lower'), 4)}"
                )
        if bits:
            lines += ["Paired bootstrap, base view: " + "; ".join(bits) + ".", ""]
        for view in ("base", "stressed", "diagnostic"):
            rows = []
            for name in ("filter", "rule_only", "turnover_control"):
                rows.append(_view_row(VARIANT_NAMES[name], trial["variants"][name]["views"][view]))
            r = trial["variants"]["random_control"]["views"][view]
            rows.append(
                [
                    VARIANT_NAMES["random_control"],
                    _num(r["trades"], 1),
                    _usd(r["net_pnl_usd"]),
                    "-",
                    "-",
                    "-",
                    _pct(r["win_rate"]),
                    "-",
                    "-",
                    "-",
                ]
            )
            lines += [f"{view.capitalize()} view:", ""]
            lines += _table(
                [
                    "variant",
                    "trades",
                    "net USD",
                    "gross USD",
                    "spread+slip USD",
                    "financing USD",
                    "win",
                    "PF",
                    "max DD",
                    "losing windows",
                ],
                rows,
            )
        lines += ["Long and short (filtered rule, base view):", ""]
        rows = [
            [
                d,
                str(s["trades"]),
                _usd(s["net_pnl_usd"]),
                _pct(s["win_rate"]),
                _usd(s["avg_win_usd"]),
                _usd(s["avg_loss_usd"]),
            ]
            for d, s in trial["views"]["base"]["by_direction"].items()
        ]
        lines += _table(["side", "trades", "net USD", "win", "avg win", "avg loss"], rows)

        lines += ["## Folds", ""]
        rows = []
        for f in trial["folds"]:
            labels = f["labels"]
            run = f["runs"]["base"]
            rows.append(
                [
                    str(f["fold"]),
                    f"{f['train'][0][:10]}..{f['train'][1][:10]}",
                    f"{f['test'][0][:10]}..{f['test'][1][:10]}",
                    f"{labels['train_rows_after_purge']} ({labels['train_positives']} won)",
                    f"{labels['validation_rows']} ({labels['validation_positives']} won)",
                    f["status"] + (f" {f['model_type']}" if f["model_type"] else ""),
                    f["threshold"] or "-",
                    f["validation_objective_usd_per_session"] or "-",
                    f"{run['filter']['filter_refusals']}/{run['filter']['filter_decisions']}",
                    _usd(run["filter"]["net_usd"]),
                    _usd(run["rule_only"]["net_usd"]),
                ]
            )
        lines += _table(
            [
                "fold",
                "train",
                "test",
                "train labels",
                "validation labels",
                "model",
                "threshold",
                "validation USD/session",
                "refused",
                "filter net",
                "rule net",
            ],
            rows,
        )
        untrained = [f for f in trial["folds"] if f["status"] != "TRAINED"]
        for f in untrained:
            lines += [f"- Fold {f['fold']} could not be trained ({f['reason']}); it held cash.", ""]
        pooled = trial.get("calibration_pooled_test")
        if pooled:
            lines += [
                "Calibration on the test windows' labels (each signal's own outcome; "
                f"diagnostic only): {pooled['rows']} labels, base rate "
                f"{_pct(pooled['base_rate'])}, mean predicted {_pct(pooled['mean_predicted'])}, "
                f"Brier {_num(pooled['brier'], 4)}, log-loss {_num(pooled['log_loss'], 4)}, "
                f"ECE {_num(pooled['ece'], 4)}.",
                "",
            ]
    sa = result["selection_aware"]
    rc = sa.get("reality_check") or {}
    dsr = sa.get("deflated_sharpe") or {}
    lines += [
        "## Selection",
        "",
        f"- Reality check across the four variants: p {_num(rc.get('p_value_no_positive_mean'), 3)}, "
        f"selection-adjusted lower bound {_num(rc.get('selection_adjusted_lower_bound'), 4)}, best "
        f"row `{sa.get('reality_check_best_row') or '-'}`; deflated Sharpe "
        f"{_num(dsr.get('dsr'), 3)} with {sa['registered_trials']} registered trials.",
        "",
        "## Method",
        "",
        f"- Features `{result['feature_version']}`: "
        + "; ".join(f"`{k}` {v}" for k, v in result["feature_definitions"].items())
        + ".",
        f"- Labels: {result['label_rule']}",
        f"- Selection: {result['selection_rule']}",
        f"- Threshold: {result['threshold_rule']}",
        f"- Challenger: {result['challenger_rule']}",
        f"- Controls: {result['controls_rule']}",
        f"- Splits: development {result['splits']['development']}, locked final test "
        f"{result['splits']['locked_final_test']} (unlocked: {result['holdout_unlocked']}), "
        f"{result['splits']['walk_forward']}, embargo {result['splits']['embargo_minutes']} "
        "minutes.",
        "",
        "## Limits",
        "",
        "- The rule alone is the base trial's rule on the test windows only, each a fresh "
        "deployment; where a test window is one of the base study's own windows it is the same "
        "run and has the same result hash.",
        "- In a fold whose filter could not be trained, the filter variant refuses every signal; "
        "the walk forward holds cash in every fold whose validation objective is not positive.",
        "- The filter decides on the broker's raw quotes, so it refuses the same signals in every "
        "view; the stressed view then tests those trades at doubled costs.",
        "- A signal not taken in the unfiltered run is labelled by trading it alone; in a "
        "filtered run it may still be blocked by another open trade.",
        "- Demo quotes may be tighter than live fills. Backtest only: neither G2 nor G3 evidence "
        "on its own.",
        "",
    ]
    return "\n".join(lines)


__all__ = ["render_cfd_filter_report"]
