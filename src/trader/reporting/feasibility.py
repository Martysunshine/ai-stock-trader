"""Computed tables for docs/cost_feasibility.md (instructions.md §6.3; CRYPTO §6; COMMODITIES §5).

All numbers come from the canonical policy, the fee schedules in ``configs/fees`` and the
instrument fixtures in ``configs/instruments``. Prices used for share-count conversion are
illustrative and labelled as such; costs scale with notional, not with the illustrative price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trader.accounting.fees import FeeSchedule, load_fee_schedule
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.config.policy import LoadedPolicy, dollar_table
from trader.domain.candidates import ReturnBasis
from trader.domain.instruments import CommodityInstrumentSpec, EquitySpec, InstrumentIdentity
from trader.domain.types import ZERO, Direction, EvidenceLevel, ProductType, Side, Track
from trader.risk.sizing import ExposureState, Sizer, SizingRequest

ILLUSTRATIVE_EQUITY_PRICE = Decimal("100")
STOP_WIDTHS = (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"), Decimal("0.025"))
NOTIONALS = (Decimal("1"), Decimal("5"), Decimal("10"))


def _q(value: Decimal, places: str = "0.0001") -> str:
    return f"{value.quantize(Decimal(places))}"


def _illustrative_equity() -> EquitySpec:
    return EquitySpec(
        identity=InstrumentIdentity(
            instrument_id="ILLUSTRATIVE:EQ",
            broker="illustrative",
            broker_asset_id="illustrative",
            symbol="EQ",
            currency="USD",
            product_type=ProductType.ORDINARY_EQUITY,
            track=Track.STOCKS,
        ),
        quantity_step=Decimal("0.000000001"),
        min_quantity=Decimal("0.000000001"),
        min_notional=Decimal("1"),
        price_tick=Decimal("0.01"),
        fractionable=True,
        evidence=EvidenceLevel.REPLAY_SIMULATED,
        as_of=datetime(2026, 11, 7, tzinfo=UTC),
        source="illustrative",
    )


def dollar_table_md(policy: LoadedPolicy) -> str:
    rows = dollar_table(policy.policy)
    labels = {
        "planned_risk_per_trade": "Planned risk per trade (0.25%)",
        "total_planned_risk": "Total planned risk (0.50%)",
        "max_symbol_notional": "Maximum symbol notional (20%)",
        "max_gross_notional": "Maximum gross notional (40%)",
        "cash_reserve": "Cash reserve (greater of 60% and USD 30)",
        "daily_loss_trigger": "Daily loss trigger (1%)",
        "weekly_loss_trigger": "Weekly loss trigger (2.5%)",
        "initial_capital_pause_floor": "Initial-capital pause floor",
    }
    out = ["| Control | USD at 50.00 |", "| --- | --- |"]
    out += [f"| {labels[k]} | {_q(v, '0.001')} |" for k, v in rows.items()]
    out.append("")
    out.append(f"Policy hash: `{policy.policy_hash}`")
    return "\n".join(out)


def fee_table_md(schedules: list[FeeSchedule], budget: Decimal) -> str:
    out = [
        "| Schedule | Evidence | Notional | Entry fee | Exit fee | Round trip | Break-even move | Round trip vs USD 0.125 budget |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for sched in schedules:
        for notional in NOTIONALS:
            if not sched.known:
                out.append(
                    f"| `{sched.schedule_id}` | {sched.config.evidence.value} | {notional} {sched.currency} "
                    "| unknown | unknown | unknown | unknown | NO_TRADE (unknown costs) |"
                )
                continue
            qty = notional / ILLUSTRATIVE_EQUITY_PRICE
            entry = sched.fee(Side.BUY, qty, notional)
            exit_ = sched.fee(Side.SELL, qty, notional)
            rt = entry + exit_
            bps = rt / notional * 10000
            verdict = (
                "exceeds budget" if rt > budget else f"{_q(rt / budget * 100, '0.1')}% of budget"
            )
            out.append(
                f"| `{sched.schedule_id}` | {sched.config.evidence.value} | {notional} {sched.currency} | "
                f"{_q(entry)} | {_q(exit_)} | {_q(rt)} | {_q(bps, '0.1')} bps | {verdict} |"
            )
    return "\n".join(out)


def max_size_table_md(policy: LoadedPolicy, schedules: list[FeeSchedule]) -> str:
    spec = _illustrative_equity()
    out = [
        "| Schedule | Stop width | Largest permitted notional (USD) | Planned loss (USD) | Round-trip fees (USD) | Gross move needed to cover fees + spread (4 bps) | Binding constraint |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    e = policy.policy.initial_trading_capital
    for sched in schedules:
        if not sched.known:
            out.append(
                f"| `{sched.schedule_id}` | any | 0 | - | unknown | - | unknown costs -> NO_TRADE |"
            )
            continue
        for width in STOP_WIDTHS:
            price = ILLUSTRATIVE_EQUITY_PRICE
            sizer = Sizer(
                SizingRequest(
                    policy=policy.policy,
                    spec=spec,
                    direction=Direction.LONG,
                    entry_price=price,
                    stop_price=price * (1 - width),
                    target_price=None,
                    fx_to_usd=Decimal(1),
                    fees=sched,
                    gap_allowance_fraction=policy.policy.gap_allowance_floor_fraction,
                    equity_usd=e,
                    portfolio_spendable_usd=e,
                    account_spendable_usd=e,
                    exposure=ExposureState(),
                    expected_gross_return=Decimal("0.01"),
                    uncertainty_margin=ZERO,
                    basis=ReturnBasis.MID_TO_MID,
                    half_spread_fraction=Decimal("0.0002"),
                )
            )
            steps = sizer.largest_feasible_steps()
            q = steps * spec.quantity_step
            ev = sizer.evaluate(q)
            if q <= 0 or spec.minimum_violations(q, price):
                minimum = sizer.evaluate(Decimal("0.01"))
                out.append(
                    f"| `{sched.schedule_id}` | {(width * 100).quantize(Decimal('0.1'))}% | 0 (minimum USD 1 order needs planned loss "
                    f"{_q(minimum.planned_loss_usd)}) | - | - | - | {', '.join(minimum.failures) or 'minimum'} |"
                )
                continue
            needed = (ev.fees_usd / ev.notional_usd + Decimal("0.0004")) * 100
            binding = sizer._binding_at(steps + 1)
            out.append(
                f"| `{sched.schedule_id}` | {(width * 100).quantize(Decimal('0.1'))}% | {_q(ev.notional_usd)} | {_q(ev.planned_loss_usd)} | "
                f"{_q(ev.fees_usd)} | {_q(needed, '0.01')}% | {binding} |"
            )
    return "\n".join(out)


def crypto_table_md(kraken: FeeSchedule, policy: LoadedPolicy) -> str:
    budget = policy.policy.exposure.risk_per_trade_fraction * policy.policy.initial_trading_capital
    gap = policy.policy.gap_allowance_floor_fraction
    out = [
        "| Liquidity | Notional (EUR, treated 1:1 USD for illustration) | Round-trip fees | Planned loss at 1% stop + 10 bps gap | Within USD 0.125? |",
        "| --- | --- | --- | --- | --- |",
    ]
    from trader.domain.types import Liquidity

    for liq in (Liquidity.TAKER, Liquidity.MAKER):
        for notional in (Decimal("5"), Decimal("10")):
            rt = kraken.fee(Side.BUY, Decimal(1), notional, liq) + kraken.fee(
                Side.SELL, Decimal(1), notional, liq
            )
            pl = notional * Decimal("0.01") + notional * gap + rt
            out.append(
                f"| {liq.value} | {notional} | {_q(rt)} | {_q(pl)} | {'yes' if pl <= budget else 'NO'} |"
            )
    return "\n".join(out)


@dataclass(frozen=True)
class CfdBound:
    instrument: str
    min_notional_usd: Decimal
    price_risk_usd: Decimal
    by_symbol_cap: Decimal
    by_price_risk: Decimal
    by_risk_with_gap: Decimal


def cfd_bounds(
    spec: CommodityInstrumentSpec, reference_price: Decimal, policy: LoadedPolicy
) -> CfdBound:
    x = policy.policy.exposure
    q = spec.min_quantity
    notional = spec.notional_usd(q, reference_price, Decimal(1))
    price_risk = spec.price_risk_usd(
        q, reference_price, reference_price * Decimal("0.99"), Decimal(1)
    )
    gap = notional * policy.policy.gap_allowance_floor_fraction
    return CfdBound(
        instrument=spec.instrument_id,
        min_notional_usd=notional,
        price_risk_usd=price_risk,
        by_symbol_cap=notional / x.max_symbol_exposure_fraction,
        by_price_risk=price_risk / x.risk_per_trade_fraction,
        by_risk_with_gap=(price_risk + gap) / x.risk_per_trade_fraction,
    )


def cfd_table_md(policy: LoadedPolicy, fixtures: Path) -> str:
    specs = load_instruments(fixtures)
    refs = {"CFDFIX:XAUUSD": Decimal("4000"), "CFDFIX:XAGUSD": Decimal("50")}
    out = [
        "| Fixture (hypothetical) | Reference price | Smallest position | Notional | Price risk at 1% stop | Equity needed: symbol cap | Equity needed: risk budget | ... incl. 10 bps gap | At USD 50 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for iid, ref in refs.items():
        spec = specs[iid]
        assert isinstance(spec, CommodityInstrumentSpec)
        b = cfd_bounds(spec, ref, policy)
        lower = max(b.by_symbol_cap, b.by_risk_with_gap)
        verdict = (
            "NO_TRADE" if lower > policy.policy.initial_trading_capital else "check other limits"
        )
        out.append(
            f"| {iid} | {ref} | {spec.min_quantity} {spec.quantity_unit} | {_q(b.min_notional_usd, '0.01')} | "
            f"{_q(b.price_risk_usd, '0.01')} | {_q(b.by_symbol_cap, '0.01')} | {_q(b.by_price_risk, '0.01')} | "
            f"{_q(b.by_risk_with_gap, '0.01')} | {verdict} |"
        )
    return "\n".join(out)


def capability_matrix_md(capabilities_dir: Path) -> str:
    profiles = [load_capabilities(p) for p in sorted(capabilities_dir.glob("*.yaml"))]
    names = sorted({n for p in profiles for n in p.capabilities})
    labels = [f"{p.broker} ({p.environment})" for p in profiles]
    # Two profiles of one broker and environment (e.g. documented and paper-certified Alpaca)
    # are told apart by their profile version.
    labels = [
        label if labels.count(label) == 1 else f"{p.broker} ({p.environment}, {p.profile_version})"
        for label, p in zip(labels, profiles, strict=True)
    ]
    header = "| Capability | " + " | ".join(labels) + " |"
    out = [header, "| --- |" + " --- |" * len(profiles)]

    def cell(value: bool | None, evidence: EvidenceLevel) -> str:
        if value is None:
            return "UNKNOWN"
        return f"{'yes' if value else 'no'} ({evidence.value.lower()})"

    for name in names:
        row = [f"`{name}`"]
        for p in profiles:
            if name in p.capabilities:
                c = p.capabilities[name]
                row.append(cell(c.supported, c.evidence))
            else:
                row.append("-")
        out.append("| " + " | ".join(row) + " |")
    out.append(
        "| account legal type | " + " | ".join(p.account_legal_type.value for p in profiles) + " |"
    )
    out.append("| rule profile | " + " | ".join(p.rule_profile.value for p in profiles) + " |")
    out.append("| cash availability | " + " | ".join(p.cash_availability for p in profiles) + " |")
    out.append(
        "| market data | "
        + " | ".join(
            f"{p.market_data_feed}{' (consolidated)' if p.market_data_consolidated else ''}"
            for p in profiles
        )
        + " |"
    )
    out.append("| fee schedule | " + " | ".join(f"`{p.fee_schedule_id}`" for p in profiles) + " |")
    out.append(
        "| evidence date | " + " | ".join(p.as_of.date().isoformat() for p in profiles) + " |"
    )
    return "\n".join(out)


def generated_sections(repo_root: Path, policy: LoadedPolicy) -> dict[str, str]:
    fees_dir = repo_root / "configs" / "fees"
    equity_schedules = [
        load_fee_schedule(fees_dir / "replay_demo.yaml"),
        load_fee_schedule(fees_dir / "ibkr_us_fractional.yaml"),
        load_fee_schedule(fees_dir / "alpaca_us_equities.yaml"),
        load_fee_schedule(fees_dir / "alpaca_us_equities_v2.yaml"),
    ]
    kraken = load_fee_schedule(fees_dir / "kraken_spot_tier1.yaml")
    budget = policy.policy.exposure.risk_per_trade_fraction * policy.policy.initial_trading_capital
    return {
        "dollar-table": dollar_table_md(policy),
        "fee-table": fee_table_md([*equity_schedules, kraken], budget),
        "max-size-table": max_size_table_md(policy, equity_schedules),
        "crypto-table": crypto_table_md(kraken, policy),
        "cfd-table": cfd_table_md(
            policy, repo_root / "configs" / "instruments" / "commodity_fixtures.yaml"
        ),
        "capability-matrix": capability_matrix_md(repo_root / "configs" / "capabilities"),
    }
