"""HTTP + WebSocket API consumed by the LED renderer (and handy for debugging).

  GET  /screen/current     what the board should show right now
  GET  /games              today's games (live data merged in)
  GET  /games/{id}         one game in depth (play-by-play summary, stats)
  GET  /standings          ?scope=division|conference|league&team=TOR
  GET  /leaders            leader categories
  GET  /teams/{abbrev}     standing, live/next/last game for a team
  GET  /players/{id}       favorite player summary
  GET  /events/recent      last N game events
  GET  /health             data freshness + API backoff state
  GET  /control/teams      current favorite teams
  PUT  /control/teams      {"teams": ["EDM"]} switch teams; {"teams": null} back to config.yaml
  GET  /control/players    current favorite players (id, name, team)
  PUT  /control/players    {"players": [8486067, 8475683]} switch; {"players": null} back to config.yaml
  GET  /control/delay      anti-spoiler delay (seconds live game data is held back)
  PUT  /control/delay      {"seconds": 30} change it; {"enabled": false} turn it off (keeps the
                           seconds); {"seconds": null} back to config.yaml
  GET  /control/screens    the "Show now" menu (which screens can be shown right now)
  POST /control/show       {"screen": "standings_league"} clear the queue and show it next
  GET  /render/frame       current screen as a raw RGB frame (width*height*3 bytes)
  GET  /render/frame.png   current screen drawn as LED dots
  WS   /events             pushes {"type": "screen"|"event"|"teams"|"players"|"delay", ...}
  GET  /debug              browser preview of the board + team picker (also at /)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from ..render import render
from ..runtime import Runtime

_DEBUG_HTML = Path(__file__).with_name("debug.html")


class TeamsRequest(BaseModel):
    teams: list[str] | None  # None = revert to config.yaml


class PlayersRequest(BaseModel):
    players: list[int] | None  # None = revert to config.yaml


class DelayRequest(BaseModel):
    seconds: float | None = None  # sent as null = revert to config.yaml
    enabled: bool | None = None


class ShowRequest(BaseModel):
    screen: str  # an id from GET /control/screens


def create_app(runtime: Runtime, manage_lifecycle: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if manage_lifecycle:
            await runtime.start()
        yield
        if manage_lifecycle:
            await runtime.stop()

    app = FastAPI(title="nhlscreen", lifespan=lifespan)
    store = runtime.store

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "now": datetime.now(timezone.utc).isoformat(),
            "updated_at": {k: v.isoformat() for k, v in store.updated_at.items()},
            "api_backoff_seconds": round(runtime.client.backoff_remaining, 1),
            "api_requests": runtime.client.request_count,
            "tracked_games": sorted(runtime.poller._tracked),
        }

    @app.get("/screen/current")
    def current_screen() -> dict[str, Any]:
        screen = runtime.screen or runtime.tick()
        return screen.model_dump(mode="json")

    @app.get("/games")
    def games() -> list[dict[str, Any]]:
        merged = [store.game(gid) or g for gid, g in store.games.items()]
        return [g.model_dump(mode="json") for g in sorted(merged, key=lambda g: (g.start_time_utc, g.id))]

    @app.get("/games/{game_id}")
    def game(game_id: int) -> dict[str, Any]:
        detail = store.details.get(game_id)
        if detail:
            # Plays are long and only needed internally; return the useful parts.
            return detail.model_dump(mode="json", exclude={"plays"})
        g = store.games.get(game_id)
        if g is None:
            raise HTTPException(404, "game not found")
        return {"game": g.model_dump(mode="json")}

    @app.get("/standings")
    def standings(scope: str = "league", team: str | None = None) -> list[dict[str, Any]]:
        rows = store.standings
        if team:
            mine = store.standing(team)
            if mine is None:
                raise HTTPException(404, "team not found")
            if scope == "division":
                rows = [s for s in rows if s.division == mine.division]
            elif scope == "conference":
                rows = [s for s in rows if s.conference == mine.conference]
        return [s.model_dump(mode="json") for s in rows]

    @app.get("/leaders")
    def leaders() -> dict[str, Any]:
        return {cat: [e.model_dump(mode="json") for e in entries] for cat, entries in store.leaders.items()}

    @app.get("/teams/{abbrev}")
    def team(abbrev: str) -> dict[str, Any]:
        abbrev = abbrev.upper()
        now = datetime.now(timezone.utc)
        today = [store.game(g.id) or g for g in store.games_for(abbrev)]
        nxt, last, standing = store.next_game(abbrev, now), store.last_game(abbrev), store.standing(abbrev)
        if not (today or nxt or last or standing):
            raise HTTPException(404, "no data for team")
        return {
            "abbrev": abbrev,
            "standing": standing.model_dump(mode="json") if standing else None,
            "today": [g.model_dump(mode="json") for g in today],
            "next_game": nxt.model_dump(mode="json") if nxt else None,
            "last_game": last.model_dump(mode="json") if last else None,
        }

    @app.get("/players/{player_id}")
    def player(player_id: int) -> dict[str, Any]:
        p = store.players.get(player_id)
        if p is None:
            raise HTTPException(404, "player not tracked (add it to favorite_players)")
        return p.model_dump(mode="json")

    @app.get("/events/recent")
    def recent_events(limit: int = 20) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in list(store.recent_events)[-limit:]]

    @app.get("/control/teams")
    def get_teams() -> dict[str, Any]:
        return runtime.teams_state()

    @app.put("/control/teams")
    async def set_teams(req: TeamsRequest) -> dict[str, Any]:
        try:
            await runtime.set_favorite_teams(req.teams)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return runtime.teams_state()

    @app.get("/control/players")
    def get_players() -> dict[str, Any]:
        return runtime.players_state()

    @app.put("/control/players")
    async def set_players(req: PlayersRequest) -> dict[str, Any]:
        try:
            await runtime.set_favorite_players(req.players)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return runtime.players_state()

    @app.get("/control/delay")
    def get_delay() -> dict[str, Any]:
        return runtime.delay_state()

    @app.put("/control/delay")
    def set_delay(req: DelayRequest) -> dict[str, Any]:
        if not req.model_fields_set:
            raise HTTPException(400, 'send "seconds" and/or "enabled"')
        try:
            if "seconds" in req.model_fields_set:
                runtime.set_spoiler_delay(req.seconds)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if req.enabled is not None:
            runtime.set_spoiler_delay_enabled(req.enabled)
        return runtime.delay_state()

    @app.get("/control/screens")
    def show_menu() -> dict[str, Any]:
        return {"screens": runtime.show_menu()}

    @app.post("/control/show")
    async def show_now(req: ShowRequest) -> dict[str, Any]:
        try:
            screens = await runtime.show_now(req.screen)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"showing": [s.id for s in screens],
                "screen": runtime.screen.model_dump(mode="json") if runtime.screen else None}

    @app.websocket("/events")
    async def events_ws(ws: WebSocket) -> None:
        await ws.accept()
        queue = store.subscribe()
        try:
            if runtime.screen:
                await ws.send_json({"type": "screen", "screen": runtime.screen.model_dump(mode="json")})
            while True:
                await ws.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass  # client went away
        finally:
            store.unsubscribe(queue)

    def _frame():
        screen = runtime.screen or runtime.tick()
        d = runtime.config.display
        return render(screen.model_dump(mode="json"), width=d.matrix_width, height=d.matrix_height)

    @app.get("/render/frame")
    def render_frame() -> Response:
        frame = _frame()
        return Response(
            frame.to_bytes(),
            media_type="application/octet-stream",
            headers={"X-Width": str(frame.width), "X-Height": str(frame.height), "Cache-Control": "no-store"},
        )

    @app.get("/render/frame.png")
    def render_frame_png(pitch: int = 6) -> Response:
        return Response(_frame().to_png(pitch=max(2, min(pitch, 12))), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/debug")

    @app.get("/debug", response_class=HTMLResponse)
    def debug() -> str:
        return _DEBUG_HTML.read_text(encoding="utf-8")

    return app
