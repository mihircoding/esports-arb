# Market making on Kalshi esports

## Why Kalshi is the maker venue

* **No maker fee on esports.** Kalshi's fee schedule lists the series that
  charge its 1.75% maker fee (NBA, NFL, NHL, CPI, ...). No esports series is
  on that list, and the esports series report `fee_type: quadratic` (taker
  fee only). A resting order that gets filled therefore costs nothing.
* **The venue is accessible from the US.** Polymarket's international
  exchange blocks US users. Polymarket US is a separate exchange with its own
  API. This project only *reads* Polymarket's public order books and uses
  them as a second opinion on fair value, and never trades there.
* **Kalshi's esports books are often wide.** Before a match, the median
  quoted spread is 3c, and a quarter of the time it is 7c or more (measured
  on 259k one-minute candles in the dataset). That leaves room to quote
  inside the spread.

## The strategy

The approach is reference-price market making with inventory skew, in the
style of Avellaneda–Stoikov (`esports_arb/mm/quoting.py`):

```
fair      = w · PolymarketMid + (1 − w) · KalshiMid
r         = fair − skew · exposure              # long team X → quote lower
bid, ask  = floor(r − h), ceil(r + h)          # 1c ticks
post-only: bid < Kalshi ask, ask > Kalshi bid
```

* Each event has two markets ("A wins", "B wins"). Both are quoted. Exposure
  is measured across both, because buying YES on B is the same as selling A.
* `ask` is sent to Kalshi as **buy NO at 1 − ask**. Kalshi nets YES and NO
  positions in the same market.
* Quoting runs from `start_hours` before the match to `stop_before_min`
  before it. There is no in-play quoting.
* Positions are held to settlement unless an opposite-side fill flattens them.

## Backtest (`esports_arb/mm/backtest.py`)

The backtest replays history minute by minute using real data:

* the Kalshi public **trade tape** (price, size, and whether the taker bought
  or sold);
* Kalshi 1-minute **top-of-book** candles, used for the post-only clamp;
* Polymarket 1-minute **price history**;
* the real **settlement result**.

**Fill rule:** a taker selling YES at price *p* fills our bid *b* if
*b > p*, because the taker would have hit us first. If *b = p*, we get
`queue_frac` of that print. The rule for the ask side mirrors this. Quotes use
information that is `latency_s` old (60s by default), which guards against the
Polymarket history timestamps leaking future prices.

**P&L decomposition for every fill:**

```
pnl = capture + drift − fees
capture = ±qty · (fair_at_fill − fill_price)     # the spread we earned vs fair value
drift   = ±qty · (settlement − fair_at_fill)     # adverse selection + luck
```

**Method:**
1. Build the dataset: `python -m esports_arb mm-data --days 21` gives 1,177
   settled matches that appear on both venues.
2. Sort the matches by start time and split them in half.
3. Grid-search half-spread × fair-value weight × stop time on the **first
   half only**.
4. Report the chosen configuration once on the **second half**.

`scripts/mm_research.py` reproduces everything, and the full output is in
`docs/mm_results.txt`.

## Results (quotes of 5 contracts, 25-contract cap per event)

| | Train (588 matches, Aug 25 – Sep 6) | **Test (589, Sep 6 – 15, out-of-sample)** |
|---|---|---|
| Chosen config | h = 8c, w = 0.5, stop 120 min before start | same |
| P&L | $83.40 (t = 1.63) | **$70.11 (t = 1.12)** |
| Matches traded | 119 | 192 |
| Contracts | 1,310 | 2,501 |
| P&L per contract | 6.4c | **2.8c** |
| Spread capture / drift | +$112 / −$29 | +$212 / −$142 |
| Same settings, fair = Kalshi mid only (w = 0) | — | $31.63 (t = 0.57) |
| Same settings, fair = Polymarket only (w = 1) | — | $29.80 (t = 0.48) |

**Adverse selection by time to start** (test set, same settings but quoting
until 5 minutes before start; per contract):

| Minutes before start | Capture | Drift | Net |
|---|---|---|---|
| 360–720 | +8.5c | −5.6c | **+2.9c** |
| 120–360 | +8.4c | −5.7c | **+2.8c** |
| 30–120 | +8.4c | −14.6c | **−6.2c** |
| 0–30 | +8.5c | −10.3c | **−1.8c** |

**Robustness** (test set, chosen settings):

| Scenario | P&L |
|---|---|
| queue share 0% | $67 |
| queue share 50% | $65 |
| latency 3 min | $45 |
| latency 10 min | $6 |
| if Kalshi added its 1.75% maker fee | $55 |

## What the data says

1. **Quoting in the last two hours before a match loses money.** In that
   window, drift is roughly twice the spread earned. Takers there know
   something: lineups, roster swaps, late news. Simply not quoting after
   T−120 min was the single biggest improvement.
2. **Polymarket is not a better oracle than Kalshi.** Five minutes before
   start, the Brier scores are the same (0.1902 vs 0.1896). The *blend* of
   the two worked best out-of-sample. That fits the idea that averaging two
   noisy estimates cuts the noise, not that one venue leads the other.
3. **The edge is small and not statistically proven.** Out-of-sample it made
   $70 over 10 days with 5-lot quotes, about $0.12 per match. Taken at face
   value that is roughly $200 a month. But with t = 1.1, the true edge could
   easily be zero. Larger size won't scale linearly, because the trade tape
   is thin (2,501 contracts filled in the whole test period). Per-game
   results are noisy: LoL and Dota 2 lost money on the test set.
4. **Speed matters.** Moving from 60s to 10-minute latency wipes out the
   edge, so run the live loop with a short `--interval`.

## Live paper trading (2026-09-15)

| Run | Settings | Duration | Events tracked | Fills | Result |
|---|---|---|---|---|---|
| 1 | early defaults: h = 3c, w = 1, stop 5 min | 47 min | 68 | 5 fills, 15 contracts (sold Secret at 48–49c vs fair 45c, R6) | +$0.52 captured vs fair, +$0.63 marked; settlement pending |
| 2 | chosen: h = 8c, w = 0.5, stop 120 min | 60 min | 71 (up to 32 live quotes) | 0 | $0 |

Run 2's lack of fills is what the backtest predicts. With 8c-wide quotes,
only about a third of matches ever trade, and fills cluster in bursts. Judge
the strategy over days of paper trading, not hours.

## Running it

```bash
# paper trading: live Kalshi + Polymarket data, simulated fills from the real Kalshi tape
python -m esports_arb -v mm-paper --minutes 120

# real money (your own Kalshi API key; small limits; post-only orders)
export KALSHI_KEY_ID=...
export KALSHI_PRIVATE_KEY_PATH=~/.kalshi/key.pem
python -m esports_arb -v mm-live --size 5 --max-exposure 25 --max-total 100 --max-loss 25 --confirm-live
touch STOP        # kill switch: cancels every resting order and exits
```

Safety rails:

* post-only orders only;
* an exposure cap per event and in total (after the cap, only risk-reducing
  quotes are placed);
* a max-loss halt, with P&L marked to fair value;
* no quoting inside the stop window;
* a `STOP` file kill switch;
* all resting orders cancelled on exit, on Ctrl-C, or on any error.

**Before trading real money:**

* paper-trade for several days first;
* start with the minimum size;
* check that Kalshi's esports fee schedule still has no maker fee;
* confirm Kalshi is available in your state.

This is research code, not financial advice.
