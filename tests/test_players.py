"""Switching favorite players at runtime (preview page -> PUT /control/players)."""

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nhlscreen.api.app import create_app
from nhlscreen.config import Config
from nhlscreen.nhl.client import NHLClient, NHLNotFound, NHLUnavailable
from nhlscreen.runtime import Runtime

KNOWN = {8486067: "player_8486067.json", 8475683: "player_8475683.json", 8478483: "player_8478483.json"}


@pytest.fixture
def runtime(fixture, tmp_path):
    rt = Runtime(Config(favorite_teams=["TOR"], favorite_players=[8486067, 8475683]),
                 state_path=tmp_path / "state.json")
    rt.lookups = []

    async def fake_player(pid):  # no network in tests
        rt.lookups.append(pid)
        if pid == 1:
            raise NHLUnavailable("down")
        if pid not in KNOWN:
            raise NHLNotFound(f"/player/{pid}/landing: HTTP 404")
        return fixture(KNOWN[pid])

    rt.client.player = fake_player
    return rt


@pytest.fixture
def api(runtime):
    with TestClient(create_app(runtime, manage_lifecycle=False)) as c:
        yield c


def test_swap_players(api, runtime):
    resp = api.put("/control/players", json={"players": [8478483, 8486067]})
    assert resp.status_code == 200
    favs = resp.json()["favorite_players"]
    assert [(p["id"], p["name"], p["team"]) for p in favs] == [(8478483, "Mitch Marner", "VGK"),
                                                               (8486067, "Gavin McKenna", "TOR")]
    assert runtime.config.favorite_players == [8478483, 8486067]
    assert 8478483 in runtime.store.players  # card data is ready right away
    assert json.loads(runtime.state_path.read_text(encoding="utf-8")) == {"favorite_players": [8478483, 8486067]}


def test_unknown_id_is_rejected_without_changes(api, runtime):
    resp = api.put("/control/players", json={"players": [8486067, 999]})
    assert resp.status_code == 400 and resp.json()["detail"] == "No NHL player with ID 999"
    assert runtime.config.favorite_players == [8486067, 8475683]
    assert not runtime.state_path.exists()


def test_api_outage_gives_a_retry_message(api):
    resp = api.put("/control/players", json={"players": [1]})
    assert resp.status_code == 400 and "try again" in resp.json()["detail"]


def test_single_player_and_reset(api, runtime):
    assert [p["id"] for p in api.put("/control/players", json={"players": [8478483]}).json()["favorite_players"]] == [8478483]
    body = api.put("/control/players", json={"players": None}).json()
    assert [p["id"] for p in body["favorite_players"]] == [8486067, 8475683]
    assert not runtime.state_path.exists()


def test_teams_and_players_are_saved_independently(api, runtime):
    runtime.poller.favorites_changed = _noop
    api.put("/control/players", json={"players": [8478483]})
    api.put("/control/teams", json={"teams": ["EDM"]})
    assert json.loads(runtime.state_path.read_text(encoding="utf-8")) == {"favorite_players": [8478483], "favorite_teams": ["EDM"]}
    api.put("/control/teams", json={"teams": None})  # resetting teams keeps the players
    assert json.loads(runtime.state_path.read_text(encoding="utf-8")) == {"favorite_players": [8478483]}


async def _noop():
    return None


def test_players_survive_restart(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"favorite_players": [8478483]}), encoding="utf-8")
    rt = Runtime(Config(favorite_players=[8486067, 8475683]), state_path=state)
    assert rt.config.favorite_players == [8478483] and rt.configured_players == [8486067, 8475683]


def test_preview_page_has_player_boxes(api):
    html = api.get("/debug").text
    assert 'id="player1"' in html and 'id="player2"' in html and 'id="playersForm"' in html


@respx.mock
async def test_client_reports_missing_player_as_not_found():
    respx.get("https://api-web.nhle.com/v1/player/999/landing").mock(return_value=httpx.Response(404))
    client = NHLClient()
    try:
        with pytest.raises(NHLNotFound):
            await client.player(999)
        assert client.backoff_remaining == 0
    finally:
        await client.aclose()
