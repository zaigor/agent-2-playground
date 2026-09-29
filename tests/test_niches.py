import json
from datetime import datetime, timezone
from pathlib import Path

from pm_scanner import cli
from pm_scanner.fees import polymarket_rate_for_event
from pm_scanner.niches import Calibration, calibrate, family_key, normalise_title, render_survey, survey
from pm_scanner.polymarket import parse_event, parse_market
from pm_scanner.sources import FixtureSource

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
SINCE = datetime(2025, 9, 1, tzinfo=timezone.utc)


def test_title_normalisation_groups_dated_variants():
    a = normalise_title("Highest temperature in NYC on May 2?")
    b = normalise_title("Highest temperature in NYC on September 26, 2026?")
    assert a == b == "highest temperature in nyc on DATE"
    assert normalise_title("Will Bitcoin hit $120,000 by Friday, 5pm?") == "will bitcoin hit $N by DAY, TIME"


def test_family_key_prefers_series_slug():
    ev = parse_event({"id": "1", "title": "Highest temperature in NYC on May 2?", "seriesSlug": "nyc-daily-weather", "markets": []})
    assert family_key(ev) == "nyc-daily-weather"
    ev2 = parse_event({"id": "2", "title": "Highest temperature in NYC on May 3?", "markets": [], "series": [{"slug": "nyc-daily-weather", "recurrence": "daily"}]})
    assert family_key(ev2) == "nyc-daily-weather" and ev2.recurrence == "daily"
    ev3 = parse_event({"id": "3", "title": "Will X resign by June 30?", "markets": []})
    assert family_key(ev3).startswith("~")


def test_price_day_before_close_from_frozen_fields():
    m = parse_market({"id": "m", "closed": True, "outcomePrices": '["1", "0"]', "clobTokenIds": '["a","b"]', "lastTradePrice": 0.999, "oneDayPriceChange": 0.6245})
    assert m.resolved_yes is True
    assert abs(m.price_day_before_close - 0.3745) < 1e-9
    m2 = parse_market({"id": "m2", "closed": True, "outcomePrices": '["0", "1"]', "clobTokenIds": '["a","b"]', "lastTradePrice": 0.02, "oneDayPriceChange": -0.0295})
    assert m2.resolved_yes is False and abs(m2.price_day_before_close - 0.0495) < 1e-9
    # open markets or missing fields give nothing
    assert parse_market({"id": "m3", "closed": False, "outcomePrices": '["0.4","0.6"]', "clobTokenIds": '["a","b"]'}).resolved_yes is None
    assert parse_market({"id": "m4", "closed": True, "outcomePrices": '["1","0"]', "clobTokenIds": '["a","b"]', "lastTradePrice": 1.0}).price_day_before_close is None
    # impossible reconstruction (change larger than price) is rejected
    assert parse_market({"id": "m5", "closed": True, "outcomePrices": '["1","0"]', "clobTokenIds": '["a","b"]', "lastTradePrice": 0.2, "oneDayPriceChange": 0.5}).price_day_before_close is None


def test_fee_rate_prefers_gamma_schedule_over_tags():
    ev = parse_event({"id": "1", "title": "t", "tags": [{"slug": "geopolitics"}], "markets": [
        {"id": "a", "clobTokenIds": '["1","2"]', "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}},
        {"id": "b", "clobTokenIds": '["3","4"]', "feeSchedule": {"rate": 0.04}},
    ]})
    assert polymarket_rate_for_event(ev) == 0.05  # schedule beats the 0.00 the tag table would give
    assert polymarket_rate_for_event(ev, 0.0) == 0.0  # explicit override still wins
    ev2 = parse_event({"id": "2", "title": "t", "tags": [{"slug": "geopolitics"}], "markets": [{"id": "a", "clobTokenIds": '["1","2"]'}]})
    assert polymarket_rate_for_event(ev2) == 0.0  # no schedule -> tag table


def test_calibration_math_and_small_samples():
    assert calibrate([(0.5, 1)] * 5).n == 5 and calibrate([(0.5, 1)] * 5).brier is None
    # perfectly calibrated coin flips: skill ~0 (Brier equals climatology)
    pairs = [(0.5, 1), (0.5, 0)] * 20
    c = calibrate(pairs)
    assert abs(c.brier - 0.25) < 1e-9 and abs(c.skill) < 1e-9 and c.base_rate == 0.5
    assert c.mid_bias is not None and abs(c.mid_bias) < 1e-9
    # overpriced longshots: priced 0.10, never hit -> longshot bias -0.10
    c2 = calibrate([(0.10, 0)] * 30 + [(0.9, 1)] * 30)
    assert abs(c2.longshot_bias + 0.10) < 1e-9 and c2.skill > 0.5
    assert [b[0] for b in c2.buckets] == [0.1, 0.9]


def test_survey_on_fixture_finds_recurring_families():
    src = FixtureSource(FIXTURES)
    events = src.poly_events_survey(("weather",), SINCE, NOW)
    rows = survey(events, now=NOW, since=SINCE, min_events=3)
    keys = {r.key: r for r in rows}
    assert {"nyc-daily-weather", "tel-aviv-daily-weather", "elon-tweets", "box-office-openings"} <= set(keys)
    nyc = keys["nyc-daily-weather"]
    assert nyc.recurrence == "daily" and nyc.n_events == 10 and nyc.n_open == 1 and nyc.n_markets > 50
    assert nyc.fee_rate == 0.05 and nyc.resolution_sources[0] == "wunderground.com"
    assert nyc.median_lifetime_days is not None and 1.5 < nyc.median_lifetime_days < 3
    assert nyc.calibration.n >= 20 and nyc.calibration.skill is not None
    assert not nyc.sport and 0 < nyc.steadiness <= 1
    # the one-off politics events do not form a family
    assert not any(k.startswith("~") for k in keys)
    text = render_survey(rows, now=NOW, since=SINCE, sort="steady")
    assert "nyc-daily-weather" in text and "elon-tweets" in text
    d = rows[0].to_dict()
    assert "steadiness" in d and "_month_share" not in d and isinstance(d["calibration"], dict)


def test_cli_niches_fixture_writes_json(tmp_path):
    out = tmp_path / "n.json"
    rc = cli.main(["niches", "--fixtures", str(FIXTURES), "--days", "400", "--min-events", "3", "--json", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert any(r["key"] == "nyc-daily-weather" for r in data)
