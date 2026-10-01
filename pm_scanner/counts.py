"""Count-window markets: "how many X between A and B" bracket ladders.

Polymarket runs dozens of these as recurring series: posts on X / Truth Social per week
(xtracker), 6.5+ and 5.5+ earthquakes per week (USGS), ships through Hormuz and Bab el-Mandeb
per week (IMF PortWatch), tornadoes per month (NCEI), days of Claude downtime per month, and so
on. They share one structure: a window [a, b), a running count, and a ladder of brackets.

This module gives three things:

1. Parsers for the Gamma wording: `parse_window` reads the window out of the market description
   ("between October 2, 12:00 PM ET and October 9, 2026, 12:00 PM ET", "between September 28,
   2026, 12:00 AM ET and October 4, 2026, 11:59 PM ET", "for all days from October 5, 2026,
   through October 11, 2026, inclusive") and `parse_bracket` reads the bracket out of the
   outcome label or question ("0-19", "200+", "<20", "more than 5", "≤6", "fewer than 20 ships",
   "between 20 and 34 tornadoes", "90 or more", "no states").

2. A pre-registered model from a catalog of timestamped occurrences (one row per post, quake
   or ship, or one row per day with a count): at decision time t inside the window, the count so
   far is known exactly, and the remainder is negative-binomial with the mean and dispersion of
   the trailing K windows of the same length, allocated over the remaining time by the
   empirical phase profile of those windows (posting rhythm) or uniformly. P(bracket) is the
   probability that count-so-far plus remainder lands inside it. Nothing is fitted on outcomes.
   `build_signal_rows` writes the `market,time,p` CSV that `signal` scores.

3. `headroom`: how much room the market leaves, measured from the price alone. For each
   resolved market, the price at the start, middle and late part of its window is scored against
   the outcome, next to two references that need no outside data: a uniform 1/k over the ladder
   and a point-in-time climatology (how often this bracket label has won in the series so far).
   If the climatology blend beats the market, the crowd is miscalibrated on base rates and a
   catalog model has room; if the market is already sharp at the window start, only the live
   count can add anything.
"""
from __future__ import annotations

import bisect
import csv
import math
import random
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .flow import price_at, yes_price_series
from .polymarket import PolyEvent, parse_event
from .signal import cluster_se

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
_MONTH_NUM = {m: i for i, m in enumerate(MONTHS.split("|"), start=1)}

# "between October 2, 12:00 PM ET and October 9, 2026, 12:00 PM ET" / "from October 1 12:00 PM ET to October 3, 2026 12:00 PM ET"
_RANGE_ET = re.compile(
    rf"(?:between|from)\s+({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?(?:\s+(\d{{4}}))?,?\s+(\d{{1,2}}):(\d{{2}})\s*(AM|PM)\s*(?:ET|EST|EDT),?"
    rf"\s+(?:and|to|through)\s+({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?(?:\s+(\d{{4}}))?,?\s+(\d{{1,2}}):(\d{{2}})\s*(AM|PM)\s*(?:ET|EST|EDT)",
    re.I,
)
# "for all days from October 5, 2026, through October 11, 2026, inclusive" (PortWatch reports UTC days)
_RANGE_DAYS = re.compile(rf"from\s+({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}}),?\s+(?:through|to|until)\s+({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", re.I)
# "US Tornadoes in October 2026", "Will Claude go down on __ days in August?" (year from the end date)
_MONTH_YEAR = re.compile(rf"\bin\s+({MONTHS}),?\s+(\d{{4}})\b", re.I)
_MONTH_ONLY = re.compile(rf"\bin\s+({MONTHS})\b(?!\s+\d)", re.I)


def _et(y: int, m: int, d: int, hh: int, mm: int, ampm: str) -> datetime:
    h = hh % 12 + (12 if ampm.upper() == "PM" else 0)
    return datetime(y, m, d, h, mm, tzinfo=ET).astimezone(UTC)


def parse_window(text: str, *, end_hint: datetime | None = None) -> tuple[datetime, datetime] | None:
    """The [start, end) of the count window in UTC, read from a market or event description.

    Returns None when no known pattern matches. `end_hint` (the market's end date) supplies the
    year when the text gives none."""
    if not text:
        return None
    m = _RANGE_ET.search(text)
    if m:
        m1, d1, y1, h1, mi1, ap1, m2, d2, y2, h2, mi2, ap2 = m.groups()
        yy2 = int(y2) if y2 else (int(y1) if y1 else (end_hint.year if end_hint else None))
        if yy2 is None:
            return None
        yy1 = int(y1) if y1 else (yy2 - 1 if _MONTH_NUM[m1.lower()] > _MONTH_NUM[m2.lower()] else yy2)
        a = _et(yy1, _MONTH_NUM[m1.lower()], int(d1), int(h1), int(mi1), ap1)
        b = _et(yy2, _MONTH_NUM[m2.lower()], int(d2), int(h2), int(mi2), ap2)
        if (int(h2), int(mi2), ap2.upper()) == (11, 59, "PM"):
            b += timedelta(minutes=1)  # "11:59 PM" means the end of that day
        return (a, b) if b > a else None
    m = _RANGE_DAYS.search(text)
    if m:
        m1, d1, y1, m2, d2, y2 = m.groups()
        a = datetime(int(y1), _MONTH_NUM[m1.lower()], int(d1), tzinfo=UTC)
        b = datetime(int(y2), _MONTH_NUM[m2.lower()], int(d2), tzinfo=UTC) + timedelta(days=1)
        return (a, b) if b > a else None
    return None


def parse_month_window(text: str, *, end_hint: datetime | None = None) -> tuple[datetime, datetime] | None:
    """A calendar-month window ("US Tornadoes in October 2026"), in UTC."""
    m = _MONTH_YEAR.search(text or "")
    if m:
        mon, year = _MONTH_NUM[m.group(1).lower()], int(m.group(2))
    else:
        m = _MONTH_ONLY.search(text or "")
        if not m or end_hint is None:
            return None
        mon = _MONTH_NUM[m.group(1).lower()]
        year = end_hint.year if mon <= end_hint.month + 1 else end_hint.year - 1
    a = datetime(year, mon, 1, tzinfo=UTC)
    b = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    return a, b


_STRIP = [
    re.compile(r"\d+(?:\.\d+)?\s+or\s+(?:above|higher|greater)\s+magnitude", re.I),  # "6.5 or above magnitude"
    re.compile(r"magnitude\s+\d+(?:\.\d+)?", re.I),
    re.compile(rf"({MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?,?(?:\s+\d{{4}})?", re.I),  # dates
    re.compile(r"\(.*?\)"),
]


def _clean(text: str) -> str:
    t = text.replace("–", "-").replace("—", "-").replace("−", "-").replace(",", "")
    for rx in _STRIP:
        t = rx.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_bracket(label: str, question: str = "") -> tuple[int, int | None] | None:
    """(low, high) of a count bracket; high is None for an open top. Tries the outcome label
    first ("0-19", "<20", "200+", "more than 5", "≤6"), then the question text."""
    for raw in (label, question):
        if not raw:
            continue
        t = _clean(raw)
        if re.search(r"\bno\s+(?:states|posts|tweets|earthquakes|ships|tornadoes|days)\b", t, re.I):
            return (0, 0)
        m = re.search(r"(?:under|fewer than|less than|below)\s+(\d+)|<\s*(\d+)", t, re.I)
        if m:
            n = int(m.group(1) or m.group(2))
            return (0, max(0, n - 1))
        m = re.search(r"(?:≤|<=|at most)\s*(\d+)|(\d+)\s+or\s+(?:fewer|less)", t, re.I)
        if m:
            return (0, int(m.group(1) or m.group(2)))
        m = re.search(r"(?:more than|over|above)\s+(\d+)|>\s*(\d+)", t, re.I)
        if m:
            return (int(m.group(1) or m.group(2)) + 1, None)
        m = re.search(r"(?:≥|>=|at least)\s*(\d+)|(\d+)\s*(?:\+|or more|or above|or higher|or greater)", t, re.I)
        if m:
            return (int(m.group(1) or m.group(2)), None)
        m = re.search(r"between\s+(\d+)\s+and\s+(\d+)|(\d+)\s*(?:-|to)\s*(\d+)", t, re.I)
        if m:
            lo, hi = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            return (int(lo), int(hi))
        m = re.fullmatch(r"(\d+)", t)
        if m:
            n = int(m.group(1))
            return (n, n)
        m = re.search(r"\b(\d+)\s+(?:earthquakes?|posts?|tweets?|ships?|tornadoes|states?|days?|times)\b", t, re.I)
        if m:
            n = int(m.group(1))
            return (n, n)
    return None


def in_bracket(n: int, br: tuple[int, int | None]) -> bool:
    lo, hi = br
    return n >= lo and (hi is None or n <= hi)


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

_TIME_COLS = ("time", "timestamp", "created_at", "createdat", "datetime", "date", "utc", "origin_time", "ts")


def _parse_ts(text: str) -> float | None:
    t = (text or "").strip()
    if not t:
        return None
    if re.fullmatch(r"\d{9,13}(\.\d+)?", t):
        v = float(t)
        return v / 1000.0 if v > 1e11 else v
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00").replace(" UTC", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y"):
            try:
                dt = datetime.strptime(t, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


@dataclass
class Catalog:
    """Timestamped occurrences (unix seconds, sorted) with weights (1 per row, or a count column)."""

    times: list[float]
    weights: list[float]
    name: str = ""

    def __post_init__(self) -> None:
        order = sorted(range(len(self.times)), key=lambda i: self.times[i])
        self.times = [self.times[i] for i in order]
        self.weights = [self.weights[i] for i in order]
        self._cum = [0.0]
        for w in self.weights:
            self._cum.append(self._cum[-1] + w)

    def count(self, a: float, b: float) -> float:
        """Weighted count of occurrences with a <= time < b."""
        i = bisect.bisect_left(self.times, a)
        j = bisect.bisect_left(self.times, b)
        return self._cum[j] - self._cum[i]

    def between(self, a: float, b: float) -> list[tuple[float, float]]:
        i = bisect.bisect_left(self.times, a)
        j = bisect.bisect_left(self.times, b)
        return list(zip(self.times[i:j], self.weights[i:j]))

    @property
    def first(self) -> float | None:
        return self.times[0] if self.times else None

    @property
    def last(self) -> float | None:
        return self.times[-1] if self.times else None


def load_catalog(path: Path, *, time_col: str | None = None, value_col: str | None = None, min_value: float | None = None, count_col: str | None = None, tz: str | None = None) -> tuple[Catalog, list[str]]:
    """Read a CSV of occurrences. `value_col`/`min_value` keep rows at or above a threshold
    (magnitude 6.5); `count_col` reads pre-aggregated rows (one per day with a count). Naive
    timestamps are UTC unless `tz` names a zone."""
    problems: list[str] = []
    times: list[float] = []
    weights: list[float] = []
    zone = ZoneInfo(tz) if tz else None
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or []}
        tcol = cols.get((time_col or "").lower()) if time_col else next((cols[c] for c in _TIME_COLS if c in cols), None)
        if tcol is None:
            return Catalog([], [], Path(path).name), [f"no time column (have: {', '.join(cols) or 'none'}); pass --time-col"]
        vcol = cols.get(value_col.lower()) if value_col else None
        if value_col and vcol is None:
            return Catalog([], [], Path(path).name), [f"value column `{value_col}` not found"]
        ccol = cols.get(count_col.lower()) if count_col else None
        if count_col and ccol is None:
            return Catalog([], [], Path(path).name), [f"count column `{count_col}` not found"]
        for i, rec in enumerate(reader, start=2):
            ts_text = rec.get(tcol) or ""
            if zone and ts_text and not re.search(r"[zZ]$|[+-]\d{2}:?\d{2}$", ts_text.strip()) and not re.fullmatch(r"\d{9,13}(\.\d+)?", ts_text.strip()):
                try:
                    naive = datetime.fromisoformat(ts_text.strip())
                    ts = naive.replace(tzinfo=zone).timestamp() if naive.tzinfo is None else naive.timestamp()
                except ValueError:
                    ts = _parse_ts(ts_text)
            else:
                ts = _parse_ts(ts_text)
            if ts is None:
                problems.append(f"line {i}: unreadable time `{ts_text}`")
                continue
            if vcol is not None and min_value is not None:
                try:
                    if float(rec.get(vcol) or "nan") < min_value:
                        continue
                except ValueError:
                    problems.append(f"line {i}: value `{rec.get(vcol)}` is not a number")
                    continue
            w = 1.0
            if ccol is not None:
                try:
                    w = float(rec.get(ccol) or 0)
                except ValueError:
                    problems.append(f"line {i}: count `{rec.get(ccol)}` is not a number")
                    continue
            times.append(ts)
            weights.append(w)
    return Catalog(times, weights, Path(path).name), problems[:50]


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

def _poisson_pmf(k: int, mu: float) -> float:
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(mu) - mu - math.lgamma(k + 1))


def _nb_pmf(k: int, mu: float, r: float) -> float:
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    p = r / (r + mu)
    return math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) + r * math.log(p) + k * math.log(1.0 - p))


def remainder_pmf(k: int, mu: float, r: float | None) -> float:
    return _poisson_pmf(k, mu) if r is None else _nb_pmf(k, mu, r)


def p_bracket(n_so_far: float, mu_rem: float, r: float | None, bracket: tuple[int, int | None], max_terms: int = 5000) -> float:
    """P(low <= n_so_far + R <= high) with R ~ Poisson(mu_rem) or NB(mu_rem, r)."""
    lo, hi = bracket
    n0 = int(round(n_so_far))
    k_lo = max(0, lo - n0)
    if hi is not None and hi - n0 < 0:
        return 0.0
    if hi is None:
        # 1 - P(R <= k_lo - 1)
        return max(0.0, 1.0 - sum(remainder_pmf(k, mu_rem, r) for k in range(0, k_lo)))
    k_hi = min(hi - n0, max_terms)
    return max(0.0, min(1.0, sum(remainder_pmf(k, mu_rem, r) for k in range(k_lo, k_hi + 1))))


@dataclass
class RateModel:
    mu: float  # expected count over a full window
    r: float | None  # negative-binomial size; None = Poisson
    k_windows: int
    window_counts: list[float]
    phases: list[float]  # occurrence times as a fraction of the window, from the reference windows (sorted)
    profile: bool

    def remaining_fraction(self, frac_elapsed: float) -> float:
        """Share of a window's expected count that falls after `frac_elapsed` of it."""
        frac_elapsed = min(max(frac_elapsed, 0.0), 1.0)
        if not self.profile or not self.phases:
            return 1.0 - frac_elapsed
        i = bisect.bisect_left(self.phases, frac_elapsed)
        return (len(self.phases) - i) / len(self.phases)


def fit_rate(catalog: Catalog, a: datetime, b: datetime, *, k_windows: int = 8, profile: bool = True) -> RateModel | None:
    """Mean and dispersion of the count over the `k_windows` windows of the same length that end
    at `a` (so nothing after the window start is used)."""
    length = (b - a).total_seconds()
    if length <= 0 or k_windows < 1:
        return None
    a_s = a.timestamp()
    if catalog.first is None or catalog.first > a_s - length:
        return None
    k = min(k_windows, int((a_s - catalog.first) // length))
    if k < 1:
        return None
    counts: list[float] = []
    phases: list[float] = []
    for i in range(k):
        w1 = a_s - (i + 1) * length
        w2 = a_s - i * length
        counts.append(catalog.count(w1, w2))
        for t, w in catalog.between(w1, w2):
            phases.extend([(t - w1) / length] * int(max(1, round(w))))
    phases.sort()
    mu = sum(counts) / len(counts)
    var = statistics.pvariance(counts) if len(counts) > 1 else mu
    r = (mu * mu / (var - mu)) if (var > mu and mu > 0) else None
    return RateModel(mu, r, len(counts), counts, phases, profile)


@dataclass
class CountMarket:
    condition_id: str
    question: str
    label: str
    bracket: tuple[int, int | None]
    window: tuple[datetime, datetime]
    event_title: str
    resolved_yes: bool | None
    end_date: datetime | None


def count_markets(events: list[dict[str, Any]], *, monthly: bool = False, log: Callable[[str], None] | None = None) -> list[CountMarket]:
    """Every market of the events whose window and bracket can be read."""
    out: list[CountMarket] = []
    for raw in events:
        ev: PolyEvent = parse_event(raw)
        for mk in ev.markets:
            text = (mk.raw.get("description") or raw.get("description") or "")
            w = parse_window(text, end_hint=mk.end_date or ev.end_date)
            if w is None and monthly:
                w = parse_month_window(mk.question or ev.title, end_hint=mk.end_date or ev.end_date) or parse_month_window(ev.title, end_hint=mk.end_date or ev.end_date)
            if w is None:
                if log:
                    log(f"no window: {mk.question[:70]}")
                continue
            br = parse_bracket(mk.group_item_title, mk.question)
            if br is None:
                if log:
                    log(f"no bracket: {mk.group_item_title!r} / {mk.question[:70]}")
                continue
            out.append(CountMarket(mk.condition_id, mk.question, mk.group_item_title or mk.question, br, w, ev.title, mk.resolved_yes, mk.end_date))
    return out


def decision_times(a: datetime, b: datetime, *, hours: tuple[int, ...] = (12,), pre_days: int = 1) -> list[datetime]:
    """Daily decision times (UTC hours) from `pre_days` before the window start to its end."""
    out: list[datetime] = []
    day = (a - timedelta(days=pre_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    while day < b:
        for h in hours:
            t = day + timedelta(hours=h)
            if a - timedelta(days=pre_days) <= t < b:
                out.append(t)
        day += timedelta(days=1)
    return out


@dataclass
class SignalOut:
    market: str
    time: str
    p: float
    note: str


def build_signal_rows(markets: list[CountMarket], catalog: Catalog, *, k_windows: int = 8, profile: bool = True, hours: tuple[int, ...] = (12,), pre_days: int = 1, log: Callable[[str], None] | None = None) -> tuple[list[SignalOut], dict[str, int]]:
    """One probability per (market, decision time). The catalog must cover the window: rows
    whose decision time is after the catalog's last entry are skipped (the count would be
    wrong, not merely stale)."""
    rows: list[SignalOut] = []
    skipped: dict[str, int] = defaultdict(int)
    by_window: dict[tuple[datetime, datetime], list[CountMarket]] = defaultdict(list)
    for m in markets:
        by_window[m.window].append(m)
    last = catalog.last or 0.0
    for (a, b), group in by_window.items():
        model = fit_rate(catalog, a, b, k_windows=k_windows, profile=profile)
        if model is None:
            skipped["no reference windows in the catalog"] += len(group)
            continue
        length = (b - a).total_seconds()
        for t in decision_times(a, b, hours=hours, pre_days=pre_days):
            if t.timestamp() > last:
                skipped["catalog ends before the decision time"] += len(group)
                continue
            n = catalog.count(a.timestamp(), min(t, b).timestamp()) if t > a else 0.0
            frac = max(0.0, (t - a).total_seconds() / length)
            mu_rem = model.mu * model.remaining_fraction(frac)
            note = f"n={n:g} mu_rem={mu_rem:.1f} mu={model.mu:.1f} r={'poisson' if model.r is None else f'{model.r:.1f}'} k={model.k_windows}"
            for m in group:
                rows.append(SignalOut(m.condition_id, t.isoformat(timespec="minutes"), round(p_bracket(n, mu_rem, model.r, m.bracket), 4), note))
    if log:
        log(f"{len(rows)} rows for {len(markets)} markets; skipped {dict(skipped)}")
    return rows, dict(skipped)


def write_signal_csv(rows: list[SignalOut], path: Path) -> None:
    with Path(path).open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["market", "time", "p", "note"])
        for r in rows:
            w.writerow([r.market, r.time, r.p, r.note])


def check_catalog(markets: list[CountMarket], catalog: Catalog) -> list[dict[str, Any]]:
    """For resolved windows the catalog covers: does the catalog's count land in the winning
    bracket? The first thing to look at: if it does not, the catalog is not what the tracker
    counts (replies, reposts, magnitude revisions, time zone)."""
    out: list[dict[str, Any]] = []
    last = catalog.last or 0.0
    by_window: dict[tuple[datetime, datetime], list[CountMarket]] = defaultdict(list)
    for m in markets:
        by_window[m.window].append(m)
    for (a, b), group in sorted(by_window.items()):
        winners = [m for m in group if m.resolved_yes]
        if len(winners) != 1 or b.timestamp() > last:
            continue
        n = catalog.count(a.timestamp(), b.timestamp())
        out.append({"window": f"{a:%Y-%m-%d %H:%M} .. {b:%Y-%m-%d %H:%M}", "count": n, "winner": winners[0].label, "bracket": winners[0].bracket, "match": in_bracket(int(round(n)), winners[0].bracket)})
    return out


# --------------------------------------------------------------------------- #
# Headroom: how sharp is the market already, with no outside data
# --------------------------------------------------------------------------- #

OFFSETS: tuple[tuple[str, float], ...] = (("start", 0.0), ("mid", 0.5), ("late", 0.9))


@dataclass
class HeadroomRow:
    family: str
    event: str
    market: str
    label: str
    offset: str
    time: int
    price: float
    y: int
    clim: float | None  # point-in-time frequency of this label winning in the series
    uniform: float


@dataclass
class HeadroomFamily:
    family: str
    events: int
    markets: int
    windows_parsed: int
    rows: int
    per_offset: dict[str, dict[str, float | None]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _market_window(mk, series: list[tuple[int, float]], text: str, event_end: datetime | None, monthly: bool) -> tuple[datetime, datetime] | None:
    w = parse_window(text, end_hint=mk.end_date or event_end)
    if w is None and monthly:
        w = parse_month_window(mk.question, end_hint=mk.end_date or event_end)
    if w is None and len(series) >= 2:
        w = (datetime.fromtimestamp(series[0][0], tz=UTC), datetime.fromtimestamp(series[-1][0], tz=UTC))
    return w


def headroom(family: str, events: list[dict[str, Any]], trades_source, *, per_family: int = 12, seed: int = 7, monthly: bool = False, offsets: tuple[tuple[str, float], ...] = OFFSETS, log: Callable[[str], None] | None = None) -> tuple[HeadroomFamily, list[HeadroomRow]]:
    """Score the market's own price at fixed fractions of each market's window against the
    outcome, with a uniform 1/k and a point-in-time label climatology as references."""
    parsed = [parse_event(e) for e in events]
    resolved_events = [e for e in parsed if any(m.resolved_yes is not None for m in e.markets)]
    resolved_events.sort(key=lambda e: (e.end_date or datetime.max.replace(tzinfo=UTC)))
    # climatology: label -> list of (event end ts, won?) over every resolved event, used point-in-time
    hist: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for e in resolved_events:
        end_ts = (e.end_date or datetime.max.replace(tzinfo=UTC)).timestamp()
        for m in e.markets:
            if m.resolved_yes is not None:
                hist[(m.group_item_title or m.question).strip().lower()].append((end_ts, int(m.resolved_yes)))
    for v in hist.values():
        v.sort()
    rng = random.Random(seed)
    sample = resolved_events if len(resolved_events) <= per_family else rng.sample(resolved_events, per_family)
    rows: list[HeadroomRow] = []
    windows = 0
    for e in sample:
        mks = [m for m in e.markets if m.resolved_yes is not None]
        k = max(1, len(mks))
        for m in mks:
            trades = trades_source.trades(m.condition_id or m.id, closed=True)
            series = yes_price_series(trades, m.outcomes)
            if len(series) < 2:
                continue
            text = m.raw.get("description") or ""
            w = _market_window(m, series, text, e.end_date, monthly)
            if w is None:
                continue
            if parse_window(text, end_hint=m.end_date or e.end_date) is not None:
                windows += 1
            a, b = w
            length = (b - a).total_seconds()
            if length <= 0:
                continue
            label = (m.group_item_title or m.question).strip().lower()
            for name, frac in offsets:
                t = int(a.timestamp() + frac * length)
                t = min(t, series[-1][0])
                price = price_at(series, t)
                if price is None:
                    continue
                past = [won for end_ts, won in hist.get(label, []) if end_ts < t]
                clim = (sum(past) / len(past)) if len(past) >= 5 else None
                rows.append(HeadroomRow(family, e.title, m.condition_id, label, name, t, price, int(m.resolved_yes), clim, 1.0 / k))
        if log:
            log(f"{family}: {e.title[:60]} -> {len(rows)} rows so far")
    per_offset: dict[str, dict[str, float | None]] = {}
    for name, _ in offsets:
        rs = [r for r in rows if r.offset == name]
        if not rs:
            continue
        per_event: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
        for r in rs:
            s, c = per_event[r.event]
            per_event[r.event] = (s + (r.price - r.y) ** 2, c + 1)
        brier = sum(s for s, _ in per_event.values()) / sum(c for _, c in per_event.values())
        clim_rows = [r for r in rs if r.clim is not None]
        gap_clim = blend_gap = None
        if clim_rows:
            gaps: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
            blends: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
            for r in clim_rows:
                s, c = gaps[r.event]
                gaps[r.event] = (s + ((r.price - r.y) ** 2 - (r.clim - r.y) ** 2), c + 1)
                s, c = blends[r.event]
                blends[r.event] = (s + ((r.price - r.y) ** 2 - (0.5 * (r.price + r.clim) - r.y) ** 2), c + 1)
            gap_clim = sum(s for s, _ in gaps.values()) / len(clim_rows)
            blend_gap = sum(s for s, _ in blends.values()) / len(clim_rows)
            gap_se, blend_se = cluster_se(gaps), cluster_se(blends)
        else:
            gap_se = blend_se = None
        per_offset[name] = {
            "rows": float(len(rs)),
            "brier_market": brier,
            "brier_uniform": statistics.mean((r.uniform - r.y) ** 2 for r in rs),
            "brier_clim": statistics.mean((r.clim - r.y) ** 2 for r in clim_rows) if clim_rows else None,
            "clim_rows": float(len(clim_rows)),
            "market_minus_clim": gap_clim,  # positive = climatology is sharper than the market
            "market_minus_clim_se": gap_se,
            "market_minus_blend": blend_gap,  # positive = the blend beats the market: base rates carry information the price lacks
            "market_minus_blend_se": blend_se,
            "mean_abs_move_to_outcome": statistics.mean(abs(r.price - r.y) for r in rs),
        }
    fam = HeadroomFamily(family, len(sample), sum(1 for e in sample for m in e.markets if m.resolved_yes is not None), windows, len(rows), per_offset)
    return fam, rows


def _f(x: float | None, fmt: str = ".3f") -> str:
    return "   -  " if x is None else format(x, fmt)


def render_headroom(fams: list[HeadroomFamily]) -> str:
    L = ["Headroom: the market's own Brier at fixed points of each window, next to a uniform 1/k and a point-in-time label climatology."]
    L.append("  positive `clim gap` = the base rate alone was sharper than the price; positive `blend gap` = mixing the base rate into the price would have helped.")
    L.append("")
    L.append(f"{'family':36s} {'ev':>3s} {'mk':>4s} {'off':5s} {'rows':>5s} {'market':>7s} {'unif':>6s} {'clim':>6s} {'clim gap':>14s} {'blend gap':>14s}")
    for f in fams:
        for i, (name, d) in enumerate(f.per_offset.items()):
            head = f"{f.family[:36]:36s} {f.events:3d} {f.markets:4d}" if i == 0 else " " * 45
            gap = f"{_f(d['market_minus_clim'], '+.3f')} ± {_f(d['market_minus_clim_se'])}" if d["market_minus_clim"] is not None else "       -      "
            bl = f"{_f(d['market_minus_blend'], '+.3f')} ± {_f(d['market_minus_blend_se'])}" if d["market_minus_blend"] is not None else "       -      "
            L.append(f"{head} {name:5s} {int(d['rows']):5d} {_f(d['brier_market']):>7s} {_f(d['brier_uniform']):>6s} {_f(d['brier_clim']):>6s} {gap:>14s} {bl:>14s}")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Gamma series
# --------------------------------------------------------------------------- #

def fetch_series_events(http, slug: str, *, gamma_url: str = "https://gamma-api.polymarket.com", page_size: int = 100) -> list[dict[str, Any]]:
    """Every event (closed and open) of a Gamma recurring series, as raw dicts."""
    ser = http.get_json(f"{gamma_url}/series", params={"slug": slug})
    ser = ser if isinstance(ser, list) else [ser]
    if not ser or not isinstance(ser[0], dict) or not ser[0].get("id"):
        return []
    sid = ser[0]["id"]
    out: list[dict[str, Any]] = []
    for closed in ("true", "false"):
        offset = 0
        while True:
            page = http.get_json(f"{gamma_url}/events", params={"series_id": sid, "closed": closed, "limit": page_size, "offset": offset})
            if not page or not isinstance(page, list):
                break
            out.extend(d for d in page if isinstance(d, dict))
            offset += page_size
            if len(page) < page_size:
                break
    return out
