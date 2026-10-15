"""Anti-spoiler delay: live game data reaches the board a fixed time after it was produced."""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from nhlscreen.api.app import create_app
from nhlscreen.config import Config
from nhlscreen.runtime import Runtime
from nhlscreen.services.poller import SPOILER_DELAY_SECONDS

from test_displayteam import TOR_GAME, StubClient


def _runtime(fixture, delay: float, **kw) -> Runtime:
    rt = Runtime(Config(favorite_teams=["TOR"]), **kw)
    rt.poller.client = StubClient(fixture)
    rt.poller.spoiler_delay_seconds = delay
    return rt


def test_delay_is_fixed_and_not_configurable():
    rt = Runtime(Config())
    assert rt.poller.spoiler_delay_seconds == SPOILER_DELAY_SECONDS == 25
    with TestClient(create_app(rt, manage_lifecycle=False)) as api:
        assert api.get("/control/delay").status_code == 404
        assert 'id="toggleDelay"' not in api.get("/debug").text


async def test_live_data_is_held_back(fixture):
    rt = _runtime(fixture, 0.2)
    try:
        await rt.poller.refresh_scoreboard()
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        assert not rt.store.games and TOR_GAME not in rt.store.details  # fetched, not shown yet
        await asyncio.sleep(0.1)
        assert TOR_GAME not in rt.store.details
        await asyncio.sleep(0.2)
        assert TOR_GAME in rt.store.games and TOR_GAME in rt.store.details
    finally:
        await rt.poller.stop()


async def test_stale_snapshot_is_released_sooner(fixture):
    """A snapshot already 0.15s old when fetched waits only the rest of the 0.3s delay."""
    rt = _runtime(fixture, 0.3)
    rt.poller.client.age = lambda path, params=None: 0.15
    try:
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        await asyncio.sleep(0.22)
        assert TOR_GAME in rt.store.details
    finally:
        await rt.poller.stop()


async def test_goal_alert_waits_for_the_delay(fixture):
    rt = _runtime(fixture, 0)
    client = rt.poller.client
    try:
        # Seed with the game before its last goal (first sighting is silent).
        full = client.pbp
        last_goal = max(i for i, p in enumerate(full["plays"]) if p.get("typeDescKey") == "goal")
        client.pbp = {**full, "plays": full["plays"][:last_goal]}
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        rt.poller.spoiler_delay_seconds = 0.2

        client.pbp = full
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        await asyncio.sleep(0.05)
        assert not [e for e in rt.store.recent_events if e.kind == "goal"]
        await asyncio.sleep(0.25)
        assert [e for e in rt.store.recent_events if e.kind == "goal"]
    finally:
        await rt.poller.stop()


async def test_clock_is_dated_to_when_the_snapshot_was_taken(fixture):
    """With no delay, a snapshot that was 12s old when fetched counts down from 12s earlier."""
    rt = _runtime(fixture, 0)
    rt.poller.client.age = lambda path, params=None: 12.0
    try:
        before = datetime.now(timezone.utc)
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        as_of = rt.store.clock_as_of(TOR_GAME)
        assert before - timedelta(seconds=12.5) < as_of < before - timedelta(seconds=11.5)
    finally:
        await rt.poller.stop()


async def test_delayed_clock_runs_exactly_delay_behind_the_snapshot(fixture):
    """Snapshot 0.1s old at fetch + 0.3s delay: shown, and anchored, 0.2s after the fetch."""
    rt = _runtime(fixture, 0.3)
    rt.poller.client.age = lambda path, params=None: 0.1
    try:
        fetched = datetime.now(timezone.utc)
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        assert rt.store.clock_as_of(TOR_GAME) is None  # nothing shown yet
        await asyncio.sleep(0.35)
        expected = fetched + timedelta(seconds=0.2)
        assert abs((rt.store.clock_as_of(TOR_GAME) - expected).total_seconds()) < 0.05
        track = rt.store.clock_tracks[TOR_GAME]
        assert abs(track.points[0].at - expected.timestamp()) < 0.05  # plan and release agree
    finally:
        await rt.poller.stop()


async def test_replay_speed_compresses_the_delay(fixture):
    rt = _runtime(fixture, 15, speed=100)  # 15 game seconds = 0.15s real
    try:
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        assert TOR_GAME not in rt.store.details
        await asyncio.sleep(0.25)
        assert TOR_GAME in rt.store.details
    finally:
        await rt.poller.stop()
