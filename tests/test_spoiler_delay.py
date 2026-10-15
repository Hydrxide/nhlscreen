"""Anti-spoiler delay: live game data reaches the board N seconds after it's fetched."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from nhlscreen.api.app import create_app
from nhlscreen.config import Config
from nhlscreen.runtime import Runtime

from test_displayteam import TOR_GAME, StubClient


def _runtime(fixture, delay: float, **kw) -> Runtime:
    rt = Runtime(Config(favorite_teams=["TOR"], spoiler_delay_seconds=delay), **kw)
    rt.poller.client = StubClient(fixture)
    return rt


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


async def test_goal_alert_waits_for_the_delay(fixture):
    rt = _runtime(fixture, 0.2)
    client = rt.poller.client
    try:
        # Seed with the game before its last goal (first sighting is silent).
        full = client.pbp
        last_goal = max(i for i, p in enumerate(full["plays"]) if p.get("typeDescKey") == "goal")
        client.pbp = {**full, "plays": full["plays"][:last_goal]}
        rt.config.spoiler_delay_seconds = 0
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        rt.config.spoiler_delay_seconds = 0.2

        client.pbp = full
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        await asyncio.sleep(0.05)
        assert not [e for e in rt.store.recent_events if e.kind == "goal"]
        await asyncio.sleep(0.25)
        assert [e for e in rt.store.recent_events if e.kind == "goal"]
    finally:
        await rt.poller.stop()


async def test_clock_is_dated_to_when_the_snapshot_was_taken(fixture):
    """A play-by-play snapshot that was 12s old when fetched counts down from 12s earlier."""
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
    """Snapshot 12s old at fetch + 0.3s delay: the countdown is anchored 11.7s before release,
    i.e. the reading's true time shifted by exactly the delay."""
    rt = _runtime(fixture, 0.3)
    rt.poller.client.age = lambda path, params=None: 12.0
    try:
        fetched = datetime.now(timezone.utc)
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        assert rt.store.clock_as_of(TOR_GAME) is None  # nothing shown yet
        await asyncio.sleep(0.45)
        expected = fetched - timedelta(seconds=12) + timedelta(seconds=0.3)
        assert abs((rt.store.clock_as_of(TOR_GAME) - expected).total_seconds()) < 0.1
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


async def test_shortening_the_delay_releases_queued_data(fixture):
    rt = _runtime(fixture, 60)
    try:
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        assert TOR_GAME not in rt.store.details
        rt.set_spoiler_delay(0)
        await asyncio.sleep(0.05)
        assert TOR_GAME in rt.store.details
    finally:
        await rt.poller.stop()


def test_delay_api_validates_and_persists(tmp_path):
    state = tmp_path / "state.json"
    rt = Runtime(Config(), state_path=state)
    with TestClient(create_app(rt, manage_lifecycle=False)) as api:
        body = api.get("/control/delay").json()
        assert body["spoiler_delay_seconds"] == 15 and body["configured_delay_seconds"] == 15

        assert api.put("/control/delay", json={"seconds": 45}).json()["spoiler_delay_seconds"] == 45
        assert json.loads(state.read_text())["spoiler_delay_seconds"] == 45
        assert Runtime(Config(), state_path=state).config.spoiler_delay_seconds == 45  # survives restart

        assert api.put("/control/delay", json={"seconds": -1}).status_code == 400
        assert api.put("/control/delay", json={"seconds": 9999}).status_code == 400

        assert api.put("/control/delay", json={"seconds": None}).json()["spoiler_delay_seconds"] == 15
        assert not state.exists()
        assert api.put("/control/delay", json={}).status_code == 400


def test_toggle_keeps_the_seconds(tmp_path):
    state = tmp_path / "state.json"
    rt = Runtime(Config(spoiler_delay_seconds=30), state_path=state)
    with TestClient(create_app(rt, manage_lifecycle=False)) as api:
        off = api.put("/control/delay", json={"enabled": False}).json()
        assert off["enabled"] is False and off["spoiler_delay_seconds"] == 30
        assert rt.poller.spoiler_delay == 0
        assert Runtime(Config(spoiler_delay_seconds=30), state_path=state).config.spoiler_delay_enabled is False

        on = api.put("/control/delay", json={"enabled": True}).json()
        assert on["enabled"] is True and rt.poller.spoiler_delay == 30
        assert not state.exists()  # back to the config.yaml choice

        api.put("/control/delay", json={"enabled": False})
        reset = api.put("/control/delay", json={"seconds": None}).json()
        assert reset["enabled"] is True and reset["spoiler_delay_seconds"] == 30


async def test_turning_the_delay_off_releases_queued_data(fixture):
    rt = _runtime(fixture, 60)
    try:
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        assert TOR_GAME not in rt.store.details
        rt.set_spoiler_delay_enabled(False)
        await asyncio.sleep(0.05)
        assert TOR_GAME in rt.store.details
    finally:
        await rt.poller.stop()
