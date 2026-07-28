"""Transient gestures painted over the steady-state board.

An overlay is how this board says something that is not a status: the board
unwrapping itself at boot, a strike when a session claims an encoder, the drain
and refill of a compaction, a fuse burning down under a held knob. They are separate from
:mod:`mft.render` because they are not about one session\'s state, and separate
from :mod:`mft.board` because the board is what they cover.

**Overlays are pure paint** (invariant 5). None of them touches session state,
so one dropped mid-flight leaves nothing to tear down -- which is what lets
:meth:`mft.daemon.Visualizer._live_overlays` retire them by simply forgetting
them, and lets every one of them be constructed, run and asserted against in a
test with no clock and no hardware.

Each class is independent; the shared parts are three small helpers here
(:func:`_handover` and the easing pair) and the grid walks in :mod:`mft.board`.
Adding a gesture means adding a class here and one line in
:meth:`mft.daemon.Visualizer._apply_effects`, and nothing else.
"""

from __future__ import annotations

import math
from typing import Optional

from . import config, font
from .board import (
    BLANK,
    Overlay,
    clamp01,
    smoothstep,
    bank_slots,
    spiral_path,
)
from .render import Cell, lerp
from .state import Session


def _handover(under: Cell, ring: float, settle: float, color: str | int | None) -> Cell:
    """The tail an encoder-sized gesture ends on: not a cut, a crossfade.

    Brightness and ring travel from wherever the gesture had them to whatever
    the steady state underneath is showing, so the encoder is seen *becoming*
    its own state rather than the gesture switching off and a color appearing
    separately. The hue crosses at the halfway point rather than being
    interpolated -- there is no meaningful blend between two points on a hue
    wheel -- so the last thing you see is the session's own color arriving.

    Shared by the spawn strike and the ``/clear`` wipe, which are the same
    gesture in different vocabularies and were the same eight lines twice.
    """
    return Cell(
        under.color if settle > 0.5 and under.color is not None else color,
        config.ANIM_NONE,
        int(ring + (under.ring - ring) * settle),
        1.0 + (under.brightness - 1.0) * settle,
    )


class TextOverlay(Overlay):
    """Spell a word across a bank, one 4x4 glyph at a time.

    Used for shouting a short reason at you (RATE when a turn dies on a rate
    limit) and for showing a count. Boot used to spell CLAUDE with this and no
    longer does -- a word you have read on every previous run is the part of an
    arrival you stop watching first.

    Each letter **strikes in at full brightness and then decays** before the
    next one strikes, so what separates two letters is a fade rather than a
    crossfade: a fade *out* leaves the glyph legible for the whole time it is
    visible, where a crossfade spends its middle showing a blend of two letters
    that is neither.

    The exception is a pixel the next letter also lights. That one holds instead
    of decaying -- it is a lamp staying on across the boundary, not one going
    out and another coming up in the same place -- so the board never blinks
    fully black between letters that overlap.

    ``color`` of ``None`` is the default and means the white LED ring alone,
    with the RGB switch underneath switched off -- the one thing on this device
    that is a color rather than a hue.
    """

    def __init__(
        self,
        text: str,
        started_at: float,
        color: str | int | None = config.TEXT_COLOR,
        bank: int = 0,
        fade: float = config.TEXT_FADE_SECONDS,
        hold: float = config.TEXT_HOLD_SECONDS,
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
        level = 1.0 - smoothstep(max(0.0, within - self.hold) / self.fade)
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
            return self._dismiss_gain * clamp01(1.0 - gone)
        t = now - self.started_at
        u = clamp01(t / self.duration)
        rise = smoothstep(t / config.WAITING_FADE_IN_SECONDS)
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
        return clamp01(0.62 * diagonal + 0.48 * across) * breath

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
            # Colorless, like the boot gesture it follows: the ring carries the
            # whole thing and the RGB stays dark. This is the one stretch where the
            # board is saying nothing, and the hue channel is how it says
            # things -- so it has no business being lit here.
            board[slot] = Cell(None, config.ANIM_NONE, int(127 * level), level)


class ShutdownOverlay(Overlay):
    """The daemon is leaving: one head, top-left corner, spiralling to the centre.

    Three movements, in order:

    * the **spiral** walks :func:`spiral_path` from the corner inwards, every
      encoder it passes fading up in the device's own violet;
    * the **hold** leaves the completed board whole and still for a beat, so it
      reads as somewhere the gesture arrived rather than a frame on the way
      down;
    * the **fade** dims the whole board out uniformly -- one hue throughout, no
      hue travel: the color is not doing anything on the way out, the lamp is
      simply going down. All sixteen go together, which is the load-bearing
      part, since every other animation here is per-encoder. And it goes all the
      way *off* rather than down to the hardware's minimum brightness, which is
      still a lit encoder wearing a color.

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
        gain = 1.0 - smoothstep(gone) if gone > 0 else 1.0
        hue = self.hue

        for slot, arrival in self._arrivals:
            if not 0 <= slot < len(board):
                continue
            level = smoothstep((t - arrival) / self.rise) * gain
            # Color-free below the floor, not merely dim: an encoder still
            # holding a hue at brightness zero is an encoder still lit.
            if level <= config.SHUTDOWN_DARK_LEVEL:
                board[slot] = BLANK
                continue
            board[slot] = Cell(hue, config.ANIM_NONE, int(127 * level), level)


class UnwrapOverlay(Overlay):
    """The daemon arriving: the exit gesture played backwards.

    :class:`ShutdownOverlay` is a spiral in and a fade out in unison. This is
    that read right-to-left, and it is the same three movements in the opposite
    order:

    * the **rise** brings all sixteen encoders up together, white rings over one
      hue, the way the exit comes up over its violet -- blue here, so which end
      of a run you are watching is legible from across the room;
    * the **hold** leaves the full board still for a beat, so it is seen whole
      at least once rather than only on the way through;
    * the **unwrap** walks :func:`spiral_path` *reversed* -- centre outward to
      the top-left corner -- letting each encoder go dark as the head leaves it,
      so the board comes apart along exactly the line it closed on.

    Reversing the path rather than reusing it forward is the whole point: the
    board ends dark in the corner where the exit's head began, and the pair reads
    as one gesture the daemon undoes on the way in and redoes on the way out. It
    hands over to a black board -- no hue left anywhere on it, which is what the
    waiting gradients that follow want underneath them.
    """

    def __init__(
        self,
        started_at: float,
        bank: int = 0,
        rise: float = config.BOOT_UNWRAP_RISE_SECONDS,
        hold: float = config.BOOT_UNWRAP_HOLD_SECONDS,
        spiral: float = config.BOOT_UNWRAP_SPIRAL_SECONDS,
        fall: float = config.BOOT_UNWRAP_FALL_SECONDS,
    ) -> None:
        self.started_at = started_at
        self.bank = bank
        self.rise = max(0.01, rise)
        self.hold = max(0.0, hold)
        self.spiral = max(0.01, spiral)
        #: Clamped below the travel time, same as the shutdown rise: a fall
        #: longer than the journey would leave the corner still lit at the end.
        self.fall = max(0.01, min(fall, self.spiral * 0.5))
        self.hue = int(
            config.COLORS.get(config.BOOT_UNWRAP_COLOR, config.BOOT_UNWRAP_COLOR)
        )
        #: (slot, departure time) along the reversed spiral, everything dark by
        #: the end of the travel. Built once rather than on each of the ~100
        #: frames, and offset by the rise and hold so a departure time is
        #: measured from the overlay's own start.
        path = tuple(reversed(spiral_path(self.bank)))
        travel = max(0.01, self.spiral - self.fall)
        head = self.rise + self.hold
        self._departures = tuple(
            (slot, head + step / max(1, len(path) - 1) * travel)
            for step, slot in enumerate(path)
        )

    @property
    def duration(self) -> float:
        return self.rise + self.hold + self.spiral

    def done(self, now: float) -> bool:
        return now - self.started_at >= self.duration

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        t = now - self.started_at
        if t < 0 or t >= self.duration:
            return
        # One envelope over the whole board, applied before the per-encoder
        # fall: this is the "up in unison" part, and it is indifferent to where
        # in the unwrap a given encoder will be let go.
        gain = smoothstep(t / self.rise)

        for slot, departure in self._departures:
            if not 0 <= slot < len(board):
                continue
            level = gain * (1.0 - smoothstep((t - departure) / self.fall))
            # The same floor the exit uses, for the same reason: a ring at the
            # bottom of a fade is still a lit encoder, and this one has to hand a
            # genuinely black board to whatever the daemon says next.
            if level <= config.SHUTDOWN_DARK_LEVEL:
                board[slot] = BLANK
                continue
            board[slot] = Cell(self.hue, config.ANIM_NONE, int(127 * level), level)


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
        settle = smoothstep(max(0.0, u - config.SPAWN_SETTLE) / (1 - config.SPAWN_SETTLE))
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

        fill = 1.0 - smoothstep(min(1.0, u / config.CLEAR_SETTLE))
        # Same handover as the spawn strike, into white rather than red: the
        # wipe is over and the encoder arrives at whatever it now is.
        settle = smoothstep(max(0.0, u - config.CLEAR_SETTLE) / (1 - config.CLEAR_SETTLE))
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
            drain = clamp01((now - self.started_at) / config.COMPACT_DRAIN_SECONDS)
            fill = 1.0 - drain
            color = config.STATE_COLORS["working"] if fill > 0.05 else "purple"
        else:
            fill = clamp01((now - self.finished_at) / config.COMPACT_REFILL_SECONDS)
            color = "purple" if fill < 0.05 else config.STATE_COLORS["working"]
        board[self.session.slot] = Cell(
            color, config.ANIM_NONE, int(127 * fill), 0.35 + 0.5 * fill
        )


class DismissOverlay(Overlay):
    """Hold an encoder and watch it empty: the ring burns down like a fuse, and
    when it reaches nothing the session is off the board.

    The gesture answers the one thing a display cannot fix by itself. A record
    can outlive the agent it describes -- a terminal killed from the window
    manager, a machine that woke somewhere else, a pid the census cannot
    resolve -- and invariant 6 says a knob that lies is worse than a knob that
    is missing. Retiring one otherwise means waiting out the TTL or restarting
    the daemon; this is a hand saying "that one is gone", which is a thing only
    the hand knows.

    It is still not a control surface (invariant 1): nothing here reaches the
    agent. Clearing a session that is in fact alive costs nothing and repairs
    itself -- its next hook event claims an encoder again, with a spawn strike
    on it, exactly as if it had just started.

    **The countdown is the point.** The drain arms a fraction of a second in, so
    a tap on the way to focusing a tab never flashes it, and then empties so
    that it reaches zero at the instant the hold matures. You can see the clear
    coming and let go, which is what makes a destructive gesture safe to hang on
    the same knob as the harmless one. White over a dark RGB, the same
    vocabulary as boot and the ``/clear`` wipe: this is the board talking about
    itself rather than reporting a state.

    Pure paint like every overlay (invariant 5) -- :meth:`matured` only reports
    that the fuse has burned down, and :mod:`mft.daemon` is what drops the
    session.
    """

    def __init__(
        self,
        session: Session,
        started_at: float,
        arm: float = config.DISMISS_ARM_SECONDS,
        duration: float = config.HOLD_SECONDS,
    ) -> None:
        #: The session, not its slot: the board compacts under everything that
        #: leaves it, so a slot is not a handle that survives a frame.
        self.session = session
        self.started_at = started_at
        self.armed_at = started_at + min(arm, duration)
        self.matures_at = started_at + duration
        self._released = False

    def release(self) -> None:
        """The finger came off before the fuse ran out."""
        self._released = True

    def matured(self, now: float) -> bool:
        return not self._released and now >= self.matures_at

    def done(self, now: float) -> bool:
        # Done at maturity as well as on release: the daemon retires it there,
        # and this is the backstop that stops a burnt-down fuse from painting a
        # slot the session it names no longer owns.
        return self._released or now >= self.matures_at

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        if now < self.armed_at:
            return
        slot = self.session.slot
        if not 0 <= slot < len(board):
            return
        span = max(0.01, self.matures_at - self.armed_at)
        fill = 1.0 - clamp01((now - self.armed_at) / span)
        board[slot] = Cell(None, config.ANIM_NONE, int(127 * fill), 1.0)


class FocusOverlay(Overlay):
    """You just switched to this session's tab: swell its encoder, once.

    The counterpart to :class:`SpawnOverlay`, and deliberately the opposite
    gesture. A spawn is news the board is telling *you*, so it strikes in a
    color of its own; this is the board acknowledging something you already
    know, so it says nothing new -- it lifts the session's own hue and its own
    ring to full and lets them fall back to whatever they were. You should be
    able to watch it out of the corner of your eye and learn only *which of the
    sixteen*, which is the entire question a marker answers.

    It drops the animation for its half second, and that is not a cosmetic
    choice: brightness does not reach the RGB while an animation is on it
    (:meth:`mft.twister.Twister.write`), so a swell over a strobing permission
    gate would otherwise be a swell you cannot see. The strobe comes back the
    moment the overlay retires -- and if you are now looking at the tab it was
    strobing about, half a second of calm is the truthful thing for it to do.

    **The gesture is on the RGB only.** The ring is left exactly where the cell
    underneath had it, which for the focused encoder is
    :data:`config.ATTENTION_RING_LEVEL` -- the marker, arriving and then simply
    staying. That is the point of the pair: the swell is the event and the ring
    is the state, and a marker that flinches every time it is set is a worse
    marker.

    Two ring gestures were tried here and both are gone. Swelling *up* is not
    available: :func:`mft.board.mark_focus` has already pinned this encoder at
    full, so a strike to full settling onto full is a four-frame ramp onto a
    level it was already at -- which read, correctly, as "the ring doesn't
    pulse". Dipping *down* is available, and legible, and wrong: the only
    envelope a light already at maximum can run is out-and-back, which is a
    different motion to the RGB's strike-and-settle no matter how carefully the
    two are given the same duration. They stopped reading as one gesture.

    (Both of those were designed against a channel that was doing nothing at
    all -- see :data:`config.RING_ANIM_OFFSET`. The dip was judged for real once
    the band was fixed, and lost on its merits.)

    Pure paint like every overlay (invariant 5). The *state* half of arriving in
    a tab -- forgiving the attention debt, waking the board -- happens in
    :meth:`mft.daemon.Visualizer._check_attention`, where mutation belongs.
    """

    def __init__(
        self,
        session: Session,
        started_at: float,
        duration: float = config.ATTENTION_PULSE_SECONDS,
    ) -> None:
        #: The session, not its slot: an overlay outlives the frame it was made
        #: in and the board compacts under sessions that end.
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
        if under is BLANK:
            # An ended or sleeping encoder is not a thing to point at. Nothing
            # to tear down either -- see the class docstring.
            return

        rise = config.ATTENTION_PULSE_RISE
        if u < rise:
            # Up from dark, linearly, so the leading edge is a strike -- and
            # *replacing* what is underneath rather than brightening it.
            #
            # A `max` against the session's own level was the obvious reading of
            # "never dim what is already lit", and it made the gesture invisible
            # on exactly the encoders worth pointing at: a session already at
            # full swallowed the whole swell and did nothing at all. A pulse is a
            # change, not a level, and the only change left to an encoder that is
            # already as bright as the pulse is to go dark first.
            swell, settle = clamp01(u / rise), 0.0
        else:
            # And back down onto its own state, not down to nothing: the overlay
            # retires at the end of this and the cell underneath returns in one
            # frame, so a fall to zero would put a black frame between the
            # gesture and the state it is handing back to. Eased, so the encoder
            # is seen settling rather than switching off.
            swell, settle = 1.0, smoothstep(clamp01((u - rise) / (1.0 - rise)))
        board[slot] = Cell(
            under.color,
            config.ANIM_NONE,
            under.ring,
            lerp(swell, under.brightness, settle),
            # Handed straight through, on purpose -- see the class docstring.
            # Passed explicitly rather than left to default, because `None` here
            # would mean "follow the brightness", and the brightness is the one
            # thing on this cell that is mid-gesture.
            under.ring_light,
        )

