"""How full a session's context window is, read out of its own transcript.

No hook payload carries token counts -- but every payload carries
``transcript_path``, and the transcript records the raw ``usage`` block of every
assistant message. The most recent one is, by definition, the size of that
agent's context right now:

    input + cache_creation + cache_read + output

Cache reads dominate and are the whole point: they *are* the conversation being
replayed, so leaving them out would report a few hundred tokens for a session
sitting at 90% full.

Two things this deliberately does not do. It never reads the whole file --
transcripts run to megabytes and this happens on the HTTP thread -- and it skips
sidechain entries, which are subagent messages living in the parent's transcript
and carrying their own much smaller contexts.

The window that total is measured against is the awkward half. The transcript
names the model on every assistant message -- ``claude-opus-5`` -- and that name
does not say which *window* it is: the 1M variant is spelled ``opus[1m]`` in
``settings.json`` and spelled exactly like the 200k one everywhere else. So the
family comes off the transcript, where it is authoritative and follows a
mid-session ``/model``, and the window marker comes off the settings files, which
are the only place it is written down. Getting this wrong is not a near miss: a
1M session at 11% full renders at 55%, which is a gauge lying in the direction
that makes you close a session you did not need to.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from . import config

log = logging.getLogger("mft.context")


def _marked_window(name: str) -> Optional[int]:
    """The window a model id spells out, or ``None`` if it doesn't spell one."""
    for token, limit in config.CONTEXT_WINDOW_MARKERS:
        if token in name:
            return limit
    return None


def _family(name: str) -> str:
    """Which model family a name belongs to, or ``""``."""
    for token, _ in config.CONTEXT_LIMITS:
        if token in name:
            return token
    return ""


#: path -> (mtime, model). A settings file is read once and then only again when
#: it changes: this is reached from the HTTP thread once per session per poll,
#: and `/model` writes the file, so the stat is both the cheap part and the
#: correct invalidation.
_settings_cache: dict[str, tuple[float, str]] = {}


def _settings_model(path: str) -> str:
    """The ``model`` a settings file names, or ``""`` -- never an exception.

    A settings file that is missing, unreadable or malformed is not an error
    here. It is one of several places a model might be named and the caller has
    a perfectly good answer without it.
    """
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        _settings_cache.pop(path, None)
        return ""
    cached = _settings_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8", errors="replace"))
        model = str(data.get("model") or "") if isinstance(data, dict) else ""
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.debug("unreadable settings at %s: %s", path, exc)
        model = ""
    _settings_cache[path] = (mtime, model)
    return model


def configured_model(cwd: str = "") -> str:
    """The model the settings files select, nearest first, or ``""``.

    Claude Code's own precedence, minus the two layers this cannot see: an
    enterprise policy file, and a `--model` passed on the command line. Both of
    those out-rank everything here, so a machine using them gets the transcript's
    answer and no marker -- which is the old behaviour, not a wrong one.
    """
    if not config.CONTEXT_SETTINGS_MODEL:
        return ""
    roots: list[str] = []
    directory = os.path.abspath(cwd) if cwd else ""
    while directory and len(roots) < config.CONTEXT_SETTINGS_DEPTH:
        roots.append(directory)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    for root in roots:
        for relative in config.CONTEXT_SETTINGS_FILES:
            model = _settings_model(os.path.join(root, relative))
            if model:
                return model
    return _settings_model(config.CONTEXT_SETTINGS_USER)


def limit_for_model(model: str, cwd: str = "") -> int:
    """Context window for a model id, by substring match.

    ``cwd`` is the session's, and only widens the search for a window marker --
    see the module docstring. The family always comes from ``model``.
    """
    name = (model or "").lower()
    marked = _marked_window(name)
    if marked is not None:
        return marked

    configured = configured_model(cwd).lower()
    marked = _marked_window(configured)
    # Only when the two agree about *which model this is*. A settings file
    # naming `sonnet[1m]` says nothing about the opus in the transcript in front
    # of us -- that is a session someone switched with `/model`, and the
    # transcript is the one that watched it happen.
    if marked is not None and _family(configured) and _family(configured) == _family(name):
        return marked

    for token, limit in config.CONTEXT_LIMITS:
        if token in name:
            return limit
    return config.CONTEXT_LIMIT_DEFAULT


def tail_lines(path: str, tail_bytes: int = config.CONTEXT_TAIL_BYTES) -> list[str]:
    """Whole lines from the last ``tail_bytes`` of a file, oldest first.

    The first line of the window is usually cut in half by the seek; it is
    dropped rather than parsed, unless the window covers the whole file.

    Shared with :mod:`mft.discover`, which reads a much smaller window off the
    same files looking for different fields.
    """
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - tail_bytes)
        handle.seek(start)
        chunk = handle.read()
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    return lines if start == 0 else lines[1:]


def _usage_tokens(usage: dict) -> int:
    return sum(
        int(usage.get(field) or 0)
        for field in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    )


def read_usage(path: str) -> Optional[tuple[int, str]]:
    """``(tokens, model)`` as of the main agent's last assistant message.

    The model comes from the same message rather than from SessionStart, so a
    mid-session ``/model`` switch moves the gauge to the right window without
    needing a hook to announce it.

    ``None`` when there's nothing to read yet: a brand new session, a transcript
    the daemon can't open, or a tail with no usable ``usage`` in it. Callers
    should treat that as "no gauge", not as zero.
    """
    if not path:
        return None
    try:
        lines = tail_lines(path)
    except OSError as exc:
        log.debug("no transcript at %s: %s", path, exc)
        return None

    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        # `isSidechain` marks a subagent's own messages, whose context window is
        # much smaller than its parent's and would drag the gauge backwards.
        # Kept defensively rather than because it currently fires: no transcript
        # written by the current Claude Code carries the flag, so subagents
        # appear to have moved out of the parent's file entirely.
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        usage = message.get("usage")
        if isinstance(usage, dict):
            tokens = _usage_tokens(usage)
            if tokens:
                return tokens, str(message.get("model") or "")
    return None


def read_title(path: str) -> str:
    """The title Claude Code generated for this session, or "".

    It writes one as an ``ai-title`` record whenever it regenerates it, which is
    roughly once a prompt, so the last one in the file is the current one. This
    is the same string it would have put in the terminal tab itself -- see
    :mod:`mft.tab`, which puts a state glyph in front of it instead.

    Cheap by the same trick as :func:`read_usage`: the string test runs on every
    line and the JSON parse only on the handful that could match.
    """
    if not path:
        return ""
    try:
        lines = tail_lines(path)
    except OSError as exc:
        log.debug("no transcript at %s: %s", path, exc)
        return ""

    for line in reversed(lines):
        if '"ai-title"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "ai-title":
            continue
        title = entry.get("aiTitle")
        if title:
            return str(title)
    return ""


def fraction(tokens: Optional[int], limit: int) -> Optional[float]:
    """0.0-1.0 fill, or ``None`` when there is nothing to report."""
    if not tokens or limit <= 0:
        return None
    return max(0.0, min(1.0, tokens / limit))
