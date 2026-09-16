"""Authenticated Kalshi REST client (API key + RSA-PSS request signing).

Create a key at kalshi.com → Account → API Keys, save the private key file,
then export:

    export KALSHI_KEY_ID=...                     # the key id shown by Kalshi
    export KALSHI_PRIVATE_KEY_PATH=~/kalshi.pem  # never commit this file

Signature = base64(RSA-PSS-SHA256(timestamp_ms + METHOD + path_without_query)).
"""
from __future__ import annotations

import base64
import os
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(self, key_id: Optional[str] = None, key_path: Optional[str] = None, base: str = BASE):
        from cryptography.hazmat.primitives import serialization  # lazy: only needed for live trading

        self.key_id = key_id or os.environ["KALSHI_KEY_ID"]
        path = os.path.expanduser(key_path or os.environ["KALSHI_PRIVATE_KEY_PATH"])
        with open(path, "rb") as fh:
            self._key = serialization.load_pem_private_key(fh.read(), password=None)
        self.base = base
        self.s = requests.Session()

    # -- signing -----------------------------------------------------------
    def _headers(self, method: str, url: str) -> Dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        path = urlparse(url).path
        sig = self._key.sign(
            (ts + method.upper() + path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "Content-Type": "application/json"}

    def _req(self, method: str, path: str, **kw) -> Any:
        url = self.base + path
        r = self.s.request(method, url, headers=self._headers(method, url), timeout=15, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    # -- endpoints ---------------------------------------------------------
    def balance(self) -> float:
        return self._req("GET", "/portfolio/balance").get("balance", 0) / 100

    def place_limit(self, ticker: str, side: str, price: float, count: int,
                    post_only: bool = True) -> Dict[str, Any]:
        """side='bid' buys YES at price; side='ask' sells YES at price (= buys NO at 1 - price)."""
        if side == "bid":
            body = {"ticker": ticker, "action": "buy", "side": "yes", "count": int(count),
                    "type": "limit", "yes_price_dollars": f"{price:.4f}"}
        else:
            body = {"ticker": ticker, "action": "buy", "side": "no", "count": int(count),
                    "type": "limit", "no_price_dollars": f"{1 - price:.4f}"}
        body.update({"client_order_id": str(uuid.uuid4()), "post_only": post_only,
                     "time_in_force": "good_till_canceled"})
        return self._req("POST", "/portfolio/orders", json=body).get("order", {})

    def cancel(self, order_id: str) -> None:
        self._req("DELETE", f"/portfolio/orders/{order_id}")

    def resting_orders(self, ticker: Optional[str] = None) -> List[dict]:
        params = {"status": "resting", "limit": 200}
        if ticker:
            params["ticker"] = ticker
        return self._req("GET", "/portfolio/orders", params=params).get("orders", [])

    def fills(self, min_ts: Optional[int] = None) -> List[dict]:
        params = {"limit": 200}
        if min_ts:
            params["min_ts"] = int(min_ts)
        return self._req("GET", "/portfolio/fills", params=params).get("fills", [])

    def positions(self) -> List[dict]:
        return self._req("GET", "/portfolio/positions", params={"limit": 200}).get("market_positions", [])
