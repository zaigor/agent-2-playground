from __future__ import annotations

from pm_scanner.lp import QuotePlan
from pm_scanner.requote import HALF_TICK, RIG, Fill, Policy, Replay
from pm_scanner.yieldrate import YieldRow, basket, daily_series, evaluate, render, render_basket

DAY = 86400
T0 = 1_791_000_000 - (1_791_000_000 % DAY) + 12 * 3600  # a UTC noon


def _plan(cid="c1", question="A deep calm book", rate=140.0, share=(0.01, 0.03), size=50.0, mid=0.30):
    bid, no_bid = 0.27, 0.72
    return QuotePlan(condition_id=cid, question=question, yes_token="y" + cid, no_token="n" + cid, tick=0.01, size=size, half_spread=0.0225, mid=mid, bid=bid, no_bid=no_bid,
                     collateral=round(size * (bid + no_bid), 2), rate_per_day=rate, reward_low=rate * share[0], reward_high=rate * share[1], days_to_end=400.0, url="", exit_cost=1.0, inside=300.0)


def _fill(ts, side="bid", price=0.27, size=50.0, mark=0.25):
    return Fill(ts, side, price, size, 0.30, 0.30, False, mark_10=mark, mark_60=mark, tick=0.01)


def _replay(policy=RIG, fills=(), *, hours=7 * 24.0, kept=0.8, start=T0):
    return Replay(policy, fills=list(fills), readings=int(hours * 60), score_minutes=kept * 1000.0, ideal_minutes=1000.0, quoting_minutes=hours * 60, span_hours=hours, start_ts=start)


def test_daily_series_pro_rates_the_partial_days_and_groups_the_fills():
    fills = [_fill(T0 + 3600), _fill(T0 + DAY + 3600 * 5)]  # two fills, on the first and second UTC day
    rows = daily_series(fills, start_ts=T0, span_hours=48.0, reward_per_day=2.0, horizon=60)  # noon to noon two days on: three UTC days, half, full, half
    assert [d for d, _r, _l in rows] == [f"{d}" for d in sorted({r[0] for r in rows})] and len(rows) == 3
    assert [round(r, 3) for _d, r, _l in rows] == [1.0, 2.0, 1.0]
    assert [round(l, 2) for _d, _r, l in rows] == [1.5, 1.5, 0.0]  # 50 shares bought at 0.27, undone at 0.25 less a tick: $1.50 each
    assert daily_series(fills, start_ts=T0, span_hours=0.0, reward_per_day=2.0, horizon=60) == []


def test_evaluate_joins_reward_share_kept_and_losses():
    plan = _plan()  # pot $140, share 1..3%: $1.40..$4.20 a day before the kept fraction
    rig = _replay(RIG, [_fill(T0 + DAY * 2)], kept=0.8)
    alt = _replay(HALF_TICK, [_fill(T0 + DAY * 2)], kept=0.98)
    row = evaluate(plan, [alt, rig], horizon=60)
    assert row is not None and row.replay is rig and row.alt is alt and row.kept == 0.8
    assert round(row.reward_low, 2) == 1.12 and round(row.reward_high, 2) == 3.36
    assert round(row.loss_per_day, 4) == round(1.5 / 7, 4) and round(row.fills_per_day, 4) == round(1 / 7, 4)
    assert round(row.net_low, 3) == round(1.12 - 1.5 / 7, 3) and row.parked == 49.5
    assert round(row.pct(row.net_low), 2) == round(100 * row.net_low / 49.5, 2) and round(row.pct(1.0, on=60.0), 4) == round(100 / 60, 4)
    assert len(row.daily) == 8 and row.days_with_fill == 1 and row.worst_day < 0 and row.daily_sd > 0
    assert row.verdict(0.1, 0.5) == "above"  # $0.9..3.1 a day on $49.5 is 1.8..6.3% a day on the model
    assert evaluate(plan, [], horizon=60) is None and evaluate(plan, [_replay(RIG, hours=0.0)], horizon=60) is None


def test_verdicts_follow_the_low_and_high_figures():
    plan = _plan(share=(0.0005, 0.001))  # $0.07..$0.14 a day on $49.50: 0.11..0.23% a day at kept 0.8
    row = evaluate(plan, [_replay(RIG, kept=1.0)], horizon=60)
    assert row.verdict(0.1, 0.5) == "in band"
    assert row.verdict(0.3, 0.5) == "under"
    lossy = evaluate(plan, [_replay(RIG, [_fill(T0 + 3600, mark=0.20)], kept=1.0)], horizon=60)  # one $3.50 fill over the week: $0.50 a day lost
    assert lossy.verdict(0.1, 0.5) == "loses"
    mixed = evaluate(_plan(share=(0.001, 0.01)), [_replay(RIG, [_fill(T0 + 3600, mark=0.25)], kept=1.0)], horizon=60)  # $0.14..1.40 a day against $0.21 lost
    assert mixed.verdict(0.1, 0.5) == "sign unknown"
    assert row.days_to_tell(0.0) is None and lossy.days_to_tell(0.05) > 0


def test_basket_fits_the_budget_and_skips_markets_under_the_band():
    good = evaluate(_plan("a", "good", share=(0.01, 0.02)), [_replay(RIG, kept=1.0)], horizon=60)
    also = evaluate(_plan("b", "also good", share=(0.005, 0.01)), [_replay(RIG, kept=1.0)], horizon=60)
    under = evaluate(_plan("c", "under", share=(0.0001, 0.0002)), [_replay(RIG, kept=1.0)], horizon=60)
    lossy = evaluate(_plan("d", "lossy", share=(0.01, 0.02)), [_replay(RIG, [_fill(T0 + 3600 * k, mark=0.20) for k in range(1, 8)], kept=1.0)], horizon=60)
    rows = [under, lossy, also, good]
    picked = basket(rows, budget=60.0, max_markets=3, lo_pct=0.1, hi_pct=0.5)
    assert [r.plan.condition_id for r in picked] == ["a"]  # the second good one does not fit $60 beside the first ($49.50 each)
    picked = basket(rows, budget=120.0, max_markets=3, lo_pct=0.1, hi_pct=0.5)
    assert [r.plan.condition_id for r in picked] == ["a", "b"]
    assert basket(rows, budget=120.0, max_markets=1, lo_pct=0.1, hi_pct=0.5)[0].plan.condition_id == "a"
    text = render(rows, budget=60.0, lo_pct=0.1, hi_pct=0.5, grain="test", horizon=60)
    assert "in band" not in text.split("verdicts")[0] or "above" in text  # the table carries verdict words
    assert "verdicts on the low figure" in text and "median %/day on parked" in text
    text_b = render_basket(picked, budget=120.0, lo_pct=0.1, hi_pct=0.5, horizon=60)
    assert "basket for $120" in text_b and "good" in text_b
    assert "no market" in render_basket([], budget=60.0, lo_pct=0.1, hi_pct=0.5, horizon=60)
    d = good.to_dict()
    assert d["condition_id"] == "a" and d["kept"] == 1.0 and len(d["daily"]) == 8 and d["pct_low"] > 0


# --------------------------------------------------------------------------- #
# The levers: distance, holding for the pair, the hours
# --------------------------------------------------------------------------- #

from pm_scanner.yieldrate import (  # noqa: E402
    DISTANCE_TICKS, SweepRow, best_window, distance_policies, fills_by_hour, hold_outcomes, render_hold, render_hours, render_sweep, reward_from_replay,
    summarize_hold, window_net,
)


def _series(values, *, start=T0, step=300):
    return [(start + i * step, v) for i, v in enumerate(values)]


def test_distance_policies_and_reward_from_the_rested_score():
    pols = distance_policies()
    assert [p.extra_ticks for p in pols] == list(DISTANCE_TICKS) and pols[2] == RIG and pols[0].label == "rig confirm=3 -2t"
    rep = Replay(RIG, readings=1440, score_minutes=1440 * 5.0, span_hours=24.0, start_ts=T0)  # a mean two-sided score of 5 all day
    lo, hi = reward_from_replay(100.0, rep, (45.0, 95.0))  # competitors 45 (all one-sided) .. 95 (all balanced)
    assert round(lo, 2) == 5.0 and round(hi, 2) == 10.0  # 5/(5+95), 5/(5+45)
    assert reward_from_replay(100.0, Replay(RIG), (1.0, 2.0)) == (0.0, 0.0)
    row = SweepRow(0, Replay(RIG, fills=[_fill(T0, mark=0.25)], readings=1440, score_minutes=1440 * 5.0, span_hours=24.0, start_ts=T0), lo, hi, 49.5)
    assert row.label == "the rig's" and row.net(60) == (5.0 - 1.5, 10.0 - 1.5)


def test_hold_outcomes_read_the_pair_completion_and_the_horizon_mark():
    # a bid fill at 0.27; the mid falls to 0.24, comes back to 0.27 after two hours and to 0.28 after three, then sits at 0.26
    series = _series([0.30] * 3 + [0.24] * 24 + [0.27] * 12 + [0.28] * 12 + [0.26] * 200)  # five-minute bars
    f = _fill(T0 + 600, mark=0.24)  # fills at T0+10min
    one, three, six = (hold_outcomes([f], series, horizon_h=h)[0] for h in (1.0, 3.5, 6.0))
    assert not one.completes_at and not one.completes_through and one.mark == 0.24 and one.loss() == 2.0  # 50 × (0.27 − 0.23)
    assert three.completes_at and three.completes_through  # the 0.28 bars start at T0 + 39 × 5 min = 3h15
    assert six.completes_at and six.mark == 0.26 and six.loss() == 1.0
    late = hold_outcomes([_fill(series[-1][0] - 60, mark=0.26)], series, horizon_h=6.0)[0]
    assert late.censored and late.loss() is None and not late.completes_at  # the series ends before the horizon
    assert hold_outcomes([f], [], horizon_h=1.0) == []
    d = summarize_hold([one], undo_minutes=60)
    assert d["marked"] == 1 and d["complete_at"] == 0 and round(d["ev_hold_through"], 2) == -2.0 and round(d["ev_undo"], 2) == -2.0  # the +60 mark is 0.24 too: 50 × (0.27 − 0.23)
    d6 = summarize_hold([six], undo_minutes=60)
    assert d6["complete_at"] == 1 and round(d6["ev_hold_at"], 2) == 0.5  # the pair completes a tick over the fill: 50 × 0.01
    assert summarize_hold([late])["marked"] == 0


def test_hours_histogram_window_and_the_out_of_sample_split():
    plan = _plan(share=(0.01, 0.02))  # $1.40 a day low reward
    # fills at 14:00 UTC on days 1..4 (in sample) and at 14:00 on day 6 (out of sample), none elsewhere
    day0 = T0 - (T0 % DAY)
    fills = [_fill(day0 + d * DAY + 14 * 3600, mark=0.25) for d in (1, 2, 3, 4, 6)]
    rep = Replay(RIG, fills=fills, readings=7 * 1440, score_minutes=7 * 1440, ideal_minutes=7 * 1440, span_hours=7 * 24.0, start_ts=day0)
    row = evaluate(plan, [rep], horizon=60)
    hist = fills_by_hour([row])
    assert len(hist) == 24 and hist[14] == (14, 5, 7.5) and sum(c for _h, c, _l in hist) == 5
    reward, loss, days = window_net([row], set(range(24)), horizon=60)
    assert round(days, 6) == 7.0 and round(reward, 2) == round(1.4 * 7, 2) and loss == 7.5
    reward_q, loss_q, _ = window_net([row], set(range(0, 12)), horizon=60)  # the quiet half: half the reward, none of the fills
    assert round(reward_q, 2) == round(0.7 * 7, 2) and loss_q == 0.0
    start, r_in, l_in, d_in = best_window([row], length=12, horizon=60, until=day0 + 4 * DAY)
    assert 14 not in {(start + i) % 24 for i in range(12)} and l_in == 0.0 and round(d_in, 6) == 4.0
    r_out, l_out, d_out = window_net([row], {(start + i) % 24 for i in range(12)}, horizon=60, since=day0 + 4 * DAY)
    assert l_out == 0.0 and round(d_out, 6) == 3.0  # the out-of-sample fill at 14h is outside the window too
    text = render_hours([row], horizon=60, length=12, split_days=4.0)
    assert "best 12-hour window" in text and "the same window on the remaining" in text
    row.comp = (10.0, 20.0)
    row.sweeps = [SweepRow(k, Replay(Policy("rig", confirm=3, extra_ticks=k), readings=1440, score_minutes=1440 * (3 - k), span_hours=24.0, start_ts=day0), *reward_from_replay(140.0, Replay(RIG, readings=1440, score_minutes=1440 * (3 - k)), (10.0, 20.0)), 49.5) for k in DISTANCE_TICKS]
    text = render_sweep([row], horizon=60)
    assert "the touch" in text and "a tick out" in text and "mkts +" in text
    assert "no market carried" in render_sweep([evaluate(plan, [rep], horizon=60)], horizon=60)
    text = render_hold([row], horizons=(6.0, 24.0), undo_minutes=60)
    assert "holding a fill for the pair" in text and "no fill has a mark" in text  # the row has no series
