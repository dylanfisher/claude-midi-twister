"""Which tab you are actually looking at, so the board can point at it.

:mod:`mft.focus` is the outbound half of this: press an encoder, raise a tab.
This is the inbound half, and it exists because the board otherwise knows
everything about a session except the one thing you know -- that you are sitting
in it right now. An encoder that marks the tab in front of you turns the board
from a list of agents into a map with a *you are here* on it, and it is what
lets :meth:`mft.state.Session.attended` finally mean what its docstring has
always claimed: the debt is forgiven the instant you focus the tab, not the
instant you press a knob.

There is no notification to subscribe to, so this polls. Two layers, because
the two questions cost wildly different amounts:

*   **Which application is in front** -- free. `CGWindowListCopyWindowInfo`
    through `ctypes`, the same way :mod:`mft.power` reaches `CGDisplayIsAsleep`,
    and the same reason: this project has two dependencies and neither of them
    is going to be a framework bridge. Only `kCGWindowName` -- the *title* --
    is redacted without Screen Recording permission; the owner's name and the
    window layer, which is all this needs, are not. No permission, no
    subprocess, ~1ms.
*   **Which tab inside it** -- an AppleScript, and therefore a subprocess. About
    80ms of wall clock on a warm machine, which is three dropped frames, so it
    never runs on the render thread.

The saving grace is that the second question usually does not have to be asked.
If the app in front hosts exactly one session on the board, the free answer is
already the exact answer, and most desks most of the time have one Claude per
terminal. The AppleScript is only for disambiguating *two or more* sessions in
the same application, and only while that application is frontmost -- switch to
a browser and the polling stops entirely.

What was tried and abandoned: terminal focus reporting (``CSI ?1004h``) is the
native answer and is unavailable twice over -- the daemon writes down a session's
tty (:mod:`mft.tab`) but never reads it, and Apple Terminal does not implement
the mode anyway. An `AXObserver` on the terminal's process is genuinely
event-driven and wants an Accessibility grant, a dynamically allocated
Objective-C class to carry the callback, and then hands back a window *title* --
a string :mod:`mft.tab` is itself writing into. A permission prompt and a
feedback loop, to save a subprocess that mostly does not run.

Adapters are shaped like the ones in :mod:`mft.focus` and for the same reason:
adding a terminal is one entry in :data:`TERMINALS`. A terminal with no
``front_tty`` is not unsupported -- it still gets the free single-session
answer, which is the common case -- it just cannot tell two of its own tabs
apart. That is the deliberate shape of the degradation everywhere here: this
module answers "nobody" far more readily than it answers wrongly, because a
missing marker is a marker you notice is missing, and a wrong one moves your
eye to the wrong knob (invariant 6).

One case it cannot see through: inside tmux, the tty Terminal reports is the
*client's*, not the pane's, so a multiplexed tab resolves to no session at all.
Marking the wrong pane would be worse.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from . import config
from .focus import _osascript
from .state import Session

log = logging.getLogger("mft.attention")

_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CORE_FOUNDATION = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)

#: `kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements`. The
#: on-screen list is documented to come back front-to-back, which is the entire
#: mechanism here: the first ordinary window in it is the one you are looking at.
_WINDOW_LIST_OPTIONS = (1 << 0) | (1 << 4)

#: `kCFStringEncodingUTF8` and `kCFNumberSInt32Type`.
_UTF8 = 0x08000100
_SINT32 = 3

#: How far down the window list to look for an ordinary window before giving up.
#: The answer is normally the first entry; a bound at all is here so that a
#: pathological list cannot turn a poll into a scan.
_MAX_WINDOWS = 40


# --- the free half: which application ---------------------------------------


def _window_list():
    """CoreGraphics and CoreFoundation with the window-list symbols bound, plus
    the CFString keys, or None where any of that does not exist.

    Cached on the function like :func:`mft.power._core_graphics`, and for a
    stronger reason: the CFStrings are allocated objects, and building three of
    them per poll would be a leak on a four-times-a-second clock.
    """
    if _window_list.cached is not None:
        return _window_list.cached or None
    _window_list.cached = False
    if sys.platform != "darwin":
        return None
    try:
        cg = ctypes.CDLL(_CORE_GRAPHICS)
        cf = ctypes.CDLL(_CORE_FOUNDATION)
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        keys = {
            name: cf.CFStringCreateWithCString(None, value.encode(), _UTF8)
            for name, value in (
                ("layer", "kCGWindowLayer"),
                ("owner", "kCGWindowOwnerName"),
            )
        }
        if not all(keys.values()):
            return None
    except Exception:
        log.debug("CoreGraphics window list unavailable", exc_info=True)
        return None
    _window_list.cached = (cg, cf, keys)
    return _window_list.cached


_window_list.cached = None


def _cf_int(cf, ref) -> Optional[int]:
    if not ref:
        return None
    out = ctypes.c_int32()
    if not cf.CFNumberGetValue(ref, _SINT32, ctypes.byref(out)):
        return None
    return out.value


def _cf_str(cf, ref) -> str:
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(256)
    if not cf.CFStringGetCString(ref, buf, len(buf), _UTF8):
        return ""
    return buf.value.decode(errors="replace")


def frontmost_app() -> Optional[str]:
    """The name of the application owning the frontmost ordinary window.

    ``""`` for "nothing in front we can name", ``None`` for "cannot ask" -- a
    distinction only the logs care about, since both mean the board marks
    nothing, but the second one is a machine where this feature is simply
    absent and the first is a desk with a browser on it.

    Layer zero is what makes it *ordinary*: the menu bar, the Dock and every
    notification banner are windows too, and each of them is in front of your
    terminal in the literal sense this list means.
    """
    libs = _window_list()
    if libs is None:
        return None
    cg, cf, keys = libs
    try:
        info = cg.CGWindowListCopyWindowInfo(_WINDOW_LIST_OPTIONS, 0)
    except Exception:
        return None
    if not info:
        return None
    try:
        count = min(cf.CFArrayGetCount(info), _MAX_WINDOWS)
        for index in range(count):
            entry = cf.CFArrayGetValueAtIndex(info, index)
            if not entry:
                continue
            if _cf_int(cf, cf.CFDictionaryGetValue(entry, keys["layer"])) != 0:
                continue
            name = _cf_str(cf, cf.CFDictionaryGetValue(entry, keys["owner"]))
            if name:
                return name
        return ""
    except Exception:
        log.debug("could not read the window list", exc_info=True)
        return None
    finally:
        try:
            cf.CFRelease(info)
        except Exception:
            pass


# --- the costly half: which tab -----------------------------------------


def _terminal_app_tty() -> str:
    """Apple Terminal has no tab ids, but every tab exposes its tty -- the same
    fact :func:`mft.focus._terminal_app_focus` searches on, asked the other way
    round, which is why one line answers where that one needs a loop.

    ``is running`` is checked for the reason it is checked there: ``tell
    application "Terminal"`` would otherwise *launch* Terminal to ask it about
    tabs it does not have.
    """
    return _osascript(
        """
    if application "Terminal" is not running then return ""
    tell application "Terminal"
        if (count of windows) is 0 then return ""
        try
            return tty of selected tab of front window
        on error
            return ""
        end try
    end tell
    """,
        retries=0,
    )


def _iterm_tty() -> str:
    """iTerm names its tabs, but it also exposes a tty, and the tty is the token
    :mod:`mft.identity` already ranks -- taking the GUID would mean teaching
    that module a second way to say the same tab."""
    return _osascript(
        """
    if application "iTerm" is not running then return ""
    tell application "iTerm"
        if (count of windows) is 0 then return ""
        try
            return tty of current session of current window
        on error
            return ""
        end try
    end tell
    """,
        retries=0,
    )


@dataclass(frozen=True)
class Terminal:
    """One terminal application, from the board's side of the glass.

    ``owners`` are what CoreGraphics calls the app; the other three are how a
    *session* says it lives there, and there are three of them because the hooks
    do not all report the same fields (:mod:`mft.identity`, at one remove).
    ``front_tty`` is optional on purpose -- see the module docstring.
    """

    name: str
    owners: tuple[str, ...]
    term_programs: tuple[str, ...] = ()
    bundles: tuple[str, ...] = ()
    env_keys: tuple[str, ...] = ()
    front_tty: Optional[Callable[[], str]] = None

    def owns(self, app: str) -> bool:
        return app.lower() in {owner.lower() for owner in self.owners}

    def hosts(self, session: Session) -> bool:
        """Does this session live in this application, as far as anyone said?"""
        terminal = session.terminal
        program = str(terminal.get("TERM_PROGRAM", "")).lower()
        bundle = str(terminal.get("__CFBundleIdentifier", "")).lower()
        if program and program in {p.lower() for p in self.term_programs}:
            return True
        if bundle and bundle in {b.lower() for b in self.bundles}:
            return True
        return any(terminal.get(key) for key in self.env_keys)


#: Every terminal the marker knows. The first two can tell their own tabs apart;
#: the rest get the free answer when they are hosting one session, and honestly
#: nothing when they are hosting two. Adding one is an entry here.
TERMINALS: tuple[Terminal, ...] = (
    Terminal(
        "terminal.app",
        owners=("Terminal",),
        term_programs=("Apple_Terminal",),
        bundles=("com.apple.Terminal",),
        front_tty=_terminal_app_tty,
    ),
    Terminal(
        "iterm2",
        owners=("iTerm2", "iTerm"),
        term_programs=("iTerm.app",),
        bundles=("com.googlecode.iterm2",),
        front_tty=_iterm_tty,
    ),
    Terminal(
        "ghostty",
        owners=("Ghostty",),
        term_programs=("ghostty",),
        bundles=("com.mitchellh.ghostty",),
    ),
    Terminal(
        "kitty",
        owners=("kitty",),
        term_programs=("kitty",),
        env_keys=("KITTY_WINDOW_ID",),
    ),
    Terminal(
        "wezterm",
        owners=("WezTerm", "wezterm-gui"),
        term_programs=("WezTerm",),
        env_keys=("WEZTERM_PANE",),
    ),
    Terminal(
        "alacritty",
        owners=("Alacritty",),
        term_programs=("alacritty",),
        bundles=("org.alacritty",),
    ),
)


def terminal_for(app: str) -> Optional[Terminal]:
    """Which of :data:`TERMINALS` is showing, or None for anything else."""
    if not app:
        return None
    for terminal in TERMINALS:
        if terminal.owns(app):
            return terminal
    return None


def tty_keys(tty: str) -> tuple[str, ...]:
    """Both spellings of a tty, as :mod:`mft.identity` tokens.

    ``ps`` says ``ttys004`` and AppleScript says ``/dev/ttys004``, and which of
    the two is on a session depends on whether it arrived by hook or by
    discovery. Emitting both is cheaper than deciding, and cannot be wrong:
    no two tabs differ only by the prefix.
    """
    tty = str(tty or "").strip()
    if not tty or tty in ("??", "-"):
        return ()
    bare = tty[len("/dev/") :] if tty.startswith("/dev/") else tty
    return (f"tty:/dev/{bare}", f"tty:{bare}")


def resolve(keys: Sequence[str], sessions: Sequence[Session]) -> Optional[Session]:
    """The session whose tab answers to one of these tokens. Pure.

    Ambiguity resolves to nothing rather than to the first match: two records
    holding the same token is the state `SessionTable.reconcile` exists to
    repair, and marking one of them in the meantime would be picking a knob by
    coin toss.
    """
    wanted = set(keys)
    if not wanted:
        return None
    found = [session for session in sessions if session.keys & wanted]
    return found[0] if len(found) == 1 else None


class AttentionWatcher:
    """The polls, and the one answer they add up to.

    Everything expensive is injected, so the interesting cases -- two tabs in
    one app, an AppleScript that fails, a machine where none of this exists --
    are all reachable from a test with no windows and no clock.

    Threading is the census's, for the census's reason (:mod:`mft.upkeep`): the
    cheap poll happens on the render thread because it is cheap, the subprocess
    happens on a thread of its own, one at a time, and ``wake`` brings the loop
    back to full rate when that thread has changed what the board should say.
    """

    def __init__(
        self,
        *,
        front: Callable[[], Optional[str]] = frontmost_app,
        wake: Callable[[], None] = lambda: None,
    ) -> None:
        self._front = front
        self._wake = wake
        self._lock = threading.Lock()
        #: The tokens naming the tab in front, or empty for "nobody". Written by
        #: the render thread and by the asking thread; read by both.
        self._keys: tuple[str, ...] = ()
        #: What CoreGraphics last called the frontmost app, for `/status` and to
        #: notice an app switch, which is when a stale tty stops being an answer.
        self._app: str = ""
        self._asking = threading.Event()
        self._last_poll = float("-inf")
        self._last_ask = float("-inf")
        #: Terminals whose AppleScript just failed, and when. A denial or a
        #: missing scripting dictionary does not get better between one poll and
        #: the next, and asking four times a second is how a quiet feature turns
        #: into a busy log.
        self._backoff: dict[str, float] = {}

    @property
    def app(self) -> str:
        with self._lock:
            return self._app

    @property
    def keys(self) -> tuple[str, ...]:
        with self._lock:
            return self._keys

    def poll(self, now: float, sessions: Sequence[Session]) -> None:
        """Look, on the render thread. Never blocks for longer than a window
        list, and never raises."""
        if now - self._last_poll < config.ATTENTION_POLL_SECONDS:
            return
        self._last_poll = now
        try:
            app = self._front() or ""
        except Exception:
            log.debug("frontmost app unavailable", exc_info=True)
            app = ""
        with self._lock:
            switched = app != self._app
            self._app = app

        terminal = terminal_for(app)
        if terminal is None:
            # Not a terminal, so nothing on the board is in front of you. This
            # is the common case and it is the cheap one: no subprocess ever
            # runs while you are reading a browser.
            self._set(())
            return

        hosted = [s for s in sessions if terminal.hosts(s)]
        if len(hosted) == 1:
            # The free answer *is* the exact answer. One Claude per terminal is
            # the ordinary desk, so this is the path most polls take.
            #
            # Sorted, and that is not tidiness: `_set` wakes the render loop when
            # the answer *changes*, and a set iterated into a tuple can come back
            # in a different order for the same tab -- which would look like a
            # change every quarter second and hold the loop at 30Hz forever.
            self._set(tuple(sorted(hosted[0].keys)))
            return
        if not hosted or terminal.front_tty is None:
            # Nothing of ours in there, or two of ours and no way to ask which.
            self._set(())
            return
        self._ask(terminal, now, force=switched)

    def _set(self, keys: tuple[str, ...]) -> None:
        with self._lock:
            changed = keys != self._keys
            self._keys = keys
        if changed:
            self._wake()

    def _ask(self, terminal: Terminal, now: float, force: bool) -> None:
        """Spend a subprocess on which tab, off the render thread.

        ``force`` is an app switch, which is the one moment the previous answer
        is certainly stale -- inside one app the tab you are on changes far less
        often than the ordinary tick, so everything else waits for it.
        """
        if self._asking.is_set():
            return
        if now - self._backoff.get(terminal.name, float("-inf")) < config.ATTENTION_BACKOFF_SECONDS:
            return
        if not force and now - self._last_ask < config.ATTENTION_ASK_SECONDS:
            return
        self._last_ask = now
        self._asking.set()
        threading.Thread(
            target=self._ask_now, args=(terminal,), name="mft-attention", daemon=True
        ).start()

    def _ask_now(self, terminal: Terminal) -> None:
        try:
            tty = terminal.front_tty()
            keys = tty_keys(tty)
            if not keys:
                # Not necessarily a failure -- a Terminal window showing no tab
                # answers this way too -- but there is nothing to mark either
                # way, and backing off keeps a genuine refusal from being asked
                # about forever.
                self._backoff[terminal.name] = self._last_ask
            self._set(keys)
        except Exception:
            log.debug("could not ask %s which tab is in front", terminal.name, exc_info=True)
            self._backoff[terminal.name] = self._last_ask
        finally:
            self._asking.clear()

    def focused(self, sessions: Sequence[Session]) -> Optional[Session]:
        """The session you are looking at, or None. Cheap; call it per frame."""
        return resolve(self.keys, sessions)
