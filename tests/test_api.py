from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from nhlscreen.api.app import create_app
from nhlscreen.config import Config
from nhlscreen.nhl import parsers
from nhlscreen.nhl.models import GameEvent
from nhlscreen.runtime import Runtime


@pytest.fixture
def runtime(fixture) -> Runtime:
    rt = Runtime(Config(favorite_teams=["TOR"], favorite_players=[8478483]))
    s = rt.store
    s.set_games(parsers.parse_score(fixture("score.json")))
    s.set_detail(parsers.parse_play_by_play(fixture("pbp_final.json")))
    s.set_standings(parsers.parse_standings(fixture("standings.json")))
    s.set_schedule("TOR", parsers.parse_schedule(fixture("schedule_tor.json")))
    s.set_leaders(parsers.parse_leaders(fixture("skater_leaders.json")))
    s.set_player(parsers.parse_player(fixture("player_8478483.json")))
    return rt


@pytest.fixture
def api(runtime):
    # Don't start the pollers: tests must not hit the real NHL API.
    with TestClient(create_app(runtime, manage_lifecycle=False)) as c:
        yield c


def test_endpoints(api):
    assert api.get("/health").json()["ok"] is True
    assert len(api.get("/games").json()) == 16
    detail = api.get("/games/2026020102").json()
    assert detail["game"]["home"]["abbrev"] == "UTA" and "plays" not in detail
    assert api.get("/games/1").status_code == 404
    assert len(api.get("/standings", params={"scope": "division", "team": "TOR"}).json()) == 8
    assert "points" in api.get("/leaders").json()
    team = api.get("/teams/tor").json()
    assert team["standing"]["abbrev"] == "TOR" and team["next_game"] is not None
    assert api.get("/players/8478483").json()["last_name"] == "Marner"
    assert api.get("/players/1").status_code == 404
    assert "nhlscreen" in api.get("/debug").text


def test_current_screen(api):
    screen = api.get("/screen/current").json()
    assert screen["type"] in {"next_game", "scoreboard", "standings", "leaders", "player_card"}
    assert screen["lines"]


def test_websocket_pushes_screen_and_events(api, runtime):
    with api.websocket_connect("/events") as ws:
        runtime.tick()
        msg = ws.receive_json()
        assert msg["type"] == "screen"
        runtime.store.add_event(GameEvent(kind="goal", game_id=1, key="k", away_abbrev="TOR", home_abbrev="UTA",
                                          occurred_at=datetime.now(timezone.utc)))
        msg = ws.receive_json()
        assert msg["type"] == "event" and msg["event"]["kind"] == "goal"
    assert len(api.get("/events/recent").json()) == 1
