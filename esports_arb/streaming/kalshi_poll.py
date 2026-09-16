"""Kalshi fallback without an API key: batched REST top-of-book polling.

GET /markets?tickers=a,b,c,... returns yes_bid/yes_ask and their sizes for
up to ~100 markets per request, so every tracked market refreshes each
`interval` seconds with a handful of requests. Only *changed* books fire
updates. Depth is top-of-book; the hub fetches full depth on demand.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

from ..http import get_json
from .books import BookStore

log = logging.getLogger("esports_arb.stream.kpoll")
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


class KalshiPoller:
    def __init__(self, store: BookStore, on_update: Callable[[str, str], None],
                 interval: float = 1.0, chunk: int = 100,
                 on_trade: Optional[Callable] = None):
        self.store, self.on_update = store, on_update
        self.interval, self.chunk = interval, chunk
        self.tickers: Set[str] = set()
        self._last: Dict[str, Tuple] = {}
        self.msgs = 0
        self.polls = 0

    async def add(self, tickers: Iterable[str]) -> None:
        self.tickers |= set(tickers)

    def apply(self, markets, now: Optional[float] = None) -> None:
        now = now or time.time()
        for m in markets:
            t = m["ticker"]
            key = (m.get("yes_bid_dollars"), m.get("yes_bid_size_fp"),
                   m.get("yes_ask_dollars"), m.get("yes_ask_size_fp"))
            if self._last.get(t) == key:
                self.store.ks(t).ts = now   # still current: refresh freshness without an update event
                continue
            self._last[t] = key
            bid, bsz = _f(m.get("yes_bid_dollars")), _f(m.get("yes_bid_size_fp"))
            ask, asz = _f(m.get("yes_ask_dollars")), _f(m.get("yes_ask_size_fp"))
            yes = [(bid, bsz)] if 0 < bid < 1 and bsz > 0 else []
            no = [(round(1 - ask, 4), asz)] if 0 < ask < 1 and asz > 0 else []
            book = self.store.ks(t)
            if book.depth == "full" and book.ready and now - book.ts < self.interval:
                continue  # a fresher full-depth fetch already covers this
            book.snapshot(yes, no, now, depth="top")
            self.msgs += 1
            self.on_update("kalshi", t)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            t0 = time.time()
            ticks = sorted(self.tickers)
            for i in range(0, len(ticks), self.chunk):
                part = ticks[i:i + self.chunk]
                try:
                    d = await asyncio.to_thread(get_json, f"{BASE}/markets",
                                                {"tickers": ",".join(part), "limit": len(part)})
                    self.apply(d.get("markets", []))
                except Exception as exc:
                    log.warning("kalshi poll failed: %s", exc)
            self.polls += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.05, self.interval - (time.time() - t0)))
            except asyncio.TimeoutError:
                pass


async def fetch_depth(store: BookStore, ticker: str) -> None:
    """Full-depth REST snapshot for one market (used when the poller spots an edge)."""
    d = await asyncio.to_thread(get_json, f"{BASE}/markets/{ticker}/orderbook")
    ob = d.get("orderbook_fp") or {}
    store.ks(ticker).snapshot(ob.get("yes_dollars"), ob.get("no_dollars"), time.time(), depth="full")
