"""Minimal JSON-over-HTTPS transport for public market-data endpoints.

* JSON numbers are parsed as :class:`~decimal.Decimal` from their literal text, so a price such
  as ``34442.3`` never passes through a binary float.
* Requests are paced (a minimum interval per transport) and retried with exponential backoff on
  HTTP 429/5xx and transient network errors; anything else fails loudly.
* Credentials, when an endpoint needs them, travel only in request headers and are never
  logged, echoed in errors or written to manifests. The one exception is a login body posted by
  ``open_session`` to a ``/api/v1/session`` endpoint (Capital.com demo prices, ADR-0045), whose
  response headers carry the session tokens; neither is logged.
* ``get_bytes`` fetches a small non-JSON document (SEC Form 4 XML, ADR-0047): https only, no
  redirect followed, no content encoding accepted and a hard size cap, so a hostile or mistaken
  URL cannot pull a large or compressed payload into the process.
* ``send_json`` is the practice trader's one order call (ADR-0056): a ``POST`` that opens a
  position or a ``DELETE`` that closes one, on Capital.com's ``/api/v1/positions`` path and
  nothing else. One attempt, never retried: an order whose answer was lost must be looked up,
  not sent again.
* Tests inject a fake transport; no test touches the network.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

USER_AGENT = "ai-stock-trader-research/0.1 (public market data; no account)"
#: The one path :meth:`UrllibTransport.open_session` may post to (Capital.com's login).
SESSION_PATH = "/api/v1/session"
#: The one path :meth:`UrllibTransport.send_json` may write to (Capital.com positions, ADR-0056):
#: ``POST`` exactly this path opens one, ``DELETE`` this path plus ``/<dealId>`` closes one.
POSITIONS_PATH = "/api/v1/positions"
_DEAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class TransportError(RuntimeError):
    """A request failed. ``status`` is the HTTP status when the server answered, else ``None``."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class JsonTransport(Protocol):
    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...


class SessionTransport(JsonTransport, Protocol):
    """A JSON transport that can also log in to a session endpoint (Capital.com)."""

    def open_session(
        self,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, dict[str, str]]: ...


class OrderTransport(SessionTransport, Protocol):
    """A session transport that can also open and close a position (the practice trader)."""

    def send_json(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...


def check_order_url(method: str, url: str) -> None:
    """Refuse, before anything is sent, every write except opening a position (``POST`` exactly
    :data:`POSITIONS_PATH`) and closing one (``DELETE`` that path plus one deal id)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.query or parts.fragment:
        raise ValueError("send_json only writes to an https positions endpoint")
    if method == "POST" and parts.path == POSITIONS_PATH:
        return
    prefix = POSITIONS_PATH + "/"
    if (
        method == "DELETE"
        and parts.path.startswith(prefix)
        and _DEAL_ID.fullmatch(parts.path[len(prefix) :])
    ):
        return
    raise ValueError(f"send_json only POSTs {POSITIONS_PATH} or DELETEs {POSITIONS_PATH}/<dealId>")


class BytesTransport(Protocol):
    """Fetches one small document as raw bytes (see :meth:`UrllibTransport.get_bytes`)."""

    def get_bytes(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        max_bytes: int,
    ) -> bytes: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect: the 3xx answer then surfaces as an ``HTTPError``."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def parse_json(payload: bytes | str) -> Any:
    return json.loads(payload, parse_float=Decimal)


def build_url(url: str, params: Mapping[str, str] | None) -> str:
    if not params:
        return url
    return f"{url}?{urllib.parse.urlencode(sorted(params.items()))}"


class UrllibTransport:
    def __init__(
        self,
        *,
        min_interval_seconds: float = 0.25,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
    ) -> None:
        self._context = ssl.create_default_context()  # OS trust store (works behind AV TLS proxies)
        self._min_interval = min_interval_seconds
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._last_request = 0.0
        self.requests = 0
        self._direct = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._context), _NoRedirect()
        )

    def _open_direct(self, request: urllib.request.Request) -> Any:
        return self._direct.open(request, timeout=self._timeout)

    def get_bytes(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        max_bytes: int,
    ) -> bytes:
        """``GET`` one document as bytes, at most ``max_bytes`` long.

        https only; a redirect is refused, not followed; an encoded (compressed) answer is
        refused, so the size cap is the size in memory. Paced and retried like ``get_json``.
        """
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("get_bytes only fetches https URLs")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        full = build_url(url, params)
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        delay = 1.0
        for attempt in range(1, self._max_attempts + 1):
            self._pace()
            request = urllib.request.Request(full, headers=request_headers)
            try:
                with self._open_direct(request) as response:
                    self.requests += 1
                    encoding = (response.headers.get("Content-Encoding") or "identity").lower()
                    payload: bytes = response.read(max_bytes + 1)
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == self._max_attempts:
                    raise TransportError(f"HTTP {exc.code} from {url}", status=exc.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self._max_attempts:
                    raise TransportError(f"network error for {url}: {exc}") from None
            else:
                if encoding != "identity":
                    raise TransportError(f"encoded response ({encoding}) from {url}")
                if len(payload) > max_bytes:
                    raise TransportError(f"response from {url} exceeds {max_bytes} bytes")
                return payload
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
        raise TransportError(f"giving up on {url}")  # pragma: no cover - loop always returns

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        full = build_url(url, params)
        request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        request_headers.update(headers or {})
        delay = 1.0
        for attempt in range(1, self._max_attempts + 1):
            self._pace()
            request = urllib.request.Request(full, headers=request_headers)
            try:
                with urllib.request.urlopen(
                    request, context=self._context, timeout=self._timeout
                ) as response:
                    self.requests += 1
                    return parse_json(response.read())
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == self._max_attempts:
                    raise TransportError(f"HTTP {exc.code} from {url}", status=exc.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self._max_attempts:
                    raise TransportError(f"network error for {url}: {exc}") from None
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
        raise TransportError(f"giving up on {url}")  # pragma: no cover - loop always returns

    def open_session(
        self,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """``POST`` a JSON login body to a session endpoint; return ``(json, response headers)``.

        The only write-shaped request this transport makes, and only to a path ending exactly in
        :data:`SESSION_PATH` (a login that returns session tokens; ADR-0045). The body and the
        returned headers carry credentials: neither is logged nor echoed in an error. One attempt,
        no retry (session endpoints are rate limited). Header names come back lower-cased.
        """
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.path != SESSION_PATH or parts.query:
            raise ValueError(f"open_session only posts to an https {SESSION_PATH} endpoint")
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        request_headers.update(headers or {})
        data = json.dumps(dict(body)).encode("utf-8")
        self._pace()
        request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, context=self._context, timeout=self._timeout
            ) as response:
                self.requests += 1
                answer = {k.lower(): v for k, v in response.headers.items()}
                return parse_json(response.read()), answer
        except urllib.error.HTTPError as exc:
            raise TransportError(f"HTTP {exc.code} from {url}", status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise TransportError(f"network error for {url}: {exc}") from None

    def send_json(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Open (``POST``) or close (``DELETE``) one position; see :func:`check_order_url`.

        One attempt and no retry, whatever happens: the caller looks the order up rather than
        sending it twice. A redirect is refused. Headers carry the session tokens and are never
        logged or echoed in an error."""
        check_order_url(method, url)
        request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(dict(body), default=str).encode("utf-8")
        request_headers.update(headers or {})
        self._pace()
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with self._open_direct(request) as response:
                self.requests += 1
                return parse_json(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                answer = parse_json(exc.read() or b"{}")
                if isinstance(answer, dict) and isinstance(answer.get("errorCode"), str):
                    detail = f" ({answer['errorCode'][:120]})"
            except (ValueError, OSError):
                pass
            raise TransportError(
                f"HTTP {exc.code} from {method} {url}{detail}", status=exc.code
            ) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise TransportError(f"network error for {method} {url}: {exc}") from None

    def _pace(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"


def _os_ca_certificates() -> list[bytes]:
    """DER certificates the OS trusts for server authentication.

    On Windows the system stores are read directly, as ``ssl`` itself does when it loads them;
    this works even after ``truststore.inject_into_ssl()`` replaced ``ssl.SSLContext`` (the
    replacement cannot list its CA certificates). Elsewhere the default context is used.
    """
    enum = getattr(ssl, "enum_certificates", None)
    if enum is not None:
        found: dict[bytes, None] = {}
        for store in ("ROOT", "CA"):
            for cert, encoding, trust in enum(store):
                if encoding == "x509_asn" and (trust is True or _SERVER_AUTH in trust):
                    found[cert] = None
        return list(found)
    return list(ssl.create_default_context().get_ca_certs(binary_form=True))


def write_os_trust_store(path: Path) -> int:
    """Write the operating system's trusted CA certificates as a PEM bundle; returns the count.

    Child tools that use ``certifi`` (``requests``, ``httpx``) cannot see an antivirus HTTPS proxy's
    root certificate; pointing ``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE`` at this bundle lets them
    verify exactly what this process verifies. The bundle holds public certificates only.
    """
    certs = _os_ca_certificates()
    pem = "".join(ssl.DER_cert_to_PEM_cert(der) for der in certs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pem, encoding="ascii", newline="\n")
    return len(certs)
