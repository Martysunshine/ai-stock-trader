"""The aggressive paper exploration book (ADR-0057): its own config, policy, universe and
database, kept apart from the USD 100 stocks practice sleeve."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from trader.brokers.catalog import load_instruments
from trader.config.host import HostRole
from trader.config.policy import load_policy
from trader.config.runtime_config import RuntimeConfig, load_config
from trader.market.watchlists import load_watchlists

ROOT = Path(__file__).resolve().parents[1]
EXPLORE = ROOT / "configs" / "paper_explore.yaml"
PRACTICE = ROOT / "configs" / "paper.yaml"


def test_the_exploration_book_is_paper_only_and_separate_from_the_practice_sleeve() -> None:
    explore = load_config(EXPLORE, host_role=HostRole.DEV)
    practice = load_config(PRACTICE, host_role=HostRole.DEV)
    runtime, other = explore.runtime, practice.runtime
    assert runtime.mode.value == "paper" and not runtime.live_enabled
    assert runtime.portfolio_id == "pf-explore" != other.portfolio_id
    (account,) = runtime.accounts
    assert account.account_id == "acct-alpaca-paper-explore" != other.accounts[0].account_id
    assert account.accrue_modelled_fees
    assert runtime.database.url_env == "TRADER_EXPLORE_DATABASE_URL"
    assert runtime.database.url_env != other.database.url_env
    # the same paper keys and certified capabilities as the practice sleeve
    assert account.secret_ref == other.accounts[0].secret_ref
    assert account.capability_profile == other.accounts[0].capability_profile


def test_the_policy_is_the_owners_300_with_wider_numbers_and_every_safety_kept() -> None:
    explore = load_config(EXPLORE, host_role=HostRole.DEV)
    assert explore.runtime.policy_file == "policy/paper_explore_v2.yaml"
    policy = explore.policy.policy
    assert (
        policy
        == load_policy(ROOT / "configs/policy/paper_explore_v2.yaml", mode=policy.mode).policy
    )
    assert policy.initial_trading_capital == Decimal(300)
    e, losses = policy.exposure, policy.loss_limits
    assert e.risk_per_trade_fraction == Decimal("0.02")
    assert (e.max_open_positions, e.max_new_entries_per_session) == (5, 30)
    assert e.max_gross_exposure_fraction == 1 and e.minimum_cash_reserve_fraction == 0
    assert e.max_symbol_exposure_fraction == Decimal("0.25")
    # ADR-0064: the loss limits are fault catchers, far above a normal day's loss
    assert losses.daily_loss_fraction == Decimal("0.10")
    assert losses.weekly_loss_fraction == Decimal("0.25")
    assert losses.max_high_water_drawdown_fraction == Decimal("0.50")
    assert losses.initial_capital_pause_floor == Decimal(150)
    assert not losses.automatic_resume_after_loss_breach
    assert policy.quality.max_quote_age_seconds == 15
    assert policy.quality.max_entry_spread_bps == 20
    i = policy.instruments
    assert not i.leverage_enabled and not i.shorting_enabled and i.long_only


def test_v2_changes_only_six_numbers_of_v1() -> None:
    v1 = load_policy(ROOT / "configs/policy/paper_explore_v1.yaml").policy.model_dump()
    v2 = load_policy(ROOT / "configs/policy/paper_explore_v2.yaml").policy.model_dump()
    changed = {
        f"{section}.{key}"
        for section, block in v2.items()
        if isinstance(block, dict)
        for key, value in block.items()
        if v1[section][key] != value
    }
    assert {k: v for k, v in v2.items() if not isinstance(v, dict)} == {
        k: v for k, v in v1.items() if not isinstance(v, dict)
    }
    assert changed == {
        "exposure.max_new_entries_per_session",
        "loss_limits.daily_loss_fraction",
        "loss_limits.weekly_loss_fraction",
        "loss_limits.max_high_water_drawdown_fraction",
        "loss_limits.initial_capital_pause_floor",
        "quality.max_quote_age_seconds",
    }


@pytest.mark.parametrize("mode", ["replay", "shadow"])
def test_a_paper_exploration_policy_is_refused_outside_paper(mode: str) -> None:
    raw = yaml.safe_load(EXPLORE.read_text(encoding="utf-8"))
    raw["mode"] = mode
    with pytest.raises(ValidationError, match="paper practice policy"):
        RuntimeConfig.model_validate(raw)


def test_the_universe_is_20_fractionable_stocks_with_the_practice_files_ids() -> None:
    explore = load_instruments(ROOT / "configs/instruments/stocks_explore.yaml")
    practice = load_instruments(ROOT / "configs/instruments/stocks_paper.yaml")
    assert len(explore) == 20
    assert set(practice) <= set(explore)
    for iid, spec in practice.items():
        assert explore[iid].identity == spec.identity
    for spec in explore.values():
        assert spec.fractionable and spec.tradable
        assert spec.identity.venue_mic in ("XNAS", "XNYS")


def test_every_stock_the_book_trades_has_a_row_on_the_markets_page() -> None:
    explore = load_instruments(ROOT / "configs/instruments/stocks_explore.yaml")
    shown = {i.symbol for i in load_watchlists().group("stocks").instruments}
    assert {spec.identity.symbol for spec in explore.values()} <= shown
