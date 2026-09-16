"""Scan live matches for series-structure arbitrage and report near-misses."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from .lp import SeriesArb, find_arbitrage, solve
from .markets import SeriesGroup, collect
from .model import implied_map_prob, match_prob


@dataclass
class GroupResult:
    group: SeriesGroup
    lp_cost: Optional[float]        # cheapest $1 guaranteed payout (after fees); < 1 = arbitrage
    arb: Optional[SeriesArb]
    same_venue_cost: dict           # venue -> LP cost using only that venue's contracts
    model_gap: Optional[float]      # match price minus independent-maps price (info only)


def _mid(quotes, contract_pred) -> Optional[float]:
    yes = [q for q in quotes if contract_pred(q.contract)]
    return min((q.ask for q in yes), default=None)


def model_gap(g: SeriesGroup) -> Optional[float]:
    """Best ask for 'A wins' vs the independent-maps price from the map-1/map-2 asks (both sides
    averaged to a mid). Informational: maps are not independent."""
    def mid(kind, index=0):
        a = _mid(g.quotes, lambda c: c.kind == kind and c.team == "A" and c.index == index)
        b = _mid(g.quotes, lambda c: c.kind == kind and c.team == "B" and c.index == index)
        if a is None or b is None:
            return None
        return (a + (1 - b)) / 2
    m, w1, w2 = mid("match"), mid("map", 1), mid("map", 2)
    if None in (m, w1, w2) or not g.best_of:
        return None
    return m - match_prob([w1, w2, (w1 + w2) / 2], g.best_of)


def analyse(g: SeriesGroup, min_edge: float = 0.0, max_cost: float = math.inf) -> GroupResult:
    opt, _, _ = solve(g.quotes, g.best_of)
    arb = find_arbitrage(g.quotes, g.best_of, min_edge=min_edge, max_cost=max_cost)
    per_venue = {}
    if len(g.venues) > 1:
        for v in g.venues:
            per_venue[v], _, _ = solve([q for q in g.quotes if q.venue == v], g.best_of)
    return GroupResult(g, opt, arb, per_venue, model_gap(g))


def scan(games: List[str], min_edge: float = 0.0, max_cost: float = math.inf) -> List[GroupResult]:
    return [analyse(g, min_edge, max_cost) for g in collect(games)]


def format_results(results: List[GroupResult], near: int = 10) -> str:
    lines = []
    arbs = sorted([r for r in results if r.arb], key=lambda r: -r.arb.profit)
    n_multi = sum(len(r.group.venues) > 1 for r in results)
    lines.append(f"{len(results)} best-of-N matches ({n_multi} on both venues), "
                 f"{sum(len(r.group.quotes) for r in results)} contract quotes, {len(arbs)} arbitrage baskets\n")
    for i, r in enumerate(arbs, 1):
        g, a = r.group, r.arb
        start = g.start.strftime("%b %d %H:%MZ") if g.start else "?"
        from datetime import datetime, timezone
        live = " LIVE — prices move every round" if g.start and g.start <= datetime.now(timezone.utc) else ""
        lines.append(f"#{i} [{g.game} BO{g.best_of}] {g.team_a} vs {g.team_b} ({start}){live}")
        lines.append(f"    cost per $1 guaranteed: {a.cost_per_unit:.4f} | payout ${a.units:.0f} | "
                     f"cost ${a.cost:.2f} (fees ${a.fees:.2f}) | profit ${a.profit:.2f} | ROI {a.roi * 100:.2f}%")
        for q, w, qty in a.legs:
            fc = q.fill_cost(qty)
            avg = fc[0] / qty if fc and qty else q.ask
            lines.append(f"      buy {qty:>8.2f} x {q.label:<40} best {q.ask:.3f} avg {avg:.3f}  (weight {w:.2f})")
        worst = min(a.payoffs.values())
        best = max(a.payoffs.values())
        lines.append(f"    payout across map sequences: ${worst:.2f} – ${best:.2f}"
                     + (f" | if cancelled: ${a.void_payoff:.2f}" if a.void_payoff is not None else ""))
        for w in a.warnings:
            lines.append(f"    ! {w}")
    if near:
        lines.append("\nclosest to arbitrage (LP cost of a guaranteed $1, after fees):")
        ranked = sorted([r for r in results if r.lp_cost is not None], key=lambda r: r.lp_cost)[:near]
        for r in ranked:
            g = r.group
            extra = "".join(f" | {v}-only {c:.4f}" for v, c in r.same_venue_cost.items() if c is not None)
            gap = f" | match - indep. model {r.model_gap * 100:+.1f}pp" if r.model_gap is not None else ""
            lines.append(f"  {r.lp_cost:.4f}  [{g.game} BO{g.best_of}] {g.team_a} vs {g.team_b} "
                         f"({'+'.join(sorted(g.venues))}, {len(g.quotes)} quotes){extra}{gap}")
    return "\n".join(lines)
