"""Historical dataset builder for the market-making backtest.

For every settled Kalshi esports match that also existed on Polymarket we
store, over the quoting window [start - hours, start]:

* Kalshi public trade tape for both markets (price, size, taker side)
* Kalshi 1-minute top-of-book candles (yes_bid / yes_ask close)
* Polymarket 1-minute price history for the same team (the fair-value signal)
* the settlement result

    python -m esports_arb mm-data --days 14 --out data/mm/dataset.json.gz
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..connectors.kalshi import BASE as KBASE, parse_ticker_start
from ..connectors.polymarket import CLOB, GAMMA, _dt
from ..games import GAMES
from ..http import get_json
from ..matcher import cluster_matches
from ..models import Match

log = logging.getLogger("esports_arb.mm")


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def kalshi_settled(game: str, since: datetime, max_pages: int = 10) -> List[dict]:
    series = GAMES[game].kalshi_series
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"series_ticker": series, "status": "settled", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = get_json(f"{KBASE}/events", params)
        evs = d.get("events", [])
        for e in evs:
            start = parse_ticker_start(e["event_ticker"])
            mk = e.get("markets", [])
            if start is None or start < since or len(mk) != 2:
                continue
            if sorted(m.get("result") for m in mk) != ["no", "yes"]:
                continue  # voided / scalar / unresolved
            out.append({"event": e["event_ticker"], "series": e["series_ticker"], "start": start,
                        "markets": [{"ticker": m["ticker"], "team": m.get("yes_sub_title") or m["title"],
                                     "result": m["result"]} for m in mk]})
        cursor = d.get("cursor")
        oldest = min((parse_ticker_start(e["event_ticker"]) or since for e in evs), default=since)
        if not cursor or not evs or oldest < since:
            break
    return out


def polymarket_closed(game: str, since: datetime, max_pages: int = 10) -> List[dict]:
    sid = GAMES[game].polymarket_series
    out = []
    for page in range(max_pages):
        batch = get_json(f"{GAMMA}/events", {"series_id": sid, "closed": "true", "limit": 100,
                                             "offset": 100 * page, "order": "startDate", "ascending": "false"})
        if not batch:
            break
        for e in batch:
            start = _dt(e.get("startTime"))
            if start is None or start < since:
                continue
            mk = next((m for m in e.get("markets", []) if m.get("sportsMarketType") == "moneyline"), None)
            if not mk:
                continue
            outcomes, tokens = json.loads(mk["outcomes"]), json.loads(mk["clobTokenIds"])
            if len(outcomes) == 2:
                out.append({"title": e.get("title", ""), "start": start, "teams": outcomes, "tokens": tokens})
        if all((_dt(e.get("startTime")) or since) < since for e in batch):
            break
    return out


def kalshi_trades(ticker: str, t0: datetime, t1: datetime) -> List[list]:
    rows, cursor = [], None
    while True:
        params = {"ticker": ticker, "limit": 1000, "min_ts": int(t0.timestamp()), "max_ts": int(t1.timestamp())}
        if cursor:
            params["cursor"] = cursor
        d = get_json(f"{KBASE}/markets/trades", params, pause=0.05)
        for t in d.get("trades", []):
            ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
            rows.append([ts, _f(t["yes_price_dollars"]), _f(t["count_fp"]), t["taker_side"]])
        cursor = d.get("cursor")
        if not cursor or not d.get("trades"):
            break
    rows.sort()
    return rows


def kalshi_candles(series: str, ticker: str, t0: datetime, t1: datetime) -> List[list]:
    d = get_json(f"{KBASE}/series/{series}/markets/{ticker}/candlesticks",
                 {"start_ts": int(t0.timestamp()), "end_ts": int(t1.timestamp()), "period_interval": 1}, pause=0.05)
    rows = []
    for c in d.get("candlesticks", []):
        bid = _f((c.get("yes_bid") or {}).get("close_dollars"))
        ask = _f((c.get("yes_ask") or {}).get("close_dollars"))
        rows.append([c["end_period_ts"], bid, ask])
    return rows


def pm_history(token: str, t0: datetime, t1: datetime) -> List[list]:
    d = get_json(f"{CLOB}/prices-history", {"market": token, "startTs": int(t0.timestamp()),
                                            "endTs": int(t1.timestamp()), "fidelity": 1})
    return [[h["t"], float(h["p"])] for h in d.get("history", [])]


def build(games: List[str], days: float = 14, hours: float = 12, out: Optional[str] = None,
          max_matches: Optional[int] = None) -> List[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    dataset: List[dict] = []
    for g in games:
        g_obj = GAMES[g]
        if not (g_obj.kalshi_series and g_obj.polymarket_series):
            continue
        ks, ps = kalshi_settled(g, since), polymarket_closed(g, since)
        log.info("%s: %d kalshi settled, %d polymarket closed", g, len(ks), len(ps))
        matches = [Match("kalshi", g, k["markets"][0]["team"], k["markets"][1]["team"], k["start"], k["event"])
                   for k in ks]
        matches += [Match("polymarket", g, p["teams"][0], p["teams"][1], p["start"], p["title"]) for p in ps]
        by_title = {("kalshi", k["event"]): k for k in ks}
        by_title.update({("polymarket", p["title"] + p["start"].isoformat()): p for p in ps})
        for cl in cluster_matches(matches, threshold=0.85, window=timedelta(hours=3)):
            if len(cl.venues) != 2:
                continue
            km = next(m for m in cl.members if m.venue == "kalshi")
            pmm = next(m for m in cl.members if m.venue == "polymarket")
            k = by_title[("kalshi", km.title)]
            p = next(x for x in ps if x["title"] == pmm.title and x["start"] == pmm.start)
            # orient the Polymarket token so it pays on Kalshi's first team
            from ..normalize import similarity
            a_team = k["markets"][0]["team"]
            idx = 0 if similarity(a_team, p["teams"][0]) >= similarity(a_team, p["teams"][1]) else 1
            t1 = k["start"]
            t0 = t1 - timedelta(hours=hours)
            try:
                rec = {
                    "game": g, "event": k["event"], "series": k["series"], "start": t1.timestamp(),
                    "teams": [m["team"] for m in k["markets"]],
                    "tickers": [m["ticker"] for m in k["markets"]],
                    "a_won": k["markets"][0]["result"] == "yes",
                    "pm_title": p["title"],
                    "trades": [kalshi_trades(m["ticker"], t0, t1 + timedelta(minutes=1)) for m in k["markets"]],
                    "candles": [kalshi_candles(k["series"], m["ticker"], t0, t1) for m in k["markets"]],
                    "pm": pm_history(p["tokens"][idx], t0 - timedelta(minutes=30), t1),
                }
            except RuntimeError as exc:
                log.warning("skip %s: %s", k["event"], exc)
                continue
            if len(rec["pm"]) < 10 or not any(rec["trades"]):
                continue
            dataset.append(rec)
            log.info("  + %s (%d/%d trades, %d pm pts)", k["event"], len(rec["trades"][0]),
                     len(rec["trades"][1]), len(rec["pm"]))
            if max_matches and len(dataset) >= max_matches:
                break
    if out:
        with gzip.open(out, "wt") as fh:
            json.dump(dataset, fh)
    return dataset


def load(path: str) -> List[dict]:
    with gzip.open(path, "rt") as fh:
        return json.load(fh)
