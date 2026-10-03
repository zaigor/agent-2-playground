"""Generic signal backtest: does a series of probabilities you produced beat the market?

Input: a CSV with one row per (market, time) you would have acted on.

  market    condition id (0x...), the market's Polymarket slug, or its numeric Gamma id
  time      when the number was actually available to you: ISO 8601 (UTC unless an offset is
            given) or unix seconds
  p         your probability that the market's first outcome ("Yes", or outcomes[0]) wins
  outcome   optional: the outcome `p` refers to when it is not the first one ("Under", a team)
  note      optional free text carried through to the output

Each row is scored against the market's price at `time` (the last trade at or before it,
rejected when older than `max_stale_hours`) and the market's resolution:

  * Brier of the signal and of the market at that moment, and their difference
    (positive = the signal was better), with a market-clustered standard error;
  * the Brier of a 50/50 blend of signal and market: if the blend beats the market, the
    signal carries information the price did not have, even when the signal alone loses;
  * the markout of the implied trade `horizon_min` later (did the price move your way?);
  * a paper trade at the taker price when |p - price| exceeds `edge` plus the taker fee,
    held to expiry, in P&L per $100 staked;
  * a reliability table of the signal.

Rows with `time` after the market's last trade are look-ahead and are dropped
with a warning; rows on unresolved markets are dropped. Prices come from the same trade
tape the rest of the repo uses (data-api /trades, cached on disk), so any resolved
Polymarket market with trades can be scored.
"""
from __future__ import annotations

import csv
import math
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fees import polymarket_taker_fee
from .flow import is_first_outcome, price_at, yes_price_series
from .http import HttpClient
from .polymarket import GAMMA_URL, PolyMarket, parse_market
from .weather import reliability_table

_HEX = re.compile(r"^0x[0-9a-fA-F]{40,}$")


@dataclass
class SignalRow:
    market: str
    time: datetime
    p: float
    outcome: str = ""
    note: str = ""
    line: int = 0


def parse_time(text: str) -> datetime:
    t = text.strip()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", t):
        return datetime.fromtimestamp(float(t), tz=timezone.utc)
    dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_signal_csv(path: Path) -> tuple[list[SignalRow], list[str]]:
    """Rows and the problems found (bad probabilities, unparseable times, missing columns)."""
    rows: list[SignalRow] = []
    problems: list[str] = []
    with Path(path).open(newline="") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or []}
        for need in ("market", "time", "p"):
            if need not in cols:
                problems.append(f"missing column `{need}` (have: {', '.join(cols) or 'none'})")
        if problems:
            return rows, problems
        for i, rec in enumerate(reader, start=2):
            try:
                p = float(rec[cols["p"]])
            except (TypeError, ValueError):
                problems.append(f"line {i}: p is not a number")
                continue
            if not 0.0 <= p <= 1.0:
                problems.append(f"line {i}: p={p} is outside [0, 1]")
                continue
            try:
                t = parse_time(rec[cols["time"]])
            except (TypeError, ValueError):
                problems.append(f"line {i}: time `{rec[cols['time']]}` is not ISO 8601 or unix seconds")
                continue
            key = (rec[cols["market"]] or "").strip()
            if not key:
                problems.append(f"line {i}: empty market")
                continue
            rows.append(SignalRow(key, t, p, (rec.get(cols.get("outcome", ""), "") or "").strip(), (rec.get(cols.get("note", ""), "") or "").strip(), i))
    return rows, problems


# --------------------------------------------------------------------------- #
# Market lookup
# --------------------------------------------------------------------------- #

class GammaResolver:
    """Finds markets by condition id, numeric id or slug, closed or open."""

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    def resolve(self, keys: list[str]) -> dict[str, PolyMarket]:
        out: dict[str, PolyMarket] = {}
        cids = [k for k in dict.fromkeys(keys) if _HEX.match(k)]
        for closed in (True, False):
            missing = [c for c in cids if c not in out]
            for i in range(0, len(missing), 20):
                params: list[tuple[str, Any]] = [("condition_ids", c) for c in missing[i : i + 20]] + [("limit", 20), ("closed", "true" if closed else "false")]
                for d in self.http.get_json(f"{GAMMA_URL}/markets", params=params) or []:
                    m = parse_market(d)
                    if m.condition_id in missing:
                        out[m.condition_id] = m
        for k in dict.fromkeys(keys):
            if k in out or _HEX.match(k):
                continue
            try:
                if k.isdigit():
                    d = self.http.get_json(f"{GAMMA_URL}/markets/{k}")
                    if isinstance(d, dict) and d.get("id"):
                        out[k] = parse_market(d)
                else:
                    ds = self.http.get_json(f"{GAMMA_URL}/markets", params={"slug": k})
                    if isinstance(ds, list) and ds:
                        out[k] = parse_market(ds[0])
            except Exception:
                continue
        return out


class FixtureResolver:
    """Every market in the fixture directory's event files, by condition id, id and slug."""

    def __init__(self, directory: Path) -> None:
        import json

        self.by_key: dict[str, PolyMarket] = {}
        for name in ("weather_events.json", "gamma_events.json", "niche_events.json", "israel_events.json"):
            path = Path(directory) / name
            if not path.exists():
                continue
            for ev in json.loads(path.read_text()):
                for d in ev.get("markets") or []:
                    m = parse_market(d)
                    for key in (m.condition_id, m.id, m.slug):
                        if key:
                            self.by_key.setdefault(key, m)

    def resolve(self, keys: list[str]) -> dict[str, PolyMarket]:
        return {k: self.by_key[k] for k in keys if k in self.by_key}


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

@dataclass
class ScoredRow:
    market: str
    question: str
    time: str
    p: float
    price: float
    y: int
    brier_signal: float
    brier_market: float
    brier_blend: float
    markout: float | None  # price move in the signal's direction after the horizon, per share
    trade: str  # "YES" / "NO" / ""
    pnl: float  # $ on one share after the taker fee, 0 when no trade
    stake: float  # $ staked on one share (price paid), 0 when no trade
    note: str = ""


def cluster_se(per_market: dict[str, tuple[float, float]]) -> float | None:
    """Standard error of a mean of per-row values when rows cluster by market.
    `per_market` maps market -> (sum of the values, number of rows). Ratio estimator."""
    g = len(per_market)
    n = sum(c for _, c in per_market.values())
    if g < 2 or n == 0:
        return None
    r = sum(s for s, _ in per_market.values()) / n
    ss = sum((s - r * c) ** 2 for s, c in per_market.values())
    return math.sqrt(ss * g / (g - 1)) / n


@dataclass
class SignalReport:
    rows_in: int
    rows_scored: int
    markets: int
    dropped: dict[str, int]
    warnings: list[str]
    brier_signal: float | None
    brier_market: float | None
    brier_blend: float | None
    gap: float | None  # market - signal, positive = signal better
    gap_se: float | None
    blend_gap: float | None  # market - blend
    blend_gap_se: float | None
    markout_mean: float | None
    markout_se: float | None
    trades: int
    pnl_per_100: float | None
    pnl_per_100_se: float | None
    stake_total: float
    reliability: list[dict[str, float]]
    min_detectable_gap: float | None  # 2 standard errors
    scored: list[ScoredRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def score_signal(rows: list[SignalRow], markets: dict[str, PolyMarket], trades_source, *, horizon_min: int = 30, edge: float = 0.05, fee_rate: float | None = None, max_stale_hours: float = 24.0, now: datetime | None = None, keep_rows: bool = True, log=None) -> SignalReport:
    dropped: dict[str, int] = defaultdict(int)
    warnings: list[str] = []
    scored: list[ScoredRow] = []
    by_market: dict[str, list[SignalRow]] = defaultdict(list)
    for r in rows:
        by_market[r.market].append(r)
    import time as _time

    t0 = _time.monotonic()
    fetched = 0
    for i, (key, group) in enumerate(by_market.items(), 1):
        if log and i % 100 == 0:
            el = _time.monotonic() - t0
            left = (len(by_market) - i) * el / i
            log(f"tapes: {i} of {len(by_market)} markets, {fetched} fetched this run (the rest came from the cache), {el / 60:.0f} min so far, about {left / 60:.0f} min left")
        m = markets.get(key)
        if m is None:
            dropped["market not found"] += len(group)
            warnings.append(f"{key}: not found on Gamma")
            continue
        y = m.resolved_yes
        if y is None:
            dropped["unresolved"] += len(group)
            warnings.append(f"{key}: not resolved yet ({m.question[:50]})")
            continue
        cached = getattr(trades_source, "cached", None)
        fetched += 0 if cached is None or cached(m.condition_id or m.id) else 1
        trades = trades_source.trades(m.condition_id or m.id, closed=True)
        series = yes_price_series(trades, m.outcomes)
        if len(series) < 2:
            dropped["no trades"] += len(group)
            warnings.append(f"{key}: no trade history")
            continue
        close_ts = series[-1][0]  # the last print is the practical close; Gamma's endDate is nominal (weather markets trade past it)
        rate = fee_rate if fee_rate is not None else (m.fee_rate if m.fee_rate is not None else 0.05)
        for r in group:
            ts = int(r.time.timestamp())
            if ts > close_ts:
                dropped["look-ahead (after close)"] += 1
                continue
            flip = bool(r.outcome) and not is_first_outcome({"outcome": r.outcome}, m.outcomes)
            p = 1.0 - r.p if flip else r.p
            price = price_at(series, ts)
            if price is None:
                dropped["no price yet"] += 1
                continue
            last_ts = max(t for t, _ in series if t <= ts)
            if ts - last_ts > max_stale_hours * 3600:
                dropped["price too stale"] += 1
                continue
            price_h = price_at(series, ts + horizon_min * 60)
            direction = 1.0 if p > price else -1.0
            markout = None if price_h is None else direction * (price_h - price)
            blend = 0.5 * (p + price)
            trade, pnl, stake = "", 0.0, 0.0
            fee_yes = polymarket_taker_fee(price, 1.0, rate)
            fee_no = polymarket_taker_fee(1.0 - price, 1.0, rate)
            if p - price > edge + fee_yes:
                trade, pnl, stake = "YES", (y - price) - fee_yes, price
            elif price - p > edge + fee_no:
                trade, pnl, stake = "NO", ((1 - y) - (1.0 - price)) - fee_no, 1.0 - price
            scored.append(ScoredRow(key, m.question, r.time.isoformat(timespec="minutes"), round(p, 4), round(price, 4), int(y), (p - y) ** 2, (price - y) ** 2, (blend - y) ** 2, markout, trade, pnl, stake, r.note))
    n = len(scored)
    per: dict[str, dict[str, tuple[float, float]]] = {k: defaultdict(lambda: (0.0, 0.0)) for k in ("gap", "blend", "markout", "pnl")}

    def add(k: str, mk: str, v: float, w: float = 1.0) -> None:
        s, c = per[k][mk]
        per[k][mk] = (s + v, c + w)

    for s in scored:
        add("gap", s.market, s.brier_market - s.brier_signal)
        add("blend", s.market, s.brier_market - s.brier_blend)
        if s.markout is not None:
            add("markout", s.market, s.markout)
        if s.trade:
            add("pnl", s.market, s.pnl * 100.0, s.stake * 100.0)
    mean = lambda k: (sum(v for v, _ in per[k].values()) / sum(c for _, c in per[k].values())) if per[k] and sum(c for _, c in per[k].values()) else None  # noqa: E731
    trades_n = sum(1 for s in scored if s.trade)
    stake_total = sum(s.stake for s in scored)
    pnl_per_100 = (sum(s.pnl for s in scored) / stake_total * 100.0) if stake_total else None
    pnl_se = cluster_se(per["pnl"]) if trades_n else None
    gap_se = cluster_se(per["gap"])
    if log:
        log(f"{n} rows scored on {len({s.market for s in scored})} markets; dropped: {dict(dropped)}")
    return SignalReport(
        rows_in=len(rows), rows_scored=n, markets=len({s.market for s in scored}), dropped=dict(dropped), warnings=warnings[:50],
        brier_signal=statistics.mean(s.brier_signal for s in scored) if n else None,
        brier_market=statistics.mean(s.brier_market for s in scored) if n else None,
        brier_blend=statistics.mean(s.brier_blend for s in scored) if n else None,
        gap=mean("gap"), gap_se=gap_se, blend_gap=mean("blend"), blend_gap_se=cluster_se(per["blend"]),
        markout_mean=mean("markout"), markout_se=cluster_se(per["markout"]),
        trades=trades_n, pnl_per_100=pnl_per_100, pnl_per_100_se=(pnl_se * 100.0 if pnl_se is not None else None), stake_total=stake_total,
        reliability=reliability_table([(s.p, s.y) for s in scored]) if n else [],
        min_detectable_gap=(2.0 * gap_se) if gap_se is not None else None,
        scored=scored if keep_rows else [],
    )


def _pm(x: float | None, se: float | None, f: str = "+.4f") -> str:
    if x is None:
        return "-"
    return f"{x:{f}}" + (f" ± {se:{f.lstrip('+')}}" if se is not None else "")


def render_signal(r: SignalReport, horizon_min: int, edge: float) -> str:
    L = [f"Signal backtest: {r.rows_in} rows in, {r.rows_scored} scored on {r.markets} resolved markets" + (f"; dropped {', '.join(f'{v} {k}' for k, v in r.dropped.items())}" if r.dropped else "") + "."]
    if r.rows_scored == 0:
        L.extend("  " + w for w in r.warnings[:10])
        return "\n".join(L)
    L.append(f"  Brier: signal {r.brier_signal:.4f}, market {r.brier_market:.4f}, 50/50 blend {r.brier_blend:.4f}")
    L.append(f"  market - signal: {_pm(r.gap, r.gap_se)}   (positive = the signal is sharper; ± is one market-clustered se; 2 se = {r.min_detectable_gap:.4f} is the smallest gap this sample could show)")
    L.append(f"  market - blend:  {_pm(r.blend_gap, r.blend_gap_se)}   (positive = the signal adds information the price lacked, even if it loses alone)")
    L.append(f"  markout {horizon_min} min after each row, in the signal's direction: {_pm(r.markout_mean, r.markout_se)} per share")
    if r.trades:
        L.append(f"  paper trades (|p - price| > {edge:.2f} + fee, taker, held to expiry): {r.trades}, ${r.stake_total:.2f} staked per share, P&L {_pm(r.pnl_per_100, r.pnl_per_100_se, '+.2f')} per $100")
    else:
        L.append(f"  paper trades: none (no row was {edge:.2f} + fee away from the price)")
    if r.reliability:
        L.append("  reliability of the signal (bin: rows, mean p, outcome rate):")
        L.append("    " + "  ".join(f"{b['lo']:.2f}-{b['hi']:.2f}: {int(b['n'])}, {b['mean_p']:.2f} -> {b['freq']:.2f}" for b in r.reliability if b.get("n")))
    if r.warnings:
        L.append(f"  warnings ({len(r.warnings)}): " + "; ".join(r.warnings[:5]))
    return "\n".join(L)
