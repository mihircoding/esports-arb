"""Per-game venue identifiers.

Each esports title lives under a different key on every venue. Keeping the
mapping in one place means adding a game is a one-line change.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Game:
    code: str
    name: str
    kalshi_series: Optional[str]      # match-winner series ticker
    polymarket_series: Optional[str]  # gamma-api series_id
    oddspapi_sport: Optional[int]     # OddsPapi sportId
    kalshi_map_series: Optional[str] = None      # per-map winner events
    kalshi_total_series: Optional[str] = None    # total maps played


GAMES = {
    g.code: g
    for g in [
        Game("lol", "League of Legends", "KXLOLGAME", "10311", 18, "KXLOLMAP", "KXLOLTOTALMAPS"),
        Game("cs2", "Counter-Strike 2", "KXCS2GAME", "10310", 17, "KXCS2MAP", "KXCS2TOTALMAPS"),
        Game("val", "Valorant", "KXVALORANTGAME", "10369", 61, "KXVALORANTMAP", "KXVALORANTTOTALMAPS"),
        Game("dota2", "Dota 2", "KXDOTA2GAME", "10309", 16, "KXDOTA2MAP", "KXDOTA2TOTALMAPS"),
        Game("r6", "Rainbow Six Siege", "KXR6GAME", "10432", None, "KXR6MAP", None),
        Game("rl", "Rocket League", "KXRLGAME", "10433", 59, "KXRLMAP", "KXRLTOTALMAPS"),
        Game("ow", "Overwatch", "KXOWGAME", "10430", None, None, None),
        Game("cod", "Call of Duty", "KXCODGAME", "10427", 56, "KXCODMAP", None),
    ]
}

DEFAULT_GAMES = ["lol", "r6", "rl", "ow", "cs2", "val", "dota2", "cod"]
