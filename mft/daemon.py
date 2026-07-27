"""The visualizer: one object owns the MIDI port, folds hook events into a
session table, and renders that table onto the Twister at a fixed frame rate.

    python -m mft.daemon

What is left in this file is deliberately only two things: **the Visualizer and
its render loop.** Everything that is not about deciding what the board says now
lives beside it and is named after its own job --

* :mod:`mft.httpd` -- the socket the hooks arrive on, and the 204 they always get
* :mod:`mft.cli` -- argv, signals, and the order of a clean shutdown
* :mod:`mft.pidfile` -- whether a daemon is already running
* :mod:`mft.status` -- what ``GET /status`` says
* :mod:`mft.tab` -- the tab strip, including when to write to it
* :mod:`mft.banks` -- which sixteen encoders the front panel is showing
* :mod:`mft.upkeep` -- keeping the roster honest against the process table

so that opening this file puts you in front of the frame, not in front of an
argument parser.

The Visualizer is still the place where the threads meet: hook events land on
HTTP threads, encoder presses on the MIDI input thread, sleep and wake on a
notification thread, and the render loop reads all of it thirty times a second.
Nothing here may block a hook and nothing may die on a bad frame; both rules are
enforced at the boundaries, in :meth:`handle_event` and :meth:`paint`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import (
    attention as attention_mod,
    banks as banks_mod,
    board as board_mod,
    config,
    context as context_mod,
    discover as discover_mod,
    focus as focus_mod,
    overlays as overlays_mod,
    power as power_mod,
    render as render_mod,
    staleness as staleness_mod,
    status as status_mod,
    tab as tab_mod,
    twister as twister_mod,
    upkeep as upkeep_mod,
)
from .events import (
    EFFECT_BANNER,
    EFFECT_CLEAR,
    EFFECT_COMPACT_END,
    EFFECT_COMPACT_START,
    EFFECT_SPAWN,
    apply_event,
)
from .identity import merge_terminal
from .state import Session, SessionTable

log = logging.getLogger("mft.daemon")

#: Events about a subagent rather than about a session. They are routed to the
#: parent and are never allowed to create a session of their own.
SUBAGENT_EVENTS = frozenset({"SubagentStart", "SubagentStop"})

#: Events that can change *who belongs in the live block*, and so are the only
#: ones worth squeezing the board back up to the top-left for. Everything else
#: -- a tool call, a notification, a turn ending -- moves nobody's encoder, and
#: compaction is a sort plus two dict rebuilds behind the table's lock, taken
#: while the render loop wants that same lock.
COMPACTING_EVENTS = frozenset({"SessionStart", "SessionEnd"})

#: Events that may only ever *update* a session, never bring one into being.
#:
#: `SessionEnd` is one because a `/clear` retires a session id while its tab
#: keeps the encoder: the replacement id has often already been adopted by the
#: time the old id's `SessionEnd` lands, and it carries no terminal, so `ensure`
#: would answer that stale id by lighting a *second* encoder for a tab that is
#: already on the board. Nothing about an ending wants a slot allocated for it
#: in the first place, whatever the order the pair arrives in.
UPDATE_ONLY_EVENTS = frozenset({"SessionEnd"})

#: A frame that fails fails every frame, so the traceback is rate-limited to
#: roughly one a minute rather than 30 a second.
PAINT_ERROR_LOG_SECONDS = 60.0

#: How often the reaper sweeps for sessions that ended or went silent.
REAP_INTERVAL_SECONDS = 5.0


class Visualizer:
    """The board: a session table, the hardware, and the loop between them."""

    def __init__(self, device: twister_mod.Twister) -> None:
        self.device = device
        self.table = SessionTable()
        #: The board's second display, and the third: see the modules.
        self.tabs = tab_mod.TabStrip(self.table)
        self.banks = banks_mod.BankFollower(device)
        self.upkeep = upkeep_mod.Upkeep(
            self.table, released=self.tabs.restore, wake=self._wake_loop
        )
        self._stop = threading.Event()
        #: What our own source looked like when we imported it, and the modules
        #: that have been written since. Checked on wake because that is both a
        #: natural pause and, on this machine, the moment a rebuilt roster is
        #: most load-bearing. See :mod:`mft.staleness`.
        self._source_marks = staleness_mod.snapshot()
        self._stale: list[str] = []
        #: Set by anything that changes what the board should say, so the render
        #: loop can sleep long between static frames without an event having to
        #: wait out that sleep. See :meth:`run`.
        self._wake = threading.Event()
        #: How lit the board is, given how long since anyone was here. Touched by
        #: every hook event and every press; read once per frame.
        self._sleep = board_mod.Sleep(time.monotonic())
        self._press_started: dict[int, float] = {}
        #: What each held encoder was aimed at when it went down. Resolved once,
        #: on the press, because a subagent's knob is only its parent's for as
        #: long as the pile stands: a subagent that finishes mid-press must
        #: still raise the tab you were reaching for, not nothing at all.
        self._press_targets: dict[int, Session] = {}
        self._overlays: list[board_mod.Overlay] = []
        #: Keyed by session, not slot: the board compacts under sessions that
        #: end, so a slot is not a stable handle for anything long-lived.
        self._compactions: dict[str, overlays_mod.CompactOverlay] = {}
        #: Per-slot, not a single overlay: pressing a second encoder before
        #: releasing the first would otherwise strand the first one on the
        #: board with nothing left to release it.
        self._peeks: dict[int, overlays_mod.PeekOverlay] = {}
        #: The waiting animation, held onto so the render loop can retire it the
        #: moment there is a live session to retire it for.
        self._waiting: overlays_mod.WaitingOverlay | None = None
        #: When to start it, or `None` once that decision has been made. The
        #: render loop holds off for a couple of frames after the boot word and
        #: only starts the animation if the board is still genuinely empty; see
        #: :meth:`_check_waiting`.
        self._waiting_due: float | None = None
        self._lock = threading.Lock()
        #: The session a focus attempt is currently running for, so a second
        #: press on the same knob doesn't stack another AppleScript behind it.
        self._focus_lock = threading.Lock()
        self._focusing = ""
        #: When the render loop last logged a dropped frame; see :meth:`paint`.
        self._last_paint_error = float("-inf")
        #: When every ring was last restated regardless of the de-dup cache.
        #: Negative infinity so the very first frame is a refresh.
        self._last_ring_refresh = float("-inf")
        #: The cells the last frame composed, to tell a board that is animating
        #: from one that is merely lit. `None` until the first frame.
        self._last_cells: list[render_mod.Cell] | None = None
        #: Set while the machine is asleep, so no frame relights a board that
        #: was just blacked out for it. See :meth:`on_system_sleep`.
        self._suspended = threading.Event()
        #: Serialises a frame against the blackout, which is the one write that
        #: has to be the last thing on the wire. Uncontended at 30Hz.
        self._paint_lock = threading.Lock()
        #: Sleep and wake, all three detectors; see :mod:`mft.power`.
        self._power = power_mod.PowerWatcher(self.on_system_sleep, self.on_system_wake)
        self._wake_clock = power_mod.WakeClock()
        #: The one that carries the weight: a poll, so it cannot be missed, and
        #: the only one that can tell a dark wake from someone opening the lid.
        self._display = power_mod.DisplayPower()
        self._last_display_poll = float("-inf")
        #: Debounces the detectors reporting one wake.
        self._last_system_wake = float("-inf")
        #: When a dead MIDI port was last retried; see :meth:`_check_port`.
        self._last_port_retry = float("-inf")
        self._last_census = float("-inf")
        #: Which tab is in front of you; see :mod:`mft.attention`.
        self._attention = attention_mod.AttentionWatcher(wake=self._wake_loop)
        #: The encoder that tab is on, recomposed every frame, and the session
        #: id it belonged to when it arrived there. The id is what makes arrival
        #: an *edge* -- a slot is not a stable handle for a session, and the
        #: pulse must fire once when you switch to a tab rather than every frame
        #: you spend in it.
        self._focused_slot: Optional[int] = None
        self._focused_id = ""

    def _wake_loop(self) -> None:
        """Bring the render loop back to full rate. Handed to collaborators that
        change the board from a thread of their own."""
        self._wake.set()

    # -- overlays -----------------------------------------------------------

    def push_overlay(self, overlay: board_mod.Overlay) -> None:
        with self._lock:
            self._overlays.append(overlay)
        # Every overlay is an animation, so the loop has to be at full rate for
        # it whatever the board underneath was doing.
        self._wake.set()

    def _live_overlays(self, now: float) -> list[board_mod.Overlay]:
        with self._lock:
            self._overlays = [o for o in self._overlays if not o.done(now)]
            return list(self._overlays)

    def _apply_effects(self, session: Session, effects: list[str]) -> None:
        now = time.monotonic()
        for effect in effects:
            if effect == EFFECT_COMPACT_START:
                overlay = overlays_mod.CompactOverlay(session, now)
                self._compactions[session.session_id] = overlay
                self.push_overlay(overlay)
            elif effect == EFFECT_COMPACT_END:
                overlay = self._compactions.pop(session.session_id, None)
                if overlay is not None:
                    overlay.finish(now)
            elif effect == EFFECT_SPAWN:
                if config.SPAWN_ANIMATION:
                    self.push_overlay(overlays_mod.SpawnOverlay(session, now))
            elif effect == EFFECT_CLEAR:
                if config.CLEAR_ANIMATION:
                    self.push_overlay(overlays_mod.ClearOverlay(session, now))
            elif effect.startswith(EFFECT_BANNER):
                word = effect[len(EFFECT_BANNER) :]
                self.push_overlay(
                    overlays_mod.TextOverlay(
                        word,
                        now,
                        color=config.BANNER_COLOR,
                        bank=board_mod.bank_of(session.slot),
                        hold=config.BANNER_SECONDS / max(1, len(word)),
                    )
                )

    # -- hook events --------------------------------------------------------

    def handle_event(self, event: dict) -> dict:
        """Apply one hook event, and wake the render loop for it.

        The wake goes in a `finally` around the whole thing rather than at each
        of the returns below: an event that decided to change nothing has cost
        one early frame, while an event whose wake was forgotten leaves the
        board a quarter-second behind the agent it is supposed to be reporting
        on. Only one of those is worth being careful about.

        The sleep timer is reset in the same place and for the same reason. Note
        that it counts *any* event, including ones dropped as unknown: something
        out there is running, which is the only question sleep is asking.
        """
        try:
            return self._apply_hook_event(event)
        finally:
            self._sleep.touch(time.monotonic())
            self._wake.set()

    def _apply_hook_event(self, event: dict) -> dict:
        session_id = event.get("session_id")
        if not session_id:
            return {"ok": False, "error": "missing session_id"}

        name = event.get("hook_event_name", "")
        if name in SUBAGENT_EVENTS:
            return self._apply_subagent_event(name, session_id, event)

        if name in UPDATE_ONLY_EVENTS and self.table.get(session_id) is None:
            log.debug("%s for a session we don't have (%s)", name, session_id[:8])
            return {"ok": True}

        # Whatever this event could tell us about the terminal it came from --
        # the full environment from `register_session.py`, the tab identity from
        # `notify.sh`'s header, nothing at all from an `http` hook. That identity
        # is what owns the encoder, so it goes in before the slot is chosen, not
        # after.
        terminal = event.get("terminal")
        terminal = terminal if isinstance(terminal, dict) and terminal else None

        session = self.table.ensure(session_id, event.get("cwd", ""), terminal)
        if session is None:
            return {"ok": False, "error": "no free encoder"}
        if terminal:
            # Merged, not assigned: the thin identity on a tool-call event would
            # otherwise overwrite the full environment the focus adapters need.
            session.terminal = merge_terminal(session.terminal, terminal)

        effects = self._fold(session, event)
        # A session ending (or restarting) is the only thing that changes who
        # belongs in the live block, so this is where the board is squeezed back
        # up to the top-left -- and only for those events, since for the rest it
        # is a no-op that still does all the work. See COMPACTING_EVENTS.
        if name in COMPACTING_EVENTS:
            self.table.compact()
        now = time.monotonic()
        context_mod.refresh(session, now)
        self.tabs.refresh_title(session, now)
        if effects:
            self._apply_effects(session, effects)
        return {"ok": True, "slot": session.slot + 1, "state": session.state}

    def _apply_subagent_event(self, name: str, session_id: str, event: dict) -> dict:
        """A subagent owns no encoder, so its events must never claim one.

        Whether these payloads carry the parent's session id or the subagent's
        own is undocumented, and `ensure` would answer the second case by
        lighting a fresh encoder from the top-left -- a subagent rendered as a
        session, which is the one thing the board is not allowed to do. So: find
        the parent or drop the event.
        """
        session = self.table.find_parent(session_id, event.get("cwd", ""))
        if session is None:
            log.debug("%s with no known parent (%s)", name, session_id[:8])
            return {"ok": True}
        self._fold(session, event)
        return {"ok": True, "slot": session.slot + 1, "state": session.state}

    def _fold(self, session: Session, event: dict) -> list[str]:
        """`apply_event`, plus the one thing worth saying out loud about it."""
        before = session.subagents
        effects = apply_event(session, event)
        if session.subagents != before:
            log.info("%s: %d subagent(s) in flight", session.short_id, session.subagents)
        return effects

    def status(self) -> dict:
        now = time.monotonic()
        return status_mod.payload(
            self.table.all(),
            now,
            device=type(self.device).__name__,
            sleep=self._sleep.gain(now),
            suspended=self._suspended.is_set(),
            port_failing=self.device.failing(),
            bank=self.banks.current,
            focused=self._focused_slot,
            focused_app=self._attention.app,
            discovery_failing=self.upkeep.discovery_failing,
            stale=self._stale,
        )

    # -- encoder input ------------------------------------------------------

    def on_midi(self, msg) -> None:
        if msg.type != "control_change":
            return
        # Any CC at all, turn or press, is a hand on the hardware -- the one
        # activity sleep can see that has nothing to do with any agent, and if
        # the board is dark it is very likely the whole reason for it. Waking
        # the loop is right for a turn on its own terms too: the ring the knob
        # lit locally is undone by the next frame, and "next" should mean now.
        now = time.monotonic()
        self._sleep.touch(now)
        self._wake.set()
        if msg.channel == config.CH_SWITCH:
            self._on_switch(msg.control, msg.value)
        elif msg.channel == config.CH_SYSTEM:
            self.banks.chose(msg.control, msg.value, now)
        # A turn is otherwise deliberately ignored: the board is a display, not
        # a control surface -- see :meth:`Twister.forget_rings`.

    def _press_target(self, slot: int) -> Optional[Session]:
        """The session a press on this encoder is aimed at, if any.

        Usually the one that owns the slot. Failing that it may be a subagent's
        knob, and a subagent is not a thing you can raise -- it runs inside its
        parent's terminal, so the parent is both the only answer and the right
        one. The pile is recomputed here rather than remembered from the last
        frame: :func:`board.compose` is pure and keeps nothing, and this runs
        once per press rather than thirty times a second.
        """
        session = self.table.by_slot(slot)
        if session is not None:
            return session
        if not (config.SUBAGENT_STACK and config.SUBAGENT_PRESS):
            return None
        sessions = self.table.all()
        claimed = {s.slot for s in sessions if 0 <= s.slot < config.SLOT_COUNT}
        return board_mod.subagent_owners(sessions, claimed).get(slot)

    def _on_switch(self, slot: int, value: int) -> None:
        session = self._press_target(slot)
        now = time.monotonic()
        # The wake for this landed in :meth:`on_midi`, and it matters here: a
        # press forgives an unattended session, and a board full of nothing but
        # unattended sessions is exactly the static one the loop will have
        # slowed down for. The dimming has to land on the press, not a quarter
        # second after it.
        if value >= 64:  # press down
            self._press_started[slot] = now
            if session is not None:
                self._press_targets[slot] = session
                # Hold to peek. The overlay goes up now but only paints once the
                # press has lasted HOLD_SECONDS, and it comes down on release --
                # a spring-loaded modal view, not a mode you can get stuck in.
                # Held on a subagent's knob it paints the *parent's* bank, which
                # is the honest answer to what you were asking by holding it.
                peek = overlays_mod.PeekOverlay(session, now)
                self._peeks[slot] = peek
                self.push_overlay(peek)
            return

        session = self._press_targets.pop(slot, None)
        started = self._press_started.pop(slot, None)
        peek = self._peeks.pop(slot, None)
        if peek is not None:
            peek.release()
        if started is None:
            return
        held = now - started
        if session is None:
            log.debug("press on unclaimed encoder %d", slot + 1)
            return

        if held >= config.HOLD_SECONDS:
            return  # the hold was the gesture; releasing just ends the peek
        session.attended()
        self.focus_session(session)

    # -- press to focus -----------------------------------------------------

    def _claimed_pids(self, exclude: Session) -> frozenset[str]:
        """Process ids other sessions have already been pinned to.

        Two Claudes in one directory are indistinguishable from the process
        table alone -- unless one of them arrived with its environment, in
        which case its process is spoken for and the other one is no longer
        ambiguous.
        """
        return frozenset(
            str(s.terminal.get("pid"))
            for s in self.table.all()
            if s is not exclude and s.terminal.get("pid")
        )

    def resolve_terminal(self, session: Session) -> dict:
        """The best terminal context we can get for this session, right now.

        A session only learns its terminal from the SessionStart command hook,
        which fires once. Anything that misses it -- a session older than the
        daemon, a daemon restarted underneath a running session, a hook that
        fired while the daemon was down -- can never be told later, so the
        press goes and reads it out of the live process table instead. Fresh
        values win over stored ones: a recorded tty is only as good as the
        process that had it.
        """
        stored = dict(session.terminal or {})
        if focus_mod.precise(stored):
            return stored
        try:
            found = discover_mod.resolve_terminal(
                session.cwd, stored.get("pid", ""), self._claimed_pids(session)
            )
        except Exception:
            log.exception("terminal lookup failed for %s", session.label)
            return stored
        if not found:
            return stored
        merged = {**stored, **found}
        if merged != stored:
            log.info("recovered terminal for %s from the process table", session.label)
            session.terminal = merged
        return merged

    def focus_session(self, session: Session) -> None:
        """Raise the session's tab, off the MIDI thread.

        Serialised: AppleScript against two terminals at once is a good way to
        get one timeout and one raised window, and a second press while the
        first is still working is a repeat, not a new destination.
        """
        with self._focus_lock:
            if self._focusing == session.session_id:
                log.debug("already focusing %s", session.label)
                return
            self._focusing = session.session_id

        def run() -> None:
            try:
                log.info("focusing %s", session.label)
                if not focus_mod.focus(self.resolve_terminal(session)):
                    log.warning("could not focus %s", session.label)
            except Exception:
                log.exception("focus failed for %s", session.label)
            finally:
                with self._focus_lock:
                    if self._focusing == session.session_id:
                        self._focusing = ""

        threading.Thread(target=run, name="mft-focus", daemon=True).start()

    # -- the empty board ----------------------------------------------------

    def _live_session(self) -> bool:
        """Is there a Claude on the board right now?

        A session that has already ended does not count: its encoder is only
        lingering so you can see how it finished, and the board is idle again in
        every sense that matters to the waiting animation.
        """
        return any(s.ended_at is None for s in self.table.all())

    def _check_waiting(self, now: float) -> None:
        """Start the waiting animation, or retire it once a Claude shows up.

        The start is deferred by a couple of frames and re-checked every frame
        until it fires. Discovery has already run by the time the boot word
        ends, but a session that started *during* the word has only a hook in
        flight to announce it -- and a waiting animation that appears for two
        frames on a board that was never empty reads as a glitch, where a tenth
        of a second of black reads as nothing at all.
        """
        if self._waiting_due is not None:
            if self._live_session():
                self._waiting_due = None  # never empty; there was nothing to wait for
            elif now >= self._waiting_due:
                self._waiting_due = None
                self._waiting = overlays_mod.WaitingOverlay(now)
                self.push_overlay(self._waiting)
            return

        waiting = self._waiting
        if waiting is None:
            return
        if waiting.done(now):
            self._waiting = None
        elif self._live_session():
            waiting.dismiss(now)

    # -- painting -----------------------------------------------------------

    def paint(
        self, now: float, overlays: list[board_mod.Overlay] | None = None
    ) -> bool:
        """Compose one frame, push it to the hardware, and say whether anything
        on the board actually moved since the last one.

        The answer is what :meth:`run` paces itself off. It compares composed
        cells rather than bytes sent, because the de-dup cache makes "we wrote
        nothing" true of an animation whose values happen to repeat as well as
        of a board that is genuinely still. A :class:`~mft.render.Cell` is a
        frozen dataclass and :func:`~mft.render.render` is a pure function of
        (session, clock), so equal cells really do mean an unchanged frame.

        Never raises. A display that dies on one bad frame takes the whole
        daemon's board with it and leaves the last frame frozen on the desk,
        which is worse than a wrong frame -- and every hook event is already
        wrapped this way on the HTTP side. Logged at most once a minute, because
        anything that fails here fails 30 times a second. A dropped frame counts
        as movement: whatever went wrong, this is not the moment to go quiet.
        """
        # A sleeping machine gets no frames at all. Checked before the compose
        # rather than before the write, because the point of a dark board is
        # that nothing is deciding what to put on it either.
        if self._suspended.is_set():
            return False
        try:
            if now - self._last_ring_refresh >= config.RING_REFRESH_SECONDS:
                # Periodically, not every frame: a knob turned by hand lights
                # its own ring and the cache would believe that ring is already
                # where we want it, but undoing that only has to look instant.
                self._last_ring_refresh = now
                self.device.forget_rings()
            cells = board_mod.compose(
                self.table.all(),
                now,
                self._live_overlays(now) if overlays is None else overlays,
                sleep=self._sleep.gain(now),
                focused=self._focused_slot,
            )
            # Under the lock, and re-checked inside it: a sleep notification
            # that arrived while this frame was composing must not have its
            # blackout overwritten by the frame that was already in flight.
            with self._paint_lock:
                if self._suspended.is_set():
                    return False
                for slot, cell in enumerate(cells):
                    self.device.write(slot, cell)
            changed = cells != self._last_cells
            self._last_cells = cells
            return changed
        except Exception:
            if now - self._last_paint_error > PAINT_ERROR_LOG_SECONDS:
                self._last_paint_error = now
                log.exception("frame dropped")
            return True

    def animate(self, overlay: board_mod.Overlay) -> None:
        """Run one overlay to completion, blocking. Used for boot and shutdown,
        where there is nothing else to render anyway."""
        period = 1.0 / config.FPS
        while not overlay.done(time.monotonic()):
            self.paint(time.monotonic(), [overlay])
            if self._stop.wait(period):
                return

    # -- sleep and wake -----------------------------------------------------

    def darken(self, reason: str) -> None:
        """Put the board out, and hold it out. Idempotent.

        The flag goes up *before* the blackout and under the same lock a frame
        holds, or the render loop gets one more frame in afterwards and the last
        word on the board is a lit one -- which is the entire bug this exists to
        prevent, and it would show up only as "sometimes it stays on overnight".

        Not conditional on there being anything worth showing: an empty board
        still breathes (`config.AMBIENT`), and a desk lamp is exactly what this
        must not be at 3am.
        """
        if self._suspended.is_set():
            return
        with self._paint_lock:
            self._suspended.set()
            try:
                self.device.blackout()
            except Exception:
                log.exception("could not darken the board")
        log.info("%s; board dark", reason)

    def relight(self, reason: str) -> None:
        """Hand the board back to the render loop. Idempotent.

        The de-dup cache goes first, since a repaint the cache suppresses is
        exactly as dark as no repaint at all.

        What this deliberately does *not* do is touch the sleep timer. A board
        that relights itself to full brightness because a screen came on is
        worse than one that comes back at the dim level it went down at -- and
        after a suspend that level is exactly where you left it, the clock it
        runs on having frozen with the machine. The first hook event or encoder
        press brings it up, as it always does.
        """
        if not self._suspended.is_set():
            return
        log.info("%s; repainting", reason)
        self._suspended.clear()
        self._forget_board()

    def _forget_board(self) -> None:
        """Drop every belief about what the device is currently showing.

        Separate from :meth:`relight` because a wake needs it whether or not the
        board was ever darkened: a suspend can leave the hardware holding
        something other than what we last sent it, and the de-dup cache would
        go on suppressing exactly the writes that would fix that.
        """
        self.device.forget_all()
        self._last_cells = None
        self._wake.set()

    def on_system_sleep(self) -> None:
        """The notification path into :meth:`darken`, with the machine waiting.

        Runs on the notification thread (:mod:`mft.power`) and does the one
        thing that cannot be done after the fact. In practice the display went
        dark a moment before this and :meth:`_check_display` already blacked the
        board out; this is what covers the case where it did not -- a lid closed
        on a lit screen, which suspends the machine without an idle timer ever
        running down.
        """
        if not config.SLEEP_BLACKOUT:
            return
        self.darken("system sleeping")

    def on_system_wake(self, source: str = "notification") -> None:
        """Re-check the table, and relight only if anyone could see it.

        Idempotent and debounced, because the detectors fire for a healthy wake
        by design.

        The board comes back only when the *display* is on. A dark wake -- Power
        Nap, a backup, a network arrival -- looks exactly like an opened lid to
        both of the other detectors, and a Mac in standby has one of these every
        fifteen minutes all night. Relighting for each of them is how a board
        stays lit through nine consecutive suspends. The screen is the thing
        that says a person is there; when it comes on, :meth:`_check_display`
        relights on the next poll.
        """
        now = time.monotonic()
        with self._lock:
            if now - self._last_system_wake < config.WAKE_DEBOUNCE_SECONDS:
                return
            self._last_system_wake = now
        if config.DISPLAY_BLACKOUT and self._display.asleep:
            log.info("system awake (%s) but the display is off; board stays dark", source)
        else:
            self.relight(f"system awake ({source})")
            # Unconditionally, and not only when that relight did something: a
            # board that was never darkened still came through a suspend, and
            # the device may not be holding what we think it is.
            self._forget_board()
        # Before adoption rather than after it, so that when adoption does fail
        # the reason is already the line above the traceback: nine times in ten
        # a job that broke across a suspend broke because the file it lives in
        # was saved while we were asleep.
        self._stale = staleness_mod.report(self._source_marks, f"awake ({source})")
        if config.WAKE_REDISCOVER:
            self.adopt_running_sessions(awaken=False)
        # Explicitly, rather than waiting for the interval to come round: the
        # clock the census runs on stopped with the machine, so a lid closed for
        # a weekend is half a minute old from in here -- and a suspend is
        # precisely when the tabs on the board got closed without telling us.
        self.upkeep.sweep_census()
        self._wake.set()

    def _check_display(self, now: float) -> None:
        """Follow the screen. Polled, on its own interval.

        The load-bearing detector, and the reason it is a poll: there is no
        notification here to miss, drop, or stop being delivered. Whatever the
        other two do or fail to do, a board sitting lit in front of a dark
        screen is one second from being noticed.
        """
        if not config.DISPLAY_BLACKOUT:
            return
        if now - self._last_display_poll < config.DISPLAY_POLL_SECONDS:
            return
        self._last_display_poll = now
        asleep = self._display.poll()
        if asleep is None:
            return
        if asleep:
            self.darken("display asleep")
        else:
            # The census too, and for the same reason the wake path runs one:
            # a screen that has been off for a while is a window in which tabs
            # got closed, and the board should not come back naming them.
            self.relight("display awake")
            self.upkeep.sweep_census()

    def _check_wake_clock(self) -> None:
        """The fallback detector, polled once a frame.

        Cheap enough to sit in the loop -- two counter reads and a subtract --
        and it is the only thing that notices a wake if the IOKit registration
        never attached. It cannot report a sleep, so a machine relying on this
        alone dims late rather than early: the board stays lit through the
        suspend and comes back correct.
        """
        slept = self._wake_clock.poll(config.WAKE_MIN_SLEEP_SECONDS)
        if slept:
            log.info("clock says the machine slept for %.0fs", slept)
            self.on_system_wake(source=f"{slept:.0f}s clock gap")

    def _check_port(self, now: float) -> None:
        """Reopen a MIDI port that has stopped accepting writes.

        A sleep can leave the USB endpoint invalid without closing it: every
        send raises and the de-dup cache -- which believes the device already
        holds what it was last sent -- would go on suppressing the writes that
        would put it back even once the hardware recovered. So this is driven by
        failed writes rather than by a wake, which also makes it the answer to a
        cable pulled out and pushed back in an hour later.
        """
        if not self.device.failing() or self._suspended.is_set():
            return
        if now - self._last_port_retry < config.PORT_RETRY_SECONDS:
            return
        self._last_port_retry = now
        if self.device.reopen():
            self._last_cells = None

    def _check_attention(self, now: float) -> None:
        """Find the encoder whose tab is in front of you, and mark the arrival.

        The finding is :mod:`mft.attention`; what is left here is the part that
        needs the board, and the part that is not pure. Arriving in a tab is the
        clearest statement of presence this daemon ever gets -- clearer than a
        hook, which only says an agent is busy, and clearer than a knob press,
        which is the same statement made with your hand -- so it does what a
        press does: forgives the debt, clears the alert, and resets the clock the
        sleep is measured on.

        Only on the edge. Sitting in a tab is not a standing amnesty: a prompt
        that arrives while you are looking at it is one you are ignoring, and its
        encoder is right to start asking.
        """
        if not config.ATTENTION_FOLLOW:
            return
        sessions = self.table.all()
        try:
            self._attention.poll(now, sessions)
            session = self._attention.focused(sessions)
        except Exception:
            log.exception("could not tell which tab is in front")
            return

        self._focused_slot = None if session is None else session.slot
        arrived = session.session_id if session is not None else ""
        if arrived and arrived != self._focused_id:
            log.debug("encoder %d is the tab in front (%s)", session.slot + 1, session.label)
            if config.ATTENTION_ATTENDS:
                session.attended()
                self._sleep.touch(now)
            if config.ATTENTION_PULSE:
                self.push_overlay(overlays_mod.FocusOverlay(session, now))
        self._focused_id = arrived

    # -- the roster ---------------------------------------------------------

    def adopt_running_sessions(self, awaken: bool = True) -> None:
        """Put the sessions that predate us on the board, and read their gauges.

        The finding is :meth:`mft.upkeep.Upkeep.adopt`; what is left here is the
        part that needs the board -- a context reading for every session it
        recognised, and the sleep timer for the ones that are news. `awaken` is
        what the wake path turns off: see :meth:`on_system_wake` for why a
        machine powering on is not evidence that anyone is at the desk.
        """
        found, new = self.upkeep.adopt()
        now = time.monotonic()
        for session in found:
            context_mod.refresh(session, now)
        if new and awaken:
            # These sessions have been running without us and none of them has
            # sent us an event yet. A daemon started onto a live board starts
            # awake, and its first half hour is measured from now.
            self._sleep.touch(now)

    # -- render loop --------------------------------------------------------

    def _boot(self) -> None:
        """Everything that happens once, before the first ordinary frame."""
        self.device.clear_all()
        self.device.listen(self.on_midi)
        self.device.start_clock()
        # Before anything that blocks: the boot word takes a couple of seconds
        # and a lid closed during it would otherwise leave that word lit on the
        # desk until morning. Failing to attach costs the sleep half only -- the
        # clock fallback in the loop still catches the wake.
        if not self._power.start():
            log.info("no sleep notifications; wake will be noticed from the clock")
        # Before the boot word rather than after it: the waiting animation that
        # follows is for an empty board, and an encoder that lights up halfway
        # through it reads as a session that just started.
        self.adopt_running_sessions()
        if config.BOOT_ANIMATION:
            # The exit gesture backwards, and then the word if it is switched
            # on: the board comes up whole, unwraps itself from the centre out
            # along the same spiral the shutdown closes on, and hands a black
            # board to the C of CLAUDE. Both block, and both are white on a dark
            # board. With the word off the unwrap is the whole of boot.
            if config.BOOT_UNWRAP_ANIMATION:
                self.animate(overlays_mod.UnwrapOverlay(time.monotonic()))
            if config.BOOT_WORD_ANIMATION:
                self.animate(
                    overlays_mod.TextOverlay(config.BOOT_WORD, time.monotonic())
                )
            # The word blocks; the waiting animation does not -- and it does not
            # start here either. It is armed, and the render loop starts it a
            # couple of frames later if the board is still empty by then, then
            # keeps running it inside the normal loop, because the whole point
            # is that it yields to the first real session -- which it can only
            # do if that session is being rendered too.
            self._waiting_due = time.monotonic() + config.WAITING_START_DELAY_SECONDS

        # After the boot word, not before it: the word blocks for a couple of
        # seconds and the clock the sleep timer runs on does not stop for it.
        self._sleep.touch(time.monotonic())

    def run(self) -> None:
        self._boot()
        active = 1.0 / config.FPS
        idle = 1.0 / config.IDLE_FPS
        if config.ATTENTION_FOLLOW:
            # A still board is exactly the board you alt-tab *into* -- an idle
            # session, a finished one, a prompt sitting there waiting -- so the
            # idle rate is the marker's real latency, and at 1Hz it is a second
            # of nothing before the encoder admits you arrived. The poll gates
            # itself on its own clock anyway (`mft.attention`); this only stops
            # the loop from sleeping through it.
            #
            # It is not free, and it is close: the window list is a third of a
            # millisecond and a frame that composes to the same cells writes
            # nothing to the wire, so ten idle frames a second cost a few
            # milliseconds of CPU against the thirty frames a second any
            # animation already spends.
            idle = min(idle, config.ATTENTION_POLL_SECONDS)
        still = 0  # consecutive frames that composed to the same board
        last_reap = time.monotonic()
        while not self._stop.is_set():
            # Cleared before the frame, not after it: an event that lands while
            # we are composing has to be able to cancel the sleep that follows,
            # or a board that just went still would sit on the stale frame for
            # up to `idle` seconds -- exactly the case this is here to serve.
            self._wake.clear()
            now = time.monotonic()
            # First in the frame, both of them: a wake that has not been noticed
            # yet makes every decision below it a decision about a board nobody
            # is writing to, and a dead port makes it one nobody is reading.
            self._check_wake_clock()
            # After the clock and before everything else: the clock reports a
            # wake for a dark wake too, and this is what decides whether that
            # wake gets to light anything up.
            self._check_display(now)
            self._check_port(now)
            self._check_waiting(now)
            # Before the bank follow and the paint: arriving in a tab forgives
            # that session's alert, and a board that chased the bank of a prompt
            # you are already reading would be moving the view to show you the
            # thing in front of you.
            self._check_attention(now)
            # Before the paint, so a followed bank and the frame that justifies
            # it land together rather than a frame apart. A peek is a modal view
            # of one session and the two self-portraits own the whole board, so
            # all three of them hold the view where it is.
            self.banks.follow(
                self.table.all(),
                now,
                blocked=bool(self._peeks) or self._waiting is not None,
            )
            still = 0 if self.paint(now) else still + 1
            # Before the reap, so an ended session's tab is handed back while
            # the record that knows which tty it was on still exists.
            self.tabs.paint(now)
            if now - last_reap > REAP_INTERVAL_SECONDS:
                self.upkeep.reap(now)
                last_reap = now
            if now - self._last_census > config.CENSUS_INTERVAL_SECONDS:
                # Last, and on its own clock: everything above answers in this
                # frame, and this one goes and asks the operating system.
                self._last_census = now
                self.upkeep.sweep_census()
            # Nothing moving means nothing to be smooth for. Sweeps, fades and
            # brightness decay all change their cells every frame, so a board
            # with any of them on it never gets here; what does is a board of
            # idle, ended and stalled sessions, or no sessions at all.
            self._wake.wait(idle if still >= config.IDLE_FRAMES else active)
        # Let go of the run loop before the shutdown animation: a sleep handler
        # that fires while that is painting would black out a board the exit is
        # about to paint anyway, and then hold the machine while it did it.
        self._power.stop()

    def shutdown_animation(self) -> None:
        """A spiral in from the top-left corner, a held beat, then all sixteen
        dimming together to dark.

        The point is not decoration: seeing it means the daemon exited on
        purpose rather than dying -- and unlike boot, something is waiting on it
        (`--stop` gives it five seconds), so it stays well inside that."""
        if not config.BOOT_ANIMATION:
            return
        self._stop.clear()
        try:
            self.animate(overlays_mod.ShutdownOverlay(time.monotonic()))
        finally:
            # The overlay's own last frame is nearly dark, not dark: the final
            # word on the board has to be an actual off, it has to be sent even
            # if the animation was cut short, and it has to go out forced --
            # the de-dup cache thinking those encoders are already off is not
            # something a board left glowing on the desk can be argued with.
            self.device.blackout()
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()  # don't sit out an idle sleep on the way to the door


if __name__ == "__main__":
    from .cli import main

    raise SystemExit(main())
