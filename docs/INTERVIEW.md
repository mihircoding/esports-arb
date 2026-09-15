# Interview notes: questions this project should prepare you for

**Q: What is an arbitrage in a binary market?**
Two contracts that together pay exactly $1 in every state of the world (A wins
or B wins). If the total price you pay, fees included, is below $1, the
difference is locked in. Arbitrage means positive payoff in some states,
non-negative payoff in all states, and zero net cost after financing. Here the
"cost" is the capital you tie up.

**Q: Why do the venues disagree at all?**
The markets are segmented. Kalshi is US-regulated, Polymarket is crypto-native
and partly geofenced, and sportsbooks each run their own books. They have
different participants and different market makers. Esports gets little
attention (tier-2 CS2 at 5am), so quotes go stale. Moving capital between
venues is slow and costly, which is exactly what stops arbitrageurs from
closing the gap immediately.

**Q: Why do fees depend on P(1−P)?**
Both Kalshi and Polymarket charge `rate · P(1−P)`, which is proportional to
the variance of the $1 payoff. The consequence for arbitrage: a 50/50 match
needs about 3c of mispricing to clear fees, while a 90/10 match needs about
1.2c.

**Q: How do you size the trade?**
Both legs must be filled in the same size, so payout is equal in both states.
Walk the two ask ladders together and keep adding units while the marginal
all-in cost is below $1. Marginal cost never decreases, so the greedy stopping
point maximizes profit. Then round down to whole lots and re-price the fees
exactly (Kalshi rounds up to the cent).

**Q: Is it really risk-free?**
No. The main risks are:
- *Resolution basis*: a cancelled match resolves 50-50 on Polymarket, to
  "fair price" on Kalshi, and void on a sportsbook, so the hedge doesn't pay $1.
- *Execution*: legging risk, stale quotes, and minimum order sizes.
- *Operational*: capital lock-up, withdrawal fees, and account limits.

The scanner puts a number on the void scenario (`void_pnl`) and flags each of
these.

**Q: Why use a NO contract on Kalshi?**
In a two-way market, "NO on B wins" pays in exactly the same states as "YES on
A wins". The two are separate order books, and sometimes the NO route is
cheaper. That makes it a free extra leg to check.

**Q: What did the data show?**
See the README results. In short, gross arbs are fairly common, but most of
them vanish after fees. The ones that survive sit at the top of very thin
books, and the Kalshi–Polymarket basis mean-reverts slowly. Taken together,
this is a monitoring or market-making opportunity more than a money printer.

**Q: How would you extend it?**
- Stream data over WebSockets instead of polling, to cut latency.
- Act as a maker on the thin side and take on the thick side, which earns the
  maker rebate and avoids paying taker fees on both legs.
- Add map/game-level and totals markets, which need a correct mapping between
  series formats.
- Model Kalshi's "fair value" void payout properly.
- Size across more than two legs with a small LP (for example, three
  sportsbooks plus an exchange).
