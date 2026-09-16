"""Series-format consistency: match winner vs map winners vs map handicap vs total maps.

Every best-of-N match ends in one of a small number of map sequences
(BO3: AA, ABA, ABB, BAA, BAB, BB). Each contract — on either venue — is a
payoff vector over those sequences. If some basket of contracts pays at
least $1 in *every* sequence but costs less than $1 after fees, it is an
arbitrage. `lp.find_arbitrage` searches for the cheapest such basket with a
linear program, so it finds combinations nobody would think to check by hand.
"""
