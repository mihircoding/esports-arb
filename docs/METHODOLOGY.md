# Methodology

## 1. Why a match-winner market can be arbitraged

A best-of-N esports match has exactly two outcomes: team A wins or team B wins
(match-winner markets have no draw). A contract that pays $1 if A wins and a
contract that pays $1 if B wins together pay **exactly $1 no matter what happens**.
If you can buy both for less than $1 in total, including fees, the
difference is profit with no exposure to who wins.

Different venues price the same match separately, with different users,
different market makers and different amounts of attention. Esports markets are
small and lightly watched, so the prices often disagree for a while.

## 2. Putting every venue on one scale

| Venue | What you buy | Price per $1 payout | Fee |
|---|---|---|---|
| Kalshi | YES on "A wins" | `yes_ask(A)` | `ceil_cent(0.07·C·P·(1−P))` |
| Kalshi | NO on "B wins" (also pays if A wins) | `1 − yes_bid(B)` | same |
| Polymarket | the "A" outcome token | CLOB ask | `C·0.05·P·(1−P)` (sports, taker only) |
| Sportsbook | a bet on A at decimal odds `d` | `1/d` | none (the margin is already in `d`) |

Kalshi's order book stores bids only. A YES ask at `p` is the same order as a
NO bid at `1 − p`, so `connectors/kalshi.py` rebuilds each ask ladder from the
opposite side's bids.

Both exchange fees scale with `P(1−P)`, which is the variance of a
Bernoulli(P) payoff. Fees are largest at 50c and shrink toward 0 at the
extremes. So a pair of 50c legs has to be about 3c mispriced before it clears
fees, while a 10c/88c pair only needs about 1.2c.

## 3. Detecting an arb (top of book)

    edge = 1 − (a + b + f_K(a) + f_P(b))

Any `edge > 0` is a candidate. The scanner checks every pair of legs in a
match cluster (for example Kalshi-YES + Polymarket, or Kalshi-NO + a
sportsbook). It skips the pair YES + NO on the same Kalshi market, because that
pair always costs at least $1.

## 4. Sizing with order-book depth

Both legs must be filled in the same size so that the payout is identical in
both outcomes. `arb.walk_books` walks the two ask ladders together (like
merging two sorted lists). At each step it takes `min(remaining at level i of
A, remaining at level j of B)` and stops as soon as the marginal unit's all-in
cost reaches `1 − min_edge`. Prices only get worse as you go deeper, so
marginal cost never decreases, and this greedy walk returns the size that
maximizes profit. The walk also stops early when a bankroll cap
(`--bankroll`) is reached.

Size is then rounded down to whole contracts, and exact fees are recomputed
(Kalshi rounds up to the cent on each fill). A 1-lot Kalshi arb with a 1c
gross edge usually disappears at this step. Opportunities below a venue's
minimum order size (Polymarket: usually 5 shares) are flagged.

## 5. Matching the same match across venues

Team names differ between venues ("BET-M 33" vs "33", "FUT Esports" vs
"FUT"). `normalize.py` handles this in four steps:

1. Unicode and punctuation folding, and removal of generic tokens (`team`, `esports`, `gaming`, ...).
2. A small alias table (`navi` → `natus vincere`, ...).
3. A token-subset rule, which scores 0.9 **unless** the extra token marks a
   different roster (`academy`, `fe`, `junior`, ...). "Spirit" and "Spirit
   Academy" are different teams, and treating them as the same would be a
   disastrous false arb.
4. A `difflib` similarity fallback.

`matcher.cluster_matches` groups listings from different venues when both team
names score ≥ 0.8 (in either order) and the start times are within 6 hours.
It then flips the orientation so that "team A" means the same team on every
venue. Kalshi encodes the start time in the event ticker in US Eastern time
(`KXRLGAME-26SEP151300…` = 13:00 ET).

## 6. What is *not* risk-free

* **Settlement-rule mismatch.** If a match is cancelled or ends in a walkover
  before play starts, Kalshi resolves to "fair market price", Polymarket
  resolves 50-50, and sportsbooks void the bet and refund the stake. The
  hedge can then pay less than $1. The scanner reports an estimated
  `void_pnl`, using 0.50 for Polymarket and the purchase price as a stand-in
  for Kalshi's fair value and for a sportsbook refund.
* **Legging risk.** The two legs are on different venues and cannot be filled
  atomically. Fill the thinner, faster-moving leg first.
* **Stale or live quotes.** Prices move quickly during a live match, and
  sportsbooks may reject or re-price a bet.
* **Capital lock-up.** Money sits on both venues until the match settles. The
  scanner reports an annualized ROI assuming settlement about 6h after start.
* **Account limits.** Sportsbooks limit accounts that bet arbs. Polymarket
  restricts US users on its international exchange.

## 7. Research: `scripts/analyze.py`

`esports_arb record` saves the best asks on every linked venue each minute.
The analysis script then measures:

* the share of match-snapshots with a gross arb and with a net (after-fee) arb,
  plus the distribution of the net edge;
* the **basis**: Kalshi's minus Polymarket's de-vigged P(team A);
* mean reversion of the basis, estimated with a pooled AR(1)
  `b_t = φ·b_{t−1} + ε`, where the half-life is `ln 0.5 / ln φ`;
* how long arbs persist, measured as run lengths of consecutive net-positive
  snapshots.
