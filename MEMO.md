# Making money on Polymarket / Kalshi with a small stake: assessment and plan

*Written 2026-09-28, updated 2026-09-29 for an Israel-resident account owner.
Everything below is net of the fee schedules in force this month.*

## 1. Bottom line first

A $50 stake cannot produce meaningful income on prediction markets, and it
cannot fund Claude credits. My expectation for one month of the best strategies
below, run well, is somewhere between **losing the stake and making a few
dollars**. The way to spend nothing and still learn the real answer is the
free paper-trading step in section 5; the way to burn money fastest is to have
me run the loop from a Claude session, so the plan below keeps me out of it.

## 2. Venue: Polymarket only

| | Polymarket (global) | Kalshi | Polymarket US |
| --- | --- | --- | --- |
| Israel | **Not on the geoblock list.** Access is IP-checked, wallet-based, no KYC. | **Restricted jurisdiction.** No account possible. | US residents only |
| Taker fee | `rate x p x (1-p)` per share: crypto 0.07, sports 0.05, finance/politics/tech 0.04, economics/culture/weather 0.05, **geopolitics 0** | `0.07 x p x (1-p)`, rounded up to the cent per order | flat 0.05 |
| Maker fee | 0, plus rebates from taker fees | 25% of taker | rebate |
| Bot access | CLOB API; a fresh wallet derives its own API keys | RSA-signed REST | separate API |

Two consequences:

* The cross-venue hedge (buy YES on one venue, NO on the other) is off the
  table: it needs a Kalshi account. The scanner still reads Kalshi's public
  prices as a *reference* when they are reachable, but nothing can execute.
* Polymarket's terms forbid VPN use to reach the site from a blocked region.
  Israel is not blocked, so that is not an issue here. What I cannot assess is
  Israeli law on this activity for a resident; that read is yours.

## 3. Funding: USDC, not the card, and not yet

* Polymarket accepts USDC or USDC.e deposits and wraps them into its pUSD
  collateral. Polygon is the cheapest route: $3 minimum, cents in gas.
* RedotPay issues a prepaid crypto card. Polymarket's card on-ramps are third
  parties (MoonPay and similar) with their own fees and rejections; I found no
  confirmation that a RedotPay card works there. If your money sits in
  RedotPay, withdraw USDC on-chain from it, or buy USDC on Polygon at any
  exchange, and deposit from there.
* **Do not fund anything until the paper-trading step (section 5) says it is
  worth funding.** Money in a wallet no bot is trading is dead capital.
* **Custody.** I run in an ephemeral container and never keep a private key in
  this repository. When the time comes: a *dedicated fresh wallet* holding only
  the trading budget, its key stored as an environment secret on your side.
  If the key leaks, the loss is capped at the budget. Never reuse a wallet
  that holds anything else.

## 4. Strategies, ranked, with the small-stake arithmetic

A fact that eliminates the most-quoted "arbitrage" first: a binary market on
Polymarket is **one** order book. A NO ask at *p* is the same order as a YES
bid at *1-p*, so buying YES and NO in one market always costs at least $1 plus
the spread. There is nothing to scan there.

| # | Strategy | How it pays | When it exists | With $50 |
| --- | --- | --- | --- | --- |
| A | **One-winner event dutch book** (Polymarket negRisk events): buy every YES for under $1, or every NO for under $N-1 | Guaranteed $1 (or $N-1) at resolution | Net edge of 0.5% to 3% appears for seconds to minutes in thin events; colocated bots take most of it; top-of-book depth is typically 5 to 50 sets | 10c to $1.50 per catch. After one or two catches the stake is locked until the event resolves, often weeks. |
| B | **Near-certain outcomes**: buy 95c to 99c shares of things already decided but not yet resolved | 1% to 3% over days | Constantly available; the risk is not the price but the resolution rules and UMA disputes, which can zero the position | 50c to $1.50 per cycle, 2 to 4 cycles a month, so $1 to $5 in a good month and minus $50 in a bad one. A resting limit order one tick inside avoids the taker fee. |
| C | **Liquidity rewards / market making** | Daily pro-rata payout by resting size near the mid | Always on, but the pool is shared by size; $50 resting in a $10k book is a fraction of a percent of the pool and payouts need $1 a day to trigger | Cents per day. Not viable at this size. |
| D | **Directional forecasting** | Being right | Negative expectation after fees unless you have a real informational edge; I cannot claim one | Gambling. |
| E | Cross-venue hedge | Guaranteed $1 | Needs Kalshi | **Not available from Israel.** |

Ranking for a small account: **A first**, in fee-free geopolitics and 0.04
politics events, **B as filler** with strict rule-reading, C and D out.

## 5. No LLM needed: run the scanner yourself, for free

Every detector in `pm_scanner` is plain arithmetic on order books. No model is
called anywhere. Running it on your laptop or a $5 VPS costs nothing in Claude
credits and nothing in money. What burns credits is *me*: every turn of a
Claude session costs real dollars, so the division of labour is:

* **You run it.** `python -m pm_scanner watch` re-scans every minute, logs
  every opportunity's appearance and disappearance to a JSONL file, and can
  push Telegram alerts. `python -m pm_scanner summarize watch.jsonl` turns a
  week of that into: opportunities per day by kind, median edge, median dollar
  value at your budget, and how many seconds they survive before a bot takes
  them.
* **I read the summary once** and we decide. One short session, not a loop.

Where a model would add value, and it is optional: reading a near-certain
market's resolution rules before you buy, and checking that two questions
really resolve identically. Both are a few calls a day. If you ever want that
automated, `claude-haiku-4-5` at $1 per million input tokens and $5 per million
output tokens makes each such check well under a cent; `claude-sonnet-5-5` at
$2 / $10 per million if you want more judgment. That is dollars a month, and
it is not needed for the paper-trading step at all.

Latency note: the dutch-book edge is a speed game. A laptop in Israel polling
once a minute will *see* opportunities and measure how long they last, which
is exactly what the test needs, but it will not *win* the sub-second ones.
Whether the ones that last a minute or more are worth having is what the log
will tell you.

## 6. How small is too small, and how big is worth it

The venue floors are low. Polymarket's minimum order is about 5 shares per leg
(the scanner reads each market's actual minimum and flags anything below it),
and the Polygon deposit minimum is $3. A 3-outcome buy-all-YES set costs about
$0.95 per set, so 5 sets is under $5; a buy-all-NO set costs about $N-1, so 5
sets of a 3-outcome event is about $10. **$50 is enough to prove the plumbing**:
that orders are accepted, fill, settle and are charged the fee you expected.

$50 is **not** enough to measure whether the strategy makes money, for two
reasons that have nothing to do with fees:

1. **Lock-up.** Each catch ties up its capital until the event resolves, from
   days to months. With $50 you get one or two catches and then sit idle.
2. **Sample size.** You want to see 20 to 30 catches to know your real capture
   rate and edge. At a few catches a week, that is one to three months, during
   which several catches are locked at once.

Capital needed for the strategy test is roughly `catches per week x average
ticket x average weeks locked`. Plausible numbers (3 a week, $25 a ticket,
4 weeks locked) give about $300 concurrently deployed; the paper-trading log
replaces my guesses with your measured values before you send anything.

Scaling up does not scale returns. Depth per catch is $5 to $50 at top of
book, and faster bots take the rest, so beyond a low four-figure stake the
extra money mostly sits idle. The 2026 research on retail arbitrage bots puts
the realistic ceiling for a solo operator in the low hundreds of dollars a
month even with good infrastructure.

**Staged plan**

| Stage | Money at risk | Duration | What it answers |
| --- | --- | --- | --- |
| 0. Paper trade with `watch` on your laptop | $0 | 1 to 2 weeks | How many catches a day, how big, how long they live, in which fee tiers |
| 1. Plumbing test | $30 to $50 | A few trades | API auth, fills, fees, settlement, the pUSD wallet setup |
| 2. Strategy test | $300 to $500, sized from Stage 0 | 1 to 2 months | Realised return net of everything, capture rate versus bots |
| 3. Scale | Only if Stage 2 is positive; ceiling is low | ongoing | Whether it is worth your time at all |

Stage 0 may well show that the executable, minute-long opportunities are too
rare to matter. That is a success of the test: it costs nothing and saves the
deposit.

## 7. What I need from you

1. **Run Stage 0.** Clone the repo on your laptop, then:
   ```bash
   pip install -e .
   python -m pm_scanner watch --platform polymarket --budget 50 --interval 60 --log watch.jsonl
   # a week later
   python -m pm_scanner summarize watch.jsonl
   ```
   Optional Telegram alerts: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. **Send me the summary** (or the JSONL). One session to read it and decide.
3. **Network allowlist, only if you want me to run or debug live from here.**
   In the session's title bar open the cloud environment menu, choose Edit,
   then Network access: pick a broader access level or add these hosts to the
   allowed domains, save, and start a new session in that environment. The
   hosts are `gamma-api.polymarket.com`, `clob.polymarket.com`,
   `data-api.polymarket.com`, `docs.polymarket.com`, `help.polymarket.com`,
   `polygon-rpc.com`, and optionally `api.elections.kalshi.com` for reference
   prices. Access levels are described at
   https://code.claude.com/docs/en/claude-code-on-the-web. If you run the
   scanner on your own machine this step is unnecessary for Stage 0.
4. **Nothing else yet.** No account, no wallet, no deposit until Stage 0 says
   so. Stage 1 will need `docs.polymarket.com` reachable from wherever the
   executor is built, so that the 2026 pUSD wallet setup is done from the
   official docs rather than from memory.

## 8. If the real goal is funding credits

A small trading stake cannot do it. Things that could, if you want them: sell
the scanner's alerts as a small paid feed (Telegram or Discord, a few dollars
a month per subscriber), take paid automation or development work, or simply
cut the credit burn by running fewer and shorter sessions. I have not started
on any of these; say so if you want one.

## Sources

* Polymarket fees: [Help Center: Trading Fees](https://help.polymarket.com/en/articles/13364478-trading-fees), [Start Polymarket fee guide](https://startpolymarket.com/learn/polymarket-fees/), [Crypticorn fee breakdown](https://www.crypticorn.com/polymarket-fees-explained/)
* Polymarket geo-blocking: [Help Center: Geographic Restrictions](https://help.polymarket.com/en/articles/13364163-geographic-restrictions), [Start Polymarket country list](https://startpolymarket.com/countries/), [CoinRithm country list](https://www.coinrithm.com/en/blog/polymarket-countries-and-availability), [Polyscope list](https://polyscope.pro/polymarket-geo-blocking-restricted-countries/)
* Polymarket deposits and pUSD: [Polymarket docs: Deposit](https://docs.polymarket.com/trading/bridge/deposit), [Bucko: pUSD basics](https://www.bucko.ai/learn/polymarket-deposits-withdrawals-basics)
* Polymarket order rules: [Polymarket docs: tick size](https://docs.polymarket.com/api-reference/market-data/get-tick-size), [QuantVPS CLOB overview](https://www.quantvps.com/blog/polymarket-clob-central-limit-order-book)
* Polymarket API auth issues (May 2026): [py-clob-client-v2 issue 70](https://github.com/Polymarket/py-clob-client-v2/issues/70), [py-clob-client issue 339](https://github.com/Polymarket/py-clob-client/issues/339)
* Polymarket liquidity rewards: [Help Center: Liquidity Rewards](https://help.polymarket.com/en/articles/13364466-liquidity-rewards), [technical postmortem](https://medium.com/@wanguolin/my-two-week-deep-dive-into-polymarket-liquidity-rewards-a-technical-postmortem-88d3a954a058)
* Kalshi fees and access: [Kalshi fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf), [Help Center: trading from outside the US](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states), [CoinPerps restricted list](https://www.coinperps.com/learn/kalshi-restricted-countries), [Laika Labs restricted list](https://laikalabs.ai/prediction-markets/kalshi-legal-supported-restricted-countries)
* Retail arbitrage economics: [1023 Jack: are Polymarket bots profitable](https://1023jack.com/market/are-polymarket-trading-bots-actually-profitable-the-math-behind-2026-s-predictio/), [LayerX bot research](https://layerx.xyz/blog/polymarketbots)
* RedotPay card: [CryptoSlate review](https://cryptoslate.com/crypto-cards/redotpay-card-review/), [Cardpilled](https://cardpilled.com/cards/redotpay)
