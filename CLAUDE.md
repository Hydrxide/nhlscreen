# nhlscreen: context for Claude sessions

A DIY LED scoreboard for live NHL stats. This repo is the **backend + renderer**. It polls the NHL's public web API, turns data into "screens", decides what the board shows, and draws each screen as a 128×64 RGB frame. The browser preview at `/` shows exactly what the panel shows. `README.md` is the user-facing guide; this file is the working context: decisions, conventions, and what's next.

## Status (as of 2026-10-15)

- **Done:** NHL client, parsers, event detection, scheduler, FastAPI API + WebSocket, browser preview with team picker, `displayteam` CLI, replay/demo system, 128×64 LED renderer (fonts + layouts), preview-page player swapping and Show now buttons, fixed 25s anti-spoiler delay with a planned (jump-free) live clock, 135 passing tests.
- **Not started:** the hardware output process on the Pi (see *Next steps*).

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # setup (no uv on the dev Mac)
.venv/bin/pytest -q                                          # all tests; offline, ~6s
.venv/bin/python -m nhlscreen                                # live NHL data -> http://127.0.0.1:9134/
.venv/bin/python tools/synth_replay.py --out replays/synthetic          # build demo game (gitignored, ~17MB)
.venv/bin/python -m nhlscreen --replay replays/synthetic --speed 10     # demo: full game in ~7 min
.venv/bin/python tools/render_previews.py --out previews/              # every screen type -> LED PNGs
.venv/bin/displayteam EDM            # switch followed team on a running backend (--reset, --list)
```

`config.yaml` (gitignored) overrides defaults; copy `config.example.yaml`. macOS has no `timeout` command; run servers as background tasks. `GET /render/frame.png` gives the live frame as a PNG.

## Architecture

```
NHL API -> nhl/client.py -> nhl/parsers.py -> nhl/models.py
               ^                                   |
     services/poller.py (adaptive loops) -----> store.py --> services/events.py (GameEvents)
                                                   |                |
                                     services/scheduler.py <--------+  (alerts / live / rotation)
                                                   |
                                     views.py (Screen: data + spans + lines)
                                                   |
                       api/app.py (REST, WS /events, /render/frame) --> render/ (128x64 frames)
                                                   |
                                     api/debug.html (LED preview + team picker at /)
```

| Module | Role |
|---|---|
| `nhl/client.py` | httpx client. Honors CDN `Cache-Control: max-age`; host-wide exponential backoff on 429/5xx/network errors (honors `Retry-After`, max 120s); returns last good data on failure. |
| `nhl/parsers.py` | **The only code that touches raw NHL JSON.** Forgiving `.get` parsing. Fix API changes here. |
| `services/poller.py` | asyncio loops: scoreboard (20s live / 60s pregame / 15min idle), favorite-game tracking (play-by-play every 2s + right-rail stats every 30s, starts 15 min before puck drop), slow refreshes (standings/leaders/players 1h, schedules 6h). `favorites_changed()` re-targets tracking. |
| `services/events.py` | Diffs snapshots into goal / goal_overturned / penalty / period_start / period_end / game_final. First sighting of a game is silent (no alert flood on startup or mid-game team switch). |
| `services/scheduler.py` | What's on screen: hand-picked "Show now" screens (`force()`) > alerts > live favorite game > rotation (next game, scoreboard pages, standings, leaders, player cards). Rotation resumes during intermissions. At startup it holds the idle screen until the first (delayed) scoreboard lands, at most `WARMUP_MAX_SECONDS` (90); Show now still works then. Standings slots are built per followed division/conference (`_standings_slots`). |
| `views.py` | Builds `Screen` models. Each screen has `data` (structured), `spans` (tagged text), `lines` (plain text, derived from spans). |
| `render/` | Pure-Python renderer, no hardware deps: `fonts.py` (hand-drawn 5×7 + 3×5 pixel fonts), `canvas.py` (RGB buffer, text, LED-dot PNG), `layouts.py` (per-screen layouts). |
| `runtime.py` | Wires everything; favorite team and player switching (`set_favorite_teams` / `set_favorite_players`, the latter validates IDs via the API: `NHLNotFound` = no such player). Runtime choices persist in `nhlscreen-state.json` next to the config, one key per choice, so resetting teams keeps players and vice versa. |
| `replay.py` | httpx transport that serves recorded frames, so the whole pipeline runs unmodified in demos and tests. |
| `static/teams.py` | Team names, brand colors, LED display colors, the home/away rule. |

## NHL API notes (unofficial, undocumented: `api-web.nhle.com/v1`)

- `/score/now` 307-redirects to `/score/{date}`; the CDN caches it ~19s. Gamecenter `play-by-play` was `max-age=0` but measured 2026-10-14 during MTL @ WSH it was `max-age=19` with `Age` cycling 0–18: the feed is a 19s snapshot. The client subtracts `Age` from `max-age` (refetch as soon as the CDN copy expires) and `client.age(path)` dates clock readings, so polling faster than ~2s gains nothing.
- `play-by-play` has score, clock, `situation`, and all events. `right-rail` has team stats (SOG, faceoffs, PP, hits) in one small response. `landing` has three stars.
- `situationCode` is 4 digits: away goalie, away skaters, home skaters, home goalie. **Pulled goalie = extra attacker, not a power play.** Official strength subtracts one skater for a pulled goalie (`Situation.away_strength`), which matches the NHL's own ev/pp/sh labels on all test goals.
- After a penalty expires the feed keeps the PP `situationCode` (e.g. 1451) and `situationDescriptions: [PP]` until the next play, but drops `situation.timeRemaining`/`secondsRemaining` (seen live 2026-10-14, MTL @ WSH). So a power play is shown only while it has time left (`views.live_game_screen`), and the renderer hides it once its local countdown runs out.
- Plays get edited in place: same `eventId`, new `typeDescKey` (seen live 2026-10-15: `missed-shot` → `goal`, `blocked-shot` → `missed-shot`), and plays get removed. The detector keys seen plays by id *and* type, so a shot upgraded to a goal still alerts.
- The NHL removes an overturned goal's play from play-by-play; the detector emits `goal_overturned` when a seen goal id disappears.
- For untracked games the scoreboard **score change** is authoritative (the goal list can lag).
- Standings `wildcardSequence` is 0 for division leaders. No published rate limits; ~35 req/min during a favorite game (play-by-play every 2s, plus right-rail and scoreboard).

## Anti-spoiler delay

Fixed at `SPOILER_DELAY_SECONDS = 25` in `services/poller.py`, always on. The user removed the adjustable/toggle version (2026-10-14) in favor of this, because the delay must exceed the ~20s snapshot interval for the clock lookahead (see *Live clock*); don't make it configurable below ~22s. The poller fetches on time but queues each scoreboard / play-by-play snapshot (`Poller._deliver`) and applies it to the Store and the event detector at produced time (fetch minus CDN age) + delay, so clock, scores, alerts and finals all lag together. Tests set `poller.spoiler_delay_seconds = 0` for immediate updates. Tracking decisions use the fresh data. Scaled by replay speed. Schedules after a final wait out the delay; Show now's goal/final alerts use the stored (delayed) detail for tracked games. New live-data paths must go through `_deliver` or they'll spoil.

## Live clock

`data.clock` = `seconds_remaining`, `running`, `as_of`, `rate`. Renderers count down: `seconds_remaining - (now - as_of) * rate`.

The feed is coarse: a new reading every ~20s, and a CDN copy labelled age 0 can hold content the origin changed up to 19s earlier (measured live 2026-10-14; no timestamp exists in headers or JSON). Counting from each reading made the board jump both ways. Fix: each tracked game's readings go into a `ClockTrack` (`services/clocktrack.py`) **when fetched**, dated to display time (produced + delay). Since the delay exceeds the snapshot interval, the next reading is usually known while the current one is shown, and the track plans a jump-free path between them: steady pace to the next running reading, run-then-hold into a stop, hold-then-start out of one. `Store.clock_segment()` gives the piece in effect; `views.live_game_screen(segment=...)` puts it in `data.clock` (`rate` 0 = held, `running` false). Observed live: no jumps, pace 0.75–1.5×. Without a track (untracked games, before the first reading) the old path applies: `as_of` = `Store.clock_as_of` = first arrival minus lag. Re-fetching an unchanged reading must not move anything. The PP timer isn't planned separately: penalty time only runs with the game clock, so `render/layouts.situation_text()` derives it from the shown game clock (`situation.seconds_remaining - (situation.clock_at - game_seconds())`). `rate` is the replay speed in demos (the track's nominal rate).

## Display decisions (made by the user; keep them unless asked)

**Text colors (spans):** every screen's text is `spans`, lines of `{kind, text, color?}`:
- `label` = **white**: titles, ranks (`1.`), `NEXT:`/`LAST:`, `vs`/`@`, stat names (`SOG`, `GP`), `GOAL!`, `A:`.
- `value` = **gold** (255,176,0): scores, clock, **period names** (`2ND`, `OT`, `1ST INT`), `FINAL`/`FINAL/OT`, `WARMUP` (NHL state `PRE`), start times, stat values, results (`L 4-5`).
- **Records** (`W-L-OTL`) are split by `views.record_spans()`: wins light green `WIN_COLOR` (120,255,120), losses light red `LOSS_COLOR` (255,110,110), OT losses gold, dashes white. Standings rows carry them as `record_spans`, which `layout_standings` draws in every table.
- `team` = team code in its resolved color. `player` = player name in **its team's own color, always** (even on the away side).
- Any span may carry an explicit `color` that overrides its kind. The backend resolves colors so renderers never apply the rules themselves.

**Team colors:** `display` = primary brand color with hue kept and HSV brightness raised to at least 0.70 (dark navy/black barely light LEDs). Black/grey primaries use the secondary (LA → silver). `LED_OVERRIDES`: COL wine `B8244A`, EDM orange, SEA ice blue. Never fall back to white.

**Home/away rule:** only for *live game info*: the live game screen and goal / no-goal alerts' score line. Home = its color, away = **white** (jersey convention). Scoreboard pages, final alert, standings, leaders, etc. use each team's own color, even if two teams look alike.

**Goal alert:** the whole `TEAM GOAL!` banner is the scoring team's own color (home or away), then a **white divider line**, then scorer (season total in gold, PPG/SHG tag), assists, score line. Holds 25s (`alert_seconds`); finals hold 60s (`final_alert_seconds`), user's choice 2026-10-15. Assists often land after the goal, so while a goal alert is queued or shown the scheduler rebuilds it from the stored (delayed) play-by-play goal with the same event id (`Scheduler._refresh_goal_alert`), keeping its timer. The feed has no "unassisted" flag: no assist fields just means none yet. A scorer's third goal of the game (exactly the third, counted by `scorer_id` over the game's goals up to it, `views.scorer_game_goals`) titles the alert `HAT TRICK!` (no team code, so it fits at 2× like `TOR GOAL!`; the team color identifies the team), same body. Scoreboard-only goals (no goal list) never get it. A goal whose scorer is a goalie (`Goal.scorer_is_goalie`, from play-by-play `rosterSpots` `positionCode` "G"; goalie assists aren't called out, user's choice) titles it `GOALIE GOAL!` instead, which wins over a hat trick; at 2× it spans the full panel width.

**Intermission screen** (type `intermission`): on a favorite game's `period_end`, the scheduler queues `views.intermission_screen()`: the live game screen with the clock forced to intermission (`INT` under the period) and no PP/EN line, as an alert for `intermission_alert_seconds` (20s, user's choice 2026-10-15), drawn by `layout_live_game`. Then the intermission rotation starts. After the 3rd or an OT period only if the score is tied at the period end (the game goes to OT / shootout; the user's rule), never after a shootout.

**Live game layout (128×64):** team codes 2×, scores 3×, period above a 2× clock between the scores (clock drops to 1× if two-digit scores would crowd it), PP/EN line under the clock, two-color divider at y=42, SOG row, then up to two small-font stat rows (PP, FO).

**Standings (default rotation has all three):** *division* tables only for divisions containing a followed team (one per division, every followed team highlighted); *wildcard* (screen type `wildcard`) per followed conference, two columns: division top 3s on the left, WC1/WC2 + cut line + chasers on the right, and a followed team further back takes the last visible row; *league* = all 32 teams paged 4 per screen (`LEAGUE_PAGE_ROWS`) with an `n/8` counter, 5s each. Those 4-row pages are all regular size, ordered rank, team, PTS, REC, STK, and drop the GP column (six columns don't fit at 1×; the record implies GP), `_BIG_STANDINGS_COLS` in `render/layouts.py`; 8-row tables stay small with GP. `conference` scope also exists (paged), not in the default rotation.

**Player cards:** small white stat labels over stat values that are **all regular (1×) size**, so goalie decimals (GAA, SV%) match whole numbers. Both skaters and goalies get "LAST GAME" plus one line in regular size (the small font was too hard to read): skaters `1G 0A @ UTA` (numbers gold, `G`/`A` white), goalies `W  30SV  .909  @ COL` (gold save count directly followed by white `SV`, no space) (decision W/L/OTL omitted for no-decision; save % dropped if too wide). Built by `last_game_spans()` in `render/layouts.py`. Avoid `@` in the small font (it reads as "e").

**Scoreboard pages:** one game per row, `NJD  5 @ DET  4  FINAL/OT`: a white `@` (regular size, 3px either side) between away and home; "AT" only if a narrower panel can't fit it. Column positions are the `_SB_*` constants in `render/layouts.py`.

**Show now (preview page):** buttons from `Runtime.SHOW_MENU`; a press clears queued alerts and the rotation position, plays every screen of that entry (configured dwell), outranking even a live game, then the rotation restarts from the top. Goal alert / Final score are rebuilt from the followed teams' most recent real game (play-by-play fetched on demand), never invented.

**Other:** ranks render as `1.`; Standings highlight the followed team's row with a neutral grey bar (a team-colored bar hid that team's code). Defaults: team TOR; players Gavin McKenna `8486067` and Sergei Bobrovsky `8475683`. The preview page lets you swap **two** favorite players (config can hold more; the API allows up to 10).

## Working conventions

- Tests use recorded fixtures in `tests/fixtures/` (real API responses, TOR @ UTA 2026-10-13); **no network in tests**. Patch `poller.favorites_changed` or use a stub client when exercising team switches.
- **Visual changes:** run `tools/render_previews.py` and look at the PNGs (Claude can Read them) before calling a layout done; check worst cases (long names, two assists, two-digit scores, conference tables).
- The user reviews in the browser demo; restart the demo server after backend changes (`debug.html` is re-read per request, Python isn't).
- Keep span semantics: when adding text, tag it `label`/`value`/`team`/`player` rather than formatting strings.
- Update `README.md` when user-facing behavior changes. Python ≥3.11 (Raspberry Pi OS Bookworm ships 3.11).

## Next steps

1. **Pi matrix output:** a separate process (needs root for GPIO) that subscribes to `ws://localhost:9134/events`, keeps the current screen, calls `render()` at ~10 fps, and pushes frames with `rpi-rgb-led-matrix` (`SetImage`/canvas, `--led-rows=64 --led-cols=64 --led-chain=2`). Blacklist `snd_bcm2835` (the library refuses to run with onboard audio) and consider `isolcpus=3`. Add a systemd unit next to `deploy/nhlscreen.service`.
2. Tune colors/brightness on the real panel (blue is the dimmest color on black; `MIN_BRIGHTNESS` and `LED_OVERRIDES` in `static/teams.py`).
3. Ideas raised but not built: show the last goal on the live screen when team stats are missing; drop the team code on the leaders screen if name + code run together on the panel.
