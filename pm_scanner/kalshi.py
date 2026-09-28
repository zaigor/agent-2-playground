"""Read-only Kalshi client (public endpoints, no auth needed).

GET https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000&cursor=..
    -> {"markets": [...], "cursor": ".."}. Prices come either as integer cents
    (`yes_bid`) or as dollar strings (`yes_bid_dollars`); we accept both.
GET https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook
    -> {"orderbook": {"yes": [[price_cents, qty], ...], "no": [[...], ...]}}
    Kalshi publishes *bids* per side only. A YES ask is a NO bid mirrored (1-p).
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .http import HttpClient
from .polymarket import Book, Level, _float, _levels, parse_dt

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SITE = "https://kalshi.com/markets"


def _price(d: dict[str, Any], cents_key: str) -> float | None:
    dollars = d.get(f"{cents_key}_dollars")
    if dollars not in (None, ""):
        f = _float(dollars)
        if f is not None:
            return f
    cents = d.get(cents_key)
    if cents in (None, ""):
        return None
    f = _float(cents)
    return None if f is None else f / 100.0


@dataclass
class KalshiMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    subtitle: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    close_time: datetime | None
    status: str
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str:
        # Kalshi's canonical URLs need the series slug; the event-ticker form
        # redirects correctly for most markets.
        return f"{KALSHI_SITE}/{self.series_ticker.lower()}/{self.event_ticker.lower()}" if self.series_ticker else KALSHI_SITE

    @property
    def full_title(self) -> str:
        return f"{self.title} {self.subtitle}".strip()


def parse_kalshi_market(d: dict[str, Any]) -> KalshiMarket:
    return KalshiMarket(
        ticker=str(d.get("ticker") or ""),
        event_ticker=str(d.get("event_ticker") or ""),
        series_ticker=str(d.get("series_ticker") or ""),
        title=str(d.get("title") or ""),
        subtitle=str(d.get("subtitle") or d.get("yes_sub_title") or ""),
        yes_bid=_price(d, "yes_bid"),
        yes_ask=_price(d, "yes_ask"),
        no_bid=_price(d, "no_bid"),
        no_ask=_price(d, "no_ask"),
        close_time=parse_dt(d.get("close_time") or d.get("expiration_time")),
        status=str(d.get("status") or ""),
        raw=d,
    )


def parse_kalshi_orderbook(ticker: str, data: dict[str, Any]) -> tuple[Book, Book]:
    ob = data.get("orderbook") if isinstance(data.get("orderbook"), dict) else data
    if "yes_dollars" in ob or "no_dollars" in ob:
        yes_bids = _levels(ob.get("yes_dollars"))
        no_bids = _levels(ob.get("no_dollars"))
    else:
        yes_bids = _levels(ob.get("yes"), cents=True)
        no_bids = _levels(ob.get("no"), cents=True)
    yes_book = Book(f"{ticker}:yes", bids=yes_bids, asks=[Level(round(1 - lv.price, 6), lv.size) for lv in no_bids])
    no_book = Book(f"{ticker}:no", bids=no_bids, asks=[Level(round(1 - lv.price, 6), lv.size) for lv in yes_bids])
    return yes_book, no_book


@dataclass
class KalshiEvent:
    event_ticker: str
    series_ticker: str
    title: str
    mutually_exclusive: bool
    category: str
    markets: list[KalshiMarket]

    @property
    def url(self) -> str:
        return f"{KALSHI_SITE}/{self.series_ticker.lower()}/{self.event_ticker.lower()}" if self.series_ticker else KALSHI_SITE


def parse_kalshi_event(d: dict[str, Any]) -> KalshiEvent:
    markets = [parse_kalshi_market(m) for m in d.get("markets") or [] if isinstance(m, dict)]
    return KalshiEvent(
        event_ticker=str(d.get("event_ticker") or ""),
        series_ticker=str(d.get("series_ticker") or ""),
        title=str(d.get("title") or ""),
        mutually_exclusive=bool(d.get("mutually_exclusive", False)),
        category=str(d.get("category") or ""),
        markets=markets,
    )


class KalshiClient:
    def __init__(self, http: HttpClient, base_url: str = KALSHI_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    def iter_markets(self, status: str = "open", limit: int = 1000, max_pages: int = 100) -> Iterator[KalshiMarket]:
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": limit, "status": status}
            if cursor:
                params["cursor"] = cursor
            data = self.http.get_json(f"{self.base_url}/markets", params=params)
            for item in (data or {}).get("markets") or []:
                if isinstance(item, dict):
                    yield parse_kalshi_market(item)
            cursor = (data or {}).get("cursor") or None
            if not cursor:
                return

    def iter_events(self, status: str = "open", limit: int = 200, max_pages: int = 200) -> Iterator[KalshiEvent]:
        """Events with nested markets; `mutually_exclusive` marks one-winner groups."""
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": limit, "status": status, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            data = self.http.get_json(f"{self.base_url}/events", params=params)
            for item in (data or {}).get("events") or []:
                if isinstance(item, dict):
                    yield parse_kalshi_event(item)
            cursor = (data or {}).get("cursor") or None
            if not cursor:
                return

    def get_orderbook(self, ticker: str) -> tuple[Book, Book]:
        data = self.http.get_json(f"{self.base_url}/markets/{ticker}/orderbook")
        return parse_kalshi_orderbook(ticker, data if isinstance(data, dict) else {})
