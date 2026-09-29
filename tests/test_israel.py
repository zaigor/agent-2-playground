import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pm_scanner import cli
from pm_scanner.israel import (
    DEFAULT_SURPLUS_PAIRS,
    Bucket,
    ErrorModel,
    aggregate,
    allocate_seats,
    classify_market,
    dhondt,
    evaluate_market,
    find_party,
    load_polls,
    parse_seat_label,
    parse_share_label,
    run_israel,
    simulate,
)
from pm_scanner.polymarket import Book, Level, parse_event
from pm_scanner.sources import FixtureSource

FIXTURES = Path(__file__).parent / "fixtures"
POLLS = Path(__file__).parent.parent / "data" / "israel_polls_2026.csv"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def test_party_aliases_resolve_market_titles():
    assert find_party("Israel Election: United Arab List (Ra'am) vote share?").key == "raam"
    assert find_party("Israel Election: RZP-Zehut # of seats?").key == "rzp"
    assert find_party("Reservists and New Economic Party").key == "reservists"
    assert find_party("Israel Election: Dems # of seats?").key == "dems"
    assert find_party("The Democrats").key == "dems"
    assert find_party("Party A") is None


def test_bracket_labels_follow_polymarket_rules():
    assert parse_seat_label("<20") == (None, 19)
    assert parse_seat_label("20-24") == (20, 24)
    assert parse_seat_label("35+") == (35, None)
    assert parse_seat_label("5") == (5, 5)
    # a value on a boundary resolves to the higher bracket -> [lo, hi)
    assert parse_share_label("18-20%") == (0.18, 0.20)
    assert parse_share_label("<16%") == (None, 0.16)
    assert parse_share_label("24%+") == (0.24, None)
    assert parse_share_label("4-4.75%") == (0.04, 0.0475)


def test_dhondt_matches_textbook_example():
    # 50/30/20 for 10 seats -> 5/3/2 ; 100k/80k/30k/20k for 8 seats -> 4/3/1/0
    assert dhondt({"a": 50, "b": 30, "c": 20}, 10) == {"a": 5, "b": 3, "c": 2}
    assert dhondt({"a": 100, "b": 80, "c": 30, "d": 20}, 8) == {"a": 4, "b": 3, "c": 1, "d": 0}


def test_allocation_drops_threshold_misses_and_pairs_share_surplus():
    shares = {"likud": 0.19, "yashar": 0.20, "dems": 0.08, "together": 0.10, "yb": 0.06, "shas": 0.07,
              "utj": 0.06, "rzp": 0.05, "otzma": 0.07, "amcha": 0.03, "joint_list": 0.06, "raam": 0.04,
              "reservists": 0.02, "blue_white": 0.01, "unity": 0.002}
    seats = allocate_seats(shares, DEFAULT_SURPLUS_PAIRS)
    assert sum(seats.values()) == 120
    assert seats["amcha"] == 0 and seats["reservists"] == 0 and seats["blue_white"] == 0  # under 3.25%
    assert seats["yashar"] > seats["likud"] > seats["together"]
    # a pair never does worse in total than the two lists running alone
    alone = allocate_seats(shares, ())
    assert seats["yashar"] + seats["dems"] >= alone["yashar"] + alone["dems"]
    assert sum(alone.values()) == 120


def test_polls_csv_seats_and_percent_cells():
    polls = load_polls(POLLS)
    assert polls[0].date.isoformat() == "2026-09-27"
    full = next(p for p in polls if p.placeholder)
    assert full.seats["yashar"] == 23 and full.shares["reservists"] == pytest.approx(0.031)
    # seat parties share the vote left after the under-threshold lists
    assert 0.97 < sum(full.shares.values()) < 1.03
    partial = next(p for p in polls if p.pollster.startswith("Kan"))
    assert "shas" not in partial.shares  # blank cell = not reported
    est = aggregate(polls, NOW.date())
    assert est.n_polls["yashar"] == 3 and est.n_polls["shas"] == 1
    assert any("PLACEHOLDER" in w for w in est.warnings)
    assert est.shares["yashar"] > est.shares["likud"]


def test_error_sd_scales_with_size_and_accepts_overrides():
    m = ErrorModel()
    tiny, small, big = m.party_sd("unity", 0.003, 0.0, 1), m.party_sd("raam", 0.045, 0.0, 1), m.party_sd("yashar", 0.19, 0.0, 1)
    assert tiny < 0.002 < 0.008 < small < 0.012 < big < 0.02
    assert ErrorModel(sd_overrides={"utj": 0.006}).party_sd("utj", 0.06, 0.02, 3) == 0.006
    # disagreement between pollsters widens the estimate
    assert m.party_sd("likud", 0.17, 0.025, 6) > m.party_sd("likud", 0.17, 0.0, 6)


def test_penny_contracts_are_never_recommended():
    b = Bucket("share", "unity", 0.005, 0.0075, "0.5-0.75%")
    ev, m, book = _mk(0.003, 0.018)
    assert evaluate_market(b, ev, m, book, 0.12, budget=500, fee_rate=0.04) == []
    assert evaluate_market(b, ev, m, book, 0.12, budget=500, fee_rate=0.04, min_price=0.0)  # only the floor stops it


def test_simulation_is_seeded_and_brackets_partition():
    est = aggregate(load_polls(POLLS), NOW.date())
    a = simulate(est, ErrorModel(), n_sims=1500, seed=7)
    b = simulate(est, ErrorModel(), n_sims=1500, seed=7)
    assert a.seats["likud"] == b.seats["likud"]
    total = a.p_seats_between("likud", None, 19) + a.p_seats_between("likud", 20, 24) + a.p_seats_between("likud", 25, 29) + a.p_seats_between("likud", 30, 34) + a.p_seats_between("likud", 35, None)
    assert total == pytest.approx(1.0)
    assert sum(a.p_most_seats(k) for k in a.seats) == pytest.approx(1.0)
    assert 0.4 < a.p_most_seats("yashar") < 1.0
    assert all(sum(a.seats[k][i] for k in a.seats) == 120 for i in range(0, 1500, 100))


def test_classify_fixture_markets():
    events = [parse_event(d) for d in json.loads((FIXTURES / "israel_events.json").read_text())]
    kinds = {}
    for ev in events:
        for m in ev.markets:
            b = classify_market(ev, m)
            if b:
                kinds[b.kind] = kinds.get(b.kind, 0) + 1
    assert kinds["seats"] >= 60 and kinds["share"] >= 60 and kinds["threshold"] == 5 and kinds["most"] >= 5
    lose = next(ev for ev in events if "lose seats" in ev.title.lower())
    b = classify_market(lose, lose.markets[0])
    assert (b.kind, b.party, b.lo, b.hi) == ("seats", "likud", None, 31)
    winner = next(ev for ev in events if "Election Winner" in ev.title)
    labels = {classify_market(winner, m).party for m in winner.markets if classify_market(winner, m)}
    assert {"yashar", "likud", "together", "shas"} <= labels  # placeholder outcomes "Party A" are skipped


def _mk(bid, ask, bid_size=100.0, ask_size=100.0):
    ev = parse_event({"id": "e", "title": "Israel Election: Yashar # of seats?", "slug": "x", "tags": [{"slug": "politics"}],
                      "markets": [{"id": "m", "question": "q", "slug": "m", "groupItemTitle": "24-25", "outcomes": '["Yes","No"]',
                                   "clobTokenIds": '["y","n"]', "outcomePrices": '["0.5","0.5"]', "bestBid": bid, "bestAsk": ask}]})
    book = Book("y", bids=[Level(bid, bid_size)], asks=[Level(ask, ask_size)])
    return ev, ev.markets[0], book


def test_evaluate_market_yes_no_and_maker_sides():
    b = Bucket("seats", "yashar", 24, 25, "24-25")
    ev, m, book = _mk(0.22, 0.28)
    # model says 45%: buy YES at 0.28 (edge 0.17 - fee), and post nothing on the NO side
    trades = evaluate_market(b, ev, m, book, 0.45, budget=500, fee_rate=0.04)
    yes = next(t for t in trades if t.action == "take YES")
    assert yes.edge_per_share == pytest.approx(0.45 - 0.28 - 0.04 * 0.28 * 0.72, abs=1e-6)
    assert yes.stake <= 0.28 * 100  # capped by the $28 resting at the ask
    assert not any(t.action in ("take NO", "post NO bid") for t in trades)
    # model says 5%: buy NO at 0.78 (edge 0.17 - fee); a maker NO bid is offered too
    trades = evaluate_market(b, ev, m, book, 0.05, budget=500, fee_rate=0.04)
    no = next(t for t in trades if t.action == "take NO")
    assert no.price == pytest.approx(0.78) and no.edge_per_share == pytest.approx(0.95 - 0.78 - 0.04 * 0.78 * 0.22, abs=1e-6)
    assert any(t.action == "post NO bid" for t in trades)
    # model agrees with the mid: nothing clears a 3c edge
    assert evaluate_market(b, ev, m, book, 0.25, budget=500, fee_rate=0.04) == []


def test_run_israel_on_fixtures_and_cli(tmp_path, capsys):
    report = run_israel(FixtureSource(FIXTURES), POLLS, budget=500, now=NOW, sims=1500)
    assert len(report.all_markets) > 120
    assert any(r["party"] == "yashar" and r["implied_seats"] for r in report.party_rows)
    assert 0 <= report.blocs["netanyahu_bloc_61"] <= 1
    assert report.trades
    takers = [t for t in report.trades if t.action.startswith("take")]
    makers = [t for t in report.trades if t.action.startswith("post")]
    assert report.trades[: len(takers)] == takers  # takers listed first
    assert all(a.edge_per_share >= b.edge_per_share for a, b in zip(makers, makers[1:]))
    assert all(min(t.price, 1 - t.price) >= 0.05 for t in report.trades)
    out = tmp_path / "r.json"
    rc = cli.main(["israel", "--fixtures", str(FIXTURES), "--polls", str(POLLS), "--sims", "800", "--json", str(out)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "Knesset election model" in text and "PLACEHOLDER" in text and "Yashar" in text
    assert json.loads(out.read_text())["trades"]
