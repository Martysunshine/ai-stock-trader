"""The intel feed's two new source readers (ADR-0047): Alpaca news and SEC EDGAR Form 4.

Every reader runs against a fake transport with synthetic content; no test touches the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from trader.config.alpaca_keys import AlpacaCredentials
from trader.data.history import DataQualityError
from trader.data.sources.alpaca_news import ALPACA_NEWS, fetch_news, parse_news_item
from trader.data.sources.sec_edgar import (
    EdgarError,
    FilingRef,
    fetch_form4,
    fetch_recent_filings,
    parse_form4,
    user_agent,
)

CREDS = AlpacaCredentials("PKTESTKEYID0000000000", "secret-never-printed")
START = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
END = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
UA = user_agent("owner@mailbox.test")


# ----------------------------------------------------------------------------- Alpaca news
def news_row(item_id: int, minute: int, **over: Any) -> dict[str, Any]:
    stamp = f"2026-11-10T13:{minute:02d}:00Z"
    row: dict[str, Any] = {
        "id": item_id,
        "headline": f"Synthetic headline {item_id}",
        "summary": "A synthetic summary.",
        "author": "Test Writer",
        "source": "benzinga",
        "url": f"https://www.benzinga.com/news/{item_id}",
        "symbols": ["NVDA", "AAPL"],
        "created_at": stamp,
        "updated_at": stamp,
        "content": "<p>An article body that must never be kept.</p>",
        "images": [{"size": "large", "url": "https://cdn.benzinga.com/x.jpg"}],
    }
    row.update(over)
    return row


class FakeNews:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[tuple[dict[str, str], dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        assert url == ALPACA_NEWS == "https://data.alpaca.markets/v1beta1/news"
        self.calls.append((dict(params or {}), dict(headers or {})))
        return self.pages[len(self.calls) - 1]


def test_news_pages_are_followed_and_the_body_is_never_requested_or_kept() -> None:
    fake = FakeNews(
        [
            {"news": [news_row(1, 1), news_row(2, 2)], "next_page_token": "abc"},
            {"news": [news_row(3, 3)], "next_page_token": None},
        ]
    )
    items = fetch_news(fake, CREDS, ["NVDA", "AAPL"], start=START, end=END)
    assert [i.source_id for i in items] == ["1", "2", "3"]
    first, second = fake.calls
    assert first[0] == {
        "symbols": "NVDA,AAPL",
        "start": "2026-11-10T09:00:00Z",
        "end": "2026-11-10T14:00:00Z",
        "limit": "50",
        "sort": "asc",
        "include_content": "false",
    }
    assert second[0]["page_token"] == "abc"
    assert first[1] == CREDS.headers()
    item = items[0]
    assert item.symbols == ("NVDA", "AAPL") and item.source == "benzinga"
    assert item.created_at == datetime(2026, 11, 10, 13, 1, tzinfo=UTC)
    assert not hasattr(item, "content") and "article body" not in repr(item)


def test_news_stops_at_the_page_limit_and_skips_an_empty_symbol_list() -> None:
    fake = FakeNews([{"news": [news_row(n, n)], "next_page_token": "more"} for n in range(1, 6)])
    items = fetch_news(fake, CREDS, ["NVDA"], start=START, end=END, max_pages=2)
    assert len(items) == 2 and len(fake.calls) == 2
    assert fetch_news(FakeNews([]), CREDS, [], start=START, end=END) == []
    with pytest.raises(ValueError, match="limit"):
        fetch_news(FakeNews([]), CREDS, ["NVDA"], start=START, end=END, limit=51)


def test_a_news_item_without_an_update_time_uses_its_creation_time() -> None:
    item = parse_news_item(news_row(9, 5, updated_at=None, author=None, symbols=None))
    assert item.updated_at == item.created_at and item.author == "" and item.symbols == ()


@pytest.mark.parametrize(
    "bad",
    [
        "not a row",
        news_row(1, 1, id=None),
        news_row(1, 1, id=True),
        news_row(1, 1, headline=["a", "list"]),
        news_row(1, 1, symbols="NVDA"),
        news_row(1, 1, symbols=["NVDA", 7]),
        news_row(1, 1, created_at=None),
        news_row(1, 1, created_at="2026-11-10T13:01:00"),
    ],
)
def test_a_malformed_news_row_is_refused(bad: object) -> None:
    with pytest.raises(ValueError):  # DataQualityError or the naive-time DomainError
        parse_news_item(bad)


def test_an_unexpected_news_payload_is_refused_without_naming_a_key() -> None:
    for payload in (["a list"], {"news": "text"}):
        with pytest.raises(DataQualityError) as caught:
            fetch_news(FakeNews([payload]), CREDS, ["NVDA"], start=START, end=END)  # type: ignore[list-item]
        assert CREDS.secret not in str(caught.value) and CREDS.key_id not in str(caught.value)


# ----------------------------------------------------------------------------- SEC EDGAR
def submissions(**over: Any) -> dict[str, Any]:
    recent: dict[str, list[Any]] = {
        "accessionNumber": [
            "0001197647-26-000013",
            "0001045810-26-000101",
            "0001197647-26-000011",
            "0001696841-26-000014",
            "0001197647-26-000002",
        ],
        "filingDate": ["2026-11-06", "2026-11-05", "2026-10-09", "2026-09-23", "2026-06-01"],
        "acceptanceDateTime": [
            "2026-11-06T21:12:02.000Z",
            "2026-11-05T21:30:00.000Z",
            "2026-10-09T21:04:09.000Z",
            "2026-09-23T20:56:28.000Z",
            "2026-06-01T20:00:00.000Z",
        ],
        "form": ["4", "8-K", "4/A", "4", "4"],
        "primaryDocument": [
            "xslF345X06/wk-form4_1793999520.xml",
            "nvda-20261105.htm",
            "xslF345X06/wk-form4a_1791925767.xml",
            "",
            "xslF345X06/wk-form4_old.xml",
        ],
    }
    payload: dict[str, Any] = {
        "cik": "1045810",
        "name": "NVIDIA CORP",
        "tickers": ["NVDA"],
        "filings": {"recent": recent, "files": []},
    }
    payload.update(over)
    return payload


class FakeEdgarJson:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        assert params is None
        self.calls.append((url, dict(headers or {})))
        return self.payload


def test_recent_form4_filings_are_listed_oldest_first_with_raw_xml_names() -> None:
    fake = FakeEdgarJson(submissions())
    refs = fetch_recent_filings(fake, UA, 1045810, "NVDA", since=date(2026, 9, 1))
    assert fake.calls == [
        ("https://data.sec.gov/submissions/CIK0001045810.json", {"User-Agent": UA})
    ]
    # The 8-K is not a Form 4, the September filing has no XML document, June is before `since`.
    assert [(r.form, r.accession) for r in refs] == [
        ("4/A", "0001197647-26-000011"),
        ("4", "0001197647-26-000013"),
    ]
    latest = refs[-1]
    assert latest.primary_document == "wk-form4_1793999520.xml"
    assert latest.accepted_at == datetime(2026, 11, 6, 21, 12, 2, tzinfo=UTC)
    assert latest.filed == date(2026, 11, 6)
    assert latest.url == (
        "https://www.sec.gov/Archives/edgar/data/1045810/000119764726000013/wk-form4_1793999520.xml"
    )
    assert latest.index_url.endswith("/000119764726000013/0001197647-26-000013-index.htm")


def test_a_company_record_for_another_ticker_is_refused() -> None:
    with pytest.raises(EdgarError, match="does not list the ticker AAPL"):
        fetch_recent_filings(FakeEdgarJson(submissions()), UA, 1045810, "AAPL", since=date.min)
    with pytest.raises(ValueError, match="positive"):
        fetch_recent_filings(FakeEdgarJson(submissions()), UA, 0, "NVDA", since=date.min)


@pytest.mark.parametrize(
    "payload",
    [
        ["a list"],
        submissions(filings=None),
        submissions(filings={"recent": {"form": "4"}}),
        submissions(filings={"recent": {"form": ["4"], "accessionNumber": []}}),
    ],
)
def test_an_unexpected_submissions_payload_is_refused(payload: Any) -> None:
    with pytest.raises(EdgarError):
        fetch_recent_filings(FakeEdgarJson(payload), UA, 1045810, "NVDA", since=date.min)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("accessionNumber", "../../../etc/passwd"),
        ("accessionNumber", "0001197647-26-0000131"),
        ("filingDate", "yesterday"),
        ("acceptanceDateTime", "2026-11-06 21:12"),
    ],
)
def test_a_filing_row_that_does_not_match_its_pattern_is_refused(column: str, value: str) -> None:
    payload = submissions()
    payload["filings"]["recent"][column][0] = value
    with pytest.raises(EdgarError):
        fetch_recent_filings(FakeEdgarJson(payload), UA, 1045810, "NVDA", since=date.min)


@pytest.mark.parametrize(
    "document",
    ["../secret.xml", "xslF345X06/../../x.xml", "a/b.xml", "form4.html", "https://evil.test/x.xml"],
)
def test_a_document_name_that_could_leave_the_filing_folder_is_left_out(document: str) -> None:
    payload = submissions()
    payload["filings"]["recent"]["primaryDocument"][0] = document
    refs = fetch_recent_filings(
        FakeEdgarJson(payload), UA, 1045810, "NVDA", since=date(2026, 11, 1)
    )
    assert refs == []


FORM4_XML = b"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0609</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-11-04</periodOfReport>
  <issuer>
    <issuerCik>0001045810</issuerCik>
    <issuerName>NVIDIA CORP</issuerName>
    <issuerTradingSymbol>nvda</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000000001</rptOwnerCik>
      <rptOwnerName>EXAMPLE   PERSON</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>true</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <isOther>0</isOther>
      <officerTitle>Chief Example Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>1</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-11-04</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>176.52</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-11-04</value></transactionDate>
      <transactionCoding><transactionCode>G</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500000</value></transactionShares>
        <transactionPricePerShare><footnoteId id="F2"/></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeHolding>
      <securityTitle><value>Common Stock</value></securityTitle>
    </nonDerivativeHolding>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle><value>Stock Option</value></securityTitle>
      <transactionDate><value>2026-11-03</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>250.5</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
</ownershipDocument>
"""


def test_a_form4_is_read_into_owners_and_transactions() -> None:
    form = parse_form4(FORM4_XML)
    assert form.issuer_cik == 1045810 and form.issuer_symbol == "NVDA"
    assert form.period == date(2026, 11, 4) and not form.amendment
    assert form.planned_10b5_1 is True
    (owner,) = form.owners
    assert owner.name == "EXAMPLE PERSON"
    assert owner.role == "officer (Chief Example Officer), director"
    buy, gift, option = form.transactions
    assert (buy.code, buy.acquired, buy.shares, buy.price) == (
        "P",
        True,
        Decimal("1000"),
        Decimal("176.52"),
    )
    assert buy.on == date(2026, 11, 4) and buy.security == "Common Stock" and not buy.derivative
    assert (gift.code, gift.acquired, gift.price) == ("G", False, None)
    assert option.derivative and option.shares == Decimal("250.5") and option.price == Decimal(0)


def test_an_amendment_and_an_older_form_without_the_plan_box() -> None:
    older = FORM4_XML.replace(
        b"<documentType>4</documentType>", b"<documentType>4/A</documentType>"
    )
    older = older.replace(b"<aff10b5One>1</aff10b5One>", b"")
    form = parse_form4(older)
    assert form.amendment and form.planned_10b5_1 is None


@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ownershipDocument/>',
        b'<?xml version="1.0"?><!doctype x><ownershipDocument/>',
        b"<ownershipDocument><!ENTITY a 'b'></ownershipDocument>",
        b'<?xml version="1.0" encoding="cp037"?><ownershipDocument/>',
        b'<?xml version="1.0" encoding="UTF-7"?><ownershipDocument/>',
        "<ownershipDocument/>".encode("utf-16"),
        b"<ownershipDocument>\x00</ownershipDocument>",
        b"<ownershipDocument><unclosed></ownershipDocument>",
        b"<somethingElse/>",
        b"<ownershipDocument><documentType>8-K</documentType></ownershipDocument>",
        FORM4_XML.replace(b"0001045810", b"not-a-cik"),
        FORM4_XML.replace(b"<value>1000</value>", b"<value>many</value>"),
        FORM4_XML.replace(b"<value>1000</value>", b"<value>-5</value>"),
        FORM4_XML.replace(b"<value>176.52</value>", b"<value>NaN</value>"),
        FORM4_XML.replace(b"<transactionCode>P<", b"<transactionCode>PP<"),
        FORM4_XML.replace(b"<value>2026-11-04</value>", b"<value>soon</value>"),
    ],
)
def test_an_unsafe_or_malformed_form4_is_refused(payload: bytes) -> None:
    with pytest.raises(EdgarError):
        parse_form4(payload)


def test_a_form4_with_too_many_rows_is_refused() -> None:
    row = (
        b"<nonDerivativeTransaction><transactionDate><value>2026-11-04</value></transactionDate>"
        b"<transactionCoding><transactionCode>S</transactionCode></transactionCoding>"
        b"</nonDerivativeTransaction>"
    )
    big = FORM4_XML.replace(b"<nonDerivativeHolding>", row * 501 + b"<nonDerivativeHolding>")
    with pytest.raises(EdgarError, match="more transactions"):
        parse_form4(big)


class FakeEdgarBytes:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, str], int]] = []

    def get_bytes(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        max_bytes: int,
    ) -> bytes:
        assert params is None
        self.calls.append((url, dict(headers or {}), max_bytes))
        return self.body


def test_fetch_form4_asks_the_archive_for_the_raw_document_with_a_size_cap() -> None:
    ref = FilingRef(
        1045810,
        "0001197647-26-000013",
        "4",
        date(2026, 11, 6),
        datetime(2026, 11, 6, 21, 12, 2, tzinfo=UTC),
        "wk-form4_1793999520.xml",
    )
    fake = FakeEdgarBytes(FORM4_XML)
    form = fetch_form4(fake, UA, ref)
    assert form.issuer_symbol == "NVDA"
    assert fake.calls == [(ref.url, {"User-Agent": UA}, 1_000_000)]


def test_the_user_agent_names_the_tool_and_the_contact() -> None:
    assert UA == "ai-stock-trader intel feed owner@mailbox.test"
