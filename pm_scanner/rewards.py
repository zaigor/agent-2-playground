"""Liquidity rewards: what a small resting quote would earn per day from Polymarket's
incentive program, against what the same quote loses to adverse selection.

Reward side (docs.polymarket.com/programs/liquidity-rewards): every minute the book is
sampled; each resting order within `max_spread` cents of the midpoint scores
S = ((v - s) / v)^2 * size (v = max spread, s = distance from the mid), the two sides of a
maker are combined as Q = max(min(Q_bid, Q_ask), max(Q_bid, Q_ask) / 3) when the mid is in
[0.10, 0.90] (min(Q_bid, Q_ask) outside it), and the day's pot (`rate_per_day`, from
clob.polymarket.com/rewards/markets/current) is split in proportion to each maker's Q summed
over the samples. The competing Q is read off the live book: the sum over every maker's Q
lies between (Q_bid + Q_ask) / 6 (everyone one-sided) and min(Q_bid, Q_ask) + |Q_bid - Q_ask| / 3
(everyone balanced), so our share is a range. Makers are also paid a rebate of 15-25% of the
taker fee on each of their fills (docs.polymarket.com/programs/maker-rebates); it is added.

Cost side: the same quote is replayed on the market's own recent tape with the paper maker
from flow.py (re-centred on every print, filled by prints through the quote, marked to the
last print), which gives fills per day and the loss per fill.

Read-only; one book and one trade-history request per sampled market.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .flow import MakerConfig, simulate_maker, yes_price_series
from .http import HttpClient
from .polymarket import CLOB_URL, GAMMA_URL, Book, PolyMarket, parse_market

REWARDS_URL = f"{CLOB_URL}/rewards/markets/current"
DEFAULT_REBATE = 0.25  # share of the taker fee paid back to the maker on a fill (sports 0.15, crypto 0.20)


def fetch_reward_configs(http: HttpClient, limit: int = 500, max_pages: int = 60) -> list[dict[str, Any]]:
    """Every market with a live reward configuration: condition_id, total_daily_rate,
    rewards_max_spread (cents), rewards_min_size (shares)."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["next_cursor"] = cursor
        data = http.get_json(REWARDS_URL, params=params)
        items = data.get("data") if isinstance(data, dict) else None
        if not items:
            break
        rows.extend(i for i in items if isinstance(i, dict))
        cursor = data.get("next_cursor")
        if not cursor or cursor == "LTE=" or len(items) < limit:
            break
    return rows


def gamma_markets_by_condition(http: HttpClient, condition_ids: list[str], chunk: int = 20, closed: bool | None = None) -> dict[str, PolyMarket]:
    out: dict[str, PolyMarket] = {}
    ids = [c for c in dict.fromkeys(condition_ids) if c]
    for i in range(0, len(ids), chunk):
        params: list[tuple[str, Any]] = [("condition_ids", c) for c in ids[i : i + chunk]] + [("limit", chunk)]
        if closed is not None:
            params.append(("closed", "true" if closed else "false"))
        data = http.get_json(f"{GAMMA_URL}/markets", params=params)
        for d in data if isinstance(data, list) else []:
            if isinstance(d, dict):
                m = parse_market(d)
                if m.condition_id:
                    out[m.condition_id] = m
    return out


# --------------------------------------------------------------------------- #
# Reward arithmetic
# --------------------------------------------------------------------------- #

def order_score(v_cents: float, s_cents: float) -> float:
    """Per-share score of an order `s_cents` from the mid in a market with max spread `v_cents`."""
    if v_cents <= 0 or s_cents < 0 or s_cents > v_cents:
        return 0.0
    return ((v_cents - s_cents) / v_cents) ** 2


def book_competition(book: Book, mid: float, v_cents: float, min_size: float) -> dict[str, float]:
    """Score mass already resting on each side within the max spread (levels below the minimum
    size are ignored, as the program ignores such orders), plus the dollars resting there."""
    qb = qa = db = da = 0.0
    for lv in book.bids:
        s = (mid - lv.price) * 100.0
        if 0 <= s <= v_cents and lv.size >= min_size:
            qb += order_score(v_cents, s) * lv.size
            db += lv.size * lv.price
    for lv in book.asks:
        s = (lv.price - mid) * 100.0
        if 0 <= s <= v_cents and lv.size >= min_size:
            qa += order_score(v_cents, s) * lv.size
            da += lv.size * (1.0 - lv.price)
    return {"q_bid": qb, "q_ask": qa, "depth_bid": db, "depth_ask": da}


def competitor_total(q_bid: float, q_ask: float, mid: float) -> tuple[float, float]:
    """(low, high) for the sum of every other maker's Q, from the book's side totals."""
    if 0.10 <= mid <= 0.90:
        high = min(q_bid, q_ask) + abs(q_bid - q_ask) / 3.0
        low = (q_bid + q_ask) / 6.0
    else:
        high = low = min(q_bid, q_ask)
    return low, high


def our_share(q_ours: float, q_bid: float, q_ask: float, mid: float) -> tuple[float, float]:
    """(low, high) share of the pot for a balanced two-sided quote scoring `q_ours`."""
    if q_ours <= 0:
        return 0.0, 0.0
    lo_comp, hi_comp = competitor_total(q_bid, q_ask, mid)
    return q_ours / (q_ours + hi_comp), q_ours / (q_ours + lo_comp)


# --------------------------------------------------------------------------- #
# Per-market evaluation
# --------------------------------------------------------------------------- #

@dataclass
class QuoteCase:
    label: str
    s_cents: float  # distance of each side from the mid
    size: float  # shares per side
    share_low: float
    share_high: float
    reward_low: float  # $/day
    reward_high: float
    fills_per_day: float
    filled_per_day: float  # $ notional filled per day
    loss_per_day: float  # adverse selection, $/day (positive = we lose)
    rebate_per_day: float
    net_low: float
    net_high: float
    capital: float  # $ resting on both sides


@dataclass
class RewardRow:
    condition_id: str
    question: str
    rate_per_day: float
    max_spread: float
    min_size: float
    mid: float | None
    spread: float | None
    q_bid: float
    q_ask: float
    depth_bid: float
    depth_ask: float
    volume_24h: float
    fee_rate: float | None
    rebate_rate: float
    tape_days: float
    tape_trades: int
    cases: list[QuoteCase] = field(default_factory=list)
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tape_window(trades: list[dict[str, Any]], outcomes: list[str] | None, days: float, now: datetime) -> tuple[list[dict[str, Any]], float]:
    """Trades from the last `days` (or the whole tape if shorter) and the span they cover."""
    since = int((now - timedelta(days=days)).timestamp())
    kept = [t for t in trades if isinstance(t.get("timestamp"), (int, float, str)) and int(t["timestamp"]) >= since]
    series = yes_price_series(kept, outcomes)
    if len(series) < 2:
        return kept, 0.0
    span = max(1.0 / 24.0, (series[-1][0] - series[0][0]) / 86400.0)
    return kept, span


def evaluate_market(cfg: dict[str, Any], market: PolyMarket, book: Book, trades: list[dict[str, Any]], *, now: datetime, days: float = 14.0, sizes: tuple[float, ...] = (1.0, 5.0), rebate_rate: float | None = None) -> RewardRow | None:
    rate = float(cfg.get("total_daily_rate") or cfg.get("rate_per_day") or 0.0)
    v = float(cfg.get("rewards_max_spread") or 0.0)
    min_size = float(cfg.get("rewards_min_size") or 0.0)
    if rate <= 0 or v <= 0 or book.best_bid is None or book.best_ask is None:
        return None
    bid, ask = book.best_bid.price, book.best_ask.price
    mid = (bid + ask) / 2.0
    spread = ask - bid
    comp = book_competition(book, mid, v, min_size)
    rb = rebate_rate if rebate_rate is not None else (float(market.raw.get("feeSchedule", {}).get("rebateRate")) if isinstance(market.raw.get("feeSchedule"), dict) and market.raw["feeSchedule"].get("rebateRate") is not None else DEFAULT_REBATE)
    fee_rate = market.fee_rate if market.fee_rate is not None else 0.0
    kept, span = _tape_window(trades, market.outcomes, days, now)
    mark = yes_price_series(kept, market.outcomes)[-1][1] if span > 0 else mid
    row = RewardRow(
        condition_id=cfg.get("condition_id", ""), question=market.question, rate_per_day=rate, max_spread=v, min_size=min_size,
        mid=round(mid, 4), spread=round(spread, 4), q_bid=comp["q_bid"], q_ask=comp["q_ask"], depth_bid=comp["depth_bid"], depth_ask=comp["depth_ask"],
        volume_24h=market.volume_24h, fee_rate=market.fee_rate, rebate_rate=rb, tape_days=round(span, 2), tape_trades=len(kept), url=market.url,
    )
    # two placements: joining the touch (half the current spread from the mid, at least one tick) and half the max spread
    placements = [("touch", max(0.5, spread * 100.0 / 2.0)), ("half-max", v / 2.0)]
    for label, s in placements:
        if s > v:
            continue
        for mult in sizes:
            size = max(min_size, 5.0) * mult
            q = order_score(v, s) * size
            lo, hi = our_share(q, comp["q_bid"], comp["q_ask"], mid)
            half = max(0.01, round(s / 100.0, 2))
            sim = simulate_maker(kept, mark, market.outcomes, MakerConfig(half_spread=half, size=size, min_hours=0.0, max_position=5 * size, fill="through")) if span > 0 else {"fills": 0, "dollars": 0.0, "pnl": 0.0, "fee_equiv": 0.0}
            per_day = 1.0 / span if span > 0 else 0.0
            loss = -sim["pnl"] * per_day
            rebate = sim["fee_equiv"] * fee_rate * rb * per_day
            row.cases.append(QuoteCase(
                label=f"{label} x{mult:g}", s_cents=round(s, 2), size=size, share_low=lo, share_high=hi,
                reward_low=rate * lo, reward_high=rate * hi, fills_per_day=sim["fills"] * per_day, filled_per_day=sim["dollars"] * per_day,
                loss_per_day=loss, rebate_per_day=rebate, net_low=rate * lo + rebate - loss, net_high=rate * hi + rebate - loss, capital=size,
            ))
    return row


# --------------------------------------------------------------------------- #
# Sampling and reporting
# --------------------------------------------------------------------------- #

def sample_configs(configs: list[dict[str, Any]], *, top: int = 30, mid_n: int = 30, low_n: int = 30, seed: int = 7, mid_band: tuple[float, float] = (10.0, 100.0), low_band: tuple[float, float] = (1.0, 10.0)) -> list[dict[str, Any]]:
    """The biggest pots plus random draws from the middle and the bottom of the rate table."""
    rng = random.Random(seed)
    by_rate = sorted(configs, key=lambda c: -(c.get("total_daily_rate") or 0))
    picked = by_rate[:top]
    chosen = {c["condition_id"] for c in picked}
    mid = [c for c in configs if mid_band[0] <= (c.get("total_daily_rate") or 0) < mid_band[1] and c["condition_id"] not in chosen]
    low = [c for c in configs if low_band[0] <= (c.get("total_daily_rate") or 0) < low_band[1] and c["condition_id"] not in chosen]
    picked += rng.sample(mid, min(mid_n, len(mid))) + rng.sample(low, min(low_n, len(low)))
    return picked


@dataclass
class RewardsReport:
    when: str
    markets_with_rewards: int
    total_daily_rate: float
    sampled: int
    evaluated: int
    rows: list[RewardRow]
    days: float

    def to_dict(self) -> dict[str, Any]:
        return {"when": self.when, "markets_with_rewards": self.markets_with_rewards, "total_daily_rate": self.total_daily_rate, "sampled": self.sampled, "evaluated": self.evaluated, "days": self.days, "rows": [r.to_dict() for r in self.rows]}


def rewards_survey(http: HttpClient, trades_source, book_fetcher, *, now: datetime, days: float = 14.0, top: int = 30, mid_n: int = 30, low_n: int = 30, seed: int = 7, configs: list[dict[str, Any]] | None = None, log=None) -> RewardsReport:
    configs = configs if configs is not None else fetch_reward_configs(http)
    total = sum(float(c.get("total_daily_rate") or 0) for c in configs)
    picked = sample_configs(configs, top=top, mid_n=mid_n, low_n=low_n, seed=seed)
    if log:
        log(f"{len(configs)} rewarded markets, ${total:,.0f}/day nominal; {len(picked)} sampled")
    markets = gamma_markets_by_condition(http, [c["condition_id"] for c in picked])
    live = [(c, markets[c["condition_id"]]) for c in picked if c["condition_id"] in markets and markets[c["condition_id"]].yes_token]
    books = book_fetcher([m.yes_token for _, m in live])
    rows: list[RewardRow] = []
    for i, (c, m) in enumerate(live):
        book = books.get(m.yes_token or "")
        if book is None:
            continue
        try:
            trades = trades_source.trades(m.condition_id, closed=True)
        except Exception as exc:  # one bad tape should not sink the survey
            if log:
                log(f"  trades failed for {m.question[:50]}: {exc}")
            continue
        row = evaluate_market(c, m, book, trades, now=now, days=days)
        if row is not None:
            rows.append(row)
        if log and (i + 1) % 20 == 0:
            log(f"  {i + 1}/{len(live)} markets evaluated")
    rows.sort(key=lambda r: -r.rate_per_day)
    return RewardsReport(when=now.isoformat(timespec="seconds"), markets_with_rewards=len(configs), total_daily_rate=total, sampled=len(picked), evaluated=len(rows), rows=rows, days=days)


# --------------------------------------------------------------------------- #
# Books-only scan of every rewarded market: where is the pot unclaimed?
# --------------------------------------------------------------------------- #

@dataclass
class PocketRow:
    condition_id: str
    question: str
    rate_per_day: float
    max_spread: float
    min_size: float
    mid: float
    spread: float
    q_bid: float
    q_ask: float
    share_low: float  # of a min-size two-sided quote at half the max spread
    share_high: float
    reward_low: float
    reward_high: float
    capital: float
    liquidity: float
    volume_24h: float
    days_to_end: float | None
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pocket_scan(configs: list[dict[str, Any]], markets: dict[str, PolyMarket], books: dict[str, Book], *, now: datetime) -> list[PocketRow]:
    """For every rewarded market with a two-sided book: the pot share a minimum-size quote
    resting at half the max spread would take against the orders on the book right now."""
    rows: list[PocketRow] = []
    for c in configs:
        m = markets.get(c.get("condition_id", ""))
        if m is None or not m.yes_token or not m.accepting_orders:
            continue
        b = books.get(m.yes_token)
        if b is None or b.best_bid is None or b.best_ask is None:
            continue
        rate = float(c.get("total_daily_rate") or 0.0)
        v = float(c.get("rewards_max_spread") or 0.0)
        ms = float(c.get("rewards_min_size") or 0.0)
        if rate <= 0 or v <= 0:
            continue
        mid = (b.best_bid.price + b.best_ask.price) / 2.0
        comp = book_competition(b, mid, v, ms)
        size = max(ms, 5.0)
        lo, hi = our_share(order_score(v, v / 2.0) * size, comp["q_bid"], comp["q_ask"], mid)
        days = (m.end_date - now).total_seconds() / 86400.0 if m.end_date else None
        rows.append(PocketRow(c["condition_id"], m.question, rate, v, ms, round(mid, 4), round(b.best_ask.price - b.best_bid.price, 4), comp["q_bid"], comp["q_ask"], lo, hi, rate * lo, rate * hi, size, m.liquidity, m.volume_24h, round(days, 1) if days is not None else None, m.url))
    rows.sort(key=lambda r: -r.reward_low)
    return rows


def rewards_pocket(http: HttpClient, book_fetcher, *, now: datetime, min_rate: float = 10.0, configs: list[dict[str, Any]] | None = None, log=None) -> list[PocketRow]:
    configs = [c for c in (configs if configs is not None else fetch_reward_configs(http)) if float(c.get("total_daily_rate") or 0) >= min_rate]
    markets = gamma_markets_by_condition(http, [c["condition_id"] for c in configs])
    if log:
        log(f"{len(configs)} rewarded markets at >= ${min_rate:g}/day, {len(markets)} found on Gamma; fetching books")
    books = book_fetcher([m.yes_token for m in markets.values() if m.yes_token])
    return pocket_scan(configs, markets, books, now=now)


def render_pocket(rows: list[PocketRow], *, top: int = 40, min_days: float = 7.0) -> str:
    if not rows:
        return "rewards pocket: no two-sided books"
    pot = sum(r.rate_per_day for r in rows)
    empty = [r for r in rows if r.q_bid < 1 and r.q_ask < 1]
    quarter = [r for r in rows if r.share_low >= 0.25]
    L = [f"Rewarded markets with a two-sided book: {len(rows)}, pots ${pot:,.0f}/day.",
         f"  no qualifying order within the max spread on either side: {len(empty)} markets, ${sum(r.rate_per_day for r in empty):,.0f}/day of pots",
         f"  a minimum-size quote at half the max spread would take >= 25% of the pot in {len(quarter)} markets: ${sum(r.reward_low for r in quarter):,.0f}/day on ${sum(r.capital for r in quarter):,.0f} of capital",
         f"    of which resolving within 2 days: {sum(1 for r in quarter if r.days_to_end is not None and r.days_to_end < 2)} (${sum(r.reward_low for r in quarter if r.days_to_end is not None and r.days_to_end < 2):,.0f}/day); {min_days:g}+ days out: {sum(1 for r in quarter if r.days_to_end is not None and r.days_to_end >= min_days)} (${sum(r.reward_low for r in quarter if r.days_to_end is not None and r.days_to_end >= min_days):,.0f}/day)"]
    for k in (5, 10, 20, 50, 100):
        t = rows[:k]
        if len(t) == k:
            L.append(f"  top {k:3} by reward: ${sum(r.reward_low for r in t):,.0f}..{sum(r.reward_high for r in t):,.0f}/day on ${sum(r.capital for r in t):,.0f} capital")
    long_ = [r for r in rows if r.days_to_end is not None and r.days_to_end >= min_days and r.spread <= 0.5 and 0.05 <= r.mid <= 0.95][:top]
    L.append("")
    L.append(f"  {min_days:g}+ days to resolution, spread <= 50c, mid in 5-95c (a live test would start here):")
    L.append(f"  {'reward lo..hi':>14} {'rate':>5} {'min':>4} {'mid':>5} {'sprd':>5} {'Q b/a':>9} {'liq $':>7} {'v24 $':>6} {'days':>5}  question")
    for r in long_:
        L.append(f"  {r.reward_low:6.1f}..{r.reward_high:<6.1f} {r.rate_per_day:5.0f} {r.min_size:4.0f} {r.mid:5.2f} {r.spread:5.2f} {r.q_bid:4.0f}/{r.q_ask:<4.0f} {r.liquidity:7.0f} {r.volume_24h:6.0f} {r.days_to_end:5.0f}  {r.question[:60]}")
    return "\n".join(L)


def _tier(rate: float) -> str:
    return ">=100/day" if rate >= 100 else ("10-100/day" if rate >= 10 else "1-10/day")


def render_rewards(r: RewardsReport, *, case: str = "touch x1", top: int = 25) -> str:
    L = [f"Liquidity rewards at {r.when}: {r.markets_with_rewards:,} markets carry a reward, ${r.total_daily_rate:,.0f}/day nominal; {r.evaluated} sampled markets evaluated on live books and their last {r.days:g} days of tape.",
         "A quote is two-sided, `size` shares each side, either joining the touch or resting at half the max spread; reward share is a range (competitors all one-sided .. all balanced).",
         "`loss` is adverse selection from the paper maker (prints through the quote, marked to the last print), `rebate` the maker fee rebate on those fills. All $/day.", ""]
    for cs in sorted({c.label for row in r.rows for c in row.cases}, key=lambda s: (s.split()[0] != "touch", s)):
        rows = [(row, c) for row in r.rows for c in row.cases if c.label == cs]
        if not rows:
            continue
        L.append(f"--- quote: {cs} ---")
        L.append(f"  {'tier':>10} {'mkts':>4} {'reward lo..hi':>14} {'fills/d':>7} {'filled $/d':>10} {'loss $/d':>9} {'rebate':>7} {'net lo..hi $/d':>16} {'capital $':>9}")
        for tier in (">=100/day", "10-100/day", "1-10/day"):
            sub = [(row, c) for row, c in rows if _tier(row.rate_per_day) == tier]
            if not sub:
                continue
            L.append(f"  {tier:>10} {len(sub):4} {sum(c.reward_low for _, c in sub):6.2f}..{sum(c.reward_high for _, c in sub):<6.2f} {sum(c.fills_per_day for _, c in sub):7.1f} {sum(c.filled_per_day for _, c in sub):10.0f} {sum(c.loss_per_day for _, c in sub):9.2f} {sum(c.rebate_per_day for _, c in sub):7.2f} {sum(c.net_low for _, c in sub):7.2f}..{sum(c.net_high for _, c in sub):<7.2f} {sum(c.capital for _, c in sub):9.0f}")
        best = sorted(rows, key=lambda rc: -rc[1].net_high)[:top] if cs == case else []
        if best:
            L.append("")
            L.append(f"  best {len(best)} markets by the optimistic net ({cs}):")
            L.append(f"  {'rate':>6} {'v':>4} {'min':>4} {'mid':>5} {'sprd':>5} {'comp Q b/a':>13} {'share lo..hi':>13} {'reward':>12} {'fills/d':>7} {'loss':>6} {'net lo..hi':>12}  question")
            for row, c in best:
                L.append(f"  {row.rate_per_day:6.0f} {row.max_spread:4.1f} {row.min_size:4.0f} {row.mid:5.2f} {row.spread:5.3f} {row.q_bid:6.0f}/{row.q_ask:<6.0f} {c.share_low * 100:5.1f}..{c.share_high * 100:<5.1f}% {c.reward_low:5.2f}..{c.reward_high:<5.2f} {c.fills_per_day:7.1f} {c.loss_per_day:6.2f} {c.net_low:5.2f}..{c.net_high:<5.2f}  {row.question[:60]}")
        L.append("")
    return "\n".join(L)
