"""Reference-price market making on Kalshi esports markets.

Kalshi is the *maker* venue: esports series are not on Kalshi's maker-fee
list, so resting orders that get filled pay no fee. Polymarket's (deeper,
global) order book is used only as a *fair-value signal* — reading its
public data needs no account. Quotes are placed around that fair value and
inventory is either flattened by opposite-side fills or held to settlement.
"""
