# Project status

Where the project stands against its plan: what works, what the research found, the milestones (M0 to M7), the release gates (G0 to G5), the detailed checklist and the next steps. What is traded on paper today, and the evidence for each rule, is in [strategy_playbook.md](strategy_playbook.md).

> **Where we are (2026-11-09): the machinery is built; no strategy has an edge.**
> Built and tested (538 tests on that date, 1,277 today): the safety core, a real-data research pipeline, a machine-learning filter pipeline, and an Alpaca **paper** bot with keys in the OS keyring. Every rule tested on real data **loses money after costs and is not positive even before costs**: crypto (BTC/ETH, 2023-2026) and stocks (AAPL, MSFT, GOOGL, AMZN, NVDA, 5-minute bars, 2020-2026). A machine-learning filter on the crypto rules could not even be trained: after costs only about 1% of their candidates are profitable, too few to learn from. The paper bot therefore records every decision and rejects every candidate (`NO_EDGE`), which is the intended safe behaviour.
> Roughly: **M0-M2 done, M3 built and tested on crypto, M4 built and certified on the Alpaca paper account (2026-11-09), M5-M7 not started.**
>
> **Update 2026-11-11: paper books trade under their own rules; the real-money rules below are unchanged.**
> 26 trials are registered and 25 are run: none passes gate G2, and one slow rule (weekly crypto momentum, T-0019) is net positive in every check at about 2% a year. All results and what is traded are in [docs/strategy_playbook.md](strategy_playbook.md). Since the owner's decisions of 9 to 11 Nov the bot also trades without a validated model, on **paper and demo money only**: a USD 300 exploration book of 20 stocks on the Alpaca paper account (`npm run bot`) and two USD 100 sleeves on a Capital.com demo account (`npm run trader`); `npm run day` starts everything. Their limits are looser than the USD 50 rules described here and are set per book ([ADR-0064](decisions.md), [runbook](runbook.md) section 0). Nothing in them is evidence for a gate, and live trading stays impossible.

## Status at a glance

| Works today | Does not exist yet |
| --- | --- |
| Risk policy, exact USD 50 sizing, persisted loss pauses, shared cross-track allocator | A strategy with a positive edge on real data, before or after costs |
| Order lifecycle, timeout recovery, reconciliation and exits, in replay and through the Alpaca paper adapter | A point-in-time stock universe and earnings calendar |
| Replay demonstrator: 14/14 acceptance checks, identical on SQLite and PostgreSQL | Paper trading sessions (gate G3 evidence) |
| Real-data research: crypto and stock baselines, hash-chained experiment registry, walk-forward statistics, reports | A live broker adapter (by design, until gates G0-G4 pass) |
| Model filters: candidate features, labelled dataset, filter hook, random and equal-turnover controls, logistic and LightGBM, numpy-only model bundles, model report | News or LLM features |
| Alpaca paper bot: `trader paper run / check / certify`, keys in Windows Credential Manager, a read-only data map for a dashboard (`docs/observability.md`), read-only Alpaca MCP access for Claude | Broker-held protective stops, an alert channel |

Legend used below: ✅ done · 🟡 partly done · ⬜ not started · ⛔ blocked until something else happens · ❌ failed.

## What the research found

| Study | Trials | Data | Result |
| --- | --- | --- | --- |
| Crypto baseline ([report](../research/reports/baseline_report.md)) | T-0001..T-0005: Candidate A 2x2 grid, Candidate B | BTC/USD, ETH/USD 15-minute bars, 2023-11..2026-08, Kraken Tier 1 fees | Every trial loses; not positive even with zero commission. G2 ❌ |
| Stock baseline ([report](../research/reports/stocks_baseline_v1_report.md)) | T-0006..T-0010: Candidate A 2x2 grid, Candidate B | AAPL, MSFT, GOOGL, AMZN, NVDA, IEX 5-minute bars, 2020-08..2026-08 | Every trial loses in all 24 three-month windows (net -60.72 to -93.38 USD summed over windows); mid-price gross before costs is negative too. G2 ❌ |
| Crypto filters ([report](../research/reports/model_report.md)) | T-0011, T-0012: logistic filter on Candidates A and B | as the crypto baseline | No fold could train a model (12 of 12 untrainable): after costs only about 1% of candidates are profitable (53 of 3,912 in the first fold) and several validation windows have none. The filter stays in cash (0 trades) while the rules alone lose 9.82 and 13.45 USD, and it does not beat the random or equal-turnover controls. Documented rejection; G2 ❌ |

Why: at USD 50 the policy risks at most USD 0.125 per trade, so positions are about USD 5-10. Spread, slippage and fees (about USD 0.04 per stock round trip under the conservative cost basis, 0.8% per side on Kraken) are larger than the small moves that 5- and 15-minute rules capture. A filter can only choose among a rule's candidates; it cannot create an edge they do not contain. "No strategy is profitable at USD 50" is a legitimate outcome of this project, and the gates make sure it is reported instead of traded.

Trial budget: 25 of 26 used (owner decisions through 2026-11-11). The free slot, T-0020, is reserved for weekly momentum across liquid US stocks. Only the owner raises the budget.

## Roadmap: planned milestones and where they stand

The plan is in [implementation_roadmap.md](specs/implementation_roadmap.md) §1. The windows are the roadmap's own indicative estimates; evidence windows (paper sessions) cannot be compressed.

| Milestone | Planned window | Goal (exit condition) | Status | What is left |
| --- | --- | --- | --- | --- |
| **M0** Feasibility | days 1-3 | Broker capabilities, cost model, data coverage; paper route usable, live blockers explicit | ✅ deliverables · ⛔ **G0 not passed** | Owner answers (residency, account type, funding) |
| **M1** Core safety | days 4-10 | Contracts, USD 50 ledger, locks, outbox, replay clock; deterministic accounting and invariants pass | ✅ in replay; paper contract suite passes against a fake Alpaca transport | The same cases on the real paper account (certification) |
| **M2** Research baseline | days 11-20 | Historical data, causal features, two baseline strategies, realistic replay reports; reproducible, no lookahead | ✅ crypto and stocks; **no edge in either** | Point-in-time universe and earnings calendar (documented limitations); optional Kraken archive |
| **M3** Model evaluation | days 21-35 | ML candidate filters with walk-forward comparison; a defensible challenger or a documented rejection | 🟡 pipeline built; crypto filters: documented rejection (untrainable) | Owner decision on the 3 reserved trials |
| **M4** Paper runtime | after M1/M2; then 30+ sessions | Alpaca paper adapter, streaming, reconciliation, incident tests; G1/G3 evidence | 🟡 adapter, runtime and certification built; certified on the paper account 2026-11-09 | At least 30 paper sessions and 20 round trips |
| **M5** Live readiness | only after G0-G4 pass | Disabled live adapter, protection proof, promotion manifest, owner enablement | ⛔ needs G0-G4 | Everything |
| **M6** Live observation | if M5 passes | Small live sample, cost/reliability comparison, go/no-go scaling report | ⛔ needs M5 | Everything |
| **M7** ASX expansion | separate, later | ASX gold equities feasibility | ⬜ | Everything; A$500 minimum parcels make it unsuitable at USD 50 |

Parallel tracks (one shared USD 50 budget, see [CRYPTO_TRADING_TRACK.md](specs/CRYPTO_TRADING_TRACK.md) and [COMMODITIES_AND_CFD_TRACK.md](specs/COMMODITIES_AND_CFD_TRACK.md)):

| Track | Status | Done | Left |
| --- | --- | --- | --- |
| **Stocks** (main track) | 🟡 | Data 2020-2026, baseline study (**no edge**), Alpaca paper adapter and runtime | Certification, paper sessions, a strategy with an edge |
| **Crypto** (BTC/ETH spot, Kraken first) | 🟡 | Cost report, 3 years of data, baseline (**no edge**), logistic filter study (**untrainable**) | Kraken adapter, forward paper, live pilot |
| **Commodities / CFD** (gold, silver) | 🟡 early | Contract units, certification, CFD sizing (margin is not exposure); gold CFD fixture is NO_TRADE at USD 50 | Real broker metadata, CFD ledger and financing accounting, data and replay, demo adapter |
| **News / LLM** | ⬜ | - | Starts only after the price-only pipeline finds an edge; offline research only, never in the trading loop |

## Release gates: what must be proven before real money

Defined in [risk_and_validation.md](specs/risk_and_validation.md) §8 and tracked in [research/promotion_manifest.json](../research/promotion_manifest.json).

| Gate | Requires | Status |
| --- | --- | --- |
| **G0** Broker and economics | Eligible account, certified capabilities, known fees, data and funding costs, plausible economics at USD 50 | ⛔ not passed: account decisions open; Alpaca fees now documented (conservative basis) |
| **G1** Software integrity | All accounting, permission, duplicate-order, protection, restart and failure tests pass | 🟡 passes in replay and against a fake Alpaca transport; paper certification passed 2026-11-09 (NVDA); restart and failure handling on real paper sessions still to show |
| **G2** Research evidence | ≥100 out-of-sample trades over ≥120 sessions after full costs, with a selection-aware positive lower bound on net expectancy | ❌ **failed for crypto and stocks** |
| **G3** Forward paper | ≥30 paper sessions, ≥20 round trips, USD 50 constraints, no unresolved incidents | ⬜ needs a strategy worth trading (certification passed 2026-11-09) |
| **G4** Live pilot readiness | Owner-configured account, protection, secrets, recovery, signed promotion manifest | ⬜ needs G0-G3 |
| **G5** Scaling | Live costs and reliability consistent with the model | ⬜ needs M6 |

## Detailed checklist: what was planned, what is left

### M0 - Feasibility ✅ (gate G0 not passed)
- [x] Broker capability matrix: [docs/broker_capabilities.md](broker_capabilities.md)
- [x] Cost model and minimum-viable-trade maths: [docs/cost_feasibility.md](cost_feasibility.md)
- [x] Data coverage report: [docs/data_coverage.md](data_coverage.md)
- [x] Alpaca regulatory fees documented from dated official sources (`configs/fees/alpaca_us_equities_v2.yaml`, ADR-0028)
- [ ] **Owner:** residency and contracting entity, account type and rule profile, funding route, protection policy

### M1 - Core safety ✅
- [x] Validated replay/paper/live configuration, host-role guard, single policy file
- [x] Decimal-only domain contracts, USD 50 ledger, settlement, fees, conservative equity
- [x] Account leadership locks, persisted pauses, loss windows, transactional outbox, order state machine
- [x] Timeout and unknown-outcome reconciliation, reducing-only exits, startup recovery
- [x] Replay broker with 11 fault types; first demonstrator with 14 acceptance checks
- [x] The contract cases run against the Alpaca paper adapter on a fake transport
- [x] The same cases on the real paper account (`trader paper certify`, 2026-11-09 on NVDA: 4/4 checks)

### M2 - Research baseline ✅
- [x] Historical data store with checksums, quality checks and `trader ingest-history`
- [x] Causal streaming features, proven identical to the runtime path
- [x] Two frozen baselines: Candidate A (trend) and Candidate B (mean reversion)
- [x] Crypto study: every trial loses ([report](../research/reports/baseline_report.md))
- [x] Stock history 2020-08..2026-10 from Alpaca IEX with the owner's keys; five splits applied point-in-time
- [x] Stock study T-0006..T-0010: every trial loses ([report](../research/reports/stocks_baseline_v1_report.md))
- [ ] Point-in-time stock universe and earnings calendar (disclosed as limitations in the report)
- [ ] Optional: venue-matched Kraken archive (owner download)

### M3 - Model evaluation 🟡
- [x] Owner budget decision: 15 trials (2026-11-08)
- [x] Labelled candidate dataset, candidate-v1 features, filter hook, random-rejection and equal-turnover controls
- [x] Logistic regression filter and LightGBM challenger; numpy-only model bundles with checksums
- [x] Walk-forward model study, calibration, selection log, `model_report.md`, and a gate that requires beating the rule and both controls
- [x] Crypto filter study T-0011, T-0012: untrainable, a documented rejection ([report](../research/reports/model_report.md))
- [ ] **Owner:** use of the 3 reserved trials
- [ ] A runtime edge estimator that scores a validated bundle (only after a filter passes G2)

### M4 - Paper runtime 🟡
- [x] Alpaca paper adapter on the official `alpaca-py` SDK, pinned (ADR-0025, ADR-0033)
- [x] Keys in the OS keyring with profiles and a keys inbox (ADR-0026, ADR-0032)
- [x] Paper runtime sharing the replay decision loop; USD 50 subledger; reconciliation; clock-skew and data-gap pauses (ADR-0031)
- [x] Certification command with a paper-only, capped grant (ADR-0035); real Alpaca instrument specs for paper
- [x] **Owner:** paper certification passed 2026-11-09; the certified profile `configs/capabilities/alpaca_paper_certified.yaml` is adopted in `configs/paper.yaml`
- [x] Owner practice mode (`trader paper run --practice`, ADR-0040): candidates whose planned target covers all costs trade on paper as `PRACTICE`, never evidence
- [ ] At least 30 paper sessions and 20 round trips, then `research/reports/forward_report.md` (gate G3)

### M5-M7 ⛔ / ⬜
- [ ] M5: owner decisions, a disabled live adapter with native protection proof, a signed promotion manifest
- [ ] M6: small live pilot (owner-enabled), cost and reliability comparison, go/no-go report
- [ ] M7: ASX gold-equities feasibility (separate track)

## What happens next (in order)

| # | Who | Step | Unblocks |
| --- | --- | --- | --- |
| 1 | Owner | Done 2026-11-09: paper certification passed on NVDA (4/4 checks, account flat, no fees) and the certified profile is adopted | M4 contract on the real paper account ✅ |
| 2 | Owner | Decide the 3 reserved trials: stock filters, or a new hypothesis where moves are large relative to costs (for example daily bars and multi-day holds; needs an approved strategy spec) | M3 / a strategy worth paper trading |
| 3 | Owner | Run paper sessions (`trader paper run`) and watch them on the local dashboard ([docs/observability.md](observability.md)); every candidate is rejected `NO_EDGE` until a strategy passes G2 | Operational evidence for G1/G3 |
| 4 | Owner | Residency, account type, funding, protection policy and alert channel ([docs/owner_setup.md](owner_setup.md) §4) | G0, M5 |

Realistic timing: a live pilot cannot be considered before a strategy passes G2 **and** about six to seven weeks of paper trading days (G3).
