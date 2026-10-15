# nhlscreen

Backend data service for a DIY NHL LED scoreboard. It polls the NHL's public web API, normalizes the data, detects goals, penalties and finals, and decides what the board should show. It serves all of this over HTTP/WebSocket on localhost.

The backend never draws pixels. It hands out layout-agnostic **screens**, so the LED panel size and renderer can be chosen later, whether that's a Pi HUB75 matrix, an ESP32, or a browser preview.

```
NHL API ─► client (cache, backoff) ─► parsers ─► store ─┬─► event detector ─► alerts
                    ▲                                    └─► scheduler ──────► current screen
              poller (adaptive cadence)                                         │
                                                         FastAPI :9134  ◄───────┘
                                                         REST + WS /events ─► renderer
```

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config.example.yaml config.yaml      # set favorite_teams / favorite_players
.venv/bin/python -m nhlscreen           # serves http://127.0.0.1:9134
```

On Windows, use `.venv\Scripts\` in place of `.venv/bin/` (and `copy` for `cp`). Python 3.11 or newer is required.

### Running the board

Live NHL data, or a demo game replayed at 10× (a full game in about 7 minutes). Both serve on port 9134, so run one at a time; Ctrl+C stops it.

```bash
# macOS / Linux / Pi
.venv/bin/python -m nhlscreen                                          # live
.venv/bin/python tools/synth_replay.py --out replays/synthetic         # build the demo game once (~17 MB)
.venv/bin/python -m nhlscreen --replay replays/synthetic --speed 10    # replay
```

```powershell
# Windows
.venv\Scripts\python.exe -m nhlscreen                                       # live
.venv\Scripts\python.exe tools\synth_replay.py --out replays\synthetic      # build the demo game once (~17 MB)
.venv\Scripts\python.exe -m nhlscreen --replay replays\synthetic --speed 10 # replay
```

Open **http://127.0.0.1:9134/** for a live preview of the board. The left column has the LED preview, **Show now** buttons and favorite players; the right column has the team picker, recent events and a collapsible **Screen JSON** debug panel (collapsed by default; the page remembers if you open it).

## What the board shows

Screens are chosen in priority order:

1. **Alerts** for favorite-team goals, overturned goals and finals. Each holds for `alert_seconds`.
2. **Live game** whenever a favorite team is playing. The rotation resumes during intermissions (configurable).
3. **Rotation**: next game and last result, the league scoreboard (paged), standings, stat leaders, and favorite player cards. Standings come in three kinds, each a separate rotation entry in `config.yaml`:
   - **Division:** only divisions that contain a followed team.
   - **Wild card:** the NHL playoff format for each conference you follow: every division's top 3, then the wild card spots, a cut line and the chasers.
   - **League:** all 32 teams across eight pages of 4, 5 seconds each.

   Followed teams are highlighted with a grey bar in every table.

Each screen has this shape:

```json
{
  "id": "live_game:2026020102", "type": "live_game", "priority": 50, "dwell_seconds": 15,
  "data":  { "...structured fields: teams + LED-safe colors, score, clock, situation, stats" },
  "spans": [
    [{"kind": "team", "text": "TOR"}, {"kind": "label", "text": " "}, {"kind": "value", "text": "2"}, "..."],
    [{"kind": "label", "text": "2nd"}, {"kind": "label", "text": " "}, {"kind": "value", "text": "12:34", "clock": true}, "..."]
  ],
  "lines": ["TOR 2  UTA 4", "2nd 12:34  PP TOR", "SOG 18-21"]
}
```

`spans` is the screen's text, line by line, split into tagged pieces so a renderer can style each one:

| kind | color | used for |
|---|---|---|
| `label` | white | titles, ranks (`1.`), `NEXT:` / `LAST:`, `vs` / `@`, stat names (`SOG`, `GP`), `GOAL!`, `A:` |
| `value` | gold | the stats: scores, clock, period names (`2ND`, `OT`, `1ST INT`), `FINAL`, `WARMUP`, start times, records, stat values, results (`L 4-5`) |
| `team` | span's `color` | team codes. On live game info `color` follows the home/away rule; elsewhere it's the team's own color |
| `player` | span's `color` | player names, always in their team's own color (even on the away side) |

Team and player spans carry their resolved `color`, so a renderer never needs to apply the color rules itself. The span with `"clock": true` is the live game clock; renderers replace its text as they count down. `lines` is the same text as plain strings, for logs and the simplest panels. `data` carries everything a richer renderer needs.

**Team colors.** Every team carries `colors.display`: its primary brand color with the hue kept but brightness raised, because dark navy and black barely light an LED. Teams whose primary is black use their secondary color, and a few hand-picked exceptions live in `LED_OVERRIDES` in `static/teams.py`; tune those by eye once the panel is built. For live game info (the live game screen and goal / no-goal alerts), teams also carry `colors.game`, which follows the jersey convention: **home team in its color, away team in white**, so the two sides never look alike. Everything else, including the league scoreboard pages and the final-score alert, uses each team's own `colors.display`. Renderers should use `colors.game` when it's present and `colors.display` otherwise.

For a live game, `data.clock` has `seconds_remaining`, `running`, `as_of` and `rate`. Renderers should **count the clock down locally**, showing `seconds_remaining - (now - as_of) * rate`; `/debug` does this. The NHL only publishes a new clock reading about every 20 seconds, and a reading can be up to ~19s older than it claims. Because the board runs 25 seconds behind (see *Anti-spoiler delay*), it already has the next reading while showing the current one, so the backend steers the clock to arrive at each reading exactly on time: `rate` is the pace for the current stretch (usually 1, a little slower after a missed whistle, a little faster after a stale reading, 0 while held at a stoppage). The clock never jumps; it just runs a bit uneven at times. The power-play time under the clock (`data.situation.seconds_remaining`, read when the game clock showed `clock_at`) counts down off that same game clock, `seconds_remaining - (clock_at - game clock now)`, so it ticks, slows and holds exactly with the main timer.

## Switching teams

### From a browser (laptop or phone)

Open **http://raspberrypi.local:9134/** (use your Pi's hostname or IP) and tap a team. The page shows all 32 teams grouped by division in their colors. A **LIVE** badge marks teams playing right now, and teams with a game still to come show its start time. Tick **Follow several** to pick more than one team; **Reset to config** returns to `config.yaml`. Every open copy of the page stays in sync, including changes made with `displayteam`.

To reach the page from other devices, open the backend to your network in `config.yaml` on the Pi and restart it:

```yaml
server:
  host: 0.0.0.0
```

There's no login, so anyone on your Wi-Fi can switch teams. Bookmark the page or add it to your phone's home screen.

### Show now (browser)

Under the board preview, **Show now** has a button for each screen: next game, scoreboard, division, wild card, league standings, leaders, player cards, goal alert and final score. A press clears the queue and plays that screen next (every page of it, each for its normal time, even during a live game), then the rotation starts over from the top. **Goal alert** and **Final score** replay your team's most recent real game. Buttons with nothing to show yet are greyed out. Every open page controls the same board, so the latest press wins.

### Anti-spoiler delay

TV and streaming broadcasts run behind real time, so a board fed straight from the NHL would flash goals before you see them. Live game data is always shown **25 seconds** after the NHL produced it: the live clock, scores, goal and no-goal alerts, and final scores. It isn't adjustable: 25s is longer than the NHL's ~20s update interval, which is what lets the board plan the clock one reading ahead (see the live clock notes above). To change it, edit `SPOILER_DELAY_SECONDS` in `services/poller.py`; keep it above ~22s. Standings, schedules, leaders and player cards aren't delayed. In replays the delay is in game seconds, so it shrinks with `--speed` like everything else.

### Favorite players (browser)

Under the team picker, the **Favorite players** box has two fields. Type a player ID, or paste the player's nhl.com link (`nhl.com/player/gavin-mckenna-8486067`), and press **Save players**. Each ID is checked with the NHL before saving, so a typo gets an error instead of a blank card. Leave a field empty to feature one player; **Reset to config** returns to `favorite_players` from `config.yaml`. The choice is saved with your team choice in `nhlscreen-state.json`.

### From a terminal: `displayteam`

```bash
displayteam              # show the current team and what's on screen
displayteam EDM          # follow the Oilers (nicknames work too: displayteam oilers)
displayteam TOR EDM      # follow several teams; their live games alternate
displayteam --reset      # back to favorite_teams from config.yaml
displayteam --list       # all team codes
```

The switch takes effect immediately. If the new team is playing, the board jumps to its live game, and live polling moves from the old team's game to the new one. A game you switch into partway through doesn't replay old goal alerts; alerts start with the next goal.

Your choice is saved to `nhlscreen-state.json` next to `config.yaml`, so it survives a reboot. Use `--reset` to delete that file and go back to the config.

The command talks to the backend at `$NHLSCREEN_URL` (default `http://127.0.0.1:9134`). From a laptop, use `export NHLSCREEN_URL=http://raspberrypi.local:9134`. It's installed at `.venv/bin/displayteam`. To run it as plain `displayteam` on the Pi:

```bash
mkdir -p ~/.local/bin && ln -sf ~/nhlscreen/.venv/bin/displayteam ~/.local/bin/displayteam
# ~/.local/bin is on PATH by default on Raspberry Pi OS (open a new shell)
```

## LED rendering

`src/nhlscreen/render/` turns a screen into the exact 128×64 frame the LED matrix will show. It's pure Python with no hardware dependency, so the same code draws the browser preview now and will drive the panel on the Pi later.

- **Fonts** (`render/fonts.py`): hand-drawn 5×7 (about 21 characters per line) and 3×5 (about 30). Letters are proportional and digits fixed-width so numbers line up. Text is drawn uppercase.
- **Layouts** (`render/layouts.py`): hand-placed for the live game, scoreboard, standings, leaders and player cards. Alerts, next game and idle center their lines and use the largest font that fits.
- **Endpoints:** `GET /render/frame` returns the current frame as raw RGB bytes (width × height × 3; size in the `X-Width`/`X-Height` headers). `GET /render/frame.png?pitch=6` returns it as an LED-dot picture.
- **Size:** `display.matrix_width` / `matrix_height` in `config.yaml` (default 128×64). The hand-placed layouts are designed for 128×64.

The preview page draws these frames as LED dots several times a second, so what you see there is what the panel will show.

## API

| | |
|---|---|
| `GET /screen/current` | what the board should show now |
| `WS /events` | pushes `{"type":"screen"}` on every change and `{"type":"event"}` for goals, penalties and finals |
| `GET /games`, `/games/{id}` | today's games, and one game in depth (stats, penalties, three stars) |
| `GET /standings?scope=division&team=TOR` | standings |
| `GET /leaders`, `/players/{id}`, `/teams/{abbrev}` | leaders, favorite players, team summary |
| `GET /events/recent`, `/health` | recent events; data freshness and API backoff state |
| `GET /render/frame`, `/render/frame.png` | the current screen as the LED matrix frame (raw RGB, or a PNG preview) |
| `GET/PUT /control/teams` | current favorite teams; `{"teams": ["EDM"]}` to switch, `{"teams": null}` to reset (used by `displayteam`) |
| `GET /control/screens`, `POST /control/show` | the Show now menu; `{"screen": "standings_league"}` plays that screen next |
| `GET/PUT /control/players` | current favorite players; `{"players": [8486067, 8475683]}` to switch (IDs checked with the NHL), `{"players": null}` to reset |

A renderer only needs to connect to `WS /events` and draw each `screen` message it receives.

## Polling and rate limits

The NHL API is unofficial and doesn't publish rate limits. Cadence follows what the CDN allows:

| Data | Live | Otherwise |
|---|---|---|
| Favorite-game play-by-play (score, clock, events) | 2 s (`live_poll_seconds`, the minimum) | not polled |
| Favorite-game team stats (SOG, faceoffs, PP) | 30 s | — |
| League scoreboard (the CDN caches it for about 19 s) | 20 s | 60 s pregame, 15 min idle |
| Standings, leaders, players / schedules | 1 h / 6 h | |

That comes to about 15 requests per minute during a favorite-team game. The client honors `Cache-Control: max-age`. On a 429 or 5xx it backs off exponentially, honors `Retry-After` and keeps serving the last good data, so the board never goes blank.

## Testing without a live game

```bash
.venv/bin/pytest                                     # unit + end-to-end tests (offline)

# Synthesize a "live" game from a finished one, then replay it at 10x:
# (game clock runs 10x: each period takes 2 minutes, a full game about 7)
.venv/bin/python tools/synth_replay.py --out replays/synthetic
.venv/bin/python -m nhlscreen --replay replays/synthetic --speed 10

# Record a real game (start before puck drop) for faithful replays later:
.venv/bin/python tools/record_game.py --team TOR --out recordings/tor-game
```

Replay mode feeds recorded responses through the real client, poller, detector and scheduler; only the HTTP transport is swapped.

To check layouts without running anything, `tools/render_previews.py --out previews/` writes every screen type as an LED-style PNG.

Working on this with Claude Code? `CLAUDE.md` has the architecture, the display decisions made so far, and next steps.

## Raspberry Pi

```bash
git clone <this repo> ~/nhlscreen && cd ~/nhlscreen
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml config.yaml
sudo cp deploy/nhlscreen.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nhlscreen
```

The backend runs as a normal user. The future LED renderer, which needs root for `rpi-rgb-led-matrix`, runs as its own process and talks to this one over `localhost:9134`.

## Layout

```
src/nhlscreen/
  nhl/client.py      HTTP, caching, backoff, last-good fallback
  nhl/parsers.py     the only code that touches raw NHL JSON
  nhl/models.py      domain models
  services/poller.py adaptive polling loops
  services/events.py snapshot diffs -> GameEvents
  services/scheduler.py  alert > live > rotation
  views.py           Screen view models
  render/            LED frames: fonts, canvas, per-screen layouts
  cli.py             displayteam command
  api/app.py         FastAPI + /debug page
  replay.py          replay transport
tools/               record_game.py, synth_replay.py, render_previews.py
tests/fixtures/      real API responses (2026-10-13, TOR @ UTA)
```
