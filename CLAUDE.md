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
.venv/bin/python -m mft.calibrate colors|white|anim|ring|ramp|dark

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
hooks/notify.sh, hooks/register_session.py  ──POST /event──►  mft/daemon.py
                                                                 │
        mft/state.py  (SessionTable: slots, states, TTLs)  ◄──────┤
        mft/render.py (Session + clock -> one Cell)               │
        mft/board.py  (64 Cells + overlays + arbitration)         │
        mft/twister.py (CC writes, de-duplicated)  ───────────────┘
        mft/focus.py  (encoder press -> raise a terminal tab)
        mft/tab.py    (state glyph -> that tab's title, via OSC on its tty)
```

- `config.py` — every tunable, all env-overridable (`MFT_*`). Colours,
  animation bands, timings, priorities. Nothing magic lives outside it.
- `state.py` — pure session bookkeeping. States (`permission`, `plan`, `error`,
  `waiting`, `streaming`, `working`, `thinking`, `done`, `idle`, `ended`) are
  *inferred* from gaps between hook events; nothing reports them.
- `render.py` — pure `(session, clock) -> Cell`. One session's appearance.
- `board.py` — everything only decidable across the whole board: motion
  arbitration, the subagent stack, overlays, ambient field.
- `context.py` — context-window fullness and Claude's own generated title, both
  tailed out of `transcript_path`.
- `tab.py` — the board's second display: a state glyph prefixed to the terminal
  tab's title. Deliberately coarser than `render.py` (the three busy states
  share a glyph) because every change is a write down someone's tty; see the
  README section.
- `discover.py` — adopt sessions that predate the daemon (transcripts joined
  with the process table).
- `font.py` — 4×4 bitmap alphabet; a bank is a 16-pixel display.

The daemon does all I/O; `state`/`render`/`board`/`font` are pure and that is
what makes them testable. Keep it that way — if a new feature wants a
subprocess or a socket, it belongs in `daemon.py`, `focus.py` or `tab.py`.
Those last two are the only places that touch something outside this process,
and `tab.py` is the only one that *writes* there — a tty it does not own, which
is why the write is short, non-blocking, and handed back when the session ends.

## Invariants

These are load-bearing. Breaking one is a design change, not a refactor.

1. **The device is a display, never a control surface.** Nothing on the hardware
   may answer a prompt, approve a tool call, or block a session. Encoder press
   raises a terminal and nothing else; knob turns are ignored entirely.
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
- New hook events: add to `install_hooks.py`, handle in `daemon.handle_event`,
  and remember `--check` exists precisely because code and installed settings
  drift silently.
- Tests are `unittest`, no fixtures framework, and `tests/test_state.py` is
  where most behaviour is pinned. Hardware and HTTP aren't tested; purity is.
