"""Record a live game's API responses for later replay.

    python tools/record_game.py --team TOR --out recordings/tor-2026-10-15

Run it shortly before puck drop. It snapshots the scoreboard plus the team's
play-by-play and stats every --interval seconds until the game is final.
Replay with:  python -m nhlscreen --replay recordings/tor-2026-10-15
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nhlscreen.replay import encode_path  # noqa: E402

BASE = "https://api-web.nhle.com/v1"


def fetch(client: httpx.Client, path: str, params: dict | None = None):
    resp = client.get(BASE + path, params=params)
    resp.raise_for_status()
    return resp.json()


def save(frame_dir: Path, path: str, data) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    (frame_dir / encode_path(path)).write_text(json.dumps(data))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--player", type=int, action="append", default=[], help="favorite player id (repeatable)")
    args = ap.parse_args()

    team = args.team.upper()
    out = Path(args.out)
    (out / "frames").mkdir(parents=True, exist_ok=True)
    (out / "meta.json").write_text(json.dumps({"interval": args.interval, "team": team}))

    with httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": "nhlscreen-recorder/0.1"}) as client:
        static = out / "frames" / "0000"
        save(static, "/standings/now", fetch(client, "/standings/now"))
        save(static, f"/club-schedule-season/{team}/now", fetch(client, f"/club-schedule-season/{team}/now"))
        save(static, "/skater-stats-leaders/current",
             fetch(client, "/skater-stats-leaders/current", {"categories": "points,goals,assists", "limit": 5}))
        save(static, "/goalie-stats-leaders/current",
             fetch(client, "/goalie-stats-leaders/current", {"categories": "wins,savePctg", "limit": 5}))
        for pid in args.player:
            save(static, f"/player/{pid}/landing", fetch(client, f"/player/{pid}/landing"))

        frame, finals_seen = 0, 0
        while True:
            frame_dir = out / "frames" / f"{frame:04d}"
            score = fetch(client, "/score/now")
            save(frame_dir, "/score/now", score)
            game = next((g for g in score.get("games", []) if team in (g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"])), None)
            if game is None:
                print(f"{team} has no game today")
                return
            gid, state = game["id"], game["gameState"]
            if state in ("PRE", "LIVE", "CRIT", "FINAL", "OFF"):
                save(frame_dir, f"/gamecenter/{gid}/play-by-play", fetch(client, f"/gamecenter/{gid}/play-by-play"))
                save(frame_dir, f"/gamecenter/{gid}/right-rail", fetch(client, f"/gamecenter/{gid}/right-rail"))
            if state in ("FINAL", "OFF"):
                save(frame_dir, f"/gamecenter/{gid}/landing", fetch(client, f"/gamecenter/{gid}/landing"))
                finals_seen += 1
                if finals_seen >= 2:
                    print(f"Recorded {frame + 1} frames to {out}")
                    return
            print(f"frame {frame:04d}  {state}  {game['awayTeam'].get('score', '-')}-{game['homeTeam'].get('score', '-')}")
            frame += 1
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
