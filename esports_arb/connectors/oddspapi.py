"""Sportsbook odds via OddsPapi (https://oddspapi.io) — requires ODDSPAPI_KEY.

OddsPapi aggregates Pinnacle, bet365, Stake, 1xBet, etc. for esports.
Match winner is market 171 with outcomes 171 (participant 1) and 172
(participant 2). Decimal odds d are converted to a price 1/d per $1 payout.

Sportsbooks don't publish depth, so each leg gets a single level whose size
is `max_payout` (a stake-limit assumption you should tune per book).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..games import GAMES
from ..http import get_json
from ..models import Leg, Match
from ..odds import decimal_to_price
from .polymarket import _dt

BASE = "https://api.oddspapi.io/v4"
MATCH_WINNER, P1, P2 = "171", "171", "172"
EXCHANGES = {"polymarket", "kalshi"}  # already covered natively
VOID_RULE = "cancelled -> bet void, stake refunded"


class OddsPapiConnector:
    venue = "books"

    def __init__(self, api_key: Optional[str] = None, days_ahead: int = 2,
                 max_payout: float = 500.0, bookmakers: Optional[List[str]] = None,
                 max_fixtures: int = 40):
        self.key = api_key or os.environ.get("ODDSPAPI_KEY")
        self.days = days_ahead
        self.max_payout = max_payout
        self.bookmakers = set(bookmakers) if bookmakers else None
        self.max_fixtures = max_fixtures

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def fetch(self, game_code: str) -> List[Match]:
        game = GAMES[game_code]
        if not self.enabled or game.oddspapi_sport is None:
            return []
        now = datetime.now(timezone.utc)
        fixtures = get_json(f"{BASE}/fixtures", {
            "apiKey": self.key, "sportId": game.oddspapi_sport, "hasOdds": "true",
            "from": now.date().isoformat(), "to": (now + timedelta(days=self.days)).date().isoformat(),
        }) or []
        out = []
        for fx in fixtures[: self.max_fixtures]:
            try:
                odds = get_json(f"{BASE}/odds", {"apiKey": self.key, "fixtureId": fx["fixtureId"]}, pause=0.2)
            except RuntimeError:
                continue
            m = self.parse_odds(game_code, fx, odds)
            if m:
                out.append(m)
        return out

    def parse_odds(self, game_code: str, fx: dict, odds: dict) -> Optional[Match]:
        a = fx.get("participant1Name") or odds.get("participant1Name")
        b = fx.get("participant2Name") or odds.get("participant2Name")
        if not a or not b:
            return None
        match = Match(venue=self.venue, game=game_code, team_a=a, team_b=b,
                      start=_dt(fx.get("startTime")), title=f"{a} vs {b}", void_rule=VOID_RULE)
        for slug, book in (odds.get("bookmakerOdds") or {}).items():
            if slug in EXCHANGES or (self.bookmakers and slug not in self.bookmakers):
                continue
            outcomes = ((book.get("markets") or {}).get(MATCH_WINNER) or {}).get("outcomes") or {}
            for oid, team, target in ((P1, a, match.legs_a), (P2, b, match.legs_b)):
                try:
                    dec = float(outcomes[oid]["players"]["0"]["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                if dec <= 1:
                    continue
                target.append(Leg(venue=f"book:{slug}", team=team,
                                  asks=[(round(decimal_to_price(dec), 6), self.max_payout)],
                                  instrument=f"{fx.get('fixtureId')}/{oid}", fee_model="none"))
        return match if match.legs_a and match.legs_b else None

    def load_depth(self, match: Match) -> None:
        return None
