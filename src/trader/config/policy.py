"""The single numeric risk policy (risk_and_validation.md §1).

``configs/policy/base.yaml`` mirrors the canonical YAML exactly and is the only place numeric
risk values live. A mode configuration may override only ``mode`` and ``live_enabled``
(ADR-0013). Every risk decision records :attr:`LoadedPolicy.policy_hash`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from trader.domain.base import ConfigModel
from trader.domain.hashing import canonical_json, sha256_hex
from trader.domain.types import BPS, DomainError, Fraction, Mode, NonNegDec, PosDec


class PolicyError(ValueError):
    pass


class InstrumentsPolicy(ConfigModel):
    ordinary_equities_only: bool
    long_only: bool
    fractional_only_if_certified: bool
    leverage_enabled: bool
    shorting_enabled: bool
    extended_hours_enabled: bool
    minimum_notional_from_broker: bool


class ExposurePolicy(ConfigModel):
    risk_per_trade_fraction: Fraction
    max_total_planned_risk_fraction: Fraction
    max_gross_exposure_fraction: Fraction
    max_symbol_exposure_fraction: Fraction
    minimum_cash_reserve_fraction: Fraction
    minimum_cash_reserve_absolute: NonNegDec
    max_open_positions: int = Field(ge=0)
    live_pilot_max_open_positions: int = Field(ge=0)
    max_pending_entry_orders: int = Field(ge=0)
    max_new_entries_per_session: int = Field(ge=0)


class LossLimitsPolicy(ConfigModel):
    daily_loss_fraction: Fraction
    weekly_loss_fraction: Fraction
    max_high_water_drawdown_fraction: Fraction
    initial_capital_pause_floor: NonNegDec
    automatic_resume_after_loss_breach: bool


class QualityPolicy(ConfigModel):
    max_quote_age_seconds: int = Field(gt=0)
    max_account_snapshot_age_seconds: int = Field(gt=0)
    max_clock_skew_milliseconds: int = Field(gt=0)
    max_entry_spread_bps: int = Field(gt=0)
    gap_allowance_floor_bps: int = Field(ge=0)
    require_feed_parity: bool
    require_positive_conservative_net_edge: bool


class ExecutionPolicy(ConfigModel):
    native_protection_required_for_live: bool
    allow_blind_submission_retries: bool
    unknown_order_blocks_new_entries: bool
    preserve_risk_reducing_exits_when_paused: bool
    funds_check_uses_actual_account_rule_profile: bool


class ExpensesPolicy(ConfigModel):
    paid_data_enabled: bool
    live_loop_llm_api_budget_usd_per_day: NonNegDec
    automatic_funding_enabled: bool
    automatic_withdrawals_enabled: bool


class RiskPolicy(ConfigModel):
    policy_version: int = Field(ge=1)
    mode: Mode
    base_currency: Literal["USD"]
    initial_trading_capital: PosDec
    live_enabled: bool
    instruments: InstrumentsPolicy
    exposure: ExposurePolicy
    loss_limits: LossLimitsPolicy
    quality: QualityPolicy
    execution: ExecutionPolicy
    expenses: ExpensesPolicy

    @model_validator(mode="after")
    def _v1_safety_invariants(self) -> RiskPolicy:
        """Reject policies this v1 implementation cannot honour safely.

        These are not alternative numeric values; they are capabilities that do not exist in
        the code (borrowing, shorting equities, automatic funding, blind retries...).
        """
        problems: list[str] = []
        if self.instruments.leverage_enabled:
            problems.append("leverage is not supported in v1")
        if self.instruments.shorting_enabled:
            problems.append("short selling is not supported in v1")
        if not self.instruments.long_only:
            problems.append("v1 stock execution is long only")
        if self.instruments.extended_hours_enabled:
            problems.append("extended-hours execution is not implemented")
        if self.execution.allow_blind_submission_retries:
            problems.append("blind submission retries are forbidden (architecture.md §7)")
        if not self.execution.unknown_order_blocks_new_entries:
            problems.append("unknown orders must block new entries")
        if not self.execution.preserve_risk_reducing_exits_when_paused:
            problems.append("risk-reducing exits must remain available while paused")
        if self.loss_limits.automatic_resume_after_loss_breach:
            problems.append("automatic resume after a loss breach is not permitted")
        if self.expenses.automatic_funding_enabled or self.expenses.automatic_withdrawals_enabled:
            problems.append("the bot has no funding authority")
        if self.expenses.live_loop_llm_api_budget_usd_per_day != 0:
            problems.append("no paid LLM calls inside the trading loop")
        if self.exposure.max_pending_entry_orders != 1:
            problems.append(
                "the storage layer enforces exactly one pending entry per portfolio (ADR-0010)"
            )
        if self.live_enabled and self.mode is not Mode.LIVE:
            problems.append("live_enabled is only valid in live mode")
        if problems:
            raise DomainError("; ".join(problems))
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def gap_allowance_floor_fraction(self) -> Decimal:
        return Decimal(self.quality.gap_allowance_floor_bps) * BPS

    @property
    def max_entry_spread_fraction(self) -> Decimal:
        return Decimal(self.quality.max_entry_spread_bps) * BPS

    def cash_reserve_usd(self, equity: Decimal) -> Decimal:
        return max(
            self.exposure.minimum_cash_reserve_fraction * equity,
            self.exposure.minimum_cash_reserve_absolute,
        )

    def max_open_positions_for(self, mode: Mode) -> int:
        if mode is Mode.LIVE:
            return min(
                self.exposure.max_open_positions, self.exposure.live_pilot_max_open_positions
            )
        return self.exposure.max_open_positions


@dataclass(frozen=True, slots=True)
class LoadedPolicy:
    policy: RiskPolicy
    canonical_json: str
    policy_hash: str
    source: str


def reject_floats(node: Any, path: str = "") -> None:
    if isinstance(node, float):
        raise PolicyError(
            f'unquoted decimal at {path or "<root>"}: quote it (e.g. "0.25") so it is read '
            "exactly instead of as a binary float"
        )
    if isinstance(node, dict):
        for key, value in node.items():
            reject_floats(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            reject_floats(value, f"{path}[{i}]")


def load_yaml_strict(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise PolicyError(f"{path} must contain a mapping")
    reject_floats(data)
    return data


def build_policy(raw: dict[str, Any], *, source: str) -> LoadedPolicy:
    try:
        policy = RiskPolicy.model_validate(raw)
    except ValueError as exc:
        raise PolicyError(f"invalid risk policy ({source}): {exc}") from exc
    canonical = canonical_json(policy.model_dump(mode="python"))
    return LoadedPolicy(
        policy=policy, canonical_json=canonical, policy_hash=sha256_hex(canonical), source=source
    )


def load_policy(
    path: Path, *, mode: Mode | None = None, live_enabled: bool | None = None
) -> LoadedPolicy:
    """Load the base policy and apply the only permitted overlay keys."""
    raw = load_yaml_strict(path)
    if mode is not None:
        raw["mode"] = mode.value
    if live_enabled is not None:
        raw["live_enabled"] = live_enabled
    return build_policy(raw, source=str(path))


#: Every USD amount in the risk policy, as paths into ``RiskPolicy``. Every other field is a
#: fraction of equity, a count, a duration, a basis-point limit, a flag or a label
#: (``tests/test_capital_views.py`` classifies each field of this model and of
#: ``configs/policy/base.yaml`` and fails on a new one that is not classified).
USD_AMOUNT_FIELDS: tuple[tuple[str, ...], ...] = (
    ("initial_trading_capital",),
    ("exposure", "minimum_cash_reserve_absolute"),
    ("loss_limits", "initial_capital_pause_floor"),
    ("expenses", "live_loop_llm_api_budget_usd_per_day"),
)


def scale_policy(loaded: LoadedPolicy, capital: Decimal) -> LoadedPolicy:
    """The policy at another starting capital, for a **diagnostic** research capital view.

    Every USD amount (:data:`USD_AMOUNT_FIELDS`) is multiplied by
    ``capital / initial_trading_capital``; fractions, counts, limits and flags are unchanged.
    Because the per-trade risk, the aggregate risk, the symbol and gross caps, the cash reserve
    fraction and the loss windows are fractions of equity, every dollar figure of
    :func:`dollar_table` scales by the same factor (risk_and_validation.md §2 at another
    capital). The result has its own ``policy_hash``. It is never a trading policy: only the
    owner changes ``configs/policy/base.yaml`` (risk_and_validation.md §1).
    """
    policy = loaded.policy
    if capital <= 0:
        raise PolicyError("a capital view needs a positive capital")
    base = policy.initial_trading_capital
    raw = policy.model_dump(mode="python")
    for path in USD_AMOUNT_FIELDS:
        node = raw
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = node[path[-1]] * capital / base
    return build_policy(
        raw, source=f"{loaded.source} scaled to USD {capital} (diagnostic capital view)"
    )


def dollar_table(policy: RiskPolicy, equity: Decimal | None = None) -> dict[str, Decimal]:
    """risk_and_validation.md §2 dollar interpretation at a given effective equity."""
    e = policy.initial_trading_capital if equity is None else equity
    x = policy.exposure
    lim = policy.loss_limits
    return {
        "planned_risk_per_trade": x.risk_per_trade_fraction * e,
        "total_planned_risk": x.max_total_planned_risk_fraction * e,
        "max_symbol_notional": x.max_symbol_exposure_fraction * e,
        "max_gross_notional": x.max_gross_exposure_fraction * e,
        "cash_reserve": policy.cash_reserve_usd(e),
        "daily_loss_trigger": lim.daily_loss_fraction * e,
        "weekly_loss_trigger": lim.weekly_loss_fraction * e,
        "initial_capital_pause_floor": lim.initial_capital_pause_floor,
    }
