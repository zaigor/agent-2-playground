from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .fees import kalshi_maker_fee, kalshi_taker_fee, polymarket_taker_fee
from .israel import DEFAULT_SURPLUS_PAIRS, ErrorModel, render_israel, run_israel
from .flow import family_flow, render_family_flow
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
    fl.add_argument("--json", type=Path, default=None, help="also write every family's flow stats to this JSON file")

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
            rows = family_flow(events, trades_src, now=now, since=since, per_family=args.per_family, min_markets=args.min_markets, max_families=args.max_families, horizon_min=args.horizon_min, include_sport=not args.no_sport, seed=args.seed, log=log)
        except Exception as exc:
            print(f"flow failed: {exc}", file=sys.stderr)
            return 2
        print(render_family_flow(rows, top=args.top, sort=args.sort))
        if args.json:
            args.json.write_text(json.dumps([r.to_dict() for r in rows], indent=1, default=str))
            print(f"\nwrote {args.json}")
        return 0

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
