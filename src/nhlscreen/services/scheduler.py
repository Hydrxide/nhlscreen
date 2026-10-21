"""Decide what the board shows right now.

Priority:
  0. screens picked by hand ("Show now" on the preview page), each for its dwell
  1. alerts (favorite-team goal / overturned goal / end of 1st or 2nd / final), each held for N seconds
  2. a live favorite-team game (rotation resumes during intermissions if configured)
  3. the configured rotation (next game, scoreboard, standings, leaders, players)
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Callable

from ..config import SPOILER_DELAY_SECONDS, Config, RotationEntry
from ..nhl.models import Game, GameEvent, Goal
from ..store import Store
from .. import views
from ..views import Screen

# When several favorite teams are live at once, alternate between them.
LIVE_ALTERNATE_SECONDS = 15.0
# Live data reaches the Store only after the spoiler delay, so right after startup the
# rotation would show whatever slow data came first (and miss a live game). Hold the
# idle screen until the first scoreboard has landed, but never longer than this
# (plus however far the spoiler delay was raised above 25s).
WARMUP_MAX_SECONDS = 90.0


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
        # Goal alerts' events by screen id, so a shown alert can pick up assists
        # the NHL adds after the goal is first posted.
        self._goal_events: dict[str, GameEvent] = {}
        # Rotation position: which rotation entry, and which of its slots. Entries
        # are expanded into slots when reached, so data that arrives after
        # startup (standings, games) is never skipped for a whole cycle.
        self._entry = 0
        self._slot = -1
        self._builder: Callable[[datetime], Screen | None] | None = None
        # Screens picked from the preview page; they play before anything else.
        self._forced: deque[Screen] = deque()
        self._forced_on = False
        self._warm_since: datetime | None = None
        self._warm = False

    def reset(self) -> None:
        """Forget the current screen, queued alerts and rotation (e.g. favorites changed)."""
        self.current = None
        self._started_at = None
        self._alerts.clear()
        self._goal_events.clear()
        self._entry, self._slot, self._builder = 0, -1, None
        self._forced.clear()
        self._forced_on = False

    def force(self, screens: list[Screen]) -> None:
        """Clear the queue and play ``screens`` next, each for its dwell; then the
        rotation starts over from the top. Queued alerts are dropped."""
        self._alerts.clear()
        self._goal_events.clear()
        self._forced = deque(screens)
        self._entry, self._slot, self._builder = 0, -1, None
        self.current, self._started_at, self._forced_on = None, None, False

    def screens_for(self, entry: RotationEntry, now: datetime) -> list[Screen]:
        """Every screen a rotation entry would show right now (e.g. all league pages)."""
        return [s for s in (build(now) for build in self._slots_for(entry, now)) if s is not None]

    # ------------------------------------------------------------------ alerts

    def on_event(self, event: GameEvent) -> None:
        favorites = self.config.favorite_teams
        if not (event.away_abbrev in favorites or event.home_abbrev in favorites):
            return
        d = self.config.display
        if event.kind == "goal":
            screen = views.goal_alert_screen(event, d.alert_seconds, self._game_goals(event.game_id))
            self._goal_events[screen.id] = event
            self._alerts.append(screen)
        elif event.kind == "goal_overturned":
            # Drop a still-queued alert for the goal that was just taken back.
            self._alerts = deque(a for a in self._alerts if a.data.get("game_id") != event.game_id
                                 or a.type != "goal_alert")
            self._alerts.append(views.overturned_screen(event, d.alert_seconds))
        elif event.kind == "game_final":
            game = self.store.game(event.game_id)
            detail = self.store.details.get(event.game_id)
            self._alerts.append(views.final_alert_screen(event, game, detail, d.final_alert_seconds))
        elif event.kind == "period_end" and event.period and event.period_type != "SO":
            # After the 1st and 2nd always; after the 3rd or an OT only if it's tied,
            # i.e. the game goes on to (more) OT or a shootout.
            game = self.store.game(event.game_id)
            tied = (event.away_score or 0) == (event.home_score or 0)
            if game is not None and not game.state.is_final and (event.period < 3 or tied):
                detail = self.store.details.get(event.game_id)
                self._alerts.append(views.intermission_screen(event, game, detail, self.config.tz,
                                                              d.intermission_alert_seconds, self.clock()))

    # ------------------------------------------------------------------ main entry

    def tick(self, now: datetime | None = None) -> Screen:
        now = now or self.clock()
        tz = self.config.tz
        cur = self.current

        # 0. Screens picked by hand play first, each for its full dwell.
        if self._forced_on and cur and not self._expired(cur, now):
            return cur
        if self._forced:
            self._forced_on = True
            return self._show(self._forced.popleft(), now)
        if self._forced_on:  # the last picked screen just finished
            self._forced_on = False
            cur = self.current = None

        if not self._warmed_up(now):
            return self._show(views.idle_screen(now, tz), now, keep_start=cur is not None and cur.id == "idle")

        # 1. Alerts: let the current alert finish, then show the next queued one.
        if cur and cur.priority >= views.PRIORITY_ALERT and not self._expired(cur, now):
            return self._show(self._refresh_goal_alert(cur), now, keep_start=True)
        if self._alerts:
            screen = self._alerts.popleft()
            queued = {a.id for a in self._alerts} | {screen.id}
            self._goal_events = {k: v for k, v in self._goal_events.items() if k in queued}
            return self._show(self._refresh_goal_alert(screen), now)

        # 2. Live favorite game.
        live = self._live_favorite_games()
        show_live = [
            g for g in live
            if not (self.config.display.rotate_during_intermission and g.clock and g.clock.in_intermission)
        ]
        if show_live:
            game = show_live[int(now.timestamp() // LIVE_ALTERNATE_SECONDS) % len(show_live)]
            screen = views.live_game_screen(game, self.store.details.get(game.id), tz, LIVE_ALTERNATE_SECONDS, now,
                                            as_of=self.store.clock_as_of(game.id), clock_rate=self.clock_rate,
                                            segment=self.store.clock_segment(game.id, now))
            return self._show(screen, now, keep_start=cur is not None and cur.id == screen.id)

        # 3. Rotation: refresh the current rotation screen's data until its dwell expires.
        if cur and cur.priority == views.PRIORITY_ROTATION and not self._expired(cur, now):
            refreshed = self._builder(now) if self._builder else None
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

    def _refresh_goal_alert(self, screen: Screen) -> Screen:
        """Rebuild a goal alert from the latest (delayed) play-by-play: assists, and
        sometimes the scorer, are filled in after the goal first appears."""
        event = self._goal_events.get(screen.id)
        if event is None or event.goal is None or event.goal.event_id is None:
            return screen
        detail = self.store.details.get(event.game_id)
        fresh = next((g for g in detail.game.goals if g.event_id == event.goal.event_id), None) if detail else None
        if fresh is None or fresh == event.goal:
            return screen
        event = event.model_copy(update={"goal": fresh})
        self._goal_events[screen.id] = event
        return views.goal_alert_screen(event, screen.dwell_seconds, detail.game.goals)

    def _game_goals(self, game_id: int) -> list[Goal] | None:
        detail = self.store.details.get(game_id)
        return detail.game.goals if detail else None

    def _warmed_up(self, now: datetime) -> bool:
        if self._warm:
            return True
        if self._warm_since is None:
            self._warm_since = now
        extra = (self.config.display.spoiler_delay_seconds - SPOILER_DELAY_SECONDS) / self.clock_rate
        waited = (now - self._warm_since).total_seconds()
        self._warm = "games" in self.store.updated_at or waited >= WARMUP_MAX_SECONDS + extra
        return self._warm

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

    def _advance(self, now: datetime) -> Screen:
        """Move to the next rotation screen that has something to show."""
        rotation = self.config.display.rotation
        # Each entry is expanded against current data when reached; visit every
        # entry at most once (plus the rest of the current one) before idling.
        for _ in range(len(rotation) + 1):
            if not rotation:
                break
            slots = self._slots_for(rotation[self._entry % len(rotation)], now)
            while self._slot + 1 < len(slots):
                self._slot += 1
                screen = slots[self._slot](now)
                if screen is not None:
                    self._builder = slots[self._slot]
                    return self._show(screen, now)
            self._entry = (self._entry + 1) % len(rotation)
            self._slot = -1
        # Nothing to show yet (e.g. data still loading): start from the top next time.
        self._entry, self._slot, self._builder = 0, -1, None
        return self._show(views.idle_screen(now, self.config.tz), now)

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
            return self._standings_slots(entry)
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

    def _standings_slots(self, entry: RotationEntry) -> list[Callable[[datetime], Screen | None]]:
        store, dwell, followed = self.store, entry.dwell, self.config.favorite_teams

        def groups(attr: str) -> dict[str, str]:
            """Division or conference -> first followed team in it (follow order, no repeats)."""
            out: dict[str, str] = {}
            for t in followed:
                s = store.standing(t)
                if s is not None:
                    out.setdefault(getattr(s, attr), t)
            return out

        if entry.scope == "division":  # only divisions that contain a followed team
            return [
                (lambda now, t=t: views.standings_screen(store.standings, t, "division", entry.limit, dwell, followed))
                for t in groups("division").values()
            ]
        if entry.scope == "wildcard":
            return [
                (lambda now, c=c: views.wildcard_screen(store.standings, c, followed, dwell))
                for c in groups("conference")
            ]
        if entry.scope == "league":
            pages = len(views.standings_pages(store.standings, "league", followed, dwell))
            return [
                (lambda now, n=n: _page(views.standings_pages(store.standings, "league", followed, dwell), n))
                for n in range(pages)
            ]
        slots: list[Callable[[datetime], Screen | None]] = []  # conference, paged
        for conf in groups("conference"):
            pages = len(views.standings_pages(store.standings, "conference", followed, dwell, conf))
            slots += [
                (lambda now, n=n, c=conf: _page(
                    views.standings_pages(store.standings, "conference", followed, dwell, c), n))
                for n in range(pages)
            ]
        return slots

    def _scoreboard_games(self) -> list[Game]:
        return [self.store.game(gid) or g for gid, g in self.store.games.items()]


def _page(screens: list[Screen], n: int) -> Screen | None:
    return screens[n] if n < len(screens) else None
