# WATER_PLAN

MCP connections, broker APIs, market-data research, and implementation handoff for Trading Stock Bot AI Agent.

- Prepared: 8 November 2026, Europe/Berlin.
- Repository: this repository (private at the time of the review).
- Inspected snapshot: the `main` branch as of 8 November 2026.
- Evidence: repository inspection and provider documentation review. No new service was connected, authenticated, funded, or execution-tested during this review.
- Existing connection reported by the owner: Alpaca MCP and API. Preserve it and verify how it is configured before adding anything.
- Deliverable status: an additive plan. All implementation tasks below are proposed, not completed.

## Status on `sunshine` (2026-11-09)

This plan was written against the `main` snapshot of 8 November 2026. Reconciled with the current branch:

| Phase | State | Evidence |
| --- | --- | --- |
| 0. Reconcile the checkout | Done | Alpaca adapter, paper runtime and certification path merged on `sunshine` (91165b1; ADR-0033 to 0035). The Alpaca MCP server was already connected read-only (ADR-0032). |
| 1. Research MCP connections | Configured; read checks partly waiting on owner sign-in | `.mcp.json`, ADR-0036, [connection_report.md](connection_report.md) |
| 2. Stock data and Alpaca paper | Built; certification waits on the owner | IEX 5-minute history 2020-08..2026-10 ingested; stock baseline T-0006..T-0010 registered and run (no edge, G2 not passed); `trader paper certify` exists, never run |
| 3. Selected research data | Not started | Needs a concrete dataset gap first (section 5.1) |
| 4. Crypto venue integration | Not started | Kraken public data and the MCP's local simulator only |
| 5. Commodity/CFD demo | Keys wrapper ready | `trader secrets set capital-demo` forces demo and trading off; the server is added after the owner stores demo keys |
| 6. Optional expansion | Not started | FMP, IBKR, Coinbase, cTrader and IG recorded as `planned` in the connection report |

Research results since the plan was written: crypto baseline (T-0001..T-0005), stock baseline (T-0006..T-0010) and crypto filters (T-0011, T-0012) all show no edge after costs. The owner raised the trial budget to 20 on 2026-11-09 (daily-bar ETF hypotheses next); 12 of 20 are used.

## 1. Decision and intended result

Keep Alpaca as the first stock paper-trading integration. Add a small number of services with distinct responsibilities:

| Priority | Service | Intended responsibility | Initial mode |
| --- | --- | --- | --- |
| P0 | Existing Alpaca API/MCP | Stock research and the first broker paper adapter | Inspect existing configuration; paper/data only |
| P1 | Kraken API/MCP | Exchange-specific crypto data and crypto integration experiments | Public market data and local paper simulation |
| P1 | Alpha Vantage MCP/API | Earnings calendar, fundamentals, occasional news and macro research | Read-only, cached, within free entitlement |
| P1 | CoinGecko MCP/API | Crypto market breadth and discovery | Public read-only research |
| P2 | Capital.com API/MCP | Gold/silver and other CFD demo research | Demo account; initially disable MCP trade operations |
| P3 | FMP or Massive | Fill a measured gap in company/event data or US-stock history/quotes | Free evaluation; paid access only within an agreed budget |
| P3 | Interactive Brokers | Broader market access and an alternative broker adapter | Eligibility/cost research, then paper where available |
| Alternative | Coinbase Advanced, Twelve Data, cTrader, IG | Substitute or complementary capabilities when requirements justify them | Read-only research or appropriate demo environment |

The integration is successful when the bot can ingest trustworthy, reproducible data and execute permitted paper workflows through its existing risk and order-management boundaries. Installing more MCP servers is not itself an improvement in strategy performance.

Do not split the initial USD 50 across several funded brokers merely to connect them. Keep the shared risk budget, separate account ledgers, and actual per-account buying-power constraints. Demo balances are simulated capital and must not inflate the shared budget.

## 2. Repository inspection report

### 2.1 What was inspected

The review covered the recursive repository tree and key implementation files: README, dependencies, broker contracts/catalogue, execution gateway, runtime wiring/live guard, historical data sources, capability profiles, policy, commodity track, boundary tests, ML guide, and baseline report. This was not a full audit or a test run.

Paths in this section describe the inspected snapshot. Before coding, compare them with the current checkout and retain any newer work.

| Area | Observed state | Consequence for this plan |
| --- | --- | --- |
| `src/trader/brokers/base.py` | `BrokerAdapter` defines account, positions, orders, fills, submission, cancellation and event draining | Implement new venues against this contract |
| `src/trader/brokers/replay/` | The only checked-in concrete broker implementation is replay | A YAML capability profile is not a working broker adapter |
| `src/trader/data/sources/alpaca_history.py` | Historical stock bars and corporate-action ingestion exist; credentials are read from environment | Verify the owner's current Alpaca setup and unblock real stock research first |
| `src/trader/data/sources/public_crypto.py` | Public Coinbase candles and Kraken OHLC, pair metadata and spread samples exist | Reuse normalization and provenance logic when extending crypto data |
| `configs/capabilities/` | Profiles exist for Alpaca, Kraken, Coinbase, IBKR, Capital.com and IG | Treat unknown/account-specific capabilities as unverified until demonstrated |
| `src/trader/execution/_gateway.py` | Submission gateway owns broker submit/cancel and unknown-outcome handling | MCP trading tools must not create a second submission path |
| `src/trader/runtime/live_guard.py` | Certified live-adapter set is empty at the inspected commit | Adding a connector does not enable live trading |
| `tests/test_boundaries.py` | Enforces research/order separation and currently restricts networking and SDK dependencies | Broker networking needs a narrowly scoped architectural update |
| `configs/policy/base.yaml` | USD 50 starting capital, paid data disabled, live-loop paid-LLM budget zero | Prefer free development access and deterministic runtime code |
| `configs/tracks/commodities.yaml` | Commodity track disabled; gold then silver, replay/demo scope | Capital.com starts as a demo integration |
| `research/reports/baseline_report.md` | No registered crypto trial was profitable under the report's assumptions; G2 not passed | New datasets or venues require new evidence, not automatic promotion |
| MCP integration | No checked-in MCP client or server configuration found in the inspected tree | Locate any local/IDE configuration before concluding that Alpaca is disconnected |

The baseline uses Coinbase historical prices as a proxy for Kraken execution. Prefer venue-matched history for subsequent Kraken research, or retain and quantify the proxy limitation. Its fee assumptions are report inputs, not a verified statement of the owner's current account fees.

### 2.2 Existing architecture worth preserving

- Strategies and models cannot directly access broker authority.
- Risk approval constructs the approved order plan.
- The submission gateway persists intent before network submission.
- A submission timeout is an unknown outcome, not a reason to blindly resend.
- Reconciliation, order events and the ledger resolve the eventual result.
- Live trading requires account-specific evidence and separately satisfied promotion gates.

The specifications already under protection remain unchanged. Implement this plan with new integration code, configuration, tests and operational documents. Follow the repository's current protected-file rules.

## 3. MCP versus direct API: responsibilities

An MCP server exposes tools to an MCP client. Connecting it to Cursor, Claude or another assistant does not automatically expose those tools to the running `trader` Python application.

Use two integration paths:

| Path | Responsibilities | Implementation recommendation |
| --- | --- | --- |
| Research and operator tools | Query market context, inspect accounts, investigate datasets, produce reports | Official MCP servers in a separate assistant/research environment |
| Trading application | Ingest streams, validate state, size positions, submit/cancel orders, reconcile fills | Direct broker/data APIs and WebSockets behind deterministic adapters |

MCP is useful for discovery and interactive workflows. Direct APIs provide explicit schemas, lifecycle control and event processing for this bot. This is an architectural choice for this repository, not a claim that MCP can never support automated systems.

### 3.1 Authority boundary

The normal order flow remains:

1. A strategy proposes a candidate using validated data.
2. The deterministic risk engine checks costs, capital, account rules and capabilities.
3. The existing approval mechanism creates an `ApprovedPlan`.
4. `SubmissionGateway` sends it through the selected `BrokerAdapter`.
5. Broker events and reconciliation update orders, positions and the ledger.

Research MCP tools may read results or prepare research artifacts. They must not independently call a broker's live `place_order` tool on the bot's trading account. If operator execution through MCP is ever introduced, it must enter the same approval/gateway path and be a separately reviewed design change.

Start with vendor read-only scopes where available. An MCP-level trading switch is not equivalent to a broker-enforced read-only API key. Do not expose funding, withdrawal, transfer, staking, or paid-data purchase tools to the initial research configuration.

## 4. Broker and exchange assessment

All providers listed here have official APIs. MCP capabilities differ by product and region. Public documentation does not certify eligibility for the owner's German-resident account.

| Provider | MCP status | API path | Why it belongs in this project | Limitation |
| --- | --- | --- | --- | --- |
| Kraken | Official local MCP bundled with Kraken CLI | Spot REST and WebSocket | Closest fit to existing crypto data and capability work | Local paper simulation does not establish exchange fill behaviour |
| Capital.com | Official local open-source MCP | REST and WebSocket | Existing first CFD demo candidate; gold/silver market discovery and positions | Current source defaults differ from documentation; API keys are trading-capable |
| Interactive Brokers | Official hosted MCP | Web API and TWS API | Broader international market coverage | Standard MCP uses review/approve; autonomous MCP has separate qualification |
| Coinbase | Official hosted Coinbase for Agents MCP | Advanced Trade REST and WebSocket | Alternative crypto venue; existing candle ingestion | Advanced Trade sandbox uses static mocked responses |
| cTrader | Official local and remote MCP | cTrader Open API | Alternative FX/metals/CFD platform integration | Requires an affiliated broker; remote MCP relies on an active web session |
| IG | Official read-only MCP documented by IG Australia | REST and streaming APIs | Secondary CFD data/execution option | MCP cannot execute trades; German-account MCP availability unverified |

Sources: [Kraken MCP] [S01], [Kraken API] [S02], [Capital.com] [S04], [IBKR MCP] [S07], [IBKR API] [S08], [Coinbase MCP] [S09], [Coinbase API] [S10], [Coinbase sandbox] [S11], [cTrader MCP] [S12], [cTrader API] [S14], [IG MCP] [S15], [IG API] [S16].

### 4.1 Kraken: first crypto connection

Start with public market tools and the CLI's local paper tools. Both can be explored without account credentials. An illustrative stdio configuration appears in section 6.

Implementation tasks:

- Reuse public metadata ingestion and extend it to streaming bid/ask/trades with exchange timestamps.
- Resolve current pair identifiers, quantity steps, minimums and account fee tiers.
- Implement private account/order/fill handling only inside a broker adapter.
- Compare results using matching venue data; retain provenance if Coinbase remains a proxy.
- Distinguish local simulation, qualified-client test environments and live exchange execution in every report.

The default CLI examples include a feedback tool that can upload content. The proposed scope explicitly selects only `market,paper`. Qualified-client API test access is a separate Kraken onboarding path. [S01] [S03]

### 4.2 Capital.com: first commodity/CFD demo connection

The official server supports market/account inspection and a preview/execute workflow. The current source sets `cap_allow_trading=True`, although the API page says trading is disabled by default. Configure the chosen release explicitly rather than relying on defaults. Capital.com states that read-only API keys are not available. [S04] [S05][S06]

Initial investigation configuration:

```dotenv
CAP_ENV=demo
CAP_ALLOW_TRADING=false
CAP_DRY_RUN=true
```

When implementing the deterministic demo adapter, obtain current instrument metadata from the API. Do not assume that a search label such as GOLD is the exact tradable EPIC. Calculate risk using contract size, quantity increment, minimum stop distance, spread, account currency and financing rules. Confirm execution by deal reference and close by the correct deal ID.

Demo trade testing can be enabled for a dedicated demo account within the implementation scope. Keep live credentials and live enablement separate.

### 4.3 IBKR: longer-term expansion

Official MCP endpoint: `https://api.ibkr.com/v1/api/mcp-public`.

Standard AI integration requires the user to review and submit orders. The separately documented autonomous mode requires an eligible individual non-margin account and, currently, ten trades of at least USD 100 each over two weeks in review-and-approve mode. A qualifying test is described as coming soon, not available evidence to rely on. This is not an immediate match for the USD 50 experiment. These conditions concern autonomous MCP mode; conventional API requirements must be assessed separately. [S07]

Before building an adapter, verify actual account availability, authentication/session maintenance, market-data entitlements, commissions, fractional-order support and protective orders through the selected API. Choose Web API or TWS based on those verified requirements. [S08]

### 4.4 Coinbase: alternative crypto venue

Official account-trading MCP endpoint: `https://agents.coinbase.com/mcp`, using OAuth. This is distinct from Coinbase wallet/payment MCP products. Use a dedicated portfolio where supported and request only the access needed. Do not enable x402 purchases in this project's initial configuration. [S09]

Advanced Trade offers REST order management and WebSocket data/account updates. Its static sandbox can validate response parsing, but cannot certify realistic fills or partial-fill behaviour. Preserve that distinction in capability evidence. [S10] [S11]

Implement Coinbase if its verified fees, available pairs, account access or execution behaviour solve a requirement that Kraken does not. Existing public Coinbase Exchange candles are not automatically equivalent to authenticated Advanced Trade integration.

### 4.5 cTrader and IG: alternatives

cTrader is a platform used by brokers, not the broker itself. Both MCP modes expose trading and market/account tools; remote MCP requires an active cTrader Web session and an account-specific token. Prefer Open API for an unattended bot, subject to application/account setup. [S12] [S13][S14]

IG Australia documents a strictly read-only MCP connection. IG's conventional APIs support trading and streaming, but MCP availability for this owner's entity remains unverified. Do not choose IG solely because an MCP page exists. [S15] [S16]

## 5. Data-server assessment and cost report

The following are data services, not execution brokers. An MCP connection inherits the provider account's entitlements and limits; it does not unlock every advertised dataset.

| Provider | Best use in this repo | Verified connection | Free/paid considerations observed on 8 Nov 2026 |
| --- | --- | --- | --- |
| Alpha Vantage | Earnings calendar, fundamentals, news sentiment, macro/commodity context | `https://mcp.alphavantage.co/mcp` | Standard free allowance: 25 API requests/day; premium endpoints remain restricted |
| FMP | Financial statements, company/event research, filings and transcripts | `https://financialmodelingprep.com/mcp` | Basic: 250 calls/day with restricted data; corporate calendars and transcripts are listed in paid tiers |
| Massive | US-equity historical data, corporate actions and plan-dependent quotes/trades | `https://mcp.massive.com/` | Free stocks: end-of-day availability, 5 requests/minute, 2 years history; listed real-time Stocks Advanced: USD 199/month |
| Twelve Data | Cross-asset and international price research; an alternative provider | `https://mcp.twelvedata.com/mcp` | Basic: 8 API credits/minute and 800/day; requests can consume multiple credits; markets/streaming depend on plan |
| CoinGecko | Crypto breadth, rankings, volume, exchange context and discovery | `https://mcp.api.coingecko.com/mcp` | Public beta currently needs no key; variable fair-use limits; Pro uses paid API access |

Sources: [Alpha Vantage server] [S17] and [limits] [S18]; [FMP server] [S19] and [pricing] [S20]; [Massive server] [S21] and [pricing] [S22]; [Twelve Data server] [S23] and [pricing] [S24]; [CoinGecko server] [S25]. Prices, limits and availability must be rechecked when implementing or purchasing.

### 5.1 Selection rationale

- Start with Alpha Vantage for occasional cached context, not continuous price polling. Fetch only entitled endpoints. News features belong after the price-only pipeline works, consistent with the repo's ML guide.
- Add CoinGecko for crypto discovery. Execution decisions still require current quotes from the actual venue.
- Evaluate FMP when earnings, fundamental or transcript data is a concrete research requirement. Its free tier must not be assumed to include those premium datasets.
- Evaluate Massive when deeper US-stock history, consolidated coverage or executable quote research is needed. Its free access is not a real-time quote feed.
- Evaluate Twelve Data as a cross-asset alternative. Verify coverage and feed semantics before treating it as a replacement for another vendor.

At USD 50 starting capital, a USD 199 monthly subscription equals 398% of the starting trading balance. That arithmetic illustrates why paid data needs a separate research budget and measurable benefit; subscription spending is not justified by the number of available tools.

### 5.2 Data quality requirements

For every provider:

1. Record provider, instrument, venue/feed, event time, availability time and ingestion time where supplied; mark missing timing fields honestly.
2. Record timezone, currency, adjustment policy, pagination/window boundaries, entitlement and any known delay.
3. Normalize to existing domain types with exact financial arithmetic.
4. Separate live data, delayed data, historical data and synthetic fixtures.
5. Preserve source/transform lineage and checksums through `research/data_manifest.json` or an approved extension.
6. Reject stale, malformed, incomplete or contradictory input rather than converting it into a valid trading observation.
7. Do not mix IEX-only volume, consolidated equity volume and another venue's crypto volume as interchangeable features.
8. Store earnings/calendar/news observations as they were available at decision time. A calendar retrieved today does not establish what was known historically.
9. Cache low-frequency fundamentals and events. Use direct streaming APIs for prices that need sub-minute freshness.
10. Treat news and tool output as untrusted data; it cannot change policy, credentials, tool permissions or execution authority.

Historical news features can contain hindsight leakage through revised data or pretrained LLM knowledge. Require appropriately timestamped inputs and forward evidence before relying on them in trading.

## 6. MCP connection instructions

These are connection templates, not claims of successful installation. Validate against the chosen MCP client's current schema and the provider's current release. Do not replace the owner's existing Alpaca entry or entire MCP configuration.

### 6.1 Connection inventory

| Server | Transport/setup | Authentication | Initial access |
| --- | --- | --- | --- |
| Alpaca | Preserve owner's current setup | Existing credentials; inspect names without exposing values | Existing paper/data access |
| Kraken | Local stdio, `kraken-mcp -s market,paper` | None for these selected groups | Market data and local simulation |
| Capital.com | Official local server or container | Demo API key, identifier, separate API password; account setup requires 2FA | Demo reads; trading disabled |
| Alpha Vantage | Hosted MCP over HTTP | Provider OAuth flow authorizes the API key | Entitled read-only data |
| CoinGecko | Hosted Streamable HTTP | None for public beta | Public research data |
| Massive | Hosted MCP | OAuth | Plan-entitled data |
| Twelve Data | Hosted MCP | OAuth using Twelve Data account | Plan-entitled data |
| FMP | Hosted MCP | `X-API-Key` header; custom-app OAuth requires provider registration | Plan-entitled data |
| IBKR | Hosted MCP | Broker-controlled authorization | Account/research access appropriate to enabled mode |
| Coinbase | Hosted MCP | Coinbase OAuth | Portfolio-scoped read access where supported |
| cTrader | Local MCP or remote configuration from cTrader Web | Account-specific setup/token | Selected demo account |
| IG | Official app flow documented for IG Australia | OAuth | Read-only, if available for the actual account |

Provider installation details: [S01], [S04], [S07], [S09], [S13], [S15], [S17], [S19], [S21], [S23], [S25].

### 6.2 Minimal assistant configuration example

For clients that support the common `mcpServers` JSON shape and an HTTP `url` field:

```json
{
  "mcpServers": {
    "kraken-research": {
      "command": "kraken-mcp",
      "args": ["-s", "market,paper"]
    },
    "alpha-vantage-research": {
      "url": "https://mcp.alphavantage.co/mcp"
    },
    "coingecko-research": {
      "url": "https://mcp.api.coingecko.com/mcp"
    }
  }
}
```

Install a reviewed, pinned release of the official Kraken CLI first and ensure `kraken-mcp` is on PATH, or use its absolute installed path. Complete Alpha Vantage authentication through the provider flow. Merge these entries with the existing configuration; do not delete Alpaca.

Some clients use different keys or TOML rather than this JSON layout. Adapt the structure using that client's documentation. An HTTP endpoint alone is not authentication or proof of authorization.

### 6.3 Capital.com local setup

Use only the official repository: https://github.com/capital-com-sv/capital-mcp . Resolve and record a reviewed commit/release or container digest before installing. The official repository supports local Python and Docker operation. [S05]

Keep the following in a private, ignored environment file or supported secret store, not in this plan or a committed MCP config:

```dotenv
CAP_ENV=demo
CAP_API_KEY=REPLACE_LOCALLY
CAP_IDENTIFIER=REPLACE_LOCALLY
CAP_API_PASSWORD=REPLACE_LOCALLY
CAP_ALLOW_TRADING=false
CAP_DRY_RUN=true
```

The placeholders are illustrative and must not be used as credentials. The API password is distinct from the normal account password.

A Docker-based stdio launch can use the documented `docker run -i --rm --env-file ...` form with the pinned official image. Do not allocate a TTY for MCP stdio. Do not expose a local HTTP listener publicly. Match the client's executable/args schema and the host OS.

Acceptance for initial connection: authenticate to demo, inspect the reported account/environment, search gold and silver instruments, retrieve metadata and prices, and confirm trading tools cannot execute while disabled. Do not place a real order as a connection test.

### 6.4 Optional remote servers

Add only the servers selected for a concrete task. Use the exact endpoint from the inventory.

- FMP: prefer `X-API-Key` headers over query-string secrets. Resolve the key through the client's supported secret mechanism. Do not commit literal values or assume `${NAME}` interpolation works. Custom OAuth clients need provider registration. [S19]
- Massive and Twelve Data: authenticate with the provider and inspect dataset entitlements before requesting large backfills. [S21] [S23]
- IBKR and Coinbase: begin with account inspection and minimum supported scopes. A successful OAuth flow does not establish product eligibility or autonomous trading permission. [S07] [S09]
- cTrader: obtain configuration from the selected account's current Remote MCP settings. Do not invent a universal token or server URL; session expiry is an explicit dependency. [S13]

### 6.5 Connection evidence

For each enabled server, record in `docs/integrations/connection_report.md`:

| Field | Required evidence |
| --- | --- |
| Identity | Provider, official source URL, local version/commit/digest where applicable |
| Client | MCP client and version; config location without secrets |
| Transport | stdio/HTTP and configured endpoint or executable |
| Authorization | Auth type and actual granted scopes; no token values |
| Environment | Public research, local simulation, broker demo/paper, or live read-only |
| Discovery | Successful initialization and discovered-tool list or digest |
| Smoke test | One harmless read request, timestamp, outcome and latency |
| Entitlements | Available datasets, observed limits and unavailable endpoints |
| Limitations | Missing permissions, account restrictions, stale data or session dependency |
| Revocation | How the user/operator disables the connection |

Use distinct statuses: `planned`, `configured`, `authenticated`, `read_verified`, `paper_verified`, and `live_verified`. These are reporting labels, not replacements for the repository's existing capability-evidence enum. A configured server is not necessarily authenticated; a paper-verified adapter is not live-certified.

## 7. Proposed code and configuration changes

Check the current tree before creating paths. Reuse existing modules where possible. This layout is a proposal, not an inventory of files that already exist.

| Path | Responsibility |
| --- | --- |
| `configs/mcp/servers.example.json` | Non-secret assistant connection examples; no automatic activation |
| `configs/integrations/providers.yaml` | Enabled providers, roles, dataset limits, freshness and non-secret settings |
| `docs/integrations/connection_report.md` | Observed connection status and evidence |
| `docs/integrations/runbook.md` | Setup, restart, reauthorization, outage handling and rollback |
| `src/trader/data/sources/` | Extend existing source modules; add selected vendor API clients/normalizers |
| `src/trader/brokers/alpaca/` | First concrete paper adapter |
| `src/trader/brokers/kraken/` | Venue adapter when implementation reaches that phase |
| `src/trader/brokers/capital_com/` | CFD demo adapter |
| `research/mcp/` | Optional MCP research client/orchestrator, outside the trading runtime |
| `tests/` | Adapter contract, fault, data-normalization and authority-boundary tests |

Do not add all optional providers as mandatory runtime dependencies. Keep MCP/LLM dependencies in a separate group or process. Introduce selected broker SDK/network dependencies only where needed.

### 7.1 Required boundary decision

At the inspected snapshot, boundary tests ban `alpaca`, `requests` and `httpx` from runtime dependencies and confine low-level networking to the market-data transport. A working broker adapter will require an explicit, narrow change.

Record an architecture decision that allows networking inside approved broker transport modules and runtime composition only. Update tests to preserve the real invariant: strategies, models, features and research cannot reach order authority. Do not disable the boundary test suite or replace it with an unrestricted networking allowance.

### 7.2 Adapter acceptance contract

For each selected broker, demonstrate:

- Account identity, currency, environment, balances and actual buying-power semantics.
- Instrument identity and current executable increments/minimums.
- Order submission, cancellation, rejection and lifecycle mapping.
- Partial fills, cumulative filled quantity and fee reconciliation.
- Client-order-ID semantics; do not assume every venue deduplicates repeated IDs.
- A timeout after broker acceptance followed by safe lookup/reconciliation.
- Disconnect/reconnect, event replay and process restart without duplicate orders.
- Native protective-order capability at the intended instrument and size before live use.
- Rate-limit behaviour and safe retry policy: bounded retries for reads, no blind order retries.
- Separation of paper/demo and live credentials, endpoints, ledgers and evidence.

Multiple execution venues must never be used as automatic failover for an order with an unknown outcome. Reconcile the original venue before considering a new order elsewhere.

## 8. Implementation sequence and completion criteria

### Phase 0 — Reconcile the current checkout

- [x] Read repository instructions and the existing protected specifications.
- [x] Record the working branch/commit and compare with the inspected snapshot.
- [x] Locate the reported Alpaca MCP/API setup without printing credentials.
- [x] Identify whether any paper/live adapter work already exists locally or on another relevant branch.
- [x] Record selected providers and retain the current USD 50 policy.

Complete when the plan reflects the actual current code and connection state rather than only the review snapshot.

### Phase 1 — Verify research MCP connections

- [x] Preserve and inspect Alpaca.
- [x] Add Kraken market/local-paper scopes. (kraken-cli v0.5.1 built from the tag; `-s market,paper`, 28 tools.)
- [x] Add CoinGecko public MCP.
- [x] Add Alpha Vantage with the owner's API entitlement when credentials are available. (Configured with the provider's OAuth sign-in; the owner signs in with `/mcp`.)
- [ ] Record actual tools, scopes, smoke tests and quotas.
- [x] Keep unavailable authentication-dependent work explicitly pending while continuing independent development.

Complete when selected read tools return valid data and the connection report distinguishes working, blocked and merely configured services.

### Phase 2 — Finish stock data and Alpaca paper execution

- [x] Verify and run the existing stock-history ingestion with the intended feed.
- [x] Resolve the stock cost basis and dataset limitations before registering new research.
- [x] Implement the narrow broker-networking architecture decision.
- [ ] Implement/certify the Alpaca paper adapter against the existing order contract. (Implemented with a contract suite, ADR-0033; certification orders wait on the owner, ADR-0035.)
- [ ] Run reconciliation and failure tests, including timeout-after-acceptance.

Complete when the bot can operate its existing strategy/risk pipeline against paper state without bypassing the gateway. A strategy without accepted edge may validly produce no entries.

### Phase 3 — Add selected research data

- [ ] Implement direct API ingestion for the chosen event/fundamental source.
- [ ] Add caching, quota limits, source timestamps, provenance and schema validation.
- [ ] Define each new feature before evaluating it; preserve a price-only baseline.
- [ ] Register comparisons within the existing experiment-budget rules.
- [ ] Report whether the added data improves out-of-sample economics after costs.

Complete when data is reproducible and the comparison has an explicit result, including a possible no-improvement result. Adding news features does not bypass the existing ML research sequence.

### Phase 4 — Crypto venue integration

- [ ] Extend Kraken streaming and venue-matched history where available.
- [ ] Verify actual pair minimums, fees and account capabilities.
- [ ] Use simulation/test access with its evidence class clearly labelled.
- [ ] Implement private order handling only behind `BrokerAdapter`.
- [ ] Evaluate Coinbase as an alternative only if verified economics or access justify it.

Complete when data/account behaviour is demonstrated to the level claimed. Never label the local Kraken simulator or Coinbase static mocks as exchange-certified fills.

### Phase 5 — Commodity/CFD demo integration

- [ ] Configure Capital.com demo with explicit initial read-only application settings.
- [ ] Retrieve gold/silver metadata and validate contract arithmetic.
- [ ] Implement a demo adapter with deal confirmation and close-by-ID semantics.
- [ ] Model spreads, financing, stops, margin and account-currency conversions.
- [ ] Exercise demo fills, cancellation, rejection and restart behaviour.

Complete when the demo passes the existing risk/ledger boundaries and has a documented feasibility verdict. Keep the commodity live track disabled unless existing promotion requirements are separately satisfied.

### Phase 6 — Optional expansion

- [ ] Select FMP, Massive or Twelve Data only against a concrete dataset gap and budget.
- [ ] Assess IBKR account/API feasibility before implementation.
- [ ] Evaluate cTrader or IG only when they solve a requirement left unmet by Capital.com.
- [ ] Retire redundant feeds or unused servers after comparison.

Any live rollout remains governed by the original promotion gates and explicit owner enablement. This document does not mark those gates as passed.

## 9. Verification, observability and rollback

### 9.1 Verification scope

Use fixtures for normalization and fault injection; use real read-only/paper requests for integration evidence. Tests must verify behaviour, not simply mirror the implementation.

| Failure | Required behaviour |
| --- | --- |
| MCP initialization or authorization fails | Record unavailable status; do not claim a working connection |
| Tool/schema changes | Stop affected workflow and report incompatibility |
| Data limit or HTTP 429 | Bounded backoff and quota accounting; no uncontrolled paid fallback |
| Quotes stale or feed interrupted | Reject new entries that depend on invalid data; retain protective/reconciliation work |
| Wrong account or environment | Refuse the operation before submission |
| Order request times out | Preserve unknown-outcome state and reconcile; do not resend blindly |
| Fill events arrive twice or out of order | Deduplicate/reconcile without double-counting cash or position |
| Credential expires | Surface the provider-specific reauthorization requirement; no silent switch to another account |
| Optional research server fails | Disable that research feature; do not destabilize order management |
| Local process restarts | Reconcile broker state before accepting new entries |

### 9.2 Metrics and logs

Record provider latency, data age, disconnects, quota consumption, authentication failures, order acknowledgement delay, unresolved orders, fill reconciliation differences and realized execution costs. Redact credentials and authorization headers. Store version and source references with integration evidence.

Respect the existing two-second quote-age and other policy checks unless a separately justified strategy-specific change is accepted; do not relax freshness just to make a delayed data subscription pass.

### 9.3 Rollback

Disable a provider in configuration and remove it from research routing. Revoke its token/key if retiring the connection. Preserve historical provenance and evidence rather than rewriting past reports. If orders or positions exist, reconcile them before shutting down the adapter; retain risk-reducing exits and native protection.

Do not automatically liquidate positions, cancel all protective orders, move capital or switch execution venues as a generic rollback action.

## 10. Coding-agent handoff

Implement this plan incrementally in this repository. Start by reconciling the current checkout and existing Alpaca setup, then finish the Alpaca data/paper path while adding the selected research MCP connections. Preserve protected specification files, the shared USD 50 policy, deterministic order authority and promotion gates. Use official provider sources and pinned local server versions. Keep MCP research separate from the execution runtime; implement execution through broker adapters and `SubmissionGateway`. For each phase, deliver code/configuration, meaningful validation and a connection report stating exactly what was tested, what remains unverified, and what depends on owner credentials or a budget decision. Do not mark setup, paper evidence or live certification complete without corresponding results.

## 11. Source register

Provider documentation was reviewed on 8 November 2026. It may change. Repository links in this report point to the current files; the review was made against the 8 November 2026 snapshot.

### Project evidence

- [README and milestone status](../../README.md)
- [Broker adapter contract](../../src/trader/brokers/base.py)
- [Submission gateway](../../src/trader/execution/_gateway.py)
- [Boundary tests](../../tests/test_boundaries.py)
- [Risk policy](../../configs/policy/base.yaml)
- [Broker capability report](../broker_capabilities.md)
- [Research baseline report](../../research/reports/baseline_report.md)
- [ML and LLM research guide](../ml_research_guide.md)

### Provider sources

| ID | Primary source |
| --- | --- |
| S01 | [Kraken official CLI/MCP repository] [S01] |
| S02 | [Kraken API documentation directory] [S02] |
| S03 | [Kraken advanced API testing FAQ] [S03] |
| S04 | [Capital.com official AI integration overview] [S04] |
| S05 | [Capital.com official MCP repository] [S05] |
| S06 | [Capital.com API documentation] [S06] |
| S07 | [IBKR AI/MCP and autonomous-mode requirements] [S07] |
| S08 | [IBKR API documentation] [S08] |
| S09 | [Coinbase account-trading MCP documentation] [S09] |
| S10 | [Coinbase Advanced Trade API] [S10] |
| S11 | [Coinbase Advanced Trade sandbox] [S11] |
| S12 | [cTrader MCP overview] [S12] |
| S13 | [cTrader remote MCP setup/session requirements] [S13] |
| S14 | [cTrader Open API] [S14] |
| S15 | [IG Australia official read-only MCP] [S15] |
| S16 | [IG REST and streaming APIs] [S16] |
| S17 | [Alpha Vantage official MCP repository] [S17] |
| S18 | [Alpha Vantage usage limits] [S18] |
| S19 | [FMP official MCP and authentication] [S19] |
| S20 | [FMP pricing and dataset entitlements] [S20] |
| S21 | [Massive official MCP quickstart] [S21] |
| S22 | [Massive stocks coverage and pricing] [S22] |
| S23 | [Twelve Data official MCP repository] [S23] |
| S24 | [Twelve Data pricing and credits] [S24] |
| S25 | [CoinGecko public MCP] [S25] |

[S01]: https://github.com/krakenfx/kraken-cli
[S02]: https://support.kraken.com/articles/206548387-where-can-i-find-documentation-for-the-api-
[S03]: https://support.kraken.com/articles/advanced-api-faq
[S04]: https://capital.com/en-int/trading-platforms/ai-integration
[S05]: https://github.com/capital-com-sv/capital-mcp
[S06]: https://open-api.capital.com/
[S07]: https://www.interactivebrokers.com/en/trading/ai-integrations.php
[S08]: https://www.interactivebrokers.com/campus/ibkr-api-page/
[S09]: https://docs.cdp.coinbase.com/ai-agents/coinbase-for-agents/coinbase-mcp
[S10]: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/overview
[S11]: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox
[S12]: https://help.ctrader.com/ctrader-ai-agent-connect/
[S13]: https://help.ctrader.com/ctrader-ai-agent-connect/remote-mcp/setup/
[S14]: https://help.ctrader.com/open-api/
[S15]: https://www.ig.com/au/trading-platforms/ai-assistant
[S16]: https://labs.ig.com/
[S17]: https://github.com/alphavantage/alpha_vantage_mcp
[S18]: https://www.alphavantage.co/support/
[S19]: https://site.financialmodelingprep.com/developer/docs/mcp-server
[S20]: https://site.financialmodelingprep.com/developer/docs/pricing
[S21]: https://massive.com/docs/ai-tools/quickstart
[S22]: https://massive.com/stocks
[S23]: https://github.com/twelvedata/mcp
[S24]: https://twelvedata.com/pricing
[S25]: https://mcp.api.coingecko.com/
