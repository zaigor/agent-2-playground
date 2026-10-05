"""A daily rate on the money: which rewarded markets would pay a resting quote a fraction of a
percent a day, net of what its fills cost, and can a week of public history tell?

5 Oct 2026, the user's question: instead of asking how much the liquidity-reward rig can make,
ask for 0.1-0.2% a day on the money (never more than 0.5%), find the markets that would pay it,
and backtest. This module joins the two models the repository already has, per market:

* the reward side is the chooser's own (`lp.choose_markets`, memo 18e's gates: deep calm books,
  long-dated, a real exit): the pot per day times the share a minimum-size two-sided quote at
  half the max spread takes against the book right now (`rewards.our_share`, a low..high range),
  scaled by the fraction of that score the rig's re-centre rule keeps over the week
  (`requote.Replay.score_share`, the half-tick drift of 4 Oct);
* the cost side is the re-quote replay (`requote.replay`) of the same quote over the market's own
  week of five-minute mids and its trade tape: fills a day and what undoing each into the mid an
  hour later (or ten minutes later, the "undo at once" shape) would cost.

Net a day = reward × kept − loss, read as a percentage of the dollars the quote parks (both sides'
collateral) and of the budget. The question the user asked is answered twice: which markets the
model puts in the band, and whether a week of history can tell a rate that small from noise. For
the second, the fills are grouped by UTC day: a daily net series, its spread, and the number of
market-days a reading would need before its two-sigma error is under the target.

Caveats (the same as `requote`'s, which this inherits): the share is a snapshot of one book, not
a week of them (the 18e pot's competitiveness figure moved by an order of magnitude within hours);
the tape is prints, not our queue position; the marks are mids, not bids; five-minute bars hide
moves inside them. Read-only, public data; nothing here sizes or sends an order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .lp import QuotePlan
from .requote import HALF_TICK, RIG, Fill, Point, Policy, Replay, fetch_mids, market_params, mid_at, replay_all, tape_prints
from .rewards import PocketRow, competitor_total


@dataclass
class YieldRow:
    plan: QuotePlan
    replay: Replay  # the rig's rule
    alt: Replay | None = None  # the half-tick rule, for the `kept` comparison
    horizon: int = 60  # minutes after a fill at which it is marked (60 = held an hour, 10 = undone at once)
    daily: list[tuple[str, float, float]] = field(default_factory=list)  # (UTC day, reward_low × kept pro-rated, fill losses)
    series: list[Point] = field(default_factory=list)  # the sampled mids the replay ran on (for the hold outcomes)
    comp: tuple[float, float] | None = None  # the book's competitor score totals (low, high), for the distance sweep
    sweeps: list["SweepRow"] = field(default_factory=list)

    # --- reward side --- #
    @property
    def kept(self) -> float:
        return self.replay.score_share

    @property
    def reward_low(self) -> float:
        return self.plan.reward_low * self.kept

    @property
    def reward_high(self) -> float:
        return self.plan.reward_high * self.kept

    # --- cost side --- #
    @property
    def loss_per_day(self) -> float:
        return self.replay.loss_per_day(self.horizon)

    @property
    def fills_per_day(self) -> float:
        return self.replay.fills_per_day

    # --- net --- #
    @property
    def net_low(self) -> float:
        return self.reward_low - self.loss_per_day

    @property
    def net_high(self) -> float:
        return self.reward_high - self.loss_per_day

    @property
    def parked(self) -> float:
        return self.plan.collateral

    def pct(self, net: float, on: float | None = None) -> float:
        base = on if on else self.parked
        return 100.0 * net / base if base > 0 else 0.0

    @property
    def days_with_fill(self) -> int:
        return sum(1 for _d, _r, loss in self.daily if loss > 0)

    @property
    def worst_day(self) -> float:
        return min((r - loss for _d, r, loss in self.daily), default=0.0)

    @property
    def daily_sd(self) -> float:
        nets = [r - loss for _d, r, loss in self.daily]
        if len(nets) < 2:
            return 0.0
        m = sum(nets) / len(nets)
        return math.sqrt(sum((x - m) ** 2 for x in nets) / (len(nets) - 1))

    def days_to_tell(self, target_dollars: float) -> float | None:
        """Market-days until the two-sigma error of the mean daily net is under `target_dollars`."""
        if target_dollars <= 0:
            return None
        return (2.0 * self.daily_sd / target_dollars) ** 2

    def verdict(self, lo_pct: float, hi_pct: float) -> str:
        a, b = self.pct(self.net_low), self.pct(self.net_high)
        if b < 0:
            return "loses"
        if a < 0:
            return "sign unknown"
        if b < lo_pct:
            return "under"
        if a > hi_pct:
            return "above"
        return "in band"

    def to_dict(self) -> dict[str, Any]:
        return {"condition_id": self.plan.condition_id, "question": self.plan.question, "rate_per_day": self.plan.rate_per_day, "size": self.plan.size, "parked": self.parked,
                "inside": self.plan.inside, "exit_cost": self.plan.exit_cost, "moves_per_day": self.plan.moves_per_day, "age_days": self.plan.age_days, "days_to_end": self.plan.days_to_end,
                "share_low": round(self.plan.reward_low / self.plan.rate_per_day, 5) if self.plan.rate_per_day else 0.0, "share_high": round(self.plan.reward_high / self.plan.rate_per_day, 5) if self.plan.rate_per_day else 0.0,
                "kept": round(self.kept, 4), "kept_half_tick": round(self.alt.score_share, 4) if self.alt else None, "reward_low": round(self.reward_low, 4), "reward_high": round(self.reward_high, 4),
                "fills_per_day": round(self.fills_per_day, 3), "loss_per_day": round(self.loss_per_day, 4), "loss_per_day_10": round(self.replay.loss_per_day(10), 4), "loss_per_day_60": round(self.replay.loss_per_day(60), 4),
                "net_low": round(self.net_low, 4), "net_high": round(self.net_high, 4), "pct_low": round(self.pct(self.net_low), 4), "pct_high": round(self.pct(self.net_high), 4),
                "span_hours": round(self.replay.span_hours, 2), "daily": [{"day": d, "reward": round(r, 4), "loss": round(loss, 4)} for d, r, loss in self.daily],
                "daily_sd": round(self.daily_sd, 4), "worst_day": round(self.worst_day, 4), "fills": [f.to_dict() for f in self.replay.fills]}


def daily_series(fills: list[Fill], *, start_ts: int, span_hours: float, reward_per_day: float, horizon: int) -> list[tuple[str, float, float]]:
    """Per UTC day across the replay's span: the reward it would have paid (pro-rated for the
    partial first and last days) and the fills' losses at `horizon`. Empty when the span is under
    an hour."""
    if span_hours <= 0:
        return []
    end_ts = start_ts + int(span_hours * 3600)
    out: list[tuple[str, float, float]] = []
    day = datetime.fromtimestamp(start_ts, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    while int(day.timestamp()) < end_ts:
        d0 = int(day.timestamp())
        d1 = d0 + 86400
        covered = max(0, min(d1, end_ts) - max(d0, start_ts)) / 86400.0
        loss = sum(x for f in fills if d0 <= f.ts < d1 for x in [f.loss(horizon)] if x is not None)
        out.append((day.strftime("%Y-%m-%d"), reward_per_day * covered, loss))
        day = datetime.fromtimestamp(d1, tz=timezone.utc)
    return out


def evaluate(plan: QuotePlan, replays: list[Replay], *, horizon: int = 60) -> YieldRow | None:
    """Join one plan with its replays (the rig's rule, and the half-tick rule for comparison when
    it is among them). None when there is no replay or it spans no time."""
    rig = next((r for r in replays if r.policy == RIG), replays[0] if replays else None)
    if rig is None or rig.span_hours <= 0:
        return None
    alt = next((r for r in replays if r.policy == HALF_TICK), None)
    row = YieldRow(plan, rig, alt, horizon)
    row.daily = daily_series(rig.fills, start_ts=rig.start_ts, span_hours=rig.span_hours, reward_per_day=row.reward_low, horizon=horizon)
    return row


def basket(rows: list[YieldRow], *, budget: float, max_markets: int, lo_pct: float, hi_pct: float) -> list[YieldRow]:
    """The markets a budget would hold, best modelled net first, each one in or above the band on
    its own low figure (a market the model already puts under the band or at a loss is not
    carried in the hope of the high figure)."""
    out: list[YieldRow] = []
    spent = 0.0
    for r in sorted(rows, key=lambda r: -r.net_low):
        if r.verdict(lo_pct, hi_pct) not in ("in band", "above"):
            continue
        if spent + r.parked > budget:
            continue
        out.append(r)
        spent += r.parked
        if len(out) >= max_markets:
            break
    return out


def render(rows: list[YieldRow], *, budget: float, lo_pct: float, hi_pct: float, grain: str, horizon: int) -> str:
    L = [f"Daily rate on the money, {len(rows)} markets the chooser's gates let through, each replayed on {grain} with the rig's rule; fills marked {horizon} minutes on. Target band {lo_pct:g}..{hi_pct:g}% a day.",
         f"  {'pot':>5} {'inside':>6} {'size':>4} {'parked$':>7} {'share lo..hi%':>14} {'kept%':>5} {'reward lo..hi':>13} {'fills/d':>7} {'loss/d':>6} {'net lo..hi $/d':>15} {'%/d on parked':>14} {'days±':>6} {'worst d':>7} {'tell':>6}  verdict      question"]
    for r in rows:
        sl, sh = (100.0 * r.plan.reward_low / r.plan.rate_per_day, 100.0 * r.plan.reward_high / r.plan.rate_per_day) if r.plan.rate_per_day else (0.0, 0.0)
        tell = r.days_to_tell(lo_pct / 100.0 * r.parked)
        tell_s = f"{tell:6.0f}" if tell is not None and tell < 1e5 else "     -"
        L.append(f"  {r.plan.rate_per_day:5.0f} {r.plan.inside or 0:6.0f} {r.plan.size:4.0f} {r.parked:7.2f} {sl:6.2f}..{sh:<6.2f} {r.kept * 100:5.0f} {r.reward_low:5.2f}..{r.reward_high:<6.2f} {r.fills_per_day:7.2f} {r.loss_per_day:6.2f} {r.net_low:6.2f}..{r.net_high:<7.2f} {r.pct(r.net_low):6.2f}..{r.pct(r.net_high):<6.2f} {r.days_with_fill:3d}/{len(r.daily):<2d} {r.worst_day:7.2f} {tell_s}  {r.verdict(lo_pct, hi_pct):<12} {r.plan.question[:52]}")
    if rows:
        n = len(rows)
        md = sum(r.replay.days for r in rows)
        fills = sum(len(r.replay.fills) for r in rows)
        loss = sum(r.loss_per_day * r.replay.days for r in rows)
        kept = sum(r.replay.score_minutes for r in rows) / max(1e-9, sum(r.replay.ideal_minutes for r in rows))
        kept_alt = sum(r.alt.score_minutes for r in rows if r.alt) / max(1e-9, sum(r.alt.ideal_minutes for r in rows if r.alt))
        in_band = [r for r in rows if r.verdict(lo_pct, hi_pct) == "in band"]
        above = [r for r in rows if r.verdict(lo_pct, hi_pct) == "above"]
        loses = [r for r in rows if r.verdict(lo_pct, hi_pct) == "loses"]
        unknown = [r for r in rows if r.verdict(lo_pct, hi_pct) == "sign unknown"]
        under = [r for r in rows if r.verdict(lo_pct, hi_pct) == "under"]
        L.append(f"  across {n} markets, {md:.0f} market-days: {fills} fills ({fills / md:.2f} a market-day), loss ${loss / md:.2f} a market-day; the rig's rule keeps {kept * 100:.0f}% of the score, the half-tick rule {kept_alt * 100:.0f}%")
        L.append(f"  verdicts on the low figure: in band {len(in_band)}, above the band {len(above)}, under it {len(under)}, sign unknown (low under zero, high over) {len(unknown)}, loses on both {len(loses)}")
        med = sorted(r.pct(r.net_low) for r in rows)[n // 2]
        medh = sorted(r.pct(r.net_high) for r in rows)[n // 2]
        L.append(f"  median %/day on parked: {med:.2f} (low share) .. {medh:.2f} (high share)")
    L.append(f"  pot: $/day.  inside: score-weighted shares others rest inside the max spread, thinner side.  parked$: both sides' collateral.  share: of the pot, against this book (competitors all balanced .. all one-sided).  kept: the fraction of that score the rig's re-centre rule held over the replay.  reward: pot × share × kept, $/day.  fills/d, loss/d: the replay's, marked {horizon} min after each fill, less a tick.  net: reward − loss.  days±: UTC days with a fill / days replayed.  worst d: the worst day's net, $.  tell: market-days before the two-sigma error of the daily mean is under {lo_pct:g}% of parked.")
    return "\n".join(L)


def render_basket(rows: list[YieldRow], *, budget: float, lo_pct: float, hi_pct: float, horizon: int) -> str:
    if not rows:
        return f"  basket for ${budget:g}: no market the model puts in or above {lo_pct:g}% a day on its low figure"
    parked = sum(r.parked for r in rows)
    lo = sum(r.net_low for r in rows)
    hi = sum(r.net_high for r in rows)
    fills = sum(r.fills_per_day for r in rows)
    sd = math.sqrt(sum(r.daily_sd ** 2 for r in rows))
    target = lo_pct / 100.0 * budget
    tell = (2.0 * sd / target) ** 2 if target > 0 else None
    L = [f"  basket for ${budget:g}: {len(rows)} markets, ${parked:.2f} parked; modelled net ${lo:.2f}..{hi:.2f} a day = {100 * lo / budget:.2f}..{100 * hi / budget:.2f}% of the budget ({100 * lo / parked:.2f}..{100 * hi / parked:.2f}% of parked); {fills:.2f} fills a day expected, marked {horizon} min on"]
    L.append(f"  the target {lo_pct:g}% of ${budget:g} is ${target:.2f} a day; the basket's daily net has a spread of ${sd:.2f} on the week, so a live run would need about {tell:.0f} days before its mean is known to within that target" if tell is not None else "")
    for r in rows:
        L.append(f"    {r.plan.question[:60]}: ${r.parked:.2f} parked, net ${r.net_low:.2f}..{r.net_high:.2f}/day, {r.fills_per_day:.2f} fills/day")
    return "\n".join(x for x in L if x)


def collect(http, trades_source, plans: list[QuotePlan], markets: dict[str, Any], *, days: int = 7, interval: int = 60, fill: str = "through", horizon: int = 60,
            policies: tuple[Policy, ...] = (RIG, HALF_TICK), pocket: dict[str, PocketRow] | None = None, sweep: bool = False, log: Callable[[str], None] | None = None) -> list[YieldRow]:
    """Replay every plan's market (one history and one tape request each) and join it with the
    plan; with `sweep`, also replay the distance policies and keep what the sweep needs."""
    out: list[YieldRow] = []
    if sweep:
        policies = tuple(dict.fromkeys(policies + distance_policies()))
    for i, p in enumerate(plans):
        m = markets.get(p.condition_id)
        if m is None:
            continue
        try:
            v, _min_size, tick = market_params(m)
            if v <= 0:
                continue
            series = fetch_mids(http, m.yes_token, days=days)
            if len(series) < 2:
                continue
            prints = tape_prints(trades_source.trades(m.condition_id, closed=False), m.outcomes)
            reps = replay_all(series, prints, max_spread_cents=v, size=p.size, tick=tick, interval=interval, fill=fill, policies=policies)
        except Exception as exc:  # noqa: BLE001  one bad tape should not sink the scan
            if log:
                log(f"  {p.question[:50]!r}: replay failed: {exc}")
            continue
        row = evaluate(p, reps, horizon=horizon)
        if row is None:
            continue
        row.series = series
        pr = (pocket or {}).get(p.condition_id)
        if pr is not None:
            row.comp = competitor_total(pr.q_bid, pr.q_ask, pr.mid)
            if sweep:
                row.sweeps = [SweepRow(r.policy.extra_ticks, r, *reward_from_replay(p.rate_per_day, r, row.comp, interval), p.collateral) for r in reps if r.policy.name == "rig" and r.policy.recentre_ticks == 1.0]
        out.append(row)
        if log and (i + 1) % 10 == 0:
            log(f"  {i + 1}/{len(plans)} replayed")
    return out


# --------------------------------------------------------------------------- #
# The levers: distance from the mid, holding a fill for the pair, the hours quoted
# --------------------------------------------------------------------------- #

DISTANCE_TICKS = (-2, -1, 0, 1)  # extra ticks on half the max spread: the touch, a tick in, the rig's own, a tick out


def distance_policies(confirm: int = 3) -> tuple[Policy, ...]:
    return tuple(Policy("rig", confirm=confirm, extra_ticks=k) for k in DISTANCE_TICKS)


def reward_from_replay(rate: float, replay: Replay, comp: tuple[float, float], interval: int = 60) -> tuple[float, float]:
    """(low, high) $/day the pot would pay the quote the replay actually rested (its mean two-sided
    score over the readings, which carries the tick rounding and the drift) against the book's
    competitor totals."""
    minutes = replay.readings * interval / 60.0
    q = replay.score_minutes / minutes if minutes > 0 else 0.0
    lo_comp, hi_comp = comp
    if q <= 0:
        return 0.0, 0.0
    return rate * q / (q + hi_comp), rate * q / (q + lo_comp)


@dataclass
class SweepRow:
    extra_ticks: int
    replay: Replay
    reward_low: float
    reward_high: float
    parked: float

    @property
    def label(self) -> str:
        return {-2: "the touch", -1: "a tick in", 0: "the rig's", 1: "a tick out"}.get(self.extra_ticks, f"{self.extra_ticks:+d} ticks")

    def net(self, horizon: int = 60) -> tuple[float, float]:
        loss = self.replay.loss_per_day(horizon)
        return self.reward_low - loss, self.reward_high - loss


def render_sweep(rows: list[YieldRow], *, horizon: int = 60, interval: int = 60) -> str:
    """Across the markets, per distance: the score rested, the reward it would take, fills and
    losses, the net as a % a day of parked, and how many markets are positive on the low figure."""
    swept = [r for r in rows if r.sweeps]
    if not swept:
        return "  distance sweep: no market carried a book reading to sweep against"
    L = [f"  distance from the mid, {len(swept)} markets, {sum(r.replay.days for r in swept):.0f} market-days, fills marked {horizon} min on:",
         f"  {'quote':<11} {'score/mkt':>9} {'reward lo..hi $/d':>18} {'fills/mkt-d':>11} {'loss/mkt-d':>10} {'net lo..hi $/mkt-d':>19} {'%/d of parked':>14} {'mkts +':>6}"]
    for k in DISTANCE_TICKS:
        sub = [(r, s) for r in swept for s in r.sweeps if s.extra_ticks == k]
        if not sub:
            continue
        n = len(sub)
        md = sum(s.replay.days for _r, s in sub)
        q = sum(s.replay.score_minutes / max(1e-9, s.replay.readings * interval / 60.0) for _r, s in sub) / n
        lo = sum(s.reward_low for _r, s in sub) / n
        hi = sum(s.reward_high for _r, s in sub) / n
        fills = sum(len(s.replay.fills) for _r, s in sub) / md
        loss = sum(s.replay.loss_per_day(horizon) * s.replay.days for _r, s in sub) / md
        parked = sum(s.parked for _r, s in sub) / n
        pos = sum(1 for _r, s in sub if s.net(horizon)[0] > 0)
        L.append(f"  {sub[0][1].label:<11} {q:9.2f} {lo:8.2f}..{hi:<8.2f} {fills:11.2f} {loss:10.2f} {lo - loss:8.2f}..{hi - loss:<8.2f} {100 * (lo - loss) / parked:6.2f}..{100 * (hi - loss) / parked:<6.2f} {pos:3d}/{n}")
    L.append("  score/mkt: the mean two-sided score the quote rested per market (size × ((v − s) / v)^2, the tick rounding and the drift included).  reward: that score against the book's competitor totals at the scan.  net: reward − loss, per market-day; the last column is the markets positive on the low figure.")
    return "\n".join(L)


@dataclass
class HoldOutcome:
    fill: Fill
    horizon_h: float
    completes_at: bool  # within the horizon a reading showed the mid back at the fill price (a print at our capped other side is then plausible)
    completes_through: bool  # back a tick beyond it (that side's level cleared: the pair surely completed)
    mark: float | None  # the mid at the horizon (None: the series ended first)

    @property
    def censored(self) -> bool:
        return self.mark is None

    def loss(self) -> float | None:
        """Undoing the fill at the horizon, less a tick (positive = a loss)."""
        if self.mark is None:
            return None
        f = self.fill
        if f.side == "bid":
            return round((f.price - (self.mark - f.tick)) * f.size, 4)
        return round(((self.mark + f.tick) - f.price) * f.size, 4)


def hold_outcomes(fills: list[Fill], series: list[Point], *, horizon_h: float) -> list[HoldOutcome]:
    """For each fill: did the mid come back within `horizon_h` hours (the rig's capped other side
    then completes the pair at a tick over the fill), and where was it at the horizon?"""
    out: list[HoldOutcome] = []
    if not series:
        return out
    last = series[-1][0]
    for f in fills:
        end = f.ts + int(horizon_h * 3600)
        at = through = False
        for ts, mid in series:
            if ts <= f.ts:
                continue
            if ts > end:
                break
            if f.side == "bid":
                at = at or mid >= f.price - 1e-9
                through = through or mid >= f.price + f.tick - 1e-9
            else:
                at = at or mid <= f.price + 1e-9
                through = through or mid <= f.price - f.tick + 1e-9
        mark = mid_at(series, end) if end <= last else None
        out.append(HoldOutcome(f, horizon_h, at, through, mark))
    return out


def summarize_hold(outs: list[HoldOutcome], *, undo_minutes: int = 60) -> dict[str, Any]:
    """Across fills with a mark at the horizon: how many came back, what the rest cost at the
    horizon, and the expected value per fill of holding (a tick's gain when the pair completes,
    the horizon loss when it does not) against undoing at `undo_minutes`."""
    kept = [o for o in outs if not o.censored]
    n = len(kept)
    if n == 0:
        return {"fills": len(outs), "marked": 0}
    at = [o for o in kept if o.completes_at]
    through = [o for o in kept if o.completes_through]
    rest_at = [o for o in kept if not o.completes_at]
    rest_through = [o for o in kept if not o.completes_through]
    gain = lambda o: o.fill.tick * o.fill.size  # noqa: E731  the pair completes a tick over the fill
    ev_at = (sum(gain(o) for o in at) - sum(o.loss() or 0.0 for o in rest_at)) / n
    ev_through = (sum(gain(o) for o in through) - sum(o.loss() or 0.0 for o in rest_through)) / n
    undo = [o.fill.loss(undo_minutes) for o in kept]
    undo_mean = sum(x for x in undo if x is not None) / max(1, sum(1 for x in undo if x is not None))
    return {"fills": len(outs), "marked": n, "horizon_h": kept[0].horizon_h, "complete_at": len(at), "complete_through": len(through),
            "loss_rest_at": sum(o.loss() or 0.0 for o in rest_at) / max(1, len(rest_at)), "loss_rest_through": sum(o.loss() or 0.0 for o in rest_through) / max(1, len(rest_through)),
            "ev_hold_at": ev_at, "ev_hold_through": ev_through, "ev_undo": -undo_mean, "worst": min((o.loss() or 0.0) for o in kept) * -1 if kept else 0.0, "worst_loss": max((o.loss() or 0.0) for o in kept)}


def render_hold(rows: list[YieldRow], *, horizons: tuple[float, ...] = (6.0, 24.0, 48.0), undo_minutes: int = 60) -> str:
    """The rig's rule after a fill is to hold and let the capped other side complete the pair.
    What the history says that is worth, per fill, against undoing."""
    L = [f"  holding a fill for the pair (the rig's rule) against undoing it {undo_minutes} min on, every fill of the rig's quote across the markets:",
         f"  {'horizon':>8} {'fills':>5} {'marked':>6} {'came back at':>12} {'through':>8} {'rest lose $':>11} {'EV hold $/fill':>16} {'EV undo':>8} {'worst $':>8}"]
    for h in horizons:
        outs = [o for r in rows for o in hold_outcomes(r.replay.fills, r.series, horizon_h=h)]
        d = summarize_hold(outs, undo_minutes=undo_minutes)
        if d.get("marked", 0) == 0:
            L.append(f"  {h:6.0f}h {d['fills']:5d} {0:6d}  (no fill has a mark that far on)")
            continue
        L.append(f"  {h:6.0f}h {d['fills']:5d} {d['marked']:6d} {100 * d['complete_at'] / d['marked']:11.0f}% {100 * d['complete_through'] / d['marked']:7.0f}% {d['loss_rest_through']:11.2f} {d['ev_hold_at']:7.2f}..{d['ev_hold_through']:<7.2f} {d['ev_undo']:8.2f} {d['worst_loss']:8.2f}")
    L.append("  came back: within the horizon a reading showed the mid at the fill price again (at) or a tick beyond (through), when the capped other side completes the pair for a tick's gain.  rest lose: mean undo cost at the horizon of the fills that did not come back (through).  EV hold: per fill, a tick's gain when it came back (at .. through), the horizon loss when not; EV undo: minus the mean undo cost at the undo mark.  worst: the costliest single fill at the horizon.")
    return "\n".join(L)


def fills_by_hour(rows: list[YieldRow], *, horizon: int = 60) -> list[tuple[int, int, float]]:
    """(UTC hour, fills, their losses) across the markets, for the rig's quote."""
    by: dict[int, list[float]] = {h: [0, 0.0] for h in range(24)}
    for r in rows:
        for f in r.replay.fills:
            h = datetime.fromtimestamp(f.ts, tz=timezone.utc).hour
            by[h][0] += 1
            by[h][1] += f.loss(horizon) or 0.0
    return [(h, int(c), l) for h, (c, l) in by.items()]


def window_net(rows: list[YieldRow], hours: set[int], *, horizon: int = 60, since: int | None = None, until: int | None = None) -> tuple[float, float, float]:
    """(reward, loss, market-days) per day summed across the markets for a quote that rests only
    in the UTC `hours`, over the fills (and the span) between `since` and `until`."""
    reward = loss = days = 0.0
    frac = len(hours) / 24.0
    for r in rows:
        t0, t1 = r.replay.start_ts, r.replay.start_ts + int(r.replay.span_hours * 3600)
        a = max(t0, since) if since is not None else t0
        b = min(t1, until) if until is not None else t1
        if b <= a:
            continue
        d = (b - a) / 86400.0
        days += d
        reward += r.reward_low * frac * d
        loss += sum(f.loss(horizon) or 0.0 for f in r.replay.fills if a <= f.ts < b and datetime.fromtimestamp(f.ts, tz=timezone.utc).hour in hours)
    return reward, loss, days


def best_window(rows: list[YieldRow], *, length: int = 12, horizon: int = 60, since: int | None = None, until: int | None = None) -> tuple[int, float, float, float]:
    """The contiguous UTC window of `length` hours with the best net per market-day over the
    period: (start hour, reward, loss, market-days)."""
    best = None
    for start in range(24):
        hrs = {(start + i) % 24 for i in range(length)}
        reward, loss, days = window_net(rows, hrs, horizon=horizon, since=since, until=until)
        if days <= 0:
            continue
        net = (reward - loss) / days
        if best is None or net > best[1]:
            best = (start, net, reward, loss, days)
    if best is None:
        return 0, 0.0, 0.0, 0.0
    start, _net, reward, loss, days = best
    return start, reward, loss, days


def render_hours(rows: list[YieldRow], *, horizon: int = 60, length: int = 12, split_days: float = 4.0) -> str:
    """Where in the day the fills land, and whether a quote that rests only in the quiet hours,
    chosen on the first `split_days` of the span, keeps its edge on the rest."""
    hist = fills_by_hour(rows, horizon=horizon)
    total = sum(c for _h, c, _l in hist) or 1
    L = [f"  fills by UTC hour, the rig's quote across the markets ({total} fills, fills marked {horizon} min on):"]
    L.append("  " + " ".join(f"{h:02d}:{c:<3d}" for h, c, _l in hist))
    top = sorted(hist, key=lambda x: -x[1])[:6]
    L.append("  busiest hours: " + ", ".join(f"{h:02d}h {c} fills ${l:.2f}" for h, c, l in top))
    if not rows:
        return "\n".join(L)
    t0 = min(r.replay.start_ts for r in rows)
    split = t0 + int(split_days * 86400)
    start, reward_in, loss_in, days_in = best_window(rows, length=length, horizon=horizon, until=split)
    hrs = {(start + i) % 24 for i in range(length)}
    full_in = window_net(rows, set(range(24)), horizon=horizon, until=split)
    reward_out, loss_out, days_out = window_net(rows, hrs, horizon=horizon, since=split)
    full_out = window_net(rows, set(range(24)), horizon=horizon, since=split)
    if days_in > 0:
        L.append(f"  best {length}-hour window on the first {split_days:g} days: {start:02d}:00-{(start + length) % 24:02d}:00 UTC, net ${(reward_in - loss_in) / days_in:.2f} a market-day against ${(full_in[0] - full_in[1]) / max(1e-9, full_in[2]):.2f} quoting all day (reward ${reward_in / days_in:.2f} vs ${full_in[0] / max(1e-9, full_in[2]):.2f}, loss ${loss_in / days_in:.2f} vs ${full_in[1] / max(1e-9, full_in[2]):.2f})")
    if days_out > 0:
        L.append(f"  the same window on the remaining {days_out / max(1, len(rows)):.1f} days: net ${(reward_out - loss_out) / days_out:.2f} a market-day against ${(full_out[0] - full_out[1]) / max(1e-9, full_out[2]):.2f} all day (reward ${reward_out / days_out:.2f} vs ${full_out[0] / max(1e-9, full_out[2]):.2f}, loss ${loss_out / days_out:.2f} vs ${full_out[1] / max(1e-9, full_out[2]):.2f})")
    L.append("  a quote that rests only in some hours earns the pot for those minutes only (the programme samples every minute) and meets only those hours' fills; the window is chosen on the first days and read on the rest, so the second line is the test.")
    return "\n".join(L)
