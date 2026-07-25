"""python3 -m unittest discover tests"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import discover  # noqa: E402
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

    def test_a_session_that_never_recorded_a_pid_is_left_to_the_TTL(self):
        """`notify.sh` reports a tab without spawning a process to read one, so
        a session the SessionStart hook missed has no pid to check. Absence of
        a match is not evidence of death; the hour is."""
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


if __name__ == "__main__":
    unittest.main()
