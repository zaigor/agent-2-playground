import math
from datetime import datetime, timezone
from pathlib import Path

from pm_scanner.cli import run_scan
from pm_scanner.scans import normalize_title
from pm_scanner.sources import FixtureSource

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _scan(**overrides):
    kwargs = dict(
        budget=50.0,
        min_edge=0.005,
        max_events=100,
        platform="both",
        near_min_price=0.95,
        near_max_days=14,
        poly_fee_rate=None,
        kalshi_multiplier=1.0,
        do_cross=True,
        now=NOW,
    )
    kwargs.update(overrides)
    return run_scan(FixtureSource(FIXTURES), **kwargs)


def test_negrisk_buy_all_yes_is_priced_net_of_fees_and_sized_to_depth():
    opps, stats = _scan()
    o = next(o for o in opps if o.kind == "negrisk_buy_all_yes")
    assert "Ruritania" in o.title
    # asks 0.40+0.35+0.20 = 0.95; politics rate 0.04 -> fees 0.0251; edge 0.0249
    assert math.isclose(o.cost_per_set, 0.95)
    assert math.isclose(o.fee_per_set, 0.0251, abs_tol=1e-4)
    assert math.isclose(o.edge_per_set, 0.0249, abs_tol=1e-4)
    assert o.fillable_sets == 20  # Carol's ask has only 20 shares
    assert o.budget_sets == 20  # $50/0.95 = 52 affordable, but depth caps at 20
    assert math.isclose(o.est_profit, 0.50, abs_tol=0.01)
    assert stats["negrisk_candidates"] >= 2


def test_negrisk_buy_all_no_pays_n_minus_one():
    opps, _ = _scan()
    o = next(o for o in opps if o.kind == "negrisk_buy_all_no")
    assert "studio" in o.title.lower()
    assert o.payout_per_set == 2.0
    assert math.isclose(o.cost_per_set, 1.96)
    assert math.isclose(o.edge_per_set, 0.0079, abs_tol=1e-4)
    assert o.fillable_sets == 25
    assert any("negRiskAugmented" in n for n in o.notes)


def test_near_certain_is_listed_separately_with_risk_note():
    opps, _ = _scan()
    o = next(o for o in opps if o.kind == "near_certain")
    assert "rain" in o.title.lower()
    assert o.cost_per_set == 0.97
    assert o.fillable_sets is None
    assert any("NOT risk-free" in n for n in o.notes)
    # sorted after the arbitrage rows
    kinds = [x.kind for x in opps]
    assert kinds.index("near_certain") > max(i for i, k in enumerate(kinds) if k != "near_certain")


def test_cross_platform_hedge_uses_kalshi_book_and_both_fee_models():
    opps, stats = _scan()
    cross = [o for o in opps if o.kind == "cross_platform"]
    assert stats["cross_matches"] == 1
    assert len(cross) == 1  # the YES+NO direction costs 1.24 and is filtered out
    o = cross[0]
    # Poly NO ask 0.38 + Kalshi YES ask 0.42 (derived from NO bid 58c, size 150)
    assert math.isclose(o.cost_per_set, 0.80)
    assert math.isclose(o.fee_per_set, 0.04 * 0.38 * 0.62 + 0.02, abs_tol=1e-4)
    assert math.isclose(o.edge_per_set, 0.1706, abs_tol=1e-4)
    assert o.fillable_sets == 150
    assert o.budget_sets == 62.5


def test_kalshi_mutually_exclusive_scan_respects_fees():
    opps, _ = _scan(platform="kalshi")
    kinds = {(o.kind, o.title) for o in opps}
    # Mayor: asks sum to 0.97, fees ~0.043 -> negative edge, must not appear
    assert not any("mayor" in t.lower() for _, t in kinds)
    o = next(o for o in opps if o.kind == "kalshi_buy_all_yes")
    assert "cup" in o.title.lower()
    assert math.isclose(o.cost_per_set, 0.90)
    assert math.isclose(o.edge_per_set, 1 - 0.90 - 0.07 * (0.40 * 0.60 + 0.30 * 0.70 + 0.20 * 0.80), abs_tol=1e-4)
    assert o.fillable_sets == 30  # Team B book has 30 on the ask


def test_fee_override_changes_edge():
    free, _ = _scan(poly_fee_rate=0.0, platform="polymarket")
    o = next(o for o in free if o.kind == "negrisk_buy_all_yes")
    assert math.isclose(o.edge_per_set, 0.05, abs_tol=1e-6)


def test_title_normalization_expands_months_and_drops_stopwords():
    assert normalize_title("Will the Senate pass the bill by Oct 15?") == normalize_title("Will the Senate pass the bill by October 15?")
    assert normalize_title("Fed & rates") == "fed rates"
