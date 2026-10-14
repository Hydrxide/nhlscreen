from nhlscreen.nhl import parsers
from nhlscreen.nhl.models import GameState

GAME_ID = 2026020102  # TOR @ UTA, 2026-10-13, UTA won 5-4


def test_parse_score(fixture):
    games = parsers.parse_score(fixture("score.json"))
    assert len(games) == 16
    game = next(g for g in games if g.id == GAME_ID)
    assert (game.away.abbrev, game.away.score, game.away.sog) == ("TOR", 4, 26)
    assert (game.home.abbrev, game.home.score, game.home.sog) == ("UTA", 5, 28)
    assert game.state is GameState.OFFICIAL and game.state.is_final
    assert game.last_period_type == "REG"
    assert len(game.goals) == 9
    assert game.goals[-1].scorer_name == "G. McKenna"
    assert game.goals[-1].assists == ["W. Nylander", "D. Raddysh"]


def test_parse_play_by_play_goals_match_official_strength(fixture):
    detail = parsers.parse_play_by_play(fixture("pbp_final.json"))
    scoreboard_game = next(g for g in parsers.parse_score(fixture("score.json")) if g.id == GAME_ID)
    official = [g.strength for g in scoreboard_game.goals]
    assert [g.strength for g in detail.game.goals] == official
    first = detail.game.goals[0]
    assert (first.team_abbrev, first.scorer_name, first.period, first.time_in_period) == ("UTA", "D. But", 1, "14:55")
    assert first.event_id is not None
    assert [(g.away_score, g.home_score) for g in detail.game.goals][-1] == (4, 5)


def test_parse_play_by_play_penalties_and_plays(fixture):
    detail = parsers.parse_play_by_play(fixture("pbp_final.json"))
    assert len(detail.penalties) == 9
    pen = detail.penalties[0]
    assert (pen.team_abbrev, pen.description, pen.duration) == ("UTA", "slashing", 2)
    assert detail.plays == sorted(detail.plays, key=lambda p: p.sort_order)
    assert sum(p.type == "game-end" for p in detail.plays) == 1


def test_decode_situation_code():
    sit = parsers.decode_situation_code("1541")
    assert sit.strength == "5v4" and sit.power_play_team == "away"
    # TOR pulled its goalie for an extra attacker: still even strength officially.
    pulled = parsers.decode_situation_code("0651")
    assert pulled.strength == "5v5" and pulled.power_play_team is None and not pulled.away_goalie_in
    assert parsers.decode_situation_code("bad") is None


def test_parse_right_rail_and_three_stars(fixture):
    stats = {s.category: s for s in parsers.parse_right_rail(fixture("rightrail_final.json"))}
    assert stats["sog"].away == 26 and stats["powerPlay"].away == "2/5"
    stars = parsers.parse_three_stars(fixture("landing_final.json"))
    assert [s.star for s in stars] == [1, 2, 3]
    assert stars[0].name == "M. Sergachev"


def test_parse_standings(fixture):
    standings = parsers.parse_standings(fixture("standings.json"))
    assert len(standings) == 32
    car = standings[0]
    assert car.abbrev == "CAR" and car.record == "6-1-1" and car.streak == "W6" and car.l10 == "6-1-1"
    assert car.wildcard_rank is None  # division leaders have wildcardSequence 0
    assert {s.division for s in standings} == {"Atlantic", "Metropolitan", "Central", "Pacific"}


def test_parse_schedule(fixture):
    schedule = parsers.parse_schedule(fixture("schedule_tor.json"))
    assert all("TOR" in (g.away_abbrev, g.home_abbrev) for g in schedule)
    assert any(g.state is GameState.FUTURE for g in schedule)


def test_parse_leaders(fixture):
    skaters = parsers.parse_leaders(fixture("skater_leaders.json"))
    assert set(skaters) == {"goals", "assists", "points"}
    assert skaters["points"][0].name == "C. McDavid"
    goalies = parsers.parse_leaders(fixture("goalie_leaders.json"))
    assert 0.9 < goalies["savePctg"][0].value < 1


def test_parse_player(fixture):
    skater = parsers.parse_player(fixture("player_8478483.json"))
    assert skater.name == "Mitch Marner" and skater.position == "R" and not skater.is_goalie
    assert skater.season_stats["points"] == 7
    goalie = parsers.parse_player(fixture("player_8480280.json"))
    assert goalie.is_goalie and "savePctg" in goalie.season_stats
