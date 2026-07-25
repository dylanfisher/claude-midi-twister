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
from typing import Any, Optional

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
    #: Stable identity of the *terminal*, which is what actually owns the slot.
    key: str = ""
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
    snoozed_until: Optional[float] = None
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

    def snoozed_at(self, now: float) -> bool:
        """Is this session snoozed as of ``now``?

        Takes the clock rather than reading it, so that everything rendered from
        one frame's timestamp agrees with itself -- :mod:`mft.render` promises to
        be a pure function of (session, clock), and a boolean that quietly
        consulted the wall clock is exactly how that promise stops being true.
        """
        return self.snoozed_until is not None and now < self.snoozed_until

    @property
    def snoozed(self) -> bool:
        """As of right now. For callers outside a frame, like ``/status``."""
        return self.snoozed_at(time.monotonic())

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


def terminal_key(terminal: dict[str, Any], cwd: str = "") -> Optional[str]:
    for name in TERMINAL_KEYS:
        value = terminal.get(name)
        if value:
            return f"{name}:{value}"
    return f"cwd:{cwd}" if cwd else None


class SessionTable:
    """Thread-safe registry of terminal -> encoder slot -> current session."""

    def __init__(self, slot_count: int = config.SLOT_COUNT) -> None:
        self.slot_count = slot_count
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        #: slot -> session_id, including recently-ended sessions during the
        #: linger window so a resumed session lands back where it was.
        self._slots: dict[int, str] = {}
        #: terminal key -> slot, so a new session id in a known tab is adopted
        #: by the slot that tab already owns.
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
        self._keys = {s.key: s.slot for s in ordered}

    def compact(self) -> None:
        with self._lock:
            self._compact()

    def _evict(self, slot: int) -> None:
        """Drop whatever currently holds ``slot``, keys included."""
        previous = self._slots.get(slot)
        if previous:
            stale = self._sessions.pop(previous, None)
            if stale is not None and self._keys.get(stale.key) == slot:
                del self._keys[stale.key]
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

        ``terminal`` comes from the SessionStart command hook and is what makes
        the slot durable: when it names a tab we already have a slot for, that
        slot adopts this session id instead of a fresh encoder lighting up.

        Returns ``None`` only when all slots are held by live sessions.
        """
        with self._lock:
            key = terminal_key(terminal, cwd) if terminal else None
            session = self._sessions.get(session_id)

            if session is not None:
                if cwd:
                    session.cwd = cwd
                if key and key != session.key:
                    self._keys.pop(session.key, None)
                    # Another tab claiming this key can only be a stale record.
                    if self._keys.get(key) not in (None, session.slot):
                        self._evict(self._keys[key])
                    session.key = key
                    self._keys[key] = session.slot
                session.last_event_at = time.monotonic()
                return session

            if key is not None:
                slot = self._keys.get(key)
                if slot is not None:
                    existing = self._sessions.get(self._slots.get(slot, ""))
                    if existing is not None:
                        # Same tab, new session id: /clear, resume, compact or
                        # fork. Keep the slot and everything on it.
                        self._rekey(existing, session_id)
                        existing.cwd = cwd or existing.cwd
                        existing.last_event_at = time.monotonic()
                        log.info(
                            "session %s adopted encoder %d (%s)",
                            existing.short_id,
                            slot + 1,
                            key,
                        )
                        return existing
                    del self._keys[key]

            slot = self._free_slot()
            if slot is None:
                log.warning("no free encoder for session %s", session_id[:8])
                return None
            self._evict(slot)

            session = Session(
                session_id=session_id, slot=slot, cwd=cwd, key=key or f"sid:{session_id}"
            )
            self._sessions[session_id] = session
            self._slots[slot] = session_id
            self._keys[session.key] = slot
            log.info("session %s -> encoder %d (%s)", session.short_id, slot + 1, cwd)
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
        if self._keys.get(session.key) == session.slot:
            del self._keys[session.key]
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
