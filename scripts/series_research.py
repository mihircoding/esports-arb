"""Is betting series-structure inconsistencies +EV?  (Polymarket, settled BO3s)

    python scripts/series_research.py data/series/history.json.gz --out docs

1. How inconsistent are pre-match prices?  Residual of the exact identity
   Over + 2*H_A = W1 + W2, and of the no-arbitrage bounds H_A <= M <= H_A + Over.
2. Are maps independent?  Realised 2-0 rate vs the independent-maps prediction.
3. Which price is right when they disagree?  Brier scores of each market vs the
   estimate implied by the *other* markets.
4. Walk-forward betting test: logistic model (own price + other-market estimate)
   fit on the first half, bets placed on the second half when the model and the
   price differ by more than a threshold chosen by cross-validation on the first
   half. Execution at price + half-spread + Polymarket taker fee.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from esports_arb.series.history import load  # noqa: E402
from esports_arb.series.model import match_prob  # noqa: E402

HALF_SPREAD = 0.01
FEE_RATE = 0.05
EPS = 1e-4


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


DROP_HALF = True   # an untraded Polymarket book reports a 0.50 mid: treat exact 0.50 as missing


def frame(data):
    rows = []
    for r in sorted(data, key=lambda r: r["start"]):
        c = r["contracts"]
        def g(k):
            p = c[k]["price"] if k in c and c[k].get("price") is not None else np.nan
            return np.nan if DROP_HALF and p == 0.5 else p
        y = lambda k: (c[k]["result"] if k in c else np.nan)
        w1, w2 = g("map1"), g("map2")
        ha, hb, o = g("h_a"), g("h_b"), g("over")
        # derive the missing handicap from the partition H_A + H_B + Over = 1
        if np.isnan(ha) and not np.isnan(hb) and not np.isnan(o):
            ha = 1 - hb - o
        rows.append(dict(
            t=r["start"], game=r["game"], m=g("match"), w1=w1, w2=w2, o=o, ha=ha,
            y_m=y("match"), y_1=y("map1"), y_2=y("map2"),
            y_o=float(y("map1") != y("map2")), y_a20=float(y("map1") == 1 and y("map2") == 1),
            rec=r,
        ))
    return {k: np.array([d[k] for d in rows]) for k in rows[0]}


def brier(p, y):
    ok = ~np.isnan(p) & ~np.isnan(y)
    return float(np.mean((p[ok] - y[ok]) ** 2)), int(ok.sum())


def cost(p):
    return p + HALF_SPREAD + FEE_RATE * p * (1 - p)


def fit_logit(X, y):
    from sklearn.linear_model import LogisticRegression
    return LogisticRegression(C=1e6, max_iter=1000).fit(X, y)


def bet_pnl(price, prob, y, thr):
    """+1 share YES when prob - price > thr, +1 share NO when price - prob > thr."""
    pnl, n = [], 0
    for p, q, yy in zip(price, prob, y):
        if q - p > thr:
            pnl.append(yy - cost(p))
        elif p - q > thr:
            pnl.append((1 - yy) - cost(1 - p))
    return np.array(pnl)


def strategy(name, price, other, y, lines, split):
    ok = ~np.isnan(price) & ~np.isnan(other) & ~np.isnan(y)
    idx = np.where(ok)[0]
    tr, te = idx[idx < split], idx[idx >= split]
    if len(tr) < 50 or len(te) < 50:
        lines.append(f"  {name:<34} not enough data ({len(tr)}/{len(te)})")
        return
    X = lambda ii: np.column_stack([logit(price[ii]), logit(other[ii])])
    # choose the threshold with 5-fold CV predictions on the training half
    folds = np.array_split(tr, 5)
    cv_prob = np.empty(len(tr))
    pos = {v: k for k, v in enumerate(tr)}
    for f in folds:
        rest = np.setdiff1d(tr, f)
        mdl = fit_logit(X(rest), y[rest])
        cv_prob[[pos[v] for v in f]] = mdl.predict_proba(X(f))[:, 1]
    best_thr, best = None, -math.inf
    for thr in (0.02, 0.03, 0.05, 0.08, 0.12):
        pnl = bet_pnl(price[tr], cv_prob, y[tr], thr)
        if len(pnl) >= 20 and pnl.sum() > best:
            best_thr, best = thr, pnl.sum()
    mdl = fit_logit(X(tr), y[tr])
    coef = mdl.coef_[0]
    if best_thr is None or best <= 0:
        lines.append(f"  {name:<34} coef own={coef[0]:+.2f} other={coef[1]:+.2f} | REJECTED: no threshold "
                     f"is profitable in cross-validation on the training half -> not traded on test")
        return
    prob_te = mdl.predict_proba(X(te))[:, 1]
    pnl = bet_pnl(price[te], prob_te, y[te], best_thr)
    t = pnl.mean() / (pnl.std(ddof=1) / math.sqrt(len(pnl))) if len(pnl) > 1 and pnl.std() > 0 else 0
    lines.append(f"  {name:<34} coef own={coef[0]:+.2f} other={coef[1]:+.2f} | thr {best_thr:.2f} "
                 f"(CV train P&L {best:+.1f}) | TEST {len(pnl)} bets, P&L {pnl.sum():+.2f}, "
                 f"{pnl.mean() * 100:+.2f}c/bet, t={t:.2f}")
    return {"te": te, "prob": prob_te, "thr": best_thr, "price": price, "y": y}


def executed(res, recs, key, lines, name, delay_min=30):
    """Re-run the test-half bets using only prices somebody actually paid.

    Decision at T-30 min. A YES (NO) bet is filled only if a taker BOUGHT the YES (NO)
    token between the decision and the start, at a price where the bet is still +EV
    by the model; we pay that trade's price plus the taker fee. No such trade -> no bet.
    """
    if not res:
        return
    pnl, skipped_unknown, not_filled = [], 0, 0
    for i, q_hat in zip(res["te"], res["prob"]):
        p = res["price"][i]
        side = 0 if q_hat - p > res["thr"] else 1 if p - q_hat > res["thr"] else None
        if side is None:
            continue
        c = recs[i]["contracts"].get(key)
        if not c or "trades" not in c or not c.get("complete", False):
            skipped_unknown += 1
            continue
        from datetime import datetime
        start = datetime.fromisoformat(recs[i]["start"]).timestamp()
        fair = q_hat if side == 0 else 1 - q_hat
        fill = next((tr for tr in c["trades"]
                     if start - delay_min * 60 <= tr[0] <= start and tr[1] == side and tr[2] == "BUY"
                     and fair - tr[3] - FEE_RATE * tr[3] * (1 - tr[3]) > 0), None)
        if fill is None:
            not_filled += 1
            continue
        px = fill[3]
        won = res["y"][i] if side == 0 else 1 - res["y"][i]
        pnl.append(won - px - FEE_RATE * px * (1 - px))
    pnl = np.array(pnl)
    if len(pnl) > 1:
        t = pnl.mean() / (pnl.std(ddof=1) / math.sqrt(len(pnl))) if pnl.std() > 0 else 0
        lines.append(f"  {name:<34} EXECUTABLE (real taker prints): {len(pnl)} bets filled, "
                     f"{not_filled} never tradable, {skipped_unknown} no trade data | P&L {pnl.sum():+.2f}, "
                     f"{pnl.mean() * 100:+.2f}c/bet, t={t:.2f}")
    else:
        lines.append(f"  {name:<34} EXECUTABLE: {len(pnl)} bets filled, {not_filled} never tradable, "
                     f"{skipped_unknown} no trade data")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--out", default="docs")
    ap.add_argument("--keep-half", action="store_true", help="keep exact 0.50 prices (default: drop)")
    a = ap.parse_args()
    global DROP_HALF
    DROP_HALF = not a.keep_half
    F = frame(load(a.data))
    n = len(F["m"])
    split = n // 2
    counts = ", ".join(f"{g}={int((F['game'] == g).sum())}" for g in np.unique(F["game"]))
    L = [f"{n} settled BO3s ({counts}), exact-0.50 prices {'dropped' if DROP_HALF else 'kept'}, "
         f"prices 30 min before start; train = first {split}, test = last {n - split} (chronological)", ""]

    # 1. consistency
    w1, w2, m, o, ha = F["w1"], F["w2"], F["m"], F["o"], F["ha"]
    r = o + 2 * ha - w1 - w2
    ok = ~np.isnan(r)
    L.append(f"1) Identity Over + 2*H_A = W1 + W2 at pre-match prices (n={ok.sum()}):")
    L.append(f"   residual median {np.median(r[ok]) * 100:+.2f}pp | mean |r| {np.mean(np.abs(r[ok])) * 100:.2f}pp | "
             f"|r| > 3pp: {np.mean(np.abs(r[ok]) > 0.03):.1%} | > 6pp: {np.mean(np.abs(r[ok]) > 0.06):.1%}")
    lb = m - ha
    ub = ha + o - m
    okb = ~np.isnan(lb) & ~np.isnan(ub)
    L.append(f"   bound H_A <= M violated: {np.mean(lb[okb] < 0):.1%} | M <= H_A + Over violated: "
             f"{np.mean(ub[okb] < 0):.1%}   (at mid prices; not net of spread/fees)")
    # basket P&L if traded at mid + half-spread + fee (the identity basket pays exactly 2 or 3)
    legs_cost = lambda ps: sum(cost(p) for p in ps)
    pnl_id = []
    for i in np.where(ok)[0]:
        if r[i] < 0:   # buy Over + 2 H_A + B-map1 + B-map2 -> pays 2
            c = legs_cost([o[i], ha[i], ha[i], 1 - w1[i], 1 - w2[i]])
            pnl_id.append(2 - c)
        else:          # buy Under + 2 (not H_A) + A-map1 + A-map2 -> pays 3
            c = legs_cost([1 - o[i], 1 - ha[i], 1 - ha[i], w1[i], w2[i]])
            pnl_id.append(3 - c)
    pnl_id = np.array(pnl_id)
    L.append(f"   identity basket after 1c half-spread + fees: profitable in {np.mean(pnl_id > 0):.1%} of matches, "
             f"mean when profitable {pnl_id[pnl_id > 0].mean() * 100 if (pnl_id > 0).any() else 0:.2f}c per basket")
    L.append("")

    # 2. independence
    ind20 = w1 * w2 + (1 - w1) * (1 - w2)
    y20 = 1 - F["y_o"]
    ok2 = ~np.isnan(ind20)
    L.append("2) Are maps independent?")
    L.append(f"   realised 2-0 rate {np.mean(y20[ok2]):.3f} vs independent-maps prediction {np.mean(ind20[ok2]):.3f}"
             + (f" vs market (1 - Over) {np.nanmean(1 - o):.3f}" if (~np.isnan(o)).any() else ""))
    L.append(f"   P(map2 = map1 winner) realised {np.mean(F['y_1'] == F['y_2']):.3f}")
    L.append("")

    # 3. which price is right
    m_ind = np.array([match_prob([a_, b_, (a_ + b_) / 2]) if not np.isnan(a_ + b_) else np.nan
                      for a_, b_ in zip(w1, w2)])
    o_ind = w1 * (1 - w2) + (1 - w1) * w2
    o_id = w1 + w2 - 2 * ha
    h_ind = w1 * w2
    L.append("3) Brier scores (lower is better) — market price vs estimate from the other markets:")
    for label, own, oth, y in (("match winner", m, m_ind, F["y_m"]),
                               ("over 2.5 maps (indep.)", o, o_ind, F["y_o"]),
                               ("over 2.5 maps (identity W1+W2-2H)", o, o_id, F["y_o"]),
                               ("A wins 2-0", ha, h_ind, F["y_a20"]),
                               ("map 1 winner", w1, np.full(n, np.nan), F["y_1"])):
        b1, n1 = brier(own, y)
        mask = ~np.isnan(own) & ~np.isnan(oth) & ~np.isnan(y)
        if mask.sum():
            b_own, _ = brier(own[mask], y[mask])
            b_oth, _ = brier(oth[mask], y[mask])
            L.append(f"   {label:<36} market {b_own:.4f} | other-markets estimate {b_oth:.4f} | n={mask.sum()}")
    L.append("")

    # 4. betting
    L.append("4) Walk-forward bets (1 share each, cost = price + 1c + fee):")
    recs = F["rec"]
    has_native_ha = np.array(["h_a" in r["contracts"] for r in recs])
    ha_native = np.where(has_native_ha, ha, np.nan)
    specs = (("match winner  vs indep. maps", m, m_ind, F["y_m"], None),
             ("over 2.5     vs indep. maps", o, o_ind, F["y_o"], "over"),
             ("over 2.5     vs identity", o, o_id, F["y_o"], "over"),
             ("A 2-0        vs W1*W2", ha_native, h_ind, F["y_a20"], "h_a"))
    for name, own, oth, y, key in specs:
        res = strategy(name, own, oth, y, L, split)
        if key:
            executed(res, recs, key, L, name)
    text = "\n".join(L)
    print(text)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "series_results.txt"), "w") as fh:
        fh.write(text + "\n")
    plot(F, m_ind, a.out)


def plot(F, m_ind, out):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    ax = axes[0]
    ok = ~np.isnan(m_ind)
    ax.scatter(m_ind[ok], F["m"][ok], s=6, alpha=0.35, color="#4C72B0")
    ax.plot([0, 1], [0, 1], color="black", lw=1)
    ax.set_xlabel("match price implied by map-1/map-2 prices (independent maps)")
    ax.set_ylabel("match price")
    ax.set_title("Match vs map markets (Polymarket, 30 min pre-start)")
    ax = axes[1]
    bins = np.linspace(0, 1, 11)
    for label, p, y, col in (("match price", F["m"], F["y_m"], "#4C72B0"),
                             ("indep.-maps estimate", m_ind, F["y_m"], "#DD8452")):
        k = ~np.isnan(p)
        idx = np.digitize(p[k], bins) - 1
        xs, ys = [], []
        for b in range(10):
            sel = idx == b
            if sel.sum() >= 15:
                xs.append(p[k][sel].mean())
                ys.append(y[k][sel].mean())
        ax.plot(xs, ys, marker="o", color=col, label=label)
    ax.plot([0, 1], [0, 1], color="black", lw=1, ls="--")
    ax.set_xlabel("predicted P(team A wins match)")
    ax.set_ylabel("realised win rate")
    ax.set_title("Calibration")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "series_study.png"), dpi=130)


if __name__ == "__main__":
    main()
