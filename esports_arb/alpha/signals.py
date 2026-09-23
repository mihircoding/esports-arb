"""Live signals for Kalshi map-winner / total-maps contracts, plus a forward paper test.

    python -m esports_arb alpha-signals                  # what is +EV right now
    python -m esports_arb alpha-paper --minutes 240      # log first signal per contract to JSONL
    python -m esports_arb alpha-settle                   # score the paper log (P&L + CLV)

This only *suggests* orders. Nothing here places trades.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..connectors.kalshi import BASE, parse_ticker_start
from ..fees import kalshi_fee, marginal_fee_per_unit
from ..games import GAMES
from ..http import get_json
from ..series.markets import _core, _kalshi_events, _side_team
from .backtest import Params, contract_fair
from .data import _OVER, _settle
from .model import from_match_price

log = logging.getLogger("esports_arb.alpha.signals")


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Signal:
    ts: str
    game: str
    match: str
    start: str
    ticker: str
    contract: str
    side: str            # buy "yes" or buy "no"
    price: float         # ask you would pay
    size: float          # contracts available at that price
    fair: float          # model fair value of the side you buy
    edge: float          # fair - price - taker fee (per contract)
    anchor_bid: float
    anchor_ask: float
    kind: str = "take"   # "take" | "quote"


def live_books(games: List[str]) -> List[dict]:
    out = []
    for game in games:
        g = GAMES[game]
        by_core = defaultdict(lambda: {"match": None, "markets": []})
        for e in _kalshi_events(g.kalshi_series):
            mk = [m for m in e.get("markets", []) if m.get("status") in ("active", "open")]
            if len(mk) == 2:
                by_core[_core(e["event_ticker"])[0]]["match"] = (e, mk)
        for series, kind in ((g.kalshi_map_series, "map"), (g.kalshi_total_series, "total")):
            for e in _kalshi_events(series):
                core, idx = _core(e["event_ticker"])
                for m in e.get("markets", []):
                    if m.get("status") in ("active", "open"):
                        by_core[core]["markets"].append((kind, idx, m))
        for core, parts in by_core.items():
            if not parts["match"] or not parts["markets"]:
                continue
            e, mk = parts["match"]
            a, b = mk[0].get("yes_sub_title") or "", mk[1].get("yes_sub_title") or ""
            start = parse_ticker_start(e["event_ticker"])
            contracts = []
            for kind, idx, m in parts["markets"]:
                if kind == "map":
                    t = _side_team(m.get("yes_sub_title") or "", a, b)
                    if t:
                        contracts.append({"kind": "map", "team": t, "index": idx, "m": m,
                                          "label": f"{m.get('yes_sub_title')} wins map {idx}"})
                else:
                    x = _OVER.search(m.get("yes_sub_title") or "")
                    if x:
                        contracts.append({"kind": "total", "line": float(x.group(1)), "m": m,
                                          "label": f"over {x.group(1)} maps"})
            lines = [c["line"] for c in contracts if c["kind"] == "total"]
            maxmap = max([c["index"] for c in contracts if c["kind"] == "map"], default=0)
            bo = 5 if (maxmap >= 4 or (lines and max(lines) > 3)) else 3
            out.append({"game": game, "core": core, "title": f"{a} vs {b}", "start": start, "best_of": bo,
                        "match": mk, "contracts": contracts})
    return out


def anchor(mk) -> Optional[tuple]:
    a, b = mk
    bid, ask = _f(a.get("yes_bid_dollars")), _f(a.get("yes_ask_dollars"))
    b_bid, b_ask = _f(b.get("yes_bid_dollars")), _f(b.get("yes_ask_dollars"))
    bid = max(bid if 0 < bid < 1 else 0, (1 - b_ask) if 0 < b_ask < 1 else 0)
    ask = min(ask if 0 < ask < 1 else 1, (1 - b_bid) if 0 < b_bid < 1 else 1)
    return (bid, ask) if 0 < bid < ask < 1 else None


def signals(games: List[str], p: Params, include_quotes: bool = False) -> List[Signal]:
    now = datetime.now(timezone.utc)
    out = []
    for g in live_books(games):
        start = g["start"]
        if start is None:
            continue
        mins = (start - now).total_seconds() / 60
        if not (p.stop_min < mins < p.start_hours * 60):
            continue
        anc = anchor(g["match"])
        if not anc or anc[1] - anc[0] > p.max_anchor_spread:
            continue
        sp = from_match_price(round((anc[0] + anc[1]) / 2, 3), p.rho, g["best_of"])
        if sp is None:
            continue
        for c in g["contracts"]:
            if c["kind"] == "map" and c["index"] > p.max_map_index:
                continue
            fair = contract_fair(c, sp, p.unplayed)
            if fair is None:
                continue
            m = c["m"]
            ask, asz = _f(m.get("yes_ask_dollars")), _f(m.get("yes_ask_size_fp"))
            bid, bsz = _f(m.get("yes_bid_dollars")), _f(m.get("yes_bid_size_fp"))
            common = dict(ts=now.isoformat(timespec="seconds"), game=g["game"], match=g["title"],
                          start=start.isoformat(), ticker=m["ticker"], contract=c["label"],
                          anchor_bid=anc[0], anchor_ask=anc[1])
            if 0 < ask < 1 and asz >= 1:
                edge = fair - ask - marginal_fee_per_unit("kalshi", ask)
                if edge > p.theta:
                    out.append(Signal(side="yes", price=ask, size=asz, fair=round(fair, 4),
                                      edge=round(edge, 4), **common))
            if 0 < bid < 1 and bsz >= 1:
                no_ask = round(1 - bid, 4)
                edge = (1 - fair) - no_ask - marginal_fee_per_unit("kalshi", no_ask)
                if edge > p.theta:
                    out.append(Signal(side="no", price=no_ask, size=bsz, fair=round(1 - fair, 4),
                                      edge=round(edge, 4), **common))
            if include_quotes and 0.03 < fair < 0.97:
                qb = round(max(0.01, fair - p.half_spread), 2)
                qa = round(min(0.99, fair + p.half_spread), 2)
                out.append(Signal(side=f"quote bid {qb:.2f} / ask {qa:.2f}", price=qb, size=p.size,
                                  fair=round(fair, 4), edge=round(p.half_spread, 4), kind="quote", **common))
    out.sort(key=lambda s: -s.edge)
    return out


def format_signals(sigs: List[Signal]) -> str:
    takes = [s for s in sigs if s.kind == "take"]
    lines = [f"{len(takes)} +EV taker signals (edge = model fair - ask - Kalshi fee, per $1 contract)"]
    for s in takes:
        lines.append(f"  {s.edge * 100:+5.1f}c  BUY {s.side.upper():<3} {s.contract:<38} @ {s.price:.2f} "
                     f"(size {s.size:g}, fair {s.fair:.3f}) | {s.game} {s.match} | start {s.start[5:16]}Z "
                     f"| match {s.anchor_bid:.2f}/{s.anchor_ask:.2f} | {s.ticker}")
    quotes = [s for s in sigs if s.kind == "quote"]
    if quotes:
        lines.append(f"\n{len(quotes)} suggested resting quotes (post-only):")
        for s in quotes:
            lines.append(f"  {s.contract:<38} {s.side} (fair {s.fair:.3f}) | {s.game} {s.match} | {s.ticker}")
    return "\n".join(lines)


# ---------------------------------------------------------------- forward paper test
def paper(games: List[str], p: Params, minutes: float, every_s: float, path: str) -> int:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    seen = set()
    if os.path.exists(path):
        for line in open(path):
            r = json.loads(line)
            seen.add((r["ticker"], r["side"]))
    end, logged = time.time() + minutes * 60, 0
    while time.time() < end:
        try:
            sigs = [s for s in signals(games, p) if s.kind == "take"]
        except Exception as exc:
            log.warning("signal pass failed: %s", exc)
            sigs = []
        new = [s for s in sigs if (s.ticker, s.side) not in seen]
        with open(path, "a") as fh:
            for s in new:
                seen.add((s.ticker, s.side))
                fh.write(json.dumps(asdict(s)) + "\n")
        logged += len(new)
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {len(sigs)} signals, {len(new)} new, {logged} logged",
              flush=True)
        time.sleep(max(0, min(every_s, end - time.time())))
    return logged


def settle(path: str) -> str:
    rows = [json.loads(l) for l in open(path)] if os.path.exists(path) else []
    if not rows:
        return "no paper signals logged yet"
    done, pending, pnl, clv = [], 0, 0.0, []
    for r in rows:
        try:
            m = get_json(f"{BASE}/markets/{r['ticker']}", pause=0.05)["market"]
        except RuntimeError:
            pending += 1
            continue
        val = _settle(m) if m.get("status") in ("finalized", "settled") or m.get("result") else None
        # closing line: last quoted mid before the market closed (or now, if still open)
        b, a = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
        if val is None:
            pending += 1
            if 0 < b < a < 1:
                mid = (b + a) / 2
                clv.append((mid if r["side"] == "yes" else 1 - mid) - r["price"])
            continue
        payoff = val if r["side"] == "yes" else 1 - val
        p_ = payoff - r["price"] - kalshi_fee(1, r["price"])
        pnl += p_
        done.append(p_)
    lines = [f"{len(rows)} paper signals: {len(done)} settled, {pending} open"]
    if done:
        mean = pnl / len(done)
        sd = (sum((x - mean) ** 2 for x in done) / max(len(done) - 1, 1)) ** 0.5
        t = mean / (sd / len(done) ** 0.5) if sd > 0 else 0
        lines.append(f"settled P&L (1 contract each): ${pnl:+.2f} | {mean * 100:+.2f}c per contract | "
                     f"win {sum(x > 0 for x in done) / len(done):.0%} | t={t:.2f}")
    if clv:
        lines.append(f"open signals, current CLV vs entry: {sum(clv) / len(clv) * 100:+.2f}c "
                     f"({sum(c > 0 for c in clv) / len(clv):.0%} positive)")
    return "\n".join(lines)
