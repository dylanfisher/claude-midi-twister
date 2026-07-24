# claude-midi-twister

Your running Claude Code sessions, one per encoder, on a DJTT Midi Fighter Twister.
Each session claims an encoder: the RGB ring under the knob says what state it's in,
the LED ring says how busy it is, and **pressing the encoder brings that session's
terminal tab to the front**.

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
  turn right / left → snooze / un-snooze
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

`install_hooks.py` backs up your settings first and tags its own entries, so
`--uninstall` removes exactly what it added and leaves your other hooks alone.
It writes to *user* settings, so every project gets the visualizer.
`--check` compares what's installed against what the code expects and exits
non-zero if you're behind — worth running after a `git pull`, since a hook that
was added to the code and never installed is invisible rather than broken.

Then, in the Midi Fighter Utility, set the encoders to accept host LED control —
otherwise the device drives its own LEDs and ignores you.

## Run

```sh
.venv/bin/python -m mft.daemon
```

Leave it running. It spells CLAUDE across the grid a letter at a time, in white
at full brightness, each letter hard-cutting in and sitting there for its full
three quarters of a second — unhurried on purpose, since a word you only catch
the tail of may as well not be spelled, and a 4x4 glyph is only legible while
every one of its pixels is lit. Then it lamp-tests every ring with one arc sweep and dissolves into a generative
field: travelling sine waves at rates that share no common factor, so it never
loops. That runs until a Claude turns up, fading out over a minute if none does.
`MFT_BOOT_ANIMATION=0` skips the whole thing. Open Claude Code anywhere and its
encoder lights up — the field gets out of the way and never paints over a live
session.

Sessions you already had open when you started the daemon appear immediately;
see [Adopting sessions that predate the daemon](#adopting-sessions-that-predate-the-daemon).

```sh
curl -s localhost:7654/status | python3 -m json.tool   # who owns which encoder
.venv/bin/python -m mft.simulate --sessions 6          # fake sessions, no Claude needed
.venv/bin/python -m mft.daemon --no-device             # no hardware needed either
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

The bundle is a folder with a shell script inside, so no compiler is involved;
`app/make_app.py` also draws the icon from scratch with zlib and some
arithmetic. Rebuild it after moving the repo, since it bakes in absolute paths.

If Spotlight can't find it, move it to `/Applications`, which is always
indexed: `.venv/bin/python app/make_app.py --dest /Applications`.

Liveness is tracked with a pid file at
`~/Library/Application Support/ClaudeTwister/daemon.pid`, not `pgrep -f` — that
matches any process merely *mentioning* the daemon, including the shell you
typed it into, which makes the toggle randomly do the wrong thing. Same
machinery is available directly:

```sh
.venv/bin/python -m mft.daemon --status   # exit 0 if running
.venv/bin/python -m mft.daemon --stop
```

On the way out, the board fills from the top-left corner in a spiral to the
centre, then all sixteen encoders run the whole colour wheel together and fade
to black. That's not decoration either: seeing it is how you know the daemon
exited cleanly rather than died. It takes under four seconds — something is
waiting on it.

To start it automatically at login instead, drop a launchd plist in
`~/Library/LaunchAgents` pointing at `.venv/bin/python -m mft.daemon`.

## How it works

```
Claude Code session ──hook──▶ POST localhost:7654/event ──▶ daemon ──MIDI──▶ Twister
       ▲                                                      │
       └──────────── AppleScript / tmux / wezterm ◀───────────┘  (encoder press)
```

Every hook except two is `type: "http"`, so events cost a local POST and **no
process spawn**. Connection failures are non-blocking — with the daemon down,
Claude Code carries on and you just have no lights.

The exceptions are `SessionStart` and `UserPromptSubmit`, which run
`hooks/register_session.py` as an `async` command hook. HTTP hooks post only the
event JSON, and the thing we need — *which terminal tab is this?* — lives in the
environment. It captures `TERM_SESSION_ID` / `ITERM_SESSION_ID` / `TMUX_PANE` /
`WEZTERM_PANE` / `KITTY_WINDOW_ID` / `__CFBundleIdentifier` and the tty, and
posts them alongside the event.

It repeats on every prompt because hooks only push: a session that started while
the daemon was down, or that was running when the daemon restarted, can never be
*asked* where it lives. Announcing once means those sessions stay unfocusable
forever; announcing each turn costs one short-lived process and makes the
identity self-healing. A session with no tty reports only its pid — the desktop
app inherits the launching terminal's variables, and two of them inherit the
same ones, so believing that would key both to a tab neither is in.

Every hook is notify-only and the daemon answers all of them `204`, with no body
at all. Claude Code parses a hook's response body as hook control JSON — it's how
a hook blocks a tool call or injects context — so a visualizer with no opinion
has to say nothing whatsoever. No hook here carries a `timeout`, because nothing
is ever waited on for an answer.

State is inferred from the gaps between events, because hooks are discrete
notifications rather than a telemetry stream:

| Hook | State |
|---|---|
| `SessionStart` | claim an encoder, `idle` |
| `UserPromptSubmit` | `thinking`, new turn, reset counters |
| `PreToolUse` / `PostToolUse` | `working`, re-read the context gauge |
| `MessageDisplay` | `streaming` (opt-in, see below) |
| `Notification` | one of `permission` / `plan` / `waiting` / `done`, by `notification_type` |
| `SubagentStart` / `SubagentStop` | stack up from the bottom-right of the bank |
| `PreCompact` / `PostCompact` | drain the arc, then refill it |
| `StopFailure` | `error` + alert, and spell the reason if it's a rate limit |
| `Stop` | `done`, fades out over 90s, then ramps back if ignored |
| `SessionEnd` | release the encoder |

A crashed terminal never fires `SessionEnd`, so slots also expire on a TTL
(`SESSION_TTL_SECONDS`).

`PermissionRequest` is deliberately **not** installed. An HTTP hook on it sits in
the path between Claude Code and its own permission prompt, where a visualizer
has no business being able to delay — let alone answer — a permission.
`Notification` already reports permission prompts after the fact, with nothing
waiting on the reply, which is all a light needs.

`MessageDisplay` fires while assistant text streams and Claude Code waits for
your hook before painting the next batch — a live pulse, at the cost of sitting
in the render path. It's off by default; enable with
`install_hooks.py --with-message-display`.

## Encoders belong to terminals, not to sessions

`SessionStart` fires with source `clear`, `resume`, `compact` and `fork`, and a
`/clear` hands you a **new `session_id` in the same tab**. Key slots on the
session id and every `/clear` teleports your agent to a different knob.

So the durable identity is the terminal — `TMUX_PANE`, the iTerm session GUID,
the tty, and failing all of those the `claude` process id — and the session id is
a mutable attribute of the slot. When a familiar tab shows up with an unfamiliar
session id, its existing encoder adopts it: turn count, tool history and
attention debt intact.

## Adopting sessions that predate the daemon

Hooks only push. Nothing in Claude Code answers questions, so a session that was
already running when the daemon started stays dark until it fires its next event
— which for a session mid-turn is seconds, and for the one parked at a
permission prompt (exactly the one you wanted to see) is never.

`mft.discover` reconstructs them from the two things the machine can still be
asked:

- **Transcripts.** `~/.claude/projects/<slug>/<session-id>.jsonl`. The filename
  is the session id and the entries carry `cwd`. What they cannot say is whether
  that session is still running: one you Ctrl-C'd two minutes ago looks exactly
  like one waiting on you.
- **The process table.** `ps` proves liveness; `lsof` gives each process its
  working directory; the tty is what makes the slot durable.

Neither is adopted alone, and a process table that can't be read adopts nothing
at all — a missing encoder is a session you find in a moment anyway, while a
phantom one is a knob that lies to you until the hour-long TTL reaps it.

Sessions whose argv names them (`--session-id`, the app-launched form) are exact.
The rest are matched newest-transcript-to-live-process within a working
directory. When two tabs sit in the *same* directory nothing on disk says which
tty is which, so both keep their encoder and give up their terminal identity —
guessing would hand the wrong knob to the next `/clear` in either tab.

Adopted sessions land as `idle`: a transcript records what a session *did*, never
what it's doing now, and inventing an attention state from a file would strobe an
encoder red for a prompt you answered before the daemon started. Their context
ring is real, though — that's read from the same transcript. The first genuine
hook event finds the session by id and takes over.

```sh
.venv/bin/python -m mft.daemon --discover      # what startup would adopt, then exit
.venv/bin/python -m mft.daemon --no-discover   # start with an empty board
```

`MFT_DISCOVER=0` is the same as `--no-discover`.

## What the lights are actually saying

The whole design rests on one fact: **you are not looking at this device.**
Peripheral vision reliably catches *movement* and *hue change*; arc position and
static colour are invisible until you turn your head. So motion is a budget.

- **At most one encoder on the board moves fast at a time.** Several sessions
  blocking at once would fill the grid with competing strobes, and a board where
  everything moves is a board where nothing stands out. The fast animation goes
  to the highest-priority attention state, oldest first; everyone else drops to a
  slow pulse.
- **The things that want you get different answers.** `Notification`
  distinguishes `permission_prompt`, `idle_prompt` and `agent_needs_input`, and
  they mean different things to you: a fast red gate, a slow amber breath, a
  yellow flash for a plan awaiting sign-off. One blinking red for all of them
  trains you to ignore blinking red. Errors are also red but *solid* — a rate
  limit is bad, but it isn't waiting on your hand.
- **Everything that blinks, blinks together.** The Twister's gate and pulse rates
  are measured in beats and it takes its beat from MIDI clock, so the daemon
  sends one. Free-running gates drift apart and read as broken hardware; gates in
  phase read as designed. `MFT_CLOCK_BPM=0` turns it off.
- **A working ring is a fuel gauge.** It fills as that agent's context window
  fills, so "this one is about to compact" is legible from across the room. No
  hook payload carries token counts, but every payload carries `transcript_path`
  and the transcript records each assistant message's `usage`, so `mft/context.py`
  reads the last one — skipping sidechain entries, which are subagents with their
  own much smaller contexts. Activity is carried by *brightness* instead: every
  tool call kicks it back to full and it decays between them, so shimmer rate is
  tool-call frequency and a session that stops calling tools sags toward the idle
  floor. Sessions with no reading yet fall back to the rotating tool-call arc.
  `MFT_CONTEXT_RING=0` keeps the arc everywhere.
- **Green is one continuous ramp, not three states.** A finished turn is solid
  bright green, fades out over 90s, and rests dim green — same hue throughout, so
  the fade *is* the transition and nothing changes colour to no purpose. Orange
  means the agent has the floor; green means you do.
- **A finished plan is not a permission gate.** Claude Code has no plan-ready
  hook — a plan arrives as a permission request for `ExitPlanMode`, or as a
  `Notification` whose only tell is its prose — but "read this and decide" and
  "may I run `rm`" want different things from you, so it gets its own hue and a
  slower flash.
- **Subagents are not sessions, and never look like one.** They own no encoder,
  answer no gesture and vanish when the parent's turn ends, so they get a hue
  used for nothing else, a stub ring, and the slowest pulse on the board. See
  below.
- **`bypassPermissions` gets a hue reserved for nothing else.** `permission_mode`
  is on most hook payloads, so any session running unattended is magenta in every
  state. You should never have to wonder which agent has the guardrails off.

### Attention debt

The most interesting thing the board can encode isn't agent state, it's *your
neglect*. A session that finishes and goes unvisited slowly ramps brightness over
five minutes — more insistent the longer you ignore it — and goes quiet the
moment you focus its tab. Same for one that's been idle-waiting a while. Finished
work is capped well below a live block, so it can never outshout one.

Turning a knob right snoozes that slot in five-minute steps: dim, still, with the
arc counting down what's left. Turning left gives the time back.

## Gestures

| Gesture | Does |
|---|---|
| press | focus that terminal tab, and forgive its attention debt |
| hold | peek: the rest of the bank becomes that session's last 15 tool calls |
| turn right / left | snooze / un-snooze, in 5-minute steps |

That's the whole input surface, and it's meant to be. Every gesture affects the
*board* — what you're looking at, what it's allowed to nag you about — and none
of them affects a session. The device can't approve a tool call, answer a prompt,
or hold anything up.

**Peek** is a modal zoom out of a grid with no screen. Hold a knob and its 15
neighbours re-render as that session's recent tool calls, oldest to newest, hue
by tool kind — edits orange, bash magenta, searches cyan, reads blue. Release and
the board snaps back. "This agent has done nothing but grep for four minutes" is
legible from across the room, and there's no other way to learn it without
opening the tab.

Which encoding a turn arrives in depends on the mode you picked in the Midi
Fighter Utility. Relative mode sends 63 and 65; absolute mode sends a position
and also fights the ring positions the daemon writes, so prefer relative. The
daemon reads both (`MFT_ENCODER_MODE=auto|relative|absolute`).

## Subagents

Sessions are handed encoders from the top-left forwards. Subagents fill from the
**bottom-right backwards**, so the two allocators grow toward each other and the
far corner is always the newest thing on the board — read the pile from the
corner inwards and you're reading it newest-first.

Neither block is ever allowed a hole in it. A session always takes the
first encoder no *live* session is on — an ended one renders dark, so it gets
displaced rather than left sitting in the middle — and when a session goes away
the ones after it slide up to close the gap. Live agents are therefore always a
solid run from the top-left, and the pile of subagents a solid run from the
bottom-right, with the empty encoders between them.

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

They're violet — a hue used for nothing else, so an encoder that isn't any of
the state colours is unambiguously a subagent — dim, with a stub ring and the
slowest pulse the board has, one step below the slowest a *session* is ever
allowed to move. That ordering is the point: a subagent reads as alive without
ever competing with a real session for your eye.

They never take a claimed encoder. If the corner is occupied, the pile lands in
whatever's left over rather than trampling someone's slot, and it collapses back
when the parent's turn ends. `MFT_SUBAGENT_STACK=0` turns them off.

Two signals feed the pile, because one of them may not be installed.
`SubagentStart`/`SubagentStop` say it directly and carry an `agent_id`, but they
are a recent hook, and a settings file written before them reports no subagents
at all — silently, since the only symptom is a corner that never lights. So the
`PreToolUse`/`PostToolUse` pair for `Task` and `Agent` is counted too, keyed on
the tool use id. The same subagent shows up in both, and nothing in either
payload says so, so the counts are kept apart and the larger wins: adding them
would double every dot, and preferring one would drop a dot each time the
signals hand over mid-turn.

A subagent event is never allowed to create a session. Whether it carries the
parent's session id or the subagent's own is undocumented, and answering the
second case the ordinary way would hand a subagent a *session* encoder from the
top-left — the one thing the board must not do. It's matched to a parent by
session id, then by working directory, and dropped if neither finds one.

`install_hooks.py --check` reports events this code handles that your settings
file doesn't have; the daemon logs the same thing at startup. That check exists
because its absence is what hid the missing `SubagentStart` — a hook nobody
installed produces no events, and no events produce no log.

Note that a subagent has no context reading of its own — sidechain entries are
skipped when the parent's gauge is read — so the ring is a deliberate stub
rather than a gauge pretending to mean something.

## Focus adapters

Pressing an encoder runs `mft/focus.py`. Adapters are three lines each: how to
recognise the terminal from the captured environment, and how to raise it.

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
Terminal.app gets both the pane selection and the window raise. Adding a
terminal means appending one `Adapter(...)` to `ADAPTERS`.

The `gui` adapters are a **chain, not a choice**. Any of them can fail for
reasons unrelated to whether it was the right one — kitty with remote control
off, a moved wezterm socket, an AppleScript that timed out against a busy app —
so a failure falls through to the next rather than ending the press. The tail
needs nothing but a pid, which every session has. The AppleScript adapters also
find the tab *before* activating anything, so trying Terminal on a session that
turns out not to be in Terminal costs a no-op instead of raising a wrong window.

A session whose environment was never captured doesn't just fail: the press
reads it back out of the process table (`ps -E` prints a process's environment),
matched on working directory, and caches what it finds. Two Claudes in one
directory are indistinguishable from out there, so they get their shared
application and no tab — until one of them is pinned by a hook, which leaves the
other unambiguous.

## The frame buffer

A bank is a 4×4 grid, which is to say a 16-pixel display, and channel 6's ring
brightness is real grayscale per pixel — so letters can crossfade instead of
hard-cutting. `mft/font.py` is a 4×4 bitmap alphabet and `mft/board.py` composes
the whole 64-cell board every frame:

1. `mft/render.py` turns each session into one `Cell` (hue, animation, arc,
   brightness). Pure function of `(session, clock)`.
2. Subagents stack into **unclaimed** encoders from the bottom-right, so
   parallelism is physically visible and then collapses back without disturbing
   anyone's slot.
3. Motion arbitration leaves the fast animation on exactly one encoder.
4. With nothing running at all, the board breathes rather than going dark. It
   stops being a dashboard and becomes an object that lives on your desk.
5. Overlays paint on top and expire on their own: the boot animation, the idle
   lamp test, the compaction sweep, a spelled-out banner (RATE, when a turn dies
   on a rate limit), the press-and-hold detail view. They're told which
   encoders a session already owns, so an idle animation can yield to real
   state rather than sit on top of it.

Overlays are pure paint and never touch session state, so one that's dropped
mid-flight leaves nothing to tear down.

Compaction is worth calling out. `PreCompact`/`PostCompact` bracket something
completely opaque in the terminal that materially changes what your agent knows:
the arc drains to zero, sits desaturated for a beat, then refills. After a week
you'll have an intuition for how often your sessions compact that there's
currently no way to acquire.

## Calibrating

Channel layout is DJTT's documented default and stable:

| Channel | Carries |
|---|---|
| 1 | encoder value / LED ring position |
| 2 | switch RGB colour, and switch press input |
| 3 | RGB animation *or* RGB brightness |
| 4 | banks, side buttons |
| 6 | ring animation *or* ring brightness |

Note the *or*: on channels 3 and 6 the low value band is rate-based gate/pulse
animations and the band above it is a plain brightness ramp, so a lit encoder can
animate or dim, not both.

There is also no *off*. Channel 2 is hue all the way down — 0 is blue, not dark.
Value 0 on channels 3 and 6 means "no animation", which stops overriding the
device, and it goes back to showing its own inactive colour. And the bottom of
the brightness ramp does not extinguish the switch LED either; it just makes it
faint. An encoder on this hardware is always lit to some degree.

So "dark" here means *the least visible thing an encoder can be*: minimum
brightness (`config.DARK_VALUE`, 17) wearing white (`config.DARK_COLOR`) rather
than whatever hue it was last showing. A board that cannot go dark should at
least go colourless — sixteen faint white pips read as a device at rest, where
sixteen faint blue ones read as a device still trying to say something. That is
what the shutdown wipe fades into, and the daemon forces it onto all 64 encoders
before letting go of the port, so a stopped daemon leaves a uniformly dim board
instead of the hue it happened to die on.

Which channel-2 value is actually white varies by unit — `MFT_WHITE` overrides
it, `MFT_DARK_COLOR` and `MFT_DARK_VALUE` override the resting appearance
outright, and `python -m mft.calibrate dark` sweeps channel 3 sixteen values at
a time if you want to see for yourself how far down it goes.

The exact value→colour and value→animation numbers drift between firmware
revisions, so `mft/config.py` ships anchors rather than gospel. Sweep your own:

```sh
.venv/bin/python -m mft.calibrate colors   # 16 hues at a time
.venv/bin/python -m mft.calibrate anim     # channel 3 band
.venv/bin/python -m mft.calibrate ring     # channel 6 band
.venv/bin/python -m mft.calibrate ramp     # ring positions
```

Everything else worth tuning — frame rate, colours per state, fade durations,
attention ramp, snooze steps, brightness floors — is in `mft/config.py` too, and
the switches you'd most likely want at runtime are environment variables:
`MFT_CLOCK_BPM`, `MFT_BOOT_ANIMATION`, `MFT_AMBIENT`, `MFT_SUBAGENT_STACK`,
`MFT_ENCODER_MODE`, `MFT_CONTEXT_RING`, `MFT_DARK_VALUE`, `MFT_DARK_COLOR`,
`MFT_WHITE`.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

## Not covered

- Token counts and cost aren't in hook payloads. Tail `transcript_path` (JSONL)
  for those — it's written asynchronously and lags the live conversation a
  little, which is fine for a light.
- The other three banks are still 48 more session slots rather than three more
  *views* of the same 16 (cost, activity heatmap, global controls). Views are the
  better use of them — you will never run 64 agents — but that's a different
  allocator plus a bank-switch listener on channel 4, not a tweak.
- Sessions aren't grouped by `cwd`, so a column doesn't mean anything yet.
- Remote sessions over SSH post to the daemon only if you forward the port.
