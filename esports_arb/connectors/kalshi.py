"""Kalshi connector (public, unauthenticated market data).

Kalshi lists each esports match as an *event* with two mutually exclusive
markets: "Team A wins" and "Team B wins". That gives four ways to be long A:

    YES on "A wins"      ask = yes_ask(A)
    NO  on "B wins"      ask = 1 - yes_bid(B)

Kalshi's order book only stores *bids* on each side; an ask on YES is the
mirror of a bid on NO (price 1 - p), so we rebuild ask ladders from the
opposite side's bids.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from ..fees import KALSHI_TAKER_RATE
from ..games import GAMES
from ..http import get_json
from ..models import Leg, Match

BASE = "https://api.elections.kalshi.com/trade-api/v2"
ET = ZoneInfo("America/New_York")
_TICKER_TIME = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
VOID_RULE = "cancelled/forfeit-before-play -> resolves to fair market price"


def parse_ticker_start(event_ticker: str) -> Optional[datetime]:
    """KXRLGAME-26SEP151300R8TSM -> 2026-09-15 13:00 ET (as UTC)."""
    m = _TICKER_TIME.search(event_ticker)
    if not m:
        return None
    yy, mon, dd, hhmm = m.groups()
    try:
        local = datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hhmm[:2]), int(hhmm[2:]), tzinfo=ET)
    except (KeyError, ValueError):
        return None
    return local.astimezone(timezone.utc)


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def ladder_from_opposite_bids(bids) -> list:
    """[[p, size], ...] bids on the other side -> ascending asks on this side."""
    out = [(round(1 - _f(p), 4), _f(s)) for p, s in bids if _f(s) > 0]
    return sorted(out)


class KalshiConnector:
    venue = "kalshi"

    def __init__(self, depth: bool = True):
        self.depth = depth

    # ---- discovery -------------------------------------------------------
    def fetch(self, game_code: str) -> List[Match]:
        game = GAMES[game_code]
        if not game.kalshi_series:
            return []
        events, cursor = [], None
        while True:
            params = {"series_ticker": game.kalshi_series, "status": "open",
                      "with_nested_markets": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = get_json(f"{BASE}/events", params)
            events += data.get("events", [])
            cursor = data.get("cursor")
            if not cursor or not data.get("events"):
                break
        return [m for e in events if (m := self._parse_event(game_code, e))]

    def _parse_event(self, game_code: str, e: dict) -> Optional[Match]:
        mkts = [m for m in e.get("markets", []) if m.get("status") in ("active", "open")]
        if len(mkts) != 2:
            return None  # skip 3-way / partially settled events
        m1, m2 = mkts
        team_a, team_b = m1.get("yes_sub_title") or m1["title"], m2.get("yes_sub_title") or m2["title"]
        match = Match(
            venue=self.venue, game=game_code, team_a=team_a, team_b=team_b,
            start=parse_ticker_start(e["event_ticker"]), title=e.get("title", ""),
            url=f"https://kalshi.com/markets/{e['series_ticker'].lower()}/{e['event_ticker'].lower()}",
            void_rule=VOID_RULE,
        )
        match.legs_a = [self._top_leg(m1, team_a, "YES"), self._top_leg(m2, team_a, "NO")]
        match.legs_b = [self._top_leg(m2, team_b, "YES"), self._top_leg(m1, team_b, "NO")]
        return match

    @staticmethod
    def _top_leg(m: dict, team: str, side: str) -> Leg:
        if side == "YES":
            price, size = _f(m.get("yes_ask_dollars")), _f(m.get("yes_ask_size_fp"))
        else:  # NO ask mirrors the YES bid
            bid = _f(m.get("yes_bid_dollars"))
            price, size = (round(1 - bid, 4) if bid > 0 else 0.0), _f(m.get("yes_bid_size_fp"))
        asks = [(price, size)] if 0 < price < 1 and size > 0 else []
        label = f'{side} on "{m.get("yes_sub_title") or m["ticker"]} wins"'
        return Leg(venue="kalshi", team=team, asks=asks, instrument=m["ticker"], side=side,
                   fee_model="kalshi", fee_rate=KALSHI_TAKER_RATE, label=label, min_size=1)

    # ---- depth ----------------------------------------------------------
    def load_depth(self, match: Match) -> None:
        if not self.depth:
            return
        books = {}
        for leg in match.legs_a + match.legs_b:
            if leg.instrument not in books:
                try:
                    ob = get_json(f"{BASE}/markets/{leg.instrument}/orderbook", pause=0.05)
                    books[leg.instrument] = ob.get("orderbook_fp") or {}
                except RuntimeError:
                    books[leg.instrument] = None
            ob = books[leg.instrument]
            if ob is None:
                continue
            # YES asks come from NO bids and vice versa
            opposite = ob.get("no_dollars") if leg.side == "YES" else ob.get("yes_dollars")
            leg.asks = ladder_from_opposite_bids(opposite or [])
