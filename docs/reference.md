# How nhlscreen works

The technical reference: what's inside, the screen format a renderer receives, and the API. To set nhlscreen up and use it, see the [README](../README.md).

nhlscreen polls the NHL's public web API, normalizes the data, detects goals, penalties and finals, and decides what the board should show. It serves all of this over HTTP and WebSocket, and draws each screen as a 128 × 64 RGB frame.

```
NHL API ─► client (cache, backoff) ─► parsers ─► store ─┬─► event detector ─► alerts
                    ▲                                    └─► scheduler ──────► current screen
              poller (adaptive cadence)                                         │
                                                         FastAPI :9134  ◄───────┘
                                                         REST + WS /events ─► renderer
```

## Screens

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

Team and player spans carry their resolved `color`, so a renderer never needs to apply the color rules itself. W-L-OTL records (`6-1-1`) are split into spans the same way: wins light green, losses light red, OT losses gold, dashes white; standings rows also carry them as `record_spans`. The span with `"clock": true` is the live game clock; renderers replace its text as they count down. `lines` is the same text as plain strings, for logs and the simplest panels. `data` carries everything a richer renderer needs.

## Team colors

Every team carries `colors.display`: its primary brand color with the hue kept but brightness raised, because dark navy and black barely light an LED. Teams whose primary is black use their secondary color, and a few hand-picked exceptions live in `LED_OVERRIDES` in `static/teams.py`; tune those by eye on your panel.

For live game info (the live game screen and goal / no-goal alerts), teams also carry `colors.game`, which follows the jersey convention: **home team in its color, away team in white**, so the two sides never look alike. Everything else, including the league scoreboard pages and the final-score alert, uses each team's own `colors.display`. Renderers should use `colors.game` when it's present and `colors.display` otherwise.

## The live clock

For a live game, `data.clock` has `seconds_remaining`, `running`, `as_of` and `rate`. Renderers should **count the clock down locally**, showing `seconds_remaining - (now - as_of) * rate`; the preview page does this.

The NHL only publishes a new clock reading about every 20 seconds, and a reading can be up to ~19s older than it claims. Because the board runs at least 25 seconds behind (the anti-spoiler delay), it already has the next reading while showing the current one, so the backend steers the clock to arrive at each reading exactly on time: `rate` is the pace for the current stretch (usually 1, a little slower after a missed whistle, a little faster after a stale reading, 0 while held at a stoppage). The clock never jumps; it just runs a bit uneven at times.

The power-play time under the clock (`data.situation.seconds_remaining`, read when the game clock showed `clock_at`) counts down off that same game clock, `seconds_remaining - (clock_at - game clock now)`, so it ticks, slows and holds exactly with the main timer.

When the next reading has the clock stopped, the clock reaches the stopped time before that reading is due and holds there. The snapshot that stopped it (usually a goal) is released at that moment instead of when due, so the goal alert, the new score and the stopped clock appear together. The delay then counts from the play itself rather than from the snapshot that carried it; nothing is shown less than the delay behind the game clock.

The delay is `display.spoiler_delay_seconds`, also settable with `PUT /control/delay`. Its default and minimum is `SPOILER_DELAY_SECONDS` (25, in `config.py`), its maximum 300. Don't lower the minimum below ~22s, or the clock can no longer be planned one reading ahead. A change during a game moves the clock plan with it: a longer delay holds the clock where it is until the plan catches up, a shorter one jumps it ahead.

## LED rendering

`src/nhlscreen/render/` turns a screen into the exact 128 × 64 frame the LED matrix shows. It's pure Python with no hardware dependency, so the same code draws the browser preview and the panel.

- **Fonts** (`render/fonts.py`): hand-drawn 5×7 (about 21 characters per line) and 3×5 (about 30). Letters are proportional and digits fixed-width so numbers line up. Text is drawn uppercase.
- **Layouts** (`render/layouts.py`): hand-placed for the live game, scoreboard, standings, leaders and player cards. Alerts, next game and idle center their lines and use the largest font that fits.
- **Endpoints:** `GET /render/frame` returns the current frame as raw RGB bytes (width × height × 3; size in the `X-Width`/`X-Height` headers). `GET /render/frame.png?pitch=6` returns it as an LED-dot picture.
- **Size:** `display.matrix_width` / `matrix_height` in `config.yaml` (default 128 × 64). The hand-placed layouts are designed for 128 × 64.

The preview page draws these frames as LED dots several times a second, so what you see there is what the panel shows.

## API

| | |
|---|---|
| `GET /screen/current` | what the board should show now |
| `WS /events` | pushes `{"type":"screen"}` on every change and `{"type":"event"}` for goals, penalties and finals (plus `teams`, `players` and `delay` when those settings change) |
| `GET /games`, `/games/{id}` | today's games, and one game in depth (stats, penalties, three stars) |
| `GET /standings?scope=division&team=TOR` | standings |
| `GET /leaders`, `/players/{id}`, `/teams/{abbrev}` | leaders, favorite players, team summary |
| `GET /events/recent`, `/health` | recent events; data freshness and API backoff state |
| `GET /render/frame`, `/render/frame.png` | the current screen as the LED matrix frame (raw RGB, or a PNG preview) |
| `GET/PUT /control/teams` | current favorite teams; `{"teams": ["EDM"]}` to switch, `{"teams": null}` to reset (used by `displayteam`) |
| `GET /control/screens`, `POST /control/show` | the Show now menu; `{"screen": "standings_league"}` plays that screen next |
| `GET/PUT /control/players` | current favorite players; `{"players": [8486067, 8475683]}` to switch (IDs checked with the NHL), `{"players": null}` to reset |
| `GET/PUT /control/delay` | the anti-spoiler delay and its allowed range; `{"seconds": 45}` to lengthen it (25–300, 400 otherwise), `{"seconds": null}` to reset |

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

## Code layout

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
  api/app.py         FastAPI + the preview page (/debug)
  replay.py          replay transport
tools/               record_game.py, synth_replay.py, render_previews.py
docs/hcontroller.md  running nhlscreen as an HController module
deploy/              systemd unit for running without HController
tests/fixtures/      real API responses (2026-10-13, TOR @ UTA)
```
