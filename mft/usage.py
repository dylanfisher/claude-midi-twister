"""How much of the current usage window is spent, and when to shout about it.

The context ring (:mod:`mft.context`) answers "how full is *this* agent", which
is a per-session question and has a per-session display. This is the other
limit, the one that is not about any session at all: the five-hour usage window,
which is spent by every session at once and, when it runs out, stops all of them
together. There is nowhere on the board for a number that belongs to no encoder
-- so it doesn't get an encoder, it gets the whole bank for a couple of seconds,
on the milestones where interrupting you for it is worth it.

**Where the number comes from.** No hook payload carries it and there is no API
to ask from in here. But Claude Code fetches its own ``/usage`` answer and
caches it in ``~/.claude.json`` under ``cachedUsageUtilization``, which is
readable, cheap, and already on this machine. Two shapes live in there and both
are used: ``utilization.limits`` is the newer list, where the entry with
``kind == "session"`` is the five-hour window, and ``utilization.five_hour`` is
the older field it grew out of. First one that parses wins.

That cache is *Claude Code's*, refreshed on its clock and not ours, so a reading
can be an hour old and there is nothing to be done about it. This is why the
number is announced at milestones rather than shown continuously: a number that
lags is a bad dial and a perfectly good "you have crossed 75%, some time in the
last while". A stale reading delays an announcement; it never invents one. What
the announcement itself looks like -- the word, then the reading as rows filling
from the bottom of the bank -- is :class:`mft.overlays.UsageOverlay`'s business,
and it is deliberately four rows coarse for the same reason.

**Announcing is one-way and one-shot.** A watermark rises past each milestone
exactly once per window, so a percentage jittering across 50 does not spell
itself six times. The watermark resets when the window does -- a new
``resets_at``, or a percentage that fell -- and the reset is silent, because
"your limit refilled" is not news you look up for.

There is no hook that can fake one of these, so seeing the animation without
waiting five hours means pointing ``MFT_USAGE_FILE`` at a file you write
yourself -- ``{"cachedUsageUtilization": {"utilization": {"limits": [{"kind":
"session", "percent": 76, "resets_at": "R"}]}}}`` -- and raising the percentage
past a milestone while the daemon runs.

**And then there is asking.** Milestones are the board volunteering the number,
which means the number is only ever available on its own schedule -- miss the
flash and the next word about it is half a window away. So the reading is also
askable: a turn of the bottom-right encoder (:data:`config.USAGE_PEEK_ENCODER`)
shows the same bar with whatever the file says right now -- without the word in
front of it, and standing still for :data:`config.USAGE_PEEK_SECONDS` rather
than flashing, because an answer does not have to catch you. That path goes
through :meth:`UsageWatcher.request`, :meth:`~UsageWatcher.take_request` and
:meth:`~UsageWatcher.current`, and it is carefully sterile: it reads the file and
paints, and touches neither the watermark nor ``resets_at``, so looking can never
consume, arm or suppress an announcement.

The first reading of a daemon's life is silent too, for the same reason
invariant 6 exists: a daemon started at 60% has not *crossed* anything, it has
merely arrived, and a board that shouts a milestone every time you restart it is
a board you stop reading. That reading is adopted as the watermark instead.
"""

from __future__ import annotations

import json
import logging
import os
from typing import NamedTuple, Optional, Sequence

from . import config

log = logging.getLogger("mft.usage")


class Reading(NamedTuple):
    """One look at the cached usage block."""

    #: 0-100. Floats happen -- the cache carries fractions on some limits.
    percent: float
    #: When the window rolls over, as the ISO string the cache wrote. Compared,
    #: never parsed: all this needs to know is whether it is a *different*
    #: window than the last reading, and string equality answers that without
    #: this module having to have an opinion about timezones.
    resets_at: str


#: path -> (mtime, reading). ``~/.claude.json`` is ~90KB and is rewritten
#: constantly by every running Claude, while the usage block inside it changes a
#: few times an hour. The stat is the cheap part and the correct invalidation;
#: the parse only happens when the file actually moved. ``None`` is cached too,
#: so a file with no usage block in it costs one stat per poll forever.
_cache: dict[str, tuple[float, Optional[Reading]]] = {}


def _from_limits(utilization: dict) -> Optional[Reading]:
    """The five-hour window out of the ``limits`` list, if it is in there."""
    limits = utilization.get("limits")
    if not isinstance(limits, list):
        return None
    for limit in limits:
        if not isinstance(limit, dict) or limit.get("kind") != "session":
            continue
        percent = limit.get("percent")
        if isinstance(percent, (int, float)):
            return Reading(float(percent), str(limit.get("resets_at") or ""))
    return None


def _from_five_hour(utilization: dict) -> Optional[Reading]:
    """The same number out of the older flat field."""
    block = utilization.get("five_hour")
    if not isinstance(block, dict):
        return None
    percent = block.get("utilization")
    if isinstance(percent, (int, float)):
        return Reading(float(percent), str(block.get("resets_at") or ""))
    return None


def read(path: str = "") -> Optional[Reading]:
    """The current session-window reading, or ``None`` -- never an exception.

    ``None`` covers every way this can fail to answer, and they are all normal:
    the file is missing, it is being rewritten as we read it, the account has no
    session limit, or this build of Claude Code caches something else entirely.
    A visualizer with no reading simply says nothing, which is the same thing it
    says at 12%.
    """
    path = path or config.USAGE_FILE
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        _cache.pop(path, None)
        return None
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    reading: Optional[Reading] = None
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8", errors="replace"))
        block = data.get("cachedUsageUtilization") if isinstance(data, dict) else None
        utilization = block.get("utilization") if isinstance(block, dict) else None
        if isinstance(utilization, dict):
            reading = _from_limits(utilization) or _from_five_hour(utilization)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # A half-written file is the common one, and it fixes itself on the next
        # poll. Not cached against this mtime, so the retry actually re-reads.
        log.debug("unreadable usage cache at %s: %s", path, exc)
        return None
    _cache[path] = (mtime, reading)
    return reading


def crossed(
    watermark: float,
    percent: float,
    milestones: Sequence[int] = config.USAGE_MILESTONES,
) -> Optional[int]:
    """The highest milestone in ``(watermark, percent]``, or ``None``.

    The *highest*, not each of them: a daemon that was asleep through half a
    window comes back to a jump from 20% to 95% and has to say one thing about
    it. Spelling 25, 50, 75 and 90 at you first would be four words about a
    number none of them is any more.
    """
    reached = [m for m in milestones if watermark < m <= percent]
    return max(reached) if reached else None


def banner_color(milestone: int) -> str | int | None:
    """What the word is spelled in: the first band this milestone reaches into.

    Colored by how much trouble the number is rather than by being a usage
    number at all -- the early ones are white like any other announcement, and
    only the ones that mean "this window is nearly over" wear a warning hue.
    """
    for threshold, color in config.USAGE_COLORS:
        if milestone >= threshold:
            return color
    return config.TEXT_COLOR


class UsageWatcher:
    """The polling clock and the watermark, over :func:`read`.

    Impure by construction -- it reads a file and it remembers -- which is why
    the two decisions worth pinning (:func:`crossed`, :func:`banner_color`) are
    functions above it rather than methods on it.
    """

    def __init__(self, path: str = "") -> None:
        self.path = path or config.USAGE_FILE
        #: The last percentage read, for `/status`. ``None`` until one is.
        self.percent: Optional[float] = None
        #: Which window that reading was in; a change is a rollover.
        self.resets_at = ""
        #: The highest point of this window already announced -- or adopted, on
        #: the first reading. Milestones are only ever spelled above it.
        self.watermark = 0.0
        self._checked_at = float("-inf")
        self._seen = False
        #: Until when a turn of the knob is ignored: while an answer is on the
        #: board, plus a grace either side. Set provisionally by the request
        #: itself -- a flick's detents all land before the next frame does, so
        #: something has to swallow them before anything has been painted -- and
        #: then extended by :meth:`showing` once the render loop knows how long
        #: the animation it pushed actually runs for.
        self._quiet_until = float("-inf")
        #: One request the render loop has not honoured yet. A bool rather than a
        #: count: a flick is one question however many detents it sent, and two
        #: readouts of the same number back to back is the thing the debounce
        #: exists to prevent, not something to queue up.
        self._pending = False

    def poll(self, now: float) -> Optional[int]:
        """The milestone to announce right now, or ``None``.

        Called from the render loop, so the common path is a clock comparison
        and the uncommon one is a stat.
        """
        if not config.USAGE_BANNER:
            return None
        if now - self._checked_at < config.USAGE_POLL_SECONDS:
            return None
        self._checked_at = now
        reading = read(self.path)
        if reading is None:
            return None
        return self.observe(reading)

    def observe(self, reading: Reading) -> Optional[int]:
        """Fold one reading in, and say what it means. The pollable part, with
        the clock and the file taken out of it, which is how it is tested."""
        rolled = (
            reading.resets_at != self.resets_at
            or reading.percent + config.USAGE_ROLLOVER_DROP < self.watermark
        )
        self.resets_at = reading.resets_at
        self.percent = reading.percent
        if not self._seen or rolled:
            # Arrived at, not crossed. Adopting the level silently is what keeps
            # a restart -- and a window rollover, which lands as a drop -- from
            # spelling a milestone nobody moved past.
            self._seen = True
            self.watermark = reading.percent
            return None
        milestone = crossed(self.watermark, reading.percent)
        # Never lowered by a reading that dipped: the watermark is what has been
        # *said*, and a percentage that wobbles a point either side of 50 must
        # spell it once, not once per wobble. A drop big enough to be a real
        # rollover is caught above, where it also gets its silence.
        self.watermark = max(self.watermark, reading.percent)
        return milestone

    def request(self, now: float) -> bool:
        """Ask for the reading to be shown; ``True`` if that ask counted.

        Called from the MIDI thread, honoured by the render loop, which is the
        only reason this is two methods instead of one: the file read behind an
        answer is a stat and, when the file moved, a 90KB parse, and the input
        pump is the one thread in here that must never be holding still. So the
        turn leaves a flag and goes back to listening.

        The debounce lives here rather than in the daemon because it is the only
        part of the gesture worth pinning: a detent is a CC, a flick is a dozen
        of them, and all twelve mean "what is the number". The first one is the
        question and the rest arrive while it is already being answered.

        What it is deaf for is the answer, not a fixed number of seconds -- see
        :meth:`showing`. The floor set here covers only the gap between the flick
        and the frame that answers it, which is where the other eleven detents
        are.
        """
        if not config.USAGE_PEEK:
            return False
        if now < self._quiet_until:
            return False
        self._quiet_until = now + config.USAGE_PEEK_GRACE_SECONDS
        self._pending = True
        return True

    def showing(self, until: float) -> None:
        """The answer is on the board until ``until``; stay deaf that long.

        Called by whoever pushed the overlay, because the length of an animation
        is the animation's business and not this module's -- a peek with a word
        in front of it runs three times as long as one without, and a debounce
        that had to know that would be a second copy of it.
        """
        self._quiet_until = max(
            self._quiet_until, until + config.USAGE_PEEK_GRACE_SECONDS
        )

    def take_request(self) -> bool:
        """Whether there is an ask to answer, consuming it if so.

        Deliberately nothing to do with the watermark: an asked-for reading is
        not a crossing, so it neither spends a milestone nor advances anything
        the milestones are measured against. You can look as often as the
        cooldown allows and the announcements stay exactly where they were.
        """
        pending, self._pending = self._pending, False
        return pending

    def current(self) -> Optional[float]:
        """The reading right now, or ``None`` -- read fresh, remembered nowhere.

        Mutates none of this watcher's state, `resets_at` least of all: that
        field is what :meth:`observe` compares to notice a window rolling over,
        and a look that quietly updated it would swallow the next rollover and
        with it the milestones on the far side. ``None`` means the file said
        nothing, and the answer to that is to say nothing back (invariant 6) --
        an empty bar is indistinguishable from a genuine 0%.
        """
        reading = read(self.path)
        return None if reading is None else reading.percent

    def payload(self) -> Optional[dict]:
        """What `/status` says about the window, or ``None`` if nothing was
        readable -- which is itself the answer to "why has it never said 75%"."""
        if self.percent is None:
            return None
        return {
            "percent": round(self.percent, 1),
            "resets_at": self.resets_at,
            "announced": int(self.watermark),
        }
