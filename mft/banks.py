"""Which sixteen of the sixty-four encoders the front panel is showing.

Two halves, deliberately kept apart:

* :func:`mft.board.bank_to_show` is the *policy* -- pure, no memory, testable
  with no hardware, and happy to name the same bank every frame;
* :class:`BankFollower` here is the part that must not be pure. It remembers
  which bank is up, it holds the cooldown, and it is the one place in the daemon
  that writes to the device for a reason other than painting.

That last sentence is why this is its own module rather than four fields on the
`Visualizer`. Invariant 1 says the device is a display and never a control
surface, and the bank select is the single exception -- it changes which sixteen
encoders you are looking at, never a session. An exception with an argument
attached is a design; an exception scattered through a thousand-line class is a
hole. Anything else that wants to write to the device for a non-painting reason
gets the same treatment: its own object, its own cooldown, its own docstring
saying why it is allowed.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from . import board, config
from .state import Session

log = logging.getLogger("mft.banks")


class BankFollower:
    """The current bank, and the rule for moving it.

    ``current`` starts at 0 as an assumption rather than a reading: nothing on
    this hardware reports the bank it is on, and asking would mean sending a
    bank select to find out -- which is the very thing that needs a reason.
    Wrong at worst until the first side button or the first followed alert.
    """

    def __init__(self, device) -> None:
        self.device = device
        self.current = 0
        self.moved_at = 0.0

    def chose(self, control: int, value: int, now: float) -> bool:
        """A side button: the human just chose a bank. Returns whether it landed.

        Recorded rather than acted on -- the device has already switched itself.
        It is also the one input that tells us something we otherwise have to
        assume, and it starts the cooldown, because a view you picked by hand
        should outlive the next notification.
        """
        if value < 64 or control not in config.BANK_SELECT_CC:
            return False
        self.current = config.BANK_SELECT_CC.index(control)
        self.moved_at = now
        log.debug("bank %d selected by hand", self.current + 1)
        return True

    def follow(self, sessions: Sequence[Session], now: float, blocked: bool) -> None:
        """Pull the front panel onto the bank where a human is blocking.

        Three brakes on it, and they are the whole reason this is safe to have
        on by default:

        * a cooldown, so two prompts on two banks cannot bounce the view between
          them, and so a bank you picked by hand stays picked;
        * ``blocked``, which the daemon raises during a peek -- a modal view of
          one session's history that would be silently replaced by another
          bank's encoders -- and while the boot word or the waiting animation
          still owns the board, the two moments the daemon is talking about
          itself rather than reporting. A bank select mid-word truncates it.
        """
        if not config.FOLLOW_ALERTS or blocked:
            return
        if now - self.moved_at < config.FOLLOW_ALERT_COOLDOWN_SECONDS:
            return
        want: Optional[int] = board.bank_to_show(sessions, self.current)
        if want is None:
            return
        log.info("bank %d has something blocking; following it", want + 1)
        self.device.bank(want)
        self.current = want
        self.moved_at = now
