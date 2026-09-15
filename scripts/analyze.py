"""Research on recorded cross-venue quotes (output of `esports_arb record`).

Questions:
  1. How often does the best cross-venue pair sum to < $1 (gross arb)?
  2. How often does it survive fees (net arb)? What does the edge distribution look like?
  3. How big is the disagreement ("basis") between Kalshi and Polymarket
     de-vigged probabilities, and does it mean-revert?
  4. How long do net arbs persist between snapshots?

    python scripts/analyze.py data/snapshots.csv --out docs
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from esports_arb.fees import marginal_fee_per_unit  # noqa: E402


def load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.dropna(subset=["ask"])
    df = df[(df.ask > 0) & (df.ask < 1)]
    df["ts"] = pd.to_datetime(df.ts)
    df["fee"] = [marginal_fee_per_unit(m, p, r) for m, p, r in zip(df.fee_model, df.ask, df.fee_rate)]
    df["all_in"] = df.ask + df.fee
    return df


def best_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """For each (snapshot, match) the cheapest cross-venue A+B combination."""
    rows = []
    for (ts, game, match), g in df.groupby(["ts", "game", "match"]):
        a, b = g[g.team == "a"], g[g.team == "b"]
        best = None
        for _, ra in a.iterrows():
            for _, rb in b.iterrows():
                if ra.venue == rb.venue:
                    continue
                gross = ra.ask + rb.ask
                net = ra.all_in + rb.all_in
                if best is None or net < best["net_cost"]:
                    best = dict(ts=ts, game=game, match=match, gross_cost=gross, net_cost=net,
                                leg_a=f"{ra.venue}/{ra.side}", leg_b=f"{rb.venue}/{rb.side}",
                                size=min(ra.ask_size, rb.ask_size), start=ra.start)
        if best:
            rows.append(best)
    out = pd.DataFrame(rows)
    out["gross_edge"] = 1 - out.gross_cost
    out["net_edge"] = 1 - out.net_cost
    return out


def basis(df: pd.DataFrame) -> pd.DataFrame:
    """De-vigged P(team a) on each venue using YES/token asks only."""
    yes = df[df.side == "YES"]
    piv = yes.pivot_table(index=["ts", "game", "match", "venue"], columns="team", values="ask").dropna()
    piv["p_a"] = piv["a"] / (piv["a"] + piv["b"])
    piv["overround"] = piv["a"] + piv["b"] - 1
    wide = piv["p_a"].unstack("venue").dropna()
    if not {"kalshi", "polymarket"} <= set(wide.columns):
        return pd.DataFrame()
    wide["basis"] = wide["kalshi"] - wide["polymarket"]
    ov = piv["overround"].unstack("venue")
    wide["ovr_kalshi"], wide["ovr_poly"] = ov.get("kalshi"), ov.get("polymarket")
    return wide.reset_index()


def half_life(b: pd.DataFrame) -> float:
    """Pooled AR(1) on basis within each match: b_t = phi * b_{t-1} + e."""
    xs, ys = [], []
    for _, g in b.sort_values("ts").groupby("match"):
        v = g.basis.values
        xs += list(v[:-1])
        ys += list(v[1:])
    if len(xs) < 10:
        return float("nan")
    x, y = np.array(xs), np.array(ys)
    phi = (x @ y) / (x @ x)
    return float(np.log(0.5) / np.log(phi)) if 0 < phi < 1 else float("inf"), float(phi)


def persistence(pairs: pd.DataFrame) -> pd.Series:
    """Run lengths (in snapshots) of consecutive net-positive arbs per match."""
    runs = []
    for _, g in pairs.sort_values("ts").groupby("match"):
        cur = 0
        for pos in (g.net_edge > 0):
            if pos:
                cur += 1
            elif cur:
                runs.append(cur)
                cur = 0
        if cur:
            runs.append(cur)
    return pd.Series(runs, dtype=float)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", default="docs")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    df = load(args.csv)
    pairs = best_pairs(df)
    b = basis(df)
    snaps = df.ts.nunique()
    span = (df.ts.max() - df.ts.min()).total_seconds() / 60

    lines = [
        f"snapshots: {snaps} over {span:.0f} min | quotes: {len(df)} | linked matches: {pairs.match.nunique()}",
        f"match-snapshots: {len(pairs)}",
        f"gross arb (sum of asks < 1): {(pairs.gross_edge > 0).mean()*100:.1f}%",
        f"net arb (after taker fees):  {(pairs.net_edge > 0).mean()*100:.1f}%",
        f"net edge percentiles (bp): " + ", ".join(
            f"p{q}={np.percentile(pairs.net_edge, q)*1e4:.0f}" for q in (5, 25, 50, 75, 95, 99)),
    ]
    pos = pairs[pairs.net_edge > 0]
    if len(pos):
        lines += [
            f"when net-positive: median edge {pos.net_edge.median()*1e4:.0f}bp, "
            f"median top-of-book size {pos['size'].median():.0f} contracts, "
            f"median $ profit at top {(pos.net_edge*pos['size']).median():.2f}",
            "net-positive by game: " + ", ".join(f"{k}={v}" for k, v in pos.groupby('game').match.nunique().items()),
            "leg combos: " + ", ".join(f"{k}={v}" for k, v in (pos.leg_a + ' + ' + pos.leg_b).value_counts().head(4).items()),
        ]
        runs = persistence(pairs)
        lines.append(f"arb persistence (snapshots): mean {runs.mean():.1f}, median {runs.median():.0f}, max {runs.max():.0f}")
    if len(b):
        hl = half_life(b)
        lines += [
            f"basis Kalshi-Polymarket (de-vigged P(team a)): mean {b.basis.mean()*100:+.2f}pp, "
            f"mean |basis| {b.basis.abs().mean()*100:.2f}pp, p95 |basis| {b.basis.abs().quantile(.95)*100:.2f}pp",
            f"median overround: kalshi {b.ovr_kalshi.median()*100:.2f}% | polymarket {b.ovr_poly.median()*100:.2f}%",
        ]
        if isinstance(hl, tuple):
            lines.append(f"basis AR(1) phi={hl[1]:.3f} -> half-life {hl[0]:.1f} snapshots")
    text = "\n".join(lines)
    print(text)
    with open(os.path.join(args.out, "results.txt"), "w") as fh:
        fh.write(text + "\n")
    pairs.to_csv(os.path.join(args.out, "best_pairs.csv"), index=False)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    ax = axes[0]
    bins = np.arange(-15, 6.5, 0.5)
    clip = lambda s: np.clip(s * 100, -15, 6)  # tails piled into the edge bins
    ax.hist(clip(pairs.gross_edge), bins=bins, alpha=0.55, label="gross (before fees)", color="#4C72B0")
    ax.hist(clip(pairs.net_edge), bins=bins, alpha=0.55, label="net (after fees)", color="#DD8452")
    ax.axvline(0, color="black", lw=1)
    ax.set_xlabel("edge per $1 payout (%), clipped to [-15, 6]")
    ax.set_ylabel("match-snapshots")
    ax.set_title("Best cross-venue pair: 1 - (ask A + ask B)")
    ax.legend(frameon=False)
    ax = axes[1]
    if len(b):
        ax.scatter(b.polymarket, b.kalshi, s=8, alpha=0.4, color="#4C72B0")
        ax.plot([0, 1], [0, 1], color="black", lw=1)
        ax.set_xlabel("Polymarket P(team a), de-vigged")
        ax.set_ylabel("Kalshi P(team a), de-vigged")
        ax.set_title("Venue agreement")
    fig.tight_layout()
    fig.savefig(os.path.join(args.out, "edge_distribution.png"), dpi=130)


if __name__ == "__main__":
    main()
