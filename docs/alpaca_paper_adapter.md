# Alpaca paper adapter (M4)

`src/trader/brokers/alpaca/` implements the `BrokerAdapter` contract (architecture.md §4, §7) for an **Alpaca paper account** with the pinned official SDK (`alpaca-py==0.44.0`, ADR-0025). The same contract suite runs against the replay broker and this adapter (`tests/test_alpaca_adapter.py`, no network).

## Paper only

- The constructor refuses any mode except `paper`, a capability profile whose environment is not `paper` or whose broker is not `alpaca`, and a key ID that looks like a live key (`AK...`).
- No parameter takes a URL. The SDK clients are created with `paper=True` and no override, so they reach `paper-api.alpaca.markets` only. A test fails if a live host or an override appears in the package.
- `runtime/live_guard.py` certifies no live adapter; `trader live` still exits 99.

## Before the first paper run (owner)

1. Store the paper keys: `uv run trader secrets set alpaca-paper` (docs/owner_setup.md §2).
2. Flatten the paper account and cancel its open orders. The adapter reports order and execution **history** only from its `since` time, but **open orders and positions always in full**: anything left over fails reconciliation and pauses entries.
3. Record real asset metadata in the instrument catalogue. `AlpacaPaperAdapter.verify_instruments()` lists every difference (asset id, tradable, fractionable). Entries are refused for instruments that do not certify.

## Outcomes

| Alpaca answer | Adapter result | What happens next |
| --- | --- | --- |
| 2xx order | `accepted=True` | the submit response is also queued as the first order event |
| 4xx (403 buying power, 422 validation, 401) | `accepted=False`, reason with HTTP status and code | intent `REJECTED`, reservation released |
| 422 "client_order_id must be unique" | `DuplicateClientOrderId` (a `BrokerTimeout`) | the order **exists**: unknown outcome, resolved by client ID |
| timeout, connection/TLS error, 408, 429 after retries, 5xx, unreadable body | `BrokerTimeout` | intent `UNKNOWN_OUTCOME`, entries pause, reconciler resolves by client ID |
| order shape this system never sends, uncertified entry, short sale, unusable fractional capability | `accepted=False` | nothing was sent |
| cancel timeout or transport error | `BrokerTimeout` | the gateway records `CANCEL_OUTCOME_UNKNOWN`, pauses entries (`UNKNOWN_ORDER`); the intent is not marked `CANCEL_REQUESTED` and its exposure stays reserved |
| cancel of a closed or unknown order (404, 422) | `request_accepted=False` | nothing changes; the order's events decide |

The SDK's automatic retry is limited to 429 (it also retries 504 by default, which could re-send an order that was accepted). Every request has a 10-second timeout and passes a token bucket on the injected clock (200 requests per minute unless the profile states a limit; no 60-second window exceeds it).

## Events, executions and versions

- `broker_version` is the order's `updated_at` in epoch microseconds (nanosecond digits truncated), so the stream and REST views of one state carry the same version and event id `"{order_id}:v{version}"`. The columns are 64-bit (migration `0002`).
- Executions come **only** from REST `FILL` account activities; the activity `id` is the execution id. Alpaca does not document that the stream's `execution_id` equals it (checked 2026-11-08), so a stream fill triggers a REST fetch, retried until the activities cover the order's filled quantity.
- The `trade_updates` stream runs in a background thread. It is never the source of truth: after every (re)connection the adapter re-reads its working orders over REST, while no stream runs it re-reads them at most every 5 seconds (injected clock), and the reconciler compares REST views.
- `drain_events` emits executions from the adapter's `since` time on; `list_executions(broker_order_id=...)` always returns an order's complete history (Alpaca's documented `order_id` filter). `list_executions()` reads activities newest first and stops at the first one older than `since`: the documented `after` filter refers to `created_at`, which is null for FILL activities, so no server-side time filter is relied on.
- `list_orders` reads both of Alpaca's lists (closed orders from `since`) and classifies by the mapped status, so an order Alpaca files as closed but that is still working for this system (for example `done_for_day`) is reported as open and keeps its exposure.
- Two different states of one order with the same `updated_at` microsecond would share a version; the OMS then ignores the second status (fills are always posted), the intent stays live and reconciliation fails closed.
- Fills carry fee `0` with status `PENDING`. Alpaca reports regulatory fees later as separate activities, which the adapter does not read yet; until fees are finalised, reconciliation can report a cash difference above one cent and pause (fail closed).

## Order statuses

Every Alpaca status (documented, or in the pinned SDK's `OrderStatus` enum) has an explicit rule (`STATUS_RULES` in `mapping.py`; a test compares it with the SDK enum). Working statuses the domain does not distinguish (`new`, `accepted`, `pending_new`, `pending_review`, `accepted_for_bidding`, `held`, `stopped`, `done_for_day`, `calculated`) map by filled quantity and are never terminal unless fully filled, so exposure stays reserved. `replaced`, `pending_replace`, `suspended` and any undocumented value raise `UnmappedStatus`: the system never replaces orders, so these need owner review. `drain_events` keeps the events it could map for the next call and raises; `OrderManager.pump` turns that into a `BROKER_EVENTS_UNAVAILABLE` alert and an `UNKNOWN_ORDER` pause instead of stopping the run loop. REST lookups raise as well, so reconciliation cannot pass while such an order exists.

Every definite refusal (local or from Alpaca) is kept by client order ID in `rejections`, so owner reports can say why an order was not accepted.

## Paper evidence still to collect

The documented profile (`configs/capabilities/alpaca_documented.yaml`) certifies no order visibility values, no client-ID uniqueness and no fractional limit orders. Until paper runs demonstrate them, unknown outcomes stay `UNRESOLVED` (entries frozen) and fractional entries are refused.

`trader paper certify` collects this evidence (docs/runbook.md section 9.1). For that run only, the adapter is built with the run's `CertificationBudget` (domain/certification.py):

- Only plans whose `model_hash` names the run's grant may use the capabilities under test (fractional LIMIT DAY and MARKET DAY buys). Each such buy must stay within USD 5: `reserved_cash_usd`, and quantity times limit price for a limit.
- Every submission of the adapter counts against the run's order budget, the round trip's exit included. Once the budget is used or the grant has expired, the order is refused and nothing is sent.
- `asset_metadata()` reads `GET /v2/assets/{symbol}`. `market_clock()` reads `GET /v2/clock`. `rate_limit_observation()` returns the newest `X-RateLimit-*` response headers; the SDK transport keeps only those three headers.

An adapter built without a budget, as in every ordinary paper run, behaves exactly as described above.
