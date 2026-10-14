"""LED matrix rendering: fonts, canvas, layouts and the /render endpoints."""

import logging
import struct
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from nhlscreen import views
from nhlscreen.api.app import create_app
from nhlscreen.config import Config
from nhlscreen.nhl import parsers
from nhlscreen.nhl.models import Clock, GameEvent, GameState, Situation
from nhlscreen.render import Canvas, render
from nhlscreen.render.canvas import GOLD, WHITE
from nhlscreen.render.fonts import REGULAR, SMALL
from nhlscreen.render.layouts import HIGHLIGHT, fit_name
from nhlscreen.runtime import Runtime
from nhlscreen.static.teams import get_team

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 10, 14, 23, 30, tzinfo=timezone.utc)


def test_fonts_cover_the_same_characters():
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,:;-+/()!?#%@'\"&=_*<>"
    for font in (REGULAR, SMALL):
        for ch in chars:
            assert font.glyph(ch) is not font.glyph("?") or ch == "?", (font.name, ch)
    assert REGULAR.measure("1") == REGULAR.measure("8")  # tabular digits
    assert REGULAR.measure("I") < REGULAR.measure("M")  # proportional letters
    assert REGULAR.normalize("Lafrenière") == "LAFRENIERE"


def test_fit_name():
    assert fit_name("C. McDavid", REGULAR, 200) == "C. McDavid"
    assert fit_name("N. MacKinnon", REGULAR, REGULAR.measure("MACKINNON")) == "MacKinnon"
    assert REGULAR.measure(fit_name("N. MacKinnon", REGULAR, 30)) <= 30


def test_png_output():
    png = Canvas(128, 64).to_png(pitch=4)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (512, 256)


@pytest.fixture
def screens(fixture):
    games = parsers.parse_score(fixture("score.json"))
    standings = parsers.parse_standings(fixture("standings.json"))
    schedule = parsers.parse_schedule(fixture("schedule_tor.json"))
    detail = parsers.parse_play_by_play(fixture("pbp_final.json"))
    detail.team_stats = parsers.parse_right_rail(fixture("rightrail_final.json"))
    detail.three_stars = parsers.parse_three_stars(fixture("landing_final.json"))

    live = detail.game.model_copy(deep=True)
    live.state, live.period = GameState.LIVE, 2
    live.clock = Clock(time_remaining="05:16", seconds_remaining=316, running=True)
    live.situation = Situation(away_skaters=5, home_skaters=4, time_remaining="01:23")
    goal = detail.game.goals[3]
    goal_event = GameEvent(kind="goal", game_id=live.id, key="g", team_abbrev="TOR", away_abbrev="TOR",
                           home_abbrev="UTA", away_score=1, home_score=3, goal=goal, occurred_at=NOW)
    final = GameEvent(kind="game_final", game_id=live.id, key="f", away_abbrev="TOR", home_abbrev="UTA",
                      away_score=4, home_score=5, occurred_at=NOW)
    nxt = min((g for g in schedule if g.state is GameState.FUTURE), key=lambda g: g.start_time_utc)
    last = max((g for g in schedule if g.state.is_final), key=lambda g: g.start_time_utc)
    return {
        "live_game": views.live_game_screen(live, detail, TZ, 15, NOW, as_of=NOW),
        "goal_alert": views.goal_alert_screen(goal_event, 15),
        "final_alert": views.final_alert_screen(final, detail.game, detail, 20),
        "goal_overturned": views.overturned_screen(final, 15),
        "scoreboard": views.scoreboard_screens(games, 4, TZ, 8)[0],
        "standings": views.standings_screen(standings, "TOR", "division", None, 10),
        "standings_league": views.standings_screen(standings, "TOR", "league", None, 10),
        "leaders": views.leaders_screen("points", parsers.parse_leaders(fixture("skater_leaders.json"))["points"], 5, 8),
        "next_game": views.next_game_screen("TOR", nxt, last, standings, TZ, NOW, 10),
        "player_card": views.player_card_screen(parsers.parse_player(fixture("player_8478483.json")), 8),
        "goalie_card": views.player_card_screen(parsers.parse_player(fixture("player_8480280.json")), 8),
        "idle": views.idle_screen(NOW, TZ),
    }


def _colors_in(cv: Canvas, x0: int, y0: int, x1: int, y1: int) -> set:
    return {cv.get(x, y) for x in range(x0, x1) for y in range(y0, y1)} - {(0, 0, 0)}


def test_every_screen_renders_without_falling_back(screens, caplog):
    caplog.set_level(logging.ERROR)
    for name, screen in screens.items():
        cv = render(screen.model_dump(mode="json"), NOW)
        assert len(cv.to_bytes()) == 128 * 64 * 3
        assert any(cv.buf), f"{name} drew nothing"
    assert not [r for r in caplog.records if "layout for" in r.getMessage()]


def test_live_game_layout_colors(screens):
    cv = render(screens["live_game"].model_dump(mode="json"), NOW)
    assert WHITE in _colors_in(cv, 0, 0, 20, 9)  # TOR is away: white
    assert get_team("UTA").display in _colors_in(cv, 100, 0, 128, 9)  # UTA home color
    assert GOLD in _colors_in(cv, 0, 10, 30, 26)  # big score in gold


def test_live_clock_counts_down_between_frames(screens):
    data = screens["live_game"].model_dump(mode="json")
    first = render(data, NOW)
    later = render(data, NOW + timedelta(seconds=5))
    clock_region = (36, 20, 92, 34)  # 2x clock between the scores
    assert _colors_in(first, *clock_region) and first.buf != later.buf


def test_standings_highlights_followed_team(screens):
    cv = render(screens["standings"].model_dump(mode="json"), NOW)
    tor_row = 17 + 5 * 6  # TOR is 6th in the Atlantic fixture
    assert cv.get(127, tor_row) == HIGHLIGHT


def test_render_endpoints(fixture):
    rt = Runtime(Config(favorite_teams=["TOR"]))
    rt.store.set_standings(parsers.parse_standings(fixture("standings.json")))
    with TestClient(create_app(rt, manage_lifecycle=False)) as api:
        frame = api.get("/render/frame")
        assert frame.status_code == 200 and len(frame.content) == 128 * 64 * 3
        assert frame.headers["x-width"] == "128" and frame.headers["x-height"] == "64"
        png = api.get("/render/frame.png", params={"pitch": 3})
        assert png.headers["content-type"] == "image/png" and png.content.startswith(b"\x89PNG")
        assert 'id="led"' in api.get("/debug").text


def test_live_clock_shrinks_when_two_digit_scores_crowd_it(screens):
    data = screens["live_game"].model_dump(mode="json")
    data["data"]["away"]["score"] = 10
    cv = render(data, NOW)
    # 2x clock would occupy rows 20-33; the 1x fallback sits at rows 24-30.
    assert not _colors_in(cv, 40, 20, 88, 24) and GOLD in _colors_in(cv, 40, 24, 88, 31)


def test_goal_alert_has_white_divider_under_banner(screens):
    cv = render(screens["goal_alert"].model_dump(mode="json"), NOW)
    rules = [y for y in range(cv.height) if all(cv.get(x, y) == WHITE for x in range(2, cv.width - 2))]
    assert len(rules) == 1
    banner_rows = [y for y in range(cv.height) if get_team("TOR").display in _colors_in(cv, 0, y, cv.width, y + 1)]
    assert min(banner_rows) < rules[0]  # banner above the divider, names below it
    assert any(y > rules[0] for y in banner_rows)


def _gold_rows(cv: Canvas, y0: int, y1: int) -> list[int]:
    return [y for y in range(y0, y1) if any(cv.get(x, y) == GOLD for x in range(cv.width))]


@pytest.mark.parametrize("card", ["player_card", "goalie_card"])
def test_player_card_values_share_one_size(screens, card):
    cv = render(screens[card].model_dump(mode="json"), NOW)
    # every stat value (GP ... SV%, GAA) sits in the same 7-row regular-font band
    assert _gold_rows(cv, 20, 42) and set(_gold_rows(cv, 20, 42)) <= set(range(31, 38))


def test_skater_last_game_uses_regular_font(screens):
    cv = render(screens["player_card"].model_dump(mode="json"), NOW)
    assert WHITE in _colors_in(cv, 0, 44, 128, 51)  # "LAST GAME"
    assert set(_gold_rows(cv, 52, 64)) <= set(range(54, 61)) and len(_gold_rows(cv, 52, 64)) == 7  # "1G 0A"


def test_last_game_lines():
    from nhlscreen.render.layouts import last_game_spans

    text = lambda spans: "".join(s["text"] for s in spans)  # noqa: E731
    goalie = {"decision": "W", "goalsAgainst": 3, "shotsAgainst": 33, "savePctg": 0.909091,
              "homeRoadFlag": "R", "opponentAbbrev": "COL"}
    assert text(last_game_spans(goalie)) == "W  30 SV  .909  @ COL"
    assert text(last_game_spans({**goalie, "decision": "O", "homeRoadFlag": "H"})) == "OTL  30 SV  .909  VS COL"
    assert text(last_game_spans({**goalie, "decision": None})) == "30 SV  .909  @ COL"  # no decision (relief)
    assert text(last_game_spans(goalie, max_width=90)) == "W  30 SV  @ COL"  # too narrow: save % dropped
    skater = {"goals": 1, "assists": 0, "homeRoadFlag": "R", "opponentAbbrev": "UTA"}
    assert text(last_game_spans(skater)) == "1G 0A  @ UTA"
    assert last_game_spans({}) is None


def test_goalie_card_shows_last_game(screens):
    cv = render(screens["goalie_card"].model_dump(mode="json"), NOW)
    assert WHITE in _colors_in(cv, 0, 44, 128, 51)  # "LAST GAME"
    assert set(_gold_rows(cv, 52, 64)) == set(range(54, 61))  # W / saves / save % in regular size
