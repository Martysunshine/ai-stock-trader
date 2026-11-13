# Commodities and CFDs — coding-agent addendum

Research checked: 6 November 2026. Status: implementation specification and research plan; instrument availability and profitable performance remain to be established.

## 1. Inspection result and preservation contract

The existing brief includes Australian gold-mining equities as a later research universe and mentions gold funds/ETCs and reference prices. It does **not** contain a complete commodities trading module. It explicitly excludes CFDs from stock v1. This addendum supplies the missing product, data, execution and validation specifications.

**Do not edit, append to, regenerate, replace, or republish existing production Markdown.** Protected files are `README.md`, `instructions.md`, `architecture.md`, `strategy_and_profitability.md`, `markets_and_brokers.md`, `risk_and_validation.md`, `implementation_roadmap.md`, and `repositories_and_sources.md`. Read them as dependencies. Add implementation work in new modules, profiles and tests; review any required code integration separately. No account connection or live order is authorized merely by loading this document.

Read [CRYPTO_TRADING_TRACK.md](CRYPTO_TRADING_TRACK.md) for the companion addition. The project now has three research tracks: existing stocks, crypto, and commodities. **CFD describes the contract used to trade an underlying, not a separate asset class or capital allocation.** A gold-mining share uses the stock execution route; a gold CFD uses a CFD route. Both can carry a gold exposure tag without duplicating their position or P&L.

The original stock-only instrument rules stay intact for `STOCKS`. New commodity profiles may describe ETFs/ETCs and CFDs, including short-CFD research, but inherit the shared numeric risk policy. Live directions and products require explicit capability certification and release evidence. A new profile must never silently change the stock profile's long-only, session or borrowing rules.

## 2. What to research and what can actually be traded

Interpret the user's “magnets” as metals, with a distinct rare-earth/magnet-material branch. Do not invent a universally tradable magnet spot market or assume rare-earth reference prices can be executed through a retail API.

| Exposure | Initial research role | Possible execution route | Distinction to preserve |
| --- | --- | --- | --- |
| Gold | First commodity candidate | Eligible gold ETC/fund, approved cash CFD; miners through stocks | Physical metal, a debt security, a company share and an OTC derivative have different risks |
| Silver | Second commodity candidate | Eligible silver ETC/fund or approved CFD | Own volatility, spread, units and lot minimums; do not copy gold sizing |
| Platinum and palladium | Later metals research | Product-specific ETC/fund or CFD if available | Availability and liquidity must be established per product |
| Copper, aluminium, nickel and zinc | Later industrial-metals research | Eligible fund, producer equity or approved derivative | Futures linkage, currency and contract units differ |
| Rare earths and magnet materials | Initially indirect exposure research | Eligible producer shares or specialist funds | Company/basket exposure is an imperfect proxy for NdPr, Dy or Tb prices |
| Oil and natural gas | Later energy research | Product-specific fund or CFD | Expiry/roll effects, financing, trading breaks and price-domain assumptions |
| Wheat, corn, coffee and other agriculture | Later discovery only | Verify an accessible product before collecting a trading dataset | Seasonality, contract specifications and data licensing need independent work |

Begin implementation with **one gold instrument and then one silver instrument**, both initially replay/demo. Do not open the whole table as a live allowlist. The gold/silver MVP requires positive prices; an energy extension must revisit this assumption rather than reusing percentage returns and log-price features blindly.

The IEA identifies neodymium, praseodymium, dysprosium and terbium as important magnet-related rare earths. USGS supplies mineral production and market-context data useful for slower research; these publications are not intraday executable price feeds. MP Materials is one public-company proxy to investigate, listed as NYSE: MP by its issuer. Evaluate a proxy's actual business, currency, costs and account eligibility before adding it. [M1–M3]

The original ASX gold-miner plan remains as written. This document does not waive its marketable-parcel, currency, brokerage or API constraints. A gold price model cannot be assumed to predict a miner's stock return: operations, financing, hedging and company news matter.

## 3. Product selection and broker route

Prefer an unlevered, fully paid exchange-traded route when a specific instrument is accessible and fits the budget. Verify the legal structure rather than treating every product called a gold “ETF” as a UCITS fund. For example, iShares Physical Metals describes its ETC securities as secured, limited-recourse debt backed by metal holdings. Record the issuer, security identifier, exchange listing, currency, expenses and current product documents. [M4]

Whole-unit minimums, commissions, FX and retail-product permissions can still make a fully paid product infeasible at USD 50. Use the existing equity/securities integration only after it is certified for that precise instrument type. Do not force an ETC through an `ordinary_equity` schema or bypass the unchanged stock allowlist.

For CFDs, **Capital.com is the first demo adapter candidate**, with IG Labs as an alternative to evaluate. Verify the actual resident country, retail status, contracting entity, API terms, product access, data entitlements and minimum funding before selecting a live provider. The original Germany-resident assumption is provisional, not established personal information. A public API page or a demo account does not prove live eligibility. [M5–M6]

Capital.com's documentation distinguishes demo and live environments and exposes instrument metadata, prices, positions and transaction confirmations. Implement environment separation explicitly. IG's documentation describes REST plus streaming access; its onboarding/API-key requirements need account-specific verification. Select one adapter at a time. [M7–M8]

Repository reference: [capital-com-sv/capital-api-postman](https://github.com/capital-com-sv/capital-api-postman), MIT license, inspected commit `f88a4b6d25c2c4f702b6a8b1fb1b79c07b806d50`. Its latest inspected push was in 2023, so treat its collections as examples and reconcile them against current documentation. Do not run a collection with live credentials or treat a sample response as a certified contract size.

For Germany retail CFDs, BaFin's intervention framework imposes leverage, margin-closeout, negative-balance and other conditions. ESMA's published framework distinguishes gold at up to 20:1 and other commodities at up to 10:1. Recheck the binding national/provider terms for the actual account; those regulatory ceilings are not recommended settings or guaranteed product availability. Negative-balance protection does not preserve the deposit, and margin closeout may occur before the strategy's intended exit. [M9–M10]

No leveraged/inverse funds, exchange-traded futures, options or physically deliverable contracts in this MVP. Each would need a separate instrument model and release gate. Tokenized gold belongs to a separately evaluated crypto wrapper, with issuer/redemption and venue risks; it is not automatically interchangeable with an ETC or CFD.

## 4. Shared capital, collateral and feasibility

**There is one USD 50 project balance across all three tracks.** Use the shared allocator and concurrency requirements in the crypto addendum and original architecture. Keep new modules in replay/shadow/demo unless every active writer participates in atomic reservations or demonstrably enforced disjoint budgets. Polling an independent stock bot cannot prevent it from spending the same risk budget.

The initial live pilot has at most one active track and one open live position. Other tracks can continue independent counterfactual research, clearly labeled. Fund movements, deposits and withdrawals are manual owner actions; account switching must reconcile pending orders and positions first.

Load numeric values from `risk_and_validation.md`, rather than maintaining an independently editable CFD policy. At USD 50, the inherited budgets are USD 0.125 planned loss per trade, USD 0.25 aggregate planned loss, USD 10 symbol notional, USD 20 gross notional and at least USD 30 cash reserve. Retain the daily/weekly loss triggers, drawdown controls, native protection and maximum entry frequency. Apply the global portfolio reporting windows without resetting existing account counters.

**Margin is collateral, not exposure.** A USD 20 margin deposit can control far more than USD 20 of commodity price risk. Calculate gross notional from the underlying contract units; never compare only margin to the 40% gross-exposure cap. A CFD's contractual margin mechanism does not authorize portfolio gearing: the project's notional limit remains below total equity.

Reserve cash deserves explicit treatment. Funds in a CFD account may support account-level losses, even if the application's ledger calls them “reserved.” A bookkeeping label does not ring-fence collateral. Before any live CFD pilot, establish how to keep the required reserve outside the broker's loss-bearing CFD collateral where necessary, and reconcile that allocation into total portfolio equity. Do not automate transfers to accomplish this. If the broker/account arrangement cannot implement the policy, live CFDs remain blocked.

Only realized net profits, subject to conservative marked-equity and currency/spendability constraints, may increase the sizing base. Funding credits, unrealized gains or released margin are not a reason to bypass aggregate limits. Aggregate BTC, equity, gold and FX risk across accounts; economic exposure tags supplement rather than replace gross-notional limits. Gold miners, gold ETCs and gold CFDs must not be assumed to diversify each other.

## 5. Contract model and dimensionally correct sizing

Add a `CommodityInstrumentSpec` with normalized fields. These are internal requirements, not claims about exact broker JSON keys:

| Field group | Required specification |
| --- | --- |
| Identity | Broker entity/account, venue or OTC provider, instrument ID, underlying, product type, settlement and reporting currencies |
| Units | Quantity unit, contract multiplier, price unit/scaling, tick and point values, quantity step, minimum/maximum size |
| Trading | Allowed directions, order types, hours, time zone/DST, maintenance breaks, close-only/market status |
| Margin | Initial/maintenance rules, tiers, netting/hedging mode, closeout calculation, account-level collateral behavior |
| Protection | Stop-distance rules, native stop capability, guaranteed-stop eligibility/premium, amendment restrictions |
| Carry | Cash versus expiring contract, long/short financing, charge times, holiday multipliers, expiry/roll adjustments |
| Evidence | Source/time, raw metadata snapshot, interpretation/version, capability certification and freshness policy |

Broker minimum size is instrument-specific. Fetch metadata and validate unit meanings; a generic help-page minimum is insufficient. Unknown multiplier, tick scaling, margin rule or fee schedule blocks trading. [M7, M11]

For a certified **linear** CFD, let `q` be positive quantity in broker units, `m` underlying units per broker unit, `P` the entry price per underlying unit, `S` the stop, `x` quote-currency-to-USD conversion, and `d` be +1 for long or -1 for short:

```text
notional_usd = abs(q * m * P) * x
gross_price_pnl_quote = d * q * m * (exit_price - entry_price)
planned_loss_usd = q * m * abs(P - S) * x
                 + stressed_adverse_fill_usd
                 + estimated_round_trip_costs_usd
```

Costs include applicable commission, spread/slippage, funding over the allowed holding horizon and guaranteed-stop premiums. Use bid/ask fills or an explicit spread deduction, never both. Translate each cash flow at its applicable FX rate in the ledger; the simple formula is not a substitute for multi-currency settlement accounting. Validate price-point scaling with worked examples and a broker statement before certifying the adapter.

Solve for size subject to all constraints: per-trade and aggregate planned loss, symbol and portfolio gross notional, actual margin availability, cash reserve, exit minimums and certified protection. Round down to the quantity step and recompute. Do not shrink the strategy's stop purely to pass the size check.

**Hypothetical sizing fixture, not a current market quote:** gold price USD 4,000/oz; smallest position 0.1 oz; stop distance 1%. Notional is USD 400 and price risk is USD 4 before costs. At 20:1 contractual leverage, initial margin would be USD 20, but this trade still violates the USD 10 symbol and USD 0.125 risk budgets. Being able to post margin does not make the trade eligible.

For a single isolated position, a useful lower-bound diagnostic is:

```text
minimum_equity_lower_bound = max(
    minimum_order_notional / 0.20,
    minimum_order_planned_loss / 0.0025,
    minimum_order_notional / 0.40
)
```

The fixture requires at least USD 2,000 by the symbol limit and USD 1,600 by price risk, before cost, reserve, collateral or other-position constraints. The diagnostic is not a funding recommendation or a complete feasibility solution. Produce the exact feasibility result for the actual contract; if USD 50 cannot satisfy it, the correct output is `NO_TRADE` with binding reasons.

## 6. Data, pricing and carrying costs

Use the intended broker's executable bid/ask stream for CFD execution analysis. A futures chart or generic spot series may be useful context, but differences in basis, spread and timestamps make it an inadequate fill model. An OTC broker's tick count is not total global gold volume.

Store raw and normalized observations with event/receive/availability times, source, instrument-spec version, price precision, validity flags and session calendars. Model daily maintenance breaks, holidays, weekends, daylight-saving changes and trading halts. Stop a strategy from entering when reliable protection or the intended exit window is unavailable.

Cash CFDs can incur overnight financing, with the direction and amount dependent on product/provider rules. Capital.com's EU explanation describes commodity funding related to futures basis plus charges. Fetch current long/short rates, applicable timestamp and holding-period accrual; include multi-day charges where the schedule requires them. Do not assume one universal Wednesday triple-charge rule or that a short position always earns funding. [M12]

For expiring or futures-linked products, store actual tradable contract prices separately from adjusted research series. Define roll timing, price adjustments, costs and holding continuity. Back-adjusted prices are not executable fills. Avoid counting both an economic roll difference and its corresponding account adjustment as independent profit/loss. No automatic roll is permitted until its ledger and order behavior are tested.

Macroeconomic releases, inventories and positioning reports are optional explanatory features. Timestamp the first actual public availability, not just the observation period. Keep historical vintages for revised series. A later revised dataset must not be used as information supposedly known at the entry time. Data licensing and delivery cost belong in the feasibility report.

## 7. Strategy hypotheses and AI role

First research **gold and silver trend continuation**, independently per instrument. Use completed 15-minute bars with one-hour context, a predeclared volatility regime filter, bounded entry spread, volatility-based stop and maximum holding period. Test long and short directions separately in the CFD demo profile; do not infer short performance from mirrored long results. The fully paid securities route remains long only unless separately authorized and implemented.

If the intended strategy avoids financing, define its last entry and flattening buffer against the broker's actual funding cut-off. Include the possibility of a failed or partial exit; do not erase financing from the simulator simply because a scheduled exit was intended. Also test whether a slower holding horizon remains economic after carry, rather than assuming faster trading is better.

Gold/silver relative-value or spread trading is a later hypothesis. It requires a justified hedge ratio, two feasible legs, synchronized fills, legging-loss allowances and combined costs. It is not a default USD 50 strategy, and two correlated legs do not cancel gross exposure or execution risk.

Begin with transparent deterministic rules. A small calibrated tabular model may filter signals using lagged returns, volatility, spread, market-session context and causally available macro features. Fit all transformations and thresholds only on training data. The model cannot change contract size, margin settings, instrument permissions, loss limits or the order state machine.

Record the hypothesis, economic rationale, parameter search budget and failure condition before evaluation. Compare cash/no trade, a suitable fully paid exposure baseline, deterministic trading and AI-filtered trading. Use purged chronological evaluation, an untouched final period, a trial registry and selection-aware uncertainty. Judge incremental net returns under matched risk and capital constraints, not signal accuracy or a high win rate alone.

LLMs may assist research or structured extraction of dated public information. Keep their output reproducible and outside order authority; preserve the existing zero paid-LLM trading-loop budget. Automatic retraining creates a challenger, never an automatically approved replacement.

## 8. CFD order lifecycle and failure handling

Reuse the existing durable order manager, outbox, reconciliation and account leadership. An intent requires a current instrument snapshot, fresh quote/account state, shared reservation and native-protection capability before submission. Demo and live use different credentials, environment IDs and explicit configuration; a missing demo service must never fall back to live.

For Capital.com, resolve an acknowledged `dealReference` through the confirmation endpoint and reconcile the resulting position/deal IDs. A successful HTTP response alone is not sufficient execution evidence. Close an identified existing position through the documented close operation; do not assume an opposite order will close rather than open a hedge. [M7]

Unless the provider's exact idempotency semantics have been certified, treat submission timeouts as unknown outcomes. Reconcile orders, activity and positions before deciding whether a submission occurred. Internal UUIDs cannot manufacture broker-side idempotency. Do not retry an unresolved entry blindly or release its reservation while it might still fill.

Model these events explicitly: accepted/rejected order, partial or full fill where supported, stop acceptance/rejection, amendment, cancellation, forced closeout, expiry/roll, financing charge, session expiry and stream reconnect. If the venue uses all-or-none immediate positions instead of exchange-style partial fills, encode that certified capability rather than simulating fictitious events.

Require a native protective order at the exact intended size before live admission. A normal stop can slip or gap; a guaranteed stop may have eligibility rules, minimum distance and a premium. If a quote moves while opening and the stop becomes invalid, reject safely or follow a preapproved recovery rule rather than silently opening unprotected exposure. Test a time exit racing the stop and a disconnect during each transition.

On a breach or uncertain state, block new exposure, preserve valid exits, reconcile fills in flight and reduce risk using the certified close route. Do not implement a generic “cancel everything” action that removes protection. Loss-triggered pauses require the existing recorded review before resume; a healthy connection alone does not clear them.

Maintain trade-only credentials where supported, redacted logs, separate demo/live accounts, secret storage, a health watchdog and durable alerts. The executor must handle broker maintenance, stale FX and token/session expiry without interpreting missing data as a trading signal.

## 9. Implementation sequence and release evidence

Deliver these as new implementation artifacts without modifying protected Markdown:

1. **Instrument feasibility matrix:** actual gold/silver candidates, legal product type, current metadata, units, minimum size, account permissions, full costs, native protection and USD 50 eligibility. Include explicit unknowns and blockers.
2. **Contract and accounting implementation:** tested units, long/short P&L, FX, margin, accrued funding, reserved cash and account reconciliation. Keep CFD valuation separate from ordinary-stock valuation.
3. **Data and replay:** broker-matched spread/price observations, causal features, deterministic strategies and conservative execution. Preserve raw versus adjusted data.
4. **Demo adapter:** exact environment selection, accepted/rejected confirmations, safe closes, protection and recovery. Enforce a local USD 50 portfolio even if the provider supplies a much larger demo balance.
5. **AI challenger report:** fully documented experiments and net comparisons. No performance claims from repository examples or training results.
6. **Shared integration certification:** cross-track reservations, correlated exposures, single ownership and unchanged stock behavior. If integration is unavailable, remain in research/demo.
7. **Optional live pilot:** only after capability, cost, evidence and owner-enablement gates. Failure of the USD 50 feasibility test is a valid reason to keep CFDs in demo while another track is evaluated.

Apply the original G2 minimum of 100 out-of-sample completed trades across 120 sessions plus sufficient statistical power and the selection-aware positive lower bound on net expectancy. Apply G3's minimum 30 completed forward sessions and 20 paper round trips. Count genuine broker trading sessions; a handful of bars is not a session. Do not pool unrelated gold, crypto and stock samples to claim a new strategy passed its own gate. Independently assess the combined portfolio.

Record trading P&L, realized/accrued financing, commissions, spread/slippage, FX, operating expenses, exposure, drawdown, rejected opportunities, forced closes and statement differences. Do not spend unrealized gains or treat a demo cash top-up as profit. Retain raw transaction and tax-reporting records; taxes require the actual jurisdiction and account treatment.

## 10. Required adversarial tests and definition of done

| Scenario | Required invariant |
| --- | --- |
| Gold contract multiplier or price scale differs by 100x | Sizing fails certification; no oversized order reaches the adapter |
| Minimum lot fits margin but violates notional/risk | `NO_TRADE`; no automatic leverage or budget increase |
| Silver tick/point units differ from gold | Independently verified P&L and stop distances |
| Long versus short with overnight charge/credit | Correct signs, currency, accrual and statement reconciliation |
| Weekend gap or funding cut-off with an unfilled exit | Conservative fill/carry accounting; no fictitious scheduled close |
| Roll or account adjustment | No invented profit from an adjusted chart; no duplicate charge |
| Entry timeout followed by delayed confirmation | At most one intended position; reservation retained until reconciliation |
| Stop rejected or time exit races protective stop | No untracked/unprotected exposure or accidental reverse position |
| Broker margin closeout | Forced execution recognized, ledger corrected, new entries paused |
| Same gold exposure appears as miner, ETC and CFD | One owner per position; portfolio exposure aggregated |
| Concurrent crypto/stock/commodity signals | Shared cash/risk cannot be allocated multiple times |
| Reboot during a loss pause | Counters and pause survive; no automatic loss-breach reset |

Completion means an independently reproducible demo/replay pipeline, correct contract accounting, realistic cost reporting, proven failure handling and a clear eligibility decision. It does not mean the bot must trade every day or that profitable results are guaranteed. A strategy earns a live pilot only by passing the existing evidence and risk requirements at the actual feasible order size.

## Sources

Primary sources checked for this addendum; legal/entity terms, product specifications, fees and APIs must be revalidated at implementation and live release.

- **M1:** [IEA rare-earth elements — executive summary](https://www.iea.org/reports/rare-earth-elements/executive-summary).
- **M2:** [USGS Mineral Commodity Summaries 2026](https://www.usgs.gov/publications/mineral-commodity-summaries-2026) and [rare-earth statistics](https://www.usgs.gov/centers/national-minerals-information-center/rare-earths-statistics-and-information).
- **M3:** [MP Materials issuer stock information](https://investors.mpmaterials.com/stock-information/default.aspx) and [business overview](https://investors.mpmaterials.com/overview/default.aspx).
- **M4:** [iShares Physical Metals issuer interim report, 31 October 2025](https://www.ishares.com/de/privatanleger/de/literature/interim-report/ishares-physical-metals-plc-interim-report-en-31-oct-25.pdf).
- **M5:** [Capital.com German site — regulations and entities](https://capital.com/de-de/terms-and-policies/regulations).
- **M6:** [IG Labs getting started](https://labs.ig.com/getting-started.html).
- **M7:** [Capital.com public API documentation](https://open-api.capital.com/).
- **M8:** [IG Labs API overview](https://labs.ig.com/getting-started.html).
- **M9:** [BaFin CFD General Administrative Act](https://bafin.de/SharedDocs/Veroeffentlichungen/EN/Aufsichtsrecht/Verfuegung/vf_190801_allgvfg_Differenzgeschaefte_en.html) and [interpretive guidance](https://bafin.de/SharedDocs/Veroeffentlichungen/EN/Auslegungsentscheidung/WA/ae_200901_Leitlinien_CFD_Allgeminvfg_en.html).
- **M10:** [ESMA CFD intervention framework](https://www.esma.europa.eu/press-news/esma-news/esma-agrees-prohibit-binary-options-and-restrict-cfds-protect-retail-investors). Historical framework reference; not a substitute for current national and account terms.
- **M11:** [Capital.com minimum trade size guidance](https://help.capital.com/hc/en-us/articles/4403989787538-What-is-the-minimum-trade-size).
- **M12:** [Capital.com EU explanation of costs and commodity financing](https://capital.com/en-eu/about-us/how-capital-makes-money).
- **M13:** [Capital.com Postman collection at inspected commit](https://github.com/capital-com-sv/capital-api-postman/tree/f88a4b6d25c2c4f702b6a8b1fb1b79c07b806d50).
