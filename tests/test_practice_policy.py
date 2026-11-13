"""Paper practice policy: the USD 100 stocks sleeve, twelve entries a session (owner decision, ADR-0043)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from trader.config.host import HostRole
from trader.config.policy import dollar_table, load_policy, scale_policy
from trader.config.runtime_config import RuntimeConfig, load_config
from trader.domain.types import Mode

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "configs" / "policy" / "base.yaml"
PRACTICE = ROOT / "configs" / "policy" / "paper_practice_v1.yaml"
#: The fields the owner's decision changes beyond the USD scaling (path -> practice value).
DECLARED: dict[tuple[str, ...], object] = {
    ("exposure", "max_new_entries_per_session"): 12,
}


def _get(tree: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = tree
    for key in path:
        node = node[key]
    return node


def test_the_practice_policy_is_base_scaled_to_usd_100_plus_the_declared_limits() -> None:
    base = load_policy(BASE, mode=Mode.PAPER)
    practice = load_policy(PRACTICE, mode=Mode.PAPER)
    expected = scale_policy(base, Decimal(100)).policy.model_dump(mode="python")
    for path, value in DECLARED.items():
        _get(expected, path[:-1])[path[-1]] = value
    assert practice.policy.model_dump(mode="python") == expected
    assert practice.policy.exposure.max_pending_entry_orders == 1


def test_the_dollar_table_at_usd_100() -> None:
    table = dollar_table(load_policy(PRACTICE, mode=Mode.PAPER).policy)
    assert {k: v.normalize() for k, v in table.items()} == {
        "planned_risk_per_trade": Decimal("0.25"),
        "total_planned_risk": Decimal("0.5"),
        "max_symbol_notional": Decimal("20"),
        "max_gross_notional": Decimal("40"),
        "cash_reserve": Decimal("60"),
        "daily_loss_trigger": Decimal("1"),
        "weekly_loss_trigger": Decimal("2.5"),
        "initial_capital_pause_floor": Decimal("90"),
    }


def test_full_positions_fit_the_gross_cap() -> None:
    x = load_policy(PRACTICE, mode=Mode.PAPER).policy.exposure
    assert x.max_open_positions * x.max_symbol_exposure_fraction <= x.max_gross_exposure_fraction


def test_the_paper_config_uses_the_practice_policy_and_a_fresh_baseline() -> None:
    cfg = load_config(ROOT / "configs" / "paper.yaml", host_role=HostRole.DEV)
    assert cfg.policy.policy.initial_trading_capital == Decimal("100.00")
    assert cfg.runtime.portfolio_id == "pf-practice"
    assert [a.account_id for a in cfg.runtime.accounts] == ["acct-alpaca-paper-practice"]


@pytest.mark.parametrize("mode", ["replay", "shadow", "live"])
def test_a_paper_practice_policy_is_refused_outside_paper(mode: str) -> None:
    raw = yaml.safe_load((ROOT / "configs" / "paper.yaml").read_text(encoding="utf-8"))
    raw["mode"] = mode
    if mode == "live":
        raw["live_account_allowlist"] = [raw["accounts"][0]["account_id"]]
    with pytest.raises(ValidationError, match=r"paper practice policy|shared policy"):
        RuntimeConfig.model_validate(raw)


def test_live_must_use_the_shared_policy() -> None:
    raw = yaml.safe_load((ROOT / "configs" / "paper.yaml").read_text(encoding="utf-8"))
    raw.update(
        mode="live",
        policy_file="policy/trend_sized_v1.yaml",
        live_account_allowlist=[raw["accounts"][0]["account_id"]],
    )
    with pytest.raises(ValidationError, match=r"must use the shared policy"):
        RuntimeConfig.model_validate(raw)
