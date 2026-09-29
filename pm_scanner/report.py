from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .scans import Opportunity

ARB_KINDS = ("negrisk_buy_all_yes", "negrisk_buy_all_no", "kalshi_buy_all_yes", "kalshi_buy_all_no", "cross_platform")


def _fmt_size(v: float | None) -> str:
    return "?" if v is None else f"{v:.0f}"


def render_text(opps: list[Opportunity], stats: dict, budget: float, min_edge: float, now: datetime) -> str:
    lines: list[str] = []
    lines.append(f"== pm_scanner {now.strftime('%Y-%m-%d %H:%MZ')}  budget ${budget:.2f}  min net edge ${min_edge:.4f}/set ==")
    lines.append("scanned: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    arbs = [o for o in opps if o.kind in ARB_KINDS]
    research = [o for o in opps if o.kind not in ARB_KINDS]

    def block(title: str, items: list[Opportunity]) -> None:
        lines.append("")
        lines.append(f"-- {title} ({len(items)}) --")
        if not items:
            lines.append("   none")
            return
        lines.append(f"{'kind':<22}{'edge/set':>9}{'edge%':>7}{'fill':>6}{'sets@$':>8}{'profit':>8}  title")
        for o in items:
            lines.append(
                f"{o.kind:<22}{o.edge_per_set:>9.4f}{o.edge_pct * 100:>6.1f}%{_fmt_size(o.fillable_sets):>6}"
                f"{o.budget_sets:>8.1f}{o.est_profit:>8.2f}  {o.title[:70]}"
            )
            legs = " | ".join(f"{lg.side} '{lg.market[:28]}' @{lg.price:.3f} x{_fmt_size(lg.size)}" for lg in o.legs)
            lines.append(f"    legs: {legs}")
            if o.days_to_resolve is not None:
                lines.append(f"    pays: in ~{o.days_to_resolve:.1f} days, ~{(o.annualized or 0) * 100:.0f}% per year of locked capital")
            lines.append(f"    url:  {o.url}")
            for n in o.notes:
                lines.append(f"    note: {n}")

    block("ARBITRAGE, net of taker fees", arbs)
    block("NEAR-CERTAIN, not arbitrage, carries resolution risk", research)
    total = sum(o.est_profit for o in arbs)
    lines.append("")
    lines.append(
        f"If every arbitrage above filled at top-of-book with ${budget:.0f} spread across them: ${total:.2f} "
        f"(each one locks its capital until resolution; sizes are top-of-book only)"
    )
    return "\n".join(lines)


def write_json(path: Path, opps: list[Opportunity], stats: dict, budget: float, now: datetime) -> None:
    payload = {
        "generated_at": now.isoformat(),
        "budget": budget,
        "stats": stats,
        "opportunities": [o.to_dict() for o in opps],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
