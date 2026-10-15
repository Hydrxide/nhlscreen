"""Normalized domain models. Nothing outside nhl/ should see raw API JSON."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field


class GameState(StrEnum):
    FUTURE = "FUT"
    PREGAME = "PRE"
    LIVE = "LIVE"
    CRITICAL = "CRIT"  # final minutes of a close game / OT
    FINAL = "FINAL"
    OFFICIAL = "OFF"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, value: str | None) -> GameState:
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def is_live(self) -> bool:
        return self in (GameState.LIVE, GameState.CRITICAL)

    @property
    def is_final(self) -> bool:
        return self in (GameState.FINAL, GameState.OFFICIAL)

    @property
    def is_upcoming(self) -> bool:
        return self in (GameState.FUTURE, GameState.PREGAME)


class TeamLine(BaseModel):
    id: int | None = None
    abbrev: str
    name: str = ""
    score: int | None = None
    sog: int | None = None


class Clock(BaseModel):
    time_remaining: str = "20:00"
    seconds_remaining: int = 1200
    running: bool = False
    in_intermission: bool = False


class Situation(BaseModel):
    """On-ice manpower. Decoded from the NHL's 4-digit situationCode."""

    away_skaters: int = 5
    home_skaters: int = 5
    away_goalie_in: bool = True
    home_goalie_in: bool = True
    # e.g. "PP" or "EN" per team, as reported by the API when available
    away_tags: list[str] = Field(default_factory=list)
    home_tags: list[str] = Field(default_factory=list)
    # Until the current situation (e.g. a power play) ends; counts down with the game clock.
    time_remaining: str | None = None
    seconds_remaining: int | None = None

    # An extra attacker (goalie pulled) doesn't change the official strength:
    # 6 skaters with an empty net is still "5" for power-play purposes.
    @property
    def away_strength(self) -> int:
        return self.away_skaters - (0 if self.away_goalie_in else 1)

    @property
    def home_strength(self) -> int:
        return self.home_skaters - (0 if self.home_goalie_in else 1)

    @property
    def strength(self) -> str:
        return f"{self.away_strength}v{self.home_strength}"

    @property
    def power_play_team(self) -> Literal["away", "home"] | None:
        if self.away_strength > self.home_strength:
            return "away"
        if self.home_strength > self.away_strength:
            return "home"
        return None


class Goal(BaseModel):
    event_id: int | None = None
    period: int
    period_type: str = "REG"
    time_in_period: str
    team_abbrev: str
    scorer_id: int | None = None
    scorer_name: str
    scorer_season_total: int | None = None
    assists: list[str] = Field(default_factory=list)
    strength: str = "ev"  # ev | pp | sh
    modifier: str | None = None  # e.g. "empty-net"
    away_score: int
    home_score: int


class Penalty(BaseModel):
    event_id: int | None = None
    period: int
    period_type: str = "REG"
    time_in_period: str
    team_abbrev: str
    player_name: str | None = None
    description: str
    duration: int


class Game(BaseModel):
    id: int
    state: GameState
    start_time_utc: datetime
    away: TeamLine
    home: TeamLine
    venue: str | None = None
    period: int | None = None
    period_type: str | None = None  # REG | OT | SO
    clock: Clock | None = None
    situation: Situation | None = None
    goals: list[Goal] = Field(default_factory=list)
    last_period_type: str | None = None  # from gameOutcome once final
    broadcasts: list[str] = Field(default_factory=list)

    def involves(self, abbrev: str) -> bool:
        return abbrev.upper() in (self.away.abbrev, self.home.abbrev)

    def side_of(self, abbrev: str) -> Literal["away", "home"] | None:
        abbrev = abbrev.upper()
        if self.away.abbrev == abbrev:
            return "away"
        if self.home.abbrev == abbrev:
            return "home"
        return None


class TeamGameStat(BaseModel):
    category: str
    away: Any
    home: Any


class ThreeStar(BaseModel):
    star: int
    player_id: int | None = None
    name: str
    team_abbrev: str
    position: str | None = None
    goals: int | None = None
    assists: int | None = None
    points: int | None = None


class Play(BaseModel):
    """A play-by-play event, trimmed to what the event detector needs."""

    event_id: int
    type: str  # goal | penalty | period-start | period-end | game-end | ...
    period: int
    period_type: str = "REG"
    time_in_period: str
    sort_order: int = 0


class GameDetail(BaseModel):
    """Everything known about one game in depth (play-by-play + stats)."""

    game: Game
    plays: list[Play] = Field(default_factory=list)
    penalties: list[Penalty] = Field(default_factory=list)
    team_stats: list[TeamGameStat] = Field(default_factory=list)
    three_stars: list[ThreeStar] = Field(default_factory=list)


class TeamStanding(BaseModel):
    abbrev: str
    name: str
    conference: str
    division: str
    games_played: int
    wins: int
    losses: int
    ot_losses: int
    points: int
    point_pctg: float
    goal_differential: int
    streak: str | None = None  # e.g. "W6"
    l10: str | None = None  # e.g. "6-1-1"
    division_rank: int
    conference_rank: int
    league_rank: int
    wildcard_rank: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}-{self.ot_losses}"


class ScheduledGame(BaseModel):
    id: int
    state: GameState
    start_time_utc: datetime
    away_abbrev: str
    home_abbrev: str
    away_score: int | None = None
    home_score: int | None = None
    venue: str | None = None
    broadcasts: list[str] = Field(default_factory=list)
    last_period_type: str | None = None


class LeaderEntry(BaseModel):
    player_id: int
    name: str
    team_abbrev: str
    position: str | None = None
    sweater_number: int | None = None
    value: float


class PlayerSummary(BaseModel):
    id: int
    name: str
    first_name: str
    last_name: str
    sweater_number: int | None = None
    position: str
    team_abbrev: str | None = None
    season: int | None = None
    season_stats: dict[str, Any] = Field(default_factory=dict)
    last_game: dict[str, Any] | None = None

    @property
    def is_goalie(self) -> bool:
        return self.position == "G"


EventKind = Literal["goal", "goal_overturned", "penalty", "period_start", "period_end", "game_final"]


class GameEvent(BaseModel):
    """Something happened in a game that a display might want to react to."""

    kind: EventKind
    game_id: int
    key: str  # stable identity for de-duplication
    team_abbrev: str | None = None
    away_abbrev: str
    home_abbrev: str
    away_score: int | None = None
    home_score: int | None = None
    period: int | None = None
    period_type: str | None = None
    goal: Goal | None = None
    penalty: Penalty | None = None
    occurred_at: datetime
