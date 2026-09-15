"""End-to-end scan: fetch -> cluster -> depth -> evaluate every leg pair."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from .arb import evaluate, top_edge
from .games import GAMES
from .matcher import Cluster, cluster_matches
from .models import Match, Opportunity

log = logging.getLogger("esports_arb")


def fetch_all(connectors, games: Iterable[str]) -> List[Match]:
    out: List[Match] = []
    for g in games:
        for c in connectors:
            try:
                ms = c.fetch(g)
                log.info("%-10s %-6s %3d matches", c.venue, g, len(ms))
                out += ms
            except Exception as exc:  # one venue failing must not kill the scan
                log.warning("%s %s failed: %s", c.venue, g, exc)
    return out


def _warnings(cluster: Cluster, a_match: Match, b_match: Match) -> List[str]:
    w = []
    if a_match.venue != b_match.venue and a_match.void_rule != b_match.void_rule:
        w.append(f"void-rule mismatch: {a_match.venue}='{a_match.void_rule}' vs "
                 f"{b_match.venue}='{b_match.void_rule}'")
    if a_match.start and b_match.start and abs(a_match.start - b_match.start) > timedelta(hours=1):
        w.append("listed start times differ by >1h — confirm it's the same match")
    now = datetime.now(timezone.utc)
    if a_match.start and a_match.start <= now:
        w.append("match may already be live — prices move fast, legging risk is high")
    if min(cluster.scores) < 0.95:
        w.append(f"fuzzy team-name match (score {min(cluster.scores):.2f}) — verify teams")
    return w


def leg_pairs(cluster: Cluster) -> List[Tuple[object, Match, object, Match]]:
    legs_a = [(leg, m) for m in cluster.members for leg in m.legs_a]
    legs_b = [(leg, m) for m in cluster.members for leg in m.legs_b]
    pairs = []
    for la, ma in legs_a:
        for lb, mb in legs_b:
            if la.instrument == lb.instrument:
                continue  # YES/NO of the same Kalshi market always sums to >= 1
            pairs.append((la, ma, lb, mb))
    return pairs


def scan(connectors, games: Optional[Iterable[str]] = None, *, min_edge: float = 0.0,
         max_cost: float = math.inf, threshold: float = 0.8, cross_only: bool = False,
         matches: Optional[List[Match]] = None) -> Tuple[List[Opportunity], List[Cluster]]:
    games = list(games or GAMES)
    matches = matches if matches is not None else fetch_all(connectors, games)
    clusters = cluster_matches(matches, threshold=threshold)
    by_venue = {c.venue: c for c in connectors}

    opps: List[Opportunity] = []
    for cl in clusters:
        if cross_only and len(cl.venues) < 2:
            continue
        # cheap top-of-book screen before paying for depth requests
        pairs = leg_pairs(cl)
        if not any((e := top_edge(la, lb)) is not None and e > min_edge for la, _, lb, _ in pairs):
            continue
        for m in cl.members:
            conn = by_venue.get(m.venue)
            if conn is not None:
                conn.load_depth(m)
        best_by_pair = {}
        for la, ma, lb, mb in leg_pairs(cl):
            if cross_only and ma.venue == mb.venue:
                continue
            opp = evaluate(la, lb, game=cl.game, title=f"{cl.anchor.team_a} vs {cl.anchor.team_b}",
                           start=cl.anchor.start, min_edge=min_edge, max_cost=max_cost)
            if opp:
                opp.warnings = _warnings(cl, ma, mb)
                need = max(la.min_size, lb.min_size)
                if opp.contracts < need:
                    opp.warnings.insert(0, f"size {opp.contracts:.0f} is below venue minimum order ({need:.0f})")
                key = (la.venue, lb.venue)
                if key not in best_by_pair or opp.profit > best_by_pair[key].profit:
                    best_by_pair[key] = opp
        opps += best_by_pair.values()
    opps.sort(key=lambda o: o.profit, reverse=True)
    return opps, clusters


def basis_rows(clusters: List[Cluster], ts: datetime) -> List[dict]:
    """Per-cluster cross-venue snapshot used by the recorder / research notebook."""
    rows = []
    for cl in clusters:
        if len(cl.venues) < 2:
            continue
        for m in cl.members:
            for team, legs in (("a", m.legs_a), ("b", m.legs_b)):
                for leg in legs:
                    rows.append({
                        "ts": ts.isoformat(), "game": cl.game,
                        "match": f"{cl.anchor.team_a} vs {cl.anchor.team_b}",
                        "start": cl.anchor.start.isoformat() if cl.anchor.start else "",
                        "venue": leg.venue, "side": leg.side, "team": team,
                        "ask": leg.best if leg.best is not None else "",
                        "ask_size": leg.best_size, "fee_model": leg.fee_model,
                        "fee_rate": leg.fee_rate,
                    })
    return rows
