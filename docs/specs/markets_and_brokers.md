# Markets, brokers, data, and feasibility

Research date: **13 October 2026**. Assume an owner-operated personal account for a Germany-resident retail user; confirm actual residence, citizenship requirements, broker entity, and account type during setup. Country eligibility is not established by this document.

## 1. Market selection

| Market/instrument | Initial role | USD 50 suitability | Requirements/blockers |
| --- | --- | --- | --- |
| Liquid US ordinary stocks, fractional | Primary paper and research track | Technically plausible with an eligible low-cost API broker | Actual country/account eligibility, fractional exits, data coverage, fees, rule regime |
| US-listed gold producers | Later separate research universe | Similar fractional constraints | Company risk, gold/FX context, current asset eligibility |
| ASX ordinary gold-mining equities | Offline/replay extension | Generally unsuitable for ordinary initial holdings at this balance | A$500 initial parcel, brokerage, FX, API, data, risk sizing |
| Physically backed gold funds/ETCs | Future instrument research only | Must assess specific product | Jurisdiction, product documentation, fractionability, API and costs |
| Gold spot/futures prices | Possible explanatory data | Data can be used without trading derivatives | Licensed, timestamped, comparable price series |
| Futures, options, CFDs, margin loans, leveraged funds, crypto | Outside v1 trading scope | Do not use to force the return target | Distinct risks and market structures |

A low share price does not mean a stock is easier to predict, safer, or more suitable for small-account trading.

## 2. Alpaca — paper-first candidate

The official SDK is useful for stock data and retail Trading API integration. Official documentation describes fractional trading from approximately USD 1 and asset-specific fractionability, making it a sensible first **paper** integration. See [fractional trading](https://docs.alpaca.markets/us/docs/fractional-trading) and the [official SDK](https://github.com/alpacahq/alpaca-py).

Resolve these live blockers before selection:

- International support depends on residence; the official non-US guide directs users to check country eligibility. Germany eligibility for the intended account is not verified here. See [non-US onboarding guidance](https://alpaca.markets/learn/live-trading-account-non-us).
- Account documentation/support describes accounts as margin or limited-margin accounts. Disabling borrowing does not change their legal classification. See [account plans](https://docs.alpaca.markets/us/docs/account-plans) and [cash-account support](https://alpaca.markets/support/alpaca-cash-accounts).
- Establish the broker's current US intraday-rule implementation for this exact account. Do not infer it from a paper flag or an old help article.
- The fractional page describes multiple supported types but also contains narrower legacy wording. Verify the exact current quantity/notional, limit, stop, protective-order, TIF, and live-account combinations. Treat conflicting documentation as unresolved, not as permission to assume support.
- Verify funding method, FX, transfer and withdrawal costs. The researched direct brokerage fee schedule lists a USD 50 international wire fee; an unsuitable transfer route can consume the entire starting balance. Other routes have different charges. See [fee schedule](https://files.alpaca.markets/disclosures/BrokFeeSched.pdf).

**Decision:** use the paper adapter for development. Select it for live trading only after the capability and cost report passes.

## 3. Interactive Brokers — assess, do not presume

IBKR offers broad market access, cash account structures, retail fractional trading, and APIs. These are valuable for later US/ASX evaluation, but access through its UI does not establish access through the desired API. See [individual accounts](https://www.interactivebrokers.com/en/accounts/individual.php).

The inspected official material has inconsistent fractional-API guidance: an API order-types page says fractional/cash-quantity trading is unsupported outside specified asset classes, while the current TWS error/reference documentation contains fractional sizing and permission fields. These do not by themselves certify a working ordinary-stock API route. Resolve the exact API, account, TWS/Gateway version, order type, permissions, and instrument with current broker support and controlled tests. See [order types](https://www.interactivebrokers.com/campus/?p=195739&post_type=ibkr-api-page) and [TWS error codes](https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/error-codes).

At the research date, the US commission page's fractional footnote states the greater of **1% of trade value or USD 0.01** per fractional trade. Its example charges USD 0.05 for a USD 5 fractional purchase. Two similar-sized sides are therefore about 2% round-trip commission before spread and other charges. The USD 0.35/1.00 whole-share headline minimums are not an adequate fractional cost model. Verify the schedule applicable to the actual entity/account. See [US commissions and disclosures](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php).

**Decision:** potentially useful at larger capital or for ASX access; not the default USD 50 intraday route. API certification and costs may make it unsuitable for this pilot.

## 4. US account rules — current transition matters

FINRA Regulatory Notice 26-10 adopted replacement intraday margin standards, effective **4 June 2026**, with broker transition permitted through **20 October 2027**. The replacement removes the old pattern-day-trader count framework and USD 25,000 PDT minimum; firms still in transition may apply the previous regime. This is not a blanket statement that every account can day-trade without restrictions. See [FINRA Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10).

Implement an explicit account-rule profile:

| Profile | Required behavior |
| --- | --- |
| `LEGACY_PDT` | Model the broker's count and restrictions; require remaining capacity for the intended round trip before entry |
| `NEW_INTRADAY_MARGIN` | Use current broker margin/risk permissions and buying-power controls; v1 still prohibits borrowing |
| `TRUE_CASH` | Use certified settled-funds rules and a settlement ledger; do not borrow against sale proceeds |
| `UNVERIFIED` | Permit research/paper; block live entries |

A true cash account has a different constraint: purchasing and reselling with unsettled proceeds can violate payment rules. Most relevant US stock transactions settle T+1, but calendars and exceptions matter. A same-day sale of a stock fully paid with settled funds is distinct from repeated reuse of unsettled sale proceeds. The pilot's conservative cash policy only allocates already-settled funds for entries. See [SEC/Investor.gov cash-account bulletin](https://www.sec.gov/resources-for-investors/investor-alerts-bulletins/ib_cashaccounts).

Do not intentionally hold an invalidated intraday trade overnight to avoid a day-trade count. If the account cannot support safe planned exits, it cannot support that entry strategy. Necessary reducing exits should follow the broker's actual permitted procedure, with any restriction reported.

## 5. ASX gold equities

Verified research examples:

| Issuer | ASX code | Primary source |
| --- | --- | --- |
| Northern Star Resources | NST | [Issuer profile](https://www.nsrltd.com/about-us/) |
| Evolution Mining | EVN | [Issuer share-price page](https://evolutionmining.com/investor-centre/share-price/) |
| Genesis Minerals | GMD | [Issuer profile](https://genesisminerals.com.au/about-us/) |

Ordinary initial ASX holdings generally face a marketable-parcel requirement of **A$500 per security**, plus brokerage. This is not a universal minimum for every subsequent purchase or every custody arrangement. Confirm the chosen broker's treatment. ASX guidance describes investing from A$500 plus costs; its market-rule definition establishes the parcel size. See [ASX shares](https://www.asx.com.au/investors/learn-about-our-investment-solutions/shares) and [market-rule procedures](https://www.asx.com.au/documents/rules/asx_market_rule_procedures.pdf).

ASX cash-equity settlement remains **T+2** in the inspected current material, with a possible T+1 transition under discussion. Recheck before implementation. See [ASX T+1 information](https://www.asx.com.au/markets/clearing-and-settlement-services/t1-settlement-cycle).

The capital requirement under this pack's risk limits can substantially exceed A$500. Illustratively, an A$500 minimum position under a 20% symbol cap requires at least A$2,500 equity. A 2% planned stop on that parcel risks A$10; a 0.25% equity risk budget requires at least A$4,000 before fees/gap allowance. These are sizing calculations, not recommended deposits or assured loss caps.

Research these stocks now; activate them only after capital, data, account/API capability, and risk feasibility are established. Use `Australia/Sydney` calendars rather than a fixed Berlin offset. Opening auctions, halts, and daylight-saving changes require explicit handling.

## 6. Real-time data

Alpaca's inspected equity data plan lists:

| Property | Basic | Algo Trader Plus |
| --- | --- | --- |
| Listed price | Free | USD 99/month |
| Real-time coverage | IEX | All US stock exchanges |
| WebSocket symbol subscriptions | 30 | Unlimited |
| Historical recent-data restriction | Latest 15 minutes restricted | No such restriction |

Source: [Alpaca market-data plans](https://docs.alpaca.markets/us/docs/about-market-data-api). Confirm entitlements and terms at use time.

Real-time IEX is a single-venue feed, not a complete consolidated view. It can support development and specifically validated same-feed strategies. Do not use its volume as total market volume or its quotes as guaranteed NBBO. Delayed SIP data can support offline research; it cannot be presented as current execution evidence.

The paper simulator may fill using a different quote basis from the feed visible to a paper-only user. Overlay conservative costs and compare the bases; otherwise a seemingly favorable paper result may not be reproducible by the bot's observable data. Provider simulation omits several live frictions. See [paper-trading limitations](https://docs.alpaca.markets/us/docs/paper-trading).

If entry safety requires consolidated quotes and they are not entitled, block the live strategy or validate a different affordable approach. Do not buy the USD 99 plan automatically to make the design work.

## 7. Instrument access and owner setup

European retail users may face product-document restrictions on US ETFs. A US ticker in a tutorial is not automatically tradable by the owner; do not default to SPY/QQQ/GLD as live holdings. Observing an entitled price series is a separate permission from buying the product. See [IBKR explanation of restricted US ETFs](https://www.interactivebrokers.co.uk/en/trading/products-cfds.php). V1 uses ordinary shares and does not substitute CFDs.

Before live enablement, the owner supplies the dedicated account ID, actual residency/account entity, approved operating budget, required credentials, and the selected protection policy. The coding agent provides exact setup navigation and a completed feasibility report; it must not ask for keys in chat or store them in source files.

The feasibility report must include one realistic tiny order's entry/exit fees, quantity precision, transfer costs, data entitlement, rule profile, available-funds semantics, API session/restart behavior, and protection method. A broker is usable only when all necessary entries **and exits** work under the actual account constraints.
