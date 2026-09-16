import math

import pytest

from esports_arb.series.lp import Quote, find_arbitrage, solve
from esports_arb.series.model import implied_map_prob, match_prob, sweep_prob
from esports_arb.series.states import Contract, series_states

M_A = Contract("match", "A")
W1, W2, W3 = (Contract("map", "A", i) for i in (1, 2, 3))
H_A = Contract("handicap", "A", line=1.5)
OVER = Contract("total", line=2.5, side="over")


def test_state_enumeration():
    assert series_states(1) == (("A",), ("B",))
    assert set(series_states(3)) == {("A", "A"), ("B", "B"), ("A", "B", "A"), ("A", "B", "B"),
                                     ("B", "A", "A"), ("B", "A", "B")}
    assert len(series_states(5)) == 20


def test_payoffs_and_complements():
    s = ("A", "A")
    assert M_A.payoff(s) == 1 and H_A.payoff(s) == 1 and OVER.payoff(s) == 0
    assert W3.payoff(s) == 0.5 and W3.complement().payoff(s) == 0.5    # unplayed map 3 -> 50-50
    assert H_A.complement().payoff(("A", "B", "A")) == 1                # B +1.5 covers a 2-1 loss
    assert H_A.complement().payoff(s) == 0
    assert OVER.complement().payoff(s) == 1
    for st in series_states(3):
        for c in (M_A, W1, W2, W3, H_A, OVER):
            assert c.payoff(st) + c.complement().payoff(st) == pytest.approx(1)


def test_exact_replication_identities():
    # match = (W1 + W2)/2 + W3 - 1/2  and  Over + 2*H_A = W1 + W2 (Polymarket unplayed rule)
    for s in series_states(3):
        assert M_A.payoff(s) == pytest.approx((W1.payoff(s) + W2.payoff(s)) / 2 + W3.payoff(s) - 0.5)
        assert OVER.payoff(s) + 2 * H_A.payoff(s) == pytest.approx(W1.payoff(s) + W2.payoff(s))


def test_independence_model():
    assert match_prob([0.6], 3) == pytest.approx(0.6 ** 2 * (3 - 2 * 0.6))
    assert match_prob([0.5], 5) == pytest.approx(0.5)
    assert implied_map_prob(match_prob([0.63], 3)) == pytest.approx(0.63, abs=1e-6)
    assert sweep_prob([0.7, 0.6]) == pytest.approx(0.42)
    # favourite is more likely to win a longer series
    assert match_prob([0.6], 5) > match_prob([0.6], 3) > 0.6


def q(contract, ask, size=100, **kw):
    return Quote(contract, ask, size, kw.pop("venue", "polymarket"), kw.pop("instrument", contract.describe()),
                 label=contract.describe(), **kw)


def full_book(m, h, o, w1, w2, spread=0.0):
    """Both sides of every market around given 'fair' prices."""
    out = []
    for c, p in ((M_A, m), (H_A, h), (OVER, o), (W1, w1), (W2, w2)):
        out += [q(c, round(p + spread, 4)), q(c.complement(), round(1 - p + spread, 4))]
    return out


def test_consistent_prices_have_no_arb():
    p = 0.6
    m, h = match_prob([p]), p * p
    o = 2 * p * (1 - p)
    opt, _, _ = solve(full_book(m, h, o, p, p, spread=0.01), 3)
    assert opt >= 1 - 1e-9
    assert find_arbitrage(full_book(m, h, o, p, p, spread=0.01), 3) is None


def test_match_below_handicap_is_arb():
    # 'A 2-0' priced above 'A wins' is impossible: buy A-match + B+1.5
    quotes = [q(M_A, 0.40), q(H_A.complement(), 0.55)]
    arb = find_arbitrage(quotes, 3)
    assert arb is not None
    assert arb.cost_per_unit == pytest.approx(0.95)
    assert arb.min_payoff == pytest.approx(100) and arb.profit == pytest.approx(5)


def test_lp_finds_replication_arb_across_many_legs():
    # over + 2*handicap cheaper than map1 + map2 (identity O + 2H = W1 + W2)
    quotes = [q(OVER, 0.40), q(H_A, 0.10), q(W1.complement(), 0.35), q(W2.complement(), 0.35)]
    arb = find_arbitrage(quotes, 3)
    assert arb is not None
    weights = {leg[0].label: leg[1] for leg in arb.legs}
    assert weights["A -1.5 maps"] == pytest.approx(1.0)       # 2 x H_A per $2 of payout
    assert arb.cost_per_unit == pytest.approx((0.40 + 2 * 0.10 + 0.35 + 0.35) / 2)
    assert all(v >= arb.units - 1e-9 for v in arb.payoffs.values())


def test_fees_can_remove_the_arb():
    quotes = [q(M_A, 0.49, fee_model="polymarket", fee_rate=0.05),
              q(H_A.complement(), 0.50, fee_model="polymarket", fee_rate=0.05)]
    assert find_arbitrage(quotes, 3) is None


def test_size_limited_by_thinnest_leg_and_rounding_up():
    quotes = [q(M_A, 0.40, size=7), q(H_A.complement(), 0.55, size=100)]
    arb = find_arbitrage(quotes, 3)
    assert arb.units == 7
    assert [leg[2] for leg in arb.legs] == [7, 7]


def test_unknown_unplayed_payoff_is_worst_case_for_buyer():
    k3 = Contract("map", "A", 3, unplayed=None, void=None)
    assert k3.payoff(("A", "A")) == 0.0
    base = [q(M_A.complement(), 0.5), q(W1, 0.5), q(W2, 0.5)]
    # Polymarket map 3 (unplayed -> 50-50): B-match + W1/2 + W2/2 + W3 pays exactly 1.5
    opt_pm, _, _ = solve(base + [q(W3, 0.2)], 3)
    assert opt_pm == pytest.approx(0.8)
    # same prices, but a map-3 contract whose unplayed settlement is unknown: no arbitrage
    opt_k, _, _ = solve(base + [q(k3, 0.2, venue="kalshi")], 3)
    assert opt_k >= 1


def test_void_warning():
    quotes = [q(M_A, 0.40), q(Contract("handicap", "B", line=-1.5, void=None), 0.55, venue="kalshi")]
    arb = find_arbitrage(quotes, 3)
    assert arb.void_payoff is None
    assert any("fair value" in w for w in arb.warnings)


def test_depth_walk_picks_profit_maximising_size():
    # second level of the A-match ladder is too expensive to be worth taking
    qa = q(M_A, 0.40, size=10)
    qa.ladder = [(0.40, 10), (0.46, 100)]
    qb = q(H_A.complement(), 0.55, size=1000)
    arb = find_arbitrage([qa, qb], 3)
    assert arb.units == 10 and arb.profit == pytest.approx(0.5)
    # a cheaper second level is taken (0.43 + 0.55 < 1)
    qa.ladder = [(0.40, 10), (0.43, 20)]
    arb = find_arbitrage([qa, qb], 3)
    assert arb.units == 30
    assert arb.profit == pytest.approx(10 * 0.05 + 20 * 0.02)


def test_bankroll_caps_size():
    arb = find_arbitrage([q(M_A, 0.40, size=1000), q(H_A.complement(), 0.55, size=1000)], 3, max_cost=95)
    assert arb.units == 100 and arb.cost == pytest.approx(95)


# ---------------------------------------------------------------- data parsing
import json as _json

from esports_arb.series.history import parse_event
from esports_arb.series.markets import _core, _side_team


def _pm_market(kind, question, outcomes, resolved, token_prefix, line=None):
    return {"sportsMarketType": kind, "question": question, "outcomes": _json.dumps(outcomes),
            "clobTokenIds": _json.dumps([token_prefix + "0", token_prefix + "1"]),
            "outcomePrices": _json.dumps(resolved), "line": line}


def test_parse_event_orients_maps_and_totals():
    e = {"title": "Counter-Strike: Alpha vs Beta (BO3) - Cup", "startTime": "2026-09-01T12:00:00Z",
         "markets": [
             _pm_market("moneyline", "Alpha vs Beta (BO3)", ["Alpha", "Beta"], ["1", "0"], "m"),
             _pm_market("child_moneyline", "Alpha vs Beta - Map 1 Winner", ["Beta", "Alpha"], ["0", "1"], "a"),
             _pm_market("child_moneyline", "Alpha vs Beta - Map 2 Winner", ["Alpha", "Beta"], ["1", "0"], "b"),
             _pm_market("totals", "Games Total: O/U 2.5", ["Under", "Over"], ["1", "0"], "t", line=2.5),
             _pm_market("map_handicap", "Map Handicap: BET (-1.5) vs Alpha (+1.5)", ["Beta", "Alpha"],
                        ["0", "1"], "h", line=-1.5),
         ]}
    r = parse_event(e)
    c = r["contracts"]
    assert c["match"]["result"] == 1.0
    assert c["map1"] == {"token": "a1", "result": 1.0}        # flipped to Alpha's token
    assert c["over"] == {"token": "t1", "result": 0.0}        # over token is the second outcome
    assert c["h_b"] == {"token": "h0", "result": 0.0}


def test_parse_event_skips_bo1_and_forfeits():
    e = {"title": "X vs Y (BO1)", "startTime": "2026-09-01T12:00:00Z", "markets": []}
    assert parse_event(e) is None
    e = {"title": "X vs Y (BO3)", "startTime": "2026-09-01T12:00:00Z", "markets": [
        _pm_market("moneyline", "q", ["X", "Y"], ["1", "0"], "m"),
        _pm_market("child_moneyline", "Map 1 Winner", ["X", "Y"], ["0.5", "0.5"], "a"),
        _pm_market("child_moneyline", "Map 2 Winner", ["X", "Y"], ["1", "0"], "b")]}
    assert parse_event(e) is None


def test_kalshi_ticker_core_and_team_side():
    assert _core("KXCS2MAP-26SEP161000EXRQUA-2") == ("26SEP161000EXRQUA", 2)
    assert _core("KXCS2GAME-26SEP161000EXRQUA") == ("26SEP161000EXRQUA", None)
    assert _side_team("QUAZAR", "ex-RUSTEC", "QUAZAR") == "B"
    assert _side_team("Unrelated", "ex-RUSTEC", "QUAZAR") is None
