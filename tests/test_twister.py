"""python3 -m unittest discover tests

What actually goes on the wire, with the hardware layer's de-dup in play. The
thing under test throughout is the end of the session: a Twister the daemon has
let go of has to be *dark*, and "dark" on this hardware is a specific value on a
specific channel sent in a specific order.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import board, config  # noqa: E402
from mft.render import Cell  # noqa: E402
from mft.twister import NullTwister, Twister  # noqa: E402


class Recorder(NullTwister):
    """A NullTwister that remembers every CC it would have sent."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[int, int, int]] = []

    def cc(self, channel: int, control: int, value: int, force: bool = False) -> None:
        key = (channel, control)
        with self._lock:
            if not force and self._last.get(key) == value:
                return
            self._last[key] = value
        self.sent.append((channel, control, value))

    def final(self, channel: int, slot: int) -> int | None:
        """The last value this slot's channel was left holding."""
        values = [v for ch, cc, v in self.sent if ch == channel and cc == slot]
        return values[-1] if values else None


class GoingDark(unittest.TestCase):
    def test_off_is_zero_brightness_not_no_animation(self):
        # Value 0 on channels 3 and 6 means "stop animating", which hands the
        # LED back to the device's own inactive colour -- a board that glows
        # dark blue at you after the daemon has exited. Zero *brightness* is
        # the only value that is actually off.
        device = Recorder()
        device.clear(0)
        self.assertEqual(device.final(config.CH_SWITCH_ANIM, 0), config.DARK_VALUE)
        self.assertEqual(device.final(config.CH_RING_ANIM, 0), config.DARK_VALUE)
        self.assertNotEqual(config.DARK_VALUE, config.ANIM_NONE)

    def test_nothing_relights_an_encoder_after_it_goes_off(self):
        # Brightness is the value holding the LED off, so it has to be the last
        # thing said about the encoder: a hue, a ring position or an animation
        # sent afterwards lights it straight back up.
        device = Recorder()
        device.clear(3)
        tail = [(ch, v) for ch, cc, v in device.sent if cc == 3]
        self.assertEqual(tail[-2:], [
            (config.CH_SWITCH_ANIM, config.DARK_VALUE),
            (config.CH_RING_ANIM, config.DARK_VALUE),
        ])

    def test_an_unlit_cell_renders_dark_on_both_channels(self):
        # The shutdown overlay's own last frames are colour-free cells; they go
        # through the same path and have to land in the same place.
        device = Recorder()
        device.write(7, Cell())
        self.assertEqual(device.final(config.CH_SWITCH_ANIM, 7), config.DARK_VALUE)
        self.assertEqual(device.final(config.CH_RING_ANIM, 7), config.DARK_VALUE)
        self.assertEqual(device.final(config.CH_ENCODER, 7), 0)

    def test_blackout_covers_every_encoder_on_every_bank(self):
        device = Recorder()
        device.blackout()
        for slot in range(config.SLOT_COUNT):
            self.assertEqual(device.final(config.CH_SWITCH_ANIM, slot), config.DARK_VALUE)
            self.assertEqual(device.final(config.CH_RING_ANIM, slot), config.DARK_VALUE)

    def test_blackout_ignores_the_dedup_cache(self):
        # The de-dup cache is an optimisation for a 30Hz render loop; on the way
        # out it is a claim about hardware state that nobody gets to re-check.
        # A board left glowing because we believed it was already off is the one
        # failure with no next frame to fix it.
        device = Recorder()
        device.blackout()
        device.sent.clear()
        device.blackout()
        self.assertEqual(len(device.sent), config.SLOT_COUNT * 3)

    def test_the_outro_ends_with_the_whole_board_off(self):
        # The gesture end to end, at the wire: every frame of the shutdown
        # overlay, then the blackout that follows it. Whatever the animation
        # was doing, the state it leaves behind is 64 dark encoders -- no hue
        # holding on at the bottom of the ramp, and no ring either.
        device = Recorder()
        overlay = board.ShutdownOverlay(0.0)
        t = 0.0
        while t < overlay.duration:
            for slot, cell in enumerate(board.compose([], t, [overlay])):
                device.write(slot, cell)
            t += 1.0 / config.FPS
        device.blackout()
        for slot in range(config.SLOT_COUNT):
            self.assertEqual(device.final(config.CH_SWITCH_ANIM, slot), config.DARK_VALUE)
            self.assertEqual(device.final(config.CH_RING_ANIM, slot), config.DARK_VALUE)
            self.assertEqual(device.final(config.CH_ENCODER, slot), 0)

    def test_closing_the_port_leaves_the_board_dark(self):
        # Letting go of the MIDI port is the last chance anything has to say
        # anything to this device, animation or no animation.
        device = Recorder()
        device.write(0, Cell(color="violet", brightness=1.0, ring=90))
        Twister.close(device)  # NullTwister.close is a no-op; the real one blacks out
        self.assertEqual(device.final(config.CH_SWITCH_ANIM, 0), config.DARK_VALUE)
        self.assertEqual(device.final(config.CH_RING_ANIM, 0), config.DARK_VALUE)


if __name__ == "__main__":
    unittest.main()
