"""Backtests for pricing Kalshi map-winner / total-maps contracts off the match price.

Anchor: the match market's best bid/ask each minute, combining both team
markets (bid_A = max(bid_A, 1 - ask_B), ask_A = min(ask_A, 1 - bid_B)). If the
anchor spread is wider than `max_anchor_spread`, nothing is priced.

Fair value: `model.from_match_price(anchor mid, rho, best_of)`.

TAKER  buy YES at the real best ask (or NO at 1 - best bid) when
       fair - price - taker fee > theta. First signal per contract/side per
       match only. The anchor is read one minute *before* the ask it trades against.
MAKER  rest bid = fair - h and ask = fair + h (post-only against the real book);
       fills come from the real trade tape (trade-through fills fully, at-touch
       gets `queue_frac`). No maker fee on Kalshi esports series.

Everything settles at Kalshi's actual settlement value (including scalar
'fair value' settlements for unplayed/cancelled maps).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..fees import kalshi_fee, marginal_fee_per_unit
from .model import from_match_price


class Step:
    def __init__(self, rows, col):
        rows = [r for r in rows if r[col] == r[col] and r[col] is not None]
        self.t = [r[0] for r in rows]
        self.v = [r[col] for r in rows]

    def at(self, t):
        i = bisect.bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 else None


def _ok(x):
    return x is not None and 0 < x < 1


def anchor_series(rec: dict):
    ms = {m["team"]: m for m in rec["markets"] if m["kind"] == "match"}
    a, b = ms.get("A"), ms.get("B")
    if not a:
        return None
    ba, aa = Step(a.get("candles", []), 1), Step(a.get("candles", []), 2)
    bb, ab = (Step(b.get("candles", []), 1), Step(b.get("candles", []), 2)) if b else (None, None)

    def at(t):
        bid, ask = ba.at(t), aa.at(t)
        bid = bid if _ok(bid) else None
        ask = ask if _ok(ask) else None
        if bb is not None:
            b2, a2 = bb.at(t), ab.at(t)
            if _ok(a2):
                bid = max(bid or 0, 1 - a2)
            if _ok(b2):
                ask = min(ask or 1, 1 - b2)
        if bid is None or ask is None or not (0 < bid < ask < 1):
            return None
        return bid, ask
    return at


def contract_fair(m: dict, sp, unplayed: float) -> Optional[float]:
    if m["kind"] == "map":
        i = m["index"]
        if i not in sp.maps:
            return None
        fa = sp.maps[i] + (1 - sp.played[i]) * unplayed
        pb = sp.played[i] - sp.maps[i] + (1 - sp.played[i]) * unplayed
        return fa if m["team"] == "A" else pb
    if m["kind"] == "total":
        return sp.over.get(m["line"])
    return None


@dataclass
class Params:
    rho: float = 0.1
    theta: float = 0.03            # taker: required edge after fee
    half_spread: float = 0.04      # maker
    size: int = 5                  # maker quote size
    max_pos: int = 25              # maker: |contracts| per contract
    queue_frac: float = 0.25
    max_anchor_spread: float = 0.06
    start_hours: float = 12.0
    stop_min: float = 5.0          # stop this many minutes before scheduled start
    unplayed: float = 0.5          # assumed settlement of an unplayed map
    max_map_index: int = 2         # only maps that are always played (BO3: 1-2) by default
    step_s: int = 60


@dataclass
class Trade:
    event: str
    game: str
    ticker: str
    kind: str
    side: str                      # "yes" | "no"
    ts: float
    price: float
    qty: float
    fee: float
    fair: float
    settle: float
    close_mid: Optional[float] = None

    @property
    def pnl(self) -> float:
        payoff = self.settle if self.side == "yes" else 1 - self.settle
        return self.qty * (payoff - self.price) - self.fee

    @property
    def clv(self) -> Optional[float]:
        if self.close_mid is None:
            return None
        close = self.close_mid if self.side == "yes" else 1 - self.close_mid
        return close - self.price


def _tradeable(rec: dict, p: Params) -> List[dict]:
    out = []
    for m in rec["markets"]:
        if m.get("settle") is None or not m.get("candles"):
            continue
        if m["kind"] == "map" and m["index"] <= p.max_map_index:
            out.append(m)
        elif m["kind"] == "total":
            out.append(m)
    return out


def run_taker(rec: dict, p: Params) -> List[Trade]:
    bo = rec.get("best_of")
    anchor = anchor_series(rec)
    if not bo or anchor is None:
        return []
    t0, t1 = rec["start"] - p.start_hours * 3600, rec["start"] - p.stop_min * 60
    trades, done = [], set()
    for m in _tradeable(rec, p):
        bid_s, ask_s = Step(m["candles"], 1), Step(m["candles"], 2)
        cb, ca = bid_s.at(t1), ask_s.at(t1)
        close_mid = (cb + ca) / 2 if _ok(cb) and _ok(ca) and cb < ca else None
        t = t0 + p.step_s
        while t < t1:
            anc = anchor(t - p.step_s)
            if anc and anc[1] - anc[0] <= p.max_anchor_spread:
                sp = from_match_price(round((anc[0] + anc[1]) / 2, 3), p.rho, bo)
                fair = contract_fair(m, sp, p.unplayed) if sp else None
                if fair is not None:
                    ask, bid = ask_s.at(t), bid_s.at(t)
                    if "yes" not in [s for (tk, s) in done if tk == m["ticker"]] and _ok(ask):
                        edge = fair - ask - marginal_fee_per_unit("kalshi", ask)
                        if edge > p.theta:
                            trades.append(Trade(rec["core"], rec["game"], m["ticker"], m["kind"], "yes", t, ask,
                                                1, kalshi_fee(1, ask), fair, m["settle"], close_mid))
                            done.add((m["ticker"], "yes"))
                    if "no" not in [s for (tk, s) in done if tk == m["ticker"]] and _ok(bid):
                        no_ask = round(1 - bid, 4)
                        edge = (1 - fair) - no_ask - marginal_fee_per_unit("kalshi", no_ask)
                        if edge > p.theta:
                            trades.append(Trade(rec["core"], rec["game"], m["ticker"], m["kind"], "no", t, no_ask,
                                                1, kalshi_fee(1, no_ask), fair, m["settle"], close_mid))
                            done.add((m["ticker"], "no"))
            t += p.step_s
    return trades


def run_maker(rec: dict, p: Params) -> List[Trade]:
    bo = rec.get("best_of")
    anchor = anchor_series(rec)
    if not bo or anchor is None:
        return []
    t0, t1 = rec["start"] - p.start_hours * 3600, rec["start"] - p.stop_min * 60
    fills = []
    for m in _tradeable(rec, p):
        bid_s, ask_s = Step(m["candles"], 1), Step(m["candles"], 2)
        tape = [tr for tr in m.get("trades", []) if t0 <= tr[0] < t1]
        k, pos = 0, 0.0
        t = t0
        while t < t1:
            nxt = t + p.step_s
            anc = anchor(t - p.step_s)
            qb = qa = None
            fair = None
            if anc and anc[1] - anc[0] <= p.max_anchor_spread:
                sp = from_match_price(round((anc[0] + anc[1]) / 2, 3), p.rho, bo)
                fair = contract_fair(m, sp, p.unplayed) if sp else None
            if fair is not None and 0.03 < fair < 0.97:
                mb, ma = bid_s.at(t), ask_s.at(t)
                qb = math.floor((fair - p.half_spread) * 100 + 1e-9) / 100
                qa = math.ceil((fair + p.half_spread) * 100 - 1e-9) / 100
                if _ok(ma):
                    qb = min(qb, round(ma - 0.01, 2))
                if _ok(mb):
                    qa = max(qa, round(mb + 0.01, 2))
                if pos >= p.max_pos or qb < 0.01:
                    qb = None
                if pos <= -p.max_pos or qa > 0.99:
                    qa = None
            left_b = left_a = p.size
            while k < len(tape) and tape[k][0] < nxt:
                ts, price, count, taker = tape[k]
                k += 1
                if ts < t or fair is None:
                    continue
                if taker == "no" and qb is not None and left_b > 0 and qb >= price:
                    q = min(left_b, count if qb > price else math.floor(count * p.queue_frac), p.max_pos - pos)
                    if q > 0:
                        fills.append(Trade(rec["core"], rec["game"], m["ticker"], m["kind"], "yes", ts, qb, q,
                                           0.0, fair, m["settle"]))
                        pos += q
                        left_b -= q
                elif taker == "yes" and qa is not None and left_a > 0 and qa <= price:
                    q = min(left_a, count if qa < price else math.floor(count * p.queue_frac), p.max_pos + pos)
                    if q > 0:
                        # selling YES at qa == buying NO at 1 - qa
                        fills.append(Trade(rec["core"], rec["game"], m["ticker"], m["kind"], "no", ts,
                                           round(1 - qa, 4), q, 0.0, fair, m["settle"]))
                        pos -= q
                        left_a -= q
            t = nxt
    return fills


def summarize(trades: List[Trade], n_matches: int) -> Dict[str, float]:
    pnl = [t.pnl for t in trades]
    per_match: Dict[str, float] = {}
    for t in trades:
        per_match[t.event] = per_match.get(t.event, 0.0) + t.pnl
    vals = list(per_match.values()) + [0.0] * max(0, n_matches - len(per_match))
    mean = sum(vals) / len(vals) if vals else 0.0
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else 0.0
    qty = sum(t.qty for t in trades)
    clvs = [t.clv for t in trades if t.clv is not None]
    return {
        "trades": len(trades), "contracts": qty, "matches_traded": len(per_match),
        "pnl": sum(pnl), "pnl_per_contract": sum(pnl) / qty if qty else 0.0,
        "t_stat_by_match": mean / (sd / math.sqrt(len(vals))) if sd > 0 else 0.0,
        "win_rate": sum(1 for x in pnl if x > 0) / len(pnl) if pnl else 0.0,
        "avg_clv": sum(clvs) / len(clvs) if clvs else float("nan"),
        "clv_positive": sum(1 for c in clvs if c > 0) / len(clvs) if clvs else float("nan"),
    }
