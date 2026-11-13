"""Markdown helpers shared by the generated research reports (implementation_roadmap.md §2).

The baseline report (``research/report.py``) and the model report (``research/model_report.py``)
format numbers, intervals and tables the same way. The helpers moved here unchanged from
``report.py``, so the published crypto baseline report re-renders byte for byte.

:func:`report_file_name` names a study's report: ``<study_id>_report.md``, except for the
studies that produce the roadmap's named artifacts (``baseline_report.md`` for the first crypto
baseline, which was published under that name before reports were per study, and
``model_report.md`` for the first model study).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: Studies whose report is a roadmap artifact with a fixed name (implementation_roadmap.md §2).
ROADMAP_REPORT_NAMES = {
    "crypto_baseline_v1": "baseline_report.md",
    "crypto_filter_v1": "model_report.md",
}


def report_file_name(study_id: str) -> str:
    """``<study_id>_report.md``, or the roadmap artifact name for the studies listed above."""
    return ROADMAP_REPORT_NAMES.get(study_id, f"{study_id}_report.md")


def fmt(value: Any, digits: int = 4) -> str:
    """A number for a report cell: ``n/a`` for missing or NaN, yes/no for booleans."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:  # NaN
            return "n/a"
        if value in (float("inf"), float("-inf")):
            return "∞" if value > 0 else "-∞"
        return f"{value:.{digits}f}"
    return str(value)


def pct(value: Any) -> str:
    """A fraction as a percentage with one decimal."""
    return "n/a" if value is None else f"{100 * float(value):.1f}%"


def ci(interval: Mapping[str, Any] | None, digits: int = 4) -> str:
    """A bootstrap interval: estimate [lower, upper]; one-sided 95% lower bound."""
    if not interval:
        return "n/a"
    return (
        f"{fmt(interval['estimate'], digits)} [{fmt(interval['lower'], digits)}, "
        f"{fmt(interval['upper'], digits)}]; "
        f"one-sided 95% lower {fmt(interval['one_sided_lower'], digits)}"
    )


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """A GitHub-flavoured Markdown table as lines."""
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out
