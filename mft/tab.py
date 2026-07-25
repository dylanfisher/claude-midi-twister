"""The same state as the encoder, in the terminal's tab strip.

An encoder tells you a session wants you; it does not tell you *which window*
that is once you have eight of them tiled. The tab already says which window --
so a coloured glyph in front of its title is the cheapest possible second
display, and it is the one you are looking at anyway when you alt-tab.

The mechanism is one OSC 0 sequence written straight to the session's tty:
``ESC ] 0 ; <title> BEL``. The daemon is not that session's process, but it does
run as the same user, so opening ``/dev/ttys004`` and writing to it puts bytes
into the emulator's parser exactly as if the session had printed them -- and OSC
is not printable, so nothing appears on screen and no redraw is disturbed. Every
terminal worth naming honours it (Terminal.app, iTerm2, Ghostty, kitty, wezterm,
Alacritty), which is why this is one code path and not an adapter table like
:mod:`mft.focus`.

Two non-obvious decisions.

**Claude Code has to be told to stop writing its own title**, via
``CLAUDE_CODE_DISABLE_TERMINAL_TITLE``, which ``install_hooks.py`` sets. Its
title carries an animated spinner, so while a turn is running it rewrites the
title several times a second and our glyph survives for about a frame. Racing it
was tried and is exactly as bad as it sounds. Taking the title over instead
means we owe the user the title they lost, which is the second decision.

**The title is Claude's own, read out of the transcript.** Claude Code writes
its generated title into the transcript as an ``ai-title`` record -- the same
string it would have put in the tab -- so the prefix goes in front of the real
thing rather than in front of something we invented. It is regenerated about
once a prompt, so :mod:`mft.daemon` re-reads it once a turn and not on the
repaint tick. Falling back to the directory name matters more than it looks:
until a session's first title is generated, that is all there is.

The title is model-written text going into an escape sequence, so it is stripped
of C0 controls on the way through. A title containing a BEL would otherwise
terminate the sequence early and leave the rest of itself on the screen.
"""

from __future__ import annotations

import logging
import os

from . import config
from .state import Session

log = logging.getLogger("mft.tab")

#: Delete every C0 control and DEL. Not escaped, deleted: there is no escaping
#: available inside an OSC string, and no real title wants one of these.
_STRIP = {code: None for code in list(range(0x20)) + [0x7F]}


def sanitize(title: str) -> str:
    """A model-written title, safe to put inside an escape sequence."""
    return str(title or "").translate(_STRIP).strip()


def glyph_for(session: Session) -> str:
    """The state glyph for a session, or "" for one with nothing to say."""
    if session.state == "ended":
        return ""
    if session.unsupervised:
        return config.TAB_UNSUPERVISED_GLYPH
    return config.TAB_GLYPHS.get(session.state, "")


def compose(glyph: str, title: str, limit: int = config.TAB_TITLE_MAX) -> str:
    """``"🔴 Update the tab painter"`` -- the whole title line.

    Truncated before the glyph goes on, so the glyph is never the thing that
    falls off the end. An empty title still yields the bare glyph: knowing a
    session is asking for you is worth more than knowing what it is called.
    """
    title = sanitize(title)
    if limit > 0 and len(title) > limit:
        title = title[: max(0, limit - 1)].rstrip() + "\N{HORIZONTAL ELLIPSIS}"
    if not glyph:
        return title
    return f"{glyph} {title}".strip()


def sequence(title: str) -> str:
    """The OSC 0 escape that sets both the window title and the tab's icon name.

    OSC 0 rather than OSC 2 (window title only): a tab strip shows the *icon
    name*, and setting only the window title paints the one place you weren't
    looking.
    """
    return f"\x1b]0;{title}\x07"


def tty_of(session: Session) -> str:
    """The tty this session's tab is on, or "" if we were never told.

    ``/dev/`` is prepended by the hook, but discovery builds terminal payloads
    out of ``ps`` output, which is not so careful.
    """
    tty = str(session.terminal.get("tty") or "").strip()
    if not tty or tty in ("??", "-"):
        return ""
    return tty if tty.startswith("/dev/") else f"/dev/{tty}"


def write(tty: str, title: str) -> bool:
    """Put one title on one tty. False on any failure, and never raises.

    ``O_NONBLOCK`` on both the open and the write, because neither is ours to
    block on: opening a tty can wait on a modem-control handshake, and writing
    to one whose emulator has stopped reading would park the render loop behind
    a window someone minimised. A single ``os.write`` of a short byte string is
    what keeps this from interleaving into the middle of the session's own
    output -- see TAB_TITLE_MAX.

    Every failure mode here is ordinary and none of them are worth more than a
    debug line: the tab was closed, the tty was recycled, the session is on a
    machine we can't reach.
    """
    fd = -1
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
        os.write(fd, sequence(title).encode("utf-8"))
        return True
    except OSError as exc:
        log.debug("tab paint failed on %s: %s", tty, exc)
        return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
