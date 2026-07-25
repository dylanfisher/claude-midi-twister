# claude-midi-twister

Your running Claude Code sessions, one per encoder, on a DJTT Midi Fighter Twister.
Each session claims an encoder: the RGB under the knob says what state it's in,
the LED ring says how full its context window is, and **pressing the encoder
brings that session's terminal tab to the front**.

```
┌────┬────┬────┬────┐   ● red gate      wants permission — a human is blocking
│ ●  │ ○  │ ◐  │    │   ● yellow flash  a plan is written and wants a yes
├────┼────┼────┼────┤   ● red solid     errored (rate limit, overload, billing)
│ ◑  │    │ ●  │    │   ● amber breath  idle-waiting on you
├────┼────┼────┼────┤   ● orange fill   working — the ring is its context window
│    │    │    │    │   ● cyan sweep    thinking
├────┼────┼────┼────┤   ● green solid   finished, then fading out
│    │    │ ◦  │ ◦  │   ● dim green     idle, and the resting state
└────┴────┴────┴────┘   ● magenta       running unsupervised
  press → focus that tab       ◦ violet dim   subagents, stacked from the corner
  hold  → peek at its history
```

Nothing on the device can answer a prompt, approve a tool call, or block a
session. It reports; you decide, in the terminal.

4 banks × 16 encoders = 64 simultaneous sessions, which is more terminals than
you have.

## Install

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python install_hooks.py --print     # look at what it will add
.venv/bin/python install_hooks.py             # merge into ~/.claude/settings.json
```

It backs up your settings first and tags its own entries, so `--uninstall`
removes exactly what it added and leaves your other hooks alone. It writes to
*user* settings, so every project gets the visualizer. Run `--check` after a
`git pull`: a hook that was added to the code and never installed is invisible
rather than broken.

Then, in the Midi Fighter Utility, set the encoders to accept host LED control —
otherwise the device drives its own LEDs and ignores you.

## Run

```sh
.venv/bin/python -m mft.daemon
```

Leave it running. Open Claude Code anywhere and its encoder lights up. Sessions
you already had open when you started the daemon are adopted automatically (see
[Sessions that predate the daemon](#sessions-that-predate-the-daemon)).

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

Cmd-Space, type "Claude Twister", enter. It toggles: launch once to start the
daemon, again to stop it. It's a background app with no dock icon or window, so
it reports back with a notification — *Running — Twister connected*, or *no
Twister found, check it's plugged in*. Logs go to
`~/Library/Logs/claude-twister.log`.

Rebuild it after moving the repo — it bakes in absolute paths. If Spotlight
can't find it, use `--dest /Applications`, which is always indexed.

To start at login instead, drop a launchd plist in `~/Library/LaunchAgents`
pointing at `.venv/bin/python -m mft.daemon`.

## How it works

```
Claude Code session ──hook──▶ POST localhost:7654/event ──▶ daemon ──MIDI──▶ Twister
       ▲                                                      │
       └──────────── AppleScript / tmux / wezterm ◀───────────┘  (encoder press)
```

Hooks post and exit `0` no matter what. With the daemon down the POST is
refused, the hook still exits `0`, and you simply have no lights — nothing is
printed, nothing is blocked. Every hook has a 5s timeout and the `curl` inside
it a 2s one, so a wedged daemon can't hold a session up either.

State is *inferred* from the gaps between events, because hooks are discrete
notifications rather than a telemetry stream:

| Hook | State |
|---|---|
| `SessionStart` | claim an encoder, `idle` |
| `UserPromptSubmit` | `thinking`, new turn, reset counters |
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | `working`, re-read the context gauge |
| `Notification` | `permission` / `plan` / `waiting` / `done`, by `notification_type` |
| `SubagentStart` / `SubagentStop` | stack up from the bottom-right of the bank |
| `PreCompact` / `PostCompact` | drain the ring, then refill it |
| `StopFailure` | `error`, and spell the reason if it's a rate limit |
| `Stop` | `done`, fades out over two minutes, then ramps back if ignored |
| `SessionEnd` | release the encoder — unless the reason is `clear` |
| `MessageDisplay` | `streaming` (opt-in, below) |

`SessionEnd` is advisory — a crashed terminal never fires it — so slots also
expire on a one-hour TTL, and that reaper is what actually keeps the board
honest.

`PermissionRequest` is deliberately **not** installed: an HTTP hook on it sits
between Claude Code and its own permission prompt, where a visualizer has no
business being able to delay — let alone answer — a permission. `Notification`
already reports permission prompts after the fact.

Two opt-ins:

- `--with-message-display` adds `MessageDisplay`, a live pulse while assistant
  text streams, at the cost of sitting in Claude Code's render path.
- `--http-hooks` uses `type: "http"` hooks — no process spawn, but Claude Code
  prints every failed HTTP hook to the user, so a stopped daemon means two
  `ECONNREFUSED` lines under every tool call. They also can't carry the terminal
  identity `notify.sh` sends (below), so slot identity falls back to the two
  hooks that read the environment. Worth it only if your daemon is always up.

## Encoders belong to terminals, not to sessions

A `/clear` hands you a **new `session_id` in the same tab**. Key slots on the
session id and every `/clear` teleports your agent to a different knob.

So the durable identity is the terminal — `TMUX_PANE`, the iTerm session GUID,
the tty, and failing all of those the `claude` process id. When a familiar tab
shows up with an unfamiliar session id, its existing encoder adopts it, with
turn count, tool history and attention debt intact.

A tab is recognised by *any* of those fields rather than by the best one in each
payload, because the payloads don't agree on which fields they carry.
`register_session.py` reads the whole environment; `notify.sh` reports the few
variables that name a tab, in a header, without spawning anything to do it;
`mft.discover` recovers a third subset out of the process table. Two partial
descriptions of one tab that overlap anywhere are one tab.

That matters because of what the alternative looks like. An event that can't
name its tab has to be answered by guessing, and a `/clear` is where the guess
falls due: the replacement session id starts sending tool calls immediately,
while the hook that says which tab it lives in is a Python process and `async`.
Get it wrong and the tab is on the board **twice** — one knob still pointing at
the terminal, so pressing it works, the other holding the live session and
pressing to nowhere — and neither goes away for an hour of TTL. So:

- every hook names its tab, not just the two that run Python;
- an anonymous event in a directory where a slot was wiped by `/clear` seconds
  ago is that clear's other half, not a new session (`CLEAR_ADOPT_SECONDS`);
- when an identity does arrive late, the two records **merge** onto the encoder
  the tab was already using, and the live session inherits what the other one
  knew about the terminal, so the knob neither moves nor stops working;
- and once every reap, `SessionTable.reconcile` re-checks the whole board for
  the invariant from outside — one terminal, one encoder — because the failure
  it catches is a board that looks entirely plausible and is quietly wrong. It
  merges what it finds, and releases the leftovers of a `/clear` that nothing
  ever joined back up. `curl -s localhost:7654/status` shows every token each
  session answers to, which is where to look when a knob is on the wrong agent.

`/clear` itself arrives as a pair (`SessionEnd` reason `clear`, then
`SessionStart` source `clear`). Either half keeps the slot, swaps in the new
session id, and resets everything turn-scoped: tool counts, subagent pile,
attention debt, and the context gauge, which belongs to a transcript that
session no longer has. Other end reasons (`logout`, `prompt_input_exit`,
`other`) actually release the encoder.

The moment gets a brief white ring wipe — the agent forgot everything.
`MFT_CLEAR_ANIMATION=0` turns it off.

## Sessions that predate the daemon

Hooks only push, and nothing in Claude Code answers questions, so a session
already running when the daemon starts stays dark until its next event — which
for the one parked at a permission prompt (exactly the one you wanted to see) is
never.

`mft.discover` reconstructs them from transcripts (`~/.claude/projects/`) joined
with the process table. Neither is trusted alone, and a process table that can't
be read adopts nothing: a missing encoder is a session you find in a moment,
while a phantom one is a knob that lies to you for an hour.

Adopted sessions land as `idle` — a transcript records what a session *did*,
never what it's doing now. Their context ring is real, read from that same
transcript. The first genuine hook event takes over. When two tabs sit in the
same working directory, both keep an encoder but give up their terminal
identity, since guessing would hand the wrong knob to the next `/clear`.

```sh
.venv/bin/python -m mft.daemon --discover      # what startup would adopt, then exit
.venv/bin/python -m mft.daemon --no-discover   # start with an empty board
```

## What the lights are saying

The design rests on one fact: **you are not looking at this device.** Peripheral
vision catches *movement* and *hue change*; arc position and static colour are
invisible until you turn your head. So motion is a budget.

- **At most one encoder moves fast at a time.** The fast animation goes to the
  highest-priority attention state, oldest first; everyone else drops to a slow
  pulse. A board where everything moves is a board where nothing stands out.
- **The things that want you get different answers.** A fast red gate for a
  permission prompt, a yellow flash for a plan awaiting sign-off, a slow amber
  breath for idle-waiting. One blinking red for all of them trains you to ignore
  blinking red. Errors are also red but *solid* — a rate limit is bad, but it
  isn't waiting on your hand.
- **Everything that blinks, blinks together.** The daemon sends MIDI clock, so
  gates stay in phase instead of drifting apart and reading as broken hardware.
  `MFT_CLOCK_BPM=0` turns it off.
- **A working ring is a fuel gauge.** It fills as that agent's context window
  fills, so "this one is about to compact" is legible from across the room. Read
  out of `transcript_path`, since no hook payload carries token counts. Activity
  is carried by *brightness* instead: every tool call kicks it back to full and
  it decays between them, so shimmer rate is tool-call frequency.
  `MFT_CONTEXT_RING=0` falls back to a rotating tool-call arc everywhere.
- **Green is one continuous ramp, not three states.** A finished turn is solid
  bright green, fades over 90s, rests dim green. Orange means the agent has the
  floor; green means you do.
- **A finished plan is not a permission gate.** "Read this and decide" and "may I
  run `rm`" want different things from you, so a plan gets its own hue and a
  slower flash.
- **`bypassPermissions` is magenta**, in every state, so you never have to wonder
  which agent has the guardrails off.
- **Compaction is visible.** `PreCompact`/`PostCompact` drain the ring, hold a
  beat, then refill — something completely opaque in the terminal that materially
  changes what your agent knows.

### Attention debt

The most interesting thing the board encodes isn't agent state, it's *your
neglect*. A session that finishes and goes unvisited ramps brightness over five
minutes — more insistent the longer you ignore it — and goes quiet the moment you
focus its tab. Same for one that's been idle-waiting a while. Finished work is
capped well below a live block, so it can never outshout one.

## Gestures

| Gesture | Does |
|---|---|
| press | focus that terminal tab, and forgive its attention debt |
| hold | peek: the rest of the bank becomes that session's last 15 tool calls |
| turn | nothing, beyond waking a sleeping board |

That's the whole input surface, and it's meant to be. Both gestures affect the
*board* — what you're looking at, what it's allowed to nag you about — and
neither affects a session.

### Peek

Hold a knob for **0.6 seconds** and its 15 neighbours re-render as that
session's recent tool calls, oldest to newest, hue by tool kind. Release and the
board snaps back — a spring-loaded modal view, not a mode you can get stuck in.
"This agent has done nothing but grep for four minutes" is legible from across
the room, and there's no other way to learn it without opening the tab.

```
┌────┬────┬────┬────┐   the held knob is purple; the rest of its bank is
│    │    │    │ ●  │   history, oldest first, filling from the far corner
├────┼────┼────┼────┤   backwards — so an agent that has only just started
│ ●  │ ●  │ ●  │ ●  │   shows a short run in the bottom-right and dark
├────┼────┼────┼────┤   encoders where it hasn't been yet
│ ●  │ ●  │ ●  │ ●  │
├────┼────┼────┼────┤   ██ read   ██ edit    ██ bash   ██ search
│ ●  │ ●  │ ██ │ ●  │   ██ web    ██ subagent   ██ everything else
└────┴────┴────┴────┘
```

| Hue | Tools |
|---|---|
| blue | `Read` |
| orange | `Edit`, `Write`, `NotebookEdit` |
| magenta | `Bash`, `BashOutput` |
| cyan | `Grep`, `Glob` |
| spring | `WebFetch`, `WebSearch` |
| violet | `Task`, `Agent` |
| azure | anything else, MCP tools included |

Under 0.6s, releasing focuses the tab; at or over it, releasing does nothing —
the hold *was* the gesture, and you held the knob precisely to avoid opening the
tab. The history is the last 15 *completed* calls, a rolling window over the
whole session rather than the current turn, so you can hold an idle agent's knob
and still read what it spent the last turn doing. `/clear` wipes it. There is no
peek on an unclaimed encoder.

## Sleep

Half an hour with no hook event from anywhere and no hand on any knob, and the
whole board fades over 20 seconds to 10% — dim enough to read as asleep from
across the room, bright enough that every colour and ring position survives. At
an hour it fades the rest of the way to actual dark.

Nothing on the board can wake it, because an agent doing anything at all is
sending events; these timings are only ever reached by an unattended desk. Any
event, or a hand on any knob, ramps it back to full over 0.4 seconds.

**An encoder asking for you is exempt.** A permission prompt, a plan approval, a
rate-limit error — anything unattended — stays at full brightness while the rest
of the board goes out around it. Going dark because the human left is precisely
the wrong answer to "a human is needed here." A session that merely *finished*
is not an alert, and sleeps with everything else.

`MFT_SLEEP=0` turns it off; `MFT_SLEEP_SECONDS` moves the first stage (also how
you watch it happen without waiting half an hour). `GET /status` reports the
current level as `sleep`, so a dark desk is distinguishable from a dead daemon.

## Subagents

Sessions fill encoders from the top-left forwards; subagents fill from the
**bottom-right backwards**, so the far corner is always the newest thing on the
board. Neither block is ever allowed a hole in it — when a session goes away the
ones after it slide up to close the gap.

```
┌────┬────┬────┬────┐   sessions →                    ← subagents
│ ●  │ ●  │ ●  │    │
├────┼────┼────┼────┤   one parent fanning out to three
│    │    │    │    │   shows up as three violet dots
├────┼────┼────┼────┤   piling in from the corner
│    │    │    │ ◦  │
├────┼────┼────┼────┤
│    │    │ ◦  │ ◦  │
└────┴────┴────┴────┘
```

Subagents are not sessions and never look like one: violet — a hue used for
nothing else — held at a steady mid brightness, with a stub ring. They own no
encoder, answer no gesture, never take a claimed slot, and collapse back when
the parent's turn ends. The stub ring is deliberate: a subagent has no context
reading of its own. `MFT_SUBAGENT_STACK=0` turns them off.

They used to breathe, on the slowest pulse the board had. That was a mistake for
a reason worth writing down: channel 3 carries an animation *or* a brightness
level, so a pulsing encoder runs at the hardware's own levels and sits near off
for most of every cycle — and a dim RGB LED reads blue whatever hue you send it.
The pile looked like faint blue pips, which is the one thing it must not look
like. Identification beat liveness; the stack moves anyway, growing and
collapsing as the parent spawns and reaps.

If your settings predate the `SubagentStart` hook you'd see no subagents at all,
silently, so `PreToolUse`/`PostToolUse` for `Task` and `Agent` are counted as a
fallback. `install_hooks.py --check` reports events this code handles that your
settings file doesn't have; the daemon logs the same at startup.

## Focus adapters

Pressing an encoder runs `mft/focus.py`. Adding a terminal means appending one
`Adapter(...)` to `ADAPTERS`.

| Terminal | Handle | Mechanism |
|---|---|---|
| Apple Terminal | tty | AppleScript, matched against each tab's `tty` |
| iTerm2 | `ITERM_SESSION_ID` GUID | AppleScript `select session` |
| tmux | `TMUX_PANE` | `tmux select-pane` / `switch-client` |
| WezTerm | `WEZTERM_PANE` | `wezterm cli activate-pane` |
| kitty | `KITTY_WINDOW_ID` | `kitty @ focus-window` |
| any macOS app | `__CFBundleIdentifier` | `open -b`, raises the app but not the tab |
| named terminals | `TERM_PROGRAM` | `open -a` |
| last resort | `pid` | walk up the process tree to the owning `.app`, `open -a` |

`mux` adapters run first and `gui` adapters second, so a tmux pane inside
Terminal.app gets both the pane selection and the window raise. The `gui`
adapters are a **chain, not a choice** — kitty with remote control off, a moved
wezterm socket, a timed-out AppleScript — so a failure falls through to the next
rather than ending the press. The tail needs nothing but a pid, which every
session has.

A session whose environment was never captured is read back out of the process
table (`ps -E`), matched on working directory. Two Claudes in one directory are
indistinguishable from out there, so they get their shared application and no
tab, until one of them is pinned by a hook.

## Configuration

Every tunable is in `mft/config.py` and env-overridable. The switches you'd most
likely want at runtime:

| Variable | Does |
|---|---|
| `MFT_SLEEP`, `MFT_SLEEP_SECONDS` | board sleep, and when the first stage lands |
| `MFT_CONTEXT_RING` | ring is a context gauge (`0` = tool-call arc everywhere) |
| `MFT_SUBAGENT_STACK` | the violet pile in the corner |
| `MFT_CLOCK_BPM` | MIDI clock; `0` stops sending it |
| `MFT_BOOT_ANIMATION`, `MFT_CLEAR_ANIMATION`, `MFT_SPAWN_ANIMATION`, `MFT_AMBIENT` | the decorative layers |
| `MFT_WHITE`, `MFT_DARK_COLOR`, `MFT_DARK_VALUE`, `MFT_RING_DARK_VALUE` | per-unit colour calibration |
| `MFT_HOST`, `MFT_PORT` | where the daemon listens |
| `MFT_DISCOVER` | `0` is the same as `--no-discover` |

### Calibrating

The channel layout is DJTT's documented default and stable:

| Channel | Carries |
|---|---|
| 1 | encoder value / LED ring position |
| 2 | switch RGB colour, and switch press input |
| 3 | RGB animation *or* RGB brightness |
| 4 | banks, side buttons |
| 6 | ring animation *or* ring brightness |

Note the *or*: on channels 3 and 6 the low value band is rate-based animations
and the band above it is a plain brightness ramp, so a lit encoder can animate
or dim, not both.

The value→colour and value→animation numbers drift between firmware revisions,
so `config.py` ships anchors rather than gospel. Sweep your own:

```sh
.venv/bin/python -m mft.calibrate colors   # 16 hues at a time
.venv/bin/python -m mft.calibrate white    # find your unit's white
.venv/bin/python -m mft.calibrate anim     # channel 3 band
.venv/bin/python -m mft.calibrate ring     # channel 6 band
.venv/bin/python -m mft.calibrate ramp     # ring positions
.venv/bin/python -m mft.calibrate dark     # find "off" if a firmware update moved it
```

**Off is a specific number and it is not the obvious one.** Channel 2 is hue all
the way down (0 is bright blue, not dark), so the RGB is only switched off on
channel 3 — where 0 means "no animation" and 17 is the *slowest pulse*, not a
dim encoder. Off is **18** (`DARK_VALUE`). The ring's floor on channel 6 is
**17** (`RING_DARK_VALUE`); the two ramps do not line up. Watch a candidate for a
few seconds before calling it off: the pulse rates just below look dark at a
glance and then swell back up.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

## Not covered

- Token counts and cost aren't in hook payloads. Tail `transcript_path` (JSONL)
  for those — it lags the live conversation a little, which is fine for a light.
- The other three banks are 48 more session slots rather than three more *views*
  of the same 16 (cost, activity heatmap, global controls). Views are the better
  use of them — you will never run 64 agents — but that's a different allocator
  plus a bank-switch listener on channel 4.
- Sessions aren't grouped by `cwd`, so a column doesn't mean anything yet.
- Remote sessions over SSH post to the daemon only if you forward the port.
