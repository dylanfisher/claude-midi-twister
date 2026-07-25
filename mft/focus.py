"""Raise the terminal a session lives in, when its encoder is pressed.

Adapters are deliberately tiny: each one says how to recognise its terminal
from the environment the session started in, and how to bring that specific
tab/pane to the front. Adding a terminal means appending one ``Adapter`` to
``ADAPTERS`` -- no other file changes.

Two kinds run in sequence, because a session is often inside both: ``mux``
adapters (tmux, screen) move the selection *within* the terminal, then the
``gui`` adapters raise the application window itself.

The gui adapters form a *chain*, not a choice. Every one of them can fail for
reasons that have nothing to do with whether it was the right adapter -- remote
control switched off in kitty, a wezterm socket that moved, an AppleScript that
came back empty because the app was mid-launch -- so a failure falls through to
the next adapter rather than ending the attempt. The tail of the chain needs
nothing but a pid, which every session has, so "pressed the knob and nothing
happened" takes a real failure rather than a missing environment variable.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("mft.focus")

Ctx = dict[str, Any]  # the env snapshot captured at SessionStart


def _run(cmd: list[str], timeout: float = 4.0) -> bool:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except Exception as exc:
        log.warning("focus command failed: %s (%s)", cmd[0], exc)
        return False
    if proc.returncode != 0:
        log.warning("focus command %s exited %d: %s", cmd[0], proc.returncode, proc.stderr.strip())
        return False
    return True


def _osascript(script: str, retries: int = 1) -> str:
    """Run AppleScript and return its result string ("" on failure).

    The tab-hunting scripts return "ok" or "notfound", so the caller can tell
    "raised the right tab" from "found nothing, changed nothing".

    Retried once by default: AppleScript against a busy or just-launched app
    fails with a timeout (-1712) or "application isn't running" often enough
    that a single retry is the difference between a knob that works and one
    that works most of the time.
    """
    for attempt in range(retries + 1):
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
        except Exception as exc:
            log.warning("osascript failed: %s", exc)
            return ""
        if proc.returncode == 0:
            return proc.stdout.strip()
        log.warning(
            "osascript exited %d%s: %s",
            proc.returncode,
            " (retrying)" if attempt < retries else "",
            proc.stderr.strip(),
        )
    return ""


def _q(value: str) -> str:
    """Quote a value for embedding in AppleScript."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _norm_tty(tty: str) -> str:
    """`ps` says ``ttys004``; AppleScript and ``os.ttyname`` say ``/dev/ttys004``."""
    tty = str(tty or "").strip()
    if not tty or tty in ("??", "-"):
        return ""
    return tty if tty.startswith("/dev/") else f"/dev/{tty}"


@dataclass(frozen=True)
class Adapter:
    name: str
    kind: str  # "mux" or "gui"
    detect: Callable[[Ctx], bool]
    focus: Callable[[Ctx], bool]


# --- multiplexers -----------------------------------------------------------


def _tmux_focus(ctx: Ctx) -> bool:
    pane = ctx.get("TMUX_PANE")
    if not pane or not shutil.which("tmux"):
        return False
    ok = _run(["tmux", "select-pane", "-t", pane])
    # -t on switch-client wants the session, which select-window resolves for us.
    ok = _run(["tmux", "select-window", "-t", pane]) and ok
    if ctx.get("TMUX"):
        _run(["tmux", "switch-client", "-t", pane])
    return ok


# --- GUI terminals ----------------------------------------------------------


def _terminal_app_focus(ctx: Ctx) -> bool:
    """Apple Terminal has no tab IDs, but every tab exposes its tty.

    The search runs *before* anything is activated, and the app is only
    activated once the tab is in hand. That ordering is what lets this adapter
    be tried on a session whose ``TERM_PROGRAM`` was never captured: guessing
    wrong costs a no-op instead of raising the wrong application. ``is
    running`` is checked for the same reason -- ``tell application "Terminal"``
    would otherwise *launch* Terminal to ask it about tabs it doesn't have.
    """
    tty = _norm_tty(ctx.get("tty", ""))
    if not tty:
        return False
    script = f"""
    if application "Terminal" is not running then return "notfound"
    tell application "Terminal"
        repeat with w in every window
            try
                repeat with t in tabs of w
                    if tty of t is {_q(tty)} then
                        set selected tab of w to t
                        set index of w to 1
                        activate
                        return "ok"
                    end if
                end repeat
            end try
        end repeat
    end tell
    return "notfound"
    """
    result = _osascript(script)
    if result != "ok":
        log.info("no Terminal tab with tty %s", tty)
    return result == "ok"


def _iterm_focus(ctx: Ctx) -> bool:
    raw = str(ctx.get("ITERM_SESSION_ID", ""))
    guid = raw.split(":", 1)[1] if ":" in raw else raw
    if not guid:
        return False
    script = f"""
    if application "iTerm" is not running then return "notfound"
    tell application "iTerm"
        repeat with w in every window
            try
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if id of s is {_q(guid)} then
                            select w
                            select t
                            select s
                            activate
                            return "ok"
                        end if
                    end repeat
                end repeat
            end try
        end repeat
    end tell
    return "notfound"
    """
    result = _osascript(script)
    if result != "ok":
        log.info("no iTerm session %s", guid)
    return result == "ok"


def _wezterm_focus(ctx: Ctx) -> bool:
    pane = ctx.get("WEZTERM_PANE")
    if not pane or not shutil.which("wezterm"):
        return False
    return _run(["wezterm", "cli", "activate-pane", "--pane-id", str(pane)])


def _kitty_focus(ctx: Ctx) -> bool:
    window = ctx.get("KITTY_WINDOW_ID")
    if not window or not shutil.which("kitty"):
        return False
    return _run(["kitty", "@", "focus-window", "--match", f"id:{window}"])


def _bundle_focus(ctx: Ctx) -> bool:
    """Raise by bundle id.

    ``__CFBundleIdentifier`` is exported into every process a macOS app
    launches, so it is present for terminals that advertise nothing else about
    themselves, and it identifies the app exactly -- no name to spell, no
    localisation, no two apps called Terminal.
    """
    bundle = str(ctx.get("__CFBundleIdentifier", "")).strip()
    if not bundle:
        return False
    return _run(["open", "-b", bundle])


#: TERM_PROGRAM values that we can at least bring to the front by name.
_APP_NAMES = {
    "apple_terminal": "Terminal",
    "iterm.app": "iTerm",
    "ghostty": "Ghostty",
    "warpterminal": "Warp",
    "alacritty": "Alacritty",
    "hyper": "Hyper",
    "vscode": "Visual Studio Code",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "tabby": "Tabby",
    "rio": "Rio",
    "wezterm": "WezTerm",
    "kitty": "kitty",
}


def _app_focus(ctx: Ctx) -> bool:
    """Raise the application by name, without picking the tab."""
    term = str(ctx.get("TERM_PROGRAM", "")).lower()
    app = _APP_NAMES.get(term) or ctx.get("TERM_PROGRAM")
    if not app:
        return False
    log.info("no tab-level adapter took for %s; raising the app", app)
    return _run(["open", "-a", str(app)])


# --- last resort: the process tree ------------------------------------------


def _ancestors(pid: int, limit: int = 12) -> list[tuple[int, str]]:
    """(pid, argv) from ``pid`` up towards launchd, nearest first."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        ).stdout
    except Exception as exc:
        log.debug("process table unreadable: %s", exc)
        return []

    parents: dict[int, int] = {}
    argv: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            child, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parents[child] = parent
        argv[child] = parts[2]

    chain: list[tuple[int, str]] = []
    current = pid
    while current > 1 and len(chain) < limit:
        if current not in argv:
            break
        chain.append((current, argv[current]))
        current = parents.get(current, 0)
    return chain


def _owning_app(pid: int) -> str:
    """The ``.app`` bundle a process is running inside, walking up its parents.

    Terminals are GUI apps, so every session process is a descendant of one:
    ``/Applications/Ghostty.app/Contents/MacOS/ghostty``. This is the identity
    that survives when a terminal exports nothing at all about itself.
    """
    for _, argv in _ancestors(pid):
        head = argv.split(None, 1)[0]
        marker = "/Contents/MacOS/"
        if marker not in head:
            continue
        bundle = head[: head.index(marker)]
        if bundle.endswith(".app") and os.path.isdir(bundle):
            return bundle
    return ""


def _ancestor_focus(ctx: Ctx) -> bool:
    try:
        pid = int(str(ctx.get("pid", "")).strip())
    except (TypeError, ValueError):
        return False
    bundle = _owning_app(pid)
    if not bundle:
        log.info("pid %d is not running inside a .app; nothing left to raise", pid)
        return False
    log.info("raising %s, found by walking up from pid %d", bundle, pid)
    return _run(["open", "-a", bundle])


def _term_is(*names: str) -> Callable[[Ctx], bool]:
    wanted = {n.lower() for n in names}
    return lambda ctx: str(ctx.get("TERM_PROGRAM", "")).lower() in wanted


def _is_apple_terminal(ctx: Ctx) -> bool:
    return _term_is("Apple_Terminal")(ctx) or (
        str(ctx.get("__CFBundleIdentifier", "")) == "com.apple.Terminal"
    )


def _maybe_terminal_app(ctx: Ctx) -> bool:
    """Worth *asking* Terminal about this session's tty.

    Either it says so outright, or it has said nothing at all and has a tty --
    in which case asking is free, because the script above changes nothing, and
    activates nothing, when the tty isn't one of Terminal's.
    """
    if not _norm_tty(ctx.get("tty", "")):
        return False
    return _is_apple_terminal(ctx) or not ctx.get("TERM_PROGRAM")


ADAPTERS: list[Adapter] = [
    Adapter("tmux", "mux", lambda c: bool(c.get("TMUX_PANE")), _tmux_focus),
    Adapter("wezterm", "gui", lambda c: bool(c.get("WEZTERM_PANE")), _wezterm_focus),
    Adapter("kitty", "gui", lambda c: bool(c.get("KITTY_WINDOW_ID")), _kitty_focus),
    Adapter("iterm2", "gui", lambda c: bool(c.get("ITERM_SESSION_ID")), _iterm_focus),
    Adapter("terminal.app", "gui", _maybe_terminal_app, _terminal_app_focus),
    # From here down nothing picks a tab; these only guarantee that *something*
    # comes to the front, in descending order of how exactly they name the app.
    Adapter("bundle-id", "gui", lambda c: bool(c.get("__CFBundleIdentifier")), _bundle_focus),
    Adapter("app-name", "gui", lambda c: bool(c.get("TERM_PROGRAM")), _app_focus),
    Adapter("ancestor", "gui", lambda c: bool(c.get("pid")), _ancestor_focus),
]

#: Keys that name one *tab* rather than an application. Apple Terminal's tty
#: belongs with them, but only once something confirms it really is Terminal.
_TAB_KEYS = ("TMUX_PANE", "WEZTERM_PANE", "KITTY_WINDOW_ID", "ITERM_SESSION_ID")


def precise(ctx: Optional[Ctx]) -> bool:
    """Can we land on this session's exact tab, rather than just its window?

    The daemon asks before pressing: a "no" is worth a trip to the process
    table for a better answer, and that trip is worth making only once.
    """
    if not ctx:
        return False
    if any(ctx.get(key) for key in _TAB_KEYS):
        return True
    return bool(_norm_tty(ctx.get("tty", ""))) and _is_apple_terminal(ctx)


def focus(ctx: Ctx) -> bool:
    """Bring the session's terminal to the front.

    Returns True as soon as one adapter reports success. A gui adapter that
    fails is not treated as the answer: the chain continues, ending with ones
    that need only a bundle id or a pid.
    """
    if not ctx:
        log.info("session has no terminal context (SessionStart hook not installed?)")
        return False

    done_any = False
    for adapter in ADAPTERS:
        if adapter.kind == "mux" and adapter.detect(ctx):
            done_any = adapter.focus(ctx) or done_any

    tried: list[str] = []
    for adapter in ADAPTERS:
        if adapter.kind != "gui" or not adapter.detect(ctx):
            continue
        tried.append(adapter.name)
        try:
            if adapter.focus(ctx):
                log.info("focused via %s adapter", adapter.name)
                return True
        except Exception:
            log.exception("%s adapter raised", adapter.name)
    if tried:
        log.warning("every focus adapter failed (tried: %s)", ", ".join(tried))
    else:
        log.info("no focus adapter matched this session's terminal: %s", sorted(ctx))
    return done_any
