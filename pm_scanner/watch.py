"""Unattended polling loop and its log analyser.

`watch` is the zero-cost "paper trading" step: run it on any machine for a week
or two and the JSONL log tells you how many opportunities exist, how big they
are, and how long they survive before someone else takes them. No LLM, no
credits, no money at risk. `summarize` turns that log into the numbers that
decide whether, and with how much, to fund an account.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

import requests

from .scans import Opportunity, utcnow

ARB_KINDS = {"negrisk_buy_all_yes", "negrisk_buy_all_no", "kalshi_buy_all_yes", "kalshi_buy_all_no", "cross_platform"}


def opp_key(o: Opportunity) -> str:
    """Identity of an opportunity across scans: same structure at the same prices."""
    legs = ",".join(f"{lg.side}@{lg.price:.3f}" for lg in o.legs)
    return f"{o.kind}|{o.url}|{legs}"


@dataclass
class Sighting:
    key: str
    opp: Opportunity
    first_seen: datetime
    last_seen: datetime
    best_profit: float
    scans_seen: int = 1


@dataclass
class WatchState:
    active: dict[str, Sighting] = field(default_factory=dict)

    def update(self, opps: list[Opportunity], now: datetime) -> tuple[list[Sighting], list[Sighting]]:
        """Fold one scan into the state. Returns (newly seen, just disappeared)."""
        seen: set[str] = set()
        new: list[Sighting] = []
        for o in opps:
            k = opp_key(o)
            if k in seen:
                continue
            seen.add(k)
            s = self.active.get(k)
            if s is None:
                s = Sighting(k, o, now, now, o.est_profit)
                self.active[k] = s
                new.append(s)
            else:
                s.last_seen = now
                s.opp = o
                s.best_profit = max(s.best_profit, o.est_profit)
                s.scans_seen += 1
        gone = [s for k, s in self.active.items() if k not in seen]
        for s in gone:
            del self.active[s.key]
        return new, gone


class JsonlLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def write(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


class TelegramNotifier:
    """Optional push alerts. Create a bot with @BotFather, message it once, read chat id from getUpdates."""

    def __init__(self, token: str, chat_id: str, timeout: float = 10.0) -> None:
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> bool:
        try:
            r = requests.post(
                self.url,
                json={"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True},
                timeout=self.timeout,
            )
            return r.status_code < 400
        except requests.RequestException:
            return False


def format_alert(s: Sighting) -> str:
    o = s.opp
    legs = "\n".join(f"  {lg.side} {lg.market[:40]} @{lg.price:.3f} x{'?' if lg.size is None else f'{lg.size:.0f}'}" for lg in o.legs)
    flag = "" if o.executable else "  [below venue minimum]"
    return (
        f"{o.kind}  edge ${o.edge_per_set:.4f}/set ({o.edge_pct * 100:.1f}%)  "
        f"fill {'?' if o.fillable_sets is None else f'{o.fillable_sets:.0f}'}  profit@budget ${o.est_profit:.2f}{flag}\n"
        f"{o.title[:90]}\n{legs}\n{o.url}"
    )


def watch(
    scan_fn: Callable[[datetime], tuple[list[Opportunity], dict]],
    interval: float,
    log: JsonlLog,
    notifier: TelegramNotifier | None = None,
    kinds: set[str] | None = None,
    iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = utcnow,
    out: TextIO = sys.stdout,
) -> WatchState:
    """Re-scan every `interval` seconds; log scans, appearances, disappearances and errors.

    A failed scan is logged and skipped without touching the state, so a network
    blip never fakes a "gone" event.
    """
    state = WatchState()
    n = 0
    while iterations is None or n < iterations:
        n += 1
        now = now_fn()
        try:
            opps, stats = scan_fn(now)
        except Exception as exc:  # keep the loop alive; the log carries the reason
            log.write({"ts": now.isoformat(), "event": "error", "error": str(exc)})
            print(f"[{now:%Y-%m-%d %H:%M:%S}] scan error: {exc}", file=out)
        else:
            if kinds is not None:
                opps = [o for o in opps if o.kind in kinds]
            new, gone = state.update(opps, now)
            log.write({"ts": now.isoformat(), "event": "scan", "stats": stats, "active": len(state.active), "new": len(new), "gone": len(gone)})
            print(f"[{now:%Y-%m-%d %H:%M:%S}] active={len(state.active)} new={len(new)} gone={len(gone)} {stats}", file=out)
            for s in new:
                log.write({"ts": now.isoformat(), "event": "new", "key": s.key, "opp": s.opp.to_dict()})
                text = format_alert(s)
                print(text, file=out)
                if notifier is not None:
                    notifier.send(text)
            for s in gone:
                log.write(
                    {
                        "ts": now.isoformat(),
                        "event": "gone",
                        "key": s.key,
                        "kind": s.opp.kind,
                        "title": s.opp.title,
                        "first_seen": s.first_seen.isoformat(),
                        "duration_s": (now - s.first_seen).total_seconds(),
                        "scans_seen": s.scans_seen,
                        "best_profit": s.best_profit,
                        "edge_per_set": s.opp.edge_per_set,
                        "executable": s.opp.executable,
                    }
                )
        if iterations is None or n < iterations:
            sleep(interval)
    return state


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def summarize(path: Path | str) -> dict:
    """Aggregate a watch log: what appeared, how often, how big, how long it lasted."""
    scans = 0
    errors = 0
    first_ts: str | None = None
    last_ts: str | None = None
    new_by_kind: dict[str, list[dict]] = defaultdict(list)
    gone_by_kind: dict[str, list[dict]] = defaultdict(list)
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            ts = rec.get("ts")
            first_ts = first_ts or ts
            last_ts = ts or last_ts
            ev = rec.get("event")
            if ev == "scan":
                scans += 1
            elif ev == "error":
                errors += 1
            elif ev == "new":
                opp = rec.get("opp") or {}
                new_by_kind[opp.get("kind", "?")].append(opp)
            elif ev == "gone":
                gone_by_kind[rec.get("kind", "?")].append(rec)

    span_h = None
    if first_ts and last_ts:
        try:
            span_h = (datetime.fromisoformat(last_ts) - datetime.fromisoformat(first_ts)).total_seconds() / 3600.0
        except ValueError:
            span_h = None

    kinds: dict[str, dict] = {}
    for kind in sorted(set(new_by_kind) | set(gone_by_kind)):
        news = new_by_kind.get(kind, [])
        gones = gone_by_kind.get(kind, [])
        durations = [float(g.get("duration_s", 0.0)) for g in gones]
        kinds[kind] = {
            "seen": len(news),
            "executable": sum(1 for o in news if o.get("executable", True)),
            "median_edge_per_set": _median([float(o.get("edge_per_set", 0.0)) for o in news]),
            "median_profit_at_budget": _median([float(o.get("est_profit", 0.0)) for o in news]),
            "total_profit_if_all_taken": round(sum(float(o.get("est_profit", 0.0)) for o in news), 2),
            "resolved_sightings": len(gones),
            "median_duration_s": _median(durations),
            "share_lasting_60s_plus": (sum(1 for d in durations if d >= 60) / len(durations)) if durations else None,
        }
    top = sorted((o for xs in new_by_kind.values() for o in xs), key=lambda o: float(o.get("est_profit", 0.0)), reverse=True)[:10]
    return {
        "scans": scans,
        "errors": errors,
        "span_hours": span_h,
        # a per-day rate is noise on a log shorter than an hour
        "per_day_rate": {k: (v["seen"] / (span_h / 24.0)) if span_h and span_h >= 1.0 else None for k, v in kinds.items()},
        "kinds": kinds,
        "top": [{"kind": o.get("kind"), "title": o.get("title"), "est_profit": o.get("est_profit"), "edge_per_set": o.get("edge_per_set")} for o in top],
    }


def render_summary(summary: dict) -> str:
    def cell(x, spec: str, width: int) -> str:
        return format("-", f">{width}") if x is None else format(format(x, spec), f">{width}")

    span = summary["span_hours"]
    lines = [
        f"scans={summary['scans']} errors={summary['errors']} span={'-' if span is None else f'{span:.1f}h'}",
        "",
        f"{'kind':<22}{'seen':>6}{'exec':>6}{'/day':>7}{'med edge':>10}{'med $':>8}{'sum $':>8}{'med life':>10}{'>=60s':>7}",
    ]
    for kind, v in summary["kinds"].items():
        lines.append(
            f"{kind:<22}{v['seen']:>6}{v['executable']:>6}"
            + cell(summary["per_day_rate"].get(kind), ".1f", 7)
            + cell(v["median_edge_per_set"], ".4f", 10)
            + cell(v["median_profit_at_budget"], ".2f", 8)
            + cell(v["total_profit_if_all_taken"], ".2f", 8)
            + cell(v["median_duration_s"], ".0f", 10)
            + cell(v["share_lasting_60s_plus"], ".0%", 7)
        )
    if summary["top"]:
        lines.append("")
        lines.append("top sightings by profit at budget:")
        for t in summary["top"]:
            lines.append(f"  {float(t['est_profit'] or 0):>7.2f}  {str(t['kind']):<20} {str(t['title'])[:70]}")
    lines.append("")
    lines.append("'sum $' assumes every sighting was fully taken at top-of-book; real capture is a fraction of it.")
    lines.append("'/day' needs at least an hour of log; 'med life' is seconds from first sighting to disappearance.")
    return "\n".join(lines)
