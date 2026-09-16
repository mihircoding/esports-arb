"""Thread-hosted streaming feed for the (synchronous) market-making engine.

Runs the WebSocket clients on a background asyncio loop and exposes
plain-Python lookups the engine calls every cycle instead of REST.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

from .books import BookStore
from .hub import kalshi_signer_from_env
from .kalshi_poll import KalshiPoller
from .kalshi_ws import KalshiStream
from .polymarket_ws import PolymarketStream


class StreamFeed:
    def __init__(self, poll_interval: float = 1.0):
        self.store = BookStore()
        self.changed: set = set()           # instruments updated since last drain
        self._lock = threading.Lock()
        self.pm = PolymarketStream(self.store, self._mark)
        signer = kalshi_signer_from_env()
        self.kalshi = (KalshiStream(self.store, self._mark, signer=signer) if signer
                       else KalshiPoller(self.store, self._mark, interval=poll_interval))
        self.max_kalshi_age = 3600 if signer else 3 * poll_interval
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._thread: Optional[threading.Thread] = None

    def _mark(self, venue: str, instrument: str) -> None:
        with self._lock:
            self.changed.add(instrument)

    def drain(self) -> set:
        with self._lock:
            out, self.changed = self.changed, set()
        return out

    # lifecycle -----------------------------------------------------------
    def start(self) -> None:
        ready = threading.Event()

        def runner():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._stop = asyncio.Event()
            ready.set()
            self._loop.run_until_complete(asyncio.gather(
                self.pm.run(self._stop), self.kalshi.run(self._stop), return_exceptions=True))

        self._thread = threading.Thread(target=runner, daemon=True, name="stream-feed")
        self._thread.start()
        ready.wait(5)

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=10)

    def subscribe(self, tokens: Iterable[str], tickers: Iterable[str]) -> None:
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.pm.add(list(tokens)), self._loop).result(10)
        asyncio.run_coroutine_threadsafe(self.kalshi.add(list(tickers)), self._loop).result(10)

    # lookups used by the engine -----------------------------------------
    def pm_asks(self, token: str) -> List[Tuple[float, float]]:
        b = self.store.poly.get(token)
        return b.ask_ladder() if b is not None and b.ready else []

    def kalshi_bbo(self, ticker: str) -> Tuple[Optional[float], Optional[float], bool]:
        b = self.store.kalshi.get(ticker)
        if b is None or not b.ready or time.time() - b.ts > self.max_kalshi_age:
            return None, None, False
        return b.best_yes_bid(), b.best_yes_ask(), True
