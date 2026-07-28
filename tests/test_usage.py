"""The five-hour window: what gets said, when, and the once.

The reading itself is a JSON file somebody else writes, so what is pinned here
is everything that decides whether the board says anything -- the milestone
arithmetic, the watermark that keeps it to one announcement per crossing, and
the two silences (a cold start, a window rolling over). See `mft.usage` for why
both of those have to be silent.

Then what the announcement looks like, which is `mft.overlays.UsageOverlay`:
the word held still, and the reading as rows filling the bank from the bottom.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from mft import board, config, overlays, usage


class CrossedTest(unittest.TestCase):
    def test_nothing_crossed_says_nothing(self) -> None:
        self.assertIsNone(usage.crossed(30.0, 44.0))

    def test_the_milestone_between_two_readings(self) -> None:
        self.assertEqual(usage.crossed(44.0, 51.0), 50)

    def test_landing_exactly_on_one_counts_as_crossing_it(self) -> None:
        self.assertEqual(usage.crossed(44.0, 50.0), 50)

    def test_a_jump_spells_only_the_highest(self) -> None:
        # A daemon that was asleep through half a window comes back to this.
        self.assertEqual(usage.crossed(20.0, 96.0), 95)

    def test_a_milestone_already_below_the_watermark_is_not_recrossed(self) -> None:
        self.assertIsNone(usage.crossed(50.0, 50.0))


class BannerColorTest(unittest.TestCase):
    def test_the_early_ones_are_plain_light(self) -> None:
        self.assertEqual(usage.banner_color(25), config.TEXT_COLOR)
        self.assertEqual(usage.banner_color(50), config.TEXT_COLOR)

    def test_the_bands_climb_with_the_trouble(self) -> None:
        self.assertEqual(usage.banner_color(75), "amber")
        self.assertEqual(usage.banner_color(90), "orange")
        self.assertEqual(usage.banner_color(95), "red")
        self.assertEqual(usage.banner_color(100), "red")


class OverlayTest(unittest.TestCase):
    """What the announcement looks like: the word, then the number as rows.

    The word half is `TextOverlay`'s, and pinned in `tests/test_state.py`. What
    is pinned here is the two things this overlay adds -- letters that do not
    fade, and a bar that reads bottom-up.
    """

    def frame(self, overlay: overlays.UsageOverlay, at: float):
        return board.compose([], at, [overlay])[: config.ENCODERS_PER_BANK]

    def rows(self, frame) -> list[list]:
        """One frame as four rows of cells, bottom row first."""
        cols = config.GRID_COLS
        grid = [frame[r * cols : (r + 1) * cols] for r in range(config.GRID_ROWS)]
        return list(reversed(grid))

    def gauge_at(self, overlay: overlays.UsageOverlay) -> float:
        """A moment inside the first flash's lit half."""
        return overlay.word_duration + overlay.flash * config.USAGE_GAUGE_DUTY / 2

    def test_a_quarter_fills_the_bottom_row_and_nothing_else(self) -> None:
        overlay = overlays.UsageOverlay(25.0, 0.0)
        rows = self.rows(self.frame(overlay, self.gauge_at(overlay)))
        self.assertTrue(all(c.brightness > 0 for c in rows[0]))
        for row in rows[1:]:
            self.assertTrue(all(c.brightness == 0 for c in row))

    def test_a_half_fills_the_bottom_two(self) -> None:
        overlay = overlays.UsageOverlay(50.0, 0.0)
        rows = self.rows(self.frame(overlay, self.gauge_at(overlay)))
        self.assertTrue(all(c.brightness > 0 for c in rows[0] + rows[1]))
        self.assertTrue(all(c.brightness == 0 for c in rows[2] + rows[3]))

    def test_a_full_window_fills_the_whole_bank(self) -> None:
        overlay = overlays.UsageOverlay(100.0, 0.0)
        frame = self.frame(overlay, self.gauge_at(overlay))
        self.assertTrue(all(c.brightness > 0 for c in frame))

    def test_the_leading_row_is_what_tells_ninety_from_a_hundred(self) -> None:
        # 90, 95 and 100 all reach the top row; only how brightly separates
        # them, which is the entire reason the partial fill exists.
        levels = []
        for percent in (90.0, 95.0, 100.0):
            overlay = overlays.UsageOverlay(percent, 0.0)
            rows = self.rows(self.frame(overlay, self.gauge_at(overlay)))
            levels.append(rows[3][0].brightness)
        self.assertLess(levels[0], levels[1])
        self.assertLess(levels[1], levels[2])

    def test_the_hue_ramps_toward_red_with_the_reading(self) -> None:
        low, high = config.USAGE_GAUGE_HUE
        hues = []
        for percent in (25.0, 50.0, 100.0):
            overlay = overlays.UsageOverlay(percent, 0.0)
            frame = self.frame(overlay, self.gauge_at(overlay))
            hues.append(next(c.color for c in frame if c.brightness > 0))
        self.assertEqual(hues, sorted(hues))
        self.assertEqual(hues[-1], high)
        self.assertGreaterEqual(hues[0], low)

    def test_a_filled_row_is_full_however_low_the_reading(self) -> None:
        # The regression that made 32% read as 25% on the hardware: the bar used
        # to be scaled bodily by the reading, so a low one was dim *and* short
        # and its partial row vanished. Height is the quantity now, and the hue
        # is the severity; brightness only marks where the bar stops.
        for percent in (25.0, 32.0, 100.0):
            bottom = self.rows(
                self.frame(o := overlays.UsageOverlay(percent, 0.0), self.gauge_at(o))
            )[0][0]
            self.assertEqual(round(bottom.brightness, 3), 1.0, percent)

    def test_the_leading_row_is_lit_in_proportion_and_never_invisible(self) -> None:
        floor, ceiling = config.USAGE_GAUGE_PARTIAL
        # 32%: one solid row, and a leading row 28% of the way up.
        leading = self.rows(
            self.frame(o := overlays.UsageOverlay(32.0, 0.0), self.gauge_at(o))
        )[1][0]
        self.assertGreater(leading.brightness, floor * 0.9)
        self.assertLess(leading.brightness, ceiling)
        # A sliver of a row is still a lit row -- it is the whole difference
        # between one reading and the next.
        self.assertGreaterEqual(overlays.UsageOverlay.level(0.01), floor)
        self.assertEqual(overlays.UsageOverlay.level(0.0), 0.0)
        self.assertEqual(overlays.UsageOverlay.level(1.0), 1.0)

    def test_the_bar_flashes_and_goes_dark_between(self) -> None:
        overlay = overlays.UsageOverlay(100.0, 0.0)
        dark = overlay.word_duration + overlay.flash * (
            config.USAGE_GAUGE_DUTY + (1 - config.USAGE_GAUGE_DUTY) / 2
        )
        frame = self.frame(overlay, dark)
        # Dark, not absent: the whole bank is still painted, so nothing
        # underneath strobes through the gap between flashes.
        self.assertTrue(all(c.brightness == 0 and c.color is None for c in frame))

    def test_a_letter_holds_full_and_does_not_fade_out(self) -> None:
        # The whole point of the word half. Sample across one letter's slot and
        # assert the glyph is at full for essentially all of it -- the only dip
        # is the cut at the very end, and the letters that follow re-light it.
        overlay = overlays.UsageOverlay(50.0, 0.0)
        step = overlay.word.step
        for at in (0.0, step * 0.25, step * 0.5, step * 0.9):
            frame = self.frame(overlay, at)
            lit = [c for c in frame if c.brightness > 0]
            self.assertTrue(lit, at)
            self.assertEqual({round(c.brightness, 3) for c in lit}, {1.0}, at)

    def test_the_word_comes_first_and_the_bar_after(self) -> None:
        overlay = overlays.UsageOverlay(100.0, 0.0)
        # A full window fills every encoder; no letter in this font does, so the
        # two halves are told apart without asserting on glyph shapes.
        word = self.frame(overlay, overlay.word.duration / 2)
        self.assertTrue(any(c.brightness == 0 for c in word))
        self.assertTrue(all(c.brightness > 0 for c in self.frame(overlay, self.gauge_at(overlay))))
        self.assertFalse(overlay.done(overlay.duration - 0.01))
        self.assertTrue(overlay.done(overlay.duration))

    def test_an_asked_for_reading_starts_on_the_bar(self) -> None:
        # No word, and no silent gap where one would have been: `TextOverlay`
        # floors an empty string to a space, so a naive delegation would spend a
        # whole letter's worth of time showing nothing at all.
        overlay = overlays.UsageOverlay(100.0, 0.0, word="")
        self.assertIsNone(overlay.word)
        self.assertEqual(overlay.word_duration, 0.0)
        self.assertEqual(overlay.duration, overlay.gauge)
        first = self.frame(overlay, overlay.flash * config.USAGE_GAUGE_DUTY / 2)
        self.assertTrue(all(c.brightness > 0 for c in first))

    def test_the_bar_is_clamped_at_both_ends(self) -> None:
        self.assertEqual(overlays.UsageOverlay(-5.0, 0.0).rows(), [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(overlays.UsageOverlay(140.0, 0.0).rows(), [1.0, 1.0, 1.0, 1.0])


class WatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.watcher = usage.UsageWatcher(path="/nonexistent")

    def observe(self, percent: float, resets_at: str = "W1"):
        return self.watcher.observe(usage.Reading(percent, resets_at))

    def test_the_first_reading_is_adopted_silently(self) -> None:
        # Started at 60%: arrived at, not crossed. A restart that shouts is a
        # board you stop reading.
        self.assertIsNone(self.observe(60.0))
        self.assertEqual(self.watcher.watermark, 60.0)

    def test_a_crossing_is_announced_once(self) -> None:
        self.observe(70.0)
        self.assertEqual(self.observe(76.0), 75)
        self.assertIsNone(self.observe(78.0))
        self.assertIsNone(self.observe(80.0))

    def test_jitter_below_a_spelled_milestone_does_not_respell_it(self) -> None:
        self.observe(49.0)
        self.assertEqual(self.observe(51.0), 50)
        self.assertIsNone(self.observe(49.5))
        self.assertIsNone(self.observe(50.5))

    def test_a_new_window_is_silent_and_rearms(self) -> None:
        self.observe(40.0)
        self.assertEqual(self.observe(80.0), 75)
        # The window rolls: a different `resets_at`, and a percentage on the floor.
        self.assertIsNone(self.observe(3.0, resets_at="W2"))
        self.assertEqual(self.watcher.watermark, 3.0)
        self.assertEqual(self.observe(26.0, resets_at="W2"), 25)

    def test_a_big_drop_alone_is_read_as_a_rollover(self) -> None:
        # Some builds cache no `resets_at` at all, so the drop is the only
        # evidence a window turned over.
        self.observe(90.0, resets_at="")
        self.assertIsNone(self.observe(2.0, resets_at=""))
        self.assertEqual(self.observe(51.0, resets_at=""), 50)

    def test_the_payload_says_nothing_until_something_was_read(self) -> None:
        self.assertIsNone(self.watcher.payload())
        self.observe(12.0)
        self.assertEqual(self.watcher.payload()["percent"], 12.0)


class PeekTest(unittest.TestCase):
    """Asking for the reading: the debounce, and that looking says nothing."""

    def setUp(self) -> None:
        self.watcher = usage.UsageWatcher(path="/nonexistent")

    def test_the_first_turn_counts_and_the_rest_of_the_flick_does_not(self) -> None:
        self.assertTrue(self.watcher.request(100.0))
        for detent in range(1, 12):
            self.assertFalse(self.watcher.request(100.0 + detent * 0.03), detent)

    def test_the_grace_alone_ends_quickly(self) -> None:
        # What a request sets on its own is only enough to swallow the rest of
        # the flick -- the readout it is really waiting on hasn't been pushed
        # yet, and if it never is (nothing readable) the knob must not stay dead.
        grace = config.USAGE_PEEK_GRACE_SECONDS
        self.assertTrue(self.watcher.request(100.0))
        self.assertFalse(self.watcher.request(100.0 + grace - 0.01))
        self.assertTrue(self.watcher.request(100.0 + grace))

    def test_the_knob_is_deaf_for_the_answer_and_then_hears_again(self) -> None:
        # The regression: the debounce was a flat five seconds, and when the
        # readout shrank to a third of that it left three seconds of a knob that
        # did nothing. It is the animation that is waited out now, whatever
        # length that animation happens to be.
        grace = config.USAGE_PEEK_GRACE_SECONDS
        self.assertTrue(self.watcher.request(100.0))
        self.watcher.take_request()
        self.watcher.showing(100.0 + 1.26)  # a wordless peek: just the flashes
        self.assertFalse(self.watcher.request(100.0 + 1.26))
        self.assertFalse(self.watcher.request(100.0 + 1.26 + grace - 0.01))
        self.assertTrue(self.watcher.request(100.0 + 1.26 + grace))

    def test_a_longer_readout_is_deaf_for_longer(self) -> None:
        self.watcher.request(100.0)
        self.watcher.showing(100.0 + 3.87)  # the same overlay, with its word
        self.assertFalse(self.watcher.request(103.0))
        self.assertTrue(self.watcher.request(100.0 + 3.87 + config.USAGE_PEEK_GRACE_SECONDS))

    def test_showing_never_shortens_the_silence(self) -> None:
        self.watcher.request(100.0)
        self.watcher.showing(100.0 + 3.0)
        self.watcher.showing(100.0 + 0.1)  # a stale or shorter answer
        self.assertFalse(self.watcher.request(101.0))

    def test_a_refused_turn_leaves_nothing_pending(self) -> None:
        self.watcher.request(100.0)
        self.watcher.take_request()
        self.assertFalse(self.watcher.request(100.1))
        self.assertFalse(self.watcher.take_request())

    def test_the_request_is_taken_once(self) -> None:
        self.assertFalse(self.watcher.take_request())
        self.watcher.request(100.0)
        self.assertTrue(self.watcher.take_request())
        self.assertFalse(self.watcher.take_request())

    def test_the_gesture_can_be_switched_off_on_its_own(self) -> None:
        with mock.patch.object(config, "USAGE_PEEK", False):
            self.assertFalse(self.watcher.request(100.0))
        self.assertFalse(self.watcher.take_request())

    def test_looking_never_announces(self) -> None:
        # The regression this whole split exists to prevent: a peek that moved
        # the watermark or `resets_at` would eat the next milestone silently.
        self.watcher.observe(usage.Reading(70.0, "W1"))
        before = (
            self.watcher.watermark,
            self.watcher.resets_at,
            self.watcher.percent,
            self.watcher.payload(),
        )
        self.watcher.request(100.0)
        self.watcher.take_request()
        self.watcher.current()
        self.assertEqual(
            (
                self.watcher.watermark,
                self.watcher.resets_at,
                self.watcher.percent,
                self.watcher.payload(),
            ),
            before,
        )
        # ...and the milestone it would have swallowed still lands.
        self.assertEqual(self.watcher.observe(usage.Reading(76.0, "W1")), 75)

    def test_nothing_readable_is_nothing_shown(self) -> None:
        self.assertIsNone(self.watcher.current())


class CurrentTest(unittest.TestCase):
    """`current` against a real file, since that is the half `read` owns."""

    def setUp(self) -> None:
        handle, self.path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)
        usage._cache.clear()
        with open(self.path, "w") as out:
            json.dump(
                {
                    "cachedUsageUtilization": {
                        "utilization": {
                            "limits": [
                                {"kind": "session", "percent": 62, "resets_at": "s"}
                            ]
                        }
                    }
                },
                out,
            )

    def test_it_reads_what_is_there_now(self) -> None:
        self.assertEqual(usage.UsageWatcher(path=self.path).current(), 62.0)


class ReadTest(unittest.TestCase):
    """The file half, against a cache written to look like Claude Code's."""

    def setUp(self) -> None:
        handle, self.path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)
        usage._cache.clear()

    def write(self, payload: dict, bump: float = 0.0) -> None:
        with open(self.path, "w") as handle:
            json.dump(payload, handle)
        if bump:
            # The reader caches on mtime, and two writes inside one filesystem
            # tick are indistinguishable from none.
            stamp = os.stat(self.path).st_mtime + bump
            os.utime(self.path, (stamp, stamp))

    def test_the_limits_list_is_preferred(self) -> None:
        self.write(
            {
                "cachedUsageUtilization": {
                    "utilization": {
                        "five_hour": {"utilization": 2, "resets_at": "old"},
                        "limits": [
                            {"kind": "weekly_all", "percent": 36, "resets_at": "w"},
                            {"kind": "session", "percent": 41, "resets_at": "s"},
                        ],
                    }
                }
            }
        )
        self.assertEqual(usage.read(self.path), usage.Reading(41.0, "s"))

    def test_the_older_flat_field_still_answers(self) -> None:
        self.write(
            {
                "cachedUsageUtilization": {
                    "utilization": {"five_hour": {"utilization": 12, "resets_at": "s"}}
                }
            }
        )
        self.assertEqual(usage.read(self.path), usage.Reading(12.0, "s"))

    def test_every_way_of_having_no_answer_is_none(self) -> None:
        self.assertIsNone(usage.read("/nonexistent/claude.json"))
        self.write({"cachedUsageUtilization": {"utilization": {}}}, bump=10.0)
        self.assertIsNone(usage.read(self.path))
        self.write({}, bump=20.0)
        self.assertIsNone(usage.read(self.path))

    def test_a_half_written_file_is_not_cached_as_an_answer(self) -> None:
        with open(self.path, "w") as handle:
            handle.write('{"cachedUsageUtilization": {"utiliz')
        self.assertIsNone(usage.read(self.path))
        # Same mtime, valid content: a reader that had cached the failure
        # against it would keep reporting nothing until the next write.
        with open(self.path, "w") as handle:
            json.dump(
                {
                    "cachedUsageUtilization": {
                        "utilization": {
                            "limits": [{"kind": "session", "percent": 7, "resets_at": "s"}]
                        }
                    }
                },
                handle,
            )
        os.utime(self.path, (1_600_000_000, 1_600_000_000))
        self.assertEqual(usage.read(self.path), usage.Reading(7.0, "s"))


if __name__ == "__main__":
    unittest.main()
