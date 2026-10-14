"""Draw a Screen (as JSON, the same shape /events sends) onto an LED canvas.

Layouts target a 128x64 matrix. Busy screens (live game, scoreboard,
standings, leaders, player card) have hand-placed layouts; the rest use
``layout_lines``, which centers the screen's span lines and picks the largest
font that fits. Colors follow the span rules: labels white, values gold,
teams and players in their resolved colors.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from ..static.teams import get_team
from .canvas import GOLD, WHITE, Canvas, dim
from .fonts import REGULAR, SMALL, Font

log = logging.getLogger(__name__)

ScreenDict = dict[str, Any]
HIGHLIGHT = (48, 48, 48)  # background bar for the followed team's row
FontChoice = tuple[Font, int]  # (font, scale)

BIG: FontChoice = (REGULAR, 2)
REG: FontChoice = (REGULAR, 1)
SML: FontChoice = (SMALL, 1)


def _rgb(value: Any, default=WHITE):
    return tuple(value) if value else default


def team_color(team: dict[str, Any] | None) -> tuple[int, int, int]:
    """colors.game on live game info (home/away rule), else the team's own color."""
    colors = (team or {}).get("colors") or {}
    return _rgb(colors.get("game") or colors.get("display"))


def own_color(team: dict[str, Any] | None) -> tuple[int, int, int]:
    return _rgb(((team or {}).get("colors") or {}).get("display"))


def clock_text(screen: ScreenDict, now: datetime) -> str | None:
    """The live clock counted down locally (see views.live_game_screen)."""
    clock = (screen.get("data") or {}).get("clock") or {}
    if not clock.get("running") or clock.get("seconds_remaining") is None or not clock.get("as_of"):
        return None
    elapsed = (now - datetime.fromisoformat(clock["as_of"])).total_seconds() * clock.get("rate", 1)
    left = max(0, int(clock["seconds_remaining"] - elapsed))
    return f"{left // 60:02d}:{left % 60:02d}"


def _span_lines(screen: ScreenDict, now: datetime) -> list[list[dict[str, Any]]]:
    lines = screen.get("spans") or [[{"kind": "label", "text": t}] for t in screen.get("lines") or []]
    clock = clock_text(screen, now)
    if clock:
        lines = [[{**s, "text": clock} if s.get("clock") else s for s in line] for line in lines]
    return lines


def _measure_spans(spans: list[dict[str, Any]], font: Font, scale: int = 1) -> int:
    return font.measure("".join(s["text"] for s in spans), scale)


def _spans_center(cv: Canvas, y: int, spans: list[dict[str, Any]], font: Font, scale: int = 1) -> None:
    x = max(0, (cv.width - _measure_spans(spans, font, scale)) // 2)
    cv.spans(x, y, spans, font, scale)


def fit_name(name: str, font: Font, max_width: int) -> str:
    """'N. MacKinnon' -> 'MacKinnon' -> 'MacKinn' until it fits."""
    if font.measure(name) <= max_width:
        return name
    if ". " in name:
        name = name.split(". ", 1)[1]
    while name and font.measure(name) > max_width:
        name = name[:-1]
    return name


# --------------------------------------------------------------------------- generic


def layout_lines(
    cv: Canvas, screen: ScreenDict, now: datetime, big_first: bool = False, rule_after: int | None = None
) -> None:
    """Center each span line, using the largest font that fits its width,
    shrinking the tallest lines until everything fits vertically.
    ``rule_after``: draw a white divider line after that line index."""
    lines = _span_lines(screen, now)
    if not lines:
        return
    options: list[list[FontChoice]] = []
    for i, spans in enumerate(lines):
        candidates = ([BIG] if big_first and i == 0 else []) + [REG, SML]
        fitting = [c for c in candidates if _measure_spans(spans, *c) <= cv.width - 2] or [SML]
        options.append(fitting)
    chosen = [0] * len(lines)

    def heights() -> list[int]:
        return [options[i][c][0].height * options[i][c][1] for i, c in enumerate(chosen)]

    rule = 1 if rule_after is not None and rule_after < len(lines) - 1 else 0
    while sum(heights()) + rule * 2 + (len(lines) - 1) > cv.height:
        shrinkable = [i for i in range(len(lines)) if chosen[i] + 1 < len(options[i])]
        if not shrinkable:
            break
        tallest = max(shrinkable, key=lambda i: (heights()[i], i))
        chosen[tallest] += 1

    used = sum(heights()) + rule
    gaps = len(lines) - 1 + rule
    gap = max(1, min(6, (cv.height - used) // (gaps + 2)))
    y = max(0, (cv.height - used - gap * gaps) // 2)
    for i, spans in enumerate(lines):
        font, scale = options[i][chosen[i]]
        _spans_center(cv, y, spans, font, scale)
        y += font.height * scale + gap
        if rule and i == rule_after:
            cv.hline(2, cv.width - 3, y, WHITE)
            y += 1 + gap


# --------------------------------------------------------------------------- live game


def _pct(v: Any) -> str:
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(v)


_LIVE_STATS = (("powerPlay", "PP", str), ("faceoffWinningPctg", "FO", _pct), ("hits", "HITS", str))


def layout_live_game(cv: Canvas, screen: ScreenDict, now: datetime) -> None:
    """
    y  1-14  TOR (2x)          2ND          UTA (2x)
    y 17-37   1  (3x)        18:38 (2x)       3  (3x)
    y 36-40                 PP TOR 01:23
    y 42     ------------- divider --------------
    y 45-51  14               SOG               19
    y 53-63  2/5               PP              0/4     (small font)
             52%               FO              48%
    """
    d = screen["data"]
    w, mid = cv.width, cv.width // 2
    away, home = d["away"], d["home"]
    away_c, home_c = team_color(away), team_color(home)

    # Teams (2x) and scores (3x) at the sides.
    cv.text(1, 1, away["abbrev"], REGULAR, away_c, 2)
    cv.text_right(w - 2, 1, home["abbrev"], REGULAR, home_c, 2)
    cv.text(1, 17, str(away.get("score") or 0), REGULAR, GOLD, 3)
    cv.text_right(w - 2, 17, str(home.get("score") or 0), REGULAR, GOLD, 3)

    # Period, then the clock between the scores: 2x unless two-digit scores
    # would crowd it, then 1x.
    clock = d.get("clock") or {}
    cv.text_center(mid, 4, d.get("period_label") or "", REGULAR, GOLD)
    shown = "INT" if clock.get("in_intermission") else (clock_text(screen, now) or clock.get("display") or "")
    away_right = 1 + REGULAR.measure(str(away.get("score") or 0), 3)
    home_left = w - 2 - REGULAR.measure(str(home.get("score") or 0), 3)
    half = REGULAR.measure(shown, 2) // 2
    if min(mid - half - away_right, home_left - (mid + half)) >= 7:
        cv.text_center(mid, 20, shown, REGULAR, GOLD, 2)
    else:
        cv.text_center(mid, 24, shown, REGULAR, GOLD)

    # Power play / empty net under the clock.
    sit = d.get("situation") or {}
    for flag, word in (("power_play", "PP "), ("empty_net", "EN ")):
        abbrev = sit.get(flag)
        if not abbrev:
            continue
        team = away if away["abbrev"] == abbrev else home
        spans = [{"kind": "label", "text": word}, {"kind": "team", "text": abbrev, "color": team_color(team)}]
        if flag == "power_play" and sit.get("time_remaining"):
            spans.append({"kind": "value", "text": " " + sit["time_remaining"]})
        _spans_center(cv, 36, spans, SMALL)
        break

    # Divider in each side's color.
    cv.hline(0, mid - 1, 42, dim(away_c, 0.45))
    cv.hline(mid, w - 1, 42, dim(home_c, 0.45))

    # Shots, then up to two team stats in the small font.
    cv.text(1, 45, str(away.get("sog") or 0), REGULAR, GOLD)
    cv.text_right(w - 2, 45, str(home.get("sog") or 0), REGULAR, GOLD)
    cv.text_center(mid, 45, "SOG", REGULAR, WHITE)
    stats = d.get("stats") or {}
    y = 53
    for key, name, fmt in _LIVE_STATS:
        if key not in stats or y + SMALL.height > cv.height:
            continue
        cv.text(1, y, fmt(stats[key]["away"]), SMALL, GOLD)
        cv.text_right(w - 2, y, fmt(stats[key]["home"]), SMALL, GOLD)
        cv.text_center(mid, y, name, SMALL, WHITE)
        y += SMALL.line_height


# --------------------------------------------------------------------------- tables


# Scoreboard columns (x): away 1 | away score ..31 | "@" 35 | home 43 | home score ..74 | status ..126
# (3px either side of "@"; the widest status, "2ND 12:34", still starts at x=80)
_SB_AT, _SB_HOME, _SB_HOME_SCORE, _SB_STATUS_MIN = 35, 43, 74, 79


def layout_scoreboard(cv: Canvas, screen: ScreenDict, now: datetime) -> None:
    """One game per row: ``NJD  5 @ DET  4   FINAL/OT`` (away @ home)."""
    games = screen["data"].get("games") or []
    pitch = cv.height // max(4, len(games))
    # "@" between the teams; "AT" only if a narrower panel can't fit the symbol
    at = "@" if REGULAR.measure("@") <= _SB_HOME - _SB_AT - 2 else "AT"
    for i, g in enumerate(games):
        y = i * pitch + (pitch - REGULAR.height) // 2
        away, home = g["away"], g["home"]
        cv.text(1, y, away["abbrev"], REGULAR, team_color(away))
        if away.get("score") is not None:
            cv.text_right(31, y, str(away["score"]), REGULAR, GOLD)
        cv.text(_SB_AT, y, at, REGULAR, WHITE)
        cv.text(_SB_HOME, y, home["abbrev"], REGULAR, team_color(home))
        if home.get("score") is not None:
            cv.text_right(_SB_HOME_SCORE, y, str(home["score"]), REGULAR, GOLD)
        status = g.get("status") or ""
        font = REGULAR if REGULAR.measure(status) <= cv.width - 2 - _SB_STATUS_MIN else SMALL
        cv.text_right(cv.width - 2, y + (1 if font is SMALL else 0), status, font, GOLD)


def _highlights(d: dict[str, Any]) -> set[str]:
    hl = d.get("highlight") or []
    return {hl} if isinstance(hl, str) else set(hl)


def layout_standings(cv: Canvas, screen: ScreenDict, now: datetime) -> None:
    d = screen["data"]
    rows = d.get("rows") or []
    followed = _highlights(d)
    cv.text(1, 0, d.get("title") or "", REGULAR, WHITE)
    if (d.get("pages") or 1) > 1:
        cv.text_right(cv.width - 2, 1, f"{d['page']}/{d['pages']}", SMALL, WHITE)
    # columns (small font): rank | team | GP | record | PTS | streak
    for text, x, right in (("GP", 46, True), ("REC", 52, False), ("PTS", 96, True), ("STK", 104, False)):
        (cv.text_right if right else cv.text)(x, 10, text, SMALL, WHITE)

    top, pitch = 17, SMALL.line_height
    max_rows = (cv.height - top + 1) // pitch
    if len(rows) > max_rows:  # keep a followed team in view
        idx = next((i for i, r in enumerate(rows) if r["team"]["abbrev"] in followed), 0)
        start = min(max(0, idx - max_rows // 2), len(rows) - max_rows)
        rows = rows[start : start + max_rows]
    for i, r in enumerate(rows):
        y = top + i * pitch
        team = r["team"]
        if team["abbrev"] in followed:
            # Neutral bar: a team-colored one hides that team's own code.
            cv.fill(0, y - 1, cv.width, pitch + 1, HIGHLIGHT)
        cv.text_right(8, y, f"{r['rank']}.", SMALL, WHITE)
        cv.text(12, y, team["abbrev"], SMALL, own_color(team))
        cv.text_right(46, y, str(r.get("gp", "")), SMALL, GOLD)
        cv.text(52, y, r.get("record") or "", SMALL, GOLD)
        cv.text_right(96, y, str(r.get("points", "")), SMALL, GOLD)
        cv.text(104, y, r.get("streak") or "", SMALL, GOLD)


def layout_wildcard(cv: Canvas, screen: ScreenDict, now: datetime) -> None:
    """
    EASTERN WILD CARD
    ATLANTIC         | WILD CARD
     1. BOS  10      |  1. FLA   8
     2. OTT  10      |  2. MTL   7
     3. TBL   8      | ----------- (playoff cut)
    METROPOLITAN     |  3. TOR   7
     1. CAR  13      |  4. BUF   7
     ...             |  ...
    """
    d = screen["data"]
    followed = _highlights(d)
    pitch = SMALL.line_height
    cv.text(1, 0, d.get("title") or "", REGULAR, WHITE)
    mid = cv.width // 2
    cv.fill(mid, 9, 1, cv.height - 9, (60, 60, 60))

    def row(x0: int, x1: int, y: int, r: dict[str, Any]) -> None:
        team = r["team"]
        if team["abbrev"] in followed:
            cv.fill(x0, y - 1, x1 - x0 + 1, pitch + 1, HIGHLIGHT)
        cv.text_right(x0 + 9, y, f"{r['rank']}.", SMALL, WHITE)
        cv.text(x0 + 13, y, team["abbrev"], SMALL, own_color(team))
        cv.text_right(x1 - 1, y, str(r.get("points", "")), SMALL, GOLD)

    # Left column: each division's top 3.
    left, left_end, y = 0, mid - 2, 10
    for block in d.get("divisions") or []:
        cv.text(left + 1, y, block["name"], SMALL, WHITE)
        y += pitch
        for r in block["rows"][:3]:
            row(left, left_end, y, r)
            y += pitch

    # Right column: wild card spots, the playoff cut line, then the chasers.
    right, right_end, y = mid + 2, cv.width - 1, 10
    cv.text(right + 1, y, "WILD CARD", SMALL, WHITE)
    y += pitch
    race, spots = d.get("wildcard") or [], d.get("spots", 2)
    for r in race[:spots]:
        row(right, right_end, y, r)
        y += pitch
    cv.hline(right, right_end, y, dim(WHITE, 0.6))  # playoff cut line
    y += 3  # keep a gap so it doesn't merge with a highlight bar below
    chasers = race[spots:]
    room = (cv.height - y) // pitch
    shown = chasers[:room]
    behind = next((r for r in chasers[room:] if r["team"]["abbrev"] in followed), None)
    if behind and room:  # a followed team further back takes the last row
        shown = chasers[: room - 1] + [behind]
    for r in shown:
        row(right, right_end, y, r)
        y += pitch


def layout_leaders(cv: Canvas, screen: ScreenDict, now: datetime) -> None:
    d = screen["data"]
    rows = d.get("rows") or []
    title = f"{d.get('title', '')} LEADERS"
    if REGULAR.measure(title) > cv.width - 2:
        title = f"{d.get('label', '')} LEADERS"
    cv.text(1, 0, title, REGULAR, WHITE)
    if not rows:
        return
    top, pitch = 11, 10
    value_w = max(REGULAR.measure(r["display"]) for r in rows)
    team_right = cv.width - 2 - value_w - 4
    for i, r in enumerate(rows[: (cv.height - top) // pitch + 1]):
        y = top + i * pitch
        if y + REGULAR.height > cv.height:
            break
        color = own_color(r["team"])
        cv.text_right(10, y, f"{r['rank']}.", REGULAR, WHITE)
        team_left = cv.text_right(team_right, y, r["team"]["abbrev"], REGULAR, color)
        cv.text(14, y, fit_name(r["name"], REGULAR, team_left - 4 - 14), REGULAR, color)
        cv.text_right(cv.width - 2, y, r["display"], REGULAR, GOLD)


_POSITIONS = {"C": "CENTER", "L": "LEFT WING", "R": "RIGHT WING", "D": "DEFENSE", "G": "GOALIE"}


def layout_player_card(cv: Canvas, screen: ScreenDict, now: datetime) -> None:
    d = screen["data"]
    team = d.get("team")
    color = own_color(team) if team else WHITE
    number = f"#{d['number']} " if d.get("number") else ""

    # Name: full, then last name only, then the small font.
    for font, name in ((REGULAR, d["name"]), (REGULAR, d.get("last_name") or d["name"]), (SMALL, d["name"])):
        if font.measure(number + name) <= cv.width - 2:
            break
    x = max(0, (cv.width - font.measure(number + name)) // 2)
    x = cv.text(x, 2, number, font, WHITE)
    cv.text(x, 2, name, font, color)

    sub = []
    if team:
        sub.append({"kind": "player", "text": team["name"], "color": color})
    if d.get("position"):
        sub.append({"kind": "label", "text": ("  " if sub else "") + _POSITIONS.get(d["position"], d["position"])})
    if sub:
        _spans_center(cv, 12, sub, SMALL)

    # Stat labels (small) over values (regular). Every value uses the same size, so
    # decimals like GAA 2.97 / SV% .907 match the whole numbers beside them.
    stats = d.get("stats") or []
    if stats:
        col = cv.width // len(stats)
        for i, st in enumerate(stats):
            cx = i * col + col // 2
            cv.text_center(cx, 24, st["label"], SMALL, WHITE)
            cv.text_center(cx, 31, st["display"], REGULAR, GOLD)

    spans = last_game_spans(d.get("last_game") or {}, max_width=cv.width - 2)
    if spans:  # "LAST GAME" + the line, both regular size
        _spans_center(cv, 44, [{"kind": "label", "text": "LAST GAME"}], REGULAR)
        _spans_center(cv, 54, spans, REGULAR)


_DECISIONS = {"W": "W", "L": "L", "O": "OTL"}


def last_game_spans(last: dict[str, Any], max_width: int = 126) -> list[dict[str, Any]] | None:
    """A player's last game as one line of spans.

    Skater:  1G 0A  @ UTA            (numbers gold, "G"/"A" white)
    Goalie:  W  30SV  .909  @ COL    (save % is dropped if the line is too wide)
    """
    if not last:
        return None
    opponent = last.get("opponentAbbrev", "")
    tail = [
        {"kind": "label", "text": "  @ " if last.get("homeRoadFlag") == "R" else "  VS "},
        {"kind": "team", "text": opponent, "color": get_team(opponent).display},
    ]
    if "goals" in last:  # skater
        return [
            {"kind": "value", "text": str(last.get("goals", 0))}, {"kind": "label", "text": "G "},
            {"kind": "value", "text": str(last.get("assists", 0))}, {"kind": "label", "text": "A"},
            *tail,
        ]
    if "shotsAgainst" not in last:
        return None
    head: list[dict[str, Any]] = []
    if last.get("decision") in _DECISIONS:
        head += [{"kind": "value", "text": _DECISIONS[last["decision"]]}, {"kind": "label", "text": "  "}]
    saves = last["shotsAgainst"] - last.get("goalsAgainst", 0)
    head += [{"kind": "value", "text": str(saves)}, {"kind": "label", "text": "SV"}]
    pct = last.get("savePctg")
    with_pct = head + ([{"kind": "label", "text": "  "}, {"kind": "value", "text": f"{pct:.3f}".lstrip("0")}]
                       if pct is not None else []) + tail
    return with_pct if _measure_spans(with_pct, REGULAR) <= max_width else head + tail


# --------------------------------------------------------------------------- dispatch

LAYOUTS: dict[str, Callable[[Canvas, ScreenDict, datetime], None]] = {
    "live_game": layout_live_game,
    "scoreboard": layout_scoreboard,
    "standings": layout_standings,
    "wildcard": layout_wildcard,
    "leaders": layout_leaders,
    "player_card": layout_player_card,
    # "TOR GOAL!" banner, a white divider, then scorer / assists / score.
    "goal_alert": lambda cv, s, now: layout_lines(cv, s, now, big_first=True, rule_after=0),
    "goal_overturned": lambda cv, s, now: layout_lines(cv, s, now, big_first=True),
    "final_alert": lambda cv, s, now: layout_lines(cv, s, now, big_first=True),
    "idle": lambda cv, s, now: layout_lines(cv, s, now, big_first=True),
}


def render(screen: ScreenDict, now: datetime | None = None, width: int = 128, height: int = 64) -> Canvas:
    """Draw one screen. Falls back to the generic layout if a layout fails."""
    now = now or datetime.now(timezone.utc)
    cv = Canvas(width, height)
    layout = LAYOUTS.get(screen.get("type", ""), layout_lines)
    try:
        layout(cv, screen, now)
    except Exception:
        log.exception("layout for %s failed; using generic layout", screen.get("type"))
        cv = Canvas(width, height)
        layout_lines(cv, screen, now)
    return cv
