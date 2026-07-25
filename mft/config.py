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

#: A session that has not sent any hook event for this long is presumed dead
#: (crashed terminal never fires SessionEnd) and its encoder is reclaimed.
SESSION_TTL_SECONDS = 60 * 60

#: How long a finished session keeps its encoder lit before fading out.
DONE_FADE_SECONDS = 90.0

#: Sessions that ended cleanly hold their encoder this long. Slots are keyed on
#: the *terminal*, not the session id, so a `/clear` lands back on the same knob
#: even though it hands out a brand new session id; this window only covers a
#: full quit-and-restart in the same tab.
SLOT_LINGER_SECONDS = 120.0

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

#: The attention states are deliberately *different* from each other, not four
#: flavours of blinking red -- one blinking red for everything trains you to
#: ignore blinking red. Priority order also decides who gets the fast animation
#: when several want it at once (see ``mft.board.arbitrate_motion``).
ATTENTION_STATES = ("permission", "plan", "error", "waiting", "done")

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
# A working agent's ring is a fuel gauge: it fills as the context window fills,
# so "this one is about to compact" is visible from across the room. No hook
# payload carries token counts, so :mod:`mft.context` reads them out of the
# transcript the payload points at.

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
    ("[1m]", 1_000_000),
    ("-1m", 1_000_000),
    ("haiku", 200_000),
    ("sonnet", 200_000),
    ("opus", 200_000),
)

#: Below this the ring would be a stub too short to read as a gauge, so an
#: agent with a nearly empty context still shows a legible pip.
CONTEXT_RING_FLOOR = 4

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

#: Each detent of a right-turn adds this much snooze; left-turn removes it.
SNOOZE_STEP_SECONDS = 300.0
SNOOZE_MAX_SECONDS = 3600.0

# --- Input ------------------------------------------------------------------

#: A press held longer than this peeks at the session instead of focusing it.
HOLD_SECONDS = 0.6

#: How the encoders are configured in the Midi Fighter Utility. "auto" reads
#: the classic relative encodings (63/65 and 1/127) as deltas and treats
#: anything else as an absolute position, which covers both stock modes without
#: making you pick. Force with "relative" or "absolute" if yours is unusual.
ENCODER_MODE = os.environ.get("MFT_ENCODER_MODE", "auto")

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
# None of it lights the RGB: not the word, not the lamp test after it, not the
# ambient layer underneath both. The boot sequence is white light moving on a
# dark board from start to finish. Colour is how this device means things, and
# the one stretch where it has nothing to say is the one stretch where the hue
# channel has no business being on.

BOOT_WORD = "CLAUDE"
#: Deliberately unhurried. The boot animation is the only time the device says
#: anything in words, and a 0.3s-per-letter version reads as a flicker you catch
#: the tail of rather than as CLAUDE: by the time you look up it is over. Each
#: letter strikes in at full brightness and then decays to black over a full
#: second before the next one strikes, which is both what makes 16 pixels
#: resolve into a glyph and what separates one letter from the next.
BOOT_FADE_SECONDS = 1.0
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
#: Lamp test. It opens the aircraft way -- one arc sweep that lights every ring
#: on all 16 encoders, once, on purpose -- and then dissolves into a generative
#: interference field that keeps running while the board has nothing to say.
#: An idle Twister is a Twister you stop looking at, so the empty state is the
#: one state worth making worth watching.
LAMP_TEST_SWEEP_SECONDS = 1.4
#: How long the field runs, fading the whole way, if no session ever shows up.
#: Long enough to be a lava lamp you glance at, short enough that a machine left
#: on overnight ends up dark rather than glowing at you from the desk.
LAMP_TEST_SECONDS = 60.0
#: A session appeared: the field gets out of the way, but over a couple of
#: frames rather than by hard-cutting, so the first encoder to light reads as
#: emerging from the field rather than as the field glitching.
LAMP_TEST_DISMISS_SECONDS = 0.8
#: The field used to wander a hue band. It doesn't any more: boot lights no RGB
#: at all, start to finish, so the sweep and the field are rings only -- white
#: light moving on a dark board, matching the word that precedes them. The band
#: was 78..122, which on a wheel that wraps around 80 was not the red-to-violet
#: it claimed to be anyway.
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
#: The hue sweeps the whole wheel once, which nothing else on the board does --
#: a static colour would just look like some other state having a moment.
SPAWN_HUES = (0, 127)
#: Fraction of the animation the ring spends filling from empty to full. One
#: unhurried sweep, not a spin: a ring that laps itself reads as an activity
#: indicator, and this has to read as a single event that happened once.
#: The remainder is the handover to the session's own steady state.
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

# --- Subagents --------------------------------------------------------------
# Subagents are not sessions and must never be mistaken for one. They own no
# encoder, they answer no gesture, and they vanish when the parent's turn ends.
# So they get their own corner of the board and their own hue rather than
# borrowing the parent's: they stack up from the bottom-right of the parent's
# bank, filling backwards, and the pile grows toward the sessions rather than
# through them. Parallelism is physically visible and then collapses back.

SUBAGENT_STACK = _flag("MFT_SUBAGENT_STACK", True)
#: Tools whose call *is* a subagent, so a PreToolUse for one counts as a spawn.
#: SubagentStart says the same thing more directly, but it is a recent hook and
#: a settings file installed before it exists reports no subagents whatsoever --
#: this is the signal that has been on the wire since the beginning. Same two
#: names the peek palette paints violet, for the same reason.
SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
#: A hue used for nothing else on the board, so a lit encoder that isn't any of
#: the state colours is unambiguously a subagent.
SUBAGENT_COLOR = "violet"
SUBAGENT_BRIGHTNESS = 0.3
#: Slower than SLOW_ANIM, which is the slowest any *session* is allowed to move,
#: so subagents read as alive without ever competing with a session for your eye.
SUBAGENT_ANIM = ANIM_PULSE["1/8"]
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
#: lamp test, which is what the boot animation's blue flash actually was. An
#: idle board has nothing to say, and the hue channel is how this device says
#: things. Set it to a hue if you want the resting state to mean something.
AMBIENT_COLOR = None
AMBIENT_PERIOD_SECONDS = 12.0
AMBIENT_BRIGHTNESS = 0.14
