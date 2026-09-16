"""Event-driven backtest of reference-price market making on Kalshi.

Time runs from `start_hours` before the match to `stop_before_min` before it
(in-play is excluded: prices jump on every round and makers get picked off).
Every `requote_s` seconds quotes are recomputed from information available
`latency_s` earlier, then held fixed while the real Kalshi trade tape plays:

* a taker SELLING YES at price p fills our bid b if b > p (we were ahead of
  them in the book) — or, if b == p, a `queue_frac` share of that print;
* a taker BUYING YES at p fills our ask a if a < p, or a share if a == p.

Positions are held to settlement using the real result, so adverse
selection shows up in P&L rather than being assumed away.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from .book import EventBook, Fill
from .quoting import QuoteParams, blend_fair, kalshi_fee, make_quotes


class Series:
    """Step function: value at t = last observation at or before t."""

    def __init__(self, rows: List[list], col: int = 1):
        rows = [r for r in rows if r[col] == r[col]]  # drop NaN
        self.t = [r[0] for r in rows]
        self.v = [r[col] for r in rows]

    def at(self, t: float) -> Optional[float]:
        i = bisect.bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 else None


@dataclass
class MatchResult:
    event: str
    game: str
    start: float
    pnl: float
    fills: int
    volume: float
    max_exposure: float
    capture: float
    drift: float
    fees: float
    capital: float


def run_match(rec: dict, p: QuoteParams, requote_s: float = 60, latency_s: float = 60) -> MatchResult:
    start = rec["start"]
    t0, t1 = start - p.start_hours * 3600, start - p.stop_before_min * 60
    pm = Series(rec["pm"])
    bids = [Series(c, 1) for c in rec["candles"]]
    asks = [Series(c, 2) for c in rec["candles"]]
    trades = [[tr for tr in m if t0 <= tr[0] < t1] for m in rec["trades"]]
    book = EventBook()
    peak_capital = 0.0
    idx = [0, 0]

    t = t0
    while t < t1:
        see = t - latency_s
        ref_a = pm.at(see)
        quotes = []
        for m in (0, 1):
            ref = None if ref_a is None else (ref_a if m == 0 else 1 - ref_a)
            kb, ka = bids[m].at(see), asks[m].at(see)
            kb = kb if kb and kb > 0 else None
            ka = ka if ka and ka < 1 else None
            fair = blend_fair(ref, kb, ka, p.ref_weight)
            bid, ask = make_quotes(fair, book.exposure(m), p, kb, ka)
            quotes.append([fair, bid, p.size if bid else 0, ask, p.size if ask else 0])

        t_end = min(t + requote_s, t1)
        for m in (0, 1):
            fair, bid, bid_left, ask, ask_left = quotes[m]
            tr = trades[m]
            while idx[m] < len(tr) and tr[idx[m]][0] < t_end:
                ts, price, count, taker = tr[idx[m]]
                idx[m] += 1
                if ts < t:
                    continue
                if taker == "no" and bid is not None and bid_left > 0 and bid >= price:
                    q = count if bid > price else math.floor(count * p.queue_frac)
                    q = min(q, bid_left)
                    if q > 0:
                        book.fill(Fill(ts, m, "buy", bid, q, kalshi_fee(p.maker_fee_rate, q, bid), fair))
                        bid_left -= q
                elif taker == "yes" and ask is not None and ask_left > 0 and ask <= price:
                    q = count if ask < price else math.floor(count * p.queue_frac)
                    q = min(q, ask_left)
                    if q > 0:
                        book.fill(Fill(ts, m, "sell", ask, q, kalshi_fee(p.maker_fee_rate, q, ask), fair))
                        ask_left -= q
            quotes[m][2], quotes[m][4] = bid_left, ask_left
        peak_capital = max(peak_capital, book.capital())
        t = t_end

    dec = book.decomposition(rec["a_won"])
    return MatchResult(
        event=rec["event"], game=rec["game"], start=start, pnl=book.settle(rec["a_won"]),
        fills=len(book.fills), volume=sum(f.qty for f in book.fills),
        max_exposure=book.max_abs_exposure, capture=dec["capture"], drift=dec["drift"],
        fees=dec["fees"], capital=peak_capital,
    )


def summarize(results: List[MatchResult]) -> Dict[str, float]:
    n = len(results)
    pnl = [r.pnl for r in results]
    traded = [r for r in results if r.fills]
    mean = sum(pnl) / n if n else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / (n - 1)) if n > 1 else 0.0
    vol = sum(r.volume for r in results)
    cap = max((r.capital for r in results), default=0.0)
    return {
        "matches": n,
        "matches_traded": len(traded),
        "total_pnl": sum(pnl),
        "mean_pnl": mean,
        "t_stat": mean / (sd / math.sqrt(n)) if sd > 0 else 0.0,
        "win_rate": sum(1 for r in traded if r.pnl > 0) / len(traded) if traded else 0.0,
        "contracts": vol,
        "pnl_per_contract": sum(pnl) / vol if vol else 0.0,
        "capture": sum(r.capture for r in results),
        "drift": sum(r.drift for r in results),
        "fees": sum(r.fees for r in results),
        "peak_capital_one_match": cap,
    }


def run(dataset: List[dict], p: QuoteParams, **kw) -> List[MatchResult]:
    return [run_match(rec, p, **kw) for rec in dataset]


def brier(dataset: List[dict], minutes_before: float = 5) -> Dict[str, float]:
    """Which venue's pre-match price predicts the winner better?"""
    out = {"polymarket": [], "kalshi_mid": []}
    for rec in dataset:
        t = rec["start"] - minutes_before * 60
        y = 1.0 if rec["a_won"] else 0.0
        pm = Series(rec["pm"]).at(t)
        kb, ka = Series(rec["candles"][0], 1).at(t), Series(rec["candles"][0], 2).at(t)
        if pm is None or kb is None or ka is None or not (0 < kb < ka < 1):
            continue
        out["polymarket"].append((pm - y) ** 2)
        out["kalshi_mid"].append(((kb + ka) / 2 - y) ** 2)
    n = len(out["polymarket"])
    return {"n": n, **{k: (sum(v) / n if n else float("nan")) for k, v in out.items()}}
