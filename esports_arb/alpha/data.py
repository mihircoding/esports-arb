"""Settled Kalshi series dataset: match + map-winner + total-maps markets.

For each settled match (grouped by the shared ticker code, e.g. 26SEP161000EXRQUA):
  * every market's settlement value (1 / 0 / Kalshi's scalar 'fair value'),
  * 1-minute best bid/ask candles over [start - hours, start],
  * the public trade tape over the same window.

    python -m esports_arb alpha-data --days 21 --out data/alpha/kalshi_series.json.gz
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..connectors.kalshi import BASE, parse_ticker_start
from ..games import GAMES
from ..http import get_json
from ..mm.data import kalshi_candles, kalshi_trades
from ..series.markets import _core, _side_team

log = logging.getLogger("esports_arb.alpha.data")
_OVER = re.compile(r"over\s+(\d+(?:\.\d+)?)", re.I)


def _settle(m: dict) -> Optional[float]:
    r = m.get("result")
    if r == "yes":
        return 1.0
    if r == "no":
        return 0.0
    try:
        return float(m.get("settlement_value_dollars"))
    except (TypeError, ValueError):
        return None


def settled_events(series: Optional[str], since: datetime, max_pages: int = 30) -> List[dict]:
    if not series:
        return []
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"series_ticker": series, "status": "settled", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = get_json(f"{BASE}/events", params, pause=0.3)
        evs = d.get("events", [])
        out += evs
        cursor = d.get("cursor")
        starts = [parse_ticker_start(e["event_ticker"]) for e in evs]
        if not cursor or not evs or all(s is None or s < since for s in starts):
            break
    return [e for e in out if (parse_ticker_start(e["event_ticker"]) or since) >= since]


def group_matches(game: str, since: datetime) -> List[dict]:
    g = GAMES[game]
    by_core = defaultdict(lambda: {"match": None, "maps": {}, "totals": []})
    for e in settled_events(g.kalshi_series, since):
        if len(e.get("markets", [])) == 2:
            by_core[_core(e["event_ticker"])[0]]["match"] = e
    for e in settled_events(g.kalshi_map_series, since):
        core, idx = _core(e["event_ticker"])
        if idx and len(e.get("markets", [])) == 2:
            by_core[core]["maps"][idx] = e
    for e in settled_events(g.kalshi_total_series, since):
        by_core[_core(e["event_ticker"])[0]]["totals"] += e.get("markets", [])

    out = []
    for core, parts in by_core.items():
        e = parts["match"]
        if e is None or not (parts["maps"] or parts["totals"]):
            continue
        mk = e["markets"]
        a, b = mk[0].get("yes_sub_title") or "", mk[1].get("yes_sub_title") or ""
        if sorted(m.get("result") for m in mk) != ["no", "yes"]:
            continue   # cancelled / forfeited match
        rec = {"game": game, "core": core, "series": {"match": e["series_ticker"]},
               "start": parse_ticker_start(e["event_ticker"]).timestamp(), "team_a": a, "team_b": b,
               "a_won": mk[0]["result"] == "yes", "markets": []}
        rec["markets"].append({"kind": "match", "team": "A", "ticker": mk[0]["ticker"],
                               "series": e["series_ticker"], "settle": _settle(mk[0])})
        rec["markets"].append({"kind": "match", "team": "B", "ticker": mk[1]["ticker"],
                               "series": e["series_ticker"], "settle": _settle(mk[1])})
        for idx, me in parts["maps"].items():
            for m in me["markets"]:
                t = _side_team(m.get("yes_sub_title") or "", a, b)
                if t:
                    rec["markets"].append({"kind": "map", "team": t, "index": idx, "ticker": m["ticker"],
                                           "series": me["series_ticker"], "settle": _settle(m)})
        for m in parts["totals"]:
            x = _OVER.search(m.get("yes_sub_title") or "")
            if x:
                rec["markets"].append({"kind": "total", "line": float(x.group(1)), "ticker": m["ticker"],
                                       "series": m["event_ticker"].split("-")[0], "settle": _settle(m)})
        lines = [m["line"] for m in rec["markets"] if m["kind"] == "total"]
        maxmap = max([m["index"] for m in rec["markets"] if m["kind"] == "map"], default=0)
        rec["best_of"] = 5 if (maxmap >= 4 or (lines and max(lines) > 3)) else 3 if (lines or maxmap == 3) else None
        out.append(rec)
    return out


def build(games: List[str], days: float = 21, hours: float = 12, out: Optional[str] = None,
          workers: int = 3) -> List[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    recs = []
    for g in games:
        r = group_matches(g, since)
        log.info("%s: %d settled matches with map/total markets", g, len(r))
        recs += r

    def fill(rec):
        t1 = datetime.fromtimestamp(rec["start"], timezone.utc)
        t0 = t1 - timedelta(hours=hours)
        for m in rec["markets"]:
            try:
                m["candles"] = kalshi_candles(m["series"], m["ticker"], t0, t1)
                m["trades"] = kalshi_trades(m["ticker"], t0, t1)
            except RuntimeError as exc:
                m["candles"], m["trades"] = [], []
                log.debug("%s: %s", m["ticker"], exc)
        return rec

    with ThreadPoolExecutor(workers) as ex:
        done = list(ex.map(fill, recs))
    if out:
        with gzip.open(out, "wt") as fh:
            json.dump(done, fh)
    log.info("saved %d matches", len(done))
    return done


def load(path: str) -> List[dict]:
    with gzip.open(path, "rt") as fh:
        return json.load(fh)
