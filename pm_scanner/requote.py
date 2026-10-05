"""Re-quote policies replayed on a market's public history: does moving the quotes away from an
approaching price avoid the fills, and what does each policy keep of the reward score?

4 Oct 2026, 19:56:31 UTC, the deep-book test (memo 18e): the rig's YES bid at 0.27, three cents
under a mid that had read 0.30 for two hours, filled when one seller sold 3,192 YES shares in a
single second, clearing the book from 0.30 to 0.27; the price then slid to 0.24 over the next
hour. The user asked whether a rig that tracked the price in real time and moved its orders
away from an approaching price ("escape") would avoid such fills. This module answers from
history rather than opinion: the CLOB's minute-by-minute midpoints (`prices-history`,
fidelity 1 for the last day, 5 for the week) and the data-api trade tape are replayed under
several policies, with the same size and distance the rig would quote.

Policies, all quoting at half the max spread (the rig's `quote_prices`), read every `interval`
seconds like the rig:

* `rig`: the live rule. Re-centre after `confirm` consecutive readings with the mid a tick or
  more from where we quoted (`--recentre-confirm 3`).
* `follow`: `confirm` 1, the rule before the Topuria chase of 4 Oct (an 11c one-minute spike,
  followed; the bid placed at the spike filled on the way back).
* `escape g`: the user's idea. Re-centre at once when the mid comes within `g` ticks of either
  quote, otherwise the rig rule.
* `wide`: the rig rule, quoting `extra_ticks` further out (a lower score per share, by
  Polymarket's ((v - s) / v)^2).
* `rig away=0.5t`: the rig rule with a half-tick drift counting as away (`--recentre-ticks
  0.5`). In a 2c-wide book the mid flips between 0.30 and 0.305 all afternoon; quotes centred
  on 0.30 score a third of quotes centred on 0.305 (3.5c and 2.5c from the mid against 2.5c
  and 2.5c, under the two-sided rule), and the rig's full-tick rule never moves them.

Fill model, from the tape: a print between two readings at a YES price beyond our quote fills
it in full (`fill="through"`: strictly beyond, the level was cleared; `fill="at"`: at our price
too, which assumes we were first in the queue). A filled side is re-armed at the next
re-centre, so the count is fills per day, not the rig's one-and-done. Each fill is marked
against the mid 10 and 60 minutes later, less a tick to cross back: that is `loss`, what undoing
it would cost. `warned` records whether the reading before the fill already showed the mid a
tick or more closer to that side than when it was quoted: a fill with no warning is one no
reading-based policy could have escaped. Score-minutes use `rewards.order_score` and the
two-sided rule, summed over readings, against the score of a quote re-centred every reading
(`ideal`), so `score_share` is the fraction of the reward the policy keeps while escaping (it
can read above 1 for a while: a drift that brings one side within a cent of the mid scores more
one-sided, at a third of credit, than two quotes three cents out).

Caveats, so the table is read as what it is: the tape is public prints, not our queue position
(a deep book may absorb a print at our level before it reaches us, so "at" is a ceiling on
fills and "through" is nearer the truth for deep books); the 5-minute week hides moves inside a
bar, so the one-minute day is the sharp test and the week the coarse one; the marks are mids,
not the bids that would have paid, so the losses are shapes, not readings (`lp --positions` is
the reading).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .flow import is_first_outcome
from .rewards import order_score
from .unwind import quote_prices

Point = tuple[int, float]


@dataclass(frozen=True)
class Policy:
    name: str
    confirm: int = 3  # readings a tick or more away before a re-centre (the rig's --recentre-confirm)
    guard_ticks: float = 0.0  # re-centre at once when the mid is within this many ticks of a quote (0 = never)
    extra_ticks: int = 0  # quote this many ticks wider than half the max spread (negative: nearer the mid, -2 is the touch in a 2c book)
    recentre_ticks: float = 1.0  # a reading counts as away when the mid is this many ticks from where we quoted (the rig's --recentre-ticks)

    @property
    def label(self) -> str:
        bits = [self.name, f"confirm={self.confirm}"]
        if self.recentre_ticks != 1.0:
            bits.append(f"away={self.recentre_ticks:g}t")
        if self.guard_ticks:
            bits.append(f"guard={self.guard_ticks:g}t")
        if self.extra_ticks:
            bits.append(f"{self.extra_ticks:+d}t")
        return " ".join(bits)


RIG = Policy("rig", confirm=3)
FOLLOW = Policy("follow", confirm=1)
ESCAPE_1 = Policy("escape", confirm=3, guard_ticks=1.0)
ESCAPE_2 = Policy("escape", confirm=3, guard_ticks=2.0)
WIDE = Policy("wide", confirm=3, extra_ticks=1)
HALF_TICK = Policy("rig", confirm=3, recentre_ticks=0.5)  # the rig's rule, but a half-tick drift of the mid counts as away
DEFAULT_POLICIES = (RIG, HALF_TICK, FOLLOW, ESCAPE_1, ESCAPE_2, WIDE)


@dataclass
class Fill:
    ts: int
    side: str  # "bid" (we bought YES) or "ask" (we sold YES)
    price: float  # YES price of the fill
    size: float
    quoted_mid: float  # where the quotes were centred
    mid_before: float  # the mid at the reading before the fill
    warned: bool  # that reading already showed the mid a tick or more closer to this side than when quoted
    mark_10: float | None = None  # mid 10 minutes after the fill
    mark_60: float | None = None
    tick: float = 0.01

    def loss(self, horizon: int = 10) -> float | None:
        """What undoing the fill into the mid `horizon` minutes later, less a tick to cross, would
        cost (positive = a loss)."""
        mark = self.mark_10 if horizon == 10 else self.mark_60
        if mark is None:
            return None
        if self.side == "bid":
            return round((self.price - (mark - self.tick)) * self.size, 4)
        return round(((mark + self.tick) - self.price) * self.size, 4)

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "side": self.side, "price": self.price, "size": self.size, "quoted_mid": self.quoted_mid, "mid_before": self.mid_before,
                "warned": self.warned, "mark_10": self.mark_10, "mark_60": self.mark_60, "loss_10": self.loss(10), "loss_60": self.loss(60)}


@dataclass
class Replay:
    policy: Policy
    fills: list[Fill] = field(default_factory=list)
    readings: int = 0
    recentres: int = 0
    score_minutes: float = 0.0
    ideal_minutes: float = 0.0
    quoting_minutes: float = 0.0  # minutes with both sides resting
    span_hours: float = 0.0
    start_ts: int = 0  # the first reading's timestamp (yieldrate groups the fills by UTC day from it)

    @property
    def days(self) -> float:
        return max(self.span_hours, 1.0) / 24.0

    @property
    def fills_per_day(self) -> float:
        return len(self.fills) / self.days

    @property
    def warned_fills(self) -> int:
        return sum(1 for f in self.fills if f.warned)

    def loss_per_day(self, horizon: int = 10) -> float:
        return sum(x for x in (f.loss(horizon) for f in self.fills) if x is not None) / self.days

    @property
    def score_share(self) -> float:
        return self.score_minutes / self.ideal_minutes if self.ideal_minutes > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"policy": self.policy.label, "fills": len(self.fills), "warned": self.warned_fills, "fills_per_day": round(self.fills_per_day, 3),
                "loss_per_day_10": round(self.loss_per_day(10), 4), "loss_per_day_60": round(self.loss_per_day(60), 4), "recentres": self.recentres,
                "score_share": round(self.score_share, 4), "readings": self.readings, "span_hours": round(self.span_hours, 2), "start_ts": self.start_ts, "detail": [f.to_dict() for f in self.fills]}


def mid_at(series: list[Point], ts: int) -> float | None:
    """The last sampled mid at or before `ts` (None before the first sample)."""
    lo, hi = 0, len(series)
    while lo < hi:  # bisect on the timestamps
        m = (lo + hi) // 2
        if series[m][0] <= ts:
            lo = m + 1
        else:
            hi = m
    return series[lo - 1][1] if lo > 0 else None


def tape_prints(trades: list[dict[str, Any]], outcomes: list[str] | None) -> list[tuple[int, float, float]]:
    """(ts, YES price, size) for every print, sorted; a data-api row or the lp cache's slimmed row."""
    out: list[tuple[int, float, float]] = []
    for t in trades:
        try:
            ts = int(t.get("timestamp", t.get("ts")))
            price, size = float(t["price"]), float(t.get("size") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if not is_first_outcome(t, outcomes):
            price = 1.0 - price
        out.append((ts, round(price, 6), size))
    out.sort()
    return out


def _two_sided(q_bid: float, q_ask: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q_bid, q_ask), max(q_bid, q_ask) / 3.0)
    return min(q_bid, q_ask)


def replay(series: list[Point], prints: list[tuple[int, float, float]], *, policy: Policy, max_spread_cents: float, size: float, tick: float = 0.01,
           interval: int = 60, fill: str = "through", start: int | None = None, end: int | None = None) -> Replay:
    """Replay one policy over a market's sampled mids and prints. `series` is sorted (ts, mid);
    `prints` is from `tape_prints`."""
    series = sorted(series)
    if len(series) < 2:
        return Replay(policy)
    t0 = max(series[0][0], start) if start is not None else series[0][0]
    t1 = min(series[-1][0], end) if end is not None else series[-1][0]
    half = max_spread_cents / 200.0 + policy.extra_ticks * tick
    v = max_spread_cents
    r = Replay(policy, span_hours=max(0.0, (t1 - t0) / 3600.0), start_ts=t0)
    bid: float | None = None
    ask: float | None = None
    quoted_mid: float | None = None
    away = 0
    prev_mid: float | None = None
    i = 0  # prints cursor
    n = len(prints)
    while i < n and prints[i][0] <= t0:
        i += 1
    t = t0
    while t <= t1:
        mid = mid_at(series, t)
        if mid is None:
            t += interval
            continue
        # prints since the last reading, against the quotes resting then
        while i < n and prints[i][0] <= t:
            ts, p, _sz = prints[i]
            i += 1
            if bid is not None and (p < bid - 1e-9 or (fill == "at" and p <= bid + 1e-9)):
                warned = prev_mid is not None and quoted_mid is not None and (quoted_mid - prev_mid) >= tick - 1e-9
                r.fills.append(Fill(ts, "bid", bid, size, quoted_mid, prev_mid if prev_mid is not None else quoted_mid, warned,
                                    mid_at(series, ts + 600), mid_at(series, ts + 3600), tick))
                bid = None
            elif ask is not None and (p > ask + 1e-9 or (fill == "at" and p >= ask - 1e-9)):
                warned = prev_mid is not None and quoted_mid is not None and (prev_mid - quoted_mid) >= tick - 1e-9
                r.fills.append(Fill(ts, "ask", ask, size, quoted_mid, prev_mid if prev_mid is not None else quoted_mid, warned,
                                    mid_at(series, ts + 600), mid_at(series, ts + 3600), tick))
                ask = None
        # the reading
        r.readings += 1
        if quoted_mid is None:
            moved = True
        else:
            is_away = abs(mid - quoted_mid) >= policy.recentre_ticks * tick - 1e-9
            away = away + 1 if is_away else 0
            threatened = policy.guard_ticks > 0 and ((bid is not None and mid - bid <= policy.guard_ticks * tick + 1e-9) or (ask is not None and ask - mid <= policy.guard_ticks * tick + 1e-9))
            moved = away >= max(1, policy.confirm) or threatened
        if moved:
            if quoted_mid is not None:
                r.recentres += 1
            quoted_mid = mid
            away = 0
            b, no_bid = quote_prices(mid, half, tick)
            bid, ask = b, round(1.0 - no_bid, 4)
        # score at this reading, against a quote re-centred here
        q_bid = order_score(v, (mid - bid) * 100.0) * size if bid is not None else 0.0
        q_ask = order_score(v, (ask - mid) * 100.0) * size if ask is not None else 0.0
        ib, ino = quote_prices(mid, max_spread_cents / 200.0, tick)
        ideal = _two_sided(order_score(v, (mid - ib) * 100.0) * size, order_score(v, (round(1.0 - ino, 4) - mid) * 100.0) * size, mid)
        minutes = interval / 60.0
        r.score_minutes += _two_sided(q_bid, q_ask, mid) * minutes
        r.ideal_minutes += ideal * minutes
        if bid is not None and ask is not None:
            r.quoting_minutes += minutes
        prev_mid = mid
        t += interval
    return r


def replay_all(series: list[Point], prints: list[tuple[int, float, float]], *, max_spread_cents: float, size: float, tick: float = 0.01, interval: int = 60,
               fill: str = "through", policies: tuple[Policy, ...] = DEFAULT_POLICIES, start: int | None = None, end: int | None = None) -> list[Replay]:
    return [replay(series, prints, policy=p, max_spread_cents=max_spread_cents, size=size, tick=tick, interval=interval, fill=fill, start=start, end=end) for p in policies]


def render(rows: list[tuple[str, list[Replay]]], *, title: str = "") -> str:
    """One block per market: a line per policy."""
    L: list[str] = []
    if title:
        L.append(title)
    L.append(f"  {'policy':<26} {'fills':>5} {'warned':>6} {'/day':>6} {'loss10/d':>8} {'loss60/d':>8} {'recentres':>9} {'score%':>6}  hours")
    for name, reps in rows:
        L.append(f"  {name}")
        for r in reps:
            L.append(f"  {r.policy.label:<26} {len(r.fills):5d} {r.warned_fills:6d} {r.fills_per_day:6.2f} {r.loss_per_day(10):8.2f} {r.loss_per_day(60):8.2f} {r.recentres:9d} {r.score_share * 100:6.1f}  {r.span_hours:5.1f}")
    L.append("  fills: prints beyond the quote between two readings (one per side per re-centre).  warned: the reading before the fill already showed the mid a tick nearer that side than when quoted; the rest no reading-based rule could escape.  loss10/d, loss60/d: $ a day to undo the fills into the mid 10 or 60 minutes later, less a tick.  score%: reward score kept, against a quote re-centred every reading.")
    return "\n".join(L)


def summarize(rows: list[tuple[str, list[Replay]]]) -> list[dict[str, Any]]:
    """Per policy across markets: fills, warned fills, loss per market-day, score share."""
    by: dict[str, dict[str, float]] = {}
    for _name, reps in rows:
        for r in reps:
            d = by.setdefault(r.policy.label, {"policy": r.policy.label, "markets": 0, "fills": 0, "warned": 0, "market_days": 0.0, "loss_10": 0.0, "loss_60": 0.0, "score": 0.0, "ideal": 0.0, "recentres": 0})
            d["markets"] += 1
            d["fills"] += len(r.fills)
            d["warned"] += r.warned_fills
            d["market_days"] += r.days
            d["loss_10"] += sum(x for x in (f.loss(10) for f in r.fills) if x is not None)
            d["loss_60"] += sum(x for x in (f.loss(60) for f in r.fills) if x is not None)
            d["score"] += r.score_minutes
            d["ideal"] += r.ideal_minutes
            d["recentres"] += r.recentres
    out = []
    for d in by.values():
        md = d["market_days"] or 1.0
        out.append({"policy": d["policy"], "markets": d["markets"], "fills": d["fills"], "warned": d["warned"], "fills_per_market_day": round(d["fills"] / md, 3),
                    "loss_10_per_market_day": round(d["loss_10"] / md, 3), "loss_60_per_market_day": round(d["loss_60"] / md, 3),
                    "score_share": round(d["score"] / d["ideal"], 4) if d["ideal"] > 0 else 0.0, "recentres_per_market_day": round(d["recentres"] / md, 2)})
    return out


def render_summary(summary: list[dict[str, Any]], *, title: str = "") -> str:
    L = [title] if title else []
    L.append(f"  {'policy':<26} {'mkts':>4} {'fills':>5} {'warned':>6} {'/mkt-day':>8} {'loss10':>7} {'loss60':>7} {'recentre/d':>10} {'score%':>6}")
    for d in summary:
        L.append(f"  {d['policy']:<26} {d['markets']:4d} {d['fills']:5d} {d['warned']:6d} {d['fills_per_market_day']:8.2f} {d['loss_10_per_market_day']:7.2f} {d['loss_60_per_market_day']:7.2f} {d['recentres_per_market_day']:10.1f} {d['score_share'] * 100:6.1f}")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Fetching, for the CLI
# --------------------------------------------------------------------------- #

def fetch_mids(http, token: str, *, days: int = 1) -> list[Point]:
    """The CLOB's sampled midpoints for `token`: one a minute for the last day (`days` 1), one
    every five minutes for the last week (the endpoint's finest grain for a week)."""
    from .polymarket import CLOB_URL

    interval, fidelity = ("1d", 1) if days <= 1 else ("1w", 5)
    data = http.get_json(f"{CLOB_URL}/prices-history", {"market": token, "interval": interval, "fidelity": fidelity})
    out: list[Point] = []
    for x in (data or {}).get("history") or []:
        try:
            out.append((int(x["t"]), float(x["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort()
    return out


def market_params(market) -> tuple[float, float, float]:
    """(max spread in cents, reward minimum size, tick) from a gamma market record."""
    raw = getattr(market, "raw", {}) or {}
    v = float(raw.get("rewardsMaxSpread") or 0.0)
    size = float(raw.get("rewardsMinSize") or 0.0)
    tick = float(raw.get("orderPriceMinTickSize") or 0.01)
    return v, size, tick


def replay_market(http, trades_source, market, *, days: int = 1, interval: int = 60, fill: str = "through", size: float | None = None,
                  policies: tuple[Policy, ...] = DEFAULT_POLICIES) -> tuple[str, list[Replay]] | None:
    """Fetch one market's mids and tape and replay every policy; None when the market carries no
    reward programme or has no history."""
    v, min_size, tick = market_params(market)
    if v <= 0:
        return None
    sz = size if size and size > 0 else (min_size or 20.0)
    series = fetch_mids(http, market.yes_token, days=days)
    if len(series) < 2:
        return None
    prints = tape_prints(trades_source.trades(market.condition_id, closed=False), market.outcomes)
    reps = replay_all(series, prints, max_spread_cents=v, size=sz, tick=tick, interval=interval, fill=fill, policies=policies)
    name = f"{market.question[:70]}  (max spread {v:g}c, size {sz:g}, {len(prints)} prints, {len(series)} mids)"
    return name, reps
