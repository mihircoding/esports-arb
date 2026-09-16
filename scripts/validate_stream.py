"""Sanity check: do the incrementally-maintained websocket books match REST?

    python scripts/validate_stream.py --seconds 120 --games cs2,lol

Streams Polymarket books for a while, then compares every book with a fresh
REST snapshot taken at the same moment (top 5 levels each side).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from esports_arb.connectors import PolymarketConnector  # noqa: E402
from esports_arb.connectors.polymarket import CLOB  # noqa: E402
from esports_arb.http import post_json  # noqa: E402
from esports_arb.streaming.books import BookStore  # noqa: E402
from esports_arb.streaming.polymarket_ws import PolymarketStream  # noqa: E402


class CheckedStream(PolymarketStream):
    """Before applying each new `book` snapshot, compare it with the book we
    had maintained from incremental updates (a REST-independent check)."""

    checks = 0
    same = 0

    def handle(self, raw, now=None):
        import json
        if raw not in ("PONG", "PING", ""):
            data = json.loads(raw)
            for ev in data if isinstance(data, list) else [data]:
                b = self.store.poly.get(ev.get("asset_id", "")) if ev.get("event_type") == "book" else None
                if b is not None and b.ready:
                    nb = {round(float(x["price"]), 4): float(x["size"]) for x in ev["bids"]}
                    na = {round(float(x["price"]), 4): float(x["size"]) for x in ev["asks"]}
                    CheckedStream.checks += 1
                    CheckedStream.same += (nb == b.bids and na == b.asks)
        super().handle(raw, now)


async def main(seconds: float, games):
    toks = []
    for g in games:
        for m in PolymarketConnector().fetch(g):
            toks += [m.legs_a[0].instrument, m.legs_b[0].instrument]
    toks = toks[:200]
    store = BookStore()
    s = CheckedStream(store, lambda v, i: None)
    await s.add(toks)
    stop = asyncio.Event()
    task = asyncio.create_task(s.run(stop))
    await asyncio.sleep(seconds)
    # compare against REST; the stream keeps running, so take a websocket copy
    # just before and just after the REST call and accept a match with either
    before = {t: (dict(store.pm(t).bids), dict(store.pm(t).asks)) for t in toks if store.pm(t).ready}
    rest = {}
    for i in range(0, len(toks), 40):
        for b in post_json(f"{CLOB}/books", [{"token_id": t} for t in toks[i:i + 40]]):
            rest[b["asset_id"]] = ({round(float(x["price"]), 4): float(x["size"]) for x in b["bids"]},
                                   {round(float(x["price"]), 4): float(x["size"]) for x in b["asks"]})
    after = {t: (dict(store.pm(t).bids), dict(store.pm(t).asks)) for t in toks if store.pm(t).ready}
    stop.set()
    task.cancel()
    top = lambda d, rev: sorted(d.items(), reverse=rev)[:5]
    top_ok = full_ok = n = 0
    shown = 0
    for t in before:
        if t not in rest or t not in after:
            continue
        n += 1
        rb, ra = rest[t]
        cands = (before[t], after[t])
        if any(top(b, True) == top(rb, True) and top(a, False) == top(ra, False) for b, a in cands):
            top_ok += 1
        elif shown < 3:
            shown += 1
            b, a = after[t]
            print("mismatch", t[:12], "ws bids", top(b, True)[:3], "rest", top(rb, True)[:3],
                  "| ws asks", top(a, False)[:3], "rest", top(ra, False)[:3])
        if any(b == rb and a == ra for b, a in cands):
            full_ok += 1
    print(f"{s.msgs} ws messages over {seconds:.0f}s for {len(toks)} tokens")
    print(f"self-consistency: {CheckedStream.same}/{CheckedStream.checks} new snapshots matched the "
          f"incrementally maintained book exactly")
    print(f"books compared: {n} | top-5 levels identical: {top_ok} ({top_ok / max(n, 1):.1%}) | "
          f"entire book identical: {full_ok} ({full_ok / max(n, 1):.1%})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--games", default="cs2,lol,r6,val,dota2,rl,ow")
    a = ap.parse_args()
    asyncio.run(main(a.seconds, a.games.split(",")))
