"""Collect every series-structure contract for live matches on both venues.

Polymarket (per event):  moneyline, child_moneyline ("Map/Game N Winner"),
                         map_handicap (line on the first outcome), totals ("Games Total: O/U x.5")
Kalshi (per match code): *GAME (match), *MAP-<n> (map n winner), *TOTALMAPS ("Over x.5 maps")

Everything is oriented to one (team A, team B) pair and returned as
`lp.Quote`s that the LP can combine freely across venues.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..connectors.kalshi import BASE as KBASE, parse_ticker_start
from ..connectors.polymarket import CLOB, GAMMA, _dt, parse_book
from ..fees import KALSHI_TAKER_RATE, POLYMARKET_SPORTS_RATE
from ..games import GAMES
from ..http import get_json, post_json
from ..matcher import cluster_matches
from ..models import Match
from ..normalize import similarity
from .lp import Quote
from .states import Contract

log = logging.getLogger("esports_arb.series")
_BO = re.compile(r"\(BO(\d)\)", re.I)
_MAPN = re.compile(r"(?:Map|Game)\s+(\d+)\s+Winner", re.I)
_OVER = re.compile(r"over\s+(\d+(?:\.\d+)?)", re.I)


@dataclass
class SeriesGroup:
    game: str
    team_a: str
    team_b: str
    start: Optional[datetime]
    best_of: Optional[int]
    title: str
    quotes: List[Quote] = field(default_factory=list)
    venues: set = field(default_factory=set)
    urls: List[str] = field(default_factory=list)

    def match(self, venue: str) -> Match:
        return Match(venue, self.game, self.team_a, self.team_b, self.start, self.title)


def _side_team(name: str, a: str, b: str) -> Optional[str]:
    sa, sb = similarity(name, a), similarity(name, b)
    if max(sa, sb) < 0.8 or abs(sa - sb) < 0.05:
        return None
    return "A" if sa > sb else "B"


# ------------------------------------------------------------------ Polymarket
def polymarket_groups(game: str, lookback_h: float = 3.0, max_pages: int = 3) -> List[SeriesGroup]:
    sid = GAMES[game].polymarket_series
    if not sid:
        return []
    now = datetime.now(timezone.utc)
    events = []
    for page in range(max_pages):
        batch = get_json(f"{GAMMA}/events", {"series_id": sid, "closed": "false", "limit": 100,
                                             "offset": 100 * page, "order": "startDate", "ascending": "false"})
        events += batch or []
        if not batch or all((_dt(e.get("startTime")) or now) < now - timedelta(hours=lookback_h) for e in batch):
            break
    groups, token_quotes = [], defaultdict(list)
    for e in events:
        start = _dt(e.get("startTime"))
        m_bo = _BO.search(e.get("title", ""))
        if start is None or start < now - timedelta(hours=lookback_h) or not m_bo or int(m_bo.group(1)) < 2:
            continue
        ml = next((m for m in e.get("markets", []) if m.get("sportsMarketType") == "moneyline"), None)
        if not ml:
            continue
        a, b = json.loads(ml["outcomes"])
        g = SeriesGroup(game, a, b, start, int(m_bo.group(1)), e.get("title", ""), venues={"polymarket"},
                        urls=[f"https://polymarket.com/event/{e.get('slug', '')}"])
        for m in e.get("markets", []):
            if m.get("closed") or not m.get("acceptingOrders", True):
                continue
            kind = m.get("sportsMarketType")
            outs, toks = json.loads(m["outcomes"]), json.loads(m["clobTokenIds"])
            if len(outs) != 2:
                continue
            c0 = None
            if kind == "moneyline":
                c0 = Contract("match", "A")
                if outs[0] != a:
                    c0 = c0.complement()
            elif kind == "child_moneyline":
                mm = _MAPN.search(m.get("question", ""))
                t = _side_team(outs[0], a, b)
                if mm and t:
                    c0 = Contract("map", t, int(mm.group(1)))
            elif kind == "map_handicap" and m.get("line") is not None:
                t = _side_team(outs[0], a, b)
                if t:
                    c0 = Contract("handicap", t, line=-float(m["line"]))
            elif kind == "totals" and m.get("line") is not None and "Games Total" in m.get("question", ""):
                c0 = Contract("total", line=float(m["line"]),
                              side="over" if outs[0].lower().startswith("over") else "under")
            if c0 is None:
                continue
            sched = m.get("feeSchedule") or {}
            rate = float(sched.get("rate", POLYMARKET_SPORTS_RATE)) if m.get("feesEnabled") else 0.0
            for tok, c in ((toks[0], c0), (toks[1], c0.complement())):
                qt = Quote(c, 0.0, 0.0, "polymarket", tok, label=f"PM {_label(c, a, b)}",
                           fee_model="polymarket" if rate else "none", fee_rate=rate,
                           lot=0.01, min_size=float(m.get("orderMinSize") or 5))
                g.quotes.append(qt)
                token_quotes[tok].append(qt)
        groups.append(g)
    toks = list(token_quotes)
    for i in range(0, len(toks), 50):
        try:
            for bk in post_json(f"{CLOB}/books", [{"token_id": t} for t in toks[i:i + 50]]):
                asks = parse_book(bk)
                for qt in token_quotes.get(bk["asset_id"], []):
                    if asks:
                        qt.ask, qt.size = asks[0]
                        qt.ladder = asks
        except RuntimeError as exc:
            log.warning("polymarket books: %s", exc)
    for g in groups:
        g.quotes = [q for q in g.quotes if q.size > 0]
    return groups


def _label(c: Contract, a: str, b: str) -> str:
    name = {"A": a, "B": b}.get(c.team, c.team)
    if c.kind == "match":
        return f"{name} wins"
    if c.kind == "map":
        return f"{name} wins map {c.index}"
    if c.kind == "handicap":
        return f"{name} {-c.line:+g}"
    return f"{c.side} {c.line:g} maps"


# ------------------------------------------------------------------ Kalshi
def _kalshi_events(series: Optional[str]) -> List[dict]:
    if not series:
        return []
    out, cursor = [], None
    for _ in range(10):
        params = {"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = get_json(f"{KBASE}/events", params, pause=0.2)
        out += d.get("events", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("events"):
            break
    return out


def _core(event_ticker: str) -> tuple:
    body = event_ticker.split("-", 1)[1]
    parts = body.split("-")
    return parts[0], (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None)


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _kalshi_quotes(m: dict, c_yes: Contract, label: str) -> List[Quote]:
    out = []
    ya, ys = _f(m.get("yes_ask_dollars")), _f(m.get("yes_ask_size_fp"))
    yb, bs = _f(m.get("yes_bid_dollars")), _f(m.get("yes_bid_size_fp"))
    if 0 < ya < 1 and ys > 0:
        out.append(Quote(c_yes, ya, ys, "kalshi", m["ticker"] + ":yes", label=f"K YES {label}",
                         fee_model="kalshi", fee_rate=KALSHI_TAKER_RATE, lot=1, min_size=1))
    if 0 < yb < 1 and bs > 0:
        out.append(Quote(c_yes.complement(), round(1 - yb, 4), bs, "kalshi", m["ticker"] + ":no",
                         label=f"K NO {label}", fee_model="kalshi", fee_rate=KALSHI_TAKER_RATE, lot=1, min_size=1))
    return out


def kalshi_groups(game: str) -> List[SeriesGroup]:
    gm = GAMES[game]
    by_core: Dict[str, dict] = defaultdict(lambda: {"match": None, "maps": {}, "totals": None})
    for e in _kalshi_events(gm.kalshi_series):
        mk = [m for m in e.get("markets", []) if m.get("status") in ("active", "open")]
        if len(mk) == 2:
            by_core[_core(e["event_ticker"])[0]]["match"] = (e, mk)
    for e in _kalshi_events(gm.kalshi_map_series):
        core, idx = _core(e["event_ticker"])
        mk = [m for m in e.get("markets", []) if m.get("status") in ("active", "open")]
        if idx and len(mk) == 2:
            by_core[core]["maps"][idx] = mk
    for e in _kalshi_events(gm.kalshi_total_series):
        mk = [m for m in e.get("markets", []) if m.get("status") in ("active", "open")]
        if mk:
            by_core[_core(e["event_ticker"])[0]]["totals"] = mk

    groups = []
    for core, parts in by_core.items():
        if parts["match"] is None:
            continue
        e, mk = parts["match"]
        a, b = mk[0].get("yes_sub_title") or "", mk[1].get("yes_sub_title") or ""
        lines = [float(x.group(1)) for m in parts["totals"] or []
                 if (x := _OVER.search(m.get("yes_sub_title") or ""))]
        best_of = None
        if lines:
            best_of = 3 if max(lines) < 3 else 5
        if parts["maps"] and max(parts["maps"]) >= 4:
            best_of = 5
        g = SeriesGroup(game, a, b, parse_ticker_start(e["event_ticker"]), best_of, e.get("title", ""),
                        venues={"kalshi"},
                        urls=[f"https://kalshi.com/markets/{e['series_ticker'].lower()}"])
        kv = dict(unplayed=None, void=None)   # Kalshi: 'fair market price' -> unknown
        g.quotes += _kalshi_quotes(mk[0], Contract("match", "A", **kv), f"{a} wins")
        g.quotes += _kalshi_quotes(mk[1], Contract("match", "B", **kv), f"{b} wins")
        for idx, mm in parts["maps"].items():
            for m in mm:
                t = _side_team(m.get("yes_sub_title") or "", a, b)
                if t:
                    g.quotes += _kalshi_quotes(m, Contract("map", t, idx, **kv),
                                               f"{m.get('yes_sub_title')} wins map {idx}")
        for m in parts["totals"] or []:
            x = _OVER.search(m.get("yes_sub_title") or "")
            if x:
                g.quotes += _kalshi_quotes(m, Contract("total", line=float(x.group(1)), side="over", **kv),
                                           f"over {x.group(1)} maps")
        groups.append(g)
    return groups


# ------------------------------------------------------------------ merge
def _orient(q: Quote, swap: bool) -> Quote:
    if not swap or q.contract.kind == "total":
        return q
    c = q.contract
    flipped = Contract(c.kind, "B" if c.team == "A" else "A", c.index, c.line, c.side, c.unplayed, c.void)
    return Quote(flipped, q.ask, q.size, q.venue, q.instrument, q.label, q.fee_model, q.fee_rate,
                 q.lot, q.min_size)


def collect(games: List[str], venues=("polymarket", "kalshi")) -> List[SeriesGroup]:
    from ..normalize import pair_score

    out = []
    for game in games:
        groups = []
        if "polymarket" in venues:
            try:
                groups += polymarket_groups(game)
            except Exception as exc:
                log.warning("polymarket %s: %s", game, exc)
        if "kalshi" in venues:
            try:
                groups += kalshi_groups(game)
            except Exception as exc:
                log.warning("kalshi %s: %s", game, exc)
        matches, owner = [], {}
        for g in groups:
            m = g.match(next(iter(g.venues)))
            owner[id(m)] = g
            matches.append(m)
        for cl in cluster_matches(matches, threshold=0.85, window=timedelta(hours=3)):
            members = [owner[id(m)] for m in cl.members if id(m) in owner]
            # cluster_matches may hand back flipped copies; recover by names
            if len(members) != len(cl.members):
                members = [g for g in groups if any(
                    g.title == m.title and g.start == m.start for m in cl.members)]
            base = next((g for g in members if "polymarket" in g.venues), members[0])
            merged = SeriesGroup(game, base.team_a, base.team_b, base.start, base.best_of, base.title,
                                 list(base.quotes), set(base.venues), list(base.urls))
            for g in members:
                if g is base:
                    continue
                _, swapped = pair_score(base.team_a, base.team_b, g.team_a, g.team_b)
                merged.quotes += [_orient(q, swapped) for q in g.quotes]
                merged.venues |= g.venues
                merged.urls += g.urls
                merged.best_of = merged.best_of or g.best_of
            if merged.best_of and merged.best_of >= 2 and merged.quotes:
                out.append(merged)
    return out
