from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pm_scanner.paper import Candidate, append_jsonl, classify, group_of, markets_in_window, read_jsonl, render_score, render_snapshot, score, snapshot
from pm_scanner.polymarket import Book, Level, parse_event

NOW = datetime(2026, 10, 7, 0, 5, tzinfo=timezone.utc)


def _event(title, tags, series, markets, end_hours=30.0):
    end = (NOW + timedelta(hours=end_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return parse_event({
        "id": title, "title": title, "slug": title.lower().replace(" ", "-"), "tags": [{"slug": t} for t in tags], "series": [{"slug": series}] if series else [], "closed": False, "endDate": end,
        "markets": [{"id": f"{title}-{i}", "question": q, "conditionId": f"0x{title}{i}", "clobTokenIds": json.dumps([f"y{title}{i}", f"n{title}{i}"]), "outcomes": json.dumps(["Yes", "No"]), "outcomePrices": json.dumps(["0.5", "0.5"]),
                     "endDate": end, "closed": False, "acceptingOrders": True, "enableOrderBook": True, "volumeNum": vol, "feeSchedule": {"rate": 0.04}} for i, (q, vol) in enumerate(markets)],
    })


def _book(token, bid, ask, bid_size=500.0, ask_size=400.0):
    return Book(token_id=token, bids=[Level(bid, bid_size)], asks=[Level(ask, ask_size)])


def test_group_of_names_the_ladders_and_drops_the_up_or_down_series():
    assert group_of(_event("hit", ["crypto", "bitcoin"], "bitcoin-hit-price-weekly", [("Will Bitcoin reach $80,000 Oct 5-11?", 1000)])) == "touch"
    assert group_of(_event("strike", ["crypto"], "btc-multi-strikes-weekly", [("above", 1)])) == "crypto"
    assert group_of(_event("aapl", ["equities", "stocks"], "apple-multi-strikes-weekly", [("above", 1)])) == "finance"
    assert group_of(_event("iran", ["politics", "iran"], "", [("x", 1)])) == "politics"
    assert group_of(_event("tweets", ["mention-markets", "politics"], "elon-tweets", [("x", 1)])) == "other"
    assert group_of(_event("nyc", ["weather"], "nyc-daily-weather", [("x", 1)])) == "weather"
    assert group_of(_event("game", ["sports", "nba"], "nba", [("x", 1)])) == "sports"
    assert group_of(_event("up", ["crypto", "up-or-down"], "btc-up-or-down-5m", [("x", 1)])) is None


def test_classify_puts_only_touch_weeklies_and_monthlies_in_the_buy_leg_and_only_the_three_groups_in_the_sell_leg():
    assert classify("touch", "bitcoin-hit-price-weekly", 0.05, 0.04, 0.06) == ("buy_touch", "YES")
    assert classify("touch", "bitcoin-hit-price-monthly", 0.15, 0.14, 0.16) == ("buy_touch_wide", "YES")
    assert classify("touch", "bitcoin-hit-price-daily", 0.05, 0.04, 0.06) == ("record", "YES")  # the daily ladders were not in the backtest's reading
    assert classify("touch", "bitcoin-hit-price-weekly", 0.97, 0.96, 0.98) == ("record", "NO")  # a near-certain rung's NO is not a barrier bet
    assert classify("politics", "", 0.03, 0.02, 0.04) == ("sell_tail", "YES")
    assert classify("finance", "apple-multi-strikes-weekly", 0.97, 0.96, 0.98) == ("sell_tail", "NO")  # the cheap side is NO at 3c
    assert classify("other", "elon-tweets", 0.06, 0.05, 0.07) == ("record", "YES")  # 6c is outside the registered 2-5c
    assert classify("crypto", "btc-multi-strikes-weekly", 0.03, 0.02, 0.04) == ("record", "YES")  # not a registered group for selling
    assert classify("weather", "nyc-daily-weather", 0.03, 0.02, 0.04) == ("record", "YES")
    assert classify("politics", "", None, None, None) == ("record", "YES")
    assert classify("politics", "", 0.03, 0.005, 0.055) == ("record", "YES")  # a bid under a cent is not a sale
    assert classify("finance", "apple-multi-strikes-weekly", 0.97, 0.94, 0.995) == ("record", "NO")  # NO's bid is 0.5c
    assert classify("touch", "bitcoin-hit-price-weekly", 0.09, 0.01, 0.17) == ("record", "YES")  # an ask 8c over a 9c mid: no book to buy
    assert classify("touch", "bitcoin-hit-price-weekly", 0.09, 0.05, 0.13) == ("buy_touch", "YES")  # 4c over: bought at 13c


def test_snapshot_reads_the_window_the_book_and_sizes_the_paper_fill_to_depth():
    touch = _event("hit", ["crypto", "bitcoin"], "bitcoin-hit-price-weekly", [("Will Bitcoin reach $80,000 Oct 5-11?", 50000), ("Will Bitcoin reach $90,000 Oct 5-11?", 20000)], end_hours=28)
    pol = _event("iran", ["politics"], "", [("Iran strikes shipping by Oct 8?", 9000)], end_hours=40)
    aapl = _event("aapl", ["equities"], "apple-multi-strikes-weekly", [("AAPL above $400 on Oct 8?", 3000)], end_hours=47.9)
    late = _event("late", ["politics"], "", [("too far out", 5000)], end_hours=60)
    soon = _event("soon", ["politics"], "", [("too soon", 5000)], end_hours=20)
    books = {"yhit0": _book("yhit0", 0.04, 0.06, ask_size=250.0), "yhit1": _book("yhit1", 0.005, 0.01), "yiran0": _book("yiran0", 0.02, 0.04, bid_size=1000.0), "yaapl0": _book("yaapl0", 0.96, 0.98, ask_size=30.0)}
    asked = []
    def get_books(ids):
        asked.append(sorted(ids)); return {t: books[t] for t in ids if t in books}
    rows = snapshot([touch, pol, aapl, late, soon], get_books, NOW, buy_stake=20.0, sell_capital=100.0)
    assert asked == [sorted(["yhit0", "yhit1", "yiran0", "yaapl0"])]  # the window keeps 24-48h only
    by = {r.market_id: r for r in rows}
    b = by["hit-0"]
    assert b.leg == "buy_touch" and b.side == "YES" and b.q == 0.05 and b.price == 0.06 and b.depth == 250.0 and b.shares == 250.0 and b.dollars == 15.0  # $20 wanted 333 shares, the ask held 250
    assert by["hit-1"].leg == "record" and by["hit-1"].q == 0.0075  # under 2c: recorded, not bought (a touch rung under 50c stays in the record)
    s = by["iran-0"]
    assert s.leg == "sell_tail" and s.side == "YES" and s.price == 0.02 and s.depth == 1000.0 and round(s.shares, 1) == 102.0 and round(s.dollars, 2) == 100.0  # $100 of capital at 98c a share
    a = by["aapl-0"]
    assert a.leg == "sell_tail" and a.side == "NO" and a.q == 0.03 and a.price == 0.02 and a.depth == 30.0 and a.shares == 30.0  # NO's bid is 1 - YES ask; its depth the YES ask's size
    assert [r.leg for r in rows] == ["buy_touch", "sell_tail", "sell_tail", "record"]  # the weather and sports of the window are not written
    text = render_snapshot(rows, NOW)
    assert "BUY the touch rung" in text and "Will Bitcoin reach $80,000" in text and "SELL the tail" in text and "too far out" not in text


def test_markets_in_window_skips_closed_and_unbookable_markets():
    ev = _event("x", ["politics"], "", [("a", 1), ("b", 1)], end_hours=30)
    ev.markets[1].closed = True
    assert [m.id for _, m, _ in markets_in_window([ev], NOW)] == ["x-0"]
    assert markets_in_window([ev], NOW, min_volume=10) == []


def test_score_pays_a_buy_that_came_in_and_a_sell_that_died_after_the_fee(tmp_path):
    rows = [
        Candidate(snapshot=NOW.isoformat(), market_id="1", condition_id="c1", question="reach", series="bitcoin-hit-price-weekly", group="touch", leg="buy_touch", end="", hours_to_end=28, fee_rate=0.04, yes_token="y1", yes_bid=0.04, yes_ask=0.06, yes_bid_size=1, yes_ask_size=1, mid=0.05, side="YES", q=0.05, price=0.06, depth=1000, shares=100.0, dollars=6.0, volume=1),
        Candidate(snapshot=NOW.isoformat(), market_id="2", condition_id="c2", question="reach2", series="bitcoin-hit-price-weekly", group="touch", leg="buy_touch", end="", hours_to_end=28, fee_rate=0.04, yes_token="y2", yes_bid=0.04, yes_ask=0.06, yes_bid_size=1, yes_ask_size=1, mid=0.05, side="YES", q=0.05, price=0.06, depth=1000, shares=100.0, dollars=6.0, volume=1),
        Candidate(snapshot=NOW.isoformat(), market_id="3", condition_id="c3", question="tail", series="", group="politics", leg="sell_tail", end="", hours_to_end=40, fee_rate=0.04, yes_token="y3", yes_bid=0.02, yes_ask=0.04, yes_bid_size=1, yes_ask_size=1, mid=0.03, side="YES", q=0.03, price=0.02, depth=1000, shares=100.0, dollars=98.0, volume=1),
        Candidate(snapshot=NOW.isoformat(), market_id="4", condition_id="c4", question="tail no", series="", group="finance", leg="sell_tail", end="", hours_to_end=40, fee_rate=0.04, yes_token="y4", yes_bid=0.96, yes_ask=0.98, yes_bid_size=1, yes_ask_size=1, mid=0.97, side="NO", q=0.03, price=0.02, depth=1000, shares=100.0, dollars=98.0, volume=1),
        Candidate(snapshot=NOW.isoformat(), market_id="5", condition_id="c5", question="open", series="", group="politics", leg="sell_tail", end="", hours_to_end=40, fee_rate=0.04, yes_token="y5", yes_bid=0.02, yes_ask=0.04, yes_bid_size=1, yes_ask_size=1, mid=0.03, side="YES", q=0.03, price=0.02, depth=1000, shares=100.0, dollars=98.0, volume=1),
        Candidate(snapshot=NOW.isoformat(), market_id="6", condition_id="c6", question="rec", series="", group="weather", leg="record", end="", hours_to_end=40, fee_rate=0.04, yes_token="y6", yes_bid=0.02, yes_ask=0.04, yes_bid_size=1, yes_ask_size=1, mid=0.03, side="YES", q=0.03, price=None, depth=0, shares=0, dollars=0, volume=1),
    ]
    path = tmp_path / "snapshots.jsonl"
    assert append_jsonl(path, rows) == 6 and len(read_jsonl(path)) == 6
    dup = Candidate(**{**rows[0].to_dict(), "snapshot": (NOW + timedelta(hours=1)).isoformat()})  # the same market read again the same night
    assert append_jsonl(path, [dup]) == 1
    res = {"c1": {"y": 1}, "c2": {"y": 0}, "c3": {"y": 0}, "c4": {"y": 0}}  # c5 still open; c4's NO came in (YES resolved 0), so that sell lost
    sc = score(read_jsonl(path), res)
    b = sc["buy_touch"]
    fee = 0.04 * 0.06 * 0.94 * 100
    assert b["positions"] == 2 and b["resolved"] == 2 and b["hits"] == 1 and b["mean_price"] == 0.06  # the second reading of market 1 is not a third position
    assert abs(b["pnl"] - (100 * (1 - 0.06) - fee + 100 * (0 - 0.06) - fee)) < 1e-9 and abs(b["dollars"] - 12.0) < 1e-9
    s = sc["sell_tail"]
    fee_s = 0.04 * 0.02 * 0.98 * 100
    assert s["positions"] == 3 and s["resolved"] == 2 and s["hits"] == 1
    assert abs(s["pnl"] - ((100 * 0.02 - fee_s) + (100 * (0.02 - 1.0) - fee_s))) < 1e-9 and abs(s["dollars"] - 196.0) < 1e-9
    assert s["groups"]["politics"]["resolved"] == 1 and s["groups"]["finance"]["hits"] == 1 and "2026-W41" in s["weeks"]
    assert "record" not in sc
    text = render_score(sc, 7, 2)
    assert "buy_touch: 2 positions, 2 resolved, 1 came in" in text and "sell_tail: 3 positions, 2 resolved, 1 came in" in text and "buy_touch_wide: no positions yet" in text
