"""Real-time market data.

* Polymarket: public CLOB WebSocket (book snapshots + level updates).
* Kalshi: authenticated WebSocket (orderbook_snapshot/orderbook_delta + trades)
  when KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH are set, otherwise a batched
  REST poller (all tracked markets' top of book in one request per ~100 tickers).

Both feed one `BookStore`; `StreamHub` re-evaluates only the match whose book
changed and records arbitrage *episodes* with millisecond timestamps.
"""
