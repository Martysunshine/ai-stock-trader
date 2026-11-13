# Documentation

Everything written about this project, grouped by what you want to do. The front page is the
[README](../README.md).

## Start here

| Document | Use it for |
| --- | --- |
| [project_status.md](project_status.md) | Milestones M0 to M7, release gates G0 to G5, the detailed checklist, next steps |
| [PROJECT_GUIDE.md](PROJECT_GUIDE.md) | What each specification file is for and where it lives in code |
| [strategy_playbook.md](strategy_playbook.md) | What is traded on paper, the evidence for each rule, what each market costs |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Setup, API keys, the git workflow, the research log |

## Operating it

| Document | Use it for |
| --- | --- |
| [runbook.md](runbook.md) | Commands: demo, research, paper, certification, pause and resume, reconcile |
| [owner_setup.md](owner_setup.md) | Owner actions: keys, accounts, decisions, the assistant's data connections |
| [observability.md](observability.md) | The read-only data map behind the console |
| [../ui/README.md](../ui/README.md) | The Trader Console and the Desk |
| [alpaca_paper_adapter.md](alpaca_paper_adapter.md) | How the Alpaca paper adapter maps orders, fills and errors |
| [replay_broker.md](replay_broker.md) | The replay broker and its fault types |

## Research

| Document | Use it for |
| --- | --- |
| [ml_research_guide.md](ml_research_guide.md) | What a model may do, the model pipeline, what counts as evidence |
| [../research/reports/](../research/reports/) | The 13 study reports: crypto and stock baselines, model filters, ETF and CFD studies |
| [cost_feasibility.md](cost_feasibility.md) | The cost model and the minimum viable trade |
| [data_coverage.md](data_coverage.md) | Which market data exists, from where, and its limits |
| [broker_capabilities.md](broker_capabilities.md) | What each broker can and cannot do |

## Reference

| Document | Use it for |
| --- | --- |
| [decisions.md](decisions.md) | The 64 architecture decision records (ADR-0001 to ADR-0064) |
| [traceability.md](traceability.md) | Every requirement mapped to code and tests |
| [dependency_evaluation.md](dependency_evaluation.md) | Each dependency: version, licence, purpose |
| [integrations/](integrations/) | The plan and the report for the assistant's data connections |

## Specifications (protected)

These nine files and the brief at the bottom of the README are never edited. Their SHA-256
hashes are pinned in [protected_spec_hashes.sha256](protected_spec_hashes.sha256) and a test
checks them.

| Document | Covers |
| --- | --- |
| [specs/instructions.md](specs/instructions.md) | The contract and ordered tasks |
| [specs/architecture.md](specs/architecture.md) | Runtime, data contracts, broker interfaces, order lifecycle, recovery |
| [specs/risk_and_validation.md](specs/risk_and_validation.md) | The risk configuration, research gates, adversarial tests, live promotion |
| [specs/strategy_and_profitability.md](specs/strategy_and_profitability.md) | What to test, how models contribute, costs, compounding |
| [specs/markets_and_brokers.md](specs/markets_and_brokers.md) | Markets, broker feasibility, account constraints |
| [specs/implementation_roadmap.md](specs/implementation_roadmap.md) | Deliverables, milestones, acceptance criteria |
| [specs/repositories_and_sources.md](specs/repositories_and_sources.md) | Inspected upstream projects and primary sources |
| [specs/CRYPTO_TRADING_TRACK.md](specs/CRYPTO_TRADING_TRACK.md) | The crypto track |
| [specs/COMMODITIES_AND_CFD_TRACK.md](specs/COMMODITIES_AND_CFD_TRACK.md) | The commodities and CFD track |
