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

**What it looks like**

- `render.py` — pure `(session, clock) -> Cell`. One session's appearance.
- `board.py` — everything only decidable across the whole board: motion
  arbitration, the subagent stack, sleep, the grid walks, `compose`. Pure.
- `overlays.py` — the transient gestures painted over that board: boot word,
  spawn strike, `/clear` wipe, compaction, peek, shutdown spiral. A new
  animation is a class here plus one line in `Visualizer._apply_effects`. Pure.
- `font.py` — 4×4 bitmap alphabet; a bank is a 16-pixel display.
- `config.py` — every tunable, all env-overridable (`MFT_*`). Colours,
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
   raises a terminal and nothing else; knob turns are ignored entirely.
   The one write that isn't paint is the bank select in `_follow_alerts`, and it
   is not an exception: it changes which sixteen encoders you are looking at,
   never a session. Anything else that wants to write to the device for a
   non-painting reason needs the same argument and the same cooldown.
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

The MIDI channel layout is documented and stable; the value tables are not.

- Channel 1 ring position, 2 RGB hue + press input, 3 RGB animation *or*
  brightness, 4 banks/side buttons, 6 ring animation *or* brightness.
- On channels 3 and 6 the low band is rate-based animations and the band above
  is a brightness ramp — a lit encoder can animate or dim, not both.
- **Off is 18** (`config.DARK_VALUE`) on channel 3, not 0 (which means "no
  animation") and not 17 (which is the *slowest pulse* — and since the daemon
  supplies the MIDI clock, all sixteen breathe in unison). The ring's floor is
  17 (`config.RING_DARK_VALUE`) — the two ramps do not line up.
- Per-unit drift goes through `mft.calibrate` and then into `config.py` or an
  `MFT_*` env var. Don't hardcode a number a sweep found.
- Encoders must be set to accept host LED control in the Midi Fighter Utility,
  or the device drives its own LEDs and ignores everything sent.

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
