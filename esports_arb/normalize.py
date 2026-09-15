"""Team-name normalisation and fuzzy matching.

The same team is spelled differently on every venue
("Team Liquid" / "Liquid" / "TL", "FUT Esports" / "FUT"). We strip generic
tokens, apply a small alias table, then fall back to string similarity.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

GENERIC = {
    "team", "esports", "esport", "e", "sports", "gaming", "club", "gg", "the", "org",
}
# Note: 'academy' is deliberately NOT generic — "Spirit" and "Spirit Academy"
# are different rosters and must not be matched.

ROSTER_MARKERS = {"academy", "junior", "young", "youth", "rising", "b", "2", "ii",
                  "white", "blue", "fe", "female", "women", "ladies", "u21"}

ALIASES = {
    "tsm": "tsm", "team solomid": "tsm",
    "navi": "natus vincere", "na vi": "natus vincere", "natus vincere": "natus vincere",
    "vp": "virtus pro", "virtus pro": "virtus pro",
    "g2": "g2", "fnc": "fnatic", "tl": "liquid", "c9": "cloud9", "eg": "evil geniuses",
    "blg": "bilibili", "bilibili": "bilibili", "jdg": "jd", "jd": "jd",
    "wbg": "weibo", "weibo": "weibo", "tes": "top", "top": "top",
    "hle": "hanwha life", "hanwha life": "hanwha life", "gen g": "gen g", "geng": "gen g",
    "t1": "t1", "dk": "dplus kia", "dplus kia": "dplus kia", "dplus": "dplus kia",
    "mibr": "mibr", "furia": "furia", "faze": "faze", "mouz": "mouz", "mousesports": "mouz",
    "sr": "shopify rebellion", "shopify rebellion": "shopify rebellion",
    "flcn": "falcons", "falcons": "falcons",
}


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t not in GENERIC]
    s = " ".join(tokens) or s.strip()
    return ALIASES.get(s, s)


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    # one name is a strict token-superset of a single-token name: "r8" vs "r8 rl"
    if ta and tb and (ta <= tb or tb <= ta):
        extra = (ta ^ tb)
        if extra & ROSTER_MARKERS:
            return 0.5  # "Spirit" vs "Spirit Academy": different rosters
        return 0.9
    ratio = SequenceMatcher(None, na, nb).ratio()
    compact = SequenceMatcher(None, na.replace(" ", ""), nb.replace(" ", "")).ratio()
    return max(ratio, compact)


def pair_score(a1: str, b1: str, a2: str, b2: str):
    """Score two (team, team) pairs. Returns (score, swapped)."""
    same = min(similarity(a1, a2), similarity(b1, b2))
    swap = min(similarity(a1, b2), similarity(b1, a2))
    return (swap, True) if swap > same else (same, False)
