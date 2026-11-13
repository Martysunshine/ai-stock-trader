"""The CFD engine's signal filter hook (T-0026): absent, it changes nothing; present, it can only
refuse a signal, never add one or change its size, stop, target or timing; and an unfiltered run
can emit every candidate with its features and its realised trade for labels."""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from decimal import Decimal

import pytest

from trader.data.quote_bars import QuoteBar
from trader.research.cfd_engine import (
    CfdEngineConfig,
    CfdEngineResult,
    CfdInstrument,
    CfdSignal,
    CfdSignalFilter,
    emit_candidates,
    run_cfd_engine,
)
from trader.research.filters import FilterDecision

from .cfd_fixtures import CFD_POLICY, FEES, MONDAY, ScriptedRule, day, flat_days, long, qbar, short
from .cfd_fixtures import spec as cfd_spec
from .test_cfd_engine import BASE

US100 = "CAPITAL:US100"

#: ``result_hash`` of this scenario by the engine as it was before the hook existed (24af8ab).
PINNED_HASH = "d45041cf33684ef0df1a814640aa18ab9c6943a1bc6f2ee486f5874522e9ce6d"


def bars() -> list[QuoteBar]:
    return [
        qbar(day(0), "20000", "20050", "19950", "20000"),
        qbar(day(1), "20010", "20500", "19950", "20300"),
        *flat_days(2, 2, "20300"),
        qbar(day(4), "20300", "20320", "20100", "20150"),
        *flat_days(5, 4, "20150"),
        qbar(day(9), "20150", "20160", "19700", "19750"),
        *flat_days(10, 6, "19750"),
    ]


def rule() -> ScriptedRule:
    return ScriptedRule(
        {
            (US100, day(0)): long("19500", "20400"),
            (US100, day(3)): long("19800", "21000", max_hold_minutes=3 * 1440),
            (US100, day(4)): short("20700", "19000"),  # the day-3 trade is still open: busy
            (US100, day(8)): short("20600", "19800"),
            (US100, day(11)): long("19000", "20500", max_hold_minutes=2 * 1440),
        }
    )


def config() -> CfdEngineConfig:
    return CfdEngineConfig(
        policy=CFD_POLICY,
        fees=FEES,
        instruments={US100: CfdInstrument(cfd_spec("US100"), tuple(bars()))},
        costs=BASE,
        start=MONDAY,
        end=day(30),
        capital=Decimal(100),
    )


def run(
    signal_filter: CfdSignalFilter | None = None, *, record_candidates: bool = False
) -> CfdEngineResult:
    return run_cfd_engine(config(), rule(), signal_filter, record_candidates=record_candidates)


class Refuse:
    """Refuses the signals it names and accepts the rest."""

    def __init__(self, refused: set[str], filter_id: str = "test-refuse") -> None:
        self.refused = refused
        self.filter_id = filter_id
        self.asked: list[CfdSignal] = []

    def decide(self, signal: CfdSignal) -> FilterDecision:
        self.asked.append(signal)
        if signal.signal_id in self.refused:
            return FilterDecision(accept=False, score=None, reason="refused")
        return FilterDecision(accept=True, score=Decimal("0.5"), reason="accepted")


def _ids(result: CfdEngineResult) -> list[str]:
    return [c.candidate_id for c in result.candidates]


def test_the_scenario_trades_refuses_and_times_out() -> None:
    result = run()
    statuses = {c.candidate_id: (c.status, c.binding) for c in result.candidates}
    assert len(result.candidates) == 5 and len(result.trades) == 4
    assert statuses[f"{US100}@{day(4).date()}"] == ("REJECTED", "busy")
    assert {t.exit_reason for t in result.trades} >= {"TARGET", "TIME_EXIT"}


def test_without_a_filter_the_engine_reproduces_its_pinned_result() -> None:
    plain = run()
    assert plain.result_hash == PINNED_HASH
    assert plain.filter_log == () and plain.filter_hash == "" and plain.signals == ()
    recorded = run(record_candidates=True)
    assert recorded.result_hash == PINNED_HASH and len(recorded.signals) == 5
    assert recorded.diagnostics == plain.diagnostics


def test_a_filter_that_accepts_everything_changes_nothing_but_its_log() -> None:
    result = run(Refuse(set()))
    plain = run()
    assert result.result_hash == plain.result_hash
    assert [t.as_dict() for t in result.trades] == [t.as_dict() for t in plain.trades]
    assert result.final_equity == plain.final_equity
    assert result.diagnostics == plain.diagnostics
    assert len(result.filter_log) == 5 and all(r.accept for r in result.filter_log)
    assert result.filter_hash and result.filter_log[0].score == "0.5"


def test_a_refusal_is_a_candidate_outcome_with_its_own_reason() -> None:
    first = f"{US100}@{day(0).date()}"
    result = run(Refuse({first}))
    outcome = next(c for c in result.candidates if c.candidate_id == first)
    assert outcome.status == "REJECTED" and outcome.reasons == ("NO_EDGE",)
    assert outcome.binding == "filter:test-refuse"
    assert first not in {t.signal_id for t in result.trades}
    (entry,) = [r for r in result.filter_log if r.candidate_id == first]
    assert entry.accept is False and entry.reason == "refused" and entry.score == ""


@pytest.mark.parametrize("size", [1, 2, 3, 5])
def test_the_filter_only_ever_refuses(size: int) -> None:
    plain = run()
    ids = _ids(plain)
    signals = {s.signal_id: s for s in run(record_candidates=True).signals}
    for refused in itertools.combinations(ids, size):
        flt = Refuse(set(refused))
        result = run(flt)
        # asked once per in-window signal, in order, whatever it answered before
        assert [s.signal_id for s in flt.asked] == ids
        assert _ids(result) == ids  # no signal added, none lost
        assert len(result.trades) <= len(plain.trades)
        for trade in result.trades:
            signal = signals[trade.signal_id]
            assert trade.signal_id not in refused
            # the signal reaches the entry unchanged: same side, stop and target
            assert (trade.stop, trade.target, trade.direction) == (
                signal.stop,
                signal.target,
                signal.direction,
            )
            same = next((t for t in plain.trades if t.signal_id == trade.signal_id), None)
            if same is not None:
                # same entry bar and price as without the filter; size only by equity
                assert (trade.entry_time, trade.entry_price) == (same.entry_time, same.entry_price)
        for outcome in result.candidates:
            assert outcome.binding.startswith("filter:") == (outcome.candidate_id in refused)


def test_a_filter_answer_must_be_a_decision() -> None:
    class Bad:
        filter_id = "bad"

        def decide(self, signal: CfdSignal) -> object:
            return True

    with pytest.raises(TypeError, match="FilterDecision"):
        run(Bad())  # type: ignore[arg-type]


def test_an_unfiltered_run_emits_every_candidate_with_features_and_its_trade() -> None:
    result = run(record_candidates=True)
    seen: list[str] = []

    def features(signal: CfdSignal) -> Mapping[str, float]:
        seen.append(signal.signal_id)
        return {"stop_distance": float(abs(signal.reference - signal.stop))}

    candidates = emit_candidates(result, features)
    assert [c.signal.signal_id for c in candidates] == _ids(result) == seen
    by_id = {t.signal_id: t for t in result.trades}
    for c in candidates:
        assert c.features["stop_distance"] > 0
        if c.status == "FILLED":
            assert c.trade is by_id[c.signal.signal_id]
        else:
            assert c.trade is None and c.binding == "busy"
    filtered = run(Refuse(set()), record_candidates=True)
    with pytest.raises(ValueError, match="unfiltered"):
        emit_candidates(filtered, features)
