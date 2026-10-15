"""Turn successive snapshots into discrete GameEvents (goal, penalty, final...).

Two sources feed the detector:
  * play-by-play for tracked (favorite-team) games: precise, has event IDs
  * scoreboard diffs for every other game: score changes and finals only

The first time a game is seen its history is recorded silently, so starting
the service mid-game doesn't fire an alert for every goal already scored.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from ..nhl.models import Game, GameDetail, GameEvent, Goal

_PLAY_KINDS = {"period-start": "period_start", "period-end": "period_end", "game-end": "game_final"}


class EventDetector:
    def __init__(self, memory: int = 2000) -> None:
        # game id -> {event id: play type}. The NHL sometimes posts a play as a
        # shot and upgrades the same event id to a goal, so a type change counts as new.
        self._seen_plays: dict[int, dict[int, str]] = {}
        self._goal_ids: dict[int, set[int]] = {}
        self._emitted: set[str] = set()
        self._emitted_order: deque[str] = deque()
        self._memory = memory

    def _remember(self, key: str) -> bool:
        """Return True the first time a key is seen."""
        if key in self._emitted:
            return False
        self._emitted.add(key)
        self._emitted_order.append(key)
        if len(self._emitted_order) > self._memory:
            self._emitted.discard(self._emitted_order.popleft())
        return True

    @staticmethod
    def _event(game: Game, now: datetime, **kw) -> GameEvent:
        return GameEvent(
            game_id=game.id,
            away_abbrev=game.away.abbrev,
            home_abbrev=game.home.abbrev,
            away_score=game.away.score,
            home_score=game.home.score,
            period=game.period,
            period_type=game.period_type,
            occurred_at=now,
            **kw,
        )

    def forget(self, game_id: int) -> None:
        """Drop play-by-play memory so a re-tracked game is seeded silently again."""
        self._seen_plays.pop(game_id, None)
        self._goal_ids.pop(game_id, None)

    # ------------------------------------------------------------------ play-by-play

    def from_detail(self, detail: GameDetail, now: datetime) -> list[GameEvent]:
        game = detail.game
        current = {p.event_id: p.type for p in detail.plays}
        current_goal_ids = {p.event_id for p in detail.plays if p.type == "goal"}

        if game.id not in self._seen_plays:
            self._seen_plays[game.id] = current
            self._goal_ids[game.id] = current_goal_ids
            if game.state.is_final:
                self._remember(f"final:{game.id}")
            return []

        seen = self._seen_plays[game.id]
        goals_by_id = {g.event_id: g for g in game.goals}
        penalties_by_id = {p.event_id: p for p in detail.penalties}
        events: list[GameEvent] = []

        for play in detail.plays:  # already in game order
            if seen.get(play.event_id) == play.type:
                continue
            if play.type == "goal" and play.event_id in goals_by_id:
                goal = goals_by_id[play.event_id]
                if self._remember(f"goal:{game.id}:{play.event_id}"):
                    events.append(self._event(game, now, kind="goal", key=f"goal:{game.id}:{play.event_id}",
                                              team_abbrev=goal.team_abbrev, goal=goal))
            elif play.type == "penalty" and play.event_id in penalties_by_id:
                pen = penalties_by_id[play.event_id]
                key = f"penalty:{game.id}:{play.event_id}"
                if self._remember(key):
                    events.append(self._event(game, now, kind="penalty", key=key,
                                              team_abbrev=pen.team_abbrev, penalty=pen))
            elif play.type in _PLAY_KINDS:
                kind = _PLAY_KINDS[play.type]
                key = f"final:{game.id}" if kind == "game_final" else f"{kind}:{game.id}:{play.period}"
                if self._remember(key):
                    ev = self._event(game, now, kind=kind, key=key)
                    ev.period, ev.period_type = play.period, play.period_type
                    events.append(ev)

        # The NHL deletes a goal's play when it is overturned on review.
        for gone in sorted(self._goal_ids[game.id] - current_goal_ids):
            key = f"overturned:{game.id}:{gone}"
            if self._remember(key):
                events.append(self._event(game, now, kind="goal_overturned", key=key))

        if game.state.is_final and self._remember(f"final:{game.id}"):
            events.append(self._event(game, now, kind="game_final", key=f"final:{game.id}"))

        self._seen_plays[game.id] = seen | current
        self._goal_ids[game.id] = current_goal_ids
        return events

    # ------------------------------------------------------------------ scoreboard

    def from_scoreboard(
        self,
        previous: dict[int, Game],
        current: list[Game],
        now: datetime,
        skip: set[int] = frozenset(),  # type: ignore[assignment]
    ) -> list[GameEvent]:
        """Score-change events for games not covered by play-by-play tracking."""
        events: list[GameEvent] = []
        for game in current:
            prev = previous.get(game.id)
            if prev is None or game.id in skip:
                continue
            prev_total = (prev.away.score or 0) + (prev.home.score or 0)
            total = (game.away.score or 0) + (game.home.score or 0)
            score_key = f"{game.away.score or 0}-{game.home.score or 0}"

            if total > prev_total:
                # The score change is authoritative; the goal list can lag behind it.
                team = _scoring_team(prev, game)
                goal = _latest_goal(game)
                if goal and goal.team_abbrev != team:
                    goal = None
                key = f"goal:{game.id}:score:{score_key}"
                if self._remember(key):
                    events.append(self._event(game, now, kind="goal", key=key, team_abbrev=team, goal=goal))
            elif total < prev_total:
                key = f"overturned:{game.id}:score:{score_key}"
                if self._remember(key):
                    events.append(self._event(game, now, kind="goal_overturned", key=key))

            if game.state.is_final and not prev.state.is_final and self._remember(f"final:{game.id}"):
                events.append(self._event(game, now, kind="game_final", key=f"final:{game.id}"))
        return events


def _latest_goal(game: Game) -> Goal | None:
    if not game.goals:
        return None
    goal = game.goals[-1]
    # Only trust it if the scoreboard's goal list has caught up with its score.
    if goal.away_score == (game.away.score or 0) and goal.home_score == (game.home.score or 0):
        return goal
    return None


def _scoring_team(prev: Game, cur: Game) -> str | None:
    if (cur.away.score or 0) > (prev.away.score or 0):
        return cur.away.abbrev
    if (cur.home.score or 0) > (prev.home.score or 0):
        return cur.home.abbrev
    return None
