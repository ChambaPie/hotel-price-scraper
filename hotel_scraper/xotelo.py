"""Client for Xotelo (data.xotelo.com): free, keyless TripAdvisor hotel listings and prices.
It's a free service: callers keep request counts capped (settings) and pause between calls."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from .http import Session


@dataclass
class Result:
    ok: bool
    data: dict = field(default_factory=dict)
    error: str = ""
    status: int = 0
    elapsed: float = 0.0


class Xotelo:
    def __init__(self, base_url: str, session: Session | None = None, user_agent: str = "hotel-deal-radar",
                 pause_seconds=(1, 2), sleep=time.sleep):
        self.base = base_url.rstrip("/")
        self.session = session or Session(user_agent)
        self.pause, self.sleep = pause_seconds, sleep
        self.requests = 0

    def _call(self, endpoint: str, params: dict) -> Result:
        if self.requests:
            self.sleep(random.uniform(*self.pause))
        self.requests += 1
        resp = self.session.get(f"{self.base}/{endpoint}?{urlencode(params)}")
        if resp.status != 200:
            return Result(False, error=resp.error or f"HTTP {resp.status}", status=resp.status, elapsed=resp.elapsed)
        try:
            body = resp.json()
        except ValueError:
            return Result(False, error="not JSON", status=resp.status, elapsed=resp.elapsed)
        if body.get("error"):
            return Result(False, error=str(body["error"])[:300], status=resp.status, elapsed=resp.elapsed)
        return Result(True, body.get("result") or {}, status=resp.status, elapsed=resp.elapsed)

    def list(self, location_key: str, offset: int = 0, limit: int = 100, sort: str = "best_value") -> Result:
        return self._call("list", {"location_key": location_key, "offset": offset, "limit": limit, "sort": sort})

    def heatmap(self, hotel_key: str, chk_out: str) -> Result:
        return self._call("heatmap", {"hotel_key": hotel_key, "chk_out": chk_out})

    def rates(self, hotel_key: str, chk_in: str, chk_out: str, currency: str, adults: int, rooms: int) -> Result:
        return self._call("rates", {"hotel_key": hotel_key, "chk_in": chk_in, "chk_out": chk_out,
                                    "currency": currency, "adults": adults, "rooms": rooms})
