"""Loaders for capability profiles and instrument catalogues."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import TypeAdapter

from trader.config.policy import load_yaml_strict, reject_floats
from trader.domain.capabilities import BrokerCapabilities
from trader.domain.instruments import AnySpec, InstrumentSpec
from trader.domain.types import DomainError

_SPEC_LIST = TypeAdapter(list[InstrumentSpec])


def load_capabilities(path: Path) -> BrokerCapabilities:
    return BrokerCapabilities.model_validate(load_yaml_strict(path))


def load_instruments(path: Path) -> dict[str, AnySpec]:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, list):
        raise DomainError(f"{path} must contain a list of instrument specs")
    reject_floats(raw)
    specs = _SPEC_LIST.validate_python(raw)
    catalogue: dict[str, AnySpec] = {}
    for spec in specs:
        if spec.instrument_id in catalogue:
            raise DomainError(f"duplicate instrument {spec.instrument_id}")
        catalogue[spec.instrument_id] = spec
    return catalogue


__all__ = ["load_capabilities", "load_instruments", "load_yaml_strict"]
