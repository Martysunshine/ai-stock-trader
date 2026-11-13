"""The byte fetch added for SEC Form 4 documents (ADR-0047): https only, no redirect followed,
no encoded answer accepted, a hard size cap. No test touches the network."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

from trader.data.sources import http
from trader.data.sources.http import TransportError, UrllibTransport, _NoRedirect

URL = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000001/form4.xml"


class FakeResponse:
    def __init__(self, body: bytes, encoding: str | None = None) -> None:
        self._body = io.BytesIO(body)
        self.headers = Message()
        if encoding is not None:
            self.headers["Content-Encoding"] = encoding

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "status", Message(), None)


def transport_with(
    monkeypatch: pytest.MonkeyPatch, answers: list[Any]
) -> tuple[UrllibTransport, list[urllib.request.Request]]:
    """A transport whose direct opener replays ``answers`` (a response or an exception each)."""
    monkeypatch.setattr(http.time, "sleep", lambda _seconds: None)
    transport = UrllibTransport(min_interval_seconds=0.0, max_attempts=3)
    seen: list[urllib.request.Request] = []

    def open_direct(request: urllib.request.Request) -> Any:
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(transport, "_open_direct", open_direct)
    return transport, seen


def test_get_bytes_returns_the_document_and_sends_plain_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, seen = transport_with(monkeypatch, [FakeResponse(b"<ownershipDocument/>")])
    body = transport.get_bytes(URL, headers={"User-Agent": "trader contact"}, max_bytes=100)
    assert body == b"<ownershipDocument/>"
    assert transport.requests == 1 and len(seen) == 1
    sent = {k.lower(): v for k, v in seen[0].header_items()}
    assert sent["user-agent"] == "trader contact"
    assert sent["accept-encoding"] == "identity"


def test_get_bytes_refuses_a_document_over_the_cap_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, seen = transport_with(monkeypatch, [FakeResponse(b"x" * 101)])
    with pytest.raises(TransportError, match="exceeds 100 bytes"):
        transport.get_bytes(URL, max_bytes=100)
    assert len(seen) == 1


def test_get_bytes_accepts_a_document_exactly_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, _ = transport_with(monkeypatch, [FakeResponse(b"x" * 100)])
    assert len(transport.get_bytes(URL, max_bytes=100)) == 100


def test_get_bytes_refuses_plain_http_and_a_silly_cap_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, seen = transport_with(monkeypatch, [])
    with pytest.raises(ValueError, match="https"):
        transport.get_bytes("http://www.sec.gov/x.xml", max_bytes=100)
    with pytest.raises(ValueError, match="positive"):
        transport.get_bytes(URL, max_bytes=0)
    assert seen == [] and transport.requests == 0


def test_get_bytes_refuses_an_encoded_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, seen = transport_with(monkeypatch, [FakeResponse(b"\x1f\x8b", encoding="gzip")])
    with pytest.raises(TransportError, match="encoded response"):
        transport.get_bytes(URL, max_bytes=100)
    assert len(seen) == 1


def test_a_redirect_is_an_error_and_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, seen = transport_with(monkeypatch, [http_error(302)])
    with pytest.raises(TransportError) as caught:
        transport.get_bytes(URL, max_bytes=100)
    assert caught.value.status == 302 and len(seen) == 1
    # The handler that makes urllib raise instead of following the redirect.
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere") is None
    handlers = [type(h) for h in UrllibTransport()._direct.handlers]
    assert _NoRedirect in handlers
    assert urllib.request.HTTPRedirectHandler not in handlers


def test_get_bytes_retries_rate_limits_and_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, seen = transport_with(
        monkeypatch, [http_error(429), http_error(503), FakeResponse(b"ok")]
    )
    assert transport.get_bytes(URL, max_bytes=100) == b"ok"
    assert len(seen) == 3


def test_get_bytes_gives_up_after_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, seen = transport_with(
        monkeypatch, [urllib.error.URLError("down"), TimeoutError(), ConnectionError("reset")]
    )
    with pytest.raises(TransportError, match="network error"):
        transport.get_bytes(URL, max_bytes=100)
    assert len(seen) == 3


def test_a_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, seen = transport_with(monkeypatch, [http_error(403)])
    with pytest.raises(TransportError) as caught:
        transport.get_bytes(URL, max_bytes=100)
    assert caught.value.status == 403 and len(seen) == 1
