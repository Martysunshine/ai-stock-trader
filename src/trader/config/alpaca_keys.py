"""Alpaca key-pair names and the credentials value (ADR-0026, ADR-0038).

Standard library only, so the key store and the MCP launcher can use them without importing the
market-data stack (calendars, pandas, numpy). ``trader.data.sources.alpaca_history`` re-exports
these names for its existing callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

KEY_ENV = "APCA_API_KEY_ID"
SECRET_ENV = "APCA_API_SECRET_KEY"


@dataclass(frozen=True, slots=True)
class AlpacaCredentials:
    key_id: str = field(repr=False)
    secret: str = field(repr=False)

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret}

    def __repr__(self) -> str:
        return "AlpacaCredentials(<redacted>)"
