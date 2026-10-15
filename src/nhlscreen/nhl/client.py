"""Async HTTP client for the NHL web API.

Responsibilities:
  * honor the CDN's Cache-Control max-age so we never refetch data that can't
    have changed yet; the copy's Age counts against it, so we refetch the moment
    the CDN's copy expires instead of locking onto copies that are already stale
  * remember how old each response's data is (``age``), so a live clock reading
    can be dated to when it was true rather than when we happened to fetch it
  * back off host-wide on 429 / 5xx / network errors (honoring Retry-After)
  * fall back to the last good response so the board never goes blank
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

_MAX_AGE = re.compile(r"max-age=(\d+)")


class NHLUnavailable(Exception):
    """No fresh data and no cached fallback for this request."""


class NHLNotFound(NHLUnavailable):
    """The API answered 404: the thing asked for (e.g. a player ID) doesn't exist."""


@dataclass
class _CacheEntry:
    data: Any
    fetched_at: float
    expires_at: float
    # When the CDN's origin produced this data (fetched_at minus the Age header).
    produced_at: float


class NHLClient:
    def __init__(
        self,
        base_url: str = "https://api-web.nhle.com/v1",
        user_agent: str = "nhlscreen/0.1",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_backoff: float = 120.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._cache: dict[str, _CacheEntry] = {}
        self._clock = clock
        self._max_backoff = max_backoff
        self._failures = 0
        self._backoff_until = 0.0
        self._locks: dict[str, asyncio.Lock] = {}
        self.request_count = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def backoff_remaining(self) -> float:
        return max(0.0, self._backoff_until - self._clock())

    def _key(self, path: str, params: dict[str, Any] | None) -> str:
        if not params:
            return path
        return path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    def age(self, path: str, params: dict[str, Any] | None = None) -> float:
        """Seconds since the data last returned for this request was produced (0 if unknown)."""
        cached = self._cache.get(self._key(path, params))
        return max(0.0, self._clock() - cached.produced_at) if cached else 0.0

    def _register_failure(self, retry_after: float | None) -> None:
        self._failures += 1
        delay = min(self._max_backoff, 2 ** min(self._failures, 10))
        if retry_after is not None:
            delay = max(delay, retry_after)
        self._backoff_until = self._clock() + delay
        log.warning("NHL API backoff for %.0fs (consecutive failures: %d)", delay, self._failures)

    async def get_json(self, path: str, params: dict[str, Any] | None = None, min_ttl: float = 0.0) -> Any:
        """GET a JSON document.

        ``min_ttl`` lets callers say "don't refetch this more often than N seconds"
        even if the CDN allows it; the effective TTL is max(min_ttl, max-age).
        """
        key = self._key(path, params)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._clock()
            cached = self._cache.get(key)
            if cached and now < cached.expires_at:
                return cached.data

            if now < self._backoff_until:
                if cached:
                    return cached.data
                raise NHLUnavailable(f"{key}: backing off for {self._backoff_until - now:.0f}s")

            try:
                self.request_count += 1
                resp = await self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                log.warning("NHL API request failed for %s: %s", key, exc)
                self._register_failure(None)
                return self._fallback(key, cached, exc)

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                log.warning("NHL API %s for %s", resp.status_code, key)
                self._register_failure(retry_after)
                return self._fallback(key, cached, None)
            if resp.status_code == 404:
                raise NHLNotFound(f"{key}: HTTP 404")
            if resp.status_code >= 400:
                # 4xx is a request problem, not an outage: don't back off.
                raise NHLUnavailable(f"{key}: HTTP {resp.status_code}")

            try:
                data = resp.json()
            except ValueError as exc:
                log.warning("NHL API returned non-JSON for %s", key)
                return self._fallback(key, cached, exc)

            self._failures = 0
            self._backoff_until = 0.0
            max_age = _parse_max_age(resp.headers.get("Cache-Control"))
            age = min(_parse_age(resp.headers.get("Age")), max_age)
            ttl = max(min_ttl, max_age - age)
            fetched = self._clock()
            self._cache[key] = _CacheEntry(data=data, fetched_at=fetched, expires_at=fetched + ttl,
                                           produced_at=fetched - age)
            return data

    @staticmethod
    def _fallback(key: str, cached: _CacheEntry | None, exc: Exception | None) -> Any:
        if cached:
            return cached.data
        raise NHLUnavailable(key) from exc

    # ------------------------------------------------------------------ endpoints

    async def score_now(self) -> Any:
        return await self.get_json(SCORE_NOW_PATH)

    async def play_by_play(self, game_id: int) -> Any:
        return await self.get_json(play_by_play_path(game_id))

    async def right_rail(self, game_id: int) -> Any:
        return await self.get_json(f"/gamecenter/{game_id}/right-rail")

    async def landing(self, game_id: int) -> Any:
        return await self.get_json(f"/gamecenter/{game_id}/landing")

    async def standings(self) -> Any:
        return await self.get_json("/standings/now")

    async def club_schedule(self, abbrev: str) -> Any:
        return await self.get_json(f"/club-schedule-season/{abbrev}/now")

    async def skater_leaders(self, categories: list[str], limit: int) -> Any:
        return await self.get_json(
            "/skater-stats-leaders/current", {"categories": ",".join(categories), "limit": limit}
        )

    async def goalie_leaders(self, categories: list[str], limit: int) -> Any:
        return await self.get_json(
            "/goalie-stats-leaders/current", {"categories": ",".join(categories), "limit": limit}
        )

    async def player(self, player_id: int) -> Any:
        return await self.get_json(f"/player/{player_id}/landing")


def play_by_play_path(game_id: int) -> str:
    return f"/gamecenter/{game_id}/play-by-play"


SCORE_NOW_PATH = "/score/now"


def _parse_age(header: str | None) -> float:
    try:
        return max(0.0, float(header)) if header else 0.0
    except ValueError:
        return 0.0


def _parse_max_age(header: str | None) -> float:
    if not header:
        return 0.0
    m = _MAX_AGE.search(header)
    return float(m.group(1)) if m else 0.0


def _parse_retry_after(header: str | None) -> float | None:
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None  # HTTP-date form; fall back to exponential backoff
