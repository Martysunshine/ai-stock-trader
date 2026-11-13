"""The shared decision loop (runtime/decision_loop.py): replay behaviour is unchanged, and the
paper ingestion path (completed bars added from outside the market view) decides exactly as
replay does on the same bars and quotes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import timedelta
from pathlib import Path

from tests.helpers import ENTRY_DECISION, harness
from trader.config.host import HostRole
from trader.config.runtime_config import load_config
from trader.data.bars import BarBuilder
from trader.data.recorded_session import load_session
from trader.domain.market import Bar, Quote, Trade, TradingStatus
from trader.ops.clock import ReplayClock
from trader.reporting.hashes import decision_hash, ledger_hash
from trader.runtime.decision_loop import DecisionLoop, no_bars
from trader.runtime.environment import build_replay_environment
from trader.runtime.replay_runner import session_close_for
from trader.runtime.scenarios import SCENARIOS
from trader.storage.db import Database

ROOT = Path(__file__).resolve().parents[1]

#: Decision and ledger hashes of every demonstrator scenario, recorded before the decision loop
#: was extracted from the replay runner. Any change here is a behaviour change of replay.
PINNED_REPLAY_HASHES: dict[str, tuple[str, str]] = {
    "baseline": (
        "765ac380599d592c3aea1825dbc4f8b639c41cf82a6ebc42c445e226d8cb31c0",
        "119ebd21a46f846fa824d7266fc1016de521506001fbd3a285589c2849bc691e",
    ),
    "timeout": (
        "e4912ebd44269e5bbbd2288a99f20c80fd4aaea2905df11ca3092adcbd55ea63",
        "112072ee0304b0787ab24a9337c82ec95e4f016c9458628946bd1f79e80bbc5d",
    ),
    "crash": (
        "fa3d2579cf0d275d78c0343c9fec93b30b04e027ca849bdebfbaa1e88422beda",
        "3ed871117343a2f0358e39f9908da142337f79f9cf0440348a04fbd5c655799d",
    ),
    "costly": (
        "d3213364d68cb3931f0ce3937b27e52ccd65aa32c72ebbc4b00a661714f02884",
        "12d0eb413ed9329ee68378a029e1849bbdba2217484b2069aaf85eb0b49bc7d1",
    ),
    "loss": (
        "72e0310eeb4ce3d4a0b83115d316280baad119b5d3bfa430d09a2f1cc93c6d7a",
        "add988c34e52180b59ad07ab6b2fe6d400331c6a72a0ecbad8f465041890062d",
    ),
}


def test_paper_style_bar_ingestion_reproduces_the_replay_baseline(tmp_path: Path) -> None:
    """Bars built outside the market view and added as *completed* bars (the paper path) give
    the same decisions, orders, fills and ledger as the replay runner on the same session."""
    scenario = SCENARIOS["baseline"]
    session = load_session(ROOT / "research" / "sessions" / scenario.session)
    db = Database(f"sqlite:///{(tmp_path / 'parity.db').as_posix()}")
    db.create_schema()
    clock = ReplayClock(session.events[0].receipt_time - timedelta(minutes=1))
    config = load_config(ROOT / "configs" / "replay.yaml", host_role=HostRole.DEV)
    env = build_replay_environment(
        config,
        db=db,
        session=session,
        run_id="demo-baseline",
        clock=clock,
        faults=scenario.faults,
    )
    loop = DecisionLoop(env)
    loop.start()
    source: dict[str, BarBuilder] = {}  # stands in for the data API's completed bars
    minutes = env.market.decision_minutes
    for event in session.events:
        clock.advance_to(event.receipt_time)
        if isinstance(event, Trade):
            if event.feed == env.market.feed:
                source.setdefault(
                    event.instrument_id, BarBuilder(event.instrument_id, event.feed, minutes)
                ).add(event)
        else:
            env.market.on_event(event)  # quotes and trading status only
        env.broker.on_market_event(event)
        bars: Callable[[], Iterable[Bar]] = no_bars
        if isinstance(event, Quote):
            builder = source.get(event.instrument_id)

            def completed(builder: BarBuilder | None = builder) -> list[Bar]:
                ready = [] if builder is None else builder.finalize_ready(clock.now())
                return [b for b in ready if env.market.add_completed_bar(b)]

            bars = completed
        loop.cycle(market_event=isinstance(event, Quote | TradingStatus), bars=bars)
    close = session_close_for(env, session.manifest.session_date)
    clock.advance_to(close)
    env.broker.end_of_session(close)
    loop.pump()
    env.reconciler.reconcile(loop.account_id, cleared_by="end-of-session")
    with db.tx() as conn:
        hashes = (decision_hash(conn), ledger_hash(conn))
    env.close()
    assert loop.stats.approved == 1
    assert hashes == PINNED_REPLAY_HASHES["baseline"]


def test_completed_bars_never_rewrite_history_or_mix_feeds(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{(tmp_path / 'view.db').as_posix()}")
    db.create_schema()
    h = harness(db)
    h.run_until(ENTRY_DECISION)
    view = h.env.market
    instrument = next(iid for iid, held in view.bars.items() if held)
    last = view.bars[instrument][-1]
    assert not view.add_completed_bar(last)  # same start: a late duplicate is refused
    assert not view.add_completed_bar(last.model_copy(update={"feed": "OTHER"}))
    newer = last.model_copy(
        update={
            "start": last.end,
            "end": last.end + timedelta(minutes=last.minutes),
            "available_time": last.end + timedelta(minutes=last.minutes, seconds=1),
        }
    )
    assert view.add_completed_bar(newer)
    assert view.bars[instrument][-1] == newer
    h.env.close()


def test_shutdown_cancels_working_entries_through_the_gateway(tmp_path: Path) -> None:
    """A graceful stop leaves no working entry order behind (it would fill unmonitored)."""
    db = Database(f"sqlite:///{(tmp_path / 'cancel.db').as_posix()}")
    db.create_schema()
    h = harness(db)
    h.run_until(ENTRY_DECISION)
    entries = [i for i in h.intents() if i.purpose == "ENTRY"]
    assert [i.state for i in entries] == ["ACKNOWLEDGED"]
    cancelled = h.runner.cancel_working_entries()
    assert cancelled == [entries[0].intent_id]
    assert [i.state for i in h.intents()] == ["CANCEL_REQUESTED"]
    assert h.runner.cancel_working_entries() == []  # nothing left to cancel
    h.env.close()
