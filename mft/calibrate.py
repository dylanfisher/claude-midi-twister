"""Find the right numbers for your unit.

The channel layout is documented and stable; the exact value -> color and
value -> animation mappings drift between firmware revisions. Rather than
trusting a table, sweep them and write down what you see.

    python -m mft.calibrate colors    # 16 hues at a time across the bank
    python -m mft.calibrate white     # which hue is not a hue (MFT_WHITE)
    python -m mft.calibrate anim      # channel 3 animation/brightness band
    python -m mft.calibrate ring      # channel 6 ring animation/brightness
    python -m mft.calibrate ramp      # ring positions 0..127
    python -m mft.calibrate dark      # which channel-3 value is actually *off*
    python -m mft.calibrate banks     # which channel-4 CCs switch bank

Paste the values you like into mft/config.py.

`banks` is the one sweep that varies the CC *number* instead of the value, and
the one whose answer you read on the front panel rather than in the lights: a
bank select is one message per bank, all at the same value, and what it changes
is which sixteen encoders are in front of you.

Every hue sweep here lights the RGB at *full* brightness and leaves the ring
dark. Both halves matter. Channel 3 carries brightness, and after a `clear_all`
it is sitting at the bottom of the ramp -- judging the wheel there means judging
sixteen LEDs too dim to have a color, which is how you end up picking a blue
and calling it white. And a lit ring next to the RGB is white by definition, so
it drags whatever is under it towards white.
"""

from __future__ import annotations

import argparse
import logging
import time

from . import board, config
from .twister import open_twister


def _pages(device, channel: int, start: int, end: int, label: str) -> None:
    per_page = config.ENCODERS_PER_BANK
    value = start
    while value <= end:
        page = []
        for slot in range(per_page):
            v = value + slot
            if v > end:
                device.cc(channel, slot, 0, force=True)
                continue
            device.cc(channel, slot, v, force=True)
            page.append(f"enc{slot + 1:>2}={v}")
        print(f"\n{label} ch{channel + 1}: " + "  ".join(page))
        try:
            input("enter for next page, ctrl-c to stop > ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        value += per_page


def _hue_stage(device) -> None:
    """Every encoder full-brightness RGB, ring dark: a hue on its own terms.

    See the module docstring -- a hue sweep read at the bottom of the brightness
    ramp, or with the ring lit beside it, is not a reading of the hue.
    """
    for slot in range(config.ENCODERS_PER_BANK):
        device.ring(slot, 0, force=True)
        device.cc(config.CH_RING_ANIM, slot, config.BRIGHTNESS_MIN, force=True)
        device.cc(config.CH_SWITCH_ANIM, slot, config.RGB_BRIGHTNESS_MAX, force=True)


def _white(device) -> None:
    """Find the value that reads as *lit* rather than as a color.

    The one hue the board needs to be sure of. It is what the boot unwrap and
    every banner word are lit in and what an encoder with nothing to say rests
    at, so if it is off by a few the whole device wears a permanent tint -- and a tint is
    exactly the thing this display uses to mean something.

    The candidates are the two ends of the wheel and nothing else: the range
    wraps, so if there is an achromatic value on this unit it is at a boundary.
    They are shown side by side, at full, because "least like a color" is a
    comparison and not something you can judge one LED at a time.
    """
    candidates = list(range(120, 128)) + list(range(0, 8))
    _hue_stage(device)
    for slot, value in enumerate(candidates):
        device.cc(config.CH_SWITCH, slot, value, force=True)
    print("\ntop and bottom of the wheel, at full brightness, rings dark:")
    print("  " + "  ".join(f"enc{s + 1:>2}={v:>3}" for s, v in enumerate(candidates)))
    print(
        "\npick the one that reads as a lit lamp rather than as a color. If they\n"
        "are all obviously blue or violet, this firmware has no white and the\n"
        "least-colored of them is the best the hardware does."
    )
    try:
        answer = input("encoder number (1-16), or blank to stop > ").strip()
    except (EOFError, KeyboardInterrupt):
        # No terminal to answer at -- piped, or run from something that is not
        # a shell. The board stays lit either way, so this is not a failure:
        # go and look, then read the value off the legend above.
        print("\nno prompt available; the board is still lit -- read the")
        print("legend above and export the value yourself.")
        return
    if not answer.isdigit() or not 1 <= int(answer) <= len(candidates):
        return
    value = candidates[int(answer) - 1]
    print(f"\n  export MFT_WHITE={value}\n")
    print("That value is both the boot gesture and the resting color of the board.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["colors", "white", "anim", "ring", "ramp", "dark", "banks"]
    )
    parser.add_argument("--color", default="green", help="base color for anim sweeps")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    device = open_twister()
    try:
        device.clear_all()
        if args.mode == "colors":
            print("each encoder shows one channel-2 value; note the ones you want")
            _hue_stage(device)
            _pages(device, config.CH_SWITCH, 0, 127, "color")

        elif args.mode == "white":
            _white(device)

        elif args.mode in ("anim", "ring"):
            # Animations only read as animations against a lit ring/RGB.
            for slot in range(config.ENCODERS_PER_BANK):
                device.color(slot, args.color)
                device.ring(slot, 100)
            channel = config.CH_SWITCH_ANIM if args.mode == "anim" else config.CH_RING_ANIM
            print("low values are gate/pulse rates, higher ones are a brightness ramp")
            _pages(device, channel, 0, 127, args.mode)

        elif args.mode == "dark":
            # Which channel-3 value actually extinguishes the switch LED, as
            # opposed to merely dimming it or handing it back to the device's
            # own inactive color. Every encoder starts lit and full, so the
            # only thing that changes per page is the candidate "off" value --
            # the answer is whichever encoders you cannot see.
            for slot in range(config.ENCODERS_PER_BANK):
                device.color(slot, args.color)
                device.ring(slot, 0)
                device.cc(config.CH_RING_ANIM, slot, config.BRIGHTNESS_MIN, force=True)
            print(
                "every encoder is lit; each page sets a different channel-3 value.\n"
                "look for the ones that go COMPLETELY dark (not just dim), and put\n"
                f"that value in MFT_DARK_VALUE (currently {config.DARK_VALUE}).\n"
                "note the page with 9-17 on it: those are pulse rates, so an encoder\n"
                "there is not dim, it is breathing -- watch it for a few seconds\n"
                "before calling it off. 17 in particular looks off at a glance and\n"
                "swells back up over 16 beats."
            )
            _pages(device, config.CH_SWITCH_ANIM, 0, 127, "dark")

        elif args.mode == "banks":
            # The one sweep whose answer you read on the *front panel* rather
            # than in the lights: a bank select changes which sixteen encoders
            # are there. So each candidate paints its own bank a distinct hue
            # first -- if the board switches, the color tells you which bank it
            # switched to, and if nothing happens the CC is wrong.
            hues = ("red", "green", "cyan", "violet")
            for bank in range(config.BANKS):
                for slot in board.bank_slots(bank):
                    device.color(slot, hues[bank % len(hues)])
                    device.ring(slot, 32 * (bank + 1))
            print(
                "each bank is painted a different color: bank 1 red, 2 green,\n"
                "3 cyan, 4 violet. below, one candidate CC at a time on channel\n"
                f"4 (currently BANK_SELECT_CC={config.BANK_SELECT_CC}).\n\n"
                "watch for the board CHANGING COLOR -- that is the bank moving,\n"
                "and the color says which bank it moved to. note the four CCs\n"
                "that work and the order they came in. if nothing ever changes,\n"
                "set MFT_FOLLOW_ALERTS=0 and leave the banks alone on this unit.\n\n"
                "unlike the other sweeps this one varies the CC *number*, not the\n"
                "value: a bank select is one message per bank, all at the same\n"
                "value, so there is nothing to ramp."
            )
            try:
                for control in range(16):
                    input(f"\nenter to send cc{control} = {config.BANK_SELECT_VALUE} > ")
                    device.cc(
                        config.CH_SYSTEM,
                        control,
                        config.BANK_SELECT_VALUE,
                        force=True,
                    )
            except (EOFError, KeyboardInterrupt):
                print()

        elif args.mode == "ramp":
            for slot in range(config.ENCODERS_PER_BANK):
                device.color(slot, "cyan")
            print("sweeping ring position 0..127 (ctrl-c to stop)")
            try:
                for value in range(128):
                    for slot in range(config.ENCODERS_PER_BANK):
                        device.ring(slot, value)
                    time.sleep(0.03)
            except KeyboardInterrupt:
                print()
    finally:
        # Not dark. Everything above exists to put something on the board for
        # you to look at, and the answer is often "let me go and look" rather
        # than a keypress at the prompt -- so the last frame stays up. The
        # daemon overwrites it on its next start.
        device.close(dark=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
