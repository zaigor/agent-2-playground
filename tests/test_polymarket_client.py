from pm_scanner.polymarket import PolymarketClient


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return self.pages[len(self.calls) - 1]


def _ev(i):
    return {"id": str(i), "title": f"E{i}", "slug": f"e{i}", "markets": []}


def test_iter_events_follows_keyset_cursor_and_dedups():
    http = FakeHttp(
        [
            {"events": [_ev(1), _ev(2)], "next_cursor": "c1"},
            {"events": [_ev(2), _ev(3)], "next_cursor": "c2"},
            {"events": [_ev(4)], "next_cursor": None},
        ]
    )
    evs = list(PolymarketClient(http).iter_events(page_size=2, max_events=10))
    assert [e.id for e in evs] == ["1", "2", "3", "4"]
    assert all(url.endswith("/events/keyset") for url, _ in http.calls)
    assert "offset" not in http.calls[0][1] and "after_cursor" not in http.calls[0][1]
    assert [p.get("after_cursor") for _, p in http.calls[1:]] == ["c1", "c2"]


def test_iter_events_stops_at_max_events():
    http = FakeHttp([{"events": [_ev(1), _ev(2)], "next_cursor": "c1"}])
    assert len(list(PolymarketClient(http).iter_events(page_size=2, max_events=2))) == 2
    assert len(http.calls) == 1


def test_iter_events_by_tag_passes_window_and_exclusion_params():
    from datetime import datetime, timezone

    http = FakeHttp([{"id": "84"}, [_ev(1), _ev(2)], [_ev(3)]])
    evs = list(PolymarketClient(http).iter_events_by_tag(
        "weather", page_size=2, closed_only=True, exclude_tag_id="102127",
        start_min=datetime(2026, 8, 1, tzinfo=timezone.utc), start_max=datetime(2026, 9, 1, tzinfo=timezone.utc),
    ))
    assert [e.id for e in evs] == ["1", "2", "3"]
    assert http.calls[0][0].endswith("/tags/slug/weather")
    params = http.calls[1][1]
    assert params["closed"] == "true" and params["exclude_tag_id"] == "102127"
    assert params["start_date_min"] == "2026-08-01T00:00:00Z" and params["start_date_max"] == "2026-09-01T00:00:00Z"
    assert http.calls[2][1]["offset"] == 2


def test_iter_events_by_tag_respects_offset_cap():
    http = FakeHttp([{"id": "1"}] + [[_ev(i), _ev(i + 1)] for i in range(0, 40, 2)])
    evs = list(PolymarketClient(http).iter_events_by_tag("x", page_size=2, max_offset=6))
    assert len(evs) == 6 and len(http.calls) == 4  # tag lookup + 3 pages
