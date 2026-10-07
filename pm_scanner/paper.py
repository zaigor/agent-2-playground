"""Paper test of the two tail trades memo 21f left open, pre-registered in memo 21g (7 Oct 2026).

Nothing here moves money. A nightly snapshot lists, for every open market whose scheduled end is
24-48 hours away, the cheap side's mid, bid, ask and depth from the live book, and writes a paper
position for the two registered legs:

* buy_touch: a crypto "reach $X" rung (weekly or monthly hit-price ladder) whose YES mid is 2-10c,
  bought at the ask, $20 or the ask's depth, whichever is less (21f: such rungs came in two and
  three quarter times their price on their last night, fourteen months, 42 hits where 15 were due);
  the 10-20c band is written as buy_touch_wide and scored apart;
* sell_tail: in politics, the "other" group (tweet counts, box office, rankings) and the stock,
  metal and oil ladders, a tail whose cheap side's mid is 2-5c, sold at that side's bid, $100 of
  capital or the bid's depth (21f: by the count those tails came in half as often as their price;
  by the dollars that traded, the seller lost outside the stock ladders; the paper fills decide).

A sell needs a bid of at least a cent and a buy an ask within 5c of the mid, else the line is a `record`.
Unregistered markets are written as `record` only where the calibration record wants them: touch rungs
under 50c, and the three sell groups under 10c (sports and weather are not kept; 21f read them).
`score` reads the record back, asks Gamma how each market resolved, and reports each leg's hits
against its prices, P&L after the taker fee per $100 staked (buys) or of capital (sells), week by
week, and the depth that was there to fill.
"""
from __future__ import annotations

import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .fees import polymarket_rate_for_event
from .niches import UP_OR_DOWN_TAG_ID, is_sport
from .polymarket import GAMMA_URL, Book, PolyEvent, PolyMarket, parse_event

CRYPTO_TAGS = {"crypto", "crypto-prices", "bitcoin", "ethereum", "solana", "xrp", "dogecoin", "hype", "bnb", "zcash"}
FINANCE_TAGS = {"equities", "stocks", "stock-prices", "finance-updown", "pyth-finance", "oil", "macro-indicators", "commodities", "gold", "forex", "fed-rates", "interest-rates"}
POLITICS_TAGS = {"politics", "global-elections", "elections", "main-election", "geopolitics", "world", "international-election-props", "world-elections", "quebec-elections", "middle-east", "military-action", "military", "military-strikes", "diplomacy-ceasefire", "iran", "israel", "trump", "canada", "brazil", "ukraine", "russia", "china", "congress", "senate", "supreme-court", "us-politics"}
MENTION_TAGS = {"mention-markets", "tweets-markets", "trump-speech"}

WINDOW_HOURS = (24.0, 48.0)
BUY_BAND = (0.02, 0.10)
BUY_WIDE_BAND = (0.10, 0.20)
SELL_BAND = (0.02, 0.05)
SELL_GROUPS = ("politics", "other", "finance")
SELL_MIN_BID = 0.01  # a tail whose bid is under a cent is recorded, not sold: the premium would not cover the fee and the tick
BUY_MAX_ASK_OVER_MID = 0.05  # a touch rung whose ask sits more than 5c above its mid has no book on that side: recorded, not bought
RECORD_Q = {"touch": 0.50, "politics": 0.10, "other": 0.10, "finance": 0.10}  # cheap-side mids under which an unregistered market is still written, for the calibration record
TOUCH_SERIES = re.compile(r"hit-price-(weekly|monthly)")


def group_of(ev: PolyEvent) -> str | None:
    """touch (a crypto reach-$X ladder), crypto, finance, weather, sports, politics, other; None for the
    5/15-minute up-or-down series."""
    tags = set(ev.tags)
    s = ev.series_slug or ""
    if "up-or-down" in s or "up-or-down" in tags:
        return None
    if s.endswith("daily-weather") or "weather" in tags:
        return "weather"
    if tags & CRYPTO_TAGS:
        return "touch" if "hit-price" in s else "crypto"
    if tags & FINANCE_TAGS or re.search(r"multi-strikes|hit-price|-hit\b", s):
        return "finance"
    if is_sport(ev):
        return "sports"
    if tags & MENTION_TAGS:
        return "other"
    if tags & POLITICS_TAGS:
        return "politics"
    return "other"


@dataclass
class Candidate:
    snapshot: str  # ISO time of the reading
    market_id: str
    condition_id: str
    question: str
    series: str
    group: str
    leg: str  # buy_touch | buy_touch_wide | sell_tail | record
    end: str  # the scheduled end (Gamma endDate)
    hours_to_end: float
    fee_rate: float
    yes_token: str
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float
    yes_ask_size: float
    mid: float | None  # YES mid
    side: str  # the cheap side: YES or NO
    q: float | None  # the cheap side's mid
    price: float | None  # the paper fill: the cheap side's ask (buy) or bid (sell)
    depth: float  # shares at that level
    shares: float  # paper size
    dollars: float  # buy: the stake; sell: the capital (shares x (1 - price))
    volume: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


def fetch_open_events(http, now: datetime, *, window: tuple[float, float] = WINDOW_HOURS, page_size: int = 100, max_offset: int = 2000) -> list[PolyEvent]:
    """Open events on Gamma whose end date falls on the days the window touches, pulled a day of end
    dates at a time (Gamma refuses offsets past about 2,000), the up-or-down series excluded."""
    lo = now + timedelta(hours=window[0]) - timedelta(days=1)
    hi = now + timedelta(hours=window[1]) + timedelta(days=1)
    day = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    seen: dict[str, PolyEvent] = {}
    while day <= hi:
        offset = 0
        while offset < max_offset:
            params = {"closed": "false", "active": "true", "limit": page_size, "offset": offset, "end_date_min": day.strftime("%Y-%m-%dT00:00:00Z"), "end_date_max": day.strftime("%Y-%m-%dT23:59:59Z"), "exclude_tag_id": UP_OR_DOWN_TAG_ID}
            data = http.get_json(f"{GAMMA_URL}/events", params)
            items = data if isinstance(data, list) else []
            for item in items:
                if isinstance(item, dict):
                    ev = parse_event(item)
                    seen.setdefault(ev.id, ev)
            if len(items) < page_size:
                break
            offset += page_size
        day += timedelta(days=1)
    return list(seen.values())


def markets_in_window(events: Iterable[PolyEvent], now: datetime, *, window: tuple[float, float] = WINDOW_HOURS, min_volume: float = 0.0) -> list[tuple[PolyEvent, PolyMarket, str]]:
    out = []
    for ev in events:
        g = group_of(ev)
        if g is None:
            continue
        for m in ev.markets:
            if not (m.is_binary and m.end_date and not m.closed and m.accepting_orders and m.enable_order_book):
                continue
            h = _hours(now, m.end_date)
            if not (window[0] <= h < window[1]):
                continue
            if (m.raw.get("volumeNum") or 0) < min_volume:
                continue
            out.append((ev, m, g))
    return out


def _best(book: Book | None) -> tuple[float | None, float | None, float, float]:
    if book is None:
        return None, None, 0.0, 0.0
    bb, ba = book.best_bid, book.best_ask
    return (bb.price if bb else None), (ba.price if ba else None), (bb.size if bb else 0.0), (ba.size if ba else 0.0)


def classify(group: str, series: str, mid: float | None, yes_bid: float | None, yes_ask: float | None) -> tuple[str, str]:
    """The leg a market belongs to and its cheap side."""
    if mid is None or yes_bid is None or yes_ask is None:
        return "record", "YES"
    side = "YES" if mid <= 0.5 else "NO"
    q = mid if side == "YES" else 1.0 - mid
    c_bid = yes_bid if side == "YES" else 1.0 - yes_ask
    c_ask = yes_ask if side == "YES" else 1.0 - yes_bid
    if group == "touch" and side == "YES" and TOUCH_SERIES.search(series or "") and c_ask <= q + BUY_MAX_ASK_OVER_MID + 1e-9:
        if BUY_BAND[0] <= q < BUY_BAND[1]:
            return "buy_touch", side
        if BUY_WIDE_BAND[0] <= q < BUY_WIDE_BAND[1]:
            return "buy_touch_wide", side
    if group in SELL_GROUPS and SELL_BAND[0] <= q < SELL_BAND[1] and c_bid >= SELL_MIN_BID:
        return "sell_tail", side
    return "record", side


def snapshot(events: Iterable[PolyEvent], books: Callable[[list[str]], dict[str, Book]], now: datetime, *, window: tuple[float, float] = WINDOW_HOURS, buy_stake: float = 20.0, sell_capital: float = 100.0, min_volume: float = 0.0) -> list[Candidate]:
    """One night's reading: every market in the window with its book, the leg it belongs to and its paper fill."""
    rows = markets_in_window(events, now, window=window, min_volume=min_volume)
    bk = books([m.yes_token for _, m, _ in rows if m.yes_token]) if rows else {}
    out: list[Candidate] = []
    stamp = now.replace(microsecond=0).isoformat()
    for ev, m, g in rows:
        yes_bid, yes_ask, bsz, asz = _best(bk.get(m.yes_token or ""))
        mid = round((yes_bid + yes_ask) / 2.0, 4) if yes_bid is not None and yes_ask is not None and yes_ask >= yes_bid else None
        leg, side = classify(g, ev.series_slug, mid, yes_bid, yes_ask)
        q = None if mid is None else (mid if side == "YES" else round(1.0 - mid, 4))
        # the cheap side's own book: for NO, its bid is 1 - YES ask (that level's size), its ask 1 - YES bid
        if side == "YES":
            c_bid, c_ask, c_bid_sz, c_ask_sz = yes_bid, yes_ask, bsz, asz
        else:
            c_bid = None if yes_ask is None else round(1.0 - yes_ask, 4)
            c_ask = None if yes_bid is None else round(1.0 - yes_bid, 4)
            c_bid_sz, c_ask_sz = asz, bsz
        price = depth = shares = dollars = None
        if leg in ("buy_touch", "buy_touch_wide") and c_ask:
            price, depth = c_ask, c_ask_sz
            shares = min(depth, buy_stake / price) if price > 0 else 0.0
            dollars = shares * price
        elif leg == "sell_tail" and c_bid:
            price, depth = c_bid, c_bid_sz
            shares = min(depth, sell_capital / (1.0 - price)) if price < 1 else 0.0
            dollars = shares * (1.0 - price)
        if leg == "record" and (g not in RECORD_Q or q is None or q >= RECORD_Q[g]):
            continue
        rate = m.fee_rate if m.fee_rate is not None else polymarket_rate_for_event(ev)
        out.append(Candidate(
            snapshot=stamp, market_id=m.id, condition_id=m.condition_id, question=m.question, series=ev.series_slug or "", group=g, leg=leg,
            end=m.end_date.isoformat() if m.end_date else "", hours_to_end=round(_hours(now, m.end_date), 2) if m.end_date else 0.0, fee_rate=rate,
            yes_token=m.yes_token or "", yes_bid=yes_bid, yes_ask=yes_ask, yes_bid_size=bsz, yes_ask_size=asz, mid=mid, side=side, q=q,
            price=price, depth=depth or 0.0, shares=round(shares or 0.0, 2), dollars=round(dollars or 0.0, 2), volume=float(m.raw.get("volumeNum") or 0),
        ))
    out.sort(key=lambda c: ({"buy_touch": 0, "buy_touch_wide": 1, "sell_tail": 2, "record": 3}[c.leg], c.group, c.q or 0))
    return out


def append_jsonl(path: Path, rows: Iterable[Candidate]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r.to_dict(), separators=(",", ":")) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def render_snapshot(rows: list[Candidate], now: datetime) -> str:
    L = [f"paper snapshot {now:%Y-%m-%d %H:%M} UTC: {len(rows)} markets end in {WINDOW_HOURS[0]:.0f}-{WINDOW_HOURS[1]:.0f}h"]
    by = defaultdict(int)
    for r in rows:
        by[(r.leg, r.group)] += 1
    L.append("  " + ", ".join(f"{leg} {g}: {n}" for (leg, g), n in sorted(by.items())))
    for leg, title in (("buy_touch", "BUY the touch rung at the ask (2-10c)"), ("buy_touch_wide", "buy_touch_wide, recorded apart (10-20c)"), ("sell_tail", "SELL the tail at its bid (2-5c)")):
        sel = [r for r in rows if r.leg == leg]
        if not sel:
            L.append(f"\n{title}: none tonight")
            continue
        L.append(f"\n{title}: {len(sel)}")
        L.append(f"  {'group':8} {'series':30} {'question':58} {'side':4} {'mid':>6} {'bid':>6} {'ask':>6} {'depth':>8} {'fill':>6} {'shares':>7} {'$':>7} {'h':>5}")
        for r in sel:
            L.append(f"  {r.group:8} {r.series[:30]:30} {r.question[:58]:58} {r.side:4} {100*(r.q or 0):6.2f} {100*(r.yes_bid if r.side=='YES' else 1-(r.yes_ask or 0)):6.2f} {100*(r.yes_ask if r.side=='YES' else 1-(r.yes_bid or 0)):6.2f} {r.depth:8,.0f} {100*(r.price or 0):6.2f} {r.shares:7,.1f} {r.dollars:7,.2f} {r.hours_to_end:5.1f}")
    return "\n".join(L)


def gamma_resolutions(http, condition_ids: Iterable[str], chunk: int = 20) -> dict[str, dict[str, Any]]:
    """condition id -> {closed, y (1/0/None), closed_time} from Gamma."""
    ids = [c for c in dict.fromkeys(condition_ids) if c]
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), chunk):
        batch = ids[i : i + chunk]
        data = http.get_json(f"{GAMMA_URL}/markets", {"condition_ids": ",".join(batch), "limit": chunk})
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            m = parse_market_like(item)
            out[m["condition_id"]] = m
    return out


def parse_market_like(item: dict[str, Any]) -> dict[str, Any]:
    from .polymarket import parse_market
    m = parse_market(item)
    y = None
    if m.resolved_yes is not None:
        y = 1 if m.resolved_yes else 0
    return {"condition_id": m.condition_id, "closed": m.closed, "y": y, "closed_time": item.get("closedTime"), "question": m.question}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def score(rows: list[dict[str, Any]], resolutions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Each leg's paper P&L after the taker fee against its prices; unresolved positions are counted
    but not scored. A buy wins when the cheap side comes in; a sell wins when it does not."""
    legs: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("leg") == "record" or not r.get("price") or not r.get("shares"):
            continue
        res = resolutions.get(r["condition_id"])
        key = r["leg"]
        L = legs.setdefault(key, {"positions": 0, "resolved": 0, "hits": 0, "sum_price": 0.0, "dollars": 0.0, "pnl": 0.0, "fees": 0.0, "weeks": defaultdict(lambda: {"positions": 0, "resolved": 0, "hits": 0, "dollars": 0.0, "pnl": 0.0}), "groups": defaultdict(lambda: {"positions": 0, "resolved": 0, "hits": 0, "dollars": 0.0, "pnl": 0.0, "sum_price": 0.0}), "depth_dollars": [], "capped": 0})
        L["positions"] += 1
        price, shares = float(r["price"]), float(r["shares"])
        is_buy = key.startswith("buy")
        L["depth_dollars"].append(float(r.get("depth") or 0) * (price if is_buy else (1.0 - price)))
        want = (20.0 / price) if is_buy else (100.0 / (1.0 - price))
        if shares < want - 1e-6:
            L["capped"] += 1
        wk = datetime.fromisoformat(r["snapshot"]).strftime("%G-W%V")
        W = L["weeks"][wk]; G = L["groups"][r["group"]]
        W["positions"] += 1; G["positions"] += 1; G["sum_price"] += price
        if not res or res.get("y") is None:
            continue
        y = res["y"]
        came_in = (y == 1) if r["side"] == "YES" else (y == 0)
        fee = float(r.get("fee_rate") or 0) * price * (1.0 - price) * shares
        pnl = shares * ((1.0 if came_in else 0.0) - price) - fee if is_buy else shares * (price - (1.0 if came_in else 0.0)) - fee
        dollars = shares * price if is_buy else shares * (1.0 - price)
        for acc in (L, W, G):
            acc["resolved"] += 1; acc["hits"] += int(came_in); acc["dollars"] += dollars; acc["pnl"] += pnl
        L["sum_price"] += price; L["fees"] += fee
    out = {}
    for key, L in legs.items():
        n = L["resolved"]
        ci = wilson(L["hits"], n)
        out[key] = {
            "positions": L["positions"], "resolved": n, "hits": L["hits"], "mean_price": (L["sum_price"] / n) if n else None, "came_in": (L["hits"] / n) if n else None, "ci": ci,
            "dollars": L["dollars"], "pnl": L["pnl"], "fees": L["fees"], "per_100": (100 * L["pnl"] / L["dollars"]) if L["dollars"] else None,
            "median_depth_dollars": statistics.median(L["depth_dollars"]) if L["depth_dollars"] else 0.0, "capped": L["capped"],
            "weeks": {k: dict(v) for k, v in sorted(L["weeks"].items())}, "groups": {k: dict(v) for k, v in sorted(L["groups"].items())},
        }
    return out


def render_score(sc: dict[str, Any], n_rows: int, n_snapshots: int) -> str:
    L = [f"paper score: {n_rows} recorded lines over {n_snapshots} snapshots; P&L after the taker fee, per $100 staked (buys) or of capital (sells); 'depth' is the median dollars that were at the fill level"]
    for leg in ("buy_touch", "buy_touch_wide", "sell_tail"):
        s = sc.get(leg)
        if not s:
            L.append(f"\n{leg}: no positions yet")
            continue
        n = s["resolved"]
        L.append(f"\n{leg}: {s['positions']} positions, {n} resolved, {s['hits']} came in" + (f" ({100*s['came_in']:.1f}%, 95% {100*s['ci'][0]:.1f}..{100*s['ci'][1]:.1f}) at a mean fill of {100*s['mean_price']:.2f}c; P&L ${s['pnl']:+,.2f} on ${s['dollars']:,.2f} = {s['per_100']:+.1f} per $100" if n else "") + f"; depth at the fill ${s['median_depth_dollars']:,.0f} median, {s['capped']} positions cut by depth")
        if s["groups"]:
            L.append(f"  {'group':10} {'pos':>4} {'res':>4} {'hits':>4} {'price':>6} {'$':>9} {'P&L':>9} {'per $100':>8}")
            for g, v in s["groups"].items():
                L.append(f"  {g:10} {v['positions']:4} {v['resolved']:4} {v['hits']:4} {(100*v['sum_price']/v['positions']) if v['positions'] else 0:6.2f} {v['dollars']:9,.2f} {v['pnl']:+9.2f} {((100*v['pnl']/v['dollars']) if v['dollars'] else 0):+8.1f}")
        if s["weeks"]:
            L.append(f"  {'week':10} {'pos':>4} {'res':>4} {'hits':>4} {'$':>9} {'P&L':>9} {'per $100':>8}")
            for w, v in s["weeks"].items():
                L.append(f"  {w:10} {v['positions']:4} {v['resolved']:4} {v['hits']:4} {v['dollars']:9,.2f} {v['pnl']:+9.2f} {((100*v['pnl']/v['dollars']) if v['dollars'] else 0):+8.1f}")
    return "\n".join(L)
