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

> **Correction (30 Sep).** The "5-7 points overpriced, +4.2c a share" claim
> below did not survive a proper measurement. It came from Gamma's frozen
> price fields, which drop a changing share of losing contracts, and from a
> twelve-day window. Re-measured from full trade histories on 251 sampled
> event-days across Dec 2025 - Sep 2026, the 10-60c brackets are priced fairly
> at local midnight (bias +0.4 +- 2.0 points) and selling them all loses about
> a cent a share after fees. Section 11 has the table. The ranking stands
> (steadiest family, mechanical resolution, free forecast data), but the edge
> has to come from forecasting better than the crowd, not from selling a
> premium that is not there.

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


## 11. Weather, second look (30 Sep): the one-liner, which forecasts, and is the gap still there

**1. The one-liner.** Everything is in the `weather` command; on a laptop with Python 3.10+:

```bash
git clone https://github.com/zaigor/agent-2-playground.git && cd agent-2-playground && git checkout claude/prediction-market-monetization-kq8bj4 && pip install -r requirements.txt && python -m pm_scanner weather --mode backtest --cities nyc,london,tel-aviv --days 45
```

It loads every daily-temperature event of the last 45 days from Gamma, pulls each
bracket's trade history from data-api (cached under `.cache/pm_trades/`, ~1,500
requests the first time, about 20 minutes), takes the price at local midnight of
the target day, pulls the forecast issued the day before from Open-Meteo's
previous-runs archive (ECMWF, GFS, ICON by default), fits a per-station bias and
error sd on the days already seen, prices every bracket, and prints: forecast MAE
and bias per model, Brier of forecast vs market on the same brackets, and the
paper P&L of trading only where they disagree by more than 5c, net of the 5% fee.
`--mode trend --cities all --days 270` reproduces the month-by-month table below
with as many sampled event-days as you have patience for; `--mode today` prices
the open books from the current forecast (add `--ensemble` for the ECMWF spread).
The container this was built in cannot reach Open-Meteo or IMS, so the forecast
path ran only against synthetic responses in the tests; the first real run is
yours, and if it fails the error prints the exact request.

**2. Is this arbitrage, and would local or Google forecasts help?**

It is not arbitrage. Nothing here is riskless; you are selling contracts the
crowd overprices and buying the ones it underprices, and single days will lose.
The edge is statistical: on a good day the sum of your fair prices is 1.00 and
the market's asks sum to 1.10-1.15, and you get paid the difference only on
average. Two ways to tilt it:

* *Better distribution, not just a better point forecast.* The market prices
  brackets; what matters is the whole curve. Multi-model blends with a
  station-specific bias correction beat any single source by a wide margin at
  day-1 lead, which is why the tool blends and calibrates rather than trusting
  one model. The ECMWF ensemble (51 members, free on Open-Meteo) gives the
  spread directly and `--ensemble` uses it.
* *Which sources.* Open-Meteo already carries the machine-learning models:
  Google DeepMind's GraphCast as run by NOAA (`gfs_graphcast025`) and ECMWF's
  AIFS (`ecmwf_aifs025_single`), next to the physics models. Google's newer
  WeatherNext 2 is not on Open-Meteo; it sits behind Google Cloud (BigQuery and
  Earth Engine), usable later if the cheap sources are not enough. Do not
  assume any of them wins at a specific airport: pass
  `--models ecmwf_ifs025,ecmwf_aifs025_single,gfs_graphcast025,icon_seamless,gfs_seamless`
  and read the MAE table; the answer differs by station and season.
* *IMS (ims.gov.il) and other national services.* For Ben Gurion a human-edited
  IMS forecast is worth testing, but it cannot be backtested: IMS does not
  archive past forecasts. Log it forward instead (one number a day, the
  forecast high for Ben Gurion, into `data/extra_forecasts.csv`) and after a
  month compare its MAE to the models' on the same days. Same for the HKO in
  Hong Kong or the KMA in Seoul. Expect them to be close to the blend, not
  better; the real local edge is elsewhere: knowing that LLBG sits on the coastal
  plain where sea breeze caps afternoon highs, or that KLGA reads warmer than
  Central Park, is what the bias term learns from data anyway.

**3. Is the gap still there?**

Measured from full trade histories (data-api keeps them; CLOB price history is
purged after a week), 251 randomly sampled event-days across 39 cities, every
bracket of each event priced at a fixed local hour of the target day. "Band" is
the 10-60c contracts; bias is hit rate minus price (negative = overpriced) with
its standard error; net is the P&L per share of selling every band contract
after the 5% fee. `python -m pm_scanner weather --mode trend --cities all
--days 300 --events-per-month 60` reruns this with more data.

| quarter | brackets | band n | bias at 00:00 local | net/share | bias at 08:00 local | net/share |
| --- | --- | --- | --- | --- | --- | --- |
| Q4 2025 (Dec, 7-9 brackets per event) | 82 | 31 | +10.7 +- 8.7 pts | -11.7c | +0.3 +- 8.7 | -1.4c |
| Q1 2026 | 375 | 178 | +0.6 +- 3.3 | -1.5c | -2.8 +- 3.4 | +1.9c |
| Q2 2026 | 369 | 198 | -0.4 +- 3.1 | -0.5c | +0.5 +- 3.3 | -1.5c |
| Q3 2026 (to 29 Sep) | 373 | 202 | +0.0 +- 3.1 | -1.0c | -0.1 +- 3.3 | -0.8c |
| **all of 2026** | 1,117 | 578 | **+0.1 +- 1.8** | **-1.0c** | **-0.8 +- 1.9** | **-0.1c** |

By hour of the target day, pooled: the band bias is within +-1 point from
midnight to noon; Brier skill of the price rises from 0.22 at midnight to 0.30
at noon and 0.56 at 15:00 as hourly observations arrive; 3-20c contracts are
priced fairly too (-0.3 pts). Month by month the bias wanders between -4 and
+5 points with standard errors of 5-9, i.e. noise. No trend, up or down, is
visible in 2026.

What this means:

* There is no premium to harvest by selling brackets blind. The 5-7 point gap I
  reported yesterday was a measurement artifact plus a twelve-day window; the
  late-September CLOB sample (400 band contracts, -5 pts) and the September
  trade sample (62, +0 pts) are not even inconsistent given their errors. The
  overround you see live (asks summing to 1.10-1.15 two days out) is a spread,
  not a bias: bids sum to 0.92-0.98, and the mid is fair.
* So the whole case rests on the forecast beating the crowd's implicit
  forecast, and the backtest is the decision. A day-ahead ECMWF/ICON/GFS blend
  with station bias correction has a MAE of roughly 1-1.5°C on airport maxima
  in the literature; if the market's implicit distribution is wider or
  mis-centred by even half a degree, that shows up as a Brier gap and a
  positive paper P&L in the report. If the report shows the market's Brier
  at or below the forecast's, stop: the bots already run the same models.
* The intraday leg is still worth testing separately: skill jumps between noon
  and 15:00 local, which is when the running maximum settles the outcome.
  `--cutoff-hour 12` and `--cutoff-hour 14` in trend mode show how much of
  that the price already reflects; a METAR reader that acts within minutes of
  each hourly reading is a speed edge, not a forecast edge, and it competes
  with bots.
* Keep the money where it was: nothing funded until a real backtest on your
  machine shows forecast Brier below market Brier by more than the fee, and a
  week of `--mode today` paper quotes fills at the quoted prices.

## 12. Backtest #1 (30 Sep, your laptop): the forecast does not beat the market

You ran `weather --mode backtest --cities nyc,london,tel-aviv --days 45`
(ECMWF, GFS, ICON; price at local midnight; forecast issued the day before).
The numbers, then what they mean, then what I changed.

| what | result |
| --- | --- |
| station-days with a resolved bracket | 127 (NYC 43, London 42, Tel Aviv 42) |
| forecast error of the daily high, pooled (mixed °F/°C, so only the ranking counts) | ECMWF 1.37, GFS 1.14, ICON 1.01, blend 0.88 |
| Brier over 1,133 brackets | market 0.0506, forecast 0.0565 |
| paper P&L, 269 trades at the 5-point threshold | +2.87 per share-unit, i.e. about +1c a share |
| by month | Aug −4.30 (36 trades), Sep +7.17 (233) |
| sell-every-10-60c-bracket baseline | −0.60 |

**Verdict under the rule set in section 11: fail.** The market's Brier is
lower than the blend's by about 12%. The crowd's implicit forecast at
midnight is better than an equal-weight ECMWF/GFS/ICON mean with a rolling
station bias. The positive paper P&L does not rescue it: 269 trades with a
per-trade spread of roughly 0.35 give a standard error near ±6, so +2.87 is
zero, and the August/September split is the same noise.

**What the Tel Aviv rows show, and the bug behind them.** In the last eight
days the market put 0.62-0.75 on the centre bracket and it won six times out
of eight; the model put 0.37-0.51 on it and sold it every day, losing the
fee and the spread each time. Two reasons, both ours:

* The error sd was pinned at 0.7°C, which is the floor of 0.4 × the 1.8°C
  prior, not a fitted value. A late-September Ben Gurion high is predictable
  to a few tenths of a degree, and the fit also double-counted rounding
  noise: the residuals are (settled integer − continuous forecast) and
  already contain the ±0.5 rounding, yet another width²/12 was added. The
  model was too wide, so it always under-priced the favourite.
* The bias was −0.5°C from the earlier part of the window, while the raw
  blend residuals of the last eight days average +0.05. The station offset
  drifts with the season, and an expanding window keeps stale values.

Both are fixed in `weather.py`: the rounding variance is removed rather
than added, the floor is 0.3 degrees, and `--calib-window N` fits the bias
and sd on the last N days only. The report now shows every number per
station in its own unit (the pooled MAE mixed °F and °C), the Brier gap and
the P&L with standard errors, and a reliability table (when the model or
the market says 30%, how often does the bracket win?) that makes an
over-wide or over-narrow distribution visible without reading the days.

**Run #2, one line, and the decision rule.**

```
python -m pm_scanner weather --mode backtest --cities nyc,london,tel-aviv --days 45 --calib-window 21 \
    --models ecmwf_ifs025,ecmwf_aifs025_single,gfs_graphcast025,icon_seamless,gfs_seamless
```

The forecast leg goes ahead only if the `all` row shows the Brier gap
(forecast − market) negative by more than twice its standard error **and**
the paper P&L positive by more than twice its standard error. Anything
short of that, in either column, and the day-ahead forecast leg is closed:
the bots already price these markets at least as well as the free models.
My expectation is that run #2 narrows the gap but does not flip it.

**What is left if it fails.** Not a forecast edge but the other two:
posting resting quotes at fair value and collecting the 10-15% overround
from takers (a liquidity edge, only measurable by a week of paper quotes
from `--mode today` and checking whether they would have filled), and the
intraday leg that reprices from each hourly observation between noon and
15:00 (a speed edge, competing with bots). Both are cheap to test and
neither needs money in the account. If neither works, weather is closed
too and the next candidate family from section 10 comes up.

## 13. Backtest #2 (30 Sep): the day-ahead forecast leg is closed; the intraday test is next

Same 45 days and stations, calibration on the last 21 days, five models
requested. GraphCast returned no rows (it is not in the previous-runs
archive, only in the live forecast API), so the blend was ECMWF IFS, ECMWF
AIFS, ICON and GFS.

| station | best single model (MAE) | blend MAE / bias | market Brier | forecast Brier | gap ± se | trades | P&L ± se |
| --- | --- | --- | --- | --- | --- | --- | --- |
| London (°C) | ICON 0.76 | 0.82 / +0.59 | 0.0569 | 0.0554 | −0.0015 ± 0.0025 | 74 | +6.7 ± 3.4 |
| NYC (°F) | ICON 1.79 | 1.35 / +0.60 | 0.0574 | 0.0643 | +0.0069 ± 0.0031 | 87 | −4.2 ± 3.8 |
| Tel Aviv (°C) | ICON 0.47 | 0.64 / −0.55 | 0.0374 | 0.0489 | +0.0115 ± 0.0049 | 75 | +0.8 ± 3.3 |
| all | | | 0.0506 | 0.0563 | **+0.0057 ± 0.0021** | 236 | +3.2 ± 6.1 |

**Verdict: fail, at 2.7 standard errors, and the rule closes the day-ahead
forecast leg.** The market is better than the blend everywhere except
London, where they tie. Tel Aviv is the clearest case: the market's Brier
of 0.037 is the sharpest of the three, and its reliability column is
straight (contracts it prices at 0.74 win 76% of the time), while the model
is over-confident at the top (0.76 → 57%). The bots pricing Ben Gurion know
that station better than four free global models with a 21-day bias fit.

What the run also showed, for the record:

* **Both ECMWF variants are badly biased at these grid points** (Tel Aviv
  −1.1°C, NYC +0.8 / +2.1°F, London +0.8 / +1.2°C) and the AI model AIFS is
  the worst single model at every station. ICON is the best everywhere and
  GFS second; at Tel Aviv and London ICON alone beats the equal-weight
  blend even before bias correction. An MAE-weighted or ICON-only blend is
  the obvious next tweak and I have not run it, on purpose: the rule was
  set before run #1 and tuning the blend on the same 45 days until it
  passes is how backtests lie. If we ever revisit day-ahead, it is with
  ICON + GFS on a fresh window.
* One caveat that cuts the other way: the Open-Meteo archive stores one
  run per day, so the forecast scored here may be older than the run the
  market had at midnight. The intraday test below does not have this
  problem, because the new information there is the station's own reading.

**The intraday test (`--mode intraday`), pushed with this section.** From
noon on, the outcome is increasingly decided by readings anyone can see:
the running maximum kills every bracket below it, and the remaining
question is how much higher the afternoon goes. The mode reads routine
hourly METARs from the Iowa Environmental Mesonet archive (free, global,
includes LLBG), and at 11:00, 13:00 and 15:00 local combines the running
maximum with the day-ahead blend through a per-hour regression, prices
each bracket with the dead-below / rolled-up-at rule, and scores it
against the price 5 minutes after the hour. It also marks each paper trade
to market 30 minutes later: if the price moves toward the model after the
reading, the reading was not yet priced when we acted, which is the speed
edge; if the price 5 minutes after the hour already reflects it, the bots
are faster and the leg is dead too.

```
python -m pm_scanner weather --mode intraday --cities nyc,london,tel-aviv --days 45 --calib-window 21 \
    --models icon_seamless,gfs_seamless,ecmwf_ifs025 --hours 11,13,15 --latency-min 5
```

Decision rule, again before seeing the result: the leg goes forward only
if, at some hour, the `all` row shows the model's Brier below the market's
by more than twice its se **and** mtm30 positive by more than twice its
se. Then the question becomes latency (can a laptop in Israel react within
the window the mtm30 measures?), and the answer to that is a second run
with `--latency-min 2` and `--latency-min 15`: the edge should shrink with
latency, and the latency we can actually deliver decides.

**If this fails too**, the last weather leg is pure market making (resting
quotes at the mid, collecting the 10-15% overround), which needs a week of
paper quotes from `--mode today` and cannot be backtested from trade
history alone. After that, weather is closed and the next family from
section 10 comes up.

## 14. Intraday and maker tests (30 Sep): the market is sharper at every hour, and the takers are informed. Weather is closed

**Intraday result.** Same 45 days and stations, routine hourly METARs from
the Iowa archive (about 24 readings a day per station, first request
worked), running maximum plus a per-hour regression on the ICON/GFS/ECMWF
blend, scored against the price five minutes after the hour.

| hour (local) | scored | market Brier | model Brier | gap ± se | paper P&L ± se | mtm30 ± se |
| --- | --- | --- | --- | --- | --- | --- |
| 11:00 | 1,122 | 0.0472 | 0.0570 | +0.0098 ± 0.0029 | +6.0 ± 6.4 | +1.6 ± 1.2 |
| 13:00 | 1,122 | 0.0377 | 0.0483 | +0.0106 ± 0.0024 | −2.0 ± 5.8 | +1.4 ± 2.1 |
| 15:00 | 1,133 | 0.0161 | 0.0300 | +0.0139 ± 0.0025 | −8.6 ± 3.9 | +2.2 ± 2.4 |

**Verdict: fail, at 3 to 6 standard errors, in every city and at every
hour.** The market at 15:05 on Ben Gurion has a Brier of 0.0002: it is
already resolved, because the afternoon reading has told everyone the
maximum is in. Our model, which does not yet know that a falling reading
ends the day, still sells the favourite at 0.98 and loses the fee 27 times.
London and NYC are less extreme but the same shape. The 30-minute
mark-to-market is positive in seven of nine city-hours, which is the
speed edge in the direction expected, but never above 1.3 standard
errors, and the price at expiry says the model was wrong more often than
the market whenever they disagreed. Note also the `mkt+30` column: the
market's own Brier keeps falling for 30 minutes after each reading, so
whoever is quoting these markets updates within the half hour, not within
the day. A laptop in Israel reacting five minutes after the hour is not
ahead of them.

So: no forecast edge day-ahead, no forecast edge intraday, no speed edge
worth a standard error. Three pre-registered tests, three fails. Weather
is efficiently priced by bots that read the same free models and the same
METARs, faster and with better afternoon models than a first attempt.

**The maker leg, measured without a week of paper quotes.** The last
argument for weather was liquidity provision: quote both sides at the
mid, earn the overround from takers, pay no fee. Whether that earns money
depends on who the takers are. If they are informed (a bot hitting a
stale quote after a reading), the maker loses more than the spread; if
they are noise (someone buying a bracket at 23:00 because the app said
sunny), the maker keeps the spread. This is measurable from the same
trade histories: every trade seen from the taker's side, marked to expiry
and to 30 minutes later, by station, local time of day and price band.
The taker's average markout is minus the makers' average edge, before any
liquidity rebate. `--mode flow` does this and needs only Polymarket, so it
ran from here.

Results, same 45 days and three stations, 127 event-days, 283,820 trades,
$1.93M notional (about $15K per station-day). Markouts are per share from
the taker's side, so a positive number means the takers won and the makers
who filled them lost. `side` in the data-api trade feed is read as the
taker's side; the 30-minute markout being positive and significant in
every bucket (the recorded side moves the price its own way, which is what
an aggressor does) supports that reading.

| slice | trades | $ traded | taker → expiry ± se | makers, per $100 filled |
| --- | --- | --- | --- | --- |
| all | 283,820 | 1,928,772 | **+0.004 ± 0.001** | **−1.90** |
| day before, all | 113,087 | 555,084 | +0.011 ± 0.003 | −5.23 |
| day before, Tel Aviv | 24,345 | 139,587 | +0.030 ± 0.006 | −11.61 |
| 00-06 local | 35,781 | 184,679 | +0.006 ± 0.006 | −3.25 |
| 06-09 local | 19,085 | 87,429 | −0.008 ± 0.005 | +4.96 |
| 09-12 local | 29,318 | 124,158 | −0.000 ± 0.004 | +0.18 |
| 12-16 local | 59,501 | 410,032 | +0.005 ± 0.002 | −2.43 |
| 16-24 local | 26,686 | 553,764 | −0.003 ± 0.002 | +0.68 |
| price 0.00-0.10 | 125,626 | 87,589 | −0.001 ± 0.001 | +8.21 |
| price 0.10-0.30 | 56,782 | 248,006 | +0.007 ± 0.005 | −3.56 |
| price 0.30-0.70 | 75,846 | 730,417 | +0.020 ± 0.005 | −4.44 |
| price 0.70-0.90 | 10,974 | 204,161 | +0.009 ± 0.008 | −1.15 |
| price 0.90-1.00 | 14,592 | 658,600 | +0.000 ± 0.002 | −0.03 |

**Verdict: the maker leg fails as well.** The takers in these markets are
informed. Whoever crosses the spread gains 0.4c a share by expiry on
average, and 2c a share in the 30-70c band where a maker's spread income
would come from, so the makers as a group lost $36.7K on $1.93M, about 2%
of everything they filled, before the liquidity rebate (25% of the $35.6K
of taker fees, so about $9K back, which does not close the gap). The day
before the target day is the worst time to be quoting, and Tel Aviv is
the worst station: the evening quotes there are picked off for 3c a share,
which is a bot with a better forecast hitting stale orders. The two slices
where makers earned, 06:00-09:00 local (+5 per $100 on $87K) and the
1-9c longshot band (+8 per $100 on $88K, buyers of 3c contracts that
die), are small, at one standard error, and are the first places another
maker would already sit.

The whole picture, in one line: over 45 days and three stations, takers
gained $37K, paid $36K in fees, and makers lost $37K. The platform earned
the money; the informed bots roughly broke even against the fee; everyone
else paid.

**Weather is closed.** Four pre-registered tests: the day-ahead forecast
(sections 12-13), the intraday forecast, the speed leg and the maker leg,
each failed on its own rule, and the last one shows why the others did:
the counterparties are informed bots, not retail. Nothing in this family
rewards a laptop with free models and a $50-500 stake. The code stays
because the trend, backtest, intraday and flow modes generalise to any
recurring family and are the tools for the next candidate.

**What the flow test suggests as the next move.** The question that
matters for a small trader is not "can we forecast better" (no, not
against bots) but "where are the takers noise, so that a quoted spread is
paid rather than picked off?" Weather says: not here. The same measurement
runs on any family with resolved markets, because it needs only the trade
tape and the outcome, and both are on Polymarket's own API, reachable
from here. A site-wide version of `flow` over the families of section 10
(taker markout to expiry by family, hours-to-close and price band, with
the notional behind it) would rank the site by maker edge instead of by
forecastability, which is the ranking we should have asked for first.
Sports, pop culture and the mention markets are where retail flow is
likeliest; whether it survives the makers already there is what the
numbers would show.


## 15. Site-wide order flow (30 Sep): nobody is collecting the spread

The `flow` command, run from here over the last 45 days: 47,011 events across
19 tags, the 60 biggest recurring families with at least ten resolved
markets, 30 resolved markets sampled per family, 1,784 markets and about
$21M of trades scored from the taker's side. The full report is in
`data/flow_families_2026-09-30.txt`. Two corrections were needed on the way
and both are in the code: sports markets name their sides after the teams
or Over/Under, not Yes/No, and the first pass read every such trade as a
first-outcome trade (which produced makers "earning" 60-80% of notional;
nonsense); and the standard error has to treat each market as one
observation, because every trade in a market shares its outcome.

| group | families | with makers > 0 | markets | $ sampled | family $/day, summed | makers, per $100 filled ± se |
| --- | --- | --- | --- | --- | --- | --- |
| sports and esports | 38 | 18 | 1,140 | 14.1M | 46.5M | **−1.1 ± 4.4** |
| crypto (hit-price, strikes) | 8 | 5 | 224 | 6.7M | 2.6M | +1.8 ± 2.1 |
| weather cities | 12 | 6 | 360 | 0.46M | 0.93M | +1.8 ± 1.5 |
| other (Elon tweets, token FDV) | 2 | 0 | 60 | 0.70M | 0.84M | −5.6 ± 1.5 |

**The finding is a null, and it is the important one.** In every group the
makers' edge is within two standard errors of zero, and the share of
families where makers come out ahead is a coin flip. Takers as a group are
not the dumb retail the market-making story needs: to expiry they break
even before the fee, everywhere, so the makers keep nothing and the
platform keeps the fee. Sports in-play (the last hour, $2.8M) and the
1-6h bucket ($9.6M) run slightly against makers; pre-game 6-24h runs
slightly for them (+2.2 per $100 on $1.4M), and sports longshot buyers
below 30c actually win (makers −10 to −28 per $100 there). Weather makers
earn a little on quotes placed more than a day out (+6 on $122K) and lose
a little in the last six hours, which is section 14 again in miniature.

**The tempting rows are artifacts.** Counter-Strike (+34 ± 10) and the
Premier League (+28 ± 11) each have 70% of their sampled dollars in one
match where the favourite lost; re-sampling Counter-Strike with a different
set of 30 markets moved it from +16 to +34, which is what a one-market
estimate does. Five of sixty families clear two standard errors, which is
about what sixty draws from zero produce (expect one or two, more with the
concentration). Miami daily weather (+14.6 ± 4.4, 22% concentration,
$73K a day) and the Japanese J-League (+13.6 ± 4.5, 49%) are the only two
that are not obviously one match, and they are exactly the shape of a
multiple-comparisons winner.

**Where this leaves the project.** Five pre-registered tests since the
niche survey: day-ahead forecast, intraday forecast, speed, the weather
maker leg, and now the site-wide maker screen. Every one came back at
zero or negative for us, and the last one says the spread-collection
business is not available to anyone at the family level, not just to us.
What is left on Polymarket at a $50-500 stake is (a) operational edges we
have not tested and that a laptop in Israel is badly placed for (in-play
sports with a faster video feed than the quote updaters; sub-minute
crypto reaction), and (b) information edges, which we ruled out on
compliance grounds at the start. I do not see a research path from here
that I would fund with your money.

Two cheap things remain honest to do, neither of which I would call a
plan: re-sample Miami and the J-League out of sample (60 fresh markets
each, one command, ten minutes) to see whether the two survivors are real
or the expected flukes; and, if they are real, a one-week maker paper test
at the mid in that one family. If they are flukes, the recommendation is
to stop spending credits on Polymarket research and put the memo away
until the fee schedule or the market structure changes.

## 16. Closing the loop (30 Sep): the two survivors re-sampled, and the maker paper test

**Out of sample.** Sixty fresh resolved markets per family over 60 days,
every market from the first pass excluded (`flow --families ... --exclude-json`;
report in `data/flow_oos_miami_jleague_2026-09-30.txt`).

| family | first pass (30 mkts) | out of sample (60 fresh) | combined (90) | all 200 resolved markets |
| --- | --- | --- | --- | --- |
| Miami daily weather | +14.6 ± 4.4 | **−1.5 ± 1.6** | +1.1 ± 1.9 | −0.9 ± 1.3 |
| Japanese J-League | +13.6 ± 4.5 | +24.3 ± 14.1 | +17.1 ± 4.9 | **−0.7 ± 7.2** |

(makers' edge per $100 filled, ± treats each market as one observation)

Miami was the expected fluke: gone at the first fresh sample. The J-League
looked like it might survive (combined +17 ± 5 over 90 markets, still
+20 ± 9 without its two biggest), then the full set of 200 resolved
markets put it at zero, with the 110 markets outside the two samples
running about −12 per $100. The lesson is about the estimator, not the
league: taker markouts are so heavy-tailed market to market that ninety
markets and a clustered error still let a +3.5-sigma reading come from
nothing. Nothing in the screen survives a bigger sample.

**The maker paper test on the tape** (`flow --maker`; report in
`data/flow_maker_paper_2026-09-30.txt`), the honest form of "a week of
paper quotes": a two-sided quote re-centred on every print at ±2, 3 or 5
cents, 20 shares a side, position cap 100 shares, filled by prints
through the quote, held to expiry, over all 200 resolved markets of each
family. Fills through the quote are a lower bound on fills and select the
adverse ones; fills at the quote (`--fill at`, assuming we were first in
the queue) are the upper bound. Both are below.

| family | quotes | fills through the quote: P&L per $100 ± se | filled $/day | fills at the quote: P&L per $100 ± se | filled $/day |
| --- | --- | --- | --- | --- | --- |
| J-League | ±2c, any hour | −19.8 ± 4.1 | 49 | −16.2 ± 4.4 | 56 |
| J-League | ±2c, ≥1h to close | −14.7 ± 5.5 | 19 | −13.5 ± 5.5 | 23 |
| Premier League | ±2c, any hour | −13.9 ± 2.9 | 100 | −11.7 ± 2.5 | 120 |
| Premier League | ±2c, ≥1h to close | −8.8 ± 2.8 | 56 | −5.5 ± 2.5 | 71 |
| Miami weather | ±2c, any hour | −3.9 ± 0.6 | 526 | −3.6 ± 0.6 | 623 |
| Miami weather | ±2c, ≥1h to close | −3.9 ± 0.6 | 525 | −3.6 ± 0.6 | 623 |

Through the quote the maker loses in every family at every spread: the
prints that reach a resting quote are the informed ones, and widening the
quote makes it worse per dollar (−20 at 2c, −24 at 5c in the J-League)
because only the jumps still reach it. Quoting only well before the close
removes the fills along with the losses (a few dollars a day). The optimistic bound is no better: even
assuming every print at our price reached us first, the maker loses 16 per
$100 in the J-League, 6-12 in the Premier League and 3.6 in Miami, on a
few dozen to a few hundred dollars of fills a day (report in
`data/flow_maker_paper_at_2026-09-30.txt`). The quote gets picked off on
the moves and earns nothing on the rest, and no spread, hour filter or
family in this set changes the sign.

**Weather, sports and the maker business are closed, and so is this
research.** The tally since the niche survey: seven pre-registered tests
(day-ahead forecast, intraday forecast, speed, weather maker flow,
site-wide maker flow, out-of-sample re-check, maker paper test), every one
at zero or negative for us, with the two flattering readings on the way
each explained by a bug or by sampling. The tools stay in the repo and
run in one line; the memo stays as the record of what was tried and why
it did not work. If anything changes the picture it will be structural:
a fee change that leaves makers a rebate worth having, a new family with
retail flow before the bots arrive, or a decision to compete on
operations (latency, uptime, capital) rather than on research. None of
those is a $50 experiment, and I would not recommend funding an account
on the strength of anything in sections 10-16.

## 17. Two last checks (30 Sep): ladders and liquidity rewards; what was left unchecked, on purpose; and how to test your own data

After section 16 the question was whether "too efficient for us" was the
right claim. It is not quite: what the tests support is narrower, that with
public data, a small stake and no infrastructure, every edge we could test
was zero or negative. Two of the untested avenues were cheap enough to
close, and you asked for them; the rest are listed below as consciously
not pursued.

### 17a. Ladder consistency (`ladder`)

Nested outcomes must be priced in order: "above $76k" cannot be worth more
than "above $74k", "launched by June" cannot be worth more than "launched by
December", "ceasefire through November" cannot be worth more than "through
October". When the bid on the harder rung exceeds the ask on the easier one,
buying YES on the easier rung and NO on the harder one pays at least $1 per
set for less than $1, whatever happens. The scanner finds ladders inside an
event (numbers, dates, O/U lines, handicaps) and across events ("... by
<date>?" questions with the same stem), reads Gamma's top of book, confirms
each candidate on the CLOB books and nets both taker fees.

Run on the 6,000 busiest open events, six snapshots over 45 minutes
(`data/ladder_snapshots_2026-09-30.txt`):

* about 7,000 ladders (5,400 O/U lines, 600 numeric, 600 date, 400
  handicap), 32,000 rungs, 6,500 adjacent pairs live on both sides per
  snapshot;
* the adjacent gap (bid on the harder rung minus ask on the easier one) has
  a median of −15c and a 90th percentile of −4c; about 340 pairs sit within
  2c of an inversion at any moment;
* 62-70 pairs per snapshot are inverted on Gamma's quotes, 27-36 of
  them executable on the live books after both taker fees, all in the
  same handful of illiquid ladders: the tails of "What will the Fed rate hit
  before 2027", "How many Senators vote for the Clarity Act", token-launch
  FDV ladders, the GTA VI Metacritic ladder, one Anthropic token-price
  ladder;
* the best single pair nets 2.2c per $1 set on about 100 sets (the top of
  the book), $2.23 at a $100 budget, capital locked 92 days: 9% a year. The
  rest net 0.1-1.6c per set on 5-100 sets. Taking every executable pair at
  a $100 budget each earns $6-14 per snapshot, the median pair
  locks its capital for three months, the median annualised return is
  about 1%;
* 40 distinct pairs appeared in the 45 minutes and 26 of them were there
  in every snapshot, unchanged in price and size: nobody is arbitraging
  them because they are not worth the gas and the three months of locked
  capital.

So ladders are checked, and the answer is the expected one. The structural
gap exists only in pennies, in rungs nobody trades, on money that has to
sit until the ladder resolves; the scanner will flag anything bigger in a
one-liner, and in an hour of snapshots nothing bigger appeared.

### 17b. Liquidity rewards (`rewards`)

**The program.** Polymarket pays makers two ways. Maker rebates return
15-25% of the taker fee on each fill against a resting order
(docs.polymarket.com/programs/maker-rebates): at a 50c price that is about
0.3c a share, against the 4-20c a share of adverse selection measured in
section 16, so rebates change nothing. Liquidity rewards are the other way:
18,600 markets carry a daily pot (`clob.polymarket.com/rewards/markets/
current`), $238,000 a day in total, 182 markets at $100 a day or more, 3,441
at $10-100, the rest at $1-10. Every minute the book is sampled; each
resting order within the market's max spread of the midpoint (usually 4.5c)
and at or above its minimum size (usually 20 shares) scores
((v − s)/v)² × size, a maker's two sides are combined (min of the two,
or the bigger one divided by 3 for one-sided quotes when the mid is
between 10c and 90c), and the pot is split in proportion. So what a
quote earns depends only on the pot and on who else is quoting inside the
max spread, both of which are on the public book.

**The survey** (`rewards`, `data/rewards_survey_2026-09-30.txt`): 264
markets, the 60 biggest pots plus 150 random ones at $10-100 a day and 60
at $1-10, each with its live book and its last 14 days of tape. For a
two-sided quote at the minimum size, the book gives the share of the pot
(a range, from "every competitor is one-sided" to "every competitor is
balanced"), the tape gives fills a day and the loss per fill from the
paper maker of section 16, marked to the last print.

| pot | markets | quote | median share | median reward $/day | median net $/day | markets net > $0.5/day | sum net $/day (low..high) | capital |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ≥ $100 | 55 | at the touch | 0.2% | 1.45 | −0.33 | 25 | −101 .. +220 | $5,010 |
| ≥ $100 | 60 | half max spread | 0.1% | 0.51 | +0.21 | 28 | +1,319 .. +1,525 | $5,110 |
| $10-100 | 95 | at the touch | 5.5% | 1.73 | +0.88 | 51 | +194 .. +410 | $2,665 |
| $10-100 | 147 | half max spread | 23% | 7.79 | +2.31 | 99 | +1,868 .. +2,024 | $3,865 |
| $1-10 | 57 | half max spread | 12.5% | 0.23 | +0.06 | 18 | −1 .. +4 | $1,330 |

The big pots are what the postmortem in the sources described: deep books
(the Fed markets have $6,000-36,000 of score resting within 2.5c), a
20-share quote takes 0.1-2% of the pot, and at the touch the adverse
selection ($578 a day across the 55) exceeds the reward. The $1-10 tier is
noise. The $10-100 tier is different: half the sampled markets have no
qualifying order within the max spread at all, 38 of 147 had not traded in
14 days, and a minimum-size quote sitting at half the max spread would
take a median 23% of a median $35 pot, with nothing on the tape to pick it
off. Summed over the 147, that is +$1,900 a day on $3,900 of capital; the
sums are dominated by a few markets, so the medians are the honest
numbers: $2.3 a day per market on $26 of capital, 99 of 147 positive.

**Every rewarded market's book** (`rewards --books-only`; the survey above
sampled, this reads all 3,554 two-sided books at $10 a day or more, $183,000
a day of pots): 1,076 markets, carrying $42,800 a day, have no qualifying
order within the max spread on either side. In 1,396 markets a minimum-size
quote at half the max spread would take at least a quarter of the pot:
$49,000 a day on $42,000 of capital, $36,500 of it in markets seven or more
days from resolution. Restricted to markets 7+ days out, spread under 50c,
minimum size at most 50 shares, there are 729 of them worth $31,000 a day on
$15,600 of capital; the top of that list is a $200-a-day pot on "Cornell
President out by October 31?" with a 7c/40c book, then Israel's next
finance minister, Trump's AI czar, Colombia's central bank, October
precipitation in New York, and hundreds of NFL season props at $50 a day
each. A second pass 23 minutes later (the saved report,
`data/rewards_pocket_2026-09-30.txt`, is this one) found 1,105 empty books
carrying $43,200, 1,494 markets at a quarter or more of the pot ($51,800 a
day on $44,400), 1,200 of them the same markets as the first pass; and the
pots themselves move: 23% of the rates changed within the hour, typically
by 10-40% either way (Jay Clayton's went from $70 to $149, Nir Barkat's
from $77 to $40), with the total unchanged, so the pot you see is re-set
by Polymarket at least hourly and a quote should be sized to the rate's
average, not its reading.

**Why this cannot be taken at face value.** Two hundred dollars a day for
twenty dollars of capital, in a thousand markets, on a public formula,
does not survive in a market with bots in it. Either an unwritten condition
stops the pot being paid when nobody else is quoting (the help centre and
the docs page state none: orders within the max spread, at or above the
minimum size, both sides when the price is under 10c or over 90c, a $1
minimum daily payout), or the pots are unclaimed because the risk that
does not show on a 14-day tape is real: the only counterparties in a dead
market are the ones who know something, a fill leaves you holding to
resolution, and a bot that quotes a thousand of these needs capital,
re-centring and a way to leave before the resolution day. Or Polymarket
counts on most of the nominal $238,000 never being claimed. This is the
one place in the whole research where the arithmetic came out positive
and the reason is not obviously a bug or a sampling accident, and I could
not settle it from documents.

**The test that settles it** is the $50 test this memo has been holding
back since section 3, and it is cheap because it needs no edge, only the
program to pay what it advertises:

1. Fund the dedicated wallet with $60-100 of USDC on Polygon (section 3).
2. Pick 3-4 markets from the `--books-only` list that are 7+ days from
   resolution, with a spread under 50c and a $50+ pot, and no news flow
   (the Cornell, finance-minister and AI-czar markets are the type; not
   weather, not anything resolving this week).
3. Rest 20 shares on each side at half the max spread from the mid (about
   2.25c each side when the max spread is 4.5c), re-centre when the mid moves, keep the orders
   up for three days. Capital at risk per market is the 20 shares on each
   side, about $20-30; the worst case is being filled on both sides by
   someone who knows the answer, so at most the capital.
4. Read the rewards page during day one (it shows the day's accrual) and
   the portfolio history after the midnight UTC payout.

On paper that is $50-200 a day per market, several hundred dollars a day
on $100 of capital, which is exactly why the expected result is that the
rewards page shows nothing and the unwritten rule is found. If instead the
payout arrives, the next step is not to scale by ten but to run a week
across twenty markets with a script that re-centres and pulls quotes 48
hours before resolution, and to watch the share fall as others notice. If
the payout is zero, this topic closes with the rest.

What it is not: it is not the maker business of section 16 (those quotes
were in markets that trade; these are in markets that do not), and it is
not an edge over anyone; it is a subsidy that appears to be lying on the
floor, and the test is whether it is really there.

### 17c. Not pursued, by decision

You decided to leave these as they are; they are recorded so nobody
re-derives them later:

* **Long-dated longshot selling.** Selling 2-5c contracts that expire months
  out is a capital charge, not an edge; at this stake it is a few dollars a
  quarter with tail risk. Not tested.
* **Information processing on public feeds** (mention markets, tweet counts,
  scheduled data releases). Section 15 showed their takers are informed, so
  people with models already trade them; building one is months of work,
  not a $50 experiment. Not tested.
* **In-play sports and sub-minute crypto.** Latency games; structurally
  against a laptop in Israel. Not tested.
* **Resolution-rule misreadings.** A research edge that is manual and rare;
  the weather rules were read carefully by the crowd. No scan built.
* **Other venues.** Kalshi is closed to you; smaller venues were out of scope.

### 17d. Can the framework test your own data? Yes: `signal`

The Polymarket side of a backtest is already solved in this repo: the
data-api trade tape gives the price of any market at any moment of its life
(kept for months after resolution), Gamma gives the resolution, and the
weather and flow work built the scoring (Brier against the market, markouts,
paper trades net of fees, market-clustered errors). What was missing was a
way in for data that is not a weather forecast. `signal` is that: a CSV of
`market,time,p` (plus optional `outcome` and `note`) scored row by row
against the price at that time. Offline example:

```
python -m pm_scanner signal --csv data/signal_example.csv --fixtures tests/fixtures
python -m pm_scanner signal --csv my_signal.csv --json out.json          # live: any resolved market
```

It reports the Brier of your numbers and of the market at the same moments,
their gap with a standard error that treats each market as one observation,
the Brier of a 50/50 blend (if the blend beats the market, your data carries
information the price lacked, even when your numbers alone lose), the price
move in your direction 30 minutes later, a paper P&L for the rows that were
more than 5c plus fee away from the price, and a reliability table. It drops
rows dated after the market's last trade (look-ahead) and rows with no trade
in the previous 24 hours (no price to compare against), and it prints the
smallest gap the file could have detected, which is the number to read
first with a small file.

**What makes a dataset backtest-ready** (the questions to answer before
collecting anything):

1. **Point-in-time.** Each row needs the moment the number was available to
   you, not the moment it describes. Revised series (economic data, box
   office estimates, poll aggregates that get restated) need their original
   vintages; a series that only exists in its revised form cannot be tested
   honestly and will look better than it is.
2. **A map to markets.** Each row must name a Polymarket market (condition
   id, slug or Gamma id) that was open at that time and has since resolved.
   Recurring families (weather, sports, crypto dailies, mention counts,
   weekly economic prints) give hundreds of resolved markets; one-off
   political markets give one.
3. **A probability, not a hunch.** The value must be P(Yes) for that market,
   produced by a rule fixed before looking at outcomes. If the data is a raw
   number (a count, a reading), write the mapping to a probability down
   first and test it on markets you have not looked at; fitting the mapping
   on the same markets you score is how every false edge in sections 12-16
   was made.
4. **Enough independent markets.** A Brier gap is the square of the price
   error you can exploit: a 10c edge is a gap of 0.01, a 5c edge 0.0025.
   Scaling the weather runs (± 0.002 on about 1,400 bracket-markets), 200
   resolved markets can show a gap of about 0.01, a 10c edge; a 5c edge
   needs a few thousand markets, and the taker fee eats the first 1-2c of
   it. Fifty markets can only show something large.
5. **Timing that beats the tape.** The price you are scored against is the
   last trade before your timestamp; the fill you would actually get is the
   ask after it. Data that arrives on a schedule (a release at 12:30) must be
   scored a realistic latency later, which is why `time` is yours to set.
6. **Public and legal.** Section 14's rules still apply: nothing non-public,
   nothing from inside the resolution source.

If a file passes these six and the report shows a blend gap two standard
errors above zero on markets you did not fit on, that is the first positive
result this research would have produced, and the next step would be the
$50 live test on those markets only.

## 18. The liquidity-reward test (30 Sep): protocol, smoke test first

You decided to run the $50 test from section 17b. This section is the
protocol, written before any money moves, so that the result is read against
what was expected rather than the other way round. The rig is
`python -m pm_scanner lp`; it is the only command in the repo that can send
an order, and it is built to do nothing else than this test.

### 18a. What the rig does and refuses to do

`lp` reads every rewarded market's pot and book (`rewards --books-only`),
keeps the long-dated ones with a spread under 50c and no news-driven family
(no weather, earthquakes, video views, post counts), ranks them by the
modelled reward for a minimum-size quote, and rests in each a **two-sided,
minimum-size, post-only** quote at half the max reward spread from the mid
(a YES bid at mid − 2.25c, and a NO bid at 1 − (mid + 2.25c), which the book
shows as a YES ask). Every minute it re-reads the book and re-centres when
the mid has moved a tick. It never sends a market order, never crosses the
spread (post-only orders are rejected rather than matched), never adds to a
side once it has been filled, pulls a market's quotes 48 hours before its
end date, refuses a budget above `MAX_BUDGET_USD`, and cancels everything on
exit or Ctrl-C. Every order, fill, scoring read and earnings read is
appended to `lp.jsonl`.

Two read-backs make the test fast. The CLOB answers, per resting order,
whether it is **currently scoring** for rewards (`get_orders_scoring`), and
it reports the account's **earnings so far today** per market
(`list_user_earnings_for_day`); the rewards page on polymarket.com shows the
same figure. So whether the program counts our quote is known within
minutes, and whether it pays is known after the first midnight UTC.

Two facts from the API worth knowing before reading results: Polymarket
publishes a `market_competitiveness` number per rewarded market (0 for the
"Jay Clayton AI czar" market, 0.22 for Cornell, 9.6 for the Fed decision)
and offers makers a `no_competition` filter on their earnings endpoint, so
unquoted pots are a known, intended feature of the program rather than an
oversight; and the pots are re-set at least hourly (section 17b), so the
rate you were quoting against may fall once you are there. The test
measures both directly: the rig logs the pot and the competitiveness of
each market at every earnings read.

### 18b. Account setup (your side, once)

1. **A fresh signer.** Create a new wallet in Rabby or MetaMask from a new
   seed, holding nothing else. Its private key is the only secret the rig
   needs and the loss if it leaks is the test budget.
2. **The Polymarket account.** Sign in at polymarket.com with that wallet;
   the account gets a Deposit Wallet whose address is in the profile menu.
   No VPN: Israel is not geoblocked (section 2).
3. **Deposit.** Use the site's deposit flow and send USDC on Polygon from
   your exchange to the address it gives; it is credited as pUSD. The smoke
   test needs about $25; the full test $60-100. Nothing else goes in.
4. **Relayer API key.** Settings → API Keys → Relayer API Keys → create;
   copy the API key and the Signer Address it shows.
5. **Environment.** On the machine that will run the rig (your laptop is
   fine; it needs to stay on for the test), set `POLY_PRIVATE_KEY`,
   `POLY_WALLET`, `POLY_RELAYER_KEY`, `POLY_RELAYER_ADDRESS` and
   `MAX_BUDGET_USD=100` as environment variables (never in a file in the
   repo), `pip install -e ".[trade]"`, then `python -m pm_scanner lp
   --check`: it prints the wallet type, the pUSD balance, whether the
   trading approvals are in place, and the plan, and sends nothing.

### 18c. The smoke test (a few dollars of risk, one afternoon)

Purpose: prove the pipe, not the thesis. One market, the minimum size, two
hours.

```
python -m pm_scanner lp --smoke                        # dry run: the one market it would quote
python -m pm_scanner lp --smoke --live --only <condition id from the dry run>   # 2 hours, then cancel
```

(Since 3 Oct a live run must name its market with `--only`: the first smoke run
quoted a different market than its dry run twenty minutes earlier, two pots
being equal, see section 20.)

(Since 4 Oct the plan carries an `exit $` column: what one full fill on the
worse side would lose if sold straight back into the book that minute, fee
included. Candidates above `--max-exit`, $2 by default, or whose book cannot
absorb the quote at all, are dropped, and a live run refuses such a plan even
with `--only`; raising `--max-exit` is the one way to accept the loss, and it
is written in the command. Every `fill` line in the log carries `sell_now` and
`loss_if_sold_now` read from the book at that second, and `lp --positions`
prints the same for whatever the account holds. The rule behind it, for the
human and the model alike: what a position is worth is what the bids pay. The
site's midpoint mark is not money, and neither is an estimate; on the morning
of 4 Oct the mark said −$0.15, an estimate said "under a dollar", and the bids
said −$2.45, then −$4.63 an hour later.)

What "the system plays as expected" means, in order:

1. `--check` shows the pUSD balance you deposited and approvals in place.
2. Two orders are accepted with status `live` (the log shows both `place`
   lines with order ids), and they appear in the market's book on the site
   at the planned prices.
3. Within the first ten minutes the `scoring` line reports 2 of 2 orders
   scoring. **This is the smoke test's verdict.** If the CLOB says the
   orders do not score while they sit inside the max spread at the minimum
   size, the unwritten rule exists and the topic closes; nothing more is
   spent.
4. The `earnings` line for today turns positive within an hour or two (the
   accrual is continuous). Expected for a $100-200 pot at a 75-100% share:
   $4-8 an hour.
5. After two hours the rig cancels both orders; `--cancel-all` is the
   manual fallback; the site shows no open orders.
6. The next day, after midnight UTC, `lp --earnings <yesterday>` and the
   portfolio history show the payout, if the day's total reached $1.

Capital parked during the smoke test: about $19 (20 shares each side at
prices summing to about 0.95). Risk: a fill on either side, at most that
$19 held to resolution, or at most the plan's `exit $` if sold straight back.
Cost if everything works: nothing.

### 18d. The $50 test (three days)

Only after 18c passes on points 1-5:

```
python -m pm_scanner lp --live --budget 50 --markets 3 --hours 72 --log lp.jsonl
python -m pm_scanner lp --earnings 2026-10-0X      # each morning
```

Pre-registered reading of the result, per day and per market:

* **Pass:** the payout arriving at midnight UTC is at least half of
  (the market's average pot that day × the share the rig logged), summed
  over the markets, and fills cost less than the payout. Then the next
  step is a week across twenty markets with the same rig (`--markets 20
  --budget 400`), watching the share and the pots as others notice, and
  not a step beyond that without a new memo section.
* **Fail:** earnings accrue on the page but no payout arrives, or the
  payout is under a quarter of the modelled figure, or the pot of every
  quoted market collapses within a day of quoting. Each of those is the
  missing rule, found; the account is emptied and the topic closes.
* **Abort early:** two fills in one market (the rig stops that market by
  itself), any fill in a market whose book was empty when quoting started
  (someone is hunting the quotes), or the pUSD balance falling below the
  parked collateral for a reason the log does not explain.

Whatever happens, the log and the earnings readings go into section 20,
with the same honesty as sections 12-16.

## 19. Niche public data (1 Oct): what exists, which markets price against it, how much room the price leaves

You asked for open data that is too niche to be obvious, where the link
to a market is not trivial, so that other players have not bothered. I
went through every recurring Polymarket series (the 690 families from
section 10, re-pulled from Gamma today) by *what it resolves on*, and
asked of each: is there a public, timestamped feed upstream of the
resolution source, and is the step from feed to probability something the
crowd would skip? Two constraints shaped the work. This container's
network policy reaches Polymarket's own APIs, PyPI and raw GitHub files
and nothing else (sixty candidate hosts tested: USGS, NOAA, CDC, BLS,
FRED, EIA, Wikipedia, Federal Register, SEC EDGAR, CourtListener,
Spotify, FlixPatrol, OpenSky, the IMF, the xtracker resolution site,
all refused), so the data pulls below are yours to run; and section 14's
rule stands, nothing non-public and nothing from inside the resolution
source's own process.

### 19a. The shape that works

The pattern that satisfies all six criteria of 17d by construction is a
**count window**: a market that resolves on an official aggregate over a
window (posts this week, quakes this week, ships this week, tornadoes
this month), published with a lag by one source, while the occurrences
themselves are public and timestamped as they happen. The count so far is
then known exactly, the remainder is a distribution with a base rate you
can read off the previous windows, and the probability of each bracket is
arithmetic. The connection is "not trivial" in exactly the sense you
meant: nobody has to know anything, they have to do a Poisson sum with the
live count, every day, for every bracket, and most people eyeball it
instead. Polymarket runs more of these than I expected:

| series | events / markets (resolved) | since | volume | resolves on | upstream feed |
| --- | --- | --- | --- | --- | --- |
| Donald Trump Truth Social posts per week | 70 / 770 (735) | Feb 2026 | $13.6M | xtracker.polymarket.com post counter | the tracker's own "Export Data" CSV (timestamps of every counted post) |
| White House, Khamenei, Zelenskyy, Ted Cruz, NYC Mayor, CZ posts per week | 59+59+58+58+58+57 / 3,949 (3,723) | Mar 2026 | $10.4M | same | same |
| Elon Musk posts per week, per 48h | 185 / 4,090 (4,023); 119 / 1,196 | 2024 | $1.44B + $195M | same | same (section 15: informed takers) |
| 6.5+ earthquakes per week; 5.5+ per week | 37 / 259 (252); 18 / 150 (141) | Dec 2025; Jun 2026 | $3.3M + $0.8M | USGS search, magnitude ≥ threshold, ET day boundaries | USGS FDSN event API (free, no key; one CSV for the whole catalog) |
| Ships through Hormuz per week; Bab el-Mandeb per week; Hormuz 7-day average at month end | 31 / 185 (173); 12 / 57 (47); 11 / 67 (53) | Mar 2026; Jul 2026 | $6.2M + $0.2M + $5.0M | IMF PortWatch daily transit calls | PortWatch's own daily CSV (published with a few days' lag, "finalised" when the next day appears); live AIS for the days PortWatch has not published |
| US tornadoes per month | 12 / 77 (67) | Dec 2025 | $1.7M | NCEI monthly count, released ~6 weeks after month end | SPC preliminary storm reports (daily CSV, same day); NCEI's final count is a stable fraction of the preliminary one |
| Monthly precipitation (Seoul, Seattle, NYC, ...) | 15 / 110 (38) plus 12 older events | Jul 2026 | $1.4M | KMA / NWS monthly total | the same agencies' daily observations (accumulation to date) plus ECMWF 15-day precipitation from Open-Meteo for the remainder |
| Claude downtime days per month | 8 / 42 (37) | Feb 2026 | $0.4M | status page history | the status page itself, daily |

Those first three rows are one family in the sense that matters: the same
rule text, the same tracker, 20-wide brackets, Friday-noon-to-Friday-noon
ET windows. Section 17c recorded your decision not to chase mention and
tweet markets as *information processing*; this is a different use of the
same markets, a base-rate model, and the smaller accounts have none of the
bot competition section 15 found around Musk.

The second shape is an **official index with a public leading series**,
where the mapping is a regression fitted on years before the test period:

| series | events / markets (resolved) | since | volume | resolves on | leading public data | mapping |
| --- | --- | --- | --- | --- | --- | --- |
| Monthly global temperature anomaly; "hottest month on record" rank | 16 / 94 (82); 14 / 35 (35) | Nov 2024 | $21.1M + $8.1M | NASA GISTEMP table, ~mid next month | Copernicus Climate Pulse ERA5 daily global temperature (public CSV, ~5-day lag); Berkeley Earth monthly quick-look | ERA5 month-to-date anomaly → GISTEMP anomaly, fitted 2015-2024; residual sd ≈ 0.03-0.05 °C against 0.05 °C brackets |
| Price of a dozen eggs (BLS average price) | 17 / 145 (135) | Jan 2025 | $7.4M | FRED APU0000708111, CPI release day | USDA AMS Egg Markets Overview (weekly wholesale, public), Daily National Shell Egg Index | wholesale of the CPI survey weeks → retail, fitted 2015-2024; the lag is weeks |
| Flu hospitalization rate (FluSurv-NET) per week | 34 / 184 (184) | Jan 2026 | $0.8M | CDC FluView, Fridays | NHSN hospital respiratory admissions (all hospitals, same Friday), state dashboards, WastewaterSCAN / NWSS influenza A (twice weekly, leads by about a week) | wastewater and NHSN of the week → FluSurv-NET rate; brackets are 0.1 wide |
| Measles cases by date | 25 / 133 (115) | May 2025 | $13.4M | CDC counter, weekly | state health department releases (Texas DSHS, Utah, South Carolina, ...), days to a week ahead of CDC aggregation | state sum plus the CDC's lag; a cumulative count, so only the "by date" tail is uncertain |
| IPO first-day closing market cap | 58 / 403 (386) | Feb 2026 | $9.6M | first-day close | the 424B4 final prospectus on EDGAR (timestamped pricing and share count); Jay Ritter's first-day return tables (public, 1980-) | priced cap × the empirical first-day return distribution → P(bracket), fitted on IPOs before 2026; the edge window is the hours between pricing and the open |
| White House calls a full lid by 6:30 PM | 30 / 180 (173) | Feb 2026 | $1.9M | pool reports | the daily guidance the White House publishes the evening before (archived by Factbase) | schedule features (evening event, travel, weekend) → P(full lid), fitted on earlier days |
| Opening weekend box office | 315 / 1,342 (1,220) | 2021 | $77.0M | the-numbers.com | Wikipedia pageviews of the film's article in the week before release (hourly API, archived since 2015, exactly point-in-time); Google Trends | the Mestyán-Yasseri-Kertész (2013) regression, refitted on 2021-2024 films, scored on 2025-26 markets; the crowd uses tracking leaks from the trades, pageviews are orthogonal to them |
| #1 Netflix show / movie (global, US) per week; Billboard #1; Spotify #1 | ~2,000; 759; 850 | 2025 | $7M; $1.3M; $2.2M | Netflix Tuesday top 10; Billboard; Spotify | Wikipedia pageviews of the titles during the chart week; Kworb's daily Spotify archive; Mediabase daily radio chart (Billboard only) | multi-outcome: P(title is #1) from relative attention; the aggregator the crowd uses (FlixPatrol) is itself a leading indicator |

Left out on purpose: TSA daily passengers (777 markets) and FlightAware
delays (136) were discontinued in April and June 2026, though TSA's own
history back to 2019 makes them the cleanest sandbox if the series
returns; the D4-drought state count ($0.1M), the GPU rental index ($0.4M)
and Claude downtime ($0.4M) are too small to pay for a model; Musk's net
worth is TSLA arithmetic that sharp traders already do; MrBeast views and
the LMArena "best AI company" markets have leading indicators (first-hour
view velocity, anonymous arena models) that nobody archived, so they can
only be collected forward; approval-rating markets are a model of Silver
Bulletin's model, not of the world.

### 19b. Two things checked from here

**A niche archive that failed the coverage test.** The one upstream feed
reachable from this container was a public GitHub archive of Truth Social
posts (29,469 posts with timestamps, scraped every four hours). It is
exactly the kind of source you had in mind, and it is useless for this
market: its scraper broke in October 2025 and resumed in late April
2026, so the weeks Polymarket has traded (February to September 2026)
have zero or a few dozen posts in it; 0 of 23 resolved windows matched
the winning bracket. That is criterion 2 of 17d failing in practice, and
it is why `counts --check-only` exists: before any backtest, the
catalog's count over each resolved window must land in the bracket that
won. If it does not, the catalog is not what the tracker counts (replies,
reposts, deletions, magnitude revisions, time zone) and the result would
be noise either way.

**How sharp the markets already are, with no outside data.** `headroom`
scores the market's own price at the start, the middle and 90% of each
window against the outcome, next to two references that need nothing
external: a uniform spread over the ladder, and a point-in-time
climatology (how often this bracket label had won in the series up to
that moment, needing five earlier outcomes). The Brier of the market at
the window start tells you whether the crowd prices the base rate; the
"blend gap" (market Brier minus the Brier of a 50/50 mix of price and
climatology) tells you whether the base rate carried information the
price lacked, which is the cheapest edge there is. Twelve resolved events
sampled per series (six for the weekly charts), every market of each,
prices from the full trade tapes; 42 series, 3,900 market-offsets:

| series | events | markets | Brier start / mid / late | uniform | blend gap at start | at mid |
| --- | --- | --- | --- | --- | --- | --- |
| trump-truth-social | 12 | 132 | 0.086 / 0.071 / 0.019 | 0.083 | +0.001 ± 0.005 | -0.005 ± 0.005 |
| whitehouse-daily-tweets | 12 | 132 | 0.064 / 0.053 / 0.020 | 0.079 | -0.001 ± 0.002 | -0.002 ± 0.006 |
| khamenei-daily-tweets | 12 | 154 | 0.053 / 0.047 / 0.014 | 0.072 | +0.001 ± 0.005 | -0.002 ± 0.004 |
| zelenskyy-tweets | 12 | 132 | 0.064 / 0.058 / 0.023 | 0.083 | +0.006 ± 0.004 | +0.003 ± 0.006 |
| ted-cruz-daily-tweets | 12 | 132 | 0.072 / 0.072 / 0.065 | 0.075 | +0.001 ± 0.004 | +0.003 ± 0.005 |
| nycmayor-tweets | 12 | 132 | 0.039 / 0.015 / 0.008 | 0.087 | -0.004 ± 0.006 | -0.013 ± 0.006 |
| cz-tweets | 12 | 132 | 0.051 / 0.039 / 0.002 | 0.083 | -0.004 ± 0.004 | -0.005 ± 0.004 |
| 6pt5-earthquake-weekly | 12 | 84 | 0.083 / 0.055 / 0.003 | 0.118 | -0.005 ± 0.006 | -0.016 ± 0.008 |
| 5-5-earthquake | 12 | 101 | 0.087 / 0.072 / 0.046 | 0.101 | +0.001 ± 0.004 | -0.006 ± 0.006 |
| ships-transit-the-strait-of-hormuz | 12 | 73 | 0.109 / 0.098 / 0.063 | 0.156 | +0.028 ± 0.015 | +0.031 ± 0.032 |
| weekly-total-bab-el-mandeb-strait | 10 | 47 | 0.166 / 0.163 / 0.154 | 0.167 | - | - |
| hormuz-transits-neg | 9 | 53 | 0.139 / 0.075 / 0.045 | 0.140 | - | - |
| monthly-tornadoes-us | 10 | 67 | 0.186 / 0.092 / 0.030 | 0.132 | - | - |
| monthly-precipitation | 5 | 38 | 0.116 / 0.109 / 0.002 | 0.114 | - | - |
| claude-downtime | 7 | 37 | 0.152 / 0.145 / 0.073 | 0.175 | - | - |
| temperature-increase | 12 | 70 | 0.215 / 0.113 / 0.024 | 0.142 | +0.101 ± 0.074 | -0.120 ± 0.033 |
| hottest-month | 12 | 30 | 0.168 / 0.002 / 0.005 | 0.317 | -0.090 | -0.098 ± 0.019 |
| egg-prices-monthly | 12 | 102 | 0.107 / 0.066 / 0.046 | 0.103 | -0.004 | -0.005 |
| flu-hospitalization-rate-week | 12 | 67 | 0.078 / 0.024 / 0.018 | 0.146 | -0.026 ± 0.026 | -0.038 ± 0.021 |
| measles | 12 | 43 | 0.242 / 0.110 / 0.032 | 0.479 | - | - |
| ipo-closing-market-cap | 12 | 83 | 0.147 / 0.098 / 0.041 | 0.123 | - | -0.186 ± 0.067 |
| white-house-call-a-full-lid | 12 | 72 | 0.218 / 0.220 / 0.094 | 0.409 | - | - |
| box-office-openings | 12 | 46 | 0.160 / 0.099 / 0.020 | 0.162 | -0.037 | -0.046 |
| rotten-tomatoes | 12 | 57 | 0.175 / 0.137 / 0.035 | 0.401 | -0.010 ± 0.018 | -0.021 ± 0.037 |
| first-week-album-sales | 12 | 73 | 0.144 / 0.111 / 0.044 | 0.137 | - | - |
| top-netflix-show-week | 6 | 94 | 0.097 / 0.022 / 0.029 | 0.110 | -0.090 | -0.093 |
| 1-us-netflix-show | 6 | 119 | 0.088 / 0.002 / 0.001 | 0.086 | - | - |
| billboard-1-song | 6 | 128 | 0.040 / 0.028 / 0.000 | 0.096 | -0.014 ± 0.004 | -0.019 ± 0.009 |
| 1-spotify-song | 6 | 139 | 0.036 / 0.000 / 0.000 | 0.089 | -0.036 ± 0.007 | -0.034 ± 0.007 |
| 1-free-app | 6 | 109 | 0.125 / 0.049 / 0.004 | 0.101 | +0.042 ± 0.052 | -0.008 ± 0.014 |
| best-ai-company | 6 | 85 | 0.108 / 0.062 / 0.001 | 0.103 | +0.056 ± 0.055 | -0.001 ± 0.000 |
| openrouter-ai-market-share | 6 | 90 | 0.032 / 0.029 / 0.013 | 0.077 | -0.005 | -0.016 |
| mrbeast-views-day-1 | 6 | 44 | 0.179 / 0.040 / 0.002 | 0.118 | - | - |
| tsa-daily | 12 | 84 | 0.094 / 0.052 / 0.065 | 0.139 | -0.000 ± 0.016 | -0.013 ± 0.011 |
| tsa-passengers | 12 | 84 | 0.098 / 0.087 / 0.037 | 0.122 | -0.010 ± 0.007 | +0.004 ± 0.063 |
| flight-delays-daily | 12 | 96 | 0.089 / 0.065 / 0.026 | 0.103 | +0.002 ± 0.010 | -0.000 ± 0.013 |
| trump-538-approval | 12 | 74 | 0.139 / 0.135 / 0.018 | 0.136 | +0.001 ± 0.007 | -0.014 ± 0.012 |
| elon-net-worth | 12 | 88 | 0.122 / 0.096 / 0.071 | 0.117 | +0.002 | -0.012 |
| b200-rental-price | 5 | 40 | 0.174 / 0.119 / 0.067 | 0.335 | - | - |
| drought-d4-weekly | 12 | 276 | 0.098 / 0.036 / 0.023 | 0.273 | -0.002 ± 0.000 | -0.001 ± 0.001 |
| tornado-risk-daily | 6 | 150 | 0.055 / 0.024 / 0.006 | 0.002 | +0.001 ± 0.000 | +0.002 ± 0.002 |
| rain-daily | 6 | 90 | 0.143 / 0.062 / 0.002 | 0.216 | +0.002 ± 0.024 | -0.027 ± 0.008 |

The full report, with the uniform and climatology columns and the
clustered standard errors, is `data/headroom_2026-10-01.txt`. How to read
it: "start" is the price at the window's first minute (for series whose
rules state no window, the market's first trade), which in a thin book is
often the maker's seed ladder rather than a price anyone could trade
against, so the honest number is "mid"; "late" is 90% of the way through.
A market that is at the uniform Brier at mid-window has not learned
anything from the public count by then.

What it says, family by family:

* **Posts per week (seven accounts, 946 markets sampled).** At the start
  of every window the market is no sharper than the uniform ladder or the
  base rate, and the blend gaps are all within one standard error of
  zero: there is no base-rate edge, the crowd prices the previous weeks
  about as well as a frequency table does. By 90% of the window six of the
  seven are sharp (0.002-0.023), so the live count is read and priced.
  The exception is Ted Cruz: 0.072 at the start, 0.072 at mid, 0.065 late,
  against a uniform 0.075-0.083. Nobody updates that market during the
  week. It is also the thinnest of the seven ($1.1M lifetime, 1c spreads
  on nothing). Musk's weekly and 48-hour series are missing from the
  table: the trade-history API answered their tapes with server errors
  during the run, and section 15 had measured them anyway.
* **Earthquakes.** The 6.5+ market tracks the USGS feed (0.083 at start
  against a uniform 0.118, 0.003 late). The 5.5+ market does not: 0.087,
  0.072, then still 0.046 at 90% of the week, with the count public and
  one day of remainder to price. That is room, in the family with
  $0.8M of lifetime volume.
* **Ships.** Hormuz shows the one positive blend gap in the table
  (+0.028 ± 0.015 at the window start), and it is not what it looks
  like. The weekly transit count swung from 15-19 ships in March to 150+
  in late June and back to 20-24 in September, and Polymarket re-centres
  the ladder every week around the latest level, so the label that keeps
  winning ("25-49", four of the twelve sampled weeks) is simply the middle
  of a ladder someone at Polymarket already centred on a forecast. "Bet
  the middle bracket" is the market maker's own prior, not niche data, and
  twelve events cannot separate it from luck. Bab el-Mandeb sits at the
  uniform Brier from start to finish (0.166 against 0.167): the market
  never learns, and a partial-week PortWatch count would be the only
  information in it. At $0.2M of lifetime volume it will not pay for the
  model.
* **Monthly and daily accumulations.** Tornadoes, precipitation, the
  global-temperature brackets, Claude downtime and the daily full-lid
  binary all open at or worse than uniform (seed ladders in thin books).
  Precipitation is still at uniform at mid-month (0.109 against 0.114) and
  only resolves at the end; the accumulation to date and the 15-day
  forecast are public and unused, on $0.3M of volume. The full-lid market
  is a coin flip (0.22) until the day itself; the published schedule is
  unused there too. The "hottest month" rank is the opposite case: 0.002
  by mid-month, the crowd already reads the daily reanalysis, and the only
  room is in the first days.
* **Everything else** (flu, eggs, measles, approval, net worth, IPOs,
  Rotten Tomatoes, box office, album sales, GPU prices, drought, the AI
  leaderboard, the weekly charts) sharpens normally through its life and
  shows negative blend gaps: the base rate adds nothing to those prices.
  Box office is worth one note: the price at the market's open is at the
  uniform Brier (0.160 against 0.162) and 0.099 by mid-life, so a
  pageviews model placed on the first day competes with a seed ladder,
  not with the tracking numbers that arrive later.

Taken together: the base rate is already in the price everywhere it can
be read (the Hormuz exception is the ladder's own centring). Where room exists it is the
live count during the window, in markets too thin for anyone to have
bothered: 5.5+ earthquakes late in the week, Ted Cruz's posts, Bab
el-Mandeb transits, monthly precipitation, the full lid. Those are
$0.2-1.2M-lifetime families, which is the honest size of this edge: a
few dollars a day for a bot that reads a public feed and quotes
minimum-size orders, in exactly the way section 17b's rewards rig does,
and combinable with it: the open markets of these very families carry
liquidity-reward pots today (full lid $50 a day per market, Bab
el-Mandeb $5-51, 5.5+ quakes $1-30, Ted Cruz $1-6), so a quote that
knows the count would be paid to rest there. The recipes below are
ordered by that reading.

### 19c. Recipes, in the order I would run them

Everything below runs on your laptop against the resolved markets; the
scoring is the `signal` command from 17d, and the decision rule is the
one written there: a blend gap two standard errors above zero on markets
the mapping was not fitted on, then the $50 live test of section 18
restricted to those markets.

1. **Earthquakes** (an afternoon). One request gives the whole catalog:

   ```
   curl -o quakes.csv "https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv&starttime=2024-01-01&minmagnitude=5.5"
   python -m pm_scanner counts --series 6pt5-earthquake-weekly --catalog quakes.csv --value-col mag --min-value 6.5 --check-only
   python -m pm_scanner counts --series 6pt5-earthquake-weekly --catalog quakes.csv --value-col mag --min-value 6.5 --out q65.csv
   python -m pm_scanner signal --csv q65.csv
   python -m pm_scanner counts --series 5-5-earthquake --catalog quakes.csv --value-col mag --min-value 5.5 --out q55.csv && python -m pm_scanner signal --csv q55.csv
   ```

   The caveat is magnitude revision: USGS reviews magnitudes for hours to
   days, and a 6.4 that becomes a 6.5 changes the count after the fact.
   The catalog you download is the revised one, so the backtest is
   slightly flattered; the `--check-only` step shows how often the final
   catalog disagrees with the bracket that won, which bounds it.

2. **Ship transits** (an afternoon). Download the daily chokepoint
   transit-call file from PortWatch (portwatch.imf.org, "Daily Chokepoints"
   dataset, CSV; one row per chokepoint per day), filter to the Strait of
   Hormuz, and run with the count column:

   ```
   python -m pm_scanner counts --series ships-transit-the-strait-of-hormuz --catalog hormuz.csv --count-col n_total --check-only
   python -m pm_scanner counts --series ships-transit-the-strait-of-hormuz --catalog hormuz.csv --count-col n_total --out hormuz.csv.signal.csv
   python -m pm_scanner signal --csv hormuz.csv.signal.csv
   ```

   Here the honest point-in-time version is stricter than the catalog: a
   day's count is published three to five days later, so the live version
   of the model would know fewer days than the backtest assumes. Score it
   twice, once as is and once with the decision time shifted by the lag,
   and believe the second. The live AIS feed (aisstream.io is free) is the
   niche data that closes that gap, and it can only be collected forward.

3. **Posts** (an hour per account, 3 Oct: minutes). The tracker's page only
   downloads one window at a time, built in the browser, but the site's own
   JavaScript calls a JSON API nobody documents (`/api/users/<handle>`,
   `/api/users/<handle>/posts?limit=&startDate=&endDate=`,
   `/api/trackings/<id>?includeStats=true`); `xtracker` walks it from the
   day the tracker started following the account (Musk: 18 Nov 2025, 12,913
   posts held on 3 Oct 2026, so about 45 weekly windows) and writes one row
   per post with the post's UTC time:

   ```
   python -m pm_scanner xtracker --list
   python -m pm_scanner xtracker elonmusk                          # -> xtracker/elonmusk.csv
   python -m pm_scanner counts --series elon-tweets --catalog xtracker/elonmusk.csv --check-only
   python -m pm_scanner counts --series elon-tweets --catalog xtracker/elonmusk.csv --out elon_signal.csv
   python -m pm_scanner signal --csv elon_signal.csv --fill-wait 6
   ```

   and the same for `whitehouse-daily-tweets`, `zelenskyy-tweets`,
   `ted-cruz-daily-tweets`, `nycmayor-tweets`, `cz-tweets`,
   `trump-truth-social`, then `elon-tweets` last (section 15 says it is
   the one with informed takers; it is also the one with $1.4B of volume).
   The model's phase profile matters most here: posting follows a weekly
   rhythm, and the default allocates the remaining count by the previous
   eight weeks' hour-of-week pattern. `--no-profile` is the control.

4. **Tornadoes** (a weekend). Build a catalog from SPC's daily preliminary
   tornado reports (`spc.noaa.gov/climo/reports/YYMMDD_rpts_torn.csv`, one
   file per day since 2004), run with `--monthly`, and apply the
   preliminary-to-final ratio from NCEI's history as a fixed scale on the
   catalog count before testing; the family is small (67 resolved
   markets), so only a large gap would show.

   Monthly precipitation has the same shape with a continuous total: the
   agency's daily observations (KMA for Seoul, the NWS climate reports
   for the US cities) are the catalog with `--count-col` holding the
   millimetres, the remainder comes from Open-Meteo's 15-day
   precipitation sum rather than the negative-binomial default, and the
   headroom table says the market is at uniform until mid-month.

5. **The regression families** (box office first, then GISTEMP, eggs,
   IPOs) need a hand-built `market,time,p` file rather than `counts`: a
   mapping from each market to the upstream series (film title to
   Wikipedia article; month to ERA5 days; IPO to its 424B4), the
   regression fitted strictly on years before the markets existed, and a
   probability per bracket from the residual distribution. Box office is
   the best of them: 1,220 resolved markets, a published method, and
   pageviews that are archived hourly and cannot leak the future. It is
   also the most work (about three hundred title mappings).

6. **The full lid** is a daily binary rather than a count: a
   `market,time,p` file from the previous evening's published guidance
   (Factbase keeps the archive), with P(full lid by 6:30) read off the
   same features in the earlier days of the series, scored at 9 AM ET on
   the day. The market sits at a coin flip until the afternoon.

### 19d. What to expect, honestly

* The volume in these families is $1-15M over their lifetimes and the
  books are thin; this is $50-500 money, which is what you have, and it
  does not scale.
* A positive result in a count family will come from the live count more
  than from the base rate (the headroom table says how much of each is
  there); that means a bot running daily, not a trade a week.
* The posts families are the largest sample and the easiest data, but
  they are the ones section 17c set aside; the base-rate model is a
  different use than information processing, and the small accounts were
  never measured. If the headroom table shows them already sharp at the
  window start and sharper mid-window, that question is closed too.
* Nothing here has been tested against outcomes yet. The tooling and the
  Polymarket side are done and offline-tested (`tests/test_counts.py`);
  the upstream pulls and the verdicts are the laptop's.
* One half of this could be tested from here after all, and was: 19e, below,
  runs the model against outcomes on the posts families using Polymarket's
  own bracket close times as the count.

### 19e. Tested against outcomes from here (1 Oct): the brackets' own close times

**Why it is possible after all.** Polymarket does not wait for the window to
end to resolve a count bracket that can no longer win. In "Donald Trump #
Truth Social posts September 11 - September 18, 2026" the "<20" market was
resolved NO on the 14th at 01:57 UTC, "20-39" on the 15th at 01:57, "40-59" on
the 16th at 04:22, "60-79" on the 18th at 00:18, each by UMA while the window
was open; the winner (80-99) and the brackets above it closed after the window.
Every early close is a public, timestamped fact about the running count: at
that moment the count was at least the ceiling plus one. The sequence is a
staircase lower bound on the count through the window, and Gamma carries it
(`closedTime` per market) for every resolved window of every posts series:
about 3,500 early closes in about 590 windows across the nine accounts.
Earthquake, ship and weather brackets close only after their windows (USGS
revises magnitudes, PortWatch publishes with a lag, monthly totals come from a
report), so this covers the posts families only, which are also the ones whose
own export this container cannot reach.

**The test, written before the numbers.** `crossings` (new command) runs the
model of `counts` at every early close of every consistent window, with two
handicaps against it:

* the count it uses is the lower bound (ceiling plus one); the true count at the
  close time is higher by whatever accumulated during the proposer's and UMA's
  lag, which the close-lag diagnostic measures from the price collapse;
* no phase profile: the remainder is the mean of the last eight windows'
  totals (winning-bracket midpoints, windows that had ended by the decision
  time only) times the fraction of the window left, negative-binomial when the
  totals are over-dispersed.

Every bracket still open at the decision time gets a probability, scored by
the `signal` harness of 17d against the last trade at or before that moment
(dropped when older than 24 hours) and the resolution, with the market's own
taker fee for the paper trade. Standard errors cluster by event (one ladder at
one moment is one observation). Windows where an early close sits above the
winning bracket (a bulk resolution, not a crossing) are excluded. The decision
rule is 17d's: a blend gap two standard errors above zero in a family makes it a
candidate for the live test; the model-minus-market gap and the paper P&L say
whether the edge survives the taker fee. Because both handicaps work against
the model, a positive result is conservative and a null one is ambiguous.

**Two variants added after the first look, so not pre-registered.** The first
run (White House and Ted Cruz, the two families whose tapes were cached)
showed two things that changed the design. The close-lag diagnostic put the
UMA close a median of 33 hours (Cruz) and 74 hours (White House) after the
bracket's price had collapsed under 10c: the lower bound dated by the close is
one to three days stale on a seven-day window, and the model's reliability
table showed exactly that, its 0.9+ calls winning 57-87% because the true count
had moved on. So:

* `--timing collapse` dates each crossing by the bracket's price collapse (the
  first print under 10c after the last one at or above it) instead of the UMA
  close. It asks whether the rest of the ladder was priced consistently at the
  moment the crowd itself marked a bracket dead. The bound is no longer
  certain: a market can sell a bracket to nothing a little before the count
  actually passes it, and the choice of "the collapse that held" looks at the
  bracket's own later prices (never at the brackets being scored).
* `--count interval` replaces the lower bound by the whole range from it to
  the ceiling of the lowest bracket still alive (the crowd has not killed that
  one, so the count has most likely not passed it), averaged. It uses only the
  staircase of dead and alive brackets at the decision time.

The pre-registered row is the first one in each table below; the variants are
there to show how much of the result is the handicap and how much is the
market. A tight-staleness run (last trade within two hours of the decision
time, so the price is one someone could plausibly have hit) is the robustness
check on the paper P&L.

**What came out.** Seven accounts (Trump, the White House, Khamenei,
Zelenskyy, Ted Cruz, the New York mayor, CZ): 419 resolved windows, 322 with
usable early closes, about 1,360 decision times and 9,900 (bracket, moment)
rows, 8,000 of them with a print inside the previous 24 hours. Pooled, with
event-clustered standard errors (A is the pre-registered test):

| variant | windows | decisions | rows scored | Brier market / model | market - model | market - blend | paper trades | P&L per $100 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A. close-dated, lower bound (pre-registered) | 322 | 1365 | 8182 of 9903 | 0.0905 / 0.1087 | -0.0182 ± 0.0032 | -0.0025 ± 0.0015 | 3318 | +4.4 ± 2.5 |
| A, prints within 2 h | 322 | 1365 | 4482 of 9903 | 0.1055 / 0.1368 | -0.0313 ± 0.0048 | -0.0069 ± 0.0021 | 2165 | +0.4 ± 2.9 |
| A, interval count | 322 | 1365 | 8182 of 9903 | 0.0905 / 0.1053 | -0.0149 ± 0.0029 | -0.0006 ± 0.0014 | 3480 | +9.2 ± 2.5 |
| B. collapse-dated, lower bound | 322 | 1359 | 7951 of 9868 | 0.1015 / 0.1228 | -0.0213 ± 0.0056 | +0.0035 ± 0.0027 | 3851 | +15.7 ± 2.8 |
| B, prints within 2 h | 322 | 1359 | 3980 of 9868 | 0.1266 / 0.1531 | -0.0265 ± 0.0088 | +0.0057 ± 0.0043 | 2358 | +16.6 ± 3.4 |
| B, prints within 72 h | 322 | 1359 | 8965 of 9868 | 0.0981 / 0.1170 | -0.0188 ± 0.0051 | +0.0040 ± 0.0025 | 4191 | +16.5 ± 2.7 |
| B, interval count | 322 | 1359 | 7951 of 9868 | 0.1015 / 0.1312 | -0.0297 ± 0.0061 | +0.0013 ± 0.0030 | 4127 | +15.7 ± 3.0 |

The paper P&L in that table is taken at the last print before the decision
time, which in a thin ladder is often a print nobody would fill at. The honest
version re-executes each trade at the first print after the decision time on
the side we would have needed (a taker buying YES for our YES, a taker selling
it for our NO), within six hours, and decides again at that price:

| variant | paper trades at the last print: n, P&L per $100 | re-executed at the next print on our side within 6 h: filled, taken, P&L per $100 | fill worse than the last print by |
| --- | --- | --- | --- |
| A. close-dated, lower bound (pre-registered) | 3318, +4.4 ± 2.5 | 2267, 1846, -2.0 ± 3.2 | 2.7c |
| A, prints within 2 h | 2165, +0.4 ± 2.9 | 1745, 1494, -2.7 ± 3.5 | 1.9c |
| B. collapse-dated, lower bound | 3851, +15.7 ± 2.8 | 2557, 2268, +11.7 ± 3.5 | 3.4c |
| B, prints within 2 h | 2358, +16.6 ± 3.4 | 1850, 1692, +13.0 ± 3.9 | 2.8c |
| B, prints within 72 h | 4191, +16.5 ± 2.7 | 2640, 2320, +11.6 ± 3.5 | 3.7c |
| B, interval count | 4127, +15.7 ± 3.0 | 2696, 2408, +13.0 ± 3.7 | 3.1c |

Reading it:

* **A, the pre-registered test, fails.** The market's Brier is 0.018 better
  than the model's, the blend gap is negative (mixing the model into the price
  makes the price worse) and no family reaches the two-standard-error rule
  (Khamenei at +0.012 ± 0.006 and Cruz at +0.004 ± 0.003 are the closest).
  Its +4 per $100 at the last print is the stale-print artefact: at the next
  print it is -2.0 ± 3.2, and with prints no older than two hours it is +0.4.
  The reliability table says why: the model's 0.9+ calls win 87% and its
  0.6-0.9 calls 53%. The count it holds is one to three days old (the lag
  table below), so it keeps betting on brackets the count has already left.
* **B, dated by the price collapse, is the first positive paper number in
  this memo.** The market is still sharper on Brier (the handicapped model
  stays overconfident) and the blend gap is positive but not at two standard
  errors (+0.0035 ± 0.0027; +0.0040 ± 0.0025 with 72-hour prints), so the
  17d criterion is not met here either. The paper P&L is another matter:
  +15.7 ± 2.8 per $100 at the last print, **+11.7 ± 3.5 re-executed at the
  next print on our side** (2,268 trades, fills 3.4c a share worse than the
  last print), and it holds under every variation tried: prints within two
  hours +13.0 ± 3.9, within 72 hours +11.6 ± 3.5, the interval count +13.0 ±
  3.7.

By family and by window phase (B; A alongside for the comparison):

| series | windows | decisions | A: market - blend | A: P&L per $100 at the fill (trades taken) | B: market - blend | B: P&L per $100 at the fill (trades taken) |
| --- | --- | --- | --- | --- | --- | --- |
| cz-tweets | 27 | 31 | -0.0031 ± 0.0029 | -32.9 ± 14.5 (17) | +0.0019 ± 0.0022 | -1.3 ± 9.3 (23) |
| khamenei-daily-tweets | 15 | 25 | +0.0117 ± 0.0062 | +10.0 ± 14.6 (18) | +0.0229 ± 0.0107 | +3.9 ± 22.0 (25) |
| nycmayor-tweets | 49 | 71 | +0.0031 ± 0.0024 | +27.3 ± 9.9 (25) | +0.0023 ± 0.0055 | +15.5 ± 8.4 (37) |
| ted-cruz-daily-tweets | 51 | 265 | +0.0037 ± 0.0032 | -6.9 ± 9.2 (207) | +0.0061 ± 0.0041 | +13.7 ± 7.9 (248) |
| trump-truth-social | 61 | 356 | -0.0029 ± 0.0024 | +2.7 ± 4.5 (722) | +0.0037 ± 0.0047 | +12.1 ± 5.4 (926) |
| whitehouse-daily-tweets | 51 | 428 | -0.0083 ± 0.0038 | -5.3 ± 6.7 (661) | +0.0003 ± 0.0083 | +14.9 ± 7.2 (766) |
| zelenskyy-tweets | 49 | 181 | -0.0038 ± 0.0026 | -6.4 ± 8.8 (196) | +0.0019 ± 0.0035 | -1.0 ± 7.4 (243) |

| window phase | windows | decisions | A: market - blend | A: P&L per $100 at the fill (trades taken) | B: market - blend | B: P&L per $100 at the fill (trades taken) |
| --- | --- | --- | --- | --- | --- | --- |
| early (<1/3) | 207 | 488 | +0.0021 ± 0.0017 | +2.2 ± 5.5 (389) | +0.0043 ± 0.0032 | +9.5 ± 4.9 (729) |
| mid | 236 | 541 | -0.0036 ± 0.0020 | -1.3 ± 4.4 (819) | -0.0017 ± 0.0034 | +10.1 ± 4.2 (1043) |
| late (>2/3) | 203 | 328 | -0.0060 ± 0.0028 | -5.3 ± 4.6 (638) | +0.0121 ± 0.0052 | +19.1 ± 6.4 (496) |

Trump, the White House, Cruz and the mayor carry it; Zelenskyy and CZ show
nothing after the fill; Khamenei has fifteen windows and is noise either way.
It is largest late in the window, where the count bound is most informative.

**Where the money comes from** (B, at the last print; the fill-adjusted
figures are three to four cents a share worse):

| trade | the bracket, relative to the count bound | trades | P&L per $100 | win rate | mean price |
| --- | --- | --- | --- | --- | --- |
| buy YES | above the bound | 1,638 | +47 | 0.27 | 0.18 |
| buy NO | holds the bound | 655 | +23 | 0.17 | 0.33 |
| buy YES | holds the bound | 167 | +25 | 0.80 | 0.64 |
| buy NO | above the bound | 1,379 | +2 | 0.26 | 0.28 |

The crowd anchors on the bracket the count is in right now (it prices that
bracket at 33c when it wins 17% of the time) and underprices the brackets
above it (18c for brackets that win 27%). "Count so far plus the remaining
rate" is the arithmetic the price skips, which is 19a's hypothesis in one
table; selling the brackets above the bound, the mirror image, earns nothing,
so this is not a generic "fade the ladder" effect.

How stale the bound is when the test is dated by the close (hours from a
crossed bracket's price collapse to its UMA close):

| series | hours from price collapse to UMA close: median [q1, q3] | n |
| --- | --- | --- |
| cz-tweets | 11 [4, 26] | 33 |
| khamenei-daily-tweets | 3 [2, 7] | 26 |
| nycmayor-tweets | 45 [11, 78] | 50 |
| ted-cruz-daily-tweets | 30 [11, 71] | 143 |
| trump-truth-social | 39 [12, 85] | 246 |
| whitehouse-daily-tweets | 64 [25, 106] | 146 |
| zelenskyy-tweets | 26 [7, 54] | 113 |

**Musk's two series**, scored after everything above and so not used to
design B: the liquid end of the family, 169 weekly and 90 two-day windows with
early closes, 31,000 rows, fills within half a cent of the last print.

| series | windows | decisions (A / B) | A: market - blend | A: P&L per $100 at the fill (trades taken) | B: market - blend | B: P&L per $100 at the fill (trades taken) |
| --- | --- | --- | --- | --- | --- | --- |
| elon-tweets (weekly) | 163 | 1,591 / 1,563 | -0.0014 ± 0.0004 | +0.7 ± 1.9 (5,060) | **+0.0018 ± 0.0007** | **+10.8 ± 1.8 (8,628)** |
| elon-tweets-48h | 87 | 163 / 161 | -0.0025 ± 0.0030 | +9.8 ± 5.6 (360) | -0.0027 ± 0.0028 | +4.7 ± 6.6 (424) |

The weekly series is the one family large enough to settle B, and it settles
it in B's favour: the blend gap is positive at 2.6 standard errors, which is
17d's criterion met, and the fill-adjusted P&L is +10.8 ± 1.8 per $100 over
8,628 trades. The shape is the thin families' shape: the brackets above the
count bound, priced at 5c on average, win 10% of the time (+78 per $100 on
$247 staked); selling the bracket that holds the bound earns +16 on $781;
selling the brackets above it +5 on $2,853. A, as everywhere, shows nothing
(+0.7 ± 1.9), and blending the close-dated model into the price makes the
price worse (-0.0014 ± 0.0004). The two-day series has 160 decisions and shows
nothing after the fill in either timing. The close lags the collapse by a
median of 41 hours in the weekly series: under B that is how long the
most-traded count market on the site takes to re-price the rest of its ladder
after killing a bracket, and it is not zero.

**Why B is not yet a result to bank.**

1. It is post hoc. The timing was changed after the first look, on A's lag
   table. It has since held across six variants on the seven thin families
   and on Musk's weekly series, which was scored last, was not used to design
   it, and meets 17d's criterion on its own. What it does not have is a timing
   written down before any number was seen; the test that was, A, fails.
2. The collapse time is chosen knowing the collapse held (it looks at the
   bracket's own later prints, never at the brackets scored), and a bracket
   can be sold to nothing shortly before the count actually passes it. Live,
   neither applies: the tracker is public and the count is read directly,
   before the crowd kills the bracket. But it makes B a proxy for the live
   signal, not the signal itself.
3. Capacity. These ladders print 5-70 shares at 10-40c. The whole seven-family
   sample staked $1,840 across 3,851 trades in seven months; at $50-500 this
   is tens of dollars a month, as 19d said.
4. The model's magnitudes are wrong even in B (a stale bound, no phase
   profile): the P&L comes from the direction of the disagreement, filtered at
   five cents plus the fee. A model with the real count and a phase profile
   should be both sharper and more profitable, which is what the laptop run
   with the tracker's export (19c, recipe 3) would show.

**What this changes in the plan.** 19c's recipes stand, but the posts
families move to the front and the first laptop run is the tracker export for
Musk, Trump, the White House, Cruz and the mayor: `counts` with the real
count, then `signal --fill-wait 6`. If that reproduces B's fill-adjusted P&L
at two standard errors with the real count (which is earlier and exact, so it
should do better, not worse), the $50 test of section 18 gets a second leg: at
each tracker update that carries the count past a bracket ceiling, take the
model's trades at taker, two to five dollars a trade, in Musk's weekly ladder,
the one where $50 fills at the print, and rest the thin ladders as maker
quotes where the reward pots pay. Until then the decision rule of 17d stands:
B meets it on Musk's series, A, the test as written before the numbers, says
no, and the laptop run with the real count is what breaks the tie.

### 19f. The real count (3 Oct, laptop): the tracker's own API, and the catalog matches 94 of 94 windows

**The data source.** xtracker.polymarket.com, the counter every posts ladder
resolves on, has no documented API and its "Posts" button only downloads one
window at a time, built in the browser. Its own JavaScript, read off the
Next.js bundle, calls a small JSON API: `/api/users` (ten tracked accounts),
`/api/users/<handle>` (the record, the active windows with their exact start
and end, the total posts held), `/api/users/<handle>/posts?limit=&startDate=&endDate=`
(the counted posts, with the post's own `createdAt` in UTC and an
`importedAt`), `/api/trackings/<id>?includeStats=true`. The `xtracker`
command walks the posts route from the day the tracker started following the
account and writes one row per post (`pm_scanner/xtracker.py`). Musk's history
came back in 59 requests: 12,913 posts, exactly the total the account record
holds, from 31 Oct 2025 to the minute of the run, no page cap in the way. The
browser exports agree with the API to the minute on all 280 posts both hold
when their "Posted At (EST)" is read as New York time, so the label means the
local zone, not fixed EST.

**The check that had to pass first.** `counts --check-only` compares the
catalog's count over every resolved window the catalog fully covers with the
bracket that won: 94 windows, 94 matches, 0 mismatches, through the week that
closed on 2 Oct (222 posts, winner 220-239). The catalog is exactly what the
resolution counts: replies and reposts included, the noon New York boundary as
parsed, nothing to adjust. The weekly pace since August runs 158 to 310 posts
per UTC week; the 4 downloaded windows ran at about 245 a week.

**The other four accounts, same evening.** Each fetch returned exactly the
total the tracker holds, and three of the four catalogs match every resolved
window they cover: the White House 54 of 54 (tracked since 15 Jan 2026, 6,062
posts), Ted Cruz 54 of 54 (since 12 Mar, 3,544), the NYC mayor 52 of 52 (since
16 Mar, 1,075; 26 to 39 posts a week, so one bracket, 20-39, wins every week
and the ladder has nothing to price). Trump's Truth Social catalog (since 21
Jan, 5,391 posts) matches 48 of 65 windows and undercounts the other 17, every
one of them on the low side. Nine of the seventeen sit one to four posts under
the floor of the bracket that won (97 against 100-119, 99 against 100-119, 138
against 140-159, 198 against 200+, 158 against 160-179, 119 against 120-139
twice, 139 against 140-159, 156 against 160-179): with 20-post brackets, a
steady loss of a few posts a week flips exactly the weeks that happened to
land just under a boundary and leaves the rest looking exact, so the 48
matches do not mean 48 complete weeks. Two bursts are missing outright: 12 to
22 May (the catalog holds 100 and 68 in two overlapping weeks that resolved
200+) and 11 to 21 Aug (141 and 145 against 200+).

Deleted posts were my first reading; the tracker's own records rule it out.
`xtracker --stats` reads each of the 73 Trump trackings with
`includeStats=true` (the record carries `total`, `pace`, `percentComplete` and
an hourly `daily` series), and the tracker's total equals the catalog's count
on every one of them, the mismatched weeks included: the tracker itself holds
100 posts for 12 to 19 May and 68 for 15 to 22 May, and zero, a whole week of
nothing, for 19 to 26 May. Trump did not fall silent for a week; the tracker's
Truth Social import did. Truth Social has no public API, so the tracker
scrapes it, and the scraper has gaps: a full outage in mid-May, a partial one
in mid-August, and a steady leak of a few posts a week the rest of the time
(the near-boundary misses). The markets resolved on the true count anyway,
which means the resolver counted from Truth Social itself when the tracker was
short. Two consequences. The Trump catalog is a lower bound, not the count,
so its backtest is not run for a verdict; and for Truth Social the tracker's
live number is also a lower bound, which is a risk to anyone pricing off it,
not an edge for us. The X accounts have none of this: their tracker runs on
the X API and matched every window. Also held: ZelenskyyUa 2,393 posts,
Cobratate 881, cz_binance 643, khamenei_ir 155.

**The pre-registered run.** `counts --out` wrote 21,494 decision rows for the
Musk ladders (daily at 12:00 UTC plus the day before each window opens; 1,374
decision times before the catalog has eight reference windows and 442 after
its last post were skipped). `signal --fill-wait 6` scored them against one
trade tape per resolved market (about one tape a second from data-api, cached
on disk, so the first run took most of an hour and the rerun seconds).

### 19g. The verdict (3 Oct): with the real count, the model does not beat the market

The numbers, on 14,727 rows across 2,580 resolved Musk markets (5,395 rows
fell after the market's last print, 745 had a price older than a day, 482 had
no price yet, 145 markets are still open):

| | real count, 12:00 UTC daily | B's proxy (19e), at the crossings | rule (17d) |
|---|---|---|---|
| Brier, signal / market / blend | 0.0346 / 0.0324 / 0.0326 | | |
| market minus signal | **-0.0022 ± 0.0006** | | |
| market minus blend | **-0.0002 ± 0.0003** | +0.0018 ± 0.0007 | >= 2 se above zero |
| markout, 30 min, signal's direction | +0.0001 ± 0.0001 per share | | |
| paper trades, at the last print | 2,323 trades, +4.26 ± 2.12 per $100 | | |
| re-executed at the first print after, within 6 h | 2,319 found one, 2,152 still cleared the edge, **+3.71 ± 2.16 per $100**, fills 0.3c against | +10.8 ± 1.8 | positive at 2 se |

Both halves of the rule fail. The market is sharper than the model by 3.7
standard errors, the blend adds nothing the price did not have, and the
re-executed P&L is positive at 1.7 standard errors, under the bar, with a
confidence interval that reaches from -0.6 to +8.0 per $100. The reliability
table says why: the model is overconfident in its tails. Rows it put at 0.95
to 1.00 resolved YES 75% of the time (36 rows), 0.70 to 0.85 came in at 44%,
0.30 to 0.50 at 21%. The remainder distribution (negative binomial on the
trailing eight weeks, allocated by the hour-of-week profile) is too narrow for
an account that posts in bursts; the market, which watches the same public
count, prices those bursts better. Fixing the dispersion on these outcomes
would be fitting on the test set, and is not done.

**Why the proxy looked better.** B's decision times were the bracket closes:
the moments the running count crossed a ceiling, when the market has to
reprice a whole rung of the ladder. The real-count model decides at noon UTC
every day, when nothing in particular has happened. If B's +10.8 was real, it
was a timing effect at the crossings, not count information, because the
count is public on the tracker and the market has it; A, the close-dated
variant written down before the numbers, said no, and B was post hoc. The
tie-breaker has now spoken for A.

**What this decides.** Per 17d and 19e: no taker leg in Musk's ladder, and the
$29 on the account goes only to the liquidity-reward test of section 18, as
the smoke test first. The posts catalogs stay useful for what they proved
tonight: the tracker is exact for X accounts, so a count model can be run on
the White House and Cruz for information with the same recipe, and the
pre-registered control (`counts --no-profile`, uniform allocation) can be
scored to see whether the phase profile helped or hurt. Neither changes the
decision unless it clears the same bar on a sample this size.

## 20. The live account (3 Oct): the credentials check and the smoke plan

`lp --check` with the four variables set, from the laptop, sending nothing: the
account is `0x66FE…34c8`, wallet type `DEPOSIT_WALLET` (Polymarket's own
proxy, so orders go through the gasless relayer and the signer never holds
MATIC), collateral balance $29.00, token allowances already at the maximum on
the four exchange contracts, `gasless_ready` true. The SDK's approvals check
first failed on my side (`'MissingTradingApprovals' object is not iterable`:
it returns a dataclass with `erc20` and `erc1155` tuples), then, fixed, listed
one missing approval: the collateral token for the perpetuals deposit contract
(`perps_deposit_contract` in the SDK's environment config), which this rig
never calls. The six that matter for resting orders (the standard and
negative-risk exchanges, the two collateral adapters, the v2 router and the v3
exchange) are granted. `--check` now names each missing approval and reports
`approvals_ok_for_quoting` separately; `--approve` grants the rest through the
relayer if that ever becomes necessary.

The quote plan found nothing at `--budget 25 --markets 3` because the
per-market cap is 1.6 × 25 / 3 = $13.33 and a minimum-size two-sided quote in
a 20-share market parks about $19; the rig now says which filter stopped each
candidate. At `--smoke --budget 25` it picked one market: "Will EU emergency
diesel stocks be at least 34M tonnes at end …", mid 0.30, our bid 0.28 and ask
0.33, 20 shares a side, $19.00 of collateral, 28 days to resolution, a pot the
rewards model puts at $200 a day with nobody else inside the max spread. The
next command is that smoke test live for two hours (section 18c), watched,
with `--cancel-all` ready; its log and the next day's `--earnings` read go
here.

**The smoke run (3 Oct, 23:47 UTC), three minutes long.** Run as `lp --live
--smoke --budget 25` with the laptop kept awake. What happened, from the log:

* 23:47:56. The plan was not the EU diesel market of the dry run twenty
  minutes earlier but "Rain during the Bahrain Grand Prix?": mid 0.895, 8.2
  days to resolution, a $200-a-day pot with nobody else inside the max spread.
  The two pots were equal and the sort was stable on input order, so the live
  run quoted whichever came first; and "rain" was not on the exclusion list
  that keeps the rig out of weather. Both orders were accepted with ids: a YES
  bid at 0.87 and a NO bid at 0.08 (a YES ask at 0.92), 20 shares each, $19.00
  parked. The scoring read at the same second said 0 of 2 scoring. The
  approvals read timed out on the RPC; the allowances line stood.
* 23:48:57. The YES bid was hit for 15.31 shares at 0.87, $13.32, sixty-one
  seconds after it was placed, in a book where nobody else had been quoting
  inside the spread. That is the abort condition written in 18d ("any fill in
  a market whose book was empty when quoting started"). The rig cancelled the
  rest of the bid and kept the ask.
* 23:50:58. The mid read 0.88 and the rig re-centred: it pulled the ask, then
  crashed trying to cancel the ask a second time with an id it had already
  cleared (a stale flag; the SDK rightly refused a `None`). The `finally`
  block ran `cancel_all` before the traceback, so no order was left open. Two
  things about that mid: our own two quotes had defined the 0.895 the rig was
  centred on, and once the bid side was gone the "move" was partly the book
  without us. The rig now reads the mid from everyone else's orders and holds
  its quotes when nobody else is in the book.

Against the checklist of 18c: point 2 passed (two live orders, ids, on the
book); point 3 is not established (one reading at placement said 0 of 2, the
checklist asked for ten minutes, and the run did not get there); points 4 and
6 did not arise; point 5 happened through the exception path and should be
confirmed on the site. The thing the test did establish is the one 17d feared:
a lone minimum-size quote in an unquoted rewarded market is taken within a
minute, at the moment the market moves against that side. Whether the pot
would have paid for it was never measured. Outcome of the money: $13.32 is in
15.31 YES shares of a weather market resolving in eight days, marked about
flat at the 0.88 mid; its disposal and the final P&L go here when known.
Morning of 4 Oct: `--cancel-all` confirmed nothing open; the site showed the
15.3 shares at an average of 87c marked at 76.5c, value $11.71, down $1.61
(12%), cash $15.68, portfolio $27.39. The seller who hit the bid was followed
by an eleven-cent fall overnight, which is what memo 14 said weather takers
look like. The book under that mark was 26 cents wide: a 73c bid for 17.69
shares, a 72c bid for 12, then nothing until 99c; the site's sell panel
offered $10.87 for the lot, a loss of $2.45, not the $0.15 the mark implied.
By 06:00 UTC the 73c bid had gone too and the best bid was 58c for 25 shares,
which would have paid $8.69 (−$4.63).

Disposal: a resting limit sell at 80c, the lone ask in that book, placed by
hand on the site, was hit later that morning: 15.3 shares at 80c, $12.24, as
maker, no fee. **Final P&L of the first smoke run: −$1.08** on $13.32 (bought
at 87c, sold at 80c), cash back to about $27.92, nothing held, no order open.
For the record, the advice that morning was to hit the 73c bid for $10.87;
the resting sell did $1.37 better, and the fill came from a buyer stepping up
to an ask with nothing near it, not from a move against it. One case, no
conclusion, but it argues for a "sell as maker inside the spread, with a
deadline" step before any taker exit if the rig ever has to unwind again.

Changed after the fact, none of it pre-registered: the re-centre crash fixed
with a regression test; the mid taken from other people's orders; "rain",
"snow", "storm", "wind", "flood" and "grand prix" added to the exclusion
list; the mid band for a candidate tightened from 0.05–0.95 to 0.15–0.85 (a
bid at 0.87 risks 87 cents to earn 13 against whoever learns of a drier
forecast first); equal pots broken by days to resolution, longer first; and a
live run must name its market with `--only` so that what is quoted is what
the dry run showed. Whether to run the smoke test again at all is a decision
for the morning, with the fill above as its first data point.

4 Oct, a small one: `MAX_BUDGET_USD=29 python -m pm_scanner lp --cancel-all`
was refused ("--budget 50 is above the hard cap 29") because the default
budget tripped the cap before the command noticed it only had to cancel.
Cancelling, reading earnings and approving size no quote, so they now skip
the cap; with a cap in the environment the plain command works as written.

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
* Order flow (sections 15-16): Polymarket data-api `/trades?market=<conditionId>&limit=10000` (`side` read as the taker's side; outcome labels matched against the market's outcome list), 30 Sep 2026; full report in `data/flow_families_2026-09-30.txt`.
* Weather (sections 11-13): Iowa Environmental Mesonet ASOS/METAR archive ([download form](https://mesonet.agron.iastate.edu/request/download.phtml), CGI `cgi-bin/request/asos.py`, routine reports = `report_type=3`); Open-Meteo [previous-runs API](https://open-meteo.com/en/docs/previous-runs-api), [ensemble API](https://open-meteo.com/en/docs/ensemble-api), model ids incl. `gfs_graphcast025` and `ecmwf_aifs025_single`; Polymarket data-api `/trades?market=<conditionId>&limit=10000` (full history of closed markets); station coordinates from published aerodrome data; IMS forecasts at [ims.gov.il](https://ims.gov.il/he) (no archive of past forecasts).
* Niche survey (section 10): Gamma `/events` by tag with `start_date_min/max` windows and `exclude_tag_id=102127`, `/series`, CLOB `/books` and `/prices-history` (history is purged about a week after a market closes; `data-api.polymarket.com/trades` keeps trades longer), 29 Sep 2026. Weather market rules cite NOAA `weather.gov/wrh/timeseries?site=<ICAO>` (US stations and LLBG Tel Aviv) and Weather Underground daily history (other cities). Forecast data for the backtest: [Open-Meteo historical forecast API](https://open-meteo.com/en/docs/historical-forecast-api), [Open-Meteo ensemble API](https://open-meteo.com/en/docs/ensemble-api).
* Section 17: Polymarket docs [Liquidity Rewards](https://docs.polymarket.com/programs/liquidity-rewards) (scoring formula, sampling, single-sided rule), [Maker Rebates](https://docs.polymarket.com/programs/maker-rebates), [Fees](https://docs.polymarket.com/trading/fees), [Market Details: liquidity reward settings](https://docs.polymarket.com/market-data/market-details#liquidity-reward-settings); CLOB `GET /rewards/markets/current` (paged, 18,600 markets on 30 Sep 2026); Gamma `/markets?condition_ids=` (20 per call; `closed=true` for resolved markets); help centre [Liquidity Rewards](https://help.polymarket.com/en/articles/13364466-liquidity-rewards) (payout at ~midnight UTC, $1 daily minimum, two-sided below 10c); reports in `data/ladder_snapshots_2026-09-30.txt`, `data/rewards_survey_2026-09-30.txt`, `data/rewards_survey_tiers_2026-09-30.txt`, `data/rewards_pocket_2026-09-30.txt`.
* Section 18: Polymarket docs [Wallets and Authentication](https://docs.polymarket.com/trading/wallets-auth) (Deposit Wallets, Relayer API keys, `SecureClient.create`), [Place Orders](https://docs.polymarket.com/trading/place-orders) (post-only limit orders), [Manage Orders](https://docs.polymarket.com/trading/manage-orders), [Deposit](https://docs.polymarket.com/trading/bridge/deposit) (USDC on Polygon wrapped to pUSD), [Python SDK](https://docs.polymarket.com/getting-started/python) (`polymarket-client` 0.11); CLOB `GET /rewards/markets/{condition_id}` (`market_competitiveness`), orders-scoring and user-earnings endpoints via the SDK.
* Section 19: Gamma `/series?slug=` and `/events?series_id=&closed=` for the 44 recurring series (events saved 1 Oct 2026; the trimmed offline fixture is `tests/fixtures/counts_events.json`); market rule texts quoted from the events' descriptions. Upstream feeds named: [USGS FDSN event web service](https://earthquake.usgs.gov/fdsnws/event/1/) (`query?format=csv&starttime=&minmagnitude=`), [IMF PortWatch](https://portwatch.imf.org/) (daily chokepoint transit calls), [SPC storm reports](https://www.spc.noaa.gov/climo/reports/) (`YYMMDD_rpts_torn.csv`) and the [NCEI tornado time series](https://www.ncei.noaa.gov/access/monitoring/tornadoes/time-series), [Wikimedia pageviews API](https://wikimedia.org/api/rest_v1/#/Pageviews%20data) and Mestyán, Yasseri, Kertész (2013), [Early Prediction of Movie Box Office Success Based on Wikipedia Activity Big Data](https://doi.org/10.1371/journal.pone.0071226), [Copernicus Climate Pulse](https://pulse.climate.copernicus.eu/) (ERA5 daily global temperature) and the [GISTEMP table](https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.txt), [USDA AMS Egg Markets Overview](https://www.ams.usda.gov/market-news/egg-market-news-reports) and [FRED APU0000708111](https://fred.stlouisfed.org/series/APU0000708111), [CDC FluView / FluSurv-NET](https://www.cdc.gov/fluview/index.html), [NHSN hospital respiratory data](https://data.cdc.gov/) and [WastewaterSCAN](https://data.wastewaterscan.org/), [SEC EDGAR full-text search](https://efts.sec.gov/LATEST/search-index) (424B4 filings) and [Jay Ritter's IPO data](https://site.warrington.ufl.edu/ritter/ipo-data/), [Factbase](https://factba.se/) (White House daily guidance archive), xtracker.polymarket.com "Export Data" (per tracked account). Public Truth Social archive checked and found to have a gap from October 2025 to April 2026: [stiles/trump-truth-social-archive](https://github.com/stiles/trump-truth-social-archive). Headroom report in `data/headroom_2026-10-01.txt`. Section 19e: per-market `closedTime` / `umaEndDate` from the same Gamma events (the early NO resolutions of count brackets), trade tapes from data-api `/trades`; reports and re-scorable rows in `data/crossings_2026-10-01/`.
