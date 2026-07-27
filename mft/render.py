"""Turn one Session into one encoder's worth of light: hue, RGB animation, ring
position, brightness. Pure function of (session, clock), so the render loop is
trivially testable and the whole visual language lives in one place.

:mod:`mft.board` composes these into the 64-cell board and resolves the things
that are only decidable across the whole board -- who gets the fast animation,
where subagents stack up, what a transient animation covers up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import config
from .state import Session

#: The shortest ring that still reads as "this encoder is claimed and nothing is
#: happening" rather than as an unlit one. The floor under every resting state.
PIP = 2

#: Continuous sweep rates, in revolutions per second. Only for states with no
#: discrete event to advance on; `working` uses the tool-call arc instead.
SWEEP_RATE = {
    "thinking": 0.35,
    "streaming": 0.5,
}


@dataclass(frozen=True)
class Cell:
    """One encoder. ``brightness`` drives both the RGB and the ring.

    Both levels are clamped on the way in, so a ``Cell`` is by construction a
    state the hardware can actually be in. That is one clamp instead of one per
    producer: every animation here is some eased ramp of an elapsed time, and an
    overlay asked for a frame slightly outside its own window computes a
    negative or over-unity level without doing anything else wrong. The wire
    clamps too (:meth:`mft.twister.Twister.cc`), but by then the cell has
    already been compared against, blended into and handed over from -- and a
    ring of -158 is not a thing to reason about.

    ``ring_level`` is the exception, and it is opt-in: ``None`` means "whatever
    the cell is lit at", which is what every gesture, overlay and blocking state
    wants -- one encoder, one brightness. A state sets it only when the ring is
    saying something the hue is not, which on this board is the context gauge and
    nothing else. It is free to be a separate number because it is a separate
    channel (6, against the RGB's 3); it is *worth* being one only where the two
    have genuinely diverged.
    """

    color: str | int | None = None
    rgb_anim: int = config.ANIM_NONE
    ring: int = 0
    brightness: float = 0.0
    ring_level: float | None = None

    def __post_init__(self) -> None:
        ring = max(0, min(127, int(self.ring)))
        brightness = max(0.0, min(1.0, float(self.brightness)))
        if ring != self.ring:
            object.__setattr__(self, "ring", ring)
        if brightness != self.brightness:
            object.__setattr__(self, "brightness", brightness)
        if self.ring_level is not None:
            level = max(0.0, min(1.0, float(self.ring_level)))
            if level != self.ring_level:
                object.__setattr__(self, "ring_level", level)

    @property
    def ring_light(self) -> float:
        """What the ring is actually lit at."""
        return self.brightness if self.ring_level is None else self.ring_level


def _sweep(now: float, rate: float) -> int:
    """A single travelling dot around the 0-127 ring."""
    return int((now * rate) % 1.0 * 127)


def _breathe(now: float, period: float, low: float, high: float) -> float:
    phase = (math.sin(now * 2 * math.pi / period) + 1) / 2
    return low + (high - low) * phase


def lerp(low: float, high: float, t: float) -> float:
    """Blend, clamped. Public because :mod:`mft.board` shimmers a subagent dot
    on exactly this shape; see the easing note there."""
    return low + (high - low) * max(0.0, min(1.0, t))


def attention_debt(session: Session, now: float) -> float:
    """0.0 -> 1.0 as an unattended session gets more insistent.

    This is the one quantity on the board that describes *you* rather than the
    agent, and it is forgiven the instant you focus the tab.
    """
    if session.attention_since is None:
        return 0.0
    age = max(0.0, now - session.attention_since)
    return min(1.0, age / config.ATTENTION_RAMP_SECONDS)


def _insistence(session: Session, now: float, ceiling: float | None = None) -> float:
    top = config.ATTENTION_CEILING if ceiling is None else ceiling
    return lerp(config.ATTENTION_FLOOR, top, attention_debt(session, now))


def _debt_anim(base: int, debt: float) -> int:
    """Escalate an animation rate by neglect, without leaving its own band.

    The counterpart to :func:`_insistence` for the states that animate: they are
    exactly the states whose brightness the wire discards (see
    :attr:`mft.config.ATTENTION_ANIM_STEPS`), so debt has to be spent on rate or
    it is not spent at all.

    Stays inside the band it started in, so a gate stays a gate and a breathe
    stays a breathe -- the difference between a strobe and a breathe is carrying
    meaning, and escalation must not spend it.

    Staying in the band is *not* what keeps a stale plan from impersonating a
    permission gate: the gate band has no room between the two base rates, so a
    fully-neglected plan does strobe faster than a fresh gate. What separates them
    is :func:`mft.board.arbitrate_motion`, which ranks by state before debt --
    with both on the board the plan is forced to ``SLOW_ANIM``, so the escalated
    rate only ever appears when the plan is the loudest thing there and there is
    no gate to confuse it with. At equal debt the ordering always holds.
    """
    steps = int(debt * config.ATTENTION_ANIM_STEPS)
    if not base or steps <= 0:
        return base
    for band in config.ANIM_BANDS:
        if base in band:
            top = len(band) - 1
            return band[min(band.index(base) + steps, top)]
    return base


def _arc_ring(session: Session) -> int:
    """Ring position from the tool-call counter, so the arc rotates one segment
    per completed call rather than sweeping on a timer."""
    fraction = (session.arc % config.ARC_SEGMENTS) / config.ARC_SEGMENTS
    return int(fraction * 127)


def stopwatch_fraction(elapsed: float, span: float) -> float:
    """How far round a stopwatch ring is, after `elapsed` seconds of a `span`.

    The one scale every filling-with-time ring on the board shares, so that half
    a ring means the same fifteen minutes on a session's encoder and on a violet
    subagent dot. The shape lives in :data:`config.STOPWATCH_CURVE`; this is only
    the interpolation between its points.

    Saturates rather than wrapping. A wrap would be ambiguous with a stopwatch
    that just started, which is the one reading that must never be wrong, and
    there is nothing you do differently at two hours than at one.
    """
    if span <= 0:
        return 0.0
    x = min(1.0, max(0.0, elapsed) / span)
    curve = config.STOPWATCH_CURVE
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            # The curve starts at (0, 0), so a zero-width leg can only be a
            # hand-edited table; hold its right end rather than dividing by it.
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


def _turn_ring(session: Session, now: float) -> int:
    """A stopwatch: the ring fills with how long this turn has been running.

    Front-loaded rather than linear, because turn lengths are: nearly every turn
    finishes inside a few minutes and the ones you care about run to half an
    hour, and no linear ring shows you both. See :data:`config.STOPWATCH_CURVE`
    for the shape and why a subagent's dot wears the same one.
    """
    started = session.turn_started_at
    if started is None:
        # Adopted mid-turn by `mft.discover`, or working without a prompt of its
        # own: we know when we started *believing* it was working, and that is
        # the same number for every purpose except the first few seconds.
        started = session.state_since
    fraction = stopwatch_fraction(now - started, config.TURN_RING_FULL_SECONDS)
    return max(config.CONTEXT_RING_FLOOR, int(127 * fraction))


def _gauge_ring(session: Session, fallback: int) -> int:
    """A fuel gauge: the ring fills as the agent's context window fills.

    ``fallback`` is what the ring says when there is no reading -- a fresh
    session, a session just cleared, or a transcript we couldn't read -- which is
    the resting pip. Deliberately not the tool-call arc: an arc is a spinner, and
    a spinner on a session that stopped working is frozen at whatever segment the
    last call left it on, which says nothing at all.
    """
    fraction = session.context_fraction if config.CONTEXT_RING else None
    if fraction is None:
        return fallback
    return max(config.CONTEXT_RING_FLOOR, int(127 * fraction))


def _resting_gauge(session: Session) -> int:
    """The gauge for a session that has stopped running, or ``PIP`` if we have no
    reading and there is nothing to draw."""
    if not config.CONTEXT_RING_IDLE:
        return PIP
    return _gauge_ring(session, PIP)


def _gauge_level(session: Session, now: float, base: float) -> float:
    """The gauge's own brightness: ``base`` when the session just stopped, fading
    to :data:`config.GAUGE_STALE_LEVEL` over the same window the `done` flash
    recedes across.

    A reading ages. Ten seconds after a turn ends it is what the session is; ten
    minutes after, it is what the session was when you last had a reason to care.
    The fade is that difference, and it is on the ring's own channel so that a
    neglected `done` encoder can ramp its hue back up to nag you without dragging
    a stale number up with it -- the reading did not get more urgent, only older.
    """
    age = max(0.0, now - session.state_since)
    return lerp(base, config.GAUGE_STALE_LEVEL, age / config.DONE_FADE_SECONDS)


def _hue(color: str | int | None) -> int:
    """A colour name or a raw wheel value, as a wheel value."""
    if isinstance(color, str):
        return config.COLORS[color]
    return config.COLOR_OFF if color is None else int(color)


def working_color(session: Session, base: str | int | None) -> str | int | None:
    """`working`'s hue, warmed toward red by how badly the turn is going.

    The one thing a tool-call board is in a position to know and has never said.
    Hue is what the encoder is *doing* and everything else about `working`
    already describes rate -- the shimmer is calls per second, the ring is how
    long the turn has run -- so none of them can tell an agent making progress
    apart from an agent retrying the same failing edit. They look identical, and
    the second one is the one you want to be interrupted about.

    Hue and nothing else, deliberately. It stays out of `arbitrate_motion` for
    the same reason the subagent shimmer does: a level (or here a colour) is not
    a rate, so this cannot compete for the one fast animation the board allows
    itself, which belongs to an encoder where a human is actually blocking. A
    failing agent is still working. It has not become an alert and must not be
    able to make itself one -- it does not pin the ring, does not owe you
    attention, and does not pull the bank onto itself.

    Interpolated on the wheel rather than switched at a threshold, so two
    failures and five look different from across the room. The curve is in
    :data:`config.FAILURE_HEAT_CURVE`: orange and red are six values apart on
    this hardware, and a linear ramp would spend the whole first failure inside
    the width of a rounding error.
    """
    fraction = session.failure_fraction
    if fraction <= 0:
        return base
    eased = 1.0 - (1.0 - fraction) ** config.FAILURE_HEAT_CURVE
    return round(lerp(_hue(base), _hue(config.FAILURE_HEAT_COLOR), eased))


def _working_brightness(session: Session, now: float) -> float:
    """Bright on each tool call, decaying between them, sagging away entirely
    once the session has stopped calling tools at all.

    Shimmer rate reads as tool-call frequency from the corner of your eye; a
    stalled session dims out, which is the failure mode you otherwise only find
    by going and looking at the terminal.
    """
    last = session.last_tool_at
    since_tool = now - (session.state_since if last is None else last)
    kick = max(0.0, 1.0 - since_tool / config.TOOL_KICK_SECONDS)
    level = lerp(config.ACTIVE_FLOOR, config.ACTIVE_BRIGHTNESS, kick)
    stalled = since_tool - config.STALL_SECONDS
    if stalled > 0:
        decay = min(1.0, stalled / config.STALL_FADE_SECONDS)
        level = lerp(level, config.IDLE_BRIGHTNESS, decay)
    return level


def render(session: Session, now: float) -> Cell:
    state = session.state
    color = config.STATE_COLORS.get(state)
    anim = config.STATE_ANIM.get(state, config.ANIM_NONE)
    ring = 0
    brightness = config.ACTIVE_BRIGHTNESS
    ring_level: float | None = None

    if state == "ended":
        return Cell(None, config.ANIM_NONE, 0, 0.0)

    if state == "idle":
        # A dim single pip -- the encoder is claimed, nothing is happening -- or
        # the gauge, if we have a reading. How full the window is outlives the
        # turn that filled it, and this is the state a session spends most of its
        # life in, so it is where the number is worth the most.
        ring = _resting_gauge(session)
        brightness = config.IDLE_BRIGHTNESS
        ring_level = _gauge_level(session, now, brightness)

    elif state == "working":
        # The stopwatch, not the gauge. See :data:`config.TURN_RING`: the window
        # barely moves during a turn and you can do nothing about it if it does,
        # where "how long has this been going" is the whole reason you looked.
        ring = _turn_ring(session, now) if config.TURN_RING else _arc_ring(session)
        brightness = _working_brightness(session, now)
        # ...and the hue says how well it is going. The shimmer above is
        # untouched: same rate, same decay, same stall sag -- only the colour
        # under it moves. See :func:`working_color`.
        color = working_color(session, color)

    elif state in SWEEP_RATE:
        ring = _sweep(now, SWEEP_RATE[state])
        brightness = config.ACTIVE_BRIGHTNESS

    elif state == "permission":
        # The only thing on the board allowed to move fast (board.arbitrate
        # enforces that), because it is the only thing that means a human is
        # blocking progress right now. Ignore it and it strobes faster: the
        # brightness below never reaches the RGB while there is an animation on
        # it, so rate is the only channel neglect has here.
        ring = 127
        anim = _debt_anim(anim, attention_debt(session, now))
        brightness = _insistence(session, now)

    elif state == "plan":
        # Same shape as `permission` -- it is the same kind of block -- but its
        # own hue and a slower flash, because the answer is "read this and
        # decide", not "unblock me". It escalates within the gate band only, so
        # it never arrives at the permission rate.
        ring = 127
        anim = _debt_anim(anim, attention_debt(session, now))
        brightness = _insistence(session, now)

    elif state == "waiting":
        ring = 127
        anim = _debt_anim(anim, attention_debt(session, now))
        brightness = _breathe(now, 2.4, 0.3, _insistence(session, now))

    elif state == "error":
        # Solid, not strobing: a rate limit is bad but it is not waiting on your
        # hand, and two identical blinking reds would be indistinguishable.
        ring = 127
        brightness = _insistence(session, now)

    elif state == "done":
        # Flash, then recede -- and then, if you never come look, slowly ramp
        # back up. Capped so it can never outshout a live block.
        age = max(0.0, now - session.state_since)
        fade = min(1.0, max(0.0, 1.0 - age / config.DONE_FADE_SECONDS))
        # The unwinding ring settles onto the gauge rather than onto the pip. It
        # starts at full and decays *through* whatever the gauge reads, so there
        # is no moment of handover to smooth: the fade simply stops mattering
        # once it drops below the level underneath it.
        ring = max(_resting_gauge(session), int(127 * fade))
        brightness = lerp(config.IDLE_BRIGHTNESS, config.ACTIVE_BRIGHTNESS, fade)
        # Taken before the debt ramp below, on purpose: the gauge follows the
        # flash down and then stays down, while the hue is free to come back up
        # and ask for you.
        ring_level = _gauge_level(session, now, brightness)
        debt = attention_debt(session, now)
        brightness = max(
            brightness, lerp(config.IDLE_BRIGHTNESS, config.DONE_DEBT_CEILING, debt)
        )

    if session.unsupervised:
        # Reserved for nothing else, on every state: you should never have to
        # wonder which agent is running with permissions turned off.
        color = config.UNSUPERVISED_COLOR

    return Cell(color, anim, ring, brightness, ring_level)
