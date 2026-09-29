"""Opportunity detectors. All edges are net of taker fees and sized to a budget.

Why there is no single-market YES+NO arbitrage scan: on both Polymarket and
Kalshi a binary market is *one* order book. A NO ask at price p is literally a
YES bid at 1-p, so best_ask(YES) + best_ask(NO) = 1 + spread >= 1 always.
The exploitable structures are:
  * negative-risk (mutually exclusive multi-outcome) events on Polymarket, where
    each outcome has its own book and the sum can drift away from $1;
  * the same question listed on both platforms at different prices;
  * outcomes that are already effectively decided but still trade below $1.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher

from .fees import kalshi_taker_fee, polymarket_rate_for_tags, polymarket_taker_fee
from .kalshi import KalshiEvent, KalshiMarket
from .polymarket import Book, PolyEvent, PolyMarket


@dataclass
class Leg:
    platform: str
    market: str
    side: str  # "BUY YES" / "BUY NO" / "SELL YES" / "SELL NO"
    price: float
    size: float | None  # top-of-book size in shares, None if unknown
    fee_per_unit: float


@dataclass
class Opportunity:
    kind: str
    platform: str
    title: str
    url: str
    cost_per_set: float  # cash needed to buy one $1-payout "set"
    payout_per_set: float
    fee_per_set: float
    edge_per_set: float  # payout - cost - fees
    edge_pct: float  # edge / cost
    fillable_sets: float | None  # limited by top-of-book depth
    budget_sets: float  # min(fillable, budget / cost)
    est_profit: float  # budget_sets * edge_per_set
    legs: list[Leg] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    end_date: str | None = None
    min_order_size: float | None = None  # smallest ticket the venue accepts per leg, in shares

    @property
    def executable(self) -> bool:
        """False when the budget- and depth-limited size is below the venue minimum."""
        return self.min_order_size is None or self.budget_sets >= self.min_order_size

    def to_dict(self) -> dict:
        d = asdict(self)
        d["executable"] = self.executable
        return d


def _size_for_budget(cost_per_set: float, fillable: float | None, budget: float) -> float:
    if cost_per_set <= 0:
        return 0.0
    affordable = budget / cost_per_set
    return affordable if fillable is None else min(fillable, affordable)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# --------------------------------------------------------------------------- #
# Polymarket negative-risk (mutually exclusive) events
# --------------------------------------------------------------------------- #

def _event_rate(event: PolyEvent, override: float | None) -> float:
    return override if override is not None else polymarket_rate_for_tags(event.tags)


def find_negrisk_candidates(
    events: list[PolyEvent],
    min_edge: float,
    fee_override: float | None = None,
    slack: float = 0.01,
) -> list[tuple[PolyEvent, str]]:
    """Cheap first pass using Gamma's top-of-book quotes (no order books).

    Returns (event, kind) pairs worth pulling full books for. `slack` widens the
    net so stale Gamma quotes do not hide a live opportunity.
    """
    out: list[tuple[PolyEvent, str]] = []
    for ev in events:
        if not ev.neg_risk or len(ev.markets) < 2:
            continue
        mkts = [m for m in ev.markets if m.is_binary and m.accepting_orders]
        if len(mkts) != len(ev.markets) or any(m.best_ask is None or m.best_bid is None for m in mkts):
            continue
        rate = _event_rate(ev, fee_override)
        sum_asks = sum(m.best_ask for m in mkts)  # type: ignore[misc]
        fee_yes = sum(polymarket_taker_fee(m.best_ask, 1.0, rate) for m in mkts)  # type: ignore[arg-type]
        if 1.0 - sum_asks - fee_yes >= min_edge - slack:
            out.append((ev, "negrisk_buy_all_yes"))
        sum_bids = sum(m.best_bid for m in mkts)  # type: ignore[misc]
        fee_no = sum(polymarket_taker_fee(1.0 - m.best_bid, 1.0, rate) for m in mkts)  # type: ignore[operator]
        if sum_bids - 1.0 - fee_no >= min_edge - slack:
            out.append((ev, "negrisk_buy_all_no"))
    return out


def evaluate_negrisk(
    event: PolyEvent,
    kind: str,
    books: dict[str, Book],
    budget: float,
    fee_override: float | None = None,
) -> Opportunity | None:
    """Price a full set from live books (top of book only, conservative)."""
    rate = _event_rate(event, fee_override)
    legs: list[Leg] = []
    n = len(event.markets)
    for m in event.markets:
        token = m.yes_token if kind == "negrisk_buy_all_yes" else m.no_token
        book = books.get(token or "")
        best = book.best_ask if book else None
        if best is None:
            return None
        side = "BUY YES" if kind == "negrisk_buy_all_yes" else "BUY NO"
        legs.append(Leg("polymarket", m.label, side, best.price, best.size, polymarket_taker_fee(best.price, 1.0, rate)))

    cost = sum(lg.price for lg in legs)
    fees = sum(lg.fee_per_unit for lg in legs)
    payout = 1.0 if kind == "negrisk_buy_all_yes" else float(n - 1)
    edge = payout - cost - fees
    fillable = min(lg.size for lg in legs if lg.size is not None) if legs else 0.0
    budget_sets = _size_for_budget(cost, fillable, budget)
    min_size = max(m.order_min_size for m in event.markets)

    notes = [
        f"fee rate {rate:.2f} from tags {event.tags[:4]}" if event.tags else f"fee rate {rate:.2f} (no tags, default)",
        "all legs are taker orders; capital is locked until the event resolves",
    ]
    if kind == "negrisk_buy_all_yes":
        notes.append("pays $1 only if exactly one outcome resolves YES: confirm the event is exhaustive (has a catch-all outcome)")
    else:
        notes.append(f"pays ${n - 1} if one outcome wins and ${n} if none does, so it is robust to non-exhaustive events")
    if event.neg_risk_augmented:
        notes.append("negRiskAugmented=true: new outcomes can be added later, which changes the payout")
    if budget_sets < min_size:
        notes.append(f"below the venue minimum of {min_size:g} shares per leg: not executable at this budget/depth")

    return Opportunity(
        kind=kind,
        platform="polymarket",
        title=event.title,
        url=event.url,
        cost_per_set=round(cost, 4),
        payout_per_set=payout,
        fee_per_set=round(fees, 4),
        edge_per_set=round(edge, 4),
        edge_pct=round(edge / cost, 4) if cost else 0.0,
        fillable_sets=fillable,
        budget_sets=round(budget_sets, 2),
        est_profit=round(budget_sets * edge, 2),
        legs=legs,
        notes=notes,
        end_date=_iso(event.end_date),
        min_order_size=min_size,
    )


# --------------------------------------------------------------------------- #
# Near-certain outcomes close to resolution (research list, NOT arbitrage)
# --------------------------------------------------------------------------- #

def scan_near_certain(
    events: list[PolyEvent],
    budget: float,
    now: datetime,
    min_price: float = 0.95,
    max_days: int = 14,
    fee_override: float | None = None,
) -> list[Opportunity]:
    out: list[Opportunity] = []
    for ev in events:
        rate = _event_rate(ev, fee_override)
        for m in ev.markets:
            if not (m.is_binary and m.accepting_orders and m.end_date):
                continue
            days = (m.end_date - now).total_seconds() / 86400.0
            if days < 0 or days > max_days:
                continue
            for side, ask in (("BUY YES", m.best_ask), ("BUY NO", m.no_best_ask)):
                if ask is None or ask < min_price or ask >= 1.0:
                    continue
                fee_unit = polymarket_taker_fee(ask, 1.0, rate)
                edge = 1.0 - ask - fee_unit
                if edge <= 0:
                    continue
                shares = budget / ask
                annualized = (edge / ask) * (365.0 / max(days, 1.0))
                notes = [
                    f"resolves in ~{days:.1f} days; ~{annualized * 100:.0f}% annualized if it pays",
                    "NOT risk-free: read the resolution rules; UMA disputes and misreadings lose the whole stake",
                    "depth unknown (no book pulled); a resting limit order one tick inside pays no taker fee",
                ]
                if shares < m.order_min_size:
                    notes.append(f"below the venue minimum of {m.order_min_size:g} shares")
                out.append(
                    Opportunity(
                        kind="near_certain",
                        platform="polymarket",
                        title=f"{ev.title} :: {m.label}" if m.label != ev.title else ev.title,
                        url=m.url,
                        cost_per_set=round(ask, 4),
                        payout_per_set=1.0,
                        fee_per_set=round(fee_unit, 4),
                        edge_per_set=round(edge, 4),
                        edge_pct=round(edge / ask, 4),
                        fillable_sets=None,
                        budget_sets=round(shares, 2),
                        est_profit=round(shares * edge, 2),
                        legs=[Leg("polymarket", m.label, side, ask, None, fee_unit)],
                        notes=notes,
                        end_date=_iso(m.end_date),
                        min_order_size=m.order_min_size,
                    )
                )
    return out


# --------------------------------------------------------------------------- #
# Cross-platform (Polymarket vs Kalshi) on the same question
# --------------------------------------------------------------------------- #

_STOP = {
    "will", "the", "a", "an", "of", "to", "in", "on", "at", "by", "be", "is", "are", "and", "or",
    "for", "than", "this", "that", "with", "as", "does", "do", "before", "after", "from",
}
_MONTHS = {
    "jan": "january", "feb": "february", "mar": "march", "apr": "april", "jun": "june", "jul": "july",
    "aug": "august", "sep": "september", "sept": "september", "oct": "october", "nov": "november", "dec": "december",
}


def normalize_title(text: str) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9%$.\s]", " ", text)
    words = []
    for w in text.split():
        w = w.strip(".")
        w = _MONTHS.get(w, w)
        if w and w not in _STOP:
            words.append(w)
    return " ".join(words)


def _index_words(norm: str) -> set[str]:
    return {w for w in norm.split() if len(w) >= 3}


@dataclass
class CrossMatch:
    poly: PolyMarket
    poly_event: PolyEvent
    kalshi: KalshiMarket
    similarity: float


def match_cross_platform(
    events: list[PolyEvent],
    kalshi_markets: list[KalshiMarket],
    min_similarity: float = 0.72,
    max_end_gap_days: float = 3.0,
) -> list[CrossMatch]:
    """Fuzzy-match binary Polymarket markets to Kalshi markets by title and date.

    An inverted word index prefilters pairs so we never run difflib over the full
    cross product (thousands x thousands).
    """
    k_norm = [normalize_title(k.full_title) for k in kalshi_markets]
    index: dict[str, set[int]] = defaultdict(set)
    for i, norm in enumerate(k_norm):
        for w in _index_words(norm):
            index[w].add(i)

    matches: list[CrossMatch] = []
    for ev in events:
        for m in ev.markets:
            if not (m.is_binary and m.accepting_orders):
                continue
            title = m.question if m.question else f"{ev.title} {m.label}"
            p_norm = normalize_title(title)
            words = _index_words(p_norm)
            if not words:
                continue
            counts: dict[int, int] = defaultdict(int)
            for w in words:
                for i in index.get(w, ()):
                    counts[i] += 1
            need = 2 if len(words) >= 3 else 1
            for i, c in counts.items():
                if c < need:
                    continue
                k = kalshi_markets[i]
                if m.end_date and k.close_time:
                    gap = abs((m.end_date - k.close_time).total_seconds()) / 86400.0
                    if gap > max_end_gap_days:
                        continue
                sim = SequenceMatcher(None, p_norm, k_norm[i]).ratio()
                if sim >= min_similarity:
                    matches.append(CrossMatch(m, ev, k, round(sim, 3)))
    return matches


def evaluate_cross(
    match: CrossMatch,
    budget: float,
    fee_override: float | None = None,
    kalshi_multiplier: float = 1.0,
    kalshi_books: tuple[Book, Book] | None = None,
) -> list[Opportunity]:
    """Two hedges: (Poly YES + Kalshi NO) and (Poly NO + Kalshi YES). Payout $1 either way."""
    pm, km, ev = match.poly, match.kalshi, match.poly_event
    rate = _event_rate(ev, fee_override)
    out: list[Opportunity] = []

    k_yes_ask, k_no_ask = km.yes_ask, km.no_ask
    k_yes_size = k_no_size = None
    if kalshi_books:
        yes_book, no_book = kalshi_books
        if yes_book.best_ask:
            k_yes_ask, k_yes_size = yes_book.best_ask.price, yes_book.best_ask.size
        if no_book.best_ask:
            k_no_ask, k_no_size = no_book.best_ask.price, no_book.best_ask.size

    combos = [
        ("BUY YES", pm.best_ask, "BUY NO", k_no_ask, k_no_size),
        ("BUY NO", pm.no_best_ask, "BUY YES", k_yes_ask, k_yes_size),
    ]
    for p_side, p_price, k_side, k_price, k_size in combos:
        if p_price is None or k_price is None:
            continue
        p_fee = polymarket_taker_fee(p_price, 1.0, rate)
        k_fee = kalshi_taker_fee(k_price, 1.0, kalshi_multiplier)
        cost = p_price + k_price
        fees = p_fee + k_fee
        edge = 1.0 - cost - fees
        fillable = k_size  # Polymarket depth not pulled here
        budget_sets = _size_for_budget(cost, fillable, budget)
        out.append(
            Opportunity(
                kind="cross_platform",
                platform="polymarket+kalshi",
                title=f"{pm.question or ev.title}  <->  {km.full_title} [{km.ticker}]",
                url=f"{pm.url} | {km.url}",
                cost_per_set=round(cost, 4),
                payout_per_set=1.0,
                fee_per_set=round(fees, 4),
                edge_per_set=round(edge, 4),
                edge_pct=round(edge / cost, 4) if cost else 0.0,
                fillable_sets=fillable,
                budget_sets=round(budget_sets, 2),
                est_profit=round(budget_sets * edge, 2),
                legs=[
                    Leg("polymarket", pm.question or pm.label, p_side, p_price, None, p_fee),
                    Leg("kalshi", km.ticker, k_side, k_price, k_size, k_fee),
                ],
                notes=[
                    f"title similarity {match.similarity:.2f}: verify resolution sources, deadlines and edge cases are identical",
                    "needs funded accounts on both platforms; capital on both sides is locked until resolution",
                    "Kalshi fee rounds up to the cent per order, so tiny orders pay more than the formula",
                ],
                end_date=_iso(pm.end_date),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Kalshi mutually-exclusive events (one winner per event)
# --------------------------------------------------------------------------- #

def scan_kalshi_mutex(
    events: list[KalshiEvent],
    budget: float,
    min_edge: float,
    multiplier: float = 1.0,
    book_fetch=None,
    slack: float = 0.01,
) -> list[Opportunity]:
    """Buy every YES (pays $1) or every NO (pays N-1) across a one-winner event.

    Kalshi's per-order ceil-to-cent makes tiny legs expensive, so the fee here is
    the unrounded per-contract rate and a note reminds you of the rounding.
    `book_fetch(ticker) -> (yes_book, no_book) | None` is only called for events
    that already look profitable from the market quotes.
    """
    out: list[Opportunity] = []
    for ev in events:
        if not ev.mutually_exclusive or len(ev.markets) < 2:
            continue
        if any(m.yes_ask is None or m.yes_bid is None for m in ev.markets):
            continue
        n = len(ev.markets)
        for kind in ("kalshi_buy_all_yes", "kalshi_buy_all_no"):
            if kind == "kalshi_buy_all_yes":
                prices = [m.yes_ask for m in ev.markets]
                payout = 1.0
            else:
                prices = [m.no_ask if m.no_ask is not None else 1.0 - m.yes_bid for m in ev.markets]
                payout = float(n - 1)
            fees = sum(kalshi_taker_fee(p, 1.0, multiplier, round_up=False) for p in prices)  # type: ignore[arg-type]
            cost = sum(prices)  # type: ignore[arg-type]
            if payout - cost - fees < min_edge - slack:
                continue
            legs: list[Leg] = []
            fillable: float | None = None
            for m, p in zip(ev.markets, prices):
                size = None
                price = p
                if book_fetch is not None:
                    books = book_fetch(m.ticker)
                    if books:
                        book = books[0] if kind == "kalshi_buy_all_yes" else books[1]
                        if book.best_ask:
                            price, size = book.best_ask.price, book.best_ask.size
                side = "BUY YES" if kind == "kalshi_buy_all_yes" else "BUY NO"
                legs.append(Leg("kalshi", f"{m.ticker} {m.subtitle}".strip(), side, price, size, kalshi_taker_fee(price, 1.0, multiplier, round_up=False)))  # type: ignore[arg-type]
                if size is not None:
                    fillable = size if fillable is None else min(fillable, size)
            cost = sum(lg.price for lg in legs)
            fees = sum(lg.fee_per_unit for lg in legs)
            edge = payout - cost - fees
            if edge < min_edge:
                continue
            budget_sets = _size_for_budget(cost, fillable, budget)
            notes = [
                "all legs taker; Kalshi rounds each order's fee up to the next cent (adds up to $0.01 per leg)",
                "capital locked until the event settles",
            ]
            if kind == "kalshi_buy_all_yes":
                notes.append("pays $1 only if exactly one listed outcome wins: confirm the event is exhaustive")
            else:
                notes.append(f"pays ${n - 1} if one listed outcome wins, ${n} if none does")
            out.append(
                Opportunity(
                    kind=kind,
                    platform="kalshi",
                    title=ev.title,
                    url=ev.url,
                    cost_per_set=round(cost, 4),
                    payout_per_set=payout,
                    fee_per_set=round(fees, 4),
                    edge_per_set=round(edge, 4),
                    edge_pct=round(edge / cost, 4) if cost else 0.0,
                    fillable_sets=fillable,
                    budget_sets=round(budget_sets, 2),
                    est_profit=round(budget_sets * edge, 2),
                    legs=legs,
                    notes=notes,
                    end_date=_iso(min((m.close_time for m in ev.markets if m.close_time), default=None)),
                    min_order_size=1.0,
                )
            )
    return out


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
