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

`watch` and `summarize` are the cheap way to answer "is there anything here?"
before any money moves. Nothing in this package calls an LLM; it runs fine on
a laptop or a small VPS and costs nothing to leave running.

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
  cli.py         `scan`, `watch`, `summarize`, `israel`, `niches` and `fee` commands
data/            israel_polls_2026.csv (hand-maintained poll table)
tests/           pytest suite running entirely on fixtures (incl. a 29 Sep 2026
                 snapshot of the Israel election markets and their order books,
                 and a sample of weather / box-office / tweet-count events)
```

Every opportunity also carries the venue's minimum order size for its legs
(Polymarket exposes it per market, usually 5 shares) and an `executable` flag
that is false when the budget- and depth-limited size is below that minimum.

## Not included on purpose

No order placement. An executor needs funded accounts, API credentials held as
environment secrets (never in this repo), egress to the trading endpoints, and
a decision from the account owner after reading `MEMO.md`. The `.env.example`
lists the secrets it would take.
