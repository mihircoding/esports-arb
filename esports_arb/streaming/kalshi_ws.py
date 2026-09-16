"""Kalshi WebSocket client (requires an API key, even for public channels).

wss://external-api-ws.kalshi.com/trade-api/ws/v2
  auth headers: sign(timestamp + "GET" + "/trade-api/ws/v2")
  -> {"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta","trade"],"market_tickers":[...]}}
  <- orderbook_snapshot {msg: {market_ticker, yes_dollars_fp, no_dollars_fp}}
  <- orderbook_delta    {msg: {market_ticker, price_dollars, delta_fp, side}}   seq per sid
  <- trade              {msg: {market_ticker, yes_price_dollars, count_fp, taker_side, ts_ms}}

A gap in `seq` means a lost delta -> the book is untrustworthy -> resubscribe
(which delivers fresh snapshots).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Dict, Iterable, Optional, Set

from .books import BookStore
from .polymarket_ws import ssl_context

log = logging.getLogger("esports_arb.stream.kalshi")
URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


class SeqGap(Exception):
    pass


class KalshiStream:
    def __init__(self, store: BookStore, on_update: Callable[[str, str], None],
                 on_trade: Optional[Callable[[str, float, float, str, float], None]] = None,
                 signer=None, url: str = URL):
        self.store, self.on_update, self.on_trade = store, on_update, on_trade
        self.signer, self.url = signer, url
        self.tickers: Set[str] = set()
        self._seq: Dict[int, int] = {}
        self._ws = None
        self._next_id = 1
        self.msgs = 0
        self.gaps = 0
        self._resubscribe = asyncio.Event()

    # -- message handling (pure, unit-testable) ---------------------------
    def handle(self, raw: str, now: Optional[float] = None) -> None:
        now = now or time.time()
        m = json.loads(raw)
        kind = m.get("type")
        sid, seq = m.get("sid"), m.get("seq")
        if kind in ("orderbook_snapshot", "orderbook_delta") and sid is not None and seq is not None:
            last = self._seq.get(sid)
            if kind == "orderbook_delta" and last is not None and seq != last + 1:
                self.gaps += 1
                self._seq.pop(sid, None)
                raise SeqGap(f"sid {sid}: expected {last + 1}, got {seq}")
            self._seq[sid] = seq
        msg = m.get("msg") or {}
        if kind == "orderbook_snapshot":
            t = msg["market_ticker"]
            self.store.ks(t).snapshot(msg.get("yes_dollars_fp"), msg.get("no_dollars_fp"), now)
            self.msgs += 1
            self.on_update("kalshi", t)
        elif kind == "orderbook_delta":
            t = msg["market_ticker"]
            book = self.store.ks(t)
            if not book.ready:
                return
            book.apply_delta(msg["side"], msg["price_dollars"], msg["delta_fp"], now)
            self.msgs += 1
            self.on_update("kalshi", t)
        elif kind == "trade" and self.on_trade:
            ts = msg.get("ts_ms", now * 1000) / 1000
            self.on_trade(msg["market_ticker"], float(msg["yes_price_dollars"]), float(msg["count_fp"]),
                          msg["taker_side"], ts)
        elif kind == "error":
            log.warning("kalshi ws error: %s", msg)

    def subscribe_cmd(self) -> str:
        cmd = {"id": self._next_id, "cmd": "subscribe",
               "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": sorted(self.tickers)}}
        self._next_id += 1
        return json.dumps(cmd)

    async def add(self, tickers: Iterable[str]) -> None:
        new = set(tickers) - self.tickers
        if new:
            self.tickers |= new
            self._resubscribe.set()   # simplest correct option: reconnect with the full list

    async def run(self, stop: asyncio.Event) -> None:
        import websockets

        backoff = 1.0
        while not stop.is_set():
            self._resubscribe.clear()
            try:
                headers = self.signer._headers("GET", self.url)
                headers.pop("Content-Type", None)
                async with websockets.connect(self.url, ssl=ssl_context() if self.url.startswith("wss") else None, additional_headers=headers,
                                              max_size=2 ** 24, open_timeout=15) as ws:
                    self._ws = ws
                    self._seq.clear()
                    if self.tickers:
                        await ws.send(self.subscribe_cmd())
                    backoff = 1.0
                    log.info("kalshi ws connected (%d markets)", len(self.tickers))
                    while not stop.is_set() and not self._resubscribe.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue   # library handles ping/pong
                        self.handle(raw)
            except asyncio.CancelledError:
                raise
            except SeqGap as exc:
                log.warning("kalshi seq gap (%s) — resubscribing", exc)
                backoff = 0.2
            except Exception as exc:
                log.warning("kalshi ws dropped: %s — reconnecting in %.0fs", exc, backoff)
            finally:
                self._ws = None
                for t in self.tickers:
                    self.store.ks(t).ready = False
            if not stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
