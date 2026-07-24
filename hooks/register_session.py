#!/usr/bin/env python3
"""Command hook: tell the daemon which terminal this session lives in.

Plain ``type: "http"`` hooks post only the event JSON -- no environment. This
one runs as a command hook so it can read the env, so it pays a process spawn.

Installed on SessionStart *and* UserPromptSubmit. SessionStart alone fires
exactly once, which means a daemon that was down at the time, or restarted
afterwards, never learns where the session lives and can never ask -- hooks are
a one-way push. Repeating it on every prompt costs one short-lived process per
turn, asynchronously, and makes the terminal identity self-healing: any session
you are actually using re-announces itself within a turn.

Anything it can't determine is simply omitted; focus adapters degrade to
"raise the app" and then to "do nothing".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

PORT = int(os.environ.get("MFT_PORT", "7654"))
HOST = os.environ.get("MFT_HOST", "127.0.0.1")
URL = f"http://{HOST}:{PORT}/event"

#: Identifiers different terminals export. Missing ones are skipped.
ENV_KEYS = (
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "TERM_SESSION_ID",  # Apple Terminal: w0t0p0:UUID
    "ITERM_SESSION_ID",
    # Exported into every process a macOS app launches, and the one identifier
    # that names the terminal exactly whatever else it does or doesn't set.
    "__CFBundleIdentifier",
    "TMUX",
    "TMUX_PANE",
    "WEZTERM_PANE",
    "WEZTERM_UNIX_SOCKET",
    "KITTY_WINDOW_ID",
    "ALACRITTY_WINDOW_ID",
    "GHOSTTY_RESOURCES_DIR",
    "WINDOWID",
    "STY",  # GNU screen
    "SSH_TTY",
    "TERM",
)


def detect_tty() -> str | None:
    """Apple Terminal identifies tabs by tty, so this is the important one."""
    for fd in (2, 1, 0):
        try:
            return os.ttyname(fd)
        except OSError:
            continue
    try:
        out = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(os.getppid())],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except Exception:
        return None
    if not out or out in ("??", "-"):
        return None
    return out if out.startswith("/dev/") else f"/dev/{out}"


def main(argv: list[str]) -> int:
    url = URL
    for index, arg in enumerate(argv):
        if arg == "--url" and index + 1 < len(argv):
            url = argv[index + 1]
        elif arg.startswith("--url="):
            url = arg.split("=", 1)[1]

    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    tty = detect_tty()
    # No tty means no terminal tab: this is the desktop app, which inherits the
    # variables of whatever launched it. Reporting those would key the slot on
    # someone else's tab and send a press to a window this session isn't in --
    # so the pid stands alone, and the daemon raises the app it belongs to.
    terminal = (
        {key: os.environ[key] for key in ENV_KEYS if os.environ.get(key)} if tty else {}
    )
    if tty:
        terminal["tty"] = tty
    terminal["pid"] = str(os.getppid())
    event["terminal"] = terminal
    event.setdefault("hook_event_name", "SessionStart")

    body = json.dumps(event).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(request, timeout=2).read()
    except (urllib.error.URLError, OSError):
        # Daemon not running: that's a fine state for a visualizer to be in.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
