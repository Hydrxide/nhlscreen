# Running nhlscreen under HController

[HController](https://github.com/Hydrxide/HController) is the hub that runs nhlscreen, restarts it if it crashes, and owns the LED panel. Its schedule decides when nhlscreen's frames are on the panel, and other apps can share the panel at other times. nhlscreen needs no code changes for this; it already meets HController's module contract.

This page covers:
- the setup to copy into HController's `hcontroller.yaml`, with each setting explained;
- the nhlscreen settings that have to agree with it;
- the variants: demo mode, running only during set hours, and running nhlscreen on another device.

HController's own [docs/modules.md](https://github.com/Hydrxide/HController/blob/main/docs/modules.md) has the general contract for any module.

## What HController uses

| nhlscreen provides | HController uses it for |
|---|---|
| `GET /health` | health status. It answers 200 whenever the server is up (see [Health](#health)). |
| `GET /render/frame` | the panel's frames: 128 × 64 raw RGB with `X-Width` / `X-Height`, fetched 10 times a second while nhlscreen is on the panel |
| `/` (the preview page) | the **Open** link on nhlscreen's dashboard card |
| `config.yaml`, `config.example.yaml` | the dashboard's config editor |
| its output | the dashboard's **Logs** view, and journald on the Pi |

## Setup

Install nhlscreen once, on the same machine as HController:

```bash
git clone https://github.com/Hydrxide/nhlscreen.git ~/nhlscreen && cd ~/nhlscreen
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml config.yaml    # optional: see below
```

`config.yaml` is optional. Without it, nhlscreen runs on its defaults, and HController's config editor shows `config.example.yaml` until you save, which creates `config.yaml`.

If `nhlscreen.service` was ever installed on this machine, turn it off. HController starts nhlscreen itself, and reports a **conflict** if another copy is already using port 9134:

```bash
sudo systemctl disable --now nhlscreen
```

## The module entry

Add this under `modules:` in `hcontroller.yaml`, then restart HController (it reads the file only at startup):

```yaml
modules:
  nhlscreen:
    name: NHL Scoreboard
    cwd: ~/nhlscreen
    command: [.venv/bin/python, -m, nhlscreen]
    url: http://127.0.0.1:9134
    health: /health
    ui: /
    display: {frame: /render/frame}
    config_file: config.yaml
    config_example: config.example.yaml
    process: always
    restart: on-failure
    stop_timeout: 10
    log_ignore: ['"GET /(render/frame|health) HTTP/[\d.]+" 200']
```

And in the `display:` section of the same file:

```yaml
display:
  width: 128             # must equal nhlscreen's display.matrix_width
  height: 64             # must equal nhlscreen's display.matrix_height
  default: nhlscreen     # shown whenever no schedule window applies
```

Why each setting:

| Setting | Why |
|---|---|
| `cwd: ~/nhlscreen` | nhlscreen looks for `./config.yaml` here, and saves your team choices to `nhlscreen-state.json` next to it |
| `command` | nhlscreen's own venv, so it doesn't depend on `PATH`; on the Pi, systemd's `PATH` is minimal. **Don't add `--config config.yaml`.** nhlscreen stops with an error if the file `--config` names doesn't exist, and it already uses `./config.yaml` when there is one. |
| `url: http://127.0.0.1:9134` | where HController reaches nhlscreen. The port must match `server.port` in nhlscreen's config (9134 by default). |
| `health: /health` | see [Health](#health) |
| `ui: /` | the **Open** link. It opens nhlscreen's preview page on port 9134 of whatever address you used to reach HController. |
| `display: {frame: /render/frame}` | lets nhlscreen go on the panel |
| `config_file`, `config_example` | the config editor. **Save & restart** applies a change straight away, and the previous file is kept as `config.yaml.bak`, which git ignores. |
| `process: always` | nhlscreen keeps polling the NHL API between games, so it's ready the moment a game starts; this costs very little. For running it only during set hours, see [that variant](#only-during-set-hours). |
| `restart: on-failure` | restarts it if it crashes, after waits of 1 s, 2 s, 4 s … up to 60 s |
| `stop_timeout: 10` | nhlscreen shuts down cleanly in well under a second; this is the limit before HController forces it |
| `log_ignore` | nhlscreen logs every request it serves, and HController asks for 10 frames a second. This drops the *successful* frame and health requests, so failures still show. nhlscreen's own lines, such as `screen -> …` every 5 to 10 seconds, are kept. |

## Keeping the two configs in step

| In nhlscreen's `config.yaml` | In `hcontroller.yaml` | Rule |
|---|---|---|
| `server.port` (9134) | the port in `url` | must match |
| `server.host` (`127.0.0.1`) | none | `127.0.0.1` is enough for HController. Set `0.0.0.0` so the **Open** link, the preview page, and `displayteam` from a laptop work from other devices. |
| `display.matrix_width`, `matrix_height` (128 × 64) | `display.width`, `display.height` | must match; otherwise HController rejects the frames and shows the error on the dashboard |
| `timezone` | `timezone` | separate settings: nhlscreen's controls the times shown on the board, HController's the schedule. Keep them the same. |

You can change nhlscreen's settings from HController's dashboard: open **Config file** on nhlscreen's card, edit, then **Save & restart**.

## Health

`/health` answers 200 whenever the server is running, even while the NHL API is slow or unreachable. That's deliberate. HController skips an unhealthy app and gives the panel to the next schedule window or the default. If nhlscreen reported unhealthy and it was the default, the panel would go dark instead of showing slightly old scores.

The response body, which HController ignores, still shows how fresh the data is (`updated_at`, `api_backoff_seconds`) and which games are being followed live (`tracked_games`).

## Scheduling

nhlscreen works best as the **default** (`display.default: nhlscreen`), with other apps in windows at times you don't usually watch games:

```yaml
display:
  default: nhlscreen
  schedule:
    - {module: off, days: daily, start: "01:00", end: "07:00"}        # panel dark overnight
    - {module: weather, days: weekdays, start: "07:00", end: "08:30"}
```

HController doesn't let apps take over the panel yet. Goal alerts, intermission screens and the live game only reach the panel while nhlscreen has it. If another app's window covers a game, that game won't appear on the panel until the window ends, although nhlscreen keeps following it.

## Variants

### Demo mode

To try the whole setup without a live game, run nhlscreen's replay instead. Build the demo game once:

```bash
cd ~/nhlscreen && .venv/bin/python tools/synth_replay.py --out replays/synthetic
```

Then use this `command` in the module entry:

```yaml
    command: [.venv/bin/python, -m, nhlscreen, --replay, replays/synthetic, --speed, "10"]
```

The numbers in `command` must be quoted, because it's a list of strings. Replay mode doesn't save team choices.

### Only during set hours

With `process: scheduled`, HController runs nhlscreen only around its schedule windows (and while it's the default with no window on, or picked with **Show now**). This saves memory and CPU between windows:

```yaml
    process: scheduled
    warmup_seconds: 90
```

- **Warm-up:** after each start, nhlscreen shows its idle screen while its first live data waits out the 25-second spoiler delay, for up to 90 seconds. `warmup_seconds: 90` starts it early enough that it's past this when its window begins.
- **Goal alerts:** it only alerts on goals scored after it started.
- **Team choices:** saved in `nhlscreen-state.json`, so they carry over between runs.

### nhlscreen on another device

If nhlscreen runs on a different machine, HController can still watch it and put it on the panel, but it doesn't start or stop it there. Give the entry a `url` and no `command`:

```yaml
modules:
  nhlscreen:
    name: NHL Scoreboard
    url: http://192.168.1.50:9134       # that machine's address
    health: /health
    ui: /
    display: {frame: /render/frame}
```

On that machine:
- **Listening:** set `server.host: 0.0.0.0` in nhlscreen's config.
- **Starting:** run nhlscreen under its own unit (`deploy/nhlscreen.service`, see the README's Raspberry Pi section).

While it's on the panel, frames use about 2 Mbit/s of network. The config editor and logs aren't available for an app on another device.

## Checking it

After restarting HController, open its dashboard (`http://<pi>:1630/`):

- **Status:** nhlscreen's card goes from **starting** to **healthy** within a few seconds.
- **Preview:** the dashboard's LED panel preview shows the same screen as nhlscreen's own preview page (`http://<pi>:9134/`). The clock may be a second apart.
- **Panel:** **Show now → NHL Scoreboard** puts it on the panel straight away.
- **Problems:**
  - **conflict:** another copy is running, such as `nhlscreen.service` or one started by hand.
  - **crashed:** **Logs** on the card shows nhlscreen's error.
  - **Frame size error under the preview:** `display.matrix_width` / `matrix_height` don't match HController's `display.width` / `height`.

## For nhlscreen development

HController relies on the following. Keep them working, or update HController and its `docs/modules.md` at the same time:

- `GET /render/frame`: status 200, `width × height × 3` raw RGB bytes, and `X-Width` / `X-Height` headers. It must render from the clock and nhlscreen's state, never from how often it's asked.
- `GET /health`: 2xx whenever nhlscreen can show something sensible.
- Running in the foreground, stopping cleanly on SIGTERM, and picking up `./config.yaml` without being told to.
- Drawing at full intensity. HController and its LED driver apply the brightness.
