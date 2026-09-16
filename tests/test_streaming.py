import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from esports_arb.matcher import Cluster
from esports_arb.models import Leg, Match
from esports_arb.streaming.books import BookStore, KalshiBook, PolyBook
from esports_arb.streaming.hub import StreamHub
from esports_arb.streaming.kalshi_poll import KalshiPoller
from esports_arb.streaming.kalshi_ws import KalshiStream, SeqGap
from esports_arb.streaming.polymarket_ws import PolymarketStream


# ---------------------------------------------------------------- books
def test_poly_book_snapshot_and_level_updates():
    b = PolyBook()
    b.snapshot([{"price": "0.40", "size": "10"}], [{"price": "0.45", "size": "5"}, {"price": "0.44", "size": "0"}])
    assert b.ask_ladder() == [(0.45, 5.0)]
    b.set_level("SELL", "0.43", "7")
    b.set_level("SELL", "0.45", "0")          # size 0 removes the level
    b.set_level("BUY", "0.41", "3")
    assert b.ask_ladder() == [(0.43, 7.0)]
    assert b.best_bid() == 0.41


def test_kalshi_book_deltas_and_mirrored_asks():
    b = KalshiBook()
    b.snapshot([["0.40", "100"]], [["0.55", "20"], ["0.50", "5"]])
    assert b.ask_ladder("yes") == [(0.45, 20.0), (0.5, 5.0)]   # 1 - NO bids
    assert b.ask_ladder("no") == [(0.6, 100.0)]
    b.apply_delta("no", "0.55", "-20")
    b.apply_delta("yes", "0.42", "8")
    assert b.best_yes_ask() == 0.5 and b.best_yes_bid() == 0.42


# ---------------------------------------------------------------- polymarket messages
def test_pm_handle_book_then_price_change_and_ignores_pre_snapshot_updates():
    store, seen = BookStore(), []
    s = PolymarketStream(store, lambda v, i: seen.append(i))
    s.handle(json.dumps({"event_type": "price_change", "price_changes": [
        {"asset_id": "t1", "price": "0.5", "size": "9", "side": "SELL"}]}))
    assert not store.pm("t1").ready and seen == []
    s.handle(json.dumps([{"event_type": "book", "asset_id": "t1",
                          "bids": [{"price": "0.30", "size": "1"}], "asks": [{"price": "0.35", "size": "2"}]}]))
    s.handle(json.dumps({"event_type": "price_change", "price_changes": [
        {"asset_id": "t1", "price": "0.33", "size": "4", "side": "SELL"},
        {"asset_id": "t1", "price": "0.35", "size": "0", "side": "SELL"}]}))
    assert store.pm("t1").ask_ladder() == [(0.33, 4.0)]
    assert seen == ["t1", "t1"]
    s.handle("PONG")


def test_pm_trade_callback():
    trades = []
    s = PolymarketStream(BookStore(), lambda v, i: None, on_trade=lambda *a: trades.append(a[:4]))
    s.handle(json.dumps({"event_type": "last_trade_price", "asset_id": "t", "price": "0.4",
                         "size": "3", "side": "BUY"}))
    assert trades == [("t", 0.4, 3.0, "BUY")]


# ---------------------------------------------------------------- kalshi messages
def _k(kind, sid, seq, **msg):
    return json.dumps({"type": kind, "sid": sid, "seq": seq, "msg": msg})


def test_kalshi_snapshot_delta_and_seq_gap():
    store, seen = BookStore(), []
    s = KalshiStream(store, lambda v, i: seen.append(i))
    s.handle(_k("orderbook_snapshot", 1, 1, market_ticker="M", yes_dollars_fp=[["0.40", "10"]],
                no_dollars_fp=[["0.50", "10"]]))
    s.handle(_k("orderbook_delta", 1, 2, market_ticker="M", price_dollars="0.52", delta_fp="3.00", side="no"))
    assert store.ks("M").best_yes_ask() == 0.48
    with pytest.raises(SeqGap):
        s.handle(_k("orderbook_delta", 1, 4, market_ticker="M", price_dollars="0.41", delta_fp="1", side="yes"))
    assert s.gaps == 1 and seen == ["M", "M"]


def test_kalshi_trade_callback_uses_ms_timestamp():
    got = []
    s = KalshiStream(BookStore(), lambda v, i: None, on_trade=lambda *a: got.append(a))
    s.handle(json.dumps({"type": "trade", "sid": 9, "seq": 1, "msg": {
        "market_ticker": "M", "yes_price_dollars": "0.36", "count_fp": "12.00", "taker_side": "no",
        "ts_ms": 1669149841500}}))
    assert got == [("M", 0.36, 12.0, "no", 1669149841.5)]


def test_kalshi_subscribe_command():
    s = KalshiStream(BookStore(), lambda v, i: None)
    s.tickers = {"B", "A"}
    cmd = json.loads(s.subscribe_cmd())
    assert cmd["cmd"] == "subscribe" and cmd["params"]["market_tickers"] == ["A", "B"]
    assert set(cmd["params"]["channels"]) == {"orderbook_delta", "trade"}


def test_poller_only_fires_on_change():
    store, seen = BookStore(), []
    p = KalshiPoller(store, lambda v, i: seen.append(i))
    m = {"ticker": "M", "yes_bid_dollars": "0.40", "yes_bid_size_fp": "5",
         "yes_ask_dollars": "0.45", "yes_ask_size_fp": "7"}
    p.apply([m], now=100.0); p.apply([m], now=105.0)
    assert seen == ["M"]
    assert store.ks("M").ts == 105.0   # unchanged poll still marks the book fresh
    assert store.ks("M").ask_ladder("yes") == [(0.45, 7.0)] and store.ks("M").depth == "top"
    p.apply([dict(m, yes_ask_dollars="0.46")])
    assert seen == ["M", "M"]


# ---------------------------------------------------------------- hub episodes
def _cluster():
    k = Match("kalshi", "cs2", "A", "B", datetime(2030, 1, 1, tzinfo=timezone.utc), "A vs B")
    k.legs_a = [Leg("kalshi", "A", [], "KA", side="YES", fee_model="kalshi", fee_rate=0.07),
                Leg("kalshi", "A", [], "KB", side="NO", fee_model="kalshi", fee_rate=0.07)]
    k.legs_b = [Leg("kalshi", "B", [], "KB", side="YES", fee_model="kalshi", fee_rate=0.07),
                Leg("kalshi", "B", [], "KA", side="NO", fee_model="kalshi", fee_rate=0.07)]
    p = Match("polymarket", "cs2", "A", "B", k.start, "A vs B")
    p.legs_a = [Leg("polymarket", "A", [], "PA", fee_model="polymarket", fee_rate=0.05)]
    p.legs_b = [Leg("polymarket", "B", [], "PB", fee_model="polymarket", fee_rate=0.05)]
    return Cluster("cs2", k, [k, p], [1.0, 1.0])


def _hub(tmp_path):
    hub = StreamHub(["cs2"], out=str(tmp_path / "ep.jsonl"), max_kalshi_age=60)
    cl = _cluster()
    hub.clusters["c"] = cl
    for ins, venue in (("KA", "kalshi"), ("KB", "kalshi"), ("PA", "polymarket"), ("PB", "polymarket")):
        hub.by_instrument[(venue, ins)] = ["c"]
    return hub


def test_hub_opens_and_closes_episode(tmp_path):
    hub = _hub(tmp_path)
    st = hub.store
    st.ks("KA").snapshot([["0.30", "50"]], [["0.68", "50"]])     # YES A ask 0.32
    st.ks("KB").snapshot([["0.60", "50"]], [["0.35", "50"]])     # YES B ask 0.65
    st.pm("PA").snapshot([], [{"price": "0.34", "size": "50"}])   # 0.34 + 0.65 + fees > 1
    st.pm("PB").snapshot([], [{"price": "0.75", "size": "50"}])
    hub.on_update("polymarket", "PB")
    assert hub.open == {}
    st.pm("PA").set_level("SELL", "0.25", "40")                     # A now 0.25 on Polymarket
    hub.on_update("polymarket", "PA")
    # two ways to be long B on Kalshi: YES on "B wins" (0.65) and NO on "A wins" (0.70)
    assert sorted(ep.leg_b for ep in hub.open.values()) == ["kalshi/NO:B", "kalshi/YES:B"]
    assert all(ep.leg_a == "polymarket:A" for ep in hub.open.values())
    time.sleep(0.01)
    st.pm("PA").set_level("SELL", "0.25", "0")                      # edge gone
    hub.on_update("polymarket", "PA")
    assert hub.open == {} and hub.closed == 2
    rec = json.loads((tmp_path / "ep.jsonl").read_text().splitlines()[0])
    assert rec["duration_ms"] >= 10 and rec["open_trigger"] == "polymarket" and not rec["censored"]
    assert rec["max_size"] == 40
    assert rec["in_play"] is False and rec["match_start"].startswith("2030-01-01")


def test_hub_ignores_stale_kalshi_books(tmp_path):
    hub = _hub(tmp_path)
    st = hub.store
    st.ks("KB").snapshot([["0.60", "50"]], [["0.35", "50"]], ts=time.time() - 3600)
    st.ks("KA").snapshot([], [], ts=time.time() - 3600)
    st.pm("PA").snapshot([], [{"price": "0.20", "size": "50"}])
    hub.on_update("polymarket", "PA")
    assert hub.open == {}


# ---------------------------------------------------------------- live socket (local server)
def test_polymarket_client_against_local_server():
    websockets = pytest.importorskip("websockets")

    async def scenario():
        got_sub = []

        async def handler(ws):
            got_sub.append(json.loads(await ws.recv()))
            await ws.send(json.dumps([{"event_type": "book", "asset_id": "t1", "bids": [],
                                       "asks": [{"price": "0.61", "size": "3"}]}]))
            await ws.send(json.dumps({"event_type": "price_change", "price_changes": [
                {"asset_id": "t1", "price": "0.60", "size": "2", "side": "SELL"}]}))
            await asyncio.sleep(0.5)

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            store = BookStore()
            s = PolymarketStream(store, lambda v, i: None, url=f"ws://127.0.0.1:{port}")
            await s.add(["t1"])
            stop = asyncio.Event()
            task = asyncio.create_task(s.run(stop))
            for _ in range(50):
                await asyncio.sleep(0.02)
                if store.pm("t1").ask_ladder() == [(0.6, 2.0), (0.61, 3.0)]:
                    break
            ladder = store.pm("t1").ask_ladder()
            stop.set()
            task.cancel()
            return got_sub, ladder

    sub, ladder = asyncio.run(scenario())
    assert sub == [{"assets_ids": ["t1"], "type": "market"}]
    assert ladder == [(0.6, 2.0), (0.61, 3.0)]


def test_kalshi_client_sends_auth_and_subscribes(tmp_path):
    websockets = pytest.importorskip("websockets")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from esports_arb.mm.kalshi_client import KalshiClient

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "k.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    signer = KalshiClient(key_id="kid", key_path=str(pem))

    async def scenario():
        seen = {}

        async def handler(ws):
            seen["key"] = ws.request.headers.get("KALSHI-ACCESS-KEY")
            seen["sig"] = bool(ws.request.headers.get("KALSHI-ACCESS-SIGNATURE"))
            seen["sub"] = json.loads(await ws.recv())
            await ws.send(json.dumps({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
                "market_ticker": "M", "yes_dollars_fp": [["0.4", "1"]], "no_dollars_fp": [["0.5", "2"]]}}))
            await asyncio.sleep(0.5)

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            store = BookStore()
            s = KalshiStream(store, lambda v, i: None, signer=signer,
                             url=f"ws://127.0.0.1:{port}/trade-api/ws/v2")
            await s.add(["M"])
            stop = asyncio.Event()
            task = asyncio.create_task(s.run(stop))
            for _ in range(50):
                await asyncio.sleep(0.02)
                if store.ks("M").ready:
                    break
            ready = store.ks("M").best_yes_ask()
            stop.set()
            task.cancel()
            return seen, ready

    seen, ask = asyncio.run(scenario())
    assert seen["key"] == "kid" and seen["sig"]
    assert seen["sub"]["params"]["market_tickers"] == ["M"]
    assert ask == 0.5
