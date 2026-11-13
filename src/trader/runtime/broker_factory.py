"""Constructs the paper broker adapter for the command line (ADR-0025).

The Alpaca adapter (``trader.brokers.alpaca.adapter``) imports the broker SDK, so it is imported
lazily, only when a paper command actually needs a broker. A build without the adapter fails
with a clear message instead of an import error, and nothing is started. The adapter refuses
anything but paper itself; this factory refuses first, from the configuration.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from trader.brokers.catalog import load_capabilities, load_instruments
from trader.config.runtime_config import AccountConfig, ResolvedConfig
from trader.data.sources.alpaca_history import AlpacaCredentials
from trader.domain.certification import CertificationBudget
from trader.domain.instruments import AnySpec
from trader.domain.types import DomainError, Mode
from trader.ops.clock import Clock
from trader.runtime.paper import PaperBroker

ALPACA_ADAPTER_MODULE = "trader.brokers.alpaca.adapter"
ALPACA_ADAPTER_CLASS = "AlpacaPaperAdapter"
#: Configuration ``broker`` values this factory can build.
PAPER_BROKERS = frozenset({"alpaca_paper"})


class AdapterUnavailable(DomainError):
    """No usable paper adapter in this build or for this configuration; nothing was started."""


def alpaca_adapter_class() -> Any:
    """The adapter class, imported on first use (raises :class:`AdapterUnavailable`)."""
    try:
        module = importlib.import_module(ALPACA_ADAPTER_MODULE)
    except ImportError as exc:
        raise AdapterUnavailable(
            f"the Alpaca paper adapter ({ALPACA_ADAPTER_MODULE}) is not available in this build: "
            f"{exc}. Nothing was started."
        ) from exc
    adapter_cls = getattr(module, ALPACA_ADAPTER_CLASS, None)
    if adapter_cls is None:
        raise AdapterUnavailable(f"{ALPACA_ADAPTER_MODULE} has no {ALPACA_ADAPTER_CLASS}")
    return adapter_cls


def paper_adapter(
    config: ResolvedConfig,
    account: AccountConfig,
    *,
    credentials: AlpacaCredentials,
    clock: Clock,
    since: datetime,
    instruments: Mapping[str, AnySpec] | None = None,
    certification: CertificationBudget | None = None,
) -> PaperBroker:
    """Build the paper adapter for ``account``; refuses anything but a paper configuration.

    ``instruments`` replaces the configured specs (the certification run passes the one it
    verified against the broker's asset record); ``certification`` is passed only by
    ``trader paper certify`` (domain/certification.py).
    """
    if config.runtime.mode is not Mode.PAPER:
        raise AdapterUnavailable(
            f"no paper adapter for a {config.runtime.mode.value} configuration"
        )
    if account.broker not in PAPER_BROKERS:
        raise AdapterUnavailable(
            f"broker {account.broker!r} has no paper adapter; known: {sorted(PAPER_BROKERS)}"
        )
    capabilities = load_capabilities(config.resolve(account.capability_profile))
    if capabilities.environment != "paper":
        raise AdapterUnavailable(
            f"capability profile {capabilities.profile_version} is {capabilities.environment}, "
            "not paper"
        )
    adapter_cls = alpaca_adapter_class()
    specs = (
        dict(instruments)
        if instruments is not None
        else load_instruments(config.resolve(account.instruments))
    )
    extra: dict[str, object] = {} if certification is None else {"certification": certification}
    adapter = adapter_cls(
        account_id=account.account_id,
        credentials=credentials,
        capabilities=capabilities,
        instruments=specs,
        clock=clock,
        mode=Mode.PAPER,
        since=since,
        **extra,
    )
    return cast(PaperBroker, adapter)


__all__ = [
    "ALPACA_ADAPTER_MODULE",
    "AdapterUnavailable",
    "alpaca_adapter_class",
    "paper_adapter",
]
