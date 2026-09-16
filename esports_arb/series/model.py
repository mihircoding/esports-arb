"""Independent-maps model: match probability from per-map probabilities."""
from __future__ import annotations

from functools import lru_cache
from typing import Sequence

from .states import series_states


def match_prob(map_probs: Sequence[float], best_of: int = 3) -> float:
    """P(A wins series) if map i is won independently with probability map_probs[i]
    (the last value is reused for later maps if the list is short)."""
    total = 0.0
    for s in series_states(best_of):
        pr = 1.0
        for i, w in enumerate(s):
            p = map_probs[min(i, len(map_probs) - 1)]
            pr *= p if w == "A" else 1 - p
        if s[-1] == "A":
            total += pr
    return total


def implied_map_prob(match_price: float, best_of: int = 3, tol: float = 1e-10) -> float:
    """Constant per-map p that reproduces a match price (inverse of match_prob)."""
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if match_prob([mid], best_of) < match_price:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2


def sweep_prob(map_probs: Sequence[float], best_of: int = 3) -> float:
    """P(A wins without dropping a map)."""
    need = best_of // 2 + 1
    pr = 1.0
    for i in range(need):
        pr *= map_probs[min(i, len(map_probs) - 1)]
    return pr
