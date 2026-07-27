"""Keeping the board's roster honest against the operating system.

:mod:`mft.discover` knows *how* to ask the process table things. This module is
the part that decides *when*, on which thread, and what to do with the answer --
which is the daemon's job, and was four methods and three timers tangled through
its render loop.

Three clocks, deliberately different, because the three questions cost
different amounts:

* **adoption** runs at boot and on wake. It reads transcripts and joins them
  against the process table, so it is the expensive one and it runs twice a day.
* **the orphan sweep** runs every reap. It is one ``signal 0`` per session -- no
  subprocess, no file read -- which is exactly why it can afford to be the thing
  that stops a closed tab from holding a knob for the full TTL.
* **the census** runs on its own interval, on its own thread, because it spawns
  ``ps``. It buys the four things the free sweep cannot have: a pid for the
  records that never learned one, the tab back for a record that mistook its
  own terminal for a host process, the tty half of
  :func:`mft.discover.orphans`, and the count that retires a record naming
  nobody in a directory with no room for it (:func:`mft.discover.phantoms`).

Everything here is wrapped whole. Nothing about a roster is worth failing to
start over, and the board fills itself in from hook events either way -- this
only decides whether it does so now or on each session's next turn.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, NamedTuple, Optional

from . import config, discover
from .state import Session, SessionTable

log = logging.getLogger("mft.upkeep")


class Adopted(NamedTuple):
    """What one adoption pass found.

    Two lists rather than one because the caller wants both and they are not the
    same question: ``found`` is everything discovery recognised, which is what
    needs a context reading; ``new`` is only what was not on the board before,
    which is the part that is actually news.
    """

    found: list[Session]
    new: list[Session]

    @classmethod
    def nothing(cls) -> "Adopted":
        return cls([], [])


class Upkeep:
    """The three sweeps, over one :class:`~mft.state.SessionTable`.

    ``released`` is called with the records each sweep dropped, before their
    slots are reused -- it is how the tab strip gets handed back while the
    record that knows which tty it was on still exists. ``wake`` nudges the
    render loop when a background thread has changed what the board should say.
    """

    def __init__(
        self,
        table: SessionTable,
        *,
        released: Callable[[list[Session]], None],
        wake: Callable[[], None],
    ) -> None:
        self.table = table
        self._released = released
        self._wake = wake
        #: The census runs a subprocess, so it runs on its own thread; this is
        #: how a caller knows one is still out there rather than starting a
        #: second. See :meth:`sweep_census`.
        self._censusing = threading.Event()
        #: Whether the last adoption pass raised. Adoption is wrapped whole and
        #: on purpose -- see this module's docstring -- but "wrapped" turned out
        #: to mean "invisible": a discovery that threw on every wake for an
        #: afternoon left a plausible-looking board and said so only in a log
        #: nobody reads until something is already wrong. `mft.status` publishes
        #: this so the question "is the roster even being rebuilt" has an answer
        #: you can curl.
        self.discovery_failing = False

    # -- adoption -----------------------------------------------------------

    def adopt(self) -> Adopted:
        """Put the sessions that predate us on the board.

        Also runs on wake, where nearly everything it finds is already there --
        `discover.adopt` hands back the sessions it *recognised* as much as the
        ones it added, so only the difference is news.
        """
        if not config.DISCOVER_ON_START:
            return Adopted.nothing()
        known = {s.session_id for s in self.table.all()}
        try:
            found = discover.adopt(self.table, discover.discover())
        except Exception:
            self.discovery_failing = True
            log.exception("discovery failed; starting with an empty board")
            return Adopted.nothing()
        self.discovery_failing = False
        # Discovery reconstructs identities from the process table and writes
        # them onto sessions a hook may already have created, so this is exactly
        # the moment one tab can end up described twice.
        self.table.reconcile()
        self.table.compact()
        new = [s for s in found if s.session_id not in known]
        if new:
            log.info(
                "adopted %d running session%s: %s",
                len(new),
                "" if len(new) == 1 else "s",
                ", ".join(s.label for s in new),
            )
        return Adopted(found, new)

    # -- the free sweep -----------------------------------------------------

    def drop_orphans(self, taken: Optional[discover.Census] = None) -> None:
        """Take back the encoders whose Claude process has exited.

        The other half of :meth:`adopt`, on the reaper's clock rather than at
        boot, and the answer to the one failure the TTL is too slow for: you
        close every session and a knob stays lit, because the tab that owned it
        never got to fire `SessionEnd`. See :func:`mft.discover.orphans` for
        what it will and will not conclude.

        ``taken`` is the slower clock's extra evidence and is only ever passed
        by :meth:`sweep_census`.
        """
        if not config.ORPHAN_SWEEP:
            return
        try:
            gone = discover.orphans(self.table.all(), taken=taken)
        except Exception:
            log.exception("orphan sweep failed")
            return
        if not gone:
            return
        for session in gone:
            log.info(
                "encoder %d is a session %s (%s); releasing it",
                session.slot + 1,
                discover.epitaph(session, taken),
                session.label,
            )
        # Same order as the reaper's, and for the same reason: the tab is handed
        # back while the record that knows which tty it was on still exists.
        self._released(self.table.release_all(gone))

    def drop_phantoms(self, taken: discover.Census) -> None:
        """Take back the encoders that describe nobody at all.

        The census's own conclusion, and the only one here that is arithmetic
        rather than a question about a specific process: a record with no
        identity, in a directory where every running Claude is already claimed by
        a record that has one, is a wrong guess adoption made and cannot take
        back. See :func:`mft.discover.phantoms` for why the counting is safe.

        Always after :meth:`drop_orphans` on the same census -- a record still
        holding a dead pid would otherwise count as claiming its process, and the
        phantom next to it would live another half minute for it.
        """
        if not config.ORPHAN_SWEEP:
            return
        try:
            gone = discover.phantoms(self.table.all(), taken)
        except Exception:
            log.exception("phantom sweep failed")
            return
        if not gone:
            return
        for session in gone:
            log.info(
                "encoder %d describes no session in %s and every Claude there is "
                "already on the board (%s); releasing it",
                session.slot + 1,
                session.cwd or "?",
                session.label,
            )
        self._released(self.table.release_all(gone))

    # -- the costly one, off the render thread ------------------------------

    def sweep_census(self) -> None:
        """Ask the process table about the whole board, on a thread.

        A subprocess in the run loop is three or four dropped frames, and the
        answer is never urgent to the millisecond. One at a time: they are all
        asking the same question, and a second thread would only ask it of a
        table the first is already holding.
        """
        if not config.ORPHAN_SWEEP or self._censusing.is_set():
            return
        self._censusing.set()
        threading.Thread(target=self._census, name="mft-census", daemon=True).start()

    def _census(self) -> None:
        try:
            taken = discover.census()
            if taken is None:
                log.debug("could not read the process table; skipping the census")
                return
            learned = discover.learn_pids(self.table.all(), taken.procs)
            for session, proc in learned:
                log.info(
                    "encoder %d is pid %d (%s), read out of the process table",
                    session.slot + 1,
                    proc.pid,
                    session.label,
                )
            # After the pass above, so a record that just learned a pid is
            # already describing its tab and has nothing here to repair.
            relabelled = discover.relabel_hosts(self.table.all(), taken.procs)
            for session, proc in relabelled:
                log.info(
                    "encoder %d is the tab on %s, not a host process (%s)",
                    session.slot + 1,
                    proc.tty,
                    session.label,
                )
            if learned or relabelled:
                # A pid is an identity token like any other, and one written
                # straight onto a record is not in the table's index until this
                # runs -- which is also where it merges with any other record
                # already answering to that process. See `SessionTable.reconcile`.
                self.table.reconcile()
            self.drop_orphans(taken)
            # Last, and on purpose: the two sweeps above are what turn a record
            # with nothing on it into a record with a pid, and what removes the
            # ones whose process is already gone. Whatever is still nameless
            # after both has had every chance to say who it is.
            self.drop_phantoms(taken)
        except Exception:
            log.exception("census failed")
        finally:
            self._censusing.clear()
            self._wake()

    # -- the plain TTL reaper, for symmetry ---------------------------------

    def reap(self, now: Optional[float] = None) -> None:
        """Drop what timed out, then re-check the one-encoder-per-tab invariant.

        Grouped with the sweeps above because it is the same shape -- decide who
        is gone, hand their tabs back, compact -- even though it needs nothing
        from the operating system to decide it. See `SessionTable.reconcile` for
        why the invariant gets re-checked from outside on this clock: what it
        catches is a board that looks entirely plausible and is quietly wrong.
        """
        self._released(self.table.reap())
        self.drop_orphans()
        self.table.reconcile(now)
