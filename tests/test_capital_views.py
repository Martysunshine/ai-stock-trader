"""Diagnostic capital views (risk_and_validation.md §1-2 and §5; docs/decisions.md "Daily bars,
overnight holds, exit signals and capital views").

* ``scale_policy`` scales exactly the USD amounts of the policy: every field of
  ``config/policy.py`` and ``configs/policy/base.yaml`` is classified here, so a new field fails
  this test until someone decides whether it is a USD amount.
* Every dollar figure of the policy (risk_and_validation.md §2) scales by capital / USD 50.
* A study with ``diagnostic_capitals`` binds them into its registrations, reports them in their
  own section and produces exactly the same walk-forward, statistics and G2 as without them:
  nothing that selects or gates reads a capital view.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from tests import model_study_fixtures as fx
from tests.daily_fixtures import (
    CALENDAR,
    Plan,
    ScriptedDailyRule,
    daily_specs,
    session_bar,
    sessions,
)
from trader.accounting.fees import load_fee_schedule
from trader.config.policy import (
    USD_AMOUNT_FIELDS,
    PolicyError,
    RiskPolicy,
    dollar_table,
    load_policy,
    scale_policy,
)
from trader.domain.types import Track
from trader.research import study as study_module
from trader.research.engine import EngineConfig, FillAssumptions, InstrumentSetup, ResearchEngine
from trader.research.registry import ExperimentRegistry, RegistryError
from trader.research.report import render_baseline_report
from trader.research.study import (
    build_registration,
    load_study,
    open_study,
    result_digest,
    run_baseline_study,
)

REPO = Path(__file__).resolve().parents[1]
POLICY_FILE = REPO / "configs" / "policy" / "base.yaml"
BASE = load_policy(POLICY_FILE)

#: Every leaf of the policy and what kind of quantity it is. Only "usd" is scaled.
CLASSIFICATION: dict[str, str] = {
    "policy_version": "label",
    "mode": "label",
    "base_currency": "label",
    "initial_trading_capital": "usd",
    "live_enabled": "flag",
    "instruments.ordinary_equities_only": "flag",
    "instruments.long_only": "flag",
    "instruments.fractional_only_if_certified": "flag",
    "instruments.leverage_enabled": "flag",
    "instruments.shorting_enabled": "flag",
    "instruments.extended_hours_enabled": "flag",
    "instruments.minimum_notional_from_broker": "flag",
    "exposure.risk_per_trade_fraction": "fraction",
    "exposure.max_total_planned_risk_fraction": "fraction",
    "exposure.max_gross_exposure_fraction": "fraction",
    "exposure.max_symbol_exposure_fraction": "fraction",
    "exposure.minimum_cash_reserve_fraction": "fraction",
    "exposure.minimum_cash_reserve_absolute": "usd",
    "exposure.max_open_positions": "count",
    "exposure.live_pilot_max_open_positions": "count",
    "exposure.max_pending_entry_orders": "count",
    "exposure.max_new_entries_per_session": "count",
    "loss_limits.daily_loss_fraction": "fraction",
    "loss_limits.weekly_loss_fraction": "fraction",
    "loss_limits.max_high_water_drawdown_fraction": "fraction",
    "loss_limits.initial_capital_pause_floor": "usd",
    "loss_limits.automatic_resume_after_loss_breach": "flag",
    "quality.max_quote_age_seconds": "duration",
    "quality.max_account_snapshot_age_seconds": "duration",
    "quality.max_clock_skew_milliseconds": "duration",
    "quality.max_entry_spread_bps": "bps",
    "quality.gap_allowance_floor_bps": "bps",
    "quality.require_feed_parity": "flag",
    "quality.require_positive_conservative_net_edge": "flag",
    "execution.native_protection_required_for_live": "flag",
    "execution.allow_blind_submission_retries": "flag",
    "execution.unknown_order_blocks_new_entries": "flag",
    "execution.preserve_risk_reducing_exits_when_paused": "flag",
    "execution.funds_check_uses_actual_account_rule_profile": "flag",
    "expenses.paid_data_enabled": "flag",
    "expenses.live_loop_llm_api_budget_usd_per_day": "usd",
    "expenses.automatic_funding_enabled": "flag",
    "expenses.automatic_withdrawals_enabled": "flag",
}


def model_leaves(model: type[BaseModel], prefix: str = "") -> list[str]:
    out: list[str] = []
    for name, info in model.model_fields.items():
        if name == "schema_version":
            continue
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out += model_leaves(annotation, f"{prefix}{name}.")
        else:
            out.append(prefix + name)
    return out


def yaml_leaves(node: Any, prefix: str = "") -> list[str]:
    if isinstance(node, dict):
        return [leaf for k, v in node.items() for leaf in yaml_leaves(v, f"{prefix}{k}.")]
    return [prefix[:-1]]


def leaf(policy: RiskPolicy, path: str) -> Any:
    node: Any = policy
    for key in path.split("."):
        node = getattr(node, key)
    return node


# ----------------------------------------------------------------------------- the helper


def test_every_policy_field_is_classified_and_exactly_the_usd_amounts_are_scaled() -> None:
    fields = model_leaves(RiskPolicy)
    assert sorted(fields) == sorted(CLASSIFICATION), "classify every new policy field here"
    raw = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8"))
    assert sorted(yaml_leaves(raw)) == sorted(CLASSIFICATION)  # the YAML mirrors the model
    usd = {p for p, kind in CLASSIFICATION.items() if kind == "usd"}
    assert usd == {".".join(p) for p in USD_AMOUNT_FIELDS}
    for path in usd:
        value = raw
        for key in path.split("."):
            value = value[key]
        assert isinstance(value, str) and Decimal(value) >= 0  # quoted decimal USD amounts


@pytest.mark.parametrize("capital", ["100", "1000", "10000", "237.5"])
def test_scaling_multiplies_every_dollar_figure_and_nothing_else(capital: str) -> None:
    c = Decimal(capital)
    k = c / BASE.policy.initial_trading_capital
    scaled = scale_policy(BASE, c)
    for path, kind in CLASSIFICATION.items():
        before, after = leaf(BASE.policy, path), leaf(scaled.policy, path)
        assert after == (before * k if kind == "usd" else before), path
    assert scaled.policy.initial_trading_capital == c
    base_table, scaled_table = dollar_table(BASE.policy), dollar_table(scaled.policy)
    assert set(base_table) == set(scaled_table)
    for name, value in base_table.items():
        assert scaled_table[name] == value * k, name
    assert scaled.policy_hash != BASE.policy_hash
    assert "diagnostic capital view" in scaled.source


def test_scaling_to_the_base_capital_changes_no_dollar_figure_and_bad_capitals_fail() -> None:
    same = scale_policy(BASE, Decimal(50))
    assert dollar_table(same.policy) == dollar_table(BASE.policy)
    for bad in (Decimal(0), Decimal(-10)):
        with pytest.raises(PolicyError):
            scale_policy(BASE, bad)


def test_the_engine_sizes_to_the_scaled_policy() -> None:
    days = sessions(date(2019, 1, 2), date(2019, 1, 12))
    p = Decimal(100)
    bars = [session_bar("ARCX:SPY", s, p, p + 1, p - 1, p) for s in days]
    plan = {("ARCX:SPY", days[1].session_date): Plan(stop=Decimal(97), target=Decimal(106))}
    results = {}
    for capital in (Decimal(50), Decimal(10000)):
        policy = scale_policy(BASE, capital).policy
        cfg = EngineConfig(
            policy=policy,
            fees=load_fee_schedule(REPO / "configs" / "fees" / "alpaca_us_equities_v2.yaml"),
            fills=FillAssumptions("base", Decimal(2), Decimal(2), Decimal("0.10")),
            track=Track.STOCKS,
            calendar=CALENDAR,
            start=datetime(2019, 1, 1, tzinfo=UTC),
            end=datetime(2019, 2, 1, tzinfo=UTC),
            loss_windows=("NY_SESSION",),
            session_basis="XNYS",
            initial_capital=capital,
        )
        setups = {
            "ARCX:SPY": InstrumentSetup(spec=daily_specs(["ARCX:SPY"])["ARCX:SPY"], bars=bars)
        }
        (trade,) = ResearchEngine(cfg, ScriptedDailyRule(plan), setups).run().trades
        assert trade.planned_loss_usd <= policy.exposure.risk_per_trade_fraction * capital
        assert trade.entry.quantity * trade.entry.price <= Decimal("0.20") * capital
        results[capital] = trade
    assert results[Decimal(10000)].quantity > 150 * results[Decimal(50)].quantity


# ----------------------------------------------------------------------------- in a study


def _study(root: Path, capitals: tuple[str, ...]) -> Any:
    cfg = load_study(root / fx.BASE_STUDY)
    raw = cfg.model_dump(mode="json")
    raw["trials"] = raw["trials"][-1:]  # T-0005 only: a pipeline check, not evidence
    raw["diagnostic_capitals"] = list(capitals)
    return type(cfg).model_validate(raw)


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    tmp = tmp_path_factory.mktemp("capital_views")
    root = fx.workspace(tmp)
    manifest = fx.synthetic_manifest(root)
    fx.write_base_study(root)
    out: dict[str, Any] = {"root": root, "manifest": manifest}
    for name, capitals in (("plain", ()), ("views", ("500", "10000"))):
        ctx = open_study(root, _study(root, capitals), manifest)
        registry = ExperimentRegistry(tmp / f"{name}.jsonl")
        for trial in ctx.config.trials:
            registry.register(build_registration(ctx, trial), fx.NOW)
        out[name] = {"ctx": ctx, "registry": registry, "result": run_baseline_study(ctx, registry)}
    return out


def _without(result: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(json.dumps(result, sort_keys=True, default=str))
    data.pop("capital_views", None)
    for trial in data["trials"].values():
        trial.pop("registration_hash")  # the capitals are bound into the registration
    return data


def test_capital_views_never_change_selection_statistics_or_g2(runs: dict[str, Any]) -> None:
    plain, views = runs["plain"]["result"], runs["views"]["result"]
    assert "capital_views" not in plain  # absent unless declared: the result shape is unchanged
    assert _without(plain) == _without(views)
    cv = views["capital_views"]
    assert cv["capitals_usd"] == ["500", "10000"] and cv["base_capital_usd"] == "50.00"
    assert "DIAGNOSTIC ONLY" in cv["note"] and "G2" in cv["note"]
    (tid,) = cv["trials"]
    for capital, view in cv["trials"][tid].items():
        assert view["capital_usd"] == capital
        assert view["sessions"] == plain["trials"][tid]["views"]["base"]["sessions"]
        assert view["policy_hash"] == scale_policy(BASE, Decimal(capital)).policy_hash
        assert "mean_session_ci" not in view and "per_trade_ci" not in view  # not evidence


def test_g2_ignores_even_an_absurdly_good_capital_view(
    runs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"note": "fake", "trials": {"T-0005": {"10000": {"net_pnl_usd": 1e9}}}}

    monkeypatch.setattr(study_module, "capital_views", fake)
    ctx, registry = runs["views"]["ctx"], runs["views"]["registry"]
    result = run_baseline_study(ctx, registry)
    assert result["capital_views"]["note"] == "fake"
    plain = runs["plain"]["result"]
    assert result["g2"] == plain["g2"] and result["walk_forward"] == plain["walk_forward"]
    assert result["selection_aware"] == plain["selection_aware"]


def test_capitals_are_bound_into_the_registration(runs: dict[str, Any]) -> None:
    ctx = runs["views"]["ctx"]
    reg = build_registration(ctx, ctx.config.trials[0])
    assert reg.cost_basis["diagnostic_capitals_usd"] == ["500", "10000"]
    with pytest.raises(RegistryError):  # a run with other capitals is a different trial
        run_baseline_study(ctx, runs["plain"]["registry"])


def test_the_report_has_a_capital_section_only_when_views_exist(runs: dict[str, Any]) -> None:
    plain, views = runs["plain"]["result"], runs["views"]["result"]
    text = render_baseline_report(views, generated_at="test", digest=result_digest(views))
    section = text.split("## 14. Capital sensitivity (diagnostic only)")[1]
    assert "Diagnostic only - not evidence" in section
    assert "| T-0005 | 500 |" in section and "| T-0005 | 10000 |" in section
    assert "gate G2 (section 13) use only those" in section
    without = render_baseline_report(plain, generated_at="test", digest=result_digest(plain))
    assert "Capital sensitivity" not in without
    assert text.split("## 14.")[0].rstrip().endswith(without.rstrip().splitlines()[-1])
