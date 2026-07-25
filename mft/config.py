"""Tunable constants for the Midi Fighter Twister visualizer.

Everything a different firmware revision or personal taste might change lives
here. The MIDI channel layout is DJTT's documented default; the *values* inside
the animation and colour tables vary a little between units, so use
``python -m mft.calibrate`` to sweep them on your own hardware and paste the
numbers you like back in.
"""

from __future__ import annotations

import os


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


# --- Daemon -----------------------------------------------------------------

HOST = os.environ.get("MFT_HOST", "127.0.0.1")
PORT = int(os.environ.get("MFT_PORT", "7654"))

#: Substring matched against available MIDI port names (case-insensitive).
PORT_MATCH = os.environ.get("MFT_PORT_MATCH", "twister")

#: Where the running daemon records its pid, so launchers can find it without
#: resorting to `pgrep -f`, which happily matches any shell that mentions the
#: daemon's name.
STATE_DIR = os.path.expanduser("~/Library/Application Support/ClaudeTwister")
PID_FILE = os.path.join(STATE_DIR, "daemon.pid")

#: Render loop rate. 30Hz is smooth for ring sweeps and cheap on USB bandwidth.
FPS = 30.0

#: The rate the loop falls back to once nothing on the board is moving -- see
#: :meth:`mft.daemon.Visualizer.run`. A hook event wakes the loop immediately,
#: so this is not the latency of anything you do; it is only how often a board
#: that is genuinely static asks itself whether it still is.
IDLE_FPS = 1.0

#: Consecutive identical frames before the loop drops to `IDLE_FPS`. Half a
#: second of hysteresis, because an animation can hold the same value for a
#: frame or two -- backing off on the first repeat would stutter the sweeps.
IDLE_FRAMES = 15

#: How often every LED ring is restated whether or not we think it changed, to
#: undo a ring the hardware lit by itself under a turned knob. Not every frame:
#: that is 64 messages a frame for a repair that only has to look instant.
RING_REFRESH_SECONDS = 0.25

# --- Sleep and wake ---------------------------------------------------------
# macOS keeps the USB bus powered while it sleeps, so a board left lit stays
# lit in an empty room; and `time.monotonic()` here does not tick through a
# suspend, so nothing in the render loop can notice on its own. See mft.power.

#: Darken the board when the machine goes to sleep, and light it again on wake.
#: Off leaves whatever was on the encoders glowing overnight -- which is a real
#: preference if the desk is somewhere you want a nightlight.
SLEEP_BLACKOUT = _flag("MFT_SLEEP_BLACKOUT", True)

#: Re-run discovery on wake. Cheap insurance rather than a fix for anything
#: sleep does: processes are frozen while the machine is, so no session can
#: start, end or change state in there. What it buys is a table re-checked
#: against the process table after the one moment the daemon was provably
#: blind, at the cost of a couple of `ps` calls per wake.
WAKE_REDISCOVER = _flag("MFT_WAKE_REDISCOVER", True)

#: How long a wake reported by one detector suppresses the other. Both fire for
#: the same wake by design (see mft.power); this is what keeps that from being
#: two rediscoveries.
WAKE_DEBOUNCE_SECONDS = 10.0

#: Slept-through time below this is a scheduling hiccup, not a suspend. Only
#: used by the clock fallback, which measures a wake rather than being told.
WAKE_MIN_SLEEP_SECONDS = 2.0

#: How often to retry a MIDI port that has started refusing writes -- a sleep
#: that invalidated the endpoint, or a cable pulled out. Not every frame: each
#: attempt enumerates the system's ports.
PORT_RETRY_SECONDS = 5.0

#: A session that has not sent any hook event for this long is presumed dead
#: (crashed terminal never fires SessionEnd) and its encoder is reclaimed. The
#: backstop, not the mechanism: a session whose process is *known* gone is taken
#: off the board within a reap by the orphan sweep below, and one whose *tab* is
#: known gone within a census. This hour is what is left over for the records
#: that named neither a process nor a tty, which is very few of them.
SESSION_TTL_SECONDS = 60 * 60

#: Release an encoder as soon as the pid behind it stops existing. Turning this
#: off costs an hour of a knob describing a tab you closed; the only reason to
#: is a diagnosis, since nothing else here can take a session off the board
#: without either a `SessionEnd` or that hour. See `mft.discover.orphans`.
ORPHAN_SWEEP = _flag("MFT_ORPHAN_SWEEP", True)

#: How often to read the whole process table on the board's behalf, which is
#: what catches the orphans no pid can: a record that named a tty and never a
#: pid, and a tab that has since closed. A subprocess, so not on the reaper's
#: five seconds -- but half a minute is still well inside "you closed it and
#: looked back at the desk". Off the render thread either way.
CENSUS_INTERVAL_SECONDS = 30.0

#: How few rows a `ps -e` has to return before that read is treated as broken
#: rather than as a quiet machine. The point is the *negative* the census draws
#: from an absent tty, and the one way that goes wrong is a read that came back
#: empty-ish for its own reasons; a real macOS process table is in the hundreds,
#: so anything under this is not a desk that emptied. See `mft.discover.Census`.
CENSUS_MIN_ROWS = 32

#: How long a finished session keeps its encoder lit before fading out. Three
#: minutes: long enough that a session that finished while you were in another
#: window is still on the board when you look back, and the fade itself is slow
#: enough to read as *going* rather than as another steady state.
DONE_FADE_SECONDS = 180.0

#: Sessions that ended cleanly hold their encoder this long. Slots are keyed on
#: the *terminal*, not the session id, so a `/clear` lands back on the same knob
#: even though it hands out a brand new session id; this window only covers a
#: full quit-and-restart in the same tab.
SLOT_LINGER_SECONDS = 120.0

# --- Sleep ------------------------------------------------------------------
# The board reports on agents, but it is lit for *you*, and the one thing it has
# no way of knowing is whether you are there. Every other fade here is about a
# session getting older; this one is about the room being empty. Nothing on the
# board wakes it -- an agent that works all night keeps sending events, so an
# empty desk is the only thing that ever reaches these timings.

#: Master switch. `MFT_SLEEP=0` if you want the board lit whatever happens.
SLEEP = _flag("MFT_SLEEP", True)

#: Nothing from any hook and no hand on any knob for this long: dim. Half an
#: hour is well past a pause in your own work -- reading a diff, a phone call --
#: and comfortably short of a lunch break, which is the first stretch where a
#: glowing board is decoration nobody is reading.
SLEEP_DIM_SECONDS = float(os.environ.get("MFT_SLEEP_SECONDS", 30 * 60))

#: And this long: off. The second stage exists because dim is still a board you
#: can read from the doorway, which is worth having when you step out, and is
#: still a lamp on your desk at 3am, which is not.
SLEEP_DARK_SECONDS = 60.0 * 60

#: Where the first stage lands. Low enough to read as asleep across the room,
#: high enough that the colours and ring positions all survive it, because the
#: point of stopping here rather than going straight out is that a dimmed board
#: is still a board.
SLEEP_DIM_LEVEL = 0.10

#: Each stage's fade. Slow on purpose, and much slower than anything else here:
#: every other fade is reporting something that happened, so it has to keep up
#: with the event. This one is reporting that nothing happened, and a board that
#: visibly snaps to a new level is a board that just said something.
SLEEP_FADE_SECONDS = 20.0

#: The way back up. Not instant -- a hard cut is the one transition this board
#: never makes, and coming back with a visible rise reads as waking rather than
#: as a frame dropping in -- but fast enough to be over before you have finished
#: turning to look at the knob you just pressed.
SLEEP_WAKE_SECONDS = 0.4

#: Below this the encoder is blanked rather than dimmed further, for the same
#: reason as SHUTDOWN_DARK_LEVEL: a hue at brightness zero is still a lit LED on
#: this hardware, and the tail of the fade has to reach an actual off.
SLEEP_DARK_LEVEL = 0.02

# --- MIDI channel layout (0-indexed, as mido wants) -------------------------

CH_ENCODER = 0  # ch1: encoder value / LED ring position
CH_SWITCH = 1  # ch2: switch RGB colour, and switch press input
CH_SWITCH_ANIM = 2  # ch3: RGB animation + RGB brightness
CH_SYSTEM = 3  # ch4: banks, side buttons
CH_SHIFT = 4  # ch5: shift-encoder
CH_RING_ANIM = 5  # ch6: ring animation + ring brightness

#: Encoders per bank, banks available. 4 x 16 = 64 addressable sessions.
ENCODERS_PER_BANK = 16
BANKS = 4
SLOT_COUNT = ENCODERS_PER_BANK * BANKS

#: Each bank is physically a 4x4 grid, which is also a 16-pixel display.
GRID_COLS = 4
GRID_ROWS = 4

# --- Colour table (channel 2 value) -----------------------------------------
# The Twister maps the whole 0..127 range onto a hue wheel that starts in the
# blues, so there is *no* off value here -- 0 is bright blue. A colour-free
# encoder is switched off on the animation channel instead
# (:meth:`mft.twister.Twister.rgb_off`). These are good-enough anchors; run the
# calibrator to taste.

#: Only meaningful as a hue; see the note above before using it to mean "dark".
COLOR_OFF = 0

#: The top of the wheel, where the hues run out and the LED goes achromatic.
#: Used for the boot word and as the resting hue of an encoder with nothing to
#: say -- the two places where the board should read as *lit* or *unlit* rather
#: than as any particular colour. Which value is actually white varies by unit:
#: run ``python -m mft.calibrate white`` and set ``MFT_WHITE`` to the one that
#: looks least like a colour.
#:
#: Judge it at full brightness. The wheel is only achromatic at one end, if at
#: all, and a dim RGB LED reads blue whatever hue you send it -- so a value
#: picked off a dark board is a blue that will announce itself the moment the
#: boot word lights it at full. This one is worth getting right twice: it is
#: both the word and the resting state of every unclaimed encoder, so an error
#: here is not one wrong pixel, it is a tint on the entire device.
WHITE = int(os.environ.get("MFT_WHITE", 127))

COLORS = {
    "white": WHITE,
    "blue": 1,
    "azure": 12,
    "cyan": 25,
    "spring": 37,
    "green": 48,
    "lime": 58,
    "yellow": 62,
    "amber": 68,
    "orange": 72,
    "red": 78,
    "magenta": 98,
    "purple": 110,
    "violet": 118,
}

# --- Animation table (channel 3 for RGB, channel 6 for the ring) ------------
# Low values are rate-based gate/strobe animations (slow -> fast); the band
# above them is a straight brightness fade. Same shape on both channels.

ANIM_NONE = 0
#: Gate/strobe rates, slowest to fastest, as documented in the MFT manual.
ANIM_GATE = {
    "1/8": 1,  # every 8 beats
    "1/4": 2,
    "1/2": 3,
    "1": 4,
    "2": 5,
    "4": 6,
    "8": 7,
    "16": 8,
}
#: Pulse (smooth breathe) rates, slowest to fastest.
ANIM_PULSE = {
    "1/8": 9,
    "1/4": 10,
    "1/2": 11,
    "1": 12,
    "2": 13,
    "4": 14,
    "8": 15,
    "16": 16,
}
#: The two rate bands, each slowest to fastest, derived from the tables above
#: rather than written out again -- a second copy of a vocabulary is how one entry
#: ends up in only one of them (see the note on STATE_PRIORITY below).
#:
#: Used to answer "what is the next rate up from this one, without leaving the
#: kind of animation it is": a gate that escalates into the pulse band stops being
#: a gate, and the difference between a strobe and a breathe is carrying meaning.
ANIM_BANDS = (tuple(ANIM_GATE.values()), tuple(ANIM_PULSE.values()))

#: Ring brightness ramp on channel 6: a linear fade from dimmest to full.
#:
#: Not the same band as the RGB's. The two channels share a layout in spirit and
#: not in numbers, which is exactly the trap below.
BRIGHTNESS_MIN = 17
BRIGHTNESS_MAX = 47

#: RGB brightness ramp on channel 3, and the one number on this device it is
#: worth reading the manual twice for.
#:
#: The animation table does not start the brightness ramp where the ring's does.
#: Channel 3 goes: 0 none, 1-8 gate, **9-17 pulse**, 18 brightness 0% (off),
#: 19-47 brightness 1..30. So 17 is not the bottom of the ramp at all -- it is
#: the *slowest pulse rate*, "brightness cycles over 16 beats".
#:
#: This board used to send 17 to mean off, which is why an idle encoder was
#: never actually dark: it was breathing, on a clock the daemon itself supplies,
#: so all sixteen of them started their cycle together the moment the daemon
#: came up and swelled and faded in unison behind the boot word. A dim RGB reads
#: blue whatever hue you send it, so what that looked like was a blue glow that
#: starts bright and dims. Not a colour bug: a value one below the one that
#: means off.
#:
#: 18 is off. Sourced from the Midi Fighter Twister user guide's animation
#: appendix ("Pulse | Value 9 - 17"; 18 = "RGB Brightness, 0 - Off"), and
#: overridable per unit if a firmware revision moves it --
#: ``python -m mft.calibrate dark`` sweeps the channel and shows you.
RGB_BRIGHTNESS_MIN = int(os.environ.get("MFT_RGB_BRIGHTNESS_MIN", 18))
RGB_BRIGHTNESS_MAX = int(os.environ.get("MFT_RGB_BRIGHTNESS_MAX", 47))

#: What "off" is on the RGB: genuinely off, the bottom of the ramp above.
#:
#: Not ``ANIM_NONE``: value 0 on that channel means *no animation*, which stops
#: overriding the device and lets it show its own inactive colour -- the blue a
#: stopped daemon used to leave glowing on the desk. And not 17, for the whole
#: reason above.
DARK_VALUE = int(os.environ.get("MFT_DARK_VALUE", RGB_BRIGHTNESS_MIN))

#: And what "off" is on the ring, which is a different channel with a different
#: table and therefore a different number. Named separately from
#: :data:`DARK_VALUE` because the two being the same integer was the assumption
#: that hid the pulse: one constant for two channels means the first one you get
#: right makes the second one look right too.
RING_DARK_VALUE = int(os.environ.get("MFT_RING_DARK_VALUE", BRIGHTNESS_MIN))

#: The hue a dark encoder wears. Moot while :data:`DARK_VALUE` is a real off --
#: an unlit LED has no colour -- but it is what the encoder wears for the one
#: message between the hue and the brightness landing, and if a unit turns out
#: to have no true off after all it is the difference between sixteen faint
#: white pips (a device at rest) and sixteen faint blue ones (a device still
#: trying to tell you something).
DARK_COLOR = int(os.environ.get("MFT_DARK_COLOR", WHITE))

#: Every gate and pulse rate above is measured in beats, and the Twister takes
#: its beat from incoming MIDI clock. Without a clock each encoder free-runs off
#: its own timer and they drift apart, which reads as broken hardware rather
#: than as design. The daemon sends clock so everything flashing flashes in
#: phase. Set to 0 to send none.
CLOCK_BPM = float(os.environ.get("MFT_CLOCK_BPM", "120"))

# --- Visual language --------------------------------------------------------
# One rule underpins all of it: you are not looking at this device. Peripheral
# vision catches *movement* and *hue change*; arc position and static colour are
# invisible until you turn your head. So motion is a budget, and it is spent on
# "a human is blocking progress" before anything else.

#: Every state a session can be in, ordered by how loudly it should shout for
#: attention. This is the vocabulary itself, not just a ranking: the per-state
#: tables below are keyed on it, and it is the tiebreak when several encoders
#: want the board's one fast animation (see ``mft.board.arbitrate_motion``).
#:
#: One list rather than several, because the states are deliberately *different*
#: from each other -- four flavours of blinking red trains you to ignore blinking
#: red -- and a second copy of the vocabulary is how one of them ends up with a
#: colour and no rank, which sorts it silently last.
STATE_PRIORITY = (
    "permission",
    "plan",
    "error",
    "waiting",
    "streaming",
    "working",
    "thinking",
    "done",
    "idle",
    "ended",
)

STATE_COLORS = {
    # Green is the "this session is yours again" hue, and it runs one continuous
    # ramp: solid bright at the end of a turn, fading down, resting dim. Idle and
    # done are the same colour on purpose -- the fade *is* the transition, so
    # there is no moment where the encoder changes hue and pulls your eye for
    # nothing.
    "idle": "green",
    "thinking": "cyan",
    "working": "orange",  # a parent agent is running; the ring is its context
    "streaming": "spring",
    "permission": "red",  # a gate is open, waiting on you: fast red strobe
    "plan": "yellow",  # a plan is written and wants a yes: yellow-green flash
    "waiting": "amber",  # agent is idling for input: slow amber breath
    "error": "red",  # rate limit, overload, billing: solid red, no motion
    "done": "green",
    "ended": None,
}

#: States that animate, and how urgently. Only one encoder on the board is
#: allowed the fast rate at a time; the rest are downgraded to SLOW_ANIM.
STATE_ANIM = {
    "permission": ANIM_GATE["4"],
    # Slower than a permission gate: a plan is a decision, not an interrupt, and
    # it is usually the thing you *want* to walk back over to.
    "plan": ANIM_GATE["2"],
    "waiting": ANIM_PULSE["1/2"],
    "streaming": ANIM_PULSE["2"],
}
#: What a downgraded animation becomes.
SLOW_ANIM = ANIM_PULSE["1/4"]

#: Idle sessions sit dim so the active ones pop.
IDLE_BRIGHTNESS = 0.22
ACTIVE_BRIGHTNESS = 1.0

# --- Plan approval ----------------------------------------------------------
# There is no PlanReady hook. A finished plan arrives as an ordinary permission
# request for the tool that exits plan mode, and/or as a Notification whose only
# distinguishing feature is its prose. Both are recognised here, because "your
# plan is ready" and "may I run rm" want very different things from you and
# should not be the same red strobe.

#: Tools whose permission request *is* a plan approval.
PLAN_TOOLS = ("ExitPlanMode", "exit_plan_mode")
#: Matched against a lowercased Notification message when there's no tool name.
#: Phrases rather than the bare word "plan", which shows up in prompts about
#: files called plan.md often enough to matter.
PLAN_MESSAGE_TOKENS = (
    "ready to execute",
    "like to proceed",
    "plan is ready",
    "written up a plan",
    "approve this plan",
    "exit plan mode",
)

#: Sessions running with `--dangerously-skip-permissions` get a hue reserved for
#: nothing else, on every state, so you never have to wonder which agent is
#: unattended.
UNSUPERVISED_COLOR = "magenta"

# --- Activity ---------------------------------------------------------------

#: The ring advances one segment per PostToolUse, so an active session visibly
#: rotates and its spin rate *is* its tool-call frequency. A ring that stops
#: turning is a stuck session -- the failure mode that is invisible in a
#: terminal until you go looking.
ARC_SEGMENTS = 16

# --- Context window ---------------------------------------------------------
# A *resting* agent's ring is a fuel gauge: it fills as the context window fills,
# so "this one is about to compact" is visible from across the room. No hook
# payload carries token counts, so :mod:`mft.context` reads them out of the
# transcript the payload points at.
#
# Resting, and not while it works, because that is when the number is worth
# something. "How full is the window" is the question you ask deciding whether to
# carry on in this session or start a fresh one -- which is a thing you decide
# between turns, standing in front of the board. During a turn it barely moves
# and you can do nothing about it either way, so the ring spends the turn on
# :data:`TURN_RING` instead and comes back to the gauge when the turn ends.

CONTEXT_RING = _flag("MFT_CONTEXT_RING", True)
#: Re-read a session's transcript at most this often. Events arrive far faster
#: than the context meaningfully moves, and this is a file read on the HTTP
#: thread.
CONTEXT_POLL_SECONDS = 2.0
#: How much of the tail to read. Transcripts run to megabytes; the last
#: assistant message is always within a few KB of the end, and the extra is
#: slack for one very large tool result.
CONTEXT_TAIL_BYTES = 512 * 1024

#: Fallback window when the model is unknown or unrecognised.
CONTEXT_LIMIT_DEFAULT = int(os.environ.get("MFT_CONTEXT_LIMIT", "200000"))
#: Substring -> window size, first match wins. Deliberately matched on
#: substrings so a new dated model id doesn't need a release here.
CONTEXT_LIMITS = (
    ("haiku", 200_000),
    ("sonnet", 200_000),
    ("opus", 200_000),
)

#: The long-window variants, matched before the families above. Kept in their own
#: table because they are the *only* thing :mod:`mft.context` goes looking in the
#: settings files for -- a family it can read off the transcript, a window marker
#: it very often cannot.
CONTEXT_WINDOW_MARKERS = (
    ("[1m]", 1_000_000),
    ("-1m", 1_000_000),
)

#: Believe the settings files about which window a model has when the transcript
#: doesn't say.
#:
#: The transcript records the model of every assistant message, which is the
#: right source -- it follows a mid-session `/model` -- but it records it as
#: `claude-opus-5`, with no marker, whether or not you are on the 1M variant.
#: `~/.claude/settings.json` is where `opus[1m]` actually lives. So the marker is
#: taken from there, and only ever *from* there: the family still comes off the
#: transcript, and a settings model of a different family is ignored rather than
#: believed over the message in front of us. Without this a 1M session reads as
#: five times as full as it is, which is worse than no gauge -- it is a gauge
#: that lies in the alarming direction.
CONTEXT_SETTINGS_MODEL = _flag("MFT_CONTEXT_SETTINGS_MODEL", True)

#: Where to look, nearest first: Claude Code's own precedence, minus the ones
#: this can't see (an enterprise policy file, a `--model` on the command line).
#: A session's `cwd` and its parents supply the project half.
CONTEXT_SETTINGS_FILES = (
    ".claude/settings.local.json",
    ".claude/settings.json",
)
CONTEXT_SETTINGS_USER = os.path.expanduser("~/.claude/settings.json")
#: How far up from a session's `cwd` to look for a project settings file. Bounded
#: so a session run from a deep subdirectory costs a handful of stats, not a walk
#: to the root.
CONTEXT_SETTINGS_DEPTH = 8

#: Below this the ring would be a stub too short to read as a gauge, so an
#: agent with a nearly empty context still shows a legible pip.
CONTEXT_RING_FLOOR = 4

#: Show the gauge in the resting states too, not only while an agent is running.
#:
#: A session idling at 95% is the one you have to deal with before it compacts on
#: you, and it used to look exactly like a fresh one -- both were the same dim
#: green pip. Costs nothing anywhere else: ring position is channel 1, which is
#: neither the animation channel nor the brightness channel, so this is the one
#: signal on the board that can be added without taking something away.
#:
#: Only where the ring has nothing better to say. `thinking` and `streaming`
#: sweep, and motion outranks a level; the blocking states pin the ring at full,
#: where it means "you" rather than "tokens".
CONTEXT_RING_IDLE = _flag("MFT_CONTEXT_RING_IDLE", True)

#: A resting gauge fades to this over :data:`DONE_FADE_SECONDS`, so a reading
#: carries its own age.
#:
#: The number stays on the ring -- you can still read it by going and looking --
#: but it stops competing for your eye with a session that finished a minute ago.
#: Deliberately not zero: an unlit gauge and a session with no reading at all
#: would be the same encoder, and those are very different things (invariant 6).
#:
#: This is the ring's *own* level, which is what makes it possible at all: the
#: ring's brightness is channel 6 and the RGB's is channel 3, so the gauge can go
#: quiet while the hue behind it does the opposite. A `done` session that you
#: never come and look at ramps back up on attention debt; its gauge does not
#: come with it, because the reading did not get any more urgent, only older.
GAUGE_STALE_LEVEL = 0.08

# --- Turn elapsed -----------------------------------------------------------
# What the ring says while an agent is actually running.

#: How long this turn has been going, as a ring that fills.
#:
#: This is the one thing about a running agent you cannot see from anywhere else.
#: Colour says what it is doing, brightness says how recently it called a tool
#: and sags when it stops -- but nothing said *how long*, and "which of these six
#: has been grinding for twenty minutes" is the question you are actually asking
#: when you look at a board full of orange.
#:
#: `MFT_TURN_RING=0` puts the tool-call arc back (:func:`mft.render._arc_ring`),
#: which is what this replaced. The arc is a fine thing to watch and a redundant
#: one to spend the ring on: its spin rate is tool-call frequency, and tool-call
#: frequency is already the brightness shimmer.
TURN_RING = _flag("MFT_TURN_RING", True)

#: A full ring. Past it the ring simply stays full, which reads correctly: the
#: difference between twenty minutes and forty is not one you act on differently.
TURN_RING_FULL_SECONDS = float(os.environ.get("MFT_TURN_RING_SECONDS", 900.0))

#: The knee of the log curve, in seconds -- roughly, how long a turn runs before
#: the ring is visibly off its floor.
#:
#: Linear was unreadable: nearly every turn lives in the first two minutes, which
#: on a linear ring to fifteen is the bottom eighth and indistinguishable from
#: the floor. Log spends the first quarter of the ring on the first half-minute
#: and the last quarter on the last ten, so short turns are legible and long ones
#: still have somewhere to go.
TURN_RING_KNEE_SECONDS = 20.0

# --- Terminal tab -----------------------------------------------------------
# The same state, in the one other place you are already looking: the tab strip.
# See :mod:`mft.tab` for how the title is written and why Claude Code has to be
# told to stop writing its own.

#: Off by default. This is the one thing the daemon writes into somebody else's
#: terminal, and the board already says everything the tab strip would; leaving
#: it dark keeps the daemon read-only outside its own process until you ask.
#: `MFT_TAB_TITLE=1` turns it back on -- everything downstream (the poll, the
#: glyphs, the hand-back on exit) is still here and still tested.
TAB_TITLE = _flag("MFT_TAB_TITLE", False)

#: One glyph per state, prefixed to the title. Deliberately *coarser* than
#: STATE_COLORS: `thinking`, `working` and `streaming` share a glyph because
#: they churn several times a second inside one turn, and every change is a
#: write down someone's tty. Collapsed, a normal turn costs three writes -- busy,
#: done, idle -- instead of a few dozen. What survives the collapse is the only
#: distinction a tab strip is any good at: is it asking me for something, is it
#: busy, is it finished.
#:
#: Circles for the ones that pass on their own and squares for the two that
#: don't, so the difference is legible in monochrome and at tab-strip size.
TAB_GLYPHS = {
    "permission": "\N{LARGE RED SQUARE}",
    "plan": "\N{LARGE YELLOW SQUARE}",
    "error": "\N{LARGE RED CIRCLE}",
    "waiting": "\N{LARGE ORANGE CIRCLE}",
    "thinking": "\N{LARGE BLUE CIRCLE}",
    "working": "\N{LARGE BLUE CIRCLE}",
    "streaming": "\N{LARGE BLUE CIRCLE}",
    "done": "\N{LARGE GREEN CIRCLE}",
    # Not green: `done` and `idle` are one colour on the board because the fade
    # between them *is* the transition, and a tab strip has no fade. Here they
    # have to be two glyphs or the distinction is lost -- and "finished
    # something you haven't looked at" is most of why you'd glance at the tab.
    "idle": "\N{MEDIUM WHITE CIRCLE}",
    #: No glyph, and the bare title written back: an ended session's tab is not
    #: ours to keep painting.
    "ended": "",
}
#: As on the board: reserved for nothing else, on every state.
TAB_UNSUPERVISED_GLYPH = "\N{LARGE PURPLE CIRCLE}"

#: Repaint at most this often. Nothing here is animated and a tab strip is not
#: something you watch, so this runs much slower than the
#: board -- and even then it usually writes nothing, because the composed title
#: is compared against the last one sent.
TAB_POLL_SECONDS = 2.0

#: Titles are truncated to this many characters before the glyph goes on. Tab
#: strips truncate anyway; doing it here keeps the escape sequence short enough
#: to be one atomic write down a tty someone else is also writing to.
TAB_TITLE_MAX = int(os.environ.get("MFT_TAB_TITLE_MAX", "64"))

# --- Discovery on startup ---------------------------------------------------
# Hooks only push, so a session that was already running when the daemon started
# stays invisible until it happens to fire its next event -- which, for an agent
# sitting at a prompt waiting for you, is never. :mod:`mft.discover` reconstructs
# those sessions from the two things the machine can still be asked: the
# transcripts on disk and the process table.

DISCOVER_ON_START = _flag("MFT_DISCOVER", True)
#: Where Claude Code keeps one directory of transcripts per project.
CLAUDE_PROJECTS_DIR = os.path.expanduser(
    os.environ.get("MFT_PROJECTS_DIR", "~/.claude/projects")
)
#: Only transcripts touched this recently are candidates. Matches
#: SESSION_TTL_SECONDS: a session the daemon would reap for silence is not one
#: worth lighting up at boot.
DISCOVER_WINDOW_SECONDS = SESSION_TTL_SECONDS
#: Enough tail to find `cwd`/`sessionId`, which appear on nearly every entry.
#: Small because this runs over every recent transcript, not just one.
DISCOVER_TAIL_BYTES = 64 * 1024
#: `ps` and `lsof` are the liveness check. If they hang, the boot sequence is
#: what hangs with them, so they get a short leash.
DISCOVER_TIMEOUT_SECONDS = 5.0

#: A tool call briefly kicks the encoder to full brightness; it decays back to
#: ACTIVE_FLOOR over this long, so frequency reads as shimmer.
TOOL_KICK_SECONDS = 1.2
ACTIVE_FLOOR = 0.55

#: Working with no tool call for this long is a stall. Brightness sags toward
#: the idle floor over STALL_FADE_SECONDS after that.
STALL_SECONDS = 45.0
STALL_FADE_SECONDS = 120.0

# --- Attention debt ---------------------------------------------------------
# The interesting thing to encode is not agent state but *your neglect*. A
# session that wants you and does not get you gets slowly more insistent, and
# goes quiet the moment you focus its tab.

#: Debt saturates after this long unattended.
ATTENTION_RAMP_SECONDS = 300.0
#: Brightness at zero debt and at full debt, for states that carry debt.
ATTENTION_FLOOR = 0.45
ATTENTION_CEILING = 1.0
#: A finished-but-unvisited session ramps too, but capped well below a genuine
#: block so it can never outshout one.
DONE_DEBT_CEILING = 0.5

#: How many rate steps a fully-neglected encoder climbs inside its own band.
#:
#: Rate rather than level, because on the states that carry debt the level cannot
#: do this job at all. Channels 3 and 6 carry an animation *or* a brightness, and
#: `Twister.write` only sends `rgb_brightness` when there is no animation -- so a
#: strobing encoder's brightness never reaches its RGB. Since the blocking states
#: also pin the ring at 127, five minutes of debt on a permission gate used to
#: change nothing you could see. This is the one quantity on the board that
#: describes you, and it was the one that failed to arrive.
#:
#: Two, not eight: the band holds eight rates, and climbing all of them turns a
#: five-minute-old prompt into a seizure. Two is one visible step, twice.
ATTENTION_ANIM_STEPS = 2

# --- Banks ------------------------------------------------------------------
# Sessions fill from slot 0 and `_compact` squeezes them back down, so with
# sixteen or fewer of them everything lives on bank 1 and none of this matters.
# Past that -- or after you have wandered onto another bank by hand -- the board
# is showing you sixteen of sixty-four encoders and not saying which sixteen. A
# permission gate three banks away is invisible, and an empty bank looks exactly
# like a dead daemon.

#: Bank select on channel 4, one CC per bank. Documented by DJTT and unverified
#: per unit: run `python -m mft.calibrate banks` and watch which one moves the
#: board. An empty tuple disables every bank gesture, which is also the honest
#: setting for a unit where the sweep found nothing.
BANK_SELECT_CC = (0, 1, 2, 3)
BANK_SELECT_VALUE = 127

#: Pull the device to the bank where a human is blocking. The whole promise of
#: the board is that a prompt is visible; a prompt on a bank you are not looking
#: at is not, and no amount of brightness or rate on the encoder fixes that.
#:
#: This is the one thing the daemon writes to the device for a reason other than
#: painting. It is still a display and not a control surface -- a bank select
#: changes which sixteen encoders you see, never a session -- but it is the
#: closest thing here to the device acting on its own, hence the cooldown.
FOLLOW_ALERTS = _flag("MFT_FOLLOW_ALERTS", True)
#: Which states are worth moving the view for: the ones where a human is the
#: thing standing in the way. Not `error` -- a rate limit resolves itself and the
#: board being wrong about which bank you want is worse than a late red. Not
#: `working` or `done`, which would move the board constantly and for nothing.
FOLLOW_STATES = ("permission", "plan", "waiting")
#: ...but never argue with a hand. A side button just pressed means you chose
#: this view, and that choice outlives one notification. Also the floor on how
#: often the view can move at all: two prompts arriving together should not
#: bounce the board between their banks.
FOLLOW_ALERT_COOLDOWN_SECONDS = 30.0

# --- Input ------------------------------------------------------------------

#: A press held longer than this peeks at the session instead of focusing it.
HOLD_SECONDS = 0.6

# --- Peek -------------------------------------------------------------------

#: Holding an encoder re-renders the rest of its bank as that session's recent
#: tool calls, oldest to newest, hue by tool kind.
PEEK_HISTORY = ENCODERS_PER_BANK - 1
TOOL_COLORS = {
    "Read": "blue",
    "NotebookEdit": "orange",
    "Edit": "orange",
    "Write": "orange",
    "Bash": "magenta",
    "BashOutput": "magenta",
    "Grep": "cyan",
    "Glob": "cyan",
    "WebFetch": "spring",
    "WebSearch": "spring",
    # Same hue subagents themselves get on the live board; a Task call in the
    # history and a subagent on the board are the same event seen twice.
    "Task": "violet",
    "Agent": "violet",
}
TOOL_COLOR_DEFAULT = "azure"

# --- Boot / shutdown --------------------------------------------------------
# 4x4 is a 16-pixel display and ring brightness is real grayscale per pixel, so
# a letter can strike at full and decay to black instead of hard-cutting off.
# The gap that leaves is what separates one letter from the next. Shutdown does
# not spell anything -- a colour wipe off both corners is how you know the
# daemon exited cleanly rather than died, and it costs a fraction of the time a
# legible word does.
#
# The word lights no RGB, nor do the waiting gradients after it or the ambient
# layer underneath both: white light moving on a dark board. Colour is how this
# device means things, and the stretch where it has nothing to say is the
# stretch where the hue channel has no business being on. The two bookend
# gestures -- the unwrap on the way in, the spiral on the way out -- are the
# exception, and they are one on purpose: each wears a single hue for its whole
# length, blue in and violet out, which is how you tell a daemon starting from a
# daemon leaving across the room.

BOOT_WORD = "CLAUDE"
#: Deliberately unhurried. The boot animation is the only time the device says
#: anything in words, and a 0.3s-per-letter version reads as a flicker you catch
#: the tail of rather than as CLAUDE: by the time you look up it is over. Each
#: letter strikes in at full brightness and then decays to black over a full
#: second before the next one strikes, which is both what makes 16 pixels
#: resolve into a glyph and what separates one letter from the next.
BOOT_FADE_SECONDS = 0.75
#: Time at full before the decay starts. Zero: the strike *is* the punctuation,
#: and a plateau on top of it only makes the word longer, not more legible.
BOOT_HOLD_SECONDS = 0.0
#: ``None`` is not "no colour" here, it is white -- and it is the only way to
#: ask for white on this hardware. Channel 2 is a hue wheel with no achromatic
#: value anywhere on it: it wraps, 48 and 127 both land on green, so the top of
#: the range is not "the hues run out", it is just more hues. Every value you
#: can send it is a colour, and a letter wearing one reads as a status rather
#: than as text.
#:
#: The ring is not a hue. A ring at full with the RGB switched off is a white
#: block, which is exactly the glyph pixel this wants -- so the word is spelled
#: in light rather than in colour. Set this to a name from COLORS to tint it.
BOOT_COLOR = None

#: Before the word: the exit gesture, run backwards. The board fades up whole in
#: white, holds, and then unwraps from the centre outward along the shutdown
#: spiral reversed, so the daemon arrives by undoing exactly the shape it leaves
#: on. It is the same argument as the word being unhurried -- this is the one
#: moment the device is allowed to be a thing rather than a status -- and it is
#: the reason the two gestures share `spiral_path` rather than each having a
#: path of their own.
BOOT_UNWRAP_ANIMATION = _flag("MFT_BOOT_UNWRAP", True)
#: The whole board coming up in unison -- the inverse of `SHUTDOWN_FADE_SECONDS`
#: and shorter than it, because arriving is allowed to be brisker than leaving:
#: the word is still to come and the total is what you actually wait through.
BOOT_UNWRAP_RISE_SECONDS = 1.0
#: The full board, whole and still, before it starts coming apart. Mirrors
#: `SHUTDOWN_HOLD_SECONDS`: without it the rise runs straight into the unwrap and
#: the board is never once seen complete.
BOOT_UNWRAP_HOLD_SECONDS = 0.5
#: Time for the head to walk all 16 encoders, centre to corner. Mirrors
#: `SHUTDOWN_SPIRAL_SECONDS`.
BOOT_UNWRAP_SPIRAL_SECONDS = 1.4
#: How long one encoder takes to go dark once the head leaves it. Overlaps the
#: travel, for the same reason the shutdown rise does: an edge moving, not
#: sixteen lamps switching off one at a time.
BOOT_UNWRAP_FALL_SECONDS = 0.3
#: The one hue the arrival wears, the way `SHUTDOWN_COLOR` is the one hue the
#: exit wears -- blue against the exit's violet, so the two ends of a run are
#: told apart at a glance rather than only by which direction the spiral went.
#: This is the exception to boot being colourless: the rule is about the *word*
#: and the field after it, which say nothing and so light no RGB. The bookend
#: gestures are not statuses either, but they are the device announcing itself,
#: and both are over before the first letter strikes. The rings still carry the
#: light -- the hue sits underneath them, exactly as it does on the way out.
BOOT_UNWRAP_COLOR = "blue"

# --- Waiting ----------------------------------------------------------------
# What the board does after the word, while no Claude is running. It used to be
# a lamp test -- an arc that lit every ring on all 16 encoders at full, then
# dissolved into a generative interference field at the same level. It read as a
# state: sixteen bright knobs is what this device looks like when it has a great
# deal to tell you, and it was saying that about an empty board. The replacement
# says the opposite thing on purpose: broad white gradients drifting across the
# grid, nothing near full, nothing sharp enough to be an event. A room tone.
#
# Colourless, like the word before it. Boot lights no RGB from start to finish
# -- colour is how this device means things and here it has nothing to mean.

#: Ceiling on the whole thing, before the fade envelope. Roughly a lit-but-idle
#: encoder, and deliberately under `ACTIVE_BRIGHTNESS`: the first real session to
#: appear has to be brighter than the wallpaper it appears on.
WAITING_BRIGHTNESS = 0.30
#: Seconds for a gradient to travel the diagonal. Slow enough that you cannot
#: watch it move -- you look back and it has moved. Two of these run at once,
#: this one and `WAITING_PERIOD_SECONDS * 1.61`, which share no common factor,
#: so the pair never lines up twice and the board never settles into a loop your
#: eye can finish.
WAITING_PERIOD_SECONDS = 9.0
#: Width of a gradient as a fraction of the axis it travels. Wide: at 0.2 it is
#: a band sweeping past, which is a thing happening, and nothing is happening.
WAITING_WIDTH = 0.55
#: The way in. The gradients are mid-travel at t=0 -- they have no start, that
#: is the point of them -- so without this the board goes from black to a third
#: brightness in one frame, which is exactly the kind of event this animation
#: exists not to be. Long enough that you cannot say when it began.
WAITING_FADE_IN_SECONDS = 2.5
#: How long it runs, fading the whole way, if no session ever shows up. Longer
#: than the lamp test's minute because it is a third the brightness, short
#: enough that a machine left on overnight still ends up dark rather than
#: glowing at you from the desk. `AMBIENT` is what remains underneath.
WAITING_SECONDS = 180.0
#: A session appeared: the gradients get out of the way, but over a couple of
#: frames rather than by hard-cutting, so the first encoder to light reads as
#: emerging from the field rather than as the field glitching.
WAITING_DISMISS_SECONDS = 0.8
#: Frames of render loop between the boot word ending and the gradients starting.
#: Discovery has already run by then, but a hook from a session that started
#: while the word was on screen has not necessarily landed -- and the waiting
#: animation appearing for two frames on a board that was never empty reads as a
#: glitch. Cheap insurance: nobody notices a tenth of a second of black, and the
#: check is repeated every frame until it fires, so a slow hook only delays it.
WAITING_START_DELAY_SECONDS = 0.1
BOOT_ANIMATION = _flag("MFT_BOOT_ANIMATION", True)

# --- Session spawn ----------------------------------------------------------
# A new session claiming an encoder is otherwise the quietest event on the
# board: `idle` is a dim green pip, which is exactly what the knob next to it
# looks like. So the moment of claiming gets its own brief, unmistakable strike
# -- the one time an encoder is allowed to be both full brightness and moving
# fast without a human blocking anything. It is short on purpose: long enough to
# catch out of the corner of your eye, over before it becomes a status.

SPAWN_ANIMATION = _flag("MFT_SPAWN_ANIMATION", True)
#: Total length of the strike. Under two seconds: this is punctuation.
SPAWN_SECONDS = 1.8
#: Held for the whole strike at full brightness. Red is otherwise reserved for
#: permission and error, but neither of those blinks the *ring* -- and three
#: hard flashes in a row is a shape nothing else on the board makes, so there is
#: nothing for it to be confused with even sharing the hue.
SPAWN_COLOR = "red"
#: The ring blinks full-on/full-off this many times before the handover. A
#: count, not a sweep: you can read "three" out of the corner of your eye
#: without having watched the whole thing.
SPAWN_FLASHES = 3
#: Fraction of the animation spent flashing. The remainder is the handover to
#: the session's own steady state.
SPAWN_SETTLE = 0.55

#: Shutdown is a colour wipe, not a word. One head leaves the top-left corner
#: and spirals inward to the centre, filling the board in the device's own
#: violet; the full board holds a beat, then dims in unison to genuine darkness.
#: It says "closed deliberately" without spending the four seconds a legible
#: word costs -- the daemon should not hold the MIDI port on the way out
#: (`--stop` gives it five before complaining).
#: Time for the head to walk all 16 encoders, corner to centre.
SHUTDOWN_SPIRAL_SECONDS = 1.6
#: How long one encoder takes to reach full once the head arrives. Overlaps the
#: travel, so the spiral reads as a moving edge rather than as pixels switching
#: on one at a time.
SHUTDOWN_RISE_SECONDS = 0.3
#: The completed board, whole and still, before it starts letting go. Long
#: enough to read as a state the board arrived at rather than a frame it passed
#: through on the way down.
SHUTDOWN_HOLD_SECONDS = 0.9
#: Uniform dim to black -- one hue throughout, no hue travel: the colour is not
#: doing anything on the way out, the lamp is simply going down. And all the way
#: *off*, not to the hardware's minimum brightness, which is still a lit encoder
#: wearing a colour.
SHUTDOWN_FADE_SECONDS = 1.4
#: Below this the encoder is switched off (:data:`DARK_VALUE`) rather than
#: dimmed further -- an encoder at the bottom of the fade is still an encoder
#: wearing a hue, and the board has to end with nothing on it at all.
SHUTDOWN_DARK_LEVEL = 0.02
#: The one hue the whole gesture wears, start to finish. The boot word is white
#: and has no hue to inherit, so the device's own violet is named here -- it is
#: also where the idle field's hue band ends up.
SHUTDOWN_COLOR = "violet"

#: Banner colour for transient words pushed onto the board later (RATE on a
#: rate-limited turn, a two-digit count, ...).
BANNER_COLOR = "red"
BANNER_SECONDS = 2.4

# --- Compaction -------------------------------------------------------------
# PreCompact/PostCompact bracket something completely opaque in the terminal
# that materially affects your agent, so it gets a real animation: arc drains to
# zero, a desaturated beat, then refills.

COMPACT_DRAIN_SECONDS = 0.6
COMPACT_REFILL_SECONDS = 0.8
#: If PostCompact never arrives, give up and hand the slot back.
COMPACT_TIMEOUT_SECONDS = 90.0

# --- /clear -----------------------------------------------------------------
# There is no `/clear` hook. What there is instead is a *pair*: `SessionEnd` with
# reason `clear`, then `SessionStart` with source `clear` and a brand new
# session id in the same tab. Both halves describe one moment, so both are
# handled by the same reset and only the first of them gets to fire the wipe.
#
# The encoder does not change hands across it: slots are keyed on the terminal,
# and the tab is exactly what a `/clear` does not touch.

CLEAR_ANIMATION = _flag("MFT_CLEAR_ANIMATION", True)
#: The whole wipe. Shorter than the spawn strike: an arrival is a thing you want
#: to catch across the room, a `/clear` is something you just typed.
CLEAR_SECONDS = 1.0
#: Fraction of it spent unwinding the ring; the remainder hands the encoder back
#: to whatever its steady state has become.
CLEAR_SETTLE = 0.6
#: The two halves of the pair are not ordered and either can go missing, so
#: whichever arrives first fires the wipe and the other is a no-op this long.
CLEAR_DEBOUNCE_SECONDS = 5.0

#: How long a slot just wiped by a `/clear` will answer to a session it cannot
#: identify. The replacement session id arrives immediately, but the hook that
#: says which *tab* it is in runs a process and is `async`, so a plain `curl`
#: event for the new id can be a long way ahead of it. Inside this window such
#: an event is taken for the other half of the clear rather than being given an
#: encoder of its own; see :meth:`mft.state.SessionTable._cleared_ghost`. Long
#: enough to cover a slow hook, short enough that a genuinely new session in the
#: same directory a minute later is still a new session.
CLEAR_ADOPT_SECONDS = 30.0

#: How long after a tab's session goes quiet a session running in a process with
#: no terminal of its own, in that same directory, is taken for its continuation
#: rather than given an encoder of its own; see
#: :meth:`mft.state.SessionTable._handed_off`. Claude Code hands a conversation
#: off to a pre-warmed spare under its background daemon, which has no tty and
#: inherits some other tab's environment, so the new session id arrives naming
#: nothing but a pid and the tab's own encoder would otherwise sit frozen on the
#: last state it heard for the full `SESSION_TTL_SECONDS`. Longer than
#: `CLEAR_ADOPT_SECONDS` because the handoff is not announced by anything and the
#: gap either side of it is a human's, not a hook's; short enough that a
#: background agent started in a repo you have not touched for a minute still
#: gets a knob of its own.
HANDOFF_ADOPT_SECONDS = 90.0

# --- Subagents --------------------------------------------------------------
# Subagents are not sessions and must never be mistaken for one. They own no
# encoder of their own, they answer only their parent's gesture, and they vanish
# when the parent's turn ends. So they get their own corner of the board and
# their own hue rather than borrowing the parent's: they stack up from the
# bottom-right of the parent's bank, filling backwards, and the pile grows
# toward the sessions rather than through them. Parallelism is physically
# visible and then collapses back.

SUBAGENT_STACK = _flag("MFT_SUBAGENT_STACK", True)
#: Whether a press on a violet dot does what a press on its parent would: raise
#: the parent's tab, hold to peek at the parent. There is nothing finer to aim
#: at -- a subagent runs inside its parent's terminal -- so the choice is only
#: between a live target and a dead knob. Off, the pile is inert paint again.
SUBAGENT_PRESS = _flag("MFT_SUBAGENT_PRESS", True)
#: Tools whose call *is* a subagent, so a PreToolUse for one counts as a spawn.
#: SubagentStart says the same thing more directly, but it is a recent hook and
#: a settings file installed before it exists reports no subagents whatsoever --
#: this is the signal that has been on the wire since the beginning. Same two
#: names the peek palette paints violet, for the same reason.
SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
#: A hue used for nothing else on the board, so a lit encoder that isn't any of
#: the state colours is unambiguously a subagent.
SUBAGENT_COLOR = "violet"
#: Held steady, and that is the whole point: channel 3 carries an animation *or*
#: a brightness level, so anything pulsing here is at the hardware's own levels
#: and spends most of each cycle near off -- where a dim RGB reads blue whatever
#: hue you sent it (see WHITE above). The pile used to breathe on ANIM_PULSE
#: ["1/8"] and what you actually saw was a row of faint blue pips, which is the
#: one thing a subagent must not look like. A constant mid-level violet costs the
#: liveness and buys the identification, and the stack already moves plenty: it
#: grows and collapses as the parent spawns and reaps.
SUBAGENT_BRIGHTNESS = 0.45
SUBAGENT_ANIM = ANIM_NONE
#: A short stub rather than a gauge or a full ring: the ring is meaningless here
#: (a subagent has no context reading of its own) and it should not look like it
#: is trying to mean something.
SUBAGENT_RING = 24

# --- Idle ambient -----------------------------------------------------------

#: With nothing running the board doesn't go dark, it breathes -- it stops being
#: a dashboard and becomes an object that lives on your desk.
AMBIENT = _flag("MFT_AMBIENT", True)
#: No hue: the ring breathes and the RGB stays dark, same as the boot word.
#: This layer sits under everything, so any colour here leaks through the gaps
#: in whatever is painted on top of it -- and it was leaking blue through the
#: waiting animation, which is what the boot animation's blue flash actually
#: was. An
#: idle board has nothing to say, and the hue channel is how this device says
#: things. Set it to a hue if you want the resting state to mean something.
AMBIENT_COLOR = None
AMBIENT_PERIOD_SECONDS = 12.0
AMBIENT_BRIGHTNESS = 0.14
