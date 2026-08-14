#!/usr/bin/env python3
"""Provider-aware, notify-only hook forwarder.

Reads one hook object from stdin, adds provider and terminal identity, and posts
it to the local visualizer. Failure is intentionally silent. Codex command
hooks receive a valid empty JSON result and neither provider ever receives a
permission/control decision from this process.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ENV_KEYS = (
    "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TERM_SESSION_ID", "ITERM_SESSION_ID",
    "__CFBundleIdentifier", "TMUX", "TMUX_PANE", "WEZTERM_PANE",
    "WEZTERM_UNIX_SOCKET", "KITTY_WINDOW_ID", "ALACRITTY_WINDOW_ID",
    "GHOSTTY_RESOURCES_DIR", "WINDOWID", "STY", "SSH_TTY", "TERM",
)


def detect_tty() -> str | None:
    for fd in (2, 1, 0):
        try:
            return os.ttyname(fd)
        except OSError:
            pass
    try:
        out = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(os.getppid())],
            capture_output=True, text=True, timeout=2, check=False,
        ).stdout.strip()
    except Exception:
        return None
    if not out or out in {"??", "-"}:
        return None
    return out if out.startswith("/dev/") else f"/dev/{out}"


def terminal_identity() -> dict[str, str]:
    tty = detect_tty()
    terminal = {key: os.environ[key] for key in ENV_KEYS if tty and os.environ.get(key)}
    if tty:
        terminal["tty"] = tty
    terminal["pid"] = str(os.getppid())
    return terminal


def forward(provider: str, url: str, event_name: str = "") -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["provider"] = provider
    payload["terminal"] = terminal_identity()
    if event_name:
        payload.setdefault("hook_event_name", event_name)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=2).read()
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    parser.add_argument("--url", default="http://127.0.0.1:7654/event")
    parser.add_argument("--event", default="")
    args = parser.parse_args(argv)
    forward(args.provider, args.url, args.event)
    if args.provider == "codex":
        # Valid hook stdout, deliberately empty: no approval, denial, rewrite,
        # injected context, or other control instruction can originate here.
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

