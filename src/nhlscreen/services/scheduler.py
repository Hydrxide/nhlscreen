"""Decide what the board shows right now.

Priority:
  1. alerts (favorite-team goal / overturned goal / final), each held for N seconds
  2. a live favorite-team game (rotation resumes during intermissions if configured)
  3. the configured rotation (next game, scoreboard, standings, leaders, players)
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Callable

from ..config import Config, RotationEntry
from ..nhl.models import Game, GameEvent
from ..store import Store
from .. import views
from ..views import Screen

# When several favorite teams are live at once, alternate between them.
LIVE_ALTERNATE_SECONDS = 15.0


class Scheduler:
    def __init__(
        self,
        store: Store,
        config: Config,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock_rate: float = 1.0,
    ) -> None:
        self.store = store
        self.config = config
        self.clock = clock
        # Game seconds per real second, for renderers' clock countdown (replays run faster).
        self.clock_rate = clock_rate
        self.current: Screen | None = None
        self._started_at: datetime | None = None
        self._alerts: deque[Screen] = deque()
        self._playlist: list[Callable[[datetime], Screen | None]] = []
        self._index = 0

    def reset(self) -> None:
        """Forget the current screen, queued alerts and rotation (e.g. favorites changed)."""
        self.current = None
        self._started_at = None
        self._alerts.clear()
        self._playlist = []
        self._index = 0

    # ------------------------------------------------------------------ alerts

    def on_event(self, event: GameEvent) -> None:
        favorites = self.config.favorite_teams
        if not (event.away_abbrev in favorites or event.home_abbrev in favorites):
            return
        d = self.config.display
        if event.kind == "goal":
            self._alerts.append(views.goal_alert_screen(event, d.alert_seconds))
        elif event.kind == "goal_overturned":
            # Drop a still-queued alert for the goal that was just taken back.
            self._alerts = deque(a for a in self._alerts if a.data.get("game_id") != event.game_id
                                 or a.type != "goal_alert")
            self._alerts.append(views.overturned_screen(event, d.alert_seconds))
        elif event.kind == "game_final":
            game = self.store.game(event.game_id)
            detail = self.store.details.get(event.game_id)
            self._alerts.append(views.final_alert_screen(event, game, detail, d.final_alert_seconds))

    # ------------------------------------------------------------------ main entry

    def tick(self, now: datetime | None = None) -> Screen:
        now = now or self.clock()
        tz = self.config.tz
        cur = self.current

        # 1. Alerts: let the current alert finish, then show the next queued one.
        if cur and cur.priority >= views.PRIORITY_ALERT and not self._expired(cur, now):
            return cur
        if self._alerts:
            return self._show(self._alerts.popleft(), now)

        # 2. Live favorite game.
        live = self._live_favorite_games()
        show_live = [
            g for g in live
            if not (self.config.display.rotate_during_intermission and g.clock and g.clock.in_intermission)
        ]
        if show_live:
            game = show_live[int(now.timestamp() // LIVE_ALTERNATE_SECONDS) % len(show_live)]
            screen = views.live_game_screen(game, self.store.details.get(game.id), tz, LIVE_ALTERNATE_SECONDS, now,
                                            as_of=self.store.clock_as_of(game.id), clock_rate=self.clock_rate)
            return self._show(screen, now, keep_start=cur is not None and cur.id == screen.id)

        # 3. Rotation: refresh the current rotation screen's data until its dwell expires.
        if cur and cur.priority == views.PRIORITY_ROTATION and not self._expired(cur, now):
            refreshed = self._rebuild(self._index - 1, now)
            if refreshed is not None and refreshed.id == cur.id:
                return self._show(refreshed, now, keep_start=True)
            return cur
        return self._advance(now)

    # ------------------------------------------------------------------ internals

    def _show(self, screen: Screen, now: datetime, keep_start: bool = False) -> Screen:
        if not keep_start or self._started_at is None:
            self._started_at = now
        self.current = screen
        return screen

    def _expired(self, screen: Screen, now: datetime) -> bool:
        return self._started_at is None or (now - self._started_at).total_seconds() >= screen.dwell_seconds

    def _live_favorite_games(self) -> list[Game]:
        out = []
        for team in self.config.favorite_teams:
            for g in self.store.games_for(team):
                game = self.store.game(g.id) or g
                if game.state.is_live and all(o.id != game.id for o in out):
                    out.append(game)
        return out

    def _rebuild(self, index: int, now: datetime) -> Screen | None:
        if 0 <= index < len(self._playlist):
            return self._playlist[index](now)
        return None

    def _advance(self, now: datetime) -> Screen:
        # Try every slot once; rebuild the playlist when a cycle completes.
        for _ in range(2):
            while self._index < len(self._playlist):
                builder = self._playlist[self._index]
                self._index += 1
                screen = builder(now)
                if screen is not None:
                    return self._show(screen, now)
            self._playlist = self._build_playlist(now)
            self._index = 0
        return self._show(views.idle_screen(now, self.config.tz), now)

    def _build_playlist(self, now: datetime) -> list[Callable[[datetime], Screen | None]]:
        """Expand rotation entries into concrete slots against current data."""
        slots: list[Callable[[datetime], Screen | None]] = []
        for entry in self.config.display.rotation:
            slots.extend(self._slots_for(entry, now))
        return slots

    def _slots_for(self, entry: RotationEntry, now: datetime) -> list[Callable[[datetime], Screen | None]]:
        store, cfg, tz, dwell = self.store, self.config, self.config.tz, entry.dwell
        teams = cfg.favorite_teams

        if entry.screen == "next_game":
            return [
                (lambda now, t=t: views.next_game_screen(
                    t, store.next_game(t, now), store.last_game(t), store.standings, tz, now, dwell))
                for t in teams
            ]
        if entry.screen == "standings":
            # Only one screen per division/conference even with several favorites in it.
            seen: set[str] = set()
            slots = []
            for t in teams:
                s = store.standing(t)
                group = None if s is None else (s.division if entry.scope == "division" else
                                                s.conference if entry.scope == "conference" else "league")
                if group in seen:
                    continue
                seen.add(group or t)
                slots.append(lambda now, t=t: views.standings_screen(store.standings, t, entry.scope, entry.limit, dwell))
            return slots
        if entry.screen == "scoreboard":
            pages = views.scoreboard_screens(self._scoreboard_games(), cfg.display.scoreboard_games_per_page, tz, dwell)
            return [
                (lambda now, n=n: _page(views.scoreboard_screens(
                    self._scoreboard_games(), cfg.display.scoreboard_games_per_page, tz, dwell), n))
                for n in range(len(pages))
            ]
        if entry.screen == "leaders":
            categories = cfg.leaders.skater_categories + cfg.leaders.goalie_categories
            return [
                (lambda now, c=c: views.leaders_screen(c, store.leaders.get(c, []), cfg.display.leaders_limit, dwell))
                for c in categories
            ]
        if entry.screen == "player_card":
            return [
                (lambda now, p=p: views.player_card_screen(store.players[p], dwell) if p in store.players else None)
                for p in cfg.favorite_players
            ]
        return []

    def _scoreboard_games(self) -> list[Game]:
        return [self.store.game(gid) or g for gid, g in self.store.games.items()]


def _page(screens: list[Screen], n: int) -> Screen | None:
    return screens[n] if n < len(screens) else None
