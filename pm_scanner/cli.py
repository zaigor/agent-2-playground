from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .fees import kalshi_maker_fee, kalshi_taker_fee, polymarket_taker_fee
from .israel import DEFAULT_SURPLUS_PAIRS, ErrorModel, render_israel, run_israel
from .ladder import render_ladder_report, scan_ladders, summarize_snapshots
from .history import week_fetcher
from .lp import LiveExchange, PaperExchange, Quoter, cheap_reason, choose_markets, depth_blockers, exit_blockers, history_blockers, merge_preview, parse_only, positions_report, render_plan, render_positions
from .rewards import gamma_markets_by_condition, render_pocket, render_rewards, rewards_pocket, rewards_survey
from .signal import FixtureResolver, GammaResolver, load_signal_csv, render_signal, score_signal
from .flow import MakerConfig, family_flow, maker_backtest, render_family_flow, render_maker, sampled_market_ids
from .niches import DEFAULT_SURVEY_TAGS, UP_OR_DOWN_TAG_ID, default_since, load_survey_tags, render_survey, survey
from .report import render_text, write_json
from .scans import (
    Opportunity,
    evaluate_cross,
    evaluate_negrisk,
    find_negrisk_candidates,
    match_cross_platform,
    scan_kalshi_mutex,
    scan_near_certain,
    utcnow,
)
from .sources import FixtureSource, LiveSource
from .weather import (
    backtest,
    bracket_markets,
    Calib,
    DEFAULT_MODELS,
    FixtureObs,
    flow,
    FixtureTrades,
    IemObs,
    intraday,
    LiveTrades,
    load_extra_forecasts,
    OpenMeteo,
    parse_station_overrides,
    render_backtest,
    render_flow,
    render_intraday,
    render_today,
    render_trend,
    STATIONS,
    today,
    trend,
)
from .watch import ARB_KINDS, JsonlLog, TelegramNotifier, render_summary, summarize, watch

DEFAULT_FLOW_TAGS = DEFAULT_SURVEY_TAGS + ("sports", "esports", "crypto")


def run_scan(
    source,
    *,
    budget: float,
    min_edge: float,
    max_events: int,
    platform: str,
    near_min_price: float,
    near_max_days: int,
    poly_fee_rate: float | None,
    kalshi_multiplier: float,
    do_cross: bool,
    now: datetime,
    max_days: float | None = None,
    allow_augmented: bool = True,
    min_annualized: float = 0.0,
) -> tuple[list[Opportunity], dict]:
    """`max_days`, `allow_augmented` and `min_annualized` keep only arbitrage that pays
    back soon and cannot have its payout changed later; near_certain keeps its own
    `near_max_days` window."""
    stats: dict = {}
    opps: list[Opportunity] = []
    events = []
    kmarkets = []

    if platform in ("polymarket", "both"):
        events = source.poly_events(max_events)
        stats["poly_events"] = len(events)
        stats["poly_markets"] = sum(len(e.markets) for e in events)
        cands = find_negrisk_candidates(events, min_edge, poly_fee_rate)
        if not allow_augmented:
            # Polymarket may add outcomes later, so "buy every YES" can stop covering the winner.
            # "Buy every NO" is unaffected: it pays N-1 if a listed outcome wins and N if a new one does.
            cands = [(ev, kind) for ev, kind in cands if not (ev.neg_risk_augmented and kind == "negrisk_buy_all_yes")]
        stats["negrisk_candidates"] = len(cands)
        token_ids: list[str] = []
        for ev, kind in cands:
            for m in ev.markets:
                t = m.yes_token if kind.endswith("yes") else m.no_token
                if t:
                    token_ids.append(t)
        books = source.poly_books(token_ids) if token_ids else {}
        for ev, kind in cands:
            o = evaluate_negrisk(ev, kind, books, budget, poly_fee_rate)
            if o and o.edge_per_set >= min_edge:
                opps.append(o)
        opps.extend(scan_near_certain(events, budget, now, near_min_price, near_max_days, poly_fee_rate))

    if platform in ("kalshi", "both"):
        kevents = source.kalshi_events()
        stats["kalshi_events"] = len(kevents)
        kmarkets = [m for e in kevents for m in e.markets]
        if not kmarkets:
            kmarkets = source.kalshi_markets()
        stats["kalshi_markets"] = len(kmarkets)
        opps.extend(scan_kalshi_mutex(kevents, budget, min_edge, kalshi_multiplier, source.kalshi_orderbook))

    if platform == "both" and do_cross and events and kmarkets:
        matches = match_cross_platform(events, kmarkets)
        stats["cross_matches"] = len(matches)
        for mt in matches:
            quick = evaluate_cross(mt, budget, poly_fee_rate, kalshi_multiplier)
            if any(o.edge_per_set >= min_edge - 0.01 for o in quick):
                kb = source.kalshi_orderbook(mt.kalshi.ticker)
                for o in evaluate_cross(mt, budget, poly_fee_rate, kalshi_multiplier, kb):
                    if o.edge_per_set >= min_edge:
                        opps.append(o)

    for o in opps:
        o.set_horizon(now)
    arb_found = [o for o in opps if o.kind in ARB_KINDS]
    kept = [o for o in arb_found if _within_horizon(o, max_days, min_annualized)]
    if len(kept) != len(arb_found):
        stats["dropped_slow_or_unknown_date"] = len(arb_found) - len(kept)
    opps = kept + [o for o in opps if o.kind not in ARB_KINDS]
    opps.sort(key=lambda o: (o.kind == "near_certain", -o.est_profit))
    return opps, stats


def _within_horizon(o: Opportunity, max_days: float | None, min_annualized: float) -> bool:
    if max_days is not None:
        if o.days_to_resolve is None or not 0 <= o.days_to_resolve <= max_days:
            return False
    return min_annualized <= 0 or (o.annualized is not None and o.annualized >= min_annualized)


def _add_scan_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--platform", choices=["polymarket", "kalshi", "both"], default="polymarket")
    s.add_argument("--budget", type=float, default=50.0, help="cash available, used to size expected profit")
    s.add_argument("--min-edge", type=float, default=0.005, help="minimum net edge per $1-payout set")
    s.add_argument("--max-events", type=int, default=3000, help="Polymarket events to pull (by 24h volume)")
    s.add_argument("--near-certain-min-price", type=float, default=0.95)
    s.add_argument("--near-certain-max-days", type=int, default=14)
    s.add_argument("--poly-fee-rate", type=float, default=None, help="override Polymarket taker rate for all events")
    s.add_argument("--kalshi-fee-multiplier", type=float, default=1.0)
    s.add_argument("--max-days", type=float, default=30.0, help="skip arbitrage resolving later than this (0 = no limit)")
    s.add_argument("--min-annualized", type=float, default=0.0, help="skip arbitrage below this yearly return, e.g. 0.2 = 20%%/yr")
    s.add_argument("--allow-augmented", action="store_true", help="keep events where Polymarket may add outcomes later")
    s.add_argument("--no-cross", action="store_true", help="skip Polymarket<->Kalshi matching")
    s.add_argument("--fixtures", type=Path, default=None, help="offline: read recorded API responses from DIR")


def _scan_kwargs(args: argparse.Namespace) -> dict:
    return dict(
        budget=args.budget,
        min_edge=args.min_edge,
        max_events=args.max_events,
        platform=args.platform,
        near_min_price=args.near_certain_min_price,
        near_max_days=args.near_certain_max_days,
        poly_fee_rate=args.poly_fee_rate,
        kalshi_multiplier=args.kalshi_fee_multiplier,
        do_cross=not args.no_cross,
        max_days=args.max_days if args.max_days > 0 else None,
        allow_augmented=args.allow_augmented,
        min_annualized=args.min_annualized,
    )


def _run_weather(args, source) -> int:
    from datetime import timedelta

    parse_station_overrides(args.station)
    cities = None if args.cities.strip().lower() == "all" else {c.strip() for c in args.cities.split(",") if c.strip()}
    unknown = sorted(c for c in (cities or set()) if c not in STATIONS)
    if unknown:
        print(f"unknown city keys: {', '.join(unknown)}; known: {', '.join(sorted(STATIONS))}", file=sys.stderr)
        return 2
    now = utcnow()
    since = now - timedelta(days=args.days)
    models = tuple(m.strip() for m in args.models.split(",") if m.strip())
    log = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731
    try:
        events = source.poly_weather_events(since, now)
        rows = bracket_markets(events, cities)
        log(f"{len(events)} weather events loaded, {len(rows)} brackets in {len({r.city for r in rows})} cities")
        trades_src = FixtureTrades(args.fixtures) if args.fixtures else LiveTrades(cache_dir=args.cache_dir)
        if args.mode == "trend":
            table, pairs = trend(rows, trades_src, cutoff_hour=args.cutoff_hour, events_per_month=args.events_per_month, fee_rate=args.fee, log=log)
            print(render_trend(table, args.cutoff_hour))
            if args.json:
                args.json.write_text(json.dumps({"rows": [r.__dict__ for r in table], "pairs": pairs}, indent=1))
                print(f"\nwrote {args.json}")
            return 0
        if args.mode == "flow":
            report = flow(rows, trades_src, horizon_min=args.horizon_min, fee_rate=args.fee, log=log)
            print(render_flow(report))
            if args.json:
                args.json.write_text(json.dumps(report.to_dict(), indent=1, default=str))
                print(f"\nwrote {args.json}")
            return 0
        forecast_src = OpenMeteo()
        extra = load_extra_forecasts(args.extra_forecasts)
        if extra:
            log(f"{sum(len(v) for v in extra.values())} hand-logged forecasts loaded from {args.extra_forecasts}")
        if args.mode == "backtest":
            report = backtest(rows, trades_src, forecast_src, cutoff_hour=args.cutoff_hour, lead_days=args.lead, models=models, edge=args.edge, fee_rate=args.fee, calib_window=args.calib_window, extra=extra, log=log)
            print(render_backtest(report))
            if args.json:
                args.json.write_text(json.dumps(report.to_dict(), indent=1, default=str))
                print(f"\nwrote {args.json}")
            return 0
        if args.mode == "intraday":
            hours = tuple(int(h) for h in args.hours.split(",") if h.strip())
            obs_src = FixtureObs(args.fixtures) if args.fixtures else IemObs(cache_dir=args.obs_cache, report_types=(3, 4) if args.include_specials else (3,))
            report = intraday(rows, trades_src, forecast_src, obs_src, hours=hours, latency_min=args.latency_min, lead_days=args.lead, models=models, edge=args.edge, fee_rate=args.fee, calib_window=args.calib_window, extra=extra, log=log)
            print(render_intraday(report))
            if args.json:
                args.json.write_text(json.dumps(report.to_dict(), indent=1, default=str))
                print(f"\nwrote {args.json}")
            return 0
        calib = None
        if args.calib and args.calib.exists():
            data = json.loads(args.calib.read_text())
            calib = {}
            for d in data.get("days", []):
                c = d.get("calib") or {}
                calib[d["city"]] = Calib(c.get("bias", 0.0), c.get("sd", 2.0), c.get("n", 0))  # last day wins
        books = source.poly_books([r.market.yes_token for r in rows if not r.event.closed and r.market.yes_token])
        quotes, summaries = today(rows, books, forecast_src, calib=calib, models=models, ensemble=args.ensemble, budget=args.budget, edge=args.edge, maker_margin=args.maker_margin, fee_rate=None, extra=extra, log=log)
        print(render_today(quotes, summaries, args.budget))
        if args.json:
            args.json.write_text(json.dumps({"quotes": [q.__dict__ for q in quotes], "forecasts": summaries}, indent=1, default=str))
            print(f"\nwrote {args.json}")
        return 0
    except Exception as exc:
        print(f"weather failed: {exc}", file=sys.stderr)
        return 2


def _run_counts(args, source) -> int:
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    from .counts import build_signal_rows, catalog_files, check_catalog, count_markets, fetch_series_events, load_catalog, write_signal_csv

    log = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731
    if not catalog_files(args.catalog):
        what = "has no CSV files yet" if Path(args.catalog).is_dir() else "not found"
        print(f"catalog {args.catalog}: {what} (export the series' data first, then pass the CSV, a folder of CSVs or a glob here)", file=sys.stderr)
        return 2
    events = _json.loads(args.events_json.read_text()) if args.events_json else fetch_series_events(source.http, args.series)
    if not events:
        print(f"no events for series `{args.series}`", file=sys.stderr)
        return 2
    markets = count_markets(events, monthly=args.monthly, log=log)
    print(f"{args.series}: {len(events)} events, {len(markets)} markets with a readable window and bracket")
    if not markets:
        return 2
    catalog, problems = load_catalog(args.catalog, time_col=args.time_col, value_col=args.value_col, min_value=args.min_value, count_col=args.count_col, tz=args.tz)
    for pr in problems:
        print(f"catalog: {pr}", file=sys.stderr)
    if not catalog.times:
        print("empty catalog", file=sys.stderr)
        return 2
    print(f"catalog {catalog.name}: {len(catalog.times)} rows, {_dt.fromtimestamp(catalog.first, tz=_tz.utc):%Y-%m-%d} .. {_dt.fromtimestamp(catalog.last, tz=_tz.utc):%Y-%m-%d %H:%M} UTC")
    checks = check_catalog(markets, catalog)
    if checks:
        ok = sum(1 for c in checks if c["match"])
        print(f"catalog vs winning bracket on {len(checks)} resolved windows: {ok} match, {len(checks) - ok} do not" + ("" if ok == len(checks) else "  <- a mismatch means the catalog is not what the tracker counts (replies, reposts, revisions, time zone)"))
        shown = checks[-10:]
        for c in shown:
            print(f"  {c['window']}  catalog {c['count']:g}  winner {c['winner']}  {'ok' if c['match'] else 'MISMATCH'}")
        bad = [c for c in checks[:-10] if not c["match"]]
        if bad:
            print(f"  earlier mismatches ({len(bad)}):")
            for c in bad:
                print(f"  {c['window']}  catalog {c['count']:g}  winner {c['winner']}  MISMATCH")
    else:
        print("no resolved window is fully covered by the catalog, so the catalog could not be checked against outcomes (it is checked on windows that start after the catalog's first row and end before its last)")
    if args.check_only:
        return 0
    hours = tuple(int(h) for h in args.hours.split(",") if h.strip())
    rows, skipped = build_signal_rows(markets, catalog, k_windows=args.windows, profile=not args.no_profile, hours=hours, pre_days=args.pre_days, log=log)
    write_signal_csv(rows, args.out)
    print(f"wrote {len(rows)} rows to {args.out}; next: python -m pm_scanner signal --csv {args.out}")
    return 0


SERIES_BY_HANDLE = {  # tracker handle (lower-case) -> Gamma series slug, for the next-step hint
    "elonmusk": "elon-tweets", "realdonaldtrump": "trump-truth-social", "whitehouse": "whitehouse-daily-tweets",
    "tedcruz": "ted-cruz-daily-tweets", "nycmayor": "nycmayor-tweets", "zelenskyyua": "zelenskyy-tweets",
    "cz_binance": "cz-tweets", "khamenei_irna": "khamenei-daily-tweets",
}


def _run_xtracker(args) -> int:
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    from .counts import _parse_dt, catalog_files
    from .http import HttpError
    from .xtracker import Tracker, compare_exports, harvest, parse_when, weekly_table, write_posts_csv

    log = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731
    tr = Tracker(pause=args.pause, base_url=args.base_url)
    if not args.list and not args.handle:
        print("give the account's handle (python -m pm_scanner xtracker elonmusk) or --list", file=sys.stderr)
        return 2
    try:
        if args.list:
            users = tr.users()
            print(f"{len(users)} tracked accounts (handle, platform, name, posts held):")
            for u in sorted(users, key=lambda u: str(u.get("handle", "")).lower()):
                n = (u.get("_count") or {}).get("posts", "")
                print(f"  {str(u.get('handle', '')):26} {str(u.get('platform', '')):14} {str(u.get('name', ''))[:40]:40} {n}")
            return 0
        user = tr.user(args.handle)
    except HttpError as exc:
        print(f"xtracker failed: {exc}", file=sys.stderr)
        return 2
    if not user or not user.get("handle"):
        print(f"no tracked account `{args.handle}` (python -m pm_scanner xtracker --list shows them)", file=sys.stderr)
        return 2
    created = _parse_dt(str(user.get("createdAt") or ""))
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=_tz.utc)
    total = (user.get("_count") or {}).get("posts")
    now = _dt.now(_tz.utc)
    print(f"{user.get('handle')} ({user.get('platform', '?')}): tracked since {created:%Y-%m-%d}, {total} posts held, {len(user.get('trackings') or [])} active windows" if created else f"{user.get('handle')}: {total} posts held")
    since = parse_when(args.since) if args.since else ((created - _td(days=30)) if created else now - _td(days=400))
    until = parse_when(args.until) if args.until else now + _td(days=1)
    out = args.out or Path("xtracker") / f"{args.handle}.csv"
    log(f"fetching {since:%Y-%m-%d} .. {until:%Y-%m-%d} in {args.chunk_days:g}-day chunks, up to {args.limit} posts a request")
    try:
        h = harvest(tr.posts_fetcher(args.handle, raw_dir=args.raw_dir), since, until, chunk=_td(days=args.chunk_days), limit=args.limit, log=log if args.verbose else None)
    except HttpError as exc:
        print(f"xtracker failed after {exc}", file=sys.stderr)
        return 2
    for note in h.notes:
        print(f"note: {note}")
    times = h.times()
    if not times:
        print(f"no posts came back ({h.requests} requests); the first record, if any, was: {_json.dumps(next(iter(h.posts.values()), None), default=str)[:300]}", file=sys.stderr)
        return 2
    n = write_posts_csv(h, out)
    print(f"{n} posts, {times[0]:%Y-%m-%d %H:%M} .. {times[-1]:%Y-%m-%d %H:%M} UTC, {h.requests} requests; post time read from `{h.time_field}`")
    if isinstance(total, int) and n < total:
        print(f"the account record holds {total} posts, {total - n} more than came back (posts before {since:%Y-%m-%d}? pass an earlier --since)")
    first = next(iter(h.posts.values()))
    shown = {k: (v[:60] + "..." if isinstance(v, str) and len(v) > 60 else v) for k, v in first.items()}
    print(f"one record, for checking the fields: {_json.dumps(shown, default=str)}")
    print("posts per UTC week (Monday to Monday), last 8 whole weeks:")
    print(weekly_table(times))
    if args.compare:
        files = catalog_files(args.compare)
        print(compare_exports(h, files) if files else f"compare: no CSV files at {args.compare}")
    if args.stats:
        try:
            trackings = tr.trackings(args.handle) or (user.get("trackings") or [])
        except HttpError:
            trackings = user.get("trackings") or []
        trackings = sorted(trackings, key=lambda t: str(t.get("startDate", "")))
        print(f"{len(trackings)} trackings ({sum(1 for t in trackings if t.get('isActive'))} open): the tracker's own record next to the catalog's count for the same window")
        print("  (a past window whose tracker count is above the catalog's means the posts route no longer holds posts the tracker counted)")
        for tr_ in trackings:
            try:
                full, _ = tr.get(f"/api/trackings/{tr_.get('id')}", {"includeStats": "true"})
            except HttpError as exc:
                print(f"  {tr_.get('title', tr_.get('id'))}: {exc}")
                continue
            a, b = _parse_dt(str(full.get("startDate") or "")), _parse_dt(str(full.get("endDate") or ""))
            ours = sum(1 for t in times if a is not None and b is not None and a.replace(tzinfo=a.tzinfo or _tz.utc) <= t < b.replace(tzinfo=b.tzinfo or _tz.utc) + _td(minutes=1))
            stats = {k: v for k, v in full.items() if k not in ("id", "userId", "title", "startDate", "endDate", "marketLink", "isActive", "createdAt", "updatedAt", "description", "user") and v not in (None, {}, [])}
            print(f"  {'open  ' if full.get('isActive') else 'closed'} {str(full.get('startDate', ''))[:16]} .. {str(full.get('endDate', ''))[:16]}  catalog {ours:4d}  tracker {_json.dumps(stats, default=str)[:600]}")
    series = SERIES_BY_HANDLE.get(args.handle.lower(), "<series>")
    print(f"wrote {out}; next: python -m pm_scanner counts --series {series} --catalog {out} --check-only")
    return 0


def _run_headroom(args, source) -> int:
    import json as _json
    from dataclasses import asdict as _asdict

    from .counts import fetch_series_events, headroom, render_headroom
    from .weather import LiveTrades

    log = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731
    slugs = [s.strip() for s in args.series.split(",") if s.strip()]
    monthly = {s.strip() for s in args.monthly.split(",") if s.strip()}
    trades_src = LiveTrades(cache_dir=args.cache_dir)
    fams, rows_all = [], []
    for slug in slugs:
        if args.events_dir:
            path = args.events_dir / f"{slug}.json"
            events = _json.loads(path.read_text()) if path.exists() else []
        else:
            events = fetch_series_events(source.http, slug)
        if not events:
            log(f"{slug}: no events")
            continue
        try:
            fam, rows = headroom(slug, events, trades_src, per_family=args.per_family, seed=args.seed, monthly=slug in monthly, log=log)
        except Exception as exc:  # one broken series should not lose the others
            log(f"{slug}: failed: {exc}")
            continue
        fams.append(fam)
        rows_all.extend(rows)
        log(render_headroom([fam]))
        if args.json:
            args.json.write_text(_json.dumps({"families": [f.to_dict() for f in fams], "rows": [_asdict(r) for r in rows_all]}, indent=1, default=str))
    if not fams:
        return 2
    print(render_headroom(fams))
    return 0


def _run_crossings(args, source) -> int:
    import json as _json

    from .counts import fetch_series_events, write_signal_csv
    from .crossings import render_crossings, run_crossings
    from .weather import LiveTrades

    slugs = [x.strip() for x in args.series.split(",") if x.strip()]
    events_by_slug: dict[str, list] = {}
    for slug in slugs:
        if args.events_dir:
            path = args.events_dir / f"{slug}.json"
            events_by_slug[slug] = _json.loads(path.read_text()) if path.exists() else []
        else:
            events_by_slug[slug] = fetch_series_events(source.http, slug)
        print(f"{slug}: {len(events_by_slug[slug])} events", file=sys.stderr)
    trades_src = LiveTrades(cache_dir=args.cache_dir)
    res = run_crossings(events_by_slug, trades_src, k_windows=args.windows, min_windows=args.min_windows, edge=args.edge, max_stale_hours=args.max_stale, horizon_min=args.horizon_min, timing=args.timing, count=args.count, lags=not args.no_lag, fill_wait_hours=args.fill_wait, log=lambda m: print(m, file=sys.stderr))
    if args.out:
        write_signal_csv(res.rows, args.out)
        print(f"signal rows written to {args.out} (re-score: python -m pm_scanner signal --csv {args.out})", file=sys.stderr)
    print(render_crossings(res, edge=args.edge, horizon_min=args.horizon_min))
    if args.json:
        args.json.write_text(_json.dumps(res.to_dict(), indent=1, default=str))
    return 0


def _run_lp(args, source) -> int:
    log = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731
    housekeeping = args.cancel_all or args.earnings or args.approve or args.positions or args.merge  # no quote is sized, so the budget is not read
    if args.budget > args.max_budget and not housekeeping:
        print(f"refusing: --budget {args.budget:g} is above the hard cap {args.max_budget:g} (MAX_BUDGET_USD)", file=sys.stderr)
        return 2
    if args.merge:
        wallet = (args.wallet or os.environ.get("POLY_WALLET", "")).strip()
        if not wallet:
            print("merge: set POLY_WALLET or pass --wallet 0x...", file=sys.stderr)
            return 2
        try:
            pv = merge_preview(source.http, wallet, args.merge)
        except Exception as exc:  # noqa: BLE001
            print(f"merge preview failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(pv, indent=1))
        if pv["pairs"] <= 0:
            print("nothing to merge: the account does not hold both sides of that market", file=sys.stderr)
            return 1
        if not args.live:
            print(f"\ndry run: merging would turn {pv['pairs']:g} YES+NO pairs into ${pv['returns_usd']:.2f} of collateral (paid ${pv['paid_usd']:.2f}); add --live to do it")
            return 0
        try:
            exchange = LiveExchange.from_env()
            print(json.dumps(exchange.merge(args.merge), indent=1, default=str))
            print(render_positions(positions_report(source.http, source.poly_books, wallet)))
        except Exception as exc:  # noqa: BLE001
            print(f"merge failed: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.positions:
        wallet = (args.wallet or os.environ.get("POLY_WALLET", "")).strip()
        if not wallet:
            print("positions: set POLY_WALLET or pass --wallet 0x... (the account address; no key is needed)", file=sys.stderr)
            return 2
        try:
            ps = positions_report(source.http, source.poly_books, wallet)
        except Exception as exc:  # noqa: BLE001
            print(f"positions failed: {exc}", file=sys.stderr)
            return 2
        print(render_positions(ps))
        if args.json:
            args.json.write_text(json.dumps([x.to_dict() for x in ps], indent=1, default=str))
        return 0
    exchange = None
    if args.live or args.check or args.earnings or args.cancel_all or args.approve:
        try:
            exchange = LiveExchange.from_env()
        except Exception as exc:
            print(f"cannot connect: {exc}", file=sys.stderr)
            return 2
        if args.cancel_all:
            exchange.cancel_all()
            print("all open orders cancelled")
            return 0
        if args.earnings:
            e = exchange.earnings(args.earnings)
            print(json.dumps({"date": args.earnings, "total": round(sum(e.values()), 4), "by_market": e}, indent=1))
            return 0
        if args.approve:
            print(json.dumps(exchange.approve(), indent=1))
            return 0
        print(json.dumps(exchange.describe(), indent=1))
    now = utcnow()
    try:
        rows = rewards_pocket(source.http, source.poly_books, now=now, min_rate=args.min_rate, log=log)
        only = parse_only(args.only)
        cand = [r for r in rows if only is None or r.condition_id in only]
        worth_a_lookup = cand if only is not None else [r for r in cand if cheap_reason(r, min_days=args.min_days, max_spread=args.max_spread, min_reward=args.min_reward, min_depth=args.min_depth) is None]
        markets = gamma_markets_by_condition(source.http, [r.condition_id for r in worth_a_lookup[:400]])
        reasons: dict[str, int] = {}
        plans = choose_markets(cand, markets, budget=args.budget, max_markets=1 if args.smoke else args.markets, min_days=args.min_days, max_spread=args.max_spread, min_reward=args.min_reward, only=only, per_market=args.per_market, reasons=reasons, max_exit=args.max_exit,
                               history=week_fetcher(source.http), min_age=args.min_age, max_moves=args.max_moves, min_depth=args.min_depth)
        if not plans and reasons:
            log("no market fits; why each candidate was passed over: " + ", ".join(f"{k}: {v}" for k, v in reasons.items()) + " (a small budget wants --smoke or --markets 1, which lifts the per-market cap, or --per-market)")
    except Exception as exc:
        print(f"lp failed: {exc}", file=sys.stderr)
        return 2
    print(render_plan(plans))
    if not plans:
        return 1
    if not args.live:
        print("\ndry run: nothing sent. Add --live (with POLY_* in the environment) to rest these quotes.")
        return 0
    if args.live and not args.check and only is None:
        print("live run: name the market(s) to quote with --only <condition id,...> from the plan above, so what is quoted is what you saw in the dry run (the 3 Oct smoke run quoted a different market than its dry run, the pots being equal)", file=sys.stderr)
        return 2
    blockers = exit_blockers(plans, args.max_exit)
    if blockers:
        for b in blockers:
            print(b, file=sys.stderr)
        print(f"live run refused: a fill in that book could not be undone for --max-exit ${args.max_exit:g} or less; pick another market, or raise --max-exit if you accept that loss (the 3 Oct smoke run's fill cost $3 to undo at once and $4.60 by morning)", file=sys.stderr)
        return 2
    blockers = history_blockers(plans, args.min_age, args.max_moves)
    if blockers:
        for b in blockers:
            print(b, file=sys.stderr)
        print(f"live run refused: the market is too young or its price too restless for --min-age {args.min_age:g} / --max-moves {args.max_moves:g}; pick another market, or lower --min-age / raise --max-moves in the command if you accept that risk (4 Oct: two one-day-old markets, 45c and 20c of travel on day one, one fill in ninety minutes costing $3.87)", file=sys.stderr)
        return 2
    blockers = depth_blockers(plans, args.min_depth)
    if blockers:
        for b in blockers:
            print(b, file=sys.stderr)
        print(f"live run refused: the book is thinner inside the max spread than --min-depth {args.min_depth:g} asks; pick another market, or lower --min-depth in the command (4 Oct: in books of a few 20-share orders the mid was whoever last placed one)", file=sys.stderr)
        return 2
    hours = 2.0 if args.smoke and args.hours == 72.0 else args.hours
    if args.until:
        until = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        hours = max(0.0, (until - utcnow()).total_seconds() / 3600.0)
        if hours <= 0:
            print(f"--until {args.until} is in the past; nothing to do", file=sys.stderr)
            return 0
    held: dict[str, tuple[float, float]] = {}
    held_px: dict[str, tuple[float | None, float | None]] = {}
    wallet = (args.wallet or os.environ.get("POLY_WALLET", "")).strip() or str(getattr(exchange.client, "wallet", "") or "")
    if wallet:
        try:
            for pos in positions_report(source.http, source.poly_books, wallet):
                yes, no = held.get(pos.condition_id, (0.0, 0.0))
                yes_px, no_px = held_px.get(pos.condition_id, (None, None))
                if pos.outcome.lower() == "yes":
                    held[pos.condition_id], held_px[pos.condition_id] = (yes + pos.shares, no), (pos.avg_price, no_px)
                else:
                    held[pos.condition_id], held_px[pos.condition_id] = (yes, no + pos.shares), (yes_px, pos.avg_price)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: could not read held positions ({exc}); a side filled by an earlier run would be quoted again", file=sys.stderr)
    q = Quoter(exchange, plans, log_path=args.log, pull_before_end_hours=args.pull_before_end_hours, recentre_confirm=args.recentre_confirm, held=held, held_prices=held_px)
    print(f"\nquoting {len(plans)} market(s) for {hours:.1f}h, checking every {args.interval:g}s; Ctrl-C cancels everything and exits")
    try:
        q.run(hours=hours, interval=args.interval)
    except KeyboardInterrupt:
        print("\nstopped; open orders cancelled")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pm_scanner", description="Read-only Polymarket/Kalshi opportunity scanner")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan once and print a report")
    _add_scan_args(s)
    s.add_argument("--json", type=Path, default=None, help="also write results to this JSON file")

    w = sub.add_parser("watch", help="re-scan on an interval and log appearances/disappearances (paper trading)")
    _add_scan_args(w)
    w.add_argument("--interval", type=float, default=60.0, help="seconds between scans")
    w.add_argument("--log", type=Path, default=Path("watch.jsonl"), help="JSONL log to append to")
    w.add_argument("--include-near-certain", action="store_true", help="also track near_certain rows (noisy)")
    w.add_argument("--iterations", type=int, default=None, help="stop after N scans (default: run forever)")
    w.add_argument("--telegram-token", default=os.environ.get("TELEGRAM_BOT_TOKEN"), help="or env TELEGRAM_BOT_TOKEN")
    w.add_argument("--telegram-chat", default=os.environ.get("TELEGRAM_CHAT_ID"), help="or env TELEGRAM_CHAT_ID")

    z = sub.add_parser("summarize", help="aggregate a watch log into rates, sizes and lifetimes")
    z.add_argument("log", type=Path)
    z.add_argument("--json", action="store_true", help="print raw JSON instead of a table")

    i = sub.add_parser("israel", help="Knesset election model vs Polymarket's Israel election markets")
    i.add_argument("--polls", type=Path, default=Path("data/israel_polls_2026.csv"), help="poll CSV (see the file header)")
    i.add_argument("--budget", type=float, default=500.0)
    i.add_argument("--min-edge", type=float, default=0.03, help="minimum edge per share after fees, e.g. 0.03 = 3 cents")
    i.add_argument("--sims", type=int, default=10000)
    i.add_argument("--seed", type=int, default=1)
    i.add_argument("--half-life", type=float, default=7.0, help="poll recency half-life in days")
    i.add_argument("--window", type=float, default=21.0, help="ignore polls older than this many days")
    i.add_argument("--error-scale", type=float, default=1.0, help="multiply every party's polling-error sd")
    i.add_argument("--bloc-sd", type=float, default=0.010, help="sd of a common right<->centre transfer, as a vote fraction")
    i.add_argument("--bloc-bias", type=float, default=0.0, help="mean of that transfer; +0.01 = polls understate the right by 1 point")
    i.add_argument("--sd", default="", help="per-party error sd overrides in vote points, e.g. utj=0.6,shas=0.9")
    i.add_argument("--min-price", type=float, default=0.05, help="never trade a side cheaper than this; the model cannot price pennies")
    i.add_argument("--surplus", default=",".join(f"{a}:{b}" for a, b in DEFAULT_SURPLUS_PAIRS), help="surplus-vote pairs, e.g. yashar:dems,together:yb")
    i.add_argument("--kelly", type=float, default=0.25, help="fraction of Kelly to stake")
    i.add_argument("--max-fraction", type=float, default=0.10, help="cap per market as a fraction of budget")
    i.add_argument("--maker-margin", type=float, default=0.06, help="how far inside the model to post resting orders")
    i.add_argument("--poly-fee-rate", type=float, default=None, help="override the taker rate (default: from tags, politics 0.04)")
    i.add_argument("--fixtures", type=Path, default=None, help="offline: read israel_events.json / israel_books.json from DIR")
    i.add_argument("--all", action="store_true", help="also list every modelled market with its model probability")
    i.add_argument("--json", type=Path, default=None, help="also write the full report to this JSON file")

    n = sub.add_parser("niches", help="survey recurring market families: cadence, depth, fees, day-before calibration")
    n.add_argument("--tags", default=None, help="comma-separated Gamma tag slugs (default: weather, culture, tech, economy, finance, mentions, politics, ...)")
    n.add_argument("--days", type=int, default=90, help="survey window: events started in the last N days (default 90)")
    n.add_argument("--min-events", type=int, default=6, help="ignore families with fewer events than this")
    n.add_argument("--top", type=int, default=60)
    n.add_argument("--sort", choices=["volume", "steady", "liquidity"], default="volume")
    n.add_argument("--include-sport", action="store_true", help="also list sports and esports families")
    n.add_argument("--include-up-or-down", action="store_true", help="also pull the 5-minute/hourly crypto series (thousands a day)")
    n.add_argument("--fixtures", type=Path, default=None, help="offline: read niche_events.json from DIR")
    n.add_argument("--json", type=Path, default=None, help="also write every family's stats to this JSON file")

    wx = sub.add_parser("weather", help="daily temperature brackets: mispricing trend, forecast backtest, or live quotes")
    wx.add_argument("--mode", choices=["trend", "backtest", "intraday", "flow", "today"], default="backtest")
    wx.add_argument("--cities", default="nyc,london,tel-aviv", help=f"comma-separated city keys, or 'all'; known: {', '.join(sorted(STATIONS))}")
    wx.add_argument("--days", type=int, default=30, help="how many days back to load events for (trend/backtest)")
    wx.add_argument("--cutoff-hour", type=int, default=0, help="local hour on the target day whose price is scored (0 = as the day starts)")
    wx.add_argument("--lead", type=int, default=1, help="use the forecast issued this many days before the target day")
    wx.add_argument("--calib-window", type=int, default=0, help="backtest/intraday: fit the station bias/sd on only the last N days (0 = all days so far)")
    wx.add_argument("--hours", default="11,13,15", help="intraday: local decision hours, comma-separated")
    wx.add_argument("--latency-min", type=int, default=5, help="intraday: minutes after the hour at which the price is taken (reaction time)")
    wx.add_argument("--include-specials", action="store_true", help="intraday: count SPECI reports too, not only routine hourly METARs")
    wx.add_argument("--obs-cache", type=Path, default=Path(".cache/pm_obs"), help="intraday: where IEM station observations are cached")
    wx.add_argument("--horizon-min", type=int, default=30, help="flow: minutes after each trade at which the taker's markout is also measured")
    wx.add_argument("--models", default=",".join(DEFAULT_MODELS), help="Open-Meteo model ids, comma-separated")
    wx.add_argument("--edge", type=float, default=0.05, help="minimum model-vs-market gap to trade, per share, before fees for backtest / after fees for today")
    wx.add_argument("--fee", type=float, default=0.05, help="taker rate (weather markets pay 0.05)")
    wx.add_argument("--events-per-month", type=int, default=25, help="trend: sampled events per month (each is ~11 trade-history calls)")
    wx.add_argument("--budget", type=float, default=300.0, help="today: total budget for sizing")
    wx.add_argument("--maker-margin", type=float, default=0.05, help="today: how far inside the model to post resting orders")
    wx.add_argument("--ensemble", action="store_true", help="today: also pull the ECMWF ensemble for the spread")
    wx.add_argument("--calib", type=Path, default=None, help="today: JSON from a backtest run to reuse per-city bias/sd")
    wx.add_argument("--station", default="", help="override coordinates, e.g. 'nyc=40.78,-73.87;tel-aviv=32.01,34.89'")
    wx.add_argument("--extra-forecasts", type=Path, default=Path("data/extra_forecasts.csv"), help="CSV city,date,source,value of hand-logged forecasts (IMS etc.) scored alongside the models")
    wx.add_argument("--cache-dir", type=Path, default=Path(".cache/pm_trades"), help="where trade histories are cached")
    wx.add_argument("--fixtures", type=Path, default=None, help="offline: weather_events.json / weather_trades.json from DIR")
    wx.add_argument("--json", type=Path, default=None, help="also write the report to this JSON file")

    fl = sub.add_parser("flow", help="order-flow screen: per family, do takers or makers win? (trade tape + outcomes only)")
    fl.add_argument("--days", type=int, default=45, help="window: events started in the last N days")
    fl.add_argument("--tags", default=None, help="comma-separated Gamma tag slugs (default: the survey tags plus sports, esports, crypto)")
    fl.add_argument("--per-family", type=int, default=30, help="resolved markets sampled per family (one trade-history request each)")
    fl.add_argument("--min-markets", type=int, default=10, help="skip families with fewer resolved markets in the window")
    fl.add_argument("--max-families", type=int, default=60, help="only the N biggest families by volume are sampled")
    fl.add_argument("--horizon-min", type=int, default=30, help="minutes after each trade for the second markout")
    fl.add_argument("--no-sport", action="store_true", help="skip sports and esports families")
    fl.add_argument("--seed", type=int, default=7)
    fl.add_argument("--top", type=int, default=40)
    fl.add_argument("--sort", choices=["maker", "volume"], default="maker")
    fl.add_argument("--window-days", type=int, default=3, help="Gamma paging window for closed events (smaller = more requests, never truncated)")
    fl.add_argument("--cache-dir", type=Path, default=Path(".cache/pm_trades"), help="where trade histories are cached")
    fl.add_argument("--fixtures", type=Path, default=None, help="offline: niche_events.json / weather_trades.json from DIR")
    fl.add_argument("--families", default=None, help="only these family keys, comma-separated (e.g. miami-daily-weather,japan-j-league)")
    fl.add_argument("--exclude-json", type=Path, default=None, help="a previous --json report: its sampled markets are excluded, so this run is out of sample")
    fl.add_argument("--maker", action="store_true", help="also run the paper maker on the tape of the --families (all their resolved markets)")
    fl.add_argument("--half-spreads", default="0.02,0.03,0.05", help="maker: quote distances from the last print, comma-separated")
    fl.add_argument("--quote-size", type=float, default=20.0, help="maker: shares per side per fill")
    fl.add_argument("--min-hours", default="0,1,6", help="maker: only quote while at least this many hours remain, comma-separated variants")
    fl.add_argument("--max-position", type=float, default=100.0, help="maker: net position cap per market, shares")
    fl.add_argument("--fill", choices=["through", "at"], default="through", help="maker: fill on prints strictly through the quote, or at it too")
    fl.add_argument("--json", type=Path, default=None, help="also write every family's flow stats to this JSON file")

    ld = sub.add_parser("ladder", help="ladder consistency: nested outcomes (by date, above a threshold) priced out of order")
    ld.add_argument("--max-events", type=int, default=6000, help="open Polymarket events to pull, by 24h volume")
    ld.add_argument("--budget", type=float, default=50.0, help="cash available, used to size each violation")
    ld.add_argument("--min-edge", type=float, default=0.0, help="minimum net edge per $1 set after both taker fees")
    ld.add_argument("--poly-fee-rate", type=float, default=None, help="override the taker rate for every market")
    ld.add_argument("--no-cross", action="store_true", help="only ladders inside one event, not '... by <date>?' questions across events")
    ld.add_argument("--repeat", type=int, default=1, help="take this many snapshots")
    ld.add_argument("--interval", type=float, default=600.0, help="seconds between snapshots")
    ld.add_argument("--top", type=int, default=15)
    ld.add_argument("--log", type=Path, default=None, help="append each snapshot's JSON to this JSONL file")
    ld.add_argument("--fixtures", type=Path, default=None, help="offline: gamma_events.json / clob_books.json from DIR")

    rw = sub.add_parser("rewards", help="liquidity rewards: what a small two-sided quote earns per day vs what it loses to adverse selection")
    rw.add_argument("--days", type=float, default=14.0, help="replay the last N days of each market's tape")
    rw.add_argument("--top", type=int, default=30, help="biggest pots to sample")
    rw.add_argument("--mid", type=int, default=30, help="random markets with $10-100/day")
    rw.add_argument("--low", type=int, default=30, help="random markets with $1-10/day")
    rw.add_argument("--seed", type=int, default=7)
    rw.add_argument("--case", default="touch x1", help="quote case to list the best markets for (touch x1, touch x5, half-max x1, half-max x5)")
    rw.add_argument("--list", type=int, default=25, help="best markets to list")
    rw.add_argument("--cache-dir", type=Path, default=Path(".cache/pm_rewards_trades"), help="where the (open-market) tapes are cached for this run")
    rw.add_argument("--configs", type=Path, default=None, help="offline: a saved JSON list from /rewards/markets/current")
    rw.add_argument("--books-only", action="store_true", help="skip the tapes: read every rewarded market's book and report where the pot is unclaimed")
    rw.add_argument("--min-rate", type=float, default=10.0, help="books-only: only markets paying at least this much per day")
    rw.add_argument("--min-days", type=float, default=7.0, help="books-only: list markets at least this many days from resolution")
    rw.add_argument("--fixtures", type=Path, default=None, help="unused: the survey needs live books and tapes")
    rw.add_argument("--json", type=Path, default=None, help="also write every sampled market's numbers to this JSON file")

    ct = sub.add_parser("counts", help="turn a catalog of timestamped occurrences (posts, quakes, ship transits) into a signal CSV for a count-window series, to score with `signal`")
    ct.add_argument("--series", required=True, help="Gamma series slug: trump-truth-social, 6pt5-earthquake-weekly, ships-transit-the-strait-of-hormuz, monthly-tornadoes-us, ...")
    ct.add_argument("--catalog", type=Path, required=True, help="CSV with one row per occurrence (time column auto-detected), a folder of such CSVs or a glob (the post tracker's per-window Posts exports; repeated ids are dropped), or one row per day with --count-col")
    ct.add_argument("--time-col", default=None, help="name of the time column when it is not obvious")
    ct.add_argument("--value-col", default=None, help="keep only rows whose value is >= --min-value (e.g. --value-col mag --min-value 6.5)")
    ct.add_argument("--min-value", type=float, default=None)
    ct.add_argument("--count-col", default=None, help="pre-aggregated catalog: the column holding the count for that row's day")
    ct.add_argument("--tz", default=None, help="IANA zone of naive timestamps in the catalog (default UTC)")
    ct.add_argument("--windows", type=int, default=8, help="reference windows before each market window for the rate and dispersion")
    ct.add_argument("--no-profile", action="store_true", help="allocate the remaining count uniformly in time instead of by the reference windows' phase profile")
    ct.add_argument("--hours", default="12", help="UTC decision hours per day, comma-separated")
    ct.add_argument("--pre-days", type=int, default=1, help="also quote this many days before the window opens")
    ct.add_argument("--monthly", action="store_true", help="windows are the calendar months named in the questions (tornadoes, downtime)")
    ct.add_argument("--events-json", type=Path, default=None, help="offline: the series' events from this JSON file instead of Gamma")
    ct.add_argument("--out", type=Path, default=Path("counts_signal.csv"), help="signal CSV to write; then `python -m pm_scanner signal --csv OUT`")
    ct.add_argument("--fixtures", type=Path, default=None, help=argparse.SUPPRESS)
    ct.add_argument("--check-only", action="store_true", help="only compare the catalog's window counts with the winning brackets")

    xt = sub.add_parser("xtracker", help="pull an account's whole counted-post history from xtracker.polymarket.com (the counter Polymarket resolves the posts ladders on) into a catalog CSV for `counts`")
    xt.add_argument("handle", nargs="?", default=None, help="the account as the tracker names it: elonmusk, realDonaldTrump, WhiteHouse, tedcruz, NYCMayor, ... (--list shows them)")
    xt.add_argument("--list", action="store_true", help="list the tracked accounts and exit")
    xt.add_argument("--out", type=Path, default=None, help="catalog CSV to write (default xtracker/<handle>.csv)")
    xt.add_argument("--since", default=None, help="first day to fetch, UTC (default: a month before the tracker started following the account)")
    xt.add_argument("--until", default=None, help="last day to fetch (default: now)")
    xt.add_argument("--chunk-days", type=float, default=7.0, help="days per request before any halving")
    xt.add_argument("--limit", type=int, default=500, help="posts asked for per request (a server cap is detected and worked around)")
    xt.add_argument("--pause", type=float, default=0.15, help="seconds between requests")
    xt.add_argument("--raw-dir", type=Path, default=None, help="also keep every raw response here (JSON per request)")
    xt.add_argument("--compare", type=Path, default=None, help="folder of the site's own Posts exports: line up their `Posted At (EST)` with the API's time on the shared posts")
    xt.add_argument("--stats", action="store_true", help="also print the tracker's own live count for each open window next to the catalog's")
    xt.add_argument("--verbose", action="store_true", help="log every request")
    xt.add_argument("--base-url", default="https://xtracker.polymarket.com", help=argparse.SUPPRESS)

    hr = sub.add_parser("headroom", help="how sharp count-window and ladder markets already are at the start, middle and late part of their window, against uniform and point-in-time base rates")
    hr.add_argument("--series", required=True, help="comma-separated Gamma series slugs")
    hr.add_argument("--per-family", type=int, default=12, help="resolved events sampled per series (each market is one trade-history request)")
    hr.add_argument("--seed", type=int, default=7)
    hr.add_argument("--monthly", default="monthly-tornadoes-us,claude-downtime", help="comma-separated slugs whose windows are calendar months")
    hr.add_argument("--events-dir", type=Path, default=None, help="offline: <slug>.json event dumps in this directory instead of Gamma")
    hr.add_argument("--cache-dir", type=Path, default=Path(".cache/pm_trades"), help="where trade histories are cached")
    hr.add_argument("--fixtures", type=Path, default=None, help=argparse.SUPPRESS)
    hr.add_argument("--json", type=Path, default=None, help="write families and rows here (updated after every series)")

    sg = sub.add_parser("signal", help="backtest your own probabilities (a CSV of market,time,p) against the market price at that time and the outcome")
    sg.add_argument("--csv", type=Path, required=True, help="columns: market (condition id, slug or Gamma id), time (ISO 8601 or unix), p (0-1), optional outcome, note")
    sg.add_argument("--horizon-min", type=int, default=30, help="minutes after each row for the markout")
    sg.add_argument("--edge", type=float, default=0.05, help="paper-trade only when |p - price| exceeds this plus the taker fee")
    sg.add_argument("--fee", type=float, default=None, help="override the taker rate (default: the market's own schedule, else 0.05)")
    sg.add_argument("--max-stale", type=float, default=24.0, help="drop rows whose last trade before `time` is older than this many hours")
    sg.add_argument("--cache-dir", type=Path, default=Path(".cache/pm_trades"), help="where trade histories are cached")
    sg.add_argument("--fixtures", type=Path, default=None, help="offline: markets and tapes from DIR")
    sg.add_argument("--fill-wait", type=float, default=0.0, help="also re-execute each paper trade at the first print after its row time on the side you would have needed, within this many hours (0 = off); the honest price in a thin market")
    sg.add_argument("--json", type=Path, default=None, help="also write every scored row to this JSON file")

    cr = sub.add_parser("crossings", help="test the count model against outcomes with no outside data: Polymarket closes each count bracket the moment the running count passes its ceiling, and those close times are a timestamped partial count (memo section 19e)")
    cr.add_argument("--series", default="trump-truth-social,whitehouse-daily-tweets,khamenei-daily-tweets,zelenskyy-tweets,ted-cruz-daily-tweets,nycmayor-tweets,cz-tweets", help="comma-separated Gamma series slugs")
    cr.add_argument("--events-dir", type=Path, default=None, help="read <slug>.json event dumps from DIR instead of Gamma")
    cr.add_argument("--windows", type=int, default=8, help="trailing windows for the base rate")
    cr.add_argument("--min-windows", type=int, default=3, help="skip decision times with fewer trailing windows than this")
    cr.add_argument("--edge", type=float, default=0.05, help="paper-trade only when |p - price| exceeds this plus the taker fee")
    cr.add_argument("--max-stale", type=float, default=24.0, help="drop rows whose last trade before the decision time is older than this many hours")
    cr.add_argument("--horizon-min", type=int, default=30, help="minutes after each row for the markout")
    cr.add_argument("--cache-dir", type=Path, default=Path(".cache/pm_trades"), help="where trade histories are cached")
    cr.add_argument("--out", type=Path, default=Path("crossings_signal.csv"), help="write the signal rows here (re-scorable with `signal --csv`)")
    cr.add_argument("--timing", choices=("close", "collapse"), default="close", help="date each crossing by the bracket's UMA close (certain, stale) or by its price collapse (fresh, assumes the crowd did not sell it dead early)")
    cr.add_argument("--count", choices=("lower", "interval"), default="lower", help="use the count lower bound alone (pre-registered) or spread it up to the ceiling of the lowest bracket still alive (post-hoc variant)")
    cr.add_argument("--fill-wait", type=float, default=6.0, help="re-execute each paper trade at the first print after the decision time on the side we need, within this many hours (0 = skip)")
    cr.add_argument("--no-lag", action="store_true", help="skip the close-lag diagnostic")
    cr.add_argument("--json", type=Path, default=None, help="also write the full result here")
    cr.add_argument("--fixtures", type=Path, default=None, help=argparse.SUPPRESS)

    lp = sub.add_parser("lp", help="liquidity-reward test rig: rest minimum-size two-sided post-only quotes in a few unquoted rewarded markets (memo section 18)")
    lp.add_argument("--live", action="store_true", help="actually send orders (default: dry run, public data only)")
    lp.add_argument("--smoke", action="store_true", help="one market only, for the first hours of a fresh account")
    lp.add_argument("--budget", type=float, default=50.0, help="total collateral to park across all quotes")
    lp.add_argument("--markets", type=int, default=3, help="how many markets to quote")
    lp.add_argument("--only", default=None, help="quote exactly these condition ids (comma-separated), skipping the filters")
    lp.add_argument("--min-rate", type=float, default=20.0, help="candidate markets: pot per day at least this")
    lp.add_argument("--min-reward", type=float, default=20.0, help="candidate markets: modelled reward for our quote at least this per day")
    lp.add_argument("--min-days", type=float, default=7.0, help="candidate markets: at least this many days to resolution")
    lp.add_argument("--max-spread", type=float, default=0.5, help="candidate markets: book spread at most this")
    lp.add_argument("--per-market", type=float, default=0.0, help="collateral cap per market (0 = 1.6 x budget / markets)")
    lp.add_argument("--hours", type=float, default=72.0, help="how long to keep quoting (the smoke test defaults to 2)")
    lp.add_argument("--until", default=None, metavar="ISO-UTC", help="quote until this time instead of --hours (e.g. 2026-10-07T10:00); a restarted run then still ends on time")
    lp.add_argument("--interval", type=float, default=60.0, help="seconds between book checks")
    lp.add_argument("--recentre-confirm", type=int, default=3, help="re-centre the quotes only after the others' mid has read a tick or more away for this many consecutive checks (in a book of a few 20-share orders the mid is whoever last placed one)")
    lp.add_argument("--pull-before-end-hours", type=float, default=48.0, help="cancel a market's quotes this long before its end date")
    lp.add_argument("--log", type=Path, default=Path("lp.jsonl"), help="JSONL record of every order, fill, scoring read and earnings read")
    lp.add_argument("--check", action="store_true", help="live credentials: print wallet, balance, approvals and the plan, send nothing")
    lp.add_argument("--approve", action="store_true", help="live: grant the trading approvals the SDK lists as missing (gasless on a deposit wallet) and exit; not needed when approvals_ok_for_quoting is true")
    lp.add_argument("--earnings", default=None, help="live: print the day's reward earnings (YYYY-MM-DD) and exit")
    lp.add_argument("--cancel-all", action="store_true", help="live: cancel every open order on the account and exit")
    lp.add_argument("--positions", action="store_true", help="what the account holds and what the book pays to sell it now (public data; needs POLY_WALLET or --wallet, no key)")
    lp.add_argument("--wallet", default=None, help="account address for --positions and --merge (default: POLY_WALLET)")
    lp.add_argument("--merge", default=None, metavar="CONDITION_ID", help="merge every YES+NO pair of this market back into collateral ($1 a pair, no price, no fee); preview from public data, --live to do it")
    lp.add_argument("--json", type=Path, default=None, help="with --positions: also write the rows as JSON here")
    lp.add_argument("--max-exit", type=float, default=2.0, help="candidate markets: undoing one full fill into today's book must lose at most this many dollars, fee included; a live run refuses a plan above it")
    lp.add_argument("--min-age", type=float, default=6.5, help="candidate markets: at least this many days of price history on the CLOB (the week it returns reads as about 7; a new market is still finding its price); a live run refuses a younger one")
    lp.add_argument("--min-depth", type=float, default=0.0, help="candidate markets: at least this many score-weighted shares of other people's orders already inside the max spread on the thinner side (0 = off; hundreds = the deep calm books of section 18e, where the mid is real and a fill is cheap to undo); a live run refuses a plan under it")
    lp.add_argument("--max-moves", type=float, default=2.0, help="candidate markets: at most this many 10-minute moves of 3c or more per day over the past week's prices (each one could have filled a quote 3c from the mid); a live run refuses a plan above it")
    lp.add_argument("--max-budget", type=float, default=float(os.environ.get("MAX_BUDGET_USD", "50")), help="hard cap; --budget above this is refused (env MAX_BUDGET_USD)")
    lp.add_argument("--fixtures", type=Path, default=None, help="unused: this command needs live data")

    f = sub.add_parser("fee", help="compute the fee for a hypothetical order")
    f.add_argument("--platform", choices=["polymarket", "kalshi"], required=True)
    f.add_argument("--price", type=float, required=True)
    f.add_argument("--shares", type=float, default=100.0)
    f.add_argument("--rate", type=float, default=0.05, help="Polymarket category rate")
    f.add_argument("--multiplier", type=float, default=1.0, help="Kalshi series multiplier")
    f.add_argument("--maker", action="store_true", help="Kalshi maker instead of taker")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "fee":
        if args.platform == "polymarket":
            fee = polymarket_taker_fee(args.price, args.shares, args.rate)
            print(f"Polymarket taker fee: ${fee:.4f} on {args.shares:g} shares @ {args.price} (rate {args.rate}); makers pay $0")
        else:
            fn = kalshi_maker_fee if args.maker else kalshi_taker_fee
            fee = fn(args.price, args.shares, args.multiplier)
            print(f"Kalshi {'maker' if args.maker else 'taker'} fee: ${fee:.2f} on {args.shares:g} contracts @ {args.price}")
        return 0

    if args.cmd == "summarize":
        summary = summarize(args.log)
        print(json.dumps(summary, indent=2) if args.json else render_summary(summary))
        return 0

    if args.cmd == "xtracker":
        return _run_xtracker(args)

    source = FixtureSource(args.fixtures) if args.fixtures else LiveSource()

    if args.cmd == "israel":
        pairs = tuple(tuple(x.split(":", 1)) for x in args.surplus.split(",") if ":" in x)
        overrides = {k.strip(): float(v) / 100 for k, v in (x.split("=", 1) for x in args.sd.split(",") if "=" in x)}
        model = ErrorModel(scale=args.error_scale, bloc_sd=args.bloc_sd, bloc_bias=args.bloc_bias, sd_overrides=overrides)
        now = utcnow()
        try:
            report = run_israel(
                source, args.polls, budget=args.budget, now=now, sims=args.sims, seed=args.seed,
                half_life=args.half_life, window=args.window, min_edge=args.min_edge, model=model,
                surplus_pairs=pairs, kelly_fraction=args.kelly, max_fraction=args.max_fraction,
                maker_margin=args.maker_margin, min_price=args.min_price, fee_override=args.poly_fee_rate,
            )
        except Exception as exc:
            print(f"israel failed: {exc}", file=sys.stderr)
            return 2
        print(render_israel(report, show_all=args.all))
        if args.json:
            args.json.write_text(json.dumps(report.to_dict(), indent=2, default=str))
            print(f"\nwrote {args.json}")
        return 0

    if args.cmd == "weather":
        return _run_weather(args, source)

    if args.cmd == "niches":
        now = utcnow()
        since = default_since(now, args.days)
        try:
            events = source.poly_events_survey(load_survey_tags(args.tags), since, now, exclude_tag_id=None if args.include_up_or_down else UP_OR_DOWN_TAG_ID)
        except Exception as exc:
            print(f"niches failed: {exc}", file=sys.stderr)
            return 2
        rows = survey(events, now=now, since=since, min_events=args.min_events)
        print(render_survey(rows, now=now, since=since, top=args.top, include_sport=args.include_sport, sort=args.sort))
        if args.json:
            args.json.write_text(json.dumps([r.to_dict() for r in rows], indent=2, default=str))
            print(f"\nwrote {args.json}")
        return 0

    if args.cmd == "flow":
        now = utcnow()
        since = default_since(now, args.days)
        log = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731
        try:
            tags = load_survey_tags(args.tags) if args.tags else DEFAULT_FLOW_TAGS
            if args.fixtures:
                events = source.poly_events_survey(tags, since, now)
                trades_src = FixtureTrades(args.fixtures)
            else:
                events = source.poly_events_survey(tags, since, now, exclude_tag_id=UP_OR_DOWN_TAG_ID, window_days=args.window_days)
                trades_src = LiveTrades(cache_dir=args.cache_dir)
            log(f"{len(events)} events loaded for {len(tags)} tags")
            only = {k.strip() for k in args.families.split(",") if k.strip()} if args.families else None
            excl = sampled_market_ids(json.loads(args.exclude_json.read_text())) if args.exclude_json else None
            if excl is not None:
                log(f"{len(excl)} previously sampled markets excluded")
            rows = family_flow(events, trades_src, now=now, since=since, per_family=args.per_family, min_markets=args.min_markets, max_families=args.max_families, horizon_min=args.horizon_min, include_sport=not args.no_sport, seed=args.seed, only_families=only, exclude_markets=excl, log=log)
        except Exception as exc:
            print(f"flow failed: {exc}", file=sys.stderr)
            return 2
        print(render_family_flow(rows, top=args.top, sort=args.sort))
        payload: dict = {"families": [r.to_dict() for r in rows]}
        if args.maker and only:
            configs = [MakerConfig(half_spread=float(h), size=args.quote_size, min_hours=float(mh), max_position=args.max_position, fill=args.fill)
                       for h in args.half_spreads.split(",") if h.strip() for mh in args.min_hours.split(",") if mh.strip()]
            try:
                results, detail = maker_backtest(events, trades_src, now=now, since=since, families=only, configs=configs, per_family=max(args.per_family, 200), seed=args.seed, log=log)
            except Exception as exc:
                print(f"maker test failed: {exc}", file=sys.stderr)
                return 2
            print()
            print(render_maker(results))
            payload["maker"] = [r.to_dict() for r in results]
            payload["maker_detail"] = detail
        if args.json:
            args.json.write_text(json.dumps(payload, indent=1, default=str))
            print(f"\nwrote {args.json}")
        return 0

    if args.cmd == "ladder":
        import time as _time

        reports = []
        for i in range(max(1, args.repeat)):
            now = utcnow()
            try:
                events = source.poly_events(args.max_events)
                rep = scan_ladders(events, source.poly_books, now=now, budget=args.budget, min_edge=args.min_edge, fee_override=args.poly_fee_rate, cross_event=not args.no_cross)
            except Exception as exc:
                print(f"ladder failed: {exc}", file=sys.stderr)
                return 2
            print(render_ladder_report(rep, top=args.top), flush=True)
            reports.append(rep)
            if args.log:
                with args.log.open("a") as fh:
                    fh.write(json.dumps(rep.to_dict(), default=str) + "\n")
            if i + 1 < args.repeat:
                print(f"\n... next snapshot in {args.interval:g}s\n", flush=True)
                _time.sleep(args.interval)
        if len(reports) > 1:
            print()
            print(summarize_snapshots(reports))
        return 0

    if args.cmd == "rewards":
        now = utcnow()
        log = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731
        try:
            configs = json.loads(args.configs.read_text()) if args.configs else None
            if args.books_only:
                rows = rewards_pocket(source.http, source.poly_books, now=now, min_rate=args.min_rate, configs=configs, log=log)
                print(render_pocket(rows, top=args.list, min_days=args.min_days))
                if args.json:
                    args.json.write_text(json.dumps([r.to_dict() for r in rows], indent=1, default=str))
                    print(f"\nwrote {args.json}")
                return 0
            report = rewards_survey(source.http, LiveTrades(cache_dir=args.cache_dir), source.poly_books, now=now, days=args.days, top=args.top, mid_n=args.mid, low_n=args.low, seed=args.seed, configs=configs, log=log)
        except Exception as exc:
            print(f"rewards failed: {exc}", file=sys.stderr)
            return 2
        print(render_rewards(report, case=args.case, top=args.list))
        if args.json:
            args.json.write_text(json.dumps(report.to_dict(), indent=1, default=str))
            print(f"\nwrote {args.json}")
        return 0

    if args.cmd == "signal":
        log = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731
        rows, problems = load_signal_csv(args.csv)
        for pr in problems:
            print(f"csv: {pr}", file=sys.stderr)
        if not rows:
            print("no usable rows", file=sys.stderr)
            return 2
        try:
            if args.fixtures:
                resolver, trades_src = FixtureResolver(args.fixtures), FixtureTrades(args.fixtures)
            else:
                resolver, trades_src = GammaResolver(source.http), LiveTrades(cache_dir=args.cache_dir)
            markets = resolver.resolve([r.market for r in rows])
            if not args.fixtures:
                need = {k for k in {r.market for r in rows} if k in markets and markets[k].resolved_yes is not None}
                have = sum(1 for k in need if trades_src.cached(markets[k].condition_id or markets[k].id))
                log(f"{len(markets)} of {len({r.market for r in rows})} markets found, {len(need)} resolved; {len(need) - have} trade tapes to fetch at about one a second (cached in {args.cache_dir}, so a stopped run resumes where it was)")
            report = score_signal(rows, markets, trades_src, horizon_min=args.horizon_min, edge=args.edge, fee_rate=args.fee, max_stale_hours=args.max_stale, log=log)
        except Exception as exc:
            print(f"signal failed: {exc}", file=sys.stderr)
            return 2
        print(render_signal(report, args.horizon_min, args.edge))
        if args.fill_wait > 0 and report.trades:
            from .crossings import executable_fills

            for f in executable_fills(report.scored, markets, trades_src, wait_hours=args.fill_wait, edge=args.edge, fee_rate=args.fee):
                print(f"  re-executed at the first print after each row on the side needed (within {args.fill_wait:g} h): {f.filled} of {f.trades} trades found one, {f.taken} still cleared the edge, P&L {'-' if f.pnl_per_100 is None else f'{f.pnl_per_100:+.2f}'}" + (f" ± {f.pnl_per_100_se:.2f}" if f.pnl_per_100_se is not None else "") + f" per $100" + ("" if f.adverse_move is None else f"; fills {f.adverse_move * 100:+.1f}c against you on average"))
        if args.json:
            args.json.write_text(json.dumps(report.to_dict(), indent=1, default=str))
            print(f"\nwrote {args.json}")
        return 0

    if args.cmd == "counts":
        return _run_counts(args, source)
    if args.cmd == "crossings":
        return _run_crossings(args, source)
    if args.cmd == "headroom":
        return _run_headroom(args, source)

    if args.cmd == "lp":
        return _run_lp(args, source)

    kwargs = _scan_kwargs(args)

    if args.cmd == "scan":
        now = utcnow()
        try:
            opps, stats = run_scan(source, now=now, **kwargs)
        except Exception as exc:  # surface proxy / network denials plainly
            print(f"scan failed: {exc}", file=sys.stderr)
            return 2
        print(render_text(opps, stats, args.budget, args.min_edge, now))
        if args.json:
            write_json(args.json, opps, stats, args.budget, now)
            print(f"\nwrote {args.json}")
        return 0

    # watch
    notifier = TelegramNotifier(args.telegram_token, args.telegram_chat) if args.telegram_token and args.telegram_chat else None
    kinds = set(ARB_KINDS) | ({"near_certain"} if args.include_near_certain else set())
    print(f"watching every {args.interval:g}s, logging to {args.log}; alerts {'on' if notifier else 'off'}; Ctrl-C to stop")
    try:
        watch(
            lambda now: run_scan(source, now=now, **kwargs),
            interval=args.interval,
            log=JsonlLog(args.log),
            notifier=notifier,
            kinds=kinds,
            iterations=args.iterations,
        )
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
