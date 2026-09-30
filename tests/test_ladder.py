from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pm_scanner.cli import main
from pm_scanner.ladder import Ladder, Rung, build_ladders, check_ladder, classify, confirm_on_books, parse_date_key, parse_number_key, scan_ladders
from pm_scanner.polymarket import Book, Level, parse_event

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def _event(title, markets, neg_risk=False, end="2027-01-01T05:00:00Z", eid="1"):
    return parse_event({
        "id": eid, "title": title, "slug": "e", "negRisk": neg_risk, "endDate": end, "tags": [{"slug": "crypto"}],
        "markets": [
            {
                "id": str(i), "question": q, "groupItemTitle": t, "outcomes": '["Yes", "No"]', "clobTokenIds": f'["y{eid}{i}", "n{eid}{i}"]',
                "bestBid": b, "bestAsk": a, "outcomePrices": '["0.5", "0.5"]', "endDate": mend or end, "acceptingOrders": True, "enableOrderBook": True,
                "feeSchedule": {"rate": 0.04},
            }
            for i, (t, q, b, a, mend) in enumerate(markets)
        ],
    })


def test_number_and_date_parsing():
    assert parse_number_key("↑ $97,500")[0] == 97500.0
    assert parse_number_key("$200M")[0] == 200e6
    assert parse_number_key("7.5+ Wins")[0] == 7.5
    assert parse_number_key("October 1") is None  # months are stripped before numbers are read, and one number is required
    assert parse_number_key("Spread -1.5 (-2.5)") is None
    # a missing year is the one nearest the market's own end date
    d = datetime(2027, 1, 1).date()
    assert parse_date_key("December 31", d)[0] == float(datetime(2026, 12, 31).toordinal())
    assert parse_date_key("March 31", datetime(2027, 4, 1).date())[0] == float(datetime(2027, 3, 31).toordinal())
    assert parse_date_key("by 2027", None)[0] == float(datetime(2027, 12, 31).toordinal())


def test_classify_directions():
    ev = _event("What price will Bitcoin hit in September?", [
        ("↑ 100,000", "Will Bitcoin reach $100,000 in September?", 0.10, 0.12, None),
        ("↑ 90,000", "Will Bitcoin reach $90,000 in September?", 0.40, 0.42, None),
        ("↓ 60,000", "Will Bitcoin dip to $60,000 in September?", 0.05, 0.07, None),
    ])
    kinds = [classify(m, ev) for m in ev.markets]
    assert kinds[0][0] == "number" and kinds[0][3] == -1 and kinds[0][1] == kinds[1][1]
    assert kinds[2][3] == +1 and kinds[2][1] != kinds[0][1]  # the down-arrow rungs form their own ladder
    low = _event("How low will Trump's approval rating go in 2026?", [("35%", "Will Trump's approval rating hit 35% in 2026?", 0.3, 0.32, None), ("30%", "Will Trump's approval rating hit 30% in 2026?", 0.1, 0.12, None)])
    assert classify(low.markets[0], low)[3] == +1  # the event title's "how low" beats the question's "hit"
    by = _event("Gemini 4.0 released by...?", [("September 30", "Gemini 4.0 released by September 30, 2026?", 0.02, 0.03, "2026-10-01T00:00:00Z"), ("October 31", "Gemini 4.0 released by October 31, 2026?", 0.9, 0.92, "2026-11-01T00:00:00Z")])
    c = [classify(m, by) for m in by.markets]
    assert c[0][0] == "date" and c[0][3] == +1 and c[0][2] < c[1][2]
    through = _event("Ceasefire continues through...?", [("September 30", "US x Iran ceasefire continues through September 30?", 0.99, 1.0, "2026-09-30T23:59:00Z"), ("December 31", "US x Iran ceasefire continues through December 31?", 0.3, 0.32, "2026-12-31T23:59:00Z")])
    assert classify(through.markets[0], through)[3] == -1
    neg = _event("OpenAI IPO", [("", "Will OpenAI not IPO by December 31, 2026?", 0.9, 0.92, "2027-01-01T00:00:00Z"), ("", "Will OpenAI not IPO by December 31, 2027?", 0.5, 0.52, "2028-01-01T00:00:00Z")])
    assert classify(neg.markets[0], neg)[3] == -1  # negated question flips the direction
    nolonger = _event("Perim Island no longer under Houthi control by...?", [("October 31", "Perim Island no longer under Houthi control by October 31?", 0.04, 0.05, "2026-11-01T00:00:00Z"), ("December 31", "Perim Island no longer under Houthi control by December 31?", 0.13, 0.16, "2027-01-01T00:00:00Z")])
    assert classify(nolonger.markets[0], nolonger)[3] == +1  # "no longer" is not a negation
    exact = _event("Next Gemini Pro Model released on...?", [("October 2", "Will the next Gemini Pro model be released on October 2, 2026?", 0.01, 0.02, None), ("October 3", "Will the next Gemini Pro model be released on October 3, 2026?", 0.01, 0.02, None)])
    assert classify(exact.markets[0], exact) is None  # exact-day brackets are not rungs
    brackets = _event("Highest temperature in Paris on September 30?", [("24°C", "Will the highest temperature in Paris be 24°C on September 30?", 0.99, 1.0, None), ("23°C", "Will the highest temperature in Paris be 23°C on September 30?", 0.0, 0.001, None)], neg_risk=True)
    assert classify(brackets.markets[0], brackets) is None


def test_over_under_and_spreads():
    d = {
        "id": "9", "title": "Phillies vs. Braves", "slug": "g", "negRisk": False, "endDate": "2026-10-08T00:00:00Z", "tags": [{"slug": "sports"}],
        "markets": [
            {"id": "1", "question": "Phillies vs. Braves: O/U 7.5", "groupItemTitle": "O/U 7.5", "outcomes": '["Over", "Under"]', "clobTokenIds": '["o1", "u1"]', "bestBid": 0.48, "bestAsk": 0.49, "outcomePrices": '["0.5", "0.5"]'},
            {"id": "2", "question": "Phillies vs. Braves: O/U 8.5", "groupItemTitle": "O/U 8.5", "outcomes": '["Over", "Under"]', "clobTokenIds": '["o2", "u2"]', "bestBid": 0.33, "bestAsk": 0.35, "outcomePrices": '["0.5", "0.5"]'},
            {"id": "3", "question": "Braves (-1.5)", "groupItemTitle": "Atlanta Braves (-1.5)", "outcomes": '["Yes", "No"]', "clobTokenIds": '["s1", "t1"]', "bestBid": 0.40, "bestAsk": 0.42, "outcomePrices": '["0.5", "0.5"]'},
            {"id": "4", "question": "Braves (-2.5)", "groupItemTitle": "Atlanta Braves (-2.5)", "outcomes": '["Yes", "No"]', "clobTokenIds": '["s2", "t2"]', "bestBid": 0.20, "bestAsk": 0.22, "outcomePrices": '["0.5", "0.5"]'},
        ],
    }
    ev = parse_event(d)
    ladders = build_ladders([ev])
    kinds = {l.kind: l for l in ladders}
    assert set(kinds) == {"ou", "spread"}
    assert [r.label for r in kinds["ou"].ordered()] == ["O/U 8.5", "O/U 7.5"]  # the higher line is the harder Over
    assert [r.label for r in kinds["spread"].ordered()] == ["Atlanta Braves (-2.5)", "Atlanta Braves (-1.5)"]
    for lad in ladders:
        vs, gaps = check_ladder(lad)
        assert not vs and all(g < 0 for g in gaps)


def test_violation_detection_confirmation_and_cross_event():
    ev = _event("Bitcoin above ___ on October 1?", [
        ("74,000", "Will Bitcoin be above $74,000 on October 1?", 0.60, 0.62, None),
        ("76,000", "Will Bitcoin be above $76,000 on October 1?", 0.65, 0.67, None),  # priced above the easier rung: violation
        ("78,000", "Will Bitcoin be above $78,000 on October 1?", 0.30, 0.32, None),
    ])
    ladders = build_ladders([ev])
    assert len(ladders) == 1 and [r.label for r in ladders[0].ordered()] == ["78,000", "76,000", "74,000"]
    vs, gaps = check_ladder(ladders[0])
    hard = [v for v in vs if v.kind == "hard"]
    assert len(hard) == 1 and hard[0].subset == "76,000" and hard[0].superset == "74,000" and abs(hard[0].gap - 0.03) < 1e-9
    assert hard[0].fee_per_set > 0 and hard[0].edge_per_set < hard[0].gap
    # confirmation on books: the live bid is lower and only 40 shares deep
    books = {"y11": Book("y11", bids=[Level(0.64, 40)], asks=[Level(0.67, 100)]), "y10": Book("y10", bids=[Level(0.60, 500)], asks=[Level(0.62, 500)])}
    rungs = {r.label: r for r in ladders[0].rungs}
    c = confirm_on_books(hard[0], books, rungs["76,000"], rungs["74,000"], budget=1000.0)
    assert c is not None and abs(c.gap - 0.02) < 1e-9 and c.fillable_sets == 40 and c.budget_sets == 40 and abs(c.est_profit - 40 * c.edge_per_set) < 0.01
    rep = scan_ladders([ev], lambda toks: books, now=NOW, budget=1000.0)
    assert len(rep.hard) == 1 and rep.hard[0].days_to_resolve is not None and rep.hard[0].annualized is not None
    # the same question across two events forms a cross-event date ladder
    e1 = _event("Will Concrete launch a token by December 31, 2026?", [("", "Will Concrete launch a token by December 31, 2026?", 0.99, 1.0, "2027-01-01T00:00:00Z")], eid="21")
    e2 = _event("Will Concrete launch a token by June 30, 2027?", [("", "Will Concrete launch a token by June 30, 2027?", 0.90, 0.94, "2027-07-01T00:00:00Z")], eid="22")
    cross = [l for l in build_ladders([e1, e2]) if l.cross_event]
    assert len(cross) == 1 and cross[0].direction == +1
    vs, _ = check_ladder(cross[0])
    assert [v.kind for v in vs] == ["hard"]  # 0.99 bid on the earlier date vs 0.94 ask on the later one


def test_ladder_cli_offline(capsys):
    assert main(["ladder", "--fixtures", str(FIXTURES), "--max-events", "500"]) == 0
    out = capsys.readouterr().out
    assert "Ladder consistency" in out
