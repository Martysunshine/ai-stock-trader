"""Export NYSE sessions from the bot's own calendar for the console (display only).

Run from the repository root with the bot's environment:

    uv run python ui/scripts/export_calendar.py --start 2026-01-01 --end 2027-11-08

``exchange_calendars`` only knows sessions about one year ahead, so pick an end date inside it.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from trader.data.calendars import ExchangeCalendar

OUT = Path(__file__).resolve().parents[1] / "shared" / "nyse-sessions.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    args = parser.parse_args()

    cal = ExchangeCalendar("XNYS")
    sessions: list[list[str | int]] = []
    day = args.start
    while day <= args.end:
        s = cal.session_for_date(day)
        if s is not None:
            sessions.append(
                [day.isoformat(), int(s.open.timestamp() * 1000), int(s.close.timestamp() * 1000)]
            )
        day += timedelta(days=1)

    doc = {
        "source": "trader.data.calendars.ExchangeCalendar(XNYS) (exchange_calendars), regular "
        "hours, display only. Re-export with ui/scripts/export_calendar.py.",
        "entry_exclusion_open_min": int(cal.entry_exclusion_open.total_seconds() // 60),
        "entry_exclusion_close_min": int(cal.entry_exclusion_close.total_seconds() // 60),
        "close_procedure_lead_min": int(cal.close_procedure_lead.total_seconds() // 60),
        "sessions": sessions,
    }
    OUT.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(sessions)} sessions to {OUT}")


if __name__ == "__main__":
    main()
