"""Arbitrage math.

A head-to-head esports match has two mutually exclusive, exhaustive
outcomes (no draws in match-winner markets). If we can buy $1 of payout on
team A for price a and $1 of payout on team B for price b, then exactly one
leg pays and we receive $1 no matter who wins:

    profit per unit = 1 - a - b - fee_a(a) - fee_b(b)

An arbitrage exists iff a + b + fees < 1.

Order books have depth, and prices get worse as we sweep. Because both legs
must be filled in equal size (so the payout is identical in both states),
we walk the two ask ladders together, like merging two sorted lists, and
keep adding units while the *marginal* unit is still profitable. The
marginal cost curve is non-decreasing, so this greedy walk finds the
profit-maximising size (a discrete version of "trade until marginal cost = 1").
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from .fees import fee_for, marginal_fee_per_unit
from .models import Leg, Opportunity


def top_edge(a: Leg, b: Leg) -> Optional[float]:
    if a.best is None or b.best is None:
        return None
    fa = marginal_fee_per_unit(a.fee_model, a.best, a.fee_rate)
    fb = marginal_fee_per_unit(b.fee_model, b.best, b.fee_rate)
    return 1.0 - (a.best + b.best + fa + fb)


def walk_books(a: Leg, b: Leg, min_edge: float = 0.0, max_cost: float = math.inf,
               integer: bool = True) -> Tuple[float, List[Tuple[float, float]], List[Tuple[float, float]]]:
    """Return (units, fills_a, fills_b) for the profit-maximising equal-size sweep."""
    ia = ib = 0
    rem_a = a.asks[0][1] if a.asks else 0.0
    rem_b = b.asks[0][1] if b.asks else 0.0
    units, spent = 0.0, 0.0
    fills_a: List[Tuple[float, float]] = []
    fills_b: List[Tuple[float, float]] = []
    while ia < len(a.asks) and ib < len(b.asks):
        pa, pb = a.asks[ia][0], b.asks[ib][0]
        unit_cost = (pa + pb + marginal_fee_per_unit(a.fee_model, pa, a.fee_rate)
                     + marginal_fee_per_unit(b.fee_model, pb, b.fee_rate))
        if 1.0 - unit_cost <= min_edge:
            break
        q = min(rem_a, rem_b)
        if max_cost < math.inf:
            q = min(q, (max_cost - spent) / unit_cost)
        if q <= 1e-9:
            break
        fills_a.append((pa, q))
        fills_b.append((pb, q))
        units += q
        spent += q * unit_cost
        rem_a -= q
        rem_b -= q
        if rem_a <= 1e-9:
            ia += 1
            rem_a = a.asks[ia][1] if ia < len(a.asks) else 0.0
        if rem_b <= 1e-9:
            ib += 1
            rem_b = b.asks[ib][1] if ib < len(b.asks) else 0.0
        if spent >= max_cost - 1e-9:
            break
    if integer and units > 0:
        units = math.floor(units + 1e-9)
        fills_a, fills_b = _truncate(fills_a, units), _truncate(fills_b, units)
    return units, fills_a, fills_b


def _truncate(fills, units):
    out, left = [], units
    for p, q in fills:
        take = min(q, left)
        if take > 0:
            out.append((p, take))
        left -= take
        if left <= 0:
            break
    return out


def exact_cost(leg: Leg, fills) -> Tuple[float, float]:
    """Cash for the fills plus venue fees (Kalshi rounds up per fill level)."""
    notional = sum(p * q for p, q in fills)
    fees = sum(fee_for(leg.fee_model, q, p, leg.fee_rate) for p, q in fills)
    return notional, fees


def void_payout(leg: Leg, avg_price: float) -> float:
    """Estimated payout per unit if the match never happens.

    polymarket -> 0.50 (market resolves 50-50)
    kalshi     -> "fair market price"; we proxy it with the price we paid
    sportsbook -> bet void, stake refunded (= price per $1 payout)
    """
    if leg.venue == "polymarket":
        return 0.5
    return avg_price


def evaluate(a: Leg, b: Leg, *, game: str = "", title: str = "", start=None,
             min_edge: float = 0.0, max_cost: float = math.inf) -> Optional[Opportunity]:
    edge = top_edge(a, b)
    if edge is None or edge <= min_edge:
        return None
    units, fa, fb = walk_books(a, b, min_edge=min_edge, max_cost=max_cost)
    if units <= 0:
        return None
    na, fee_a = exact_cost(a, fa)
    nb, fee_b = exact_cost(b, fb)
    cost = na + nb + fee_a + fee_b
    profit = units - cost
    if profit <= 0:
        return None  # rounding on Kalshi fees can kill tiny arbs
    void = units * (void_payout(a, na / units) + void_payout(b, nb / units)) - cost
    return Opportunity(game=game, title=title, start=start, leg_a=a, leg_b=b,
                       contracts=units, cost=cost, fees=fee_a + fee_b,
                       profit=profit, top_edge=edge, void_pnl=void)


def hedge_stakes(prices: List[float], payout: float) -> List[float]:
    """Stakes that make every outcome pay `payout` (price = 1/decimal odds)."""
    return [payout * p for p in prices]
