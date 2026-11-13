"""The intel feed's sources (ADR-0047): news, insider filings and pre-market activity.

Each source turns the configured targets into records. A source only reads; it never decides
anything. Adding one (X posts, another news provider) means one more class with this ``fetch``
and one more entry in ``configs/intel/sources.yaml``.

* **Alpaca news.** One request for every followed symbol, oldest update first, continued from
  the newest update already read. Headline, summary and metadata only.
* **SEC EDGAR Form 4.** Per company: the list of recent filings, then each new Form 4 document,
  oldest first, a bounded number per run. Needs the owner's contact line; without it the source
  reports ``waiting`` and makes no request.
* **Alpaca SIP pre-market.** 5-minute bars from 04:00 New York to the open, requested only while
  that window (plus the data delay) is open on a weekday. One record per stock and session,
  revised as the morning goes on.

A failure is reported per instrument where it is per instrument (EDGAR) and for the whole source
where one request serves every symbol (Alpaca). Error text never contains a key or the contact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol

from trader.config.secrets import SecretsError
from trader.data.sources.alpaca_bars import (
    alpaca_number,
    alpaca_time,
    fetch_alpaca_bars,
    fetch_bar_rows,
)
from trader.data.sources.alpaca_news import NEWS_PAGE_LIMIT, NewsItem, fetch_news
from trader.data.sources.http import BytesTransport, JsonTransport
from trader.data.sources.sec_edgar import (
    FilingRef,
    Form4,
    fetch_form4,
    fetch_recent_filings,
    user_agent,
)
from trader.domain.types import canon_decimal, iso_utc
from trader.intel.classify import EXTRACTOR_VERSION, classify
from trader.intel.config import SourceConfig, Target
from trader.intel.records import (
    SUMMARY_LIMIT,
    TITLE_LIMIT,
    IntelRecord,
    Kind,
    clean_text,
    parse_time,
)
from trader.market.sources import NEW_YORK, ny_minutes, session_open, short_error

if TYPE_CHECKING:
    from trader.config.alpaca_keys import AlpacaCredentials
    from trader.config.secrets import SecEdgarContact

SourceStatus = Literal["ok", "partial", "error", "waiting", "idle"]
NEWS_PAGES = 10
#: The news cursor steps back this far, so an item updated while a page was read is not missed.
NEWS_OVERLAP = timedelta(minutes=2)
PREMARKET_OPEN = time(4, 0)
#: Requests continue this long after the last pre-market bar has become available.
PREMARKET_GRACE_MINUTES = 10
SIP_FEED = "sip"
FORM4_VERSION = "form4-v1"
PREMARKET_VERSION = "premarket-v1"
WAITING_FOR_CONTACT = "waiting for SEC_EDGAR_CONTACT in .env"
MAX_URL = 500


@dataclass
class SourceResult:
    """What one run of a source produced, and where its next run should continue."""

    records: list[IntelRecord] = field(default_factory=list)
    #: Target id -> why it failed.
    errors: dict[str, str] = field(default_factory=dict)
    cursor: dict[str, str] = field(default_factory=dict)
    status: SourceStatus = "ok"
    note: str | None = None
    retry_after: datetime | None = None


class IntelSource(Protocol):
    id: str
    kind: Kind

    def fetch(
        self, targets: Sequence[Target], cursor: Mapping[str, str], *, now: datetime
    ) -> SourceResult: ...


def safe_url(value: str) -> str | None:
    """An https link without spaces or control characters, else nothing."""
    ok = value.startswith("https://") and len(value) <= MAX_URL and value.isprintable()
    return value if ok and " " not in value else None


def _grouped(value: Decimal) -> str:
    whole = value == value.to_integral_value()
    return f"{value:,.0f}" if whole else f"{value:,.2f}"


# ----------------------------------------------------------------------------- news
def news_record(item: NewsItem, targets: Sequence[str]) -> IntelRecord | None:
    title = clean_text(item.headline, TITLE_LIMIT)
    if not title:
        return None
    summary = clean_text(item.summary, SUMMARY_LIMIT)
    category, tags = classify(title, summary)
    return IntelRecord(
        kind="news",
        source="alpaca_news",
        source_id=item.source_id,
        targets=tuple(sorted(targets)),
        title=title,
        summary=summary,
        category=category,
        tags=tags,
        extractor_version=EXTRACTOR_VERSION,
        url=safe_url(item.url),
        published_at=item.created_at,
        updated_at=item.updated_at,
        provenance={
            "provider": "alpaca",
            "feed": clean_text(item.source, 40),
            "author": clean_text(item.author, 80),
            "symbols": ",".join(clean_text(s, 12) for s in item.symbols[:20]),
        },
    )


class AlpacaNewsSource:
    id = "alpaca_news"
    kind: Kind = "news"

    def __init__(
        self,
        transport: JsonTransport,
        credentials: Callable[[], AlpacaCredentials],
        config: SourceConfig,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._config = config

    def fetch(
        self, targets: Sequence[Target], cursor: Mapping[str, str], *, now: datetime
    ) -> SourceResult:
        by_symbol: dict[str, list[str]] = {}
        for target in targets:
            for symbol in target.news:
                by_symbol.setdefault(symbol, []).append(target.id)
        result = SourceResult(cursor=dict(cursor))
        if not by_symbol:
            result.status, result.note = "idle", "no instrument follows news"
            return result
        horizon = now - timedelta(hours=self._config.lookback_hours)
        read_to = parse_time(cursor.get("updated_after"))
        start = max(horizon, read_to - NEWS_OVERLAP) if read_to is not None else horizon
        items = fetch_news(
            self._transport,
            self._credentials(),
            sorted(by_symbol),
            start=start,
            end=now,
            max_pages=NEWS_PAGES,
        )
        for item in items:
            mine = sorted({t for s in item.symbols for t in by_symbol.get(s, ())})
            record = news_record(item, mine) if mine else None
            if record is not None:
                result.records.append(record)
        if items:
            result.cursor["updated_after"] = iso_utc(max(i.updated_at for i in items))
        if len(items) >= NEWS_PAGES * NEWS_PAGE_LIMIT:
            result.note = "a backlog of news is being read; continuing on the next run"
        return result


# ----------------------------------------------------------------------------- insiders
def form4_record(target: Target, ref: FilingRef, form: Form4) -> IntelRecord:
    """One Form 4 as a record. Open-market purchases (code P) and sales (code S) of
    non-derivative securities are summed; grants, exercises, gifts and tax withholding are
    listed but never counted as buying or selling."""
    buy_shares = sell_shares = buy_value = sell_value = Decimal(0)
    codes: list[str] = []
    for tx in form.transactions:
        if tx.code not in codes:
            codes.append(tx.code)
        if tx.derivative or tx.code not in ("P", "S"):
            continue
        value = tx.shares * tx.price if tx.price is not None else Decimal(0)
        if tx.code == "P":
            buy_shares, buy_value = buy_shares + tx.shares, buy_value + value
        else:
            sell_shares, sell_value = sell_shares + tx.shares, sell_value + value
    if buy_shares > 0 and sell_shares > 0:
        category = "insider_mixed"
        action = f"bought {_grouped(buy_shares)} and sold {_grouped(sell_shares)} shares"
    elif buy_shares > 0:
        category = "insider_buy"
        action = f"bought {_grouped(buy_shares)} shares"
        if buy_value > 0:
            action += f" for about USD {_grouped(buy_value.quantize(Decimal(1)))}"
    elif sell_shares > 0:
        category = "insider_sell"
        action = f"sold {_grouped(sell_shares)} shares"
        if sell_value > 0:
            action += f" for about USD {_grouped(sell_value.quantize(Decimal(1)))}"
    else:
        category = "insider_other"
        action = "no open-market trade" + (f" (codes {', '.join(sorted(codes))})" if codes else "")
    owners = [
        {"name": clean_text(o.name, 80), "role": clean_text(o.role, 100)} for o in form.owners[:5]
    ]
    who = owners[0]["name"] if owners else "An insider"
    if owners and owners[0]["role"]:
        who += f" ({owners[0]['role']})"
    if len(form.owners) > 1:
        who += f" and {len(form.owners) - 1} more"
    facts: dict[str, Any] = {
        "form": ref.form,
        "amendment": form.amendment,
        "planned_10b5_1": form.planned_10b5_1,
        "period": form.period.isoformat() if form.period else None,
        "owners": owners,
        "buy_shares": canon_decimal(buy_shares),
        "buy_value": canon_decimal(buy_value),
        "sell_shares": canon_decimal(sell_shares),
        "sell_value": canon_decimal(sell_value),
        "transactions_total": len(form.transactions),
        "transactions": [
            {
                "code": tx.code,
                "acquired": tx.acquired,
                "shares": canon_decimal(tx.shares),
                "price": canon_decimal(tx.price) if tx.price is not None else None,
                "on": tx.on.isoformat(),
                "security": clean_text(tx.security, 60),
                "derivative": tx.derivative,
            }
            for tx in form.transactions[:40]
        ],
    }
    return IntelRecord(
        kind="insider",
        source="edgar_form4",
        source_id=ref.accession,
        targets=(target.id,),
        title=clean_text(f"{who}: {action}", TITLE_LIMIT),
        category=category,
        tags=tuple(sorted(codes)),
        extractor_version=FORM4_VERSION,
        url=ref.index_url,
        published_at=ref.accepted_at,
        facts=facts,
        provenance={"provider": "sec", "form": ref.form, "document": ref.primary_document},
    )


def _filing_key(ref: FilingRef) -> str:
    return f"{iso_utc(ref.accepted_at)}|{ref.accession}"


class EdgarForm4Source:
    id = "edgar_form4"
    kind: Kind = "insider"

    def __init__(
        self,
        json_transport: JsonTransport,
        bytes_transport: BytesTransport,
        contact: Callable[[], SecEdgarContact],
        config: SourceConfig,
    ) -> None:
        self._json = json_transport
        self._bytes = bytes_transport
        self._contact = contact
        self._config = config

    def fetch(
        self, targets: Sequence[Target], cursor: Mapping[str, str], *, now: datetime
    ) -> SourceResult:
        result = SourceResult(cursor=dict(cursor))
        wanted = [(t, t.cik) for t in targets if t.cik is not None]
        if not wanted:
            result.status, result.note = "idle", "no instrument follows insider filings"
            return result
        try:
            agent = user_agent(self._contact().email)
        except SecretsError:
            result.status, result.note = "waiting", WAITING_FOR_CONTACT
            return result
        since = (now - timedelta(days=self._config.lookback_days)).date()
        budget = self._config.max_documents_per_run
        backlog = False
        for target, cik in wanted:
            try:
                refs = fetch_recent_filings(self._json, agent, cik, target.symbol, since=since)
            except Exception as exc:  # one company failing must not stop the others
                result.errors[target.id] = short_error(exc)
                continue
            done = cursor.get(target.id, "")
            for ref in refs:
                if _filing_key(ref) <= done:
                    continue
                if budget <= 0:
                    backlog = True
                    break
                budget -= 1
                try:
                    form = fetch_form4(self._bytes, agent, ref)
                except Exception as exc:  # stop here; the next run retries this filing
                    result.errors[target.id] = short_error(exc)
                    break
                result.cursor[target.id] = _filing_key(ref)
                # A company can also file as the owner of another issuer's shares: not ours.
                if form.issuer_cik == cik:
                    result.records.append(form4_record(target, ref, form))
        if result.errors:
            result.status = "error" if len(result.errors) == len(wanted) else "partial"
        if backlog:
            result.note = "more filings to read; continuing on the next run"
        return result


# ----------------------------------------------------------------------------- pre-market
def premarket_window(now: datetime, delay_minutes: int) -> tuple[date, datetime, datetime] | None:
    """``(session date, 04:00, the open)`` in UTC while pre-market bars for today's New York
    session can be requested, else ``None`` (weekend, too early, or the morning is over)."""
    local, minutes = ny_minutes(now)
    first = PREMARKET_OPEN.hour * 60 + PREMARKET_OPEN.minute + delay_minutes
    opened = session_open(now)
    open_minutes = ny_minutes(opened)[1]
    last = open_minutes + delay_minutes + PREMARKET_GRACE_MINUTES
    if local.weekday() >= 5 or not first <= minutes <= last:
        return None
    start = datetime.combine(local.date(), PREMARKET_OPEN, tzinfo=NEW_YORK).astimezone(UTC)
    return local.date(), start, opened


class AlpacaPremarketSource:
    id = "alpaca_premarket"
    kind: Kind = "premarket"

    def __init__(
        self,
        transport: JsonTransport,
        credentials: Callable[[], AlpacaCredentials],
        config: SourceConfig,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._config = config
        self._closes: tuple[date, dict[str, tuple[Decimal, str]]] | None = None

    def _prior_closes(
        self, creds: AlpacaCredentials, symbols: Sequence[str], session: date, end: datetime
    ) -> dict[str, tuple[Decimal, str]]:
        """The last regular close before ``session`` per symbol, from the consolidated daily bar
        (asked once per session)."""
        if self._closes is not None and self._closes[0] == session:
            return self._closes[1]
        rows = fetch_bar_rows(
            self._transport,
            creds,
            symbols,
            timeframe="1Day",
            start=end - timedelta(days=10),
            end=end,
            feed=SIP_FEED,
        )
        closes: dict[str, tuple[Decimal, str]] = {}
        for symbol, symbol_rows in rows.items():
            for row in symbol_rows:  # oldest first; Alpaca stamps a daily bar 00:00 New York
                day = alpaca_time(row["t"]).astimezone(NEW_YORK).date()
                if day < session:
                    closes[symbol] = (alpaca_number(row["c"]), day.isoformat())
        if len(closes) == len(symbols):
            self._closes = (session, closes)
        return closes

    def fetch(
        self, targets: Sequence[Target], cursor: Mapping[str, str], *, now: datetime
    ) -> SourceResult:
        result = SourceResult(cursor=dict(cursor))
        wanted = [t for t in targets if t.premarket]
        window = premarket_window(now, self._config.delay_minutes) if wanted else None
        if window is None:
            result.status, result.note = "idle", "outside the pre-market window"
            return result
        session, start, opened = window
        end = min(now - timedelta(minutes=self._config.delay_minutes), opened)
        creds = self._credentials()
        symbols = [t.symbol for t in wanted]
        closes = self._prior_closes(creds, symbols, session, end)
        bars = fetch_alpaca_bars(
            self._transport, creds, symbols, minutes=5, start=start, end=end, feed=SIP_FEED
        )
        for target in wanted:
            mine = [b for b in bars.get(target.symbol, []) if start <= b.start < opened]
            if not mine:
                continue
            last = mine[-1].close
            volume = sum((b.volume for b in mine), Decimal(0))
            through = mine[-1].start + timedelta(minutes=5)
            prior = closes.get(target.symbol)
            gap = None
            if prior is not None and prior[0] > 0:
                gap = ((last / prior[0] - 1) * 100).quantize(Decimal("0.01"))
            title = f"{target.symbol} pre-market: last {canon_decimal(last)}"
            if gap is not None:
                title += f", {gap:+.2f}% against the prior close"
            title += f", volume {_grouped(volume)}"
            result.records.append(
                IntelRecord(
                    kind="premarket",
                    source="alpaca_premarket",
                    source_id=f"{target.symbol}:{session.isoformat()}",
                    targets=(target.id,),
                    title=title,
                    category="premarket",
                    extractor_version=PREMARKET_VERSION,
                    published_at=through,
                    updated_at=through,
                    facts={
                        "session": session.isoformat(),
                        "prior_close": canon_decimal(prior[0]) if prior else None,
                        "prior_close_date": prior[1] if prior else None,
                        "last": canon_decimal(last),
                        "high": canon_decimal(max(b.high for b in mine)),
                        "low": canon_decimal(min(b.low for b in mine)),
                        "volume": canon_decimal(volume),
                        "gap_pct": canon_decimal(gap) if gap is not None else None,
                        "bars": len(mine),
                        "through": iso_utc(through),
                    },
                    provenance={
                        "provider": "alpaca",
                        "feed": SIP_FEED,
                        "delay_minutes": str(self._config.delay_minutes),
                    },
                )
            )
        return result
