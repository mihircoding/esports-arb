"""Paper / live market-making loop.

    python -m esports_arb mm-paper -g cs2,lol --minutes 60          # no account needed
    python -m esports_arb mm-live  -g cs2 --max-exposure 20 --confirm-live   # real orders

Each cycle:
  1. discover Kalshi events that are also on Polymarket (every few minutes)
  2. fair value = Polymarket book mid (bid_A = 1 - ask_B), blended with Kalshi mid
  3. desired quotes from `quoting.make_quotes` (same code the backtest uses)
  4. reconcile: cancel stale orders, post new post-only orders
  5. pull fills, update the event book, enforce risk limits

Safety rails: post-only orders only, per-event exposure cap, global exposure
cap in dollars, max-loss stop (marked to fair value), no quoting inside
`stop_before_min` of the start, a STOP file kill switch, and every resting
order is cancelled on exit.
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..connectors import KalshiConnector, PolymarketConnector
from ..http import get_json
from ..matcher import cluster_matches
from .book import EventBook, Fill
from .quoting import QuoteParams, blend_fair, kalshi_fee, make_quotes

log = logging.getLogger("esports_arb.mm.live")
KBASE = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass
class Order:
    oid: str
    ticker: str
    side: str      # bid | ask
    price: float
    size: float
    left: float
    ts: float


@dataclass
class LiveEvent:
    event: str
    title: str
    start: datetime
    tickers: List[str]             # [team A market, team B market]
    pm_tokens: List[str]           # Polymarket tokens paying on [A, B]
    book: EventBook = field(default_factory=EventBook)
    fair_a: Optional[float] = None


# ---------------------------------------------------------------- brokers
class PaperBroker:
    """Simulated Kalshi account. Fills come from the *real* public trade tape
    using the same rule as the backtest (trade-through, or queue share at-touch)."""

    def __init__(self, queue_frac: float = 0.25, maker_fee_rate: float = 0.0):
        self.orders: Dict[str, Order] = {}
        self.queue_frac = queue_frac
        self.fee_rate = maker_fee_rate
        self._n = 0
        self._last_trade_ts: Dict[str, float] = {}
        self.pending_fills: List[Tuple[str, str, float, float, float, float]] = []

    def place(self, ticker, side, price, size) -> str:
        self._n += 1
        oid = f"paper-{self._n}"
        self.orders[oid] = Order(oid, ticker, side, price, size, size, time.time())
        self._last_trade_ts.setdefault(ticker, time.time())
        return oid

    def cancel(self, oid) -> None:
        self.orders.pop(oid, None)

    def poll_fills(self) -> List[Tuple[str, str, float, float, float, float]]:
        """-> [(ticker, side 'buy'|'sell', price, qty, fee, ts)]"""
        out = []
        for ticker in {o.ticker for o in self.orders.values()}:
            since = self._last_trade_ts.get(ticker, time.time())
            try:
                d = get_json(f"{KBASE}/markets/trades", {"ticker": ticker, "limit": 200,
                                                         "min_ts": int(since)})
            except RuntimeError:
                continue
            trades = []
            for t in d.get("trades", []):
                ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
                if ts > since:
                    trades.append((ts, float(t["yes_price_dollars"]), float(t["count_fp"]), t["taker_side"]))
            trades.sort()
            if trades:
                self._last_trade_ts[ticker] = trades[-1][0]
            for ts, price, count, taker in trades:
                for o in list(self.orders.values()):
                    if o.ticker != ticker or o.left <= 0 or ts < o.ts:
                        continue
                    hit = (taker == "no" and o.side == "bid" and o.price >= price) or \
                          (taker == "yes" and o.side == "ask" and o.price <= price)
                    if not hit:
                        continue
                    through = o.price > price if o.side == "bid" else o.price < price
                    q = min(o.left, count if through else math.floor(count * self.queue_frac))
                    if q <= 0:
                        continue
                    o.left -= q
                    fee = kalshi_fee(self.fee_rate, q, o.price)
                    out.append((ticker, "buy" if o.side == "bid" else "sell", o.price, q, fee, ts))
                    if o.left <= 0:
                        self.orders.pop(o.oid, None)
        return out

    def cancel_all(self) -> None:
        self.orders.clear()


class KalshiBroker:
    """Real orders through the authenticated API."""

    def __init__(self, client):
        self.c = client
        self.orders: Dict[str, Order] = {}
        self._fill_ts = time.time()
        self._seen = set()

    def place(self, ticker, side, price, size) -> Optional[str]:
        try:
            o = self.c.place_limit(ticker, side, price, int(size), post_only=True)
        except RuntimeError as exc:
            log.warning("place failed: %s", exc)
            return None
        oid = o.get("order_id")
        if oid:
            self.orders[oid] = Order(oid, ticker, side, price, size, size, time.time())
        return oid

    def cancel(self, oid) -> None:
        try:
            self.c.cancel(oid)
        except RuntimeError as exc:
            log.warning("cancel %s failed: %s", oid, exc)
        self.orders.pop(oid, None)

    def poll_fills(self):
        out = []
        for f in self.c.fills(min_ts=self._fill_ts - 5):
            fid = f.get("fill_id") or f.get("trade_id")
            if fid in self._seen:
                continue
            self._seen.add(fid)
            ticker = f["ticker"]
            yes_px = float(f.get("yes_price_dollars") or f.get("yes_price", 0) / 100)
            qty = float(f.get("count_fp") or f.get("count", 0))
            # buying YES or selling NO = long YES
            long_yes = (f.get("side") == "yes") == (f.get("action") == "buy")
            fee = float(f.get("fee_cost") or 0)
            out.append((ticker, "buy" if long_yes else "sell", yes_px, qty, fee, time.time()))
            oid = f.get("order_id")
            if oid in self.orders:
                self.orders[oid].left -= qty
                if self.orders[oid].left <= 0:
                    self.orders.pop(oid, None)
        self._fill_ts = time.time()
        return out

    def cancel_all(self) -> None:
        for oid in list(self.orders):
            self.cancel(oid)


# ---------------------------------------------------------------- engine
def pm_mid(books: Dict[str, list], tok_a: str, tok_b: str) -> Optional[float]:
    ask_a = books.get(tok_a, [])
    ask_b = books.get(tok_b, [])
    if not ask_a or not ask_b:
        return None
    best_ask_a = ask_a[0][0]
    best_bid_a = 1 - ask_b[0][0]
    if not (0 < best_bid_a < best_ask_a < 1) or best_ask_a - best_bid_a > 0.10:
        return None  # too wide to trust as a fair value
    return (best_ask_a + best_bid_a) / 2


class Engine:
    def __init__(self, broker, params: QuoteParams, games: List[str], max_total_exposure: float = 100.0,
                 max_loss: float = 25.0, interval: float = 20.0, rediscover_s: float = 300.0,
                 log_path: Optional[str] = None, stop_file: str = "STOP"):
        self.b, self.p, self.games = broker, params, games
        self.max_total, self.max_loss = max_total_exposure, max_loss
        self.interval, self.rediscover_s = interval, rediscover_s
        self.events: Dict[str, LiveEvent] = {}
        self.by_ticker: Dict[str, Tuple[LiveEvent, int]] = {}
        self.working: Dict[Tuple[str, str], str] = {}   # (ticker, side) -> order id
        self.log_path, self.stop_file = log_path, stop_file
        self._last_discover = 0.0
        self.halted = False

    # discovery -----------------------------------------------------------
    def discover(self) -> None:
        kal, pm = KalshiConnector(depth=False), PolymarketConnector(lookback_hours=0)
        matches = []
        for g in self.games:
            try:
                matches += kal.fetch(g) + pm.fetch(g)
            except Exception as exc:
                log.warning("discover %s: %s", g, exc)
        now = datetime.now(timezone.utc)
        for cl in cluster_matches(matches, threshold=0.9):
            if len(cl.venues) < 2 or min(cl.scores) < 0.9:
                continue
            k = next(m for m in cl.members if m.venue == "kalshi")
            p = next(m for m in cl.members if m.venue == "polymarket")
            if k.start is None or k.start <= now:
                continue
            ev = k.legs_a[0].instrument.rsplit("-", 1)[0]
            if ev in self.events:
                continue
            tickers = [k.legs_a[0].instrument, k.legs_b[0].instrument]
            le = LiveEvent(ev, f"{k.team_a} vs {k.team_b}", k.start, tickers,
                           [p.legs_a[0].instrument, p.legs_b[0].instrument])
            self.events[ev] = le
            for i, t in enumerate(tickers):
                self.by_ticker[t] = (le, i)
        self._last_discover = time.time()
        log.info("tracking %d events on both venues", len(self.events))

    # one cycle -----------------------------------------------------------
    def cycle(self) -> None:
        if time.time() - self._last_discover > self.rediscover_s:
            self.discover()
        now = datetime.now(timezone.utc)
        active = [e for e in self.events.values()
                  if 0 < (e.start - now).total_seconds() / 60 - self.p.stop_before_min
                  and (e.start - now).total_seconds() / 3600 < self.p.start_hours]

        # fair values from one batched Polymarket book request
        from ..http import post_json
        from ..connectors.polymarket import CLOB, parse_book
        toks = [t for e in active for t in e.pm_tokens]
        books = {}
        for i in range(0, len(toks), 40):
            try:
                for b in post_json(f"{CLOB}/books", [{"token_id": t} for t in toks[i:i + 40]]):
                    books[b["asset_id"]] = parse_book(b)
            except RuntimeError:
                pass

        desired: Dict[Tuple[str, str], Tuple[float, float]] = {}
        for e in active:
            ref_a = pm_mid(books, *e.pm_tokens)
            for m, ticker in enumerate(e.tickers):
                try:
                    ob = get_json(f"{KBASE}/markets/{ticker}/orderbook", pause=0.05).get("orderbook_fp", {})
                except RuntimeError:
                    continue
                yes_bids = [float(p) for p, s in ob.get("yes_dollars") or [] if float(s) > 0]
                no_bids = [float(p) for p, s in ob.get("no_dollars") or [] if float(s) > 0]
                kb = max(yes_bids) if yes_bids else None
                ka = round(1 - max(no_bids), 4) if no_bids else None
                # don't count our own resting orders as the market
                ref = None if ref_a is None else (ref_a if m == 0 else 1 - ref_a)
                fair = blend_fair(ref, kb, ka, self.p.ref_weight)
                if m == 0:
                    e.fair_a = fair
                bid, ask = make_quotes(fair, e.book.exposure(m), self.p, kb, ka)
                if self._total_exposure() >= self.max_total:
                    # only allow risk-reducing quotes
                    if e.book.exposure(m) >= 0:
                        bid = None
                    if e.book.exposure(m) <= 0:
                        ask = None
                if bid is not None:
                    desired[(ticker, "bid")] = (bid, self.p.size)
                if ask is not None:
                    desired[(ticker, "ask")] = (ask, self.p.size)

        self._reconcile(desired)
        self._apply_fills()
        self._risk_checks()

    def _reconcile(self, desired) -> None:
        live = {k: self.b.orders.get(oid) for k, oid in self.working.items()}
        for key, order in live.items():
            want = desired.get(key)
            if order is None or want is None or abs(order.price - want[0]) > 1e-9:
                if order is not None:
                    self.b.cancel(order.oid)
                self.working.pop(key, None)
        for key, (price, size) in desired.items():
            if key in self.working:
                continue
            oid = self.b.place(key[0], key[1], price, size)
            if oid:
                self.working[key] = oid
                self._log({"type": "quote", "ticker": key[0], "side": key[1], "price": price, "size": size})

    def _apply_fills(self) -> None:
        for ticker, side, price, qty, fee, ts in self.b.poll_fills():
            if ticker not in self.by_ticker:
                continue
            ev, m = self.by_ticker[ticker]
            fair = ev.fair_a if m == 0 else (1 - ev.fair_a if ev.fair_a is not None else price)
            ev.book.fill(Fill(ts, m, side, price, qty, fee, fair if fair is not None else price))
            log.info("FILL %s %s %.0f @ %.2f (fair %.3f) exposure=%+.0f", ticker, side, qty, price,
                     fair or float("nan"), ev.book.exposure())
            self._log({"type": "fill", "ticker": ticker, "side": side, "price": price, "qty": qty,
                       "fee": fee, "fair": fair, "exposure": ev.book.exposure()})

    def _total_exposure(self) -> float:
        return sum(abs(e.book.exposure()) for e in self.events.values())

    def marked_pnl(self) -> float:
        return sum(e.book.mark(e.fair_a) for e in self.events.values()
                   if e.fair_a is not None and e.book.fills)

    def _risk_checks(self) -> None:
        pnl = self.marked_pnl()
        if pnl < -self.max_loss:
            log.error("max loss hit (%.2f) — cancelling everything and halting", pnl)
            self.halted = True
        if os.path.exists(self.stop_file):
            log.error("STOP file found — halting")
            self.halted = True

    def _log(self, row: dict) -> None:
        if self.log_path:
            row["ts"] = datetime.now(timezone.utc).isoformat()
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(row) + "\n")

    def run(self, minutes: float) -> dict:
        end = time.time() + minutes * 60
        stop = {"flag": False}
        signal.signal(signal.SIGINT, lambda *a: stop.update(flag=True))
        signal.signal(signal.SIGTERM, lambda *a: stop.update(flag=True))
        self.discover()
        try:
            while time.time() < end and not self.halted and not stop["flag"]:
                t = time.time()
                try:
                    self.cycle()
                except Exception as exc:  # never leave orders behind because of a bug
                    log.exception("cycle error: %s", exc)
                    self.halted = True
                    break
                log.info("cycle: %d working orders | exposure %.0f | marked P&L $%.2f",
                         len(self.working), self._total_exposure(), self.marked_pnl())
                time.sleep(max(0.0, self.interval - (time.time() - t)))
        finally:
            self.b.cancel_all()
            self.working.clear()
            self._apply_fills()
        summary = {
            "events_tracked": len(self.events),
            "fills": sum(len(e.book.fills) for e in self.events.values()),
            "contracts": sum(f.qty for e in self.events.values() for f in e.book.fills),
            "marked_pnl": self.marked_pnl(),
            "spread_capture_vs_fair": sum(
                (1 if f.side == "buy" else -1) * f.qty * (f.fair - f.price)
                for e in self.events.values() for f in e.book.fills),
            "open_positions": {e.event: e.book.pos for e in self.events.values() if any(e.book.pos)},
        }
        self._log({"type": "summary", **summary})
        return summary
