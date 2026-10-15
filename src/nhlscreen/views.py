"""Layout-agnostic screen view models.

A renderer receives a Screen and draws it however its panel allows. ``data``
holds structured fields (scores, colors, clocks). ``spans`` is the screen as
short lines of text, each split into tagged pieces so a renderer can style them:

    label   white   titles, ranks ("1."), "NEXT:", "vs"/"@", stat names
    value   gold    the stats: scores, clocks, period names, times, records
    team    color   a team code; ``color`` follows the home/away rule on live
                    game info and is the team's own color elsewhere
    player  color   a player name, in the player's team's own color

Any span may carry an explicit ``color``, which wins over its kind's default
(e.g. the goal alert's "GOAL!" in the scoring team's color).

``lines`` is the same text as plain strings, for logs and the simplest panels.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from .nhl.models import (
    Game,
    GameDetail,
    GameEvent,
    GameState,
    LeaderEntry,
    PlayerSummary,
    ScheduledGame,
    TeamStanding,
)
from .services.clocktrack import ClockSegment
from .static.teams import get_team

ScreenType = Literal[
    "goal_alert",
    "goal_overturned",
    "final_alert",
    "live_game",
    "scoreboard",
    "standings",
    "wildcard",
    "next_game",
    "leaders",
    "player_card",
    "idle",
]

PRIORITY_ALERT = 100
PRIORITY_LIVE = 50
PRIORITY_ROTATION = 10


class Screen(BaseModel):
    id: str  # stable identity, e.g. "live_game:2026020102"
    type: ScreenType
    priority: int = PRIORITY_ROTATION
    dwell_seconds: float
    data: dict[str, Any] = Field(default_factory=dict)
    spans: list[list[dict[str, Any]]] = Field(default_factory=list)
    lines: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _lines_from_spans(self) -> "Screen":
        if self.spans and not self.lines:
            self.lines = ["".join(span["text"] for span in line) for line in self.spans]
        return self

    def content_key(self) -> tuple:
        """Identity + content, ignoring timestamps; used to detect real changes."""
        data = {k: v for k, v in self.data.items() if k != "clock"}
        clock = self.data.get("clock") or {}
        return (self.id, repr(data), clock.get("display"), clock.get("running"), clock.get("in_intermission"),
                clock.get("rate"))


# --------------------------------------------------------------------------- spans

Span = dict[str, Any]


def label(text: str) -> Span:
    return {"kind": "label", "text": text}


def value(text: Any, clock: bool = False) -> Span:
    span: Span = {"kind": "value", "text": str(text)}
    if clock:
        span["clock"] = True  # the live game clock: renderers count it down locally
    return span


def team_span(abbrev: str, side: Literal["home", "away"] | None = None) -> Span:
    """A team code. ``side`` (live game info only) applies home color / away white."""
    team = get_team(abbrev)
    color = team.game_color(side) if side else team.display
    return {"kind": "team", "text": abbrev, "team": team.abbrev, "color": color}


def player_span(name: str, abbrev: str | None) -> Span:
    """A player name in their team's own color (never the away-white rule)."""
    if not abbrev:
        return label(name)
    return {"kind": "player", "text": name, "team": abbrev, "color": get_team(abbrev).display}


def colored(text: str, color: tuple[int, int, int], kind: str = "label") -> Span:
    """A span with an explicit color, overriding its kind's default."""
    return {"kind": kind, "text": text, "color": color}


def line(*parts: Span | str) -> list[Span]:
    """Build a line; bare strings (spacing, punctuation) are labels."""
    return [label(p) if isinstance(p, str) else p for p in parts]


def score_line(
    away: str, away_score: int | None, home: str, home_score: int | None, live: bool = False
) -> list[Span]:
    """``live``: use home/away colors (live game info)."""
    return line(team_span(away, "away" if live else None), " ", value(away_score or 0), "  ",
                team_span(home, "home" if live else None), " ", value(home_score or 0))


# --------------------------------------------------------------------------- formatting

_CATEGORY_LABELS = {
    "points": "PTS",
    "goals": "G",
    "assists": "A",
    "plusMinus": "+/-",
    "wins": "W",
    "savePctg": "SV%",
    "goalsAgainstAverage": "GAA",
    "shutouts": "SO",
    "goalsPp": "PPG",
    "toi": "TOI",
}

_CATEGORY_TITLES = {
    "points": "Points",
    "goals": "Goals",
    "assists": "Assists",
    "plusMinus": "Plus/Minus",
    "wins": "Goalie Wins",  # plain "Wins" reads like team wins
    "savePctg": "Save %",
    "goalsAgainstAverage": "Goals Against Avg",
    "shutouts": "Shutouts",
}


def ordinal(n: int) -> str:
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n if n < 20 else n % 10, "th")
    return f"{n}{suffix}"


def period_label(period: int | None, period_type: str | None) -> str:
    if not period:
        return ""
    if period_type == "SO":
        return "SO"
    if period_type == "OT" or period > 3:
        ot_number = period - 3
        return "OT" if ot_number <= 1 else f"{ot_number}OT"
    return ordinal(period)


def local_time(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def local_day(dt: datetime, tz: ZoneInfo, now: datetime) -> str:
    local, today = dt.astimezone(tz).date(), now.astimezone(tz).date()
    delta = (local - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if 1 < delta < 7:
        return local.strftime("%a")
    return local.strftime("%b %-d")


def final_label(last_period_type: str | None) -> str:
    return "FINAL" if last_period_type in (None, "REG") else f"FINAL/{last_period_type}"


def game_status(game: Game, tz: ZoneInfo) -> str:
    if game.state == GameState.PREGAME:
        return "WARMUP"  # the NHL's "PRE" state: teams on the ice, puck not yet dropped
    if game.state.is_upcoming or game.state == GameState.UNKNOWN:
        return local_time(game.start_time_utc, tz)
    if game.state.is_final:
        return final_label(game.last_period_type or game.period_type)
    label = period_label(game.period, game.period_type)
    if game.clock and game.clock.in_intermission:
        return f"{label} INT"
    if game.clock:
        return f"{label} {game.clock.time_remaining}"
    return label


def status_spans(game: Game, tz: ZoneInfo, live_clock: bool = False) -> list[Span]:
    """game_status() as spans. All of it is game state, so all of it is a value:
    start time, "WARMUP", period name ("2nd", "OT", "1st INT"), clock, "FINAL/OT"."""
    if game.state == GameState.PREGAME:
        return [value("WARMUP")]
    if game.state.is_upcoming or game.state == GameState.UNKNOWN:
        return [value(local_time(game.start_time_utc, tz))]
    if game.state.is_final:
        return [value(final_label(game.last_period_type or game.period_type))]
    plabel = period_label(game.period, game.period_type)
    if game.clock and game.clock.in_intermission:
        return [value(f"{plabel} INT")]
    if game.clock:
        return [value(plabel), label(" "), value(game.clock.time_remaining, clock=live_clock)]
    return [value(plabel)]


def format_stat(category: str, amount: float) -> str:
    if category == "savePctg":
        return f"{amount:.3f}".lstrip("0")
    if category == "goalsAgainstAverage":
        return f"{amount:.2f}"
    if category == "toi":
        return f"{int(amount) // 60}:{int(amount) % 60:02d}"
    return f"{amount:+.0f}" if category == "plusMinus" else f"{amount:.0f}"


def team_view(
    abbrev: str,
    score: int | None = None,
    sog: int | None = None,
    side: Literal["home", "away"] | None = None,
) -> dict[str, Any]:
    """A team as renderers need it. ``side`` is set only for live game info
    (the live game screen, goal / no-goal alerts): ``colors.game`` then follows
    the jersey convention (home color, away white). ``colors.display`` is always
    the team's own color, used everywhere else, scoreboard pages included."""
    team = get_team(abbrev)
    colors: dict[str, Any] = team.colors()
    if side:
        colors["game"] = team.game_color(side)
    return {"abbrev": team.abbrev, "name": team.name, "side": side, "score": score, "sog": sog, "colors": colors}


# --------------------------------------------------------------------------- live game


def live_game_screen(
    game: Game,
    detail: GameDetail | None,
    tz: ZoneInfo,
    dwell: float,
    now: datetime,
    as_of: datetime | None = None,
    clock_rate: float = 1.0,
    segment: ClockSegment | None = None,
) -> Screen:
    """``segment``: the planned clock (services/clocktrack.py) for a tracked game. When
    given it replaces the latest reading's countdown, so the clock never jumps."""
    clock = game.clock
    if segment is not None and clock is not None and not clock.in_intermission:
        start = int(segment.seconds)
        clock = clock.model_copy(update={"seconds_remaining": start, "time_remaining": f"{start // 60:02d}:{start % 60:02d}",
                                         "running": segment.rate > 0})
        game = game.model_copy(update={"clock": clock})
        as_of, clock_rate = datetime.fromtimestamp(segment.start, timezone.utc), segment.rate
    sit = game.situation
    power_play = empty_net = None
    pp_side = en_side = None
    if sit:
        pp_side = sit.power_play_team
        power_play = getattr(game, pp_side).abbrev if pp_side else None
        if not sit.away_goalie_in:
            empty_net, en_side = game.away.abbrev, "away"
        elif not sit.home_goalie_in:
            empty_net, en_side = game.home.abbrev, "home"

    stats = {}
    if detail:
        stats = {s.category: {"away": s.away, "home": s.home} for s in detail.team_stats}
    last_goal = game.goals[-1] if game.goals else None
    status = game_status(game, tz)

    data = {
        "game_id": game.id,
        "state": game.state.value,
        "away": team_view(game.away.abbrev, game.away.score, game.away.sog, side="away"),
        "home": team_view(game.home.abbrev, game.home.score, game.home.sog, side="home"),
        "period": game.period,
        "period_label": period_label(game.period, game.period_type),
        "status": status,
        "clock": {
            "display": clock.time_remaining if clock else None,
            "seconds_remaining": clock.seconds_remaining if clock else None,
            "running": clock.running if clock else False,
            "in_intermission": clock.in_intermission if clock else False,
            # Renderers count down locally while running is true:
            #   shown = seconds_remaining - (now - as_of) * rate
            # as_of is when this clock reading was true; rate is 1 except in replays, or
            # the planned pace while the clock is steered to the next reading.
            "as_of": (as_of or now).isoformat(),
            "rate": clock_rate,
        },
        "situation": {
            "strength": sit.strength if sit else "5v5",
            "power_play": power_play,
            "empty_net": empty_net,
            "time_remaining": sit.time_remaining if sit else None,
        },
        "last_goal": last_goal.model_dump() if last_goal else None,
        "stats": stats,
    }
    line2 = status_spans(game, tz, live_clock=True)
    if power_play:
        line2 += line("  PP ", team_span(power_play, pp_side))
    if empty_net:
        line2 += line("  EN ", team_span(empty_net, en_side))
    spans = [
        score_line(game.away.abbrev, game.away.score, game.home.abbrev, game.home.score, live=True),
        line2,
        line("SOG ", value(f"{game.away.sog or 0}-{game.home.sog or 0}")),
    ]
    return Screen(id=f"live_game:{game.id}", type="live_game", priority=PRIORITY_LIVE,
                  dwell_seconds=dwell, data=data, spans=spans)


# --------------------------------------------------------------------------- alerts


def goal_alert_screen(event: GameEvent, dwell: float) -> Screen:
    goal = event.goal
    team = event.team_abbrev or "???"
    plabel = period_label(event.period, event.period_type)
    data = {
        "game_id": event.game_id,
        "team": team_view(team),  # the banner uses the team's own color, home or away
        "away": team_view(event.away_abbrev, event.away_score, side="away"),
        "home": team_view(event.home_abbrev, event.home_score, side="home"),
        "scorer": goal.scorer_name if goal else None,
        "scorer_season_total": goal.scorer_season_total if goal else None,
        "assists": goal.assists if goal else [],
        "strength": goal.strength if goal else None,
        "modifier": goal.modifier if goal else None,
        "time_in_period": goal.time_in_period if goal else None,
        "period_label": period_label(goal.period, goal.period_type) if goal else plabel,
    }
    # The whole "TOR GOAL!" banner is in the scoring team's own color, always.
    spans = [line(team_span(team), colored(" GOAL!", get_team(team).display))]
    if goal:
        scorer = line(player_span(goal.scorer_name, team))
        if goal.scorer_season_total:
            scorer += line(" (", value(goal.scorer_season_total), ")")
        tag = {"pp": " PPG", "sh": " SHG"}.get(goal.strength, "")
        if goal.modifier == "empty-net":
            tag += " EN"
        if tag:
            scorer += line(tag)
        spans.append(scorer)
        if goal.assists:
            assists = line("A: ")
            for i, name in enumerate(goal.assists):
                assists += line(", " if i else "", player_span(name, team))
            spans.append(assists)
    spans.append(score_line(event.away_abbrev, event.away_score, event.home_abbrev, event.home_score, live=True))
    return Screen(id=f"alert:{event.key}", type="goal_alert", priority=PRIORITY_ALERT,
                  dwell_seconds=dwell, data=data, spans=spans)


def overturned_screen(event: GameEvent, dwell: float) -> Screen:
    data = {
        "game_id": event.game_id,
        "away": team_view(event.away_abbrev, event.away_score, side="away"),
        "home": team_view(event.home_abbrev, event.home_score, side="home"),
    }
    spans = [line("NO GOAL"),
             score_line(event.away_abbrev, event.away_score, event.home_abbrev, event.home_score, live=True)]
    return Screen(id=f"alert:{event.key}", type="goal_overturned", priority=PRIORITY_ALERT,
                  dwell_seconds=dwell, data=data, spans=spans)


def final_alert_screen(event: GameEvent, game: Game | None, detail: GameDetail | None, dwell: float) -> Screen:
    final = final_label(game.last_period_type or game.period_type) if game else "FINAL"
    stars = [s.model_dump() for s in detail.three_stars] if detail else []
    winner = None
    if event.away_score is not None and event.home_score is not None and event.away_score != event.home_score:
        winner = event.away_abbrev if event.away_score > event.home_score else event.home_abbrev
    data = {
        "game_id": event.game_id,
        "label": final,
        "winner": winner,
        "away": team_view(event.away_abbrev, event.away_score),
        "home": team_view(event.home_abbrev, event.home_score),
        "three_stars": stars,
    }
    spans = [line(value(final)), score_line(event.away_abbrev, event.away_score, event.home_abbrev, event.home_score)]
    if stars:
        spans.append(line("1st star: ", player_span(stars[0]["name"], stars[0]["team_abbrev"])))
    return Screen(id=f"alert:{event.key}", type="final_alert", priority=PRIORITY_ALERT,
                  dwell_seconds=dwell, data=data, spans=spans)


# --------------------------------------------------------------------------- rotation screens


def scoreboard_screens(games: list[Game], per_page: int, tz: ZoneInfo, dwell: float) -> list[Screen]:
    games = sorted(games, key=lambda g: (g.start_time_utc, g.id))
    pages = [games[i : i + per_page] for i in range(0, len(games), per_page)]
    screens = []
    for n, page in enumerate(pages):
        rows = [
            {
                "game_id": g.id,
                "state": g.state.value,
                "away": team_view(g.away.abbrev, g.away.score),
                "home": team_view(g.home.abbrev, g.home.score),
                "status": game_status(g, tz),
            }
            for g in page
        ]
        def pad(score: int | None) -> str:
            return f"{'' if score is None else score:>2}"

        spans = [
            line(team_span(g.away.abbrev), " ", value(pad(g.away.score)), " @ ",
                 team_span(g.home.abbrev), " ", value(pad(g.home.score)), "  ", *status_spans(g, tz))
            for g in page
        ]
        screens.append(
            Screen(id=f"scoreboard:{n}", type="scoreboard", dwell_seconds=dwell,
                   data={"page": n + 1, "pages": len(pages), "games": rows}, spans=spans)
        )
    return screens


STANDINGS_PAGE_ROWS = 8  # rows that fit under the title + column headers at 128x64


def _standing_row(s: TeamStanding, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "team": team_view(s.abbrev),
        "gp": s.games_played,
        "record": s.record,
        "points": s.points,
        "streak": s.streak,
        "l10": s.l10,
    }


def _standing_spans(r: dict[str, Any]) -> list[Span]:
    return line(f"{r['rank']:>2}. ", team_span(r["team"]["abbrev"]), " ", value(f"{r['points']:>3}"), "  ",
                value(r["record"]))


def standings_screen(
    standings: list[TeamStanding],
    team: str,
    scope: str,
    limit: int | None,
    dwell: float,
    followed: list[str] | None = None,
) -> Screen | None:
    """One table around ``team``: its division (the usual case), conference or the league."""
    mine = next((s for s in standings if s.abbrev == team), None)
    if mine is None:
        return None
    if scope == "division":
        rows, title, rank = [s for s in standings if s.division == mine.division], mine.division, "division_rank"
    elif scope == "conference":
        rows, title, rank = [s for s in standings if s.conference == mine.conference], mine.conference, "conference_rank"
    else:
        rows, title, rank = list(standings), "League", "league_rank"
    rows.sort(key=lambda s: getattr(s, rank))
    if limit:
        rows = rows[:limit]
    table = [_standing_row(s, getattr(s, rank)) for s in rows]
    data = {"title": title, "scope": scope, "highlight": followed or [team], "rows": table}
    spans = [line(title.upper())] + [_standing_spans(r) for r in table]
    return Screen(id=f"standings:{scope}:{team}", type="standings", dwell_seconds=dwell, data=data, spans=spans)


def standings_pages(
    standings: list[TeamStanding],
    scope: Literal["league", "conference"],
    followed: list[str],
    dwell: float,
    conference: str | None = None,
) -> list[Screen]:
    """A whole league or conference table, split into screens of 8 rows."""
    if scope == "league":
        rows, title, rank, key = sorted(standings, key=lambda s: s.league_rank), "NHL Standings", "league_rank", "league"
    else:
        rows = sorted((s for s in standings if s.conference == conference), key=lambda s: s.conference_rank)
        title, rank, key = conference or "", "conference_rank", f"conference:{conference}"
    chunks = [rows[i : i + STANDINGS_PAGE_ROWS] for i in range(0, len(rows), STANDINGS_PAGE_ROWS)]
    screens = []
    for n, chunk in enumerate(chunks):
        table = [_standing_row(s, getattr(s, rank)) for s in chunk]
        data = {"title": title, "scope": scope, "page": n + 1, "pages": len(chunks), "highlight": followed,
                "rows": table}
        spans = [line(f"{title.upper()} {n + 1}/{len(chunks)}")] + [_standing_spans(r) for r in table]
        screens.append(Screen(id=f"standings:{key}:{n}", type="standings", dwell_seconds=dwell, data=data, spans=spans))
    return screens


WILDCARD_SPOTS = 2  # playoff spots per conference beyond each division's top 3


def wildcard_screen(
    standings: list[TeamStanding], conference: str, followed: list[str], dwell: float
) -> Screen | None:
    """The NHL's playoff format for one conference: each division's top 3, then the wild card race."""
    teams = [s for s in standings if s.conference == conference]
    if not teams:
        return None
    divisions = []
    for name in sorted({s.division for s in teams}):
        top = sorted((s for s in teams if s.division == name and not s.wildcard_rank), key=lambda s: s.division_rank)
        divisions.append({"name": name, "rows": [_standing_row(s, s.division_rank) for s in top[:3]]})
    race = sorted((s for s in teams if s.wildcard_rank), key=lambda s: s.wildcard_rank)
    wildcard = [_standing_row(s, s.wildcard_rank) for s in race]
    data = {
        "conference": conference,
        "title": f"{conference} Wild Card",
        "divisions": divisions,
        "wildcard": wildcard,
        "spots": WILDCARD_SPOTS,
        "highlight": followed,
    }
    spans = [line(data["title"].upper())]
    for block in divisions:
        spans += [line(block["name"].upper())] + [_standing_spans(r) for r in block["rows"]]
    spans += [line("WILD CARD")] + [_standing_spans(r) for r in wildcard[:WILDCARD_SPOTS]]
    spans += [line("----")] + [_standing_spans(r) for r in wildcard[WILDCARD_SPOTS:]]
    return Screen(id=f"wildcard:{conference}", type="wildcard", dwell_seconds=dwell, data=data, spans=spans)


def next_game_screen(
    team: str,
    game: ScheduledGame | None,
    last: ScheduledGame | None,
    standings: list[TeamStanding],
    tz: ZoneInfo,
    now: datetime,
    dwell: float,
) -> Screen | None:
    if game is None and last is None:
        return None
    records = {s.abbrev: s.record for s in standings}
    data: dict[str, Any] = {"team": team_view(team), "record": records.get(team)}
    spans: list[list[Span]] = []
    if game:
        home = game.home_abbrev == team
        opponent = game.away_abbrev if home else game.home_abbrev
        data["next"] = {
            "game_id": game.id,
            "opponent": team_view(opponent),
            "opponent_record": records.get(opponent),
            "home": home,
            "start_time_utc": game.start_time_utc.isoformat(),
            "day": local_day(game.start_time_utc, tz, now),
            "time": local_time(game.start_time_utc, tz),
            "seconds_until": max(0, int((game.start_time_utc - now).total_seconds())),
            "broadcasts": game.broadcasts,
            "venue": game.venue,
        }
        spans += [
            line("NEXT: ", "vs " if home else "@ ", team_span(opponent)),
            line(data["next"]["day"] + " ", value(data["next"]["time"])),
        ]
    if last:
        home = last.home_abbrev == team
        ours, theirs = (last.home_score, last.away_score) if home else (last.away_score, last.home_score)
        opponent = last.away_abbrev if home else last.home_abbrev
        result = "W" if (ours or 0) > (theirs or 0) else ("OTL" if last.last_period_type in ("OT", "SO") else "L")
        data["last"] = {
            "game_id": last.id,
            "opponent": team_view(opponent),
            "home": home,
            "result": result,
            "score": f"{ours}-{theirs}",
            "label": final_label(last.last_period_type),
        }
        spans.append(line("LAST: ", value(f"{result} {ours}-{theirs}"), " vs " if home else " @ ", team_span(opponent)))
    return Screen(id=f"next_game:{team}", type="next_game", dwell_seconds=dwell, data=data, spans=spans)


def leaders_screen(category: str, entries: list[LeaderEntry], limit: int, dwell: float) -> Screen | None:
    if not entries:
        return None
    short = _CATEGORY_LABELS.get(category, category)
    title = _CATEGORY_TITLES.get(category, category)
    rows = [
        {
            "rank": i + 1,
            "player_id": e.player_id,
            "name": e.name,
            "team": team_view(e.team_abbrev),
            "value": e.value,
            "display": format_stat(category, e.value),
        }
        for i, e in enumerate(entries[:limit])
    ]
    spans = [line(f"{title.upper()} LEADERS")] + [
        line(f"{r['rank']}. ", player_span(r["name"], r["team"]["abbrev"]), " ", team_span(r["team"]["abbrev"]), " ",
             value(r["display"]))
        for r in rows
    ]
    return Screen(id=f"leaders:{category}", type="leaders", dwell_seconds=dwell,
                  data={"category": category, "label": short, "title": title, "rows": rows}, spans=spans)


_SKATER_STATS = [("gamesPlayed", "GP"), ("goals", "G"), ("assists", "A"), ("points", "P"), ("plusMinus", "+/-")]
_GOALIE_STATS = [("gamesPlayed", "GP"), ("wins", "W"), ("goalsAgainstAvg", "GAA"), ("savePctg", "SV%"), ("shutouts", "SO")]


def player_card_screen(player: PlayerSummary, dwell: float) -> Screen:
    spec = _GOALIE_STATS if player.is_goalie else _SKATER_STATS
    stats = []
    for key, stat_label in spec:
        raw = player.season_stats.get(key)
        if raw is None:
            continue
        fmt_key = {"goalsAgainstAvg": "goalsAgainstAverage"}.get(key, key)
        stats.append({"key": key, "label": stat_label, "value": raw, "display": format_stat(fmt_key, float(raw))})
    data = {
        "player_id": player.id,
        "name": player.name,
        "last_name": player.last_name,
        "number": player.sweater_number,
        "position": player.position,
        "team": team_view(player.team_abbrev) if player.team_abbrev else None,
        "season": player.season,
        "stats": stats,
        "last_game": player.last_game,
    }
    number = f"#{player.sweater_number} " if player.sweater_number else ""
    stat_line: list[Span] = []
    for i, st in enumerate(stats):
        stat_line += line("  " if i else "", st["label"] + " ", value(st["display"]))
    spans = [line(number, player_span(player.name, player.team_abbrev)), stat_line]
    return Screen(id=f"player_card:{player.id}", type="player_card", dwell_seconds=dwell, data=data, spans=spans)


def idle_screen(now: datetime, tz: ZoneInfo) -> Screen:
    # Short dwell so real content replaces it as soon as data arrives.
    return Screen(id="idle", type="idle", dwell_seconds=3,
                  data={"time": local_time(now, tz)}, spans=[line("NHL"), line(value(local_time(now, tz)))])
