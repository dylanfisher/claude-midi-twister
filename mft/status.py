"""What ``GET /status`` says, as one pure function.

    curl -s localhost:7654/status | python3 -m json.tool

Split out of :mod:`mft.daemon` because it is the daemon's *explanation of
itself* rather than part of running it, and because it is the first thing anyone
-- person or agent -- reaches for when the board looks wrong. Building the dict
here, from arguments rather than from a `Visualizer`'s attributes, means the
whole answer can be read in one screen and asserted against without a daemon.

Everything in here exists to answer one of the ways a board can look broken
while the daemon is perfectly healthy: it is asleep, the machine is, the port
died, or you are looking at a bank the sessions are not on.
"""

from __future__ import annotations

from typing import Sequence

from . import config, focus
from .state import Session


def session_payload(session: Session, now: float) -> dict:
    """One session's row. Keys are stable; adding to them is cheap and dropping
    one is a breaking change to anything grepping this output."""
    return {
        "session_id": session.session_id,
        "encoder": session.slot + 1,
        "bank": session.slot // config.ENCODERS_PER_BANK + 1,
        "key": session.key,
        # Every token it answers to, not just the strongest: when a knob is on
        # the wrong session this is the field that says which tab the daemon
        # thinks it belongs to, and why.
        "keys": sorted(session.keys),
        "state": session.state,
        "cwd": session.cwd,
        "turns": session.turn_count,
        "tool_calls": session.tool_calls,
        "last_tool": session.last_tool,
        "subagents": session.subagents,
        "model": session.model,
        "context_tokens": session.context_tokens,
        "context_limit": session.context_limit,
        "context_pct": (
            round(100 * session.context_fraction)
            if session.context_fraction is not None
            else None
        ),
        "alert": session.alert,
        "unsupervised": session.unsupervised,
        "attention_for": (
            round(now - session.attention_since, 1) if session.attention_since else 0
        ),
        "terminal": session.terminal.get("TERM_PROGRAM", "?"),
        "idle_for": round(now - session.last_event_at, 1),
    }


def payload(
    sessions: Sequence[Session],
    now: float,
    *,
    device: str,
    sleep: float,
    suspended: bool,
    port_failing: bool,
    bank: int,
) -> dict:
    """The whole answer. ``bank`` is zero-based in; one-based out, like slots."""
    return {
        "device": device,
        # Why the desk is dark, which is otherwise indistinguishable from a
        # daemon that died with the lights off.
        "sleep": round(sleep, 2),
        # The other two ways to be dark and healthy: the machine is asleep, or
        # the port stopped taking writes and is waiting to be reopened.
        "suspended": suspended,
        "port_failing": port_failing,
        # Terminals macOS has refused us Automation access to. Non-empty means
        # press-to-focus is raising apps rather than tabs, and the remedy is a
        # permission, not a code change.
        "focus_denied": focus.denied_apps(),
        # Which sixteen of the sixty-four encoders are actually on the front
        # panel. The other reason a board looks empty: everything is on a bank
        # you are not looking at. Compare against each session's own `bank`.
        "bank": bank + 1,
        "sessions": [
            session_payload(s, now) for s in sorted(sessions, key=lambda s: s.slot)
        ],
    }
