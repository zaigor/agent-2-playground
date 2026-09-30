import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from pm_scanner import cli
from pm_scanner.polymarket import Book, Level, parse_event
from pm_scanner.sources import FixtureSource
from pm_scanner.weather import (
    STATIONS,
    Calib,
    FixtureTrades,
    OpenMeteo,
    actual_temperature,
    backtest,
    render_backtest,
    bracket_markets,
    bracket_probability,
    city_of,
    cutoff_utc,
    group_by_event,
    parse_bracket,
    parse_station_overrides,
    price_at,
    summarise_pairs,
    target_date,
    today,
    trend,
    yes_price_series,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_bracket_parsing_covers_every_market_shape():
    assert parse_bracket("Will the highest temperature in New York City be between 64-65°F on September 26?") == (64, 65, "F")
    assert parse_bracket("Will the highest temperature in New York City be 63°F or below on September 26?") == (None, 63, "F")
    assert parse_bracket("Will the highest temperature in New York City be 82°F or higher on September 26?") == (82, None, "F")
    assert parse_bracket("Will the highest temperature in Tel Aviv be 28°C on September 30?") == (28, 28, "C")
    assert parse_bracket("Will the lowest temperature in Helsinki be between -5--4°C on January 3?") == (-5, -4, "C")
    assert parse_bracket("Will the lowest temperature in Helsinki be -9°C or below on January 3?") == (None, -9, "C")
    assert parse_bracket("Will it rain in Seattle?") is None


def test_city_and_target_date():
    assert city_of("nyc-daily-weather") == ("nyc", "high")
    assert city_of("tel-aviv-daily-lowest-temperature") == ("tel-aviv", "low")
    assert city_of("elon-tweets") is None
    ev = parse_event({"id": "1", "title": "Highest temperature in NYC on September 26?", "endDate": "2026-09-26T12:00:00Z", "markets": []})
    assert target_date(ev) == date(2026, 9, 26)
    ev = parse_event({"id": "2", "title": "Highest temperature in Seoul on January 1?", "createdAt": "2025-12-30T10:00:00Z", "markets": []})
    assert target_date(ev) == date(2026, 1, 1)


def test_cutoff_is_local_midnight_in_utc():
    assert cutoff_utc(STATIONS["nyc"], date(2026, 9, 26), 0) == datetime(2026, 9, 26, 4, 0, tzinfo=timezone.utc)  # EDT
    assert cutoff_utc(STATIONS["tel-aviv"], date(2026, 9, 27), 0) == datetime(2026, 9, 26, 21, 0, tzinfo=timezone.utc)  # IDT
    assert cutoff_utc(STATIONS["london"], date(2026, 1, 10), 8).hour == 8  # GMT


def test_yes_price_series_flips_no_trades_and_price_at():
    series = yes_price_series([
        {"timestamp": 100, "price": 0.30, "outcome": "Yes"},
        {"timestamp": 200, "price": 0.60, "outcome": "No"},  # a NO at 0.60 is a YES at 0.40
        {"timestamp": 50, "price": 0.20, "outcome": "Yes"},
    ])
    assert series == [(50, 0.20), (100, 0.30), (200, 0.40)]
    assert price_at(series, datetime.fromtimestamp(150, tz=timezone.utc)) == 0.30
    assert price_at(series, datetime.fromtimestamp(10, tz=timezone.utc)) is None
    assert price_at(series, datetime.fromtimestamp(999, tz=timezone.utc)) == 0.40


def test_bracket_probability_partitions_and_handles_open_ends():
    mu, sd = 71.3, 2.5
    total = bracket_probability(None, 63, mu, sd) + sum(bracket_probability(lo, lo + 1, mu, sd) for lo in range(64, 82, 2)) + bracket_probability(82, None, mu, sd)
    assert abs(total - 1.0) < 1e-9
    assert bracket_probability(70, 71, mu, sd) > bracket_probability(76, 77, mu, sd)
    assert bracket_probability(28, 28, 28.0, 0.001) > 0.999  # certainty collapses onto the rounded integer


def test_calib_fit_uses_prior_until_enough_days():
    assert Calib.fit([1.0, -1.0], prior_sd=3.2).sd == 3.2
    c = Calib.fit([1.0, 1.5, 0.5, 1.0, 1.2, 0.8, 1.1, 0.9], prior_sd=3.2)
    # residual variance 0.086 minus the rounding noise 1/12 leaves ~0, so the 0.3-degree floor binds
    assert abs(c.bias - 1.0) < 1e-9 and abs(c.sd - 0.3) < 1e-9 and c.n == 8


def test_calib_fit_removes_rounding_noise_and_honours_window():
    import random
    rng = random.Random(3)
    truth_sd = 1.0
    # residual = round(actual) - forecast where actual - forecast ~ N(0.4, 1.0): the settled integer adds 1/12 variance
    res = [round(0.4 + rng.gauss(0, truth_sd)) for _ in range(4000)]
    c = Calib.fit([float(x) for x in res], prior_sd=1.8)
    assert abs(c.bias - 0.4) < 0.06 and abs(c.sd - truth_sd) < 0.06
    # a window keeps only the last N residuals, so an old bias does not linger
    old = [-2.0] * 30
    recent = [0.5] * 10
    w = Calib.fit(old + recent, prior_sd=1.8, window=10)
    assert w.n == 10 and abs(w.bias - 0.5) < 1e-9 and abs(w.sd - 0.3) < 1e-9
    assert Calib.fit(old + recent, prior_sd=1.8).n == 40


def test_openmeteo_daily_extreme_parses_multi_model_hourly():
    times = [f"2026-09-26T{h:02d}:00" for h in range(24)] + [f"2026-09-27T{h:02d}:00" for h in range(24)]
    payload = {"hourly": {"time": times,
                          "temperature_2m_previous_day1_ecmwf_ifs025": [60 + (h % 24) / 2 for h in range(48)],
                          "temperature_2m_previous_day1_gfs_seamless": [58 + (h % 24) / 2 for h in range(48)]}}
    out = OpenMeteo._daily_extreme(payload, "temperature_2m_previous_day1", ("ecmwf_ifs025", "gfs_seamless"), "high")
    assert out[date(2026, 9, 26)] == {"ecmwf_ifs025": 71.5, "gfs_seamless": 69.5}
    low = OpenMeteo._daily_extreme(payload, "temperature_2m_previous_day1", ("ecmwf_ifs025",), "low")
    assert low[date(2026, 9, 27)] == {"ecmwf_ifs025": 60.0}
    single = OpenMeteo._daily_extreme({"hourly": {"time": times[:24], "temperature_2m": [20.0] * 24}}, "temperature_2m", ("icon_seamless",), "high")
    assert single[date(2026, 9, 26)] == {"icon_seamless": 20.0}


def test_fixture_events_resolve_to_one_bracket_each():
    events = FixtureSource(FIXTURES).poly_weather_events(SINCE, NOW)
    rows = bracket_markets(events)
    assert {r.city for r in rows} == {"nyc", "tel-aviv"}
    for evrows in group_by_event(rows).values():
        assert len(evrows) == 11
        assert actual_temperature(evrows) is not None


def test_trend_on_fixture_prices_every_bracket_at_local_midnight():
    events = FixtureSource(FIXTURES).poly_weather_events(SINCE, NOW)
    rows = bracket_markets(events)
    table, pairs = trend(rows, FixtureTrades(FIXTURES), cutoff_hour=0, events_per_month=25)
    assert set(pairs) == {"2026-09"}
    assert len(pairs["2026-09"]) == 33 and all(0 <= p <= 1 for p, _ in pairs["2026-09"])
    assert sum(y for _, y in pairs["2026-09"]) == 3  # one winner per event
    assert table == []  # fewer than 30 live pairs -> no summary row
    row = summarise_pairs("2026-09", 3, pairs["2026-09"] * 2, 0.05)
    assert row is not None and row.markets >= 30 and 0 <= row.base_rate <= 1


class FakeForecast:
    """Deterministic forecasts: the actual bracket midpoint plus a fixed offset per model."""

    def __init__(self, truth, offsets):
        self.truth = truth  # {(city, day): actual}
        self.offsets = offsets

    def previous_runs(self, station, start, end, models, lead_days, kind):
        out = {}
        for (city, d), actual in self.truth.items():
            if city == station.key and start <= d <= end:
                out[d] = {m: actual + self.offsets[m] for m in models}
        return out

    def forecast(self, station, days, models, kind):
        return self.previous_runs(station, date(2000, 1, 1), date(2100, 1, 1), models, 1, kind)

    def ensemble(self, station, days, model, kind):
        return {}


def _truth(rows):
    return {(evrows[0].city, evrows[0].day): actual_temperature(evrows) for evrows in group_by_event(rows).values()}


def test_backtest_with_a_perfect_forecast_beats_the_market():
    events = FixtureSource(FIXTURES).poly_weather_events(SINCE, NOW)
    rows = bracket_markets(events)
    fc = FakeForecast(_truth(rows), {"ecmwf_ifs025": 0.0, "gfs_seamless": 1.0})
    r = backtest(rows, FixtureTrades(FIXTURES), fc, models=("ecmwf_ifs025", "gfs_seamless"), min_calib_days=0)
    assert r.model_errors["ecmwf_ifs025"]["mae"] == 0.0 and abs(r.model_errors["gfs_seamless"]["bias"] + 1.0) < 1e-9
    assert r.n_scored == 33 and r.model_brier < r.market_brier
    assert r.trades > 0 and r.pnl > 0
    assert set(r.by_month) == {"2026-09"}
    assert set(r.by_city) == {"nyc", "tel-aviv"} and r.by_city["nyc"]["unit"] == "F" and r.by_city["tel-aviv"]["unit"] == "C"
    assert r.by_city["nyc"]["model_errors"]["ecmwf_ifs025"]["mae"] == 0.0 and "blend" in r.by_city["nyc"]["model_errors"]
    assert sum(c["n_scored"] for c in r.by_city.values()) == r.n_scored
    assert abs(sum(c["pnl"] for c in r.by_city.values()) - r.pnl) < 1e-9 and r.pnl_se is not None
    assert r.reliability["model"] and r.reliability["market"] and sum(b["n"] for b in r.reliability["market"]) == r.n_scored
    text = render_backtest(r)
    assert "Reliability" in text and "tel-aviv     C" in text and "market beats forecast" not in text
    d = r.to_dict()
    assert d["days"][0]["day"].startswith("2026-09") and d["by_city"]["nyc"]["days"] == 2
    w = backtest(rows, FixtureTrades(FIXTURES), fc, models=("ecmwf_ifs025", "gfs_seamless"), min_calib_days=0, calib_window=1)
    assert w.calib_window == 1 and "calibration window 1 days" in render_backtest(w)


def test_today_quotes_only_edges_and_respects_depth(monkeypatch):
    events = FixtureSource(FIXTURES).poly_weather_events(SINCE, NOW)
    rows = bracket_markets(events)
    truth = _truth(rows)
    for ev in events:  # pretend the fixture events are still open and trading
        ev.closed = False
        for m in ev.markets:
            m.closed = False
            m.accepting_orders = True
    fc = FakeForecast(truth, {"ecmwf_ifs025": 0.0})
    # a book that prices the true bracket at 30c and everything else at 10c
    books = {}
    for r in rows:
        actual = truth[(r.city, r.day)]
        is_true = (r.lo is None or r.lo <= actual) and (r.hi is None or actual <= r.hi)
        p = 0.30 if is_true else 0.10
        books[r.market.yes_token] = Book(token_id=r.market.yes_token, bids=[Level(p - 0.01, 50)], asks=[Level(p + 0.01, 40)])
    quotes, summaries = today(rows, books, fc, models=("ecmwf_ifs025",), budget=300, edge=0.04, fee_rate=0.05)
    assert len(summaries) == 3
    takers = [q for q in quotes if "taker" in q.action]
    assert takers and all(q.edge_per_share >= 0.04 for q in takers)
    buys = [q for q in takers if q.action.startswith("buy YES")]
    assert buys and all(q.size <= 30 for q in buys)  # capped at 10% of budget
    assert quotes[0].action.endswith("(taker)")  # takers rank first


def test_extra_forecasts_join_the_model_table(tmp_path):
    from pm_scanner.weather import load_extra_forecasts, merge_extra

    csv = tmp_path / "x.csv"
    csv.write_text("city,date,source,value\n# comment\ntel-aviv,2026-09-27,ims,29\nnyc,2026-09-26,nws,71\nbad,line\n")
    extra = load_extra_forecasts(csv)
    assert extra[("tel-aviv", date(2026, 9, 27))] == {"ims": 29.0} and extra[("nyc", date(2026, 9, 26))] == {"nws": 71.0}
    merged = merge_extra({date(2026, 9, 27): {"ecmwf_ifs025": 28.0}}, extra, "tel-aviv")
    assert merged[date(2026, 9, 27)] == {"ecmwf_ifs025": 28.0, "ims": 29.0}
    assert load_extra_forecasts(tmp_path / "missing.csv") == {}
    events = FixtureSource(FIXTURES).poly_weather_events(SINCE, NOW)
    rows = bracket_markets(events)
    fc = FakeForecast(_truth(rows), {"ecmwf_ifs025": 0.0})
    r = backtest(rows, FixtureTrades(FIXTURES), fc, models=("ecmwf_ifs025",), min_calib_days=0, extra=extra)
    assert "ims" in r.model_errors and r.model_errors["ims"]["n"] == 1


def test_station_override_and_cli_trend(tmp_path):
    parse_station_overrides("nyc=40.78,-73.87")
    assert STATIONS["nyc"].lat == 40.78 and STATIONS["nyc"].icao == "KLGA"
    out = tmp_path / "t.json"
    rc = cli.main(["weather", "--mode", "trend", "--fixtures", str(FIXTURES), "--days", "400", "--cities", "nyc,tel-aviv", "--json", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert "2026-09" in data["pairs"] and len(data["pairs"]["2026-09"]) == 33
    assert cli.main(["weather", "--mode", "trend", "--fixtures", str(FIXTURES), "--cities", "atlantis"]) == 2
