# Hardware gotchas

The MIDI channel layout is documented and stable; the value tables are not.

- Channel 1 ring position, 2 RGB hue + press input, 3 RGB animation *or*
  brightness, 4 banks/side buttons, 6 ring animation *or* brightness.
- On channels 3 and 6 the low band is rate-based animations and the band above
  is a brightness ramp — a lit encoder can animate or dim, not both.
- **Off is 18** (`config.DARK_VALUE`) on channel 3, not 0 (which means "no
  animation") and not 17 (which is the *slowest pulse* — and since the daemon
  supplies the MIDI clock, all sixteen breathe in unison).
- **Channel 6's tables are channel 3's plus 48** (`config.RING_ANIM_OFFSET`):
  49-56 gate, 57-64 pulse, 65-95 brightness, so the ring's floor is **65**
  (`config.RING_DARK_VALUE`) and its ceiling 95. An RGB brightness sent on
  channel 6 lands below the indicator band and does nothing — silently, which is
  how it survived for the whole life of this board and made every ring
  brightness feature (focus marker, `RING_CEILING`, focus pulse, gauge fade,
  sleep dimming) inert. If a ring feature "doesn't work", suspect this first and
  sweep the whole channel, not just 0-47.
- Per-unit drift goes through `mft.calibrate` and then into `config.py` or an
  `MFT_*` env var. Don't hardcode a number a sweep found.
- Encoders must be set to accept host LED control in the Midi Fighter Utility,
  or the device drives its own LEDs and ignores everything sent.
- **The device has its own sleep timer**, also set in the Utility, and it counts
  *physical input* rather than incoming MIDI. A board that is only ever looked at
  — which is the whole design (invariant 1) — eventually dozes off mid-session
  and comes back only for a hand on a knob. There is no keep-alive: the daemon
  already sends a 120bpm clock continuously and restates all sixteen rings every
  `RING_REFRESH_SECONDS`, and the device slept through both. There is no readback
  to detect it with either — the LED path is write-only, and nothing on the input
  port reports LED or power state. So from in here a dozing device is
  *indistinguishable* from a board the daemon is deliberately holding dark: sends
  still succeed, `port_failing` stays false, the port enumerates exactly once, and
  the log is clean. Turn the timer off in the Utility; this one is not fixable in
  code. Don't be tempted by "lit board, no input for N minutes → probably asleep"
  either: an untouched board is the resting state of a display, so that flag would
  fire on healthy hardware constantly, which is the phantom of invariant 6.
