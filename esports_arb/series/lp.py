"""Linear-programming arbitrage search over series outcome states.

Given quotes (contract, ask, size, fee model), solve

    minimise   sum_j c_j x_j
    subject to sum_j payoff_j(s) x_j >= 1     for every outcome state s
               x_j >= 0

where c_j = ask_j + taker fee per contract. If the optimum is below 1, the
basket x pays at least $1 whatever happens and costs less — an arbitrage.
The basket is then scaled to the largest size the top-of-book allows, leg
quantities are rounded *up* to tradable units (which only raises payoffs),
and fees are recomputed exactly (Kalshi rounds up per order).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from ..fees import fee_for, marginal_fee_per_unit
from .states import Contract, series_states


@dataclass
class Quote:
    contract: Contract
    ask: float
    size: float
    venue: str                 # "kalshi" | "polymarket"
    instrument: str            # ticker+side / token id
    label: str = ""
    fee_model: str = "none"
    fee_rate: float = 0.0
    lot: float = 1.0           # smallest tradable increment
    min_size: float = 0.0
    ladder: Optional[List[tuple]] = None   # full ask ladder [(price, size)], best first

    @property
    def levels(self) -> List[tuple]:
        return self.ladder or [(self.ask, self.size)]

    @property
    def depth(self) -> float:
        return sum(sz for _, sz in self.levels)

    def fill_cost(self, qty: float) -> Optional[tuple]:
        """(notional, fees) to buy `qty` sweeping the ladder, or None if too deep."""
        left, notional, fees = qty, 0.0, 0.0
        for px, sz in self.levels:
            take = min(left, sz)
            if take <= 0:
                break
            notional += take * px
            fees += fee_for(self.fee_model, take, px, self.fee_rate)
            left -= take
        if left > 1e-9:
            return None
        return notional, fees

    @property
    def unit_cost(self) -> float:
        return self.ask + marginal_fee_per_unit(self.fee_model, self.ask, self.fee_rate)


@dataclass
class SeriesArb:
    best_of: int
    cost_per_unit: float               # LP optimum (< 1 means arbitrage)
    legs: List[tuple]                  # (quote, weight per unit, quantity)
    units: float                       # guaranteed $ payout
    cost: float                        # exact cash incl. fees for the rounded basket
    fees: float
    min_payoff: float
    profit: float                      # min_payoff - cost
    payoffs: dict = field(default_factory=dict)   # state -> payoff of rounded basket
    void_payoff: Optional[float] = None           # None if some leg's cancel payoff is unknown
    warnings: List[str] = field(default_factory=list)

    @property
    def roi(self) -> float:
        return self.profit / self.cost if self.cost else 0.0


def payoff_matrix(quotes: Sequence[Quote], best_of: int) -> np.ndarray:
    states = series_states(best_of)
    return np.array([[q.contract.payoff(s) for q in quotes] for s in states])


def solve(quotes: Sequence[Quote], best_of: int):
    """Return (optimum, weights) of the LP, or (None, None) if infeasible."""
    from scipy.optimize import linprog

    quotes = [q for q in quotes if 0 < q.ask < 1 and q.size > 0]
    if not quotes:
        return None, None, []
    P = payoff_matrix(quotes, best_of)
    c = np.array([q.unit_cost for q in quotes])
    res = linprog(c, A_ub=-P, b_ub=-np.ones(P.shape[0]), bounds=[(0, None)] * len(quotes),
                  method="highs")
    if not res.success:
        return None, None, quotes
    return float(res.fun), res.x, quotes


def find_arbitrage(quotes: Sequence[Quote], best_of: int, min_edge: float = 0.0,
                   max_cost: float = math.inf) -> Optional[SeriesArb]:
    opt, x, qs = solve(quotes, best_of)
    if opt is None or opt >= 1 - min_edge:
        return None
    used = [(q, w) for q, w in zip(qs, x) if w > 1e-9]
    states = series_states(best_of)

    def build(units: float):
        legs = []
        for q, w in used:
            qty = round(math.ceil(units * w / q.lot - 1e-9) * q.lot, 6)
            fc = q.fill_cost(qty)
            if fc is None:
                return None
            legs.append((q, w, qty, fc))
        payoffs = {s: sum(qty * q.contract.payoff(s) for q, _, qty, _ in legs) for s in states}
        notional = sum(fc[0] for *_, fc in legs)
        fees = sum(fc[1] for *_, fc in legs)
        return legs, payoffs, notional, fees, min(payoffs.values()) - notional - fees

    # profit(Q) is concave (prices only get worse deeper in the book), so the best
    # whole-unit size is found by checking every point where some leg changes level.
    cands = set()
    for q, w in used:
        cum = 0.0
        for _, sz in q.levels:
            cum += sz
            u = math.floor(cum / w + 1e-9)
            cands |= {u, u - 1}          # u-1 in case rounding up to a lot overshoots the level
    if max_cost < math.inf:
        cap = math.floor(max_cost / opt)
        cands = {min(u, cap) for u in cands} | {cap}
    best = None
    for units in sorted(u for u in cands if u >= 1):
        b = build(units)
        if b is not None and (best is None or b[4] > best[1][4]):
            best = (units, b)
    if best is None:
        return None
    units, (legs4, payoffs, notional, fees, profit) = best
    if profit <= 0:
        return None
    legs = [(q, w, qty) for q, w, qty, _ in legs4]
    cost = notional + fees
    min_pay = min(payoffs.values())
    void = None
    if all(q.contract.void is not None for q, _, _ in legs):
        void = sum(qty * q.contract.void for q, _, qty in legs)
    arb = SeriesArb(best_of, opt, legs, units, cost, fees, min_pay, profit, payoffs, void)
    if void is None:
        arb.warnings.append("some legs settle a cancelled match at 'fair value' (Kalshi) — void P&L unknown")
    elif void < cost:
        arb.warnings.append(f"if the match is cancelled the basket returns ${void:.2f} (< cost ${cost:.2f})")
    small = [q for q, _, qty in legs if qty < q.min_size]
    if small:
        arb.warnings.append("below venue minimum order size: " + ", ".join(q.label or q.instrument for q in small))
    if len({q.venue for q, _, _ in legs}) > 1:
        arb.warnings.append("legs on two venues — fill the thinner leg first")
    return arb
