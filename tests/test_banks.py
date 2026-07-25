"""Which sixteen of the sixty-four encoders the front panel is showing.

All against the pure policy in :func:`mft.board.bank_to_show`, which is why it
returns a bank instead of sending one: the interesting cases are "two prompts on
two banks" and "you already looked at this one", and neither needs hardware.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mft import board, config  # noqa: E402
from mft.state import SessionTable  # noqa: E402


class FollowingAlerts(unittest.TestCase):
    def setUp(self):
        self.table = SessionTable()

    def session(self, sid: str, slot: int, state: str = "permission", at=0.0):
        """A session parked on a chosen slot.

        The allocator hands out slot 0 upwards and would put every one of these on
        bank 1, which is the one arrangement where none of this matters.
        """
        session = self.table.ensure(sid, "/tmp/p", {"tty": f"/dev/{sid}"})
        session.slot = slot
        session.state = state
        session.attention_since = at
        session.alert = True
        return session

    def test_a_block_on_another_bank_asks_for_that_bank(self):
        self.session("a", slot=config.ENCODERS_PER_BANK * 2 + 3)
        self.assertEqual(board.bank_to_show(self.table.all(), 0), 2)

    def test_a_block_on_the_visible_bank_asks_for_nothing(self):
        # Not "return the current bank": the daemon writes whatever it is handed,
        # and a bank select every frame is a message to the device thirty times a
        # second saying nothing.
        self.session("a", slot=3)
        self.assertIsNone(board.bank_to_show(self.table.all(), 0))

    def test_an_attended_session_never_moves_the_view(self):
        session = self.session("a", slot=config.ENCODERS_PER_BANK * 2)
        session.attended()
        session.alert = False
        self.assertIsNone(board.bank_to_show(self.table.all(), 0))

    def test_a_working_session_never_moves_the_view(self):
        # Work happening on another bank is not something you have to see, and
        # following it would move the board more or less permanently.
        self.session("a", slot=config.ENCODERS_PER_BANK, state="working")
        self.assertIsNone(board.bank_to_show(self.table.all(), 0))

    def test_the_worst_block_wins_when_two_banks_have_one(self):
        # Same ranking as the motion budget: state first, then whoever has been
        # waiting longest. A plan and a gate should not disagree about which one
        # the board owes its attention to depending on where they landed.
        self.session("plan", slot=config.ENCODERS_PER_BANK, state="plan", at=0.0)
        self.session("gate", slot=config.ENCODERS_PER_BANK * 3, at=500.0)
        self.assertEqual(board.bank_to_show(self.table.all(), 0), 3)

    def test_the_oldest_debt_wins_within_one_state(self):
        self.session("new", slot=config.ENCODERS_PER_BANK, at=500.0)
        self.session("old", slot=config.ENCODERS_PER_BANK * 2, at=0.0)
        self.assertEqual(board.bank_to_show(self.table.all(), 0), 2)

    def test_an_ended_session_is_not_worth_a_bank(self):
        session = self.session("a", slot=config.ENCODERS_PER_BANK)
        session.ended_at = 1.0
        self.assertIsNone(board.bank_to_show(self.table.all(), 0))

    def test_an_empty_board_asks_for_nothing(self):
        self.assertIsNone(board.bank_to_show([], 0))

    def test_the_gesture_can_be_switched_off(self):
        self.session("a", slot=config.ENCODERS_PER_BANK)
        real = config.FOLLOW_ALERTS
        self.addCleanup(setattr, config, "FOLLOW_ALERTS", real)
        config.FOLLOW_ALERTS = False
        self.assertIsNone(board.bank_to_show(self.table.all(), 0))

    def test_a_unit_with_no_bank_ccs_never_follows(self):
        # The honest setting for a Twister where `calibrate banks` found nothing:
        # the policy has to go quiet too, or the daemon spends every frame being
        # told to move the view and having no way to do it.
        self.session("a", slot=config.ENCODERS_PER_BANK)
        real = config.BANK_SELECT_CC
        self.addCleanup(setattr, config, "BANK_SELECT_CC", real)
        config.BANK_SELECT_CC = ()
        self.assertIsNone(board.bank_to_show(self.table.all(), 0))


class WritingTheBank(unittest.TestCase):
    """The wire half: a bank select is the one write that is not painting."""

    def test_a_bank_select_is_never_swallowed_by_the_dedup_cache(self):
        # The cache is keyed on (channel, control) and every bank shares one
        # value, so a plain write would go out once per bank per lifetime and
        # then be silently dropped -- including the return to a bank you have
        # been to before, which is the common case.
        from mft.twister import NullTwister

        device = NullTwister()
        device.bank(2)
        device.bank(0)
        device.bank(2)
        key = (config.CH_SYSTEM, config.BANK_SELECT_CC[2])
        self.assertEqual(device._last[key], config.BANK_SELECT_VALUE)

    def test_a_bank_out_of_range_is_not_sent(self):
        from mft.twister import NullTwister

        device = NullTwister()
        device.bank(99)
        device.bank(-1)
        self.assertEqual(device._last, {})


if __name__ == "__main__":
    unittest.main()
