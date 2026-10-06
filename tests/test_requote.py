from __future__ import annotations

from types import SimpleNamespace

from pm_scanner.requote import (
    DEFAULT_POLICIES, ESCAPE_1, FOLLOW, HALF_TICK, RIG, WIDE, Fill, Policy, fetch_mids, market_params, mid_at, render, render_summary, replay, replay_all,
    replay_market, summarize, tape_prints,
)

T0 = 1_791_000_000


def _mids(values, *, start=T0, step=60):
    return [(start + i * step, v) for i, v in enumerate(values)]


def _print(ts, yes_price, size=100.0, outcome="Yes"):
    price = yes_price if outcome == "Yes" else round(1.0 - yes_price, 4)
    return {"timestamp": ts, "price": price, "size": size, "outcome": outcome}


OUT = ["Yes", "No"]


def test_tape_prints_and_mid_at():
    prints = tape_prints([_print(T0 + 5, 0.27, 50, "No"), _print(T0 + 1, 0.30), {"timestamp": "x", "price": 0.5}, {"ts": T0 + 9, "price": 0.31, "size": 1, "outcome": "Yes"}], OUT)
    assert prints == [(T0 + 1, 0.30, 100.0), (T0 + 5, 0.27, 50.0), (T0 + 9, 0.31, 1.0)]  # a NO print at 0.73 is a YES print at 0.27; the lp cache's "ts" key is read too
    series = _mids([0.30, 0.31, 0.32])
    assert mid_at(series, T0 - 1) is None and mid_at(series, T0) == 0.30 and mid_at(series, T0 + 59) == 0.30 and mid_at(series, T0 + 60) == 0.31 and mid_at(series, T0 + 10_000) == 0.32


def test_a_drift_with_warning_fills_the_rig_and_the_escape_rule_steps_away():
    # the mid walks down a tick a minute from 0.50; a print at 0.46 lands between the 0.48 and 0.47 readings
    series = _mids([0.50] * 5 + [0.49, 0.48] + [0.47] * 25)
    prints = tape_prints([_print(T0 + 6 * 60 + 30, 0.46)], OUT)
    rig, follow, escape = (replay(series, prints, policy=p, max_spread_cents=4.5, size=20) for p in (RIG, FOLLOW, ESCAPE_1))
    assert len(rig.fills) == 1 and rig.fills[0].side == "bid" and rig.fills[0].price == 0.47 and rig.fills[0].warned  # the 0.48 reading already showed the approach
    assert rig.fills[0].mid_before == 0.48 and rig.fills[0].quoted_mid == 0.50 and rig.fills[0].mark_10 == 0.47 and rig.fills[0].loss(10) == 0.20  # 20 shares, a tick to cross
    assert follow.fills == [] and escape.fills == []  # both had moved the bid to 0.45 before the print
    assert escape.recentres < follow.recentres  # escape only moves when a quote is threatened
    assert rig.readings == 32 and rig.span_hours > 0.5 and rig.fills_per_day > 0


def test_a_one_second_sweep_fills_every_reading_based_policy_without_warning():
    # 4 Oct 19:56:31: the mid read 0.30 for hours, then one print cleared the book to 0.27 and the mid slid on
    series = _mids([0.30] * 6 + [0.275, 0.265, 0.26, 0.255] + [0.25] * 12 + [0.245, 0.24] * 10)
    prints = tape_prints([_print(T0 + 5 * 60 + 1, 0.27, 3192.0)], OUT)
    reps = replay_all(series, prints, max_spread_cents=4.5, size=50, fill="at")
    by = {r.policy.label: r for r in reps}
    for label in ("rig confirm=3", "rig confirm=3 away=0.5t", "follow confirm=1", "escape confirm=3 guard=1t", "escape confirm=3 guard=2t"):
        f = by[label].fills
        assert len(f) == 1 and f[0].price == 0.27 and f[0].side == "bid" and not f[0].warned and f[0].mid_before == 0.30, label
        assert f[0].mark_10 == 0.25 and f[0].loss(10) == 1.5 and f[0].mark_60 == 0.24 and f[0].loss(60) == 2.0  # 50 shares, undone a tick under the later mid
    assert by["wide confirm=3 +1t"].fills == []  # a bid at 0.26 is not reached by a print at 0.27
    # "through" wants a print strictly under the quote: the same tape then fills nobody
    assert all(r.fills == [] for r in replay_all(series, prints, max_spread_cents=4.5, size=50, fill="through"))


def test_following_a_spike_buys_the_reversion_and_the_rig_holds():
    # the Topuria chase: an 11c one-minute spike in the mid, then a print back at the old price
    series = _mids([0.50] * 5 + [0.61] + [0.50] * 26)
    prints = tape_prints([_print(T0 + 5 * 60 + 30, 0.50)], OUT)  # the reversion prints while the quotes sit at the spike
    rig, follow, escape = (replay(series, prints, policy=p, max_spread_cents=4.5, size=20) for p in (RIG, FOLLOW, ESCAPE_1))
    assert rig.fills == [] and rig.recentres == 0  # one reading away is not three
    assert len(follow.fills) == 1 and follow.fills[0].price == 0.58 and follow.fills[0].side == "bid" and follow.fills[0].loss(10) == 1.80
    assert len(escape.fills) == 1 and escape.fills[0].price == 0.58  # the spike "threatened" the ask, so escape re-centred into it too


def test_a_half_tick_drift_leaves_the_rig_off_centre_and_the_half_tick_rule_follows_it():
    series = _mids([0.30] * 5 + [0.305] * 20 + [0.30] * 20 + [0.305] * 20)
    rig, half = (replay(series, [], policy=p, max_spread_cents=4.5, size=50) for p in (RIG, HALF_TICK))
    assert rig.recentres == 0 and 0.5 < rig.score_share < 0.75  # quotes 0.27/0.33 under a 0.305 mid score a third of centred ones
    assert half.recentres == 3 and half.score_share > 0.95 and half.fills == []


def test_policy_labels_and_the_ask_side_loss():
    assert [p.label for p in DEFAULT_POLICIES] == ["rig confirm=3", "rig confirm=3 away=0.5t", "follow confirm=1", "escape confirm=3 guard=1t", "escape confirm=3 guard=2t", "wide confirm=3 +1t"]
    assert Policy("x", confirm=2, guard_ticks=1.5, extra_ticks=2).label == "x confirm=2 guard=1.5t +2t"
    f = Fill(T0, "ask", 0.53, 20, 0.50, 0.50, False, mark_10=0.56, mark_60=None)
    assert f.loss(10) == 0.80 and f.loss(60) is None  # sold YES at 0.53, buying back at 0.57
    d = f.to_dict()
    assert d["loss_10"] == 0.80 and d["loss_60"] is None and d["side"] == "ask"
    assert WIDE.extra_ticks == 1 and replay([], [], policy=RIG, max_spread_cents=4.5, size=20).readings == 0


def test_summary_and_render_cover_every_policy():
    series = _mids([0.50] * 5 + [0.49, 0.48] + [0.47] * 25)
    prints = tape_prints([_print(T0 + 6 * 60 + 30, 0.46)], OUT)
    rows = [("A", replay_all(series, prints, max_spread_cents=4.5, size=20)), ("B", replay_all(_mids([0.40] * 30), [], max_spread_cents=4.5, size=20))]
    s = summarize(rows)
    assert [d["policy"] for d in s] == [p.label for p in DEFAULT_POLICIES]
    rig = next(d for d in s if d["policy"] == "rig confirm=3")
    assert rig["markets"] == 2 and rig["fills"] == 1 and rig["warned"] == 1 and rig["loss_10_per_market_day"] > 0 and rig["score_share"] > 1  # a drift that brings one side near the mid can out-score two centred quotes
    text = render(rows, title="t") + "\n" + render_summary(s, title="s")
    assert "rig confirm=3" in text and "escape confirm=3 guard=1t" in text and "warned" in text and "A" in text and "B" in text
    assert rows[0][1][0].to_dict()["detail"][0]["warned"] is True


class _Http:
    def __init__(self):
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        return {"history": [{"t": T0 + i * 60, "p": 0.30} for i in range(30)] + [{"t": "bad"}]}


class _Tape:
    def trades(self, cid, closed=True):
        return [_print(T0 + 10 * 60 + 5, 0.26, 50)]


def test_fetch_mids_and_replay_market_read_the_market_record():
    http = _Http()
    assert len(fetch_mids(http, "tok", days=1)) == 30 and http.calls[-1][1] == {"market": "tok", "interval": "1d", "fidelity": 1}
    fetch_mids(http, "tok", days=7)
    assert http.calls[-1][1] == {"market": "tok", "interval": "1w", "fidelity": 5}
    assert len(fetch_mids(http, "tok", days=30)) == 30 and http.calls[-1][1] == {"market": "tok", "interval": "max", "fidelity": 10}
    assert fetch_mids(http, "tok", days=8)[0][0] >= T0 + 29 * 60 - 8 * 86400  # cut to the last `days` days of what came back
    m = SimpleNamespace(raw={"rewardsMaxSpread": 4.5, "rewardsMinSize": 50, "orderPriceMinTickSize": 0.01}, yes_token="tok", condition_id="0xc", outcomes=OUT, question="Q")
    assert market_params(m) == (4.5, 50.0, 0.01)
    got = replay_market(http, _Tape(), m, days=1, fill="through")
    assert got is not None
    name, reps = got
    assert name.startswith("Q  (max spread 4.5c, size 50") and len(reps) == len(DEFAULT_POLICIES)
    assert len(reps[0].fills) == 1 and reps[0].fills[0].price == 0.27 and reps[0].fills[0].size == 50  # a print at 0.26 is through the 0.27 bid
    assert replay_market(http, _Tape(), SimpleNamespace(raw={}, yes_token="t", condition_id="0", outcomes=OUT, question="no programme"), days=1) is None
    assert replay_market(http, _Tape(), m, days=1, size=20)[1][0].fills[0].size == 20
