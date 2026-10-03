from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pm_scanner.cli import main
from pm_scanner.lp import PaperExchange, QuotePlan, Quoter, choose_markets, make_plan, quote_prices, render_plan
from pm_scanner.polymarket import parse_market
from pm_scanner.rewards import PocketRow

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def _row(cid, q, rate=100.0, mid=0.30, spread=0.20, days=30.0, share=1.0, min_size=20.0, v=4.5):
    return PocketRow(cid, q, rate, v, min_size, mid, spread, 0.0, 0.0, share, share, rate * share, rate * share, min_size, 100.0, 10.0, days)


def _market(cid, yes="y", no="n", accepting=True):
    return parse_market({"id": cid, "question": f"Q {cid}", "conditionId": cid, "outcomes": '["Yes", "No"]', "clobTokenIds": f'["{yes}", "{no}"]', "outcomePrices": '["0.5", "0.5"]', "acceptingOrders": accepting, "orderMinSize": 5})


def test_quote_prices_round_outward_and_stay_inside_the_grid():
    assert quote_prices(0.30, 0.0225, 0.01) == (0.27, 0.67)  # bid 0.2775 -> 0.27; ask 0.3225 -> 0.33 -> NO bid 0.67
    assert quote_prices(0.015, 0.0225, 0.01) == (0.01, 0.96)  # clipped at 1c; ask 0.0375 -> 0.04
    bid, no_bid = quote_prices(0.99, 0.0225, 0.01)
    assert bid <= 0.99 and 1 - no_bid > bid


def test_make_plan_and_choose_markets_respect_budget_filters_and_exclusions():
    rows = [
        _row("0xa", "Cornell President out by October 31?", rate=200, mid=0.29, spread=0.07, days=31),
        _row("0xb", "Will the highest temperature in Seoul be 21°C on October 1?", rate=210, mid=0.55, spread=0.03, days=1),
        _row("0xc", "Will Jay Clayton be Trump's AI czar?", rate=149, mid=0.61, spread=0.25, days=92),
        _row("0xd", "Will Nir Barkat be the next Israeli Minister of Finance?", rate=40, mid=0.26, spread=0.45, days=151),
        _row("0xe", "Will Bitcoin be above $200k on December 31?", rate=60, mid=0.10, spread=0.60, days=90),  # too wide
        _row("0xf", "MrBeast next video views between 29 and 29.5 million?", rate=250, mid=0.63, spread=0.08, days=1.2),  # short-dated, excluded family
    ]
    markets = {r.condition_id: _market(r.condition_id, f"y{r.condition_id}", f"n{r.condition_id}") for r in rows}
    p = make_plan(rows[0], markets["0xa"])
    assert p is not None and p.size == 20 and p.bid == 0.26 and p.ask == 0.32 and abs(p.collateral - 20 * (0.26 + 0.68)) < 1e-9
    plans = choose_markets(rows, markets, budget=50.0, max_markets=3, min_days=7)
    assert [x.condition_id for x in plans] == ["0xa", "0xc"]  # 0xd would exceed $50 with the first two (18.8 + 19.1 + 19.1)
    big = _row("0xg", "Any perp charged in the Cornell case?", rate=600, mid=0.28, spread=0.06, days=120, min_size=50)
    markets["0xg"] = _market("0xg", "yg", "ng")
    capped = choose_markets(rows + [big], markets, budget=50.0, max_markets=3, min_days=7)
    assert "0xg" not in [x.condition_id for x in capped]  # a $47 market would swallow a budget meant for three
    assert [x.condition_id for x in choose_markets(rows + [big], markets, budget=50.0, max_markets=1, min_days=7)] == ["0xg"]
    assert sum(x.collateral for x in plans) <= 50.0
    plans2 = choose_markets(rows, markets, budget=100.0, max_markets=5, min_days=7)
    assert [x.condition_id for x in plans2] == ["0xa", "0xc", "0xd"]
    only = choose_markets(rows, markets, budget=100.0, max_markets=5, only={"0xb"})
    assert [x.condition_id for x in only] == ["0xb"]  # --only skips the filters
    assert "total collateral" in render_plan(plans)


def _plan(cid="0xa", days=30.0, mid=0.30):
    return QuotePlan(cid, "Q", "y", "n", 0.01, 20.0, 0.0225, mid, 0.27, 0.67, 18.8, 100.0, 100.0, 100.0, days)


def test_quoter_places_recentres_retires_filled_sides_and_cancels_at_end(tmp_path):
    books = {"y": (0.25, 0.35)}
    x = PaperExchange(books)
    clock = [0.0]
    q = Quoter(x, [_plan()], log_path=tmp_path / "lp.jsonl", clock=lambda: clock[0], printer=lambda s: None)
    s = q.step()
    assert s["active"] == 1 and len(x.orders) == 2
    prices = sorted((o.token, o.price) for o in x.orders.values())
    assert prices == [("n", 0.67), ("y", 0.27)]  # mid 0.30: YES bid 0.27, YES ask 0.33 as a NO bid at 0.67
    # the mid does not move: nothing is replaced
    q.step()
    assert q.states[0].replaced == 0 and len(x.orders) == 2
    # the mid moves two ticks: both orders are re-centred
    books["y"] = (0.27, 0.37)
    q.step()
    assert q.states[0].replaced == 1 and sorted(o.price for o in x.orders.values()) == [0.29, 0.65]
    # the YES bid fills: that side is retired, the ask stays
    bid = next(o for o in x.orders.values() if o.token == "y")
    bid.matched = 20.0
    q.step()
    st = q.states[0]
    assert st.bid_done and st.bid_filled == 20.0 and not st.ask_done and [o.token for o in x.orders.values()] == ["n"]
    # the ask fills too: the market stops
    ask = next(o for o in x.orders.values() if o.token == "n")
    x.orders.pop(ask.id)  # vanished from the open list = filled
    q.step()
    assert st.stopped == "both sides filled" and not x.orders
    # a fresh market inside the resolution window is pulled, and run() cancels everything at the end
    q2 = Quoter(PaperExchange({"y": (0.25, 0.35)}), [_plan(days=1.0)], pull_before_end_hours=48, clock=lambda: clock[0], printer=lambda s: None)
    q2.step()
    assert q2.states[0].stopped == "resolution window"
    x3 = PaperExchange({"y": (0.25, 0.35)})
    q3 = Quoter(x3, [_plan()], log_path=tmp_path / "run.jsonl", clock=lambda: clock[0], printer=lambda s: None)
    q3.run(hours=1.0, interval=0.0, sleep=lambda s: clock.__setitem__(0, clock[0] + 1800))
    kinds = [json.loads(l)["kind"] for l in (tmp_path / "run.jsonl").read_text().splitlines()]
    assert kinds[0] == "start" and kinds[-1] == "end" and "scoring" in kinds and "earnings" in kinds and x3.log[-1][0] == "cancel_all"


def test_lp_refuses_budget_above_the_cap(capsys):
    assert main(["lp", "--budget", "500", "--max-budget", "50"]) == 2
    assert "hard cap" in capsys.readouterr().err


def test_describe_reads_the_sdk_approvals_dataclass():
    """The SDK's `missing` is a dataclass with erc20/erc1155 tuples, not a list (the first live
    --check on 3 Oct reported "'MissingTradingApprovals' object is not iterable")."""
    from dataclasses import dataclass

    from pm_scanner.lp import LiveExchange

    @dataclass(frozen=True)
    class Missing:
        erc20: tuple = ()
        erc1155: tuple = ()

    @dataclass(frozen=True)
    class State:
        missing: Missing
        is_fully_approved: bool

    @dataclass
    class Balance:
        balance: str
        allowances: dict

    class Client:
        wallet = "0xabc"
        wallet_type = "DEPOSIT_WALLET"

        def get_balance_allowance(self, asset_type):
            return Balance("29000000", {"0xe1": "1"})

        def get_trading_approvals_state(self):
            return State(Missing(), True)

        def is_gasless_ready(self):
            return True

    out = LiveExchange(Client()).describe()
    assert out["approvals_ok"] is True and out["approvals_missing"] == [] and "approvals_error" not in out
    assert out["collateral_balance"] == 29.0

    class Short(Client):
        def get_trading_approvals_state(self):
            return State(Missing(erc20=("usdc->ctf",)), False)

    out = LiveExchange(Short()).describe()
    assert out["approvals_ok"] is False and out["approvals_missing"] == ["usdc->ctf"]


def test_choose_markets_explains_an_empty_plan():
    from pm_scanner.lp import choose_markets
    from pm_scanner.rewards import PocketRow

    import inspect

    fields = list(inspect.signature(PocketRow).parameters)
    base = {f: 0.0 for f in fields}
    base.update(condition_id="c1", question="Will X happen?", url="", rate_per_day=40.0, max_spread=3.0, min_size=50.0, mid=0.5, spread=0.02, days_to_end=30.0, reward_low=40.0, reward_high=40.0)
    row = PocketRow(**{k: v for k, v in base.items() if k in fields})
    reasons: dict = {}
    plans = choose_markets([row], {}, budget=25.0, max_markets=3, reasons=reasons)
    assert plans == [] and reasons["not on Gamma or not accepting orders"] == 1 and reasons["per-market cap $"] == 13.33
