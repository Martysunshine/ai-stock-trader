"""``python -m trader.market refresh | watch --every N`` (ADR-0045).

Kept off the full ``trader`` CLI's import path (typer, the ledger and the database stack) so the
resident ``watch`` beside the console stays small; ``trader market ...`` runs the same functions.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from trader.market.feed import FeedError, run_refresh, run_watch


def _echo(message: str) -> None:
    print(message, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trader.market",
        description="Fetch the dashboard watchlists into data/market/ (prices only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("refresh", help="Refresh every group once, then exit.")
    once.add_argument("--group", action="append", help="Only this group (repeatable).")
    watch = sub.add_parser("watch", help="Refresh now and then every N minutes until stopped.")
    watch.add_argument("--every", type=int, default=5, help="Minutes between refreshes (1-60).")
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            return run_refresh(groups=args.group, echo=_echo)
        return run_watch(every_minutes=args.every, echo=_echo)
    except FeedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
