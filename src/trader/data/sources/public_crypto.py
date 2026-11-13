"""Keyless public crypto market data: Coinbase Exchange candles and Kraken OHLC/metadata.

No account, API key or subscription is involved; these are public read-only endpoints.

* **Coinbase Exchange** ``/products/{id}/candles`` returns at most 300 buckets per request,
  newest first, as ``[time, low, high, open, close, volume]``. Buckets without trades are
  omitted, so a missing bucket cannot be told apart from an outage. No VWAP or trade count is
  supplied.
* **Kraken** ``/0/public/OHLC`` returns at most 720 recent rows as
  ``[time, open, high, low, close, vwap, volume, count]``; the **last row is the current,
  uncommitted frame** and is always dropped (CRYPTO §5). It is not a historical backfill
  service; deep Kraken history needs the downloadable OHLCVT archive (owner download).
* **Kraken AssetPairs** supplies the pair's increments and minimums, which replace the
  placeholder instrument metadata; **Ticker** gives best bid/ask point samples.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trader.data.history import DataQualityError, HistoryBar
from trader.data.sources.http import JsonTransport
from trader.domain.types import BPS, iso_utc, require_utc

COINBASE_BASE = "https://api.exchange.coinbase.com"
COINBASE_MAX_BUCKETS = 300
COINBASE_GRANULARITIES = {1, 5, 15, 60, 360, 1440}
KRAKEN_BASE = "https://api.kraken.com/0/public"
KRAKEN_INTERVALS = {1, 5, 15, 30, 60, 240, 1440, 10080, 21600}
_KRAKEN_ASSET_NAMES = {"XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "ZUSD": "USD", "ZEUR": "EUR"}


def _num(value: object) -> Decimal:
    if isinstance(value, (bool, float)):
        raise DataQualityError("market data number arrived as a binary float or boolean")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise DataQualityError(f"unexpected numeric value {value!r}")


# ----------------------------------------------------------------------------- Coinbase


def fetch_coinbase_candles(
    transport: JsonTransport,
    product_id: str,
    *,
    minutes: int,
    start: datetime,
    end: datetime,
) -> list[HistoryBar]:
    """All buckets with ``start <= bucket < end`` (unsorted, may contain page overlaps)."""
    if minutes not in COINBASE_GRANULARITIES:
        raise ValueError(f"Coinbase has no {minutes}-minute granularity")
    start, end = require_utc(start), require_utc(end)
    step = timedelta(minutes=minutes)
    page = step * COINBASE_MAX_BUCKETS
    out: list[HistoryBar] = []
    cursor = start
    while cursor < end:
        page_end = min(cursor + page, end)
        payload = transport.get_json(
            f"{COINBASE_BASE}/products/{product_id}/candles",
            {
                "granularity": str(minutes * 60),
                "start": iso_utc(cursor),
                "end": iso_utc(page_end - step),  # the range is inclusive of its last bucket
            },
        )
        if not isinstance(payload, list):
            raise DataQualityError(f"unexpected Coinbase candles payload: {str(payload)[:200]}")
        for row in payload:
            if not isinstance(row, list) or len(row) < 6:
                raise DataQualityError(f"malformed Coinbase candle {row!r}")
            bucket = datetime.fromtimestamp(int(_num(row[0])), tz=UTC)
            if not cursor <= bucket < page_end:
                continue
            out.append(
                HistoryBar(
                    start=bucket,
                    minutes=minutes,
                    low=_num(row[1]),
                    high=_num(row[2]),
                    open=_num(row[3]),
                    close=_num(row[4]),
                    volume=_num(row[5]),
                )
            )
        cursor = page_end
    return out


def fetch_coinbase_product(transport: JsonTransport, product_id: str) -> dict[str, Any]:
    payload = transport.get_json(f"{COINBASE_BASE}/products/{product_id}")
    if not isinstance(payload, dict):
        raise DataQualityError("unexpected Coinbase product payload")
    keep = (
        "id",
        "base_currency",
        "quote_currency",
        "quote_increment",
        "base_increment",
        "min_market_funds",
        "status",
        "trading_disabled",
    )
    return {k: str(payload.get(k)) for k in keep}


# ----------------------------------------------------------------------------- Kraken


def _kraken_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DataQualityError("unexpected Kraken payload")
    errors = payload.get("error") or []
    if errors:
        raise DataQualityError(f"Kraken error: {errors}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise DataQualityError("Kraken payload without result")
    return result


def fetch_kraken_ohlc(transport: JsonTransport, pair: str, *, minutes: int) -> list[HistoryBar]:
    """Committed recent OHLC rows; the uncommitted final row is always discarded."""
    if minutes not in KRAKEN_INTERVALS:
        raise ValueError(f"Kraken has no {minutes}-minute interval")
    result = _kraken_result(
        transport.get_json(f"{KRAKEN_BASE}/OHLC", {"pair": pair, "interval": str(minutes)})
    )
    keys = [k for k in result if k != "last"]
    if len(keys) != 1:
        raise DataQualityError(f"expected one pair in Kraken OHLC result, got {keys}")
    rows = result[keys[0]]
    if not isinstance(rows, list):
        raise DataQualityError("malformed Kraken OHLC rows")
    out: list[HistoryBar] = []
    for row in rows[:-1]:  # the last row is the current, not-yet-committed frame
        if not isinstance(row, list) or len(row) < 8:
            raise DataQualityError(f"malformed Kraken OHLC row {row!r}")
        out.append(
            HistoryBar(
                start=datetime.fromtimestamp(int(_num(row[0])), tz=UTC),
                minutes=minutes,
                open=_num(row[1]),
                high=_num(row[2]),
                low=_num(row[3]),
                close=_num(row[4]),
                vwap=_num(row[5]),
                volume=_num(row[6]),
                trade_count=int(_num(row[7])),
            )
        )
    return out


def kraken_asset(code: str) -> str:
    return _KRAKEN_ASSET_NAMES.get(code, code)


def fetch_kraken_pairs(transport: JsonTransport, pairs: Sequence[str]) -> dict[str, dict[str, Any]]:
    result = _kraken_result(
        transport.get_json(f"{KRAKEN_BASE}/AssetPairs", {"pair": ",".join(pairs)})
    )
    out: dict[str, dict[str, Any]] = {}
    for key, meta in result.items():
        if not isinstance(meta, dict):
            raise DataQualityError("malformed Kraken AssetPairs entry")
        out[key] = meta
    return out


def kraken_pair_to_spec(key: str, meta: dict[str, Any], *, as_of: datetime) -> dict[str, Any]:
    """Instrument-spec mapping for configs/instruments (research use, not tradable)."""
    base = kraken_asset(str(meta["base"]))
    quote = kraken_asset(str(meta["quote"]))
    lot_decimals = int(_num(meta["lot_decimals"]))
    tick = meta.get("tick_size")
    price_tick = str(tick) if tick is not None else f"1e-{int(_num(meta['pair_decimals']))}"
    return {
        "kind": "crypto_spot",
        "identity": {
            "instrument_id": f"KRAKEN:{base}/{quote}",
            "broker": "kraken",
            "broker_asset_id": key,
            "symbol": str(meta.get("wsname", f"{base}/{quote}")),
            "currency": quote,
            "product_type": "CRYPTO_SPOT",
            "track": "CRYPTO",
        },
        "base_asset": base,
        "quote_asset": quote,
        "fee_asset": "QUOTE",
        "quantity_step": format(Decimal(1).scaleb(-lot_decimals), "f"),
        "min_quantity": str(meta["ordermin"]),
        "min_notional": str(meta.get("costmin", "0")),
        "price_tick": format(Decimal(price_tick), "f"),
        "exposure_tags": [base, "CRYPTO"],
        "tradable": False,
        "fractionable": True,
        "market_status": "online" if str(meta.get("status", "online")) == "online" else "halted",
        "evidence": "DOCUMENTED",
        "as_of": iso_utc(as_of),
        "source": f"{KRAKEN_BASE}/AssetPairs?pair={meta.get('altname', key)} (public metadata; "
        "tradable=false until an eligible account is certified)",
    }


def sample_kraken_spreads(
    transport: JsonTransport, pairs: Sequence[str], *, samples: int, now: datetime
) -> list[dict[str, str]]:
    """Point samples of best bid/ask; evidence for spread assumptions, not a distribution."""
    out: list[dict[str, str]] = []
    for i in range(samples):
        result = _kraken_result(
            transport.get_json(f"{KRAKEN_BASE}/Ticker", {"pair": ",".join(pairs)})
        )
        for key, ticker in sorted(result.items()):
            ask = _num(ticker["a"][0])
            bid = _num(ticker["b"][0])
            mid = (ask + bid) / 2
            out.append(
                {
                    "pair": key,
                    "sample": str(i),
                    "sampled_near": iso_utc(now),
                    "bid": str(bid),
                    "ask": str(ask),
                    "spread_bps": f"{(ask - bid) / mid / BPS:.4f}",
                }
            )
    return out
