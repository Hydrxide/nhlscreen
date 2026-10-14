"""Wires client, store, poller and scheduler together and runs the screen loop."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import Config, RotationEntry
from .nhl import parsers
from .nhl.client import NHLClient, NHLNotFound, NHLUnavailable
from .nhl.models import GameEvent
from .services.poller import Poller
from .services.scheduler import Scheduler
from .static.teams import TEAMS
from .store import Store
from . import views
from .views import Screen

log = logging.getLogger(__name__)


class Runtime:
    def __init__(
        self,
        config: Config,
        transport: httpx.AsyncBaseTransport | None = None,
        speed: float = 1.0,
        tick_seconds: float = 0.5,
        state_path: Path | None = None,
    ) -> None:
        self.config = config
        # From config.yaml; "reset" (displayteam --reset, the preview page) returns to these.
        self.configured_teams = list(config.favorite_teams)
        self.configured_players = list(config.favorite_players)
        self.state_path = state_path
        self._load_state()
        self._favorites_lock = asyncio.Lock()
        self.store = Store()
        self.client = NHLClient(config.api_base_url, config.user_agent, transport=transport)
        self.scheduler = Scheduler(self.store, config, clock_rate=speed)
        self.poller = Poller(self.client, self.store, config, on_event=self.scheduler.on_event, speed=speed)
        self.tick_seconds = tick_seconds
        self._screen_task: asyncio.Task | None = None
        self._last_key: tuple | None = None

    @property
    def screen(self) -> Screen | None:
        return self.scheduler.current

    async def start(self) -> None:
        self.poller.start()
        self._screen_task = asyncio.create_task(self._screen_loop(), name="screen")

    async def stop(self) -> None:
        if self._screen_task:
            self._screen_task.cancel()
            await asyncio.gather(self._screen_task, return_exceptions=True)
        await self.poller.stop()
        await self.client.aclose()

    # ------------------------------------------------------------------ favorite teams

    async def set_favorite_teams(self, teams: list[str] | None) -> list[str]:
        """Switch favorite teams at runtime. ``None`` restores config.yaml's teams.

        Raises ValueError for unknown team codes.
        """
        new = self.configured_teams if teams is None else list(dict.fromkeys(t.upper() for t in teams))
        unknown = [t for t in new if t not in TEAMS]
        if unknown or not new:
            raise ValueError(f"unknown team code(s): {', '.join(unknown) or '(none given)'}")
        async with self._favorites_lock:
            self.config.favorite_teams = new
            self._save_state("favorite_teams", None if teams is None else new)
            await self.poller.favorites_changed()
            self.scheduler.reset()
            self.tick()
        log.info("favorite teams -> %s", ", ".join(new))
        # Let every open control page update, whoever made the change.
        self.store.publish({"type": "teams", **self.teams_state(include_screen=False)})
        return new

    # ------------------------------------------------------------------ favorite players

    MAX_PLAYERS = 10

    async def set_favorite_players(self, players: list[int] | None) -> list[int]:
        """Switch favorite players at runtime. ``None`` restores config.yaml's players.

        Every ID is looked up with the NHL first; raises ValueError naming the
        bad ID (or saying the API is unreachable) without changing anything.
        """
        new = self.configured_players if players is None else list(dict.fromkeys(players))
        if len(new) > self.MAX_PLAYERS:
            raise ValueError(f"at most {self.MAX_PLAYERS} favorite players")
        found = []
        for pid in new:
            try:
                found.append(parsers.parse_player(await self.client.player(pid)))
            except NHLNotFound:
                raise ValueError(f"No NHL player with ID {pid}") from None
            except NHLUnavailable:
                raise ValueError(f"Couldn't reach the NHL API to look up player {pid}; try again") from None
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"No NHL player with ID {pid}") from None
        async with self._favorites_lock:
            self.config.favorite_players = new
            for player in found:
                self.store.set_player(player)
            self._save_state("favorite_players", None if players is None else new)
        log.info("favorite players -> %s", ", ".join(p.name for p in found) or "(none)")
        self.store.publish({"type": "players", **self.players_state()})
        return new

    def players_state(self) -> dict[str, Any]:
        def describe(pid: int) -> dict[str, Any]:
            p = self.store.players.get(pid)
            if p is None:  # not fetched yet
                return {"id": pid, "name": None}
            return {"id": pid, "name": p.name, "team": p.team_abbrev, "position": p.position,
                    "number": p.sweater_number}

        return {
            "favorite_players": [describe(pid) for pid in self.config.favorite_players],
            "configured_players": self.configured_players,
        }

    def teams_state(self, include_screen: bool = True) -> dict[str, Any]:
        """Favorite teams plus every team, grouped for a picker UI."""
        divisions = {s.abbrev: (s.conference, s.division) for s in self.store.standings}
        # Only games still worth switching to: live now, or yet to start.
        status: dict[str, dict[str, str]] = {}
        for game in (self.store.game(g.id) or g for g in self.store.games.values()):
            if game.state.is_live:
                entry = {"state": "live", "label": views.game_status(game, self.config.tz)}
            elif game.state.is_upcoming:
                entry = {"state": "upcoming", "label": views.local_time(game.start_time_utc, self.config.tz)}
            else:
                continue
            for abbrev in (game.away.abbrev, game.home.abbrev):
                if status.get(abbrev, {}).get("state") != "live":
                    status[abbrev] = entry
        teams = [
            {
                "abbrev": t.abbrev,
                "name": t.name,
                "color": t.display,
                "conference": divisions.get(t.abbrev, ("", ""))[0],
                "division": divisions.get(t.abbrev, ("", ""))[1],
                "game": status.get(t.abbrev),  # None | {"state": "live"|"upcoming", "label": ...}
            }
            for t in sorted(TEAMS.values(), key=lambda t: t.abbrev)
        ]
        state: dict[str, Any] = {
            "favorite_teams": self.config.favorite_teams,
            "configured_teams": self.configured_teams,
            "teams": teams,
        }
        if include_screen:
            state["screen"] = self.screen.model_dump(mode="json") if self.screen else None
        return state

    def _read_state(self) -> dict[str, Any]:
        if not (self.state_path and self.state_path.exists()):
            return {}
        try:
            state = json.loads(self.state_path.read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.state_path, exc)
            return {}

    def _load_state(self) -> None:
        """Choices made at runtime (picker, displayteam) override config.yaml."""
        state = self._read_state()
        teams = state.get("favorite_teams")
        if teams and all(t in TEAMS for t in teams):
            log.info("favorite teams from %s: %s (config.yaml has %s)",
                     self.state_path, ", ".join(teams), ", ".join(self.configured_teams))
            self.config.favorite_teams = list(teams)
        players = state.get("favorite_players")
        if isinstance(players, list) and all(isinstance(p, int) for p in players):
            log.info("favorite players from %s: %s", self.state_path, players)
            self.config.favorite_players = list(players)

    def _save_state(self, key: str, value: Any | None) -> None:
        """Set (or with None, clear) one saved choice; other choices are kept."""
        if not self.state_path:
            return
        state = self._read_state()
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
        try:
            if state:
                self.state_path.write_text(json.dumps(state) + "\n")
            else:
                self.state_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not save state to %s: %s", self.state_path, exc)

    # ------------------------------------------------------------------ show now

    # Buttons on the preview page: id -> (label, rotation entry used if config has none).
    SHOW_MENU: dict[str, tuple[str, RotationEntry | None]] = {
        "next_game": ("Next game", RotationEntry(screen="next_game", dwell=10)),
        "scoreboard": ("Scoreboard", RotationEntry(screen="scoreboard", dwell=8)),
        "standings_division": ("Division", RotationEntry(screen="standings", scope="division", dwell=12)),
        "standings_wildcard": ("Wild card", RotationEntry(screen="standings", scope="wildcard", dwell=12)),
        "standings_league": ("League standings", RotationEntry(screen="standings", scope="league", dwell=10)),
        "leaders": ("Leaders", RotationEntry(screen="leaders", dwell=8)),
        "player_card": ("Player cards", RotationEntry(screen="player_card", dwell=8)),
        "goal_alert": ("Goal alert", None),
        "final_alert": ("Final score", None),
    }

    def _entry_for(self, kind: str) -> RotationEntry:
        """The configured rotation entry for this screen (its dwell), else the default."""
        default = self.SHOW_MENU[kind][1]
        assert default is not None
        for entry in self.config.display.rotation:
            if entry.screen == default.screen and (entry.screen != "standings" or entry.scope == default.scope):
                return entry
        return default

    def _recent_game_id(self, final_only: bool) -> int | None:
        """The followed teams' most recent game (today's live/final game, or the last result)."""
        candidates = []
        for team in self.config.favorite_teams:
            for g in self.store.games_for(team):
                game = self.store.game(g.id) or g
                if game.state.is_final or (game.state.is_live and not final_only):
                    candidates.append((game.start_time_utc, game.id))
            last = self.store.last_game(team)
            if last:
                candidates.append((last.start_time_utc, last.id))
        return max(candidates)[1] if candidates else None

    def show_menu(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        menu = []
        for kind, (label, entry) in self.SHOW_MENU.items():
            if entry is None:
                ok = self._recent_game_id(final_only=kind == "final_alert") is not None
                why = None if ok else "No recent game for your teams yet"
            else:
                ok = bool(self.scheduler.screens_for(self._entry_for(kind), now))
                why = None if ok else "Nothing to show yet (data still loading, or none today)"
            menu.append({"id": kind, "label": label, "available": ok, "reason": why})
        return menu

    async def show_now(self, kind: str) -> list[Screen]:
        """Play ``kind`` next (clearing the queue); the rotation resumes from the top after."""
        if kind not in self.SHOW_MENU:
            raise ValueError(f"unknown screen {kind!r}")
        label = self.SHOW_MENU[kind][0]
        if kind in ("goal_alert", "final_alert"):
            screens = [await self._alert_preview(kind)]
        else:
            screens = self.scheduler.screens_for(self._entry_for(kind), datetime.now(timezone.utc))
        if not screens:
            raise ValueError(f"Nothing to show for {label} yet")
        self.scheduler.force(screens)
        self.tick()
        log.info("show now: %s (%d screen%s)", label, len(screens), "" if len(screens) == 1 else "s")
        return screens

    async def _alert_preview(self, kind: str) -> Screen:
        """A goal or final alert rebuilt from the followed teams' most recent real game."""
        game_id = self._recent_game_id(final_only=kind == "final_alert")
        if game_id is None:
            raise ValueError("No recent game for your teams yet")
        try:
            detail = parsers.parse_play_by_play(await self.client.play_by_play(game_id))
        except NHLUnavailable:
            raise ValueError("Couldn't reach the NHL API; try again") from None
        game, d, now = detail.game, self.config.display, datetime.now(timezone.utc)
        base = {"game_id": game.id, "away_abbrev": game.away.abbrev, "home_abbrev": game.home.abbrev,
                "occurred_at": now}
        if kind == "goal_alert":
            goals = [g for g in game.goals if g.team_abbrev in self.config.favorite_teams] or game.goals
            if not goals:
                raise ValueError("No goals in your team's most recent game")
            goal = goals[-1]
            event = GameEvent(kind="goal", key=f"preview:{game.id}:{goal.event_id}", team_abbrev=goal.team_abbrev,
                              away_score=goal.away_score, home_score=goal.home_score, period=goal.period,
                              period_type=goal.period_type, goal=goal, **base)
            return views.goal_alert_screen(event, d.alert_seconds)
        try:
            detail.three_stars = parsers.parse_three_stars(await self.client.landing(game_id))
        except NHLUnavailable:
            pass  # the final screen works without stars
        event = GameEvent(kind="game_final", key=f"preview:final:{game.id}", away_score=game.away.score,
                          home_score=game.home.score, **base)
        return views.final_alert_screen(event, game, detail, d.final_alert_seconds)

    # ------------------------------------------------------------------ screen

    def tick(self) -> Screen:
        screen = self.scheduler.tick()
        key = screen.content_key()
        if key != self._last_key:
            if self._last_key is None or key[0] != self._last_key[0]:
                log.info("screen -> %s | %s", screen.id, " / ".join(screen.lines[:2]))
            self._last_key = key
            self.store.publish({"type": "screen", "screen": screen.model_dump(mode="json")})
        return screen

    async def _screen_loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("screen tick failed")
            await asyncio.sleep(self.tick_seconds)
