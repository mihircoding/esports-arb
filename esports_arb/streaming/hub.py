"""Event-driven arbitrage detection on streaming books.

Every book update re-evaluates only the matches that contain that
instrument. An *episode* is the lifetime of one leg-pair's positive edge:
it opens on the first update where the after-fee edge exceeds `min_edge`
and closes on the first update where it doesn't. Episodes are written as
JSON lines with millisecond timestamps, so persistence can be measured far
more finely than with minute-level REST snapshots.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..arb import evaluate, top_edge
from ..connectors import KalshiConnector, PolymarketConnector
from ..matcher import Cluster, cluster_matches
from ..scanner import leg_pairs
from .books import BookStore
from .kalshi_poll import KalshiPoller, fetch_depth
from .kalshi_ws import KalshiStream
from .polymarket_ws import PolymarketStream

log = logging.getLogger("esports_arb.stream.hub")


@dataclass
class Episode:
    key: Tuple
    game: str
    match: str
    leg_a: str
    leg_b: str
    start: float
    trigger: str
    first_edge: float
    max_edge: float
    max_profit: float = 0.0
    max_size: float = 0.0
    updates: int = 1
    max_book_age: float = 0.0
    end: Optional[float] = None
    end_trigger: str = ""
    match_start: Optional[float] = None

    def record(self, censored: bool = False) -> dict:
        return {
            "game": self.game, "match": self.match, "leg_a": self.leg_a, "leg_b": self.leg_b,
            "start": datetime.fromtimestamp(self.start, timezone.utc).isoformat(timespec="milliseconds"),
            "duration_ms": round(((self.end or time.time()) - self.start) * 1000, 1),
            "censored": censored, "open_trigger": self.trigger, "close_trigger": self.end_trigger,
            "first_edge": round(self.first_edge, 5), "max_edge": round(self.max_edge, 5),
            "max_profit": round(self.max_profit, 4), "max_size": self.max_size,
            "updates": self.updates, "max_book_age_s": round(self.max_book_age, 3),
            "match_start": (datetime.fromtimestamp(self.match_start, timezone.utc).isoformat()
                            if self.match_start else None),
            # in-play prices move every round; with a polled Kalshi book most in-play
            # "arbs" are one venue reacting before the other is refreshed
            "in_play": bool(self.match_start and self.start >= self.match_start),
        }


def _label(leg) -> str:
    return f"{leg.venue}{'/' + leg.side if leg.venue == 'kalshi' else ''}:{leg.team}"


class StreamHub:
    def __init__(self, games: List[str], min_edge: float = 0.0, out: Optional[str] = None,
                 kalshi_signer=None, poll_interval: float = 1.0, rediscover_s: float = 600.0,
                 max_kalshi_age: Optional[float] = None, report_s: float = 30.0,
                 cross_only: bool = True):
        self.games, self.min_edge, self.out = games, min_edge, out
        self.store = BookStore()
        self.pm = PolymarketStream(self.store, self.on_update)
        if kalshi_signer is not None:
            self.kalshi = KalshiStream(self.store, self.on_update, signer=kalshi_signer)
            self.kalshi_mode = "websocket"
        else:
            self.kalshi = KalshiPoller(self.store, self.on_update, interval=poll_interval)
            self.kalshi_mode = f"rest-poll {poll_interval:g}s"
        self.max_kalshi_age = max_kalshi_age or (3 * poll_interval if kalshi_signer is None else 3600)
        self.rediscover_s, self.report_s, self.cross_only = rediscover_s, report_s, cross_only
        self.clusters: Dict[str, Cluster] = {}
        self.by_instrument: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        self.open: Dict[Tuple, Episode] = {}
        self.closed = 0
        self.evals = 0
        self.eval_time = 0.0
        self._depth_req: Dict[str, float] = {}
        self._depth_q: Optional[asyncio.Queue] = None

    # -- discovery ---------------------------------------------------------
    def discover(self) -> Tuple[List[str], List[str]]:
        kal, pm = KalshiConnector(depth=False), PolymarketConnector()
        matches = []
        for g in self.games:
            for c in (kal, pm):
                try:
                    matches += c.fetch(g)
                except Exception as exc:
                    log.warning("discover %s %s: %s", c.venue, g, exc)
        tokens, tickers = [], []
        for cl in cluster_matches(matches):
            if len(cl.venues) < 2:
                continue
            ck = f"{cl.game}:{cl.anchor.team_a} vs {cl.anchor.team_b}:{cl.anchor.start}"
            if ck in self.clusters:
                continue
            self.clusters[ck] = cl
            for m in cl.members:
                for leg in m.legs_a + m.legs_b:
                    leg.asks = []   # books come from the stream from now on
                    key = ("kalshi" if leg.venue == "kalshi" else "polymarket", leg.instrument)
                    if ck not in self.by_instrument[key]:
                        self.by_instrument[key].append(ck)
                    (tickers if key[0] == "kalshi" else tokens).append(leg.instrument)
        return sorted(set(tokens)), sorted(set(tickers))

    # -- evaluation --------------------------------------------------------
    def _refresh(self, cl: Cluster, now: float) -> float:
        """Load current ladders into the cluster's legs. Returns the oldest Kalshi book age."""
        oldest = 0.0
        for m in cl.members:
            for leg in m.legs_a + m.legs_b:
                if leg.venue == "kalshi":
                    b = self.store.kalshi.get(leg.instrument)
                    if b is None or not b.ready or now - b.ts > self.max_kalshi_age:
                        leg.asks = []
                        continue
                    leg.asks = b.ask_ladder("yes" if leg.side == "YES" else "no")
                    oldest = max(oldest, now - b.ts)
                else:
                    b = self.store.poly.get(leg.instrument)
                    leg.asks = b.ask_ladder() if b is not None and b.ready else []
        return oldest

    def on_update(self, venue: str, instrument: str) -> None:
        now = time.time()
        for ck in self.by_instrument.get((venue, instrument), []):
            self.evaluate_cluster(ck, venue, now)
        self.eval_time += time.time() - now

    def evaluate_cluster(self, ck: str, trigger: str, now: float) -> None:
        cl = self.clusters[ck]
        age = self._refresh(cl, now)
        self.evals += 1
        live = set()
        for la, ma, lb, mb in leg_pairs(cl):
            if self.cross_only and ma.venue == mb.venue:
                continue
            key = (ck, la.instrument, la.side, lb.instrument, lb.side)
            e = top_edge(la, lb)
            opp = None
            if e is not None and e > self.min_edge:
                opp = evaluate(la, lb, game=cl.game, min_edge=self.min_edge)
                self._want_depth(la, lb)
            if opp is None:
                continue
            live.add(key)
            ep = self.open.get(key)
            if ep is None:
                self.open[key] = Episode(key, cl.game, f"{cl.anchor.team_a} vs {cl.anchor.team_b}",
                                         _label(la), _label(lb), now, trigger, opp.top_edge, opp.top_edge,
                                         opp.profit, opp.contracts, max_book_age=age,
                                         match_start=cl.anchor.start.timestamp() if cl.anchor.start else None)
                log.info("ARB OPEN  %s | %s + %s | edge %.2f%% size %.0f", self.open[key].match,
                         _label(la), _label(lb), opp.top_edge * 100, opp.contracts)
            else:
                ep.updates += 1
                ep.max_edge = max(ep.max_edge, opp.top_edge)
                ep.max_profit = max(ep.max_profit, opp.profit)
                ep.max_size = max(ep.max_size, opp.contracts)
                ep.max_book_age = max(ep.max_book_age, age)
        for key in [k for k in self.open if k[0] == ck and k not in live]:
            ep = self.open.pop(key)
            ep.end, ep.end_trigger = now, trigger
            self._write(ep.record())
            self.closed += 1
            log.info("ARB CLOSE %s | %s + %s | lasted %.0f ms", ep.match, ep.leg_a, ep.leg_b,
                     (ep.end - ep.start) * 1000)

    def _want_depth(self, la, lb) -> None:
        """With the poller, books are top-of-book only; fetch depth for sizing (≤ 1 per 5 s)."""
        if self._depth_q is None:
            return
        for leg in (la, lb):
            if leg.venue != "kalshi":
                continue
            b = self.store.kalshi.get(leg.instrument)
            if b and b.depth == "top" and time.time() - self._depth_req.get(leg.instrument, 0) > 5:
                self._depth_req[leg.instrument] = time.time()
                self._depth_q.put_nowait(leg.instrument)

    async def _depth_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                t = await asyncio.wait_for(self._depth_q.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            try:
                await fetch_depth(self.store, t)
                self.on_update("kalshi", t)
            except Exception as exc:
                log.debug("depth %s: %s", t, exc)

    def _write(self, rec: dict) -> None:
        if not self.out:
            return
        with open(self.out, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    # -- main loop ---------------------------------------------------------
    async def _reporter(self, stop: asyncio.Event, t0: float) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.report_s)
            except asyncio.TimeoutError:
                pass
            el = max(time.time() - t0, 1e-9)
            ready_pm = sum(b.ready for b in self.store.poly.values())
            ready_k = sum(b.ready for b in self.store.kalshi.values())
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {len(self.clusters)} matches | "
                  f"pm {self.pm.msgs / el:.1f} msg/s ({ready_pm} books) | kalshi[{self.kalshi_mode}] "
                  f"{self.kalshi.msgs / el:.1f} upd/s ({ready_k} books) | open arbs {len(self.open)} | "
                  f"closed {self.closed} | eval {1e6 * self.eval_time / max(self.evals, 1):.0f} µs",
                  flush=True)

    async def _rediscover(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.rediscover_s)
                return
            except asyncio.TimeoutError:
                pass
            toks, ticks = await asyncio.to_thread(self.discover)
            await self.pm.add(toks)
            await self.kalshi.add(ticks)

    async def run(self, minutes: float) -> dict:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        self._depth_q = asyncio.Queue()
        toks, ticks = await asyncio.to_thread(self.discover)
        await self.pm.add(toks)
        await self.kalshi.add(ticks)
        print(f"streaming {len(self.clusters)} matches: {len(toks)} Polymarket tokens (websocket), "
              f"{len(ticks)} Kalshi markets ({self.kalshi_mode})", flush=True)
        t0 = time.time()
        tasks = [asyncio.create_task(c) for c in (
            self.pm.run(stop), self.kalshi.run(stop), self._depth_worker(stop),
            self._reporter(stop, t0), self._rediscover(stop))]
        try:
            await asyncio.wait_for(stop.wait(), timeout=minutes * 60)
        except asyncio.TimeoutError:
            pass
        stop.set()
        await asyncio.sleep(0.5)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for ep in list(self.open.values()):  # still open at shutdown: right-censored
            ep.end, ep.end_trigger = time.time(), "shutdown"
            self._write(ep.record(censored=True))
        return {"matches": len(self.clusters), "episodes_closed": self.closed,
                "episodes_censored": len(self.open), "pm_msgs": self.pm.msgs,
                "kalshi_updates": self.kalshi.msgs, "evaluations": self.evals,
                "mean_eval_us": 1e6 * self.eval_time / max(self.evals, 1),
                "seconds": time.time() - t0}


def kalshi_signer_from_env():
    if os.environ.get("KALSHI_KEY_ID") and os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        from ..mm.kalshi_client import KalshiClient
        return KalshiClient()
    return None
