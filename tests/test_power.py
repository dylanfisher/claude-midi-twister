"""Sleep and wake: the clock arithmetic, and the cache invalidation around it.

The IOKit half cannot be tested without a machine that will actually suspend
for you, so what is pinned here is everything that decides *what happens* once
something reports a sleep -- which is the part that can be silently wrong. See
the module docstring in `mft.power` for why there are two detectors at all.
"""

from __future__ import annotations

import unittest

from mft import config, power, twister


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


if __name__ == "__main__":
    unittest.main()
