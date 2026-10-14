"""Switching the followed team at runtime (`displayteam` CLI -> PUT /control/teams)."""

import asyncio
import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from nhlscreen import cli
from nhlscreen.api.app import create_app
from nhlscreen.config import Config
from nhlscreen.nhl import parsers
from nhlscreen.nhl.models import Clock, GameState
from nhlscreen.runtime import Runtime
from nhlscreen.static.teams import resolve_team, suggest_teams

TOR_GAME = 2026020102  # TOR @ UTA


def test_resolve_team():
    assert resolve_team("tor").abbrev == "TOR"
    assert resolve_team("leafs").abbrev == "TOR"
    assert resolve_team("Maple Leafs").abbrev == "TOR"
    assert resolve_team("oilers").abbrev == "EDM"
    assert resolve_team("xyz") is None
    assert "TOR" in suggest_teams("TRO")


# --------------------------------------------------------------------------- runtime + API


@pytest.fixture
def runtime(fixture, tmp_path):
    rt = Runtime(Config(favorite_teams=["TOR"]), state_path=tmp_path / "state.json")
    rt.store.set_games(parsers.parse_score(fixture("score.json")))
    rt.store.set_standings(parsers.parse_standings(fixture("standings.json")))
    rt.poller_calls = 0

    async def fake_favorites_changed():  # no network in tests
        rt.poller_calls += 1

    rt.poller.favorites_changed = fake_favorites_changed
    return rt


@pytest.fixture
def api(runtime):
    with TestClient(create_app(runtime, manage_lifecycle=False)) as c:
        yield c


def test_switch_team_updates_config_screen_and_state(api, runtime):
    runtime.tick()
    resp = api.put("/control/teams", json={"teams": ["edm"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["favorite_teams"] == ["EDM"] and body["configured_teams"] == ["TOR"]
    assert runtime.config.favorite_teams == ["EDM"]
    assert runtime.poller_calls == 1
    assert body["screen"]["id"] != "next_game:TOR"
    assert json.loads(runtime.state_path.read_text()) == {"favorite_teams": ["EDM"]}


def test_unknown_team_rejected(api, runtime):
    resp = api.put("/control/teams", json={"teams": ["XYZ"]})
    assert resp.status_code == 400
    assert runtime.config.favorite_teams == ["TOR"]


def test_reset_restores_config_and_clears_state(api, runtime):
    api.put("/control/teams", json={"teams": ["EDM"]})
    resp = api.put("/control/teams", json={"teams": None})
    assert resp.json()["favorite_teams"] == ["TOR"]
    assert not runtime.state_path.exists()


def test_switch_survives_restart(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"favorite_teams": ["EDM"]}))
    rt = Runtime(Config(favorite_teams=["TOR"]), state_path=state)
    assert rt.config.favorite_teams == ["EDM"] and rt.configured_teams == ["TOR"]


def test_switch_shows_new_teams_live_game_immediately(api, runtime):
    game = runtime.store.games[TOR_GAME]
    game.state, game.period = GameState.LIVE, 2
    game.clock = Clock(time_remaining="08:00", seconds_remaining=480, running=True)
    runtime.config.favorite_teams = ["EDM"]
    runtime.scheduler.reset()
    assert runtime.tick().type != "live_game"
    screen = api.put("/control/teams", json={"teams": ["UTA"]}).json()["screen"]
    assert screen["id"] == f"live_game:{TOR_GAME}"


# --------------------------------------------------------------------------- CLI


def test_cli_end_to_end(api, monkeypatch, capsys):
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: contextlib.nullcontext(api))
    assert cli.displayteam(["oilers"]) == 0
    out = capsys.readouterr().out
    assert "Displaying: EDM (Oilers)" in out and "--reset to restore" in out
    assert cli.displayteam([]) == 0
    assert "EDM" in capsys.readouterr().out
    assert cli.displayteam(["--reset"]) == 0
    assert "Displaying: TOR (Maple Leafs)" in capsys.readouterr().out


def test_cli_unknown_team_suggests(capsys):
    assert cli.displayteam(["TRO"]) == 2
    assert "Did you mean TOR" in capsys.readouterr().err


def test_cli_backend_down(capsys):
    assert cli.displayteam(["--url", "http://127.0.0.1:9", "TOR"]) == 1
    assert "Is it running?" in capsys.readouterr().err


# --------------------------------------------------------------------------- poller tracking


class StubClient:
    """Serves fixtures; the TOR @ UTA game is live."""

    def __init__(self, fixture):
        self.score = fixture("score.json")
        game = next(g for g in self.score["games"] if g["id"] == TOR_GAME)
        game["gameState"] = "LIVE"
        self.pbp = fixture("pbp_final.json")
        self.pbp["gameState"] = "LIVE"
        self.schedule = fixture("schedule_tor.json")
        self.schedule_requests: list[str] = []

    async def score_now(self):
        return self.score

    async def club_schedule(self, abbrev):
        self.schedule_requests.append(abbrev)
        return self.schedule

    async def play_by_play(self, game_id):
        return self.pbp

    async def right_rail(self, game_id):
        return {"teamGameStats": []}


async def test_poller_retargets_live_tracking(fixture):
    rt = Runtime(Config(favorite_teams=["EDM"]))
    rt.poller.client = StubClient(fixture)
    try:
        await rt.poller.refresh_scoreboard()
        assert TOR_GAME not in rt.poller._tracked

        await rt.set_favorite_teams(["TOR"])
        assert TOR_GAME in rt.poller._tracked
        assert rt.poller.client.schedule_requests == ["TOR"]
        await asyncio.sleep(0.05)  # first play-by-play poll seeds silently: no goal flood
        assert TOR_GAME in rt.store.details
        assert not [e for e in rt.store.recent_events if e.kind == "goal"]

        await rt.set_favorite_teams(["EDM"])
        assert TOR_GAME not in rt.poller._tracked
    finally:
        await rt.poller.stop()


# --------------------------------------------------------------------------- web picker


def test_root_opens_control_page(api):
    resp = api.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307) and resp.headers["location"] == "/debug"
    assert 'id="teamGrid"' in api.get("/").text


def test_team_list_for_picker(api, runtime):
    game = runtime.store.games[TOR_GAME]
    game.state, game.period = GameState.LIVE, 2
    game.clock = Clock(time_remaining="08:00", seconds_remaining=480, running=True)
    upcoming = runtime.store.games[2026020104]  # EDM @ LAK
    upcoming.state = GameState.FUTURE
    teams = {t["abbrev"]: t for t in api.get("/control/teams").json()["teams"]}
    assert len(teams) == 32
    assert teams["TOR"]["division"] == "Atlantic" and teams["TOR"]["conference"] == "Eastern"
    assert teams["TOR"]["game"] == {"state": "live", "label": "2nd 08:00"} == teams["UTA"]["game"]
    assert teams["EDM"]["game"]["state"] == "upcoming" and teams["EDM"]["game"]["label"].endswith("PM")
    assert teams["BOS"]["game"] is None  # already final: nothing to switch to
    assert len(teams["TOR"]["color"]) == 3


def test_team_change_is_pushed_to_open_pages(api):
    with api.websocket_connect("/events") as ws:
        api.put("/control/teams", json={"teams": ["EDM"]})
        messages = [ws.receive_json() for _ in range(2)]
    teams_msgs = [m for m in messages if m["type"] == "teams"]
    assert teams_msgs and teams_msgs[0]["favorite_teams"] == ["EDM"]
