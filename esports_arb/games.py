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


GAMES = {
    g.code: g
    for g in [
        Game("lol", "League of Legends", "KXLOLGAME", "10311", 18),
        Game("cs2", "Counter-Strike 2", "KXCS2GAME", "10310", 17),
        Game("val", "Valorant", "KXVALORANTGAME", "10369", 61),
        Game("dota2", "Dota 2", "KXDOTA2GAME", "10309", 16),
        Game("r6", "Rainbow Six Siege", "KXR6GAME", "10432", None),
        Game("rl", "Rocket League", "KXRLGAME", "10433", 59),
        Game("ow", "Overwatch", "KXOWGAME", "10430", None),
        Game("cod", "Call of Duty", "KXCODGAME", "10427", 56),
    ]
}

DEFAULT_GAMES = ["lol", "r6", "rl", "ow", "cs2", "val", "dota2", "cod"]
