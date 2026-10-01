import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pm_scanner.crossings import (
    base_rate,
    decisions,
    load_windows,
    parse_gamma_time,
    phase_of,
    render_crossings,
    run_crossings,
    signal_rows,
)

FIX = Path(__file__).parent / "fixtures" / "crossings_events.json"


def _events():
    return json.loads(FIX.read_text())


def test_parse_gamma_time_formats():
    want = datetime(2026, 9, 7, 10, 12, 58, tzinfo=timezone.utc)
    for s in ("2026-09-07T10:12:58Z", "2026-09-07 10:12:58+00", "2026-09-07 10:12:58.000001+00", "2026-09-07 10:12:58+00:00:00"):
        got = parse_gamma_time(s)
        assert got is not None and abs((got - want).total_seconds()) < 1, s
    assert parse_gamma_time(None) is None and parse_gamma_time("not a time") is None


def test_fixture_windows_crossings_and_staircase():
    ws = load_windows(_events(), "trump-truth-social")
    assert len(ws) == 6 and all(len(w.brackets) == 11 for w in ws)
    assert all(w.winner is not None and w.consistent() for w in ws)
    for w in ws:
        xs = w.crossings()
        assert len(xs) >= 3, w.title
        # closes come in bracket order and every crossed ceiling is below the winner
        assert [x.lo for x in xs] == sorted(x.lo for x in xs)
        assert all(x.hi < w.winner.lo for x in xs)
    # the base rate uses only windows that ended by t
    last = ws[-1]
    assert base_rate(ws, last, last.a, min_windows=3) is not None
    assert base_rate(ws[:1] + [last], last, last.a, min_windows=3) is None


def test_decisions_and_rows_sum_to_one():
    ws = load_windows(_events(), "trump-truth-social")
    decs, skipped = decisions(ws, k=8, min_windows=3)
    assert decs and skipped.get("too few trailing windows", 0) > 0
    for d in decs:
        assert 0.0 < d.frac < 1.0 and d.n_min >= 20 and d.mu_rem >= 0
        assert all(x.closed_at is None or x.closed_at > d.t for x in d.open_brackets)
    rows = signal_rows(decs)
    assert len(rows) == sum(len(d.open_brackets) for d in decs)
    by_dec = {}
    for r in rows:
        by_dec.setdefault((r.note.split("|")[1], r.time), 0.0)
        by_dec[(r.note.split("|")[1], r.time)] += r.p
    assert all(abs(s - 1.0) < 1e-3 for s in by_dec.values()), by_dec


def test_inconsistent_window_is_skipped():
    evs = _events()
    ws = load_windows(evs, "trump-truth-social")
    last = ws[-1]
    above = [x for x in last.brackets if x.lo > last.winner.lo][0]
    bad = copy.deepcopy(evs)
    for m in bad[-1]["markets"] if bad[-1]["id"] == last.event_id else [m for e in bad for m in e["markets"] if e["id"] == last.event_id]:
        if m["conditionId"] == above.condition_id:
            m["closedTime"] = (last.a + (last.b - last.a) / 2).strftime("%Y-%m-%d %H:%M:%S+00")
    ws2 = load_windows(bad, "trump-truth-social")
    assert not [w for w in ws2 if w.event_id == last.event_id][0].consistent()
    _, skipped = decisions(ws2, min_windows=3)
    assert skipped.get("early close above the winner") == 1


class _FakeTrades:
    """Price 1/k until the window starts, drifting to the outcome by the end."""

    def __init__(self, windows):
        self.by_id = {}
        for w in windows:
            k = len(w.brackets)
            for x in w.brackets:
                y = 1.0 if x.resolved_yes else 0.0
                pts = [(w.a - timedelta(hours=2), max(1.0 / k, 0.15)), (w.a + (w.b - w.a) * 0.5, 0.5 / k + 0.5 * y), (w.b - timedelta(minutes=1), 0.02 + 0.96 * y)]
                self.by_id[x.condition_id] = [{"timestamp": int(t.timestamp()), "price": p, "outcome": "Yes", "side": "BUY", "size": 10} for t, p in pts]

    def trades(self, condition_id, closed=True):
        return self.by_id.get(condition_id, [])


def test_run_and_render_with_fake_trades():
    evs = _events()
    ws = load_windows(evs, "trump-truth-social")
    res = run_crossings({"trump-truth-social": evs}, _FakeTrades(ws), min_windows=3, max_stale_hours=24 * 14)
    assert res.windows == 6 and res.decisions > 0 and res.report.rows_scored > 0
    assert [g.name for g in res.by_series] == ["trump-truth-social"]
    assert {g.name for g in res.by_phase} <= {"early (<1/3)", "mid", "late (>2/3)"}
    assert res.lags and res.lags["trump-truth-social"]["n"] >= 3
    text = render_crossings(res)
    assert "Bracket-crossing test" in text and "by series" in text
    assert phase_of(0.1) == "early (<1/3)" and phase_of(0.5) == "mid" and phase_of(0.9) == "late (>2/3)"
    d = res.to_dict()
    assert d["decisions"] == res.decisions and len(d["rows"]) == len(res.rows)
