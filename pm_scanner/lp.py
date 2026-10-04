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

from .http import HttpClient
from .polymarket import PolyMarket
from .rewards import PocketRow
from .unwind import Unwind, _dec, ceil_tick, floor_tick, no_bids_from_yes_asks, quote_prices, unwind_into  # noqa: F401  (quote_prices and the tick helpers are re-exported for the tests and the CLI)

EXCLUDE_WORDS = ("temperature", "weather", "earthquake", "video", "posts from", "tweets", "views", "hurricane", "category", "precipitation", "rain", "snow", "storm", "wind", "flood", "grand prix")


def best_prices(bids: list[tuple[float, float]], asks: list[tuple[float, float]], own_bids: list[tuple[float, float]] | None = None, own_asks: list[tuple[float, float]] | None = None) -> tuple[float | None, float | None]:
    """Best bid and ask of everyone else: our own resting orders are subtracted from their levels.
    Alone in a book, our two quotes would otherwise define the mid we re-centre on, and a fill on
    one side would read as a move (the smoke test of 3 Oct chased itself that way)."""

    def others(levels: list[tuple[float, float]], own: list[tuple[float, float]] | None) -> list[float]:
        out = []
        for price, size in levels:
            for op, osz in own or ():
                if abs(op - price) < 1e-9:
                    size -= osz
            if size > 1e-6:
                out.append(price)
        return out

    b, a = others(bids, own_bids), others(asks, own_asks)
    return (max(b) if b else None, min(a) if a else None)


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
    exit_cost: float | None = None  # dollars lost undoing one full fill on the worse side into the bids left below the quote at planning, fee included; None = no exit
    exit_yes: float | None = None  # net per share the bids pay for the YES side's shares
    exit_no: float | None = None  # net per share the NO bids pay for the NO side's shares

    @property
    def ask(self) -> float:
        return round(1.0 - self.no_bid, _dec(self.tick))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_plan(row: PocketRow, market: PolyMarket, tick: float = 0.01, mid: float | None = None) -> QuotePlan | None:
    if not market.yes_token or not market.no_token:
        return None
    m = row.mid if mid is None else mid
    h = row.max_spread / 100.0 / 2.0
    bid, no_bid = quote_prices(m, h, tick)
    size = float(math.ceil(max(row.min_size, market.order_min_size, 5.0)))
    exit_yes = getattr(row, "exit_yes", None)
    exit_no = getattr(row, "exit_no", None)
    costs = [(bid - exit_yes) * size if exit_yes is not None else None, (no_bid - exit_no) * size if exit_no is not None else None]
    exit_cost = None if any(c is None for c in costs) else round(max(c for c in costs if c is not None), 2)
    return QuotePlan(
        condition_id=row.condition_id, question=row.question, yes_token=market.yes_token, no_token=market.no_token, tick=tick,
        size=size, half_spread=h, mid=round(m, 4), bid=bid, no_bid=no_bid, collateral=round(size * (bid + no_bid), 2),
        rate_per_day=row.rate_per_day, reward_low=row.reward_low, reward_high=row.reward_high, days_to_end=row.days_to_end, url=row.url,
        exit_cost=exit_cost, exit_yes=exit_yes, exit_no=exit_no,
    )


def exit_blockers(plans: list[QuotePlan], max_exit: float) -> list[str]:
    """Why a live run must not place these quotes: a side that, once filled, could not be sold
    back into the book (no exit) or would lose more than `max_exit` dollars doing so."""
    out: list[str] = []
    for p in plans:
        if p.exit_cost is None:
            out.append(f"{p.question[:60]}: the book cannot absorb {p.size:g} shares on at least one side, so a fill has no exit")
        elif p.exit_cost > max_exit:
            out.append(f"{p.question[:60]}: undoing one fill into the book would lose ${p.exit_cost:.2f}, above --max-exit {max_exit:g}")
    return out


def choose_markets(rows: list[PocketRow], markets: dict[str, PolyMarket], *, budget: float, max_markets: int, min_days: float = 7.0, max_spread: float = 0.5, min_reward: float = 20.0, exclude_words: tuple[str, ...] = EXCLUDE_WORDS, only: set[str] | None = None, per_market: float = 0.0, reasons: dict[str, int] | None = None, max_exit: float = 2.0) -> list[QuotePlan]:
    """The best pots that fit the budget: long-dated, not too wide, not news-driven families,
    and with a book that would take one fill back for at most `max_exit` dollars (the 3 Oct
    smoke run was filled in a book where getting out cost $3 at once and $4.60 by morning).
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

    for r in sorted(rows, key=lambda r: (-r.reward_low, -(r.days_to_end or 0.0))):  # equal pots: the longer-dated first
        if only is not None and r.condition_id not in only:
            continue
        if only is None:
            if r.days_to_end is None or r.days_to_end < min_days:
                skip(f"ends within {min_days:g} days")
                continue
            if r.spread > max_spread:
                skip(f"book spread above {max_spread:g}")
                continue
            if not 0.15 <= r.mid <= 0.85:
                skip("mid outside 0.15..0.85 (near-certain markets: the bid risks 85c to earn 15c against whoever knows first)")
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
        if only is None and p.exit_cost is None:
            skip("the book cannot absorb one fill: no exit")
            continue
        if only is None and p.exit_cost > max_exit:
            skip(f"undoing one fill into the book would lose more than ${max_exit:g}")
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
    def approve(self) -> dict[str, Any]:
        """Grant whatever trading approvals the SDK lists as missing (a relayed, gasless
        transaction on a deposit wallet; an on-chain transaction from the signer on an EOA)."""
        before = self.describe()
        if before.get("approvals_ok"):
            return {"submitted": False, "missing_before": [], "note": "every approval was already in place"}
        self.client.setup_trading_approvals()
        after = self.describe()
        return {"submitted": True, "missing_before": before.get("approvals_missing", []), "missing_after": after.get("approvals_missing", []), "approvals_ok": after.get("approvals_ok")}

    def top_of_book(self, token: str, own_bids: list[tuple[float, float]] | None = None, own_asks: list[tuple[float, float]] | None = None) -> tuple[float | None, float | None]: ...
    def book(self, token: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]: ...
    def merge(self, condition_id: str) -> dict[str, Any]: ...
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

    def top_of_book(self, token: str, own_bids=None, own_asks=None) -> tuple[float | None, float | None]:
        if self.book_fn is not None:
            return self.book_fn(token)
        return self.books.get(token, (None, None))  # the paper books are other people's orders already

    def book(self, token: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        bb, ba = self.top_of_book(token)
        return ([(bb, 1e6)] if bb is not None else []), ([(ba, 1e6)] if ba is not None else [])

    def merge(self, condition_id: str) -> dict[str, Any]:
        self.log.append(("merge", condition_id))
        return {"submitted": False, "paper": True, "condition_id": condition_id}

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


QUOTING_CONTRACTS = frozenset({"standard_exchange", "neg_risk_exchange", "collateral_adapter", "neg_risk_collateral_adapter", "exchange_v3", "protocol_v2_router", "conditional_tokens"})


def _contract_names(client) -> dict[str, str]:
    """Polymarket contract address (lower-case) -> its name in the SDK's environment config."""
    try:
        cfg = client._ctx.environment_config  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for name in dir(cfg):
        if name.startswith("_"):
            continue
        v = getattr(cfg, name, None)
        if isinstance(v, str) and len(v) == 42 and v.lower().startswith("0x"):
            out[v.lower()] = name
    return out


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
            names = _contract_names(c)
            missing = []
            for x in items:
                spender = str(getattr(x, "spender", None) or getattr(x, "operator", "") or "").lower()
                missing.append(names.get(spender, spender or str(x)))
            out["approvals_missing"] = missing
            # the SDK's list includes contracts this rig never calls (the perpetuals deposit); what
            # resting an order needs is the exchanges and the collateral adapters
            out["approvals_ok_for_quoting"] = not any(m in QUOTING_CONTRACTS or m.startswith("0x") for m in missing)
        except Exception as exc:  # noqa: BLE001
            out["approvals_error"] = str(exc)
        try:
            out["gasless_ready"] = bool(c.is_gasless_ready())
        except Exception as exc:  # noqa: BLE001
            out["gasless_error"] = str(exc)
        return out

    def merge(self, condition_id: str) -> dict[str, Any]:
        """Merge every YES+NO pair of `condition_id` back into collateral: $1 per pair, no price,
        no fee, relayed gaslessly on a deposit wallet. The SDK reads the two balances and
        merges the smaller; the 4 Oct smoke run left 20 of each."""
        handle = self.client.merge_positions(condition_id=condition_id, amount="max")
        result = handle.wait()
        out: dict[str, Any] = {"submitted": True, "condition_id": condition_id}
        for name in ("state", "status", "transaction_hash", "tx_hash", "hash", "error"):
            val = getattr(result, name, None)
            if val is not None:
                out[name] = str(val)
        out["result"] = str(result)[:400]
        return out

    def book(self, token: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        ob = self.client.get_order_book(token_id=token)
        return [(float(l.price), float(l.size)) for l in ob.bids], [(float(l.price), float(l.size)) for l in ob.asks]

    def top_of_book(self, token: str, own_bids=None, own_asks=None) -> tuple[float | None, float | None]:
        bids, asks = self.book(token)
        return best_prices(bids, asks, own_bids, own_asks)

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
    bid_px: float | None = None  # the price the resting bid was placed at (re-centred quotes differ from the plan's)
    ask_px: float | None = None  # the NO bid's price
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

    def _post(self, st: MarketState, mid: float, *, want_bid: bool = True, want_ask: bool = True) -> None:
        """Place the sides asked for and not yet retired; a side already resting is never posted
        twice (an id would be overwritten and the first order orphaned on the book)."""
        p = st.plan
        bid, no_bid = quote_prices(mid, p.half_spread, p.tick)
        try:
            if want_bid and not st.bid_done and st.bid_id is None:
                st.bid_id = self.x.place(p.yes_token, bid, p.size)
                st.bid_px = bid
                self.log("place", market=p.question[:60], side="YES bid", price=bid, size=p.size, order_id=st.bid_id)
            if want_ask and not st.ask_done and st.ask_id is None:
                st.ask_id = self.x.place(p.no_token, no_bid, p.size)
                st.ask_px = no_bid
                self.log("place", market=p.question[:60], side="NO bid (YES ask)", price=no_bid, yes_ask=round(1 - no_bid, 4), size=p.size, order_id=st.ask_id)
            st.quoted_mid = mid
        except Exception as exc:  # noqa: BLE001
            self.log("error", market=p.question[:60], error=str(exc))

    def _exit_now(self, p: QuotePlan, side: str, shares: float, entry: float) -> dict[str, Any]:
        """What the book would pay this second for the shares just bought: the number the memo
        wants on every fill, never estimated afterwards."""
        try:
            bids, asks = self.x.book(p.yes_token)
            u = unwind_into(bids if side == "bid" else no_bids_from_yes_asks(asks), shares)
        except Exception as exc:  # noqa: BLE001
            return {"exit_error": str(exc)}
        return {"sell_now": round(u.net, 2), "loss_if_sold_now": round(entry * shares - u.net, 2), "book_absorbs": u.complete}

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
                        resting = st.bid_px if side == "bid" else st.ask_px  # the 4 Oct log printed the plan's 0.60 for a NO bid resting at 0.68
                        entry = resting if resting is not None else (p.bid if side == "bid" else p.no_bid)
                        self.log("fill", market=p.question[:60], side=side, shares=matched, price=entry, **self._exit_now(p, side, matched, entry))
                        if o is not None:  # partially filled and still resting: take the rest down, never average in
                            self.x.cancel([oid])
                            open_by_id.pop(oid, None)
                    setattr(st, f"{side}_id", None)
            if st.bid_done and st.ask_done:
                self._pull(st, "both sides filled")
                st.stopped = "both sides filled"
                self.log("stop", market=p.question[:60], reason=st.stopped)
                continue
            have_bid = st.bid_id is not None and st.bid_id in open_by_id
            have_ask = st.ask_id is not None and st.ask_id in open_by_id
            own_bids = [(o.price, o.size - o.matched) for o in (open_by_id[st.bid_id],)] if have_bid else []
            own_asks = [(round(1.0 - o.price, 4), o.size - o.matched) for o in (open_by_id[st.ask_id],)] if have_ask else []
            bb, ba = self.x.top_of_book(p.yes_token, own_bids, own_asks)
            if bb is None or ba is None:
                if st.quoted_mid is None:
                    self.log("error", market=p.question[:60], error="one-sided or empty book, waiting")
                    continue
                mid = st.quoted_mid  # nobody else is quoting: keep our quotes where they are
            else:
                mid = (bb + ba) / 2.0
            need_bid, need_ask = not st.bid_done and not have_bid, not st.ask_done and not have_ask
            moved = st.quoted_mid is None or abs(mid - st.quoted_mid) >= self.recentre_ticks * p.tick - 1e-9
            if moved and (have_bid or have_ask):
                self._pull(st, f"re-centre: mid {st.quoted_mid} -> {round(mid, 4)}")
                st.replaced += 1
                have_bid = have_ask = False  # pulled: the ids are gone (the 3 Oct run cancelled a None here)
                need_bid, need_ask = not st.bid_done, not st.ask_done
            if need_bid or need_ask:
                if need_bid and have_bid and st.bid_id:
                    self.x.cancel([st.bid_id]); st.bid_id = None
                if need_ask and have_ask and st.ask_id:
                    self.x.cancel([st.ask_id]); st.ask_id = None
                self._post(st, mid, want_bid=need_bid, want_ask=need_ask)
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


# --------------------------------------------------------------------------- #
# What the account holds, priced by the book and not by the site's midpoint mark
# --------------------------------------------------------------------------- #

DATA_API = "https://data-api.polymarket.com"


@dataclass
class Position:
    title: str
    outcome: str
    condition_id: str
    token: str
    shares: float
    avg_price: float
    site_price: float  # the midpoint mark the site shows
    end_date: str
    best_bid: float | None
    best_bid_size: float
    unwind: Unwind

    @property
    def cost(self) -> float:
        return self.shares * self.avg_price

    @property
    def site_value(self) -> float:
        return self.shares * self.site_price

    @property
    def sell_now(self) -> float:
        return self.unwind.net

    @property
    def pnl_if_sold(self) -> float:
        return self.unwind.net - self.cost

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(cost=round(self.cost, 4), site_value=round(self.site_value, 4), sell_now=round(self.sell_now, 4), pnl_if_sold=round(self.pnl_if_sold, 4), book_absorbs=self.unwind.complete)
        return d


def fetch_positions(http: HttpClient, wallet: str) -> list[dict[str, Any]]:
    data = http.get_json(f"{DATA_API}/positions", {"user": wallet, "sizeThreshold": 0})
    return [d for d in (data or []) if float(d.get("size") or 0) > 0]


def positions_report(http: HttpClient, book_fetcher, wallet: str) -> list[Position]:
    """Every open position of `wallet` with what its own token's bids pay for it right now."""
    raw = fetch_positions(http, wallet)
    books = book_fetcher([str(d["asset"]) for d in raw]) if raw else {}
    out: list[Position] = []
    for d in raw:
        token = str(d["asset"])
        b = books.get(token)
        bids = [(lv.price, lv.size) for lv in b.bids] if b is not None else []
        shares = float(d["size"])
        best = max(bids, key=lambda lv: lv[0]) if bids else None
        out.append(Position(str(d.get("title", "")), str(d.get("outcome", "")), str(d.get("conditionId", "")), token, shares, float(d.get("avgPrice") or 0.0),
                            float(d.get("curPrice") or 0.0), str(d.get("endDate", "")), best[0] if best else None, best[1] if best else 0.0, unwind_into(bids, shares)))
    return out


def merge_preview(http: HttpClient, wallet: str, condition_id: str) -> dict[str, Any]:
    """What a merge of `condition_id` would return, from the public positions feed: the pairs
    are the smaller of the YES and NO balances, a dollar each."""
    legs = [d for d in fetch_positions(http, wallet) if str(d.get("conditionId", "")).lower() == condition_id.lower()]
    by = {str(d.get("outcome", "")).lower(): float(d.get("size") or 0.0) for d in legs}
    yes, no = by.get("yes", 0.0), by.get("no", 0.0)
    pairs = min(yes, no)
    paid = sum(float(d.get("size") or 0.0) * float(d.get("avgPrice") or 0.0) for d in legs)
    return {"condition_id": condition_id, "title": legs[0].get("title", "") if legs else "", "yes_shares": yes, "no_shares": no, "pairs": round(pairs, 4), "returns_usd": round(pairs, 2), "paid_usd": round(paid, 2),
            "left_over": {"yes": round(yes - pairs, 4), "no": round(no - pairs, 4)}}


def render_positions(ps: list[Position]) -> str:
    if not ps:
        return "no open positions"
    L = [f"  {'shares':>7} {'avg':>5} {'cost $':>7} {'site':>5} {'bid':>5} {'depth':>6} {'sell now $':>10} {'P&L $':>7}  market"]
    for p in ps:
        bid = f"{p.best_bid:5.2f}" if p.best_bid is not None else " none"
        sell = f"{p.sell_now:10.2f}" + ("" if p.unwind.complete else "*")
        L.append(f"  {p.shares:7.2f} {p.avg_price:5.2f} {p.cost:7.2f} {p.site_price:5.2f} {bid} {p.best_bid_size:6.1f} {sell} {p.pnl_if_sold:7.2f}  {p.title[:50]} ({p.outcome}, ends {p.end_date})")
    L.append(f"  selling everything into the book now returns ${sum(p.sell_now for p in ps):.2f} for ${sum(p.cost for p in ps):.2f} paid: P&L ${sum(p.pnl_if_sold for p in ps):+.2f}"
             + ("; * = the bids cannot absorb all of it, the figure is for the part they can" if any(not p.unwind.complete for p in ps) else ""))
    L.append("  'site' is the midpoint mark polymarket.com shows; 'sell now' is what the bids pay, taker fee included. Only the second is money.")
    return "\n".join(L)


def render_plan(plans: list[QuotePlan]) -> str:
    if not plans:
        return "no market fits the budget and filters"
    L = [f"  {'reward lo..hi':>14} {'rate':>5} {'mid':>5} {'bid':>5} {'ask':>5} {'size':>4} {'collat':>7} {'exit $':>6} {'days':>5}  question"]
    for p in plans:
        ex = f"{p.exit_cost:6.2f}" if p.exit_cost is not None else "  none"
        L.append(f"  {p.reward_low:6.1f}..{p.reward_high:<6.1f} {p.rate_per_day:5.0f} {p.mid:5.2f} {p.bid:5.2f} {p.ask:5.2f} {p.size:4.0f} {p.collateral:7.2f} {ex} {p.days_to_end if p.days_to_end is not None else float('nan'):5.0f}  {p.question[:60]}")
    L.append(f"  total collateral parked: ${sum(p.collateral for p in plans):.2f} in {len(plans)} markets; modelled reward ${sum(p.reward_low for p in plans):.0f}..{sum(p.reward_high for p in plans):.0f}/day")
    L.append("  exit $: what one full fill on the worse side would lose if sold straight back into what today's book keeps below that quote, fee included (a fill means every order at or above it was taken first; none = nothing below could absorb it)")
    for p in plans:
        L.append(f"  --only {p.condition_id}   # {p.question[:50]}  {p.url}")
    return "\n".join(L)


def parse_only(text: str | None) -> set[str] | None:
    if not text:
        return None
    return {t.strip() for t in re.split(r"[,\s]+", text) if t.strip()}
