"""What names a terminal tab, and how well.

Invariant 3 in one file: **encoders belong to terminals, not sessions.** A
`/clear` hands out a brand new session id in the *same* tab, so anything keyed
on the session id would teleport an agent to a different knob every time you
cleared. The terminal is the durable thing.

The awkward part -- and the reason this is a module rather than three functions
on :class:`~mft.state.SessionTable` -- is that no two hooks describe a tab the
same way. ``register_session.py`` reads the whole environment, ``notify.sh``
reports what it can learn without spawning a process, and :mod:`mft.discover`
recovers a third subset from the process table. So a tab is not identified by
one token but by a *set* of them, any one of which is enough to recognise it,
ranked by how well each survives the things that end a session id.

Nothing here knows what a session is. It takes payload dicts and token strings
and answers questions about them, which is what lets :mod:`mft.state` and
:mod:`mft.discover` both ask without either owning the answer.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence


#: Every field a hook can name a tab with, in order of how well each survives
#: the things that end a session id. This tuple *is* the ranking: `key_rank`,
#: `best_key` and every "which of these two describes the tab better" question
#: in the project is an index into it.
TERMINAL_KEYS = (
    "TMUX_PANE",
    "ITERM_SESSION_ID",
    "WEZTERM_PANE",
    "KITTY_WINDOW_ID",
    "TERM_SESSION_ID",
    "tty",
    "pid",  # the claude process itself: survives /clear, not a restart
    # The process a session with no terminal at all is running inside; see
    # :func:`is_hostless`. Weakest of the lot because it is not one of the tab's
    # names: it identifies this session's events and nothing else, which is why a
    # session that was handed off into such a process can hold one of these *and*
    # the pid of the tab it came from without the two contradicting each other.
    "host",
)


_KEY_RANK = {name: index for index, name in enumerate(TERMINAL_KEYS)}


def is_hostless(terminal: dict[str, Any]) -> bool:
    """Does this payload describe a bare process rather than a terminal tab?

    Both hooks drop every environment variable when they can find no controlling
    tty, and for the same reason: a session running under Claude Code's own
    background daemon -- a pre-warmed spare, a fork, a resume, a desktop-app
    window -- *inherits* the variables of whatever launched that daemon, so
    reporting them would key the encoder on a stranger's tab. What survives is
    exactly ``{"pid": ...}``, and that is the tell.
    """
    return set(terminal) == {"pid"}


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
    if is_hostless(terminal):
        # Under ``host:`` rather than ``pid:``, because that is what it is. A pid
        # with a tab behind it is one of the tab's names and outlives a `/clear`;
        # this one names a process the daemon handed a conversation to, and the
        # tab it came from -- if there was one -- keeps its own.
        return [f"host:{terminal['pid']}"]
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
    if is_hostless(arriving) and not is_hostless(stored) and stored:
        # A bare pid is not a description of a tab (:func:`is_hostless`), so it
        # cannot be the truth about one either: taking it whole would throw away
        # the tty a press needs and leave the encoder pointing at a pty host with
        # no window. The session keeps a ``host:`` token for matching, which is
        # all this payload can honestly contribute.
        return dict(stored)
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


def hostless_keys(keys: Sequence[str]) -> bool:
    """Is this everything a payload with no terminal behind it could offer?"""
    return bool(keys) and all(key_name(key) == "host" for key in keys)


def names_tab(keys: Iterable[str]) -> bool:
    """Does this set hold a token that names a terminal, not just a process?"""
    return any(key_rank(key)[0] < _KEY_RANK["pid"] for key in keys)


def best_key(keys: Iterable[str]) -> Optional[str]:
    """The most durable token in a set, or None if there are none."""
    return min(keys, key=key_rank, default=None)
