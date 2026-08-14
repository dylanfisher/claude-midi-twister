/* The pure half of the daemon, ported: mft/render.py + mft/board.py.
 *
 * A Cell is one encoder's worth of light. `render(session, now)` turns one
 * session into one Cell; `compose` stacks the whole board and resolves the
 * things only decidable across all sixteen -- who gets the one fast animation,
 * where subagents pile up, what an overlay is covering.
 *
 * Everything here is a pure function of (state, clock), same as the original,
 * which is why the simulator can be driven from a scripted scenario and from a
 * human clicking around without either knowing about the other.
 */

import * as C from "./config.js?v=74";

// --- the easing vocabulary, all of it (mft/board.py:116,122, render.lerp) ---
export const clamp01 = (t) => Math.max(0, Math.min(1, t));
export const lerp = (lo, hi, t) => lo + (hi - lo) * clamp01(t);
export const smoothstep = (t) => { t = clamp01(t); return t * t * (3 - 2 * t); };

/** One encoder. `brightness` drives both the RGB and the ring, unless
 *  `ringLevel` is set -- which a state does only when the ring is saying
 *  something the hue is not. On this board that is the context gauge and
 *  nothing else. */
export function Cell({ color = null, anim = 0, ring = 0, brightness = 0, ringLevel = null } = {}) {
  return {
    color,
    anim,
    ring: Math.max(0, Math.min(127, Math.round(ring))),
    brightness: clamp01(brightness),
    ringLevel: ringLevel === null ? null : clamp01(ringLevel),
    get ringLight() { return this.ringLevel === null ? this.brightness : this.ringLevel; },
  };
}

export const DARK = () => Cell({});

// --- the grid walks (mft/board.py) -----------------------------------------

/* The 16 slots of a bank as one inward spiral from the top-left. A path rather
 * than a scan, because a raster order teleports back to the left edge three
 * times, which reads as four unrelated sweeps rather than as one thing
 * travelling. */
export function spiralPath(n = 4) {
  const out = [];
  let top = 0, bottom = n - 1, left = 0, right = n - 1;
  while (top <= bottom && left <= right) {
    for (let c = left; c <= right; c++) out.push(top * n + c);
    top++;
    for (let r = top; r <= bottom; r++) out.push(r * n + right);
    right--;
    if (top <= bottom) { for (let c = right; c >= left; c--) out.push(bottom * n + c); bottom--; }
    if (left <= right) { for (let r = bottom; r >= top; r--) out.push(r * n + left); left++; }
  }
  return out;
}

/* Sessions fill top-left forwards; subagents fill bottom-right backwards. The
 * two allocators grow toward each other and the far corner is always the newest
 * thing on the board. */
export function spawnOrder(reverse = false) {
  const slots = [...Array(C.PER_BANK).keys()];
  return reverse ? slots.reverse() : slots;
}

// --- the stopwatch (mft/render.py) -----------------------------------------

/** How far round a stopwatch ring is, after `elapsed` seconds of a `span`.
 *  Saturates rather than wrapping: a wrap would be ambiguous with a stopwatch
 *  that just started, which is the one reading that must never be wrong. */
export function stopwatchFraction(elapsed, span) {
  if (span <= 0) return 0;
  const x = Math.min(1, Math.max(0, elapsed) / span);
  const curve = C.STOPWATCH_CURVE;
  for (let i = 0; i < curve.length - 1; i++) {
    const [x0, y0] = curve[i], [x1, y1] = curve[i + 1];
    if (x <= x1) {
      if (x1 === x0) return y1;
      return y0 + (y1 - y0) * ((x - x0) / (x1 - x0));
    }
  }
  return curve[curve.length - 1][1];
}

/** stopwatchFraction as a ring position, held off zero by `floor`. The floor is
 *  an offset, not a clamp: clamped, the ring sat on the stub for nineteen
 *  seconds, and a stopwatch has to move on its first frame. */
export function stopwatchRing(elapsed, span, floor) {
  return floor + Math.round((127 - floor) * stopwatchFraction(elapsed, span));
}

// --- attention -------------------------------------------------------------

/** 0 -> 1 as an unattended session gets more insistent. The one quantity on the
 *  board that describes *you* rather than the agent, forgiven the instant you
 *  focus the tab. */
export function attentionDebt(session, now) {
  if (session.attentionSince === null || session.attentionSince === undefined) return 0;
  return Math.min(1, Math.max(0, now - session.attentionSince) / C.ATTENTION_RAMP_SECONDS);
}

function insistence(session, now) {
  return lerp(C.ATTENTION_FLOOR, C.ATTENTION_CEILING, attentionDebt(session, now));
}

/* Escalate an animation rate by neglect without leaving its own band, so a gate
 * stays a gate and a breathe stays a breathe. These states are exactly the ones
 * whose brightness the wire discards, so debt has to be spent on rate or it is
 * not spent at all. */
const BANDS = [
  [1, 2, 3, 4, 5, 6, 7, 8],
  [9, 10, 11, 12, 13, 14, 15, 16],
];
function debtAnim(base, debt) {
  const steps = Math.floor(debt * C.ATTENTION_ANIM_STEPS);
  if (!base || steps <= 0) return base;
  for (const band of BANDS) {
    const i = band.indexOf(base);
    if (i >= 0) return band[Math.min(i + steps, band.length - 1)];
  }
  return base;
}

const sweep = (now, rate) => Math.floor(((now * rate) % 1) * 127);
const breathe = (now, period, low, high) =>
  low + (high - low) * ((Math.sin((now * 2 * Math.PI) / period) + 1) / 2);

const SWEEP_RATE = { thinking: 0.35, streaming: 0.5 };

/* `working`'s hue, warmed toward red by how badly the turn is going -- the one
 * thing a tool-call board is in a position to know and has never said. An agent
 * making progress and an agent retrying the same failing edit look identical,
 * and the second is the one you want to be interrupted about. Interpolated on
 * the wheel rather than switched at a threshold: orange and red are six values
 * apart, and a linear ramp would spend the first failure inside a rounding
 * error. Hue and nothing else -- a failing agent is still working, and must not
 * be able to promote itself into an alert. */
export function workingColor(session, base) {
  const f = session.failureFraction || 0;
  if (f <= 0) return base;
  const eased = 1 - Math.pow(1 - f, C.FAILURE_HEAT_CURVE);
  const from = C.COLORS[base] ? C.COLORS[base].midi : 72;
  const to = C.COLORS[C.FAILURE_HEAT_COLOR].midi;
  return Math.round(lerp(from, to, eased));
}

function workingBrightness(session, now) {
  const last = session.lastToolAt;
  const sinceTool = now - (last === null || last === undefined ? session.stateSince : last);
  const kick = Math.max(0, 1 - sinceTool / C.TOOL_KICK_SECONDS);
  let level = lerp(C.ACTIVE_FLOOR, C.ACTIVE_BRIGHTNESS, kick);
  const stalled = sinceTool - C.STALL_SECONDS;
  if (stalled > 0) level = lerp(level, C.IDLE_BRIGHTNESS, Math.min(1, stalled / C.STALL_FADE_SECONDS));
  return level;
}

function restingGauge(session) {
  const f = session.contextFraction;
  if (f === null || f === undefined) return C.PIP;
  return Math.max(C.CONTEXT_RING_FLOOR, Math.round(127 * f));
}

function gaugeLevel(session, now, base) {
  const age = Math.max(0, now - session.stateSince);
  return lerp(base, C.GAUGE_STALE_LEVEL, age / C.DONE_FADE_SECONDS);
}

// --- render (mft/render.py:309) --------------------------------------------

/** One session, one encoder. Pure function of (session, clock). */
export function render(session, now) {
  const state = session.state;
  let color = C.STATE_COLORS[state];
  let anim = C.STATE_ANIM[state] || C.ANIM_NONE;
  let ring = 0;
  let brightness = C.ACTIVE_BRIGHTNESS;
  let ringLevel = null;

  if (state === "ended") return Cell({});

  if (state === "idle") {
    // A dim single pip -- claimed, nothing happening -- or the gauge, if we
    // have a reading. How full the window is outlives the turn that filled it.
    ring = restingGauge(session);
    brightness = C.IDLE_BRIGHTNESS;
    ringLevel = gaugeLevel(session, now, brightness);
  } else if (state === "working") {
    // The stopwatch, not the gauge: the window barely moves during a turn and
    // "how long has this been going" is the whole reason you looked.
    ring = stopwatchRing(now - (session.turnStartedAt ?? session.stateSince),
                         C.TURN_RING_FULL_SECONDS, C.CONTEXT_RING_FLOOR);
    brightness = workingBrightness(session, now);
    color = workingColor(session, color);
    // brightness carries the shimmer -- ACTIVE_FLOOR between tool calls, up to
    // ACTIVE_BRIGHTNESS on a kick -- which is a real signal on the lens. The
    // ring is a position, not an intensity, and reading ACTIVE_FLOOR as a grey
    // partial-opacity dot rather than a lit one is exactly the split every other
    // state above already gets: full white ring, independent lens level.
    ringLevel = C.ATTENTION_RING_LEVEL;
  } else if (SWEEP_RATE[state]) {
    // Same split as the blocking states below: SWEEP_BRIGHTNESS is deliberately
    // dim because it reaches the lens on this page (see that constant's
    // comment), but the travelling dot is the entire signal for "thinking" and
    // "streaming" and reads as barely-there at 0.3 opacity. Full ring, dim lens.
    ring = sweep(now, SWEEP_RATE[state]);
    brightness = C.SWEEP_BRIGHTNESS;
    ringLevel = C.ATTENTION_RING_LEVEL;
  } else if (state === "permission" || state === "plan") {
    // The only things on the board allowed to move fast, because they are the
    // only things that mean a human is blocking progress right now. Ignore one
    // and it strobes faster: brightness never reaches the RGB while there is an
    // animation on it, so rate is the only channel neglect has here.
    ring = 127;
    anim = debtAnim(anim, attentionDebt(session, now));
    brightness = insistence(session, now);
    // The lens dims with insistence() -- ATTENTION_CEILING is tuned down from
    // mft/config.py's 1.0 because this page's CSS lets brightness reach the
    // lens on every state, animated or not (see that constant's comment). The
    // white ring has no such reason to hold back: a block is asking for you as
    // hard as a spawn strike does, so it gets the same full ring the strike
    // gets, decoupled onto its own channel.
    ringLevel = C.ATTENTION_RING_LEVEL;
  } else if (state === "waiting") {
    ring = 127;
    anim = debtAnim(anim, attentionDebt(session, now));
    brightness = breathe(now, 2.4, 0.3, insistence(session, now));
    ringLevel = C.ATTENTION_RING_LEVEL;
  } else if (state === "error") {
    // Solid, not strobing: a rate limit is bad but it is not waiting on your
    // hand, and two identical blinking reds would be indistinguishable.
    ring = 127;
    brightness = insistence(session, now);
    ringLevel = C.ATTENTION_RING_LEVEL;
  } else if (state === "done") {
    // Flash, then recede -- and then, if you never come look, slowly ramp back
    // up. Capped so it can never outshout a live block.
    const age = Math.max(0, now - session.stateSince);
    const fade = Math.min(1, Math.max(0, 1 - age / C.DONE_FADE_SECONDS));
    ring = Math.max(restingGauge(session), Math.round(127 * fade));
    brightness = lerp(C.IDLE_BRIGHTNESS, C.DONE_FLASH_BRIGHTNESS, fade);
    // Same split as every state above: the lens flashes to DONE_FLASH_BRIGHTNESS,
    // dimmed for the browser same as SWEEP_BRIGHTNESS is, but the ring's own
    // flash peaks at full white -- the instant a turn ends is exactly the
    // moment this board most wants your eye, same as a spawn strike.
    ringLevel = gaugeLevel(session, now, lerp(C.IDLE_BRIGHTNESS, C.ATTENTION_RING_LEVEL, fade));
    brightness = Math.max(brightness,
      lerp(C.IDLE_BRIGHTNESS, C.DONE_DEBT_CEILING, attentionDebt(session, now)));
  }

  // Reserved for nothing else, on every state: you should never have to wonder
  // which agent is running with permissions turned off.
  if (session.unsupervised) color = C.UNSUPERVISED_COLOR;

  return Cell({ color, anim, ring, brightness, ringLevel });
}

// --- the whole board -------------------------------------------------------

/** Loudest-first rank for one session, for arbitration and for bank following. */
export function attentionRank(session, now) {
  const i = C.STATE_PRIORITY.indexOf(session.state);
  return [i < 0 ? 99 : i, -attentionDebt(session, now)];
}

/* Invariant 4: one fast animation on the board at a time, always on the encoder
 * where a human is blocking. Motion is a budget -- peripheral vision catches
 * movement, so a board where everything moves is a board where nothing stands
 * out. Everything that loses is demoted to SLOW_ANIM, not silenced: it still
 * says "I am a kind of thing that blinks", just not loudly. */
export function arbitrateMotion(cells, sessions, now) {
  let winner = null, best = null;
  for (const [slot, s] of sessions) {
    const cell = cells.get(slot);
    if (!cell || !cell.anim) continue;
    const rank = attentionRank(s, now);
    if (best === null || rank[0] < best[0] || (rank[0] === best[0] && rank[1] < best[1])) {
      best = rank; winner = slot;
    }
  }
  for (const [slot, cell] of cells) {
    if (cell.anim && slot !== winner) cells.set(slot, Cell({ ...cell, anim: C.SLOW_ANIM }));
  }
  return winner;
}

/* Subagents pile up from the far corner in the parent's own bank, in violet. A
 * dot's brightness shimmers on its own phase so the pile reads as several
 * things rather than one block, and its ring is how long it has been out. */
export function subagentCell(sub, now, index) {
  const age = Math.max(0, now - sub.startedAt);
  // Each dot on its own phase, so the pile reads as several things rather than
  // one block. The band is the real one: idle to kick, never up to full --
  // a subagent is not a session and must not look like one.
  const phase = (Math.sin(now * (2 * Math.PI) / C.SUBAGENT_KICK_SECONDS + index * 1.9) + 1) / 2;
  return Cell({
    color: C.SUBAGENT_COLOR,
    ring: stopwatchRing(age, C.SUBAGENT_RING_FULL_SECONDS, C.SUBAGENT_RING_FLOOR),
    brightness: lerp(C.SUBAGENT_IDLE_BRIGHTNESS, C.SUBAGENT_KICK_BRIGHTNESS, phase),
  });
}

/* The slow breath under an otherwise empty board: the daemon is alive and the
 * board is not broken, at a level low enough that it never competes with a
 * session. Going dark because nothing is happening looks exactly like going
 * dark because the daemon died. */
export function ambient(now, slot) {
  const phase = (Math.sin((now * 2 * Math.PI) / C.AMBIENT_PERIOD - slot * 0.35) + 1) / 2;
  return Cell({ color: "azure", ring: 0, brightness: C.AMBIENT_BRIGHTNESS * phase * 0.6 });
}

/** The focused encoder holds its ring at full: a "you are here" on the map. */
/* Rebuilt through Cell() rather than spread, here and in the two functions
 * either side of it. `ringLight` is a getter over `ringLevel`, and a spread
 * evaluates it once and freezes the answer as a plain property — so patching
 * `ringLevel` onto a spread copy changed a number nothing downstream reads, and
 * twister.js's `cell.ringLight` went on returning the pre-patch value. Every
 * ring-brightness feature on this page was inert that way: the focus marker,
 * the ceiling below, the sleep fade. The hardware has its own version of this
 * bug (see the ring band note in CLAUDE.md), which is not a coincidence — both
 * are a write that succeeds and means nothing. */
export function markFocus(cells, slot) {
  const cell = cells.get(slot);
  if (cell) cells.set(slot, Cell({ ...cell, ringLevel: C.ATTENTION_RING_LEVEL }));
}

/** Rings are capped only on the states with nothing of their own to say at full
 *  brightness -- idle's resting pip and the like. Every state whose ring
 *  brightness carries a real signal (a block asking for you, a kick per tool
 *  call, the flash a turn ends on) is exempt, the same as the focus marker: the
 *  ceiling exists to leave the marker headroom, not to mute a signal. */
export function capRings(cells, sessions) {
  const EXEMPT = ["permission", "plan", "error", "waiting", "working", "done", "thinking", "streaming"];
  for (const [slot, cell] of cells) {
    const s = sessions.get(slot);
    const exempt = s && EXEMPT.includes(s.state);
    if (exempt || cell.ringLevel === C.ATTENTION_RING_LEVEL) continue;
    const light = cell.ringLevel === null ? cell.brightness : cell.ringLevel;
    if (light > C.RING_CEILING) cells.set(slot, Cell({ ...cell, ringLevel: C.RING_CEILING }));
  }
}

/** Blend `over` onto `under` by `t`, for an overlay handing back to the board. */
export function handover(under, over, t) {
  t = clamp01(t);
  return Cell({
    color: t < 0.5 ? over.color : under.color,
    anim: t < 0.5 ? over.anim : under.anim,
    ring: Math.round(lerp(over.ring, under.ring, t)),
    brightness: lerp(over.brightness, under.brightness, t),
    ringLevel: lerp(over.ringLight, under.ringLight, t),
  });
}
