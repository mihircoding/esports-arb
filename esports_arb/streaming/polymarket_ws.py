"""Polymarket CLOB market-channel WebSocket client.

wss://ws-subscriptions-clob.polymarket.com/ws/market
  -> {"assets_ids": [...], "type": "market"}
  <- book            full snapshot for one token
  <- price_change    [{asset_id, price, size (new total), side BUY|SELL}, ...]
  <- last_trade_price
Send the text frame "PING" every 10 s (server answers "PONG").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from typing import Callable, Iterable, Optional, Set

from .books import BookStore

log = logging.getLogger("esports_arb.stream.pm")
URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def ssl_context() -> ssl.SSLContext:
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=ca) if ca and os.path.exists(ca) else ssl.create_default_context()


class PolymarketStream:
    def __init__(self, store: BookStore, on_update: Callable[[str, str], None],
                 on_trade: Optional[Callable[[str, float, float, str, float], None]] = None,
                 url: str = URL, ping_s: float = 10.0):
        self.store, self.on_update, self.on_trade = store, on_update, on_trade
        self.url, self.ping_s = url, ping_s
        self.tokens: Set[str] = set()
        self._ws = None
        self.msgs = 0
        self.connected = asyncio.Event()

    # -- message handling (pure, unit-testable) ---------------------------
    def handle(self, raw: str, now: Optional[float] = None) -> None:
        now = now or time.time()
        if raw in ("PONG", "PING", ""):
            return
        data = json.loads(raw)
        for ev in data if isinstance(data, list) else [data]:
            kind = ev.get("event_type")
            if kind == "book":
                tok = ev["asset_id"]
                self.store.pm(tok).snapshot(ev.get("bids", []), ev.get("asks", []), now)
                self.msgs += 1
                self.on_update("polymarket", tok)
            elif kind == "price_change":
                touched = set()
                for ch in ev.get("price_changes", []):
                    tok = ch["asset_id"]
                    book = self.store.pm(tok)
                    if not book.ready:
                        continue  # wait for the snapshot
                    book.set_level(ch["side"], ch["price"], ch["size"], now)
                    touched.add(tok)
                self.msgs += 1
                for tok in touched:
                    self.on_update("polymarket", tok)
            elif kind == "last_trade_price" and self.on_trade:
                self.on_trade(ev["asset_id"], float(ev["price"]), float(ev.get("size", 0)),
                              ev.get("side", ""), now)

    # -- subscription management ------------------------------------------
    async def add(self, tokens: Iterable[str]) -> None:
        new = set(tokens) - self.tokens
        if not new:
            return
        self.tokens |= new
        if self._ws is not None:
            await self._ws.send(json.dumps({"assets_ids": sorted(new), "operation": "subscribe"}))

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(self.ping_s)
            await ws.send("PING")

    async def run(self, stop: asyncio.Event) -> None:
        import websockets

        backoff = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(self.url, ssl=ssl_context() if self.url.startswith("wss") else None, max_size=2 ** 24,
                                              open_timeout=15) as ws:
                    self._ws = ws
                    if self.tokens:
                        await ws.send(json.dumps({"assets_ids": sorted(self.tokens), "type": "market"}))
                    self.connected.set()
                    backoff = 1.0
                    log.info("polymarket ws connected (%d tokens)", len(self.tokens))
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        while not stop.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                            except asyncio.TimeoutError:
                                raise ConnectionError("no data for 30s")
                            self.handle(raw)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("polymarket ws dropped: %s — reconnecting in %.0fs", exc, backoff)
            finally:
                self._ws = None
                self.connected.clear()
                # books are stale until the next snapshot arrives
                for tok in self.tokens:
                    self.store.pm(tok).ready = False
            if not stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
