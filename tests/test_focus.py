"""python3 -m unittest discover tests

Press-to-focus, which is the one place the visualizer stops being a display
and has to actually hit something. The subject here is the *chain*: what it
tries, in what order, and -- mostly -- that a failure anywhere in it keeps
going instead of quietly being the answer.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import discover, focus  # noqa: E402

TERMINAL = {
    "pid": "42",
    "tty": "/dev/ttys004",
    "TERM_PROGRAM": "Apple_Terminal",
    "TERM_SESSION_ID": "AAAA-BBBB",
    "__CFBundleIdentifier": "com.apple.Terminal",
}


class Chain(unittest.TestCase):
    """Every gui adapter is a *candidate*, not a verdict."""

    def setUp(self):
        self.tried: list[str] = []

    def adapter(self, name: str, detect: bool, result: bool) -> focus.Adapter:
        def attempt(ctx):
            self.tried.append(name)
            return result

        return focus.Adapter(name, "gui", lambda ctx: detect, attempt)

    def run_with(self, adapters, ctx=None):
        original = focus.ADAPTERS
        focus.ADAPTERS = adapters
        try:
            return focus.focus(dict(ctx if ctx is not None else TERMINAL))
        finally:
            focus.ADAPTERS = original

    def test_a_failing_adapter_falls_through_to_the_next(self):
        ok = self.run_with(
            [
                self.adapter("first", detect=True, result=False),
                self.adapter("second", detect=True, result=True),
            ]
        )
        self.assertTrue(ok)
        self.assertEqual(self.tried, ["first", "second"])

    def test_success_stops_the_chain(self):
        self.run_with(
            [
                self.adapter("first", detect=True, result=True),
                self.adapter("second", detect=True, result=True),
            ]
        )
        self.assertEqual(self.tried, ["first"])

    def test_adapters_that_do_not_recognise_the_session_are_skipped(self):
        self.run_with(
            [
                self.adapter("wrong", detect=False, result=True),
                self.adapter("right", detect=True, result=True),
            ]
        )
        self.assertEqual(self.tried, ["right"])

    def test_an_adapter_that_raises_does_not_end_the_attempt(self):
        def explode(ctx):
            self.tried.append("boom")
            raise RuntimeError("osascript went missing")

        ok = self.run_with(
            [
                focus.Adapter("boom", "gui", lambda c: True, explode),
                self.adapter("after", detect=True, result=True),
            ]
        )
        self.assertTrue(ok)
        self.assertEqual(self.tried, ["boom", "after"])

    def test_nothing_to_go_on_is_not_a_failed_attempt(self):
        self.assertFalse(focus.focus({}))


class Detection(unittest.TestCase):
    def _detected_by(self, ctx):
        return [a.name for a in focus.ADAPTERS if a.detect(ctx)]

    def test_a_real_terminal_session_is_reachable_by_tab(self):
        self.assertTrue(focus.precise(TERMINAL))
        self.assertTrue(self._detected_by(TERMINAL))

    def test_tab_level_env_beats_a_missing_tty(self):
        self.assertTrue(focus.precise({"ITERM_SESSION_ID": "w0t0p0:GUID"}))
        self.assertTrue(focus.precise({"TMUX_PANE": "%3"}))

    def test_a_bare_tty_is_not_yet_a_tab(self):
        """It could be Terminal's, and is worth asking about -- but until
        something says so, a press on it is a guess, and the daemon should go
        and look the session up rather than take it."""
        self.assertFalse(focus.precise({"tty": "/dev/ttys004"}))
        self.assertTrue(focus._maybe_terminal_app({"tty": "/dev/ttys004"}))

    def test_a_tty_belonging_to_another_terminal_is_not_offered_to_terminal(self):
        self.assertFalse(
            focus._maybe_terminal_app({"tty": "/dev/ttys004", "TERM_PROGRAM": "ghostty"})
        )

    def test_a_pid_alone_is_still_worth_a_press(self):
        """The desktop app has no tty and no terminal variables. It is not
        precise, but the ancestor adapter can still raise it."""
        self.assertFalse(focus.precise({"pid": "42"}))
        self.assertEqual(self._detected_by({"pid": "42"}), ["ancestor"])

    def test_nothing_is_nothing(self):
        self.assertEqual(self._detected_by({}), [])
        self.assertFalse(focus.precise({}))
        self.assertFalse(focus.precise(None))

    def test_ttys_are_normalised_to_what_applescript_says(self):
        self.assertEqual(focus._norm_tty("ttys004"), "/dev/ttys004")
        self.assertEqual(focus._norm_tty("/dev/ttys004"), "/dev/ttys004")
        for absent in ("", "??", "-", None):
            self.assertEqual(focus._norm_tty(absent), "")


class Recovery(unittest.TestCase):
    """Reading a session's terminal back out of the process table, which is the
    only thing a session that missed the hook has left."""

    PROCS = [
        discover.Proc(
            pid=1,
            tty="/dev/ttys001",
            cwd="/work/a",
            env={"TERM_PROGRAM": "Apple_Terminal", "TERM_SESSION_ID": "A"},
        ),
        discover.Proc(
            pid=2,
            tty="/dev/ttys002",
            cwd="/work/b",
            env={"TERM_PROGRAM": "Apple_Terminal", "TERM_SESSION_ID": "B"},
        ),
        discover.Proc(
            pid=3,
            tty="/dev/ttys003",
            cwd="/work/b",
            env={"TERM_PROGRAM": "Apple_Terminal", "TERM_SESSION_ID": "C"},
        ),
    ]

    def resolve(self, cwd, **kwargs):
        return discover.resolve_terminal(cwd, processes=self.PROCS, **kwargs)

    def test_the_only_claude_in_a_directory_is_the_one(self):
        self.assertEqual(self.resolve("/work/a")["tty"], "/dev/ttys001")

    def test_a_known_pid_is_re_read_rather_than_guessed_at(self):
        """This is also how a session discovered before we read environments
        gets upgraded from a bare tty to a full identity."""
        found = self.resolve("/work/b", pid="3")
        self.assertEqual(found["tty"], "/dev/ttys003")
        self.assertEqual(found["TERM_SESSION_ID"], "C")

    def test_a_dead_pid_falls_back_to_the_directory(self):
        self.assertEqual(self.resolve("/work/a", pid="999")["tty"], "/dev/ttys001")

    def test_two_claudes_in_one_directory_yield_only_their_app(self):
        found = self.resolve("/work/b")
        self.assertEqual(found, {"TERM_PROGRAM": "Apple_Terminal"})
        self.assertFalse(focus.precise(found))

    def test_claiming_one_of_them_disambiguates_the_other(self):
        self.assertEqual(self.resolve("/work/b", claimed=frozenset({"2"}))["tty"],
                         "/dev/ttys003")

    def test_an_unknown_directory_invents_nothing(self):
        self.assertEqual(self.resolve("/work/c"), {})
        self.assertEqual(self.resolve(""), {})

    def test_an_unreadable_process_table_invents_nothing(self):
        self.assertEqual(discover.resolve_terminal("/work/a", processes=[]), {})

    def test_a_session_without_a_tty_keeps_none_of_its_inherited_env(self):
        """The desktop app inherits the launching terminal's variables. Two of
        them inherit the *same* ones, so trusting them would have both sessions
        keyed to one tab that neither is in."""
        proc = discover.Proc(pid=9, cwd="/work/a", env={"TERM_SESSION_ID": "stale"})
        self.assertEqual(proc.terminal, {"pid": "9"})


if __name__ == "__main__":
    unittest.main()
