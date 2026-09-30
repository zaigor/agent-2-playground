from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pm_scanner.cli import main
from pm_scanner.flow import FlowAcc, family_flow, hours_bucket, is_first_outcome, price_band, render_family_flow, resolved_markets, score_trades
from pm_scanner.sources import FixtureSource

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=400)


class SyntheticTrades:
    """Informed takers in one family, noise takers in the rest: buys the winner more often than not."""

    def __init__(self, events, informed_prefix: str, seed: int = 1) -> None:
        self.rng = random.Random(seed)
        self.y = {(m.condition_id or m.id): int(m.resolved_yes) for _, m in resolved_markets(events)}
        self.informed_prefix = informed_prefix
        self.title = {(m.condition_id or m.id): e.title for e, m in resolved_markets(events)}
        self.calls = 0

    def trades(self, condition_id: str, closed: bool = True):
        self.calls += 1
        y = self.y[condition_id]
        informed = self.title[condition_id].lower().startswith(self.informed_prefix)
        out = []
        t0 = int(NOW.timestamp()) - 3 * 86400
        for i in range(60):
            p = round(self.rng.uniform(0.2, 0.8), 2)
            # informed takers buy YES when it wins (and NO when it loses) 80% of the time; noise takers 50%
            buys_winner = self.rng.random() < (0.8 if informed else 0.5)
            outcome = "Yes" if (y == 1) == buys_winner else "No"
            out.append({"timestamp": t0 + i * 3600, "price": p if outcome == "Yes" else round(1 - p, 2), "size": 10.0, "side": "BUY", "outcome": outcome})
        return out


def test_buckets_and_accumulator():
    assert hours_bucket(0.5) == "<1h" and hours_bucket(30) == "1-7d" and hours_bucket(1000) == ">7d" and hours_bucket(-1) == "after"
    assert price_band(0.05) == "0.00-0.10" and price_band(0.99) == "0.90-1.00"
    acc = FlowAcc()
    acc.add(10, 0.4, 0.01, +0.6, None)  # a taker bought YES at 0.40 and it won
    acc.add(10, 0.4, 0.01, -0.4, None)  # another bought YES at 0.40 and it lost
    row = acc.row()
    assert row["trades"] == 2 and abs(row["taker_markout_expiry"] - 0.1) < 1e-9 and abs(row["maker_pnl"] + 2.0) < 1e-9
    assert abs(row["maker_pnl_per_100"] + 25.0) < 1e-9  # -2 on $8 notional


def test_score_trades_sides_and_horizon():
    from collections import defaultdict
    sinks, bands, tot = defaultdict(FlowAcc), defaultdict(FlowAcc), FlowAcc()
    trades = [
        {"timestamp": 1000, "price": 0.30, "size": 10, "side": "BUY", "outcome": "Yes"},   # long YES at 0.30
        {"timestamp": 2000, "price": 0.60, "size": 10, "side": "BUY", "outcome": "No"},    # long NO at 0.60 = short YES at 0.40
        {"timestamp": 3000, "price": 0.50, "size": 10, "side": "SELL", "outcome": "Yes"},  # short YES at 0.50
        {"timestamp": 9000, "price": 0.90, "size": 10, "side": "SELL", "outcome": "No"},   # short NO at 0.90 = long YES at 0.10
    ]
    score_trades(trades, 1, 0.05, 1000, sinks, bands, [tot])
    r = tot.row()
    # markouts: +0.70, -0.60, -0.50, +0.90 -> mean +0.125 per share, maker -5.0 in $
    assert r["trades"] == 4 and abs(r["taker_markout_expiry"] - 0.125) < 1e-9 and abs(r["maker_pnl"] + 5.0) < 1e-9
    assert sum(a.n for a in sinks.values()) == 4 and "<1h" in sinks


def test_family_flow_separates_informed_from_noise_families():
    events = FixtureSource(FIXTURES).poly_events_survey((), SINCE, NOW)
    fams = {}
    from pm_scanner.niches import family_key
    for e, m in resolved_markets(events):
        fams.setdefault(family_key(e), []).append(e)
    assert fams, "fixture has resolved markets"
    prefix = sorted(fams, key=lambda k: -len(fams[k]))[0]
    informed_title = fams[prefix][0].title.lower()[:6]
    src = SyntheticTrades(events, informed_title)
    rows = family_flow(events, src, now=NOW, since=SINCE, per_family=5, min_markets=1, max_families=50, include_sport=True)
    assert rows and src.calls <= 5 * len(rows) + 5
    by_key = {r.key: r for r in rows}
    informed = [r for r in rows if r.example.lower().startswith(informed_title)]
    noise = [r for r in rows if not r.example.lower().startswith(informed_title)]
    assert informed and noise
    assert informed[0].total["taker_markout_expiry"] > max(n.total["taker_markout_expiry"] for n in noise) - 0.05
    for r in rows:
        assert abs(sum(b["trades"] for b in r.by_hours) - r.total["trades"]) == 0
        assert abs(sum(b["dollars"] for b in r.by_band) - r.total["dollars"]) < 1e-6
        assert r.family_dollars_per_day >= 0 and r.n_sampled <= 5
    text = render_family_flow(rows, top=10)
    assert "Order flow by family" in text and "Hours-to-close detail" in text and rows[0].key[:10] in text
    assert render_family_flow([], top=5).startswith("no family")
    d = rows[0].to_dict()
    assert d["total"]["maker_pnl_per_100"] is not None


def test_flow_cli_offline(capsys):
    rc = main(["flow", "--fixtures", str(FIXTURES), "--days", "400", "--per-family", "3", "--min-markets", "1"])
    out = capsys.readouterr()
    assert rc == 0 and ("Order flow by family" in out.out or "no family" in out.out)


def test_team_named_outcomes_are_mapped_to_the_first_outcome():
    from collections import defaultdict
    outcomes = ["Yankees", "Red Sox"]
    assert is_first_outcome({"outcome": "Yankees"}, outcomes) and not is_first_outcome({"outcome": "red sox"}, outcomes)
    assert is_first_outcome({"outcome": "Over", "outcomeIndex": 0}, ["Over", "Under"]) and not is_first_outcome({"outcomeIndex": 1}, None)
    assert is_first_outcome({"outcome": "Yes"}, None) and not is_first_outcome({"outcome": "No"}, ["Yes", "No"])
    sinks, bands, tot = defaultdict(FlowAcc), defaultdict(FlowAcc), FlowAcc()
    # Yankees win (y=1). A taker buying Red Sox at 0.40 lost 0.40 a share; buying Yankees at 0.60 gained 0.40.
    trades = [
        {"timestamp": 1000, "price": 0.40, "size": 10, "side": "BUY", "outcome": "Red Sox"},
        {"timestamp": 2000, "price": 0.60, "size": 10, "side": "BUY", "outcome": "Yankees"},
    ]
    score_trades(trades, 1, 0.05, 60, sinks, bands, [tot], outcomes=outcomes)
    r = tot.row()
    assert abs(r["taker_markout_expiry"]) < 1e-9 and abs(r["maker_pnl"]) < 1e-9
    # without the outcome list the Red Sox buy would be misread as a YES buy that won
    sinks2, bands2, tot2 = defaultdict(FlowAcc), defaultdict(FlowAcc), FlowAcc()
    score_trades(trades, 1, 0.05, 60, sinks2, bands2, [tot2])
    assert tot2.row()["taker_markout_expiry"] > 0.4
