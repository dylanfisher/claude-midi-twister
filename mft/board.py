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
from typing import Iterable, Optional, Sequence

from . import config, font
from .render import Cell, render
from .state import Session, priority


def blank_board(slot_count: int = config.SLOT_COUNT) -> list[Cell]:
    return [Cell() for _ in range(slot_count)]


def bank_of(slot: int) -> int:
    return slot // config.ENCODERS_PER_BANK


def bank_slots(bank: int) -> range:
    start = bank * config.ENCODERS_PER_BANK
    return range(start, start + config.ENCODERS_PER_BANK)


def spawn_order(bank: int) -> list[int]:
    """Where subagents land, in the order they take it.

    Sessions are handed encoders from the top-left forwards, so subagents fill
    from the **bottom-right backwards**: the two allocators grow toward each
    other and the far corner is always the newest thing on the board. Reading
    the pile from the corner inwards is reading it newest-first.
    """
    return list(reversed(bank_slots(bank)))


# --- overlays ---------------------------------------------------------------


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


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

    Each letter **strikes in at full brightness and then decays to nothing**
    before the next one strikes. Two letters are never on the board at once, so
    the darkness between them is what separates them: a fade *out* leaves the
    glyph legible for the whole time it is visible, where a crossfade spends its
    middle showing a blend of two letters that is neither.

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
        self.step = self.fade + self.hold

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
        for offset, slot in enumerate(bank_slots(self.bank)):
            lit = glyph[offset] * level
            board[slot] = Cell(
                self.color if lit > 0.02 else None,
                config.ANIM_NONE,
                127 if lit > 0.02 else 0,
                lit,
            )


class LampTestOverlay(Overlay):
    """Every LED once, then a generative field that runs until Claude shows up.

    Two things in one overlay, because they are two halves of one gesture:

    * the **sweep** is the lamp test proper, in the aircraft sense -- one arc
      travelling across all 16 rings so a dead LED has nowhere to hide;
    * the **field** it dissolves into is four travelling sine waves at mutually
      unrelated rates, two of them driving brightness and two driving hue, with
      the ripple's centre drifting. Nothing about it loops, so it never settles
      into a pattern your eye can finish and stop watching.

    It is deliberately the *idle* state and nothing else: it fades out over a
    minute on its own, and :meth:`dismiss` pulls it off the board the moment a
    real session appears. It never paints a claimed encoder, and everywhere else
    it only writes a cell it would light *more* than whatever is underneath, so
    nothing the board actually has to say is ever dimmed by decoration.
    """

    def __init__(
        self,
        started_at: float,
        bank: int = 0,
        sweep: float = config.LAMP_TEST_SWEEP_SECONDS,
        duration: float = config.LAMP_TEST_SECONDS,
    ) -> None:
        self.started_at = started_at
        self.bank = bank
        self.sweep = max(0.01, sweep)
        self.duration = max(self.sweep, duration)
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
            return now - self.dismissed_at >= config.LAMP_TEST_DISMISS_SECONDS
        return now - self.started_at >= self.duration

    def _gain(self, now: float) -> float:
        """The overall fade envelope: a cosine ease over the full minute, so it
        holds near full for a while and then slides away, rather than visibly
        dimming from the first second."""
        if self.dismissed_at is not None:
            gone = (now - self.dismissed_at) / config.LAMP_TEST_DISMISS_SECONDS
            return self._dismiss_gain * max(0.0, 1.0 - gone)
        u = max(0.0, min(1.0, (now - self.started_at) / self.duration))
        return (1 + math.cos(math.pi * u)) / 2

    def _sweep_fill(self, offset: int, t: float) -> float:
        local = (t / self.sweep) * (config.ENCODERS_PER_BANK + 6) - offset
        return max(0.0, min(1.0, local / 6.0))

    @staticmethod
    def _field(x: float, y: float, t: float) -> tuple[float, float]:
        """Two independent scalar fields over the 4x4 grid, in -1..1.

        The rates share no common factor, which is the whole trick: the sum
        never repeats, so sixteen pixels are enough to read as motion with
        somewhere to go. Brightness and hue come off *different* fields --
        locking them together would collapse the whole thing back into one
        heightmap wearing a gradient.
        """
        cx = 1.5 + 1.2 * math.sin(0.11 * t)
        cy = 1.5 + 1.2 * math.cos(0.13 * t)
        level = (
            math.sin(1.7 * x + 0.90 * t)
            + math.sin(1.3 * y - 0.61 * t)
            + math.sin(0.9 * (x + y) + 0.37 * t)
            + math.sin(2.1 * math.hypot(x - cx, y - cy) - 1.13 * t)
        ) / 4
        hue = (
            math.sin(0.8 * y - 0.23 * t) + math.sin(0.7 * (x - y) + 0.17 * t)
        ) / 2
        return level, hue

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        t = now - self.started_at
        if t < 0:
            return
        gain = self._gain(now)
        # The sweep runs to completion first -- every ring has to actually reach
        # full or it is not a lamp test -- and only then dissolves into the
        # field, so the two read as one gesture rather than two clips spliced.
        mix = _smoothstep((t - self.sweep) / (0.8 * self.sweep))
        low, high = config.LAMP_TEST_HUES

        for offset, slot in enumerate(bank_slots(self.bank)):
            if slot in claimed:
                continue  # a session's own encoder is never decoration
            x = float(offset % 4)
            y = float(offset // 4)
            level, hue_field = self._field(x, y, t)
            level = _smoothstep((level + 1) / 2)
            level = level * mix + self._sweep_fill(offset, t) * (1 - mix)
            level *= gain
            # Ambient breathing underneath is left alone wherever it is already
            # the brighter of the two, which is what turns the tail of the fade
            # into a crossfade back to the idle board rather than a blackout.
            if level <= board[slot].brightness:
                continue
            hue = int(low + (high - low) * (hue_field + 1) / 2)
            board[slot] = Cell(hue, config.ANIM_NONE, int(127 * level), level)


def spiral_path(bank: int = 0) -> list[int]:
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
    return path


class ShutdownOverlay(Overlay):
    """The daemon is leaving: one head, top-left corner, spiralling to the centre.

    Three movements, in order:

    * the **spiral** walks :func:`spiral_path` from the corner inwards, every
      encoder it passes fading up in the device's own violet;
    * the **cycle** then runs the full hue wheel with all sixteen encoders on
      *one* hue. Unison is the load-bearing part -- every other animation on
      this board is per-encoder, so sixteen knobs changing colour as a single
      object is a thing that can only mean the end;
    * the **fade** takes the whole board out uniformly, and all the way *off*
      rather than down to the hardware's minimum brightness, which is still a
      lit encoder wearing a colour.

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
        cycle: float = config.SHUTDOWN_CYCLE_SECONDS,
        fade: float = config.SHUTDOWN_FADE_SECONDS,
    ) -> None:
        self.started_at = started_at
        self.bank = bank
        self.spiral = max(0.01, spiral)
        #: Clamped below the travel time: a rise longer than the journey would
        #: mean the centre never reaches full before the cycle starts.
        self.rise = max(0.01, min(rise, self.spiral * 0.5))
        self.cycle = max(0.0, cycle)
        self.fade = max(0.01, fade)
        self.base_hue = config.COLORS.get(config.SHUTDOWN_COLOR, config.SHUTDOWN_COLOR)

    @property
    def duration(self) -> float:
        return self.spiral + self.cycle + self.fade

    def done(self, now: float) -> bool:
        return now - self.started_at >= self.duration

    def _arrivals(self) -> list[tuple[int, float]]:
        """(slot, arrival time) along the spiral, everything full by the end of
        the travel: the last encoder starts rising a rise-length before it."""
        path = spiral_path(self.bank)
        travel = max(0.01, self.spiral - self.rise)
        return [
            (slot, step / max(1, len(path) - 1) * travel)
            for step, slot in enumerate(path)
        ]

    def _hue(self, t: float) -> int:
        """One hue for the whole board, off the clock alone.

        Position never enters into it, which is what keeps the sixteen encoders
        reading as one object: during the spiral they are all the boot violet,
        and during the cycle they all travel the wheel together and arrive back
        where they started.
        """
        if t <= self.spiral or not self.cycle:
            return int(self.base_hue)
        turn = min(1.0, (t - self.spiral) / self.cycle)
        return int((self.base_hue + turn * 128) % 128)

    def apply(self, board: list[Cell], now: float, claimed=frozenset()) -> None:
        t = now - self.started_at
        if t < 0 or t >= self.duration:
            return
        # One envelope over the whole board, applied after the per-encoder rise:
        # this is the "uniformly out" part, and it has to be indifferent to
        # where in the spiral a given encoder was lit.
        gone = (t - self.spiral - self.cycle) / self.fade
        gain = 1.0 - _smoothstep(gone) if gone > 0 else 1.0
        hue = self._hue(t)

        for slot, arrival in self._arrivals():
            if not 0 <= slot < len(board):
                continue
            level = _smoothstep((t - arrival) / self.rise) * gain
            # Colour-free below the floor, not merely dim: an encoder still
            # holding a hue at brightness zero is an encoder still lit.
            if level <= config.SHUTDOWN_DARK_LEVEL:
                board[slot] = Cell()
                continue
            board[slot] = Cell(hue, config.ANIM_NONE, int(127 * level), level)


class SpawnOverlay(Overlay):
    """A session just claimed this encoder: strike it, then settle.

    The arrival of a Claude is otherwise the quietest thing that happens on the
    board -- a new session renders as `idle`, which is a dim green pip
    indistinguishable at a glance from the dim green pip beside it. So the
    claiming gets a gesture of its own: the ring fills once from empty to full,
    decelerating into it, while the hue runs the entire colour wheel at full
    brightness. One sweep rather than a spin -- a ring that laps itself is how
    every *activity* signal on this board works, and this is a single event that
    happened once. Nothing else here sweeps its hue, so there is no other thing
    on the board it can be confused for.

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

        # Ease-out, so the fill arrives quickly and decelerates into full rather
        # than stopping dead: the deceleration is what makes it read as landing.
        fill = min(1.0, u / config.SPAWN_SETTLE)
        fill = 1 - (1 - fill) ** 2
        low, high = config.SPAWN_HUES
        color: str | int | None = int(low + (high - low) * u)

        # The tail is a handover, not a cut: brightness, hue and ring all arrive
        # at whatever is underneath, so the full ring visibly recedes onto the
        # session's own ring position instead of snapping to it.
        settle = _smoothstep(max(0.0, u - config.SPAWN_SETTLE) / (1 - config.SPAWN_SETTLE))
        brightness = 1.0 + (under.brightness - 1.0) * settle
        ring = 127 * fill + (under.ring - 127 * fill) * settle
        if settle > 0.5 and under.color is not None:
            # Hand the hue over before the brightness has finished falling, so
            # the last thing you see is the session's own colour arriving.
            color = under.color
        board[slot] = Cell(color, config.ANIM_NONE, int(ring), brightness)


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
        if self.finished_at is None:
            drain = min(1.0, (now - self.started_at) / config.COMPACT_DRAIN_SECONDS)
            fill = 1.0 - drain
            color = config.STATE_COLORS["working"] if fill > 0.05 else "purple"
        else:
            fill = min(1.0, (now - self.finished_at) / config.COMPACT_REFILL_SECONDS)
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
        history = list(self.session.tool_history)
        others = [s for s in bank_slots(bank_of(held)) if s != held]
        # Newest lands on the slot nearest the held knob, so the history reads
        # outward from the thing you are holding.
        recent = history[-len(others) :]
        padding = [None] * (len(others) - len(recent))
        for slot, tool in zip(others, padding + recent):
            if tool is None:
                board[slot] = Cell()
                continue
            color = config.TOOL_COLORS.get(tool, config.TOOL_COLOR_DEFAULT)
            board[slot] = Cell(color, config.ANIM_NONE, 127, 0.7)
        board[held] = Cell("purple", config.ANIM_NONE, 127, 1.0)


# --- composition ------------------------------------------------------------


def arbitrate_motion(board: list[Cell], sessions: Sequence[Session]) -> None:
    """Leave the fast animation on exactly one encoder.

    Several sessions blocking at once would otherwise fill the board with
    competing strobes, and a board where everything moves is a board where
    nothing stands out. The winner is the highest-priority attention state,
    oldest first; everyone else drops to a slow pulse.
    """
    animated = [s for s in sessions if board[s.slot].rgb_anim and not s.snoozed]
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


def stack_subagents(
    board: list[Cell], sessions: Sequence[Session], claimed: set[int]
) -> None:
    """Pile in-flight subagents into the bottom-right of their parent's bank.

    They get a hue used for nothing else, a stub ring and the slowest pulse on
    the board, because the one thing you must never do is mistake a subagent for
    a session: it owns no encoder, answers no gesture, and disappears when the
    parent's turn ends. Anything already claimed -- a real session, another
    parent's subagents -- is skipped rather than trampled, so the pile shrinks
    into whatever room is left instead of stealing someone's slot.
    """
    # Deterministic across frames: parents are served in encoder order, so a
    # subagent doesn't hop to a different knob because a dict reordered.
    for session in sorted(sessions, key=lambda s: s.slot):
        if not session.subagents or session.snoozed:
            continue
        free = (s for s in spawn_order(bank_of(session.slot)) if s not in claimed)
        for slot in list(free)[: session.subagents]:
            claimed.add(slot)
            board[slot] = Cell(
                config.SUBAGENT_COLOR,
                config.SUBAGENT_ANIM,
                config.SUBAGENT_RING,
                config.SUBAGENT_BRIGHTNESS,
            )


def ambient(board: list[Cell], now: float) -> None:
    """Nothing is running, so the board breathes rather than going dark."""
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

    for overlay in overlays:
        overlay.apply(board, now, frozenset(claimed))
    return board
