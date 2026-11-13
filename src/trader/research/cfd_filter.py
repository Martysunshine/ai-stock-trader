"""Veto filters on a frozen CFD rule's signals, their controls, and labels to train them (T-0026).

Every filter here implements :class:`~trader.research.cfd_engine.CfdSignalFilter`: it is handed
one frozen signal and answers accept or refuse, nothing else (``research/cfd_engine.py``,
"Signal filter"). Features come from :class:`~trader.features.cfd_breakout.BreakoutFeatureBook`,
which reads only bars that ended at or before the signal's decision time.

* :class:`CfdModelFilter` - a sealed :class:`~trader.models.bundle.ModelBundle` scores the
  signal's features; the signal is accepted when the calibrated probability that it is profitable
  after all costs is at least the bundle's threshold (compared exactly, as
  ``research.filters.ModelThresholdFilter``).
* :class:`RefuseAll` - the filter of a fold that could not be trained: it refuses every signal
  (fail closed: cash), never falls back to the unfiltered rule.
* :class:`RandomRefusal` - the random control: each signal is refused with probability ``rate``,
  drawn from ``sha256(salt + signal_id)`` exactly as ``research.filters.HashRandomReject``.
* :class:`SmallestBreakRefusal` - the equal-turnover control: it refuses exactly as many signals
  as the filter refused in the same window, choosing them by a naive rule, the smallest break
  beyond the channel first (the signal's ``priority``, the break in mean true ranges; ties by
  signal id). Its universe is the set of signals the filter saw; any other signal is an error.

Labels (:func:`label_window`): an unfiltered run of the rule over a window, in the base cost
view, emits every in-window signal with its features (``cfd_engine.emit_candidates``). A signal
the run took is labelled by its realised trade; one it did not take (another crypto trade open,
the pause, the day's entry limit) is labelled by the same signal simulated alone from the sleeve's
capital with the same costs (:func:`trade_alone`). Its prices do not depend on the portfolio, so
the alone result of a taken signal is its realised one (a test checks it). Outcome bars end at
``outcome_end``: a trade still open there, or a signal the engine would refuse even alone (spread,
flat time, a stop beyond the open, a size the sleeve cannot carry), has no label. The label is the
win flag, net USD after the spread, slippage and financing above zero, and the net return on the
trade's notional.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

import numpy as np

from trader.data.quote_bars import QuoteBar
from trader.domain.types import Direction, canon_decimal
from trader.features.cfd_breakout import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    BreakoutFeatureBook,
    InsufficientHistory,
)
from trader.models.bundle import ModelBundle
from trader.research.cfd_engine import (
    CfdEngineConfig,
    CfdInstrument,
    CfdRule,
    CfdSignal,
    emit_candidates,
    run_cfd_engine,
)
from trader.research.filters import (
    FilterDecision,
    _draw_below,
    _draw_score,
    _hash_draw,
    _require_rate,
    quantize_score,
)
from trader.research.training import FoldData, to_datetime64

FeatureFn = Callable[[CfdSignal], Mapping[str, float]]


# ----------------------------------------------------------------------------- features


class SignalFeatures:
    """``signal -> features`` for the hourly channel rule, from a feature book."""

    def __init__(self, book: BreakoutFeatureBook) -> None:
        self.book = book

    def __call__(self, signal: CfdSignal) -> dict[str, float]:
        flat = signal.deadline or signal.decided_at + timedelta(minutes=signal.max_hold_minutes)
        return self.book.features(signal.instrument_id, signal.decided_at, signal.direction, flat)


# ----------------------------------------------------------------------------- filters


class CfdModelFilter:
    """Accept when the bundle's calibrated probability is at least its threshold."""

    def __init__(self, bundle: ModelBundle, features: FeatureFn) -> None:
        if bundle.feature_version != FEATURE_VERSION:
            raise ValueError(
                f"bundle {bundle.bundle_id} uses features {bundle.feature_version!r}, "
                f"not {FEATURE_VERSION}"
            )
        if tuple(bundle.feature_names) != FEATURE_NAMES:
            raise ValueError(f"bundle {bundle.bundle_id} has other feature names")
        self.bundle = bundle
        self.threshold = bundle.threshold
        self.features = features
        self.filter_id = f"model:{bundle.bundle_id}:{canon_decimal(bundle.threshold)}"

    def decide(self, signal: CfdSignal) -> FilterDecision:
        probability = self.bundle.predict_proba(self.features(signal))
        accept = Decimal(probability) >= self.threshold
        return FilterDecision(
            accept=accept,
            score=quantize_score(probability),
            reason="probability_at_or_above_threshold" if accept else "probability_below_threshold",
        )


class RefuseAll:
    """The filter of a fold without a trained model: refuse every signal (cash)."""

    filter_id = "no-trained-model"

    def decide(self, signal: CfdSignal) -> FilterDecision:
        del signal
        return FilterDecision(accept=False, score=None, reason="no_trained_model")


class RandomRefusal:
    """Random control: refuse each signal with probability ``rate`` (hash draw, seeded)."""

    def __init__(self, rate: Decimal, salt: str) -> None:
        self.rate = _require_rate(rate)
        if not salt:
            raise ValueError("a random control needs a salt")
        self.salt = salt
        self.filter_id = f"random-refusal:{canon_decimal(rate)}:{salt}"

    def decide(self, signal: CfdSignal) -> FilterDecision:
        draw = _hash_draw(self.salt, signal.signal_id)
        refuse = _draw_below(draw, self.rate)
        return FilterDecision(
            accept=not refuse,
            score=_draw_score(draw),
            reason="random_refusal" if refuse else "random_accept",
        )


class SmallestBreakRefusal:
    """Equal-turnover control: refuse ``count`` signals of ``universe``, the smallest break first.

    ``universe`` holds every signal the reference filter saw in the window, with its break
    (``CfdSignal.priority``); ties go to the smaller signal id."""

    filter_id = "smallest-break-first"

    def __init__(self, universe: Sequence[tuple[str, Decimal]], count: int) -> None:
        ids = [sid for sid, _ in universe]
        if len(set(ids)) != len(ids):
            raise ValueError("a signal appears twice in the control's universe")
        if not 0 <= count <= len(ids):
            raise ValueError(f"cannot refuse {count} of {len(ids)} signals")
        ranked = sorted(universe, key=lambda x: (x[1], x[0]))
        self.universe = frozenset(ids)
        self.refused = frozenset(sid for sid, _ in ranked[:count])
        self.count = count

    @classmethod
    def matching(cls, seen: Sequence[CfdSignal], refusals: int) -> SmallestBreakRefusal:
        """The control for a filter run: the signals it saw and how many it refused."""
        return cls([(s.signal_id, s.priority) for s in seen], refusals)

    def decide(self, signal: CfdSignal) -> FilterDecision:
        if signal.signal_id not in self.universe:
            raise ValueError(
                f"signal {signal.signal_id} was not seen by the filter; the equal-turnover "
                "control must run on exactly the same signals"
            )
        refuse = signal.signal_id in self.refused
        return FilterDecision(
            accept=not refuse,
            score=None,
            reason="smallest_break_refused" if refuse else "smallest_break_accepted",
        )


# ----------------------------------------------------------------------------- labels


class DecisionWindow:
    """A rule whose signals are kept only when decided in ``[start, until)``; its state still
    sees every bar, so the signals it keeps are exactly the rule's own."""

    def __init__(self, rule: CfdRule, start: datetime, until: datetime) -> None:
        self.rule = rule
        self.start = start
        self.until = until

    @property
    def strategy_id(self) -> str:
        return self.rule.strategy_id

    @property
    def spec_hash(self) -> str:
        return self.rule.spec_hash

    def on_bar(self, instrument_id: str, bar: QuoteBar) -> CfdSignal | None:
        signal = self.rule.on_bar(instrument_id, bar)
        if signal is None or not self.start <= signal.decided_at < self.until:
            return None
        return signal

    def exit_signal(self, instrument_id: str, bar: QuoteBar, direction: Direction) -> str | None:
        return self.rule.exit_signal(instrument_id, bar, direction)


class _OneSignal:
    """Emits one given signal at its decision bar and nothing else."""

    def __init__(self, signal: CfdSignal) -> None:
        self.signal = signal

    @property
    def strategy_id(self) -> str:
        return "alone"

    @property
    def spec_hash(self) -> str:
        return "0" * 64

    def on_bar(self, instrument_id: str, bar: QuoteBar) -> CfdSignal | None:
        s = self.signal
        if instrument_id == s.instrument_id and bar.end == s.decided_at:
            return s
        return None

    def exit_signal(self, instrument_id: str, bar: QuoteBar, direction: Direction) -> str | None:
        del instrument_id, bar, direction
        return None


@dataclass(frozen=True, slots=True)
class Outcome:
    net_usd: Decimal
    notional_usd: Decimal
    exit_time: datetime
    exit_reason: str


def trade_alone(config: CfdEngineConfig, signal: CfdSignal) -> Outcome | None:
    """``signal`` simulated alone from ``config.capital`` with ``config``'s costs, on its
    instrument's bars from the decision bar to ``config.end``. ``None``: not taken, or still
    open when the bars end."""
    inst = config.instruments[signal.instrument_id]
    bars = tuple(b for b in inst.bars if signal.decided_at <= b.end and b.start < config.end)
    alone = replace(
        config,
        instruments={signal.instrument_id: CfdInstrument(inst.spec, bars, inst.fx_pair)},
        start=signal.decided_at,
    )
    result = run_cfd_engine(alone, _OneSignal(signal))
    if not result.trades:
        return None
    (trade,) = result.trades
    if trade.exit_time is None or trade.exit_reason == "END_OF_DATA":
        return None
    return Outcome(trade.net_pnl(), trade.notional_usd, trade.exit_time, trade.exit_reason or "")


@dataclass(frozen=True, slots=True)
class CfdLabel:
    signal_id: str
    instrument_id: str
    decided_at: datetime
    direction: str
    break_atr: Decimal
    features: Mapping[str, float]
    status_in_run: str
    source: Literal["realised", "alone"]
    net_usd: Decimal
    notional_usd: Decimal
    exit_time: datetime
    exit_reason: str

    @property
    def win(self) -> bool:
        return self.net_usd > 0

    @property
    def net_return(self) -> float:
        return float(self.net_usd / self.notional_usd) if self.notional_usd else 0.0


@dataclass(frozen=True, slots=True)
class LabelWindow:
    labels: tuple[CfdLabel, ...]
    #: signals seen, and why the ones without a label have none
    counts: dict[str, int]


def label_window(
    config: CfdEngineConfig,
    rule: CfdRule,
    features: FeatureFn,
    *,
    decide_until: datetime,
) -> LabelWindow:
    """Labels of every signal decided in ``[config.start, decide_until)``; outcome bars end at
    ``config.end``. ``config`` is the base cost view."""
    windowed = DecisionWindow(rule, config.start, decide_until)
    result = run_cfd_engine(config, windowed, record_candidates=True)
    missing: set[str] = set()

    def safe(signal: CfdSignal) -> Mapping[str, float]:
        try:
            return features(signal)
        except InsufficientHistory:
            missing.add(signal.signal_id)
            return {}

    counts = {"signals": 0, "realised": 0, "alone": 0, "no_features": 0, "no_outcome": 0}
    out: list[CfdLabel] = []
    for c in emit_candidates(result, safe):
        counts["signals"] += 1
        s = c.signal
        if s.signal_id in missing:
            counts["no_features"] += 1
            continue
        source: Literal["realised", "alone"]
        if (
            c.trade is not None
            and c.trade.exit_time is not None
            and (c.trade.exit_reason != "END_OF_DATA")
        ):
            outcome: Outcome | None = Outcome(
                c.trade.net_pnl(),
                c.trade.notional_usd,
                c.trade.exit_time,
                c.trade.exit_reason or "",
            )
            source = "realised"
        else:
            outcome = trade_alone(config, s)
            source = "alone"
        if outcome is None:
            counts["no_outcome"] += 1
            continue
        counts[source] += 1
        out.append(
            CfdLabel(
                signal_id=s.signal_id,
                instrument_id=s.instrument_id,
                decided_at=s.decided_at,
                direction=s.direction.value,
                break_atr=s.priority,
                features=dict(c.features),
                status_in_run=c.status,
                source=source,
                net_usd=outcome.net_usd,
                notional_usd=outcome.notional_usd,
                exit_time=outcome.exit_time,
                exit_reason=outcome.exit_reason,
            )
        )
    return LabelWindow(tuple(out), counts)


def fold_data(labels: Sequence[CfdLabel], *, family: str, source: Mapping[str, str]) -> FoldData:
    """Labels as :class:`~trader.research.training.FoldData` (columns ``FEATURE_NAMES``)."""
    rows = sorted(labels, key=lambda r: (r.decided_at, r.signal_id))
    x = np.asarray(
        [[r.features[name] for name in FEATURE_NAMES] for r in rows], dtype=np.float64
    ).reshape(len(rows), len(FEATURE_NAMES))
    return FoldData(
        X=x,
        y=np.asarray([int(r.win) for r in rows], dtype=np.int64),
        decision_time=np.asarray(
            [to_datetime64(r.decided_at) for r in rows], dtype="datetime64[ms]"
        ),
        exit_time=np.asarray([to_datetime64(r.exit_time) for r in rows], dtype="datetime64[ms]"),
        net_return=np.asarray([r.net_return for r in rows], dtype=np.float64),
        feature_names=FEATURE_NAMES,
        feature_version=FEATURE_VERSION,
        family=family,
        data_hashes=dict(source),
    )


__all__ = [
    "CfdLabel",
    "CfdModelFilter",
    "DecisionWindow",
    "LabelWindow",
    "RandomRefusal",
    "RefuseAll",
    "SignalFeatures",
    "SmallestBreakRefusal",
    "fold_data",
    "label_window",
    "trade_alone",
]
