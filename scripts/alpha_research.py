"""Kalshi series alpha research: can map-winner / total-maps contracts be priced
off the match market well enough to make money?

    python scripts/alpha_research.py data/alpha/kalshi_series.json.gz --out docs

Chronological split (first half train, second half test). Everything that is
chosen (rho, taker threshold, maker half-spread) is chosen on train only.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from dataclasses import replace
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from esports_arb.alpha.backtest import (Params, Step, _ok, anchor_series, contract_fair,  # noqa: E402
                                        run_maker, run_taker, summarize)
from esports_arb.alpha.data import load  # noqa: E402
from esports_arb.alpha.model import from_match_price  # noqa: E402

DATA = {}


def _run(args):
    kind, which, params = args
    recs = DATA[which]
    fn = run_taker if kind == "taker" else run_maker
    trades = [t for r in recs for t in fn(r, params)]
    return params, summarize(trades, len(recs)), trades


def calibration(recs, rho, minutes_before=5):
    """Log loss / Brier of model fair vs the contract's own mid, on binary-settled contracts."""
    rows = []
    for r in recs:
        bo = r.get("best_of")
        anc = anchor_series(r)
        if not bo or anc is None:
            continue
        t = r["start"] - minutes_before * 60
        a = anc(t)
        if not a or a[1] - a[0] > 0.06:
            continue
        sp = from_match_price(round((a[0] + a[1]) / 2, 3), rho, bo)
        if sp is None:
            continue
        for m in r["markets"]:
            if m["kind"] not in ("map", "total") or m.get("settle") not in (0.0, 1.0):
                continue
            if m["kind"] == "map" and m["index"] > 2:
                continue
            f = contract_fair(m, sp, 0.5)
            b, k = Step(m.get("candles", []), 1).at(t), Step(m.get("candles", []), 2).at(t)
            mid = (b + k) / 2 if _ok(b) and _ok(k) and b < k and k - b <= 0.10 else None
            rows.append((m["kind"], f, mid, m["settle"]))
    return rows


def ll(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def fmt(s):
    return (f"{s['trades']:>5} trades {s['contracts']:>7.0f} ct in {s['matches_traded']:>4} matches | "
            f"P&L ${s['pnl']:>8.2f} | {s['pnl_per_contract'] * 100:+6.2f}c/ct | t={s['t_stat_by_match']:+5.2f} | "
            f"win {s['win_rate']:.0%} | CLV {s['avg_clv'] * 100:+.2f}c ({s['clv_positive']:.0%} +)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--out", default="docs")
    a = ap.parse_args()
    recs = sorted([r for r in load(a.data) if r.get("best_of")], key=lambda r: r["start"])
    half = len(recs) // 2
    DATA["train"], DATA["test"] = recs[:half], recs[half:]
    games = defaultdict(int)
    for r in recs:
        games[r["game"]] += 1
    L = [f"{len(recs)} settled Kalshi matches with map/total markets "
         f"({', '.join(f'{g}={n}' for g, n in sorted(games.items()))}); train {half} / test {len(recs) - half}", ""]

    # 1. choose rho by log loss on train, report test calibration
    L.append("1) Correlated-maps model: log loss on TRAIN map-1/2 + total contracts (5 min pre-start)")
    best_rho, best_ll = None, math.inf
    for rho in (0.0, 0.05, 0.1, 0.15, 0.2, 0.3):
        rows = calibration(DATA["train"], rho)
        v = sum(ll(f, y) for _, f, _, y in rows) / len(rows)
        L.append(f"   rho={rho:.2f}: {v:.4f}  (n={len(rows)})")
        if v < best_ll:
            best_rho, best_ll = rho, v
    rows = calibration(DATA["test"], best_rho)
    both = [(k, f, m, y) for k, f, m, y in rows if m is not None]
    L.append(f"   chosen rho={best_rho}. TEST, contracts with a quoted mid (n={len(both)}):")
    for kind in ("map", "total"):
        sub = [x for x in both if x[0] == kind]
        if sub:
            bm = np.mean([(f - y) ** 2 for _, f, _, y in sub])
            bk = np.mean([(m - y) ** 2 for _, _, m, y in sub])
            L.append(f"     {kind:<6} Brier: model-from-match-price {bm:.4f} | contract's own mid {bk:.4f} (n={len(sub)})")
    L.append("")

    base = Params(rho=best_rho)
    with Pool() as pool:
        # 2. taker
        grid = [("taker", "train", replace(base, theta=th, max_anchor_spread=sp))
                for th in (0.02, 0.03, 0.05, 0.08) for sp in (0.04, 0.08)]
        res = pool.map(_run, grid)
        L.append("2) TAKER (buy mispriced map/total contracts at the real ask). TRAIN grid:")
        for p_, s, _ in res:
            L.append(f"   theta={p_.theta:.2f} anchor<= {p_.max_anchor_spread:.2f} | {fmt(s)}")
        ok = [x for x in res if x[1]["trades"] >= 30]
        bt = max(ok, key=lambda x: x[1]["pnl"])[0] if ok else None
        if bt and max(x[1]["pnl"] for x in ok) > 0:
            _, s, tt = _run(("taker", "test", bt))
            L.append(f"   TEST theta={bt.theta} anchor<= {bt.max_anchor_spread}: {fmt(s)}")
            for kind in ("map", "total"):
                sub = [t for t in tt if t.kind == kind]
                if sub:
                    L.append(f"      {kind:<6} {fmt(summarize(sub, len(DATA['test'])))}")
            byg = defaultdict(list)
            for t in tt:
                byg[t.game].append(t)
            for g, sub in sorted(byg.items()):
                L.append(f"      {g:<6} {fmt(summarize(sub, len(DATA['test'])))}")
            for lbl, pp in (("stop 30 min before start", replace(bt, stop_min=30)),
                            ("rho = 0 (independent maps)", replace(bt, rho=0.0))):
                L.append(f"      robustness, {lbl}: {fmt(_run(('taker', 'test', pp))[1])}")
        else:
            L.append("   no profitable taker configuration on TRAIN -> not tested")
        L.append("")

        # 3. maker
        grid = [("maker", "train", replace(base, half_spread=h)) for h in (0.02, 0.03, 0.05, 0.08)]
        res = pool.map(_run, grid)
        L.append("3) MAKER (quote fair +/- h in map/total books; fills from the real tape). TRAIN grid:")
        for p_, s, _ in res:
            L.append(f"   h={p_.half_spread:.2f} | {fmt(s)}")
        bm_ = max(res, key=lambda x: x[1]["pnl"])
        if bm_[1]["pnl"] > 0:
            bp = bm_[0]
            _, s, tt = _run(("maker", "test", bp))
            L.append(f"   TEST h={bp.half_spread}: {fmt(s)}")
            for kind in ("map", "total"):
                sub = [t for t in tt if t.kind == kind]
                if sub:
                    L.append(f"      {kind:<6} {fmt(summarize(sub, len(DATA['test'])))}")
            for lbl, pp in (("queue share 0", replace(bp, queue_frac=0.0)),
                            ("stop 30 min before start", replace(bp, stop_min=30)),
                            ("rho = 0", replace(bp, rho=0.0))):
                L.append(f"      robustness, {lbl}: {fmt(_run(('maker', 'test', pp))[1])}")
        else:
            L.append("   no profitable maker configuration on TRAIN -> not tested")

    text = "\n".join(L)
    print(text)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "alpha_results.txt"), "w") as fh:
        fh.write(text + "\n")


if __name__ == "__main__":
    main()
