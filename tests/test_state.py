"""python3 -m unittest discover tests"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import board, config, context, font, twister  # noqa: E402
from mft.render import Cell, attention_debt, render  # noqa: E402
from mft.state import SessionTable, apply_event, classify_notification  # noqa: E402


def event(name: str, **kw) -> dict:
    return {"hook_event_name": name, "session_id": "s", **kw}


def set_subagents(session, count: int) -> None:
    """Put `count` subagents in flight, for the board tests.

    `Session.subagents` is derived from the identifiers of the subagents in
    flight rather than being a number anyone can set, so the tests that only
    care about how many dots appear go through here.
    """
    session.subagents_in_flight = {f"agent:{i}" for i in range(count)}


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
        self.assertEqual(list(self.session.tool_history), ["Read", "Bash"])

    def test_tool_history_is_bounded(self):
        self.feed(*[event("PostToolUse", tool_name="Grep")] * 50)
        self.assertEqual(len(self.session.tool_history), config.PEEK_HISTORY)

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
        session.tool_history.extend(["Read", "Bash"])
        session.arc, session.tool_calls, session.context_tokens = 5, 9, 120_000
        session.subagents_in_flight.add("agent:x")
        session.alert = True
        session.attention_since = 0.0

        self.end(vis, "old", "clear")
        self.assertEqual(list(session.tool_history), [])
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
                o for o in vis._live_overlays(0.0) if isinstance(o, board.ClearOverlay)
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
        self.assertTrue(any(isinstance(o, board.ClearOverlay) for o in live))
        self.assertFalse(
            any(isinstance(o, board.SpawnOverlay) for o in live),
            "nothing claimed an encoder here; an agent on one forgot everything",
        )

    def test_a_resume_is_still_an_arrival(self):
        vis = self.visualizer()
        self.start(vis, "s", source="resume")
        self.assertTrue(
            any(isinstance(o, board.SpawnOverlay) for o in vis._live_overlays(0.0))
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
        """One vocabulary, not three. A state with a colour but no rank sorts
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

    def test_a_working_ring_is_a_context_gauge(self):
        self.session.state = "working"
        self.session.context_limit = 200_000
        self.session.context_tokens = 100_000
        half = render(self.session, 0.0).ring
        self.session.context_tokens = 190_000
        nearly_full = render(self.session, 0.0).ring
        self.assertAlmostEqual(half, 63, delta=1)
        self.assertGreater(nearly_full, half)
        self.assertLessEqual(nearly_full, 127)

    def test_a_full_context_does_not_overflow_the_ring(self):
        self.session.state = "working"
        self.session.context_limit = 200_000
        self.session.context_tokens = 999_000
        self.assertEqual(render(self.session, 0.0).ring, 127)

    def test_working_falls_back_to_the_arc_without_a_reading(self):
        self.session.state = "working"
        self.session.arc = 4
        self.assertIsNone(self.session.context_fraction)
        self.assertEqual(render(self.session, 0.0).ring, int(127 * 4 / config.ARC_SEGMENTS))

    def test_unsupervised_sessions_get_their_own_colour(self):
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
        overlay = board.ShutdownOverlay(0.0)
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
        lamp = board.LampTestOverlay(t0)
        lamp.dismiss(t0 + 0.5)
        compaction = board.CompactOverlay(session, t0)
        compaction.finish(t0 + 1.0)
        return [
            board.TextOverlay(config.BOOT_WORD, t0),
            board.TextOverlay("RATE", t0, color=config.BANNER_COLOR),
            lamp,
            board.ShutdownOverlay(t0),
            board.SpawnOverlay(session, t0),
            board.ClearOverlay(session, t0),
            compaction,
            board.PeekOverlay(session, t0),
        ]

    def test_no_overlay_ever_paints_outside_the_hardware(self):
        """Every one of these is an eased ramp of an elapsed time, and several
        get their start or end from an event that arrives mid-flight. Asked for
        a frame outside its own window, an overlay should land on the end of its
        ramp -- not compute a negative fill and hand it to the wire."""
        table = SessionTable()
        session = table.ensure("s", "/tmp/p")
        session.tool_history.extend(["Read", "Bash", "Grep"])
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
        session.tool_history.append("Read")
        overlays = self._every_overlay(session, 0.0)
        for step in range(200):
            board.compose(
                table.all(),
                step * 0.02,
                overlays,
                slot_count=config.ENCODERS_PER_BANK,
            )

    def test_boot_animation_spells_something_then_ends(self):
        overlay = board.TextOverlay("CLAUDE", 0.0)
        mid = board.compose([], overlay.duration / 2, [overlay])
        self.assertTrue(any(c.brightness > 0 for c in mid[: config.ENCODERS_PER_BANK]))
        self.assertFalse(overlay.done(overlay.duration - 0.01))
        self.assertTrue(overlay.done(overlay.duration))

    def test_the_boot_word_is_colourless_all_the_way_through(self):
        # The word is the one thing on this device that is text rather than
        # status. Colour is how everything else here means something, so the
        # letters stay out of that vocabulary entirely -- and on this hardware
        # that means no hue at all rather than a white one. The RGB channel is a
        # wheel with no achromatic value anywhere on it (it wraps; 48 and 127
        # are both green), so a colour of None is the only white there is: the
        # ring draws the glyph and the RGB stays dark.
        #
        # Brightness is the letter's own decay and is not asserted here; that is
        # test_each_boot_letter_strikes_full_then_decays_to_dark's business.
        overlay = board.TextOverlay(config.BOOT_WORD, 0.0)
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

    def test_a_colourless_letter_still_lights_the_ring_and_only_the_ring(self):
        # The pixel that carries the glyph, as the hardware sees it: ring at
        # full, RGB extinguished. Both halves matter -- a lit ring is the white
        # block, and any hue underneath it would tint the letter.
        overlay = board.TextOverlay("C", 0.0, color=None)
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

    def test_a_banner_still_gets_to_be_a_colour(self):
        # Colourless is the boot word's choice, not a property of TextOverlay:
        # a banner shouting RATE at you is status, and status is a hue.
        overlay = board.TextOverlay("RATE", 0.0, color="red")
        frame = board.compose([], 0.0, [overlay])[: config.ENCODERS_PER_BANK]
        lit = [c for c in frame if c.brightness > 0]
        self.assertTrue(lit)
        self.assertEqual({c.color for c in lit}, {"red"})

    def test_nothing_in_the_boot_sequence_lights_the_rgb(self):
        # The invariant, rather than the three places that happened to break it.
        # Boot is the word, then the lamp test -- and underneath both of them,
        # ambient, which only shows through where whatever is on top is dimmer.
        # That last one is how the blue got out: it is painted by compose() and
        # not by any overlay, so testing the overlays alone would miss it.
        # Every frame, every slot, all the way through: no hue.
        word = board.TextOverlay(config.BOOT_WORD, 0.0)
        lamp = board.LampTestOverlay(word.duration)
        t = 0.0
        end = word.duration + lamp.duration
        while t < end:
            overlays = [word] if t < word.duration else [lamp]
            for cell in board.compose([], t, overlays):
                self.assertIsNone(cell.color, t)
            t += 0.25
        # And the idle board it settles onto once the lamp test has expired,
        # which is what you are left looking at.
        for cell in board.compose([], end + 1.0, []):
            self.assertIsNone(cell.color)

    def test_the_boot_sequence_switches_the_rgb_off_on_the_wire(self):
        # The board saying "no colour" is only half of it. This is the other
        # half: what channel 3 actually carries for all 64 encoders, every
        # frame, from clear_all through the word and the lamp test.
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
        word = board.TextOverlay(config.BOOT_WORD, 0.0)
        lamp = board.LampTestOverlay(word.duration)
        t = 0.0
        while t < word.duration + 5.0:
            overlays = [word] if t < word.duration else [lamp]
            for slot, cell in enumerate(board.compose([], t, overlays)):
                recorder.write(slot, cell)
            t += 1.0 / config.FPS

        rgb = {v for ch, _, v in recorder.sent if ch == config.CH_SWITCH_ANIM}
        self.assertEqual(rgb, {config.DARK_VALUE})
        # And spelled out, so a future edit to DARK_VALUE has to face it: the
        # off value is above the pulse band, not at the bottom of it.
        self.assertEqual(config.DARK_VALUE, 18)
        self.assertGreater(config.DARK_VALUE, max(config.ANIM_PULSE.values()))
        self.assertNotIn(config.DARK_VALUE, set(config.ANIM_GATE.values()))

    def test_switching_the_rgb_off_dims_before_it_recolours(self):
        # At startup channel 3 is at whatever it was before we opened the port,
        # so a hue sent first lands on a lit LED and flashes it. The off has to
        # go first.
        recorder = _RecordingTwister()
        recorder.rgb_off(0)
        channels = [ch for ch, _, _ in recorder.sent]
        self.assertEqual(channels, [config.CH_SWITCH_ANIM, config.CH_SWITCH])

    def test_the_lamp_test_still_lights_every_ring(self):
        # Colourless is not the same as absent. The sweep is a lamp test in the
        # aircraft sense -- every ring reaches full, or a dead LED has somewhere
        # to hide -- and that has to survive losing the hue.
        lamp = board.LampTestOverlay(0.0)
        seen = set()
        t = 0.0
        while t < config.LAMP_TEST_SWEEP_SECONDS * 1.5:
            frame = board.compose([], t, [lamp])[: config.ENCODERS_PER_BANK]
            seen |= {i for i, c in enumerate(frame) if c.brightness > 0.9}
            t += 1.0 / config.FPS
        self.assertEqual(seen, set(range(config.ENCODERS_PER_BANK)))

    def test_each_boot_letter_strikes_full_then_decays_to_its_successor(self):
        # A letter is at full the instant it appears, and by the time the next
        # one strikes the only pixels still lit are the ones that letter is
        # about to light anyway -- those hold rather than blinking off and
        # straight back on. Everything else is gone.
        overlay = board.TextOverlay(config.BOOT_WORD, 0.0)
        for index, char in enumerate(config.BOOT_WORD):
            start = board.compose([], index * overlay.step + 0.001, [overlay])
            end = board.compose([], (index + 1) * overlay.step - 0.001, [overlay])
            lit = [c.brightness for c in start[: config.ENCODERS_PER_BANK] if c.brightness > 0]
            self.assertTrue(lit, f"letter {index} should strike")
            self.assertAlmostEqual(max(lit), 1.0, places=2, msg="strikes at full")

            glyph = font.pixels(char)
            nxt = (
                font.pixels(config.BOOT_WORD[index + 1])
                if index + 1 < len(config.BOOT_WORD)
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
        # CLAUDE flickered through full darkness five times on the way past.
        overlay = board.TextOverlay("CLAUDE", 0.0)
        for index in range(len(overlay.text) - 1):
            boundary = (index + 1) * overlay.step - 0.001
            frame = board.compose([], boundary, [overlay])
            self.assertTrue(
                any(c.brightness > 0.02 for c in frame[: config.ENCODERS_PER_BANK]),
                f"board went black between letter {index} and {index + 1}",
            )

    def test_boot_letters_are_white(self):
        # No hue at all: the RGB switch is left off and the word is spelled in
        # the encoder rings, which are the only white light on the device.
        overlay = board.TextOverlay(config.BOOT_WORD, 0.0)
        frame = board.compose([], overlay.step * 0.1, [overlay])
        lit = [c for c in frame[: config.ENCODERS_PER_BANK] if c.brightness > 0]
        self.assertTrue(lit)
        self.assertTrue(all(c.color is None and c.ring == 127 for c in lit))

    def test_boot_is_unhurried_and_shutdown_is_not(self):
        # Boot is the only time the device speaks in words, and a word you can
        # only catch the tail of may as well not be spelled. Shutdown has
        # something waiting on it (`--stop` gives the daemon five seconds), so
        # it says "on purpose" without spelling anything.
        boot = board.TextOverlay(config.BOOT_WORD, 0.0)
        shutdown = board.ShutdownOverlay(0.0)
        self.assertGreater(boot.duration, 4.0, "CLAUDE should be legible, not a blink")
        self.assertLess(shutdown.duration, boot.duration)

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

        overlay = board.ShutdownOverlay(0.0)
        first = board.compose([], 0.15, [overlay])
        self.assertGreater(first[path[0]].brightness, 0.0)
        self.assertEqual(first[path[-1]].brightness, 0.0)
        filled = board.compose([], overlay.spiral, [overlay])
        self.assertTrue(all(c.brightness > 0.95 for c in filled[:16]))

    def test_shutdown_dims_in_unison_on_one_hue_then_goes_dark(self):
        overlay = board.ShutdownOverlay(0.0)
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
        # The colour does nothing on the way out -- only the lamp goes down.
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

    def test_shutdown_is_slower_than_before_but_still_beats_the_stop_timeout(self):
        # `--stop` gives the daemon five seconds to clear the LEDs and let go of
        # the MIDI port; the animation has to leave room for both.
        overlay = board.ShutdownOverlay(0.0)
        self.assertGreater(overlay.duration, 3.0)
        self.assertLess(overlay.duration, 4.5)

    def test_lamp_test_lights_every_led_before_it_gets_generative(self):
        # The aircraft half of the gesture: a dead LED has to have nowhere to
        # hide, so every ring goes fully lit during the opening sweep.
        overlay = board.LampTestOverlay(0.0)
        peak = [0.0] * config.ENCODERS_PER_BANK
        t = 0.0
        while t <= config.LAMP_TEST_SWEEP_SECONDS:
            cells = board.compose([], t, [overlay])
            for slot in range(config.ENCODERS_PER_BANK):
                peak[slot] = max(peak[slot], cells[slot].brightness)
            t += 0.02
        self.assertTrue(all(level > 0.95 for level in peak), peak)

    def test_lamp_test_keeps_moving_and_never_repeats(self):
        overlay = board.LampTestOverlay(0.0)
        frames = [
            tuple(
                (c.ring, c.color)
                for c in board.compose([], t, [overlay])[: config.ENCODERS_PER_BANK]
            )
            for t in (5.0, 5.5, 9.0, 17.0, 31.0)
        ]
        self.assertEqual(len(set(frames)), len(frames), "the field is not generative")

    def test_lamp_test_fades_out_over_a_minute_on_its_own(self):
        overlay = board.LampTestOverlay(0.0)

        def brightest(t):
            cells = board.compose([], t, [overlay])[: config.ENCODERS_PER_BANK]
            return max(c.brightness for c in cells)

        self.assertGreater(brightest(6.0), 0.5)
        self.assertLess(brightest(50.0), brightest(6.0))
        self.assertFalse(overlay.done(config.LAMP_TEST_SECONDS - 0.01))
        self.assertTrue(overlay.done(config.LAMP_TEST_SECONDS))

    def test_lamp_test_gets_out_of_the_way_when_a_session_shows_up(self):
        overlay = board.LampTestOverlay(0.0)
        overlay.dismiss(10.0)
        self.assertFalse(overlay.done(10.0 + config.LAMP_TEST_DISMISS_SECONDS / 2))
        self.assertTrue(overlay.done(10.0 + config.LAMP_TEST_DISMISS_SECONDS))
        # ...and it retreats rather than hard-cutting, so the first encoder to
        # light reads as emerging from the field.
        lit = board.compose([], 10.1, [overlay])[: config.ENCODERS_PER_BANK]
        self.assertTrue(any(c.brightness > 0 for c in lit))

    def test_lamp_test_never_dims_a_live_session(self):
        table = SessionTable()
        session = table.ensure("s", "/tmp/p")
        session.state = "waiting"
        overlay = board.LampTestOverlay(0.0)
        overlay.dismiss(4.0)
        bare = board.compose(table.all(), 4.1)[session.slot]
        over = board.compose(table.all(), 4.1, [overlay])[session.slot]
        self.assertEqual(bare, over)

    def test_spawn_strikes_bright_then_settles_into_the_session(self):
        table = SessionTable()
        session = table.ensure("s", "/tmp/p")
        session.state = "idle"
        overlay = board.SpawnOverlay(session, 0.0)

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
        overlay = board.SpawnOverlay(session, 0.0)
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
        # of each blink: the ring blinks, the colour does not.
        self.assertTrue(all(c.color == config.SPAWN_COLOR for c in flashing))
        self.assertTrue(all(abs(c.brightness - 1.0) < 1e-6 for c in flashing))

        self.assertFalse(overlay.done(config.SPAWN_SECONDS - 0.01))
        self.assertTrue(overlay.done(config.SPAWN_SECONDS))

    def test_spawn_follows_its_session_when_the_board_compacts(self):
        table = SessionTable()
        first, second = table.ensure("a", "/tmp/a"), table.ensure("b", "/tmp/b")
        overlay = board.SpawnOverlay(second, 0.0)
        table.release(first)  # second slides up to slot 0 mid-flash
        cells = board.compose(table.all(), 0.2, [overlay])
        self.assertAlmostEqual(cells[second.slot].brightness, 1.0, places=5)

    def test_starting_a_session_flashes_its_encoder(self):
        from mft.daemon import Visualizer
        from mft.twister import NullTwister

        vis = Visualizer(NullTwister())
        vis.handle_event({"session_id": "s", "hook_event_name": "SessionStart"})
        self.assertTrue(
            any(isinstance(o, board.SpawnOverlay) for o in vis._live_overlays(0.0))
        )

    def test_compaction_drains_then_refills(self):
        overlay = board.CompactOverlay(SessionTable().ensure("s"), 0.0)
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
        overlay = board.ClearOverlay(session, 0.0)

        strike = board.compose([session], 0.0, [overlay])[session.slot]
        self.assertEqual(strike.ring, 127)
        # White, like the boot word: the RGB stays dark, because this is the
        # board saying something rather than reporting a status.
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
        overlay = board.CompactOverlay(SessionTable().ensure("s"), 0.0)
        self.assertFalse(overlay.done(config.COMPACT_TIMEOUT_SECONDS - 1))
        self.assertTrue(overlay.done(config.COMPACT_TIMEOUT_SECONDS + 1))

    def test_peek_paints_tool_history_across_the_bank(self):
        session = SessionTable().ensure("s", "/tmp/p")
        session.tool_history.extend(["Read", "Bash", "Edit"])
        overlay = board.PeekOverlay(session, 0.0)
        early = board.compose([session], config.HOLD_SECONDS / 2, [overlay])
        self.assertIsNone(early[1].color, "a tap should not flash the detail view")
        cells = board.compose([session], config.HOLD_SECONDS + 1, [overlay])
        lit = [c.color for c in cells[: config.ENCODERS_PER_BANK] if c.color]
        self.assertIn(config.TOOL_COLORS["Bash"], lit)
        self.assertIn(config.TOOL_COLORS["Edit"], lit)
        self.assertFalse(overlay.done(1.0))
        overlay.release()
        self.assertTrue(overlay.done(1.0))


class EncoderTurns(unittest.TestCase):
    """A knob is not a control. Turning one changes nothing about the session
    and is undone; all it can do is wake a sleeping board."""

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

    def test_a_turn_wakes_a_sleeping_board(self):
        """The one thing a turn does, and the only gesture that does *only*
        this: a hand on the hardware is the one activity sleep can see that has
        nothing to do with any agent, and a dark board is the likeliest reason
        for the hand. A press would wake it too, but a press on a claimed
        encoder also goes and focuses that terminal."""
        vis = self.visualizer()
        vis._sleep.last_activity = time.monotonic() - 2 * config.SLEEP_DARK_SECONDS
        self.assertEqual(vis._sleep.gain(time.monotonic()), 0.0)
        vis.on_midi(
            SimpleNamespace(
                type="control_change", channel=config.CH_ENCODER, control=0, value=65
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
