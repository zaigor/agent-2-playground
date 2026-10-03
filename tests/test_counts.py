from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pm_scanner.counts import (
    Catalog,
    build_signal_rows,
    check_catalog,
    count_markets,
    decision_times,
    fit_rate,
    headroom,
    in_bracket,
    load_catalog,
    p_bracket,
    parse_bracket,
    parse_month_window,
    parse_window,
    render_headroom,
    write_signal_csv,
)

FIXTURES = Path(__file__).parent / "fixtures"
UTC = timezone.utc


def test_parse_window_reads_the_three_gamma_wordings():
    posts = "posts on Truth Social between September 18, 12:00 PM ET and September 25, 2026, 12:00 PM ET. For the purposes"
    assert parse_window(posts) == (datetime(2026, 9, 18, 16, tzinfo=UTC), datetime(2026, 9, 25, 16, tzinfo=UTC))  # EDT = UTC-4
    quakes = "that occur anywhere on Earth between September 14, 2026, 12:00 AM ET and September 20, 2026, 11:59 PM ET. The resolution"
    old_quakes = "occur anywhere on Earth between December 22, 12:00 AM ET, and December 28, 2025, 11:59 PM ET. The resolution"
    assert parse_window(old_quakes) == (datetime(2025, 12, 22, 5, tzinfo=UTC), datetime(2025, 12, 29, 5, tzinfo=UTC))  # EST, comma before `and`
    assert parse_window(quakes) == (datetime(2026, 9, 14, 4, tzinfo=UTC), datetime(2026, 9, 21, 4, tzinfo=UTC))  # 11:59 PM closes the day
    ships = "reports for the Strait of Hormuz for all days from September 21, 2026, through September 27, 2026, inclusive."
    assert parse_window(ships) == (datetime(2026, 9, 21, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC))
    elon = "posts on X from October 1 12:00 PM ET to October 3, 2026 12:00 PM ET."
    assert parse_window(elon) == (datetime(2026, 10, 1, 16, tzinfo=UTC), datetime(2026, 10, 3, 16, tzinfo=UTC))
    # the year is missing on the first date and the window crosses New Year
    ny = "between December 30, 12:00 PM ET and January 6, 2027, 12:00 PM ET"
    a, b = parse_window(ny)
    assert a.year == 2026 and b.year == 2027 and a.hour == 17  # EST = UTC-5
    assert parse_window("resolves on the Monday following release") is None
    jan = datetime(2026, 8, 15, tzinfo=UTC)
    assert parse_month_window("Will there be under 100 tornadoes in the US in July?", end_hint=jan) == (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))
    assert parse_month_window("US Tornadoes in October 2026") == (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    assert parse_month_window("Will Claude go down on __ days in December?", end_hint=datetime(2027, 1, 10, tzinfo=UTC)) == (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))


def test_parse_bracket_label_forms():
    assert parse_bracket("<20") == (0, 19)
    assert parse_bracket("20-39") == (20, 39)
    assert parse_bracket("100–129") == (100, 129)
    assert parse_bracket("200+") == (200, None)
    assert parse_bracket("0") == (0, 0)
    assert parse_bracket("more than 5") == (6, None)
    assert parse_bracket("≤6") == (0, 6)
    assert parse_bracket(">5") == (6, None) and parse_bracket(">9") == (10, None) and parse_bracket("≤5") == (0, 5)
    assert parse_bracket("", "Will there be 2 earthquakes of 6.5 or above magnitude worldwide?") == (2, 2)
    assert parse_bracket("", "Will there be more than 5 earthquakes of 6.5 or above magnitude worldwide?") == (6, None)
    assert parse_bracket("", "Will fewer than 20 ships transit the Strait of Hormuz between October 5-October 11?") == (0, 19)
    assert parse_bracket("", "Will 20-24 ships transit the Strait of Hormuz between October 5-October 11?") == (20, 24)
    assert parse_bracket("", "Will 40 or more ships transit the Strait of Hormuz between October 5-October 11?") == (40, None)
    assert parse_bracket("", "Will there be between 20 and 34 tornadoes in the US in October 2026?") == (20, 34)
    assert parse_bracket("", "Will there be 90 or more tornadoes in the US in October 2026?") == (90, None)
    assert parse_bracket("", "Will no states be in D4 (Exceptional Drought) this week?") == (0, 0)
    assert parse_bracket("", "Will Donald Trump post 120-139 Truth Social posts from October 2 to October 9, 2026?") == (120, 139)
    assert parse_bracket("", "Will Khamenei post 0-4 posts from October 2 to October 9, 2026?") == (0, 4)
    assert parse_bracket("Anthropic") is None
    assert in_bracket(19, (0, 19)) and not in_bracket(20, (0, 19)) and in_bracket(10**6, (200, None))


def _uniform_catalog(start: datetime, days: int, per_day: float, phase_hours: tuple[int, ...] = (2, 8, 14, 20)) -> Catalog:
    times = []
    for d in range(days):
        for h in phase_hours:
            for _ in range(int(per_day / len(phase_hours))):
                times.append((start + timedelta(days=d, hours=h)).timestamp())
    return Catalog(times, [1.0] * len(times))


def test_catalog_fit_rate_and_p_bracket():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    cat = _uniform_catalog(start, 70, 16)  # 112 per week, every week
    a, b = datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 3, 8, tzinfo=UTC)
    assert cat.count(a.timestamp(), b.timestamp()) == 112
    model = fit_rate(cat, a, b, k_windows=8)
    assert model is not None and model.k_windows == 8 and model.mu == 112 and model.r is None  # no overdispersion -> Poisson
    assert abs(model.remaining_fraction(0.5) - 0.5) < 0.02 and model.remaining_fraction(0.0) == 1.0 and model.remaining_fraction(1.0) == 0.0
    assert fit_rate(cat, datetime(2026, 1, 3, tzinfo=UTC), datetime(2026, 1, 10, tzinfo=UTC)) is None  # no full reference window before it
    ladder = [(0, 19), (20, 39), (40, 59), (60, 79), (80, 99), (100, 119), (120, 139), (140, 159), (160, 179), (180, 199), (200, None)]
    total = sum(p_bracket(0, 112.0, None, br) for br in ladder)
    assert abs(total - 1.0) < 1e-6
    assert p_bracket(130, 10.0, None, (100, 119)) == 0.0  # already above the bracket
    assert p_bracket(130, 0.0, None, (120, 139)) == 1.0  # window over, count inside
    nb_total = sum(p_bracket(0, 112.0, 5.0, br) for br in ladder)
    assert abs(nb_total - 1.0) < 1e-6 and p_bracket(0, 112.0, 5.0, (200, None)) > p_bracket(0, 112.0, None, (200, None))  # fatter tail
    # dispersion is read off the reference windows
    bursty = Catalog([t for t in cat.times if (datetime.fromtimestamp(t, tz=UTC).isocalendar().week % 2 == 0)] * 2, [1.0] * (2 * sum(1 for t in cat.times if datetime.fromtimestamp(t, tz=UTC).isocalendar().week % 2 == 0)))
    m2 = fit_rate(bursty, a, b, k_windows=8)
    assert m2 is not None and m2.r is not None and m2.r > 0


def test_load_catalog_formats(tmp_path):
    p = tmp_path / "quakes.csv"
    p.write_text("time,latitude,longitude,mag\n2026-09-15T10:00:00.000Z,1,2,6.7\n2026-09-16T10:00:00.000Z,1,2,6.1\n1789000000,1,2,7.0\nbad,1,2,6.9\n")
    cat, problems = load_catalog(p, value_col="mag", min_value=6.5)
    assert len(cat.times) == 2 and len(problems) == 1
    daily = tmp_path / "portwatch.csv"
    daily.write_text("date,portname,n_total\n2026-09-21,Hormuz,31\n2026-09-22,Hormuz,28\n")
    cat2, _ = load_catalog(daily, count_col="n_total")
    assert cat2.count(datetime(2026, 9, 21, tzinfo=UTC).timestamp(), datetime(2026, 9, 23, tzinfo=UTC).timestamp()) == 59
    local = tmp_path / "local.csv"
    local.write_text("created_at,text\n2026-09-18 13:00:00,hi\n")
    cat3, _ = load_catalog(local, tz="America/New_York")
    assert cat3.times[0] == datetime(2026, 9, 18, 17, tzinfo=UTC).timestamp()
    (tmp_path / "nocol.csv").write_text("a,b\n1,2\n")
    cat4, problems4 = load_catalog(tmp_path / "nocol.csv")
    assert not cat4.times and "no time column" in problems4[0]


def test_count_markets_fixture_windows_and_brackets():
    events = json.loads((FIXTURES / "counts_events.json").read_text())
    markets = count_markets(events, monthly=True)
    assert len(markets) == sum(len(e["markets"]) for e in events) == 51
    by_title = {}
    for m in markets:
        by_title.setdefault(m.event_title, []).append(m)
    trump = next(v for k, v in by_title.items() if "September 18 - September 25" in k)
    assert trump[0].window == (datetime(2026, 9, 18, 16, tzinfo=UTC), datetime(2026, 9, 25, 16, tzinfo=UTC))
    assert [m.bracket for m in trump][:3] == [(0, 19), (20, 39), (40, 59)] and trump[-1].bracket == (200, None)
    assert sum(1 for m in trump if m.resolved_yes) == 1
    quake = next(v for k, v in by_title.items() if "September 14 - September 20" in k)
    assert quake[0].window == (datetime(2026, 9, 14, 4, tzinfo=UTC), datetime(2026, 9, 21, 4, tzinfo=UTC))
    assert [m.bracket for m in quake] == [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, None)]
    ships = next(v for k, v in by_title.items() if "Hormuz" in k)
    assert ships[0].window == (datetime(2026, 9, 21, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC)) and ships[-1].bracket == (40, None)
    tornado = next(v for k, v in by_title.items() if "Tornadoes" in k)
    assert tornado[0].window == (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)) and tornado[0].bracket == (0, 99) and tornado[1].bracket == (100, 129)
    assert len(count_markets(events, monthly=False)) == 51 - len(tornado)


def test_build_rows_and_check_catalog(tmp_path):
    events = json.loads((FIXTURES / "counts_events.json").read_text())
    markets = [m for m in count_markets(events) if "Truth Social" in m.event_title]
    windows = sorted({m.window for m in markets})
    a0 = windows[0][0]
    # a catalog of 16 posts a day for twelve weeks before and through both windows
    cat = _uniform_catalog(a0 - timedelta(days=70), 90, 16)
    rows, skipped = build_signal_rows(markets, cat, k_windows=8, hours=(12,), pre_days=1)
    assert not skipped and rows
    times = sorted({r.time for r in rows if r.market == markets[0].condition_id})
    assert len(times) == len(decision_times(*markets[0].window, hours=(12,), pre_days=1)) and len(times) >= 8
    for t in times:
        assert abs(sum(r.p for r in rows if r.time == t and r.market in {m.condition_id for m in markets if m.window == markets[0].window}) - 1.0) < 1e-3
    # the live count narrows the ladder: by the last decision time the 100-119 bracket (112 posts) carries most of the mass
    last = times[-1]
    best = max((r for r in rows if r.time == last and r.market in {m.condition_id for m in markets if m.window == markets[0].window}), key=lambda r: r.p)
    assert next(m for m in markets if m.condition_id == best.market).bracket == (100, 119) and best.p > 0.9
    out = tmp_path / "sig.csv"
    write_signal_csv(rows, out)
    with out.open() as fh:
        recs = list(csv.DictReader(fh))
    assert recs and set(recs[0]) == {"market", "time", "p", "note"} and 0.0 <= float(recs[0]["p"]) <= 1.0
    checks = check_catalog(markets, cat)
    assert len(checks) == 2 and all(c["count"] == 112 for c in checks)
    # the catalog counts 112 posts a week; the winning bracket says whether that matches what the tracker counted
    assert all(c["match"] == in_bracket(112, c["bracket"]) for c in checks)
    # a catalog that stops before the window: rows are not produced for times it does not cover
    short = Catalog([t for t in cat.times if t < windows[0][0].timestamp() + 86400], [1.0] * sum(1 for t in cat.times if t < windows[0][0].timestamp() + 86400))
    rows2, skipped2 = build_signal_rows(markets, short, k_windows=8)
    assert skipped2.get("catalog ends before the decision time") and len(rows2) < len(rows)


class _FakeTrades:
    """Price 1/k until the window starts, drifting to the outcome by the end."""

    def __init__(self, markets):
        self.by_id = {}
        by_window = {}
        for m in markets:
            by_window.setdefault(m.window, []).append(m)
        for (a, b), group in by_window.items():
            k = len(group)
            for m in group:
                y = 1.0 if m.resolved_yes else 0.0
                pts = [(a - timedelta(hours=2), 1.0 / k), (a + (b - a) * 0.5, 0.5 / k + 0.5 * y), (b - timedelta(minutes=1), 0.02 + 0.96 * y)]
                self.by_id[m.condition_id] = [{"timestamp": int(t.timestamp()), "price": p, "size": 10, "outcome": "Yes", "side": "BUY"} for t, p in pts]

    def trades(self, condition_id, closed=True):
        return self.by_id.get(condition_id, [])


def test_headroom_scores_market_against_uniform_and_climatology():
    events = json.loads((FIXTURES / "counts_events.json").read_text())
    quake_events = [e for e in events if "earthquakes" in e["title"]]
    markets = count_markets(quake_events)
    fam, rows = headroom("6pt5-earthquake-weekly", quake_events, _FakeTrades(markets), per_family=12)
    assert fam.events == 2 and fam.markets == 14 and fam.windows_parsed == 14 and set(fam.per_offset) == {"start", "mid", "late"}
    start, late = fam.per_offset["start"], fam.per_offset["late"]
    assert abs(start["brier_market"] - start["brier_uniform"]) < 1e-9  # the fake price is 1/k at the window start
    assert late["brier_market"] < start["brier_market"]
    assert start["market_minus_clim"] is None  # two events: no label has five earlier outcomes
    text = render_headroom([fam])
    assert "6pt5-earthquake-weekly" in text and "start" in text and "late" in text


def test_parse_window_accepts_noon_parenthetical():
    from datetime import datetime, timezone
    from pm_scanner.counts import parse_window
    text = ("If Elon Musk (@elonmusk), posts less than 40 times on X between May 31, 2024, 12:00 PM ET (noon) "
            "and June 7, 2024, 12:00 PM ET this market will resolve to \"Yes\".")
    w = parse_window(text)
    assert w is not None
    a, b = w
    assert a == datetime(2024, 5, 31, 16, 0, tzinfo=timezone.utc)  # noon EDT
    assert b == datetime(2024, 6, 7, 16, 0, tzinfo=timezone.utc)


def test_load_catalog_reads_tracker_posts_exports(tmp_path):
    """The tracker's per-window "Posts" export: unquoted `M/D/YYYY, h:mm:ss PM` timestamps that split
    into two CSV fields, quoted multi-line content, ET times labelled EST, and overlapping windows."""
    from datetime import datetime, timezone
    from pm_scanner.counts import load_catalog
    head = "Post ID,User,Content,Posted At (EST),Imported At (EST)\n"
    a = tmp_path / "elonmusk-Sep_29___Oct_6-posts.csv"
    b = tmp_path / "elonmusk-Oct_2___Oct_9-posts.csv"
    a.write_text(head
        + '2106454808676716916,elonmusk,"Starship deploying Starlink V3 satellites https://pbs.twimg.com/x.jpg",10/3/2026, 2:42:17 PM,10/3/2026, 2:45:30 PM\n'
        + '2106454043895964113,elonmusk,"Falcon 9 flew more missions, in ONE YEAR\n\nthan the Shuttle",10/3/2026, 2:39:15 PM,10/3/2026, 2:45:30 PM\n'
        + '2106000000000000001,elonmusk,"noon edge",10/1/2026, 12:00:00 PM,10/1/2026, 12:03:00 PM\n', encoding="utf-8")
    b.write_text(head
        + '2106454808676716916,elonmusk,"Starship deploying Starlink V3 satellites https://pbs.twimg.com/x.jpg",10/3/2026, 2:42:17 PM,10/3/2026, 2:45:30 PM\n'
        + '2106999999999999999,elonmusk,"later",10/5/2026, 9:00:00 AM,10/5/2026, 9:02:00 AM\n', encoding="utf-8")
    cat, notes = load_catalog(tmp_path)
    assert len(cat.times) == 4, notes                      # 5 rows, one repeated id
    assert any("repeated ids dropped" in n for n in notes) and any("America/New_York" in n for n in notes)
    assert not [n for n in notes if "unreadable" in n]
    noon_et = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc).timestamp()   # 12:00 EDT
    assert cat.count(noon_et, noon_et + 1) == 1                               # the edge post is in, not before
    assert cat.count(datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc).timestamp(), datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc).timestamp()) == 2
    one, _ = load_catalog(a)
    assert len(one.times) == 3
    fixed, _ = load_catalog(a, tz="Etc/GMT+5")                                # fixed EST moves everything an hour
    assert fixed.times[0] == one.times[0] + 3600
