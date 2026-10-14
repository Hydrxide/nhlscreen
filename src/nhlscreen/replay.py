"""Replay recorded API snapshots through the real pipeline.

A replay directory looks like:

    meta.json            {"interval": 10}
    frames/0000/score__now.json
    frames/0000/gamecenter__2026020102__play-by-play.json
    frames/0001/...

Frame N is "what the API returned N*interval seconds into the recording".
ReplayTransport plugs into httpx, so the client, parsers, poller, event
detector and scheduler all run unmodified. A path missing from a frame falls
back to the most recent earlier frame that has it (static data like standings
is usually only recorded once).
"""

from __future__ import annotations

import bisect
import json
import time
from pathlib import Path

import httpx


def encode_path(path: str) -> str:
    """'/v1/gamecenter/1/play-by-play?x=1' -> 'gamecenter__1__play-by-play.json'"""
    path = path.split("?", 1)[0]
    if path.startswith("/v1/"):
        path = path[4:]
    return path.strip("/").replace("/", "__") + ".json"


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, directory: str | Path, speed: float = 10.0, clock=time.monotonic) -> None:
        self.root = Path(directory)
        meta = json.loads((self.root / "meta.json").read_text())
        self.interval = float(meta.get("interval", 10))
        self.frames = sorted(p for p in (self.root / "frames").iterdir() if p.is_dir())
        if not self.frames:
            raise ValueError(f"no frames in {self.root}")
        self.speed = speed
        self.clock = clock
        self._start = clock()
        # file name -> sorted frame indices that contain it
        self._index: dict[str, list[int]] = {}
        for i, frame in enumerate(self.frames):
            for f in frame.iterdir():
                self._index.setdefault(f.name, []).append(i)

    @property
    def frame_index(self) -> int:
        elapsed = (self.clock() - self._start) * self.speed
        return min(int(elapsed // self.interval), len(self.frames) - 1)

    @property
    def finished(self) -> bool:
        return self.frame_index >= len(self.frames) - 1

    def _lookup(self, name: str) -> Path | None:
        """Most recent frame at or before the current one that has this file."""
        frames = self._index.get(name, [])
        pos = bisect.bisect_right(frames, self.frame_index)
        return self.frames[frames[pos - 1]] / name if pos else None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        found = self._lookup(encode_path(request.url.path))
        if found is None:
            return httpx.Response(404, json={"error": "not recorded"}, request=request)
        return httpx.Response(
            200,
            content=found.read_bytes(),
            headers={"Content-Type": "application/json", "Cache-Control": "max-age=0"},
            request=request,
        )
