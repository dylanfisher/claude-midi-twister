# Claude Midi Twister

**[Try the browser demo →](https://dylanfisher.github.io/claude-midi-twister/)**

Visualizes running Claude Code sessions on a DJTT Midi Fighter Twister — one
encoder per session. Color and animation show state (working, waiting on you,
errored, done), the ring shows turn length or context window fill, and
pressing an encoder brings that session's terminal tab to the front.

```
┌────┬────┬────┬────┐   red gate       wants permission
│ ●  │ ○  │ ◐  │    │   yellow flash   plan awaiting approval
├────┼────┼────┼────┤   red solid      errored
│ ◑  │    │ ●  │    │   amber breath   idle-waiting on you
├────┼────┼────┼────┤   orange fill    working (ring = turn length)
│    │    │    │    │   cyan sweep     thinking
├────┼────┼────┼────┤   green solid    finished, fading out
│    │    │ ◦  │ ◦  │   dim green      idle (ring = context window fill)
└────┴────┴────┴────┘   magenta        running unsupervised
  press → focus that tab       ◦ violet   subagents
  hold  → clear it off the board
  turn bottom-right → how much of the usage window is spent
```

Nothing on the device answers a prompt, approves a tool call, or blocks a
session — it only displays state. The one knob turn it reads is the
bottom-right one, which spells out the five-hour usage window on the whole
bank for a couple of seconds and then hands it back; the same word plays
itself on the way past 25/50/75/90/95/100% without being asked. 4 banks × 16
encoders supports 64 simultaneous sessions; only one bank of 16 is visible at
a time.

## Requirements

- macOS
- Python 3.14
- A DJTT Midi Fighter Twister
- The Midi Fighter Utility, to set encoders to accept host LED control and
  turn off the device's own sleep timer (it dims on its own otherwise)

## Install

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python install_hooks.py --print     # preview what it will add
.venv/bin/python install_hooks.py             # merge into ~/.claude/settings.json
```

This installs hooks into your **user** settings, so every project gets the
visualizer. It backs up your settings first and tags its own entries, so
`--uninstall` removes exactly what it added. Run `--check` after a `git pull`
to catch drift between the code and your installed hooks.

## Run

```sh
.venv/bin/python -m mft.daemon
```

Leave it running. Open Claude Code anywhere and its encoder lights up.
Sessions already running when the daemon starts are adopted automatically.

```sh
.venv/bin/python -m mft.daemon --status     # exit 0 if running
.venv/bin/python -m mft.daemon --stop
curl -s localhost:7654/status | python3 -m json.tool   # who owns which encoder
```

Developing without hardware:

```sh
.venv/bin/python -m mft.daemon --no-device
.venv/bin/python -m mft.simulate --sessions 6          # fake sessions, no Claude needed
```

### Or: the app

```sh
.venv/bin/python app/make_app.py     # -> ~/Applications/Claude Twister.app
```

Launch it from Spotlight. It's a background app with no dock icon or window;
launch once to start the daemon, again to stop it. Logs go to
`~/Library/Logs/claude-twister.log`. Rebuild it after moving the repo or
upgrading Python, since it bakes in absolute paths.

To start at login, drop a launchd plist in `~/Library/LaunchAgents` pointing
at `.venv/bin/claude-twister -m mft.daemon`.

## How it works

```
Claude Code session ──hook──▶ POST localhost:7654/event ──▶ daemon ──MIDI──▶ Twister
       ▲                                                      │
       └──────────── AppleScript / tmux / wezterm ◀───────────┘  (encoder press)
```

Claude Code hooks POST session events to a local HTTP daemon, which infers
session state from those events and renders it to the Twister at 30Hz.
Pressing an encoder raises the matching terminal tab via AppleScript, tmux, or
similar, depending on the terminal.

Hooks always exit `0`. If the daemon isn't running, the POST fails silently
and you just get no lights — nothing is printed, nothing is blocked, no
session is ever slowed down.

## Configuration

Every tunable lives in `mft/config.py` and is overridable with an `MFT_*`
env var — colors, timings, animation curves, which port the daemon listens
on. See the file for the full list.

## Development

```sh
.venv/bin/python -m unittest discover -s tests     # run the test suite
.venv/bin/python -m mft.simulate --sessions 6      # fake sessions, no hardware
.venv/bin/python -m mft.calibrate colors|white|anim|ring|ramp|dark|banks
open demo/index.html                               # Web MIDI bench, no daemon
```

See `CLAUDE.md` for the module layout and design invariants.
