""""Show now" buttons: play a picked screen next, then resume the rotation from the top."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nhlscreen.api.app import create_app
from nhlscreen.config import Config, DisplayConfig, RotationEntry
from nhlscreen.nhl import parsers
from nhlscreen.nhl.models import Clock, GameEvent, GameState
from nhlscreen.runtime import Runtime
from nhlscreen.services.scheduler import Scheduler
from nhlscreen.store import Store
from nhlscreen import views

T0 = datetime(2026, 10, 14, 23, 30, tzinfo=timezone.utc)
TOR_GAME = 2026020102


def _store(fixture) -> Store:
    s = Store()
    s.set_games(parsers.parse_score(fixture("score.json")))
    s.set_standings(parsers.parse_standings(fixture("standings.json")))
    s.set_schedule("TOR", parsers.parse_schedule(fixture("schedule_tor.json")))
    s.set_leaders(parsers.parse_leaders(fixture("skater_leaders.json")))
    for pid in (8486067, 8475683):
        s.set_player(parsers.parse_player(fixture(f"player_{pid}.json")))
    return s


def _config(**display) -> Config:
    return Config(favorite_teams=["TOR"], favorite_players=[8486067, 8475683], display=DisplayConfig(**display))


def test_forced_screens_play_in_order_then_rotation_restarts(fixture):
    store = _store(fixture)
    sched = Scheduler(store, _config(rotation=[RotationEntry(screen="next_game", dwell=5),
                                               RotationEntry(screen="standings", dwell=5)]))
    sched.tick(T0)
    pages = views.standings_pages(store.standings, "league", ["TOR"], 3)
    sched.force(pages[:2])
    assert sched.tick(T0 + timedelta(seconds=1)).id == "standings:league:0"
    assert sched.tick(T0 + timedelta(seconds=3)).id == "standings:league:0"  # holds its dwell
    assert sched.tick(T0 + timedelta(seconds=4)).id == "standings:league:1"
    assert sched.tick(T0 + timedelta(seconds=7)).id == "next_game:TOR"  # rotation from the top


def test_forced_screen_beats_a_live_game_and_clears_alerts(fixture):
    store = _store(fixture)
    game = store.games[TOR_GAME]
    game.state, game.period = GameState.LIVE, 2
    game.clock = Clock(time_remaining="10:00", seconds_remaining=600, running=True)
    sched = Scheduler(store, _config())
    assert sched.tick(T0).type == "live_game"
    sched.on_event(GameEvent(kind="goal", game_id=TOR_GAME, key="g", team_abbrev="TOR", away_abbrev="TOR",
                             home_abbrev="UTA", away_score=1, home_score=0, occurred_at=T0))
    standings = views.standings_screen(store.standings, "TOR", "division", None, 4)
    sched.force([standings])
    assert sched.tick(T0 + timedelta(seconds=1)).id == standings.id
    assert sched.tick(T0 + timedelta(seconds=6)).type == "live_game"  # queued goal alert was cleared


@pytest.fixture
def runtime(fixture):
    rt = Runtime(_config())
    loaded = _store(fixture)
    rt.store.games, rt.store.standings = loaded.games, loaded.standings
    rt.store.schedules, rt.store.leaders, rt.store.players = loaded.schedules, loaded.leaders, loaded.players

    async def pbp(game_id):
        return fixture("pbp_final.json")

    async def landing(game_id):
        return fixture("landing_final.json")

    rt.client.play_by_play, rt.client.landing = pbp, landing  # no network in tests
    return rt


def test_menu_lists_what_can_be_shown(runtime):
    menu = {m["id"]: m for m in runtime.show_menu()}
    assert list(menu) == ["next_game", "scoreboard", "standings_division", "standings_wildcard",
                          "standings_league", "leaders", "player_card", "goal_alert", "final_alert"]
    assert all(m["available"] for m in menu.values())
    runtime.store.players = {}
    item = next(m for m in runtime.show_menu() if m["id"] == "player_card")
    assert not item["available"] and item["reason"]


async def test_show_league_plays_every_page(runtime):
    screens = await runtime.show_now("standings_league")
    assert [s.id for s in screens] == [f"standings:league:{n}" for n in range(4)]
    assert runtime.screen.id == "standings:league:0"


async def test_goal_alert_replays_the_teams_latest_real_goal(runtime):
    [screen] = await runtime.show_now("goal_alert")
    assert screen.type == "goal_alert" and screen.lines[0] == "TOR GOAL!"
    assert screen.lines[1].startswith("G. McKenna")  # TOR's last goal at UTA, 2026-10-13
    assert runtime.screen.id == screen.id


async def test_final_alert_replays_the_latest_final(runtime):
    [screen] = await runtime.show_now("final_alert")
    assert screen.type == "final_alert" and screen.lines[1] == "TOR 4  UTA 5"
    assert screen.data["three_stars"][0]["name"] == "M. Sergachev"


async def test_bad_requests(runtime):
    with pytest.raises(ValueError, match="unknown screen"):
        await runtime.show_now("nope")
    runtime.store.players = {}
    with pytest.raises(ValueError, match="Nothing to show for Player cards"):
        await runtime.show_now("player_card")


def test_api(runtime):
    with TestClient(create_app(runtime, manage_lifecycle=False)) as api:
        assert len(api.get("/control/screens").json()["screens"]) == 9
        resp = api.post("/control/show", json={"screen": "standings_wildcard"})
        assert resp.status_code == 200 and resp.json()["showing"] == ["wildcard:Eastern"]
        assert resp.json()["screen"]["id"] == "wildcard:Eastern"
        assert api.post("/control/show", json={"screen": "nope"}).status_code == 400
        assert 'id="showNow"' in api.get("/debug").text
