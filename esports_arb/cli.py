"""Command line interface.

    python -m esports_arb scan                       # all games, all venues
    python -m esports_arb scan -g lol,r6,rl,ow --min-edge 0.005 --bankroll 500
    python -m esports_arb scan --cross-only --json out.json
    python -m esports_arb record --interval 60 --iterations 30 --out data/snapshots.csv
    python -m esports_arb near-misses                # closest-to-arb pairs right now
    python -m esports_arb stream --minutes 30 --out data/stream/episodes.jsonl   # websockets

  market making (Kalshi maker, Polymarket fair value):
    python -m esports_arb mm-data --days 21 --out data/mm/dataset.json.gz
    python -m esports_arb mm-backtest --data data/mm/dataset.json.gz
    python -m esports_arb mm-paper -g cs2,lol --minutes 60 --stream
    python -m esports_arb mm-live -g cs2 --max-exposure 20 --max-loss 10 --confirm-live
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


def cmd_stream(args):
    import asyncio
    from .streaming.hub import StreamHub, kalshi_signer_from_env
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    signer = None if args.no_kalshi_ws else kalshi_signer_from_env()
    hub = StreamHub(_games(args.games), min_edge=args.min_edge, out=args.out, kalshi_signer=signer,
                    poll_interval=args.poll_interval, report_s=args.report_every,
                    cross_only=not args.include_same_venue)
    print(json.dumps(asyncio.run(hub.run(args.minutes)), indent=2))


def _mm_params(args):
    from .mm.quoting import QuoteParams
    return QuoteParams(half_spread=args.half_spread, size=args.size, max_exposure=args.max_exposure,
                       skew=args.skew, ref_weight=args.ref_weight, stop_before_min=args.stop_before,
                       start_hours=args.start_hours)


def cmd_mm_data(args):
    from .mm.data import build
    d = build(_games(args.games), days=args.days, hours=args.start_hours, out=args.out)
    print(f"saved {len(d)} matches -> {args.out}")


def cmd_mm_backtest(args):
    from .mm.backtest import brier, run, summarize
    from .mm.data import load
    data = load(args.data)
    res = run(data, _mm_params(args))
    for k, v in summarize(res).items():
        print(f"{k:24s} {v:,.4f}" if isinstance(v, float) else f"{k:24s} {v}")
    print("brier (5 min pre-match):", brier(data))


def cmd_mm_run(args, live: bool):
    from .mm.live import Engine, KalshiBroker, PaperBroker
    if live:
        if not args.confirm_live:
            raise SystemExit("refusing to trade real money without --confirm-live")
        from .mm.kalshi_client import KalshiClient
        client = KalshiClient()
        print(f"LIVE on Kalshi. balance ${client.balance():.2f}. Ctrl-C or create ./STOP to halt.")
        broker = KalshiBroker(client)
    else:
        broker = PaperBroker()
    feed = None
    if args.stream:
        from .streaming.feed import StreamFeed
        feed = StreamFeed(poll_interval=args.poll_interval)
    eng = Engine(broker, _mm_params(args), _games(args.games), max_total_exposure=args.max_total,
                 max_loss=args.max_loss, interval=args.interval, log_path=args.log, feed=feed)
    print(json.dumps(eng.run(args.minutes), indent=2))


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

    def mm_common(sp, games="cs2,lol,val,dota2,r6,rl,ow"):
        sp.add_argument("-g", "--games", default=games)
        sp.add_argument("--half-spread", type=float, default=0.08)
        sp.add_argument("--size", type=int, default=5)
        sp.add_argument("--max-exposure", type=int, default=25, help="contracts per event")
        sp.add_argument("--skew", type=float, default=0.0004)
        sp.add_argument("--ref-weight", type=float, default=0.5, help="1 = Polymarket fair value only, 0 = Kalshi mid only")
        sp.add_argument("--stop-before", type=float, default=120.0, help="stop quoting N minutes before start")
        sp.add_argument("--start-hours", type=float, default=12.0)

    st = sub.add_parser("stream", help="real-time arb detection over websockets; logs arb episodes")
    st.add_argument("-g", "--games", default=",".join(DEFAULT_GAMES))
    st.add_argument("--minutes", type=float, default=30)
    st.add_argument("--min-edge", type=float, default=0.0)
    st.add_argument("--out", default="data/stream/episodes.jsonl")
    st.add_argument("--poll-interval", type=float, default=1.0,
                    help="Kalshi REST poll seconds when no KALSHI_KEY_ID is set")
    st.add_argument("--no-kalshi-ws", action="store_true", help="force REST polling for Kalshi")
    st.add_argument("--include-same-venue", action="store_true")
    st.add_argument("--report-every", type=float, default=60)
    st.set_defaults(func=cmd_stream)

    d = sub.add_parser("mm-data", help="download settled matches for the MM backtest")
    mm_common(d)
    d.add_argument("--days", type=float, default=21)
    d.add_argument("--out", default="data/mm/dataset.json.gz")
    d.set_defaults(func=cmd_mm_data)

    b = sub.add_parser("mm-backtest", help="backtest the market maker on a dataset")
    mm_common(b)
    b.add_argument("--data", default="data/mm/dataset.json.gz")
    b.set_defaults(func=cmd_mm_backtest)

    for name, live in (("mm-paper", False), ("mm-live", True)):
        r2 = sub.add_parser(name, help=("REAL-MONEY" if live else "paper") + " market making on Kalshi")
        mm_common(r2, games="cs2,lol,val,dota2,r6,rl,ow")
        r2.add_argument("--minutes", type=float, default=60)
        r2.add_argument("--interval", type=float, default=20, help="seconds between requotes")
        r2.add_argument("--max-total", type=float, default=100, help="total |exposure| cap, contracts")
        r2.add_argument("--max-loss", type=float, default=25, help="halt if marked P&L < -this ($)")
        r2.add_argument("--log", default=f"data/mm/{name}.jsonl")
        r2.add_argument("--stream", action="store_true",
                        help="websocket market data, requote on every book change (min 1s apart)")
        r2.add_argument("--poll-interval", type=float, default=1.0,
                        help="Kalshi REST poll seconds when no API key is set (with --stream)")
        if live:
            r2.add_argument("--confirm-live", action="store_true")
        r2.set_defaults(func=(lambda a, _l=live: cmd_mm_run(a, _l)))

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
