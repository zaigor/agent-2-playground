from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .fees import kalshi_maker_fee, kalshi_taker_fee, polymarket_taker_fee
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
from .watch import ARB_KINDS, JsonlLog, TelegramNotifier, render_summary, summarize, watch


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
        if not allow_augmented:  # Polymarket may add outcomes later, so "buy every YES" can stop covering the winner
            cands = [(ev, kind) for ev, kind in cands if not ev.neg_risk_augmented]
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
