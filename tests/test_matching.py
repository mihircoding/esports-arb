from datetime import datetime, timedelta, timezone

from esports_arb.connectors.kalshi import ladder_from_opposite_bids, parse_ticker_start
from esports_arb.matcher import cluster_matches
from esports_arb.models import Leg, Match
from esports_arb.normalize import normalize, pair_score, similarity

T0 = datetime(2026, 9, 15, 16, tzinfo=timezone.utc)


def m(venue, a, b, start=T0, game="rl"):
    return Match(venue=venue, game=game, team_a=a, team_b=b, start=start, title=f"{a} vs {b}",
                 legs_a=[Leg(venue, a, [(0.5, 1)], f"{venue}-{a}")],
                 legs_b=[Leg(venue, b, [(0.5, 1)], f"{venue}-{b}")])


def test_normalize_strips_generic_tokens():
    assert normalize("FUT Esports") == normalize("FUT")
    assert normalize("Team Liquid") == "liquid"
    assert normalize("Natus Vincere") == normalize("NAVI")


def test_academy_and_female_rosters_are_distinct():
    assert similarity("Team Spirit", "Spirit Academy") < 0.8
    assert similarity("MIBR", "MIBR fe") < 0.8


def test_pair_score_detects_swapped_order():
    score, swapped = pair_score("Bigodes", "Virtus.pro", "Virtus.pro", "Bigodes")
    assert swapped and score == 1.0


def test_cluster_flips_orientation_and_shares_legs():
    k = m("kalshi", "Bigodes", "Virtus.pro")
    p = m("polymarket", "Virtus.pro", "Bigodes")
    clusters = cluster_matches([k, p])
    assert len(clusters) == 1
    c = clusters[0]
    assert c.members[1].team_a == "Bigodes"
    assert c.members[1].legs_a[0].team == "Bigodes"


def test_cluster_respects_time_window_and_game():
    k = m("kalshi", "G2", "Vitality")
    p_late = m("polymarket", "G2", "Vitality", start=T0 + timedelta(days=2))
    p_other_game = m("polymarket", "G2", "Vitality", game="cs2")
    assert len(cluster_matches([k, p_late])) == 2
    assert len(cluster_matches([k, p_other_game])) == 2


def test_same_venue_never_clusters():
    assert len(cluster_matches([m("kalshi", "A", "B"), m("kalshi", "A", "B")])) == 2


def test_kalshi_ticker_time_is_eastern():
    # 13:00 EDT == 17:00 UTC
    assert parse_ticker_start("KXRLGAME-26SEP151300R8TSM") == datetime(2026, 9, 15, 17, tzinfo=timezone.utc)
    assert parse_ticker_start("garbage") is None


def test_kalshi_ladder_mirrors_opposite_bids():
    asks = ladder_from_opposite_bids([["0.10", "5"], ["0.63", "100"], ["0.70", "0.00"]])
    assert asks == [(0.37, 100.0), (0.9, 5.0)]
