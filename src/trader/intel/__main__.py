"""``python -m trader.intel refresh | watch | brief | label | labels`` (ADR-0047, ADR-0051).

Kept off the full ``trader`` CLI's import path (typer, the ledger and the database stack) so the
resident ``watch`` beside the console stays small; ``trader intel ...`` runs the same functions.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from trader.intel.brief import BriefError, run_brief
from trader.intel.config import SOURCE_IDS
from trader.intel.feed import FeedError, run_refresh, run_watch
from trader.intel.labels import (
    CATALYSTS,
    CONFIDENCES,
    DIRECTIONS,
    LabelError,
    run_label,
    run_labels,
)


def _echo(message: str) -> None:
    print(message, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trader.intel",
        description=(
            "Read news, insider filings and pre-market activity into data/intel/ "
            "(information only; nothing here can place an order)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("refresh", help="Run every due source once, then exit.")
    once.add_argument(
        "--source",
        action="append",
        choices=SOURCE_IDS,
        help="Run exactly this source now, due or not (repeatable).",
    )
    sub.add_parser("watch", help="Refresh now and then at each source's cadence until stopped.")
    brief = sub.add_parser("brief", help="Print what the feed knows now, per instrument.")
    brief.add_argument("--symbols", help="Comma-separated symbols or ids (default: all).")
    brief.add_argument("--limit", type=int, default=5, help="Headlines per instrument (1-30).")
    label = sub.add_parser(
        "label", help="Record why an instrument is moving, before its session (catalyst label)."
    )
    label.add_argument("--target", required=True, help="Instrument id, e.g. stocks:NVDA.")
    label.add_argument("--date", required=True, help="Session date YYYY-MM-DD (today or ahead).")
    label.add_argument("--catalyst", required=True, choices=CATALYSTS)
    label.add_argument("--direction", required=True, choices=DIRECTIONS)
    label.add_argument("--confidence", required=True, choices=CONFIDENCES)
    label.add_argument(
        "--evidence",
        action="append",
        required=True,
        help="A reference the brief printed, e.g. news:alpaca_news:63087291 (repeatable).",
    )
    label.add_argument("--labeller", required=True, help="Who judged, e.g. claude-fable-5.")
    label.add_argument("--note", default="", help="One line: what the label rests on.")
    listed = sub.add_parser("labels", help="List the stored catalyst labels.")
    listed.add_argument("--date", help="Only this session (YYYY-MM-DD).")
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            return run_refresh(only=args.source, echo=_echo)
        if args.command == "brief":
            return run_brief(symbols=args.symbols, limit=args.limit, echo=_echo)
        if args.command == "label":
            return run_label(
                target=args.target,
                session=args.date,
                catalyst=args.catalyst,
                direction=args.direction,
                confidence=args.confidence,
                evidence=args.evidence,
                labeller=args.labeller,
                note=args.note,
                echo=_echo,
            )
        if args.command == "labels":
            return run_labels(session=args.date, echo=_echo)
        return run_watch(echo=_echo)
    except (FeedError, BriefError, LabelError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
