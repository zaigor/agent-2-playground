import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pm_scanner.cli import run_scan
from pm_scanner.scans import Leg, Opportunity
from pm_scanner.sources import FixtureSource
from pm_scanner.watch import JsonlLog, WatchState, opp_key, render_summary, summarize, watch

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _opp(kind="negrisk_buy_all_yes", url="u1", price=0.40, profit=0.5, edge=0.02):
    return Opportunity(
        kind=kind, platform="polymarket", title="t", url=url,
        cost_per_set=0.95, payout_per_set=1.0, fee_per_set=0.02, edge_per_set=edge, edge_pct=0.02,
        fillable_sets=20, budget_sets=20, est_profit=profit,
        legs=[Leg("polymarket", "A", "BUY YES", price, 30, 0.01)], notes=[], end_date=None, min_order_size=5.0,
    )


def test_opp_key_changes_with_price_but_not_with_size():
    a, b, c = _opp(price=0.40), _opp(price=0.40, profit=0.9), _opp(price=0.41)
    assert opp_key(a) == opp_key(b)
    assert opp_key(a) != opp_key(c)


def test_watch_state_tracks_new_and_gone():
    st = WatchState()
    new, gone = st.update([_opp(url="a"), _opp(url="b")], T0)
    assert [s.opp.url for s in new] == ["a", "b"] and gone == []
    new, gone = st.update([_opp(url="b", profit=0.9)], T0 + timedelta(seconds=30))
    assert new == [] and [s.opp.url for s in gone] == ["a"]
    b = st.active[opp_key(_opp(url="b"))]
    assert b.best_profit == 0.9 and b.scans_seen == 2 and b.first_seen == T0


def test_watch_loop_logs_and_survives_scan_errors(tmp_path):
    log = tmp_path / "w.jsonl"
    clock = iter(T0 + timedelta(seconds=60 * i) for i in range(10))
    results = [
        ([_opp(url="a"), _opp(url="b")], {"n": 1}),
        RuntimeError("proxy 403"),
        ([_opp(url="b")], {"n": 3}),
        ([], {"n": 4}),
    ]

    def scan_fn(now):
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    slept = []
    state = watch(scan_fn, interval=60, log=JsonlLog(log), iterations=4, sleep=slept.append, now_fn=lambda: next(clock), out=open(tmp_path / "out.txt", "w"))
    assert slept == [60, 60, 60]  # no sleep after the final iteration
    assert state.active == {}
    events = [json.loads(l)["event"] for l in log.read_text().splitlines()]
    assert events == ["scan", "new", "new", "error", "scan", "gone", "scan", "gone"]
    gone = [json.loads(l) for l in log.read_text().splitlines() if json.loads(l)["event"] == "gone"]
    # 'a' was first seen at T0 and declared gone at T0+120s (the errored scan at +60s did not count)
    assert gone[0]["duration_s"] == 120.0
    assert gone[1]["duration_s"] == 180.0

    summary = summarize(log)
    assert summary["scans"] == 3 and summary["errors"] == 1
    k = summary["kinds"]["negrisk_buy_all_yes"]
    assert k["seen"] == 2 and k["resolved_sightings"] == 2 and k["median_duration_s"] == 150.0
    assert k["share_lasting_60s_plus"] == 1.0
    text = render_summary(summary)
    assert "negrisk_buy_all_yes" in text and "scans=3" in text


def test_watch_filters_kinds():
    st_log = []

    class L:
        def write(self, rec):
            st_log.append(rec)

    watch(lambda now: ([_opp(kind="near_certain", url="n"), _opp(url="a")], {}), interval=1, log=L(), kinds={"negrisk_buy_all_yes"}, iterations=1, sleep=lambda s: None, now_fn=lambda: T0, out=open("/dev/null", "w"))
    assert [r["event"] for r in st_log] == ["scan", "new"]
    assert st_log[1]["opp"]["kind"] == "negrisk_buy_all_yes"


def test_min_order_size_flags_unexecutable_tickets():
    opps, _ = run_scan(
        FixtureSource(FIXTURES), budget=3.0, min_edge=0.005, max_events=100, platform="polymarket",
        near_min_price=0.95, near_max_days=14, poly_fee_rate=None, kalshi_multiplier=1.0, do_cross=False, now=T0,
    )
    o = next(o for o in opps if o.kind == "negrisk_buy_all_yes")
    assert o.min_order_size == 5.0
    assert o.budget_sets < 5 and not o.executable  # $3 buys ~3 sets, venue wants 5 per leg
    assert any("below the venue minimum" in n for n in o.notes)
    assert o.to_dict()["executable"] is False
