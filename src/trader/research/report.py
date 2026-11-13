"""Render a baseline study report (``research/reports/<study>_report.md``) from its result.

The verdict sentences are computed from the numbers; nothing is pre-filled as passed
(roadmap §2). Backtest evidence is labelled as such and kept separate from paper or live
evidence (instructions.md §5 "Reporting").

Wording that depends on the market (venue, fee basis, data source, universe, limitations) is
chosen by the study's track (``result["track"]``) and filled from the study's own registered
disclosures, so a stock study never inherits crypto sentences. A result written before the
track was recorded (the crypto baseline, ``crypto_baseline_v1``) renders with the crypto
wording, byte for byte as it was first published.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from trader.research.markdown import ci as _ci
from trader.research.markdown import fmt as _f
from trader.research.markdown import pct as _pct
from trader.research.markdown import table as _table
from trader.strategies.registry import family_label

# The G2 checks in the order the gate defines them (study._g2). A result read back from JSON
# has its keys sorted, so the table follows this order instead of the mapping's own order.
G2_CHECK_ORDER = (
    "oos_trades_at_least_100",
    "sessions_at_least_120",
    "effective_sessions_at_least_120",
    "positive_lower_bound_session_net",
    "positive_lower_bound_per_trade_net",
    "selection_adjusted_lower_bound_positive",
)

_FAMILY_LABELS = {
    "CANDIDATE_A": "Candidate A (trend continuation)",
    "CANDIDATE_B": "Candidate B (mean reversion)",
}


def _continuous(c: Mapping[str, Any]) -> str:
    stop = c.get("permanent_pause_at")
    tail = f"; paused {str(stop)[:10]}" if stop else "; never paused"
    return f"{_f(c['net_pnl_usd'], 2)} USD, {c['trades']} trades{tail}"


def _ordered_checks(checks: Mapping[str, bool]) -> list[tuple[str, bool]]:
    known = [(k, checks[k]) for k in G2_CHECK_ORDER if k in checks]
    return known + [(k, v) for k, v in checks.items() if k not in G2_CHECK_ORDER]


# ----------------------------------------------------------------------------- track wording


@dataclass(frozen=True, slots=True)
class Wording:
    """Market-specific sentences of a report; every number around them stays computed."""

    cost_basis: str  # verdict: what "the registered cost basis" consists of
    idle_reason: str  # verdict: why no trial could trade in a window
    fee_level: str  # verdict: the fee level the mid-price gross is compared with
    data_note: str  # section 2
    basis_title: str  # section 2: proxy-versus-venue basis table (only with basis data)
    basis_column: str
    basis_missing: str
    fees_line: str  # section 3
    entries_per: str  # section 3: the policy's entry-count window
    warmup: str  # section 3: what precedes each deployment window
    break_even_note: str  # section 5
    budget: str  # section 4
    benchmark_label: str  # section 7
    benchmark_note: str  # section 7
    limitations_before: tuple[str, ...]  # section 12, before the computed sequencing item
    limitations_after: tuple[str, ...]  # section 12, after it


_CRYPTO = Wording(
    cost_basis="(Kraken Pro Tier 1 taker fees, assumed spread and slippage)",
    idle_reason=(
        ": at the smallest legal Kraken order, price risk plus 0.80% taker fees on both legs "
        "exceeded the USD 0.125 per-trade risk budget. At USD 50 the venue minimums, not the "
        "strategy, decide whether trading is even permitted."
    ),
    fee_level="(Kraken Tier 1 taker costs about 0.08 USD per round trip at these sizes)",
    data_note=(
        "Coinbase candles omit intervals without trades, so a missing bar may be an outage or a "
        "quiet interval; research treats it as missing data (feature state resets and must warm "
        "up again; an entry needs the next bar within the order TTL). Coinbase supplies no VWAP "
        "or trade count; the strategies use a volume-weighted *typical price* reference that a "
        "live Kraken process computes identically. Full provenance: `research/data_manifest.json`."
    ),
    basis_title=(
        "**Proxy basis (Coinbase prices standing in for Kraken execution)**, measured on a "
        "recent common week; no strategy was evaluated on it:"
    ),
    basis_column="Mean Kraken-Coinbase (bps)",
    basis_missing=(
        "**Proxy basis:** not measurable in this checkout. The recent Kraken capture (720-row "
        "endpoint) cannot be re-downloaded later; the values measured at acquisition are in "
        "the committed report and in docs/data_coverage.md."
    ),
    fees_line=(
        "(Kraken Pro Tier 1: 0.40% maker / 0.80% taker, documented 2026-11-06; marketable "
        "orders pay taker on both legs; rounded up to 0.0001)."
    ),
    entries_per="UTC day",
    warmup="two days of bars warm up the features",
    break_even_note="Kraken Tier 1 taker is 80 bps per side.",
    budget="the project's ten-configuration budget",
    benchmark_label="Buy-and-hold BTC+ETH (policy-capped)",
    benchmark_note=(
        "Buy-and-hold is limited by the same symbol cap, gross cap and cash reserve, so it holds "
        "roughly USD 9-10 per symbol over the walk-forward test span; its exposure is far higher "
        "than the rules', which is why it is a context benchmark rather than an exposure-matched "
        "control. An AI-filtered comparator belongs to M3."
    ),
    limitations_before=(
        "Fixed two-symbol universe chosen by specification, not point-in-time membership; "
        "results do not generalise to other assets.",
        "Coinbase prices proxy for Kraken execution; the measured basis is small but non-zero "
        "and was observed over one recent week only.",
        "Bar-only replay: no quotes, queue position or intrabar path; passive (maker) execution "
        "is therefore not modelled, and taker fees apply to every fill.",
    ),
    limitations_after=(
        "One USD account currency assumed; EUR pairs, FX and the owner's actual fee tier are "
        "unknown. Taxes are not modelled.",
        "Loss-pause reviews are assumed at the next window; a real owner might pause longer.",
        "The walk-forward series is short relative to the G2 floor of 120 effective sessions and "
        "100 trades unless stated otherwise above; uncertainty intervals are wide.",
    ),
)

_GENERIC_TAIL = (
    "Loss-pause reviews are assumed at the next window; a real owner might pause longer.",
    "The walk-forward series is short relative to the G2 floor of 120 effective sessions and "
    "100 trades unless stated otherwise above; uncertainty intervals are wide.",
)


def _base_totals(result: Mapping[str, Any]) -> tuple[int, float, float]:
    """Trades, fees and assumed spread+slippage summed over every trial's base view."""
    views = [t["views"]["base"] for t in result["trials"].values()]
    return (
        sum(int(v["trades"]) for v in views),
        sum(float(v["fees_usd"]) for v in views),
        sum(float(v["execution_cost_usd"]) for v in views),
    )


def _stocks_wording(result: Mapping[str, Any]) -> Wording:
    schedule = result["fee_schedule"]
    description = result.get("fee_schedule_description") or "see the fee schedule file"
    feeds = sorted({str(d["feed"]) for d in result["datasets"]})
    symbols = [str(i).split(":")[-1] for i in result.get("universe", [])]
    disclosures = result.get("disclosures") or {}
    trades, fees, _ = _base_totals(result)
    fee_level = (
        f"(the registered fees averaged {_f(fees / trades, 4)} USD per round trip in the base runs)"
        if trades
        else "(no trade was taken under the registered fees)"
    )
    per_symbol = result["comparators"]["buy_and_hold"].get("per_symbol_notional_usd")
    holding = (
        f"about USD {_f(per_symbol, 2)} per symbol"
        if per_symbol is not None
        else "an equal policy-capped notional per symbol"
    )
    single_venue = any("IEX" in f for f in feeds)
    overnight = _overnight_trials(result)
    data_note = (
        f"Bars from {', '.join(f'`{f}`' for f in feeds)} are raw (unadjusted); splits and symbol "
        "changes are applied point-in-time from the corporate-actions record. A missing bar is "
        "treated as missing data (feature state resets and must warm up again; an entry needs "
        + (
            "the bar at the next session's open)."
            if overnight
            else "the next bar within the order TTL)."
        )
    )
    if overnight:
        data_note += (
            " Distributions are neither credited nor adjusted: an ex-dividend drop counts as a "
            "price loss."
        )
    if single_venue:
        data_note += (
            " IEX is a single venue: bar volume (the participation cap) and the source session "
            "VWAP (the rules' reference) are IEX-only and are never compared with consolidated "
            "volume."
        )
    data_note += " Full provenance: `research/data_manifest.json`."
    declared = [
        f"{label} (declared at registration): {disclosures[key]}"
        for key, label in (
            ("universe_selection", "Universe"),
            ("entry_exit_timing", "Entry, exit and feed"),
        )
        if disclosures.get(key)
    ]
    return Wording(
        cost_basis=f"(fee schedule `{schedule}`, assumed spread and slippage)",
        idle_reason="; the candidate outcomes in section 5 give the rejection reasons.",
        fee_level=fee_level,
        data_note=data_note,
        basis_title=(
            "**Proxy basis (proxy prices standing in for the execution venue)**, measured on a "
            "common window; no strategy was evaluated on it:"
        ),
        basis_column="Mean venue-proxy (bps)",
        basis_missing="**Proxy basis:** not measurable in this checkout.",
        fees_line=f"({description}).",
        entries_per="NY session",
        warmup=(
            f"bars of the {result['warmup_sessions']} preceding exchange sessions feed the "
            "features only; no decision before the window is traded"
            if result.get("warmup_sessions")
            else "bars from the two preceding calendar days feed the features, and both stock "
            "rules reset their state at every session open"
        ),
        break_even_note="Compare it with the registered fee schedule in section 3.",
        budget=(
            f"the project's trial budget ({result['trial_budget']} configurations when this "
            "study ran)"
            if result.get("trial_budget") is not None
            else "the project's trial budget"
        ),
        benchmark_label=(
            f"Buy-and-hold {'+'.join(symbols)} (policy-capped)"
            if symbols
            else "Buy-and-hold universe (policy-capped)"
        ),
        benchmark_note=(
            f"Buy-and-hold is limited by the same symbol cap, gross cap and cash reserve, so it "
            f"holds {holding} over the walk-forward test span; its exposure is far higher "
            "than the rules', which is why it is a context benchmark rather than an "
            "exposure-matched control. An AI-filtered comparator belongs to M3."
        ),
        limitations_before=(
            *declared,
            "Bar-only replay: no quotes, queue position or intrabar path; the spread is an "
            "assumption rather than a measurement, and passive (limit-resting) execution is not "
            "modelled.",
        ),
        limitations_after=(
            f"Costs: {description}. Taxes are not modelled; one USD account currency is assumed.",
            *_GENERIC_TAIL,
        ),
    )


def _overnight_trials(result: Mapping[str, Any]) -> dict[str, Mapping[str, str]]:
    """The declared timing of multi-day (overnight) trials; empty for intraday studies."""
    return {tid: t["timing"] for tid, t in result.get("trials", {}).items() if t.get("timing")}


def _entries_line(result: Mapping[str, Any]) -> str:
    """Section 3's entry and exit mechanics (daily wording only when a trial holds overnight)."""
    overnight = _overnight_trials(result)
    if not overnight:
        return (
            "- Entries: marketable limit at estimated ask + 5 bps, filled only at the next bar's "
            "open within the limit, else missed. Stop is checked before target in the same bar; "
            "the target needs the estimated bid strictly through it. The signal bar's close is "
            "never a fill price."
        )
    offsets = "; ".join(
        f"{tid} {t['entry_limit_offset_bps']} bps"
        + (f", at most {t['max_hold_sessions']} sessions" if t.get("max_hold_sessions") else "")
        + (", target is a cost gate only" if t.get("target_role") == "COST_GATE" else "")
        for tid, t in overnight.items()
    )
    return (
        "- Entries (daily bars, positions carried overnight): decided on a completed session's "
        "close; a marketable limit at the estimated ask plus the rule's frozen offset "
        f"({offsets}), filled only at the next session's open within the limit, else missed. "
        "Exit signals and time exits sell at the next open; a stop rests for the whole hold, is "
        "checked before the target in the same bar, and an open through it sells at that open "
        "(STOP_GAP). The target needs the estimated bid strictly through it. The signal bar's "
        "close is never a fill price."
    )


def _crypto_daily_wording(result: Mapping[str, Any]) -> Wording:
    """The crypto sentences for a daily (overnight) crypto study: its own data, warm-up, fee and
    budget sentences. The 15-minute crypto studies keep :data:`_CRYPTO` byte for byte."""
    trades, fees, _ = _base_totals(result)
    warmup = result.get("warmup_sessions")
    budget = result.get("trial_budget")
    return replace(
        _CRYPTO,
        idle_reason=(
            ": a trend rule holds cash while its month-end closes stay below their average, and "
            "at USD 50 the smallest legal Kraken order can exceed the USD 0.125 per-trade risk "
            "budget; the candidate outcomes in section 5 give the rejection reasons."
        ),
        fee_level=(
            f"(Kraken Tier 1 taker fees averaged {_f(fees / trades, 4)} USD per round trip in "
            "the base runs)"
            if trades
            else "(no trade was taken under the Kraken Tier 1 taker fees)"
        ),
        data_note=(
            "Coinbase daily candles (one per UTC day, 00:00-24:00 UTC) stand in for Kraken "
            "prices. A day without trades has no candle, so a missing day may be an outage or a "
            "day without trades; research treats it as missing data (the rule's daily features "
            "reset and must warm up again; an entry needs the bar at the next day's open). "
            "Coinbase supplies no VWAP or trade count, and the daily rules use neither. Full "
            "provenance: `research/data_manifest.json`."
        ),
        warmup=(
            f"bars of the {warmup} preceding UTC days feed the features only; no decision before "
            "the window is traded"
            if warmup
            else _CRYPTO.warmup
        ),
        budget=(
            f"the project's trial budget ({budget} configurations when this study ran)"
            if budget is not None
            else "the project's trial budget"
        ),
    )


#: Section 11 of a daily crypto study (the stock studies have their own reports).
_CRYPTO_DAILY_STOCK_NOTE = (
    "- Stock-track results are in their own reports (`research/reports/"
    "stocks_baseline_v1_report.md`, `research/reports/etf_daily_v1_report.md`); this crypto "
    "study does not restate them."
)


def wording_for(result: Mapping[str, Any]) -> Wording:
    """The sentences for the study's track; a result without a track is the crypto baseline."""
    track = result.get("track", "CRYPTO")
    if track == "CRYPTO":
        return _crypto_daily_wording(result) if _overnight_trials(result) else _CRYPTO
    if track == "STOCKS":
        return _stocks_wording(result)
    raise ValueError(f"no baseline report wording for track {track}")


# ----------------------------------------------------------------------------- sections


def _verdict(result: Mapping[str, Any], w: Wording) -> list[str]:
    trials = result["trials"]
    wf = result["walk_forward"]["summary"]
    folds = result["walk_forward"]["folds"]
    positive = [tid for tid, t in trials.items() if t["views"]["base"]["net_pnl_usd"] > 0]
    cash_folds = sum(1 for f in folds if f["selected"] == "CASH")
    lines = []
    if positive:
        lines.append(
            f"- Trials with positive development-period net PnL under the registered costs: "
            f"{', '.join(positive)} (before any selection correction)."
        )
    else:
        lines.append(
            "- **No registered trial made money** over the development period under the "
            f"registered cost basis {w.cost_basis}."
        )
    first = next(iter(trials.values()))["views"]["base"]
    idle = [
        win[:10]
        for k, win in enumerate(first.get("windows", []))
        if all(t["views"]["base"]["window_trades"][k] == 0 for t in trials.values())
    ]
    if idle:
        lines.append(
            f"- In {len(idle)} of {len(first['windows'])} windows (starting {', '.join(idle)}) no "
            f"trial could trade at all{w.idle_reason}"
        )
    mid = {
        tid: t["views"]["diagnostic"]["gross_pnl_usd"]
        + t["views"]["diagnostic"]["execution_cost_usd"]
        for tid, t in trials.items()
    }
    lines.append(
        "- Before any spread, slippage or commission (mid-price gross of the zero-commission runs) "
        "the rules made "
        + ", ".join(f"{tid} {_f(v, 2)}" for tid, v in mid.items())
        + " USD (same fill model; section 12 shows how few trades the stop-before-target rule "
        "decided)."
    )
    diag_positive = [
        tid for tid, t in trials.items() if t["views"]["diagnostic"]["net_pnl_usd"] > 0
    ]
    if diag_positive:
        lines.append(
            "- With commissions removed (diagnostic, not achievable) these trials were positive: "
            f"{', '.join(diag_positive)}; see the break-even fee column."
        )
    else:
        best = max(mid, key=lambda k: mid[k])
        best_trades = trials[best]["views"]["diagnostic"]["trades"] or 1
        lines.append(
            "- Even with **zero commission** (diagnostic) no trial was positive after the assumed "
            f"spread and slippage. The best mid-price gross ({best}) is "
            f"{_f(mid[best] / best_trades, 5)} USD per trade, far below any realistic fee level "
            f"{w.fee_level}."
        )
    lines.append(
        f"- Walk-forward selection held cash in {cash_folds} of {len(folds)} folds; the "
        f"walk-forward series made {_f(wf.get('net_pnl_usd'), 2)} USD over "
        f"{wf.get('sessions')} sessions with {wf.get('trades')} trades."
    )
    gate = result["g2"]
    lines.append(
        f"- **G2 (research evidence): {gate['status']}.** Live or paper promotion of any of these "
        "baselines is not supported by this evidence."
    )
    return lines


def _crypto_stock_status(stock_status: Mapping[str, str] | None) -> list[str]:
    """Section 11 of a crypto study: where the stock track stood when the report was made."""
    lines = []
    if stock_status is not None:
        lines.append(
            f"- Dataset `{stock_status['dataset']}`: **{stock_status['status']}** - "
            f"{stock_status['detail']}"
        )
    lines.append(
        "- Candidate A (runtime implementation, 5-minute bars, session VWAP) and Candidate B "
        "(`candidate_b_stocks_v1`) are frozen; neither has been evaluated on real stock data. The "
        "research engine, Candidate A parity with the runtime replay and the split, symbol-change, "
        "missing-data, DST and early-close handling are verified on the deterministic synthetic "
        "dataset `synthetic_equities_5m_v1` only."
    )
    lines.append(
        "- Alpaca commission and regulatory fees remain unverified (docs/cost_feasibility.md); "
        "stock results would also need a documented cost basis before registration."
    )
    return lines


def _stock_results(result: Mapping[str, Any]) -> list[str]:
    """Section 11 of a stock study: what the stock trials showed on the development period."""
    lines = []
    rows = result["datasets"]
    splits = result["splits"]
    if rows:
        names = sorted({str(d["dataset"]) for d in rows})
        feeds = sorted({str(d["feed"]) for d in rows})
        firsts = [str(d["first"])[:10] for d in rows if d.get("first")]
        lasts = [str(d["last"])[:10] for d in rows if d.get("last")]
        span = f"bars {min(firsts)} .. {max(lasts)}" if firsts and lasts else "no bars"
        lines.append(
            f"- Evaluated on {', '.join(f'`{n}`' for n in names)} ({', '.join(feeds)}; "
            f"{len(rows)} instruments, {span}) over the development period "
            f"{splits['development']}; the final test period {splits['locked_final_test']} "
            f"{'was UNLOCKED' if result['holdout_unlocked'] else 'stays locked'}."
        )
    families: dict[str, list[str]] = {}
    for tid, t in result["trials"].items():
        families.setdefault(str(t["family"]), []).append(tid)
    for family, tids in families.items():
        parts = []
        for tid in tids:
            views = result["trials"][tid]["views"]
            parts.append(
                f"{tid} {_f(views['base']['net_pnl_usd'], 2)} USD over "
                f"{views['base']['trades']} trades (stressed "
                f"{_f(views['stressed']['net_pnl_usd'], 2)}, zero-fee "
                f"{_f(views['diagnostic']['net_pnl_usd'], 2)})"
            )
        best = max(tids, key=lambda k: result["trials"][k]["views"]["base"]["net_pnl_usd"])
        ranking = (
            f" Best development net: {best} (uncorrected for selection)." if len(tids) > 1 else ""
        )
        lines.append(
            f"- {_FAMILY_LABELS.get(family) or family_label(family)}, {len(tids)} registered "
            f"variant{'s' if len(tids) != 1 else ''}: {'; '.join(parts)}.{ranking}"
        )
    wf = result["walk_forward"]
    folds = wf["folds"]
    chosen = [str(f["selected"]) for f in folds]
    picks = ", ".join(f"{c} {chosen.count(c)}" for c in dict.fromkeys(chosen)) or "no folds"
    s = wf["summary"]
    lines.append(
        f"- Walk-forward selection over {len(folds)} folds ({picks}): "
        f"{_f(s.get('net_pnl_usd'), 2)} USD over {s.get('sessions')} sessions with "
        f"{s.get('trades')} trades; G2 {result['g2']['status']}."
    )
    trades, fees, execution = _base_totals(result)
    per_trade = f"{_f(fees / trades, 4)} USD fees per trade" if trades else "no trades"
    lines.append(
        f"- Costs in the base runs under `{result['fee_schedule']}`: {_f(fees, 2)} USD fees and "
        f"{_f(execution, 2)} USD assumed spread and slippage over {trades} trades ({per_trade})."
    )
    lines.append(
        "- Read every number above with section 12: the universe, event-calendar, feed and "
        "order-type limitations declared at registration apply to all of them."
    )
    return lines


def render_baseline_report(
    result: Mapping[str, Any],
    *,
    generated_at: str,
    digest: str,
    stock_status: Mapping[str, str] | None = None,
) -> str:
    """The Markdown report. ``stock_status`` (the stock dataset's status) is shown by crypto
    studies, which report where the stock track stands; a stock study reports its results."""
    w = wording_for(result)
    stocks = result.get("track") == "STOCKS"
    lines: list[str] = []
    add = lines.append
    add("# Research baseline report (M2)")
    add("")
    add(
        "> **Evidence class: BACKTEST** (bar-based historical replay on a USD 50 ledger). This is "
        "not paper, shadow or live evidence and says nothing about future returns. Generated by "
        "`trader research baseline`; do not edit by hand."
    )
    add("")
    add(f"- Study: `{result['study_id']}` - {result['title']}")
    add(f"- Generated: {generated_at}; result digest `{digest[:16]}`")
    add(f"- Policy hash `{result['policy_hash'][:16]}`; fee schedule `{result['fee_schedule']}`")
    add(f"- Final test period locked: {'NO - unlocked' if result['holdout_unlocked'] else 'yes'}")
    add("")
    add("## 1. Verdict")
    add("")
    lines.extend(_verdict(result, w))
    add("")

    add("## 2. Data")
    add("")
    rows = [
        [
            d["dataset"],
            d["instrument"],
            d["rows"],
            f"{(d['first'] or '-')[:10]} .. {(d['last'] or '-')[:10]}",
            d["coverage"],
            d["missing_intervals"],
            "; ".join(d["largest_gaps"]) or "-",
            f"`{d['content_sha256'][:12]}`",
        ]
        for d in result["datasets"]
    ]
    lines += _table(
        ["Dataset", "Instrument", "Bars", "Range", "Coverage", "Missing", "First gaps", "Content"],
        rows,
    )
    add("")
    add(w.data_note)
    add("")
    basis = [b for b in result.get("basis", []) if "mean_close_diff_bps" in b]
    if basis:
        add(w.basis_title)
        add("")
        lines += _table(
            [
                "Instrument",
                "Common bars",
                w.basis_column,
                "Mean abs (bps)",
                "p95 abs (bps)",
                "15m return corr.",
            ],
            [
                [
                    b["instrument"],
                    b["common_bars"],
                    _f(b["mean_close_diff_bps"], 2),
                    _f(b["mean_abs_close_diff_bps"], 2),
                    _f(b["p95_abs_close_diff_bps"], 2),
                    _f(b["return_correlation"], 4),
                ]
                for b in basis
            ],
        )
        add("")
    elif result.get("basis"):
        add(w.basis_missing)
        add("")

    months = result.get("window_months", 3)
    add("## 3. Execution and cost assumptions")
    add("")
    fills = result["fills"]
    add(f"- Fees: `{result['fee_schedule']}` {w.fees_line}")
    for name in ("base", "stressed"):
        f = fills[name]
        add(
            f"- {name.capitalize()} fills: half-spread {f['half_spread_bps']} bps, slippage "
            f"{f['slippage_bps']} bps, participation {f['participation']} of bar volume, latency "
            f"{f['latency_ms']} ms, simulated missed entries {f['missed_entry_percent']}%."
        )
    add(_entries_line(result))
    add(
        "- Risk at USD 50 (shared policy): planned loss per trade <= 0.125 USD including both "
        "fees and a 10 bps gap allowance; <= 10 USD per symbol, <= 20 USD gross, >= 30 USD cash "
        f"reserve; at most 2 new entries per {w.entries_per} and one pending entry; daily 0.50 "
        "USD and weekly 1.25 USD loss pauses, 10% drawdown and the USD 45 floor."
    )
    add(
        f"- Research assumptions: every {months}-month window is an independent deployment from "
        f"a fresh USD 50 ledger ({w.warmup}; a position still open at the window end is closed "
        "at the last bar's estimated bid). Inside a window a daily/weekly loss pause is treated "
        "as reviewed at the next loss window, and a drawdown or floor pause stops new entries "
        "until the window ends."
    )
    add(
        "- Bar data cannot resolve sub-bar order: these results are preliminary strategy "
        "research and do not certify execution realism (risk_and_validation.md §6)."
    )
    add("")

    add("## 4. Registered trials")
    add("")
    trials = result["trials"]
    lines += _table(
        ["Trial", "Family", "Overrides", "Spec hash", "Registration"],
        [
            [
                tid,
                t["family"],
                ", ".join(f"{k}={v}" for k, v in t["overrides"].items()) or "(base spec)",
                f"`{t['spec_hash'][:12]}`",
                f"`{t['registration_hash'][:12]}`",
            ]
            for tid, t in trials.items()
        ],
    )
    add("")
    narrowed = {tid: t["universe"] for tid, t in trials.items() if t.get("universe")}
    if narrowed:  # trials that trade part of the study universe (registered per trial)
        study_universe = result.get("universe", [])
        for tid in trials:
            ids = narrowed.get(tid, study_universe)
            symbols = ", ".join(str(i).split(":")[-1] for i in ids)
            add(f"- {tid} universe ({len(ids)} of {len(study_universe)}): {symbols}")
        add("")
    add(
        f"All {len(trials)} trials were registered in `research/experiment_registry.jsonl` before "
        f"running and count against {w.budget}, including failures."
    )
    add("")

    add("## 5. Development-period results per trial")
    add("")
    add(
        "Each trial is a separate counterfactual; different trials are never summed. Each row "
        f"adds up the trial's consecutive {months}-month deployments over the development period "
        "(the locked final test is excluded), each starting from USD 50. 'Continuous' runs the "
        "same rule as one deployment over the whole period and shows when the policy's 10% "
        "drawdown or USD 45 floor pause would have stopped it for owner review."
    )
    add("")
    rows = []
    for tid, t in trials.items():
        b, s, d = (t["views"][k] for k in ("base", "stressed", "diagnostic"))
        rows.append(
            [
                tid,
                b["trades"],
                _f(b["net_pnl_usd"], 2),
                _f(b["net_expectancy_per_trade_usd"]),
                _pct(b["win_rate"]),
                _f(b["profit_factor"], 2),
                _f(b["max_drawdown_usd"], 2),
                _f(b["fees_usd"], 2),
                _f(b["execution_cost_usd"], 2),
                _f(s["net_pnl_usd"], 2),
                _f(d["net_pnl_usd"], 2),
                _f(t["views"]["break_even_commission_per_side_bps"], 1),
                f"{b['negative_windows']}/{len(b['window_nets_usd'])}",
                _f(d["gross_pnl_usd"] + d["execution_cost_usd"], 2),
                _continuous(t["views"]["continuous"]),
            ]
        )
    lines += _table(
        [
            "Trial",
            "Trades",
            "Net USD",
            "Net/trade",
            "Win rate",
            "Profit factor",
            "Max DD USD",
            "Fees",
            "Spread+slip",
            "Stressed net",
            "Zero-fee net",
            "Break-even fee (bps/side)",
            "Losing windows",
            "Mid-price gross (zero fee)",
            "Continuous",
        ],
        rows,
    )
    add("")
    add(
        "The break-even fee is the per-side commission at which the zero-commission run would net "
        "zero; it is an upper bound on an affordable fee level (negative = no fee level works). "
        f"{w.break_even_note}"
    )
    add("")
    add("**Candidate outcomes (base view)**")
    add("")
    for tid, t in trials.items():
        c = t["views"]["base"].get("candidates", {})
        reasons = ", ".join(f"{k} {v}" for k, v in c.get("by_reason", {}).items()) or "none"
        status = ", ".join(f"{k} {v}" for k, v in c.get("by_status", {}).items())
        add(f"- {tid}: {c.get('total', 0)} candidates - {status}; reasons: {reasons}")
    add("")

    add("## 6. Walk-forward selection (out-of-sample)")
    add("")
    wf = result["walk_forward"]
    add(wf["selection_rule"])
    add("")
    lines += _table(
        ["Fold", "Validation", "Test", "Best validation net/session", "Selected"],
        [
            [
                f["fold"],
                f"{f['validation'][0][:10]} .. {f['validation'][1][:10]}",
                f"{f['test'][0][:10]} .. {f['test'][1][:10]}",
                _f(max(f["validation_net_per_session"].values()), 5),
                f["selected"],
            ]
            for f in wf["folds"]
        ],
    )
    add("")
    s = wf["summary"]
    add(
        f"- Sessions {s['sessions']} ({s.get('first_session')} .. {s.get('last_session')}), "
        f"trades {s['trades']}, net {_f(s['net_pnl_usd'], 2)} USD, total project net "
        f"{_f(s['total_project_net_pnl_usd'], 2)} USD (operating costs {_f(s['operating_costs_usd'], 2)})."
    )
    add(f"- Mean net per session (USD): {_ci(s.get('mean_session_ci'), 5)}")
    add(f"- Net expectancy per trade (USD): {_ci(s.get('per_trade_ci'), 4)}")
    add(
        f"- Effective sessions {_f(s.get('effective_sessions'), 1)} (variance inflation "
        f"{_f(s.get('variance_inflation'), 2)}); Sharpe {_f(s['sharpe_annualised'], 2)} "
        f"({s['annualisation']}); max drawdown {_f(s['max_drawdown_usd'], 2)} USD."
    )
    power = s.get("power_sessions") or {}
    if power:
        add(
            "- Prospective power (80%, one-sided 5%): sessions needed to detect the observed mean "
            f"{_f(power.get('observed_mean'), 0)}; 0.01 USD/session {_f(power.get('0.01_usd_per_session'), 0)}; "
            f"0.05 USD/session {_f(power.get('0.05_usd_per_session'), 0)}."
        )
    add("")

    add("## 7. Comparators (same USD 50, same costs and policy)")
    add("")
    comp = result["comparators"]
    bh = comp["buy_and_hold"]
    lines += _table(
        ["Strategy", "Net USD", "Mean/session", "Max DD USD", "Exposure", "Trades"],
        [
            ["Cash / no trade", "0.00", "0.0000", "0.00", "0%", 0],
            [
                w.benchmark_label,
                _f(bh["net_pnl_usd"], 2),
                _f(bh["mean_session_pnl_usd"]),
                _f(bh["max_drawdown_usd"], 2),
                "100% of capped notional",
                "1 per symbol",
            ],
            [
                "Walk-forward deterministic rules",
                _f(s["net_pnl_usd"], 2),
                _f(s["mean_session_pnl_usd"]),
                _f(s["max_drawdown_usd"], 2),
                _pct(s["exposure_fraction"]),
                s["trades"],
            ],
        ],
    )
    add("")
    add(w.benchmark_note)
    add("")

    add("## 8. Selection-aware statistics")
    add("")
    sel = result["selection_aware"]
    rc = sel.get("reality_check")
    if rc:
        best_tid = list(trials)[int(rc["best_index"])]
        add(
            f"- Reality check over {rc['strategies']} trials x {rc['sessions']} test-window "
            f"sessions: best trial {best_tid} mean {_f(rc['best_mean'], 5)} USD/session; "
            f"selection-adjusted one-sided 95% lower bound {_f(rc['selection_adjusted_lower_bound'], 5)}; "
            f"p-value (no positive mean) {_f(rc['p_value_no_positive_mean'], 3)}."
        )
    dsr = sel.get("deflated_sharpe")
    if dsr:
        add(
            f"- Deflated Sharpe ratio for the best trial (N = {sel['registered_trials']} "
            f"registered trials): per-session Sharpe {_f(dsr['sharpe'], 4)} vs expected best-of-N "
            f"{_f(dsr['benchmark_sharpe'], 4)}; DSR {_f(dsr['dsr'], 3)} (>0.95 would be needed)."
        )
    add("")

    add("## 9. Stress and loss-trigger probabilities")
    add("")
    trig = result.get("loss_trigger_probabilities")
    if trig:
        add(
            f"Model-dependent path bootstrap of the walk-forward series over {int(trig['horizon_sessions'])} "
            f"sessions: daily limit {_pct(trig['daily_limit'])} (session sums understate intraday "
            f"losses), weekly limit {_pct(trig['weekly_limit'])}, 10% drawdown "
            f"{_pct(trig['high_water_drawdown'])}, USD 45 floor {_pct(trig['capital_floor'])}. "
            "These are model estimates, not guarantees."
        )
    add("")
    rows = [
        [
            tid,
            _pct(tr["daily_limit"]),
            _pct(tr["weekly_limit"]),
            _pct(tr["high_water_drawdown"]),
            _pct(tr["capital_floor"]),
        ]
        for tid, t in trials.items()
        if (tr := t["views"].get("loss_triggers"))
    ]
    if rows:
        add(
            "Probability that each rule, deployed from USD 50, reaches a loss trigger within 90 "
            "sessions (stationary block bootstrap of its own development-period session PnL; "
            "model-dependent, and session sums understate intraday excursions):"
        )
        add("")
        lines.extend(
            _table(["Trial", "Daily limit", "Weekly limit", "10% drawdown", "USD 45 floor"], rows)
        )
        add("")
    add(
        "Stressed runs (2x spread and slippage plus 5% simulated missed entries) are in the "
        "'Stressed net' column of section 5. A stressed run can lose less than the base run "
        "because wider assumed costs make the cost gate reject more candidates, so fewer "
        "losing trades are taken; that is not evidence of robustness."
    )
    add("")

    add("## 10. Regimes (descriptive)")
    add("")
    reg = result.get("regimes") or {}
    if reg:
        add(reg.get("definition", ""))
        add("")
        lines += _table(
            ["Regime", "Sessions", "Walk-forward net USD", "Buy-and-hold net USD"],
            [
                [
                    name,
                    reg[name]["sessions"],
                    _f(reg[name]["strategy_net_usd"], 2),
                    _f(reg[name]["benchmark_net_usd"], 2),
                ]
                for name in ("trending_up", "choppy", "adverse", "high_volatility")
                if name in reg
            ],
        )
    add("")

    add("## 11. Stocks track")
    add("")
    if stocks:
        lines.extend(_stock_results(result))
    elif _overnight_trials(result):
        lines.append(_CRYPTO_DAILY_STOCK_NOTE)
    else:
        lines.extend(_crypto_stock_status(stock_status))
    add("")

    add("## 12. Bias and limitations")
    add("")
    sequencing = (
        "Pessimistic intrabar sequencing: when one bar touches both the stop and the target, the "
        "stop is assumed (never target-first). Zero-commission runs: "
        + "; ".join(
            f"{tid} {t['views']['diagnostic']['ambiguous_bar_stops']} of "
            f"{t['views']['diagnostic']['trades']} trades"
            for tid, t in trials.items()
        )
        + " were decided this way, so this bias is small. (Stops inside the entry bar are not "
        "an assumption: the entry fills at that bar's open, so its low comes after the entry.)"
    )
    for item in (*w.limitations_before, sequencing, *w.limitations_after):
        add(f"- {item}")
    add("")

    add("## 13. Gate G2 assessment")
    add("")
    gate = result["g2"]
    lines += _table(
        ["Check", "Result"],
        [[k, "pass" if v else "FAIL"] for k, v in _ordered_checks(gate["checks"])],
    )
    add("")
    add(f"**G2 status: {gate['status']}.** {gate['note']}")
    add("")
    capital = result.get("capital_views")
    if capital:  # only studies that declared diagnostic capitals have this section
        lines.extend(_capital_section(capital))
        add("")
    return "\n".join(lines)


def _capital_section(views: Mapping[str, Any]) -> list[str]:
    """Section 14: the diagnostic capital views (never part of G2 or selection)."""
    base = views.get("base_capital_usd", "50.00")
    lines = [
        "## 14. Capital sensitivity (diagnostic only)",
        "",
        f"> **Diagnostic only - not evidence.** {views['note']}",
        "",
        f"Scaled policy fields: {', '.join(f'`{f}`' for f in views['scaled_policy_fields'])}. "
        f"The USD {base} results are the base views of section 5; walk-forward selection "
        "(section 6), the selection-aware statistics (section 8) and gate G2 (section 13) use "
        "only those.",
        "",
    ]
    rows = []
    for tid, per_capital in views["trials"].items():
        for capital, v in per_capital.items():
            rows.append(
                [
                    tid,
                    capital,
                    v["trades"],
                    _f(v["net_pnl_usd"], 2),
                    _pct(v["net_return_on_capital"]),
                    _f(v["gross_pnl_usd"], 2),
                    _f(v["fees_usd"], 2),
                    _f(v["fees_per_trade_usd"], 4),
                    _f(v["execution_cost_usd"], 2),
                    _pct(v["win_rate"]),
                    _f(v["profit_factor"], 2),
                    _f(v["max_drawdown_usd"], 2),
                    f"{v['negative_windows']}/{len(v['window_nets_usd'])}",
                ]
            )
    lines += _table(
        [
            "Trial",
            "Capital USD",
            "Trades",
            "Net USD",
            "Net / capital",
            "Gross USD",
            "Fees",
            "Fees/trade",
            "Spread+slip",
            "Win rate",
            "Profit factor",
            "Max DD USD",
            "Losing windows",
        ],
        rows,
    )
    lines += [
        "",
        "Fees rounded up per order (a cent per fee type) weigh far less at a larger capital, "
        "while spread and slippage scale with the notional. A better result at a larger capital "
        "does not make the USD 50 deployment tradable and is not a reason to change the policy.",
    ]
    return lines
