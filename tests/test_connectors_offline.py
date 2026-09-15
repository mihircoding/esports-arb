"""Parser tests on recorded API payloads (no network)."""
import json
from unittest import mock

from esports_arb.connectors.kalshi import KalshiConnector
from esports_arb.connectors.oddspapi import OddsPapiConnector
from esports_arb.connectors.polymarket import PolymarketConnector, parse_book
from esports_arb.scanner import scan

KALSHI_EVENT = {
    "event_ticker": "KXRLGAME-26SEP151200BIGVP", "series_ticker": "KXRLGAME",
    "title": "Bigodes vs. Virtus.pro",
    "markets": [
        {"ticker": "KXRLGAME-26SEP151200BIGVP-BIG", "status": "active", "yes_sub_title": "Bigodes",
         "title": "x", "yes_ask_dollars": "0.3000", "yes_ask_size_fp": "50", "yes_bid_dollars": "0.2800",
         "yes_bid_size_fp": "40"},
        {"ticker": "KXRLGAME-26SEP151200BIGVP-VP", "status": "active", "yes_sub_title": "Virtus.pro",
         "title": "y", "yes_ask_dollars": "0.7200", "yes_ask_size_fp": "30", "yes_bid_dollars": "0.6900",
         "yes_bid_size_fp": "20"},
    ],
}


def pm_event(prices):
    return [{
        "title": "Rocket League: Virtus.pro vs Bigodes (BO5)", "slug": "rl-vp-bigode",
        "startTime": "2026-09-15T16:00:00Z",
        "markets": [{
            "sportsMarketType": "moneyline", "closed": False, "acceptingOrders": True,
            "outcomes": json.dumps(["Virtus.pro", "Bigodes"]), "clobTokenIds": json.dumps(["t_vp", "t_big"]),
            "gameStartTime": "2026-09-15 16:00:00+00", "feesEnabled": True,
            "feeSchedule": {"rate": 0.05}, "orderMinSize": 5,
        }],
    }]


def test_kalshi_event_parsing_builds_four_legs():
    match = KalshiConnector(depth=False)._parse_event("rl", KALSHI_EVENT)
    assert (match.team_a, match.team_b) == ("Bigodes", "Virtus.pro")
    yes_a, no_b = match.legs_a
    assert yes_a.best == 0.30 and yes_a.side == "YES"
    assert no_b.best == 0.31 and no_b.side == "NO" and no_b.best_size == 20
    assert match.start.hour == 16  # 12:00 EDT


def test_polymarket_book_sorted_ascending():
    book = {"asks": [{"price": "0.99", "size": "5"}, {"price": "0.61", "size": "10"}, {"price": "0.7", "size": "0"}]}
    assert parse_book(book) == [(0.61, 10.0), (0.99, 5.0)]


def test_end_to_end_scan_finds_cross_venue_arb():
    books = [{"asset_id": "t_vp", "asks": [{"price": "0.60", "size": "100"}]},
             {"asset_id": "t_big", "asks": [{"price": "0.45", "size": "100"}]}]
    with mock.patch("esports_arb.connectors.kalshi.get_json",
                    return_value={"events": [KALSHI_EVENT], "cursor": ""}), \
         mock.patch("esports_arb.connectors.polymarket.get_json", side_effect=[pm_event(None), []]), \
         mock.patch("esports_arb.connectors.polymarket.post_json", return_value=books):
        kal = KalshiConnector(depth=False)
        pm = PolymarketConnector(lookback_hours=1e6)
        matches = kal.fetch("rl") + pm.fetch("rl")
        opps, clusters = scan([kal, pm], ["rl"], matches=matches)
    assert len(clusters) == 1
    # Kalshi YES Bigodes 0.30 + Polymarket Virtus.pro 0.60 -> 10c gross edge before fees
    best = opps[0]
    assert {best.leg_a.venue, best.leg_b.venue} == {"kalshi", "polymarket"}
    assert best.profit > 0
    assert any("void-rule" in w for w in best.warnings)


def test_oddspapi_parser():
    fx = {"fixtureId": "f1", "participant1Name": "NAVI", "participant2Name": "M80",
          "startTime": "2026-09-16T12:00:00Z"}
    odds = {"bookmakerOdds": {
        "pinnacle": {"markets": {"171": {"outcomes": {"171": {"players": {"0": {"price": 1.25}}},
                                                     "172": {"players": {"0": {"price": 4.2}}}}}}},
        "polymarket": {"markets": {"171": {"outcomes": {"171": {"players": {"0": {"price": 1.3}}}}}}},
    }}
    m = OddsPapiConnector(api_key="x").parse_odds("cs2", fx, odds)
    assert [l.venue for l in m.legs_a] == ["book:pinnacle"]  # exchange slugs skipped
    assert m.legs_a[0].best == 0.8
    assert round(m.legs_b[0].best, 4) == round(1 / 4.2, 4)
