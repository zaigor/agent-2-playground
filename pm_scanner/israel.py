"""Knesset election model (27 Oct 2026) against Polymarket's Israel election markets.

    polls (seats per party, CSV) -> recency- and pollster-weighted vote-share estimate
    -> Monte Carlo with independent + bloc-correlated polling error
    -> 3.25% threshold + Bader-Ofer allocation (D'Hondt with surplus-vote pairs)
    -> P(outcome) for every seat-bracket, vote-share, threshold and most-seats market
    -> edge net of the politics taker fee, quarter-Kelly sizing, and maker quotes.

Everything here is public information (published polls, published prices). The error
model is a parametrised guess calibrated loosely on 2019-2022 (see MEMO.md); treat its
probabilities as a second opinion to argue with, not as truth.
"""
from __future__ import annotations

import csv
import math
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .fees import polymarket_rate_for_tags, polymarket_taker_fee
from .polymarket import Book, PolyEvent, PolyMarket

THRESHOLD = 0.0325
KNESSET_SEATS = 120
ELECTION_DAY = date(2026, 10, 27)


# ----------------------------------------------------------------------------- parties
@dataclass(frozen=True)
class Party:
    key: str
    name: str
    aliases: tuple[str, ...]
    bloc: str  # netanyahu | change | arab | none


PARTIES: tuple[Party, ...] = (
    Party("likud", "Likud", ("likud",), "netanyahu"),
    Party("shas", "Shas", ("shas",), "netanyahu"),
    Party("utj", "United Torah Judaism", ("united torah judaism", "utj"), "netanyahu"),
    Party("rzp", "Religious Zionist Party-Zehut", ("religious zionist party-zehut", "religious zionist", "religious zionism", "rzp-zehut", "rzp"), "netanyahu"),
    Party("otzma", "Otzma Yehudit", ("otzma yehudit", "otzma"), "netanyahu"),
    Party("amcha", "Amcha Yisrael", ("amcha yisrael", "people of israel", "amcha"), "netanyahu"),
    Party("yashar", "Yashar", ("yashar",), "change"),
    Party("together", "Together (BeYachad)", ("together", "beyachad", "b'yachad", "byachad"), "change"),
    Party("dems", "The Democrats", ("the democrats", "democrats", "dems"), "change"),
    Party("yb", "Yisrael Beiteinu", ("yisrael beiteinu", "yisrael beytenu", "israel beiteinu"), "change"),
    Party("joint_list", "The Joint List", ("the joint list", "joint list", "hadash"), "arab"),
    Party("raam", "Ra'am", ("united arab list (ra'am)", "united arab list", "ra'am", "raam"), "arab"),
    Party("reservists", "Reservists-New Economic Party", ("reservists and new economic party", "reservists-new economic party", "reservists"), "none"),
    Party("blue_white", "Blue and White", ("blue and white", "blue & white"), "none"),
    Party("unity", "Unity", ("unity",), "none"),
)
PARTY_BY_KEY: dict[str, Party] = {p.key: p for p in PARTIES}
BLOCS: dict[str, tuple[str, ...]] = {
    "netanyahu": tuple(p.key for p in PARTIES if p.bloc == "netanyahu"),
    "change": tuple(p.key for p in PARTIES if p.bloc == "change"),
    "arab": tuple(p.key for p in PARTIES if p.bloc == "arab"),
}
# Surplus-vote (Bader-Ofer) pairs reported in the Hebrew press by late September 2026.
# likud:rzp and otzma:amcha were "planned"/"uncertain" then; shas:utj is the habitual pair.
DEFAULT_SURPLUS_PAIRS: tuple[tuple[str, str], ...] = (
    ("yashar", "dems"),
    ("together", "yb"),
    ("joint_list", "raam"),
    ("likud", "rzp"),
    ("shas", "utj"),
    ("otzma", "amcha"),
)


def _norm(text: str) -> str:
    text = text.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def find_party(text: str) -> Party | None:
    """Longest alias that appears as a whole word in `text`."""
    t = _norm(text)
    best: tuple[int, Party] | None = None
    for p in PARTIES:
        for alias in p.aliases:
            if re.search(r"(?<![a-z'])" + re.escape(alias) + r"(?![a-z'])", t):
                if best is None or len(alias) > best[0]:
                    best = (len(alias), p)
    return best[1] if best else None


# ----------------------------------------------------------------------------- polls
@dataclass
class Poll:
    date: date
    pollster: str
    sample: int | None
    shares: dict[str, float]  # party -> estimated vote share (fraction)
    seats: dict[str, float]  # party -> seats as published (0 = under the threshold)
    placeholder: bool = False

    @property
    def label(self) -> str:
        return f"{self.date.isoformat()} {self.pollster}"


def _parse_cell(cell: str) -> tuple[str, float] | None:
    cell = cell.strip()
    if not cell or cell in ("-", "—"):
        return None
    if cell.endswith("%"):
        return ("share", float(cell[:-1]) / 100.0)
    return ("seats", float(cell))


def load_polls(path: Path, under_threshold_share: float = 0.02) -> list[Poll]:
    """CSV: date,pollster,sample,<party keys...>. Seats as published; '2.6%' for a party
    the poll puts under the threshold; 0 for under-threshold without a figure; blank =
    not reported. Lines starting with # are comments."""
    lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    polls: list[Poll] = []
    for row in reader:
        try:
            d = date.fromisoformat(row["date"].strip())
        except (KeyError, ValueError, AttributeError):
            continue
        sample = None
        try:
            sample = int(float(row.get("sample") or "")) if (row.get("sample") or "").strip() else None
        except ValueError:
            sample = None
        seats: dict[str, float] = {}
        explicit_share: dict[str, float] = {}
        for key, cell in row.items():
            if key in ("date", "pollster", "sample") or key is None or key not in PARTY_BY_KEY:
                continue
            parsed = _parse_cell(cell or "")
            if parsed is None:
                continue
            kind, val = parsed
            if kind == "share":
                explicit_share[key] = val
            else:
                seats[key] = val
        wasted = sum(explicit_share.values()) + under_threshold_share * sum(1 for v in seats.values() if v <= 0)
        wasted = min(wasted, 0.30)
        shares: dict[str, float] = dict(explicit_share)
        for key, s in seats.items():
            shares[key] = under_threshold_share if s <= 0 else s / KNESSET_SEATS * (1.0 - wasted)
        pollster = (row.get("pollster") or "?").strip()
        polls.append(Poll(d, pollster, sample, shares, seats, placeholder="placeholder" in pollster.lower()))
    polls.sort(key=lambda p: p.date, reverse=True)
    return polls


@dataclass
class Estimate:
    as_of: date
    shares: dict[str, float]
    spread: dict[str, float]  # weighted sd across polls, fraction
    n_polls: dict[str, int]
    used: list[tuple[Poll, float]]  # (poll, weight)
    warnings: list[str] = field(default_factory=list)


def aggregate(polls: Iterable[Poll], as_of: date, half_life_days: float = 7.0, window_days: float = 21.0) -> Estimate:
    """Recency weight 0.5^(age/half_life), then divided by that pollster's poll count so
    a prolific house does not dominate. Parties are averaged over the polls that report them."""
    window = [p for p in polls if 0 <= (as_of - p.date).days <= window_days]
    if not window:
        window = sorted(polls, key=lambda p: p.date, reverse=True)[:5]
    counts = Counter(p.pollster for p in window)
    used: list[tuple[Poll, float]] = []
    for p in window:
        age = max(0, (as_of - p.date).days)
        used.append((p, 0.5 ** (age / half_life_days) / counts[p.pollster]))
    shares: dict[str, float] = {}
    spread: dict[str, float] = {}
    n_polls: dict[str, int] = {}
    warnings: list[str] = []
    for party in PARTIES:
        pts = [(p.shares[party.key], w) for p, w in used if party.key in p.shares]
        n_polls[party.key] = len(pts)
        if not pts:
            shares[party.key] = 0.005
            spread[party.key] = 0.005
            warnings.append(f"no poll reports {party.name}; assuming 0.5% of the vote")
            continue
        wsum = sum(w for _, w in pts)
        mean = sum(x * w for x, w in pts) / wsum
        var = sum(w * (x - mean) ** 2 for x, w in pts) / wsum
        shares[party.key] = mean
        spread[party.key] = math.sqrt(var)
    total = sum(shares.values())
    if abs(total - 1.0) > 0.06:
        warnings.append(f"poll shares sum to {total * 100:.1f}% before normalisation; check the CSV")
    if any(p.placeholder for p, _ in used):
        warnings.append("PLACEHOLDER poll rows are in use: replace them with real polls before trusting any number")
    if len(used) < 3:
        warnings.append(f"only {len(used)} poll(s) in the window; the estimate is fragile")
    return Estimate(as_of, shares, spread, n_polls, used, warnings)


# ----------------------------------------------------------------------------- seats
def dhondt(votes: dict[str, float], seats: int) -> dict[str, int]:
    """Highest-averages allocation. Starts from the lower Hare quota (D'Hondt always
    satisfies it) so only a handful of seats go through the averages loop."""
    total = sum(votes.values())
    if total <= 0 or seats <= 0 or not votes:
        return {k: 0 for k in votes}
    alloc = {k: int(v * seats // total) for k, v in votes.items()}
    left = seats - sum(alloc.values())
    while left > 0:
        best = max(votes, key=lambda k: (votes[k] / (alloc[k] + 1), votes[k]))
        alloc[best] += 1
        left -= 1
    return alloc


def allocate_seats(
    shares: dict[str, float],
    surplus_pairs: Iterable[tuple[str, str]] = DEFAULT_SURPLUS_PAIRS,
    threshold: float = THRESHOLD,
    seats: int = KNESSET_SEATS,
) -> dict[str, int]:
    """Bader-Ofer: lists under the threshold are dropped, paired lists compete as one and
    then split their seats between themselves by the same highest-averages rule."""
    qualifying = {k: v for k, v in shares.items() if v >= threshold}
    units: dict[str, float] = {}
    members: dict[str, tuple[str, ...]] = {}
    paired: set[str] = set()
    for a, b in surplus_pairs:
        if a in qualifying and b in qualifying and a not in paired and b not in paired:
            units[f"{a}+{b}"] = qualifying[a] + qualifying[b]
            members[f"{a}+{b}"] = (a, b)
            paired.update((a, b))
    for k, v in qualifying.items():
        if k not in paired:
            units[k] = v
            members[k] = (k,)
    out = {k: 0 for k in shares}
    for unit, n in dhondt(units, seats).items():
        ms = members[unit]
        if len(ms) == 1:
            out[ms[0]] = n
        else:
            for k, s in dhondt({m: qualifying[m] for m in ms}, n).items():
                out[k] = s
    return out


# ----------------------------------------------------------------------------- simulation
@dataclass
class ErrorModel:
    scale: float = 1.0  # multiplies every independent sd
    base_sd: float = 0.008  # share sd for a tiny list (0.8 points)
    slope: float = 0.05  # extra sd per unit of share: a 20% list gets +1.0 point
    bloc_sd: float = 0.010  # sd of a common right<->change transfer (1 point)
    arab_sd: float = 0.006  # sd of an Arab-turnout shock shared by the Arab lists
    bloc_bias: float = 0.0  # mean of the transfer; +0.01 = polls understate the right by 1 point
    other_share: float = 0.01  # votes for lists not modelled, all under the threshold
    sd_overrides: dict[str, float] = field(default_factory=dict)  # party key -> sd as a vote fraction

    def party_sd(self, party: str, share: float, spread: float, n_polls: int) -> float:
        """Independent error sd for one list, as a vote fraction. The floor is half the
        list's own share until it reaches `base_sd` (a 0.3% list cannot miss by a point;
        a 4% one can), plus `slope` per unit of share, plus the polls' own disagreement."""
        if party in self.sd_overrides:
            return self.sd_overrides[party]
        base = min(0.5 * share, self.base_sd) + self.slope * share
        return self.scale * math.sqrt(base**2 + spread**2 / max(1, n_polls))


@dataclass
class SimResult:
    n: int
    seats: dict[str, list[int]]
    shares: dict[str, list[float]]
    winners: list[str]  # party key with most seats per simulation (tie rules applied)

    def p_seats_between(self, party: str, lo: int | None, hi: int | None) -> float:
        xs = self.seats[party]
        lo_v = -1 if lo is None else lo
        hi_v = 10**9 if hi is None else hi
        return sum(1 for s in xs if lo_v <= s <= hi_v) / self.n

    def p_share_between(self, party: str, lo: float | None, hi: float | None) -> float:
        xs = self.shares[party]
        lo_v = -1.0 if lo is None else lo
        hi_v = 2.0 if hi is None else hi
        return sum(1 for x in xs if lo_v <= x < hi_v) / self.n

    def p_wins_seat(self, party: str) -> float:
        return self.p_seats_between(party, 1, None)

    def p_most_seats(self, party: str) -> float:
        return sum(1 for w in self.winners if w == party) / self.n

    def p_bloc_at_least(self, keys: Iterable[str], n: int) -> float:
        keys = tuple(keys)
        return sum(1 for i in range(self.n) if sum(self.seats[k][i] for k in keys) >= n) / self.n

    def seat_summary(self, party: str) -> dict[str, float]:
        xs = sorted(self.seats[party])
        mean = sum(xs) / self.n
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / self.n)
        return {
            "mean": mean,
            "sd": sd,
            "p5": xs[int(0.05 * (self.n - 1))],
            "p50": xs[int(0.50 * (self.n - 1))],
            "p95": xs[int(0.95 * (self.n - 1))],
            "p_seat": self.p_wins_seat(party),
        }


def simulate(
    est: Estimate,
    model: ErrorModel = ErrorModel(),
    surplus_pairs: Iterable[tuple[str, str]] = DEFAULT_SURPLUS_PAIRS,
    n_sims: int = 10000,
    seed: int = 1,
    threshold: float = THRESHOLD,
) -> SimResult:
    rng = random.Random(seed)
    keys = [p.key for p in PARTIES]
    means = {k: est.shares.get(k, 0.005) for k in keys}
    sds = {k: model.party_sd(k, means[k], est.spread.get(k, 0.0), est.n_polls.get(k, 0)) for k in keys}
    bloc_mass = {b: sum(means[k] for k in ks) or 1.0 for b, ks in BLOCS.items()}
    pairs = tuple(surplus_pairs)
    seats_out: dict[str, list[int]] = {k: [] for k in keys}
    shares_out: dict[str, list[float]] = {k: [] for k in keys}
    winners: list[str] = []
    names = {k: PARTY_BY_KEY[k].name for k in keys}
    for _ in range(n_sims):
        transfer = rng.gauss(model.bloc_bias, model.bloc_sd)
        arab = rng.gauss(0.0, model.arab_sd)
        raw: dict[str, float] = {}
        for k in keys:
            x = means[k] + rng.gauss(0.0, sds[k])
            bloc = PARTY_BY_KEY[k].bloc
            if bloc == "netanyahu":
                x += transfer * means[k] / bloc_mass["netanyahu"]
            elif bloc == "change":
                x -= transfer * means[k] / bloc_mass["change"]
            elif bloc == "arab":
                x += arab * means[k] / bloc_mass["arab"]
            raw[k] = max(0.0005, x)
        total = sum(raw.values()) + model.other_share
        shares = {k: v / total for k, v in raw.items()}
        seats = allocate_seats(shares, pairs, threshold)
        top = max(seats.values())
        tied = [k for k in keys if seats[k] == top]
        if len(tied) > 1:  # rules: more valid votes, then alphabetical
            tied.sort(key=lambda k: (-shares[k], names[k]))
        winners.append(tied[0])
        for k in keys:
            seats_out[k].append(seats[k])
            shares_out[k].append(shares[k])
    return SimResult(n_sims, seats_out, shares_out, winners)


# ----------------------------------------------------------------------------- markets
@dataclass
class Bucket:
    kind: str  # seats | share | threshold | most
    party: str
    lo: float | None
    hi: float | None
    label: str


_SEAT_LT = re.compile(r"^<\s*(\d+)$")
_SEAT_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_SEAT_PLUS = re.compile(r"^(\d+)\s*\+$")
_SEAT_EXACT = re.compile(r"^(\d+)$")
_SHARE_LT = re.compile(r"^<\s*([\d.]+)%$")
_SHARE_RANGE = re.compile(r"^([\d.]+)\s*-\s*([\d.]+)%$")
_SHARE_PLUS = re.compile(r"^([\d.]+)%\s*\+$")


def parse_seat_label(label: str) -> tuple[int | None, int | None] | None:
    s = _norm(label).replace(" seats", "")
    if m := _SEAT_LT.match(s):
        return (None, int(m.group(1)) - 1)
    if m := _SEAT_RANGE.match(s):
        return (int(m.group(1)), int(m.group(2)))
    if m := _SEAT_PLUS.match(s):
        return (int(m.group(1)), None)
    if m := _SEAT_EXACT.match(s):
        return (int(m.group(1)), int(m.group(1)))
    return None


def parse_share_label(label: str) -> tuple[float | None, float | None] | None:
    """Polymarket resolves a value on a boundary to the higher bracket, so brackets are [lo, hi)."""
    s = _norm(label).replace(" ", "")
    if m := _SHARE_LT.match(s):
        return (None, float(m.group(1)) / 100)
    if m := _SHARE_RANGE.match(s):
        return (float(m.group(1)) / 100, float(m.group(2)) / 100)
    if m := _SHARE_PLUS.match(s):
        return (float(m.group(1)) / 100, None)
    return None


def classify_market(event: PolyEvent, market: PolyMarket) -> Bucket | None:
    title = _norm(event.title)
    label = market.group_item_title or market.question
    if "# of seats" in title or "number of seats" in title:
        party = find_party(title.split(":", 1)[-1])
        rng = parse_seat_label(label)
        if party and rng:
            return Bucket("seats", party.key, rng[0], rng[1], label)
        return None
    if "vote share" in title:
        party = find_party(title.split(":", 1)[-1])
        rng = parse_share_label(label)
        if party and rng:
            return Bucket("share", party.key, rng[0], rng[1], label)
        return None
    if "win a seat" in title:
        party = find_party(label)
        return Bucket("threshold", party.key, 1, None, label) if party else None
    if "election winner" in title or "most seats" in title:
        party = find_party(label)
        return Bucket("most", party.key, None, None, label) if party else None
    if "lose seats" in title:
        party = find_party(title)
        m = re.search(r"less than (\d+) seats", _norm(market.raw.get("description") or ""))
        if party and m:
            return Bucket("seats", party.key, None, int(m.group(1)) - 1, f"<{m.group(1)}")
        return None
    return None


def model_probability(sim: SimResult, b: Bucket) -> float:
    if b.kind == "seats":
        return sim.p_seats_between(b.party, None if b.lo is None else int(b.lo), None if b.hi is None else int(b.hi))
    if b.kind == "share":
        return sim.p_share_between(b.party, b.lo, b.hi)
    if b.kind == "threshold":
        return sim.p_wins_seat(b.party)
    if b.kind == "most":
        return sim.p_most_seats(b.party)
    raise ValueError(b.kind)


@dataclass
class Trade:
    event: str
    label: str
    party: str
    kind: str
    url: str
    model_p: float
    bid: float | None
    ask: float | None
    depth_bid: float  # $ resting at the best bid (what a NO taker can hit)
    depth_ask: float  # $ resting at the best ask (what a YES taker can lift)
    fee_rate: float
    action: str  # take YES | take NO | post YES bid | post NO bid
    price: float
    edge_per_share: float
    stake: float
    exp_profit: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__ | {"party_name": PARTY_BY_KEY[self.party].name}


def _best(book: Book | None, market: PolyMarket) -> tuple[float | None, float, float | None, float]:
    bid = ask = None
    dbid = dask = 0.0
    if book is not None:
        if book.best_bid:
            bid, dbid = book.best_bid.price, book.best_bid.price * book.best_bid.size
        if book.best_ask:
            ask, dask = book.best_ask.price, book.best_ask.price * book.best_ask.size
    if bid is None:
        bid = market.best_bid
    if ask is None:
        ask = market.best_ask
    return bid, dbid, ask, dask


def evaluate_market(
    b: Bucket,
    event: PolyEvent,
    market: PolyMarket,
    book: Book | None,
    p: float,
    *,
    budget: float,
    fee_rate: float,
    min_edge: float = 0.03,
    kelly_fraction: float = 0.25,
    max_fraction: float = 0.10,
    maker_margin: float = 0.06,
    min_price: float = 0.05,
) -> list[Trade]:
    """Up to four candidate trades. Any leg whose cheap side costs less than `min_price` is
    dropped: a few thousand simulations cannot price a 1c contract, and the "profit" on one
    is mostly model noise."""
    bid, dbid, ask, dask = _best(book, market)
    notes: list[str] = []
    if bid is not None and ask is not None and ask - bid > 0.10:
        notes.append(f"spread {ask - bid:.2f}: the mid is not a price")
    out: list[Trade] = []
    common = dict(event=event.title, label=b.label, party=b.party, kind=b.kind, url=market.url, model_p=p, bid=bid, ask=ask, depth_bid=dbid, depth_ask=dask, fee_rate=fee_rate)
    cap = budget * max_fraction

    def priced(x: float) -> bool:
        return min_price <= x <= 1 - min_price

    # --- taker, YES side: pay the ask, win $1 with probability p
    if ask is not None and priced(ask):
        fee = polymarket_taker_fee(ask, 1.0, fee_rate)
        edge = p - ask - fee
        if edge >= min_edge:
            kelly = (p - ask) / (1 - ask)
            stake = min(budget * min(kelly * kelly_fraction, max_fraction), dask if dask > 0 else cap)
            shares = stake / ask if ask > 0 else 0.0
            n = notes + ([f"top of book only ${dask:.0f}"] if 0 < dask < 20 else [])
            out.append(Trade(**common, action="take YES", price=ask, edge_per_share=edge, stake=stake, exp_profit=shares * edge, notes=n))
    # --- taker, NO side: NO costs 1-bid, wins $1 with probability 1-p
    if bid is not None and priced(bid):
        cost = 1 - bid
        fee = polymarket_taker_fee(cost, 1.0, fee_rate)
        edge = (1 - p) - cost - fee
        if edge >= min_edge:
            kelly = ((1 - p) - cost) / (1 - cost)
            depth_no = dbid / bid * cost if bid > 0 else 0.0  # the YES bid, priced in NO dollars
            stake = min(budget * min(kelly * kelly_fraction, max_fraction), depth_no if depth_no > 0 else cap)
            shares = stake / cost
            n = notes + ([f"top of book only ${depth_no:.0f}"] if 0 < depth_no < 20 else [])
            out.append(Trade(**common, action="take NO", price=cost, edge_per_share=edge, stake=stake, exp_profit=shares * edge, notes=n))
    # --- maker quotes: no fee, fill not guaranteed; sit `maker_margin` on our side of the model
    if bid is not None and ask is not None and 0 <= bid < ask <= 1:
        yes_bid = round(min(p - maker_margin, ask - 0.01), 2)
        if yes_bid > bid + 1e-9 and p - yes_bid >= min_edge and priced(yes_bid):
            out.append(Trade(**common, action="post YES bid", price=yes_bid, edge_per_share=p - yes_bid, stake=cap, exp_profit=cap / yes_bid * (p - yes_bid), notes=notes + ["maker: pays no fee, fills only if someone sells into it"]))
        yes_ask = round(max(p + maker_margin, bid + 0.01), 2)
        if yes_ask < ask - 1e-9 and yes_ask - p >= min_edge and priced(yes_ask):
            no_bid = round(1 - yes_ask, 2)
            out.append(Trade(**common, action="post NO bid", price=no_bid, edge_per_share=yes_ask - p, stake=cap, exp_profit=cap / no_bid * (yes_ask - p), notes=notes + ["maker: shows as a YES ask at %.2f; pays no fee" % yes_ask]))
    return out


def implied_seat_mean(rows: list[tuple[Bucket, PolyMarket, Book | None]]) -> tuple[float | None, float, float]:
    """Market-implied expected seats from bracket mids; also the sums of asks and bids,
    which show how wide the whole event's spread is (1.00/1.00 would be perfectly tight)."""
    mids: list[tuple[float, float]] = []
    ask_sum = bid_sum = 0.0
    for b, m, book in rows:
        bid, _, ask, _ = _best(book, m)
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2
            ask_sum += ask
            bid_sum += bid
        elif m.outcome_prices:
            mid = m.outcome_prices[0]
        else:
            continue
        if b.lo is None and b.hi is not None:
            centre = b.hi - 1.0
        elif b.hi is None and b.lo is not None:
            centre = b.lo + 2.0
        elif b.lo is not None and b.hi is not None:
            centre = (b.lo + b.hi) / 2
        else:
            continue
        mids.append((mid, centre))
    tot = sum(m for m, _ in mids)
    if not mids or tot <= 0:
        return None, ask_sum, bid_sum
    return sum(m * c for m, c in mids) / tot, ask_sum, bid_sum


# ----------------------------------------------------------------------------- orchestration
@dataclass
class IsraelReport:
    now: datetime
    budget: float
    estimate: Estimate
    sim: SimResult
    model: ErrorModel
    surplus_pairs: tuple[tuple[str, str], ...]
    party_rows: list[dict[str, Any]]
    blocs: dict[str, float]
    trades: list[Trade]
    all_markets: list[dict[str, Any]]
    unmodelled: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.now.isoformat(),
            "budget": self.budget,
            "polls_used": [{"poll": p.label, "weight": round(w, 4), "seats": p.seats} for p, w in self.estimate.used],
            "poll_shares": self.estimate.shares,
            "error_model": self.model.__dict__,
            "surplus_pairs": self.surplus_pairs,
            "parties": self.party_rows,
            "blocs": self.blocs,
            "trades": [t.to_dict() for t in self.trades],
            "markets": self.all_markets,
            "unmodelled": self.unmodelled,
            "warnings": self.warnings,
        }


def run_israel(
    source,
    polls_path: Path,
    *,
    budget: float,
    now: datetime,
    sims: int = 10000,
    seed: int = 1,
    half_life: float = 7.0,
    window: float = 21.0,
    min_edge: float = 0.03,
    model: ErrorModel | None = None,
    surplus_pairs: Iterable[tuple[str, str]] = DEFAULT_SURPLUS_PAIRS,
    kelly_fraction: float = 0.25,
    max_fraction: float = 0.10,
    maker_margin: float = 0.06,
    min_price: float = 0.05,
    tag: str = "israel-election",
    fee_override: float | None = None,
) -> IsraelReport:
    model = model or ErrorModel()
    pairs = tuple(surplus_pairs)
    polls = load_polls(polls_path)
    est = aggregate(polls, now.date(), half_life, window)
    sim = simulate(est, model, pairs, sims, seed)

    events: list[PolyEvent] = source.poly_events_by_tag(tag)
    classified: list[tuple[PolyEvent, PolyMarket, Bucket]] = []
    unmodelled: list[str] = []
    for ev in events:
        hit = False
        for m in ev.markets:
            b = classify_market(ev, m)
            if b:
                classified.append((ev, m, b))
                hit = True
        if not hit:
            unmodelled.append(ev.title)
    books = source.poly_books([m.yes_token for _, m, _ in classified if m.yes_token]) if classified else {}

    trades: list[Trade] = []
    all_markets: list[dict[str, Any]] = []
    per_party_seats: dict[str, list[tuple[Bucket, PolyMarket, Book | None]]] = {}
    for ev, m, b in classified:
        book = books.get(m.yes_token or "")
        p = model_probability(sim, b)
        rate = fee_override if fee_override is not None else polymarket_rate_for_tags(ev.tags)
        bid, dbid, ask, dask = _best(book, m)
        all_markets.append({"event": ev.title, "label": b.label, "party": b.party, "kind": b.kind, "model_p": round(p, 4), "bid": bid, "ask": ask, "depth_bid": round(dbid), "depth_ask": round(dask), "url": m.url})
        trades.extend(evaluate_market(b, ev, m, book, p, budget=budget, fee_rate=rate, min_edge=min_edge, kelly_fraction=kelly_fraction, max_fraction=max_fraction, maker_margin=maker_margin, min_price=min_price))
        if b.kind == "seats" and "lose seats" not in _norm(ev.title):
            per_party_seats.setdefault(b.party, []).append((b, m, book))
    # takers first (actionable now), then resting quotes; within each, by edge per share so a
    # 5c contract with a 6c edge does not outrank a 40c contract with a 30c edge on leverage
    trades.sort(key=lambda t: (t.action.startswith("post"), -t.edge_per_share))

    party_rows: list[dict[str, Any]] = []
    for p in PARTIES:
        s = sim.seat_summary(p.key)
        implied, ask_sum, bid_sum = implied_seat_mean(per_party_seats.get(p.key, []))
        polled = [pl.seats[p.key] for pl, _ in est.used if p.key in pl.seats]
        party_rows.append({
            "party": p.key, "name": p.name, "bloc": p.bloc,
            "poll_share": round(est.shares[p.key], 4), "poll_seats_avg": round(sum(polled) / len(polled), 1) if polled else None,
            "n_polls": est.n_polls[p.key], "spread_pts": round(est.spread[p.key] * 100, 2),
            "sd_pts": round(model.party_sd(p.key, est.shares[p.key], est.spread[p.key], est.n_polls[p.key]) * 100, 2),
            **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()},
            "p_most": round(sim.p_most_seats(p.key), 4),
            "implied_seats": None if implied is None else round(implied, 1), "ask_sum": round(ask_sum, 2), "bid_sum": round(bid_sum, 2),
        })
    net, chg, arab = BLOCS["netanyahu"], BLOCS["change"], BLOCS["arab"]
    blocs = {
        "netanyahu_bloc_61": sim.p_bloc_at_least(net, 61),
        "change_bloc_61_without_arab": sim.p_bloc_at_least(chg, 61),
        "change_plus_arab_61": sim.p_bloc_at_least(chg + arab, 61),
        "netanyahu_plus_reservists_61": sim.p_bloc_at_least(net + ("reservists",), 61),
    }
    warnings = list(est.warnings)
    if not events:
        warnings.append("no events came back for the tag; nothing to compare against")
    return IsraelReport(now, budget, est, sim, model, pairs, party_rows, blocs, trades, all_markets, unmodelled, warnings)


def render_israel(r: IsraelReport, show_all: bool = False) -> str:
    L: list[str] = []
    days = (ELECTION_DAY - r.now.date()).days
    L.append(f"== Knesset election model vs Polymarket  {r.now.strftime('%Y-%m-%d %H:%MZ')}  {days} days to the vote  budget ${r.budget:.0f} ==")
    for w in r.warnings:
        L.append(f"!! {w}")
    L.append("")
    L.append(f"-- polls in the window ({len(r.estimate.used)}), weight = recency x 1/pollster count --")
    for p, w in r.estimate.used:
        seats = " ".join(f"{k}={v:g}" for k, v in p.seats.items())
        L.append(f"   w={w:.2f}  {p.label:<40} {seats}")
    L.append("")
    L.append(f"-- model, {r.sim.n} simulations; error sd per party in vote-share points; pairs {', '.join(a + '+' + b for a, b in r.surplus_pairs)} --")
    L.append(f"{'party':<30}{'bloc':<10}{'polls':>6}{'share':>7}{'sd':>5}{'seats':>7}{'p5-p95':>9}{'P(seat)':>8}{'P(most)':>8}{'mkt seats':>10}{'asks/bids':>11}")
    for row in r.party_rows:
        implied = "-" if row["implied_seats"] is None else f"{row['implied_seats']:.1f}"
        sums = "-" if row["ask_sum"] == 0 else f"{row['ask_sum']:.2f}/{row['bid_sum']:.2f}"
        L.append(
            f"{row['name']:<30}{row['bloc']:<10}{row['n_polls']:>6}{row['poll_share'] * 100:>6.1f}%{row['sd_pts']:>5.1f}"
            f"{row['mean']:>7.1f}{row['p5']:>4}-{row['p95']:<4}{row['p_seat'] * 100:>7.0f}%{row['p_most'] * 100:>7.0f}%{implied:>10}{sums:>11}"
        )
    L.append("")
    L.append("-- blocs --")
    for k, v in r.blocs.items():
        L.append(f"   {k:<34}{v * 100:>6.1f}%")
    L.append("")
    L.append(f"-- trades with edge >= min after the {r.trades[0].fee_rate * 100:.0f}% taker fee: takers first, then resting quotes, by edge per share --" if r.trades else "-- no trade clears the minimum edge --")
    if r.trades:
        L.append(f"{'action':<13}{'price':>6}{'model':>7}{'edge':>7}{'stake':>7}{'e.profit':>9}  market")
        for t in r.trades:
            L.append(f"{t.action:<13}{t.price:>6.2f}{t.model_p:>7.2f}{t.edge_per_share:>7.3f}{t.stake:>7.0f}{t.exp_profit:>9.2f}  {t.event[:38]} | {t.label} ({PARTY_BY_KEY[t.party].name})")
            L.append(f"    book: bid {t.bid if t.bid is not None else '-'} (${t.depth_bid:.0f})  ask {t.ask if t.ask is not None else '-'} (${t.depth_ask:.0f})  {t.url}")
            for n in t.notes:
                L.append(f"    note: {n}")
    if show_all:
        L.append("")
        L.append("-- every modelled market --")
        for m in sorted(r.all_markets, key=lambda x: (x["event"], x["label"])):
            L.append(f"   {m['model_p']:>6.3f}  bid {m['bid']!s:>6} ask {m['ask']!s:>6}  {m['event'][:40]} | {m['label']}")
    if r.unmodelled:
        L.append("")
        L.append("-- not modelled (coalition/legal questions, or unrecognised): " + "; ".join(r.unmodelled[:12]) + (" ..." if len(r.unmodelled) > 12 else ""))
    L.append("")
    L.append("Stakes are fractional Kelly, capped per market and at the top-of-book depth for takers; maker quotes pay no fee "
             "but may never fill, and their profit is 'if filled'. Replace PLACEHOLDER polls, re-run daily, log each run before any money moves.")
    return "\n".join(L)
