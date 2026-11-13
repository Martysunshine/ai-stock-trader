# Data coverage

Status on 2026-11-07. Market data files live under `data/` (gitignored); provenance, entitlement, checksums and quality findings are in [research/data_manifest.json](../research/data_manifest.json). `trader ingest-history` re-checks every acquired dataset's content checksum before use, and re-acquires it on a fresh clone.

## Acquired

| Dataset | Source and entitlement | Range | Bars | Coverage | Use |
| --- | --- | --- | --- | --- | --- |
| `coinbase_btc_eth_usd_15m_v1` | Coinbase Exchange public candles (no account, no key, no fee); research use, not redistributed | 2023-11-01 .. 2026-11-01 | BTC 105,159; ETH 105,157 | 99.95% / 99.94% (57 and 59 missing 15-minute intervals; the longest, on both pairs, is 25 bars from 2026-05-08 01:30 UTC) | Crypto baseline study (price proxy for Kraken) |
| `kraken_btc_eth_usd_15m_recent_v1` | Kraken public OHLC (recent 720 rows only; uncommitted last row dropped) | 2026-10-31 .. 2026-11-07 | 672 each | 100% | Pair metadata, spread samples, venue basis check |
| `coinbase_btc_eth_usd_15m_recent_v1` | Coinbase, same week as the Kraken capture | 2026-10-31 .. 2026-11-07 | 672 each | 100% | Venue basis check only |
| `synthetic_equities_5m_v1` | SYNTHETIC (generated, seeded) | 2026-10-26 .. 2026-12-01 | SYNA 1,908; SYNB 1,836 | by construction: one 30-minute feed gap, one missing session | Calendar, split, rename and missing-data tests |
| `synthetic_base_v1`, `synthetic_gap_v1` | SYNTHETIC recorded sessions (M1) | 2026-11-27 | 1,680 / 1,602 events | - | First demonstrator |

**Venue basis.** Over the common week, Kraken minus Coinbase 15-minute closes averaged +1.04 bps (BTC) and +0.58 bps (ETH), mean absolute difference 1.3 and 1.1 bps, 15-minute return correlation 0.997 and 0.998. Kraken best bid/ask point samples showed full spreads of 0.013 bps (BTC) and 0.045 bps (ETH); the research assumption of 1 bps half-spread plus 2 bps slippage per side is deliberately conservative.

## Blocked

| Dataset | Blocker | Owner action |
| --- | --- | --- |
| `alpaca_us_equities_5m_v1` (AAPL, MSFT, GOOGL, AMZN, NVDA, 5-minute, IEX feed, raw prices plus corporate actions, 2024-11 .. 2026-11) | No Alpaca API keys | [owner_setup.md](owner_setup.md) §5 |
| `kraken_ohlcvt_archive_btc_eth_usd_15m_v1` (venue-matched deep Kraken history) | Archive must be downloaded by the owner | [owner_setup.md](owner_setup.md) §5 |
| `alpaca_us_etfs_daily_sip_v1` (16 ETFs: SPY, QQQ, IWM, DIA, XLB, XLE, XLF, XLI, XLK, XLP, XLU, XLV, XLY, TLT, IEF, GLD; daily bars, SIP feed, raw prices plus corporate actions, 2016-01 .. 2026-11). Feeds the daily ETF study `etf_daily_v1` (T-0013 Candidate C, T-0014 Candidate D; development from 2017-02-01 after a 260-session warm-up; locked final test 2026-08 .. 2026-11) | Not yet acquired (added 2026-11-09) | `uv run trader ingest-history --dataset alpaca_us_etfs_daily_sip_v1` with the stored paper keys; then review `quality.missing_sessions` and move this row to Acquired ([runbook.md](runbook.md) §10.1) |

## Source semantics

| Source | Facts that matter for research |
| --- | --- |
| Coinbase candles | At most 300 buckets per request; buckets without trades are omitted, so a missing bar may be an outage or a quiet interval (treated as missing data). No VWAP or trade count is supplied; strategies use a volume-weighted *typical price* reference that any venue's bars reproduce. |
| Kraken recent OHLC | At most 720 rows, last row uncommitted. Not a backfill service. |
| Alpaca Basic (free) | Real-time IEX only, a single venue, not NBBO; its volume is never total market volume. Historical bars are requested raw; splits and symbol changes come from the corporate-actions endpoint and are applied point-in-time. |
| Alpaca Algo Trader Plus | USD 99/month for consolidated SIP. Not purchased (`paid_data_enabled: false`). |
| Alpaca daily bars (SIP, history) | Free without a subscription when the request's end is at least 15 minutes old ([Market Data FAQ](https://docs.alpaca.markets/docs/market-data-faq)). One row per session, stamped 00:00 New York; stored as `[session open, session close)` with early closes (`data/daily_bars.py`). Open, high, low and close exclude extended-hours trades (conditions T/U update only a daily bar's volume). Volume, trade count and VWAP include 04:00-20:00 New York trading. The open is the first qualifying trade, not necessarily the opening auction. The close is normally the closing-auction print. Checked with read-only probes on 2026-11-09: SPY 2025-03-10 daily volume 99,326,624 against 99,319,842 summed over its hourly bars from 04:00 to 20:00. |
| Alpaca trading fees (stock studies) | Verified 2026-11-08 from the [Alpaca Brokerage Fee Schedule](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf) (revised 2026-10-12), SEC Section 31 orders, FINRA TAF filings and CAT fee alerts: zero commission; SEC fee and FINRA TAF on sells, CAT fee on buys and sells, computed on the exact fractional quantity and rounded up to the cent. `stocks_baseline_v1` uses `configs/fees/alpaca_us_equities_v2.yaml`: the highest rates of 2022-01 .. 2026-11 (SEC 27.80 per million, TAF 0.000195 per share up to 9.79, CAT 0.000048 per share), rounded per order. Sources and reasoning: that file and [cost_feasibility.md](cost_feasibility.md) §2. |
| Alpaca trading fees (ETF study) | `etf_daily_v1` uses the same `alpaca_us_equities_v2.yaml`. Two points are assumed and must be confirmed with dated sources before registration: Alpaca's zero commission and the SEC, TAF and CAT pass-through fees apply to ETF trades as to shares, and the 2022-2026 maximum rates also bound 2017-2021. CAT (charged from 2024) is applied to the whole span, which is conservative. |
| Feed parity | A strategy uses the same feed in research and live (`require_feed_parity: true`). The daily ETF study uses SIP daily bars. The paper runtime polls IEX 5-minute bars, so a daily rule needs SIP daily bars at its decision time before it can run on paper. |
| Gold and silver CFD data | The broker's executable bid/ask stream is needed; a futures chart is context, not a fill model. |

## Known gaps

- No point-in-time stock universe or earnings calendar: the stock universe is a fixed, disclosed current-symbol list (selection bias), and Candidate B's "no company event" condition cannot be enforced until a point-in-time calendar exists.
- Bar data only: no quotes or trades, so intrabar paths, queue position and passive (maker) fills cannot be modelled.
- The crypto study uses Coinbase prices as a proxy for Kraken execution; the venue-matched archive is blocked on an owner download.
