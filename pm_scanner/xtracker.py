"""xtracker.polymarket.com, the post counter Polymarket resolves its posts-per-window ladders on.

The site has no documented API, but its own JavaScript calls a small JSON one (read off the
Next.js bundle on 3 Oct 2026):

    GET /api/users                                        the tracked accounts
    GET /api/users/<handle>                               one account: record, active trackings, total posts
    GET /api/users/<handle>/trackings                     the account's trackings (windows and market links)
    GET /api/trackings/<id>?includeStats=true             one tracking with its current count
    GET /api/users/<handle>/posts?limit=&startDate=&endDate=   the counted posts

Every response is `{"success": true, "data": ...}`. The per-card "Posts" download on the site is
built in the browser from the same posts, so this module is the export the site does not offer:
`harvest` walks an account's whole history in date chunks, halving a chunk whenever a response
looks cut off (the server's page cap is not documented either), and `write_posts_csv` writes the
one-row-per-post catalog that `counts` reads. The posts carry machine timestamps, so the time
zone question the browser export raises ("Posted At (EST)") does not arise here;
`compare_exports` checks the two against each other on the posts they share.
"""
from __future__ import annotations

import csv
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .counts import _parse_dt, _repair_row
from .http import HttpClient, HttpError

BASE_URL = "https://xtracker.polymarket.com"
UTC = timezone.utc

# The post's own time, in order of preference. A record's `createdAt` is often the database
# row's stamp (the import), so explicit post-time names win over it.
TIME_FIELDS = ("postedAt", "posted_at", "publishedAt", "published_at", "platformCreatedAt", "tweetCreatedAt",
               "originalCreatedAt", "postCreatedAt", "timestamp", "date", "time", "createdAt", "created_at")
IMPORT_FIELDS = ("importedAt", "imported_at", "syncedAt", "synced_at", "indexedAt", "updatedAt")
KIND_FIELDS = ("type", "kind", "postType", "post_type")
TEXT_FIELDS = ("content", "text", "body")
MAX_REQUESTS = 2000

FetchFn = Callable[[dict[str, Any]], tuple[list[dict[str, Any]], dict[str, Any]]]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(text: str) -> datetime:
    """`2025-11-01`, `2025-11-01T16:00`, or any ISO stamp, as an aware UTC datetime."""
    dt = _parse_dt(text)
    if dt is None:
        raise ValueError(f"unreadable date: {text!r}")
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def unwrap(payload: Any) -> tuple[Any, dict[str, Any]]:
    """`data` and everything beside it (pagination hints) from a `{"success", "data"}` envelope."""
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise HttpError(f"tracker said: {payload.get('error') or payload}")
        if "data" in payload:
            meta = {k: v for k, v in payload.items() if k not in ("data", "success")}
            data = payload["data"]
            if isinstance(data, dict) and isinstance(data.get("posts"), list):  # {"data": {"posts": [...], "pagination": {...}}}
                meta.update({k: v for k, v in data.items() if k != "posts"})
                data = data["posts"]
            return data, meta
    return payload, {}


def post_id(rec: dict[str, Any]) -> str:
    for k in ("platformId", "platform_id", "tweetId", "tweet_id", "statusId", "id"):
        v = rec.get(k)
        if v not in (None, ""):
            return str(v)
    return json.dumps(rec, sort_keys=True)[:80]


def time_field(rec: dict[str, Any]) -> str | None:
    for k in TIME_FIELDS:
        if rec.get(k) not in (None, "") and _parse_dt(str(rec[k])) is not None:
            return k
    return None


def post_time(rec: dict[str, Any], fld: str | None = None) -> datetime | None:
    fld = fld or time_field(rec)
    if fld is None:
        return None
    dt = _parse_dt(str(rec.get(fld, "")))
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _first(rec: dict[str, Any], keys: tuple[str, ...], skip: str | None = None) -> str:
    for k in keys:
        if k != skip and rec.get(k) not in (None, ""):
            return str(rec[k])
    return ""


def _walk(meta: Any, depth: int = 0):
    if isinstance(meta, dict):
        for k, v in meta.items():
            yield k, v
            if depth < 2 and isinstance(v, dict):
                yield from _walk(v, depth + 1)


def looks_truncated(n: int, limit: int, meta: dict[str, Any]) -> bool:
    """Did the server stop before the end? Page-size hit, or a pagination hint says more exist."""
    if limit and n >= limit:
        return True
    for k, v in _walk(meta):
        lk = k.lower()
        if lk in ("hasmore", "has_more", "hasnextpage", "has_next_page") and v:
            return True
        if lk in ("nextcursor", "next_cursor", "next", "nextpage", "next_page") and v not in (None, "", False, 0):
            return True
        if lk in ("total", "totalcount", "total_count", "count") and isinstance(v, (int, float)) and v > n:
            return True
        if lk in ("totalpages", "total_pages", "pages") and isinstance(v, (int, float)) and v > 1:
            return True
    return False


def next_cursor(meta: dict[str, Any]) -> Any:
    for k, v in _walk(meta):
        if k.lower() in ("nextcursor", "next_cursor", "cursor", "next") and v not in (None, "", False, 0):
            return v
    return None


@dataclass
class Harvest:
    posts: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: int = 0
    notes: list[str] = field(default_factory=list)
    time_field: str | None = None
    cap: int | None = None              # a page size the server imposed silently, once detected
    truncated_leaves: int = 0           # chunks at the minimum size that still looked cut off

    def add(self, recs: list[dict[str, Any]]) -> int:
        new = 0
        for r in recs:
            if not isinstance(r, dict):
                continue
            k = post_id(r)
            if k not in self.posts:
                self.posts[k] = r
                new += 1
            if self.time_field is None:
                self.time_field = time_field(r)
        return new

    def times(self) -> list[datetime]:
        out = []
        for r in self.posts.values():
            t = post_time(r, self.time_field)
            if t is not None:
                out.append(t)
        return sorted(out)


def harvest(fetch: FetchFn, start: datetime, end: datetime, *, chunk: timedelta = timedelta(days=7), limit: int = 500,
            min_chunk: timedelta = timedelta(hours=1), log: Callable[[str], None] | None = None) -> Harvest:
    """Every post with `start <= time < end`, by date-chunked requests.

    `fetch(params)` returns (records, meta) for one GET of the posts route. A chunk that comes
    back cut off (page size hit, a pagination hint, or a count equal to a cap the server has shown
    before) is split in two and both halves are fetched; the first non-empty chunk is also
    re-fetched as two halves once, to catch a server that clamps `limit` without saying so.
    Chunks overlap by a second so a post on a boundary is never lost (ids are deduplicated)."""
    h = Harvest()
    log = log or (lambda m: None)
    seen: Counter = Counter()
    calibrated = False
    cap_possible = True
    stack: list[tuple[datetime, datetime]] = []
    t = start
    while t < end:
        stack.append((t, min(t + chunk, end)))
        t += chunk
    stack.reverse()  # oldest first

    def get(a: datetime, b: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        recs, meta = fetch({"limit": limit, "startDate": _iso(a - timedelta(seconds=1)), "endDate": _iso(b + timedelta(seconds=1))})
        h.requests += 1
        return recs, meta

    def split(a: datetime, b: datetime) -> None:
        mid = a + (b - a) / 2
        stack.extend([(mid, b), (a, mid)])

    while stack:
        a, b = stack.pop()
        if h.requests >= MAX_REQUESTS:
            h.notes.append(f"stopped after {MAX_REQUESTS} requests with {len(stack) + 1} chunks left")
            break
        recs, meta = get(a, b)
        n = len(recs)
        if recs and h.requests == 1:
            inside = [r for r in recs if (pt := post_time(r)) is None or a - timedelta(days=1) <= pt <= b + timedelta(days=1)]
            if len(inside) < n * 0.9:
                h.notes.append("the server ignored startDate/endDate (posts far outside the asked window came back): paging instead")
                return _page_all(fetch, limit=limit, log=log, into=h)
        h.add(recs)
        seen[n] += 1
        cut = looks_truncated(n, limit, meta) or (h.cap is not None and n >= h.cap) or (cap_possible and h.cap is None and n >= 10 and n == max(seen) and seen[n] >= 2)
        if not calibrated and n >= 10 and not cut and b - a > 2 * min_chunk:
            calibrated = True
            mid = a + (b - a) / 2
            new_total = 0
            halves = []
            for x, y in ((a, mid), (mid, b)):
                sub, submeta = get(x, y)
                new_total += h.add(sub)
                halves.append((x, y, len(sub), submeta))
            if new_total > 0:
                h.cap = n
                h.notes.append(f"the server returns at most {n} posts per request whatever `limit` says; chunks are halved to stay under it")
                for x, y, m, submeta in halves:
                    if (looks_truncated(m, limit, submeta) or m >= h.cap) and y - x > min_chunk:
                        split(x, y)
                log(f"  {a:%Y-%m-%d %H:%M} .. {b:%Y-%m-%d %H:%M}  {n} posts, page cap detected")
                continue
            cap_possible = False
        if cut and b - a > min_chunk:
            split(a, b)
            log(f"  {a:%Y-%m-%d %H:%M} .. {b:%Y-%m-%d %H:%M}  {n} posts, cut off: splitting")
            continue
        if cut:
            h.truncated_leaves += 1
        log(f"  {a:%Y-%m-%d %H:%M} .. {b:%Y-%m-%d %H:%M}  {n} posts")
    if h.truncated_leaves:
        h.notes.append(f"{h.truncated_leaves} chunks of {min_chunk} still looked cut off; posts in those hours may be missing")
    return h


def _page_all(fetch: FetchFn, *, limit: int, log: Callable[[str], None], into: Harvest) -> Harvest:
    """Fallback when the date filter is ignored: walk the whole list by offset, page or cursor."""
    h = into
    recs, meta = fetch({"limit": limit})
    h.requests += 1
    h.add(recs)
    if not recs:
        return h
    page_size = len(recs)
    for strategy in ("offset", "page", "cursor"):
        got_any = False
        cursor = next_cursor(meta)
        k = 1
        while h.requests < MAX_REQUESTS:
            if strategy == "offset":
                params = {"limit": limit, "offset": len(h.posts)}
            elif strategy == "page":
                params = {"limit": limit, "page": k + 1}
            else:
                if cursor is None:
                    break
                params = {"limit": limit, "cursor": cursor}
            recs, meta = fetch(params)
            h.requests += 1
            new = h.add(recs)
            log(f"  {strategy} {params.get('offset', params.get('page', ''))}: {len(recs)} posts, {new} new")
            if new == 0:
                break
            got_any = True
            k += 1
            cursor = next_cursor(meta)
            if len(recs) < page_size:
                break
        if got_any:
            h.notes.append(f"paged by {strategy}")
            return h
    h.notes.append("no paging parameter worked (offset, page, cursor): only the first page was read")
    return h


def write_posts_csv(h: Harvest, path: Path) -> int:
    """One row per post, oldest first: post_id,time,imported_at,kind,content. `counts` reads `time` (UTC)."""
    rows = []
    for k, r in h.posts.items():
        t = post_time(r, h.time_field)
        if t is None:
            continue
        rows.append((t, k, _first(r, IMPORT_FIELDS, skip=h.time_field), _first(r, KIND_FIELDS), _first(r, TEXT_FIELDS)))
    rows.sort()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["post_id", "time", "imported_at", "kind", "content"])
        for t, k, imp, kind, text in rows:
            w.writerow([k, _iso(t), imp, kind, text])
    return len(rows)


def compare_exports(h: Harvest, files: list[Path], *, zone: str = "America/New_York") -> str:
    """Line up the browser export's `Posted At (EST)` with the API's time on the posts both hold."""
    tz = ZoneInfo(zone)
    diffs: Counter = Counter()
    shared = 0
    for fp in files:
        with fp.open(newline="", encoding="utf-8-sig") as f:
            rd = csv.reader(f)
            header = next(rd, None)
            if not header:
                continue
            cols = {c.strip().lower(): i for i, c in enumerate(header)}
            ic = next((cols[c] for c in ("post id", "post_id", "id") if c in cols), None)
            tc = next((i for c, i in cols.items() if "posted" in c), None)
            if ic is None or tc is None:
                continue
            for row in rd:
                row = _repair_row(row, len(header))
                if len(row) <= max(ic, tc):
                    continue
                rec = h.posts.get(row[ic].strip())
                if rec is None:
                    continue
                api_t = post_time(rec, h.time_field)
                csv_dt = _parse_dt(row[tc])
                if api_t is None or csv_dt is None:
                    continue
                csv_t = csv_dt.replace(tzinfo=tz) if csv_dt.tzinfo is None else csv_dt
                shared += 1
                diffs[round((api_t - csv_t).total_seconds() / 60)] += 1
    if not shared:
        return "export vs API: no shared post ids"
    top, n = diffs.most_common(1)[0]
    if len(diffs) == 1 and top == 0:
        return f"export vs API: all {shared} shared posts agree to the minute when `Posted At (EST)` is read as {zone}; the API's `{h.time_field}` is the post time"
    return f"export vs API: {shared} shared posts, API minus export ({zone}) = {top} min for {n} of them" + (f", other offsets {dict(diffs.most_common(4)[1:])}" if len(diffs) > 1 else "") + " (a constant offset means the export's label is a fixed zone, or the API field is not the post time)"


class Tracker:
    """The JSON routes, over the repo's HttpClient."""

    def __init__(self, http: HttpClient | None = None, base_url: str = BASE_URL, pause: float = 0.15) -> None:
        self.http = http or HttpClient(timeout=30.0)
        self.base_url = base_url.rstrip("/")
        self.pause = pause

    def get(self, route: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
        if self.pause:
            time.sleep(self.pause)
        return unwrap(self.http.get_json(f"{self.base_url}{route}", params=params))

    def users(self) -> list[dict[str, Any]]:
        data, _ = self.get("/api/users")
        return data if isinstance(data, list) else []

    def user(self, handle: str) -> dict[str, Any]:
        data, _ = self.get(f"/api/users/{handle}")
        return data if isinstance(data, dict) else {}

    def trackings(self, handle: str) -> list[dict[str, Any]]:
        data, _ = self.get(f"/api/users/{handle}/trackings")
        return data if isinstance(data, list) else []

    def posts_fetcher(self, handle: str, *, raw_dir: Path | None = None) -> FetchFn:
        route = f"/api/users/{handle}/posts"
        state = {"limit_ok": True}

        def fetch(params: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            p = dict(params)
            if not state["limit_ok"]:
                p["limit"] = min(int(p.get("limit", 100)), 100)
            try:
                data, meta = self.get(route, p)
            except HttpError as exc:
                if "HTTP 400" in str(exc) and state["limit_ok"] and int(p.get("limit", 0)) > 100:
                    state["limit_ok"] = False  # the server refused the page size: ask for 100 from now on
                    return fetch(params)
                raise
            recs = data if isinstance(data, list) else []
            if raw_dir is not None:
                raw_dir.mkdir(parents=True, exist_ok=True)
                name = re.sub(r"[^A-Za-z0-9]+", "_", "_".join(f"{k}-{v}" for k, v in sorted(p.items())))[:150] or "page"
                (raw_dir / f"{name}.json").write_text(json.dumps({"params": p, "meta": meta, "data": recs}, indent=1))
            return recs, meta

        return fetch


def weekly_table(times: list[datetime], weeks: int = 8) -> str:
    """Posts per Monday-to-Monday UTC week for the last few whole weeks (a sanity pace, not the market's windows)."""
    if not times:
        return ""
    last = times[-1]
    monday = (last - timedelta(days=last.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    lines = []
    for k in range(weeks, 0, -1):
        a, b = monday - timedelta(days=7 * k), monday - timedelta(days=7 * (k - 1))
        if b <= times[0]:
            continue  # before the history starts
        n = sum(1 for t in times if a <= t < b)
        lines.append(f"  {a:%Y-%m-%d} .. {b:%Y-%m-%d}  {n}")
    return "\n".join(lines)
