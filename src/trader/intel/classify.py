"""Headline categories by fixed keyword rules (ADR-0047).

No model reads the text. A headline gets the first category whose words appear in it, in the
order below, and ``unknown`` when none do; ``tags`` lists every category that matched. The rules
are deliberately plain: they are the deterministic baseline the strategy specification asks for
before any language model may be considered (strategy_and_profitability.md section 7).

Checked against a day of live headlines on 2026-11-10: about four in five are general market
commentary and come out ``unknown``, which is the honest answer for them. Phrases that proved
ambiguous there are left out on purpose ("to buy" also means a rating, "stake in" a fund's
holding, "fine" also starts "fine-tuning", "forecast" also means an economist's).

Changing a rule changes what the same headline is called, so bump ``EXTRACTOR_VERSION`` with it;
every stored record carries the version that named it.
"""

from __future__ import annotations

import re

EXTRACTOR_VERSION = "keywords-v1"
UNKNOWN = "unknown"

#: Category -> phrases, matched on word boundaries in the lower-cased title and summary.
#: Order matters: the first category that matches names the headline. Analyst comes before
#: guidance so that "analyst forecasts" is not read as the company's own outlook.
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "earnings",
        (
            "earnings",
            "quarterly results",
            "q1 results",
            "q2 results",
            "q3 results",
            "q4 results",
            "eps",
            "revenue beat",
            "revenue miss",
            "beats estimates",
            "misses estimates",
            "reports results",
        ),
    ),
    (
        "analyst",
        (
            "analyst",
            "analysts",
            "price target",
            "upgrade",
            "upgrades",
            "upgraded",
            "downgrade",
            "downgrades",
            "downgraded",
            "initiates coverage",
            "overweight",
            "underweight",
            "outperform",
            "underperform",
        ),
    ),
    (
        "guidance",
        (
            "guidance",
            "outlook",
            "raises forecast",
            "cuts forecast",
            "lowers forecast",
            "full-year forecast",
            "profit warning",
        ),
    ),
    (
        "financing",
        (
            "offering",
            "private placement",
            "convertible notes",
            "senior notes",
            "share sale",
            "buyback",
            "repurchase",
            "dividend",
            "stock split",
        ),
    ),
    (
        "mna",
        (
            "acquire",
            "acquires",
            "acquisition",
            "merger",
            "merge",
            "takeover",
            "buyout",
            "divest",
            "divestiture",
            "spin-off",
            "spinoff",
        ),
    ),
    (
        "legal_regulatory",
        (
            "lawsuit",
            "sues",
            "sued",
            "settlement",
            "antitrust",
            "investigation",
            "probe",
            "subpoena",
            "sec charges",
            "regulator",
            "regulators",
            "fined",
            "fines",
            "recall",
            "export controls",
            "sanctions",
            "ban",
        ),
    ),
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (category, re.compile(r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, words)) + r")(?![a-z0-9])"))
    for category, words in RULES
)
CATEGORIES: tuple[str, ...] = (*(category for category, _ in RULES), UNKNOWN)


def classify(title: str, summary: str = "") -> tuple[str, tuple[str, ...]]:
    """``(category, tags)`` for a headline: the first matching category and all that matched."""
    text = f"{title} {summary}".lower()
    tags = tuple(category for category, pattern in _PATTERNS if pattern.search(text))
    return (tags[0] if tags else UNKNOWN), tags
