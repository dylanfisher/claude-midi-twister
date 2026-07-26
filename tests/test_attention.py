"""The tab in front of you: who it resolves to, and what it costs to find out.

The window server and AppleScript are both injected, so what is pinned here is
the decision layer -- when the free answer is already the exact one, when the
subprocess is worth spending, and above all the cases where the honest answer is
"nobody". That last one is the whole risk of this feature: a marker on the wrong
knob moves your eye somewhere real work is not (invariant 6), so the refusals
are tested harder than the successes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import attention, board, config, overlays  # noqa: E402
from mft.render import Cell  # noqa: E402
from mft.state import SessionTable  # noqa: E402


TERMINAL_APP = {"TERM_PROGRAM": "Apple_Terminal"}
ITERM = {"TERM_PROGRAM": "iTerm.app"}


class Watcher(attention.AttentionWatcher):
    """The real watcher with both outside calls replaced by a variable.

    ``_ask_now`` is run inline rather than on a thread: the thread is not the
    behaviour under test and a test that has to wait for one is a test that
    intermittently does not.
    """

    def __init__(self, front_app: str = "", tty: str = "") -> None:
        super().__init__(front=lambda: self.front_app)
        self.front_app = front_app
        self.tty = tty
        self.asks = 0

    def _ask(self, terminal, now, force):  # type: ignore[override]
        self.asks += 1
        self._last_ask = now
        terminal = attention.Terminal(
            terminal.name, terminal.owners, front_tty=lambda: self.tty
        )
        self._ask_now(terminal)


class Resolving(unittest.TestCase):
    """Tokens to a session, which is :mod:`mft.identity`'s ranking read backwards."""

    def setUp(self):
        self.table = SessionTable()

    def session(self, sid: str, tty: str, terminal=TERMINAL_APP):
        """`ensure` takes an identity payload; ``session.terminal`` -- the env
        snapshot the adapters read -- is written by the daemon, so a test that
        wants one has to say so."""
        session = self.table.ensure(sid, "/tmp/p", {**terminal, "tty": tty})
        session.terminal = {**terminal, "tty": tty}
        return session

    def test_a_tty_finds_its_session(self):
        self.session("a", "/dev/ttys001")
        wanted = self.session("b", "/dev/ttys002")
        found = attention.resolve(attention.tty_keys("/dev/ttys002"), self.table.all())
        self.assertIs(found, wanted)

    def test_either_spelling_of_a_tty_finds_it(self):
        """`ps` says ``ttys004`` and AppleScript says ``/dev/ttys004``, and which
        one is on the record depends on whether it arrived by hook or by
        discovery."""
        wanted = self.session("a", "ttys004")
        found = attention.resolve(attention.tty_keys("/dev/ttys004"), self.table.all())
        self.assertIs(found, wanted)

    def test_an_unknown_tty_is_nobody(self):
        self.session("a", "/dev/ttys001")
        self.assertIsNone(
            attention.resolve(attention.tty_keys("/dev/ttys009"), self.table.all())
        )

    def test_nothing_resolves_to_nobody(self):
        self.session("a", "/dev/ttys001")
        self.assertIsNone(attention.resolve((), self.table.all()))

    def test_two_records_on_one_token_resolve_to_nobody(self):
        """The state `SessionTable.reconcile` exists to repair. Marking one of
        them in the meantime is picking a knob by coin toss."""
        one = self.session("a", "/dev/ttys001")
        two = self.table.ensure("b", "/tmp/q", {"pid": 4242})
        two.keys = set(one.keys)
        self.assertIsNone(
            attention.resolve(attention.tty_keys("/dev/ttys001"), self.table.all())
        )

    def test_a_tty_that_names_nothing_yields_no_tokens(self):
        for junk in ("", "  ", "??", "-"):
            self.assertEqual(attention.tty_keys(junk), ())


class WhichTerminal(unittest.TestCase):
    def setUp(self):
        self.table = SessionTable()

    def test_the_window_server_name_picks_the_adapter(self):
        self.assertEqual(attention.terminal_for("Terminal").name, "terminal.app")
        self.assertEqual(attention.terminal_for("iTerm2").name, "iterm2")
        self.assertEqual(attention.terminal_for("Ghostty").name, "ghostty")

    def test_anything_else_is_not_a_terminal(self):
        for app in ("Safari", "Figma", "", "Slack"):
            self.assertIsNone(attention.terminal_for(app))

    def test_a_session_is_matched_by_whichever_field_its_hooks_reported(self):
        term = attention.terminal_for("Terminal")
        by_program = self.table.ensure("a", "/tmp", {"tty": "/dev/ttys001"})
        by_program.terminal = {"TERM_PROGRAM": "Apple_Terminal"}
        by_bundle = self.table.ensure("b", "/tmp", {"tty": "/dev/ttys002"})
        by_bundle.terminal = {"__CFBundleIdentifier": "com.apple.Terminal"}
        self.assertTrue(term.hosts(by_program))
        self.assertTrue(term.hosts(by_bundle))

    def test_a_session_in_another_terminal_is_not_hosted(self):
        term = attention.terminal_for("Terminal")
        elsewhere = self.table.ensure("a", "/tmp", {"tty": "/dev/ttys001"})
        elsewhere.terminal = dict(ITERM)
        self.assertFalse(term.hosts(elsewhere))

    def test_a_terminal_that_reported_nothing_is_hosted_by_nobody(self):
        """A bare pid names no application, and guessing one would put the marker
        on a knob chosen by which adapter happened to be asked first."""
        naked = self.table.ensure("a", "/tmp", {"pid": 999})
        naked.terminal = {"pid": 999}
        for term in attention.TERMINALS:
            self.assertFalse(term.hosts(naked))


class Polling(unittest.TestCase):
    """What each poll costs, which is the whole design."""

    def setUp(self):
        self.table = SessionTable()
        self.watcher = Watcher()

    def session(self, sid: str, tty: str, terminal=TERMINAL_APP):
        """`ensure` takes an identity payload; ``session.terminal`` -- the env
        snapshot the adapters read -- is written by the daemon, so a test that
        wants one has to say so."""
        session = self.table.ensure(sid, "/tmp/p", {**terminal, "tty": tty})
        session.terminal = {**terminal, "tty": tty}
        return session

    def poll(self, now: float = 100.0):
        self.watcher.poll(now, self.table.all())
        return self.watcher.focused(self.table.all())

    def test_one_session_in_the_front_app_costs_no_subprocess(self):
        """The common desk, and the reason this feature is affordable at all."""
        wanted = self.session("a", "/dev/ttys001")
        self.watcher.front_app = "Terminal"
        self.assertIs(self.poll(), wanted)
        self.assertEqual(self.watcher.asks, 0)

    def test_a_browser_in_front_marks_nothing_and_asks_nothing(self):
        self.session("a", "/dev/ttys001")
        self.watcher.front_app = "Safari"
        self.assertIsNone(self.poll())
        self.assertEqual(self.watcher.asks, 0)

    def test_two_sessions_in_one_terminal_spend_the_subprocess(self):
        self.session("a", "/dev/ttys001")
        wanted = self.session("b", "/dev/ttys002")
        self.watcher.front_app = "Terminal"
        self.watcher.tty = "/dev/ttys002"
        self.assertIs(self.poll(), wanted)
        self.assertEqual(self.watcher.asks, 1)

    def test_two_sessions_in_a_terminal_that_cannot_be_asked_mark_nothing(self):
        """Ghostty has no ``front_tty``. One session there is still free and
        exact; two is a question it cannot answer, and inventing an answer is
        the failure this whole module is shaped to avoid."""
        self.session("a", "/dev/ttys001", terminal={"TERM_PROGRAM": "ghostty"})
        self.session("b", "/dev/ttys002", terminal={"TERM_PROGRAM": "ghostty"})
        self.watcher.front_app = "Ghostty"
        self.assertIsNone(self.poll())
        self.assertEqual(self.watcher.asks, 0)

    def test_a_terminal_hosting_nothing_marks_nothing(self):
        self.session("a", "/dev/ttys001", terminal=ITERM)
        self.watcher.front_app = "Terminal"
        self.assertIsNone(self.poll())
        self.assertEqual(self.watcher.asks, 0)

    def test_an_answer_that_names_no_session_marks_nothing(self):
        """A tmux client's tty is not its pane's, and this is what that looks
        like from in here."""
        self.session("a", "/dev/ttys001")
        self.session("b", "/dev/ttys002")
        self.watcher.front_app = "Terminal"
        self.watcher.tty = "/dev/ttys099"
        self.assertIsNone(self.poll())

    def test_the_cheap_poll_is_rate_limited(self):
        self.session("a", "/dev/ttys001")
        self.watcher.front_app = "Terminal"
        self.assertIsNotNone(self.poll(now=100.0))
        # Switching away inside the interval is not seen until the interval is up.
        self.watcher.front_app = "Safari"
        self.assertIsNotNone(self.poll(now=100.0 + config.ATTENTION_POLL_SECONDS / 2))
        self.assertIsNone(self.poll(now=100.0 + config.ATTENTION_POLL_SECONDS * 2))

    def test_switching_apps_clears_a_stale_answer_for_free(self):
        """The expensive question is never asked to *unset* the marker: the
        window server already said you are somewhere else."""
        self.session("a", "/dev/ttys001")
        self.session("b", "/dev/ttys002")
        self.watcher.front_app = "Terminal"
        self.watcher.tty = "/dev/ttys002"
        self.assertIsNotNone(self.poll(now=100.0))
        asks = self.watcher.asks
        self.watcher.front_app = "Figma"
        self.assertIsNone(self.poll(now=200.0))
        self.assertEqual(self.watcher.asks, asks)

    def test_an_unreachable_window_server_marks_nothing(self):
        """Not macOS, or the symbols are missing. No marker, no crash, no log
        every quarter second."""
        self.session("a", "/dev/ttys001")
        watcher = attention.AttentionWatcher(front=lambda: None)
        watcher.poll(100.0, self.table.all())
        self.assertIsNone(watcher.focused(self.table.all()))

    def test_a_window_server_that_raises_marks_nothing(self):
        def boom():
            raise OSError("no window server")

        self.session("a", "/dev/ttys001")
        watcher = attention.AttentionWatcher(front=boom)
        watcher.poll(100.0, self.table.all())
        self.assertIsNone(watcher.focused(self.table.all()))


class Marking(unittest.TestCase):
    """What the focused encoder actually looks like."""

    def setUp(self):
        self.table = SessionTable()

    def test_the_marker_is_on_the_ring_and_only_the_ring(self):
        """Hue and RGB brightness are spent on what the session is doing and how
        much it wants you; the ring has its own channel, which is why the marker
        survives an animation that discards brightness outright."""
        cells = [Cell("green", config.ANIM_NONE, 40, 0.2)] * 4
        board.mark_focus(cells, 2)
        self.assertEqual(cells[2].ring_level, config.ATTENTION_RING_LEVEL)
        self.assertEqual(cells[2].color, "green")
        self.assertEqual(cells[2].ring, 40)
        self.assertEqual(cells[2].brightness, 0.2)
        self.assertIsNone(cells[1].ring_level)

    def test_a_dark_encoder_is_not_marked(self):
        cells = [board.BLANK] * 4
        board.mark_focus(cells, 1)
        self.assertIs(cells[1], board.BLANK)

    def test_a_slot_off_the_board_is_ignored(self):
        cells = [board.BLANK] * 4
        board.mark_focus(cells, 99)
        board.mark_focus(cells, -1)

    def test_compose_marks_the_focused_session(self):
        session = self.table.ensure("a", "/tmp", {"tty": "/dev/ttys001"})
        cells = board.compose(self.table.all(), 100.0, focused=session.slot)
        self.assertEqual(cells[session.slot].ring_level, config.ATTENTION_RING_LEVEL)

    def test_a_sleeping_board_keeps_the_tab_you_are_looking_at_lit(self):
        """The room is not empty if a session's tab is in front of you -- that
        is the strongest evidence of presence the daemon ever gets."""
        session = self.table.ensure("a", "/tmp", {"tty": "/dev/ttys001"})
        session.state = "working"
        dark = board.compose(self.table.all(), 100.0, sleep=0.05)
        lit = board.compose(self.table.all(), 100.0, sleep=0.05, focused=session.slot)
        self.assertGreater(
            lit[session.slot].brightness, dark[session.slot].brightness
        )


class Pulsing(unittest.TestCase):
    """The swell, which is pure paint and so is the easy half."""

    def setUp(self):
        self.table = SessionTable()
        self.session = self.table.ensure("a", "/tmp", {"tty": "/dev/ttys001"})
        self.under = Cell("green", config.SLOW_ANIM, 40, 0.15)

    def paint(self, at: float) -> Cell:
        cells = [self.under] * 4
        overlays.FocusOverlay(self.session, 0.0).apply(cells, at)
        return cells[self.session.slot]

    def test_it_swells_and_settles(self):
        peak = self.paint(config.ATTENTION_PULSE_SECONDS * config.ATTENTION_PULSE_RISE)
        self.assertAlmostEqual(peak.brightness, 1.0, places=2)
        late = self.paint(config.ATTENTION_PULSE_SECONDS * 0.95)
        self.assertLess(late.brightness, peak.brightness)

    def test_it_drops_the_animation_so_the_swell_can_be_seen(self):
        """Brightness does not reach the RGB while an animation is on it, so a
        pulse that kept one would be a pulse you cannot see."""
        self.assertEqual(self.paint(0.1).rgb_anim, config.ANIM_NONE)

    def test_it_never_dims_what_is_already_brighter(self):
        self.under = Cell("red", config.ANIM_NONE, 127, 1.0)
        self.assertEqual(self.paint(config.ATTENTION_PULSE_SECONDS * 0.9).brightness, 1.0)

    def test_it_keeps_the_session_hue(self):
        """A spawn strike is news and wears its own colour. This is an
        acknowledgement, so it says nothing you did not already know."""
        self.assertEqual(self.paint(0.1).color, "green")

    def test_it_leaves_a_dark_encoder_dark(self):
        self.under = board.BLANK
        self.assertIs(self.paint(0.1), board.BLANK)

    def test_it_retires(self):
        overlay = overlays.FocusOverlay(self.session, 0.0)
        self.assertFalse(overlay.done(config.ATTENTION_PULSE_SECONDS * 0.5))
        self.assertTrue(overlay.done(config.ATTENTION_PULSE_SECONDS + 0.01))


if __name__ == "__main__":
    unittest.main()
