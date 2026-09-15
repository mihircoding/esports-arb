"""Tiny HTTP helper with retries and polite rate limiting."""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "esports-arb/0.1 (+research)"


def get_json(url: str, params: Optional[dict] = None, retries: int = 3, pause: float = 0.0) -> Any:
    return _request("GET", url, params=params, retries=retries, pause=pause)


def post_json(url: str, body: Any, retries: int = 3) -> Any:
    return _request("POST", url, json=body, retries=retries)


def _request(method: str, url: str, retries: int = 3, pause: float = 0.0, **kw) -> Any:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = _SESSION.request(method, url, timeout=20, **kw)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            if pause:
                time.sleep(pause)
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"{method} {url} failed: {last}")
