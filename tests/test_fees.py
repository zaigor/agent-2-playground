import math

from pm_scanner.fees import (
    kalshi_maker_fee,
    kalshi_taker_fee,
    polymarket_rate_for_tags,
    polymarket_taker_fee,
)


def test_polymarket_fee_matches_published_per_100_share_caps():
    # $1.00 per 100 shares at 50c for the 0.04 tier, $1.75 for crypto (0.07)
    assert math.isclose(polymarket_taker_fee(0.5, 100, 0.04), 1.00)
    assert math.isclose(polymarket_taker_fee(0.5, 100, 0.07), 1.75)
    assert polymarket_taker_fee(0.5, 100, 0.0) == 0.0
    # fee shrinks toward the extremes
    assert polymarket_taker_fee(0.95, 100, 0.05) < polymarket_taker_fee(0.5, 100, 0.05)


def test_polymarket_rate_from_tags_is_conservative():
    assert polymarket_rate_for_tags(["geopolitics"]) == 0.0
    assert polymarket_rate_for_tags(["geopolitics", "politics"]) == 0.04  # highest matched wins
    assert polymarket_rate_for_tags(["crypto", "bitcoin"]) == 0.07
    assert polymarket_rate_for_tags(["unknown-tag"]) == 0.05
    assert polymarket_rate_for_tags([]) == 0.05


def test_kalshi_fee_formula_and_rounding():
    assert math.isclose(kalshi_taker_fee(0.5, 100), 1.75)  # 0.07 * 100 * 0.25
    assert kalshi_taker_fee(0.42, 1) == 0.02  # 0.017052 rounds up to the cent
    assert math.isclose(kalshi_taker_fee(0.42, 1, round_up=False), 0.07 * 0.42 * 0.58)
    assert math.isclose(kalshi_maker_fee(0.5, 100), 0.44)  # 0.4375 -> 0.44
    assert kalshi_taker_fee(0.5, 100, multiplier=2.0) == 3.5


def test_polymarket_rate_for_event_uses_market_schedule_first():
    from pm_scanner.polymarket import parse_event

    ev = parse_event({"id": "1", "title": "t", "tags": [{"slug": "sports"}], "markets": [
        {"id": "a", "clobTokenIds": '["1","2"]', "feeSchedule": {"rate": 0.03}},
    ]})
    from pm_scanner.fees import polymarket_rate_for_event

    assert polymarket_rate_for_event(ev) == 0.03  # Gamma says 0.03 even though the table says 0.05
    assert polymarket_rate_for_event(ev, override=0.01) == 0.01
