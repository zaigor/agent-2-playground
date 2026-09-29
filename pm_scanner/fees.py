"""Fee models for Polymarket (Fee Structure V2, 2026) and Kalshi.

Polymarket (global site), effective 2026-03-30 with the July 2026 sports change:
    taker fee per share = rate * price * (1 - price)
    makers pay 0 and receive rebates funded by taker fees.
    rate by category: crypto 0.07, sports 0.03, finance/politics/mentions/tech 0.04,
    economics/culture/weather/other 0.05, geopolitics/world events 0.00.
    (Polymarket US, a separate CFTC exchange, charges a flat 0.05 taker.)

    Since 2026 Gamma publishes the schedule per market (`feeSchedule.rate`);
    `polymarket_rate_for_event` uses that first and this table only as a fallback.
    On 29 Sep 2026 the published sports rate was 0.03 and many "geopolitics" events
    carried the 0.04 politics or 0.05 culture schedule, so trust the market field.

Kalshi (fee schedule filed with the CFTC):
    taker fee = ceil(0.07 * multiplier * contracts * P * (1 - P) * 100) / 100
    maker fee = 25% of the taker rate (0.0175), same shape.
    The ceil() rounds each order up to the next cent, so tiny orders pay
    proportionally more than the formula suggests.

These numbers move. Override them from the CLI when they do.
"""
from __future__ import annotations

import math
from collections.abc import Iterable

POLYMARKET_TAG_RATES: dict[str, float] = {
    # crypto
    "crypto": 0.07,
    "bitcoin": 0.07,
    "ethereum": 0.07,
    "solana": 0.07,
    # sports and esports (Gamma `sports_fees_v2` / `v3` schedules show 0.03 on 29 Sep 2026)
    "sports": 0.03,
    "nfl": 0.03,
    "nba": 0.03,
    "mlb": 0.03,
    "nhl": 0.03,
    "soccer": 0.03,
    "football": 0.03,
    "tennis": 0.03,
    "mma": 0.03,
    "ufc": 0.03,
    "esports": 0.03,
    # finance / politics / mentions / tech
    "finance": 0.04,
    "business": 0.04,
    "stocks": 0.04,
    "politics": 0.04,
    "elections": 0.04,
    "us-politics": 0.04,
    "mentions": 0.04,
    "mention-markets": 0.04,
    "tech": 0.04,
    "science": 0.04,
    "ai": 0.04,
    # economics / culture / weather / other
    "economics": 0.05,
    "economy": 0.05,
    "fed": 0.05,
    "inflation": 0.05,
    "culture": 0.05,
    "pop-culture": 0.05,
    "entertainment": 0.05,
    "movies": 0.05,
    "music": 0.05,
    "weather": 0.05,
    "climate": 0.05,
    # geopolitics / world events are fee-free
    "geopolitics": 0.0,
    "world": 0.0,
    "world-events": 0.0,
    "middle-east": 0.0,
    "ukraine": 0.0,
    "russia": 0.0,
    "israel": 0.0,
    "iran": 0.0,
    "china": 0.0,
}

# When we cannot classify an event we assume the mid-range paid rate so the
# reported edge is conservative (never overstated).
DEFAULT_POLYMARKET_RATE = 0.05

KALSHI_TAKER_COEFF = 0.07
KALSHI_MAKER_COEFF = 0.0175


def polymarket_rate_for_tags(tags: Iterable[str], default: float = DEFAULT_POLYMARKET_RATE) -> float:
    """Pick the fee rate for an event from its Gamma tag slugs.

    If several tags match we take the *highest* rate. A geopolitics event that is
    also tagged "politics" will therefore be costed at 0.04 rather than 0.00;
    that undercounts edge, which is the safe direction for a scanner.
    """
    matched = [POLYMARKET_TAG_RATES[t.lower()] for t in tags if t and t.lower() in POLYMARKET_TAG_RATES]
    if not matched:
        return default
    return max(matched)


def polymarket_rate_for_event(event, override: float | None = None, default: float = DEFAULT_POLYMARKET_RATE) -> float:
    """Taker rate for an event: the override, else Gamma's own `feeSchedule.rate` on its
    markets (the highest if they differ), else the tag table above.

    Gamma started publishing the schedule per market in 2026; it is authoritative and
    catches cases the tag table gets wrong (sports are 0.03, and many "geopolitics"
    events actually carry the 0.04 politics or 0.05 culture schedule).
    """
    if override is not None:
        return override
    rates = [m.fee_rate for m in getattr(event, "markets", []) if getattr(m, "fee_rate", None) is not None]
    if rates:
        return max(rates)
    return polymarket_rate_for_tags(getattr(event, "tags", []), default)


def polymarket_taker_fee(price: float, shares: float, rate: float) -> float:
    """Taker fee in dollars for `shares` filled at `price` (0-1)."""
    if shares <= 0 or rate <= 0:
        return 0.0
    price = min(max(price, 0.0), 1.0)
    return rate * shares * price * (1.0 - price)


def kalshi_taker_fee(price: float, contracts: float, multiplier: float = 1.0, round_up: bool = True) -> float:
    """Kalshi taker fee in dollars. `price` in dollars (0-1), rounding up to the cent per order."""
    if contracts <= 0:
        return 0.0
    price = min(max(price, 0.0), 1.0)
    raw = KALSHI_TAKER_COEFF * multiplier * contracts * price * (1.0 - price)
    if not round_up:
        return raw
    return math.ceil(raw * 100 - 1e-9) / 100


def kalshi_maker_fee(price: float, contracts: float, multiplier: float = 1.0, round_up: bool = True) -> float:
    if contracts <= 0:
        return 0.0
    price = min(max(price, 0.0), 1.0)
    raw = KALSHI_MAKER_COEFF * multiplier * contracts * price * (1.0 - price)
    if not round_up:
        return raw
    return math.ceil(raw * 100 - 1e-9) / 100
