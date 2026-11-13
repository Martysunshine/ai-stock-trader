"""Rule-family registry: a frozen specification's ``family`` -> its rule factory.

``research/study.py`` resolves every trial's specification through this registry
(risk_and_validation.md §5: a trial is a frozen specification plus its parameters). The two
streaming families resolve exactly as before the registry existed: the same specification
classes, the same ``streaming-v1`` version check and the same rule classes, so their spec hashes
and registrations are unchanged. A runtime Candidate A specification has no ``family`` key and
is resolved by the study itself (``CandidateASpec``).

Adding a family (e.g. a daily rule) needs no change elsewhere:

* define, in the family's own module, a module-level ``RULE_FAMILY = RuleFamily(...)``, and add
  one line ``"<FAMILY>": "<module path>"`` to :data:`FAMILY_MODULES` (imported lazily on first
  use, so the family module may import anything a strategy may import); or
* call :func:`register` (tests use this with :func:`unregister`).

A family's specification object must be frozen and expose ``strategy_id``, ``version``,
``family``, ``feature_version``, a ``spec_hash`` property and pydantic's ``model_dump``. Optional
attributes the research engine reads from the rule or its ``spec`` (see
``research/engine.py``): ``holding`` (``"INTRADAY"`` or ``"OVERNIGHT"``), ``max_hold_sessions``,
``target_role`` (``"EXIT"`` or ``"COST_GATE"``), ``use_source_vwap``; and a rule may implement
``exit_signal(instrument_id, bar)``.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.config.policy import load_yaml_strict
from trader.data.calendars import ExchangeCalendar, Utc247Calendar
from trader.features.streaming import FEATURE_VERSION as STREAMING_VERSION
from trader.strategies.research_rules import (
    MeanReversion,
    MeanReversionSpec,
    TrendBreakout,
    TrendBreakoutSpec,
)

AnyCalendar = ExchangeCalendar | Utc247Calendar
#: (frozen specification, calendar, price ticks by instrument) -> a research rule object.
RuleBuilder = Callable[[Any, AnyCalendar, Mapping[str, Decimal]], Any]

#: The registration's description of the features a streaming rule uses (unchanged text).
STREAMING_FEATURES: tuple[str, ...] = (
    "reference (rolling VW typical price or session VWAP)",
    "ATR",
    "long ATR",
    "efficiency ratio",
    "prior high",
    "context return",
)


class UnknownRuleFamilyError(ValueError):
    """A specification names a family that no factory is registered for."""


@dataclass(frozen=True, slots=True)
class RuleFamily:
    """How to validate a family's specification and build its rule."""

    family: str
    #: Report label, e.g. "Candidate C (dip-buy in an uptrend)".
    label: str
    #: Validates the raw YAML mapping (overrides already merged) into a frozen specification;
    #: raises ``ValueError`` (pydantic's ``ValidationError`` is one) when it does not conform.
    parse: Callable[[dict[str, Any]], Any]
    build: RuleBuilder
    #: The feature description bound into each registration.
    features: tuple[str, ...] = STREAMING_FEATURES
    #: Optional: the description for one specification, when it depends on the specification
    #: (e.g. its calendar); ``None`` = ``features`` for every specification of the family.
    features_of: Callable[[Any], tuple[str, ...]] | None = None


def _streaming_parser(model: type[TrendBreakoutSpec | MeanReversionSpec]) -> Callable[..., Any]:
    def parse(raw: dict[str, Any]) -> TrendBreakoutSpec | MeanReversionSpec:
        spec = model.model_validate(raw)
        if spec.feature_version != STREAMING_VERSION:
            raise ValueError("rule specification and feature implementation versions differ")
        return spec

    return parse


def _build_trend(spec: Any, calendar: AnyCalendar, ticks: Mapping[str, Decimal]) -> Any:
    return TrendBreakout(spec, calendar, ticks)


def _build_mean_reversion(spec: Any, calendar: AnyCalendar, ticks: Mapping[str, Decimal]) -> Any:
    return MeanReversion(spec, calendar, ticks)


_FAMILIES: dict[str, RuleFamily] = {
    "CANDIDATE_A": RuleFamily(
        family="CANDIDATE_A",
        label="Candidate A (trend continuation)",
        parse=_streaming_parser(TrendBreakoutSpec),
        build=_build_trend,
    ),
    "CANDIDATE_B": RuleFamily(
        family="CANDIDATE_B",
        label="Candidate B (mean reversion)",
        parse=_streaming_parser(MeanReversionSpec),
        build=_build_mean_reversion,
    ),
}

#: Families defined in their own modules, imported on first use. Each module defines
#: ``RULE_FAMILY: RuleFamily`` for exactly this family name.
FAMILY_MODULES: dict[str, str] = {
    "CANDIDATE_C": "trader.strategies.dip_buy",
    "CANDIDATE_D": "trader.strategies.trend_timing",
}

_BUILTIN = frozenset(_FAMILIES)


def register(entry: RuleFamily, *, replace: bool = False) -> None:
    """Add a family; a built-in family can never be replaced."""
    if entry.family in _BUILTIN:
        raise ValueError(f"{entry.family} is a built-in family and cannot be replaced")
    if entry.family in _FAMILIES and not replace:
        raise ValueError(f"rule family {entry.family} is already registered")
    _FAMILIES[entry.family] = entry


def unregister(family: str) -> None:
    """Remove a family added with :func:`register` (built-in families stay)."""
    if family in _BUILTIN:
        raise ValueError(f"{family} is a built-in family")
    _FAMILIES.pop(family, None)


def rule_family(family: str) -> RuleFamily:
    entry = _FAMILIES.get(family)
    if entry is None and family in FAMILY_MODULES:
        module = importlib.import_module(FAMILY_MODULES[family])
        candidate = getattr(module, "RULE_FAMILY", None)
        if not isinstance(candidate, RuleFamily) or candidate.family != family:
            raise UnknownRuleFamilyError(
                f"{FAMILY_MODULES[family]} does not define RULE_FAMILY for {family}"
            )
        _FAMILIES[family] = entry = candidate
    if entry is None:
        raise UnknownRuleFamilyError(f"unknown rule family {family!r}")
    return entry


def families() -> tuple[str, ...]:
    """Every family that can be resolved (registered or lazily importable)."""
    return tuple(sorted(set(_FAMILIES) | set(FAMILY_MODULES)))


def spec_features(spec: Any) -> tuple[str, ...]:
    """The feature description a registration binds for ``spec`` (its family's ``features``
    unless the family describes each specification itself)."""
    entry = rule_family(spec.family)
    return entry.features if entry.features_of is None else entry.features_of(spec)


def family_label(family: str) -> str:
    """The report label of a family, or the family name when it is not resolvable."""
    try:
        return rule_family(family).label
    except UnknownRuleFamilyError:
        return family


def load_spec(path: Path, overrides: Mapping[str, object] | None = None) -> Any:
    """Load a frozen rule specification by its ``family`` (overrides merged first)."""
    raw = load_yaml_strict(path)
    if overrides:
        raw = {**raw, **overrides}
    family = raw.get("family")
    if not isinstance(family, str):
        raise UnknownRuleFamilyError(f"unknown rule family {family!r} in {path}")
    try:
        entry = rule_family(family)
    except UnknownRuleFamilyError as exc:
        raise UnknownRuleFamilyError(f"unknown rule family {family!r} in {path}") from exc
    return entry.parse(raw)


def build(spec: Any, calendar: AnyCalendar, ticks: Mapping[str, Decimal] | None = None) -> Any:
    """Build the research rule of a specification resolved through this registry."""
    return rule_family(spec.family).build(spec, calendar, dict(ticks or {}))
