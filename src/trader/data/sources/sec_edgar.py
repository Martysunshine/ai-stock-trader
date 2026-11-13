"""SEC EDGAR Form 4 (insider transactions) for the intel feed (ADR-0047).

Two requests per company, both to the SEC's own hosts:

* ``https://data.sec.gov/submissions/CIK##########.json`` lists a company's recent filings as
  column arrays (``form``, ``accessionNumber``, ``acceptanceDateTime``, ``primaryDocument``...).
* ``https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<document>.xml`` is the raw Form 4.

Shapes verified against EDGAR on 2026-11-10: ``acceptanceDateTime`` is UTC (the filing index
shows the same instant in New York time), and ``primaryDocument`` carries a style-sheet prefix
(``xslF345X06/``) that is stripped to reach the raw XML.

The SEC asks automated readers to name a contact in the ``User-Agent`` and to stay under ten
requests a second. The contact is passed in by the caller; this module never reads a secret.

A Form 4 is untrusted input. Before parsing, a document that declares a DTD or an entity, uses
an encoding other than UTF-8/ASCII/Latin-1, or contains a NUL byte is refused, so the XML parser
never sees an entity to expand. The document URL is built from a constant host and from a CIK,
an accession number and a file name that each match a strict pattern, never from free text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

from trader.data.history import DataQualityError
from trader.data.sources.http import BytesTransport, JsonTransport
from trader.domain.types import require_utc

EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions"
EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
#: Forms read: the insider transaction report and its amendment.
FORM4 = frozenset({"4", "4/A"})
MAX_FORM4_BYTES = 1_000_000
MAX_TRANSACTIONS = 500

_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_STYLE_PREFIX = re.compile(r"^xsl[A-Za-z0-9]+/")
_DOCUMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.xml$")
_DECLARED_ENCODING = re.compile(rb"""<\?xml[^>]*encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
_SAFE_ENCODINGS = frozenset({b"utf-8", b"utf8", b"us-ascii", b"ascii", b"iso-8859-1"})
_COLUMNS = ("form", "accessionNumber", "filingDate", "acceptanceDateTime", "primaryDocument")


class EdgarError(DataQualityError):
    """An EDGAR answer is not what this module is prepared to read."""


@dataclass(frozen=True, slots=True)
class FilingRef:
    cik: int
    accession: str
    form: str
    filed: date
    accepted_at: datetime
    primary_document: str

    @property
    def url(self) -> str:
        return (
            f"{EDGAR_ARCHIVES}/{self.cik}/{self.accession.replace('-', '')}/{self.primary_document}"
        )

    @property
    def index_url(self) -> str:
        folder = self.accession.replace("-", "")
        return f"{EDGAR_ARCHIVES}/{self.cik}/{folder}/{self.accession}-index.htm"


@dataclass(frozen=True, slots=True)
class InsiderOwner:
    name: str
    director: bool
    officer: bool
    ten_percent_owner: bool
    other: bool
    officer_title: str

    @property
    def role(self) -> str:
        parts = []
        if self.officer:
            parts.append(f"officer ({self.officer_title})" if self.officer_title else "officer")
        if self.director:
            parts.append("director")
        if self.ten_percent_owner:
            parts.append("10% owner")
        if self.other or not parts:
            parts.append("other")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class InsiderTransaction:
    security: str
    on: date
    #: SEC transaction code: P open-market purchase, S open-market sale, A grant, M option
    #: exercise, F shares withheld for tax, G gift, and others.
    code: str
    #: True = acquired, False = disposed, None = not stated.
    acquired: bool | None
    shares: Decimal
    #: None when the form gives no price (often a footnote instead).
    price: Decimal | None
    derivative: bool


@dataclass(frozen=True, slots=True)
class Form4:
    issuer_cik: int
    issuer_symbol: str
    period: date | None
    owners: tuple[InsiderOwner, ...]
    transactions: tuple[InsiderTransaction, ...]
    amendment: bool
    #: The filer ticked "made under a Rule 10b5-1 plan"; None on older forms without the box.
    planned_10b5_1: bool | None


def user_agent(contact: str) -> str:
    """The ``User-Agent`` the SEC asks for: who is calling and how to reach them."""
    return f"ai-stock-trader intel feed {contact}"


def _headers(ua: str) -> dict[str, str]:
    return {"User-Agent": ua}


def _column(recent: Mapping[str, Any], name: str, length: int) -> list[Any]:
    values = recent.get(name)
    if not isinstance(values, list) or len(values) != length:
        raise EdgarError(f"EDGAR filings column {name} is missing or ragged")
    return values


def fetch_recent_filings(
    transport: JsonTransport,
    ua: str,
    cik: int,
    symbol: str,
    *,
    since: date,
    forms: frozenset[str] = FORM4,
) -> list[FilingRef]:
    """Form 4 filings for ``cik`` filed on or after ``since``, oldest acceptance first.

    Refuses a company record whose tickers do not include ``symbol``: a wrong CIK in the
    configuration must fail loudly, not attach another company's insiders to this symbol.
    A filing without a usable XML document (a paper filing) is left out.
    """
    if cik <= 0:
        raise ValueError("cik must be positive")
    payload = transport.get_json(f"{EDGAR_SUBMISSIONS}/CIK{cik:010d}.json", None, _headers(ua))
    if not isinstance(payload, dict):
        raise EdgarError("unexpected EDGAR submissions payload")
    tickers = payload.get("tickers")
    if not isinstance(tickers, list) or symbol.upper() not in {str(t).upper() for t in tickers}:
        raise EdgarError(f"EDGAR company {cik} does not list the ticker {symbol}")
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict) or not isinstance(recent.get("form"), list):
        raise EdgarError("unexpected EDGAR filings block")
    length = len(recent["form"])
    form, accession, filed, accepted, document = (_column(recent, c, length) for c in _COLUMNS)
    refs: list[FilingRef] = []
    for i in range(length):
        if form[i] not in forms:
            continue
        try:
            filed_on = date.fromisoformat(str(filed[i]))
            accepted_at = require_utc(
                datetime.fromisoformat(str(accepted[i]).replace("Z", "+00:00"))
            )
        except ValueError as exc:
            raise EdgarError(f"unexpected EDGAR filing date for {accession[i]!r}") from exc
        if filed_on < since:
            continue
        if not isinstance(accession[i], str) or not _ACCESSION.fullmatch(accession[i]):
            raise EdgarError("unexpected EDGAR accession number")
        name = _STYLE_PREFIX.sub("", str(document[i] or ""))
        if not _DOCUMENT.fullmatch(name):
            continue
        refs.append(FilingRef(cik, accession[i], str(form[i]), filed_on, accepted_at, name))
    return sorted(refs, key=lambda r: (r.accepted_at, r.accession))


def fetch_form4(
    transport: BytesTransport, ua: str, ref: FilingRef, *, max_bytes: int = MAX_FORM4_BYTES
) -> Form4:
    return parse_form4(transport.get_bytes(ref.url, None, _headers(ua), max_bytes=max_bytes))


# --------------------------------------------------------------------------- parsing
def _refuse_unsafe(payload: bytes) -> None:
    if b"\x00" in payload or payload[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise EdgarError("Form 4 is not UTF-8 text")
    declared = _DECLARED_ENCODING.search(payload[:300])
    if declared and declared.group(1).lower() not in _SAFE_ENCODINGS:
        raise EdgarError("Form 4 declares an unexpected encoding")
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise EdgarError("Form 4 declares a DTD or an entity")


def _find_text(node: ElementTree.Element, path: str, limit: int = 200) -> str:
    """Text at ``path``; a ``<value>`` child is used when present. Empty when absent."""
    found = node.find(path)
    if found is None:
        return ""
    value = found.find("value")
    text = (value.text if value is not None else found.text) or ""
    return " ".join(text.split())[:limit]


def _flag(node: ElementTree.Element | None, path: str) -> bool | None:
    if node is None:
        return None
    text = _find_text(node, path).lower()
    if text in ("1", "true"):
        return True
    if text in ("0", "false"):
        return False
    return None


def _decimal(text: str, what: str) -> Decimal | None:
    if text == "":
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise EdgarError(f"Form 4 {what} is not a number") from exc
    if not value.is_finite() or value < 0:
        raise EdgarError(f"Form 4 {what} is not a non-negative number")
    return value


def _date(text: str, what: str) -> date:
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise EdgarError(f"Form 4 {what} is not a date") from exc


def _owner(node: ElementTree.Element) -> InsiderOwner:
    relation = node.find("reportingOwnerRelationship")
    return InsiderOwner(
        name=_find_text(node, "reportingOwnerId/rptOwnerName", 120),
        director=bool(_flag(relation, "isDirector")),
        officer=bool(_flag(relation, "isOfficer")),
        ten_percent_owner=bool(_flag(relation, "isTenPercentOwner")),
        other=bool(_flag(relation, "isOther")),
        officer_title=_find_text(relation, "officerTitle", 80) if relation is not None else "",
    )


def _transaction(node: ElementTree.Element, *, derivative: bool) -> InsiderTransaction:
    shares = _decimal(_find_text(node, "transactionAmounts/transactionShares"), "share count")
    code = _find_text(node, "transactionCoding/transactionCode", 4).upper()
    if not re.fullmatch(r"[A-Z]", code):
        raise EdgarError("Form 4 transaction code is not one letter")
    side = _find_text(node, "transactionAmounts/transactionAcquiredDisposedCode", 4).upper()
    return InsiderTransaction(
        security=_find_text(node, "securityTitle", 80),
        on=_date(_find_text(node, "transactionDate"), "transaction date"),
        code=code,
        acquired={"A": True, "D": False}.get(side),
        shares=shares if shares is not None else Decimal(0),
        price=_decimal(_find_text(node, "transactionAmounts/transactionPricePerShare"), "price"),
        derivative=derivative,
    )


def parse_form4(payload: bytes) -> Form4:
    """Read a raw Form 4 ``ownershipDocument``. Pure; raises :class:`EdgarError`."""
    _refuse_unsafe(payload)
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise EdgarError("Form 4 is not well-formed XML") from exc
    if root.tag != "ownershipDocument":
        raise EdgarError("Form 4 root element is not ownershipDocument")
    document_type = _find_text(root, "documentType", 8)
    if document_type not in FORM4:
        raise EdgarError("document is not a Form 4")
    cik_text = _find_text(root, "issuer/issuerCik", 12)
    if not cik_text.isdigit():
        raise EdgarError("Form 4 issuer CIK is not a number")
    rows = [(n, False) for n in root.findall("nonDerivativeTable/nonDerivativeTransaction")]
    rows += [(n, True) for n in root.findall("derivativeTable/derivativeTransaction")]
    if len(rows) > MAX_TRANSACTIONS:
        raise EdgarError("Form 4 lists more transactions than this reader accepts")
    period = _find_text(root, "periodOfReport")
    return Form4(
        issuer_cik=int(cik_text),
        issuer_symbol=_find_text(root, "issuer/issuerTradingSymbol", 12).upper(),
        period=_date(period, "period") if period else None,
        owners=tuple(_owner(n) for n in root.findall("reportingOwner")[:10]),
        transactions=tuple(_transaction(n, derivative=d) for n, d in rows),
        amendment=document_type == "4/A",
        planned_10b5_1=_flag(root, "aff10b5One"),
    )
