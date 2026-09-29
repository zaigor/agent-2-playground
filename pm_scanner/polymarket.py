"""Read-only Polymarket clients: Gamma (market metadata) and CLOB (order books).

Gamma:  GET https://gamma-api.polymarket.com/events/keyset?active=true&closed=false&limit=..&after_cursor=..
        (plain /events caps offset at 2000; keyset returns {"events": [..], "next_cursor": ..})
        Events carry `negRisk` (mutually exclusive multi-outcome), `tags`, and
        nested `markets`. Market fields that matter here: `clobTokenIds`,
        `outcomes`, `outcomePrices` (all JSON-encoded strings), `bestBid`,
        `bestAsk` (for outcome[0], i.e. YES), `endDate`, `acceptingOrders`.
CLOB:   GET  https://clob.polymarket.com/book?token_id=..
        POST https://clob.polymarket.com/books  body: [{"token_id": ..}, ...]
        Levels are {"price": "0.45", "size": "120"}; `asset_id` is the token id.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .http import HttpClient, HttpError

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
SITE_URL = "https://polymarket.com"


def _jsonish_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
        except json.JSONDecodeError:
            return []
        return out if isinstance(out, list) else []
    return []


def _float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Level:
    price: float
    size: float


@dataclass
class Book:
    token_id: str
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)

    @property
    def best_bid(self) -> Level | None:
        return max(self.bids, key=lambda lv: lv.price) if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return min(self.asks, key=lambda lv: lv.price) if self.asks else None


def _levels(raw: Any, cents: bool = False) -> list[Level]:
    out: list[Level] = []
    for item in raw or []:
        if isinstance(item, dict):
            price, size = _float(item.get("price")), _float(item.get("size"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, size = _float(item[0]), _float(item[1])
        else:
            continue
        if price is None or size is None or size <= 0:
            continue
        if cents:
            price = price / 100.0
        out.append(Level(price, size))
    return out


def parse_book(data: dict[str, Any]) -> Book:
    token = str(data.get("asset_id") or data.get("token_id") or "")
    return Book(token_id=token, bids=_levels(data.get("bids")), asks=_levels(data.get("asks")))


@dataclass
class PolyMarket:
    id: str
    question: str
    slug: str
    condition_id: str
    outcomes: list[str]
    token_ids: list[str]
    outcome_prices: list[float]
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    end_date: datetime | None
    neg_risk: bool
    liquidity: float
    volume_24h: float
    accepting_orders: bool
    enable_order_book: bool
    group_item_title: str
    order_min_size: float  # smallest order the CLOB accepts, in shares (Gamma `orderMinSize`, usually 5)
    raw: dict = field(default_factory=dict, repr=False)
    closed: bool = False
    fee_rate: float | None = None  # Gamma `feeSchedule.rate` (taker rate of the category); None when Gamma omits it
    last_trade_price: float | None = None
    one_day_change: float | None = None  # Gamma `oneDayPriceChange`; frozen at close for resolved markets
    created_at: datetime | None = None

    @property
    def resolved_yes(self) -> bool | None:
        """True/False once a binary market has settled to 1/0, else None."""
        if not self.closed or len(self.outcome_prices) != 2:
            return None
        p = self.outcome_prices[0]
        return True if p > 0.99 else (False if p < 0.01 else None)

    @property
    def price_day_before_close(self) -> float | None:
        """Last price about 24h before the final trade of a resolved market.

        Gamma freezes `lastTradePrice` and `oneDayPriceChange` when a market closes, so
        last - change is the price one day earlier. Checked against CLOB price history on
        1,020 weather markets: correlation 0.90, median gap 1.5 cents.
        """
        if self.last_trade_price is None or self.one_day_change is None:
            return None
        p = self.last_trade_price - self.one_day_change
        return p if 0.0 <= p <= 1.0 else None

    @property
    def is_binary(self) -> bool:
        return len(self.token_ids) == 2

    @property
    def yes_token(self) -> str | None:
        return self.token_ids[0] if self.token_ids else None

    @property
    def no_token(self) -> str | None:
        return self.token_ids[1] if len(self.token_ids) > 1 else None

    @property
    def url(self) -> str:
        return f"{SITE_URL}/market/{self.slug}" if self.slug else SITE_URL

    @property
    def label(self) -> str:
        return self.group_item_title or self.question

    # Gamma's bestBid/bestAsk are quoted on the YES token. The NO book is the
    # mirror image (a NO ask at p is a YES bid at 1-p), so:
    @property
    def no_best_ask(self) -> float | None:
        return None if self.best_bid is None else round(1.0 - self.best_bid, 6)

    @property
    def no_best_bid(self) -> float | None:
        return None if self.best_ask is None else round(1.0 - self.best_ask, 6)


def parse_market(d: dict[str, Any]) -> PolyMarket:
    return PolyMarket(
        id=str(d.get("id", "")),
        question=str(d.get("question") or ""),
        slug=str(d.get("slug") or ""),
        condition_id=str(d.get("conditionId") or ""),
        outcomes=[str(o) for o in _jsonish_list(d.get("outcomes"))],
        token_ids=[str(t) for t in _jsonish_list(d.get("clobTokenIds"))],
        outcome_prices=[_float(p, 0.0) or 0.0 for p in _jsonish_list(d.get("outcomePrices"))],
        best_bid=_float(d.get("bestBid")),
        best_ask=_float(d.get("bestAsk")),
        spread=_float(d.get("spread")),
        end_date=parse_dt(d.get("endDate")),
        neg_risk=bool(d.get("negRisk", False)),
        liquidity=_float(d.get("liquidityNum") or d.get("liquidity"), 0.0) or 0.0,
        volume_24h=_float(d.get("volume24hr"), 0.0) or 0.0,
        accepting_orders=bool(d.get("acceptingOrders", True)),
        enable_order_book=bool(d.get("enableOrderBook", True)),
        group_item_title=str(d.get("groupItemTitle") or ""),
        order_min_size=_float(d.get("orderMinSize"), 5.0) or 5.0,
        raw=d,
        closed=bool(d.get("closed", False)),
        fee_rate=_float((d.get("feeSchedule") or {}).get("rate")) if isinstance(d.get("feeSchedule"), dict) else None,
        last_trade_price=_float(d.get("lastTradePrice")),
        one_day_change=_float(d.get("oneDayPriceChange")),
        created_at=parse_dt(d.get("createdAt")),
    )


@dataclass
class PolyEvent:
    id: str
    title: str
    slug: str
    neg_risk: bool
    neg_risk_augmented: bool
    tags: list[str]
    markets: list[PolyMarket]
    end_date: datetime | None
    closed: bool = False
    series_slug: str = ""  # Gamma recurring series (e.g. nyc-daily-weather); "" for one-off events
    recurrence: str = ""  # daily / weekly / monthly / ... as Gamma labels the series
    created_at: datetime | None = None
    volume: float = 0.0
    liquidity: float = 0.0
    resolution_source: str = ""

    @property
    def url(self) -> str:
        return f"{SITE_URL}/event/{self.slug}" if self.slug else SITE_URL


def parse_event(d: dict[str, Any]) -> PolyEvent:
    tags: list[str] = []
    for t in d.get("tags") or []:
        if isinstance(t, dict):
            slug = t.get("slug") or t.get("label")
            if slug:
                tags.append(str(slug).lower())
        elif isinstance(t, str):
            tags.append(t.lower())
    markets = [parse_market(m) for m in d.get("markets") or [] if isinstance(m, dict)]
    series = d.get("series") or []
    first_series = series[0] if series and isinstance(series[0], dict) else {}
    return PolyEvent(
        id=str(d.get("id", "")),
        title=str(d.get("title") or ""),
        slug=str(d.get("slug") or ""),
        neg_risk=bool(d.get("negRisk", False)) or any(m.neg_risk for m in markets),
        neg_risk_augmented=bool(d.get("negRiskAugmented", False)),
        tags=tags,
        markets=markets,
        end_date=parse_dt(d.get("endDate")),
        closed=bool(d.get("closed", False)),
        series_slug=str(d.get("seriesSlug") or first_series.get("slug") or ""),
        recurrence=str(first_series.get("recurrence") or ""),
        created_at=parse_dt(d.get("createdAt")),
        volume=_float(d.get("volume"), 0.0) or 0.0,
        liquidity=_float(d.get("liquidity"), 0.0) or 0.0,
        resolution_source=str(d.get("resolutionSource") or ""),
    )


class PolymarketClient:
    def __init__(self, http: HttpClient, gamma_url: str = GAMMA_URL, clob_url: str = CLOB_URL) -> None:
        self.http = http
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")

    def iter_events(self, page_size: int = 100, max_events: int = 3000) -> Iterator[PolyEvent]:
        # /events rejects offset > 2000 (HTTP 422), so page with the keyset cursor instead.
        cursor: str | None = None
        seen = 0
        ids: set[str] = set()  # volumes move between requests, so an event can land on two pages
        while seen < max_events:
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "order": "volume24hr",
                "ascending": "false",
            }
            if cursor:
                params["after_cursor"] = cursor
            data = self.http.get_json(f"{self.gamma_url}/events/keyset", params=params)
            items = data.get("events") if isinstance(data, dict) else None
            if not items:
                return
            for item in items:
                if isinstance(item, dict):
                    ev = parse_event(item)
                    if ev.id in ids:
                        continue
                    ids.add(ev.id)
                    yield ev
                    seen += 1
                    if seen >= max_events:
                        return
            cursor = data.get("next_cursor")
            if not cursor or len(items) < page_size:
                return

    def tag_id(self, tag_slug: str) -> str | None:
        data = self.http.get_json(f"{self.gamma_url}/tags/slug/{tag_slug}")
        tid = data.get("id") if isinstance(data, dict) else None
        return str(tid) if tid is not None else None

    def iter_events_by_tag(
        self,
        tag_slug: str,
        page_size: int = 100,
        include_closed: bool = False,
        *,
        closed_only: bool = False,
        start_min: datetime | None = None,
        start_max: datetime | None = None,
        exclude_tag_id: str | None = None,
        max_offset: int = 4000,
    ) -> Iterator[PolyEvent]:
        """Events carrying a Gamma tag (e.g. `israel-election`).

        `start_min` / `start_max` bound the event start date so a big tag can be pulled in
        windows: /events rejects offsets beyond a few thousand (HTTP 422), hence `max_offset`.
        `exclude_tag_id` drops e.g. the 5-minute crypto series (tag 102127, "up-or-down").
        """
        tid = self.tag_id(tag_slug)
        if tid is None:
            return
        offset = 0
        while offset < max_offset:
            params: dict[str, Any] = {"tag_id": tid, "limit": page_size, "offset": offset}
            if closed_only:
                params["closed"] = "true"
            elif not include_closed:
                params["closed"] = "false"
            if start_min is not None:
                params["start_date_min"] = start_min.strftime("%Y-%m-%dT%H:%M:%SZ")
            if start_max is not None:
                params["start_date_max"] = start_max.strftime("%Y-%m-%dT%H:%M:%SZ")
            if exclude_tag_id:
                params["exclude_tag_id"] = exclude_tag_id
            data = self.http.get_json(f"{self.gamma_url}/events", params=params)
            items = data if isinstance(data, list) else []
            for item in items:
                if isinstance(item, dict):
                    yield parse_event(item)
            if len(items) < page_size:
                return
            offset += page_size

    def get_book(self, token_id: str) -> Book:
        data = self.http.get_json(f"{self.clob_url}/book", params={"token_id": token_id})
        book = parse_book(data if isinstance(data, dict) else {})
        if not book.token_id:
            book.token_id = token_id
        return book

    def get_books(self, token_ids: Iterable[str], chunk: int = 50) -> dict[str, Book]:
        ids = [t for t in dict.fromkeys(token_ids) if t]
        out: dict[str, Book] = {}
        for i in range(0, len(ids), chunk):
            batch = ids[i : i + chunk]
            try:
                data = self.http.post_json(f"{self.clob_url}/books", [{"token_id": t} for t in batch])
                for item in data if isinstance(data, list) else []:
                    if isinstance(item, dict):
                        book = parse_book(item)
                        if book.token_id:
                            out[book.token_id] = book
            except HttpError:
                pass
            for t in batch:
                if t not in out:
                    try:
                        out[t] = self.get_book(t)
                    except HttpError:
                        out[t] = Book(token_id=t)
        return out
