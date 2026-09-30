"""Order-flow screen: who wins, takers or makers, per recurring market family.

Every trade in a resolved market is scored from the taker's side: the taker's exposure to
YES (buying Yes or selling No is long YES) times (outcome - price). A positive size-weighted
markout means the takers were informed and the makers who filled them lost; a negative one
means the takers were noise and the makers kept the spread. The maker's P&L is the mirror
image before any liquidity rebate, and makers pay no fee. Bucketed by hours to the market's
last trade and by price band, so "where and when is the flow friendly" is visible per family.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .fees import polymarket_rate_for_event
from .niches import family_key, is_sport
from .polymarket import PolyEvent, PolyMarket

HOURS_BUCKETS: tuple[tuple[str, float, float], ...] = (("<1h", 0.0, 1.0), ("1-6h", 1.0, 6.0), ("6-24h", 6.0, 24.0), ("1-7d", 24.0, 168.0), (">7d", 168.0, math.inf))
PRICE_BANDS: tuple[tuple[float, float], ...] = ((0.0, 0.10), (0.10, 0.30), (0.30, 0.70), (0.70, 0.90), (0.90, 1.01))


def hours_bucket(hours: float) -> str:
    for name, lo, hi in HOURS_BUCKETS:
        if lo <= hours < hi:
            return name
    return "after" if hours < 0 else ">7d"


def price_band(p: float) -> str:
    for lo, hi in PRICE_BANDS:
        if lo <= p < hi:
            return f"{lo:.2f}-{min(hi, 1.0):.2f}"
    return "?"


class FlowAcc:
    """Size-weighted taker markouts for one slice of trades."""

    def __init__(self) -> None:
        self.n = 0
        self.shares = 0.0
        self.dollars = 0.0
        self.fees = 0.0
        self.w_exp: list[tuple[float, float]] = []  # (size, taker markout to expiry per share)
        self.w_h: list[tuple[float, float]] = []  # (size, taker markout at the horizon per share)

    def add(self, size: float, p: float, fee: float, mo_exp: float, mo_h: float | None) -> None:
        self.n += 1
        self.shares += size
        self.dollars += size * p
        self.fees += size * fee
        self.w_exp.append((size, mo_exp))
        if mo_h is not None:
            self.w_h.append((size, mo_h))

    @staticmethod
    def _wmean_se(pairs: list[tuple[float, float]]) -> tuple[float | None, float | None]:
        tot = sum(w for w, _ in pairs)
        if not pairs or tot <= 0:
            return None, None
        mean = sum(w * x for w, x in pairs) / tot
        if len(pairs) < 2:
            return mean, None
        se = math.sqrt(sum((w * (x - mean)) ** 2 for w, x in pairs)) / tot
        return mean, se

    def row(self, **keys: Any) -> dict[str, Any]:
        m_exp, se_exp = self._wmean_se(self.w_exp)
        m_h, se_h = self._wmean_se(self.w_h)
        maker = -sum(w * x for w, x in self.w_exp)
        per100 = (maker / self.dollars * 100) if self.dollars else None
        per100_se = (se_exp * self.shares / self.dollars * 100) if (se_exp is not None and self.dollars) else None
        return {**keys, "trades": self.n, "shares": self.shares, "dollars": self.dollars, "taker_fees": self.fees,
                "taker_markout_expiry": m_exp, "taker_markout_expiry_se": se_exp,
                "taker_markout_horizon": m_h, "taker_markout_horizon_se": se_h,
                "maker_pnl": maker, "maker_pnl_per_100": per100, "maker_pnl_per_100_se": per100_se}


def is_first_outcome(trade: dict[str, Any], outcomes: list[str] | None) -> bool:
    """Whether a trade record is on the market's first outcome (the one `resolved_yes` refers to).
    Weather and most binary markets name them Yes/No; sports name them after the teams or
    Over/Under, so the label is matched against the market's own outcome list."""
    idx = trade.get("outcomeIndex")
    if idx in (0, 1):
        return idx == 0
    label = str(trade.get("outcome", "")).strip().lower()
    if outcomes and len(outcomes) >= 2:
        if label == str(outcomes[0]).strip().lower():
            return True
        if label == str(outcomes[1]).strip().lower():
            return False
    return label != "no"


def yes_price_series(trades: list[dict[str, Any]], outcomes: list[str] | None = None) -> list[tuple[int, float]]:
    pts: list[tuple[int, float]] = []
    for t in trades:
        try:
            ts, p = int(t["timestamp"]), float(t["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if not is_first_outcome(t, outcomes):
            p = 1.0 - p
        pts.append((ts, p))
    pts.sort()
    return pts


def price_at(series: list[tuple[int, float]], ts: int) -> float | None:
    last = None
    for t, p in series:
        if t > ts:
            break
        last = p
    return last


def score_trades(trades: list[dict[str, Any]], y: int, fee_rate: float, horizon_s: int, sinks: dict[str, FlowAcc], band_sinks: dict[str, FlowAcc], totals: list[FlowAcc], outcomes: list[str] | None = None) -> None:
    """Add every trade of one resolved market to the hours-to-close, price-band and total sinks.
    `y` is the settled value of the first outcome; `outcomes` names the two sides."""
    series = yes_price_series(trades, outcomes)
    if not series:
        return
    close_ts = series[-1][0]
    for t in trades:
        try:
            ts, price, size = int(t["timestamp"]), float(t["price"]), float(t.get("size") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if size <= 0 or not 0 < price < 1:
            continue
        is_yes = is_first_outcome(t, outcomes)
        p_yes = price if is_yes else 1.0 - price
        direction = 1 if (str(t.get("side", "BUY")).upper() == "BUY") == is_yes else -1
        mo_exp = direction * (y - p_yes)
        p_h = price_at(series, ts + horizon_s)
        mo_h = direction * (p_h - p_yes) if p_h is not None else None
        fee = fee_rate * p_yes * (1 - p_yes)
        hb = hours_bucket((close_ts - ts) / 3600.0)
        for acc in (sinks[hb], band_sinks[price_band(p_yes)], *totals):
            acc.add(size, p_yes, fee, mo_exp, mo_h)


def cluster_se_per_100(per_market: list[tuple[float, float]]) -> float | None:
    """Standard error of the makers' edge per $100 treating each market as one observation
    (ratio estimator: sum of maker P&L over sum of dollars). Trades within a market share the
    outcome, so this is the honest error; the trade-level one is far too small."""
    if len(per_market) < 2:
        return None
    dollars = sum(d for _, d in per_market)
    if dollars <= 0:
        return None
    ratio = sum(p for p, _ in per_market) / dollars
    n = len(per_market)
    resid = sum((p - ratio * d) ** 2 for p, d in per_market)
    return math.sqrt(resid * n / (n - 1)) / dollars * 100


@dataclass
class FamilyFlow:
    key: str
    example: str
    sport: bool
    fee_rate: float
    n_resolved: int  # resolved markets of the family in the window
    n_sampled: int
    family_dollars_per_day: float  # Gamma volume of the family's closed events in the window, per day
    total: dict[str, Any]
    by_hours: list[dict[str, Any]] = field(default_factory=list)
    by_band: list[dict[str, Any]] = field(default_factory=list)
    n_markets_with_trades: int = 0
    maker_pnl_per_100_se_cluster: float | None = None  # market-clustered se of total maker_pnl_per_100
    top_market_share: float | None = None  # share of sampled dollars in the single biggest market
    per_market: list[dict[str, Any]] = field(default_factory=list)  # question, dollars, maker_pnl

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolved_markets(events: list[PolyEvent]) -> list[tuple[PolyEvent, PolyMarket]]:
    out = []
    for e in events:
        if not e.closed:
            continue
        for m in e.markets:
            if m.resolved_yes is not None and (m.condition_id or m.id) and (m.raw.get("volumeNum") or 0) > 0:
                out.append((e, m))
    return out


def sampled_market_ids(report: list[dict[str, Any]] | dict[str, Any]) -> set[str]:
    """Market ids (or questions, for older reports) already used by a previous run, for out-of-sample re-sampling."""
    out: set[str] = set()
    rows = report.get("families", []) if isinstance(report, dict) else report
    for r in rows:
        for x in r.get("per_market", []):
            out.add(str(x.get("market_id") or x.get("question")))
    return out


def family_flow(events: list[PolyEvent], trades_source, *, now: datetime, since: datetime, per_family: int = 30, min_markets: int = 10, max_families: int = 60, horizon_min: int = 30, include_sport: bool = True, seed: int = 7, only_families: set[str] | None = None, exclude_markets: set[str] | None = None, log=None) -> list[FamilyFlow]:
    """Sample resolved markets per family, pull their trades and score the takers' markouts.
    `only_families` restricts the run to those keys; `exclude_markets` (ids or questions from a
    previous report) keeps the sample out of sample."""
    fams: dict[str, list[tuple[PolyEvent, PolyMarket]]] = defaultdict(list)
    for e, m in resolved_markets(events):
        if exclude_markets and (str(m.id) in exclude_markets or m.question in exclude_markets):
            continue
        fams[family_key(e)].append((e, m))
    window_days = max(1.0, (now - since).total_seconds() / 86400)
    ranked = []
    for key, pairs in fams.items():
        if only_families is not None and key not in only_families:
            continue
        if len(pairs) < min_markets:
            continue
        sport = any(is_sport(e) for e, _ in pairs[:5])
        if sport and not include_sport:
            continue
        vol = sum(e.volume for e in {id(e): e for e, _ in pairs}.values())
        ranked.append((vol, key, pairs, sport))
    ranked.sort(key=lambda x: -x[0])
    rng = random.Random(seed)
    out: list[FamilyFlow] = []
    for i, (vol, key, pairs, sport) in enumerate(ranked[:max_families]):
        sample = pairs if len(pairs) <= per_family else rng.sample(pairs, per_family)
        sinks: dict[str, FlowAcc] = defaultdict(FlowAcc)
        bands: dict[str, FlowAcc] = defaultdict(FlowAcc)
        total = FlowAcc()
        fee = max(polymarket_rate_for_event(e) for e, _ in pairs[:20])
        per_market: list[dict[str, Any]] = []
        for e, m in sample:
            trades = trades_source.trades(m.condition_id or m.id)
            one = FlowAcc()
            score_trades(trades, int(m.resolved_yes), fee, horizon_min * 60, sinks, bands, [total, one], outcomes=m.outcomes)
            if one.n:
                r1 = one.row()
                per_market.append({"market_id": str(m.id), "condition_id": m.condition_id, "question": m.question, "end": m.end_date.isoformat() if m.end_date else None, "dollars": r1["dollars"], "maker_pnl": r1["maker_pnl"], "trades": one.n})
        if log:
            log(f"flow {i + 1}/{min(len(ranked), max_families)}: {key} ({len(sample)} markets, {total.n} trades)")
        if total.n == 0:
            continue
        order = {name: j for j, (name, _, _) in enumerate(HOURS_BUCKETS)} | {"after": 99}
        tot_row = total.row()
        out.append(FamilyFlow(
            key=key, example=pairs[-1][0].title, sport=sport, fee_rate=fee, n_resolved=len(pairs), n_sampled=len(sample),
            family_dollars_per_day=vol / window_days, total=tot_row,
            by_hours=[acc.row(bucket=b) for b, acc in sorted(sinks.items(), key=lambda kv: order.get(kv[0], 98))],
            by_band=[acc.row(band=b) for b, acc in sorted(bands.items())],
            n_markets_with_trades=len(per_market),
            maker_pnl_per_100_se_cluster=cluster_se_per_100([(x["maker_pnl"], x["dollars"]) for x in per_market]),
            top_market_share=(max(x["dollars"] for x in per_market) / tot_row["dollars"]) if tot_row["dollars"] else None,
            per_market=per_market,
        ))
    return out


# --------------------------------------------------------------------------- #
# Maker paper test on the tape
# --------------------------------------------------------------------------- #

@dataclass
class MakerConfig:
    half_spread: float = 0.03  # quote at last trade +/- this
    size: float = 20.0  # shares per side per fill
    min_hours: float = 0.0  # quote only while at least this many hours remain before the market's last trade
    max_position: float = 100.0  # net shares cap per market, either side
    fill: str = "through"  # "through": a trade strictly beyond our price fills us; "at": at our price too

    @property
    def label(self) -> str:
        return f"h={self.half_spread:.2f} size={self.size:g} min_h={self.min_hours:g} cap={self.max_position:g} {self.fill}"


def simulate_maker(trades: list[dict[str, Any]], y: float, outcomes: list[str] | None, cfg: MakerConfig) -> dict[str, Any]:
    """Replay one market's tape with a naive two-sided maker.

    After every print at YES price p we rest a bid at p - h and an ask at p + h (clipped to
    1c..99c). A later print at q fills the bid when q < bid (or q <= bid with fill="at") and the
    ask when q > ask, for min(cfg.size, trade size) shares; the quote is then re-centred on q.
    Fills are held to expiry (`y` is the resolution, or a mark-to-market price for an open
    market). This is conservative on price (we never get filled by prints at the touch unless
    asked) and optimistic on queue position (a print through our level is assumed to reach
    us), which is the usual paper-maker bound."""
    series = yes_price_series(trades, outcomes)
    if len(series) < 2:
        return {"fills": 0, "dollars": 0.0, "pnl": 0.0, "position": 0.0, "buys": 0, "sells": 0, "shares": 0.0, "fee_equiv": 0.0, "first_ts": None, "last_ts": None}
    close_ts = series[-1][0]
    sizes = {}
    for t in trades:  # size by (ts, yes-price) so the series and sizes line up
        try:
            ts, price, size = int(t["timestamp"]), float(t["price"]), float(t.get("size") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        p_yes = price if is_first_outcome(t, outcomes) else 1.0 - price
        sizes[(ts, round(p_yes, 4))] = sizes.get((ts, round(p_yes, 4)), 0.0) + size
    bid = ask = None
    position = 0.0
    fills = buys = sells = 0
    dollars = pnl = shares = fee_equiv = 0.0  # fee_equiv = sum of shares * p * (1-p): what the maker-rebate pool is shared by
    for ts, q in series:
        hours_left = (close_ts - ts) / 3600.0
        size = min(cfg.size, sizes.get((ts, round(q, 4)), cfg.size))
        if bid is not None and hours_left >= cfg.min_hours and size > 0:
            hit_bid = q < bid or (cfg.fill == "at" and q <= bid)
            hit_ask = q > ask or (cfg.fill == "at" and q >= ask)
            if hit_bid and position + size <= cfg.max_position:
                position += size
                fills += 1
                buys += 1
                dollars += size * bid
                pnl += size * (y - bid)
                shares += size
                fee_equiv += size * bid * (1.0 - bid)
            elif hit_ask and position - size >= -cfg.max_position:
                position -= size
                fills += 1
                sells += 1
                dollars += size * (1.0 - ask)
                pnl += size * (ask - y)
                shares += size
                fee_equiv += size * ask * (1.0 - ask)
        bid = max(0.01, round(q - cfg.half_spread, 2))
        ask = min(0.99, round(q + cfg.half_spread, 2))
    return {"fills": fills, "dollars": dollars, "pnl": pnl, "position": position, "buys": buys, "sells": sells, "shares": shares, "fee_equiv": fee_equiv, "first_ts": series[0][0], "last_ts": close_ts}


@dataclass
class MakerResult:
    family: str
    config: str
    markets: int
    markets_with_fills: int
    fills: int
    dollars: float
    pnl: float
    per_100: float | None
    per_100_se: float | None  # market-clustered
    dollars_per_day: float  # filled notional per calendar day of the window, for the sampled markets
    markets_positive: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def maker_backtest(events: list[PolyEvent], trades_source, *, now: datetime, since: datetime, families: set[str], configs: list[MakerConfig], per_family: int = 200, seed: int = 7, log=None) -> tuple[list[MakerResult], list[dict[str, Any]]]:
    """Run the paper maker over every resolved market of the given families (up to per_family)."""
    fams: dict[str, list[tuple[PolyEvent, PolyMarket]]] = defaultdict(list)
    for e, m in resolved_markets(events):
        k = family_key(e)
        if k in families:
            fams[k].append((e, m))
    window_days = max(1.0, (now - since).total_seconds() / 86400)
    rng = random.Random(seed)
    results: list[MakerResult] = []
    detail: list[dict[str, Any]] = []
    for key in sorted(fams):
        pairs = fams[key]
        sample = pairs if len(pairs) <= per_family else rng.sample(pairs, per_family)
        tapes = [(m, trades_source.trades(m.condition_id or m.id)) for _, m in sample]
        if log:
            log(f"maker {key}: {len(sample)} markets, {sum(len(t) for _, t in tapes)} trades")
        for cfg in configs:
            per_market = []
            for m, trades in tapes:
                r = simulate_maker(trades, int(m.resolved_yes), m.outcomes, cfg)
                per_market.append((r["pnl"], r["dollars"]))
                detail.append({"family": key, "config": cfg.label, "question": m.question, **r})
            dollars = sum(d for _, d in per_market)
            pnl = sum(p for p, _ in per_market)
            with_fills = [(p, d) for p, d in per_market if d > 0]
            results.append(MakerResult(
                family=key, config=cfg.label, markets=len(sample), markets_with_fills=len(with_fills),
                fills=sum(int(x["fills"]) for x in detail if x["family"] == key and x["config"] == cfg.label),
                dollars=dollars, pnl=pnl, per_100=(pnl / dollars * 100) if dollars else None,
                per_100_se=cluster_se_per_100(with_fills) if len(with_fills) >= 2 else None,
                dollars_per_day=dollars / window_days, markets_positive=sum(1 for p, _ in with_fills if p > 0),
            ))
    return results, detail


def render_maker(results: list[MakerResult]) -> str:
    if not results:
        return "maker paper test: no markets"
    L = ["Maker paper test on the tape: two-sided quotes re-centred on every print, filled only by prints through them, held to expiry.",
         "`per $100` is P&L per $100 of filled notional (± treats each market as one observation); `$/day` is filled notional per day over the sampled markets.", ""]
    L.append(f"  {'family':24} {'config':44} {'mkts':>5} {'filled':>6} {'fills':>6} {'$ filled':>9} {'$/day':>7} {'pnl $':>8} {'per $100 ±se':>16} {'mkts>0':>7}")
    for r in results:
        L.append(f"  {r.family[:24]:24} {r.config[:44]:44} {r.markets:5} {r.markets_with_fills:6} {r.fills:6} {r.dollars:9,.0f} {r.dollars_per_day:7,.0f} {r.pnl:+8,.0f} {_pm(r.per_100, r.per_100_se, '+.2f'):>16} {r.markets_positive:3}/{r.markets_with_fills:<3}")
    return "\n".join(L)


def _pm(x: float | None, se: float | None, f: str) -> str:
    if x is None:
        return "-"
    return f"{x:{f}}" + (f"±{se:{f.lstrip('+')}}" if se is not None else "")


def render_family_flow(rows: list[FamilyFlow], *, top: int = 40, sort: str = "maker") -> str:
    if not rows:
        return "no family had enough resolved markets with trades"
    if sort == "maker":
        rows = sorted(rows, key=lambda r: -(r.total["maker_pnl_per_100"] or -1e9))
    elif sort == "volume":
        rows = sorted(rows, key=lambda r: -r.family_dollars_per_day)
    L = ["Order flow by family: taker markout to expiry per share (positive = takers informed = makers lose) and the makers' edge per $100 filled, before rebates.",
         "The ± on maker/$100 treats each market as one observation (outcomes are shared within a market); `top mkt` is the share of sampled $ in the biggest single market.", ""]
    L.append(f"  {'family':34} {'sport':5} {'fee':>4} {'mkts':>5} {'trades':>7} {'$ sampled':>10} {'family $/day':>12} {'taker→expiry':>12} {'maker/$100 ±se(mkt)':>20} {'top mkt':>7} {'best bucket':>16} {'worst bucket':>16}")
    for r in rows[:top]:
        t = r.total
        hb = [b for b in r.by_hours if b["dollars"] >= 500 and b["maker_pnl_per_100"] is not None]
        best = max(hb, key=lambda b: b["maker_pnl_per_100"]) if hb else None
        worst = min(hb, key=lambda b: b["maker_pnl_per_100"]) if hb else None
        fmt_b = lambda b: f"{b['bucket']} {b['maker_pnl_per_100']:+.1f}" if b else "-"  # noqa: E731
        top_share = f"{r.top_market_share * 100:6.0f}%" if r.top_market_share is not None else f"{'-':>7}"
        L.append(f"  {r.key[:34]:34} {'yes' if r.sport else '':5} {r.fee_rate:4.2f} {r.n_markets_with_trades:5} {t['trades']:7} {t['dollars']:10,.0f} {r.family_dollars_per_day:12,.0f} {t['taker_markout_expiry']:+12.3f} {_pm(t['maker_pnl_per_100'], r.maker_pnl_per_100_se_cluster, '+.2f'):>20} {top_share} {fmt_b(best):>16} {fmt_b(worst):>16}")
    L.append("")
    L.append("Hours-to-close detail (makers' edge per $100, with $ sampled) for the families above:")
    names = [n for n, _, _ in HOURS_BUCKETS]
    L.append(f"  {'family':34} " + " ".join(f"{n:>16}" for n in names))
    for r in rows[:top]:
        cells = {b["bucket"]: b for b in r.by_hours}
        L.append(f"  {r.key[:34]:34} " + " ".join((f"{cells[n]['maker_pnl_per_100']:+6.1f} ${cells[n]['dollars']:>8,.0f}" if n in cells and cells[n]["maker_pnl_per_100"] is not None else f"{'-':>16}") for n in names))
    L.append("")
    L.append("Price-band detail (makers' edge per $100) for the families above:")
    bands = [f"{lo:.2f}-{min(hi, 1.0):.2f}" for lo, hi in PRICE_BANDS]
    L.append(f"  {'family':34} " + " ".join(f"{b:>16}" for b in bands))
    for r in rows[:top]:
        cells = {b["band"]: b for b in r.by_band}
        L.append(f"  {r.key[:34]:34} " + " ".join((f"{cells[b]['maker_pnl_per_100']:+6.1f} ${cells[b]['dollars']:>8,.0f}" if b in cells and cells[b]["maker_pnl_per_100"] is not None else f"{'-':>16}") for b in bands))
    L.append("")
    L.append("Reading: a maker earns the `maker/$100` figure only on the fills it gets, at these prices and times; trades within one market share")
    L.append("the outcome, so the se is optimistic. `family $/day` is Gamma volume of the family's closed events in the window, per day.")
    return "\n".join(L)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
