# claude-midi-twister

Your running Claude Code sessions, one per encoder, on a DJTT Midi Fighter Twister.
Each session claims an encoder: the RGB under the knob says what state it's in,
the LED ring says how long the turn has been running (or, once it's resting, how
full its context window is), and **pressing the encoder brings that session's
terminal tab to the front**.

```
┌────┬────┬────┬────┐   ● red gate      wants permission — a human is blocking
│ ●  │ ○  │ ◐  │    │   ● yellow flash  a plan is written and wants a yes
├────┼────┼────┼────┤   ● red solid     errored (rate limit, overload, billing)
│ ◑  │    │ ●  │    │   ● amber breath  idle-waiting on you
├────┼────┼────┼────┤   ● orange fill   working — the ring is the turn's length
│    │    │    │    │   ● cyan sweep    thinking
├────┼────┼────┼────┤   ● green solid   finished, then fading out
│    │    │ ◦  │ ◦  │   ● dim green     idle — the ring is its context window
└────┴────┴────┴────┘   ● magenta       running unsupervised
  press → focus that tab       ◦ violet        subagents, stacked from the corner,
  hold  → peek at its history                  each shimmering on its own tool calls
```

Nothing on the device can answer a prompt, approve a tool call, or block a
session. It reports; you decide, in the terminal.

4 banks × 16 encoders = 64 simultaneous sessions, which is more terminals than
you have. Only sixteen are on the front panel at a time, so a block on another
bank [pulls the view onto itself](#banks-and-the-board-following-a-prompt).

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

It also sets one environment variable, `CLAUDE_CODE_DISABLE_TERMINAL_TITLE`,
which is the only thing here that changes how a session behaves — it hands the
terminal title to the daemon, which can paint the session's state there too.
That painting is off by default; see
[the tab strip](#the-same-state-in-the-tab-strip) for `MFT_TAB_TITLE=1`.

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

Building also leaves a copy of the venv's interpreter at
`.venv/bin/claude-twister`, and the app runs the daemon through that. macOS
names a process after the executable file it ran, so this is the difference
between finding *claude-twister* in Activity Monitor and hunting through six
processes called *Python*. Nothing else changes: it's the same interpreter,
sitting in the same `bin/`, so it finds the same venv. Symlinking doesn't work
— the kernel resolves through it and reports `Python` anyway — and neither does
rewriting `argv[0]`, which only touches the command line. Rebuild it after a
Python minor upgrade, alongside the venv itself.

To start at login instead, drop a launchd plist in `~/Library/LaunchAgents`
pointing at `.venv/bin/claude-twister -m mft.daemon`.

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
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | `working`, re-read the context gauge — and if it carries an `agent_id`, brighten that subagent's dot |
| `Notification` | `permission` / `plan` / `waiting` / `done`, by `notification_type` |
| `SubagentStart` / `SubagentStop` | stack up from the bottom-right of the bank |
| `PreCompact` / `PostCompact` | drain the ring, then refill it |
| `StopFailure` | `error`, and spell the reason if it's a rate limit |
| `Stop` | `done`, fades out over two minutes, then ramps back if ignored |
| `SessionEnd` | release the encoder — unless the reason is `clear` |
| `MessageDisplay` | `streaming` (opt-in, below) |

`SessionEnd` is advisory — a crashed terminal never fires it — so slots also
expire on a one-hour TTL, and are taken back the moment the process behind them
is gone (below). Between them, those two are what actually keeps the board
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

### When the session moves out of the tab

Claude Code doesn't necessarily run your session in the process you started. It
pre-warms spare processes under a shared background daemon and hands a
conversation to one of them — same tab, same conversation, **new session id, no
tty**. This is the one that looks worst on the board: the tab's knob sits frozen
on the last state it heard, decaying to dim green, while the session that is
actually running lights a second knob you can't press.

Nothing announces the move and nothing you can read reconstructs it. The hooks
deliberately report *no* environment when they find no tty — a process under that
daemon inherits the variables of whichever tab happened to start it, so quoting
them would key the encoder on a stranger's tab — which leaves a bare pid. Walking
up the process tree lands on that same first tab, and the transcripts carry no
lineage between the two session ids.

What's left is the directory and the timing, so that's what the handoff is
matched on. A session that names nothing but a pid, in the directory of a tab
that named itself properly, has no turn in flight and went quiet less than
`HANDOFF_ADOPT_SECONDS` ago, is taken for that tab's conversation continuing and
adopts its encoder. The pid is recorded as a `host:` token rather than a `pid:`
one — it names a process, not one of the tab's names — so the tab keeps its own
tty and pid, and pressing the knob still raises the right window.

One record then names two live processes, and the orphan sweep below wants
either of them alive: the tab's own `claude` exiting is a handoff completing,
not a session ending, and reaping on it alone put out the encoder of a session
that was working.

Being wrong here costs a background agent painting on the knob of an idle tab in
its own repository, and a press that raises that tab: the near miss, not a lie.
A session with no tab in its directory — the desktop app — still gets an encoder
of its own and the ancestor focus adapter raises the app it lives in.

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

## Sessions that outlive their process

The same process table answers the opposite question, and it is the one you
notice: you close every Claude session and a knob is still lit. `SessionEnd` is
the only thing that retires a slot promptly and it is the hook that most
reliably doesn't fire — a closed tab, a killed window, a `kill -9`, a machine
that came back from a crash. What's left is an encoder describing a session you
shut an hour ago.

So once every reap, `discover.orphans` asks whether the pid each session
recorded still exists — signal 0, no subprocess, cheap enough to sit in the run
loop — and releases the ones that don't. Every path that gives a session a
terminal writes that pid: `register_session.py` sends its `getppid()`, which is
the `claude` process itself, and discovery reads it out of the process table.

That settles every session it can ask about, and its problem is the ones it
can't. `notify.sh` names a tab without spawning a process to learn a pid, so a
session whose `SessionStart` never reached the daemon — daemon down when the tab
opened, daemon restarted mid-turn — runs its whole life on records the sweep has
nothing to ask about. Those are the knobs that were still going stale.

### The census

Every `CENSUS_INTERVAL_SECONDS` (30, on its own thread — one `ps` is ~150ms, or
four dropped frames) the daemon reads the whole process table once and spends it
on the two things the pid sweep can't have for free:

- **`discover.learn_pids`** gives a pid to the records that never got one, by
  matching them against processes that are running *right now* — argv's
  `--session-id`, then an identity token the hook and the process table both
  carry, then being the only Claude in a directory that only wants one. Every
  step must be unambiguous; a wrong pid is worse than none, since it would answer
  the sweep's question with somebody else's life. This is presence of evidence in
  both directions, and it shrinks the set below rather than judging it.
- **The tty half of `discover.orphans`.** A terminal tab holds its pty for
  exactly as long as it's open; close it and no process on the machine is on that
  tty again. So a session that named a tty whose tty is now free had its tab
  closed — whatever any pid says.

That second one is the only *absence* anything here concludes from, and the
reason it's allowed to is that it recognises nothing. The earlier version of this
section refused to compare sessions against the live process table, because that
comparison rests on `claude_processes` still knowing what a session's argv looks
like, and the day that changes it clears the board. Reading which ttys are in use
needs no such knowledge — it's a column, not a command line. The census
self-checks the read before trusting the negative (`Census.usable`: a real
machine has hundreds of processes and at least one terminal, so a table with
neither is a read that went wrong, not a desk that emptied), and the records with
*neither* a pid nor a tty — the ones that would need argv recognition — still
keep the hour.

It also settles pid reuse, which the pid sweep gets wrong in the expensive
direction: a recycled number reads as alive, but a live `claude` is on its tty by
definition, so a recorded tty belonging to nobody outranks it.

One case where a freed tty is expected and not acted on: a session handed off
into a process under Claude Code's background daemon holds a live `host:` pid.
Its tab closing isn't it ending — it's running somewhere else, and it keeps its
encoder.

The census also runs on wake, explicitly rather than on the interval: the clock
it's paced by stopped with the machine, so a lid closed for a weekend is half a
minute old from in here — and a suspend is precisely when the tabs on the board
got closed without telling anyone.

Sessions that ended cleanly are skipped by all of it — their process is
*supposed* to be gone, and they're already fading out on `SLOT_LINGER_SECONDS`.
`MFT_ORPHAN_SWEEP=0` turns both sweeps off, which is only useful for a
diagnosis: without them nothing takes a session off the board but a `SessionEnd`
or that hour.

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
- **A working ring is a stopwatch.** It fills with how long the current turn has
  been running, which is the one thing about a live agent nothing else on the
  board says: hue is what it's doing, brightness is how recently it called a tool
  and sags when it stops, and neither of them is *this one has been grinding for
  twenty minutes*. Log-scaled, because turn lengths are — the first quarter of
  the ring covers the first half-minute, so an ordinary turn is visibly moving,
  and a long one still has somewhere to go. It saturates at fifteen minutes
  rather than wrapping: a wrapped ring is indistinguishable from a turn that just
  started, and there's nothing you do differently at forty minutes than at
  twenty. `MFT_TURN_RING=0` puts the old rotating tool-call arc back, which this
  replaced — the arc is a fine thing to watch and a redundant one to spend the
  ring on, since its spin rate is tool-call frequency and so is the shimmer.

- **A resting ring is a fuel gauge.** It fills as that agent's context window
  fills, so "this one is about to compact" is legible from across the room. Read
  out of `transcript_path`, since no hook payload carries token counts.

  Resting and not working, because that's when the number is worth something:
  "how full is the window" is what you ask deciding whether to carry on here or
  start fresh, and that's a decision you make between turns, standing in front of
  the board. During a turn it barely moves and you can't act on it either way.
  Not in the states where the ring is already saying something louder, either:
  `thinking` and `streaming` sweep, and a blocking state pins the ring at full,
  where it means *you* rather than tokens. `MFT_CONTEXT_RING_IDLE=0` goes back to
  a bare pip; `MFT_CONTEXT_RING=0` removes the gauge entirely.

  **The gauge dims as it ages**, over the same three minutes the `done` flash
  recedes across, down to a floor it holds forever. A reading has a shelf life:
  ten seconds after a turn ends it's what the session *is*, ten minutes after
  it's what the session was when you last had a reason to care. Dimmer and never
  dark — an unlit gauge and a session with no reading at all would be the same
  encoder, and those are very different things.

  This is the one place on the board where the ring and the RGB are lit
  *differently*, and it's only possible because they're separate channels (6
  against 3). A finished session you never come and look at ramps its hue back up
  on attention debt; its gauge doesn't come with it, because the reading didn't
  get more urgent, only older. Everything else — every gesture, overlay and
  blocking state — is one encoder at one brightness, which is the `Cell` default.

- **Which context window, though.** The transcript names the model on every
  assistant message and that name doesn't say which *window* it is: the 1M
  variant is spelled `opus[1m]` in `settings.json` and spelled exactly like the
  200k one everywhere else. So the family comes off the transcript, where it's
  authoritative and follows a mid-session `/model`, and the window marker comes
  off the settings files, where it's the only place written down — and only when
  the two agree about which model this is, since a settings file naming
  `sonnet[1m]` says nothing about the opus in the transcript in front of us.
  Getting this wrong isn't a near miss: a 1M session at 11% full renders at 55%,
  a gauge lying in the direction that makes you close a session you didn't need
  to. `MFT_CONTEXT_SETTINGS_MODEL=0` trusts the transcript alone.
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

For the states that *animate*, that ramp goes on the rate instead: an ignored
permission gate strobes faster, climbing two steps over the same five minutes.
This isn't a flourish, it's the only channel available. Channels 3 and 6 carry an
animation **or** a brightness, never both, and `Twister.write` only sends a
brightness when there's no animation — so on a strobing encoder the brightness
never reaches the RGB at all. Since a blocking state also pins the ring at full,
neglect used to have nowhere to land: five minutes of ignoring a gate looked
exactly like five seconds. The escalation stays inside its own band, so a strobe
stays a strobe and a breathe stays a breathe.

A fully-neglected plan does end up strobing faster than a *fresh* permission
gate — the gate band has no room between the two base rates. What keeps them
apart is the motion budget: it ranks by state before debt, so with both on the
board the plan loses the fast animation outright and drops to a slow pulse. The
escalated rate only ever appears when the plan is the loudest thing there.

## Gestures

| Gesture | Does |
|---|---|
| press | focus that terminal tab, and forgive its attention debt |
| hold | peek: the rest of the bank becomes that session's last 15 tool calls |
| turn | nothing, beyond waking a sleeping board |

That's the whole input surface, and it's meant to be. Both gestures affect the
*board* — what you're looking at, what it's allowed to nag you about — and
neither affects a session.

## Banks, and the board following a prompt

Four banks of sixteen is sixty-four encoders, but only sixteen are on the front
panel at a time. Sessions fill from the first encoder and the allocator squeezes
them back down as they end, so with sixteen or fewer everything lives on bank 1
and none of this matters. Past that — or after you've wandered onto another bank
by hand — the board is showing you sixteen of sixty-four and not saying which
sixteen. **A permission gate three banks away is invisible**, and an empty bank
looks exactly like a dead daemon.

So a block on a bank you aren't looking at pulls the view onto itself. Everything
the escalating strobe buys is lost if the encoder isn't on the panel.

This is the one thing the daemon writes to the device for a reason other than
painting, and it's the closest anything here comes to acting on its own — so it's
braked three ways. It won't move within 30s of the last move, so two prompts on
two banks can't bounce the view between them and a bank you picked by hand stays
picked. It won't move during a peek, which is a modal view of one session's
history that another bank's encoders would silently replace. And it won't move
while the boot word or the waiting animation still owns the board.

It follows only the states where a human is the thing in the way —
`permission`, `plan`, `waiting` — and only unattended ones. Not `error`: a rate
limit resolves itself, and the board being wrong about which bank you want is
worse than a late red. Not `working`, which would move the view more or less
permanently for something you don't have to see.

It's still a display and not a control surface: a bank select changes which
sixteen encoders you're looking at, never a session. `MFT_FOLLOW_ALERTS=0` turns
it off, and `curl localhost:7654/status` reports the visible bank alongside each
session's own — which is the other answer to "why is my board empty".

Nothing on this hardware reports the current bank, so the daemon assumes bank 1
at startup and learns the truth from the first side button or the first followed
alert. The bank-select CCs are documented by DJTT and unverified per unit; run
`python -m mft.calibrate banks`, and if nothing moves, set `MFT_FOLLOW_ALERTS=0`
and leave the banks alone on that unit.

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

## When the *machine* sleeps

A different thing entirely, and easy to confuse with the section above: that one
is the board deciding nobody is there, this one is the Mac actually suspending.

macOS keeps the USB bus powered through sleep, so a board left lit **stays lit**
— close the lid on a session mid-turn and sixteen encoders go on describing it
at an empty desk all night. So the daemon darkens the board on the way down and
puts it back on the way up.

**Nothing is lost while you're away, and that is not luck.** `time.monotonic()`
on macOS is `mach_absolute_time`, which does not advance while the machine is
asleep, and every deadline in the daemon rides it. A nine-hour suspend is one
frame that took a moment longer than usual: no session ages, no TTL expires, no
`working` encoder decays into a stall it never had. The board you come back to
is the board you left. Nothing is missed on the hook side either — the sessions
are frozen along with everything else, so there are no events to miss.

What that frozen clock also means is that a suspend is undetectable from inside
the loop, so it has to be reported from outside. Two detectors, in `power.py`:

- **IOKit power notifications** (`IORegisterForSystemPower`, through `ctypes` —
  this project is not taking on a framework bridge for one callback). The only
  thing that fires *before* the machine goes down, which the darkening half
  needs; there is no blacking out a board after the fact. The handler consents
  to the sleep immediately and unconditionally, and never calls
  `IOCancelPowerChange`: vetoing a sleep is not a thing a display gets to do.
- **A clock comparison**, for when that fails to attach. `mach_continuous_time`
  counts time spent asleep and `mach_absolute_time` doesn't, so the gap between
  them grows only across a suspend. It can report a wake and never a sleep, and
  only after the fact — but after the fact is still in time to repaint.

Both fire for the same wake on a healthy machine, on purpose: the handler is
cheap and debounced, and the cost of trusting either one alone is a dead board.

On wake the de-dup cache is dropped before anything else, because a repaint the
cache suppresses is exactly as dark as no repaint at all. Then discovery runs
again — not a fix for anything sleep does, since no session can start or end
while the machine is down, just the table re-checked against the process table
after the one moment the daemon was provably blind.

**The board does not brighten on wake.** A dark wake — Power Nap, a backup —
looks identical to you opening the lid, and a board that comes up to full for a
3am backup is worse than one that resumes at the dim level it went down at. That
level is still exactly right, because the clock behind it froze too. Your first
keystroke in any session brings it back up, the way it always does.

**The port is the failure you'd actually see.** A sleep can leave the USB
endpoint invalid without closing it: every write raises, and the de-dup cache —
believing the device already holds what it last sent — would go on suppressing
the writes that would fix it even after the hardware came back. So a port that
has started refusing writes is reopened, cache and clock and all, every five
seconds until it takes. The same path covers a cable pulled out and pushed back
in an hour later.

`MFT_SLEEP_BLACKOUT=0` leaves the board glowing overnight, if it's somewhere you
want a nightlight. `MFT_WAKE_REDISCOVER=0` skips the rediscovery. `GET /status`
reports `suspended` and `port_failing`, which are the other two ways to be dark
and healthy.

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
nothing else — with a stub ring and no animation. They own no encoder of their
own, never take a claimed slot, and collapse back when the parent's turn ends.
The stub ring is deliberate: a subagent has no context reading of its own.
`MFT_SUBAGENT_STACK=0` turns them off.

Each dot **brightens on the tool calls its own subagent makes** and sinks back
between them — the same shimmer a session's encoder carries, and the same thing
it means: rate reads as how hard the pile is working, and one dot sitting at the
floor while its neighbours flicker is a subagent that has hung. Crucially it is
*per dot*, not per pile: a fan-out where one agent is grinding and four are
blocked looks different from five all working, which is the distinction you
actually want out of the corner of your eye. `MFT_SUBAGENT_SHIMMER=0` puts the
flat pile back — worth reaching for if you run wide fan-outs, since sixteen
independent decay curves in one corner is a lot of motion even when it's slow.

This works because every hook payload Claude Code writes carries `agent_id`, not
just the subagent events: a tool call made *inside* a subagent arrives as an
ordinary `PreToolUse` on the **parent's** `session_id`, with the subagent's id
alongside it. That is the entire per-subagent signal available, and it is enough
for exactly one thing — a level. There is no per-subagent state, notification or
permission to read, and a dot must never grow one: a violet pip that could go
alert-red would be a subagent impersonating a session.

A tool call whose `agent_id` names a subagent the daemon never saw start is
*ignored*, never counted. Nothing would ever retract such a record, so it would
hold a pip for the rest of the turn — invariant 6, and the same reasoning as the
unreadable `tool_use_id` below.

Pressing one raises the **parent's** tab, and holding one peeks at the parent —
the same two gestures the parent's own encoder answers. There is nothing finer
to aim at and there never will be: a subagent's whole identity here is an opaque
key on the parent, no subagent hook event carries a terminal, and it would not
matter if one did, because a subagent runs inside the parent's terminal. The
window a press could raise is the window the parent is already sitting in. So
the choice was only ever between a live target and four dead knobs — and a
fanned-out session is exactly the one you most want a big target for.
`MFT_SUBAGENT_PRESS=0` makes the pile inert paint again.

They used to breathe, on the slowest pulse the board had. That was a mistake for
a reason worth writing down: channel 3 carries an animation *or* a brightness
level, so a pulsing encoder runs at the hardware's own levels and sits near off
for most of every cycle — and a dim RGB LED reads blue whatever hue you send it.
The pile looked like faint blue pips, which is the one thing it must not look
like. Identification beat liveness.

The shimmer above is what liveness looks like once you accept that: it rides the
*brightness* ramp rather than the animation band, so the hue never leaves mid
violet and the dot stays identifiable at every level it passes through. Same
signal the abandoned pulse was reaching for, on the channel that could carry it.
It also stays clear of `arbitrate_motion` entirely — a level is not a rate, so
the pile cannot compete with the one fast animation the board allows itself, and
that animation belongs to whichever encoder a human is blocking on.

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

## The same state, in the tab strip

An encoder tells you a session wants you. It does not tell you *which window*,
once you have eight of them tiled — and the thing that does is already on
screen. So `mft/tab.py` puts a coloured glyph in front of each session's
terminal tab title:

| | State |
|---|---|
| 🟥 | permission gate — a tool call is waiting on you |
| 🟨 | a plan is written and wants a yes |
| 🔴 | error: rate limit, overload, billing |
| 🟠 | idle-waiting for input |
| 🔵 | busy — thinking, working, streaming |
| 🟢 | finished, and you haven't looked |
| ⚪ | idle |
| 🟣 | `bypassPermissions`, in every state, as on the board |

The mechanism is one `ESC ] 0 ; <title> BEL` written straight to the session's
tty. The daemon isn't that session's process, but it runs as the same user, so
opening `/dev/ttys004` and writing to it puts bytes into the emulator's parser
exactly as if the session had printed them — and OSC isn't printable, so nothing
appears on screen. Every terminal worth naming honours it, which is why this is
one code path and not an adapter table like focus.

**It is deliberately coarser than the board.** `thinking`, `working` and
`streaming` share one glyph, because they churn several times a second inside a
turn and every change is a write down someone's tty. Collapsed, a turn costs
three writes — busy, done, idle — instead of a few dozen. The repaint tick runs
at 5s (a two-hundredth of the frame rate) and then still compares against the
last line it *sent* before writing anything. What survives the collapse is the
only distinction a tab strip is any good at: is it asking me for something, is
it busy, is it finished.

**The title is Claude's own.** Claude Code writes its generated title into the
transcript as an `ai-title` record — the same string it would have put in the
tab — so the glyph goes in front of the real thing rather than something we
invented. Until a session has one, the directory name stands in. It's
model-written text going into an escape sequence, so it's stripped of C0
controls on the way through: a title containing a BEL would otherwise terminate
the sequence early and spill the rest of itself onto the screen.

**Claude Code has to be told to stop writing its own title**, and
`install_hooks.py` sets `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` to do it. Its
title carries an animated spinner, so while a turn runs it rewrites the title
several times a second and a glyph of ours survives for about a frame. Racing it
is exactly as bad as it sounds. This is the only thing installed here that
changes how a session behaves, and `--uninstall` puts it back; what you trade is
the spinner, and the title while the daemon is down.

Tabs are handed back — same title, no glyph — when a session ends and when the
daemon exits. A green dot on a tab whose daemon died an hour ago is precisely
the phantom this project spends its time avoiding.

All of it ships **off** (`config.TAB_TITLE`). It is the one thing the daemon
writes into a terminal it does not own, and the board already says everything
the tab strip would, so the default is to stay read-only outside this process.
`MFT_TAB_TITLE=1` turns it on.

## Configuration

Every tunable is in `mft/config.py` and env-overridable. The switches you'd most
likely want at runtime:

| Variable | Does |
|---|---|
| `MFT_SLEEP`, `MFT_SLEEP_SECONDS` | board sleep, and when the first stage lands |
| `MFT_TURN_RING`, `MFT_TURN_RING_SECONDS` | a working ring is the turn's length (`0` = the old tool-call arc), and what fills it |
| `MFT_CONTEXT_RING` | a resting ring is a context gauge (`0` = a bare pip) |
| `MFT_CONTEXT_RING_IDLE` | `0` keeps the gauge off the resting states |
| `MFT_CONTEXT_SETTINGS_MODEL` | `0` reads the context window off the transcript alone, never `settings.json` |
| `MFT_FOLLOW_ALERTS` | `0` stops the board following a block onto its bank |
| `MFT_SUBAGENT_STACK` | the violet pile in the corner |
| `MFT_SUBAGENT_SHIMMER` | `0` holds every dot at one level instead of brightening it on its own tool calls |
| `MFT_TAB_TITLE`, `MFT_TAB_TITLE_MAX` | the glyph in the terminal tab strip |
| `MFT_CLOCK_BPM` | MIDI clock; `0` stops sending it |
| `MFT_BOOT_ANIMATION`, `MFT_CLEAR_ANIMATION`, `MFT_SPAWN_ANIMATION`, `MFT_AMBIENT` | the decorative layers |
| `MFT_BOOT_UNWRAP` | `0` drops the unwrap that precedes the boot word, leaving CLAUDE alone |
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
.venv/bin/python -m mft.calibrate banks    # which channel-4 CCs actually switch bank
```

`banks` is the odd one out: it varies the CC *number* rather than the value,
because a bank select is one message per bank all at the same value, and you read
the answer on the front panel rather than in the lights. It paints each bank a
different colour first, so a board that changes colour is a bank that moved, and
the colour says which one it moved to.

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
