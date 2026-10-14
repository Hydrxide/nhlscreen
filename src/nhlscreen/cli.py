"""`displayteam`: switch the board's team from a terminal.

    displayteam              show the current team and what's on screen
    displayteam EDM          switch to the Oilers (also: `displayteam oilers`)
    displayteam TOR EDM      follow several teams
    displayteam --reset      go back to the teams in config.yaml
    displayteam --list       list all team codes

Talks to the running backend at $NHLSCREEN_URL (default http://127.0.0.1:9134).
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from .static.teams import TEAMS, resolve_team, suggest_teams

DEFAULT_URL = "http://127.0.0.1:9134"


def _describe(state: dict) -> str:
    teams = state["favorite_teams"]
    names = ", ".join(f"{t} ({TEAMS[t].name})" if t in TEAMS else t for t in teams)
    out = f"Displaying: {names}"
    if teams != state.get("configured_teams"):
        out += f"   [config.yaml: {', '.join(state.get('configured_teams') or [])}; --reset to restore]"
    screen = state.get("screen")
    if screen:
        out += f"\nOn screen:  {' | '.join(screen.get('lines') or [screen['id']])}"
    return out


def displayteam(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="displayteam",
        description="Switch which NHL team the LED board follows.",
    )
    ap.add_argument("teams", nargs="*", metavar="TEAM", help="3-letter code or nickname, e.g. TOR or leafs")
    ap.add_argument("--reset", action="store_true", help="revert to the teams in config.yaml")
    ap.add_argument("--list", action="store_true", help="list team codes")
    ap.add_argument("--url", default=os.environ.get("NHLSCREEN_URL", DEFAULT_URL), help="backend URL")
    args = ap.parse_args(argv)

    if args.list:
        for t in sorted(TEAMS.values(), key=lambda t: t.abbrev):
            print(f"{t.abbrev}  {t.name}")
        return 0

    codes: list[str] = []
    for text in args.teams:
        team = resolve_team(text)
        if team is None:
            hint = suggest_teams(text)
            msg = f"Unknown team '{text}'."
            if hint:
                msg += f" Did you mean {' or '.join(hint)}?"
            print(f"{msg} Run `displayteam --list` for all codes.", file=sys.stderr)
            return 2
        codes.append(team.abbrev)

    try:
        with httpx.Client(base_url=args.url, timeout=20) as client:
            if args.reset:
                resp = client.put("/control/teams", json={"teams": None})
            elif codes:
                resp = client.put("/control/teams", json={"teams": codes})
            else:
                resp = client.get("/control/teams")
    except httpx.HTTPError:
        print(f"Can't reach the nhlscreen backend at {args.url}. Is it running?", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        print(f"Backend refused: {detail}", file=sys.stderr)
        return 1
    print(_describe(resp.json()))
    return 0


def main() -> None:
    sys.exit(displayteam())
