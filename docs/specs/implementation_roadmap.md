# Implementation roadmap and acceptance criteria

The first ninety days are an **engineering and evidence window**, not a promised return period. Estimates below assume one capable coding agent with owner support for broker/account setup. Data access, failed hypotheses, or account restrictions can extend the schedule.

## 1. Milestones

| Stage | Indicative window | Concrete outputs | Exit condition |
| --- | --- | --- | --- |
| M0 — feasibility | Days 1–3 | Broker capability matrix, cost model, account-rule questions, data coverage report | Paper route usable; live blockers explicit |
| M1 — core safety | Days 4–10 | Contracts, USD 50 ledger, policy loader, locks, event journal, outbox, replay clock | Deterministic accounting and required invariants pass |
| M2 — research baseline | Days 11–20 | Historical manifest, causal features, two separate baseline specifications, realistic replay reports | Reproducible results; no hidden lookahead or unrealistic fills |
| M3 — model evaluation | Days 21–35 | Tabular candidate filters, walk-forward comparison, selection log, statistical report | Either defensible challenger or documented rejection |
| M4 — paper runtime | Start once M1/M2 integrity passes; then at least 30 sessions | Broker stream, reconciliation, real-time paper, incident tests, cost overlay | G1/G3 operational evidence; no forced trading to accelerate observation |
| M5 — live readiness | Only after G0–G4 pass | Disabled live adapter, owner setup/runbook, protection proof, promotion manifest | Account/economics verified and owner deliberately enables pilot |
| M6 — live observation | Remaining time if prerequisites pass | Small execution sample, cost/reliability comparison, go/no-go scaling report | Continue only while policy and evidence support operation |
| M7 — ASX expansion | Separate later milestone | Gold/FX/company data research and ASX feasibility | Capital/API/data/risk requirements pass independently |

Paper operations can overlap later offline research, but safety gates cannot be bypassed. An unprofitable baseline is a useful measured result; it is not a reason to connect a live account anyway.

## 2. Required generated artifacts

| Artifact | Contents |
| --- | --- |
| `docs/broker_capabilities.md` | Documented vs paper vs intended-live evidence, API versions, account rules, exit combinations, unresolved items |
| `docs/cost_feasibility.md` | Exact tiny-order costs, FX/transfer routes, recurring expense budget, minimum viable capital/horizon calculations |
| `research/data_manifest.json` | Source/feed, entitlement, date range, availability/corrections, adjustment basis, checksums, missing data |
| `research/experiment_registry.jsonl` | All registered and rejected trials with frozen specs and selection history |
| `research/reports/baseline_report.md` | Same-capital comparisons, execution assumptions, session metrics, bias limits |
| `research/reports/model_report.md` | Chronological splits, tuning history, calibration, economic ablation, uncertainty/selection correction |
| `research/reports/forward_report.md` | Paper vs observable quotes, USD 50 budget, fills, real-time issues, conservative cost overlay |
| `docs/runbook.md` | Start, monitor, pause, reconcile, close, recover, update, and review loss breaches |
| `docs/owner_setup.md` | Exact broker/data navigation, credentials stored locally, account/protection decisions |
| `research/promotion_manifest.json` | Immutable versions, capabilities, gate evidence, mode/account enablement |

Do not prepopulate “passed” fields before running checks. Reports identify backtest, paper, shadow, and live evidence separately.

## 3. Proposed CLI contract

These are commands **to implement**, not commands available in the delivered planning pack:

```bash
uv sync --frozen
uv run trader config-check --config configs/replay.yaml
uv run trader ingest-history --manifest research/data_manifest.json
uv run trader replay --config configs/replay.yaml --run-id baseline-001
uv run trader evaluate --run-id baseline-001
uv run trader paper --config configs/paper.yaml --account-alias pilot
uv run trader reconcile --config configs/paper.yaml
uv run trader pause --account-alias pilot --reason owner_requested
uv run trader report --run-id baseline-001
```

Implement a separately guarded live entry point requiring an explicit live configuration, allowlisted account, promotion manifest, and `--enable-live` startup flag. No replay/paper command may silently fall back to live. A model or external article cannot supply that flag or edit the startup policy.

`pause` stops new exposure while preserving certified reducing exits. A separate `close-positions` command must verify holdings, outstanding sell orders, account identity, and broker capabilities, and report pending liquidation accurately. It must not claim closure until confirmed fills/reconciliation support it.

## 4. First integration demonstration

Provide a sanitized recorded session where:

1. The model/rule creates one eligible candidate and one rejected candidate.
2. An order is sized below USD 50 cash/exposure limits with exact fees.
3. A simulated broker acknowledges, partially fills, and finally closes the position.
4. The ledger matches fill-derived PnL and all reservations clear correctly.
5. A rerun reproduces decisions and ledger hashes.
6. A timeout variant reconciles the accepted order without sending a duplicate.
7. A costly-broker variant produces no trade.
8. A loss-trigger variant persists a pause across restart.

Then run equivalent paper contract cases as supported by the dedicated account. Differences must become explicit adapter capabilities or blockers, not unexplained exceptions.

## 5. Owner decisions, at the point they are needed

The agent should resolve reversible technical choices itself. It should present a completed, reviewable broker/cost comparison before requesting the live broker/account choice. Setup-dependent items are:

- Actual account entity/residency and eligible broker; dedicated personal vs business account if the owner chooses differently from this pack's assumption.
- Available data entitlement and a separate operating-cost ceiling. Default new paid subscriptions: zero.
- Broker-held protection, or a consciously selected and documented alternative if required capabilities are unavailable.
- Dedicated paper credentials and, much later, live credentials/enablement supplied through local secret setup.
- An owner alert channel if unattended live operation is enabled.

Do not ask the owner to choose model architectures or infrastructure before providing the evidence that makes the choice meaningful.

## 6. Ninety-day review

Deliver a decision-ready report showing what was implemented, tested, and actually achieved. Include trading balance and external cash flows, net trading and total project PnL, open exposure, drawdown, full costs, strategy trials, effective evidence sample, incidents, and remaining account/data limitations.

Possible outcomes: continue the verified pilot; continue paper observation; reject a strategy; change a horizon under a new experiment; or conclude the USD 50 live route is currently uneconomic. Capital increases and ASX activation require new feasibility checks. The review never treats failure to reach USD 100,000 as a reason to loosen safety controls.
