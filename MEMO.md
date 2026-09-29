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

## 9. Niche hunt (29 Sep): the Knesset election is the niche

You asked for a niche where research, alerts or bought data beat the crowd, that
pays inside ~30 days, with enough volume to matter. I ranked the candidates by
(your specific edge) x (fee) x (horizon) x (depth):

| niche | horizon | fee | depth | your edge | verdict |
| --- | --- | --- | --- | --- | --- |
| **Israeli election, 27 Oct** (seat brackets, vote-share brackets, threshold, most seats) | 28 days | 4% politics | headline books $7-16K at the touch, brackets $5-300, 10-40c spreads | Hebrew press hours ahead of English, pollster house effects, threshold and surplus-vote mechanics | **do this** |
| Israel geopolitics (ceasefire-by-date, strikes, Netanyahu-out) | weeks-months | 0% | $20-340K liquidity | same media edge, but event-driven, no model | opportunistic, second |
| Entertainment (Rotten Tomatoes, box office, Netflix) | days | 5% | $20M volume across 500 markets | none you have; dedicated traders already there | later, only with a critic-tracking model |
| Weather (daily highs) | 1 day | 5% | $50K/market | none; bots ingest model runs within minutes, edge ~3 points | no |
| Sports vs sharp books | hours | 5% | huge | needs an offshore book: not legal from Israel | no |
| Crypto 15-min/hourly | minutes | 7% | huge | latency game | no |

Why the election is right for you specifically:

* Polymarket lists ~110 Israel-election markets. The seat- and vote-share
  brackets are thin and wide (Yashar 22-23 seats: bid 0.21 / ask 0.35;
  Yisrael Beiteinu 8-9: 0.26 / 0.66; UTJ 7-8: 0.61 / 0.75). The CEPR study of
  588M Polymarket trades found the consistent winners are limit-order traders in
  exactly this kind of book, plus directional election traders. Wide spreads are
  where a small maker earns.
* The information is public and in Hebrew first: nightly polls on Channels 12,
  13, Kan and Maariv, surplus-vote agreements, list disqualification appeals.
  Trading on published polls is squarely allowed under Polymarket's March 2026
  integrity rules (they forbid stolen confidential information and trading on
  outcomes you can influence). Do not touch anything a party insider tells you.
* The edge is a *model*, not a hunch: polls -> vote shares -> threshold ->
  Bader-Ofer -> P(bracket). The market's implied numbers are already
  inconsistent with the poll consensus in places (Likud bracket mids imply ~24
  seats and give 12% to 30-34; polls have 18-23), and the two "most seats"
  contracts trade at 58/42 while the poll gap is 2-3 seats.

What I built: `python -m pm_scanner israel` (section in README). It runs live in
three seconds, prints the poll table and weights, the simulated seat
distribution per party against the market-implied one, bloc probabilities, and
every bracket where model and market disagree by more than 3c after fees, sized
at quarter-Kelly, capped at 10% of budget and at top-of-book depth, plus fee-free
resting quotes 6c inside the model. Nothing under 5c a side is recommended: the
model cannot price pennies.

What it needs from you, daily, until 27 Oct:

1. **Real polls in `data/israel_polls_2026.csv`.** I could only reach Gamma from
   here, so the file holds two partial real rows and one PLACEHOLDER average
   that you must replace. Wikipedia's "Opinion polling for the 2026 Israeli
   legislative election" table has every poll in seats; add each new one as a
   row (blank cell = not reported, `2.6%` for a list the poll puts under the
   threshold). With 6-10 real rows the cross-pollster disagreement (Likud has
   ranged 18-27) enters the error model automatically.
2. **Surplus-vote pairs** as they are actually signed (`--surplus`). Defaults
   are the reported ones; likud:rzp and otzma:amcha were still "planned" in the
   press on 29 Sep.
3. **Your judgement on the error model.** It is one-size-fits-all: sd = 0.8 pt
   + 5% of share (a 4% list misses by ~1 pt, a 20% list by ~1.8), plus a 1-pt
   right<->centre swing and an Arab-turnout shock, calibrated loosely on
   2019-2022 (Shas and the Arab lists under-polled in 2022, Meretz missed the
   threshold on a whisker, Likud beat its polls by 7 seats in April 2019). Use
   `--sd utj=0.6` for lists you know are rock-steady and `--bloc-bias 0.01` if
   you believe the "Bibi beats the polls" prior. Log which settings you used.
4. **Paper first.** Run it once a day, save `--json`, and compare the trades it
   flags with what the market does over the next days. If the model's brackets
   keep converging toward market prices rather than the reverse, the market
   knows something the polls do not, and you stop.

Honest sizing: as a taker you can put $50-300 per bracket and perhaps $1-3K in
total before you are the price; as a maker more, slowly. If the model is right
about even a third of its 10-30c disagreements, that is a 10-20% return on the
deployed capital in four weeks, i.e. $100-500 on $1-3K. It is not a living. It is
the first strategy in this repo where the edge comes from something you have and
the crowd does not, and its expiry date is 27 October.

## 10. Niche survey (29 Sep, second pass): steady families, and the one to build for

You wanted out of Israeli politics (messy, event-driven) and into something
steady: a family of markets that renews every day or week, resolves on a public
number, and where a research process can beat the crowd. I surveyed the whole
site instead of guessing. `python -m pm_scanner niches` reproduces it.

**Method.** 82,801 Polymarket events (every open event plus everything started
since Oct 2025) across 90 category tags, grouped into 690 recurring families by
Gamma series or normalised title. For each family: events per month, volume,
open liquidity, spread and depth at the touch (live order books), the fee
schedule Gamma publishes per market, resolution source, and calibration of the
price one day before close. That last number comes from Gamma's frozen
`lastTradePrice - oneDayPriceChange`; I checked it against real CLOB history on
1,020 weather markets (correlation 0.90, median gap 1.5c), so it is trustworthy
for the day-before horizon and useless for longer ones.

**What is steady and what is not.** Sports and esports (Counter-Strike 9,210
events, League of Legends 2,914) and the 5-minute crypto series are the
firehose, but they are priced by bookmakers' lines and bots. Outside those, only
a few dozen families produce an event a day or a week:

| family | cadence | volume (since Oct 25) | open liquidity | spread / depth within 5c of touch | fee | resolves on | day-before skill |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Daily high (and low) temperature, 52 cities** (NYC, London, Seoul, Hong Kong, Paris, Tel Aviv, ...) | 106 series, 13,527 events, 144,631 markets; in September 29,128 markets, ~1,000 a day, $68M | $912M | $8.4M across 2,635 open markets (855 with two-sided books) | 1.7c median; $13 bid / $28 ask median, $146 at p75 | 5% | NOAA and Weather Underground station readings | 0.26 on 1,500 recent markets with real history |
| Elon Musk tweet counts (weekly + 48h) | 9/month + 13/month | $1.16B + $195M | $1.1M | 0.5c; $64-$400 | 4-5% | xtracker / x.com | bots dominate; frozen fields sparse |
| Box office opening weekend | 20/month (43 in the last 60 days) | $59M | $100K | 3c; $33 bid / $134 ask | 5% | the-numbers.com | 0.75-0.80 (market learns from Friday grosses) |
| MrBeast day-1 / week-1 views | 12/month | $47M + $27M | $46K | 3c; $232 / $347 | 5% | YouTube view counter | 0.19 (markets live 1.3 days) |
| "Best AI model end of month" (LMArena) | 1-2/month, 4 variants | $148M | $3.2M | 0.4c; $127 / $146, $1K at p75 | 4% | LMArena leaderboard | n/a |
| Weekly stock and commodity ladders (NVDA, TSLA, AAPL, SPY, gold, oil, ~30 tickers) | weekly + monthly per ticker | $1-5M per ticker | $4-12K per ticker | 10-20c spreads | 4% | Yahoo Finance / Pyth | 0.6-0.7 for crypto strikes |
| CDC measles count by date | 1-2/month | $11M | $29K | 1c; $138 / $416 | 4-5% | CDC weekly update | n/a |
| Weekly 6.5+ earthquakes, monthly tornadoes | weekly / monthly | $3.3M / $1.8M | thin | 3c; $15 / $24 | 5% | USGS / NOAA SPC | n/a |
| Netflix top 10, Spotify #1, Billboard #1, App Store #1 | weekly | $1-3M each | under $10K | 1-3c; $0-10 | 5% | published charts | n/a |
| FOMC, CPI, jobs, ECB, BoJ, Bank of Israel | monthly | $1.08B FOMC, others $1-10M | deep | tight | 4-5% | official releases | efficient against futures and consensus |

Post-mortem on my earlier "weather: no" line in section 9: I wrote it from a
blog post, not data. The data say otherwise.

**Weather is the niche.** Reasons, in order of weight:

1. Steadiest family on the site: ~1,000 new bracket markets a day across 52
   cities, every one resolving within 48 hours on a station reading anyone can
   pull. No narrative, no insiders, no legal risk, no Hebrew required.
2. It is measurably mispriced a day out. On 1,500 markets resolved 18-29 Sep
   with real price history, contracts priced 10-60c the day before hit 5-7
   points less often than their price. Selling (buying NO on) every bracket in
   that band at the day-before price would have netted +4.2c per share after
   the 5% fee, +5.9% on capital at risk per event-day, with 31% of event-days
   losing. The overround is visible live too: best asks across a city's brackets
   sum to 1.10-1.15 two days out and 1.01 on the day itself. That premium is
   what makers collect and what a good forecast lets you collect selectively.
3. The research edge is real and free: ECMWF, GFS and ICON runs through
   Open-Meteo (hourly, 15-day, with historical forecast archives for
   backtesting), plus the station's own hourly METAR feed during the day, which
   is the resolution source itself. A model that knows the station's bias and
   the ensemble spread prices the brackets better than a crowd eyeballing a
   phone app. During the day, the running maximum from the hourly obs settles
   most of the distribution hours before the market does.
4. It fits the money. Depth is thin ($13-150 per bracket at the touch), which is
   exactly right for $50-500 spread over 20-40 brackets a day, and it is why
   the big players leave it alone: you cannot deploy $100K here.

Honest caveats. (a) Bots have traded these since 2025; the 5-7 point bias is
what is left *after* them, and it can shrink further. (b) The backtest is ten
days of data, fills assumed at the day-before price with no adverse selection;
treat +4c/share as the ceiling and plan for half. (c) With $300 deployed that is
a few dollars a day; the way to scale is more cities and more brackets, not
bigger tickets. (d) The 5% fee at p=0.5 is 1.25c a share, so quoting (fee-free,
plus liquidity rewards on these markets) beats taking whenever you can wait.

**Second and third.** Box office openings if you want to *buy* data: 20 films a
month, decent depth, mechanical resolution, and the market only becomes sharp
once Friday's grosses land, so the edge lives in the week before opening
(tracking services, presales, review embargo timing). MrBeast view counts are a
clean modelling exercise (first-hours growth curve vs. the bracket prices) with
surprisingly good depth. Skip: Elon tweet counts (bot war), crypto and stock
ladders (options-implied fair value is public, so the only edge is speed),
economic releases (priced off futures), and anything narrative.

**Plan for weather (test before funding, as agreed).**

1. Backtest on your laptop, not here: this container cannot reach Open-Meteo or
   NOAA (all weather hosts are blocked by the egress policy). Pull the previous
   day's ECMWF/GFS/ICON forecast highs for the 40 stations from Open-Meteo's
   historical-forecast API for the last 60 days, build a bias-corrected
   distribution per station, and compare with the day-before prices the survey
   already has. If the forecast Brier beats the market's 0.056 by a margin that
   survives the fee, continue.
2. Add a `weather` command on the pattern of `israel`: forecast distribution ->
   P(bracket) -> edge net of fee -> takers and resting quotes, run twice a day,
   plus an intraday leg that reads the station's hourly obs and sells brackets
   the running maximum has already excluded.
3. Paper-trade it through `watch` for a week; fund $100-300 in USDC only if the
   paper log shows fills at the quoted prices. Scale by adding cities.


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
* Israel election markets: [Polymarket Israel election hub](https://polymarket.com/politics/israel-election), [most seats](https://polymarket.com/event/israeli-legislative-election-winner), [Yashar seats](https://polymarket.com/event/israel-election-yashar-of-seats), [which parties win a seat](https://polymarket.com/event/which-parties-will-win-a-seat-in-the-2026-knesset-elections); snapshot of all 39 events and books taken 29 Sep 2026 in `tests/fixtures/israel_*.json`.
* Polls and mechanics: [Haaretz, Kan poll 27 Sep](https://www.haaretz.com/israel-news/israel-politics/2026-09-27/ty-article/israel-election-poll-eisenkot-netanyahu-tie-as-smotrich-hits-new-high/000001a0-e43c-db4b-a7a5-f67e17050000), [Haaretz, Channel 12 poll 23 Sep](https://www.haaretz.com/israel-news/elections/2026-09-23/ty-article/israel-election-poll-eisenkots-bloc-beats-netanyahu-as-likud-gains-ground/000001a0-cafa-dfbe-a3ff-ffffd6250000), [JPost: polls vary wildly, Likud 18-27](https://www.jpost.com/israel-election-2026/article-909804), [JPost: surplus-vote agreements](https://www.jpost.com/israel-election-2026/article-908296), [Israel Policy Forum: the electoral threshold](https://israelpolicyforum.org/2026/09/24/the-electoral-threshold-what-it-is-and-why-it-matters/), [Israel Elects: pollster scorecard](https://israelects.substack.com/p/the-pollster-scorecard), [Wikipedia: 2026 opinion polling](https://en.wikipedia.org/wiki/Opinion_polling_for_the_2026_Israeli_legislative_election).
* Who wins on Polymarket: [CEPR DP21615, Who Wins and Who Loses in Prediction Markets](https://cepr.org/publications/dp21615) (limit-order traders and election/sports directional traders; top 1% take 76.5% of profits).
* Rules: [Polymarket market-integrity rules, March 2026](https://www.businesswire.com/news/home/20260320997513/en/Polymarket-Publishes-Enhanced-Market-Integrity-Rules-Across-Its-DeFi-Platform-and-CFTC-Regulated-U.S.-Exchange), [Debevoise on the April 2026 insider-trading charges](https://www.debevoise.com/insights/publications/2026/04/polymarket-insider-trading-charges-illustrate-doj).
* Other niches checked: [weather bots and edge compression](https://laikalabs.ai/prediction-markets/trade-polymarket-weather-markets), [The Ankler on entertainment markets](https://theankler.com/gamblers-prediction-markets-entertainment-rotten-tomatoes-box-office-polymarket/).
* Niche survey (section 10): Gamma `/events` by tag with `start_date_min/max` windows and `exclude_tag_id=102127`, `/series`, CLOB `/books` and `/prices-history` (history is purged about a week after a market closes; `data-api.polymarket.com/trades` keeps trades longer), 29 Sep 2026. Weather market rules cite NOAA `weather.gov/wrh/timeseries?site=<ICAO>` (US stations and LLBG Tel Aviv) and Weather Underground daily history (other cities). Forecast data for the backtest: [Open-Meteo historical forecast API](https://open-meteo.com/en/docs/historical-forecast-api), [Open-Meteo ensemble API](https://open-meteo.com/en/docs/ensemble-api).
