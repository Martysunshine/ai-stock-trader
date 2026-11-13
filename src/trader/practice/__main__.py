"""``python -m trader.practice scan | watch --every N`` (ADR-0049), ``certify`` and
``trade [--live]`` (ADR-0056).

Kept off the full ``trader`` CLI's import path (typer, the ledger and the database stack) so the
resident ``watch`` beside the console stays small; ``trader journal scan`` and
``trader journal watch`` run the same functions.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from trader.ops.clock import SystemClock
from trader.practice import setups
from trader.practice.journal import JournalError
from trader.practice.watch import run_scan, run_watch


def _echo(message: str) -> None:
    print(message, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trader.practice",
        description="Read the practice setups and open trades into runs/practice/scan.json. "
        "Places and journals nothing.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("scan", help="Scan once, print it, then exit.")
    watch = sub.add_parser("watch", help="Scan now and then every N minutes until stopped.")
    watch.add_argument("--every", type=int, default=15, help="Minutes between scans (1-240).")
    for command in (once, watch):
        command.add_argument("--market-root", type=Path, help="The market feed's folder.")
    sub.add_parser(
        "certify",
        help="One minimum-size Bitcoin round trip on the demo account (ADR-0056).",
    )
    trade = sub.add_parser(
        "trade",
        help="Run the practice setups on the demo account (ADR-0056). A dry run unless --live.",
    )
    trade.add_argument("--live", action="store_true", help="Journal and send demo orders.")
    trade.add_argument("--setup", help="Run only this setup key.")
    sub.add_parser(
        "forward",
        help="Each setup's demo trades against its trial; writes runs/practice/forward.json.",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "forward":
            from trader.practice.forward import run_forward

            return run_forward(SystemClock().now(), _echo)
        if args.command == "trade":
            from trader.practice.trader import run_trader  # the order path loads only here

            return run_trader(live=args.live, only=args.setup, echo=_echo)
        if args.command == "certify":
            from trader.practice.certify import run_certify  # the order path loads only here

            return run_certify(_echo)
        if args.command == "scan":
            for line in setups.render(run_scan(args.market_root, SystemClock().now())):
                _echo(line)
            return 0
        return run_watch(every_minutes=args.every, market_root=args.market_root, echo=_echo)
    except (setups.SetupError, JournalError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
