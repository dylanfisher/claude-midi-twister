"""The same state as the encoder, in the terminal's tab strip.

An encoder tells you a session wants you; it does not tell you *which window*
that is once you have eight of them tiled. The tab already says which window --
so a colored glyph in front of its title is the cheapest possible second
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
once a prompt, so :meth:`TabStrip.refresh_title` re-reads it once a turn and
not on the repaint tick. Falling back to the directory name matters more than it looks:
until a session's first title is generated, that is all there is.

The title is model-written text going into an escape sequence, so it is stripped
of C0 controls on the way through. A title containing a BEL would otherwise
terminate the sequence early and leave the rest of itself on the screen.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from . import config, context
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


class TabStrip:
    """The whole tab strip as one object: what we last wrote, and when.

    The functions above are the vocabulary; this is the thing that decides when
    to speak. It lives here rather than in :mod:`mft.daemon` because every write
    it makes goes down a tty this process does not own, and that whole argument
    -- short, non-blocking, handed back at the end -- is the subject of this
    module. The daemon calls :meth:`paint` once a frame and forgets about it.

    Stateless with respect to the sessions themselves: the last line written to
    each tab is remembered on the session record (``tab_painted``), so a session
    that goes away takes its bookkeeping with it.
    """

    def __init__(self, table) -> None:
        self.table = table
        #: When the strip was last considered. Nothing here animates, so it is
        #: decoupled from the frame rate entirely; see :meth:`paint`.
        self._last_paint = float("-inf")

    # -- reading ------------------------------------------------------------

    def refresh_title(self, session: Session, now: float) -> None:
        """Top up our copy of the title Claude Code generated for this session.

        On the HTTP thread with the context read, and for the same reason: this
        is a file read, and the render loop has a frame to make. Once a turn is
        as often as it can change, and the rate limit only matters for the
        session that has no title yet -- which asks again every few seconds
        until one exists, because until then the tab says nothing but a
        directory name.

        A read that comes back empty leaves `tab_title_turn` alone, so the next
        event tries again; a read that succeeds is trusted even though it may be
        the previous turn's title, since it is about to be overwritten anyway
        and a turn of lag on a tab strip is not a thing anyone can see.
        """
        if not config.TAB_TITLE or not session.transcript_path:
            return
        if session.tab_title and session.tab_title_turn == session.turn_count:
            return
        if now - session.tab_title_at < config.TAB_POLL_SECONDS:
            return
        session.tab_title_at = now
        try:
            title = context.read_title(session.transcript_path)
        except Exception:
            log.exception("title read failed for %s", session.label)
            return
        if title:
            session.tab_title = title
            session.tab_title_turn = session.turn_count

    # -- writing ------------------------------------------------------------

    def paint(self, now: float) -> None:
        """Put each session's state glyph in front of its tab title.

        Rate-limited to a twentieth of the board's frame rate and then, past
        that, gated on the composed line having actually changed -- so a session
        that spends ten minutes working costs one write, not eighteen thousand.
        The comparison is against what we last *sent*, not against the state, so
        a title that changed while the glyph didn't still gets through.
        """
        if not config.TAB_TITLE:
            return
        if now - self._last_paint < config.TAB_POLL_SECONDS:
            return
        self._last_paint = now
        for session in self.table.all():
            self._paint_one(session, glyph_for(session))

    def _paint_one(self, session: Session, glyph: str) -> None:
        tty = tty_of(session)
        if not tty:
            return
        # The directory name until Claude Code has generated a title. It is a
        # worse label but it is never wrong, and a tab reading "🔴" alone tells
        # you a session wants you without telling you which one.
        title = session.tab_title or os.path.basename(session.cwd)
        line = compose(glyph, title)
        if line == session.tab_painted:
            return
        if write(tty, line):
            session.tab_painted = line

    def restore(self, sessions: Optional[Iterable[Session]] = None) -> None:
        """Hand the tab back: the same title, without our glyph on it.

        For sessions that ended, and for every session when the daemon exits. A
        glyph outlives the thing it describes otherwise, and a green dot on a
        tab whose daemon died an hour ago is precisely the phantom encoder this
        project spends its time avoiding.

        A tty another live session is still on is left alone. That happens when
        two records turn out to describe one tab and the duplicate is dropped
        (`SessionTable.reconcile`) -- restoring there would strip the glyph off
        a session that is still running, and the survivor believes it already
        painted that tab, so nothing would put it back.
        """
        if not config.TAB_TITLE:
            return
        retiring = list(self.table.all() if sessions is None else sessions)
        ids = {id(s) for s in retiring}
        claimed = {
            tty_of(s)
            for s in self.table.all()
            if id(s) not in ids and s.state != "ended"
        } - {""}
        for session in retiring:
            if tty_of(session) in claimed:
                continue
            self._paint_one(session, "")
