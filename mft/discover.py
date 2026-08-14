"""Find the agent sessions that were already running when we started.

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
    :func:`mft.identity.terminal_keys` wants for a durable slot.

A session's *subagents* are a third file each, under
``<session-id>/subagents/agent-<agent_id>.jsonl``, and :func:`subagents` reads
them the same way for the same reason: the pile is exactly the state a restart
mid-fan-out loses hardest, because every one of those subagents will announce
its *end* to a daemon that never heard it start.

Neither is sufficient alone, so nothing is adopted without both. The bias
throughout is toward showing too little: a missing encoder is a session you
find in a moment anyway, while a phantom one is a knob that lies until the TTL
reaps it an hour later.

The same process table answers the opposite question, which is why
:func:`orphans` lives here too: a session on the board whose claude is gone.
Adoption is a guess that has to be right; that one is a fact, and it is checked
on a much tighter loop -- see its docstring for why it is deliberately narrower
than everything else in this file.

:func:`census` is the middle clock between them. Adoption runs at boot and on
wake; the pid check runs constantly and for free; this reads the whole process
table every half minute and spends it on the things neither of those can do --
giving a pid to the records that never had one (:func:`learn_pids`), taking a
tab back from a record that called it a host process (:func:`relabel_hosts`),
noticing that a tty on the board belongs to nobody, and counting the records in
a directory against the Claudes actually running there (:func:`phantoms`). The
tty one is the only *absence* anything here draws a conclusion from, and it is
allowed to because it recognises nothing: a closed tab frees its pty, and
reading which ttys are in use does not depend on knowing what a Claude process
looks like.

The join at the heart of Claude adoption is a guess, and :func:`phantoms` is what keeps
a wrong one from lasting: nothing on disk says which of a directory's transcripts
belongs to which of its processes, so a session that exited an hour ago can be
matched to a live Claude and put on the board in place of the session that really
owns it. See that function for what makes the arithmetic safe to act on. Codex
adoption is isolated at the bottom of this module: its App Server supplies the
live thread metadata, which is joined narrowly to the same process-table tty
evidence.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Optional

from . import config
from .context import tail_lines
from .identity import is_hostless, merge_terminal, terminal_keys
from .state import AGENT_KEY, Session, SessionTable, Subagent

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
    #: What the tail says the agent is doing, before the freshness gate in
    #: :func:`discover` decides whether to believe it. See :func:`activity`.
    state: str = ""


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
    #: The state to adopt into, or ``""`` for the resting default. Only ever one
    #: of the busy states; see :func:`activity`.
    state: str = ""
    #: The subagents still in flight, each mapped to ``(idle_for, out_for)`` --
    #: how long ago it was last seen writing, and how long ago it was spawned.
    #: Ages rather than timestamps; see :func:`subagents`.
    subagents: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: Added at the end to preserve the positional constructor used by older
    #: callers and fixtures.
    provider: str = "claude"
    title: str = ""


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


def process_rows() -> Optional[list[tuple[int, str, str]]]:
    """Every process on the machine as ``(pid, tty, argv)``, or ``None``.

    Split out from :func:`claude_processes` because the same read answers a
    second question that has nothing to do with recognising Claude: *which
    ttys are still in use*. See :func:`census` for why that distinction is the
    load-bearing one -- a tty column needs no knowledge of what a session's
    argv looks like, so it keeps working on the day that knowledge goes stale.
    """
    out = _run(["ps", "-eo", "pid=,tty=,command="])
    if out is None:
        return None
    rows: list[tuple[int, str, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        raw_pid, tty, argv = parts
        try:
            pid = int(raw_pid)
        except ValueError:
            continue
        rows.append((pid, "" if tty in ("??", "-") else f"/dev/{tty}", argv))
    return rows


def claude_processes(
    rows: Optional[list[tuple[int, str, str]]] = None,
) -> Optional[list[Proc]]:
    """Every live Claude Code session process.

    ``None`` means the process table could not be read at all, which is a very
    different thing from "nothing is running" -- discovery refuses to adopt
    anything in that case rather than trusting transcripts on their own.
    """
    rows = process_rows() if rows is None else rows
    if rows is None:
        return None

    found: list[tuple[int, str, str]] = []
    for pid, tty, argv in rows:
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
        found.append((pid, tty, argv))

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


def _codex_rows(rows: list[tuple[int, str, str]]) -> list[tuple[int, str, str]]:
    """Interactive Codex rows, without the costly cwd/environment reads."""
    found: list[tuple[int, str, str]] = []
    for pid, tty, argv in rows:
        parts = argv.split()
        if not parts or os.path.basename(parts[0]) != "codex":
            continue
        if any(token in parts[1:3] for token in ("app-server", "exec", "mcp-server")):
            continue
        found.append((pid, tty, argv))
    return found


def codex_process_ids(
    rows: Optional[list[tuple[int, str, str]]] = None,
) -> Optional[frozenset[int]]:
    """Pids of live interactive Codex CLIs, cheap enough for a 1Hz watcher."""
    rows = process_rows() if rows is None else rows
    if rows is None:
        return None
    return frozenset(pid for pid, _, _ in _codex_rows(rows))


def codex_processes(
    rows: Optional[list[tuple[int, str, str]]] = None,
) -> Optional[list[Proc]]:
    """Live interactive Codex CLI processes, excluding helper subcommands."""
    rows = process_rows() if rows is None else rows
    if rows is None:
        return None
    found = _codex_rows(rows)
    pids = [pid for pid, _, _ in found]
    cwds = _cwds(pids)
    envs = _environments(pids)
    uuid = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,}$", re.I)
    result = []
    for pid, tty, argv in found:
        explicit = next((p for p in argv.split() if uuid.match(p)), "")
        result.append(Proc(pid, tty, cwds.get(pid, ""), explicit, envs.get(pid, {})))
    return result


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


@dataclass(frozen=True)
class Census:
    """One read of the process table, taken for the whole board at once.

    Two answers out of one `ps`, and they are worth very different amounts:

    *   ``procs`` is the Claude processes, which is a *recognition* -- it rests
        on :func:`claude_processes` still knowing what a session's argv looks
        like, and that is a thing Claude Code can change under us.
    *   ``ttys`` is every tty any process on the machine is sitting on, which
        recognises nothing. A terminal tab holds its pty for exactly as long as
        it is open: close it and the pty is freed, and no process anywhere is on
        it again. That makes "this session's tty is in use by nobody" a fact
        about the tab rather than a guess about the session -- which is the
        whole reason this exists, because it is the one negative that stays true
        when everything else here goes stale.

    :attr:`usable` is the self-check that makes the negative safe to act on: a
    real machine has hundreds of processes and at least one terminal, so a table
    that has neither is a read that went wrong rather than a desk that emptied.
    """

    procs: list[Proc]
    ttys: frozenset[str]
    #: How many rows the `ps` returned, before any filtering.
    size: int

    @property
    def usable(self) -> bool:
        return self.size >= config.CENSUS_MIN_ROWS and bool(self.ttys)


def census(rows: Optional[list[tuple[int, str, str]]] = None) -> Optional[Census]:
    """Take one. ``None`` if the process table could not be read at all."""
    rows = process_rows() if rows is None else rows
    if rows is None:
        return None
    procs = claude_processes(rows) or []
    return Census(
        procs=procs,
        ttys=frozenset(tty for _, tty, _ in rows if tty),
        size=len(rows),
    )


def _recorded_tty(session: Session) -> str:
    """The pty this session says it lives on, if it named one."""
    tty = str(session.terminal.get("tty") or "")
    if not tty:
        tty = next(
            (key.split(":", 1)[1] for key in session.keys if key.startswith("tty:")),
            "",
        )
    return tty if tty.startswith("/dev/") else ""


def learn_pids(
    sessions: Iterable[Session], procs: list[Proc]
) -> list[tuple[Session, Proc]]:
    """Give a live process id to the records that never got one.

    The orphan sweep settles a session outright when it knows which process to
    ask about, and does nothing at all when it doesn't -- so the cheapest way to
    widen it is not to weaken the test but to leave fewer records outside it.
    ``notify.sh`` reports a tab's identity without a pid (it will not spawn a
    process to learn one), so a session whose `SessionStart` never reached the
    daemon -- daemon down when the tab opened, daemon restarted mid-turn -- runs
    its whole life on records the sweep has to skip.

    This is presence of evidence in both directions: a session is matched only
    to a process that is running *now*, and the pid written onto it is that
    process's. Matching goes strongest-first and every step must be unambiguous;
    a session that two processes could equally be is left alone, because a wrong
    pid here is worse than none -- it would answer the sweep's question with
    somebody else's life.

    What gets written on is the process's whole :attr:`Proc.terminal`, not only
    its number. A pid on its own *is* a description -- ``{"pid": N}`` is exactly
    what :func:`mft.identity.is_hostless` calls a process with no terminal behind
    it -- so writing one onto a record with nothing else on it filed a tab under
    ``host:`` instead of ``pid:`` + ``tty:``, in a namespace that can never match
    the tab's own record and that :func:`orphans` treats as immune. The tty was
    on hand the whole time; the fix is to say the rest of what we found.

    Returns the pairs it made; the caller writes them into the table's index by
    reconciling, the same way :func:`adopt` does.
    """
    live = [s for s in sessions if s.ended_at is None]
    # A process another record is already pinned to is spoken for, and so is a
    # directory two pidless records are both sitting in: the weakest match below
    # is "the only Claude in this cwd", and it is only true when it is the only
    # *session* there too.
    claimed = {pid for s in live for pid in _recorded_pids(s)}
    spare = [p for p in procs if p.pid not in claimed]
    wanting = [s for s in live if not _recorded_pids(s)]
    crowded = {
        s.cwd for s in wanting if s.cwd and sum(1 for o in wanting if o.cwd == s.cwd) > 1
    }

    matched: list[tuple[Session, Proc]] = []
    for session in wanting:
        found = _match(session, spare, by_cwd=session.cwd not in crowded)
        if found is None:
            continue
        spare.remove(found)
        session.terminal = _describe(session.terminal, found)
        matched.append((session, found))
    return matched


def _describe(stored: dict[str, Any], found: Proc) -> dict[str, Any]:
    """Fold what the process table knows about a process onto a record.

    ``merge_terminal`` declines to let a bare pid overwrite a description of a
    tab, which is the right answer for a hook payload and the wrong one here:
    that pid is the thing this record was missing. So the merge decides the tab
    fields and the pid is written back afterwards either way.
    """
    described = merge_terminal(dict(stored or {}), found.terminal)
    described["pid"] = str(found.pid)
    return described


def relabel_hosts(
    sessions: Iterable[Session], procs: list[Proc]
) -> list[tuple[Session, Proc]]:
    """Give a tab back to a record that is calling its own process a *host*.

    A record whose only token is ``host:N`` is saying it has no terminal of its
    own -- it was handed a conversation by a process somewhere else. When pid N
    turns out to be sitting on a tty, that is not what happened: the record
    described itself wrong, and it is stuck that way. ``host:N`` never matches
    the tab's own ``pid:N`` (:func:`mft.identity.terminal_keys` keeps the two
    namespaces apart on purpose), so `SessionTable.reconcile` sees two unrelated
    terminals, and a live ``host:`` pid makes :func:`orphans` skip the tty test
    -- the encoder is immune to every way the board has of letting go of one.

    Nothing is guessed here: the pid is the record's own, and this only recovers
    the tty and environment already sitting next to it in the process table. A
    genuine handoff is untouched, because the process it names is one of
    :data:`NOT_A_SESSION` and never appears in a census at all.

    Returns the pairs it repaired, for the caller to log and reconcile.
    """
    live = [s for s in sessions if s.ended_at is None]
    by_pid = {p.pid: p for p in procs if p.tty}
    repaired: list[tuple[Session, Proc]] = []
    for session in live:
        # A record already describing a tab has the better answer, even when it
        # also holds a ``host:`` token -- that is a session handed off *out of*
        # a terminal, and both halves of it are true.
        if session.terminal and not is_hostless(session.terminal):
            continue
        pids = _pid_sources(session)[1]
        if is_hostless(session.terminal):
            pids = _numbers([session.terminal["pid"]]) + pids
        for pid in dict.fromkeys(pids):
            found = by_pid.get(pid)
            if found is None:
                continue
            session.terminal = _describe(session.terminal, found)
            repaired.append((session, found))
            break
    return repaired


def _match(session: Session, procs: list[Proc], by_cwd: bool = True) -> Optional[Proc]:
    """Which of these processes is this session, if exactly one of them is."""
    named = [p for p in procs if p.session_id and p.session_id == session.session_id]
    if len(named) == 1:
        return named[0]
    # A token the two descriptions share: the tab named itself to the hook and
    # names itself again in the process table's environment.
    if session.keys:
        shared = [p for p in procs if session.keys & set(terminal_keys(p.terminal))]
        if len(shared) == 1:
            return shared[0]
    # Last and weakest, and only when it is the only Claude in the directory --
    # two of them there are indistinguishable from out here.
    if by_cwd and session.cwd:
        here = [p for p in procs if p.cwd and p.cwd == session.cwd]
        if len(here) == 1:
            return here[0]
    return None


def orphans(
    sessions: Iterable[Session],
    alive: Optional[Callable[[int], bool]] = None,
    taken: Optional[Census] = None,
) -> list[Session]:
    """Sessions whose Claude process is gone, and whose encoder is now a lie.

    `SessionEnd` is the only thing that retires a slot promptly, and it is the
    one hook that routinely does not fire: a closed terminal tab, a killed
    window, a `kill -9`, a laptop that came back from a crash. What is left is a
    record on an encoder that answers to nothing, and it sits there for the full
    `SESSION_TTL_SECONDS` -- an hour of a knob describing a session you closed.
    That is the orphan you actually see: the board is empty, and one light is on.

    Two facts, either of which settles it, and neither of which is a guess:

    1.  **Every pid it recorded is gone.** Signal 0, no subprocess, cheap enough
        to run on the reaper's five seconds. Every path that creates a session
        with a terminal writes a pid -- ``register_session.py`` sends its
        ``getppid()``, which is the claude process itself, and discovery reads
        the process table directly -- and a pid that no longer exists is not a
        matter of interpretation.
    2.  **Its tty belongs to nobody.** Only when a `Census` is supplied, so only
        on that slower clock. A terminal tab holds its pty open for exactly as
        long as it is open; once it closes, no process on the machine is on that
        tty again. So a session that named a tty and whose tty is now free had
        its tab closed, whatever any pid says.

    The second is what the first cannot reach. ``notify.sh`` names a tab without
    naming a process, so a session whose `SessionStart` never got through has a
    tty and no pid, and fact 1 has nothing to ask about; :func:`learn_pids`
    shrinks that set and this closes what is left of it. It also settles pid
    reuse, which fact 1 gets wrong in the dangerous direction: a recycled number
    reads as alive, but a live claude is on its tty by definition, so a recorded
    tty that is free contradicts it and the tty wins.

    Deliberately still not concluded:

    *   **Anything from a Claude process we failed to recognise.** Comparing
        each session against the *live* claude processes would catch a record
        with neither pid nor tty, but that evidence rests on
        :func:`claude_processes` still knowing what a session's argv looks like,
        so the day that changes it clears the board. The tty test needs no such
        knowledge -- it reads a column, not a command line -- which is exactly
        why it is the one absence trusted here. Records with neither keep the
        TTL.
    *   **A session handed off into someone else's process.** A live ``host:``
        pid is a session running outside the tab it came from
        (:meth:`mft.state.SessionTable._handed_off`), so its tab's tty going
        free is the tab closing and not the session ending. It keeps its
        encoder; see :func:`_pid_sources`.

    Sessions that ended cleanly are skipped: their process is *supposed* to be
    gone, and they are already on the `SLOT_LINGER_SECONDS` clock that fades
    them out. Reaping those here would just cut the fade off.
    """
    check = pid_alive if alive is None else alive
    trust_ttys = taken is not None and taken.usable
    dead: list[Session] = []
    for session in sessions:
        if session.ended_at is not None:
            continue
        tab_pids, host_pids = _pid_sources(session)
        pids = tab_pids + host_pids
        if pids and not any(check(pid) for pid in pids):
            dead.append(session)
            continue
        if not trust_ttys or any(check(pid) for pid in host_pids):
            continue
        tty = _recorded_tty(session)
        if tty and tty not in taken.ttys:  # type: ignore[union-attr]
            dead.append(session)
    return dead


def phantoms(
    sessions: Iterable[Session], taken: Optional[Census] = None
) -> list[Session]:
    """Records describing nobody, in a directory with no room left for them.

    The hole :func:`orphans` cannot reach, and the one that produced the knob
    this function was written for. Adoption joins transcripts to processes by
    working directory, newest transcript first (:func:`discover`), and that join
    is a guess whenever a directory holds more recent transcripts than live
    Claudes: a session that exited twenty minutes ago has a *newer* transcript
    than one that has been sitting at a prompt since lunch, so the dead one is
    matched and the live one is missed. Then, because the directory matched more
    than once, the ambiguity rule strips the terminal identity off everything it
    matched -- deliberately, so a wrong tab is never recorded -- and the result
    is a record with no tty and no pid. Both of `orphans`' facts need one of
    those to ask about, so the phantom is immune to the sweep and holds its
    encoder for the full `SESSION_TTL_SECONDS`. An hour of a knob describing a
    session that ended before the daemon started.

    What settles it is counting, and only in a directory where the counting is
    safe. Every live Claude in that cwd that some *identified* record already
    claims -- by pid or by tty -- is spoken for. When all of them are, a record
    there with no identity at all has nothing left to be, because there is no
    process left for it to be running as.

    Three things keep this from being the "compare the board against the live
    processes" test that :func:`orphans` deliberately refuses:

    *   It concludes nothing in a directory where no Claude was recognised. That
        is the failure mode of recognition going stale -- `claude_processes` no
        longer knowing what a session's argv looks like -- and it reads here as
        "no evidence", never as "nobody is there".
    *   It only ever releases a record that names *nothing*: no tty, no pid, no
        terminal at all. A record that describes a tab is answered by the sweep
        that can check it, and is left alone here whatever the arithmetic says.
    *   Every process must be claimed by a record that is itself identified, so
        one phantom cannot account for another.

    Run it after :func:`orphans` on the same census, so that a record still
    holding a dead pid has already gone rather than counting as a claim.
    """
    if taken is None or not taken.usable:
        return []
    live = [s for s in sessions if s.ended_at is None]
    procs_by_cwd: dict[str, list[Proc]] = {}
    for proc in taken.procs:
        if proc.cwd:
            procs_by_cwd.setdefault(proc.cwd, []).append(proc)

    found: list[Session] = []
    for cwd, procs in procs_by_cwd.items():
        here = [s for s in live if s.cwd == cwd]
        nameless = [s for s in here if not s.keys and not s.terminal]
        if not nameless:
            continue
        claimed: set[int] = set()
        for session in here:
            if session in nameless:
                continue
            claimed |= {p.pid for p in _claimed_procs(session, procs)}
        if len(claimed) >= len(procs):
            found.extend(nameless)
    return found


def _claimed_procs(session: Session, procs: list[Proc]) -> list[Proc]:
    """Which of these processes this record says it is -- by number or by pty."""
    pids = set(_recorded_pids(session))
    tty = _recorded_tty(session)
    return [p for p in procs if p.pid in pids or (tty and p.tty == tty)]


def epitaph(
    session: Session,
    taken: Optional[Census] = None,
    alive: Optional[Callable[[int], bool]] = None,
) -> str:
    """Which of :func:`orphans`' two facts settled this one, for the log.

    Worth the few lines: the two are found on different clocks and mean
    different things to whoever is reading the log at the time -- a dead pid is
    a session that exited, a freed tty is a window that closed -- and one
    wording for both spent a while claiming `pid None` had gone.
    """
    check = pid_alive if alive is None else alive
    pids = _recorded_pids(session)
    if pids and not any(check(pid) for pid in pids):
        return f"pid {', '.join(str(pid) for pid in pids)} is gone"
    tty = _recorded_tty(session)
    if taken is not None and tty and tty not in taken.ttys:
        return f"the tab on {tty} is closed"
    return "it is gone"


def _recorded_pids(session: Session) -> list[int]:
    """Every process this record claims to be. See :func:`_pid_sources`."""
    tab_pids, host_pids = _pid_sources(session)
    return tab_pids + host_pids


def _pid_sources(session: Session) -> tuple[list[int], list[int]]:
    """Which processes this record claims to be, split by what they mean.

    The two are told apart because the tty test needs to: the tab's own pid dying
    is a session ending, while a ``host:`` pid living is a session that has left
    its tab and is running somewhere else, and only the second has anything to
    say about whether a freed pty is bad news.

    ``terminal`` is the current description of the tab -- `merge_terminal`
    replaces it wholesale when an arriving pid contradicts the stored one -- so
    when it names a pid, that is the pid and nothing else is consulted.
    ``keys`` is the fallback for the records that never had a terminal written
    onto them, and it is an *accumulator*: a session that outlived a restart in
    the same tab can hold the pid it used to be as well as the one it is. Any
    one of them alive is enough, which is why the caller asks whether they are
    all dead rather than whether one is.

    A ``host:`` token is added to whichever of those two answers, never instead
    of it: a session handed off into a process under Claude Code's background
    daemon (:meth:`mft.state.SessionTable._handed_off`) is described by its
    tab's terminal and running in someone else's process, so the record names
    two live things and the death of the tab's own is not the session ending.
    Leaving it out reaped the encoder of a working session the moment the
    terminal's claude exited -- the exact lie this function exists to prevent,
    through the other door.
    """
    raw = [session.terminal.get("pid")] if session.terminal.get("pid") else []
    if not raw:
        raw = [key.split(":", 1)[1] for key in session.keys if key.startswith("pid:")]
    hosts = [key.split(":", 1)[1] for key in session.keys if key.startswith("host:")]
    return _numbers(raw), _numbers(hosts)


def _numbers(values: Iterable[Any]) -> list[int]:
    found = []
    for value in values:
        try:
            found.append(int(value))
        except (TypeError, ValueError):
            continue
    return found


# --- transcripts ------------------------------------------------------------


#: Entry types that are a turn happening. Everything else in a transcript --
#: `ai-title`, `mode`, `permission-mode`, `last-prompt`, `attachment`, `summary`
#: -- is metadata written alongside the conversation, and several of them are
#: appended *after* the last message, which is why the scan below looks for a
#: type rather than taking the final line.
_CONVERSATION = ("user", "assistant")


def activity(lines: list[str], sidechain: bool = False) -> str:
    """What the tail of a transcript says the agent is doing *now*, or ``""``.

    The premise `adopt` was written without: a transcript is not only a record of
    what a session did. Claude Code writes the assistant message carrying a
    `tool_use` block as soon as it has it, which is *before* the tool runs, so a
    session in the middle of a tool call has that call sitting unanswered at the
    end of its file. The last conversational entry is therefore a live reading,
    and it is the only one available for a session that started before we did.

    Five shapes, and only the first four are worth anything:

    *   An assistant message ending in a `tool_use` -- a call is out and nothing
        has come back.
    *   An assistant message with no `stop_reason` at all -- it is still being
        written, so the agent has the floor with no call out.
    *   A user message carrying a `tool_result` -- the call came back and the
        agent has the floor again.
    *   A user message carrying prose -- a prompt was submitted and nothing has
        answered it yet.
    *   Anything else, which in practice means an assistant message that stopped
        on `end_turn`: the turn is over, and there is nothing here the resting
        default doesn't already say.

    What this deliberately cannot do is find a **permission prompt**, which is
    the session `mft.discover` most wants and the reason its module docstring
    opens the way it does. An agent blocked on your approval and an agent
    halfway through a slow `npm test` write byte-identical tails -- the request
    is the tool call, and whether a human is being asked about it is not in the
    file. Guessing would strobe an encoder red at a session nobody needs to look
    at, which is the phantom invariant 6 is about, so both read as `working` and
    the real answer arrives with the next hook.

    `thinking` and `working` are both honest at the resolution the board renders
    them: one is "no tool call is out", the other is "one is".

    ``sidechain`` swaps which half of a conversation is read: the session's own
    messages, or a subagent's. It is a swap and not a widening because the two
    never belong to the same reading -- a subagent's turn ending says nothing
    about its parent, and the parent's tail is what :func:`discover` wants.
    Modern Claude Code writes them to separate files anyway; the flag stays
    because the entries are still marked, and :func:`subagents` reads a file
    where *every* entry carries it.
    """
    for line in reversed(lines):
        # Cheap gate. Every conversational entry names its role, and the
        # metadata records that trail the file do not.
        if '"user"' not in line and '"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = entry.get("type")
        if kind not in _CONVERSATION or bool(entry.get("isSidechain")) != sidechain:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        blocks = (
            {b.get("type") for b in content if isinstance(b, dict)}
            if isinstance(content, list)
            else set()
        )
        if kind == "assistant":
            # `stop_reason` is the authority when it is there, but a streamed
            # message can reach disk before it has one -- which is exactly the
            # in-flight case this exists to catch -- so the block is enough.
            if message.get("stop_reason") == "tool_use" or "tool_use" in blocks:
                return "working"
            # Still no `stop_reason` and no call out yet: a message being
            # streamed right now that has only got as far as its thinking. The
            # missing reason is the whole tell -- a turn that is over always
            # names one -- and reading this shape as "over" is what dropped a
            # live subagent out of the pile every time we looked at it
            # mid-thought, since `subagents` uses this as a hard filter.
            # Safe against a writer that omits the field entirely, because
            # every caller gates on the file's mtime first: `_believable` and
            # `subagents` both discard a reading older than
            # DISCOVER_ACTIVE_SECONDS, so a stale tail cannot hold a knob lit.
            if not message.get("stop_reason"):
                return "thinking"
            return ""
        if "tool_result" in blocks:
            return "working"
        # A plain user message: a prompt with no answer yet. `tool_result` is
        # ruled out above, so this is a human turn and not the harness replying
        # to a call.
        return "thinking"
    return ""


#: Where a session's subagents keep their own transcripts: a directory named
#: after the session id, beside the session's own file, holding one
#: `agent-<agent_id>.jsonl` each. The id in that filename is the same one hooks
#: send as `agent_id`, which is the whole reason this is recoverable -- a
#: rebuilt key is indistinguishable from one a `SubagentStart` created, so the
#: `SubagentStop` that eventually arrives pops the dot it was meant to pop.
_SUBAGENT_DIR = "subagents"
_SUBAGENT_FILE = re.compile(r"^agent-(.+)\.jsonl$")


def subagents(transcript_path: str, now: float = 0.0) -> dict[str, tuple[float, float]]:
    """The subagents of one session that are still going, how stale each is, and
    how long each has been out.

    :func:`activity` applied once per subagent, for the same reason and with the
    same caveat. A subagent that is still working has an unanswered `tool_use`
    at the end of its file; one that has handed its answer back stopped on
    `end_turn`. Nothing else on disk distinguishes them, and nothing needs to:
    the pile is a count of who is out, not a record of what they did.

    Keyed the way hooks key it (see :data:`_SUBAGENT_DIR`) and ordered by
    creation, so a rebuilt pile stacks in spawn order rather than in whatever
    order the directory happened to be read -- a dot that moves knobs is a dot
    you have to re-read.

    The values are **ages**, not timestamps, and that is deliberate. A file's
    mtime is wall clock and every deadline on the board is
    :func:`time.monotonic`; handing a session one of those as if it were the
    other would put a subagent's last tool call days in the future. Converting
    is :func:`adopt`'s job, because it is the one holding both clocks.

    Two ages, because the ring wants the second one. The subagent's own file is
    born when it spawns and touched as it writes, so mtime is the activity and
    birth time is the spawn -- the same tie-break already read below for spawn
    order, spent twice. Without it, a daemon restarted into a fan-out that has
    been grinding for half an hour adopts a pile of empty rings, which is the
    exact reading the stopwatch exists to give and the exact moment it is worth
    most. `st_birthtime` is a macOS luxury; where it is missing this degrades to
    mtime, which says the subagent has been out no longer than it has been quiet
    -- an understatement, and invariant 6's preferred direction.

    Anything the freshness gate drops is left off entirely rather than adopted
    flat. A subagent killed along with the daemon leaves a file that says
    "mid-tool-call" for as long as the disk survives, and invariant 6 says the
    missing dot is the cheaper mistake.
    """
    if not config.DISCOVER_SUBAGENTS or not transcript_path.endswith(".jsonl"):
        return {}
    now = now or time.time()
    root = os.path.join(transcript_path[: -len(".jsonl")], _SUBAGENT_DIR)
    try:
        entries = list(os.scandir(root))
    except OSError:
        # The ordinary case by a long way: a session that spawned nobody has no
        # such directory. Not worth a log line per session per adoption.
        return {}

    live: list[tuple[float, str, tuple[float, float]]] = []
    for entry in entries:
        found = _SUBAGENT_FILE.match(entry.name)
        if not found:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        age = now - stat.st_mtime
        if age > config.DISCOVER_ACTIVE_SECONDS:
            continue
        try:
            lines = tail_lines(entry.path, config.DISCOVER_TAIL_BYTES)
        except OSError as exc:
            log.debug("unreadable subagent transcript %s: %s", entry.path, exc)
            continue
        if not activity(lines, sidechain=True):
            continue
        # Creation time is spawn order, and now also how long the dot has been
        # out. macOS has it; the fallback keeps this from being a platform
        # assumption for the sake of a tie-break.
        born = getattr(stat, "st_birthtime", stat.st_mtime)
        # Never younger than it is quiet: a clock skew that put the two the other
        # way round would draw a ring shorter than the silence it sits beside.
        out_for = max(0.0, age, now - born)
        live.append((born, f"{AGENT_KEY}{found.group(1)}", (max(0.0, age), out_for)))

    live.sort()
    return {key: ages for _, key, ages in live}


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
    state = activity(lines) if config.DISCOVER_STATES else ""
    return Transcript(session_id, path, cwd, modified_at, permission_mode, state)


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
    now = now or time.time()
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
                state=_believable(transcript, now),
                subagents=subagents(transcript.path, now),
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
        d if d.session_id not in ambiguous else replace(d, terminal={}) for d in found
    ]


def discover_codex(
    processes: Optional[list[Proc]] = None,
    threads: Optional[list[Any]] = None,
    now: float = 0.0,
    window: float = 0.0,
) -> list[Discovered]:
    """Conservatively join stable App Server thread metadata to live CLIs."""
    from . import codex

    now = now or time.time()
    procs = codex_processes() if processes is None else processes
    if not procs:
        return []
    rows = codex.list_threads() if threads is None else threads
    window = window or config.DISCOVER_WINDOW_SECONDS
    recent = [t for t in rows if not t.updated_at or now - t.updated_at <= window]
    by_id = {t.thread_id: t for t in recent}
    by_session = {t.session_id: t for t in recent}
    by_cwd: dict[str, list[Any]] = {}
    for thread in recent:
        by_cwd.setdefault(thread.cwd, []).append(thread)

    found: list[Discovered] = []
    for proc in procs:
        thread = by_id.get(proc.session_id) or by_session.get(proc.session_id)
        if thread is None and proc.cwd:
            candidates = by_cwd.get(proc.cwd, [])
            same_cwd_procs = [p for p in procs if p.cwd == proc.cwd]
            # Refuse an assignment unless both sides of the terminal/thread join
            # are unique. A dark encoder is recoverable; a wrong focus target is not.
            if len(candidates) == 1 and len(same_cwd_procs) == 1:
                thread = candidates[0]
        if thread is None:
            continue
        found.append(Discovered(
            session_id=thread.thread_id,
            cwd=thread.cwd,
            transcript_path=thread.path,
            provider="codex",
            title=thread.title,
            terminal=proc.terminal,
        ))
    return found


def discover_codex_starts(
    pids: Iterable[int], processes: Optional[list[Proc]] = None
) -> list[Discovered]:
    """Live Codex processes as provisional sessions, before the first prompt.

    App Server does not publish a newly opened TUI until it has a prompt, which
    is exactly the boundary this path exists to get ahead of.  A process still
    gives us the two facts the board needs safely: it is alive, and its tty
    names the terminal that owns the encoder.  The temporary id is replaced by
    the first real hook through :meth:`mft.state.SessionTable.ensure`'s normal
    same-terminal handoff.
    """
    wanted = frozenset(pids)
    rows = codex_processes() if processes is None else processes
    if not rows:
        return []
    return [
        Discovered(
            session_id=f"startup-pid-{proc.pid}",
            cwd=proc.cwd,
            transcript_path="",
            provider="codex",
            terminal=proc.terminal,
        )
        for proc in rows
        if proc.pid in wanted
    ]


def _believable(transcript: Transcript, now: float) -> str:
    """The transcript's reading, unless the file is too cold to trust it.

    A tail is a snapshot of an unfinished turn, and an unfinished turn looks the
    same whether it is still being written or was abandoned at lunchtime -- the
    process is alive either way, so :func:`orphans` has nothing to say about it
    and the file's own mtime is the only thing left. Past
    `DISCOVER_ACTIVE_SECONDS` the reading is dropped and adoption falls back to
    the resting default, which is only the behaviour this whole path replaced.
    """
    if not transcript.state:
        return ""
    age = now - transcript.modified_at
    if age > config.DISCOVER_ACTIVE_SECONDS:
        log.debug(
            "%s looks %s but has been quiet for %.0fs; adopting it at rest",
            transcript.session_id[:8],
            transcript.state,
            age,
        )
        return ""
    return transcript.state


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


def adopt(
    table: SessionTable, found: list[Discovered], now: float = 0.0
) -> list[Session]:
    """Put discovered sessions on the board.

    They land as ``idle`` unless the transcript caught them mid-turn, in which
    case they land in the busy state :func:`activity` read off it. The rule that
    draws that line is which way a wrong guess fails. `working` and `thinking`
    are quiet: an encoder that says busy about a session that isn't costs you a
    glance. The *attention* states are not -- `permission` strobes red and
    `done` opens an attention debt that gets more insistent the longer you
    ignore it, so inventing either from a file would nag you about a prompt
    answered before the daemon started. So a finished turn is adopted at rest
    even though the transcript plainly says `end_turn`: the encoder waits for a
    live event before it asks you for anything.

    No ``turn_started_at`` is invented either. We know when we started believing
    this session was working and not when its turn began, and
    :func:`mft.render._turn_ring` already falls back to `state_since` for
    exactly this case rather than being handed a number nobody measured.

    The violet pile comes back too, and it is the one thing here that is not a
    guess: a subagent's transcript names it with the same id its hooks carry, so
    a rebuilt dot is the dot a `SubagentStop` will remove. ``now`` is the
    board's clock -- :func:`subagents` hands back ages precisely so the two
    clocks meet here and nowhere else.

    The first real hook event finds the session by id and takes over.
    """
    now = now or time.monotonic()
    adopted: list[Session] = []
    for entry in found:
        # A hook event can beat discovery to the same session, and what it says
        # is live where this is reconstructed -- so a session that already
        # exists keeps its own terminal identity rather than being rekeyed onto
        # a tab this guessed at.
        known = table.get(entry.session_id, entry.provider) is not None
        terminal = None if known else (entry.terminal or None)
        session = table.ensure(
            entry.session_id, entry.cwd, terminal, provider=entry.provider
        )
        if session is None:
            log.info("no free encoder for discovered session %s", entry.session_id[:8])
            break
        session.terminal = session.terminal or dict(entry.terminal)
        session.transcript_path = session.transcript_path or entry.transcript_path
        session.tab_title = session.tab_title or entry.title
        session.permission_mode = session.permission_mode or entry.permission_mode
        # The same guard as the terminal above, and for the same reason: a live
        # record knows better than a file, and `ensure` can hand back one that
        # was already on the board under another id. Only a session sitting at
        # rest is told what its transcript thinks it is doing.
        if entry.state and not known and session.state == "idle":
            session.set_state(entry.state)
        # Same guard again, plus one more: a pile that is already standing was
        # built from live events, and the file can only be a worse copy of it.
        # This fills an empty pile, it never edits one.
        if entry.subagents and not known and not session.subagents_in_flight:
            for key, (idle_for, out_for) in entry.subagents.items():
                session.subagents_in_flight[key] = Subagent(
                    started_at=now - out_for, last_tool_at=now - idle_for
                )
        adopted.append(session)
    return adopted
