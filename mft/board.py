"""The whole device as one frame buffer, plus the transient animations that
compose over it.

:mod:`mft.render` decides what one session looks like. Everything that is only
decidable across the whole board lives here:

* **motion arbitration** -- at most one encoder moves fast at a time, and it is
  always the one where a human is blocking progress. Motion is the only thing
  peripheral vision reliably catches, so it is a budget, not a decoration.
* **subagent stack** -- subagents pile up from the bottom-right of the parent's
  bank into *unclaimed* encoders, in a hue of their own, making parallelism
  physically visible without disturbing anyone's slot.
* **overlays** -- boot, compaction, a spelled-out banner, the press-and-hold
  detail view. They paint over the steady-state board and expire on their own,
  so nothing has to be torn back down.
"""

from __future__ import annotations

import math
import time
from functools import lru_cache
from itertools import islice
from typing import Iterable, Optional, Sequence

from . import config, font
from .render import Cell, render
from .state import Session, priority

#: A dark encoder. Shared rather than rebuilt: :class:`~mft.render.Cell` is
#: frozen, so one instance can back every unlit slot on every frame.
BLANK = Cell()


def blank_board(slot_count: int = config.SLOT_COUNT) -> list[Cell]:
    return [BLANK] * slot_count


def bank_of(slot: int) -> int:
    return slot // config.ENCODERS_PER_BANK


def bank_slots(bank: int) -> range:
    start = bank * config.ENCODERS_PER_BANK
    return range(start, start + config.ENCODERS_PER_BANK)


@lru_cache(maxsize=None)
def spawn_order(bank: int) -> tuple[int, ...]:
    """Where subagents land, in the order they take it.

    Sessions are handed encoders from the top-left forwards, so subagents fill
    from the **bottom-right backwards**: the two allocators grow toward each
    other and the far corner is always the newest thing on the board. Reading
    the pile from the corner inwards is reading it newest-first.
    """
    return tuple(reversed(bank_slots(bank)))


# --- overlays ---------------------------------------------------------------


def _clamp01(t: float) -> float:
    return max(0.0, min(1.0, t))


def _smoothstep(t: float) -> float:
    t = _clamp01(t)
    return t * t * (3 - 2 * t)


def _handover(under: Cell, ring: float, settle: float, color: str | int | None) -> Cell:
    """The tail an encoder-sized gesture ends on: not a cut, a crossfade.

    Brightness and ring travel from wherever the gesture had them to whatever
    the steady state underneath is showing, so the encoder is seen *becoming*
    its own state rather than the gesture switching off and a colour appearing
    separately. The hue crosses at the halfway point rather than being
    interpolated -- there is no meaningful blend between two points on a hue
    wheel -- so the last thing you see is the session's own colour arriving.

    Shared by the spawn strike and the ``/clear`` wipe, which are the same
    gesture in different vocabularies and were the same eight lines twice.
    """
    return Cell(
        under.color if settle > 0.5 and under.color is not None else color,
        config.ANIM_NONE,
        int(ring + (under.ring - ring) * settle),
        1.0 + (under.brightness - 1.0) * settle,
    )


class Overlay:
    """Something transient painted over the steady-state board.

    Overlays are pure paint: they never touch session state, so an overlay that
    is dropped mid-flight leaves nothing to clean up.
    """

    def done(self, now: float) -> bool:
        raise NotImplementedError

    def apply(
        self, board: list[Cell], now: float, claimed: frozenset[int] = frozenset()
    ) -> None:
        """Paint over the composed board. ``claimed`` is the set of slots a
        session or subagent already owns this frame, for the overlays that have
        an opinion about painting on top of live state."""
        raise NotImplementedError


class TextOverlay(Overlay):
    """Spell a word across a bank, one 4x4 glyph at a time.

    Used for the boot animation (CLAUDE), for shouting a short reason at you
    (RATE when a turn dies on a rate limit) and for showing a count.

    Each letter **strikes in at full brightness and then decays** before the
    next one strikes, so what separates two letters is a fade rather than a
    crossfade: a fade *out* leaves the glyph legible for the whole time it is
    visible, where a crossfade spends its middle showing a blend of two letters
    that is neither.

    The exception is a pixel the next letter also lights. That one holds instead
    of decaying -- it is a lamp staying on across the boundary, not one going
    out and another coming up in the same place -- so the board never blinks
    fully black between letters that overlap.

    ``color`` of ``None`` is the boot default and means the white LED ring
    alone, with the RGB switch underneath switched off -- the one thing on this
    device that is a colour rather than a hue.
    """

    def __init__(
        self,
        text: str,
        started_at: float,
        color: str | int | None = config.BOOT_COLOR,
        bank: int = 0,
        fade: float = config.BOOT_FADE_SECONDS,
        hold: float = config.BOOT_HOLD_SECONDS,
        reverse: bool = False,
    ) -> None:
        self.text = (text[::-1] if reverse else text) or " "
        self.color = color
        self.bank = bank
        #: How long a letter takes to decay from full to dark.
        self.fade = max(0.01, fade)
        #: How long it sits at full before the decay starts.
        self.hold = max(0.0, hold)
        self.started_at = started_at
        #: One letter's worth of time. Floored: a zero-fade, zero-hold overlay
        #: would otherwise be a word of zero length divided by itself.
        self.step = max(0.01, self.fade + self.hold)

    @property
    def duration(self) -> float:
        return self.step * len(self.text)

    def done(self, now: float) -> bool:
        return now - self.started_at >= self.duration

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        t = now - self.started_at
        if t < 0 or t >= self.duration:
            return

        index = min(len(self.text) - 1, int(t / self.step))
        within = t - index * self.step
        # Full for `hold`, then an eased decay over `fade`. Eased rather than
        # linear because a linear ramp on these LEDs reads as dropping off a
        # cliff at the end; smoothstep keeps the tail of the fade visible, which
        # is the part that separates one letter from the next.
        level = 1.0 - _smoothstep(max(0.0, within - self.hold) / self.fade)
        glyph = font.pixels(self.text[index])
        # A pixel the next letter also lights never decays: it is the same lamp
        # staying on across the boundary, not one going out and another coming
        # up in the same place. Only the pixels that actually change fade.
        nxt = (
            font.pixels(self.text[index + 1])
            if index + 1 < len(self.text)
            else None
        )
        for offset, slot in enumerate(bank_slots(self.bank)):
            lit = glyph[offset] * level
            if nxt is not None:
                lit = max(lit, glyph[offset] * nxt[offset])
            board[slot] = Cell(
                self.color if lit > 0.02 else None,
                config.ANIM_NONE,
                127 if lit > 0.02 else 0,
                lit,
            )


class WaitingOverlay(Overlay):
    """Slow white gradients drifting over an empty board, until Claude shows up.

    This is the *waiting* state and it is supposed to look like one. Two broad
    raised-cosine gradients travel the grid -- one down the diagonal, one across
    the columns -- at periods with no common factor, so the pair never lines up
    twice and there is no loop for your eye to finish. A slow breath rides on
    top of the sum. Nothing here is sharp and nothing here is bright: the whole
    thing is capped at :data:`config.WAITING_BRIGHTNESS`, well under a live
    session, because the board is saying nothing and a bright board is a board
    saying something.

    What it replaced was a lamp test -- one arc lighting every ring on all 16
    encoders at full, then a generative interference field at that same level.
    Correct as a self-test and wrong as a resting state: sixteen knobs at full
    is what this device looks like when everything at once needs you.

    It fades out over :data:`config.WAITING_SECONDS` on its own, and
    :meth:`dismiss` pulls it off the board the moment a real session appears. It
    never paints a claimed encoder, and everywhere else it only writes a cell it
    would light *more* than whatever is underneath, so nothing the board actually
    has to say is ever dimmed by decoration.
    """

    def __init__(
        self,
        started_at: float,
        bank: int = 0,
        duration: float = config.WAITING_SECONDS,
    ) -> None:
        self.started_at = started_at
        self.bank = bank
        self.duration = max(0.01, duration)
        self.dismissed_at: Optional[float] = None
        #: Gain at the instant of dismissal, so the quick fade-out starts from
        #: wherever the slow one had got to rather than jumping back to full.
        self._dismiss_gain = 1.0

    def dismiss(self, now: float) -> None:
        """A session exists: retreat, quickly but not instantly."""
        if self.dismissed_at is None:
            self._dismiss_gain = self._gain(now)
            self.dismissed_at = now

    def done(self, now: float) -> bool:
        if self.dismissed_at is not None:
            return now - self.dismissed_at >= config.WAITING_DISMISS_SECONDS
        return now - self.started_at >= self.duration

    def _gain(self, now: float) -> float:
        """The overall envelope: eased up over a couple of seconds, then a
        cosine slide over the full run, so it holds near full for a while and
        then leaves, rather than visibly dimming from the first second."""
        if self.dismissed_at is not None:
            gone = (now - self.dismissed_at) / config.WAITING_DISMISS_SECONDS
            # Clamped at both ends. Only the lower one ever fires in the daemon,
            # where the dismissal and the frames after it come off the same
            # monotonic clock -- but a gain above 1.0 is an overbright cell
            # (ring > 127) rather than a slightly wrong one, and nothing
            # downstream re-clamps it.
            return self._dismiss_gain * _clamp01(1.0 - gone)
        t = now - self.started_at
        u = _clamp01(t / self.duration)
        rise = _smoothstep(t / config.WAITING_FADE_IN_SECONDS)
        return rise * (1 + math.cos(math.pi * u)) / 2

    @staticmethod
    def _gradient(u: float, t: float, period: float) -> float:
        """One broad gradient travelling an axis, in 0..1.

        ``u`` is a position along that axis in 0..1 and the band wraps, so a
        gradient leaving the bottom-right is already entering the top-left --
        the grid is four encoders wide and a sweep that ran off the end would
        spend most of its period on a dark board. Raised cosine rather than a
        ramp with an edge: an edge is a thing arriving, and nothing is arriving.
        """
        head = (t / period) % 1.0
        # Wrapped distance from the crest, 0..0.5.
        gap = abs(((u - head + 0.5) % 1.0) - 0.5)
        if gap >= config.WAITING_WIDTH:
            return 0.0
        return (1 + math.cos(math.pi * gap / config.WAITING_WIDTH)) / 2

    def _level(self, x: float, y: float, t: float) -> float:
        """The two gradients plus the breath, in 0..1, before the fade envelope.

        The second period is the first times an irrational-enough ratio: two
        gradients that lined up would beat, and a beat is a pattern you can
        learn to expect. Weighted rather than averaged so neither one is quite
        the whole picture, then breathed over ~40 seconds so even a still frame
        of the pair is somewhere on a slower ramp.
        """
        slow = config.WAITING_PERIOD_SECONDS
        diagonal = self._gradient((x + y) / 6.0, t, slow)
        across = self._gradient(x / 3.0, -t, slow * 1.61)
        breath = 0.72 + 0.28 * (1 + math.sin(2 * math.pi * t / 41.0)) / 2
        return _clamp01(0.62 * diagonal + 0.48 * across) * breath

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        t = now - self.started_at
        if t < 0:
            return
        gain = self._gain(now) * config.WAITING_BRIGHTNESS

        for offset, slot in enumerate(bank_slots(self.bank)):
            if slot in claimed:
                continue  # a session's own encoder is never decoration
            x = float(offset % 4)
            y = float(offset // 4)
            level = self._level(x, y, t) * gain
            # Ambient breathing underneath is left alone wherever it is already
            # the brighter of the two, which is what turns the tail of the fade
            # into a crossfade back to the idle board rather than a blackout.
            if level <= board[slot].brightness:
                continue
            # Colourless, like the word it follows: the ring carries the whole
            # thing and the RGB stays dark. Boot is the one stretch where the
            # board is saying nothing, and the hue channel is how it says
            # things -- so it has no business being lit here.
            board[slot] = Cell(None, config.ANIM_NONE, int(127 * level), level)


@lru_cache(maxsize=None)
def spiral_path(bank: int = 0) -> tuple[int, ...]:
    """The 16 slots of a bank as one inward spiral from the top-left corner.

    A path rather than a scan, because the whole point of the shutdown wipe is
    that it is *continuous*: a raster order teleports back to the left edge
    three times, which reads as four unrelated sweeps rather than as one thing
    travelling. A spiral also ends where it should -- the centre of the grid --
    so the board fills from the outside in and closes rather than running off
    an edge.
    """
    rows, cols = config.GRID_ROWS, config.GRID_COLS
    top, bottom, left, right = 0, rows - 1, 0, cols - 1
    base = bank * config.ENCODERS_PER_BANK
    path: list[int] = []
    while top <= bottom and left <= right:
        for col in range(left, right + 1):
            path.append(base + top * cols + col)
        top += 1
        for row in range(top, bottom + 1):
            path.append(base + row * cols + right)
        right -= 1
        if top <= bottom:
            for col in range(right, left - 1, -1):
                path.append(base + bottom * cols + col)
            bottom -= 1
        if left <= right:
            for row in range(bottom, top - 1, -1):
                path.append(base + row * cols + left)
            left += 1
    return tuple(path)


class ShutdownOverlay(Overlay):
    """The daemon is leaving: one head, top-left corner, spiralling to the centre.

    Three movements, in order:

    * the **spiral** walks :func:`spiral_path` from the corner inwards, every
      encoder it passes fading up in the device's own violet;
    * the **hold** leaves the completed board whole and still for a beat, so it
      reads as somewhere the gesture arrived rather than a frame on the way
      down;
    * the **fade** dims the whole board out uniformly -- one hue throughout, no
      hue travel: the colour is not doing anything on the way out, the lamp is
      simply going down. All sixteen go together, which is the load-bearing
      part, since every other animation here is per-encoder. And it goes all the
      way *off* rather than down to the hardware's minimum brightness, which is
      still a lit encoder wearing a colour.

    Unwinding the spiral backwards would be a fourth gesture arguing with the
    first; letting go everywhere at once is what a clean exit is. Seeing this at
    all means the daemon exited on purpose rather than died.
    """

    def __init__(
        self,
        started_at: float,
        bank: int = 0,
        spiral: float = config.SHUTDOWN_SPIRAL_SECONDS,
        rise: float = config.SHUTDOWN_RISE_SECONDS,
        hold: float = config.SHUTDOWN_HOLD_SECONDS,
        fade: float = config.SHUTDOWN_FADE_SECONDS,
    ) -> None:
        self.started_at = started_at
        self.bank = bank
        self.spiral = max(0.01, spiral)
        #: Clamped below the travel time: a rise longer than the journey would
        #: mean the centre never reaches full before the hold starts.
        self.rise = max(0.01, min(rise, self.spiral * 0.5))
        self.hold = max(0.0, hold)
        self.fade = max(0.01, fade)
        self.hue = int(config.COLORS.get(config.SHUTDOWN_COLOR, config.SHUTDOWN_COLOR))
        #: (slot, arrival time) along the spiral, everything full by the end of
        #: the travel: the last encoder starts rising a rise-length before it.
        #: Fixed the moment the overlay exists, so it is built once rather than
        #: on each of the ~100 frames the gesture lasts.
        path = spiral_path(self.bank)
        travel = max(0.01, self.spiral - self.rise)
        self._arrivals = tuple(
            (slot, step / max(1, len(path) - 1) * travel)
            for step, slot in enumerate(path)
        )

    @property
    def duration(self) -> float:
        return self.spiral + self.hold + self.fade

    def done(self, now: float) -> bool:
        return now - self.started_at >= self.duration

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        t = now - self.started_at
        if t < 0 or t >= self.duration:
            return
        # One envelope over the whole board, applied after the per-encoder rise:
        # this is the "uniformly out" part, and it has to be indifferent to
        # where in the spiral a given encoder was lit.
        gone = (t - self.spiral - self.hold) / self.fade
        gain = 1.0 - _smoothstep(gone) if gone > 0 else 1.0
        hue = self.hue

        for slot, arrival in self._arrivals:
            if not 0 <= slot < len(board):
                continue
            level = _smoothstep((t - arrival) / self.rise) * gain
            # Colour-free below the floor, not merely dim: an encoder still
            # holding a hue at brightness zero is an encoder still lit.
            if level <= config.SHUTDOWN_DARK_LEVEL:
                board[slot] = BLANK
                continue
            board[slot] = Cell(hue, config.ANIM_NONE, int(127 * level), level)


class SpawnOverlay(Overlay):
    """A session just claimed this encoder: strike it, then settle.

    The arrival of a Claude is otherwise the quietest thing that happens on the
    board -- a new session renders as `idle`, which is a dim green pip
    indistinguishable at a glance from the dim green pip beside it. So the
    claiming gets a gesture of its own: the ring blinks full-on/full-off three
    times over bright red RGB. Three hard flashes is a countable shape -- you
    can read it out of the corner of your eye without having watched it start --
    and nothing else on this board blinks its ring, so the red is not confusable
    with the permission and error states that own that hue.

    It ends by crossfading into whatever is underneath rather than cutting, so
    the encoder is seen *becoming* its steady state instead of flashing and then
    separately turning green.
    """

    def __init__(
        self,
        session: Session,
        started_at: float,
        duration: float = config.SPAWN_SECONDS,
    ) -> None:
        #: The session, not its slot: a session arriving is exactly the event
        #: that compacts the board, so the slot can move out from under us.
        self.session = session
        self.started_at = started_at
        self.duration = max(0.01, duration)

    def done(self, now: float) -> bool:
        return now - self.started_at >= self.duration

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        u = (now - self.started_at) / self.duration
        if not 0.0 <= u < 1.0:
            return
        slot = self.session.slot
        if not 0 <= slot < len(board):
            return
        under = board[slot]

        # Square wave, deliberately: an eased blink reads as breathing, and
        # breathing is a state. The last flash ends dark so the ring is empty at
        # the moment the handover starts and can be seen rising onto the
        # session's own position rather than falling onto it.
        phase = min(1.0, u / config.SPAWN_SETTLE) * config.SPAWN_FLASHES
        fill = 1.0 if (phase % 1.0) < 0.5 and phase < config.SPAWN_FLASHES else 0.0
        settle = _smoothstep(max(0.0, u - config.SPAWN_SETTLE) / (1 - config.SPAWN_SETTLE))
        board[slot] = _handover(under, 127 * fill, settle, config.SPAWN_COLOR)


class ClearOverlay(Overlay):
    """`/clear`: the agent on this encoder just forgot everything.

    The ring strikes white and unwinds to nothing, then hands the encoder back
    to a steady state that is by now an idle pip with an empty gauge. White on a
    dark RGB is the boot vocabulary -- it is what this device does when it is
    saying something that is not a status, and forgetting is exactly that.

    Deliberately the same family as :class:`CompactOverlay` and deliberately not
    the same gesture: compaction drains the arc and *refills* it, because the
    agent keeps what mattered. This one drains and stops.
    """

    def __init__(
        self,
        session: Session,
        started_at: float,
        duration: float = config.CLEAR_SECONDS,
    ) -> None:
        #: The session, not its slot: the id on this slot changes in the middle
        #: of the very event being animated.
        self.session = session
        self.started_at = started_at
        self.duration = max(0.01, duration)

    def done(self, now: float) -> bool:
        return now - self.started_at >= self.duration

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        u = (now - self.started_at) / self.duration
        if not 0.0 <= u < 1.0:
            return
        slot = self.session.slot
        if not 0 <= slot < len(board):
            return
        under = board[slot]

        fill = 1.0 - _smoothstep(min(1.0, u / config.CLEAR_SETTLE))
        # Same handover as the spawn strike, into white rather than red: the
        # wipe is over and the encoder arrives at whatever it now is.
        settle = _smoothstep(max(0.0, u - config.CLEAR_SETTLE) / (1 - config.CLEAR_SETTLE))
        board[slot] = _handover(under, 127 * fill, settle, None)


class CompactOverlay(Overlay):
    """Compaction, made visible: the arc drains to zero, sits desaturated for a
    beat, then refills.

    PreCompact/PostCompact bracket something that is completely opaque in the
    terminal and materially changes what your agent knows. Watching it happen a
    few dozen times is the only way to acquire any intuition about it.
    """

    def __init__(self, session: Session, started_at: float) -> None:
        #: The session, not its slot: compaction outlives the events that
        #: reshuffle the board, and the drain has to follow its encoder.
        self.session = session
        self.started_at = started_at
        self.finished_at: Optional[float] = None

    def finish(self, now: float) -> None:
        """PostCompact arrived: start the refill."""
        if self.finished_at is None:
            self.finished_at = now

    def done(self, now: float) -> bool:
        if self.finished_at is not None:
            return now - self.finished_at > config.COMPACT_REFILL_SECONDS
        # PostCompact never came (blocked, crashed): hand the encoder back.
        return now - self.started_at > config.COMPACT_TIMEOUT_SECONDS

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        if not 0 <= self.session.slot < len(board):
            return
        # Clamped at both ends: a frame from before the drain started, or from
        # before PostCompact landed, is a fraction outside 0..1 rather than an
        # error, and it should read as the end of the ramp it is past.
        if self.finished_at is None:
            drain = _clamp01((now - self.started_at) / config.COMPACT_DRAIN_SECONDS)
            fill = 1.0 - drain
            color = config.STATE_COLORS["working"] if fill > 0.05 else "purple"
        else:
            fill = _clamp01((now - self.finished_at) / config.COMPACT_REFILL_SECONDS)
            color = "purple" if fill < 0.05 else config.STATE_COLORS["working"]
        board[self.session.slot] = Cell(
            color, config.ANIM_NONE, int(127 * fill), 0.35 + 0.5 * fill
        )


class PeekOverlay(Overlay):
    """Hold an encoder and its bank becomes a detail view of that one session:
    its last 15 tool calls, oldest to newest, hue by tool kind.

    A modal zoom out of a grid with no screen. "This agent has done nothing but
    grep for four minutes" is legible from across the room, and there is no
    other way to learn it without opening the tab.
    """

    def __init__(
        self,
        session: Session,
        started_at: float,
        delay: float = config.HOLD_SECONDS,
    ) -> None:
        self.session = session
        #: The overlay goes up on press-down but only paints once the press has
        #: lasted long enough to be a hold, so a quick tap to focus a tab does
        #: not flash the detail view on its way past.
        self.started_at = started_at + delay
        self._released = False

    def release(self) -> None:
        self._released = True

    def done(self, now: float) -> bool:
        return self._released

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        if now < self.started_at:
            return
        held = self.session.slot
        if not 0 <= held < len(board):
            return
        history = list(self.session.tool_history)
        others = [s for s in bank_slots(bank_of(held)) if s != held and s < len(board)]
        # Newest lands on the slot nearest the held knob, so the history reads
        # outward from the thing you are holding.
        recent = history[-len(others) :]
        padding = [None] * (len(others) - len(recent))
        for slot, tool in zip(others, padding + recent):
            if tool is None:
                board[slot] = BLANK
                continue
            color = config.TOOL_COLORS.get(tool, config.TOOL_COLOR_DEFAULT)
            board[slot] = Cell(color, config.ANIM_NONE, 127, 0.7)
        board[held] = Cell("purple", config.ANIM_NONE, 127, 1.0)


# --- banks ------------------------------------------------------------------


def bank_to_show(sessions: Sequence[Session], current: int) -> Optional[int]:
    """The bank the board should be showing, or ``None`` to leave it alone.

    Everything the escalating strobe of a permission gate buys is lost if the
    gate is on a bank you are not looking at: sixteen encoders are visible and
    the other forty-eight are a filing cabinet. So the loudest unattended block
    gets to pull the view onto itself.

    Deliberately only the *blocking* states, and only unattended ones. Following
    a working session would move the board constantly and for nothing -- work
    happening elsewhere is not a thing you have to see -- and following a block
    you have already acknowledged would undo the acknowledgement.

    Pure, and returning a bank rather than sending one, so the policy is testable
    with no hardware and the wire stays in :mod:`mft.daemon`. The daemon owns the
    cooldown too: this function has no memory and will happily name the same bank
    every frame.
    """
    if not config.FOLLOW_ALERTS or not config.BANK_SELECT_CC:
        return None
    blocking = [
        s
        for s in sessions
        if s.alert and s.ended_at is None and s.state in config.FOLLOW_STATES
    ]
    if not blocking:
        return None

    def rank(session: Session) -> tuple[int, float]:
        owed = session.attention_since
        return priority(session.state), session.state_since if owed is None else owed

    want = bank_of(min(blocking, key=rank).slot)
    return None if want == current else want


# --- composition ------------------------------------------------------------


def arbitrate_motion(board: list[Cell], sessions: Sequence[Session]) -> None:
    """Leave the fast animation on exactly one encoder.

    Several sessions blocking at once would otherwise fill the board with
    competing strobes, and a board where everything moves is a board where
    nothing stands out. The winner is the highest-priority attention state,
    oldest first; everyone else drops to a slow pulse.
    """
    animated = [
        s
        for s in sessions
        if 0 <= s.slot < len(board) and board[s.slot].rgb_anim
    ]
    if len(animated) <= 1:
        return
    def rank(session: Session) -> tuple[int, float]:
        owed = session.attention_since
        return priority(session.state), session.state_since if owed is None else owed

    animated.sort(key=rank)
    for session in animated[1:]:
        cell = board[session.slot]
        board[session.slot] = Cell(
            cell.color, config.SLOW_ANIM, cell.ring, cell.brightness
        )


def subagent_owners(
    sessions: Sequence[Session],
    claimed: Iterable[int] = (),
    slot_count: int = config.SLOT_COUNT,
) -> dict[int, Session]:
    """Which parent each piled subagent slot belongs to.

    Placement only -- no `Cell` is built here -- because the answer is wanted in
    two places that must not disagree: the painter below, and the daemon
    resolving a press. Anything already claimed, a real session or another
    parent's subagents, is skipped rather than trampled, so the pile shrinks
    into whatever room is left instead of stealing someone's slot.

    The mapping is the *parent*, and it can never be anything finer. A subagent
    has no identity beyond an opaque key in ``subagents_in_flight`` -- no cwd,
    no tty, no pid -- and it wouldn't help if it did: a subagent runs inside its
    parent's terminal, so the only window a press could ever raise is the one
    the parent is already sitting in.
    """
    taken = set(claimed)
    owners: dict[int, Session] = {}
    # Deterministic across frames: parents are served in encoder order, so a
    # subagent doesn't hop to a different knob because a dict reordered.
    for session in sorted(sessions, key=lambda s: s.slot):
        count = session.subagents
        if not count:
            continue
        free = (
            s
            for s in spawn_order(bank_of(session.slot))
            if s not in taken and s < slot_count
        )
        for slot in islice(free, count):
            taken.add(slot)
            owners[slot] = session
    return owners


def stack_subagents(
    board: list[Cell], sessions: Sequence[Session], claimed: set[int]
) -> None:
    """Pile in-flight subagents into the bottom-right of their parent's bank.

    They get a hue used for nothing else, a stub ring and no animation at all,
    because the one thing you must never do is mistake a subagent for a session:
    it owns no encoder of its own, it answers only the one gesture its parent
    would have answered, and it disappears when the parent's turn ends.
    """
    for slot in subagent_owners(sessions, claimed, len(board)):
        claimed.add(slot)
        board[slot] = Cell(
            config.SUBAGENT_COLOR,
            config.SUBAGENT_ANIM,
            config.SUBAGENT_RING,
            config.SUBAGENT_BRIGHTNESS,
        )


class Sleep:
    """How lit the board should be, given how long since anyone was here.

    Every other fade on this board is a session getting older. This one is the
    room being empty: it runs off hook events and hands on knobs, and an agent
    that works through the night keeps sending events, so the only thing that
    ever reaches these timings is an unattended desk.

    Two stages, because they answer two different questions. Dim answers "is
    anything waiting for me?" from the doorway and keeps every colour and ring
    position intact to answer it with. Off answers "is this thing still on?" at
    three in the morning, and the only right answer then is no light at all.

    Not an :class:`Overlay`. Overlays are transient -- they expire, and the board
    underneath is what they were covering. This is a standing condition of the
    whole board that reverses, and it belongs *under* the overlays rather than
    beside them; see :func:`compose`.
    """

    def __init__(self, started_at: float = 0.0) -> None:
        self.last_activity = started_at
        #: When the board was last woken while it was already fading, and the
        #: gain it was at when that happened -- so the ramp back up starts from
        #: where the fade had got to rather than jumping to the floor and rising
        #: from there. The same bookkeeping as
        #: :attr:`WaitingOverlay._dismiss_gain`, in the other direction.
        self._woke_at: Optional[float] = None
        self._woke_from = 1.0

    def touch(self, now: float) -> None:
        """Something happened: a hook event, or a hand on a knob."""
        level = self.gain(now)
        if level < 1.0:
            # Anchored on wherever we actually are, which covers being touched
            # again part-way up as well as part-way down -- both are a ramp from
            # here to full, and neither may show a step.
            self._woke_at = now
            self._woke_from = level
        else:
            self._woke_at = None
        self.last_activity = now

    def gain(self, now: float) -> float:
        """1.0 awake, falling to ``SLEEP_DIM_LEVEL`` and then to 0.0.

        Pure: the daemon's `/status` handler reads this off the HTTP thread while
        the render loop is reading it too, so it settles nothing and stores
        nothing. A finished wake ramp is left in place rather than cleared,
        because falling through it lands on 1.0 anyway.
        """
        if not config.SLEEP:
            return 1.0
        if self._woke_at is not None:
            rising = (now - self._woke_at) / config.SLEEP_WAKE_SECONDS
            if rising < 1.0:
                return self._woke_from + (1.0 - self._woke_from) * _smoothstep(rising)

        away = now - self.last_activity
        dark = (away - config.SLEEP_DARK_SECONDS) / config.SLEEP_FADE_SECONDS
        if dark > 0:
            return config.SLEEP_DIM_LEVEL * (1.0 - _smoothstep(dark))
        dim = (away - config.SLEEP_DIM_SECONDS) / config.SLEEP_FADE_SECONDS
        if dim > 0:
            return 1.0 - (1.0 - config.SLEEP_DIM_LEVEL) * _smoothstep(dim)
        return 1.0


def dim(board: list[Cell], gain: float, spared: frozenset[int] = frozenset()) -> None:
    """Scale the whole board toward dark, leaving the encoders in `spared` lit.

    `spared` is how a sleeping board still knows how to shout: an encoder with a
    permission prompt open is asking for a human, and going dark because the
    human left is precisely the wrong answer. Everything else goes down together.

    The animation is dropped on the way down, which is not cosmetic: channels 3
    and 6 carry *either* an animation or a brightness and never both
    (:meth:`mft.twister.Twister.write`), so an encoder that keeps its animation
    cannot be dimmed at all. A dim that silently doesn't dim would be worse than
    one that costs a strobe -- and in practice every animated state is either
    spared or means someone is working, so nothing reaches here still moving.
    """
    for slot, cell in enumerate(board):
        if slot in spared or cell is BLANK:
            continue
        level = cell.brightness * gain
        # Colour-free below the floor rather than merely dim, as in
        # ShutdownOverlay: brightness zero on a hue is still a lit encoder.
        if level <= config.SLEEP_DARK_LEVEL:
            board[slot] = BLANK
            continue
        board[slot] = Cell(cell.color, config.ANIM_NONE, int(cell.ring * gain), level)


def ambient(board: list[Cell], now: float) -> None:
    """Nothing is running, so the board breathes rather than going dark.

    It lies *underneath* everything, including the boot sequence -- the waiting
    gradients only write a cell they would light more than what is already
    there, so whatever colour this is shows through the gaps in them. Which is
    why it is no colour at all: a blue breathing through the waiting animation
    was the blue on the board at boot, and an idle board has by definition
    nothing to say.
    """
    for slot in bank_slots(0):
        index = slot % config.ENCODERS_PER_BANK
        phase = (
            now * 2 * math.pi / config.AMBIENT_PERIOD_SECONDS
            - index * math.pi / config.ENCODERS_PER_BANK
        )
        level = (math.sin(phase) + 1) / 2
        board[slot] = Cell(
            config.AMBIENT_COLOR,
            config.ANIM_NONE,
            int(127 * level),
            config.AMBIENT_BRIGHTNESS * level,
        )


def compose(
    sessions: Iterable[Session],
    now: Optional[float] = None,
    overlays: Iterable[Overlay] = (),
    slot_count: int = config.SLOT_COUNT,
    sleep: float = 1.0,
) -> list[Cell]:
    """Steady state, then subagents, then arbitration, then overlays on top."""
    now = time.monotonic() if now is None else now
    sessions = list(sessions)
    board = blank_board(slot_count)

    claimed = set()
    for session in sessions:
        if 0 <= session.slot < slot_count:
            board[session.slot] = render(session, now)
            claimed.add(session.slot)

    if config.SUBAGENT_STACK:
        stack_subagents(board, sessions, claimed)
    arbitrate_motion(board, sessions)

    if config.AMBIENT and not claimed:
        ambient(board, now)

    # Under the overlays, not among them. Every overlay is a gesture -- a spawn
    # strike, a banner, a `/clear` wipe, a press-and-hold peek -- and every one
    # of those is itself the activity that wakes the board, so dimming them
    # would be the sleep arguing with its own wake. It also means quitting a
    # dark daemon still plays the shutdown spiral at full brightness.
    if sleep < 1.0:
        dim(board, sleep, frozenset(s.slot for s in sessions if s.alert))

    frozen = frozenset(claimed)
    for overlay in overlays:
        overlay.apply(board, now, frozen)
    return board
