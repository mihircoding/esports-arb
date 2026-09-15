import math

import pytest

from esports_arb.arb import evaluate, hedge_stakes, top_edge, walk_books
from esports_arb.models import Leg


def leg(venue, asks, fee="none", rate=0.0, team="A", inst=None):
    return Leg(venue=venue, team=team, asks=asks, instrument=inst or f"{venue}-{team}",
               fee_model=fee, fee_rate=rate)


def test_no_arb_when_prices_sum_above_one():
    a = leg("x", [(0.55, 100)])
    b = leg("y", [(0.47, 100)], team="B")
    assert top_edge(a, b) == pytest.approx(-0.02)
    assert evaluate(a, b) is None


def test_simple_arb_without_fees():
    a = leg("x", [(0.40, 100)])
    b = leg("y", [(0.55, 100)], team="B")
    o = evaluate(a, b)
    assert o.contracts == 100
    assert o.cost == pytest.approx(95)
    assert o.profit == pytest.approx(5)
    assert o.roi == pytest.approx(5 / 95)


def test_fees_can_kill_an_arb():
    # 0.49 + 0.50 = 0.99 -> 1% gross, but Kalshi+Polymarket fees at ~50c are ~3%
    a = leg("kalshi", [(0.49, 100)], fee="kalshi", rate=0.07)
    b = leg("polymarket", [(0.50, 100)], fee="polymarket", rate=0.05, team="B")
    assert top_edge(a, b) < 0
    assert evaluate(a, b) is None


def test_fees_matter_less_at_extreme_prices():
    # 0.10 + 0.88 = 0.98; fees ~0.63c + ~0.53c, so still an arb
    a = leg("kalshi", [(0.10, 100)], fee="kalshi", rate=0.07)
    b = leg("polymarket", [(0.88, 100)], fee="polymarket", rate=0.05, team="B")
    o = evaluate(a, b)
    assert o is not None and o.profit > 0
    assert o.fees == pytest.approx(0.63 + 100 * 0.05 * 0.88 * 0.12)


def test_walk_stops_when_marginal_unit_unprofitable():
    a = leg("x", [(0.40, 10), (0.45, 10), (0.60, 10)])
    b = leg("y", [(0.50, 15), (0.54, 100)], team="B")
    units, fa, fb = walk_books(a, b)
    # 10 @ .40+.50, 5 @ .45+.50, 5 @ .45+.54 (=.99 still < 1), then .60+.54 > 1
    assert units == 20
    assert sum(q for _, q in fa) == 20 and sum(q for _, q in fb) == 20
    o = evaluate(a, b)
    assert o.profit == pytest.approx(10 * 0.10 + 5 * 0.05 + 5 * 0.01)


def test_min_edge_filters_thin_levels():
    a = leg("x", [(0.40, 10), (0.45, 10)])
    b = leg("y", [(0.50, 15), (0.54, 100)], team="B")
    units, _, _ = walk_books(a, b, min_edge=0.02)
    assert units == 15  # the 1c level is dropped


def test_bankroll_cap():
    a = leg("x", [(0.40, 1000)])
    b = leg("y", [(0.50, 1000)], team="B")
    o = evaluate(a, b, max_cost=90)
    assert o.contracts == 100
    assert o.cost <= 90 + 1e-9


def test_kalshi_rounding_can_kill_one_lot():
    # 1 contract: gross edge 1c, Kalshi rounds 0.0175 -> 0.02
    a = leg("kalshi", [(0.50, 1)], fee="kalshi", rate=0.07)
    b = leg("y", [(0.49, 1)], team="B")
    assert top_edge(a, b) < 0 or evaluate(a, b) is None


def test_void_pnl_polymarket_vs_book():
    a = leg("polymarket", [(0.30, 100)])
    b = leg("book:pinnacle", [(0.60, 100)], team="B")
    o = evaluate(a, b)
    # cancelled: PM pays 0.50/share, book refunds 0.60/unit, cost 90
    assert o.void_pnl == pytest.approx(100 * (0.5 + 0.6) - 90)


def test_hedge_stakes_equalise_payout():
    prices = [1 / 2.5, 1 / 1.8]
    stakes = hedge_stakes(prices, 100)
    assert stakes[0] * 2.5 == pytest.approx(100)
    assert stakes[1] * 1.8 == pytest.approx(100)
