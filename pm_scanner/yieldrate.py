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
from .requote import HALF_TICK, RIG, Fill, Policy, Replay, replay_market


@dataclass
class YieldRow:
    plan: QuotePlan
    replay: Replay  # the rig's rule
    alt: Replay | None = None  # the half-tick rule, for the `kept` comparison
    horizon: int = 60  # minutes after a fill at which it is marked (60 = held an hour, 10 = undone at once)
    daily: list[tuple[str, float, float]] = field(default_factory=list)  # (UTC day, reward_low × kept pro-rated, fill losses)

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
            policies: tuple[Policy, ...] = (RIG, HALF_TICK), log: Callable[[str], None] | None = None) -> list[YieldRow]:
    """Replay every plan's market and join it with the plan."""
    out: list[YieldRow] = []
    for i, p in enumerate(plans):
        m = markets.get(p.condition_id)
        if m is None:
            continue
        try:
            got = replay_market(http, trades_source, m, days=days, interval=interval, fill=fill, size=p.size, policies=policies)
        except Exception as exc:  # noqa: BLE001  one bad tape should not sink the scan
            if log:
                log(f"  {p.question[:50]!r}: replay failed: {exc}")
            continue
        if got is None:
            continue
        _name, reps = got
        row = evaluate(p, reps, horizon=horizon)
        if row is not None:
            out.append(row)
        if log and (i + 1) % 10 == 0:
            log(f"  {i + 1}/{len(plans)} replayed")
    return out
