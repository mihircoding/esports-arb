"""Arbitrage-episode persistence from `esports_arb stream` output.

    python scripts/stream_analyze.py data/stream/episodes.jsonl --out docs

Reports:
  * episode count, duration quantiles (ms), share shorter than common polling intervals
  * a Kaplan–Meier survival curve (episodes still open at shutdown are right-censored)
  * which venue's update opened / closed each episode (who creates / corrects the mispricing)
  * edge and size at the peak of each episode
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter

import numpy as np


def load(path):
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def kaplan_meier(durations, observed):
    order = np.argsort(durations)
    d, o = np.asarray(durations)[order], np.asarray(observed)[order]
    n = len(d)
    times, surv, s = [0.0], [1.0], 1.0
    for i in range(n):
        if o[i]:
            s *= 1 - 1 / (n - i)
            times.append(d[i])
            surv.append(s)
    return np.array(times), np.array(surv)


def km_median(times, surv):
    below = np.where(surv <= 0.5)[0]
    return float(times[below[0]]) if len(below) else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episodes")
    ap.add_argument("--out", default="docs")
    ap.add_argument("--log", help="stream.log, to report message rates")
    a = ap.parse_args()
    eps = load(a.episodes)
    if not eps:
        print("no episodes recorded")
        return
    head = []
    if a.log and os.path.exists(a.log):
        rows = [l for l in open(a.log) if "msg/s" in l]
        if rows:
            head.append("feed status at end: " + rows[-1].strip())
    blocks = [("ALL", eps), ("PRE-MATCH", [e for e in eps if not e.get("in_play")]),
              ("IN-PLAY", [e for e in eps if e.get("in_play")])]
    text = "\n\n".join(["\n".join(head)] + [f"== {name} ==\n" + summarize(sub) for name, sub in blocks if sub])
    print(text)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "stream_results.txt"), "w") as fh:
        fh.write(text + "\n")
    plot([("pre-match", [e for e in eps if not e.get("in_play")]),
          ("in-play", [e for e in eps if e.get("in_play")])], a.out)


def summarize(eps):
    dur = np.array([e["duration_ms"] for e in eps])
    obs = np.array([not e["censored"] for e in eps])
    closed = dur[obs]
    t, s = kaplan_meier(dur, obs)
    lines = [
        f"episodes: {len(eps)} ({obs.sum()} closed, {(~obs).sum()} still open at shutdown) "
        f"across {len({e['match'] for e in eps})} matches",
        "closed-episode duration (ms): " + ", ".join(
            f"p{q}={np.percentile(closed, q):,.0f}" for q in (10, 25, 50, 75, 90)) if len(closed) else "",
        f"Kaplan-Meier median lifetime (incl. censored): {km_median(t, s) / 1000:,.1f} s",
    ]
    for lim in (1_000, 5_000, 60_000) if len(closed) else ():
        lines.append(f"  share of closed episodes shorter than {lim / 1000:>4.0f}s: {(closed < lim).mean():.1%}")
    lines.append("opened by an update from: " + ", ".join(f"{k}={v}" for k, v in Counter(
        e["open_trigger"] for e in eps).most_common()))
    lines.append("closed by an update from: " + ", ".join(f"{k}={v}" for k, v in Counter(
        e["close_trigger"] for e in eps if not e["censored"]).most_common()))
    edge = np.array([e["max_edge"] for e in eps])
    size = np.array([e["max_size"] for e in eps])
    prof = np.array([e["max_profit"] for e in eps])
    lines.append(f"peak edge per $1: median {np.median(edge) * 100:.2f}% | p90 {np.percentile(edge, 90) * 100:.2f}%")
    lines.append(f"peak executable size: median {np.median(size):.0f} | p90 {np.percentile(size, 90):.0f} contracts")
    lines.append(f"peak $ profit: median {np.median(prof):.2f} | sum over episodes {prof.sum():.2f}")
    lines.append("by game: " + ", ".join(f"{k}={v}" for k, v in Counter(e["game"] for e in eps).most_common()))
    lines.append("leg combos: " + ", ".join(f"{k}={v}" for k, v in Counter(
        e["leg_a"].split(":")[0] + " + " + e["leg_b"].split(":")[0] for e in eps).most_common(4)))
    return "\n".join(l for l in lines if l)


def plot(groups, out):

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    ax = axes[0]
    colors = {"pre-match": "#4C72B0", "in-play": "#DD8452"}
    all_closed = []
    for name, eps in groups:
        if not eps:
            continue
        dur = np.array([e["duration_ms"] for e in eps])
        obs = np.array([not e["censored"] for e in eps])
        t, s = kaplan_meier(dur, obs)
        ax.step(t / 1000, s, where="post", color=colors[name], label=f"{name} (n={len(eps)})")
        all_closed.append((name, dur[obs]))
    ax.legend(frameon=False, loc="lower left")
    ax.set_xscale("symlog", linthresh=1)
    for x, lab in ((1, "1s"), (60, "60s poll")):
        ax.axvline(x, color="gray", ls="--", lw=0.8)
        ax.text(x, 0.95, f" {lab}", color="gray", fontsize=8)
    ax.set_xlabel("seconds since the arb appeared (symlog)")
    ax.set_ylabel("share of arbs still open")
    ax.set_title("Arb survival (Kaplan–Meier)")
    ax.set_ylim(0, 1.02)
    ax = axes[1]
    allv = np.concatenate([c for _, c in all_closed]) if all_closed else np.array([])
    if len(allv):
        bins = np.logspace(np.log10(max(allv.min(), 1)), np.log10(allv.max() + 1), 30)
        for name, c in all_closed:
            ax.hist(c, bins=bins, alpha=0.6, color=colors[name], label=name)
        ax.set_xscale("log")
        ax.legend(frameon=False)
    ax.set_xlabel("episode duration (ms, log)")
    ax.set_ylabel("episodes")
    ax.set_title("Closed-episode durations")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "stream_persistence.png"), dpi=130)


if __name__ == "__main__":
    main()
