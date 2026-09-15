"""Core data types.

Everything is expressed in *price per $1 of payout* so that exchange
contracts (Kalshi, Polymarket) and sportsbook decimal odds live on the
same scale: a sportsbook price of 2.50 is a "contract" costing 1/2.50 = $0.40.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

Level = Tuple[float, float]  # (price per $1 payout, size in $1-payout units)


@dataclass
class Leg:
    """One way to buy exposure to `team` winning."""

    venue: str               # "kalshi" | "polymarket" | "book:<slug>"
    team: str                # display name of the team this leg pays on
    asks: List[Level]        # ascending by price; size in contracts/shares/$payout
    instrument: str          # ticker / token id / fixture+outcome
    side: str = "YES"        # Kalshi: YES on team, or NO on the opponent
    fee_model: str = "none"  # "kalshi" | "polymarket" | "none"
    fee_rate: float = 0.0
    label: str = ""          # what is literally bought, e.g. 'NO on "NIP wins"'
    min_size: float = 0.0    # venue minimum order size

    @property
    def best(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def best_size(self) -> float:
        return self.asks[0][1] if self.asks else 0.0


@dataclass
class Match:
    """A single head-to-head match as listed on one venue."""

    venue: str
    game: str
    team_a: str
    team_b: str
    start: Optional[datetime]
    title: str
    legs_a: List[Leg] = field(default_factory=list)  # ways to be long team_a
    legs_b: List[Leg] = field(default_factory=list)  # ways to be long team_b
    url: str = ""
    void_rule: str = ""  # how the venue settles cancelled matches

    def key(self) -> str:
        return f"{self.venue}:{self.game}:{self.team_a} vs {self.team_b}"


@dataclass
class Opportunity:
    game: str
    title: str
    start: Optional[datetime]
    leg_a: Leg
    leg_b: Leg
    contracts: float        # $ payout locked in (both legs pay this much)
    cost: float             # total cash outlay incl. fees
    fees: float
    profit: float           # contracts - cost
    top_edge: float         # 1 - (pA + pB + fees) at top of book, per $1
    void_pnl: float = 0.0   # estimated P&L if the match is cancelled (see arb.void_payout)
    warnings: List[str] = field(default_factory=list)

    @property
    def roi(self) -> float:
        return self.profit / self.cost if self.cost > 0 else 0.0

    def annualized(self, now: datetime) -> Optional[float]:
        """Simple annualised return, assuming capital is tied up until ~start+6h."""
        if self.start is None:
            return None
        hours = max((self.start - now).total_seconds() / 3600 + 6, 1.0)
        return self.roi * (24 * 365 / hours)
