# Strategy playbook

The one place that says what we trade, what the evidence for each rule is, what each market costs, and how a rule gets into the book, moves up and drops out. The numbers come from the registered trials (`research/experiment_registry.jsonl`, reports in `research/reports/`) and from the demo account's own quotes. The live setups are `trader_setups` in `configs/practice/setups.yaml`; where each one stands is `python -m trader.practice forward`. Update this file whenever a trial finishes or a forward test is reviewed. Last updated 2026-11-11 (ADR-0062).

## 1. Where we stand

**No rule we have tested earns money in a way that matters at this size.**

- 26 trials have been registered and run. One rule passes the practice rule (ADR-0052) on the broker's own quotes: weekly momentum on Bitcoin and Ether (T-0019). It made **+19.51 USD summed over 34 independent three-month windows of USD 100, about 2% a year**, +1.23 USD with costs doubled (about nothing), and +0.42 USD in walk forward.
- Holding Bitcoin and Ether over the same windows made **+239.50 USD**. Every rule that made money made a fraction of what holding the same asset made. The one place a rule beat holding was a falling market (T-0023's nineteen months: rule +15.13, holding -61.26).
- The stocks bot's rule (Candidate A) lost in every test (T-0006 to T-0010: -61 to -93 USD, and -16 to -18 USD even with no fees). It trades because the owner chose practice mode (ADR-0040), not because it earns.
- So the only thing in this data that made money is owning assets that rose. The active rules recover their costs at best. Nothing here supports a goal of USD 10,000 from USD 300 by trading skill alone.

## 2. What was tested

Net USD is the sum over independent three-month windows, each a fresh deployment (USD 50 for T-0001 to T-0017, USD 100 from T-0018). "Before costs" is zero fees (older studies) or mid prices with no financing (CFD studies). "Doubled" is the stressed view: spread and slippage doubled, financing times 1.5, 5% of entries missed. "Hold" is buying and holding the same markets over the same windows.

| Trials | Rule | Market and period | Net | Before costs | Doubled | Hold | Read |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-0001 to T-0005 | breakout rules A and B, 15-minute bars | Bitcoin and Ether spot, Kraken fees | -0.8 to -22.9 | -8.0 to -11.3 | -0.8 to -21.1 | -8.2 | no edge before costs |
| T-0006 to T-0010 | breakout rules A and B, 5-minute bars | five US stocks, 2020-08 to 2026-08, 1,275 to 1,999 trades | -60.7 to -93.4 | -15.9 to -18.1 | -48.1 to -75.9 | -2.3 | no edge before costs |
| T-0011, T-0012 | trained filter on the crypto breakouts | Bitcoin and Ether | not trainable: about 1% of labels profitable | | | | dead end |
| T-0013 | RSI(2) dip-buy | four ETFs | -1.6 | +1.2 | -1.6 | +24.3 | Alpaca's fixed cents at USD 50 ate it |
| T-0014 | 10-month trend timing | four ETFs | -1.6 | +3.0 | -1.7 | +24.3 | same |
| T-0015 | 10-month trend, sized at 1% risk | SPY, QQQ, TLT, GLD, 80 trades | **+6.3** | +10.0 | +5.4 | +32.6 | positive; G2 not passed (too few trades) |
| T-0016 | 10-month trend, sized | Bitcoin and Ether daily, 33 trades | **+8.9** | +11.1 | +7.8 | +165.7 | positive; 33 trades |
| T-0017 | RSI(2) dip-buy, sized | four ETFs, 239 trades | -6.0 | +4.1 | -6.6 | +27.8 | fees ate it |
| T-0018 | RSI(2) dip-buy | four US index CFDs, 122 trades | +4.4 | +10.0 | -0.0 | +88.5 | financing 3.45 of gross 7.85; forward test |
| T-0019 | weekly momentum, long or short | Bitcoin and Ether CFDs, 372 trades | **+19.5** | +32.8 | +1.2 | +239.5 | **the one rule that passes**; walk forward +0.42 |
| T-0021 | break of the 24-hour range | gold, US 500, US Tech 100, 2,425 trades | -80.5 | -1.4 | -141.3 | +106.8 | no edge even at mid |
| T-0022 | fade of the 24-hour break | same, 2,972 trades | -103.5 | -12.5 | -151.0 | +106.8 | no edge |
| T-0023 | break of the 24-hour range | Bitcoin and Ether, 2025-01 to 2026-07, 584 trades | +15.1 | +54.2 | -27.5 | -61.3 | fails doubled costs; forward test. As one continuous account: -2.30 after 130 trades |
| T-0024 | fade | same, 408 trades | -58.9 | -49.7 | -66.5 | -61.3 | no |
| T-0025 | weekly momentum | gold, silver, oil, EUR/USD, four indices, 693 trades | -14.4 | +21.5 | -41.6 | +93.8 | gross -2.06: the rule does not carry over from crypto |
| T-0026 | trained veto on T-0023 | same as T-0023 | +5.0 (rule alone +15.1, random refusal +9.5) | +19.0 | -10.9 | | the model refuses no better than chance |

## 3. What the numbers say

1. **Intraday rules on liquid markets have no edge before costs.** Stocks and crypto at 5 to 15 minutes (T-0001 to T-0010) and gold and the indices at one hour (T-0021, T-0022) are negative even with no fees, so the spread is the whole result. Crypto at one hour earned before costs only in a stretch when holding the two coins lost money (T-0023: shorts +28.24, longs -13.11; holding -61.26 summed over the same windows), and its fade lost.
2. **Slow rules earn a little before costs; costs decide.** T-0013 to T-0019 are all positive before costs. After costs, ETFs at USD 50 lose to Alpaca's fixed cents (about 0.04 USD a round trip on a 1.5 to 5 USD position). On CFDs a long pays financing every night: about 2.2 bps on the indices (8% a year), 6.2 bps on Bitcoin and Ether (22% a year). A week-long Bitcoin long costs about 0.43% in financing before the spread. Financing took 3.99 of T-0019's 23.50 gross, 3.45 of T-0018's 7.85, and 13.61 in T-0025 against a gross of -2.06.
3. **Nothing beat holding in a rising market.** The table's Hold column is larger than Net on every row where the asset rose. A trend rule's real job is to avoid the drawdown (T-0023's period), not to beat the climb.
4. **A rule does not carry from one asset class to another.** Weekly momentum made money on Bitcoin and Ether (T-0019) and nothing on gold, silver, oil, EUR/USD and four indices (T-0025, gross -2.06 before costs). Treat the crypto result as one asset in one long rise until forward trades say otherwise.
5. **Filters did not help.** The trained veto (T-0026) did worse than refusing the same number of trades at random.
6. **Scale.** A sleeve is USD 100 and risks 1% (crypto) or 2% (CFDs) at the stop, so a stop-out costs USD 1 to 2 and a position is roughly USD 10 to 85 of notional. T-0019's average trade nets 5 cents. A rule with a real edge earns cents to a few dollars a month here; a good year of simply holding a stock index is a few to ten percent, USD 3 to 10 on a sleeve.

## 4. The book today

The trader (`python -m trader.practice trade --live`, `npm run trader`, ADR-0056) runs these. `configs/practice/setups.yaml` is the truth; the console's "Practice trader" block shows each one's last decision and result.

| Setup | Sleeve | Status | Rule | Decides |
| --- | --- | --- | --- | --- |
| `weekly_momentum_v1` | crypto | tested (T-0019) | long if the week rose, short if it fell; stop 3 x ATR(10), target 2R, out after a week | Monday 00:00 UTC |
| `crypto_hourly_breakout_v1` | crypto | forward test (T-0023) | close beyond the previous 24 hourly bars; stop 2 x mean true range, target 2R; flat by 21:00 UTC | hours ending 00:00 to 17:00 UTC |
| `index_dip_v1` | gold and CFDs | forward test (T-0018) | close above the 200-day average and RSI(2) below 10: buy the next open; sell at the open after the first close above the 5-day average; stop 3 x ATR(10) | the first open after a daily close (Friday's at the Sunday 23:00 UTC open) |
| `practice_commodity_breakout_v1` | gold and CFDs | practice (ADR-0060, ADR-0063) | the T-0021 break on gold, silver, copper | hours ending 01:00 to 20:00 UTC, flat by 21:00 |
| `practice_energy_breakout_v1` | gold and CFDs | practice | the same rule on crude oil, Brent, heating oil, natural gas | same |
| `practice_macro_breakout_v1` | gold and CFDs | practice | the same rule on EUR/USD, US 500, US Tech 100, US 30 | same |
| `practice_crypto_breakout_v1` | crypto | practice (ADR-0060) | the T-0023 break with longer hours | hours ending 00:00 to 20:00 UTC, flat by 21:00 |
| Candidate A, exploration book | stocks (Alpaca paper, USD 300) | practice (ADR-0057, ADR-0064) | the T-0006 to T-0010 rule on 20 large stocks: close above the previous six 5-minute bars and above the session's VWAP; stop 1.5 x ATR(6), target 2R, out after 60 minutes | every 5 minutes, 15:00 to 20:30 UTC, flat before the close |

The stocks row is run by the paper bot (`npm run bot`), not by the trader. Its limits are in `configs/policy/paper_explore_v2.yaml` (USD 75 a stock, five positions, 30 entries a session), and `trader paper day --config configs/paper_explore.yaml` reports a session.

Fixed by the owner: crypto and CFD trades are held days to a week at most (ADR-0049). Slower holds are his call.

Slots are setups (ADR-0063): the policy holds one setup's notional to its sleeve's equity, and one contract is USD 40 to 85, so a setup holds about one position. The break runs as three setups by family so the sleeve can hold up to four trades at once, each family with its own ledger and loss cap.

## 5. How a rule moves: the league

1. **Idea** (section 7). It needs a trial slot, which only the owner can add (`trader research decide-budget`), and a decision record with every number fixed before it runs.
2. **Trial**: a backtest on the broker's own bid and ask. ADR-0052's rule decides: net positive in the base view, in the doubled-cost view and in walk forward. A pass makes it a **tested** setup. A rule that fails one check may run as a **forward test** if the owner says so. A rule that fails more is not traded. The rule is never loosened after a result is known.
3. **Practice** (ADR-0060): a rule without a passing trial kept trading on demo, as the stocks bot runs `--practice`. Labelled in the console, never research evidence.
4. **League** (ADR-0062): the forward ledger (`python -m trader.practice forward`) puts every setup in one state by rule.

| State | Meaning | Then |
| --- | --- | --- |
| `COLLECTING` | fewer closed trades than its review count (60) | nothing; a result on so few trades says nothing |
| `CANDIDATE` | at the count with a positive net and a positive average a trade | register a trial for it; this is not a pass |
| `DROP` | at the count with a net at or below zero | remove it from the setups |
| `STOPPED` | below its loss cap (-5 USD for forward tests and practice) | no more entries; owner review |
| `OFF` | switched off in the setups file | |

5. **Nothing here moves to real money.** That needs the gates in `docs/specs/risk_and_validation.md` (G2, G3, G4) and the owner's enablement.

**Adding a market to a rule** (reuse):

1. Read the broker's `/markets/<epic>`: spread, minimum size and step, margin, overnight fee, opening hours. Reject a market whose spread is over the entry limit (20 bps CFDs, 60 bps crypto) or that closes before the rule's flat hour.
2. Add the epic to the next version of the instrument file (`configs/instruments/capital_demo_cfds_vN.yaml`; never edit a version a trial binds), to the sleeve's rules (`configs/practice/capital_demo.yaml` or `crypto_cfd.yaml`: size range and group), and to `CAPITAL_DEMO_EPICS` in `src/trader/config/secrets.py` with the two tests that pin it.
3. Add it to the setup's `epics`, then restart the trader.
4. A tested rule on a new market is a practice setup until a trial says otherwise (point 4 above).

**Adding a rule**: a family in `strategies/cfd_rules.py`, a spec file, a study file in `configs/research/`, a decision record, `trader research register`, `trader research cfd`, pin the inputs in `tests/test_registered_inputs.py`, then a `trader_setups` entry.

## 6. What the markets cost

Quoted spread in basis points (2026 median from the hourly dataset, or the quote read on 2026-11-11 where marked *), and financing per night in basis points on the notional (long / short; negative is paid to you). Rates move: silver's long charge was 1.5 on 2026-11-10 and 3.9 on 2026-11-11.

| Markets | Spread | Financing long / short |
| --- | --- | --- |
| US 30, US Tech 100, US 500, EUR/USD | 0.4 to 0.8 | 2.2 / 0 (indices), 0.8 / 0 (EUR/USD) |
| Gold, Russell 2000 | 1.1 to 1.8 | gold 1.5 / -0.7 |
| Copper* | 2.7 | 1.1 / 1.1 |
| Brent*, crude oil, heating oil* | 4.3 to 4.8 | 1.1 / 1.1 |
| Bitcoin, Ether | 6.8, 8.1 | 6.2 / -1.4 |
| Silver | 8.5 to 9.6 | 3.9 / -3.1 (2026-11-11) |
| Natural gas* | 14.2 | 1.1 / 1.1 |

Left out. Over the entry limit: platinum (39), palladium (70), nickel (40), aluminium (34), lead (39), Copper UK (22). Inside it but closing between 17:30 and 19:20 UTC, before the 21:00 UTC flat time: zinc (16), cocoa (9.5), US coffee (15), UK coffee (6.2), wheat (13). Rare earths and magnet metals have no commodity CFD on Capital.com; only company shares and ETFs (MP Materials, lithium and uranium funds), a different product.

## 7. What to try next

In order. Each needs a budget slot and a decision record first (section 5).

1. **Weekly cross-sectional momentum on liquid US stocks** (T-0020, the stocks plan). The strongest prior in the book: decades of evidence in many markets (Jegadeesh and Titman 1993), liquid names, costs of cents at Alpaca. It needs the daily mode and the engine hook of the stocks plan (stages 2 and 3).
2. **Hold with a trend exit, on spot and ETFs, at a size where fixed fees stop mattering.** T-0014 at USD 1,000 made +51.77 where USD 50 lost 1.62: the fees stop mattering at size, but +51.77 over 38 quarters is about 0.5% a year, so the rule is not the earner. The earner is the asset's own rise; the trend exit only cuts the drawdown (T-0015: +6.3 against +32.6 for holding). Moskowitz, Ooi and Pedersen (2012), Hurst, Ooi and Pedersen (2017) and Faber (2007) support it. It means holds longer than a week and spot instead of CFDs, so it needs the owner to change the crypto and CFD mandate.
3. **Turn of the month on the US indices**: long from the last session of a month to the third of the next, about 12 trades a year. Documented since Ariel (1987) and Lakonishok and Smidt (1988), weaker in recent decades. At CFD costs of about 14 bps a cycle it would earn cents.
4. **Relative value with financing hedged**: Brent against crude oil, gold against silver. It needs a two-leg engine.
5. **Cross-sectional momentum across the large crypto coins** (Liu, Tsyvinski and Wu 2022). It needs hourly bid and ask history for more coins and pays wider spreads.

Not worth a slot: more break and fade variants of the 24-hour range (T-0021 to T-0026 settled it), more filters on the same rule, other lookbacks of the weekly rule on the same markets (that is tuning).

## 8. Commands

- `python -m trader.practice forward`: every setup's demo trades, its league state and its trial's per-trade averages (writes `runs/practice/forward.json`).
- `uv run trader research status`: every trial, its latest result and the budget.
- `uv run trader research register --study <file>`, then `uv run trader research cfd --study <file>`: register and run a CFD study. Never two registry writers at once.
- `npm run trader`: start the trader in its own window (`-- --dry-run` decides without sending).
