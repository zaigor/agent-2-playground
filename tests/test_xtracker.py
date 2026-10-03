from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pm_scanner.counts import load_catalog
from pm_scanner.xtracker import compare_exports, harvest, looks_truncated, parse_when, post_time, time_field, unwrap, weekly_table, write_posts_csv

UTC = timezone.utc
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _posts(days: int, per_day: int, start: datetime = T0) -> list[dict]:
    out = []
    for d in range(days):
        for k in range(per_day):
            t = start + timedelta(days=d, minutes=(k * 1440) // per_day)
            n = d * per_day + k
            out.append({"id": f"cm{n:06d}", "userId": "u1", "platformId": str(2_000_000_000_000 + n), "content": f"post {n}, with a comma\nand a line",
                        "createdAt": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "importedAt": (t + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    return out


class FakeServer:
    """The posts route as the fetcher sees it: newest first, dates inclusive, `limit` clamped silently."""

    def __init__(self, posts, *, cap=100, honour_dates=True, hint=False, offset=False):
        self.posts = sorted(posts, key=lambda r: r["createdAt"], reverse=True)
        self.cap, self.honour_dates, self.hint, self.offset = cap, honour_dates, hint, offset
        self.calls = 0

    def __call__(self, params):
        self.calls += 1
        rows = self.posts
        if self.honour_dates and "startDate" in params:
            a, b = parse_when(params["startDate"]), parse_when(params["endDate"])
            rows = [r for r in rows if a <= parse_when(r["createdAt"]) <= b]
        lim = min(int(params.get("limit", self.cap)), self.cap)
        off = int(params.get("offset", 0)) if self.offset else 0
        page = rows[off : off + lim]
        meta = {"pagination": {"hasMore": off + lim < len(rows)}} if self.hint else {}
        return page, meta


def test_unwrap_handles_flat_and_nested_envelopes():
    assert unwrap({"success": True, "data": [1, 2]}) == ([1, 2], {})
    data, meta = unwrap({"success": True, "data": {"posts": [1], "pagination": {"total": 5}}, "page": 1})
    assert data == [1] and meta == {"page": 1, "pagination": {"total": 5}}


def test_post_time_prefers_the_explicit_post_field_over_created_at():
    rec = {"createdAt": "2026-10-03T18:45:30.000Z", "postedAt": "2026-10-03T18:42:17.000Z"}
    assert time_field(rec) == "postedAt"
    assert post_time(rec) == datetime(2026, 10, 3, 18, 42, 17, tzinfo=UTC)
    assert time_field({"createdAt": "2026-10-03T18:45:30.000Z", "importedAt": "x"}) == "createdAt"
    ms = str(int(datetime(2026, 10, 3, 18, 42, 17, tzinfo=UTC).timestamp() * 1000))
    assert post_time({"createdAt": ms}) == datetime(2026, 10, 3, 18, 42, 17, tzinfo=UTC)


def test_looks_truncated_reads_the_hints():
    assert looks_truncated(100, 100, {})
    assert not looks_truncated(40, 100, {})
    assert looks_truncated(40, 100, {"pagination": {"hasMore": True}})
    assert looks_truncated(40, 100, {"total": 250})
    assert looks_truncated(40, 100, {"nextCursor": "abc"})
    assert not looks_truncated(40, 100, {"pagination": {"hasMore": False, "total": 40}})


def test_harvest_recovers_everything_under_a_silent_page_cap():
    posts = _posts(21, 40)  # 280 a week, like Musk; the server hands back at most 100 and says nothing
    srv = FakeServer(posts, cap=100)
    h = harvest(srv, T0, T0 + timedelta(days=21), chunk=timedelta(days=7), limit=500)
    assert len(h.posts) == len(posts)
    assert h.cap == 100
    assert any("at most 100 posts per request" in n for n in h.notes)
    assert h.truncated_leaves == 0
    assert h.time_field == "createdAt"
    assert srv.calls < 60


def test_harvest_splits_on_the_pagination_hint_and_the_limit():
    posts = _posts(14, 30)
    srv = FakeServer(posts, cap=50, hint=True)
    h = harvest(srv, T0, T0 + timedelta(days=14), chunk=timedelta(days=7), limit=50)
    assert len(h.posts) == len(posts)
    assert h.cap is None  # never needed the calibration: the limit itself said when a page was full


def test_harvest_pages_when_the_server_ignores_dates():
    posts = _posts(10, 25)
    srv = FakeServer(posts, cap=60, honour_dates=False, offset=True)
    h = harvest(srv, T0, T0 + timedelta(days=10), chunk=timedelta(days=7), limit=60)
    assert len(h.posts) == len(posts)
    assert any("paged by offset" in n for n in h.notes)


def test_harvest_handles_an_empty_history():
    srv = FakeServer([], cap=100)
    h = harvest(srv, T0, T0 + timedelta(days=14), chunk=timedelta(days=7), limit=100)
    assert h.posts == {} and h.requests == 2 and h.notes == []


def test_write_posts_csv_is_a_counts_catalog(tmp_path: Path):
    posts = _posts(3, 8)
    srv = FakeServer(posts, cap=100)
    h = harvest(srv, T0, T0 + timedelta(days=3), limit=100)
    out = tmp_path / "x" / "elonmusk.csv"
    assert write_posts_csv(h, out) == 24
    with out.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["time"] == "2026-09-01T00:00:00Z" and rows[0]["post_id"] == "2000000000000"
    assert rows[0]["imported_at"].startswith("2026-09-01T00:03:00") and "comma\nand" in rows[0]["content"]
    cat, notes = load_catalog(out)
    assert not notes
    assert len(cat.times) == 24
    assert cat.count(T0.timestamp(), (T0 + timedelta(days=1)).timestamp()) == 8
    assert cat.first == T0.timestamp()


def test_compare_exports_confirms_the_export_zone(tmp_path: Path):
    posts = _posts(2, 6)
    h = harvest(FakeServer(posts, cap=100), T0, T0 + timedelta(days=2), limit=100)
    ny = ZoneInfo("America/New_York")
    fp = tmp_path / "a-posts.csv"
    with fp.open("w", newline="") as f:
        f.write("Post ID,User,Content,Posted At (EST),Imported At (EST)\n")
        for r in posts:
            t = parse_when(r["createdAt"]).astimezone(ny)
            row = [r["platformId"], "Elon Musk", r["content"].replace("\n", " "), f"{t.month}/{t.day}/{t.year}, {t:%I:%M:%S %p}".replace(" 0", " "), f"{t.month}/{t.day}/{t.year}, {t:%I:%M:%S %p}".replace(" 0", " ")]
            f.write(",".join(('"' + c + '"') if ("," in c and "/" not in c) else c for c in row) + "\n")
    text = compare_exports(h, [fp])
    assert text.startswith("export vs API: all 12 shared posts agree")
    text5 = compare_exports(h, [fp], zone="Etc/GMT+5")
    assert "60 min" in text5 or "-60 min" in text5


def test_weekly_table_counts_whole_utc_weeks():
    times = [T0 + timedelta(hours=6 * k) for k in range(100)]  # 4 a day from Tue 1 Sep
    text = weekly_table(times, weeks=2)
    assert "2026-09-14 .. 2026-09-21  28" in text
