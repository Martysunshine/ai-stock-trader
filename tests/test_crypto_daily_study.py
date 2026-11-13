"""The daily crypto study ``crypto_daily_v1`` (configs/research/crypto_daily_v1.yaml; ADR-0042).

SYNTHETIC data only: the prices below are a deterministic function of the day index and a
SHA-256 stream, built to exercise the machinery. Nothing here reads or evaluates real market
data, and the real experiment registry is never touched.

* The 24/7 calendar: a session is a UTC day, the month end is the month's last calendar day, and
  the session arithmetic, warm-up bounds and daily features work on it as on XNYS.
* The daily bar shape on the 24/7 calendar: one bar ``[00:00, 24:00)`` UTC of 1440 minutes, and
  the fixed-interval checks the dataset uses agree with the session checks.
* Candidate D on track CRYPTO: the frozen specification differs from the ETF one only where the
  calendar requires it, the rule needs the UTC-day calendar, and decisions come only at UTC
  month ends.
* The study declares one trial (T-0016), every choice fixed before data, with its disclosures.
* End to end: the declared study, with only the holdout moved onto the five-year SYNTHETIC
  fixture, ingests through the Coinbase path from a fake transport, registers in a temporary
  registry, runs and renders a report with daily crypto wording.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.daily_fixtures import CALENDAR as XNYS
from trader.config.policy import load_yaml_strict
from trader.data.calendars import Utc247Calendar
from trader.data.daily_bars import classify_session_bar, expected_session_opens
from trader.data.history import HistoryBar, to_domain_bars
from trader.data.ingest import IngestContext, ingest, load_bars
from trader.data.manifest import HistoryDataset, load_manifest
from trader.data.quality import expected_starts_24_7
from trader.data.sources.public_crypto import fetch_coinbase_candles
from trader.domain.hashing import content_hash
from trader.domain.market import Bar
from trader.domain.types import Track
from trader.features.daily import (
    DailyFeatures,
    DailyWindows,
    divide,
    is_month_end,
    max_sessions_per_month,
    next_session_date,
)
from trader.research.engine import Trade
from trader.research.registry import ExperimentRegistry
from trader.research.report import render_baseline_report
from trader.research.splits import folds
from trader.research.study import (
    LoadedData,
    StudyConfig,
    StudyContext,
    build_registration,
    deployment_windows,
    load_development_data,
    load_study,
    open_study,
    resolve_rule,
    result_digest,
    run_baseline_study,
    run_trial,
    warmup_start,
)
from trader.strategies import registry
from trader.strategies.trend_timing import (
    CRYPTO_FEATURES,
    EXIT_BELOW_SMA,
    EXIT_SMA_UNAVAILABLE,
    FEATURES,
    TrendTiming,
    TrendTimingSpec,
)

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO / "configs" / "research" / "crypto_daily_v1.yaml"
MANIFEST = REPO / "research" / "data_manifest.json"
DATASET = "coinbase_btc_eth_usd_daily_v1"
SPEC_FILE = "src/trader/strategies/specs/candidate_d_crypto_monthly_v1.yaml"
ETF_SPEC_FILE = "src/trader/strategies/specs/candidate_d_etf_monthly_v1.yaml"
#: Pinned: the frozen specification may only change as a new, separately registered trial.
SPEC_HASH = "095cd50e277e21ca82c923a4648eced8191789cfe006991d8dc2782d2af385bc"
UTC_DAYS = Utc247Calendar()
NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
BTC, ETH = "KRAKEN:BTC/USD", "KRAKEN:ETH/USD"
PRICE_IDS = {"COINBASE:BTC/USD": "BTC-USD", "COINBASE:ETH/USD": "ETH-USD"}
ALLOWED_EXITS = {
    f"SIGNAL:{EXIT_BELOW_SMA}",
    f"SIGNAL:{EXIT_SMA_UNAVAILABLE}",
    "STOP",
    "STOP_GAP",
    "TARGET",
    "TIME_EXIT",
    "END_OF_DATA",
}


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def midnight(day: date) -> datetime:
    return _utc(day.year, day.month, day.day)


# ----------------------------------------------------------------------------- fixture

FIRST, END = date(2016, 1, 1), date(2021, 1, 1)  # five years of UTC days, END exclusive
ETH_FIRST = date(2016, 5, 18)  # as on Coinbase: ETH-USD's candles start that week
#: Days without a candle: two in ETH's first week (as on Coinbase) and one later BTC day.
MISSING = (
    ("ETH-USD", date(2016, 5, 21)),
    ("ETH-USD", date(2016, 5, 22)),
    ("BTC-USD", date(2019, 6, 12)),
)
#: (first day index, drift per day, noise amplitude): bull, bear, recovery, sideways, bull, bear,
#: bull. Invented; prices stay low enough for Kraken's minimum orders at USD 50.
PHASES = (
    (0, Decimal("0.0025"), Decimal("0.012")),
    (500, Decimal("-0.0030"), Decimal("0.015")),
    (760, Decimal("0.0030"), Decimal("0.012")),
    (1000, Decimal("0"), Decimal("0.015")),
    (1250, Decimal("0.0020"), Decimal("0.012")),
    (1500, Decimal("-0.0025"), Decimal("0.015")),
    (1650, Decimal("0.0020"), Decimal("0.012")),
)
CENT = Decimal("0.01")


def _unit(seed: str, i: int) -> Decimal:
    """A deterministic value in [-1, 1) from SHA-256 (no random module)."""
    digest = hashlib.sha256(f"{seed}|{i}".encode()).digest()
    return Decimal(int.from_bytes(digest[:8], "big")) / Decimal(2**63) - 1


def _phase(k: int) -> tuple[Decimal, Decimal]:
    drift, amplitude = PHASES[0][1], PHASES[0][2]
    for start, d, a in PHASES:
        if k >= start:
            drift, amplitude = d, a
    return drift, amplitude


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def utc_days(first: date = FIRST, end: date = END) -> list[date]:
    return [first + timedelta(days=i) for i in range((end - first).days)]


def history(symbol: str) -> list[HistoryBar]:
    """SYNTHETIC UTC-day candles for ``BTC-USD`` or ``ETH-USD`` (stored-history shape)."""
    eth = symbol == "ETH-USD"
    price = Decimal(10) if eth else Decimal(1000)
    beta, scale = (Decimal("1.3"), Decimal("1.4")) if eth else (Decimal(1), Decimal(1))
    skip = {d for s, d in MISSING if s == symbol}
    out: list[HistoryBar] = []
    for k, day in enumerate(utc_days()):
        if eth and day < ETH_FIRST:
            continue
        drift, amplitude = _phase(k)
        r = beta * drift + scale * amplitude * _unit(symbol + ":r", k)
        o = _q(price * (1 + Decimal("0.0005") * _unit(symbol + ":g", k)))
        c = _q(price * (1 + r))
        h = _q(max(o, c) * (1 + Decimal("0.006") * abs(_unit(symbol + ":h", k))))
        low = _q(min(o, c) * (1 - Decimal("0.006") * abs(_unit(symbol + ":l", k))))
        price = c
        if day in skip:
            continue
        volume = Decimal(5000) + _q(Decimal(500) * _unit(symbol + ":v", k))
        out.append(
            HistoryBar(
                start=midnight(day), minutes=1440, open=o, high=h, low=low, close=c, volume=volume
            )
        )
    return out


def domain_bars(symbol: str, instrument_id: str) -> list[Bar]:
    return to_domain_bars(
        history(symbol),
        instrument_id=instrument_id,
        feed="synthetic-utc-daily",
        finalization_delay=timedelta(seconds=1),
        require_source_vwap=False,
    )


class FakeCoinbase:
    """Serves the SYNTHETIC candles like Coinbase Exchange: ``[time, low, high, open, close,
    volume]`` rows, newest first, for buckets with ``start <= time <= end`` (inclusive)."""

    def __init__(self) -> None:
        self.rows: dict[str, list[list[Any]]] = {
            symbol: [
                [int(b.start.timestamp()), b.low, b.high, b.open, b.close, b.volume]
                for b in history(symbol)
            ]
            for symbol in PRICE_IDS.values()
        }
        self.calls = 0

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.calls += 1
        product = url.split("/products/")[1].split("/")[0]
        if url.endswith("/candles"):
            p = dict(params or {})
            assert p["granularity"] == "86400"
            lo = datetime.fromisoformat(p["start"].replace("Z", "+00:00")).timestamp()
            hi = datetime.fromisoformat(p["end"].replace("Z", "+00:00")).timestamp()
            rows = [r for r in self.rows[product] if lo <= r[0] <= hi]
            return sorted(rows, key=lambda r: -int(r[0]))
        base = product.split("-")[0]
        return {
            "id": product,
            "base_currency": base,
            "quote_currency": "USD",
            "quote_increment": "0.01",
            "base_increment": "0.00000001",
            "min_market_funds": "1",
            "status": "online",
            "trading_disabled": False,
        }


# ----------------------------------------------------------------------------- 24/7 calendar


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2016, 1, 31), True),
        (date(2016, 1, 30), False),
        (date(2016, 2, 29), True),  # leap year
        (date(2016, 2, 28), False),
        (date(2017, 2, 28), True),
        (date(2016, 4, 30), True),  # a Saturday: still the month's last UTC day
        (date(2016, 4, 29), False),  # ... although it is XNYS's month end
        (date(2016, 12, 31), True),
        (date(2020, 2, 29), True),
        (date(2023, 2, 28), True),
    ],
)
def test_the_utc_month_end_is_the_last_calendar_day(day: date, expected: bool) -> None:
    assert is_month_end(UTC_DAYS, day) is expected


def test_xnys_month_ends_are_unchanged_by_the_utc_calendar() -> None:
    assert (
        is_month_end(XNYS, date(2016, 4, 29)) and XNYS.session_for_date(date(2016, 4, 30)) is None
    )
    assert next_session_date(XNYS, date(2016, 4, 29)) == date(2016, 5, 2)
    assert next_session_date(UTC_DAYS, date(2016, 4, 29)) == date(2016, 4, 30)


def test_the_utc_calendar_counts_every_day_as_a_session() -> None:
    day = date(2016, 2, 28)
    session = UTC_DAYS.session_for_date(day)
    assert (session.open, session.close, session.minutes) == (
        _utc(2016, 2, 28),
        _utc(2016, 2, 29),
        1440,
    )
    assert UTC_DAYS.session_on(day) == session == UTC_DAYS.session_at(_utc(2016, 2, 28, 13))
    assert UTC_DAYS.is_session_day(date(2016, 5, 1))  # a Sunday
    assert UTC_DAYS.next_session(day).session_date == date(2016, 2, 29)
    assert UTC_DAYS.sessions_after(day).session_date == date(2016, 2, 29)
    assert UTC_DAYS.sessions_after(day, 2).session_date == date(2016, 3, 1)
    assert UTC_DAYS.session_date_of(_utc(2016, 2, 28, 23, 59)) == day
    assert UTC_DAYS.session_opening_at(_utc(2016, 2, 28)) == session
    assert UTC_DAYS.session_opening_at(_utc(2016, 2, 28, 1)) is None
    # Opens strictly before ts: a day opening exactly at ts does not count.
    assert UTC_DAYS.session_open_before(_utc(2017, 8, 1), 1) == _utc(2017, 7, 31)
    assert UTC_DAYS.session_open_before(_utc(2017, 8, 1), 365) == _utc(2016, 8, 1)
    assert UTC_DAYS.session_open_before(_utc(2017, 8, 1, 6), 1) == _utc(2017, 8, 1)
    for bad in (
        lambda: UTC_DAYS.sessions_after(day, 0),
        lambda: UTC_DAYS.session_open_before(_utc(2017, 8, 1), 0),
    ):
        with pytest.raises(ValueError):
            bad()


def test_warmup_bounds_follow_the_calendar() -> None:
    windows = DailyWindows(atr_period=20, month_end_sma_months=10)
    assert (max_sessions_per_month(XNYS), max_sessions_per_month(UTC_DAYS)) == (23, 31)
    assert windows.warmup_sessions == windows.warmup_sessions_on(XNYS) == 230  # unchanged
    assert windows.warmup_sessions_on(UTC_DAYS) == 310
    assert DailyWindows(sma_periods=(200,)).warmup_sessions_on(UTC_DAYS) == 200
    # Ten month ends are ready within the bound from every starting day of a year.
    bars = domain_bars("BTC-USD", BTC)
    for offset in range(0, 366, 7):
        f = DailyFeatures(windows, UTC_DAYS)
        for i, bar in enumerate(bars[offset:]):
            f.update(bar)
            if f.ready:
                assert i + 1 <= windows.warmup_sessions_on(UTC_DAYS)
                break
        else:  # pragma: no cover - the fixture is long enough
            raise AssertionError("never warm")


def test_daily_features_on_utc_days_use_last_day_closes_and_reset_on_a_missing_day() -> None:
    windows = DailyWindows(atr_period=20, month_end_sma_months=10)
    bars = domain_bars("ETH-USD", ETH)  # misses 21 and 22 May 2016
    f = DailyFeatures(windows, UTC_DAYS)
    month_closes: list[Decimal] = []
    for bar in bars:
        day = bar.start.date()
        f.update(bar)
        if day == date(2016, 5, 23):
            assert f.gaps == 1 and f.count == 1  # the missing days reset the state
            month_closes.clear()
        if (day + timedelta(days=1)).month != day.month:
            month_closes.append(bar.close)
            assert f.is_month_end
            if len(month_closes) >= 10:
                assert f.ready
                assert f.month_end_sma() == divide(sum(month_closes[-10:], Decimal(0)), Decimal(10))
        else:
            assert not f.is_month_end
        if day == date(2017, 2, 27):
            assert not f.ready  # nine month ends since the reset: May 2016 .. January 2017
        if day == date(2017, 2, 28):
            assert f.ready  # the tenth
    assert f.gaps == 1  # weekends are sessions, never gaps


# ----------------------------------------------------------------------------- bar shape


def test_a_utc_day_bar_is_one_whole_day_and_the_fixed_grid_equals_the_session_grid() -> None:
    start, end = _utc(2016, 1, 1), _utc(2016, 3, 1)
    opens, by_day = expected_session_opens(UTC_DAYS, start, end)
    assert opens == expected_starts_24_7(start, end, 1440)
    assert len(opens) == 60 and by_day["2016-02-29"] == [_utc(2016, 2, 29)]
    ok = HistoryBar(
        start=_utc(2016, 2, 29),
        minutes=1440,
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(1),
        close=Decimal(1),
        volume=Decimal(1),
    )
    assert classify_session_bar(ok, UTC_DAYS) == "OK"
    late = replace(ok, start=_utc(2016, 2, 29, 1))
    short = replace(ok, minutes=60)
    assert (
        classify_session_bar(late, UTC_DAYS)
        == classify_session_bar(short, UTC_DAYS)
        == "MISALIGNED"
    )
    (bar,) = to_domain_bars(
        [ok],
        instrument_id=BTC,
        feed="f",
        finalization_delay=timedelta(seconds=1),
        require_source_vwap=False,
    )
    assert (bar.start, bar.end, bar.minutes) == (_utc(2016, 2, 29), _utc(2016, 3, 1), 1440)
    assert bar.available_time == _utc(2016, 3, 1) + timedelta(seconds=1)


def test_coinbase_daily_candles_are_fetched_at_granularity_86400_without_gaps_or_overlaps() -> None:
    transport = FakeCoinbase()
    bars = fetch_coinbase_candles(
        transport, "BTC-USD", minutes=1440, start=_utc(2016, 1, 1), end=_utc(2018, 1, 1)
    )
    starts = sorted(b.start for b in bars)
    assert starts == [midnight(d) for d in utc_days(FIRST, date(2018, 1, 1))]
    assert all(b.minutes == 1440 for b in bars)
    assert transport.calls == 3  # 731 days in pages of 300 buckets


# ----------------------------------------------------------------------------- specification


def test_the_crypto_spec_is_the_etf_rule_on_the_utc_day_calendar() -> None:
    spec = registry.load_spec(REPO / SPEC_FILE)
    etf = registry.load_spec(REPO / ETF_SPEC_FILE)
    assert isinstance(spec, TrendTimingSpec) and isinstance(etf, TrendTimingSpec)
    assert spec.spec_hash == SPEC_HASH == content_hash(spec.model_dump(mode="python"))
    assert spec.track is Track.CRYPTO and spec.universe == (BTC, ETH)
    # Only the identity, the universe and the time exit differ (260 XNYS sessions and 365 UTC
    # days are both one year); every other parameter is the ETF rule's.
    differ = {k for k, v in spec.model_dump().items() if etf.model_dump()[k] != v}
    assert differ == {"strategy_id", "track", "universe", "max_hold_sessions"}
    assert (spec.max_hold_sessions, etf.max_hold_sessions) == (365, 260)
    assert spec.warmup_sessions_on(UTC_DAYS) == 310
    assert spec.max_hold_minutes_on(UTC_DAYS) == 365 * 1440
    assert etf.max_hold_minutes_on(XNYS) == etf.candidate_max_hold_minutes
    assert registry.spec_features(spec) == CRYPTO_FEATURES
    assert registry.spec_features(etf) == FEATURES == registry.rule_family("CANDIDATE_D").features


def test_the_calendar_must_match_the_track_and_crypto_ids_name_venue_and_pair() -> None:
    spec = registry.load_spec(REPO / SPEC_FILE)
    with pytest.raises(ValueError, match="UTC-day calendar"):
        TrendTiming(spec, XNYS)
    assert isinstance(registry.build(spec, UTC_DAYS, {}), TrendTiming)
    for bad in (["ARCX:SPY"], ["BTC/USD"], [BTC, BTC]):
        with pytest.raises(ValidationError):
            registry.load_spec(REPO / SPEC_FILE, {"universe": bad})
    with pytest.raises(ValidationError):  # a stock specification still needs MIC ids
        registry.load_spec(REPO / ETF_SPEC_FILE, {"universe": [BTC]})


def test_decisions_come_only_at_utc_month_ends_and_match_a_batch_recomputation() -> None:
    spec = registry.load_spec(REPO / SPEC_FILE)
    assert isinstance(spec, TrendTimingSpec)
    rule = TrendTiming(spec, UTC_DAYS, {BTC: Decimal("0.1")})
    bars = domain_bars("BTC-USD", BTC)
    month_closes: list[Decimal] = []
    seen = 0
    for bar in bars:
        day = bar.start.date()
        candidate = rule.on_bar(bar, bar.available_time)
        reason = rule.exit_signal(BTC, bar)
        if day == date(2019, 6, 13):  # the day after the missing one: the state restarts
            month_closes.clear()
        last_day = (day + timedelta(days=1)).month != day.month
        if not last_day:
            assert candidate is None and reason is None
            continue
        month_closes.append(bar.close)
        if len(month_closes) < 10:
            assert candidate is None and reason == EXIT_SMA_UNAVAILABLE
            continue
        sma = divide(sum(month_closes[-10:], Decimal(0)), Decimal(10))
        assert (candidate is not None) is (bar.close > sma)
        assert reason == (EXIT_BELOW_SMA if bar.close < sma else None)
        if candidate is not None:
            seen += 1
            next_open = midnight(day + timedelta(days=1))
            assert candidate.expires_at == next_open + timedelta(seconds=120)
            assert candidate.max_hold_minutes == 365 * 1440
            assert candidate.track is Track.CRYPTO and candidate.stop_price < bar.close
    assert seen > 5


# ----------------------------------------------------------------------------- the study


def test_the_study_declares_one_trend_trial_with_every_choice_fixed() -> None:
    cfg = load_study(STUDY)
    manifest = load_manifest(MANIFEST)
    ds = manifest.dataset(DATASET)
    assert (cfg.study_id, cfg.track, cfg.session_basis) == (
        "crypto_daily_v1",
        Track.CRYPTO,
        "UTC_DAY",
    )
    assert cfg.price_dataset == DATASET and cfg.annualisation_sessions == 365
    assert cfg.loss_windows == ("UTC_DAY", "UTC_WEEK")
    assert [(m.price_instrument_id, m.execution_instrument_id) for m in cfg.instruments] == [
        ("COINBASE:BTC/USD", BTC),
        ("COINBASE:ETH/USD", ETH),
    ]
    assert [i.instrument_id for i in ds.request.instruments] == list(PRICE_IDS)
    assert cfg.instrument_specs == "configs/instruments/kraken_spot_usd.yaml"
    assert cfg.fee_schedule == "configs/fees/kraken_spot_tier1_usd.yaml"
    assert cfg.policy == "configs/policy/trend_sized_v1.yaml"  # ADR-0041 variant, ADR-0042
    assert (cfg.fills.half_spread_bps, cfg.fills.slippage_bps) == (Decimal(1), Decimal(2))
    assert (cfg.stress.multiplier, cfg.stress.missed_entry_percent) == (Decimal(2), 5)
    assert cfg.diagnostic_capitals == (Decimal(1000),)
    assert (cfg.bootstrap.replicates, cfg.bootstrap.mean_block_sessions) == (2000, Decimal(30))
    assert [t.trial_id for t in cfg.trials] == ["T-0016"]
    (trial,) = cfg.trials
    assert trial.spec_file == SPEC_FILE and trial.overrides == {} and trial.instruments is None
    ctx = open_study(REPO, cfg, manifest)
    assert ctx.fees.known and isinstance(ctx.calendar, Utc247Calendar)
    spec: object = resolve_rule(ctx, trial)
    assert isinstance(spec, TrendTimingSpec) and spec.spec_hash == SPEC_HASH
    # The warm-up covers the rule on UTC days and lies inside both pairs' contiguous candles.
    assert cfg.warmup_sessions == 365 >= spec.warmup_sessions_on(UTC_DAYS)
    plan = cfg.splits
    first = warmup_start(ctx, plan.development_start)
    assert first == _utc(2016, 8, 1) == UTC_DAYS.session_open_before(plan.development_start, 365)
    for record in ds.files:
        assert record.first_start is not None and first >= record.first_start
        for run in record.quality["missing_ranges_first"]:
            stamp, count = run.split(" +")
            gap_end = datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(
                days=int(count.split()[0])
            )
            assert gap_end <= first, run
    # Whole quarters up to the locked final test, which ends where the dataset ends.
    assert (plan.development_start, plan.holdout_start) == (_utc(2017, 8, 1), _utc(2026, 8, 1))
    assert plan.holdout_end == ds.request.end
    windows = deployment_windows(plan)
    assert len(windows) == 36 and windows[-1][1] == plan.holdout_start
    assert len(folds(plan)) == 31 and folds(plan)[0].test[0] == _utc(2018, 11, 1)
    assert plan.embargo_minutes == 0
    text = " ".join((cfg.universe_selection, cfg.horizon, cfg.entry_exit_timing)).lower()
    for phrase in (
        "sample size",
        "at least 100",
        "minimum order",
        "risk_limit",
        "usd 1000",
        "end_of_data",
        "instrument-id order",
        "disaster stop",
        "not in the published rule",
        "coinbase",
        "kraken",
        "basis",
        "next utc day's open",
    ):
        assert phrase in text, phrase


def test_the_manifest_records_the_daily_dataset_as_utc_day_fixed_intervals() -> None:
    ds = load_manifest(MANIFEST).dataset(DATASET)
    req = ds.request
    assert (ds.source, ds.feed, ds.kind) == (
        "coinbase_exchange_public",
        "COINBASE_EXCHANGE_CANDLES",
        "HISTORICAL_BARS",
    )
    assert (req.bar_minutes, req.calendar, req.timeframe, req.is_daily) == (
        1440,
        "UTC_24_7",
        None,
        False,
    )
    assert (req.start, req.end) == (_utc(2016, 1, 1), _utc(2026, 11, 1))
    assert ds.status == "ACQUIRED" and ds.restorable
    by_id = {f.instrument_id: f for f in ds.files}
    assert by_id["COINBASE:BTC/USD"].quality["missing_intervals"] == 0
    assert by_id["COINBASE:ETH/USD"].first_start == _utc(2016, 5, 18)


def test_the_study_cannot_register_on_a_dataset_that_is_not_acquired() -> None:
    manifest = load_manifest(MANIFEST)
    pending = manifest.dataset(DATASET).model_copy(update={"status": "REQUESTED", "files": ()})
    ctx = open_study(REPO, load_study(STUDY), manifest.replace(pending))
    with pytest.raises(ValueError, match="acquire the data before registering"):
        build_registration(ctx, ctx.config.trials[0])


# ----------------------------------------------------------------------------- end to end


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(REPO / "configs", root / "configs")
    shutil.copytree(
        REPO / "src" / "trader" / "strategies" / "specs",
        root / "src" / "trader" / "strategies" / "specs",
    )
    return root


def _synthetic_crypto_study() -> StudyConfig:
    """``crypto_daily_v1`` as declared, with only the holdout moved onto the five-year SYNTHETIC
    fixture (2016-2020) and fewer bootstrap replicates (a pipeline check, not evidence). The
    development start, 2017-08-01, and the 365-day warm-up are the declared ones."""
    raw: dict[str, Any] = load_yaml_strict(STUDY)
    raw["splits"] = {
        **raw["splits"],
        "holdout_start": "2020-08-01T00:00:00Z",
        "holdout_end": "2021-01-01T00:00:00Z",
    }
    raw["bootstrap"] = {**raw["bootstrap"], "replicates": 200}
    return StudyConfig.model_validate(raw)


def _ingest(root: Path) -> HistoryDataset:
    entry = load_manifest(MANIFEST).dataset(DATASET)
    request = entry.request.model_copy(update={"start": midnight(FIRST), "end": midnight(END)})
    return ingest(
        entry.model_copy(update={"request": request}),
        IngestContext(repo_root=root, transport=FakeCoinbase(), now=NOW),
    )


def _trades(ctx: StudyContext, data: LoadedData) -> list[Trade]:
    trades: list[Trade] = []
    for start, end in deployment_windows(ctx.config.splits):
        trades += run_trial(ctx, ctx.config.trials[0], data, "base", start=start, end=end).trades
    return trades


@pytest.mark.slow
def test_the_crypto_study_runs_end_to_end_on_the_synthetic_utc_day_fixture(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ds = _ingest(root)
    assert ds.status == "ACQUIRED", ds.blocker
    by_id = {f.instrument_id: f for f in ds.files}
    btc, eth = by_id["COINBASE:BTC/USD"], by_id["COINBASE:ETH/USD"]
    assert btc.quality["missing_ranges_first"] == ["2019-06-12T00:00:00Z +1 bars"]
    assert eth.quality["missing_ranges_first"] == [
        "2016-01-01T00:00:00Z +138 bars",
        "2016-05-21T00:00:00Z +2 bars",
    ]
    assert eth.first_start == midnight(ETH_FIRST)
    for iid in PRICE_IDS:
        stored = load_bars(ds, iid, root)
        assert all(classify_session_bar(b, UTC_DAYS) == "OK" for b in stored)
    manifest = load_manifest(MANIFEST).replace(ds)

    cfg = _synthetic_crypto_study()
    ctx = open_study(root, cfg, manifest)
    reg = build_registration(ctx, cfg.trials[0])
    assert (reg.trial_id, reg.family, reg.track) == ("T-0016", "CANDIDATE_D", Track.CRYPTO)
    assert reg.universe == (BTC, ETH) and reg.features == CRYPTO_FEATURES
    assert reg.rule["spec_hash"] == SPEC_HASH and reg.feature_version == "daily-v1"
    assert reg.splits["warmup_sessions"] == 365
    assert reg.cost_basis["diagnostic_capitals_usd"] == ["1000"]
    assert reg.cost_basis["fee_schedule"] == "kraken-pro-spot-tier1-usd-2026-11-06"
    assert [d["content_sha256"] for d in reg.datasets] == [btc.content_sha256, eth.content_sha256]
    registry_ = ExperimentRegistry(tmp_path / "registry.jsonl")
    assert registry_.register(reg, NOW)

    # Engine timing on UTC days: entries fill at the open of the UTC day after a month end,
    # signal exits too; nothing outlives its window; BTC never enters while its features
    # rebuild after the missing day (2019-06-12): the tenth month end after it is 2020-03-31, so
    # the first BTC entry can come at the open of 2020-04-01 and none before.
    data = load_development_data(ctx, uses_source_vwap=False, unlocked=False)
    trades = _trades(ctx, data)
    assert {t.instrument_id for t in trades} == {BTC, ETH}
    btc_entries = sorted(t.entry.time.date() for t in trades if t.instrument_id == BTC)
    assert date(2020, 4, 1) in btc_entries
    for t in trades:
        assert t.entry.time.day == 1
        assert t.entry.time - midnight(t.entry.time.date()) < timedelta(seconds=2)
        bar = next(
            b for b in data.setups[t.instrument_id].bars if b.start == midnight(t.entry.time.date())
        )
        assert t.entry.mid_reference == bar.open
        reason = t.exits[-1].reason
        assert reason in ALLOWED_EXITS and reason != "TIME_EXIT"
        if reason.startswith("SIGNAL:"):
            assert t.exits[-1].time.day == 1
        if t.instrument_id == BTC:
            assert not (date(2019, 6, 12) <= t.entry.time.date() < date(2020, 4, 1))

    result = run_baseline_study(ctx, registry_)
    assert result["track"] == "CRYPTO" and result["warmup_sessions"] == 365
    assert result["fee_schedule"] == "kraken-pro-spot-tier1-usd-2026-11-06"
    trial = result["trials"]["T-0016"]
    assert trial["timing"] == {
        "holding": "OVERNIGHT",
        "max_hold_sessions": "365",
        "target_role": "EXIT",
        "entry_limit_offset_bps": "100",
    }
    base = trial["views"]["base"]
    assert base["trades"] == len(trades) > 0
    assert base["fees_usd"] > 0 and trial["views"]["diagnostic"]["fees_usd"] == 0
    assert len(base["window_nets_usd"]) == 12
    assert set(base["exit_reasons"]) <= ALLOWED_EXITS
    assert result["capital_views"]["capitals_usd"] == ["1000"]
    assert len(result["walk_forward"]["folds"]) == 7
    assert result["g2"]["status"] == "NOT_PASSED"
    assert not result["g2"]["checks"]["oos_trades_at_least_100"]
    assert all("unavailable" in str(row.get("status", "")) for row in result["basis"])

    report = render_baseline_report(result, generated_at="test", digest=result_digest(result))
    assert "Evidence class: BACKTEST" in report and "G2 status: NOT_PASSED" in report
    section3 = report.split("## 3. Execution and cost assumptions")[1].split("## 4.")[0]
    assert "Entries (daily bars, positions carried overnight)" in section3
    assert "T-0016 100 bps, at most 365 sessions" in section3
    assert "bars of the 365 preceding UTC days feed the features only" in section3
    assert "at most 2 new entries per UTC day" in section3
    assert "two days of bars warm up" not in report
    assert "a trend rule holds cash while its month-end closes stay below" in report
    assert "price risk plus 0.80% taker fees on both legs exceeded" not in report
    assert "Coinbase daily candles (one per UTC day" in report
    assert "Buy-and-hold BTC+ETH" in report
    assert "## 14. Capital sensitivity (diagnostic only)" in report
    section11 = report.split("## 11. Stocks track")[1].split("## 12.")[0]
    assert "Stock-track results are in their own reports" in section11
    assert "neither has been evaluated on real stock data" not in report


def test_every_deployment_window_runs_from_a_utc_month_start_to_a_utc_month_start() -> None:
    """Every deployment window of the study starts and ends on a 1st of a month at 00:00 UTC, so
    each window's last session is a month end and its first open follows one."""
    plan = _synthetic_crypto_study().splits
    for start, end in deployment_windows(plan):
        for ts in (start, end):
            assert ts == midnight(ts.date()) and ts.day == 1
        assert is_month_end(UTC_DAYS, (end - timedelta(days=1)).date())
