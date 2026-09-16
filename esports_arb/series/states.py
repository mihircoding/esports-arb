"""Outcome states of a best-of-N series and contract payoffs over them."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from typing import Optional, Tuple

State = Tuple[str, ...]  # sequence of map winners, e.g. ("A", "B", "A")


@lru_cache(maxsize=None)
def series_states(best_of: int) -> Tuple[State, ...]:
    """All map sequences that end the series (first to ceil(N/2) wins)."""
    need = best_of // 2 + 1
    out = []
    for n in range(need, best_of + 1):
        for seq in product("AB", repeat=n):
            a, b = seq.count("A"), seq.count("B")
            # the last map must be the clinching one
            if max(a, b) == need and min(a, b) == n - need and seq[-1] == ("A" if a == need else "B"):
                out.append(seq)
    return tuple(out)


def winner(s: State) -> str:
    return s[-1]


@dataclass(frozen=True)
class Contract:
    """A binary contract that pays $1 when its condition holds.

    kind:
      match     team wins the series
      map       team wins map `index`; `unplayed` = payoff if that map is never played
      handicap  team wins by more than `line` maps (line = 1.5 -> 2-0 in a BO3)
      total     `side` in {"over","under"} on the number of maps played vs `line`
    """
    kind: str
    team: str = "A"            # "A" | "B"
    index: int = 0
    line: float = 0.0
    side: str = "over"
    unplayed: Optional[float] = 0.5   # None -> unknown; treated as 0 (worst case for a buyer)
    void: Optional[float] = 0.5       # payoff if the whole match is cancelled; None -> unknown

    def payoff(self, s: State) -> float:
        if self.kind == "match":
            return 1.0 if winner(s) == self.team else 0.0
        if self.kind == "map":
            if self.index > len(s):
                return self.unplayed if self.unplayed is not None else 0.0
            return 1.0 if s[self.index - 1] == self.team else 0.0
        if self.kind == "handicap":
            mine = s.count(self.team)
            other = len(s) - mine
            return 1.0 if mine - other > self.line else 0.0
        if self.kind == "total":
            over = len(s) > self.line
            return 1.0 if over == (self.side == "over") else 0.0
        raise ValueError(self.kind)

    def complement(self) -> "Contract":
        """The other outcome of the same binary market."""
        other = "B" if self.team == "A" else "A"
        unp = None if self.unplayed is None else 1 - self.unplayed
        void = None if self.void is None else 1 - self.void
        if self.kind in ("match", "map"):
            return Contract(self.kind, other, self.index, self.line, self.side, unp, void)
        if self.kind == "handicap":
            # "A -1.5" <-> "B +1.5"  ==  B wins by more than -1.5 maps
            return Contract("handicap", other, 0, -self.line, self.side, unp, void)
        if self.kind == "total":
            return Contract("total", self.team, 0, self.line, "under" if self.side == "over" else "over",
                            unp, void)
        raise ValueError(self.kind)

    def describe(self) -> str:
        t = self.team
        if self.kind == "match":
            return f"{t} wins match"
        if self.kind == "map":
            return f"{t} wins map {self.index}"
        if self.kind == "handicap":
            return f"{t} {-self.line:+g} maps"
        return f"{self.side} {self.line:g} maps"
