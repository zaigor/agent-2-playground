"""Bracket-crossing test: the count-window model scored against outcomes using nothing but
Polymarket's own resolution metadata (memo section 19e).

Polymarket resolves a count bracket early, the moment the running count passes its ceiling. In
"Donald Trump # Truth Social posts September 11 - September 18, 2026" the "<20" market closed on
the 14th, "20-39" on the 15th, "40-59" on the 16th and "60-79" at 00:18 on the 18th, each a NO
resolved by UMA while the window was still open. Every early close is therefore a public,
timestamped fact about the running count: at that moment the count was at least the ceiling
plus one. The sequence of closes is a staircase lower bound on the count through the window, it
exists for every resolved posts window, and it is what lets the count-aware model of `counts` be
tested against outcomes without the tracker's own export, which this container cannot reach.
(Earthquake, ship and weather brackets all close after the window ends, so the test covers the
posts families only.)

The test is the one `counts` would run with a real catalog, handicapped twice: the count used is
the lower bound (the true count at the close time is higher by whatever accumulated during the
proposer's and UMA's lag), and there is no phase profile (the remainder is the trailing windows'
mean rate times the fraction of the window left). Both handicaps work against the model, so a
positive result is conservative and a null one is ambiguous.

For each early close at time t of a NO bracket in window [a, b):

  n_min    = the largest ceiling closed by t, plus one
  mu, r    = mean and dispersion of the totals of the k trailing windows of the same series that
             ended by t (a total is the winning bracket's midpoint; the open top bracket counts
             as its floor plus half the ladder's typical width)
  mu_rem   = mu * (b - t) / (b - a)
  p(i)     = P(lo_i <= n_min + R <= hi_i), R ~ NB(mu_rem, r), Poisson when the totals are not
             over-dispersed, for every bracket i still open at t

(with `count="interval"`, n_min is replaced by every value from n_min to the ceiling of the lowest
bracket still alive at t, averaged: the crowd has not killed that bracket, so the count has most
likely not passed it either; this is the post-hoc variant, see the memo) and each (bracket, t, p)
is a `signal` row: scored against the last trade at or before t and the
resolution, with the market's taker fee for the paper trade. Standard errors cluster by event
(one ladder at one moment is one observation), which is stricter than `signal`'s per-market
clustering.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .counts import SignalOut, p_bracket, parse_bracket, parse_window
from .flow import yes_price_series
from .polymarket import PolyMarket, parse_event
from .signal import ScoredRow, SignalReport, SignalRow, cluster_se, render_signal, score_signal

PhaseName = str


def parse_gamma_time(text: str | None) -> datetime | None:
    """Gamma writes close times four ways: '2026-09-07T15:33:58Z', '2026-09-07 15:33:58+00',
    '2026-09-07 10:12:58.760575+00' and '2026-09-07 10:12:58.760575+00:00:00'."""
    if not text:
        return None
    s = str(text).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    elif s.endswith("+00:00:00"):
        s = s[:-9] + "+00:00"
    elif s.endswith("+00"):
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Bracket:
    condition_id: str
    label: str
    lo: int
    hi: int | None
    closed_at: datetime | None
    resolved_yes: bool | None
    market: PolyMarket = field(repr=False)
    crossed_at: datetime | None = None  # when the crossing is dated: the close (default) or the price collapse

    def __post_init__(self) -> None:
        if self.crossed_at is None:
            self.crossed_at = self.closed_at


@dataclass
class Window:
    series: str
    event_id: str
    title: str
    a: datetime
    b: datetime
    brackets: list[Bracket]  # sorted by lo
    width: float  # typical bracket width, for the open top's midpoint
    min_in: timedelta = timedelta(hours=1)

    @property
    def length(self) -> timedelta:
        return self.b - self.a

    @property
    def winner(self) -> Bracket | None:
        ws = [x for x in self.brackets if x.resolved_yes]
        return ws[0] if len(ws) == 1 else None

    @property
    def total_mid(self) -> float | None:
        """The window's total as the winning bracket's midpoint."""
        w = self.winner
        if w is None:
            return None
        return (w.lo + w.hi) / 2.0 if w.hi is not None else w.lo + self.width / 2.0

    def early_no(self) -> list[Bracket]:
        """NO brackets that closed while the window was open, in close order."""
        return sorted((x for x in self.brackets if x.resolved_yes is False and x.closed_at is not None and self.a + self.min_in < x.closed_at < self.b), key=lambda x: x.closed_at)

    def crossings(self) -> list[Bracket]:
        """Early closes with a ceiling, dated by `crossed_at` and inside the window: the count had
        passed the ceiling by then."""
        early = {x.condition_id for x in self.early_no()}
        return sorted((x for x in self.brackets if x.condition_id in early and x.hi is not None and x.crossed_at is not None and self.a + self.min_in < x.crossed_at < self.b), key=lambda x: x.crossed_at)

    def consistent(self) -> bool:
        """Every early close is below the winning bracket (a crossing, not a bulk resolution)."""
        w = self.winner
        if w is None:
            return False
        return all(x.hi is not None and x.hi < w.lo for x in self.early_no())


def load_windows(events: list[dict[str, Any]], series: str, *, log: Callable[[str], None] | None = None) -> list[Window]:
    """One Window per event whose rule states a window and that has at least two brackets."""
    out: list[Window] = []
    for raw in events:
        ev = parse_event(raw)
        brs: list[Bracket] = []
        win: tuple[datetime, datetime] | None = None
        for mk in ev.markets:
            text = mk.raw.get("description") or raw.get("description") or ""
            w = parse_window(text, end_hint=mk.end_date or ev.end_date)
            if w is None:
                continue
            win = win or w
            br = parse_bracket(mk.group_item_title, mk.question)
            if br is None:
                continue
            closed_at = parse_gamma_time(mk.raw.get("closedTime")) or parse_gamma_time(mk.raw.get("umaEndDate"))
            brs.append(Bracket(mk.condition_id, mk.group_item_title or mk.question, br[0], br[1], closed_at, mk.resolved_yes, mk))
        if win is None or len(brs) < 2:
            if log:
                log(f"skip {ev.title[:60]!r}: no window or fewer than two brackets")
            continue
        brs.sort(key=lambda x: (x.lo, x.hi if x.hi is not None else 10**9))
        widths = [x.hi - x.lo + 1 for x in brs if x.hi is not None]
        out.append(Window(series, ev.id, ev.title, win[0], win[1], brs, float(statistics.median(widths)) if widths else 1.0))
    out.sort(key=lambda w: w.b)
    return out


@dataclass
class BaseRate:
    mu: float
    r: float | None
    n: int


def base_rate(windows: list[Window], current: Window, t: datetime, *, k: int = 8, min_windows: int = 3) -> BaseRate | None:
    """Mean and dispersion of the totals of the last `k` windows of the series that ended by `t`
    and have (within 20%) the current window's length. Nothing after `t` is used."""
    length = current.length.total_seconds()
    prev = [w for w in windows if w is not current and w.b <= t and w.total_mid is not None and abs(w.length.total_seconds() - length) <= 0.2 * length]
    prev.sort(key=lambda w: w.b)
    tot = [w.total_mid for w in prev[-k:] if w.total_mid is not None]
    if len(tot) < max(1, min_windows):
        return None
    mu = sum(tot) / len(tot)
    var = statistics.pvariance(tot) if len(tot) > 1 else mu
    r = (mu * mu / (var - mu)) if (var > mu and mu > 0) else None
    return BaseRate(mu, r, len(tot))


@dataclass
class Decision:
    window: Window
    t: datetime
    n_min: int  # the count is at least this (largest crossed ceiling plus one)
    frac: float
    base: BaseRate
    mu_rem: float
    open_brackets: list[Bracket]
    n_max: int | None = None  # ... and, if the next bracket up is still alive, at most this

    def counts(self, count: str, max_points: int = 21) -> list[int]:
        """The count values the model averages over: the lower bound alone, or the interval up to
        the ceiling of the lowest bracket still alive (the crowd has not killed it, so the count
        has most likely not passed it either)."""
        if count != "interval" or self.n_max is None or self.n_max < self.n_min:
            return [self.n_min]
        span = self.n_max - self.n_min
        if span < max_points:
            return list(range(self.n_min, self.n_max + 1))
        return sorted({self.n_min + round(i * span / (max_points - 1)) for i in range(max_points)})


def decisions(windows: list[Window], *, k: int = 8, min_windows: int = 3, merge: timedelta = timedelta(minutes=10)) -> tuple[list[Decision], dict[str, int]]:
    """One decision per distinct early-close time (closes within `merge` of each other count once,
    at the last of them), with the count lower bound and the base rate known at that time."""
    skipped: dict[str, int] = defaultdict(int)
    out: list[Decision] = []
    for w in windows:
        xs = w.crossings()
        if not xs:
            skipped["no early close"] += 1
            continue
        if not w.consistent():
            skipped["early close above the winner"] += 1
            continue
        groups: list[list[Bracket]] = []
        for x in xs:
            if groups and x.crossed_at is not None and groups[-1][-1].crossed_at is not None and x.crossed_at - groups[-1][-1].crossed_at <= merge:
                groups[-1].append(x)
            else:
                groups.append([x])
        for g in groups:
            t = g[-1].crossed_at
            assert t is not None
            n_min = max(x.hi + 1 for x in xs if x.hi is not None and x.crossed_at is not None and x.crossed_at <= t)
            base = base_rate(windows, w, t, k=k, min_windows=min_windows)
            if base is None:
                skipped["too few trailing windows"] += 1
                continue
            frac = (t - w.a) / (w.b - w.a)
            open_ = [x for x in w.brackets if x.crossed_at is None or x.crossed_at > t]
            if not open_:
                skipped["nothing open"] += 1
                continue
            lowest = min(open_, key=lambda x: x.lo)
            n_max = lowest.hi if lowest.hi is not None else n_min + int(w.width) - 1
            out.append(Decision(w, t, n_min, frac, base, base.mu * (1.0 - frac), open_, n_max))
    return out, dict(skipped)


def signal_rows(decs: list[Decision], *, count: str = "lower") -> list[SignalOut]:
    rows: list[SignalOut] = []
    for d in decs:
        ns = d.counts(count)
        for x in d.open_brackets:
            p = sum(p_bracket(n, d.mu_rem, d.base.r, (x.lo, x.hi)) for n in ns) / len(ns)
            n_txt = f"n>={d.n_min}" if len(ns) == 1 else f"n={d.n_min}..{d.n_max}"
            note = f"{d.window.series}|{d.window.event_id}|{x.label}|{n_txt}|frac={d.frac:.2f}|mu={d.base.mu:.0f}|k={d.base.n}"
            rows.append(SignalOut(x.condition_id, d.t.isoformat(timespec="minutes"), round(p, 4), note))
    return rows


def note_field(note: str, key: str) -> str:
    for part in note.split("|"):
        if part.startswith(key + "="):
            return part[len(key) + 1 :]
    return ""


def phase_of(frac: float) -> PhaseName:
    return "early (<1/3)" if frac < 1 / 3 else ("mid" if frac < 2 / 3 else "late (>2/3)")


@dataclass
class GroupStat:
    name: str
    windows: int
    decisions: int
    rows: int
    brier_market: float
    brier_signal: float
    gap: float | None  # market - signal, event-clustered se
    gap_se: float | None
    blend_gap: float | None
    blend_gap_se: float | None
    trades: int
    pnl_per_100: float | None
    pnl_per_100_se: float | None


def breakdown(scored: list[ScoredRow], key: Callable[[ScoredRow], str]) -> list[GroupStat]:
    groups: dict[str, list[ScoredRow]] = defaultdict(list)
    for s in scored:
        groups[key(s)].append(s)
    out: list[GroupStat] = []
    for name, rows in groups.items():
        per: dict[str, dict[str, tuple[float, float]]] = {k: defaultdict(lambda: (0.0, 0.0)) for k in ("gap", "blend", "pnl")}

        def add(k: str, ev: str, v: float, w: float = 1.0) -> None:
            s0, c0 = per[k][ev]
            per[k][ev] = (s0 + v, c0 + w)

        for s in rows:
            ev = s.note.split("|")[1] if s.note.count("|") >= 1 else s.market
            add("gap", ev, s.brier_market - s.brier_signal)
            add("blend", ev, s.brier_market - s.brier_blend)
            if s.trade:
                add("pnl", ev, s.pnl * 100.0, s.stake * 100.0)

        def mean(k: str) -> float | None:
            c = sum(c0 for _, c0 in per[k].values())
            return (sum(v for v, _ in per[k].values()) / c) if c else None

        trades = sum(1 for s in rows if s.trade)
        pnl_se = cluster_se(per["pnl"]) if trades else None
        out.append(GroupStat(
            name, len({s.note.split("|")[1] for s in rows if "|" in s.note}), len({(s.note.split("|")[1] if "|" in s.note else s.market, s.time) for s in rows}), len(rows),
            statistics.mean(s.brier_market for s in rows), statistics.mean(s.brier_signal for s in rows),
            mean("gap"), cluster_se(per["gap"]), mean("blend"), cluster_se(per["blend"]),
            trades, (mean("pnl") * 100.0 if mean("pnl") is not None else None), (pnl_se * 100.0 if pnl_se is not None else None),
        ))
    return out


def collapse_time(series: list[tuple[int, float]], close_ts: float, *, threshold: float = 0.10) -> int | None:
    """The first print after the last one at or above `threshold` before `close_ts`: when the
    market stopped pricing the bracket as alive. None when it never printed that high (dead from
    the seed) or never printed below it before the close."""
    hits = [t for t, p in series if p >= threshold and t <= close_ts]
    if not hits:
        return None
    last_alive = max(hits)
    after = [t for t, _ in series if last_alive < t <= close_ts]
    return min(after) if after else None


def date_crossings_by_collapse(windows: list[Window], trades_source, *, threshold: float = 0.10) -> dict[str, list[float]]:
    """Set each crossed bracket's `crossed_at` to its price collapse (kept at the close when there
    is none inside the window) and return, per series, the hours between the collapse and the
    close: how long the crowd had the crossing before UMA did, which is how stale the lower
    bound is when the test is dated by the close."""
    lags: dict[str, list[float]] = defaultdict(list)
    for w in windows:
        for x in w.crossings():
            if x.closed_at is None:
                continue
            series = yes_price_series(trades_source.trades(x.condition_id, closed=True), x.market.outcomes)
            c = collapse_time(series, x.closed_at.timestamp(), threshold=threshold)
            if c is None:
                continue
            ct = datetime.fromtimestamp(c, tz=timezone.utc)
            lags[w.series].append((x.closed_at - ct).total_seconds() / 3600.0)
            if w.a < ct < x.closed_at:
                x.crossed_at = ct
    return dict(lags)


def _quartiles(xs: list[float]) -> dict[str, float]:
    s = sorted(xs)
    n = len(s)
    q = lambda f: s[min(n - 1, int(f * (n - 1)))]  # noqa: E731
    return {"n": float(n), "median": q(0.5), "q1": q(0.25), "q3": q(0.75)}


@dataclass
class CrossingsResult:
    series: list[str]
    windows: int
    windows_consistent: int
    decisions: int
    skipped: dict[str, int]
    rows: list[SignalOut]
    report: SignalReport
    by_series: list[GroupStat]
    by_phase: list[GroupStat]
    lags: dict[str, dict[str, float]]
    timing: str = "close"
    count: str = "lower"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timing": self.timing, "count": self.count, "series": self.series, "windows": self.windows, "windows_consistent": self.windows_consistent, "decisions": self.decisions, "skipped": self.skipped,
            "rows": [asdict(r) for r in self.rows], "report": self.report.to_dict(),
            "by_series": [asdict(g) for g in self.by_series], "by_phase": [asdict(g) for g in self.by_phase], "lags": self.lags,
        }


def run_crossings(events_by_series: dict[str, list[dict[str, Any]]], trades_source, *, k_windows: int = 8, min_windows: int = 3, edge: float = 0.05, max_stale_hours: float = 24.0, horizon_min: int = 30, timing: str = "close", count: str = "lower", lags: bool = True, log: Callable[[str], None] | None = None) -> CrossingsResult:
    """`timing="close"` dates each crossing by the bracket's UMA close (a certain lower bound,
    stale by the proposer's lag); `timing="collapse"` dates it by the bracket's price collapse
    (when the crowd itself marked the bracket dead: fresher, but a market can sell a bracket to
    nothing a little before the count actually passes it, so the bound is no longer certain)."""
    if timing not in ("close", "collapse"):
        raise ValueError("timing must be 'close' or 'collapse'")
    if count not in ("lower", "interval"):
        raise ValueError("count must be 'lower' or 'interval'")
    windows: list[Window] = []
    for slug, events in events_by_series.items():
        ws = load_windows(events, slug, log=log)
        if log:
            log(f"{slug}: {len(ws)} windows, {sum(1 for w in ws if w.crossings())} with early closes, {sum(1 for w in ws if w.crossings() and not w.consistent())} inconsistent")
        windows.extend(ws)
    lag_raw: dict[str, list[float]] = {}
    if timing == "collapse":
        lag_raw = date_crossings_by_collapse(windows, trades_source)
    elif lags:
        lag_raw = date_crossings_by_collapse([Window(w.series, w.event_id, w.title, w.a, w.b, [Bracket(x.condition_id, x.label, x.lo, x.hi, x.closed_at, x.resolved_yes, x.market) for x in w.brackets], w.width) for w in windows], trades_source)
    decs_all: list[Decision] = []
    skipped: dict[str, int] = defaultdict(int)
    for slug in events_by_series:
        decs, sk = decisions([w for w in windows if w.series == slug], k=k_windows, min_windows=min_windows)
        decs_all.extend(decs)
        for k, v in sk.items():
            skipped[k] += v
    rows = signal_rows(decs_all, count=count)
    markets = {x.condition_id: x.market for w in windows for x in w.brackets}
    srows = [SignalRow(r.market, datetime.fromisoformat(r.time), r.p, "", r.note) for r in rows]
    report = score_signal(srows, markets, trades_source, horizon_min=horizon_min, edge=edge, max_stale_hours=max_stale_hours, log=log)
    by_series = sorted(breakdown(report.scored, lambda s: s.note.split("|")[0]), key=lambda g: g.name)
    order = ["early (<1/3)", "mid", "late (>2/3)"]
    by_phase = sorted(breakdown(report.scored, lambda s: phase_of(float(note_field(s.note, "frac") or 0.0))), key=lambda g: order.index(g.name) if g.name in order else 9)
    lag_summary = {k: _quartiles(v) for k, v in lag_raw.items() if v}
    return CrossingsResult(list(events_by_series), len(windows), sum(1 for w in windows if w.crossings() and w.consistent()), len(decs_all), dict(skipped), rows, report, by_series, by_phase, lag_summary, timing, count)


def _pm(x: float | None, se: float | None, f: str = "+.4f") -> str:
    if x is None:
        return "-"
    return f"{x:{f}}" + (f" ± {se:{f.lstrip('+')}}" if se is not None else "")


def render_crossings(res: CrossingsResult, *, edge: float = 0.05, horizon_min: int = 30) -> str:
    L = [f"Bracket-crossing test, crossings dated by the {'UMA close' if res.timing == 'close' else 'price collapse'}, count {'= the lower bound' if res.count == 'lower' else 'spread up to the next live bracket'}: {res.windows} windows in {len(res.series)} series, {res.windows_consistent} with usable early closes, {res.decisions} decision times, {len(res.rows)} rows" + (f"; skipped windows/decisions: {', '.join(f'{v} {k}' for k, v in res.skipped.items())}" if res.skipped else "") + "."]
    L.append(render_signal(res.report, horizon_min, edge))
    L.append("  (the ± above cluster by market; the tables below cluster by event, one ladder at one moment being one observation)")
    hdr = f"  {'group':34s} {'win':>4s} {'dec':>5s} {'rows':>5s}  {'Brier mkt/model':>16s}  {'market-model':>18s}  {'market-blend':>18s}  {'trades':>6s}  {'P&L/$100':>18s}"
    for title, groups in (("by series", res.by_series), ("by window phase at the decision time", res.by_phase)):
        L.append(f"  {title}:")
        L.append(hdr)
        for g in groups:
            L.append(f"  {g.name[:34]:34s} {g.windows:4d} {g.decisions:5d} {g.rows:5d}  {g.brier_market:7.4f} /{g.brier_signal:7.4f}  {_pm(g.gap, g.gap_se):>18s}  {_pm(g.blend_gap, g.blend_gap_se):>18s}  {g.trades:6d}  {_pm(g.pnl_per_100, g.pnl_per_100_se, '+.2f'):>18s}")
    if res.lags:
        L.append("  hours from a crossed bracket's price collapse (first print under 10c that held) to its UMA close (median [q1, q3], n): how long the crowd had the crossing before the close dated it")
        for k, q in sorted(res.lags.items()):
            L.append(f"    {k:34s} {q['median']:6.1f} [{q['q1']:5.1f}, {q['q3']:5.1f}]  n={int(q['n'])}")
    return "\n".join(L)
