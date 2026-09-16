import base64
import math
from unittest import mock

import pytest

from esports_arb.mm.backtest import Series, brier, run_match, summarize
from esports_arb.mm.book import EventBook, Fill
from esports_arb.mm.live import PaperBroker, pm_mid
from esports_arb.mm.quoting import QuoteParams, blend_fair, ceil_tick, floor_tick, kalshi_fee, make_quotes

P = QuoteParams(half_spread=0.03, size=5, max_exposure=10, skew=0.001, min_price=0.05, max_price=0.95)


# ---------------------------------------------------------------- quoting
def test_ticks():
    assert floor_tick(0.4299999) == 0.42
    assert floor_tick(0.43) == 0.43
    assert ceil_tick(0.4300001) == 0.44


def test_symmetric_quotes_when_flat():
    assert make_quotes(0.50, 0, P) == (0.47, 0.53)


def test_inventory_skew_lowers_quotes_when_long():
    p = QuoteParams(half_spread=0.03, max_exposure=100, skew=0.002)
    bid, ask = make_quotes(0.50, 10, p)            # reservation 0.48
    assert (bid, ask) == (0.45, 0.51)
    assert make_quotes(0.50, -10, p) == (0.49, 0.55)


def test_exposure_limit_removes_risk_adding_side():
    assert make_quotes(0.5, 10, P)[0] is None
    assert make_quotes(0.5, -10, P)[1] is None


def test_post_only_clamp():
    bid, ask = make_quotes(0.50, 0, P, k_bid=0.52, k_ask=0.46)
    assert bid <= 0.45 and ask >= 0.53


def test_no_quotes_outside_price_band_or_without_fair():
    assert make_quotes(0.97, 0, P) == (None, None)
    assert make_quotes(None, 0, P) == (None, None)


def test_blend_fair():
    assert blend_fair(0.60, 0.40, 0.50, 1.0) == 0.60
    assert blend_fair(0.60, 0.40, 0.50, 0.0) == pytest.approx(0.45)
    assert blend_fair(0.60, 0.40, 0.50, 0.5) == pytest.approx(0.525)
    assert blend_fair(None, 0.40, 0.50, 1.0) is None


def test_maker_fee_default_zero_and_rounding():
    assert kalshi_fee(0.0, 100, 0.5) == 0
    assert kalshi_fee(0.0175, 100, 0.5) == pytest.approx(0.44)


# ---------------------------------------------------------------- book
def test_book_settlement_and_decomposition():
    b = EventBook()
    b.fill(Fill(0, 0, "buy", 0.40, 10, 0.0, 0.45))   # long A: +0.05 edge each
    b.fill(Fill(1, 1, "buy", 0.50, 10, 0.0, 0.55))   # long B: +0.05 edge each -> fully hedged
    assert b.exposure() == 0
    assert b.settle(True) == pytest.approx(10 - 9)
    assert b.settle(False) == pytest.approx(10 - 9)
    d = b.decomposition(True)
    assert d["capture"] == pytest.approx(1.0)
    assert d["capture"] + d["drift"] + d["fees"] == pytest.approx(b.settle(True))


def test_short_yes_accounting():
    b = EventBook()
    b.fill(Fill(0, 0, "sell", 0.70, 10, 0.0, 0.65))
    assert b.exposure() == -10
    assert b.settle(False) == pytest.approx(7.0)   # keep premium
    assert b.settle(True) == pytest.approx(-3.0)   # pay out $1 each
    assert b.capital() == pytest.approx(3.0)


# ---------------------------------------------------------------- backtest
def _rec(trades_a, a_won=True, pm=0.5):
    start = 100_000.0
    return {"event": "E", "game": "cs2", "start": start, "a_won": a_won,
            "pm": [[start - 50_000, pm]],
            "candles": [[[start - 50_000, 0.40, 0.60]], [[start - 50_000, 0.40, 0.60]]],
            "trades": [trades_a, []]}


def test_trade_through_fills_full_size_at_our_price():
    start = 100_000.0
    rec = _rec([[start - 1000, 0.45, 100, "no"]])   # taker sells YES at 0.45 < our 0.47 bid
    r = run_match(rec, P, latency_s=0)
    assert r.fills == 1 and r.volume == 5
    assert r.pnl == pytest.approx(5 * (1 - 0.47))
    assert r.capture == pytest.approx(5 * 0.03)


def test_at_touch_uses_queue_fraction_and_ignores_wrong_side():
    start = 100_000.0
    rec = _rec([[start - 1000, 0.47, 8, "no"],        # at our bid: floor(8*0.25)=2
                [start - 900, 0.47, 8, "yes"]])       # taker BUYING at 0.47 can't hit our bid
    r = run_match(rec, P, latency_s=0)
    assert r.volume == 2


def test_no_fills_after_stop_time_or_before_window():
    start = 100_000.0
    rec = _rec([[start - 60, 0.30, 100, "no"],               # inside stop_before window (5 min)
                [start - 13 * 3600, 0.30, 100, "no"]])      # before quoting starts
    assert run_match(rec, P).fills == 0


def test_summary_and_brier():
    start = 100_000.0
    res = [run_match(_rec([[start - 1000, 0.45, 100, "no"]]), P, latency_s=0),
           run_match(_rec([], a_won=False), P)]
    s = summarize(res)
    assert s["matches"] == 2 and s["matches_traded"] == 1
    b = brier([_rec([], pm=0.8)])
    assert b["polymarket"] == pytest.approx(0.04) and b["kalshi_mid"] == pytest.approx(0.25)


def test_series_step_lookup():
    s = Series([[10, 0.1], [20, float("nan")], [30, 0.3]])
    assert s.at(5) is None and s.at(25) == 0.1 and s.at(30) == 0.3


# ---------------------------------------------------------------- live pieces
def test_pm_mid_uses_complement_token_for_bid():
    books = {"a": [(0.52, 10)], "b": [(0.50, 10)]}      # bid_a = 1 - 0.50
    assert pm_mid(books, "a", "b") == pytest.approx(0.51)
    assert pm_mid({"a": [(0.70, 1)], "b": [(0.50, 1)]}, "a", "b") is None  # 20c wide


def test_paper_broker_fills_from_public_tape():
    br = PaperBroker(queue_frac=0.5)
    with mock.patch("esports_arb.mm.live.time.time", return_value=1000.0):
        oid = br.place("T", "bid", 0.40, 5)
    tape = {"trades": [
        {"created_time": "1970-01-01T00:16:50Z", "yes_price_dollars": "0.38", "count_fp": "3", "taker_side": "no"},
        {"created_time": "1970-01-01T00:17:00Z", "yes_price_dollars": "0.40", "count_fp": "10", "taker_side": "no"},
    ]}
    with mock.patch("esports_arb.mm.live.get_json", return_value=tape):
        fills = br.poll_fills()
    assert [(f[1], f[2], f[3]) for f in fills] == [("buy", 0.40, 3), ("buy", 0.40, 2)]
    assert oid not in br.orders   # fully filled


def test_kalshi_signature_verifies(tmp_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from esports_arb.mm.kalshi_client import KalshiClient

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "k.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    c = KalshiClient(key_id="kid", key_path=str(pem))
    h = c._headers("GET", "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance?x=1")
    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/v2/portfolio/balance").encode()
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    assert h["KALSHI-ACCESS-KEY"] == "kid"


def test_kalshi_ask_is_sent_as_buy_no():
    from esports_arb.mm.kalshi_client import KalshiClient
    c = KalshiClient.__new__(KalshiClient)
    with mock.patch.object(KalshiClient, "_req", return_value={"order": {"order_id": "1"}}) as req:
        c.place_limit("T", "ask", 0.63, 4)
    body = req.call_args.kwargs["json"]
    assert body["side"] == "no" and body["action"] == "buy" and body["no_price_dollars"] == "0.3700"
    assert body["post_only"] is True
