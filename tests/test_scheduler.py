from datetime import datetime, timedelta, timezone

import pytest

from nhlscreen.config import Config, DisplayConfig, RotationEntry
from nhlscreen.nhl import parsers
from nhlscreen.nhl.models import Clock, GameEvent, GameState
from nhlscreen.services.scheduler import Scheduler
from nhlscreen.store import Store

T0 = datetime(2026, 10, 14, 23, 30, tzinfo=timezone.utc)
TOR_GAME = 2026020102


@pytest.fixture
def store(fixture) -> Store:
    s = Store()
    s.set_games(parsers.parse_score(fixture("score.json")))
    s.set_standings(parsers.parse_standings(fixture("standings.json")))
    s.set_schedule("TOR", parsers.parse_schedule(fixture("schedule_tor.json")))
    s.set_leaders(parsers.parse_leaders(fixture("skater_leaders.json")))
    s.set_player(parsers.parse_player(fixture("player_8478483.json")))
    return s


def make_config(**display) -> Config:
    return Config(
        favorite_teams=["TOR"],
        favorite_players=[8478483],
        display=DisplayConfig(**display),
    )


def run(sched: Scheduler, seconds: float, step: float = 1.0) -> list[str]:
    """Tick through time and return the sequence of distinct screen ids."""
    ids: list[str] = []
    t = 0.0
    while t <= seconds:
        screen = sched.tick(T0 + timedelta(seconds=t))
        if not ids or ids[-1] != screen.id:
            ids.append(screen.id)
        t += step
    return ids


def goal_event(team="TOR", game_id=TOR_GAME, away="TOR", home="UTA") -> GameEvent:
    return GameEvent(kind="goal", game_id=game_id, key=f"goal:{game_id}:1", team_abbrev=team,
                     away_abbrev=away, home_abbrev=home, away_score=1, home_score=0, occurred_at=T0)


def test_rotation_follows_config_and_dwell(store):
    cfg = make_config(rotation=[
        RotationEntry(screen="next_game", dwell=10),
        RotationEntry(screen="standings", dwell=5),
        RotationEntry(screen="leaders", dwell=5),
        RotationEntry(screen="player_card", dwell=5),
    ])
    cfg.leaders.goalie_categories = []
    ids = run(Scheduler(store, cfg), 40)
    assert ids[:7] == [
        "next_game:TOR",
        "standings:division:TOR",
        "leaders:points",
        "leaders:goals",
        "leaders:assists",
        "player_card:8478483",
        "next_game:TOR",  # wraps around
    ]


def test_scoreboard_pages(store):
    cfg = make_config(scoreboard_games_per_page=4, rotation=[RotationEntry(screen="scoreboard", dwell=2)])
    ids = run(Scheduler(store, cfg), 9)
    assert ids[:4] == ["scoreboard:0", "scoreboard:1", "scoreboard:2", "scoreboard:3"]


def test_empty_slots_are_skipped(store):
    cfg = make_config(rotation=[RotationEntry(screen="player_card"), RotationEntry(screen="standings")])
    cfg.favorite_players = [123]  # not loaded in the store
    assert Scheduler(store, cfg).tick(T0).id == "standings:division:TOR"


def test_idle_when_nothing_to_show():
    cfg = make_config(rotation=[RotationEntry(screen="standings")])
    assert Scheduler(Store(), cfg).tick(T0).type == "idle"


def _go_live(store: Store, intermission: bool = False) -> None:
    game = store.games[TOR_GAME]
    game.state = GameState.LIVE
    game.period = 2
    game.clock = Clock(time_remaining="12:34", seconds_remaining=754, running=not intermission,
                       in_intermission=intermission)


def test_live_favorite_game_takes_over(store):
    sched = Scheduler(store, make_config())
    assert sched.tick(T0).type == "next_game"
    _go_live(store)
    screen = sched.tick(T0 + timedelta(seconds=1))
    assert screen.id == f"live_game:{TOR_GAME}"
    assert screen.data["status"] == "2nd 12:34"
    assert screen.data["away"]["abbrev"] == "TOR"


def test_intermission_rotates(store):
    _go_live(store, intermission=True)
    assert Scheduler(store, make_config()).tick(T0).type == "next_game"
    assert Scheduler(store, make_config(rotate_during_intermission=False)).tick(T0).type == "live_game"


def _period_end(period: int, period_type: str = "REG", score: tuple[int, int] = (1, 2)) -> GameEvent:
    return GameEvent(kind="period_end", game_id=TOR_GAME, key=f"period_end:{TOR_GAME}:{period}", away_abbrev="TOR",
                     home_abbrev="UTA", away_score=score[0], home_score=score[1], period=period,
                     period_type=period_type, occurred_at=T0)


def test_intermission_screen_after_first_and_second(store):
    _go_live(store)  # clock still running: the period-end play can land before the feed flips to INT
    store.games[TOR_GAME].away.sog, store.games[TOR_GAME].home.sog = 11, 9
    sched = Scheduler(store, make_config())
    sched.tick(T0)
    sched.on_event(_period_end(2))
    screen = sched.tick(T0 + timedelta(seconds=1))
    assert screen.type == "intermission" and screen.dwell_seconds == 20
    assert screen.data["period_label"] == "2nd" and screen.data["clock"]["in_intermission"]
    assert screen.data["situation"]["power_play"] is None
    assert screen.lines[0].startswith("TOR") and "SOG 11-9" in screen.lines
    assert sched.tick(T0 + timedelta(seconds=20)).type == "intermission"
    assert sched.tick(T0 + timedelta(seconds=22)).type != "intermission"


def test_no_intermission_screen_after_decided_third_or_ot(store):
    _go_live(store)
    sched = Scheduler(store, make_config())
    sched.tick(T0)
    sched.on_event(_period_end(3))
    sched.on_event(_period_end(4, "OT"))
    sched.on_event(_period_end(5, "SO", (2, 2)))
    assert sched.tick(T0 + timedelta(seconds=1)).type == "live_game"


@pytest.mark.parametrize("period,period_type,label", [(3, "REG", "3rd"), (4, "OT", "OT")])
def test_intermission_screen_when_tied_heading_to_ot_or_shootout(store, period, period_type, label):
    _go_live(store)
    sched = Scheduler(store, make_config())
    sched.tick(T0)
    sched.on_event(_period_end(period, period_type, (2, 2)))
    screen = sched.tick(T0 + timedelta(seconds=1))
    assert screen.type == "intermission" and screen.data["period_label"] == label


def test_goal_alert_preempts_then_expires(store):
    _go_live(store)
    sched = Scheduler(store, make_config(alert_seconds=10))
    sched.tick(T0)
    sched.on_event(goal_event())
    alert = sched.tick(T0 + timedelta(seconds=1))
    assert alert.type == "goal_alert" and alert.lines[0] == "TOR GOAL!"
    assert sched.tick(T0 + timedelta(seconds=10)).type == "goal_alert"
    assert sched.tick(T0 + timedelta(seconds=11)).type == "live_game"


def test_goal_alert_picks_up_late_assists(store, fixture):
    # The NHL often posts a goal first and adds its assists a little later.
    detail = parsers.parse_play_by_play(fixture("pbp_final.json"))
    goal = next(g for g in detail.game.goals if g.team_abbrev == "TOR")
    ev = goal_event()
    ev.goal = goal.model_copy(update={"assists": []})
    sched = Scheduler(store, make_config(alert_seconds=25))
    sched.on_event(ev)
    alert = sched.tick(T0)
    assert alert.data["assists"] == [] and not any(l.startswith("A:") for l in alert.lines)

    store.set_detail(detail)
    alert = sched.tick(T0 + timedelta(seconds=5))
    assert alert.type == "goal_alert" and alert.data["assists"] == ["J. Tavares"]
    assert "A: J. Tavares" in alert.lines
    # Still the same alert on the same timer: it ends 25s after it started.
    assert sched.tick(T0 + timedelta(seconds=24)).type == "goal_alert"
    assert sched.tick(T0 + timedelta(seconds=25)).type != "goal_alert"


def test_non_favorite_goal_is_ignored(store):
    sched = Scheduler(store, make_config())
    sched.on_event(goal_event(team="EDM", game_id=1, away="EDM", home="LAK"))
    assert sched.tick(T0).type != "goal_alert"


def test_overturn_cancels_queued_goal_alert(store):
    sched = Scheduler(store, make_config(alert_seconds=10))
    sched.on_event(goal_event())
    first = sched.tick(T0)
    assert first.type == "goal_alert"
    second_goal = goal_event()
    second_goal.key = "goal:x:2"
    sched.on_event(second_goal)
    sched.on_event(GameEvent(kind="goal_overturned", game_id=TOR_GAME, key="o", away_abbrev="TOR",
                             home_abbrev="UTA", occurred_at=T0))
    assert sched.tick(T0 + timedelta(seconds=11)).type == "goal_overturned"


def test_clock_as_of_only_moves_when_reading_changes(store, monkeypatch):
    # Advance the clock on every call: Windows' clock can return the same time twice in a row.
    ticks = iter(T0 + timedelta(seconds=n) for n in range(1000))

    class SteppingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks)

    monkeypatch.setattr("nhlscreen.store.datetime", SteppingClock)
    game = store.games[TOR_GAME].model_copy(deep=True)
    game.state, game.period = GameState.LIVE, 1
    game.clock = Clock(time_remaining="10:00", seconds_remaining=600, running=True)
    store.set_games([game])
    first = store.clock_as_of(TOR_GAME)
    store.set_games([game.model_copy(deep=True)])  # same reading re-fetched
    assert store.clock_as_of(TOR_GAME) == first
    moved = game.model_copy(deep=True)
    moved.clock = Clock(time_remaining="09:50", seconds_remaining=590, running=True)
    store.set_games([moved])
    assert store.clock_as_of(TOR_GAME) > first


def test_rotation_starts_from_the_top_after_idle(fixture):
    store = Store()
    sched = Scheduler(store, make_config(rotation=[RotationEntry(screen="next_game", dwell=5),
                                                   RotationEntry(screen="standings", dwell=5)]))
    assert sched.tick(T0).type == "idle"  # nothing loaded yet
    store.set_standings(parsers.parse_standings(fixture("standings.json")))
    store.set_schedule("TOR", parsers.parse_schedule(fixture("schedule_tor.json")))
    assert sched.tick(T0 + timedelta(seconds=4)).id == "next_game:TOR"  # first entry, not skipped
