"""Screen text is split into label (white) / value (gold) / team spans."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from nhlscreen import views
from nhlscreen.nhl import parsers
from nhlscreen.nhl.models import Clock, GameEvent, GameState, Goal
from nhlscreen.static.teams import WHITE, get_team

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)


def kinds(line):
    """[(kind, text)] without pure-whitespace spans."""
    return [(s["kind"], s["text"].strip()) for s in line if s["text"].strip()]


def test_lines_are_the_joined_spans(fixture):
    standings = parsers.parse_standings(fixture("standings.json"))
    screen = views.standings_screen(standings, "TOR", "division", None, 10)
    assert screen.lines == ["".join(s["text"] for s in line) for line in screen.spans]
    assert screen.lines[1].startswith(" 1. ")


def test_leaders_rank_is_label_name_in_team_color_value_is_gold(fixture):
    leaders = parsers.parse_leaders(fixture("skater_leaders.json"))["points"]
    screen = views.leaders_screen("points", leaders, 5, 8)
    assert kinds(screen.spans[0]) == [("label", "POINTS LEADERS")]
    row = screen.spans[1]
    assert kinds(row) == [("label", "1."), ("player", "C. McDavid"), ("team", "EDM"), ("value", "17")]
    name = next(s for s in row if s["kind"] == "player")
    assert name["team"] == "EDM" and name["color"] == get_team("EDM").display


def test_next_and_last_indicators_are_labels(fixture):
    schedule = parsers.parse_schedule(fixture("schedule_tor.json"))
    standings = parsers.parse_standings(fixture("standings.json"))
    nxt = min((g for g in schedule if g.state is GameState.FUTURE), key=lambda g: g.start_time_utc)
    last = max((g for g in schedule if g.state.is_final), key=lambda g: g.start_time_utc)
    screen = views.next_game_screen("TOR", nxt, last, standings, TZ, NOW, 10)
    assert kinds(screen.spans[0]) == [("label", "NEXT:"), ("label", "vs"), ("team", "BUF")]
    assert kinds(screen.spans[1]) == [("label", "Tomorrow"), ("value", "7:00 PM")]
    assert kinds(screen.spans[2]) == [("label", "LAST:"), ("value", "L 4-5"), ("label", "@"), ("team", "UTA")]


def test_live_game_spans_flag_the_clock(fixture):
    game = next(g for g in parsers.parse_score(fixture("score.json")) if g.id == 2026020102)
    game.state, game.period = GameState.LIVE, 2
    game.clock = Clock(time_remaining="12:34", seconds_remaining=754, running=True)
    screen = views.live_game_screen(game, None, TZ, 15, NOW)
    assert kinds(screen.spans[0]) == [("team", "TOR"), ("value", "4"), ("team", "UTA"), ("value", "5")]
    assert kinds(screen.spans[1]) == [("value", "2nd"), ("value", "12:34")]  # period names are stats
    assert [s for s in screen.spans[1] if s.get("clock")] == [{"kind": "value", "text": "12:34", "clock": True}]
    assert kinds(screen.spans[2]) == [("label", "SOG"), ("value", "26-28")]


def test_scoreboard_status_kinds(fixture):
    games = [g for g in parsers.parse_score(fixture("score.json")) if g.id in (2026020090, 2026020102)]
    upcoming = games[1].model_copy(deep=True)
    upcoming.state = GameState.FUTURE
    screen = views.scoreboard_screens([games[0], upcoming], 4, TZ, 8)[0]
    assert kinds(screen.spans[0])[-1] == ("value", "FINAL/OT")  # game state: a stat
    assert kinds(screen.spans[1])[-1] == ("value", "9:00 PM")  # start time: a value


def test_pregame_scoreboard_row_says_warmup(fixture):
    # A tracked game's play-by-play reports 0-0 during warmups, before the state goes LIVE.
    game = next(g for g in parsers.parse_score(fixture("score.json")) if g.id == 2026020102).model_copy(deep=True)
    game.state, game.away.score, game.home.score = GameState.PREGAME, 0, 0
    screen = views.scoreboard_screens([game], 4, TZ, 8)[0]
    assert kinds(screen.spans[0])[-1] == ("value", "WARMUP")
    assert screen.data["games"][0]["status"] == "WARMUP"


def test_live_game_team_spans_use_home_away_colors(fixture):
    game = next(g for g in parsers.parse_score(fixture("score.json")) if g.id == 2026020102)  # TOR @ UTA
    game.state, game.period = GameState.LIVE, 2
    game.clock = Clock(time_remaining="12:34", seconds_remaining=754, running=True)
    teams = [s for s in views.live_game_screen(game, None, TZ, 15, NOW).spans[0] if s["kind"] == "team"]
    assert [(t["text"], t["color"]) for t in teams] == [("TOR", WHITE), ("UTA", get_team("UTA").display)]


def test_goal_alert_player_names_use_team_color_even_when_away():
    goal = Goal(period=2, time_in_period="05:16", team_abbrev="TOR", scorer_name="A. Matthews", scorer_season_total=5,
                assists=["J. Tavares", "W. Nylander"], strength="pp", away_score=1, home_score=3)
    event = GameEvent(kind="goal", game_id=1, key="g", team_abbrev="TOR", away_abbrev="TOR", home_abbrev="UTA",
                      away_score=1, home_score=3, goal=goal, occurred_at=NOW)
    screen = views.goal_alert_screen(event, 15)
    assert {s["color"] for s in screen.spans[0]} == {get_team("TOR").display}  # banner: own color
    assert [s["color"] for s in screen.spans[-1] if s["kind"] == "team"][0] == WHITE  # score line: away
    players = [s for line in screen.spans for s in line if s["kind"] == "player"]
    assert [p["text"] for p in players] == ["A. Matthews", "J. Tavares", "W. Nylander"]
    assert all(p["color"] == get_team("TOR").display for p in players)
    assert screen.lines[1] == "A. Matthews (5) PPG" and screen.lines[2] == "A: J. Tavares, W. Nylander"


def test_hat_trick_alert_title_on_third_goal_only():
    def goal(eid, scorer, n):
        return Goal(event_id=eid, period=1, time_in_period="05:00", team_abbrev="TOR", scorer_id=scorer,
                    scorer_name="A. Matthews" if scorer == 1 else "W. Nylander", scorer_season_total=n,
                    assists=["J. Tavares"], strength="ev", away_score=eid, home_score=0)
    goals = [goal(1, 1, 1), goal(2, 2, 1), goal(3, 1, 2), goal(4, 1, 3), goal(5, 1, 4)]

    def alert(g):
        event = GameEvent(kind="goal", game_id=1, key=f"g{g.event_id}", team_abbrev="TOR", away_abbrev="TOR",
                          home_abbrev="UTA", away_score=g.away_score, home_score=0, goal=g, occurred_at=NOW)
        return views.goal_alert_screen(event, 25, goals)

    assert [alert(g).lines[0] for g in goals] == ["TOR GOAL!", "TOR GOAL!", "TOR GOAL!", "HAT TRICK!", "TOR GOAL!"]
    hat = alert(goals[3])
    assert hat.data["hat_trick"] and hat.lines[1:3] == ["A. Matthews (3)", "A: J. Tavares"]
    assert {s["color"] for s in hat.spans[0]} == {get_team("TOR").display}
    # Without the game's goal list (scoreboard-only goals) it stays a plain goal alert.
    event = GameEvent(kind="goal", game_id=1, key="g", team_abbrev="TOR", away_abbrev="TOR", home_abbrev="UTA",
                      away_score=4, home_score=0, goal=goals[3], occurred_at=NOW)
    assert views.goal_alert_screen(event, 25).lines[0] == "TOR GOAL!"


def test_player_card_and_final_star_names(fixture):
    card = views.player_card_screen(parsers.parse_player(fixture("player_8478483.json")), 8)
    assert kinds(card.spans[0]) == [("label", "#93"), ("player", "Mitch Marner")]
    assert card.spans[0][1]["color"] == get_team("VGK").display

    game = next(g for g in parsers.parse_score(fixture("score.json")) if g.id == 2026020102)
    detail = parsers.parse_play_by_play(fixture("pbp_final.json"))
    detail.three_stars = parsers.parse_three_stars(fixture("landing_final.json"))
    final = GameEvent(kind="game_final", game_id=game.id, key="f", away_abbrev="TOR", home_abbrev="UTA",
                      away_score=4, home_score=5, occurred_at=NOW)
    screen = views.final_alert_screen(final, game, detail, 20)
    assert kinds(screen.spans[0]) == [("value", "FINAL")]
    star = screen.spans[2][-1]
    assert star["kind"] == "player" and star["text"] == "M. Sergachev" and star["color"] == get_team("UTA").display


def test_wins_leaders_title_says_goalie(fixture):
    from nhlscreen.render import render
    from nhlscreen.render.fonts import REGULAR

    wins = parsers.parse_leaders(fixture("goalie_leaders.json"))["wins"]
    screen = views.leaders_screen("wins", wins, 5, 8)
    assert screen.lines[0] == "GOALIE WINS LEADERS"
    assert REGULAR.measure(screen.lines[0]) <= 126  # fits the panel without the short-label fallback
    assert any(render(screen.model_dump(mode="json"), NOW).buf)
