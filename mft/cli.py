"""Argv in, process out: the daemon's front door.

    python -m mft.daemon [--no-device] [--status|--stop|--discover]

Split out of :mod:`mft.daemon` because starting a process and painting a board
are unrelated jobs that happened to share a file. Everything here runs once, at
either end of the daemon's life: parse flags, answer the ones that are questions
rather than commands, wire the signals, hold the pid, and take the whole thing
down in the right order.

That order is the only subtle thing in this file. On the way out the tabs are
handed back *before* the shutdown animation, because the tabs are the part of
this that outlives the process and the animation takes a couple of seconds that
`--stop` is already counting.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import signal
import time
from pathlib import Path

from . import config, discover as discover_mod, httpd, pidfile, twister as twister_mod
from .daemon import Visualizer

log = logging.getLogger("mft.cli")

#: How long `--stop` waits for the daemon to clear the LEDs and let go of MIDI.
STOP_TIMEOUT_SECONDS = 5.0
STOP_POLL_SECONDS = 0.1


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude Code -> Midi Fighter Twister")
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument(
        "--match", default=config.PORT_MATCH, help="MIDI port name substring"
    )
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
    return parser


# -- the flags that are questions, not commands ------------------------------


def print_status(running: int | None) -> int:
    print(f"running (pid {running})" if running else "not running")
    return 0 if running else 1


def print_discovered() -> int:
    """What startup would adopt, without starting anything."""
    found = discover_mod.discover()
    for entry in found:
        tab = entry.terminal.get("tty") or entry.terminal.get("pid") or "unknown tab"
        # The state is a guess read off a transcript, so this is where you check
        # it against what the session is actually doing.
        print(f"{entry.session_id[:8]}  {entry.state or 'idle':8}  {tab}  {entry.cwd}")
    print(f"{len(found)} running session{'' if len(found) == 1 else 's'}")
    return 0


def stop_daemon(running: int | None) -> int:
    if not running:
        print("not running")
        return 1
    os.kill(running, signal.SIGTERM)
    for _ in range(int(STOP_TIMEOUT_SECONDS / STOP_POLL_SECONDS)):
        time.sleep(STOP_POLL_SECONDS)
        if pidfile.read() is None:
            print(f"stopped (pid {running})")
            return 0
    print(f"pid {running} did not exit")
    return 1


# -- running one -------------------------------------------------------------


def run_daemon(args: argparse.Namespace) -> int:
    device = (
        twister_mod.NullTwister()
        if args.no_device
        else twister_mod.open_twister(args.match)
    )
    visualizer = Visualizer(device)
    server = httpd.serve(visualizer, args.host, args.port)
    warn_about_hook_drift()

    def shutdown(*_args) -> None:
        log.info("shutting down")
        visualizer.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    pidfile.write()
    try:
        visualizer.run()
    finally:
        # Before the animation, which takes a couple of seconds: the tabs are
        # the part of this that outlives the process, and the board is not.
        visualizer.tabs.restore()
        visualizer.shutdown_animation()
        server.shutdown()
        device.close()
        pidfile.clear()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    running = pidfile.read()

    if args.status:
        return print_status(running)
    if args.discover:
        return print_discovered()
    if args.no_discover:
        config.DISCOVER_ON_START = False
    if args.stop:
        return stop_daemon(running)
    if running:
        print(f"already running (pid {running})")
        return 1

    return run_daemon(args)
