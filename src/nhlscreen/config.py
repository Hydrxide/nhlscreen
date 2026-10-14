"""YAML configuration loaded into typed pydantic models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator

ScreenKind = Literal["next_game", "scoreboard", "standings", "leaders", "player_card"]


class PollingConfig(BaseModel):
    # Favorite-team live game: play-by-play carries score, clock, situation, events.
    live_poll_seconds: float = Field(5.0, ge=2.0)
    # Team game stats (SOG, faceoffs, PP, hits) from the right-rail endpoint.
    live_stats_seconds: float = Field(30.0, ge=10.0)
    # League scoreboard. The CDN caches it ~19s, so faster polling gains nothing.
    scoreboard_live_seconds: float = Field(20.0, ge=10.0)
    scoreboard_pregame_seconds: float = Field(60.0, ge=10.0)
    scoreboard_idle_seconds: float = Field(900.0, ge=60.0)
    # How long before puck drop a game counts as "pregame" for faster polling.
    pregame_window_seconds: float = 3600.0
    standings_seconds: float = 3600.0
    leaders_seconds: float = 3600.0
    schedule_seconds: float = 21600.0
    players_seconds: float = 3600.0


class RotationEntry(BaseModel):
    screen: ScreenKind
    dwell: float = Field(8.0, gt=0)
    # standings only
    scope: Literal["division", "conference", "league"] = "division"
    limit: int | None = None


class DisplayConfig(BaseModel):
    alert_seconds: float = 15.0
    final_alert_seconds: float = 20.0
    rotate_during_intermission: bool = True
    # LED matrix size in pixels (layouts are designed for 128x64).
    matrix_width: int = Field(128, ge=32)
    matrix_height: int = Field(64, ge=16)
    scoreboard_games_per_page: int = Field(4, ge=1)
    leaders_limit: int = Field(5, ge=1, le=20)
    rotation: list[RotationEntry] = Field(
        default_factory=lambda: [
            RotationEntry(screen="next_game", dwell=10),
            RotationEntry(screen="scoreboard", dwell=8),
            RotationEntry(screen="standings", scope="division", dwell=12),
            RotationEntry(screen="leaders", dwell=8),
            RotationEntry(screen="player_card", dwell=8),
        ]
    )


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


class LeadersConfig(BaseModel):
    skater_categories: list[str] = Field(default_factory=lambda: ["points", "goals", "assists"])
    goalie_categories: list[str] = Field(default_factory=lambda: ["wins", "savePctg"])


class Config(BaseModel):
    favorite_teams: list[str] = Field(default_factory=lambda: ["TOR"])
    # NHL player IDs: Gavin McKenna, Sergei Bobrovsky
    favorite_players: list[int] = Field(default_factory=lambda: [8486067, 8475683])
    timezone: str = "America/Toronto"
    api_base_url: str = "https://api-web.nhle.com/v1"
    user_agent: str = "nhlscreen/0.1 (DIY LED scoreboard)"
    server: ServerConfig = Field(default_factory=ServerConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    leaders: LeadersConfig = Field(default_factory=LeadersConfig)

    @field_validator("favorite_teams")
    @classmethod
    def _upper(cls, teams: list[str]) -> list[str]:
        return [t.upper() for t in teams]

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, tz: str) -> str:
        ZoneInfo(tz)  # raises if unknown
        return tz

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Config.model_validate(data)
