"""Venue fee models.

Kalshi (taker):   fee = ceil_to_cent(0.07 * C * P * (1 - P))   per order
Polymarket:       fee = C * rate * P * (1 - P)                  (sports rate 0.05, taker only)
Sportsbooks:      no explicit fee — the vig is already inside the odds.

Both exchange fees are proportional to P(1-P), i.e. the variance of a
Bernoulli(P) payoff. They peak at P = 0.5 and vanish at the extremes, which
matters for arbitrage: a pair of 50c legs pays the most in fees.
"""
from __future__ import annotations

import math

KALSHI_TAKER_RATE = 0.07
POLYMARKET_SPORTS_RATE = 0.05


def kalshi_fee(contracts: float, price: float, rate: float = KALSHI_TAKER_RATE) -> float:
    raw = rate * contracts * price * (1.0 - price)
    # round *up* to the next cent; subtract a tiny epsilon so 0.07 doesn't become 0.08
    return math.ceil(raw * 100 - 1e-9) / 100


def polymarket_fee(shares: float, price: float, rate: float = POLYMARKET_SPORTS_RATE) -> float:
    return shares * rate * price * (1.0 - price)


def fee_for(model: str, qty: float, price: float, rate: float = 0.0) -> float:
    if model == "kalshi":
        return kalshi_fee(qty, price, rate or KALSHI_TAKER_RATE)
    if model == "polymarket":
        return polymarket_fee(qty, price, rate or POLYMARKET_SPORTS_RATE)
    return 0.0


def marginal_fee_per_unit(model: str, price: float, rate: float = 0.0) -> float:
    """Un-rounded fee per $1 payout — used for top-of-book screening."""
    if model == "kalshi":
        return (rate or KALSHI_TAKER_RATE) * price * (1 - price)
    if model == "polymarket":
        return (rate or POLYMARKET_SPORTS_RATE) * price * (1 - price)
    return 0.0
