"""SYNTHETIC daily-bar fixtures for the daily research engine and the daily rule families.

Nothing here is market data and no real instrument is evaluated: the prices are a deterministic
function of the session index and a SHA-256 stream, built to exercise the machinery.

* :func:`daily_history` - one bar per XNYS regular session (``start`` = the session open,
  ``end`` = its close or early close, available from ``end + 1 s``) for the 16-ETF basket of the
  daily-bar design (``ARCX:SPY``, ``XNAS:QQQ`` ...; identifiers only, the prices are invented).
  It includes:
  - regimes: a bull run with a sharp three-session dip every 37 sessions per equity ETF (offset
    per symbol, so the dips of different ETFs fall on different days), a bear market, a
    recovery, a sideways stretch and a second bull run; bonds and gold have their own drifts;
  - a market-wide opening gap down of about 6% at one session (gaps through stops);
  - two splits on raw prices: ``ARCX:XLK`` 2-for-1 and ``ARCX:XLE`` 3-for-1;
  - the calendar's early closes (bars of 210 minutes);
  - one missing session (``ARCX:XLB``) unless ``missing=()``.
* :func:`daily_specs` - fractional equity specs for the basket (SYNTHETIC evidence).
* :func:`daily_study` - an in-memory ``StudyContext`` (documented Alpaca cost basis, base policy)
  and the fixture's ``LoadedData`` for ``run_trial`` / ``run_windows``.
* :class:`ScriptedDailyRule` - an overnight rule that emits planned candidates and exit signals
  on given sessions (engine tests).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from trader.accounting.fees import load_fee_schedule
from trader.config.policy import load_policy
from trader.data.calendars import ExchangeCalendar, Session
from trader.data.corporate_actions import SplitEvent
from trader.data.history import HistoryBar
from trader.data.manifest import load_manifest
from trader.domain.candidates import TradeCandidate
from trader.domain.instruments import EquitySpec
from trader.domain.market import Bar
from trader.domain.types import BPS, Direction, Track
from trader.research.engine import InstrumentSetup
from trader.research.study import LoadedData, StudyConfig, StudyContext, TrialCfg

REPO = Path(__file__).resolve().parents[1]
CALENDAR = ExchangeCalendar("XNYS")
FEED = "synthetic-daily"
CENT = Decimal("0.01")

#: The 16-ETF basket of the daily-bar design: identifiers only; every price is SYNTHETIC.
UNIVERSE: tuple[str, ...] = (
    "ARCX:SPY",
    "XNAS:QQQ",
    "ARCX:IWM",
    "ARCX:DIA",
    "ARCX:XLB",
    "ARCX:XLE",
    "ARCX:XLF",
    "ARCX:XLI",
    "ARCX:XLK",
    "ARCX:XLP",
    "ARCX:XLU",
    "ARCX:XLV",
    "ARCX:XLY",
    "XNAS:TLT",
    "XNAS:IEF",
    "ARCX:GLD",
)
EQUITY_ETFS: tuple[str, ...] = UNIVERSE[:13]

START = date(2016, 1, 1)
END = date(2021, 1, 1)  # exclusive: five years of sessions
SPLITS = (
    SplitEvent("ARCX:XLK", date(2018, 6, 4), Decimal(2), Decimal(1), "synthetic fixture"),
    SplitEvent("ARCX:XLE", date(2019, 9, 9), Decimal(3), Decimal(1), "synthetic fixture"),
)
#: Session index (from START) of the market-wide opening gap down.
GAP_SESSION = 1000
DEFAULT_MISSING: tuple[tuple[str, date], ...] = (("ARCX:XLB", date(2018, 1, 18)),)

_START_PRICE = {
    "ARCX:SPY": "200",
    "XNAS:QQQ": "110",
    "ARCX:IWM": "115",
    "ARCX:DIA": "170",
    "XNAS:TLT": "120",
    "XNAS:IEF": "105",
    "ARCX:GLD": "105",
}

# (first session index, equity drift per session, noise amplitude): bull, bear, recovery,
# sideways, bull.
_EQUITY_PHASES = (
    (0, Decimal("0.0007"), Decimal("0.006")),
    (330, Decimal("-0.0022"), Decimal("0.010")),
    (480, Decimal("0.0025"), Decimal("0.008")),
    (620, Decimal("0"), Decimal("0.010")),
    (900, Decimal("0.0006"), Decimal("0.006")),
)
_BOND_DRIFT = ((0, Decimal("0.0002")), (330, Decimal("0.0010")), (480, Decimal("-0.0004")))
_GOLD_DRIFT = ((0, Decimal("0.0005")), (600, Decimal("-0.0004")), (1000, Decimal("0.0003")))
_DIP = (Decimal("-0.014"), Decimal("-0.016"), Decimal("-0.011"), Decimal("0.010"), Decimal("0.009"))


def sessions(start: date = START, end: date = END) -> list[Session]:
    """Every XNYS session in ``[start, end)``."""
    out: list[Session] = []
    day = start - timedelta(days=1)
    while True:
        session = CALENDAR.sessions_after(day)
        if session.session_date >= end:
            return out
        out.append(session)
        day = session.session_date


def _unit(seed: str, i: int) -> Decimal:
    """A deterministic value in [-1, 1) from SHA-256 (no random module)."""
    digest = hashlib.sha256(f"{seed}|{i}".encode()).digest()
    return Decimal(int.from_bytes(digest[:8], "big")) / Decimal(2**63) - 1


def _phase(table: Sequence[tuple[Any, ...]], k: int) -> tuple[Any, ...]:
    current = table[0]
    for row in table:
        if k >= row[0]:
            current = row
    return current


def _return(iid: str, k: int) -> Decimal:
    noise = _unit(iid + ":r", k)
    if iid in EQUITY_ETFS:
        j = EQUITY_ETFS.index(iid)
        beta = Decimal("0.7") + Decimal("0.05") * j
        _, drift, amplitude = _phase(_EQUITY_PHASES, k)
        r = beta * drift + amplitude * noise
        bull = (drift > 0 and k < 330) or k >= 900
        position = (k + 3 * j) % 37
        if bull and position < len(_DIP):
            r += _DIP[position]
        return r
    if iid in ("XNAS:TLT", "XNAS:IEF"):
        scale = Decimal(1) if iid == "XNAS:TLT" else Decimal("0.5")
        return scale * _phase(_BOND_DRIFT, k)[1] + Decimal("0.004") * scale * noise
    return _phase(_GOLD_DRIFT, k)[1] + Decimal("0.008") * noise


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class DailyFixture:
    bars: dict[str, list[Bar]]
    splits: tuple[SplitEvent, ...]
    missing: tuple[tuple[str, date], ...]
    early_closes: tuple[date, ...]
    gap_session: date
    first: date = field(default=START)

    def setups(self, specs: Mapping[str, EquitySpec] | None = None) -> dict[str, InstrumentSetup]:
        specs = specs or daily_specs(self.bars)
        return {
            iid: InstrumentSetup(
                spec=specs[iid],
                bars=bars,
                splits=tuple(s for s in self.splits if s.instrument_id == iid),
            )
            for iid, bars in self.bars.items()
            if iid in specs
        }

    def history(self, instrument_id: str) -> list[HistoryBar]:
        """The bars as stored history rows (``minutes`` varies on early closes)."""
        return [
            HistoryBar(
                start=b.start,
                minutes=b.minutes,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                vwap=b.vwap,
                trade_count=b.trade_count,
            )
            for b in self.bars[instrument_id]
        ]


def session_bar(
    instrument_id: str,
    session: Session,
    o: Decimal,
    h: Decimal,
    low: Decimal,
    c: Decimal,
    volume: Decimal = Decimal(5_000_000),
) -> Bar:
    """One daily bar: the whole regular session, available one second after its close."""
    vwap = min(max(_q((h + low + c) / 3), low), h)
    return Bar(
        instrument_id=instrument_id,
        feed=FEED,
        minutes=int((session.close - session.open).total_seconds() // 60),
        start=session.open,
        end=session.close,
        available_time=session.close + timedelta(seconds=1),
        complete=True,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=volume,
        trade_count=1000,
        vwap=vwap,
    )


def daily_history(
    instruments: Iterable[str] = UNIVERSE,
    *,
    start: date = START,
    end: date = END,
    missing: Sequence[tuple[str, date]] = DEFAULT_MISSING,
    splits: Sequence[SplitEvent] = SPLITS,
) -> DailyFixture:
    """Deterministic SYNTHETIC daily bars for ``instruments`` over ``[start, end)``."""
    days = sessions(start, end)
    gap_day = days[GAP_SESSION].session_date if len(days) > GAP_SESSION else days[-1].session_date
    skip = set(missing)
    out: dict[str, list[Bar]] = {}
    for iid in instruments:
        price = Decimal(_START_PRICE.get(iid, str(30 + 4 * UNIVERSE.index(iid))))
        factor = Decimal(1)  # shares after per share before, cumulative (raw = economic / factor)
        bars: list[Bar] = []
        for k, session in enumerate(days):
            for s in splits:
                if s.instrument_id == iid and s.ex_date == session.session_date:
                    factor *= s.ratio
            r = _return(iid, k)
            gap = Decimal("0.003") * _unit(iid + ":g", k)
            if k == GAP_SESSION and iid in EQUITY_ETFS:
                gap = Decimal("-0.06")
                r = Decimal("-0.05")
            o = _q(price * (1 + gap) / factor)
            c = _q(price * (1 + r) / factor)
            h = _q(max(o, c) * (1 + Decimal("0.004") * abs(_unit(iid + ":h", k))))
            low = _q(min(o, c) * (1 - Decimal("0.004") * abs(_unit(iid + ":l", k))))
            price = c * factor
            if (iid, session.session_date) in skip:
                continue
            volume = Decimal(5_000_000) * factor + _q(Decimal(100_000) * _unit(iid + ":v", k))
            bars.append(session_bar(iid, session, o, h, low, c, volume))
        out[iid] = bars
    return DailyFixture(
        bars=out,
        splits=tuple(s for s in splits if s.instrument_id in out),
        missing=tuple(m for m in missing if m[0] in out),
        early_closes=tuple(s.session_date for s in days if s.is_early_close),
        gap_session=gap_day,
        first=start,
    )


def daily_specs(instruments: Iterable[str] = UNIVERSE) -> dict[str, EquitySpec]:
    """Fractional equity specs for the basket (SYNTHETIC; not broker metadata)."""
    out: dict[str, EquitySpec] = {}
    for iid in instruments:
        venue, symbol = iid.split(":")
        out[iid] = EquitySpec.model_validate(
            {
                "identity": {
                    "instrument_id": iid,
                    "broker": "replay",
                    "broker_asset_id": f"synthetic-{symbol.lower()}",
                    "symbol": symbol,
                    "venue_mic": venue,
                    "currency": "USD",
                    "product_type": "ORDINARY_EQUITY",
                    "track": "STOCKS",
                },
                "quantity_step": "0.000000001",
                "min_quantity": "0.000000001",
                "min_notional": "1.00",
                "price_tick": "0.01",
                "fractionable": True,
                "evidence": "REPLAY_SIMULATED",
                "as_of": "2026-11-09T00:00:00Z",
                "source": "synthetic daily fixture (tests/daily_fixtures.py)",
            }
        )
    return out


# ----------------------------------------------------------------------------- studies


def study_config(
    trials: Sequence[TrialCfg],
    instruments: Sequence[str] = UNIVERSE,
    **updates: Any,
) -> StudyConfig:
    """A daily ETF study over the fixture (SYNTHETIC: a pipeline check, never evidence)."""
    raw: dict[str, Any] = {
        "study_id": "synthetic_etf_daily",
        "title": "SYNTHETIC daily ETF study (pipeline check, not evidence)",
        "track": "STOCKS",
        "description": "Deterministic synthetic daily bars; checks the daily-bar machinery.",
        "price_dataset": "synthetic_us_etfs_daily_test",
        "instruments": [
            {"price_instrument_id": i, "execution_instrument_id": i} for i in instruments
        ],
        "instrument_specs": "tests/daily_fixtures.py",
        "fee_schedule": "configs/fees/alpaca_us_equities_v2.yaml",
        "policy": "configs/policy/base.yaml",
        "fills": {
            "half_spread_bps": "2.0",
            "slippage_bps": "2.0",
            "participation": "0.10",
            "latency_ms": 500,
        },
        "stress": {"multiplier": "2", "missed_entry_percent": 5},
        "loss_windows": ["NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"],
        "session_basis": "XNYS",
        "annualisation_sessions": 252,
        "splits": {
            "development_start": "2017-01-01T00:00:00Z",
            "holdout_start": "2020-07-01T00:00:00Z",
            "holdout_end": "2021-01-01T00:00:00Z",
            "walk_forward": {
                "train_months": 12,
                "validation_months": 3,
                "test_months": 3,
                "step_months": 3,
            },
            "embargo_minutes": 0,
        },
        "operating_costs_usd_per_month": "0",
        "bootstrap": {"replicates": 200, "mean_block_sessions": "5", "seed": 20261109},
        "universe_selection": "SYNTHETIC fixed basket (identifiers only)",
        "horizon": "daily bars, multi-day holds",
        "entry_exit_timing": "decision at a completed session's close, entry at the next open",
        "trials": [t.model_dump(mode="json") for t in trials],
    }
    raw.update(updates)
    return StudyConfig.model_validate(raw)


def daily_study(
    config: StudyConfig,
    fixture: DailyFixture,
    *,
    repo_root: Path = REPO,
) -> tuple[StudyContext, LoadedData]:
    """An in-memory study context (no manifest dataset is read) and the fixture's data."""
    ids = [m.execution_instrument_id for m in config.instruments]
    specs = daily_specs(ids)
    ctx = StudyContext(
        repo_root=repo_root,
        config=config,
        manifest=load_manifest(REPO / "research" / "data_manifest.json"),
        policy=load_policy(REPO / config.policy),
        fees=load_fee_schedule(REPO / config.fee_schedule),
        specs=dict(specs),
    )
    setups = {iid: setup for iid, setup in fixture.setups(specs).items() if iid in specs}
    data = LoadedData(
        setups=setups,
        start=config.splits.development_start,
        end=config.splits.holdout_start,
    )
    return ctx, data


# ----------------------------------------------------------------------------- scripted rule


@dataclass(frozen=True, slots=True)
class Plan:
    """A candidate to emit at the close of ``session``."""

    stop: Decimal
    target: Decimal
    max_hold_minutes: int = 60 * 24 * 30
    ttl_seconds: int = 60


class ScriptedDailyRule:
    """An overnight daily rule driven by a script (engine tests).

    ``plans[(instrument, session date)]`` emits a long candidate on that session's completed
    bar; ``exits[(instrument, session date)]`` returns that exit-signal reason for it.
    """

    strategy_id = "scripted-daily"
    spec_hash = "d" * 64
    uses_source_vwap = False

    def __init__(
        self,
        plans: Mapping[tuple[str, date], Plan],
        exits: Mapping[tuple[str, date], str] | None = None,
        *,
        holding: str = "OVERNIGHT",
        max_hold_sessions: int | None = None,
        target_role: str | None = None,
        with_exit_signal: bool = True,
    ) -> None:
        self.plans = dict(plans)
        self.exits = dict(exits or {})
        self.holding = holding
        self.max_hold_sessions = max_hold_sessions
        self.target_role = target_role
        self.seen: list[tuple[str, date]] = []
        self.signal_calls: list[tuple[str, date]] = []
        self.splits: list[tuple[str, Decimal]] = []
        if not with_exit_signal:
            self.exit_signal = None  # type: ignore[assignment]

    def on_bar(self, bar: Bar, now: datetime) -> TradeCandidate | None:
        assert bar.is_usable_at(now)
        day = CALENDAR.session_date_of(bar.start)
        self.seen.append((bar.instrument_id, day))
        plan = self.plans.get((bar.instrument_id, day))
        if plan is None:
            return None
        return TradeCandidate(
            candidate_id=f"c-{bar.instrument_id}-{day.isoformat()}",
            strategy_id=self.strategy_id,
            strategy_version=1,
            spec_hash=self.spec_hash,
            instrument_id=bar.instrument_id,
            track=Track.STOCKS,
            direction=Direction.LONG,
            feed=bar.feed,
            bar_end=bar.end,
            decision_time=bar.available_time,
            reference_price=bar.close,
            stop_price=plan.stop,
            target_price=plan.target,
            max_hold_minutes=plan.max_hold_minutes,
            entry_limit_offset_bps=Decimal(5),
            entry_ttl_seconds=plan.ttl_seconds,
            gap_allowance_fraction=Decimal(10) * BPS,
            snapshot_hash="0" * 64,
            expires_at=bar.available_time + timedelta(seconds=60),
        )

    def exit_signal(self, instrument_id: str, bar: Bar) -> str | None:
        day = CALENDAR.session_date_of(bar.start)
        self.signal_calls.append((instrument_id, day))
        return self.exits.get((instrument_id, day))

    def on_split(self, instrument_id: str, divisor: Decimal) -> None:
        self.splits.append((instrument_id, divisor))

    def data_ok(self, instrument_id: str) -> bool:
        return True


def utc(day: date, hour: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
