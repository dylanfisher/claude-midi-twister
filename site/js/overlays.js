/* A port of mft/overlays.py -- the transient gestures painted over the board.
 *
 * Invariant 5: overlays are pure paint. They never mutate session state, so one
 * dropped mid-flight leaves nothing to tear down. Each one is an object with
 * `done(now)` and `paint(cells, now)`; `paint` reads whatever the board already
 * computed and writes over it, which is how every gesture crossfades back onto
 * the state underneath instead of snapping.
 */

import * as C from "./config.js?v=62";
import { Cell, clamp01, lerp, smoothstep, spiralPath, handover } from "./board.js?v=62";
import { pixels } from "./font.js?v=62";

const SPIRAL = spiralPath(4);

class Overlay {
  constructor(startedAt) { this.startedAt = startedAt; this.length = 1; }
  elapsed(now) { return Math.max(0, now - this.startedAt); }
  done(now) { return this.elapsed(now) >= this.length; }
  paint() {}
}

/* Spells a word across a bank one 4x4 glyph at a time. Each letter strikes at
 * full and decays to black before the next strikes, and the darkness in between
 * is what separates one letter from the next -- with four pixels of resolution
 * there is nothing else that could. A pixel the next letter also lights holds
 * instead of decaying, so the word flows rather than stutters. */
export class TextOverlay extends Overlay {
  constructor(startedAt, word, { color = null, letterSeconds = C.BOOT_LETTER_SECONDS } = {}) {
    super(startedAt);
    this.word = word.toUpperCase();
    this.color = color;
    this.letterSeconds = letterSeconds;
    this.length = this.word.length * letterSeconds + letterSeconds;
  }
  paint(cells, now) {
    const t = this.elapsed(now);
    const i = Math.floor(t / this.letterSeconds);
    const within = (t % this.letterSeconds) / this.letterSeconds;
    const cur = i < this.word.length ? pixels(this.word[i]) : new Array(16).fill(0);
    const next = i + 1 < this.word.length ? pixels(this.word[i + 1]) : new Array(16).fill(0);
    const decay = 1 - smoothstep(within);
    for (let s = 0; s < 16; s++) {
      // A pixel the next letter also lights holds instead of decaying.
      const level = cur[s] ? (next[s] ? Math.max(decay, within * 0.9) : decay) : 0;
      cells.set(s, Cell({
        color: this.color,
        ring: level > 0.02 ? 127 : 0,
        brightness: this.color === null ? 0 : level,
        ringLevel: level,
      }));
    }
  }
}

/* Slow white gradients drifting over an empty board until a session shows up.
 * Two broad raised-cosine gradients -- one down the diagonal, one across the
 * columns -- at periods with no common factor, so the pair never lines up twice
 * and there is no loop for your eye to finish. */
export class WaitingOverlay extends Overlay {
  constructor(startedAt) { super(startedAt); this.length = C.WAITING_SECONDS; }
  paint(cells, now) {
    const t = this.elapsed(now);
    const p = C.WAITING_PERIOD;
    for (let s = 0; s < 16; s++) {
      const row = Math.floor(s / 4), col = s % 4;
      // Two periods with no common factor: the pair never lines up twice.
      const a = Math.cos(((row + col) / 6 - t / p) * 2 * Math.PI) * 0.5 + 0.5;
      const b = Math.cos((col / 4 - t / (p * 1.44)) * 2 * Math.PI) * 0.5 + 0.5;
      const breath = Math.sin((t * 2 * Math.PI) / (p * 1.22)) * 0.25 + 0.75;
      const width = 1 / Math.max(0.1, C.WAITING_WIDTH);
      const level = Math.pow(a * 0.6 + b * 0.4, width) * breath * C.WAITING_BRIGHTNESS;
      cells.set(s, Cell({ color: null, ring: 127, brightness: 0, ringLevel: level }));
    }
  }
}

/* Boot arrival: the shutdown played backwards. All sixteen rise together (white
 * rings over blue), hold, then the spiral runs reversed -- centre outward to the
 * top-left corner -- each encoder going dark as the head leaves it. Ends on a
 * black board, so CLAUDE has somewhere to land. */
export class UnwrapOverlay extends Overlay {
  constructor(startedAt) {
    super(startedAt);
    this.length = C.BOOT_RISE + C.BOOT_HOLD + C.BOOT_SPIRAL + C.BOOT_FALL;
  }
  paint(cells, now) {
    const t = this.elapsed(now);
    const path = [...SPIRAL].reverse();
    let base = 1;
    if (t < C.BOOT_RISE) base = smoothstep(t / C.BOOT_RISE);
    const spiralT = t - C.BOOT_RISE - C.BOOT_HOLD;
    for (let i = 0; i < 16; i++) {
      const slot = path[i];
      let level = base;
      if (spiralT > 0) {
        const head = (spiralT / C.BOOT_SPIRAL) * 16;
        level = base * clamp01(1 - (head - i));
      }
      cells.set(slot, Cell({
        color: C.BOOT_UNWRAP_COLOR,
        ring: 127,
        brightness: level * 0.7,
        ringLevel: level,
      }));
    }
  }
}

/* The spiral. One head leaves the top-left corner and walks inward to the
 * centre in the device's own violet; hold for a beat; then all sixteen fade out
 * together, uniformly, all the way to off. */
export class ShutdownOverlay extends Overlay {
  constructor(startedAt) {
    super(startedAt);
    this.length = C.SHUTDOWN_RISE + C.SHUTDOWN_SPIRAL + C.SHUTDOWN_HOLD + C.SHUTDOWN_FADE;
  }
  paint(cells, now) {
    const t = this.elapsed(now);
    const spiralT = t - C.SHUTDOWN_RISE;
    const fadeStart = C.SHUTDOWN_RISE + C.SHUTDOWN_SPIRAL + C.SHUTDOWN_HOLD;
    const fade = t > fadeStart ? 1 - smoothstep((t - fadeStart) / C.SHUTDOWN_FADE) : 1;
    for (let i = 0; i < 16; i++) {
      const slot = SPIRAL[i];
      let level = 0;
      if (spiralT > 0) {
        const head = (spiralT / C.SHUTDOWN_SPIRAL) * 16;
        level = clamp01(head - i);
      }
      cells.set(slot, Cell({
        color: C.SHUTDOWN_COLOR,
        ring: 127,
        brightness: level * fade * 0.8,
        ringLevel: level * fade,
      }));
    }
  }
}

/* A session claims an encoder: the ring blinks full-on/full-off three times over
 * bright red, then crossfades into the steady state. Three hard flashes is a
 * countable shape -- you can tell it apart from a state that happens to be
 * blinking without watching it for a second and a half. */
export class SpawnOverlay extends Overlay {
  constructor(startedAt, slot) { super(startedAt); this.slot = slot; this.length = C.SPAWN_SECONDS; }
  paint(cells, now) {
    const t = this.elapsed(now);
    const flashLen = (C.SPAWN_SECONDS - C.SPAWN_SETTLE) / C.SPAWN_FLASHES;
    const under = cells.get(this.slot) || Cell({});
    if (t < C.SPAWN_SECONDS - C.SPAWN_SETTLE) {
      const on = (t % flashLen) / flashLen < 0.5;
      cells.set(this.slot, Cell({
        color: C.SPAWN_COLOR, ring: on ? 127 : 0,
        brightness: on ? 1 : 0.15, ringLevel: on ? 1 : 0,
      }));
    } else {
      const k = (t - (C.SPAWN_SECONDS - C.SPAWN_SETTLE)) / C.SPAWN_SETTLE;
      cells.set(this.slot, handover(under,
        Cell({ color: C.SPAWN_COLOR, ring: 127, brightness: 1, ringLevel: 1 }), smoothstep(k)));
    }
  }
}

/* /clear: the ring strikes white and unwinds to nothing, then hands back to an
 * idle pip with an empty gauge. It drains and *stops* -- which is the whole
 * difference from a compaction, and the reason the two gestures had to be
 * different shapes rather than different colors. */
export class ClearOverlay extends Overlay {
  constructor(startedAt, slot) { super(startedAt); this.slot = slot; this.length = C.CLEAR_SECONDS + C.CLEAR_SETTLE; }
  paint(cells, now) {
    const t = this.elapsed(now);
    const under = cells.get(this.slot) || Cell({});
    if (t < C.CLEAR_SECONDS) {
      const k = smoothstep(t / C.CLEAR_SECONDS);
      cells.set(this.slot, Cell({ color: null, ring: Math.round(127 * (1 - k)), brightness: 0, ringLevel: 1 - k * 0.4 }));
    } else {
      const k = (t - C.CLEAR_SECONDS) / C.CLEAR_SETTLE;
      cells.set(this.slot, handover(under, Cell({ color: null, ring: 0, brightness: 0, ringLevel: 0.6 }), smoothstep(k)));
    }
  }
}

/* Same family as clear, deliberately a different gesture: the arc drains to
 * zero, sits desaturated for a beat, then *refills* -- because the agent keeps
 * what mattered. A compaction is not a loss and must not look like one. */
export class CompactOverlay extends Overlay {
  constructor(startedAt, slot, refillTo = 0.35) {
    super(startedAt);
    this.slot = slot;
    this.refillTo = refillTo;
    this.length = C.COMPACT_DRAIN + C.COMPACT_HOLD + C.COMPACT_REFILL;
  }
  paint(cells, now) {
    const t = this.elapsed(now);
    let ring, level;
    if (t < C.COMPACT_DRAIN) {
      const k = smoothstep(t / C.COMPACT_DRAIN);
      ring = 127 * (1 - k); level = 1 - k * 0.5;
    } else if (t < C.COMPACT_DRAIN + C.COMPACT_HOLD) {
      ring = 0; level = 0.3;
    } else {
      const k = smoothstep((t - C.COMPACT_DRAIN - C.COMPACT_HOLD) / C.COMPACT_REFILL);
      ring = 127 * this.refillTo * k; level = lerp(0.3, 0.5, k);
    }
    cells.set(this.slot, Cell({ color: "azure", ring, brightness: 0.35, ringLevel: level }));
  }
}

/* Hold an encoder and its ring burns down like a fuse; when it reaches empty
 * the session comes off the board. The drain arms a fraction of a second in, so
 * a tap on the way to focusing a tab never flashes it, and you can watch the
 * clear coming and let go -- which is what makes a destructive gesture safe to
 * hang on the same knob as the harmless one. White over a dark switch: the
 * board talking about itself rather than reporting a state. */
export class DismissOverlay extends Overlay {
  constructor(startedAt, slot) {
    super(startedAt);
    this.slot = slot;
    this.length = C.HOLD_SECONDS;
  }

  /** The fuse has burned all the way down; the caller drops the session. */
  matured(now) { return this.elapsed(now) >= C.HOLD_SECONDS; }

  paint(cells, now) {
    const t = this.elapsed(now);
    if (t < C.DISMISS_ARM) return;
    const fill = 1 - clamp01((t - C.DISMISS_ARM) / (C.HOLD_SECONDS - C.DISMISS_ARM));
    cells.set(this.slot, Cell({ color: null, ring: 127 * fill, brightness: 1, ringLevel: 1 }));
  }
}

/* You switched to this session's tab: its RGB swells out from dark to full and
 * settles back onto its own level, once. The ring is untouched -- markFocus has
 * already pinned it at full and it stays there for as long as you are looking. */
export class FocusOverlay extends Overlay {
  constructor(startedAt, slot) { super(startedAt); this.slot = slot; this.length = C.FOCUS_SECONDS; }
  paint(cells, now) {
    const under = cells.get(this.slot) || Cell({});
    const k = this.elapsed(now) / C.FOCUS_SECONDS;
    const swell = Math.sin(clamp01(k) * Math.PI);
    cells.set(this.slot, { ...under, brightness: Math.max(under.brightness, swell), ringLevel: C.ATTENTION_RING_LEVEL });
  }
}

/* The board following the machine to sleep, and back. Not a fade to black for
 * its own sake: the board follows the *screen*, so the room going dark and the
 * board going dark are the same event. */
export class SleepOverlay extends Overlay {
  constructor(startedAt, waking = false) {
    super(startedAt);
    this.waking = waking;
    // Waking is fast on purpose: the board you come back to is the board you
    // left, and it should be there before you have finished sitting down.
    this.length = waking ? C.SLEEP_WAKE_SECONDS : C.SLEEP_FADE_SECONDS;
  }
  done(now) { return this.waking && this.elapsed(now) >= this.length; }
  paint(cells, now) {
    const k = clamp01(this.elapsed(now) / this.length);
    const level = this.waking ? smoothstep(k) : lerp(C.SLEEP_DARK_LEVEL, 1, 1 - smoothstep(k));
    for (const [slot, cell] of cells) {
      cells.set(slot, { ...cell, brightness: cell.brightness * level, ringLevel: cell.ringLight * level });
    }
  }
}
