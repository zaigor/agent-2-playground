"""Ladder consistency: nested outcomes must be priced monotonically.

Markets that differ only by a number or a date form a ladder, and each rung is a
subset of the rung before it:

  "Bitcoin above 74,000 / 76,000 / 78,000 on October 1?"   P falls as the threshold rises
  "Gemini 4.0 released by Sep 30 / Oct 15 / Oct 31?"          P rises with the date
  "Ceasefire continues through Oct 31 / Nov 30?"              P falls with the date
  "Team total O/U 0.5 / 1.5 / 2.5" (outcomes Over/Under)      P(Over) falls with the line
  "Chelsea (-1.5) / Chelsea (-2.5)"                           P(cover) falls with the handicap

Ladders are found inside one event (same title stem, one number or date differing) and
across events for "... by <date>?" questions with the same stem. Exact-day markets
("released on October 2") are brackets, not ladders, and are left alone; a negated
question ("no release by ...", "not IPO by ...") flips the direction.

For a pair A ⊂ B (A is the harder outcome) the prices must satisfy P(A) <= P(B). They break
it when bid(A) > ask(B): buy YES on B at its ask and NO on A at 1 - bid(A). The pair pays at
least $1 whatever happens (B-and-not-A pays $2) for a cost of ask(B) + 1 - bid(A) < 1.
Both legs are taker orders, so the edge is quoted net of the taker fee on each.

Everything here is read-only: Gamma's top of book finds candidates, CLOB books confirm them.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from .fees import polymarket_rate_for_event, polymarket_taker_fee
from .polymarket import Book, PolyEvent, PolyMarket

MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

_MONTH_DAY = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?"
_DATE_RE = re.compile(rf"\b{_MONTH_DAY}\b", re.I)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_PREP = r"(by|before|through|until|on or prior to|on or before|prior to|as of)"
_PREP_DATE_RE = re.compile(rf"\b{_PREP}\s+(?:the\s+)?(?:end\s+of\s+)?(?:{_MONTH_DAY}|(20\d{{2}}))\b\??", re.I)
_NUM_RE = re.compile(r"(?<![\w.])[-+]?\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?[%kKmM]?(?![\w,])")
_SPREAD_RE = re.compile(r"\(([-+])\s?(\d+(?:\.\d+)?)\)")
_OU_RE = re.compile(r"\bO/U\s+(\d+(?:\.\d+)?)", re.I)
_NEG_RE = re.compile(r"\b(not|no|never)\b(?!\s+longer)", re.I)  # "no longer under control by ..." is not a negation

# P falls as the number rises ("above 80k" is rarer than "above 70k")
_DECREASING = ("↑", "above", " over ", "more than", "at least", "or more", "or higher", "higher than", "exceed", "reach", " hit ", " hits ", "surpass", "greater", "how high", "highest", "+ ")
# P rises with the number ("dip to 60k" is rarer than "dip to 70k")
_INCREASING = ("↓", "below", " under ", "less than", "at most", "or fewer", "or less", "or lower", "lower than", "fewer than", "dip", "drop", "fall", "how low", "lowest")
_DATE_DOWN = ("through", "until")  # "continues through Oct 31" is a subset of "through Sep 30"


@dataclass
class Rung:
    market: PolyMarket
    event: PolyEvent
    key: float  # threshold (number, or date ordinal)
    label: str

    @property
    def mid(self) -> float | None:
        if self.market.best_bid is None or self.market.best_ask is None:
            return None
        return (self.market.best_bid + self.market.best_ask) / 2.0

    @property
    def quoted(self) -> bool:
        """A live two-sided quote, not a 1c/99c placeholder book."""
        b, a = self.market.best_bid, self.market.best_ask
        return b is not None and a is not None and a - b <= 0.15 and not (b <= 0.01 and a >= 0.99)


@dataclass
class Ladder:
    id: str
    title: str
    kind: str  # number | date | spread | ou
    direction: int  # +1: P rises with key; -1: P falls with key
    rungs: list[Rung]  # sorted by key
    cross_event: bool = False

    def ordered(self) -> list[Rung]:
        """Rungs from the hardest (smallest probability) to the easiest outcome."""
        return list(self.rungs if self.direction > 0 else reversed(self.rungs))

    @property
    def url(self) -> str:
        return self.rungs[0].event.url if self.rungs else ""


@dataclass
class Violation:
    ladder: str
    kind: str  # hard (bid of the subset above the ask of the superset) | soft (mids inverted)
    subset: str
    superset: str
    subset_bid: float
    superset_ask: float
    gap: float  # subset_bid - superset_ask, before fees
    fee_per_set: float
    edge_per_set: float  # net of both taker fees
    fillable_sets: float | None = None
    budget_sets: float = 0.0
    est_profit: float = 0.0
    url: str = ""
    subset_id: str = ""
    superset_id: str = ""
    subset_question: str = ""
    superset_question: str = ""
    days_to_resolve: float | None = None  # until the later of the two markets ends: the capital is locked that long
    annualized: float | None = None  # net edge / cost, scaled to a year

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def set_horizon(self, sub: "Rung", sup: "Rung", now: datetime) -> None:
        ends = [d for d in (sub.market.end_date, sup.market.end_date) if d]
        if not ends:
            return
        days = max(0.0, (max(ends) - now).total_seconds() / 86400.0)
        self.days_to_resolve = round(days, 1)
        cost = self.superset_ask + (1.0 - self.subset_bid) + self.fee_per_set
        if cost > 0:
            self.annualized = round(self.edge_per_set / cost * 365.0 / max(days, 1.0), 4)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _closest_year(month: int, day: int, anchor: date | None) -> int | None:
    if anchor is None:
        return datetime.utcnow().year
    best = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        dist = abs(d.toordinal() - anchor.toordinal())
        if best is None or dist < best[0]:
            best = (dist, year)
    return best[1] if best else None


def parse_date_key(text: str, anchor: date | None) -> tuple[float, str] | None:
    """A date in `text` as a day ordinal plus the matched phrase. A missing year is the one
    that puts the date closest to `anchor` (the market's own end date), so "by March 31" on a
    market ending in April 2027 is March 2027 and "through December 31" on one ending on
    New Year's Day is the December just before."""
    m = _DATE_RE.search(text)
    if m:
        month, day = MONTHS[m.group(1)[:3].lower()], int(m.group(2))
        year = int(m.group(3)) if m.group(3) else _closest_year(month, day, anchor)
        if year is None:
            return None
        try:
            return float(date(year, month, day).toordinal()), m.group(0)
        except ValueError:
            return None
    y = _YEAR_RE.search(text)
    if y:
        return float(date(int(y.group(1)), 12, 31).toordinal()), y.group(0)
    return None


def parse_number_key(text: str) -> tuple[float, str] | None:
    """The single number in `text` (thousands separators, $, % and k/m suffixes allowed).
    None when there are zero or several numbers, so date-like titles never become thresholds."""
    nums = list(_NUM_RE.finditer(_DATE_RE.sub(" ", text)))
    if len(nums) != 1:
        return None
    raw = nums[0].group(0)
    body = raw.replace("$", "").replace(",", "").replace("%", "")
    mult = 1.0
    if body[-1:] in ("k", "K"):
        mult, body = 1e3, body[:-1]
    elif body[-1:] in ("m", "M"):
        mult, body = 1e6, body[:-1]
    try:
        return float(body) * mult, raw
    except ValueError:
        return None


def _number_direction(*texts: str) -> int | None:
    """First text that carries a cue wins: title arrows, then the event title ("How low will
    ... go?"), then the question ("hit 35%" alone would read as a ceiling)."""
    for text in texts:
        low = " " + text.lower() + " "
        inc = any(w in low for w in _INCREASING)
        dec = any(w in low for w in _DECREASING)
        if inc and not dec:
            return +1
        if dec and not inc:
            return -1
        if inc and dec:
            return +1 if any(w in low for w in ("how low", "lowest", "↓", "dip", "drop", "fall")) else -1
    return None


def _prep_direction(prep: str) -> int:
    return -1 if prep.lower() in _DATE_DOWN else +1


def _negated(question: str) -> bool:
    return bool(_NEG_RE.search(question))


def _title_date(title: str, anchor: date | None) -> tuple[float, int | None, str] | None:
    """A title that is just a date, optionally led by a preposition: (key, direction-from-
    preposition or None, label)."""
    t = title.strip().rstrip("?").strip()
    prep = None
    m = re.match(rf"^{_PREP}\s+(?:the\s+)?(?:end\s+of\s+)?(.*)$", t, re.I)
    body = t
    if m:
        prep, body = m.group(1), m.group(2)
    dk = parse_date_key(body, anchor)
    if dk is None:
        return None
    rest = body.replace(dk[1], "").strip(" ,.?")
    if rest:
        return None
    return dk[0], (_prep_direction(prep) if prep else None), title.strip()


def classify(market: PolyMarket, event: PolyEvent) -> tuple[str, str, float, int, str] | None:
    """(kind, stem, key, direction, label) for a market that can sit on a ladder, else None."""
    title = market.group_item_title.strip()
    question = market.question.strip()
    outcomes = tuple(o.lower() for o in market.outcomes)
    anchor_dt = market.end_date or event.end_date
    anchor = anchor_dt.date() if anchor_dt else None

    sp = _SPREAD_RE.search(title)
    if sp:  # "Chelsea FC (-1.5)": the signed handicap; P(cover) rises with it
        key = float(sp.group(1) + sp.group(2))
        stem = _SPREAD_RE.sub("(#)", title).strip().lower()
        if stem.replace("(#)", "").strip():
            return "spread", stem, key, +1, title
    ou = _OU_RE.search(title) or _OU_RE.search(question)
    if ou and outcomes[:1] in (("over",), ("under",)):
        key = float(ou.group(1))
        src = title if _OU_RE.search(title) else question
        stem = _OU_RE.sub("O/U #", src).strip().lower()
        return "ou", stem, key, (-1 if outcomes[0] == "over" else +1), src

    if event.neg_risk or market.neg_risk:
        return None  # mutually exclusive outcomes are brackets (temperature 24°C, 25°C ...), not nested rungs
    if title:
        td = _title_date(title, anchor)
        if td is not None:
            key, direction, label = td
            if direction is None:
                pm = _PREP_DATE_RE.search(question)
                if pm is None:
                    return None  # "released on October 2": an exact-day bracket, not a rung
                direction = _prep_direction(pm.group(1))
            if _negated(question):
                direction = -direction
            return "date", f"{event.id}:@", key, direction, label
        nk = parse_number_key(title)
        if nk is not None and not _DATE_RE.search(title):
            direction = _number_direction(title, event.title, question)
            if direction is None:
                return None
            stem = title.replace(nk[1], "#").strip().lower()
            return "number", stem, nk[0], direction, title
    pm = _PREP_DATE_RE.search(question)
    if pm is not None:
        dq = parse_date_key(pm.group(0), anchor)
        if dq is None:
            return None
        direction = _prep_direction(pm.group(1))
        if _negated(question):
            direction = -direction
        stem = _PREP_DATE_RE.sub("@", question).strip().lower()
        return "date", stem, dq[0], direction, pm.group(0).rstrip("?")
    return None


def question_stem(market: PolyMarket, event: PolyEvent) -> tuple[str, float, int] | None:
    """Cross-event key for '... by <date>?' questions: (stem with the date blanked, date
    ordinal, direction)."""
    q = market.question.strip()
    if event.neg_risk or market.neg_risk:
        return None
    pm = _PREP_DATE_RE.search(q)
    if pm is None:
        return None
    anchor_dt = market.end_date or event.end_date
    dk = parse_date_key(pm.group(0), anchor_dt.date() if anchor_dt else None)
    if dk is None:
        return None
    direction = _prep_direction(pm.group(1))
    if _negated(q):
        direction = -direction
    stem = re.sub(r"\s+", " ", _PREP_DATE_RE.sub("@", q)).strip(" ?").lower()
    return stem, dk[0], direction


def build_ladders(events: list[PolyEvent], cross_event: bool = True, min_rungs: int = 2) -> list[Ladder]:
    groups: dict[tuple, list[tuple[float, PolyMarket, PolyEvent, str, int]]] = defaultdict(list)
    for ev in events:
        for m in ev.markets:
            if not m.is_binary or not m.enable_order_book:
                continue
            c = classify(m, ev)
            if c is None:
                continue
            kind, stem, key, direction, label = c
            groups[(ev.id, stem, tuple(o.lower() for o in m.outcomes), kind)].append((key, m, ev, label, direction))
    ladders: list[Ladder] = []
    for (ev_id, stem, _outs, kind), items in groups.items():
        items.sort(key=lambda x: x[0])
        keys = [x[0] for x in items]
        if len(items) < min_rungs or len(set(keys)) != len(keys):
            continue  # duplicate thresholds mean the stem is ambiguous (two teams' spreads, say)
        directions = {x[4] for x in items}
        if len(directions) != 1:
            continue
        rungs = [Rung(m, e, k, lab) for k, m, e, lab, _ in items]
        ladders.append(Ladder(id=f"{ev_id}:{stem}", title=f"{items[0][2].title} | {stem}", kind=kind, direction=directions.pop(), rungs=rungs))
    if cross_event:
        cross: dict[tuple, list[tuple[float, PolyMarket, PolyEvent, int]]] = defaultdict(list)
        for ev in events:
            for m in ev.markets:
                if not m.is_binary or not m.enable_order_book:
                    continue
                qs = question_stem(m, ev)
                if qs is None:
                    continue
                cross[(qs[0], tuple(o.lower() for o in m.outcomes))].append((qs[1], m, ev, qs[2]))
        for (stem, _outs), items in cross.items():
            ev_ids = {e.id for _, _, e, _ in items}
            keys = [k for k, _, _, _ in items]
            dirs = {d for _, _, _, d in items}
            if len(ev_ids) < 2 or len(set(keys)) != len(keys) or len(items) < min_rungs or len(dirs) != 1:
                continue
            items.sort(key=lambda x: x[0])
            rungs = [Rung(m, e, k, m.question) for k, m, e, _ in items]
            ladders.append(Ladder(id=f"x:{stem}", title=stem, kind="date", direction=dirs.pop(), rungs=rungs, cross_event=True))
    return ladders


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #

def _rate(rung: Rung, override: float | None) -> float:
    if override is not None:
        return override
    if rung.market.fee_rate is not None:
        return rung.market.fee_rate
    return polymarket_rate_for_event(rung.event, None)


def pair_edge(sub: Rung, sup: Rung, sub_bid: float, sup_ask: float, fee_override: float | None = None) -> tuple[float, float, float]:
    """(gap, fee, net edge) per $1 set for: buy YES(superset) at sup_ask, buy NO(subset) at 1 - sub_bid."""
    gap = sub_bid - sup_ask
    fee = polymarket_taker_fee(sup_ask, 1.0, _rate(sup, fee_override)) + polymarket_taker_fee(1.0 - sub_bid, 1.0, _rate(sub, fee_override))
    return gap, fee, gap - fee


def check_ladder(ladder: Ladder, fee_override: float | None = None, soft_tolerance: float = 0.005) -> tuple[list[Violation], list[float]]:
    """Violations on Gamma's top of book, plus the gap (subset bid - superset ask) of every
    adjacent pair that is live on both sides; negative gaps are consistent ladders."""
    out: list[Violation] = []
    gaps: list[float] = []
    rungs = ladder.ordered()
    for i, sub in enumerate(rungs):
        for sup in rungs[i + 1 :]:
            sb, sa = sub.market.best_bid, sup.market.best_ask
            if sb is None or sa is None or not sub.market.accepting_orders or not sup.market.accepting_orders:
                continue
            gap, fee, net = pair_edge(sub, sup, sb, sa, fee_override)
            live = sub.quoted and sup.quoted
            if sup is rungs[i + 1] and live:
                gaps.append(gap)
            if gap > 0:
                out.append(Violation(ladder.title, "hard", sub.label, sup.label, sb, sa, round(gap, 4), round(fee, 4), round(net, 4), url=ladder.url, subset_id=sub.market.yes_token or "", superset_id=sup.market.yes_token or "", subset_question=sub.market.question, superset_question=sup.market.question))
            elif live and sub.mid is not None and sup.mid is not None and sub.mid > sup.mid + soft_tolerance:
                out.append(Violation(ladder.title, "soft", sub.label, sup.label, sb, sa, round(gap, 4), round(fee, 4), round(net, 4), url=ladder.url))
    return out, gaps


def confirm_on_books(v: Violation, books: dict[str, Book], sub: Rung, sup: Rung, budget: float, fee_override: float | None = None) -> Violation | None:
    """Re-price a hard violation from live books (top of book only) and size it to the budget."""
    b_sub, b_sup = books.get(v.subset_id), books.get(v.superset_id)
    if not b_sub or not b_sup or b_sub.best_bid is None or b_sup.best_ask is None:
        return None
    sb, sa = b_sub.best_bid.price, b_sup.best_ask.price
    gap, fee, net = pair_edge(sub, sup, sb, sa, fee_override)
    cost = sa + (1.0 - sb) + fee
    fillable = min(b_sub.best_bid.size, b_sup.best_ask.size)
    sets = min(fillable, budget / cost) if cost > 0 else 0.0
    return Violation(v.ladder, "hard", v.subset, v.superset, sb, sa, round(gap, 4), round(fee, 4), round(net, 4), fillable, round(sets, 2), round(sets * net, 2), v.url, v.subset_id, v.superset_id, sub.market.question, sup.market.question)


@dataclass
class LadderReport:
    when: str
    events: int
    ladders: int
    rungs: int
    pairs: int  # adjacent pairs live on both sides
    by_kind: dict[str, int]
    gap_median: float | None
    gap_p90: float | None  # 90th percentile of the adjacent gap (closest to a violation)
    within_2c: int  # adjacent pairs whose gap is > -0.02 (a violation is two ticks away)
    soft: list[Violation]
    hard: list[Violation]  # confirmed on books, net of fees
    hard_gamma: list[Violation]  # as seen on Gamma before confirmation
    hard_unconfirmed: int  # seen on Gamma but gone or unprofitable on the books
    examples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def scan_ladders(events: list[PolyEvent], book_fetcher: Callable[[list[str]], dict[str, Book]] | None, *, now: datetime, budget: float = 50.0, min_edge: float = 0.0, fee_override: float | None = None, cross_event: bool = True, examples: int = 5) -> LadderReport:
    ladders = build_ladders(events, cross_event=cross_event)
    soft: list[Violation] = []
    hard_gamma: list[tuple[Violation, Rung, Rung]] = []
    gaps: list[float] = []
    by_kind: dict[str, int] = defaultdict(int)
    for lad in ladders:
        by_kind[lad.kind] += 1
        vs, g = check_ladder(lad, fee_override)
        gaps.extend(g)
        rungs = {r.label: r for r in lad.rungs}
        for v in vs:
            if v.kind == "hard":
                hard_gamma.append((v, rungs[v.subset], rungs[v.superset]))
            else:
                soft.append(v)
    hard: list[Violation] = []
    unconfirmed = 0
    if hard_gamma:
        tokens = [t for v, _, _ in hard_gamma for t in (v.subset_id, v.superset_id) if t]
        books = book_fetcher(tokens) if book_fetcher else {}
        for v, sub, sup in hard_gamma:
            c = confirm_on_books(v, books, sub, sup, budget, fee_override) if books else None
            if c is not None and c.edge_per_set >= min_edge and c.gap > 0:
                c.set_horizon(sub, sup, now)
                hard.append(c)
            else:
                unconfirmed += 1
    hard.sort(key=lambda v: -v.est_profit)
    soft.sort(key=lambda v: -v.gap)
    gs = sorted(gaps)
    ex = []
    for lad in [l for l in ladders if sum(r.quoted for r in l.rungs) >= 2][:examples]:
        ex.append({"title": lad.title[:90], "kind": lad.kind, "rungs": [(r.label, r.market.best_bid, r.market.best_ask) for r in lad.ordered()][:8]})
    return LadderReport(
        when=now.isoformat(timespec="seconds"),
        events=len(events), ladders=len(ladders), rungs=sum(len(l.rungs) for l in ladders), pairs=len(gaps),
        by_kind=dict(by_kind),
        gap_median=round(statistics.median(gs), 4) if gs else None,
        gap_p90=round(gs[int(0.9 * (len(gs) - 1))], 4) if gs else None,
        within_2c=sum(1 for g in gs if g > -0.02),
        soft=soft, hard=hard, hard_gamma=[v for v, _, _ in hard_gamma], hard_unconfirmed=unconfirmed, examples=ex,
    )


def render_ladder_report(r: LadderReport, top: int = 15) -> str:
    L = [f"Ladder consistency at {r.when}: {r.events} events, {r.ladders} ladders ({', '.join(f'{k} {v}' for k, v in sorted(r.by_kind.items()))}), {r.rungs} rungs, {r.pairs} adjacent pairs live on both sides."]
    if r.pairs:
        L.append(f"  adjacent gap (subset bid - superset ask): median {r.gap_median:+.3f}, 90th pct {r.gap_p90:+.3f}; {r.within_2c} pairs within 2c of a violation.")
    L.append(f"  soft violations (mids inverted on live quotes): {len(r.soft)}; hard on Gamma: {len(r.hard_gamma)}, confirmed on books net of fees: {len(r.hard)}.")
    if r.hard:
        L.append("")
        L.append("  Executable (buy YES on the superset, NO on the subset; pays >= $1 per set):")
        L.append(f"  {'net/set':>8} {'gap':>6} {'fee':>6} {'sets':>7} {'profit':>7} {'days':>5} {'/yr':>6}  ladder | subset -> superset")
        for v in r.hard[:top]:
            days = f"{v.days_to_resolve:5.0f}" if v.days_to_resolve is not None else "    -"
            ann = f"{v.annualized * 100:5.0f}%" if v.annualized is not None else "     -"
            L.append(f"  {v.edge_per_set:+8.3f} {v.gap:+6.3f} {v.fee_per_set:6.3f} {v.budget_sets:7.1f} {v.est_profit:7.2f} {days} {ann}  {v.ladder[:50]} | {v.subset[:30]} -> {v.superset[:30]}")
    if r.hard_gamma and not r.hard:
        L.append("")
        L.append("  Hard on Gamma's quotes but not on the books (stale or one-sided quotes):")
        for v in r.hard_gamma[:top]:
            L.append(f"  gap {v.gap:+.3f}  {v.ladder[:60]} | {v.subset[:30]} ({v.subset_bid:.3f}) -> {v.superset[:30]} ({v.superset_ask:.3f})")
    if r.soft:
        L.append("")
        L.append("  Soft (mids inverted, not executable after the spread):")
        for v in r.soft[:top]:
            L.append(f"  gap {v.gap:+.3f}  {v.ladder[:60]} | {v.subset[:30]} ({v.subset_bid:.2f}) -> {v.superset[:30]} ({v.superset_ask:.2f})")
    if r.examples:
        L.append("")
        L.append("  Sample ladders (hardest rung first, bid/ask):")
        for e in r.examples:
            L.append(f"  [{e['kind']}] {e['title']}: " + "; ".join(f"{lab} {b if b is not None else '-'}/{a if a is not None else '-'}" for lab, b, a in e["rungs"]))
    return "\n".join(L)


def summarize_snapshots(reports: list[LadderReport]) -> str:
    if not reports:
        return "no snapshots"
    hard = [v for r in reports for v in r.hard]
    pairs = sum(r.pairs for r in reports)
    keys = {(v.ladder, v.subset, v.superset) for v in hard}
    L = [f"{len(reports)} snapshots, {pairs} live pair-checks, {sum(len(r.hard_gamma) for r in reports)} hard on Gamma, {len(hard)} executable on books across {len(keys)} distinct pairs."]
    if hard:
        best = max(hard, key=lambda v: v.est_profit)
        L.append(f"  best: {best.ladder[:60]} | {best.subset[:30]} -> {best.superset[:30]}: net {best.edge_per_set:+.3f}/set, {best.budget_sets:.1f} sets, ${best.est_profit:.2f} at the budget")
        L.append(f"  total budget-sized profit if every one were taken: ${sum(v.est_profit for v in hard):.2f}; median days to resolution {statistics.median([v.days_to_resolve for v in hard if v.days_to_resolve is not None] or [0]):.0f}")
    else:
        L.append("  none was executable after fees on live books.")
    return "\n".join(L)
