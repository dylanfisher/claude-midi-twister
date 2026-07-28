"""Session bookkeeping: which Claude Code session owns which encoder.

Two things live here, and they are the same thing seen from two sides:
:class:`Session`, one agent's record, and :class:`SessionTable`, the registry
that decides which encoder that record is on. Everything is pure state -- the
daemon does the I/O -- which is what makes nearly all of this project's
behaviour testable without hardware, a socket or a Claude.

What a session's state *means* is :mod:`mft.render`. How a hook payload turns
into one is :mod:`mft.events`. How a tab is recognised across a `/clear` is
:mod:`mft.identity`. What is left here is slot allocation and the repairs that
keep one tab on exactly one encoder, which is most of the length and all of the
subtlety.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from . import config
from .context import fraction as context_fraction
from .identity import (
    best_key,
    hostless_keys,
    key_name,
    key_rank,
    names_tab,
    terminal_keys,
)

log = logging.getLogger("mft.state")

#: Rank per state, precomputed from the one ordered vocabulary in
#: :data:`mft.config.STATE_PRIORITY`. A state nobody ranked sorts last rather
#: than raising, so an unrecognised state costs its encoder the fast animation
#: and nothing else.
_RANK = {state: index for index, state in enumerate(config.STATE_PRIORITY)}
_UNRANKED = len(config.STATE_PRIORITY)


def priority(state: str) -> int:
    return _RANK.get(state, _UNRANKED)


#: Namespaces in :attr:`Session.subagents_in_flight`, so the two signals that
#: feed it stay distinguishable after the fact -- which is what lets
#: :attr:`Session.subagents` prefer one over the other rather than adding them
#: together. Written down here rather than in :mod:`mft.events` because they are
#: a property of the dict, and the dict belongs to the session.
AGENT_KEY = "agent:"
TOOL_KEY = "tool:"


@dataclass(frozen=True)
class Subagent:
    """The two things a violet dot draws itself from: when its subagent was
    spawned, and when it was last seen doing something.

    Two fields rather than one because they answer different questions and move
    in opposite directions -- the ring grows out of `started_at` and the
    brightness sinks away from `last_tool_at`. They were one float for a while,
    and the collapse hid the ambiguity in plain sight: the tool-use path never
    updates its value, so what read as one dict of activity stamps was really
    spawn times for half its keys and activity stamps for the other half.

    Frozen, so a tool call replaces the record rather than editing it. That costs
    nothing -- assigning an existing key leaves it exactly where it was in the
    dict, and the dict's order is the pile's order -- and it buys the thing every
    other value on this board already has: you can hold one and know it still
    says what it said. A mutable version made ``dict(in_flight)`` a snapshot that
    silently wasn't one.
    """

    started_at: float
    last_tool_at: float


@dataclass
class Session:
    session_id: str
    slot: int
    cwd: str = ""
    state: str = "idle"
    #: Whatever the SessionStart hook could learn about the terminal it lives
    #: in: TERM_PROGRAM, tty, tmux pane, iTerm GUID, ...
    terminal: dict[str, Any] = field(default_factory=dict)
    #: Every identity token this session's terminal has ever answered to; see
    #: :func:`terminal_keys`. The *terminal* is what owns the slot, and it is
    #: described by a different subset of tokens depending on which hook is
    #: talking, so a session accumulates them rather than holding one.
    keys: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.monotonic)
    last_event_at: float = field(default_factory=time.monotonic)
    state_since: float = field(default_factory=time.monotonic)
    turn_started_at: Optional[float] = None
    ended_at: Optional[float] = None
    tool_calls: int = 0
    turn_count: int = 0
    last_tool: str = ""
    last_tool_at: Optional[float] = None
    last_message: str = ""
    #: Ring segment, advanced once per completed tool call. The fallback
    #: activity signal for when there is no context reading to show instead.
    arc: int = 0
    #: How badly this turn is going, in units of failed tool calls: up one on a
    #: failure, down :data:`config.FAILURE_COOL_STEP` on a success, and it is
    #: what slides `working`'s hue toward red. Turn-scoped -- a new prompt is a
    #: fresh start, and the heat of a turn you have already answered describes
    #: nothing you can still act on.
    failure_heat: float = 0.0
    #: The tool call the heat above was last raised by. A failed call can arrive
    #: twice -- as `PostToolUseFailure` and as a `PostToolUse` whose response
    #: says it errored -- and the second one must not be read as the agent
    #: recovering. Only ever compared, never trusted for anything else.
    failed_tool_use: str = ""
    #: Where this session's transcript lives, and what came out of it. Filled by
    #: the daemon, which owns the file read; see :mod:`mft.context`.
    transcript_path: str = ""
    model: str = ""
    context_tokens: int = 0
    context_limit: int = 0
    context_checked_at: float = 0.0
    #: The tab strip's copy of all this; see :mod:`mft.tab`. `tab_title` is
    #: Claude Code's own generated title, re-read from the transcript once a
    #: turn (`tab_title_turn` is the turn it was read on), and `tab_painted` is
    #: the last line actually written down the tty -- which is what makes the
    #: repaint a comparison instead of a write.
    tab_title: str = ""
    tab_title_turn: int = -1
    tab_title_at: float = 0.0
    tab_painted: str = ""
    #: Identifiers of the subagents currently in flight, each mapped to a
    #: :class:`Subagent` record of when it started and when it was last seen
    #: doing something. Two independent signals feed this --
    #: SubagentStart/Stop by ``agent_id``, and PreToolUse/PostToolUse on
    #: Task/Agent by tool use id -- because SubagentStart is a recent hook and
    #: a settings file that predates it silently reports no subagents at all.
    #: Keyed rather than counted because neither signal is guaranteed to pair
    #: up (a killed subagent never stops) and both may describe the same subagent.
    #: Reset at the top of every turn, which is the only real floor available.
    #:
    #: A dict rather than a set because the keys are only half of it: the values
    #: are what let the pile shimmer per dot instead of sitting at one level, and
    #: fill its ring instead of holding a stub. It is ordered by arrival and stays
    #: that way -- a dot must not hop knobs because its neighbour called a tool.
    subagents_in_flight: dict[str, Subagent] = field(default_factory=dict)
    #: From the hook payload; "bypassPermissions" means nobody is watching.
    permission_mode: str = ""
    #: Set when the session wants a human. Cleared by pressing the encoder.
    alert: bool = False
    #: When this session started owing you attention. Debt ramps from here and
    #: is forgiven the moment you focus the tab.
    attention_since: Optional[float] = None
    compacting_since: Optional[float] = None
    #: When this slot was last wiped by a `/clear`. Only ever read to keep the
    #: two halves of the SessionEnd/SessionStart pair from wiping it twice.
    cleared_at: Optional[float] = None

    def set_state(self, state: str) -> None:
        if state != self.state:
            log.info("%s: %s -> %s", self.short_id, self.state, state)
            self.state = state
            self.state_since = time.monotonic()

    def owe_attention(self, now: Optional[float] = None) -> None:
        if self.attention_since is None:
            self.attention_since = now if now is not None else time.monotonic()

    def attended(self) -> None:
        """You looked at it. Clear the alert and forgive the debt."""
        self.alert = False
        self.attention_since = None

    def tool_failed(self, tool_use: str = "") -> None:
        """A tool call came back an error: warm the working hue toward red.

        Saturating rather than counting on: what the board is being asked is
        "is this going badly", and the answer stops getting more useful after
        the third failure. An unbounded count would also take proportionally
        longer to cool down, so an agent that failed thirty times and then
        recovered would stay red for the rest of the turn.

        Counted once per call rather than once per report: a failure that
        arrives as both `PostToolUseFailure` and an errored `PostToolUse` is one
        failing edit, and reading it twice would make every failure on an
        install that sends both worth double.
        """
        if tool_use and tool_use == self.failed_tool_use:
            return
        self.failure_heat = min(config.FAILURE_HEAT_FULL, self.failure_heat + 1.0)
        self.failed_tool_use = tool_use

    def tool_succeeded(self, tool_use: str = "") -> None:
        """A tool call came back clean: cool back toward `working`'s own hue.

        A call already counted as a failure never cools -- see
        :attr:`failed_tool_use`. Anything with no id to compare is taken at face
        value, which is the same bet the rest of this file makes about payload
        fields that move around: the cost is a third of one failure's worth of
        heat, and the alternative is a board that never cools at all on an
        install whose payloads don't carry ids.
        """
        if tool_use and tool_use == self.failed_tool_use:
            return
        heat = self.failure_heat - config.FAILURE_COOL_STEP
        # Snapped rather than merely clamped: three thirds of a failure leave
        # 1.1e-16 behind, and "cooled all the way back" is a thing the status
        # payload prints and the tests ask about. Nothing visible turns on it --
        # it is a rounding error's worth of hue -- but a field that is never
        # quite zero is a field nobody can compare against.
        self.failure_heat = 0.0 if heat < config.FAILURE_COOL_STEP / 2 else heat

    @property
    def failure_fraction(self) -> float:
        """0.0 -> 1.0 across :data:`config.FAILURE_HEAT_FULL` failures."""
        if not config.FAILURE_HEAT or config.FAILURE_HEAT_FULL <= 0:
            return 0.0
        return min(1.0, max(0.0, self.failure_heat / config.FAILURE_HEAT_FULL))

    @property
    def context_fraction(self) -> Optional[float]:
        """How full this agent's context window is, or ``None`` if unknown."""
        return context_fraction(self.context_tokens, self.context_limit)

    @property
    def subagents(self) -> int:
        """How many violet dots this session is owed, from the two signals.

        The same subagent is visible down both paths and nothing in either
        payload says so, so the two are counted separately and the larger wins.
        Not added: that would double every subagent on a machine where both
        signals arrive. Not one-preferred-over-the-other either, because the
        two do not arrive in step -- SubagentStart lands after the PreToolUse
        that caused it, and switching signals mid-turn makes the pile visibly
        drop a dot and pick it back up. `max` is monotone across the handover,
        and degrades to whichever signal a given install actually sends.
        """
        agents = sum(1 for k in self.subagents_in_flight if k.startswith(AGENT_KEY))
        return max(agents, len(self.subagents_in_flight) - agents)

    @property
    def subagent_activity(self) -> list[Optional[float]]:
        """One entry per violet dot, in the order they arrived: when that
        subagent last called a tool, or ``None`` if nothing ever said.

        ``None`` is the common case and not a defect. Only the ``agent_id``
        signal is fine-grained enough to attribute a tool call to one subagent,
        so a dot owed by the tool-use path alone has no activity of its own and
        renders at the flat level the whole pile used to sit at. The padding is
        at the end because that path's dots are the ones :attr:`subagents` adds
        beyond the agent-keyed records.
        """
        stamps: list[Optional[float]] = [
            rec.last_tool_at
            for k, rec in self.subagents_in_flight.items()
            if k.startswith(AGENT_KEY)
        ]
        return (stamps + [None] * self.subagents)[: self.subagents]

    @property
    def subagent_started(self) -> list[Optional[float]]:
        """One entry per violet dot, in the order they arrived: when that
        subagent was spawned, or ``None`` if nothing said.

        ``None`` is rare here, unlike :attr:`subagent_activity`, and the
        difference is the whole reason this is a second property rather than the
        same list read twice. *Both* signals know a spawn time -- the tool-use
        path's PreToolUse is the spawn -- where only the ``agent_id`` path can
        attribute a later tool call to one subagent. So this takes the larger
        namespace rather than always the agent-keyed one, matching what
        :attr:`subagents` counted, and an install too old for SubagentStart gets
        a filling ring on every dot instead of a row of stubs.

        Where both signals are live the two lists can disagree about which dot is
        which -- dot *n*'s ring and dot *n*'s shimmer may be two different
        subagents for the handover of a turn. That slop is already in
        :attr:`subagent_activity`'s padding and it is the same size: both
        namespaces are ordered by arrival, so index *n* is the *n*-th spawn
        either way, and the pile is a count of who is out rather than a roster.
        """
        agents: list[Optional[float]] = []
        tools: list[Optional[float]] = []
        for key, rec in self.subagents_in_flight.items():
            target = agents if key.startswith(AGENT_KEY) else tools
            target.append(rec.started_at)
        stamps = agents if len(agents) >= len(tools) else tools
        return (stamps + [None] * self.subagents)[: self.subagents]

    @property
    def unsupervised(self) -> bool:
        return self.permission_mode == "bypassPermissions"

    @property
    def key(self) -> str:
        """The strongest token this session is known by, for logs and /status.

        Derived rather than stored: the set is the identity, and a single field
        would go stale the moment a better token arrived. Falls back to the
        session id, which owns nothing and survives nothing -- a session with no
        terminal identity keeps its encoder by being the record it is, and
        loses it to the next `/clear`.
        """
        return best_key(self.keys) or f"sid:{self.session_id}"

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def label(self) -> str:
        return f"{os.path.basename(self.cwd) or '~'}#{self.short_id}"


class SessionTable:
    """Thread-safe registry of terminal -> encoder slot -> current session."""

    def __init__(self, slot_count: int = config.SLOT_COUNT) -> None:
        self.slot_count = slot_count
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        #: slot -> session_id, including recently-ended sessions during the
        #: linger window so a resumed session lands back where it was.
        self._slots: dict[int, str] = {}
        #: Every identity token any session answers to -> its slot, so a new
        #: session id in a known tab is adopted by the slot that tab already
        #: owns. A cache of what the sessions themselves hold, rebuilt from them
        #: by :meth:`_compact`, never the truth on its own.
        self._keys: dict[str, int] = {}

    # -- slot allocation ----------------------------------------------------

    def _free_slot(self) -> Optional[int]:
        """The lowest encoder no live session is sitting on.

        An ended session renders as a dark encoder, so leaving one in the middle
        of the live block would put a hole in the board. Its slot is therefore
        fair game before an untouched slot further along: the arriving session
        lands top-left-most and the ended one is displaced, never the reverse.
        """
        live = {s.slot for s in self._sessions.values() if s.ended_at is None}
        for slot in range(self.slot_count):
            if slot not in live:
                return slot
        return None

    def _compact(self) -> None:
        """Squeeze the board back up to the top-left.

        Live sessions take slots 0..n-1 in the order they already had, so
        nobody's encoder jumps sideways, and ended-but-lingering sessions sink
        below them. Losing a session in the middle therefore closes the hole it
        left instead of leaving a dark encoder between two lit ones.
        """
        ordered = sorted(
            self._sessions.values(), key=lambda s: (s.ended_at is not None, s.slot)
        )
        self._slots = {}
        for slot, session in enumerate(ordered):
            if session.slot != slot:
                log.info(
                    "session %s moved encoder %d -> %d",
                    session.short_id,
                    session.slot + 1,
                    slot + 1,
                )
                session.slot = slot
            self._slots[slot] = session.session_id
        # Rebuilt from the sessions, in board order, first writer winning: a
        # token two records somehow both claim resolves to the live one nearer
        # the top-left rather than to whichever happened to be indexed last.
        # `reconcile` is what notices that they clash at all.
        self._keys = {}
        for session in ordered:
            for key in session.keys:
                self._keys.setdefault(key, session.slot)

    def compact(self) -> None:
        with self._lock:
            self._compact()

    # -- terminal identity --------------------------------------------------

    def _bind(self, session: Session, keys: Sequence[str]) -> None:
        """Record every token a payload just said this session answers to.

        Tokens accumulate, with one exception: a token whose *field* the session
        already holds under a different value is a contradiction, not an
        addition. One tab cannot be two ttys, so the arriving value is the
        session's and the old one is dropped -- which is what lets a record that
        was handed the wrong tab's identity (see :meth:`_cleared_ghost`) correct
        itself the moment the tab it really lives in says so.
        """
        fresh = [key for key in keys if key]
        if not fresh:
            return
        names = {key_name(key) for key in fresh}
        contradicted = {
            key for key in session.keys if key_name(key) in names and key not in fresh
        }
        if any(key_name(key) != "cwd" for key in fresh):
            # A directory is what a payload with no identity in it falls back to,
            # and every tab in that directory shares it. The moment something
            # names the tab properly, that guess stops being this session's.
            contradicted |= {key for key in session.keys if key_name(key) == "cwd"}
        if contradicted:
            log.info(
                "session %s is not %s after all; it is %s",
                session.short_id,
                ", ".join(sorted(contradicted)),
                ", ".join(sorted(fresh)),
            )
            for key in contradicted:
                if self._keys.get(key) == session.slot:
                    del self._keys[key]
            session.keys -= contradicted
        session.keys.update(fresh)
        for key in fresh:
            self._keys[key] = session.slot

    def _owner(self, keys: Sequence[str]) -> Optional[Session]:
        """The session already holding any of these tokens, strongest first.

        Strongest first because the weak end of :data:`TERMINAL_KEYS` is
        reusable; see :func:`key_rank`. A stale token -- one indexed against a
        slot nothing lives on any more -- is dropped as it is found rather than
        answered with.
        """
        for key in sorted(keys, key=key_rank):
            slot = self._keys.get(key)
            if slot is None:
                continue
            session = self._sessions.get(self._slots.get(slot, ""))
            if session is not None:
                return session
            del self._keys[key]
        return None

    def _absorb(self, live: Session, stale: Session) -> Session:
        """One terminal, two records: keep the live one, on the older encoder.

        The repair for an identity that arrived late. Whatever the daemon learned
        about the tab while it could not name it belongs to the session that is
        actually running there, so the live record takes the established slot and
        inherits anything it is missing; the other is released. The lower slot
        wins because that is the encoder the tab has been using -- healing this
        must not also move the knob.
        """
        slot = min(live.slot, stale.slot)
        log.warning(
            "encoders %d and %d are the same terminal (%s); merging onto %d",
            live.slot + 1,
            stale.slot + 1,
            best_key(live.keys | stale.keys) or "?",
            slot + 1,
        )
        keys = set(live.keys) | set(stale.keys)
        if not live.terminal:
            live.terminal = dict(stale.terminal)
        if not live.transcript_path:
            live.transcript_path = stale.transcript_path
        live.created_at = min(live.created_at, stale.created_at)
        live.turn_count = max(live.turn_count, stale.turn_count)
        self._release(stale)
        live.slot = slot
        live.keys = keys
        self._compact()
        return live

    def _cleared_ghost(self, cwd: str, now: float) -> Optional[Session]:
        """The record a `/clear` just emptied here, if it is about to be orphaned.

        `/clear` retires a session id and hands out a new one in the same tab.
        The replacement announces its terminal on SessionStart -- but that hook
        runs a process to read the environment and is `async`, so a plain `curl`
        event for the new id can beat it to the daemon by a wide margin. Keyed on
        nothing, that event lights a *second* encoder: the tab's real knob sits
        on the wiped record (still focusable, which is why pressing it works)
        while the new one carries the session (and cannot be pressed, because it
        knows no terminal). Both then survive an hour of TTL.

        So a terminal-less event for an unknown session id, in the directory of a
        record that was wiped by `/clear` moments ago and has not said anything
        since, is taken for that clear's other half. The window is short and the
        conditions are narrow, because the cost of being wrong is two tabs in one
        directory sharing an encoder until the real identity arrives and
        :meth:`_bind` sorts them out.
        """
        if not cwd:
            return None
        candidates = [
            s
            for s in self._sessions.values()
            if s.cwd == cwd
            and s.cleared_at is not None
            and now - s.cleared_at <= config.CLEAR_ADOPT_SECONDS
            # Silent since the wipe: anything that has spoken for itself since
            # is a session, not the empty half of a pair.
            and s.last_event_at <= s.cleared_at
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.cleared_at or 0.0)

    def _handed_off(
        self, cwd: str, now: float, exclude: Optional[Session] = None
    ) -> Optional[Session]:
        """The tab whose conversation just moved into a process with no terminal.

        Claude Code no longer necessarily runs your session in the process you
        started: it pre-warms spares under a shared background daemon and hands
        the conversation to one, under a *new* session id, in a process with no
        tty. Nothing announces the move. The tab's own encoder therefore freezes
        on the last state it heard -- dim green, an hour of it -- while the live
        session lights a second knob that cannot be pressed, because the only
        token it has is the pid of its host.

        Ancestry cannot repair this: the daemon that owns the spare was spawned by
        whichever tab happened to start it first, so walking up from the host
        lands on someone else's terminal. The transcripts carry no lineage
        either. What is left is the directory and the timing, so those are what
        this matches on -- the same evidence, and the same narrowness, as
        :meth:`_cleared_ghost`:

        * the same working directory,
        * a tab that named itself properly (a token stronger than a bare pid),
        * with no turn in flight, because a session mid-turn has not gone
          anywhere -- and a turn whose `Stop` never arrived counts as over once
          it has stalled, or one missed hook would block the repair for as long
          as the record lives,
        * and quiet for less than :data:`~mft.config.HANDOFF_ADOPT_SECONDS`.

        The most recently active such tab wins, since two tabs in one repository
        are told apart by nothing else here. Being wrong costs a background agent
        painting on the knob of an idle tab in its own repository, and a press
        that raises that tab -- which is the near-miss, not a lie. Being right is
        the difference between the board tracking your session and pointing at
        where it used to be.
        """
        if not cwd:
            return None
        candidates = [
            s
            for s in self._sessions.values()
            if s is not exclude
            and s.cwd == cwd
            and s.ended_at is None
            and (
                s.turn_started_at is None
                or now - s.last_event_at > config.STALL_SECONDS
            )
            and now - s.last_event_at <= config.HANDOFF_ADOPT_SECONDS
            and names_tab(s.keys)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.last_event_at)

    def _evict(self, slot: int) -> None:
        """Drop whatever currently holds ``slot``, keys included."""
        previous = self._slots.get(slot)
        if previous:
            stale = self._sessions.pop(previous, None)
            if stale is not None:
                for key in stale.keys:
                    if self._keys.get(key) == slot:
                        del self._keys[key]
        self._slots.pop(slot, None)

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(session_id)

    def _rekey(self, session: Session, session_id: str) -> None:
        """Point an existing slot at a new session id, keeping its history."""
        self._sessions.pop(session.session_id, None)
        session.session_id = session_id
        self._sessions[session_id] = session
        self._slots[session.slot] = session_id

    def ensure(
        self,
        session_id: str,
        cwd: str = "",
        terminal: Optional[dict[str, Any]] = None,
    ) -> Optional[Session]:
        """Get the session, creating and assigning a slot if it's new.

        ``terminal`` is what makes the slot durable: when its tokens name a tab
        we already have a slot for, that slot adopts this session id instead of a
        fresh encoder lighting up. Every event that can carry one does -- see
        :func:`terminal_keys` -- because an event that arrives without one has to
        be answered by guessing, and the guesses are what this class spends most
        of its length keeping honest.

        Returns ``None`` only when all slots are held by live sessions.
        """
        with self._lock:
            now = time.monotonic()
            keys = terminal_keys(terminal, cwd) if terminal else []
            session = self._sessions.get(session_id)

            if session is not None:
                if cwd:
                    session.cwd = cwd
                if keys:
                    # This id already has a record, and the tokens that just
                    # arrived may belong to a *different* record -- which means
                    # one tab is on the board twice, and now we can prove it.
                    owner = self._owner(keys)
                    hostless = hostless_keys(keys) and not names_tab(session.keys)
                    if owner is None and hostless:
                        # This record was created by an event that could not name
                        # anything at all -- `notify.sh` beating the hook that
                        # reads the environment, which it routinely does -- and
                        # the identity now arriving says it is running in a bare
                        # process. If a tab here just went quiet, the two are one
                        # session and this is a second encoder for it.
                        owner = self._handed_off(session.cwd, now, exclude=session)
                    if owner is not None and owner is not session:
                        session = self._absorb(session, owner)
                    self._bind(session, keys)
                session.last_event_at = now
                return session

            if keys:
                owner = self._owner(keys)
                if owner is not None:
                    # Same tab, new session id: /clear, resume, compact or fork.
                    # Keep the slot and everything on it.
                    self._rekey(owner, session_id)
                    owner.cwd = cwd or owner.cwd
                    owner.last_event_at = now
                    self._bind(owner, keys)
                    log.info(
                        "session %s adopted encoder %d (%s)",
                        owner.short_id,
                        owner.slot + 1,
                        owner.key,
                    )
                    return owner

            if hostless_keys(keys):
                # A new session id whose only name is the process it runs in, in
                # a directory whose tab just went quiet: the same conversation,
                # moved. Keep the tab's encoder.
                tab = self._handed_off(cwd, now)
                if tab is not None:
                    self._rekey(tab, session_id)
                    tab.cwd = cwd or tab.cwd
                    tab.last_event_at = now
                    self._bind(tab, keys)
                    log.info(
                        "session %s adopted encoder %d (%s): handed off into a "
                        "process with no terminal of its own",
                        tab.short_id,
                        tab.slot + 1,
                        tab.key,
                    )
                    return tab

            # Only for an event with nothing to go on. One that named a tab and
            # matched no slot is a new tab, whatever else is going on in this
            # directory -- guessing is strictly for the events that force it.
            ghost = self._cleared_ghost(cwd, now) if not keys else None
            if ghost is not None:
                self._rekey(ghost, session_id)
                ghost.cwd = cwd or ghost.cwd
                ghost.last_event_at = now
                log.info(
                    "session %s adopted encoder %d as the other half of a /clear "
                    "in %s, with no terminal of its own yet",
                    ghost.short_id,
                    ghost.slot + 1,
                    cwd,
                )
                return ghost

            slot = self._free_slot()
            if slot is None:
                log.warning("no free encoder for session %s", session_id[:8])
                return None
            self._evict(slot)

            session = Session(session_id=session_id, slot=slot, cwd=cwd)
            self._sessions[session_id] = session
            self._slots[slot] = session_id
            self._bind(session, keys)
            log.info(
                "session %s -> encoder %d (%s, %s)",
                session.short_id,
                slot + 1,
                cwd,
                session.key,
            )
            return session

    def find_parent(self, session_id: str, cwd: str = "") -> Optional[Session]:
        """The session an event about a *subagent* belongs to, or None.

        Never creates anything, which is the whole point: a subagent owns no
        encoder, so an event of its own that we can't attribute must vanish
        rather than light one up. Exact id first, since the payload may well
        carry the parent's. Failing that the working directory, which for a
        subagent is its parent's -- ambiguous only between two tabs in the same
        directory, where the cost is a violet dot on the wrong bank rather than
        a phantom session.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                return session
            if not cwd:
                return None
            candidates = [
                s for s in self._sessions.values() if s.cwd == cwd and s.state != "ended"
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda s: s.last_event_at)

    def by_slot(self, slot: int) -> Optional[Session]:
        with self._lock:
            sid = self._slots.get(slot)
            return self._sessions.get(sid) if sid else None

    def all(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def _release(self, session: Session) -> None:
        """Drop one session. Leaves the board un-compacted; the caller squeezes
        it back up once it has finished removing things."""
        self._sessions.pop(session.session_id, None)
        if self._slots.get(session.slot) == session.session_id:
            del self._slots[session.slot]
        for key in session.keys:
            if self._keys.get(key) == session.slot:
                del self._keys[key]
        log.info("released encoder %d (%s)", session.slot + 1, session.short_id)

    def release(self, session: Session) -> None:
        with self._lock:
            self._release(session)
            self._compact()

    def release_all(self, sessions: Iterable[Session]) -> list[Session]:
        """Drop a batch of sessions and compact once, at the end.

        The batch form exists for the same reason :meth:`reap` compacts after
        its sweep rather than inside it: every release renumbers the slots below
        it, so releasing three sessions one at a time moves the survivors three
        times and logs every move.

        Returns only the records that were actually still in the table, so a
        caller working from a list it gathered a moment ago cannot double-drop.
        """
        with self._lock:
            dropped = [
                s for s in sessions if self._sessions.get(s.session_id) is s
            ]
            for session in dropped:
                self._release(session)
            if dropped:
                self._compact()
        return dropped

    def reap(self) -> list[Session]:
        """Drop sessions that ended a while ago, or that went silent (a
        crashed terminal never fires SessionEnd). Returns the freed slots."""
        now = time.monotonic()
        freed: list[Session] = []
        with self._lock:
            for session in list(self._sessions.values()):
                stale = now - session.last_event_at > config.SESSION_TTL_SECONDS
                lingered = (
                    session.ended_at is not None
                    and now - session.ended_at > config.SLOT_LINGER_SECONDS
                )
                if stale or lingered:
                    self._release(session)
                    freed.append(session)
            # Once, after the whole sweep: compacting per-session would renumber
            # the survivors again for every reap in the same pass, and each pass
            # logs every move it makes.
            if freed:
                self._compact()
        return freed

    # -- the no-orphans sweep -----------------------------------------------

    def reconcile(self, now: Optional[float] = None) -> list[Session]:
        """Repair the board's one structural invariant: **one encoder per tab.**

        :meth:`ensure` already keeps that true for every identity that arrives
        through it, which is nearly all of them. This is the fallback for the
        rest, and it exists because the failure it catches is both invisible and
        long-lived: an orphaned record renders as a plausible encoder, answers a
        press by focusing the right terminal, and sits there for the full
        `SESSION_TTL_SECONDS` while the session it used to be lights a second
        knob that cannot be pressed. Nothing about that looks like a bug from
        across the room, so it gets checked from outside rather than trusted.

        Two repairs, in this order:

        1.  Any token held by two records means one tab on two encoders; the
            live one keeps the older encoder (:meth:`_absorb`). This is also
            where identities written straight onto a session get indexed --
            :mod:`mft.discover` sets ``terminal`` on a record it did not create,
            and the press path upgrades one out of the process table.
        2.  A record wiped by a `/clear` that has been silent ever since, in a
            directory where a *newer* record with no terminal identity of its own
            is running, is the ghost of that clear rather than a session. It is
            released. The identity test is what keeps this off a genuine second
            tab in the same directory: a real session announces its terminal
            within a turn, and one that has is never taken for a ghost.

        Returns the records it dropped. Cheap enough to call on every reap:
        sixteen slots, a handful of tokens each, no I/O.
        """
        now = time.monotonic() if now is None else now
        dropped: list[Session] = []
        with self._lock:
            # One merge per pass, because a merge renumbers every slot under it.
            # Bounded by the number of sessions: each pass removes one record.
            for _ in range(len(self._sessions) + 1):
                if not self._merge_one_duplicate(dropped):
                    break
            for ghost in self._ghosts(now):
                log.warning(
                    "encoder %d is the ghost of a /clear in %s (%s); releasing it",
                    ghost.slot + 1,
                    ghost.cwd or "?",
                    ghost.short_id,
                )
                self._release(ghost)
                dropped.append(ghost)
            if dropped:
                self._compact()
        return dropped

    def _merge_one_duplicate(self, dropped: list[Session]) -> bool:
        """Find the first pair of records describing one terminal and merge it."""
        owners: dict[str, Session] = {}
        for session in sorted(self._sessions.values(), key=lambda s: s.slot):
            # What the record says it is, plus anything written onto its
            # `terminal` from outside this class and never indexed. No `cwd`
            # fallback: two tabs in one directory are two tabs.
            keys = set(session.keys) | set(terminal_keys(session.terminal))
            clash = next((owners[key] for key in keys if key in owners), None)
            if clash is None:
                self._bind(session, sorted(keys))
                for key in keys:
                    owners.setdefault(key, session)
                continue
            # The one that spoke most recently is the session; the other is
            # whatever it used to be called.
            live, stale = (
                (session, clash)
                if session.last_event_at >= clash.last_event_at
                else (clash, session)
            )
            live.keys |= keys
            self._absorb(live, stale)
            dropped.append(stale)
            return True
        return False

    def _ghosts(self, now: float) -> list[Session]:
        """Records left behind by a `/clear` whose replacement never found them.

        See :meth:`reconcile`. The window is the same one :meth:`_cleared_ghost`
        uses to adopt: inside it the replacement may still be about to arrive and
        say so itself, and there is nothing to repair until it has had the chance.
        """
        keyless = [
            s
            for s in self._sessions.values()
            if not s.keys and s.ended_at is None and not s.terminal
        ]
        if not keyless:
            return []
        return [
            s
            for s in self._sessions.values()
            if s.cleared_at is not None
            and s.last_event_at <= s.cleared_at
            and now - s.cleared_at > config.CLEAR_ADOPT_SECONDS
            and any(
                other is not s and other.cwd == s.cwd and other.created_at >= s.cleared_at
                for other in keyless
            )
        ]
