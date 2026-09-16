"""Position and P&L accounting for one two-market Kalshi event.

Positions are signed YES contracts per market (selling YES you don't own is
the same as buying NO at 1 - p; Kalshi nets the two automatically).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Fill:
    ts: float
    market: int          # 0 = team A market, 1 = team B market
    side: str            # "buy" (YES) | "sell" (YES)
    price: float
    qty: float
    fee: float
    fair: float          # fair value (of this market's YES) at fill time


@dataclass
class EventBook:
    pos: List[float] = field(default_factory=lambda: [0.0, 0.0])
    cash: float = 0.0
    fees: float = 0.0
    fills: List[Fill] = field(default_factory=list)
    max_abs_exposure: float = 0.0

    def exposure(self, market: int = 0) -> float:
        """Net long contracts on the team of `market` winning."""
        e = self.pos[0] - self.pos[1]
        return e if market == 0 else -e

    def fill(self, f: Fill) -> None:
        sgn = 1 if f.side == "buy" else -1
        self.pos[f.market] += sgn * f.qty
        self.cash -= sgn * f.qty * f.price + f.fee
        self.fees += f.fee
        self.fills.append(f)
        self.max_abs_exposure = max(self.max_abs_exposure, abs(self.exposure()))

    def capital(self) -> float:
        """Worst-case cash at risk right now (collateral Kalshi would lock)."""
        # the two markets are mutually exclusive: payoff in each state
        pay_a = self.pos[0] * 1 + self.pos[1] * 0
        pay_b = self.pos[0] * 0 + self.pos[1] * 1
        return max(0.0, -(self.cash + min(pay_a, pay_b)))

    def settle(self, a_won: bool) -> float:
        payoff = self.pos[0] if a_won else self.pos[1]
        return self.cash + payoff

    def mark(self, fair_a: float) -> float:
        return self.cash + self.pos[0] * fair_a + self.pos[1] * (1 - fair_a)

    def decomposition(self, a_won: bool) -> Dict[str, float]:
        """pnl = spread capture (vs fair at fill) + drift (settlement - fair) - fees."""
        capture = drift = 0.0
        for f in self.fills:
            sgn = 1 if f.side == "buy" else -1
            outcome = 1.0 if (a_won == (f.market == 0)) else 0.0
            capture += sgn * f.qty * (f.fair - f.price)
            drift += sgn * f.qty * (outcome - f.fair)
        return {"capture": capture, "drift": drift, "fees": -self.fees}
