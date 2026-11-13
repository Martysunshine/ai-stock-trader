"""Conservative, timestamped currency conversion (CRYPTO §3, COMMODITIES §5).

Assets are converted at the bid of ``CCY/USD`` and costs/liabilities at the ask, so conversion
never flatters equity or understates risk. A stale or missing quote is an error, never a
silent fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from trader.domain.market import FxQuote
from trader.domain.types import ONE, DomainError, require_utc


class StaleFxError(DomainError):
    pass


class FxBook:
    def __init__(self, base_currency: str = "USD", max_age: timedelta = timedelta(seconds=5)):
        self.base_currency = base_currency
        self.max_age = max_age
        self._quotes: dict[str, FxQuote] = {}

    def update(self, quote: FxQuote) -> None:
        if quote.quote != self.base_currency:
            raise DomainError(f"FX quotes must be quoted in {self.base_currency}")
        self._quotes[quote.base] = quote

    def _quote(self, currency: str, now: datetime) -> FxQuote:
        quote = self._quotes.get(currency)
        if quote is None:
            raise StaleFxError(f"no FX quote for {currency}/{self.base_currency}")
        if require_utc(now) - quote.receipt_time > self.max_age:
            raise StaleFxError(
                f"FX quote {currency}/{self.base_currency} is older than {self.max_age}"
            )
        return quote

    def asset_rate(self, currency: str, now: datetime) -> Decimal:
        """Rate for valuing an asset held in ``currency`` (bid side)."""
        if currency == self.base_currency:
            return ONE
        return self._quote(currency, now).bid

    def cost_rate(self, currency: str, now: datetime) -> Decimal:
        """Rate for valuing a cost or risk amount in ``currency`` (ask side)."""
        if currency == self.base_currency:
            return ONE
        return self._quote(currency, now).ask
