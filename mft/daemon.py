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

from . import (
    board as board_mod,
    config,
    context as context_mod,
    discover as discover_mod,
    focus as focus_mod,
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
)

log = logging.getLogger("mft.daemon")

#: Events about a subagent rather than about a session. They are routed to the
#: parent and are never allowed to create a session of their own.
SUBAGENT_EVENTS = frozenset({"SubagentStart", "SubagentStop"})

#: Events that may only ever *update* a session, never bring one into being.
#:
#: `SessionEnd` is one because a `/clear` retires a session id while its tab
#: keeps the encoder: the replacement id has often already been adopted by the
#: time the old id's `SessionEnd` lands, and it carries no terminal, so `ensure`
#: would answer that stale id by lighting a *second* encoder for a tab that is
#: already on the board. Nothing about an ending wants a slot allocated for it
#: in the first place, whatever the order the pair arrives in.
UPDATE_ONLY_EVENTS = frozenset({"SessionEnd"})


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
    except Exception:
        return
    if missing:
        log.warning(
            "hooks out of date: %s not installed, so those events never arrive "
            "-- re-run install_hooks.py",
            ", ".join(missing),
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


# --- encoder turns ----------------------------------------------------------


def turn_delta(previous: int | None, value: int, mode: str = config.ENCODER_MODE) -> int:
    """Detents turned since the last message, positive for clockwise.

    The Midi Fighter Utility offers both relative and absolute encoder modes,
    and which one you picked changes what arrives here entirely. Relative mode
    sends 63 for one detent left and 65 for one right; absolute mode sends a
    position, so the delta is the difference from last time. "auto" recognises
    the relative encoding by its values and falls back to absolute, which covers
    both stock modes without making you configure anything. Absolute mode is
    still the worse choice overall, because the hardware also fights the ring
    positions we write.
    """
    if mode == "relative" or (mode == "auto" and value in (63, 65)):
        return value - 64
    if previous is None:
        return 0
    return value - previous


class Visualizer:
    def __init__(self, device: twister_mod.Twister) -> None:
        self.device = device
        self.table = SessionTable()
        self._stop = threading.Event()
        self._press_started: dict[int, float] = {}
        self._encoder_values: dict[int, int] = {}
        self._overlays: list[board_mod.Overlay] = []
        #: Keyed by session, not slot: the board compacts under sessions that
        #: end, so a slot is not a stable handle for anything long-lived.
        self._compactions: dict[str, board_mod.CompactOverlay] = {}
        #: Per-slot, not a single overlay: pressing a second encoder before
        #: releasing the first would otherwise strand the first one on the
        #: board with nothing left to release it.
        self._peeks: dict[int, board_mod.PeekOverlay] = {}
        #: The idle animation, held onto so the render loop can retire it the
        #: moment there is a live session to retire it for.
        self._lamp: board_mod.LampTestOverlay | None = None
        self._lock = threading.Lock()
        #: The session a focus attempt is currently running for, so a second
        #: press on the same knob doesn't stack another AppleScript behind it.
        self._focus_lock = threading.Lock()
        self._focusing = ""

    # -- overlays -----------------------------------------------------------

    def push_overlay(self, overlay: board_mod.Overlay) -> None:
        with self._lock:
            self._overlays.append(overlay)

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

    # -- hook events --------------------------------------------------------

    def handle_event(self, event: dict) -> dict:
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

        # The SessionStart command hook enriches the payload with whatever it
        # could learn about the terminal; plain HTTP hooks don't have env. That
        # terminal identity is what owns the encoder, so it goes in before the
        # slot is chosen, not after.
        terminal = event.get("terminal")
        terminal = terminal if isinstance(terminal, dict) and terminal else None

        session = self.table.ensure(session_id, event.get("cwd", ""), terminal)
        if session is None:
            return {"ok": False, "error": "no free encoder"}
        if terminal:
            session.terminal = terminal

        before = session.subagents
        effects = apply_event(session, event)
        if session.subagents != before:
            log.info("%s: %d subagent(s) in flight", session.short_id, session.subagents)
        # A session ending (or restarting) is the only thing that changes who
        # belongs in the live block, so this is where the board is squeezed back
        # up to the top-left. It is a no-op the rest of the time.
        self.table.compact()
        self.refresh_context(session, time.monotonic())
        if effects:
            self._apply_effects(session, effects)
        return {"ok": True, "slot": session.slot + 1, "state": session.state}

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "device": type(self.device).__name__,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "encoder": s.slot + 1,
                    "bank": s.slot // config.ENCODERS_PER_BANK + 1,
                    "key": s.key,
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
                    "snoozed_for": (
                        round(s.snoozed_until - now, 1) if s.snoozed else 0
                    ),
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
        if msg.channel == config.CH_SWITCH:
            self._on_switch(msg.control, msg.value)
        elif msg.channel == config.CH_ENCODER:
            self.on_encoder_turn(msg.control, msg.value)

    def _on_switch(self, slot: int, value: int) -> None:
        session = self.table.by_slot(slot)
        if value >= 64:  # press down
            self._press_started[slot] = time.monotonic()
            if session is not None:
                # Hold to peek. The overlay goes up now but only paints once the
                # press has lasted HOLD_SECONDS, and it comes down on release --
                # a spring-loaded modal view, not a mode you can get stuck in.
                peek = board_mod.PeekOverlay(session, time.monotonic())
                self._peeks[slot] = peek
                self.push_overlay(peek)
            return

        started = self._press_started.pop(slot, None)
        peek = self._peeks.pop(slot, None)
        if peek is not None:
            peek.release()
        if started is None:
            return
        held = time.monotonic() - started
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

    def on_encoder_turn(self, slot: int, value: int) -> None:
        """Right snoozes, left un-snoozes."""
        previous = self._encoder_values.get(slot)
        self._encoder_values[slot] = value
        delta = turn_delta(previous, value)
        if not delta:
            return
        session = self.table.by_slot(slot)
        if session is None:
            return

        now = time.monotonic()
        base = max(now, session.snoozed_until or now)
        remaining = base - now + delta * config.SNOOZE_STEP_SECONDS
        remaining = max(0.0, min(config.SNOOZE_MAX_SECONDS, remaining))
        session.snoozed_until = now + remaining if remaining else None
        log.info("%s snoozed for %.0fs", session.label, remaining)

    # -- render loop --------------------------------------------------------

    def _live_overlays(self, now: float) -> list[board_mod.Overlay]:
        with self._lock:
            self._overlays = [o for o in self._overlays if not o.done(now)]
            return list(self._overlays)

    def _check_lamp_test(self, now: float) -> None:
        """Retire the idle animation once a Claude is actually on the board.

        A session that has already ended does not count: its encoder is only
        lingering so you can see how it finished, and the board is idle again
        in every sense that matters here.
        """
        lamp = self._lamp
        if lamp is None:
            return
        if lamp.done(now):
            self._lamp = None
        elif any(s.ended_at is None for s in self.table.all()):
            lamp.dismiss(now)

    def paint(self, now: float, overlays: list[board_mod.Overlay] | None = None) -> None:
        cells = board_mod.compose(
            self.table.all(),
            now,
            self._live_overlays(now) if overlays is None else overlays,
        )
        for slot, cell in enumerate(cells):
            self.device.write(slot, cell)

    def animate(self, overlay: board_mod.Overlay) -> None:
        """Run one overlay to completion, blocking. Used for boot and shutdown,
        where there is nothing else to render anyway."""
        period = 1.0 / config.FPS
        while not overlay.done(time.monotonic()):
            self.paint(time.monotonic(), [overlay])
            if self._stop.wait(period):
                return

    def adopt_running_sessions(self) -> None:
        """Put the sessions that predate us on the board.

        Wrapped whole: nothing about a display is worth failing to start over,
        and the board fills itself in from hook events either way -- this only
        decides whether it does so now or on each session's next turn.
        """
        if not config.DISCOVER_ON_START:
            return
        try:
            adopted = discover_mod.adopt(self.table, discover_mod.discover())
        except Exception:
            log.exception("discovery failed; starting with an empty board")
            return
        now = time.monotonic()
        for session in adopted:
            self.refresh_context(session, now)
        self.table.compact()
        if adopted:
            log.info(
                "adopted %d running session%s: %s",
                len(adopted),
                "" if len(adopted) == 1 else "s",
                ", ".join(s.label for s in adopted),
            )

    def run(self) -> None:
        self.device.clear_all()
        self.device.listen(self.on_midi)
        self.device.start_clock()
        # Before the boot word rather than after it: the lamp test that follows
        # retires the moment a session exists, and an encoder that lights up
        # halfway through the idle field reads as a session that just started.
        self.adopt_running_sessions()
        if config.BOOT_ANIMATION:
            self.animate(
                board_mod.TextOverlay(config.BOOT_WORD, time.monotonic())
            )
            # The word blocks; the lamp test does not. It goes on the overlay
            # stack and keeps running inside the normal render loop, because
            # the whole point is that it yields to the first real session --
            # which it can only do if that session is being rendered too.
            self._lamp = board_mod.LampTestOverlay(time.monotonic())
            self.push_overlay(self._lamp)

        period = 1.0 / config.FPS
        last_reap = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            self._check_lamp_test(now)
            self.paint(now)
            if now - last_reap > 5.0:
                self.table.reap()
                last_reap = now
            self._stop.wait(period)

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
            return json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            log.warning("bad json from hook: %s", exc)
            return None

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
        visualizer.shutdown_animation()
        server.shutdown()
        device.close()
        clear_pid()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
