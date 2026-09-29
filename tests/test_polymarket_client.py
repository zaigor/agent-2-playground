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
