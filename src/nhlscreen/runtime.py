"""Wires client, store, poller and scheduler together and runs the screen loop."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .nhl.client import NHLClient
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
        # Teams from config.yaml; `displayteam --reset` returns to these.
        self.configured_teams = list(config.favorite_teams)
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
            self._save_state(None if teams is None else new)
            await self.poller.favorites_changed()
            self.scheduler.reset()
            self.tick()
        log.info("favorite teams -> %s", ", ".join(new))
        # Let every open control page update, whoever made the change.
        self.store.publish({"type": "teams", **self.teams_state(include_screen=False)})
        return new

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

    def _load_state(self) -> None:
        if not (self.state_path and self.state_path.exists()):
            return
        try:
            teams = json.loads(self.state_path.read_text()).get("favorite_teams")
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.state_path, exc)
            return
        if teams and all(t in TEAMS for t in teams):
            log.info("favorite teams from %s: %s (config.yaml has %s)",
                     self.state_path, ", ".join(teams), ", ".join(self.configured_teams))
            self.config.favorite_teams = list(teams)

    def _save_state(self, teams: list[str] | None) -> None:
        if not self.state_path:
            return
        try:
            if teams is None:
                self.state_path.unlink(missing_ok=True)
            else:
                self.state_path.write_text(json.dumps({"favorite_teams": teams}) + "\n")
        except OSError as exc:
            log.warning("could not save state to %s: %s", self.state_path, exc)

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
