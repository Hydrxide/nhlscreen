"""Division / league / wild card standings: which screens appear, and what they show."""

from datetime import datetime, timedelta, timezone

import pytest

from nhlscreen import views
from nhlscreen.config import Config, DisplayConfig, RotationEntry
from nhlscreen.nhl import parsers
from nhlscreen.render import render
from nhlscreen.render.layouts import HIGHLIGHT
from nhlscreen.services.scheduler import Scheduler
from nhlscreen.store import Store

T0 = datetime(2026, 10, 14, 23, 30, tzinfo=timezone.utc)


@pytest.fixture
def standings(fixture):
    return parsers.parse_standings(fixture("standings.json"))


def test_league_pages_cover_all_teams_in_order(standings):
    pages = views.standings_pages(standings, "league", ["TOR"], 10)
    assert len(pages) == 8 and all(len(p.data["rows"]) == 4 for p in pages)
    ranks = [r["rank"] for p in pages for r in p.data["rows"]]
    assert ranks == list(range(1, 33))
    assert [p.data["page"] for p in pages] == list(range(1, 9)) and pages[0].data["pages"] == 8
    assert pages[0].lines[0] == "NHL STANDINGS 1/8"


def test_wildcard_screen_follows_nhl_format(standings):
    screen = views.wildcard_screen(standings, "Eastern", ["TOR"], 12)
    divisions = {b["name"]: [r["team"]["abbrev"] for r in b["rows"]] for b in screen.data["divisions"]}
    assert divisions == {"Atlantic": ["BOS", "OTT", "TBL"], "Metropolitan": ["CAR", "NYR", "PIT"]}
    race = [(r["rank"], r["team"]["abbrev"]) for r in screen.data["wildcard"]]
    assert race[:3] == [(1, "FLA"), (2, "MTL"), (3, "TOR")] and len(race) == 10
    assert screen.type == "wildcard" and screen.data["spots"] == 2


def _ids(config: Config, standings) -> list[str]:
    store = Store()
    store.set_games([])  # the first scoreboard has landed: past the startup idle hold
    store.set_standings(standings)
    sched = Scheduler(store, config)
    ids, t = [], 0
    while t < 200:
        screen = sched.tick(T0 + timedelta(seconds=t))
        if not ids or ids[-1] != screen.id:
            ids.append(screen.id)
        t += 1
    return ids


def _config(teams: list[str]) -> Config:
    return Config(favorite_teams=teams, favorite_players=[], display=DisplayConfig(rotation=[
        RotationEntry(screen="standings", scope="division", dwell=2),
        RotationEntry(screen="standings", scope="wildcard", dwell=2),
        RotationEntry(screen="standings", scope="league", dwell=2),
    ]))


def test_rotation_only_shows_followed_divisions_and_conferences(standings):
    ids = _ids(_config(["TOR"]), standings)
    cycle = ids[: ids.index("standings:division:TOR", 1)]
    assert cycle == ["standings:division:TOR", "wildcard:Eastern",
                     *(f"standings:league:{n}" for n in range(8))]

    ids = _ids(_config(["TOR", "EDM", "BOS"]), standings)  # BOS shares TOR's division
    cycle = ids[: ids.index("standings:division:TOR", 1)]
    assert [i for i in cycle if i.startswith("standings:division")] == ["standings:division:TOR",
                                                                        "standings:division:EDM"]
    assert [i for i in cycle if i.startswith("wildcard")] == ["wildcard:Eastern", "wildcard:Western"]


def test_division_table_highlights_every_followed_team(standings):
    screen = views.standings_screen(standings, "TOR", "division", None, 10, ["TOR", "BOS"])
    cv = render(screen.model_dump(mode="json"), T0)
    rows = {r["team"]["abbrev"]: 17 + i * 6 for i, r in enumerate(screen.data["rows"])}
    assert cv.get(127, rows["TOR"]) == HIGHLIGHT and cv.get(127, rows["BOS"]) == HIGHLIGHT
    assert cv.get(127, rows["OTT"]) != HIGHLIGHT


def test_league_pages_use_regular_size_rows(standings):
    page = next(p for p in views.standings_pages(standings, "league", ["TOR"], 5)
                if "TOR" in [r["team"]["abbrev"] for r in p.data["rows"]])
    cv = render(page.model_dump(mode="json"), T0)
    for i, r in enumerate(page.data["rows"]):
        y = 18 + i * 12  # regular font rows, 12px apart; all four on the panel
        assert any(cv.get(x, y + 6) != (0, 0, 0) for x in range(18, 36))  # bottom of the 7px team code
        assert (cv.get(127, y) == HIGHLIGHT) == (r["team"]["abbrev"] == "TOR")


def test_records_are_colored_by_part(standings):
    assert [(s["text"], s.get("color")) for s in views.record_spans("6-1-1")] == [
        ("6", views.WIN_COLOR), ("-", None), ("1", views.LOSS_COLOR), ("-", None), ("1", None)]
    page = views.standings_pages(standings, "league", ["TOR"], 5)[0]
    assert page.lines[1].endswith(page.data["rows"][0]["record"])  # plain text unchanged
    # Both table sizes draw the backend's colors: wins green, losses red, dashes white.
    for screen, x, y in ((views.standings_pages(standings, "league", ["TOR"], 5)[0], 61, 18),
                         (views.standings_screen(standings, "TOR", "division", None, 10), 52, 17)):
        cv = render(screen.model_dump(mode="json"), T0)
        colors = {cv.get(px, py) for px in range(x, x + 40) for py in range(y, y + 7)}
        assert {tuple(views.WIN_COLOR), tuple(views.LOSS_COLOR), (255, 255, 255)} <= colors


def test_wildcard_keeps_a_far_back_followed_team_visible(standings):
    screen = views.wildcard_screen(standings, "Eastern", ["PHI"], 12)  # PHI is WC10, past the visible rows
    cv = render(screen.model_dump(mode="json"), T0)
    highlighted = [y for y in range(cv.height) if cv.get(127, y) == HIGHLIGHT]
    assert highlighted and min(highlighted) > 48  # PHI took the last row of the right column


def test_standings_arriving_after_startup_appear_in_the_first_cycle(fixture, standings):
    """Regression: the rotation used to be expanded on the first tick, before data
    loaded, so standings were skipped for a whole cycle after startup."""
    store = Store()
    store.set_games([])
    store.set_leaders({"points": parsers.parse_leaders(fixture("skater_leaders.json"))["points"]})
    config = Config(favorite_teams=["TOR"], favorite_players=[], display=DisplayConfig(rotation=[
        RotationEntry(screen="leaders", dwell=2),
        RotationEntry(screen="standings", scope="division", dwell=2),
        RotationEntry(screen="standings", scope="league", dwell=2),
    ]))
    config.leaders.skater_categories, config.leaders.goalie_categories = ["points"], []
    sched = Scheduler(store, config)
    assert sched.tick(T0).id == "leaders:points"  # standings not loaded yet
    store.set_standings(standings)  # ...they arrive while leaders is showing
    # The old scheduler restarted its stale playlist here and showed leaders again.
    assert sched.tick(T0 + timedelta(seconds=2)).id == "standings:division:TOR"
    assert sched.tick(T0 + timedelta(seconds=4)).id == "standings:league:0"
