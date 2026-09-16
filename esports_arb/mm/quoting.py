"""Quote construction — shared by the backtest, paper trader and live trader.

Everything is in YES-price space of one Kalshi market ("Team X wins").

    reservation r = fair - skew * exposure_X          (Avellaneda–Stoikov-style inventory skew)
    bid = floor_tick(r - h)    ask = ceil_tick(r + h)
    post-only: bid <= kalshi_ask - tick, ask >= kalshi_bid + tick

`exposure_X` is our net long exposure to team X winning, counted across
*both* markets of the event (YES on X, NO on the opponent). Being long X
lowers both quotes so we buy X less eagerly and sell it more eagerly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Optional, Tuple

TICK = 0.01


@dataclass
class QuoteParams:
    half_spread: float = 0.03      # h: distance from reservation price
    size: int = 10                 # contracts per quote
    max_exposure: int = 50         # |net exposure| per event (contracts)
    skew: float = 0.0004           # price shift per contract of exposure
    ref_weight: float = 1.0        # 1 = Polymarket fair value only, 0 = Kalshi mid only
    min_price: float = 0.06        # don't quote heavy favourites/longshots
    max_price: float = 0.94
    stop_before_min: float = 5.0   # stop quoting this many minutes before start
    start_hours: float = 12.0      # begin quoting this many hours before start
    queue_frac: float = 0.25       # backtest: share of volume at our exact price we'd get
    maker_fee_rate: float = 0.0    # Kalshi esports series have no maker fee (fee schedule)
    tick: float = TICK

    def to_dict(self):
        return asdict(self)


def floor_tick(p: float, tick: float = TICK) -> float:
    return round(math.floor(p / tick + 1e-9) * tick, 4)


def ceil_tick(p: float, tick: float = TICK) -> float:
    return round(math.ceil(p / tick - 1e-9) * tick, 4)


def blend_fair(ref: Optional[float], k_bid: Optional[float], k_ask: Optional[float],
               w: float) -> Optional[float]:
    """Mix the reference (Polymarket) price with Kalshi's own mid."""
    k_mid = None
    if k_bid is not None and k_ask is not None and 0 < k_bid < k_ask < 1:
        k_mid = (k_bid + k_ask) / 2
    if ref is None or not (0 < ref < 1):
        return k_mid if w < 1 else None
    if k_mid is None or w >= 1:
        return ref
    return w * ref + (1 - w) * k_mid


def make_quotes(fair: Optional[float], exposure: float, p: QuoteParams,
                k_bid: Optional[float] = None, k_ask: Optional[float] = None
                ) -> Tuple[Optional[float], Optional[float]]:
    """Return (bid, ask) for one market; None means 'don't quote that side'."""
    if fair is None or not (p.min_price <= fair <= p.max_price):
        return None, None
    r = fair - p.skew * exposure
    bid = floor_tick(r - p.half_spread, p.tick)
    ask = ceil_tick(r + p.half_spread, p.tick)
    if k_ask is not None and 0 < k_ask < 1:
        bid = min(bid, round(k_ask - p.tick, 4))
    if k_bid is not None and 0 < k_bid < 1:
        ask = max(ask, round(k_bid + p.tick, 4))
    if exposure >= p.max_exposure:
        bid = None
    if exposure <= -p.max_exposure:
        ask = None
    if bid is not None and not (p.tick <= bid <= 1 - p.tick):
        bid = None
    if ask is not None and not (p.tick <= ask <= 1 - p.tick):
        ask = None
    return bid, ask


def kalshi_fee(rate: float, contracts: float, price: float) -> float:
    if rate <= 0 or contracts <= 0:
        return 0.0
    return math.ceil(rate * contracts * price * (1 - price) * 100 - 1e-9) / 100
