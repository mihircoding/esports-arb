import pytest

from esports_arb.alpha.model import from_match_price, price_series, seq_prob
from esports_arb.series.model import match_prob


def test_rho_zero_is_independent_maps():
    sp = price_series(0.6, 0.0, 3)
    assert sp.match == pytest.approx(match_prob([0.6], 3))
    assert sp.sweep_a == pytest.approx(0.36)
    assert sp.over[2.5] == pytest.approx(2 * 0.6 * 0.4)


def test_probabilities_sum_to_one_and_symmetry():
    for bo in (3, 5):
        sp = price_series(0.5, 0.2, bo)
        assert sp.match == pytest.approx(0.5)
        assert sp.played[1] == pytest.approx(1.0)
        assert sp.maps[1] == pytest.approx(0.5)


def test_correlation_raises_sweeps_and_lowers_overs():
    ind = price_series(0.6, 0.0, 3)
    cor = price_series(0.6, 0.15, 3)
    assert cor.sweep_a + cor.sweep_b > ind.sweep_a + ind.sweep_b
    assert cor.over[2.5] == pytest.approx(2 * 0.6 * 0.4 * (1 - 0.15))
    # beta-binomial moment: E[p^2] = mu^2 + rho mu (1-mu)
    assert seq_prob(0.6, 0.15, 2, 0) == pytest.approx(0.36 + 0.15 * 0.24)


def test_inversion_from_match_price():
    for rho in (0.0, 0.1, 0.3):
        for m in (0.2, 0.5, 0.83):
            sp = from_match_price(m, rho, 3)
            assert sp.match == pytest.approx(m, abs=1e-6)
    # correlation weakens the best-of amplification, so the same match price needs a
    # per-map probability closer to the match price itself
    assert 0.8 > from_match_price(0.8, 0.3).mu > from_match_price(0.8, 0.0).mu
    assert from_match_price(0.999, 0.1) is None


def test_bo5_totals_and_map3_fair():
    sp = price_series(0.55, 0.1, 5)
    assert set(sp.over) == {3.5, 4.5}
    assert sp.over[3.5] >= sp.over[4.5]
    assert sp.played[3] == pytest.approx(1.0) and sp.played[4] == pytest.approx(sp.over[3.5])
    sp3 = price_series(0.55, 0.1, 3)
    assert sp3.map_fair(3, 0.5) == pytest.approx(sp3.maps[3] + (1 - sp3.over[2.5]) * 0.5)
