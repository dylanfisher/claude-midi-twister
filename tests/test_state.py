"""python3 -m unittest discover tests"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import board, config, context, font, overlays, twister  # noqa: E402
from mft import render as render_mod  # noqa: E402
from mft.render import Cell, attention_debt, render  # noqa: E402
from mft.events import apply_event, classify_notification  # noqa: E402
from mft.identity import merge_terminal, terminal_keys  # noqa: E402
from mft.state import SessionTable, Subagent  # noqa: E402


def event(name: str, **kw) -> dict:
    return {"hook_event_name": name, "session_id": "s", **kw}


def set_subagents(session, count: int) -> None:
    """Put `count` subagents in flight, for the board tests.

    `Session.subagents` is derived from the identifiers of the subagents in
    flight rather than being a number anyone can set, so the tests that only
    care about how many dots appear go through here.

    Both stamps are left at zero -- long ago, so every dot sits at the shimmer
    floor, and spawned at the origin so its ring is whatever the board's clock
    says. Tests about brightness or the stopwatch set their own.
    """
    session.subagents_in_flight = {
        f"agent:{i}": Subagent(0.0, 0.0) for i in range(count)
    }


class SlotAllocation(unittest.TestCase):
    def test_sessions_get_stable_distinct_slots(self):
        table = SessionTable()
        a = table.ensure("a", "/tmp/a")
        b = table.ensure("b", "/tmp/b")
        self.assertNotEqual(a.slot, b.slot)
        self.assertIs(table.ensure("a"), a)

    def test_runs_out_of_slots_gracefully(self):
        table = SessionTable(slot_count=2)
        table.ensure("a")
        table.ensure("b")
        self.assertIsNone(table.ensure("c"))

    def test_released_slot_is_reused(self):
        table = SessionTable(slot_count=1)
        a = table.ensure("a")
        table.release(a)
        b = table.ensure("b")
        self.assertEqual(b.slot, 0)

    def test_sessions_fill_from_the_top_left_with_no_gaps(self):
        table = SessionTable()
        sessions = [table.ensure(name) for name in "abc"]
        self.assertEqual([s.slot for s in sessions], [0, 1, 2])

    def test_losing_a_session_closes_the_hole_it_left(self):
        table = SessionTable()
        a, b, c = (table.ensure(name) for name in "abc")
        table.release(b)
        self.assertEqual([a.slot, c.slot], [0, 1], "no dark encoder in the middle")
        self.assertIs(table.by_slot(1), c)
        self.assertEqual(table.ensure("d").slot, 2)

    def test_an_ended_session_sinks_below_the_live_ones(self):
        table = SessionTable()
        a, b = table.ensure("a"), table.ensure("b")
        apply_event(a, event("SessionEnd", session_id="a"))
        table.compact()
        self.assertEqual([b.slot, a.slot], [0, 1])

    def test_a_new_session_displaces_a_lingering_ended_one(self):
        """An ended session renders dark, so it never holds the top-left."""
        table = SessionTable(slot_count=2)
        a, b = table.ensure("a"), table.ensure("b")
        apply_event(b, event("SessionEnd", session_id="b"))
        c = table.ensure("c")
        self.assertEqual([a.slot, c.slot], [0, 1])
        self.assertIsNone(table.get("b"))


class TerminalIdentity(unittest.TestCase):
    """A `/clear` hands out a brand new session id in the same tab. Keying
    slots on the session id would teleport the agent to a different knob."""

    TERM = {"TERM_PROGRAM": "Apple_Terminal", "tty": "/dev/ttys004"}

    def test_new_session_id_in_the_same_terminal_keeps_the_slot(self):
        table = SessionTable()
        first = table.ensure("old", "/tmp/p", self.TERM)
        first.turn_count = 7
        second = table.ensure("new", "/tmp/p", self.TERM)
        self.assertEqual(second.slot, first.slot)
        self.assertEqual(second.session_id, "new")
        self.assertEqual(second.turn_count, 7, "history should survive a /clear")
        self.assertIsNone(table.get("old"))
        self.assertEqual(len(table.all()), 1)

    def test_different_terminals_get_different_slots(self):
        table = SessionTable()
        a = table.ensure("a", "/tmp/p", self.TERM)
        b = table.ensure("b", "/tmp/p", {**self.TERM, "tty": "/dev/ttys005"})
        self.assertNotEqual(a.slot, b.slot)

    def test_terminal_arriving_late_adopts_the_existing_slot(self):
        # SessionStart is async, so a plain HTTP event can land first.
        table = SessionTable()
        early = table.ensure("a", "/tmp/p")
        late = table.ensure("a", "/tmp/p", self.TERM)
        self.assertIs(early, late)
        self.assertEqual(late.key, "tty:/dev/ttys004")
        self.assertIs(table.ensure("b", "/tmp/p", self.TERM), late)

    def test_multiplexer_pane_wins_over_tty(self):
        table = SessionTable()
        session = table.ensure("a", "", {"TMUX_PANE": "%3", "tty": "/dev/ttys004"})
        self.assertEqual(session.key, "TMUX_PANE:%3")

    def test_one_field_in_common_is_enough_to_recognise_a_tab(self):
        # `register_session.py` reports the whole environment and `notify.sh`
        # reports the little it can get cheaply, so two events about one tab
        # overlap only partly. Matching on the strongest token of each payload
        # would make those two descriptions two tabs.
        table = SessionTable()
        full = table.ensure("a", "/tmp/p", {"TERM_SESSION_ID": "w0:1", "tty": "/dev/ttys004"})
        thin = table.ensure("b", "/tmp/p", {"tty": "/dev/ttys004"})
        self.assertIs(thin, full)
        self.assertEqual(len(table.all()), 1)

    def test_a_tab_never_ends_up_on_two_encoders(self):
        # The orphan: a plain event for the new session id lands before anything
        # says which tab it is in, so it gets an encoder of its own. When the
        # identity finally arrives, the two records are one tab and the live one
        # keeps the encoder the tab has been using.
        table = SessionTable()
        first = table.ensure("old", "/tmp/p", self.TERM)
        first.terminal = dict(self.TERM)
        blind = table.ensure("new", "/tmp/p")
        self.assertNotEqual(blind.slot, first.slot, "nothing to key on yet")

        merged = table.ensure("new", "/tmp/p", self.TERM)
        self.assertEqual(len(table.all()), 1)
        self.assertEqual(merged.session_id, "new")
        self.assertEqual(merged.slot, 0, "on the encoder the tab already had")
        self.assertEqual(
            merged.terminal, self.TERM, "and focusable, which the blind record was not"
        )

    def test_a_record_handed_the_wrong_tab_corrects_itself(self):
        table = SessionTable()
        session = table.ensure("a", "/tmp/p", self.TERM)
        moved = table.ensure("a", "/tmp/p", {**self.TERM, "tty": "/dev/ttys009"})
        self.assertIs(moved, session)
        self.assertEqual(moved.key, "tty:/dev/ttys009")
        self.assertNotIn("tty:/dev/ttys004", moved.keys)
        # And the tty it gave up no longer answers for it.
        other = table.ensure("b", "/tmp/p", self.TERM)
        self.assertIsNot(other, moved)

    def test_terminal_descriptions_are_unioned_not_replaced(self):
        stored = {"TERM_PROGRAM": "Apple_Terminal", "tty": "/dev/ttys004", "pid": "9"}
        thin = {"tty": "/dev/ttys004"}
        self.assertEqual(merge_terminal(stored, thin), stored)
        # ...unless they disagree about which tab this is, and then the new one
        # is the whole truth rather than half of a mixture of two tabs.
        self.assertEqual(
            merge_terminal(stored, {"tty": "/dev/ttys009"}), {"tty": "/dev/ttys009"}
        )

    def test_tokens_are_ordered_by_how_well_they_survive(self):
        self.assertEqual(
            terminal_keys({"tty": "/dev/ttys004", "TMUX_PANE": "%3", "pid": "9"}),
            ["TMUX_PANE:%3", "tty:/dev/ttys004", "pid:9"],
        )


class ClearedSlots(unittest.TestCase):
    """`/clear` retires a session id and keeps the tab. The replacement's first
    event routinely beats the hook that says where it lives, so the wiped slot
    has to be able to recognise its own other half with nothing to go on."""

    TERM = {"TERM_PROGRAM": "Apple_Terminal", "tty": "/dev/ttys004"}

    def cleared_table(self):
        table = SessionTable()
        session = table.ensure("old", "/tmp/p", self.TERM)
        session.terminal = dict(self.TERM)
        apply_event(session, {"hook_event_name": "SessionEnd", "reason": "clear"})
        return table, session

    def test_the_replacement_lands_on_the_wiped_encoder(self):
        table, old = self.cleared_table()
        new = table.ensure("new", "/tmp/p")  # a tool call, no terminal
        self.assertIs(new, old)
        self.assertEqual(new.session_id, "new")
        self.assertEqual(new.slot, 0)
        self.assertEqual(len(table.all()), 1)
        self.assertEqual(new.terminal, self.TERM, "still focusable")

    def test_a_different_tab_in_the_same_directory_is_left_alone(self):
        # It says which tab it is in, so it is not the other half of anything.
        table, old = self.cleared_table()
        other = table.ensure("b", "/tmp/p", {**self.TERM, "tty": "/dev/ttys009"})
        self.assertIsNot(other, old)
        self.assertEqual(len(table.all()), 2)

    def test_the_window_closes(self):
        table, old = self.cleared_table()
        old.cleared_at = time.monotonic() - config.CLEAR_ADOPT_SECONDS - 1
        self.assertIsNot(table.ensure("new", "/tmp/p"), old)

    def test_a_cleared_session_that_spoke_again_is_not_a_ghost(self):
        table, old = self.cleared_table()
        apply_event(old, {"hook_event_name": "UserPromptSubmit"})
        self.assertIsNot(table.ensure("new", "/tmp/p"), old)


class HandedOff(unittest.TestCase):
    """Claude Code hands a conversation to a pre-warmed process under its own
    background daemon: new session id, no tty, and an environment inherited from
    whichever tab started that daemon. Nothing announces the move, so the tab's
    encoder would freeze on the last state it heard while the live session lit a
    second knob nobody can press."""

    TERM = {"TERM_PROGRAM": "Apple_Terminal", "tty": "/dev/ttys005", "pid": "26655"}
    HOST = {"pid": "27843"}

    def quiet_tab(self):
        """A tab at rest: it finished a turn a moment ago and went silent."""
        table = SessionTable()
        tab = table.ensure("old", "/tmp/p", self.TERM)
        tab.terminal = dict(self.TERM)
        apply_event(tab, {"hook_event_name": "Stop"})
        return table, tab

    def test_a_bare_pid_is_not_a_tab(self):
        self.assertEqual(terminal_keys(self.HOST), ["host:27843"])
        self.assertEqual(terminal_keys({"pid": "9", "tty": "/dev/ttys004"})[-1], "pid:9")

    def test_the_moved_session_keeps_the_tab_s_encoder(self):
        table, tab = self.quiet_tab()
        moved = table.ensure("new", "/tmp/p", self.HOST)
        self.assertIs(moved, tab)
        self.assertEqual(moved.session_id, "new")
        self.assertEqual(moved.slot, 0)
        self.assertEqual(len(table.all()), 1, "one tab, one encoder")
        # The tab's own pid is not contradicted by the host's, so the record can
        # be both the session that is running and the terminal it is shown in.
        self.assertIn("pid:26655", moved.keys)
        self.assertIn("host:27843", moved.keys)
        self.assertEqual(moved.key, "tty:/dev/ttys005", "still names the tab")

    def test_the_host_s_events_land_on_that_encoder_too(self):
        table, tab = self.quiet_tab()
        table.ensure("new", "/tmp/p", self.HOST)
        self.assertIs(table.ensure("new", "/tmp/p", self.HOST), tab)
        # ...and so does the tab, if it starts speaking for itself again.
        self.assertIs(table.ensure("newer", "/tmp/p", self.TERM), tab)
        self.assertEqual(len(table.all()), 1)

    def test_the_tab_stays_focusable(self):
        # The whole risk of merging these: a bare pid taken as the truth about
        # the terminal would leave the encoder pointing at a pty host with no
        # window, so a press would raise nothing.
        self.assertEqual(merge_terminal(self.TERM, self.HOST), self.TERM)
        # Two descriptions of the *same* bare process still replace each other.
        self.assertEqual(merge_terminal({"pid": "1"}, {"pid": "2"}), {"pid": "2"})

    def test_a_tab_mid_turn_has_not_handed_off(self):
        table = SessionTable()
        busy = table.ensure("old", "/tmp/p", self.TERM)
        apply_event(busy, {"hook_event_name": "UserPromptSubmit"})
        moved = table.ensure("new", "/tmp/p", self.HOST)
        self.assertIsNot(moved, busy)
        self.assertEqual(len(table.all()), 2)

    def test_a_stalled_turn_counts_as_over(self):
        # `Stop` is the only thing that ends a turn, and it can go missing. A
        # record stuck mid-turn forever would never be repairable.
        table = SessionTable()
        busy = table.ensure("old", "/tmp/p", self.TERM)
        apply_event(busy, {"hook_event_name": "UserPromptSubmit"})
        busy.last_event_at = time.monotonic() - config.STALL_SECONDS - 1
        self.assertIs(table.ensure("new", "/tmp/p", self.HOST), busy)

    def test_the_window_closes(self):
        table, tab = self.quiet_tab()
        tab.last_event_at = time.monotonic() - config.HANDOFF_ADOPT_SECONDS - 1
        self.assertIsNot(table.ensure("new", "/tmp/p", self.HOST), tab)

    def test_a_session_with_no_tab_in_its_directory_gets_its_own_encoder(self):
        # The desktop app, which has no terminal at all and never did.
        table = SessionTable()
        table.ensure("old", "/tmp/p", self.TERM)
        app = table.ensure("new", "/tmp/q", self.HOST)
        self.assertEqual(app.slot, 1)
        self.assertEqual(app.key, "host:27843")

    def test_the_identity_may_arrive_after_the_encoder_did(self):
        # `notify.sh` reports nothing at all for a session with no tty, and it
        # routinely beats the hook that reports the pid, so the moved session can
        # already have a knob of its own by the time anything can be matched.
        table, tab = self.quiet_tab()
        blind = table.ensure("new", "/tmp/p")
        self.assertIsNot(blind, tab, "nothing to go on yet")
        merged = table.ensure("new", "/tmp/p", self.HOST)
        self.assertEqual(len(table.all()), 1)
        self.assertEqual(merged.session_id, "new")
        self.assertEqual(merged.slot, 0, "on the encoder the tab was using")
        self.assertEqual(merged.terminal, self.TERM, "and focusable")

    def test_the_reported_failure_end_to_end(self):
        """Knob 3 dim green while the session it is showing works on knob 6."""
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        vis = Visualizer(NullTwister())
        for name in ("SessionStart", "UserPromptSubmit", "Stop"):
            vis.handle_event(
                {
                    "session_id": "old",
                    "cwd": "/tmp/p",
                    "hook_event_name": name,
                    "terminal": self.TERM,
                }
            )
        # The conversation moves into a spare process, which announces itself and
        # then gets to work.
        vis.handle_event(
            {
                "session_id": "new",
                "cwd": "/tmp/p",
                "hook_event_name": "SessionStart",
                "terminal": self.HOST,
            }
        )
        vis.handle_event(
            {
                "session_id": "new",
                "cwd": "/tmp/p",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "terminal": self.HOST,
            }
        )
        sessions = vis.table.all()
        self.assertEqual(len(sessions), 1, "one tab, one encoder")
        self.assertEqual(sessions[0].session_id, "new")
        self.assertEqual(sessions[0].state, "working", "and it is the live state")
        self.assertEqual(sessions[0].terminal["tty"], "/dev/ttys005")


class NoOrphans(unittest.TestCase):
    """The fallback sweep. Whatever gets past `ensure`, a tab may not hold two
    encoders -- and an orphaned record is invisible from across the room, so it
    is checked rather than trusted."""

    TERM = {"TERM_PROGRAM": "Apple_Terminal", "tty": "/dev/ttys004"}

    def test_an_identity_written_from_outside_is_still_reconciled(self):
        # `discover` sets `terminal` on a session it did not create, so the
        # duplicate it makes never passes through `ensure` at all.
        table = SessionTable()
        live = table.ensure("a", "/tmp/p", self.TERM)
        stray = table.ensure("b", "/tmp/q")
        stray.terminal = dict(self.TERM)
        stray.last_event_at = live.last_event_at + 1

        dropped = table.reconcile()
        self.assertEqual([s.session_id for s in dropped], ["a"])
        self.assertEqual(len(table.all()), 1)
        self.assertEqual(table.by_slot(0), stray)
        self.assertEqual(stray.slot, 0, "on the older encoder")

    def test_the_ghost_of_a_clear_is_released(self):
        table = SessionTable()
        old = table.ensure("old", "/tmp/p", self.TERM)
        old.terminal = dict(self.TERM)
        apply_event(old, {"hook_event_name": "SessionEnd", "reason": "clear"})
        # A replacement that landed outside the adoption window, so it is on an
        # encoder of its own and knows no terminal: nothing will ever join these
        # two up, and both would sit there for an hour of TTL.
        old.cleared_at = time.monotonic() - config.CLEAR_ADOPT_SECONDS - 1
        old.last_event_at = old.cleared_at
        stray = table.ensure("new", "/tmp/p")
        self.assertEqual(len(table.all()), 2)

        dropped = table.reconcile()
        self.assertEqual([s.session_id for s in dropped], ["old"])
        self.assertEqual(table.all(), [stray])
        self.assertEqual(stray.slot, 0, "and the board closes up behind it")

    def test_a_quiet_second_tab_is_not_a_ghost(self):
        # Same directory, cleared, silent -- but the other session named its own
        # tab, so it is a session and not the other half of this clear.
        table = SessionTable()
        old = table.ensure("old", "/tmp/p", self.TERM)
        apply_event(old, {"hook_event_name": "SessionEnd", "reason": "clear"})
        old.cleared_at = time.monotonic() - config.CLEAR_ADOPT_SECONDS - 1
        old.last_event_at = old.cleared_at
        table.ensure("b", "/tmp/p", {**self.TERM, "tty": "/dev/ttys009"})

        self.assertEqual(table.reconcile(), [])
        self.assertEqual(len(table.all()), 2)

    def test_hooks_that_arrive_out_of_order_still_leave_one_knob(self):
        """The whole reported failure, driven through the daemon in the order it
        actually happens: `/clear`, then a tool call from the new session id, and
        only later the hook that says which tab that id lives in."""
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        vis = Visualizer(NullTwister())
        term = {"TERM_PROGRAM": "Apple_Terminal", "tty": "/dev/ttys004", "pid": "900"}
        vis.handle_event(
            {
                "session_id": "old",
                "cwd": "/tmp/p",
                "hook_event_name": "SessionStart",
                "terminal": term,
            }
        )
        vis.handle_event({"session_id": "old", "hook_event_name": "Stop", "cwd": "/tmp/p"})
        vis.handle_event(
            {
                "session_id": "old",
                "cwd": "/tmp/p",
                "hook_event_name": "SessionEnd",
                "reason": "clear",
            }
        )
        # `notify.sh` names the tab on every event, so the ordinary path is that
        # even this one is recognised...
        vis.handle_event(
            {
                "session_id": "new",
                "cwd": "/tmp/p",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "terminal": {"tty": "/dev/ttys004", "TERM_PROGRAM": "Apple_Terminal"},
            }
        )
        # ...and the late SessionStart changes nothing.
        vis.handle_event(
            {
                "session_id": "new",
                "cwd": "/tmp/p",
                "hook_event_name": "SessionStart",
                "source": "clear",
                "terminal": term,
            }
        )
        sessions = vis.table.all()
        self.assertEqual(len(sessions), 1, "one tab, one encoder")
        self.assertEqual(sessions[0].session_id, "new")
        self.assertEqual(sessions[0].slot, 0)
        self.assertEqual(
            sessions[0].terminal["pid"], "900", "the full identity survived the thin one"
        )
        self.assertIs(vis.table.by_slot(0), sessions[0], "and the press finds it")

    def test_an_anonymous_event_after_a_clear_lands_on_the_same_knob(self):
        # The same sequence with a hook that cannot name the tab at all: an
        # `http` hook, or a `notify.sh` older than this behaviour.
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        vis = Visualizer(NullTwister())
        vis.handle_event(
            {
                "session_id": "old",
                "cwd": "/tmp/p",
                "hook_event_name": "SessionStart",
                "terminal": {"tty": "/dev/ttys004"},
            }
        )
        vis.handle_event(
            {
                "session_id": "old",
                "cwd": "/tmp/p",
                "hook_event_name": "SessionEnd",
                "reason": "clear",
            }
        )
        vis.handle_event(
            {
                "session_id": "new",
                "cwd": "/tmp/p",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
            }
        )
        self.assertEqual(len(vis.table.all()), 1)
        session = vis.table.by_slot(0)
        self.assertEqual(session.session_id, "new")
        self.assertEqual(session.state, "working")
        self.assertEqual(session.terminal, {"tty": "/dev/ttys004"}, "still focusable")

    def test_the_terminal_header_names_the_tab(self):
        from mft.httpd import parse_terminal_header

        self.assertEqual(
            parse_terminal_header("tty=/dev/ttys004;TERM_SESSION_ID=w0t0p0:AB-12;"),
            {"tty": "/dev/ttys004", "TERM_SESSION_ID": "w0t0p0:AB-12"},
        )
        # An identity we can't read costs the event its tab, never the event.
        self.assertEqual(parse_terminal_header(""), {})
        self.assertEqual(parse_terminal_header("nonsense"), {})
        self.assertEqual(parse_terminal_header("tty=;=x;tty=/dev/ttys1"), {"tty": "/dev/ttys1"})
        self.assertEqual(parse_terminal_header("x=" + "y" * 8192), {})

    def test_a_healthy_board_is_left_exactly_as_it_was(self):
        table = SessionTable()
        a = table.ensure("a", "/tmp/a", {"tty": "/dev/ttys001"})
        b = table.ensure("b", "/tmp/b", {"tty": "/dev/ttys002"})
        c = table.ensure("c", "/tmp/c")  # no identity at all, and that is fine
        self.assertEqual(table.reconcile(), [])
        self.assertEqual([s.slot for s in (a, b, c)], [0, 1, 2])


class StateMachine(unittest.TestCase):
    def setUp(self):
        self.table = SessionTable()
        self.session = self.table.ensure("s", "/tmp/p")

    def feed(self, *events):
        effects = []
        for e in events:
            effects += apply_event(self.session, e)
        return effects

    def test_turn_lifecycle(self):
        self.feed(event("SessionStart"))
        self.assertEqual(self.session.state, "idle")
        self.feed(event("UserPromptSubmit"))
        self.assertEqual(self.session.state, "thinking")
        self.feed(event("PreToolUse", tool_name="Bash"))
        self.assertEqual(self.session.state, "working")
        self.assertEqual(self.session.tool_calls, 1)
        self.feed(event("Stop"))
        self.assertEqual(self.session.state, "done")

    def test_the_three_attention_states_are_distinct(self):
        for kind, expected in (
            ("permission_prompt", "permission"),
            ("idle_prompt", "waiting"),
            ("agent_needs_input", "waiting"),
            ("agent_completed", "done"),
        ):
            self.feed(event("Notification", notification_type=kind, message="..."))
            self.assertEqual(self.session.state, expected, kind)

    def test_a_finished_plan_is_not_the_same_as_a_permission_gate(self):
        # There is no PlanReady hook, so both roads in have to be recognised.
        self.feed(event("PermissionRequest", tool_name="ExitPlanMode"))
        self.assertEqual(self.session.state, "plan")
        self.feed(
            event(
                "Notification",
                notification_type="permission_prompt",
                message="Claude has written up a plan and is ready to execute. "
                "Would you like to proceed?",
            )
        )
        self.assertEqual(self.session.state, "plan")
        self.assertTrue(self.session.alert)
        self.assertNotEqual(
            config.STATE_COLORS["plan"], config.STATE_COLORS["permission"]
        )

    def test_an_ordinary_permission_request_is_still_a_permission(self):
        self.feed(event("PermissionRequest", tool_name="Bash"))
        self.assertEqual(self.session.state, "permission")

    def test_permission_prompt_raises_an_alert(self):
        self.feed(event("Notification", notification_type="permission_prompt"))
        self.assertEqual(self.session.state, "permission")
        self.assertTrue(self.session.alert)

    def test_untyped_notification_falls_back_to_the_message(self):
        self.assertEqual(
            classify_notification({"message": "Claude needs your permission"}),
            "permission",
        )
        self.assertEqual(classify_notification({"message": "waiting for your input"}), "waiting")

    def test_the_idle_nag_leaves_a_finished_session_green(self):
        # Claude Code posts this a minute after every turn ends. `done` is
        # already the right answer, and amber would take the floor back.
        self.feed(
            event("Stop"),
            event("Notification", notification_type="idle_prompt"),
        )
        self.assertEqual(self.session.state, "done")
        self.assertFalse(self.session.alert)

    def test_the_idle_nag_leaves_an_idle_session_alone(self):
        self.session.set_state("idle")
        self.feed(event("Notification", notification_type="idle_prompt"))
        self.assertEqual(self.session.state, "idle")
        self.assertFalse(self.session.alert)

    def test_an_untyped_idle_message_is_the_same_nag(self):
        self.feed(
            event("Stop"),
            event("Notification", message="Claude is idle"),
        )
        self.assertEqual(self.session.state, "done")

    def test_the_idle_nag_still_lands_on_a_session_that_is_not_resting(self):
        # Nothing else has said this turn ended, so the nag is the only thing
        # that knows -- amber beats an orange knob that is lying.
        self.feed(
            event("UserPromptSubmit"),
            event("Notification", notification_type="idle_prompt"),
        )
        self.assertEqual(self.session.state, "waiting")
        self.assertTrue(self.session.alert)

    def test_a_real_ask_is_still_amber_after_a_finished_turn(self):
        self.feed(
            event("Stop"),
            event("Notification", notification_type="agent_needs_input"),
        )
        self.assertEqual(self.session.state, "waiting")
        self.assertTrue(self.session.alert)

    def test_an_unreadable_notification_is_not_treated_as_the_nag(self):
        self.feed(event("Stop"), event("Notification"))
        self.assertEqual(self.session.state, "waiting")

    def test_next_prompt_clears_the_alert(self):
        self.feed(
            event("Notification", notification_type="permission_prompt"),
            event("UserPromptSubmit"),
        )
        self.assertFalse(self.session.alert)
        self.assertIsNone(self.session.attention_since)

    def test_stop_failure_is_an_error(self):
        self.feed(event("StopFailure", error_type="overloaded"))
        self.assertEqual(self.session.state, "error")
        self.assertTrue(self.session.alert)

    def test_rate_limit_asks_for_a_banner(self):
        effects = self.feed(event("StopFailure", error_type="rate_limit_error"))
        self.assertIn("banner:RATE", effects)

    def test_session_start_asks_for_the_spawn_flash(self):
        # `idle` is a dim pip; without this the arrival of a Claude is the least
        # visible thing the board does.
        self.assertIn("spawn", self.feed(event("SessionStart")))
        self.assertEqual(self.feed(event("UserPromptSubmit")), [])

    def test_compaction_brackets_are_effects(self):
        self.assertIn("compact:start", self.feed(event("PreCompact", trigger="auto")))
        self.assertIsNotNone(self.session.compacting_since)
        self.assertIn("compact:end", self.feed(event("PostCompact", trigger="auto")))
        self.assertIsNone(self.session.compacting_since)

    def test_tool_calls_advance_the_arc(self):
        self.feed(
            event("PostToolUse", tool_name="Read"),
            event("PostToolUse", tool_name="Bash"),
        )
        self.assertEqual(self.session.arc, 2)

    def working(self):
        """A session mid-turn, which is the only state the heat is painted in."""
        self.feed(event("UserPromptSubmit"), event("PreToolUse", tool_name="Edit"))
        return self.session

    def test_a_failed_tool_call_warms_the_working_hue_toward_red(self):
        session = self.working()
        cool = render(session, time.monotonic()).color
        self.feed(event("PostToolUseFailure", tool_name="Edit", tool_use_id="a"))
        warm = render(session, time.monotonic()).color
        self.assertEqual(session.failure_heat, 1.0)
        self.assertGreater(
            render_mod._hue(warm), render_mod._hue(cool), "one failure is invisible"
        )
        self.assertLess(render_mod._hue(warm), config.COLORS["red"])

    def test_failures_saturate_at_red_and_stop_there(self):
        session = self.working()
        for n in range(6):
            self.feed(event("PostToolUseFailure", tool_name="Edit", tool_use_id=str(n)))
        self.assertEqual(session.failure_heat, config.FAILURE_HEAT_FULL)
        self.assertEqual(
            render_mod._hue(render(session, time.monotonic()).color),
            config.COLORS["red"],
        )

    def test_successful_calls_cool_it_back_to_orange(self):
        session = self.working()
        self.feed(event("PostToolUseFailure", tool_name="Edit", tool_use_id="a"))
        # A third of a failure each, so it takes three of them.
        for n in range(3):
            self.assertGreater(session.failure_heat, 0.0)
            self.feed(event("PostToolUse", tool_name="Edit", tool_use_id=f"ok{n}"))
        self.assertEqual(session.failure_heat, 0.0)
        self.assertEqual(render(session, time.monotonic()).color, "orange")

    def test_a_failure_reported_twice_is_one_failure(self):
        # `PostToolUseFailure` and a `PostToolUse` whose response says it errored
        # are the same call arriving down two pipes; the second is not recovery.
        session = self.working()
        self.feed(
            event("PostToolUseFailure", tool_name="Edit", tool_use_id="a"),
            event(
                "PostToolUse",
                tool_name="Edit",
                tool_use_id="a",
                tool_response={"is_error": True},
            ),
        )
        self.assertEqual(session.failure_heat, 1.0)

    def test_an_errored_response_counts_without_the_failure_hook(self):
        # Settings written before `PostToolUseFailure` existed report every
        # failure as an ordinary PostToolUse, silently.
        session = self.working()
        self.feed(
            event(
                "PostToolUse",
                tool_name="Bash",
                tool_use_id="a",
                tool_response={"is_error": True},
            )
        )
        self.assertEqual(session.failure_heat, 1.0)

    def test_heat_is_turn_scoped(self):
        session = self.working()
        self.feed(event("PostToolUseFailure", tool_name="Edit", tool_use_id="a"))
        self.feed(event("Stop"), event("UserPromptSubmit"))
        self.assertEqual(session.failure_heat, 0.0)
        self.feed(event("PostToolUseFailure", tool_name="Edit", tool_use_id="b"))
        self.feed({"hook_event_name": "SessionEnd", "reason": "clear"})
        self.assertEqual(session.failure_heat, 0.0)

    def test_a_failing_session_is_still_only_working(self):
        # It may warm the hue and nothing else: no alert, no debt, no ring pin,
        # no animation. A failing agent has not become a thing that blocks you.
        session = self.working()
        for n in range(6):
            self.feed(event("PostToolUseFailure", tool_name="Edit", tool_use_id=str(n)))
        cell = render(session, time.monotonic())
        self.assertEqual(session.state, "working")
        self.assertFalse(session.alert)
        self.assertIsNone(session.attention_since)
        self.assertEqual(cell.rgb_anim, config.ANIM_NONE)
        self.assertLess(cell.ring, 127)

    def test_only_working_carries_the_heat(self):
        session = self.working()
        for n in range(6):
            self.feed(event("PostToolUseFailure", tool_name="Edit", tool_use_id=str(n)))
        self.feed(event("Stop"))
        self.assertEqual(render(session, time.monotonic()).color, "green")

    def test_subagents_are_counted_by_agent_id(self):
        self.feed(
            event("SubagentStart", agent_id="x"),
            event("SubagentStart", agent_id="y"),
            event("SubagentStop", agent_id="x"),
        )
        self.assertEqual(self.session.subagents, 1)
        self.feed(event("SubagentStop", agent_id="y"))
        self.assertEqual(self.session.subagents, 0)

    def test_a_stop_for_an_unknown_subagent_never_goes_negative(self):
        self.feed(event("SubagentStop", agent_id="never-started"))
        self.assertEqual(self.session.subagents, 0)
        self.feed(event("SubagentStop"))
        self.assertEqual(self.session.subagents, 0)

    def test_a_duplicate_start_is_still_one_subagent(self):
        """The signals are not guaranteed to arrive once each, so identity --
        not a delta -- is what the count is built from."""
        self.feed(*[event("SubagentStart", agent_id="x")] * 3)
        self.assertEqual(self.session.subagents, 1)

    def test_task_tool_calls_count_when_subagentstart_never_arrives(self):
        """The signal that works on a settings file older than SubagentStart."""
        for tool in ("Task", "Agent"):
            with self.subTest(tool=tool):
                self.feed(event("UserPromptSubmit"))
                self.feed(event("PreToolUse", tool_name=tool, tool_use_id="t1"))
                self.assertEqual(self.session.subagents, 1)
                self.feed(event("PostToolUse", tool_name=tool, tool_use_id="t1"))
                self.assertEqual(self.session.subagents, 0)

    def test_an_ordinary_tool_call_is_not_a_subagent(self):
        self.feed(event("PreToolUse", tool_name="Bash", tool_use_id="t1"))
        self.assertEqual(self.session.subagents, 0)

    def test_a_task_call_with_no_id_is_not_counted(self):
        """An id we can't read is never discarded either, so the pile would
        only ever grow -- worse than not counting it."""
        self.feed(event("PreToolUse", tool_name="Task"))
        self.assertEqual(self.session.subagents, 0)

    def test_one_subagent_seen_down_both_paths_is_one_dot(self):
        self.feed(
            event("PreToolUse", tool_name="Task", tool_use_id="t1"),
            event("SubagentStart", agent_id="x"),
        )
        self.assertEqual(self.session.subagents, 1)
        self.feed(
            event("SubagentStop", agent_id="x"),
            event("PostToolUse", tool_name="Task", tool_use_id="t1"),
        )
        self.assertEqual(self.session.subagents, 0)

    def test_the_pile_does_not_flicker_while_the_signals_hand_over(self):
        """SubagentStart lands *after* the PreToolUse that caused it, so a count
        that switched signals partway would visibly drop a dot and pick it up."""
        counts = []
        for e in (
            event("PreToolUse", tool_name="Task", tool_use_id="t1"),
            event("PreToolUse", tool_name="Task", tool_use_id="t2"),
            event("SubagentStart", agent_id="x"),
            event("SubagentStart", agent_id="y"),
        ):
            self.feed(e)
            counts.append(self.session.subagents)
        self.assertEqual(counts, [1, 2, 2, 2])

    def test_a_tool_call_inside_a_subagent_credits_that_subagent(self):
        """Every hook payload carries `agent_id`, not just the subagent events,
        so a tool call made inside a subagent arrives on the parent's session
        with the subagent's id alongside it. That is the whole activity signal.
        """
        self.feed(
            event("SubagentStart", agent_id="x"),
            event("SubagentStart", agent_id="y"),
        )
        before = dict(self.session.subagents_in_flight)
        self.feed(event("PostToolUse", tool_name="Grep", agent_id="y"))
        after = self.session.subagents_in_flight
        self.assertEqual(after["agent:x"], before["agent:x"])
        self.assertGreater(
            after["agent:y"].last_tool_at, before["agent:y"].last_tool_at
        )
        # The credit lands on the activity stamp alone. A subagent that
        # calls a tool has not just been spawned, and a ring that reset on
        # every call would be a stopwatch nobody could read.
        self.assertEqual(
            after["agent:y"].started_at, before["agent:y"].started_at
        )

    def test_the_parents_own_tool_calls_touch_no_subagent(self):
        self.feed(event("SubagentStart", agent_id="x"))
        before = dict(self.session.subagents_in_flight)
        self.feed(event("PostToolUse", tool_name="Read"))
        self.assertEqual(self.session.subagents_in_flight, before)

    def test_an_unknown_agent_id_never_creates_a_dot(self):
        """Invariant 6. A record this path invented has nothing that would ever
        retract it, so it would hold a violet pip for the rest of the turn."""
        self.feed(event("PostToolUse", tool_name="Grep", agent_id="ghost"))
        self.assertEqual(self.session.subagents, 0)
        self.assertEqual(self.session.subagents_in_flight, {})

    def test_dots_owed_to_the_tool_path_alone_have_no_activity(self):
        """`subagent_activity` still has to be one entry per dot: the pile is
        sized by `subagents`, and only the agent path can attribute a call."""
        self.feed(
            event("PreToolUse", tool_name="Task", tool_use_id="t1"),
            event("PreToolUse", tool_name="Task", tool_use_id="t2"),
        )
        self.assertEqual(self.session.subagent_activity, [None, None])
        self.feed(event("SubagentStart", agent_id="x"))
        activity = self.session.subagent_activity
        self.assertEqual(len(activity), 2)
        self.assertIsNotNone(activity[0])
        self.assertIsNone(activity[1])

    def test_a_subagent_is_born_bright(self):
        """One that hasn't called a tool yet is thinking, not stalled."""
        self.feed(event("SubagentStart", agent_id="x"))
        self.assertAlmostEqual(
            board.subagent_brightness(
                self.session.subagent_activity[0], time.monotonic()
            ),
            config.SUBAGENT_KICK_BRIGHTNESS,
            places=3,
        )

    def test_a_subagent_finishing_after_the_turn_does_not_undo_the_green(self):
        """Ten background subagents outlive the turn that spawned them: `Stop`
        lands while they run, and their stops arrive seconds later. Taken as
        work they leave a finished session orange until the next prompt."""
        self.feed(
            event("UserPromptSubmit"),
            event("PreToolUse", tool_name="Task", tool_use_id="t1"),
            event("SubagentStart", agent_id="x"),
            event("Stop"),
        )
        self.assertEqual(self.session.state, "done")
        self.feed(
            event("SubagentStop", agent_id="x"),
            event("PostToolUse", tool_name="Task", tool_use_id="t1"),
        )
        self.assertEqual(self.session.state, "done")
        # ...but the pile still empties, so the violet pips are honest.
        self.assertEqual(self.session.subagents, 0)

    def test_a_straggler_before_the_turn_ends_is_still_work(self):
        self.feed(
            event("UserPromptSubmit"),
            event("PreToolUse", tool_name="Task", tool_use_id="t1"),
            event("MessageDisplay"),
        )
        self.assertEqual(self.session.state, "streaming")
        self.feed(event("SubagentStop", agent_id="x"))
        self.assertEqual(self.session.state, "working")

    def test_a_tool_call_is_proof_of_a_live_turn(self):
        """A session adopted mid-turn never saw its UserPromptSubmit; without
        this it could never look busy at all."""
        self.feed(event("PreToolUse", tool_name="Bash", tool_use_id="t1"))
        self.assertEqual(self.session.state, "working")
        self.feed(event("MessageDisplay"), event("PostToolUse", tool_name="Bash"))
        self.assertEqual(self.session.state, "working")

    def test_a_new_turn_clears_the_pile(self):
        """The only real floor available: a killed subagent never stops."""
        self.feed(event("SubagentStart", agent_id="x"))
        self.feed(event("UserPromptSubmit"))
        self.assertEqual(self.session.subagents, 0)

    def test_tool_counter_resets_each_turn(self):
        self.feed(
            event("UserPromptSubmit"),
            event("PreToolUse", tool_name="Read"),
            event("Stop"),
            event("UserPromptSubmit"),
        )
        self.assertEqual(self.session.tool_calls, 0)

    def test_bypass_permissions_is_remembered(self):
        self.feed(event("PreToolUse", tool_name="Bash", permission_mode="bypassPermissions"))
        self.assertTrue(self.session.unsupervised)

    def test_unknown_event_is_only_a_liveness_ping(self):
        self.feed(event("UserPromptSubmit"), event("SomeFutureHook"))
        self.assertEqual(self.session.state, "thinking")


class Clearing(unittest.TestCase):
    """`/clear` has no hook of its own. It arrives as a *pair* -- SessionEnd
    with reason `clear`, then SessionStart with source `clear` and a brand new
    session id in the same tab -- and the encoder has to sit still through it."""

    TERM = {"TMUX_PANE": "%3"}

    def visualizer(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        return Visualizer(NullTwister())

    def start(self, vis, sid, **kw):
        return vis.handle_event(
            {
                "session_id": sid,
                "hook_event_name": "SessionStart",
                "cwd": "/tmp/p",
                "terminal": self.TERM,
                **kw,
            }
        )

    def end(self, vis, sid, reason):
        return vis.handle_event(
            {"session_id": sid, "hook_event_name": "SessionEnd", "reason": reason}
        )

    def test_clearing_keeps_the_encoder_and_the_session_that_owns_it(self):
        vis = self.visualizer()
        self.start(vis, "old")
        other = vis.table.ensure("other", "/tmp/q", {"TMUX_PANE": "%9"})
        first = vis.table.get("old")
        first.turn_count = 4

        self.end(vis, "old", "clear")
        self.assertIsNone(first.ended_at, "a /clear is not the end of anything")
        self.assertEqual(first.slot, 0, "and it does not move")
        self.assertEqual(other.slot, 1)

        self.start(vis, "new", source="clear")
        session = vis.table.get("new")
        self.assertIs(session, first)
        self.assertEqual(session.slot, 0)
        self.assertEqual(len(vis.table.all()), 2, "one tab, one encoder")
        self.assertEqual(session.turn_count, 4, "the slot's history is the tab's")

    def test_clearing_resets_what_the_agent_forgot(self):
        vis = self.visualizer()
        self.start(vis, "old")
        session = vis.table.get("old")
        session.arc, session.tool_calls, session.context_tokens = 5, 9, 120_000
        session.subagents_in_flight["agent:x"] = Subagent(0.0, 0.0)
        session.alert = True
        session.attention_since = 0.0

        self.end(vis, "old", "clear")
        self.assertEqual((session.arc, session.tool_calls), (0, 0))
        self.assertEqual(session.subagents, 0)
        self.assertEqual(session.context_tokens, 0, "the gauge is not the tab's")
        self.assertFalse(session.alert)
        self.assertIsNone(session.attention_since)
        self.assertEqual(session.state, "idle")

    def test_a_real_ending_still_releases(self):
        for reason in ("logout", "prompt_input_exit", "other", ""):
            with self.subTest(reason=reason):
                vis = self.visualizer()
                self.start(vis, "s")
                self.end(vis, "s", reason)
                session = vis.table.get("s")
                self.assertIsNotNone(session.ended_at)
                self.assertEqual(session.state, "ended")

    def test_the_pair_wipes_once_however_it_is_ordered(self):
        def wipes(vis):
            return [
                o for o in vis._live_overlays(0.0) if isinstance(o, overlays.ClearOverlay)
            ]

        forwards = self.visualizer()
        self.start(forwards, "old")
        self.end(forwards, "old", "clear")
        self.start(forwards, "new", source="clear")
        self.assertEqual(len(wipes(forwards)), 1)

        # SessionStart first, its SessionEnd trailing behind with an id nothing
        # on the board answers to any more. One tab, one encoder, one wipe.
        backwards = self.visualizer()
        self.start(backwards, "old")
        self.start(backwards, "new", source="clear")
        self.end(backwards, "old", "clear")
        self.assertEqual(len(wipes(backwards)), 1)
        self.assertEqual(len(backwards.table.all()), 1)
        self.assertEqual(backwards.table.get("new").slot, 0)

    def test_a_stale_session_end_never_lights_an_encoder(self):
        # It carries no terminal, so `ensure` could only answer it by allocating
        # a fresh slot -- a phantom session for a tab that has already moved on.
        vis = self.visualizer()
        self.end(vis, "never-seen", "other")
        self.assertEqual(vis.table.all(), [])

    def test_clearing_is_a_wipe_rather_than_an_arrival(self):
        vis = self.visualizer()
        self.start(vis, "old")
        vis._overlays.clear()  # the arrival's own flash, which is not in question
        self.start(vis, "new", source="clear")
        live = vis._live_overlays(0.0)
        self.assertTrue(any(isinstance(o, overlays.ClearOverlay) for o in live))
        self.assertFalse(
            any(isinstance(o, overlays.SpawnOverlay) for o in live),
            "nothing claimed an encoder here; an agent on one forgot everything",
        )

    def test_a_resume_is_still_an_arrival(self):
        vis = self.visualizer()
        self.start(vis, "s", source="resume")
        self.assertTrue(
            any(isinstance(o, overlays.SpawnOverlay) for o in vis._live_overlays(0.0))
        )


class Rendering(unittest.TestCase):
    def setUp(self):
        self.session = SessionTable().ensure("s", "/tmp/p")

    def test_every_state_renders_in_range(self):
        for state in config.STATE_COLORS:
            self.session.state = state
            cell = render(self.session, 12.5)
            self.assertTrue(0 <= cell.ring <= 127, state)
            self.assertTrue(0.0 <= cell.brightness <= 1.0, state)

    def test_every_state_has_a_rank(self):
        """One vocabulary, not three. A state with a color but no rank sorts
        silently last and quietly loses every motion arbitration it enters."""
        self.assertEqual(set(config.STATE_COLORS), set(config.STATE_PRIORITY))
        for state in config.STATE_ANIM:
            self.assertIn(state, config.STATE_PRIORITY)

    def test_a_cell_cannot_be_built_out_of_range(self):
        """The clamp lives on the Cell rather than on each of the dozen eased
        ramps that build one, so an overlay asked for a frame outside its own
        window is a cell at the end of its ramp and never a ring of -158."""
        self.assertEqual(Cell(ring=200, brightness=4.0).ring, 127)
        self.assertEqual(Cell(ring=200, brightness=4.0).brightness, 1.0)
        self.assertEqual(Cell(ring=-158, brightness=-0.3).ring, 0)
        self.assertEqual(Cell(ring=-158, brightness=-0.3).brightness, 0.0)
        # ...and leaves a legal one exactly as it was asked for.
        legal = Cell("red", config.ANIM_NONE, 64, 0.5)
        self.assertEqual((legal.ring, legal.brightness), (64, 0.5))

    def test_ended_encoder_goes_dark(self):
        self.session.state = "ended"
        cell = render(self.session, 0.0)
        self.assertIsNone(cell.color)
        self.assertEqual(cell.ring, 0)

    def test_the_arc_advances_with_tool_calls_not_with_time(self):
        with mock.patch.object(config, "TURN_RING", False):
            self.session.state = "working"
            self.session.last_tool_at = 0.0
            still = {render(self.session, t / 10).ring for t in range(20)}
            self.assertEqual(len(still), 1, "a session doing nothing should not spin")
            self.session.arc += 1
            self.assertNotIn(render(self.session, 0.0).ring, still)

    def test_a_stalled_session_dims_out(self):
        self.session.state = "working"
        self.session.last_tool_at = 0.0
        busy = render(self.session, 0.0).brightness
        stalled = render(
            self.session, config.STALL_SECONDS + config.STALL_FADE_SECONDS
        ).brightness
        self.assertGreater(busy, stalled)
        self.assertAlmostEqual(stalled, config.IDLE_BRIGHTNESS, places=5)

    def test_done_fades_then_ramps_back_if_ignored(self):
        self.session.state = "done"
        self.session.state_since = 0.0
        self.session.attention_since = 0.0
        fresh = render(self.session, 1.0).brightness
        faded = render(self.session, config.DONE_FADE_SECONDS + 1).brightness
        nagging = render(self.session, config.ATTENTION_RAMP_SECONDS).brightness
        self.assertGreater(fresh, faded)
        self.assertGreater(nagging, faded)
        self.assertLessEqual(nagging, config.DONE_DEBT_CEILING)

    def test_attention_debt_is_forgiven_when_you_look(self):
        self.session.attention_since = 0.0
        self.assertEqual(attention_debt(self.session, config.ATTENTION_RAMP_SECONDS), 1.0)
        self.session.attended()
        self.assertEqual(attention_debt(self.session, 1_000), 0.0)

    def test_a_working_ring_is_a_stopwatch(self):
        """The one thing about a running agent nothing else on the board says.
        Color is what it is doing and brightness is how recently it called a
        tool; neither of them is "this one has been going for twenty minutes"."""
        self.session.state = "working"
        self.session.turn_started_at = 0.0
        rings = [render(self.session, t).ring for t in (0.0, 30.0, 120.0, 600.0, 1800.0)]
        self.assertEqual(rings, sorted(rings), "a longer turn is a fuller ring")
        self.assertEqual(len(set(rings)), len(rings), "and a distinguishable one")
        self.assertGreaterEqual(rings[0], config.CONTEXT_RING_FLOOR)

    def test_a_long_turn_saturates_rather_than_wrapping(self):
        """A wrapped ring would be indistinguishable from a turn that just
        started, which is the one reading that must never be wrong."""
        self.session.state = "working"
        self.session.turn_started_at = 0.0
        full = config.TURN_RING_FULL_SECONDS
        self.assertEqual(render(self.session, full).ring, 127)
        self.assertEqual(render(self.session, full * 10).ring, 127)

    def test_the_stopwatch_reads_five_fifteen_thirty_sixty(self):
        """The scale the whole board is read by, and the reason it isn't linear:
        a flat hour would put every ordinary turn in the bottom segment, so the
        first quarter of the ring buys the first five minutes and the last
        quarter still has half an hour left in it.

        The tolerance is three values of the 127 because the floor is an offset
        rather than a clamp -- see :func:`mft.render.stopwatch_ring`, which buys
        a live first twenty seconds with it. Three values is a quarter of one
        LED's blend; you cannot see it on the hardware and you can see the stub.
        """
        self.session.state = "working"
        self.session.turn_started_at = 0.0
        for minutes, want in ((5, 0.25), (15, 0.5), (30, 0.75), (60, 1.0)):
            with self.subTest(minutes=minutes):
                ring = render(self.session, minutes * 60.0).ring
                self.assertAlmostEqual(ring / 127, want, delta=3 / 127)

    def test_a_turn_that_just_started_is_visibly_climbing(self):
        """Most turns are short, and the first leg of the curve is the steepest
        so that they aren't all indistinguishable from a turn that hasn't begun.
        Half a minute has to be clear of the floor, not sitting on it."""
        self.session.state = "working"
        self.session.turn_started_at = 0.0
        floor = config.CONTEXT_RING_FLOOR
        self.assertGreater(render(self.session, 30.0).ring, floor)
        self.assertAlmostEqual(
            render(self.session, 60.0).ring / 127, 0.125, delta=4 / 127
        )
        # And the first minute climbs faster than any minute after it.
        first = render(self.session, 60.0).ring - render(self.session, 0.0).ring
        later = render(self.session, 300.0).ring - render(self.session, 240.0).ring
        self.assertGreater(first, later)

    def test_the_stopwatch_leaves_its_floor_on_the_first_frame(self):
        """The floor is an offset, not a clamp. Clamped, the curve took nineteen
        seconds to catch up with the stub and every turn shorter than that was
        frozen at the one value that means "this hasn't started" -- which is a
        large share of every turn this board ever shows.

        Tween, in other words: the ring has to be climbing while you are looking
        at it, and on this hardware that means spending values, not segments."""
        self.session.state = "working"
        self.session.turn_started_at = 0.0
        floor = config.CONTEXT_RING_FLOOR
        self.assertEqual(render(self.session, 0.0).ring, floor)
        self.assertGreater(render(self.session, 5.0).ring, floor)
        # Every couple of seconds through the first minute, so it reads as motion
        # rather than as a ring that occasionally jumps.
        steps = {render(self.session, float(t)).ring for t in range(0, 61)}
        self.assertGreaterEqual(len(steps), 12)

    def test_a_turn_adopted_mid_flight_still_runs_its_stopwatch(self):
        """`mft.discover` hands over sessions that predate the daemon, and they
        arrive with no prompt of their own to have started at."""
        self.session.state = "working"
        self.session.turn_started_at = None
        self.session.state_since = 0.0
        self.assertGreater(render(self.session, 600.0).ring, render(self.session, 1.0).ring)

    def test_the_gauge_is_not_on_a_working_ring(self):
        """It moves by nothing you can act on during a turn, and the ring has
        something better to say. It comes back the moment the turn ends."""
        self.session.state = "working"
        self.session.turn_started_at = 0.0
        self.session.context_limit = 200_000
        self.session.context_tokens = 190_000
        busy = render(self.session, 5.0).ring
        self.session.state = "done"
        self.session.state_since = 5.0
        self.assertGreater(render(self.session, 5.0).ring, busy)

    def test_a_full_context_does_not_overflow_the_ring(self):
        self.session.state = "idle"
        self.session.context_limit = 200_000
        self.session.context_tokens = 999_000
        self.assertEqual(render(self.session, 0.0).ring, 127)

    def test_working_falls_back_to_the_arc_when_the_stopwatch_is_off(self):
        with mock.patch.object(config, "TURN_RING", False):
            self.session.state = "working"
            self.session.arc = 4
            self.assertEqual(
                render(self.session, 0.0).ring, int(127 * 4 / config.ARC_SEGMENTS)
            )

    def test_an_idle_session_still_shows_its_gauge(self):
        # How full the window is outlives the turn that filled it. A session
        # resting at 95% is the one to deal with before it compacts, and it used
        # to look exactly like a fresh one.
        self.session.state = "idle"
        self.session.context_limit = 200_000
        self.session.context_tokens = 190_000
        full = render(self.session, 0.0)
        self.session.context_tokens = 10_000
        empty = render(self.session, 0.0)
        self.assertGreater(full.ring, empty.ring)
        # Still resting, though: the gauge is on the ring, and nothing about it
        # makes the encoder any brighter.
        self.assertEqual(full.brightness, config.IDLE_BRIGHTNESS)
        self.assertEqual(full.brightness, empty.brightness)

    def test_an_idle_session_with_no_reading_is_still_a_pip(self):
        # Deliberately not the arc: a spinner on a session that stopped working
        # is frozen at whatever segment the last call left it on, which says
        # nothing at all.
        self.session.state = "idle"
        self.session.arc = 4
        self.assertIsNone(self.session.context_fraction)
        self.assertEqual(render(self.session, 0.0).ring, render_mod.PIP)

    def test_a_settled_done_session_keeps_its_gauge(self):
        self.session.state = "done"
        self.session.state_since = 0.0
        self.session.context_limit = 200_000
        self.session.context_tokens = 190_000
        settled = render(self.session, config.DONE_FADE_SECONDS + 1).ring
        self.assertGreater(settled, render_mod.PIP)
        # The unwinding ring starts above the gauge and decays through it, so
        # there is no visible handover -- just a fade that stops mattering.
        self.assertEqual(render(self.session, 0.0).ring, 127)

    def test_a_resting_gauge_dims_as_it_ages(self):
        """The reading stays on the ring; it stops competing for your eye with
        the sessions that finished a minute ago."""
        self.session.state = "done"
        self.session.state_since = 0.0
        self.session.context_limit = 200_000
        self.session.context_tokens = 190_000
        fresh = render(self.session, 1.0)
        stale = render(self.session, config.DONE_FADE_SECONDS + 1)
        self.assertGreater(fresh.ring_light, stale.ring_light)
        self.assertAlmostEqual(stale.ring_light, config.GAUGE_STALE_LEVEL, places=5)
        # Dimmer, never dark: an unlit gauge and a session with no reading at all
        # would be the same encoder, and they mean very different things.
        self.assertGreater(stale.ring_light, 0.0)
        # The reading itself does not move -- only what it is lit at. (Early on,
        # `ring` is still the `done` flash unwinding down through the gauge, so
        # this is asked either side of where that lands.)
        later = render(self.session, config.DONE_FADE_SECONDS * 4)
        self.assertEqual(stale.ring, later.ring)
        self.assertGreaterEqual(stale.ring_light, later.ring_light)

    def test_a_neglected_done_session_nags_without_its_gauge_coming_back(self):
        """Ring brightness is channel 6 and the RGB's is channel 3, which is what
        makes this possible. The reading did not get more urgent, only older."""
        self.session.state = "done"
        self.session.state_since = 0.0
        self.session.attention_since = 0.0
        self.session.context_limit = 200_000
        self.session.context_tokens = 190_000
        nagging = render(self.session, config.ATTENTION_RAMP_SECONDS)
        quiet = render(self.session, 1.0)
        self.assertGreater(nagging.brightness, config.IDLE_BRIGHTNESS)
        self.assertLess(nagging.ring_light, quiet.ring_light)

    def test_an_idle_gauge_fades_on_the_same_clock(self):
        self.session.state = "idle"
        self.session.state_since = 0.0
        self.session.context_limit = 200_000
        self.session.context_tokens = 190_000
        fresh = render(self.session, 1.0)
        stale = render(self.session, config.DONE_FADE_SECONDS + 1)
        self.assertAlmostEqual(fresh.ring_light, config.IDLE_BRIGHTNESS, places=2)
        self.assertLess(stale.ring_light, fresh.ring_light)

    def test_a_cell_without_its_own_ring_level_is_lit_as_one_thing(self):
        """The default, and what every gesture, overlay and blocking state
        wants: one encoder, one brightness."""
        self.assertEqual(Cell(brightness=0.6).ring_light, 0.6)
        self.assertEqual(Cell(brightness=0.6, ring_level=0.1).ring_light, 0.1)
        self.assertEqual(Cell(brightness=0.6, ring_level=4.0).ring_light, 1.0)
        self.assertEqual(Cell(brightness=0.6, ring_level=-2.0).ring_light, 0.0)

    def test_a_blocking_ring_is_lit_with_its_encoder(self):
        """The ring means "you" in these states, not "tokens", so it has no
        business dimming on a clock of its own."""
        for state in ("permission", "plan", "waiting", "error"):
            self.session.state = state
            cell = render(self.session, 12.5)
            self.assertIsNone(cell.ring_level, state)

    def test_neglect_speeds_the_gate_up_rather_than_brightening_it(self):
        # The brightness below is discarded by the wire on any animated cell
        # (see Twister.write), so rate is the only channel debt has here.
        for state in ("permission", "plan", "waiting"):
            with self.subTest(state=state):
                self.session.state = state
                self.session.attention_since = 0.0
                fresh = render(self.session, 0.0).rgb_anim
                neglected = render(self.session, config.ATTENTION_RAMP_SECONDS).rgb_anim
                self.assertEqual(fresh, config.STATE_ANIM[state])
                self.assertGreater(neglected, fresh)

    def test_a_stale_plan_stays_a_gate_and_stays_ordered(self):
        # It does *not* stay below a fresh permission gate's rate -- the band has
        # no room between the two base rates. What it must not do is leave the
        # band (a plan that breathes is a different message) or overtake a
        # permission gate carrying the same debt. Board.test_a_plan_never
        # _out_strobes_a_gate covers the case where both are on screen at once.
        self.session.state = "plan"
        self.session.attention_since = 0.0
        stale = render(self.session, config.ATTENTION_RAMP_SECONDS * 10).rgb_anim
        self.assertIn(stale, config.ANIM_GATE.values())

        gate = SessionTable().ensure("g", "/tmp/p")
        gate.state = "permission"
        gate.attention_since = 0.0
        equal_debt = render(gate, config.ATTENTION_RAMP_SECONDS * 10).rgb_anim
        self.assertGreater(equal_debt, stale)

    def test_a_breathe_never_escalates_into_a_strobe(self):
        self.session.state = "waiting"
        self.session.attention_since = 0.0
        neglected = render(self.session, config.ATTENTION_RAMP_SECONDS * 10).rgb_anim
        self.assertIn(neglected, config.ANIM_PULSE.values())

    def test_an_attended_session_is_back_to_its_base_rate(self):
        self.session.state = "permission"
        self.session.attention_since = 0.0
        self.session.attended()
        self.assertEqual(
            render(self.session, config.ATTENTION_RAMP_SECONDS).rgb_anim,
            config.STATE_ANIM["permission"],
        )

    def test_unsupervised_sessions_get_their_own_color(self):
        self.session.permission_mode = "bypassPermissions"
        for state in ("working", "idle", "permission"):
            self.session.state = state
            self.assertEqual(render(self.session, 0.0).color, config.UNSUPERVISED_COLOR)


class ContextReading(unittest.TestCase):
    """No hook carries token counts, so they come out of the transcript."""

    def write(self, *entries: dict) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        with handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
        self.addCleanup(Path(handle.name).unlink, True)
        return handle.name

    @staticmethod
    def assistant(read: int, model: str = "claude-opus-5", **kw) -> dict:
        return {
            "type": "assistant",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": 5,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": read,
                    "output_tokens": 200,
                },
            },
            **kw,
        }

    def test_the_last_assistant_message_is_the_context_size(self):
        path = self.write(self.assistant(10_000), self.assistant(90_000))
        tokens, model = context.read_usage(path)
        self.assertEqual(tokens, 90_305)
        self.assertEqual(model, "claude-opus-5")

    def test_subagent_messages_do_not_count(self):
        # Sidechains live in the parent's transcript with their own much smaller
        # context; reading one would make a nearly-full agent look empty.
        path = self.write(
            self.assistant(90_000), self.assistant(500, isSidechain=True)
        )
        self.assertEqual(context.read_usage(path)[0], 90_305)

    def test_missing_or_empty_transcript_is_no_reading_not_zero(self):
        self.assertIsNone(context.read_usage(""))
        self.assertIsNone(context.read_usage("/nonexistent/transcript.jsonl"))
        self.assertIsNone(context.read_usage(self.write()))
        self.assertIsNone(context.read_usage(self.write({"type": "user"})))

    def test_a_truncated_tail_does_not_crash_the_parser(self):
        path = self.write(self.assistant(1_000))
        Path(path).write_text('{"type": "assist' + "\n" + json.dumps(self.assistant(2_000)))
        self.assertEqual(context.read_usage(path)[0], 2_305)

    def test_a_million_token_model_gets_a_million_token_gauge(self):
        self.assertEqual(context.limit_for_model("claude-opus-5[1m]"), 1_000_000)
        self.assertEqual(context.limit_for_model("claude-sonnet-5"), 200_000)
        self.assertEqual(
            context.limit_for_model("something-new"), config.CONTEXT_LIMIT_DEFAULT
        )

    def settings(self, model: str, name: str = "settings.json") -> str:
        """A project directory whose `.claude/<name>` selects `model`."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        claude = root / ".claude"
        claude.mkdir()
        (claude / name).write_text(json.dumps({"model": model}))
        context._settings_cache.clear()
        self.addCleanup(context._settings_cache.clear)
        return str(root)

    def test_the_window_marker_comes_from_settings_when_the_transcript_lacks_it(self):
        # The whole reason this exists: Claude Code writes `claude-opus-5` into
        # the transcript whether or not you are on the 1M variant, and `opus[1m]`
        # is only ever written down in settings.json. Without this a session at
        # 11% full renders at 55%.
        cwd = self.settings("opus[1m]")
        self.assertEqual(context.limit_for_model("claude-opus-5", cwd), 1_000_000)

    def test_a_settings_model_of_another_family_is_not_believed(self):
        # `/model` mid-session: the transcript watched it happen and settings.json
        # did not, so the family always comes off the message in front of us.
        cwd = self.settings("sonnet[1m]")
        self.assertEqual(context.limit_for_model("claude-opus-5", cwd), 200_000)

    def test_settings_never_shrink_a_window_the_transcript_spelled_out(self):
        cwd = self.settings("opus")
        self.assertEqual(context.limit_for_model("claude-opus-5[1m]", cwd), 1_000_000)

    def test_a_project_settings_file_outranks_the_user_one(self):
        cwd = self.settings("opus[1m]", "settings.local.json")
        with mock.patch.object(config, "CONTEXT_SETTINGS_USER", "/nonexistent.json"):
            self.assertEqual(context.limit_for_model("claude-opus-5", cwd), 1_000_000)

    def test_settings_are_found_from_a_subdirectory(self):
        root = Path(self.settings("opus[1m]"))
        deep = root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        self.assertEqual(context.limit_for_model("claude-opus-5", str(deep)), 1_000_000)

    def test_unreadable_settings_are_not_an_error(self):
        root = Path(self.settings("opus[1m]"))
        (root / ".claude" / "settings.json").write_text("{ not json at all")
        with mock.patch.object(config, "CONTEXT_SETTINGS_USER", "/nonexistent.json"):
            self.assertEqual(context.limit_for_model("claude-opus-5", str(root)), 200_000)
            self.assertEqual(context.configured_model(str(root)), "")

    def test_a_rewritten_settings_file_is_read_again(self):
        root = Path(self.settings("opus[1m]"))
        self.assertEqual(context.limit_for_model("claude-opus-5", str(root)), 1_000_000)
        target = root / ".claude" / "settings.json"
        target.write_text(json.dumps({"model": "opus"}))
        os.utime(target, (0, 0))  # `/model` writes this file; the mtime is the cue
        with mock.patch.object(config, "CONTEXT_SETTINGS_USER", "/nonexistent.json"):
            self.assertEqual(context.limit_for_model("claude-opus-5", str(root)), 200_000)

    def test_the_daemon_records_what_it_reads(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        vis = Visualizer(NullTwister())
        path = self.write(self.assistant(120_000, model="claude-opus-5[1m]"))
        vis.handle_event(
            {
                "session_id": "s",
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "transcript_path": path,
            }
        )
        session = vis.table.get("s")
        self.assertEqual(session.context_tokens, 120_305)
        self.assertEqual(session.context_limit, 1_000_000)
        self.assertAlmostEqual(session.context_fraction, 0.120305, places=5)


class Board(unittest.TestCase):
    def setUp(self):
        self.table = SessionTable()

    def blocked(self, sid: str, at: float = 0.0):
        session = self.table.ensure(sid, "/tmp/p", {"tty": f"/dev/{sid}"})
        session.state = "permission"
        session.attention_since = at
        return session

    def test_only_one_encoder_moves_fast(self):
        for i in range(3):
            self.blocked(f"s{i}", at=float(i))
        cells = board.compose(self.table.all(), 10.0)
        fast = [c for c in cells if c.rgb_anim == config.STATE_ANIM["permission"]]
        self.assertEqual(len(fast), 1, "motion is a budget, not a decoration")
        slow = [c for c in cells if c.rgb_anim == config.SLOW_ANIM]
        self.assertEqual(len(slow), 2)

    def test_a_plan_never_out_strobes_a_gate_on_the_same_board(self):
        # A fully-neglected plan does climb past a fresh gate's rate -- the gate
        # band has no room between the two base rates. This is what makes that
        # safe: ranked by state before debt, the plan loses the board's one fast
        # animation outright, however long it has been sitting there.
        stale = self.table.ensure("plan", "/tmp/p", {"tty": "/dev/plan"})
        stale.state = "plan"
        stale.attention_since = 0.0
        fresh = self.blocked("gate", at=1_000.0)

        cells = board.compose(self.table.all(), 1_000.0)
        self.assertEqual(cells[stale.slot].rgb_anim, config.SLOW_ANIM)
        self.assertGreaterEqual(
            cells[fresh.slot].rgb_anim, config.STATE_ANIM["permission"]
        )

    def test_the_oldest_neediest_session_wins_the_motion(self):
        first = self.blocked("a", at=0.0)
        self.blocked("b", at=5.0)
        cells = board.compose(self.table.all(), 10.0)
        self.assertEqual(cells[first.slot].rgb_anim, config.STATE_ANIM["permission"])

    def test_unclaimed_encoders_are_dark(self):
        session = self.table.ensure("a", "/tmp/p")
        cells = board.compose(self.table.all(), 1.0)
        self.assertEqual(len(cells), config.SLOT_COUNT)
        for slot, cell in enumerate(cells):
            if slot != session.slot:
                self.assertIsNone(cell.color)

    def subagent_slots(self, cells) -> list[int]:
        """Occupied subagent slots, ascending."""
        return [
            slot
            for slot, cell in enumerate(cells)
            if cell.color == config.SUBAGENT_COLOR
        ]

    def spawn(self, count: int) -> list[int]:
        parent = self.table.ensure("a", "/tmp/p")  # slot 0, the other end
        parent.state = "working"
        set_subagents(parent, count)
        return self.subagent_slots(board.compose(self.table.all(), 1.0))

    def test_subagents_stack_from_the_bottom_right_backwards(self):
        last = config.ENCODERS_PER_BANK - 1
        # The first one lands in the corner and the pile grows back from there,
        # toward the sessions rather than away from them.
        self.assertEqual(self.spawn(1), [last])
        self.assertEqual(self.spawn(3), [last - 2, last - 1, last])

    def test_subagents_never_take_a_claimed_encoder(self):
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        set_subagents(parent, 2)
        # Fill the corner the stack would otherwise want first.
        last = config.ENCODERS_PER_BANK - 1
        squatter = self.table.ensure("b", "/tmp/q", {"tty": "/dev/b"})
        squatter.slot = last
        squatter.state = "working"
        cells = board.compose(self.table.all(), 1.0)
        self.assertEqual(self.subagent_slots(cells), [last - 2, last - 1])

    def test_two_parents_share_one_unbroken_pile(self):
        """The mirror of the session rule: no dark encoder inside the stack."""
        last = config.ENCODERS_PER_BANK - 1
        for name, count in (("a", 1), ("b", 2)):
            parent = self.table.ensure(name, f"/tmp/{name}", {"tty": f"/dev/{name}"})
            parent.state = "working"
            set_subagents(parent, count)
        cells = board.compose(self.table.all(), 1.0)
        self.assertEqual(self.subagent_slots(cells), [last - 2, last - 1, last])

    def test_subagents_do_not_look_like_sessions(self):
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        set_subagents(parent, 1)
        cell = board.compose(self.table.all(), 1.0)[config.ENCODERS_PER_BANK - 1]
        self.assertNotIn(config.SUBAGENT_COLOR, config.STATE_COLORS.values())
        self.assertLess(cell.brightness, config.ACTIVE_BRIGHTNESS)
        # Slower than the slowest a session is ever allowed to move, so a
        # subagent can never compete with one for the eye.
        self.assertLess(cell.rgb_anim, config.SLOW_ANIM)

    def test_every_dot_in_the_pile_knows_which_parent_it_belongs_to(self):
        """The daemon resolves a press through this, so it has to agree with
        the paint exactly -- a dot owned by nobody is a knob that does nothing
        while looking identical to one that works."""
        last = config.ENCODERS_PER_BANK - 1
        parents = {}
        for name, count in (("a", 1), ("b", 2)):
            parent = self.table.ensure(name, f"/tmp/{name}", {"tty": f"/dev/{name}"})
            parent.state = "working"
            set_subagents(parent, count)
            parents[name] = parent
        sessions = self.table.all()
        claimed = {s.slot for s in sessions}
        owners = board.subagent_owners(sessions, claimed)
        # Served in encoder order from the corner backwards: a's one dot takes
        # the corner, b's two grow back from it.
        self.assertEqual(
            owners,
            {
                last: parents["a"],
                last - 1: parents["b"],
                last - 2: parents["b"],
            },
        )
        self.assertEqual(
            sorted(owners), self.subagent_slots(board.compose(sessions, 1.0))
        )

    def test_nobody_owns_a_slot_that_a_session_owns(self):
        last = config.ENCODERS_PER_BANK - 1
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        set_subagents(parent, 2)
        squatter = self.table.ensure("b", "/tmp/q", {"tty": "/dev/b"})
        squatter.slot = last
        squatter.state = "working"
        sessions = self.table.all()
        owners = board.subagent_owners(sessions, {s.slot for s in sessions})
        self.assertEqual(sorted(owners), [last - 2, last - 1])

    def test_no_subagents_in_flight_is_an_empty_map(self):
        session = self.table.ensure("a", "/tmp/p")
        self.assertEqual(board.subagent_owners([session], {session.slot}), {})

    def test_painting_the_pile_claims_it(self):
        """`compose` freezes `claimed` for the overlays and checks it before
        breathing, so a painted slot that stayed unclaimed would be paint an
        overlay felt free to write over."""
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        set_subagents(parent, 2)
        cells = board.blank_board()
        claimed = {parent.slot}
        board.stack_subagents(cells, [parent], claimed, 0.0)
        painted = set(self.subagent_slots(cells))
        self.assertTrue(painted)
        self.assertTrue(painted <= claimed)

    def test_a_dot_brightens_on_its_own_tool_call_and_sinks_between_them(self):
        now = 1000.0
        kicked = board.subagent_brightness(now, now)
        mid = board.subagent_brightness(now - config.SUBAGENT_KICK_SECONDS / 2, now)
        cold = board.subagent_brightness(now - config.SUBAGENT_KICK_SECONDS * 10, now)
        self.assertAlmostEqual(kicked, config.SUBAGENT_KICK_BRIGHTNESS)
        self.assertAlmostEqual(cold, config.SUBAGENT_IDLE_BRIGHTNESS)
        self.assertGreater(kicked, mid)
        self.assertGreater(mid, cold)

    def test_the_floor_stays_visible(self):
        """A subagent you can't see is worse than one that isn't busy, and a
        dark violet dot is indistinguishable from an encoder nobody owns."""
        self.assertGreater(config.SUBAGENT_IDLE_BRIGHTNESS, 0.0)

    def test_a_dot_with_no_signal_holds_the_level_the_pile_always_had(self):
        """An install too old to send SubagentStart loses nothing."""
        self.assertEqual(
            board.subagent_brightness(None, 1000.0), config.SUBAGENT_BRIGHTNESS
        )

    def test_each_dot_shimmers_on_its_own(self):
        """The point of keying the pile: one busy subagent must not light up
        its idle siblings, or the shimmer says nothing the count didn't."""
        now = 1000.0
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        parent.subagents_in_flight = {
            "agent:x": Subagent(now - 600.0, now - 600.0),
            "agent:y": Subagent(now, now),
        }
        cells = board.blank_board()
        board.stack_subagents(cells, [parent], {parent.slot}, now)
        levels = sorted(c.brightness for c in cells if c.color == config.SUBAGENT_COLOR)
        self.assertEqual(len(levels), 2)
        self.assertAlmostEqual(levels[0], config.SUBAGENT_IDLE_BRIGHTNESS)
        self.assertAlmostEqual(levels[1], config.SUBAGENT_KICK_BRIGHTNESS)

    def test_the_shimmer_can_be_turned_off(self):
        with mock.patch.object(config, "SUBAGENT_SHIMMER", False):
            self.assertEqual(
                board.subagent_brightness(1000.0, 1000.0), config.SUBAGENT_BRIGHTNESS
            )

    def test_the_ring_is_a_stopwatch_on_how_long_the_subagent_has_been_out(self):
        """The scale the whole board is read by: a quarter ring is five minutes
        out, half is fifteen, three quarters is thirty, all the way round is an
        hour. The same curve a session's turn ring wears, which is the point --
        a dot at half and an encoder at half are the same fifteen minutes."""
        for minutes, want in ((5, 0.25), (15, 0.5), (30, 0.75), (60, 1.0)):
            with self.subTest(minutes=minutes):
                ring = board.subagent_ring(1000.0 - minutes * 60, 1000.0)
                self.assertAlmostEqual(ring / 127, want, delta=3 / 127)

    def test_a_dot_and_an_encoder_at_the_same_age_read_the_same(self):
        """One scale, or it is a scale you have to look twice to read. The dots
        sit on the same board as the sessions they were spawned from."""
        self.assertEqual(config.SUBAGENT_RING_SECONDS, config.TURN_RING_FULL_SECONDS)
        for minutes in (1, 5, 15, 30, 90):
            with self.subTest(minutes=minutes):
                session = SessionTable().ensure("s", {})
                session.state = "working"
                session.turn_started_at = 1000.0 - minutes * 60
                self.assertEqual(
                    board.subagent_ring(1000.0 - minutes * 60, 1000.0),
                    render_mod._turn_ring(session, 1000.0),
                )

    def test_a_just_spawned_dot_still_shows_a_ring(self):
        """An empty ring reads as an unclaimed encoder, which is the one thing
        a lit dot must never look like."""
        self.assertEqual(board.subagent_ring(1000.0, 1000.0), config.SUBAGENT_RING_FLOOR)

    def test_the_stopwatch_saturates_rather_than_wrapping(self):
        """A wrap is ambiguous with a fresh spawn, and there is nothing you do
        at ninety minutes that you didn't already do at forty."""
        for elapsed in (config.SUBAGENT_RING_SECONDS, 100_000.0):
            self.assertEqual(board.subagent_ring(1000.0 - elapsed, 1000.0), 127)

    def test_a_dot_with_no_spawn_time_wears_the_old_stub(self):
        """Nothing about the pile gets worse on an install that can't feed the
        stopwatch: it goes back to the flat stub every dot used to wear."""
        self.assertEqual(board.subagent_ring(None, 1000.0), config.SUBAGENT_RING)
        with mock.patch.object(config, "SUBAGENT_TIME_RING", False):
            self.assertEqual(board.subagent_ring(0.0, 1000.0), config.SUBAGENT_RING)

    def test_each_dot_keeps_its_own_stopwatch(self):
        """The mirror of the shimmer test, and the reading that matters most:
        one dot most of the way round beside fresh ones is the fan-out that
        went out and never came back."""
        now = 1000.0
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        parent.subagents_in_flight = {
            "agent:old": Subagent(now - config.SUBAGENT_RING_SECONDS, now),
            "agent:new": Subagent(now, now),
        }
        cells = board.blank_board()
        board.stack_subagents(cells, [parent], {parent.slot}, now)
        rings = sorted(c.ring for c in cells if c.color == config.SUBAGENT_COLOR)
        self.assertEqual(rings, [config.SUBAGENT_RING_FLOOR, 127])

    def test_a_tool_call_does_not_rewind_the_stopwatch(self):
        """The two stamps move independently: a subagent that calls a tool has
        not just been spawned. Pinned on the board rather than the record
        because this is the whole reason the record has two fields."""
        now = 1000.0
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        parent.subagents_in_flight = {"agent:x": Subagent(now - 15 * 60, now)}
        cells = board.blank_board()
        board.stack_subagents(cells, [parent], {parent.slot}, now)
        dot = next(c for c in cells if c.color == config.SUBAGENT_COLOR)
        self.assertAlmostEqual(dot.ring / 127, 0.5, delta=0.02)
        self.assertAlmostEqual(dot.brightness, config.SUBAGENT_KICK_BRIGHTNESS)

    def test_a_pile_from_the_tool_use_path_alone_still_fills_its_rings(self):
        """Only the `agent_id` signal can attribute a tool call, so those dots
        have no shimmer -- but a PreToolUse for a Task *is* the spawn, so an
        install too old for SubagentStart still gets every stopwatch."""
        now = 1000.0
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        parent.subagents_in_flight = {
            "tool:1": Subagent(now - config.SUBAGENT_RING_SECONDS, now)
        }
        cells = board.blank_board()
        board.stack_subagents(cells, [parent], {parent.slot}, now)
        dot = next(c for c in cells if c.color == config.SUBAGENT_COLOR)
        self.assertEqual(dot.ring, 127)
        self.assertAlmostEqual(dot.brightness, config.SUBAGENT_BRIGHTNESS)

    def test_the_pile_never_animates(self):
        """Invariant 4: the shimmer is a level, never a rate. Nothing in the
        corner may compete with the encoder a human is blocking on."""
        now = 1000.0
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        parent.subagents_in_flight = {
            "agent:x": Subagent(now, now),
            "agent:y": Subagent(now - 5.0, now - 5.0),
        }
        cells = board.compose([parent], now)
        pile = [c for c in cells if c.color == config.SUBAGENT_COLOR]
        self.assertEqual(len(pile), 2)
        self.assertTrue(all(c.rgb_anim == config.ANIM_NONE for c in pile))

    def test_an_empty_board_breathes_rather_than_going_dark(self):
        lit = [c for c in board.compose([], 1.0) if c.brightness > 0]
        self.assertTrue(lit)


class Sleeping(unittest.TestCase):
    """Nobody has been here for half an hour: the board dims, then goes out.

    Every other fade on this board is a session getting older. This one is the
    room being empty, so what it has to get right is coming *back*: a board that
    cannot be woken is a board that has broken, and one that wakes with a step
    is one that just said something it didn't mean.
    """

    def dark_at(self) -> float:
        """Far enough into the second stage to be genuinely dark, whatever the
        configured timings happen to be."""
        return config.SLEEP_DARK_SECONDS + config.SLEEP_FADE_SECONDS

    def setUp(self):
        self.sleep = board.Sleep(0.0)
        self.table = SessionTable()

    def test_a_board_nobody_touches_dims_and_then_goes_out(self):
        levels = []
        t = 0.0
        while t <= self.dark_at() + 1.0:
            levels.append(self.sleep.gain(t))
            t += 1.0
        # Never brighter than it was: the whole gesture is one direction.
        for here, then in zip(levels, levels[1:]):
            self.assertLessEqual(then, here + 1e-9)
        self.assertEqual(levels[0], 1.0)
        self.assertEqual(levels[-1], 0.0)

    def test_it_holds_full_brightness_until_the_timeout_and_pauses_at_the_floor(self):
        # Not a slow sag from the first second -- for half an hour nothing has
        # happened yet, and the board should not be reporting that.
        self.assertEqual(self.sleep.gain(config.SLEEP_DIM_SECONDS - 1.0), 1.0)
        # The first stage lands on the floor and stays there, so there is a
        # readable board for the whole stretch between the two timings.
        settled = config.SLEEP_DIM_SECONDS + config.SLEEP_FADE_SECONDS
        self.assertAlmostEqual(self.sleep.gain(settled), config.SLEEP_DIM_LEVEL, places=5)
        midway = (settled + config.SLEEP_DARK_SECONDS) / 2
        self.assertAlmostEqual(self.sleep.gain(midway), config.SLEEP_DIM_LEVEL, places=5)

    def test_the_fade_is_slow_enough_not_to_read_as_an_event(self):
        # A step this board can see is a step it means. Nothing in either stage
        # may move more than a few percent in a frame.
        t = config.SLEEP_DIM_SECONDS
        while t <= self.dark_at():
            step = abs(self.sleep.gain(t + 1 / config.FPS) - self.sleep.gain(t))
            self.assertLess(step, 0.02, t)
            t += 0.1

    def test_a_touch_brings_it_back_from_wherever_the_fade_had_got_to(self):
        part_way = config.SLEEP_DIM_SECONDS + config.SLEEP_FADE_SECONDS * 0.5
        level = self.sleep.gain(part_way)
        self.assertLess(level, 1.0)
        self.sleep.touch(part_way)
        # Rising from *here*, not from the floor and not from full: a wake that
        # jumps in either direction is a visible glitch.
        self.assertAlmostEqual(self.sleep.gain(part_way), level, places=5)
        self.assertGreater(self.sleep.gain(part_way + config.SLEEP_WAKE_SECONDS / 2), level)
        self.assertEqual(self.sleep.gain(part_way + config.SLEEP_WAKE_SECONDS), 1.0)

    def test_a_touch_part_way_up_does_not_start_over(self):
        """Two events in quick succession on a dark board: the second must not
        drop the brightness back to where the first one started from."""
        self.sleep.touch(self.dark_at())
        early = self.dark_at() + config.SLEEP_WAKE_SECONDS * 0.5
        level = self.sleep.gain(early)
        self.sleep.touch(early)
        self.assertGreaterEqual(self.sleep.gain(early), level - 1e-9)

    def test_waking_restarts_the_clock(self):
        self.sleep.touch(self.dark_at())
        awake = self.dark_at() + config.SLEEP_DIM_SECONDS - 1.0
        self.assertEqual(self.sleep.gain(awake), 1.0)
        self.assertLess(self.sleep.gain(awake + config.SLEEP_FADE_SECONDS * 2), 1.0)

    def test_the_switch_pins_it_awake(self):
        config.SLEEP = False
        try:
            self.assertEqual(self.sleep.gain(self.dark_at()), 1.0)
        finally:
            config.SLEEP = True

    # -- what it does to the board ------------------------------------------

    def slept(self, t: float):
        return board.compose(self.table.all(), 1.0, sleep=self.sleep.gain(t))

    def test_an_idle_encoder_sleeps_and_a_permission_prompt_does_not(self):
        quiet = self.table.ensure("a", "/tmp/a", {"tty": "/dev/a"})
        quiet.state = "idle"
        asking = self.table.ensure("b", "/tmp/b", {"tty": "/dev/b"})
        asking.state = "permission"
        asking.alert = True
        cells = self.slept(self.dark_at())
        # The board is out, except for the one encoder that is asking for a
        # human. Going dark because the human left is the wrong answer to that.
        self.assertEqual(cells[quiet.slot], board.BLANK)
        self.assertEqual(cells[asking.slot], render(asking, 1.0))

    def test_a_finished_session_is_not_an_alert_and_sleeps(self):
        done = self.table.ensure("a", "/tmp/a")
        done.state = "done"
        done.state_since = 1.0
        self.assertGreater(board.compose(self.table.all(), 1.0)[done.slot].brightness, 0)
        self.assertEqual(self.slept(self.dark_at())[done.slot], board.BLANK)

    def test_the_empty_board_stops_breathing_once_it_is_asleep(self):
        self.assertTrue([c for c in board.compose([], 1.0) if c.brightness > 0])
        cells = self.slept(self.dark_at())
        self.assertTrue(all(c is board.BLANK for c in cells))

    def test_a_dimmed_encoder_carries_no_animation(self):
        """Channels 3 and 6 hold either an animation or a brightness, never
        both, so an encoder that keeps its animation cannot be dimmed at all."""
        session = self.table.ensure("a", "/tmp/a")
        session.state = "streaming"
        lit = board.compose(self.table.all(), 1.0)[session.slot]
        self.assertTrue(lit.rgb_anim)
        dozing = self.slept(config.SLEEP_DIM_SECONDS + config.SLEEP_FADE_SECONDS)
        cell = dozing[session.slot]
        self.assertEqual(cell.rgb_anim, config.ANIM_NONE)
        self.assertLess(cell.brightness, lit.brightness)
        # Dim, not dark: the first stage is still a board you can read.
        self.assertGreater(cell.brightness, 0.0)
        self.assertEqual(cell.color, lit.color)

    def test_overlays_are_not_dimmed(self):
        """Every overlay is a gesture, and every gesture is the activity that
        wakes the board -- including the shutdown spiral on the way out."""
        overlay = overlays.ShutdownOverlay(0.0)
        cells = board.compose(
            [], overlay.spiral, [overlay], sleep=self.sleep.gain(self.dark_at())
        )
        self.assertTrue(all(c.brightness > 0.95 for c in cells[:16]))


class _RecordingTwister(twister.NullTwister):
    """A device that keeps every CC it would have put on the wire, so a test can
    assert about what the hardware receives rather than about what the board
    meant. De-duplication is left in place: it is part of what gets sent."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[int, int, int]] = []

    def cc(self, channel: int, control: int, value: int, force: bool = False) -> None:
        key = (channel, control)
        if not force and self._last.get(key) == value:
            return
        self._last[key] = value
        self.sent.append((channel, control, value))


class Overlays(unittest.TestCase):
    def _every_overlay(self, session, t0):
        """One of each, including the two that are driven by a second event
        arriving later -- which is what puts a frame *before* the ramp they
        then compute from."""
        waiting = overlays.WaitingOverlay(t0)
        waiting.dismiss(t0 + 0.5)
        compaction = overlays.CompactOverlay(session, t0)
        compaction.finish(t0 + 1.0)
        return [
            overlays.TextOverlay("RATE", t0, color=config.BANNER_COLOR),
            waiting,
            overlays.ShutdownOverlay(t0),
            overlays.UnwrapOverlay(t0),
            overlays.SpawnOverlay(session, t0),
            overlays.ClearOverlay(session, t0),
            compaction,
            overlays.DismissOverlay(session, t0),
        ]

    def test_no_overlay_ever_paints_outside_the_hardware(self):
        """Every one of these is an eased ramp of an elapsed time, and several
        get their start or end from an event that arrives mid-flight. Asked for
        a frame outside its own window, an overlay should land on the end of its
        ramp -- not compute a negative fill and hand it to the wire."""
        table = SessionTable()
        session = table.ensure("s", "/tmp/p")
        set_subagents(session, 2)
        overlays = self._every_overlay(session, 10.0)
        for state in config.STATE_PRIORITY:
            session.state = state
            for step in range(-50, 250):  # deliberately starts before t0
                for cell in board.compose(table.all(), 10.0 + step * 0.02, overlays):
                    self.assertTrue(0 <= cell.ring <= 127, (state, cell))
                    self.assertTrue(0.0 <= cell.brightness <= 1.0, (state, cell))

    def test_overlays_do_not_run_off_a_short_board(self):
        """Overlays index by slot, and a board is not always 64 long."""
        table = SessionTable()
        session = table.ensure("s", "/tmp/p")
        overlays = self._every_overlay(session, 0.0)
        for step in range(200):
            board.compose(
                table.all(),
                step * 0.02,
                overlays,
                slot_count=config.ENCODERS_PER_BANK,
            )

    def test_a_word_spells_something_then_ends(self):
        overlay = overlays.TextOverlay("RATE", 0.0)
        mid = board.compose([], overlay.duration / 2, [overlay])
        self.assertTrue(any(c.brightness > 0 for c in mid[: config.ENCODERS_PER_BANK]))
        self.assertFalse(overlay.done(overlay.duration - 0.01))
        self.assertTrue(overlay.done(overlay.duration))

    def test_an_untinted_word_is_colorless_all_the_way_through(self):
        # A word with no color asked for is text rather than status. Color is
        # how everything else here means something, so those letters stay out of
        # that vocabulary entirely -- and on this hardware that means no hue at
        # all rather than a white one. The RGB channel is a wheel with no
        # achromatic value anywhere on it (it wraps; 48 and 127 are both green),
        # so a color of None is the only white there is: the ring draws the
        # glyph and the RGB stays dark.
        #
        # Brightness is the letter's own decay and is not asserted here; that is
        # test_each_letter_strikes_full_then_decays_to_its_successor's business.
        overlay = overlays.TextOverlay("CLAUDE", 0.0)
        t = 0.0
        while t < overlay.duration:
            frame = board.compose([], t, [overlay])[: config.ENCODERS_PER_BANK]
            # The overlay's own cutoff, below which a pixel is dark rather than
            # very dim -- the tail of every letter's decay passes through it.
            lit = [c for c in frame if c.brightness > 0.02]
            self.assertEqual({c.color for c in frame}, {None}, t)
            if lit:
                self.assertEqual({c.ring for c in lit}, {127}, t)
            t += 1.0 / config.FPS

    def test_a_colorless_letter_still_lights_the_ring_and_only_the_ring(self):
        # The pixel that carries the glyph, as the hardware sees it: ring at
        # full, RGB extinguished. Both halves matter -- a lit ring is the white
        # block, and any hue underneath it would tint the letter.
        overlay = overlays.TextOverlay("C", 0.0, color=None)
        frame = board.compose([], 0.0, [overlay])[: config.ENCODERS_PER_BANK]
        lit = [c for c in frame if c.brightness > 0]
        dark = [c for c in frame if c.brightness == 0]
        self.assertTrue(lit and dark)
        for cell in lit:
            self.assertIsNone(cell.color)
            self.assertEqual(cell.ring, 127)
        for cell in dark:
            self.assertIsNone(cell.color)
            self.assertEqual(cell.ring, 0)

    def test_a_banner_still_gets_to_be_a_color(self):
        # Colorless is a default, not a property of TextOverlay:
        # a banner shouting RATE at you is status, and status is a hue.
        overlay = overlays.TextOverlay("RATE", 0.0, color="red")
        frame = board.compose([], 0.0, [overlay])[: config.ENCODERS_PER_BANK]
        lit = [c for c in frame if c.brightness > 0]
        self.assertTrue(lit)
        self.assertEqual({c.color for c in lit}, {"red"})

    def test_nothing_in_the_quiet_stretch_lights_the_rgb(self):
        # The invariant, rather than the three places that happened to break it.
        # After boot the board is the waiting gradients -- and underneath them,
        # ambient, which only shows through where whatever is on top is dimmer.
        # That last one is how the blue got out: it is painted by compose() and
        # not by any overlay, so testing the overlays alone would miss it.
        # Every frame, every slot, all the way through: no hue.
        waiting = overlays.WaitingOverlay(0.0)
        t = 0.0
        end = waiting.duration
        while t < end:
            showing = [waiting]
            for cell in board.compose([], t, showing):
                self.assertIsNone(cell.color, t)
            t += 0.25
        # And the idle board it settles onto once the waiting field has gone,
        # which is what you are left looking at.
        for cell in board.compose([], end + 1.0, []):
            self.assertIsNone(cell.color)

    def test_the_quiet_stretch_switches_the_rgb_off_on_the_wire(self):
        # The board saying "no color" is only half of it. This is the other
        # half: what channel 3 actually carries for all 64 encoders, every
        # frame, from clear_all through the waiting gradients.
        #
        # It is a separate test because the bug it exists for was invisible from
        # the Cell side. Every cell said color=None all the way through, and the
        # value that None turned into was 17 -- which is not the bottom of the
        # brightness ramp, it is the slowest *pulse* rate. Sixteen encoders
        # breathing in unison off the daemon's own MIDI clock, reading blue
        # because a dim RGB always does. The board was right and the wire was
        # wrong, so only a test at this level catches it coming back.
        recorder = _RecordingTwister()
        recorder.clear_all()
        waiting = overlays.WaitingOverlay(0.0)
        t = 0.0
        while t < 5.0:
            for slot, cell in enumerate(board.compose([], t, [waiting])):
                recorder.write(slot, cell)
            t += 1.0 / config.FPS

        rgb = {v for ch, _, v in recorder.sent if ch == config.CH_SWITCH_ANIM}
        self.assertEqual(rgb, {config.DARK_VALUE})
        # And spelled out, so a future edit to DARK_VALUE has to face it: the
        # off value is above the pulse band, not at the bottom of it.
        self.assertEqual(config.DARK_VALUE, 18)
        self.assertGreater(config.DARK_VALUE, max(config.ANIM_PULSE.values()))
        self.assertNotIn(config.DARK_VALUE, set(config.ANIM_GATE.values()))

    def test_switching_the_rgb_off_dims_before_it_recolors(self):
        # At startup channel 3 is at whatever it was before we opened the port,
        # so a hue sent first lands on a lit LED and flashes it. The off has to
        # go first.
        recorder = _RecordingTwister()
        recorder.rgb_off(0)
        channels = [ch for ch, _, _ in recorder.sent]
        self.assertEqual(channels, [config.CH_SWITCH_ANIM, config.CH_SWITCH])

    def test_the_waiting_animation_reaches_every_encoder_but_never_full(self):
        # It replaced a lamp test, and this is the trade that was made: the
        # gradients still travel the whole grid, so no encoder sits out, but
        # nothing on the board goes near full. A board with nothing to say does
        # not get to look like a board with everything to say.
        waiting = overlays.WaitingOverlay(0.0)
        seen, peak = set(), 0.0
        t = 0.0
        while t < config.WAITING_PERIOD_SECONDS * 4:
            # Painted onto a bare board rather than composed: the ambient layer
            # underneath lights every encoder by itself, and it would answer
            # this question for the overlay.
            frame = board.blank_board(config.ENCODERS_PER_BANK)
            waiting.apply(frame, t)
            seen |= {
                i
                for i, c in enumerate(frame)
                if c.brightness > config.AMBIENT_BRIGHTNESS
            }
            peak = max(peak, max(c.brightness for c in frame))
            t += 1.0 / config.FPS
        self.assertEqual(seen, set(range(config.ENCODERS_PER_BANK)))
        self.assertLessEqual(peak, config.WAITING_BRIGHTNESS + 1e-9)
        self.assertLess(peak, config.ACTIVE_BRIGHTNESS)

    def test_each_letter_strikes_full_then_decays_to_its_successor(self):
        # A letter is at full the instant it appears, and by the time the next
        # one strikes the only pixels still lit are the ones that letter is
        # about to light anyway -- those hold rather than blinking off and
        # straight back on. Everything else is gone.
        word = "CLAUDE"
        overlay = overlays.TextOverlay(word, 0.0)
        for index, char in enumerate(word):
            start = board.compose([], index * overlay.step + 0.001, [overlay])
            end = board.compose([], (index + 1) * overlay.step - 0.001, [overlay])
            lit = [c.brightness for c in start[: config.ENCODERS_PER_BANK] if c.brightness > 0]
            self.assertTrue(lit, f"letter {index} should strike")
            self.assertAlmostEqual(max(lit), 1.0, places=2, msg="strikes at full")

            glyph = font.pixels(char)
            nxt = (
                font.pixels(word[index + 1])
                if index + 1 < len(word)
                else (0.0,) * config.ENCODERS_PER_BANK
            )
            for offset, cell in enumerate(end[: config.ENCODERS_PER_BANK]):
                shared = glyph[offset] and nxt[offset]
                if shared:
                    self.assertAlmostEqual(
                        cell.brightness, 1.0, places=2,
                        msg=f"{char}[{offset}] is also in the next letter and should hold",
                    )
                else:
                    self.assertLess(
                        cell.brightness, 0.02,
                        f"{char}[{offset}] should be gone before the next letter",
                    )

    def test_the_board_never_goes_black_between_overlapping_letters(self):
        # The bug this guards: every letter decayed all the way to nothing, so
        # a word flickered through full darkness between every pair of letters.
        overlay = overlays.TextOverlay("CLAUDE", 0.0)
        for index in range(len(overlay.text) - 1):
            boundary = (index + 1) * overlay.step - 0.001
            frame = board.compose([], boundary, [overlay])
            self.assertTrue(
                any(c.brightness > 0.02 for c in frame[: config.ENCODERS_PER_BANK]),
                f"board went black between letter {index} and {index + 1}",
            )

    def test_untinted_letters_are_white(self):
        # No hue at all: the RGB switch is left off and the word is spelled in
        # the encoder rings, which are the only white light on the device.
        overlay = overlays.TextOverlay("CLAUDE", 0.0)
        frame = board.compose([], overlay.step * 0.1, [overlay])
        lit = [c for c in frame[: config.ENCODERS_PER_BANK] if c.brightness > 0]
        self.assertTrue(lit)
        self.assertTrue(all(c.color is None and c.ring == 127 for c in lit))

    def test_a_word_is_unhurried_and_shutdown_is_not(self):
        # A word you can only catch the tail of may as well not be spelled, so
        # the default letter time is deliberately slow. Shutdown has something
        # waiting on it (`--stop` gives the daemon five seconds), so it says
        # "on purpose" without spelling anything.
        word = overlays.TextOverlay("CLAUDE", 0.0)
        shutdown = overlays.ShutdownOverlay(0.0)
        self.assertGreater(word.duration, 4.0, "a word should be legible, not a blink")
        self.assertLess(shutdown.duration, word.duration)

    def test_shutdown_spirals_from_the_top_left_corner_into_the_centre(self):
        path = board.spiral_path(0)
        self.assertEqual(len(path), config.ENCODERS_PER_BANK)
        self.assertEqual(sorted(path), list(range(config.ENCODERS_PER_BANK)))
        self.assertEqual(path[0], 0, "the head leaves the top-left corner")
        # Every step is to a neighbour -- a spiral is a path, not an order.
        for here, then in zip(path, path[1:]):
            self.assertEqual(
                abs(here % 4 - then % 4) + abs(here // 4 - then // 4), 1, (here, then)
            )
        # ...and it ends in the middle, not on an edge.
        self.assertIn(path[-1], (5, 6, 9, 10))

        overlay = overlays.ShutdownOverlay(0.0)
        first = board.compose([], 0.15, [overlay])
        self.assertGreater(first[path[0]].brightness, 0.0)
        self.assertEqual(first[path[-1]].brightness, 0.0)
        filled = board.compose([], overlay.spiral, [overlay])
        self.assertTrue(all(c.brightness > 0.95 for c in filled[:16]))

    def test_shutdown_dims_in_unison_on_one_hue_then_goes_dark(self):
        overlay = overlays.ShutdownOverlay(0.0)
        seen, levels = set(), []
        t = overlay.spiral
        while t < overlay.duration - 0.01:
            cells = board.compose([], t, [overlay])[: config.ENCODERS_PER_BANK]
            # Every encoder wears the same hue at the same brightness: sixteen
            # knobs as one object is the whole point of the exit.
            self.assertEqual(len({c.color for c in cells}), 1, t)
            self.assertEqual(len({round(c.brightness, 6) for c in cells}), 1, t)
            seen.add(cells[0].color)
            levels.append(cells[0].brightness)
            t += 0.05
        # The color does nothing on the way out -- only the lamp goes down.
        self.assertEqual(seen, {overlay.hue, None}, seen)
        self.assertAlmostEqual(levels[0], 1.0, places=2)
        # Held whole, then monotonically dimmer: no swell, no flicker back up.
        held = board.compose([], overlay.spiral + overlay.hold * 0.5, [overlay])
        self.assertAlmostEqual(held[0].brightness, 1.0, places=2)
        for here, then in zip(levels, levels[1:]):
            self.assertLessEqual(then, here + 1e-9)
        self.assertEqual(levels[-1], 0.0)

        # It ends genuinely off, not merely dim: an encoder still holding a hue
        # at the hardware's floor brightness is an encoder still lit.
        last = board.compose([], overlay.duration - 0.01, [overlay])
        self.assertTrue(all(c.color is None for c in last[:16]), last[0])
        self.assertTrue(overlay.done(overlay.duration))

    def test_the_unwrap_rises_in_unison_on_one_hue_and_holds_the_board_whole(self):
        # The inverse of the shutdown fade: sixteen encoders as one object, on
        # one hue for the whole gesture the way the exit is, arriving at a full
        # board rather than passing through one.
        overlay = overlays.UnwrapOverlay(0.0)
        levels = []
        # Past the dark floor: the first couple of frames of an eased rise are
        # under it, and below the floor an encoder is off rather than dim --
        # which is the point of the floor and not an exception to the hue.
        t = 0.15
        while t < overlay.rise:
            cells = board.compose([], t, [overlay])[: config.ENCODERS_PER_BANK]
            self.assertEqual({c.color for c in cells}, {overlay.hue}, t)
            self.assertEqual(len({round(c.brightness, 6) for c in cells}), 1, t)
            levels.append(cells[0].brightness)
            t += 0.05
        for here, then in zip(levels, levels[1:]):
            self.assertGreaterEqual(then, here - 1e-9)
        held = board.compose([], overlay.rise + overlay.hold * 0.5, [overlay])
        self.assertTrue(all(c.brightness > 0.95 for c in held[:16]), held[0])
        self.assertTrue(all(c.ring > 120 for c in held[:16]), held[0])

    def test_the_unwrap_comes_apart_from_the_centre_out_and_ends_black(self):
        # The shutdown's spiral, reversed: the head leaves the middle and the
        # last thing lit is the corner the exit gesture starts from.
        overlay = overlays.UnwrapOverlay(0.0)
        path = board.spiral_path(0)
        # Just after the head departs the centre, and well before it reaches the
        # corner: the middle is out, the corner is still whole.
        t = overlay.rise + overlay.hold + overlay.fall + 0.05
        early = board.compose([], t, [overlay])
        self.assertEqual(early[path[-1]].brightness, 0.0, "the centre goes first")
        self.assertGreater(early[path[0]].brightness, 0.9, "the corner goes last")

        # The hue leaves with the light, not after it: the word gets a board
        # with nothing on it, color included.
        last = board.compose([], overlay.duration - 0.01, [overlay])
        self.assertTrue(all(c.brightness == 0.0 for c in last[:16]), last[0])
        self.assertTrue(all(c.color is None for c in last[:16]), last[0])
        self.assertTrue(overlay.done(overlay.duration))

    def test_the_two_bookends_wear_different_hues(self):
        # Both ends of a run are one hue held for the whole gesture; which end
        # you are watching is the hue, since the spiral direction is only
        # legible if you caught the start.
        self.assertNotEqual(overlays.UnwrapOverlay(0.0).hue, overlays.ShutdownOverlay(0.0).hue)

    def test_the_unwrap_is_the_whole_of_boot_and_is_brief(self):
        # It is all there is between launching the daemon and a board that can
        # report, so it has to be a gesture you catch rather than one you wait
        # out. The word that used to follow it is gone for the same reason.
        unwrap = overlays.UnwrapOverlay(0.0)
        self.assertLess(unwrap.duration, 4.0)

    def test_shutdown_is_slower_than_before_but_still_beats_the_stop_timeout(self):
        # `--stop` gives the daemon five seconds to clear the LEDs and let go of
        # the MIDI port; the animation has to leave room for both.
        overlay = overlays.ShutdownOverlay(0.0)
        self.assertGreater(overlay.duration, 3.0)
        self.assertLess(overlay.duration, 4.5)

    def test_waiting_never_flashes_the_board_on(self):
        # The failure mode of the thing it replaced, pinned: there is no opening
        # sweep, nothing steps, and no frame is much brighter than the one
        # before it. What you should not be able to see is the animation start.
        overlay = overlays.WaitingOverlay(0.0)
        first = board.blank_board(config.ENCODERS_PER_BANK)
        overlay.apply(first, 0.0)
        self.assertEqual(max(c.brightness for c in first), 0.0)
        previous = None
        t = 0.0
        while t <= 30.0:
            cells = board.compose([], t, [overlay])[: config.ENCODERS_PER_BANK]
            levels = [c.brightness for c in cells]
            if previous is not None:
                jump = max(abs(a - b) for a, b in zip(levels, previous))
                self.assertLess(jump, 0.05, t)
            previous = levels
            t += 1.0 / config.FPS

    def test_waiting_keeps_moving_and_never_repeats(self):
        overlay = overlays.WaitingOverlay(0.0)
        frames = [
            tuple(
                (c.ring, c.color)
                for c in board.compose([], t, [overlay])[: config.ENCODERS_PER_BANK]
            )
            for t in (5.0, 5.5, 9.0, 17.0, 31.0)
        ]
        self.assertEqual(len(set(frames)), len(frames), "the field is not generative")

    def test_waiting_fades_out_on_its_own(self):
        overlay = overlays.WaitingOverlay(0.0)

        def brightest(start):
            # Over a window rather than at an instant: the gradients travel, so
            # any single frame might be one where the crest is off the grid.
            return max(
                cell.brightness
                for step in range(300)
                for cell in board.compose([], start + step * 0.1, [overlay])[
                    : config.ENCODERS_PER_BANK
                ]
            )

        early = brightest(2.0)
        self.assertGreater(early, config.WAITING_BRIGHTNESS * 0.6)
        self.assertLess(brightest(config.WAITING_SECONDS - 35.0), early / 2)
        self.assertFalse(overlay.done(config.WAITING_SECONDS - 0.01))
        self.assertTrue(overlay.done(config.WAITING_SECONDS))

    def test_waiting_gets_out_of_the_way_when_a_session_shows_up(self):
        overlay = overlays.WaitingOverlay(0.0)
        overlay.dismiss(10.0)
        self.assertFalse(overlay.done(10.0 + config.WAITING_DISMISS_SECONDS / 2))
        self.assertTrue(overlay.done(10.0 + config.WAITING_DISMISS_SECONDS))
        # ...and it retreats rather than hard-cutting, so the first encoder to
        # light reads as emerging from the field.
        lit = board.compose([], 10.1, [overlay])[: config.ENCODERS_PER_BANK]
        self.assertTrue(any(c.brightness > 0 for c in lit))

    def test_waiting_never_dims_a_live_session(self):
        table = SessionTable()
        session = table.ensure("s", "/tmp/p")
        session.state = "waiting"
        overlay = overlays.WaitingOverlay(0.0)
        overlay.dismiss(4.0)
        bare = board.compose(table.all(), 4.1)[session.slot]
        over = board.compose(table.all(), 4.1, [overlay])[session.slot]
        self.assertEqual(bare, over)

    def test_waiting_waits_a_couple_of_frames_before_it_starts(self):
        # The board is empty at the instant boot ends whether or not anything
        # is running: a session that started during the unwrap has only a hook
        # in flight to say so. So the decision is deferred and re-taken
        # every frame until it fires.
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        vis = Visualizer(NullTwister())
        vis._waiting_due = 10.0 + config.WAITING_START_DELAY_SECONDS
        vis._check_waiting(10.0)
        self.assertIsNone(vis._waiting)
        vis._check_waiting(10.0 + config.WAITING_START_DELAY_SECONDS)
        self.assertIsInstance(vis._waiting, overlays.WaitingOverlay)
        self.assertIn(vis._waiting, vis._live_overlays(10.2))

    def test_waiting_never_starts_on_a_board_that_was_never_empty(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        vis = Visualizer(NullTwister())
        vis.table.ensure("s", "/tmp/p")
        vis._waiting_due = 10.0 + config.WAITING_START_DELAY_SECONDS
        for step in range(10):
            vis._check_waiting(10.0 + step * 0.05)
        self.assertIsNone(vis._waiting)
        self.assertEqual(vis._live_overlays(11.0), [])

    def test_a_session_that_ended_does_not_count_as_waiting_being_over(self):
        # Its encoder is only lingering so you can see how it finished.
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        vis = Visualizer(NullTwister())
        session = vis.table.ensure("s", "/tmp/p")
        session.ended_at = 5.0
        vis._waiting_due = 10.0
        vis._check_waiting(10.0)
        self.assertIsInstance(vis._waiting, overlays.WaitingOverlay)
        # ...and a live one does retire it, without a hard cut.
        vis.table.ensure("t", "/tmp/q")
        vis._check_waiting(11.0)
        self.assertEqual(vis._waiting.dismissed_at, 11.0)
        self.assertFalse(vis._waiting.done(11.0 + config.WAITING_DISMISS_SECONDS / 2))

    def test_spawn_strikes_bright_then_settles_into_the_session(self):
        table = SessionTable()
        session = table.ensure("s", "/tmp/p")
        session.state = "idle"
        overlay = overlays.SpawnOverlay(session, 0.0)

        idle = board.compose([session], 0.1)[session.slot]
        strike = board.compose([session], 0.1, [overlay])[session.slot]
        self.assertGreater(strike.brightness, idle.brightness)
        self.assertAlmostEqual(strike.brightness, 1.0, places=5)

        # It hands the encoder back rather than cutting: by the last frame it is
        # indistinguishable from the steady state underneath.
        landed = board.compose([session], config.SPAWN_SECONDS - 0.01, [overlay])[
            session.slot
        ]
        self.assertEqual(landed.color, idle.color)
        self.assertAlmostEqual(landed.brightness, idle.brightness, places=2)

    def test_spawn_is_brief_and_blinks_a_countable_number_of_times(self):
        session = SessionTable().ensure("s", "/tmp/p")
        overlay = overlays.SpawnOverlay(session, 0.0)
        self.assertLess(config.SPAWN_SECONDS, 2.0, "punctuation, not a status")
        cells = [
            board.compose([session], t / 200, [overlay])[session.slot]
            for t in range(int(config.SPAWN_SECONDS * 200))
        ]
        flashing = cells[: int(config.SPAWN_SECONDS * config.SPAWN_SETTLE * 200)]

        # Full on or fully dark, never in between: an eased blink reads as
        # breathing, and breathing is a state rather than an event.
        self.assertEqual({c.ring for c in flashing}, {0, 127})
        # Three flashes, countable at a glance -- rising edges, so a run that
        # starts lit is still one flash rather than two.
        edges = sum(
            1
            for prev, cell in zip(flashing, flashing[1:])
            if prev.ring == 0 and cell.ring == 127
        )
        self.assertEqual(edges + (1 if flashing[0].ring else 0), config.SPAWN_FLASHES)
        # Bright red RGB held across the whole strike, including the dark half
        # of each blink: the ring blinks, the color does not.
        self.assertTrue(all(c.color == config.SPAWN_COLOR for c in flashing))
        self.assertTrue(all(abs(c.brightness - 1.0) < 1e-6 for c in flashing))

        self.assertFalse(overlay.done(config.SPAWN_SECONDS - 0.01))
        self.assertTrue(overlay.done(config.SPAWN_SECONDS))

    def test_spawn_follows_its_session_when_the_board_compacts(self):
        table = SessionTable()
        first, second = table.ensure("a", "/tmp/a"), table.ensure("b", "/tmp/b")
        overlay = overlays.SpawnOverlay(second, 0.0)
        table.release(first)  # second slides up to slot 0 mid-flash
        cells = board.compose(table.all(), 0.2, [overlay])
        self.assertAlmostEqual(cells[second.slot].brightness, 1.0, places=5)

    def test_starting_a_session_flashes_its_encoder(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        vis = Visualizer(NullTwister())
        vis.handle_event({"session_id": "s", "hook_event_name": "SessionStart"})
        self.assertTrue(
            any(isinstance(o, overlays.SpawnOverlay) for o in vis._live_overlays(0.0))
        )

    def test_compaction_drains_then_refills(self):
        overlay = overlays.CompactOverlay(SessionTable().ensure("s"), 0.0)
        drained = board.compose([], config.COMPACT_DRAIN_SECONDS, [overlay])[0]
        self.assertEqual(drained.ring, 0)
        overlay.finish(config.COMPACT_DRAIN_SECONDS)
        refilled = board.compose(
            [],
            config.COMPACT_DRAIN_SECONDS + config.COMPACT_REFILL_SECONDS + 0.1,
            [overlay],
        )[0]
        self.assertEqual(refilled.ring, 127)
        self.assertTrue(overlay.done(1_000))

    def test_clearing_unwinds_the_ring_to_nothing_and_hands_the_encoder_back(self):
        session = SessionTable().ensure("s", "/tmp/p")
        session.state = "idle"
        overlay = overlays.ClearOverlay(session, 0.0)

        strike = board.compose([session], 0.0, [overlay])[session.slot]
        self.assertEqual(strike.ring, 127)
        # White: the RGB stays dark, because this is the board saying something
        # rather than reporting a status.
        self.assertIsNone(strike.color)

        emptied = board.compose(
            [session], config.CLEAR_SECONDS * config.CLEAR_SETTLE, [overlay]
        )[session.slot]
        self.assertEqual(emptied.ring, 0)

        # ...and then becomes the steady state rather than cutting to it.
        idle = board.compose([session], config.CLEAR_SECONDS - 0.01)[session.slot]
        landed = board.compose([session], config.CLEAR_SECONDS - 0.01, [overlay])[
            session.slot
        ]
        self.assertEqual(landed.color, idle.color)
        self.assertAlmostEqual(landed.brightness, idle.brightness, places=2)
        self.assertTrue(overlay.done(config.CLEAR_SECONDS))

    def test_clearing_is_briefer_than_an_arrival(self):
        # An arrival is worth catching across the room; a /clear is something
        # you just typed and are already looking at.
        self.assertLess(config.CLEAR_SECONDS, config.SPAWN_SECONDS)

    def test_compaction_gives_up_if_postcompact_never_arrives(self):
        overlay = overlays.CompactOverlay(SessionTable().ensure("s"), 0.0)
        self.assertFalse(overlay.done(config.COMPACT_TIMEOUT_SECONDS - 1))
        self.assertTrue(overlay.done(config.COMPACT_TIMEOUT_SECONDS + 1))

    def test_a_held_knob_burns_its_ring_down_to_nothing(self):
        session = SessionTable().ensure("s", "/tmp/p")
        overlay = overlays.DismissOverlay(session, 0.0)
        under = board.compose([session], config.DISMISS_ARM_SECONDS / 2, [])[0]
        early = board.compose([session], config.DISMISS_ARM_SECONDS / 2, [overlay])[0]
        self.assertEqual(early, under, "a tap should not flash the countdown")
        middle = board.compose([session], config.HOLD_SECONDS / 2, [overlay])[0]
        end = board.compose([session], config.HOLD_SECONDS * 0.99, [overlay])[0]
        self.assertLess(end.ring, middle.ring, "the fuse burns down")
        self.assertLess(end.ring, 10)
        self.assertIsNone(middle.color, "white on a dark switch, like the wipe")

    def test_the_fuse_ends_on_release_or_on_maturity(self):
        session = SessionTable().ensure("s", "/tmp/p")
        overlay = overlays.DismissOverlay(session, 0.0)
        self.assertFalse(overlay.done(config.HOLD_SECONDS / 2))
        self.assertFalse(overlay.matured(config.HOLD_SECONDS / 2))
        self.assertTrue(overlay.matured(config.HOLD_SECONDS))
        self.assertTrue(overlay.done(config.HOLD_SECONDS))

        let_go = overlays.DismissOverlay(session, 0.0)
        let_go.release()
        self.assertTrue(let_go.done(0.0), "the gesture is spring-loaded")
        self.assertFalse(
            let_go.matured(config.HOLD_SECONDS * 2), "a fuse let go never fires"
        )


class EncoderTurns(unittest.TestCase):
    """A knob is not a control. Turning one changes nothing about the session,
    is undone by the next frame, and leaves the rest of the board exactly as it
    found it -- including a board that had dimmed itself. The bottom-right knob
    of the current bank is the single exception, because it asks a question."""

    def visualizer(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        return Visualizer(NullTwister())

    def test_a_turn_changes_nothing_about_the_session(self):
        vis = self.visualizer()
        vis.handle_event(
            {"session_id": "a", "cwd": "/tmp/a", "hook_event_name": "SessionStart"}
        )
        session = vis.table.by_slot(0)
        before = dataclasses.asdict(session)
        vis.on_midi(
            SimpleNamespace(
                type="control_change", channel=config.CH_ENCODER, control=0, value=65
            )
        )
        self.assertEqual(dataclasses.asdict(vis.table.by_slot(0)), before)

    def asleep(self, vis):
        vis._sleep.last_activity = time.monotonic() - 2 * config.SLEEP_DARK_SECONDS
        self.assertEqual(vis._sleep.gain(time.monotonic()), 0.0)

    def turn(self, vis, control):
        vis.on_midi(
            SimpleNamespace(
                type="control_change",
                channel=config.CH_ENCODER,
                control=control,
                value=65,
            )
        )

    def test_a_turn_leaves_a_dimmed_board_where_it_found_it(self):
        """A turn is the one input here nobody necessarily meant: a sleeve, a
        cable, a hand reaching past. It used to count as a hand on the hardware
        and relight the whole board, which is sixteen encoders swelling out of
        the dark because something brushed one knob."""
        vis = self.visualizer()
        self.asleep(vis)
        self.turn(vis, 0)
        self.assertEqual(
            vis._sleep.gain(time.monotonic() + config.SLEEP_WAKE_SECONDS), 0.0
        )

    def test_the_knob_that_asks_a_question_still_wakes_it(self):
        """The exception, and it is the whole of the exception: a turn that is a
        question has to be answered on a board you can see. A press wakes it
        too, but a press on a claimed encoder also goes and focuses a terminal.
        """
        vis = self.visualizer()
        self.asleep(vis)
        self.turn(vis, config.USAGE_PEEK_ENCODER)
        self.assertEqual(
            vis._sleep.gain(time.monotonic() + config.SLEEP_WAKE_SECONDS), 1.0
        )

    def test_a_press_still_wakes_it(self):
        vis = self.visualizer()
        self.asleep(vis)
        vis.on_midi(
            SimpleNamespace(
                type="control_change", channel=config.CH_SWITCH, control=3, value=127
            )
        )
        self.assertEqual(
            vis._sleep.gain(time.monotonic() + config.SLEEP_WAKE_SECONDS), 1.0
        )

    def recorder_visualizer(self):
        from mft.daemon import Visualizer
        from test_twister import Recorder

        return Visualizer(Recorder())

    def rings_sent(self, device) -> set[int]:
        return {slot for ch, slot, _ in device.sent if ch == config.CH_ENCODER}

    def test_a_static_board_still_restates_every_ring_periodically(self):
        """A knob turned by hand lights its own ring, and the daemon may never
        hear about it -- the MIDI input port is optional, and a knob nudged
        before the daemon started was never a message at all. So the repair
        cannot depend on noticing: the rings go out again on a timer."""
        vis = self.recorder_visualizer()
        vis.handle_event(
            {"session_id": "a", "cwd": "/tmp/a", "hook_event_name": "SessionStart"}
        )
        vis.paint(1.0)
        vis.device.sent.clear()

        # Mid-interval: the de-dup cache is right about every ring, so nothing
        # about an unchanged board goes on the wire.
        vis.paint(1.0 + config.RING_REFRESH_SECONDS / 2)
        self.assertEqual(self.rings_sent(vis.device), set())

        # A refresh frame restates all of them anyway, cache or no cache.
        vis.paint(1.0 + config.RING_REFRESH_SECONDS)
        self.assertEqual(self.rings_sent(vis.device), set(range(config.SLOT_COUNT)))


class FrameRate(unittest.TestCase):
    """The loop paces itself off whether the board is moving, so a desk full of
    idle sessions costs nothing to keep lit."""

    def visualizer(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        return Visualizer(NullTwister())

    def start(self, vis, session_id="a"):
        vis.handle_event(
            {
                "session_id": session_id,
                "cwd": f"/tmp/{session_id}",
                "hook_event_name": "SessionStart",
            }
        )
        return vis.table.get(session_id)

    def test_an_idle_board_reports_nothing_moving(self):
        vis = self.visualizer()
        self.start(vis)
        self.assertTrue(vis.paint(1.0), "the first frame is always a change")
        self.assertFalse(vis.paint(1.1))
        self.assertFalse(vis.paint(9.9))

    def test_a_sweeping_session_reports_movement_every_frame(self):
        vis = self.visualizer()
        self.start(vis)
        vis.handle_event({"session_id": "a", "hook_event_name": "UserPromptSubmit"})
        t = 1.0
        vis.paint(t)
        for _ in range(10):
            t += 1.0 / config.FPS
            self.assertTrue(vis.paint(t), "a sweep moves on every frame")

    def test_an_event_wakes_the_loop_out_of_its_slow_sleep(self):
        vis = self.visualizer()
        self.start(vis)
        vis.paint(1.0)
        vis._wake.clear()
        vis.handle_event({"session_id": "a", "hook_event_name": "UserPromptSubmit"})
        self.assertTrue(vis._wake.is_set())

    def test_a_press_wakes_the_loop_too(self):
        # Attention is forgiven on the press, and a board of unattended sessions
        # sitting at their ceiling is precisely a static one.
        vis = self.visualizer()
        self.start(vis)
        vis.paint(1.0)
        vis._wake.clear()
        vis._on_switch(0, 127)
        self.assertTrue(vis._wake.is_set())


class NothingAnswersBack(unittest.TestCase):
    """The device is a display. It must not be able to influence a session."""

    def test_no_hook_can_decide_anything(self):
        import install_hooks

        hooks = install_hooks.build_hooks(
            "http://127.0.0.1:7654/event", with_message_display=True
        )
        self.assertNotIn("PermissionRequest", hooks)
        # No HTTP hook carries a timeout, because none of them is waited on for
        # an answer -- a timeout would only ever be a symptom of one that is.
        for event, entries in hooks.items():
            for entry in entries:
                for hook in entry["hooks"]:
                    if hook["type"] == "http":
                        self.assertNotIn("timeout", hook, event)

    def test_installing_removes_a_permission_hook_left_by_an_older_version(self):
        import install_hooks

        stale = {
            "hooks": {
                "PermissionRequest": [
                    {
                        "hooks": [
                            {
                                "type": "http",
                                "url": "http://127.0.0.1:7654/permission",
                                "_source": install_hooks.TAG,
                            }
                        ]
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "yours"}]}],
            }
        }
        merged = install_hooks.merge(stale, install_hooks.build_hooks("u", False))
        self.assertNotIn("PermissionRequest", merged["hooks"])
        self.assertIn(
            {"type": "command", "command": "yours"}, merged["hooks"]["Stop"][0]["hooks"]
        )

    def test_the_daemon_puts_no_body_on_the_wire(self):
        from mft.daemon import Visualizer

        self.assertFalse(hasattr(Visualizer, "handle_permission"))


class HoldingToClear(unittest.TestCase):
    """Hold a knob and the session on it comes off the board.

    The one gesture that takes something away, and the only repair a display
    cannot make for itself: a record whose agent is gone, with no event coming
    to say so."""

    def setUp(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        self.vis = Visualizer(NullTwister())
        self.focused = []
        self.vis.focus_session = self.focused.append
        self.session = self.vis.table.ensure("a", "/tmp/p")
        self.session.state = "idle"

    def hold(self, slot: int) -> None:
        """A press that outlasts the fuse, the way the render loop sees it."""
        self.vis._on_switch(slot, 127)
        self.vis._check_holds(time.monotonic() + config.HOLD_SECONDS)
        self.vis._on_switch(slot, 0)

    def test_a_hold_takes_the_session_off_the_board(self):
        self.hold(self.session.slot)
        self.assertEqual(self.vis.table.all(), [])
        self.assertEqual(self.focused, [], "you held it to avoid opening it")

    def test_letting_go_early_focuses_the_tab_and_keeps_the_session(self):
        self.vis._on_switch(self.session.slot, 127)
        self.vis._check_holds(time.monotonic())  # a frame mid-hold
        self.vis._on_switch(self.session.slot, 0)
        self.assertEqual(self.focused, [self.session])
        self.assertEqual(self.vis.table.all(), [self.session])

    def test_the_encoder_is_free_for_whoever_is_next(self):
        second = self.vis.table.ensure("b", "/tmp/q")
        self.hold(self.session.slot)
        self.assertEqual(second.slot, 0, "the board squeezes back up")
        self.assertIsNone(self.vis.table.get("a"))

    def test_a_session_that_was_only_sleeping_comes_straight_back(self):
        # Clearing is a statement about the board, never about the agent: an
        # encoder taken from a session that is in fact alive is re-claimed by
        # its very next hook event.
        self.hold(self.session.slot)
        self.vis.handle_event(
            {"session_id": "a", "cwd": "/tmp/p", "hook_event_name": "UserPromptSubmit"}
        )
        self.assertIsNotNone(self.vis.table.get("a"))

    def test_an_empty_encoder_has_nothing_to_clear(self):
        self.hold(self.session.slot + 1)
        self.assertEqual(self.vis.table.all(), [self.session])

    def test_the_gesture_can_be_switched_off(self):
        with mock.patch.object(config, "DISMISS_ON_HOLD", False):
            self.hold(self.session.slot)
        self.assertEqual(self.vis.table.all(), [self.session])

    def test_the_view_does_not_move_out_from_under_a_finger(self):
        self.vis._on_switch(self.session.slot, 127)
        self.assertTrue(self.vis._holds, "the fuse blocks the bank follow")


class PressingASubagent(unittest.TestCase):
    """A violet dot is a second target for its parent's tab.

    There is nothing finer to aim at: a subagent runs inside its parent's
    terminal, so the parent is the only window a press could raise -- and the
    alternative was four dead knobs on the busiest session on the board."""

    def setUp(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        self.vis = Visualizer(NullTwister())
        self.focused = []
        self.vis.focus_session = self.focused.append
        self.parent = self.vis.table.ensure("parent", "/tmp/p")
        self.parent.state = "working"
        set_subagents(self.parent, 1)
        self.pile = config.ENCODERS_PER_BANK - 1

    def press(self, slot: int) -> None:
        self.vis._on_switch(slot, 127)
        self.vis._on_switch(slot, 0)

    def test_a_press_on_the_pile_raises_the_parents_tab(self):
        self.press(self.pile)
        self.assertEqual(self.focused, [self.parent])

    def test_a_hold_on_the_pile_clears_nothing(self):
        # A press on a dot raises the parent's tab because that is the only
        # window there is. Clearing has no such translation: it would take the
        # parent's encoder away over a knob that is not the parent's encoder.
        self.vis._on_switch(self.pile, 127)
        self.assertEqual(self.vis._holds, {})
        self.vis._check_holds(config.HOLD_SECONDS * 2)
        self.assertIsNotNone(self.vis.table.get("parent"))

    def test_a_subagent_that_finishes_mid_press_still_raises_the_parent(self):
        # The pile is recomputed every frame, so the dot under your finger can
        # be gone by the time you let go. The aim was taken on the way down.
        self.vis._on_switch(self.pile, 127)
        set_subagents(self.parent, 0)
        self.vis._on_switch(self.pile, 0)
        self.assertEqual(self.focused, [self.parent])

    def test_a_dark_encoder_is_still_nothing(self):
        self.press(self.pile - 1)
        self.assertEqual(self.focused, [])

    def test_the_pile_is_inert_when_the_gesture_is_turned_off(self):
        with mock.patch.object(config, "SUBAGENT_PRESS", False):
            self.press(self.pile)
        self.assertEqual(self.focused, [])

    def test_a_press_on_the_parent_is_unchanged(self):
        self.press(self.parent.slot)
        self.assertEqual(self.focused, [self.parent])


class SubagentRouting(unittest.TestCase):
    """A subagent owns no encoder, so its events must never be able to claim
    one. Whether they carry the parent's session id or the subagent's own is
    undocumented, and the second case would otherwise light a fresh knob."""

    def visualizer(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        return Visualizer(NullTwister())

    def test_a_subagent_event_never_allocates_an_encoder(self):
        vis = self.visualizer()
        vis.handle_event(
            {
                "session_id": "some-subagent",
                "cwd": "/tmp/nowhere",
                "hook_event_name": "SubagentStart",
                "agent_id": "x",
            }
        )
        self.assertEqual(vis.table.all(), [])

    def test_a_subagent_event_finds_its_parent_by_working_directory(self):
        vis = self.visualizer()
        vis.handle_event(
            {"session_id": "parent", "cwd": "/tmp/p", "hook_event_name": "SessionStart"}
        )
        vis.handle_event(
            {
                "session_id": "its-own-id",
                "cwd": "/tmp/p",
                "hook_event_name": "SubagentStart",
                "agent_id": "x",
            }
        )
        self.assertEqual(len(vis.table.all()), 1)
        self.assertEqual(vis.table.get("parent").subagents, 1)

    def test_the_parents_own_session_id_still_works(self):
        vis = self.visualizer()
        vis.handle_event({"session_id": "parent", "hook_event_name": "SessionStart"})
        vis.handle_event(
            {
                "session_id": "parent",
                "hook_event_name": "SubagentStart",
                "agent_id": "x",
            }
        )
        self.assertEqual(vis.table.get("parent").subagents, 1)

    def test_hook_drift_is_reported(self):
        """The bug that hid all of this: an event the code handles but that
        nobody installed is completely silent."""
        import install_hooks

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            hooks = install_hooks.build_hooks("http://x/event", False)
            del hooks["SubagentStart"]
            path.write_text(json.dumps({"hooks": hooks}))
            self.assertEqual(install_hooks.missing_events(path), ["SubagentStart"])

            path.write_text(json.dumps({"hooks": install_hooks.build_hooks("u", False)}))
            self.assertEqual(install_hooks.missing_events(path), [])

    def test_settings_with_none_of_our_hooks_is_not_drift(self):
        import install_hooks

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{}]}]}}))
            self.assertEqual(install_hooks.missing_events(path), [])


if __name__ == "__main__":
    unittest.main()
