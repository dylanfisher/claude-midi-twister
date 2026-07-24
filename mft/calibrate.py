"""Find the right numbers for your unit.

The channel layout is documented and stable; the exact value -> colour and
value -> animation mappings drift between firmware revisions. Rather than
trusting a table, sweep them and write down what you see.

    python -m mft.calibrate colors    # 16 hues at a time across the bank
    python -m mft.calibrate anim      # channel 3 animation/brightness band
    python -m mft.calibrate ring      # channel 6 ring animation/brightness
    python -m mft.calibrate ramp      # ring positions 0..127

Paste the values you like into mft/config.py.
"""

from __future__ import annotations

import argparse
import logging
import time

from . import config
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["colors", "anim", "ring", "ramp"])
    parser.add_argument("--color", default="green", help="base colour for anim sweeps")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    device = open_twister()
    try:
        device.clear_all()
        if args.mode == "colors":
            print("each encoder shows one channel-2 value; note the ones you want")
            _pages(device, config.CH_SWITCH, 0, 127, "colour")

        elif args.mode in ("anim", "ring"):
            # Animations only read as animations against a lit ring/RGB.
            for slot in range(config.ENCODERS_PER_BANK):
                device.color(slot, args.color)
                device.ring(slot, 100)
            channel = config.CH_SWITCH_ANIM if args.mode == "anim" else config.CH_RING_ANIM
            print("low values are gate/pulse rates, higher ones are a brightness ramp")
            _pages(device, channel, 0, 127, args.mode)

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
        device.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
