"""Manual sportsbook odds from a JSON file.

For books without an API (DraftKings, FanDuel, Underdog, ...), paste odds into
a file like `data/manual_odds.example.json` and pass `--manual path`.
American (`"american": -150`) or decimal (`"decimal": 1.67`) odds accepted.
"""
from __future__ import annotations

import json
from typing import List

from ..models import Leg, Match
from ..odds import american_to_decimal, decimal_to_price
from .polymarket import _dt


def _price(o: dict) -> float:
    dec = o["decimal"] if "decimal" in o else american_to_decimal(float(o["american"]))
    return round(decimal_to_price(float(dec)), 6)


class ManualBookConnector:
    venue = "manual"

    def __init__(self, path: str):
        with open(path) as fh:
            self.rows = json.load(fh)

    def fetch(self, game_code: str) -> List[Match]:
        out = []
        for r in self.rows:
            if r["game"] != game_code:
                continue
            m = Match(venue=self.venue, game=game_code, team_a=r["team_a"], team_b=r["team_b"],
                      start=_dt(r.get("start")), title=f"{r['team_a']} vs {r['team_b']}",
                      void_rule="cancelled -> bet void, stake refunded")
            for book in r["books"]:
                size = float(book.get("max_payout", 500))
                m.legs_a.append(Leg(f"book:{book['name']}", r["team_a"], [(_price(book["a"]), size)],
                                    f"{book['name']}/{r['team_a']}"))
                m.legs_b.append(Leg(f"book:{book['name']}", r["team_b"], [(_price(book["b"]), size)],
                                    f"{book['name']}/{r['team_b']}"))
            out.append(m)
        return out

    def load_depth(self, match: Match) -> None:
        return None
