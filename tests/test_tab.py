"""python3 -m unittest discover tests

The tab strip: which glyph a state gets, what goes inside the escape sequence,
and -- the part that actually matters -- how rarely anything is written at all.
The tty write itself is not tested; composing the line is, and the daemon's
repaint decision is exercised through a fake `mft.tab.write`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import config, context, tab  # noqa: E402
from mft.state import Session  # noqa: E402


def session(**kwargs) -> Session:
    fields = {"session_id": "abcdef123456", "slot": 0, "cwd": "/Users/x/projects/repo"}
    fields.update(kwargs)
    return Session(**fields)


class Glyphs(unittest.TestCase):
    def test_every_state_has_one(self):
        """A state with no glyph paints a bare title, which reads as a bug.

        STATE_PRIORITY is the one list of states there is, deliberately -- see
        its comment in config."""
        for state in config.STATE_PRIORITY:
            self.assertIn(state, config.TAB_GLYPHS, state)

    def test_the_busy_states_share_a_glyph(self):
        """The whole repaint budget rests on this: a turn is one write, not
        one per tool call."""
        busy = {config.TAB_GLYPHS[s] for s in ("thinking", "working", "streaming")}
        self.assertEqual(len(busy), 1)

    def test_done_and_idle_differ(self):
        """They are one colour on the board because the fade between them is
        the transition. A tab strip has no fade."""
        self.assertNotEqual(config.TAB_GLYPHS["done"], config.TAB_GLYPHS["idle"])

    def test_ended_paints_nothing(self):
        self.assertEqual(tab.glyph_for(session(state="ended")), "")

    def test_unsupervised_wins_over_every_state(self):
        for state in ("idle", "working", "permission", "done"):
            s = session(state=state, permission_mode="bypassPermissions")
            self.assertEqual(tab.glyph_for(s), config.TAB_UNSUPERVISED_GLYPH, state)

    def test_unsupervised_still_ends(self):
        s = session(state="ended", permission_mode="bypassPermissions")
        self.assertEqual(tab.glyph_for(s), "")


class Compose(unittest.TestCase):
    def test_glyph_goes_in_front(self):
        self.assertEqual(tab.compose("X", "a title"), "X a title")

    def test_no_glyph_is_the_bare_title(self):
        self.assertEqual(tab.compose("", "a title"), "a title")

    def test_no_title_is_the_bare_glyph(self):
        self.assertEqual(tab.compose("X", ""), "X")

    def test_control_characters_are_deleted(self):
        """A BEL in a model-written title would terminate the OSC early and
        spill the rest of itself onto the screen."""
        line = tab.compose("X", "before\x07\x1b]0;evil\x07after\n")
        self.assertEqual(line, "X before]0;evilafter")
        self.assertNotIn("\x07", tab.sequence(line)[:-1])

    def test_truncation_never_eats_the_glyph(self):
        line = tab.compose("X", "y" * 200, limit=10)
        self.assertTrue(line.startswith("X "))
        self.assertEqual(len(line), 12)
        self.assertTrue(line.endswith("\N{HORIZONTAL ELLIPSIS}"))

    def test_sequence_is_osc_zero(self):
        self.assertEqual(tab.sequence("hi"), "\x1b]0;hi\x07")


class Tty(unittest.TestCase):
    def test_bare_names_are_qualified(self):
        self.assertEqual(tab.tty_of(session(terminal={"tty": "ttys004"})), "/dev/ttys004")

    def test_qualified_names_are_left_alone(self):
        s = session(terminal={"tty": "/dev/ttys004"})
        self.assertEqual(tab.tty_of(s), "/dev/ttys004")

    def test_ps_placeholders_are_not_ttys(self):
        for raw in ("", "??", "-", "  "):
            self.assertEqual(tab.tty_of(session(terminal={"tty": raw})), "", repr(raw))

    def test_a_session_that_never_said_has_none(self):
        self.assertEqual(tab.tty_of(session()), "")


class ReadTitle(unittest.TestCase):
    """`ai-title` is Claude Code's own generated title, and the last one in the
    transcript is the current one."""

    def transcript(self, *lines: str) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        handle.write("".join(line + "\n" for line in lines))
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_last_one_wins(self):
        path = self.transcript(
            '{"type":"ai-title","aiTitle":"first"}',
            '{"type":"user"}',
            '{"type":"ai-title","aiTitle":"second"}',
        )
        self.assertEqual(context.read_title(path), "second")

    def test_no_title_yet(self):
        path = self.transcript('{"type":"user"}')
        self.assertEqual(context.read_title(path), "")

    def test_a_missing_transcript_is_not_an_error(self):
        self.assertEqual(context.read_title("/nonexistent/transcript.jsonl"), "")
        self.assertEqual(context.read_title(""), "")

    def test_truncated_lines_are_skipped(self):
        path = self.transcript(
            '{"type":"ai-title","aiTitle":"good"}',
            '{"type":"ai-title","aiTitle":"trunc',
        )
        self.assertEqual(context.read_title(path), "good")

    def test_the_word_alone_is_not_a_title(self):
        """The string test is a filter, not the parse."""
        path = self.transcript('{"type":"user","message":"what is an ai-title?"}')
        self.assertEqual(context.read_title(path), "")


class Repaints(unittest.TestCase):
    """The point of the whole exercise: writes are rare."""

    def setUp(self):
        from mft import daemon as daemon_mod, twister

        # Shipped off; these pin what it does when someone turns it on.
        was = config.TAB_TITLE
        config.TAB_TITLE = True
        self.addCleanup(lambda: setattr(config, "TAB_TITLE", was))

        self.writes: list[tuple[str, str]] = []
        real = tab.write
        tab.write = lambda tty, title: (self.writes.append((tty, title)), True)[1]
        self.addCleanup(lambda: setattr(tab, "write", real))
        self.vis = daemon_mod.Visualizer(twister.NullTwister())

    def add(self, **kwargs) -> Session:
        s = self.vis.table.ensure("s" + str(len(self.vis.table.all())), "/tmp/repo")
        s.terminal["tty"] = "/dev/ttys004"
        for key, value in kwargs.items():
            setattr(s, key, value)
        return s

    def test_first_paint_writes(self):
        self.add(state="permission", tab_title="Fix the parser")
        self.vis.paint_tabs(1000.0)
        self.assertEqual(len(self.writes), 1)
        self.assertTrue(self.writes[0][1].endswith("Fix the parser"))

    def test_an_unchanged_board_writes_nothing(self):
        self.add(state="working", tab_title="Fix the parser")
        self.vis.paint_tabs(1000.0)
        self.vis.paint_tabs(2000.0)
        self.assertEqual(len(self.writes), 1)

    def test_churn_inside_a_turn_is_one_write(self):
        s = self.add(state="thinking", tab_title="Fix the parser")
        for tick, state in enumerate(
            ["thinking", "working", "streaming", "working", "thinking"], start=1
        ):
            s.state = state
            self.vis.paint_tabs(1000.0 + tick * config.TAB_POLL_SECONDS)
        self.assertEqual(len(self.writes), 1)

    def test_the_tick_is_rate_limited(self):
        s = self.add(state="working", tab_title="a")
        self.vis.paint_tabs(1000.0)
        s.state = "done"
        self.vis.paint_tabs(1000.0 + config.TAB_POLL_SECONDS / 2)
        self.assertEqual(len(self.writes), 1)

    def test_a_new_title_gets_through_without_a_state_change(self):
        s = self.add(state="working", tab_title="Fix the parser")
        self.vis.paint_tabs(1000.0)
        s.tab_title = "Fix the lexer"
        self.vis.paint_tabs(2000.0)
        self.assertEqual(len(self.writes), 2)

    def test_a_session_with_no_tty_is_skipped(self):
        s = self.add(state="permission", tab_title="x")
        s.terminal.clear()
        self.vis.paint_tabs(1000.0)
        self.assertEqual(self.writes, [])

    def test_the_directory_stands_in_until_a_title_exists(self):
        self.add(state="done", cwd="/Users/x/projects/repo")
        self.vis.paint_tabs(1000.0)
        self.assertTrue(self.writes[0][1].endswith("repo"))

    def test_ending_hands_the_tab_back(self):
        s = self.add(state="working", tab_title="Fix the parser")
        self.vis.paint_tabs(1000.0)
        s.state = "ended"
        self.vis.paint_tabs(2000.0)
        self.assertEqual(self.writes[-1][1], "Fix the parser")

    def test_shutdown_hands_every_tab_back(self):
        self.add(state="permission", tab_title="Fix the parser")
        self.vis.paint_tabs(1000.0)
        self.vis.restore_tabs()
        self.assertEqual(self.writes[-1][1], "Fix the parser")

    def test_a_tty_a_live_session_still_holds_is_left_alone(self):
        """Two records for one tab: dropping the duplicate must not strip the
        glyph off the survivor."""
        survivor = self.add(state="permission", tab_title="Fix the parser")
        duplicate = self.add(state="idle", tab_title="Fix the parser")
        self.vis.paint_tabs(1000.0)
        self.writes.clear()
        self.vis.restore_tabs([duplicate])
        self.assertEqual(self.writes, [])
        self.assertIn(config.TAB_GLYPHS["permission"], survivor.tab_painted)

    def test_the_feature_can_be_turned_off(self):
        self.add(state="permission", tab_title="x")
        real = config.TAB_TITLE
        config.TAB_TITLE = False
        self.addCleanup(lambda: setattr(config, "TAB_TITLE", real))
        self.vis.paint_tabs(1000.0)
        self.vis.restore_tabs()
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
