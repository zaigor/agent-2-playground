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

# offline demo on recorded fixtures (no network needed)
python -m pm_scanner scan --fixtures tests/fixtures --budget 50

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

`watch` and `summarize` are the cheap way to answer "is there anything here?"
before any money moves. Nothing in this package calls an LLM; it runs fine on
a laptop or a small VPS and costs nothing to leave running.

## Fee models (change them when the venues do)

* Polymarket, Fee Structure V2 (2026): taker fee per share = `rate * p * (1-p)`;
  makers pay nothing. Rate by category: crypto 0.07, sports 0.05,
  finance/politics/mentions/tech 0.04, economics/culture/weather/other 0.05,
  geopolitics/world events 0. The rate is inferred from Gamma tags and the
  *highest* matching rate wins, so the edge is never overstated. Override with
  `--poly-fee-rate`.
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
  cli.py         `scan`, `watch`, `summarize` and `fee` commands
tests/           pytest suite running entirely on fixtures
```

Every opportunity also carries the venue's minimum order size for its legs
(Polymarket exposes it per market, usually 5 shares) and an `executable` flag
that is false when the budget- and depth-limited size is below that minimum.

## Not included on purpose

No order placement. An executor needs funded accounts, API credentials held as
environment secrets (never in this repo), egress to the trading endpoints, and
a decision from the account owner after reading `MEMO.md`. The `.env.example`
lists the secrets it would take.
