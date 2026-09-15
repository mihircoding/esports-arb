"""Cluster the same real-world match across venues."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import List

from .models import Match
from .normalize import pair_score


@dataclass
class Cluster:
    game: str
    anchor: Match
    members: List[Match] = field(default_factory=list)
    scores: List[float] = field(default_factory=list)

    @property
    def venues(self):
        return {m.venue for m in self.members}


def _flip(m: Match) -> Match:
    return Match(venue=m.venue, game=m.game, team_a=m.team_b, team_b=m.team_a, start=m.start,
                 title=m.title, legs_a=m.legs_b, legs_b=m.legs_a, url=m.url, void_rule=m.void_rule)


def cluster_matches(matches: List[Match], threshold: float = 0.8,
                    window: timedelta = timedelta(hours=6)) -> List[Cluster]:
    """Greedy clustering. Members are re-oriented so team_a/team_b line up with the anchor."""
    clusters: List[Cluster] = []
    for m in matches:
        best, best_score, best_swap = None, 0.0, False
        for c in clusters:
            if c.game != m.game or m.venue in c.venues:
                continue
            a = c.anchor
            if a.start and m.start and abs(a.start - m.start) > window:
                continue
            score, swapped = pair_score(a.team_a, a.team_b, m.team_a, m.team_b)
            if score > best_score:
                best, best_score, best_swap = c, score, swapped
        if best is not None and best_score >= threshold:
            best.members.append(_flip(m) if best_swap else m)
            best.scores.append(best_score)
        else:
            clusters.append(Cluster(game=m.game, anchor=m, members=[m], scores=[1.0]))
    return clusters
