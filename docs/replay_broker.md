# Replay broker: fill rules and faults

`src/trader/brokers/replay/` implements the same adapter contract as the future paper adapter (`brokers/base.py`). Its rules are deliberately conservative (risk_and_validation.md §6) and are tested in `tests/test_execution_adversarial.py` and `tests/test_exits.py`.

## Fill rules

| Rule | Behaviour |
| --- | --- |
| Latency | An order can only execute against quotes whose event time is at least 500 ms after the broker accepted it. Execution is never at the signal bar's close. |
| Prices | Buys execute at the ask, sells at the bid, plus optional slippage in bps. Buy prices round **up**, sell prices **down**, at 0.0001. |
| Limits | A limit executes only if the executable price respects it, and then at that price. A bar touching a price is not a fill. |
| Liquidity | Each quote fills at most `participation × displayed size` (default 50%). `PARTIAL_FIRST_FILL` forces a deterministic partial fill. |
| Halts | Nothing executes while halted. After a resume, the first quote is used even if it gaps through the stop: stops are triggers, not guaranteed prices. |
| Cancels | A cancel request is acknowledged (`PENDING_CANCEL`) but takes effect on the next market event, so fills can race it. |
| DAY orders | Expire at the session close. |
| Fees | Charged per execution at the schedule's granularity (rounded up). |
| Selling | A sell larger than the held quantity minus open sells is rejected. There is no shorting. |
| Client order ID | Unique at the broker. A repeated ID is rejected and never filled twice. |
| Authority | `submit` requires the authority object bound by the current submission gateway, and an authentic, unexpired `ApprovedPlan`. A restarted gateway replaces the old authority, so a stale worker cannot submit. |

The replay account holds USD 100,000, like a paper account. The internal USD 50 subledger governs everything. Reconciliation compares cash *deltas* against the baseline recorded at account opening.

## Fault catalogue (`FaultKind`)

| Fault | Effect |
| --- | --- |
| `TIMEOUT_BEFORE_ACCEPT` | Raises `BrokerTimeout`; the order never reaches the book |
| `TIMEOUT_AFTER_ACCEPT` | The order is accepted, then `BrokerTimeout` is raised |
| `CRASH_AFTER_ACCEPT` | The order is accepted, then `SimulatedCrash` (a `BaseException`) kills the worker |
| `DELAYED_VISIBILITY(n)` | The first *n* client-ID lookups return nothing |
| `SUPPRESS_STREAM_EVENTS` | The account stream loses the order's events; only REST reconciliation can see them |
| `REJECT` | Definitive rejection |
| `PARTIAL_FIRST_FILL(f)` | The first execution is capped at fraction *f* |
| `FILL_AFTER_CANCEL_REQUEST` | A fill is processed before a pending cancel |
| `DUPLICATE_EVENTS` / `OUT_OF_ORDER_EVENTS` | The stream duplicates or reverses events |
| `FEE_PENDING` | Fees arrive as estimates (accrued, then finalised) |
| `inject_manual_position()` | Someone trades the account outside the system |

## Limits of bar- and quote-level replay

- The synthetic data has 30-second granularity. It can exercise the software but **cannot certify execution realism** for a spread-sensitive strategy (risk §6, last paragraph).
- Queue position, hidden liquidity, auction prints and market-wide halts are not modelled.
- Stressed-friction runs (2× spread/slippage) arrive with real data in M2.
