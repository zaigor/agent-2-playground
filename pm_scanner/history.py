"""How restless a market's price has been: the CLOB's week of price history, measured.

4 Oct 2026: the chooser read a market's pot, book and days to resolution, and picked two
markets created the day before, whose prices had moved 45c and 20c in their first day.
One filled within ninety minutes and cost $3.87 to undo. The pot was the bait for price
discovery. This module reads what the chooser could not see: how long the market has
been trading and how often its price jumped by a quote's width over the past week.

    GET https://clob.polymarket.com/prices-history?market=<token>&interval=1w&fidelity=10

returns up to a week of 10-minute midpoints (about 1,000 points for a market a week old,
fewer for a younger one). `measure_week` turns them into an age in days and a count of
moves of `band` or more per day; `choose_markets` and the live gate read both.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .http import HttpClient, HttpError
from .polymarket import CLOB_URL

Point = tuple[int, float]  # unix seconds, YES price


@dataclass(frozen=True)
class PriceWeek:
    days: float  # span of the history, at most about 7
    points: int
    moves: int  # steps of `band` or more between consecutive samples
    path: float  # sum of |price change| over the span
    low: float
    high: float
    band: float

    @property
    def moves_per_day(self) -> float:
        return self.moves / self.days if self.days > 0 else float("inf")

    @property
    def path_per_day(self) -> float:
        return self.path / self.days if self.days > 0 else float("inf")

    def to_dict(self) -> dict[str, Any]:
        return {"days": round(self.days, 2), "points": self.points, "moves": self.moves, "moves_per_day": round(self.moves_per_day, 2),
                "path": round(self.path, 3), "path_per_day": round(self.path_per_day, 3), "low": self.low, "high": self.high, "band": self.band}


def measure_week(points: list[Point], *, band: float = 0.03) -> PriceWeek | None:
    """Age and restlessness from a price series; None when there are fewer than two points."""
    pts = sorted((int(t), float(p)) for t, p in points if p is not None)
    if len(pts) < 2:
        return None
    prices = [p for _, p in pts]
    steps = [abs(b - a) for a, b in zip(prices, prices[1:])]
    days = (pts[-1][0] - pts[0][0]) / 86400.0
    return PriceWeek(days=days, points=len(pts), moves=sum(1 for s in steps if s >= band - 1e-9), path=sum(steps),
                     low=min(prices), high=max(prices), band=band)


def fetch_price_week(http: HttpClient, token: str, *, fidelity_min: int = 10) -> list[Point]:
    data = http.get_json(f"{CLOB_URL}/prices-history", {"market": token, "interval": "1w", "fidelity": fidelity_min})
    out: list[Point] = []
    for x in (data or {}).get("history") or []:
        try:
            out.append((int(x["t"]), float(x["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def week_fetcher(http: HttpClient, *, band: float = 0.03, fidelity_min: int = 10) -> Callable[[str], PriceWeek | None]:
    """A cached `token -> PriceWeek | None` for the chooser; a failed request reads as None
    (no history), which the chooser skips and the live gate refuses."""
    cache: dict[str, PriceWeek | None] = {}

    def get(token: str) -> PriceWeek | None:
        if token not in cache:
            try:
                cache[token] = measure_week(fetch_price_week(http, token, fidelity_min=fidelity_min), band=band)
            except (HttpError, ValueError, TypeError):
                cache[token] = None
        return cache[token]

    return get
