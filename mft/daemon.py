"""The daemon: one process owns the MIDI port, listens for hook POSTs, and
renders every known session onto the Twister at a fixed frame rate.

    python -m mft.daemon

Hooks fire and forget at it over HTTP, so a dead daemon costs each session a
failed connection and nothing else. Nothing here ever holds a hook open or puts
a body on the wire: this is a display, and a display that can block a tool call
is a liability every time it hangs.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import (
    board as board_mod,
    config,
    context as context_mod,
    discover as discover_mod,
    focus as focus_mod,
    power as power_mod,
    render as render_mod,
    tab as tab_mod,
    twister as twister_mod,
)
from .state import (
    EFFECT_BANNER,
    EFFECT_CLEAR,
    EFFECT_COMPACT_END,
    EFFECT_COMPACT_START,
    EFFECT_SPAWN,
    Session,
    SessionTable,
    apply_event,
    merge_terminal,
)

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

#: Header `hooks/notify.sh` carries the terminal identity in, as
#: ``name=value;name=value``. A header rather than a field spliced into the
#: event JSON: the script's whole job is to pipe stdin at `curl` untouched, and
#: rewriting JSON in `sh` to add one object is how that stops being reliable.
#:
#: The point of it is that *every* event names its tab, not just the two that
#: run the Python hook. An event that arrives anonymous can only be answered by
#: guessing which session it belongs to, and a wrong guess is a knob that lies.
TERMINAL_HEADER = "X-MFT-Terminal"

#: Bounds on what we will read out of that header: it is trusted input from our
#: own hook, but it arrives over a socket and nothing else here is unbounded.
TERMINAL_HEADER_MAX = 4096
TERMINAL_HEADER_FIELDS = 24

#: A frame that fails fails every frame, so the traceback is rate-limited to
#: roughly one a minute rather than 30 a second.
PAINT_ERROR_LOG_SECONDS = 60.0

#: How often the reaper sweeps for sessions that ended or went silent.
REAP_INTERVAL_SECONDS = 5.0


def parse_terminal_header(raw: str) -> dict:
    """``name=value;name=value`` -> a terminal dict, or ``{}`` if it says nothing.

    Deliberately forgiving: this is a display, and an identity we cannot parse
    should cost the event its tab, not the event. Values are taken verbatim up to
    the first ``;`` -- terminal identifiers are short, printable and delimiter
    free (``w0t0p0:UUID``, ``%3``, ``/dev/ttys004``) -- and anything else is
    dropped rather than repaired.
    """
    if not raw or len(raw) > TERMINAL_HEADER_MAX:
        return {}
    terminal: dict[str, str] = {}
    for field in raw.split(";")[:TERMINAL_HEADER_FIELDS]:
        name, sep, value = field.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name or not value:
            continue
        terminal[name] = value
    return terminal


def warn_about_hook_drift() -> None:
    """Say so when the installed hooks are older than the code reading them.

    A hook this daemon handles but that nobody installed is completely silent:
    that part of the board simply never lights, and there is nothing to see in
    a log that was never written. Worth one line at startup.
    """
    # By path, not by name: the daemon is normally started from an app bundle or
    # a launchd plist, neither of which has the repo root on sys.path.
    script = Path(__file__).resolve().parent.parent / "install_hooks.py"
    try:
        spec = importlib.util.spec_from_file_location("mft_install_hooks", script)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        missing = module.missing_events()
        env = module.missing_env()
    except Exception:
        return
    if missing:
        log.warning(
            "hooks out of date: %s not installed, so those events never arrive "
            "-- re-run install_hooks.py",
            ", ".join(missing),
        )
    if env and config.TAB_TITLE:
        log.warning(
            "settings are missing %s, so Claude Code will keep overwriting the "
            "tab titles this daemon paints -- re-run install_hooks.py",
            ", ".join(env),
        )


# --- pid file ---------------------------------------------------------------


def read_pid() -> int | None:
    """The running daemon's pid, or None if it isn't running.

    A stale pid file (daemon was SIGKILLed, machine lost power) is treated as
    absent and cleaned up, so a crash never wedges the launcher.
    """
    try:
        pid = int(Path(config.PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)  # signal 0 only checks that we may signal it
    except ProcessLookupError:
        Path(config.PID_FILE).unlink(missing_ok=True)
        return None
    except PermissionError:
        # Someone else's process now owns that pid: not ours.
        Path(config.PID_FILE).unlink(missing_ok=True)
        return None
    return pid


def write_pid() -> None:
    path = Path(config.PID_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n")


def clear_pid() -> None:
    Path(config.PID_FILE).unlink(missing_ok=True)


class Visualizer:
    def __init__(self, device: twister_mod.Twister) -> None:
        self.device = device
        self.table = SessionTable()
        self._stop = threading.Event()
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
        self._compactions: dict[str, board_mod.CompactOverlay] = {}
        #: Per-slot, not a single overlay: pressing a second encoder before
        #: releasing the first would otherwise strand the first one on the
        #: board with nothing left to release it.
        self._peeks: dict[int, board_mod.PeekOverlay] = {}
        #: The waiting animation, held onto so the render loop can retire it the
        #: moment there is a live session to retire it for.
        self._waiting: board_mod.WaitingOverlay | None = None
        #: When to start it, or `None` once that decision has been made. The
        #: render loop holds off for a couple of frames after the boot word and
        #: only starts the animation if the board is still genuinely empty; see
        #: :meth:`_check_waiting`.
        self._waiting_due: float | None = None
        #: Which bank is on the front panel, and when it last moved. Starts at 0
        #: as an assumption rather than a reading: nothing on this hardware
        #: reports the current bank, and asking would mean sending a bank select
        #: to find out -- which is the very thing that needs a reason. Wrong at
        #: worst until the first side button or the first followed alert.
        self._bank = 0
        self._bank_moved_at = 0.0
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
        #: When the tab strip was last considered. Nothing there animates, so it
        #: is decoupled from the frame rate entirely; see :meth:`paint_tabs`.
        self._last_tab_paint = float("-inf")
        #: Set while the machine is asleep, so no frame relights a board that
        #: was just blacked out for it. See :meth:`on_system_sleep`.
        self._suspended = threading.Event()
        #: Serialises a frame against the blackout, which is the one write that
        #: has to be the last thing on the wire. Uncontended at 30Hz.
        self._paint_lock = threading.Lock()
        #: Sleep and wake, both detectors; see :mod:`mft.power`.
        self._power = power_mod.PowerWatcher(self.on_system_sleep, self.on_system_wake)
        self._wake_clock = power_mod.WakeClock()
        #: Debounces the two detectors reporting one wake.
        self._last_system_wake = float("-inf")
        #: When a dead MIDI port was last retried; see :meth:`_check_port`.
        self._last_port_retry = float("-inf")

    # -- overlays -----------------------------------------------------------

    def push_overlay(self, overlay: board_mod.Overlay) -> None:
        with self._lock:
            self._overlays.append(overlay)
        # Every overlay is an animation, so the loop has to be at full rate for
        # it whatever the board underneath was doing.
        self._wake.set()

    def _apply_effects(self, session: Session, effects: list[str]) -> None:
        now = time.monotonic()
        for effect in effects:
            if effect == EFFECT_COMPACT_START:
                overlay = board_mod.CompactOverlay(session, now)
                self._compactions[session.session_id] = overlay
                self.push_overlay(overlay)
            elif effect == EFFECT_COMPACT_END:
                overlay = self._compactions.pop(session.session_id, None)
                if overlay is not None:
                    overlay.finish(now)
            elif effect == EFFECT_SPAWN:
                if config.SPAWN_ANIMATION:
                    self.push_overlay(board_mod.SpawnOverlay(session, now))
            elif effect == EFFECT_CLEAR:
                if config.CLEAR_ANIMATION:
                    self.push_overlay(board_mod.ClearOverlay(session, now))
            elif effect.startswith(EFFECT_BANNER):
                word = effect[len(EFFECT_BANNER) :]
                self.push_overlay(
                    board_mod.TextOverlay(
                        word,
                        now,
                        color=config.BANNER_COLOR,
                        bank=board_mod.bank_of(session.slot),
                        hold=config.BANNER_SECONDS / max(1, len(word)),
                    )
                )

    # -- context gauge ------------------------------------------------------

    def refresh_context(self, session: Session, now: float) -> None:
        """Top up the session's context reading from its transcript.

        Rate-limited, because this runs on the HTTP thread and events arrive far
        faster than a context window moves. A failed read leaves the last good
        value standing rather than collapsing the ring to nothing.
        """
        if not config.CONTEXT_RING or not session.transcript_path:
            return
        if now - session.context_checked_at < config.CONTEXT_POLL_SECONDS:
            return
        session.context_checked_at = now
        try:
            usage = context_mod.read_usage(session.transcript_path)
        except Exception:
            log.exception("context read failed for %s", session.label)
            return
        if usage is None:
            return
        tokens, model = usage
        session.context_tokens = tokens
        if model:
            session.model = model
        session.context_limit = context_mod.limit_for_model(session.model)

    # -- terminal tab -------------------------------------------------------

    def refresh_tab_title(self, session: Session, now: float) -> None:
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
            title = context_mod.read_title(session.transcript_path)
        except Exception:
            log.exception("title read failed for %s", session.label)
            return
        if title:
            session.tab_title = title
            session.tab_title_turn = session.turn_count

    def paint_tabs(self, now: float) -> None:
        """Put each session's state glyph in front of its tab title.

        Rate-limited to a twentieth of the board's frame rate and then, past
        that, gated on the composed line having actually changed -- so a session
        that spends ten minutes working costs one write, not eighteen thousand.
        The comparison is against what we last *sent*, not against the state, so
        a title that changed while the glyph didn't still gets through.
        """
        if not config.TAB_TITLE:
            return
        if now - self._last_tab_paint < config.TAB_POLL_SECONDS:
            return
        self._last_tab_paint = now
        for session in self.table.all():
            self._paint_tab(session, tab_mod.glyph_for(session))

    def _paint_tab(self, session: Session, glyph: str) -> None:
        tty = tab_mod.tty_of(session)
        if not tty:
            return
        # The directory name until Claude Code has generated a title. It is a
        # worse label but it is never wrong, and a tab reading "🔴" alone tells
        # you a session wants you without telling you which one.
        title = session.tab_title or os.path.basename(session.cwd)
        line = tab_mod.compose(glyph, title)
        if line == session.tab_painted:
            return
        if tab_mod.write(tty, line):
            session.tab_painted = line

    def restore_tabs(self, sessions: list[Session] | None = None) -> None:
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
        sessions = self.table.all() if sessions is None else sessions
        retiring = {id(s) for s in sessions}
        claimed = {
            tab_mod.tty_of(s)
            for s in self.table.all()
            if id(s) not in retiring and s.state != "ended"
        } - {""}
        for session in sessions:
            if tab_mod.tty_of(session) in claimed:
                continue
            self._paint_tab(session, "")

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
            # A subagent owns no encoder, so its events must never be able to
            # claim one. Whether these payloads carry the parent's session id or
            # the subagent's own is undocumented, and `ensure` would answer the
            # second case by lighting a fresh encoder from the top-left -- a
            # subagent rendered as a session, which is the one thing the board
            # is not allowed to do. So: find the parent or drop the event.
            session = self.table.find_parent(session_id, event.get("cwd", ""))
            if session is None:
                log.debug("%s with no known parent (%s)", name, session_id[:8])
                return {"ok": True}
            before = session.subagents
            apply_event(session, event)
            if session.subagents != before:
                log.info(
                    "%s: %d subagent(s) in flight", session.short_id, session.subagents
                )
            return {"ok": True, "slot": session.slot + 1, "state": session.state}

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

        before = session.subagents
        effects = apply_event(session, event)
        if session.subagents != before:
            log.info("%s: %d subagent(s) in flight", session.short_id, session.subagents)
        # A session ending (or restarting) is the only thing that changes who
        # belongs in the live block, so this is where the board is squeezed back
        # up to the top-left -- and only for those events, since for the rest it
        # is a no-op that still does all the work. See COMPACTING_EVENTS.
        if name in COMPACTING_EVENTS:
            self.table.compact()
        now = time.monotonic()
        self.refresh_context(session, now)
        self.refresh_tab_title(session, now)
        if effects:
            self._apply_effects(session, effects)
        return {"ok": True, "slot": session.slot + 1, "state": session.state}

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "device": type(self.device).__name__,
            # Why the desk is dark, which is otherwise indistinguishable from a
            # daemon that died with the lights off.
            "sleep": round(self._sleep.gain(now), 2),
            # The other two ways to be dark and healthy: the machine is asleep,
            # or the port stopped taking writes and is waiting to be reopened.
            "suspended": self._suspended.is_set(),
            "port_failing": self.device.failing(),
            # Which sixteen of the sixty-four encoders are actually on the front
            # panel. The other reason a board looks empty: everything is on a bank
            # you are not looking at. Compare against each session's own `bank`.
            "bank": self._bank + 1,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "encoder": s.slot + 1,
                    "bank": s.slot // config.ENCODERS_PER_BANK + 1,
                    "key": s.key,
                    # Every token it answers to, not just the strongest: when a
                    # knob is on the wrong session this is the field that says
                    # which tab the daemon thinks it belongs to, and why.
                    "keys": sorted(s.keys),
                    "state": s.state,
                    "cwd": s.cwd,
                    "turns": s.turn_count,
                    "tool_calls": s.tool_calls,
                    "last_tool": s.last_tool,
                    "subagents": s.subagents,
                    "model": s.model,
                    "context_tokens": s.context_tokens,
                    "context_limit": s.context_limit,
                    "context_pct": (
                        round(100 * s.context_fraction)
                        if s.context_fraction is not None
                        else None
                    ),
                    "alert": s.alert,
                    "unsupervised": s.unsupervised,
                    "attention_for": (
                        round(now - s.attention_since, 1) if s.attention_since else 0
                    ),
                    "terminal": s.terminal.get("TERM_PROGRAM", "?"),
                    "idle_for": round(now - s.last_event_at, 1),
                }
                for s in sorted(self.table.all(), key=lambda s: s.slot)
            ],
        }

    # -- encoder input ------------------------------------------------------

    def on_midi(self, msg) -> None:
        if msg.type != "control_change":
            return
        # Any CC at all, turn or press, is a hand on the hardware -- the one
        # activity sleep can see that has nothing to do with any agent, and if
        # the board is dark it is very likely the whole reason for it. Waking
        # the loop is right for a turn on its own terms too: the ring the knob
        # lit locally is undone by the next frame, and "next" should mean now.
        self._sleep.touch(time.monotonic())
        self._wake.set()
        if msg.channel == config.CH_SWITCH:
            self._on_switch(msg.control, msg.value)
        elif msg.channel == config.CH_SYSTEM:
            self._on_system(msg.control, msg.value)
        # A turn is otherwise deliberately ignored: the board is a display, not
        # a control surface -- see :meth:`Twister.forget_rings`.

    def _on_system(self, control: int, value: int) -> None:
        """A side button: the human just chose a bank.

        Recorded rather than acted on. It is also the one input that tells us
        something we otherwise have to assume -- see `self._bank` -- and it starts
        the cooldown, because a view you picked by hand should outlive the next
        notification. `on_midi` has already touched sleep for it.
        """
        if value < 64 or control not in config.BANK_SELECT_CC:
            return
        self._bank = config.BANK_SELECT_CC.index(control)
        self._bank_moved_at = time.monotonic()
        log.debug("bank %d selected by hand", self._bank + 1)

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
                peek = board_mod.PeekOverlay(session, now)
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

    # -- render loop --------------------------------------------------------

    def _live_overlays(self, now: float) -> list[board_mod.Overlay]:
        with self._lock:
            self._overlays = [o for o in self._overlays if not o.done(now)]
            return list(self._overlays)

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
                self._waiting = board_mod.WaitingOverlay(now)
                self.push_overlay(self._waiting)
            return

        waiting = self._waiting
        if waiting is None:
            return
        if waiting.done(now):
            self._waiting = None
        elif self._live_session():
            waiting.dismiss(now)

    def _follow_alerts(self, now: float) -> None:
        """Pull the front panel onto the bank where a human is blocking.

        The policy is `board.bank_to_show`; everything here is the part that must
        not be pure. Three brakes on it, and they are the whole reason this is
        safe to have on by default:

        * a cooldown, so two prompts on two banks cannot bounce the view between
          them, and so a bank you picked by hand stays picked;
        * never during a peek, which is a modal view of one session's history and
          would be silently replaced by another bank's encoders;
        * never while the boot word or the waiting animation still owns the
          board, which are the two moments the daemon is talking about itself
          rather than reporting -- and a bank select mid-word truncates the word.
        """
        if not config.FOLLOW_ALERTS or self._peeks or self._waiting is not None:
            return
        if now - self._bank_moved_at < config.FOLLOW_ALERT_COOLDOWN_SECONDS:
            return
        want = board_mod.bank_to_show(self.table.all(), self._bank)
        if want is None:
            return
        log.info("bank %d has something blocking; following it", want + 1)
        self.device.bank(want)
        self._bank = want
        self._bank_moved_at = now

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

    # -- sleep and wake -----------------------------------------------------

    def on_system_sleep(self) -> None:
        """Darken the board, with the machine waiting on us to finish.

        Runs on the notification thread (:mod:`mft.power`) and does exactly the
        one thing that cannot be done after the fact. The flag goes up *before*
        the blackout and under the same lock a frame holds, or the render loop
        gets one more frame in afterwards and the last word on the board is a
        lit one -- which is the entire bug this exists to prevent, and it would
        show up only as "sometimes it stays on overnight".

        Not conditional on there being anything worth showing: an empty board
        still breathes (`config.AMBIENT`), and a desk lamp is exactly what this
        must not be at 3am.
        """
        if not config.SLEEP_BLACKOUT:
            return
        with self._paint_lock:
            self._suspended.set()
            try:
                self.device.blackout()
            except Exception:
                log.exception("could not darken the board for sleep")
        log.info("system sleeping; board dark")

    def on_system_wake(self, source: str = "notification") -> None:
        """Put the board back, and re-check the table it is painting.

        Idempotent and debounced, because both detectors fire for a healthy
        wake by design. The order matters: the cache goes first, since a repaint
        that the de-dup suppresses is exactly as dark as no repaint at all.

        What this deliberately does *not* do is touch the sleep timer. Opening
        the lid looks identical to a Power Nap from here, and a board that
        relights itself to full brightness for a backup at 3am is worse than one
        that comes back at the dim level it went down at -- which, since the
        clock it runs on froze with the machine, is exactly where you left it.
        The first hook event or encoder press brings it up, as it always does.
        """
        now = time.monotonic()
        with self._lock:
            if now - self._last_system_wake < config.WAKE_DEBOUNCE_SECONDS:
                return
            self._last_system_wake = now
        log.info("system awake (%s); repainting", source)
        self._suspended.clear()
        self.device.forget_all()
        self._last_cells = None
        if config.WAKE_REDISCOVER:
            self.adopt_running_sessions(awaken=False)
        self._wake.set()

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

    def animate(self, overlay: board_mod.Overlay) -> None:
        """Run one overlay to completion, blocking. Used for boot and shutdown,
        where there is nothing else to render anyway."""
        period = 1.0 / config.FPS
        while not overlay.done(time.monotonic()):
            self.paint(time.monotonic(), [overlay])
            if self._stop.wait(period):
                return

    def adopt_running_sessions(self, awaken: bool = True) -> None:
        """Put the sessions that predate us on the board.

        Wrapped whole: nothing about a display is worth failing to start over,
        and the board fills itself in from hook events either way -- this only
        decides whether it does so now or on each session's next turn.

        Also runs on wake, where nearly everything it finds is already on the
        board -- `discover.adopt` hands back the sessions it *recognised* as
        much as the ones it added, so only the difference is news, and only the
        difference is worth logging or waking the board for. `awaken` is what
        the wake path turns off: see :meth:`on_system_wake` for why a machine
        powering on is not evidence that anyone is at the desk.
        """
        if not config.DISCOVER_ON_START:
            return
        known = {s.session_id for s in self.table.all()}
        try:
            adopted = discover_mod.adopt(self.table, discover_mod.discover())
        except Exception:
            log.exception("discovery failed; starting with an empty board")
            return
        now = time.monotonic()
        for session in adopted:
            self.refresh_context(session, now)
        # Discovery reconstructs identities from the process table and writes
        # them onto sessions a hook may already have created, so this is exactly
        # the moment one tab can end up described twice.
        self.table.reconcile(now)
        self.table.compact()
        adopted = [s for s in adopted if s.session_id not in known]
        if adopted:
            log.info(
                "adopted %d running session%s: %s",
                len(adopted),
                "" if len(adopted) == 1 else "s",
                ", ".join(s.label for s in adopted),
            )
        if adopted and awaken:
            # These sessions have been running without us and none of them has
            # sent us an event yet. A daemon started onto a live board starts
            # awake, and its first half hour is measured from now.
            self._sleep.touch(now)

    def run(self) -> None:
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
            self.animate(
                board_mod.TextOverlay(config.BOOT_WORD, time.monotonic())
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
        active = 1.0 / config.FPS
        idle = 1.0 / config.IDLE_FPS
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
            self._check_port(now)
            self._check_waiting(now)
            # Before the paint, so a followed bank and the frame that justifies
            # it land together rather than a frame apart.
            self._follow_alerts(now)
            still = 0 if self.paint(now) else still + 1
            # Before the reap, so an ended session's tab is handed back while
            # the record that knows which tty it was on still exists.
            self.paint_tabs(now)
            if now - last_reap > REAP_INTERVAL_SECONDS:
                self.restore_tabs(self.table.reap())
                # Alongside the reaper, and for the same reason it exists: what
                # this catches is a board that looks entirely plausible and is
                # quietly wrong -- one tab on two encoders -- and would stay that
                # way for an hour of TTL. See `SessionTable.reconcile`.
                self.table.reconcile(now)
                last_reap = now
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
            self.animate(board_mod.ShutdownOverlay(time.monotonic()))
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


class HookHandler(BaseHTTPRequestHandler):
    server_version = "mft/1.0"
    visualizer: Visualizer  # set on the server instance

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self) -> None:
        """204, deliberately, for every hook without exception.

        Claude Code parses a hook's response body as hook control JSON -- it is
        how a hook blocks a tool call or injects context. A visualizer has no
        opinion about any of that, so it must never put a body on the wire.
        Debug output lives on GET /status instead.
        """
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_event(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            event = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            log.warning("bad json from hook: %s", exc)
            return None
        if not isinstance(event, dict):
            log.warning("hook payload is not an object: %r", type(event).__name__)
            return None
        # The body wins: `register_session.py` puts a richer terminal in it than
        # a header can carry, and it is the same field either way.
        if not isinstance(event.get("terminal"), dict) or not event["terminal"]:
            identity = parse_terminal_header(self.headers.get(TERMINAL_HEADER, ""))
            if identity:
                event["terminal"] = identity
        return event

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        event = self._read_event()
        if event is None:
            self._send_empty()
            return

        try:
            result = self.server.visualizer.handle_event(event)
        except Exception:
            log.exception("event handling failed")
        else:
            if not result.get("ok"):
                log.warning("event rejected: %s", result.get("error"))
        self._send_empty()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/status"):
            self._send(200, self.server.visualizer.status())
        else:
            self._send(200, {"ok": True, "hint": "POST hook JSON here"})

    def log_message(self, fmt: str, *args) -> None:
        log.debug("http: " + fmt, *args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Code -> Midi Fighter Twister")
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--match", default=config.PORT_MATCH, help="MIDI port name substring")
    parser.add_argument("--no-device", action="store_true", help="run without hardware")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--stop", action="store_true", help="stop a running daemon")
    parser.add_argument(
        "--status",
        action="store_true",
        help="exit 0 if the daemon is running, 1 if not",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="print the running sessions startup would adopt, then exit",
    )
    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="start with an empty board instead of adopting running sessions",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    running = read_pid()

    if args.status:
        print(f"running (pid {running})" if running else "not running")
        return 0 if running else 1

    if args.discover:
        found = discover_mod.discover()
        for entry in found:
            tab = entry.terminal.get("tty") or entry.terminal.get("pid") or "unknown tab"
            print(f"{entry.session_id[:8]}  {tab}  {entry.cwd}")
        print(f"{len(found)} running session{'' if len(found) == 1 else 's'}")
        return 0

    if args.no_discover:
        config.DISCOVER_ON_START = False

    if args.stop:
        if not running:
            print("not running")
            return 1
        os.kill(running, signal.SIGTERM)
        for _ in range(50):  # give it 5s to clear the LEDs and let go of MIDI
            time.sleep(0.1)
            if read_pid() is None:
                print(f"stopped (pid {running})")
                return 0
        print(f"pid {running} did not exit")
        return 1

    if running:
        print(f"already running (pid {running})")
        return 1

    device = (
        twister_mod.NullTwister()
        if args.no_device
        else twister_mod.open_twister(args.match)
    )
    visualizer = Visualizer(device)

    server = ThreadingHTTPServer((args.host, args.port), HookHandler)
    server.visualizer = visualizer
    threading.Thread(target=server.serve_forever, name="mft-http", daemon=True).start()
    log.info("listening on http://%s:%d", args.host, args.port)
    warn_about_hook_drift()

    def shutdown(*_args) -> None:
        log.info("shutting down")
        visualizer.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    write_pid()
    try:
        visualizer.run()
    finally:
        # Before the animation, which takes a couple of seconds: the tabs are
        # the part of this that outlives the process, and the board is not.
        visualizer.restore_tabs()
        visualizer.shutdown_animation()
        server.shutdown()
        device.close()
        clear_pid()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
