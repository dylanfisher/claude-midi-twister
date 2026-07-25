"""Find the Claude Code sessions that were already running when we started.

Hooks are a one-way push: nothing in Claude Code answers questions, so a
session that existed before the daemon did stays dark until it fires its next
event. For a session mid-turn that is a few seconds. For the one sitting at a
permission prompt -- exactly the one you wanted the board to show you -- it is
forever.

Two sources, joined:

*   **Transcripts.** ``~/.claude/projects/<slug>/<session-id>.jsonl``. The
    filename is the session id and the entries carry ``cwd``, so a recently
    modified transcript names a session and where it lives. What it cannot say
    is whether that session is still *running*: a transcript you Ctrl-C'd two
    minutes ago looks exactly like one waiting for your answer.
*   **The process table.** Proves liveness, and carries the tty, which is what
    :func:`mft.state.terminal_keys` wants for a durable slot.

Neither is sufficient alone, so nothing is adopted without both. The bias
throughout is toward showing too little: a missing encoder is a session you
find in a moment anyway, while a phantom one is a knob that lies until the TTL
reaps it an hour later.

The same process table answers the opposite question, which is why
:func:`orphans` lives here too: a session on the board whose claude is gone.
Adoption is a guess that has to be right; that one is a fact, and it is checked
on a much tighter loop -- see its docstring for why it is deliberately narrower
than everything else in this file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from . import config
from .context import tail_lines
from .state import Session, SessionTable

log = logging.getLogger("mft.discover")

#: Argv markers for the helper processes that surround a real session: the
#: pty hosts, the pre-warmed spares, and the background daemon itself. All of
#: them are called `claude` and none of them is an agent.
NOT_A_SESSION = ("bg-pty-host", "bg-spare", "--bg-spare", "daemon run")


#: Environment variables worth lifting back out of a live process. This is the
#: same set ``hooks/register_session.py`` exports, because it is read from the
#: same place -- macOS `ps -E` prints a process's environment -- so a session
#: recovered here focuses exactly as precisely as one that ran the hook.
ENV_KEYS = (
    "TERM_PROGRAM",
    "TERM_SESSION_ID",
    "ITERM_SESSION_ID",
    "TMUX",
    "TMUX_PANE",
    "WEZTERM_PANE",
    "KITTY_WINDOW_ID",
    "ALACRITTY_WINDOW_ID",
    "GHOSTTY_RESOURCES_DIR",
    "__CFBundleIdentifier",
)


@dataclass(frozen=True)
class Proc:
    """A live Claude Code process."""

    pid: int
    tty: str = ""
    cwd: str = ""
    #: Set only when argv names one outright, which the app-launched form does
    #: and a terminal-launched `claude` does not.
    session_id: str = ""
    #: The identifying slice of its environment; see :data:`ENV_KEYS`. Excluded
    #: from equality so a Proc stays comparable and hashable.
    env: dict[str, str] = field(default_factory=dict, compare=False)

    @property
    def terminal(self) -> dict[str, str]:
        """What ``hooks/register_session.py`` would have collected, recovered
        from outside the process. ``pid`` matches its semantics: the claude
        process itself, which is that hook's ``getppid()``.

        The environment only counts when the process holds a tty. A session
        launched from the desktop app has no terminal of its own but still
        *inherits* one's variables -- and two of them inherit the same
        ``TERM_SESSION_ID``, which as a slot key would have them fighting over
        one encoder and as a focus target would raise a tab neither is in. With
        only a pid left, the ancestor adapter raises the app they really live
        in, which is the true answer.
        """
        terminal = {"pid": str(self.pid)}
        if self.tty:
            terminal["tty"] = self.tty
            terminal.update(self.env)
        return terminal


@dataclass(frozen=True)
class Transcript:
    session_id: str
    path: str
    cwd: str
    modified_at: float
    permission_mode: str = ""


@dataclass(frozen=True)
class Discovered:
    """A session worth putting on the board, and what we know about it."""

    session_id: str
    cwd: str
    transcript_path: str
    #: Empty when the transcript could not be pinned to one specific process.
    #: The session still gets an encoder; it just falls back to a session-id
    #: key, so press-to-focus can't jump to its tab.
    terminal: dict[str, str] = field(default_factory=dict)
    permission_mode: str = ""


# --- the process table ------------------------------------------------------


def _run(argv: list[str]) -> Optional[str]:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=config.DISCOVER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", argv[0], exc)
        return None
    return done.stdout


def _session_id_from_argv(argv: str) -> str:
    """The uuid after ``--session-id``, if argv carries one."""
    parts = argv.split()
    for index, part in enumerate(parts[:-1]):
        if part == "--session-id":
            return parts[index + 1]
    return ""


def _cwds(pids: list[int]) -> dict[int, str]:
    """pid -> working directory, in one `lsof` call.

    ``-Fpn`` is lsof's parseable output: a ``p<pid>`` line, then an ``n<path>``
    line for each of that process's descriptors -- here only ``cwd``.
    """
    if not pids:
        return {}
    out = _run(
        ["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(str(p) for p in pids)]
    )
    if not out:
        return {}
    cwds: dict[int, str] = {}
    pid: Optional[int] = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            cwds[pid] = line[1:]
    return cwds


def _environments(pids: list[int]) -> dict[int, dict[str, str]]:
    """pid -> the :data:`ENV_KEYS` it was started with, in one `ps` call.

    ``ps -E`` appends the environment after argv for processes we own, which is
    every session on this machine. Values are matched by key rather than
    positionally because there is no delimiter between argv and environment;
    the last match wins, since a variable that also appears inside a command
    line appears there first.
    """
    if not pids:
        return {}
    out = _run(
        ["ps", "-Eww", "-o", "pid=,command=", "-p", ",".join(str(p) for p in pids)]
    )
    if not out:
        return {}
    found: dict[int, dict[str, str]] = {}
    for line in out.splitlines():
        head, _, rest = line.strip().partition(" ")
        try:
            pid = int(head)
        except ValueError:
            continue
        env = {}
        for key in ENV_KEYS:
            matches = re.findall(r"(?:^|\s)" + re.escape(key) + r"=(\S*)", rest)
            if matches and matches[-1]:
                env[key] = matches[-1]
        if env:
            found[pid] = env
    return found


def claude_processes() -> Optional[list[Proc]]:
    """Every live Claude Code session process.

    ``None`` means the process table could not be read at all, which is a very
    different thing from "nothing is running" -- discovery refuses to adopt
    anything in that case rather than trusting transcripts on their own.
    """
    out = _run(["ps", "-eo", "pid=,tty=,command="])
    if out is None:
        return None

    found: list[tuple[int, str, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        raw_pid, tty, argv = parts
        # The interpreter path for a terminal session is `.../versions/2.1.x`,
        # so match on the install root rather than on the program name.
        if "claude" not in argv.lower():
            continue
        if any(marker in argv for marker in NOT_A_SESSION):
            continue
        # Hook scripts, shell snapshots and our own `python -m mft.daemon` all
        # mention claude somewhere in a long command line. A session's argv
        # starts with the binary.
        head = argv.split(None, 1)[0]
        if os.path.basename(head) not in ("claude", "ClaudeCode") and not (
            "/claude/versions/" in head or head.endswith("/claude")
        ):
            continue
        try:
            pid = int(raw_pid)
        except ValueError:
            continue
        found.append((pid, "" if tty in ("??", "-") else f"/dev/{tty}", argv))

    pids = [pid for pid, _, _ in found]
    cwds = _cwds(pids)
    envs = _environments(pids)
    return [
        Proc(
            pid=pid,
            tty=tty,
            cwd=cwds.get(pid, ""),
            session_id=_session_id_from_argv(argv),
            env=envs.get(pid, {}),
        )
        for pid, tty, argv in found
    ]


# --- liveness ---------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Is there still a process with this number? Signal 0 asks without sending.

    ``EPERM`` counts as alive: something is holding the number, and this is only
    ever asked about a process we started ourselves, so the answer in practice
    is that the pid was reused by something we don't own.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def orphans(
    sessions: Iterable[Session], alive: Optional[Callable[[int], bool]] = None
) -> list[Session]:
    """Sessions whose Claude process is gone, and whose encoder is now a lie.

    `SessionEnd` is the only thing that retires a slot promptly, and it is the
    one hook that routinely does not fire: a closed terminal tab, a killed
    window, a `kill -9`, a laptop that came back from a crash. What is left is a
    record on an encoder that answers to nothing, and it sits there for the full
    `SESSION_TTL_SECONDS` -- an hour of a knob describing a session you closed.
    That is the orphan you actually see: the board is empty, and one light is on.

    The check is the recorded pid and nothing else. Every path that creates a
    session with a terminal writes one -- ``register_session.py`` sends its
    ``getppid()``, which is the claude process itself, and discovery reads the
    process table directly -- so this covers nearly everything, and what it
    covers it settles outright: a pid that no longer exists is not a matter of
    interpretation. Two things were deliberately left out of it:

    *   **Matching against the live process table.** It would catch the handful
        of records that never learned a pid (an event from ``notify.sh`` for a
        session whose SessionStart the daemon missed), but the evidence there is
        the *absence* of a match rather than the presence of a death, so every
        way that matching can be wrong -- an argv shape :func:`claude_processes`
        stops recognising, a `ps` that fails to read an environment -- goes
        wrong by clearing the board. Absence of proof is not what invariant 6
        asks for. Those records keep the TTL.
    *   **Pid reuse.** A dead session's number handed to some unrelated process
        reads as alive here. It costs an hour of one stale encoder, at odds
        long enough that paying for it with a subprocess in the run loop, every
        five seconds, would be the more expensive mistake.

    Sessions that ended cleanly are skipped: their process is *supposed* to be
    gone, and they are already on the `SLOT_LINGER_SECONDS` clock that fades
    them out. Reaping those here would just cut the fade off.
    """
    check = pid_alive if alive is None else alive
    dead: list[Session] = []
    for session in sessions:
        if session.ended_at is not None:
            continue
        pids = _recorded_pids(session)
        if pids and not any(check(pid) for pid in pids):
            dead.append(session)
    return dead


def _recorded_pids(session: Session) -> list[int]:
    """Which processes this record claims to be, best evidence first.

    ``terminal`` is the current description of the tab -- `merge_terminal`
    replaces it wholesale when an arriving pid contradicts the stored one -- so
    when it names a pid, that is the pid and nothing else is consulted.
    ``keys`` is the fallback for the records that never had a terminal written
    onto them, and it is an *accumulator*: a session that outlived a restart in
    the same tab can hold the pid it used to be as well as the one it is. Any
    one of them alive is enough, which is why the caller asks whether they are
    all dead rather than whether one is.
    """
    raw = [session.terminal.get("pid")] if session.terminal.get("pid") else []
    if not raw:
        raw = [key.split(":", 1)[1] for key in session.keys if key.startswith("pid:")]
    pids = []
    for value in raw:
        try:
            pids.append(int(value))
        except (TypeError, ValueError):
            continue
    return pids


# --- transcripts ------------------------------------------------------------


def _read_transcript(path: str, modified_at: float) -> Optional[Transcript]:
    """What the tail of a transcript says about the session that owns it.

    The session id comes from the filename and is *checked* against the entries
    rather than parsed out of them: a resumed session writes its parent's id in
    old entries, and the file it is appending to is the one that matters.
    """
    session_id = os.path.basename(path)[: -len(".jsonl")]
    try:
        lines = tail_lines(path, config.DISCOVER_TAIL_BYTES)
    except OSError as exc:
        log.debug("unreadable transcript %s: %s", path, exc)
        return None

    cwd = ""
    permission_mode = ""
    for line in reversed(lines):
        if not cwd and '"cwd"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("isSidechain"):
            continue
        cwd = cwd or str(entry.get("cwd") or "")
        permission_mode = permission_mode or str(entry.get("permissionMode") or "")
        if cwd and permission_mode:
            break
    if not cwd:
        return None
    return Transcript(session_id, path, cwd, modified_at, permission_mode)


def recent_transcripts(
    projects_dir: str = "", window: float = 0.0, now: float = 0.0
) -> list[Transcript]:
    """Every transcript touched inside the window, most recent first."""
    projects_dir = projects_dir or config.CLAUDE_PROJECTS_DIR
    window = window or config.DISCOVER_WINDOW_SECONDS
    now = now or time.time()

    fresh: list[tuple[float, str]] = []
    try:
        projects = os.scandir(projects_dir)
    except OSError as exc:
        log.debug("no projects dir at %s: %s", projects_dir, exc)
        return []
    with projects:
        for project in projects:
            if not project.is_dir():
                continue
            try:
                entries = list(os.scandir(project.path))
            except OSError:
                continue
            for entry in entries:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    modified_at = entry.stat().st_mtime
                except OSError:
                    continue
                if now - modified_at <= window:
                    fresh.append((modified_at, entry.path))

    fresh.sort(reverse=True)
    found = [_read_transcript(path, modified_at) for modified_at, path in fresh]
    return [t for t in found if t is not None]


# --- the join ---------------------------------------------------------------


def discover(
    processes: Optional[list[Proc]] = None,
    projects_dir: str = "",
    now: float = 0.0,
) -> list[Discovered]:
    """Live sessions, most recently active first.

    Matching runs in two passes. Sessions whose argv names them are exact and
    are claimed first; the rest are matched by working directory, newest
    transcript to spare process, which is the best available guess at "this
    process is the one appending to this file".
    """
    procs = claude_processes() if processes is None else processes
    if procs is None:
        log.warning("could not read the process table; skipping discovery")
        return []
    if not procs:
        return []

    transcripts = recent_transcripts(projects_dir, now=now)
    if not transcripts:
        return []

    named = {p.session_id: p for p in procs if p.session_id}
    unclaimed: dict[str, list[Proc]] = {}
    for proc in procs:
        if not proc.session_id and proc.cwd:
            unclaimed.setdefault(proc.cwd, []).append(proc)

    found: list[Discovered] = []
    matched_by_cwd: dict[str, list[str]] = {}
    for transcript in transcripts:
        proc = named.pop(transcript.session_id, None)
        if proc is None:
            spare = unclaimed.get(transcript.cwd)
            if not spare:
                continue
            proc = spare.pop(0)
            matched_by_cwd.setdefault(transcript.cwd, []).append(transcript.session_id)
        found.append(
            Discovered(
                session_id=transcript.session_id,
                cwd=transcript.cwd,
                transcript_path=transcript.path,
                terminal=proc.terminal,
                permission_mode=transcript.permission_mode,
            )
        )

    # A cwd that matched more than once is a directory with several sessions
    # open, and nothing on disk says which tty belongs to which. Guessing would
    # hand the wrong encoder to the next `/clear` in either tab, so those
    # sessions give up their terminal identity and keep only their slot.
    ambiguous = {
        session_id
        for cwd, ids in matched_by_cwd.items()
        if len(ids) > 1
        for session_id in ids
    }
    if not ambiguous:
        return found
    log.info("%d discovered sessions could not be pinned to a tab", len(ambiguous))
    return [
        d
        if d.session_id not in ambiguous
        else Discovered(
            session_id=d.session_id,
            cwd=d.cwd,
            transcript_path=d.transcript_path,
            terminal={},
            permission_mode=d.permission_mode,
        )
        for d in found
    ]


#: The keys that name an *application* rather than a tab. What is left of a
#: terminal identity when two sessions in one directory can't be told apart.
_APP_KEYS = ("TERM_PROGRAM", "__CFBundleIdentifier")


def resolve_terminal(
    cwd: str,
    pid: str = "",
    claimed: frozenset[str] = frozenset(),
    processes: Optional[list[Proc]] = None,
) -> dict[str, str]:
    """Look a session's terminal up in the process table, right now.

    The SessionStart hook is the good path, but it only fires once: a session
    that was already running when the daemon started, or that was running when
    the daemon was restarted, has no captured environment and no way to send
    one. Without this it stays unfocusable for its entire life. With it, the
    press itself goes and finds the tab.

    ``pid`` (a previously recorded one) is trusted first and re-read, which
    also upgrades the bare ``{pid, tty}`` that older discovery recorded into a
    full environment. Otherwise the session is matched on its working
    directory, and only when exactly one unclaimed process is sitting in it --
    two Claudes in one directory look identical from out here, and pointing a
    knob at the wrong tab is worse than pointing it at none. Even then the
    application they share is worth returning: raising the right window is
    still better than doing nothing.
    """
    procs = claude_processes() if processes is None else processes
    if not procs:
        return {}

    if pid:
        for proc in procs:
            if str(proc.pid) == str(pid) and (not cwd or not proc.cwd or proc.cwd == cwd):
                return proc.terminal

    if not cwd:
        return {}
    candidates = [p for p in procs if p.cwd == cwd and str(p.pid) not in claimed]
    if len(candidates) == 1:
        return candidates[0].terminal
    if not candidates:
        return {}

    shared = {
        key: candidates[0].env[key]
        for key in _APP_KEYS
        if candidates[0].env.get(key)
        and all(p.env.get(key) == candidates[0].env.get(key) for p in candidates)
    }
    log.info(
        "%d Claudes are running in %s; falling back to %s",
        len(candidates),
        cwd,
        shared or "nothing",
    )
    return shared


def adopt(table: SessionTable, found: list[Discovered]) -> list[Session]:
    """Put discovered sessions on the board.

    They land as ``idle``: a transcript records what a session *did*, never what
    it is doing now, and inventing an attention state from a file would strobe
    an encoder red at you for a prompt that was answered before the daemon
    started. The first real hook event finds the session by id and takes over.
    """
    adopted: list[Session] = []
    for entry in found:
        # A hook event can beat discovery to the same session, and what it says
        # is live where this is reconstructed -- so a session that already
        # exists keeps its own terminal identity rather than being rekeyed onto
        # a tab this guessed at.
        known = table.get(entry.session_id) is not None
        terminal = None if known else (entry.terminal or None)
        session = table.ensure(entry.session_id, entry.cwd, terminal)
        if session is None:
            log.info("no free encoder for discovered session %s", entry.session_id[:8])
            break
        session.terminal = session.terminal or dict(entry.terminal)
        session.transcript_path = session.transcript_path or entry.transcript_path
        session.permission_mode = session.permission_mode or entry.permission_mode
        adopted.append(session)
    return adopted
