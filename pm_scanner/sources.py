"""Data sources: live APIs or recorded fixtures (for tests and for running offline)."""
from __future__ import annotations

import json
from pathlib import Path

from .http import HttpClient, HttpError
from .kalshi import KalshiClient, KalshiEvent, KalshiMarket, parse_kalshi_event, parse_kalshi_market, parse_kalshi_orderbook
from .polymarket import Book, PolyEvent, PolymarketClient, parse_book, parse_event


class LiveSource:
    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()
        self.poly = PolymarketClient(self.http)
        self.kalshi = KalshiClient(self.http)

    def poly_events(self, max_events: int) -> list[PolyEvent]:
        return list(self.poly.iter_events(max_events=max_events))

    def poly_books(self, token_ids: list[str]) -> dict[str, Book]:
        return self.poly.get_books(token_ids)

    def poly_events_by_tag(self, tag_slug: str) -> list[PolyEvent]:
        return list(self.poly.iter_events_by_tag(tag_slug))

    def kalshi_markets(self) -> list[KalshiMarket]:
        return list(self.kalshi.iter_markets())

    def kalshi_events(self) -> list[KalshiEvent]:
        return list(self.kalshi.iter_events())

    def kalshi_orderbook(self, ticker: str) -> tuple[Book, Book] | None:
        try:
            return self.kalshi.get_orderbook(ticker)
        except HttpError:
            return None


class FixtureSource:
    """Reads gamma_events.json, clob_books.json, kalshi_markets.json, kalshi_orderbooks.json,
    plus israel_events.json / israel_books.json for the election model."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def _load(self, name: str, default):
        path = self.dir / name
        if not path.exists():
            return default
        return json.loads(path.read_text())

    def poly_events(self, max_events: int) -> list[PolyEvent]:
        return [parse_event(d) for d in self._load("gamma_events.json", [])][:max_events]

    def poly_books(self, token_ids: list[str]) -> dict[str, Book]:
        raw = self._load("clob_books.json", []) + self._load("israel_books.json", [])
        books = {b.token_id: b for b in (parse_book(d) for d in raw)}
        return {t: books.get(t, Book(token_id=t)) for t in token_ids}

    def poly_events_by_tag(self, tag_slug: str) -> list[PolyEvent]:
        name = "israel_events.json" if "israel" in tag_slug else "gamma_events.json"
        return [parse_event(d) for d in self._load(name, [])]

    def kalshi_markets(self) -> list[KalshiMarket]:
        data = self._load("kalshi_markets.json", {})
        return [parse_kalshi_market(d) for d in data.get("markets", [])]

    def kalshi_events(self) -> list[KalshiEvent]:
        data = self._load("kalshi_events.json", {})
        return [parse_kalshi_event(d) for d in data.get("events", [])]

    def kalshi_orderbook(self, ticker: str) -> tuple[Book, Book] | None:
        data = self._load("kalshi_orderbooks.json", {}).get(ticker)
        return parse_kalshi_orderbook(ticker, data) if data else None
