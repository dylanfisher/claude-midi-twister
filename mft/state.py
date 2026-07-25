"""Session bookkeeping: which Claude Code session owns which encoder, and what
that encoder should look like right now.

Hook events are discrete, so "thinking" and "working" are *inferred* from the
gaps between events rather than reported directly. Everything here is pure
state; the daemon does the I/O.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from . import config
from .context import fraction as context_fraction

log = logging.getLogger("mft.state")

#: Rank per state, precomputed from the one ordered vocabulary in
#: :data:`mft.config.STATE_PRIORITY`. A state nobody ranked sorts last rather
#: than raising, so an unrecognised state costs its encoder the fast animation
#: and nothing else.
_RANK = {state: index for index, state in enumerate(config.STATE_PRIORITY)}
_UNRANKED = len(config.STATE_PRIORITY)


def priority(state: str) -> int:
    return _RANK.get(state, _UNRANKED)


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
    #: Where this session's transcript lives, and what came out of it. Filled by
    #: the daemon, which owns the file read; see :mod:`mft.context`.
    transcript_path: str = ""
    model: str = ""
    context_tokens: int = 0
    context_limit: int = 0
    context_checked_at: float = 0.0
    #: Recent tool names, oldest first, for the press-and-hold detail view.
    tool_history: deque[str] = field(
        default_factory=lambda: deque(maxlen=config.PEEK_HISTORY)
    )
    #: Identifiers of the subagents currently in flight. Two independent signals
    #: feed this -- SubagentStart/Stop by ``agent_id``, and PreToolUse/PostToolUse
    #: on Task/Agent by tool use id -- because SubagentStart is a recent hook and
    #: a settings file that predates it silently reports no subagents at all.
    #: A set rather than a counter because neither signal is guaranteed to pair
    #: up (a killed subagent never stops) and both may describe the same subagent.
    #: Reset at the top of every turn, which is the only real floor available.
    subagents_in_flight: set[str] = field(default_factory=set)
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
        agents = sum(1 for k in self.subagents_in_flight if k.startswith(_AGENT_KEY))
        return max(agents, len(self.subagents_in_flight) - agents)

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


# --- terminal identity ------------------------------------------------------

#: In order of how well each survives the things that end a session id.
#: A `/clear` hands out a brand new session id in the *same* tab, so keying
#: slots on the session id would teleport an agent to a different knob every
#: time you cleared. The terminal is the durable thing; the session id is an
#: attribute of the slot, not its identity.
TERMINAL_KEYS = (
    "TMUX_PANE",
    "ITERM_SESSION_ID",
    "WEZTERM_PANE",
    "KITTY_WINDOW_ID",
    "TERM_SESSION_ID",
    "tty",
    "pid",  # the claude process itself: survives /clear, not a restart
)


_KEY_RANK = {name: index for index, name in enumerate(TERMINAL_KEYS)}


def terminal_keys(terminal: dict[str, Any], cwd: str = "") -> list[str]:
    """Every identity token a terminal payload carries, strongest first.

    All of them rather than only the best one, because *which* fields a given
    payload has is not stable. The SessionStart hook reads the whole
    environment; ``notify.sh`` reports what it can learn without a process
    spawn; discovery recovers a third subset from the process table. Matching on
    one token per payload means two descriptions of the same tab that overlap
    only in their weaker fields look like two tabs -- and that is exactly how a
    session ends up lighting a second encoder while the first one keeps pointing
    at its terminal. Matching on the whole set, a single field in common is
    enough to recognise the tab.
    """
    keys = [f"{name}:{terminal[name]}" for name in TERMINAL_KEYS if terminal.get(name)]
    if not keys and cwd:
        keys = [f"cwd:{cwd}"]
    return keys


def merge_terminal(
    stored: dict[str, Any], arriving: dict[str, Any]
) -> dict[str, Any]:
    """Fold a new description of a tab into what we already knew about it.

    A union rather than a replacement, because the hooks do not all report the
    same fields: ``register_session.py`` sends the whole environment and
    ``notify.sh`` sends the little it can learn without spawning a process, so
    taking the latest payload whole would keep dropping the details the focus
    adapters need. The exception is an identifying field they *both* carry with
    different values -- one tab cannot be two ttys, so what we stored describes
    some other tab and the arriving payload is the entire truth about this one.
    """
    contradicted = any(
        name in stored and stored[name] != value
        for name, value in arriving.items()
        if name in _KEY_RANK
    )
    return dict(arriving) if contradicted else {**stored, **arriving}


def key_name(key: str) -> str:
    """``tty`` out of ``tty:/dev/ttys004``: which field a token came from."""
    return key.split(":", 1)[0]


def key_rank(key: str) -> tuple[int, str]:
    """Sort order for tokens: :data:`TERMINAL_KEYS` first, ties by value.

    Every comparison between tokens is this one -- which of two names a tab
    better -- and the weak end of the list is weak in a specific way: a pid names
    a tab exactly until that process exits and the number comes back around.
    """
    return (_KEY_RANK.get(key_name(key), len(TERMINAL_KEYS)), key)


def best_key(keys: Iterable[str]) -> Optional[str]:
    """The most durable token in a set, or None if there are none."""
    return min(keys, key=key_rank, default=None)


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


# --- hook event -> state ----------------------------------------------------

#: `Notification` carries a `notification_type` naming exactly what it wants
#: from you, and the three that matter want three different things back. One
#: blinking red for all of them trains you to ignore blinking red.
NOTIFICATION_STATES = {
    "permission_prompt": "permission",  # a gate is open: fast red
    "idle_prompt": "waiting",  # nothing is happening: slow amber
    "agent_needs_input": "waiting",
    "elicitation_dialog": "permission",
    "agent_completed": "done",  # green, decaying
}

#: Fallback for payloads without a `notification_type`, matched against the
#: human-readable message.
_MESSAGE_STATES = (
    ("permission", "permission"),
    ("approve", "permission"),
    ("needs your", "waiting"),
    ("waiting for your", "waiting"),
    ("idle", "waiting"),
    ("needs_input", "waiting"),
    ("completed", "done"),
)


def is_plan_approval(event: dict[str, Any]) -> bool:
    """Is this event Claude asking you to sign off on a plan?

    Claude Code has no PlanReady hook: finishing a plan asks permission to run
    the tool that leaves plan mode, so it arrives as an ordinary
    PermissionRequest (with a recognisable ``tool_name``) or as a Notification
    whose only tell is its prose. Both roads lead here.
    """
    tool = str(event.get("tool_name") or "")
    if tool in config.PLAN_TOOLS:
        return True
    message = str(event.get("message", "")).lower()
    return any(token in message for token in config.PLAN_MESSAGE_TOKENS)


def classify_notification(event: dict[str, Any]) -> Optional[str]:
    if is_plan_approval(event):
        return "plan"
    kind = str(event.get("notification_type") or "").strip()
    if kind:
        return NOTIFICATION_STATES.get(kind)
    message = str(event.get("message", "")).lower()
    for token, state in _MESSAGE_STATES:
        if token in message:
            return state
    # An unlabelled notification with no recognisable message still means
    # *something* wants you; treat it as the gentle one.
    return "waiting"


#: Namespaces in `Session.subagents_in_flight`, so the two signals stay
#: distinguishable after the fact -- which is what lets `Session.subagents`
#: prefer one over the other rather than adding them together.
_AGENT_KEY = "agent:"
_TOOL_KEY = "tool:"


def _tool_use_key(event: dict[str, Any]) -> Optional[str]:
    """The in-flight key for a Task/Agent tool call, or None if it isn't one.

    A subagent spawn is visible twice: as SubagentStart, and as an ordinary
    PreToolUse for the tool that spawned it. The second one has been installed
    since the beginning, so it is what makes the corner light up on a settings
    file written before SubagentStart existed.
    """
    if event.get("tool_name", "") not in config.SUBAGENT_TOOLS:
        return None
    # Payload key spelling has moved around; an id we can't read is worse than
    # no count, since a made-up one never gets discarded and the pile only grows.
    for field_name in ("tool_use_id", "toolUseID", "tool_use_ID"):
        value = event.get(field_name)
        if value:
            return f"{_TOOL_KEY}{value}"
    return None


def _agent_key(event: dict[str, Any]) -> Optional[str]:
    agent_id = event.get("agent_id")
    return f"{_AGENT_KEY}{agent_id}" if agent_id else None


#: Effects the daemon acts on, because they need the board or the wire rather
#: than the session record: transient animations and banners.
EFFECT_COMPACT_START = "compact:start"
EFFECT_COMPACT_END = "compact:end"
EFFECT_BANNER = "banner:"
EFFECT_SPAWN = "spawn"
EFFECT_CLEAR = "clear"


#: `SessionEnd` reasons that are not the end of anything. `clear` is reported
#: when you run `/clear`, and the tab it names is still sitting there with a new
#: session id already on its way in -- so releasing the encoder would mean every
#: `/clear` frees a slot and immediately re-claims one, which on an allocator
#: that hands out the lowest free index moves the agent to a different knob
#: mid-session. Exactly the thrash keying slots on the terminal exists to avoid.
SESSION_END_KEEPS_SLOT = frozenset({"clear"})


def clear_session(session: Session, now: float) -> list[str]:
    """`/clear`: same tab, same encoder, an agent that remembers nothing.

    Everything turn-scoped goes, because none of it describes the session that
    is about to exist. Everything slot-scoped stays, because the tab has not
    moved and the slot belongs to the tab.

    Called from *both* halves of the pair -- ``SessionEnd(clear)`` and
    ``SessionStart(clear)`` -- because their order is not guaranteed and either
    one can go missing. Running twice is therefore normal and has to be
    harmless: the reset is idempotent by construction, and the wipe is fired
    only by whichever half arrives first.
    """
    session.tool_calls = 0
    session.turn_started_at = None
    session.subagents_in_flight.clear()
    session.tool_history.clear()
    session.arc = 0
    session.compacting_since = None
    session.last_tool = ""
    session.last_tool_at = None
    session.last_message = ""
    # The gauge describes a transcript this session no longer has. Zeroed rather
    # than left standing, because a full context ring on an agent that has just
    # forgotten everything is the one reading that is definitely wrong; the next
    # poll of the new transcript fills it back in from the truth.
    session.context_tokens = 0
    # ...and the next event re-reads it immediately rather than waiting out a
    # poll interval that was started by the session which no longer exists.
    session.context_checked_at = 0.0
    # Not an ending, so nothing here may look like one: an `ended_at` would sink
    # the encoder below the live block and a `SLOT_LINGER_SECONDS` later hand it
    # to somebody else.
    session.ended_at = None
    session.attended()
    session.set_state("idle")

    if (
        session.cleared_at is not None
        and now - session.cleared_at < config.CLEAR_DEBOUNCE_SECONDS
    ):
        return []
    session.cleared_at = now
    return [EFFECT_CLEAR]


def apply_event(session: Session, event: dict[str, Any]) -> list[str]:
    """Fold one hook payload into the session's state.

    Returns any :mod:`~mft.board` effects the daemon should stage, which is how
    animations that live on the board rather than on one encoder (a compaction
    sweep, a spelled-out banner) get triggered without state.py knowing about
    rendering.
    """
    name = event.get("hook_event_name", "")
    now = time.monotonic()
    effects: list[str] = []
    session.last_event_at = now
    if event.get("cwd"):
        session.cwd = event["cwd"]
    if event.get("permission_mode"):
        session.permission_mode = event["permission_mode"]
    # Every payload carries these two, and they are what the context gauge is
    # read from. Recorded here; the file itself is read by the daemon, since
    # this module does no I/O.
    if event.get("transcript_path"):
        session.transcript_path = event["transcript_path"]
    if event.get("model"):
        session.model = str(event["model"])

    if name == "SessionStart":
        # Not a fresh encoder: a `/clear` or resume keeps the tab's slot and
        # everything on it, so only the turn-scoped fields reset.
        if str(event.get("source") or "") == "clear":
            # The second half of the `/clear` pair, and a different event from
            # an arrival: nothing claimed an encoder here, an agent that was
            # already on this one forgot everything. The wipe says that; the
            # spawn strike would say a new Claude appeared, which it didn't.
            effects += clear_session(session, now)
        else:
            session.set_state("idle")
            session.ended_at = None
            session.attended()
            session.turn_started_at = None
            # `idle` is a dim pip, so without this the arrival of a session --
            # the one moment you actually want to see -- is the least visible
            # thing the board ever does. A resume gets it too: it is also a
            # Claude starting in that tab, which is what the flash announces.
            effects.append(EFFECT_SPAWN)

    elif name == "UserPromptSubmit":
        session.turn_started_at = now
        session.turn_count += 1
        session.tool_calls = 0
        session.subagents_in_flight.clear()
        session.attended()
        session.set_state("thinking")

    elif name == "PreToolUse":
        session.tool_calls += 1
        session.last_tool = event.get("tool_name", "")
        session.last_tool_at = now
        key = _tool_use_key(event)
        if key:
            session.subagents_in_flight.add(key)
        session.set_state("working")

    elif name in ("PostToolUse", "PostToolUseFailure"):
        tool = event.get("tool_name", "")
        session.last_tool = tool
        session.last_tool_at = now
        key = _tool_use_key(event)
        if key:
            session.subagents_in_flight.discard(key)
        # The arc is the activity signal: one segment per completed call, so
        # spin rate is tool-call frequency and a ring that stops is a stall.
        session.arc = (session.arc + 1) % config.ARC_SEGMENTS
        session.tool_history.append(tool)
        session.set_state("working")

    elif name == "MessageDisplay":
        session.set_state("streaming")

    elif name == "Notification":
        session.last_message = event.get("message", "")
        state = classify_notification(event)
        if state:
            session.set_state(state)
            if state != "done":
                session.alert = True
            session.owe_attention(now)

    elif name == "PermissionRequest":
        # Display only. Nothing here answers the request: the visualizer never
        # decides a permission, it only shows you that one is open.
        session.alert = True
        session.owe_attention(now)
        session.set_state("plan" if is_plan_approval(event) else "permission")

    elif name == "SubagentStart":
        key = _agent_key(event)
        if key:
            session.subagents_in_flight.add(key)
        session.set_state("working")

    elif name == "SubagentStop":
        key = _agent_key(event)
        if key:
            session.subagents_in_flight.discard(key)
        else:
            # No id to match on, but something definitely finished, and a pile
            # that only ever grows is worse than one that shrinks arbitrarily.
            stale = next(
                (k for k in session.subagents_in_flight if k.startswith(_AGENT_KEY)),
                None,
            )
            if stale:
                session.subagents_in_flight.discard(stale)
        session.set_state("working")

    elif name == "PreCompact":
        session.compacting_since = now
        effects.append(EFFECT_COMPACT_START)

    elif name == "PostCompact":
        session.compacting_since = None
        effects.append(EFFECT_COMPACT_END)

    elif name == "StopFailure":
        reason = str(event.get("error_type") or event.get("message") or "failure")
        session.last_message = reason
        session.alert = True
        session.owe_attention(now)
        session.set_state("error")
        if "rate" in reason.lower():
            effects.append(EFFECT_BANNER + "RATE")

    elif name == "Stop":
        session.turn_started_at = None
        session.set_state("done")
        # Finishing is a soft debt, not an alert: the encoder recedes, then
        # slowly gets more insistent for as long as you don't come look.
        session.owe_attention(now)

    elif name == "SessionEnd":
        # The reason is the whole event: `clear` means the tab is still there
        # and keeps its encoder, `logout` / `prompt_input_exit` / `other` mean
        # the session is actually gone. Treated as advisory either way -- it is
        # reported not to fire on `/exit`, and the TTL reaper is what actually
        # keeps the board honest. This is the fast path when it does arrive.
        if str(event.get("reason") or "") in SESSION_END_KEEPS_SLOT:
            effects += clear_session(session, now)
            # The path recorded at the top of this function is the *ending*
            # session's transcript, and reading it back would refill the gauge
            # this wipe just emptied. The replacement announces its own.
            session.transcript_path = ""
        else:
            session.ended_at = now
            session.attended()
            session.set_state("ended")

    else:
        # Unknown/new event type: treat as a liveness ping only.
        log.debug("unhandled hook event %r", name)

    return effects
