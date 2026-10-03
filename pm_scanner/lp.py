"""Liquidity-provider test (MEMO.md section 18): rest a two-sided, minimum-size, post-only
quote in a few rewarded markets nobody else quotes, keep it centred on the mid, and read
back whether the CLOB counts it as scoring and what it earns.

This is a test rig, not a trading bot. What it will never do: send a market order, cross the
spread (every order is post-only), quote more than the cash budget, add to a filled side, or
keep orders up past the pull-before-end window. Everything it sends and sees goes to a
JSONL log. Credentials come from the environment only (see .env.example); without them the
`--dry-run` path plans and prices the quotes from public data and stops.

Order mechanics: a YES bid is a BUY on the YES token at (mid − h); a YES ask is a BUY on the
NO token at 1 − (mid + h), which the book shows as a sell of YES. Both rest at half the
market's max reward spread (h = v/2), the size is the market's reward minimum, so the
collateral parked per market is about size × (bid + no_bid) ≈ size × (1 − 2h) dollars.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .polymarket import PolyMarket
from .rewards import PocketRow

EXCLUDE_WORDS = ("temperature", "weather", "earthquake", "video", "posts from", "tweets", "views", "hurricane", "category", "precipitation")


def floor_tick(x: float, tick: float) -> float:
    return math.floor(x / tick + 1e-9) * tick


def ceil_tick(x: float, tick: float) -> float:
    return math.ceil(x / tick - 1e-9) * tick


def _dec(tick: float) -> int:
    return max(0, -int(math.floor(math.log10(tick) + 1e-9)))


@dataclass
class QuotePlan:
    condition_id: str
    question: str
    yes_token: str
    no_token: str
    tick: float
    size: float  # shares per side
    half_spread: float  # dollars from the mid
    mid: float
    bid: float  # BUY YES here
    no_bid: float  # BUY NO here (= 1 - YES ask)
    collateral: float
    rate_per_day: float
    reward_low: float
    reward_high: float
    days_to_end: float | None
    url: str = ""

    @property
    def ask(self) -> float:
        return round(1.0 - self.no_bid, _dec(self.tick))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def quote_prices(mid: float, half_spread: float, tick: float) -> tuple[float, float]:
    """(YES bid, NO bid) for a quote centred on `mid`, rounded outward to the tick and kept
    inside 1c..99c."""
    d = _dec(tick)
    bid = round(min(max(floor_tick(mid - half_spread, tick), tick), 1.0 - tick), d)
    ask = round(min(max(ceil_tick(mid + half_spread, tick), tick), 1.0 - tick), d)
    if ask <= bid:
        ask = round(bid + tick, d)
    return bid, round(1.0 - ask, d)


def make_plan(row: PocketRow, market: PolyMarket, tick: float = 0.01, mid: float | None = None) -> QuotePlan | None:
    if not market.yes_token or not market.no_token:
        return None
    m = row.mid if mid is None else mid
    h = row.max_spread / 100.0 / 2.0
    bid, no_bid = quote_prices(m, h, tick)
    size = float(math.ceil(max(row.min_size, market.order_min_size, 5.0)))
    return QuotePlan(
        condition_id=row.condition_id, question=row.question, yes_token=market.yes_token, no_token=market.no_token, tick=tick,
        size=size, half_spread=h, mid=round(m, 4), bid=bid, no_bid=no_bid, collateral=round(size * (bid + no_bid), 2),
        rate_per_day=row.rate_per_day, reward_low=row.reward_low, reward_high=row.reward_high, days_to_end=row.days_to_end, url=row.url,
    )


def choose_markets(rows: list[PocketRow], markets: dict[str, PolyMarket], *, budget: float, max_markets: int, min_days: float = 7.0, max_spread: float = 0.5, min_reward: float = 20.0, exclude_words: tuple[str, ...] = EXCLUDE_WORDS, only: set[str] | None = None, per_market: float = 0.0, reasons: dict[str, int] | None = None) -> list[QuotePlan]:
    """The best pots that fit the budget: long-dated, not too wide, not news-driven families.
    `per_market` caps the collateral of one market (0 = 1.6 × budget / max_markets, so a
    50-share market does not swallow a budget meant for three 20-share ones). `reasons`, when
    given, collects why each candidate was passed over."""
    plans: list[QuotePlan] = []
    spent = 0.0
    cap = per_market if per_market > 0 else 1.6 * budget / max(1, max_markets)
    if reasons is not None:
        reasons["per-market cap $"] = round(cap, 2)

    def skip(why: str) -> None:
        if reasons is not None:
            reasons[why] = reasons.get(why, 0) + 1

    for r in sorted(rows, key=lambda r: -r.reward_low):
        if only is not None and r.condition_id not in only:
            continue
        if only is None:
            if r.days_to_end is None or r.days_to_end < min_days:
                skip(f"ends within {min_days:g} days")
                continue
            if r.spread > max_spread:
                skip(f"book spread above {max_spread:g}")
                continue
            if not 0.05 <= r.mid <= 0.95:
                skip("mid outside 0.05..0.95")
                continue
            if r.reward_low < min_reward:
                skip(f"modelled reward below ${min_reward:g}/day")
                continue
            if any(w in r.question.lower() for w in exclude_words):
                skip("excluded family")
                continue
        m = markets.get(r.condition_id)
        if m is None or not m.accepting_orders:
            skip("not on Gamma or not accepting orders")
            continue
        p = make_plan(r, m)
        if p is None:
            skip("no token ids")
            continue
        if only is None and p.collateral > cap:
            skip(f"minimum quote needs more than the per-market cap (${cap:.2f})")
            continue
        if spent + p.collateral > budget:
            skip("over the remaining budget")
            continue
        plans.append(p)
        spent += p.collateral
        if len(plans) >= max_markets:
            break
    return plans


# --------------------------------------------------------------------------- #
# Exchange adapters
# --------------------------------------------------------------------------- #

@dataclass
class Order:
    id: str
    token: str
    price: float
    size: float
    matched: float = 0.0


class Exchange(Protocol):
    def top_of_book(self, token: str) -> tuple[float | None, float | None]: ...
    def place(self, token: str, price: float, size: float) -> str | None: ...
    def cancel(self, order_ids: list[str]) -> None: ...
    def cancel_all(self) -> None: ...
    def open_orders(self) -> list[Order]: ...
    def scoring(self, order_ids: list[str]) -> dict[str, bool]: ...
    def earnings(self, date: str) -> dict[str, float]: ...
    def market_rewards(self, condition_id: str) -> dict[str, Any]: ...


class PaperExchange:
    """Public books only; orders are recorded, never sent. Used by --dry-run and the tests."""

    def __init__(self, books: dict[str, tuple[float | None, float | None]] | None = None, book_fn=None) -> None:
        self.books = books or {}
        self.book_fn = book_fn
        self.orders: dict[str, Order] = {}
        self.n = 0
        self.log: list[tuple[str, Any]] = []

    def top_of_book(self, token: str) -> tuple[float | None, float | None]:
        if self.book_fn is not None:
            return self.book_fn(token)
        return self.books.get(token, (None, None))

    def place(self, token: str, price: float, size: float) -> str | None:
        self.n += 1
        oid = f"paper-{self.n}"
        self.orders[oid] = Order(oid, token, price, size)
        self.log.append(("place", (token, price, size)))
        return oid

    def cancel(self, order_ids: list[str]) -> None:
        for oid in order_ids:
            self.orders.pop(oid, None)
        self.log.append(("cancel", list(order_ids)))

    def cancel_all(self) -> None:
        self.orders.clear()
        self.log.append(("cancel_all", None))

    def open_orders(self) -> list[Order]:
        return list(self.orders.values())

    def scoring(self, order_ids: list[str]) -> dict[str, bool]:
        return {oid: True for oid in order_ids}

    def earnings(self, date: str) -> dict[str, float]:
        return {}

    def market_rewards(self, condition_id: str) -> dict[str, Any]:
        return {}


class LiveExchange:
    """The official `polymarket-client` SecureClient behind the small interface above.
    Every order is a post-only GTC limit order; nothing here can take liquidity."""

    def __init__(self, client) -> None:
        self.client = client

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LiveExchange":
        env = os.environ if env is None else env
        key = env.get("POLY_PRIVATE_KEY", "").strip()
        wallet = env.get("POLY_WALLET", "").strip() or None
        relayer_key = env.get("POLY_RELAYER_KEY", "").strip()
        relayer_addr = env.get("POLY_RELAYER_ADDRESS", "").strip()
        if not key:
            raise RuntimeError("POLY_PRIVATE_KEY is not set (see .env.example)")
        from polymarket import RelayerApiKey, SecureClient  # imported here so the read-only commands never need it

        api_key = RelayerApiKey(key=relayer_key, address=relayer_addr) if relayer_key and relayer_addr else None
        return cls(SecureClient.create(private_key=key, wallet=wallet, api_key=api_key))

    def describe(self) -> dict[str, Any]:
        c = self.client
        out: dict[str, Any] = {"wallet": str(getattr(c, "wallet", "")), "wallet_type": str(getattr(c, "wallet_type", ""))}
        try:
            ba = c.get_balance_allowance(asset_type="COLLATERAL")
            out["collateral_balance"] = float(ba.balance) / 1e6 if float(ba.balance) > 1e4 else float(ba.balance)
            out["allowances"] = {k: str(v) for k, v in dict(ba.allowances).items()} if ba.allowances else {}
        except Exception as exc:  # noqa: BLE001
            out["collateral_balance_error"] = str(exc)
        try:
            st = c.get_trading_approvals_state()
            out["approvals_ok"] = bool(st.is_fully_approved)
            miss = st.missing
            items = list(miss) if isinstance(miss, (list, tuple)) else [*getattr(miss, "erc20", ()), *getattr(miss, "erc1155", ())]
            out["approvals_missing"] = [str(x) for x in items]
        except Exception as exc:  # noqa: BLE001
            out["approvals_error"] = str(exc)
        try:
            out["gasless_ready"] = bool(c.is_gasless_ready())
        except Exception as exc:  # noqa: BLE001
            out["gasless_error"] = str(exc)
        return out

    def top_of_book(self, token: str) -> tuple[float | None, float | None]:
        ob = self.client.get_order_book(token_id=token)
        bids = [float(l.price) for l in ob.bids]
        asks = [float(l.price) for l in ob.asks]
        return (max(bids) if bids else None, min(asks) if asks else None)

    def place(self, token: str, price: float, size: float) -> str | None:
        r = self.client.place_limit_order(token_id=token, side="BUY", price=f"{price:.4f}".rstrip("0").rstrip("."), size=f"{size:g}", post_only=True)
        if getattr(r, "ok", False):
            return str(r.order_id)
        raise RuntimeError(f"order rejected: {getattr(r, 'code', '?')} {getattr(r, 'message', '')}")

    def cancel(self, order_ids: list[str]) -> None:
        if order_ids:
            self.client.cancel_orders(order_ids=list(order_ids))

    def cancel_all(self) -> None:
        self.client.cancel_all()

    def open_orders(self) -> list[Order]:
        out = []
        for page in self.client.list_open_orders():
            for o in page.items:
                out.append(Order(str(o.id), str(o.asset_id), float(o.price), float(o.original_size), float(o.size_matched)))
        return out

    def scoring(self, order_ids: list[str]) -> dict[str, bool]:
        if not order_ids:
            return {}
        return {str(k): bool(v) for k, v in self.client.get_orders_scoring(order_ids=list(order_ids)).items()}

    def earnings(self, date: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for page in self.client.list_user_earnings_for_day(date=date):
            for e in page.items:
                out[str(e.condition_id)] = out.get(str(e.condition_id), 0.0) + float(e.earnings)
        return out

    def market_rewards(self, condition_id: str) -> dict[str, Any]:
        """The market's current pot and Polymarket's own competitiveness figure."""
        for page in self.client.list_market_rewards(condition_id=condition_id):
            for m in page.items:
                cfg = list(m.rewards_config or [])
                return {"rate_per_day": sum(float(getattr(c, "rate_per_day", 0) or 0) for c in cfg), "competitiveness": float(m.market_competitiveness)}
        return {}


# --------------------------------------------------------------------------- #
# The quoter
# --------------------------------------------------------------------------- #

@dataclass
class MarketState:
    plan: QuotePlan
    bid_id: str | None = None
    ask_id: str | None = None
    quoted_mid: float | None = None
    bid_filled: float = 0.0  # YES shares bought
    ask_filled: float = 0.0  # NO shares bought
    bid_done: bool = False  # a side is retired once it has been filled: never average in
    ask_done: bool = False
    replaced: int = 0
    stopped: str = ""


class Quoter:
    def __init__(self, exchange: Exchange, plans: list[QuotePlan], *, log_path: Path | None = None, pull_before_end_hours: float = 48.0, recentre_ticks: float = 1.0, clock=time.time, printer=print) -> None:
        self.x = exchange
        self.states = [MarketState(p) for p in plans]
        self.log_path = log_path
        self.pull_before_end_hours = pull_before_end_hours
        self.recentre_ticks = recentre_ticks
        self.clock = clock
        self.printer = printer
        self.started = clock()

    def log(self, kind: str, **data: Any) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **data}
        if self.log_path:
            with self.log_path.open("a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        if kind in ("place", "cancel", "fill", "stop", "scoring", "earnings", "pots", "error", "start", "end"):
            self.printer(json.dumps(rec, default=str))

    def _hours_left(self, st: MarketState) -> float | None:
        if st.plan.days_to_end is None:
            return None
        return st.plan.days_to_end * 24.0 - (self.clock() - self.started) / 3600.0

    def _post(self, st: MarketState, mid: float) -> None:
        p = st.plan
        bid, no_bid = quote_prices(mid, p.half_spread, p.tick)
        try:
            if not st.bid_done:
                st.bid_id = self.x.place(p.yes_token, bid, p.size)
                self.log("place", market=p.question[:60], side="YES bid", price=bid, size=p.size, order_id=st.bid_id)
            if not st.ask_done:
                st.ask_id = self.x.place(p.no_token, no_bid, p.size)
                self.log("place", market=p.question[:60], side="NO bid (YES ask)", price=no_bid, yes_ask=round(1 - no_bid, 4), size=p.size, order_id=st.ask_id)
            st.quoted_mid = mid
        except Exception as exc:  # noqa: BLE001
            self.log("error", market=p.question[:60], error=str(exc))

    def _pull(self, st: MarketState, reason: str) -> None:
        ids = [i for i in (st.bid_id, st.ask_id) if i]
        if ids:
            self.x.cancel(ids)
            self.log("cancel", market=st.plan.question[:60], order_ids=ids, reason=reason)
        st.bid_id = st.ask_id = None

    def step(self) -> dict[str, Any]:
        open_by_id = {o.id: o for o in self.x.open_orders()}
        summary: dict[str, Any] = {"active": 0, "stopped": 0}
        for st in self.states:
            p = st.plan
            if st.stopped:
                summary["stopped"] += 1
                continue
            hours = self._hours_left(st)
            if hours is not None and hours < self.pull_before_end_hours:
                self._pull(st, "resolution window")
                st.stopped = "resolution window"
                self.log("stop", market=p.question[:60], reason=st.stopped)
                continue
            # fills: an order that vanished with matched size, or that reports matched size
            for side in ("bid", "ask"):
                oid = getattr(st, f"{side}_id")
                if not oid:
                    continue
                o = open_by_id.get(oid)
                if o is None or o.matched > 0:
                    matched = o.matched if o else p.size
                    if matched > 0:
                        setattr(st, f"{side}_filled", getattr(st, f"{side}_filled") + matched)
                        setattr(st, f"{side}_done", True)
                        self.log("fill", market=p.question[:60], side=side, shares=matched, price=(p.bid if side == "bid" else p.no_bid))
                        if o is not None:  # partially filled and still resting: take the rest down, never average in
                            self.x.cancel([oid])
                            open_by_id.pop(oid, None)
                    setattr(st, f"{side}_id", None)
            if st.bid_done and st.ask_done:
                self._pull(st, "both sides filled")
                st.stopped = "both sides filled"
                self.log("stop", market=p.question[:60], reason=st.stopped)
                continue
            bb, ba = self.x.top_of_book(p.yes_token)
            if bb is None or ba is None:
                self.log("error", market=p.question[:60], error="one-sided or empty book, waiting")
                continue
            mid = (bb + ba) / 2.0
            have_bid = st.bid_id is not None and st.bid_id in open_by_id
            have_ask = st.ask_id is not None and st.ask_id in open_by_id
            need_bid, need_ask = not st.bid_done and not have_bid, not st.ask_done and not have_ask
            moved = st.quoted_mid is None or abs(mid - st.quoted_mid) >= self.recentre_ticks * p.tick - 1e-9
            if moved and (have_bid or have_ask):
                self._pull(st, f"re-centre: mid {st.quoted_mid} -> {round(mid, 4)}")
                st.replaced += 1
                need_bid, need_ask = not st.bid_done, not st.ask_done
            if need_bid or need_ask:
                if need_bid and have_bid:
                    self.x.cancel([st.bid_id]); st.bid_id = None  # type: ignore[list-item]
                if need_ask and have_ask:
                    self.x.cancel([st.ask_id]); st.ask_id = None  # type: ignore[list-item]
                self._post(st, mid)
            summary["active"] += 1
        return summary

    def report(self, date: str) -> None:
        ids = [i for st in self.states for i in (st.bid_id, st.ask_id) if i]
        try:
            sc = self.x.scoring(ids)
            self.log("scoring", orders=len(ids), scoring=sum(1 for v in sc.values() if v), detail=sc)
        except Exception as exc:  # noqa: BLE001
            self.log("error", error=f"scoring: {exc}")
        try:
            e = self.x.earnings(date)
            mine = {st.plan.question[:50]: round(e.get(st.plan.condition_id, 0.0), 4) for st in self.states}
            self.log("earnings", date=date, total=round(sum(e.values()), 4), by_market=mine)
        except Exception as exc:  # noqa: BLE001
            self.log("error", error=f"earnings: {exc}")
        try:  # the pot is re-set hourly and may respond to our presence: record it alongside the earnings
            pots = {st.plan.question[:50]: self.x.market_rewards(st.plan.condition_id) for st in self.states if not st.stopped}
            if any(pots.values()):
                self.log("pots", by_market=pots)
        except Exception as exc:  # noqa: BLE001
            self.log("error", error=f"market rewards: {exc}")

    def run(self, *, hours: float, interval: float = 60.0, report_every: int = 10, sleep=time.sleep) -> None:
        self.log("start", markets=[st.plan.to_dict() for st in self.states], hours=hours, interval=interval)
        end = self.clock() + hours * 3600.0
        i = 0
        try:
            while self.clock() < end and any(not st.stopped for st in self.states):
                s = self.step()
                if i % report_every == 0:
                    self.report(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                i += 1
                sleep(interval)
        finally:
            try:
                self.x.cancel_all()
            finally:
                self.log("end", states=[{k: v for k, v in asdict(st).items() if k != "plan"} | {"market": st.plan.question[:60]} for st in self.states])


def render_plan(plans: list[QuotePlan]) -> str:
    if not plans:
        return "no market fits the budget and filters"
    L = [f"  {'reward lo..hi':>14} {'rate':>5} {'mid':>5} {'bid':>5} {'ask':>5} {'size':>4} {'collat':>7} {'days':>5}  question"]
    for p in plans:
        L.append(f"  {p.reward_low:6.1f}..{p.reward_high:<6.1f} {p.rate_per_day:5.0f} {p.mid:5.2f} {p.bid:5.2f} {p.ask:5.2f} {p.size:4.0f} {p.collateral:7.2f} {p.days_to_end if p.days_to_end is not None else float('nan'):5.0f}  {p.question[:60]}")
    L.append(f"  total collateral parked: ${sum(p.collateral for p in plans):.2f} in {len(plans)} markets; modelled reward ${sum(p.reward_low for p in plans):.0f}..{sum(p.reward_high for p in plans):.0f}/day")
    return "\n".join(L)


def parse_only(text: str | None) -> set[str] | None:
    if not text:
        return None
    return {t.strip() for t in re.split(r"[,\s]+", text) if t.strip()}
