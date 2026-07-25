"""The one file that says whether a daemon is already running.

Split out of :mod:`mft.daemon` because it is the only piece of that module with
nothing to do with the board: it is process bookkeeping, read by the launcher
(``--status``, ``--stop``) and by the app bundle's toggle script long before
anything has opened a MIDI port.

Liveness is a signal 0 rather than the file's mere existence. A daemon that was
SIGKILLed or lost to a power cut leaves its pid behind, and a launcher that
believed that file would refuse to start for the rest of the machine's life --
so a pid nothing answers to is treated as absent *and* cleaned up on the spot.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config


def path() -> Path:
    """Where the pid lives. A function, not a constant, because
    :data:`mft.config.PID_FILE` is env-overridable and the tests move it."""
    return Path(config.PID_FILE)


def read() -> int | None:
    """The running daemon's pid, or None if it isn't running.

    A stale pid file (daemon was SIGKILLed, machine lost power) is treated as
    absent and cleaned up, so a crash never wedges the launcher.
    """
    try:
        pid = int(path().read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)  # signal 0 only checks that we may signal it
    except ProcessLookupError:
        clear()
        return None
    except PermissionError:
        # Someone else's process now owns that pid: not ours.
        clear()
        return None
    return pid


def write() -> None:
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(f"{os.getpid()}\n")


def clear() -> None:
    path().unlink(missing_ok=True)
