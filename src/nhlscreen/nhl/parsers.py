"""Raw NHL API JSON -> domain models.

The NHL web API is unofficial and changes without notice. Every field access
lives in this module, and parsing is deliberately forgiving (``.get`` with
defaults) so a renamed field degrades one value instead of crashing the board.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import (
    Clock,
    Game,
    GameDetail,
    GameState,
    Goal,
    LeaderEntry,
    Penalty,
    Play,
    PlayerSummary,
    ScheduledGame,
    Situation,
    TeamGameStat,
    TeamLine,
    TeamStanding,
    ThreeStar,
)

JSON = dict[str, Any]


def _text(value: Any) -> str:
    """Localized names arrive as {"default": "...", "fr": "..."}."""
    if isinstance(value, dict):
        return str(value.get("default", ""))
    return "" if value is None else str(value)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _broadcasts(raw: JSON) -> list[str]:
    seen: list[str] = []
    for b in raw.get("tvBroadcasts") or []:
        network = b.get("network")
        if network and network not in seen:
            seen.append(network)
    return seen


def _team_line(raw: JSON) -> TeamLine:
    return TeamLine(
        id=raw.get("id"),
        abbrev=raw.get("abbrev", "???"),
        name=_text(raw.get("commonName") or raw.get("name")),
        score=raw.get("score"),
        sog=raw.get("sog"),
    )


def _clock(raw: JSON | None) -> Clock | None:
    if not raw:
        return None
    return Clock(
        time_remaining=raw.get("timeRemaining", "00:00"),
        seconds_remaining=int(raw.get("secondsRemaining", 0)),
        running=bool(raw.get("running", False)),
        in_intermission=bool(raw.get("inIntermission", False)),
    )


def decode_situation_code(code: str | None) -> Situation | None:
    """situationCode is 4 digits: away goalie, away skaters, home skaters, home goalie."""
    if not code or len(code) != 4 or not code.isdigit():
        return None
    return Situation(
        away_goalie_in=code[0] == "1",
        away_skaters=int(code[1]),
        home_skaters=int(code[2]),
        home_goalie_in=code[3] == "1",
    )


def _situation(raw: JSON | None) -> Situation | None:
    """Live 'situation' block from play-by-play/landing (absent at even strength)."""
    if not raw:
        return None
    sit = decode_situation_code(raw.get("situationCode")) or Situation()
    sit.away_tags = list((raw.get("awayTeam") or {}).get("situationDescriptions") or [])
    sit.home_tags = list((raw.get("homeTeam") or {}).get("situationDescriptions") or [])
    sit.time_remaining = raw.get("timeRemaining")
    seconds = raw.get("secondsRemaining")
    if seconds is None and sit.time_remaining:
        try:
            minutes, secs = sit.time_remaining.split(":")
            seconds = int(minutes) * 60 + int(secs)
        except ValueError:
            seconds = None
    sit.seconds_remaining = int(seconds) if seconds is not None else None
    return sit


def _period(raw: JSON) -> tuple[int | None, str | None]:
    pd = raw.get("periodDescriptor") or {}
    return pd.get("number") or raw.get("period"), pd.get("periodType")


# --------------------------------------------------------------------------- score


def _score_goal(raw: JSON) -> Goal:
    pd = raw.get("periodDescriptor") or {}
    modifier = raw.get("goalModifier")
    return Goal(
        period=pd.get("number") or raw.get("period", 0),
        period_type=pd.get("periodType", "REG"),
        time_in_period=raw.get("timeInPeriod", "00:00"),
        team_abbrev=raw.get("teamAbbrev", "???"),
        scorer_id=raw.get("playerId"),
        scorer_name=_text(raw.get("name")),
        scorer_season_total=raw.get("goalsToDate"),
        assists=[_text(a.get("name")) for a in raw.get("assists") or []],
        strength=raw.get("strength", "ev"),
        modifier=None if modifier in (None, "none") else modifier,
        away_score=raw.get("awayScore", 0),
        home_score=raw.get("homeScore", 0),
    )


def parse_game(raw: JSON) -> Game:
    """Parse one game object from /score, /gamecenter/*/landing or play-by-play."""
    period, period_type = _period(raw)
    return Game(
        id=raw["id"],
        state=GameState.parse(raw.get("gameState")),
        start_time_utc=_dt(raw["startTimeUTC"]),
        away=_team_line(raw.get("awayTeam") or {}),
        home=_team_line(raw.get("homeTeam") or {}),
        venue=_text(raw.get("venue")) or None,
        period=period,
        period_type=period_type,
        clock=_clock(raw.get("clock")),
        situation=_situation(raw.get("situation")),
        goals=[_score_goal(g) for g in raw.get("goals") or []],
        last_period_type=(raw.get("gameOutcome") or {}).get("lastPeriodType"),
        broadcasts=_broadcasts(raw),
    )


def parse_score(data: JSON) -> list[Game]:
    """/score/now or /score/{date}: every game on the current NHL day."""
    return [parse_game(g) for g in data.get("games") or []]


# --------------------------------------------------------------------------- play-by-play


def _strength_for(team_is_away: bool, sit: Situation | None) -> str:
    if sit is None:
        return "ev"
    ours, theirs = (sit.away_strength, sit.home_strength) if team_is_away else (sit.home_strength, sit.away_strength)
    if ours > theirs:
        return "pp"
    if ours < theirs:
        return "sh"
    return "ev"


def parse_play_by_play(data: JSON) -> GameDetail:
    """/gamecenter/{id}/play-by-play: live state plus goals, penalties and plays."""
    game = parse_game(data)
    team_by_id = {t.id: t.abbrev for t in (game.away, game.home) if t.id is not None}
    names = {
        r.get("playerId"): f"{_text(r.get('firstName'))[:1]}. {_text(r.get('lastName'))}".strip()
        for r in data.get("rosterSpots") or []
    }

    plays: list[Play] = []
    goals: list[Goal] = []
    penalties: list[Penalty] = []
    for p in data.get("plays") or []:
        pd = p.get("periodDescriptor") or {}
        period, period_type = pd.get("number", 0), pd.get("periodType", "REG")
        play = Play(
            event_id=p["eventId"],
            type=p.get("typeDescKey", ""),
            period=period,
            period_type=period_type,
            time_in_period=p.get("timeInPeriod", "00:00"),
            sort_order=p.get("sortOrder", 0),
        )
        plays.append(play)
        d = p.get("details") or {}
        team = team_by_id.get(d.get("eventOwnerTeamId"), "???")

        if play.type == "goal":
            team_is_away = team == game.away.abbrev
            sit = decode_situation_code(p.get("situationCode"))
            opp_goalie_in = True if sit is None else (sit.home_goalie_in if team_is_away else sit.away_goalie_in)
            assists = [names.get(d.get(k), "") for k in ("assist1PlayerId", "assist2PlayerId") if d.get(k)]
            goals.append(
                Goal(
                    event_id=play.event_id,
                    period=period,
                    period_type=period_type,
                    time_in_period=play.time_in_period,
                    team_abbrev=team,
                    scorer_id=d.get("scoringPlayerId"),
                    scorer_name=names.get(d.get("scoringPlayerId"), "Unknown"),
                    scorer_season_total=d.get("scoringPlayerTotal"),
                    assists=[a for a in assists if a],
                    strength=_strength_for(team_is_away, sit),
                    modifier=None if opp_goalie_in else "empty-net",
                    away_score=d.get("awayScore", 0),
                    home_score=d.get("homeScore", 0),
                )
            )
        elif play.type == "penalty":
            player = d.get("committedByPlayerId") or d.get("servedByPlayerId")
            penalties.append(
                Penalty(
                    event_id=play.event_id,
                    period=period,
                    period_type=period_type,
                    time_in_period=play.time_in_period,
                    team_abbrev=team,
                    player_name=names.get(player),
                    description=str(d.get("descKey", "penalty")).replace("-", " "),
                    duration=int(d.get("duration", 2)),
                )
            )

    plays.sort(key=lambda p: p.sort_order)
    game.goals = goals
    return GameDetail(game=game, plays=plays, penalties=penalties)


# --------------------------------------------------------------------------- game extras


def parse_right_rail(data: JSON) -> list[TeamGameStat]:
    """/gamecenter/{id}/right-rail: team game stats (sog, faceoffs, pp, hits...)."""
    return [
        TeamGameStat(category=s["category"], away=s.get("awayValue"), home=s.get("homeValue"))
        for s in data.get("teamGameStats") or []
        if "category" in s
    ]


def parse_three_stars(data: JSON) -> list[ThreeStar]:
    """/gamecenter/{id}/landing: three stars (populated once the game is final)."""
    stars = []
    for s in (data.get("summary") or {}).get("threeStars") or []:
        stars.append(
            ThreeStar(
                star=s.get("star", 0),
                player_id=s.get("playerId"),
                name=_text(s.get("name")),
                team_abbrev=s.get("teamAbbrev", "???"),
                position=s.get("position"),
                goals=s.get("goals"),
                assists=s.get("assists"),
                points=s.get("points"),
            )
        )
    return stars


# --------------------------------------------------------------------------- standings / schedule


def parse_standings(data: JSON) -> list[TeamStanding]:
    out = []
    for s in data.get("standings") or []:
        streak = f"{s['streakCode']}{s.get('streakCount', '')}" if s.get("streakCode") else None
        l10 = (
            f"{s['l10Wins']}-{s.get('l10Losses', 0)}-{s.get('l10OtLosses', 0)}"
            if "l10Wins" in s
            else None
        )
        out.append(
            TeamStanding(
                abbrev=_text(s.get("teamAbbrev")),
                name=_text(s.get("teamCommonName") or s.get("teamName")),
                conference=s.get("conferenceName", ""),
                division=s.get("divisionName", ""),
                games_played=s.get("gamesPlayed", 0),
                wins=s.get("wins", 0),
                losses=s.get("losses", 0),
                ot_losses=s.get("otLosses", 0),
                points=s.get("points", 0),
                point_pctg=s.get("pointPctg", 0.0),
                goal_differential=s.get("goalDifferential", 0),
                streak=streak,
                l10=l10,
                division_rank=s.get("divisionSequence", 0),
                conference_rank=s.get("conferenceSequence", 0),
                league_rank=s.get("leagueSequence", 0),
                wildcard_rank=s.get("wildcardSequence") or None,
            )
        )
    return out


def parse_schedule(data: JSON) -> list[ScheduledGame]:
    """/club-schedule-season/{abbrev}/now: full season for one club."""
    out = []
    for g in data.get("games") or []:
        away, home = g.get("awayTeam") or {}, g.get("homeTeam") or {}
        out.append(
            ScheduledGame(
                id=g["id"],
                state=GameState.parse(g.get("gameState")),
                start_time_utc=_dt(g["startTimeUTC"]),
                away_abbrev=away.get("abbrev", "???"),
                home_abbrev=home.get("abbrev", "???"),
                away_score=away.get("score"),
                home_score=home.get("score"),
                venue=_text(g.get("venue")) or None,
                broadcasts=_broadcasts(g),
                last_period_type=(g.get("gameOutcome") or {}).get("lastPeriodType"),
            )
        )
    return out


# --------------------------------------------------------------------------- leaders / players


def parse_leaders(data: JSON) -> dict[str, list[LeaderEntry]]:
    """/skater-stats-leaders/current or /goalie-stats-leaders/current."""
    out: dict[str, list[LeaderEntry]] = {}
    for category, entries in data.items():
        if not isinstance(entries, list):
            continue
        out[category] = [
            LeaderEntry(
                player_id=e["id"],
                name=f"{_text(e.get('firstName'))[:1]}. {_text(e.get('lastName'))}",
                team_abbrev=e.get("teamAbbrev", "???"),
                position=e.get("position"),
                sweater_number=e.get("sweaterNumber"),
                value=float(e.get("value", 0)),
            )
            for e in entries
            if "id" in e
        ]
    return out


def parse_player(data: JSON) -> PlayerSummary:
    """/player/{id}/landing."""
    featured = data.get("featuredStats") or {}
    season_stats = ((featured.get("regularSeason") or {}).get("subSeason")) or {}
    last5 = data.get("last5Games") or []
    first, last = _text(data.get("firstName")), _text(data.get("lastName"))
    return PlayerSummary(
        id=data["playerId"],
        name=f"{first} {last}".strip(),
        first_name=first,
        last_name=last,
        sweater_number=data.get("sweaterNumber"),
        position=data.get("position", ""),
        team_abbrev=data.get("currentTeamAbbrev"),
        season=featured.get("season"),
        season_stats=dict(season_stats),
        last_game=dict(last5[0]) if last5 else None,
    )
