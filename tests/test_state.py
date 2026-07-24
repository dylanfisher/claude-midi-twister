"""python3 -m unittest discover tests"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import board, config, context  # noqa: E402
from mft.render import attention_debt, render  # noqa: E402
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


class Rendering(unittest.TestCase):
    def setUp(self):
        self.session = SessionTable().ensure("s", "/tmp/p")

    def test_every_state_renders_in_range(self):
        for state in config.STATE_COLORS:
            self.session.state = state
            cell = render(self.session, 12.5)
            self.assertTrue(0 <= cell.ring <= 127, state)
            self.assertTrue(0.0 <= cell.brightness <= 1.0, state)

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

    def test_snoozed_session_is_dim_and_still(self):
        self.session.state = "permission"
        self.session.snoozed_until = __import__("time").monotonic() + 60
        cell = render(self.session, 3.0)
        self.assertEqual(cell.rgb_anim, config.ANIM_NONE)
        self.assertLessEqual(cell.brightness, config.IDLE_BRIGHTNESS)

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

    def test_a_snoozed_parent_spawns_nothing(self):
        parent = self.table.ensure("a", "/tmp/p")
        parent.state = "working"
        set_subagents(parent, 4)
        parent.snoozed_until = __import__("time").monotonic() + 60
        self.assertEqual(self.subagent_slots(board.compose(self.table.all(), 1.0)), [])

    def test_an_empty_board_breathes_rather_than_going_dark(self):
        lit = [c for c in board.compose([], 1.0) if c.brightness > 0]
        self.assertTrue(lit)


class Overlays(unittest.TestCase):
    def test_boot_animation_spells_something_then_ends(self):
        overlay = board.TextOverlay("CLAUDE", 0.0)
        mid = board.compose([], overlay.duration / 2, [overlay])
        self.assertTrue(any(c.brightness > 0 for c in mid[: config.ENCODERS_PER_BANK]))
        self.assertFalse(overlay.done(overlay.duration - 0.01))
        self.assertTrue(overlay.done(overlay.duration))

    def test_each_boot_letter_strikes_full_then_decays_to_dark(self):
        # The gap is what separates one letter from the next: a letter is at
        # full the instant it appears and at nothing by the time the next one
        # strikes, so two glyphs are never on the board together.
        overlay = board.TextOverlay(config.BOOT_WORD, 0.0)
        for index in range(len(config.BOOT_WORD)):
            start = board.compose([], index * overlay.step + 0.001, [overlay])
            end = board.compose([], (index + 1) * overlay.step - 0.001, [overlay])
            lit = [c.brightness for c in start[: config.ENCODERS_PER_BANK] if c.brightness > 0]
            self.assertTrue(lit, f"letter {index} should strike")
            self.assertAlmostEqual(max(lit), 1.0, places=2, msg="strikes at full")
            self.assertTrue(
                all(c.brightness < 0.02 for c in end[: config.ENCODERS_PER_BANK]),
                "and is gone before the next letter",
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

    def test_shutdown_cycles_the_wheel_in_unison_then_goes_dark(self):
        overlay = board.ShutdownOverlay(0.0)
        seen = set()
        t = overlay.spiral
        while t < overlay.spiral + overlay.cycle:
            cells = board.compose([], t, [overlay])[: config.ENCODERS_PER_BANK]
            # Every encoder wears the same hue at the same brightness: sixteen
            # knobs as one object is the whole point of the cycle.
            self.assertEqual(len({c.color for c in cells}), 1, t)
            self.assertEqual(len({round(c.brightness, 6) for c in cells}), 1, t)
            seen.add(cells[0].color)
            t += 0.05
        # A full trip round the wheel, not a wobble in one corner of it.
        self.assertLess(min(seen), 8)
        self.assertGreater(max(seen), 119)

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

    def test_spawn_is_brief_and_moves_the_whole_way(self):
        session = SessionTable().ensure("s", "/tmp/p")
        overlay = board.SpawnOverlay(session, 0.0)
        self.assertLess(config.SPAWN_SECONDS, 2.0, "punctuation, not a status")
        rings = [
            board.compose([session], t / 100, [overlay])[session.slot].ring
            for t in range(int(config.SPAWN_SECONDS * 100))
        ]
        self.assertGreater(len(set(rings)), 20, "the ring should actually travel")
        # One sweep out to full, then back down onto the session's own ring --
        # never a lap. A ring that laps itself is how activity reads on this
        # board, and a session starting is one event, not an activity.
        peak = rings.index(max(rings))
        self.assertGreaterEqual(max(rings), 126)
        self.assertEqual(rings[:peak], sorted(rings[:peak]))
        self.assertEqual(rings[peak:], sorted(rings[peak:], reverse=True))
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
    def test_relative_encoding(self):
        from mft.daemon import turn_delta

        self.assertEqual(turn_delta(None, 65), 1)
        self.assertEqual(turn_delta(None, 63), -1)

    def test_absolute_encoding_needs_a_previous_value(self):
        from mft.daemon import turn_delta

        self.assertEqual(turn_delta(None, 40), 0)
        self.assertEqual(turn_delta(40, 44), 4)


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
