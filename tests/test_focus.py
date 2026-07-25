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


class NeverStartsASession(unittest.TestCase):
    """The tail of the chain reaches for a whole application, and `open` is not
    the inert verb it looks like: on Claude Code's own bundle it answers with a
    *new* Claude, which posts a SessionStart, claims the next free encoder and
    flashes it. A press may raise a running app and it may do nothing at all,
    but it may never create the thing the board exists to watch."""

    def setUp(self):
        self.opened: list[list[str]] = []
        self.patch("_run", lambda cmd, timeout=4.0: self.opened.append(cmd) or True)

    def patch(self, name: str, value) -> None:
        original = getattr(focus, name)
        setattr(focus, name, value)
        self.addCleanup(setattr, focus, name, original)

    def owns(self, bundle: str, pid: int = 7) -> None:
        self.patch("_owning_app", lambda _: (bundle, pid))

    def test_claude_is_recognised_by_bundle_id_and_by_bundle_path(self):
        """The two adapters arrive holding different spellings of the app."""
        self.assertTrue(focus._spawns_a_session("com.anthropic.claude-code"))
        self.assertTrue(focus._spawns_a_session("/Users/me/.local/share/claude/ClaudeCode.app"))
        self.assertTrue(focus._spawns_a_session("/Applications/ClaudeCode.app/"))

    def test_ordinary_terminals_are_left_alone(self):
        for app in ("com.apple.Terminal", "/Applications/Ghostty.app", "iTerm", ""):
            self.assertFalse(focus._spawns_a_session(app))

    def test_the_bundle_adapter_declines_claudes_own_app(self):
        ctx = {"__CFBundleIdentifier": "com.anthropic.claude-code"}
        self.assertFalse(focus._bundle_focus(ctx))
        self.assertEqual(self.opened, [])

    def test_the_bundle_adapter_still_opens_a_terminal(self):
        self.assertTrue(focus._bundle_focus({"__CFBundleIdentifier": "com.apple.Terminal"}))
        self.assertEqual(self.opened, [["open", "-b", "com.apple.Terminal"]])

    def test_the_app_name_adapter_declines_claudes_own_app(self):
        """`TERM_PROGRAM` is whatever the environment says, so it reaches
        `open` unvetted unless this adapter checks too."""
        self.assertFalse(focus._app_focus({"TERM_PROGRAM": "ClaudeCode.app"}))
        self.assertEqual(self.opened, [])

    def test_a_running_app_is_fronted_rather_than_opened(self):
        self.owns("/Applications/Ghostty.app")
        self.patch("_raise_pid", lambda pid, label="": pid == 7)
        self.assertTrue(focus._ancestor_focus({"pid": "42"}))
        self.assertEqual(self.opened, [])

    def test_a_terminal_that_cannot_be_fronted_is_still_opened(self):
        """Fronting fails for ordinary reasons -- no windows yet, Automation
        refused -- and for a terminal `open` remains a fair second try."""
        self.owns("/Applications/Ghostty.app")
        self.patch("_raise_pid", lambda pid, label="": False)
        self.assertTrue(focus._ancestor_focus({"pid": "42"}))
        self.assertEqual(self.opened, [["open", "-a", "/Applications/Ghostty.app"]])

    def test_claudes_app_that_cannot_be_fronted_is_left_alone(self):
        """The press does nothing, and that is the point: the session really is
        in there, so nothing further down the chain would do better, and `open`
        would answer by starting a second Claude beside it."""
        self.owns("/Users/me/.local/share/claude/ClaudeCode.app")
        self.patch("_raise_pid", lambda pid, label="": False)
        self.assertFalse(focus._ancestor_focus({"pid": "42"}))
        self.assertEqual(self.opened, [])

    def test_a_session_outside_any_app_is_still_nothing_to_raise(self):
        self.owns("", pid=0)
        self.assertFalse(focus._ancestor_focus({"pid": "42"}))
        self.assertEqual(self.opened, [])


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
