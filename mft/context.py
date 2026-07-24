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
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from . import config

log = logging.getLogger("mft.context")


def limit_for_model(model: str) -> int:
    """Context window for a model id, by substring match."""
    name = (model or "").lower()
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


def fraction(tokens: Optional[int], limit: int) -> Optional[float]:
    """0.0-1.0 fill, or ``None`` when there is nothing to report."""
    if not tokens or limit <= 0:
        return None
    return max(0.0, min(1.0, tokens / limit))
