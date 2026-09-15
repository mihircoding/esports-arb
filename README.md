# esports-arb

A fee-aware, depth-aware **arbitrage scanner for esports match-winner markets**.
It covers League of Legends, CS2, Valorant, Rainbow Six Siege, Rocket League,
Overwatch, Dota 2 and Call of Duty, on these venues:

| Venue | Data | Auth |
|---|---|---|
| **Kalshi** (CFTC-regulated exchange) | public REST: events + full order books | none |
| **Polymarket** (CLOB) | Gamma API for discovery, CLOB `/books` for depth | none |
| **Sportsbooks** (Pinnacle, bet365, Stake, ...) | [OddsPapi](https://oddspapi.io) aggregator | `ODDSPAPI_KEY` (optional) |
| **Any other book** (DraftKings, FanDuel, ...) | paste odds into a JSON file (`--manual`) | none |

For each match, the scanner:

1. Finds the same match on every venue, even when team names differ.
2. Converts every quote to a *price per $1 of payout*.
3. Checks every way of being long team A against every way of being long
   team B.
4. Sizes the trade by walking both order books together.
5. Subtracts each venue's real fee schedule.

It reports only trades that still make money after all of that, with warnings
for the risks that make an "arb" less than risk-free.

---

## How the arbitrage works (short version)

A match has two outcomes. $1 of "A wins" plus $1 of "B wins" **always pays exactly $1**.

```
profit per $1 = 1 − ask_A − ask_B − fee(ask_A) − fee(ask_B)
```

If that number is positive, buy both legs in equal size and the profit is
locked in, whoever wins.

* **Kalshi** gives two ways to be long A: `YES on "A wins"` or `NO on "B wins"`.
  The scanner checks both.
* **Fees** (both scale with `P(1−P)`, so they are largest for 50/50 matches):
  * Kalshi: `ceil_to_cent(0.07 · C · P · (1−P))`
  * Polymarket sports: `0.05 · C · P · (1−P)`, charged to takers only
  * Sportsbooks: no separate fee; the margin is already built into the odds
    (decimal odds `d` → price `1/d`)
* **Sizing:** `walk_books` merges the two ask ladders and keeps buying while
  the *marginal* pair still costs less than $1 after fees. That gives the
  profit-maximizing size. The result is then capped by `--bankroll` and
  rounded down to whole contracts.
* **Not actually risk-free:** if a match is cancelled, Kalshi resolves to
  "fair price", Polymarket resolves 50-50, and sportsbooks refund the stake.
  Each opportunity therefore shows an estimated `if cancelled` P&L, plus flags
  for fuzzy name matches, live matches, and sizes below a venue's minimum
  order.

The full derivation is in **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**.
Interview-style Q&A is in **[docs/INTERVIEW.md](docs/INTERVIEW.md)**.

## Architecture

```
esports_arb/
├── games.py            per-game ids on each venue (KXLOLGAME / series 10311 / sportId 18 ...)
├── models.py           Leg (one way to be long a team), Match, Opportunity
├── connectors/
│   ├── kalshi.py       events → 2 markets → 4 legs; asks rebuilt from opposite-side bids
│   ├── polymarket.py   moneyline markets + batched CLOB order books
│   ├── oddspapi.py     sportsbook decimal odds → price legs
│   └── manual.py       pasted American/decimal odds
├── normalize.py        team-name folding, aliases, roster-aware fuzzy match
├── matcher.py          cluster the same match across venues (names + ±6h start window)
├── fees.py / odds.py   fee schedules; odds conversion; multiplicative & Shin de-vig
├── arb.py              top-of-book edge, depth walk, exact fees, cancelled-match P&L
├── scanner.py          fetch → cluster → cheap screen → load depth → evaluate all leg pairs
└── cli.py              scan / near-misses / record
scripts/analyze.py      research on recorded snapshots (edge distribution, basis, AR(1) half-life)
```

## Quick start

```bash
pip install -r requirements.txt            # just `requests`
python -m esports_arb scan                  # all games, Kalshi + Polymarket (+ books if key set)
python -m esports_arb scan -g lol,r6,rl,ow --min-edge 0.005 --bankroll 250 --json arbs.json
python -m esports_arb near-misses --limit 20   # closest cross-venue pairs, even if edge < 0

export ODDSPAPI_KEY=...                     # optional: add sportsbooks
python -m esports_arb scan --manual data/manual_odds.example.json   # or paste odds

# research
python -m esports_arb record --interval 60 --iterations 60 --out data/snapshots.csv
pip install -r requirements-dev.txt
python scripts/analyze.py data/snapshots.csv --out docs
pytest -q
```

Example output (live, 2026-09-15):

```
177 matches, 87 listed on 2+ venues, 2 opportunities

#1 [cs2] M80 vs NIP  (start Sep 17 09:00Z)
    leg A: long M80: kalshi NO on "NIP wins" @ 0.450   (≈ decimal 2.22)
    leg B: long NIP: polymarket "NIP" token @ 0.510   (≈ decimal 1.96)
    top-of-book edge 1.02% | size 1 | cost $0.99 (fees $0.03) | profit $0.01 | ROI 0.76% | ann. 140%
    if cancelled: est. P&L $-0.04
    ! size 1 is below venue minimum order (5)
    ! void-rule mismatch: kalshi='... fair market price' vs polymarket='... resolves 50-50'
```

## Results: 44 minutes of live Kalshi and Polymarket quotes

`esports_arb record` saved the best asks every 60s for 44 minutes on
2026-09-15 (15:00–15:44 UTC): 45 snapshots, 23,347 quotes, **87 matches listed
on both venues** (CS2, LoL, R6, Valorant, Dota 2, Rocket League; no Overwatch
or CoD matches were open). The raw data is in `data/snapshots.csv.gz`, and
`python scripts/analyze.py data/snapshots.csv.gz` reproduces everything below.

![edge distribution](docs/edge_distribution.png)

| Metric | Value |
|---|---|
| Match-snapshots where the best cross-venue pair costs < $1 (**gross arb**) | **12.9%** (28 of 87 matches at some point) |
| ... still < $1 **after taker fees** (**net arb**) | **2.8%** (7 matches: 6 CS2, 1 Rocket League) |
| Net edge percentiles | p50 −378bp · p95 −32bp · p99 +348bp |
| Median net edge when positive | 102bp |
| Median top-of-book size when positive | **1 contract** |
| How long a net arb lasts (consecutive snapshots) | median 3 min, mean 10.9, max 45 (the whole window) |
| Kalshi − Polymarket basis (de-vigged P) | mean −0.55pp, mean \|basis\| 1.89pp, p95 7.73pp |
| Median overround (ask A + ask B − 1) | Kalshi 3.0% · Polymarket 2.0% |
| Basis AR(1) | φ = 0.957 → half-life ≈ 16 min |

What this says:

1. **The two venues agree closely on who wins** (right-hand plot), but they
   regularly differ by several points on smaller matches. Those differences
   fade slowly, with a half-life of about 16 minutes rather than seconds. That
   is what you'd expect from thin, slow-moving markets.
2. **Fees remove about 80% of the apparent arbs.** 12.9% of match-snapshots
   show a gross arb, but only 2.8% survive fees. The gaps that fees wipe out
   are usually a single cent (median gross edge 1c). That is less than the
   2–3c the two venues charge together on mid-priced legs, where `P(1−P)` is
   largest.
3. **The arbs that survive can't be traded at meaningful size.** The typical
   one has **one contract** at the top of the book, which is below
   Polymarket's 5-share minimum. The biggest persistent edge (Rocket League,
   about 3.5%, shortly before the match started) had 0.08 contracts on offer.
   A taker-taker arb bot here earns cents.
4. **The Kalshi NO leg matters.** Most net-positive pairs used `NO on
   "opponent wins"` rather than `YES on team`. A scanner that only checks YES
   contracts would miss them.
5. **Takeaway:** the durable opportunity is **market making**, not taking.
   Quote passively on the thin venue near the other venue's price, and hedge
   on the other venue when filled. That avoids one taker fee (Polymarket even
   pays makers a rebate) and captures the slow-reverting basis. It is the
   natural next step for this project.


## Limitations and next steps

* Polling REST, not streaming. The next step is Kalshi and Polymarket
  WebSockets, which would cut latency and make persistence measurements much
  finer-grained.
* Only taker-taker trades are modeled. Posting a maker order on the thin venue
  (Polymarket pays makers a rebate) and taking on the other would capture far
  more of the observed dislocations.
* Match-winner markets only. Map/game winners and totals need a careful
  mapping between best-of formats.
* Sportsbook sizes are assumed (`--book-limit`), because books don't publish
  depth. The OddsPapi connector is covered by unit tests on recorded payloads
  but needs your own key to run live.
* Not investment advice. Check each venue's terms and your jurisdiction.
  Polymarket's international exchange is geofenced for US users.
