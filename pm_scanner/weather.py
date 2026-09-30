"""Daily temperature markets: stations, bracket parsing, day-before prices from trade
history, forecast distributions from Open-Meteo, a backtest, and live quoting.

Three modes, all read-only:

  trend     How mispriced were the brackets at a fixed hour before the day started, month
            by month? Uses the data-api trade history (kept for months, unlike CLOB price
            history), sampling N events per month. Answers "is the edge still there?".
  backtest  Previous-day forecasts (Open-Meteo previous-runs API: ECMWF, GFS, ICON, ...)
            turned into bracket probabilities with a rolling per-station bias/sd, scored
            against the market's prices at the same hour and traded on paper net of fees.
  today     The same distribution from the current forecast against the open books.

Market rules (all 52 cities, checked 29 Sep 2026): the highest (or lowest) hourly
"Temp" reading at a named airport station on the local calendar day, rounded to the
nearest whole degree (half rounds up), in the unit the city uses. Brackets are inclusive
integer ranges with open ends ("63°F or below", "82°F or higher").
"""
from __future__ import annotations

import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .fees import polymarket_rate_for_event, polymarket_taker_fee
from .http import HttpClient, HttpError
from .polymarket import Book, PolyEvent, PolyMarket

DATA_API = "https://data-api.polymarket.com"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
OPEN_METEO_HISTORICAL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
DEFAULT_MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")
WEATHER_TAG = "weather"


@dataclass(frozen=True)
class Station:
    key: str  # city key used in the series slug: nyc, london, tel-aviv, ...
    icao: str
    name: str
    lat: float
    lon: float
    unit: str  # "F" or "C"
    tz: str  # IANA zone of the local calendar day the market resolves on

    @property
    def default_sd(self) -> float:
        """Prior day-ahead error sd of the hourly-max forecast before any local calibration."""
        return 3.2 if self.unit == "F" else 1.8


# Airport coordinates are from published aerodrome data (to ~0.01 deg, which is inside one
# Open-Meteo grid cell). Hong Kong resolves on the Observatory's own climate page, so the
# HKO headquarters coordinates are used there. Correct any entry with --station.
STATIONS: dict[str, Station] = {s.key: s for s in [
    Station("nyc", "KLGA", "LaGuardia Airport", 40.777, -73.872, "F", "America/New_York"),
    Station("london", "EGLC", "London City Airport", 51.505, 0.055, "C", "Europe/London"),
    Station("seoul", "RKSI", "Incheon Intl Airport", 37.469, 126.451, "C", "Asia/Seoul"),
    Station("hong-kong", "HKO", "Hong Kong Observatory HQ", 22.302, 114.174, "C", "Asia/Hong_Kong"),
    Station("shanghai", "ZSPD", "Shanghai Pudong Intl Airport", 31.143, 121.805, "C", "Asia/Shanghai"),
    Station("paris", "LFPB", "Paris-Le Bourget Airport", 48.969, 2.441, "C", "Europe/Paris"),
    Station("wellington", "NZWN", "Wellington Intl Airport", -41.327, 174.805, "C", "Pacific/Auckland"),
    Station("tokyo", "RJTT", "Tokyo Haneda Airport", 35.553, 139.781, "C", "Asia/Tokyo"),
    Station("miami", "KMIA", "Miami Intl Airport", 25.795, -80.290, "F", "America/New_York"),
    Station("atlanta", "KATL", "Hartsfield-Jackson Intl Airport", 33.640, -84.427, "F", "America/New_York"),
    Station("dallas", "KDAL", "Dallas Love Field", 32.847, -96.852, "F", "America/Chicago"),
    Station("toronto", "CYYZ", "Toronto Pearson Intl Airport", 43.677, -79.631, "C", "America/Toronto"),
    Station("beijing", "ZBAA", "Beijing Capital Intl Airport", 40.080, 116.585, "C", "Asia/Shanghai"),
    Station("shenzhen", "ZGSZ", "Shenzhen Bao'an Intl Airport", 22.639, 113.811, "C", "Asia/Shanghai"),
    Station("seattle", "KSEA", "Seattle-Tacoma Intl Airport", 47.449, -122.309, "F", "America/Los_Angeles"),
    Station("chicago", "KORD", "Chicago O'Hare Intl Airport", 41.978, -87.905, "F", "America/Chicago"),
    Station("madrid", "LEMD", "Madrid-Barajas Airport", 40.472, -3.561, "C", "Europe/Madrid"),
    Station("ankara", "LTAC", "Esenboga Intl Airport", 40.128, 32.995, "C", "Europe/Istanbul"),
    Station("munich", "EDDM", "Munich Airport", 48.354, 11.786, "C", "Europe/Berlin"),
    Station("buenos-aires", "SAEZ", "Ministro Pistarini Intl Airport", -34.822, -58.536, "C", "America/Argentina/Buenos_Aires"),
    Station("taipei", "RCSS", "Taipei Songshan Airport", 25.069, 121.552, "C", "Asia/Taipei"),
    Station("chengdu", "ZUUU", "Chengdu Shuangliu Intl Airport", 30.578, 103.947, "C", "Asia/Shanghai"),
    Station("singapore", "WSSS", "Singapore Changi Airport", 1.350, 103.994, "C", "Asia/Singapore"),
    Station("los-angeles", "KLAX", "Los Angeles Intl Airport", 33.942, -118.408, "F", "America/Los_Angeles"),
    Station("la", "KLAX", "Los Angeles Intl Airport", 33.942, -118.408, "F", "America/Los_Angeles"),
    Station("milan", "LIMC", "Milan Malpensa Airport", 45.630, 8.723, "C", "Europe/Rome"),
    Station("chongqing", "ZUCK", "Chongqing Jiangbei Intl Airport", 29.719, 106.642, "C", "Asia/Shanghai"),
    Station("guangzhou", "ZGGG", "Guangzhou Baiyun Intl Airport", 23.392, 113.299, "C", "Asia/Shanghai"),
    Station("sao-paulo", "SBGR", "Sao Paulo-Guarulhos Intl Airport", -23.432, -46.469, "C", "America/Sao_Paulo"),
    Station("amsterdam", "EHAM", "Amsterdam Schiphol Airport", 52.308, 4.764, "C", "Europe/Amsterdam"),
    Station("wuhan", "ZHHH", "Wuhan Tianhe Intl Airport", 30.784, 114.208, "C", "Asia/Shanghai"),
    Station("warsaw", "EPWA", "Warsaw Chopin Airport", 52.166, 20.967, "C", "Europe/Warsaw"),
    Station("san-francisco", "KSFO", "San Francisco Intl Airport", 37.619, -122.375, "F", "America/Los_Angeles"),
    Station("tel-aviv", "LLBG", "Ben Gurion Intl Airport", 32.011, 34.887, "C", "Asia/Jerusalem"),
    Station("moscow", "UUWW", "Moscow Vnukovo Intl Airport", 55.591, 37.261, "C", "Europe/Moscow"),
    Station("kuala-lumpur", "WMKK", "Kuala Lumpur Intl Airport", 2.746, 101.710, "C", "Asia/Kuala_Lumpur"),
    Station("denver", "KBKF", "Buckley Space Force Base", 39.702, -104.752, "F", "America/Denver"),
    Station("busan", "RKPK", "Gimhae Intl Airport", 35.179, 128.938, "C", "Asia/Seoul"),
    Station("lucknow", "VILK", "Chaudhary Charan Singh Intl Airport", 26.761, 80.889, "C", "Asia/Kolkata"),
    Station("austin", "KAUS", "Austin-Bergstrom Intl Airport", 30.194, -97.670, "F", "America/Chicago"),
    Station("helsinki", "EFHK", "Helsinki-Vantaa Airport", 60.317, 24.963, "C", "Europe/Helsinki"),
    Station("istanbul", "LTFM", "Istanbul Airport", 41.262, 28.742, "C", "Europe/Istanbul"),
    Station("houston", "KHOU", "William P. Hobby Airport", 29.645, -95.279, "F", "America/Chicago"),
    Station("cape-town", "FACT", "Cape Town Intl Airport", -33.965, 18.602, "C", "Africa/Johannesburg"),
    Station("qingdao", "ZSQD", "Qingdao Jiaodong Intl Airport", 36.362, 120.088, "C", "Asia/Shanghai"),
    Station("manila", "RPLL", "Ninoy Aquino Intl Airport", 14.509, 121.020, "C", "Asia/Manila"),
    Station("jeddah", "OEJN", "King Abdulaziz Intl Airport", 21.680, 39.157, "C", "Asia/Riyadh"),
    Station("mexico-city", "MMMX", "Benito Juarez Intl Airport", 19.436, -99.072, "C", "America/Mexico_City"),
    Station("karachi", "OPKC", "Jinnah Intl Airport", 24.907, 67.161, "C", "Asia/Karachi"),
    Station("zhengzhou", "ZHCC", "Zhengzhou Xinzheng Intl Airport", 34.520, 113.841, "C", "Asia/Shanghai"),
    Station("jinan", "ZSJN", "Jinan Yaoqiang Intl Airport", 36.857, 117.216, "C", "Asia/Shanghai"),
    Station("panama", "MPMG", "Marcos A. Gelabert Intl Airport", 8.973, -79.556, "C", "America/Panama"),
    Station("panama-city", "MPMG", "Marcos A. Gelabert Intl Airport", 8.973, -79.556, "C", "America/Panama"),
    Station("jakarta", "WIHH", "Halim Perdanakusuma Intl Airport", -6.267, 106.891, "C", "Asia/Jakarta"),
    Station("lagos", "DNMM", "Murtala Muhammed Intl Airport", 6.577, 3.321, "C", "Africa/Lagos"),
    Station("phoenix", "KPHX", "Phoenix Sky Harbor Intl Airport", 33.434, -112.012, "F", "America/Phoenix"),
]}

_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


def city_of(series_slug: str) -> tuple[str, str] | None:
    """('nyc', 'high') from 'nyc-daily-weather'; ('nyc', 'low') from 'nyc-daily-lowest-temperature'."""
    if series_slug.endswith("-daily-weather"):
        return series_slug[: -len("-daily-weather")], "high"
    if series_slug.endswith("-daily-lowest-temperature"):
        return series_slug[: -len("-daily-lowest-temperature")], "low"
    if series_slug.endswith("-daily-highest-temperature"):
        return series_slug[: -len("-daily-highest-temperature")], "high"
    return None


def parse_bracket(question: str) -> tuple[int | None, int | None, str] | None:
    """(lo, hi, unit) of an inclusive integer bracket; None for an open end.

    "63°F or below" -> (None, 63, 'F'); "between 64-65°F" -> (64, 65, 'F');
    "82°F or higher" -> (82, None, 'F'); "be 26°C" -> (26, 26, 'C').
    """
    q = question.replace("−", "-").replace("–", "-")
    unit_m = re.search(r"°\s*([FC])", q)
    if not unit_m:
        return None
    unit = unit_m.group(1)
    m = re.search(r"between\s+(-?\d+)\s*-\s*(-?\d+)\s*°", q)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b), unit)
    m = re.search(r"(-?\d+)\s*°\s*[FC]\s+or\s+(below|lower|less)", q)
    if m:
        return (None, int(m.group(1)), unit)
    m = re.search(r"(-?\d+)\s*°\s*[FC]\s+or\s+(higher|above|more)", q)
    if m:
        return (int(m.group(1)), None, unit)
    m = re.search(r"be\s+(-?\d+)\s*°\s*[FC]", q)
    if m:
        v = int(m.group(1))
        return (v, v, unit)
    return None


def target_date(event: PolyEvent) -> date | None:
    """The local calendar day named in the title, e.g. 'Highest temperature in NYC on September 26?'."""
    m = re.search(r"\bon\s+([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", event.title)
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    month = _MONTHS[m.group(1).lower()]
    day = int(m.group(2))
    if m.group(3):
        year = int(m.group(3))
    else:
        anchor = event.end_date or event.created_at
        if anchor is None:
            return None
        year = anchor.year
        # a January market created in December
        if month == 1 and anchor.month == 12:
            year += 1
        if month == 12 and anchor.month == 1:
            year -= 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def cutoff_utc(station: Station, day: date, hour: int) -> datetime:
    """`hour` o'clock local time on the target day (0 = the moment the day starts) as UTC."""
    local = datetime.combine(day, time(hour=hour), tzinfo=ZoneInfo(station.tz))
    return local.astimezone(timezone.utc)


@dataclass
class BracketMarket:
    city: str
    kind: str  # high | low
    day: date
    lo: int | None
    hi: int | None
    unit: str
    market: PolyMarket
    event: PolyEvent

    @property
    def resolved_yes(self) -> bool | None:
        return self.market.resolved_yes

    @property
    def midpoint(self) -> float:
        """Representative temperature for the bracket (bound +-0.5 when open-ended)."""
        if self.lo is None and self.hi is None:
            return float("nan")
        if self.lo is None:
            return self.hi - 0.5  # type: ignore[operator]
        if self.hi is None:
            return self.lo + 0.5
        return (self.lo + self.hi) / 2

    def label(self) -> str:
        if self.lo is None:
            return f"<={self.hi}{self.unit}"
        if self.hi is None:
            return f">={self.lo}{self.unit}"
        return f"{self.lo}{self.unit}" if self.lo == self.hi else f"{self.lo}-{self.hi}{self.unit}"


def bracket_markets(events: Iterable[PolyEvent], cities: set[str] | None = None, kinds: set[str] = frozenset({"high"})) -> list[BracketMarket]:
    out: list[BracketMarket] = []
    for ev in events:
        ck = city_of(ev.series_slug)
        if not ck or ck[1] not in kinds:
            continue
        city, kind = ck
        if cities and city not in cities:
            continue
        day = target_date(ev)
        if day is None:
            continue
        for m in ev.markets:
            b = parse_bracket(m.question)
            if not b or not m.is_binary:
                continue
            out.append(BracketMarket(city, kind, day, b[0], b[1], b[2], m, ev))
    return out


def group_by_event(rows: list[BracketMarket]) -> dict[str, list[BracketMarket]]:
    g: dict[str, list[BracketMarket]] = defaultdict(list)
    for r in rows:
        g[r.event.id].append(r)
    return g


def actual_temperature(rows: list[BracketMarket]) -> float | None:
    """The resolved bracket's midpoint for one event's brackets, if exactly one won."""
    winners = [r for r in rows if r.resolved_yes is True]
    if len(winners) != 1:
        return None
    return winners[0].midpoint


# --------------------------------------------------------------------------- #
# Trade history -> price at a given time
# --------------------------------------------------------------------------- #

def yes_price_series(trades: list[dict[str, Any]]) -> list[tuple[int, float]]:
    """(timestamp, YES price) sorted by time. A trade on the No token at p is a YES trade at 1-p."""
    pts: list[tuple[int, float]] = []
    for t in trades:
        try:
            ts = int(t["timestamp"])
            p = float(t["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if str(t.get("outcome", "Yes")).lower() == "no" or t.get("outcomeIndex") == 1:
            p = 1.0 - p
        pts.append((ts, p))
    pts.sort()
    return pts


def price_at(series: list[tuple[int, float]], when: datetime) -> float | None:
    ts = int(when.timestamp())
    last = None
    for t, p in series:
        if t > ts:
            break
        last = p
    return last


class LiveTrades:
    """data-api.polymarket.com/trades with an on-disk cache (trade history is immutable once a
    market is closed, and each market is one request, so cache aggressively)."""

    def __init__(self, http: HttpClient | None = None, cache_dir: Path | None = None, pause: float = 0.6) -> None:
        self.http = http or HttpClient(timeout=60.0)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.pause = pause

    def trades(self, condition_id: str, closed: bool = True) -> list[dict[str, Any]]:
        path = self.cache_dir / f"{condition_id}.json" if self.cache_dir else None
        if path and path.exists():
            return json.loads(path.read_text())
        import time as _time

        data = self.http.get_json(f"{DATA_API}/trades", params={"market": condition_id, "limit": 10000})
        rows = [{"timestamp": t.get("timestamp"), "price": t.get("price"), "size": t.get("size"), "outcome": t.get("outcome"), "side": t.get("side")} for t in (data if isinstance(data, list) else [])]
        if path and closed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, separators=(",", ":")))
        _time.sleep(self.pause)
        return rows


class FixtureTrades:
    def __init__(self, directory: Path) -> None:
        self.data = json.loads((Path(directory) / "weather_trades.json").read_text())

    def trades(self, condition_id: str, closed: bool = True) -> list[dict[str, Any]]:
        return self.data.get(condition_id, [])


# --------------------------------------------------------------------------- #
# Forecast distribution
# --------------------------------------------------------------------------- #

def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bracket_probability(lo: int | None, hi: int | None, mu: float, sd: float) -> float:
    """P(round(T) in [lo, hi]) for T ~ N(mu, sd); the rounding makes the bracket [lo-0.5, hi+0.5)."""
    sd = max(sd, 1e-6)
    upper = 1.0 if hi is None else _phi((hi + 0.5 - mu) / sd)
    lower = 0.0 if lo is None else _phi((lo - 0.5 - mu) / sd)
    return max(0.0, min(1.0, upper - lower))


@dataclass
class Calib:
    """Per-station forecast error model: actual = forecast + bias + N(0, sd)."""

    bias: float = 0.0
    sd: float = 2.0
    n: int = 0

    @classmethod
    def fit(cls, residuals: list[float], prior_sd: float, min_n: int = 8, bracket_width: float = 1.0, window: int = 0) -> "Calib":
        """Fit bias and sd from past residuals (rounded actual minus continuous forecast).

        `window` > 0 uses only the most recent residuals, so a seasonal drift in the station
        bias does not linger. The residuals already contain the rounding noise of the settled
        integer (variance width^2/12), so that is removed to recover the spread of the
        underlying temperature that `bracket_probability` integrates; the floor keeps a run
        of lucky days from collapsing the distribution onto one bracket."""
        if window > 0:
            residuals = residuals[-window:]
        if len(residuals) < max(min_n, 2):
            return cls(0.0, prior_sd, len(residuals))
        bias = statistics.mean(residuals)
        var = statistics.variance(residuals) - bracket_width**2 / 12.0
        sd = math.sqrt(max(var, (0.3 * bracket_width) ** 2))
        return cls(bias, sd, len(residuals))


def blend(values: dict[str, float]) -> float | None:
    vals = [v for v in values.values() if v is not None and not math.isnan(v)]
    return statistics.mean(vals) if vals else None


class OpenMeteo:
    """Open-Meteo (free, no key). Hourly temperatures in the station's unit and local time;
    the daily max/min is taken over the 24 hourly values of each local date, which mirrors how
    the markets read hourly station observations."""

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(timeout=60.0)

    @staticmethod
    def _daily_extreme(payload: dict[str, Any], variable: str, models: tuple[str, ...], kind: str) -> dict[date, dict[str, float]]:
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        out: dict[date, dict[str, float]] = defaultdict(dict)
        for model in models:
            key = f"{variable}_{model}" if f"{variable}_{model}" in hourly else (variable if len(models) == 1 and variable in hourly else None)
            if key is None:
                continue
            per_day: dict[date, list[float]] = defaultdict(list)
            for t, v in zip(times, hourly.get(key) or []):
                if v is None:
                    continue
                per_day[date.fromisoformat(str(t)[:10])].append(float(v))
            for d, vals in per_day.items():
                if len(vals) >= 12:  # skip partial days
                    out[d][model] = max(vals) if kind == "high" else min(vals)
        return out

    def _params(self, station: Station, models: tuple[str, ...]) -> dict[str, Any]:
        return {
            "latitude": station.lat,
            "longitude": station.lon,
            "models": ",".join(models),
            "timezone": station.tz,
            "temperature_unit": "fahrenheit" if station.unit == "F" else "celsius",
        }

    def previous_runs(self, station: Station, start: date, end: date, models: tuple[str, ...] = DEFAULT_MODELS, lead_days: int = 1, kind: str = "high") -> dict[date, dict[str, float]]:
        """Forecast for each day in [start, end] as issued `lead_days` earlier (previous-runs API)."""
        variable = f"temperature_2m_previous_day{lead_days}"
        params = self._params(station, models) | {"hourly": variable, "start_date": start.isoformat(), "end_date": end.isoformat()}
        try:
            payload = self.http.get_json(OPEN_METEO_PREVIOUS_RUNS, params=params)
        except HttpError as exc:
            raise HttpError(f"Open-Meteo previous-runs failed for {station.key}: {exc}") from exc
        return self._daily_extreme(payload, variable, models, kind)

    def forecast(self, station: Station, days: int = 3, models: tuple[str, ...] = DEFAULT_MODELS, kind: str = "high") -> dict[date, dict[str, float]]:
        params = self._params(station, models) | {"hourly": "temperature_2m", "forecast_days": days}
        try:
            payload = self.http.get_json(OPEN_METEO_FORECAST, params=params)
        except HttpError as exc:
            raise HttpError(f"Open-Meteo forecast failed for {station.key}: {exc}") from exc
        return self._daily_extreme(payload, "temperature_2m", models, kind)

    def ensemble(self, station: Station, days: int = 3, model: str = "ecmwf_ifs025", kind: str = "high") -> dict[date, list[float]]:
        """Per-member daily extremes from an ensemble model, for a data-driven spread."""
        params = self._params(station, (model,)) | {"hourly": "temperature_2m", "forecast_days": days}
        try:
            payload = self.http.get_json(OPEN_METEO_ENSEMBLE, params=params)
        except HttpError as exc:
            raise HttpError(f"Open-Meteo ensemble failed for {station.key}: {exc}") from exc
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        out: dict[date, list[float]] = defaultdict(list)
        for key, vals in hourly.items():
            if not key.startswith("temperature_2m"):
                continue
            per_day: dict[date, list[float]] = defaultdict(list)
            for t, v in zip(times, vals or []):
                if v is not None:
                    per_day[date.fromisoformat(str(t)[:10])].append(float(v))
            for d, xs in per_day.items():
                if len(xs) >= 12:
                    out[d].append(max(xs) if kind == "high" else min(xs))
        return out


# --------------------------------------------------------------------------- #
# Mode 1: trend of the market's own mispricing
# --------------------------------------------------------------------------- #

@dataclass
class TrendRow:
    month: str
    events: int
    markets: int
    base_rate: float
    brier: float
    skill: float
    band_n: int
    band_bias: float  # hit rate minus price, 0.10 <= p < 0.60
    band_se: float  # standard error of band_bias; a bias inside +-2 se is noise
    band_net_per_share: float  # selling every band contract, after the taker fee
    longshot_bias: float | None  # 0.03 <= p < 0.20
    favourite_bias: float | None  # 0.60 <= p < 0.90


def summarise_pairs(month: str, events: int, pairs: list[tuple[float, int]], fee_rate: float) -> TrendRow | None:
    live = [(p, y) for p, y in pairs if 0.01 < p < 0.99]
    if len(live) < 30:
        return None
    base = statistics.mean(y for _, y in live)
    brier = statistics.mean((p - y) ** 2 for p, y in live)
    clim = statistics.mean((base - y) ** 2 for _, y in live)
    band = [(p, y) for p, y in live if 0.10 <= p < 0.60]
    ls = [(p, y) for p, y in live if 0.03 <= p < 0.20]
    fav = [(p, y) for p, y in live if 0.60 <= p < 0.90]
    return TrendRow(
        month=month,
        events=events,
        markets=len(live),
        base_rate=base,
        brier=brier,
        skill=1 - brier / clim if clim > 0 else 0.0,
        band_n=len(band),
        band_bias=statistics.mean(y - p for p, y in band) if band else float("nan"),
        band_se=(statistics.pstdev([y - p for p, y in band]) / math.sqrt(len(band))) if len(band) > 1 else float("nan"),
        band_net_per_share=statistics.mean(p - y - fee_rate * p * (1 - p) for p, y in band) if band else float("nan"),
        longshot_bias=statistics.mean(y - p for p, y in ls) if len(ls) >= 10 else None,
        favourite_bias=statistics.mean(y - p for p, y in fav) if len(fav) >= 10 else None,
    )


def trend(rows: list[BracketMarket], trades_source, *, cutoff_hour: int = 0, events_per_month: int = 25, fee_rate: float = 0.05, seed: int = 7, log=None) -> tuple[list[TrendRow], dict[str, list[tuple[float, int]]]]:
    """Sample resolved events per month, fetch every bracket's trades, take the YES price at
    `cutoff_hour` local on the target day, and summarise calibration and the sell-the-band P&L."""
    import random

    by_event = group_by_event([r for r in rows if r.event.closed])
    by_month: dict[str, list[list[BracketMarket]]] = defaultdict(list)
    for evrows in by_event.values():
        if actual_temperature(evrows) is None:
            continue
        by_month[evrows[0].day.strftime("%Y-%m")].append(evrows)
    rng = random.Random(seed)
    pairs_by_month: dict[str, list[tuple[float, int]]] = {}
    out: list[TrendRow] = []
    for month in sorted(by_month):
        sample = by_month[month]
        rng.shuffle(sample)
        sample = sample[:events_per_month]
        pairs: list[tuple[float, int]] = []
        for evrows in sample:
            st = STATIONS.get(evrows[0].city)
            if st is None:
                continue
            when = cutoff_utc(st, evrows[0].day, cutoff_hour)
            for r in evrows:
                series = yes_price_series(trades_source.trades(r.market.condition_id))
                p = price_at(series, when)
                if p is None or r.resolved_yes is None:
                    continue
                pairs.append((p, int(r.resolved_yes)))
            if log:
                log(f"{month} {evrows[0].event.title}: {len(pairs)} priced brackets so far")
        pairs_by_month[month] = pairs
        row = summarise_pairs(month, len(sample), pairs, fee_rate)
        if row:
            out.append(row)
    return out, pairs_by_month


def render_trend(rows: list[TrendRow], cutoff_hour: int) -> str:
    L = [
        f"Mispricing of daily-temperature brackets at {cutoff_hour:02d}:00 local on the target day, by month",
        "  band = contracts priced 10-60c; bias = hit rate minus price (negative: overpriced), with its standard error; net = P&L per share of selling all of them after the 5% fee",
        f"{'month':8} {'events':>6} {'mkts':>5} {'base':>5} {'Brier':>6} {'skill':>5} | {'band n':>6} {'bias':>7} {'+-se':>5} {'net/sh':>7} | {'3-20c bias':>10} {'60-90c bias':>11}",
    ]
    for r in rows:
        f = lambda v: f"{v:+.3f}" if v is not None else "   -  "  # noqa: E731
        L.append(f"{r.month:8} {r.events:6} {r.markets:5} {r.base_rate:5.2f} {r.brier:6.3f} {r.skill:5.2f} | {r.band_n:6} {r.band_bias:+7.3f} {r.band_se:5.3f} {r.band_net_per_share:+7.3f} | {f(r.longshot_bias):>10} {f(r.favourite_bias):>11}")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Mode 2: forecast backtest
# --------------------------------------------------------------------------- #

@dataclass
class BacktestDay:
    city: str
    day: date
    actual: float
    forecasts: dict[str, float]
    blend: float
    calib: Calib
    markets: list[dict[str, Any]] = field(default_factory=list)  # per bracket: label, p_market, p_model, y, action, pnl


RELIABILITY_BINS = (0.0, 0.05, 0.15, 0.30, 0.50, 0.70, 0.85, 0.95, 1.0001)


def reliability_table(pairs: list[tuple[float, int]]) -> list[dict[str, float]]:
    """Bin predicted probabilities and report how often the bracket actually won in each bin."""
    out = []
    for lo, hi in zip(RELIABILITY_BINS, RELIABILITY_BINS[1:]):
        inbin = [(p, y) for p, y in pairs if lo <= p < hi]
        if not inbin:
            continue
        out.append({"lo": lo, "hi": min(hi, 1.0), "n": len(inbin), "mean_p": statistics.mean(p for p, _ in inbin), "freq": statistics.mean(y for _, y in inbin)})
    return out


def _errors(res: list[float]) -> dict[str, float]:
    return {"n": len(res), "mae": statistics.mean(abs(x) for x in res), "bias": statistics.mean(res)}


def _se_of_sum(xs: list[float]) -> float | None:
    return statistics.pstdev(xs) * math.sqrt(len(xs)) if len(xs) >= 2 else None


def _se_of_mean(xs: list[float]) -> float | None:
    return statistics.pstdev(xs) / math.sqrt(len(xs)) if len(xs) >= 2 else None


@dataclass
class BacktestReport:
    cutoff_hour: int
    lead_days: int
    models: tuple[str, ...]
    edge: float
    fee_rate: float
    days: list[BacktestDay]
    model_errors: dict[str, dict[str, float]]  # per model, all stations pooled (mixed units): n, mae, bias
    market_brier: float | None
    model_brier: float | None
    n_scored: int
    trades: int
    pnl: float
    pnl_sell_band: float
    by_month: dict[str, dict[str, float]]
    calib_window: int = 0
    by_city: dict[str, dict[str, Any]] = field(default_factory=dict)  # unit, days, model_errors, brier, pnl, se
    reliability: dict[str, list[dict[str, float]]] = field(default_factory=dict)  # "model" / "market" bins
    pnl_se: float | None = None
    brier_diff_se: float | None = None  # se of mean(forecast_sq - market_sq); brackets of one day are correlated, so optimistic

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["days"] = [{**asdict(x), "day": x.day.isoformat()} for x in self.days]
        return d


def backtest(rows: list[BracketMarket], trades_source, forecast_source, *, cutoff_hour: int = 0, lead_days: int = 1, models: tuple[str, ...] = DEFAULT_MODELS, edge: float = 0.05, fee_rate: float = 0.05, min_calib_days: int = 8, calib_window: int = 0, extra: dict[tuple[str, date], dict[str, float]] | None = None, log=None) -> BacktestReport:
    by_event = group_by_event([r for r in rows if r.event.closed])
    # forecasts per city over the span of days seen
    per_city_days: dict[str, list[date]] = defaultdict(list)
    for evrows in by_event.values():
        if actual_temperature(evrows) is not None and evrows[0].city in STATIONS:
            per_city_days[evrows[0].city].append(evrows[0].day)
    forecasts: dict[tuple[str, date], dict[str, float]] = {}
    for city, days in per_city_days.items():
        st = STATIONS[city]
        kind = "high"
        fc = merge_extra(forecast_source.previous_runs(st, min(days), max(days), models, lead_days, kind), extra or {}, city)
        for d, vals in fc.items():
            forecasts[(city, d)] = vals
        if log:
            log(f"{city}: forecasts for {len(fc)} days from {', '.join(models)}")
    # walk days in order per city with an expanding calibration window
    days_out: list[BacktestDay] = []
    residuals: dict[str, list[float]] = defaultdict(list)
    model_res: dict[str, list[float]] = defaultdict(list)
    market_sq: list[float] = []
    model_sq: list[float] = []
    trade_pnls: list[float] = []
    rel_model: list[tuple[float, int]] = []
    rel_market: list[tuple[float, int]] = []
    pnl_band = 0.0
    by_month: dict[str, dict[str, float]] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "sell_band": 0.0, "scored": 0})
    # per-station accumulators (units differ between stations, so errors are never pooled across them in the report)
    c_model_res: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    c_blend_res: dict[str, list[float]] = defaultdict(list)
    c_market_sq: dict[str, list[float]] = defaultdict(list)
    c_model_sq: dict[str, list[float]] = defaultdict(list)
    c_pnls: dict[str, list[float]] = defaultdict(list)
    c_band: dict[str, float] = defaultdict(float)
    events_sorted = sorted((evrows for evrows in by_event.values() if actual_temperature(evrows) is not None and evrows[0].city in STATIONS), key=lambda e: (e[0].city, e[0].day))
    for evrows in events_sorted:
        city, day = evrows[0].city, evrows[0].day
        st = STATIONS[city]
        fvals = forecasts.get((city, day))
        actual = actual_temperature(evrows)
        if not fvals or actual is None:
            continue
        mu0 = blend(fvals)
        if mu0 is None:
            continue
        calib = Calib.fit(residuals[city], st.default_sd, min_calib_days, window=calib_window)
        mu = mu0 + calib.bias
        when = cutoff_utc(st, day, cutoff_hour)
        bd = BacktestDay(city, day, actual, dict(fvals), mu0, calib)
        month = day.strftime("%Y-%m")
        for r in sorted(evrows, key=lambda x: (x.lo if x.lo is not None else -999)):
            pm_series = yes_price_series(trades_source.trades(r.market.condition_id))
            p_market = price_at(pm_series, when)
            y = r.resolved_yes
            p_model = bracket_probability(r.lo, r.hi, mu, calib.sd)
            rec: dict[str, Any] = {"label": r.label(), "p_model": round(p_model, 4), "p_market": p_market, "y": int(y) if y is not None else None, "action": "", "pnl": 0.0}
            if p_market is not None and y is not None and calib.n >= min_calib_days:
                market_sq.append((p_market - y) ** 2)
                model_sq.append((p_model - y) ** 2)
                c_market_sq[city].append((p_market - y) ** 2)
                c_model_sq[city].append((p_model - y) ** 2)
                rel_model.append((p_model, y))
                rel_market.append((p_market, y))
                by_month[month]["scored"] += 1
                fee = fee_rate * p_market * (1 - p_market)
                if p_market - p_model > edge and p_market >= 0.05:
                    rec["action"] = "sell"
                    rec["pnl"] = p_market - y - fee
                elif p_model - p_market > edge and p_market <= 0.95:
                    rec["action"] = "buy"
                    rec["pnl"] = y - p_market - fee
                if rec["action"]:
                    trade_pnls.append(rec["pnl"])
                    c_pnls[city].append(rec["pnl"])
                    by_month[month]["trades"] += 1
                    by_month[month]["pnl"] += rec["pnl"]
                if 0.10 <= p_market < 0.60:
                    band = p_market - y - fee
                    pnl_band += band
                    c_band[city] += band
                    by_month[month]["sell_band"] += band
            bd.markets.append(rec)
        days_out.append(bd)
        residuals[city].append(actual - mu0)
        c_blend_res[city].append(actual - mu0)
        for mname, v in fvals.items():
            model_res[mname].append(actual - v)
            c_model_res[city][mname].append(actual - v)
    model_errors = {m: _errors(v) for m, v in model_res.items() if v}
    if len(model_res) > 1:
        bl = [d.actual - d.blend for d in days_out]
        if bl:
            model_errors["blend"] = _errors(bl)
    by_city: dict[str, dict[str, Any]] = {}
    for city in sorted(c_blend_res):
        errs = {m: _errors(v) for m, v in c_model_res[city].items() if v}
        if len(errs) > 1:
            errs["blend"] = _errors(c_blend_res[city])
        msq, fsq = c_market_sq[city], c_model_sq[city]
        by_city[city] = {
            "unit": STATIONS[city].unit, "days": len(c_blend_res[city]), "model_errors": errs,
            "n_scored": len(msq),
            "market_brier": statistics.mean(msq) if msq else None,
            "model_brier": statistics.mean(fsq) if fsq else None,
            "brier_diff_se": _se_of_mean([f - m for f, m in zip(fsq, msq)]),
            "trades": len(c_pnls[city]), "pnl": sum(c_pnls[city]), "pnl_se": _se_of_sum(c_pnls[city]),
            "sell_band": c_band[city],
        }
    return BacktestReport(
        cutoff_hour=cutoff_hour, lead_days=lead_days, models=models, edge=edge, fee_rate=fee_rate, days=days_out,
        model_errors=model_errors,
        market_brier=statistics.mean(market_sq) if market_sq else None,
        model_brier=statistics.mean(model_sq) if model_sq else None,
        n_scored=len(market_sq), trades=len(trade_pnls), pnl=sum(trade_pnls), pnl_sell_band=pnl_band, by_month=dict(by_month),
        calib_window=calib_window, by_city=by_city,
        reliability={"model": reliability_table(rel_model), "market": reliability_table(rel_market)},
        pnl_se=_se_of_sum(trade_pnls),
        brier_diff_se=_se_of_mean([f - m for f, m in zip(model_sq, market_sq)]),
    )


def _pm(x: float | None, se: float | None, fmt: str) -> str:
    if x is None:
        return "-"
    return f"{x:{fmt}}" + (f"±{se:{fmt.lstrip('+')}}" if se is not None else "")


def render_backtest(r: BacktestReport) -> str:
    win = f", calibration window {r.calib_window} days" if r.calib_window else ", expanding calibration window"
    L = [f"Weather backtest  cutoff {r.cutoff_hour:02d}:00 local, forecasts issued {r.lead_days} day(s) earlier, models {', '.join(r.models)}{win}", ""]
    L.append("Forecast error of the daily high per station (degrees in the station's own unit; never pooled across F and C):")
    for city, c in r.by_city.items():
        for m, e in c["model_errors"].items():
            L.append(f"  {city:12} {c['unit']}  {m:18} n={e['n']:4}  MAE={e['mae']:.2f}  bias={e['bias']:+.2f}")
    L.append("")
    L.append("Brier (lower is better) and paper P&L per station, after the first 8 calibration days:")
    L.append(f"  {'city':12} {'scored':>6} {'market':>7} {'forecast':>8} {'fc-mkt ±se':>16} {'trades':>6} {'pnl ±se':>14} {'sell band':>9}")
    for city, c in r.by_city.items():
        if c["market_brier"] is None:
            continue
        diff = c["model_brier"] - c["market_brier"]
        L.append(f"  {city:12} {c['n_scored']:6} {c['market_brier']:7.4f} {c['model_brier']:8.4f} {_pm(diff, c['brier_diff_se'], '+.4f'):>16} {c['trades']:6} {_pm(c['pnl'], c['pnl_se'], '+.2f'):>14} {c['sell_band']:+9.2f}")
    if r.market_brier is not None and r.model_brier is not None:
        diff = r.model_brier - r.market_brier
        verdict = "forecast beats market" if diff < 0 else "market beats forecast"
        L.append(f"  {'all':12} {r.n_scored:6} {r.market_brier:7.4f} {r.model_brier:8.4f} {_pm(diff, r.brier_diff_se, '+.4f'):>16} {r.trades:6} {_pm(r.pnl, r.pnl_se, '+.2f'):>14} {r.pnl_sell_band:+9.2f}   -> {verdict}")
    L.append(f"  (edge threshold {r.edge:.2f}, fee {r.fee_rate:.2f}; the se treats brackets as independent, so it is optimistic)")
    L.append("")
    if r.reliability.get("model"):
        L.append("Reliability: how often a bracket won when the forecast (left) or the market (right) gave it this probability:")
        L.append(f"  {'bin':>11} {'n':>5} {'mean p':>7} {'won':>6}   | {'n':>5} {'mean p':>7} {'won':>6}")
        mk = {(b["lo"], b["hi"]): b for b in r.reliability.get("market", [])}
        for b in r.reliability["model"]:
            m = mk.get((b["lo"], b["hi"]))
            right = f"{m['n']:5} {m['mean_p']:7.2f} {m['freq']:6.2f}" if m else f"{'-':>5} {'-':>7} {'-':>6}"
            L.append(f"  {b['lo']:.2f}-{b['hi']:.2f} {b['n']:5} {b['mean_p']:7.2f} {b['freq']:6.2f}   | {right}")
        L.append("")
    L.append(f"{'month':8} {'scored':>6} {'trades':>6} {'pnl':>8} {'sell band':>9}")
    for m in sorted(r.by_month):
        v = r.by_month[m]
        L.append(f"{m:8} {int(v['scored']):6} {int(v['trades']):6} {v['pnl']:+8.2f} {v['sell_band']:+9.2f}")
    L.append("")
    L.append("Last days (blend forecast -> calibrated mean, actual, and the brackets the rule would have traded):")
    for d in r.days[-8:]:
        acts = ", ".join(f"{m['label']} {m['action']} @{m['p_market']:.2f} vs model {m['p_model']:.2f} -> {m['pnl']:+.2f}" for m in d.markets if m["action"])
        L.append(f"  {d.city:12} {d.day}  blend {d.blend:5.1f} bias {d.calib.bias:+.1f} sd {d.calib.sd:.1f}  actual {d.actual:5.1f}  | {acts or 'no trade'}")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Mode 3: today
# --------------------------------------------------------------------------- #

@dataclass
class Quote:
    city: str
    day: date
    label: str
    p_model: float
    bid: float | None
    ask: float | None
    depth_bid: float
    depth_ask: float
    action: str
    price: float
    edge_per_share: float
    size: float
    url: str


def today(rows: list[BracketMarket], books: dict[str, Book], forecast_source, *, calib: dict[str, Calib] | None = None, models: tuple[str, ...] = DEFAULT_MODELS, ensemble: bool = False, budget: float = 300.0, edge: float = 0.04, maker_margin: float = 0.05, fee_rate: float | None = None, max_fraction: float = 0.10, extra: dict[tuple[str, date], dict[str, float]] | None = None, log=None) -> tuple[list[Quote], list[dict[str, Any]]]:
    """Forecast distribution for each open event vs. the live book. Takers first, then resting quotes."""
    open_rows = [r for r in rows if not r.event.closed and r.market.accepting_orders]
    by_event = group_by_event(open_rows)
    quotes: list[Quote] = []
    summaries: list[dict[str, Any]] = []
    cache: dict[str, dict[date, dict[str, float]]] = {}
    ens_cache: dict[str, dict[date, list[float]]] = {}
    for evrows in by_event.values():
        city, day = evrows[0].city, evrows[0].day
        st = STATIONS.get(city)
        if st is None:
            continue
        if city not in cache:
            cache[city] = merge_extra(forecast_source.forecast(st, 4, models, "high"), extra or {}, city)
            if ensemble:
                try:
                    ens_cache[city] = forecast_source.ensemble(st, 4, "ecmwf_ifs025", "high")
                except HttpError as exc:
                    if log:
                        log(str(exc))
                    ens_cache[city] = {}
        fvals = cache[city].get(day)
        if not fvals:
            continue
        mu0 = blend(fvals)
        if mu0 is None:
            continue
        c = (calib or {}).get(city, Calib(0.0, st.default_sd, 0))
        sd = c.sd
        members = ens_cache.get(city, {}).get(day) if ensemble else None
        if members and len(members) >= 10:
            sd = max(statistics.pstdev(members) * 1.2, 0.5 * st.default_sd)  # ensembles are under-dispersive
        mu = mu0 + c.bias
        rate = fee_rate if fee_rate is not None else polymarket_rate_for_event(evrows[0].event)
        summaries.append({"city": city, "day": day.isoformat(), "models": fvals, "blend": round(mu0, 2), "bias": round(c.bias, 2), "sd": round(sd, 2), "ensemble_members": len(members) if members else 0})
        for r in sorted(evrows, key=lambda x: (x.lo if x.lo is not None else -999)):
            p = bracket_probability(r.lo, r.hi, mu, sd)
            book = books.get(r.market.yes_token or "")
            bid = book.best_bid.price if book and book.best_bid else r.market.best_bid
            ask = book.best_ask.price if book and book.best_ask else r.market.best_ask
            dbid = sum(l.price * l.size for l in book.bids if l.price >= (bid or 0) - 0.02) if book else 0.0
            dask = sum(l.price * l.size for l in book.asks if l.price <= (ask or 1) + 0.02) if book else 0.0
            common = dict(city=city, day=day, label=r.label(), p_model=round(p, 4), bid=bid, ask=ask, depth_bid=round(dbid), depth_ask=round(dask), url=r.market.url)
            cap = budget * max_fraction
            if ask is not None and 0.05 <= ask <= 0.95:
                e = p - ask - polymarket_taker_fee(ask, 1.0, rate)
                if e >= edge:
                    k = max(0.0, (p - ask) / (1 - ask)) * 0.25
                    quotes.append(Quote(**common, action="buy YES (taker)", price=ask, edge_per_share=round(e, 4), size=round(min(cap, budget * k, dask or cap), 2)))
            if bid is not None and 0.05 <= bid <= 0.95:
                e = bid - p - polymarket_taker_fee(bid, 1.0, rate)
                if e >= edge:
                    k = max(0.0, (bid - p) / bid) * 0.25
                    quotes.append(Quote(**common, action="buy NO (taker)", price=round(1 - bid, 4), edge_per_share=round(e, 4), size=round(min(cap, budget * k, dbid or cap), 2)))
            if 0.05 <= p <= 0.95:
                yb = round(max(0.01, p - maker_margin), 3)
                if bid is None or yb > bid:
                    quotes.append(Quote(**common, action="post YES bid (maker)", price=yb, edge_per_share=round(p - yb, 4), size=round(min(cap, budget * 0.05), 2)))
                nb = round(max(0.01, 1 - (p + maker_margin)), 3)
                if ask is None or (1 - nb) < ask:
                    quotes.append(Quote(**common, action="post NO bid (maker)", price=nb, edge_per_share=round((p + maker_margin) - p, 4), size=round(min(cap, budget * 0.05), 2)))
    quotes.sort(key=lambda q: (q.action.startswith("post"), -q.edge_per_share))
    return quotes, summaries


def render_today(quotes: list[Quote], summaries: list[dict[str, Any]], budget: float) -> str:
    L = [f"Weather today  budget ${budget:.0f}  forecasts (blend of models, bias-corrected, sd) per open event:"]
    for s in summaries:
        L.append(f"  {s['city']:12} {s['day']}  " + "  ".join(f"{m}={v:.1f}" for m, v in s["models"].items()) + f"  -> blend {s['blend']:.1f} bias {s['bias']:+.1f} sd {s['sd']:.1f}" + (f"  ens={s['ensemble_members']}" if s["ensemble_members"] else ""))
    L.append("")
    if not quotes:
        L.append("no bracket clears the edge threshold")
        return "\n".join(L)
    L.append(f"{'city':12} {'day':10} {'bracket':10} {'model':>6} {'bid':>6} {'ask':>6} {'action':22} {'price':>6} {'edge':>6} {'size$':>6}  url")
    for q in quotes[:60]:
        fb = lambda v: f"{v:6.3f}" if v is not None else "     -"  # noqa: E731
        L.append(f"{q.city:12} {q.day} {q.label:10} {q.p_model:6.3f} {fb(q.bid)} {fb(q.ask)} {q.action:22} {q.price:6.3f} {q.edge_per_share:+6.3f} {q.size:6.0f}  {q.url}")
    return "\n".join(L)


def load_extra_forecasts(path: Path | None) -> dict[tuple[str, date], dict[str, float]]:
    """CSV of hand-logged forecasts: city,date,source,value (header optional). Values are in the
    city's unit. Lets a source with no archive (IMS for Tel Aviv, HKO, KMA...) be scored
    forward: log one number a day and it joins the model table on the same days."""
    out: dict[tuple[str, date], dict[str, float]] = defaultdict(dict)
    if not path or not Path(path).exists():
        return out
    for line in Path(path).read_text().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 4 or parts[0].lower() in ("city", "#", "") or parts[0].startswith("#"):
            continue
        try:
            out[(parts[0], date.fromisoformat(parts[1]))][parts[2]] = float(parts[3])
        except ValueError:
            continue
    return out


def merge_extra(forecasts: dict[date, dict[str, float]], extra: dict[tuple[str, date], dict[str, float]], city: str) -> dict[date, dict[str, float]]:
    merged: dict[date, dict[str, float]] = {d: dict(v) for d, v in forecasts.items()}
    for (c, d), vals in extra.items():
        if c == city:
            merged.setdefault(d, {}).update(vals)
    return merged


def parse_station_overrides(spec: str) -> None:
    """--station nyc=40.78,-73.87 (lat,lon) overrides coordinates in place."""
    for item in (spec or "").split(";"):
        if "=" not in item:
            continue
        key, coords = item.split("=", 1)
        lat, lon = (float(x) for x in coords.split(","))
        old = STATIONS.get(key.strip())
        if old:
            STATIONS[key.strip()] = Station(old.key, old.icao, old.name, lat, lon, old.unit, old.tz)
