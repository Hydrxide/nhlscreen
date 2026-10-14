"""Background loops that keep the Store fresh, with cadence adapted to game state.

  scoreboard   20s while anything is live, 60s pregame, 15 min otherwise
  favorite     play-by-play every 5s (+ team stats every 30s) while a favorite
               team's game is live or about to start
  standings / leaders / schedules / players   slow refreshes (hours)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from ..config import Config
from ..nhl import parsers
from ..nhl.client import NHLClient, NHLUnavailable
from ..nhl.models import Game, GameEvent
from ..store import Store
from .events import EventDetector

log = logging.getLogger(__name__)

Now = Callable[[], datetime]


class Poller:
    def __init__(
        self,
        client: NHLClient,
        store: Store,
        config: Config,
        on_event: Callable[[GameEvent], None] | None = None,
        now: Now = lambda: datetime.now(timezone.utc),
        speed: float = 1.0,
    ) -> None:
        self.client = client
        self.store = store
        self.config = config
        self.detector = EventDetector()
        self.on_event = on_event
        self.now = now
        # >1 compresses every sleep; used by replay mode.
        self.speed = speed
        self._tasks: list[asyncio.Task] = []
        self._tracked: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        p = self.config.polling
        self._tasks = [
            asyncio.create_task(self._scoreboard_loop(), name="scoreboard"),
            asyncio.create_task(self._every(p.standings_seconds, self.refresh_standings), name="standings"),
            asyncio.create_task(self._every(p.leaders_seconds, self.refresh_leaders), name="leaders"),
            asyncio.create_task(self._every(p.schedule_seconds, self.refresh_schedules), name="schedules"),
            asyncio.create_task(self._every(p.players_seconds, self.refresh_players), name="players"),
        ]

    async def stop(self) -> None:
        tasks = self._tasks + list(self._tracked.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks, self._tracked = [], {}

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.05, seconds / self.speed))

    async def _every(self, seconds: float, fn: Callable[[], Awaitable[None]]) -> None:
        while True:
            await self._safely(fn)
            await self._sleep(seconds)

    async def _safely(self, fn: Callable[[], Awaitable[None]]) -> None:
        try:
            await fn()
        except NHLUnavailable as exc:
            log.warning("%s unavailable: %s", getattr(fn, "__name__", fn), exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # never let one bad payload kill a loop
            log.exception("Refresh failed: %s", getattr(fn, "__name__", fn))

    def _emit(self, events: list[GameEvent]) -> None:
        for ev in events:
            log.info("event %s %s", ev.kind, ev.key)
            self.store.add_event(ev)
            if self.on_event:
                self.on_event(ev)

    # ------------------------------------------------------------------ scoreboard

    async def refresh_scoreboard(self) -> None:
        games = parsers.parse_score(await self.client.score_now())
        previous = dict(self.store.games)
        self.store.set_games(games)
        self._emit(self.detector.from_scoreboard(previous, games, self.now(), skip=set(self._tracked)))
        self._update_tracking(games)

    def scoreboard_interval(self) -> float:
        p = self.config.polling
        now = self.now()
        games = [self.store.game(g.id) or g for g in self.store.games.values()]
        if any(g.state.is_live for g in games):
            return p.scoreboard_live_seconds
        upcoming = [g.start_time_utc for g in games if g.state.is_upcoming]
        if any((t - now).total_seconds() <= p.pregame_window_seconds for t in upcoming):
            return p.scoreboard_pregame_seconds
        if upcoming:
            # Sleep until the pregame window opens, but no longer than the idle interval.
            until_window = (min(upcoming) - now).total_seconds() - p.pregame_window_seconds
            return max(p.scoreboard_pregame_seconds, min(p.scoreboard_idle_seconds, until_window))
        return p.scoreboard_idle_seconds

    async def _scoreboard_loop(self) -> None:
        while True:
            await self._safely(self.refresh_scoreboard)
            await self._sleep(self.scoreboard_interval())

    # ------------------------------------------------------------------ favorite games

    def _should_track(self, game: Game) -> bool:
        if not any(game.involves(t) for t in self.config.favorite_teams):
            return False
        if game.state.is_live:
            return True
        if game.state.is_upcoming:
            seconds_to_start = (game.start_time_utc - self.now()).total_seconds()
            return seconds_to_start <= 15 * 60
        return False

    def _update_tracking(self, games: list[Game]) -> None:
        for game in games:
            task = self._tracked.get(game.id)
            if task is not None and task.done():
                continue  # finished tracking this game; don't restart it
            if task is None and self._should_track(game):
                log.info("Tracking favorite game %s (%s @ %s)", game.id, game.away.abbrev, game.home.abbrev)
                self._tracked[game.id] = asyncio.create_task(self._track_game(game.id), name=f"game:{game.id}")

    async def favorites_changed(self) -> None:
        """Re-point live tracking and schedules at the new favorite teams."""
        favorites = self.config.favorite_teams
        for game_id, task in list(self._tracked.items()):
            game = self.store.game(game_id)
            if game is None or not any(game.involves(t) for t in favorites):
                task.cancel()
                del self._tracked[game_id]
                self.detector.forget(game_id)
        await self._safely(self.refresh_schedules)
        # Picks up a live game for the new team right away (and starts tracking it).
        await self._safely(self.refresh_scoreboard)

    async def refresh_game(self, game_id: int, with_stats: bool) -> Game:
        detail = parsers.parse_play_by_play(await self.client.play_by_play(game_id))
        if with_stats:
            try:
                detail.team_stats = parsers.parse_right_rail(await self.client.right_rail(game_id))
            except NHLUnavailable as exc:
                log.warning("team stats unavailable for %s: %s", game_id, exc)
        if detail.game.state.is_final and not (self.store.details.get(game_id) or detail).three_stars:
            try:
                detail.three_stars = parsers.parse_three_stars(await self.client.landing(game_id))
            except NHLUnavailable as exc:
                log.warning("three stars unavailable for %s: %s", game_id, exc)
        events = self.detector.from_detail(detail, self.now())
        self.store.set_detail(detail)
        self._emit(events)
        return detail.game

    async def _track_game(self, game_id: int) -> None:
        p = self.config.polling
        last_stats = -1e9
        elapsed = 0.0
        while True:
            with_stats = elapsed - last_stats >= p.live_stats_seconds
            if with_stats:
                last_stats = elapsed
            try:
                game = await self.refresh_game(game_id, with_stats)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("game %s refresh failed: %s", game_id, exc)
                game = None
            if game is not None and game.state.is_final:
                log.info("Game %s final; stopping live tracking", game_id)
                # One more scoreboard refresh so the league view reflects the final.
                await self._safely(self.refresh_scoreboard)
                # Schedules carry last-game results for the next_game screen.
                await self._safely(self.refresh_schedules)
                return
            interval = p.live_poll_seconds
            if game is not None and game.clock and game.clock.in_intermission:
                interval = max(interval, 30.0)  # nothing changes during intermission
            await self._sleep(interval)
            elapsed += interval

    # ------------------------------------------------------------------ slow data

    async def refresh_standings(self) -> None:
        self.store.set_standings(parsers.parse_standings(await self.client.standings()))

    async def refresh_leaders(self) -> None:
        cfg, limit = self.config.leaders, max(self.config.display.leaders_limit, 5)
        if cfg.skater_categories:
            self.store.set_leaders(parsers.parse_leaders(await self.client.skater_leaders(cfg.skater_categories, limit)))
        if cfg.goalie_categories:
            self.store.set_leaders(parsers.parse_leaders(await self.client.goalie_leaders(cfg.goalie_categories, limit)))

    async def refresh_schedules(self) -> None:
        for team in self.config.favorite_teams:
            try:
                self.store.set_schedule(team, parsers.parse_schedule(await self.client.club_schedule(team)))
            except NHLUnavailable as exc:
                log.warning("schedule unavailable for %s: %s", team, exc)

    async def refresh_players(self) -> None:
        for pid in self.config.favorite_players:
            try:
                self.store.set_player(parsers.parse_player(await self.client.player(pid)))
            except NHLUnavailable as exc:
                log.warning("player %s unavailable: %s", pid, exc)
