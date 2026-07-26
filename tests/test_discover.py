"""python3 -m unittest discover tests"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import discover  # noqa: E402
from mft.identity import is_hostless, terminal_keys  # noqa: E402
from mft.state import Session, SessionTable  # noqa: E402


def write_transcript(
    root: Path, project: str, session_id: str, cwd: str, *, age: float = 0.0, **fields
) -> Path:
    """One transcript, laid out the way Claude Code lays them out."""
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    entry = {"type": "user", "sessionId": session_id, "cwd": cwd, **fields}
    path.write_text(json.dumps(entry) + "\n")
    if age:
        stamp = time.time() - age
        import os

        os.utime(path, (stamp, stamp))
    return path


class ProcessTable(unittest.TestCase):
    def test_session_id_is_read_out_of_argv(self):
        argv = "/Users/x/.local/share/claude/versions/2.1.209 --session-id abc --resume /y"
        self.assertEqual(discover._session_id_from_argv(argv), "abc")

    def test_argv_without_a_session_id_yields_nothing(self):
        self.assertEqual(discover._session_id_from_argv("claude"), "")
        self.assertEqual(discover._session_id_from_argv("claude --session-id"), "")

    def test_helper_processes_are_not_sessions(self):
        for argv in (
            "claude bg-pty-host --bg-pty-host /tmp/x.sock 200 50",
            "claude bg-spare --bg-spare /tmp/x.claim.sock",
            "/Users/x/.local/bin/claude daemon run --json-path /y",
        ):
            self.assertTrue(
                any(marker in argv for marker in discover.NOT_A_SESSION),
                f"{argv} should be filtered out",
            )

    def test_terminal_identity_matches_the_hook_shape(self):
        proc = discover.Proc(pid=42, tty="/dev/ttys004", cwd="/tmp")
        self.assertEqual(proc.terminal, {"pid": "42", "tty": "/dev/ttys004"})
        self.assertEqual(discover.Proc(pid=42).terminal, {"pid": "42"})


class Transcripts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_reads_cwd_and_mode_off_the_tail(self):
        path = write_transcript(
            self.root, "-tmp-a", "sess-a", "/tmp/a", permissionMode="bypassPermissions"
        )
        found = discover.recent_transcripts(str(self.root))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].session_id, "sess-a")
        self.assertEqual(found[0].cwd, "/tmp/a")
        self.assertEqual(found[0].permission_mode, "bypassPermissions")
        self.assertEqual(found[0].path, str(path))

    def test_stale_transcripts_are_out_of_the_window(self):
        write_transcript(self.root, "-tmp-a", "fresh", "/tmp/a")
        write_transcript(self.root, "-tmp-a", "stale", "/tmp/a", age=99_999)
        found = discover.recent_transcripts(str(self.root))
        self.assertEqual([t.session_id for t in found], ["fresh"])

    def test_most_recent_first(self):
        write_transcript(self.root, "-tmp-a", "older", "/tmp/a", age=300)
        write_transcript(self.root, "-tmp-a", "newer", "/tmp/a", age=10)
        found = discover.recent_transcripts(str(self.root))
        self.assertEqual([t.session_id for t in found], ["newer", "older"])

    def test_a_transcript_with_no_cwd_is_skipped(self):
        directory = self.root / "-tmp-a"
        directory.mkdir()
        (directory / "empty.jsonl").write_text('{"type": "summary"}\n')
        self.assertEqual(discover.recent_transcripts(str(self.root)), [])

    def test_a_missing_projects_dir_is_not_an_error(self):
        self.assertEqual(discover.recent_transcripts(str(self.root / "nope")), [])


class Join(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_transcript_with_no_live_process_is_not_adopted(self):
        write_transcript(self.root, "-tmp-a", "dead", "/tmp/a")
        found = discover.discover(processes=[], projects_dir=str(self.root))
        self.assertEqual(found, [])

    def test_an_unreadable_process_table_adopts_nothing(self):
        write_transcript(self.root, "-tmp-a", "live", "/tmp/a")
        self.assertEqual(
            discover.discover(processes=None, projects_dir=str(self.root)), []
        )

    def test_matches_a_terminal_session_by_working_directory(self):
        write_transcript(self.root, "-tmp-a", "sess-a", "/tmp/a")
        proc = discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a")
        found = discover.discover(processes=[proc], projects_dir=str(self.root))
        self.assertEqual([d.session_id for d in found], ["sess-a"])
        self.assertEqual(found[0].terminal["tty"], "/dev/ttys001")

    def test_argv_beats_the_directory_guess(self):
        write_transcript(self.root, "-tmp-a", "named", "/tmp/a", age=300)
        write_transcript(self.root, "-tmp-a", "newer", "/tmp/a", age=10)
        procs = [
            discover.Proc(pid=7, cwd="/tmp/a", session_id="named"),
            discover.Proc(pid=8, tty="/dev/ttys002", cwd="/tmp/a"),
        ]
        found = discover.discover(processes=procs, projects_dir=str(self.root))
        by_id = {d.session_id: d for d in found}
        self.assertEqual(set(by_id), {"named", "newer"})
        self.assertEqual(by_id["named"].terminal["pid"], "7")
        self.assertEqual(by_id["newer"].terminal["tty"], "/dev/ttys002")

    def test_one_process_adopts_only_the_newest_of_its_transcripts(self):
        """Resuming and clearing leave older transcripts behind in the same
        directory; only one of them belongs to the process that is running."""
        write_transcript(self.root, "-tmp-a", "old", "/tmp/a", age=600)
        write_transcript(self.root, "-tmp-a", "current", "/tmp/a", age=5)
        proc = discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a")
        found = discover.discover(processes=[proc], projects_dir=str(self.root))
        self.assertEqual([d.session_id for d in found], ["current"])

    def test_two_tabs_in_one_directory_keep_slots_but_lose_their_tty(self):
        write_transcript(self.root, "-tmp-a", "one", "/tmp/a", age=5)
        write_transcript(self.root, "-tmp-a", "two", "/tmp/a", age=6)
        procs = [
            discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a"),
            discover.Proc(pid=8, tty="/dev/ttys002", cwd="/tmp/a"),
        ]
        found = discover.discover(processes=procs, projects_dir=str(self.root))
        self.assertEqual({d.session_id for d in found}, {"one", "two"})
        # Guessing which tab owns which would hand the wrong encoder to the
        # next /clear in either one.
        self.assertEqual([d.terminal for d in found], [{}, {}])


class Adoption(unittest.TestCase):
    def test_adopted_sessions_are_idle_and_carry_their_transcript(self):
        table = SessionTable()
        entry = discover.Discovered(
            session_id="sess-a",
            cwd="/tmp/a",
            transcript_path="/tmp/a.jsonl",
            terminal={"tty": "/dev/ttys001", "pid": "7"},
            permission_mode="bypassPermissions",
        )
        adopted = discover.adopt(table, [entry])
        self.assertEqual(len(adopted), 1)
        session = adopted[0]
        self.assertEqual(session.state, "idle")
        self.assertIsNone(session.attention_since)
        self.assertFalse(session.alert)
        self.assertEqual(session.transcript_path, "/tmp/a.jsonl")
        self.assertTrue(session.unsupervised)
        # Keyed on the tab, so the next /clear in it lands on the same encoder.
        self.assertEqual(session.key, "tty:/dev/ttys001")

    def test_an_unpinned_session_still_gets_an_encoder(self):
        table = SessionTable()
        adopted = discover.adopt(
            table,
            [discover.Discovered("sess-a", "/tmp/a", "/tmp/a.jsonl")],
        )
        self.assertEqual(adopted[0].key, "sid:sess-a")

    def test_a_hook_event_that_arrived_first_wins(self):
        table = SessionTable()
        live = table.ensure("sess-a", "/tmp/a", {"tty": "/dev/ttys009"})
        live.transcript_path = "/live.jsonl"
        adopted = discover.adopt(
            table,
            [
                discover.Discovered(
                    "sess-a", "/tmp/a", "/stale.jsonl", {"tty": "/dev/ttys001"}
                )
            ],
        )
        self.assertIs(adopted[0], live)
        self.assertEqual(live.transcript_path, "/live.jsonl")
        self.assertEqual(live.key, "tty:/dev/ttys009")

    def test_adoption_stops_at_the_last_encoder(self):
        table = SessionTable(slot_count=2)
        found = [
            discover.Discovered(f"sess-{n}", f"/tmp/{n}", f"/tmp/{n}.jsonl")
            for n in range(4)
        ]
        self.assertEqual(len(discover.adopt(table, found)), 2)


class Orphans(unittest.TestCase):
    """A session on the board whose claude process has gone. See
    `discover.orphans` -- the check is the recorded pid and only that."""

    def setUp(self):
        self.table = SessionTable()
        self.living = {7}

    def alive(self, pid: int) -> bool:
        return pid in self.living

    def add(self, session_id: str, **terminal) -> Session:
        session = self.table.ensure(session_id, f"/tmp/{session_id}", terminal)
        assert session is not None
        # What `daemon.handle_event` does alongside `ensure`: the table keys the
        # slot, the daemon keeps the description.
        session.terminal = dict(terminal)
        return session

    def sweep(self) -> list[str]:
        gone = discover.orphans(self.table.all(), alive=self.alive)
        return [s.session_id for s in gone]

    def test_a_dead_pid_is_an_orphan(self):
        self.add("live", pid="7", tty="/dev/ttys001")
        self.add("closed", pid="8", tty="/dev/ttys002")
        self.assertEqual(self.sweep(), ["closed"])

    def test_the_encoder_comes_back(self):
        live = self.add("live", pid="7", tty="/dev/ttys001")
        closed = self.add("closed", pid="8", tty="/dev/ttys002")
        self.assertEqual(closed.slot, 1)
        self.assertEqual(
            self.table.release_all(discover.orphans(self.table.all(), alive=self.alive)),
            [closed],
        )
        self.assertEqual([s.session_id for s in self.table.all()], ["live"])
        self.assertEqual(live.slot, 0)
        self.assertEqual(self.table.ensure("next", "/tmp/next").slot, 1)

    def test_a_session_that_never_recorded_a_pid_survives_the_pid_sweep(self):
        """`notify.sh` reports a tab without spawning a process to read one, so
        a session the SessionStart hook missed has no pid to check. This sweep
        has nothing to ask about and says nothing; the census is what settles
        these, off a tty rather than a pid. See `Ttys`."""
        self.add("anonymous", tty="/dev/ttys003")
        self.add("nameless")
        self.assertEqual(self.sweep(), [])

    def test_an_ended_session_keeps_its_fade(self):
        """Its process is meant to be gone, and it is already on the linger
        clock -- reaping it here would cut the fade off."""
        ended = self.add("ended", pid="9", tty="/dev/ttys004")
        ended.ended_at = time.monotonic()
        self.assertEqual(self.sweep(), [])

    def test_a_pid_that_is_not_a_number_is_not_a_death(self):
        self.add("odd", pid="", tty="/dev/ttys005")
        self.add("odder", pid="not-a-pid", tty="/dev/ttys006")
        self.assertEqual(self.sweep(), [])

    def test_a_record_with_only_a_key_left_still_counts(self):
        """Merges and discovery leave records carrying a pid token with no
        terminal description behind it."""
        session = self.add("keyed")
        session.keys.add("pid:8")
        self.assertEqual(self.sweep(), ["keyed"])

    def test_a_pid_this_tab_used_to_be_is_not_a_death(self):
        """`keys` accumulates: a restart in the same tab leaves the old pid
        token next to the live one, and one alive is enough."""
        session = self.add("restarted")
        session.keys.update({"pid:8", "pid:7"})
        self.assertEqual(self.sweep(), [])

    def test_the_host_of_a_handed_off_session_counts_as_alive(self):
        """A session handed off into a process under Claude Code's background
        daemon is described by its tab's terminal and running somewhere else.
        The tab's own claude exiting is not that session ending -- consulting
        only the terminal reaped a working encoder."""
        session = self.add("moved", pid="8", tty="/dev/ttys001")
        session.keys.add("host:7")
        self.assertEqual(self.sweep(), [])

    def test_a_host_that_is_gone_too_is_still_a_death(self):
        session = self.add("moved", pid="8", tty="/dev/ttys001")
        session.keys.add("host:9")
        self.assertEqual(self.sweep(), ["moved"])

    def test_the_terminal_settles_a_stale_key(self):
        session = self.add("restarted", pid="7", tty="/dev/ttys001")
        session.keys.add("pid:8")  # what it was before the restart
        self.assertEqual(self.sweep(), [])

    def test_our_own_process_is_alive(self):
        import os

        self.assertTrue(discover.pid_alive(os.getpid()))

    def test_releasing_twice_drops_once(self):
        closed = self.add("closed", pid="8", tty="/dev/ttys002")
        self.assertEqual(self.table.release_all([closed]), [closed])
        self.assertEqual(self.table.release_all([closed]), [])


def rows(*procs: tuple[int, str, str], filler: int = 64) -> list[tuple[int, str, str]]:
    """A process table with these processes in it, padded out to a size the
    census will treat as a real read. See `config.CENSUS_MIN_ROWS`."""
    return list(procs) + [(9000 + n, "", "/usr/sbin/whatever") for n in range(filler)]


class Censuses(unittest.TestCase):
    def test_a_short_table_is_a_failed_read_not_an_empty_desk(self):
        self.assertFalse(discover.census([(1, "/dev/ttys001", "claude")]).usable)

    def test_a_table_with_no_ttys_at_all_is_a_failed_read(self):
        """Every negative the census draws comes off the tty column, so a
        column that came back empty is the one thing it cannot conclude from."""
        self.assertFalse(discover.census(rows((1, "", "/bin/launchd"))).usable)

    def test_a_real_table_is_usable(self):
        taken = discover.census(rows((1, "/dev/ttys001", "claude")))
        self.assertTrue(taken.usable)
        self.assertIn("/dev/ttys001", taken.ttys)
        self.assertEqual([p.pid for p in taken.procs], [1])

    def test_an_unreadable_table_is_no_census_at_all(self):
        """Distinct from an unusable one: nothing to be self-checked."""
        with mock.patch.object(discover, "process_rows", return_value=None):
            self.assertIsNone(discover.census())


class Ttys(unittest.TestCase):
    """The half of `discover.orphans` that a pid cannot reach: a tab closes,
    its pty is freed, and no process on the machine is on that tty again."""

    def setUp(self):
        self.table = SessionTable()
        self.living = {7}

    def alive(self, pid: int) -> bool:
        return pid in self.living

    def add(self, session_id: str, **terminal) -> Session:
        session = self.table.ensure(session_id, f"/tmp/{session_id}", terminal)
        assert session is not None
        session.terminal = dict(terminal)
        return session

    def sweep(self, *in_use: str) -> list[str]:
        taken = discover.census(rows(*[(1000 + n, tty, "sh") for n, tty in enumerate(in_use)]))
        gone = discover.orphans(self.table.all(), alive=self.alive, taken=taken)
        return [s.session_id for s in gone]

    def test_a_freed_tty_settles_a_session_with_no_pid(self):
        """The orphan the pid sweep can only leave to the hour."""
        self.add("closed", tty="/dev/ttys003")
        self.add("open", tty="/dev/ttys004")
        self.assertEqual(self.sweep("/dev/ttys004"), ["closed"])

    def test_a_held_tty_is_a_tab_that_is_still_open(self):
        self.add("open", tty="/dev/ttys003")
        self.assertEqual(self.sweep("/dev/ttys003"), [])

    def test_a_tty_key_counts_as_much_as_a_description(self):
        session = self.add("keyed")
        session.keys.add("tty:/dev/ttys003")
        self.assertEqual(self.sweep("/dev/ttys004"), ["keyed"])

    def test_a_freed_tty_beats_a_pid_that_reads_alive(self):
        """Pid reuse, which the pid sweep gets wrong in the direction that
        costs an encoder: the number came back around. A live claude is on its
        tty by definition, so a tty belonging to nobody outranks it."""
        self.add("recycled", pid="7", tty="/dev/ttys003")
        self.assertEqual(self.sweep("/dev/ttys004"), ["recycled"])

    def test_a_handed_off_session_keeps_its_encoder(self):
        """Its tab closing is not it ending -- it is running in the host, and
        the host is alive. The one case a freed tty is expected."""
        session = self.add("moved", pid="8", tty="/dev/ttys003")
        session.keys.add("host:7")
        self.assertEqual(self.sweep("/dev/ttys004"), [])

    def test_an_unusable_census_concludes_nothing(self):
        self.add("closed", tty="/dev/ttys003")
        gone = discover.orphans(
            self.table.all(), alive=self.alive, taken=discover.census([])
        )
        self.assertEqual(gone, [])

    def test_a_session_with_no_tty_at_all_still_keeps_the_hour(self):
        """Nothing here reads a Claude process's argv, and a record with
        neither pid nor tty is exactly what would need that. See
        `discover.phantoms` for the one thing that does settle those, which is
        arithmetic about a directory rather than a question about a process."""
        self.add("nameless")
        self.assertEqual(self.sweep("/dev/ttys004"), [])

    def test_an_ended_session_keeps_its_fade(self):
        ended = self.add("ended", tty="/dev/ttys003")
        ended.ended_at = time.monotonic()
        self.assertEqual(self.sweep("/dev/ttys004"), [])

    def test_the_log_says_which_fact_settled_it(self):
        closed = self.add("closed", tty="/dev/ttys003")
        dead = self.add("dead", pid="8", tty="/dev/ttys007")
        taken = discover.census(rows((1000, "/dev/ttys004", "sh")))
        self.assertEqual(
            discover.epitaph(closed, taken, self.alive), "the tab on /dev/ttys003 is closed"
        )
        self.assertEqual(discover.epitaph(dead, taken, self.alive), "pid 8 is gone")


class Phantoms(unittest.TestCase):
    """A record naming nobody, in a directory with no room left for it.

    The knob this was written for: adoption matched a session that had exited
    twenty minutes earlier -- its transcript was the newer one -- and gave it the
    process belonging to a session that had been idle at a prompt since lunch.
    The ambiguity rule then stripped the terminal off it, leaving a record with
    no pid and no tty, which is the one shape `orphans` cannot ask about.
    """

    def setUp(self):
        self.table = SessionTable()

    def add(self, session_id: str, cwd: str, **terminal) -> Session:
        session = self.table.ensure(session_id, cwd, terminal or None)
        assert session is not None
        session.terminal = dict(terminal)
        return session

    def census(self, *procs: discover.Proc) -> discover.Census:
        return discover.Census(
            procs=list(procs), ttys=frozenset({"/dev/ttys001"}), size=400
        )

    def sweep(self, taken: discover.Census) -> list[str]:
        return [s.session_id for s in discover.phantoms(self.table.all(), taken)]

    def test_a_record_naming_nobody_goes_when_every_claude_is_spoken_for(self):
        self.add("real", "/tmp/a", pid="7", tty="/dev/ttys001")
        self.add("phantom", "/tmp/a")
        taken = self.census(discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a"))
        self.assertEqual(self.sweep(taken), ["phantom"])

    def test_it_stays_while_there_is_a_process_it_could_be(self):
        """The safe half, and the common one: two live sessions in a directory
        where only one of them has said which tab it is. The other is exactly
        what the nameless record might be."""
        self.add("real", "/tmp/a", pid="7", tty="/dev/ttys001")
        self.add("maybe", "/tmp/a")
        taken = self.census(
            discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a"),
            discover.Proc(pid=8, tty="/dev/ttys002", cwd="/tmp/a"),
        )
        self.assertEqual(self.sweep(taken), [])

    def test_a_directory_with_no_recognised_claude_concludes_nothing(self):
        """The day `claude_processes` stops recognising a session's argv, this
        has to read as no evidence rather than as an empty desk."""
        self.add("real", "/tmp/a", pid="7", tty="/dev/ttys001")
        self.add("nameless", "/tmp/a")
        self.assertEqual(self.sweep(self.census()), [])

    def test_a_tty_claims_a_process_as_well_as_a_pid_does(self):
        """`notify.sh` names a tab without naming a process, so the record
        accounting for a Claude often holds no pid at all."""
        self.add("real", "/tmp/a", tty="/dev/ttys001")
        self.add("phantom", "/tmp/a")
        taken = self.census(discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a"))
        self.assertEqual(self.sweep(taken), ["phantom"])

    def test_two_nameless_records_cannot_account_for_each_other(self):
        self.add("one", "/tmp/a")
        self.add("two", "/tmp/a")
        taken = self.census(discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a"))
        self.assertEqual(self.sweep(taken), [])

    def test_a_record_that_describes_a_tab_is_never_touched_here(self):
        """Even one whose process is gone: that is the sweep next door, which
        can check it. This one only ever releases a record naming nothing."""
        self.add("elsewhere", "/tmp/a", pid="999", tty="/dev/ttys009")
        taken = self.census(discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a"))
        self.assertEqual(self.sweep(taken), [])

    def test_another_directory_is_another_question(self):
        self.add("real", "/tmp/a", pid="7", tty="/dev/ttys001")
        self.add("nameless", "/tmp/b")
        taken = self.census(discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a"))
        self.assertEqual(self.sweep(taken), [])

    def test_an_ended_session_keeps_its_fade(self):
        self.add("real", "/tmp/a", pid="7", tty="/dev/ttys001")
        ended = self.add("phantom", "/tmp/a")
        ended.ended_at = time.monotonic()
        taken = self.census(discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a"))
        self.assertEqual(self.sweep(taken), [])

    def test_an_unusable_census_concludes_nothing(self):
        self.add("real", "/tmp/a", pid="7", tty="/dev/ttys001")
        self.add("phantom", "/tmp/a")
        thin = discover.Census(
            procs=[discover.Proc(pid=7, tty="/dev/ttys001", cwd="/tmp/a")],
            ttys=frozenset(),
            size=3,
        )
        self.assertEqual(self.sweep(thin), [])
        self.assertEqual(discover.phantoms(self.table.all(), None), [])


class LearnPids(unittest.TestCase):
    """Giving the pid sweep something to ask about. See `discover.learn_pids`."""

    def setUp(self):
        self.table = SessionTable()

    def add(self, session_id: str, cwd: str = "/tmp/p", **terminal) -> Session:
        session = self.table.ensure(session_id, cwd, terminal)
        assert session is not None
        session.terminal = dict(terminal)
        return session

    def test_a_shared_token_names_the_process(self):
        session = self.add("anonymous", tty="/dev/ttys003")
        proc = discover.Proc(pid=41, tty="/dev/ttys003", cwd="/tmp/p")
        self.assertEqual(discover.learn_pids([session], [proc]), [(session, proc)])
        self.assertEqual(session.terminal["pid"], "41")

    def test_it_writes_the_whole_terminal_not_only_the_number(self):
        """A pid alone *is* a description -- of a process with no tab behind it.

        Written onto a record with nothing else on it, it filed a terminal under
        `host:`, where the tab's own record can never match it and the orphan
        sweep will not touch it.
        """
        session = self.add("adopted")
        proc = discover.Proc(
            pid=41,
            tty="/dev/ttys003",
            cwd="/tmp/p",
            env={"TERM_SESSION_ID": "ABC", "TERM_PROGRAM": "Apple_Terminal"},
        )
        discover.learn_pids([session], [proc])
        self.assertFalse(is_hostless(session.terminal))
        self.assertEqual(session.terminal["tty"], "/dev/ttys003")
        self.assertEqual(session.terminal["TERM_SESSION_ID"], "ABC")
        self.assertIn("pid:41", terminal_keys(session.terminal))

    def test_a_process_with_no_tty_still_hands_over_its_pid(self):
        """The desktop-app case: nothing to describe, and the pid is the point."""
        session = self.add("app-session")
        proc = discover.Proc(pid=42, cwd="/elsewhere", session_id="app-session")
        discover.learn_pids([session], [proc])
        self.assertTrue(is_hostless(session.terminal))
        self.assertEqual(session.terminal["pid"], "42")

    def test_argv_names_it_outright(self):
        session = self.add("app-session")
        proc = discover.Proc(pid=42, cwd="/elsewhere", session_id="app-session")
        discover.learn_pids([session], [proc])
        self.assertEqual(session.terminal["pid"], "42")

    def test_the_only_claude_in_the_directory(self):
        session = self.add("nameless")
        proc = discover.Proc(pid=43, tty="/dev/ttys009", cwd="/tmp/p")
        discover.learn_pids([session], [proc])
        self.assertEqual(session.terminal["pid"], "43")

    def test_two_claudes_in_one_directory_are_left_alone(self):
        """A wrong pid is worse than none: it would answer the orphan sweep's
        question with somebody else's life."""
        session = self.add("nameless")
        procs = [
            discover.Proc(pid=44, tty="/dev/ttys009", cwd="/tmp/p"),
            discover.Proc(pid=45, tty="/dev/ttys010", cwd="/tmp/p"),
        ]
        self.assertEqual(discover.learn_pids([session], procs), [])
        self.assertNotIn("pid", session.terminal)

    def test_a_session_that_has_a_pid_is_not_touched(self):
        session = self.add("known", pid="8", tty="/dev/ttys003")
        proc = discover.Proc(pid=41, tty="/dev/ttys003", cwd="/tmp/p")
        self.assertEqual(discover.learn_pids([session], [proc]), [])
        self.assertEqual(session.terminal["pid"], "8")

    def test_one_process_is_claimed_once(self):
        first = self.add("first", cwd="/tmp/p")
        second = self.add("second", cwd="/tmp/p")
        proc = discover.Proc(pid=46, tty="/dev/ttys009", cwd="/tmp/p")
        # Ambiguous the moment there are two of them, in either order.
        self.assertEqual(discover.learn_pids([first, second], [proc]), [])

    def test_an_ended_session_is_not_given_a_pid(self):
        session = self.add("ended", tty="/dev/ttys003")
        session.ended_at = time.monotonic()
        proc = discover.Proc(pid=47, tty="/dev/ttys003", cwd="/tmp/p")
        self.assertEqual(discover.learn_pids([session], [proc]), [])


class RelabelHosts(unittest.TestCase):
    """A record that called its own tab a host process. See
    `discover.relabel_hosts`."""

    def setUp(self):
        self.table = SessionTable()

    def add(self, session_id: str, cwd: str = "/tmp/p", **terminal) -> Session:
        session = self.table.ensure(session_id, cwd, terminal)
        assert session is not None
        session.terminal = dict(terminal)
        return session

    def test_a_host_pid_sitting_on_a_tty_is_a_tab(self):
        session = self.add("mislabelled", pid="62882")
        self.assertEqual(session.keys, {"host:62882"})
        proc = discover.Proc(
            pid=62882, tty="/dev/ttys000", cwd="/tmp/p", env={"TERM_SESSION_ID": "ABC"}
        )
        self.assertEqual(discover.relabel_hosts([session], [proc]), [(session, proc)])
        self.assertFalse(is_hostless(session.terminal))
        self.assertEqual(session.terminal["tty"], "/dev/ttys000")
        self.table.reconcile()
        self.assertIn("pid:62882", session.keys)

    def test_a_real_handoff_is_left_alone(self):
        """The process a conversation was handed to holds no tty, and a census
        filters it out of the table entirely (`NOT_A_SESSION`)."""
        session = self.add("handed-off", pid="900")
        self.assertEqual(discover.relabel_hosts([session], []), [])
        self.assertEqual(session.terminal, {"pid": "900"})
        other = discover.Proc(pid=901, tty="/dev/ttys002", cwd="/tmp/p")
        self.assertEqual(discover.relabel_hosts([session], [other]), [])
        self.assertTrue(is_hostless(session.terminal))

    def test_a_record_that_names_its_tab_is_not_touched(self):
        """A session handed off *out of* a tab holds both tokens, and the tab
        description it already has is the better one."""
        session = self.add("both", tty="/dev/ttys004", pid="500")
        session.keys.add("host:900")
        proc = discover.Proc(pid=900, tty="/dev/ttys009", cwd="/tmp/p")
        self.assertEqual(discover.relabel_hosts([session], [proc]), [])
        self.assertEqual(session.terminal["tty"], "/dev/ttys004")

    def test_the_phantom_encoder_this_exists_for(self):
        """The whole failure, end to end.

        A session adopted at boot with no terminal learns its pid off the
        process table; the tab it is in registers separately with a tty. Filed
        under `host:`, the two never met and the sweep could not touch either --
        one terminal, two encoders, for as long as the tab stayed open.
        """
        adopted = self.table.ensure("adopted", "/tmp/p", {})
        adopted.terminal = {}
        proc = discover.Proc(pid=62882, tty="/dev/ttys000", cwd="/tmp/p")
        discover.learn_pids(self.table.all(), [proc])
        self.table.reconcile()
        # Then the tab it is sitting in runs the SessionStart hook.
        tab = self.table.ensure("tab", "/tmp/p", {"tty": "/dev/ttys000", "pid": "62882"})
        self.table.reconcile()
        self.assertEqual(len(self.table.all()), 1)
        self.assertEqual(tab.slot, adopted.slot)

    def test_an_ended_session_is_not_repaired(self):
        session = self.add("gone", pid="62882")
        session.ended_at = time.monotonic()
        proc = discover.Proc(pid=62882, tty="/dev/ttys000", cwd="/tmp/p")
        self.assertEqual(discover.relabel_hosts([session], [proc]), [])


if __name__ == "__main__":
    unittest.main()
