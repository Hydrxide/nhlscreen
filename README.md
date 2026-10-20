# nhlscreen

> This project is not actively maintained, and was created with AI assistance for personal use.

A DIY NHL scoreboard for a 128 × 64 LED panel. It follows your teams through the NHL's public web API and shows their live games (score, clock, power plays, shots), goal alerts with the scorer and assists, final scores, the league scoreboard, standings, stat leaders and player cards.

It runs on a Mac, a Linux PC, a Windows PC or a Raspberry Pi. A **browser preview** shows exactly what the panel shows, so you don't need any LED hardware to use it. On the Pi with the panel, [HController](https://github.com/Hydrxide/HController) runs nhlscreen and puts it on the panel; see [Put it on the LED panel](#put-it-on-the-led-panel).

This README is a setup guide. How it works inside, the API and the screen format are in [docs/reference.md](docs/reference.md).

**Contents:** [What you need](#what-you-need) · [1. Install](#1-install) · [2. Run it](#2-run-it) · [3. Pick your teams and players](#3-pick-your-teams-and-players) · [Open it from your phone](#open-it-from-your-phone) · [Put it on the LED panel](#put-it-on-the-led-panel) · [Start it at boot without HController (Linux)](#start-it-at-boot-without-hcontroller-linux) · [What the board shows](#what-the-board-shows) · [Troubleshooting](#troubleshooting) · [For developers](#for-developers) · [License](#license)

## What you need

- **Python 3.11 or newer** and **git**.

| | Install Python and git | Check |
|---|---|---|
| **Raspberry Pi OS, Debian, Ubuntu** | `sudo apt update && sudo apt install -y git python3 python3-venv` | `python3 --version` |
| **macOS** | `xcode-select --install` for git. Python from [python.org](https://www.python.org/downloads/macos/) or `brew install python@3.13` | `python3 --version` |
| **Windows** | `winget install Git.Git` and `winget install Python.Python.3.13` in PowerShell, or the installers from [git-scm.com](https://git-scm.com/download/win) and [python.org](https://www.python.org/downloads/windows/). Then open a new PowerShell window. | `py --version` |

If `python3 --version` shows something older than 3.11, call the newer Python by its full name below, for example `python3.13 -m venv .venv`. The `python3` that comes with macOS is 3.9, and Ubuntu 22.04's is 3.10; both are too old.

## 1. Install

**macOS, Linux and the Pi:**

```bash
cd ~
git clone https://github.com/Hydrxide/nhlscreen.git
cd nhlscreen
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.yaml config.yaml
```

**Windows (PowerShell):**

```powershell
cd ~
git clone https://github.com/Hydrxide/nhlscreen.git
cd nhlscreen
py -m venv .venv
.venv\Scripts\pip install -e .
copy config.example.yaml config.yaml
```

Use PowerShell rather than the old Command Prompt for the Windows commands in this guide. There's no need to "activate" the venv: the commands call its Python directly.

`config.yaml` is optional: without it nhlscreen follows the Toronto Maple Leafs, in Toronto's timezone. Open it in any text editor now and set at least:

```yaml
favorite_teams: [EDM]          # one or more team codes; `displayteam --list` shows them all
timezone: America/Edmonton     # the times the board shows (an IANA name, e.g. America/New_York)
```

Every other setting is explained in `config.example.yaml`. nhlscreen reads the file only at startup, so restart it after a change.

## 2. Run it

**macOS, Linux and the Pi:**

```bash
cd ~/nhlscreen
.venv/bin/python -m nhlscreen
```

**Windows:**

```powershell
cd ~\nhlscreen
.venv\Scripts\python.exe -m nhlscreen
```

Then open **http://127.0.0.1:9134/** in a browser on the same machine. It keeps running until you press **Ctrl+C**.

**Check it's working:** the page's LED preview first shows an idle screen (NHL and the time). Within 90 seconds it switches to the board: your team's live game if it's playing, otherwise the rotation of next game, scoreboard, standings, leaders and player cards. The wait is the [anti-spoiler delay](#anti-spoiler-delay) on the first live data.

The page has the LED preview, **Show now** buttons and favorite players on the left, and the team picker, recent events and a collapsible **Screen JSON** debug panel on the right.

### A demo game

No game on? Replay one at 10× speed (a full game in about 7 minutes). Build the demo once (about 17 MB), then run it instead of the live board; both use port 9134, so stop the other first.

```bash
.venv/bin/python tools/synth_replay.py --out replays/synthetic
.venv/bin/python -m nhlscreen --replay replays/synthetic --speed 10
```

```powershell
.venv\Scripts\python.exe tools\synth_replay.py --out replays\synthetic
.venv\Scripts\python.exe -m nhlscreen --replay replays\synthetic --speed 10
```

Replays don't save team or player choices.

## 3. Pick your teams and players

`config.yaml` holds the defaults. You can change the teams and players while it runs, from the browser or a terminal. Those choices are saved to `nhlscreen-state.json` next to `config.yaml`, win over the config file through restarts, and go away with **Reset to config** (or `displayteam --reset`).

### Teams, from the browser

Tap a team on the preview page. It shows all 32 teams grouped by division in their colors. A **LIVE** badge marks teams playing right now, and teams with a game still to come show its start time. Tick **Follow several** to pick more than one team. Every open copy of the page stays in sync.

If the new team is playing, the board jumps to its live game. A game you switch into partway through doesn't replay old goal alerts; alerts start with the next goal.

### Players, from the browser

The **Favorite players** box has two fields. Type a player ID, or paste the player's nhl.com link (`nhl.com/player/gavin-mckenna-8486067`), and press **Save players**. Each ID is checked with the NHL before saving, so a typo gets an error instead of a blank card. Leave a field empty to feature one player.

### Teams, from a terminal: `displayteam`

```bash
.venv/bin/displayteam              # the current team and what's on screen
.venv/bin/displayteam EDM          # follow the Oilers (nicknames work too: oilers)
.venv/bin/displayteam TOR EDM      # follow several teams; their live games alternate
.venv/bin/displayteam --reset      # back to favorite_teams from config.yaml
.venv/bin/displayteam --list       # all team codes
```

On Windows, use `.venv\Scripts\displayteam` in place of `.venv/bin/displayteam`.

It talks to the board at `$NHLSCREEN_URL` (default `http://127.0.0.1:9134`), so it can switch a board on another machine:

```bash
export NHLSCREEN_URL=http://raspberrypi.local:9134        # macOS / Linux
```

```powershell
$env:NHLSCREEN_URL = "http://raspberrypi.local:9134"      # Windows
```

To type plain `displayteam` on the Pi or Linux, link it into `~/.local/bin`, which Raspberry Pi OS puts on your `PATH` (open a new shell afterwards):

```bash
mkdir -p ~/.local/bin && ln -sf ~/nhlscreen/.venv/bin/displayteam ~/.local/bin/displayteam
```

### Show now

Under the board preview, **Show now** has a button for each screen: next game, scoreboard, division, wild card, league standings, leaders, player cards, goal alert and final score. A press plays that screen next (every page of it, each for its normal time, even during a live game), then the rotation starts over from the top. **Goal alert** and **Final score** replay your team's most recent real game. Buttons with nothing to show yet are greyed out.

## Open it from your phone

By default nhlscreen only answers on the machine it runs on. To open the preview page from a phone or another computer, set this in `config.yaml` and restart it:

```yaml
server:
  host: 0.0.0.0
```

Then open `http://<machine>:9134/`, where `<machine>` is its name (`raspberrypi.local` on a Pi) or its IP address:

| | Find the IP address |
|---|---|
| Pi / Linux | `hostname -I` |
| macOS | `ipconfig getifaddr en0` |
| Windows | `ipconfig`, the **IPv4 Address** line |

The first time it starts this way, Windows asks whether to allow Python through the firewall: allow it on **private** networks. macOS asks the same only if its firewall is on.

There's no login, so anyone on your Wi-Fi can switch teams. Bookmark the page or add it to your phone's home screen.

## Put it on the LED panel

On the Pi, [HController](https://github.com/Hydrxide/HController) runs nhlscreen, restarts it if it crashes, and owns the LED panel: its schedule decides when nhlscreen is on the panel, and other apps can share the panel at other times. HController also works on a Mac or PC for testing, with its dashboard preview in place of the panel.

1. Set up HController with its README.
2. Install nhlscreen next to it ([step 1](#1-install) above).
3. Register it in HController's config, with **[docs/hcontroller.md](docs/hcontroller.md)**: the block to paste, what each setting does, and the settings that must agree between the two.

## Start it at boot without HController (Linux)

Only do this if HController **isn't** running nhlscreen: two copies would compete for port 9134, and HController would report a conflict.

On a Pi, or any Linux machine with systemd:

```bash
cd ~/nhlscreen
sed "s#/home/pi#$HOME#g; s#^User=pi#User=$USER#" deploy/nhlscreen.service | sudo tee /etc/systemd/system/nhlscreen.service > /dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now nhlscreen
journalctl -u nhlscreen -f          # its output; Ctrl+C stops watching
```

The `sed` fills in your username and home folder, since the unit file is written for a user called `pi`. The service needs `config.yaml` to exist. Restart it after editing the config: `sudo systemctl restart nhlscreen`.

On macOS and Windows there's no service: run it in a terminal ([step 2](#2-run-it)) while you use it.

## What the board shows

On startup the board holds a plain idle screen (NHL + the time) until the first live data has waited out the 25s spoiler delay, so it never opens on out-of-date screens; it stops waiting after 90 seconds.

Screens are chosen in priority order:

1. **Alerts** for favorite-team goals, overturned goals and finals. Goal and no-goal alerts hold for `alert_seconds` (25s), finals for `final_alert_seconds` (60s). The NHL often posts a goal before its assists; a goal alert on screen fills them in (and any scorer correction) as they arrive. A player's third goal of the game is titled **HAT TRICK!** (in the scoring team's color) instead of TOR GOAL!, and a goal scored by a goalie is titled **GOALIE GOAL!**. When the 1st or 2nd period ends (or the 3rd / an OT ends tied, so the game goes to OT or a shootout), an **intermission screen** holds for `intermission_alert_seconds` (20s): the live game layout with `INT` in place of the clock, the score, and SOG / PP / FO.
2. **Live game** whenever a favorite team is playing. The rotation resumes during intermissions (configurable).
3. **Rotation**: next game and last result, the league scoreboard (paged), standings, stat leaders, and favorite player cards. Standings come in three kinds, each a separate rotation entry in `config.yaml`:
   - **Division:** only divisions that contain a followed team.
   - **Wild card:** the NHL playoff format for each conference you follow: every division's top 3, then the wild card spots, a cut line and the chasers.
   - **League:** all 32 teams across eight pages of 4, 5 seconds each, in larger text: points, record and streak (no games played column).

   Followed teams are highlighted with a grey bar in every table.

On live game screens and goal alerts, the home team is in its color and the away team in white (the jersey convention), so the two sides never look alike. Everywhere else each team has its own color.

### Anti-spoiler delay

TV and streaming broadcasts run behind real time, so a board fed straight from the NHL would flash goals before you see them. Live game data is always shown **25 seconds** after the NHL produced it: the live clock, scores, goal and no-goal alerts, and final scores. It isn't adjustable: 25s is longer than the NHL's ~20s update interval, which is what lets the board run the clock smoothly (see [docs/reference.md](docs/reference.md#the-live-clock)). Standings, schedules, leaders and player cards aren't delayed. In replays the delay is in game seconds, so it shrinks with `--speed` like everything else.

## Troubleshooting

| What you see | What to do |
|---|---|
| `ERROR: Package 'nhlscreen' requires a different Python` | The venv was made with Python older than 3.11. Delete the `.venv` folder and make it again with a newer Python (see [What you need](#what-you-need)). |
| `error while attempting to bind on address ('127.0.0.1', 9134)` | Another copy is running on port 9134: another terminal, the demo, its systemd service, or HController. Stop it first, or run this one on another port with `--port 9135`. |
| `FileNotFoundError` at startup | `--config` names a file that doesn't exist. Leave `--config` out to use `./config.yaml` when there is one, or the defaults. |
| The preview stays on the idle screen | Normal for up to 90 seconds after starting. If it stays longer, the terminal shows the NHL API errors; the board keeps retrying. |
| Times on the board are wrong | Set `timezone` in `config.yaml` and restart. |
| A change in `config.yaml` didn't take effect | Restart nhlscreen. For teams and players, also press **Reset to config** on the page: choices made there win over the file. |
| The page doesn't load from a phone | Set `server.host: 0.0.0.0` and restart, allow the firewall prompt, and check both are on the same network (see [Open it from your phone](#open-it-from-your-phone)). |
| `pip install -e '.[dev]'` fails on Windows | Run it in PowerShell, not Command Prompt, which doesn't understand the single quotes. |

## For developers

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                                          # unit + end-to-end tests, offline, ~6 s
.venv/bin/python tools/render_previews.py --out previews/    # every screen type as an LED-style PNG
.venv/bin/python tools/record_game.py --team TOR --out recordings/tor-game   # record a real game (start before puck drop)
.venv/bin/python -m nhlscreen --replay recordings/tor-game   # and replay it
```

On Windows, use `.venv\Scripts\` in place of `.venv/bin/`.

Replay mode feeds recorded responses through the real client, poller, detector and scheduler; only the HTTP transport is swapped.

- **[docs/reference.md](docs/reference.md):** the architecture, the screen format, colors, the live clock, the LED renderer, the API, polling and rate limits, and the code layout.
- **[docs/hcontroller.md](docs/hcontroller.md):** running under HController, and what HController relies on.
- **`CLAUDE.md`:** working on this with Claude Code: the display decisions made so far, and next steps.

## License

MIT; see [LICENSE](LICENSE).

nhlscreen is a fan project. It isn't affiliated with or endorsed by the NHL or its teams; NHL and the team names are trademarks of their owners. Game data comes from the NHL's public web API, which is unofficial and undocumented, and the test fixtures are responses recorded from it.
