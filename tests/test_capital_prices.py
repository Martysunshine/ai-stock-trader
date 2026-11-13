"""Capital.com DEMO prices for the market feed (ADR-0045): demo host only, tokens in memory only,
mid prices, one re-login after a 401, then a 30-minute back-off. No test touches the network."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader.config import secrets
from trader.config.secrets import CapitalDemoCredentials, SecretsError, require_capital_demo
from trader.data.history import DataQualityError
from trader.data.sources.capital_prices import (
    AUTH_BACKOFF,
    CAPITAL_DEMO_HOST,
    LOGIN_INTERVAL_SECONDS,
    PRICES_URL,
    SESSION_URL,
    CapitalAuthError,
    CapitalPrices,
    parse_mid_bars,
)
from trader.data.sources.http import (
    SESSION_PATH,
    TransportError,
    UrllibTransport,
    parse_json,
)

SRC = Path(__file__).resolve().parents[1] / "src"
NOW = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)
CREDS = CapitalDemoCredentials("demo-api-key-never-printed", "owner@mailbox.test", " pass word ")


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.t = now

    def now(self) -> datetime:
        return self.t


def price_row(
    t: str, o: tuple[str, str], h: tuple[str, str], lo: tuple[str, str], c: tuple[str, str]
) -> str:
    def side(p: tuple[str, str]) -> str:
        return f'{{"bid": {p[0]}, "ask": {p[1]}}}'

    return (
        f'{{"snapshotTime": "{t}", "snapshotTimeUTC": "{t}", "openPrice": {side(o)}, '
        f'"highPrice": {side(h)}, "lowPrice": {side(lo)}, "closePrice": {side(c)}, '
        f'"lastTradedVolume": 923}}'
    )


#: Shaped like the demo host's answer on 2026-11-09 (numbers parsed as Decimal, as the transport does).
PRICES = parse_json(
    '{"prices": ['
    + price_row(
        "2026-11-09T17:50:00",
        ("4103.33", "4103.83"),
        ("4103.56", "4104.06"),
        ("4100.65", "4101.15"),
        ("4101.81", "4102.31"),
    )
    + ","
    + price_row(
        "2026-11-09T17:55:00",
        ("4101.79", "4102.29"),
        ("4102.92", "4103.42"),
        ("4101.67", "4102.17"),
        ("4101.96", "4102.47"),
    )
    + ',{"snapshotTimeUTC": "2026-11-09T18:00:00", "openPrice": {"bid": 4101.97, "ask": null},'
    ' "highPrice": {"bid": 4104.1, "ask": 4104.6}, "lowPrice": {"bid": 4101.77, "ask": 4102.27},'
    ' "closePrice": {"bid": 4103.39, "ask": 4103.79}, "lastTradedVolume": 538}'
    '], "instrumentType": "COMMODITIES", "tickSize": 0.01, "pipPosition": 2}'
)


class FakeCapital:
    """Records every call; ``price_status`` answers price requests with that HTTP error."""

    def __init__(self) -> None:
        self.sessions: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.gets: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.price_status: list[int | None] = []  # consumed per GET; None = success
        self.login_status: int | None = None

    def open_session(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> tuple[Any, dict[str, str]]:
        self.sessions.append((url, dict(body), dict(headers or {})))
        if self.login_status is not None:
            raise TransportError(f"HTTP {self.login_status} from {url}", status=self.login_status)
        n = len(self.sessions)
        return {"accountType": "CFD"}, {"CST": f"cst-{n}", "X-SECURITY-TOKEN": f"xst-{n}"}

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.gets.append((url, dict(params or {}), dict(headers or {})))
        status = self.price_status.pop(0) if self.price_status else None
        if status is not None:
            raise TransportError(f"HTTP {status} from {url}", status=status)
        return PRICES


def make(fake: FakeCapital, clock: Clock | None = None) -> tuple[CapitalPrices, list[float]]:
    slept: list[float] = []
    ticks = iter(float(i) * 0.1 for i in range(1000))
    prices = CapitalPrices(
        fake,
        lambda: CREDS,
        clock=clock or Clock(),
        monotonic=lambda: next(ticks),
        sleep=slept.append,
    )
    return prices, slept


def test_login_posts_the_demo_session_with_the_api_key_header() -> None:
    fake = FakeCapital()
    prices, _ = make(fake)
    prices.fetch_bars("GOLD", minutes=5, max_bars=3)
    [(url, body, headers)] = fake.sessions
    assert url == SESSION_URL == "https://demo-api-capital.backend-capital.com/api/v1/session"
    assert body == {
        "identifier": "owner@mailbox.test",
        "password": " pass word ",  # byte-exact
        "encryptedPassword": False,
    }
    assert headers == {"X-CAP-API-KEY": "demo-api-key-never-printed"}


def test_price_requests_carry_the_session_tokens_and_resolution() -> None:
    fake = FakeCapital()
    prices, _ = make(fake)
    prices.fetch_bars("GOLD", minutes=5, max_bars=3)
    prices.fetch_bars("US500", minutes=60, max_bars=5000, to=datetime(2026, 11, 5, tzinfo=UTC))
    prices.fetch_bars("EURUSD", minutes=1440)
    (u1, p1, h1), (u2, p2, h2), (u3, p3, _) = fake.gets
    assert u1 == f"{PRICES_URL}/GOLD" and p1 == {"resolution": "MINUTE_5", "max": "3"}
    assert h1 == h2 == {"CST": "cst-1", "X-SECURITY-TOKEN": "xst-1"}  # one login, reused
    assert u2.endswith("/US500") and p2 == {
        "resolution": "HOUR",
        "max": "1000",  # capped at the source's maximum
        "to": "2026-11-05T00:00:00",  # UTC, no zone suffix
    }
    assert u3.endswith("/EURUSD") and p3 == {"resolution": "DAY", "max": "1000"}
    assert len(fake.sessions) == 1
    with pytest.raises(ValueError, match="epic"):
        prices.fetch_bars("../positions", minutes=5)
    with pytest.raises(ValueError, match="resolution"):
        prices.fetch_bars("GOLD", minutes=15)


def test_bars_are_exact_bid_ask_mids_without_volume() -> None:
    bars = parse_mid_bars(PRICES, minutes=5)
    assert len(bars) == 2  # the one-sided row is skipped, never half-filled
    first, second = bars
    assert first.start == datetime(2026, 11, 9, 17, 50, tzinfo=UTC)
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("4103.58"),
        Decimal("4103.81"),
        Decimal("4100.90"),
        Decimal("4102.06"),
    )
    assert second.close == Decimal("4102.215")  # an odd spread keeps its half cent
    assert first.volume == second.volume == 0  # lastTradedVolume is a tick count
    with pytest.raises(DataQualityError, match=r"unexpected Capital\.com prices payload"):
        parse_mid_bars({"errorCode": "x"}, minutes=5)


def test_a_401_logs_in_again_once_and_retries() -> None:
    fake = FakeCapital()
    prices, slept = make(fake)
    prices.fetch_bars("GOLD", minutes=5)
    fake.price_status = [401]  # the session expired
    bars = prices.fetch_bars("GOLD", minutes=5)
    assert len(bars) == 2
    assert prices.logins == 2 and len(fake.sessions) == 2
    assert fake.gets[-1][2] == {"CST": "cst-2", "X-SECURITY-TOKEN": "xst-2"}
    assert slept and all(s <= LOGIN_INTERVAL_SECONDS for s in slept)  # logins paced 1/s
    assert prices.backoff_until is None


def test_a_second_401_backs_off_for_30_minutes() -> None:
    fake = FakeCapital()
    clock = Clock()
    prices, _ = make(fake, clock)
    fake.price_status = [401, 401]
    with pytest.raises(CapitalAuthError) as caught:
        prices.fetch_bars("GOLD", minutes=5)
    assert caught.value.retry_after == NOW + AUTH_BACKOFF == NOW + timedelta(minutes=30)
    assert caught.value.status == 401
    assert len(fake.sessions) == 2 and len(fake.gets) == 2  # one re-login, no more
    assert "cst-" not in str(caught.value) and "demo-api-key" not in str(caught.value)
    clock.t = NOW + timedelta(minutes=29)
    with pytest.raises(CapitalAuthError, match="backing off until"):
        prices.fetch_bars("GOLD", minutes=5)
    assert len(fake.sessions) == 2 and len(fake.gets) == 2  # nothing was sent
    clock.t = NOW + timedelta(minutes=31)
    assert len(prices.fetch_bars("GOLD", minutes=5)) == 2
    assert len(fake.sessions) == 3 and prices.backoff_until is None


def test_a_refused_login_backs_off_without_retrying() -> None:
    fake = FakeCapital()
    fake.login_status = 401
    prices, _ = make(fake)
    with pytest.raises(CapitalAuthError, match="login refused"):
        prices.fetch_bars("GOLD", minutes=5)
    assert len(fake.sessions) == 1 and fake.gets == []
    assert prices.backoff_until == NOW + AUTH_BACKOFF


def test_other_errors_pass_through_without_a_new_login() -> None:
    fake = FakeCapital()
    prices, _ = make(fake)
    fake.price_status = [404]
    with pytest.raises(TransportError) as caught:
        prices.fetch_bars("NOPE", minutes=5)
    assert caught.value.status == 404 and len(fake.sessions) == 1


def test_the_host_is_the_demo_host_and_the_live_host_appears_nowhere_in_src() -> None:
    assert CAPITAL_DEMO_HOST == "https://demo-api-capital.backend-capital.com"
    live = re.compile(r"(?<!demo-)api-capital\.backend-capital\.com")
    offenders = [
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".yaml", ".yml", ".json", ".toml", ".txt", ".md"}
        and live.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert offenders == []


def test_open_session_refuses_any_other_path_before_sending() -> None:
    transport = UrllibTransport()
    for url in (
        f"{CAPITAL_DEMO_HOST}/api/v1/positions",
        f"{CAPITAL_DEMO_HOST}/api/v1/session/encryptionKey",
        f"{CAPITAL_DEMO_HOST}{SESSION_PATH}?x=1",
        f"http://demo-api-capital.backend-capital.com{SESSION_PATH}",
    ):
        with pytest.raises(ValueError, match="only posts"):
            transport.open_session(url, {"identifier": "x"})
    assert transport.requests == 0


def test_transport_errors_carry_an_optional_status() -> None:
    assert TransportError("x").status is None
    assert TransportError("x", status=401).status == 401


# ----------------------------------------------------------------------------- credentials
@pytest.fixture
def ring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[tuple[str, str], str]:
    """An in-memory keyring and a temporary .env (never the developer's real ones)."""
    store: dict[tuple[str, str], str] = {}

    def get(service: str, user: str) -> str | None:
        return store.get((service, user))

    monkeypatch.setattr(secrets, "_get", get)
    monkeypatch.setattr(secrets, "DOTENV_PATH", tmp_path / "repo.env")
    return store


def test_require_capital_demo_reads_dotenv_then_keyring_and_is_redacted(
    ring: dict[tuple[str, str], str],
) -> None:
    secrets.DOTENV_PATH.write_text(
        "CAP_API_KEY=key-from-dotenv\nCAP_IDENTIFIER=owner@mailbox.test\n", encoding="utf-8"
    )
    ring[("ai-stock-trader/capital-demo", "api_password")] = " exact pass "
    creds = require_capital_demo()
    assert (creds.api_key, creds.identifier, creds.password) == (
        "key-from-dotenv",
        "owner@mailbox.test",
        " exact pass ",
    )
    assert repr(creds) == "CapitalDemoCredentials(<redacted>)"
    assert "key-from-dotenv" not in f"{creds!r} {creds}"


def test_require_capital_demo_fails_closed_naming_the_field_only(
    ring: dict[tuple[str, str], str],
) -> None:
    secrets.DOTENV_PATH.write_text("CAP_API_KEY=key-from-dotenv\n", encoding="utf-8")
    with pytest.raises(SecretsError, match="identifier missing") as caught:
        require_capital_demo()
    assert "key-from-dotenv" not in str(caught.value)
