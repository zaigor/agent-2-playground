from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pm_scanner.cli import main
from pm_scanner.lp import PaperExchange, QuotePlan, Quoter, choose_markets, exit_blockers, make_plan, quote_prices, render_plan
from pm_scanner.polymarket import Book, Level, parse_market
from pm_scanner.rewards import PocketRow
from pm_scanner.unwind import unwind_into

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def _row(cid, q, rate=100.0, mid=0.30, spread=0.20, days=30.0, share=1.0, min_size=20.0, v=4.5, depth=1e6):
    """A pocket row whose book is one level each side at the touch, `depth` shares deep."""
    best_bid, best_ask = mid - spread / 2, mid + spread / 2
    u_yes = unwind_into([(best_bid, depth)], min_size) if best_bid > 0 else None
    u_no = unwind_into([(1 - best_ask, depth)], min_size) if best_ask < 1 else None
    return PocketRow(cid, q, rate, v, min_size, mid, spread, 0.0, 0.0, share, share, rate * share, rate * share, min_size, 100.0, 10.0, days,
                     exit_yes=u_yes.net / min_size if u_yes is not None and u_yes.complete else None, exit_no=u_no.net / min_size if u_no is not None and u_no.complete else None)


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
    plans = choose_markets(rows, markets, budget=50.0, max_markets=3, min_days=7, max_exit=10.0)
    assert [x.condition_id for x in plans] == ["0xa", "0xc"]  # 0xd would exceed $50 with the first two (18.8 + 19.1 + 19.1)
    big = _row("0xg", "Any perp charged in the Cornell case?", rate=600, mid=0.28, spread=0.06, days=120, min_size=50)
    markets["0xg"] = _market("0xg", "yg", "ng")
    capped = choose_markets(rows + [big], markets, budget=50.0, max_markets=3, min_days=7, max_exit=10.0)
    assert "0xg" not in [x.condition_id for x in capped]  # a $47 market would swallow a budget meant for three
    assert [x.condition_id for x in choose_markets(rows + [big], markets, budget=50.0, max_markets=1, min_days=7, max_exit=10.0)] == ["0xg"]
    assert sum(x.collateral for x in plans) <= 50.0
    plans2 = choose_markets(rows, markets, budget=100.0, max_markets=5, min_days=7, max_exit=10.0)
    assert [x.condition_id for x in plans2] == ["0xa", "0xc", "0xd"]
    reasons: dict = {}
    assert [x.condition_id for x in choose_markets(rows, markets, budget=50.0, max_markets=3, min_days=7, reasons=reasons)] == ["0xa"]  # the default exit cap
    assert reasons["undoing one fill into the book would lose more than $2"] == 2  # 0xc (25c wide) and 0xd (45c wide)
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


def test_lp_housekeeping_does_not_read_the_budget(capsys, monkeypatch):
    """`MAX_BUDGET_USD=29 lp --cancel-all` was refused on 4 Oct because the default --budget 50
    tripped the cap; cancelling, reading earnings and approving size no quote."""
    for var in ("POLY_PRIVATE_KEY", "POLY_WALLET", "POLY_RELAYER_KEY", "POLY_RELAYER_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
    for flags in (["--cancel-all"], ["--earnings", "2026-10-04"], ["--approve"]):
        assert main(["lp", "--max-budget", "29", *flags]) == 2
        err = capsys.readouterr().err
        assert "hard cap" not in err and "cannot connect" in err


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


class StrictPaperExchange(PaperExchange):
    """Cancels like the SDK: an order id must be a string (the 3 Oct smoke run died on a None)."""

    def cancel(self, order_ids):
        assert all(isinstance(i, str) for i in order_ids), order_ids
        super().cancel(order_ids)


def test_quoter_survives_a_partial_fill_followed_by_a_move(tmp_path):
    books = {"y": (0.25, 0.35)}
    x = StrictPaperExchange(books)
    q = Quoter(x, [_plan()], log_path=tmp_path / "lp.jsonl", clock=lambda: 0.0, printer=lambda s: None)
    q.step()
    bid = next(o for o in x.orders.values() if o.token == "y")
    bid.matched = 15.3  # partly filled, the rest still resting
    q.step()
    st = q.states[0]
    assert st.bid_done and st.bid_filled == 15.3 and st.bid_id is None and [o.token for o in x.orders.values()] == ["n"]
    fills = [json.loads(l) for l in (tmp_path / "lp.jsonl").read_text().splitlines() if '"fill"' in l]
    assert fills[0]["sell_now"] == 3.68 and fills[0]["loss_if_sold_now"] == 0.45 and fills[0]["book_absorbs"] is True  # 15.3 bought at 0.27, the bids at 0.25, fee 5%
    books["y"] = (0.22, 0.35)  # the others' mid moves two ticks: the ask is re-centred, the bid is not re-posted
    q.step()
    assert st.replaced == 1 and st.bid_id is None and st.ask_id is not None
    assert [o.token for o in x.orders.values()] == ["n"] and not st.stopped


def test_best_prices_ignores_our_own_orders():
    from pm_scanner.lp import best_prices

    bids = [(0.87, 20.0), (0.80, 100.0)]
    asks = [(0.92, 20.0), (0.97, 50.0)]
    assert best_prices(bids, asks) == (0.87, 0.92)
    assert best_prices(bids, asks, own_bids=[(0.87, 20.0)], own_asks=[(0.92, 20.0)]) == (0.80, 0.97)
    assert best_prices([(0.87, 35.0)], asks, own_bids=[(0.87, 20.0)], own_asks=[(0.92, 20.0)]) == (0.87, 0.97)  # someone joined our level
    assert best_prices([(0.87, 20.0)], [(0.92, 20.0)], own_bids=[(0.87, 20.0)], own_asks=[(0.92, 20.0)]) == (None, None)


def test_quoter_holds_its_quotes_when_nobody_else_is_in_the_book(tmp_path):
    seen = {"calls": 0}

    def book(token):
        seen["calls"] += 1
        return (0.25, 0.35) if seen["calls"] == 1 else (None, None)

    x = StrictPaperExchange(book_fn=book)
    q = Quoter(x, [_plan()], log_path=tmp_path / "lp.jsonl", clock=lambda: 0.0, printer=lambda s: None)
    q.step()
    q.step()
    assert q.states[0].replaced == 0 and len(x.orders) == 2


def test_unwind_walks_the_bids_and_charges_the_taker_fee():
    from pm_scanner.unwind import exit_cost, no_bids_from_yes_asks

    bids = [(0.72, 12.0), (0.73, 17.69)]  # the book on the morning of 4 Oct, out of order on purpose
    u = unwind_into(bids, 15.31)
    assert u.complete and abs(u.gross - 15.31 * 0.73) < 1e-9 and abs(u.fee - 0.05 * 15.31 * 0.73 * 0.27) < 1e-9 and round(u.net, 2) == 11.03
    deeper = unwind_into(bids, 25.0)
    assert deeper.complete and abs(deeper.gross - (17.69 * 0.73 + 7.31 * 0.72)) < 1e-9
    short = unwind_into(bids, 40.0)
    assert not short.complete and abs(short.filled - 29.69) < 1e-9
    assert exit_cost(0.87, 15.31, bids) == round(0.87 * 15.31 - u.net, 4) and exit_cost(0.87, 40.0, bids) is None
    empty = unwind_into([], 5.0)
    assert empty.net == 0.0 and not empty.complete and empty.avg_price is None
    assert no_bids_from_yes_asks([(0.80, 15.3), (0.99, 96.6)]) == [(0.2, 15.3), (0.01, 96.6)]


def test_plan_carries_the_exit_cost_and_the_live_gate_reads_it():
    """3 Oct: the smoke run's bid at 0.87 sat 14c above the next bid; one fill cost $3 to undo at once.
    The plan now prints that number before anything is placed, and a live run refuses it."""
    rows = [_row("0xa", "Cornell President out by October 31?", rate=200, mid=0.29, spread=0.07, days=31), _row("0xc", "Will Jay Clayton be Trump's AI czar?", rate=149, mid=0.61, spread=0.25, days=92)]
    markets = {r.condition_id: _market(r.condition_id, f"y{r.condition_id}", f"n{r.condition_id}") for r in rows}
    pa, pc = make_plan(rows[0], markets["0xa"]), make_plan(rows[1], markets["0xc"])
    assert pa.exit_cost == 0.32 and pc.exit_cost == 2.15  # (our price - what the touch pays net of fee) x 20 shares, worse side
    text = render_plan([pa, pc])
    assert "exit $" in text and "  0.32" in text and "  2.15" in text
    blockers = exit_blockers([pa, pc], 2.0)
    assert len(blockers) == 1 and "$2.15" in blockers[0] and "Jay Clayton" in blockers[0]
    assert exit_blockers([pa, pc], 2.5) == []
    thin = _row("0xt", "Thin?", rate=100, mid=0.5, spread=0.04, days=30, depth=5.0)  # five shares at the touch cannot take a 20-share fill
    markets["0xt"] = _market("0xt", "yt", "nt")
    pt = make_plan(thin, markets["0xt"])
    assert pt.exit_cost is None and "none" in render_plan([pt]) and "no exit" in exit_blockers([pt], 2.0)[0]
    reasons: dict = {}
    assert choose_markets([thin], markets, budget=50.0, max_markets=1, min_days=7, reasons=reasons) == [] and reasons["the book cannot absorb one fill: no exit"] == 1
    assert [p.condition_id for p in choose_markets(rows, markets, budget=50.0, max_markets=3, only={"0xc"})] == ["0xc"]  # --only still shows it; the live gate is separate


def test_positions_report_prices_by_the_bids_not_the_site_mark():
    """4 Oct, 06:00 UTC: the site said the position was worth $10.56 (midpoint 0.69); the bids paid $8.69."""
    from pm_scanner.lp import positions_report, render_positions

    class Http:
        def get_json(self, url, params=None):
            assert url.endswith("/positions") and params == {"user": "0xw", "sizeThreshold": 0}
            return [
                {"asset": "t1", "conditionId": "0xc", "size": 15.3076, "avgPrice": 0.87, "curPrice": 0.69, "title": "Rain during the Bahrain Grand Prix?", "outcome": "Yes", "endDate": "2026-10-12"},
                {"asset": "t2", "conditionId": "0xd", "size": 0, "avgPrice": 0.5, "curPrice": 0.5, "title": "closed", "outcome": "No", "endDate": ""},
            ]

    def books(tokens):
        assert tokens == ["t1"]
        return {t: Book(t, bids=[Level(0.57, 5.0), Level(0.58, 25.04)], asks=[Level(0.80, 15.3), Level(0.99, 96.6)]) for t in tokens}

    ps = positions_report(Http(), books, "0xw")
    assert len(ps) == 1 and ps[0].best_bid == 0.58 and ps[0].best_bid_size == 25.04 and ps[0].unwind.complete
    assert round(ps[0].cost, 2) == 13.32 and round(ps[0].site_value, 2) == 10.56 and round(ps[0].sell_now, 2) == 8.69 and round(ps[0].pnl_if_sold, 2) == -4.63
    text = render_positions(ps)
    assert "8.69" in text and "-4.63" in text and "Only the second is money" in text and "Rain during" in text
    assert render_positions([]) == "no open positions"
    d = ps[0].to_dict()
    assert d["book_absorbs"] is True and d["sell_now"] == round(ps[0].sell_now, 4)
