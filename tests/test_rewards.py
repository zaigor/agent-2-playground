from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pm_scanner.polymarket import Book, Level, parse_market
from pm_scanner.rewards import RewardsReport, book_competition, competitor_total, evaluate_market, order_score, our_share, render_rewards, sample_configs

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def test_order_score_and_share_bounds():
    assert order_score(3.0, 1.0) == ((3 - 1) / 3) ** 2 and order_score(3.0, 0.0) == 1.0 and order_score(3.0, 3.5) == 0.0
    # competitors: 300 score on bids, 100 on asks, mid inside [0.1, 0.9]
    lo, hi = competitor_total(300.0, 100.0, 0.5)
    assert abs(lo - 400 / 6) < 1e-9 and abs(hi - (100 + 200 / 3)) < 1e-9
    s_lo, s_hi = our_share(50.0, 300.0, 100.0, 0.5)
    assert s_lo < s_hi and abs(s_lo - 50 / (50 + hi)) < 1e-9 and abs(s_hi - 50 / (50 + lo)) < 1e-9
    assert competitor_total(300.0, 100.0, 0.95) == (100.0, 100.0)  # outside the band only two-sided liquidity counts
    assert our_share(0.0, 1.0, 1.0, 0.5) == (0.0, 0.0)


def test_book_competition_ignores_far_and_small_levels():
    book = Book("t", bids=[Level(0.49, 100), Level(0.47, 500), Level(0.40, 10000), Level(0.495, 5)], asks=[Level(0.51, 100), Level(0.53, 500)])
    c = book_competition(book, 0.50, 4.5, 20)
    # 0.40 is 10c away (outside), 0.495 is below the min size; 0.49 and 0.47 count on the bid side
    assert abs(c["q_bid"] - (order_score(4.5, 1.0) * 100 + order_score(4.5, 3.0) * 500)) < 1e-9
    assert abs(c["q_ask"] - (order_score(4.5, 1.0) * 100 + order_score(4.5, 3.0) * 500)) < 1e-9
    assert abs(c["depth_bid"] - (100 * 0.49 + 500 * 0.47)) < 1e-9


def _tape(n: int, start: datetime, step_s: int = 600, p: float = 0.50, jump: float = 0.0):
    out = []
    for i in range(n):
        price = p + (jump if i >= n // 2 else 0.0)
        out.append({"timestamp": int((start + timedelta(seconds=i * step_s)).timestamp()), "price": round(price, 3), "size": 50, "side": "BUY", "outcome": "Yes"})
    return out


def test_evaluate_market_reward_and_loss_sides():
    cfg = {"condition_id": "0xabc", "total_daily_rate": 100.0, "rewards_max_spread": 3.0, "rewards_min_size": 20}
    market = parse_market({"id": "1", "question": "Q?", "conditionId": "0xabc", "outcomes": '["Yes", "No"]', "clobTokenIds": '["y", "n"]', "outcomePrices": '["0.5", "0.5"]', "feeSchedule": {"rate": 0.04, "rebateRate": 0.25}, "volume24hr": 1000})
    book = Book("y", bids=[Level(0.49, 200)], asks=[Level(0.51, 200)])
    # a flat tape: no print ever goes through a quote at the touch, so no loss; reward share is set by the book
    row = evaluate_market(cfg, market, book, _tape(200, NOW - timedelta(days=2)), now=NOW, days=14)
    assert row is not None and row.mid == 0.5 and len(row.cases) == 4
    touch = next(c for c in row.cases if c.label == "touch x1")
    assert touch.size == 20 and touch.fills_per_day == 0 and touch.loss_per_day == 0 and 0 < touch.reward_low <= touch.reward_high <= 100
    assert abs(touch.net_low - touch.reward_low) < 1e-9
    # a tape that jumps 10c: the quote at the touch gets picked off and loses on the mark
    row2 = evaluate_market(cfg, market, book, _tape(200, NOW - timedelta(days=2), jump=0.10), now=NOW, days=14)
    touch2 = next(c for c in row2.cases if c.label == "touch x1")
    assert touch2.fills_per_day > 0 and touch2.loss_per_day > 0 and touch2.net_high < touch2.reward_high
    text = render_rewards(RewardsReport(when="t", markets_with_rewards=2, total_daily_rate=200.0, sampled=2, evaluated=2, rows=[row, row2], days=14))
    assert "quote: touch x1" in text and "1-10/day" not in text and ">=100/day" in text


def test_sample_configs_covers_the_tiers():
    cfgs = [{"condition_id": f"0x{i:03d}", "total_daily_rate": r} for i, r in enumerate([500, 200, 50, 20, 15, 5, 2, 1, 1, 0.001])]
    picked = sample_configs(cfgs, top=2, mid_n=2, low_n=2, seed=1)
    rates = sorted((c["total_daily_rate"] for c in picked), reverse=True)
    assert rates[:2] == [500, 200] and len(picked) == 6 and all(1 <= r < 100 for r in rates[2:])


def test_pocket_scan_finds_unclaimed_pots():
    from pm_scanner.rewards import pocket_scan, render_pocket
    cfgs = [
        {"condition_id": "0xa", "total_daily_rate": 50.0, "rewards_max_spread": 4.5, "rewards_min_size": 20},
        {"condition_id": "0xb", "total_daily_rate": 200.0, "rewards_max_spread": 3.0, "rewards_min_size": 100},
    ]
    mk = lambda cid, tok, end: parse_market({"id": cid, "question": f"Q {cid}?", "conditionId": cid, "outcomes": '["Yes", "No"]', "clobTokenIds": f'["{tok}", "n{tok}"]', "outcomePrices": '["0.5", "0.5"]', "endDate": end, "acceptingOrders": True})  # noqa: E731
    markets = {"0xa": mk("0xa", "ta", "2026-12-31T00:00:00Z"), "0xb": mk("0xb", "tb", "2026-10-01T00:00:00Z")}
    books = {
        "ta": Book("ta", bids=[Level(0.10, 30)], asks=[Level(0.40, 30)]),  # nothing within 4.5c of the 0.25 mid: the pot is unclaimed
        "tb": Book("tb", bids=[Level(0.49, 5000)], asks=[Level(0.51, 5000)]),  # deep two-sided book at the touch
    }
    rows = pocket_scan(cfgs, markets, books, now=NOW)
    by = {r.condition_id: r for r in rows}
    assert by["0xa"].share_low == 1.0 and by["0xa"].reward_low == 50.0 and by["0xa"].days_to_end > 7
    assert by["0xb"].share_low < 0.02 and by["0xb"].reward_low < 5.0
    text = render_pocket(rows, top=10)
    assert "no qualifying order within the max spread on either side: 1 markets, $50/day" in text and "Q 0xa?" in text and "Q 0xb?" not in text.split("a live test would start here")[1]
