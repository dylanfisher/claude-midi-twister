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

#: 0 .. config.SLOT_COUNT-1, spanning all four banks. It is also the CC number
#: on every channel: bank N occupies CC N*16 .. N*16+15, so slot and control are
#: the same integer and there is no mapping to get wrong.
Slot = int


def _ramp(fraction: float, low: int, high: int) -> int:
    """Map 0.0-1.0 onto one of the hardware's brightness bands."""
    fraction = max(0.0, min(1.0, fraction))
    return low + round(fraction * (high - low))


def _brightness_value(fraction: float) -> int:
    """Ring brightness, channel 6."""
    return _ramp(fraction, config.BRIGHTNESS_MIN, config.BRIGHTNESS_MAX)


def _rgb_brightness_value(fraction: float) -> int:
    """RGB brightness, channel 3 -- a *different* band to the ring's, and one
    that starts one value further up than it looks like it should. See
    :data:`config.RGB_BRIGHTNESS_MIN`; below it is the pulse band, not a dimmer
    lamp."""
    return _ramp(fraction, config.RGB_BRIGHTNESS_MIN, config.RGB_BRIGHTNESS_MAX)


class Twister:
    """Connected hardware. Use :func:`open_twister` to build one."""

    def __init__(self, outport, inport=None):
        self._out = outport
        self._in = inport
        self._lock = threading.RLock()
        self._last: dict[tuple[int, int], int] = {}
        self._clock_stop = threading.Event()
        #: Consecutive failed sends. Nonzero means the endpoint is gone -- a
        #: pulled cable, or a USB device that did not survive a sleep -- and is
        #: what :meth:`failing` answers so the daemon can try :meth:`reopen`.
        self._failures = 0
        #: What to restart after a reopen. A new pair of ports needs the input
        #: pump and the clock put back on them, and only this object knows they
        #: were ever running.
        self._callback: Optional[Callable[[object], None]] = None
        self._clock_bpm: Optional[float] = None
        # Imported here rather than at module scope so that importing this
        # module -- which the daemon does before it knows whether there is any
        # hardware -- does not require mido to be installed.
        self._mido = None if outport is None else __import__("mido")

    # -- low level ----------------------------------------------------------

    def _send(self, msg) -> None:
        """Put one message on the wire.

        Serialised on ``_lock`` so the clock thread and the render loop can
        never interleave halfway through each other's messages.
        """
        with self._lock:
            try:
                self._out.send(msg)
                self._failures = 0
            except Exception as exc:  # a yanked USB cable shouldn't kill the daemon
                # Logged once per outage, not once per message: whatever breaks
                # a port breaks it for every one of the ~200 writes a frame, 30
                # times a second, and a log that says so is not more informative
                # than a log that says it once.
                if not self._failures:
                    log.warning("MIDI send failed: %s", exc)
                self._failures += 1

    def cc(self, channel: int, control: int, value: int, force: bool = False) -> None:
        """One control change, dropped if the device already has that value.

        The de-dup is what makes a 30Hz loop over 64 encoders affordable: a
        steady board is 256 dict lookups a frame and no bytes at all. Held under
        one lock with the send, so the cached value and the wire cannot disagree
        about what the device was last told.
        """
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

    def ring(self, slot: Slot, value: int, force: bool = False) -> None:
        """LED ring position, 0-127."""
        self.cc(config.CH_ENCODER, slot, value, force=force)

    def color(self, slot: Slot, color: str | int | None) -> None:
        """Switch RGB hue. Accepts a name from ``config.COLORS``, a raw
        0-127 value, or ``None`` for off."""
        if color is None:
            value = config.COLOR_OFF
        elif isinstance(color, str):
            value = config.COLORS[color]
        else:
            value = color
        self.cc(config.CH_SWITCH, slot, value)

    def rgb_anim(self, slot: Slot, value: int) -> None:
        """RGB gate/pulse animation (0 = none)."""
        self.cc(config.CH_SWITCH_ANIM, slot, value)

    def bank(self, index: int) -> None:
        """Show one of the four banks.

        The only write in this class that changes what the *device* is doing
        rather than what one encoder looks like. It is still a display -- a bank
        select swaps which sixteen encoders are on the front panel, and touches no
        session -- but it is the one message the daemon sends for a reason other
        than painting, so it goes through `force=True`: the de-duplicating cache
        is keyed on (channel, control) and every bank select shares a value, so a
        cached one would be swallowed exactly when the board needs to move back to
        a bank it has been to before.
        """
        if not (0 <= index < len(config.BANK_SELECT_CC)):
            return
        self.cc(
            config.CH_SYSTEM,
            config.BANK_SELECT_CC[index],
            config.BANK_SELECT_VALUE,
            force=True,
        )

    def rgb_off(self, slot: Slot, force: bool = False) -> None:
        """Switch the RGB off.

        Off, not dim. Channel 2 has no dark end -- the whole 0-127 range is hue,
        and 0 is blue -- and ``ANIM_NONE`` on channel 3 is not it either: value
        0 stops overriding the device and hands the LED back to its own inactive
        color, which is the blue a stopped daemon used to leave glowing on the
        desk. The off is the bottom of channel 3's brightness ramp,
        :data:`config.DARK_VALUE`, which is one value further up than the ring's
        -- see :data:`config.RGB_BRIGHTNESS_MIN`.

        Brightness first, hue second, and that order matters exactly once: at
        startup the device's channel 3 is at whatever it was before we opened
        the port, so a hue sent first lands on a fully lit LED and flashes it.
        Sending the off first means the hue arrives at an LED that is already
        dark. Nothing is relit by it -- color and brightness are independent on
        this hardware, which is why a state change that only alters the hue does
        not also reset the encoder to full.
        """
        self.cc(config.CH_SWITCH_ANIM, slot, config.DARK_VALUE, force=force)
        self.cc(config.CH_SWITCH, slot, config.DARK_COLOR, force=force)

    def ring_off(self, slot: Slot, force: bool = False) -> None:
        """Same story one channel over: zero brightness, not "no animation" --
        but the ring's own floor, which is not the RGB's."""
        self.cc(config.CH_RING_ANIM, slot, config.RING_DARK_VALUE, force=force)

    def rgb_brightness(self, slot: Slot, fraction: float) -> None:
        self.cc(config.CH_SWITCH_ANIM, slot, _rgb_brightness_value(fraction))

    def ring_brightness(self, slot: Slot, fraction: float) -> None:
        self.cc(config.CH_RING_ANIM, slot, _brightness_value(fraction))

    def write(self, slot: Slot, cell) -> None:
        """Push one composed :class:`mft.render.Cell` to the hardware.

        Channels 3 and 6 each carry *either* an animation or a brightness
        level, never both, so an animated encoder can strobe or dim but not
        both at once. Animation wins where there is one.
        """
        if cell.color is None:
            # Unclaimed/ended: color-free, which on this hardware means off
            # rather than "hue 0" -- see :meth:`rgb_off`.
            self.rgb_off(slot)
        else:
            self.color(slot, cell.color)
            if cell.rgb_anim:
                self.rgb_anim(slot, cell.rgb_anim)
            else:
                self.rgb_brightness(slot, cell.brightness)
        self.ring(slot, cell.ring)
        self.ring_brightness(slot, cell.ring_light)

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
        self.stop_clock()
        self._clock_bpm = bpm
        # A fresh event per clock thread rather than clearing the shared one:
        # the outgoing thread may not have looked at the flag yet when the
        # incoming one clears it, and two threads driving the beat is a device
        # running at double rate -- which reads as animations that will not sit
        # still and no error anywhere. Only reachable through `reopen`, which is
        # why it took a fake port to find.
        stop = self._clock_stop = threading.Event()
        interval = 60.0 / (bpm * 24)  # MIDI clock is 24 pulses per quarter note

        def tick() -> None:
            self._send(self._mido.Message("start"))
            next_at = time.monotonic()
            while not stop.is_set():
                self._send(self._mido.Message("clock"))
                next_at += interval
                # Sleep to an absolute deadline; sleeping `interval` each time
                # accumulates drift and the whole point here is phase.
                stop.wait(max(0.0, next_at - time.monotonic()))

        threading.Thread(target=tick, name="mft-clock", daemon=True).start()
        log.info("sending MIDI clock at %.0f bpm", bpm)

    def stop_clock(self) -> None:
        if not self._clock_stop.is_set():
            self._clock_stop.set()
            self._send(self._mido.Message("stop"))

    # -- bulk ---------------------------------------------------------------

    def clear(self, slot: Slot, force: bool = False) -> None:
        """One encoder, dark on both channels.

        Position before brightness on the ring, and brightness before hue on the
        RGB (see :meth:`rgb_off`): in both cases the message that darkens the LED
        goes after the one that would otherwise be seen on a lit one.
        """
        self.ring(slot, 0, force=force)
        self.rgb_off(slot, force=force)
        self.ring_off(slot, force=force)

    def forget_rings(self) -> None:
        """Drop what the de-dup cache believes every encoder's ring holds.

        For when something other than us moved them -- a turn in absolute mode
        lights the ring locally -- and the cached value is now a lie that would
        suppress the write that puts it back. Only the rings: a knob cannot
        touch the RGB, and the hues are the expensive half of a frame.

        Called every frame (:meth:`mft.daemon.Visualizer.paint`) rather than off the
        incoming turn, so a ring the daemon never asked for is undone whether or
        not we saw the message that made it -- there is no MIDI input port on
        every setup, and a knob nudged before the daemon started was never a
        message at all.
        """
        with self._lock:
            for slot in range(config.SLOT_COUNT):
                self._last.pop((config.CH_ENCODER, slot), None)

    def forget_all(self) -> None:
        """Drop the whole de-dup cache, so the next frame restates every cell.

        For when what the device holds and what the cache believes it holds have
        come apart completely and there is no reasoning about which cells: a
        reopened port, or a board that was blacked out from under the render
        loop for a sleep. The next frame costs a full ~200 messages and then the
        de-dup is back to suppressing nearly all of them.
        """
        with self._lock:
            self._last.clear()

    def clear_all(self, force: bool = False) -> None:
        for slot in range(config.SLOT_COUNT):
            self.clear(slot, force=force)

    def blackout(self) -> None:
        """The last word on the board: every encoder off, no matter what the
        de-dup cache believes it already sent.

        Used on the way out, where "the LED is already off so skip the write"
        is exactly the assumption you cannot afford to get wrong -- the daemon
        does not get a second chance to correct a board it left glowing.
        """
        self.clear_all(force=True)

    # -- input --------------------------------------------------------------

    def listen(self, callback: Callable[[object], None]) -> None:
        """Start a thread pumping incoming messages into ``callback``."""
        self._callback = callback
        if self._in is None:
            log.info("no MIDI input port; encoder presses disabled")
            return
        self._pump()

    def _pump(self) -> None:
        """One thread per input port, and it ends with that port.

        The identity check is what makes :meth:`reopen` safe: closing a port
        that a thread is iterating does not reliably raise, so the old pump can
        outlive its port and go on delivering to a callback that has already
        been given a newer one. A pump that finds it is no longer holding *the*
        input port stands down instead.
        """
        inport = self._in
        if inport is None:
            return

        def pump() -> None:
            try:
                for msg in inport:
                    if inport is not self._in:
                        return
                    try:
                        self._callback(msg)
                    except Exception:
                        log.exception("input callback failed")
            except Exception:
                log.debug("MIDI input pump ended", exc_info=True)

        threading.Thread(target=pump, name="mft-input", daemon=True).start()

    # -- the port -----------------------------------------------------------

    def failing(self) -> bool:
        """True once a send has failed and none has succeeded since."""
        return self._failures > 0

    def reopen(self, match: str = config.PORT_MATCH) -> bool:
        """Swap in a fresh pair of ports, in place. True if there is one.

        In place rather than by handing back a new object, because the daemon is
        not the only thing holding this: the clock thread and the input pump
        both close over `self`, and a device swapped out from under them leaves
        two threads driving a port nobody is reading.

        A sleep can leave the endpoint invalid without closing it -- every send
        raises, the port still exists, and mido has nothing to say about it. So
        the trigger for this is :meth:`failing`, which is about writes rather
        than about anything the OS reports, and the same path serves a cable
        pulled out and pushed back in.
        """
        if self._mido is None:  # never had hardware; nothing to get back
            return False
        with self._lock:
            self.stop_clock()
            for port in (self._out, self._in):
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        pass
            self._out = self._in = None
            out_name, in_name = find_ports(match)
            if out_name is None:
                return False
            try:
                self._out = self._mido.open_output(out_name)
            except Exception as exc:
                log.warning("could not reopen MIDI output %r: %s", out_name, exc)
                return False
            if in_name:
                try:
                    self._in = self._mido.open_input(in_name)
                except Exception as exc:
                    log.warning("could not reopen MIDI input %r: %s", in_name, exc)
            # The device came back dark and remembering nothing, so neither may
            # the cache: every cell has to be restated by the next frame.
            self._last.clear()
            self._failures = 0
        if self._callback is not None:
            self._pump()
        if self._clock_bpm:
            self.start_clock(self._clock_bpm)
        log.info("reopened %s%s", out_name, f" / {in_name}" if self._in else "")
        return True

    def close(self, dark: bool = True) -> None:
        """Hang up. ``dark`` is the daemon's exit: leave nothing glowing.

        The calibrator passes ``False``. Its whole product is a lit board you
        are being asked to look at, and blacking that out on the way through
        the door means a non-interactive run shows you nothing at all -- the
        LEDs go out in the same breath as the prompt that asks you about them.
        """
        try:
            self.stop_clock()
            if dark:
                self.blackout()
        finally:
            for port in (self._out, self._in):
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        pass


class NullTwister(Twister):
    """Stand-in when no device is plugged in, so the daemon still runs.

    A real :class:`Twister` with no ports, rather than a parallel
    implementation: everything above the wire -- de-dup, clamping, the
    channel layout, :meth:`~Twister.write` -- is the code the daemon actually
    ships, so ``--no-device`` exercises it instead of a lookalike.
    """

    def __init__(self) -> None:
        super().__init__(None, None)

    def cc(self, channel: int, control: int, value: int, force: bool = False) -> None:
        value = max(0, min(127, int(value)))
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

    def close(self, dark: bool = True) -> None:
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
