"""Sleep and wake: the clock arithmetic, and the cache invalidation around it.

The IOKit half cannot be tested without a machine that will actually suspend
for you, so what is pinned here is everything that decides *what happens* once
something reports a sleep -- which is the part that can be silently wrong. See
the module docstring in `mft.power` for why there are two detectors at all.
"""

from __future__ import annotations

import time
import unittest

from mft import config, power, twister
from mft import daemon as daemon_mod


class WakeClockTest(unittest.TestCase):
    """The fallback detector, driven by a reader that lies on demand."""

    def setUp(self) -> None:
        self.slept = 0.0
        self.clock = power.WakeClock(read=lambda: self.slept)

    def test_a_machine_that_never_sleeps_never_reports_a_wake(self) -> None:
        for _ in range(5):
            self.assertEqual(self.clock.poll(), 0.0)

    def test_a_suspend_is_reported_once(self) -> None:
        self.slept += 3600.0
        self.assertEqual(self.clock.poll(), 3600.0)
        # The gap does not close again when the machine wakes -- it is
        # cumulative since boot -- so a detector that compared against anything
        # but the last reading would report the same wake every frame forever.
        self.assertEqual(self.clock.poll(), 0.0)

    def test_scheduling_noise_is_not_a_suspend(self) -> None:
        self.slept += 0.05
        self.assertEqual(self.clock.poll(minimum=2.0), 0.0)
        # And it is not *lost* either: the reading still moved, so a real sleep
        # right after it reports its own length rather than that plus the noise.
        self.slept += 60.0
        self.assertEqual(self.clock.poll(minimum=2.0), 60.0)

    def test_no_clocks_means_no_wakes_rather_than_a_crash(self) -> None:
        clock = power.WakeClock(read=lambda: None)
        self.assertEqual(clock.poll(), 0.0)

    def test_clocks_that_appear_late_do_not_report_the_whole_uptime(self) -> None:
        readings = [None, 5000.0, 5000.0]
        clock = power.WakeClock(read=lambda: readings.pop(0))
        # First real reading is a baseline, not a five-thousand-second sleep.
        self.assertEqual(clock.poll(), 0.0)
        self.assertEqual(clock.poll(), 0.0)

    def test_the_real_reader_is_either_a_number_or_nothing(self) -> None:
        slept = power.slept_since_boot()
        if slept is not None:
            # A machine cannot have slept a negative amount, and the value is
            # meaningless in absolute terms -- only its changes matter.
            self.assertGreaterEqual(slept, 0.0)


class DisplayPowerTest(unittest.TestCase):
    """The detector that decides whether a wake is a wake.

    Its whole job is the dark wake: a Mac in standby wakes for maintenance every
    fifteen minutes, and both of the other detectors call that a person opening
    the lid. The screen is what says otherwise.
    """

    def setUp(self) -> None:
        self.asleep = False
        self.display = power.DisplayPower(read=lambda: self.asleep)

    def test_a_screen_that_stays_on_reports_nothing(self) -> None:
        for _ in range(5):
            self.assertIsNone(self.display.poll())
        self.assertFalse(self.display.asleep)

    def test_each_edge_is_reported_once(self) -> None:
        self.asleep = True
        self.assertIs(self.display.poll(), True)
        self.assertIsNone(self.display.poll())
        self.asleep = False
        self.assertIs(self.display.poll(), False)
        self.assertIsNone(self.display.poll())

    def test_a_screen_already_off_at_boot_is_an_edge(self) -> None:
        # Awake until told otherwise, so a daemon that starts onto a dark desk
        # darkens on its first poll rather than waiting for a screen to come on
        # and go off again.
        display = power.DisplayPower(read=lambda: True)
        self.assertIs(display.poll(), True)

    def test_a_display_we_cannot_ask_is_always_awake(self) -> None:
        # The degradation that matters: no readings must mean the board keeps
        # working, never that it sits dark waiting for a screen to report in.
        display = power.DisplayPower(read=lambda: None)
        self.assertIsNone(display.poll())
        self.assertFalse(display.asleep)

    def test_a_reader_that_goes_away_holds_the_last_state(self) -> None:
        readings = [True, None, None, False]
        display = power.DisplayPower(read=lambda: readings.pop(0))
        self.assertIs(display.poll(), True)
        self.assertIsNone(display.poll())
        self.assertIsNone(display.poll())
        # Still asleep through the gap, so the board stays dark rather than
        # relighting because the question stopped being answerable.
        self.assertTrue(display.asleep)
        self.assertIs(display.poll(), False)

    def test_the_real_reader_is_either_a_bool_or_nothing(self) -> None:
        self.assertIn(power.display_asleep(), (True, False, None))


class ForgetAllTest(unittest.TestCase):
    """The de-dup cache after something else changed the board.

    A blackout for sleep, or a port that went away and came back, leaves the
    hardware dark and the cache still believing it holds a lit board -- at which
    point the de-dup suppresses exactly the writes that would fix it. This is
    that bug, in the form it would actually take.
    """

    def setUp(self) -> None:
        self.device = twister.NullTwister()
        self.sent: list[tuple[int, int, int]] = []
        real = self.device.cc

        def record(channel, control, value, force=False):
            before = dict(self.device._last)
            real(channel, control, value, force=force)
            if self.device._last != before or force:
                self.sent.append((channel, control, value))

        self.device.cc = record

    def test_a_repeated_value_is_written_once(self) -> None:
        self.device.cc(1, 0, 42)
        self.device.cc(1, 0, 42)
        self.assertEqual(len(self.sent), 1)

    def test_forgetting_restates_it(self) -> None:
        self.device.cc(1, 0, 42)
        self.device.forget_all()
        self.device.cc(1, 0, 42)
        self.assertEqual(len(self.sent), 2)

    def test_a_blackout_leaves_nothing_the_next_frame_can_skip(self) -> None:
        # What a lit board plus a sleep plus a wake actually looks like.
        self.device.cc(config.CH_SWITCH, 3, 60)
        self.device.blackout()
        self.device.forget_all()
        self.sent.clear()
        self.device.cc(config.CH_SWITCH, 3, 60)
        self.assertEqual(self.sent, [(config.CH_SWITCH, 3, 60)])

    def test_a_device_that_never_had_a_port_cannot_be_reopened(self) -> None:
        # NullTwister is what `--no-device` runs, and the port watchdog polls
        # every frame: reopening has to be a cheap, honest no.
        self.assertFalse(self.device.failing())
        self.assertFalse(self.device.reopen())


class DarkWakeTest(unittest.TestCase):
    """The bug this all exists for, at the level it actually bit.

    Nine consecutive suspends, one `will sleep` notification between them, and a
    board that was lit for all of it. What follows is that sequence: the clock
    reports each maintenance wake, and the screen -- still off -- is what keeps
    the board from believing it.
    """

    def setUp(self) -> None:
        self.vis = daemon_mod.Visualizer(twister.NullTwister())
        self.asleep = False
        self.vis._display = power.DisplayPower(read=lambda: self.asleep)
        # Both of these go to the process table, and neither is what is under
        # test here.
        self.vis.upkeep.sweep_census = lambda: None
        self.vis.adopt_running_sessions = lambda awaken=True: None

    def poll_display(self) -> None:
        """One display poll, with the interval taken out of the picture."""
        self.vis._last_display_poll = float("-inf")
        self.vis._check_display(time.monotonic())

    def wake(self, source: str) -> None:
        """One reported wake, with the debounce taken out of the picture."""
        self.vis._last_system_wake = float("-inf")
        self.vis.on_system_wake(source=source)

    def test_a_dark_screen_darkens_the_board(self) -> None:
        self.asleep = True
        self.poll_display()
        self.assertTrue(self.vis._suspended.is_set())

    def test_a_maintenance_wake_does_not_relight_a_dark_screen(self) -> None:
        self.asleep = True
        self.poll_display()
        for _ in range(9):
            # What the clock fallback did all night, every fifteen minutes.
            self.wake("911s clock gap")
            self.poll_display()
            self.assertTrue(self.vis._suspended.is_set())

    def test_the_screen_coming_on_is_what_relights_it(self) -> None:
        self.asleep = True
        self.poll_display()
        self.asleep = False
        self.poll_display()
        self.assertFalse(self.vis._suspended.is_set())

    def test_a_wake_onto_a_lit_screen_relights_immediately(self) -> None:
        # The lid opened rather than a backup running: the notification and the
        # screen agree, and nothing waits for the next poll.
        self.vis.darken("system sleeping")
        self.asleep = False
        self.wake("notification")
        self.assertFalse(self.vis._suspended.is_set())

    def test_a_darkened_board_composes_no_frames(self) -> None:
        # The blackout is not a paint that happens to be dark -- nothing is
        # deciding what to put on the board at all.
        self.asleep = True
        self.poll_display()
        self.assertFalse(self.vis.paint(time.monotonic()))

    def test_darkening_and_relighting_are_idempotent(self) -> None:
        self.vis.darken("twice")
        self.vis.darken("twice")
        self.assertTrue(self.vis._suspended.is_set())
        self.vis.relight("twice")
        self.vis.relight("twice")
        self.assertFalse(self.vis._suspended.is_set())

    def test_a_wake_always_forgets_the_board(self) -> None:
        # Even one that relights nothing, because a suspend can leave the
        # hardware holding something other than what we last sent it.
        self.vis.device.cc(config.CH_SWITCH, 3, 60)
        self.vis._last_cells = []
        self.wake("notification")
        self.assertIsNone(self.vis._last_cells)
        self.assertEqual(self.vis.device._last, {})


class BoardRefreshTest(unittest.TestCase):
    """The repair for a write the hardware dropped without saying so.

    A wake forgets the cache and repaints, but that repaint goes out while the
    USB device may still be coming back: the endpoint is valid, nothing raises,
    `port_failing` stays false, and the messages are simply not heard. The cache
    then believes a hue it never delivered, and a resting encoder -- which never
    changes value again -- keeps the device's own inactive blue until its
    session next does something. So the board restates itself on a slow clock
    whether or not anything moved.
    """

    def setUp(self) -> None:
        self.vis = daemon_mod.Visualizer(twister.NullTwister())
        self.vis.handle_event(
            {
                "session_id": "resting",
                "cwd": "/tmp/p",
                "hook_event_name": "Stop",
                "terminal": "test",
            }
        )
        self.sent: list[tuple[int, int, int]] = []
        real = self.vis.device.cc

        def record(channel, control, value, force=False):
            before = dict(self.vis.device._last)
            real(channel, control, value, force=force)
            if self.vis.device._last != before or force:
                self.sent.append((channel, control, value))

        self.vis.device.cc = record

    def hues(self) -> list[tuple[int, int, int]]:
        return [msg for msg in self.sent if msg[0] == config.CH_SWITCH]

    def test_a_still_board_is_written_once_between_refreshes(self) -> None:
        now = time.monotonic()
        self.vis.paint(now)
        painted = len(self.hues())
        self.assertTrue(painted, "the first frame states every hue")
        for tick in range(1, 10):
            self.vis.paint(now + tick * (config.BOARD_REFRESH_SECONDS / 20))
        self.assertEqual(len(self.hues()), painted, "the de-dup still holds")

    def test_the_slow_clock_restates_it(self) -> None:
        now = time.monotonic()
        self.vis.paint(now)
        painted = len(self.hues())
        self.vis.paint(now + config.BOARD_REFRESH_SECONDS + 0.01)
        self.assertEqual(len(self.hues()), painted * 2)

    def test_the_ring_refresh_survives_a_board_refresh(self) -> None:
        # The two clocks share a frame every so often, and the fast one must not
        # be starved by the slow one resetting it.
        now = time.monotonic()
        self.vis.paint(now)
        self.vis.paint(now + config.BOARD_REFRESH_SECONDS + 0.01)
        rings = len([msg for msg in self.sent if msg[0] == config.CH_ENCODER])
        self.vis.paint(
            now + config.BOARD_REFRESH_SECONDS + config.RING_REFRESH_SECONDS + 0.02
        )
        self.assertGreater(
            len([msg for msg in self.sent if msg[0] == config.CH_ENCODER]), rings
        )


if __name__ == "__main__":
    unittest.main()
