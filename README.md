# pm-scanner

Read-only, fee-aware opportunity scanner for **Polymarket** and **Kalshi**.
It pulls public market data, prices every candidate *net of taker fees*, and
sizes the achievable profit against a cash budget so the number you see is the
number a small account could actually make, not a headline spread.

Start with [`MEMO.md`](MEMO.md): it is the honest assessment of what a $50
stake can and cannot do, and what is needed before anything trades.

## What it looks for

| kind | where | idea |
| --- | --- | --- |
| `negrisk_buy_all_yes` | Polymarket | one-winner event where the YES asks sum to less than $1 net of fees |
| `negrisk_buy_all_no` | Polymarket | same event, NO asks sum to less than $N-1 (robust to "no listed outcome wins") |
| `kalshi_buy_all_yes/no` | Kalshi | the same two structures on `mutually_exclusive` events |
| `cross_platform` | both | same question on both venues; buy YES on one and NO on the other under $1 |
| `near_certain` | Polymarket | outcomes at 95c+ resolving within days. **Not** arbitrage; resolution risk |

There is deliberately no single-market YES+NO check: on both venues a binary
market is one order book, so a NO ask at *p* is a YES bid at *1-p* and the two
best asks always sum to at least $1.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# offline demo on recorded fixtures (no network needed); --max-days 0 lifts the
# default 30-day payout horizon, which the fixture events sit outside of
python -m pm_scanner scan --fixtures tests/fixtures --budget 50 --max-days 0

# one live scan (needs egress to gamma-api.polymarket.com and clob.polymarket.com;
# add api.elections.kalshi.com for --platform both)
python -m pm_scanner scan --platform polymarket --budget 50 --min-edge 0.005 --json scan-$(date +%F).json
python -m pm_scanner scan --platform polymarket --poly-fee-rate 0   # pretend fee-free
python -m pm_scanner fee --platform kalshi --price 0.42 --shares 1  # fee for one order

# paper trading: re-scan every 60s, log every appearance/disappearance, run for days
python -m pm_scanner watch --platform polymarket --budget 50 --interval 60 --log watch.jsonl
#   optional push alerts: export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python -m pm_scanner summarize watch.jsonl     # rates, sizes, lifetimes by kind

pytest -q
```

### Knesset election model (`israel`)

The 27 Oct 2026 election has ~110 Polymarket markets (seat brackets, vote-share
brackets, threshold and most-seats questions) with 10-40c spreads and a few
hundred dollars at the touch. `israel` turns published polls into a seat
distribution and prices every bracket against it:

```bash
# polls live in data/israel_polls_2026.csv (seats per party per poll; see the header)
python -m pm_scanner israel --budget 500                       # live Gamma + CLOB
python -m pm_scanner israel --fixtures tests/fixtures --all    # offline, on the 29 Sep snapshot
python -m pm_scanner israel --sd utj=0.6,shas=0.9 --bloc-bias 0.01 --surplus yashar:dems,together:yb
```

Pipeline: recency- and pollster-weighted vote shares -> Monte Carlo with
independent, bloc-correlated and Arab-turnout error -> 3.25% threshold and
Bader-Ofer allocation with surplus-vote pairs -> P(bracket) for each market ->
edge net of the 4% politics taker fee, quarter-Kelly stake capped at 10% of
budget and at top-of-book depth, plus fee-free resting quotes 6c inside the
model. Nothing under 5c a side is ever recommended. The shipped CSV contains
PLACEHOLDER rows; the tool shouts until they are replaced with real polls.

### Niche survey (`niches`)

Which market families recur every day or week, how deep they are, what they
cost in fees, and how well their prices were calibrated a day before
resolution. Families are Gamma recurring series (`nyc-daily-weather`,
`box-office-openings`, `elon-tweets`, ...) or, for one-off titles, the title
with dates and numbers normalised:

```bash
python -m pm_scanner niches --days 90 --sort steady            # live; ~10 min for the default 16 tags
python -m pm_scanner niches --tags weather,box-office --days 60 --json niches.json
python -m pm_scanner niches --fixtures tests/fixtures --days 400 --min-events 3   # offline sample
```

The calibration column needs no price-history calls: Gamma freezes
`lastTradePrice` and `oneDayPriceChange` when a market closes, so their
difference is the price one day before the close (checked against CLOB history
on 1,020 weather markets: correlation 0.90, median gap 1.5c). `skill` is the
Brier skill of that price; a negative `LSbias`/`midbias` means 3-20c / 30-70c
contracts hit less often than they were priced. The 29 Sep 2026 survey and its
conclusions are in MEMO.md section 10.

### Daily temperature markets (`weather`)

52 cities, ~1,000 bracket markets a day, each resolving on a named airport
station's hourly readings. Three modes:

```bash
# 1. is the mispricing still there? price of every bracket at local midnight of the target day,
#    from data-api trade history (kept for months), 25 sampled event-days per month
python -m pm_scanner weather --mode trend --cities all --days 270 --events-per-month 25

# 2. do previous-day forecasts beat those prices? Open-Meteo previous-runs (ECMWF, GFS, ICON),
#    rolling per-station bias/sd, Brier vs the market with standard errors, a reliability
#    table, paper P&L net of the 5% fee, all per station and pooled
python -m pm_scanner weather --mode backtest --cities nyc,london,tel-aviv --days 45
#    ... with the AI models too and the station bias fitted on the last 21 days only
python -m pm_scanner weather --mode backtest --cities nyc,london,tel-aviv --days 45 --calib-window 21 \
    --models ecmwf_ifs025,ecmwf_aifs025_single,gfs_graphcast025,icon_seamless,gfs_seamless

# 2b. the intraday leg: at 11:00, 13:00 and 15:00 local, the station's running maximum (IEM
#     METAR archive) plus the day-ahead blend, scored against the price 5 minutes later and
#     marked to market 30 minutes after that (did the price move toward the model?)
python -m pm_scanner weather --mode intraday --cities nyc,london,tel-aviv --days 45 --calib-window 21 \
    --models icon_seamless,gfs_seamless,ecmwf_ifs025 --hours 11,13,15 --latency-min 5

# 2c. the maker leg from the trade histories alone: every trade seen from the taker's side, marked
#     to expiry and 30 min later, by station, local time of day and price band. Negative taker
#     markout = takers lose = the makers who filled them earned it (before liquidity rebates).
python -m pm_scanner weather --mode flow --cities nyc,london,tel-aviv --days 45

# 3. today's forecast distribution vs the open books: takers with edge, then resting quotes
python -m pm_scanner weather --mode today --cities nyc,london,tel-aviv --budget 300 --ensemble

python -m pm_scanner weather --mode trend --fixtures tests/fixtures --days 400   # offline sample
```

Trade histories are cached under `.cache/pm_trades/` (one request per market,
about 0.7s each; a 30-day, 3-city backtest is ~1,000 requests the first time
and instant afterwards). Open-Meteo needs no key. Coordinates for every
station are in `weather.py`; fix one with `--station nyc=40.78,-73.87`. The
forecast is the mean of the requested models plus a per-station bias, with an
error sd fitted on the days already seen (all of them, or the last N with
`--calib-window N`; the prior 1.8°C / 3.2°F applies until 8 days exist). The
settled value is an integer, so the fitted residuals carry rounding noise;
that is removed before the sd is used, with a floor of 0.3 degrees. Each
bracket's probability is the normal mass on `[lo-0.5, hi+0.5)` because the
rules round to whole degrees.

The intraday mode reads routine hourly METARs from the Iowa Environmental
Mesonet archive (`mesonet.agron.iastate.edu`, free, cached under
`.cache/pm_obs/`; add `--include-specials` to count SPECI reports). Brackets
below the value already reached are priced at zero, the bracket holding it
collects the mass of the remaining-hours forecast below its upper edge, and a
per-hour regression of (final − running max) on (forecast − running max)
supplies that forecast's mean and spread.

`watch` and `summarize` are the cheap way to answer "is there anything here?"
before any money moves. Nothing in this package calls an LLM; it runs fine on
a laptop or a small VPS and costs nothing to leave running.

## Order flow by family (`flow`)

The question a small trader should ask first: where are the takers noise, so
that a resting quote gets paid rather than picked off? This needs only the
trade tape and the outcome, both on Polymarket's own API.

```bash
# every recurring family with >=10 resolved markets in the last 45 days, the 60 biggest by
# volume, 30 markets sampled each: taker markout to expiry (positive = takers informed),
# the makers' edge per $100 filled before rebates, by hours-to-close and by price band
python -m pm_scanner flow --days 45 --per-family 30 --max-families 60 --json flow.json

python -m pm_scanner flow --no-sport --sort volume     # non-sport families, biggest first
python -m pm_scanner flow --fixtures tests/fixtures --days 400 --min-markets 1   # offline sample
```

Each sampled market is one trade-history request (cached under
`.cache/pm_trades/`), so 60 families take about 30 minutes the first time.
Closed events are paged from Gamma in 3-day windows because the `/events`
offset cap is now about 2,000. `side` in the trade feed is read as the
taker's side. The weather-specific version (`weather --mode flow`) buckets
by local time of the target day instead of hours to close.

## Ladders, liquidity rewards, and your own signal

Three more read-only commands, added when the research was being closed
(memo section 17):

```bash
# ladder consistency: nested outcomes ("above 74k / 76k / 78k", "by Sep 30 / Oct 31",
# "continues through ...", O/U lines, handicaps) must be priced monotonically. Candidates
# from Gamma's top of book, confirmed on CLOB books, net of both taker fees, sized to the
# budget, with days to resolution and the annualised return of the locked capital
python -m pm_scanner ladder --max-events 6000 --budget 100
python -m pm_scanner ladder --repeat 6 --interval 600 --log ladder.jsonl   # six snapshots, ten minutes apart
python -m pm_scanner ladder --fixtures tests/fixtures                       # offline

# liquidity rewards: for 90 rewarded markets (the 30 biggest pots plus random $10-100 and
# $1-10 ones) the share of the daily pot a two-sided quote at the minimum size would earn
# against the live book, and what the same quote loses on the market's last 14 days of tape
python -m pm_scanner rewards --days 14 --json rewards.json
# ... and, without tapes, every rewarded market's book at $10+/day: where a minimum-size quote at
# half the max spread would take a quarter or more of the pot (the "unclaimed pot" list)
python -m pm_scanner rewards --books-only --min-rate 10 --min-days 7 --json pocket.json

# your own probabilities: a CSV of market,time,p is scored against the price at that time
# (Brier vs the market, a 50/50 blend, markout, paper P&L after fees, reliability), with
# market-clustered errors and look-ahead rows dropped
python -m pm_scanner signal --csv data/signal_example.csv --fixtures tests/fixtures   # offline example
python -m pm_scanner signal --csv my_signal.csv --horizon-min 30 --edge 0.05 --json out.json
```

The signal CSV needs `market` (condition id `0x...`, market slug, or numeric
Gamma id), `time` (ISO 8601, UTC unless an offset is given, or unix seconds:
the moment the number was actually available to you, not the moment the data
refers to) and `p` (your probability that the first outcome, usually Yes, wins);
`outcome` says which outcome `p` refers to when it is not the first one, and
`note` is carried through. Any resolved Polymarket market with a trade
history can be scored; the report says how big a Brier gap the sample could
have detected (two standard errors), which is the number to compare a small
file against.

### Count windows: `counts` and `headroom` (memo section 19)

Dozens of recurring series are "how many X between A and B" ladders: posts on X and Truth
Social per week (seven accounts), 6.5+ and 5.5+ earthquakes per week, ships through Hormuz and
Bab el-Mandeb per week, tornadoes per month. `counts` turns a catalog of timestamped
occurrences into the `market,time,p` CSV that `signal` scores, with a pre-registered model: the
count so far is exact, the remainder is negative-binomial with the mean and dispersion of the
previous eight windows, spread over the remaining time by those windows' phase profile.

```
# USGS catalog of every 5.5+ quake since 2024 (one request, on your laptop):
#   https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv&starttime=2024-01-01&minmagnitude=5.5
python -m pm_scanner counts --series 6pt5-earthquake-weekly --catalog quakes.csv --value-col mag --min-value 6.5 --out quakes_signal.csv
python -m pm_scanner signal --csv quakes_signal.csv

# IMF PortWatch daily chokepoint counts (one row per day): the count column instead of one row per ship
python -m pm_scanner counts --series ships-transit-the-strait-of-hormuz --catalog hormuz.csv --count-col n_total --out hormuz_signal.csv

# posts: pull an account's whole counted history from xtracker.polymarket.com (the counter the
# ladders resolve on; its JSON routes are read off the site's own code, see pm_scanner/xtracker.py)
python -m pm_scanner xtracker --list                      # the tracked accounts and their handles
python -m pm_scanner xtracker elonmusk                    # -> xtracker/elonmusk.csv, one row per post, UTC
python -m pm_scanner counts --series elon-tweets --catalog xtracker/elonmusk.csv --check-only     # does the catalog land in the winning bracket?
python -m pm_scanner counts --series elon-tweets --catalog xtracker/elonmusk.csv --out elon_signal.csv
python -m pm_scanner signal --csv elon_signal.csv --fill-wait 6
```

`--catalog` also takes a folder or glob of the site's per-window "Posts" downloads (overlapping
windows are deduplicated by post id, and `Posted At (EST)` is read as New York time unless
`--tz` says otherwise); `xtracker --compare <that folder>` lines the two up on the posts they share.

`--check-only` compares the catalog's count over each resolved window with the winning
bracket first: a mismatch means the catalog is not what the tracker counts (replies, reposts,
magnitude revisions, time zone) and the backtest would be meaningless. On 3 Oct 2026 the X
accounts matched every resolved window (Musk 94 of 94, White House 54, Cruz 54, NYC mayor 52);
Trump's Truth Social catalog missed 17 of 65 because the tracker's scraper has gaps (memo 19f).
With the real count, the Musk model was not sharper than the market: market minus blend
-0.0002 ± 0.0003, re-executed P&L +3.71 ± 2.16 per $100, both under the pre-registered bar
(memo 19g).

`headroom` needs no outside data. It scores the market's own price at the start, middle and
late part of each window against the outcome, next to a uniform 1/k and a point-in-time
climatology of how often each bracket label has won in the series. A positive blend gap means
the crowd misprices the base rate and a catalog model has room; a market already at a tiny
Brier at the window start leaves room only for the live count.

```
python -m pm_scanner headroom --series trump-truth-social,6pt5-earthquake-weekly,ships-transit-the-strait-of-hormuz --per-family 12
```

### Testing the count model against outcomes with no outside data: `crossings` (memo section 19e)

Polymarket closes a count bracket the moment the running count passes its
ceiling ("<20" on Monday, "20-39" on Tuesday, ...), each a NO resolved by UMA
while the window is still open. Those close timestamps are a public staircase
lower bound on the count through every resolved posts window, so the model in
`counts` can be scored against outcomes without the tracker's export: at each
early close the count is at least the ceiling plus one, the remainder is the
trailing windows' mean rate times the fraction of the window left, and every
bracket still open gets a probability that `signal` scores against the price
at that moment and the resolution.

```
python -m pm_scanner crossings --series trump-truth-social,ted-cruz-daily-tweets --out crossings_signal.csv
python -m pm_scanner crossings --events-dir dumps/ --max-stale 72 --json crossings.json   # offline event dumps, looser price staleness
```

The report is the `signal` block plus event-clustered tables by series and by
window phase, every paper trade re-executed at the first print after the
decision time on the side we would have needed (`--fill-wait`, the honest
price in a thin ladder), and the hours between the price collapse of each
crossed bracket and its close (how stale the lower bound is). `--timing close`
dates a crossing by the UMA close (certain, one to three days stale);
`--timing collapse` by the bracket's price collapse (fresher, not certain).
`--count interval` spreads the count up to the ceiling of the lowest bracket
still alive. The count is a lower bound and there is no phase profile, so the
test is biased against the model. Earthquake, ship and weather brackets close
only after the window, so this covers the posts families. Results and the
re-scorable rows: `data/crossings_2026-10-01/`; the pre-registered close-dated
test fails, the collapse-dated one shows a paper P&L of about +11 per $100
that survives the fill check, in the thin ladders and in Musk's weekly series
(memo 19e has the caveats).

## The one live command: `lp` (memo section 18)

Everything above is read-only. `lp` is the test rig for the liquidity-reward
question in memo section 17b: it rests a two-sided, minimum-size, post-only
quote at half the max reward spread in a few rewarded markets that nobody
else quotes, keeps it centred, and reads back from the CLOB whether the
orders are scoring and what they earned that day. It never sends a market
order, never crosses the spread, never adds to a side that has been filled,
pulls quotes 48 hours before a market ends, and cancels everything on exit.

```bash
pip install -e ".[trade]"                       # the official polymarket-client SDK
python -m pm_scanner lp                         # dry run: the plan, priced from public books, nothing sent
python -m pm_scanner lp --check                 # with POLY_* set: wallet type, balance, approvals, the plan
python -m pm_scanner lp --smoke --live --only <condition id>          # one market, two hours, then cancel: does the CLOB score it?
python -m pm_scanner lp --live --budget 50 --markets 3 --only <ids> --hours 72 --log lp.jsonl
python -m pm_scanner lp --check --markets 1 --budget 60 --min-reward 0.5 --min-depth 200   # section 18e: one deep calm book
# three unattended days on a laptop: a restart loop that still ends on time
MAX_BUDGET_USD=29 systemd-inhibit --what=sleep --why=lp bash -c 'until python -m pm_scanner lp --live --markets 1 --budget 25 --only <id> --until 2026-10-07T10:00 --log lp.jsonl; do sleep 300; done'
python -m pm_scanner lp --earnings 2026-10-01   # the day's reward accrual per market
python -m pm_scanner lp --cancel-all
python -m pm_scanner lp --positions             # what the account holds, priced by the bids, not by the site's midpoint
python -m pm_scanner lp --merge <condition id> [--live]   # both sides held: turn the pairs back into $1 each, no price, no fee
```

The plan's `exit $` column is what one full fill on the worse side would lose if
sold straight back into what the book keeps below that quote, fee included (a
fill means every order at or above our price was taken first); candidates above
`--max-exit` ($2 by default), or whose book cannot absorb the quote at all, are
dropped, and a live run refuses such a plan even when named with `--only`.
The `age` and `mv/d` columns come from the CLOB's week of 10-minute prices:
days of history (7.0 is a full week; a younger market is still finding its
price) and moves of 3 cents or more per day over it, each one a move that could
have filled a quote 3 cents from the mid. Candidates under `--min-age` (6.5
days) or over `--max-moves` (2 a day) are dropped and refused live the same way
(4 Oct: two one-day-old markets had moved 45c and 20c since listing; one filled
within ninety minutes). `inside` is the score-weighted size other people
already rest inside the max spread on the thinner side; `--min-depth N` (off by
default) asks for the opposite of the unquoted pocket, the deep calm books of
memo section 18e where the mid is real and a fill is cheap to undo, and a live
run asked for depth refuses a thinner book. Every `fill` line in the log carries `sell_now` and
`loss_if_sold_now` read from the book at that second. At start the rig cancels any order a dead run left
resting and reads the account's positions, so a side filled before a crash is
not quoted again. Once one side is held, the other is capped so the pair never
costs more than $1 (a YES bid at most 1 − NO price − tick, and the reverse);
the quotes follow the others' mid only after it has read a tick or more away
for `--recentre-confirm` consecutive checks (3), because in a book of a few
20-share orders the mid is whoever last placed one (4 Oct: sold YES at 0.49,
mid read 11c higher a minute later, bought YES at 0.55, a loss locked by
construction). What a position is worth is what the bids pay
(`--positions`); the site's midpoint mark is not money. The abort rules of
memo section 18e run in the rig itself: `--abort-fills 2` (a second fill in
one market), `--abort-loss 2` (a fill that would lose over $2 if sold straight
back) and `--max-refusal-hours 1` (a restart loop refused by the live gate for
an hour, each refusal written to the log) each cancel every quote, write an
`abort` line and end the run with exit 0, so an `until` loop around it stops
too. The positions are left for `--positions`; a new run is a decision, not a
retry. A cap is moved in the command, never in code, and 0 turns one off.

Credentials come from the environment only (`.env.example`): the signer key
of a fresh wallet, the Polymarket account wallet address and a Relayer API
key from polymarket.com settings. `--budget` above `MAX_BUDGET_USD` is
refused (`--cancel-all`, `--earnings` and `--approve` size no quote and do not
read the budget).

### Did a re-quoting rule cause or avoid a fill? `requote`

```
python -m pm_scanner requote --only <condition ids> --days 7 --fill at        # the week, five-minute mids
python -m pm_scanner requote --only <id> --days 1 --fill at --detail          # the last day, one-minute mids, every fill marked
```

Replays the rig's quoting rule (`rig`: re-centre after three readings a tick
away), `follow` (every reading), `escape` (re-centre at once when the mid comes
within one or two ticks of a quote), `wide` (a tick further out) and the rig
rule with a half-tick drift counting as away, over a market's public minute
history and trade tape, at the reward minimum size. Per policy: fills, how
many had warning at the reading before (the only kind a reading-based rule can
escape), what undoing each would cost against the mid 10 and 60 minutes later,
re-centres, and the reward score kept against a quote re-centred every reading.
Read-only, public data; the counts are ceilings (the tape is prints, not our
queue position) and the losses are mid marks, not the `--positions` reading.
Memo section 20, 4 Oct: the 19:56 UTC fill came from a one-second 3,192-share
print with no approach to escape from; the half-tick drift cost the rig a
sixth of its score.

## Fee models (change them when the venues do)

* Polymarket, Fee Structure V2 (2026): taker fee per share = `rate * p * (1-p)`;
  makers pay nothing. Gamma now publishes the schedule per market
  (`feeSchedule.rate`) and that is used first; observed on 29 Sep 2026: crypto
  0.07, finance/politics/mentions/tech 0.04, economics/culture/weather 0.05,
  sports and esports 0.03, and a minority of geopolitics markets with no fee.
  When a market has no schedule the rate falls back to the Gamma tag table in
  `fees.py`, where the *highest* matching rate wins so the edge is never
  overstated. Override with `--poly-fee-rate`.
* Kalshi: taker fee = `ceil(0.07 * m * contracts * p * (1-p))` rounded up to the
  cent per order; maker fee is 25% of that. `--kalshi-fee-multiplier` sets `m`.

## Layout

```
pm_scanner/
  http.py        requests wrapper (retries, timeouts, proxy/CA from env)
  fees.py        fee formulas and Polymarket tag -> rate mapping
  polymarket.py  Gamma + CLOB read-only client and parsers
  kalshi.py      Kalshi public API client and parsers
  scans.py       detectors; every Opportunity is net of fees and budget-sized
  sources.py     LiveSource (APIs) and FixtureSource (recorded JSON)
  report.py      text table + JSON output
  watch.py       polling loop, JSONL log, Telegram alerts, log summarizer
  israel.py      Knesset polls -> seat simulation -> bracket probabilities -> edges
  niches.py      recurring-family survey: cadence, depth, fees, day-before calibration
  flow.py        order-flow screen: taker markouts and makers' edge per family, hours-to-close and price band; paper maker on the tape
  ladder.py      ladder consistency: nested outcomes by number or date, violations net of fees, confirmed on books
  rewards.py     liquidity rewards: reward share of a small quote against the live book vs its adverse selection on the tape
  signal.py      generic backtest of a CSV of your own probabilities against the price at that time and the outcome
  crossings.py   bracket-crossing test: early bracket closes as a timestamped count lower bound, scored against outcomes with `signal`
  xtracker.py    xtracker.polymarket.com's JSON routes: an account's whole counted-post history as a catalog CSV
  counts.py      count-window ladders: windows and brackets from the Gamma wording, catalog -> negative-binomial probabilities -> signal CSV; headroom of the market itself
  lp.py          the liquidity-reward test rig: minimum-size post-only quotes in unquoted rewarded markets, scoring and earnings read-back
  requote.py     re-quote policies (the rig's, follow, escape, wide, half-tick) replayed on a market's minute history and tape: fills, warning, undo cost, score kept
  history.py     the CLOB's week of prices per market, measured: age in days and 3-cent moves per day (the chooser's restlessness filter)
  weather.py     temperature brackets: stations, trade-history prices, Open-Meteo forecasts, IEM observations, trend/backtest/intraday/flow/today
  cli.py         `scan`, `watch`, `summarize`, `israel`, `niches`, `weather`, `flow`, `ladder`, `rewards`, `signal`, `lp` and `fee` commands
data/            israel_polls_2026.csv (hand-maintained poll table), saved reports, signal_example.csv
tests/           pytest suite running entirely on fixtures (incl. a 29 Sep 2026
                 snapshot of the Israel election markets and their order books,
                 a sample of weather / box-office / tweet-count events, and three
                 resolved temperature events with their trade histories)
```

Every opportunity also carries the venue's minimum order size for its legs
(Polymarket exposes it per market, usually 5 shares) and an `executable` flag
that is false when the budget- and depth-limited size is below that minimum.

## Not included on purpose

No trading strategy executes here. The only command that sends orders is
`lp`, the liquidity-reward test rig above, capped by `MAX_BUDGET_USD`, and it
exists because the account owner decided to run that test after reading
`MEMO.md` section 17. Credentials live in the environment, never in this
repository, and the wallet holds only the test budget.
