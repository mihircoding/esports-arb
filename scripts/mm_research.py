"""Market-making research: walk-forward parameter choice + robustness.

    python scripts/mm_research.py data/mm/dataset.json.gz --out docs

1. Sort matches by start time; first half = train, second half = test.
2. Grid-search half-spread x fair-value weight x skew on TRAIN only.
3. Report the chosen config on TEST (never seen during selection), against
   the same quoting rule without the Polymarket signal (ref_weight = 0).
4. Stress the fill model: queue share and latency.
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
from collections import defaultdict
from dataclasses import replace
from multiprocessing import Pool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from esports_arb.mm.backtest import brier, run, summarize  # noqa: E402
from esports_arb.mm.data import load  # noqa: E402
from esports_arb.mm.quoting import QuoteParams  # noqa: E402

BASE = QuoteParams(size=5, max_exposure=25)
GRID = {
    "half_spread": [0.02, 0.03, 0.05, 0.08, 0.12],
    "ref_weight": [0.0, 0.5, 1.0],
    "stop_before_min": [5, 30, 120],
}
TRAIN = []


def _eval(params):
    return params, summarize(run(TRAIN, params))


def fmt(s):
    return (f"matches {s['matches']:>4} traded {s['matches_traded']:>4} | P&L ${s['total_pnl']:>8.2f} "
            f"| mean ${s['mean_pnl']:>6.3f} t={s['t_stat']:>5.2f} | contracts {s['contracts']:>8.0f} "
            f"| ${s['pnl_per_contract']:.4f}/ct | capture ${s['capture']:>7.2f} drift ${s['drift']:>7.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--out", default="docs")
    a = ap.parse_args()
    data = sorted(load(a.data), key=lambda r: r["start"])
    half = len(data) // 2
    train, test = data[:half], data[half:]
    lines = [f"dataset: {len(data)} settled matches on Kalshi+Polymarket "
             f"({', '.join(f'{g}={n}' for g, n in sorted(count_games(data).items()))})",
             f"train: {len(train)} | test: {len(test)} (chronological split)", ""]

    b = brier(data)
    lines.append(f"Brier score 5 min before start (lower = better), n={b['n']}: "
                 f"Polymarket {b['polymarket']:.4f} vs Kalshi mid {b['kalshi_mid']:.4f}")
    lines.append("")

    TRAIN[:] = train
    combos = [replace(BASE, half_spread=hs, ref_weight=w, stop_before_min=sb)
              for hs, w, sb in itertools.product(*GRID.values())]
    with Pool() as pool:  # fork start method shares TRAIN with workers
        grid = pool.map(_eval, combos)
    grid.sort(key=lambda x: x[1]["total_pnl"], reverse=True)
    lines.append("TRAIN grid (top 8 by P&L):")
    for p, s in grid[:8]:
        lines.append(f"  h={p.half_spread:.2f} w={p.ref_weight:.1f} stop={p.stop_before_min:>3.0f}m | {fmt(s)}")
    best = grid[0][0]
    lines.append("")

    lines.append(f"TEST (out-of-sample) with chosen h={best.half_spread} w={best.ref_weight} "
                 f"stop={best.stop_before_min:.0f}m:")
    test_best = run(test, best)
    lines.append(f"  chosen          | {fmt(summarize(test_best))}")
    for w in (0.0, 0.5, 1.0):
        if w != best.ref_weight:
            lines.append(f"  same, w={w:.1f}    | {fmt(summarize(run(test, replace(best, ref_weight=w))))}")
    lines.append("")

    lines.append("TEST by game (chosen config):")
    per = defaultdict(list)
    for r in test_best:
        per[r.game].append(r)
    for g, rs in sorted(per.items()):
        lines.append(f"  {g:6s} {fmt(summarize(rs))}")
    lines.append("")

    lines.append("Adverse selection by time to start (TEST, chosen config but quoting until 5 min before, per contract):")
    for label, cap, drift, qty in markouts(test, replace(best, stop_before_min=5)):
        lines.append(f"  {label:>9} min before | {qty:>6.0f} contracts | capture {cap:+.4f} | drift {drift:+.4f} "
                     f"| net {cap + drift:+.4f}")
    lines.append("")

    lines.append("Robustness on TEST (chosen config):")
    for qf in (0.0, 0.1, 0.25, 0.5):
        lines.append(f"  queue_frac={qf:<4} latency=60s | {fmt(summarize(run(test, replace(best, queue_frac=qf))))}")
    for lat in (180, 600):
        lines.append(f"  queue_frac=0.25 latency={lat}s | {fmt(summarize(run(test, best, latency_s=lat)))}")
    fee = replace(best, maker_fee_rate=0.0175)
    lines.append(f"  if Kalshi added its 1.75% maker fee | {fmt(summarize(run(test, fee)))}")

    text = "\n".join(lines)
    print(text)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "mm_results.txt"), "w") as fh:
        fh.write(text + "\n")
    plot(train, test, best, a.out)


def markouts(data, p):
    from esports_arb.mm.backtest import run_match
    from esports_arb.mm.book import EventBook
    buckets = [(0, 30), (30, 120), (120, 360), (360, 720)]
    acc = {b: [0.0, 0.0, 0.0] for b in buckets}
    orig = EventBook.fill
    seen = []
    EventBook.fill = lambda self, f: (seen.append(f), orig(self, f))[1]
    try:
        for rec in data:
            seen.clear()
            run_match(rec, p)
            for f in seen:
                mins = (rec["start"] - f.ts) / 60
                b = next((b for b in buckets if b[0] <= mins < b[1]), None)
                if b is None:
                    continue
                sgn = 1 if f.side == "buy" else -1
                won = 1.0 if rec["a_won"] == (f.market == 0) else 0.0
                acc[b][0] += sgn * f.qty * (f.fair - f.price)
                acc[b][1] += sgn * f.qty * (won - f.fair)
                acc[b][2] += f.qty
    finally:
        EventBook.fill = orig
    return [(f"{lo}-{hi}", c / q if q else 0, d / q if q else 0, q) for (lo, hi), (c, d, q) in acc.items()]


def count_games(data):
    c = defaultdict(int)
    for r in data:
        c[r["game"]] += 1
    return c


def plot(train, test, best, out):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    ax = axes[0]
    for w, color, label in ((best.ref_weight, "#4C72B0", f"chosen (w={best.ref_weight})"),
                            (0.0, "#DD8452", "Kalshi-mid only (w=0)")):
        res = run(train + test, replace(best, ref_weight=w))
        cum, xs = 0.0, []
        for r in res:
            cum += r.pnl
            xs.append(cum)
        ax.plot(range(len(xs)), xs, color=color, label=label)
    ax.axvline(len(train), color="black", lw=1, ls="--")
    ax.text(len(train), ax.get_ylim()[1], " test →", va="top")
    ax.set_xlabel("match # (chronological)")
    ax.set_ylabel("cumulative P&L ($, 5-lot quotes)")
    ax.set_title("Market making on Kalshi esports")
    ax.legend(frameon=False)

    ax = axes[1]
    res = run(test, best)
    cap = [r.capture for r in res if r.fills]
    drift = [r.drift for r in res if r.fills]
    ax.scatter(cap, drift, s=12, alpha=0.6, color="#4C72B0")
    ax.axhline(0, color="black", lw=0.8)
    ax.axvline(0, color="black", lw=0.8)
    ax.set_xlabel("spread capture vs fair value ($)")
    ax.set_ylabel("drift to settlement ($)")
    ax.set_title("Per-match P&L decomposition (test)")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "mm_backtest.png"), dpi=130)


if __name__ == "__main__":
    main()
