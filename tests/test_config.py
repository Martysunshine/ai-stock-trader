"""Configuration, policy and host-role guards (instructions.md §6.1; risk_and_validation.md §1-2)."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from trader.config.host import HostRole, HostRoleError, check_host_role, current_host_role
from trader.config.policy import LoadedPolicy, PolicyError, build_policy, dollar_table, load_policy
from trader.config.runtime_config import load_config
from trader.config.track_profiles import TrackProfile, load_track_profiles
from trader.domain.types import DomainError, Mode, Track


def _spec_yaml(repo_root: Path) -> dict[str, object]:
    text = (repo_root / "docs" / "specs" / "risk_and_validation.md").read_text(encoding="utf-8")
    block = re.search(r"```yaml\n(.*?)```", text, re.DOTALL)
    assert block is not None
    data = yaml.safe_load(block.group(1))
    assert isinstance(data, dict)
    return data


def test_policy_file_mirrors_the_canonical_specification(repo_root: Path) -> None:
    ours = yaml.safe_load((repo_root / "configs/policy/base.yaml").read_text(encoding="utf-8"))
    assert ours == _spec_yaml(repo_root)


def test_dollar_table_matches_section_2(policy: LoadedPolicy) -> None:
    t = dollar_table(policy.policy)
    assert t == {
        "planned_risk_per_trade": Decimal("0.125"),
        "total_planned_risk": Decimal("0.25"),
        "max_symbol_notional": Decimal("10"),
        "max_gross_notional": Decimal("20"),
        "cash_reserve": Decimal("30"),
        "daily_loss_trigger": Decimal("0.50"),
        "weekly_loss_trigger": Decimal("1.25"),
        "initial_capital_pause_floor": Decimal("45"),
    }


def test_policy_hash_is_stable_and_content_sensitive(repo_root: Path) -> None:
    a = load_policy(repo_root / "configs/policy/base.yaml")
    b = load_policy(repo_root / "configs/policy/base.yaml")
    assert a.policy_hash == b.policy_hash
    raw = yaml.safe_load((repo_root / "configs/policy/base.yaml").read_text(encoding="utf-8"))
    raw["exposure"]["max_open_positions"] = 1
    assert build_policy(raw, source="mutated").policy_hash != a.policy_hash


def test_unquoted_decimal_is_rejected(tmp_path: Path, repo_root: Path) -> None:
    text = (repo_root / "configs/policy/base.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        text.replace('risk_per_trade_fraction: "0.0025"', "risk_per_trade_fraction: 0.0025"),
        "utf-8",
    )
    with pytest.raises(PolicyError, match="unquoted decimal"):
        load_policy(bad)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("instruments", "leverage_enabled"), True, "leverage"),
        (("instruments", "shorting_enabled"), True, "short"),
        (("execution", "allow_blind_submission_retries"), True, "blind"),
        (("loss_limits", "automatic_resume_after_loss_breach"), True, "automatic resume"),
        (("expenses", "automatic_funding_enabled"), True, "funding"),
        (("expenses", "live_loop_llm_api_budget_usd_per_day"), "1.00", "LLM"),
        (("live_enabled",), True, "live_enabled"),
    ],
)
def test_policy_rejects_unsupported_or_unsafe_settings(
    repo_root: Path, path: tuple[str, ...], value: object, message: str
) -> None:
    raw = yaml.safe_load((repo_root / "configs/policy/base.yaml").read_text(encoding="utf-8"))
    node = raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(PolicyError, match=message):
        build_policy(raw, source="test")


def test_default_configuration_is_replay(repo_root: Path) -> None:
    cfg = load_config(repo_root / "configs/replay.yaml", host_role=HostRole.DEV)
    assert cfg.runtime.mode is Mode.REPLAY
    assert cfg.policy.policy.mode is Mode.REPLAY
    assert not cfg.policy.policy.live_enabled


@pytest.mark.parametrize(
    ("role", "mode", "allowed"),
    [
        (HostRole.DEV, Mode.REPLAY, True),
        (HostRole.DEV, Mode.PAPER, True),
        (HostRole.DEV, Mode.LIVE, False),
        (HostRole.PAPER, Mode.LIVE, False),
        (HostRole.LIVE, Mode.PAPER, False),
        (HostRole.LIVE, Mode.REPLAY, False),
        (HostRole.LIVE, Mode.LIVE, True),
    ],
)
def test_host_role_matrix(role: HostRole, mode: Mode, allowed: bool) -> None:
    if allowed:
        check_host_role(role, mode)
    else:
        with pytest.raises(HostRoleError):
            check_host_role(role, mode)


def test_live_host_rejects_paper_config_and_dev_rejects_live(repo_root: Path) -> None:
    with pytest.raises(HostRoleError):
        load_config(repo_root / "configs/paper.yaml", host_role=HostRole.LIVE)
    with pytest.raises(HostRoleError):
        load_config(repo_root / "configs/live.example.yaml", host_role=HostRole.DEV)


def test_host_role_defaults_to_dev_and_rejects_garbage() -> None:
    assert current_host_role({}) is HostRole.DEV
    with pytest.raises(HostRoleError):
        current_host_role({"TRADER_HOST_ROLE": "prod"})


def test_config_rejects_inline_secrets_and_sqlite_outside_replay(
    tmp_path: Path, repo_root: Path
) -> None:
    base = (repo_root / "configs/replay.yaml").read_text(encoding="utf-8")
    secret = tmp_path / "secret.yaml"
    secret.write_text(base + "api_key: abc\n", "utf-8")
    with pytest.raises((DomainError, ValueError)):
        load_config(secret, host_role=HostRole.DEV)
    paper_sqlite = tmp_path / "paper_sqlite.yaml"
    paper_sqlite.write_text(base.replace("mode: replay", "mode: paper"), "utf-8")
    with pytest.raises((DomainError, ValueError), match="SQLite"):
        load_config(paper_sqlite, host_role=HostRole.DEV)


def test_live_enabled_outside_live_mode_is_rejected(tmp_path: Path, repo_root: Path) -> None:
    base = (repo_root / "configs/replay.yaml").read_text(encoding="utf-8")
    f = tmp_path / "x.yaml"
    f.write_text(base.replace("live_enabled: false", "live_enabled: true"), "utf-8")
    with pytest.raises((DomainError, ValueError), match="live_enabled"):
        load_config(f, host_role=HostRole.DEV)


def test_track_profiles_cannot_widen_stocks_or_carry_risk_numbers(repo_root: Path) -> None:
    profiles = load_track_profiles(repo_root / "configs/tracks")
    assert profiles[Track.STOCKS].enabled
    assert not profiles[Track.CRYPTO].enabled
    assert not profiles[Track.COMMODITIES].enabled
    good = yaml.safe_load((repo_root / "configs/tracks/stocks.yaml").read_text(encoding="utf-8"))
    for mutation in (
        {"replay_directions": ["LONG", "SHORT"]},
        {"allowed_product_types": ["ORDINARY_EQUITY", "CFD"]},
        {"decision_bar_minutes": 1},
        {"risk_per_trade_fraction": "0.01"},
    ):
        with pytest.raises((DomainError, ValueError)):
            TrackProfile.model_validate({**good, **mutation})
    crypto = yaml.safe_load((repo_root / "configs/tracks/crypto.yaml").read_text(encoding="utf-8"))
    with pytest.raises((DomainError, ValueError)):
        TrackProfile.model_validate({**crypto, "replay_directions": ["LONG", "SHORT"]})
    commodities = yaml.safe_load(
        (repo_root / "configs/tracks/commodities.yaml").read_text(encoding="utf-8")
    )
    with pytest.raises((DomainError, ValueError)):
        TrackProfile.model_validate({**commodities, "live_directions": ["SHORT"]})
