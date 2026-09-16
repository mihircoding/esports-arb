"""Historical dataset for the series-consistency study (Polymarket, settled BO3s).

For each settled best-of-3 with a match market and map-1/map-2 markets we store
the price of every series contract `minutes_before` the scheduled start
(1-minute price history, last point at or before that time) and how each
contract resolved.

    python -m esports_arb series-data --days 45 --out data/series/history.json.gz
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..connectors.polymarket import CLOB, GAMMA, _dt
from ..games import GAMES
from ..http import get_json

log = logging.getLogger("esports_arb.series.history")
_BO = re.compile(r"\(BO(\d)\)", re.I)
_MAPN = re.compile(r"(?:Map|Game)\s+(\d+)\s+Winner", re.I)


def _price_at(token: str, t: datetime, window_h: float = 6) -> Optional[float]:
    d = get_json(f"{CLOB}/prices-history", {"market": token, "fidelity": 1,
                                            "startTs": int((t - timedelta(hours=window_h)).timestamp()),
                                            "endTs": int(t.timestamp())})
    hist = d.get("history", [])
    return float(hist[-1]["p"]) if hist else None


def _resolved(m: dict) -> Optional[float]:
    try:
        p = [float(x) for x in json.loads(m["outcomePrices"])]
    except (KeyError, ValueError, TypeError):
        return None
    return p[0] if p and p[0] in (0.0, 0.5, 1.0) else None


def parse_event(e: dict) -> Optional[dict]:
    m_bo = _BO.search(e.get("title", ""))
    start = _dt(e.get("startTime"))
    if not m_bo or int(m_bo.group(1)) != 3 or start is None:
        return None
    mk = e.get("markets", [])
    ml = next((m for m in mk if m.get("sportsMarketType") == "moneyline"), None)
    if not ml or _resolved(ml) not in (0.0, 1.0):
        return None
    a, b = json.loads(ml["outcomes"])
    rec = {"title": e["title"], "start": start.isoformat(), "team_a": a, "team_b": b, "contracts": {}}
    rec["contracts"]["match"] = {"token": json.loads(ml["clobTokenIds"])[0], "result": _resolved(ml)}
    for m in mk:
        kind = m.get("sportsMarketType")
        outs = json.loads(m.get("outcomes") or "[]")
        toks = json.loads(m.get("clobTokenIds") or "[]")
        res = _resolved(m)
        if len(outs) != 2 or res is None:
            continue
        if kind == "child_moneyline":
            mm = _MAPN.search(m.get("question", ""))
            if not mm or int(mm.group(1)) not in (1, 2, 3):
                continue
            flip = outs[0] != a
            rec["contracts"][f"map{mm.group(1)}"] = {"token": toks[1] if flip else toks[0],
                                                    "result": (1 - res) if flip else res}
        elif kind == "map_handicap" and m.get("line") == -1.5:
            key = "h_a" if outs[0] == a else "h_b" if outs[0] == b else None
            if key:
                rec["contracts"][key] = {"token": toks[0], "result": res}
        elif kind == "totals" and m.get("line") == 2.5 and "Games Total" in m.get("question", ""):
            over_first = outs[0].lower().startswith("over")
            rec["contracts"]["over"] = {"token": toks[0] if over_first else toks[1],
                                        "result": res if over_first else 1 - res}
    c = rec["contracts"]
    if not ("map1" in c and "map2" in c):
        return None
    if c["map1"]["result"] not in (0.0, 1.0) or c["map2"]["result"] not in (0.0, 1.0):
        return None   # forfeits / cancellations
    return rec


def closed_events(game: str, since: datetime, until: Optional[datetime] = None) -> List[dict]:
    """Page through settled events one day (of end date) at a time — Gamma caps offsets."""
    until = until or datetime.now(timezone.utc)
    out, seen = [], set()
    day = since
    while day < until:
        nxt = day + timedelta(days=1)
        for page in range(20):
            batch = get_json(f"{GAMMA}/events", {
                "series_id": GAMES[game].polymarket_series, "closed": "true", "limit": 100,
                "offset": 100 * page, "end_date_min": day.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": nxt.strftime("%Y-%m-%dT%H:%M:%SZ")})
            for e in batch or []:
                if e.get("id") in seen:
                    continue
                seen.add(e.get("id"))
                r = parse_event(e)
                if r and datetime.fromisoformat(r["start"]) >= since:
                    r["game"] = game
                    out.append(r)
            if not batch or len(batch) < 100:
                break
        day = nxt
    return out


def build(games: List[str], days: float = 45, minutes_before: float = 30, out: Optional[str] = None,
          workers: int = 6) -> List[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = []
    for g in games:
        ev = closed_events(g, since)
        log.info("%s: %d settled BO3s with map markets", g, len(ev))
        events += ev

    def fill(rec):
        t = datetime.fromisoformat(rec["start"]) - timedelta(minutes=minutes_before)
        for c in rec["contracts"].values():
            try:
                c["price"] = _price_at(c["token"], t)
            except RuntimeError:
                c["price"] = None
        return rec

    with ThreadPoolExecutor(workers) as ex:
        done = list(ex.map(fill, events))
    done = [r for r in done if all(r["contracts"][k].get("price") is not None for k in ("match", "map1", "map2"))]
    if out:
        with gzip.open(out, "wt") as fh:
            json.dump(done, fh)
    log.info("saved %d events", len(done))
    return done


def load(path: str) -> List[dict]:
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- execution evidence
DATA_API = "https://data-api.polymarket.com"


def _token_index(games: List[str], since: datetime) -> dict:
    """token id -> (conditionId, [token0, token1]) for every settled market in the window."""
    idx = {}
    until = datetime.now(timezone.utc)
    for game in games:
        day = since
        while day < until:
            nxt = day + timedelta(days=1)
            for page in range(20):
                batch = get_json(f"{GAMMA}/events", {
                    "series_id": GAMES[game].polymarket_series, "closed": "true", "limit": 100,
                    "offset": 100 * page, "end_date_min": day.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_date_max": nxt.strftime("%Y-%m-%dT%H:%M:%SZ")})
                for e in batch or []:
                    for m in e.get("markets", []):
                        toks = json.loads(m.get("clobTokenIds") or "[]")
                        for t in toks:
                            idx[t] = (m.get("conditionId"), toks)
                if not batch or len(batch) < 100:
                    break
            day = nxt
    return idx


def enrich_trades(data: List[dict], games: List[str], days: float, keys=("over", "h_a", "h_b"),
                  window_h: float = 6, workers: int = 6) -> List[dict]:
    """Attach public taker trades from the `window_h` hours before start for thin markets.

    Each trade: [ts, which, side, price, size] with which = 0 for the stored token
    (the contract's YES) and 1 for its complement. `complete` is False when the
    market had more trades than one API page, i.e. pre-start trades may be missing.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days + 1)
    idx = _token_index(games, since)
    jobs = []
    for r in data:
        for k in keys:
            c = r["contracts"].get(k)
            if c and c["token"] in idx:
                jobs.append((r, c))

    def fetch(job):
        r, c = job
        cid, toks = idx[c["token"]]
        try:
            trades = get_json(f"{DATA_API}/trades", {"market": cid, "limit": 1000})
        except RuntimeError:
            return
        start = datetime.fromisoformat(r["start"]).timestamp()
        c["complete"] = len(trades) < 1000
        c["trades"] = sorted(
            [int(t["timestamp"]), 0 if t["asset"] == c["token"] else 1, t["side"], float(t["price"]),
             float(t["size"])]
            for t in trades if start - window_h * 3600 <= int(t["timestamp"]) <= start)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(fetch, jobs))
    return data
