"""Async HTTP client for Polymarket public APIs (Gamma + CLOB). No auth required."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Iterable

import aiohttp

from .config import CLOB_URL, GAMMA_URL, Config
from .models import Market, OrderBook

log = logging.getLogger("polyscan.api")


class RateLimiter:
    """Simple token bucket: `rate` requests per `per` seconds."""

    def __init__(self, rate: int, per: float):
        self.rate, self.per = rate, per
        self.tokens = float(rate)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.updated) * self.rate / self.per)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) * self.per / self.rate)


class PolyClient:
    BOOKS_BATCH = 200
    GAMMA_PAGE = 100  # Gamma caps `limit` at 100

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(cfg.concurrency)
        # conservative versus documented limits
        self._gamma_rl = RateLimiter(40, 10)
        self._clob_rl = RateLimiter(60, 10)
        self.n_requests = 0

    async def __aenter__(self) -> "PolyClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg.request_timeout),
            headers={"User-Agent": "polyscan/1.0", "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    async def _request(self, method: str, url: str, *, params=None, json_body=None, retries: int = 4) -> Any:
        assert self._session is not None, "use `async with PolyClient(cfg)`"
        rl = self._gamma_rl if url.startswith(GAMMA_URL) else self._clob_rl
        for attempt in range(retries + 1):
            await rl.acquire()
            async with self._sem:
                try:
                    self.n_requests += 1
                    async with self._session.request(method, url, params=params, json=json_body) as r:
                        if r.status == 200:
                            return await r.json(content_type=None)
                        if r.status in (429, 500, 502, 503, 504):
                            raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status)
                        text = await r.text()
                        log.warning("HTTP %s %s -> %s %s", method, url, r.status, text[:200])
                        return None
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    if attempt == retries:
                        log.error("giving up %s %s: %r", method, url, e)
                        return None
                    await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.random() * 0.3)
        return None

    # ------------------------------------------------------------------ Gamma
    async def fetch_markets(self, max_markets: int) -> list[Market]:
        """Active, open markets ordered by 24h volume (descending)."""
        offsets = list(range(0, max_markets, self.GAMMA_PAGE))

        async def page(off: int) -> list[dict]:
            params = {
                "active": "true", "closed": "false", "archived": "false",
                "limit": str(self.GAMMA_PAGE), "offset": str(off),
                "order": "volume24hr", "ascending": "false",
            }
            return await self._request("GET", f"{GAMMA_URL}/markets", params=params) or []

        pages = await asyncio.gather(*(page(o) for o in offsets))
        seen, out = set(), []
        for p in pages:
            for d in p:
                m = Market.from_gamma(d)
                if m and m.id not in seen and m.accepting_orders and not m.closed:
                    seen.add(m.id)
                    out.append(m)
        return out

    async def fetch_event(self, event_id: str) -> dict | None:
        return await self._request("GET", f"{GAMMA_URL}/events/{event_id}")

    async def fetch_event_markets(self, event_id: str) -> tuple[list[Market], list[dict]] | None:
        """Return (open tradable markets, raw list of all markets) for an event."""
        ev = await self.fetch_event(event_id)
        if not ev:
            return None
        raw = ev.get("markets") or []
        mkts = []
        for d in raw:
            m = Market.from_gamma(d, event=ev)
            if m and m.active and not m.closed:
                mkts.append(m)
        return mkts, raw

    # ------------------------------------------------------------------- CLOB
    async def fetch_books(self, token_ids: Iterable[str]) -> dict[str, OrderBook]:
        ids = list(dict.fromkeys(str(t) for t in token_ids))
        chunks = [ids[i:i + self.BOOKS_BATCH] for i in range(0, len(ids), self.BOOKS_BATCH)]

        async def chunk(c: list[str]) -> list[dict]:
            return await self._request("POST", f"{CLOB_URL}/books", json_body=[{"token_id": t} for t in c]) or []

        res: dict[str, OrderBook] = {}
        for lst in await asyncio.gather(*(chunk(c) for c in chunks)):
            for d in lst if isinstance(lst, list) else []:
                ob = OrderBook.from_clob(d)
                res[ob.token_id] = ob
        return res

    async def price_history(self, token_id: str, interval: str = "1w", fidelity: int = 60) -> list[tuple[int, float]]:
        data = await self._request(
            "GET", f"{CLOB_URL}/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": str(fidelity)},
        )
        hist = (data or {}).get("history") or []
        return [(int(h["t"]), float(h["p"])) for h in hist if "t" in h and "p" in h]
