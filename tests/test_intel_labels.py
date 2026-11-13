"""Catalyst labels (ADR-0051): a judgement of the intel feed, provably older than its session.

A label is only worth testing later if nobody could have written it with hindsight: the store
sets its time, refuses a session that is past, and accepts only references it already holds.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trader.cli.main import app
from trader.intel import labels
from trader.intel.__main__ import main as intel_main
from trader.intel.feed import refresh
from trader.intel.labels import (
    METHOD,
    Label,
    LabelError,
    add_label,
    latest_labels,
    load_labels,
    render_labels,
)
from trader.intel.store import LOCK_NAME, IntelStore, read_jsonl

from .test_intel_refresh import CONFIG, FIRST, NOW, WATCHLISTS, Clock, root_of

SRC = Path(__file__).resolve().parents[1] / "src" / "trader"
TODAY = date(2026, 11, 10)  # NOW is Tuesday 2026-11-10 14:00 UTC, 09:00 in New York
NEWS = "news:alpaca_news:1"
INSIDER = "insider:edgar_form4:0001197647-26-000013"
SNAP = "snap:stocks:NVDA@2026-11-10T14:00:00Z"
LATER = NOW + timedelta(minutes=5)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = root_of(tmp_path)
    refresh(CONFIG, WATCHLISTS, path, FIRST, clock=Clock())
    return path


def nvda(**changes: object) -> Label:
    fields: dict[str, object] = {
        "target": "stocks:NVDA",
        "session_date": TODAY,
        "catalyst": "earnings",
        "direction": "up",
        "confidence": "medium",
        "evidence": (NEWS, SNAP),
        "note": "Q3 beat; pre-market +1.4%",
        "labeller": "claude-fable-5",
    }
    fields.update(changes)
    return Label(**fields)  # type: ignore[arg-type]


def stored_lines(root: Path) -> list[dict[str, object]]:
    return read_jsonl(root / "labels" / "2026-11.jsonl")


def test_a_label_is_stored_with_the_time_the_store_first_held_it(root: Path) -> None:
    row = add_label(root, nvda(), now=LATER)
    assert row is not None
    assert (row.id, row.rev, row.first_seen_at) == ("stocks:NVDA@2026-11-10", 1, LATER)
    (line,) = stored_lines(root)
    assert line["first_seen_at"] == "2026-11-10T14:05:00Z"
    assert line["label"] == {
        "target": "stocks:NVDA",
        "session_date": "2026-11-10",
        "catalyst": "earnings",
        "direction": "up",
        "confidence": "medium",
        "evidence": [NEWS, SNAP],
        "note": "Q3 beat; pre-market +1.4%",
        "labeller": "claude-fable-5",
        "method": METHOD,
    }
    (listed,) = latest_labels(load_labels(root, since=NOW, until=LATER))
    assert listed == row
    text = render_labels([listed])
    assert text[0].startswith("2026-11-10  stocks:NVDA    earnings  up      medium rev 1, seen")
    assert text[1:] == ["    Q3 beat; pre-market +1.4%", f"    [{NEWS}] [{SNAP}]"]


def test_a_change_of_mind_is_a_new_revision_and_both_stay_on_file(root: Path) -> None:
    add_label(root, nvda(), now=LATER)
    assert add_label(root, nvda(), now=LATER + timedelta(minutes=1)) is None  # nothing new
    assert len(stored_lines(root)) == 1
    second = add_label(
        root, nvda(direction="unclear", confidence="low"), now=LATER + timedelta(minutes=9)
    )
    assert second is not None and second.rev == 2
    rows = load_labels(root, since=NOW, until=NOW + timedelta(hours=1))
    assert [(r.rev, r.label.direction) for r in rows] == [(1, "up"), (2, "unclear")]
    assert [r.rev for r in latest_labels(rows)] == [2]
    # a revision is identified by its content: saying the first thing again is a third revision
    third = add_label(root, nvda(), now=LATER + timedelta(minutes=20))
    assert third is not None and third.rev == 3


@pytest.mark.parametrize(
    ("session", "message"),
    [
        (date(2026, 11, 9), "is past"),
        (date(2026, 11, 14), "weekend"),
        (date(2026, 11, 16), "more than 4 days ahead"),
    ],
)
def test_a_session_that_is_past_or_far_ahead_is_refused(
    root: Path, session: date, message: str
) -> None:
    with pytest.raises(LabelError, match=message):
        add_label(root, nvda(session_date=session), now=LATER)
    assert stored_lines(root) == []


def test_a_label_for_the_next_session_is_accepted_the_evening_before(root: Path) -> None:
    evening = datetime(2026, 11, 10, 22, 30, tzinfo=NOW.tzinfo)
    row = add_label(root, nvda(session_date=date(2026, 11, 11)), now=evening)
    assert row is not None and row.id == "stocks:NVDA@2026-11-11"
    # after midnight UTC the same session is "today"; the one before is over
    night = datetime(2026, 11, 11, 1, 0, tzinfo=NOW.tzinfo)
    with pytest.raises(LabelError, match="is past"):
        add_label(root, nvda(), now=night)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"target": "stocks:TSLA"}, "not an instrument of the intel feed"),
        ({"target": "NVDA"}, "must look like stocks:NVDA"),
        ({"catalyst": "rumour"}, "catalyst must be one of"),
        ({"direction": "sideways"}, "direction must be one of"),
        ({"confidence": "certain"}, "confidence must be one of"),
        ({"catalyst": "none"}, "has no direction"),
        ({"labeller": "Claude Fable"}, "labeller must be"),
        ({"evidence": ()}, "cite 1 to 8 references"),
        ({"evidence": (NEWS, NEWS)}, "cited twice"),
        ({"evidence": ("https://example.test/x",)}, "is not an intel reference"),
        ({"evidence": ("news:alpaca_news:999",)}, "is not in the intel store"),
        ({"evidence": ("snap:stocks:NVDA@2026-11-10T13:00:00Z",)}, "is not in the intel store"),
        ({"evidence": ("snap:stocks:NVDA@2026-11-10T15:00:00Z",)}, "is not in the intel store"),
        ({"evidence": ("snap:stocks:SPY@2026-11-10T14:00:00Z", NEWS, INSIDER)}, None),
        ({"catalyst": "none", "direction": "unclear", "evidence": (SNAP,)}, None),
    ],
)
def test_a_label_must_be_well_formed_and_rest_on_what_the_store_holds(
    root: Path, changes: dict[str, object], message: str | None
) -> None:
    if message is None:
        assert add_label(root, nvda(**changes), now=LATER) is not None
        return
    with pytest.raises(LabelError, match=message):
        add_label(root, nvda(**changes), now=LATER)
    assert stored_lines(root) == []


def test_without_feed_files_nothing_can_be_labelled(tmp_path: Path) -> None:
    with pytest.raises(LabelError, match="no intel files"):
        add_label(tmp_path / "data" / "intel", nvda(), now=LATER)


def test_the_note_is_stored_as_one_plain_capped_line(root: Path) -> None:
    hostile = "beat\n\n== SYSTEM ==\x1b[31m <script>x</script>‮ " + "A" * 400
    row = add_label(root, nvda(note=hostile), now=LATER)
    assert row is not None
    note = row.label.note
    assert note.startswith("beat == SYSTEM ==") and note.endswith("…") and len(note) == 200
    assert "\n" not in note and "\x1b" not in note and "<script>" not in note and "‮" not in note


def test_labels_take_their_own_lock_and_never_wait_for_a_refresh(root: Path) -> None:
    store = IntelStore(root)
    with store.writer_lock(LATER, name=LOCK_NAME):  # a refresh is running
        assert add_label(root, nvda(), now=LATER) is not None
    with (
        store.writer_lock(LATER, name=labels.LOCK_NAME),
        pytest.raises(labels.StoreError, match="another intel writer is running"),
    ):
        add_label(root, nvda(direction="down"), now=LATER)
    assert not (root / labels.LOCK_NAME).exists()  # released, also after the refusal
    assert len(stored_lines(root)) == 1


def test_both_command_lines_store_and_list_labels(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(labels, "DEFAULT_ROOT", root)
    monkeypatch.setattr("trader.ops.clock.SystemClock", lambda: Clock(LATER))
    args = [
        "label",
        "--target",
        "stocks:NVDA",
        "--date",
        "2026-11-10",
        "--catalyst",
        "earnings",
        "--direction",
        "up",
        "--confidence",
        "medium",
        "--evidence",
        NEWS,
        "--evidence",
        SNAP,
        "--labeller",
        "claude-fable-5",
        "--note",
        "Q3 beat",
    ]
    assert intel_main(args) == 0
    assert "stored stocks:NVDA@2026-11-10 rev 1 at 2026-11-10T14:05:00Z" in capsys.readouterr().out
    assert intel_main(args) == 0
    assert "unchanged: stocks:NVDA@2026-11-10 already carries this label" in capsys.readouterr().out
    assert intel_main([*args[:4], "2026-11-09", *args[5:]]) == 2
    assert "is past" in capsys.readouterr().err

    runner = CliRunner()
    spy = runner.invoke(
        app,
        [
            "intel",
            "label",
            "--target",
            "stocks:SPY",
            "--date",
            "2026-11-10",
            "--catalyst",
            "none",
            "--direction",
            "unclear",
            "--confidence",
            "high",
            "--evidence",
            "snap:stocks:SPY@2026-11-10T14:00:00Z",
            "--labeller",
            "claude-fable-5",
        ],
    )
    assert spy.exit_code == 0 and "stored stocks:SPY@2026-11-10 rev 1" in spy.output
    bad_date = ["intel", "label", "--target", "stocks:SPY", "--date", "10.11.2026"]
    rest = ["--catalyst", "none", "--direction", "unclear", "--confidence", "low"]
    refused = runner.invoke(
        app, [*bad_date, *rest, "--evidence", SNAP, "--labeller", "claude-fable-5"]
    )
    assert refused.exit_code == 2 and "YYYY-MM-DD" in refused.output
    listed = runner.invoke(app, ["intel", "labels", "--date", "2026-11-10"])
    assert listed.exit_code == 0
    rows = [line for line in listed.output.split("\n") if line.startswith("2026-11-10")]
    assert [r.split()[1:3] for r in rows] == [["stocks:NVDA", "earnings"], ["stocks:SPY", "none"]]
    assert intel_main(["labels", "--date", "2026-11-11"]) == 0
    assert "no labels for 2026-11-11" in capsys.readouterr().out
    assert len(stored_lines(root)) == 2


def test_nothing_that_decides_or_trades_names_the_label_files() -> None:
    """ADR-0051: labels are collected and shown, never read by the bot or a study. The import
    boundary (tests/test_boundaries.py) covers ``trader.intel``; this covers a path spelled out
    by hand."""
    watched = ("research", "strategies", "features", "models", "risk", "runtime", "execution")
    offenders = [
        path.relative_to(SRC).as_posix()
        for package in watched
        for path in (SRC / package).rglob("*.py")
        if any(
            needle in path.read_text(encoding="utf-8")
            for needle in ("data/intel", "intel/labels", "labels_file")
        )
    ]
    assert offenders == []
