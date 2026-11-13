"""Render the M3 model report (``research/reports/model_report.md``) from a model study result.

Contents follow implementation_roadmap.md §2 ("chronological splits, tuning history,
calibration, economic ablation, uncertainty/selection correction") and docs/ml_research_guide.md
§5. Every verdict sentence is computed from the numbers; nothing is pre-filled as passed, and the
backtest evidence class is stated (instructions.md §5 "Reporting"). The roadmap's M3 exit
condition - a defensible challenger or a documented rejection - is stated from Gate G2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from trader.research.markdown import ci, fmt, pct, report_file_name, table
from trader.research.report import G2_CHECK_ORDER

_VARIANT_LABELS = {
    "rule_only": "Rule only",
    "model_filter": "Rule + model filter",
    "random_control": "Random rejection at the model's veto rate (mean of salts)",
    "turnover_control": "Equal accepted-candidate count (mean of salts)",
}


def _usd(value: str | None, digits: int = 2) -> str:
    return "n/a" if not value else fmt(float(Decimal(value)), digits)


def _ordered(checks: Mapping[str, bool]) -> list[tuple[str, bool]]:
    known = [(k, checks[k]) for k in G2_CHECK_ORDER if k in checks]
    return known + [(k, v) for k, v in checks.items() if k not in G2_CHECK_ORDER]


def _span(window: Sequence[str]) -> str:
    return f"{window[0][:16].replace('T', ' ')} .. {window[1][:16].replace('T', ' ')}"


def _salt_range(control: Mapping[str, Any] | None) -> str:
    if not control:
        return "n/a"
    return (
        f"{fmt(control['mean_net_pnl_usd'], 2)} [{fmt(control['min_net_pnl_usd'], 2)}, "
        f"{fmt(control['max_net_pnl_usd'], 2)}]"
    )


def _fold_salts(runs: Mapping[str, Any], name: str) -> str:
    salts = runs.get(name, {}).get("salts") or []
    if not salts:
        return "n/a"
    nets = [float(Decimal(s["net_usd"])) for s in salts]
    return f"{fmt(sum(nets) / len(nets), 2)} [{fmt(min(nets), 2)}, {fmt(max(nets), 2)}]"


# ----------------------------------------------------------------------------- sections


def _verdict(result: Mapping[str, Any]) -> list[str]:
    lines = []
    for tid, t in result["trials"].items():
        v = t["variants"]
        rule, model = v["rule_only"], v["model_filter"]
        random, turnover = v.get("random_control"), v.get("turnover_control")
        trained = t["folds_trained"]
        total = len(t["folds"])
        text = (
            f"- **{tid}** ({t['family']}): a model was trained in {trained} of {total} folds. "
            f"Over the test windows the rule alone made {fmt(rule['net_pnl_usd'], 2)} USD "
            f"({rule['trades']} trades) and the rule with the model filter "
            f"{fmt(model['net_pnl_usd'], 2)} USD ({model['trades']} trades)"
        )
        if random:
            text += (
                f"; random rejection at the same veto rate made {fmt(random['mean_net_pnl_usd'], 2)}"
                f" USD on average over {random['seeds']} salts"
            )
        if turnover:
            text += (
                f" and equal accepted-candidate counts {fmt(turnover['mean_net_pnl_usd'], 2)} USD"
            )
        lines.append(text + ".")
        controls = [(n, c) for n, c in (("random", random), ("equal-turnover", turnover)) if c]
        if trained == 0:
            lines.append(
                f"  - No fold of {tid} could be trained, so its filter vetoed every candidate "
                "(cash); this is a rejection of the recipe, not evidence about the rule."
            )
        elif controls and all(c["salts_at_least_model"] == 0 for _, c in controls):
            p_values = ", ".join(
                f"{n} p = {fmt(c['p_value_model_not_better'], 3)}" for n, c in controls
            )
            lines.append(
                f"  - The filter's net exceeded every control salt ({p_values}); this is still "
                "not selection-corrected (section 8)."
            )
        else:
            lines.append(
                "  - The filter did **not** beat its controls on every salt: what it gained over "
                "the rule is not distinguishable from simply trading less (strategy §6)."
            )
    wf = result["walk_forward"]
    folds = wf["folds"]
    s = wf["summary"]
    cash = sum(1 for f in folds if f["selected"] == "CASH")
    lines.append(
        f"- Walk-forward selection across trials held cash in {cash} of {len(folds)} folds; the "
        f"series made {fmt(s.get('net_pnl_usd'), 2)} USD over {s.get('sessions')} sessions with "
        f"{s.get('trades')} trades."
    )
    gate = result["g2"]
    lines.append(
        f"- **G2 (research evidence): {gate['status']}.** "
        + (
            "M3 outcome: **documented rejection** - no defensible challenger; no filter here is "
            "a candidate for paper or live use."
            if gate["status"] != "PASSED"
            else "M3 outcome: a challenger for review only; promotion still needs Gate G3 forward "
            "evidence and owner approval."
        )
    )
    return lines


def _splits(result: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    rows = []
    for tid, t in result["trials"].items():
        for f in t["folds"]:
            lab = f["labels"]
            rows.append(
                [
                    f["fold"],
                    _span(f["train"]),
                    _span(f["validation"]),
                    _span(f["test"]),
                    tid,
                    f"{lab['train_rows_after_purge']} of {lab['train_rows']} "
                    f"({lab['train_positives']} wins)",
                    f"{lab['validation_rows']} ({lab['validation_positives']} wins)",
                    f"{lab['test_rows']} ({lab['test_positives']} wins)",
                    f["status"],
                ]
            )
    lines += table(
        [
            "Fold",
            "Train",
            "Validation",
            "Test",
            "Trial",
            "Train labels after purge",
            "Validation labels",
            "Test labels",
            "Model",
        ],
        rows,
    )
    splits = result["splits"]
    lines += [
        "",
        f"Development period {splits['development']}; walk-forward {splits['walk_forward']}; "
        f"embargo {splits['embargo_minutes']} minutes (the maximum hold). The locked final test "
        f"period {splits['locked_final_test']} "
        f"{'was UNLOCKED' if result['holdout_unlocked'] else 'was never loaded'}.",
        "",
        "Training labels use bars before the training end only and are purged when their decision "
        "or exit falls within the embargo before it; validation labels resolve inside the embargo "
        "with bars before the test start; test labels serve calibration diagnostics only. "
        f"Label rule: {result['label_rule']}.",
    ]
    return lines


def _trials(result: Mapping[str, Any]) -> list[str]:
    rows = []
    for tid, t in result["trials"].items():
        grid = "; ".join(", ".join(f"{k}={v}" for k, v in p.items()) for p in t["grid"])
        controls = t["controls"]
        rows.append(
            [
                tid,
                t["family"],
                f"`{t['spec_file'].rsplit('/', 1)[-1]}` "
                + (", ".join(f"{k}={v}" for k, v in t["overrides"].items()) or "(spec defaults)"),
                t["model_type"],
                grid,
                t["calibration"],
                ", ".join(t["threshold_grid"]),
                t["min_accepts"],
                f"{controls['random_seeds']} salts"
                + (", equal turnover" if controls["turnover_matched"] else ""),
                t["seed"],
                f"`{t['registration_hash'][:12]}`",
            ]
        )
    lines = table(
        [
            "Trial",
            "Family",
            "Rule",
            "Model",
            "Grid",
            "Calibration",
            "Thresholds",
            "Min accepts",
            "Controls",
            "Seed",
            "Registration",
        ],
        rows,
    )
    versions = {
        ", ".join(f"{k} {v}" for k, v in sorted(t["library_versions"].items()))
        for t in result["trials"].values()
    }
    lines += [
        "",
        f"All {len(result['trials'])} trials were registered in "
        "`research/experiment_registry.jsonl` before running, with the filter recipe, library "
        f"versions ({'; '.join(sorted(versions))}), feature set `{result['feature_version']}` "
        f"({len(result['feature_names'])} columns) and label definition bound into the "
        f"registration hash. They count against the project's trial budget "
        f"({result['trial_budget']} configurations when this study ran).",
        "",
        f"- Selection protocol: {result['selection_rule']}",
        f"- Threshold rule: {result['threshold_rule']}.",
        f"- Controls: {result['controls_rule']}.",
        f"- Untrainable fold: {result['untrainable_rule']}.",
    ]
    return lines


def _tuning(result: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for tid, t in result["trials"].items():
        for f in t["folds"]:
            head = f"**{tid}, fold {f['fold']}**"
            if f["status"] == "TRAINED":
                head += (
                    f" - bundle `{f['bundle_id']}` (checksum `{f['bundle_checksum'][:12]}`), "
                    f"threshold {f['threshold']}"
                )
            else:
                head += f" - UNTRAINABLE: {f['reason']}"
            lines += [head, ""]
            log = f["selection_log"]
            grid = [e for e in log if e.get("stage") == "grid"]
            if grid:
                lines += table(
                    ["Grid point", "Parameters", "Validation log-loss", "Fit"],
                    [
                        [
                            e["index"],
                            ", ".join(
                                f"{k[6:]}={v}" for k, v in e.items() if k.startswith("param.")
                            ),
                            fmt(float(e["val_log_loss"]), 5),
                            ", ".join(
                                f"{k} {v}"
                                for k, v in e.items()
                                if k not in ("stage", "index", "val_log_loss")
                                and not k.startswith("param.")
                            ),
                        ]
                        for e in grid
                    ],
                )
                lines.append("")
            for e in log:
                if e.get("stage") == "selected":
                    lines.append(
                        f"- Selected grid point {e['index']} (lowest validation log-loss)."
                    )
                elif e.get("stage") == "calibration":
                    params = ", ".join(
                        f"{k}={v}" for k, v in e.items() if k not in ("stage", "method")
                    )
                    lines.append(
                        f"- Calibration `{e['method']}` fitted on validation scores"
                        + (f": {params}." if params else ".")
                    )
            replays = {r["threshold"]: r for r in f["threshold_replays"]}
            thresholds = [e for e in log if e.get("stage") == "threshold"]
            if thresholds:
                lines.append("")
                lines += table(
                    [
                        "Threshold",
                        "Validation accepts",
                        "Eligible",
                        "Objective (USD/session)",
                        "Replay trades",
                        "Replay net USD",
                    ],
                    [
                        [
                            e["threshold"],
                            f"{e['accepts']} of {e['validation_rows']}",
                            e["eligible"],
                            e["objective"] or "-",
                            replays[e["threshold"]]["trades"] if e["threshold"] in replays else "-",
                            _usd(replays[e["threshold"]]["net_usd"], 4)
                            if e["threshold"] in replays
                            else "-",
                        ]
                        for e in thresholds
                    ],
                )
            if lines[-1] != "":
                lines.append("")
    return lines


def _calibration(result: Mapping[str, Any]) -> list[str]:
    lines = [
        "Validation figures are in-sample for the calibration map (it was fitted there); the "
        "test-window figures are out-of-sample. Test labels are every candidate the rule proposed "
        "in the window, each simulated alone.",
        "",
    ]
    for tid, t in result["trials"].items():
        rows = []
        for f in t["folds"]:
            vm = f["val_metrics"]
            c = f.get("calibration_test")
            rows.append(
                [
                    f["fold"],
                    f"{fmt(float(vm['brier_raw']))} -> {fmt(float(vm['brier_calibrated']))}"
                    if vm
                    else "untrainable",
                    f"{fmt(float(vm['ece_raw']))} -> {fmt(float(vm['ece_calibrated']))}"
                    if vm
                    else "-",
                    c["rows"] if c else "-",
                    pct(c["base_rate"]) if c else "-",
                    pct(c["mean_predicted"]) if c else "-",
                    fmt(c["brier"]) if c else "-",
                    fmt(c["log_loss"]) if c else "-",
                    fmt(c["ece"]) if c else "-",
                    f"{c['accepted']} ({pct(c['accepted_win_rate'])} wins)" if c else "-",
                    f"{pct(c['mean_net_return_accepted'])} vs {pct(c['mean_net_return_all'])}"
                    if c
                    else "-",
                ]
            )
        lines += [f"**{tid}**", ""]
        lines += table(
            [
                "Fold",
                "Validation Brier raw -> calibrated",
                "Validation ECE raw -> calibrated",
                "Test candidates",
                "Test win rate",
                "Test mean predicted",
                "Test Brier",
                "Test log-loss",
                "Test ECE",
                "Accepted (test)",
                "Mean net return accepted vs all",
            ],
            rows,
        )
        lines.append("")
        pooled = t.get("calibration_pooled_test")
        if pooled:
            lines.append(
                f"Pooled test reliability ({pooled['rows']} candidates, Brier "
                f"{fmt(pooled['brier'])}, ECE {fmt(pooled['ece'])}):"
            )
            lines.append("")
            lines += table(
                ["Predicted", "Candidates", "Mean predicted", "Observed win rate"],
                [
                    [
                        f"{b['lower']:.1f} - {b['upper']:.1f}",
                        b["count"],
                        fmt(b["mean_predicted"]),
                        fmt(b["observed_rate"]),
                    ]
                    for b in pooled["bins"]
                ],
            )
            lines.append("")
        else:
            lines += ["No trained fold, so no test-window calibration.", ""]
    return lines


def _ablation(result: Mapping[str, Any]) -> list[str]:
    lines = [
        "Every variant is the same frozen rule in the same USD 50 engine with the same policy, fees "
        "and fill assumptions, each test window a fresh deployment. Only the filter differs. "
        "Controls are averaged over their salts; 'p' is the share of control salts whose net was "
        "at least the model's, counted as (1 + k) / (1 + N).",
        "",
    ]
    for tid, t in result["trials"].items():
        v = t["variants"]
        rows = []
        for name in ("rule_only", "model_filter"):
            s = v[name]
            rows.append(
                [
                    _VARIANT_LABELS[name],
                    s["trades"],
                    fmt(s["net_pnl_usd"], 2),
                    fmt(s["net_expectancy_per_trade_usd"]),
                    pct(s["win_rate"]),
                    fmt(s["max_drawdown_usd"], 2),
                    fmt(s["sharpe_annualised"], 2),
                    pct(s["exposure_fraction"]),
                    "-",
                ]
            )
        for name in ("random_control", "turnover_control"):
            c = v.get(name)
            if not c:
                continue
            rows.append(
                [
                    _VARIANT_LABELS[name],
                    fmt(c["mean_trades"], 1),
                    fmt(c["mean_net_pnl_usd"], 2),
                    fmt(c["pooled_net_per_trade_usd"]),
                    pct(c["pooled_win_rate"]),
                    fmt(c["mean_max_drawdown_usd"], 2),
                    fmt(c["sharpe_annualised_of_mean"], 2),
                    "-",
                    f"{c['salts_at_least_model']} of {c['seeds']} (p {fmt(c['p_value_model_not_better'], 3)})",
                ]
            )
        model = v["model_filter"]
        lines += [
            f"**{tid}** - the filter vetoed {model['filter_vetoes']} of "
            f"{model['filter_decisions']} candidates it saw.",
            "",
        ]
        lines += table(
            [
                "Variant",
                "Trades",
                "Net USD",
                "Net/trade",
                "Win rate",
                "Max DD USD",
                "Sharpe (ann.)",
                "Exposure",
                "Salts >= model",
            ],
            rows,
        )
        lines.append("")
        lines += table(
            [
                "Fold",
                "Rule net",
                "Model net (trades)",
                "Veto rate",
                "Random net mean [min, max]",
                "Equal-turnover net mean [min, max]",
            ],
            [
                [
                    f["fold"],
                    _usd(f["runs"]["rule_only"]["net_usd"]),
                    f"{_usd(f['runs']['model_filter']['net_usd'])} "
                    f"({f['runs']['model_filter']['trades']})",
                    pct(Decimal(f["runs"]["model_filter"]["veto_rate"])),
                    _fold_salts(f["runs"], "random_control"),
                    _fold_salts(f["runs"], "turnover_control"),
                ]
                for f in t["folds"]
            ],
        )
        lines.append("")
        lines.append(
            f"Random control over the walk-forward series: {_salt_range(v.get('random_control'))}"
            f" USD; equal-turnover control: {_salt_range(v.get('turnover_control'))} USD."
        )
        lines.append("")
    return lines


def _walk_forward(result: Mapping[str, Any]) -> list[str]:
    wf = result["walk_forward"]
    tids = list(result["trials"])
    lines = [
        "Per fold, the trial whose filter had the highest validation objective (net PnL per "
        "session of the validation replay at its chosen threshold) is deployed in the next test "
        "window if that objective is positive; otherwise the fold holds cash. Untrainable trials "
        "are not eligible.",
        "",
    ]
    lines += table(
        ["Fold", "Validation", "Test", *(f"{t} objective" for t in tids), "Selected"],
        [
            [
                f["fold"],
                _span(f["validation"]),
                _span(f["test"]),
                *(
                    _usd(f["validation_objective_usd_per_session"].get(t), 6)
                    if t in f["validation_objective_usd_per_session"]
                    else "untrained"
                    for t in tids
                ),
                f["selected"],
            ]
            for f in wf["folds"]
        ],
    )
    s = wf["summary"]
    lines += [
        "",
        f"- Sessions {s['sessions']} ({s.get('first_session')} .. {s.get('last_session')}), "
        f"trades {s['trades']}, net {fmt(s['net_pnl_usd'], 2)} USD, total project net "
        f"{fmt(s['total_project_net_pnl_usd'], 2)} USD (operating costs "
        f"{fmt(s['operating_costs_usd'], 2)}).",
        f"- Mean net per session (USD): {ci(s.get('mean_session_ci'), 5)}",
        f"- Net expectancy per trade (USD): {ci(s.get('per_trade_ci'), 4)}",
        f"- Effective sessions {fmt(s.get('effective_sessions'), 1)} (variance inflation "
        f"{fmt(s.get('variance_inflation'), 2)}); Sharpe {fmt(s['sharpe_annualised'], 2)} "
        f"({s['annualisation']}); max drawdown {fmt(s['max_drawdown_usd'], 2)} USD.",
    ]
    return lines


def _uncertainty(result: Mapping[str, Any]) -> list[str]:
    sel = result["selection_aware"]
    lines = []
    rc = sel.get("reality_check")
    if rc:
        lines.append(
            f"- Reality check over {rc['strategies']} variants x {rc['sessions']} test-window "
            f"sessions (every variant of every trial): best `{sel['reality_check_best_row']}` mean "
            f"{fmt(rc['best_mean'], 5)} USD/session; selection-adjusted one-sided 95% lower bound "
            f"{fmt(rc['selection_adjusted_lower_bound'], 5)}; p-value (no positive mean) "
            f"{fmt(rc['p_value_no_positive_mean'], 3)}."
        )
    else:
        lines.append("- Reality check: fewer than 10 test-window sessions; not computed.")
    dsr = sel.get("deflated_sharpe")
    if dsr:
        lines.append(
            f"- Deflated Sharpe ratio for the best model-filter series "
            f"`{sel['deflated_sharpe_row']}` (N = {sel['registered_trials']} registered trials, "
            f"baselines included): per-session Sharpe {fmt(dsr['sharpe'], 4)} vs expected "
            f"best-of-N {fmt(dsr['benchmark_sharpe'], 4)}; DSR {fmt(dsr['dsr'], 3)} (>0.95 would "
            "be needed)."
        )
    for tid, t in result["trials"].items():
        for name, interval in t["paired_differences"].items():
            label = name.replace("model_minus_", "").replace("_", " ")
            lines.append(
                f"- {tid} model filter minus {label}, mean USD per session: {ci(interval, 5)}"
            )
    lines += [
        "",
        "Paired intervals are stationary block bootstraps over the shared test-window sessions; a "
        "positive one-sided lower bound against both controls would be needed before a filter "
        "could be called better than trading less.",
    ]
    return lines


def _limitations(result: Mapping[str, Any]) -> list[str]:
    base_report = report_file_name(result["base_study_id"])
    items = [
        "A filter can only select from the rule's candidates; it cannot create an edge the "
        "candidates do not contain.",
    ]
    if result.get("track") == "CRYPTO":
        items.append(
            "The crypto baseline found no gross edge: with zero commission every rule trial was "
            "still negative after the assumed spread and slippage "
            "(research/reports/baseline_report.md), so a crypto filter starts from candidates "
            "without an edge."
        )
    else:
        items.append(
            f"Read these results with the base study's report (research/reports/{base_report})."
        )
    items += [
        "The validation window chooses the grid point, fits the calibration map and chooses the "
        "threshold, so validation figures are optimistic; only the test windows are "
        "out-of-sample.",
        "Labels simulate each candidate alone from a fresh USD 50 ledger. In the engine, pauses, "
        "entry limits, a pending entry and busy instruments act after the filter, so realised "
        "trades are a subset of the accepted candidates.",
        "The equal-turnover control matches the number of accepted candidates per test window, "
        "not USD turnover; the random control matches the veto rate only in expectation.",
        "A fold without a trainable model vetoes every candidate (cash), and its controls then "
        "trade nothing either.",
        "Bar-only replay: no quotes, queue position or intrabar path; costs are the base study's "
        "registered assumptions.",
        "Model checksums reproduce only with the recorded library versions; the registration "
        "binds them, so a changed environment needs a new registered trial.",
        "The walk-forward series is short relative to the G2 floor of 120 effective sessions and "
        "100 trades unless stated otherwise above; uncertainty intervals are wide.",
    ]
    return [f"- {item}" for item in items]


def render_model_report(result: Mapping[str, Any], *, generated_at: str, digest: str) -> str:
    """The Markdown model report for a ``research.model_study`` result."""
    lines: list[str] = []
    add = lines.append
    add("# Model study report (M3)")
    add("")
    add(
        "> **Evidence class: BACKTEST** (bar-based historical replay on a USD 50 ledger). This is "
        "not paper, shadow or live evidence and says nothing about future returns. Generated by "
        "`trader research models`; do not edit by hand."
    )
    add("")
    add(f"- Study: `{result['study_id']}` - {result['title']}")
    add(
        f"- Base study: `{result['base_study_id']}` (`{result['base_study_file']}`) - data, "
        "universe, costs, fills, policy and splits"
    )
    add(f"- Generated: {generated_at}; result digest `{digest[:16]}`")
    add(f"- Policy hash `{result['policy_hash'][:16]}`; fee schedule `{result['fee_schedule']}`")
    add(f"- Final test period locked: {'NO - unlocked' if result['holdout_unlocked'] else 'yes'}")
    for d in result["datasets"]:
        add(
            f"- Data: `{d['dataset']}` {d['instrument']}, {d['rows']} bars, "
            f"{(d['first'] or '-')[:10]} .. {(d['last'] or '-')[:10]}, content "
            f"`{d['content_sha256'][:12]}`"
        )
    add("")
    sections = (
        ("1. Verdict", _verdict),
        ("2. Chronological splits", _splits),
        ("3. Registered trials and protocol", _trials),
        ("4. Tuning history (validation only)", _tuning),
        ("5. Calibration", _calibration),
        ("6. Economic ablation (test windows)", _ablation),
        ("7. Walk-forward selection across trials (out-of-sample)", _walk_forward),
        ("8. Selection-aware uncertainty", _uncertainty),
        ("9. Bias and limitations", _limitations),
    )
    for title, render in sections:
        add(f"## {title}")
        add("")
        lines.extend(render(result))
        if lines[-1] != "":
            add("")
    add("## 10. Gate G2 assessment")
    add("")
    gate = result["g2"]
    lines.extend(
        table(
            ["Check", "Result"], [[k, "pass" if v else "FAIL"] for k, v in _ordered(gate["checks"])]
        )
    )
    add("")
    add(f"**G2 status: {gate['status']}.** {gate['note']}")
    add("")
    return "\n".join(lines)
