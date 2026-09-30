from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from pm_scanner.cli import main
from pm_scanner.signal import FixtureResolver, cluster_se, load_signal_csv, parse_time, score_signal
from pm_scanner.weather import FixtureTrades

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_rows():
    """(condition id, mid-tape timestamp, resolution) for every fixture market with a decent tape."""
    events = json.loads((FIXTURES / "weather_events.json").read_text())
    tapes = json.loads((FIXTURES / "weather_trades.json").read_text())
    out = []
    for e in events:
        for m in e["markets"]:
            cid = m.get("conditionId")
            if cid in tapes and len(tapes[cid]) >= 100:
                ts = sorted(int(t["timestamp"]) for t in tapes[cid])
                out.append((cid, ts[len(ts) // 2], int(float(json.loads(m["outcomePrices"])[0]) > 0.5)))
    return out


def _write_csv(path: Path, rows, p_of, extra=None):
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["market", "time", "p", "note"])
        for cid, ts, y in rows:
            w.writerow([cid, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), p_of(cid, ts, y), "t"])
        for r in extra or []:
            w.writerow(r)


def test_parse_time_and_csv_validation(tmp_path):
    assert parse_time("1790380913").year == 2026
    assert parse_time("2026-09-26T14:00:00Z") == datetime(2026, 9, 26, 14, tzinfo=timezone.utc)
    assert parse_time("2026-09-26T16:00:00+02:00") == datetime(2026, 9, 26, 14, tzinfo=timezone.utc)
    p = tmp_path / "s.csv"
    p.write_text("market,time,p\n0xabc,2026-09-26T14:00:00Z,0.4\n0xabc,not a time,0.4\n0xabc,2026-09-26T14:00:00Z,1.4\n")
    rows, problems = load_signal_csv(p)
    assert len(rows) == 1 and len(problems) == 2
    p2 = tmp_path / "bad.csv"
    p2.write_text("foo,bar\n1,2\n")
    rows, problems = load_signal_csv(p2)
    assert not rows and problems and "missing column" in problems[0]


def test_perfect_signal_beats_market_and_market_signal_is_flat(tmp_path):
    rows = _fixture_rows()
    assert len(rows) >= 10
    resolver, trades = FixtureResolver(FIXTURES), FixtureTrades(FIXTURES)
    markets = resolver.resolve([r[0] for r in rows])
    assert len(markets) == len(rows)
    # a signal that knows the outcome: large positive gap, positive paper P&L
    csv_path = tmp_path / "perfect.csv"
    _write_csv(csv_path, rows, lambda cid, ts, y: 0.95 if y else 0.05)
    sig, _ = load_signal_csv(csv_path)
    rep = score_signal(sig, markets, trades, horizon_min=30, edge=0.05)
    assert rep.rows_scored == len(rows) and rep.gap is not None and rep.gap > 0.05 and rep.trades > 0 and rep.pnl_per_100 > 0
    assert rep.blend_gap is not None and rep.blend_gap > 0 and rep.min_detectable_gap is not None
    # a signal equal to the market price: zero gap, no trades
    from pm_scanner.flow import price_at, yes_price_series
    def market_p(cid, ts, y):
        return price_at(yes_price_series(trades.trades(cid), markets[cid].outcomes), ts)
    csv2 = tmp_path / "market.csv"
    _write_csv(csv2, rows, market_p)
    sig2, _ = load_signal_csv(csv2)
    rep2 = score_signal(sig2, markets, trades, horizon_min=30, edge=0.05)
    assert rep2.rows_scored == len(rows) and abs(rep2.gap) < 1e-9 and rep2.trades == 0 and abs(rep2.blend_gap) < 1e-9


def test_lookahead_unknown_and_outcome_flip_are_handled(tmp_path):
    rows = _fixture_rows()[:3]
    resolver, trades = FixtureResolver(FIXTURES), FixtureTrades(FIXTURES)
    markets = resolver.resolve([r[0] for r in rows])
    cid, ts, y = rows[0]
    csv_path = tmp_path / "s.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["market", "time", "p", "outcome"])
        w.writerow([cid, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), 0.5, ""])
        w.writerow([cid, "2030-01-01T00:00:00Z", 0.5, ""])  # after the market closed: look-ahead
        w.writerow(["0x" + "0" * 64, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), 0.5, ""])  # unknown market
        w.writerow([cid, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), 0.9, "No"])  # p refers to NO: becomes 0.1 on YES
    sig, _ = load_signal_csv(csv_path)
    rep = score_signal(sig, markets, trades, horizon_min=30, edge=0.05)
    assert rep.rows_scored == 2 and rep.dropped.get("look-ahead (after close)") == 1 and rep.dropped.get("market not found") == 1
    assert {r.p for r in rep.scored} == {0.5, 0.1}


def test_cluster_se_and_signal_cli(tmp_path, capsys):
    assert cluster_se({"a": (1.0, 2), "b": (1.0, 2)}) == 0.0  # every market at the same mean
    assert cluster_se({"a": (2.0, 2), "b": (0.0, 2)}) > 0
    assert cluster_se({"a": (1.0, 2)}) is None
    rows = _fixture_rows()
    csv_path = tmp_path / "s.csv"
    _write_csv(csv_path, rows, lambda cid, ts, y: 0.5)
    assert main(["signal", "--csv", str(csv_path), "--fixtures", str(FIXTURES), "--json", str(tmp_path / "out.json")]) == 0
    out = capsys.readouterr().out
    assert "Signal backtest" in out and "market - signal" in out and (tmp_path / "out.json").exists()
