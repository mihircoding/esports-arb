import math

import pytest

from esports_arb.fees import kalshi_fee, marginal_fee_per_unit, polymarket_fee
from esports_arb.odds import (american_to_decimal, decimal_to_price, devig_multiplicative,
                              devig_shin, overround)


def test_kalshi_fee_rounds_up_to_cent():
    # 0.07 * 100 * 0.5 * 0.5 = 1.75 exactly
    assert kalshi_fee(100, 0.50) == pytest.approx(1.75)
    # 0.07 * 1 * 0.45 * 0.55 = 0.017325 -> 0.02
    assert kalshi_fee(1, 0.45) == pytest.approx(0.02)
    # exact cents must not be bumped by float noise: 0.07*10*.5*.5 = .175 -> .18
    assert kalshi_fee(10, 0.50) == pytest.approx(0.18)
    assert kalshi_fee(4, 0.50) == pytest.approx(0.07)


def test_polymarket_fee_is_parabolic():
    assert polymarket_fee(100, 0.5) == pytest.approx(1.25)
    assert polymarket_fee(100, 0.9) == pytest.approx(0.45)
    assert polymarket_fee(100, 0.1) == pytest.approx(polymarket_fee(100, 0.9))


def test_fee_peaks_at_half():
    grid = [i / 100 for i in range(1, 100)]
    fees = [marginal_fee_per_unit("kalshi", p) for p in grid]
    assert grid[fees.index(max(fees))] == pytest.approx(0.5)


def test_american_and_decimal():
    assert american_to_decimal(+150) == pytest.approx(2.5)
    assert american_to_decimal(-200) == pytest.approx(1.5)
    assert decimal_to_price(2.5) == pytest.approx(0.4)
    with pytest.raises(ValueError):
        decimal_to_price(1.0)


def test_overround_and_devig():
    prices = [1 / 1.8, 1 / 2.1]
    assert overround(prices) > 0
    assert sum(devig_multiplicative(prices)) == pytest.approx(1)
    shin = devig_shin(prices)
    assert sum(shin) == pytest.approx(1)
    # Shin shades the longshot more than multiplicative de-vig does
    assert shin[1] < devig_multiplicative(prices)[1]
