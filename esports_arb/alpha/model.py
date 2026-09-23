"""Correlated-maps pricing model (beta-binomial).

Map results within a series are positively correlated (a team that is
stronger than the market thinks tends to win *both* maps): on 2,273 settled
Polymarket BO3s, 2-0s happened 59.8% of the time vs 54.7% under independence.

Model: the per-map win probability p for team A is uncertain,
p ~ Beta(mean mu, intra-series correlation rho). Maps are independent *given* p.
Then for any map sequence with a wins for A and b for B:
    P(sequence) = E[p^a (1-p)^b] = B(alpha + a, beta + b) / B(alpha, beta)

Given the match price (the best-calibrated contract) and rho, solve for mu,
then price every map-winner and total-maps contract consistently.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Optional

from ..series.states import series_states


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def seq_prob(mu: float, rho: float, a: int, b: int) -> float:
    """E[p^a (1-p)^b] for p ~ Beta with mean mu and ICC rho (rho=0 -> independent maps)."""
    mu = min(max(mu, 1e-9), 1 - 1e-9)
    if rho <= 1e-9:
        return mu ** a * (1 - mu) ** b
    s = (1 - rho) / rho
    al, be = mu * s, (1 - mu) * s
    return math.exp(_log_beta(al + a, be + b) - _log_beta(al, be))


@dataclass
class SeriesPrices:
    mu: float
    match: float
    maps: Dict[int, float]          # P(A wins map i AND map i is played)
    played: Dict[int, float]        # P(map i is played)
    over: Dict[float, float]        # P(maps played > line)
    sweep_a: float                  # P(A wins without dropping a map)
    sweep_b: float

    def map_fair(self, index: int, unplayed_value: float = 0.5) -> float:
        """Fair price of 'A wins map i' when an unplayed map settles at `unplayed_value`."""
        return self.maps[index] + (1 - self.played[index]) * unplayed_value


def price_series(mu: float, rho: float, best_of: int = 3) -> SeriesPrices:
    states = series_states(best_of)
    match, maps, played, over = 0.0, {}, {}, {}
    sweep_a = sweep_b = 0.0
    need = best_of // 2 + 1
    for s in states:
        pr = seq_prob(mu, rho, s.count("A"), s.count("B"))
        if s[-1] == "A":
            match += pr
        for i, w in enumerate(s, 1):
            played[i] = played.get(i, 0.0) + pr
            if w == "A":
                maps[i] = maps.get(i, 0.0) + pr
        for line in [x + 0.5 for x in range(need, best_of)]:
            if len(s) > line:
                over[line] = over.get(line, 0.0) + pr
            else:
                over.setdefault(line, 0.0)
        if len(s) == need:
            if s[-1] == "A":
                sweep_a += pr
            else:
                sweep_b += pr
    for i in range(1, best_of + 1):
        maps.setdefault(i, 0.0)
        played.setdefault(i, 0.0)
    return SeriesPrices(mu, match, maps, played, over, sweep_a, sweep_b)


@lru_cache(maxsize=100_000)
def _solve_mu(match: float, rho: float, best_of: int) -> float:
    lo, hi = 1e-6, 1 - 1e-6
    for _ in range(60):
        mid = (lo + hi) / 2
        if price_series(mid, rho, best_of).match < match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def from_match_price(match: float, rho: float, best_of: int = 3) -> Optional[SeriesPrices]:
    if not (0.01 <= match <= 0.99):
        return None
    mu = _solve_mu(round(match, 4), round(rho, 4), best_of)
    return price_series(mu, rho, best_of)
