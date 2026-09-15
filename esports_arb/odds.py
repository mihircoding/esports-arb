"""Odds conversions and de-vigging."""
from __future__ import annotations

from typing import Sequence, List


def american_to_decimal(american: float) -> float:
    if american == 0:
        raise ValueError("American odds cannot be 0")
    return 1 + (american / 100 if american > 0 else 100 / abs(american))


def decimal_to_price(decimal_odds: float) -> float:
    """Decimal odds -> cost per $1 of payout (the 'implied probability')."""
    if decimal_odds <= 1:
        raise ValueError("decimal odds must be > 1")
    return 1.0 / decimal_odds


def price_to_decimal(price: float) -> float:
    return 1.0 / price


def overround(prices: Sequence[float]) -> float:
    """Sum of implied probabilities minus 1. Negative => arbitrage (before fees)."""
    return sum(prices) - 1.0


def devig_multiplicative(prices: Sequence[float]) -> List[float]:
    s = sum(prices)
    return [p / s for p in prices]


def devig_shin(prices: Sequence[float], tol: float = 1e-10) -> List[float]:
    """Shin (1993) de-vig for a 2+ way market.

    Models the bookmaker's margin as protection against a fraction z of
    insiders; tends to shade longshots more than favourites, which matches
    the empirical favourite-longshot bias. Solved by bisection on z.
    """
    booksum = sum(prices)
    n = len(prices)
    if booksum <= 1:
        return devig_multiplicative(prices)

    def probs(z: float) -> List[float]:
        return [
            ((z * z + 4 * (1 - z) * p * p / booksum) ** 0.5 - z) / (2 * (1 - z))
            for p in prices
        ]

    lo, hi = 0.0, 0.999
    for _ in range(200):
        mid = (lo + hi) / 2
        if sum(probs(mid)) > 1:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    out = probs((lo + hi) / 2)
    s = sum(out)
    return [p / s for p in out] if n else out
