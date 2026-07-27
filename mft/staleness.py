"""Whether the code this process is running is still the code on disk.

A daemon is a long-lived process that imported its modules once, and this one is
developed while it runs -- so the ordinary way to break it is not to write a bug
but to save a file. Python holds the image it imported; editing `discover.py`
afterwards changes nothing for the process until it restarts, and nothing in the
running daemon has any reason to notice.

That failure is quiet in a specific and expensive way. A module saved
half-finished -- a helper called a few lines above where it is about to be
defined -- imports cleanly at the moment it is loaded and raises `NameError`
only when the path that calls it runs. If that path is wrapped, and the ones
worth wrapping are (see :mod:`mft.upkeep`, where a failed adoption is
deliberately not fatal), the daemon goes on looking healthy while one of its
jobs is dead. It cost an afternoon of missing encoders once, which is the reason
this file exists.

So: take the mtimes at boot, compare them later, say so. On the same shelf as
:mod:`mft.pidfile` -- a small, dull fact about the process rather than anything
to do with sessions or paint.

Deliberately **not** a reloader. Re-importing a module under a running render
loop swaps the code out from under objects the old one built, and half the point
of :mod:`mft.state` is that a `Session` created at boot is the same object at
midnight. The remedy for stale code is a restart; the job here is only to tell
you that you need one. That is invariant 6 pointed at the daemon itself -- a
job that lies quietly for an hour is worse than a fact you have to act on.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("mft.staleness")


def sources() -> dict[str, str]:
    """Every module of ours currently imported, and the file it came from.

    Ours only. The standard library and site-packages are not edited mid-run,
    and walking them would turn a cheap check into a few thousand stats.
    """
    found: dict[str, str] = {}
    prefix = f"{__package__}."
    for name, module in list(sys.modules.items()):
        if name != __package__ and not name.startswith(prefix):
            continue
        path = getattr(module, "__file__", "") or ""
        if path.endswith(".py"):
            found[name] = path
    return found


def snapshot() -> dict[str, float]:
    """What every loaded module's source looked like when we loaded it.

    Taken at boot, a moment after the imports themselves, so a file saved in
    between reads as fresh. That window is milliseconds wide and the cost of
    losing it is one warning we never print, not one we print wrongly.
    """
    marks: dict[str, float] = {}
    for name, path in sources().items():
        try:
            marks[name] = os.stat(path).st_mtime
        except OSError:
            # A module whose file we cannot stat is one we also cannot compare.
            # Leaving it out of the baseline says "no opinion", which is the
            # honest answer and keeps it from being reported as changed later.
            continue
    return marks


def changed(baseline: dict[str, float]) -> list[str]:
    """The modules whose source has been written since `baseline` was taken.

    Names, sorted, so the answer is stable enough to log and to assert against.
    A module that has appeared since the snapshot is not news -- whenever it was
    imported, it was imported from the file that is on disk now -- so only names
    already in `baseline` can count as stale.
    """
    current = sources()
    stale: list[str] = []
    for name, mark in baseline.items():
        path = current.get(name)
        if not path:
            continue
        try:
            if os.stat(path).st_mtime > mark:
                stale.append(name)
        except OSError:
            continue
    return sorted(stale)


def report(baseline: dict[str, float], when: str) -> list[str]:
    """:func:`changed`, plus the one line that makes it actionable.

    Never raises: this is a diagnostic, and a diagnostic that can take the
    daemon down with it is worse than no diagnostic at all.
    """
    try:
        stale = changed(baseline)
    except Exception:
        log.exception("could not check whether our own source has moved")
        return []
    if stale:
        log.warning(
            "%s: this process is older than the source of %s -- restart the "
            "daemon (--stop, then start it again). Until then it runs the code "
            "it booted with, including any job of it that is failing quietly.",
            when,
            ", ".join(stale),
        )
    return stale
