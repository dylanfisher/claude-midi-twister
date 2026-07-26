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
  hold  → peek at its history                  ring = how long it has been out
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
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | `working`, re-read the context gauge — and if it carries an `agent_id`, brighten that subagent's dot. A failure warms the hue toward red, a success cools it back |
| `Notification` | `permission` / `plan` / `waiting` / `done`, by `notification_type` — except the idle nag at a resting session, which is dropped |
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

Adopted sessions land in the state their transcript caught them in. That file is
not only a record of what a session did: Claude Code writes the assistant message
carrying a tool call as soon as it has it, which is *before* the tool runs, so a
session mid-turn has that call sitting unanswered at the end of its file. An
unanswered call, or a returned one, is `working`; a prompt nothing has replied to
is `thinking`.

Two things it won't do. It won't invent an **attention** state — a finished turn
is adopted at rest even though the transcript plainly says so, because `done`
opens a debt that gets more insistent the longer you ignore it and `permission`
strobes red, and neither should nag you about a turn that ended before the daemon
booted. And it can't *find* a permission prompt anyway: an agent blocked on your
approval and an agent halfway through a slow `npm test` write byte-identical
tails, since the request is the tool call and whether a human is being asked
about it is not in the file. Both read as `working`.

Freshness is what separates a live turn from an abandoned one — the process is
alive either way, so the sweep in [Sessions that outlive their
process](#sessions-that-outlive-their-process) has nothing to say about it, and
the file's own mtime is all that's left. Past `DISCOVER_ACTIVE_SECONDS` (three
minutes) the reading is dropped and the session lands `idle`. On a real desk that
gate does most of the work: of five transcripts sitting mid-turn here, one was a
session genuinely thinking and four were prompts abandoned an hour ago.

**The violet pile comes back too.** It is the state a restart loses hardest: a
daemon that missed a fan-out starting will hear every one of those subagents
*end*, and a pop for a dot it never put up buys nothing. Claude Code gives each
subagent its
own transcript, under `<session-id>/subagents/agent-<agent_id>.jsonl` beside the
parent's file, so `discover.subagents` reads each one exactly the way the section
above reads a parent: an unanswered `tool_use` at the end is a subagent still
out, a turn that stopped on `end_turn` handed its answer back. The id in that
filename is the same one hooks carry, so a rebuilt dot is indistinguishable from
one `SubagentStart` created and the eventual `SubagentStop` pops the right one.
That file's birth time is read alongside its mtime, so a rebuilt dot comes back
with its stopwatch already running — a restart into a fan-out that has been
grinding for half an hour would otherwise adopt a pile of empty rings, which is
the exact reading the stopwatch exists to give and the moment it is worth most.
The same freshness gate applies, for a sharper version of the same reason — a
subagent killed along with the daemon leaves a file frozen mid-call forever. A
pile that is already standing is never edited from a file, only an empty one
filled. `MFT_DISCOVER_SUBAGENTS=0` turns it off.

Their context ring is real, read from that same transcript. The first genuine
hook event takes over, and overwrites whatever was guessed. When two tabs sit in
the
same working directory, both keep an encoder but give up their terminal
identity, since guessing would hand the wrong knob to the next `/clear`. That
give-up is also how a wrong guess gets *out* again — see [where a record naming
nobody comes from](#where-a-record-naming-nobody-comes-from).

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
on the things the pid sweep can't have for free:

- **`discover.learn_pids`** gives a pid to the records that never got one, by
  matching them against processes that are running *right now* — argv's
  `--session-id`, then an identity token the hook and the process table both
  carry, then being the only Claude in a directory that only wants one. Every
  step must be unambiguous; a wrong pid is worse than none, since it would answer
  the sweep's question with somebody else's life. This is presence of evidence in
  both directions, and it shrinks the set below rather than judging it. What it
  writes on the record is the process's *whole* description, not just its
  number — see below.
- **`discover.relabel_hosts`** takes that back when it was wrong. A pid on its
  own is already a description: `{"pid": N}` is exactly what a session with no
  terminal of its own reports, so a record wearing nothing else gets filed under
  `host:N` rather than `pid:N` + `tty:`. Those are different namespaces on
  purpose and nothing compares across them, so such a record can never meet the
  tab's own record — and a live `host:` pid makes `orphans` skip the tty test as
  well. One terminal, two encoders, immune to every way the board has of letting
  a knob go. When a `host:` pid turns out to be sitting on a tty it was never a
  host, and this hands the tab back. Nothing is guessed: the pid is the record's
  own and only the tty and environment beside it are recovered. A genuine
  handoff is untouched, because the process it names is a `NOT_A_SESSION` helper
  that no census returns.
- **The tty half of `discover.orphans`.** A terminal tab holds its pty for
  exactly as long as it's open; close it and no process on the machine is on that
  tty again. So a session that named a tty whose tty is now free had its tab
  closed — whatever any pid says.
- **`discover.phantoms`**, which is arithmetic rather than a question about any
  particular process, and is the only thing that reaches a record naming
  *nothing*. Every live Claude in a directory that some **identified** record
  already claims — by pid or by tty — is spoken for; when all of them are, a
  record sitting in that directory with no tty, no pid and no terminal has
  nothing left to be. It concludes nothing in a directory where no Claude was
  recognised (that's recognition going stale, and it has to read as no evidence),
  it only ever releases a record that names nothing, and one nameless record can
  never account for another. It runs after the two sweeps above on the same
  census, so a record still holding a dead pid has already gone rather than
  counting as a claim.

That last one is the only *absence* anything here concludes from, and the
reason it's allowed to is that it recognises nothing. The earlier version of this
section refused to compare sessions against the live process table, because that
comparison rests on `claude_processes` still knowing what a session's argv looks
like, and the day that changes it clears the board. Reading which ttys are in use
needs no such knowledge — it's a column, not a command line. The census
self-checks the read before trusting the negative (`Census.usable`: a real
machine has hundreds of processes and at least one terminal, so a table with
neither is a read that went wrong, not a desk that emptied). A record with
*neither* a pid nor a tty is the one shape neither half can ask about, and it is
what `phantoms` above is for — but only where the counting settles it. Where it
doesn't, that record still keeps the hour.

### Where a record naming nobody comes from

Worth spelling out, because it looks impossible: every path that creates a
session writes down *something*. Adoption is the one that doesn't. The
transcript-to-process join is a guess whenever a directory holds more recent
transcripts than live Claudes, and it guesses newest-first — so a session that
exited twenty minutes ago beats one that has been parked at a prompt since
lunch, takes the live process that really belongs to the parked one, and lands
on the board. Then, because that directory matched more than once, the ambiguity
rule strips the terminal off everything it matched (deliberately — a wrong tab
must never be recorded), and what's left is a record for a session that no longer
exists, wearing no identity, immune to both of `orphans`' facts, holding an
encoder for the full hour. That is the knob `phantoms` was written for.

Nothing on disk links a transcript to a process id — the transcript never
mentions one, and a `claude` process holds no handle to its own file — so the
join cannot be made exact and the guess is not going away. What changed is how
long a wrong one lasts: half a minute, once the *real* session says which tab it
is. Until that happens the two records are genuinely indistinguishable from out
here, and the board keeps them both, which is the honest answer.

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

- **The idle nag doesn't take the floor back.** Claude Code posts an
  `idle_prompt` notification sixty seconds after every turn it finishes, so on a
  board you walked away from it is the most common notification there is. Landing
  it as `waiting` turned a session that had just gone green amber a minute later,
  with its ring pinned full — the shape that means *the agent is blocked on you*
  — for a session that has the floor and is doing nothing with it. At rest it now
  says nothing at all: the green `done` ramp already knows the turn ended, and
  fading is the honest way to say "a while ago". `agent_needs_input` is a real
  ask and stays amber, and the nag still lands on a session that *isn't* resting,
  where it's the only thing that knows the turn is over and amber beats an orange
  knob that's lying. See `events.is_idle_nag`.
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

- **A working encoder gets redder as its tool calls fail.** Everything else
  about `working` is a rate — the shimmer is calls per second, the ring is how
  long the turn has run — so an agent retrying the same failing edit six times
  looks exactly like an agent doing good work. It is the same orange, shimmering
  at the same speed, and it is the one you actually want to walk over to. So the
  hue carries how *well* it is going: one failed call warms it, three make it
  red, and every clean call after that cools it back a third of the way to
  orange. A turn that hits a wall and recovers visibly comes back.

  Hue and nothing else. It never pins the ring, never animates, never owes you
  attention and never pulls the bank onto itself — a failing agent is still
  working and hasn't become a thing that blocks you, and the alert vocabulary is
  reserved for the states where a human is actually in the way. It can't be
  confused with `error` for the same reason a glance settles it: `error` is
  solid red with a full ring, this shimmers with a stopwatch under it.

  Failures are read from `PostToolUseFailure` *and* from a `PostToolUse` whose
  response says it errored — the second because the failure hook is recent, so a
  settings file written before it reports every failure as an ordinary success
  and the board would never warm at all. Structured response keys only: an agent
  reading an error log is not an agent hitting errors. `MFT_FAILURE_HEAT=0`
  turns it off, and `curl localhost:7654/status` reports the heat as `failures`,
  in units of failed calls.

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
focus its tab, whether you got there by pressing its encoder or by switching to
the tab yourself ([the tab you're looking at](#the-other-direction-the-tab-youre-looking-at)).
Same for one that's been idle-waiting a while. Finished work is
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
the loop, so it has to be reported from outside. Three detectors, in `power.py`:

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
- **The display's power state** (`CGDisplayIsAsleep`), which is the one that
  carries the weight. A poll rather than a notification, so nothing can fail to
  deliver it; true through a suspend *and* through a dark wake, which is the
  distinction the other two cannot make; and 28 microseconds a call, which buys
  the right to just ask every second and stop reasoning about it.

All three fire for the same wake on a healthy machine, on purpose: the handler
is cheap and debounced, and the cost of trusting any one alone is a board that
lies.

**The board follows the screen.** Lit when you can see it, dark when you can't —
including the screen going off on its own idle timer with the machine still
awake, which is the one case none of this used to cover. `MFT_DISPLAY_BLACKOUT=0`
turns that off and leaves the notification as the only way down.

That rule is not a nicety; it is the fix for a real outage. This is what the log
looked like before it existed:

```
12:51:15  system sleeping; board dark
13:06:34  system awake (911s clock gap); repainting
13:22:54  system awake (931s clock gap); repainting
13:41:04  system awake (997s clock gap); repainting
          ... six more ...
```

One `will sleep` notification, ever. Ten wakes, every one of them from the clock
and none from IOKit. With `standby` and `powernap` on, the Mac was dark-waking
for maintenance every fifteen minutes; the fallback dutifully relit the board
for each one; and the notification that would have put it back never came again.
The board glowed at an empty desk for two and a half hours. Two things were
wrong, and both are fixed: **the notification thread was falling out of its run
loop** after a single delivery — `CFRunLoopRun` returns the moment the loop has
no sources left in it, and nothing re-entered it — and **a dark wake was allowed
to relight the board at all**. Now a wake only counts if the screen is on.

On wake the de-dup cache is dropped before anything else, because a repaint the
cache suppresses is exactly as dark as no repaint at all. Then discovery runs
again — not a fix for anything sleep does, since no session can start or end
while the machine is down, just the table re-checked against the process table
after the one moment the daemon was provably blind.

**The board does not brighten on wake.** Coming back is not the same as coming
back *up*: a board that returns to full brightness because a screen switched on
is worse than one that resumes at the dim level it went down at. After a suspend
that level is still exactly right, because the clock behind it froze too. Your
first keystroke in any session brings it back up, the way it always does.

**The port is the failure you'd actually see.** A sleep can leave the USB
endpoint invalid without closing it: every write raises, and the de-dup cache —
believing the device already holds what it last sent — would go on suppressing
the writes that would fix it even after the hardware came back. So a port that
has started refusing writes is reopened, cache and clock and all, every five
seconds until it takes. The same path covers a cable pulled out and pushed back
in an hour later.

`MFT_SLEEP_BLACKOUT=0` and `MFT_DISPLAY_BLACKOUT=0` together leave the board
glowing overnight, if it's somewhere you want a nightlight; the first alone only
gives up the notification, which was never the half that worked.
`MFT_WAKE_REDISCOVER=0` skips the rediscovery. `GET /status`
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
nothing else — and no animation at all. They own no encoder of their own, never
take a claimed slot, and collapse back when the parent's turn ends.
`MFT_SUBAGENT_STACK=0` turns them off.

Two things move on a dot, and both are quantities rather than states. The hue is
what identifies a subagent, and the hue never moves, so a level and a stopwatch
next to it are still unmistakable.

The **ring fills with how long that subagent has been out**: a quarter round is
ten minutes, half is twenty, all the way round is forty
(`MFT_SUBAGENT_RING_SECONDS`, raise it toward an hour if your fan-outs run long).
Past full it stays full, because a wrap would be ambiguous with a fresh spawn and
there is nothing you do at ninety minutes you didn't already do at forty.

Linear, deliberately, where a session's turn ring is log-scaled. A turn's ring is
a curve because nearly every turn is over inside two minutes and a linear scale
buries all of them at the floor; a subagent is spawned for precisely the work
that isn't, so its readings are spread across the whole span and the scale can be
one you read off the hardware without a curve in your head.

This is the reading you cannot get anywhere else. The parent's terminal shows one
line of `Task` output whether the subagent is thinking or wedged, and the pile
itself only says how many are out. A dot most of the way round, sitting at the
shimmer floor beside fresh ones, is a fan-out that went out and never came back —
legible from across the room. `MFT_SUBAGENT_TIME_RING=0` puts the old flat stub
back on every dot, which is also what a dot whose spawn nothing recorded wears.

Each dot also **brightens on the tool calls its own subagent makes** and sinks back
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

A restart mid-fan-out is the other way the pile goes missing, and that one is
recoverable from disk — see [sessions that predate the
daemon](#sessions-that-predate-the-daemon).

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

## The other direction: the tab you're looking at

Press-to-focus is the board pointing at a tab. `mft/attention.py` is the same
arrow reversed — switch to a Claude tab yourself and its encoder **swells once
and then holds its ring at full**, so the board stops being a list of agents and
becomes a map with a *you are here* on it.

The swell goes **out from dark**, not up from wherever the session was sitting.
The first version brightened what was underneath and never dimmed it, which
sounds right and made the gesture invisible on precisely the encoders worth
pointing at: a session already lit at full swallowed the whole thing. A pulse is
a change, not a level, and the only change left to an encoder that is already as
bright as the pulse is to go dark first. It falls back onto the session's own
level rather than onto black, because the overlay retires into that cell one
frame later.

**The swell is the RGB's alone; the ring doesn't move.** That split is the point
of the pair — the swell is the event, the ring is the state — and a marker that
flinches every time it's set is a worse marker.

It took two abandoned attempts to be sure of that. Swelling the ring *up* isn't
available: the marker has already pinned the focused encoder at full, so a
strike to full settling onto full is a four-frame ramp onto a level it was
already at, which reads exactly as "the ring doesn't pulse". Dipping it *down*
is available and legible — out over the knee, back over the tail — and still
wrong, because out-and-back is a different motion to the RGB's
strike-and-settle, and matching their durations doesn't make them one gesture.

Both were designed against a channel that was doing nothing at all (see
[calibrating](#calibrating), and `RING_ANIM_OFFSET` — channel 6's brightness
band was 48 values away from where this board was writing). The dip was only
judged for real once that was fixed, and lost on its merits.

The marker is on the **ring**, deliberately. Hue says what the session is doing,
and RGB brightness is already spent on how badly it wants you — and is discarded
outright while an animation is on it (see [attention debt](#attention-debt)), so
a marker there would be invisible on exactly the busy sessions worth finding.
The ring has its own channel and its own level, so it reads over a shimmer, a
sweep or a strobe without arguing with any of them. Ring *position* is left
alone too: it's a gauge, a stopwatch or an arc, and overwriting it would trade a
fact for a pointer.

And every *other* ring is held under a ceiling (`MFT_RING_CEILING`, 0.5) so that
full means something. "The brightest ring on the board" is only a place to look
if nothing else is up there with it, and before the cap a working session's ring
was allowed to sit at exactly the level the marker uses. The cap is a clip, not
a scale — the top comes off every ring and the bottom is left where it is,
because the dim end is where an encoder stops reading as claimed at all, and a
board of pips too faint to see is the failure mode that costs you a session
(invariant 6). Ring position is untouched by it; a gauge three-quarters full
still reads three-quarters full, under a lower roof. Overlays are painted after
the cap and ignore it, so the swell on arrival, a spawn strike and the boot word
all still reach full.

Arriving in a tab also does what a press does — forgives the attention debt,
clears the alert, resets the sleep clock. That's the line the debt section has
always claimed ("goes quiet the moment you focus its tab") finally being
literally true rather than a press standing in for it. Only on the *edge*,
though: sitting in a tab is not a standing amnesty, and a prompt that arrives
while you're looking at it is one you're ignoring.

### How it knows, and what it costs

There's no event to subscribe to, so it polls — in two layers, because the two
questions cost wildly different amounts.

| Question | How | Cost |
|---|---|---|
| which **app and window** is in front | `CGWindowListCopyWindowInfo` via `ctypes` | 0.33ms, no permission, no subprocess |
| which **tab** inside it | AppleScript for the selected tab's tty | ~60ms of subprocess, off the render thread |

The saving grace is that the second question usually doesn't get asked. **If the
app in front holds exactly one session on the board, the free answer is already
the exact answer** — and one Claude per terminal is the ordinary desk. The
AppleScript only runs to disambiguate two or more sessions in the *same*
application, only while that application is frontmost, and never merely to
*clear* the marker: switching to a browser is answered for free.

The free half reads the window *number* as well as the owner's name, and that is
what keeps the expensive half from being felt. An app switch and a window switch
are both edges it can see, and on an edge the AppleScript is asked immediately
rather than waiting out `ATTENTION_ASK_SECONDS` — so ⌘-tab and ⌘-` are both as
fast as the free poll. The one move left paying the floor is ⌘-{ between two tabs
of a *single* window holding two Claudes: they share a window, and the title that
would tell them apart is the one field the window server redacts. That case is
answered within 0.4s, which costs a subprocess two and a half times a second and
only while such a window is frontmost.

The cheap poll rides the render loop, which is what made the first version of
this feel late: a board with nothing moving on it drops to `IDLE_FPS` (1Hz), and
a still board is precisely the board you alt-tab *into* — an idle session, a
finished one, a prompt that has been sitting there. The poll's own clock never
got a chance to fire. So the loop holds its idle wait down to
`ATTENTION_POLL_SECONDS` while the marker is on: ten wakeups a second instead of
one, each of them a third of a millisecond of window list and a compose that
produces identical cells and therefore writes nothing to the wire. Three
milliseconds of CPU a second, against the thirty frames a second any animation on
the board already costs.

Those two clocks are now the *same* number, and that is a trap worth naming: the
poll's rate limit is checked against a frame time, so a frame landing a hair
early — scheduler jitter, or plain float subtraction, where `100.1 - 100.0` is
`0.0999999999` — would fail the gate and push the poll a whole interval out,
silently doubling the latency about half the time. The gate carries a little
slack (`attention._POLL_SLACK`) so the loop, which is the clock that actually
matters, sets the pace.

Only `kCGWindowName`, the window *title*, is redacted without Screen Recording
permission. The owner's name, the window layer and the window number, which is
all this needs, are not.

| Terminal | Can tell its own tabs apart |
|---|---|
| Apple Terminal | yes — `tty of selected tab of front window` |
| iTerm2 | yes — `tty of current session of current window` |
| Ghostty, kitty, WezTerm, Alacritty | one session: yes, free. Two: no marker. |

A terminal without a tab query isn't unsupported — it still gets the free
single-session answer, which is the common case. Adding one is a `Terminal(...)`
in `attention.TERMINALS`, the same shape as a focus adapter.

### What it refuses to guess

A missing marker is one you notice is missing; a wrong one moves your eye to a
knob where no work is happening (invariant 6). So the answer is "nobody" for:
two sessions in a terminal that can't be asked, a tty that names no session, two
records holding the same token (the state `reconcile` exists to repair — marking
one of them would be a coin toss), and a machine where the window server can't
be reached at all.

**tmux is the honest gap.** The tty Terminal reports is the *client's*, not the
pane's, so a multiplexed tab resolves to nothing. Marking the wrong pane would
be worse.

`GET /status` carries `focused` (the encoder, 1-based) and `focused_app` (what
the window server says is in front), which together are the whole diagnosis when
the marker is on the wrong knob or on none.

Three things were tried and dropped. Terminal focus reporting (`CSI ?1004h`) is
the native answer and is unavailable twice over — the daemon writes *down* a
session's tty but never reads it, and Apple Terminal doesn't implement the mode
anyway. An `AXObserver` on the terminal's process is genuinely event-driven and
wants an Accessibility grant, a dynamically allocated Objective-C class to carry
the callback, and then hands back a window *title* — a string `tab.py` is itself
writing into. A permission prompt and a feedback loop, to save a subprocess that
mostly doesn't run.

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
| `MFT_DISPLAY_BLACKOUT` | `0` stops the board following the screen off |
| `MFT_TURN_RING`, `MFT_TURN_RING_SECONDS` | a working ring is the turn's length (`0` = the old tool-call arc), and what fills it |
| `MFT_FAILURE_HEAT`, `MFT_FAILURE_HEAT_FULL` | a working encoder reddens as its tool calls fail, and how many failures reach full red |
| `MFT_CONTEXT_RING` | a resting ring is a context gauge (`0` = a bare pip) |
| `MFT_CONTEXT_RING_IDLE` | `0` keeps the gauge off the resting states |
| `MFT_CONTEXT_SETTINGS_MODEL` | `0` reads the context window off the transcript alone, never `settings.json` |
| `MFT_FOLLOW_ALERTS` | `0` stops the board following a block onto its bank |
| `MFT_ATTENTION_FOLLOW` | `0` stops the board marking the tab you're looking at |
| `MFT_ATTENTION_PULSE` | `0` keeps the standing ring marker but drops the swell on arrival |
| `MFT_ATTENTION_ATTENDS` | `0` leaves forgiving the debt to the encoder press alone |
| `MFT_RING_CEILING` | how bright an unfocused ring may get (`1.0` = no ceiling, the old board) |
| `MFT_SUBAGENT_STACK` | the violet pile in the corner |
| `MFT_SUBAGENT_SHIMMER` | `0` holds every dot at one level instead of brightening it on its own tool calls |
| `MFT_TAB_TITLE`, `MFT_TAB_TITLE_MAX` | the glyph in the terminal tab strip |
| `MFT_CLOCK_BPM` | MIDI clock; `0` stops sending it |
| `MFT_BOOT_ANIMATION`, `MFT_CLEAR_ANIMATION`, `MFT_SPAWN_ANIMATION`, `MFT_AMBIENT` | the decorative layers |
| `MFT_BOOT_UNWRAP` | `0` drops the unwrap, leaving whatever follows it alone |
| `MFT_BOOT_WORD` | `1` spells CLAUDE after the unwrap; off by default |
| `MFT_WHITE`, `MFT_DARK_COLOR`, `MFT_DARK_VALUE`, `MFT_RING_DARK_VALUE` | per-unit colour calibration |
| `MFT_RING_BRIGHTNESS_MIN`, `MFT_RING_BRIGHTNESS_MAX`, `MFT_RING_ANIM_OFFSET` | channel 6's brightness band, if your firmware moved it |
| `MFT_HOST`, `MFT_PORT` | where the daemon listens |
| `MFT_DISCOVER` | `0` is the same as `--no-discover` |
| `MFT_DISCOVER_SUBAGENTS` | `0` adopts sessions without rebuilding their violet pile |

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
dim encoder. Off is **18** (`DARK_VALUE`). Watch a candidate for a few seconds
before calling it off: the pulse rates just below look dark at a glance and then
swell back up.

**And channel 6's tables are not channel 3's.** They are channel 3's shifted up
by a whole block (`RING_ANIM_OFFSET`, 48): 49–56 gate, 57–64 pulse, **65–95
brightness**. So the ring's floor is 65 (`RING_DARK_VALUE`) and its ceiling 95,
and an RGB brightness value sent on channel 6 lands *below* the indicator band
and does nothing whatsoever.

That last sentence is the most expensive one in this file. This board sent 17–47
on channel 6 from the day it was written, on the entirely reasonable belief that
the ring's ramp was the RGB's ramp give or take a value — a belief that survived
being written down twice, in two separately-named constants, with a comment
warning that the two do not line up. What it meant in practice was that every
ring brightness the daemon ever computed reached the wire and stopped: the focus
marker, the `RING_CEILING` added to make that marker legible, the focus pulse,
the gauge's stale fade, the sleep dimming. None of them had ever done anything.

The symptom is worth recognising, because it does not look like a wrong number.
A wrong brightness looks like a wrong brightness. A value *below the band* looks
like the feature doesn't work — like a design that failed rather than a constant
that missed — and that sends you off rewriting animation curves, which is where
this one was eventually found. **If a ring feature does nothing, sweep the whole
of channel 6 before touching the code that paints it.** `mft.calibrate ring`
covers 0–127 and would have shown this on day one.

### The demo bench

`demo/index.html` is `mft.calibrate`'s interactive counterpart: a single page
that drives the device over Web MIDI, with no daemon, no Python and no port of
its own. Open it, click something, look at the board.

```sh
open demo/index.html                       # Chrome; see below if it refuses
```

It auto-connects to the first output whose name matches *twister*, so if the
board lights when you press a button you are done. If Chrome declines Web MIDI
on a `file://` URL, serve it instead — `localhost` is unambiguously a secure
context:

```sh
python3 -m http.server -d demo 8765        # then open http://localhost:8765/
```

Chrome only. Web MIDI is not in Safari, and Firefox needs it enabled by hand.
The first load asks for MIDI permission; if you dismiss that, the slider icon in
the address bar is where you take it back.

**Run it with the daemon stopped** — `.venv/bin/python -m mft.daemon --stop`.
Two CoreMIDI clients on one device is fine on macOS and the page will detect a
live daemon and say so, but the daemon repaints at 30Hz and will eventually
paint over whatever you sent. (It de-duplicates its writes, so a value on a
channel it isn't currently changing does survive — which is what makes the page
usable in a pinch while a daemon is up, and also what makes the results
confusing if you forget.)

What's on it, in the order the panels appear:

1. **Connection** — port pick, and the MIDI clock. Start the clock before
   judging any animation: the device takes its gate and pulse rates from
   incoming clock, and without one every encoder free-runs off its own timer and
   they drift apart, which reads as broken hardware. Also a blackout button,
   because a bench that leaves the board lit is a bench you stop trusting.
2. **Ring brightness, channel 6** — the single question that decides whether the
   ring marker, the `RING_CEILING` and the focus pulse work at all. A ladder
   (47 → 32 → 17), a blink, a one-value-at-a-time sweep of the whole ramp, and
   an A/B that puts the marker level next to the ceiling level on two adjacent
   encoders so you can answer "is that difference legible" rather than "is there
   a difference". Ring position is forced to full throughout, so there is
   something to dim.
3. **Target encoder** — which of the 64 everything else talks to, plus the bank
   select. Slot is also the CC number on every channel, and the pad shows both.
4. **The four channels** — a slider each for ring position, hue, ch3 and ch6,
   with the named colours from `config.COLORS` as buttons and the animation
   tables as dropdowns with their raw values spelled out.
5. **Gestures** — the real envelopes from `overlays.py` and `render.py`, ported
   to JS and played at 30fps: the focus pulse (and a ×4-slow version, for when
   half a second is too fast to tell what you're looking at), the spawn strike,
   the done flash and fade, the working shimmer, the log-scaled turn stopwatch.
   Each one prints the value stream it just sent, so what you saw and what went
   on the wire are side by side.
6. **The state vocabulary** — all ten session states painted across encoders
   1–10 at once, which is the fastest way to see whether two of them have
   drifted into looking like each other.
7. **Sweeps** — the same seven sweeps `mft.calibrate` offers, sixteen values at
   a time with next/prev paging.
8. **Raw CC and monitor** — anything the panels above don't cover, and an
   inbound log so you can watch presses and turns arrive.

The value tables in the page are a **mirror** of `config.py`, not a second
source of truth. A number that turns out to be wrong for your unit gets fixed in
`config.py` (or an `MFT_*` env var) and then copied back into the page.

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
