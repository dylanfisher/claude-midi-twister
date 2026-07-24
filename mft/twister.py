"""Thin MIDI layer over the DJTT Midi Fighter Twister.

Owns the USB port (MIDI access is exclusive on Windows and flaky when shared
anywhere else), exposes one method per LED feature, and de-duplicates writes so
a 30Hz render loop only puts bytes on the wire when something actually changed.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from . import config

log = logging.getLogger("mft.twister")

Slot = int  # 0 .. config.SLOT_COUNT-1, spanning all four banks


def slot_to_cc(slot: Slot) -> int:
    """Bank N occupies CC N*16 .. N*16+15 on every channel."""
    if not 0 <= slot < config.SLOT_COUNT:
        raise ValueError(f"slot {slot} out of range")
    return slot


def slot_bank(slot: Slot) -> int:
    return slot // config.ENCODERS_PER_BANK


def _brightness_value(fraction: float) -> int:
    """Map 0.0-1.0 onto the hardware's brightness band."""
    fraction = max(0.0, min(1.0, fraction))
    span = config.BRIGHTNESS_MAX - config.BRIGHTNESS_MIN
    return config.BRIGHTNESS_MIN + round(fraction * span)


class Twister:
    """Connected hardware. Use :func:`open_twister` to build one."""

    def __init__(self, outport, inport=None):
        self._out = outport
        self._in = inport
        self._lock = threading.Lock()
        self._last: dict[tuple[int, int], int] = {}
        self._mido = __import__("mido")
        self._clock_stop = threading.Event()

    # -- low level ----------------------------------------------------------

    def _send(self, msg) -> None:
        """Serialised on ``_lock`` so the clock thread and the render loop can
        never interleave halfway through each other's messages."""
        with self._lock:
            try:
                self._out.send(msg)
            except Exception as exc:  # a yanked USB cable shouldn't kill the daemon
                log.warning("MIDI send failed: %s", exc)

    def cc(self, channel: int, control: int, value: int, force: bool = False) -> None:
        value = max(0, min(127, int(value)))
        key = (channel, control)
        with self._lock:
            if not force and self._last.get(key) == value:
                return
            self._last[key] = value
        self._send(
            self._mido.Message(
                "control_change", channel=channel, control=control, value=value
            )
        )

    # -- per-encoder features ----------------------------------------------

    def ring(self, slot: Slot, value: int) -> None:
        """LED ring position, 0-127."""
        self.cc(config.CH_ENCODER, slot_to_cc(slot), value)

    def color(self, slot: Slot, color: str | int | None) -> None:
        """Switch RGB hue. Accepts a name from ``config.COLORS``, a raw
        0-127 value, or ``None`` for off."""
        if color is None:
            value = config.COLOR_OFF
        elif isinstance(color, str):
            value = config.COLORS[color]
        else:
            value = color
        self.cc(config.CH_SWITCH, slot_to_cc(slot), value)

    def rgb_anim(self, slot: Slot, value: int) -> None:
        """RGB gate/pulse animation (0 = none)."""
        self.cc(config.CH_SWITCH_ANIM, slot_to_cc(slot), value)

    def rgb_off(self, slot: Slot) -> None:
        """Extinguish the switch LED completely.

        Channel 2 has no "off" -- its whole 0-127 range is hue, and 0 is blue.
        The only way to get a genuinely dark encoder is value 0 on the
        animation/brightness channel, and nothing may set a brightness after.
        """
        self.cc(config.CH_SWITCH_ANIM, slot_to_cc(slot), config.ANIM_NONE)

    def rgb_brightness(self, slot: Slot, fraction: float) -> None:
        self.cc(config.CH_SWITCH_ANIM, slot_to_cc(slot), _brightness_value(fraction))

    def ring_anim(self, slot: Slot, value: int) -> None:
        self.cc(config.CH_RING_ANIM, slot_to_cc(slot), value)

    def ring_brightness(self, slot: Slot, fraction: float) -> None:
        self.cc(config.CH_RING_ANIM, slot_to_cc(slot), _brightness_value(fraction))

    def write(self, slot: Slot, cell) -> None:
        """Push one composed :class:`mft.render.Cell` to the hardware.

        Channels 3 and 6 each carry *either* an animation or a brightness
        level, never both, so an animated encoder can strobe or dim but not
        both at once. Animation wins where there is one.
        """
        if cell.color is None:
            # Unclaimed/ended: colour-free, which on this hardware means off
            # rather than "hue 0" -- see :meth:`rgb_off`.
            self.rgb_off(slot)
        else:
            self.color(slot, cell.color)
            if cell.rgb_anim:
                self.rgb_anim(slot, cell.rgb_anim)
            else:
                self.rgb_brightness(slot, cell.brightness)
        self.ring(slot, cell.ring)
        self.ring_brightness(slot, cell.brightness)

    # -- clock --------------------------------------------------------------

    def start_clock(self, bpm: float = config.CLOCK_BPM) -> None:
        """Drive the device's gate and pulse animations from a shared beat.

        The Twister's animation rates are all expressed in beats, and it takes
        its beat from incoming MIDI clock. Without one, every encoder free-runs
        off its own timer and they drift out of phase, which reads as broken
        hardware. With one, everything flashing on the board flashes together,
        which reads as design.
        """
        if bpm <= 0 or self._out is None:
            return
        interval = 60.0 / (bpm * 24)  # MIDI clock is 24 pulses per quarter note

        def tick() -> None:
            self._send(self._mido.Message("start"))
            next_at = time.monotonic()
            while not self._clock_stop.is_set():
                self._send(self._mido.Message("clock"))
                next_at += interval
                # Sleep to an absolute deadline; sleeping `interval` each time
                # accumulates drift and the whole point here is phase.
                self._clock_stop.wait(max(0.0, next_at - time.monotonic()))

        threading.Thread(target=tick, name="mft-clock", daemon=True).start()
        log.info("sending MIDI clock at %.0f bpm", bpm)

    def stop_clock(self) -> None:
        if not self._clock_stop.is_set():
            self._clock_stop.set()
            self._send(self._mido.Message("stop"))

    # -- bulk ---------------------------------------------------------------

    def clear(self, slot: Slot) -> None:
        self.ring(slot, 0)
        self.rgb_off(slot)
        self.ring_anim(slot, config.ANIM_NONE)

    def clear_all(self) -> None:
        for slot in range(config.SLOT_COUNT):
            self.clear(slot)

    # -- input --------------------------------------------------------------

    def listen(self, callback: Callable[[object], None]) -> None:
        """Start a thread pumping incoming messages into ``callback``."""
        if self._in is None:
            log.info("no MIDI input port; encoder presses disabled")
            return

        def pump() -> None:
            for msg in self._in:
                try:
                    callback(msg)
                except Exception:
                    log.exception("input callback failed")

        threading.Thread(target=pump, name="mft-input", daemon=True).start()

    def close(self) -> None:
        try:
            self.stop_clock()
            self.clear_all()
        finally:
            for port in (self._out, self._in):
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        pass


class NullTwister(Twister):
    """Stand-in when no device is plugged in, so the daemon still runs."""

    def __init__(self) -> None:  # noqa: D107 - deliberately skips Twister.__init__
        self._out = None
        self._in = None
        self._lock = threading.Lock()
        self._last = {}
        self._clock_stop = threading.Event()

    def cc(self, channel: int, control: int, value: int, force: bool = False) -> None:
        key = (channel, control)
        with self._lock:
            if not force and self._last.get(key) == value:
                return
            self._last[key] = value
        log.debug("(no device) ch%d cc%d = %d", channel + 1, control, value)

    def listen(self, callback) -> None:
        return

    def start_clock(self, bpm: float = config.CLOCK_BPM) -> None:
        return

    def stop_clock(self) -> None:
        return

    def close(self) -> None:
        return


def find_ports(match: str = config.PORT_MATCH) -> tuple[Optional[str], Optional[str]]:
    import mido

    match = match.lower()
    out = next((n for n in mido.get_output_names() if match in n.lower()), None)
    inp = next((n for n in mido.get_input_names() if match in n.lower()), None)
    return out, inp


def open_twister(match: str = config.PORT_MATCH) -> Twister:
    try:
        import mido
    except ImportError:
        log.error("mido not installed; running without hardware")
        return NullTwister()

    out_name, in_name = find_ports(match)
    if out_name is None:
        log.warning(
            "no MIDI output matching %r (saw: %s); running without hardware",
            match,
            ", ".join(mido.get_output_names()) or "none",
        )
        return NullTwister()

    outport = mido.open_output(out_name)
    inport = None
    if in_name:
        try:
            inport = mido.open_input(in_name)
        except Exception as exc:
            log.warning("could not open MIDI input %r: %s", in_name, exc)
    log.info("connected to %s%s", out_name, f" / {in_name}" if inport else "")
    return Twister(outport, inport)
