#!/usr/bin/env python3
"""Compatibility entry point for the provider-aware hook forwarder.

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

import sys

from forward import main as forward_main


def main(argv: list[str]) -> int:
    return forward_main(["--provider", "claude", *argv])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
