"""Capital.com DEMO price history: mid bars for the dashboard's market feed (ADR-0045) and
two-sided bid/ask bars for research ingest (ADR-0052).

Read only: the client logs in and reads ``/prices``; it has no order, position or account call.
The practice trader's order client (``trader.practice.capital_orders``, ADR-0056) shares its
logged-in session through :meth:`CapitalPrices.call`; the orders live there, not here.

* **Host.** The demo host is hard-coded (:data:`CAPITAL_DEMO_HOST`); no setting, argument or
  environment variable can point it elsewhere, and ``tests/test_capital_prices.py`` fails if the
  live host's name appears anywhere under ``src/``.
* **Login.** ``POST /api/v1/session`` with header ``X-CAP-API-KEY`` and the JSON body
  ``{identifier, password, encryptedPassword: false}``. The session tokens come back as the
  ``CST`` and ``X-SECURITY-TOKEN`` response headers; they are kept in this object's memory only
  (never logged, written or put in an error) and sent as headers on each price request. Capital.com
  allows one session request per second, so logins are paced to at least
  :data:`LOGIN_INTERVAL_SECONDS` apart.
* **Prices.** ``GET /api/v1/prices/{epic}?resolution=MINUTE_5|HOUR|DAY&max=N[&to=...]`` returns
  ``{"prices": [{"snapshotTime", "snapshotTimeUTC", "openPrice": {"bid", "ask"}, "closePrice",
  "highPrice", "lowPrice", "lastTradedVolume"}, ...], "instrumentType", "tickSize",
  "pipPosition"}``, oldest first, at most 1000 rows; ``to`` (UTC, ``YYYY-MM-DDTHH:MM:SS``) pages
  backwards. Checked against the demo host on 2026-11-09: DAY candles start at 00:00 UTC, HOUR and
  MINUTE_5 candles on the UTC clock grid, and the last row is the bar still forming.
* **Mid prices.** Each OHLC field is the mid of its bid and ask, ``(bid + ask) / 2`` in exact
  decimals (basis ``mid``). A row missing either side is skipped, never half-filled.
  ``lastTradedVolume`` is a tick count, not traded volume, so bars carry volume 0.
* **Expired or refused sessions.** A 401 on a price request logs in again once and retries; a
  second 401 (or a 401 at login) stops all requests for :data:`AUTH_BACKOFF` and raises
  :class:`CapitalAuthError` with the time requests may resume.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from trader.data.history import DataQualityError, HistoryBar
from trader.data.sources.http import SESSION_PATH, SessionTransport, TransportError
from trader.domain.types import iso_utc, require_utc

if TYPE_CHECKING:
    from trader.config.secrets import CapitalDemoCredentials


class Clock(Protocol):
    """An injected clock (``trader.ops.clock.SystemClock`` in use; data/ may not import ops)."""

    def now(self) -> datetime: ...


#: The only host this module talks to: Capital.com's DEMO environment.
CAPITAL_DEMO_HOST = "https://demo-api-capital.backend-capital.com"
SESSION_URL = CAPITAL_DEMO_HOST + SESSION_PATH
PRICES_URL = f"{CAPITAL_DEMO_HOST}/api/v1/prices"
#: Bar minutes -> Capital.com resolution.
RESOLUTIONS = {5: "MINUTE_5", 60: "HOUR", 1440: "DAY"}
MAX_BARS = 1000
LOGIN_INTERVAL_SECONDS = 1.0
AUTH_BACKOFF = timedelta(minutes=30)
_EPIC = re.compile(r"^[A-Z0-9_.]{1,40}$")
_FIELDS = (
    ("open", "openPrice"),
    ("high", "highPrice"),
    ("low", "lowPrice"),
    ("close", "closePrice"),
)


class CapitalAuthError(TransportError):
    """Capital.com refused the session; no request is made before ``retry_after``."""

    def __init__(self, message: str, *, retry_after: datetime) -> None:
        super().__init__(message, status=401)
        self.retry_after = retry_after


def _num(value: object) -> Decimal:
    if isinstance(value, (bool, float)):
        raise DataQualityError("Capital.com number arrived as a binary float or boolean")
    if isinstance(value, (Decimal, int, str)):
        return Decimal(value)
    raise DataQualityError(f"unexpected numeric value {value!r}")


def _side(field: object, side: str) -> Decimal | None:
    if not isinstance(field, dict):
        return None
    value = field.get(side)
    return None if value is None else _num(value)


def capital_time(value: object) -> datetime:
    """``snapshotTimeUTC`` (naive ISO text in UTC) as an aware UTC datetime."""
    if not isinstance(value, str):
        raise DataQualityError(f"unexpected Capital.com timestamp {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return require_utc(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))


def parse_mid_bars(payload: Any, *, minutes: int) -> list[HistoryBar]:
    """Mid-price bars from a ``/prices`` payload, oldest first; one-sided rows are skipped."""
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise DataQualityError(f"unexpected Capital.com prices payload: {str(payload)[:200]}")
    out: list[HistoryBar] = []
    for row in payload["prices"]:
        if not isinstance(row, dict):
            raise DataQualityError(f"malformed Capital.com price row {row!r}")
        mids: dict[str, Decimal] = {}
        for name, key in _FIELDS:
            bid, ask = _side(row.get(key), "bid"), _side(row.get(key), "ask")
            if bid is None or ask is None:
                break
            mids[name] = (bid + ask) / 2
        if len(mids) != len(_FIELDS):
            continue
        out.append(
            HistoryBar(
                start=capital_time(row.get("snapshotTimeUTC")),
                minutes=minutes,
                open=mids["open"],
                high=mids["high"],
                low=mids["low"],
                close=mids["close"],
                volume=Decimal(0),
            )
        )
    out.sort(key=lambda b: b.start)
    return out


@dataclass(frozen=True, slots=True)
class QuotePage:
    """One ``/prices`` answer as two-sided bars, oldest first: ``bid[i]`` and ``ask[i]`` are the
    same bar. ``one_sided`` counts rows left out because a side was missing, ``crossed`` rows left
    out because a field's ask was below its bid."""

    bid: tuple[HistoryBar, ...]
    ask: tuple[HistoryBar, ...]
    one_sided: int = 0
    crossed: int = 0


def parse_quote_bars(payload: Any, *, minutes: int) -> QuotePage:
    """Bid bars and ask bars from a ``/prices`` payload (research ingest, ADR-0052).

    Each side keeps the source's own open, high, low and close. A row missing either side of any
    field is left out, as is a row whose ask is below its bid: neither is a quote one could have
    dealt on. ``lastTradedVolume`` is the source's tick count for the bar, stored on both sides
    as ``volume``; it is not traded volume and nothing sizes an order by it."""
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise DataQualityError(f"unexpected Capital.com prices payload: {str(payload)[:200]}")
    rows: list[tuple[HistoryBar, HistoryBar]] = []
    one_sided = crossed = 0
    for row in payload["prices"]:
        if not isinstance(row, dict):
            raise DataQualityError(f"malformed Capital.com price row {row!r}")
        sides: dict[str, dict[str, Decimal]] = {"bid": {}, "ask": {}}
        for name, key in _FIELDS:
            bid, ask = _side(row.get(key), "bid"), _side(row.get(key), "ask")
            if bid is None or ask is None:
                break
            sides["bid"][name], sides["ask"][name] = bid, ask
        if len(sides["bid"]) != len(_FIELDS):
            one_sided += 1
            continue
        if any(sides["ask"][name] < sides["bid"][name] for name, _ in _FIELDS):
            crossed += 1
            continue
        start = capital_time(row.get("snapshotTimeUTC"))
        ticks = row.get("lastTradedVolume")
        volume = Decimal(0) if ticks is None else _num(ticks)
        pair = tuple(
            HistoryBar(
                start=start,
                minutes=minutes,
                open=sides[side]["open"],
                high=sides[side]["high"],
                low=sides[side]["low"],
                close=sides[side]["close"],
                volume=volume,
            )
            for side in ("bid", "ask")
        )
        rows.append((pair[0], pair[1]))
    rows.sort(key=lambda r: r[0].start)
    return QuotePage(
        bid=tuple(r[0] for r in rows),
        ask=tuple(r[1] for r in rows),
        one_sided=one_sided,
        crossed=crossed,
    )


def _capital_stamp(ts: datetime) -> str:
    """The ``to``/``from`` format: UTC, seconds, no zone suffix."""
    return require_utc(ts).strftime("%Y-%m-%dT%H:%M:%S")


class CapitalPrices:
    """A logged-in reader of Capital.com DEMO prices. One instance per process."""

    def __init__(
        self,
        transport: SessionTransport,
        credentials: Callable[[], CapitalDemoCredentials],
        *,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        backoff_until: datetime | None = None,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._tokens: dict[str, str] | None = None
        self._last_login: float | None = None
        self._backoff_until = backoff_until
        self.logins = 0

    @property
    def backoff_until(self) -> datetime | None:
        """While set and in the future, no request is made (after a refused session)."""
        return self._backoff_until

    def fetch_bars(
        self, epic: str, *, minutes: int, max_bars: int = MAX_BARS, to: datetime | None = None
    ) -> list[HistoryBar]:
        """The newest ``max_bars`` mid bars of ``epic`` (ending at ``to`` when given)."""
        return parse_mid_bars(
            self.fetch_payload(epic, minutes=minutes, max_bars=max_bars, to=to), minutes=minutes
        )

    def fetch_quote_bars(
        self, epic: str, *, minutes: int, max_bars: int = MAX_BARS, to: datetime | None = None
    ) -> QuotePage:
        """The newest ``max_bars`` two-sided bars of ``epic`` (ending at ``to`` when given)."""
        return parse_quote_bars(
            self.fetch_payload(epic, minutes=minutes, max_bars=max_bars, to=to), minutes=minutes
        )

    def fetch_payload(
        self, epic: str, *, minutes: int, max_bars: int = MAX_BARS, to: datetime | None = None
    ) -> Any:
        """One ``/prices`` answer as the source gave it (numbers already exact decimals)."""
        if not _EPIC.match(epic):
            raise ValueError(f"not a Capital.com epic: {epic!r}")
        if minutes not in RESOLUTIONS:
            raise ValueError(f"Capital.com has no {minutes}-minute resolution here")
        params = {"resolution": RESOLUTIONS[minutes], "max": str(max(1, min(MAX_BARS, max_bars)))}
        if to is not None:
            params["to"] = _capital_stamp(to)
        url = f"{PRICES_URL}/{epic}"
        return self.call(lambda headers: self._transport.get_json(url, params, headers))

    def call(self, request: Callable[[dict[str, str]], Any], what: str = "price request") -> Any:
        """Run ``request`` with the session's headers: log in first when needed, and once more
        after a 401 (an expired session; a request refused for authentication was not carried
        out, so sending it again is safe). A second 401 backs off as a refused session."""
        self._check_backoff()
        if self._tokens is None:
            self._login()
        try:
            return request(self._headers())
        except TransportError as exc:
            if exc.status != 401:
                raise
            self._tokens = None  # expired or revoked: log in again, once
            self._login()
            try:
                return request(self._headers())
            except TransportError as again:
                if again.status != 401:
                    raise
                raise self._refused(f"{what} refused after a fresh login") from None

    # ------------------------------------------------------------------ internals
    def _headers(self) -> dict[str, str]:
        if self._tokens is None:  # pragma: no cover - _login always sets them or raises
            raise CapitalAuthError("no session", retry_after=self._clock.now())
        return dict(self._tokens)

    def _check_backoff(self) -> None:
        until = self._backoff_until
        if until is not None:
            if self._clock.now() < until:
                raise CapitalAuthError(
                    f"Capital.com refused the session; backing off until {iso_utc(until)}",
                    retry_after=until,
                )
            self._backoff_until = None

    def _refused(self, what: str) -> CapitalAuthError:
        self._tokens = None
        until = self._clock.now() + AUTH_BACKOFF
        self._backoff_until = until
        return CapitalAuthError(
            f"Capital.com {what} (HTTP 401); backing off until {iso_utc(until)}",
            retry_after=until,
        )

    def _login(self) -> None:
        if self._last_login is not None:
            wait = LOGIN_INTERVAL_SECONDS - (self._monotonic() - self._last_login)
            if wait > 0:
                self._sleep(wait)
        creds = self._credentials()
        self._last_login = self._monotonic()
        self.logins += 1
        try:
            _, headers = self._transport.open_session(
                SESSION_URL,
                {
                    "identifier": creds.identifier,
                    "password": creds.password,
                    "encryptedPassword": False,
                },
                {"X-CAP-API-KEY": creds.api_key},
            )
        except TransportError as exc:
            if exc.status == 401:
                raise self._refused("login refused") from None
            raise
        lower = {k.lower(): v for k, v in headers.items()}
        cst, token = lower.get("cst"), lower.get("x-security-token")
        if not cst or not token:
            raise DataQualityError("Capital.com session answered without CST/X-SECURITY-TOKEN")
        self._tokens = {"CST": cst, "X-SECURITY-TOKEN": token}
