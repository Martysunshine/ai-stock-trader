"""Day trades on hourly bars (ADR-0055): the channel rule with the break and against it, the
fixed flat time in the simulator, and a study that feeds the rule hourly bars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trader.data.manifest import load_manifest
from trader.data.quote_bars import QuoteBar
from trader.domain.types import Direction
from trader.research.cfd_engine import CfdSignal
from trader.research.cfd_study import build_cfd_registration, load_cfd_study, open_cfd_study
from trader.strategies.cfd_rules import (
    CfdHourlyChannel,
    CfdHourlyChannelSpec,
    build_cfd_rule,
    load_cfd_spec,
)

from .cfd_fixtures import MONDAY, ScriptedRule, long, qbar
from .test_cfd_engine import only, run

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "src/trader/strategies/specs"
GOLD = "CAPITAL:GOLD"
TICKS = {GOLD: Decimal("0.01")}


def hour(n: int) -> datetime:
    """The start of hour ``n`` counted from Monday 5 January 2026, 00:00 UTC."""
    return MONDAY + timedelta(hours=n)


def hbar(n: int, o: str, h: str, low: str, c: str, *, half: str = "0.1") -> QuoteBar:
    return qbar(hour(n), o, h, low, c, half=half, minutes=60)


def quiet(first: int, count: int, price: str = "4000") -> list[QuoteBar]:
    """Hourly bars two points around ``price``: a range of 3998 to 4002, true range 4."""
    p = Decimal(price)
    return [hbar(n, str(p), str(p + 2), str(p - 2), str(p)) for n in range(first, first + count)]


def spec(name: str) -> CfdHourlyChannelSpec:
    loaded = load_cfd_spec(SPECS / f"{name}.yaml")
    assert isinstance(loaded, CfdHourlyChannelSpec)
    return loaded


def feed(rule: CfdHourlyChannel, bars: list[QuoteBar]) -> list[CfdSignal]:
    return [s for b in bars if (s := rule.on_bar(GOLD, b)) is not None]


def test_the_four_shipped_rules_are_the_two_sides_for_each_sleeve() -> None:
    cfd_with, cfd_against = spec("cfd_hourly_breakout_v1"), spec("cfd_hourly_fade_v1")
    crypto_with = spec("cfd_crypto_hourly_breakout_v1")
    crypto_against = spec("cfd_crypto_hourly_fade_v1")
    assert (cfd_with.mode, cfd_against.mode) == ("breakout", "fade")
    assert (crypto_with.mode, crypto_against.mode) == ("breakout", "fade")
    for with_, against in ((cfd_with, cfd_against), (crypto_with, crypto_against)):
        same = ("channel_hours", "atr_hours", "stop_atr_multiplier", "flat_hour_utc")
        assert all(getattr(with_, f) == getattr(against, f) for f in same)
        assert (with_.reward_risk, against.reward_risk) == (2, 1)
        assert with_.channel_hours == with_.atr_hours == 24 and with_.stop_atr_multiplier == 2
    # flat before the broker's 22:00 UTC financing charge, on both sleeves
    assert (cfd_with.entry_first_hour_utc, cfd_with.entry_last_hour_utc) == (7, 17)
    assert (cfd_with.flat_hour_utc, crypto_with.flat_hour_utc) == (20, 21)
    assert (crypto_with.entry_first_hour_utc, crypto_with.entry_last_hour_utc) == (0, 17)
    raw = cfd_with.model_dump(mode="python")
    with pytest.raises(ValidationError, match="span from the first entry"):
        CfdHourlyChannelSpec.model_validate({**raw, "max_hold_hours": 24})
    with pytest.raises(ValidationError, match="before the flat hour"):
        CfdHourlyChannelSpec.model_validate({**raw, "entry_last_hour_utc": 20})


def test_a_close_above_the_channel_is_bought_by_the_breakout_and_sold_by_the_fade() -> None:
    # 24 quiet hours (Monday 00:00 to 24:00), then eight more, then the hour ending 09:00 UTC on
    # Tuesday closes at 4010, above the 4002 top of the previous 24 bars
    bars = [*quiet(0, 32), hbar(32, "4000", "4011", "3999", "4010")]
    # mean true range of the last 24 bars: 23 quiet ones of 4 and the break's 12
    volatility = (Decimal(23 * 4) + Decimal(12)) / 24
    stop_distance = 2 * volatility

    (bought,) = feed(CfdHourlyChannel(spec("cfd_hourly_breakout_v1"), TICKS), bars)
    assert bought.direction is Direction.LONG and bought.reference == Decimal("4010")
    assert bought.decided_at == hour(33)
    assert Decimal("4010") - bought.stop == pytest.approx(stop_distance, abs=Decimal("0.01"))
    assert bought.stop <= Decimal("4010") - stop_distance  # rounded away from the close
    assert bought.target - Decimal("4010") == pytest.approx(
        2 * (Decimal("4010") - bought.stop), abs=Decimal("0.01")
    )
    assert bought.deadline == datetime(2026, 1, 6, 20, tzinfo=UTC)
    assert bought.max_hold_minutes == 11 * 60 and bought.target_role == "EXIT"

    (sold,) = feed(CfdHourlyChannel(spec("cfd_hourly_fade_v1"), TICKS), bars)
    assert sold.direction is Direction.SHORT
    assert sold.stop >= Decimal("4010") + stop_distance
    assert Decimal("4010") - sold.target == pytest.approx(
        sold.stop - Decimal("4010"), abs=Decimal("0.01")
    )
    assert sold.deadline == bought.deadline


def test_a_close_below_the_channel_is_the_mirror() -> None:
    bars = [*quiet(0, 32), hbar(32, "4000", "4001", "3989", "3990")]
    (sold,) = feed(CfdHourlyChannel(spec("cfd_hourly_breakout_v1"), TICKS), bars)
    assert sold.direction is Direction.SHORT and sold.stop > Decimal("3990") > sold.target
    (bought,) = feed(CfdHourlyChannel(spec("cfd_hourly_fade_v1"), TICKS), bars)
    assert bought.direction is Direction.LONG and bought.stop < Decimal("3990") < bought.target


def test_no_decision_outside_the_entry_hours_before_24_bars_or_inside_the_channel() -> None:
    rule = CfdHourlyChannel(spec("cfd_hourly_breakout_v1"), TICKS)
    # a break in the hour ending 03:00 UTC on Tuesday: before the first entry hour (07:00)
    assert feed(rule, [*quiet(0, 26), hbar(26, "4000", "4011", "3999", "4010")]) == []
    # a break in the hour ending 19:00 UTC: after the last entry hour (17:00)
    late = CfdHourlyChannel(spec("cfd_hourly_breakout_v1"), TICKS)
    assert feed(late, [*quiet(0, 42), hbar(42, "4000", "4011", "3999", "4010")]) == []
    # a break on the first Monday morning: fewer than 24 earlier bars
    early = CfdHourlyChannel(spec("cfd_hourly_breakout_v1"), TICKS)
    assert feed(early, [*quiet(0, 8), hbar(8, "4000", "4011", "3999", "4010")]) == []
    # a close at the top of the channel is not beyond it
    inside = CfdHourlyChannel(spec("cfd_hourly_breakout_v1"), TICKS)
    assert feed(inside, [*quiet(0, 32), hbar(32, "4000", "4002", "3999", "4002")]) == []
    # a daily bar is never an hourly decision
    daily = CfdHourlyChannel(spec("cfd_hourly_breakout_v1"), TICKS)
    days = [qbar(MONDAY + timedelta(days=n), "4000", "4002", "3998", "4000") for n in range(30)]
    assert (
        feed(daily, [*days, qbar(MONDAY + timedelta(days=30), "4000", "4050", "3999", "4040")])
        == []
    )


def test_the_channel_counts_bars_across_a_weekend() -> None:
    # Friday's last 24 hours, no bars on the weekend, then Monday's 08:00 hour breaks Friday's top
    friday = MONDAY + timedelta(days=4)
    bars = [
        qbar(
            friday - timedelta(hours=3) + timedelta(hours=n),
            "4000",
            "4002",
            "3998",
            "4000",
            half="0.1",
            minutes=60,
        )
        for n in range(24)
    ]
    monday = MONDAY + timedelta(days=7, hours=8)
    bars.append(qbar(monday, "4005", "4012", "4004", "4010", half="0.1", minutes=60))
    (signal,) = feed(CfdHourlyChannel(spec("cfd_hourly_breakout_v1"), TICKS), bars)
    assert signal.direction is Direction.LONG
    assert signal.deadline == datetime(2026, 1, 12, 20, tzinfo=UTC)


def test_build_cfd_rule_returns_the_hourly_rule() -> None:
    rule = build_cfd_rule(spec("cfd_crypto_hourly_fade_v1"), {}, TICKS)
    assert isinstance(rule, CfdHourlyChannel) and rule.strategy_id == "cfd_crypto_hourly_fade"
    assert rule.exit_signal(GOLD, quiet(0, 1)[0], Direction.LONG) is None


# ----------------------------------------------------------------------------- simulator


def test_a_day_trade_is_closed_at_its_flat_time_and_pays_no_financing() -> None:
    # decided on the hour ending 10:00, bought at the 10:00 open, never stopped, flat at 20:00
    bars = quiet(0, 48)
    flat = hour(20)
    rule = ScriptedRule({(GOLD, hour(9)): long("3900", "4200", deadline=flat)})
    trade = only(run({"GOLD": bars}, rule))
    assert trade["entry"]["time"] == "2026-01-05T10:00:00Z"
    assert trade["exit_reason"] == "TIME_EXIT"
    assert trade["exits"][0]["time"] == "2026-01-05T20:00:00Z"
    assert trade["nights_charged"] == 0 and trade["financing_usd"] == 0
    assert trade["holding_minutes"] == 10 * 60

    # the same trade without a flat time is held through the 22:00 UTC charge
    overnight = ScriptedRule({(GOLD, hour(9)): long("3900", "4200", max_hold_minutes=24 * 60)})
    held = only(run({"GOLD": bars}, overnight))
    assert held["exits"][0]["time"] == "2026-01-06T10:00:00Z" and held["nights_charged"] == 1


def test_an_entry_that_would_open_at_or_after_the_flat_time_is_not_taken() -> None:
    # the hour after the decision is missing, so the next open is at the flat time itself
    bars = [b for b in quiet(0, 48) if b.start != hour(10)]
    rule = ScriptedRule({(GOLD, hour(9)): long("3900", "4200", deadline=hour(11))})
    result = run({"GOLD": bars}, rule)
    assert result.trades == ()
    (outcome,) = result.candidates
    assert outcome.status == "REJECTED" and outcome.binding == "open_after_flat_time"


# ----------------------------------------------------------------------------- studies


def test_an_hourly_study_says_so_in_its_registration_and_a_daily_one_reads_as_before() -> None:
    manifest = load_manifest(ROOT / "research/data_manifest.json")
    hourly = load_cfd_study(ROOT / "configs/research/cfd_hourly_channel_v1.yaml")
    crypto = load_cfd_study(ROOT / "configs/research/cfd_crypto_hourly_channel_v1.yaml")
    daily = load_cfd_study(ROOT / "configs/research/cfd_index_dip_v1.yaml")
    assert [t.trial_id for t in hourly.trials] == ["T-0021", "T-0022"]
    assert [t.trial_id for t in crypto.trials] == ["T-0023", "T-0024"]
    assert [i.instrument_id for i in hourly.instruments] == [GOLD, "CAPITAL:US500", "CAPITAL:US100"]
    assert crypto.splits.development_start == datetime(2025, 1, 1, tzinfo=UTC)
    assert hourly.splits.development_start == datetime(2019, 1, 1, tzinfo=UTC)

    ctx = open_cfd_study(ROOT, hourly, manifest)
    registration = build_cfd_registration(ctx, hourly.trials[0])
    assert registration.splits["bars"]["decision_minutes"] == 60
    assert registration.features == (
        "channel(24 hourly bars)",
        "mean_true_range(24 hourly bars)",
    )
    assert registration.family == "CFD_HOURLY_CHANNEL"
    assert registration.parameters["mode"] == "breakout"
    assert len(registration.datasets) == 6  # bid and ask of three instruments

    old = build_cfd_registration(open_cfd_study(ROOT, daily, manifest), daily.trials[0])
    assert old.splits["bars"] == {
        "source_minutes": 60,
        "calendar": "BROKER_CFD",
        "closed_run_max_weekdays": 2,
    }
