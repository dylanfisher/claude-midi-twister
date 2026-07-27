"""The whole device as one frame buffer.

:mod:`mft.render` decides what one session looks like. :mod:`mft.overlays`
decides what a transient gesture looks like. Everything that is only decidable
*across the whole board* lives here:

* **motion arbitration** -- at most one encoder moves fast at a time, and it is
  always the one where a human is blocking progress. Motion is the only thing
  peripheral vision reliably catches, so it is a budget, not a decoration.
* **the subagent stack** -- subagents pile up from the bottom-right of the
  parent's bank into *unclaimed* encoders, in a hue of their own, making
  parallelism physically visible without disturbing anyone's slot.
* **sleep** -- a standing dimming of everything at once, because the room is
  empty rather than because any one session got older.
* **the focus marker** -- at most one encoder is the tab in front of you, which
  makes "which one" a question about the whole board and not about any session.
* **composition** -- :func:`compose`, which puts those in the one order that
  works and then lets the overlays paint on top.

Also the grid itself: :func:`bank_of`, :func:`bank_slots`, :func:`spawn_order`
and :func:`spiral_path` are the four ways this project walks sixteen encoders,
and they are here rather than in :mod:`mft.config` because each is a decision
about what a gesture should look like, not a tunable.

Everything in this module is pure. It is handed sessions and a clock and hands
back a list of :class:`~mft.render.Cell`; the wire is :mod:`mft.twister`\'s and
the timing is :mod:`mft.daemon`\'s.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from functools import lru_cache
from itertools import islice
from typing import Iterable, Iterator, Optional, Sequence

from . import config
from .render import Cell, lerp, render, stopwatch_ring
from .state import Session, priority

# --- the grid ---------------------------------------------------------------

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


# --- easing -----------------------------------------------------------------
#
# Public, and imported by :mod:`mft.overlays`: these two plus
# :func:`mft.render.lerp` are the whole easing vocabulary of the project, and
# every ramp in it is one of the three. Anything that reaches for its own
# easing is either a new gesture worth arguing about or a copy of one of these.


def clamp01(t: float) -> float:
    """0..1, whatever came in. Every animation here is an elapsed time over a
    duration, and asking one for a frame outside its own window is normal."""
    return max(0.0, min(1.0, t))


def smoothstep(t: float) -> float:
    """Eased 0..1. Linear ramps on these LEDs read as dropping off a cliff at
    the end; this keeps the tail of a fade visible, which is the part that
    separates one gesture from the next."""
    t = clamp01(t)
    return t * t * (3 - 2 * t)


# --- what paints over it ----------------------------------------------------
#
# The base class only; the gestures themselves are :mod:`mft.overlays`. It
# lives here because :func:`compose` below is the thing that applies them, and
# a board that had to import its own decorations to describe them would be a
# cycle.


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


# --- who is loudest ---------------------------------------------------------


def attention_rank(session: Session) -> tuple[int, float]:
    """Sort key for "which of these most needs a human", worst first.

    State before age, so a permission gate always outranks a plan however long
    the plan has been sitting there; then the moment the debt started, so the
    oldest of two equal blocks wins. A session that owes nothing is ranked on
    when it entered the state instead, which is the same question asked of a
    thing with no debt to date from.

    One function because it is one decision, asked in the two places that must
    never disagree about it: :func:`arbitrate_motion` picks the encoder that
    gets to move fast, and :func:`bank_to_show` picks the bank you get to see.
    A board that strobes one knob and follows a different one is worse than
    either policy alone.
    """
    owed = session.attention_since
    return priority(session.state), session.state_since if owed is None else owed


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
    with no hardware and the wire stays in :class:`mft.banks.BankFollower`, which
    owns the cooldown too: this function has no memory and will happily name the
    same bank every frame.
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
    want = bank_of(min(blocking, key=attention_rank).slot)
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
    animated.sort(key=attention_rank)
    for session in animated[1:]:
        cell = board[session.slot]
        board[session.slot] = replace(cell, rgb_anim=config.SLOW_ANIM)


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
    return {slot: session for slot, session, _ in _subagent_pile(
        sessions, claimed, slot_count
    )}


def _subagent_pile(
    sessions: Sequence[Session],
    claimed: Iterable[int] = (),
    slot_count: int = config.SLOT_COUNT,
) -> Iterator[tuple[int, Session, int]]:
    """Placement proper: ``(slot, parent, index within that parent's pile)``.

    The index is what the painter needs and the daemon does not, which is why
    this sits behind :func:`subagent_owners` rather than replacing it -- a press
    resolves to a parent and nothing finer, and widening that return type would
    invite a caller to think otherwise.
    """
    taken = set(claimed)
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
        for index, slot in enumerate(islice(free, count)):
            taken.add(slot)
            yield slot, session, index


def subagent_brightness(last_tool_at: Optional[float], now: float) -> float:
    """How lit one violet dot is, given when its subagent last called a tool.

    Bright on the call, decaying to a floor between them -- :func:`lerp` on the
    same shape as a session's shimmer, minus the stall stage. A subagent has no
    stall reading worth drawing: it is short-lived by construction and dies with
    its parent's turn, so "quiet for two minutes" is a state the dot will rarely
    live long enough to reach, and sinking it toward invisible would only hide
    the one that hung.

    ``None`` -- no per-subagent signal at all -- holds the flat level the whole
    pile used to sit at, so an install without SubagentStart loses nothing.
    """
    if last_tool_at is None or not config.SUBAGENT_SHIMMER:
        return config.SUBAGENT_BRIGHTNESS
    kick = max(0.0, 1.0 - (now - last_tool_at) / config.SUBAGENT_KICK_SECONDS)
    return lerp(config.SUBAGENT_IDLE_BRIGHTNESS, config.SUBAGENT_KICK_BRIGHTNESS, kick)


def subagent_ring(started_at: Optional[float], now: float) -> int:
    """How far round one violet dot's ring is, given when its subagent spawned.

    The same stopwatch a session's turn ring wears (:data:`config.STOPWATCH_CURVE`),
    over :data:`config.SUBAGENT_RING_SECONDS` and then held full: a quarter ring
    is five minutes out, half is fifteen, three quarters is thirty, all the way
    round is an hour. One scale for both, because the dots sit on the same board
    as the encoders and a ring that meant two different things depending on which
    it was drawn on would be a scale you have to look twice to read.

    ``None`` -- no spawn time at all, or the stopwatch switched off -- falls back
    to the flat stub every dot used to wear, so nothing about the pile gets worse
    on an install that can't feed this.
    """
    if started_at is None or not config.SUBAGENT_TIME_RING:
        return config.SUBAGENT_RING
    return stopwatch_ring(
        now - started_at, config.SUBAGENT_RING_SECONDS, config.SUBAGENT_RING_FLOOR
    )


def stack_subagents(
    board: list[Cell], sessions: Sequence[Session], claimed: set[int], now: float
) -> None:
    """Pile in-flight subagents into the bottom-right of their parent's bank.

    They get a hue used for nothing else and no animation at all, because the one
    thing you must never do is mistake a subagent for a session: it owns no
    encoder of its own, it answers only the one gesture its parent would have
    answered, and it disappears when the parent's turn ends.

    Two things move, and both are quantities rather than states. Brightness, per
    dot, on each tool call that subagent makes (:func:`subagent_brightness`), and
    the ring, filling with how long the subagent has been out
    (:func:`subagent_ring`). Between them a hung fan-out is legible from across
    the room: a dot that stopped shimmering with most of its ring gone. Neither
    crosses the line -- a level and a stopwatch are not states, and no amount of
    either makes a violet dot read as a session, because the hue never moves.
    """
    activity: dict[int, list[Optional[float]]] = {}
    started: dict[int, list[Optional[float]]] = {}
    for slot, session, index in _subagent_pile(sessions, claimed, len(board)):
        claimed.add(slot)
        # Per parent, not per dot: both properties walk the whole pile, and a
        # sixteen-wide fan-out would otherwise rebuild both lists sixteen times.
        stamps = activity.setdefault(session.slot, session.subagent_activity)
        births = started.setdefault(session.slot, session.subagent_started)
        board[slot] = Cell(
            config.SUBAGENT_COLOR,
            config.SUBAGENT_ANIM,
            subagent_ring(births[index] if index < len(births) else None, now),
            subagent_brightness(stamps[index] if index < len(stamps) else None, now),
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
                return self._woke_from + (1.0 - self._woke_from) * smoothstep(rising)

        away = now - self.last_activity
        dark = (away - config.SLEEP_DARK_SECONDS) / config.SLEEP_FADE_SECONDS
        if dark > 0:
            return config.SLEEP_DIM_LEVEL * (1.0 - smoothstep(dark))
        dim = (away - config.SLEEP_DIM_SECONDS) / config.SLEEP_FADE_SECONDS
        if dim > 0:
            return 1.0 - (1.0 - config.SLEEP_DIM_LEVEL) * smoothstep(dim)
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
        # The ring's own level goes down with everything else when it has one --
        # a gauge that stayed lit on a sleeping board would be the one thing
        # still shining in a dark room, which is exactly what sleeping is for.
        ring_level = None if cell.ring_level is None else cell.ring_level * gain
        board[slot] = Cell(
            cell.color, config.ANIM_NONE, int(cell.ring * gain), level, ring_level
        )


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


def cap_rings(board: list[Cell]) -> None:
    """Hold every ring under :data:`config.RING_CEILING`, so the marker has room.

    Run *before* :func:`mark_focus`, which is the entire arrangement: the cap
    takes the top off the board and the marker is the one encoder allowed back
    up there. Doing it the other way round would cap the marker too, and a
    marker at the same level as everything else is not a marker.

    The ring only, and by setting ``ring_level`` rather than ``brightness``: the
    hue is how loudly a session is asking for you and nothing here is entitled
    to quieten that. It is a separate channel (6, against the RGB's 3) and this
    is exactly the case the split exists for -- see :class:`mft.render.Cell`.

    Ring *position* is untouched. A gauge that reads three-quarters full still
    reads three-quarters full; it is the light behind it that moved.
    """
    ceiling = config.RING_CEILING
    if ceiling >= 1.0:
        return
    for slot, cell in enumerate(board):
        if cell is BLANK:
            continue
        if cell.ring_light > ceiling:
            board[slot] = replace(cell, ring_level=ceiling)


def mark_focus(board: list[Cell], slot: int) -> None:
    """Hold one encoder's ring at full: the tab you are looking at.

    The ring and nothing else, which is the whole reason this is legible. Hue
    says what a session is doing and the RGB channel's brightness is spent on
    how much it wants you -- and is discarded outright while an animation is on
    it -- so a marker there would be invisible on exactly the busy sessions
    worth marking. The ring carries its own level on its own channel, so it
    reads over a shimmer, a sweep or a strobe without arguing with any of them.

    Ring *position* is left alone for the same reason: it is a gauge, a
    stopwatch or an arc, and overwriting it would trade a fact for a pointer.

    Nothing happens to a blank encoder. An ended session is not somewhere you
    are, whatever the window server thinks the app in front is.
    """
    if not 0 <= slot < len(board):
        return
    cell = board[slot]
    if cell is BLANK:
        return
    board[slot] = replace(cell, ring_level=config.ATTENTION_RING_LEVEL)


def compose(
    sessions: Iterable[Session],
    now: Optional[float] = None,
    overlays: Iterable[Overlay] = (),
    slot_count: int = config.SLOT_COUNT,
    sleep: float = 1.0,
    focused: Optional[int] = None,
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
        stack_subagents(board, sessions, claimed, now)
    arbitrate_motion(board, sessions)

    if config.AMBIENT and not claimed:
        ambient(board, now)

    # After arbitration, before the dimming: the marker is not motion and has
    # nothing to win from the budget, but it *is* a thing a sleeping board
    # should keep -- a tab in front of you is the strongest evidence there is
    # that somebody is here, so `dim` is told to spare it below.
    #
    # The cap first and the marker second, always: the ceiling is what makes the
    # marker the brightest ring on the board rather than one of several.
    cap_rings(board)
    if focused is not None:
        mark_focus(board, focused)

    # Under the overlays, not among them. Every overlay is a gesture -- a spawn
    # strike, a banner, a `/clear` wipe, a press-and-hold peek -- and every one
    # of those is itself the activity that wakes the board, so dimming them
    # would be the sleep arguing with its own wake. It also means quitting a
    # dark daemon still plays the shutdown spiral at full brightness.
    if sleep < 1.0:
        spared = {s.slot for s in sessions if s.alert}
        if focused is not None:
            spared.add(focused)
        dim(board, sleep, frozenset(spared))

    frozen = frozenset(claimed)
    for overlay in overlays:
        overlay.apply(board, now, frozen)
    return board


