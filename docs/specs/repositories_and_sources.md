# Inspected repositories and primary sources

Inspected on **13 October 2026**, using the GitHub plugin for repository metadata, upstream README material where available, and reference HEAD commits. These are research snapshots, not certified dependencies or independently verified trading returns. Confirm licenses, supported versions, transitive dependencies, and behavior before importing code.

## 1. Repository shortlist

| Project | License reported by GitHub | Intended use | Adoption decision |
| --- | --- | --- | --- |
| [alpacahq/alpaca-py](https://github.com/alpacahq/alpaca-py) | Apache-2.0 | Official paper/retail Trading API and data integration | Primary first broker SDK; certify every used combination |
| [microsoft/qlib](https://github.com/microsoft/qlib) | MIT | Research datasets, feature/model experiments, evaluation structure | Optional offline research; not a substitute for account-aware execution |
| [AI4Finance-Foundation/FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading) | Apache-2.0 | FinRL-X modular strategy/allocation/risk/execution concepts | Reference architecture; evaluate selected modules in isolation |
| [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL) | MIT | RL research environments and educational baselines | Later experiment only; upstream directs production-oriented work toward FinRL-X |
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | Apache-2.0 | Established event-driven research/execution engine | Candidate independent reference engine; metadata inspected, README path was unavailable in the requested fetch |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | LGPL-3.0 | Deterministic event-driven simulation/execution architecture | Candidate reference engine; review adapter/fractional capabilities and license obligations |
| [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) | MIT | Bounded factor/model research automation | Optional later offline automation; never authorized to change live policy |

Do not conflate the original FinRL's MIT license with FinRL-Trading's Apache-2.0 license. A permissive repository license does not grant rights to redistribute its market data or third-party model weights.

### Reference HEAD commits

These are full commit IDs retrieved from the default branch, not merely README blob hashes. They document the inspection point; they are not instructions to deploy an unreviewed development branch.

| Repository | Reference commit |
| --- | --- |
| alpaca-py | `f3e22febfbf2a0f70d6c227e86282593b6127ca4` |
| qlib | `54355232463878d2eebb91fe0ee5fa7fa1f5976c` |
| FinRL-Trading | `4409abe925c904e570be78ebfb5e77ac3491dff8` |
| FinRL | `8511e7c15fbe1fbe5eb25e9f89932ac1ecd546a0` |
| Lean | `80e7843f645673bcbeaab963049f76f20f6785e1` |
| nautilus_trader | `ca478e42f434d2ce185493f2bb42d112a88d1ffe` |
| RD-Agent | `484776c211e4fbbeef03e0ec00d6bbee7362a4f4` |

The Qlib README at inspection described unreleased source changes to artifact/configuration loading. Pin a reviewed release and follow the corresponding migration guidance. Do not broadly enable trusted artifact loading to make downloaded models work.

FinRL-X's inspected README describes data, strategy, backtest, and Alpaca execution modules with a common portfolio-weight interface. Use that separation as a design reference. Its example backtests, like other upstream example charts, are not proof that the proposed USD 50 intraday strategy will profit.

## 2. Reuse procedure

1. Create a dependency evaluation record with exact version/commit, purpose, license/NOTICE obligations, supported Python/platform versions, and required dependencies.
2. Run a small offline/paper contract experiment before adopting an execution component.
3. Inspect fee/fill/quantity/account assumptions. Tutorials using whole shares or large portfolios cannot silently determine the USD 50 design.
4. Preserve required attribution and notices; avoid mixing entire frameworks when a small interface or dependency suffices.
5. Verify artifact loading and dependencies; do not execute an unknown model or deployment script with account credentials.
6. Record whether published results are a backtest, paper experiment, audited live record, or an unsupported claim. This pack verified no independently audited profitable live record.

## 3. Broker and data references

| Source | What it supports | Required follow-up |
| --- | --- | --- |
| [Alpaca fractional trading](https://docs.alpaca.markets/us/docs/fractional-trading) | Fractional API concepts, asset eligibility, order rules | Resolve conflicting wording and confirm intended-account capabilities |
| [Alpaca account plans](https://docs.alpaca.markets/us/docs/account-plans) | Account structure and API overview | Confirm actual legal account type and current rule implementation |
| [Alpaca cash-account support](https://alpaca.markets/support/alpaca-cash-accounts) | Margin/limited-margin description | Older support material; recheck account-specific availability |
| [Alpaca non-US account guide](https://alpaca.markets/learn/live-trading-account-non-us) | Conditional international eligibility and onboarding | Germany eligibility is unresolved in this pack |
| [Alpaca market-data plans](https://docs.alpaca.markets/us/docs/about-market-data-api) | Free IEX vs USD 99 consolidated coverage and limits | Confirm plan/entitlement at setup |
| [Alpaca paper trading](https://docs.alpaca.markets/us/docs/paper-trading) | Simulation behavior and omitted live frictions | Conservative overlay and actual execution validation |
| [Alpaca brokerage fee schedule](https://files.alpaca.markets/disclosures/BrokFeeSched.pdf) | Direct brokerage service/transfer fees | Identify the owner's actual transfer route |
| [IBKR individual accounts](https://www.interactivebrokers.com/en/accounts/individual.php) | Cash/margin structures, fractions, market access | Determine entity, permissions, data, and costs |
| [IBKR stock commissions](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php) | US fractional footnote and other fee details | Do not apply whole-share headline pricing to a fraction |
| [IBKR API order types](https://www.interactivebrokers.com/campus/?p=195739&post_type=ibkr-api-page) | API limitations and simulation caveats | Resolve fractional API ambiguity with current confirmation |
| [IBKR TWS error codes](https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/error-codes) | Fractional sizing/permission/order restrictions | An error code's existence is not proof of supported trading |
| [IBKR restricted US ETF explanation](https://www.interactivebrokers.co.uk/en/trading/products-cfds.php) | Product-document restrictions for European retail access | Check each product; this plan does not use the suggested CFD alternative |

## 4. Exchange and account-rule references

| Source | Supported finding |
| --- | --- |
| [FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10) | New intraday margin standards effective 4 June 2026, transition ending 20 October 2027 |
| [SEC/Investor.gov cash-account bulletin](https://www.sec.gov/resources-for-investors/investor-alerts-bulletins/ib_cashaccounts) | Payment/settlement constraints, sale-proceeds timing, freeriding examples |
| [ASX shares guidance](https://www.asx.com.au/investors/learn-about-our-investment-solutions/shares) | Ordinary share investing from A$500 plus brokerage; market/company risks |
| [ASX market-rule procedures](https://www.asx.com.au/documents/rules/asx_market_rule_procedures.pdf) | Marketable-parcel definition; older reference requiring current broker confirmation |
| [ASX settlement transition information](https://www.asx.com.au/markets/clearing-and-settlement-services/t1-settlement-cycle) | Current T+2 and potential T+1 context |
| [Northern Star profile](https://www.nsrltd.com/about-us/) | Gold producer, ASX NST |
| [Evolution share-price page](https://evolutionmining.com/investor-centre/share-price/) | ASX EVN identification |
| [Genesis profile](https://genesisminerals.com.au/about-us/) | Gold producer, ASX GMD |

Implement account rules from the broker's actual profile; do not encode this dated source snapshot as permanently correct policy.

## 5. Research reading

- [Qlib: An AI-oriented Quantitative Investment Platform](https://arxiv.org/abs/2009.11189): infrastructure for AI quantitative research.
- [FinRL-X: An AI-Native Modular Infrastructure for Quantitative Trading](https://arxiv.org/abs/2603.21330): a modular, deployment-consistent research/execution interface.
- [R&D-Agent-Quant](https://arxiv.org/abs/2505.15155): automated factor/model research; an optional extension, not a trading-authority mechanism.
- [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf): selection bias in historical strategy experiments and an assessment framework.

These primary research sources inform the design. They do not establish a protected-capital route from USD 50 to USD 100,000 in three months.
