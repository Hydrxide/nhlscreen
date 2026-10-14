"""Build a replay from a *finished* game so you can test without a live one.

    python tools/synth_replay.py --fixtures tests/fixtures --out replays/synthetic
    python -m nhlscreen --replay replays/synthetic --speed 10

Frames are spaced evenly in *game time* (``--step`` game seconds each, which is
also the replay interval), so the clock flows continuously: at --speed 1 the
game plays in real time, at --speed 10 a 20-minute period takes 2 minutes.
Each frame rewrites score, clock, period, state and on-ice situation in both
the play-by-play and scoreboard documents, as if the game were live.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nhlscreen.replay import encode_path  # noqa: E402

STATIC = {
    "standings.json": "/standings/now",
    "skater_leaders.json": "/skater-stats-leaders/current",
    "goalie_leaders.json": "/goalie-stats-leaders/current",
}
# Plays the backend actually reads; the rest only bloat hundreds of frames.
EVENT_TYPES = {"goal", "penalty", "period-start", "period-end", "game-end"}
REG_PERIOD, OT_PERIOD = 1200, 300


def _seconds(mmss: str) -> int:
    m, s = mmss.split(":")
    return int(m) * 60 + int(s)


def _clock(seconds_left: int, running: bool, intermission: bool) -> dict:
    return {
        "timeRemaining": f"{seconds_left // 60:02d}:{seconds_left % 60:02d}",
        "secondsRemaining": seconds_left,
        "running": running,
        "inIntermission": intermission,
    }


def build(
    fixtures: Path,
    out: Path,
    step: int = 10,
    pregame_frames: int = 6,
    intermission_frames: int = 24,
    final_frames: int = 3,
) -> int:
    load = lambda name: json.loads((fixtures / name).read_text())  # noqa: E731
    pbp, score, rail, landing = (load("pbp_final.json"), load("score.json"),
                                 load("rightrail_final.json"), load("landing_final.json"))
    gid = pbp["id"]
    away_id, home_id = pbp["awayTeam"]["id"], pbp["homeTeam"]["id"]
    plays = sorted(pbp["plays"], key=lambda p: p["sortOrder"])
    last_period = max(p["periodDescriptor"]["number"] for p in plays)
    game_end = next((p for p in plays if p["typeDescKey"] == "game-end"), plays[-1])
    end_elapsed = _seconds(game_end["timeInPeriod"])

    def period_length(n: int) -> int:
        return REG_PERIOD if n <= 3 else OT_PERIOD

    out.mkdir(parents=True, exist_ok=True)
    (out / "frames").mkdir(exist_ok=True)
    (out / "meta.json").write_text(json.dumps({"interval": step, "synthetic_from": gid}))

    def write(frame: int, path: str, data) -> None:
        d = out / "frames" / f"{frame:04d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / encode_path(path)).write_text(json.dumps(data, separators=(",", ":")))

    # Static data once, in frame 0.
    for name, path in STATIC.items():
        write(0, path, load(name))
    write(0, "/club-schedule-season/TOR/now", load("schedule_tor.json"))
    for f in fixtures.glob("player_*.json"):
        write(0, f"/player/{f.stem.split('_', 1)[1]}/landing", json.loads(f.read_text()))

    # Slim scoreboard: other games are final and only need teams + score.
    base_score = copy.deepcopy(score)
    for g in base_score["games"]:
        for key in ("tvBroadcasts", "threeMinRecap", "threeMinRecapFr", "condensedGame", "condensedGameFr"):
            g.pop(key, None)
        if g["id"] != gid:
            g.pop("goals", None)
    score_idx = next(i for i, g in enumerate(base_score["games"]) if g["id"] == gid)
    all_score_goals = base_score["games"][score_idx].get("goals", [])

    base_pbp = {k: v for k, v in pbp.items() if k not in ("plays", "summary")}

    # (state, period, elapsed-in-period, clock dict) for every frame
    timeline: list[tuple[str, int, int, dict]] = []
    timeline += [("PRE", 1, -1, _clock(REG_PERIOD, False, False))] * pregame_frames
    for period in range(1, last_period + 1):
        length = period_length(period)
        stop = end_elapsed if period == last_period else length
        for elapsed in range(0, stop + 1, step):
            timeline.append(("LIVE", period, elapsed, _clock(length - elapsed, 0 < elapsed < length, False)))
        if period < last_period:
            timeline += [("LIVE", period, length, _clock(0, False, True))] * intermission_frames
    timeline += [("OFF", last_period, end_elapsed, _clock(0, False, False))] * final_frames

    for frame, (state, period, elapsed, clock) in enumerate(timeline):
        def happened(p) -> bool:
            n = p["periodDescriptor"]["number"]
            return n < period or (n == period and _seconds(p["timeInPeriod"]) <= elapsed)

        so_far = [p for p in plays if happened(p)] if state != "PRE" else []
        goals = [p for p in so_far if p["typeDescKey"] == "goal"]
        away = sum(1 for g in goals if g["details"]["eventOwnerTeamId"] == away_id)
        home = len(goals) - away

        def sog(tid: int) -> int:
            return sum(1 for p in so_far if p["typeDescKey"] in ("shot-on-goal", "goal")
                       and p.get("details", {}).get("eventOwnerTeamId") == tid)

        pd = {"number": period, "periodType": "REG" if period <= 3 else "OT", "maxRegulationPeriods": 3}

        p = copy.deepcopy(base_pbp)
        p.update(gameState=state, clock=clock, periodDescriptor=pd,
                 plays=[x for x in so_far if x["typeDescKey"] in EVENT_TYPES])
        p["awayTeam"].update(score=away, sog=sog(away_id))
        p["homeTeam"].update(score=home, sog=sog(home_id))
        if state != "OFF":
            p.pop("gameOutcome", None)
        code = so_far[-1].get("situationCode") if so_far else None
        if state == "LIVE" and clock["running"] and code and code != "1551":
            p["situation"] = {"situationCode": code, "awayTeam": {}, "homeTeam": {}}
        write(frame, f"/gamecenter/{gid}/play-by-play", p)
        if frame == 0:
            write(frame, f"/gamecenter/{gid}/right-rail", {"teamGameStats": []})
        if state == "OFF":
            write(frame, f"/gamecenter/{gid}/right-rail", rail)
        if state == "OFF":
            write(frame, f"/gamecenter/{gid}/landing", landing)

        s = copy.deepcopy(base_score)
        g = s["games"][score_idx]
        g.update(gameState=state, clock=clock, periodDescriptor=pd, period=period,
                 goals=all_score_goals[: len(goals)])
        g["awayTeam"].update(score=away, sog=sog(away_id))
        g["homeTeam"].update(score=home, sog=sog(home_id))
        if state != "OFF":
            g.pop("gameOutcome", None)
        write(frame, "/score/now", s)

    return len(timeline)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default="tests/fixtures")
    ap.add_argument("--out", default="replays/synthetic")
    ap.add_argument("--step", type=int, default=10, help="game seconds per frame")
    args = ap.parse_args()
    out = Path(args.out)
    if (out / "frames").exists():
        shutil.rmtree(out / "frames")  # stale frames would mix with the new ones
    frames = build(Path(args.fixtures), out, args.step)
    print(f"Wrote {frames} frames to {out}")


if __name__ == "__main__":
    main()
