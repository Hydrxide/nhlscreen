"""Render every screen type from the test fixtures to LED-style PNGs.

    python tools/render_previews.py --out previews/

Use it to eyeball layout changes without running the server: open the PNGs
(or have Claude read them) and check spacing, colors and overflow.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nhlscreen import views  # noqa: E402
from nhlscreen.nhl import parsers  # noqa: E402
from nhlscreen.nhl.models import Clock, GameEvent, GameState, Situation  # noqa: E402
from nhlscreen.render import render  # noqa: E402

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)


def build_screens(fixtures: Path) -> dict[str, views.Screen]:
    load = lambda name: json.loads((fixtures / name).read_text())  # noqa: E731
    games = parsers.parse_score(load("score.json"))
    standings = parsers.parse_standings(load("standings.json"))
    schedule = parsers.parse_schedule(load("schedule_tor.json"))
    skaters = parsers.parse_leaders(load("skater_leaders.json"))
    goalies = parsers.parse_leaders(load("goalie_leaders.json"))
    detail = parsers.parse_play_by_play(load("pbp_final.json"))  # TOR @ UTA, 2026-10-13
    detail.team_stats = parsers.parse_right_rail(load("rightrail_final.json"))
    detail.three_stars = parsers.parse_three_stars(load("landing_final.json"))

    live = detail.game.model_copy(deep=True)
    live.state, live.period, live.period_type = GameState.LIVE, 2, "REG"
    live.away.score, live.home.score, live.away.sog, live.home.sog = 1, 3, 14, 19
    live.clock = Clock(time_remaining="05:16", seconds_remaining=316, running=True)
    live.situation = Situation(away_skaters=5, home_skaters=4, time_remaining="01:23")
    intermission = live.model_copy(deep=True)
    intermission.clock = Clock(time_remaining="00:00", seconds_remaining=0, running=False, in_intermission=True)
    intermission.situation = None

    def goal_event(goal):
        return GameEvent(kind="goal", game_id=live.id, key="g", team_abbrev=goal.team_abbrev, away_abbrev="TOR",
                         home_abbrev="UTA", away_score=goal.away_score, home_score=goal.home_score, goal=goal,
                         period=goal.period, occurred_at=NOW)

    final = GameEvent(kind="game_final", game_id=live.id, key="f", away_abbrev="TOR", home_abbrev="UTA",
                      away_score=4, home_score=5, occurred_at=NOW)
    page = [games[0], games[12].model_copy(deep=True), games[14].model_copy(deep=True), games[3]]
    page[1].state, page[1].away.score, page[1].home.score = GameState.FUTURE, None, None
    page[2].state, page[2].period = GameState.LIVE, 2
    page[2].clock = Clock(time_remaining="12:34", seconds_remaining=754, running=True)
    nxt = min((g for g in schedule if g.state is GameState.FUTURE), key=lambda g: g.start_time_utc)
    last = max((g for g in schedule if g.state.is_final), key=lambda g: g.start_time_utc)

    screens = {
        "live_game": views.live_game_screen(live, detail, TZ, 15, NOW, as_of=NOW),
        "live_intermission": views.live_game_screen(intermission, None, TZ, 15, NOW),
        "goal_alert": views.goal_alert_screen(goal_event(detail.game.goals[3]), 15),  # Matthews PPG
        "goal_alert_two_assists": views.goal_alert_screen(goal_event(detail.game.goals[-1]), 15),
        "final_alert": views.final_alert_screen(final, detail.game, detail, 20),
        "goal_overturned": views.overturned_screen(final, 15),
        "scoreboard": views.scoreboard_screens(page, 4, TZ, 8)[0],
        "standings_division": views.standings_screen(standings, "TOR", "division", None, 10),
        "standings_conference": views.standings_screen(standings, "TOR", "conference", None, 10),
        "leaders_points": views.leaders_screen("points", skaters["points"], 5, 8),
        "leaders_save_pct": views.leaders_screen("savePctg", goalies["savePctg"], 5, 8),
        "next_game": views.next_game_screen("TOR", nxt, last, standings, TZ, NOW, 10),
        "idle": views.idle_screen(NOW, TZ),
    }
    for f in sorted(fixtures.glob("player_*.json")):
        player = parsers.parse_player(json.loads(f.read_text()))
        screens[f"player_{player.last_name.lower()}"] = views.player_card_screen(player, 8)
    return screens


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default="tests/fixtures")
    ap.add_argument("--out", default="previews")
    ap.add_argument("--pitch", type=int, default=5, help="pixels per LED in the PNG")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    screens = build_screens(Path(args.fixtures))
    for name, screen in screens.items():
        (out / f"{name}.png").write_bytes(render(screen.model_dump(mode="json"), NOW).to_png(pitch=args.pitch))
    print(f"Wrote {len(screens)} previews to {out}/")


if __name__ == "__main__":
    main()
