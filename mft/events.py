"""One hook payload, folded into one session's state.

Claude Code's hooks are discrete events, and the board wants continuous states:
`thinking`, `working`, `streaming` are all *inferred* from which events arrived
and how long ago. This module is that inference and nothing else -- a pure
function of (session, payload), with no idea what an encoder is.

Two things make it longer than it looks. The notification vocabulary is not
stable, so a state is read from ``notification_type`` when there is one and from
the prose when there isn't. And a subagent is visible down two independent
signals that neither pair up nor arrive in step, which is why they are namespaced
rather than counted; see :attr:`mft.state.Session.subagents`.

`apply_event` returns *effects* -- strings the daemon acts on -- for the handful
of things that need the board or the wire rather than a field on a record: a
compaction sweep, a spawn strike, a spelled-out banner. That is how an animation
gets triggered without this module knowing that rendering exists.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from . import config
from .state import AGENT_KEY, TOOL_KEY, Session

log = logging.getLogger("mft.events")


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
            return f"{TOOL_KEY}{value}"
    return None


def _agent_key(event: dict[str, Any]) -> Optional[str]:
    agent_id = event.get("agent_id")
    return f"{AGENT_KEY}{agent_id}" if agent_id else None


def _touch_subagent(session: Session, event: dict[str, Any], now: float) -> None:
    """Credit a tool call to the subagent that made it, if one did.

    Every hook payload Claude Code builds carries `agent_id`, not just the
    subagent events: a tool call made *inside* a subagent arrives as an ordinary
    PreToolUse on the parent's `session_id` with the subagent's id alongside it.
    That is the whole activity signal, and it is the only one -- there is no
    per-subagent state, notification or permission to read.

    Only ever stamps a record that already exists; never creates one. A pile
    that grows from a signal nothing will ever retract is exactly the phantom
    encoder invariant 6 is about, and the spawn paths above already cover every
    subagent worth a dot. So an install too old to send SubagentStart keeps the
    flat pile it has always had rather than gaining a dot that outlives its
    subagent by a turn.
    """
    key = _agent_key(event)
    if key and key in session.subagents_in_flight:
        session.subagents_in_flight[key] = now


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


def still_working(session: Session) -> None:
    """Paint `working`, but only for a turn that is still running.

    Subagents outlive the turn that spawned them. A turn that launches ten of
    them in the background finishes -- `Stop`, green, decaying -- while they are
    all still going, and their `SubagentStop`s, plus the `PostToolUse` of the
    call that spawned each one, land seconds *after* it. Taken at face value
    those repaint `working` over the green and, since nothing else is coming,
    leave the encoder orange until the next prompt: a session sitting there
    waiting for you, saying it is busy. That is exactly the knob that lies for
    an hour.

    `turn_started_at` is the only thing that says a turn is live, and `Stop`
    clears it. `PreToolUse` sets it when it is missing, because a tool call is
    itself proof of a live turn -- otherwise a session adopted mid-turn, or one
    whose `UserPromptSubmit` went missing, could never look busy at all.

    The count is deliberately *not* guarded: a straggling subagent still adds
    and removes its violet pip, so the board keeps saying something is running.
    Only the parent's own colour is protected.
    """
    if session.turn_started_at is None:
        return
    session.set_state("working")


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
            session.subagents_in_flight[key] = now
        _touch_subagent(session, event, now)
        if session.turn_started_at is None:
            session.turn_started_at = now
        session.set_state("working")

    elif name in ("PostToolUse", "PostToolUseFailure"):
        tool = event.get("tool_name", "")
        session.last_tool = tool
        session.last_tool_at = now
        key = _tool_use_key(event)
        if key:
            session.subagents_in_flight.pop(key, None)
        _touch_subagent(session, event, now)
        # The arc is the activity signal: one segment per completed call, so
        # spin rate is tool-call frequency and a ring that stops is a stall.
        session.arc = (session.arc + 1) % config.ARC_SEGMENTS
        session.tool_history.append(tool)
        still_working(session)

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
            # Born bright. A subagent that has not called a tool yet is thinking,
            # not stalled, and starting it at the floor would make every spawn
            # look like it arrived already dead.
            session.subagents_in_flight[key] = now
        still_working(session)

    elif name == "SubagentStop":
        key = _agent_key(event)
        if key:
            session.subagents_in_flight.pop(key, None)
        else:
            # No id to match on, but something definitely finished, and a pile
            # that only ever grows is worse than one that shrinks arbitrarily.
            stale = next(
                (k for k in session.subagents_in_flight if k.startswith(AGENT_KEY)),
                None,
            )
            if stale:
                session.subagents_in_flight.pop(stale, None)
        still_working(session)

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
