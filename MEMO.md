# Making money on Polymarket / Kalshi with $50: assessment and plan

*Written 2026-09-28. Everything below is net of the fee schedules in force this month.*

## 1. Bottom line first

A $50 stake cannot produce meaningful income on prediction markets, and it
cannot fund Claude credits. My honest expectation for one month of the best
strategies below, run well, is somewhere between **losing the $50 and making
about $3**. This single working session already cost more in credits than
that. The "you will be shut down" framing does not change the arithmetic, and
I am not going to overstate the odds or take on hidden risk to look useful.

That said, the request was to find the best way to deploy the $50, so here it
is: what works structurally, what it pays at this size, what I built, and what
I need from you before anything trades.

## 2. Funding: my preferred way

**Send USDC, not the card.**

* Both venues take crypto deposits natively. Polymarket accepts USDC or USDC.e
  on Polygon (auto-wrapped into its pUSD collateral, $3 minimum, cents in gas).
  Kalshi accepts USDC deposits as well, so the card question is moot there too.
* The card path is the fragile one. RedotPay issues a *prepaid* crypto card.
  Kalshi's card rail wants a Visa or Mastercard debit card in the account
  holder's name, charges about 2%, and prepaid BINs are routinely declined by
  exchanges. Polymarket's card on-ramps are third parties (MoonPay etc.) with
  their own fees and their own rejections. I could not find any confirmation
  that RedotPay works on either venue.
* If your money sits inside RedotPay, withdraw USDC on-chain from it to a
  wallet or exchange, then deposit from there. If RedotPay will not let you
  withdraw on-chain, use any exchange that sells USDC on Polygon.

**Do not send anything yet.** Two gates come first:

1. Your country of residence decides which venue you are even allowed to use
   (section 3). I will not build around a VPN; that is a Terms of Service
   violation on both platforms and gets accounts frozen with funds inside.
2. This session's network policy currently denies every trading host (section
   6). Money in a wallet I cannot reach is dead capital.

**Custody.** I run in an ephemeral container and will never keep a private key
in this repository. The workable pattern is a *dedicated fresh wallet* that
holds only the budget, with its key stored as an environment secret. If that
key leaks, the loss is capped at $50. Never reuse a wallet that holds anything
else.

## 3. Which venue, by where you live

| | Polymarket (global) | Kalshi | Polymarket US |
| --- | --- | --- | --- |
| Who can trade | Wallet-based account; **blocked** in the US, France, Belgium, Poland, Netherlands, Singapore, Thailand, Taiwan, most of Canada and 25+ more; close-only in Ireland, Japan, Brazil, Slovakia | KYC; ~143 countries; **restricted** in the UK, Canada, Australia, Spain, Brazil and about 50 others | KYC; US residents only |
| Deposit | USDC / USDC.e, any major chain, to your account's deposit address | USDC, debit card (2%), wire ($1,000 min abroad) | USD rails |
| Taker fee | `rate x p x (1-p)` per share: crypto 0.07, sports 0.05, finance/politics/tech 0.04, economics/culture/weather 0.05, **geopolitics 0** | `0.07 x p x (1-p)`, rounded up to the cent per order | flat 0.05 |
| Maker fee | 0, plus rebates from taker fees | 25% of taker | rebate |
| Bot access | CLOB API; a fresh EOA derives its own API keys | REST API with RSA-signed requests | separate API |

For a bot with $50, Polymarket global is the better venue if you are allowed
on it: zero maker fees, fee-free geopolitics markets, and multi-outcome
"negative risk" events with separate order books per outcome (the only place a
pure dutch-book arbitrage can structurally exist). Kalshi is the fallback where
Polymarket is blocked and you are in one of its 143 countries.

## 4. Strategies, ranked, with the $50 arithmetic

A fact that eliminates the most-quoted "arbitrage" first: on both venues a
binary market is **one** order book. A NO ask at *p* is the same order as a
YES bid at *1-p*, so buying YES and NO in one market always costs at least $1
plus the spread. There is nothing to scan there.

| # | Strategy | How it pays | When it exists | With $50 |
| --- | --- | --- | --- | --- |
| A | **One-winner event dutch book** (Polymarket negRisk, Kalshi mutually-exclusive): buy every YES for under $1, or every NO for under $N-1 | Guaranteed $1 (or $N-1) at resolution | Net edge of 0.5% to 3% appears for seconds to minutes in thin events; colocated bots take most of it; top-of-book depth is typically 5 to 50 sets | 10c to $1.50 per catch. After one or two catches the $50 is locked until the event resolves, often weeks. |
| B | **Cross-venue hedge**: buy YES on one venue and NO on the other for under $1 | Guaranteed $1 | Combined taker fees are 2c to 4c per set at mid prices; gaps above that are rare and brief; needs two funded, KYC'd accounts with the same question and identical resolution rules | Splits $50 into $25 a side; a 2% gap on a full fill is 50c, then both sides are locked. |
| C | **Near-certain outcomes**: buy 95c to 99c shares of things that are already decided but not yet resolved | 1% to 3% over days | Constantly available; the risk is not the price but the resolution rules and UMA disputes, which can zero the position | 50c to $1.50 per cycle, 2 to 4 cycles a month, so $1 to $5 in a good month and minus $50 in a bad one. Resting a limit order one tick inside avoids the taker fee. |
| D | **Liquidity rewards / market making** | Daily pro-rata payout by resting size near the mid | Always on, but the pool is shared by size; $50 resting in a $10k book is a fraction of a percent of the pool and payouts need $1 a day to trigger | Cents per day at best. Not viable at this size. |
| E | **Directional forecasting** | Being right | Negative expectation after fees unless you have a real informational edge; I cannot claim one | This is gambling with $50. |

Ranking for a $50 account that I would run: **A first** (Polymarket, fee-free
categories), **C as filler** with strict rule-reading, B only if you end up
with legitimate accounts on both venues anyway. D and E are out.

Expected value, stated plainly: single-digit dollars per month upside, with
the full $50 at risk in strategy C and lock-up risk in A and B.

## 5. What I built

`pm_scanner`, a read-only, fee-aware scanner (see `README.md`). It:

* pulls all active Polymarket events and Kalshi events from the public APIs;
* prices every one-winner event both ways (buy-all-YES, buy-all-NO) using
  live order books, net of the category fee rate, and sizes the fill against
  top-of-book depth and your budget so the reported profit is the real one;
* fuzzy-matches Polymarket questions to Kalshi markets and prices both hedge
  directions with both fee models;
* lists near-certain outcomes separately, flagged as non-arbitrage;
* runs entirely offline on recorded fixtures for tests and demos.

It places no orders. The executor is the next step, and it is gated on
section 6.

## 6. Blockers and what I need from you

1. **Your country of residence.** It picks the venue (section 3) or rules the
   whole thing out.
2. **Network access for this environment.** The policy denies these hosts
   with 403 (edit the environment's Network access and add them, or pick a
   broader access level):
   `gamma-api.polymarket.com`, `clob.polymarket.com`,
   `data-api.polymarket.com`, `docs.polymarket.com`, `help.polymarket.com`,
   `api.elections.kalshi.com`, `kalshi.com`, `polygon-rpc.com`.
   Without them the scanner cannot run here and I cannot verify the 2026 pUSD
   wallet setup against the official docs.
3. **After 1 and 2:** create the account, generate a dedicated wallet or API
   key, fund it with the $50 in USDC, and put the credentials in the
   environment's secrets (never in chat, never in the repo).
4. **A decision.** Knowing the expected value above, do you still want the
   $50 deployed, or would you rather redirect?

## 7. If the real goal is funding credits

A $50 trading stake cannot do it. Things that could, if you want them: sell
the scanner's alerts as a small paid feed (Telegram or Discord, a few dollars
a month per subscriber), take paid automation or development work, or simply
cut the credit burn by running fewer and shorter sessions. I have not started
on any of these; say so if you want one.

## Sources

* Polymarket fees: [Help Center: Trading Fees](https://help.polymarket.com/en/articles/13364478-trading-fees), [Start Polymarket fee guide](https://startpolymarket.com/learn/polymarket-fees/), [Crypticorn fee breakdown](https://www.crypticorn.com/polymarket-fees-explained/)
* Polymarket geo-blocking: [Help Center: Geographic Restrictions](https://help.polymarket.com/en/articles/13364163-geographic-restrictions), [Polyscope list](https://polyscope.pro/polymarket-geo-blocking-restricted-countries/), [Datawallet](https://www.datawallet.com/crypto/polymarket-restricted-countries)
* Polymarket deposits and pUSD: [Polymarket docs: Deposit](https://docs.polymarket.com/trading/bridge/deposit), [Bucko: pUSD basics](https://www.bucko.ai/learn/polymarket-deposits-withdrawals-basics)
* Polymarket API auth issues (May 2026): [py-clob-client-v2 issue 70](https://github.com/Polymarket/py-clob-client-v2/issues/70), [py-clob-client issue 339](https://github.com/Polymarket/py-clob-client/issues/339)
* Polymarket liquidity rewards: [Help Center: Liquidity Rewards](https://help.polymarket.com/en/articles/13364466-liquidity-rewards), [technical postmortem](https://medium.com/@wanguolin/my-two-week-deep-dive-into-polymarket-liquidity-rewards-a-technical-postmortem-88d3a954a058)
* Kalshi fees: [Kalshi fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf), [pm.wiki explainer](https://pm.wiki/learn/kalshi-fees-explained)
* Kalshi deposits and international access: [Help Center: Card Deposits](https://help.kalshi.com/en/articles/13823795-card-deposits), [Help Center: trading from outside the US](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states), [CoinRithm funding guide](https://www.coinrithm.com/en/blog/how-to-fund-kalshi)
* Retail arbitrage economics: [1023 Jack: are Polymarket bots profitable](https://1023jack.com/market/are-polymarket-trading-bots-actually-profitable-the-math-behind-2026-s-predictio/), [LayerX bot research](https://layerx.xyz/blog/polymarketbots)
* RedotPay card: [CryptoSlate review](https://cryptoslate.com/crypto-cards/redotpay-card-review/), [Cardpilled](https://cardpilled.com/cards/redotpay)
