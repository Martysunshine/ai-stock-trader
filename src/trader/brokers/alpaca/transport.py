"""The Alpaca transport: the only module that talks to the alpaca-py SDK's REST client.

:class:`AlpacaTransport` covers exactly the calls the paper adapter uses and returns raw JSON
objects (``raw_data=True``), which :mod:`trader.brokers.alpaca.mapping` converts to domain
objects. :class:`SdkTransport` wraps ``alpaca.trading.TradingClient`` with ``paper=True`` and no
URL override: the paper endpoint is the SDK's hard-coded default, and this package never accepts
a base URL (ADR-0025). ``truststore.inject_into_ssl()`` runs once before the first client is
created so the SDK trusts the operating-system certificate store.

Outcome classification (architecture.md §7; ``execution/_gateway.py`` treats
:class:`~trader.brokers.base.BrokerTimeout` as an *unknown* outcome):

* a definite 4xx answer (400/401/403/409/422, e.g. 403 insufficient buying power or a 422
  ``40010001``-style validation error) -> :class:`DefiniteRejection` carrying status, code and
  message; 404 -> :class:`NotFound`;
* a timeout, a connection or TLS error, an unparseable body, a 408, any 5xx, or a 429 that is
  still refused after the SDK's retries -> :class:`~trader.brokers.base.BrokerTimeout`.

The SDK's automatic retry is narrowed to 429 (it also retries 504 by default; a retried
``POST /v2/orders`` after an ambiguous 504 could meet a duplicate-client-ID 422 for an order
that does exist). Every request carries a timeout; ``requests`` has none by default.
Credentials live only inside the SDK clients and are never formatted into a message or a repr.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol, runtime_checkable

import truststore
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream

from trader.brokers.alpaca.mapping import JsonObject
from trader.brokers.base import BrokerError, BrokerTimeout
from trader.data.sources.alpaca_history import AlpacaCredentials

DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
#: Response headers kept for owner reports (an allow-list: nothing else is retained).
RATE_LIMIT_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")
#: The SDK retries only rate-limit answers (see the module docstring).
SDK_RETRY_STATUS_CODES = (429,)
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9./-]{0,31}")
_UNKNOWN_STATUSES = frozenset({408, 429})

TradeHandler = Callable[[Any], Awaitable[None]]


class DefiniteRejection(BrokerError):
    """Alpaca definitely refused the request; nothing was created or changed."""

    def __init__(self, *, http_status: int, code: str, message: str) -> None:
        super().__init__(f"Alpaca refused the request (HTTP {http_status}, code {code}): {message}")
        self.http_status = http_status
        self.code = code
        self.message = message

    @property
    def is_duplicate_client_order_id(self) -> bool:
        text = self.message.lower()
        return "client_order_id" in text and "unique" in text


class NotFound(DefiniteRejection):
    """HTTP 404: the order, asset or resource does not exist (or is not visible yet)."""


class TradeStreamSession(Protocol):
    """One ``trade_updates`` websocket session (alpaca-py ``TradingStream``)."""

    def subscribe_trade_updates(self, handler: TradeHandler) -> None: ...

    def run(self) -> None: ...

    def stop(self) -> None: ...


class AlpacaTransport(Protocol):
    """Exactly the Alpaca Trading API calls the paper adapter uses (raw JSON in and out)."""

    def get_account(self) -> JsonObject: ...

    def list_positions(self) -> list[JsonObject]: ...

    def list_orders(self, *, status: str, after: str | None, limit: int) -> list[JsonObject]:
        """``GET /v2/orders`` in ascending ``submitted_at`` order (``after`` is exclusive)."""
        ...

    def get_order(self, order_id: str) -> JsonObject: ...

    def get_order_by_client_id(self, client_order_id: str) -> JsonObject | None: ...

    def submit_order(self, payload: Mapping[str, str]) -> JsonObject: ...

    def cancel_order(self, order_id: str) -> None: ...

    def list_fill_activities(
        self,
        *,
        order_id: str | None,
        direction: Literal["asc", "desc"],
        page_token: str | None,
        page_size: int,
    ) -> list[JsonObject]:
        """``GET /v2/account/activities/FILL`` (optionally one order's), paged by activity id."""
        ...

    def list_fee_activities(
        self,
        *,
        direction: Literal["asc", "desc"],
        page_token: str | None,
        page_size: int,
    ) -> list[JsonObject]:
        """``GET /v2/account/activities/FEE``, paged by activity id."""
        ...

    def get_clock(self) -> JsonObject: ...

    def get_asset(self, symbol: str) -> JsonObject: ...

    def open_trade_stream(self, on_connected: Callable[[], None]) -> TradeStreamSession: ...


@runtime_checkable
class RateLimitObserver(Protocol):
    """A transport that remembers the newest rate-limit response headers it saw."""

    def rate_limit_headers(self) -> Mapping[str, str]: ...


# --------------------------------------------------------------------------- errors


def translate_api_error(exc: APIError) -> BrokerError | BrokerTimeout:
    """Classify an SDK ``APIError`` as a definite rejection or an unknown outcome."""
    status = _status_code(exc)
    code, message = _error_body(str(exc))
    if status is None or status in _UNKNOWN_STATUSES or status >= 500 or status < 400:
        return BrokerTimeout(f"Alpaca outcome unknown (HTTP {status}, code {code}): {message}")
    if status == 404:
        return NotFound(http_status=status, code=code, message=message)
    return DefiniteRejection(http_status=status, code=code, message=message)


def _status_code(exc: APIError) -> int | None:
    try:
        status = exc.status_code
    except AttributeError:
        return None
    return status if isinstance(status, int) else None


def _error_body(text: str) -> tuple[str, str]:
    try:
        body = json.loads(text)
    except ValueError:
        return "-", text[:300]
    if not isinstance(body, dict):
        return "-", text[:300]
    return str(body.get("code", "-")), str(body.get("message", ""))[:300]


# --------------------------------------------------------------------------- SDK transport

_truststore_injected = False


def _inject_truststore() -> None:
    global _truststore_injected
    if not _truststore_injected:
        truststore.inject_into_ssl()
        _truststore_injected = True


class _ConnectionAwareStream(TradingStream):
    """``TradingStream`` that reports every successful (re)connection.

    The SDK reconnects internally and silently; events during the gap are lost, so the adapter
    re-reads open orders over REST after each connection.
    """

    def __init__(self, on_connected: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._on_connected = on_connected

    async def _start_ws(self) -> None:
        await super()._start_ws()  # type: ignore[no-untyped-call]
        self._on_connected()


class _PaperTradingClient(TradingClient):
    """``TradingClient`` whose every HTTP request carries a timeout."""

    def __init__(self, request_timeout_seconds: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._request_timeout_seconds = request_timeout_seconds

    def _one_request(self, method: str, url: str, opts: dict[str, Any], retry: int) -> Any:
        timed = {**opts, "timeout": self._request_timeout_seconds}
        return super()._one_request(method, url, timed, retry)


class SdkTransport:
    """:class:`AlpacaTransport` over alpaca-py 0.44.0 (paper endpoint only)."""

    def __init__(
        self,
        credentials: AlpacaCredentials,
        *,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        _inject_truststore()
        client = _PaperTradingClient(
            request_timeout_seconds,
            api_key=credentials.key_id,
            secret_key=credentials.secret,
            paper=True,
            raw_data=True,
        )
        client._retry_codes = list(SDK_RETRY_STATUS_CODES)
        self._rate_limit: dict[str, str] = {}
        client._session.hooks["response"].append(self._observe_headers)
        self._client = client
        self._stream_credentials = credentials

    def __repr__(self) -> str:
        return "SdkTransport(endpoint=paper)"

    # ------------------------------------------------------------------ plumbing
    def _observe_headers(self, response: Any, *args: Any, **kwargs: Any) -> Any:
        """``requests`` response hook: keep only the allow-listed rate-limit headers."""
        headers = getattr(response, "headers", None) or {}
        seen = {name: str(headers[name]) for name in RATE_LIMIT_HEADERS if name in headers}
        if seen:
            self._rate_limit = seen
        return response

    def rate_limit_headers(self) -> Mapping[str, str]:
        return dict(self._rate_limit)

    def _call(self, fn: Callable[[], object]) -> object:
        try:
            return fn()
        except APIError as exc:
            raise translate_api_error(exc) from None
        except (OSError, ValueError) as exc:
            # requests' ConnectionError, Timeout and JSON errors all derive from OSError; a
            # garbled body is a ValueError. The request may have been processed: unknown.
            raise BrokerTimeout(f"Alpaca transport error ({type(exc).__name__})") from None

    def _object(self, fn: Callable[[], object]) -> JsonObject:
        result = self._call(fn)
        if not isinstance(result, dict):
            raise BrokerTimeout(f"unexpected Alpaca response shape {type(result).__name__}")
        return result

    def _list(self, fn: Callable[[], object]) -> list[JsonObject]:
        result = self._call(fn)
        if not isinstance(result, list) or not all(isinstance(r, dict) for r in result):
            raise BrokerTimeout("unexpected Alpaca response shape (expected a list of objects)")
        return list(result)

    # ------------------------------------------------------------------ protocol
    def get_account(self) -> JsonObject:
        return self._object(lambda: self._client.get("/account"))

    def list_positions(self) -> list[JsonObject]:
        return self._list(lambda: self._client.get("/positions"))

    def list_orders(self, *, status: str, after: str | None, limit: int) -> list[JsonObject]:
        params: dict[str, object] = {"status": status, "limit": limit, "direction": "asc"}
        if after is not None:
            params["after"] = after
        return self._list(lambda: self._client.get("/orders", params))

    def get_order(self, order_id: str) -> JsonObject:
        return self._object(lambda: self._client.get(f"/orders/{order_id}"))

    def get_order_by_client_id(self, client_order_id: str) -> JsonObject | None:
        params = {"client_order_id": client_order_id}
        try:
            return self._object(lambda: self._client.get("/orders:by_client_order_id", params))
        except NotFound:
            return None

    def submit_order(self, payload: Mapping[str, str]) -> JsonObject:
        body = dict(payload)
        return self._object(lambda: self._client.post("/orders", body))

    def cancel_order(self, order_id: str) -> None:
        self._call(lambda: self._client.delete(f"/orders/{order_id}"))

    def list_fill_activities(
        self,
        *,
        order_id: str | None,
        direction: Literal["asc", "desc"],
        page_token: str | None,
        page_size: int,
    ) -> list[JsonObject]:
        params: dict[str, object] = {"direction": direction, "page_size": page_size}
        if order_id is not None:
            params["order_id"] = order_id
        if page_token is not None:
            params["page_token"] = page_token
        return self._list(lambda: self._client.get("/account/activities/FILL", params))

    def list_fee_activities(
        self,
        *,
        direction: Literal["asc", "desc"],
        page_token: str | None,
        page_size: int,
    ) -> list[JsonObject]:
        params: dict[str, object] = {"direction": direction, "page_size": page_size}
        if page_token is not None:
            params["page_token"] = page_token
        return self._list(lambda: self._client.get("/account/activities/FEE", params))

    def get_clock(self) -> JsonObject:
        return self._object(lambda: self._client.get("/clock"))

    def get_asset(self, symbol: str) -> JsonObject:
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError(f"not an Alpaca symbol: {symbol!r}")
        return self._object(lambda: self._client.get(f"/assets/{symbol}"))

    def open_trade_stream(self, on_connected: Callable[[], None]) -> TradeStreamSession:
        _inject_truststore()
        creds = self._stream_credentials
        stream: TradeStreamSession = _ConnectionAwareStream(
            on_connected,
            api_key=creds.key_id,
            secret_key=creds.secret,
            paper=True,
            raw_data=True,
        )
        return stream
