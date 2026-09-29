"""Niche survey: which Polymarket market families recur, how deep they are, what they cost
in fees, and how well their prices were calibrated a day before resolution.

The point is to find *steady* families (a new event every day or week, resolved on a
public number) where a research process could beat the crowd, as opposed to one-off
events. Everything here is read-only and works from Gamma event JSON, so it can run
against recorded fixtures.

Family key: Gamma's `seriesSlug` when the event belongs to a recurring series (e.g.
`nyc-daily-weather`, `elon-tweets`, `box-office-openings`), else the title with dates,
numbers and weekdays normalised so "Highest temperature in X on May 2?" and "... on
May 3?" fall together.

Calibration one day before close uses the frozen Gamma fields (see
`PolyMarket.price_day_before_close`), so no per-market history calls are needed.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from .fees import polymarket_rate_for_event
from .polymarket import PolyEvent

_MONTHS = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
_SPORT_TAGS = {"sports", "esports", "games"}

# Default tag list for a survey: the non-sport categories with recurring families.
DEFAULT_SURVEY_TAGS = (
    "weather", "climate", "pop-culture", "movies", "music", "tech", "ai", "science",
    "economy", "economics", "finance", "stocks", "mention-markets", "politics", "geopolitics", "world",
)
UP_OR_DOWN_TAG_ID = "102127"  # the 5-minute / hourly crypto "Up or Down" series: thousands a day, all bots


def normalise_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\$[\d,\.]+[kmb]?", "$N", t)
    t = re.sub(_MONTHS + r"\.?\s+\d{1,2}(st|nd|rd|th)?(,?\s*\d{4})?", "DATE", t)
    t = re.sub(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", "TIME", t)
    t = re.sub(r"\b(19|20)\d{2}\b", "YEAR", t)
    t = re.sub(r"\bq[1-4]\b", "QTR", t)
    t = re.sub(r"\b\d+(\.\d+)?%?", "N", t)
    t = re.sub(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", "DAY", t)
    return re.sub(r"\s+", " ", t).strip(" ?.!")


def family_key(event: PolyEvent) -> str:
    return event.series_slug or ("~" + normalise_title(event.title))


def is_sport(event: PolyEvent) -> bool:
    return bool(_SPORT_TAGS & set(event.tags)) or any((m.raw.get("feeType") or "").startswith("sports") for m in event.markets)


def _domain(url: str) -> str:
    if "//" not in url:
        return url[:40]
    return urlparse(url).netloc.replace("www.", "")


@dataclass
class Calibration:
    """Prices one day before close vs. outcomes, over resolved binary markets that were
    still live then (0.01 < p < 0.99 and some volume)."""

    n: int = 0
    base_rate: float | None = None
    brier: float | None = None
    skill: float | None = None  # 1 - Brier / Brier(climatology); 1 = perfect, 0 = knows nothing
    longshot_bias: float | None = None  # hit rate minus price for 0.03 <= p < 0.20 (positive: longshots underpriced)
    mid_bias: float | None = None  # same for 0.30 <= p < 0.70
    buckets: list[tuple[float, int, float, float]] = field(default_factory=list)  # (bucket lo, n, avg price, hit rate)


def calibrate(pairs: list[tuple[float, int]]) -> Calibration:
    if len(pairs) < 20:
        return Calibration(n=len(pairs))
    base = statistics.mean(y for _, y in pairs)
    brier = statistics.mean((p - y) ** 2 for p, y in pairs)
    clim = statistics.mean((base - y) ** 2 for _, y in pairs)
    ls = [(p, y) for p, y in pairs if 0.03 <= p < 0.20]
    mid = [(p, y) for p, y in pairs if 0.30 <= p < 0.70]
    by_bucket: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for p, y in pairs:
        by_bucket[min(int(p * 10), 9)].append((p, y))
    buckets = [
        (b / 10, len(v), statistics.mean(p for p, _ in v), statistics.mean(y for _, y in v))
        for b, v in sorted(by_bucket.items())
    ]
    return Calibration(
        n=len(pairs),
        base_rate=base,
        brier=brier,
        skill=(1 - brier / clim) if clim > 0 else None,
        longshot_bias=(statistics.mean(y for _, y in ls) - statistics.mean(p for p, _ in ls)) if len(ls) >= 20 else None,
        mid_bias=(statistics.mean(y for _, y in mid) - statistics.mean(p for p, _ in mid)) if len(mid) >= 20 else None,
        buckets=buckets,
    )


@dataclass
class FamilyStats:
    key: str
    example: str
    recurrence: str
    sport: bool
    n_events: int
    n_open: int
    n_markets: int
    volume: float  # lifetime $ volume of the events seen
    open_liquidity: float  # Gamma liquidity of the open events
    events_per_month: float  # over the survey window
    months_active: int  # distinct months (of the window) with a new event
    median_lifetime_days: float | None  # created -> end
    median_spread: float | None  # Gamma spread on open markets with liquidity
    fee_rate: float
    resolution_sources: list[str]
    tags: list[str]
    calibration: Calibration

    @property
    def steadiness(self) -> float:
        """0..1: share of the window's months with a new event, times min(1, events/month / 4).
        A daily series scores 1; a monthly one about 0.25; a one-off near 0."""
        if self.months_active <= 0:
            return 0.0
        return min(1.0, self.events_per_month / 4.0) * self._month_share

    _month_share: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_month_share", None)
        d["steadiness"] = round(self.steadiness, 3)
        return d


def survey(events: list[PolyEvent], *, now: datetime, since: datetime, min_events: int = 6) -> list[FamilyStats]:
    """Group events into families and compute cadence, depth, fee and calibration per family."""
    months_in_window = max(1, (now.year - since.year) * 12 + now.month - since.month + 1)
    fams: dict[str, list[PolyEvent]] = defaultdict(list)
    for ev in events:
        fams[family_key(ev)].append(ev)
    out: list[FamilyStats] = []
    for key, evs in fams.items():
        if len(evs) < min_events:
            continue
        in_window = [e for e in evs if e.created_at and e.created_at >= since]
        months = {(e.created_at.year, e.created_at.month) for e in in_window if e.created_at}
        opens = [e for e in evs if not e.closed]
        lifetimes = [(e.end_date - e.created_at).total_seconds() / 86400 for e in evs if e.end_date and e.created_at]
        spreads = [m.spread for e in opens for m in e.markets if m.spread is not None and m.liquidity > 0]
        pairs: list[tuple[float, int]] = []
        for e in evs:
            if not e.closed:
                continue
            for m in e.markets:
                y = m.resolved_yes
                p = m.price_day_before_close
                if y is None or p is None or not (0.01 < p < 0.99) or m.raw.get("volumeNum", 1) in (0, None):
                    continue
                pairs.append((p, int(y)))
        sources = Counter(_domain(e.resolution_source or next((m.raw.get("resolutionSource") or "" for m in e.markets), "")) for e in evs)
        tags = Counter(t for e in evs for t in e.tags)
        rec = Counter(e.recurrence for e in evs if e.recurrence)
        fs = FamilyStats(
            key=key,
            example=evs[-1].title,
            recurrence=rec.most_common(1)[0][0] if rec else "",
            sport=any(is_sport(e) for e in evs[:5]),
            n_events=len(evs),
            n_open=len(opens),
            n_markets=sum(len(e.markets) for e in evs),
            volume=sum(e.volume for e in evs),
            open_liquidity=sum(e.liquidity for e in opens),
            events_per_month=len(in_window) / months_in_window,
            months_active=len(months),
            median_lifetime_days=statistics.median(lifetimes) if lifetimes else None,
            median_spread=statistics.median(spreads) if spreads else None,
            fee_rate=max(polymarket_rate_for_event(e) for e in evs[:20]),
            resolution_sources=[s for s, _ in sources.most_common(2) if s],
            tags=[t for t, _ in tags.most_common(5)],
            calibration=calibrate(pairs),
        )
        fs._month_share = len(months) / months_in_window
        out.append(fs)
    out.sort(key=lambda f: -f.volume)
    return out


def default_since(now: datetime, days: int = 90) -> datetime:
    return (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)


def render_survey(rows: list[FamilyStats], *, now: datetime, since: datetime, top: int = 60, include_sport: bool = False, sort: str = "volume") -> str:
    rows = [r for r in rows if include_sport or not r.sport]
    if sort == "steady":
        rows.sort(key=lambda r: (-r.steadiness, -r.volume))
    elif sort == "volume":
        rows.sort(key=lambda r: -r.volume)
    elif sort == "liquidity":
        rows.sort(key=lambda r: -r.open_liquidity)
    L = [
        f"Polymarket niche survey  window {since:%Y-%m-%d} .. {now:%Y-%m-%d}   families={len(rows)}  (showing {min(top, len(rows))}, sorted by {sort})",
        "  steady = months with a new event x min(1, events/month / 4); skill = Brier skill of the price one day before close",
        "  LS/mid bias = hit rate minus price for 3-20c / 30-70c contracts a day before close: negative means those contracts were overpriced",
        "",
        f"{'family':36} {'rec':7} {'ev':>4} {'open':>4} {'mkts':>5} {'vol$M':>7} {'liq$K':>6} {'ev/mo':>5} {'steady':>6} {'life':>5} {'spr':>5} {'fee':>4} {'n_cal':>5} {'skill':>5} {'LSbias':>6} {'midbias':>7}  source / example",
    ]
    for r in rows[:top]:
        c = r.calibration
        fmt = lambda v, w, s: (f"{v:{s}}" if v is not None else "-").rjust(w)  # noqa: E731
        L.append(
            f"{r.key[:36]:36} {r.recurrence[:7]:7} {r.n_events:4} {r.n_open:4} {r.n_markets:5} {r.volume / 1e6:7.2f} {r.open_liquidity / 1e3:6.0f} "
            f"{r.events_per_month:5.1f} {r.steadiness:6.2f} {fmt(r.median_lifetime_days, 5, '.1f')} {fmt(r.median_spread, 5, '.3f')} {r.fee_rate:4.2f} "
            f"{c.n:5} {fmt(c.skill, 5, '.2f')} {fmt(c.longshot_bias, 6, '+.3f')} {fmt(c.mid_bias, 7, '+.3f')}  "
            f"{(r.resolution_sources[0] if r.resolution_sources else '')[:22]} | {r.example[:48]}"
        )
    return "\n".join(L)


def load_survey_tags(spec: str | None) -> tuple[str, ...]:
    if not spec:
        return DEFAULT_SURVEY_TAGS
    return tuple(t.strip() for t in spec.split(",") if t.strip())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
