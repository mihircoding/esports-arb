"""Command line interface.

    python -m esports_arb scan                       # all games, all venues
    python -m esports_arb scan -g lol,r6,rl,ow --min-edge 0.005 --bankroll 500
    python -m esports_arb scan --cross-only --json out.json
    python -m esports_arb record --interval 60 --iterations 30 --out data/snapshots.csv
    python -m esports_arb near-misses                # closest-to-arb pairs right now
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone

from .arb import top_edge
from .connectors import KalshiConnector, ManualBookConnector, OddsPapiConnector, PolymarketConnector
from .games import DEFAULT_GAMES, GAMES
from .report import format_opps, opp_to_dict
from .scanner import basis_rows, leg_pairs, scan


def build_connectors(args):
    conns = [KalshiConnector(depth=not args.no_depth), PolymarketConnector()]
    books = OddsPapiConnector(max_payout=args.book_limit)
    if books.enabled:
        conns.append(books)
    else:
        logging.info("ODDSPAPI_KEY not set — sportsbooks skipped (use --manual for pasted odds)")
    if args.manual:
        conns.append(ManualBookConnector(args.manual))
    return conns


def _games(s: str):
    games = [g.strip() for g in s.split(",") if g.strip()]
    bad = [g for g in games if g not in GAMES]
    if bad:
        raise SystemExit(f"unknown game(s) {bad}; choose from {sorted(GAMES)}")
    return games


def cmd_scan(args):
    opps, clusters = scan(build_connectors(args), _games(args.games), min_edge=args.min_edge,
                          max_cost=args.bankroll or math.inf, cross_only=args.cross_only)
    linked = sum(1 for c in clusters if len(c.venues) > 1)
    print(f"{len(clusters)} matches, {linked} listed on 2+ venues, {len(opps)} opportunities\n")
    print(format_opps(opps, args.limit))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump([opp_to_dict(o) for o in opps], fh, indent=2)


def cmd_near(args):
    _, clusters = scan(build_connectors(args), _games(args.games), min_edge=1.0)  # evaluate nothing
    rows = []
    for c in clusters:
        if len(c.venues) < 2:
            continue
        for la, ma, lb, mb in leg_pairs(c):
            if ma.venue == mb.venue:
                continue
            e = top_edge(la, lb)
            if e is not None:
                rows.append((e, c, la, lb))
    rows.sort(key=lambda r: r[0], reverse=True)
    print("edge%    game  match / legs")
    for e, c, la, lb in rows[: args.limit]:
        print(f"{e*100:+6.2f}  {c.game:5s} {c.anchor.team_a} vs {c.anchor.team_b}: "
              f"{la.venue}{'/'+la.side if la.venue=='kalshi' else ''} {la.best:.3f} + "
              f"{lb.venue}{'/'+lb.side if lb.venue=='kalshi' else ''} {lb.best:.3f}")


def cmd_record(args):
    conns = build_connectors(args)
    games = _games(args.games)
    new = not os.path.exists(args.out)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    fields = ["ts", "game", "match", "start", "venue", "side", "team", "ask", "ask_size",
              "fee_model", "fee_rate"]
    for i in range(args.iterations):
        t0 = time.time()
        ts = datetime.now(timezone.utc)
        opps, clusters = scan(conns, games, min_edge=0.0, cross_only=False)
        rows = basis_rows(clusters, ts)
        with open(args.out, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            if new:
                w.writeheader()
                new = False
            w.writerows(rows)
        print(f"[{ts:%H:%M:%S}] snapshot {i+1}/{args.iterations}: {len(rows)} quotes, {len(opps)} arbs",
              flush=True)
        if i + 1 < args.iterations:
            time.sleep(max(0, args.interval - (time.time() - t0)))


def main(argv=None):
    p = argparse.ArgumentParser(prog="esports_arb", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("-g", "--games", default=",".join(DEFAULT_GAMES))
        sp.add_argument("--manual", help="JSON file of pasted sportsbook odds")
        sp.add_argument("--book-limit", type=float, default=500.0, help="assumed max payout per book leg")
        sp.add_argument("--no-depth", action="store_true", help="top-of-book only (fewer requests)")
        sp.add_argument("--limit", type=int, default=25)

    s = sub.add_parser("scan", help="find arbitrage now")
    common(s)
    s.add_argument("--min-edge", type=float, default=0.0, help="per-$1 edge after fees, e.g. 0.01")
    s.add_argument("--bankroll", type=float, default=0.0, help="max $ outlay per opportunity")
    s.add_argument("--cross-only", action="store_true", help="ignore same-venue arbs")
    s.add_argument("--json")
    s.set_defaults(func=cmd_scan)

    n = sub.add_parser("near-misses", help="rank cross-venue pairs by edge, even if negative")
    common(n)
    n.set_defaults(func=cmd_near)

    r = sub.add_parser("record", help="log cross-venue quotes to CSV for research")
    common(r)
    r.add_argument("--interval", type=float, default=60)
    r.add_argument("--iterations", type=int, default=10)
    r.add_argument("--out", default="data/snapshots.csv")
    r.set_defaults(func=cmd_record)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
