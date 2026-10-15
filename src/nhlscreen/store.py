"""In-memory snapshot of everything the board knows, plus a tiny pub/sub."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from .services.clocktrack import ClockSegment, ClockTrack
from .nhl.models import (
    Game,
    GameDetail,
    GameEvent,
    LeaderEntry,
    PlayerSummary,
    ScheduledGame,
    TeamStanding,
)

log = logging.getLogger(__name__)


class Store:
    def __init__(self, recent_events: int = 50) -> None:
        self.games: dict[int, Game] = {}
        self.details: dict[int, GameDetail] = {}
        self.standings: list[TeamStanding] = []
        self.schedules: dict[str, list[ScheduledGame]] = {}
        self.leaders: dict[str, list[LeaderEntry]] = {}
        self.players: dict[int, PlayerSummary] = {}
        self.recent_events: deque[GameEvent] = deque(maxlen=recent_events)
        self.updated_at: dict[str, datetime] = {}
        # game id -> (clock reading, when that reading was first seen)
        self._clock_seen: dict[int, tuple[tuple, datetime]] = {}
        # Tracked games' clock readings, including ones not shown yet (the plan's lookahead).
        self.clock_tracks: dict[int, ClockTrack] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    # ------------------------------------------------------------------ updates

    def touch(self, section: str) -> None:
        self.updated_at[section] = datetime.now(timezone.utc)

    def set_games(self, games: list[Game], age: float = 0.0) -> None:
        """``age``: how many seconds old the data already was (see _note_clock)."""
        self.games = {g.id: g for g in games}
        for g in games:
            if g.id not in self.details:  # tracked games get fresher clocks from play-by-play
                self._note_clock(g, age)
        self.touch("games")

    def set_detail(self, detail: GameDetail, age: float = 0.0) -> None:
        previous = self.details.get(detail.game.id)
        if previous:
            # play-by-play refreshes don't carry these; keep what we had
            detail.team_stats = detail.team_stats or previous.team_stats
            detail.three_stars = detail.three_stars or previous.three_stars
        self.details[detail.game.id] = detail
        self._note_clock(detail.game, age)
        # The detail feed is fresher than the scoreboard; reflect it there too.
        if detail.game.id in self.games:
            self.games[detail.game.id] = detail.game
        self.touch(f"game:{detail.game.id}")

    def set_standings(self, standings: list[TeamStanding]) -> None:
        self.standings = standings
        self.touch("standings")

    def set_schedule(self, abbrev: str, games: list[ScheduledGame]) -> None:
        self.schedules[abbrev] = games
        self.touch(f"schedule:{abbrev}")

    def set_leaders(self, leaders: dict[str, list[LeaderEntry]]) -> None:
        self.leaders.update(leaders)
        self.touch("leaders")

    def set_player(self, player: PlayerSummary) -> None:
        self.players[player.id] = player
        self.touch(f"player:{player.id}")

    def _note_clock(self, game: Game, age: float = 0.0) -> None:
        """Date a new clock reading to when it was true: ``age`` seconds before now.

        The NHL's CDN serves snapshots up to ~19s old; treating a reading as
        current when it arrives would put the countdown up to that far ahead.
        """
        c = game.clock
        reading = (game.period, c.seconds_remaining, c.running, c.in_intermission) if c else ()
        seen = self._clock_seen.get(game.id)
        if seen is None or seen[0] != reading:
            self._clock_seen[game.id] = (reading, datetime.now(timezone.utc) - timedelta(seconds=age))

    def clock_as_of(self, game_id: int) -> datetime | None:
        """When the game's current clock reading was true (first arrival minus its age).

        Re-fetching an unchanged reading must not move this forward, or a
        renderer counting down from it would stall and then jump.
        """
        seen = self._clock_seen.get(game_id)
        return seen[1] if seen else None

    def clock_track(self, game_id: int, nominal_rate: float = 1.0) -> ClockTrack:
        track = self.clock_tracks.get(game_id)
        if track is None:
            track = self.clock_tracks[game_id] = ClockTrack(nominal_rate)
        return track

    def clock_segment(self, game_id: int, now: datetime) -> ClockSegment | None:
        """The planned clock for a tracked game at ``now`` (None: count down from as_of)."""
        track = self.clock_tracks.get(game_id)
        return track.segment(now.timestamp()) if track else None

    # ------------------------------------------------------------------ queries

    def game(self, game_id: int) -> Game | None:
        detail = self.details.get(game_id)
        return detail.game if detail else self.games.get(game_id)

    def games_for(self, abbrev: str) -> list[Game]:
        return sorted((g for g in self.games.values() if g.involves(abbrev)), key=lambda g: g.start_time_utc)

    def standing(self, abbrev: str) -> TeamStanding | None:
        return next((s for s in self.standings if s.abbrev == abbrev.upper()), None)

    def next_game(self, abbrev: str, now: datetime) -> ScheduledGame | None:
        upcoming = [
            g
            for g in self.schedules.get(abbrev.upper(), [])
            # an in-progress game started up to a few hours ago still counts as "next"
            if not g.state.is_final and g.start_time_utc >= now - timedelta(hours=6)
        ]
        return min(upcoming, key=lambda g: g.start_time_utc, default=None)

    def last_game(self, abbrev: str) -> ScheduledGame | None:
        done = [g for g in self.schedules.get(abbrev.upper(), []) if g.state.is_final]
        return max(done, key=lambda g: g.start_time_utc, default=None)

    # ------------------------------------------------------------------ pub/sub

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, message: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                log.warning("Dropping slow subscriber")
                self._subscribers.discard(q)

    def add_event(self, event: GameEvent) -> None:
        self.recent_events.append(event)
        self.publish({"type": "event", "event": event.model_dump(mode="json")})
