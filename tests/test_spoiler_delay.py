"""Anti-spoiler delay: live game data reaches the board a set time after it was produced."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nhlscreen.api.app import create_app
from nhlscreen.config import MAX_SPOILER_DELAY_SECONDS, SPOILER_DELAY_SECONDS, Config
from nhlscreen.runtime import Runtime

from test_displayteam import TOR_GAME, StubClient


def _runtime(fixture, delay: float, **kw) -> Runtime:
    rt = Runtime(Config(favorite_teams=["TOR"]), **kw)
    rt.poller.client = StubClient(fixture)
    rt.poller.spoiler_delay_seconds = delay
    return rt


def test_delay_is_25_and_can_only_be_raised():
    assert Config().display.spoiler_delay_seconds == SPOILER_DELAY_SECONDS == 25
    for too_short_or_long in (0, 20, 24.9, MAX_SPOILER_DELAY_SECONDS + 1):
        with pytest.raises(ValidationError):
            Config.model_validate({"display": {"spoiler_delay_seconds": too_short_or_long}})
    rt = Runtime(Config.model_validate({"display": {"spoiler_delay_seconds": 40}}))
    assert rt.poller.spoiler_delay_seconds == 40


def test_delay_set_from_the_preview_page_is_saved(tmp_path):
    state = tmp_path / "nhlscreen-state.json"
    rt = Runtime(Config(), state_path=state)
    with TestClient(create_app(rt, manage_lifecycle=False)) as api:
        assert 'id="delayForm"' in api.get("/debug").text
        assert api.get("/control/delay").json() == {
            "spoiler_delay_seconds": 25, "configured_seconds": 25, "min_seconds": 25, "max_seconds": 300}
        too_short = api.put("/control/delay", json={"seconds": 20})
        assert too_short.status_code == 400 and "25 to 300" in too_short.json()["detail"]
        assert rt.poller.spoiler_delay_seconds == 25

        assert api.put("/control/delay", json={"seconds": 45}).json()["spoiler_delay_seconds"] == 45
        assert rt.poller.spoiler_delay_seconds == rt.config.display.spoiler_delay_seconds == 45
        assert json.loads(state.read_text())["spoiler_delay_seconds"] == 45
        assert Runtime(Config(), state_path=state).poller.spoiler_delay_seconds == 45  # survives a restart

        assert api.put("/control/delay", json={"seconds": None}).json()["spoiler_delay_seconds"] == 25
        assert rt.poller.spoiler_delay_seconds == 25
        assert not state.exists()


async def test_raising_the_delay_holds_queued_data_longer(fixture):
    rt = _runtime(fixture, 0.2)
    try:
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        rt.poller.set_spoiler_delay(0.5)
        await asyncio.sleep(0.3)
        assert TOR_GAME not in rt.store.details
        await asyncio.sleep(0.3)
        assert TOR_GAME in rt.store.details
    finally:
        await rt.poller.stop()


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


def _clock(seconds: int, running: bool) -> dict:
    return {"timeRemaining": f"{seconds // 60:02d}:{seconds % 60:02d}", "secondsRemaining": seconds,
            "running": running, "inIntermission": False}


@pytest.mark.parametrize("stopped", [True, False])
async def test_goal_goes_out_as_the_clock_stops(fixture, stopped):
    """The board's clock reaches a stop before that snapshot is due and holds there.
    A goal in it goes out at that moment, not after the hold."""
    # 100x: the 50 game-second delay is 0.5s, and 10 game seconds of clock take 0.1s.
    rt = _runtime(fixture, 50, speed=100)
    client, ages = rt.poller.client, []
    client.age = lambda path, params=None: ages.pop(0)
    full = client.pbp
    last_goal = max(i for i, p in enumerate(full["plays"]) if p.get("typeDescKey") == "goal")
    try:
        # Before the goal: clock running at 10:00, shown right away (already 0.5s old).
        client.pbp, ages[:] = {**full, "plays": full["plays"][:last_goal], "clock": _clock(600, True)}, [0.5]
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        # Queued ahead of the goal, due later than its clock stop: a re-fetch of that
        # snapshot (as with max-age=0) and a scoreboard. Neither may hold it back.
        ages[:] = [0.2, 0.0]
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)
        rt.poller._update_tracking = lambda games: None
        await rt.poller.refresh_scoreboard()
        # The goal, clock stopped at 9:50 (or still running): due 0.5s from now.
        client.pbp, ages[:] = {**full, "clock": _clock(590, not stopped)}, [0.0]
        await rt.poller.refresh_game(TOR_GAME, with_stats=False)

        await asyncio.sleep(0.25)
        goals = [e for e in rt.store.recent_events if e.kind == "goal"]
        assert bool(goals) == stopped  # the board's clock stopped at 9:50 0.1s in
        if stopped:
            assert rt.store.clock_segment(TOR_GAME, datetime.now(timezone.utc)).value(0) == 590
        await asyncio.sleep(0.35)
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
