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
    """

    color: str | int | None = None
    rgb_anim: int = config.ANIM_NONE
    ring: int = 0
    brightness: float = 0.0

    def __post_init__(self) -> None:
        ring = max(0, min(127, int(self.ring)))
        brightness = max(0.0, min(1.0, float(self.brightness)))
        if ring != self.ring:
            object.__setattr__(self, "ring", ring)
        if brightness != self.brightness:
            object.__setattr__(self, "brightness", brightness)


def _sweep(now: float, rate: float) -> int:
    """A single travelling dot around the 0-127 ring."""
    return int((now * rate) % 1.0 * 127)


def _breathe(now: float, period: float, low: float, high: float) -> float:
    phase = (math.sin(now * 2 * math.pi / period) + 1) / 2
    return low + (high - low) * phase


def _lerp(low: float, high: float, t: float) -> float:
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
    return _lerp(config.ATTENTION_FLOOR, top, attention_debt(session, now))


def _arc_ring(session: Session) -> int:
    """Ring position from the tool-call counter, so the arc rotates one segment
    per completed call rather than sweeping on a timer."""
    fraction = (session.arc % config.ARC_SEGMENTS) / config.ARC_SEGMENTS
    return int(fraction * 127)


def _working_ring(session: Session) -> int:
    """A fuel gauge: the ring fills as the agent's context window fills.

    Activity is already carried by brightness -- every tool call kicks it back
    to full -- so the ring is free to show the one thing about a long-running
    agent you otherwise cannot see until it compacts on you.

    Falls back to the rotating tool-call arc when there is no reading yet (a
    fresh session, or a transcript we couldn't read), so a session never sits
    there looking empty when it isn't.
    """
    fraction = session.context_fraction if config.CONTEXT_RING else None
    if fraction is None:
        return _arc_ring(session)
    return max(config.CONTEXT_RING_FLOOR, int(127 * fraction))


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
    level = _lerp(config.ACTIVE_FLOOR, config.ACTIVE_BRIGHTNESS, kick)
    stalled = since_tool - config.STALL_SECONDS
    if stalled > 0:
        decay = min(1.0, stalled / config.STALL_FADE_SECONDS)
        level = _lerp(level, config.IDLE_BRIGHTNESS, decay)
    return level


def render(session: Session, now: float) -> Cell:
    state = session.state
    color = config.STATE_COLORS.get(state)
    anim = config.STATE_ANIM.get(state, config.ANIM_NONE)
    ring = 0
    brightness = config.ACTIVE_BRIGHTNESS

    if state == "ended":
        return Cell(None, config.ANIM_NONE, 0, 0.0)

    if state == "idle":
        # A dim single pip: the encoder is claimed, nothing is happening.
        ring = 2
        brightness = config.IDLE_BRIGHTNESS

    elif state == "working":
        ring = _working_ring(session)
        brightness = _working_brightness(session, now)

    elif state in SWEEP_RATE:
        ring = _sweep(now, SWEEP_RATE[state])
        brightness = config.ACTIVE_BRIGHTNESS

    elif state == "permission":
        # The only thing on the board allowed to move fast (board.arbitrate
        # enforces that), because it is the only thing that means a human is
        # blocking progress right now.
        ring = 127
        brightness = _insistence(session, now)

    elif state == "plan":
        # Same shape as `permission` -- it is the same kind of block -- but its
        # own hue and a slower flash, because the answer is "read this and
        # decide", not "unblock me".
        ring = 127
        brightness = _insistence(session, now)

    elif state == "waiting":
        ring = 127
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
        ring = max(2, int(127 * fade))
        brightness = _lerp(config.IDLE_BRIGHTNESS, config.ACTIVE_BRIGHTNESS, fade)
        debt = attention_debt(session, now)
        brightness = max(
            brightness, _lerp(config.IDLE_BRIGHTNESS, config.DONE_DEBT_CEILING, debt)
        )

    if session.unsupervised:
        # Reserved for nothing else, on every state: you should never have to
        # wonder which agent is running with permissions turned off.
        color = config.UNSUPERVISED_COLOR

    return Cell(color, anim, ring, brightness)
