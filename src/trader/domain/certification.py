"""Paper certification grants (architecture.md §4 evidence levels; implementation_roadmap.md M4).

``trader paper certify`` demonstrates broker capabilities that a capability profile still lists
as UNKNOWN, by placing a handful of tiny orders on the owner's *paper* account. Those orders take
the ordinary route - risk engine, ``risk/approval.py``, outbox, ``execution/_gateway.py`` - and
what lets them past the checks an ordinary run applies (a capability that is not yet usable, an
order with no validated edge) is a :class:`CertificationGrant`:

* it exists only in paper mode: constructing one for any other mode raises;
* every order is bounded to :data:`CERTIFICATION_MAX_ORDER_NOTIONAL_USD` (USD 5) and the whole
  run to :data:`CERTIFICATION_MAX_ORDERS` broker submissions. These are hard ceilings: a grant
  may lower them, never raise them;
* it names the capabilities under test (a subset of :data:`CERTIFIABLE_CAPABILITIES`);
* it expires after at most :data:`CERTIFICATION_MAX_DURATION`.

Only the certify command creates a grant, so an ordinary paper run never holds one and
``EdgeEvidence.CERTIFICATION`` is never accepted there (``risk/engine.py``). Paper evidence never
certifies live availability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from trader.domain.capabilities import FRACTIONAL_LIMIT_DAY, FRACTIONAL_MARKET_DAY
from trader.domain.types import DomainError, Mode, require_utc

#: Hard per-order ceiling: the notional bound of one certification order, in USD.
CERTIFICATION_MAX_ORDER_NOTIONAL_USD: Final = Decimal("5")
#: Hard per-run ceiling on broker submissions (every order the run sends, its exits included).
CERTIFICATION_MAX_ORDERS: Final = 6
#: A grant expires at the latest this long after it was issued.
CERTIFICATION_MAX_DURATION: Final = timedelta(minutes=30)
#: Capabilities a certification run may exercise although the profile does not make them usable.
CERTIFIABLE_CAPABILITIES: Final = frozenset({FRACTIONAL_LIMIT_DAY, FRACTIONAL_MARKET_DAY})
#: Prefix of the model hash every certification plan carries (``ApprovedPlan.model_hash``).
CERTIFICATION_MODEL_PREFIX: Final = "certification:"
ZERO_USD: Final = Decimal(0)
_RUN_ID = re.compile(r"^certification-\d{8}T\d{6}Z$")


class CertificationError(DomainError):
    """A certification grant or budget was used outside its narrow scope."""


def certification_run_id(started_at: datetime) -> str:
    """``certification-YYYYMMDDTHHMMSSZ``: the run's UTC start, also the report's file name."""
    return f"certification-{require_utc(started_at):%Y%m%dT%H%M%SZ}"


@dataclass(frozen=True, slots=True)
class CertificationGrant:
    """Permission for one paper certification run (see the module docstring)."""

    run_id: str
    account_id: str
    mode: Mode
    issued_at: datetime
    expires_at: datetime
    max_orders: int = CERTIFICATION_MAX_ORDERS
    max_order_notional_usd: Decimal = CERTIFICATION_MAX_ORDER_NOTIONAL_USD
    capabilities_under_test: frozenset[str] = CERTIFIABLE_CAPABILITIES

    def __post_init__(self) -> None:
        if self.mode is not Mode.PAPER:
            raise CertificationError(
                f"certification orders exist only in paper mode, not {self.mode.value}"
            )
        if not _RUN_ID.fullmatch(self.run_id):
            raise CertificationError(f"malformed certification run id {self.run_id!r}")
        if not self.account_id:
            raise CertificationError("a certification grant names its account")
        issued, expires = require_utc(self.issued_at), require_utc(self.expires_at)
        if not issued < expires <= issued + CERTIFICATION_MAX_DURATION:
            raise CertificationError(
                f"a certification grant expires after its issue and within "
                f"{CERTIFICATION_MAX_DURATION}"
            )
        if not 0 < self.max_orders <= CERTIFICATION_MAX_ORDERS:
            raise CertificationError(
                f"max_orders must be 1..{CERTIFICATION_MAX_ORDERS}, not {self.max_orders}"
            )
        if not ZERO_USD < self.max_order_notional_usd <= CERTIFICATION_MAX_ORDER_NOTIONAL_USD:
            raise CertificationError(
                f"max_order_notional_usd must be in (0, {CERTIFICATION_MAX_ORDER_NOTIONAL_USD}], "
                f"not {self.max_order_notional_usd}"
            )
        extra = self.capabilities_under_test - CERTIFIABLE_CAPABILITIES
        if extra:
            raise CertificationError(f"capabilities not certifiable in paper: {sorted(extra)}")

    @property
    def model_hash(self) -> str:
        """The ``model_hash`` of this run's approved plans (``ApprovedPlan.model_hash``)."""
        return f"{CERTIFICATION_MODEL_PREFIX}{self.run_id}"

    def is_active(self, now: datetime) -> bool:
        return self.issued_at <= require_utc(now) < self.expires_at

    def covers(self, model_hash: str) -> bool:
        """Whether a plan with this ``model_hash`` was approved for this run."""
        return model_hash == self.model_hash


@dataclass
class CertificationBudget:
    """Counts the broker submissions of one certification run.

    Shared by every adapter instance of the run (the duplicate-ID probe uses a second one); a
    submission beyond ``grant.max_orders`` is refused before it is sent.
    """

    grant: CertificationGrant
    sent: list[str] = field(default_factory=list)

    @property
    def used(self) -> int:
        return len(self.sent)

    @property
    def remaining(self) -> int:
        return max(self.grant.max_orders - self.used, 0)

    def consume(self, client_order_id: str) -> bool:
        """Count one submission about to be sent; ``False`` (send nothing) when exhausted."""
        if self.used >= self.grant.max_orders:
            return False
        self.sent.append(client_order_id)
        return True


@dataclass(frozen=True, slots=True)
class AssetMetadata:
    """A broker's asset record for one instrument (fetched, never assumed)."""

    instrument_id: str
    symbol: str
    broker_asset_id: str
    asset_class: str
    exchange: str
    status: str
    tradable: bool
    fractionable: bool
    marginable: bool
    shortable: bool

    def certification_problems(self) -> list[str]:
        """Why this asset cannot carry a fractional certification order (empty = it can)."""
        problems = []
        if self.status != "active":
            problems.append(f"{self.symbol} is {self.status}, not active")
        if not self.tradable:
            problems.append(f"{self.symbol} is not tradable")
        if not self.fractionable:
            problems.append(f"{self.symbol} is not fractionable")
        if self.asset_class != "us_equity":
            problems.append(f"{self.symbol} is {self.asset_class}, not us_equity")
        return problems


__all__ = [
    "CERTIFIABLE_CAPABILITIES",
    "CERTIFICATION_MAX_DURATION",
    "CERTIFICATION_MAX_ORDERS",
    "CERTIFICATION_MAX_ORDER_NOTIONAL_USD",
    "CERTIFICATION_MODEL_PREFIX",
    "AssetMetadata",
    "CertificationBudget",
    "CertificationError",
    "CertificationGrant",
    "certification_run_id",
]
