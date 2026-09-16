"""Level-2 order books maintained from snapshots and incremental updates."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Level = Tuple[float, float]


def _p(x) -> float:
    return round(float(x), 4)


@dataclass
class PolyBook:
    """One Polymarket outcome token: bids and asks keyed by price."""

    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)
    ts: float = 0.0          # local receive time (s)
    ready: bool = False      # a full snapshot has been applied

    def snapshot(self, bids, asks, ts: Optional[float] = None) -> None:
        self.bids = {_p(b["price"]): float(b["size"]) for b in bids if float(b["size"]) > 0}
        self.asks = {_p(a["price"]): float(a["size"]) for a in asks if float(a["size"]) > 0}
        self.ready = True
        self.ts = ts or time.time()

    def set_level(self, side: str, price, size, ts: Optional[float] = None) -> None:
        """Polymarket price_change carries the new *total* size at that price."""
        book = self.bids if side.upper() == "BUY" else self.asks
        p, s = _p(price), float(size)
        if s <= 0:
            book.pop(p, None)
        else:
            book[p] = s
        self.ts = ts or time.time()

    def ask_ladder(self) -> List[Level]:
        return sorted(self.asks.items())

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None


@dataclass
class KalshiBook:
    """One Kalshi market. Kalshi publishes bids only, for YES and for NO.

    YES ask at p  ==  NO bid at 1 - p   (and vice versa).
    """

    yes: Dict[float, float] = field(default_factory=dict)   # YES bids
    no: Dict[float, float] = field(default_factory=dict)    # NO bids
    ts: float = 0.0
    ready: bool = False
    depth: str = "full"      # "full" (websocket / orderbook REST) or "top" (poller)

    def snapshot(self, yes_levels, no_levels, ts: Optional[float] = None, depth: str = "full") -> None:
        self.yes = {_p(p): float(s) for p, s in (yes_levels or []) if float(s) > 0}
        self.no = {_p(p): float(s) for p, s in (no_levels or []) if float(s) > 0}
        self.ready, self.depth = True, depth
        self.ts = ts or time.time()

    def apply_delta(self, side: str, price, delta, ts: Optional[float] = None) -> None:
        book = self.yes if side == "yes" else self.no
        p = _p(price)
        s = book.get(p, 0.0) + float(delta)
        if s <= 1e-9:
            book.pop(p, None)
        else:
            book[p] = round(s, 4)
        self.ts = ts or time.time()

    def ask_ladder(self, side: str) -> List[Level]:
        """Asks to BUY `side` ('yes'|'no'), rebuilt from the opposite side's bids."""
        opp = self.no if side == "yes" else self.yes
        return sorted((round(1 - p, 4), s) for p, s in opp.items())

    def best_yes_bid(self) -> Optional[float]:
        return max(self.yes) if self.yes else None

    def best_yes_ask(self) -> Optional[float]:
        return round(1 - max(self.no), 4) if self.no else None


class BookStore:
    def __init__(self):
        self.poly: Dict[str, PolyBook] = {}
        self.kalshi: Dict[str, KalshiBook] = {}

    def pm(self, token: str) -> PolyBook:
        return self.poly.setdefault(token, PolyBook())

    def ks(self, ticker: str) -> KalshiBook:
        return self.kalshi.setdefault(ticker, KalshiBook())
