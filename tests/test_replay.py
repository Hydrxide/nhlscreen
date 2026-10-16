"""End to end: replay a synthesized game through the real client/poller/scheduler."""

import asyncio
import json
import sys
from pathlib import Path

from nhlscreen.config import Config
from nhlscreen.replay import ReplayTransport, encode_path
from nhlscreen.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import synth_replay  # noqa: E402


def test_encode_path():
    assert encode_path("/v1/gamecenter/1/play-by-play") == "gamecenter__1__play-by-play.json"
    assert encode_path("/v1/skater-stats-leaders/current?categories=goals") == "skater-stats-leaders__current.json"


async def test_replay_end_to_end(tmp_path):
    frames = synth_replay.build(ROOT / "tests" / "fixtures", tmp_path, step=60)
    assert frames > 60

    speed = 2400.0
    transport = ReplayTransport(tmp_path, speed=speed)
    config = Config(favorite_teams=["TOR"], favorite_players=[8478483])
    config.display.alert_seconds = 0.05
    config.display.final_alert_seconds = 0.1
    config.display.intermission_alert_seconds = 0.05
    runtime = Runtime(config, transport=transport, speed=speed, tick_seconds=0.02)
    await runtime.start()
    screen_types = set()
    try:
        for _ in range(400):
            await asyncio.sleep(0.02)
            if runtime.screen:
                screen_types.add(runtime.screen.type)
            if any(e.kind == "game_final" for e in runtime.store.recent_events):
                break
        for _ in range(25):  # let the final alert play and rotation resume
            await asyncio.sleep(0.02)
            screen_types.add(runtime.screen.type)
    finally:
        await runtime.stop()

    events = list(runtime.store.recent_events)
    kinds = [e.kind for e in events]
    goals = [e for e in events if e.kind == "goal"]
    assert len(goals) == 9, kinds
    assert [g.goal.scorer_name for g in goals][:2] == ["D. But", "V. Trocheck"]
    assert (goals[-1].away_score, goals[-1].home_score) == (4, 5)
    assert kinds.count("game_final") == 1
    assert "penalty" in kinds and "period_end" in kinds
    assert {"live_game", "goal_alert", "intermission", "final_alert"} <= screen_types
    assert runtime.screen.type not in {"live_game", "goal_alert", "intermission", "final_alert"}  # back to rotation


def test_synthetic_clock_flows_evenly(tmp_path):
    """Consecutive live frames advance the game clock by exactly one step."""
    synth_replay.build(ROOT / "tests" / "fixtures", tmp_path, step=10)
    frames = sorted((tmp_path / "frames").iterdir())
    pbp_name = "gamecenter__2026020102__play-by-play.json"
    clocks = []
    for f in frames:
        doc = json.loads((f / pbp_name).read_text(encoding="utf-8"))
        if doc["gameState"] == "LIVE" and doc["clock"]["running"]:
            clocks.append((doc["periodDescriptor"]["number"], doc["clock"]["secondsRemaining"]))
    steps = {a[1] - b[1] for a, b in zip(clocks, clocks[1:]) if a[0] == b[0]}
    assert steps == {10}
    assert {p for p, _ in clocks} == {1, 2, 3}


def test_replay_transport_falls_back_to_earlier_frames(tmp_path):
    synth_replay.build(ROOT / "tests" / "fixtures", tmp_path, step=60)
    t = [0.0]
    transport = ReplayTransport(tmp_path, speed=1.0, clock=lambda: t[0])
    t[0] = 60 * 50  # well into the game; standings were only recorded in frame 0
    assert transport._lookup("standings__now.json").parent.name == "0000"
    assert transport._lookup("score__now.json").parent.name == "0050"
    assert transport._lookup("nope.json") is None
