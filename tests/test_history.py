from __future__ import annotations

from pm_scanner.history import PriceWeek, fetch_price_week, measure_week, week_fetcher
from pm_scanner.http import HttpError


def _week(jumps: int, *, start: int = 1_790_000_000, points: int = 1008, step: int = 600, base: float = 0.40, every: int = 100) -> list[tuple[int, float]]:
    """A week of 10-minute prices that drift by a tenth of a cent and jump 3c `jumps` times, one jump every `every` samples."""
    out, p = [], base
    for i in range(points):
        if 0 < i <= jumps * every and i % every == 0:
            p += 0.03 if (i // every) % 2 else -0.03  # alternating direction, so the price stays in range
        else:
            p += 0.001 if i % 2 else -0.001
        out.append((start + i * step, round(p, 4)))
    return out


def test_measure_week_counts_the_jumps_and_the_age():
    w = measure_week(_week(7))
    assert w is not None and w.points == 1008 and abs(w.days - 1007 * 600 / 86400) < 1e-9  # 6.99 days
    assert w.moves == 7 and abs(w.moves_per_day - 7 / w.days) < 1e-9  # the 0.1c drift never counts
    assert abs(w.path - (7 * 0.03 + 1000 * 0.001)) < 1e-6 and w.low < 0.40 < w.high
    assert measure_week(_week(0)).moves == 0
    day_old = measure_week(_week(46, points=136, every=2))  # the Kostyantynivka market on 4 Oct: 136 points, 46 jumps
    assert day_old.days < 1.0 and day_old.moves_per_day > 40
    assert measure_week([]) is None and measure_week([(1, 0.5)]) is None
    assert measure_week([(2, 0.50), (1, 0.47)]).moves == 1  # unsorted input, and a step of exactly the band counts
    d = w.to_dict()
    assert set(d) >= {"days", "points", "moves", "moves_per_day", "path", "path_per_day", "low", "high", "band"}


class _Http:
    def __init__(self, histories: dict[str, list[dict]]):
        self.histories, self.calls = histories, []

    def get_json(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        token = params["market"]
        if token not in self.histories:
            raise HttpError(f"GET {url} -> HTTP 404")
        return {"history": self.histories[token]}


def test_fetch_and_fetcher_read_the_clob_week_and_cache_it():
    pts = _week(3)
    http = _Http({"tok": [{"t": t, "p": p} for t, p in pts] + [{"t": "bad", "p": None}]})
    assert fetch_price_week(http, "tok") == pts  # the malformed point is dropped
    assert http.calls[0][0].endswith("/prices-history") and http.calls[0][1] == {"market": "tok", "interval": "1w", "fidelity": 10}
    get = week_fetcher(http)
    a, b = get("tok"), get("tok")
    assert isinstance(a, PriceWeek) and a.moves == 3 and a is b and len(http.calls) == 2  # one request for two reads
    assert get("missing") is None and get("missing") is None and len(http.calls) == 3  # a failed request reads as no history, once
