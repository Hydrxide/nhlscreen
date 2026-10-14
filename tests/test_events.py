from datetime import datetime, timezone

import pytest

from nhlscreen.nhl import parsers
from nhlscreen.nhl.models import GameDetail, GameState
from nhlscreen.services.events import EventDetector

NOW = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def full(fixture) -> GameDetail:
    return parsers.parse_play_by_play(fixture("pbp_final.json"))


def snapshot(full: GameDetail, n_plays: int, state: GameState = GameState.LIVE) -> GameDetail:
    """The game as it looked after its first n plays."""
    detail = full.model_copy(deep=True)
    detail.plays = detail.plays[:n_plays]
    ids = {p.event_id for p in detail.plays}
    detail.game.goals = [g for g in detail.game.goals if g.event_id in ids]
    detail.penalties = [p for p in detail.penalties if p.event_id in ids]
    detail.game.state = state
    last = detail.game.goals[-1] if detail.game.goals else None
    detail.game.away.score = last.away_score if last else 0
    detail.game.home.score = last.home_score if last else 0
    return detail


def index_of(full: GameDetail, kind: str, nth: int = 0) -> int:
    return [i for i, p in enumerate(full.plays) if p.type == kind][nth]


def test_first_observation_is_silent(full):
    det = EventDetector()
    assert det.from_detail(snapshot(full, 150), NOW) == []


def test_goal_event_has_scorer_and_score(full):
    det = EventDetector()
    goal_at = index_of(full, "goal")
    det.from_detail(snapshot(full, goal_at), NOW)
    events = det.from_detail(snapshot(full, goal_at + 1), NOW)
    goals = [e for e in events if e.kind == "goal"]
    assert len(goals) == 1
    ev = goals[0]
    assert ev.team_abbrev == "UTA" and ev.goal.scorer_name == "D. But"
    assert (ev.away_score, ev.home_score) == (0, 1)
    # Same snapshot again: no duplicate.
    assert det.from_detail(snapshot(full, goal_at + 1), NOW) == []


def test_penalty_and_period_events(full):
    det = EventDetector()
    det.from_detail(snapshot(full, 0), NOW)
    kinds = [e.kind for e in det.from_detail(snapshot(full, index_of(full, "period-end") + 1), NOW)]
    assert "period_start" in kinds and "penalty" in kinds and kinds[-1] == "period_end"


def test_overturned_goal(full):
    det = EventDetector()
    goal_at = index_of(full, "goal")
    det.from_detail(snapshot(full, goal_at), NOW)
    det.from_detail(snapshot(full, goal_at + 3), NOW)

    reviewed = snapshot(full, goal_at + 3)
    goal_id = full.plays[goal_at].event_id
    reviewed.plays = [p for p in reviewed.plays if p.event_id != goal_id]
    reviewed.game.goals = []
    reviewed.game.home.score = 0
    events = det.from_detail(reviewed, NOW)
    assert [e.kind for e in events] == ["goal_overturned"]


def test_final_emitted_once(full):
    det = EventDetector()
    det.from_detail(snapshot(full, len(full.plays) - 5), NOW)
    events = det.from_detail(snapshot(full, len(full.plays), GameState.FINAL), NOW)
    assert [e.kind for e in events].count("game_final") == 1
    assert det.from_detail(snapshot(full, len(full.plays), GameState.OFFICIAL), NOW) == []


def test_scoreboard_diff(fixture):
    det = EventDetector()
    games = parsers.parse_score(fixture("score.json"))
    game = next(g for g in games if g.id == 2026020102)

    before = game.model_copy(deep=True)
    before.state, before.home.score = GameState.LIVE, 4
    after = game.model_copy(deep=True)
    after.state = GameState.LIVE

    events = det.from_scoreboard({game.id: before}, [after], NOW)
    assert [e.kind for e in events] == ["goal"]
    assert events[0].team_abbrev == "UTA"

    # Tracked games are handled by play-by-play, not the scoreboard.
    assert EventDetector().from_scoreboard({game.id: before}, [after], NOW, skip={game.id}) == []

    # Score went down: goal taken back.
    assert [e.kind for e in det.from_scoreboard({game.id: after}, [before], NOW)] == ["goal_overturned"]

    # Live -> final.
    final = game.model_copy(deep=True)
    assert [e.kind for e in det.from_scoreboard({game.id: after}, [final], NOW)] == ["game_final"]


def test_scoreboard_first_sighting_is_silent(fixture):
    games = parsers.parse_score(fixture("score.json"))
    assert EventDetector().from_scoreboard({}, games, NOW) == []
