"""Polymarket connector (Gamma API for discovery, CLOB for order books).

Each esports match is an event; the match-winner market has
sportsMarketType == "moneyline" and two outcome tokens, one per team.
Buying the token for team A pays $1 if A wins.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..fees import POLYMARKET_SPORTS_RATE
from ..games import GAMES
from ..http import get_json, post_json
from ..models import Leg, Match

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
VOID_RULE = "cancelled, pre-match walkover, or postponed >14d -> resolves 50-50"


def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_book(book: dict) -> list:
    asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", []) if float(a["size"]) > 0]
    return sorted(asks)


class PolymarketConnector:
    venue = "polymarket"

    def __init__(self, lookback_hours: float = 3.0, max_pages: int = 5):
        self.lookback = timedelta(hours=lookback_hours)
        self.max_pages = max_pages

    def fetch(self, game_code: str) -> List[Match]:
        game = GAMES[game_code]
        if not game.polymarket_series:
            return []
        now = datetime.now(timezone.utc)
        events = []
        for page in range(self.max_pages):
            batch = get_json(f"{GAMMA}/events", {
                "series_id": game.polymarket_series, "closed": "false", "limit": 100,
                "offset": 100 * page, "order": "startDate", "ascending": "false",
            })
            if not batch:
                break
            events += batch
            starts = [_dt(e.get("startTime")) for e in batch]
            if all(s is None or s < now - self.lookback for s in starts):
                break

        matches, tokens = [], []
        for e in events:
            start = _dt(e.get("startTime"))
            if start is None or start < now - self.lookback:
                continue
            mk = next((m for m in e.get("markets", [])
                       if m.get("sportsMarketType") == "moneyline"
                       and not m.get("closed") and m.get("acceptingOrders", True)), None)
            if not mk:
                continue
            outcomes = json.loads(mk["outcomes"])
            token_ids = json.loads(mk["clobTokenIds"])
            if len(outcomes) != 2 or len(token_ids) != 2:
                continue
            sched = mk.get("feeSchedule") or {}
            rate = float(sched.get("rate", POLYMARKET_SPORTS_RATE)) if mk.get("feesEnabled") else 0.0
            legs = [Leg(venue="polymarket", team=o, asks=[], instrument=t,
                        fee_model="polymarket" if rate else "none", fee_rate=rate,
                        label=f'"{o}" token', min_size=float(mk.get("orderMinSize") or 5))
                    for o, t in zip(outcomes, token_ids)]
            match = Match(venue=self.venue, game=game_code, team_a=outcomes[0], team_b=outcomes[1],
                          start=_dt(mk.get("gameStartTime")) or start, title=e.get("title", ""),
                          legs_a=[legs[0]], legs_b=[legs[1]],
                          url=f"https://polymarket.com/event/{e.get('slug', '')}", void_rule=VOID_RULE)
            matches.append(match)
            tokens += token_ids

        books = {}
        for i in range(0, len(tokens), 40):
            chunk = tokens[i:i + 40]
            try:
                for b in post_json(f"{CLOB}/books", [{"token_id": t} for t in chunk]):
                    books[b["asset_id"]] = parse_book(b)
            except RuntimeError:
                continue
        for m in matches:
            for leg in m.legs_a + m.legs_b:
                leg.asks = books.get(leg.instrument, [])
        return matches

    def load_depth(self, match: Match) -> None:  # books already fetched in full
        return None
