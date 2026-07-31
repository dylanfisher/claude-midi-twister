# claude-midi-twister

A daemon that visualizes running Claude Code sessions on a DJTT Midi Fighter
Twister: one encoder per session, press to focus that session's terminal tab.
Python 3.14, stdlib only except `mido` + `python-rtmidi`. macOS-only in practice
(focus adapters and the app bundle are AppleScript/`open`).

Read `README.md` first — it is the design document, not a quickstart, and most
"why is it like this" questions are answered there at length.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m unittest discover -s tests     # the whole suite
.venv/bin/python -m unittest tests.test_state      # one module

.venv/bin/python -m mft.daemon                     # run it (needs hardware)
.venv/bin/python -m mft.daemon --no-device         # run it without hardware
.venv/bin/python -m mft.daemon --status | --stop | --discover
.venv/bin/python -m mft.simulate --sessions 6      # fake sessions, no Claude needed
.venv/bin/python -m mft.calibrate colors|white|anim|ring|ramp|dark|banks
open demo/index.html                               # Web MIDI bench, no daemon
                                                   #   (stop the daemon first)

curl -s localhost:7654/status | python3 -m json.tool
.venv/bin/python install_hooks.py --print|--check|--uninstall
.venv/bin/python app/make_app.py                   # -> ~/Applications/Claude Twister.app
                                                   #    + .venv/bin/claude-twister
```

Always use `.venv/bin/python`, never bare `python3` — the venv holds the MIDI
deps. `.venv/bin/claude-twister` is that same interpreter under a name Activity
Monitor can show; the app bundle runs the daemon through it, and so can you.
There is no linter, formatter, or type checker configured; match the
surrounding code by hand.

### Developing without a Twister

`--no-device` plus `mft.simulate` exercises everything but the wire. That is the
default way to work here: hardware is only needed to judge *how a thing looks*,
never to know whether it runs. Logs go to stderr, or
`~/Library/Logs/claude-twister.log` under the app bundle.

## Architecture

Hooks (installed into **user** `~/.claude/settings.json`) fire and forget HTTP
POSTs at one long-lived daemon, which owns the MIDI port and renders at 30Hz.

```
hooks/notify.sh, hooks/register_session.py
              │
      POST ───┴──► mft/httpd.py ──► mft/daemon.py (Visualizer + render loop)
                                          │
   identity.py ─► state.py ─► events.py   │  what is on the board
                     ▲                    │
   render.py ─► board.py ─► overlays.py   │  what it looks like
                                          │
   twister.py ◄───────────────────────────┘  what goes on the wire
   focus.py · tab.py · power.py · upkeep.py   what touches the outside
   attention.py                               what the outside is doing
```

Every module is one job, and the filename is the job. Start at `daemon.py` for
"when does this happen", `board.py` for "what does the board look like",
`state.py` for "who owns which encoder".

**What is on the board**

- `state.py` — `Session` (one agent's record) and `SessionTable` (which encoder
  it is on). Slot allocation and the repairs that keep one tab on exactly one
  encoder. Pure.
- `events.py` — one hook payload folded into one session. States (`permission`,
  `plan`, `error`, `waiting`, `streaming`, `working`, `thinking`, `done`,
  `idle`, `ended`) are *inferred* from gaps between events; nothing reports
  them. Pure.
- `identity.py` — what names a terminal tab, and how well. Invariant 3 in one
  file. Pure, and knows nothing about sessions.
- `context.py` — context-window fullness and Claude's own generated title, both
  tailed out of `transcript_path`.
- `usage.py` — the other limit: how much of the five-hour usage window is spent,
  read out of Claude Code's own `~/.claude.json` cache. Belongs to no session,
  so it gets no encoder — it gets the whole bank for a few seconds on
  25/50/75/90/95/100% and nothing in between, once each per window. What that
  looks like is `overlays.UsageOverlay`: the word `USE`, then the reading as
  rows filling from the bottom. The same animation is also *askable* — a turn
  of the bottom-right knob, debounced so one flick is one readout — and that
  path is sterile by design: it reads the file and paints, and never moves the
  watermark, so looking can't swallow a milestone.

**What it looks like**

- `render.py` — pure `(session, clock) -> Cell`. One session's appearance.
- `board.py` — everything only decidable across the whole board: motion
  arbitration, the subagent stack, sleep, the grid walks, `compose`. Pure.
- `overlays.py` — the transient gestures painted over that board: boot unwrap,
  spawn strike, `/clear` wipe, compaction, hold-to-clear fuse, usage
  announcement, shutdown spiral. A new
  animation is a class here plus one line in `Visualizer._apply_effects`. Pure.
- `font.py` — 4×4 bitmap alphabet; a bank is a 16-pixel display.
- `config.py` — every tunable, all env-overridable (`MFT_*`). Colors,
  animation bands, timings, priorities. Nothing magic lives outside it.

**What touches the outside**

- `twister.py` — the MIDI port, one method per LED feature, writes de-duplicated.
- `focus.py` — encoder press → raise a terminal tab. Adding a terminal is one
  adapter.
- `attention.py` — the same arrow reversed: which tab is in front of *you*, so
  the focused encoder can hold its ring at full. Two layers, and the split is
  the whole design — the window server names the frontmost app for free
  (`ctypes`, no permission), and an AppleScript names the tab inside it for
  ~80ms, which is only ever spent when one terminal holds two sessions. Refuses
  to guess far more readily than it answers; tmux is the documented gap.
- `tab.py` — the board's second display: a state glyph prefixed to the tab's
  title, written as OSC down that session's tty. `TabStrip` decides when.
  Deliberately coarser than `render.py` (the three busy states share a glyph)
  because every change is a write down someone's tty; see the README section.
- `discover.py` — how to ask the process table things: adopt sessions that
  predate the daemon (transcripts joined with the process table), and `orphans`,
  which releases the encoders whose recorded pid no longer exists.
- `upkeep.py` — *when* to ask, on which thread, and what to do with the answer.
  Three clocks: adoption at boot/wake, the free pid sweep every reap, the `ps`
  census on its own thread.
- `power.py` — system sleep and wake, over `ctypes` into IOKit, plus the
  display's power state out of CoreGraphics. The board follows the screen, and
  that poll is the detector that carries the weight: the IOKit notification can
  stop being delivered, and on a Mac that dark-wakes for maintenance it does.
  Load-bearing fact, documented at length there and in the README:
  `time.monotonic()` on macOS does not advance while the machine is asleep, so
  every deadline in the daemon pauses with it and a suspend is invisible from
  inside the loop. That is the behaviour you want and the reason this module has
  to exist.
- `banks.py` — which sixteen encoders the front panel shows, and the cooldown on
  moving it. The one non-painting write to the device (invariant 1).

**The process**

- `httpd.py` — the socket the hooks arrive on, and the bodiless 204 they always
  get (invariant 2). Knows nothing about sessions.
- `cli.py` — argv, signals, and the order of a clean shutdown. `--status`,
  `--stop`, `--discover`.
- `pidfile.py` — whether a daemon is already running.
- `status.py` — what `GET /status` says, as one pure function.

`state`/`events`/`identity`/`render`/`board`/`overlays`/`font` are pure and that
is what makes them testable. Keep it that way — if a new feature wants a
subprocess, a socket or a framework, it belongs in `daemon.py`, `httpd.py`,
`focus.py`, `tab.py`, `upkeep.py` or `power.py`. Those are the only places that
touch something outside this process, and `tab.py` is the only one that *writes*
there — a tty it does not own, which is why the write is short, non-blocking,
and handed back when the session ends. `power.py` is the only one something
outside calls *in* to, which is why its callbacks are quick, never raise, and
always consent.

## Invariants

These are load-bearing. Breaking one is a design change, not a refactor.

1. **The device is a display, never a control surface.** Nothing on the hardware
   may answer a prompt, approve a tool call, or block a session. Encoder press
   raises a terminal; a hold takes that encoder's session off the *board* and
   never touches the agent, which goes on running and re-claims a knob with its
   next event. Every knob turn is ignored but one: the bottom-right encoder of
   the current bank (`USAGE_PEEK_ENCODER`) asks for the usage window and gets
   the announcement painted over the bank.
   That turn and the bank select in `_follow_alerts` are the two inputs that
   aren't a press, and neither is an exception: both change only what you are
   looking at, never a session, and both are cooldowned so a knob leaned on
   can't become a stream. Anything else that wants a non-painting read or write
   of the device needs the same two arguments.
2. **No hook can block or slow a session.** Hooks post and exit 0 whatever
   happens; the daemon answers every event with a bodiless 204 and never puts a
   body on the wire. A dead daemon costs a failed connect and nothing more.
3. **Encoders belong to terminals, not sessions.** Slot identity comes from
   `state.terminal_keys`, so `/clear` keeps its encoder — and **one terminal
   never holds two encoders**. Every hook carries what it can of the tab's
   identity, records that turn out to describe the same tab merge onto the older
   encoder, and `SessionTable.reconcile` re-checks that from outside once every
   reap. Anything that creates or rekeys a session goes through
   `SessionTable.ensure`. See the README section.
4. **One fast animation on the board at a time** (`board.arbitrate_motion`),
   always on the encoder where a human is blocking. Motion is a budget.
5. **Overlays are pure paint.** They never mutate session state, so one dropped
   mid-flight leaves nothing to tear down.
6. **Bias toward showing too little.** A missing encoder is a session you find
   in a moment; a phantom one is a knob that lies for an hour.

## Hardware gotchas

The MIDI channel layout is stable but the value tables are not, and getting one
wrong fails silently. They live in `mft/CLAUDE.md`, which loads whenever you
work under `mft/` — read it before touching `twister.py`, `config.py`, or
`calibrate.py`.

## Conventions

- **Module docstrings carry the reasoning.** Every file opens with prose
  explaining why it exists and what the non-obvious decisions were, including
  the approach that was tried and abandoned. New modules do the same; new
  constants get `#:` comments saying what changing them costs.
- **Commit messages are declarative prose about the behaviour**, not
  conventional-commit prefixes — "Dim at half an hour, dark at an hour, unless
  it's asking for you". Sentence case, no scope tags.
- `from __future__ import annotations` at the top of every module.
- New hook events: add to `install_hooks.py`, fold into a session in
  `events.apply_event`, and route in `daemon.Visualizer._apply_hook_event` only
  if the event needs something other than the default (a subagent's parent, an
  update-only rule, a compaction). Remember `--check` exists precisely because
  code and installed settings drift silently.
- Tests are `unittest`, no fixtures framework, and `tests/test_state.py` is
  where most behaviour is pinned. Hardware and HTTP aren't tested; purity is.
- **Bump `?v=` on every change under `site/`** — one shared number across every
  asset reference, raised in the same commit. The page ships to GitHub Pages
  with no build step, so nothing fingerprints the filenames and a partial bump
  is worse than none. The exact references and the grep are in `site/CLAUDE.md`,
  which loads whenever you work under `site/`.
