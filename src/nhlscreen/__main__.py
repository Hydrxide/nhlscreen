"""Entry point: ``python -m nhlscreen [--config config.yaml] [--replay DIR]``."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .api.app import create_app
from .config import load_config
from .runtime import Runtime


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nhlscreen", description="NHL LED board backend")
    parser.add_argument("--config", "-c", help="YAML config file (default: ./config.yaml if present)")
    parser.add_argument("--replay", metavar="DIR", help="replay recorded snapshots instead of calling the NHL API")
    parser.add_argument("--speed", type=float, default=10.0, help="replay speed multiplier (default 10)")
    parser.add_argument("--host", help="override server.host")
    parser.add_argument("--port", type=int, help="override server.port")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    config_path = args.config or ("config.yaml" if Path("config.yaml").exists() else None)
    config = load_config(config_path)

    transport, speed = None, 1.0
    if args.replay:
        from .replay import ReplayTransport

        transport, speed = ReplayTransport(args.replay, speed=args.speed), args.speed
        # Replays compress time, so don't let the poll-rate floors stretch it back out.
        # Poll several times per frame so the clock never lags a frame behind.
        config.polling.live_poll_seconds = min(config.polling.live_poll_seconds, transport.interval / 5)
        config.polling.live_stats_seconds = config.polling.live_poll_seconds
        logging.getLogger(__name__).info(
            "Replaying %s (%d frames, %.0fs apart) at %.0fx",
            args.replay, len(transport.frames), transport.interval, speed,
        )

    # `displayteam` changes are saved next to the config so they survive restarts.
    state_dir = Path(config_path).resolve().parent if config_path else Path.cwd()
    state_path = None if args.replay else state_dir / "nhlscreen-state.json"

    runtime = Runtime(config, transport=transport, speed=speed, state_path=state_path)
    app = create_app(runtime)
    uvicorn.run(
        app,
        host=args.host or config.server.host,
        port=args.port or config.server.port,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    main()
