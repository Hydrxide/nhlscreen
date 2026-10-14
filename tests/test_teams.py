import colorsys
from datetime import datetime, timezone

from nhlscreen.nhl import parsers
from nhlscreen.nhl.models import GameEvent
from nhlscreen.static.teams import LED_OVERRIDES, TEAMS, WHITE, get_team
from nhlscreen import views

from zoneinfo import ZoneInfo


def _hsv(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))


def test_display_colors_are_bright_enough_for_leds():
    for team in TEAMS.values():
        assert max(team.display) >= 0.7 * 255 - 1, team.abbrev


def test_display_colors_keep_the_team_hue():
    """No team falls back to white, and dark primaries keep their hue."""
    for team in TEAMS.values():
        assert team.display != WHITE, team.abbrev
        if team.abbrev in LED_OVERRIDES:
            continue
        h_primary, s_primary, _ = _hsv(team.primary)
        if s_primary >= 0.2:
            assert abs(_hsv(team.display)[0] - h_primary) < 0.01, team.abbrev


def test_specific_teams():
    assert get_team("TOR").display == (0, 63, 178)  # brightened navy -> royal blue
    assert get_team("DET").display == get_team("DET").primary  # already bright red
    lak = get_team("LAK").display  # black primary -> its silver, lifted
    assert max(lak) - min(lak) < 15 and max(lak) >= 178
    assert get_team("EDM").display == (255, 76, 0)  # override: orange


def test_jersey_convention():
    tor = get_team("TOR")
    assert tor.game_color("home") == tor.display
    assert tor.game_color("away") == WHITE


def test_live_game_uses_home_away_colors(fixture):
    game = next(g for g in parsers.parse_score(fixture("score.json")) if g.id == 2026020102)  # TOR @ UTA
    screen = views.live_game_screen(game, None, ZoneInfo("America/Toronto"), 15, game.start_time_utc)
    assert screen.data["away"]["colors"]["game"] == WHITE and screen.data["away"]["side"] == "away"
    assert screen.data["home"]["colors"]["game"] == get_team("UTA").display
    assert screen.data["away"]["colors"]["display"] == get_team("TOR").display  # own color still there



def test_scoreboard_and_final_use_own_colors(fixture):
    """Home/away colors are for live game info only."""
    game = next(g for g in parsers.parse_score(fixture("score.json")) if g.id == 2026020102)  # TOR @ UTA
    rows = views.scoreboard_screens([game], 4, ZoneInfo("America/Toronto"), 8)[0].data["games"]
    assert "game" not in rows[0]["away"]["colors"] and "game" not in rows[0]["home"]["colors"]

    final = GameEvent(kind="game_final", game_id=game.id, key="final", away_abbrev="TOR", home_abbrev="UTA",
                      away_score=4, home_score=5, occurred_at=game.start_time_utc)
    data = views.final_alert_screen(final, game, None, 20).data
    assert "game" not in data["away"]["colors"] and "game" not in data["home"]["colors"]


def test_goal_alert_uses_home_away_colors(fixture):
    goal = GameEvent(kind="goal", game_id=1, key="g", team_abbrev="TOR", away_abbrev="TOR", home_abbrev="UTA",
                     away_score=1, home_score=0, occurred_at=datetime.now(timezone.utc))
    screen = views.goal_alert_screen(goal, 15)
    # The "TOR GOAL!" banner is always the scoring team's own color, even away...
    assert {s["color"] for s in screen.spans[0]} == {get_team("TOR").display}
    # ...while the score line follows home/away.
    assert screen.data["away"]["colors"]["game"] == WHITE
    assert screen.data["home"]["colors"]["game"] == get_team("UTA").display


def test_single_team_screens_have_no_game_color(fixture):
    standings = parsers.parse_standings(fixture("standings.json"))
    screen = views.standings_screen(standings, "TOR", "division", None, 10)
    assert all("game" not in r["team"]["colors"] for r in screen.data["rows"])
